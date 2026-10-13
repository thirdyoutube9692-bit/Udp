# -*- coding: utf-8 -*-
"""
RAJA OFFICIAL - Dashboard Server (IND Region)
Master Stop / Per-Account Stop / Target Level Auto-Stop / accounts_db.json
"""

from banner_renderer import render_banner_for_uid

import asyncio
import json
import os
import time
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from aiohttp import web


# ==================== CONFIG ====================
ADMIN_KEY = "Samrin01"
USER_KEY = "masterji"
TELEGRAM_LINK = "https://t.me/MASTER_FF_01"
SESSION_COOKIE = "raja_session"
SESSION_TTL = 86400 * 7
BULK_STAGGER_DELAY = 1.5
AUTO_LW_LEVEL = 3

ADMIN_ONLY_PATHS = {
    "/api/account/add",
    "/api/account/bulk_add",
    "/api/account/delete",
    "/api/account/pause",
    "/api/account/pause_till_4am",
    "/api/account/resume_now",
    "/api/account/pause_all",
    "/api/account/mode",
    "/api/account/bulk_mode",
    "/api/account/refresh",
    "/api/logs/clear",
    "/api/master/stop",
    "/api/master/start",
    "/api/account/stop",
    "/api/account/unstop",
    "/api/accounts/remove_all",
}


# ==================== SESSION STORE ====================
_sessions: Dict[str, Dict[str, Any]] = {}


def _create_session(role: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"role": role, "expiry": time.time() + SESSION_TTL}
    return token


def _get_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    entry = _sessions.get(token)
    if not entry:
        return None
    if time.time() > entry.get("expiry", 0):
        _sessions.pop(token, None)
        return None
    return entry


def _validate_session(token: Optional[str]) -> bool:
    return _get_session(token) is not None


def _destroy_session(token: Optional[str]):
    if token:
        _sessions.pop(token, None)


def _next_4am_timestamp() -> float:
    now = datetime.now()
    target = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target.timestamp()


