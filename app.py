import io
import os
import re
import json
import time
import uuid
import asyncio
import pathlib
import threading
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlparse, parse_qs

from flask import Flask, Response, request, jsonify
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont, ImageOps

from ffproto import encrypt_fields_hex
from ffproto.crypto import decrypt_packet
import AccountPersonalShowInfo_pb2

import JwtGen
from JwtGen import ILoveYou

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =====================================================================
#  RENDER CONFIG
# =====================================================================
AVATAR_ZOOM = 1.00
AVATAR_SHIFT_Y = 0
AVATAR_SHIFT_X = 0
AVATAR_BORDER = 5
MAX_PRIME_LEVEL = 8

BASE_DIR = pathlib.Path(__file__).parent.absolute()
PIN_DIR = BASE_DIR / "pin"
PRIME_DIR = BASE_DIR / "prime"
FONT_FILE = BASE_DIR / "arial_unicode_bold.otf"
FONT_CHEROKEE = BASE_DIR / "NotoSansCherokee.ttf"
DATA_FILE = BASE_DIR / "data.json"

ICON_BASE = "https://freefiremobile-a.akamaihd.net/common/Local/PK/FF_UI_Icon"
PIN_BASE = "https://raw.githubusercontent.com/d8004316-sys/danger-icons/main/icons"
PRIME_BASE = "https://raw.githubusercontent.com/d8004316-sys/danger-icons/main"
BANNER_BASE = "https://raw.githubusercontent.com/d8004316-sys/danger-banners/main/banners"


# =====================================================================
#  ACCOUNTS  (region is NOT hardcoded — fetched from JwtGen)
# =====================================================================
ACCOUNTS = [
    {"uid": "8136263047", "password": "B4C932E05745CDCF4632C4926FE921D26591E8E847E7BF7BAC8822231DA8BCB9"},
    {"uid": "8173105159", "password": "B4C932E05745CDCF4632C4926FE921D26591E8E847E7BF7BAC8822231DA8BCB9"},
]

REGION_MAP = {}          # region -> account dict
_REGION_LOCK = threading.Lock()
_REGION_TS = 0.0
REGION_TTL = 45 * 60     # refresh JWTs every 45 minutes


# =====================================================================
#  FLASK APP
# =====================================================================
app = Flask(__name__)
CORS(app)

executor = ThreadPoolExecutor(max_workers=4)
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

if not DATA_FILE.exists():
    raise RuntimeError("data.json not found")
with open(DATA_FILE, "r", encoding="utf-8") as f:
    items = json.load(f)
ITEMS_LOOKUP = {item["itemID"]: item for item in items}


# =====================================================================
#  LOGIN / REGION MAP
# =====================================================================
def _login_all() -> dict:
    """Log every account in and return {REGION: account}."""
    async def _run():
        found = {}
        for acc in ACCOUNTS:
            print(f"[+] Logging in account {acc['uid']} ...")
            try:
                jwt, url, region, platform = await ILoveYou(
                    acc["uid"], acc["password"], False
                )
            except Exception as e:
                print(f"[!] Login failed for {acc['uid']}: {e}")
                continue

            if not jwt or not region:
                print(f"[!] Account {acc['uid']} got no JWT / region — skipping.")
                continue

            key = str(region).upper()
            found[key] = {
                "uid": acc["uid"],
                "password": acc["password"],
                "jwt": jwt,
                "url": url,
                "region": region,
                "platform": platform,
            }
            print(f"[+] Account {acc['uid']} -> region {key} (platform={platform})")
        return found

    try:
        return asyncio.run(_run())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()


def refresh_accounts(force: bool = False) -> dict:
    global REGION_MAP, _REGION_TS
    with _REGION_LOCK:
        fresh = REGION_MAP and (time.time() - _REGION_TS) < REGION_TTL
        if fresh and not force:
            return REGION_MAP

        new = _login_all()
        if new:
            REGION_MAP.update(new)
            _REGION_TS = time.time()
        elif REGION_MAP:
            _REGION_TS = time.time()
        else:
            raise RuntimeError("No usable accounts — all logins failed.")
        print(f"[+] Region map ready: {list(REGION_MAP.keys())}")
        return REGION_MAP


