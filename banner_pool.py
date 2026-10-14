# language: Python, file: banner_pool.py
# Pool of FF accounts used ONLY for fetching arbitrary profiles.
# Uses JwtGen.ILoveYou to login, decodes JWT for account_id + expiry,
# caches the JWT until it expires, then re-logins.

import os
import sys
import time
import json
import base64
import pathlib
import asyncio
import types
from typing import Optional, Dict, Any, List


# ---------- ffproto stub (JwtGen imports it; we don't use it) ----------
try:
    import ffproto  # noqa
except ImportError:
    _stub = types.ModuleType("ffproto")
    def _noop(*a, **kw):
        return {}
    _stub.get_freefire_update_info = _noop
    sys.modules["ffproto"] = _stub


# ---------- JwtGen (module-level FreeFireVer() runs on import) ----------
try:
    import JwtGen as _jg
    from JwtGen import ILoveYou
    _JWTGEN_OK = True
except Exception as _e:
    print("[pool] JwtGen import failed: %s" % _e)
    ILoveYou = None
    _jg = None
    _JWTGEN_OK = False


BASE        = pathlib.Path(__file__).parent
POOL_FILE   = BASE / "banner_pool.json"
STATE_FILE  = BASE / "banner_pool_state.json"
FIELDS_FILE = BASE / "banner_fields.json"

_lock: Optional[asyncio.Lock] = None
_state_cache: Optional[Dict[str, Dict[str, Any]]] = None


# ==================== JWT decoding ====================
def jwt_payload(jwt_str: str) -> Dict[str, Any]:
    try:
        parts = jwt_str.split(".")
        if len(parts) < 2:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        return json.loads(raw)
    except Exception:
        return {}


def jwt_expiry(jwt_str: str) -> int:
    p = jwt_payload(jwt_str)
    for k in ("exp", "expire", "expires_at", "eat"):
        v = p.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def jwt_account_id(jwt_str: str) -> Optional[int]:
    p = jwt_payload(jwt_str)
    for k in ("account_id", "aid", "sub", "uid", "user_id"):
        v = p.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _fmt_dur(sec: int) -> str:
    sec = max(0, int(sec))
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


# ==================== config ====================
def load_pool() -> List[Dict[str, str]]:
    if not POOL_FILE.exists():
        return []
    try:
        d = json.loads(POOL_FILE.read_text())
        if isinstance(d, dict):
            return d.get("accounts", [])
        if isinstance(d, list):
            return d
    except Exception as e:
        print("[pool] config parse fail: %s" % e)
    return []


def jwt_buffer() -> int:
    try:
        d = json.loads(POOL_FILE.read_text())
        return int(d.get("jwt_buffer_seconds", 300))
    except Exception:
        return 300


# ==================== state persistence ====================
def _load_state(force_reload: bool = False) -> Dict[str, Dict[str, Any]]:
    global _state_cache
    if _state_cache is not None and not force_reload:
        return _state_cache
    if not STATE_FILE.exists():
        _state_cache = {}
        return _state_cache
    try:
        _state_cache = json.loads(STATE_FILE.read_text())
    except Exception:
        _state_cache = {}
    return _state_cache


def _save_state(state: Dict[str, Dict[str, Any]]):
    global _state_cache
    _state_cache = state
    tmp = str(STATE_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ==================== login ====================
async def _login(uid: str, password: str) -> Optional[Dict[str, Any]]:
    if not _JWTGEN_OK or ILoveYou is None:
        print("[pool] JwtGen unavailable, cannot login")
        return None

    print("[pool] login start uid=%s" % uid)
    try:
        jwt, url, region, platform = await ILoveYou(uid, password, False)
    except Exception as e:
        print("[pool] ILoveYou exception for %s: %s" % (uid, e))
        return None

    if not jwt or not url:
        print("[pool] login failed for %s (no jwt/url)" % uid)
        return None

    aid = jwt_account_id(jwt)
    exp = jwt_expiry(jwt)
    rel = getattr(_jg, "Ob_VerSion", "OB55") if _jg else "OB55"

    entry = {
        "uid": uid,
        "password": password,
        "jwt": jwt,
        "token": jwt,                # alias for renderer
        "url": url,
        "server_url": url,           # alias
        "region": str(region or "IND").upper(),
        "platform": platform,
        "account_id": aid,
        "release_version": rel,
        "exp_ts": exp,
        "cached_at": time.time(),
    }

    print("[pool] login OK uid=%s account_id=%s region=%s release=%s exp_in=%s"
          % (uid, aid, entry["region"], rel, _fmt_dur(exp - time.time()) if exp else "unknown"))
    return entry


def _is_valid(entry: Dict[str, Any]) -> bool:
    exp = int(entry.get("exp_ts") or 0)
    buf = jwt_buffer()
    if exp <= 0:
        # no exp claim — 20 min fallback
        return (time.time() - entry.get("cached_at", 0)) < 1200
    return (exp - time.time()) > buf


# ==================== public API ====================
async def get_pool_session() -> Optional[Dict[str, Any]]:
    """Return valid pool session. Reuses cached JWT until near expiry."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()

    async with _lock:
        state = _load_state(force_reload=True)

        # 1. reuse any valid cached session
        for uid, entry in state.items():
            if _is_valid(entry):
                remain = entry.get("exp_ts", 0) - time.time()
                print("[pool] reuse jwt uid=%s account_id=%s exp_in=%s"
                      % (uid, entry.get("account_id"), _fmt_dur(remain)))
                return entry
            else:
                print("[pool] cached jwt for uid=%s expired/near-expiry" % uid)

        # 2. login pool accounts until one succeeds
        for acc in load_pool():
            uid = str(acc.get("uid", "")).strip()
            pwd = str(acc.get("password", "")).strip()
            if not uid or not pwd:
                continue
            entry = await _login(uid, pwd)
            if entry:
                state[uid] = entry
                _save_state(state)
                return entry

        print("[pool] no usable session")
        return None


def get_pool_status() -> Dict[str, Any]:
    """Debug helper — current session state."""
    state = _load_state(force_reload=True)
    now = time.time()
    out = []
    for uid, e in state.items():
        exp = int(e.get("exp_ts") or 0)
        out.append({
            "uid": uid,
            "account_id": e.get("account_id"),
            "region": e.get("region"),
            "platform": e.get("platform"),
            "valid": _is_valid(e),
            "exp_in_seconds": max(0, int(exp - now)) if exp else None,
            "cached_at": e.get("cached_at"),
        })
    return {
        "jwtgen_loaded": _JWTGEN_OK,
        "accounts": out,
        "now": now,
    }


# ==================== fields cache ====================
def get_cached_fields(uid: str) -> Optional[Dict[str, Any]]:
    if not FIELDS_FILE.exists():
        return None
    try:
        d = json.loads(FIELDS_FILE.read_text())
        return d.get(str(uid))
    except Exception:
        return None


def set_cached_fields(uid: str, fields: Dict[str, Any], meta: Optional[Dict[str, Any]] = None):
    d = {}
    if FIELDS_FILE.exists():
        try:
            d = json.loads(FIELDS_FILE.read_text())
        except Exception:
            d = {}
    entry = dict(fields or {})
    entry["_cached_at"] = time.time()
    if meta:
        entry["_source"] = meta
    d[str(uid)] = entry
    tmp = str(FIELDS_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FIELDS_FILE)


# ── alias for banner_renderer ──
get_pool_creds = get_pool_session