# ==================== BOT STATE ====================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.master_stop: bool = False

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {"time": time.strftime("%H:%M:%S"), "level": level, "message": message, "uid": uid}
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid, nickname, region, level, exp, likes=0, password=None, guest_uid=None):
        uid_str = str(uid)
        lvl_val = max(1, int(level or 1))
        now = time.time()
        if uid_str not in self.accounts:
            auto_mode = "LONE_WOLF" if lvl_val >= AUTO_LW_LEVEL else "BR"
            self.accounts[uid_str] = {
                "uid": uid_str,
                "guest_uid": str(guest_uid) if guest_uid else uid_str,
                "password": password or "",
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "IND",
                "level": lvl_val,
                "initial_exp": exp, "current_exp": exp, "gained_exp": 0,
                "likes": likes or 0,
                "mode": auto_mode, "mode_manual": False,
                "status": "ONLINE", "is_paused": False,
                "is_stopped": False,
                "target_level": 0,
                "matches_played": 0, "matches_completed": 0, "active_matches": 0,
                "uptime_seconds": 0, "login_start_time": now,
                "consecutive_failures": 0,
                "login_failures": 0,
                "pause_until": None, "pause_till_4am": False,
                "last_match_time": None, "last_match_failed": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "_last_lvl": lvl_val,
            }
        else:
            acc = self.accounts[uid_str]
            if guest_uid:
                acc["guest_uid"] = str(guest_uid)
            if password:
                acc["password"] = password
            if nickname: acc["nickname"] = nickname
            if region: acc["region"] = "IND"
            if level: acc["level"] = lvl_val
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            if not acc.get("mode_manual", False):
                old_lvl = int(acc.get("_last_lvl", 1) or 1)
                acc["mode"] = "LONE_WOLF" if lvl_val >= AUTO_LW_LEVEL else "BR"
                if old_lvl < AUTO_LW_LEVEL <= lvl_val:
                    self.log(
                        f"{acc.get('nickname','Unknown')} reached Lv{lvl_val} — auto switched to LONE WOLF",
                        "success", uid_str
                    )
                acc["_last_lvl"] = lvl_val
            if not acc.get("is_stopped", False):
                acc["status"] = "ONLINE"
                acc["login_start_time"] = now
            acc["consecutive_failures"] = 0
            acc["login_failures"] = 0
            acc["last_updated"] = time.strftime("%H:%M:%S")
        self.recalc_totals()

    def update_exp(self, uid, current_exp, level=None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                old_lvl = int(acc.get("level", 1) or 1)
                acc["level"] = int(level)

                target = int(acc.get("target_level", 0) or 0)
                if target > 0 and int(level) >= target and not acc.get("is_stopped", False):
                    acc["is_stopped"] = True
                    acc["status"] = "TARGET_REACHED"
                    worker = self.account_workers.get(uid_str)
                    if worker and not worker.done():
                        worker.cancel()
                        self.account_workers.pop(uid_str, None)
                    self.log(
                        f"🎯 {acc.get('nickname','Unknown')} ({uid_str}) REACHED TARGET LV.{target} — Auto-stopped!",
                        "success", uid_str
                    )

                if not acc.get("mode_manual", False):
                    new_lvl = int(level)
                    acc["mode"] = "LONE_WOLF" if new_lvl >= AUTO_LW_LEVEL else "BR"
                    if old_lvl < AUTO_LW_LEVEL <= new_lvl:
                        self.log(
                            f"{acc.get('nickname','Unknown')} reached Lv{new_lvl} — auto switched to LONE WOLF",
                            "success", uid_str
                        )
                    acc["_last_lvl"] = new_lvl
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            d = current_exp - old
            if d > 0:
                self.log(f"{acc['nickname']} ({uid_str}) +{d} EXP! Total: +{acc['gained_exp']}", "success", uid_str)
            self.recalc_totals()

    def get_account_level(self, uid):
        acc = self.accounts.get(str(uid))
        return int(acc.get("level", 1) or 1) if acc else 1

    def get_account_mode(self, uid):
        acc = self.accounts.get(str(uid))
        if not acc:
            return "BR"
        if acc.get("mode_manual", False):
            mode = str(acc.get("mode", "BR") or "BR").upper()
            return "LONE_WOLF" if mode == "LONE_WOLF" else "BR"
        level = int(acc.get("level", 1) or 1)
        return "LONE_WOLF" if level >= AUTO_LW_LEVEL else "BR"

    def set_account_mode(self, uid, mode):
        uid_str = str(uid)
        mode = str(mode or "").upper().replace("-", "_").replace(" ", "_")
        if mode in ("LW", "LONEWOLF", "LONE_WOLF"):
            mode = "LONE_WOLF"
        elif mode == "BR":
            mode = "BR"
        else:
            return False, "Invalid mode"
        acc = self.accounts.get(uid_str)
        if not acc:
            return False, "Account not found"
        level = int(acc.get("level", 1) or 1)
        if level < AUTO_LW_LEVEL and mode != "BR":
            return False, f"Lone Wolf is locked until Level {AUTO_LW_LEVEL}"
        acc["mode"] = mode
        acc["mode_manual"] = True
        acc["last_updated"] = time.strftime("%H:%M:%S")
        self.log(f"{acc.get('nickname','Unknown')} ({uid_str}) switched to {mode} (manual)", "success", uid_str)
        return True, mode

    def update_status(self, uid, status, active_matches=None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            if self.accounts[uid_str].get("is_stopped", False):
                self.accounts[uid_str]["status"] = "STOPPED"
                return
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def is_paused(self, uid):
        acc = self.accounts.get(str(uid))
        if not acc: return False
        return bool(acc.get("is_paused", False) or acc.get("status") == "PAUSED")

    def is_stopped(self, uid):
        acc = self.accounts.get(str(uid))
        if not acc: return False
        return bool(acc.get("is_stopped", False) or acc.get("status") == "STOPPED")

    def set_paused(self, uid, paused):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["is_paused"] = bool(paused)
            if paused:
                self.accounts[uid_str]["status"] = "PAUSED"
            elif not self.accounts[uid_str].get("is_stopped", False):
                self.accounts[uid_str]["status"] = "ONLINE"
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            return True
        return False

    def increment_match_started(self, uid):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] = self.accounts[uid_str].get("matches_played", 0) + 1
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_completed"] = self.accounts[uid_str].get("matches_completed", 0) + 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["consecutive_failures"] = 0

    def match_failed(self, uid):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["last_match_failed"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def mark_login_failure(self, uid):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["consecutive_failures"] = self.accounts[uid_str].get("consecutive_failures", 0) + 1
            fails = self.accounts[uid_str]["consecutive_failures"]
            if fails >= 10:
                self.accounts[uid_str]["status"] = "DISABLED"
                self.accounts[uid_str]["is_paused"] = True
                self.log(f"{uid_str} disabled after {fails} failures", "error", uid_str)

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())


bot_state = BotState()


# ==================== PATHS ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "index.html")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
ACCOUNTS_DB_FILE = os.path.join(BASE_DIR, "accounts_db.json")


# ==================== LOGIN PAGE ====================
LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#02060d">
<title>RAJA | Secure Access</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=JetBrains+Mono:wght@400;500;700&family=Cinzel:wght@600;700;900&family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css">
<style>
:root{--bg:#02060d;--neon:#00f0ff;--neon2:#ff00aa;--line:rgba(0,240,255,.16);--line2:rgba(0,240,255,.45);--text:#d8f4ff;--text2:#5c8ba8;--text3:#3a5d78;--err:#ff0044;--ok:#00ff41}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;font-family:'Rajdhani',sans-serif}
body{background:radial-gradient(ellipse at 50% 30%,#041024 0%,#02060d 70%);color:var(--text);display:grid;place-items:center;padding:20px;position:relative}
.bg-layer{position:fixed;inset:0;overflow:hidden;pointer-events:none;z-index:0}
.bg-layer::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 50% at 15% 0%,rgba(0,240,255,.28),transparent 60%),radial-gradient(ellipse 55% 50% at 85% 100%,rgba(255,0,170,.22),transparent 60%),radial-gradient(ellipse 50% 60% at 50% 50%,rgba(0,255,65,.08),transparent 65%);animation:bgBreath 8s ease-in-out infinite}
@keyframes bgBreath{0%,100%{opacity:.85}50%{opacity:1}}
.bg-grid{position:absolute;inset:0;background-image:linear-gradient(rgba(0,240,255,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(0,240,255,.045) 1px,transparent 1px);background-size:56px 56px;mask-image:radial-gradient(ellipse at 50% 45%,#000 25%,transparent 78%);-webkit-mask-image:radial-gradient(ellipse at 50% 45%,#000 25%,transparent 78%)}
.wrap{position:relative;z-index:2;width:100%;max-width:420px}
.card{position:relative;border-radius:26px;overflow:hidden;background:linear-gradient(180deg,rgba(8,18,32,.94),rgba(4,10,20,.97));border:1px solid var(--line2);box-shadow:0 40px 100px rgba(0,0,0,.75),0 0 80px rgba(0,240,255,.18);backdrop-filter:blur(26px);animation:cardIn .7s cubic-bezier(.16,1,.3,1)}
@keyframes cardIn{from{opacity:0;transform:translateY(30px) scale(.97)}}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,#00f0ff 25%,#ff00aa 50%,#00f0ff 75%,transparent);background-size:300% 100%;animation:textFlow 5s linear infinite}
@keyframes textFlow{to{background-position:300% 0}}
.card-inner{padding:38px 30px 26px;position:relative}
.hero{text-align:center;margin-bottom:24px}
.seal{width:90px;height:90px;margin:0 auto 20px;position:relative;display:flex;align-items:center;justify-content:center;border-radius:50%;background:radial-gradient(circle at 30% 30%,rgba(255,255,255,.1),transparent 60%),linear-gradient(135deg,#0b0f1e,#050810);box-shadow:inset 0 0 0 1.5px rgba(0,240,255,.5),inset 0 0 24px rgba(0,240,255,.2),0 0 40px rgba(0,240,255,.45)}
.seal::before{content:'';position:absolute;inset:-5px;border-radius:50%;padding:2px;background:conic-gradient(from -45deg,transparent 0deg,#00f0ff 60deg,#ff00aa 140deg,transparent 200deg,transparent 360deg);-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude;animation:ringSpin 6s linear infinite}
@keyframes ringSpin{to{transform:rotate(360deg)}}
.seal::after{content:'R';font-family:'Cinzel',serif;font-weight:900;font-size:46px;line-height:1;background:linear-gradient(180deg,#ffffff 0%,#00f0ff 40%,#0088ff 70%,#0044cc 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;filter:drop-shadow(0 0 14px rgba(0,240,255,.7));z-index:2;position:relative}
.brand-title{font-family:'Cinzel',serif;font-weight:900;font-size:26px;letter-spacing:8px;line-height:1.1;background:linear-gradient(180deg,#ffffff 0%,#c8f0ff 55%,#7ec8e8 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;filter:drop-shadow(0 2px 14px rgba(0,240,255,.4));margin-bottom:6px}
.brand-sub{font-family:'Orbitron',sans-serif;font-size:9px;font-weight:700;letter-spacing:5px;color:var(--neon);text-transform:uppercase;margin-bottom:14px}
.status-badge{display:inline-flex;align-items:center;gap:7px;padding:5px 13px;border-radius:99px;background:rgba(0,255,65,.1);border:1px solid rgba(0,255,65,.35);font-family:'Orbitron',sans-serif;font-size:8px;font-weight:800;letter-spacing:2.5px;color:#6ee7b7;text-transform:uppercase}
.status-badge .dot{width:5px;height:5px;border-radius:50%;background:#00ff41;box-shadow:0 0 8px #00ff41;animation:pulseDot 2s infinite}
@keyframes pulseDot{50%{opacity:.4;transform:scale(1.4)}}
.greeting{font-size:10px;font-weight:700;color:var(--text3);letter-spacing:2.5px;text-transform:uppercase;text-align:center;margin-bottom:6px;margin-top:20px}
.form-heading{font-size:22px;font-weight:800;letter-spacing:-.3px;text-align:center;margin-bottom:6px;color:#fff}
.form-desc{text-align:center;font-size:12px;color:var(--text2);margin-bottom:22px}
.field{position:relative;margin-bottom:14px}
.field input{width:100%;padding:16px 48px 16px 46px;border:1px solid var(--line);background:rgba(0,0,0,.5);color:var(--text);font-size:14px;font-family:'JetBrains Mono',monospace;letter-spacing:3px;outline:none}
.field input:focus{border-color:var(--line2);box-shadow:0 0 20px rgba(0,240,255,.25)}
.field .ico-l{position:absolute;left:16px;top:50%;transform:translateY(-50%);color:var(--text2);font-size:14px;pointer-events:none}
.field.focused .ico-l{color:var(--neon)}
.field .ico-r{position:absolute;right:16px;top:50%;transform:translateY(-50%);color:var(--text2);font-size:14px;cursor:pointer;user-select:none}
.field .ico-r:hover{color:var(--neon)}
.btn{width:100%;padding:16px;border-radius:14px;border:0;cursor:pointer;font-family:'Orbitron',sans-serif;font-weight:800;font-size:12.5px;letter-spacing:2px;color:#000;text-transform:uppercase;background:linear-gradient(180deg,#00f0ff,#0088ff);box-shadow:0 12px 34px rgba(0,240,255,.4);display:flex;align-items:center;justify-content:center;gap:9px;margin-top:6px}
.btn:hover{transform:translateY(-2px);box-shadow:0 18px 42px rgba(0,240,255,.6)}
.btn:active{transform:translateY(0) scale(.99)}
.btn:disabled{opacity:.6;cursor:not-allowed}
.btn-tg{width:100%;padding:13px;border-radius:14px;margin-top:10px;border:1px solid var(--line);background:rgba(10,16,32,.6);color:var(--text);cursor:pointer;text-decoration:none;font-family:'Orbitron',sans-serif;font-weight:700;font-size:11px;letter-spacing:1.5px;display:flex;align-items:center;justify-content:center;gap:9px;text-transform:uppercase}
.btn-tg:hover{border-color:var(--line2);background:rgba(0,240,255,.08)}
.btn-tg i{font-size:15px;color:#229ED9}
.msg{margin-top:14px;padding:11px 14px;border-radius:11px;font-size:12px;font-weight:600;display:none;align-items:center;gap:9px}
.msg.err{display:flex;background:rgba(255,0,68,.12);border:1px solid rgba(255,0,68,.4);color:#ff8899}
.msg.ok{display:flex;background:rgba(0,255,65,.12);border:1px solid rgba(0,255,65,.4);color:#88ff99}
.shake{animation:shake .5s}
@keyframes shake{10%,90%{transform:translateX(-2px)}20%,80%{transform:translateX(4px)}30%,50%,70%{transform:translateX(-6px)}40%,60%{transform:translateX(6px)}}
.divider{height:1px;margin:22px 0 16px;background:linear-gradient(90deg,transparent,var(--line),transparent)}
.foot{text-align:center;font-size:9.5px;font-weight:600;color:var(--text3);letter-spacing:1.5px;text-transform:uppercase;line-height:1.9}
.foot .hl{color:var(--neon);font-weight:800}
.foot .tg{color:#5b9eff}
</style>
</head>
<body>
<div class="bg-layer"><div class="bg-grid"></div></div>
<div class="wrap">
  <div class="card" id="mainCard">
    <div class="card-inner">
      <div class="hero">
        <div class="seal"></div>
        <div class="brand-title">RAJA</div>
        <div class="brand-sub">Command Center</div>
        <div class="status-badge"><span class="dot"></span>Secure Access</div>
      </div>
      <div class="greeting" id="greeting">Welcome</div>
      <div class="form-heading">Access Command Center</div>
      <div class="form-desc">Enter your access key to continue</div>
      <form id="loginForm" autocomplete="off">
        <div class="field" id="keyField">
          <input type="password" id="keyInput" placeholder="Enter access key" autocomplete="new-password" autofocus>
          <i class="fa-solid fa-key ico-l"></i>
          <i class="fa-solid fa-eye ico-r" id="eyeToggle"></i>
        </div>
        <button type="submit" class="btn" id="loginBtn"><i class="fa-solid fa-shield-halved"></i><span>Unlock RAJA System</span></button>
        <a class="btn-tg" href="{{TG_LINK}}" target="_blank" rel="noopener"><i class="fa-brands fa-telegram"></i><span>Contact Owner</span></a>
        <div class="msg" id="msg"></div>
      </form>
      <div class="divider"></div>
      <div class="foot">
        <div>Protected Access <span style="opacity:.5">●</span> <span class="hl">RAJA PREMIUM</span></div>
        <div class="tg"><i class="fa-brands fa-telegram"></i> @MASTER_FF_01</div>
      </div>
    </div>
  </div>
</div>
<script>
var input=document.getElementById('keyInput');var eye=document.getElementById('eyeToggle');var form=document.getElementById('loginForm');var msg=document.getElementById('msg');var btn=document.getElementById('loginBtn');var card=document.getElementById('mainCard');var keyField=document.getElementById('keyField');
(function(){var h=new Date().getHours();var g='Welcome';if(h<12)g='Good Morning';else if(h<18)g='Good Afternoon';else g='Good Evening';document.getElementById('greeting').textContent=g})();
input.addEventListener('focus',function(){keyField.classList.add('focused')});
input.addEventListener('blur',function(){keyField.classList.remove('focused')});
eye.addEventListener('click',function(){if(input.type==='password'){input.type='text';eye.className='fa-solid fa-eye-slash ico-r'}else{input.type='password';eye.className='fa-solid fa-eye ico-r'}});
form.addEventListener('submit',async function(e){e.preventDefault();var key=input.value.trim();if(!key){show('Please enter access key','err');shake();return}btn.disabled=true;btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i><span>Verifying...</span>';try{var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:key})});var d=await r.json();if(d.status==='ok'){show('Access granted as '+(d.role||'user'),'ok');btn.innerHTML='<i class="fa-solid fa-check"></i><span>Success</span>';setTimeout(function(){window.location.href='/'},700)}else{show(d.error||'Invalid access key','err');input.value='';input.focus();btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-shield-halved"></i><span>Unlock RAJA System</span>';shake()}}catch(err){show('Connection failed. Try again.','err');btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-shield-halved"></i><span>Unlock RAJA System</span>';shake()}});
function show(text,type){msg.className='msg '+type;msg.innerHTML='<i class="fa-solid '+(type==='err'?'fa-circle-xmark':'fa-circle-check')+'"></i><span>'+text+'</span>'}
function shake(){card.classList.remove('shake');void card.offsetWidth;card.classList.add('shake')}
</script>
</body>
</html>""".replace("{{TG_LINK}}", TELEGRAM_LINK)


# ==================== AUTH MIDDLEWARE ====================
@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    if path in ("/login", "/api/login"):
        return await handler(request)
    token = request.cookies.get(SESSION_COOKIE)
    session = _get_session(token)
    if not session:
        if path.startswith("/api/"):
            return web.json_response({"status": "error", "error": "Unauthorized"}, status=401)
        return web.HTTPFound("/login")
    if path in ADMIN_ONLY_PATHS and session.get("role") != "admin":
        return web.json_response({"status": "error", "error": "Admin access required"}, status=403)
    return await handler(request)


# ==================== LOGIN HANDLERS ====================
async def handle_login_page(request):
    token = request.cookies.get(SESSION_COOKIE)
    if _validate_session(token):
        return web.HTTPFound("/")
    return web.Response(text=LOGIN_PAGE_HTML, content_type="text/html", charset="utf-8")


async def handle_login(request):
    try:
        data = await request.json()
        key = str(data.get("key", "")).strip()
        if not key:
            return web.json_response({"status": "error", "error": "Key required"}, status=400)
        role = None
        if key == ADMIN_KEY:
            role = "admin"
        elif key == USER_KEY:
            role = "user"
        else:
            bot_state.log(f"Failed login attempt from {request.remote}", "warning")
            return web.json_response({"status": "error", "error": "Invalid key"}, status=401)
        token = _create_session(role)
        resp = web.json_response({"status": "ok", "role": role})
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Lax")
        bot_state.log(f"Login successful as {role.upper()}", "success")
        return resp
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_logout(request):
    token = request.cookies.get(SESSION_COOKIE)
    _destroy_session(token)
    resp = web.json_response({"status": "ok"})
    resp.del_cookie(SESSION_COOKIE)
    bot_state.log("User logged out", "info")
    return resp


async def handle_session_info(request):
    token = request.cookies.get(SESSION_COOKIE)
    session = _get_session(token)
    if not session:
        return web.json_response({"status": "error", "error": "Unauthorized"}, status=401)
    return web.json_response({
        "status": "ok",
        "role": session.get("role", "user"),
        "is_admin": session.get("role") == "admin",
        "auto_lw_level": AUTO_LW_LEVEL,
    })


# ==================== MAIN HANDLERS ====================
async def handle_index(request):
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = f"<h1>File not found at: {TEMPLATE_PATH}</h1>"
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_get_stats(request):
    accounts_data = list(bot_state.accounts.values())
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    now = time.time()
    for acc in accounts_data:
        start = acc.get("login_start_time", 0)
        acc["uptime_seconds"] = int(now - start) if start else 0
    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "total_active_matches": sum(int(a.get("active_matches", 0) or 0) for a in accounts_data),
        "accounts": accounts_data,
        "logs": bot_state.logs[-60:],
        "uptime": int(time.time() - bot_state.start_time),
        "auto_lw_level": AUTO_LW_LEVEL,
        "master_stop": bot_state.master_stop,
    })


async def handle_add_account(request):
    try:
        data = await request.json()
        existing = _load_accounts_file()
        uid = ""
        pwd = ""
        target_level = int(data.get("target_level", 0) or 0)

        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID and Password required"})
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            existing.append({"uid": uid, "password": pwd, "target_level": target_level})
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token required"})
            existing = [acc for acc in existing if acc.get("token") != token]
            existing.append({"token": token})
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})

        _save_accounts_file(existing)

        # NOTE: No placeholder is created. Real account appears after successful login
        # with correct in-game ID, nickname, level, and stats.

        bot_state.log(f"New credentials saved: {data.get('uid') or 'Token'} (Target: {target_level if target_level > 0 else 'None'})", "success")
        if "on_account_added" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](data))
        _save_accounts_db()
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_bulk_add(request):
    try:
        data = await request.json()
        new_accounts = data.get("accounts", [])
        if not isinstance(new_accounts, list):
            return web.json_response({"status": "error", "error": "Invalid accounts list"})
        existing = _load_accounts_file()
        existing_keys = set()
        for acc in existing:
            if "uid" in acc and acc.get("uid"): existing_keys.add(f"uid:{acc['uid']}")
            elif "token" in acc and acc.get("token"): existing_keys.add(f"tok:{str(acc['token'])[:20]}")
        added = 0; skipped = 0; invalid = 0; added_entries = []
        for acc in new_accounts:
            if not isinstance(acc, dict): invalid += 1; continue
            if "uid" in acc and "password" in acc:
                uid = str(acc["uid"]).strip()
                pwd = str(acc["password"]).strip()
                if not uid or not pwd: invalid += 1; continue
                key = f"uid:{uid}"
                if key in existing_keys: skipped += 1; continue
                existing_keys.add(key)
                entry = {"uid": uid, "password": pwd, "target_level": int(acc.get("target_level", 0) or 0)}
                existing.append(entry); added_entries.append(entry); added += 1
            elif "token" in acc:
                token = str(acc["token"]).strip()
                if not token: invalid += 1; continue
                key = f"tok:{token[:20]}"
                if key in existing_keys: skipped += 1; continue
                existing_keys.add(key)
                entry = {"token": token}
                existing.append(entry); added_entries.append(entry); added += 1
            else: invalid += 1
        _save_accounts_file(existing)
        if added > 0:
            bot_state.log(f"Bulk add: +{added} accounts, {skipped} skipped, {invalid} invalid", "success")
            if "on_account_added" in bot_state.refresh_callbacks:
                asyncio.create_task(_staggered_worker_start(added_entries))
        _save_accounts_db()
        return web.json_response({"status": "ok", "added": added, "skipped": skipped, "invalid": invalid})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def _staggered_worker_start(entries):
    callback = bot_state.refresh_callbacks.get("on_account_added")
    if not callback: return
    for i, entry in enumerate(entries):
        try:
            if i > 0: await asyncio.sleep(BULK_STAGGER_DELAY)
            await callback(entry)
        except Exception as e:
            bot_state.log(f"Worker start failed: {e}", "error")


async def handle_delete_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        token = str(data.get("token", "")).strip()
        existing = _load_accounts_file()
        if token: existing = [acc for acc in existing if str(acc.get("token", "")).strip() != token]
        elif uid: existing = [acc for acc in existing if str(acc.get("uid", "")).strip() != uid]
        _save_accounts_file(existing)
        worker_keys = []
        if uid: worker_keys.append(uid)
        if token: worker_keys.append(token[:10])
        for key in worker_keys:
            worker = bot_state.account_workers.get(key)
            if worker:
                if not worker.done(): worker.cancel()
                bot_state.account_workers.pop(key, None)
        if uid and uid in bot_state.accounts: del bot_state.accounts[uid]
        bot_state.log(f"Account {uid or token[:10] or 'unknown'} removed", "warning", uid or None)
        _save_accounts_db()
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_refresh_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if "on_refresh_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_refresh_account"](uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_mode_switch(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        mode = str(data.get("mode", "")).strip()
        ok, result = bot_state.set_account_mode(uid, mode)
        if not ok:
            return web.json_response({"status": "error", "error": result}, status=400)
        _save_accounts_db()
        return web.json_response({"status": "ok", "uid": uid, "mode": result, "level": bot_state.get_account_level(uid)})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_bulk_mode_switch(request):
    try:
        data = await request.json()
        mode = str(data.get("mode", "BR")).strip()
        changed = 0; skipped = 0
        for uid in list(bot_state.accounts.keys()):
            ok, _ = bot_state.set_account_mode(uid, mode)
            if ok: changed += 1
            else: skipped += 1
        bot_state.log(f"Bulk mode switch to {mode}: {changed} changed, {skipped} skipped", "success")
        _save_accounts_db()
        return web.json_response({"status": "ok", "changed": changed, "skipped": skipped, "mode": mode})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_pause_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"}, status=400)
        acc = bot_state.accounts.get(uid)
        if not acc:
            return web.json_response({"status": "error", "error": "Account not found"}, status=404)
        if acc.get("is_stopped", False):
            return web.json_response({"status": "error", "error": "Account is stopped"}, status=400)
        currently_paused = bot_state.is_paused(uid)
        new_state = not currently_paused
        bot_state.set_paused(uid, new_state)
        if not new_state:
            acc["pause_until"] = None
            acc["pause_till_4am"] = False
        bot_state.log(f"{acc.get('nickname','Unknown')} ({uid}) {'paused' if new_state else 'resumed'}",
                      "warning" if new_state else "success", uid)
        _save_accounts_db()
        return web.json_response({"status": "ok", "uid": uid, "is_paused": new_state})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_pause_till_4am(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"}, status=400)
        acc = bot_state.accounts.get(uid)
        if not acc:
            return web.json_response({"status": "error", "error": "Account not found"}, status=404)
        until_ts = _next_4am_timestamp()
        acc["pause_until"] = until_ts
        acc["pause_till_4am"] = True
        bot_state.set_paused(uid, True)
        until_str = datetime.fromtimestamp(until_ts).strftime("%d %b %I:%M %p")
        bot_state.log(f"{acc.get('nickname','Unknown')} ({uid}) paused till {until_str}", "warning", uid)
        _save_accounts_db()
        return web.json_response({"status": "ok", "uid": uid, "pause_until": until_ts})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_resume_now(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"}, status=400)
        acc = bot_state.accounts.get(uid)
        if not acc:
            return web.json_response({"status": "error", "error": "Account not found"}, status=404)
        acc["pause_until"] = None
        acc["pause_till_4am"] = False
        bot_state.set_paused(uid, False)
        bot_state.log(f"{acc.get('nickname','Unknown')} ({uid}) manually resumed", "success", uid)
        _save_accounts_db()
        return web.json_response({"status": "ok", "uid": uid})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_pause_all(request):
    try:
        if not bot_state.accounts:
            return web.json_response({"status": "error", "error": "No accounts"}, status=400)
        any_running = any(not bot_state.is_paused(uid) and not bot_state.is_stopped(uid) for uid in bot_state.accounts.keys())
        new_state = any_running
        for uid in bot_state.accounts.keys():
            if bot_state.is_stopped(uid): continue
            bot_state.set_paused(uid, new_state)
            if not new_state:
                bot_state.accounts[uid]["pause_until"] = None
                bot_state.accounts[uid]["pause_till_4am"] = False
        bot_state.log(f"All accounts {'paused' if new_state else 'resumed'}",
                      "warning" if new_state else "success")
        _save_accounts_db()
        return web.json_response({"status": "ok", "all_paused": new_state})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_clear_logs(request):
    bot_state.logs.clear()
    return web.json_response({"status": "ok"})


# ==================== MASTER STOP / STOP ACCOUNT ====================
async def handle_master_stop(request):
    bot_state.master_stop = True
    bot_state.log("⛔ MASTER STOP - All processes halted", "warning")
    _save_accounts_db()
    return web.json_response({"status": "ok", "master_stop": True})


async def handle_master_start(request):
    bot_state.master_stop = False
    bot_state.log("✅ MASTER START - All processes resumed", "success")
    _save_accounts_db()
    return web.json_response({"status": "ok", "master_stop": False})


async def handle_master_status(request):
    return web.json_response({"status": "ok", "master_stop": bot_state.master_stop})


async def handle_accounts_db(request):
    _save_accounts_db()
    if os.path.exists(ACCOUNTS_DB_FILE):
        with open(ACCOUNTS_DB_FILE, "r", encoding="utf-8") as f:
            return web.json_response(json.load(f))
    return web.json_response({"updated_at": "", "master_stop": False, "total_accounts": 0, "accounts": []})


async def handle_stop_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"}, status=400)
        if uid in bot_state.accounts:
            bot_state.accounts[uid]["is_stopped"] = True
            bot_state.accounts[uid]["status"] = "STOPPED"
            worker = bot_state.account_workers.get(uid)
            if worker and not worker.done():
                worker.cancel()
                bot_state.account_workers.pop(uid, None)
            bot_state.log(f"⏹ Account {uid} STOPPED (kept in DB)", "warning", uid)
            _save_accounts_db()
            return web.json_response({"status": "ok", "is_stopped": True})
        return web.json_response({"status": "error", "error": "Not found"}, status=404)
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_unstop_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"}, status=400)
        if uid in bot_state.accounts:
            bot_state.accounts[uid]["is_stopped"] = False
            bot_state.accounts[uid]["is_paused"] = False
            bot_state.accounts[uid]["status"] = "ONLINE"
            if "on_account_added" in bot_state.refresh_callbacks:
                creds = _load_accounts_file()
                for c in creds:
                    if str(c.get("uid", "")) == uid:
                        asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](c))
                        break
            bot_state.log(f"▶ Account {uid} RESUMED", "success", uid)
            _save_accounts_db()
            return web.json_response({"status": "ok", "is_stopped": False})
        return web.json_response({"status": "error", "error": "Not found"}, status=404)
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_remove_all(request):
    try:
        for uid, worker in list(bot_state.account_workers.items()):
            try:
                if worker and not worker.done():
                    worker.cancel()
            except Exception:
                pass
        bot_state.account_workers.clear()
        bot_state.accounts.clear()
        bot_state.account_credentials.clear()
        _save_accounts_file([])
        bot_state.logs.clear()
        bot_state.log("🗑 ALL ACCOUNTS REMOVED - Terminal cleared", "warning")
        _save_accounts_db()
        return web.json_response({"status": "ok", "removed": True})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)



# ==================== BANNER ====================
async def handle_banner(request):
    uid = request.match_info.get("uid", "").strip()
    if not uid:
        return web.json_response({"status": "error", "error": "uid required"}, status=400)
    try:
        png = await render_banner_for_uid(uid, bot_state)
    except LookupError as e:
        return web.json_response({"status": "error", "error": str(e)}, status=404)
    except Exception as e:
        bot_state.log(f"Banner render failed for {uid}: {e}", "error", uid)
        return web.json_response({"status": "error", "error": str(e)}, status=500)
    return web.Response(body=png, content_type="image/png",
                        headers={"Cache-Control": "no-store"})


async def handle_banner_info(request):
    uid = request.match_info.get("uid", "").strip()
    creds = bot_state.account_credentials.get(uid)
    if not creds:
        for v in bot_state.account_credentials.values():
            if str(v.get("account_id", "")) == uid:
                creds = v
                break
    if not creds:
        return web.json_response({"status": "error", "error": "not in sessions"}, status=404)
    from banner_renderer import fetch_profile, extract_fields
    import asyncio as _asyncio
    loop = _asyncio.get_running_loop()
    info = await loop.run_in_executor(
        None,
        lambda: fetch_profile(
            creds.get("token"),
            creds.get("server_url") or creds.get("url"),
            int(creds.get("account_id")),
        ),
    )
    if info is None:
        return web.json_response({"status": "error", "error": "no profile"}, status=502)
    return web.json_response({"status": "ok", "uid": uid, "fields": extract_fields(info)})


# ==================== HELPERS ====================
def _load_accounts_file():
    if not os.path.exists(ACCOUNTS_FILE): return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_accounts_file(accounts):
    try:
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        os.replace(tmp, ACCOUNTS_FILE)
    except Exception as e:
        print(f"Save accounts failed: {e}")


def _save_accounts_db():
    try:
        # Load credentials to lookup passwords
        creds = _load_accounts_file()
        cred_map = {}
        for c in creds:
            if c.get("uid"):
                cred_map[str(c["uid"])] = c.get("password", "")

        accounts_list = []
        for uid, acc in bot_state.accounts.items():
            stored_password = acc.get("password", "") or cred_map.get(str(acc.get("guest_uid", uid)), "")
            accounts_list.append({
                "uid": str(acc.get("guest_uid", acc.get("uid", ""))),
                "password": stored_password,
                "name": acc.get("nickname", ""),
                "account_id": str(acc.get("uid", "")),
                "region": acc.get("region", "IND"),
                "level": int(acc.get("level", 1) or 1),
                "current_exp": int(acc.get("current_exp", 0) or 0),
                "base_exp": int(acc.get("initial_exp", 0) or 0),
                "gained_exp": int(acc.get("gained_exp", 0) or 0),
                "status": acc.get("status", "ONLINE"),
                "is_paused": bool(acc.get("is_paused", False)),
                "is_stopped": bool(acc.get("is_stopped", False)),
                "mode": acc.get("mode", "BR"),
                "target_level": int(acc.get("target_level", 0) or 0),
                "login_failures": int(acc.get("login_failures", 0) or 0),
                "matches_played": int(acc.get("matches_played", 0) or 0),
                "matches_completed": int(acc.get("matches_completed", 0) or 0),
                "last_updated": acc.get("last_updated", "")
            })
        data = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "master_stop": bot_state.master_stop,
            "total_accounts": len(accounts_list),
            "accounts": accounts_list
        }
        tmp = ACCOUNTS_DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, ACCOUNTS_DB_FILE)
    except Exception as e:
        print(f"Save DB failed: {e}")


async def _db_persist_task():
    while True:
        try:
            _save_accounts_db()
        except Exception:
            pass
        await asyncio.sleep(15)


# ==================== AUTO RESUME TASK ====================
async def auto_resume_task():
    while True:
        try:
            now = time.time()
            for uid, acc in list(bot_state.accounts.items()):
                if acc.get("is_stopped", False): continue
                until = acc.get("pause_until")
                if until and now >= until:
                    acc["pause_until"] = None
                    acc["pause_till_4am"] = False
                    bot_state.set_paused(uid, False)
                    bot_state.log(
                        f"{acc.get('nickname','Unknown')} ({uid}) auto-resumed (4 AM)",
                        "success", uid
                    )
        except Exception as e:
            print(f"Auto-resume error: {e}")
        await asyncio.sleep(30)


# ==================== START SERVER ====================
async def start_web_dashboard(host="0.0.0.0", port=5000):
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/session", handle_session_info)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_get("/api/banner/{uid}.png", handle_banner)
    app.router.add_get("/api/banner_info/{uid}", handle_banner_info)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/bulk_add", handle_bulk_add)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/mode", handle_mode_switch)
    app.router.add_post("/api/account/bulk_mode", handle_bulk_mode_switch)
    app.router.add_post("/api/account/pause", handle_pause_account)
    app.router.add_post("/api/account/pause_till_4am", handle_pause_till_4am)
    app.router.add_post("/api/account/resume_now", handle_resume_now)
    app.router.add_post("/api/account/pause_all", handle_pause_all)
    app.router.add_post("/api/logs/clear", handle_clear_logs)
    app.router.add_post("/api/master/stop", handle_master_stop)
    app.router.add_post("/api/master/start", handle_master_start)
    app.router.add_get("/api/master/status", handle_master_status)
    app.router.add_get("/api/accounts_db", handle_accounts_db)
    app.router.add_post("/api/account/stop", handle_stop_account)
    app.router.add_post("/api/account/unstop", handle_unstop_account)
    app.router.add_post("/api/accounts/remove_all", handle_remove_all)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    asyncio.create_task(auto_resume_task())
    asyncio.create_task(_db_persist_task())

    print(f"\033[92m[+] RAJA Dashboard: http://localhost:{port}\033[0m")
    print(f"\033[93m[+] Admin key: {ADMIN_KEY}\033[0m")
    print(f"\033[93m[+] User key : {USER_KEY}\033[0m")
    print(f"\033[93m[+] Telegram : {TELEGRAM_LINK}\033[0m")
    print(f"\033[93m[+] Auto LW  : Lv{AUTO_LW_LEVEL}+\033[0m")