# =====================================================================
#  TOPUP.PK  ->  region lookup for a target UID
# =====================================================================
BASE_URL = "https://topup.pk"
LOGIN_URL = f"{BASE_URL}/api/auth/player_id_login"
APP_ID = 100067


def extract_captcha_params(url):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    cid = params.get("cid", [None])[0]
    ddk = params.get("hash", [None])[0]
    referer = params.get("referer", [None])[0]
    if referer:
        referer = unquote(referer)
    if not referer:
        referer = LOGIN_URL
    return cid, ddk, referer


def solve_datadome(cid, ddk, referer):
    url = "https://api-sdk.datadome.co/sdk/"
    payload = {
        "cid": cid, "ddv": "1.13.9", "ddvc": "4.0.41", "ddk": ddk,
        "request": referer, "os": "Android", "osr": "9", "osn": "P", "osv": "28",
        "ua": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
        "screen_x": "393", "screen_y": "858", "screen_d": "240",
        "events": '[{"id":1,"message":"response validation","source":"sdk","date":%d}]'
                  % int(time.time() * 1000),
        "camera": '{"auth":"false","info":"{}}"',
        "mdl": "SM-G973F", "prd": "SM-G973F", "mnf": "samsung",
        "dev": "star2qltechn", "hrd": "qcom",
        "fgp": "samsung/star2qltezh/star2qltechn:9/PQ3B.190801.03250903/"
               "G9650ZHU2ARC6:user/release-keys",
        "tgs": "release-keys", "inte": "android-java-okhttp",
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "User-Agent": "okhttp/4.9.1"}
    resp = requests.post(url, data=payload, headers=headers, timeout=15, verify=False)
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") == 200 and "cookie" in result:
        m = re.search(r"datadome=([^;]+)", result["cookie"])
        if m:
            return m.group(1)
    raise Exception("DataDome solving failed")


def get_player_info(uid):
    """Returns (region, nickname). Both may be None on failure."""
    try:
        s = requests.Session()
        s.cookies.set("source", "mb", domain=".topup.pk", path="/")
        s.cookies.set("region", "PK", domain=".topup.pk", path="/")
        s.cookies.set("language", "en", domain=".topup.pk", path="/")
        s.cookies.set("mspid2", uuid.uuid4().hex, domain=".topup.pk", path="/")

        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE_URL, "Referer": f"{BASE_URL}/",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Chromium";v="139", "Not;A=Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?1", "Sec-Ch-Ua-Platform": '"Android"',
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin", "Connection": "keep-alive",
        }
        s.get(BASE_URL, headers=headers, verify=False)

        payload = {"app_id": APP_ID, "login_id": str(uid)}

        for _ in range(3):
            resp = s.post(LOGIN_URL, json=payload, headers=headers, verify=False)

            if resp.status_code == 200:
                data = resp.json()
                print(f"[debug] topup.pk response: {data}")
                if "code" in data and data["code"] != 0:
                    print(f"[!] topup.pk error: {data}")
                    return None, None
                region = data.get("region")
                nickname = data.get("nickname")
                if (not region or region == "Unknown") and isinstance(data.get("data"), dict):
                    region = data["data"].get("region")
                return region, nickname

            if resp.status_code == 403:
                try:
                    j = resp.json()
                    if "url" in j and "geo.captcha-delivery.com" in j["url"]:
                        cid, ddk, referer = extract_captcha_params(j["url"])
                        if not cid or not ddk:
                            return None, None
                        dd = solve_datadome(cid, ddk, referer)
                        s.cookies.set("datadome", dd, domain=".topup.pk", path="/")
                        headers["Cookie"] = f"datadome={dd}"
                        continue
                    return None, None
                except Exception as e:
                    print(f"[!] Captcha handling failed: {e}")
                    return None, None

            print(f"[!] topup.pk status {resp.status_code}: {resp.text[:200]}")
            return None, None

    except Exception as e:
        print(f"[!] topup.pk lookup failed: {e}")
        return None, None

    return None, None


