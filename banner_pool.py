# language: Python, file: banner_pool.py
# Session pool for banner fetching — allows rendering banner for ANY uid,
# not just accounts already running in master.py.

import time
import asyncio
import json
import pathlib
from typing import Optional, Dict, Any

POOL_FILE = pathlib.Path(__file__).parent / "banner_pool.json"

DEFAULT_POOL = [
    {"uid": "8187459126", "password": "FFDEVILFIBOB2Z034ISX0P6NZ"},
    {"uid": "8173105159", "password": "B4C932E05745CDCF4632C4926FE921D26591E8E847E7BF7BAC8822231DA8BCB9"},
]

_sessions: Dict[str, Dict[str, Any]] = {}
_lock: Optional[asyncio.Lock] = None
CACHE_TTL = 45 * 60


def _pool() -> list:
    if POOL_FILE.exists():
        try:
            data = json.loads(POOL_FILE.read_text())
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return DEFAULT_POOL


async def _login_via_master(uid: str, password: str) -> Optional[Dict[str, Any]]:
    """Use master.py's own login primitives — no bot_state pollution."""
    from master import (
        get_access_token, version_config, build_majorlogin_payload,
        send_majorlogin, get_device_for_account,
    )

    vc = await version_config()
    if not vc:
        print("[banner_pool] version_config failed")
        return None
    release_version, client_version, server_url = vc

    tok = await get_access_token(uid, password)
    if not tok:
        print("[banner_pool] get_access_token failed for %s" % uid)
        return None
    open_id, access_token, platform = tok

    device = get_device_for_account(uid)
    payload = await build_majorlogin_payload(open_id, access_token, platform, client_version, device)
    if not payload:
        print("[banner_pool] payload build failed")
        return None

    res = await send_majorlogin(payload, release_version, server_url)
    if res is None:
        print("[banner_pool] MajorLogin rejected")
        return None

    return {
        "token": res.token,
        "server_url": res.url,
        "release_version": release_version,
        "client_version": client_version,
        "account_id": res.account_id,
        "cached_at": time.time(),
    }


async def get_pool_creds() -> Optional[Dict[str, Any]]:
    """Return a live pool session. Cached for CACHE_TTL seconds."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        now = time.time()
        for acc in _pool():
            uid = str(acc["uid"])
            e = _sessions.get(uid)
            if e and now - e.get("cached_at", 0) < CACHE_TTL:
                return e

        for acc in _pool():
            uid = str(acc["uid"])
            pwd = str(acc.get("password", ""))
            if not uid or not pwd:
                continue
            try:
                entry = await _login_via_master(uid, pwd)
                if entry:
                    _sessions[uid] = entry
                    print("[banner_pool] session ready: %s (region=%s)"
                          % (uid, entry.get("account_id")))
                    return entry
            except Exception as e:
                print("[banner_pool] login exception for %s: %s" % (uid, e))
                continue

    print("[banner_pool] no usable session")
    return None
