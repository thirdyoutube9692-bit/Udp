# language: Python, file: banner_renderer.py
# FF profile banner generator — piggybacks on master.py's live session.
# Uses bot_state.account_credentials to grab JWT + server_url per UID,
# calls GetPlayerPersonalShow, renders avatar|banner composite as PNG.

import io
import os
import json
import pathlib
import asyncio
from typing import Optional, Dict, Any

import requests
import urllib3
from PIL import Image, ImageDraw, ImageFont, ImageOps
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from AccountPersonalShowInfo_pb2 import AccountPersonalShowInfo

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------- paths ----------
BASE_DIR  = pathlib.Path(__file__).parent.absolute()
ASSETS    = BASE_DIR / "banner_assets"
DATA_FILE = ASSETS / "data.json"
PIN_DIR   = ASSETS / "pin"
PRIME_DIR = ASSETS / "prime"
FONT_MAIN = ASSETS / "arial_unicode_bold.otf"
FONT_CHER = ASSETS / "NotoSansCherokee.ttf"

# ---------- crypto (matches master.py) ----------
AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV  = b'6oyZDr22E3ychjM%'

# ---------- CDN bases ----------
ICON_BASE   = "https://freefiremobile-a.akamaihd.net/common/Local/PK/FF_UI_Icon"
PIN_BASE    = "https://raw.githubusercontent.com/d8004316-sys/danger-icons/main/icons"
PRIME_BASE  = "https://raw.githubusercontent.com/d8004316-sys/danger-icons/main"
BANNER_BASE = "https://raw.githubusercontent.com/d8004316-sys/danger-banners/main/banners"

# ---------- render constants ----------
TARGET_HEIGHT   = 400
AVATAR_BORDER   = 5
AVATAR_ZOOM     = 1.00
MAX_PRIME_LEVEL = 8

# ---------- HTTP session ----------
_sess = requests.Session()
_sess.headers.update({"User-Agent": "Mozilla/5.0"})

# ---------- item lookup (lazy) ----------
_ITEMS: Optional[Dict[int, dict]] = None


def _items() -> Dict[int, dict]:
    global _ITEMS
    if _ITEMS is None:
        if not DATA_FILE.exists():
            raise RuntimeError("data.json missing at %s" % DATA_FILE)
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            _ITEMS = {i["itemID"]: i for i in json.load(f)}
    return _ITEMS


# ---------- protobuf helpers ----------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def _build_proto(fields: dict) -> bytes:
    parts = []
    for k, v in fields.items():
        if isinstance(v, dict):
            nested = _build_proto(v)
            parts.append(_varint((k << 3) | 2) + _varint(len(nested)) + nested)
        elif isinstance(v, int):
            parts.append(_varint((k << 3) | 0) + _varint(v))
        elif isinstance(v, (str, bytes)):
            ev = v.encode() if isinstance(v, str) else v
            parts.append(_varint((k << 3) | 2) + _varint(len(ev)) + ev)
    return b"".join(parts)


def _aes_encrypt(plain: bytes) -> bytes:
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(pad(plain, 16))


def _aes_decrypt(blob: bytes) -> Optional[bytes]:
    try:
        return unpad(AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(blob), 16)
    except Exception:
        return None


# ---------- profile fetch ----------
def fetch_profile(jwt: str, server_url: str, target_uid: int) -> Optional[AccountPersonalShowInfo]:
    body = _aes_encrypt(_build_proto({1: target_uid, 2: 10, 3: 1, 4: 1, 5: 1, 6: 1}))

    base = (server_url or "").rstrip("/")
    if base and not base.startswith("http"):
        base = "https://" + base
    if not base:
        base = "https://client.ind.freefiremobile.com"
    url = base + "/GetPlayerPersonalShow"

    headers = {
        "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
        "Accept": "*/*",
        "Accept-Encoding": "deflate, gzip",
        "X-GA-SV": "1790187833",
        "Authorization": "Bearer " + jwt,
        "X-GA": "v1 1",
        "ReleaseVersion": "OB55",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": "2018.4.12f1",
    }

    try:
        r = _sess.post(url, headers=headers, data=body, verify=False, timeout=20)
    except Exception as e:
        print("[banner] request error: %s" % e)
        return None

    if r.status_code != 200:
        print("[banner] HTTP %s: %r" % (r.status_code, r.content[:120]))
        return None

    content = r.content

    candidates = [content]
    dec = _aes_decrypt(content)
    if dec:
        candidates.append(dec)
    for off in (64, 40, 20):
        if off < len(content):
            candidates.append(content[off:])
            d2 = _aes_decrypt(content[off:])
            if d2:
                candidates.append(d2)

    # Parse every candidate, keep the one with most populated fields.
    best = None
    best_score = 0
    for blob in candidates:
        p = AccountPersonalShowInfo()
        try:
            p.ParseFromString(blob)
        except Exception:
            continue
        if not p.HasField("basic_info"):
            continue
        b = p.basic_info
        score = 0
        if b.nickname:  score += 1
        if b.level:     score += 1
        if b.head_pic:  score += 3   # weighted — avatar is what we need most
        if b.banner_id: score += 3   # weighted — banner too
        if b.pin_id:    score += 1
        if b.HasField("prime_privilege_detail") and b.prime_privilege_detail.prime_level:
            score += 1
        # bonus if the parsed size looks plausible (>100 bytes)
        if len(blob) > 100:
            score += 1

        print("[banner] candidate offset parse: score=%d nickname=%r level=%d avatar=%d banner=%d pin=%d"
              % (score, b.nickname[:20] if b.nickname else "", b.level,
                 b.head_pic, b.banner_id, b.pin_id))

        if score > best_score:
            best_score = score
            best = p

    if best is None:
        print("[banner] no candidate parsed successfully")
    else:
        print("[banner] best parse score=%d" % best_score)
    return best