# =====================================================================
#  Fetch player info using the region-matched account
# =====================================================================
def _do_fetch(acc, target_uid):
    jwt = acc["jwt"]
    fields = {1: target_uid, 2: 10, 3: 1, 4: 1, 5: 1, 6: 1}
    data = bytes.fromhex(encrypt_fields_hex(fields))

    base_url = (acc["url"] or "https://client.ind.freefiremobile.com").rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    endpoint = f"{base_url}/GetPlayerPersonalShow"

    headers = {
        "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
        "Accept": "*/*",
        "Accept-Encoding": "deflate, gzip",
        "X-GA-SV": "1790187833",
        "Authorization": f"Bearer {jwt}",
        "X-GA": "v1 1",
        "ReleaseVersion": JwtGen.Ob_VerSion,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": "2018.4.12f1",
        "Content-Length": str(len(data)),
    }

    print(f"[+] Trying account {acc['uid']} (region {acc['region']}) -> {endpoint}")
    try:
        req = requests.post(endpoint, headers=headers, data=data, verify=False, timeout=20)
    except Exception as e:
        print(f"[!] request error: {e}")
        return None

    if req.status_code != 200:
        print(f"[!] HTTP {req.status_code} — {req.content[:120]!r}")
        return None

    try:
        plaintext = decrypt_packet(req.content)
    except Exception:
        plaintext = req.content

    info = AccountPersonalShowInfo_pb2.AccountPersonalShowInfo()
    try:
        info.ParseFromString(plaintext)
    except Exception as e:
        print(f"[!] protobuf parse error: {e}")
        return None

    if not info.HasField("basic_info"):
        print(f"[debug] no basic_info in response from {acc['uid']}")
        return None

    return info


def fetch_player_info(target_uid: int, hinted_region: str | None):
    """Sync version — tries the hinted region first, then every account."""
    region_map = refresh_accounts()
    tried = set()

    if hinted_region:
        key = str(hinted_region).upper()
        if key in region_map:
            acc = region_map[key]
            tried.add(acc["uid"])
            info = _do_fetch(acc, target_uid)
            if info is not None:
                return info
            print(f"[!] Account {acc['uid']} returned nothing — falling back.")

    print("[+] Falling back to trying all accounts...")
    for _, acc in region_map.items():
        if acc["uid"] in tried:
            continue
        info = _do_fetch(acc, target_uid)
        if info is not None:
            return info

    raise Exception("Could not fetch player info with any configured account.")


# =====================================================================
#  PROTOBUF FIELD EXTRACTION
#  NOTE: proto3 scalars have NO presence -> read directly.
#        Only message fields use HasField().
# =====================================================================
def extract_profile_fields(info) -> dict:
    out = {}

    if not info.HasField("basic_info"):
        return out

    basic = info.basic_info  # AccountInfoBasic (scalars, proto3)

    if basic.nickname:
        out["nickname"] = basic.nickname
    if basic.region:
        out["region"] = basic.region
    if basic.level:
        out["level"] = basic.level
    if basic.head_pic:
        out["avatar_id"] = basic.head_pic
    if basic.banner_id:
        out["banner_id"] = basic.banner_id
    if basic.pin_id:
        out["pin_id"] = basic.pin_id
    if basic.avatar_frame:
        out["avatar_frame"] = basic.avatar_frame
    if basic.clan_id:
        out["clan_id"] = basic.clan_id

    if basic.HasField("prime_privilege_detail"):
        ppd = basic.prime_privilege_detail
        if ppd.prime_level:
            out["prime_level"] = ppd.prime_level

    if info.HasField("clan_basic_info"):
        cb = info.clan_basic_info
        if cb.clan_id and "clan_id" not in out:
            out["clan_id"] = cb.clan_id
        if cb.clan_level:
            out["clan_level"] = cb.clan_level

    return out


# =====================================================================
#  ICON DOWNLOAD / LOOKUP HELPERS
# =====================================================================
def download_and_save(url: str, filepath: pathlib.Path) -> bool:
    try:
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(resp.content)
        return True
    except Exception:
        return False


def download_all_pins_and_primes():
    pin_items = [item for item in items if item.get("type") == "Pins"]
    for pin in pin_items:
        pin_id = pin["itemID"]
        filepath = PIN_DIR / f"{pin_id}.png"
        if not filepath.exists():
            url = f"{PIN_BASE}/{pin_id}.png"
            if download_and_save(url, filepath):
                print(f"Downloaded pin {pin_id}")
            else:
                print(f"Failed to download pin {pin_id}")

    for level in range(1, MAX_PRIME_LEVEL + 1):
        filepath = PRIME_DIR / f"prime_{level}.png"
        if not filepath.exists():
            url = f"{PRIME_BASE}/FF_VIP_{level}.png"
            if download_and_save(url, filepath):
                print(f"Downloaded prime level {level}")
            else:
                print(f"Failed to download prime level {level}")


def get_item_icon(item_id) -> str | None:
    try:
        item = ITEMS_LOOKUP.get(int(item_id))
    except (TypeError, ValueError):
        return None
    return item["icon"] if item else None


def fetch_image(url: str) -> bytes:
    resp = session.get(url, timeout=10)
    resp.raise_for_status()
    return resp.content


def get_pin_image(pin_id) -> bytes | None:
    if not pin_id or str(pin_id) == "0":
        return None
    filepath = PIN_DIR / f"{pin_id}.png"
    if filepath.exists():
        with open(filepath, "rb") as f:
            return f.read()
    url = f"{PIN_BASE}/{pin_id}.png"
    if download_and_save(url, filepath):
        with open(filepath, "rb") as f:
            return f.read()
    return None


def get_prime_image(prime_level) -> bytes | None:
    try:
        prime_level = int(prime_level)
    except (TypeError, ValueError):
        return None
    if not (1 <= prime_level <= MAX_PRIME_LEVEL):
        return None
    filepath = PRIME_DIR / f"prime_{prime_level}.png"
    if filepath.exists():
        with open(filepath, "rb") as f:
            return f.read()
    url = f"{PRIME_BASE}/FF_VIP_{prime_level}.png"
    if download_and_save(url, filepath):
        with open(filepath, "rb") as f:
            return f.read()
    return None


# =====================================================================
#  IMAGE RENDERING
# =====================================================================
def bytes_to_image(img_bytes):
    if img_bytes:
        return Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    return Image.new("RGBA", (100, 100), (0, 0, 0, 0))


def load_unicode_font(size, font_file):
    try:
        if os.path.exists(font_file):
            return ImageFont.truetype(str(font_file), size)
    except Exception:
        pass
    return ImageFont.load_default()


def is_cherokee(c):
    return 0x13A0 <= ord(c) <= 0x13FF or 0xAB70 <= ord(c) <= 0xABBF


def process_banner_image(data, avatar_bytes, banner_bytes, pin_bytes, prime_bytes):
    avatar_img = bytes_to_image(avatar_bytes)
    banner_img = bytes_to_image(banner_bytes)
    pin_img = bytes_to_image(pin_bytes) if pin_bytes else None
    prime_img = bytes_to_image(prime_bytes) if prime_bytes else None

    level = str(data.get("level", "0"))
    name = data.get("nickname", "Unknown")
    guild = data.get("guildName", "")

    TARGET_HEIGHT = 400

    zoom_size = int(TARGET_HEIGHT * AVATAR_ZOOM)
    avatar_img = avatar_img.resize((zoom_size, zoom_size), Image.LANCZOS)

    center_x = zoom_size // 2
    center_y = zoom_size // 2
    half_target = TARGET_HEIGHT // 2

    left = center_x - half_target - AVATAR_SHIFT_X
    top = center_y - half_target - AVATAR_SHIFT_Y
    right = left + TARGET_HEIGHT
    bottom = top + TARGET_HEIGHT

    avatar_img = avatar_img.crop((left, top, right, bottom))
    avatar_img = ImageOps.expand(avatar_img, border=AVATAR_BORDER, fill="white")

    av_w, av_h = avatar_img.size

    b_w, b_h = banner_img.size
    if b_w > 50 and b_h > 50:
        new_banner_h = TARGET_HEIGHT
        new_banner_w = int((b_w / b_h) * new_banner_h)
        banner_img = banner_img.resize((new_banner_w, new_banner_h), Image.LANCZOS)
    else:
        new_banner_w = 800
        banner_img = banner_img.resize((new_banner_w, TARGET_HEIGHT), Image.LANCZOS)

    b_w, b_h = banner_img.size
    final_w = av_w + b_w
    combined = Image.new("RGBA", (final_w, TARGET_HEIGHT))

    combined.paste(avatar_img, (0, 0))
    combined.paste(banner_img, (av_w, 0))

    draw = ImageDraw.Draw(combined)

    font_large = load_unicode_font(125, FONT_FILE)
    font_large_cherokee = load_unicode_font(125, FONT_CHEROKEE)
    font_small = load_unicode_font(95, FONT_FILE)
    font_small_cherokee = load_unicode_font(95, FONT_CHEROKEE)
    font_level = load_unicode_font(50, FONT_FILE)

    def draw_text(x, y, text, f_main, f_alt, stroke):
        cx = x
        for ch in text:
            f = f_alt if is_cherokee(ch) else f_main
            for dx in range(-stroke, stroke + 1):
                for dy in range(-stroke, stroke + 1):
                    if dx != 0 or dy != 0:
                        draw.text((cx + dx, y + dy), ch, font=f, fill="black")
            draw.text((cx, y), ch, font=f, fill="white")
            cx += f.getlength(ch)

    draw_text(av_w + 65, 10, str(name), font_large, font_large_cherokee, 4)
    draw_text(av_w + 65, 240, str(guild or ""), font_small, font_small_cherokee, 3)

    if pin_img and pin_img.size != (100, 100):
        pin_img = pin_img.resize((130, 130), Image.LANCZOS)
        combined.paste(pin_img, (0, TARGET_HEIGHT - 130), pin_img)

    if prime_img:
        prime_img = prime_img.resize((150, 150), Image.LANCZOS)
        combined.paste(prime_img, (275, 0), prime_img)

    lvl_text = f"Lvl.{level}"
    bbox = draw.textbbox((0, 0), lvl_text, font=font_level)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]

    stroke = 3
    for dx in range(-stroke, stroke + 1):
        for dy in range(-stroke, stroke + 1):
            if dx != 0 or dy != 0:
                draw.text(
                    (final_w - w - 30 + dx, TARGET_HEIGHT - h - 40 + dy),
                    lvl_text, font=font_level, fill="black"
                )
    draw.text(
        (final_w - w - 30, TARGET_HEIGHT - h - 40),
        lvl_text, font=font_level, fill="white"
    )

    img_io = io.BytesIO()
    combined.save(img_io, "PNG", optimize=True)
    img_io.seek(0)
    return img_io