def extract_fields(info: AccountPersonalShowInfo) -> dict:
    out = {}
    if not info.HasField("basic_info"):
        return out
    b = info.basic_info
    if b.nickname:  out["nickname"]  = b.nickname
    if b.level:     out["level"]     = b.level
    if b.head_pic:  out["avatar_id"] = b.head_pic
    if b.banner_id: out["banner_id"] = b.banner_id
    if b.pin_id:    out["pin_id"]    = b.pin_id
    if b.HasField("prime_privilege_detail"):
        pl = b.prime_privilege_detail.prime_level
        if pl:
            out["prime_level"] = pl
    if info.HasField("clan_basic_info"):
        cb = info.clan_basic_info
        if cb.clan_id:    out["clan_id"]    = cb.clan_id
        if cb.clan_level: out["clan_level"] = cb.clan_level
    return out


# ---------- image fetch helpers ----------
def _get(url: str) -> Optional[bytes]:
    try:
        r = _sess.get(url, timeout=10)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def _get_icon(item_id) -> Optional[str]:
    try:
        it = _items().get(int(item_id))
    except (TypeError, ValueError):
        return None
    return it["icon"] if it else None


def _fetch_avatar(avatar_id) -> bytes:
    icon = _get_icon(avatar_id)
    if not icon:
        raise LookupError("avatar_id %s not in data.json" % avatar_id)
    b = _get("%s/%s.png" % (ICON_BASE, icon))
    if b:
        return b
    b = _get("%s/%s.png" % (PIN_BASE, avatar_id))
    if b:
        return b
    raise RuntimeError("avatar %s unreachable from CDN or mirror" % avatar_id)


def _fetch_banner(banner_id) -> bytes:
    icon = _get_icon(banner_id)
    if not icon:
        raise LookupError("banner_id %s not in data.json" % banner_id)
    b = _get("%s/%s.png" % (BANNER_BASE, banner_id))
    if b:
        return b
    b = _get("%s/%s.png" % (ICON_BASE, icon.replace("storebg", "basebg")))
    if b:
        return b
    b = _get("%s/%s.png" % (PIN_BASE, banner_id))
    if b:
        return b
    raise RuntimeError("banner %s unreachable from all mirrors" % banner_id)


def _fetch_pin(pin_id) -> Optional[bytes]:
    if not pin_id or str(pin_id) == "0":
        return None
    fp = PIN_DIR / ("%s.png" % pin_id)
    if fp.exists():
        return fp.read_bytes()
    b = _get("%s/%s.png" % (PIN_BASE, pin_id))
    if b:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(b)
        return b
    return None


def _fetch_prime(level) -> Optional[bytes]:
    try:
        level = int(level)
    except (TypeError, ValueError):
        return None
    if not (1 <= level <= MAX_PRIME_LEVEL):
        return None
    fp = PRIME_DIR / ("prime_%s.png" % level)
    if fp.exists():
        return fp.read_bytes()
    b = _get("%s/FF_VIP_%s.png" % (PRIME_BASE, level))
    if b:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(b)
        return b
    return None


# ---------- rendering ----------
def _bytes_to_img(b: Optional[bytes], default=(100, 100)) -> Image.Image:
    if b:
        return Image.open(io.BytesIO(b)).convert("RGBA")
    return Image.new("RGBA", default, (0, 0, 0, 0))


def _font(path: pathlib.Path, size: int):
    try:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    except Exception:
        pass
    return ImageFont.load_default()


def _is_cherokee(ch: str) -> bool:
    o = ord(ch)
    return 0x13A0 <= o <= 0x13FF or 0xAB70 <= o <= 0xABBF


def _draw_stroked_text(draw, x, y, text, f_main, f_alt, stroke, fill="white"):
    cx = x
    for ch in text:
        f = f_alt if _is_cherokee(ch) else f_main
        for dx in range(-stroke, stroke + 1):
            for dy in range(-stroke, stroke + 1):
                if dx or dy:
                    draw.text((cx + dx, y + dy), ch, font=f, fill="black")
        draw.text((cx, y), ch, font=f, fill=fill)
        cx += f.getlength(ch)