# =====================================================================
#  BANNER BUILD PIPELINE
#  Primary: danger-banners repo -> Fallback: Akamai CDN
# =====================================================================
def build_banner_bytes(avatar_id, banner_id, pin_id, prime_level, level, name, guild):
    avatar_icon = get_item_icon(avatar_id)
    banner_icon = get_item_icon(banner_id)

    if not avatar_icon:
        raise LookupError(f"Avatar ID {avatar_id} not found in data.json")
    if not banner_icon:
        raise LookupError(f"Banner ID {banner_id} not found in data.json")

    # --- Avatar: CDN -> GitHub icons mirror ---
    def fetch_avatar_with_fallback(icon_name, item_id):
        primary_url = f"{ICON_BASE}/{icon_name}.png"
        try:
            print(f"[+] Avatar CDN: {primary_url}")
            return fetch_image(primary_url)
        except Exception as e:
            print(f"[!] Avatar CDN failed: {e}")

        fallback_url = f"{PIN_BASE}/{item_id}.png"
        try:
            print(f"[+] Avatar fallback (icons): {fallback_url}")
            return fetch_image(fallback_url)
        except Exception as e:
            raise RuntimeError(
                f"Could not fetch avatar for item_id {item_id} "
                f"from CDN or GitHub icons mirror."
            )

    # --- Banner: danger-banners repo (primary) -> Akamai CDN (fallback) ---
    def fetch_banner_with_fallback(item_id, icon_name_base):
        # 1. PRIMARY: danger-banners repo
        banners_url = f"{BANNER_BASE}/{item_id}.png"
        try:
            print(f"[+] Banner (danger-banners): {banners_url}")
            return fetch_image(banners_url)
        except Exception as e:
            print(f"[!] danger-banners failed: {e}")

        # 2. FALLBACK: original Akamai CDN
        cdn_url = f"{ICON_BASE}/{icon_name_base}.png"
        try:
            print(f"[+] Banner fallback (CDN): {cdn_url}")
            return fetch_image(cdn_url)
        except Exception as e:
            print(f"[!] Banner CDN failed: {e}")

        # 3. LAST RESORT: GitHub icons mirror
        icons_url = f"{PIN_BASE}/{item_id}.png"
        try:
            print(f"[+] Banner fallback (icons): {icons_url}")
            return fetch_image(icons_url)
        except Exception as e:
            raise RuntimeError(
                f"Could not fetch banner for item_id {item_id} "
                f"from danger-banners, CDN, or icons mirror."
            )

    avatar_bytes = fetch_avatar_with_fallback(avatar_icon, avatar_id)

    banner_icon_base = banner_icon.replace("storebg", "basebg")
    banner_bytes = fetch_banner_with_fallback(banner_id, banner_icon_base)

    # --- Pin & Prime ---
    try:
        pin_bytes = get_pin_image(pin_id)
    except Exception:
        pin_bytes = None

    try:
        prime_bytes = get_prime_image(prime_level)
    except Exception:
        prime_bytes = None

    banner_data = {
        "level": level,
        "nickname": name,
        "guildName": guild,
    }

    return executor.submit(
        process_banner_image,
        banner_data,
        avatar_bytes,
        banner_bytes,
        pin_bytes,
        prime_bytes,
    ).result(timeout=30)


# =====================================================================
#  ROUTES
# =====================================================================
@app.route("/")
def home():
    return jsonify({
        "status": "Banner API — auto profile fetch from UID",
        "endpoint": "/profile?uid=123456789",
        "optional_overrides": [
            "level", "name", "guild", "avatar_id",
            "banner_id", "pin_id", "prime_level"
        ],
        "debug_endpoint": "/raw?uid=123456789",
        "note": f"prime_level 1–{MAX_PRIME_LEVEL}; "
                f"avatar zoom={AVATAR_ZOOM}, border={AVATAR_BORDER}px",
    })


@app.route("/profile")
def get_banner():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"error": "uid is required"}), 400

    try:
        target_uid = int(uid)
    except ValueError:
        return jsonify({"error": "uid must be numeric"}), 400

    hinted_region, nickname_hint = get_player_info(target_uid)

    try:
        info = fetch_player_info(target_uid, hinted_region)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    fields = extract_profile_fields(info)

    level = request.args.get("level") or fields.get("level") or 0

    name = (
        request.args.get("name")
        or fields.get("nickname")
        or nickname_hint
        or "Unknown"
    )

    guild = request.args.get("guild")
    if guild is None:
        guild = ""

    avatar_id = request.args.get("avatar_id") or fields.get("avatar_id")
    banner_id = request.args.get("banner_id") or fields.get("banner_id")
    pin_id = request.args.get("pin_id") or fields.get("pin_id")

    prime_level = request.args.get("prime_level", type=int)
    if prime_level is None:
        prime_level = fields.get("prime_level")
    if prime_level is not None:
        try:
            prime_level = int(prime_level)
        except (TypeError, ValueError):
            prime_level = None
    if prime_level is not None and not (1 <= prime_level <= MAX_PRIME_LEVEL):
        prime_level = None

    if not avatar_id or not banner_id:
        return jsonify({
            "error": "Could not resolve avatar/banner from profile.",
            "extracted": {k: str(v) for k, v in fields.items()},
            "hint": "Pass avatar_id / banner_id manually if needed.",
        }), 422

    try:
        img_io = build_banner_bytes(
            avatar_id, banner_id, pin_id, prime_level, level, name, guild
        )
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return Response(
        img_io.getvalue(),
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.route("/raw")
def raw_profile():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"error": "uid is required"}), 400

    try:
        target_uid = int(uid)
    except ValueError:
        return jsonify({"error": "uid must be numeric"}), 400

    hinted_region, nickname_hint = get_player_info(target_uid)
    try:
        info = fetch_player_info(target_uid, hinted_region)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({
        "uid": target_uid,
        "hinted_region": hinted_region,
        "nickname_hint": nickname_hint,
        "extracted": {k: str(v) for k, v in extract_profile_fields(info).items()},
        "basic_info": str(info.basic_info) if info.HasField("basic_info") else None,
    })


# =====================================================================
#  STARTUP
# =====================================================================
def _startup():
    print("Downloading missing pins and primes...")
    download_all_pins_and_primes()
    print("Startup download complete.")

    print("Logging in accounts...")
    try:
        refresh_accounts(force=True)
    except Exception as e:
        print(f"[!] Initial login failed: {e} "
              f"(will retry lazily on first request)")


_startup()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