def render_banner(info: AccountPersonalShowInfo, overrides: Optional[Dict[str, Any]] = None) -> bytes:
    overrides = overrides or {}
    fields = extract_fields(info)

    level = overrides.get("level") or fields.get("level") or 0
    name  = overrides.get("name")  or fields.get("nickname") or "Unknown"
    guild = overrides.get("guild") or ""
    avatar_id = overrides.get("avatar_id") or fields.get("avatar_id")
    banner_id = overrides.get("banner_id") or fields.get("banner_id")
    pin_id    = overrides.get("pin_id")    or fields.get("pin_id")
    prime_lvl = overrides.get("prime_level")
    if prime_lvl is None:
        prime_lvl = fields.get("prime_level")

    # Fallback to defaults when profile has no custom avatar/banner set
    # Nulla (101000001) is always in data.json
    if not avatar_id:
        avatar_id = 101000001
        print("[banner] avatar_id missing in profile, using default 101000001")
    if not banner_id:
        banner_id = 901000001
        print("[banner] banner_id missing in profile, using default 901000001")

    avatar_bytes = _fetch_avatar(avatar_id)
    banner_bytes = _fetch_banner(banner_id)
    pin_bytes    = _fetch_pin(pin_id)
    prime_bytes  = _fetch_prime(prime_lvl)

    # avatar: zoom + center-crop + white border
    av = _bytes_to_img(avatar_bytes)
    zs = int(TARGET_HEIGHT * AVATAR_ZOOM)
    av = av.resize((zs, zs), Image.LANCZOS)
    cx = cy = zs // 2
    half = TARGET_HEIGHT // 2
    av = av.crop((cx - half, cy - half, cx + half, cy + half))
    av = ImageOps.expand(av, border=AVATAR_BORDER, fill="white")
    av_w, _ = av.size

    # banner: scale to TARGET_HEIGHT
    bg = _bytes_to_img(banner_bytes)
    bw, bh = bg.size
    if bw > 50 and bh > 50:
        nw = int((bw / bh) * TARGET_HEIGHT)
        bg = bg.resize((nw, TARGET_HEIGHT), Image.LANCZOS)
    else:
        bg = bg.resize((800, TARGET_HEIGHT), Image.LANCZOS)
    bw, _ = bg.size

    combined = Image.new("RGBA", (av_w + bw, TARGET_HEIGHT))
    combined.paste(av, (0, 0))
    combined.paste(bg, (av_w, 0))
    draw = ImageDraw.Draw(combined)

    f_name   = _font(FONT_MAIN, 125)
    f_name_c = _font(FONT_CHER, 125)
    f_guild  = _font(FONT_MAIN, 95)
    f_guild_c= _font(FONT_CHER, 95)
    f_lvl    = _font(FONT_MAIN, 50)

    _draw_stroked_text(draw, av_w + 65, 10, str(name), f_name, f_name_c, 4)
    _draw_stroked_text(draw, av_w + 65, 240, str(guild or ""), f_guild, f_guild_c, 3)

    if pin_bytes:
        pin_img = _bytes_to_img(pin_bytes).resize((130, 130), Image.LANCZOS)
        combined.paste(pin_img, (0, TARGET_HEIGHT - 130), pin_img)

    if prime_bytes:
        pi = _bytes_to_img(prime_bytes).resize((150, 150), Image.LANCZOS)
        combined.paste(pi, (275, 0), pi)

    lvl_text = "Lvl.%s" % level
    bbox = draw.textbbox((0, 0), lvl_text, font=f_lvl)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    final_w = av_w + bw
    _draw_stroked_text(draw, final_w - tw - 30, TARGET_HEIGHT - th - 40,
                       lvl_text, f_lvl, f_lvl, 3)

    out = io.BytesIO()
    combined.save(out, "PNG", optimize=True)
    return out.getvalue()


# ---------- high-level ----------
async def render_banner_for_uid(uid: str, bot_state, overrides=None) -> bytes:
    loop = asyncio.get_running_loop()
    uid = str(uid)

    creds = bot_state.account_credentials.get(uid)
    if not creds:
        for k, v in bot_state.account_credentials.items():
            if str(v.get("account_id", "")) == uid:
                creds = v
                break

    if not creds:
        raise LookupError("uid %s not in running sessions" % uid)

    jwt        = creds.get("token")
    server_url = creds.get("server_url") or creds.get("url")
    acc_id     = creds.get("account_id")

    if not jwt or not acc_id:
        raise RuntimeError("uid %s has no JWT - is the worker logged in?" % uid)

    def _work():
        info = fetch_profile(jwt, server_url, int(acc_id))
        if info is None:
            raise RuntimeError("GetPlayerPersonalShow returned no profile")
        return render_banner(info, overrides=overrides)

    return await loop.run_in_executor(None, _work)
