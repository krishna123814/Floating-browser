import json
import os
import re
import shutil
import time
import math
import threading
import hashlib
import hmac
import requests
import streamlit as st
import streamlit.components.v1 as components
import datetime
from urllib.parse import urlencode, quote
from concurrent.futures import ThreadPoolExecutor
import td_symbols as _td
import base64
import io
import zipfile
import replay_symbols as _replay

# ─── Fast2SMS API key ──────────────────────────────────────────────────────
# HF Spaces: Settings → Variables and secrets (env vars).
# Streamlit Cloud: Settings → Secrets (st.secrets) — kept as fallback.
def _get_secret(name: str, default: str = "") -> str:
    val = os.environ.get(name, "")
    if val:
        return val
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

FAST2SMS_KEY = _get_secret("FAST2SMS_KEY")

# ─── Alert channels (Brevo email / Telegram / Fast2SMS) ─────────────────────
# HF Space → Settings → Variables and secrets mein ye add karo:
#   BREVO_API_KEY       = Brevo ki API key (xkeysib-...)
#   ALERT_EMAIL_TO      = jis email par alert chahiye
#   BREVO_SENDER_EMAIL  = Brevo mein VERIFIED sender email (optional — na ho to
#                         ALERT_EMAIL_TO hi sender ki tarah use hoga)
#   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID = (optional, baad mein Telegram jodne
#                         ke liye — abhi na ho to skip ho jaata hai)
BREVO_API_KEY      = _get_secret("BREVO_API_KEY").strip()
ALERT_EMAIL_TO     = _get_secret("ALERT_EMAIL_TO").strip()
BREVO_SENDER_EMAIL = (_get_secret("BREVO_SENDER_EMAIL").strip()
                      or ALERT_EMAIL_TO)
TELEGRAM_BOT_TOKEN = _get_secret("TELEGRAM_BOT_TOKEN").strip()
TELEGRAM_CHAT_ID   = _get_secret("TELEGRAM_CHAT_ID").strip()
#   TELEGRAM_RELAY_URL / RELAY_SECRET = (optional) Render par chalne wala telegram-relay ka URL
#                         (https://<service>.onrender.com) aur uska secret. Set ho to Telegram message
#                         seedha api.telegram.org ki jagah is relay se jaata hai (HF se Telegram timeout ho to).
TELEGRAM_RELAY_URL = _get_secret("TELEGRAM_RELAY_URL").strip().rstrip("/")
RELAY_SECRET       = _get_secret("RELAY_SECRET").strip()

st.set_page_config(
    page_title="BankNifty Live Chart",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown("""<style>
/* ── Streamlit ke saare ads/badges/watermarks permanently hide ── */
#MainMenu                        {display:none!important}
footer                           {display:none!important}
header                           {display:none!important}
[data-testid="stToolbar"]        {display:none!important}
[data-testid="stDecoration"]     {display:none!important}
[data-testid="stStatusWidget"]   {display:none!important}
[data-testid="manage-app-button"]{display:none!important}
.reportview-container .main footer{display:none!important}
.viewerBadge_container__1QSob   {display:none!important}
.styles_viewerBadge__1yB5_      {display:none!important}
#stDecoration                    {display:none!important}
/* ── Layout ── */
.main .block-container{padding:0!important;max-width:100%!important;margin:0!important}
.stApp{background:#131722;overflow:hidden}
iframe{border:none!important}
</style>""", unsafe_allow_html=True)

# ─── Constants ────────────────────────────────────────────────────────────────
CREDS_FILE        = ".fyers_creds.json"
DAILY_CACHE_FILE  = "btc_daily_cache.json"
BN_DAILY_CACHE    = "bn_daily_cache.json"
DAILY_CACHE_TTL   = 300        # 5 min — aaj ki candle bhi update rahe
HIST_CACHE_TTL    = 300       # seconds for intraday cache (5 min — reduces API load)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Default credentials (user can override in sidebar).
# HF Spaces env vars / Streamlit Cloud secrets se aate hain (see _get_secret
# above); agar kahin set nahi hain (jaise local dev mein), khaali string
# fallback hoti hai — app tab manual-login form dikha dega.
DEFAULT_APP_ID    = _get_secret("FYERS_APP_ID")
DEFAULT_SECRET    = _get_secret("FYERS_SECRET")
DEFAULT_CLIENT_ID = _get_secret("FYERS_CLIENT_ID")
DEFAULT_PASSWORD  = _get_secret("FYERS_PASSWORD")

# ─── App-startup / login debug log ─────────────────────────────────────────
# Login ke turant baad screen chart par redirect ho jaati hai, isliye startup
# ke exact steps (creds load, session check, thread launch, koi bhi exception)
# yahan capture karte hain — taaki header ke chhote debug icon se poora
# startup trace copy karke dekha ja sake, chahe crash/redirect kitna bhi
# jaldi ho jaaye.
#
# NOTE (important bug fix): Streamlit HAR script-rerun (button click sahit)
# par poori .py file top-se-bottom dobara EXECUTE karta hai — isi process
# ke andar, but top-level statements jaise `_STARTUP_LOG = []` HAR rerun par
# phir se chalte hain. Matlab RAM-only list sirf ek single rerun ke andar
# hi zinda rehti thi — agla rerun (jaise BankNifty Update button ka apna
# hi rerun) aate hi khaali ho jaati thi. Isi wajah se purana "fresh boot
# detection" (list khaali → fresh boot) HAR baar True aata tha, chahe
# process bilkul restart na hua ho — jo ki galat tha.
# FIX: ab log disk par ek chhoti JSON file (_STARTUP_LOG_FILE) mein bhi
# turant likha jaata hai, aur module load hote hi (yaani har rerun ke
# start mein bhi) usi file se wapas load kar liya jaata hai — isliye ab
# log sach me kabhi khaali nahi hota (process restart ke baad bhi nahi),
# jab tak file delete na ho. Fresh-boot ab OS process-id (`os.getpid()`)
# ko file mein save kiye gaye pichhle PID se compare karke detect hota
# hai — PID sirf real naye process par badalta hai, Streamlit rerun par
# nahi, isliye ye ab sahi tarah "restart hua ya sirf rerun hua" batata hai.
_STARTUP_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "startup_debug_log.json")
_STARTUP_LOG_LOCK = threading.Lock()
_STARTUP_LOG_MAX  = 1500

# Disk par HAR _slog() call par pura 1500-line file rewrite karna heavy
# I/O hoga (proxy/market-depth jaise background loops bhi _slog use karte
# hain, jo har 1-2s chalte hain). Isliye disk-persist sirf un lines ke liye
# jo permanent debug block me actually dikhti hain (login + BN update +
# errors) — baaki sab RAM me hi rehti hain (is rerun ke liye kaafi hai).
_PERSIST_TAGS = ("SESSDBG", "BN_BTN_CLICK", "sess_active", "LOGIN_MANUAL",
                  "LOGIN_URL_SECRET",
                  "Script run start", "EXCEPTION")
_STARTUP_LOG_DIRTY_COUNT = 0
_STARTUP_LOG_FLUSH_EVERY = 3

_STARTUP_LOG_LAST_DISK_ERROR = None

def _load_startup_log_from_disk() -> list:
    try:
        with open(_STARTUP_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("lines", []) if isinstance(data, dict) else []
    except Exception:
        return []

def _save_startup_log_to_disk(lines: list, boot_pid: int) -> None:
    global _STARTUP_LOG_LAST_DISK_ERROR
    try:
        with open(_STARTUP_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump({"boot_pid": boot_pid, "lines": lines}, f, ensure_ascii=False)
        _STARTUP_LOG_LAST_DISK_ERROR = None
    except Exception as _e_disk:
        # Pehle ye silently swallow ho jaata tha — ab error capture karte
        # hain taaki debug panel mein dikh sake ki disk-persist kyun fail
        # ho raha hai (permission/path/disk-full jaisi wajah).
        _STARTUP_LOG_LAST_DISK_ERROR = f"{type(_e_disk).__name__}: {_e_disk}"

_STARTUP_LOG: list = _load_startup_log_from_disk()

def _slog(msg: str, level: str = "info") -> None:
    """Thread-safe startup/diagnostic log line add karo (RAM hamesha,
    disk sirf login/update/error-relevant lines ke liye — see _PERSIST_TAGS).
    level: info|ok|warn|err"""
    global _STARTUP_LOG_DIRTY_COUNT
    try:
        t = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%H:%M:%S")
    except Exception:
        t = time.strftime("%H:%M:%S")
    line = {"t": t, "level": level, "msg": str(msg)}
    s = str(msg)
    # BN_BTN_CLICK / LOGIN_* / EXCEPTION / err-level → critical, turant flush.
    # SESSDBG / sess_active jaisi baar-baar aane wali lines → throttled flush
    # (kho nahi rahi, RAM me hai, disk-write bas thoda batch hoti hai).
    is_critical = any(tag in s for tag in
                       ("BN_BTN_CLICK", "LOGIN_MANUAL",
                        "LOGIN_URL_SECRET", "EXCEPTION")) or level == "err"
    is_persist_worthy = is_critical or any(tag in s for tag in _PERSIST_TAGS)
    with _STARTUP_LOG_LOCK:
        _STARTUP_LOG.append(line)
        if len(_STARTUP_LOG) > _STARTUP_LOG_MAX:
            del _STARTUP_LOG[: len(_STARTUP_LOG) - _STARTUP_LOG_MAX]
        if is_critical:
            _STARTUP_LOG_DIRTY_COUNT = 0
            _save_startup_log_to_disk(_STARTUP_LOG, os.getpid())
        elif is_persist_worthy:
            _STARTUP_LOG_DIRTY_COUNT += 1
            if _STARTUP_LOG_DIRTY_COUNT >= _STARTUP_LOG_FLUSH_EVERY:
                _STARTUP_LOG_DIRTY_COUNT = 0
                _save_startup_log_to_disk(_STARTUP_LOG, os.getpid())

def _slog_exception(where: str, exc: Exception) -> None:
    """Exception ko poori traceback ke saath log karo — copy-paste karne layak."""
    import traceback
    tb = traceback.format_exc()
    _slog(f"EXCEPTION in {where}: {exc}\n{tb}", level="err")

def _startup_log_snapshot() -> list:
    with _STARTUP_LOG_LOCK:
        return list(_STARTUP_LOG)

# ── Fresh-boot detection — ab RAM-list ki jagah disk-saved boot_pid se
# compare hota hai (see NOTE above). YAHIN capture karna zaroori hai, is
# point se aage koi bhi _slog() call nahi hona chahiye jo isse pehle chale
# — warna PID file update ho jaayegi ek naya check karne se pehle hi.
_prev_boot_pid = None
try:
    with open(_STARTUP_LOG_FILE, "r", encoding="utf-8") as _f_bp:
        _prev_boot_pid = json.load(_f_bp).get("boot_pid")
except Exception:
    _prev_boot_pid = None
_is_fresh_boot = (_prev_boot_pid != os.getpid())

# ─── Global live-tick store (updated by WebSocket thread) ─────────────────────
_LIVE: dict = {
    "ltp":       None,
    "prev_close": None,
    "ts":        0,
    "source":    None,   # "ws" (Fyers WebSocket push) | "rest" (1s REST-poll fallback)
}
_LIVE_LOCK = threading.Lock()

# ─── BankNifty WS/REST feed health — DEBUGGING ke liye ─────────────────────
# PEHLE: _on_ws_message/_on_ws_error/_on_ws_close/_rest_live_loop ke andar
# saare exceptions `except: pass` se chup-chaap gira diye jaate the — jab
# feed 82s+ stale ho jaata tha, koi bhi log/reason kahin nahi dikhta tha
# (na WS disconnect, na REST call fail, kuch nahi). Isliye ye dict +
# _slog() calls add kiye — ab startup-debug-log (header ke 🐞 icon se) mein
# turant pata chalega ki WS disconnect hua ya REST fallback fail ho raha
# hai, aur exact exception/traceback ke saath.
_BN_FEED_DEBUG: dict = {
    "ws_connected":        False,
    "ws_last_connect_ts":  0,
    "ws_last_message_ts":  0,
    "ws_last_error":       None,
    "ws_last_close":       None,
    "rest_last_attempt_ts": 0,
    "rest_last_success_ts": 0,
    "rest_last_error":     None,
}
_BN_FEED_DEBUG_LOCK = threading.Lock()

# ─── Per-minute candle tracker — resets at each new minute boundary ────────────
_CANDLE: dict = {"minute": None, "open": None, "high": None, "low": None}
_CANDLE_LOCK = threading.Lock()

def _update_candle_ltp(ltp: float) -> None:
    """Feed one LTP tick into the running 1-minute candle."""
    now_sec      = int(time.time())
    minute_epoch = (now_sec // 60) * 60
    closed = None
    with _CANDLE_LOCK:
        if _CANDLE["minute"] != minute_epoch:
            if _CANDLE["minute"] is not None and _CANDLE["open"] is not None:
                closed = {"time": _CANDLE["minute"], "open": _CANDLE["open"],
                          "high": _CANDLE["high"], "low": _CANDLE["low"],
                          "close": _CANDLE.get("last") if _CANDLE.get("last") is not None else _CANDLE["open"]}
            _CANDLE["minute"] = minute_epoch
            _CANDLE["open"]   = ltp
            _CANDLE["high"]   = ltp
            _CANDLE["low"]    = ltp
        else:
            if ltp > (_CANDLE["high"] or ltp): _CANDLE["high"] = ltp
            if ltp < (_CANDLE["low"]  or ltp): _CANDLE["low"]  = ltp
        _CANDLE["last"] = ltp
    if closed is not None:
        # 1-min candle band hua — final OHLC ka explicit event (lock ke bahar)
        try:
            _ws_publish("bn_candle", {"event": "close", "tf": "1m", "candle": closed, "ts": time.time()})
        except Exception:
            pass

def _set_candle_from_bar(minute_epoch: int, o: float, h: float, l: float, c: float) -> None:
    """Populate candle directly from a complete 1-min OHLC bar (REST path)."""
    with _CANDLE_LOCK:
        _CANDLE["minute"] = minute_epoch
        _CANDLE["open"]   = o
        _CANDLE["high"]   = h
        _CANDLE["low"]    = l
        _CANDLE["last"]   = c

# ─── IST helper ───────────────────────────────────────────────────────────────
def _ist_now():
    return datetime.datetime.now(IST)

def _fmt_last_candle_date(real_utc_epoch: int, daily: bool = False) -> str:
    """(2026-09-15) 'Update All Files Now' ke result-messages mein aakhri
    candle ki readable date/time dikhane ke liye — taaki update ke baad
    turant pata chal jaaye ki data SAHI mein taaza hua ya nahi (sirf 'ok'
    msg se pata nahi chalta tha). real_utc_epoch: asli UTC epoch (seconds).
    daily=True to sirf date (Nifty500/BTC 1D jaise daily-candle sources ke
    liye), warna date+time (5m candle sources ke liye)."""
    dt = datetime.datetime.fromtimestamp(real_utc_epoch, tz=IST)
    return dt.strftime("%Y-%m-%d") if daily else dt.strftime("%Y-%m-%d %H:%M IST")

# ─── Credential helpers ───────────────────────────────────────────────────────
def load_creds() -> dict:
    if os.path.exists(CREDS_FILE):
        try:
            with open(CREDS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_creds(d: dict):
    with open(CREDS_FILE, "w") as f:
        json.dump(d, f)

def fyers_get_access_token(app_id: str, secret_key: str, auth_code: str) -> tuple[bool, str, dict]:
    """Step 5: exchange auth_code for access_token. Returns (ok, token_or_msg, full_response)."""
    app_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()
    payload = {"grant_type": "authorization_code", "appIdHash": app_hash, "code": auth_code}
    try:
        resp = requests.post(
            "https://api-t1.fyers.in/api/v3/validate-authcode",
            json=payload,
            timeout=10,
        )
        raw = {}
        try:
            raw = resp.json()
        except Exception:
            raw = {"raw_text": resp.text, "status_code": resp.status_code}
        # Log to file for debugging
        _write_login_log(payload, resp.status_code, raw)
        if raw.get("s") == "ok" and "access_token" in raw:
            return True, raw["access_token"], raw
        return False, raw.get("message", str(raw)), raw
    except Exception as e:
        err = {"exception": str(e)}
        _write_login_log(payload, 0, err)
        return False, str(e), err


# ─── Funds (Available Balance) + Nearest Strike helpers ───────────────────────
FYERS_META_FILE = "fyers_meta.json"
_FYERS_META_CACHE = {"balance": None, "strike": None, "ts": 0.0}
_FYERS_META_LOCK = threading.Lock()
_FYERS_META_TTL = 20  # seconds — funds API ko itni jaldi baar-baar hit nahi karna

BN_STRIKE_STEP = 100  # BankNifty option strikes 100 ke multiples mein hote hain

def fyers_get_available_balance(app_id: str, access_token: str) -> tuple[bool, "float | str"]:
    """Fyers /api/v3/funds se 'Available Balance' nikalta hai."""
    try:
        headers = {"Authorization": f"{app_id}:{access_token}"}
        r = requests.get(
            "https://api-t1.fyers.in/api/v3/funds",
            headers=headers, timeout=6,
        ).json()
        if r.get("s") != "ok":
            return False, r.get("message", str(r))
        for item in r.get("fund_limit", []):
            if item.get("title") == "Available Balance":
                return True, float(item.get("equityAmount", 0))
        return False, "Available Balance field not found"
    except Exception as e:
        return False, str(e)

def get_nearest_bn_strike() -> "int | None":
    """Current live BankNifty LTP se nazdiktareen strike (round to 100) nikalta hai."""
    ltp = _LIVE.get("ltp")
    if not ltp:
        return None
    return int(round(ltp / BN_STRIKE_STEP) * BN_STRIKE_STEP)

def refresh_fyers_meta_cache() -> dict:
    """Balance + nearest strike ko cache karta hai (TTL ke andar dobara fetch nahi karta),
    aur fyers_meta.json mein likh deta hai taaki chart iframe use poll kar sake."""
    now = time.time()
    with _FYERS_META_LOCK:
        stale = (now - _FYERS_META_CACHE["ts"]) >= _FYERS_META_TTL
    if stale:
        creds = load_creds()
        balance = _FYERS_META_CACHE["balance"]
        if creds.get("access_token") and creds.get("app_id"):
            ok, val = fyers_get_available_balance(creds["app_id"], creds["access_token"])
            if ok:
                balance = val
        strike = get_nearest_bn_strike()
        with _FYERS_META_LOCK:
            _FYERS_META_CACHE.update({"balance": balance, "strike": strike, "ts": now})
    with _FYERS_META_LOCK:
        payload = dict(_FYERS_META_CACHE)
    try:
        with open(FYERS_META_FILE, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass
    return payload



# ─── Real Option Chain (CE/PE, LTP, OI, Chg%) — jaisa broker app mein hota hai ──
OC_FILE   = "fyers_optionchain.json"
_OC_CACHE = {"data": None, "ts": 0.0}
_OC_LOCK  = threading.Lock()
_OC_TTL   = 2  # seconds — real-broker jaisa near-live feel, phir bhi rate-limit safe
_OC_DEBUG = {"last_error": "", "last_status": None, "last_url": "", "ts": 0.0}

BN_OC_SYMBOL = "NSE:NIFTYBANK-INDEX"

def fyers_get_option_chain(app_id: str, access_token: str, symbol: str = BN_OC_SYMBOL,
                            strikecount: int = 10, timestamp: str = "") -> "dict | None":
    """Fyers Option Chain API se CE/PE strikes fetch karta hai.
    Primary domain fail ho to fallback domain try karta hai (Fyers docs mein
    dono variants dikhte hain). Har attempt ki debug info _OC_DEBUG mein
    save hoti hai taaki failure ka exact reason pata chal sake."""
    headers = {"Authorization": f"{app_id}:{access_token}"}
    params = {"symbol": symbol, "strikecount": strikecount, "timestamp": timestamp}
    urls = [
        "https://api-t1.fyers.in/data/options-chain",
        "https://api.fyers.in/v3/data/options-chain",
        "https://api-t1.fyers.in/data/options-chain-v3",
    ]
    raw = None
    last_err = ""
    last_status = None
    last_url = ""
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=8)
            last_status = resp.status_code
            last_url = url
            try:
                r = resp.json()
            except Exception:
                last_err = f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"
                continue
            if r.get("s") == "ok":
                raw = r
                break
            else:
                last_err = r.get("message", str(r))[:300]
        except Exception as e:
            last_err = str(e)
            last_url = url

    _OC_DEBUG.update({"last_error": last_err, "last_status": last_status,
                       "last_url": last_url, "ts": time.time()})

    if not raw:
        return None

    d = raw.get("data", {})
    chain = d.get("optionsChain", [])

    # Underlying/spot entry pehchano (isme option_type nahi hota, 'fp' field hoti hai)
    spot = None
    for item in chain:
        if not item.get("option_type"):
            spot = item.get("ltp") or item.get("fp")
            break
    if spot is None:
        spot = _LIVE.get("ltp")

    # Strike-wise CE/PE group karo
    rows_map: dict = {}
    for item in chain:
        ot = item.get("option_type")
        if ot not in ("CE", "PE"):
            continue
        strike = item.get("strike_price")
        if strike is None:
            continue
        row = rows_map.setdefault(strike, {"strike": strike, "ce": None, "pe": None})
        leg = {
            "ltp":   item.get("ltp", 0),
            "chg":   item.get("ltpch", 0),
            "chgp":  item.get("ltpchp", 0),
            "oi":    item.get("oi", 0),
            "oich":  item.get("oich", 0),
            "oichp": item.get("oichp", 0),
            "volume":item.get("volume", 0),
            "bid":   item.get("bid", 0),
            "ask":   item.get("ask", 0),
            "symbol":item.get("symbol", ""),
        }
        if ot == "CE":
            row["ce"] = leg
        else:
            row["pe"] = leg

    rows = sorted(rows_map.values(), key=lambda x: x["strike"])
    atm = get_nearest_bn_strike() if spot is None else int(round(spot / BN_STRIKE_STEP) * BN_STRIKE_STEP)

    expiries = d.get("expiryData", [])
    selected_expiry_label = expiries[0].get("date") if expiries else ""
    # Fyers "expiry" field on the expiryData item is epoch seconds (string) —
    # frontend Rollover/Greeks features need this to compute time-to-expiry.
    try:
        selected_expiry_epoch = int(expiries[0].get("expiry")) if expiries and expiries[0].get("expiry") else None
    except (TypeError, ValueError):
        selected_expiry_epoch = None

    return {
        "spot": spot,
        "atm": atm,
        "rows": rows,
        "call_oi": d.get("callOi", 0),
        "put_oi": d.get("putOi", 0),
        "expiries": expiries,
        "expiry_label": selected_expiry_label,
        "expiry_epoch": selected_expiry_epoch,
        "ts": time.time(),
    }

# --- Render relay (main.ts): public market data (klines / time / exchangeInfo) yahin se aata hai.
# Trading (orders/positions/balance) browser se seedha Render ko jaati hai - HF beech me nahi.
BINANCE_BASE_URL = "https://replay-qda7.onrender.com"
BINANCE_EAPI_URL = "https://replay-qda7.onrender.com"

# ─── Server-side Network Diagnostic (HF Space se) ──────────────────────────
# Ye phone-browser wale ws-test.html jaisa hi test hai, bas yahan HF Space
# ke SERVER (jahan ye poora app.py Streamlit process chalta hai) se chalta
# hai — Render relay REST, aur seedhe (Render-bypass) Binance REST + WS
# domains (spot/futures/options) sab test karta hai. Phone ke result se
# compare karke pata chalta hai block kahan hai: sirf phone/carrier-level,
# sirf HF-datacenter-level, ya dono jagah (matlab Binance/Options-WS ka
# global infra issue).
def _diag_ws_test(url: str, timeout_s: float = 8.0) -> tuple:
    """Ek WS URL blocking-mode mein connect karta hai (websocket-client ki
    create_connection) — OPEN + ek message padhne ki koshish, phir clean
    close. Returns (ok: bool, detail: str)."""
    if websocket is None:
        return False, "websocket-client package missing"
    t0 = time.time()
    try:
        ws = websocket.create_connection(url, timeout=timeout_s)
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return False, f"FAILED to open after {ms}ms — {type(e).__name__}: {e}"
    ms_open = int((time.time() - t0) * 1000)
    try:
        ws.settimeout(timeout_s)
        msg = ws.recv()
        got_msg = f"first message received ({len(msg)} bytes) — feed is live ✅"
    except Exception as e:
        got_msg = f"opened, but no message received — {type(e).__name__}: {e}"
    try:
        ws.close()
    except Exception:
        pass
    return True, f"OPEN ✅ in {ms_open}ms — {got_msg}"

def _diag_rest_test(url: str, timeout_s: float = 10.0) -> tuple:
    """Direct REST GET test."""
    t0 = time.time()
    try:
        r = requests.get(url, timeout=timeout_s)
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            return True, f"OK — HTTP 200 in {ms}ms"
        return False, f"HTTP {r.status_code} in {ms}ms — {r.text[:150]}"
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return False, f"FAILED after {ms}ms — {type(e).__name__}: {e}"

def _diag_fetch_live_option_symbol() -> str:
    """Render relay se (already-working path) nearest-expiry, still-TRADING
    BTC option symbol nikalta hai — taaki phone-test jaisa hi REAL symbol
    server-side WS test mein bhi use ho, kisi expired/fake symbol se galat
    fail na aaye."""
    try:
        ok, info = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/exchangeInfo", {}, "", signed=False)
        if not ok or not isinstance(info, dict):
            return ""
        now_ms = int(time.time() * 1000)
        syms = [
            s for s in (info.get("optionSymbols") or [])
            if s.get("underlying") == "BTCUSDT"
            and s.get("expiryDate", 0) > now_ms
            and s.get("status", "TRADING") == "TRADING"
        ]
        syms.sort(key=lambda s: s.get("expiryDate", 0))
        return syms[0]["symbol"] if syms else ""
    except Exception:
        return ""

def _run_network_diagnostic() -> str:
    """Poora diagnostic chalata hai aur ek markdown-formatted result string
    return karta hai — login page ke 'Network Diagnostic' card mein dikhaya
    jaata hai."""
    lines = []

    lines.append("**A — Render relay (existing working path) REST**")
    ok, msg = _diag_rest_test(f"{BINANCE_EAPI_URL}/eapi/v1/time")
    lines.append(f"{'✅' if ok else '❌'} Render → `/eapi/v1/time`: {msg}")

    sym = _diag_fetch_live_option_symbol()
    if sym:
        lines.append(f"🔎 Live option symbol auto-picked (via Render): `{sym}`")
    else:
        sym = "BTC-260926-84000-C"
        lines.append(f"⚠️ Live symbol fetch fail — fallback use ho raha: `{sym}`")

    lines.append("\n**B — HF Space SERVER se seedha Binance WS (Render bypass)**")
    for label, url in [
        ("Spot WS (stream.binance.com)",           "wss://stream.binance.com:9443/ws/btcusdt@trade"),
        ("Futures WS (fstream.binance.com)",       "wss://fstream.binance.com/ws/btcusdt@markPrice"),
        (f"Options WS depth@100ms ({sym})",        f"wss://nbstream.binance.com/eoptions/ws/{sym}@depth@100ms"),
        (f"Options WS depth@500ms ({sym})",        f"wss://nbstream.binance.com/eoptions/ws/{sym}@depth@500ms"),
        ("Options WS WRONG symbol (control)",      "wss://nbstream.binance.com/eoptions/ws/btcusdt@depth@100ms"),
        ("Echo WS (generic control)",               "wss://echo.websocket.org"),
    ]:
        ok, msg = _diag_ws_test(url)
        lines.append(f"{'✅' if ok else '❌'} {label}: {msg}")

    lines.append("\n**C — HF Space SERVER se seedha Binance REST (Render bypass)**")
    for label, url in [
        ("Spot REST (api.binance.com)",     "https://api.binance.com/api/v3/ping"),
        ("Options REST (eapi.binance.com)", "https://eapi.binance.com/eapi/v1/ping"),
    ]:
        ok, msg = _diag_rest_test(url)
        lines.append(f"{'✅' if ok else '❌'} {label}: {msg}")

    return "\n\n".join(lines)

# ─── Binance "kya waqai blocked hai?" — deep server-side diagnostic (JSON) ───
# HF Space SERVER se (jahan ye app.py chalta hai) Binance ke saare public
# REST + WebSocket endpoints (spot / USDⓈ-M futures / COIN-M / options) ko
# layer-by-layer test karta hai: DNS -> TCP -> TLS -> HTTP/WS handshake -> data.
# Saath me controls (Google, Cloudflare, Bybit, OKX, Coinbase, Kraken)
# bhi chalte hain taaki pata chale block Binance-specific hai ya poora
# outbound hi limited hai. Output pure JSON dict (koi table nahi) — sabse upar
# "summary" me verdict + likely_cause, neeche har test ka raw evidence.
_BBD_KEEP_HDRS = {
    "server", "via", "x-cache", "x-amz-cf-id", "x-amz-cf-pop", "cf-ray",
    "cf-cache-status", "x-mbx-uuid", "x-mbx-used-weight-1m", "retry-after",
    "location", "content-type", "date", "x-served-by", "x-request-id",
}
_BBD_REACHABLE = {"ok", "reachable_auth_required", "reachable_bad_request", "redirect"}
_BBD_BLOCKED = {"cdn_challenge_202", "egress_proxy_block", "geo_block_451", "cdn_block_403_cloudfront", "cdn_block_403_cloudflare",
                "forbidden_403", "ip_ban_418", "rate_limit_429"}


def _bbd_ms(t0) -> int:
    return int((time.time() - t0) * 1000)


def _bbd_exc_class(e) -> str:
    """Exception ko network-layer category me badalta hai."""
    msg = f"{type(e).__name__}: {e}".lower()
    if ("name or service not known" in msg or "getaddrinfo" in msg
            or "nodename nor servname" in msg or "temporary failure in name resolution" in msg):
        return "dns_fail"
    if "timed out" in msg or "timeout" in msg:
        return "timeout"
    if "connection refused" in msg or "errno 111" in msg:
        return "tcp_refused"
    if "reset by peer" in msg or "connection reset" in msg or "errno 104" in msg:
        return "tcp_reset"
    if "network is unreachable" in msg or "no route to host" in msg:
        return "no_route"
    if "proxy" in msg:
        return "proxy_error"
    if "ssl" in msg or "certificate" in msg:
        return "tls_fail"
    if "remote end closed" in msg or "connection aborted" in msg or "eof" in msg:
        return "conn_closed"
    return "other_error"


def _bbd_http_class(status, body, hdrs) -> str:
    b = (body or "").lower()
    h = " ".join(f"{k}:{v}" for k, v in (hdrs or {}).items()).lower()
    if status in (200, 204):
        return "ok"
    if status == 202:
        return "cdn_challenge_202"       # CloudFront/WAF ne challenge page diya (asli content nahi)
    if status == 451 or "restricted location" in b:
        return "geo_block_451"
    if "not in allowlist" in b or "egress settings" in b:
        return "egress_proxy_block"      # server ke beech ka proxy/firewall allowlist se rok raha
    if status == 403:
        if "cloudfront" in b or "cloudfront" in h or "x-amz-cf" in h:
            return "cdn_block_403_cloudfront"
        if "cloudflare" in b or "cf-ray" in h:
            return "cdn_block_403_cloudflare"
        return "forbidden_403"
    if status == 418:
        return "ip_ban_418"
    if status == 429:
        return "rate_limit_429"
    if status == 401:
        return "reachable_auth_required"
    if status in (400, 404, 405):
        return "reachable_bad_request"
    if 300 <= status < 400:
        return "redirect"
    if status >= 500:
        return "upstream_5xx"
    return f"http_{status}"


def _bbd_rest(url, timeout=10, headers=None, head=300, method="GET") -> dict:
    """Ek REST call — sirf pehla chunk padhta hai (bade exchangeInfo se bachne ko)."""
    t0 = time.time()
    try:
        r = requests.request(method, url, timeout=timeout, headers=headers or {},
                             allow_redirects=False, stream=True)
        try:
            chunk = next(r.iter_content(2048), b"")
        except Exception:
            chunk = b""
        body = chunk.decode("utf-8", "replace")
        hdrs = {k: v for k, v in r.headers.items() if k.lower() in _BBD_KEEP_HDRS}
        st_code = r.status_code
        try:
            r.close()
        except Exception:
            pass
        return {"class": _bbd_http_class(st_code, body, hdrs), "status": st_code,
                "ms": _bbd_ms(t0), "t_recv_ms": int(time.time() * 1000), "json_like": body.lstrip()[:1] in ("{", "["),
                "headers": hdrs, "body_head": body[:head]}
    except Exception as e:
        return {"class": _bbd_exc_class(e), "status": None, "ms": _bbd_ms(t0),
                "error": f"{type(e).__name__}: {str(e)[:250]}"}


def _bbd_ws(url, timeout=8, send=None, origin=None, recv_timeout=6, min_msgs=1) -> dict:
    """Ek WebSocket test: handshake -> (optional subscribe msg) -> messages.
    min_msgs=2 tab use karo jab subscribe-RPC ka ack pehla msg hota hai (data doosra)."""
    if websocket is None:
        return {"class": "lib_missing", "error": "websocket-client package missing"}
    t0 = time.time()
    try:
        kw = {"timeout": timeout}
        if origin:
            kw["origin"] = origin
        ws = websocket.create_connection(url, **kw)
    except Exception as e:
        code = getattr(e, "status_code", None)
        if code is not None:      # WebSocketBadStatusException — server ne handshake reject kiya
            body = getattr(e, "resp_body", b"")
            if isinstance(body, (bytes, bytearray)):
                body = bytes(body).decode("utf-8", "replace")
            hdrs = {str(k): str(v) for k, v in (getattr(e, "resp_headers", None) or {}).items()
                    if str(k).lower() in _BBD_KEEP_HDRS}
            return {"class": f"ws_handshake_http_{code}", "http_class": _bbd_http_class(code, body, hdrs),
                    "status": code, "ms": _bbd_ms(t0), "headers": hdrs,
                    "body_head": str(body)[:300], "error": f"{type(e).__name__}: {str(e)[:200]}"}
        return {"class": _bbd_exc_class(e), "ms": _bbd_ms(t0),
                "error": f"{type(e).__name__}: {str(e)[:250]}"}
    open_ms = _bbd_ms(t0)
    res = {"class": "ws_open_no_data", "open_ms": open_ms, "messages": 0}
    heads = []
    try:
        if send is not None:
            ws.send(send if isinstance(send, str) else json.dumps(send))
        deadline = time.time() + recv_timeout
        while len(heads) < max(1, min_msgs):
            left = deadline - time.time()
            if left <= 0:
                res["recv_error"] = "no data within %ss" % recv_timeout
                break
            ws.settimeout(left)
            m = ws.recv()
            heads.append((m if isinstance(m, str) else repr(m)))
            if len(heads) == 1:
                res["first_msg_ms"] = _bbd_ms(t0)
                res["first_msg_bytes"] = len(m)
        res["messages"] = len(heads)
        if heads:
            res["first_msg_head"] = heads[0][:200]
        if len(heads) > 1:
            res["second_msg_head"] = heads[1][:200]
        if len(heads) >= max(1, min_msgs):
            res["class"] = "ws_live"
    except Exception as e:
        res["messages"] = len(heads)
        if heads:
            res["first_msg_head"] = heads[0][:200]
        res["recv_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    try:
        ws.close()
    except Exception:
        pass
    return res


def _bbd_ws_soak(url, seconds=15, send=None) -> dict:
    """Feed stability: `seconds` tak WS khula rakh ke messages gine (rate, max gap, drop)."""
    if websocket is None:
        return {"class": "lib_missing"}
    t0 = time.time()
    try:
        ws = websocket.create_connection(url, timeout=8)
    except Exception as e:
        code = getattr(e, "status_code", None)
        return {"class": f"ws_handshake_http_{code}" if code else _bbd_exc_class(e),
                "error": f"{type(e).__name__}: {str(e)[:200]}"}
    if send is not None:
        try:
            ws.send(send if isinstance(send, str) else json.dumps(send))
        except Exception:
            pass
    n, first_ms, last_t, max_gap, closed_at = 0, None, None, 0.0, None
    end = time.time() + seconds
    while time.time() < end:
        try:
            ws.settimeout(min(3, max(0.2, end - time.time())))
            ws.recv()
            now = time.time()
            n += 1
            if first_ms is None:
                first_ms = int((now - t0) * 1000)
            if last_t is not None:
                max_gap = max(max_gap, now - last_t)
            last_t = now
        except Exception as e:
            if "timed out" in str(e).lower() or "timeout" in str(e).lower():
                continue
            closed_at = round(time.time() - t0, 1)
            break
    try:
        ws.close()
    except Exception:
        pass
    return {"class": "ws_live" if n else "ws_open_no_data", "seconds": seconds, "messages": n,
            "msgs_per_sec": round(n / float(seconds), 2), "first_msg_ms": first_ms,
            "max_gap_s": round(max_gap, 2), "closed_early_at_s": closed_at}


def _bbd_app_url_audit(rest_res, ws_res) -> dict:
    """App ke apne source (app.py, chart.html, baaki .py/.html) me se saare Binance / Render
    URLs nikalta hai (READ-ONLY, koi code nahi badalta) aur har host ko upar ke test
    result se milata hai — taaki screenshot ki zaroorat na pade."""
    import re, glob
    base = os.path.dirname(os.path.abspath(__file__))
    pat = re.compile(r"(?:https?|wss?)://[^\s'\"`)<>\\,;]+")
    cmt_js = re.compile(r"(?<!:)//")
    # host -> status (server test se)
    hstat = {}
    for g in list(rest_res) + list(ws_res):
        for it in (rest_res.get(g, []) + ws_res.get(g, [])):
            u = it.get("url") or ""
            if "://" not in u:
                continue
            h = u.split("/")[2]
            hstat.setdefault(h, set()).add(it.get("class", "?"))
    def _st(h):
        cs = hstat.get(h.split(":")[0]) or hstat.get(h)
        if not cs:
            return "not_tested_directly"
        if cs & {"ok", "ws_live"}:
            return "OK_from_server"
        if cs & {"geo_block_451", "ws_handshake_http_451"}:
            return "451_BLOCKED_from_server"
        return "other:" + ",".join(sorted(cs))[:60]
    found, files, ident_use = {}, [], {}
    for fp in sorted(glob.glob(os.path.join(base, "*.py")) + glob.glob(os.path.join(base, "*.html"))):
        try:
            if os.path.getsize(fp) > 8_000_000:
                continue
            txt = open(fp, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        fn = os.path.basename(fp)
        files.append(fn)
        is_py = fn.endswith(".py")
        _lines = txt.splitlines()
        skip = []   # diagnostic ka apna code audit se bahar (warna khud ke test URLs 'app usage' lagte)
        if is_py:
            def _idx(pred, start=0):
                for k in range(start, len(_lines)):
                    if pred(_lines[k]):
                        return k
                return None
            a1 = _idx(lambda L: L.startswith("# ") and "Server-side Network Diagnostic (HF Space se)" in L)
            b1 = _idx(lambda L: L.startswith("def _binance_server_time"), a1 or 0) if a1 is not None else None
            if a1 is not None and b1 is not None and b1 > a1:
                skip.append((a1 + 1, b1))
            a2 = _idx(lambda L: L.lstrip().startswith("st.markdown(") and "login-title" in L and "Network Diagnostic (Server-side)" in L)
            b2 = _idx(lambda L: L.lstrip().startswith("# ") and "Direct HTTP Readiness" in L, a2) if a2 is not None else None
            if a2 is not None and b2 is not None and b2 > a2:
                skip.append((a2 - 6, b2))
        for ln, line in enumerate(_lines, 1):
            if any(x <= ln <= y for x, y in skip):
                continue
            if is_py:
                for ident in ("BINANCE_BASE_URL", "BINANCE_EAPI_URL"):
                    if ident in line and not line.lstrip().startswith("#") and not line.lstrip().startswith(ident + " ="):
                        d = ident_use.setdefault(ident, {"count": 0, "lines": []})
                        d["count"] += 1
                        if len(d["lines"]) < 40:
                            d["lines"].append(ln)
            for m in pat.finditer(line):
                u = m.group(0).rstrip(".:'\"")
                host = u.split("/")[2] if u.count("/") >= 2 else ""
                if not ("binance" in host or "onrender" in host) or "{" in host or "$" in host:
                    continue
                pre = line[:m.start()]
                st = line.lstrip()
                in_c = (("#" in pre) if is_py else bool(cmt_js.search(pre))) or st.startswith(("#", "//", "*", "/*", "<!--"))
                key = (fn, u[:150], in_c)
                e = found.setdefault(key, {"n": 0, "lines": []})
                e["n"] += 1
                if len(e["lines"]) < 6:
                    e["lines"].append(ln)
    # untested binance hosts: chhota live check (render host ko nahi chhedte)
    extra = sorted({k[1].split("/")[2] for k in found if "binance" in k[1].split("/")[2] and _st(k[1].split("/")[2]) == "not_tested_directly"})[:10]
    if extra:
        with ThreadPoolExecutor(max_workers=6) as _ex:
            fut = {h: _ex.submit(_bbd_rest, "https://" + h.split(":")[0] + "/", 8, None, 80) for h in extra if not h.startswith("ws")}
            for h, f in fut.items():
                try:
                    hstat.setdefault(h.split(":")[0], set()).add(f.result(timeout=15).get("class", "?"))
                except Exception:
                    pass
    code_urls, hosts = [], {}
    for (fn, u, in_c), e in sorted(found.items()):
        h = u.split("/")[2]
        row = {"file": fn, "url": u, "in_comment": in_c, "count": e["n"], "lines": e["lines"],
               "host_status": ("RENDER_RELAY (probe nahi kiya)" if "onrender" in h else _st(h))}
        code_urls.append(row)
        hh = hosts.setdefault(h, {"host_status": row["host_status"], "refs_in_code": 0, "refs_in_comments": 0, "files": set()})
        hh["refs_in_comments" if in_c else "refs_in_code"] += e["n"]
        hh["files"].add(fn)
    for h in hosts.values():
        h["files"] = sorted(h["files"])
    return {"files_scanned": files, "render_dependency_via_variables": ident_use,
            "render_dependency_note": "BINANCE_BASE_URL / BINANCE_EAPI_URL dono replay-qda7.onrender.com (Render) ki taraf point karte hain — in variables ka har use = Render pe nirbharta.",
            "by_host": hosts,
            "problem_hosts_used_in_code": sorted(h for h, v in hosts.items()
                                                 if v["refs_in_code"] and (v["host_status"].startswith("451") or "RENDER" in v["host_status"])),
            "code_urls": [r for r in code_urls if not r["in_comment"]][:200],
            "comment_only_urls_count": sum(1 for r in code_urls if r["in_comment"])}


def _bbd_host_layers(host, port=443, timeout=6) -> dict:
    """DNS -> TCP connect -> TLS handshake (cert bhi). Har layer alag report."""
    import socket, ssl
    out = {"host": host, "port": port}
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        out["dns"] = {"ok": True, "ms": _bbd_ms(t0), "ips": ips[:8]}
    except Exception as e:
        out["dns"] = {"ok": False, "ms": _bbd_ms(t0), "class": "dns_fail",
                      "error": f"{type(e).__name__}: {e}"}
        return out
    t1 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        out["tcp"] = {"ok": True, "ms": _bbd_ms(t1), "peer": s.getpeername()[0]}
    except Exception as e:
        out["tcp"] = {"ok": False, "ms": _bbd_ms(t1), "class": _bbd_exc_class(e),
                      "error": f"{type(e).__name__}: {e}"}
        return out
    t2 = time.time()
    try:
        ss = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        cert = ss.getpeercert() or {}
        subj = dict(x[0] for x in cert.get("subject", []))
        iss = dict(x[0] for x in cert.get("issuer", []))
        out["tls"] = {"ok": True, "ms": _bbd_ms(t2), "version": ss.version(),
                      "cipher": (ss.cipher() or [None])[0],
                      "cert_cn": subj.get("commonName"),
                      "cert_issuer": iss.get("organizationName") or iss.get("commonName"),
                      "cert_not_after": cert.get("notAfter")}
        ss.close()
    except Exception as e:
        out["tls"] = {"ok": False, "ms": _bbd_ms(t2), "class": "tls_fail",
                      "error": f"{type(e).__name__}: {str(e)[:200]}"}
        try:
            s.close()
        except Exception:
            pass
    return out


def _bbd_server_info() -> dict:
    """Server ka public IP / country / ASN — geo-block samajhne ke liye."""
    info = {}
    for url in ("https://ipinfo.io/json", "https://api.ipify.org?format=json"):
        try:
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                j = r.json()
                info = {k: j.get(k) for k in ("ip", "city", "region", "country", "org", "timezone") if j.get(k)}
                info["source"] = url
                break
        except Exception as e:
            info.setdefault("errors", []).append(f"{url}: {type(e).__name__}: {str(e)[:120]}")
    return info


def _bbd_parse_opt(sym):
    """'BTC-260930-81000-C' -> (expiry_ms(08:00 UTC), strike, 'C'/'P') ya None."""
    try:
        u, ymd, k, cp = str(sym).split("-")
        y, m, d = 2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])
        exp = int(datetime.datetime(y, m, d, 8, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        return exp, float(k), cp, ymd
    except Exception:
        return None


def _bbd_pick_option_symbol(now_ms) -> dict:
    """Live BTC option symbol — pehle WS optionMarkPrice se (jo server se chal raha hai,
    koi REST/relay nahi); fail ho to direct eapi REST. ATM strike + nearest expiry (>2h door)."""
    res = {"symbol": None, "source": None, "expiry_code": None, "index_price": None,
           "symbols_in_stream": 0, "expiries_seen": [], "errors": []}
    if websocket is not None:
        try:
            ws = websocket.create_connection("wss://fstream.binance.com/market/ws/btcusdt@optionMarkPrice", timeout=10)
            ws.settimeout(10)
            raw = ws.recv()
            try:
                ws.close()
            except Exception:
                pass
            d = json.loads(raw)
            items = d if isinstance(d, list) else (d.get("data") if isinstance(d, dict) and isinstance(d.get("data"), list) else [d])
            rows = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                pr = _bbd_parse_opt(it.get("s"))
                if pr and pr[0] > now_ms + 2 * 3600 * 1000 and pr[2] == "C":
                    rows.append((pr, it))
            res["symbols_in_stream"] = len(items)
            if rows:
                res["expiries_seen"] = sorted({r[0][3] for r in rows})[:6]
                near = min(r[0][0] for r in rows)
                same = [r for r in rows if r[0][0] == near]
                idx = None
                for _, it in same:
                    try:
                        idx = float(it.get("i"))
                        break
                    except Exception:
                        pass
                res["index_price"] = idx
                def _key(r):
                    bo = 0.0
                    try:
                        bo = float(r[1].get("bo") or 0)
                    except Exception:
                        pass
                    dist = abs(r[0][1] - idx) if idx else r[0][1]
                    return (0 if bo > 0 else 1, dist)
                best = min(same, key=_key)
                res.update(symbol=best[1]["s"], source="ws fstream/market optionMarkPrice",
                           expiry_code=best[0][3])
                return res
            res["errors"].append("ws optionMarkPrice: message aaya par live BTC call symbol nahi mila")
        except Exception as e:
            res["errors"].append(f"ws optionMarkPrice: {type(e).__name__}: {str(e)[:150]}")
    try:
        r = requests.get("https://eapi.binance.com/eapi/v1/exchangeInfo", timeout=15)
        if r.status_code == 200:
            syms = [x for x in (r.json().get("optionSymbols") or [])
                    if x.get("underlying") == "BTCUSDT" and x.get("expiryDate", 0) > now_ms + 2 * 3600 * 1000
                    and x.get("status", "TRADING") == "TRADING"]
            syms.sort(key=lambda x: x.get("expiryDate", 0))
            if syms:
                sym = syms[0]["symbol"]
                res.update(symbol=sym, source="direct eapi.binance.com REST", expiry_code=sym.split("-")[1])
                return res
            res["errors"].append("direct REST: exchangeInfo mila par live BTC symbol nahi")
        else:
            res["errors"].append(f"direct REST: HTTP {r.status_code} — {r.text[:120]}")
    except Exception as e:
        res["errors"].append(f"direct REST: {type(e).__name__}: {str(e)[:150]}")
    return res


def _bbd_count(items) -> dict:
    by = {}
    for it in items:
        c = it.get("class", "unknown")
        by[c] = by.get(c, 0) + 1
    reach = sum(v for k, v in by.items() if k in _BBD_REACHABLE)
    blk = sum(v for k, v in by.items() if k in _BBD_BLOCKED)
    return {"total": len(items), "reachable": reach, "blocked_by_server": blk,
            "network_fail": len(items) - reach - blk, "by_class": by}


def _bbd_options_history_probe(now_ms, live_sym) -> dict:
    """Options HISTORY test (backtest feature ke liye): kya HF server se eapi klines / exerciseHistory
    chalte hain — chalu contract ke liye bhi aur EXPIRE ho chuke contract ke liye bhi? Sirf public market-data."""
    B = "https://eapi.binance.com"
    DAY, HR = 86400000, 3600000
    tests = []

    def _get(name, path):
        url = B + path
        t0 = time.time()
        rec = {"name": name, "url": url}
        try:
            r = requests.get(url, timeout=10, allow_redirects=False)
            body = r.text[:300000]
            hdrs = {k: v for k, v in r.headers.items() if k.lower() in _BBD_KEEP_HDRS}
            rec.update({"status": r.status_code, "class": _bbd_http_class(r.status_code, body[:2048], hdrs),
                        "ms": _bbd_ms(t0), "head": body[:160]})
            try:
                j = json.loads(body)
            except Exception:
                j = None
            if isinstance(j, list):
                rec["n"] = len(j)
            tests.append(rec)
            return j
        except Exception as e:
            rec.update({"class": _bbd_exc_class(e), "status": None, "ms": _bbd_ms(t0),
                        "error": f"{type(e).__name__}: {str(e)[:200]}"})
            tests.append(rec)
            return None

    # 1) exerciseHistory — pichhle 5 din me expire hue contracts ki list (+ settlement)
    eh = _get("exerciseHistory (5 din)", f"/eapi/v1/exerciseHistory?underlying=BTCUSDT&startTime={now_ms - 5 * DAY}&endTime={now_ms}&limit=100")
    recs = []
    if isinstance(eh, list):
        for x in eh:
            if not isinstance(x, dict):
                continue
            sy = x.get("symbol") or x.get("s")
            ex_ms = x.get("expiryDate") or x.get("expiry")
            try:
                ex_ms = int(ex_ms)
            except Exception:
                ex_ms = 0
            if sy and ex_ms and ex_ms < now_ms:
                recs.append((ex_ms, sy))
    recs.sort()
    exp_recent = recs[-1] if recs else None
    exp_oldest = recs[0] if recs else None

    # 2) EXPIRE ho chuke contract ki klines (asli sawal) — recent aur sabse purana (retention dekhne ko)
    if exp_recent:
        ex_ms, sy = exp_recent
        _get(f"EXPIRED klines 1h ({sy}, expiry se pehle 24h)", f"/eapi/v1/klines?symbol={sy}&interval=1h&startTime={ex_ms - DAY}&endTime={ex_ms}&limit=24")
        _get(f"EXPIRED klines 5m ({sy}, expiry se pehle 2h)", f"/eapi/v1/klines?symbol={sy}&interval=5m&startTime={ex_ms - 2 * HR}&endTime={ex_ms}&limit=24")
    if exp_oldest and exp_oldest != exp_recent:
        ex_ms, sy = exp_oldest
        _get(f"EXPIRED klines 1h sabse purana ({sy})", f"/eapi/v1/klines?symbol={sy}&interval=1h&startTime={ex_ms - DAY}&endTime={ex_ms}&limit=24")

    # 3) CHALU contract ki 24 ghante purani candles (kal ka entry wala case)
    if live_sym:
        _get(f"LIVE klines 1h ({live_sym}, 26h-22h pehle)", f"/eapi/v1/klines?symbol={live_sym}&interval=1h&startTime={now_ms - 26 * HR}&endTime={now_ms - 22 * HR}&limit=10")
        _get(f"LIVE klines 1m ({live_sym}, abhi ke 5 min)", f"/eapi/v1/klines?symbol={live_sym}&interval=1m&limit=5")
    else:
        tests.append({"name": "LIVE klines", "class": "skipped", "error": "live option symbol nahi mila"})

    verdict = []
    for t in tests:
        n = t.get("n")
        if t.get("status") == 200 and n:
            v = f"WORKS ✅ ({n} records/candles)"
        elif t.get("status") == 200:
            v = "200 OK par KHAALI ⚠️ (reply aayi, data nahi — us contract/window ki candle hi nahi bani ya history nahi rakhi)"
        elif t.get("class") == "geo_block_451" or t.get("status") == 451:
            v = "BLOCKED ❌ 451"
        else:
            v = f"❌ {t.get('class')} (HTTP {t.get('status')})"
        verdict.append(f"{t['name']}: {v}")
    return {"expired_symbols_found": len(recs), "live_symbol": live_sym, "tests": tests, "verdict": verdict}


def _oh_relay_test() -> dict:
    """Options HISTORY test — Render relay ke through (HF -> Render -> Binance).
    Backtest ke liye jo sab chahiye wo ek saath: spot, option chain list, expire hue contracts + settlement,
    option candles (premium history), abhi ka mark/bid-ask. Do demo hisaab bhi: (A) expire ho chuka ATM call,
    (B) chalu ATM call (24h pehle kharidta to aaj kitna). Sirf public market-data, koi key/order nahi."""
    base = BINANCE_EAPI_URL.rstrip("/")
    t_all = time.time()
    DAY, HR = 86400000, 3600000
    now_ms = int(t_all * 1000)
    steps, verdict, backtests = [], [], []

    def g(label, path, params=None, timeout=45):
        t0 = time.time()
        rec = {"step": label, "url": base + path + (("?" + urlencode(params)) if params else "")}
        try:
            r = requests.get(base + path, params=params, timeout=timeout)
            try:
                j = r.json()
            except Exception:
                j = None
            rec.update({"status": r.status_code, "ok": (r.status_code == 200 and j is not None),
                        "ms": int((time.time() - t0) * 1000),
                        "n": (len(j) if isinstance(j, list) else None), "head": r.text[:150]})
            steps.append(rec)
            return j if rec["ok"] else None
        except Exception as e:
            rec.update({"status": None, "ok": False, "ms": int((time.time() - t0) * 1000),
                        "error": f"{type(e).__name__}: {str(e)[:160]}"})
            steps.append(rec)
            return None

    def fv(c, key, idx):                      # option klines object bhi ho sakti hai, spot kline list bhi
        try:
            return float(c[key] if isinstance(c, dict) else c[idx])
        except Exception:
            return None

    def candle_at(cs, t_ms):                  # t_ms par ya uske baad ki pehli candle
        best = None
        for c in cs or []:
            ot = int(c.get("openTime") if isinstance(c, dict) else c[0])
            if ot >= t_ms - 1000 and (best is None or ot < best[0]):
                best = (ot, c)
        return best[1] if best else None

    def spot_at(t_ms):
        k = g(f"spot 1h candle @ {datetime.datetime.utcfromtimestamp(t_ms/1000).strftime('%d-%b %H:%M')}Z",
              "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1h", "startTime": int(t_ms), "limit": 1})
        return fv(k[0], "open", 1) if k else None

    # 0) relay jaga lo (Render free cold-start 30-60s) + chalu hai?
    g("relay /health (cold start jagana)", "/health", None, 70)
    # 1) spot index abhi
    ix = g("index BTCUSDT (abhi ka spot)", "/eapi/v1/index", {"underlying": "BTCUSDT"})
    spot_now = None
    try:
        spot_now = float(ix.get("indexPrice"))
    except Exception:
        pass
    # 2) option symbols
    ei = g("exchangeInfo (chalu option symbols)", "/eapi/v1/exchangeInfo", None, 60)
    syms = []
    try:
        syms = [x for x in ei.get("optionSymbols", []) if str(x.get("underlying", "")).upper().startswith("BTC")]
    except Exception:
        pass
    # 3) expire ho chuke contracts + settlement (pichhle 7 din)
    eh = g("exerciseHistory (7 din: expire contracts + settlement)", "/eapi/v1/exerciseHistory",
           {"underlying": "BTCUSDT", "startTime": now_ms - 7 * DAY, "endTime": now_ms, "limit": 1000})
    recs = []
    if isinstance(eh, list):
        for x in eh:
            try:
                if x.get("symbol") and int(x.get("expiryDate")) < now_ms:
                    recs.append({"symbol": x["symbol"], "strike": float(x["strikePrice"]), "settle": float(x["realStrikePrice"]),
                                 "expiry": int(x["expiryDate"]), "res": x.get("strikeResult")})
            except Exception:
                continue

    # ── (B) CHALU contract: 24h pehle ATM call kharidta to aaj ──
    try:
        tgt = now_ms - DAY
        cand = [s for s in syms if s.get("side") == "CALL" and s.get("status", "TRADING") == "TRADING"
                and now_ms + 36 * HR <= int(s.get("expiryDate", 0)) <= now_ms + 10 * DAY]
        if cand:
            ex0 = min(int(s["expiryDate"]) for s in cand)
            sp = spot_at(tgt)
            same = [s for s in cand if int(s["expiryDate"]) == ex0]
            pick = min(same, key=lambda s: abs(float(s["strikePrice"]) - (sp or spot_now or 0)))
            sy = pick["symbol"]
            ks = g(f"option klines 1h {sy} (kal se aaj)", "/eapi/v1/klines",
                   {"symbol": sy, "interval": "1h", "startTime": tgt - 2 * HR, "endTime": now_ms, "limit": 40})
            c0 = candle_at(ks, tgt)
            mk = g(f"mark {sy} (abhi)", "/eapi/v1/mark", {"symbol": sy})
            dp = g(f"depth {sy} (bid/ask abhi)", "/eapi/v1/depth", {"symbol": sy, "limit": 5})
            entry = fv(c0, "open", 1) if c0 else None
            exit_mark = None
            try:
                exit_mark = float((mk[0] if isinstance(mk, list) else mk).get("markPrice"))
            except Exception:
                pass
            bid = ask = None
            try:
                bid = float(dp["bids"][0][0]); ask = float(dp["asks"][0][0])
            except Exception:
                pass
            bt = {"type": "CHALU contract (24h pehle ATM call)", "symbol": sy, "spot_then": sp, "spot_now": spot_now,
                  "entry_premium": entry, "exit_mark_now": exit_mark, "bid_now": bid, "ask_now": ask,
                  "candles": (len(ks) if isinstance(ks, list) else 0)}
            if entry is not None and exit_mark is not None:
                bt["profit_per_1_contract_mark"] = round(exit_mark - entry, 2)
                if bid is not None:
                    bt["profit_per_1_contract_if_sold_at_bid"] = round(bid - entry, 2)
                if entry:
                    bt["pct_mark"] = round((exit_mark - entry) / entry * 100, 1)
            backtests.append(bt)
        else:
            backtests.append({"type": "CHALU contract", "error": "exchangeInfo se 36h+ door ka expiry nahi mila (ya exchangeInfo fail)"})
    except Exception as e:
        backtests.append({"type": "CHALU contract", "error": f"{type(e).__name__}: {e}"})

    # ── (A) EXPIRE ho chuka contract: expiry se 24h pehle ATM call, exit = settlement ──
    try:
        calls = [r for r in recs if r["symbol"].endswith("-C")]
        if calls:
            ex1 = max(r["expiry"] for r in calls)
            ent = ex1 - DAY
            sp = spot_at(ent)
            same = [r for r in calls if r["expiry"] == ex1]
            pick = min(same, key=lambda r: abs(r["strike"] - (sp or same[0]["settle"])))
            sy = pick["symbol"]
            ks = g(f"option klines 1h {sy} (expire ho chuka)", "/eapi/v1/klines",
                   {"symbol": sy, "interval": "1h", "startTime": ent - 2 * HR, "endTime": ex1, "limit": 40})
            c0 = candle_at(ks, ent)
            entry = fv(c0, "open", 1) if c0 else None
            settle_val = max(0.0, pick["settle"] - pick["strike"])
            bt = {"type": "EXPIRE contract (expiry se 24h pehle ATM call)", "symbol": sy, "spot_then": sp,
                  "settlement_price": pick["settle"], "strike": pick["strike"], "entry_premium": entry,
                  "exit_value_at_expiry": settle_val, "candles": (len(ks) if isinstance(ks, list) else 0)}
            if entry is not None:
                bt["profit_per_1_contract"] = round(settle_val - entry, 2)
                if entry:
                    bt["pct"] = round((settle_val - entry) / entry * 100, 1)
            backtests.append(bt)
        else:
            backtests.append({"type": "EXPIRE contract", "error": "exerciseHistory se koi expire hua call nahi mila"})
    except Exception as e:
        backtests.append({"type": "EXPIRE contract", "error": f"{type(e).__name__}: {e}"})

    # ── verdict ──
    def okn(prefix):
        r = [s for s in steps if s["step"].startswith(prefix)]
        return r[0] if r else None
    for pre, what in [("relay /health", "Render relay chal raha"), ("index", "Spot index"),
                      ("exchangeInfo", "Chalu option list"), ("exerciseHistory", "Expire contracts + settlement"),
                      ("option klines", "Option candles (premium history)"), ("mark", "Abhi ka mark price"), ("depth", "Bid/ask")]:
        r = okn(pre)
        if not r:
            verdict.append(f"{what}: — (test tak pahunche hi nahi)")
        elif r.get("ok"):
            verdict.append(f"{what}: ✅" + (f" ({r['n']} records)" if r.get("n") else ""))
        elif r.get("status") == 451:
            verdict.append(f"{what}: ❌ 451 (Render ka IP bhi Binance ne roka — region badlo, jaise Frankfurt)")
        else:
            verdict.append(f"{what}: ❌ HTTP {r.get('status')} {r.get('error') or r.get('head','')[:80]}")
    for b in backtests:
        if b.get("error"):
            verdict.append(f"Backtest [{b['type']}]: ❌ {b['error']}")
        elif "profit_per_1_contract_mark" in b or "profit_per_1_contract" in b:
            p = b.get("profit_per_1_contract_mark", b.get("profit_per_1_contract"))
            verdict.append(f"Backtest [{b['type']}] {b['symbol']}: ✅ entry {b['entry_premium']} → profit/contract {p}")
        else:
            verdict.append(f"Backtest [{b['type']}] {b.get('symbol')}: ⚠️ entry premium nahi mila (candles={b.get('candles')}) — us ghante trade hi nahi hua")
    return {"verdict": verdict, "backtests": backtests, "steps": steps, "relay": base,
            "took_s": round(time.time() - t_all, 1)}


_OH_BROWSER_HTML = r"""<div style="font-family:system-ui,sans-serif;color:#e6e9ef;background:#11151f;padding:10px;border-radius:8px">
<button id="go" style="width:100%;padding:12px;font-size:15px;border-radius:8px;border:1px solid #444;background:#fff;color:#111">▶ Phone se Render relay test (CORS check)</button>
<pre id="out" style="white-space:pre-wrap;word-break:break-all;font-size:11px;background:#0b0e15;padding:8px;border-radius:6px;max-height:220px;overflow:auto;margin:8px 0 0"></pre></div>
<script>
const B='__RELAY__',$=i=>document.getElementById(i);
async function t(n,u){const t0=performance.now();try{const r=await fetch(B+u,{cache:'no-store'});const x=await r.text();let j=null;try{j=JSON.parse(x)}catch(e){}
 return {n:n,s:r.status,ok:r.ok&&j!==null,ms:Math.round(performance.now()-t0),c:Array.isArray(j)?j.length:null,j:j};}
 catch(e){return {n:n,ok:false,err:'fetch_failed(CORS/network): '+String(e).slice(0,80),ms:Math.round(performance.now()-t0),j:null};}}
$('go').onclick=async()=>{$('go').disabled=true;const o=$('out'),L=[];const P=x=>{L.push(x);o.textContent=L.join('\n');};
 P('Chal raha hai… (Render so raha ho to 30-60s)');const now=Date.now();
 let r=await t('relay /health','/health');P((r.ok?'✅ ':'❌ ')+r.n+' '+(r.s||'')+' '+(r.err||'')+' '+r.ms+'ms');
 r=await t('index','/eapi/v1/index?underlying=BTCUSDT');P((r.ok?'✅ ':'❌ ')+r.n+' '+(r.j&&r.j.indexPrice||'')+' '+(r.s||'')+' '+(r.err||''));
 r=await t('exerciseHistory','/eapi/v1/exerciseHistory?underlying=BTCUSDT&startTime='+(now-5*864e5)+'&endTime='+now+'&limit=100');P((r.ok?'✅ ':'❌ ')+r.n+' '+(r.c!==null?r.c+' records':'')+' '+(r.s||'')+' '+(r.err||''));
 let sy=null;if(r.ok&&r.j&&r.j.length){const e=r.j[r.j.length-1];sy=e.symbol;}
 if(sy){const e=r.j[r.j.length-1],ex=+e.expiryDate;const k=await t('EXPIRED klines '+sy,'/eapi/v1/klines?symbol='+sy+'&interval=1h&startTime='+(ex-864e5)+'&endTime='+ex+'&limit=24');P((k.ok?'✅ ':'❌ ')+k.n+' '+(k.c!==null?k.c+' candles':'')+' '+(k.s||'')+' '+(k.err||''));}
 r=await t('spot klines','/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=3');P((r.ok?'✅ ':'❌ ')+r.n+' '+(r.s||'')+' '+(r.err||''));
 P('— Ho gaya. ✅ = phone se relay CORS ke saath chal raha. —');$('go').disabled=false;};
</script>"""


def _run_binance_block_diagnostic() -> dict:
    """Poora test chalata hai, JSON-able dict return karta hai (summary sabse upar)."""
    import platform
    t_start = time.time()
    now_ms = int(t_start * 1000)
    UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"}

    # ── REST specs: (group, name, url, headers, head_chars) ──
    R = []
    def add(g, n, u, h=None, head=300):
        R.append((g, n, u, h, head))
    for n, p in [("ping", "/api/v3/ping"), ("time", "/api/v3/time"),
                 ("ticker_price", "/api/v3/ticker/price?symbol=BTCUSDT"),
                 ("klines_1m", "/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=2"),
                 ("depth5", "/api/v3/depth?symbol=BTCUSDT&limit=5"),
                 ("exchangeInfo_btcusdt", "/api/v3/exchangeInfo?symbol=BTCUSDT")]:
        add("spot", n, "https://api.binance.com" + p)
    add("spot", "ping_browser_UA", "https://api.binance.com/api/v3/ping", UA)
    for h in ("api-gcp", "api1", "api2", "api3", "api4"):
        add("spot_alt_hosts", f"{h}.binance.com ping", f"https://{h}.binance.com/api/v3/ping")
    add("spot_vision", "data-api.binance.vision ping", "https://data-api.binance.vision/api/v3/ping")
    add("spot_vision", "data-api.binance.vision ticker", "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT")
    add("spot_vision", "data-api.binance.vision time", "https://data-api.binance.vision/api/v3/time")
    add("spot_vision", "data-api.binance.vision klines_1m (candles history)", "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=2")
    add("spot_vision", "data-api.binance.vision depth5 (REST)", "https://data-api.binance.vision/api/v3/depth?symbol=BTCUSDT&limit=5")
    for n, p in [("ping", "/fapi/v1/ping"), ("time", "/fapi/v1/time"),
                 ("ticker_price", "/fapi/v1/ticker/price?symbol=BTCUSDT"),
                 ("premiumIndex", "/fapi/v1/premiumIndex?symbol=BTCUSDT"),
                 ("depth5", "/fapi/v1/depth?symbol=BTCUSDT&limit=5"),
                 ("exchangeInfo", "/fapi/v1/exchangeInfo")]:
        add("futures_usdm", n, "https://fapi.binance.com" + p)
    for n, p in [("ping", "/dapi/v1/ping"), ("time", "/dapi/v1/time")]:
        add("futures_coinm", n, "https://dapi.binance.com" + p)
    for n, p in [("ping", "/eapi/v1/ping"), ("time", "/eapi/v1/time"),
                 ("index_btcusdt", "/eapi/v1/index?underlying=BTCUSDT"),
                 ("mark_all", "/eapi/v1/mark"), ("ticker_all", "/eapi/v1/ticker"),
                 ("exchangeInfo", "/eapi/v1/exchangeInfo")]:
        add("options", n, "https://eapi.binance.com" + p)
    add("website", "www.binance.com/en", "https://www.binance.com/en")
    add("binance_us_control", "api.binance.us ping", "https://api.binance.us/api/v3/ping")
    add("control_general", "google generate_204", "https://www.google.com/generate_204")
    add("control_general", "cloudflare trace", "https://cloudflare.com/cdn-cgi/trace", None, 700)
    add("control_other_exchanges", "bybit time", "https://api.bybit.com/v5/market/time")
    add("control_other_exchanges", "okx time", "https://www.okx.com/api/v5/public/time")
    add("control_other_exchanges", "coinbase time", "https://api.coinbase.com/v2/time")
    add("control_other_exchanges", "kraken time", "https://api.kraken.com/0/public/Time")

    # ── WS specs (symbol ki zaroorat nahi wale): (group, name, url, send) ──
    W = [
        ("spot_ws", "stream.binance.com:9443 btcusdt@trade", "wss://stream.binance.com:9443/ws/btcusdt@trade", None),
        ("spot_ws", "stream.binance.com:443 btcusdt@trade", "wss://stream.binance.com/ws/btcusdt@trade", None),
        ("spot_ws", "data-stream.binance.com btcusdt@kline_1m", "wss://data-stream.binance.com/ws/btcusdt@kline_1m", None),
        ("spot_ws", "data-stream.binance.vision btcusdt@trade", "wss://data-stream.binance.vision/ws/btcusdt@trade", None),
        ("spot_ws", "data-stream.binance.vision btcusdt@kline_1m", "wss://data-stream.binance.vision/ws/btcusdt@kline_1m", None),
        ("spot_ws", "data-stream.binance.com btcusdt@trade", "wss://data-stream.binance.com/ws/btcusdt@trade", None),
        ("spot_depth_ws", "data-stream.binance.com btcusdt@depth20@100ms (spot depth)", "wss://data-stream.binance.com/ws/btcusdt@depth20@100ms", None),
        ("spot_depth_ws", "data-stream.binance.vision btcusdt@depth20@100ms (spot depth)", "wss://data-stream.binance.vision/ws/btcusdt@depth20@100ms", None),
        ("spot_depth_ws", "data-stream.binance.com btcusdt@bookTicker (best bid/ask)", "wss://data-stream.binance.com/ws/btcusdt@bookTicker", None),
        ("spot_ws", "combined stream kline+trade", "wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/btcusdt@trade", None),
        ("futures_ws", "fstream btcusdt@markPrice (control: legacy /ws path, ab market data nahi deta)", "wss://fstream.binance.com/ws/btcusdt@markPrice", None),
        ("futures_ws", "market/ws btcusdt@markPrice (naya /market path)", "wss://fstream.binance.com/market/ws/btcusdt@markPrice", None),
        ("futures_ws", "legacy /ws btcusdt@depth (control: legacy me sirf public data chalta)", "wss://fstream.binance.com/ws/btcusdt@depth5@500ms", None),
        ("futures_ws", "fstream/market btcusdt@optionMarkPrice (options mark, chart.html wala)", "wss://fstream.binance.com/market/ws/btcusdt@optionMarkPrice", None),
        ("futures_ws", "dstream btcusd_perp@markPrice (COIN-M)", "wss://dstream.binance.com/ws/btcusd_perp@markPrice", None),
        ("options_ws_legacy", "nbstream WRONG-symbol control (handshake reaction dekhne ko)", "wss://nbstream.binance.com/eoptions/ws/btcusdt@depth@100ms", None),
        ("control_other_exchanges_ws", "bybit spot public trade", "wss://stream.bybit.com/v5/public/spot",
         {"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}),
        ("control_other_exchanges_ws", "okx tickers", "wss://ws.okx.com:8443/ws/v5/public",
         {"op": "subscribe", "args": [{"channel": "tickers", "instId": "BTC-USDT"}]}),
        ("control_other_exchanges_ws", "coinbase ticker", "wss://ws-feed.exchange.coinbase.com",
         {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}),
        ("control_general_ws", "postman echo", "wss://ws.postman-echo.com/raw", "ping"),
    ]
    layer_hosts = [
        ("api.binance.com", 443), ("api-gcp.binance.com", 443), ("api1.binance.com", 443),
        ("api2.binance.com", 443), ("api3.binance.com", 443), ("api4.binance.com", 443),
        ("data-api.binance.vision", 443), ("fapi.binance.com", 443), ("dapi.binance.com", 443),
        ("eapi.binance.com", 443), ("www.binance.com", 443),
        ("stream.binance.com", 443), ("stream.binance.com", 9443),
        ("data-stream.binance.com", 443), ("data-stream.binance.vision", 443),
        ("fstream.binance.com", 443), ("dstream.binance.com", 443), ("nbstream.binance.com", 443),
        ("api.binance.us", 443), ("api.bybit.com", 443), ("www.google.com", 443),
    ]

    rest_res, ws_res, layers = {}, {}, {}
    server, sym_info = {}, {}
    with ThreadPoolExecutor(max_workers=70) as ex:
        f_server = ex.submit(_bbd_server_info)
        f_sym = ex.submit(_bbd_pick_option_symbol, now_ms)
        f_layers = {f"{h}:{p}": ex.submit(_bbd_host_layers, h, p) for h, p in layer_hosts}
        f_rest = [(g, n, u, ex.submit(_bbd_rest, u, 10, h, hd)) for g, n, u, h, hd in R]
        f_ws = [(g, n, u, ex.submit(_bbd_ws, u, 8, s)) for g, n, u, s in W]
        # ── Trading readiness probes (FAKE key, koi real order/account nahi) ──
        _FK = {"X-MBX-APIKEY": "diag_fake_key_not_real_0000000000000000000000000000000000000000"}
        _ts = int(time.time() * 1000)
        TR = [
            ("options eapi GET account (options trading host)", "GET", f"https://eapi.binance.com/eapi/v1/account?timestamp={_ts}&signature=00"),
            ("options eapi POST order (order place)", "POST", f"https://eapi.binance.com/eapi/v1/order?timestamp={_ts}&signature=00"),
            ("options eapi POST listenKey (user-data stream)", "POST", "https://eapi.binance.com/eapi/v1/listenKey"),
            ("usdm fapi GET account", "GET", f"https://fapi.binance.com/fapi/v2/account?timestamp={_ts}&signature=00"),
            ("coinm dapi GET account", "GET", f"https://dapi.binance.com/dapi/v1/account?timestamp={_ts}&signature=00"),
            ("spot api GET account", "GET", f"https://api.binance.com/api/v3/account?timestamp={_ts}&signature=00"),
            ("spot data-api.binance.vision GET account (market-data host)", "GET", f"https://data-api.binance.vision/api/v3/account?timestamp={_ts}&signature=00"),
            ("probe: ws-eapi.binance.com host (exist/redirect?)", "GET", "https://ws-eapi.binance.com/"),
            ("spot data-api.binance.vision POST order/test", "POST", f"https://data-api.binance.vision/api/v3/order/test?timestamp={_ts}&signature=00"),
        ]
        f_tr = [("trading_probe_rest", n, u, ex.submit(_bbd_rest, u, 10, _FK, 300, m)) for n, m, u in TR]
        _tm = {"id": "diag", "method": "time"}
        TW = [
            ("WS-API usdm ws-fapi (orders WS pe)", "wss://ws-fapi.binance.com/ws-fapi/v1", _tm),
            ("WS-API coinm ws-dapi", "wss://ws-dapi.binance.com/ws-dapi/v1", _tm),
            ("WS-API spot ws-api", "wss://ws-api.binance.com:443/ws-api/v3", _tm),
            ("private user-data fstream (fake listenKey, handshake reaction)", "wss://fstream.binance.com/private/ws?listenKey=diagfakekey&events=ORDER_TRADE_UPDATE", None),
        ]
        f_trw = [("trading_ws", n, u, ex.submit(_bbd_ws, u, 8, snd, None, 6, 1)) for n, u, snd in TW]
        f_rest = f_rest + f_tr
        f_ws = f_ws + f_trw
        try:
            server = f_server.result(timeout=30)
        except Exception as e:
            server = {"error": f"{type(e).__name__}: {e}"}
        try:
            sym_info = f_sym.result(timeout=40)
        except Exception as e:
            sym_info = {"symbol": None, "errors": [f"{type(e).__name__}: {e}"]}
        sym = sym_info.get("symbol")

        # ── Phase 2: live option symbol (WS se mila) chahiye wale tests ──
        f_rest2, f_ws2 = [], []
        if sym:
            sl = sym.lower()
            code = sym_info.get("expiry_code") or sym.split("-")[1]
            for n, p in [("depth5", f"/eapi/v1/depth?symbol={sym}&limit=5"),
                         ("mark_symbol", f"/eapi/v1/mark?symbol={sym}"),
                         ("ticker_symbol", f"/eapi/v1/ticker?symbol={sym}"),
                         ("klines_1m", f"/eapi/v1/klines?symbol={sym}&interval=1m&limit=2")]:
                u = "https://eapi.binance.com" + p
                f_rest2.append(("options", n + " (live symbol)", u, ex.submit(_bbd_rest, u, 10, None, 300)))
            PUB, MKT = "wss://fstream.binance.com/public/ws", "wss://fstream.binance.com/market/ws"
            OPT = [  # (name, url, send, min_msgs)
                (f"public/ws {sl}@depth10@500ms (chart.html wala)", f"{PUB}/{sl}@depth10@500ms", None, 1),
                (f"public/ws {sl}@depth10@100ms", f"{PUB}/{sl}@depth10@100ms", None, 1),
                (f"public/ws {sl}@depth5@500ms", f"{PUB}/{sl}@depth5@500ms", None, 1),
                (f"public/ws {sl}@depth20@500ms", f"{PUB}/{sl}@depth20@500ms", None, 1),
                (f"public/ws {sl}@depth@100ms (diff)", f"{PUB}/{sl}@depth@100ms", None, 1),
                (f"public/stream combined {sl}@depth10@500ms", f"wss://fstream.binance.com/public/stream?streams={sl}@depth10@500ms", None, 1),
                (f"public/ws + SUBSCRIBE {sl}@depth10@500ms", PUB,
                 {"method": "SUBSCRIBE", "params": [f"{sl}@depth10@500ms"], "id": 1}, 2),
                (f"market/ws {sl}@depth10@500ms (control: depth /public pe hota hai)", f"{MKT}/{sl}@depth10@500ms", None, 1),
                (f"public/ws {sl}@bookTicker", f"{PUB}/{sl}@bookTicker", None, 1),
                (f"public/ws {sl}@ticker (probe)", f"{PUB}/{sl}@ticker", None, 1),
                (f"public/ws btcusdt@optionTicker@{code} (chart.html wala)", f"{PUB}/btcusdt@optionTicker@{code}", None, 1),
                (f"market/ws btcusdt@optionTicker@{code} (naya /market path)", f"{MKT}/btcusdt@optionTicker@{code}", None, 1),
                (f"market/ws + SUBSCRIBE btcusdt@optionTicker@{code}", MKT,
                 {"method": "SUBSCRIBE", "params": [f"btcusdt@optionTicker@{code}"], "id": 2}, 2),
                (f"public/ws + SUBSCRIBE btcusdt@optionTicker@{code}", PUB,
                 {"method": "SUBSCRIBE", "params": [f"btcusdt@optionTicker@{code}"], "id": 3}, 2),
                (f"public/ws btcusdt@optionOpenInterest@{code} (probe)", f"{PUB}/btcusdt@optionOpenInterest@{code}", None, 1),
                (f"market/ws btcusdt@optionMarkPrice (options chain)", f"{MKT}/btcusdt@optionMarkPrice", None, 1),
            ]
            for n, u, snd, mm in OPT:
                f_ws2.append(("options_ws", n, u, ex.submit(_bbd_ws, u, 8, snd, None, 10, mm)))
            for n, u, snd in [("SOAK 15s spot kline_1m (data-stream.binance.com)", "wss://data-stream.binance.com/ws/btcusdt@kline_1m", None),
                              ("SOAK 15s spot depth20@100ms", "wss://data-stream.binance.com/ws/btcusdt@depth20@100ms", None),
                              ("SOAK 15s optionMarkPrice (chain)", f"{MKT}/btcusdt@optionMarkPrice", None),
                              (f"SOAK 15s option depth10@500ms {sym}", f"{PUB}/{sl}@depth10@500ms", None)]:
                f_ws2.append(("soak", n, u, ex.submit(_bbd_ws_soak, u, 15, snd)))
            for n, u in [(f"nbstream {sym}@depth@100ms (purana/legacy)", f"wss://nbstream.binance.com/eoptions/ws/{sym}@depth@100ms"),
                         (f"nbstream {sym}@depth@500ms (purana/legacy)", f"wss://nbstream.binance.com/eoptions/ws/{sym}@depth@500ms")]:
                f_ws2.append(("options_ws_legacy", n, u, ex.submit(_bbd_ws, u, 8, None)))
        for g, n, u, f in f_rest + f_rest2:
            try:
                r = f.result(timeout=60)
            except Exception as e:
                r = {"class": "diag_exception", "error": f"{type(e).__name__}: {e}"}
            r = {"name": n, "url": u, **r}
            rest_res.setdefault(g, []).append(r)
        for g, n, u, f in f_ws + f_ws2:
            try:
                r = f.result(timeout=60)
            except Exception as e:
                r = {"class": "diag_exception", "error": f"{type(e).__name__}: {e}"}
            r = {"name": n, "url": u, **r}
            ws_res.setdefault(g, []).append(r)
        for k, f in f_layers.items():
            try:
                layers[k] = f.result(timeout=40)
            except Exception as e:
                layers[k] = {"error": f"{type(e).__name__}: {e}"}

    # ── Options HISTORY test (klines expired + live, exerciseHistory) ──
    try:
        opt_hist = _bbd_options_history_probe(now_ms, sym)
    except Exception as _e_oh:
        opt_hist = {"verdict": [f"options history probe crash: {type(_e_oh).__name__}: {_e_oh}"], "tests": []}

    # ── Summary / verdict ──
    B_REST = ["spot", "spot_alt_hosts", "spot_vision", "futures_usdm", "futures_coinm", "options", "website"]
    B_WS = ["spot_ws", "spot_depth_ws", "futures_ws", "options_ws"]   # legacy nbstream verdict count me nahi (deprecated)
    b_rest_items = [i for g in B_REST for i in rest_res.get(g, [])]
    b_ws_items = [i for g in B_WS for i in ws_res.get(g, []) if "WRONG-symbol" not in i["name"] and "(control" not in i["name"] and "(probe" not in i["name"]]
    ctrl_gen = rest_res.get("control_general", [])
    ctrl_oth = rest_res.get("control_other_exchanges", [])
    ctrl_ws = ws_res.get("control_other_exchanges_ws", []) + ws_res.get("control_general_ws", [])
    c_b, c_g, c_o = _bbd_count(b_rest_items), _bbd_count(ctrl_gen), _bbd_count(ctrl_oth)
    ws_live = sum(1 for i in b_ws_items if i.get("class") == "ws_live")
    ws_open = sum(1 for i in b_ws_items if i.get("class") in ("ws_live", "ws_open_no_data"))
    ctrl_ws_live = sum(1 for i in ctrl_ws if i.get("class") in ("ws_live", "ws_open_no_data"))
    by_all = c_b["by_class"]
    lines, cause = [], "UNKNOWN"
    sc = (server or {}).get("country")
    lines.append(f"Server public IP: {(server or {}).get('ip', '?')} | country={sc or '?'} | org={(server or {}).get('org', '?')}")
    if c_g["total"] and c_g["reachable"] == 0:
        cause = "SERVER_OUTBOUND_BROKEN"
        if any(i.get("class") == "egress_proxy_block" for i in ctrl_gen + b_rest_items):
            cause = "EGRESS_PROXY_ALLOWLIST"
            lines.append("Response me 'host not in allowlist' aaya — server ke beech ka egress proxy/firewall sirf allow-listed hosts jaane deta hai (Binance/Google dono roke ja rahe). Ye Binance ka block nahi, hosting ki network policy hai.")
        lines.append("Google/Cloudflare tak bhi nahi pahunch raha — problem Binance-specific nahi, server ka poora outbound internet limited/toota hua hai.")
    elif c_b["reachable"] == 0:
        if by_all.get("geo_block_451"):
            cause = "GEO_BLOCK_451"
            lines.append(f"Binance ne HTTP 451 (restricted location) diya — REGION/IP based block. Server country={sc}. Ye network fault nahi, Binance ka apna legal block hai.")
        elif any(k.startswith("cdn_block") or k == "forbidden_403" for k in by_all):
            cause = "CDN_WAF_IP_BLOCK"
            lines.append("Binance ke CDN/WAF ne 403 diya — datacenter/ASN/IP ko block kiya ja raha hai (server ne request receive ki aur reject ki).")
        elif by_all.get("ip_ban_418") or by_all.get("rate_limit_429"):
            cause = "IP_BAN_OR_RATE_LIMIT"
            lines.append("HTTP 418/429 — IP ban ya rate-limit (asli block nahi, request-volume ki wajah se temporary ban). retry-after header dekho.")
        else:
            dom = max(by_all, key=by_all.get) if by_all else "unknown"
            cause = "NETWORK_LEVEL_BLOCK"
            lines.append(f"Binance ke saare REST endpoints network-level par fail (dominant={dom}) jabki controls ok hain — DNS/TCP/timeout layer par block (HF egress se Binance IPs tak route nahi). 'dns_tcp_tls' section me exact layer dekho.")
    elif c_b["reachable"] < c_b["total"]:
        cause = "GEO_BLOCK_451_PARTIAL" if by_all.get("geo_block_451") else "PARTIAL_BLOCK"
        good = [f"{i['name']}" for g in B_REST for i in rest_res.get(g, []) if i.get("class") == "ok"]
        bad_hosts = sorted({i["url"].split("/")[2] for g in B_REST for i in rest_res.get(g, [])
                            if i.get("class") in _BBD_BLOCKED})
        if cause == "GEO_BLOCK_451_PARTIAL":
            lines.append(f"Binance REST par HTTP 451 (restricted location) — server country={sc}. Ye REGION/IP block hai, network fault nahi (DNS/TCP/TLS sab ok). Blocked hosts: {bad_hosts}")
        else:
            lines.append(f"Kuch Binance endpoints chal rahe, kuch nahi ({c_b['reachable']}/{c_b['total']} reachable). Blocked hosts: {bad_hosts}")
        lines.append(f"Jo REST bina block ke chal raha: {good}")
    else:
        cause = "REST_NOT_BLOCKED"
        lines.append(f"Binance REST poora reachable ({c_b['reachable']}/{c_b['total']}) — REST par block NAHI hai.")
    if b_ws_items:
        if ws_open == 0:
            lines.append("Binance WebSockets me se ek bhi open nahi hua" + (" jabki other-exchange WS open hue — Binance WS specific block/handshake reject." if ctrl_ws_live else " (control WS bhi fail — server se WS hi limited ho sakta hai)."))
            if cause in ("REST_NOT_BLOCKED",):
                cause = "WS_ONLY_BLOCK"
        elif ws_live < len(b_ws_items):
            _live = [i["name"] for i in b_ws_items if i.get("class") == "ws_live"]
            _bad = [f"{i['name'][:55]}={i.get('class')}" for i in b_ws_items if i.get("class") != "ws_live"]
            lines.append(f"Binance WS: {ws_live}/{len(b_ws_items)} live. LIVE: {_live}")
            lines.append(f"Binance WS fail/no-data: {_bad}")
        else:
            lines.append(f"Binance WS poore chal rahe ({ws_live}/{len(b_ws_items)} live).")
    if cause in ("REST_NOT_BLOCKED",) and (not b_ws_items or ws_live == len(b_ws_items)):
        cause = "NOT_BLOCKED"
        lines.append("Server se Binance REST + WS dono chal rahe — HF par block NAHI hai; problem app logic / ban / thread level par dhoondo.")
    lines.append(f"Binance direct: {c_b['reachable']}/{c_b['total']} reachable.")
    if c_o["total"]:
        lines.append(f"Other exchanges REST (bybit/okx/coinbase/kraken): {c_o['reachable']}/{c_o['total']} reachable.")
    if not sym:
        lines.append(f"Live option symbol nahi mila (options depth/WS tests skip): {sym_info.get('errors')}")
    retry = [i.get("headers", {}).get("retry-after") for i in b_rest_items if i.get("headers", {}).get("retry-after")]
    if retry:
        lines.append(f"retry-after headers mile: {retry[:3]}")

    # ═══ Tere 3 kaam ke liye seedha jawab: kya chal raha, konsa URL, fail to EXACT wajah ═══
    def _reason(it):
        c = it.get("class", "?")
        if c in ("geo_block_451", "ws_handshake_http_451"):
            return f"HTTP 451 — Binance ka geo/IP block (server country={sc}). Asli block yahi hai."
        if c in ("ws_handshake_http_404", "reachable_bad_request"):
            return "404/400 — URL, path ya stream-name galat/deprecated. Ye BLOCK nahi, URL ki galti hai."
        if c.startswith("ws_handshake_http_403") or c.startswith("cdn_block") or c == "forbidden_403":
            return "403 — CDN/WAF ne IP/datacenter reject kiya (block)."
        if c in ("ws_handshake_http_418", "ws_handshake_http_429", "ip_ban_418", "rate_limit_429"):
            return "418/429 — IP ban ya rate limit (temporary)."
        if c == "ws_open_no_data" and "fstream.binance.com/ws/" in (it.get("url") or "") and "@depth" not in (it.get("url") or ""):
            return ("Handshake pass par data nahi — Binance ne 2026-03-06 ko fstream WS /public /market /private me split kiya, purana /ws path "
                    "23-Apr-2026 ke baad sirf public (depth/bookTicker) data deta hai; market streams ab /market/ws pe.")
        if c == "ws_open_no_data" and "/market/ws/" in (it.get("url") or "") and "@depth" in (it.get("url") or ""):
            return "Handshake pass par data nahi — depth/bookTicker /public path pe hote hain, /market pe nahi (Binance split)."
        if c == "ws_open_no_data":
            return ("Handshake PASS hua (matlab geo-block NAHI) par 10s me data nahi aaya — stream ka naam/format ya symbol quiet hai "
                    "(recv_error: %s)" % (it.get("recv_error") or "-"))
        if c == "cdn_challenge_202":
            return "202 — CDN challenge page (asli data nahi)."
        if c in ("timeout", "tcp_refused", "tcp_reset", "no_route", "dns_fail", "tls_fail", "conn_closed"):
            return f"Network-level: {c} — {it.get('error','')[:120]}"
        return f"class={c} {it.get('error','') or ''}"[:200]

    def _need(items, pred):
        cand = [i for i in items if pred(i)]
        live = [i for i in cand if i.get("class") in ("ws_live", "ok")]
        if live:
            return {"status": "WORKS ✅", "use_this": live[0].get("url"),
                    "also_working": [i["url"] for i in live[1:4]]}
        return {"status": "NOT WORKING ❌",
                "tried": [{"url": i.get("url"), "class": i.get("class"), "exact_reason": _reason(i)} for i in cand[:8]] or "koi test nahi chala"}

    _ws = lambda g: ws_res.get(g, [])
    _rs = lambda g: rest_res.get(g, [])
    needs = {
        "1_spot_live_price_WS": _need(_ws("spot_ws"), lambda i: ("kline" in i["name"] or "@trade" in i["name"]) and "combined" not in i["name"]),
        "2_spot_candle_history_REST": _need(_rs("spot_vision") + _rs("spot"), lambda i: "klines" in i["name"]),
        "3_spot_depth_WS": _need(_ws("spot_depth_ws"), lambda i: "depth" in i["name"]),
        "4_option_chain_WS": _need(_ws("futures_ws"), lambda i: "optionMarkPrice" in i["name"]),
        "5_option_depth_WS": _need(_ws("options_ws"), lambda i: "@depth" in i["name"]),
        "6_option_ticker_WS": _need(_ws("options_ws"), lambda i: "optionTicker" in i["name"]),
    }
    if sym_info.get("symbol"):
        needs["4_option_chain_WS"]["chain_info"] = {
            "symbols_in_one_message": sym_info.get("symbols_in_stream"), "expiries": sym_info.get("expiries_seen"),
            "index_price": sym_info.get("index_price"), "test_symbol": sym_info.get("symbol"), "picked_via": sym_info.get("source")}
    _blk_hosts = sorted({i["url"].split("/")[2] for g in list(rest_res) + list(ws_res) for i in
                         (rest_res.get(g, []) + ws_res.get(g, []))
                         if i.get("class") in ("geo_block_451", "ws_handshake_http_451") and i.get("url")})
    _ok_hosts = sorted({i["url"].split("/")[2] for g in list(rest_res) + list(ws_res) for i in
                        (rest_res.get(g, []) + ws_res.get(g, []))
                        if i.get("class") in ("ok", "ws_live") and i.get("url") and "binance" in i["url"]})
    if _ok_hosts:
        block_scope = "PER-HOST (poora Binance block NAHI) — kuch hosts 451, kuch chal rahe"
        lines.append(f"Binance ke {len(_ok_hosts)} host chal rahe hain, isliye poora block nahi hai. 451 wale hosts: {_blk_hosts}. Chalne wale: {_ok_hosts}")
    elif _blk_hosts:
        block_scope = "FULL (koi Binance host nahi chala)"
    else:
        block_scope = "NO_BINANCE_HOST_WORKED (block-type unclear, 'rest'/'ws' section dekho)"
    _fails = [k for k, v in needs.items() if str(v.get("status", "")).startswith("NOT")]
    if not _fails:
        lines.append("Tere saare kaam (spot live, candles, spot depth, option chain, option depth) HF server se WORK kar rahe hain — "
                     "to server-side block nahi hai; gadbad app ke URLs/code me ya browser-side hai (neeche Browser Test chalao).")
    else:
        lines.append(f"HF server se ye kaam NAHI chal rahe: {_fails} — har ek ki exact wajah 'needs' me hai.")
    lines.append("NOTE: ye test HF SERVER se hai. Browser seedha Binance se jude to phone ka network alag hai — uske liye niche 'Browser Test' chalao.")

    # ── Server clock vs Binance time (signed trading ke liye zaroori) ──
    clock = {}
    try:
        for it in rest_res.get("spot_vision", []):
            if "time" in it["name"] and it.get("class") == "ok":
                import re as _re
                mt = _re.search(r'"serverTime"\s*:\s*(\d+)', it.get("body_head", ""))
                if mt:
                    skew = int(it.get("t_recv_ms", 0)) - int(mt.group(1)) - int(it.get("ms", 0) / 2)
                    clock = {"skew_ms_approx": skew, "ok_for_signed_orders": abs(skew) < 900,
                             "note": "response aane ke waqt ka local clock vs Binance serverTime (rtt/2 minus). Signed orders ke liye <1s chahiye."}
                break
    except Exception as e:
        clock = {"error": f"{type(e).__name__}: {e}"}

    # ── Trading readiness (HF server se REAL orders ho sakte hain ya nahi) ──
    def _tr_row(it):
        c = it.get("class", "?")
        if c == "reachable_bad_request" and it.get("status") == 404:
            v = "404 — ye host ye trading endpoint deta hi nahi (market-data-only host), isse trading nahi ho sakti"
        elif c == "redirect":
            v = "REDIRECT — Location header dekho (host exist karta hai)"
        elif c in ("reachable_auth_required", "reachable_bad_request", "ok"):
            v = "REACHABLE ✅ (auth layer tak pahunch gaya — real key se chalega)"
        elif c == "geo_block_451":
            v = "BLOCKED ❌ 451 — real order/account/listenKey isi wajah se HF se nahi ja sakta"
        elif c in ("ws_live", "ws_open_no_data"):
            v = "HANDSHAKE OK ✅"
        elif c.startswith("ws_handshake_http_"):
            v = "REJECTED ❌ " + c.replace("ws_handshake_http_", "HTTP ")
        else:
            v = "❌ " + c
        return {"url": it.get("url", "")[:110], "status": it.get("status"), "class": c, "verdict": v,
                "body_head": (it.get("body_head") or it.get("first_msg_head") or it.get("error") or "")[:110]}
    trading = {"rest": {i["name"]: _tr_row(i) for i in rest_res.get("trading_probe_rest", [])},
               "ws_api": {i["name"]: _tr_row(i) for i in ws_res.get("trading_ws", [])},
               "note": "Sab probes FAKE key se hain — koi real order/account access nahi hua. 401/-2014/-1022 = host tak pahunch gaye (achhi baat); 451 = block."}
    _eapi = [i for i in rest_res.get("trading_probe_rest", []) if "eapi" in i["url"]]
    _eapi_ok = bool(_eapi) and all(i.get("class") in ("reachable_auth_required", "reachable_bad_request", "ok") and i.get("status") != 404 for i in _eapi)
    trading["options_real_trading_from_HF"] = ("POSSIBLE ✅ (eapi auth tak pahunch raha)" if _eapi_ok else
        "NOT POSSIBLE abhi ❌ — eapi (order/account/listenKey) HF server se 451; Binance docs me options order sirf REST eapi se documented hai (options ka WS-API documented nahi mila)")
    trading["ip_warning"] = ("HF Space ka outbound IP har restart pe badalta hai (pehle runs me alag alag AWS IP mile) — isliye Binance API key me "
                             "IP-whitelist HF ke liye lag nahi sakta; key sirf Trade permission (Withdraw OFF) ke saath rakho.")
    lines.append("Trading probes (fake key): " + "; ".join(f"{k.split(' (')[0]}={v['class']}" for k, v in list(trading['rest'].items())[:8]))
    lines.append(f"Options real trading HF se: {trading['options_real_trading_from_HF']}")
    if clock:
        lines.append(f"Server clock skew vs Binance ~{clock.get('skew_ms_approx')} ms")

    # ── Soak (15s feed stability) ──
    soak = {i["name"]: {k: i.get(k) for k in ("class", "messages", "msgs_per_sec", "max_gap_s", "first_msg_ms", "closed_early_at_s", "error")}
            for i in ws_res.get("soak", [])}

    # ── App ke apne source ka URL audit (screenshot ki jagah) ──
    try:
        audit = _bbd_app_url_audit(rest_res, ws_res)
    except Exception as e:
        audit = {"error": f"{type(e).__name__}: {e}"}
    if audit.get("problem_hosts_used_in_code"):
        lines.append(f"App ke code me abhi ye problem hosts use ho rahe (451 ya Render): {audit['problem_hosts_used_in_code']}")

    # ── App ki apni recent log (warn/err + Binance wali) ──
    app_log = []
    try:
        for _l in _startup_log_snapshot()[-400:]:
            _m = str(_l.get("msg", ""))
            if _l.get("level") in ("warn", "err") or any(k in _m for k in ("inance", "BN", "option", "WS ", "depth", "451", "relay")):
                app_log.append({"t": _l.get("t"), "level": _l.get("level"), "msg": _m[:300]})
        app_log = app_log[-40:]
    except Exception as e:
        app_log = [{"error": f"{type(e).__name__}: {e}"}]

    app_state = {}
    try:
        app_state["binance_ban_until_ms"] = _BINANCE_BAN_UNTIL_MS
        app_state["binance_ban_active_now"] = _BINANCE_BAN_UNTIL_MS > now_ms
        app_state["last_ban_event"] = dict(_BN_BAN_EVENT)
    except Exception as e:
        app_state["ban_state_error"] = f"{type(e).__name__}: {e}"

    app_state["recent_app_log"] = app_log
    env = {
        "python": platform.python_version(), "platform": platform.platform(),
        "requests": getattr(requests, "__version__", "?"),
        "websocket_client": getattr(websocket, "__version__", None) if websocket else None,
        "hf_space_id": os.environ.get("SPACE_ID"), "hf_space_host": os.environ.get("SPACE_HOST"),
        "proxy_env": {k: os.environ[k][:80] for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                                       "http_proxy", "https_proxy", "all_proxy", "no_proxy") if os.environ.get(k)},
    }
    try:
        with open("/etc/resolv.conf") as _rf:
            env["resolv_conf_nameservers"] = [l.split()[1] for l in _rf if l.startswith("nameserver") and len(l.split()) > 1][:4]
    except Exception:
        pass

    return {
        "summary": {
            "likely_cause": cause,
            "block_scope": block_scope,
            "needs": needs,
            "trading_readiness": trading,
            "feed_stability_15s": soak,
            "server_clock": clock,
            "app_urls_summary": {"problem_hosts_used_in_code": audit.get("problem_hosts_used_in_code"),
                                 "render_variable_uses": {k: v.get("count") for k, v in (audit.get("render_dependency_via_variables") or {}).items()},
                                 "by_host": audit.get("by_host")},
            "verdict": lines,
            "binance_rest": c_b,
            "binance_ws": {"total": len(b_ws_items), "live": ws_live, "opened": ws_open},
            "control_general_rest": c_g, "control_other_exchanges_rest": c_o,
            "control_other_exchanges_ws_opened": ctrl_ws_live,
            "option_symbol_used": sym_info,
            "options_history_server": opt_hist.get("verdict"),
        },
        "app_url_audit": audit, "server": server, "env": env, "app_state": app_state,
        "dns_tcp_tls": layers, "rest": rest_res, "ws": ws_res, "options_history": opt_hist,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "took_s": round(time.time() - t_start, 1),
    }


_BBD_BROWSER_HTML = r"""<div style="font-family:system-ui,sans-serif;color:#e6e9ef;background:#11151f;padding:10px;border-radius:8px">
<button id="go" style="width:100%;padding:12px;font-size:15px;border-radius:8px;border:1px solid #444;background:#fff;color:#111">▶ Browser Test Chalao (phone se)</button>
<div id="st" style="font-size:12px;color:#9aa4b8;margin:6px 0">Ye test tere PHONE/BROWSER se seedha Binance ko hit karta hai (~20s).</div>
<button id="cp" style="display:none;width:100%;padding:8px;margin-bottom:6px;border-radius:8px;border:1px solid #444;background:#222;color:#fff">📋 Result copy karo</button>
<pre id="out" style="white-space:pre-wrap;word-break:break-all;font-size:11px;background:#0b0e15;padding:8px;border-radius:6px;max-height:380px;overflow:auto;margin:0"></pre>
</div>
<script>
const R={ua:navigator.userAgent,online:navigator.onLine,tests:[]};
const $=id=>document.getElementById(id);
function show(){$('out').textContent=JSON.stringify(R,null,1);}
function wsTest(name,url,o){o=o||{};const min=o.min||1,wait=o.wait||9000;
 return new Promise(res=>{const t0=performance.now();let n=0,first=null,opened=false,done=false,ws=null;
  const fin=(cls,x)=>{if(done)return;done=true;try{ws&&ws.close()}catch(e){}
   const r=Object.assign({name:name,url:url,class:cls,ms:Math.round(performance.now()-t0),msgs:n},x||{});
   if(first&&!o.keep)r.first_head=first.slice(0,120);R.tests.push(r);show();res({r:r,first:first});};
  try{ws=new WebSocket(url)}catch(e){return fin('ws_construct_error',{error:String(e)})}
  ws.onopen=()=>{opened=true;if(o.send)ws.send(JSON.stringify(o.send));};
  ws.onmessage=e=>{n++;if(first===null)first=(typeof e.data==='string')?e.data:'[binary]';if(n>=min)fin('ws_live');};
  ws.onclose=ev=>fin(opened?'ws_closed_after_open':'ws_never_opened(451/block/network)',{code:ev.code,reason:ev.reason});
  setTimeout(()=>fin(opened?'ws_open_no_data':'ws_connect_timeout'),wait);});}
async function restTest(name,url){const t0=performance.now();
 try{const r=await fetch(url,{cache:'no-store'});R.tests.push({name:name,url:url,class:r.ok?'ok':('http_'+r.status),status:r.status,ms:Math.round(performance.now()-t0)});}
 catch(e){R.tests.push({name:name,url:url,class:'fetch_failed(block/CORS/network)',error:String(e).slice(0,120),ms:Math.round(performance.now()-t0)});}show();}
function pick(raw){try{const d=JSON.parse(raw);const it=Array.isArray(d)?d:(d.data||[d]);const now=Date.now()+2*3600e3;const rows=[];
 it.forEach(x=>{const p=String(x.s||'').split('-');if(p.length!==4||p[3]!=='C')return;const y=2000+ +p[1].slice(0,2),m=+p[1].slice(2,4),dd=+p[1].slice(4,6);
  const ex=Date.UTC(y,m-1,dd,8);if(ex>now)rows.push({s:x.s,ex:ex,k:+p[2],code:p[1],i:+x.i,bo:+x.bo||0});});
 if(!rows.length)return null;const near=Math.min.apply(null,rows.map(r=>r.ex));const same=rows.filter(r=>r.ex===near);const idx=same[0].i;
 same.sort((a,b)=>((a.bo>0?0:1)-(b.bo>0?0:1))||(Math.abs(a.k-idx)-Math.abs(b.k-idx)));return {sym:same[0].s,code:same[0].code,count:it.length,idx:idx};}catch(e){return null;}}
async function jget(name,url){const t0=performance.now();
 try{const r=await fetch(url,{cache:'no-store'});const txt=await r.text();let j=null;try{j=JSON.parse(txt)}catch(e){}
  return {name:name,url:url,status:r.status,class:r.ok?'ok':('http_'+r.status),ms:Math.round(performance.now()-t0),n:Array.isArray(j)?j.length:null,head:txt.slice(0,140),j:j};}
 catch(e){return {name:name,url:url,class:'fetch_failed(block/CORS/network)',error:String(e).slice(0,120),ms:Math.round(performance.now()-t0),j:null};}}
async function histTest(sym){const now=Date.now(),B='https://eapi.binance.com',D=864e5,H=36e5,out=[];
 const add=r=>{const c=Object.assign({},r);delete c.j;out.push(c);};
 const eh=await jget('exerciseHistory (5 din)',B+'/eapi/v1/exerciseHistory?underlying=BTCUSDT&startTime='+(now-5*D)+'&endTime='+now+'&limit=100');add(eh);
 let recs=[];if(Array.isArray(eh.j)){eh.j.forEach(x=>{const sy=x&&(x.symbol||x.s),ex=+(x&&(x.expiryDate||x.expiry));if(sy&&ex&&ex<now)recs.push([ex,sy]);});}
 recs.sort((a,b)=>a[0]-b[0]);const rec=recs.length?recs[recs.length-1]:null,old=recs.length?recs[0]:null;
 if(rec){add(await jget('EXPIRED klines 1h ('+rec[1]+', expiry se pehle 24h)',B+'/eapi/v1/klines?symbol='+rec[1]+'&interval=1h&startTime='+(rec[0]-D)+'&endTime='+rec[0]+'&limit=24'));
  add(await jget('EXPIRED klines 5m ('+rec[1]+', expiry se pehle 2h)',B+'/eapi/v1/klines?symbol='+rec[1]+'&interval=5m&startTime='+(rec[0]-2*H)+'&endTime='+rec[0]+'&limit=24'));}
 if(old&&old!==rec)add(await jget('EXPIRED klines 1h sabse purana ('+old[1]+')',B+'/eapi/v1/klines?symbol='+old[1]+'&interval=1h&startTime='+(old[0]-D)+'&endTime='+old[0]+'&limit=24'));
 if(sym){add(await jget('LIVE klines 1h ('+sym+', 26h-22h pehle)',B+'/eapi/v1/klines?symbol='+sym+'&interval=1h&startTime='+(now-26*H)+'&endTime='+(now-22*H)+'&limit=10'));
  add(await jget('LIVE klines 1m ('+sym+', abhi ke 5 min)',B+'/eapi/v1/klines?symbol='+sym+'&interval=1m&limit=5'));}
 R.options_history=out;
 R.options_history_verdict=out.map(t=>t.name+': '+((t.status===200&&t.n)?('WORKS ✅ ('+t.n+' candles/records)'):(t.status===200?'200 OK par KHAALI ⚠️':(t.status===451?'BLOCKED ❌ 451':('❌ '+t.class+(t.status?(' HTTP '+t.status):''))))));show();}
async function main(){$('go').disabled=true;$('st').textContent='Chal raha hai… (~30s)';
 const t=[
  wsTest('spot kline data-stream.binance.com','wss://data-stream.binance.com/ws/btcusdt@kline_1m'),
  wsTest('spot trade data-stream.binance.vision','wss://data-stream.binance.vision/ws/btcusdt@trade'),
  wsTest('spot kline stream.binance.com (app pehle yahi use karta)','wss://stream.binance.com:9443/ws/btcusdt@kline_1m'),
  wsTest('spot depth20 data-stream.binance.com','wss://data-stream.binance.com/ws/btcusdt@depth20@100ms'),
  wsTest('spot depth20 data-stream.binance.vision','wss://data-stream.binance.vision/ws/btcusdt@depth20@100ms'),
  restTest('REST klines data-api.binance.vision','https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=2'),
  restTest('REST ping api.binance.com','https://api.binance.com/api/v3/ping'),
  restTest('REST eapi ping','https://eapi.binance.com/eapi/v1/ping')];
 const mk=await wsTest('option chain optionMarkPrice','wss://fstream.binance.com/market/ws/btcusdt@optionMarkPrice',{keep:true,wait:10000});
 const pk=mk.first?pick(mk.first):null;
  if(pk){R.option_symbol=pk;const s=pk.sym.toLowerCase(),P='wss://fstream.binance.com/public/ws';
  t.push(wsTest('opt depth10@500ms (chart.html)',P+'/'+s+'@depth10@500ms',{wait:10000}));
  t.push(wsTest('opt depth10@100ms',P+'/'+s+'@depth10@100ms',{wait:10000}));
  t.push(wsTest('opt depth5@500ms',P+'/'+s+'@depth5@500ms',{wait:10000}));
  t.push(wsTest('opt public/stream combined depth10@500ms','wss://fstream.binance.com/public/stream?streams='+s+'@depth10@500ms',{wait:10000}));
  t.push(wsTest('opt SUBSCRIBE depth10@500ms',P,{send:{method:'SUBSCRIBE',params:[s+'@depth10@500ms'],id:1},min:2,wait:10000}));
  t.push(wsTest('opt bookTicker',P+'/'+s+'@bookTicker',{wait:10000}));
  t.push(wsTest('opt optionTicker',P+'/btcusdt@optionTicker@'+pk.code,{wait:10000}));
 }else{R.option_symbol='symbol nahi mila (optionMarkPrice hi live nahi ya parse fail)';}
 await Promise.all(t);
 await histTest(pk?pk.sym:null);
 const L=n=>R.tests.filter(x=>x.name.indexOf(n)===0);const ok=a=>a.some(x=>x.class==='ws_live'||x.class==='ok');
 R.summary={spot_live_price:ok(L('spot kline')) || ok(L('spot trade')),spot_candles_REST:ok(L('REST klines')),spot_depth:ok(L('spot depth')),
  option_chain:ok(L('option chain')),option_depth:ok(L('opt depth'))||ok(L('opt public'))||ok(L('opt SUBSCRIBE')),
  options_history:R.options_history_verdict,
  note:'ws_never_opened = handshake reject/451/network (browser exact HTTP code nahi batata). ws_open_no_data = handshake PASS, block nahi.'};
show();$('st').textContent='Ho gaya. Neeche result — copy karke bhej do.';$('cp').style.display='block';$('go').disabled=false;}
$('go').onclick=main;
$('cp').onclick=()=>{const txt=JSON.stringify(R,null,1);
 try{navigator.clipboard.writeText(txt).then(()=>{$('cp').textContent='✅ Copy ho gaya'})}catch(e){}
 const ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');$('cp').textContent='✅ Copy ho gaya'}catch(e){}ta.remove();};
</script>
"""


def _binance_server_time() -> int:
    try:
        r = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/time",
            proxies=None,
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("serverTime", int(time.time() * 1000))
    except Exception:
        pass
    return int(time.time() * 1000)

def _binance_sign(params: dict, secret_key: str) -> str:
    params["timestamp"] = _binance_server_time()
    params["recvWindow"] = 10000
    qs = urlencode(params, doseq=True)
    sig = hmac.new(secret_key.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"

# ── Binance IP-ban aware backoff (SHARED across saare _binance_call callers:
# ticker, mark, exchangeInfo, depth, orders, panic — sab isi ek function se
# jaate hain, isliye ek hi jagah backoff lagane se sab protect ho jaate hain).
# Jab Binance apna "Way too many requests ... banned until <ms>" error deta
# hai, us exact timestamp tak koi bhi naya REST call is IP se bheja hi nahi
# jaata (network hit skip) — retry karte rehna sirf ban ko lamba/severe
# banata hai. Timestamp parse na ho paaye to bhi 60s ka safety cooldown.
_BINANCE_BAN_UNTIL_MS = 0.0
_BINANCE_BAN_LOCK = threading.Lock()

# ── (2026-09-27) DIAGNOSTICS — "kitni request gayi, kaunsi call ne ban
# trigger kiya, Binance ne exact kya bola" — is se pehle koi visibility
# nahi thi (koi request-counter, koi ban-cause record nahi tha), isliye
# har ban "achanak" lagta tha. Ab har actual outgoing request (skip-hone-
# wali nahi) yahan log hoti hai (rolling window), aur jis bhi call par
# Binance ban/error deta hai uska poora snapshot (path/http-status/raw-
# msg/us waqt tak kitni requests ja chuki thi) yahan save hota hai.
_BN_REQ_LOG = []                 # [(ts, path), ...] rolling — last 120s
_BN_REQ_LOG_LOCK = threading.Lock()
_BN_REQ_LOG_WINDOW_S = 120

_BN_LAST_WEIGHT = {"value": None, "header": None, "path": None, "ts": 0.0}
_BN_LAST_WEIGHT_LOCK = threading.Lock()

_BN_BAN_EVENT = {
    "path": None, "http_status": None, "msg": "", "triggered_at": 0.0,
    "reqs_last_10s": 0, "reqs_last_60s": 0, "until_ms": 0.0,
}
_BN_BAN_EVENT_LOCK = threading.Lock()

def _bn_req_log_add(path: str) -> None:
    now = time.time()
    with _BN_REQ_LOG_LOCK:
        _BN_REQ_LOG.append((now, path))
        cutoff = now - _BN_REQ_LOG_WINDOW_S
        while _BN_REQ_LOG and _BN_REQ_LOG[0][0] < cutoff:
            _BN_REQ_LOG.pop(0)

def _bn_req_counts() -> tuple[int, int]:
    """(requests_last_10s, requests_last_60s) — dono error/success sab
    outgoing attempts count karte hain (skip-hone-wale nahi, kyunki wo
    Binance tak gaye hi nahi)."""
    now = time.time()
    with _BN_REQ_LOG_LOCK:
        last10 = sum(1 for t, _ in _BN_REQ_LOG if now - t <= 10)
        last60 = sum(1 for t, _ in _BN_REQ_LOG if now - t <= 60)
    return last10, last60

def _bn_capture_weight_header(headers) -> None:
    """Binance apne response headers mein X-MBX-USED-WEIGHT* bhejta hai —
    ye asli, official rate-limit-usage number hai (guess/estimate nahi).
    Jo bhi mila usse save kar lete hain (naam thoda vary kar sakta hai
    endpoint ke hisaab se, e.g. 1M window)."""
    try:
        for k, v in headers.items():
            if k.upper().startswith("X-MBX-USED-WEIGHT"):
                with _BN_LAST_WEIGHT_LOCK:
                    _BN_LAST_WEIGHT["value"] = v
                    _BN_LAST_WEIGHT["header"] = k
                    _BN_LAST_WEIGHT["ts"] = time.time()
                return
    except Exception:
        pass

def _binance_check_ban(msg, path: str = "", http_status: "int | None" = None) -> None:
    global _BINANCE_BAN_UNTIL_MS
    if not msg:
        return
    s = str(msg)
    low = s.lower()
    until_ms = None
    idx = low.find("banned until ")
    if idx != -1:
        rest = s[idx + len("banned until "):]
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            try:
                until_ms = float(digits)
            except ValueError:
                until_ms = None
    if until_ms is None and "way too many requests" in low:
        until_ms = (time.time() + 60) * 1000
    if until_ms is None:
        return  # ye ek normal (non-ban) error hai — diagnostics record nahi karte
    with _BINANCE_BAN_LOCK:
        if until_ms > _BINANCE_BAN_UNTIL_MS:
            _BINANCE_BAN_UNTIL_MS = until_ms
    r10, r60 = _bn_req_counts()
    with _BN_BAN_EVENT_LOCK:
        _BN_BAN_EVENT.update({
            "path": path, "http_status": http_status, "msg": s,
            "triggered_at": time.time(), "reqs_last_10s": r10,
            "reqs_last_60s": r60, "until_ms": until_ms,
        })

def _binance_ban_remaining_s():
    """Ban active ho to baaki seconds return karta hai, warna None."""
    with _BINANCE_BAN_LOCK:
        until = _BINANCE_BAN_UNTIL_MS
    remaining = (until / 1000.0) - time.time()
    return remaining if remaining > 0 else None

def _binance_call(base: str, path: str, params: dict, api_key: str,
                   signed: bool = False, secret_key: str = None):
    _ban_left = _binance_ban_remaining_s()
    if _ban_left is not None:
        return False, f"Error -1003: Binance IP-ban abhi active hai (~{int(_ban_left)}s baaki) — client-side backoff, request bheja hi nahi gaya"
    headers = {"X-MBX-APIKEY": api_key} if api_key else {}
    if signed:
        qs = _binance_sign(params.copy(), secret_key)
    else:
        qs = urlencode(params, doseq=True)
    url = f"{base}{path}"
    if qs:
        url = f"{url}?{qs}"
    _bn_req_log_add(path)   # actual network attempt — count it before it goes out
    try:
        r = requests.get(url, headers=headers, timeout=20)
        _bn_capture_weight_header(r.headers)
        ct = r.headers.get("Content-Type", "")
        if "application/json" in ct:
            data = r.json()
            if r.status_code == 200:
                return True, data
            code = data.get("code", r.status_code)
            msg = data.get("msg", "Unknown error")
            _binance_check_ban(msg, path=path, http_status=r.status_code)
            return False, f"Error {code}: {msg}"
        text = r.text.strip()
        if "<html" in text.lower():
            return False, f"HTTP {r.status_code}: Binance returned HTML instead of JSON (endpoint may be unavailable for your account/region)"
        _binance_check_ban(text, path=path, http_status=r.status_code)
        return False, f"HTTP {r.status_code}: {text[:500]}"
    except requests.exceptions.Timeout:
        return False, "Request timed out"
    except requests.exceptions.ConnectionError:
        return False, "Connection error"
    except Exception as e:
        return False, str(e)



# ─── SV3 BTC 1D history (Binance public klines) ────────────────────────────
# SV3 (Long Term Replay) ke "3M/9M/27M/81M/243M" jaise long timeframes ke
# liye BTC ko yahan bhi ek "symbol" ki tarah treat karte hain — bilkul
# Nifty500 stocks jaisa hi per-symbol daily JSON, HF disk par. Farak sirf
# itna hai ki data-source Fyers nahi, Binance hai (public klines endpoint,
# koi API-key/login zaroori nahi) — isliye ye pipeline Nifty500 ke
# incremental-update loop se poori tarah ALAG/independent hai, taaki galti
# se BTC ko Fyers se fetch karne ki koshish na ho (aur na hi Nifty500 loop
# BTC ko touch kare).
#
# NOTE: Binance is HF Space se direct blocked hai — isliye ye saare calls
# _binance_call() ke through jaate hain, jo BINANCE_BASE_URL (Render relay,
# replay-qda7.onrender.com) ko seedha hit karta hai, koi IP proxy nahi.
BTC_1D_SYMBOL         = "BTCUSDT"
BTC_1D_FILENAME       = f"{BTC_1D_SYMBOL}.json"
_BTC_LISTING_START_MS = 1502928000000   # 2017-08-17 00:00:00 UTC — Binance par BTCUSDT listing ki date

def _binance_klines_daily_chunk(start_ms: int, end_ms: int) -> list:
    """Binance /api/v3/klines se ek chunk (max 1000 daily candles) — public
    endpoint, signing nahi chahiye. Candle 'openTime' Binance mein already
    UTC-midnight par align hoti hai (24x7 market, koi session/IST offset
    nahi) — isliye seedha t=openTime_sec use karte hain, jo _sv2_resample_
    btc_daily() ke 'UTC calendar-day' convention se bhi match karta hai."""
    ok, data = _binance_call(
        BINANCE_BASE_URL, "/api/v3/klines",
        {"symbol": BTC_1D_SYMBOL, "interval": "1d",
         "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        "", signed=False,
    )
    if not ok or not isinstance(data, list):
        return []
    out = []
    for k in data:
        # k = [openTime, open, high, low, close, volume, closeTime, ...]
        try:
            out.append({
                "t": int(k[0]) // 1000,
                "o": float(k[1]), "h": float(k[2]),
                "l": float(k[3]), "c": float(k[4]),
            })
        except Exception:
            continue
    return out

def _fetch_btc_full_daily_binance() -> list:
    """BTCUSDT ka POORA available daily history — listing (17-Aug-2017) se
    aaj tak, Binance klines se 1000-candle (~1000-din) chunks mein
    paginate karke. Proxy zaroori hai (SV2 jaisa hi)."""
    now_ms   = int(time.time() * 1000)
    chunk_ms = 1000 * 86400 * 1000   # ek chunk = 1000 din
    all_rows: dict = {}
    start = _BTC_LISTING_START_MS
    while start < now_ms:
        end   = min(start + chunk_ms - 1, now_ms)
        chunk = _binance_klines_daily_chunk(start, end)
        for r in chunk:
            all_rows[r["t"]] = r
        if not chunk:
            start = end + 1
            continue
        last_t_ms = chunk[-1]["t"] * 1000
        if last_t_ms < start:   # safety: progress na ho to infinite-loop se bacho
            break
        start = last_t_ms + 86_400_000
        time.sleep(0.2)   # Binance rate-limit ke liye halka gap
    return sorted(all_rows.values(), key=lambda r: r["t"])

def _fetch_btc_incremental_daily_binance(from_date: str, to_date: str) -> list:
    """Sirf gap (last-saved-date+1 se aaj tak) fetch karta hai — daily
    auto-update ke liye, poora history dobara nahi khinchta. Nifty500 wale
    _fetch_symbol_incremental_daily() jaisa hi role, bas Binance-based."""
    start_ms = int(datetime.datetime.strptime(from_date, "%Y-%m-%d")
                   .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    end_ms   = int(datetime.datetime.strptime(to_date, "%Y-%m-%d")
                   .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) + 86_399_000
    if start_ms > end_ms:
        return []
    all_rows: dict = {}
    cur      = start_ms
    chunk_ms = 1000 * 86400 * 1000
    while cur <= end_ms:
        end   = min(cur + chunk_ms - 1, end_ms)
        chunk = _binance_klines_daily_chunk(cur, end)
        for r in chunk:
            all_rows[r["t"]] = r
        if not chunk:
            break
        cur = chunk[-1]["t"] * 1000 + 86_400_000
        time.sleep(0.2)
    return sorted(all_rows.values(), key=lambda r: r["t"])

def _btc_sv3_incremental_update() -> tuple[bool, str]:
    """Daily gap-fill: BTCUSDT 1D `.bin` (sv3/symbols/BTCUSDT.bin) + Binance ke
    naye missing din -> `.bin` ke END me append (NO_PRELOAD_PLAN Step 3C).
    Nifty500 wale _nifty500_incremental_update se poori tarah independent.
    Sirf BAND (closed) din append hota hai — append-only file me adhoora
    aaj ka candle badla nahi ja sakta (agla update use poora karke laayega)."""
    g = _bin_append_guard()
    if g:
        return False, g
    existing  = _sv3_read_symbol_from_disk(BTC_1D_SYMBOL) or []
    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if existing:
        last_t    = max(r["t"] for r in existing)
        last_date = datetime.datetime.utcfromtimestamp(last_t).date()
        from_d    = (last_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        if from_d > today_str:
            return True, f"BTC 1D already up-to-date. Last candle: {last_date}."
        new_rows = _fetch_btc_incremental_daily_binance(from_d, today_str)
    else:
        new_rows = _fetch_btc_full_daily_binance()
    now_s = int(time.time())
    new_rows = [r for r in (new_rows or []) if r["t"] + 86400 <= now_s]   # sirf closed din
    if not new_rows:
        if existing:
            _btc1d_last_date2 = datetime.datetime.utcfromtimestamp(max(r["t"] for r in existing)).date()
            return True, f"BTC 1D — koi naya (band) candle nahi mila (already up-to-date). Last candle: {_btc1d_last_date2}."
        return True, "BTC 1D — koi naya candle nahi mila (Binance se bhi kuch nahi aaya, aur koi purana data bhi nahi hai)."
    try:
        n = _sv3_bin_apply(BTC_1D_SYMBOL, new_rows, existing)
    except Exception as e:
        _slog_exception("_btc_sv3_incremental_update(bin)", e)
        return False, f"BTC 1D .bin write fail: {e}"
    _sv3_after_update([BTC_1D_SYMBOL])
    tot = _bin_read_header(_sv3_bin_path(BTC_1D_SYMBOL))
    _btc1d_last_date = datetime.datetime.utcfromtimestamp(tot["last_t"]).date()
    return True, f"BTC 1D update: {tot['row_count']} total candles ({n} naye). Last candle: {_btc1d_last_date}."


try:
    import websocket  # pip install websocket-client
except ImportError:
    websocket = None




# ─── WebSocket hub — bidirectional (browser ⇄ server), port 8503 ────────────
# HF Space ka sirf ek external port (8501, nginx) khula hai, isliye ye server
# bhi 127.0.0.1:8503 par andar chalta hai aur nginx `location /ws` isko proxy
# karta hai (browser: wss://<host>/ws). Alag file nahi — app.py ke andar hi
# background thread "WSHub" (FastAPI + uvicorn) me, taaki live state
# (_LIVE ...) seedha isi process ki memory se mile.
#
# Protocol (JSON text frames):
#   browser → server : {"id": "c1", "action": "echo", "data": {...}}
#   server  → browser: {"id": "c1", "ok": true, "data": ...}   (reply, same id)
#                      {"id": "c1", "ok": false, "error": "..."}
#   server  → browser: {"type": "push", "topic": "bn_tick", "data": {...}}
#   built-in actions : subscribe / unsubscribe  ({"topics": [...]})
#
# Naya function WS par daalna = sirf `@_ws_action("naam")` lagana. Wahi function
# HTTP route se bhi call ho sakta hai (dono ek hi registry use kar sakte hain).
#
# NOTE: Streamlit har rerun par script dobara chalata hai, isliye hub state
# @st.cache_resource singleton me hai (process-wide, rerun me reset nahi hota).
import asyncio as _ws_asyncio

_WS_PORT = 8503

@st.cache_resource
def _get_ws_hub() -> dict:
    return {
        "loop":    None,      # WSHub thread ka asyncio loop
        "clients": {},        # websocket -> set(topics)
        "actions": {},        # action naam -> sync fn(data) -> JSON-able
        "last":    {},        # topic -> last payload (subscribe par snapshot)
        "tasks":   set(),
        "stats":   {"in": 0, "out": 0, "connects": 0,
                    "last_error": None, "started_ts": 0.0},
    }

_WS_HUB = _get_ws_hub()
# Har browser connect hote hi in topics par khud subscribe ho jaata hai (client se demand nahi)
_WS_HUB.setdefault("auto_topics", set()).update({"bn_tick", "bn_candle", "bn_oc", "bn_oc_ltp"})

def _ws_action(name: str):
    """Decorator: function ko WS action ke roop me register karta hai.
    Function sync hota hai (executor me chalta hai, isliye blocking call
    theek hai) — signature fn(data) -> JSON-serialisable."""
    def _deco(fn):
        _WS_HUB["actions"][name] = fn     # rerun par naya code overwrite kar deta hai
        return fn
    return _deco

@_ws_action("ping")
def _wsa_ping(data):
    return {"ts": time.time()}

@_ws_action("echo")
def _wsa_echo(data):
    return data

# ─── Origin check — Cross-Site WebSocket Hijacking se bachav ────────────────
# Browser WS handshake me hamesha Origin bhejta hai. Same-host / HF domain / localhost
# allow; baaki band. Origin header na ho (curl, websocket-client) => allow (browser nahi hai).
# Extra origins ke liye HF secret WS_ALLOWED_ORIGINS = "host1,host2" (comma separated).
def _ws_origin_ok(ws) -> bool:
    try:
        from urllib.parse import urlparse as _up_o
        origin = ws.headers.get("origin", "")
        if not origin:
            return True
        oh   = (_up_o(origin).hostname or "").lower()
        host = (ws.headers.get("host", "") or "").split(":")[0].lower()
        if not oh:
            return False
        if oh == host or oh in ("localhost", "127.0.0.1"):
            return True
        if oh.endswith(".hf.space") or oh == "huggingface.co" or oh.endswith(".huggingface.co"):
            return True
        extra = [x.strip().lower() for x in os.environ.get("WS_ALLOWED_ORIGINS", "").split(",") if x.strip()]
        return oh in extra
    except Exception:
        return False

# ─── Generic "api" action — koi bhi JSON HTTP endpoint WS se ─────────────────
# {method:"GET|POST", path:"/api/...", body:<json text>, timeout:<sec>} ko side-server
# (8502, loopback) par chalata hai — HTTP wale handler ka poora logic (validation,
# limits, errors) wahi rehta hai, sirf transport WS ho jaata hai. Binary/Range
# endpoints (music/image files) yahan se nahi jaate — wo HTTP par hi rahenge.
# (2026-10-01) "/api/render_store/" bhi block: WS hub par token-auth nahi hota, aur side-server ko WS se header nahi
# jaate — to ye raasta khula chhodne se render_store ka token-check bypass ho jaata. Is ke liye HTTP (X-Store-Token) hi.
_WS_API_BLOCK = ("/api/music/", "/api/image/", "/api/video/", "/api/render_store/")

def _ws_side_port() -> int:
    try:
        with open(".api_port") as _pf:
            return int(_pf.read().strip())
    except Exception:
        return 8502

def _ws_err_resp(status: int, msg: str) -> dict:
    return {"status": status, "ctype": "application/json",
            "body": json.dumps({"ok": False, "msg": msg, "ts": time.time()})}

@_ws_action("api")
def _wsa_api(data):
    d      = data if isinstance(data, dict) else {}
    method = str(d.get("method") or "GET").upper()
    path   = str(d.get("path") or "")
    if method not in ("GET", "POST"):
        return _ws_err_resp(405, f"method {method} allowed nahi")
    if not path.startswith("/api/") or path.startswith(_WS_API_BLOCK):
        return _ws_err_resp(415, "ye endpoint WS par nahi — HTTP use karo (binary/Range)")
    body = d.get("body")
    try:
        tmo = min(max(float(d.get("timeout") or 130), 1.0), 140.0)
    except Exception:
        tmo = 130.0
    kw = {"timeout": tmo}
    if body is not None:
        kw["data"]    = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        kw["headers"] = {"Content-Type": "application/json"}
    r = requests.request(method, f"http://127.0.0.1:{_ws_side_port()}{path}", **kw)
    ctype = r.headers.get("content-type", "")
    if not ("json" in ctype.lower() or ctype.lower().startswith("text/")):
        return _ws_err_resp(415, f"binary response ({ctype or 'unknown'}) — HTTP use karo")
    return {"status": r.status_code, "ctype": ctype or "application/json",
            "body": r.content.decode("utf-8", "replace")}

async def _ws_send_many(wss, text):
    async def _one(w):
        try:
            await w.send_text(text)
            _WS_HUB["stats"]["out"] += 1
        except Exception:
            _WS_HUB["clients"].pop(w, None)     # dead client
    await _ws_asyncio.gather(*[_one(w) for w in wss])

def _ws_publish(topic: str, data) -> None:
    """Kisi bhi thread se call karo (Fyers/Binance callbacks etc.) — subscribed
    saare browsers ko push. Non-blocking; koi client/loop na ho to no-op."""
    hub = _WS_HUB
    hub["last"][topic] = data
    loop = hub.get("loop")
    if loop is None:
        return
    try:
        targets = [w for w, tp in list(hub["clients"].items()) if topic in tp]
        if not targets:
            return
        text = json.dumps({"type": "push", "topic": topic, "data": data}, default=str)
        _ws_asyncio.run_coroutine_threadsafe(_ws_send_many(targets, text), loop)
    except Exception as e:
        hub["stats"]["last_error"] = f"publish: {type(e).__name__}: {e}"

def _ws_server_checks(main_port: int = 8501) -> list:
    """Starting-page WS readiness (server side). Rows: (ok True/False/None(warn), label, detail)."""
    rows = []
    try:
        _ws_hub_start()          # idempotent
    except Exception as _e:
        rows.append((False, "WSHub start", f"{type(_e).__name__}: {str(_e)[:80]}"))
    for _ in range(30):          # loop ready hone ka max ~3s intezaar
        if _WS_HUB.get("loop") is not None:
            break
        time.sleep(0.1)
    _alive = any(t.name == "WSHub" and t.is_alive() for t in threading.enumerate())
    _err   = _WS_HUB["stats"].get("last_error")
    if _alive and _WS_HUB.get("loop") is not None:
        rows.append((True, "WSHub thread",
                     f"zinda — 127.0.0.1:{_WS_PORT}, {len(_WS_HUB['actions'])} actions registered"))
    elif _alive:
        rows.append((None, "WSHub thread", "thread hai par event-loop abhi ready nahi"))
    else:
        rows.append((False, "WSHub thread", "chal nahi raha" + (f" — {_err}" if _err else
                     " (fastapi/uvicorn requirements.txt me hai? Space logs dekho)")))

    def _health(port):
        _last = None
        for _ in range(10):      # uvicorn bind hone ka chhota intezaar
            try:
                _t0 = time.time()
                _r = requests.get(f"http://127.0.0.1:{port}/ws/health", timeout=1.5)
                return _r, int((time.time() - _t0) * 1000), None
            except Exception as _e:
                _last = _e
                time.sleep(0.2)
        return None, 0, _last

    _r, _ms, _e = _health(_WS_PORT)
    if _r is None:
        rows.append((False, f"Hub :{_WS_PORT}", f"nahi pahuncha: {type(_e).__name__}"))
    else:
        try:
            _j = _r.json()
            rows.append((_r.status_code == 200 and bool(_j.get("ok")), f"Hub :{_WS_PORT}",
                         f"/ws/health → {_r.status_code} ({_ms} ms), clients={_j.get('clients')}"))
        except Exception:
            rows.append((False, f"Hub :{_WS_PORT}", f"/ws/health JSON nahi (status {_r.status_code})"))

    _r, _ms, _e = _health(main_port)
    if _r is None:
        rows.append((False, f"/ws/health on :{main_port}", f"nahi pahuncha: {type(_e).__name__}"))
    else:
        _isj = "json" in _r.headers.get("content-type", "").lower()
        if _r.status_code in (502, 503, 504):
            rows.append((None, f"/ws/health on :{main_port}",
                         f"nginx route live hai par hub jawab nahi de raha (status {_r.status_code})"))
        elif _isj and _r.status_code == 200:
            rows.append((True, f"/ws/health on :{main_port}", f"nginx /ws route OK ({_ms} ms)"))
        else:
            rows.append((False, f"/ws/health on :{main_port}",
                         f"JSON nahi (status {_r.status_code}) — nginx.conf me `location /ws` nahi mila, Space rebuild hua?"))

    if websocket is None:
        rows.append((None, f"WS handshake :{main_port}", "websocket-client installed nahi — skip"))
    else:
        try:
            _t0 = time.time()
            _c = websocket.create_connection(f"ws://127.0.0.1:{main_port}/ws", timeout=3)
            try:
                _hello = json.loads(_c.recv())
                _c.send(json.dumps({"id": "t1", "action": "echo", "data": {"k": 1}}))
                _rep = json.loads(_c.recv())
            finally:
                try:
                    _c.close()
                except Exception:
                    pass
            _ok = (_hello.get("type") == "hello" and bool(_rep.get("ok"))
                   and (_rep.get("data") or {}).get("k") == 1)
            rows.append((_ok, f"WS handshake via :{main_port}",
                         f"nginx → hub, hello + echo OK ({int((time.time() - _t0) * 1000)} ms)" if _ok
                         else f"galat jawab: {str(_rep)[:80]}"))
        except Exception as _e:
            rows.append((False, f"WS handshake via :{main_port}", f"{type(_e).__name__}: {str(_e)[:100]}"))

    _st = _WS_HUB["stats"]
    if _st.get("last_error"):
        rows.append((None, "Hub last_error", str(_st["last_error"])[:140]))
    rows.append((True, "Hub stats", f"connects={_st.get('connects')} in={_st.get('in')} out={_st.get('out')}"))
    return rows

def _ws_hub_start() -> None:
    """WSHub thread start karta hai (idempotent — live thread-name check)."""
    def _run():
        # FastAPI/uvicorn code alag module (ws_hub.py) mein hai — Streamlit ko app.py
        # mein ASGI-app jaisa pattern na dikhe (warna wo uvicorn app:<naam> chalata hai).
        try:
            import ws_hub as _ws_hub_mod
        except Exception as e:
            _WS_HUB["stats"]["last_error"] = f"import ws_hub: {type(e).__name__}: {e}"
            _slog(f"WSHub: ws_hub.py import fail ({e}) — Dockerfile mein COPY ws_hub.py hai?", level="err")
            return
        _ws_hub_mod.run(_WS_HUB, _WS_PORT, _ws_origin_ok, _slog, _slog_exception)

    with _get_api_register_lock():
        if any(t.name == "WSHub" and t.is_alive() for t in threading.enumerate()):
            return
        threading.Thread(target=_run, name="WSHub", daemon=True).start()


# ─── BTC SERVER-SIDE LIVE FEED (Binance WS → HF server → browser hub) ───────
# (2026-09-29) Browser ab Binance se seedha WS nahi kholta (chart.html me
# window._BTC_SERVER_FEED flag). HF server hi Binance ke WS sunta hai aur
# WSHub ke topics par push karta hai:
#   btc_kline      : spot kline_1m  (data-stream.binance.com)     ← candle chart
#   btc_trade      : spot trade     (data-stream.binance.com)     ← live tick
#   btc_oc_mark    : optionMarkPrice array, saare BTC options      (fstream /market)
#   btc_oc_ticker  : optionTicker batches (24h last/chg%/volume)   (fstream /public)
#   btc_depth      : option depth10@500ms, per-symbol             (fstream /public)
# Sab hosts HF-server test me ✅ the (451 wale api/stream/eapi hosts use NAHI hote).
# Lease model: browser `btc_feed_start` / `btc_depth_sub` action se lease renew
# karta hai; lease khatam (browser band) to upstream WS khud band ho jaate hain.
@st.cache_resource
def _get_btcf() -> dict:
    return {
        "lease":    {"tick": 0.0, "oc": 0.0},
        "depth":    {},          # SYMBOL -> last lease ts
        "expiries": set(),       # "YYMMDD" codes (mark stream se discover)
        "feeds":    {},          # feed name -> stats
        "lock":     threading.Lock(),
    }

_BTCF = _get_btcf()
BTCF_LEASE_SEC       = 300      # tick/oc lease (browser har ~30s renew karta hai)
BTCF_DEPTH_LEASE_SEC = 60       # depth symbol lease (browser har ~20s renew karta hai)
BTCF_PUBLISH_MS      = 250      # spot kline/trade coalesce interval
BTCF_MAX_DEPTH_SYMS  = 20
_BTCF_SPOT_KLINE_URL = "wss://data-stream.binance.com/ws/btcusdt@kline_1m"
_BTCF_SPOT_TRADE_URL = "wss://data-stream.binance.com/ws/btcusdt@trade"
_BTCF_MARK_URL       = "wss://fstream.binance.com/market/ws/btcusdt@optionMarkPrice"
_BTCF_PUBLIC_URL     = "wss://fstream.binance.com/public/ws"

def _btcf_stat(name: str) -> dict:
    return _BTCF["feeds"].setdefault(name, {
        "connected": False, "connects": 0, "msgs": 0, "published": 0,
        "last_ts": 0.0, "last_error": None, "url": "", "stopped_ts": 0.0})

def _btcf_lease_ok(key: str) -> bool:
    now = time.time()
    if key == "depth":
        return any((now - ts) < BTCF_DEPTH_LEASE_SEC for ts in list(_BTCF["depth"].values()))
    return (now - _BTCF["lease"].get(key, 0.0)) < BTCF_LEASE_SEC

def _btcf_run(name: str, url: str, lease_key: str, on_msg, on_open=None, on_idle=None) -> None:
    """Reconnecting upstream WS loop. Lease khatam hone par khud band ho jaata hai."""
    st = _btcf_stat(name)
    fail = 0
    if websocket is None:
        st["last_error"] = "websocket-client installed nahi"
        return
    _slog(f"BTC server feed [{name}] start → {url}", level="info")
    while _btcf_lease_ok(lease_key):
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=10)
            ws.settimeout(1.0)
            st.update(connected=True, url=url, last_error=None)
            st["connects"] += 1
            fail = 0
            if on_open:
                on_open(ws)
            while _btcf_lease_ok(lease_key):
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if on_idle:
                        on_idle(ws)
                    continue
                if not raw:
                    raise ConnectionError("upstream closed")
                st["msgs"] += 1
                st["last_ts"] = time.time()
                on_msg(raw, ws)
                if on_idle:
                    on_idle(ws)
        except Exception as e:
            st["last_error"] = f"{type(e).__name__}: {str(e)[:140]}"
        finally:
            st["connected"] = False
            try:
                if ws is not None:
                    ws.close()
            except Exception:
                pass
        if not _btcf_lease_ok(lease_key):
            break
        fail += 1
        time.sleep(min(1.0 * (2 ** (fail - 1)), 15.0))
    st["connected"] = False
    st["stopped_ts"] = time.time()
    _slog(f"BTC server feed [{name}] band (lease khatam)", level="info")

def _btcf_spot_loop(name: str, url: str, topic: str, is_kline: bool) -> None:
    state = {"last_pub": 0.0, "last_t": None}
    def _on_msg(raw, ws):
        try:
            d = json.loads(raw)
        except Exception:
            return
        if not isinstance(d, dict):
            return
        now = time.time()
        if is_kline:
            k = d.get("k")
            if not k:
                return
            # nayi candle / closed candle kabhi drop nahi hoti, baaki 250ms coalesce
            must = bool(k.get("x")) or k.get("t") != state["last_t"]
            state["last_t"] = k.get("t")
        else:
            if d.get("p") is None:
                return
            must = False
        if must or (now - state["last_pub"]) * 1000.0 >= BTCF_PUBLISH_MS:
            state["last_pub"] = now
            _btcf_stat(name)["published"] += 1
            _ws_publish(topic, d)
    _btcf_run(name, url, "tick", _on_msg)

def _btcf_kline_loop() -> None:
    _btcf_spot_loop("spot_kline", _BTCF_SPOT_KLINE_URL, "btc_kline", True)

def _btcf_trade_loop() -> None:
    _btcf_spot_loop("spot_trade", _BTCF_SPOT_TRADE_URL, "btc_trade", False)

def _btcf_mark_loop() -> None:
    def _on_msg(raw, ws):
        try:
            arr = json.loads(raw)
        except Exception:
            return
        if not isinstance(arr, list) or not arr:
            return
        exp = _BTCF["expiries"]
        for it in arr:
            try:
                parts = str(it.get("s") or "").split("-")
                if len(parts) == 4 and parts[1] not in exp:
                    exp.add(parts[1])
            except Exception:
                pass
        _btcf_stat("opt_mark")["published"] += 1
        _ws_publish("btc_oc_mark", arr)
    _btcf_run("opt_mark", _BTCF_MARK_URL, "oc", _on_msg)

def _btcf_ticker_loop() -> None:
    # optionTicker: sirf badle hue symbols aate hain. Sab known expiries ek hi
    # connection par SUBSCRIBE (max 200 streams), browser ko 1s batches me.
    st = {"sent": set(), "rpc": 1, "buf": {}, "last_flush": 0.0}
    def _sync(ws):
        for code in sorted(_BTCF["expiries"] - st["sent"]):
            st["rpc"] += 1
            ws.send(json.dumps({"method": "SUBSCRIBE",
                                "params": [f"btcusdt@optionTicker@{code}"], "id": st["rpc"]}))
            st["sent"].add(code)
        now = time.time()
        if st["buf"] and (now - st["last_flush"]) >= 1.0:
            batch = list(st["buf"].values())
            st["buf"].clear()
            st["last_flush"] = now
            _btcf_stat("opt_ticker")["published"] += 1
            _ws_publish("btc_oc_ticker", batch)
    def _on_open(ws):
        st["sent"] = set()
        st["buf"].clear()
        _sync(ws)
    def _on_msg(raw, ws):
        try:
            m = json.loads(raw)
        except Exception:
            return
        if isinstance(m, dict) and m.get("e") == "24hrTicker" and m.get("s"):
            st["buf"][m["s"]] = m
    _btcf_run("opt_ticker", _BTCF_PUBLIC_URL, "oc", _on_msg, on_open=_on_open, on_idle=_sync)

def _btcf_depth_loop() -> None:
    st = {"subbed": set(), "rpc": 1000}
    def _sync(ws):
        now = time.time()
        for sym, ts in list(_BTCF["depth"].items()):
            if (now - ts) >= BTCF_DEPTH_LEASE_SEC:
                _BTCF["depth"].pop(sym, None)
        want = set(_BTCF["depth"].keys())
        add, rem = want - st["subbed"], st["subbed"] - want
        if add:
            st["rpc"] += 1
            ws.send(json.dumps({"method": "SUBSCRIBE",
                                "params": [f"{s.lower()}@depth10@500ms" for s in sorted(add)],
                                "id": st["rpc"]}))
        if rem:
            st["rpc"] += 1
            ws.send(json.dumps({"method": "UNSUBSCRIBE",
                                "params": [f"{s.lower()}@depth10@500ms" for s in sorted(rem)],
                                "id": st["rpc"]}))
        st["subbed"] = want
    def _on_open(ws):
        st["subbed"] = set()
        _sync(ws)
    def _on_msg(raw, ws):
        try:
            m = json.loads(raw)
        except Exception:
            return
        if isinstance(m, dict) and m.get("s") and ("b" in m or "a" in m):
            _btcf_stat("opt_depth")["published"] += 1
            _ws_publish("btc_depth", {"s": m["s"], "m": m})
    _btcf_run("opt_depth", _BTCF_PUBLIC_URL, "depth", _on_msg, on_open=_on_open, on_idle=_sync)

def _btcf_ensure(kind: str) -> None:
    """Idempotent  thread-name check (lock ke saath). Dead thread ho to naya start."""
    table = {
        "tick":  (("BtcSrvKline", _btcf_kline_loop), ("BtcSrvTrade", _btcf_trade_loop)),
        "oc":    (("BtcSrvOptMark", _btcf_mark_loop), ("BtcSrvOptTicker", _btcf_ticker_loop)),
        "depth": (("BtcSrvOptDepth", _btcf_depth_loop),),
    }
    with _BTCF["lock"]:
        alive = {t.name for t in threading.enumerate() if t.is_alive()}
        for tname, fn in table.get(kind, ()):
            if tname not in alive:
                threading.Thread(target=fn, name=tname, daemon=True).start()

@_ws_action("btc_feed_start")
def _wsa_btc_feed_start(data):
    d = data if isinstance(data, dict) else {}
    want = d.get("want") or ["tick", "oc"]
    now = time.time()
    started = []
    for k in ("tick", "oc"):
        if k in want:
            _BTCF["lease"][k] = now
            _btcf_ensure(k)
            started.append(k)
    return {"ok": True, "started": started, "ts": now}

@_ws_action("btc_depth_sub")
def _wsa_btc_depth_sub(data):
    d = data if isinstance(data, dict) else {}
    import re as _re_btc
    sym = str(d.get("symbol") or "").strip().upper()
    if not _re_btc.match(r"^BTC-\d{6}-\d+(\.\d+)?-[CP]$", sym):
        raise ValueError(f"bad option symbol: {sym[:40]}")
    with _BTCF["lock"]:
        if sym not in _BTCF["depth"] and len(_BTCF["depth"]) >= BTCF_MAX_DEPTH_SYMS:
            oldest = min(_BTCF["depth"], key=lambda s: _BTCF["depth"][s])
            _BTCF["depth"].pop(oldest, None)
        _BTCF["depth"][sym] = time.time()
    _btcf_ensure("depth")
    return {"ok": True, "symbol": sym}

@_ws_action("btc_feed_status")
def _wsa_btc_feed_status(data):
    now = time.time()
    feeds = {}
    for name, s in list(_BTCF["feeds"].items()):
        feeds[name] = dict(s, age_sec=(round(now - s["last_ts"], 1) if s["last_ts"] else None))
    return {"leases_age_sec": {k: (round(now - v, 1) if v else None) for k, v in _BTCF["lease"].items()},
            "depth_symbols": sorted(_BTCF["depth"].keys()),
            "expiries": sorted(_BTCF["expiries"]),
            "feeds": feeds,
            "hub_clients": len(_WS_HUB["clients"]),
            "threads": sorted(t.name for t in threading.enumerate() if t.name.startswith("BtcSrv"))}



# ── Finnhub WebSocket — LIVE ticks for Twelve Data registry symbols
# (Dow/GBP-USD/Apple/Amazon) ────────────────────────────────────────────
# Historical/backfill data Twelve Data se aata hai (td_symbols.py:
# td_update_all, manual Update All, HF disk par store hota hai).
# LIVE price-update ab is Finnhub WebSocket se aata hai — free-plan pe
# bhi real push-based streaming milti hai (Twelve Data free-plan pe
# sirf REST polling milti hai, 8 credit/min cap ke saath, isliye 60s se
# tez nahi ho sakti thi).
_FINNHUB_WS_STATE = {"connected": False, "last_msg_ts": None, "last_error": None}
_FINNHUB_WS_BACKOFF_BASE = 2.0
_FINNHUB_WS_BACKOFF_MAX  = 30.0

def _finnhub_ws_backoff_sleep(fail_count: int) -> None:
    import random as _fh_random
    delay = min(_FINNHUB_WS_BACKOFF_BASE * (2 ** max(0, fail_count - 1)), _FINNHUB_WS_BACKOFF_MAX)
    time.sleep(delay + _fh_random.uniform(0, delay * 0.3))

def _finnhub_symbol_to_key_map() -> dict:
    """Finnhub 'finnhub_symbol' → registry 'key' — WS message aane par
    turant pata chal jaaye ye kis symbol ka tick hai."""
    return {e["finnhub_symbol"]: e["key"] for e in _td.TD_SYMBOL_REGISTRY if e.get("finnhub_symbol")}

def _finnhub_ws_on_message(ws, message):
    try:
        data = json.loads(message)
    except Exception:
        return
    if not isinstance(data, dict) or data.get("type") != "trade":
        return
    _sym_to_key = _finnhub_symbol_to_key_map()
    for trade in (data.get("data") or []):
        if not isinstance(trade, dict):
            continue
        sym = trade.get("s")
        key = _sym_to_key.get(sym)
        if not key:
            continue
        price = trade.get("p")
        ts_ms = trade.get("t")   # Finnhub epoch milliseconds
        epoch_ts = (ts_ms / 1000.0) if ts_ms else time.time()
        try:
            _td.td_apply_live_tick(key, price, epoch_ts)
        except Exception:
            pass
    _FINNHUB_WS_STATE["last_msg_ts"] = time.time()
    _FINNHUB_WS_STATE["last_error"] = None

def _finnhub_ws_loop():
    if websocket is None or not FINNHUB_API_KEY:
        _FINNHUB_WS_STATE["last_error"] = (
            "websocket-client missing" if websocket is None else "FINNHUB_API_KEY secret missing"
        )
        return
    fail_count = 0
    while True:
        try:
            def _on_open(ws):
                nonlocal fail_count
                _FINNHUB_WS_STATE.update({"connected": True, "last_msg_ts": time.time(), "last_error": None})
                fail_count = 0
                for entry in _td.TD_SYMBOL_REGISTRY:
                    _fh_sym = entry.get("finnhub_symbol")
                    if _fh_sym:
                        try:
                            ws.send(json.dumps({"type": "subscribe", "symbol": _fh_sym}))
                        except Exception:
                            pass
            wsapp = websocket.WebSocketApp(
                f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}",
                on_open=_on_open,
                on_message=_finnhub_ws_on_message,
                on_error=lambda ws, e: _FINNHUB_WS_STATE.update({"connected": False, "last_error": str(e)}),
                on_close=lambda ws, c, m: _FINNHUB_WS_STATE.update({"connected": False}),
            )
            wsapp.run_forever(ping_interval=20, ping_timeout=10)  # Render (wss) seedha reachable hai — IP proxy nahi chahiye
        except Exception as e:
            _FINNHUB_WS_STATE["last_error"] = str(e)
        _FINNHUB_WS_STATE["connected"] = False
        fail_count += 1
        _finnhub_ws_backoff_sleep(fail_count)

def _ensure_finnhub_ws_thread():
    """Idempotent — dobara call hone par bhi duplicate thread nahi banti."""
    names = {t.name for t in threading.enumerate()}
    if "FinnhubLiveWS" not in names:
        threading.Thread(target=_finnhub_ws_loop, name="FinnhubLiveWS", daemon=True).start()


# ─── Market Depth (5-level Bid/Ask order book) — jaisa Fyers app ka
# "Market Depth" bottom-sheet dikhata hai (Qty(Orders) | Bid | Ask | (Orders)Qty,
# total buy/sell %, aur Price Stats: Open/High/Low/PrevClose/AvgPrice/Circuits/
# Volume/LTQ). Ye Option Chain API se ALAG endpoint hai — option chain sirf
# best bid/ask (1 level) deta hai, depth API 5 levels + totalbuyqty/totalsellqty
# deta hai. Har symbol ka apna chhota TTL cache — jab tak koi ek strike ka depth
# modal khula ho, sirf usi symbol ke liye poll hota hai (saare strikes ka depth
# fetch karne ki zaroorat nahi, rate-limit safe). ──────────────────────────────
_DEPTH_CACHE: dict = {}
_DEPTH_LOCK = threading.Lock()
_DEPTH_TTL  = 3.0  # seconds — depth apna alag chhota TTL, sirf active symbol ke liye
# (2026-09-25) 1.5 → 3.0: browser ka apna WS depth-stream (100ms) already
# live update deta hai, ye REST poll sirf fallback/backup hai. 1.5s bahut
# tight tha — browser REST-poll (bhi ~1.5s) + is backend poll ke combine
# hone se same Binance IP par double load pad raha tha, jisse "Way too many
# requests" wala temporary IP-ban trigger hua tha.

_DEPTH_DEBUG = {"last_error": "", "last_status": None, "last_branch": "", "last_symbol": "", "ts": 0.0}

# Market-depth heartbeat (`_live_data_pusher` ke andar) ki run-count/state -- sirf
# debug/diagnostic ke liye, taaki frontend confirm kar sake ki pusher zinda hai aur
# backend ka _active_depth_symbol kya dikh raha hai.
_MD_PUSHER_DEBUG = {"runs": 0, "last_active_symbol": None, "last_run_ts": 0.0}

def fyers_get_market_depth(app_id: str, access_token: str, symbol: str) -> "dict | None":
    """Fyers Market Depth API se 5-level bid/ask order book + price stats fetch karta hai.
    Response shape match karta hai Fyers app ke 'Market Depth' screen se:
    bids/asks (5 levels each, price+volume+orders), totalbuyqty/totalsellqty,
    o/h/l/c, ltp/ltq/volume, upper_ckt/lower_ckt, atp (avg price).
    Poori tarah try/except mein wrapped hai — koi bhi unexpected exception yahin
    pakdi jaati hai taaki side-server ka do_GET crash na ho (warna browser ko
    'connection failed' milta hai, kisi asli JSON error ke bajaye)."""
    try:
        headers = {"Authorization": f"{app_id}:{access_token}"}
        params = {"symbol": symbol, "ohlcv_flag": "1"}
        resp = requests.get("https://api-t1.fyers.in/data/depth", headers=headers, params=params, timeout=6)
        last_status = resp.status_code
        try:
            r = resp.json()
        except Exception:
            err = f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"
            _DEPTH_DEBUG.update({"last_error": err, "last_status": last_status, "ts": time.time()})
            return {"error": err}

        if not isinstance(r, dict) or r.get("s") != "ok":
            err = (r.get("message", str(r)) if isinstance(r, dict) else str(r))[:300]
            _DEPTH_DEBUG.update({"last_error": err, "last_status": last_status, "ts": time.time()})
            return {"error": f"Fyers depth API fail — {err} (HTTP {last_status})"}

        dmap = r.get("d") or {}
        if not isinstance(dmap, dict) or not dmap:
            err = "Fyers ne depth data khaali bheja (dmap empty)"
            _DEPTH_DEBUG.update({"last_error": err, "last_status": last_status, "ts": time.time()})
            return {"error": err}

        # Normally dmap ki key exact 'symbol' hoti hai, lekin agar Fyers thoda
        # alag casing/format bhejde to fallback: agar sirf ek hi entry hai to
        # wahi use kar lo (case-insensitive match bhi try karo).
        d = dmap.get(symbol)
        if d is None:
            for k, v in dmap.items():
                if k.upper() == symbol.upper():
                    d = v
                    break
        if d is None and len(dmap) == 1:
            d = next(iter(dmap.values()))
        if not d:
            err = f"Symbol '{symbol}' depth response mein nahi mila. Mile keys: {list(dmap.keys())[:5]}"
            _DEPTH_DEBUG.update({"last_error": err, "last_status": last_status, "ts": time.time()})
            return {"error": err}

        _DEPTH_DEBUG.update({"last_error": "", "last_status": last_status, "ts": time.time()})
        return {
            "symbol": symbol,
            "ltp": d.get("ltp", 0),
            "ch": d.get("ch", 0),
            "chp": d.get("chp", 0),
            "bids": d.get("bids", []),
            "asks": d.get("ask", []),
            "total_buy_qty":  d.get("totalbuyqty", 0),
            "total_sell_qty": d.get("totalsellqty", 0),
            "open": d.get("o", 0),
            "high": d.get("h", 0),
            "low": d.get("l", 0),
            "prev_close": d.get("c", 0),
            "atp": d.get("atp", 0),
            "upper_ckt": d.get("upper_ckt", 0),
            "lower_ckt": d.get("lower_ckt", 0),
            "volume": d.get("v", 0),
            "ltq": d.get("ltq", 0),
            "ts": time.time(),
        }
    except Exception as e:
        _DEPTH_DEBUG.update({"last_error": str(e), "last_status": None, "ts": time.time()})
        return {"error": f"Exception: {e}"}

def refresh_market_depth_cache(symbol: str) -> dict:
    """TTL-cached depth fetch for Fyers/NSE symbols (BankNifty etc.) — ek hi
    symbol baar-baar poll hone par bhi upstream ko sirf har _DEPTH_TTL
    second mein ek baar hit karta hai.

    BTC/Binance market depth yahan handle nahi hota — wo WS hub se aata hai
    (`_wsa_btc_depth_sub`). Ye function sirf Fyers/NSE symbols ke liye hai.

    DEBUG: har return path apna `_debug.branch` set karta hai taaki frontend
    (chart.html ke Order Book debug panel) ko pata chale ki backend ke andar
    EXACT kaunsi wajah se fail hua — 'cache_hit' / 'fyers_creds_missing' /
    'fyers_api_error' / 'unrecognized_symbol_format' / 'exception' / 'ok'.
    """
    branch = "unknown"
    try:
        now = time.time()
        with _DEPTH_LOCK:
            entry = _DEPTH_CACHE.get(symbol)
            if entry and (now - entry["ts"]) < _DEPTH_TTL:
                cached = dict(entry["data"])
                cached["_debug"] = {**cached.get("_debug", {}), "branch": "cache_hit",
                                     "cache_age_s": round(now - entry["ts"], 2)}
                return cached

        _looks_fyers = symbol.upper().startswith(("NSE:", "BSE:", "MCX:"))

        if not _looks_fyers:
            branch = "unrecognized_symbol_format"
            payload = {
                "error": (
                    f"'{symbol}' — symbol format pehchana nahi gaya, ya backend "
                    f"depth support nahi karta. Expected: 'NSE:...' (Fyers/index "
                    f"options). BTC/Binance depth ab browser WebSocket se seedha "
                    f"aata hai, backend ke through nahi."
                ),
                "_debug": {"branch": branch, "symbol": symbol},
            }
            with _DEPTH_LOCK:
                _DEPTH_CACHE[symbol] = {"data": payload, "ts": now}
            _DEPTH_DEBUG.update({"last_error": payload["error"], "last_status": None,
                                  "last_branch": branch, "last_symbol": symbol, "ts": now})
            return payload

        creds = load_creds()
        if not creds.get("access_token") or not creds.get("app_id"):
            branch = "fyers_creds_missing"
            payload = {
                "error": "Fyers login nahi mila — pehle login karo.",
                "_debug": {"branch": branch, "symbol": symbol},
            }
        else:
            payload = fyers_get_market_depth(creds["app_id"], creds["access_token"], symbol) or {"error": "unknown error"}
            branch = "fyers_api_error" if payload.get("error") else "ok"
            payload["_debug"] = {"branch": branch, "symbol": symbol,
                                  "http_status": _DEPTH_DEBUG.get("last_status")}
        with _DEPTH_LOCK:
            _DEPTH_CACHE[symbol] = {"data": payload, "ts": now}
        _DEPTH_DEBUG.update({"last_error": payload.get("error", ""), "last_branch": branch,
                              "last_symbol": symbol, "ts": now})
        return payload
    except Exception as e:
        branch = "exception"
        _DEPTH_DEBUG.update({"last_error": str(e), "last_status": None,
                              "last_branch": branch, "last_symbol": symbol, "ts": time.time()})
        return {"error": f"refresh_market_depth_cache exception: {e}",
                "_debug": {"branch": branch, "symbol": symbol}}

# ─── Next-month expiry chain — apna alag TTL cache (SV1's "This Month /
# Next Month" toggle ke liye). Current-month jitni baar refresh karne ki
# zaroorat nahi (next month kam frequently move karta hai) — isliye lamba
# TTL rakha taaki Fyers rate-limit par extra load na pade. ────────────────
_OC_NEXT_CACHE = {"data": None, "ts": 0.0}
_OC_NEXT_LOCK  = threading.Lock()
_OC_NEXT_TTL   = 30  # seconds

def _oc_next_month_chain(app_id: str, access_token: str, expiries: list) -> "dict | None":
    """Current chain ke 'expiries' list se agla (2nd) monthly expiry dhoondh
    kar uska poora CE/PE chain fetch karta hai. Fyers isi endpoint ko
    'timestamp' param (us expiry ka epoch) ke saath dobara call karke deta
    hai — koi alag endpoint nahi hai."""
    now = time.time()
    with _OC_NEXT_LOCK:
        stale = (now - _OC_NEXT_CACHE["ts"]) >= _OC_NEXT_TTL
        cached = _OC_NEXT_CACHE["data"]
    if not stale:
        return cached
    result = None
    if expiries and len(expiries) > 1:
        try:
            next_epoch = int(expiries[1].get("expiry"))
        except (TypeError, ValueError):
            next_epoch = None
        if next_epoch:
            result = fyers_get_option_chain(app_id, access_token, timestamp=str(next_epoch))
    with _OC_NEXT_LOCK:
        _OC_NEXT_CACHE.update({"data": result, "ts": now})
    return result


def refresh_option_chain_cache() -> dict:
    """TTL ke andar cache use karta hai, warna Fyers se dobara fetch karta hai,
    aur fyers_optionchain.json mein likh deta hai (chart iframe poll fallback ke liye).
    Failure hone par bhi ek 'error' field ke saath JSON likhta hai — silently
    hang nahi hota. Saath mein 'next' key mein next-month expiry ka chain bhi
    bundle karta hai taaki frontend This-Month/Next-Month switch client-side
    hi kar sake, koi extra request ki zaroorat nahi."""
    now = time.time()
    with _OC_LOCK:
        stale = (now - _OC_CACHE["ts"]) >= _OC_TTL
    if stale:
        creds = load_creds()
        if not creds.get("access_token") or not creds.get("app_id"):
            payload = {"error": "Fyers login nahi mila — creds file mein access_token/app_id missing hai."}
        else:
            # strikecount=20 — BTC (Binance) window ke barabar, taaki frontend
            # ka "Strikes" dropdown (max 20 each side) BankNifty ke liye bhi
            # bina extra rows-missing ke kaam kare.
            data = fyers_get_option_chain(creds["app_id"], creds["access_token"], strikecount=20)
            if data:
                with _OC_LOCK:
                    _OC_CACHE.update({"data": data, "ts": now})
                payload = dict(data)
                payload["next"] = _oc_next_month_chain(
                    creds["app_id"], creds["access_token"], data.get("expiries") or []
                )
            else:
                payload = {
                    "error": f"Fyers Option Chain API fail ho gayi — {_OC_DEBUG.get('last_error','unknown error')} "
                             f"(HTTP {_OC_DEBUG.get('last_status')}, URL: {_OC_DEBUG.get('last_url')})",
                }
        try:
            with open(OC_FILE, "w") as f:
                json.dump(payload, f)
        except Exception:
            pass
        return payload

    with _OC_LOCK:
        cached = _OC_CACHE["data"]
    if not cached:
        return {"error": "Cache khaali hai"}
    result = dict(cached)
    creds = load_creds()
    if creds.get("access_token") and creds.get("app_id"):
        result["next"] = _oc_next_month_chain(creds["app_id"], creds["access_token"], cached.get("expiries") or [])
    else:
        result["next"] = None
    return result


# ─── Option Chain background refresher ─────────────────────────────────────
# Ek alag background daemon thread khud apni raftaar se (har ~1s check, andar
# TTL-gated hai to asli Fyers call sirf _OC_TTL second mein ek baar hoti hai)
# option chain fetch karta rehta hai aur natije ko _OC_LAST_PAYLOAD mein likhta
# hai. _live_data_pusher fragment sirf ye already-computed cache padhta hai —
# koi network I/O nahi, isliye kabhi block nahi karta. ──────────
_OC_LAST_PAYLOAD: dict = {"data": None, "ts": 0.0}
_OC_LAST_PAYLOAD_LOCK = threading.Lock()

# ── Fyers option-chain BG-loop heartbeat — Fyers ke liye koi option-chain WebSocket nahi hai (poori tarah
# is background poll-loop par depend), isliye har iteration apna timestamp yahan
# likhta hai, aur wahi 'diag' block ke through payload ke saath frontend tak jaata
# hai — taaki thread crash/atakna debug panel mein dikhe. ─────────
@st.cache_resource
def _get_fyers_oc_heartbeat() -> dict:
    return {"bg_loop_ts": 0.0}

_FYERS_OC_HEARTBEAT = _get_fyers_oc_heartbeat()

_OC_PUB_STATE = {"sig": None}

def _option_chain_bg_loop():
    while True:
        try:
            payload = refresh_option_chain_cache()
        except Exception as e:
            payload = {"error": f"bg loop exception: {e}"}
        now = time.time()
        _FYERS_OC_HEARTBEAT["bg_loop_ts"] = now
        # ── Diagnostics block — payload ke saath hi bundle karke bhejte
        # hain taaki frontend debug panel ko backend se WS ki tarah alag
        # se poll na karna pade. bg_loop hamesha isi tick par abhi-abhi
        # update hua hai (age ~0), isliye ye field khud staleness detect
        # karne ke liye nahi (uske liye frontend apna postMessage-arrival
        # timestamp use karta hai) — ye sirf 'thread zinda hai aur last
        # fetch attempt ka nateeja kya tha' batane ke liye hai. ──────────
        if isinstance(payload, dict):
            payload["diag"] = {
                "bg_loop_last_ts": now,
                "last_fetch_error": _OC_DEBUG.get("last_error"),
                "last_fetch_status": _OC_DEBUG.get("last_status"),
                "last_fetch_url": _OC_DEBUG.get("last_url"),
                "last_fetch_ts": _OC_DEBUG.get("ts"),
            }
        with _OC_LAST_PAYLOAD_LOCK:
            _OC_LAST_PAYLOAD.update({"data": payload, "ts": now})
        # WebSocket: naya chain (ya naya error) aate hi sab browsers ko push; live LTP legs subscribe
        try:
            _oc_live_sync_subs(payload)
            if isinstance(payload, dict):
                with _OC_LIVE_LOCK:
                    payload["oc_live"] = {"want": len(_OC_LIVE["want"]), "subs": len(_OC_LIVE["subs"]),
                                          "live": len(_OC_LIVE["ltp"]),
                                          "last_tick_ts": _OC_LIVE.get("last_tick_ts")}
            _sig = (payload.get("error") if isinstance(payload, dict) else None, _OC_CACHE.get("ts"))
            if _sig != _OC_PUB_STATE["sig"]:
                _OC_PUB_STATE["sig"] = _sig
                _ws_publish("bn_oc", payload)
        except Exception as e:
            _WS_HUB["stats"]["last_error"] = f"bn_oc publish: {type(e).__name__}: {e}"
        time.sleep(1)

def get_cached_option_chain_payload() -> dict:
    """Non-blocking read — background thread ye already update kar raha hai.
    Streamlit fragment/pusher isi ko call kare, kabhi refresh_option_chain_cache()
    seedha na bulaye (warna wapas blocking wapas aa jaayegi)."""
    with _OC_LAST_PAYLOAD_LOCK:
        data = _OC_LAST_PAYLOAD["data"]
        age  = time.time() - _OC_LAST_PAYLOAD["ts"]
    if data is None:
        return {"error": "Option chain load ho raha hai… (pehli fetch abhi baaki hai)"}
    if age > 15:
        # Background thread kisi wajah se ruk gaya ho to purana data dikhane
        # ke bajaye saaf bata do — silently stale data dikhana bhi galat hai.
        d = dict(data)
        d["stale_warning"] = f"Data {int(age)}s purana hai — background refresh check karo"
        return d
    return data


def _write_login_log(payload: dict, status_code: int, response: dict):
    """Write login attempt details to login_debug.json for inspection."""
    try:
        safe_payload = {k: ("***" if k == "code" else v) for k, v in payload.items()}
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S IST", time.localtime()),
            "request": safe_payload,
            "http_status": status_code,
            "response": response,
        }
        with open("login_debug.json", "w") as f:
            json.dump(entry, f, indent=2)
    except Exception:
        pass

# ─── Token expiry monitor (background) ─────────────────────────────────────────
# NOTE: Fyers vagator login API is IP-restricted (blocks cloud/VPS IPs).
# So we only MONITOR expiry and set a flag — user does a quick 15-sec re-auth.
_TOKEN_STATUS: dict = {"expired": False, "checked_at": 0.0, "running": False}
_TOKEN_STATUS_LOCK = threading.Lock()

# ─── Generic alert system (Fyers / Binance / kuch bhi) ─────────────────────
# Ek hi function: send_alert(source, message). Ye jo channels configured hain
# (Brevo email, Telegram, Fast2SMS) un sab par bhejta hai. Har `source` ka
# apna 1-ghante ka rate-limit hai, taaki ek hi alert baar baar na aaye, aur
# Fyers ka alert Binance ka alert nahi rokta.
_ALERT_LAST_SENT: dict = {}          # {source: last_success_ts}
_ALERT_LAST_RESULT: dict = {}        # {"ts", "source", "channels": {name: "ok"/"err text"}}
_ALERT_LOCK = threading.Lock()
_ALERT_COOLDOWN_SEC = 3600

def _alert_via_brevo(subject: str, message: str, html: str = "") -> str:
    """Brevo transactional email (HTTPS API). Return 'ok' ya error text."""
    if not BREVO_API_KEY:
        return "skip: BREVO_API_KEY set nahi"
    if not ALERT_EMAIL_TO:
        return "skip: ALERT_EMAIL_TO set nahi"
    if not BREVO_SENDER_EMAIL:
        return "skip: sender email nahi (BREVO_SENDER_EMAIL / ALERT_EMAIL_TO)"
    try:
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY,
                     "accept": "application/json",
                     "content-type": "application/json"},
            json={
                "sender": {"name": "Trading Alerts", "email": BREVO_SENDER_EMAIL},
                "to": [{"email": ALERT_EMAIL_TO}],
                "subject": subject,
                "textContent": message,
                **({"htmlContent": html} if html else {}),
            },
            timeout=12,
        )
        if r.status_code in (200, 201, 202):
            return "ok"
        return f"err: HTTP {r.status_code} {r.text[:200]}"
    except Exception as e:
        return f"err: {type(e).__name__}: {e}"

def _alert_via_telegram(message: str) -> str:
    """Telegram bot (optional). Return 'ok' ya error text."""
    if TELEGRAM_RELAY_URL and RELAY_SECRET:
        try:
            r = requests.post(
                f"{TELEGRAM_RELAY_URL}/send",
                headers={"X-Relay-Secret": RELAY_SECRET},
                json={"text": message},
                timeout=70,   # Render free service so rahi ho to pehli request ~50s leti hai
            )
            if r.status_code == 200 and r.json().get("ok"):
                return "ok"
            return f"err: relay HTTP {r.status_code} {r.text[:200]}"
        except Exception as e:
            return f"err: relay {type(e).__name__}: {e}"
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return "skip: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set nahi"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=12,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return "ok"
        return f"err: HTTP {r.status_code} {r.text[:200]}"
    except Exception as e:
        # NOTE: exception text mein token aa sakta hai (URL mein hota hai) — hata do
        txt = str(e).replace(TELEGRAM_BOT_TOKEN, "***")
        return f"err: {type(e).__name__}: {txt}"

def _alert_via_sms(message: str) -> str:
    """Fast2SMS (optional, purana tarika). Return 'ok' ya error text."""
    if not FAST2SMS_KEY:
        return "skip: FAST2SMS_KEY set nahi"
    try:
        creds = load_creds()
        phone = creds.get("alert_phone", "7018093451")
        r = requests.get(
            "https://www.fast2sms.com/dev/bulkV2",
            headers={"authorization": FAST2SMS_KEY},
            params={"route": "q", "numbers": str(phone),
                    "message": message, "flash": 0},
            timeout=10,
        )
        data = r.json()
        if data.get("return", False):
            return "ok"
        return f"err: {str(data.get('message', data))[:200]}"
    except Exception as e:
        return f"err: {type(e).__name__}: {e}"

def _alert_email_and_telegram(subject: str, message: str, html: str = "") -> str:
    """(2026-10-01) Email (Brevo) + Telegram dono par bhejo. Gmail email rok de to Telegram se alert fir bhi pahunche.
    'ok' tab jab kam se kam ek channel chala; warna error text."""
    r_mail = _alert_via_brevo(subject, message, html)
    r_tg = _alert_via_telegram(f"{subject}\n\n{message}")
    try:
        _slog(f"[alert2] email={r_mail} telegram={r_tg}",
              level="ok" if "ok" in (r_mail, r_tg) else "warn")
    except Exception:
        pass
    if r_mail == "ok" or r_tg == "ok":
        return "ok"
    return f"email: {r_mail} | telegram: {r_tg}"

def send_alert(source: str, message: str, force: bool = False) -> bool:
    """Alert bhejo. source = 'Fyers' / 'Binance' / ... (rate-limit key + subject).
    force=True → cooldown ignore (test button ke liye). True tab jab kam se kam
    ek channel se sach mein chala gaya."""
    now = time.time()
    with _ALERT_LOCK:
        last = _ALERT_LAST_SENT.get(source, 0.0)
        if (not force) and (now - last < _ALERT_COOLDOWN_SEC):
            return False
        # Race se bachne ke liye pehle hi mark karo; fail hua to neeche wapas hata denge
        _ALERT_LAST_SENT[source] = now

    subject = f"[{source}] Alert"
    results = {
        "email":    _alert_via_brevo(subject, message),
        "telegram": _alert_via_telegram(f"[{source}] {message}"),
        "sms":      _alert_via_sms(f"[{source}] {message}"),
    }
    ok = any(v == "ok" for v in results.values())

    with _ALERT_LOCK:
        if not ok:
            _ALERT_LAST_SENT[source] = last   # fail hua → agli baar phir try hoga
        _ALERT_LAST_RESULT.update({"ts": now, "source": source, "channels": results})

    try:
        _slog(f"ALERT[{source}] sent={ok} " +
              " ".join(f"{k}={v}" for k, v in results.items()),
              level="ok" if ok else "warn")
    except Exception:
        pass
    return ok

def _token_monitor_loop():
    """Checks Fyers token validity every 5 min. Sets expired flag and sends alert."""
    with _TOKEN_STATUS_LOCK:
        if _TOKEN_STATUS["running"]:
            return
        _TOKEN_STATUS["running"] = True

    while True:
        try:
            creds = load_creds()
            if not creds.get("access_token"):
                time.sleep(60)
                continue

            headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
            today = _ist_now().strftime("%Y-%m-%d")
            try:
                res = requests.get(
                    "https://api-t1.fyers.in/data/history",
                    headers=headers,
                    params={"symbol": "NSE:NIFTYBANK-INDEX", "resolution": "D",
                            "date_format": "1", "range_from": today, "range_to": today, "cont_flag": "1"},
                    timeout=8,
                ).json()
                still_active = res.get("s") == "ok"
            except Exception:
                still_active = True  # network glitch, assume ok

            with _TOKEN_STATUS_LOCK:
                _TOKEN_STATUS["expired"] = not still_active
                _TOKEN_STATUS["checked_at"] = time.time()

            # Reset session cache so sidebar reflects truth
            if not still_active:
                _sess_cache.update({"active": False, "ts": 0.0})
                # Write sentinel so next rerun clears _force_active
                try:
                    with open(".token_expired_flag", "w") as _f:
                        _f.write("1")
                except Exception:
                    pass
                # Alert bhejo (email/Telegram/SMS — Fyers ke liye ghante mein max 1)
                send_alert(
                    "Fyers",
                    "BankNifty Dashboard Alert: Fyers token expired! "
                    "Please re-login at your dashboard to restore live data."
                )

            time.sleep(300)  # check every 5 minutes
        except Exception:
            time.sleep(60)


def _extract_auth_code(url_or_code: str) -> str:
    """Extract auth_code from a full Google redirect URL or return as-is."""
    import urllib.parse
    s = url_or_code.strip()
    if s.startswith("http"):
        parsed = urllib.parse.urlparse(s)
        qs = urllib.parse.parse_qs(parsed.query)
        return qs.get("auth_code", [s])[0]
    return s

# ─── Session check ─────────────────────────────────────────────────────────────
_sess_cache = {"active": False, "ts": 0.0}

# ─── Session-check debug instrumentation ───────────────────────────────────
# Har is_session_active() call, kis reason se True/False decide hua, aur
# kahan se call hua — ye sab yahan log hota hai (_STARTUP_LOG mein, jo
# already thread-safe + global hai). "🔍 Session Debug" panel (login page
# ke top par) isi log ko live dikhata hai — koi restart/rebuild ki zaroorat
# nahi, agla rerun hote hi naye lines dikh jaate hain.
def _sess_debug_caller() -> str:
    """Kis function ne is_session_active() call kiya — stack se nikaalo."""
    import inspect
    try:
        stack = inspect.stack()
        # frame[0]=yahi helper, frame[1]=is_session_active, frame[2]=asli caller
        return stack[2].function if len(stack) > 2 else "?"
    except Exception:
        return "?"

def _sess_debug(reason: str, active: bool, update_cache: bool, caller: str) -> None:
    try:
        age = time.time() - _sess_cache["ts"]
    except Exception:
        age = -1
    _slog(
        f"🔍 SESSDBG caller={caller} update_cache={update_cache} → "
        f"reason={reason} result={active} | "
        f"cache_now(active={_sess_cache.get('active')}, age={age:.1f}s) "
        f"force_active={st.session_state.get('_force_active')}",
        level="info",
    )

def is_session_active(update_cache: bool = True) -> bool:
    """Token exist karna + profile API ok = session active.
    Market band hone par bhi False nahi karega.

    update_cache=True (default): sirf top-level PAGE-ROUTING check ke liye
    use karo (jo decide karta hai login-page dikhana hai ya chart). Ye
    global `_sess_cache` (poore server-process ke liye shared, session-
    specific nahi) mein result likhta hai.

    update_cache=False: kisi bhi doosre jagah se — jaise data-update
    buttons (BankNifty/BTC Update Karo) — call karo, jinka kaam sirf
    "abhi fetch ke liye token valid hai ya nahi" jaanna hai. Ye result
    ko global cache mein WRITE nahi karta, taaki aisa button galti se
    poore app ko "logged in" mode mein switch na kar de (asal login-flow
    complete kiye bina) — ye hi pehle wala bug tha.

    Har return path pe _sess_debug() se ek log-line jaati hai — taaki
    exactly pata chale kis WAJAH se True/False mila (debug panel isi
    log ko dikhata hai, login page ke top par, live + copyable).
    """
    now = time.time()
    caller = _sess_debug_caller()

    # 1. token_monitor ne expire flag set kiya? clear _force_active
    if os.path.exists(".token_expired_flag"):
        try:
            os.remove(".token_expired_flag")
        except Exception:
            pass
        if update_cache:
            st.session_state["_force_active"] = False
            _sess_cache.update({"active": False, "ts": 0.0})

    # 2. login ke turant baad force-active flag
    if st.session_state.get("_force_active"):
        if update_cache:
            _sess_cache.update({"active": True, "ts": now})
        _sess_debug("force_active_flag", True, update_cache, caller)
        return True

    # 2. fresh cache — read-only calls (update_cache=False) bhi cache PADH
    # sakte hain (taaki wo bhi rate-limited rahein), bas WRITE nahi karte.
    if now - _sess_cache["ts"] < 120:
        _sess_debug(f"cache_fresh(age={now - _sess_cache['ts']:.1f}s)", _sess_cache["active"], update_cache, caller)
        return _sess_cache["active"]

    creds = load_creds()
    if not creds.get("access_token"):
        if update_cache:
            _sess_cache.update({"active": False, "ts": now})
        _sess_debug("no_access_token", False, update_cache, caller)
        return False

    # 3. Profile endpoint use karo — market hours se independent
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    try:
        res = requests.get(
            "https://api-t1.fyers.in/api/v3/profile",
            headers=headers, timeout=4,
        ).json()
        active = res.get("s") == "ok" or res.get("code") == 200
        if not active:
            # fallback: history endpoint — "no_data" = market closed but token valid
            today = _ist_now().strftime("%Y-%m-%d")
            res2 = requests.get(
                "https://api-t1.fyers.in/data/history",
                headers=headers,
                params={"symbol": "NSE:NIFTYBANK-INDEX", "resolution": "D",
                        "date_format": "1", "range_from": today,
                        "range_to": today, "cont_flag": "1"},
                timeout=4,
            ).json()
            active = res2.get("s") in ("ok", "no_data")
    except Exception:
        # FIX: pehle yahan "active = True" tha (fail-open) — soch ye thi ki
        # transient network glitch par user ko galti se logged-out na dikhaya
        # jaaye. Lekin isi wajah se ek confusing bug ban gaya tha: agar token
        # sach mein expire ho chuka ho aur Fyers API isi wajah se fail/timeout
        # ho (jo bhi ho sakta hai jab auth hi invalid ho), ye code galat se
        # "session valid hai" maan leta tha aur chart mode khol deta tha —
        # jabki asli data-fetch (jo alag se sahi tarah fail hoti hai) kabhi
        # kaam nahi karta. Ab fail-safe: real check fail ho to session ko
        # INVALID maano, taaki UI aur asli data-fetch dono ek hi (sahi) nateeje
        # par sehmat rahein.
        active = False

    if update_cache:
        _sess_cache.update({"active": active, "ts": now})
    _sess_debug("live_profile_check", active, update_cache, caller)
    return active

# ─── Stack View 2: .gz data load (local file pehle, GitHub fallback) + resample ─
import gzip as _gzip
import io as _io

# (Step 2) `_SV2_CACHE` (bn_raw/btc_raw/*_tfs_full) HATA diya — ab RAM me kuch nahi rehta.

# ── SV2 disk-cache filenames — `_SV2_CACHE_DIR` upar (Storage Manager
# section) define hai. READ path (neeche) SIRF is HF `/data` disk-cache se
# hota hai (koi fallback nahi). Agar disk par file na mile, seedha fail hota hai —
# `_url_fetch_save_to_disk` wale Migration card se pehle ek-baari daalni
# padegi (already kar chuke ho).
_SV2_BN_FILENAME  = "banknifty_5m_csv_json.gz"
_SV2_BTC_FILENAME = "Bitcoin_BTCUSDT_IST_5m_json.gz"

def _sv2_parse_gz_bytes(raw_bytes: bytes) -> list:
    """Raw .gz bytes ko decompress + JSON parse karo."""
    with _gzip.open(_io.BytesIO(raw_bytes), "rb") as f:
        data = json.load(f)
    # Both formats supported: {"meta":..,"data":[..]} or plain list
    return data["data"] if isinstance(data, dict) else data

def _sv2_read_gz_from_disk(label: str, filename: str) -> "tuple[list, str]":
    """HF `/data/sv2_cache/<filename>` se raw .gz bytes padh ke parse karta
    hai — koi network call nahi, koi fallback nahi. Returns (rows, source) —
    source 'disk'/'failed'."""
    path = os.path.join(_SV2_CACHE_DIR, filename)
    try:
        if not os.path.isfile(path):
            _slog(f"SV2 [{label}] disk-cache file nahi mili: {path} — "
                  f"pehle starting-page Migration card se daalo.", level="err")
            return [], "failed"
        with open(path, "rb") as f:
            raw_bytes = f.read()
        rows = _sv2_parse_gz_bytes(raw_bytes)
        _slog(f"SV2 [{label}] HF disk se load hui ({len(rows)} rows) — {path}", level="ok")
        return rows, "disk"
    except Exception as e:
        _slog_exception(f"_sv2_read_gz_from_disk({label})", e)
        return [], "failed"

# (2026-10-02, NO_PRELOAD_PLAN Step 2) Ye 2 loaders ab RAM me CACHE NAHI
# karte (pehle `_SV2_CACHE["bn_raw"/"btc_raw"]` me poori file hamesha padi
# rehti thi). Ab inka kaam sirf purani gz se LAZY fallback hai — jab `.bin`
# missing/corrupt ho aur env BIN_READ_FALLBACK != "0" ho — wo bhi sirf us
# ek request ke liye, result turant free. Startup par kabhi nahi chalte.
def _sv2_load_bn_gz() -> list:
    """BankNifty 5m candles — purani gz se (transient, cache nahi)."""
    rows, _source = _sv2_read_gz_from_disk("BankNifty", _SV2_BN_FILENAME)
    return rows

def _sv2_load_btc_gz() -> list:
    """BTC 5m candles — purani gz se (transient, cache nahi)."""
    rows, _source = _sv2_read_gz_from_disk("BTC", _SV2_BTC_FILENAME)
    return rows

# (2026-10-02, NO_PRELOAD_PLAN Step 2) SV1 replay master ka startup PREWARM,
# 132-symbol multi-TF prewarm thread aur "warming up" banner HATA diye gaye.
# Master ab `.bin` (memory-mapped) hai: request aane par sirf zaroori window
# padha jaata hai, isliye warm-up hai hi nahi — app turant ready hoti hai.

# ─── Music player ──────────────────────────────────────────────────────────
# Music files /data/music/ (HF persistent storage) me rehti hain — file manager
# (WebDAV, /dav) se add/delete/rename karo. Music panel /api/music_list (disk
# listing) + /api/music/<file> (HTTP GET + Range support) side-port routes se
# play karta hai — koi base64 nahi.
MUSIC_DIR = "/data/music" if os.path.isdir("/data") else os.path.join(os.getcwd(), "_local_music")
try:
    os.makedirs(MUSIC_DIR, exist_ok=True)
except Exception:
    pass

_MUSIC_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus")


def _music_sorted_names() -> list:
    """/data/music ki asli disk-listing (sorted audio filenames)."""
    try:
        return sorted(
            f for f in os.listdir(MUSIC_DIR)
            if f.lower().endswith(_MUSIC_EXTS)
        )
    except Exception:
        return []

# ─── Video player ──────────────────────────────────────────────────────────
# Videos /data/video/ (HF persistent storage, sub-folders allowed, e.g.
# /data/video/punjabi_songs/x.mp4) — file manager (WebDAV, /dav) se manage karo.
# Panel /api/video_list + /api/video/<rel/path> (HTTP GET + Range) se chalta hai.
VIDEO_DIR = "/data/video" if os.path.isdir("/data") else os.path.join(os.getcwd(), "_local_video")
try:
    os.makedirs(VIDEO_DIR, exist_ok=True)
except Exception:
    pass

_VIDEO_EXTS = (".mp4", ".m4v", ".webm", ".mov")
_VIDEO_MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


def _video_list_items(limit: int = 1000) -> list:
    """/data/video ki disk-listing (sub-folders ke saath): [(rel_path, size_bytes, mtime), ...]."""
    out = []
    try:
        base = os.path.realpath(VIDEO_DIR)
        for root, dirs, files in os.walk(base):
            dirs.sort()
            for f in sorted(files):
                if f.lower().endswith(_VIDEO_EXTS):
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    try:
                        _st = os.stat(full)
                        sz, mt = _st.st_size, int(_st.st_mtime)
                    except OSError:
                        sz, mt = 0, 0
                    out.append((rel, sz, mt))
                    if len(out) >= limit:
                        return out
    except Exception:
        pass
    return out


def _video_resolve(rel: str):
    """URL se aaya relative path -> asli file path, ya None. VIDEO_DIR se bahar
    (../, absolute path, symlink escape) kuch bhi allowed nahi."""
    if not rel or "\\" in rel or "\x00" in rel:
        return None
    if not rel.lower().endswith(_VIDEO_EXTS):
        return None
    base = os.path.realpath(VIDEO_DIR)
    full = os.path.realpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep):
        return None
    return full if os.path.isfile(full) else None


def _video_parse_range(header, fsize: int):
    """Range header -> (status, start, end). status 200 (poori file), 206 (partial)
    ya 416 (unsatisfiable). 'bytes=a-b', 'bytes=a-' aur 'bytes=-n' teeno handle."""
    import re as _re
    if not header or fsize <= 0:
        return 200, 0, max(fsize - 1, 0)
    m = _re.match(r"\s*bytes=(\d*)-(\d*)\s*$", header)
    if not m or not (m.group(1) or m.group(2)):
        return 200, 0, fsize - 1
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else fsize - 1
    else:
        n = int(m.group(2))
        start, end = max(0, fsize - n), fsize - 1
    end = min(end, fsize - 1)
    if start >= fsize or start > end:
        return 416, 0, 0
    return 206, start, end


# ─── Image Viewer ──────────────────────────────────────────────────────────
# Images /data/images/ (HF persistent storage) me rehti hain — file manager
# (WebDAV, /dav) se add/delete/rename karo. Header ke 🖼️ icon wala viewer isi
# folder ki disk-listing (/api/image_list) se chalta hai.
IMAGES_DIR = "/data/images" if os.path.isdir("/data") else os.path.join(os.getcwd(), "_local_images")
try:
    os.makedirs(IMAGES_DIR, exist_ok=True)
except Exception:
    pass

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")

# (2026-10-02, NO_PRELOAD_PLAN Step 3A) Image RAM-cache (`_IMAGES_RAM_CACHE`),
# batch-prewarm thread aur startup trigger HATA diye (pehle poora 509MB folder
# RAM me bhara jaata tha). Ab image seedha disk se stream hoti hai
# (`_image_etag` + route me ETag/304); OS page cache hot files ko khud sambhal
# leta hai (lazy + evictable, app memory me count nahi hota).


def _image_etag(st_) -> str:
    """Weak-free strong ETag: file ki mtime+size se. File replace/edit hui to
    ETag badal jaata hai => browser fresh file leta hai."""
    return f'"{st_.st_mtime_ns:x}-{st_.st_size:x}"'


def _images_meta(names: list) -> dict:
    """Image viewer ke sort ke liye: {name: [mtime_epoch_sec, size_bytes]} --
    seedha disk (os.stat) se, koi guess nahi. Jo file stat na ho sake wo skip."""
    out: dict = {}
    for n in names or []:
        try:
            st_ = os.stat(os.path.join(IMAGES_DIR, n))
            out[n] = [int(st_.st_mtime), int(st_.st_size)]
        except Exception:
            pass
    return out


def _images_sorted_names() -> list:
    try:
        return sorted(
            f for f in os.listdir(IMAGES_DIR)
            if f.lower().endswith(_IMAGE_EXTS)
        )
    except Exception:
        return []


# Twelve Data — external (non-NSE, non-crypto) symbols jaise Gold/Dow Jones.
# Generic registry td_symbols.py mein hai — naya symbol wahan add karo,
# yahan kuch badalne ki zaroorat nahi.
TWELVEDATA_API_KEY = _get_secret("TWELVEDATA_API_KEY")

# Finnhub — LIVE ticks (WebSocket, free-tier) ke liye. Twelve Data se sirf
# historical/backfill hota hai; live-price update yahan se aata hai (dekho
# _finnhub_ws_loop() aur td_symbols.td_apply_live_tick()).
FINNHUB_API_KEY = _get_secret("FINNHUB_API_KEY")

# ─── Local persistent storage — chart save/restore (drawings, Future Line,
# zoom, layouts, per-panel settings). HF Space ke bucket-mounted persistent
# volume (Space Settings → Storage → mounted at /data, dekho runtime.volumes
# mein mountPath) par save hota hai. Personal single-user app hai,
# isliye per-device split nahi — sirf 3 chhoti JSON files (state/settings/
# layout), koi bhi browser is Space ko khole to wahi teeno milte hain, koi
# login/auth ki zaroorat nahi. Agar /data mount na mile (jaise local dev
# testing mein), local folder par fallback ho jaata hai taaki crash na ho.
LOCAL_STATE_DIR = "/data/app_state" if os.path.isdir("/data") else os.path.join(os.getcwd(), "_local_app_state")
try:
    os.makedirs(LOCAL_STATE_DIR, exist_ok=True)
except Exception:
    pass

# ─── Render store (2026-09-30) — Render wale main.ts ka rules / trade journal / render_log ab yahin
# HF `/data/app_state/render/` par save hota hai (egress limit ki wajah se yahin shift kiya).
#   rules.json        → {data:[...]} poori rules list, whole-blob latest-wins (atomic write)
#   journal.jsonl     → har order/cancel/panic/close/rule-exit ki ek line (kabhi delete nahi hoti)
#   render_log.jsonl  → Render ka browser>render / render>binance record (30 din se purani rows roz-ek-baar hat-ti hain)
# Raaste (side server, nginx /api/*): GET|POST /api/render_store/<rules|journal|log|log_telegram>
#   DO ALAG SERVICES, DO ALAG LOG FILES (aapas me mix nahi hote) — poori guide: RENDER_LOG_GUIDE.md
#     log           -> render_log.jsonl           -> Render service "replay-qda7" (main.ts: Binance trade + rules engine)
#     log_telegram  -> render_log_telegram.jsonl  -> Render service "telegram-aib3" (telegram_relay.js)
#   Dono par same token (X-Store-Token = RENDER_STORE_TOKEN), same row format, same 30-din prune. Rows me "service" field bhi hota hai.
#   Render (main.ts) → header X-Store-Token == secret RENDER_STORE_TOKEN (HF Space secret; main.ts me HF_STORE_TOKEN).
#   (2026-10-01) GET /api/render_store/log ab TOKEN ke peeche hai (pehle khula tha; log rows me symbol/side/qty/price
#   jaate hain, isliye public nahi rakha). Sirf GET /api/render_store/ping bina token ke khula hai (koi trade data nahi).
#   chart.html ka Render Log tab ab X-Store-Token bhejta hai — token browser ke localStorage ('rlog_token') me ek baar
#   poocha jaata hai, source me kahin hardcode nahi. WS "api" transport se /api/render_store/* BLOCK hai (_WS_API_BLOCK),
#   warna WS hub (jahan token nahi lagta) token-check bypass kar deta.
# NOTE: Storage Manager ka "Wipe All" button/function (2026-10-01) permanently hata diya gaya hai — ab is folder
#   (rules + journal + log) ko ek-click me uda dene wala koi raasta nahi. Sirf "Selected Delete" bacha hai.
RENDER_STORE_DIR   = os.path.join(LOCAL_STATE_DIR, "render")
RENDER_STORE_TOKEN = _get_secret("RENDER_STORE_TOKEN").strip()
_RS_FILES          = {"rules": "rules.json", "journal": "journal.jsonl", "log": "render_log.jsonl", "log_telegram": "render_log_telegram.jsonl"}
_RS_LOG_NAMES      = ("log", "log_telegram")   # jin store-names par 30-din prune lagta hai (dono log files alag-alag prune hoti hain)
_RS_LOCK           = threading.Lock()
_RS_LOG_KEEP_DAYS  = 30
_RS_LOG_MAX_LINES  = 300_000          # 30 din se pehle bhi file is se badi ho to sirf latest itni lines
_RS_MAX_BODY       = 3 * 1024 * 1024
_RS_MAX_ROWS       = 2000
_RS_SEEN: dict     = {}               # batch_id -> ts (retry duplicate rokne ke liye, last ~300)
_RS_LAST_PRUNE     = [0.0]

def _rs_path(name: str) -> str:
    return os.path.join(RENDER_STORE_DIR, _RS_FILES[name])

def _rs_token_ok(tok) -> bool:
    if not RENDER_STORE_TOKEN:
        return False
    try:
        return hmac.compare_digest(str(tok or "").encode("utf-8"), RENDER_STORE_TOKEN.encode("utf-8"))
    except Exception:
        return False

def _rs_persistent() -> bool:
    return bool(str(LOCAL_STATE_DIR).startswith("/data"))

def _rs_read_rules():
    """rules.json ka content (list) ya None (file abhi bani nahi)."""
    p = _rs_path("rules")
    with _RS_LOCK:
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
    return obj.get("data") if isinstance(obj, dict) else obj

def _rs_write_rules(data: list) -> None:
    os.makedirs(RENDER_STORE_DIR, exist_ok=True)
    p = _rs_path("rules")
    tmp = p + ".tmp"
    with _RS_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"data": data, "saved_at": time.time()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

def _rs_prune_log_locked(name: str = "log") -> None:
    """render_log.jsonl / render_log_telegram.jsonl se purani (30 din+) rows hatao — streaming, poori file RAM me nahi. _RS_LOCK pakda hona chahiye."""
    p = _rs_path(name)
    if not os.path.exists(p):
        return
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=_RS_LOG_KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    from collections import deque
    keep = deque(maxlen=_RS_LOG_MAX_LINES)
    for line in open(p, "r", encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            ts = str(json.loads(line).get("ts") or "")
        except Exception:
            continue
        if ts[:19] >= cutoff:
            keep.append(line)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for line in keep:
            f.write(line + "\n")
    os.replace(tmp, p)

def _rs_append(name: str, rows: list, batch_id: str = "") -> int:
    """rows ko <name>.jsonl me append. Return: likhi gayi rows (0 = duplicate batch_id)."""
    os.makedirs(RENDER_STORE_DIR, exist_ok=True)
    p = _rs_path(name)
    with _RS_LOCK:
        if batch_id and batch_id in _RS_SEEN:
            return 0
        with open(p, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if batch_id:
            _RS_SEEN[batch_id] = time.time()
            if len(_RS_SEEN) > 300:
                for k in sorted(_RS_SEEN, key=_RS_SEEN.get)[:100]:
                    _RS_SEEN.pop(k, None)
        if name in _RS_LOG_NAMES and time.time() - _RS_LAST_PRUNE[0] > 24 * 3600:
            _RS_LAST_PRUNE[0] = time.time()
            for _ln in _RS_LOG_NAMES:          # dono log files alag-alag prune
                try:
                    _rs_prune_log_locked(_ln)
                except Exception as e:
                    _slog_exception("_rs_prune_log " + _ln, e)
    return len(rows)

def _rs_tail(name: str, limit: int) -> list:
    """File ki aakhri `limit` rows (newest-first, ts desc). Peeche se chunk-wise padhta hai — badi file poori load nahi hoti."""
    p = _rs_path(name)
    limit = max(1, min(int(limit), 5000))
    with _RS_LOCK:
        if not os.path.exists(p):
            return []
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            buf = b""
            while pos > 0 and buf.count(b"\n") <= limit + 1:
                step = min(262144, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
    lines = buf.split(b"\n")
    if pos > 0:
        lines = lines[1:]          # pehli line adhoori ho sakti hai
    out = []
    for ln in lines[-(limit + 1):]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    out = out[-limit:]
    out.sort(key=lambda r: str((r or {}).get("ts") or ""), reverse=True)
    return out

def _rs_ping() -> dict:
    sizes = {}
    for k in _RS_FILES:
        try:
            sizes[k] = os.path.getsize(_rs_path(k)) if os.path.exists(_rs_path(k)) else 0
        except Exception:
            sizes[k] = None
    return {"ok": True, "configured": bool(RENDER_STORE_TOKEN), "persistent": _rs_persistent(),
            "dir": RENDER_STORE_DIR, "bytes": sizes, "ts": time.time()}

def _rs_handle(method: str, name: str, qs: dict, token, raw_body: bytes):
    """Side-server handler ka core. Return (http_status, dict). Koi exception bahar nahi jaati."""
    try:
        if name == "ping" and method == "GET":
            return 200, _rs_ping()
        if name not in _RS_FILES:
            return 404, {"ok": False, "msg": "unknown store"}
        # (2026-10-01) rules / journal / log — SAB token ke peeche (log GET pehle khula tha)
        if not RENDER_STORE_TOKEN:
            return 503, {"ok": False, "msg": "RENDER_STORE_TOKEN secret set nahi (HF Space Settings → Secrets)"}
        if not _rs_token_ok(token):
            return 401, {"ok": False, "msg": "bad or missing X-Store-Token"}
        if method == "GET":
            if name == "rules":
                return 200, {"ok": True, "data": _rs_read_rules(), "persistent": _rs_persistent()}
            try:
                lim = int((qs.get("limit") or ["500"])[0])
            except Exception:
                lim = 500
            return 200, {"ok": True, "rows": _rs_tail(name, lim), "persistent": _rs_persistent()}
        if method == "POST":
            if not raw_body or len(raw_body) > _RS_MAX_BODY:
                return 413, {"ok": False, "msg": "body khaali ya bahut bada"}
            body = json.loads(raw_body.decode("utf-8"))
            if name == "rules":
                data = body.get("data")
                if not isinstance(data, list):
                    return 400, {"ok": False, "msg": "data list chahiye"}
                _rs_write_rules(data)
                return 200, {"ok": True, "count": len(data), "persistent": _rs_persistent()}
            rows = body.get("rows")
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                return 400, {"ok": False, "msg": "rows list of objects chahiye"}
            if len(rows) > _RS_MAX_ROWS:
                return 413, {"ok": False, "msg": f"ek batch me max {_RS_MAX_ROWS} rows"}
            n = _rs_append(name, rows, str(body.get("batch_id") or ""))
            return 200, {"ok": True, "written": n, "duplicate": (n == 0 and len(rows) > 0), "persistent": _rs_persistent()}
        return 405, {"ok": False, "msg": "method allowed nahi"}
    except Exception as e:
        try:
            _slog_exception(f"render_store {method} {name}", e)
        except Exception:
            pass
        return 500, {"ok": False, "msg": f"{type(e).__name__}: {e}"}

try:
    os.makedirs(RENDER_STORE_DIR, exist_ok=True)
except Exception:
    pass

# (2026-09-21) "diary" = 📔 Meri Diary (chart.html) — ek JSON blob /data/app_state/diary.json me, baaki teeno
# kinds jaisa hi save lane (WS action local_state_save) + build-time inject. Entries me per-entry `t` hota hai, restore
# merge chart.html me hota hai.
# (2026-09-25) "reminders" = 🔔 Reminders (chart.html) — ek JSON blob {items:[...]} /data/app_state/
# reminders.json me, state/settings/layout jaisa hi whole-blob latest-wins save lane (per-entry merge nahi,
# diary se alag). Har item: {id, label, type: "once"|"recurring", date: "YYYY-MM-DD"|"MM-DD", time: "HH:MM"}.
# Alert-bhejna (Brevo email) is JSON ko _reminder_check_loop() background thread padhta hai — dekho neeche.
# (2026-09-25) Reminder thread ko "abhi jaag, reminders.json badli hai" batane ka
# signal. @st.cache_resource = process-wide singleton (Streamlit rerun me file dobara
# chalti hai, plain Event() har rerun naya bana deta aur purani thread ko signal na milta).
@st.cache_resource
def _get_reminder_wake() -> threading.Event:
    return threading.Event()

# (2026-10-01) "routine" = ⏰ Daily Routine + To-Do (chart.html) — JSON blob /data/app_state/routine.json,
# reminders jaisa whole-blob latest-wins lane (koi server-side processing nahi, sirf save/restore).
# MYENGINE-STEP-7 (2026-10-03): "accounts" = 💰 Savings Accounts ledger (⏰ panel ka Accounts tab) — apni alag file
# /data/app_state/accounts.json (routine blob me NAHI: paisa-data hai, email-merge / routine pruning se door rehna chahiye).
# Blob: {v:1, rev:int, accounts:[...], entries:[...], t}. Save par REVISION CHECK hota hai — dekho _accounts_save().
_LOCAL_STATE_KINDS = ("state", "settings", "layout", "diary", "reminders", "routine", "accounts", "moneymgr")   # moneymgr = 💵 Money Manager (2026-10-04)

# ─── ⏰ Routine email-response — core helpers (2026-10-01) ───────────────────
# 7 PM email se aaye jawab (✅ ho gaya / ❌ nahi hua + reason) server routine.json me seedha likhta hai, aur browser
# ka whole-blob save kabhi unhe overwrite na kare — isliye har jawab "rlog" me (k,id,d,s,why,t) log hota hai.
# Browser ka save aane par jo rlog entries browser ne abhi dekhi nahi, wo incoming blob par laga di jaati hain.
#   blob extra fields: miss:{date:{routineId:{why,t}}}, hmiss:{date:{habitId:{why,t}}}, todos[i].miss={d,why,t}, rlog:[...]
_ROUTINE_LOCK = threading.RLock()
_ROUTINE_RLOG_KEEP_SEC = 45 * 86400

def _routine_blank() -> dict:
    return {"v": 1, "routines": [], "done": {}, "todos": [], "habits": [], "hdone": {},
            "miss": {}, "hmiss": {}, "rlog": [], "t": 0,
            "practice": {}, "plog": {}, "pmiss": {}, "sunday": {},
            "rough": [], "info": [], "travel": [],
            "dipanshu": {}}   # DIPANSHU-STEP-1 (2026-10-03): 👦 Dipanshu tab ka data
    # MYENGINE-STEP-1 (2026-10-03): 📝 Rough / ℹ️ Info / ✈️ Travel tabs ka data (lists)

def _routine_norm(b: dict) -> dict:
    # MYENGINE-STEP-1 (2026-10-03): rough/info/travel bhi list hi hone chahiye (browser ke naye tabs ka data; server inhe sirf paas karta hai, badalta nahi)
    for k, dflt in (("routines", []), ("todos", []), ("habits", []), ("rlog", []), ("rough", []), ("info", []), ("travel", [])):
        if not isinstance(b.get(k), list):
            b[k] = list(dflt)
    for k in ("done", "hdone", "miss", "hmiss", "plog", "pmiss", "sunday"):
        if not isinstance(b.get(k), dict):
            b[k] = {}
    # DIPANSHU-STEP-1 (2026-10-03): dipanshu = dict {start, habits:[], done:{}, miss:{}} (unknown keys jaise start ko chhedna nahi)
    dp = b.get("dipanshu")
    if not isinstance(dp, dict):
        dp = b["dipanshu"] = {}
    if not isinstance(dp.get("habits"), list):
        dp["habits"] = []
    for k in ("done", "miss"):
        if not isinstance(dp.get(k), dict):
            dp[k] = {}
    return b

def _routine_read_disk():
    path = os.path.join(LOCAL_STATE_DIR, "routine.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return _routine_norm(d)
    except Exception as e:
        _slog_exception("_routine_read_disk", e)
        # BACKUP-1: main file corrupt / padh nahi payi -> pichhli .bak se padho (agla save main file ko theek kar deta hai)
        try:
            if os.path.exists(path + ".bak"):
                with open(path + ".bak", "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    _slog("[routine] routine.json corrupt tha — .bak se padha", level="warn")
                    return _routine_norm(d)
        except Exception as e2:
            _slog_exception("_routine_read_disk bak", e2)
    return None

def _routine_ekey(e: dict) -> str:
    return f"{e.get('k')}:{e.get('id')}:{e.get('d')}:{e.get('t')}"

# MYENGINE-STEP-10B (2026-10-03): 🤖 Telegram se likhe gaye items bhi rlog me jaate hain, k = "x". Shape:
#   {k:"x", id:<item id>, d:<aaj ki date>, s:"a"|"r"|"d"|"u", w:"t"|"g"|"i"|"v", t:<ms>, item:{...sirf s="a" par}}
#   w = list: t=todos, g=rough, i=info, v=travel.  s: a=item jodo, r=item hatao (undo of add), d=todo done, u=todo wapas open (undo of done).
# Apply idempotent hai (same id dobara jodna / hatana kuch nahi badalta), isliye browser-save merge aur panel-pull dono me safe.
# chart.html ka _rtApplyEntry iska JS twin hai.
_TG_LISTKEY = {"t": "todos", "g": "rough", "i": "info", "v": "travel"}

def _routine_apply_entry(b: dict, e: dict) -> None:
    """Ek email-response entry blob par lagao. k: r=routine, h=habit, t=todo. s: d=done, m=missed(+why)."""
    k, i, d, s = e.get("k"), str(e.get("id", "")), str(e.get("d", "")), e.get("s")
    why, t = str(e.get("why", ""))[:300], int(e.get("t") or 0)
    if not i or not d:
        return
    if k in ("r", "h"):
        dn, ms = ("done", "miss") if k == "r" else ("hdone", "hmiss")
        dm, mm = b.setdefault(dn, {}), b.setdefault(ms, {})
        if s == "d":
            dm.setdefault(d, {})[i] = 1
            if d in mm:
                mm[d].pop(i, None)
                if not mm[d]:
                    del mm[d]
        elif s == "m":
            if d in dm:
                dm[d].pop(i, None)
                if not dm[d]:
                    del dm[d]
            mm.setdefault(d, {})[i] = {"why": why, "t": t}
        elif s == "u":      # TGX: Telegram undo — done aur missed dono hatao (pending wapas)
            for _mp in (dm, mm):
                if d in _mp:
                    _mp[d].pop(i, None)
                    if not _mp[d]:
                        del _mp[d]
    elif k == "dp":
        # DIPANSHU-STEP-1 (2026-10-03): 👦 Dipanshu habit tick (idempotent) — blob.dipanshu.done / miss
        dp = b.setdefault("dipanshu", {})
        if not isinstance(dp, dict):
            dp = b["dipanshu"] = {}
        dm, mm = dp.setdefault("done", {}), dp.setdefault("miss", {})
        if s == "d":
            dm.setdefault(d, {})[i] = 1
            if d in mm:
                mm[d].pop(i, None)
                if not mm[d]:
                    del mm[d]
        elif s == "m":
            if d in dm:
                dm[d].pop(i, None)
                if not dm[d]:
                    del dm[d]
            mm.setdefault(d, {})[i] = {"why": why, "t": t}
        elif s == "u":      # TGX: undo
            for _mp in (dm, mm):
                if d in _mp:
                    _mp[d].pop(i, None)
                    if not _mp[d]:
                        del _mp[d]
    elif k == "p":
        # (2026-10-02) 🎯 Practice: n = us din ke sets (absolute), s=d (n>0) / m (n==0 + why)
        pl, pm = b.setdefault("plog", {}), b.setdefault("pmiss", {})
        try:
            n = float(e.get("n") or 0)
        except Exception:
            n = 0.0
        n = int(n) if n == int(n) else round(n, 1)
        if s == "d" and n > 0:
            pl.setdefault(d, {})[i] = n
            if d in pm:
                pm[d].pop(i, None)
                if not pm[d]:
                    del pm[d]
        elif s == "m":
            if d in pl:
                pl[d].pop(i, None)
                if not pl[d]:
                    del pl[d]
            pm.setdefault(d, {})[i] = {"why": why, "t": t}
        elif s == "u":      # TGX: undo
            for _mp in (pl, pm):
                if d in _mp:
                    _mp[d].pop(i, None)
                    if not _mp[d]:
                        del _mp[d]
    elif k == "t":
        for x in b.get("todos", []):
            if isinstance(x, dict) and str(x.get("id")) == i:
                if s == "d":
                    x["done"], x["doneAt"] = True, t
                    x.pop("miss", None)
                elif s == "m":
                    x["done"], x["doneAt"] = False, 0
                    x["miss"] = {"d": d, "why": why, "t": t}
                elif s == "u":      # TGX: undo
                    x["done"], x["doneAt"] = False, 0
                    x.pop("miss", None)
                break
    elif k == "s":
        # (2026-10-02) 🗓 Sunday check: id = "taskId:n". s=d -> done, s=m -> skip (+why). blob.sunday.tasks[i].done[n] = {d, s:"d"|"s"}
        tid, _sep, n = i.rpartition(":")
        if not tid or not n.isdigit():
            return
        sd = b.get("sunday")
        for x in (sd.get("tasks") if isinstance(sd, dict) and isinstance(sd.get("tasks"), list) else []):
            if isinstance(x, dict) and str(x.get("id")) == tid:
                if not isinstance(x.get("done"), dict):
                    x["done"] = {}
                if s == "d":
                    x["done"][n] = {"d": d, "s": "d"}
                elif s == "m":
                    x["done"][n] = {"d": d, "s": "s", "why": why}
                elif s == "u":      # TGX: undo
                    x["done"].pop(n, None)
                break
    elif k == "x":
        # MYENGINE-STEP-10B (2026-10-03): 🤖 Telegram write (upar _TG_LISTKEY ka comment dekho)
        lk = _TG_LISTKEY.get(str(e.get("w", "")))
        if not lk:
            return
        lst = b.get(lk)
        if not isinstance(lst, list):
            lst = b[lk] = []
        idx = next((j for j, x in enumerate(lst) if isinstance(x, dict) and str(x.get("id")) == i), -1)
        if s == "a":
            it = e.get("item")
            if idx < 0 and isinstance(it, dict) and str(it.get("id")) == i:
                lst.append(dict(it))
        elif s == "r":
            if idx >= 0:
                del lst[idx]
        elif s in ("d", "u") and lk == "todos" and idx >= 0:
            x = lst[idx]
            if s == "d":
                x["done"], x["doneAt"] = True, t
                x.pop("miss", None)
            else:
                x["done"], x["doneAt"] = False, 0

def _routine_merge_rlog(incoming: dict) -> dict:
    """Browser ka whole-blob save: disk rlog ki jo entries incoming ne dekhi nahi, wo incoming par laga do."""
    _routine_norm(incoming)
    disk = _routine_read_disk() or {}
    dl = disk.get("rlog") if isinstance(disk.get("rlog"), list) else []
    il = incoming["rlog"]
    cutoff = int((time.time() - _ROUTINE_RLOG_KEEP_SEC) * 1000)
    seen = {_routine_ekey(e) for e in il if isinstance(e, dict)}
    missing = [e for e in dl if isinstance(e, dict) and _routine_ekey(e) not in seen and int(e.get("t") or 0) >= cutoff]
    missing.sort(key=lambda e: int(e.get("t") or 0))
    for e in missing:
        _routine_apply_entry(incoming, e)
        il.append(e)
    incoming["rlog"] = [e for e in il if isinstance(e, dict) and int(e.get("t") or 0) >= cutoff]
    return incoming

def _local_state_file(kind: str) -> str:
    return os.path.join(LOCAL_STATE_DIR, f"{kind}.json")

def _local_state_load_all() -> dict:
    """Teeno kinds (state/settings/layout) disk se ek hi baar mein padh ke
    dict return karta hai — chart.html isko ek hi GET call mein pull karta
    hai (3 alag-alag GET calls ki jagah ek hi)."""
    out = {}
    for kind in _LOCAL_STATE_KINDS:
        path = _local_state_file(kind)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    out[kind] = json.load(f)
            else:
                out[kind] = None
        except Exception as e:
            _slog_exception(f"_local_state_load_all({kind})", e)
            out[kind] = None
    return out

def _local_state_mtimes() -> dict:
    """Teeno kinds (state/settings/layout) ki HF `/data` disk file ka last-
    modified time (epoch milliseconds) return karta hai — file exist nahi
    karti to None. Ye chart.html ko bataata hai ki server par kis kind ka
    data AAKHRI BAAR kab successfully likha gaya tha (asli disk write ka
    waqt, kisi in-memory ack ka nahi), taaki browser apne localStorage ke
    saved-at timestamp se compare karke tay kar sake ki restore ke liye
    HF ka data lena hai ya local ka (jo bhi zyada recent ho) — is se
    'HF ack pending ho to purana/stale server-data fresh local drawing ko
    overwrite kar deta hai' wala bug fix hota hai."""
    out = {}
    for kind in _LOCAL_STATE_KINDS:
        path = _local_state_file(kind)
        try:
            out[kind] = int(os.stat(path).st_mtime * 1000) if os.path.exists(path) else None
        except Exception as e:
            _slog_exception(f"_local_state_mtimes({kind})", e)
            out[kind] = None
    return out

def _local_state_save_raw(kind: str, data) -> tuple[bool, str]:
    """Ek kind ko atomically disk par save karta hai — pehle .tmp file mein
    poora likh ke phir os.replace() se rename karte hain, taaki beech mein
    process crash/restart ho jaaye to bhi purani file corrupt na ho, aur
    parallel save-requests aapas mein garbled na likh dein."""
    if kind not in _LOCAL_STATE_KINDS:
        return False, f"unknown kind: {kind}"
    path = _local_state_file(kind)
    # (2026-09-29) tmp naam ab unique (pid+thread) — WS executor me parallel saves (2 tabs) ek hi .tmp par na takraayein
    tmp  = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        payload = json.dumps(data)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        # BACKUP-1: naya likhne se pehle purani file ki .bak copy (routine / accounts / reminders) — file corrupt ho to .bak bachti hai
        if kind in ("routine", "accounts", "reminders", "moneymgr") and os.path.isfile(path) and os.path.getsize(path) > 2:
            try:   # sirf valid JSON ki copy — corrupt file achhi .bak ko overwrite na kare
                with open(path, "rb") as _fb:
                    _raw_old = _fb.read()
                json.loads(_raw_old.decode("utf-8"))
                with open(path + ".bak.tmp", "wb") as _fb2:
                    _fb2.write(_raw_old)
                os.replace(path + ".bak.tmp", path + ".bak")
            except Exception as _e_bak:
                _slog_exception(f"_local_state_save({kind}) bak", _e_bak)
        os.replace(tmp, path)
        if kind == "reminders":
            try:
                _get_reminder_wake().set()   # checker thread turant naya schedule dekhe
            except Exception:
                pass
        return True, f"{kind} saved ({len(payload)} bytes) -> {path}"
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        _slog_exception(f"_local_state_save({kind})", e)
        return False, str(e)

# ─── MYENGINE-STEP-7 (2026-10-03): 💰 Accounts — revision-checked save ────────────────────────────────────────
# Do device ek saath edit karein to purana device naye ko overwrite na kare. Har save payload me `rev` = wo revision
# jis par browser ne apna change banaya. Server:
#   incoming rev == disk rev  -> save, disk rev +1 (naya rev ack me wapas)
#   incoming rev <  disk rev  -> STALE: save refuse (ack me stale=True + server rev). Agar content wahi hai jo disk par
#                                pehle se hai (ack kho gaya tha, resend aaya) to refuse nahi, "already saved" maano.
#   incoming rev >  disk rev  -> (server file purani/gayab) save, rev = incoming+1
_ACCOUNTS_LOCK = threading.RLock()

def _accounts_read_disk():
    path = _local_state_file("accounts")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception as e:
        _slog_exception("_accounts_read_disk", e)
    return None

def _accounts_core(d: dict) -> str:
    """Content compare ke liye: rev aur t hata kar stable JSON string."""
    return json.dumps({k: v for k, v in d.items() if k not in ("rev", "t")}, sort_keys=True)

def _accounts_save(data) -> tuple:
    """-> (ok, msg, rev, stale). rev = disk par abhi ka revision (ok ho to naya, stale ho to server wala)."""
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list) or not isinstance(data.get("entries"), list):
        return False, "accounts payload galat hai (accounts/entries list chahiye)", None, False
    try:
        inrev = max(0, int(data.get("rev") or 0))
    except Exception:
        inrev = 0
    with _ACCOUNTS_LOCK:
        disk = _accounts_read_disk()
        try:
            drev = max(0, int((disk or {}).get("rev") or 0))
        except Exception:
            drev = 0
        if disk is not None and inrev < drev:
            if _accounts_core(disk) == _accounts_core(data):
                return True, f"accounts already saved (rev {drev})", drev, False
            return False, f"STALE: server par rev {drev} hai, aapke paas rev {inrev} — pehle naya data lo", drev, True
        if disk is not None and inrev == drev and _accounts_core(disk) == _accounts_core(data):
            return True, f"accounts unchanged (rev {drev})", drev, False
        newrev = max(inrev, drev) + 1
        out = dict(data)
        out["rev"] = newrev
        ok, msg = _local_state_save_raw("accounts", out)
        return ok, msg, (newrev if ok else drev), False

@_ws_action("accounts_pull")
def _wsa_accounts_pull(data):
    """Stale refusal ke baad browser 'Naya data lo' dabata hai -> server ka accounts.json wapas bhejo."""
    with _ACCOUNTS_LOCK:
        d = _accounts_read_disk()
    return {"ok": True, "data": d}

def _local_state_save(kind: str, data) -> tuple[bool, str]:
    """(2026-10-01) Wrapper: kind='routine' par email-responses (rlog) merge karke hi likhta hai, baaki kinds jaisa pehle."""
    if kind == "accounts":
        _ok_a, _msg_a, _rev_a, _stale_a = _accounts_save(data)   # MYENGINE-STEP-7: revision check
        return _ok_a, _msg_a
    if kind == "moneymgr" and isinstance(data, dict):     # MM-TG (2026-10-04): Telegram ke changes purane phone se mit na jaayein
        try:
            with _MM_LOCK:
                data = _mm_merge_incoming(data)
                return _local_state_save_raw(kind, data)
        except Exception as e:
            _slog_exception("_local_state_save(moneymgr merge)", e)
    if kind == "routine" and isinstance(data, dict):
        try:
            with _ROUTINE_LOCK:
                data = _routine_merge_rlog(data)
                return _local_state_save_raw(kind, data)
        except Exception as e:
            _slog_exception("_local_state_save(routine merge)", e)
    return _local_state_save_raw(kind, data)

# ─── (2026-09-29) DIRECT WS action: "local_state_save" ───────────────────────
# chart.html seedha wsCall("local_state_save", {kind, seq, payload}) bhejta hai.
# Hub ise executor thread me chalata hai (blocking disk write theek). Reply = ack
# {kind, ok, msg, ts, seq, persistent}. Save ka sirf yahi ek raasta hai.
@_ws_action("local_state_save")
def _wsa_local_state_save(data):
    d = data if isinstance(data, dict) else {}
    _k, _seq, _pl = d.get("kind", ""), d.get("seq"), d.get("payload")
    _persistent = bool(str(LOCAL_STATE_DIR).startswith("/data"))
    if not _k or _pl is None:
        return {"kind": _k, "ok": False, "seq": _seq, "ts": time.time(),
                "msg": "missing kind/payload", "persistent": _persistent}
    if _k == "accounts":
        # MYENGINE-STEP-7 (2026-10-03): accounts ack me naya `rev` (ya stale refusal ka server `rev` + stale=True) bhi jaata hai
        _ok, _msg, _rev, _stale = _accounts_save(_pl)
        _slog(f"[ws] local_state_save kind='accounts' seq={_seq} ok={_ok} stale={_stale} rev={_rev} msg={_msg}", level="ok" if _ok else "err")
        return {"kind": _k, "ok": _ok, "msg": _msg, "ts": time.time(), "seq": _seq, "persistent": _persistent,
                "rev": _rev, "stale": _stale}
    _ok, _msg = _local_state_save(_k, _pl)
    _slog(f"[ws] local_state_save kind={_k!r} seq={_seq} ok={_ok} msg={_msg}", level="ok" if _ok else "err")
    return {"kind": _k, "ok": _ok, "msg": _msg, "ts": time.time(), "seq": _seq, "persistent": _persistent}

# ─── Reminders (🔔) — background email-alert checker (2026-09-25) ──────────
# Items browser se save hote hain (kind="reminders", jaisa diary/state/
# settings/layout, upar _LOCAL_STATE_KINDS/_local_state_save se hi hota hai,
# koi naya kind nahi chahiye). Ye process-wide daemon thread (koi
# bhi browser tab khula ho ya na ho, jab tak HF Space process zinda hai)
# har 60s reminders.json check karta hai aur match milne par Brevo email
# bhejta hai (_alert_via_brevo — same channel jo Fyers/Binance alerts use
# karte hain, upar dekho). Duplicate-send se bachne ke liye alag disk file
# (reminders_sent.json) me record rakhte hain — in-memory nahi, taaki
# process restart ke baad bhi wahi din dobara email na bheje.
_REMINDERS_SENT_FILE = os.path.join(LOCAL_STATE_DIR, "reminders_sent.json")
_REMINDERS_SENT_LOCK = threading.Lock()
# (2026-09-25, updated) Default alert-schedule: event se 1 din pehle 19:00
# (7 PM) par pehla alert, event wale din 07:00 (7 AM) par doosra alert.
# Har reminder isi default ke saath ADD hota hai (chart.html form me
# already 19:00/07:00 bhara hota hai) lekin PER-REMINDER change bhi ho
# sakta hai — item me apna "time_before"/"time_same" save hota hai, checker
# yahan wahi padhta hai (purane items jinme ye fields na hon, unke liye
# default fallback).
_REMINDER_DEFAULT_TIME_BEFORE = "19:00"
_REMINDER_DEFAULT_TIME_SAME   = "07:00"

def _reminders_sent_load() -> dict:
    try:
        if os.path.exists(_REMINDERS_SENT_FILE):
            with open(_REMINDERS_SENT_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
    except Exception as e:
        _slog_exception("_reminders_sent_load", e)
    return {}

def _reminders_sent_save(d: dict) -> None:
    tmp = _REMINDERS_SENT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, _REMINDERS_SENT_FILE)
    except Exception as e:
        _slog_exception("_reminders_sent_save", e)

def _reminder_anchor_dates(item: dict, today: "datetime.date") -> list:
    """Item ka asli event-date (ya recurring ke liye candidates) return
    karta hai. Recurring me sirf 'MM-DD' store hota hai, isliye pichhle/is/
    agle saal — teeno candidates banate hain, taaki year-boundary (jaise
    1 Jan ka -1-din offset pichhle saal ke 31 Dec par jaata hai) sahi se
    handle ho bina kisi special-case ke (neeche offset check dono type ke
    liye same tarike se chalta hai)."""
    try:
        if item.get("type") == "every":
            # (2026-10-06) Period tab: 'YYYY-MM-DD' = pichhli period ki date, every_days = cycle. Event = date + k*N (k>=1).
            # Aaj ke aas-paas ke 3-4 candidates (alert event se 1 din pehle jata hai).
            if not str(item.get("date") or "").strip():
                return []   # (v2) history khaali — abhi koi event nahi
            y, m, d = [int(x) for x in str(item.get("date", "")).split("-")]
            n = int(item.get("every_days"))
            if n < 2 or n > 400:
                return []
            base = datetime.date(y, m, d)
            k0 = (today - base).days // n
            return [base + datetime.timedelta(days=k * n) for k in range(max(1, k0 - 1), k0 + 3)]
        if item.get("type") == "once":
            y, m, d = [int(x) for x in str(item.get("date", "")).split("-")]
            return [datetime.date(y, m, d)]
        else:
            m, d = [int(x) for x in str(item.get("date", "")).split("-")]
            out = []
            for yy in (today.year - 1, today.year, today.year + 1):
                try:
                    out.append(datetime.date(yy, m, d))
                except ValueError:
                    if (m, d) == (2, 29):
                        out.append(datetime.date(yy, 2, 28))   # 29 Feb non-leap saal me 28 Feb par
            return out
    except Exception:
        return []

# ── Smart scheduler (2026-09-25) ────────────────────────────────────────────
# Pehle: har 60s par 5 state files padhna + exact-minute match (miss hone par alert
# hamesha ke liye gaya). Ab:
#   1) sirf reminders.json padhte hain, wo bhi tab jab file ka mtime badla ho (cache)
#   2) agla due-time nikaal ke tab tak so jaate hain (idle = ~0 kaam), max 30 min
#      safety-wake; reminders save hone par _REMINDER_WAKE se turant jaagte hain
#   3) GRACE window: target time ke baad _REMINDER_GRACE_SEC tak kabhi bhi bhej do
#      (restart / sleep / Brevo fail ke baad late alert aa jaata hai, miss nahi hota)
#   4) email fail -> _REMINDER_RETRY_SEC baad dobara try (grace window ke andar)
_REMINDER_GRACE_SEC     = 6 * 3600   # target ke baad itni der tak late-send allowed
_REMINDER_MAX_SLEEP_SEC = 30 * 60    # safety wake (clock jump / missed signal ke liye)
_REMINDER_RETRY_SEC     = 300        # fail hone par retry gap
_REMINDER_FILE_CACHE    = {"mtime": None, "items": []}

def _reminder_load_items() -> list:
    """Sirf reminders.json padhta hai; file badli na ho to cache se deta hai."""
    path = _local_state_file("reminders")
    try:
        mt = os.stat(path).st_mtime if os.path.exists(path) else None
    except Exception:
        mt = None
    if mt is None:
        _REMINDER_FILE_CACHE.update(mtime=None, items=[])
        return []
    if mt == _REMINDER_FILE_CACHE["mtime"]:
        return _REMINDER_FILE_CACHE["items"]
    items = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("items") if isinstance(data, dict) else None
        items = raw if isinstance(raw, list) else []
    except Exception as e:
        _slog_exception("_reminder_load_items", e)
        return _REMINDER_FILE_CACHE["items"]   # parse fail: purana cache, mtime update nahi (retry)
    _REMINDER_FILE_CACHE.update(mtime=mt, items=items)
    return items

def _reminder_parse_hhmm(s: str, fallback: str) -> "datetime.time":
    for cand in (s, fallback):
        try:
            hh, mm = [int(x) for x in str(cand).strip().split(":")[:2]]
            return datetime.time(hh, mm)
        except Exception:
            continue
    return datetime.time(7, 0)

_REMINDER_KIND_ICON = {"birthday": "\U0001F382", "anniversary": "\U0001F48D"}

def _reminder_age_info(it: dict, anchor: "datetime.date"):
    """Birthday/Anniversary ke liye (saal, total_din, origin_date) — chart.html me
    'origin_year' (janam/shaadi ka saal) save hota hai. Saal na ho / kind alag ho /
    origin future me ho to None (email me age line nahi aati)."""
    try:
        if str(it.get("kind") or "") not in ("birthday", "anniversary"):
            return None
        if it.get("type") == "once":
            return None
        oy = int(it.get("origin_year"))
        m, d = [int(x) for x in str(it.get("date")).split("-")]
        years = anchor.year - oy
        if years < 0:
            return None
        try:
            origin = datetime.date(oy, m, d)
        except ValueError:
            origin = datetime.date(oy, 2, 28)
        return years, (anchor - origin).days, origin
    except Exception:
        return None

def _reminder_targets(items: list, now: "datetime.datetime") -> list:
    """Sab (target_dt, sent_key, subj, msg) — pichhle/is/agle saal ke anchors ke liye.
    sent_key = rid:<target-date>:<offset> (purane format jaisa, target-date hi use hota
    hai kyunki grace window midnight cross kar sakti hai)."""
    out = []
    today = now.date()
    for it in items:
        try:
            if not isinstance(it, dict):
                continue
            rid = str(it.get("id") or "")
            if not rid:
                continue
            label = str(it.get("label") or "Reminder")
            t_before = _reminder_parse_hhmm(it.get("time_before"), _REMINDER_DEFAULT_TIME_BEFORE)
            t_same   = _reminder_parse_hhmm(it.get("time_same"),   _REMINDER_DEFAULT_TIME_SAME)
            _ev_cfg = it.get("cfg") if (it.get("type") == "every" and isinstance(it.get("cfg"), dict)) else None
            if _ev_cfg is not None:   # (2026-10-06) Period tab v2: user-configurable alerts (2 din pehle / 1 din pehle / expected din / agle din follow-up)
                schedule = tuple(
                    (_off, _reminder_parse_hhmm(_ev_cfg.get(_tk), _dt), _wh)
                    for _off, _ok, _tk, _dt, _wh in (
                        (2, "o2", "t2", "19:00", "2 din pehle"),
                        (1, "o1", "t1", "19:00", "KAL (1 din pehle)"),
                        (0, "o0", "t0", "07:00", "AAJ"),
                        (-1, "of", "tf", "10:00", "FOLLOW-UP"),
                    ) if _ev_cfg.get(_ok)
                )
            elif it.get("only_before"):   # Period tab (v1): sirf 1 din pehle wala alert
                schedule = ((1, t_before, "KAL (1 din pehle)"),)
            else:
                schedule = ((1, t_before, "KAL (1 din pehle)"), (0, t_same, "AAJ"))
            for anchor in _reminder_anchor_dates(it, today):
                for off, t, when in schedule:
                    tdate = anchor - datetime.timedelta(days=off)
                    tdt = datetime.datetime.combine(tdate, t, tzinfo=IST)
                    key = f"{rid}:{tdate.isoformat()}:{off}"
                    kind = str(it.get("kind") or "")
                    icon = _REMINDER_KIND_ICON.get(kind, "")
                    title = f"{icon} {label}" if icon else label
                    info = _reminder_age_info(it, anchor)
                    subj = f"[Reminder] {title} \u2014 {when}"
                    lines = [title, "", f"Event date: {anchor.isoformat()}"]
                    if info:
                        yrs, days, origin = info
                        subj += f" ({yrs} saal)"
                        if kind == "anniversary":
                            lines.append(f"Total saal: {yrs} saal ({days:,} din)")
                            lines.append(f"Shuruaat: {origin.strftime('%d %b %Y')}")
                        else:
                            lines.append(f"Total age: {yrs} saal ({days:,} din)")
                            lines.append(f"Janam: {origin.strftime('%d %b %Y')}")
                        lines.append(f"Kal {yrs} saal poore ho rahe hain." if off == 1
                                     else f"Aaj {yrs} saal poore hue. \U0001F389")
                    lines.append(f"Alert: {tdate.isoformat()} {t.strftime('%H:%M')} ({when})")
                    msg = "\n".join(lines)
                    if it.get("plain"):   # Period tab: alert me sirf wahi text jo user ne likha (label)
                        _txt = title
                        if _ev_cfg is not None:
                            if off == -1:
                                _txt = str(_ev_cfg.get("ftext") or "Kal period expected thi — hui kya? App me log kar do.")
                            elif off == 2:
                                _txt = f"{title} — 2 din baad"
                            elif off == 0:
                                _txt = f"{title} — aaj"
                        subj = _txt
                        msg = _txt
                    out.append((tdt, key, subj, msg))
        except Exception as _e_item:
            _slog_exception("_reminder_targets (item)", _e_item)
    return out

def _reminder_check_loop():
    wake = _get_reminder_wake()
    while True:
        sleep_for = _REMINDER_MAX_SLEEP_SEC
        try:
            items = _reminder_load_items()
            if items:
                now = _ist_now()
                targets = _reminder_targets(items, now)
                with _REMINDERS_SENT_LOCK:
                    sent = _reminders_sent_load()
                    dirty = False
                    for tdt, key, subj, msg in sorted(targets, key=lambda x: x[0]):
                        if sent.get(key):
                            continue
                        age = (now - tdt).total_seconds()
                        if age < 0:
                            # future target: sleep sirf itni der (agar sabse nazdeek ho)
                            sleep_for = min(sleep_for, max(1.0, -age))
                            continue
                        if age > _REMINDER_GRACE_SEC:
                            continue   # bahut late — skip (grace se bahar)
                        res = _alert_email_and_telegram(subj, msg)
                        late = f", {int(age // 60)} min late" if age >= 120 else ""
                        _slog(f"[reminder] {key} email -> {res}{late}",
                              level="ok" if res == "ok" else "err")
                        if res == "ok":
                            sent[key] = time.time()
                            dirty = True
                        else:
                            sleep_for = min(sleep_for, _REMINDER_RETRY_SEC)
                    if dirty:
                        cutoff = time.time() - 30 * 86400   # 30 din se purani entries prune
                        sent = {k: v for k, v in sent.items() if v >= cutoff}
                        _reminders_sent_save(sent)
        except Exception as e:
            _slog_exception("_reminder_check_loop", e)
            sleep_for = 60
        # Sone se pehle wake-flag clear, phir wait — save-signal ya timeout par jaagta hai
        wake.clear()
        wake.wait(timeout=max(1.0, sleep_for))

def _ensure_reminder_thread():
    names = {t.name for t in threading.enumerate()}
    if "ReminderChecker" not in names:
        threading.Thread(target=_reminder_check_loop, name="ReminderChecker", daemon=True).start()

_ensure_reminder_thread()

# ─── ⏰ Routine — roz shaam 7 PM email + jawab-page (2026-10-01) ───────────────
# Flow: 19:00 IST par server email bhejta hai (aaj ke Routine + To-Do + Habit, har ek ke saath status) + ek link.
# Link /api/routine_resp?d=<date>&tok=<hmac> ek page kholta hai: har kaam ke 2 button ✅ Ho gaya / ❌ Nahi hua
# (❌ par reason box, zaroori). Submit → POST /api/routine_resp → routine.json update (rlog me entry) →
# browser ⏰ panel kholte hi `routine_pull` se wo entries utha leta hai.
# Secrets (HF Space): ROUTINE_LINK_SECRET (optional; na ho to BREVO_API_KEY se token banta hai; Render ka koi use nahi),
# ROUTINE_BASE_URL (optional; na ho to SPACE_HOST env se https://<SPACE_HOST>). Email Brevo se (BREVO_API_KEY/ALERT_EMAIL_TO).
_ROUTINE_EMAIL_HOUR, _ROUTINE_EMAIL_MIN = 19, 0
_ROUTINE_EMAIL_GRACE_SEC = 3 * 3600          # 7 PM ke baad itni der tak late-send allowed (Space so raha ho to)
_ROUTINE_SENT_FILE = os.path.join(LOCAL_STATE_DIR, "routine_email_sent.json")
_ROUTINE_WEEKDAYS = ["Ravivaar", "Somvaar", "Mangalvaar", "Budhvaar", "Guruvaar", "Shukravaar", "Shanivaar"]  # JS getDay() order
_ROUTINE_SECS = (("r", "🌅 Routine"), ("t", "✅ To-Do"), ("h", "🔁 Habit"), ("p", "🎯 Practice"), ("dp", "👦 Dipanshu"), ("o", "🏖 Holiday"))   # tab order  # DIPANSHU-STEP-1 (2026-10-03)
# (2026-10-02) Shaam ki report + jawab-page me Sunday section bhi (subah ka Aaj ka Plan _ROUTINE_SECS hi use karta hai — uska apna Sunday section hai)
_ROUTINE_SECS_EVE = _ROUTINE_SECS + (("s", "🗓 Sunday"),)
_ROUTINE_ICON = {"d": "✅", "m": "❌", "": "⬜"}

def _routine_secret() -> str:
    return (_get_secret("ROUTINE_LINK_SECRET").strip() or BREVO_API_KEY or "")

def _routine_tok(d: str) -> str:
    sec = _routine_secret()
    if not sec:
        return ""
    return hmac.new(sec.encode("utf-8"), f"routine-resp:{d}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]

def _routine_tok_ok(d: str, tok: str) -> bool:
    """Token sahi ho AUR date aaj ya kal (IST) ki ho — purani email ka link expire."""
    try:
        dd = datetime.date.fromisoformat(str(d))
        today = _ist_now().date()
        if not (today - datetime.timedelta(days=1) <= dd <= today):
            return False
        exp = _routine_tok(str(d))
        return bool(exp) and hmac.compare_digest(exp.encode("utf-8"), str(tok or "").encode("utf-8"))
    except Exception:
        return False

def _routine_base_url() -> str:
    u = _get_secret("ROUTINE_BASE_URL").strip().rstrip("/")
    if u:
        return u
    h = (os.environ.get("SPACE_HOST") or "").strip()
    return f"https://{h}" if h else ""

# ─── 🎯 Practice helpers (2026-10-02) ───────────────────────────────────────
# blob.practice = {lifetime:"...", steps:[{id,label,monthly,created}]}, blob.plog = {date:{stepId:sets}}, blob.pmiss = {date:{stepId:{why,t}}}
# 1 mahina = 28 din (4 hafte), weekly = monthly/4, daily = weekly/7 (60 -> 15 -> ~2.14). Har step ki apni START DATE hai:
# hafta/mahina usi din se gine jaate hain (din 1-7 = hafta 1, din 1-28 = mahina 1), step start se pehle kuch count/alert nahi.
def _practice_steps(b: dict) -> list:
    p = b.get("practice")
    st = p.get("steps") if isinstance(p, dict) else []
    return [x for x in (st if isinstance(st, list) else []) if isinstance(x, dict) and x.get("id") is not None]

def _prac_start(st: dict) -> datetime.date:
    """Step ki start date (YYYY-MM-DD). Purane step (start nahi) => bani hui date."""
    try:
        return datetime.date.fromisoformat(str(st.get("start") or ""))
    except Exception:
        pass
    try:
        ms = int(st.get("created") or 0)
        if ms > 0:
            return datetime.datetime.fromtimestamp(ms / 1000, IST).date()
    except Exception:
        pass
    return _ist_now().date()

def _prac_targets(st: dict):
    try:
        m = float(st.get("monthly") or 0)
    except Exception:
        m = 0.0
    return m, m / 4.0, m / 28.0

def _fmt_n(x) -> str:
    try:
        x = float(x)
    except Exception:
        return "0"
    return str(int(round(x))) if abs(x - round(x)) < 0.05 else f"{x:.1f}"

def _prac_sum(b: dict, sid: str, d0: datetime.date, d1: datetime.date) -> float:
    pl, tot, d = (b.get("plog") or {}), 0.0, d0
    while d <= d1:
        try:
            tot += float((pl.get(d.isoformat()) or {}).get(sid) or 0)
        except Exception:
            pass
        d += datetime.timedelta(days=1)
    return tot

def _routine_status(done: dict, miss: dict, iid: str):
    if done.get(iid):
        return "d", ""
    m = miss.get(iid)
    if isinstance(m, dict):
        return "m", str(m.get("why", ""))
    return "", ""

def _dipc_items(b: dict, d: str) -> list:
    """DIPANSHU-STEP-1 (2026-10-03): 👦 Dipanshu ke us din (d) ke habit items (contract E2). Sirf scheduled weekday,
    d >= created (IST date) aur d >= dipanshu.start (valid ho to)."""
    dp = b.get("dipanshu")
    if not isinstance(dp, dict):
        return []
    dd = datetime.date.fromisoformat(d)
    dow = (dd.weekday() + 1) % 7
    try:
        st0 = datetime.date.fromisoformat(str(dp.get("start") or ""))
    except Exception:
        st0 = None
    if st0 and dd < st0:
        return []
    done_d = (dp.get("done") or {}).get(d, {}) if isinstance(dp.get("done"), dict) else {}
    miss_d = (dp.get("miss") or {}).get(d, {}) if isinstance(dp.get("miss"), dict) else {}
    hs = []
    for h in (dp.get("habits") if isinstance(dp.get("habits"), list) else []):
        if not isinstance(h, dict) or dow not in (h.get("days") or []):
            continue
        try:
            cd = datetime.datetime.fromtimestamp(int(h.get("created") or 0) / 1000, IST).date()
        except Exception:
            cd = None
        if cd and dd < cd:
            continue
        hs.append(h)
    hs.sort(key=lambda h: ((h.get("time") or "99:99"), h.get("created") or 0))
    out = []
    for h in hs:
        iid = str(h.get("id"))
        s, why = _routine_status(done_d, miss_d, iid)
        out.append({"k": "dp", "id": iid, "label": str(h.get("label", "")),
                    "sub": ("⏰ " + str(h["time"])) if h.get("time") else "", "s": s, "why": why})
    return out

def _routine_collect(b: dict, d: str, sunday: bool = False, dip: bool = False) -> list:
    """Din `d` ke teeno tabs ke items: [{k,id,label,sub,s,why}] — tab order Routine, To-Do, Habit.
    sunday=True (sirf shaam ki report/jawab-page): 🗓 Sunday items bhi jodta hai (k="s", id="taskId:n")."""
    out = []
    dd = datetime.date.fromisoformat(d)
    dow = (dd.weekday() + 1) % 7          # python Mon=0 → JS Sun=0
    done, miss = (b.get("done") or {}).get(d, {}), (b.get("miss") or {}).get(d, {})
    rs = [r for r in b.get("routines", []) if isinstance(r, dict) and dow in (r.get("days") or [])]
    rs.sort(key=lambda r: ((r.get("time") or "99:99"), r.get("created") or 0))
    for r in rs:
        iid = str(r.get("id"))
        s, why = _routine_status(done, miss, iid)
        out.append({"k": "r", "id": iid, "label": str(r.get("label", "")),
                    "sub": ("⏰ " + str(r["time"])) if r.get("time") else "", "s": s, "why": why})
    tds = []
    for x in b.get("todos", []):
        if not isinstance(x, dict):
            continue
        if x.get("done"):
            try:
                da = datetime.datetime.fromtimestamp(int(x.get("doneAt") or 0) / 1000, IST).date().isoformat()
            except Exception:
                da = ""
            if da != d:
                continue
        elif x.get("due") and str(x.get("due")) > d:
            continue
        tds.append(x)
    tds.sort(key=lambda x: (str(x.get("due") or "9999-12-31"), x.get("pri") or 2, x.get("created") or 0))
    for x in tds:
        m = x.get("miss") if isinstance(x.get("miss"), dict) else {}
        s = "d" if x.get("done") else ("m" if m.get("d") == d else "")
        pri = {1: "🔴", 2: "🟡", 3: "🟢"}.get(x.get("pri"), "🟡")
        out.append({"k": "t", "id": str(x.get("id")), "label": str(x.get("text", "")),
                    "sub": pri + ((" · due " + str(x["due"])) if x.get("due") else ""),
                    "s": s, "why": str(m.get("why", "")) if s == "m" else ""})
    hdone, hmiss = (b.get("hdone") or {}).get(d, {}), (b.get("hmiss") or {}).get(d, {})
    hs = [h for h in b.get("habits", []) if isinstance(h, dict) and dow in (h.get("days") or [])]
    hs.sort(key=lambda h: h.get("created") or 0)
    for h in hs:
        iid = str(h.get("id"))
        s, why = _routine_status(hdone, hmiss, iid)
        out.append({"k": "h", "id": iid, "label": str(h.get("label", "")), "sub": "", "s": s, "why": why})
    # 🎯 Practice (roz) — s: d = sets logged (>0), m = 0 sets + reason
    pl_d, pm_d = (b.get("plog") or {}).get(d, {}), (b.get("pmiss") or {}).get(d, {})
    for st in _practice_steps(b):
        S0 = _prac_start(st)
        if dd < S0:
            continue                                   # step abhi start nahi hua
        wk0 = S0 + datetime.timedelta(days=7 * ((dd - S0).days // 7))
        sid = str(st.get("id"))
        _m, w_t, d_t = _prac_targets(st)
        try:
            n = float(pl_d.get(sid) or 0)
        except Exception:
            n = 0.0
        if n > 0:
            s_, why, nn = "d", "", n
        elif isinstance(pm_d.get(sid), dict):
            s_, why, nn = "m", str(pm_d[sid].get("why", "")), 0
        else:
            s_, why, nn = "", "", None
        wk = _prac_sum(b, sid, wk0, dd)
        sub = f"aaj {_fmt_n(n)} sets · target {d_t:.1f}/din · hafte me {_fmt_n(wk)}/{_fmt_n(w_t)}"
        out.append({"k": "p", "id": sid, "label": str(st.get("label", "Practice")), "sub": sub, "s": s_, "why": why, "n": nn})
    if dip:   # DIPANSHU-STEP-1 (2026-10-03): Practice ke baad, Sunday se pehle
        try:
            out += _dipc_items(b, d)
        except Exception as e:
            _slog_exception("_routine_collect dipanshu", e)
    if sunday:
        try:
            out += _sun_eve_items(b, d)
        except Exception as e:
            _slog_exception("_routine_collect sunday", e)
    try:   # HOLIDAY (2026-10-04): holiday ke din ke extra kaam (k="o") — plan, shaam ki checklist, email, report sab me
        out += _hol_items(d)
    except Exception as e:
        _slog_exception("_routine_collect holiday", e)
    return out

def _routine_email_build(items: list, d: str, url: str):
    import html as _hm
    dd = datetime.date.fromisoformat(d)
    wk = _ROUTINE_WEEKDAYS[(dd.weekday() + 1) % 7]
    n, nd = len(items), sum(1 for i in items if i["s"] == "d")
    subj = f"Aaj ka hisaab - {nd}/{n} done ({dd.day}/{dd.month})"   # plain subject (emoji nahi) — alerts jaisa, deliverability ke liye
    tl, hl = [f"{dd.day}/{dd.month} · {wk} — {nd}/{n} kaam ho chuke.", ""], []
    for sec, title in _ROUTINE_SECS_EVE:
        its = [i for i in items if i["k"] == sec]
        if not its:
            continue
        tl.append(title)
        hl.append(f'<div style="font-weight:700;margin:14px 0 4px">{_hm.escape(title)}</div>')
        for i in its:
            sub = f" ({i['sub']})" if i["sub"] else ""
            why = f" — {i['why']}" if i["s"] == "m" and i["why"] else ""
            tl.append(f"  {_ROUTINE_ICON[i['s']]} {i['label']}{sub}{why}")
            hl.append(f'<div style="padding:3px 0">{_ROUTINE_ICON[i["s"]]} {_hm.escape(i["label"])}'
                      f'<span style="color:#888"> {_hm.escape(sub)}{_hm.escape(why)}</span></div>')
        tl.append("")
    if url:
        tl += ["Har kaam ke liye ✅ ho gaya / ❌ nahi hua (reason ke saath) batane ke liye ye link kholo:", url]
    else:
        tl += ["(Ye test email hai — isme jaanboojhkar koi link nahi hai.)"]
    html = ('<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:480px">'
            f'<div style="font-size:16px;font-weight:700">⏰ Aaj ka hisaab</div>'
            f'<div style="color:#888">{dd.day}/{dd.month} · {wk} — {nd}/{n} kaam ho chuke</div>'
            + "".join(hl) +
            f'<div style="margin:18px 0 6px"><a href="{_hm.escape(url)}" style="background:#26a69a;color:#fff;'
            'padding:11px 20px;border-radius:6px;text-decoration:none;font-weight:700;display:inline-block">'
            '✅ / ❌ Jawab do</a></div>'
            '<div style="color:#888;font-size:12px">Link sirf aaj-kal ke liye chalta hai.</div></div>')
    return subj, "\n".join(tl), html

def _routine_send_email(d: str, force: bool = False, nolink: bool = False):
    """(status, msg) — status 'ok' | 'skip' | 'err'. force=True: sab done ho tab bhi bhejo (test button).
    nolink=True: TEST — wahi list, par email me koi link nahi (Gmail link ki wajah se rok raha hai ya nahi, ye jaanchne ke liye)."""
    base = ""
    if not nolink:
        if not _routine_secret():
            return "err", "link secret nahi (ROUTINE_LINK_SECRET ya BREVO_API_KEY set karo)"
        base = _routine_base_url()
        if not base:
            return "err", "base URL nahi (ROUTINE_BASE_URL ya SPACE_HOST)"
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    items = _routine_collect(b, d, sunday=True, dip=True)   # DIPANSHU-STEP-1 (2026-10-03)
    if not items:
        return "skip", "aaj ke liye koi kaam nahi"
    if not force and all(i["s"] == "d" for i in items):
        return "skip", "sab kaam pehle se done"
    subj, text, html = _routine_email_build(items, d, "" if nolink else f"{base}/api/routine_resp?d={d}&tok={_routine_tok(d)}")
    if nolink:
        subj = subj.replace("Aaj ka hisaab", "Aaj ka hisaab TEST (bina link)", 1)
    # (2026-10-03) Raat 7 PM: email SIRF email par (HF link wala text Telegram par kabhi nahi jaata).
    # Telegram par sirf ek message: ✅/❌ buttons wali checklist (_tgx_send_checklist).
    res = _alert_via_brevo(subj, text)        # plain-text email (HTML Brevo me atak raha tha, 2026-10-01)
    if nolink:                                # test button: sirf email, Telegram par kuch nahi
        return ("ok", "email gayi") if res == "ok" else ("err", res)
    kb = "skip"
    for _try in range(3):                     # relay so raha ho to 2-3 baar koshish (email dobara nahi jaati)
        try:
            kb = _tgx_send_checklist(d)
        except Exception as _e_kb:
            _slog_exception("routine buttons", _e_kb)
            kb = f"err: {type(_e_kb).__name__}: {_e_kb}"
        if kb in ("ok", "skip"):
            break
        time.sleep(20)
    _slog(f"[routine-buttons] {d} -> {kb} (email={res})", level="ok" if kb in ("ok", "skip") else "warn")
    if res == "ok" or kb == "ok":
        return "ok", ("email + Telegram checklist gayi" if res == "ok" and kb == "ok"
                      else "email gayi" if res == "ok" else "Telegram checklist gayi (email: " + str(res)[:80] + ")")
    return "err", f"email: {res} | telegram: {kb}"

def _routine_sent_load(path: str = _ROUTINE_SENT_FILE) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                x = json.load(f)
            return x if isinstance(x, dict) else {}
    except Exception as e:
        _slog_exception("_routine_sent_load", e)
    return {}

def _routine_sent_save(x: dict, path: str = _ROUTINE_SENT_FILE) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(x, f)
        os.replace(tmp, path)
    except Exception as e:
        _slog_exception("_routine_sent_save", e)

def _routine_email_loop():
    while True:
        sleep_for = 1800
        try:
            now = _ist_now()
            target = now.replace(hour=_ROUTINE_EMAIL_HOUR, minute=_ROUTINE_EMAIL_MIN, second=0, microsecond=0)
            age = (now - target).total_seconds()
            d = now.date().isoformat()
            if age < 0:
                sleep_for = min(1800, max(5.0, -age + 1))
            elif age <= _ROUTINE_EMAIL_GRACE_SEC:
                sent = _routine_sent_load()
                if not sent.get(d):
                    st_, msg = _routine_send_email(d)
                    _slog(f"[routine-email] {d} -> {st_}: {msg}", level="ok" if st_ != "err" else "err")
                    if st_ == "err":
                        sleep_for = _REMINDER_RETRY_SEC
                    else:
                        sent[d] = {"ts": time.time(), "res": st_}
                        cut = (now.date() - datetime.timedelta(days=30)).isoformat()
                        _routine_sent_save({k: v for k, v in sent.items() if k >= cut})
        except Exception as e:
            _slog_exception("_routine_email_loop", e)
            sleep_for = 60
        time.sleep(sleep_for)

def _ensure_routine_email_thread():
    if "RoutineEmail" not in {t.name for t in threading.enumerate()}:
        threading.Thread(target=_routine_email_loop, name="RoutineEmail", daemon=True).start()

_ensure_routine_email_thread()

# ─── ⏰ Routine — 9 PM follow-up (2026-10-07) ───────────────────────────────
# Kai kaam 7 PM tak nahi, raat ~9 baje tak pure hote hain. Isliye 7 PM checklist
# ke alawa 9 PM par ek DOOSRA Telegram message — par sirf un kaamon ke liye jo
# abhi tak ✅/❌ mark NAHI hue (_tgx_send_checklist khud hi sirf pending items
# (`s == ""`) ki checklist banata hai, isliye yahan koi naya filter likhne ki
# zaroorat nahi — bas usi function ko 9 PM par ek baar aur call karna hai).
# Agar 9 PM tak sab already done/missed mark ho chuke hon, checklist khaali
# hoti hai → _tgx_send_checklist khud "skip" return karta hai, koi message nahi jaata.
_ROUTINE_FOLLOWUP_HOUR, _ROUTINE_FOLLOWUP_MIN = 21, 0
_ROUTINE_FOLLOWUP_GRACE_SEC = 2 * 3600        # 9 PM ke baad itni der tak late-send allowed (Space so raha ho to)
_ROUTINE_FOLLOWUP_SENT_FILE = os.path.join(LOCAL_STATE_DIR, "routine_followup_sent.json")

def _routine_followup_loop():
    while True:
        sleep_for = 1800
        try:
            now = _ist_now()
            target = now.replace(hour=_ROUTINE_FOLLOWUP_HOUR, minute=_ROUTINE_FOLLOWUP_MIN, second=0, microsecond=0)
            age = (now - target).total_seconds()
            d = now.date().isoformat()
            if age < 0:
                sleep_for = min(1800, max(5.0, -age + 1))
            elif age <= _ROUTINE_FOLLOWUP_GRACE_SEC:
                sent = _routine_sent_load(_ROUTINE_FOLLOWUP_SENT_FILE)
                if not sent.get(d):
                    try:
                        res = _tgx_send_checklist(d)   # khud hi sirf pending ("s"=="") items bhejta hai; kuch na ho to "skip"
                    except Exception as e:
                        _slog_exception("_routine_followup_loop send", e)
                        res = f"err: {type(e).__name__}: {e}"
                    _slog(f"[routine-followup] {d} -> {res}", level="ok" if res in ("ok", "skip") else "err")
                    if res in ("ok", "skip"):
                        sent[d] = {"ts": time.time(), "res": res}
                        cut = (now.date() - datetime.timedelta(days=30)).isoformat()
                        _routine_sent_save({k: v for k, v in sent.items() if k >= cut}, _ROUTINE_FOLLOWUP_SENT_FILE)
                    else:
                        sleep_for = _REMINDER_RETRY_SEC   # fail hua — grace window ke andar dobara koshish
        except Exception as e:
            _slog_exception("_routine_followup_loop", e)
            sleep_for = 60
        time.sleep(sleep_for)

def _ensure_routine_followup_thread():
    if "RoutineFollowup" not in {t.name for t in threading.enumerate()}:
        threading.Thread(target=_routine_followup_loop, name="RoutineFollowup", daemon=True).start()

_ensure_routine_followup_thread()

@_ws_action("routine_pull")
def _wsa_routine_pull(data):
    """Browser ⏰ panel kholte hi: server par aaye email-responses (rlog) wapas bhejo."""
    b = _routine_read_disk() or {}
    return {"ok": True, "rlog": b.get("rlog") if isinstance(b.get("rlog"), list) else []}

@_ws_action("routine_email_now")
def _wsa_routine_email_now(data):
    """⏰ panel ka 📧 button — aaj ke liye email abhi bhejo (test; sab done ho tab bhi)."""
    st_, msg = _routine_send_email(_ist_now().date().isoformat(), force=True)
    _slog(f"[routine-email] manual -> {st_}: {msg}", level="ok" if st_ == "ok" else "err")
    return {"ok": st_ == "ok", "status": st_, "msg": msg}

@_ws_action("routine_email_nolink")
def _wsa_routine_email_nolink(data):
    """📧 Email tab ka "bina link" test button — wahi list, link ke bina (deliverability check)."""
    st_, msg = _routine_send_email(_ist_now().date().isoformat(), force=True, nolink=True)
    _slog(f"[routine-email] nolink test -> {st_}: {msg}", level="ok" if st_ == "ok" else "err")
    return {"ok": st_ == "ok", "status": st_, "msg": msg}

# ─── 📌 TOP NOTE (2026-10-04): subah ke plan, shaam ke checklist aur weekly/monthly reports ke sabse upar ye 2 line ───
# Badalni ho to sirf neeche ka text edit karo (khaali "" rakhoge to kahin nahi dikhega).
_TOP_NOTE = "🙏राम राम व्यापारी जी 🙏"

def _top_note_prefix() -> str:
    return (_TOP_NOTE + "\n\n") if _TOP_NOTE else ""

# ─── 🗓 Aaj ka Plan — roz subah 6 AM (2026-10-02) ─────────────────────────────
# Roz 6:00 AM IST par email + Telegram: aaj ke saare Routine + To-Do (aaj/overdue) + Habit + Practice ki list (padhne ke liye, link nahi).
# Practice me step ka aaj ka target, hafte aur 28-din cycle ka hisaab; jo step abhi start nahi hua uska "kab shuru" bhi.
_PLAN_HOUR, _PLAN_MIN = 6, 0
_PLAN_GRACE_SEC = 3 * 3600               # 6 AM ke baad itni der tak late-send allowed (Space so raha ho to)
_PLAN_SENT_FILE = os.path.join(LOCAL_STATE_DIR, "today_plan_sent.json")

# ─── 🗯 Tapori lines (2026-10-02) — "Din shubh ho!" ki jagah din ke result ke hisaab se line ──────────────
# File: /data/app_state/tapori_challenge_lines_4000.txt (file manager /dav se daalo; code me hardcode nahi).
# Format: "### N. ..." header se section shuru, "# ..." comment, baaki har non-empty line = ek line.
#   1 = subah, kal FAIL tha | 2 = subah, kal SUCCESS tha | 3 = shaam jawab-page, SUCCESS (sab tick) | 4 = shaam jawab-page, FAIL (koi cross)
# Ek hi line baar-baar na aaye isliye har section ki last _TAPORI_RECENT_KEEP lines yaad rakhta hai (tapori_recent.json).
# File na mile / section khali ho to None — subah purani "Din shubh ho! 💪" jaati hai, shaam ko kuch nahi dikhta.
_TAPORI_FILE = os.path.join(LOCAL_STATE_DIR, "tapori_challenge_lines_4000.txt")
_TAPORI_RECENT_FILE = os.path.join(LOCAL_STATE_DIR, "tapori_recent.json")
_TAPORI_SECS = {1: "am_fail", 2: "am_ok", 3: "pm_ok", 4: "pm_fail"}
_TAPORI_RECENT_KEEP = 400
_TAPORI_LOCK = threading.Lock()
_TAPORI_CACHE: dict = {"mtime": None, "secs": {}}

def _tapori_load() -> dict:
    """{key: [lines]} — file badalne par (mtime) apne aap dobara padhta hai."""
    try:
        mt = os.path.getmtime(_TAPORI_FILE)
    except Exception:
        return {}
    if _TAPORI_CACHE["mtime"] == mt:
        return _TAPORI_CACHE["secs"]
    secs: dict = {}
    try:
        cur = None
        with open(_TAPORI_FILE, "r", encoding="utf-8") as f:
            for raw in f:
                ln = raw.strip()
                if not ln:
                    continue
                if ln.startswith("###"):
                    m = re.match(r"###\s*(\d+)", ln)
                    cur = _TAPORI_SECS.get(int(m.group(1))) if m else None
                    continue
                if ln.startswith("#") or cur is None:
                    continue
                secs.setdefault(cur, []).append(ln)
    except Exception as e:
        _slog_exception("_tapori_load", e)
        return {}
    _TAPORI_CACHE["mtime"], _TAPORI_CACHE["secs"] = mt, secs
    return secs

def _tapori_pick(key: str):
    """Section `key` (am_fail/am_ok/pm_ok/pm_fail) se ek random line, recent wali chhod ke. Na mile to None."""
    try:
        lines = _tapori_load().get(key) or []
        if not lines:
            return None
        import random as _tp_rand
        with _TAPORI_LOCK:
            rec = {}
            try:
                if os.path.exists(_TAPORI_RECENT_FILE):
                    with open(_TAPORI_RECENT_FILE, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                if not isinstance(rec, dict):
                    rec = {}
            except Exception:
                rec = {}
            used = [x for x in (rec.get(key) or []) if isinstance(x, int)]
            keep = min(_TAPORI_RECENT_KEEP, max(0, len(lines) - 1))
            avoid = set(used[-keep:]) if keep else set()
            cand = [i for i in range(len(lines)) if i not in avoid] or list(range(len(lines)))
            idx = _tp_rand.choice(cand)
            rec[key] = (used + [idx])[-_TAPORI_RECENT_KEEP:]
            try:
                tmp = _TAPORI_RECENT_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(rec, f)
                os.replace(tmp, _TAPORI_RECENT_FILE)
            except Exception as e:
                _slog_exception("_tapori_pick save", e)
        return lines[idx]
    except Exception as e:
        _slog_exception("_tapori_pick", e)
        return None

def _day_result(b: dict, d: str):
    """Din `d` ka result: "ok" (saare kaam ✅) | "fail" (koi ❌) | "open" (koi ❌ nahi par kuch baaki/⬜) | None (koi kaam hi nahi)."""
    try:
        sts = [i["s"] for i in _routine_collect(b, d, sunday=True)]
    except Exception as e:
        _slog_exception("_day_result", e)
        return None
    if not sts:
        return None
    if "m" in sts:
        return "fail"
    return "ok" if all(x == "d" for x in sts) else "open"

# ═══ 🗓 SUNDAY TASKS (2026-10-02) — chart.html `_rtSGen/_rtSDate` ka exact twin (GAP model) ═══
# blob["sunday"] = {"tasks":[{id,label,start:"YYYY-MM-DD"(Sunday),gap:N,created,done:{"n":{d,s}}}]}
# Sunday 0 = start; n-th Sunday = start + 7n din; checks = gap, 2*gap, 3*gap … <=200. Dates store nahi hoti.
_SUN_MAX = 200
_SUN_PLAN_MAX = 3                         # plan email me har task ke max itne open checks (sabse naye)

def _sun_gen(gap, max_n: int = _SUN_MAX) -> list:
    try:
        g = int(gap)
    except Exception:
        return []
    if g < 1:
        return []
    return list(range(g, max_n + 1, g))

# (2026-10-04) Start wala Sunday (n=0) bhi pehla check — un tasks ke liye jinki start date 4 Oct 2026 ya baad ki hai
# (start aaj/aage = Sunday 0 abhi "late" nahi ho sakta, isliye purane tasks ka schedule bilkul nahi badalta).
# Pehle ye `created` time par tika tha — task 3 Oct ko bana ho (start 4 Oct) to miss ho jata tha. chart.html `_rtSChecks` ka twin.
_SUN_ZERO_FROM_DATE = "2026-10-04"

def _sun_checks(t: dict) -> list:
    zero = str(t.get("start") or "") >= _SUN_ZERO_FROM_DATE
    return ([0] if zero else []) + _sun_gen(t.get("gap"))

def _sun_date(start: str, n: int):
    try:
        return datetime.date.fromisoformat(str(start)) + datetime.timedelta(days=7 * int(n))
    except Exception:
        return None

def _sun_tasks(b: dict) -> list:
    s = b.get("sunday")
    t = s.get("tasks") if isinstance(s, dict) else None
    return [x for x in t if isinstance(x, dict)] if isinstance(t, list) else []

def _sun_open_items(b: dict, dd: datetime.date) -> list:
    """Din `dd` tak ke open checks (date<=dd, handled nahi): [(label, n, date, late_days, more)].
    Har task ke max 3 sabse naye; `more` = us task ke kitne purane open checks chhupe (sirf task ke aakhri item par, baaki par 0)."""
    out = []
    for t in _sun_tasks(b):
        done = t.get("done") if isinstance(t.get("done"), dict) else {}
        opens = []
        for n in _sun_checks(t):
            dt = _sun_date(t.get("start"), n)
            if dt is None or dt > dd:
                continue
            if str(n) in done:
                continue
            opens.append((str(t.get("label", "")), n, dt, (dd - dt).days))
        more = max(0, len(opens) - _SUN_PLAN_MAX)
        kept = opens[-_SUN_PLAN_MAX:]
        out += [o + (more if i == len(kept) - 1 else 0,) for i, o in enumerate(kept)]
    return out

def _sun_eve_items(b: dict, d: str) -> list:
    """Shaam ki report ke Sunday items (din `d`): [{k:"s", id:"taskId:n", label, sub, s, why}].
    Shaamil: jo checks date<=d aur abhi open hain (har task ke sabse naye _SUN_PLAN_MAX, baaki "+N purane aur"),
    saath me jo us din (date==d ya usi din mark hue) done/skip hain taaki jawab ke baad status dikhe. Future wale kabhi nahi."""
    out = []
    try:
        dd = datetime.date.fromisoformat(d)
    except Exception:
        return out
    for t in _sun_tasks(b):
        tid = str(t.get("id", ""))
        if not tid:
            continue
        done = t.get("done") if isinstance(t.get("done"), dict) else {}
        opens, handled = [], []
        for n in _sun_checks(t):
            dt = _sun_date(t.get("start"), n)
            if dt is None or dt > dd:
                continue
            rec = done.get(str(n))
            if str(n) in done:
                is_d = isinstance(rec, dict) and rec.get("s") == "d"
                if dt == dd or (isinstance(rec, dict) and str(rec.get("d")) == d):
                    handled.append((n, dt, "d" if is_d else "m", str(rec.get("why", "")) if isinstance(rec, dict) and not is_d else ""))
                continue
            opens.append((n, dt, "", ""))
        more = max(0, len(opens) - _SUN_PLAN_MAX)
        rows = sorted(opens[-_SUN_PLAN_MAX:] + handled, key=lambda r: r[0])
        start = len(out)
        for n, dt, st_, why in rows:
            late = (dd - dt).days
            sub = f"Sunday {n} · {dt.day}/{dt.month}" + (f" · ⚠️ {late} din late" if st_ == "" and late > 0 else "")
            out.append({"k": "s", "id": f"{tid}:{n}", "label": str(t.get("label", "")), "sub": sub, "s": st_, "why": why})
        if more and len(out) > start:
            out[-1]["sub"] += f" · +{more} purane aur"
    return out

def _plan_build(b: dict, d: str):
    """(subject, text) — din `d` ka plan. Koi kaam nahi to (None, None)."""
    dd = datetime.date.fromisoformat(d)
    wk = _ROUTINE_WEEKDAYS[(dd.weekday() + 1) % 7]
    items = [i for i in _routine_collect(b, d, dip=True) if i["k"] != "p"]   # DIPANSHU-STEP-1 (2026-10-03)
    pitems = []
    ahead = []
    pdone = 0
    _pl_today = (b.get("plog") or {}).get(d, {}) if isinstance(b.get("plog"), dict) else {}
    for st in _practice_steps(b):
        S0 = _prac_start(st)
        _m, w_t, d_t = _prac_targets(st)
        if dd < S0:
            ahead.append(f"  ⏳ {st.get('label', 'Practice')} — {S0.day}/{S0.month} se shuru ({(S0 - dd).days} din baad)")
            continue
        k = (dd - S0).days
        ws, cs = S0 + datetime.timedelta(days=7 * (k // 7)), S0 + datetime.timedelta(days=28 * (k // 28))
        sid = str(st.get("id"))
        got_w, got_c = _prac_sum(b, sid, ws, dd), _prac_sum(b, sid, cs, dd)
        try:
            got_t = float(_pl_today.get(sid) or 0) if isinstance(_pl_today, dict) else 0.0
        except Exception:
            got_t = 0.0
        if got_t > 0:
            pdone += 1
        pitems.append(f"  {'✅' if got_t > 0 else '⬜'} {st.get('label', 'Practice')} — aaj ka target {d_t:.1f} sets"
                      + (f" (aaj {_fmt_n(got_t)} kiye)" if got_t > 0 else "")
                      + f"\n      hafte me {_fmt_n(got_w)}/{_fmt_n(w_t)} · 28-din cycle me {_fmt_n(got_c)}/{_fmt_n(_m)} (din {k + 1}, hafta {(k % 28) // 7 + 1}/4)")
    sitems = []
    try:
        for lbl, sn, sdt, late, more in _sun_open_items(b, dd):
            sitems.append(f"  ⬜ {lbl} — Sunday {sn} ({sdt.day}/{sdt.month})" + (f" ⚠️ late ({late} din)" if late > 0 else ""))
            if more:
                sitems[-1] += f" (+{more} purane aur)"
    except Exception as e:
        _slog_exception("_plan_build sunday", e)
        sitems = []
    n = len(items) + len(pitems) + len(sitems)
    if n == 0 and not ahead:
        return None, None
    ndone = sum(1 for i in items if i["s"] == "d") + pdone
    L = [x for x in _top_note_prefix().split("\n")][:-1] + [f"🗓 Aaj ka Plan — {dd.day}/{dd.month} · {wk}",
         f"Total {n} kaam" + (f" · ✅ {ndone} ho gaye · ⬜ {n - ndone} baaki" if ndone else ""), ""]
    for sec, title in _ROUTINE_SECS:
        if sec == "p":
            if pitems:
                L += [f"{title} ({len(pitems)})"] + pitems + [""]
            continue
        its = [i for i in items if i["k"] == sec]
        if not its:
            continue
        L.append(f"{title} ({len(its)})")
        for i in its:
            sub = f" ({i['sub']})" if i["sub"] else ""
            late = ""
            if sec == "t" and i["s"] == "" and " · due " in i["sub"] and i["sub"].split(" · due ")[-1] < d:
                late = " ⚠️ late"
            why = f" — {i['why']}" if i["s"] == "m" and i.get("why") else ""
            L.append(f"  {_ROUTINE_ICON.get(i['s'], '⬜')} {i['label']}{sub}{late}{why}")
        L.append("")
    if sitems:
        L += [f"🗓 Sunday ({len(sitems)})"] + sitems + [""]
    if ahead:
        L += ["🎯 Aane wale Practice step"] + ahead + [""]
    # (2026-10-07) Pehle yahan "tapori" random taunt-line aati thi (_tapori_pick).
    # Ab band — hamesha yahi fixed Hindi line aayegi.
    L.append("आपका दिन शुभ हो 🙏")
    return f"Aaj ka Plan - {n} kaam ({dd.day}/{dd.month})", "\n".join(L)

def _plan_send(d: str):
    """(status, msg) — ok | skip | err."""
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    subj, text = _plan_build(b, d)
    if not subj:
        return "skip", "aaj ke liye koi kaam nahi"
    res = _alert_email_and_telegram(subj, text)
    return ("ok", "plan gaya") if res == "ok" else ("err", str(res))

def _plan_now_text() -> str:
    """(2026-10-02) Telegram se "today plan" likhne par — aaj ka plan abhi, wahi text jo subah 6 AM ko jaata hai (subject + body)."""
    d = _ist_now().date().isoformat()
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    subj, text = _plan_build(b, d)
    if not subj:
        return "🗓 Aaj ke liye koi kaam nahi hai."
    return f"{subj}\n\n{text}"

# ═══ MYENGINE-STEP-10A (2026-10-03): 🤖 Telegram Remote — padhne wale commands (koi data change nahi) ═══════════════
# Relay (Render) har message ka text + update_id yahan `POST /api/cmd` par bhejta hai (header X-Relay-Secret), jawab ka text wapas leta hai.
# Saari parsing / validation / data access yahin hai (relay patla hai). Step 10A me SIRF padhne wale commands:
#   help, today plan, todo, rough, info [topic], travel [person], balance.  Likhne wale (todo add/done, rough add, info add,
#   travel add, undo) ka naam table me hai par abhi "jald aayega" jawab dete hain — 10B / 10C unhe asli handler se badlenge.
# NAYA COMMAND JODNE KA TARIKA: `_TG_COMMANDS` table me (prefix tuple, handler) row jodo. Handler: fn(ctx) -> reply text.
#   ctx = {"text": original, "args": prefix ke baad ka hissa (original case), "today": "YYYY-MM-DD", "update_id": int|None}
# Table ka order matter karta hai: lamba / khaas prefix pehle (jaise ("todo","add") ko ("todo",) se pehle). "plan" ka dheela match sabse AKHIR me.
_TG_MAX_REPLY = 3900          # Telegram ~4096 chars leta hai; relay bhi 4000 par kaatta hai
_TG_MAX_TEXT_IN = 3000        # aane wale message ki lambai ki hadd (bulk add ke liye 3000)
_TG_SEEN_MAX = 300            # update_id duplicate guard: itne purane id yaad rakhte hain
_TG_LOCK = threading.RLock()
_TG_SEEN = {}                 # update_id -> time (dict insertion order = purana pehle)
_TG_STATE = {"todo_ids": []}  # sabse naye `todo` jawab ka numbering (id list) — `todo done <number>` (10B) yahi padhta hai (restart par khaali)

_TG_MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
              "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
              "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}
_TG_MON_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# ── pure helpers (data / network nahi) ──
def _tg_norm(s) -> str:
    """trim + extra spaces ek + lowercase (chart.html ke _rtNorm jaisa)."""
    return " ".join(str("" if s is None else s).split()).lower()

def _tg_one(s, mx: int) -> str:
    """ek-line text: newline/tab -> space, extra spaces ek, trim, max length (chart.html ke _rtInfoOne jaisa)."""
    t = " ".join(str("" if s is None else s).split())
    return t[:mx].strip() if len(t) > mx else t

def _tg_is_iso(s) -> bool:
    if not isinstance(s, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
        return False
    try:
        datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
        return True
    except ValueError:
        return False

def _tg_days_between(a, b):
    """b - a din (ISO strings). Galat date ho to None."""
    if not _tg_is_iso(a) or not _tg_is_iso(b):
        return None
    return (datetime.date(int(b[:4]), int(b[5:7]), int(b[8:10])) - datetime.date(int(a[:4]), int(a[5:7]), int(a[8:10]))).days

def _tg_fmt_long(s) -> str:
    return f"{int(s[8:10])} {_TG_MON_SHORT[int(s[5:7]) - 1]} {s[:4]}" if _tg_is_iso(s) else "—"

def _tg_fmt_short(s) -> str:
    return f"{int(s[8:10])} {_TG_MON_SHORT[int(s[5:7]) - 1]}" if _tg_is_iso(s) else "—"

def _tg_parse_date(txt, today: str):
    """Date styles: `today`, `10 jan`, `10-01`, `10-01-2026`, `10 jan 2026` (DD-MM; saal na ho to today ka saal).
    Return: 'YYYY-MM-DD' ya None (samajh nahi aayi / calendar me nahi). Khaali text bhi None — default (aaj) lagana caller ka kaam."""
    t = _tg_norm(txt)
    if not t:
        return None
    if t == "today":
        return today if _tg_is_iso(today) else None
    if not _tg_is_iso(today):
        return None
    yr = int(today[:4])
    m = re.fullmatch(r"([0-9]{1,2}) ([a-z]+)(?: ([0-9]{4}))?", t)
    if m:
        day, mon, y = int(m.group(1)), _TG_MONTHS.get(m.group(2)), (int(m.group(3)) if m.group(3) else yr)
    else:
        m = re.fullmatch(r"([0-9]{1,2})-([0-9]{1,2})(?:-([0-9]{4}))?", t)
        if not m:
            return None
        day, mon, y = int(m.group(1)), int(m.group(2)), (int(m.group(3)) if m.group(3) else yr)
    if not mon:
        return None
    try:
        return datetime.date(y, mon, day).isoformat()
    except ValueError:
        return None

def _tg_split_fields(rest) -> list:
    """`a | b | c` -> ['a','b','c'] (har field trim; khaali field '' rehta hai). 10B / 10C ke add commands yahi use karenge."""
    return [f.strip() for f in str("" if rest is None else rest).split("|")]

# Field limits chart.html jaise hi: To-Do 120 (input maxlength), Rough 500, Info topic/label 60, Travel person 60 / purpose 100.
# Ye pure validators 10B (todo / rough add) aur 10C (info / travel add) ke liye hain; 10A me inhe koi command use nahi karta.
_TG_MAX = {"todo": 120, "rough": 500, "info_topic": 60, "info_label": 60, "info_note": 300, "travel_person": 60, "travel_purpose": 100}

def _tg_clean_todo_text(text) -> dict:
    t = _tg_one(text, _TG_MAX["todo"])
    return {"ok": True, "text": t} if t else {"ok": False, "err": "To-Do ka text khaali hai."}

def _tg_clean_rough_text(text) -> dict:
    t = _tg_one(text, _TG_MAX["rough"])
    return {"ok": True, "text": t} if t else {"ok": False, "err": "Rough ka text khaali hai."}

def _tg_canon_name(items: list, field: str, typed: str, mx: int) -> str:
    """typed naam kisi purane naam se match kare (trim + lowercase) to us ki sabse purani spelling; warna jaisa typed (chart.html _rtInfoCanonTopic / _rtTravelCanonPerson jaisa)."""
    k = _tg_norm(typed)
    old = [e for e in items if isinstance(e, dict) and _tg_norm(e.get(field)) == k]
    if old and k:
        def cr(e):
            try:
                return float(e.get("created_at") or 0)
            except Exception:
                return 0.0
        return _tg_one(min(old, key=cr).get(field), mx)
    return _tg_one(typed, mx)

def _tg_clean_info(topic, label, date_txt, today: str) -> dict:
    """`info add <topic> | <label> | <date>` ke fields. label / date optional (date khaali = aaj). -> {ok, topic, label, date} ya {ok False, err}."""
    tp = _tg_one(topic, _TG_MAX["info_topic"])
    if not tp:
        return {"ok": False, "err": "Topic khaali nahi ho sakta."}
    dt_in = str("" if date_txt is None else date_txt).strip()
    dt = today if not dt_in else _tg_parse_date(dt_in, today)
    if not dt:
        return {"ok": False, "err": f"Date samajh nahi aayi: {_tg_one(dt_in, 30)}"}
    return {"ok": True, "topic": tp, "label": _tg_one(label, _TG_MAX["info_label"]), "date": dt}

def _tg_clean_travel(person, purpose, dep_txt, ret_txt, today: str) -> dict:
    """`travel add <person> | <purpose> | <departure> | <return>` ke fields (_rtTravelClean jaise rules). departure khaali = aaj, return khaali = abhi bahar hai ("")."""
    pe = _tg_one(person, _TG_MAX["travel_person"])
    if not pe:
        return {"ok": False, "err": "Person ka naam khaali nahi ho sakta."}
    pu = _tg_one(purpose, _TG_MAX["travel_purpose"])
    if not pu:
        return {"ok": False, "err": "Kis kaam se gaya — ye khaali nahi ho sakta."}
    d_in = str("" if dep_txt is None else dep_txt).strip()
    dep = today if not d_in else _tg_parse_date(d_in, today)
    if not dep:
        return {"ok": False, "err": f"Date samajh nahi aayi: {_tg_one(d_in, 30)}"}
    r_in = str("" if ret_txt is None else ret_txt).strip()
    ret = ""
    if r_in:
        ret = _tg_parse_date(r_in, today)
        if not ret:
            return {"ok": False, "err": f"Date samajh nahi aayi: {_tg_one(r_in, 30)}"}
        if ret < dep:
            return {"ok": False, "err": "Return date departure se pehle nahi ho sakti."}
    return {"ok": True, "person": pe, "purpose": pu, "departure_date": dep, "return_date": ret}

def _tg_fit_lines(lines: list, budget: int, keep: str = "last"):
    """Itni lines rakho jo `budget` characters me aayein. keep='last' = sabse naye (aakhri) rakho, 'first' = shuru wale.
    Return (kept_lines, hidden_count). Kam se kam 1 line hamesha (lambi ho to kaat kar)."""
    if not lines:
        return [], 0
    seq = list(reversed(lines)) if keep == "last" else list(lines)
    kept, used = [], 0
    for ln in seq:
        add = len(ln) + 1
        if kept and used + add > budget:
            break
        kept.append(ln if (kept or add <= budget) else ln[:max(10, budget - 2)] + "…")
        used += add
    if keep == "last":
        kept.reverse()
    return kept, len(lines) - len(kept)

def _tg_cap(text: str) -> str:
    return text if len(text) <= _TG_MAX_REPLY else text[:_TG_MAX_REPLY - 12].rstrip() + "\n…(kata gaya)"

def _tg_paise(x) -> int:
    """chart.html ke _acPaise jaisa: Math.round(Number(x) * 100); galat / non-finite = 0. (JS round half-up: floor(v + 0.5))."""
    try:
        if isinstance(x, str):
            if "_" in x:
                return 0
            x = float(x) if x.strip() != "" else 0.0
        n = float(x)
    except Exception:
        return 0
    if not math.isfinite(n):
        return 0
    return int(math.floor(n * 100 + 0.5))

def _tg_inr(p: int) -> str:
    """paise -> '₹1,00,000.00' (Indian grouping)."""
    neg = p < 0
    p = abs(int(p))
    rup, pai = divmod(p, 100)
    s = str(rup)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if neg else "") + "₹" + s + f".{pai:02d}"

# ── reply builders (blob / accounts dict lekar text banate hain; koi file nahi padhte) ──
_TG_HELP = (
    "🤖 Commands:\n"
    "• today plan — aaj ka plan\n"
    "• aaj holiday hai / kal holiday hai — holiday ke kaam us din ki list me jud jayenge (holiday = list, holiday add <kaam>)\n"
    "• todo — open To-Do list\n"
    "• rough — Rough list\n"
    "• info <topic> — us topic ki entries (sirf `info` likho to topics ki list)\n"
    "• travel <naam> — us ki trips + total din (sirf `travel` = naamon ki list)\n"
    "• balance — savings accounts ka balance\n"
    "• todo add <text> | <due> | <priority> — naya To-Do (due: kal, 10 jan · priority: high/med/low, dono optional)\n"          # MYENGINE-STEP-10B
    "• todo done <number ya naam> — To-Do done\n"
    "• todo del <number ya naam> — To-Do hatao\n"
    "• habit / routine / dip — aaj ki list (streak ke saath)\n"
    "• habit done 1 2 (ya all / naam) · habit miss 2 | wajah — tick / missed (routine, dip me bhi)\n"
    "• practice — aaj ke sets · practice 1 3 (set 3) · practice 1 +1 (ek aur)\n"
    "• rough del <number> — Rough entry hatao\n"
    "• tick — pending kaam ki ✅/❌ checklist (buttons) · tick kal = beete kal ki\n"
    "• backup — abhi poora backup (zip) Telegram par\n"
    "• rough add <text> — Rough me jodo\n"
    "• info add (pehli line) + neeche har line `topic | label | date` — kai Info entries ek saath (max 30)\n"
    "• info add <topic> | <label> | <date> — Info me entry (label, date optional; date: today, 10 jan, 10-01, 10-01-2026)\n"          # MYENGINE-STEP-10C
    "• travel add <naam> | <kaam> | <departure> | <return> — trip jodo (departure na ho to aaj, return na ho to abhi bahar)\n"
    "• account add <naam> | <opening balance> | <date> — Accounts me naya account (date na ho to aaj)\n"   # ACCOUNT-ADD (2026-10-03)
    "• + <kaam> — todo add ka shortcut (jaise `+ doodh lana | kal | high`) · r <text> — rough add ka shortcut\n"        # TGX2
    "• Kai kaam ek saath: pehli line `todo add` (ya `+`), neeche har line ek kaam (har line me `| kal | high` chalega). Rough ke liye `rough add` ya `r`. Max 30, `undo` se sab wapas\n"
    "• find <shabd> — To-Do, Rough, Info, Travel sab me dhoondho\n"
    "• status — server, threads, backup, disk ki halat\n"
    "• undo — aakhri likhna wapas\n"
    "• help — ye list"
)

def _tg_reply_todo(b: dict, today: str):
    """-> (text, ids). ids = jis order me numbers dikhaye (1 se), `todo done <number>` ke liye."""
    items = [x for x in (b.get("todos") or []) if isinstance(x, dict) and not x.get("done") and str(x.get("text", "")).strip()]
    def key(x):
        try:
            pri = int(x.get("pri") or 2)
        except Exception:
            pri = 2
        try:
            cr = float(x.get("created") or 0)
        except Exception:
            cr = 0.0
        return (str(x.get("due") or "9999-12-31"), pri, cr)
    items.sort(key=key)
    if not items:
        return "✅ Koi open To-Do nahi.", []
    icon = {1: "🔴", 2: "🟡", 3: "🟢"}
    lines = []
    for i, x in enumerate(items, 1):
        try:
            pri = int(x.get("pri") or 2)
        except Exception:
            pri = 2
        due = str(x.get("due") or "")
        dtxt = ""
        if _tg_is_iso(due):
            dtxt = " · due " + _tg_fmt_short(due) + (" ⚠️ late" if due < today else "")
        lines.append(f"{i}. {icon.get(pri, '🟡')} {_tg_one(x.get('text'), 200)}{dtxt}")
    kept, hidden = _tg_fit_lines(lines, _TG_MAX_REPLY - 120, keep="first")
    txt = f"✅ To-Do ({len(items)}):\n" + "\n".join(kept)
    if hidden:
        txt += f"\n…aur {hidden} (list lambi hai)"
    return txt, [str(x.get("id")) for x in items]

def _tg_reply_rough(b: dict) -> str:
    items = [x for x in (b.get("rough") or []) if isinstance(x, dict) and str(x.get("text", "")).strip()]
    if not items:
        return "📝 Rough list khaali hai."
    lines = [f"{i}. {_tg_one(x.get('text'), 300)}" for i, x in enumerate(items, 1)]
    kept, hidden = _tg_fit_lines(lines, _TG_MAX_REPLY - 120, keep="last")
    txt = f"📝 Rough ({len(items)}):\n"
    if hidden:
        txt += f"…pehle ki {hidden} entries aur hain\n"
    return txt + "\n".join(kept)

def _tg_topics(items: list, field: str) -> list:
    """[(key, naam, count)] — naam = sabse purani entry ki spelling; A-Z by key. (chart.html _rtInfoTopics / _rtTravelPeople jaisa)"""
    m = {}
    def cr(e):
        try:
            return float(e.get("created_at") or 0)
        except Exception:
            return 0.0
    for e in sorted(items, key=cr):
        k = _tg_norm(e.get(field))
        if k not in m:
            m[k] = [k, _tg_one(e.get(field), 60), 0]
        m[k][2] += 1
    return sorted((tuple(v) for v in m.values()), key=lambda v: v[0])

def _tg_good(list_, field: str) -> list:
    return [x for x in (list_ or []) if isinstance(x, dict) and isinstance(x.get("id"), str) and x.get("id") and _tg_norm(x.get(field))]

def _tg_names_line(tops: list) -> str:
    kept, hidden = _tg_fit_lines([f"{n} ({c})" for _k, n, c in tops], 900, keep="first")
    return ", ".join(kept) + (f" …+{hidden}" if hidden else "")

def _tg_reply_info(b: dict, topic: str, today: str) -> str:
    items = _tg_good(b.get("info"), "topic")
    tops = _tg_topics(items, "topic")
    if not tops:
        return "ℹ️ Info me abhi koi entry nahi."
    key = _tg_norm(topic)
    if not key:
        return "ℹ️ Topics: " + _tg_names_line(tops) + "\n(`info <topic>` likho)"
    hit = next((t for t in tops if t[0] == key), None)
    if not hit:
        return f"ℹ️ '{_tg_one(topic, 40)}' naam ka topic nahi mila.\nTopics: " + _tg_names_line(tops)
    def cmp_key(e):
        d = e.get("date") if _tg_is_iso(e.get("date")) else ""
        try:
            c = float(e.get("created_at") or 0)
        except Exception:
            c = 0.0
        return (d, c, str(e.get("id")))
    mine = sorted([e for e in items if _tg_norm(e.get("topic")) == key], key=cmp_key)
    lines, prev = [], None
    for e in mine:
        g = _tg_days_between(prev, e.get("date")) if prev is not None else None
        gtxt = ""
        if g is not None:
            gtxt = " · same din" if g == 0 else f" · +{g} din"
        lab = _tg_one(e.get("label"), 60)
        note = _tg_one(e.get("note"), 120)
        lines.append(f"{_tg_fmt_long(e.get('date'))}{' · ' + lab if lab else ''}{gtxt}{' — ' + note if note else ''}")
        prev = e.get("date") if _tg_is_iso(e.get("date")) else prev
    kept, hidden = _tg_fit_lines(lines, _TG_MAX_REPLY - 150, keep="last")
    txt = f"ℹ️ {hit[1]} ({hit[2]} {'entry' if hit[2] == 1 else 'entries'}):\n"
    if hidden:
        txt += f"…pehle ki {hidden} entries aur hain\n"
    return txt + "\n".join(kept)

def _tg_travel_days(t: dict, today: str) -> dict:
    """chart.html _rtTravelDays jaisa: state done / away / soon / bad."""
    dep, ret = t.get("departure_date"), t.get("return_date")
    if not _tg_is_iso(dep):
        return {"state": "bad", "days": None, "same": False, "togo": None}
    if _tg_is_iso(ret):
        g = _tg_days_between(dep, ret)
        if g is None or g < 0:
            return {"state": "bad", "days": None, "same": False, "togo": None}
        return {"state": "done", "days": g + 1, "same": g == 0, "togo": None}
    if ret:
        return {"state": "bad", "days": None, "same": False, "togo": None}
    d = _tg_days_between(dep, today)
    if d is None:
        return {"state": "bad", "days": None, "same": False, "togo": None}
    if d < 0:
        return {"state": "soon", "days": None, "same": False, "togo": -d}
    return {"state": "away", "days": d + 1, "same": False, "togo": None}

def _tg_travel_dates(t: dict) -> str:
    dep, ret = t.get("departure_date"), t.get("return_date")
    if not _tg_is_iso(dep):
        return "—"
    if not _tg_is_iso(ret):
        return _tg_fmt_long(dep) + " se"
    if ret == dep:
        return _tg_fmt_long(dep)
    if dep[:4] == ret[:4]:
        return _tg_fmt_short(dep) + " to " + _tg_fmt_long(ret)
    return _tg_fmt_long(dep) + " to " + _tg_fmt_long(ret)

def _tg_reply_travel(b: dict, person: str, today: str) -> str:
    items = _tg_good(b.get("travel"), "person")
    tops = _tg_topics(items, "person")
    if not tops:
        return "✈️ Travel me abhi koi trip nahi."
    key = _tg_norm(person)
    if not key:
        return "✈️ Log: " + _tg_names_line(tops) + "\n(`travel <naam>` likho)"
    hit = next((t for t in tops if t[0] == key), None)
    if not hit:
        return f"✈️ '{_tg_one(person, 40)}' naam ka koi nahi mila.\nLog: " + _tg_names_line(tops)
    def cmp_key(e):
        d = e.get("departure_date") if _tg_is_iso(e.get("departure_date")) else ""
        try:
            c = float(e.get("created_at") or 0)
        except Exception:
            c = 0.0
        return (d, c, str(e.get("id")))
    mine = sorted([e for e in items if _tg_norm(e.get("person")) == key], key=cmp_key, reverse=True)   # naya pehle
    lines, days, ongoing, ongoing_days = [], 0, 0, 0
    for e in mine:
        di = _tg_travel_days(e, today)
        if di["state"] == "done":
            dtxt = "1 day (same day)" if di["same"] else f"{di['days']} days"
        elif di["state"] == "away":
            dtxt = f"Abhi bahar hai ({di['days']} {'day' if di['days'] == 1 else 'days'} so far)"
        elif di["state"] == "soon":
            dtxt = f"Aage ki date ({di['togo']} din baad)"
        else:
            dtxt = "date kharab"
        if di["days"] is not None:
            days += di["days"]
            if di["state"] == "away":
                ongoing += 1
                ongoing_days += di["days"]
        purpose = _tg_one(e.get("purpose"), 100)
        place = _tg_one(e.get("place"), 60)
        lines.append(f"{_tg_travel_dates(e)} · {purpose}{' · ' + place if place else ''} · {dtxt}")
    kept, hidden = _tg_fit_lines(lines, _TG_MAX_REPLY - 250, keep="first")
    tot = f"{len(mine)} {'trip' if len(mine) == 1 else 'trips'} · {days} {'day' if days == 1 else 'days'} away"
    if ongoing:
        tot += f" (abhi chal rahi trip ke {ongoing_days} {'day' if ongoing_days == 1 else 'days'} milake)"
    txt = f"✈️ {hit[1]}: {tot}\n" + "\n".join(kept)
    if hidden:
        txt += f"\n…aur {hidden} purani trips"
    return txt

def _tg_reply_balance(acc) -> str:
    """Savings accounts ka balance (sirf non-archived; Fyers nahi). Integer paise: opening + credits - debits
    (chart.html _acClosingP jaisa — galat type / date / amount wali entry aur dusre account ki entry ignore). Account number kabhi nahi, sirf naam + bank."""
    acc = acc if isinstance(acc, dict) else {}
    accounts = [a for a in (acc.get("accounts") or []) if isinstance(a, dict) and isinstance(a.get("id"), str) and a.get("id") and isinstance(a.get("name"), str)]
    entries = acc.get("entries") if isinstance(acc.get("entries"), list) else []
    rows, total = [], 0
    for a in accounts:
        if a.get("archived"):
            continue
        bal = _tg_paise(a.get("opening_balance"))
        for e in entries:
            if not isinstance(e, dict) or e.get("account_id") != a["id"]:
                continue
            if e.get("type") not in ("Credit", "Debit") or not _tg_is_iso(e.get("date")):
                continue
            p = _tg_paise(e.get("amount"))
            if p <= 0:
                continue
            bal += p if e.get("type") == "Credit" else -p
        total += bal
        bank = _tg_one(a.get("bank_name"), 30)
        rows.append(f"• {_tg_one(a.get('name'), 40) or '(naam nahi)'}{' (' + bank + ')' if bank else ''}: {_tg_inr(bal)}")
    if not rows:
        return "💰 Koi savings account nahi hai."
    kept, hidden = _tg_fit_lines(rows, _TG_MAX_REPLY - 150, keep="first")
    txt = f"💰 Total: {_tg_inr(total)}\n" + "\n".join(kept)
    if hidden:
        txt += f"\n…aur {hidden} accounts"
    return txt

# ── command handlers ──
def _tg_today() -> str:
    return _ist_now().date().isoformat()

def _tg_h_help(ctx):
    return _TG_HELP

def _tg_h_plan(ctx):
    return _plan_now_text()          # wahi text jo purana /api/plan_now deta hai (6 AM wala plan)

def _tg_h_todo(ctx):
    if ctx["args"].strip():
        return "Samajh nahi aaya. `todo` likho (list ke liye). `help` dekho."
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    txt, ids = _tg_reply_todo(b, ctx["today"])
    with _TG_LOCK:
        _TG_STATE["todo_ids"] = ids   # 10B: `todo done <number>` isi list se
    return txt

def _tg_h_rough(ctx):
    if ctx["args"].strip():
        return "Samajh nahi aaya. `rough` likho (list ke liye). `help` dekho."
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    return _tg_reply_rough(b)

def _tg_h_info(ctx):
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    return _tg_reply_info(b, ctx["args"], ctx["today"])

def _tg_h_travel(ctx):
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    return _tg_reply_travel(b, ctx["args"], ctx["today"])

def _tg_h_balance(ctx):
    with _ACCOUNTS_LOCK:
        acc = _accounts_read_disk()
    return _tg_reply_balance(acc)

def _tg_h_not_yet(ctx):
    # 10B / 10C: ye rows asli handler se badlo. Abhi jaan-bujh kar kuch likhta nahi, aur "plan" wale dheele match se bhi bachata hai.
    return "Ye command abhi available nahi hai (jald aayega). `help` dekho."

def _tg_is_plan_text(words: list) -> bool:
    """Dheela 'plan' match — sirf chhote message (<=4 shabd) me, aur 'kal / tomorrow' ke bina (purane relay jaisa)."""
    return len(words) <= 4 and "plan" in words and not ({"kal", "tomorrow"} & set(words))

# ═══ MYENGINE-STEP-10B (2026-10-03): 🤖 Telegram Remote — likhne wale commands (todo add / todo done / rough add / undo) ═══
# Sab likhna EK shared helper `_tg_write` se hota hai (save + rlog record + undo record). 10C ke info / travel add bhi isi se.
# Rasta: routine.json ka blob (under _ROUTINE_LOCK) -> `_routine_apply_entry` ("x" entry) -> rlog me wahi entry -> save.
# Browser ka agla whole-blob save `_routine_merge_rlog` se ye entry wapas laga deta hai (overwrite nahi hota), aur ⏰ panel
# kholte hi `routine_pull` / `_rtPull` (chart.html) isse utha leta hai.
# Undo: tg_undo.json (LOCAL_STATE_DIR, browser sync ka hissa nahi) me chhota stack, max _TG_UNDO_MAX; `undo` sabse aakhri write hatata hai.
_TG_UNDO_MAX = 20
_TG_UNDO_LOCK = threading.RLock()     # lock order hamesha: _ROUTINE_LOCK pehle, phir _TG_UNDO_LOCK
_TG_LAST_T = [0]                      # pichla ms stamp (hamesha badhta; rlog key k:id:d:t unique rahe)
_TG_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"

def _tg_now_ms() -> int:
    with _TG_LOCK:
        n = int(time.time() * 1000)
        if n <= _TG_LAST_T[0]:
            n = _TG_LAST_T[0] + 1
        _TG_LAST_T[0] = n
        return n

def _tg_new_id(prefix: str, now_ms: int) -> str:
    """chart.html `_rtId(p)` jaisa: prefix + base36(ms) + 4 random base36 (t=todo, g=rough, i=info, v=travel)."""
    import secrets
    n, s = int(now_ms), ""
    while n:
        n, r = divmod(n, 36)
        s = _TG_B36[r] + s
    return prefix + (s or "0") + "".join(secrets.choice(_TG_B36) for _ in range(4))

def _tg_undo_file() -> str:
    return os.path.join(LOCAL_STATE_DIR, "tg_undo.json")

def _tg_undo_load() -> list:
    try:
        p = _tg_undo_file()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                x = json.load(f)
            if isinstance(x, list):
                return [r for r in x if isinstance(r, dict)]
    except Exception as e:
        _slog_exception("_tg_undo_load", e)
    return []

def _tg_undo_save(st: list) -> bool:
    p = _tg_undo_file()
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st[-_TG_UNDO_MAX:], f)
        os.replace(tmp, p)
        return True
    except Exception as e:
        _slog_exception("_tg_undo_save", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

def _tg_read_blob():
    """-> (blob, err). routine.json hai par padh nahi paye to err (tab likhna mana hai, warna file khaali blob se overwrite ho jaati).
    File hai hi nahi to khaali blob theek hai."""
    b = _routine_read_disk()
    if b is not None:
        return b, ""
    if os.path.exists(os.path.join(LOCAL_STATE_DIR, "routine.json")):
        return None, "⚠️ App ka data padh nahi paya, isliye kuch change nahi kiya."
    return _routine_blank(), ""

def _tg_find(b: dict, w: str, iid: str):
    """-> (list, index). index -1 = nahi mila."""
    lst = b.get(_TG_LISTKEY.get(w, "")) if isinstance(b, dict) else None
    if not isinstance(lst, list):
        return [], -1
    for j, x in enumerate(lst):
        if isinstance(x, dict) and str(x.get("id")) == str(iid):
            return lst, j
    return lst, -1

def _tg_write(w: str, s: str, iid: str, item=None, undo=None, now_ms=None):
    """SHARED WRITE HELPER (10B banaya, 10C bhi yahi use karega). -> (ok, err_text).
      w = list ("t" todos, "g" rough, "i" info, "v" travel); s = "a" add (item chahiye) | "r" remove | "d" todo done | "u" todo wapas open
      undo = None ya {"kind": <_TG_UNDO_KINDS ka naam>, "id": iid, "text": <confirm / undo message me dikhne wala text>, ...} — save ke BAAD stack par push hota hai
    Blob par lagana + rlog entry + b["t"] + atomic save sab _ROUTINE_LOCK ke andar; undo record bhi lock me, save safal hone ke baad."""
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return False, err
        t = int(now_ms) if now_ms else _tg_now_ms()
        e = {"k": "x", "id": str(iid), "d": _tg_today(), "s": s, "w": w, "t": t}
        if item is not None:
            e["item"] = item
        _routine_apply_entry(b, e)
        cutoff = int((time.time() - _ROUTINE_RLOG_KEEP_SEC) * 1000)
        keep = []
        for x in b["rlog"]:
            try:
                if isinstance(x, dict) and int(x.get("t") or 0) >= cutoff:
                    keep.append(x)
            except Exception:
                pass
        keep.append(e)
        b["rlog"] = keep
        b["t"] = t
        ok, msg = _local_state_save_raw("routine", b)
        if not ok:
            return False, "⚠️ Save nahi ho paya, kuch change nahi hua. Dobara try karo."
        if undo:
            with _TG_UNDO_LOCK:
                st = _tg_undo_load()
                st.append(dict(undo, at=t))
                _tg_undo_save(st)
    return True, ""

# Undo ke reverse-functions: kind -> fn(b, rec) -> (op, msg). op = (w, s, iid) jo likhna hai, ya None (kuch karna nahi; msg hi jawab).
# 10C: `_TG_UNDO_KINDS["info_add"] = _tg_undo_remover("i", "Info entry")` aur `["travel_add"] = _tg_undo_remover("v", "Trip")`.
def _tg_undo_remover(w: str, label: str):
    def fn(b, rec):
        lst, idx = _tg_find(b, w, rec.get("id"))
        txt = _tg_one(rec.get("text"), 120)
        if idx < 0:
            return None, f"{label} app me ab nahi hai (pehle hi hat chuki): {txt}"
        return (w, "r", rec.get("id")), f"↩️ Undo: {label} hata di: {txt}"
    return fn

def _tg_undo_todo_done(b, rec):
    lst, idx = _tg_find(b, "t", rec.get("id"))
    txt = _tg_one(rec.get("text"), 120)
    if idx < 0:
        return None, f"Wo To-Do app me ab nahi hai (hat chuka): {txt}"
    if not lst[idx].get("done"):
        return None, f"Wo To-Do pehle se open hai: {txt}"
    return ("t", "u", rec.get("id")), f"↩️ Undo: To-Do wapas open kiya: {txt}"

_TG_UNDO_KINDS = {
    "todo_add": _tg_undo_remover("t", "To-Do"),
    "rough_add": _tg_undo_remover("g", "Rough entry"),
    "todo_done": _tg_undo_todo_done,
}

_TG_PRI_WORDS = {"1": 1, "high": 1, "h": 1, "urgent": 1, "!!!": 1, "2": 2, "med": 2, "medium": 2, "m": 2, "normal": 2,
                 "3": 3, "low": 3, "l": 3}

def _tg_parse_due(txt, today: str):
    """`today/aaj`, `tomorrow/kal`, `parso`, ya _tg_parse_date wale formats -> 'YYYY-MM-DD' ya None."""
    t = _tg_norm(txt)
    if not _tg_is_iso(today):
        return None
    base = datetime.date.fromisoformat(today)
    if t in ("today", "aaj"):
        return today
    if t in ("tomorrow", "kal"):
        return (base + datetime.timedelta(days=1)).isoformat()
    if t in ("parso", "day after tomorrow"):
        return (base + datetime.timedelta(days=2)).isoformat()
    return _tg_parse_date(t, today)

def _tg_todo_pick(arg, b: dict, today: str):
    """-> (todo item dict, err). arg = number (`todo` list wala) ya naam ka hissa. Restart ke baad number abhi ki list se banta hai."""
    a = _tg_one(arg, 120)
    if not a:
        return None, "Number ya naam likho, jaise `2` ya `doodh`."
    if re.fullmatch(r"[0-9]{1,4}", a):
        n = int(a)
        with _TG_LOCK:
            ids = list(_TG_STATE.get("todo_ids") or [])
        if not ids:
            _t, ids = _tg_reply_todo(b, today)      # server restart ke baad: abhi ki list ke hisaab se
        if not ids:
            return None, "Koi open To-Do nahi hai."
        if n < 1 or n > len(ids):
            return None, f"Number {n} list me nahi hai (1 se {len(ids)} tak). `todo` likho."
        lst, idx = _tg_find(b, "t", ids[n - 1])
        if idx < 0:
            return None, "Ye To-Do ab app me nahi mila (hat gaya hoga). `todo` likho."
        return lst[idx], ""
    low = a.lower()
    opn = [x for x in (b.get("todos") or []) if isinstance(x, dict) and not x.get("done")]
    exact = [x for x in opn if str(x.get("text", "")).strip().lower() == low]
    if len(exact) == 1:
        return exact[0], ""
    cands = [x for x in opn if low in str(x.get("text", "")).lower()]
    if len(cands) == 1:
        return cands[0], ""
    if not cands:
        return None, f"'{a[:40]}' naam ka koi open To-Do nahi mila. `todo` likho."
    return None, "Kai match mile: " + " · ".join(_tg_one(x.get("text"), 40) for x in cands[:5]) + (" …" if len(cands) > 5 else "") + "\nThoda aur poora naam ya number likho."

def _tg_h_todo_add(ctx):
    parts = _tg_split_fields(ctx["args"])
    raw = parts[0] if parts else ""
    c = _tg_clean_todo_text(raw)
    if not c["ok"]:
        return "Kya add karna hai? Aise likho: `todo add doodh lana` ya `todo add doodh lana | kal | high`"
    pri, due = 2, ""
    for f in parts[1:]:
        if not f:
            continue
        p = _TG_PRI_WORDS.get(_tg_norm(f))
        if p:
            pri = p
            continue
        dd_ = _tg_parse_due(f, ctx["today"])
        if dd_:
            due = dd_
            continue
        return (f"'{_tg_one(f, 30)}' samajh nahi aaya.\nAise likho: `todo add doodh lana | kal | high`\n"
                "(due: today, kal, 10 jan, 10-01 · priority: high / med / low)")
    now = _tg_now_ms()
    iid = _tg_new_id("t", now)
    item = {"id": iid, "text": c["text"], "pri": pri, "due": due, "done": False, "doneAt": 0, "created": now}
    ok, err = _tg_write("t", "a", iid, item=item, undo={"kind": "todo_add", "id": iid, "text": c["text"]}, now_ms=now)
    if not ok:
        return err
    cut = " (bahut lamba tha, kaat diya)" if len(" ".join(str(raw).split())) > _TG_MAX["todo"] else ""
    extra = {1: " 🔴", 2: "", 3: " 🟢"}[pri] + (f" · due {_tg_fmt_short(due)}" if due else "")
    return f"✅ To-Do add ho gaya: {c['text']}{extra}{cut}\n(wapas lene ke liye `undo` likho)"

def _tg_h_todo_done(ctx):
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        it, err = _tg_todo_pick(ctx["args"], b, ctx["today"])
        if err:
            return err
        iid = str(it.get("id"))
        text = _tg_one(it.get("text"), 120)
        if it.get("done"):
            return f"Ye pehle se done hai: {text}"
        ok, err = _tg_write("t", "d", iid, undo={"kind": "todo_done", "id": iid, "text": text})
    if not ok:
        return err
    return f"✅ Done: {text}\n(wapas lene ke liye `undo` likho)"

def _tg_h_rough_add(ctx):
    raw = ctx["args"]
    c = _tg_clean_rough_text(raw)
    if not c["ok"]:
        return "Kya likhna hai? Aise likho: `rough add kuch bhi`"
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        pos = sum(1 for x in b.get("rough", []) if isinstance(x, dict) and str(x.get("text", "")).strip()) + 1
        now = _tg_now_ms()
        iid = _tg_new_id("g", now)
        ok, err = _tg_write("g", "a", iid, item={"id": iid, "text": c["text"], "created_at": now},
                            undo={"kind": "rough_add", "id": iid, "text": c["text"]}, now_ms=now)
    if not ok:
        return err
    cut = " (bahut lamba tha, kaat diya)" if len(" ".join(str(raw).split())) > _TG_MAX["rough"] else ""
    return f"📝 Rough me add ho gaya (#{pos}): {_tg_one(c['text'], 300)}{cut}\n(wapas lene ke liye `undo` likho)"

def _tg_h_undo(ctx):
    if ctx["args"].strip():
        return "Samajh nahi aaya. Sirf `undo` likho."
    with _ROUTINE_LOCK:
        with _TG_UNDO_LOCK:
            st = _tg_undo_load()
            if not st:
                return "Undo ke liye kuch nahi hai."
            rec = st[-1]
            fn = _TG_UNDO_KINDS.get(rec.get("kind"))
            if fn is None:
                st.pop()
                _tg_undo_save(st)
                return "Aakhri record undo layak nahi tha, hata diya. `undo` dobara likh sakte ho."
            if rec.get("kind") == "account_add":    # ACCOUNT-ADD (2026-10-03): accounts.json par undo (routine blob nahi)
                done, msg = _tg_undo_account(rec)
                if not done:
                    return msg
                st.pop()
                _tg_undo_save(st)
                if st:
                    msg += f"\n({len(st)} aur undo ho sakte hain)"
                return msg
            if rec.get("kind") in _MM_UNDO_KINDS:   # MM-TG (2026-10-04): 💵 Money Manager add / del / edit ka undo (moneymgr.json)
                done, msg = _mm_undo(rec)
                if not done:
                    return msg
                st.pop()
                _tg_undo_save(st)
                if st:
                    msg += f"\n({len(st)} aur undo ho sakte hain)"
                return msg
            if rec.get("kind") == "bulk_add":       # TGX2: bulk add ek saath wapas
                done, msg = _tg_undo_bulk(rec)
                if not done:
                    return msg
                st.pop()
                _tg_undo_save(st)
                if st:
                    msg += f"\n({len(st)} aur undo ho sakte hain)"
                return msg
            if rec.get("kind") in _TGX_SPECIAL_UNDO:    # TGX: habit/routine mark, practice, delete
                done, msg = _tgx_undo_special(rec)
                if not done:
                    return msg
                st.pop()
                _tg_undo_save(st)
                if st:
                    msg += f"\n({len(st)} aur undo ho sakte hain)"
                return msg
            b, err = _tg_read_blob()
            if err:
                return err
            op, msg = fn(b, rec)
            if op:
                ok, err = _tg_write(op[0], op[1], op[2])
                if not ok:
                    return err          # record stack me hi rehta hai (dobara try kar sakte ho)
            st.pop()
            _tg_undo_save(st)
            if st:
                msg += f"\n({len(st)} aur undo ho sakte hain)"
            return msg
# ═══ MYENGINE-STEP-10B END ═══

# ═══ MYENGINE-STEP-10C (2026-10-03): 🤖 Telegram Remote — `info add` / `travel add` (10B ka _tg_write + 10A ke date parser / validators) ═══
# Dono commands wahi shared write helper `_tg_write` use karte hain (w="i" info, w="v" travel) — chart.html me kuch badalna nahi pada,
# kyunki 10B ka "x" apply (Python + JS) pehle se chaaron lists ko jaanta hai. Dono `undo` se wapas ho sakte hain.
_TG_INFO_FMT = "`info add cylinder | khatam hua | 10 jan` (label aur date optional; date na likho to aaj)"
_TG_TRAVEL_FMT = "`travel add mummy | hospital | 10 jan | 12 jan` (departure na likho to aaj; return na likho to abhi bahar hai)"

def _tg_too_long(raw, mx: int) -> bool:
    """True agar (spaces ek karke) raw text mx se lamba tha — jawab me 'kaat diya' batane ke liye."""
    return len(" ".join(str("" if raw is None else raw).split())) > mx

def _tg_info_gap_before(items: list, key: str, date: str):
    """Naayi entry (date) se pehle wali same-topic entry se kitne din (app ke gap jaisa: date ke hisaab se, pehli entry = None)."""
    prev = None
    for e in items:
        if isinstance(e, dict) and _tg_norm(e.get("topic")) == key and _tg_is_iso(e.get("date")) and e.get("date") <= date:
            if prev is None or e.get("date") > prev:
                prev = e.get("date")
    return None if prev is None else _tg_days_between(prev, date)

def _tg_h_info_add(ctx):
    f = _tg_split_fields(ctx["args"])
    if not ctx["args"].strip() or not f[0]:
        return "Kya jodna hai? Aise likho: " + _TG_INFO_FMT
    if len(f) > 3:
        return "Bahut zyada `|` hain — sirf 3 hisse: `info add <topic> | <label> | <date>`. Example: " + _TG_INFO_FMT
    f += [""] * (3 - len(f))
    c = _tg_clean_info(f[0], f[1], f[2], ctx["today"])
    if not c["ok"]:
        return c["err"] + "\nExample: " + _TG_INFO_FMT
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        items = [x for x in (b.get("info") or []) if isinstance(x, dict)]
        topic = _tg_canon_name(items, "topic", c["topic"], _TG_MAX["info_topic"])
        gap = _tg_info_gap_before(items, _tg_norm(topic), c["date"])
        now = _tg_now_ms()
        iid = _tg_new_id("i", now)
        item = {"id": iid, "topic": topic, "date": c["date"], "label": c["label"], "note": "", "created_at": now}
        shown = f"{topic}{' · ' + c['label'] if c['label'] else ''} · {_tg_fmt_long(c['date'])}"
        ok, err = _tg_write("i", "a", iid, item=item, undo={"kind": "info_add", "id": iid, "text": shown}, now_ms=now)
    if not ok:
        return err
    if gap is None:
        gtxt = " (is topic ki pehli entry)"
    elif gap == 0:
        gtxt = " (pichli entry ke same din)"
    else:
        gtxt = f" (+{gap} din pichli entry se)"
    cut = " (lamba tha, kaat diya)" if (_tg_too_long(f[0], _TG_MAX["info_topic"]) or _tg_too_long(f[1], _TG_MAX["info_label"])) else ""
    return f"ℹ️ Info add ho gaya: {shown}{gtxt}{cut}\n(wapas lene ke liye `undo` likho)"

def _tg_travel_state_text(di: dict) -> str:
    if di["state"] == "done":
        return "1 day (same day)" if di["same"] else f"{di['days']} days"
    if di["state"] == "away":
        return f"Abhi bahar hai ({di['days']} {'day' if di['days'] == 1 else 'days'} so far)"
    if di["state"] == "soon":
        return f"Aage ki date ({di['togo']} din baad)"
    return "date kharab"

def _tg_h_travel_add(ctx):
    f = _tg_split_fields(ctx["args"])
    if not ctx["args"].strip() or not f[0]:
        return "Kaun gaya? Aise likho: " + _TG_TRAVEL_FMT
    if len(f) > 4:
        return "Bahut zyada `|` hain — sirf 4 hisse: `travel add <naam> | <kaam> | <departure> | <return>`. Example: " + _TG_TRAVEL_FMT
    f += [""] * (4 - len(f))
    c = _tg_clean_travel(f[0], f[1], f[2], f[3], ctx["today"])
    if not c["ok"]:
        return c["err"] + "\nExample: " + _TG_TRAVEL_FMT
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        items = [x for x in (b.get("travel") or []) if isinstance(x, dict)]
        person = _tg_canon_name(items, "person", c["person"], _TG_MAX["travel_person"])
        now = _tg_now_ms()
        iid = _tg_new_id("v", now)
        item = {"id": iid, "person": person, "purpose": c["purpose"], "departure_date": c["departure_date"],
                "return_date": c["return_date"], "place": "", "note": "", "created_at": now}
        di = _tg_travel_days(item, ctx["today"])
        shown = f"{person} · {c['purpose']} · {_tg_travel_dates(item)}"
        ok, err = _tg_write("v", "a", iid, item=item, undo={"kind": "travel_add", "id": iid, "text": shown}, now_ms=now)
    if not ok:
        return err
    cut = " (lamba tha, kaat diya)" if (_tg_too_long(f[0], _TG_MAX["travel_person"]) or _tg_too_long(f[1], _TG_MAX["travel_purpose"])) else ""
    return f"✈️ Trip add ho gayi: {shown} · {_tg_travel_state_text(di)}{cut}\n(wapas lene ke liye `undo` likho)"

# undo ke reverse-functions (10B ka _tg_undo_remover wahi: item hatao, ya bata do ki pehle hi hat chuka)
_TG_UNDO_KINDS["info_add"] = _tg_undo_remover("i", "Info entry")
_TG_UNDO_KINDS["travel_add"] = _tg_undo_remover("v", "Trip")
# ═══ MYENGINE-STEP-10C END ═══

# ═══ ACCOUNT-ADD (2026-10-03): 🤖 Telegram `account add <naam> | <opening balance> | <date>` — 💰 Accounts tab me naya account ═══
# Rasta: accounts.json (alag file, revision-checked `_accounts_save`) — routine blob nahi. Shape chart.html ke _acAddAccount jaisi:
#   {id:"ac…", name, opening_balance:<number rupaye>, opening_date:"YYYY-MM-DD", bank_name:"", last4:"", archived:false, created_at:<ms>}
# `undo` se wapas ho sakta hai (sirf jab us account me koi entry nahi). Browser: Accounts tab ka "Naya data lo" / app reload se naya account dikhta hai.
_TG_ACCT_FMT = "`account add Krishan | 19809.95 | 1 oct 2026` (date na likho to aaj; balance negative bhi ho sakta hai)"
_TG_AC_MAX = 1e12

def _tg_parse_amount(txt):
    """chart.html _acParseAmount ka port: '1,00,000', '₹ 25000.5', '-1500' -> float; galat / 2 se zyada decimal / bahut bada -> None."""
    if isinstance(txt, (int, float)):
        txt = str(txt)
    if not isinstance(txt, str):
        return None
    t = re.sub(r"[₹,\s]", "", txt)
    if not re.fullmatch(r"-?([0-9]+(\.[0-9]*)?|\.[0-9]+)", t):
        return None
    if "." in t and len(t) - t.index(".") - 1 > 2:
        return None
    n = float(t)
    if not math.isfinite(n) or abs(n) >= _TG_AC_MAX:
        return None
    return 0.0 if n == 0 else n

def _tg_ac_fmt_num(n: float) -> str:
    return str(int(n)) if n == int(n) else ("%.2f" % n).rstrip("0").rstrip(".")

def _tg_h_account_add(ctx):
    f = _tg_split_fields(ctx["args"])
    if not ctx["args"].strip() or not f[0]:
        return "Account ka naam likho. Aise: " + _TG_ACCT_FMT
    if len(f) > 3:
        return "Bahut zyada `|` hain — sirf 3 hisse: `account add <naam> | <opening balance> | <date>`. Example: " + _TG_ACCT_FMT
    f += [""] * (3 - len(f))
    name = _tg_one(f[0], 200)
    if len(name) > 40:
        return "Naam 40 akshar se chhota rakho."
    ob = _tg_parse_amount(f[1])
    if not f[1] or ob is None:
        return "Opening balance sahi number likho (paise tak, jaise 25000 ya -1500.50). Example: " + _TG_ACCT_FMT
    if f[2]:
        od = _tg_parse_date(f[2], ctx["today"])
        if not od:
            return "Date samajh nahi aayi. Aise likho: `1 oct 2026`, `01-10-2026` ya `today`. Example: " + _TG_ACCT_FMT
    else:
        od = ctx["today"]
    now = _tg_now_ms()
    with _ACCOUNTS_LOCK:
        data = _accounts_read_disk() or {"v": 1, "rev": 0, "accounts": [], "entries": [], "t": 0}
        if not isinstance(data.get("accounts"), list):
            data["accounts"] = []
        if not isinstance(data.get("entries"), list):
            data["entries"] = []
        key = _tg_norm(name)
        if any(isinstance(a, dict) and _tg_norm(a.get("name")) == key for a in data["accounts"]):
            return f"\"{name}\" naam ka account pehle se hai. Alag naam rakho."
        iid = _tg_new_id("ac", now)
        data["accounts"].append({"id": iid, "name": name, "opening_balance": ob, "opening_date": od, "bank_name": "",
                                 "last4": "", "archived": False, "created_at": now})
        data["t"] = now
        ok, msg, _rev, stale = _accounts_save(data)
    if not ok:
        return "⚠️ Save nahi ho paya, kuch change nahi hua. Dobara try karo."
    shown = f"{name} · {_tg_inr(_tg_paise(ob))} · {_tg_fmt_long(od)}"
    with _TG_UNDO_LOCK:        # accounts lock chhodne ke BAAD (lock order: undo ka _ACCOUNTS_LOCK andar leta hai)
        st = _tg_undo_load()
        st.append({"kind": "account_add", "id": iid, "text": shown, "at": now})
        _tg_undo_save(st)
    return f"💰 Account add ho gaya: {shown}\n(wapas lene ke liye `undo` likho)"

def _tg_undo_account(rec) -> tuple:
    """-> (done: bool, msg). done False = stack me rehne do (dobara try)."""
    txt = _tg_one(rec.get("text"), 120)
    with _ACCOUNTS_LOCK:
        data = _accounts_read_disk()
        accs = data.get("accounts") if isinstance(data, dict) and isinstance(data.get("accounts"), list) else []
        idx = next((j for j, a in enumerate(accs) if isinstance(a, dict) and a.get("id") == rec.get("id")), -1)
        if idx < 0:
            return True, f"Account app me ab nahi hai (pehle hi hat chuka): {txt}"
        if any(isinstance(e, dict) and e.get("account_id") == rec.get("id") for e in (data.get("entries") or [])):
            return True, f"Is account me entries ban chuki hain, isliye undo nahi kiya: {txt}\n(app ke Accounts tab se hi hatao)"
        del accs[idx]
        data["t"] = _tg_now_ms()
        ok, _m, _r, _s = _accounts_save(data)
    if not ok:
        return False, "⚠️ Undo save nahi ho paya. `undo` dobara likho."
    return True, f"↩️ Undo: Account hata diya: {txt}"
_TG_UNDO_KINDS["account_add"] = lambda b, rec: (None, "")   # placeholder — asli undo `_tg_h_undo` me `_tg_undo_account` se hota hai
# ═══ ACCOUNT-ADD END ═══

# (prefix tuple, handler) — lamba / khaas pehle. Prefix ke shabd poore match hone chahiye (todo != todos).
_TG_COMMANDS = (
    (("todo", "add"), _tg_h_todo_add),         # 10B (MYENGINE-STEP-10B)
    (("todo", "done"), _tg_h_todo_done),       # 10B
    (("rough", "add"), _tg_h_rough_add),       # 10B
    (("info", "add"), _tg_h_info_add),         # 10C (MYENGINE-STEP-10C)
    (("travel", "add"), _tg_h_travel_add),     # 10C
    (("account", "add"), _tg_h_account_add),   # ACCOUNT-ADD (2026-10-03)
    (("undo",), _tg_h_undo),                    # 10B
    (("today", "plan"), _tg_h_plan),
    (("aaj", "ka", "plan"), _tg_h_plan),
    (("help",), _tg_h_help),
    (("todo",), _tg_h_todo),
    (("rough",), _tg_h_rough),
    (("info",), _tg_h_info),
    (("travel",), _tg_h_travel),
    (("balance",), _tg_h_balance),
)

def _tg_normalize_cmd(text: str) -> str:
    """'/plan', '/todo@MyBot' -> 'plan', 'todo' (slash hatao, @bot hatao)."""
    t = " ".join(str(text or "").split())
    if t.startswith("/"):
        t = t[1:]
        first, _sp, rest = t.partition(" ")
        first = first.split("@", 1)[0]
        t = (first + (" " + rest if rest else "")).strip()
    return t

def _tg_dispatch(text: str, today: str, update_id=None) -> str:
    """Ek message -> jawab text. Kabhi exception nahi uchhalta (galti par saaf Hinglish jawab)."""
    try:
        _sr = _tgx_sets_try(text)                    # PRACTICE-SETS: label dabane ke baad number = sets
        if _sr is None:
            _sr = _tgx_skip_reason_try(text)         # SKIP: ⏸ ke baad ka agla normal message = wajah
        if _sr is not None:
            return _tg_cap(_sr)
        bulk = _tg_bulk_parse(text)                   # TGX2: kai lines ek saath (todo add / + / rough add / r)
        if bulk is not None:
            return _tg_cap(_tg_bulk_add(bulk[0], bulk[1], today))
        t = _tg_shortcut(_tg_normalize_cmd(text))     # TGX2: `+ kaam` = todo add, `r text` = rough add
        if not t:
            return ""
        low = t.lower()
        words = low.split()
        for prefix, handler in _TG_COMMANDS:
            n = len(prefix)
            if tuple(words[:n]) == prefix:
                args = " ".join(t.split()[n:])        # original case, prefix ke baad
                return _tg_cap(handler({"text": text, "args": args, "today": today, "update_id": update_id}) or "")
        if _tg_is_plan_text(words):                   # sabse AKHIR me
            return _tg_cap(_tg_h_plan({"text": text, "args": "", "today": today, "update_id": update_id}) or "")
        return "Samajh nahi aaya. `help` likho."
    except Exception as e:
        _slog_exception("tg_dispatch", e)
        return "⚠️ Kuch gadbad hui, dobara try karo."

def _tg_seen_check(update_id) -> bool:
    """True = ye update_id pehle aa chuka (duplicate, ignore karo). Warna yaad kar leta hai (bounded).
    10B ke write commands ke liye bhi yahi guard: pehle mark, phir kaam (retry par double write nahi)."""
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        return False
    with _TG_LOCK:
        if update_id in _TG_SEEN:
            return True
        _TG_SEEN[update_id] = time.time()
        while len(_TG_SEEN) > _TG_SEEN_MAX:
            _TG_SEEN.pop(next(iter(_TG_SEEN)))
    return False

def _tg_cmd_api(raw: bytes) -> tuple:
    """/api/cmd ka kaam (secret check handler me ho chuka). -> (http_code, obj)."""
    try:
        data = json.loads((raw or b"{}").decode("utf-8") or "{}")
    except Exception:
        return 400, {"ok": False, "msg": "bad json"}
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return 400, {"ok": False, "msg": "text chahiye"}
    text = data["text"].strip()
    if not text:
        return 200, {"ok": True, "text": ""}
    if len(text) > _TG_MAX_TEXT_IN:
        return 200, {"ok": True, "text": "Message bahut lamba hai. `help` likho."}
    uid = data.get("update_id")
    if _tg_seen_check(uid):
        return 200, {"ok": True, "dup": True, "text": ""}
    return 200, {"ok": True, "text": _tg_dispatch(text, _tg_today(), uid if isinstance(uid, int) else None)}
# ═══ MYENGINE-STEP-10A END ═══

# ═══ TGX (2026-10-03): 🤖 Telegram remote ke naye features (pehle alag file thi, ab yahin — sab ek hi app.py me) ═══
# Commands : habit / routine / dip (list + done + miss), practice (sets), todo del, rough del, backup, cb (button dabane par)
# Buttons  : shaam 7 PM ko Telegram par har pending kaam ke ✅/❌ inline buttons (_tgx_send_checklist)
# Backup   : roz raat 10 PM zip (routine, accounts, reminders ...) Telegram par + 7 din ke local snapshots
# Undo     : mark / practice / item_del ke undo (`undo` command _tgx_undo_special bulata hai)
# Naam ka niyam: is block ke saare naam `_tgx_` / `_TGX_` se shuru hote hain (purane code se takraav nahi).
_TGX_SPECIAL_UNDO = ("mark", "practice", "item_del", "skip")

_TGX_KINDS = {                       # command naam -> (item kind, title)
    "routine": ("r", "🌅 Routine"),
    "habit": ("h", "🔁 Habit"),
    "dip": ("dp", "👦 Dipanshu"),
}

_TGX_ICON = {"d": "✅", "m": "❌", "": "⬜", "x": "⏸"}

_TGX_KEMO = {"r": "🌅", "t": "📌", "h": "🔁", "dp": "👦", "p": "🎯", "s": "🗓", "o": "🏖"}   # (2026-10-03) Practice + Sunday bhi

BACKUP_HOUR, BACKUP_MIN = 22, 0

_TGX_BACKUP_GRACE_SEC = 3 * 3600

_TGX_BACKUP_KEEP_DAYS = 7

_TGX_BACKUP_MAX_RAW = 8 * 1024 * 1024

_TGX_BACKUP_ESSENTIAL = ("routine", "accounts", "reminders", "moneymgr")

_TGX_BACKUP_OPTIONAL = ("settings", "diary", "layout", "state", "routine_skips", "holiday")

def _tgx_recent(rlog):
    cutoff = int((time.time() - _ROUTINE_RLOG_KEEP_SEC) * 1000)
    out = []
    for x in rlog:
        try:
            if isinstance(x, dict) and int(x.get("t") or 0) >= cutoff:
                out.append(x)
        except Exception:
            pass
    return out

def _tgx_write_entries(entries, undo=None):
    """Kai rlog entries ek saath (ek hi save). -> (ok, err). Lock order: _ROUTINE_LOCK phir _TG_UNDO_LOCK (app.py jaisa)."""
    if not entries:
        return True, ""
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return False, err
        for e in entries:
            e["t"] = _tg_now_ms()
            _routine_apply_entry(b, e)
        b["rlog"] = _tgx_recent(b.get("rlog") or []) + list(entries)
        b["t"] = max(int(e["t"]) for e in entries)
        ok, _msg = _local_state_save_raw("routine", b)
        if not ok:
            return False, "⚠️ Save nahi ho paya, kuch change nahi hua. Dobara try karo."
        if undo:
            with _TG_UNDO_LOCK:
                st = _tg_undo_load()
                st.append(dict(undo, at=b["t"]))
                _tg_undo_save(st)
    return True, ""

def _tgx_today_items(b, today, k):
    return [i for i in _routine_collect(b, today, dip=True) if i["k"] == k]

def _tgx_streak(b, hid, today):
    """chart.html `_rtHStreak` jaisa: scheduled dino ko ginte hue peeche; aaj pending ho to streak nahi toot-ta."""
    h = next((x for x in (b.get("habits") or []) if isinstance(x, dict) and str(x.get("id")) == str(hid)), None)
    if not h:
        return 0
    hd = b.get("hdone") if isinstance(b.get("hdone"), dict) else {}
    d = datetime.date.fromisoformat(today)
    n = 0
    for i in range(400):
        if ((d.weekday() + 1) % 7) in (h.get("days") or []):
            if (hd.get(d.isoformat()) or {}).get(str(h.get("id"))):
                n += 1
            elif i > 0:
                break
        d -= datetime.timedelta(days=1)
    return n

def _tgx_pick(its, arg, cmd):
    """arg = 'all' | '1 2 3' | '1,3' | '2-4' | naam ka hissa -> (selected items, err)."""
    a = " ".join(str(arg or "").split())
    if not a:
        return None, f"Number ya naam likho. Pehle `{cmd}` se list dekho."
    if a.lower() in ("all", "sab"):
        sel = [i for i in its if i["s"] == ""]
        return (sel, "") if sel else (None, "Sab items pehle se marked hain.")
    toks = [t for t in re.split(r"[,\s]+", a) if t]
    if all(re.fullmatch(r"[0-9]{1,3}(-[0-9]{1,3})?", t) for t in toks):
        idxs = []
        for t in toks:
            if "-" in t:
                lo, hi = (int(x) for x in t.split("-"))
                if hi < lo or hi - lo > 60:
                    return None, f"'{t}' sahi range nahi hai."
                idxs += list(range(lo, hi + 1))
            else:
                idxs.append(int(t))
        for n in idxs:
            if n < 1 or n > len(its):
                return None, f"Number {n} list me nahi hai (1 se {len(its)} tak)." if its else "Aaj ke liye is list me kuch nahi hai."
        seen, sel = set(), []
        for n in idxs:
            if n not in seen:
                seen.add(n)
                sel.append(its[n - 1])
        return sel, ""
    low = a.lower()
    exact = [i for i in its if i["label"].strip().lower() == low]
    if len(exact) == 1:
        return exact, ""
    m = [i for i in its if low in i["label"].lower()]
    if len(m) == 1:
        return m, ""
    if not m:
        return None, f"'{a[:40]}' naam ka kuch nahi mila. `{cmd}` se list dekho."
    return None, "Kai match mile: " + " · ".join(i["label"][:30] for i in m[:5]) + "\nThoda aur poora naam ya number likho."

def _tgx_reply_list(cmd, b, today):
    k, title = _TGX_KINDS[cmd]
    its = _tgx_today_items(b, today, k)
    if not its:
        return f"{title}: aaj koi item nahi hai."
    done = sum(1 for i in its if i["s"] == "d")
    lines = []
    for n, i in enumerate(its, 1):
        sub = f" ({i['sub']})" if i["sub"] else ""
        st = ""
        if k == "h":
            s = _tgx_streak(b, i["id"], today)
            if s > 0:
                st = f" 🔥{s}"
        why = f" — {i['why']}" if i["s"] == "m" and i.get("why") else ""
        lines.append(f"{n}. {_TGX_ICON.get(i['s'], '⬜')} {i['label']}{sub}{st}{why}")
    kept, hidden = _tg_fit_lines(lines, _TG_MAX_REPLY - 250, keep="first")
    txt = f"{title} — aaj {done}/{len(its)} done\n" + "\n".join(kept)
    if hidden:
        txt += f"\n…aur {hidden} (list lambi hai)"
    txt += f"\n\n`{cmd} done 1 2` · `{cmd} done all` · `{cmd} miss 3 | wajah`"
    return txt

def _tgx_h_list(cmd):
    def h(ctx):
        a = ctx["args"].strip()
        if a:
            return f"Samajh nahi aaya. `{cmd}` (list), `{cmd} done <number>` ya `{cmd} miss <number> | wajah` likho."
        with _ROUTINE_LOCK:
            b, err = _tg_read_blob()
            if err:
                return err
            return _tgx_reply_list(cmd, b, ctx["today"])
    return h

def _tgx_apply_marks(cmd, sel, status, why, today, b_items_k):
    k, title = _TGX_KINDS[cmd]
    ents, undo_items, labels, skipped = [], [], [], []
    for i in sel:
        if i["s"] == status and (status == "d" or (i.get("why") or "") == why):
            skipped.append(i["label"])
            continue
        ents.append({"k": k, "id": str(i["id"]), "d": today, "s": status, "why": why if status == "m" else ""})
        undo_items.append({"id": str(i["id"]), "prev": i["s"], "why": i.get("why") or ""})
        labels.append(i["label"])
    if not ents:
        return "Ye pehle se aise hi marked hain: " + ", ".join(s[:30] for s in skipped[:5])
    undo = {"kind": "mark", "k": k, "d": today, "text": ", ".join(s[:30] for s in labels[:3]) + (" …" if len(labels) > 3 else ""),
            "items": undo_items}
    ok, err = _tgx_write_entries(ents, undo=undo)
    if not ok:
        return err
    ic = _TGX_ICON[status]
    head = f"{ic} {title}: " + ", ".join(s[:40] for s in labels[:6]) + (f" …(+{len(labels) - 6})" if len(labels) > 6 else "")
    if status == "m":
        head += f"\nWajah: {why}"
    if skipped:
        head += f"\n(pehle se marked, chhode: {len(skipped)})"
    return head + "\n(wapas lene ke liye `undo` likho)"

def _tgx_h_mark(cmd, status):
    def h(ctx):
        arg, why = ctx["args"], ""
        if status == "m":
            parts = _tg_split_fields(arg)
            arg = parts[0]
            why = _tg_one(" ".join(p for p in parts[1:] if p), 300)
            if not why:
                return f"Wajah bhi likho: `{cmd} miss 2 | wajah`"
        k, _title = _TGX_KINDS[cmd]
        with _ROUTINE_LOCK:
            b, err = _tg_read_blob()
            if err:
                return err
            its = _tgx_today_items(b, ctx["today"], k)
            if not its:
                return f"Aaj ke liye is list me kuch nahi hai."
            sel, err = _tgx_pick(its, arg, cmd)
            if err:
                return err
            return _tgx_apply_marks(cmd, sel, status, why, ctx["today"], k)
    return h

def _tgx_practice_rows(b, today):
    dd = datetime.date.fromisoformat(today)
    pl = (b.get("plog") or {}).get(today, {}) if isinstance(b.get("plog"), dict) else {}
    rows = []
    for st in _practice_steps(b):
        s0 = _prac_start(st)
        if dd < s0:
            continue
        wk0 = s0 + datetime.timedelta(days=7 * ((dd - s0).days // 7))
        sid = str(st.get("id"))
        _m, w_t, d_t = _prac_targets(st)
        try:
            cur = float(pl.get(sid) or 0)
        except Exception:
            cur = 0.0
        rows.append({"id": sid, "label": str(st.get("label", "Practice")), "cur": cur, "d_t": d_t, "w_t": w_t,
                     "wk": _prac_sum(b, sid, wk0, dd)})
    return rows

def _tgx_h_practice(ctx):
    a = ctx["args"].strip()
    today = ctx["today"]
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        rows = _tgx_practice_rows(b, today)
        if not rows:
            return "🎯 Abhi koi Practice step shuru nahi hua."
        if not a:
            lines = [f"{n}. {'✅' if r['cur'] > 0 else '⬜'} {r['label']} — aaj {_fmt_n(r['cur'])} sets · target {r['d_t']:.1f}/din · hafte me {_fmt_n(r['wk'])}/{_fmt_n(r['w_t'])}"
                     for n, r in enumerate(rows, 1)]
            return "🎯 Practice (aaj):\n" + "\n".join(lines) + "\n\n`practice 1 3` (3 sets) · `practice 1 +1` (ek aur)"
        m = re.fullmatch(r"([0-9]{1,2})\s+(\+?)([0-9]{1,4}(?:\.[0-9])?)", a)
        if not m:
            return "Aise likho: `practice 1 3` (aaj ke total 3 sets) ya `practice 1 +1` (ek aur). `practice` se list dekho."
        n, plus, val = int(m.group(1)), m.group(2) == "+", float(m.group(3))
        if n < 1 or n > len(rows):
            return f"Number {n} list me nahi hai (1 se {len(rows)} tak)."
        r = rows[n - 1]
        new = r["cur"] + val if plus else val
        new = int(new) if new == int(new) else round(new, 1)
        if new > 1000:
            return "Sets bahut zyada hain."
        if new <= 0:
            return "0 sets ke liye checklist ka 0 button dabao ya `tick` likho (wajah bhi wahin se jaati hai). Galti se likha ho to `undo` use karo."
        ent = {"k": "p", "id": r["id"], "d": today, "s": "d", "n": new, "why": ""}
        undo = {"kind": "practice", "id": r["id"], "d": today, "prev": r["cur"], "text": r["label"]}
        ok, err = _tgx_write_entries([ent], undo=undo)
        if not ok:
            return err
        wk = r["wk"] - r["cur"] + new
        _tgx_why_drop("p", r["id"], today)
        _lowsets_clear(today, r["id"])
        hint = ""
        if new <= _TGX_LOW_SETS:
            _tgx_why_ask("p", r["id"], today, r["label"], new, notify=False)
            hint = "\n📝 Ab wajah ek message me likho."
        return (f"🎯 {r['label']}: aaj {_fmt_n(new)} sets (target {r['d_t']:.1f}) · hafte me {_fmt_n(wk)}/{_fmt_n(r['w_t'])}\n"
                "(wapas lene ke liye `undo` likho)" + hint)

def _tgx_h_todo_del(ctx):
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        it, err = _tg_todo_pick(ctx["args"], b, ctx["today"])
        if err:
            return err
        iid, text = str(it.get("id")), _tg_one(it.get("text"), 120)
        ok, err = _tg_write("t", "r", iid, undo={"kind": "item_del", "w": "t", "id": iid, "item": dict(it), "text": text})
    if not ok:
        return err
    return f"🗑 To-Do hata diya: {text}\n(wapas lene ke liye `undo` likho)"

def _tgx_h_rough_del(ctx):
    a = ctx["args"].strip()
    if not re.fullmatch(r"[0-9]{1,4}", a):
        return "Number likho, jaise `rough del 2` (number `rough` list wala)."
    n = int(a)
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        items = [x for x in (b.get("rough") or []) if isinstance(x, dict) and str(x.get("text", "")).strip()]
        if n < 1 or n > len(items):
            return f"Number {n} list me nahi hai (1 se {len(items)} tak). `rough` likho." if items else "Rough list khaali hai."
        it = items[n - 1]
        iid, text = str(it.get("id")), _tg_one(it.get("text"), 200)
        ok, err = _tg_write("g", "r", iid, undo={"kind": "item_del", "w": "g", "id": iid, "item": dict(it), "text": text})
    if not ok:
        return err
    return f"🗑 Rough hata diya: {text}\n(wapas lene ke liye `undo` likho)"

def _tgx_undo_special(rec):
    """-> (done: bool, msg). done=False ho to record stack me rehta hai."""
    kind = rec.get("kind")
    text = _tg_one(rec.get("text"), 120)
    if kind == "mark":
        ents = []
        for it in rec.get("items") or []:
            prev = it.get("prev") or ""
            if prev == "d":
                ents.append({"k": rec["k"], "id": it["id"], "d": rec["d"], "s": "d", "why": ""})
            elif prev == "m":
                ents.append({"k": rec["k"], "id": it["id"], "d": rec["d"], "s": "m", "why": it.get("why") or "undo"})
            else:
                ents.append({"k": rec["k"], "id": it["id"], "d": rec["d"], "s": "u", "why": ""})
            _tgx_why_drop(rec["k"], str(it["id"]), rec["d"])
        ok, err = _tgx_write_entries(ents)
        return (True, f"↩️ Undo: mark wapas liya: {text}") if ok else (False, err)
    if kind == "skip":
        with _SKIP_LOCK:
            dm = _skip_all()
            (dm.get(rec.get("d")) or {}).pop(f"{rec.get('k')}|{rec.get('id')}", None)
            ok = _skip_save(dm)
            _TGX_SKIP_PENDING[:] = [p for p in _TGX_SKIP_PENDING if not (p["k"] == rec.get("k") and p["id"] == rec.get("id") and p["d"] == rec.get("d"))]
        return (True, f"↩️ Undo: skip hata diya: {text}") if ok else (False, "⚠️ Undo save nahi hua.")
    if kind == "practice":
        prev = float(rec.get("prev") or 0)
        if prev > 0:
            n = int(prev) if prev == int(prev) else round(prev, 1)
            ent = {"k": "p", "id": rec["id"], "d": rec["d"], "s": "d", "n": n, "why": ""}
        else:
            ent = {"k": "p", "id": rec["id"], "d": rec["d"], "s": "u", "why": ""}
        ok, err = _tgx_write_entries([ent])
        _tgx_why_drop("p", str(rec["id"]), rec["d"])
        _lowsets_clear(rec["d"], str(rec["id"]))
        return (True, f"↩️ Undo: Practice wapas: {text}") if ok else (False, err)
    if kind == "item_del":
        w, iid, item = str(rec.get("w") or ""), str(rec.get("id") or ""), rec.get("item")
        b, err = _tg_read_blob()
        if err:
            return False, err
        _lst, idx = _tg_find(b, w, iid)
        if idx >= 0:
            return True, f"Wo item app me pehle se hai: {text}"
        if not isinstance(item, dict) or str(item.get("id")) != iid:
            return True, "Purana item wapas nahi laa sakta (data nahi bacha)."
        ok, err = _tg_write(w, "a", iid, item=item)
        return (True, f"↩️ Undo: wapas jod diya: {text}") if ok else (False, err)
    return True, "Ye undo layak nahi tha."

def _tgx_relay_ok():
    return bool(TELEGRAM_RELAY_URL and RELAY_SECRET)

def _tgx_send_with_keyboard(text, keyboard):
    """inline keyboard wala message. -> 'ok' ya error text."""
    markup = {"inline_keyboard": keyboard}
    if _tgx_relay_ok():
        try:
            r = requests.post(f"{TELEGRAM_RELAY_URL}/send", headers={"X-Relay-Secret": RELAY_SECRET},
                              json={"text": text, "reply_markup": markup}, timeout=70)
            if r.status_code == 200 and r.json().get("ok"):
                return "ok"
            return f"err: relay HTTP {r.status_code} {r.text[:200]}"
        except Exception as e:
            return f"err: relay {type(e).__name__}: {e}"
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return "skip: Telegram set nahi"
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "reply_markup": markup}, timeout=15)
        return "ok" if (r.status_code == 200 and r.json().get("ok")) else f"err: HTTP {r.status_code} {r.text[:200]}"
    except Exception as e:
        return f"err: {type(e).__name__}: {str(e).replace(TELEGRAM_BOT_TOKEN, '***')}"

def _tgx_send_document(filename, data, caption=""):
    """file Telegram par. -> 'ok' ya error text."""
    if _tgx_relay_ok():
        try:
            r = requests.post(f"{TELEGRAM_RELAY_URL}/senddoc", headers={"X-Relay-Secret": RELAY_SECRET},
                              json={"filename": filename, "caption": caption[:900], "b64": base64.b64encode(data).decode("ascii")},
                              timeout=120)
            if r.status_code == 200 and r.json().get("ok"):
                return "ok"
            return f"err: relay HTTP {r.status_code} {r.text[:200]}"
        except Exception as e:
            return f"err: relay {type(e).__name__}: {e}"
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return "skip: Telegram set nahi"
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                          data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:900]},
                          files={"document": (filename, data, "application/zip")}, timeout=60)
        return "ok" if (r.status_code == 200 and r.json().get("ok")) else f"err: HTTP {r.status_code} {r.text[:200]}"
    except Exception as e:
        return f"err: {type(e).__name__}: {str(e).replace(TELEGRAM_BOT_TOKEN, '***')}"

# ═══ PRACTICE-SETS (2026-10-03): Telegram checklist me Practice par "kitne sets hue?" ═══
_TGX_SETS_PENDING = []          # [{id,d,label,ts}] — agla message (sirf number) us Practice ke sets banta hai (FIFO)

def _tgx_set_practice(iid, d, n):
    """Practice step `iid` ke din `d` ke total sets = n (n==0 -> 0 sets, missed). -> (ok, err, label)."""
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return False, err, ""
        it = next((i for i in _routine_collect(b, d, sunday=True, dip=True) if i["k"] == "p" and str(i["id"]) == iid), None)
        if not it:
            return False, "⚠️ Ye Practice ab app me nahi mila.", ""
        try:
            cur = float(it.get("n") or 0) if it["s"] == "d" else 0.0
        except Exception:
            cur = 0.0
        ent = {"k": "p", "id": iid, "d": d, "s": "d" if n > 0 else "m", "n": n,
               "why": "" if n > 0 else "Telegram (0 sets)"}
        undo = {"kind": "practice", "id": iid, "d": d, "prev": cur, "text": it["label"]}
        ok, err = _tgx_write_entries([ent], undo=undo)
    if ok:
        _tgx_why_drop("p", iid, d)          # sets badle -> purani wajah / pending ab laagu nahi
        _lowsets_clear(d, iid)
    return ok, err, it["label"]

def _tgx_sets_try(text):
    """Sets ka intezaar ho aur message sirf number ho (jaise `3`, `2.5`, `3 sets`) to wahi sets hain. Warna None."""
    now = time.time()
    with _SKIP_LOCK:
        pend = [p for p in _TGX_SETS_PENDING if now - p["ts"] <= _TGX_SKIP_WAIT_SEC]
        _TGX_SETS_PENDING[:] = pend
        if not pend:
            return None
        m = re.fullmatch(r"\s*([0-9]{1,4}(?:\.[0-9])?)\s*(?:sets?)?\s*", str(text or "").lower())
        if not m:
            return None
        cur = pend.pop(0)
        _TGX_SETS_PENDING[:] = pend
    val = float(m.group(1))
    n = int(val) if val == int(val) else round(val, 1)
    if n > 1000:
        with _SKIP_LOCK:
            _TGX_SETS_PENDING.insert(0, cur)
        return "Sets bahut zyada hain, sahi number likho."
    ok, err, label = _tgx_set_practice(cur["id"], cur["d"], n)
    if not ok:
        return err
    msg = (f"🎯 {label}: {_fmt_n(n)} sets ✅" if n > 0 else f"🎯 {label}: 0 sets (nahi hua) ❌") + "\n(wapas lene ke liye `undo` likho)"
    if n <= _TGX_LOW_SETS:
        _tgx_why_ask("p", cur["id"], cur["d"], label, n, notify=False)
        msg += "\n📝 Ab wajah ek message me likho."
    with _SKIP_LOCK:
        nxt = _TGX_SETS_PENDING[0] if _TGX_SETS_PENDING else None
    if nxt:
        msg += f"\n\nAb agla: 🎯 {nxt['label']} — kitne sets hue? (number likho)"
    return msg

def _tgx_cb_sets(iid, d, s):
    """Practice buttons: s = n1..n5 (seedha itne sets) ya nq (number likhne ke liye puchho). Toast ✅/🎯 se shuru (relay row hata deta hai)."""
    if s == "nq":
        with _ROUTINE_LOCK:
            b, err = _tg_read_blob()
            if err:
                return err
            it = next((i for i in _routine_collect(b, d, sunday=True, dip=True) if i["k"] == "p" and str(i["id"]) == iid), None)
        if not it:
            return "⚠️ Ye Practice ab app me nahi mila."
        label = it["label"][:60]
        with _SKIP_LOCK:
            first = not [p for p in _TGX_SETS_PENDING if time.time() - p["ts"] <= _TGX_SKIP_WAIT_SEC]
            _TGX_SETS_PENDING.append({"id": iid, "d": d, "label": label, "ts": time.time()})
        if first:
            try:
                _alert_via_telegram(f"🎯 {label}: kitne sets hue? Sirf number likho (jaise 3). 0 likhoge to 'nahi hua' ginega.")
            except Exception as e:
                _slog_exception("sets prompt", e)
        return f"🎯 {label} — kitne sets? Number likho"
    n = int(s[1:])
    ok, err, label = _tgx_set_practice(iid, d, n)
    if not ok:
        return err
    if n <= _TGX_LOW_SETS:
        _tgx_why_ask("p", iid, d, label, n, notify=True)
        return f"{'✅' if n > 0 else '❌'} {label[:60]} — {n} sets — wajah likho"
    return f"✅ {label[:60]} — {n} sets"

# ═══ SKIP (2026-10-03): ⏸ Telegram skip button — item us din ke hisaab se bahar (success % me minus nahi) ═══
# Alag file (routine.json ka browser-blob nahi) taaki chart.html purana rehne par bhi data na khoye.
# routine_skips.json = {"v":1,"d":{date:{"k|id":{"why":str,"t":ms}}}}   k = r/t/h/dp/p/s
_SKIP_LOCK = threading.RLock()
_SKIP_FILE = os.path.join(LOCAL_STATE_DIR, "routine_skips.json")
_TGX_SKIP_PENDING = []          # [{k,id,d,label,ts}] — wajah ka intezaar (agla normal message wajah banta hai)
_TGX_SKIP_WAIT_SEC = 1800

def _skip_all() -> dict:
    try:
        if os.path.exists(_SKIP_FILE):
            with open(_SKIP_FILE, "r", encoding="utf-8") as f:
                x = json.load(f)
            if isinstance(x, dict) and isinstance(x.get("d"), dict):
                return x["d"]
    except Exception as e:
        _slog_exception("_skip_all", e)
    return {}

def _skip_save(dm: dict) -> bool:
    cut = (_ist_now().date() - datetime.timedelta(days=400)).isoformat()
    dm = {k: v for k, v in dm.items() if k >= cut and v}
    tmp = _SKIP_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(_SKIP_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "d": dm}, f, ensure_ascii=False)
        os.replace(tmp, _SKIP_FILE)
        return True
    except Exception as e:
        _slog_exception("_skip_save", e)
        return False

# ═══ WHY (2026-10-06): ❌ Nahi hua par aur 🎯 Practice 0/1 set par bhi wajah puchho ═══
# ❌ ya 0/1 set dabane par bot wajah maangta hai; agla normal message wajah ban jaata hai (skip jaisa hi, ek message sab pending par).
# 0 set -> pmiss.why (blob) · 1 set -> routine_lowsets.json {"v":1,"d":{date:{stepId:{"n","why","t"}}}} (plog me sirf number rehta hai)
_TGX_WHY_PENDING = []           # [{k,id,d,label,n,ts}]
_TGX_LOW_SETS = 1               # itne ya kam sets par wajah puchho
_LOWSETS_FILE = os.path.join(LOCAL_STATE_DIR, "routine_lowsets.json")

def _lowsets_all() -> dict:
    try:
        if os.path.exists(_LOWSETS_FILE):
            with open(_LOWSETS_FILE, "r", encoding="utf-8") as f:
                x = json.load(f)
            if isinstance(x, dict) and isinstance(x.get("d"), dict):
                return x["d"]
    except Exception as e:
        _slog_exception("_lowsets_all", e)
    return {}

def _lowsets_save(dm: dict) -> bool:
    cut = (_ist_now().date() - datetime.timedelta(days=400)).isoformat()
    dm = {k: v for k, v in dm.items() if k >= cut and v}
    tmp = _LOWSETS_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(_LOWSETS_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "d": dm}, f, ensure_ascii=False)
        os.replace(tmp, _LOWSETS_FILE)
        return True
    except Exception as e:
        _slog_exception("_lowsets_save", e)
        return False

def _lowsets_clear(d: str, iid: str) -> None:
    """us din/step ka purana 1-set wajah hata do (sets badal gaye ya undo hua)."""
    with _SKIP_LOCK:
        dm = _lowsets_all()
        if iid in (dm.get(d) or {}):
            dm[d].pop(iid, None)
            _lowsets_save(dm)

def _tgx_why_drop(k: str, iid: str, d: str) -> None:
    with _SKIP_LOCK:
        _TGX_WHY_PENDING[:] = [p for p in _TGX_WHY_PENDING if not (p["k"] == k and p["id"] == iid and p["d"] == d)]

def _tgx_why_ask(k: str, iid: str, d: str, label: str, n=None, notify: bool = True) -> None:
    """wajah ka intezaar jodo (n = practice ke sets, baaki None). notify: koi aur pending na ho to Telegram par poochho."""
    label = str(label)[:60]
    now = time.time()
    with _SKIP_LOCK:
        live = [p for p in list(_TGX_SKIP_PENDING) + list(_TGX_WHY_PENDING) if now - p["ts"] <= _TGX_SKIP_WAIT_SEC]
        _TGX_WHY_PENDING[:] = [p for p in _TGX_WHY_PENDING if not (p["k"] == k and p["id"] == iid and p["d"] == d)]
        _TGX_WHY_PENDING.append({"k": k, "id": iid, "d": d, "label": label, "n": n, "ts": now})
    if notify and not live:
        try:
            if k == "p":
                head = f"🎯 {label}: {_fmt_n(n or 0)} set hue" + (" (nahi hua)" if not n else "")
            else:
                head = f"❌ {label} — nahi hua"
            _alert_via_telegram(head + "\nAb wajah ek message me likh do (kai pending hon to sab par wahi wajah lagegi). "
                                "Galti se dabaya ho to `undo` likho.")
        except Exception as e:
            _slog_exception("why prompt", e)

def _tgx_why_apply(wpend: list, why: str):
    """pending items par wajah likho. -> (names, err). Fail par err (pending wapas rakhna caller ka kaam)."""
    ents, hol, low, names = [], [], [], []
    for p in wpend:
        k = p["k"]
        if k == "o":
            hol.append(p)
        elif k == "p":
            if p.get("n"):
                low.append(p)
            else:
                ents.append({"k": "p", "id": p["id"], "d": p["d"], "s": "m", "n": 0, "why": why})
        else:
            ents.append({"k": k, "id": p["id"], "d": p["d"], "s": "m", "why": why})
    if ents:
        ok, err = _tgx_write_entries(ents)
        if not ok:
            return [], err or "⚠️ Wajah save nahi ho payi, dobara likho."
        names += [p["label"][:30] for p in wpend if p["k"] not in ("o",) and not (p["k"] == "p" and p.get("n"))]
    for p in hol:
        ok, _m = _hol_set_status(p["d"], p["id"], "m", why)
        if not ok:
            return names, "⚠️ Holiday wajah save nahi hui, dobara likho."
        names.append(p["label"][:30])
    if low:
        with _SKIP_LOCK:
            dm = _lowsets_all()
            for p in low:
                dm.setdefault(p["d"], {})[p["id"]] = {"n": p["n"], "why": why, "t": _tg_now_ms()}
                names.append(p["label"][:30])
            if not _lowsets_save(dm):
                return names, "⚠️ Wajah save nahi ho payi, dobara likho."
    return names, ""

def _skip_find(skips: dict, k: str, iid: str, d0: str, d1: str):
    """[d0, d1] ke beech kisi bhi din ka skip record (k,id) ke liye -> (date, rec) ya None."""
    key = f"{k}|{iid}"
    for dk in sorted(skips):
        if d0 <= dk <= d1 and isinstance(skips[dk], dict) and key in skips[dk]:
            return dk, skips[dk][key]
    return None

def _skip_lines(excused: list) -> list:
    if not excused:
        return []
    L = ["", "⏸ Skip hue (hisaab se bahar, reason ke saath):"]
    for d, ico, lbl, why in sorted(excused)[:14]:
        L.append(f"  • {_rep_dm(d)} {ico} {lbl}" + (f" — {why}" if why else ""))
    if len(excused) > 14:
        L.append(f"  …aur {len(excused) - 14} skip")
    return L

def _tgx_skip_reason_try(text):
    """Skip / ❌ / Practice 0-1 set dabane ke baad ka agla normal message = wajah (sab pending par). Command ho to None (command chalega)."""
    now = time.time()
    with _SKIP_LOCK:
        pend = [p for p in _TGX_SKIP_PENDING if now - p["ts"] <= _TGX_SKIP_WAIT_SEC]
        _TGX_SKIP_PENDING[:] = pend
        wpend = [p for p in _TGX_WHY_PENDING if now - p["ts"] <= _TGX_SKIP_WAIT_SEC]
        _TGX_WHY_PENDING[:] = wpend
        if not pend and not wpend:
            return None
    raw = (text or "").strip()
    if not raw or raw.startswith("/") or _tg_bulk_parse(raw) is not None:
        return None
    t = _tg_shortcut(_tg_normalize_cmd(raw))
    words = (t or "").lower().split()
    if not words:
        return None
    for prefix, _h in _TG_COMMANDS:
        if tuple(words[:len(prefix)]) == prefix:
            return None
    if _tg_is_plan_text(words):
        return None
    why = " ".join(raw.split())[:300]
    skip_names, miss_names = [], []
    if pend:
        with _SKIP_LOCK:
            dm = _skip_all()
            for p in pend:
                rec = (dm.get(p["d"]) or {}).get(f"{p['k']}|{p['id']}")
                if isinstance(rec, dict):
                    rec["why"] = why
                    skip_names.append(p["label"][:30])
            ok = _skip_save(dm)
            _TGX_SKIP_PENDING[:] = []
        if not ok:
            return "⚠️ Wajah save nahi ho payi, dobara likho."
    if wpend:
        miss_names, err = _tgx_why_apply(wpend, why)
        if err:
            return err                      # pending rehti hai (list abhi saaf nahi hui) — dobara likhne par phir koshish
        with _SKIP_LOCK:
            done = {(p["k"], p["id"], p["d"]) for p in wpend}
            _TGX_WHY_PENDING[:] = [p for p in _TGX_WHY_PENDING if (p["k"], p["id"], p["d"]) not in done]
    if skip_names and not miss_names:
        return f"⏸ Wajah likh li: {why}\n({', '.join(skip_names)})"
    return f"📝 Wajah likh li: {why}\n({', '.join(skip_names + miss_names)})"

def _tgx_cb_skip(k, iid, d):
    """⏸ button: item pending ho to skip record banao, wajah ka intezaar. Toast ⏸ se shuru (relay row hata deta hai)."""
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        it = next((i for i in _routine_collect(b, d, sunday=True, dip=True) if i["k"] == k and str(i["id"]) == iid), None)
    if not it:
        return "⚠️ Ye kaam ab app me nahi mila."
    label = it["label"][:60]
    if it["s"] in ("d", "m"):
        return f"⚠️ Pehle se {_TGX_ICON[it['s']]} mark hai: {label}"
    key = f"{k}|{iid}"
    with _SKIP_LOCK:
        dm = _skip_all()
        if key in (dm.get(d) or {}):
            return f"⏸ Pehle se skip: {label}"
        dm.setdefault(d, {})[key] = {"why": "", "t": _tg_now_ms()}
        if not _skip_save(dm):
            return "⚠️ Skip save nahi ho paya, dobara dabao."
        first = not [p for p in _TGX_SKIP_PENDING if time.time() - p["ts"] <= _TGX_SKIP_WAIT_SEC]
        _TGX_SKIP_PENDING.append({"k": k, "id": iid, "d": d, "label": label, "ts": time.time()})
    try:
        with _TG_UNDO_LOCK:
            st = _tg_undo_load()
            st.append({"kind": "skip", "k": k, "id": iid, "d": d, "text": label, "at": _tg_now_ms()})
            _tg_undo_save(st)
    except Exception as e:
        _slog_exception("skip undo push", e)
    if first:
        try:
            _alert_via_telegram(f"⏸ Skip: {label}\nAb wajah ek message me likh do (kai skip kiye hon to sab par wahi wajah lagegi). "
                                "Galti se dabaya ho to `undo` likho.")
        except Exception as e:
            _slog_exception("skip prompt", e)
    return f"⏸ {label} — skip. Ab wajah likho"

def _tgx_build_checklist(b, d):
    """pending (r / t / h / dp) items ke ✅/❌ buttons. -> (text, keyboard) ya None. callback_data = 'k|id|date|s' (<=64 bytes)."""
    items = [i for i in _routine_collect(b, d, sunday=True, dip=True) if i["k"] in _TGX_KEMO and i["s"] == ""]
    sk = _skip_all().get(d) or {}
    items = [i for i in items if f"{i['k']}|{i['id']}" not in sk]          # ⏸ skip wale dobara nahi
    items = [i for i in items if len(f"{i['k']}|{i['id']}|{d}|d".encode("utf-8")) <= 64]
    kb, used, nbtn = [], [], 0
    for i in items:
        need = 8 if i["k"] == "p" else 3          # Telegram: ek message me max 100 buttons
        if nbtn + need > 99:
            break
        nbtn += need
        used.append(i)
        base = f"{i['k']}|{i['id']}|{d}|"
        if i["k"] == "p":                         # 🎯 Practice: ✅/❌ nahi, "kitne sets?" — 1-5 seedha, ya label dabao aur number likho
            kb.append([{"text": f"🎯 {i['label'][:24]} — kitne sets?", "callback_data": base + "nq"}])
            kb.append([{"text": str(n), "callback_data": base + f"n{n}"} for n in range(0, 6)]
                      + [{"text": "⏸", "callback_data": base + "x"}])
        else:
            # naam poori chaudai ki apni row me (3 buttons ek row me hon to naam 8-10 akshar par kat jaata tha);
            # neeche ❌ / ⏸ ki row. Button count wahi (3/kaam), callback_data wahi — relay dono rows saath hatata hai.
            lab = f"{_TGX_KEMO[i['k']]} {i['label']}".strip()
            if len(lab) > 40:
                lab = lab[:39].rstrip() + "…"
            kb.append([{"text": "✅ " + lab, "callback_data": base + "d"}])
            kb.append([{"text": "❌ Nahi hua", "callback_data": base + "m"},
                       {"text": "⏸ Skip", "callback_data": base + "x"}])
    items = used
    if not items:
        return None
    dd = datetime.date.fromisoformat(d)
    text = (_top_note_prefix() + f"⏰ Raat ka hisaab — {dd.day}/{dd.month}: {len(items)} kaam baaki\n"
            "Button dabao: ✅ ho gaya · ❌ nahi hua (wajah likhni hai) · ⏸ skip (aaj mumkin nahi — hisaab se bahar, wajah baad me likhni hai).\n"
            "🎯 Practice: 0-5 button se sets chuno, ya upar wala label dabao aur number likho. ❌ / 0 / 1 set par wajah bhi likhni hai.")
    return text, kb

def _tgx_send_checklist(d):
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    built = _tgx_build_checklist(b, d)
    if not built:
        return "skip"
    return _tgx_send_with_keyboard(*built)

def _tgx_h_cb(ctx):
    """Button dabane par relay `/cb k|id|date|s` bhejta hai. Jawab = chhota toast (relay Telegram me dikhata hai)."""
    parts = ctx["args"].strip().split("|")
    if len(parts) != 4:
        return "⚠️ Button samajh nahi aaya."
    k, iid, d, s = parts
    if k not in _TGX_KEMO or not (s in ("d", "m", "x") or (k == "p" and re.fullmatch(r"n[0-5q]", s))) or not _tg_is_iso(d) or not iid:
        return "⚠️ Button samajh nahi aaya."
    gap = (datetime.date.fromisoformat(ctx["today"]) - datetime.date.fromisoformat(d)).days
    if gap < 0 or gap > 3:
        return "⚠️ Ye button purana ho gaya (3 din se zyada)."
    if s == "x":
        return _tgx_cb_skip(k, iid, d)
    if s.startswith("n"):
        return _tgx_cb_sets(iid, d, s)
    if k == "o":                                  # HOLIDAY: holiday.json me likho (routine blob me nahi)
        r_ = _hol_cb_mark(iid, d, s)
        if s == "m" and r_.startswith("❌") and "pehle se" not in r_[:12]:
            _hol_it = next((i for i in _hol_items(d, _hol_read()) if i["id"] == iid), None)
            _tgx_why_ask("o", iid, d, (_hol_it or {}).get("label") or "Holiday kaam", None)
            return r_ + " — wajah likho"
        if s == "d":
            _tgx_why_drop("o", iid, d)
        return r_
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        it = next((i for i in _routine_collect(b, d, sunday=True, dip=True) if i["k"] == k and str(i["id"]) == iid), None)
        if not it:
            return "⚠️ Ye kaam ab app me nahi mila."
        if it["s"] == s:
            return f"{_TGX_ICON[s]} pehle se: {it['label'][:60]}"
        why = "" if s == "d" else "Telegram button (wajah nahi di)"
        undo = {"kind": "mark", "k": k, "d": d, "text": it["label"][:60],
                "items": [{"id": iid, "prev": it["s"], "why": it.get("why") or ""}]}
        ent = {"k": k, "id": iid, "d": d, "s": s, "why": why}
        if k == "p":                       # Practice: ✅ = 1 set, ❌ = 0 sets
            ent["n"] = 1 if s == "d" else 0
        ok, err = _tgx_write_entries([ent], undo=undo)
    if not ok:
        return err
    if k == "p":                                  # purane Practice ✅/❌ buttons: 1 set / 0 set — wajah dono par
        _tgx_why_drop("p", iid, d)
        _lowsets_clear(d, iid)
    ask = (s == "m") or (k == "p" and s == "d")
    if ask:
        _tgx_why_ask(k, iid, d, it["label"], (0 if s == "m" else 1) if k == "p" else None)
    else:
        _tgx_why_drop(k, iid, d)
    return (f"{_TGX_ICON[s]} {it['label'][:60]}" + (" — 1 set" if (k == "p" and s == "d") else "")
            + (" — wajah likho" if ask else ""))

def _tgx_h_tick(ctx):
    """(2026-10-03) `tick` -> aaj ki pending kaam ki ✅/❌ checklist (7 PM wali) abhi bhejo. `tick kal` -> beete kal ki."""
    a = ctx["args"].strip().lower()
    today = ctx["today"]
    if a in ("", "aaj", "today"):
        d = today
    elif a in ("kal", "yesterday"):
        d = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    else:
        return "Aise likho: `tick` (aaj ki checklist) ya `tick kal` (beete kal ki)."
    res = _tgx_send_checklist(d)
    if res == "ok":
        return ""                       # checklist hi ek message hai; alag jawab nahi (relay khaali text ignore karta hai)
    if res == "skip":
        return "🎉 Koi kaam baaki nahi — sab mark ho chuka." if d == today else "🎉 Us din ka koi kaam baaki nahi."
    return f"⚠️ Checklist nahi bhej paya ({str(res)[:120]})."

def _tgx_backup_dir():
    p = os.path.join(LOCAL_STATE_DIR, "backups")
    os.makedirs(p, exist_ok=True)
    return p

def _tgx_backup_files():
    out, total = [], 0
    names = [(k + ".json") for k in _TGX_BACKUP_ESSENTIAL] + [(k + ".json") for k in _TGX_BACKUP_OPTIONAL] + ["reports.json"]
    for nm in names:
        p = os.path.join(LOCAL_STATE_DIR, nm)
        if not os.path.isfile(p):
            continue
        sz = os.path.getsize(p)
        if nm[:-5] in _TGX_BACKUP_ESSENTIAL or nm == "reports.json" or total + sz <= _TGX_BACKUP_MAX_RAW:
            out.append((nm, p))
            total += sz
    return out

def _tgx_make_backup(today):
    """-> (zip bytes, [file names]). Local snapshot bhi likhta hai aur 7 din se purane hatata hai."""
    files = _tgx_backup_files()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nm, p in files:
            try:
                with open(p, "rb") as f:
                    z.writestr(nm, f.read())
            except Exception as e:
                _slog_exception(f"backup read {nm}", e)
    data = buf.getvalue()
    try:
        bd = _tgx_backup_dir()
        with open(os.path.join(bd, f"backup_{today}.zip"), "wb") as f:
            f.write(data)
        cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=_TGX_BACKUP_KEEP_DAYS)).isoformat()
        for fn in os.listdir(bd):
            m = re.fullmatch(r"backup_([0-9]{4}-[0-9]{2}-[0-9]{2})\.zip", fn)
            if m and m.group(1) < cutoff:
                os.remove(os.path.join(bd, fn))
    except Exception as e:
        _slog_exception("backup snapshot", e)
    return data, [nm for nm, _p in files]

def _tgx_run_backup(today):
    data, names = _tgx_make_backup(today)
    persistent = os.path.isdir("/data")
    cap = f"💾 Backup {today} — {len(names)} file ({len(data) // 1024} KB)\n" + ", ".join(n[:-5] for n in names)
    if not persistent:
        cap += "\n⚠️ /data persistent nahi hai — Space restart par app ka data ud sakta hai. HF me persistent storage on karo."
    cap += "\nRestore: zip kholo, jo file wapas chahiye use app_state folder me rakho."
    res = _tgx_send_document(f"backup_{today}.zip", data, cap)
    _slog(f"[backup] {today} {len(data)} bytes -> {res}", level="ok" if res == "ok" else "err")
    return res

def _tgx_h_backup(ctx):
    res = _tgx_run_backup(ctx["today"])
    if res == "ok":
        return "💾 Backup upar bhej diya."
    if res.startswith("skip"):
        return "⚠️ Telegram set nahi, backup sirf server par rakha (backups folder)."
    return f"⚠️ Backup Telegram par nahi gaya ({res[:120]}). Server par snapshot rakha hai."

def _tgx_sent_file():
    return os.path.join(LOCAL_STATE_DIR, "backup_sent.json")

def _tgx_sent_load():
    try:
        if os.path.exists(_tgx_sent_file()):
            with open(_tgx_sent_file(), "r", encoding="utf-8") as f:
                x = json.load(f)
            return x if isinstance(x, dict) else {}
    except Exception as e:
        _slog_exception("backup sent load", e)
    return {}

def _tgx_sent_save(x):
    tmp = _tgx_sent_file() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(x, f)
        os.replace(tmp, _tgx_sent_file())
    except Exception as e:
        _slog_exception("backup sent save", e)

def _tgx_backup_loop():
    while True:
        sleep_for = 1800
        try:
            now = _ist_now()
            target = now.replace(hour=BACKUP_HOUR, minute=BACKUP_MIN, second=0, microsecond=0)
            age = (now - target).total_seconds()
            d = now.date().isoformat()
            if age < 0:
                sleep_for = min(1800, max(5.0, -age + 1))
            elif age <= _TGX_BACKUP_GRACE_SEC:
                sent = _tgx_sent_load()
                if not sent.get(d):
                    res = _tgx_run_backup(d)
                    if res == "ok" or res.startswith("skip"):
                        sent[d] = {"ts": time.time(), "res": res[:20]}
                        cut = (now.date() - datetime.timedelta(days=30)).isoformat()
                        _tgx_sent_save({k: v for k, v in sent.items() if k >= cut})
                    else:
                        sleep_for = _REMINDER_RETRY_SEC
        except Exception as e:
            _slog_exception("_tgx_backup_loop", e)
            sleep_for = 60
        time.sleep(sleep_for)

# Commands _TG_COMMANDS ke SAMNE (lamba / khaas prefix pehle). Ye block _TG_COMMANDS banne ke baad hi chalna chahiye.
_TG_COMMANDS = (
    (("habit", "done"), _tgx_h_mark("habit", "d")),
    (("habit", "miss"), _tgx_h_mark("habit", "m")),
    (("habit",), _tgx_h_list("habit")),
    (("routine", "done"), _tgx_h_mark("routine", "d")),
    (("routine", "miss"), _tgx_h_mark("routine", "m")),
    (("routine",), _tgx_h_list("routine")),
    (("dip", "done"), _tgx_h_mark("dip", "d")),
    (("dip", "miss"), _tgx_h_mark("dip", "m")),
    (("dip",), _tgx_h_list("dip")),
    (("practice",), _tgx_h_practice),
    (("todo", "del"), _tgx_h_todo_del),
    (("todo", "delete"), _tgx_h_todo_del),
    (("rough", "del"), _tgx_h_rough_del),
    (("rough", "delete"), _tgx_h_rough_del),
    (("backup",), _tgx_h_backup),
    (("cb",), _tgx_h_cb),
    (("tick",), _tgx_h_tick),
) + tuple(_TG_COMMANDS)
for _tgx_kind in _TGX_SPECIAL_UNDO:       # `undo` in kinds ko "undo layak" maane; asli kaam _tgx_undo_special()
    _TG_UNDO_KINDS[_tgx_kind] = lambda b, rec: (None, "")
if "TgBackup" not in {t.name for t in threading.enumerate()}:
    threading.Thread(target=_tgx_backup_loop, name="TgBackup", daemon=True).start()
# ═══ TGX END ═══

# ═══ TGX2 (2026-10-03): 🤖 Telegram — bulk add, shortcuts (+ / r), find, status ═══
# Sab purane helpers hi use karte hain (_tg_write, _tg_new_id, undo stack) — chart.html me kuch badalna nahi pada.
_TG_BOOT_TS = time.time()
_TG_BULK_MAX = 30
_TG_BULLET_RE = re.compile(r"^\s*(?:[-*•+]|\d{1,2}[.)])\s+")   # "- kaam", "• kaam", "1. kaam", "2) kaam" ka bullet hatao

def _tg_shortcut(t: str) -> str:
    """`+ doodh lana` -> `todo add doodh lana`, `r idea` -> `rough add idea`. Baaki text jaisa tha waisa."""
    s0 = (t or "").strip()
    if s0 == "+":
        return "todo add"
    if s0.startswith("+"):
        return "todo add " + s0[1:].strip()
    first, _sp, rest = s0.partition(" ")
    if first.lower() == "r" and rest.strip():
        return "rough add " + rest.strip()
    return t

def _tg_bulk_parse(text):
    """Multi-line message ho aur pehli line todo add / + / rough add / r / info add se shuru ho to -> (w, [lines]) (w: 't' todo, 'g' rough, 'i' info). Warna None."""
    raw = str(text or "").replace("\r", "\n")
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    if len(lines) < 2:
        return None
    first = _tg_normalize_cmd(lines[0])
    words = first.split()
    if not words:
        return None
    low = [x.lower() for x in words]
    if low[:2] == ["todo", "add"]:
        w, rest = "t", " ".join(words[2:])
    elif low[:2] == ["rough", "add"]:
        w, rest = "g", " ".join(words[2:])
    elif low[:2] == ["info", "add"]:                  # (2026-10-03) kai Info entries: har line `topic | label | date`
        w, rest = "i", " ".join(words[2:])
    elif first.startswith("+"):
        w, rest = "t", first[1:].strip()
    elif low[0] == "r":
        w, rest = "g", " ".join(words[1:])
    else:
        return None
    items = [rest] if rest else []
    for ln in lines[1:]:
        ln = _TG_BULLET_RE.sub("", ln).strip()
        if w == "i":                                   # har line par `info add` likha ho (copy-paste) to bhi chalega
            ln = re.sub(r"^\s*info\s+add\s+", "", ln, flags=re.I).strip()
        if ln:
            items.append(ln)
    return w, items

def _tg_todo_parse(raw, today: str) -> dict:
    """Ek line `kaam | due | priority` -> {text, pri, due, cut} ya {err}. (_tg_h_todo_add jaisa hi niyam.)"""
    parts = _tg_split_fields(raw)
    first = parts[0] if parts else ""
    c = _tg_clean_todo_text(first)
    if not c["ok"]:
        return {"err": "text khaali hai"}
    pri, due = 2, ""
    for f in parts[1:]:
        if not f:
            continue
        p = _TG_PRI_WORDS.get(_tg_norm(f))
        if p:
            pri = p
            continue
        dd_ = _tg_parse_due(f, today)
        if dd_:
            due = dd_
            continue
        return {"err": f"'{_tg_one(f, 20)}' samajh nahi aaya (due: kal, 10 jan · priority: high/med/low)"}
    return {"text": c["text"], "pri": pri, "due": due, "cut": len(" ".join(str(first).split())) > _TG_MAX["todo"]}

def _tg_bulk_add(w: str, raw_items: list, today: str) -> str:
    label = {"t": "To-Do", "g": "Rough entry", "i": "Info entry"}.get(w, "Rough entry")
    if not raw_items:
        return ("Kya add karna hai? Pehli line `info add` likho, neeche har line `topic | label | date`." if w == "i"
                else "Kya add karna hai? Pehli line `todo add` likho, neeche har line ek kaam.")
    if len(raw_items) > _TG_BULK_MAX:
        return f"Ek baar me max {_TG_BULK_MAX} likh sakte ho (tumne {len(raw_items)} bheje). Do hisso me bhejo."
    good, bad = [], []
    for n, raw in enumerate(raw_items, 1):
        if w == "t":
            r = _tg_todo_parse(raw, today)
            if r.get("err"):
                bad.append(f"{n}. {_tg_one(raw, 30)} — {r['err']}")
                continue
            good.append(r)
        elif w == "i":
            f = _tg_split_fields(raw)
            if not f or not f[0]:
                bad.append(f"{n}. (topic khaali)")
                continue
            if len(f) > 3:
                bad.append(f"{n}. {_tg_one(raw, 30)} — sirf 3 hisse: topic | label | date")
                continue
            f += [""] * (3 - len(f))
            c = _tg_clean_info(f[0], f[1], f[2], today)
            if not c["ok"]:
                bad.append(f"{n}. {_tg_one(raw, 30)} — {c['err']}")
                continue
            c["cut"] = _tg_too_long(f[0], _TG_MAX["info_topic"]) or _tg_too_long(f[1], _TG_MAX["info_label"])
            good.append(c)
        else:
            c = _tg_clean_rough_text(raw)
            if not c["ok"]:
                bad.append(f"{n}. (khaali line)")
                continue
            good.append({"text": c["text"], "cut": len(" ".join(str(raw).split())) > _TG_MAX["rough"]})
    if not good:
        return "Kuch add nahi hua:\n" + "\n".join(bad[:10])
    ids, shown, fail, last_now = [], [], "", 0
    with _ROUTINE_LOCK:
        b, err = _tg_read_blob()
        if err:
            return err
        for g in good:
            now = _tg_now_ms()
            iid = _tg_new_id(w, now)
            if w == "t":
                item = {"id": iid, "text": g["text"], "pri": g["pri"], "due": g["due"], "done": False, "doneAt": 0, "created": now}
            elif w == "i":
                _b2, _e2 = _tg_read_blob()               # pichli lines ka topic bhi match ho (ek hi naam ki spelling)
                _its = [x for x in ((_b2 or {}).get("info") or []) if isinstance(x, dict)]
                topic = _tg_canon_name(_its, "topic", g["topic"], _TG_MAX["info_topic"])
                item = {"id": iid, "topic": topic, "date": g["date"], "label": g["label"], "note": "", "created_at": now}
            else:
                item = {"id": iid, "text": g["text"], "created_at": now}
            ok, err = _tg_write(w, "a", iid, item=item, now_ms=now)
            if not ok:
                fail = err
                break
            ids.append(iid)
            last_now = now
            extra = ""
            if w == "t":
                extra = {1: " 🔴", 2: "", 3: " 🟢"}[g["pri"]] + (f" · due {_tg_fmt_short(g['due'])}" if g["due"] else "")
            if w == "i":
                shown.append(f"{len(ids)}. {topic}{' · ' + g['label'] if g['label'] else ''} · {_tg_fmt_long(g['date'])}{' ✂️' if g.get('cut') else ''}")
            else:
                shown.append(f"{len(ids)}. {_tg_one(g['text'], 60)}{extra}{' ✂️' if g.get('cut') else ''}")
        if ids:
            with _TG_UNDO_LOCK:
                st = _tg_undo_load()
                st.append({"kind": "bulk_add", "w": w, "ids": ids, "text": f"{len(ids)} {label}", "at": last_now})
                _tg_undo_save(st)
    if not ids:
        return fail or "⚠️ Save nahi ho paya, kuch add nahi hua."
    out = f"✅ {len(ids)} {label} add hue:\n" + "\n".join(shown)
    if any(g.get("cut") for g in good):
        out += "\n✂️ = bahut lamba tha, kaat diya"
    if fail:
        out += f"\n⚠️ Baaki {len(good) - len(ids)} save nahi ho paye."
    if bad:
        out += f"\n⚠️ {len(bad)} line skip hui:\n" + "\n".join(bad[:8])
    return out + "\n(sab wapas lene ke liye `undo` likho)"

def _tg_undo_bulk(rec) -> tuple:
    """_tg_h_undo ke andar (locks pehle se liye hue) -> (done, msg)."""
    w = rec.get("w")
    ids = rec.get("ids") if isinstance(rec.get("ids"), list) else []
    label = {"t": "To-Do", "g": "Rough entry", "i": "Info entry"}.get(w, "Rough entry")
    removed = 0
    for iid in ids:
        b, err = _tg_read_blob()
        if err:
            return False, err
        _lst, idx = _tg_find(b, w, iid)
        if idx < 0:
            continue
        ok, err = _tg_write(w, "r", iid)
        if not ok:
            return False, err
        removed += 1
    if removed == 0:
        return True, f"{len(ids)} {label} app me ab nahi the (pehle hi hat chuke)."
    return True, f"↩️ Undo: {removed} {label} hata diye"

_TG_UNDO_KINDS["bulk_add"] = lambda b, rec: (None, "")    # asli kaam _tg_undo_bulk

def _tg_h_find(ctx):
    q = _tg_norm(ctx["args"])
    if len(q) < 2:
        return "Kya dhoondhna hai? Aise likho: `find doodh` (kam se kam 2 akshar)."
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    def hit(*vals):
        return any(q in _tg_norm(v) for v in vals)
    sec = []
    def add(title, rows):
        if rows:
            show = rows[:8]
            more = f"\n…aur {len(rows) - 8}" if len(rows) > 8 else ""
            sec.append(f"{title} ({len(rows)}):\n" + "\n".join(show) + more)
    todos = [x for x in (b.get("todos") or []) if isinstance(x, dict) and hit(x.get("text"))]
    todos.sort(key=lambda x: bool(x.get("done")))
    add("✅ To-Do", [("✔️ " if x.get("done") else "⬜ ") + _tg_one(x.get("text"), 80) + (f" · due {_tg_fmt_short(x.get('due'))}" if _tg_is_iso(x.get("due")) and not x.get("done") else "") for x in todos])
    add("📝 Rough", [_tg_one(x.get("text"), 100) for x in (b.get("rough") or []) if isinstance(x, dict) and hit(x.get("text"))])
    add("ℹ️ Info", [f"{_tg_fmt_short(x.get('date'))} · {_tg_one(x.get('topic'), 30)}" + (f" · {_tg_one(x.get('label'), 40)}" if x.get("label") else "") + (f" — {_tg_one(x.get('note'), 40)}" if x.get("note") else "")
                    for x in (b.get("info") or []) if isinstance(x, dict) and hit(x.get("topic"), x.get("label"), x.get("note"))])
    add("✈️ Travel", [f"{_tg_one(x.get('person'), 30)} · {_tg_one(x.get('purpose'), 40)} · {_tg_travel_dates(x)}"
                      for x in (b.get("travel") or []) if isinstance(x, dict) and hit(x.get("person"), x.get("purpose"))])
    if not sec:
        return f"🔍 '{_tg_one(ctx['args'], 40)}' kahin nahi mila (To-Do, Rough, Info, Travel me)."
    return f"🔍 '{_tg_one(ctx['args'], 40)}':\n\n" + "\n\n".join(sec)

def _tg_h_status(ctx):
    now = _ist_now()
    up = max(0, int(time.time() - _TG_BOOT_TS))
    lines = [f"🟢 Server chal raha hai · {now.strftime('%d %b %I:%M %p')} IST", f"⏱ Chalu hue: {up // 3600}h {up % 3600 // 60}m pehle (restart ke baad)"]
    lines.append("🧩 Build: tick + skip(⏸) + practice-sets(0-5) + wajah(❌/0/1 set) + info-bulk (2026-10-06)")   # kaun sa app.py chal raha hai, pehchanne ke liye
    names = {t.name for t in threading.enumerate()}
    alive = [n for n in _TW_TRACKED if n in names]
    dead = [n for n in _TW_TRACKED if n not in names]
    lines.append(f"🧵 Background kaam: {len(alive)}/{len(_TW_TRACKED)} chal rahe" + (f"\n   band: {', '.join(dead)}\n   (Fyers/Binance wale sirf mode me chalte hain)" if dead else ""))
    lines.append("🔑 Fyers token: " + ("⚠️ expire hone ka flag mila" if os.path.exists(".token_expired_flag") else "expiry flag nahi"))
    try:
        zs = sorted(f for f in os.listdir(_tgx_backup_dir()) if re.fullmatch(r"backup_[0-9]{4}-[0-9]{2}-[0-9]{2}\.zip", f))
        lines.append("💾 Aakhri backup: " + (zs[-1][7:-4] if zs else "abhi tak koi nahi (`backup` likho)"))
    except Exception:
        lines.append("💾 Aakhri backup: pata nahi chala")
    try:
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(LOCAL_STATE_DIR, "routine.json")), IST)
        lines.append("🗂 App data aakhri baar badla: " + mt.strftime("%d %b %I:%M %p"))
    except Exception:
        pass
    try:
        lines.append(f"📀 Disk khaali: {shutil.disk_usage(LOCAL_STATE_DIR).free // (1024 * 1024)} MB")
    except Exception:
        pass
    lines.append(f"⏰ Email {_ROUTINE_EMAIL_HOUR % 12 or 12}:{_ROUTINE_EMAIL_MIN:02d} PM · Report {_REPORT_HOUR % 12 or 12}:{_REPORT_MIN:02d} PM")
    return "\n".join(lines)

_TG_COMMANDS = (
    (("find",), _tg_h_find),
    (("status",), _tg_h_status),
) + tuple(_TG_COMMANDS)
# ═══ TGX2 END ═══

# ═══ MM-TG (2026-10-04): 🤖 Telegram se 💵 Money Manager ka poora control ═══════════════════════════════════════
# Data: /data/app_state/moneymgr.json = {v:1, cats:[{id,type,name,subs:[{id,name}]}], tx:[{id,type,ts,amount,cat,sub,acct,catName,subName,note}], t, tg}
# Commands (sab _TG_COMMANDS me, relay ko kuch nahi badalna):
#   kharcha 250 food/chai | note | kal   (ya `k 250 chai`)  ·  aay 5000 salary        -> entry jodo
#   category na mile / na likhi ho to bot ✅ buttons dikhata hai (category -> sub), note = jo shabd match nahi hua
#   mm [aaj|kal|<date>] · mm mahina [sep|pichhla] · mm cat [income] [mahina] · mm total · mm find <shabd>
#   mm del <no.> [no. ...] · mm edit <no.> <amount> | <cat[/sub]> | <note> | <date>
#   mm cats · mm addcat <kharcha|aay> <naam> · mm addsub <cat>/<naam> · mm rename <cat[/sub]> | <naya> · mm delcat <cat>
#   mm backup · undo (add / del / edit teeno wapas)
# SYNC: Money Manager poora blob "latest wins" hai. Isliye Telegram ka har change blob ke `tg` journal me [{id,t,k,del?}] likha jaata hai.
#   Browser ka save aane par `_mm_merge_incoming` dekhta hai ki browser ne wo journal-entries dekhi hain ya nahi (browser ke blob me wahi (id,t) hai?).
#   Nahi dekhi -> Telegram ki entry / delete / category browser ke blob me wapas laga deta hai, taaki purana khula phone Telegram ka kaam mita na de.
_MM_LOCK = threading.RLock()          # lock order: _tg_h_undo me ROUTINE -> UNDO -> MM. MM ke andar kabhi UNDO / ROUTINE mat lo.
_MM_PEND = {}                         # pid -> {typ, amt, date, note, stage, cat, ids, at}  (button waali adhoori entry, RAM me)
_MM_PEND_TTL = 1800
_MM_LIST = {"ids": [], "at": 0.0}     # sabse naye `mm` list ka numbering (mm del / mm edit yahi padhte hain)
_MM_LIST_TTL = 1800
_MM_TG_KEEP = 200
_MM_TG_KEEP_SEC = 30 * 86400
_MM_MAX_AMT = 1e9
_MM_BTN_MAX = 60
_MM_UNDO_KINDS = ("mm_add", "mm_del", "mm_edit")
_MM_FMT = "`kharcha 250 food/chai` · `kharcha 250 chai | note | kal` · `aay 5000 salary`"
_MM_TYPE_WORD = {"kharcha": "expense", "kharch": "expense", "expense": "expense", "exp": "expense", "expenses": "expense",
                 "aay": "income", "aamdani": "income", "kamai": "income", "income": "income", "inc": "income", "incomes": "income"}


def _mm_read():
    """-> (blob, err). File hai par padh nahi paye / shape galat to err (tab likhna mana: warna asli data overwrite ho jaata)."""
    path = _local_state_file("moneymgr")
    if not os.path.exists(path):
        return None, "💵 Money Manager ka data server par abhi nahi hai. Pehle app me 💵 kholo (sync hone do), phir try karo."
    try:
        with open(path, "r", encoding="utf-8") as f:
            b = json.load(f)
    except Exception as e:
        _slog_exception("_mm_read", e)
        return None, "⚠️ Money Manager ka data padh nahi paya, isliye kuch change nahi kiya."
    if not isinstance(b, dict) or not isinstance(b.get("cats"), list) or not isinstance(b.get("tx"), list):
        return None, "⚠️ Money Manager ka data galat shape me hai, kuch change nahi kiya."
    return b, ""


def _mm_tg_log(b, iid, now, kind="t", dele=False):
    """blob ke `tg` journal me ek change likho (same id ki purani entry hatao; 30 din / 200 tak rakho)."""
    cut = now - _MM_TG_KEEP_SEC * 1000
    tg = [g for g in (b.get("tg") or []) if isinstance(g, dict) and g.get("id") != iid
          and isinstance(g.get("t"), (int, float)) and g["t"] >= cut]
    e = {"id": iid, "t": now, "k": kind}
    if dele:
        e["del"] = True
    tg.append(e)
    b["tg"] = tg[-_MM_TG_KEEP:]
    b["t"] = now


def _mm_merge_incoming(data):
    """Browser ka moneymgr save aaya -> Telegram ke wo changes jo browser ne dekhe nahi, usme wapas laga do."""
    if not isinstance(data, dict) or not isinstance(data.get("tx"), list) or not isinstance(data.get("cats"), list):
        return data
    disk, err = _mm_read()
    if err or not disk:
        return data
    dtg = [g for g in (disk.get("tg") or []) if isinstance(g, dict) and isinstance(g.get("id"), str)]
    if not dtg:
        return data
    seen = {(g.get("id"), g.get("t")) for g in (data.get("tg") or []) if isinstance(g, dict)}
    dtx = {t.get("id"): t for t in disk["tx"] if isinstance(t, dict)}
    dcat = {c.get("id"): c for c in disk["cats"] if isinstance(c, dict)}
    n = 0
    for g in dtg:
        iid = g["id"]
        if (iid, g.get("t")) in seen:
            continue
        if g.get("k") == "c":                                   # category
            if g.get("del"):
                before = len(data["cats"])
                data["cats"] = [c for c in data["cats"] if not (isinstance(c, dict) and c.get("id") == iid)]
                n += 1 if len(data["cats"]) != before else 0
                continue
            src = dcat.get(iid)
            if not src:
                continue
            cur = next((c for c in data["cats"] if isinstance(c, dict) and c.get("id") == iid), None)
            if cur is None:
                data["cats"].append(src)
            else:
                cur["name"] = src.get("name", cur.get("name"))
                cur["subs"] = cur.get("subs") if isinstance(cur.get("subs"), list) else []
                for s in (src.get("subs") or []):
                    if not isinstance(s, dict):
                        continue
                    ex = next((x for x in cur["subs"] if isinstance(x, dict) and x.get("id") == s.get("id")), None)
                    if ex is None:
                        cur["subs"].append(s)
                    else:
                        ex["name"] = s.get("name", ex.get("name"))
            n += 1
            continue
        if g.get("del"):                                        # entry delete
            before = len(data["tx"])
            data["tx"] = [t for t in data["tx"] if not (isinstance(t, dict) and t.get("id") == iid)]
            n += 1 if len(data["tx"]) != before else 0
        else:                                                   # entry add / edit
            src = dtx.get(iid)
            if not src:
                continue
            ix = next((i for i, t in enumerate(data["tx"]) if isinstance(t, dict) and t.get("id") == iid), -1)
            if ix >= 0:
                data["tx"][ix] = src
            else:
                data["tx"].append(src)
            n += 1
    data["tg"] = dtg
    if n:
        data["t"] = max(int(data.get("t") or 0), int(time.time() * 1000))
        _slog(f"[mm] browser save me Telegram ke {n} change wapas lagaye (phone purana tha)", level="ok")
    return data


# ── chhote helpers ──
def _mm_cats(b, typ):
    return [c for c in b["cats"] if isinstance(c, dict) and c.get("type") == typ and c.get("id") and c.get("name")]

def _mm_subs(c):
    return [s for s in (c.get("subs") or []) if isinstance(s, dict) and s.get("id") and s.get("name")]

def _mm_cmap(b):
    return {c.get("id"): c for c in b["cats"] if isinstance(c, dict)}

def _mm_cn(cm, t):
    c = cm.get(t.get("cat"))
    cn = c.get("name") if c else (t.get("catName") or "?")
    sn = ""
    if t.get("sub"):
        s = next((s for s in (c.get("subs") or []) if isinstance(s, dict) and s.get("id") == t.get("sub")), None) if c else None
        sn = s.get("name") if s else (t.get("subName") or "")
    return cn, sn

def _mm_money(x):
    return _tg_inr(_tg_paise(x))

def _mm_tsday(t):
    try:
        return datetime.datetime.fromtimestamp(float(t.get("ts")) / 1000.0, IST).date().isoformat()
    except Exception:
        return ""

def _mm_amt(t):
    try:
        v = float(t.get("amount") or 0)
        return v if math.isfinite(v) else 0.0
    except Exception:
        return 0.0

def _mm_sum(b, y, mo):
    inc = exp = 0.0
    pre = f"{y:04d}-{mo:02d}"
    for t in b["tx"]:
        if isinstance(t, dict) and _mm_tsday(t).startswith(pre):
            if t.get("type") == "income":
                inc += _mm_amt(t)
            else:
                exp += _mm_amt(t)
    return inc, exp

def _mm_match(items, q, key=lambda x: x["name"]):
    """exact -> shuru ka hissa -> (3+ akshar ho to) beech ka hissa. List wapas (0, 1 ya zyada)."""
    q = _tg_norm(q)
    if not q:
        return []
    ex = [x for x in items if _tg_norm(key(x)) == q]
    if ex:
        return ex
    pre = [x for x in items if _tg_norm(key(x)).startswith(q)]
    if pre:
        return pre
    return [x for x in items if q in _tg_norm(key(x))] if len(q) >= 3 else []

def _mm_parse_date(txt, today):
    """Paisa-entry ke liye: aaj, kal (= beeta hua kal!), parso, ya 10 jan / 10-01 / 10-01-2026."""
    t = _tg_norm(txt)
    if t in ("aaj", "today"):
        return today
    if t in ("kal", "yesterday"):
        return (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    if t == "parso":
        return (datetime.date.fromisoformat(today) - datetime.timedelta(days=2)).isoformat()
    return _tg_parse_date(txt, today)

def _mm_ts_for(d, today, now):
    if d == today:
        return now
    return int(datetime.datetime(int(d[:4]), int(d[5:7]), int(d[8:10]), 12, 0, tzinfo=IST).timestamp() * 1000)

def _mm_dlabel(d, today):
    if d == today:
        return "aaj"
    if _tg_days_between(d, today) == 1:
        return "kal"
    return _tg_fmt_long(d)

def _mm_line(cm, i, t):
    cn, sn = _mm_cn(cm, t)
    s = f"{i}. {'🟢' if t.get('type') == 'income' else '🔻'} {_mm_money(t.get('amount'))} · {cn}" + (f" › {sn}" if sn else "")
    if t.get("note"):
        s += f" · {_tg_one(t['note'], 40)}"
    return s

def _mm_set_list(ids):
    with _MM_LOCK:
        _MM_LIST["ids"] = list(ids)
        _MM_LIST["at"] = time.time()

def _mm_pick_no(tok):
    """'3' -> id (taaza list se). -> (id, err)"""
    with _MM_LOCK:
        ids, at = list(_MM_LIST["ids"]), _MM_LIST["at"]
    if not ids or time.time() - at > _MM_LIST_TTL:
        return None, "Pehle `mm` (ya `mm find <shabd>`) se list dekho, phir number likho."
    if not re.fullmatch(r"[0-9]{1,3}", tok or ""):
        return None, f"'{_tg_one(tok, 20)}' number nahi hai."
    n = int(tok)
    if n < 1 or n > len(ids):
        return None, f"Number 1 se {len(ids)} ke beech likho."
    return ids[n - 1], ""


# ── entry jodna ──
def _mm_looks_date(x):
    """date jaisa dikhta hai (40 jan, 31-02) par parse nahi hua -> galti maano, note nahi."""
    t = _tg_norm(x)
    m = re.fullmatch(r"[0-9]{1,2} ([a-z]+)(?: [0-9]{4})?", t)
    if m and m.group(1) in _TG_MONTHS:
        return True
    return bool(re.fullmatch(r"[0-9]{1,2}-[0-9]{1,2}(?:-[0-9]{4})?", t))

def _mm_parse_add(args, today):
    f = _tg_split_fields(args)
    head = (f[0] if f else "").strip()
    if not head:
        return "Amount likho. Aise: " + _MM_FMT
    words = head.split()
    amt, spec = _tg_parse_amount(words[0]), " ".join(words[1:])
    if amt is None and len(words) > 1:                      # `kharcha chai 250` bhi chalega
        amt, spec = _tg_parse_amount(words[-1]), " ".join(words[:-1])
    if amt is None or amt <= 0 or amt >= _MM_MAX_AMT:
        return "Amount sahi number likho (0 se zyada, paise tak). Aise: " + _MM_FMT
    date, got, note = today, False, ""
    for x in f[1:]:
        if not x:
            continue
        d = None if got else _mm_parse_date(x, today)
        if d:
            date, got = d, True
        elif _mm_looks_date(x):
            return f"Date samajh nahi aayi: '{_tg_one(x, 20)}'. Aise likho: `kal`, `10 jan`, `10-01-2026`."
        else:
            note = (note + " · " + x) if note else x
    if date > today:
        return "Aane wali date nahi chalti. Date aaj ya pehle ki likho (`kal` = beeta hua kal)."
    return {"amt": round(amt, 2), "spec": spec.strip(), "note": _tg_one(note, 200), "date": date}

def _mm_resolve(b, typ, spec):
    """spec ('food/chai', 'chai', '') -> {cat, sub, need: None|'cat'|'sub', cands, note}. need != None => buttons."""
    cats = _mm_cats(b, typ)
    spec = _tg_one(spec, 80)
    out = {"cat": None, "sub": None, "need": "cat", "cands": cats, "note": ""}
    if not spec:
        return out
    if "/" in spec:
        cq, _s, sq = spec.partition("/")
        cq, sq = cq.strip(), sq.strip()
        m = _mm_match(cats, cq)
        if len(m) == 1:
            c = m[0]
            out["cat"] = c
            if not sq:
                out["need"] = None
                return out
            allsubs = _mm_subs(c)
            sm = _mm_match(allsubs, sq)
            if len(sm) == 1:
                out.update(sub=sm[0], need=None)
                return out
            out.update(need="sub", cands=sm or allsubs, note=sq)
            return out
        out.update(cands=m or cats, note="" if m else spec)
        return out
    m = _mm_match(cats, spec)
    if len(m) == 1:
        out.update(cat=m[0], need=None)
        return out
    if len(m) > 1:
        out["cands"] = m
        return out
    flat = [(c, s) for c in cats for s in _mm_subs(c)]
    fm = _mm_match(flat, spec, key=lambda cs: cs[1]["name"])
    if len(fm) == 1:
        out.update(cat=fm[0][0], sub=fm[0][1], need=None)
        return out
    if len(fm) > 1:
        seen, cl = set(), []
        for c, _s in fm:
            if c["id"] not in seen:
                seen.add(c["id"])
                cl.append(c)
        out["cands"] = cl
        return out
    out["note"] = spec
    return out

def _mm_head(typ, amt, note=""):
    return f"{'🔻 Kharcha' if typ == 'expense' else '🟢 Aay'} {_mm_money(amt)}" + (f" · {_tg_one(note, 40)}" if note else "")

def _mm_kb(pid, kind, items):
    btns = [{"text": _tg_one(x["name"], 20) or "?", "callback_data": f"mm|{pid}|{kind}|{i}"} for i, x in enumerate(items[:_MM_BTN_MAX])]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if kind == "s":
        rows.append([{"text": "➖ Bina sub", "callback_data": f"mm|{pid}|s|n"}])
    rows.append([{"text": "❎ Cancel", "callback_data": f"mm|{pid}|x|0"}])
    return rows

def _mm_pend_purge():
    now = time.time()
    with _MM_LOCK:
        for k in [k for k, v in _MM_PEND.items() if now - v.get("at", 0) > _MM_PEND_TTL]:
            _MM_PEND.pop(k, None)
        while len(_MM_PEND) > 20:
            _MM_PEND.pop(next(iter(_MM_PEND)))

def _mm_do_add(typ, amt, date, note, cid, sid):
    """Asli write. -> (ok, text). Undo record MM lock chhodne ke BAAD push hota hai."""
    today = _tg_today()
    with _MM_LOCK:
        b, err = _mm_read()
        if err:
            return False, err
        c = _mm_cmap(b).get(cid)
        if not c:
            return False, "⚠️ Category ab app me nahi mili. Dobara try karo."
        s = next((x for x in _mm_subs(c) if x["id"] == sid), None) if sid else None
        now = _tg_now_ms()
        rec = {"id": _tg_new_id("t", now), "type": typ, "ts": _mm_ts_for(date, today, now), "amount": round(float(amt), 2),
               "cat": c["id"], "sub": s["id"] if s else "", "acct": "Cash", "catName": c["name"], "subName": s["name"] if s else "",
               "note": _tg_one(note, 200)}
        b["tx"].append(rec)
        _mm_tg_log(b, rec["id"], now)
        ok, _m = _local_state_save_raw("moneymgr", b)
        inc, exp = _mm_sum(b, int(date[:4]), int(date[5:7])) if ok else (0, 0)
    if not ok:
        return False, "⚠️ Save nahi ho paya, kuch change nahi hua. Dobara try karo."
    shown = f"{_mm_money(amt)} · {c['name']}" + (f" › {s['name']}" if s else "")
    with _TG_UNDO_LOCK:
        st = _tg_undo_load()
        st.append({"kind": "mm_add", "id": rec["id"], "text": shown, "at": now})
        _tg_undo_save(st)
    lines = [f"💵 {'Kharcha' if typ == 'expense' else 'Aay'} add: {shown} · {_mm_dlabel(date, today)}"]
    if rec["note"]:
        lines.append(f"📝 {rec['note']}")
    lines.append(f"📊 {_TG_MON_SHORT[int(date[5:7]) - 1]} {date[:4]}: Aay {_mm_money(inc)} · Kharcha {_mm_money(exp)} · Balance {_mm_money(inc - exp)}")
    lines.append("(wapas lene ke liye `undo` likho)")
    return True, "\n".join(lines)

def _mm_h_add(typ):
    def h(ctx):
        p = _mm_parse_add(ctx["args"], ctx["today"])
        if isinstance(p, str):
            return p
        with _MM_LOCK:
            b, err = _mm_read()
            if err:
                return err
            if not _mm_cats(b, typ):
                return f"💵 Abhi {'expense' if typ == 'expense' else 'income'} ki koi category nahi. Pehle `mm addcat {'kharcha' if typ == 'expense' else 'aay'} <naam>` likho."
            r = _mm_resolve(b, typ, p["spec"])
        note = " · ".join(x for x in (r["note"], p["note"]) if x)
        if r["need"] is None:
            ok, txt = _mm_do_add(typ, p["amt"], p["date"], note, r["cat"]["id"], r["sub"]["id"] if r["sub"] else "")
            return txt
        # buttons (category ya sub)
        _mm_pend_purge()
        import secrets
        pid = "".join(secrets.choice(_TG_B36) for _ in range(4))
        stage = "cat" if r["need"] == "cat" else "sub"
        cands = r["cands"][:_MM_BTN_MAX]
        if not cands:
            return "Is category me koi sub nahi. Category likh ke dobara bhejo: " + _MM_FMT
        with _MM_LOCK:
            _MM_PEND[pid] = {"typ": typ, "amt": p["amt"], "date": p["date"], "note": note, "stage": stage,
                             "cat": r["cat"]["id"] if r["cat"] else None, "ids": [x["id"] for x in cands], "at": time.time()}
        why = "Category chuno:" if stage == "cat" else f"{r['cat']['name']} ka sub chuno:"
        res = _tgx_send_with_keyboard(f"{_mm_head(typ, p['amt'], note)} · {_mm_dlabel(p['date'], ctx['today'])}\n{why}",
                                      _mm_kb(pid, "c" if stage == "cat" else "s", cands))
        if res != "ok":
            with _MM_LOCK:
                _MM_PEND.pop(pid, None)
            return "⚠️ Buttons nahi bhej paya. Category likh ke bhejo: " + _MM_FMT
        return ""
    return h

def _mm_finish(pid, p, cid, sid):
    with _MM_LOCK:
        if _MM_PEND.pop(pid, None) is None:
            return "⚠️ Ye entry pehle hi ho chuki / purani hai."
    ok, txt = _mm_do_add(p["typ"], p["amt"], p["date"], p["note"], cid, sid)
    if not ok:
        with _MM_LOCK:
            _MM_PEND[pid] = p
        return txt
    _alert_via_telegram(txt)
    first = txt.split("\n")[0]
    return "💵 " + first.replace("💵 ", "", 1)

def _mm_h_cb(ctx):
    parts = ctx["args"].strip().split("|")
    if len(parts) != 4 or parts[0] != "mm":
        return "⚠️ Button samajh nahi aaya."
    _, pid, kind, idx = parts
    _mm_pend_purge()
    with _MM_LOCK:
        p = _MM_PEND.get(pid)
    if not p:
        return "⚠️ Ye entry purani ho gayi — dobara `kharcha ...` likho."
    if kind == "x":
        with _MM_LOCK:
            _MM_PEND.pop(pid, None)
        return "❎ Cancel kar diya"
    if kind == "c" and p["stage"] == "cat":
        try:
            cid = p["ids"][int(idx)]
        except Exception:
            return "⚠️ Button samajh nahi aaya."
        with _MM_LOCK:
            b, err = _mm_read()
            if err:
                return err
            c = _mm_cmap(b).get(cid)
            subs = _mm_subs(c) if c else []
        if not c:
            return "⚠️ Category ab app me nahi mili."
        if subs:
            subs = subs[:_MM_BTN_MAX]
            with _MM_LOCK:
                p.update(stage="sub", cat=cid, ids=[s["id"] for s in subs], at=time.time())
            r = _tgx_send_with_keyboard(f"{_mm_head(p['typ'], p['amt'], p['note'])} · {c['name']}\nSub chuno:", _mm_kb(pid, "s", subs))
            return f"💵 {c['name']} — ab sub chuno" if r == "ok" else "⚠️ Sub buttons nahi bhej paya"
        return _mm_finish(pid, p, cid, "")
    if kind == "s" and p["stage"] == "sub":
        sid = ""
        if idx != "n":
            try:
                sid = p["ids"][int(idx)]
            except Exception:
                return "⚠️ Button samajh nahi aaya."
        return _mm_finish(pid, p, p["cat"], sid)
    return "⚠️ Ye button purana hai."

_MM_ORIG_CB = _tgx_h_cb

def _mm_cb_wrap(ctx):
    """`cb mm|...` -> Money Manager; baaki `cb` (✅/❌ checklist) purane handler ko."""
    if ctx["args"].strip().startswith("mm|"):
        return _mm_h_cb(ctx)
    return _MM_ORIG_CB(ctx)


# ── dekhna ──
def _mm_month_arg(arg, today):
    a = _tg_norm(arg)
    y, mo = int(today[:4]), int(today[5:7])
    if not a or a in ("is", "ye", "abhi", "current"):
        return y, mo
    if a in ("pichhla", "pichla", "last", "prev", "pehle"):
        return (y - 1, 12) if mo == 1 else (y, mo - 1)
    m = re.fullmatch(r"([a-z]+)(?: ([0-9]{4}))?", a)
    if m and m.group(1) in _TG_MONTHS:
        mm_ = _TG_MONTHS[m.group(1)]
        return (int(m.group(2)) if m.group(2) else (y if mm_ <= mo else y - 1)), mm_
    m = re.fullmatch(r"([0-9]{1,2})[-/]([0-9]{4})", a)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(2)), int(m.group(1))
    return None

def _mm_day_list(b, d, today):
    cm = _mm_cmap(b)
    rows = sorted([t for t in b["tx"] if isinstance(t, dict) and _mm_tsday(t) == d], key=lambda t: t.get("ts") or 0)
    if not rows:
        return f"💵 {_mm_dlabel(d, today)} ({_tg_fmt_long(d)}): koi entry nahi."
    inc = sum(_mm_amt(t) for t in rows if t.get("type") == "income")
    exp = sum(_mm_amt(t) for t in rows if t.get("type") != "income")
    show = rows[:30]
    _mm_set_list([t.get("id") for t in show])
    out = [f"💵 {_mm_dlabel(d, today)} ({_tg_fmt_long(d)}) — {len(rows)} entry"]
    out += [_mm_line(cm, i + 1, t) for i, t in enumerate(show)]
    if len(rows) > len(show):
        out.append(f"…aur {len(rows) - len(show)}")
    out.append(f"Aay {_mm_money(inc)} · Kharcha {_mm_money(exp)}")
    out.append("(hatane ke liye `mm del <no.>`, badalne ke liye `mm edit <no.> <amount>`)")
    return "\n".join(out)

def _mm_month_text(b, y, mo, cat_mode=False, typ="expense"):
    pre = f"{y:04d}-{mo:02d}"
    cm = _mm_cmap(b)
    rows = [t for t in b["tx"] if isinstance(t, dict) and _mm_tsday(t).startswith(pre)]
    inc, exp = _mm_sum(b, y, mo)
    title = f"{_TG_MON_SHORT[mo - 1]} {y}"
    if not rows:
        return f"💵 {title}: koi entry nahi."
    agg = {}
    for t in rows:
        if (t.get("type") == "income") != (typ == "income"):
            continue
        cn, sn = _mm_cn(cm, t)
        e = agg.setdefault(cn, [0.0, {}])
        e[0] += _mm_amt(t)
        if sn:
            e[1][sn] = e[1].get(sn, 0.0) + _mm_amt(t)
    tot = inc if typ == "income" else exp
    ordered = sorted(agg.items(), key=lambda kv: -kv[1][0])
    lines = []
    if not cat_mode:
        lines = [f"💵 {title} — {len(rows)} entry", f"🟢 Aay {_mm_money(inc)}", f"🔻 Kharcha {_mm_money(exp)}",
                 f"💰 Balance {_mm_money(inc - exp)}"]
        if ordered:
            lines.append(f"\nTop {'aay' if typ == 'income' else 'kharcha'}:")
    else:
        lines = [f"💵 {title} · {'Aay' if typ == 'income' else 'Kharcha'} {_mm_money(tot)} — category-wise"]
    for cn, (v, subs) in ordered[:(8 if not cat_mode else 25)]:
        pct = f" ({int(round(v * 100 / tot))}%)" if tot else ""
        lines.append(f"• {cn} {_mm_money(v)}{pct}")
        if cat_mode:
            for sn, sv in sorted(subs.items(), key=lambda kv: -kv[1])[:4]:
                lines.append(f"    ◦ {sn} {_mm_money(sv)}")
    return "\n".join(lines)

def _mm_total_text(b):
    inc = sum(_mm_amt(t) for t in b["tx"] if isinstance(t, dict) and t.get("type") == "income")
    exp = sum(_mm_amt(t) for t in b["tx"] if isinstance(t, dict) and t.get("type") != "income")
    return (f"💵 Ab tak ka poora ({len(b['tx'])} entries)\n🟢 Total Income {_mm_money(inc)}\n🔻 Total Expenses {_mm_money(exp)}\n"
            f"💰 Total Balance {_mm_money(inc - exp)}")

def _mm_find(b, q):
    qn = _tg_norm(q)
    if len(qn) < 2:
        return "Kya dhoondhna hai? Aise: `mm find chai` (kam se kam 2 akshar)."
    cm = _mm_cmap(b)
    hits = []
    for t in b["tx"]:
        if not isinstance(t, dict):
            continue
        cn, sn = _mm_cn(cm, t)
        if qn in _tg_norm(f"{t.get('note', '')} {cn} {sn} {t.get('amount', '')}"):
            hits.append(t)
    if not hits:
        return f"🔍 '{_tg_one(q, 30)}' kisi entry me nahi mila."
    hits.sort(key=lambda t: -(t.get("ts") or 0))
    show = hits[:20]
    _mm_set_list([t.get("id") for t in show])
    exp = sum(_mm_amt(t) for t in hits if t.get("type") != "income")
    inc = sum(_mm_amt(t) for t in hits if t.get("type") == "income")
    out = [f"🔍 '{_tg_one(q, 30)}' — {len(hits)} entry"]
    for i, t in enumerate(show):
        out.append(_mm_line(cm, i + 1, t) + f" · {_tg_fmt_short(_mm_tsday(t))}")
    if len(hits) > len(show):
        out.append(f"…aur {len(hits) - len(show)}")
    out.append(f"Kharcha {_mm_money(exp)}" + (f" · Aay {_mm_money(inc)}" if inc else ""))
    return "\n".join(out)

def _mm_cats_text(b):
    out = []
    for typ, title in (("expense", "🔻 Kharcha"), ("income", "🟢 Aay")):
        cs = _mm_cats(b, typ)
        out.append(f"{title} ({len(cs)}):")
        for c in cs:
            sn = [s["name"] for s in _mm_subs(c)]
            out.append(f"• {c['name']}" + (f" ({', '.join(sn[:8])}{'…' if len(sn) > 8 else ''})" if sn else ""))
    return "\n".join(out)


# ── badalna: del / edit / category ──
def _mm_del(b_unused, toks):
    if not toks:
        return "Number likho. Aise: `mm del 3` ya `mm del 2 5` (pehle `mm` se list dekho)."
    ids = []
    for tk in toks:
        iid, err = _mm_pick_no(tk)
        if err:
            return err
        if iid not in ids:
            ids.append(iid)
    with _MM_LOCK:
        b, err = _mm_read()
        if err:
            return err
        cm = _mm_cmap(b)
        gone = [t for t in b["tx"] if isinstance(t, dict) and t.get("id") in ids]
        if not gone:
            return "Ye entries ab app me nahi mili (pehle hi hat chuki)."
        keep = [t for t in b["tx"] if not (isinstance(t, dict) and t.get("id") in ids)]
        b["tx"] = keep
        now = _tg_now_ms()
        for t in gone:
            _mm_tg_log(b, t["id"], now, dele=True)
            now += 1
        ok, _m = _local_state_save_raw("moneymgr", b)
        lines = [_mm_line(cm, i + 1, t) for i, t in enumerate(gone)]
    if not ok:
        return "⚠️ Save nahi ho paya, kuch change nahi hua. Dobara try karo."
    with _TG_UNDO_LOCK:
        st = _tg_undo_load()
        st.append({"kind": "mm_del", "txs": gone, "text": f"{len(gone)} entry", "at": _tg_now_ms()})
        _tg_undo_save(st)
    _mm_set_list([])
    return f"🗑 Hata diya ({len(gone)}):\n" + "\n".join(lines) + "\n(wapas lene ke liye `undo` likho)"

def _mm_edit(args, today):
    f = _tg_split_fields(args)
    head = (f[0] if f else "").split()
    if not head:
        return "Aise likho: `mm edit 3 300` ya `mm edit 3 300 | food/chai | note | kal` (khaali field = jaisa tha waisa; note `-` = note hatao)."
    iid, err = _mm_pick_no(head[0])
    if err:
        return err
    new_amt = None
    if len(head) > 1:
        new_amt = _tg_parse_amount(head[1])
        if new_amt is None or new_amt <= 0 or new_amt >= _MM_MAX_AMT:
            return "Amount sahi number likho (0 se zyada)."
    f += [""] * (4 - len(f))
    spec, note, dtxt = f[1], f[2], f[3]
    if new_amt is None and not (spec or note or dtxt):
        return "Kya badalna hai? Amount, category, note ya date me se kuch likho."
    nd = None
    if dtxt:
        nd = _mm_parse_date(dtxt, today)
        if not nd:
            return "Date samajh nahi aayi. Aise: `kal`, `10 jan`, `10-01-2026`."
        if nd > today:
            return "Aane wali date nahi chalti."
    with _MM_LOCK:
        b, err = _mm_read()
        if err:
            return err
        ix = next((i for i, t in enumerate(b["tx"]) if isinstance(t, dict) and t.get("id") == iid), -1)
        if ix < 0:
            return "Ye entry ab app me nahi mili."
        old = dict(b["tx"][ix])
        t = dict(old)
        cm = _mm_cmap(b)
        if new_amt is not None:
            t["amount"] = round(new_amt, 2)
        if spec:
            r = _mm_resolve(b, t.get("type") if t.get("type") in ("income", "expense") else "expense", spec)
            if r["need"] is not None or not r["cat"]:
                names = ", ".join(c["name"] for c in r["cands"][:10])
                return f"Category '{_tg_one(spec, 30)}' saaf nahi mili. Options: {names or '—'}"
            t.update(cat=r["cat"]["id"], sub=r["sub"]["id"] if r["sub"] else "", catName=r["cat"]["name"],
                     subName=r["sub"]["name"] if r["sub"] else "")
        if note:
            t["note"] = "" if note == "-" else _tg_one(note, 200)
        if nd:
            oldd = _mm_tsday(old)
            t["ts"] = old.get("ts") if nd == oldd else _mm_ts_for(nd, today, _tg_now_ms())
        b["tx"][ix] = t
        now = _tg_now_ms()
        _mm_tg_log(b, iid, now)
        ok, _m = _local_state_save_raw("moneymgr", b)
        cm = _mm_cmap(b)
        line = _mm_line(cm, 1, t)[3:]
    if not ok:
        return "⚠️ Save nahi ho paya, kuch change nahi hua. Dobara try karo."
    with _TG_UNDO_LOCK:
        st = _tg_undo_load()
        st.append({"kind": "mm_edit", "id": iid, "old": old, "text": line[:100], "at": now})
        _tg_undo_save(st)
    return f"✏️ Badal diya: {line}\n(wapas lene ke liye `undo` likho)"

def _mm_cat_target(b, spec):
    """'food' ya 'food/chai' (dono type me dhoondo) -> (cat, sub, err)."""
    cq, _s, sq = spec.partition("/")
    m = _mm_match([c for c in b["cats"] if isinstance(c, dict) and c.get("id") and c.get("name")], cq.strip())
    if not m:
        return None, None, f"Category '{_tg_one(cq, 30)}' nahi mili. `mm cats` se naam dekho."
    if len(m) > 1:
        return None, None, "Kai categories mil rahi hain: " + ", ".join(f"{c['name']} ({'aay' if c.get('type') == 'income' else 'kharcha'})" for c in m[:8]) + ". Poora naam likho."
    c = m[0]
    if not sq.strip():
        return c, None, ""
    sm = _mm_match(_mm_subs(c), sq)
    if len(sm) != 1:
        return None, None, f"Sub '{_tg_one(sq, 30)}' {c['name']} me nahi mila / saaf nahi."
    return c, sm[0], ""

def _mm_admin(sub, rest):
    """addcat / addsub / rename / delcat. -> reply text"""
    with _MM_LOCK:
        b, err = _mm_read()
        if err:
            return err
        now = _tg_now_ms()
        if sub == "addcat":
            w = rest.split(None, 1)
            typ = _MM_TYPE_WORD.get((w[0] if w else "").lower())
            name = _tg_one(w[1], 40) if len(w) > 1 else ""
            if not typ or not name:
                return "Aise: `mm addcat kharcha Chai-Nashta` ya `mm addcat aay Bonus`."
            if any(_tg_norm(c["name"]) == _tg_norm(name) for c in _mm_cats(b, typ)):
                return f"'{name}' naam ki category pehle se hai."
            c = {"id": _tg_new_id("c", now), "type": typ, "name": name, "subs": []}
            b["cats"].append(c)
            _mm_tg_log(b, c["id"], now, kind="c")
            msg = f"📁 Category add: {name} ({'kharcha' if typ == 'expense' else 'aay'})"
        elif sub == "addsub":
            cs, _s, sn = rest.partition("/")
            sn = _tg_one(sn, 40)
            if not cs.strip() or not sn:
                return "Aise: `mm addsub food/chai` (category/sub)."
            c, _x, e = _mm_cat_target(b, cs.strip())
            if e:
                return e
            if any(_tg_norm(s["name"]) == _tg_norm(sn) for s in _mm_subs(c)):
                return f"'{sn}' {c['name']} me pehle se hai."
            c["subs"] = c.get("subs") if isinstance(c.get("subs"), list) else []
            c["subs"].append({"id": _tg_new_id("s", now), "name": sn})
            _mm_tg_log(b, c["id"], now, kind="c")
            msg = f"📁 Sub add: {c['name']} › {sn}"
        elif sub == "rename":
            tgt, _p, new = rest.partition("|")
            new = _tg_one(new, 40)
            if not tgt.strip() or not new:
                return "Aise: `mm rename food | Khana` ya `mm rename food/chai | Cutting chai`."
            c, s, e = _mm_cat_target(b, tgt.strip())
            if e:
                return e
            if s:
                old = s["name"]
                s["name"] = new
                for t in b["tx"]:
                    if isinstance(t, dict) and t.get("cat") == c["id"] and t.get("sub") == s["id"]:
                        t["subName"] = new
                msg = f"✏️ Sub rename: {c['name']} › {old} → {new}"
            else:
                old = c["name"]
                c["name"] = new
                for t in b["tx"]:
                    if isinstance(t, dict) and t.get("cat") == c["id"]:
                        t["catName"] = new
                msg = f"✏️ Category rename: {old} → {new}"
            _mm_tg_log(b, c["id"], now, kind="c")
        else:   # delcat
            if "/" in rest:
                return "Sub hatana Telegram se nahi hota (app ke ⚙ Categories se hatao). Category hatane ke liye: `mm delcat food`."
            c, _s, e = _mm_cat_target(b, rest.strip())
            if e:
                return e
            used = sum(1 for t in b["tx"] if isinstance(t, dict) and t.get("cat") == c["id"])
            if used:
                return f"{c['name']} me {used} entries hain, isliye nahi hatayi. Pehle unhe doosri category me daalo (`mm edit`)."
            b["cats"] = [x for x in b["cats"] if not (isinstance(x, dict) and x.get("id") == c["id"])]
            _mm_tg_log(b, c["id"], now, kind="c", dele=True)
            msg = f"🗑 Category hata di: {c['name']}"
        ok, _m = _local_state_save_raw("moneymgr", b)
    return msg if ok else "⚠️ Save nahi ho paya, kuch change nahi hua. Dobara try karo."

def _mm_h_backup(ctx):
    with _MM_LOCK:
        b, err = _mm_read()
    if err:
        return err
    out = {k: v for k, v in b.items() if k != "tg"}
    res = _tgx_send_document(f"money_manager_backup_{ctx['today']}.json", json.dumps(out, ensure_ascii=False).encode("utf-8"),
                             f"💵 Money Manager backup · {len(b['tx'])} entries (app me 📥 Backup import se wapas aa sakta hai)")
    if res == "ok":
        return ""
    return f"⚠️ Backup nahi bhej paya ({str(res)[:120]})."

_MM_HELP = (
    "💵 Money Manager commands:\n"
    "• kharcha 250 food/chai | note | kal — kharcha jodo (ya `k 250 chai`); category na mile to buttons aate hain\n"
    "• aay 5000 salary — income jodo\n"
    "• mm — aaj ki entries (numbered) · mm kal · mm 10 jan\n"
    "• mm mahina [sep | pichhla] — mahine ka hisaab · mm cat [income] [mahina] — category-wise · mm total\n"
    "• mm find <shabd> — entries dhoondho\n"
    "• mm del 3 (ya 2 5) · mm edit 3 300 | food/chai | note | kal — pehle `mm` se list dekho\n"
    "• mm cats · mm addcat kharcha <naam> · mm addsub food/chai · mm rename food | Khana · mm delcat <naam>\n"
    "• mm backup — json Telegram par · undo — add / del / edit wapas\n"
    "(`kal` = beeta hua kal)"
)

def _mm_h_main(ctx):
    a = ctx["args"].strip()
    today = ctx["today"]
    w = a.split(None, 1)
    first = w[0].lower() if w else ""
    rest = w[1].strip() if len(w) > 1 else ""
    if first in ("help", "?"):
        return _MM_HELP
    if first == "backup":
        return _mm_h_backup(ctx)
    if first in ("addcat", "addsub", "rename", "delcat"):
        return _mm_admin(first, rest)
    if first == "del" or first == "delete":
        return _mm_del(None, rest.split())
    if first == "edit":
        return _mm_edit(rest, today)
    with _MM_LOCK:
        b, err = _mm_read()
    if err:
        return err
    if first in ("", "aaj", "today"):
        return _mm_day_list(b, today, today)
    if first in ("kal", "parso"):
        return _mm_day_list(b, _mm_parse_date(first, today), today)
    if first == "find":
        return _mm_find(b, rest)
    if first == "total":
        return _mm_total_text(b)
    if first == "cats":
        return _mm_cats_text(b)
    if first in ("mahina", "month", "mahine"):
        ym = _mm_month_arg(rest, today)
        return _mm_month_text(b, ym[0], ym[1]) if ym else "Mahina samajh nahi aaya. Aise: `mm mahina`, `mm mahina sep`, `mm mahina pichhla`."
    if first in ("cat", "category"):
        ws = rest.split()
        typ = "income" if any(x.lower() in ("income", "incomes", "aay") for x in ws) else "expense"
        ym = _mm_month_arg(" ".join(x for x in ws if x.lower() not in ("income", "incomes", "aay", "kharcha", "expense", "expenses")), today)
        return _mm_month_text(b, ym[0], ym[1], cat_mode=True, typ=typ) if ym else "Mahina samajh nahi aaya. Aise: `mm cat`, `mm cat sep`, `mm cat income`."
    d = _mm_parse_date(a, today)
    if d:
        return _mm_day_list(b, d, today)
    return "Samajh nahi aaya. `mm help` likho."


# ── undo ──
def _mm_undo(rec):
    """-> (done, msg). done False = stack me rehne do."""
    kind = rec.get("kind")
    with _MM_LOCK:
        b, err = _mm_read()
        if err:
            return False, err
        now = _tg_now_ms()
        if kind == "mm_add":
            n0 = len(b["tx"])
            b["tx"] = [t for t in b["tx"] if not (isinstance(t, dict) and t.get("id") == rec.get("id"))]
            if len(b["tx"]) == n0:
                return True, f"Entry app me ab nahi hai (pehle hi hat chuki): {rec.get('text')}"
            _mm_tg_log(b, rec["id"], now, dele=True)
            msg = f"↩️ Undo: Entry hata di: {rec.get('text')}"
        elif kind == "mm_del":
            have = {t.get("id") for t in b["tx"] if isinstance(t, dict)}
            back = [t for t in (rec.get("txs") or []) if isinstance(t, dict) and t.get("id") and t["id"] not in have]
            for t in back:
                b["tx"].append(t)
                _mm_tg_log(b, t["id"], now)
                now += 1
            msg = f"↩️ Undo: {len(back)} entry wapas aa gayi" if back else "Ye entries pehle se app me hain."
        elif kind == "mm_edit":
            old = rec.get("old")
            ix = next((i for i, t in enumerate(b["tx"]) if isinstance(t, dict) and t.get("id") == rec.get("id")), -1)
            if ix < 0 or not isinstance(old, dict):
                return True, "Entry app me ab nahi mili, undo nahi hua."
            b["tx"][ix] = old
            _mm_tg_log(b, old["id"], now)
            msg = f"↩️ Undo: Edit wapas: {rec.get('text')}"
        else:
            return True, "Undo samajh nahi aaya, hata diya."
        ok, _m = _local_state_save_raw("moneymgr", b)
    if not ok:
        return False, "⚠️ Undo save nahi ho paya. `undo` dobara likho."
    return True, msg

for _mm_k in _MM_UNDO_KINDS:
    _TG_UNDO_KINDS[_mm_k] = lambda b, rec: (None, "")      # asli kaam `_mm_undo` (_tg_h_undo ke andar se)

_TG_HELP = _TG_HELP.replace("\n• undo —", "\n• 💵 Money Manager: kharcha 250 food/chai · aay 5000 salary · mm (list) · mm mahina · mm del/edit · `mm help` = poori list\n• undo —", 1)

_TG_COMMANDS = (
    ((("cb",), _mm_cb_wrap),) +
    tuple(((w,), _mm_h_add(t)) for w, t in _MM_TYPE_WORD.items()) +
    ((("k",), _mm_h_add("expense")), (("mm",), _mm_h_main))
) + tuple(_TG_COMMANDS)
# ═══ MM-TG END ═══

def _plan_sent_load() -> dict:
    try:
        if os.path.exists(_PLAN_SENT_FILE):
            with open(_PLAN_SENT_FILE, "r", encoding="utf-8") as f:
                x = json.load(f)
            return x if isinstance(x, dict) else {}
    except Exception as e:
        _slog_exception("_plan_sent_load", e)
    return {}

def _plan_sent_save(x: dict) -> None:
    tmp = _PLAN_SENT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(x, f)
        os.replace(tmp, _PLAN_SENT_FILE)
    except Exception as e:
        _slog_exception("_plan_sent_save", e)

def _plan_loop():
    while True:
        sleep_for = 1800
        try:
            now = _ist_now()
            target = now.replace(hour=_PLAN_HOUR, minute=_PLAN_MIN, second=0, microsecond=0)
            age = (now - target).total_seconds()
            d = now.date().isoformat()
            if age < 0:
                sleep_for = min(1800, max(5.0, -age + 1))
            elif age <= _PLAN_GRACE_SEC:
                sent = _plan_sent_load()
                if not sent.get(d):
                    st_, msg = _plan_send(d)
                    _slog(f"[today-plan] {d} -> {st_}: {msg}", level="ok" if st_ != "err" else "err")
                    if st_ == "err":
                        sleep_for = _REMINDER_RETRY_SEC
                    else:
                        sent[d] = {"ts": time.time(), "res": st_}
                        cut = (now.date() - datetime.timedelta(days=30)).isoformat()
                        _plan_sent_save({k: v for k, v in sent.items() if k >= cut})
        except Exception as e:
            _slog_exception("_plan_loop", e)
            sleep_for = 60
        time.sleep(sleep_for)

def _ensure_plan_thread():
    if "TodayPlan" not in {t.name for t in threading.enumerate()}:
        threading.Thread(target=_plan_loop, name="TodayPlan", daemon=True).start()

_ensure_plan_thread()

@_ws_action("today_plan_now")
def _wsa_today_plan_now(data):
    """📧 Email tab ka 'Aaj ka Plan abhi bhejo' button (test)."""
    st_, msg = _plan_send(_ist_now().date().isoformat())
    _slog(f"[today-plan] manual -> {st_}: {msg}", level="ok" if st_ != "err" else "err")
    return {"ok": st_ == "ok", "status": st_, "msg": msg}

# ─── 🕉️ Ekadashi alert (2026-10-01) ───────────────────────────────────────────
# Ekadashi ki tareekh har mahine badalti hai, isliye list yahan fixed hai (aam/Delhi panchang, English calendar dates).
# Har Ekadashi se EK DIN PEHLE raat 7 PM (IST) par email + Telegram alert. ⏰ panel ka "🕉️ Ekadashi" tab yahi list dikhata hai.
# Nayi tareekhein jodni ho to neeche _EKADASHI_LIST me ("YYYY-MM-DD", "Naam") add karo.
_EKADASHI_LIST = (
    ("2026-10-06", "Indira Ekadashi"),
    ("2026-10-22", "Papankusha Ekadashi"),
    ("2026-11-05", "Rama Ekadashi"),
    ("2026-11-20", "Devutthana Ekadashi"),
    ("2026-12-04", "Utpanna Ekadashi"),
    ("2026-12-20", "Mokshada Ekadashi"),
    # 2027 — drikpanchang / hindusphere / panchangbodh / ISKCON se milaya (udaya-tithi, Delhi style). Kuch din panchang-wise ±1 din badal sakte hain
    # (jaise Apr 16/17, Jun 1, Jul 29/30, Aug 28) — alert 1 din pehle jaata hai, isliye apne panchang se ek baar check kar lo.
    ("2027-01-03", "Saphala Ekadashi"),
    ("2027-01-19", "Pausha Putrada Ekadashi"),
    ("2027-02-02", "Shattila Ekadashi"),
    ("2027-02-17", "Jaya Ekadashi"),
    ("2027-03-04", "Vijaya Ekadashi"),
    ("2027-03-18", "Amalaki Ekadashi"),
    ("2027-04-02", "Papmochani Ekadashi"),
    ("2027-04-17", "Kamada Ekadashi"),
    ("2027-05-02", "Varuthini Ekadashi"),
    ("2027-05-16", "Mohini Ekadashi"),
    ("2027-06-01", "Apara Ekadashi"),
    ("2027-06-14", "Nirjala Ekadashi"),
    ("2027-06-30", "Yogini Ekadashi"),
    ("2027-07-14", "Devshayani Ekadashi"),
    ("2027-07-29", "Kamika Ekadashi"),
    ("2027-08-12", "Shravana Putrada Ekadashi"),
    ("2027-08-28", "Aja Ekadashi"),
    ("2027-09-11", "Parsva Ekadashi"),
    ("2027-09-26", "Indira Ekadashi"),
    ("2027-10-11", "Papankusha Ekadashi"),
    ("2027-10-25", "Rama Ekadashi"),
    ("2027-11-10", "Devutthana Ekadashi"),
    ("2027-11-24", "Utpanna Ekadashi"),
    ("2027-12-09", "Mokshada Ekadashi"),
    ("2027-12-23", "Saphala Ekadashi"),
)
# Nayi Ekadashi bina code badle: LOCAL_STATE_DIR/ekadashi_extra.json me [["YYYY-MM-DD","Naam"], ...] likh do (ya app.py ki list me jodo).
_EKADASHI_EXTRA_FILE = os.path.join(LOCAL_STATE_DIR, "ekadashi_extra.json")
_EKADASHI_WARN_DAYS = 45

def _ekadashi_all() -> list:
    """_EKADASHI_LIST + ekadashi_extra.json, date se sorted, duplicate dates ek."""
    out = {d: n for d, n in _EKADASHI_LIST}
    try:
        if os.path.exists(_EKADASHI_EXTRA_FILE):
            with open(_EKADASHI_EXTRA_FILE, "r", encoding="utf-8") as f:
                for row in json.load(f):
                    if isinstance(row, (list, tuple)) and len(row) >= 2 and _tg_is_iso(str(row[0])):
                        out[str(row[0])] = str(row[1])[:60]
    except Exception as e:
        _slog_exception("_ekadashi_all", e)
    return sorted(out.items())
_EKADASHI_ALERT_HOUR, _EKADASHI_ALERT_MIN = 19, 0
_EKADASHI_GRACE_SEC = 3 * 3600        # 7 PM ke baad itni der tak late-send allowed (Space so raha ho to)
_EKADASHI_SENT_FILE = os.path.join(LOCAL_STATE_DIR, "ekadashi_sent.json")
_EKADASHI_LOCK = threading.Lock()

def _ekadashi_sent_load() -> dict:
    try:
        if os.path.exists(_EKADASHI_SENT_FILE):
            with open(_EKADASHI_SENT_FILE, "r", encoding="utf-8") as f:
                x = json.load(f)
            return x if isinstance(x, dict) else {}
    except Exception as e:
        _slog_exception("_ekadashi_sent_load", e)
    return {}

def _ekadashi_sent_save(x: dict) -> None:
    tmp = _EKADASHI_SENT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(x, f)
        os.replace(tmp, _EKADASHI_SENT_FILE)
    except Exception as e:
        _slog_exception("_ekadashi_sent_save", e)

def _ekadashi_alert_dt(d: str):
    """Ekadashi `d` se ek din pehle 7 PM IST (tz-aware)."""
    day = datetime.date.fromisoformat(d) - datetime.timedelta(days=1)
    return datetime.datetime(day.year, day.month, day.day, _EKADASHI_ALERT_HOUR, _EKADASHI_ALERT_MIN, tzinfo=IST)

def _ekadashi_notify_loop():
    while True:
        sleep_for = 1800
        try:
            now = _ist_now()
            _ek = _ekadashi_all()
            try:   # list khatam hone se 45 din pehle ek baar yaad dilao
                _last = _ek[-1][0] if _ek else ""
                if _last and (datetime.date.fromisoformat(_last) - now.date()).days <= _EKADASHI_WARN_DAYS:
                    with _EKADASHI_LOCK:
                        _sw = _ekadashi_sent_load()
                        if not _sw.get("warn:" + _last):
                            _rw = _alert_email_and_telegram("🕉️ Ekadashi list khatam hone wali hai",
                                                            f"Aakhri Ekadashi {_last} ki hai. Nayi tareekhein app.py ki _EKADASHI_LIST me ya ekadashi_extra.json me jod do.")
                            if _rw == "ok":
                                _sw["warn:" + _last] = time.time()
                                _ekadashi_sent_save(_sw)
            except Exception as _e_w:
                _slog_exception("ekadashi warn", _e_w)
            for d, name in _ek:
                age = (now - _ekadashi_alert_dt(d)).total_seconds()
                if age < 0:
                    sleep_for = min(sleep_for, max(5.0, -age + 1))
                    continue
                if age > _EKADASHI_GRACE_SEC:
                    continue
                with _EKADASHI_LOCK:
                    sent = _ekadashi_sent_load()
                    if sent.get(d):
                        continue
                    dd = datetime.date.fromisoformat(d)
                    wk = _ROUTINE_WEEKDAYS[(dd.weekday() + 1) % 7]
                    res = _alert_email_and_telegram(
                        f"🕉️ Kal Ekadashi hai — {name}",
                        f"Kal {dd.day}/{dd.month} ({wk}) ko {name} hai.")
                    _slog(f"[ekadashi] {d} {name} alert -> {res}", level="ok" if res == "ok" else "err")
                    if res == "ok":
                        sent[d] = time.time()
                        _ekadashi_sent_save(sent)
                    else:
                        sleep_for = min(sleep_for, _REMINDER_RETRY_SEC)
        except Exception as e:
            _slog_exception("_ekadashi_notify_loop", e)
            sleep_for = 60
        time.sleep(sleep_for)

def _ensure_ekadashi_thread():
    if "EkadashiNotify" not in {t.name for t in threading.enumerate()}:
        threading.Thread(target=_ekadashi_notify_loop, name="EkadashiNotify", daemon=True).start()

_ensure_ekadashi_thread()

@_ws_action("ekadashi_list")
def _wsa_ekadashi_list(data):
    """⏰ panel ka 🕉️ Ekadashi tab — list + har ek ka alert time aur alert gaya ya nahi."""
    sent = _ekadashi_sent_load()
    out = []
    for d, name in _ekadashi_all():
        a = _ekadashi_alert_dt(d)
        out.append({"date": d, "name": name, "alert_date": a.date().isoformat(),
                    "alert_time": a.strftime("%H:%M"), "sent": bool(sent.get(d))})
    return {"ok": True, "items": out}

# ─── 📊 Weekly + Monthly Success/Fail Report (2026-10-02, v2) ─────────────────
# Har Practice STEP ki apni start date hai. Us din se: 1 hafta = 7 din, 1 mahina = 28 din (= 4 hafte).
# Weekly report: step ke har 7vein din (start+6, +13, ...) raat 8:30 PM IST. Monthly: har 28vein din (start+27, +55, ...) —
# wo din hamesha hafte ka bhi aakhri din hota hai, isliye usi din weekly + monthly DONO aati hain.
# Score = sections (Routine / To-Do / Habit / us step ki Practice) ke % ka average; >= 80% => SUCCESS, warna FAIL.   # DIPANSHU-STEP-3 (2026-10-03): 95 -> 80 (sab reports)
# Report banate hi /data/app_state/reports.json me FREEZE (snapshot) — baad me data badle to report nahi badalti.
# Read-only: ⏰ panel ka 📊 Reports tab sirf padhta hai (WS "reports_list"). Email + Telegram par bhi jaati hai.
_REPORT_HOUR, _REPORT_MIN = 20, 30
_REPORT_PASS_PCT = 80                   # DIPANSHU-STEP-3 (2026-10-03): common 80% (pehle 95)
_REPORT_GRACE_SEC = 8400                # 8:30 PM ke baad ~2h20m tak late-send allowed (11 PM tak)
_REPORTS_FILE = os.path.join(LOCAL_STATE_DIR, "reports.json")
_REPORT_LOCK = threading.RLock()
_REPORT_KEEP = {"week": 80, "month": 30}
_REP_DAY2 = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

def _rep_pass(pct) -> bool:
    """Display jaisa hi rule: rounded % >= 80 => SUCCESS (80% dikhe to FAIL na ho). # DIPANSHU-STEP-3 (2026-10-03)"""
    return pct is not None and int(pct + 0.5) >= _REPORT_PASS_PCT

def _reports_load() -> list:
    try:
        if os.path.exists(_REPORTS_FILE):
            with open(_REPORTS_FILE, "r", encoding="utf-8") as f:
                x = json.load(f)
            return [r for r in x if isinstance(r, dict)] if isinstance(x, list) else []
    except Exception as e:
        _slog_exception("_reports_load", e)
    return []

def _reports_save(lst: list) -> None:
    tmp = f"{_REPORTS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lst, f)
        os.replace(tmp, _REPORTS_FILE)
    except Exception as e:
        _slog_exception("_reports_save", e)

def _rep_ms_date(ms):
    try:
        v = int(ms or 0)
        if v <= 0:
            return ""
        return datetime.datetime.fromtimestamp(v / 1000, IST).date().isoformat()
    except Exception:
        return ""

def _rep_dm(d: str) -> str:
    return f"{int(d[8:10])}/{int(d[5:7])}"

def _rep_range_label(a: datetime.date, z: datetime.date) -> str:
    if a.month == z.month and a.year == z.year:
        return f"{a.day}-{z.day} {z.strftime('%b')} {z.year}"
    return f"{a.day} {a.strftime('%b')} - {z.day} {z.strftime('%b')} {z.year}"

def _report_compute(b: dict, kind: str, start: datetime.date, end: datetime.date, step: dict) -> dict:
    """Period [start,end] ka hisaab (aaj ke baad ke din ginti me nahi). Practice = sirf `step`."""
    today = _ist_now().date()
    last = min(end, today)
    days = []
    d0 = start
    while d0 <= last:
        days.append(d0)
        d0 += datetime.timedelta(days=1)
    sd, ed = start.isoformat(), last.isoformat()
    secs, misses, per_day = [], [], {}
    skips, excused = _skip_all(), []          # ⏸ skip: hisaab (total) se bahar

    def _created_after(x, dd):
        c = _rep_ms_date(x.get("created"))
        return bool(c) and c > dd.isoformat()

    # 🌅 Routine + 🔁 Habit (scheduled din ginte hain)
    for key, title, lst_k, done_k, miss_k, ico in (
            ("r", "Routine", "routines", "done", "miss", "🌅"),
            ("h", "Habit", "habits", "hdone", "hmiss", "🔁")):
        tot = dn_n = 0
        for dd in days:
            d, dow = dd.isoformat(), (dd.weekday() + 1) % 7
            dm, mm = (b.get(done_k) or {}).get(d, {}), (b.get(miss_k) or {}).get(d, {})
            for x in b.get(lst_k, []):
                if not isinstance(x, dict) or dow not in (x.get("days") or []) or _created_after(x, dd):
                    continue
                iid = str(x.get("id"))
                _sk = (skips.get(d) or {}).get(f"{key}|{iid}")
                if _sk is not None and not dm.get(iid):
                    excused.append((d, ico, str(x.get("label", "")), str(_sk.get("why", "")) if isinstance(_sk, dict) else ""))
                    continue
                tot += 1
                pd_ = per_day.setdefault(d, [0, 0])
                pd_[1] += 1
                if dm.get(iid):
                    dn_n += 1
                    pd_[0] += 1
                else:
                    why = (mm.get(iid) or {}).get("why", "") if isinstance(mm.get(iid), dict) else ""
                    misses.append((d, ico, str(x.get("label", "")), why))
        if tot:
            secs.append({"key": key, "label": f"{ico} {title}", "done": dn_n, "total": tot, "pct": dn_n * 100.0 / tot})

    # ✅ To-Do (due / done / miss us period me)
    inr = lambda v: bool(v) and sd <= v <= ed
    tt = td = 0
    for x in b.get("todos", []):
        if not isinstance(x, dict):
            continue
        due = str(x.get("due") or "")
        da = _rep_ms_date(x.get("doneAt")) if x.get("done") else ""
        m = x.get("miss") if isinstance(x.get("miss"), dict) else {}
        md = str(m.get("d", ""))
        if inr(due) or inr(da) or inr(md):
            _sk = None if x.get("done") else _skip_find(skips, "t", str(x.get("id")), sd, ed)
            if _sk:
                excused.append((_sk[0], "✅", str(x.get("text", "")), str(_sk[1].get("why", "")) if isinstance(_sk[1], dict) else ""))
                continue
            tt += 1
            if x.get("done") and (not da or da <= ed):
                td += 1
            else:
                misses.append((md or due, "✅", str(x.get("text", "")), str(m.get("why", "")) if md else ""))
    if tt:
        secs.append({"key": "t", "label": "✅ To-Do", "done": td, "total": tt, "pct": td * 100.0 / tt})

    # 🗓 Sunday (DIPANSHU-STEP-4, 2026-10-03): har task ke jitne check-dates [start, last] me aaye, wo total; done = s "d"; baaki miss
    sun_tot = sun_dn = 0
    for tk in _sun_tasks(b):
        tdone = tk.get("done") if isinstance(tk.get("done"), dict) else {}
        for n in _sun_checks(tk):
            sdt = _sun_date(tk.get("start"), n)
            if sdt is None or not (start <= sdt <= last):
                continue
            rec_n = tdone.get(str(n)) if isinstance(tdone.get(str(n)), dict) else {}
            _sk = None if rec_n.get("s") == "d" else _skip_find(skips, "s", f"{tk.get('id')}:{n}", sdt.isoformat(), ed)
            if _sk:
                excused.append((_sk[0], "🗓", f"{tk.get('label', '')} (Sunday {n})", str(_sk[1].get("why", "")) if isinstance(_sk[1], dict) else ""))
                continue
            sun_tot += 1
            pd_ = per_day.setdefault(sdt.isoformat(), [0, 0])
            pd_[1] += 1
            if rec_n.get("s") == "d":
                sun_dn += 1
                pd_[0] += 1
            else:
                misses.append((sdt.isoformat(), "🗓", f"{tk.get('label', '')} (Sunday {n})",
                               str(rec_n.get("why", "")) if rec_n.get("s") == "s" else ""))
    if sun_tot:
        secs.append({"key": "s", "label": "🗓 Sunday", "done": sun_dn, "total": sun_tot, "pct": sun_dn * 100.0 / sun_tot})

    # 🏖 Holiday (2026-10-04): jin dino holiday set tha, un dino ke holiday kaam (skip = hisaab se bahar)
    try:
        _hh = _hol_read()
        h_tot = h_dn = 0
        for dd in days:
            d_ = dd.isoformat()
            for it in _hol_items(d_, _hh):
                _sk = (skips.get(d_) or {}).get(f"o|{it['id']}")
                if _sk is not None and it["s"] != "d":
                    excused.append((d_, "🏖", it["label"], str(_sk.get("why", "")) if isinstance(_sk, dict) else ""))
                    continue
                h_tot += 1
                pd_ = per_day.setdefault(d_, [0, 0])
                pd_[1] += 1
                if it["s"] == "d":
                    h_dn += 1
                    pd_[0] += 1
                else:
                    misses.append((d_, "🏖", it["label"], it["why"] if it["s"] == "m" else ""))
        if h_tot:
            secs.append({"key": "o", "label": "🏖 Holiday", "done": h_dn, "total": h_tot, "pct": h_dn * 100.0 / h_tot})
    except Exception as e:
        _slog_exception("_report_compute holiday", e)

    # 🎯 Practice (sirf is step ki): period ke sets vs target — weekly = monthly/4, monthly = monthly
    prac = []
    m_t, w_t, _d = (0, 0, 0) if step.get("main") else _prac_targets(step)   # DIPANSHU-STEP-4: "Main" stream me Practice section nahi
    tgt = m_t if kind == "month" else w_t
    sid = str(step.get("id"))
    _plg = b.get("plog") or {}
    _ex_d = [dd for dd in days if f"p|{sid}" in (skips.get(dd.isoformat()) or {})
             and not float((_plg.get(dd.isoformat()) or {}).get(sid) or 0)]     # skip + us din 0 sets -> us din ka target hata do
    if tgt > 0 and _ex_d:
        _lbl0 = str(step.get("label", "Practice"))
        for dd in _ex_d:
            _r0 = (skips.get(dd.isoformat()) or {}).get(f"p|{sid}")
            excused.append((dd.isoformat(), "🎯", _lbl0, str(_r0.get("why", "")) if isinstance(_r0, dict) else ""))
        tgt = max(0.0, tgt - (m_t / 28.0) * len(_ex_d))
    if tgt > 0:
        sets = _prac_sum(b, sid, start, last)
        pct = min(100.0, sets * 100.0 / tgt)
        lbl = str(step.get("label", "Practice"))
        secs.append({"key": "p", "label": f"🎯 {lbl}", "done": sets, "total": tgt, "pct": pct, "sets": True})
        daily = [(dd, float((b.get("plog") or {}).get(dd.isoformat(), {}).get(sid) or 0)) for dd in days]
        prac.append({"label": lbl, "sid": sid, "daily": daily})
        if sets < tgt:
            misses.append((ed, "🎯", f"{lbl}: {_fmt_n(sets)}/{_fmt_n(tgt)} sets", f"{_fmt_n(tgt - sets)} sets kam"))

    pct = (sum(x["pct"] for x in secs) / len(secs)) if secs else None
    for x in secs:
        x["ok"] = _rep_pass(x["pct"])
    ok = None if pct is None else _rep_pass(pct)
    return {"secs": secs, "misses": sorted(misses), "days": days, "per_day": per_day, "prac": prac, "pct": pct, "ok": ok,
            "excused": excused}

def _report_build(b: dict, kind: str, start: datetime.date, end: datetime.date, saved: list, step: dict, preview: bool = False) -> dict:
    c = _report_compute(b, kind, start, end, step)
    sid, slabel = str(step.get("id")), str(step.get("label", "Practice"))
    title = "Weekly" if kind == "week" else "Monthly"
    label = _rep_range_label(start, end)
    # ── Section-wise success (alag alag) + TOTAL (sabka average) ──
    head = [f"📊 {title} Report · {slabel} · {label}" + (" (abhi tak, preview)" if preview else ""), ""]
    sec_lines, sec_rows = ["━ Section-wise (80%+ = success) ━"], []   # DIPANSHU-STEP-3 (2026-10-03)
    for x in c["secs"]:
        tag = "✅ SUCCESS" if x["ok"] else "❌ FAIL"
        cnt = f"{_fmt_n(x['done'])}/{_fmt_n(x['total'])} sets" if x.get("sets") else f"{x['done']}/{x['total']}"
        sec_lines.append(f"{x['label']}: {cnt} — {x['pct']:.0f}% {tag}")
        sec_rows.append({"label": x["label"], "done": x["done"], "total": x["total"], "pct": round(x["pct"], 1),
                         "ok": x["ok"], "sets": bool(x.get("sets"))})
    if not c["secs"]:
        sec_lines.append("Is period me koi Routine/To-Do/Habit/Practice data nahi mila.")
    if c["pct"] is None:
        tot_lines = ["", "📈 TOTAL: DATA NAHI"]
    else:
        tot_lines = ["", "━ Total ━", f"📈 TOTAL: {c['pct']:.0f}% " + ("✅ SUCCESS" if c["ok"] else "❌ FAIL") +
                     f"  ({len(c['secs'])} section ka average)"]
    res = "DATA NAHI" if c["ok"] is None else (("✅ SUCCESS" if c["ok"] else "❌ FAIL") + f" — {c['pct']:.0f}%")
    L = head + sec_lines + tot_lines
    dstart = len(L)
    if kind == "week":
        for pr in c["prac"]:
            L += ["", f"🎯 {pr['label']} din-ba-din: " + " · ".join(f"{_rep_dm(dd.isoformat())} {_fmt_n(v)}" for dd, v in pr["daily"])]
    else:
        wk = [r for r in saved if r.get("type") == "week" and r.get("step") == sid
              and start.isoformat() <= str(r.get("end", "")) <= end.isoformat()]
        wk.sort(key=lambda r: r.get("end", ""))
        if wk:
            L += ["", "📅 Hafte ke result:"]
            for i, r in enumerate(wk, 1):
                L.append(f"  Hafta {i} ({r.get('label', '')}): " + ("DATA NAHI" if r.get("ok") is None else (("✅" if r["ok"] else "❌") + f" {r.get('pct', 0):.0f}%")))
        sc = [(v[0] * 100.0 / v[1], d) for d, v in c["per_day"].items() if v[1] > 0]
        if sc:
            best, worst = max(sc, key=lambda t: (t[0], -int(t[1][8:10]))), min(sc, key=lambda t: (t[0], int(t[1][8:10])))
            if abs(best[0] - worst[0]) >= 0.5:
                L += ["", f"🏆 Sabse achha din: {_rep_dm(best[1])} ({best[0]:.0f}%)", f"⚠️ Sabse kamzor din: {_rep_dm(worst[1])} ({worst[0]:.0f}%)"]
        for pr in c["prac"]:
            tot = sum(v for _d, v in pr["daily"])
            act = sum(1 for _d, v in pr["daily"] if v > 0)
            L += ["", f"🎯 {pr['label']}: {_fmt_n(tot)} sets, {act} din practice hui"]
            _ls = _lowsets_all()
            for _dd, _v in pr["daily"]:
                _rl = (_ls.get(_dd.isoformat()) or {}).get(pr.get("sid")) or {}
                if _v > 0 and _rl.get("why"):
                    L.append(f"   ↳ {_rep_dm(_dd.isoformat())}: {_fmt_n(_v)} set — {_rl['why']}")
    if c["misses"]:
        L += ["", "❌ Miss hue (reason ke saath):"]
        for d, ico, lbl, why in c["misses"][:14]:
            L.append(f"  • {_rep_dm(d)} {ico} {lbl}" + (f" — {why}" if why else ""))
        if len(c["misses"]) > 14:
            L.append(f"  …aur {len(c['misses']) - 14} miss")
    L += _skip_lines(c.get("excused") or [])
    detail = "\n".join(L[dstart:]).strip("\n")
    subj = f"{title} Report {slabel} {label} - " + ("DATA NAHI" if c["ok"] is None else (("SUCCESS" if c["ok"] else "FAIL") + f" {c['pct']:.0f}%"))
    rid = ("w:" if kind == "week" else "m:") + sid + ":" + end.isoformat()
    return {"id": rid, "type": kind, "step": sid, "step_label": slabel, "start": start.isoformat(), "end": end.isoformat(),
            "label": label, "pct": None if c["pct"] is None else round(c["pct"], 1), "ok": c["ok"], "subject": subj,
            "text": _top_note_prefix() + "\n".join(L), "secs": sec_rows, "detail": detail,
            "created": time.time(), "sent": 0}

# ═══ DIPANSHU-STEP-3 (2026-10-03): 👦 Dipanshu ki alag weekly (7 din) + monthly (28 din) report ═══
# Start = dipanshu.start (pehli aadat ka din). Score = Σdone / Σtotal (aadat-din), 80%+ = success. Practice se koi lena-dena nahi.
def _dip_start(b: dict):
    """dipanshu.start (valid ISO) -> date; warna sabse purani aadat ke `created` ki IST date; warna None."""
    dp = b.get("dipanshu") if isinstance(b, dict) else None
    if not isinstance(dp, dict):
        return None
    try:
        return datetime.date.fromisoformat(str(dp.get("start") or ""))
    except Exception:
        pass
    ds = sorted(x for x in (_rep_ms_date(h.get("created")) for h in (dp.get("habits") if isinstance(dp.get("habits"), list) else [])
                            if isinstance(h, dict)) if x)
    try:
        return datetime.date.fromisoformat(ds[0]) if ds else None
    except Exception:
        return None

def _dip_due(b: dict, today: datetime.date) -> list:
    """Aaj Dipanshu ki kaun si report banni hai: [(kind, start, end)] — weekly pehle, phir monthly."""
    s0 = _dip_start(b)
    if s0 is None or today < s0:
        return []
    k = (today - s0).days
    out = []
    if k >= 6 and k % 7 == 6:
        out.append(("week", today - datetime.timedelta(days=6), today))
    if k >= 27 and k % 28 == 27:
        out.append(("month", today - datetime.timedelta(days=27), today))
    return out

def _dip_compute(b: dict, kind: str, start: datetime.date, end: datetime.date) -> dict:
    today = _ist_now().date()
    last = min(end, today)
    days = []
    d = start
    while d <= last:
        days.append(d)
        d += datetime.timedelta(days=1)
    dp = b.get("dipanshu") if isinstance(b.get("dipanshu"), dict) else {}
    done_m = dp.get("done") if isinstance(dp.get("done"), dict) else {}
    miss_m = dp.get("miss") if isinstance(dp.get("miss"), dict) else {}
    secs, misses, per_day = [], [], {}
    skips, excused = _skip_all(), []
    sd, st = 0, 0
    for h in (dp.get("habits") if isinstance(dp.get("habits"), list) else []):
        if not isinstance(h, dict):
            continue
        hid = str(h.get("id"))
        hdays = h.get("days") or []
        cd = _rep_ms_date(h.get("created"))
        tot = dn = 0
        for dd in days:
            ds = dd.isoformat()
            if ((dd.weekday() + 1) % 7) not in hdays or (cd and ds < cd):
                continue
            _sk = (skips.get(ds) or {}).get(f"dp|{hid}")
            if _sk is not None and not (done_m.get(ds) or {}).get(hid):
                excused.append((ds, "👦", str(h.get("label", "")), str(_sk.get("why", "")) if isinstance(_sk, dict) else ""))
                continue
            tot += 1
            pd_ = per_day.setdefault(ds, [0, 0])
            pd_[1] += 1
            if (done_m.get(ds) or {}).get(hid):
                dn += 1
                pd_[0] += 1
            else:
                mm = (miss_m.get(ds) or {}).get(hid)
                misses.append((ds, "👦", str(h.get("label", "")), str(mm.get("why", "")) if isinstance(mm, dict) else ""))
        if tot == 0:
            continue
        pct_h = dn * 100.0 / tot
        secs.append({"key": hid, "label": "👦 " + str(h.get("label", "")), "done": dn, "total": tot, "pct": pct_h, "ok": _rep_pass(pct_h)})
        sd += dn
        st += tot
    pct = (sd * 100.0 / st) if st > 0 else None
    return {"secs": secs, "misses": sorted(misses), "days": days, "per_day": per_day, "pct": pct,
            "ok": None if pct is None else _rep_pass(pct), "sd": sd, "st": st, "excused": excused}

def _dip_report_build(b: dict, kind: str, start: datetime.date, end: datetime.date, saved: list, preview: bool = False) -> dict:
    c = _dip_compute(b, kind, start, end)
    title = "Weekly" if kind == "week" else "Monthly"
    label = f"{start.day} {start.strftime('%b')} – {end.day} {end.strftime('%b')}"
    L = [f"📊 {title} Report · 👦 Dipanshu · {label}" + (" (abhi tak, preview)" if preview else ""), "", "━ Aadat-wise (80%+ = success) ━"]
    rows = []
    for x in c["secs"]:
        L.append(f"{x['label']}: {x['done']}/{x['total']} — {x['pct']:.0f}% " + ("✅ SUCCESS" if x["ok"] else "❌ FAIL"))
        rows.append({"label": x["label"], "done": x["done"], "total": x["total"], "pct": round(x["pct"], 1), "ok": x["ok"], "sets": False})
    if not c["secs"]:
        L.append("Is period me koi Dipanshu aadat data nahi mila.")
    if c["pct"] is None:
        L += ["", "━ Total ━", "📈 TOTAL: DATA NAHI"]
    else:
        L += ["", "━ Total ━", f"📈 TOTAL: {c['pct']:.0f}% " + ("✅ SUCCESS" if c["ok"] else "❌ FAIL") + f"  ({c['sd']}/{c['st']} aadat-din)"]
    dstart = len(L)
    if kind == "week":
        pdl = [f"{_rep_dm(d)} {v[0]}/{v[1]}" for d, v in sorted(c["per_day"].items()) if v[1] > 0]
        if pdl:
            L += ["", "Din-ba-din: " + " · ".join(pdl)]
    else:
        wk = [r for r in saved if r.get("type") == "week" and r.get("step") == "dipanshu"
              and start.isoformat() <= str(r.get("end", "")) <= end.isoformat()]
        wk.sort(key=lambda r: r.get("end", ""))
        if wk:
            L += ["", "📅 Hafte ke result:"]
            for i, r in enumerate(wk, 1):
                L.append(f"  Hafta {i} ({r.get('label', '')}): " + ("DATA NAHI" if r.get("ok") is None else (("✅" if r["ok"] else "❌") + f" {r.get('pct', 0):.0f}%")))
        sc = [(v[0] * 100.0 / v[1], d) for d, v in c["per_day"].items() if v[1] > 0]
        if sc:
            best, worst = max(sc, key=lambda t: (t[0], -int(t[1][8:10]))), min(sc, key=lambda t: (t[0], int(t[1][8:10])))
            if abs(best[0] - worst[0]) >= 0.5:
                L += ["", f"🏆 Sabse achha din: {_rep_dm(best[1])} ({best[0]:.0f}%)", f"⚠️ Sabse kamzor din: {_rep_dm(worst[1])} ({worst[0]:.0f}%)"]
    if c["misses"]:
        L += ["", "❌ Miss hue (reason ke saath):"]
        for d, ico, lbl, why in c["misses"][:14]:
            L.append(f"  • {_rep_dm(d)} {ico} {lbl}" + (f" — {why}" if why else ""))
        if len(c["misses"]) > 14:
            L.append(f"  …aur {len(c['misses']) - 14} miss")
    L += _skip_lines(c.get("excused") or [])
    detail = "\n".join(L[dstart:]).strip("\n")
    subj = f"{title} Report 👦 Dipanshu {label} - " + ("DATA NAHI" if c["ok"] is None else (("SUCCESS" if c["ok"] else "FAIL") + f" {c['pct']:.0f}%"))
    return {"id": ("w:" if kind == "week" else "m:") + "dipanshu:" + end.isoformat(), "type": kind, "step": "dipanshu",
            "step_label": "👦 Dipanshu", "start": start.isoformat(), "end": end.isoformat(), "label": label,
            "pct": None if c["pct"] is None else round(c["pct"], 1), "ok": c["ok"], "subject": subj,
            "text": _top_note_prefix() + "\n".join(L), "secs": rows, "detail": detail, "created": time.time(), "sent": 0}
# ═══ DIPANSHU-STEP-3 END ═══

# ═══ DIPANSHU-STEP-4 (2026-10-03): "Main" fallback report — jab ek bhi Practice step nahi (tab pehle koi report banti hi nahi thi) ═══
_REPORTS_META_FILE = os.path.join(LOCAL_STATE_DIR, "reports_meta.json")
_MAIN_STEP = {"id": "main", "label": "📋 Routine", "main": True}

def _main_start(b: dict) -> datetime.date:
    """Main stream ki start date: reports_meta.json ka main_start; na ho to routines/habits/todos/sunday-tasks ke sabse purane
    `created` (IST) ki date (ya aaj) — file me atomically likh do. Anchor kabhi routine blob me nahi rakhte."""
    try:
        if os.path.exists(_REPORTS_META_FILE):
            with open(_REPORTS_META_FILE, "r", encoding="utf-8") as f:
                m = json.load(f)
            if isinstance(m, dict):
                return datetime.date.fromisoformat(str(m.get("main_start") or ""))
    except Exception:
        pass
    ds = []
    for key in ("routines", "habits", "todos"):
        ds += [_rep_ms_date(x.get("created")) for x in (b.get(key) if isinstance(b.get(key), list) else []) if isinstance(x, dict)]
    ds += [_rep_ms_date(x.get("created")) for x in _sun_tasks(b)]
    ds = sorted(x for x in ds if x)
    try:
        s0 = datetime.date.fromisoformat(ds[0]) if ds else _ist_now().date()
    except Exception:
        s0 = _ist_now().date()
    tmp = f"{_REPORTS_META_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"main_start": s0.isoformat()}, f)
        os.replace(tmp, _REPORTS_META_FILE)
    except Exception as e:
        _slog_exception("_main_start", e)
    return s0

# ═══ MAIN-SUNDAY (2026-10-04): Main report ab Practice se alag HAMESHA chalti hai, aur hafta Somvar–Sunday ka hota hai ═══
# Anchor (_main_start = sabse purana routine/habit/todo/sunday-task ka `created` din) sirf ye tay karta hai ki ginti KAHAN se shuru ho
# (har kaam apne add hone ke din se ginta hai — _report_compute me _created_after) aur pehla hafta kaun sa hai (anchor wala Somvar–Sunday hafta).
# Report hamesha Sunday raat 8:30 PM — chahe kisi habit ke 2 hi din hue hon. Monthly = har 4vein Sunday (28 din), usme chaaron hafton ke result.
def _main_cycle(b: dict, ref: datetime.date) -> tuple:
    """(first_sunday, week_sunday, n) — first_sunday: anchor wale hafte ka Sunday; week_sunday: ref wale hafte ka Sunday;
    n: first_sunday se kitne hafte baad (0-based, week_sunday < first_sunday ho to negative)."""
    s0 = _main_start(b)
    first = s0 - datetime.timedelta(days=s0.weekday()) + datetime.timedelta(days=6)
    wsun = ref + datetime.timedelta(days=6 - ref.weekday())
    return first, wsun, (wsun - first).days // 7

def _main_due(b: dict, today: datetime.date) -> list:
    """[(kind, start, end, step)] — sirf Sunday ko. Pehle weekly (Somvar–Sunday), phir har 4vein Sunday par monthly (28 din)."""
    if today.weekday() != 6:
        return []
    first, _ws, n = _main_cycle(b, today)
    if n < 0:
        return []
    out = [("week", today - datetime.timedelta(days=6), today, dict(_MAIN_STEP))]
    if (n + 1) % 4 == 0:
        out.append(("month", today - datetime.timedelta(days=27), today, dict(_MAIN_STEP)))
    return out
# ═══ DIPANSHU-STEP-4 END ═══

# ═══ HOLIDAY (2026-10-04) ═══
# Achanak chutti / holiday par us din ke extra kaam. Data holiday.json (LOCAL_STATE_DIR): SERVER hi akela writer hai
# (browser ⏰ → 🏖 Holiday tab WS action `holiday_op` se, Telegram `aaj holiday hai` / `kal holiday hai` se).
#   {"tasks":[{id,label,created,del}], "days":{"YYYY-MM-DD":{t}}, "done":{date:{taskId:"d"|"m"}}, "why":{date:{taskId:text}}}
# Holiday wale din ye kaam `_routine_collect` me k="o" items ban kar aate hain -> subah ka plan, shaam ki ✅/❌ checklist (Telegram + email) aur
# weekly/monthly report me apne aap. Lock order: _ROUTINE_LOCK pehle, phir _HOL_LOCK (kabhi ulta nahi).
_HOL_LOCK = threading.RLock()
_HOL_MAX_TASKS = 60

def _hol_file() -> str:
    return os.path.join(LOCAL_STATE_DIR, "holiday.json")

def _hol_blank() -> dict:
    return {"tasks": [], "days": {}, "done": {}, "why": {}}

def _hol_norm(h) -> dict:
    out = _hol_blank()
    if not isinstance(h, dict):
        return out
    for t in h.get("tasks") or []:
        if isinstance(t, dict) and t.get("id") and str(t.get("label", "")).strip():
            out["tasks"].append({"id": str(t["id"])[:24], "label": _tg_one(t.get("label"), 80),
                                 "created": int(t.get("created") or 0), "del": int(t.get("del") or 0)})
    if isinstance(h.get("days"), dict):
        out["days"] = {k: (v if isinstance(v, dict) else {}) for k, v in h["days"].items() if _tg_is_iso(k)}
    for key in ("done", "why"):
        src = h.get(key)
        if isinstance(src, dict):
            for d, m in src.items():
                if _tg_is_iso(d) and isinstance(m, dict):
                    out[key][d] = {str(i): v for i, v in m.items()
                                   if (v in ("d", "m") if key == "done" else isinstance(v, str))}
    return out

def _hol_read() -> dict:
    p = _hol_file()
    for path in (p, p + ".bak"):
        try:
            if os.path.exists(path):
                with _HOL_LOCK, open(path, "r", encoding="utf-8") as f:
                    return _hol_norm(json.load(f))
        except Exception as e:
            _slog_exception("_hol_read", e)
    return _hol_blank()

def _hol_write(h: dict) -> bool:
    p = _hol_file()
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with _HOL_LOCK:
            payload = json.dumps(h)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            if os.path.isfile(p) and os.path.getsize(p) > 2:
                try:
                    with open(p, "rb") as fb:
                        raw = fb.read()
                    json.loads(raw.decode("utf-8"))
                    with open(p + ".bak.tmp", "wb") as fb2:
                        fb2.write(raw)
                    os.replace(p + ".bak.tmp", p + ".bak")
                except Exception as e2:
                    _slog_exception("_hol_write bak", e2)
            os.replace(tmp, p)
        return True
    except Exception as e:
        _slog_exception("_hol_write", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

def _hol_items(d: str, h=None) -> list:
    """Din `d` ke holiday kaam (agar d holiday hai) -> [{k:"o",id,label,sub,s,why}]. Aaj/aage ki date par sab chalu kaam;
    beeti date par sirf wo jo us din tak ban chuke the aur us din tak hate nahi the."""
    h = h if isinstance(h, dict) else _hol_read()
    if d not in h["days"]:
        return []
    today = _tg_today()
    dm, wm = h["done"].get(d, {}), h["why"].get(d, {})
    out = []
    for t in h["tasks"]:
        cd = _rep_ms_date(t["created"]) if t["created"] else ""
        dl = _rep_ms_date(t["del"]) if t["del"] else ""
        if d >= today:
            if t["del"]:
                continue
        else:
            if (cd and cd > d) or (dl and dl <= d):
                continue
        st = dm.get(t["id"], "")
        out.append({"k": "o", "id": t["id"], "label": t["label"], "sub": "", "s": st,
                    "why": wm.get(t["id"], "") if st == "m" else ""})
    return out

def _hol_active_tasks(h: dict) -> list:
    return [t for t in h["tasks"] if not t["del"]]

def _hol_set_status(d: str, tid: str, st: str, why: str = ""):
    """st: "d" | "m" | "" (saaf). -> (ok, msg)"""
    with _HOL_LOCK:
        h = _hol_read()
        if d not in h["days"]:
            return False, "ye din holiday nahi hai"
        if not any(t["id"] == tid for t in h["tasks"]):
            return False, "kaam nahi mila"
        dm, wm = h["done"].setdefault(d, {}), h["why"].setdefault(d, {})
        if st in ("d", "m"):
            dm[tid] = st
        else:
            dm.pop(tid, None)
        if st == "m" and why:
            wm[tid] = str(why)[:300]
        else:
            wm.pop(tid, None)
        return (True, "ok") if _hol_write(h) else (False, "save nahi ho paya")

def _hol_cb_mark(iid: str, d: str, st: str) -> str:
    h = _hol_read()
    it = next((i for i in _hol_items(d, h) if i["id"] == iid), None)
    if not it:
        return "⚠️ Ye holiday kaam ab nahi mila."
    if it["s"] == st:
        return f"{_TGX_ICON[st]} pehle se: {it['label'][:60]}"
    ok, msg = _hol_set_status(d, iid, st, "" if st == "d" else "Telegram button (wajah nahi di)")
    return f"{_TGX_ICON[st]} {it['label'][:60]}" if ok else f"⚠️ {msg}"

def _hol_new_id() -> str:
    return "o" + os.urandom(5).hex()

def _hol_add_task(label: str):
    """-> (ok, msg, task|None)"""
    label = _tg_one(label, 80)
    if not label:
        return False, "kaam ka naam khaali hai", None
    with _HOL_LOCK:
        h = _hol_read()
        act = _hol_active_tasks(h)
        if len(act) >= _HOL_MAX_TASKS:
            return False, f"holiday kaam {_HOL_MAX_TASKS} se zyada nahi ho sakte", None
        if any(_tg_norm(t["label"]) == _tg_norm(label) for t in act):
            return False, "ye kaam pehle se list me hai", None
        t = {"id": _hol_new_id(), "label": label, "created": int(time.time() * 1000), "del": 0}
        h["tasks"].append(t)
        return (True, "ok", t) if _hol_write(h) else (False, "save nahi ho paya", None)

def _hol_del_task(tid: str):
    with _HOL_LOCK:
        h = _hol_read()
        t = next((x for x in h["tasks"] if x["id"] == tid and not x["del"]), None)
        if not t:
            return False, "kaam nahi mila", None
        t["del"] = int(time.time() * 1000)     # soft delete: purane dino ki report me wo kaam bana rahe
        return (True, "ok", t) if _hol_write(h) else (False, "save nahi ho paya", None)

def _hol_set_day(d: str, on: bool):
    """-> (ok, msg, changed)"""
    if not _tg_is_iso(d):
        return False, "date galat hai", False
    today = _tg_today()
    if d < today:
        return False, "beeti date par holiday set/hata nahi sakte", False
    if (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(today)).days > 366:
        return False, "date bahut door hai (1 saal ke andar rakho)", False
    with _HOL_LOCK:
        h = _hol_read()
        had = d in h["days"]
        if on:
            if not had:
                h["days"][d] = {"t": int(time.time() * 1000)}
        else:
            h["days"].pop(d, None)
            h["done"].pop(d, None)
            h["why"].pop(d, None)
        changed = (had != on)
        return (True, "ok", changed) if (not changed or _hol_write(h)) else (False, "save nahi ho paya", False)

def _hol_label(d: str, today: str) -> str:
    n = _tg_days_between(today, d)
    dd = datetime.date.fromisoformat(d)
    base = f"{dd.day} {_TG_MON_SHORT[dd.month - 1]}"
    return ("aaj" if n == 0 else "kal" if n == 1 else "parso" if n == 2 else base) + (f" ({base})" if n in (0, 1, 2) else "")

def _hol_plan_text(d: str) -> str:
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    _subj, text = _plan_build(b, d)
    return text or ""

def _hol_payload() -> dict:
    h, today = _hol_read(), _tg_today()
    return {"ok": True, "today": today,
            "tasks": [{"id": t["id"], "label": t["label"]} for t in _hol_active_tasks(h)],
            "days": sorted(d for d in h["days"] if d >= today),
            "today_items": [{"id": i["id"], "label": i["label"], "s": i["s"]} for i in _hol_items(today, h)]}

@_ws_action("holiday_get")
def _wsa_holiday_get(data):
    return _hol_payload()

@_ws_action("holiday_op")
def _wsa_holiday_op(data):
    op = str((data or {}).get("op", ""))
    today = _tg_today()
    if op == "add":
        ok, msg, _t = _hol_add_task(str(data.get("label", "")))
    elif op == "del":
        ok, msg, _t = _hol_del_task(str(data.get("id", "")))
    elif op == "day":
        d, on = str(data.get("date", "")), bool(data.get("on"))
        ok, msg, changed = _hol_set_day(d, on)
        if ok and on and changed and d == today:      # aaj holiday jodne par updated plan turant Telegram par
            try:
                txt = _hol_plan_text(today)
                if txt:
                    _alert_via_telegram("🏖 Aaj holiday set — updated list:\n\n" + txt)
            except Exception as e:
                _slog_exception("holiday push plan", e)
    elif op == "mark":
        st = str(data.get("s", ""))
        if st not in ("d", "m", ""):
            return {"ok": False, "msg": "status galat"}
        ok, msg = _hol_set_status(str(data.get("date", "")), str(data.get("id", "")), st, str(data.get("why", "")))
    else:
        return {"ok": False, "msg": "op samajh nahi aaya"}
    if not ok:
        return {"ok": False, "msg": msg}
    return _hol_payload()

# ── Telegram: `aaj holiday hai` / `kal holiday hai` / `holiday 12/10` / `holiday nahi aaj` / `holiday` (list) / `holiday add <kaam>` ──
_HOL_NEG = {"nahi", "nhi", "cancel", "cancelled", "hatao", "hata", "remove", "band", "off", "galat", "no"}

def _hol_dates(txt: str, today: str) -> list:
    t = _tg_norm(txt)
    t = re.sub(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?",
               lambda m: f"{m.group(1)}-{m.group(2)}" + ((("-" + (m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)))) if m.group(3) else ""), t)
    base = datetime.date.fromisoformat(today)
    out = []
    for m in re.finditer(r"\b(aaj|today|kal|tomorrow|parso|parson)\b|(\d{1,2}-\d{1,2}(?:-\d{4})?)|(\d{1,2} [a-z]{3,9}(?: \d{4})?)", t):
        w = m.group(1)
        if w:
            iso = (base + datetime.timedelta(days={"aaj": 0, "today": 0, "kal": 1, "tomorrow": 1, "parso": 2, "parson": 2}[w])).isoformat()
        else:
            iso = _tg_parse_date(m.group(2) or m.group(3), today)
        if iso and iso not in out:
            out.append(iso)
    if len(out) == 2 and re.search(r"\b(se|to|till|tak)\b", t):      # "12/10 se 14/10" -> range
        a, z = sorted(out)
        n = (datetime.date.fromisoformat(z) - datetime.date.fromisoformat(a)).days
        if 0 < n <= 30:
            out = [(datetime.date.fromisoformat(a) + datetime.timedelta(days=i)).isoformat() for i in range(n + 1)]
    return out[:31]

def _hol_list_text(today: str) -> str:
    h = _hol_read()
    days = sorted(d for d in h["days"] if d >= today)
    tasks = _hol_active_tasks(h)
    L = ["🏖 Holiday"]
    L.append("Aane wale holiday: " + (", ".join(_hol_label(d, today) for d in days[:10]) if days else "koi set nahi"))
    L.append("")
    if tasks:
        L.append(f"Holiday kaam ({len(tasks)}):")
        L += [f"{i}. {t['label']}" for i, t in enumerate(tasks, 1)]
    else:
        L.append("Holiday kaam abhi koi nahi.")
    L += ["", "• `aaj holiday hai` / `kal holiday hai` / `holiday 12/10`", "• `aaj holiday nahi` — hatane ke liye",
          "• `holiday add <kaam>` · `holiday del <number>`"]
    return "\n".join(L)

def _hol_h_cmd(ctx):
    today = ctx["today"]
    t = _tg_normalize_cmd(ctx["text"])
    m = re.match(r"(?i)^(?:holiday|holidays|chutti)\s+(?:add|jodo|\+)\s+(.+)$", t)
    if m:
        ok, msg, tk = _hol_add_task(m.group(1))
        if not ok:
            return f"⚠️ {msg}"
        n = len(_hol_active_tasks(_hol_read()))
        return f"✅ Holiday kaam joda: {tk['label']}\nAb total {n} holiday kaam."
    m = re.match(r"(?i)^(?:holiday|holidays|chutti)\s+(?:del|delete|remove)\s+(.+)$", t)
    if m:
        arg, act = m.group(1).strip(), _hol_active_tasks(_hol_read())
        tk = None
        if arg.isdigit() and 1 <= int(arg) <= len(act):
            tk = act[int(arg) - 1]
        else:
            tk = next((x for x in act if _tg_norm(x["label"]) == _tg_norm(arg)), None)
        if not tk:
            return "⚠️ Kaam nahi mila. `holiday` likho (numbered list), phir `holiday del <number>`."
        ok, msg, _x = _hol_del_task(tk["id"])
        return f"🗑 Holiday kaam hataya: {tk['label']}" if ok else f"⚠️ {msg}"
    dates = _hol_dates(t, today)
    neg = bool(_HOL_NEG & set(_tg_norm(t).split()))
    if not dates:
        if neg:
            return "Kaun si date? Jaise `aaj holiday nahi` ya `holiday nahi 12/10`."
        return _hol_list_text(today)
    if neg:
        gone, past = [], []
        for d in dates:
            ok, msg, ch = _hol_set_day(d, False)
            if ok and ch:
                gone.append(d)
            elif not ok:
                past.append(d)
        if gone:
            return "✅ Holiday hataya: " + ", ".join(_hol_label(d, today) for d in gone) + "\nIs din ki list me ab holiday kaam nahi aayenge."
        return "Is date par holiday set hi nahi tha." if not past else "⚠️ Beeti date par kuch nahi badal sakta."
    bad = [d for d in dates if d < today]
    dates = [d for d in dates if d >= today]
    if not dates:
        return "⚠️ Beeti date par holiday set nahi hota. Aaj ya aage ki date likho."
    new, already = [], []
    for d in dates:
        ok, msg, ch = _hol_set_day(d, True)
        if not ok:
            return f"⚠️ {msg}"
        (new if ch else already).append(d)
    tasks = _hol_active_tasks(_hol_read())
    L = ["🏖 Holiday set: " + ", ".join(_hol_label(d, today) for d in dates)]
    if already and not new:
        L[0] = "🏖 Pehle se holiday set hai: " + ", ".join(_hol_label(d, today) for d in already)
    if not tasks:
        L.append("⚠️ Holiday kaam ki list khaali hai — app me ⏰ → 🏖 Holiday tab me ya `holiday add <kaam>` se kaam jodo.")
    elif today in dates:
        txt = _hol_plan_text(today)
        L.append(f"Aaj ki updated list ({len(tasks)} holiday kaam judte hain):\n\n{txt}" if txt else "")
    else:
        d0 = dates[0]
        L.append(f"{_hol_label(d0, today)} subah 6 AM ki list me ye {len(tasks)} holiday kaam judenge, aur raat ki ✅/❌ checklist aur report me bhi:")
        L += [f"• {t['label']}" for t in tasks[:25]]
    if bad:
        L.append("(Beeti dates chhod di.)")
    return "\n".join(x for x in L if x is not None)

_TG_COMMANDS = (
    (("aaj", "holiday"), _hol_h_cmd), (("kal", "holiday"), _hol_h_cmd), (("parso", "holiday"), _hol_h_cmd),
    (("today", "holiday"), _hol_h_cmd), (("tomorrow", "holiday"), _hol_h_cmd),
    (("aaj", "chutti"), _hol_h_cmd), (("kal", "chutti"), _hol_h_cmd), (("parso", "chutti"), _hol_h_cmd),
    (("holiday",), _hol_h_cmd), (("holidays",), _hol_h_cmd), (("chutti",), _hol_h_cmd),
) + tuple(_TG_COMMANDS)
# ═══ HOLIDAY END ═══


def _report_due(b: dict, today: datetime.date) -> list:
    """Aaj kis step ki kaun si report banni hai: [(kind, start, end, step)]. Pehle weekly, phir monthly (monthly weekly ko dekhti hai)."""
    wk, mo = [], []
    for st in _practice_steps(b):
        k = (today - _prac_start(st)).days
        if k >= 6 and k % 7 == 6:
            wk.append(("week", today - datetime.timedelta(days=6), today, st))
        if k >= 27 and k % 28 == 27:
            mo.append(("month", today - datetime.timedelta(days=27), today, st))
    return wk + mo

def _report_run(kind: str, start: datetime.date, end: datetime.date, step: dict):
    """Report banao (pehle se bani ho to wahi frozen wali), bhejo. (status, msg): ok | skip | err."""
    rid = ("w:" if kind == "week" else "m:") + str(step.get("id")) + ":" + end.isoformat()
    with _REPORT_LOCK:
        lst = _reports_load()
        rec = next((r for r in lst if r.get("id") == rid), None)
        if rec is None:
            with _ROUTINE_LOCK:
                b = _routine_read_disk() or _routine_blank()
            rec = _dip_report_build(b, kind, start, end, lst) if step.get("dip") else _report_build(b, kind, start, end, lst, step)   # DIPANSHU-STEP-3 (2026-10-03)
            lst.append(rec)
            # DIPANSHU-STEP-3 (2026-10-03): pruning ab har (type, step) ke hisaab se — Practice aur Dipanshu ek dusre ke records nahi mitate
            for t_, keep in _REPORT_KEEP.items():
                for sid_ in {r.get("step") for r in lst if r.get("type") == t_}:
                    same = sorted([r for r in lst if r.get("type") == t_ and r.get("step") == sid_], key=lambda r: r.get("end", ""))
                    drop = {id(r) for r in same[:-keep]} if len(same) > keep else set()
                    lst = [r for r in lst if id(r) not in drop]
            _reports_save(lst)
        if rec.get("sent"):
            return "skip", "already sent"
        res = _alert_email_and_telegram(rec["subject"], rec["text"])
        if res == "ok":
            rec["sent"] = time.time()
            _reports_save(lst)
            return "ok", "report gayi"
        return "err", str(res)

def _report_loop():
    while True:
        sleep_for = 1800
        try:
            now = _ist_now()
            target = now.replace(hour=_REPORT_HOUR, minute=_REPORT_MIN, second=0, microsecond=0)
            age = (now - target).total_seconds()
            if age < 0:
                sleep_for = min(1800, max(5.0, -age + 1))
            elif age <= _REPORT_GRACE_SEC:
                with _ROUTINE_LOCK:
                    b = _routine_read_disk() or _routine_blank()
                for kind, a, z, st in _report_due(b, now.date()):
                    st_, msg = _report_run(kind, a, z, st)
                    _slog(f"[report] {kind} {st.get('label')} {z} -> {st_}: {msg}", level="ok" if st_ != "err" else "err")
                    if st_ == "err":
                        sleep_for = min(sleep_for, _REMINDER_RETRY_SEC)
                # DIPANSHU-STEP-3 (2026-10-03)
                for kind, a, z in _dip_due(b, now.date()):
                    st_, msg = _report_run(kind, a, z, {"id": "dipanshu", "label": "👦 Dipanshu", "dip": True})
                    _slog(f"[report] {kind} Dipanshu {z} -> {st_}: {msg}", level="ok" if st_ != "err" else "err")
                    if st_ == "err":
                        sleep_for = min(sleep_for, _REMINDER_RETRY_SEC)
                # DIPANSHU-STEP-4 (2026-10-03)
                for kind, a, z, st in _main_due(b, now.date()):
                    st_, msg = _report_run(kind, a, z, st)
                    _slog(f"[report] {kind} Main {z} -> {st_}: {msg}", level="ok" if st_ != "err" else "err")
                    if st_ == "err":
                        sleep_for = min(sleep_for, _REMINDER_RETRY_SEC)
        except Exception as e:
            _slog_exception("_report_loop", e)
            sleep_for = 60
        time.sleep(sleep_for)

def _ensure_report_thread():
    if "ReportSender" not in {t.name for t in threading.enumerate()}:
        threading.Thread(target=_report_loop, name="ReportSender", daemon=True).start()

_ensure_report_thread()

@_ws_action("reports_list")
def _wsa_reports_list(data):
    """📊 Reports tab — saved (frozen) reports, naye pehle. Read-only."""
    lst = sorted(_reports_load(), key=lambda r: (r.get("end", ""), r.get("type", "")), reverse=True)
    return {"ok": True, "items": lst[:120]}   # DIPANSHU-STEP-3 (2026-10-03): 60 -> 120

@_ws_action("report_preview")
def _wsa_report_preview(data):
    """📊 Preview — chal rahe hafte/28-din cycle ka abhi tak ka hisaab (save/send NAHI hota). data: {kind: week|month, step: id}."""
    d = data if isinstance(data, dict) else {}
    kind = "month" if d.get("kind") == "month" else "week"
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
    if d.get("step") == "dipanshu":   # DIPANSHU-STEP-3 (2026-10-03)
        s0 = _dip_start(b)
        if s0 is None:
            return {"ok": False, "msg": "pehle Dipanshu tab me ek aadat jodo"}
        today_ = _ist_now().date()
        if today_ < s0:
            return {"ok": False, "msg": f"Dipanshu report {s0.day}/{s0.month} se start hogi — abhi report nahi"}
        k_ = (today_ - s0).days
        size_ = 28 if kind == "month" else 7
        st_ = s0 + datetime.timedelta(days=size_ * (k_ // size_))
        return {"ok": True, "item": _dip_report_build(b, kind, st_, st_ + datetime.timedelta(days=size_ - 1), _reports_load(), preview=True)}
    if d.get("step") == "main":   # MAIN-SUNDAY (2026-10-04): Practice se alag, Somvar–Sunday hafta, har 4vein Sunday monthly
        today_ = _ist_now().date()
        first, wsun, n_ = _main_cycle(b, today_)
        if n_ < 0:
            return {"ok": False, "msg": f"Routine report {first.day}/{first.month} (Sunday) se start hogi — abhi report nahi"}
        if kind == "month":
            end_ = first + datetime.timedelta(days=7 * (4 * (n_ // 4) + 3))
            a_ = end_ - datetime.timedelta(days=27)
        else:
            end_ = wsun
            a_ = wsun - datetime.timedelta(days=6)
        return {"ok": True, "item": _report_build(b, kind, a_, end_, _reports_load(), dict(_MAIN_STEP), preview=True)}
    steps = _practice_steps(b)
    st = next((x for x in steps if str(x.get("id")) == str(d.get("step"))), None) or (steps[0] if steps else None)
    if st is None:
        return {"ok": False, "msg": "pehle Practice tab me ek step banao"}
    today = _ist_now().date()
    S = _prac_start(st)
    if today < S:
        return {"ok": False, "msg": f"step {S.day}/{S.month} se start hoga — abhi report nahi"}
    k = (today - S).days
    size = 28 if kind == "month" else 7
    start = S + datetime.timedelta(days=size * (k // size))
    end = start + datetime.timedelta(days=size - 1)
    return {"ok": True, "item": _report_build(b, kind, start, end, _reports_load(), st, preview=True)}

# ── Jawab page (GET) + submit (POST) — side-server routes ──
_ROUTINE_PAGE = """<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<title>Aaj ka hisaab</title><style>
*{box-sizing:border-box}body{margin:0;background:#131722;color:#d1d4dc;font:15px/1.4 -apple-system,Segoe UI,Roboto,Arial,sans-serif;padding:14px;max-width:560px;margin:auto}
h1{font-size:18px;margin:4px 0}.sub{color:#787b86;font-size:12px;margin-bottom:10px}
h2{font-size:14px;margin:18px 0 6px;color:#fff}.it{background:#1e222d;border:1px solid #2a2e39;border-radius:8px;padding:9px 10px;margin:6px 0}
.it.err{border-color:#ef5350}.lb{font-weight:600}.sb{color:#787b86;font-weight:400;font-size:12px;margin-left:6px}
.btns{display:flex;gap:8px;margin-top:7px}.btns button{flex:1;padding:9px 4px;border-radius:7px;border:1px solid #363a45;background:#2a2e39;color:#d1d4dc;font-size:14px}
.btns .ok.on{background:#26a69a;border-color:#26a69a;color:#fff}.btns .no.on{background:#ef5350;border-color:#ef5350;color:#fff}
input.n{width:100%;margin-top:7px;padding:9px;background:#131722;color:#d1d4dc;border:1px solid #363a45;border-radius:7px;font:15px inherit}
textarea{display:none;width:100%;margin-top:7px;padding:8px;background:#131722;color:#d1d4dc;border:1px solid #363a45;border-radius:7px;font:14px inherit;min-height:56px}
#send{width:100%;margin-top:18px;padding:13px;border-radius:8px;border:0;background:#2962ff;color:#fff;font-size:16px;font-weight:700}
#send:disabled{opacity:.5}#msg{margin:12px 0;font-size:14px}#msg.ok{color:#26a69a}#msg.bad{color:#ef9a9a}
</style></head><body><h1>⏰ Aaj ka hisaab</h1><div class="sub">__SUB__</div>__BODY__
<button id="send">Bhejo</button><div id="msg"></div><div id="line" style="display:none;margin:6px 0 20px;padding:12px;border-radius:8px;background:#1e222d;font-size:15px;line-height:1.5"></div>
<script>
var D=__D__,TOK=__TOK__;
function msg(t,c){var m=document.getElementById('msg');m.textContent=t;m.className=c||'';}
function key(it){var n=it.querySelector('.n');return (it.dataset.s||'')+'|'+it.querySelector('.why').value.trim()+'|'+(n?n.value.trim():'');}
document.querySelectorAll('.it').forEach(function(it){
  var ok=it.querySelector('.ok'),no=it.querySelector('.no'),why=it.querySelector('.why'),pn=it.querySelector('.n');
  function set(s,focus){it.dataset.s=s;if(ok){ok.classList.toggle('on',s==='d');no.classList.toggle('on',s==='m');}
    why.style.display=s==='m'?'block':'none';if(s==='m'&&focus)why.focus();}
  if(pn){pn.oninput=function(){var v=pn.value.trim();set(v===''?'':(parseFloat(v)>0?'d':'m'),false);};}
  else{ok.onclick=function(){set(it.dataset.s==='d'?'':'d');};
  no.onclick=function(){set(it.dataset.s==='m'?'':'m',true);};}
  set(it.dataset.s||'');it.dataset.init=key(it);
});
document.getElementById('send').onclick=function(){
  var items=[],bad=null,btn=this;
  document.querySelectorAll('.it').forEach(function(it){
    it.classList.remove('err');var s=it.dataset.s||'';if(!s||key(it)===it.dataset.init)return;
    var w=it.querySelector('.why').value.trim();
    if(s==='m'&&!w){it.classList.add('err');bad=bad||it;return;}
    var nn=it.querySelector('.n');
    items.push({k:it.dataset.k,id:it.dataset.id,s:s,why:w,n:nn?nn.value.trim():''});
  });
  if(bad){msg('❌ wale kaam ka reason likhna zaroori hai.','bad');bad.scrollIntoView({block:'center'});return;}
  if(!items.length){msg('Koi naya badlav nahi hai.','bad');return;}
  btn.disabled=true;msg('Bhej raha hoon…');
  fetch(location.pathname,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({d:D,tok:TOK,items:items})})
   .then(function(r){return r.json();}).then(function(j){
     if(j&&j.ok){msg('✅ Save ho gaya ('+j.n+' kaam). App me update dikhega.','ok');
       var ln=document.getElementById('line');if(j.line){ln.textContent=j.line;ln.style.display='block';ln.style.borderLeft='4px solid '+(j.res==='ok'?'#26a69a':'#ef5350');}else{ln.style.display='none';}
       document.querySelectorAll('.it').forEach(function(it){it.dataset.init=key(it);});}
     else{msg('⚠️ '+((j&&j.msg)||'error'),'bad');}
     btn.disabled=false;})
   .catch(function(e){msg('⚠️ Network error: '+e,'bad');btn.disabled=false;});
};
</script></body></html>"""

def _routine_page(d: str, tok: str):
    """(http_code, html) — jawab page."""
    import html as _hm
    if not _routine_tok_ok(d, tok):
        return 403, ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                     '<body style="font:16px sans-serif;padding:24px;background:#131722;color:#d1d4dc">'
                     '⚠️ Ye link galat ya expire ho gaya hai. Latest email ka link kholo.</body>')
    b = _routine_read_disk() or _routine_blank()
    items = _routine_collect(b, d, sunday=True, dip=True)   # DIPANSHU-STEP-1 (2026-10-03)
    dd = datetime.date.fromisoformat(d)
    parts = []
    for sec, title in _ROUTINE_SECS_EVE:
        its = [i for i in items if i["k"] == sec]
        if not its:
            continue
        parts.append(f"<h2>{_hm.escape(title)}</h2>")
        for i in its:
            sub = f'<span class="sb">{_hm.escape(i["sub"])}</span>' if i["sub"] else ""
            if i["k"] == "p":
                nv = "" if i.get("n") is None else _fmt_n(i["n"])
                parts.append(
                    f'<div class="it" data-k="p" data-id="{_hm.escape(i["id"], quote=True)}" data-s="{i["s"]}">'
                    f'<div class="lb">{_hm.escape(i["label"])}{sub}</div>'
                    f'<input class="n" type="number" inputmode="decimal" min="0" max="500" step="1" value="{nv}" placeholder="Aaj kitne sets kiye?">'
                    f'<textarea class="why" maxlength="300" placeholder="0 sets — kyon? (zaroori)">{_hm.escape(i["why"])}</textarea></div>')
                continue
            is_s = i["k"] == "s"   # 🗓 Sunday: ❌ = Skip (check band ho jata hai, jaisa app ke ⏭ Skip se)
            parts.append(
                f'<div class="it" data-k="{i["k"]}" data-id="{_hm.escape(i["id"], quote=True)}" data-s="{i["s"]}">'
                f'<div class="lb">{_hm.escape(i["label"])}{sub}</div>'
                '<div class="btns"><button type="button" class="ok">✅ Ho gaya</button>'
                f'<button type="button" class="no">{"⏭ Skip (nahi hua)" if is_s else "❌ Nahi hua"}</button></div>'
                f'<textarea class="why" maxlength="300" placeholder="{"Skip kyon? (zaroori)" if is_s else "Kyon nahi hua? (zaroori)"}">{_hm.escape(i["why"])}</textarea></div>')
    if not parts:
        parts.append('<div class="sub">Is din ke liye koi kaam nahi mila.</div>')
    wk = _ROUTINE_WEEKDAYS[(dd.weekday() + 1) % 7]
    page = (_ROUTINE_PAGE.replace("__SUB__", f"{dd.day}/{dd.month}/{dd.year} · {wk}")
            .replace("__BODY__", "".join(parts))
            .replace("__D__", json.dumps(d)).replace("__TOK__", json.dumps(str(tok))))
    return 200, page

def _routine_resp_post(raw: bytes):
    """(http_code, json_obj) — jawab-page ka submit. Token + known-item check; ❌ par reason zaroori."""
    try:
        j = json.loads((raw or b"{}").decode("utf-8"))
        d, tok, its = str(j.get("d", "")), str(j.get("tok", "")), j.get("items")
    except Exception:
        return 400, {"ok": False, "msg": "bad request"}
    if not _routine_tok_ok(d, tok):
        return 403, {"ok": False, "msg": "link galat ya expire ho gaya"}
    if not isinstance(its, list) or not its or len(its) > 400:
        return 400, {"ok": False, "msg": "items nahi mile"}
    with _ROUTINE_LOCK:
        b = _routine_read_disk() or _routine_blank()
        known = {(i["k"], i["id"]) for i in _routine_collect(b, d, sunday=True, dip=True)}   # DIPANSHU-STEP-1 (2026-10-03)
        now_ms, ents, hol_ents = int(time.time() * 1000), [], []
        for it in its:
            if not isinstance(it, dict):
                continue
            k, iid, s, why = str(it.get("k", "")), str(it.get("id", "")), str(it.get("s", "")), str(it.get("why", "")).strip()[:300]
            if (k, iid) not in known:
                continue
            if k == "p":
                try:
                    nv = float(str(it.get("n", "")).strip())
                except Exception:
                    continue
                if nv < 0 or nv > 500:
                    continue
                nv = int(nv) if nv == int(nv) else round(nv, 1)
                s = "d" if nv > 0 else "m"
                if s == "m" and not why:
                    return 400, {"ok": False, "msg": "0 sets ka reason zaroori hai"}
                ents.append({"k": "p", "id": iid, "d": d, "s": s, "n": nv, "why": why if s == "m" else "", "t": now_ms})
                continue
            if s not in ("d", "m"):
                continue
            if s == "m" and not why:
                return 400, {"ok": False, "msg": "❌ wale kaam ka reason zaroori hai"}
            if k == "o":                          # HOLIDAY: alag file me
                hol_ents.append((iid, s, why if s == "m" else ""))
                continue
            ents.append({"k": k, "id": iid, "d": d, "s": s, "why": why if s == "m" else "", "t": now_ms})
        if not ents and not hol_ents:
            return 400, {"ok": False, "msg": "koi valid item nahi"}
        for _hi, _hs, _hw in hol_ents:
            _hol_set_status(d, _hi, _hs, _hw)
        for e in ents:
            _routine_apply_entry(b, e)
            b["rlog"].append(e)
        b["t"] = now_ms
        ok, msg = _local_state_save_raw("routine", b)
    _slog(f"[routine-email] jawab {d}: {len(ents) + len(hol_ents)} items ok={ok}", level="ok" if ok else "err")
    if not ok:
        return 500, {"ok": False, "msg": msg}
    # (2026-10-07) Pehle yahan "tapori" random line aati thi (_tapori_pick pm_ok/pm_fail).
    # Ab band — sab jagah (Telegram + is response-page) wahi fixed Hindi line.
    res = _day_result(b, d)
    line = "आपका दिन शुभ हो 🙏" if res in ("ok", "fail") else None
    return 200, {"ok": True, "n": len(ents) + len(hol_ents), "line": line, "res": res}

# ── Heartbeat (2026-09-25) — Healthchecks.io ───────────────────────────────
# Maqsad: pata chale ki HF Space (free, 48h inactivity sleep) kab sota hai / thread
# marta hai. Ye thread har _HEARTBEAT_EVERY_SEC par HEARTBEAT_URL ko GET karta hai
# (HF Secret; na ho to kuch nahi karta). Ping SIRF tab jaati hai jab ReminderChecker
# thread zinda ho -> ping band = Space so gaya / process mara / reminder thread mara.
# Ye Space ko ping NAHI karta (bahar se koi request nahi), isliye 48h ka timer
# natural chalta hai. Alert Healthchecks.io khud email se bhejta hai.
_HEARTBEAT_EVERY_SEC = 60            # (2026-10-02) 300 se 60 sec — Healthchecks.io me Period 1 min + Grace 1-2 min rakho

def _heartbeat_loop():
    was_ok = None
    while True:
        try:
            url = _get_secret("HEARTBEAT_URL").strip()
            alive = any(t.name == "ReminderChecker" for t in threading.enumerate())
            if url and alive:
                try:
                    r = requests.get(url, timeout=10)
                    ok = r.status_code == 200
                except Exception:
                    ok = False
                if ok != was_ok:   # sirf state badalne par log, spam nahi
                    _slog(f"[heartbeat] ping {'ok' if ok else 'FAIL'}",
                          level="ok" if ok else "err")
                    was_ok = ok
        except Exception as e:
            _slog_exception("_heartbeat_loop", e)
        time.sleep(_HEARTBEAT_EVERY_SEC)

def _ensure_heartbeat_thread():
    names = {t.name for t in threading.enumerate()}
    if "HeartbeatPinger" not in names:
        threading.Thread(target=_heartbeat_loop, name="HeartbeatPinger", daemon=True).start()

_ensure_heartbeat_thread()

# ── Render keep-alive (2026-10-01) ─────────────────────────────────────────
# Render free services 15 min idle ke baad so jaati hain. Ye thread har _RENDER_PING_EVERY_SEC (15s) par
# neeche ke URLs ko GET karta hai taaki wo jaagti rahein. URL list HF Secret RENDER_PING_URLS (comma-separated)
# se aati hai; na ho to ye do default. Kisi URL se _RENDER_DOWN_AFTER_SEC (60s) tak koi successful jawab na aaye
# to email + Telegram alert (ek outage = ek alert, har URL ka alag); wapas theek hone par "wapas chal gaya" message.
_RENDER_PING_EVERY_SEC = 15           # ping har 15s (pichli ping hang ho to bhi — har URL ke liye alag worker)
_RENDER_DOWN_AFTER_SEC = 60           # (2026-10-02) itne sec se success nahi -> DOWN alert (purana "N fail" rule hata)
_RENDER_MAX_INFLIGHT   = 6            # ek URL ki itni pings ek saath hang ho sakti hain (thread cap)
_RENDER_PING_TIMEOUT   = 60          # sleeping Render service ko jaagne me ~50s lagte hain
_RENDER_PING_DEFAULT   = "https://telegram-aib3.onrender.com/health,https://replay-qda7.onrender.com/health"
_RENDER_PING_STATE: dict = {}        # {url: {fails, alerted, ok, last_check, ms, why, since, total, bad, last_ok, down_at, inflight, ...}} — login-page dashboard bhi yahi padhta hai

# ── Render ping record — HF /data (persistent) par (2026-10-01) ───────────────
# Har ping nahi likhte (roz ~2,900 rows hoti). Sirf:
#   render_ping_events.jsonl : DOWN (confirmed outage) / UP (outage kitni der chala) / SLOW (cold-start) /
#                              PINGER_START (har boot par — do boot ke beech ka gap isse dikhta hai). ~300KB cap.
#   render_ping_daily.json   : {IST-date: {url: {total, bad, ok_n, ms_sum, ms_max, slow, outages}}} — 30 din rakhta hai.
# Dashboard in dono ko FILE se padhta hai (memory se nahi), isliye restart ke baad bhi history dikhti hai.
_RP_EVENTS_FILE      = os.path.join(LOCAL_STATE_DIR, "render_ping_events.jsonl")
_RP_DAILY_FILE       = os.path.join(LOCAL_STATE_DIR, "render_ping_daily.json")
_RP_EVENTS_MAX_BYTES = 300 * 1024
_RP_EVENTS_KEEP_LINES = 1000
_RP_DAILY_KEEP_DAYS  = 30
_RP_SLOW_MS          = 10000          # isse zyada time lage (par HTTP 200) to SLOW / cold-start
_RP_FLUSH_EVERY_SEC  = 300            # daily summary disk par max itne gap se (event aane par turant)
_RP_LOCK             = threading.Lock()
_RP_DAILY: dict      = {}
_RP_DAILY_DIRTY      = False
_RP_LAST_FLUSH       = 0.0

def _rp_read_daily() -> dict:
    try:
        with open(_RP_DAILY_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def _rp_read_events(n: int = 8) -> list:
    """Aakhri n events (naye pehle). File na ho / kharab ho to []."""
    try:
        with open(_RP_EVENTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max(1, n):]
    except Exception:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out

def _rp_event(kind: str, url: str, **extra) -> None:
    try:
        row = {"t": round(time.time(), 1), "ist": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
               "kind": kind, "url": url, "pid": os.getpid()}
        row.update(extra)
        with _RP_LOCK:
            if os.path.exists(_RP_EVENTS_FILE) and os.path.getsize(_RP_EVENTS_FILE) > _RP_EVENTS_MAX_BYTES:
                with open(_RP_EVENTS_FILE, "r", encoding="utf-8") as f:
                    tail = f.readlines()[-_RP_EVENTS_KEEP_LINES:]
                with open(_RP_EVENTS_FILE, "w", encoding="utf-8") as f:
                    f.writelines(tail)
            with open(_RP_EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        _slog_exception("_rp_event", e)

def _rp_record(url: str, ok: bool, ms: int) -> dict:
    """Aaj (IST) ke bucket me ek ping jodo. Bucket return karta hai (slow/outages badhane ke liye)."""
    global _RP_DAILY_DIRTY
    day = _ist_now().strftime("%Y-%m-%d")
    b = _RP_DAILY.setdefault(day, {}).setdefault(
        url, {"total": 0, "bad": 0, "ok_n": 0, "ms_sum": 0, "ms_max": 0, "slow": 0, "outages": 0})
    b["total"] += 1
    if ok:
        b["ok_n"] += 1
        b["ms_sum"] += ms
        b["ms_max"] = max(b["ms_max"], ms)
    else:
        b["bad"] += 1
    _RP_DAILY_DIRTY = True
    return b

def _rp_flush_daily(force: bool = False) -> None:
    global _RP_DAILY_DIRTY, _RP_LAST_FLUSH
    if not _RP_DAILY_DIRTY:
        return
    if not force and time.time() - _RP_LAST_FLUSH < _RP_FLUSH_EVERY_SEC:
        return
    try:
        cutoff = (_ist_now() - datetime.timedelta(days=_RP_DAILY_KEEP_DAYS)).strftime("%Y-%m-%d")
        for d in [d for d in _RP_DAILY if d < cutoff]:
            del _RP_DAILY[d]
        tmp = _RP_DAILY_FILE + ".tmp"
        with _RP_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_RP_DAILY, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _RP_DAILY_FILE)
        _RP_DAILY_DIRTY = False
        _RP_LAST_FLUSH = time.time()
    except Exception as e:
        _slog_exception("_rp_flush_daily", e)

def _render_ping_urls() -> list:
    raw = _get_secret("RENDER_PING_URLS").strip() or _RENDER_PING_DEFAULT
    return [u.strip() for u in raw.split(",") if u.strip()]

# ── Render /health JSON inspector (2026-10-06) ─────────────────────────────────────────────────────
# Ping sirf status 200 dekhta tha. Ab 200 ke saath aaya JSON bhi padhta hai (sirf main.ts wala "my-engine-render" —
# jisme uptime_s ho; relay jaise dusre /health ko chhod deta hai) aur ye cases alert karta hai:
#   restart (uptime_s ghata, 60s se chhota restart bhi) | restart ke baad rules_live >0 se 0 (rules gayab) |
#   Binance IP-ban (ban_s>0) | rules_synced lagatar false (>=3 min) | HF store configured nahi (rules live hain) |
#   journal_queue >=50 | Render par Telegram configured nahi.
_RENDER_HEALTH_BODY: dict = {}      # {url: (ping_start_ts, json)}
_RENDER_HEALTH_STATE: dict = {}     # {url: {...}}
_RENDER_HEALTH_LOCK = threading.Lock()
_RH_SYNC_BAD_SEC   = 180
_RH_JOURNAL_MAX    = 50

def _render_health_inspect(url: str) -> None:
    item = _RENDER_HEALTH_BODY.get(url)
    if not item:
        return
    t_start, b = item
    if not isinstance(b, dict) or "uptime_s" not in b:
        return
    alerts = []
    with _RENDER_HEALTH_LOCK:
        st = _RENDER_HEALTH_STATE.setdefault(url, {"t": 0.0, "up": None, "rl": None, "ban": False,
                                                    "sync_bad": 0.0, "cool": {}})
        if t_start < st["t"]:                              # der se aaya purana jawab — naya state na bigade
            return
        st["t"] = t_start
        now = time.time()

        def _cool(key: str, sec: float) -> bool:           # True = abhi alert bhej sakte hain
            if now - st["cool"].get(key, 0.0) >= sec:
                st["cool"][key] = now
                return True
            return False

        up = b.get("uptime_s")
        rl = b.get("rules_live")
        bn_ = b.get("binance") or {}
        store = b.get("store") or {}

        # 1) restart (uptime_s pichli ping se kam) + 4) rules gayab
        if isinstance(up, (int, float)) and isinstance(st["up"], (int, float)) and up + 5 < st["up"]:
            alerts.append(("♻️ Render restart hua",
                           f"{url}\nuptime ab {int(up)}s (pehle {int(st['up'])}s tha). Rules live: {st['rl']} → {rl}.",
                           f"{url} RESTART"))
            if isinstance(st["rl"], int) and st["rl"] > 0 and rl == 0:
                alerts.append(("🚨 Render ke SL/Target rules GAYAB",
                               f"{url}\nRestart se pehle {st['rl']} live rule the, ab 0. Rules HF store se load nahi hue ya "
                               f"save nahi hue the. Open positions check karo, SL dobara lagao!",
                               f"{url} RULES_LOST"))
        st["up"] = up
        st["rl"] = rl if isinstance(rl, int) else st["rl"]

        # 2) Binance IP-ban
        ban_s = bn_.get("ban_s") or 0
        if ban_s > 0 and not st["ban"]:
            st["ban"] = True
            alerts.append(("⛔ Binance IP-BAN (Render)",
                           f"{url}\nban_s={ban_s}. Ban me SL/Target exit orders bhi nahi ja payenge — positions dekho.",
                           f"{url} BAN"))
        elif ban_s <= 0:
            st["ban"] = False

        # 3) rules HF store me sync nahi ho rahe
        if store.get("rules_synced") is False:
            if not st["sync_bad"]:
                st["sync_bad"] = now
            elif now - st["sync_bad"] >= _RH_SYNC_BAD_SEC and _cool("sync", 1800):
                alerts.append(("⚠️ Render rules HF store me save nahi ho rahe",
                               f"{url}\n{int(now - st['sync_bad'])}s se rules_synced=false. last_fail: {store.get('last_fail')}. "
                               f"Restart hua to rules chale jayenge.", f"{url} RULES_UNSYNCED"))
        else:
            st["sync_bad"] = 0.0
        if store.get("configured") is False and (rl or 0) > 0 and _cool("noconf", 6 * 3600):
            alerts.append(("⚠️ Render par HF store configured nahi",
                           f"{url}\n{rl} live rule sirf memory me hain — restart par chale jayenge. HF_STORE_URL / HF_STORE_TOKEN set karo.",
                           f"{url} STORE_OFF"))

        # 5) journal queue atka
        jq = store.get("journal_queue") or 0
        if jq >= _RH_JOURNAL_MAX and _cool("jq", 1800):
            alerts.append(("⚠️ Render journal queue atka",
                           f"{url}\njournal_queue={jq} (HF store tak trade journal nahi pahunch raha).", f"{url} JOURNAL_QUEUE"))

        # Render par Telegram configured nahi — trade alerts nahi aayenge
        if b.get("telegram") is False and _cool("tg", 6 * 3600):
            alerts.append(("⚠️ Render par Telegram configured nahi",
                           f"{url}\nTELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (ya RELAY_URL + RELAY_SECRET) set karo — warna order fail/rule exit ke Telegram alert nahi aayenge.",
                           f"{url} TG_OFF"))
    for a in alerts:
        _render_alert_async(*a)

def _render_ping_one(url: str) -> tuple:
    t0 = time.time()
    try:
        r = requests.get(url, timeout=_RENDER_PING_TIMEOUT)
        if r.status_code == 200:
            try:                                          # (2026-10-06) /health ka JSON — _render_health_inspect padhta hai
                _RENDER_HEALTH_BODY[url] = (t0, r.json())
            except Exception:
                _RENDER_HEALTH_BODY.pop(url, None)
        return url, r.status_code == 200, f"HTTP {r.status_code}", int((time.time() - t0) * 1000)
    except Exception as e:
        return url, False, f"{type(e).__name__}", int((time.time() - t0) * 1000)

def _rp_bucket(url: str) -> dict:
    """Aaj (IST) ka bucket (total/bad badhaye bina) — outage/slow count karne ke liye."""
    day = _ist_now().strftime("%Y-%m-%d")
    return _RP_DAILY.setdefault(day, {}).setdefault(
        url, {"total": 0, "bad": 0, "ok_n": 0, "ms_sum": 0, "ms_max": 0, "slow": 0, "outages": 0})

# ── Render pinger v2 (2026-10-02) — har URL independent, ping har 15s FIX ──────────────────────────
# * Scheduler (thread "RenderPinger") har _RENDER_PING_EVERY_SEC par HAR URL ke liye ek alag worker thread chhodta hai —
#   pichli ping hang/slow ho to bhi agli ping time par jati hai (wait nahi karta). Ek URL me max _RENDER_MAX_INFLIGHT ping ek saath.
# * DOWN rule (time-based): kisi URL par _RENDER_DOWN_AFTER_SEC (60s) se koi SUCCESSFUL (HTTP 200) jawab nahi aaya
#   -> usi URL ka ek DOWN alert (email + Telegram). Doosre URL ka state/alert bilkul alag.
#   Beech me ek-aadh ping fail ho par agar success aate rahe to alert nahi.
# * UP rule: DOWN alert ke baad pehla success -> usi URL ka "wapas chal gaya" alert (ek outage = ek DOWN + ek UP).
_RENDER_PING_LOCK = threading.Lock()

def _render_ping_state(url: str) -> dict:
    st = _RENDER_PING_STATE.get(url)
    if st is None:
        st = _RENDER_PING_STATE[url] = {"fails": 0, "alerted": False, "ok": None, "last_check": 0.0, "ms": 0, "why": "",
                                        "since": 0.0, "total": 0, "bad": 0,
                                        "last_ok": time.time(),      # boot/pehli baar dekhne se 60s ka grace
                                        "down_at": 0.0, "inflight": 0, "latest_start": 0.0, "last_fail_why": ""}
    return st

def _render_alert_async(subject: str, text: str, tag: str) -> None:
    """Alert alag thread me (email/Telegram dheema ho to ping scheduler/dusre URL ko na roke)."""
    def _run():
        try:
            res = _alert_email_and_telegram(subject, text)
            _slog(f"[render-ping] {tag} alert -> {res}", level="ok" if res == "ok" else "err")
        except Exception as e:
            _slog_exception("_render_alert_async", e)
    threading.Thread(target=_run, name="RenderAlertSend", daemon=True).start()

def _render_ping_result(url: str, t_start: float, ok: bool, why: str, ms: int) -> None:
    alert = None
    with _RENDER_PING_LOCK:
        st = _render_ping_state(url)
        now_t = time.time()
        st["total"] += 1
        if not ok:
            st["bad"] += 1
            st["fails"] += 1
            st["last_fail_why"] = why
        else:
            st["fails"] = 0
        _bk = _rp_record(url, ok, ms)
        if ok and ms >= _RP_SLOW_MS:
            _bk["slow"] += 1
            _rp_event("SLOW", url, ms=ms, why=why)
        if t_start >= st["latest_start"]:                 # purani (der se aayi) ping naye status ko na bigade
            if st["ok"] is None or st["ok"] != ok:
                st["since"] = now_t
            st.update(ok=ok, last_check=now_t, ms=ms, why=why, latest_start=t_start)
        if ok:
            st["last_ok"] = now_t
            if st["alerted"]:
                down_for = int(now_t - st["down_at"]) if st["down_at"] else None
                _rp_event("UP", url, ms=ms, down_for_sec=down_for)
                _rp_flush_daily(force=True)
                st["alerted"] = False
                alert = ("🛰️ Render wapas chal gaya (HF Space se)",
                         f"HF Space ke pinger ke hisaab se {url} ab theek chal raha hai."
                         + (f" (~{down_for // 60}m {down_for % 60}s down raha)" if down_for is not None else ""),
                         f"{url} UP")
    if alert:
        _render_alert_async(*alert)

def _render_ping_check_down(url: str) -> None:
    alert = None
    with _RENDER_PING_LOCK:
        st = _render_ping_state(url)
        now_t = time.time()
        silent = now_t - st["last_ok"]
        if silent >= _RENDER_DOWN_AFTER_SEC and not st["alerted"]:
            st["alerted"] = True
            st["down_at"] = st["last_ok"]
            why = st.get("last_fail_why") or "koi jawab nahi (timeout/hang)"
            if st["ok"] is not False:                      # dashboard bhi DOWN dikhaye
                st.update(ok=False, since=st["last_ok"], why=why)
            _rp_bucket(url)["outages"] += 1
            _rp_event("DOWN", url, why=why, silent_sec=int(silent),
                      since_ist=datetime.datetime.fromtimestamp(st["last_ok"], tz=IST).strftime("%Y-%m-%d %H:%M:%S"))
            _rp_flush_daily(force=True)
            alert = ("🛰️ Render DOWN (HF Space se)",
                     f"HF Space ke pinger ko {url} se pichle {int(silent)} sec se koi successful jawab nahi aaya ({why}).",
                     f"{url} DOWN")
            _slog(f"[render-ping] {url} DOWN — {int(silent)}s se success nahi ({why})", level="err")
    if alert:
        _render_alert_async(*alert)

def _render_ping_worker(url: str) -> None:
    t0 = time.time()
    try:
        _u, ok, why, ms = _render_ping_one(url)
    except Exception as e:
        ok, why, ms = False, type(e).__name__, int((time.time() - t0) * 1000)
    try:
        _render_ping_result(url, t0, ok, why, ms)
    except Exception as e:
        _slog_exception("_render_ping_worker", e)
    try:
        if ok:
            _render_health_inspect(url)
    except Exception as e:
        _slog_exception("_render_health_inspect", e)
    finally:
        with _RENDER_PING_LOCK:
            st = _RENDER_PING_STATE.get(url)
            if st:
                st["inflight"] = max(0, st["inflight"] - 1)

def _render_ping_loop():
    global _RP_DAILY
    try:
        _RP_DAILY = _rp_read_daily()          # restart ke baad purani history se aage badho
    except Exception:
        _RP_DAILY = {}
    _rp_event("PINGER_START", "-", persistent=str(LOCAL_STATE_DIR).startswith("/data"))
    next_ping = time.monotonic()
    while True:
        try:
            urls = _render_ping_urls()
            now_m = time.monotonic()
            if now_m >= next_ping:
                next_ping += _RENDER_PING_EVERY_SEC
                if next_ping < now_m:                 # bahut peeche reh gaye (Space freeze) to dubara sync
                    next_ping = now_m + _RENDER_PING_EVERY_SEC
                for url in urls:
                    with _RENDER_PING_LOCK:
                        st = _render_ping_state(url)
                        fire = st["inflight"] < _RENDER_MAX_INFLIGHT
                        if fire:
                            st["inflight"] += 1
                    if fire:
                        threading.Thread(target=_render_ping_worker, args=(url,), name="RenderPingWorker", daemon=True).start()
            for url in urls:                          # har URL ka DOWN-check alag (har ~1s)
                _render_ping_check_down(url)
            with _RENDER_PING_LOCK:
                _rp_flush_daily()
        except Exception as e:
            _slog_exception("_render_ping_loop", e)
        time.sleep(1)

def _ensure_render_ping_thread():
    if "RenderPinger" not in {t.name for t in threading.enumerate()}:
        threading.Thread(target=_render_ping_loop, name="RenderPinger", daemon=True).start()

_ensure_render_ping_thread()

# ── Thread watcher (2026-09-25) ────────────────────────────────────────────
# Maqsad: pata chale KAUNSA background thread KAB band/shuru hua, aur poora process
# (Space sleep/restart) kab gaya. Sirf dekhta aur likhta hai — koi thread chhedta nahi.
#   * har _TW_EVERY_SEC par threading.enumerate() se named threads ka set dekhta hai
#   * pehle zinda tha, ab nahi  -> "DEAD";  naya/wapas aaya -> "STARTED"
#   * thread_watch_last.json me har cycle "main abhi zinda hu" timestamp likhta hai.
#     Naye boot par purana timestamp padh ke gap batata hai (gap bada = process band tha:
#     sleep/restart/rebuild) — thread mar jaane se ye alag pehchana jaata hai.
#   * log: LOCAL_STATE_DIR/thread_watch.log (disk, restart ke baad bhi rehta hai, ~300KB cap)
#     + stdout (HF Logs tab) + _slog (app ka startup panel).
# NOTE: raat ko / market band hone par Fyers WS jaise threads ka band hona normal ho sakta hai.
# Yahan sirf "zinda hai ya nahi" dekha jaata hai, "data aa raha hai ya nahi" nahi.
_TW_EVERY_SEC = 60
_TW_LOG_FILE  = os.path.join(LOCAL_STATE_DIR, "thread_watch.log")
_TW_LAST_FILE = os.path.join(LOCAL_STATE_DIR, "thread_watch_last.json")
_TW_LOG_MAX_BYTES = 300 * 1024
_TW_TRACKED = (
    "ReminderChecker", "HeartbeatPinger", "RenderPinger", "EkadashiNotify",
    "FyersWS", "FyersRESTPoller", "FyersTokenMonitor", "FinnhubLiveWS",
    "OptionChainBG",
    "BNHistoryAPI", "WSHub",
)

def _tw_log(msg: str, level: str = "info") -> None:
    ts = _ist_now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} IST | pid={os.getpid()} | {msg}"
    try:
        print(f"[thread-watch] {line}", flush=True)
    except Exception:
        pass
    try:
        _slog(f"[thread-watch] {msg}", level=level)
    except Exception:
        pass
    try:
        if os.path.exists(_TW_LOG_FILE) and os.path.getsize(_TW_LOG_FILE) > _TW_LOG_MAX_BYTES:
            with open(_TW_LOG_FILE, "r", encoding="utf-8") as f:
                tail = f.readlines()[-1500:]
            with open(_TW_LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(_TW_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def _tw_read_last() -> dict:
    try:
        with open(_TW_LAST_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _tw_write_last(alive: list) -> None:
    tmp = _TW_LAST_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "pid": os.getpid(), "alive": alive}, f)
        os.replace(tmp, _TW_LAST_FILE)
    except Exception:
        pass

def _thread_watch_loop():
    prev_boot = _tw_read_last()
    now = time.time()
    if prev_boot.get("ts"):
        gap_min = (now - float(prev_boot["ts"])) / 60.0
        last_seen = datetime.datetime.fromtimestamp(float(prev_boot["ts"]), IST).strftime("%Y-%m-%d %H:%M:%S")
        if prev_boot.get("pid") == os.getpid():
            _tw_log(f"watcher restart (same process); last seen {last_seen} IST")
        elif gap_min > (_TW_EVERY_SEC * 3) / 60.0:
            _tw_log(f"BOOT: naya process. Pichhla process aakhri baar {last_seen} IST par zinda tha "
                    f"(gap ~{gap_min:.0f} min) -> Space sleep/restart/rebuild hua tha", level="warn")
        else:
            _tw_log(f"BOOT: naya process, pichhla {last_seen} IST (gap ~{gap_min:.1f} min, chhota restart)")
    else:
        _tw_log("BOOT: pehla record (koi purana timestamp nahi)")
    prev = set()
    first = True
    while True:
        try:
            names = {t.name for t in threading.enumerate()}
            cur = {n for n in _TW_TRACKED if n in names}
            if first:
                _tw_log("alive at boot: " + (", ".join(sorted(cur)) or "(koi tracked thread nahi)"))
                first = False
            else:
                for n in sorted(prev - cur):
                    _tw_log(f"DEAD: {n} (pehle zinda tha, ab nahi mila)", level="warn")
                for n in sorted(cur - prev):
                    _tw_log(f"STARTED: {n}")
            prev = cur
            _tw_write_last(sorted(cur))
        except Exception as e:
            _slog_exception("_thread_watch_loop", e)
        time.sleep(_TW_EVERY_SEC)

def _ensure_thread_watch():
    names = {t.name for t in threading.enumerate()}
    if "ThreadWatcher" not in names:
        threading.Thread(target=_thread_watch_loop, name="ThreadWatcher", daemon=True).start()

_ensure_thread_watch()


# ─── SERVER-SIDE-ONLY storage diagnostic ────────────────────────────────────
# Bilkul chart.html (client/JS) se independent — sirf app.py ke andar os/json
# se check karta hai ki (a) HF ka persistent volume /data actually mounted
# hai ya nahi, (b) usmein likhne/padhne ki permission hai ya nahi (asli
# write+read+delete round-trip test — sirf os.path.isdir se satisfy nahi
# hote, mount "dikh" sakta hai par read-only bhi ho sakta hai), (c)
# LOCAL_STATE_DIR abhi kaunsa path use kar raha hai (persistent /data wala
# ya ephemeral fallback), aur (d) teeno state files (state/settings/layout)
# disk par actually maujood hain ya nahi, unka size aur last-modified time.
# Agar ye sab green aaye phir bhi data purana/missing lage, to bug pakka
# client-side (chart.html bridge/save-trigger) mein hai, server-side nahi.
def _storage_diagnostic() -> dict:
    info: dict = {
        "data_mount_exists":      os.path.isdir("/data"),
        "data_mount_writable":    os.access("/data", os.W_OK) if os.path.isdir("/data") else False,
        "local_state_dir":        LOCAL_STATE_DIR,
        "local_state_dir_exists": os.path.isdir(LOCAL_STATE_DIR),
        "is_persistent_path":     LOCAL_STATE_DIR.startswith("/data"),
        "write_read_delete_ok":   False,
        "write_read_delete_error": None,
        "files": {},
    }
    # Real round-trip test — sirf folder "exist" karna kaafi nahi, likh-
    # padh-delete karke confirm karte hain.
    _probe_path = os.path.join(LOCAL_STATE_DIR, "_diag_probe.json")
    try:
        _probe_val = {"ts": time.time()}
        with open(_probe_path, "w", encoding="utf-8") as f:
            json.dump(_probe_val, f)
        with open(_probe_path, "r", encoding="utf-8") as f:
            _read_back = json.load(f)
        os.remove(_probe_path)
        info["write_read_delete_ok"] = (_read_back == _probe_val)
    except Exception as e:
        info["write_read_delete_error"] = str(e)

    for _kind in _LOCAL_STATE_KINDS:
        _p = _local_state_file(_kind)
        if os.path.exists(_p):
            try:
                _st = os.stat(_p)
                info["files"][_kind] = {
                    "exists": True,
                    "size_bytes": _st.st_size,
                    "modified": datetime.datetime.fromtimestamp(_st.st_mtime, IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                }
            except Exception as e:
                info["files"][_kind] = {"exists": True, "stat_error": str(e)}
        else:
            info["files"][_kind] = {"exists": False}
    return info

# ─── Storage Manager — poore /data mount ko list/delete karne ke liye
# (login page ke "Storage Manager" card se). _storage_diagnostic() sirf
# health-check tha; ye do functions actual file-management karte hain:
# (1) _storage_list_all_files — /data ke andar SAB kuch recursively list
#     karta hai (sirf state/settings/layout nahi — koi stray/test file bhi
#     dikh jaayegi, jaise "Test File Likho" button se banayi CLAUDE_TEST_
#     FILE.txt), har entry ka size + last-modified ke saath.
# (2) _storage_delete_files — chuni hui specific files delete karta hai
#     (path-traversal se bachne ke liye root ke bahar koi path resolve
#     nahi hone dete).
# (2026-10-01) (3) _storage_wipe_all — PERMANENTLY HATA DIYA (Wipe All button + function).
def _storage_root() -> str:
    """Jise list/delete karna hai — HF ka /data mount agar maujood hai,
    warna LOCAL_STATE_DIR ka parent folder (local dev fallback)."""
    return "/data" if os.path.isdir("/data") else os.path.dirname(LOCAL_STATE_DIR)

# ── SV2/SV3 dedicated disk-cache folders ───
# Har view ka apna alag folder hai — SV1 (`replay_cache`, replay_symbols.py
# mein already hai) se aur ek-doosre se bhi kabhi mix nahi honge:
#   /data/sv2_cache/                         → BankNifty + BTC gz (SV2)
#   /data/sv3_cache/_index.json              → Nifty500 symbol-list
#   /data/sv3_cache/symbols/<SYMBOL>.json    → har symbol ki alag file (SV3)
_SV2_CACHE_DIR    = os.path.join(_storage_root(), "sv2_cache")
_SV3_CACHE_DIR    = os.path.join(_storage_root(), "sv3_cache")
_SV3_SYMBOLS_DIR  = os.path.join(_SV3_CACHE_DIR, "symbols")
# ── Personal Backup (Google Drive → HF /data) — app-data se poori tarah
# alag folder, sirf personal files (photos/videos/etc.) store karne ke liye.
# Kabhi SV1/SV2/SV3 ke folders se mix nahi hota, kabhi cron/reveal logic
# ise touch nahi karta — ye purely ek "dump yahan" backup-space hai.
_PERSONAL_BACKUP_DIR = os.path.join(_storage_root(), "personal_backup")
for _d in (_SV2_CACHE_DIR, _SV3_SYMBOLS_DIR, _PERSONAL_BACKUP_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        pass

def _url_fetch_save_to_disk(url: str, dest_path: str, timeout: int = 90) -> tuple[bool, str]:
    """Koi bhi URL se bytes download
    karke `dest_path` par ATOMICALLY save karta hai (tmp-file + os.replace —
    beech mein crash ho to bhi purani/adhuri file corrupt nahi hoti). Folder
    khud-ba-khud bana deta hai agar na ho. Returns (ok, message)."""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        raw = resp.content
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        tmp_path = dest_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(raw)
        os.replace(tmp_path, dest_path)
        return True, f"{len(raw):,} bytes saved → `{dest_path}`"
    except Exception as e:
        return False, f"Fetch/save fail: {e}"

# ─── Personal Backup — Google Drive → HF /data (background thread) ─────────
# Bade folders (multi-GB) download karne me time lagta hai, isliye ye UI ko
# block nahi karta — ek background thread me chalta hai, state yahan
# _GDRIVE_IMPORT_STATE me update hoti rehti hai, card bas usi state ko
# dikhata hai (har rerun/click par fresh read hoti hai).
# NOTE: plain module-level dict har Streamlit rerun par reset ho jata tha,
# isliye thread ka result UI tak nahi pahunchta tha. @st.cache_resource se
# ye process-wide singleton ban gaya.
@st.cache_resource
def _get_gdrive_lock() -> threading.Lock:
    return threading.Lock()

@st.cache_resource
def _get_gdrive_state() -> dict:
    return {
        "running": False,
        "started_at": 0,
        "finished_at": 0,
        "ok": None,       # None = kabhi chala hi nahi, True/False = last result
        "msg": "",
        "detail": "",     # full traceback / exact error text
        "subfolder": "",
        "dest_dir": "",
        "url": "",
    }

_GDRIVE_IMPORT_LOCK  = _get_gdrive_lock()
_GDRIVE_IMPORT_STATE: dict = _get_gdrive_state()


def _sanitize_subfolder_name(name: str) -> str:
    """Free-text subfolder name ko safe path-component me convert karta
    hai — sirf alnum/space/-/_ allow, baaki sab strip, khaali ho to
    fallback default deta hai."""
    name = (name or "").strip()
    safe = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_", ".")).strip()
    safe = safe.replace(" ", "_")
    return safe or f"import_{int(time.time())}"


def _dir_total_size(path: str) -> tuple[int, int]:
    """(total_bytes, file_count) — recursively, poore folder ka."""
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
                count += 1
            except OSError:
                pass
    return total, count


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _gdrive_do_import(url: str, dest_dir: str, log_fn=None, flatten: bool = False) -> tuple[bool, str]:
    """Actual download — Google Drive public link (file YA folder, dono
    handle karta hai) se `dest_dir` (HF /data ke andar, already alag
    folder) me. `gdown` library use karta hai kyunki plain `requests` se
    bade Drive files par Google ka 'confirm token' + virus-scan-skip
    warning page aata hai jo `gdown` khud handle karta hai (folder-listing
    bhi khud recursively karta hai)."""
    try:
        import gdown  # noqa: F401  (HF Space ke requirements.txt me add hona chahiye)
    except ImportError:
        return False, (
            "`gdown` package installed nahi hai. HF Space ke `requirements.txt` "
            "me ek line `gdown` add karke Space ko restart karo, phir dobara try karo."
        )

    url = (url or "").strip()
    if not url:
        return False, "Google Drive link khaali hai."

    os.makedirs(dest_dir, exist_ok=True)

    try:
        is_folder = "/folders/" in url or "folders?id=" in url
        if is_folder:
            _log_sv = log_fn or (lambda *a, **k: None)
            _log_sv(f"[gdrive_import] folder download shuru — {url} → {dest_dir}", level="info")
            try:
                result = gdown.download_folder(
                    url=url, output=dest_dir, quiet=True, use_cookies=False, remaining_ok=True,
                )
            except TypeError:
                # naye/purane gdown me kuch kwargs na ho to minimal call
                result = gdown.download_folder(url=url, output=dest_dir, quiet=True)
            if not result:
                return False, (
                    "Folder download se koi file nahi aayi — link public hai confirm karo "
                    "('Anyone with the link' → Viewer), ya folder khaali hai."
                )
            if flatten:
                # gdown Drive-folder ke naam ka extra sub-folder bana deta hai — video player ke liye
                # saari files seedha dest_dir me laa do (same disk par move = instant), khaali folders hata do.
                _abs_dest = os.path.abspath(dest_dir)
                for _p in result:
                    try:
                        if not os.path.isfile(_p) or os.path.dirname(os.path.abspath(_p)) == _abs_dest:
                            continue
                        _bn = os.path.basename(_p)
                        _tgt = os.path.join(dest_dir, _bn)
                        if os.path.exists(_tgt):
                            if os.path.getsize(_tgt) == os.path.getsize(_p):
                                os.remove(_p)      # same naam + same size = pehle se aa chuki
                                continue
                            _b0, _e0 = os.path.splitext(_bn)
                            _tgt = os.path.join(dest_dir, f"{_b0}_{int(time.time())}{_e0}")
                        os.replace(_p, _tgt)
                    except Exception as _e_fl:
                        _log_sv(f"[gdrive_import] flatten skip {_p}: {_e_fl}", level="warn")
                for _root, _dirs, _files in os.walk(dest_dir, topdown=False):
                    if os.path.abspath(_root) != _abs_dest:
                        try:
                            os.rmdir(_root)
                        except OSError:
                            pass
            total_bytes, file_count = _dir_total_size(dest_dir)
            return True, f"{file_count} file(s), {_fmt_bytes(total_bytes)} → `{dest_dir}`"
        else:
            _log_sv = log_fn or (lambda *a, **k: None)
            _log_sv(f"[gdrive_import] single-file download shuru — {url} → {dest_dir}", level="info")
            # `fuzzy=` argument naye gdown versions me hata diya gaya (TypeError),
            # isliye link se file-id nikaal ke canonical URL banate hain —
            # ye har gdown version me chalta hai.
            import re as _re
            _m = _re.search(r"/file/d/([A-Za-z0-9_-]+)", url) or _re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
            _dl_url = f"https://drive.google.com/uc?id={_m.group(1)}" if _m else url
            out_path = gdown.download(url=_dl_url, output=dest_dir + os.sep, quiet=True)
            if not out_path:
                return False, "gdown ne koi file return nahi ki (None) — link public nahi hai, quota exceed hai, ya link galat hai."
            size = os.path.getsize(out_path)
            return True, f"1 file, {_fmt_bytes(size)} → `{out_path}`"
    except Exception as e:
        import traceback as _tb
        _GDRIVE_IMPORT_STATE["detail"] = _tb.format_exc()
        return False, f"Download fail: {type(e).__name__}: {e}"


# ── Import destination picker (2026-10-05) — user khud folder chunta hai ────────
# Sirf in "user-content" roots ke andar (app_state / sv2 / sv3 jaise app-data folders yahan kabhi nahi aate).
_GD_DEST_ROOTS = ("personal_backup", "video", "music", "images")

def _gd_list_dest_folders(max_depth: int = 4) -> list:
    """Chunne layak saare folders (storage root se relative, '/' separator): roots + unke sub-folders."""
    root = os.path.abspath(_storage_root())
    out = []
    for r in _GD_DEST_ROOTS:
        base = os.path.join(root, r)
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            continue
        for dirpath, dirnames, _files in os.walk(base):
            dirnames.sort()
            rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            out.append(rel)
            if rel.count("/") >= max_depth:
                dirnames[:] = []
    return out

def _gd_resolve_dest(dest_rel: str, new_sub: str = ""):
    """Chuna hua folder (+ optional naya sub-folder, 'a/b' bhi chalega) -> asli path, ya None agar allowed roots se bahar."""
    root = os.path.abspath(_storage_root())
    rel = (dest_rel or "").strip().strip("/")
    top = rel.split("/")[0] if rel else ""
    if top not in _GD_DEST_ROOTS:
        return None
    parts = [rel]
    for comp in re.split(r"[\\/]+", new_sub or ""):
        comp = comp.strip()
        if not comp or comp in (".", ".."):
            continue
        parts.append(_sanitize_subfolder_name(comp))
    full = os.path.abspath(os.path.join(root, *parts))
    allowed = os.path.join(root, top)
    if full == allowed or full.startswith(allowed + os.sep):
        return full
    return None


def _gdrive_start_background_import(url: str, dest_rel: str = "personal_backup", new_sub: str = "", flatten: bool = True) -> tuple[bool, str]:
    """Ek baar me sirf ek import chal sakta hai (lock se guard). Turant
    (running=True) return kar deta hai, asal kaam background thread me
    hota hai — card baad me _GDRIVE_IMPORT_STATE poll karke result dikhayega."""
    with _GDRIVE_IMPORT_LOCK:
        if _GDRIVE_IMPORT_STATE["running"]:
            return False, "Ek import pehle se chal raha hai — usko finish hone do."
        # Subfolder OPTIONAL hai: khaali ho to files seedha personal_backup me,
        # naam diya ho to personal_backup/<naam>/ me jaayengi.
        dest_dir = _gd_resolve_dest(dest_rel, new_sub)
        if not dest_dir:
            return False, "Chuna hua folder allowed nahi hai (sirf personal_backup / video / music / images ke andar)."
        _flatten = bool(flatten)
        safe_sub = os.path.relpath(dest_dir, os.path.abspath(_storage_root())).replace(os.sep, "/")
        _GDRIVE_IMPORT_STATE.update({
            "running": True, "started_at": time.time(), "finished_at": 0,
            "ok": None, "msg": "Shuru ho raha hai…", "detail": "", "subfolder": safe_sub,
            "dest_dir": dest_dir, "url": url,
        })

    def _runner():
        try:
            ok, msg = _gdrive_do_import(url, dest_dir, log_fn=_slog, flatten=_flatten)
        except Exception as e:
            import traceback as _tb
            _GDRIVE_IMPORT_STATE["detail"] = _tb.format_exc()
            ok, msg = False, f"Unexpected error: {type(e).__name__}: {e}"
        with _GDRIVE_IMPORT_LOCK:
            _GDRIVE_IMPORT_STATE.update({
                "running": False, "finished_at": time.time(), "ok": ok, "msg": msg,
            })
        _slog(f"[gdrive_import] {'OK' if ok else 'FAIL'}: {msg}", level="ok" if ok else "err")
        if not ok and _GDRIVE_IMPORT_STATE.get("detail"):
            _slog(f"[gdrive_import] traceback:\n{_GDRIVE_IMPORT_STATE['detail']}", level="err")

    threading.Thread(target=_runner, daemon=True, name="gdrive_personal_import").start()
    return True, f"Background me shuru ho gaya → `{dest_dir}`"


# ═══ ⬇ YouTube → HF /data/video downloader (2026-10-05) ═════════════════════════
# Login-page ka "🎥 YouTube → HF Storage" card isse chalata hai. yt-dlp background thread me video seedha
# HF persistent disk par save karta hai — phone/PC par kuch download nahi hota.
# Zaroori (Space rebuild par): requirements.txt me `yt-dlp[default]`, Dockerfile me `ffmpeg` (1080p = video+audio
# alag streams, merge ke liye) aur `deno` (YouTube ka JS challenge). Card ki status-line batati hai kya missing hai.
# State @st.cache_resource me hai — Streamlit har rerun par module dobara chalata hai, plain dict reset ho jaata.
@st.cache_resource
def _get_ytdl_state() -> dict:
    return {"jobs": {}, "lock": threading.Lock(), "run": threading.Lock(), "seq": 0}

_YTDL = _get_ytdl_state()
_YTDL_HOSTS      = ("youtube.com", "youtu.be", "youtube-nocookie.com")   # sirf YouTube (generic extractor se local/internal URL hit na ho)
_YTDL_MIN_FREE   = 2 * 1024 ** 3      # itni disk free na ho to start/chalta download ruk jaata hai
_YTDL_KEEP_JOBS  = 12
_YTDL_QUALITIES  = (1080, 720, 480, 360)

# ── Routes (2026-10-05): HF ka IP YouTube par block ho to download ek-ek route try karta hai, aur job par likhta hai
# ki kaunsa chala (job["via"]) / kaun-kaun fail hue (job["tries"]).
#   1) optional paid proxy  — HF Secret `YT_PROXY` (jaise http://user:pass@host:port) set ho to sabse pehle
#   2) direct (HF IP)
#   3) direct + alag player client (tv / android_vr / ios / web_safari)
#   4) WARP (free Cloudflare) — wgcf + wireproxy local SOCKS5 127.0.0.1:40000 (Dockerfile me install; app khud start karta hai)
#   5) WARP + alag player client
_YTDL_PROXY       = _get_secret("YT_PROXY").strip()
_YTDL_ALT_CLIENTS = ["tv", "android_vr", "ios", "web_safari"]
_WARP_PORT        = 40000
_WARP_PROXY_URL   = f"socks5h://127.0.0.1:{_WARP_PORT}"
_WARP_DIR         = os.path.join(LOCAL_STATE_DIR, "warp")

@st.cache_resource
def _get_warp_state() -> dict:
    return {"lock": threading.Lock(), "proc": None}

_WARP = _get_warp_state()

def _warp_port_open() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", _WARP_PORT), timeout=1):
            return True
    except OSError:
        return False

def _warp_ensure() -> tuple:
    """WARP SOCKS5 proxy chalu karo (agar band ho). Return (ok, msg). Pehli baar free WARP account banata hai
    (wgcf register) aur /data/app_state/warp me save karta hai — dobara nahi banta."""
    import subprocess, tempfile, time as _t
    with _WARP["lock"]:
        if _warp_port_open():
            return True, "WARP proxy chal raha hai"
        wgcf, wproxy = shutil.which("wgcf"), shutil.which("wireproxy")
        if not wgcf or not wproxy:
            return False, "wgcf/wireproxy image me nahi hain (Dockerfile ka download step fail hua — Space ka build log dekho)"
        try:
            os.makedirs(_WARP_DIR, exist_ok=True)
            acct = os.path.join(_WARP_DIR, "wgcf-account.toml")
            prof = os.path.join(_WARP_DIR, "wgcf-profile.conf")
            if not os.path.isfile(prof):
                if not os.path.isfile(acct):
                    r = subprocess.run([wgcf, "register", "--accept-tos"], cwd=_WARP_DIR,
                                       capture_output=True, text=True, timeout=60)
                    if not os.path.isfile(acct):
                        return False, "wgcf register fail: " + ((r.stderr or r.stdout or "").strip()[-200:] or "no output")
                r = subprocess.run([wgcf, "generate"], cwd=_WARP_DIR, capture_output=True, text=True, timeout=60)
                if not os.path.isfile(prof):
                    return False, "wgcf generate fail: " + ((r.stderr or r.stdout or "").strip()[-200:] or "no output")
            with open(prof, "r", encoding="utf-8") as f:
                base = f.read()
            conf = os.path.join(tempfile.gettempdir(), "wireproxy.conf")
            with open(conf, "w", encoding="utf-8") as f:
                f.write(base.rstrip() + f"\n\n[Socks5]\nBindAddress = 127.0.0.1:{_WARP_PORT}\n")
            logp = os.path.join(tempfile.gettempdir(), "wireproxy.log")
            logf = open(logp, "ab")
            proc = subprocess.Popen([wproxy, "-c", conf], stdout=logf, stderr=subprocess.STDOUT)
            _WARP["proc"] = proc
            for _ in range(30):
                _t.sleep(0.5)
                if _warp_port_open():
                    return True, "WARP proxy start ho gaya"
                if proc.poll() is not None:
                    break
            try:
                tail = open(logp, "rb").read()[-300:].decode("utf-8", "replace").strip()
            except Exception:
                tail = ""
            return False, "wireproxy start nahi hua: " + (tail or "no log")
        except subprocess.TimeoutExpired:
            return False, "wgcf timeout (Cloudflare tak pahunch nahi paaya)"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

def _ytdl_strategies() -> list:
    """[(naam, extra yt-dlp opts, needs_warp)] — try karne ka order."""
    alt = {"extractor_args": {"youtube": {"player_client": list(_YTDL_ALT_CLIENTS)}}}
    out = []
    if _YTDL_PROXY:
        out.append(("paid proxy (YT_PROXY)", {"proxy": _YTDL_PROXY}, False))
    out += [
        ("direct (HF IP)", {}, False),
        ("direct + alag player client", dict(alt), False),
        ("WARP", {"proxy": _WARP_PROXY_URL}, True),
        ("WARP + alag player client", dict(alt, proxy=_WARP_PROXY_URL), True),
    ]
    return out

def _ytdl_probe() -> list:
    """Bina download kiye: HF se google/youtube direct aur WARP ke through kaisa jaa raha hai (curl)."""
    import subprocess
    rows = []
    def _curl(label, url, extra):
        try:
            r = subprocess.run(["curl", "-sS", "-o", "/dev/null", "--max-time", "15", "-w", "HTTP %{http_code} in %{time_total}s"]
                               + extra + [url], capture_output=True, text=True, timeout=25)
            out, err = (r.stdout or "").strip(), (r.stderr or "").strip()
            ok = r.returncode == 0 and out.startswith(("HTTP 2", "HTTP 3"))
            rows.append(f"{'✅' if ok else '❌'} {label}: {out or '-'} {err[:160]}".rstrip())
        except Exception as e:
            rows.append(f"❌ {label}: {type(e).__name__}: {e}")
    _curl("direct → google", "https://www.google.com/generate_204", [])
    _curl("direct → youtube", "https://www.youtube.com/", [])
    for _lbl, _u in (("m.youtube", "https://m.youtube.com/"), ("youtu.be", "https://youtu.be/"), ("ytimg", "https://i.ytimg.com/generate_204")):
        _curl(f"direct → {_lbl}", _u, [])
    ok, msg = _warp_ensure()
    rows.append(f"{'✅' if ok else '❌'} WARP start: {msg}")
    if ok:
        sx = ["--socks5-hostname", f"127.0.0.1:{_WARP_PORT}"]
        _curl("WARP → google", "https://www.google.com/generate_204", sx)
        _curl("WARP → youtube", "https://www.youtube.com/", sx)
        for _lbl, _u in (("m.youtube", "https://m.youtube.com/"), ("youtu.be", "https://youtu.be/"), ("ytimg", "https://i.ytimg.com/generate_204")):
            _curl(f"WARP → {_lbl}", _u, sx)
    if _YTDL_PROXY:
        _curl("paid proxy → youtube", "https://www.youtube.com/", ["-x", _YTDL_PROXY])
    return rows

def _ytdl_cookie_path() -> str:
    return os.path.join(LOCAL_STATE_DIR, "yt_cookies.txt")

def _ytdl_url_ok(u: str) -> bool:
    try:
        from urllib.parse import urlparse as _ytdl_urlparse
        p = _ytdl_urlparse((u or "").strip())
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    h = p.hostname.lower()
    return any(h == d or h.endswith("." + d) for d in _YTDL_HOSTS)

def _ytdl_env() -> dict:
    """Card ki status-line ke liye: kya installed hai (import kiye bina — sasta)."""
    out = {"ytdlp": None, "ffmpeg": bool(shutil.which("ffmpeg")), "js": None,
           "cookies": os.path.isfile(_ytdl_cookie_path()),
           "warp": bool(shutil.which("wgcf") and shutil.which("wireproxy"))}
    try:
        from importlib.metadata import version as _pkg_ver
        out["ytdlp"] = _pkg_ver("yt-dlp")
    except Exception:
        pass
    for rt in ("deno", "node", "bun"):
        if shutil.which(rt):
            out["js"] = rt
            break
    return out

def _ytdl_save_cookies(data: bytes) -> tuple:
    if not data or len(data) > 2 * 1024 * 1024:
        return False, "File khaali ya 2MB se badi hai."
    if b"youtube.com" not in data and b"google.com" not in data:
        return False, "Ye YouTube/Google ki cookies.txt (Netscape format) nahi lagti."
    p = _ytdl_cookie_path()
    tmp = p + ".tmp"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, p)
        return True, f"Cookies save ho gayi ({len(data)} bytes)."
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def _ytdl_delete_cookies() -> None:
    try:
        os.remove(_ytdl_cookie_path())
    except Exception:
        pass

def _ytdl_explain(msg: str) -> str:
    m = (msg or "").lower()
    if "not a bot" in m or "sign in to confirm" in m:
        return "YouTube ne HF server ko bot samjha — card me 🍪 cookies.txt upload karo, ya phone se download karke upload karo."
    if "javascript runtime" in m or "n challenge" in m or "nsig" in m:
        return "JS runtime (deno) nahi mila ya kaam nahi kar raha — Dockerfile me deno jodo."
    if "ffmpeg" in m or "ffprobe" in m:
        return "ffmpeg nahi mila — Dockerfile me ffmpeg jodo (1080p merge ke liye zaroori)."
    if "private video" in m:
        return "Ye video private hai."
    if "requested format is not available" in m:
        return "Ye quality/format nahi mila — kam quality try karo."
    if "429" in m or "too many requests" in m:
        return "YouTube ne rate-limit kiya — kuch der baad try karo."
    if "video unavailable" in m or "not available" in m:
        return "Video available nahi (hata di gayi / region-block / ya bot-block)."
    return (msg or "unknown error")[:300]

def _ytdl_run(jid: str) -> None:
    job = _YTDL["jobs"].get(jid)
    if not job:
        return
    with _YTDL["run"]:                      # ek time par ek hi download (HF free CPU/disk ke liye)
        if job["cancel"]:
            job.update(state="cancelled", finished=time.time())
            return
        job["state"] = "running"
        try:
            import yt_dlp
            from yt_dlp.utils import DownloadCancelled
        except Exception as e:
            job.update(state="error", finished=time.time(),
                       error="yt-dlp installed nahi — requirements.txt me `yt-dlp[default]` jodo aur Space rebuild karo",
                       detail=f"{type(e).__name__}: {e}")
            return

        tmp_cookie = None
        tmp_parts: set = set()
        disk_chk = [0.0]

        class _YtLog:
            def debug(self, m): pass
            def info(self, m): pass
            def warning(self, m): job["warn"] = str(m)[:300]
            def error(self, m): job["last_err"] = re.sub(r"\x1b\[[0-9;]*m", "", str(m))[:600]

        def _hook(d):
            if job["cancel"]:
                raise DownloadCancelled()
            st_ = d.get("status")
            info = d.get("info_dict") or {}
            if d.get("tmpfilename"):
                tmp_parts.add(d["tmpfilename"])
            if st_ == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                job.update(pct=(round(got * 100.0 / tot, 1) if tot else None),
                           speed=d.get("speed"), eta=d.get("eta"),
                           title=str(info.get("title") or job.get("title") or "")[:120],
                           idx=info.get("playlist_index"),
                           n=info.get("n_entries") or info.get("playlist_count"))
                now = time.time()
                if now - disk_chk[0] > 5:
                    disk_chk[0] = now
                    try:
                        if shutil.disk_usage(job["dest_dir"]).free < _YTDL_MIN_FREE:
                            job["nospace"] = True
                            raise DownloadCancelled()
                    except DownloadCancelled:
                        raise
                    except Exception:
                        pass
            elif st_ == "finished":
                job["pct"] = 100.0

        def _post(path):
            job["files"] += 1
            job["last_file"] = os.path.basename(str(path))

        try:
            os.makedirs(job["dest_dir"], exist_ok=True)
            base = job["dest_dir"].replace("%", "%%")
            if job["playlist"]:
                tmpl = os.path.join(base, "%(playlist_title).80B", "%(playlist_index)03d - %(title).120B [%(id)s].%(ext)s")
            else:
                tmpl = os.path.join(base, "%(title).120B [%(id)s].%(ext)s")
            h = int(job["height"])
            has_ff = bool(shutil.which("ffmpeg"))
            # Sort: pehle resolution (h tak), phir H.264, phir AAC/mp4 — player H.264/AAC mp4 chahta hai.
            # H.264 1080p na ho to us video ka VP9/AV1 1080p lega (720p H.264 par nahi girega).
            opts = {
                "outtmpl": tmpl,
                "format": "bv*+ba/b" if has_ff else "b[ext=mp4]/b",
                "format_sort": [f"res:{h}", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
                "merge_output_format": "mp4/webm",
                "noplaylist": not job["playlist"],
                "ignoreerrors": "only_download" if job["playlist"] else False,
                "windowsfilenames": True,
                "retries": 5, "fragment_retries": 5, "socket_timeout": 30,
                "concurrent_fragment_downloads": 4,
                "continuedl": True, "nooverwrites": True,
                "quiet": True, "no_warnings": True, "noprogress": True, "no_color": True,
                "logger": _YtLog(), "progress_hooks": [_hook], "post_hooks": [_post],
            }
            if not has_ff:
                job["warn"] = "ffmpeg nahi hai — sirf single-file (aam taur par 360p) quality milegi."
            env = _ytdl_env()
            if env["js"] and env["js"] != "deno":
                opts["js_runtimes"] = {env["js"]: {}}          # deno default hai; node/bun ho to batana padta hai
            ck = _ytdl_cookie_path()
            if os.path.isfile(ck):
                import tempfile
                fd, tmp_cookie = tempfile.mkstemp(prefix="ytck_", suffix=".txt")
                os.close(fd)
                shutil.copyfile(ck, tmp_cookie)                # yt-dlp cookie file wapas likhta hai — original ko chhoone nahi dete
                opts["cookiefile"] = tmp_cookie

            free = shutil.disk_usage(job["dest_dir"]).free
            if free < _YTDL_MIN_FREE:
                job.update(state="error", finished=time.time(),
                           error=f"Disk me sirf {_fmt_bytes(free)} bachi hai — kam se kam {_fmt_bytes(_YTDL_MIN_FREE)} chahiye.")
                return

            # Routes ek-ek karke: pehla jo chale wahi job["via"] me likha rehta hai; fail wale job["tries"] me.
            def _clean(t):
                t = re.sub(r"\x1b\[[0-9;]*m", "", str(t))
                return t.replace(_YTDL_PROXY, "***") if _YTDL_PROXY else t
            attempts, ok_run, warp_state = [], False, None
            for name, extra, needs_warp in _ytdl_strategies():
                if job["cancel"]:
                    break
                if needs_warp:
                    if warp_state is None:
                        job["via"] = "WARP start ho raha…"
                        warp_state = _warp_ensure()
                    if not warp_state[0]:
                        attempts.append(f"{name}: WARP start nahi hua — {warp_state[1]}")
                        job["tries"] = list(attempts)
                        continue
                o = dict(opts)
                o.update(extra)
                o["extractor_retries"] = 1
                job["via"] = name
                job["last_err"] = ""
                try:
                    with yt_dlp.YoutubeDL(o) as ydl:
                        rc = ydl.download([job["url"]])
                    if job["files"] == 0 and rc:
                        raise RuntimeError(job.get("last_err") or "download fail")
                    ok_run = True
                    break
                except DownloadCancelled:
                    raise
                except Exception as e_try:
                    raw_try = _clean(f"{type(e_try).__name__}: {e_try}")
                    attempts.append(f"{name}: {_ytdl_explain(raw_try)[:160]}")
                    job["tries"] = list(attempts)
                    job["detail"] = "\n".join(attempts)
                    if job["files"] > 0:        # beech me file aa chuki — doosre route par mat jao
                        raise
            if job["cancel"]:
                job.update(state="cancelled", finished=time.time())
            elif ok_run:
                job.update(state="done", finished=time.time(), pct=100.0)
                if job["files"] == 0:
                    job["warn"] = "Koi nayi file nahi bani — shayad ye video pehle se isi folder me hai."
            else:
                job.update(state="error", finished=time.time(),
                           error="Kisi bhi route se download nahi hua — neeche dekho kaun-kaun fail hua.",
                           detail="\n".join(attempts))
        except DownloadCancelled:
            if job.get("nospace"):
                job.update(state="error", finished=time.time(),
                           error=f"Disk kam bachi (< {_fmt_bytes(_YTDL_MIN_FREE)}) — download rok diya.")
            else:
                job.update(state="cancelled", finished=time.time())
        except Exception as e:
            import traceback as _tb
            raw = f"{type(e).__name__}: {e}"
            if job["cancel"]:
                job.update(state="cancelled", finished=time.time())
            else:
                job.update(state="error", finished=time.time(),
                           error=_ytdl_explain(re.sub(r"\x1b\[[0-9;]*m", "", raw)),
                           detail=re.sub(r"\x1b\[[0-9;]*m", "", _tb.format_exc())[-1500:])
        finally:
            if tmp_cookie:
                try:
                    os.remove(tmp_cookie)
                except Exception:
                    pass
            if job["state"] == "cancelled":                     # adhoori .part files saaf (error par resume ke liye rehne dete hain)
                for pth in tmp_parts:
                    try:
                        os.remove(pth)
                    except Exception:
                        pass
        _slog(f"[ytdl] {jid} {job['state']} files={job['files']} err={job.get('error') or '-'}",
              level="ok" if job["state"] == "done" else "warn")

def _ytdl_start(url: str, dest_rel: str, new_sub: str, height: int, playlist: bool) -> tuple:
    url = (url or "").strip()
    if not _ytdl_url_ok(url):
        return False, "Sirf YouTube link chalega (youtube.com / youtu.be)."
    if (dest_rel or "").strip("/").split("/")[0] != "video":
        return False, "Folder sirf /data/video ke andar hi chun sakte ho."
    dest_dir = _gd_resolve_dest(dest_rel, new_sub)
    if not dest_dir:
        return False, "Chuna hua folder allowed nahi hai."
    if height not in _YTDL_QUALITIES:
        height = 720
    try:
        os.makedirs(dest_dir, exist_ok=True)
        free = shutil.disk_usage(dest_dir).free
        if free < _YTDL_MIN_FREE:
            return False, f"Disk me sirf {_fmt_bytes(free)} bachi hai — kam se kam {_fmt_bytes(_YTDL_MIN_FREE)} chahiye."
    except Exception as e:
        return False, f"Folder/disk check fail: {type(e).__name__}: {e}"
    with _YTDL["lock"]:
        jobs = _YTDL["jobs"]
        for j in jobs.values():
            if j["url"] == url and j["dest_dir"] == dest_dir and j["state"] in ("queued", "running"):
                return False, "Ye link isi folder ke liye pehle se queue/chal raha hai."
        _YTDL["seq"] += 1
        jid = f"y{int(time.time())}_{_YTDL['seq']}"
        jobs[jid] = {
            "id": jid, "url": url, "dest_dir": dest_dir, "height": height, "playlist": bool(playlist),
            "state": "queued", "pct": None, "speed": None, "eta": None, "title": "", "idx": None, "n": None,
            "files": 0, "last_file": "", "warn": "", "error": "", "detail": "", "last_err": "", "via": "", "tries": [],
            "created": time.time(), "finished": 0.0, "cancel": False, "nospace": False,
        }
        old = sorted((k for k, j in jobs.items() if j["state"] not in ("queued", "running")),
                     key=lambda k: jobs[k]["created"])
        while len(jobs) > _YTDL_KEEP_JOBS and old:
            jobs.pop(old.pop(0), None)
    threading.Thread(target=_ytdl_run, args=(jid,), daemon=True, name=f"ytdl_{jid}").start()
    _slog(f"[ytdl] queued {jid} {height}p playlist={playlist} → {dest_dir} url={url}")
    return True, f"Queue me daal diya → `{dest_dir}`"

def _ytdl_cancel(jid: str) -> None:
    j = _YTDL["jobs"].get(jid)
    if j and j["state"] in ("queued", "running"):
        j["cancel"] = True


def _storage_list_all_files() -> list[dict]:
    """Storage root ke andar saari files recursively list karta hai (sub-
    folders sahit) — har entry mein rel_path (delete ke liye reference),
    size_bytes, aur modified (IST) hota hai. Alphabetically sorted."""
    root = _storage_root()
    out: list[dict] = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            try:
                rel = os.path.relpath(full, root)
                _st = os.stat(full)
                out.append({
                    "rel_path": rel,
                    "size_bytes": _st.st_size,
                    "modified": datetime.datetime.fromtimestamp(_st.st_mtime, IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                })
            except Exception:
                continue
    out.sort(key=lambda d: d["rel_path"])
    return out

def _storage_safe_full_path(rel_path: str):
    """rel_path ko storage root ke ANDAR hi resolve karta hai — agar
    (../.. waghera se) root ke bahar nikalne ki koshish ho to None return
    karta hai, taaki delete sirf storage root ke andar hi ho sake."""
    root = os.path.abspath(_storage_root())
    full = os.path.abspath(os.path.join(root, rel_path))
    if full == root or full.startswith(root + os.sep):
        return full
    return None

def _storage_delete_files(rel_paths: list) -> tuple[bool, str]:
    """_storage_list_all_files() se aaye hue rel_path(s) ko ek-ek karke
    delete karta hai — ek fail ho to baaki pe rukta nahi, end mein combined
    result deta hai."""
    ok_count = 0
    errors: list[str] = []
    for rel in rel_paths:
        full = _storage_safe_full_path(rel)
        if full is None:
            errors.append(f"{rel}: invalid path (blocked)")
            continue
        try:
            if os.path.isfile(full):
                os.remove(full)
                ok_count += 1
            else:
                errors.append(f"{rel}: file nahi mili (pehle se hi delete ho chuki?)")
        except Exception as e:
            errors.append(f"{rel}: {e}")
    msg = f"{ok_count} file(s) delete ho gayi."
    if errors:
        msg += " Errors: " + "; ".join(errors)
    return (len(errors) == 0), msg

# (2026-10-01) _storage_wipe_all() PERMANENTLY HATA DIYA — ye poora /data (saari drawings/settings/layout + Render ke
# rules.json / journal.jsonl / render_log.jsonl) ek click me uda deta tha. Ab sirf _storage_delete_files() (chuni hui
# files) bacha hai. Wapas mat lagana; zaroorat ho to /data ki file HF Space → Files se haath se hatao.

# ─── Daily update-status tracker — HF `/data/update_status.json` par persist
# hota hai (restart-safe). Read: mtime-based RAM cache (file badli tabhi disk
# dobara padhti hai), isliye chart HTML banate waqt koi network call nahi.
# Write: atomic (tmp + os.replace). ──
_UPDATE_STATUS_FILENAME = "update_status.json"
_UPDATE_STATUS_PATH = os.path.join(_storage_root(), _UPDATE_STATUS_FILENAME)
_UPDATE_STATUS_LOCK = threading.Lock()
_UPDATE_STATUS_RAM = {"mtime": None, "data": {}}

def _load_update_status() -> dict:
    """Local file se status. File na ho ya corrupt ho to khaali dict —
    matlab "kabhi update nahi hua"."""
    with _UPDATE_STATUS_LOCK:
        try:
            mt = os.path.getmtime(_UPDATE_STATUS_PATH)
        except OSError:
            return {}
        if _UPDATE_STATUS_RAM["mtime"] != mt:
            try:
                with open(_UPDATE_STATUS_PATH, "r", encoding="utf-8") as f:
                    d = json.load(f)
                _UPDATE_STATUS_RAM.update(mtime=mt, data=d if isinstance(d, dict) else {})
            except Exception:
                return {}
        return json.loads(json.dumps(_UPDATE_STATUS_RAM["data"]))

def _save_update_status(status: dict) -> tuple[bool, str]:
    try:
        os.makedirs(os.path.dirname(_UPDATE_STATUS_PATH), exist_ok=True)
        tmp = _UPDATE_STATUS_PATH + ".tmp"
        with _UPDATE_STATUS_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _UPDATE_STATUS_PATH)
            _UPDATE_STATUS_RAM["mtime"] = None   # agli read taaza file padhe
        return True, "update_status.json disk par save ho gayi."
    except Exception as e:
        _slog_exception("_save_update_status", e)
        return False, f"update_status save fail: {e}"

def _mark_updated_today(source: str) -> None:
    status = _load_update_status()
    status[source] = {"last_date": _ist_now().strftime("%Y-%m-%d"), "last_ts": int(time.time())}
    ok, msg = _save_update_status(status)
    _slog(f"AUTO_UPDATE [{source}] status-mark: {msg}", level="ok" if ok else "warn")

# ─── MANUAL-ONLY UPDATE ALL — saare 6 sources (bn/btc/nifty500/btc_sv3_1d/
# sv1_master/td_symbols) sirf is EK function se, sirf user ke 💾 debug-panel
# button-click se update hote hain — koi background thread/daily-silent-run
# nahi hai. Sequential hai (parallel nahi), taaki ek saath saari
# proxy/Binance/Fyers calls na takrayein aur log/status clean rahe; is wajah
# se poora run 1-3 minute le sakta hai.
def _manual_update_all_sources() -> dict:
    """chart.html 💾 debug panel ke naye 'Update All Files Now' button ke
    liye — har source ka (ok, msg) collect karke ek dict return karta hai
    (key: {"ok": bool, "msg": str}). Safal source ka status local disk par
    mark hota hai (_mark_updated_today), taaki panel ka purana 'last
    update' UI bina kisi change ke chalta rahe. Ek source fail ho to baaki
    sources par koi asar nahi — har ek apne try/except mein isolated hai."""
    _sources = [
        ("bn", _append_new_bn_candles),
        ("btc", _append_new_btc_candles),
        ("nifty500", _nifty500_incremental_update),
        ("btc_sv3_1d", _btc_sv3_incremental_update),
        ("sv1_master", _replay_master_incremental_update),
        # td_symbols (Twelve Data) abhi DISABLED — dobara chalana ho to neeche wali line uncomment karo.
        # ("td_symbols", lambda: _td.td_update_all(TWELVEDATA_API_KEY, log_fn=_slog)),
    ]
    results: dict = {}
    for _src, _fn in _sources:
        try:
            _ok, _msg = _fn()
        except Exception as _e_upd:
            _slog_exception(f"manual_update_all[{_src}]", _e_upd)
            _ok, _msg = False, f"{type(_e_upd).__name__}: {_e_upd}"
        results[_src] = {"ok": bool(_ok), "msg": str(_msg)}
        if _ok:
            _mark_updated_today(_src)
        _slog(f"[UPDATE_ALL] {_src}: {'ok' if _ok else 'FAILED'} — {_msg}",
              level="ok" if _ok else "err")
    return results

# ─── (2026-09-28) Update All — HTTP transport ───────────
# Kaam 1-3 min ka hai, isliye ek HTTP request ko itni der khula rakhna proxy
# timeout me phans sakta hai. Isliye: /api/update_all background thread start
# karta hai (turant return), aur /api/update_all_status se browser poll karta
# hai. State module-level hai (side-server thread se st.session_state nahi
# milta). _manual_update_all_sources() me koi st.session_state use nahi hota.
_UPDATE_ALL_LOCK = threading.Lock()
_UPDATE_ALL_STATE = {"running": False, "started_at": 0.0, "finished_at": 0.0,
                     "results": None, "error": None, "run_id": 0}

def _update_all_worker(run_id: int) -> None:
    try:
        res = _manual_update_all_sources()
        with _UPDATE_ALL_LOCK:
            _UPDATE_ALL_STATE.update(results=res, error=None)
    except Exception as e:
        _slog_exception("update_all_worker", e)
        with _UPDATE_ALL_LOCK:
            _UPDATE_ALL_STATE.update(results=None, error=f"{type(e).__name__}: {e}")
    finally:
        with _UPDATE_ALL_LOCK:
            _UPDATE_ALL_STATE.update(running=False, finished_at=time.time())
        _slog(f"[http] update_all run#{run_id} — finished")

def _update_all_start() -> dict:
    """Agar pehle se chal raha hai to naya nahi shuru karta (double-click safe)."""
    with _UPDATE_ALL_LOCK:
        if _UPDATE_ALL_STATE["running"]:
            return {"ok": True, "started": False, "running": True,
                    "run_id": _UPDATE_ALL_STATE["run_id"]}
        _UPDATE_ALL_STATE.update(running=True, started_at=time.time(), finished_at=0.0,
                                 results=None, error=None,
                                 run_id=_UPDATE_ALL_STATE["run_id"] + 1)
        rid = _UPDATE_ALL_STATE["run_id"]
    threading.Thread(target=_update_all_worker, args=(rid,), daemon=True,
                     name=f"update_all_{rid}").start()
    _slog(f"[http] update_all run#{rid} — started")
    return {"ok": True, "started": True, "running": True, "run_id": rid}



def _update_all_status() -> dict:
    with _UPDATE_ALL_LOCK:
        st_ = dict(_UPDATE_ALL_STATE)
    done = (not st_["running"]) and st_["finished_at"] > 0
    out = {"kind": "update_all_result", "ok": True, "running": st_["running"],
           "done": done, "run_id": st_["run_id"]}
    if done:
        out["results"] = st_["results"] or {}
        if st_["error"]:
            out["ok"] = False
            out["error"] = st_["error"]
    return out

# ─── Append naye candles — ab SEEDHA `.bin` ke END me (NO_PRELOAD_PLAN Step 2) ─
# Pehle: poori gz load -> merge -> poori gz dobara compress (heavy, RAM me
# poori file). Ab: sirf header se last_t padho, naye candles fetch karo, `.bin`
# ke end me append karo (torn-write safe, dekho _bin_append_rows). Purani
# `.gz` files ko ab KABHI nahi chhuaa jaata — wo cutover-din ka snapshot/
# backup hain (NO_PRELOAD_PLAN 6.7).
_GZ_APPEND_OFFSET = 19800   # BankNifty ke liye real-UTC → IST-naive (same as _IST_NAIVE_OFFSET, upar define se pehle yahan bhi chahiye)

def _bin_append_guard() -> "str | None":
    """Update-flow chalne se pehle sanity: numpy hai, aur converter beech me
    nahi chal raha (dono ek saath manifest/files badalte to takkar hoti)."""
    if _np is None:
        return "numpy install nahi hai — requirements.txt me `numpy` add karo."
    try:
        if _get_bin_state()["running"]:
            return "🧱 .bin Converter abhi chal raha hai — uske khatam hone ke baad update karo."
    except Exception:
        pass
    return None

def _fetch_binance_5m_since(last_t: int) -> list:
    """Binance public klines (5m) — last_t ke AGLE candle se ab tak. Sirf
    BAND (closed) candles lautata hai (append-only file me adhoori forming-
    candle ek baar likh di to badli nahi ja sakti, isliye usse chhod dete
    hain; agla update use poora karke laayega)."""
    start_ms = (last_t + 300) * 1000
    end_ms = int(time.time() * 1000)
    new_rows = []
    cursor = start_ms
    while cursor < end_ms:
        resp = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/klines",  # Render relay (already public-whitelisted) — IP proxy nahi chahiye
            params={"symbol": "BTCUSDT", "interval": "5m",
                    "startTime": cursor, "endTime": end_ms, "limit": 1000},
            proxies=None, timeout=20,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for k in batch:
            new_rows.append({"t": int(k[0] // 1000), "o": float(k[1]),
                              "h": float(k[2]), "l": float(k[3]), "c": float(k[4])})
        cursor = batch[-1][0] + 300_000   # next candle after last openTime
        if len(batch) < 1000:
            break
    now_s = int(time.time())
    return [r for r in new_rows if r["t"] + 300 <= now_s]

def _bin_append_dicts(dest: str, new_rows: list) -> int:
    """list-of-dict {t,o,h,l,c} -> normalize -> sirf last_t se AAGE wale rows
    `dest` ke end me append. Returns appended count."""
    arr, _st = _bin_rows_to_array(new_rows)
    arr, _info = _bin_normalize_sorted(arr)
    h = _bin_read_header(dest)
    if h["row_count"]:
        arr = arr[arr["t"] > h["last_t"]]
    return _bin_append_rows(dest, arr)

def _bin_mark_diverged(name: str, dest: str) -> None:
    """Manifest me likh do ki ye `.bin` ab purani gz se AAGE hai (append hua).
    Converter (force bhi) is asset ko overwrite nahi karega — warna append
    hua data purani gz se dobara likh jaata."""
    try:
        man = _bin_manifest_load()
        h = _bin_read_header(dest)
        e = dict(man["datasets"].get(name) or {})
        e.update({
            "rows": h["row_count"], "first_t": h["first_t"], "last_t": h["last_t"],
            "tf_seconds": h["tf_seconds"], "flags": h["flags"],
            "bytes": _BIN_HDR_SIZE + h["row_count"] * _BIN_ROW_SIZE,
            "diverged": True,
            "appended_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        e.setdefault("warnings", [])
        e.setdefault("verify", "ok")
        man["datasets"][name] = e
        _bin_manifest_save(man)
    except Exception as ex:
        _slog(f"manifest diverged-mark fail ({name}): {ex}", level="err")

def _append_new_btc_candles() -> tuple[bool, str]:
    """SV2 BTC: Binance REST (5m) se `src/sv2_btc_5m.bin` ke aakhri candle ke
    baad ka naya data fetch -> `.bin` ke end me append -> SV2 TF bins
    background me rebuild. (Binance call proxy se, disk-write local.)"""
    g = _bin_append_guard()
    if g:
        return False, g
    dest = os.path.join(_bin_root(), "src", "sv2_btc_5m.bin")
    try:
        h = _bin_read_header(dest)
    except Exception as e:
        return False, f"SV2 BTC .bin nahi mili/kharab ({e}) — pehle starting-page par 🧱 .bin Converter chalao."
    if not h["row_count"]:
        return False, "SV2 BTC .bin khaali hai — pehle .bin Converter theek se chalao."
    last_t = h["last_t"]   # BTC timestamps real UTC hain (offset nahi)
    if (last_t + 300) * 1000 >= int(time.time() * 1000):
        return True, f"BTC data already up-to-date hai, naya candle abhi bana nahi. Last candle: {_fmt_last_candle_date(last_t)}."
    try:
        new_rows = _fetch_binance_5m_since(last_t)
        if not new_rows:
            return True, f"Binance se koi naya (band) candle nahi mila (already latest). Last candle: {_fmt_last_candle_date(last_t)}."
        n = _bin_append_dicts(dest, new_rows)
        if n == 0:
            return True, f"BTC: koi naya candle append nahi hua (already latest). Last candle: {_fmt_last_candle_date(last_t)}."
        _bin_mark_diverged("sv2_btc_src", dest)
        _sv2_rebuild_start("btc")
        new_last = _bin_read_header(dest)["last_t"]
        return True, (f"BTC: {n} naye candles .bin me append hue. Last candle: {_fmt_last_candle_date(new_last)}. "
                      f"(SV2 TF bins background me rebuild ho rahe hain — chhoti der me chart me dikhenge.)")
    except Exception as e:
        _slog_exception("BTC_BTN_CLICK _append_new_btc_candles()", e)
        return False, f"BTC append fail: {e} (poori traceback debug block me EXCEPTION tag ke saath)"


# ─── SV1 Replay Master File — daily incremental update (append-only .bin) ─────
def _replay_master_incremental_update() -> tuple[bool, str]:
    """SV1 replay-symbols ka master — Binance se naye 5m candles khinch kar
    `src/sv1_master_5m.bin` ke END me append karta hai (poori file kabhi load
    nahi hoti). Kai din na chalaya jaaye to bhi safe — 'kitna gap hai utna
    fetch karega', koi jump/reset nahi."""
    g = _bin_append_guard()
    if g:
        return False, g
    dest = _replay.master_bin_path()
    try:
        h = _bin_read_header(dest)
    except Exception as e:
        return False, f"SV1 master .bin nahi mili/kharab ({e}) — pehle starting-page par 🧱 .bin Converter chalao."
    if not h["row_count"]:
        return False, "SV1 master .bin khaali hai — pehle .bin Converter theek se chalao."
    last_t = h["last_t"]
    if (last_t + 300) * 1000 >= int(time.time() * 1000):
        return True, f"SV1 master file already up-to-date hai, naya candle abhi bana nahi. Last candle: {_fmt_last_candle_date(last_t)}."
    try:
        new_rows = _fetch_binance_5m_since(last_t)
        if not new_rows:
            return True, f"Binance se koi naya (band) candle nahi mila (already latest). Last candle: {_fmt_last_candle_date(last_t)}."
        n = _bin_append_dicts(dest, new_rows)
        if n == 0:
            return True, f"SV1 master: koi naya candle append nahi hua (already latest). Last candle: {_fmt_last_candle_date(last_t)}."
        _bin_mark_diverged("sv1_master", dest)
        try:
            _replay.replay_invalidate_caches()
        except Exception:
            pass
        new_last = _bin_read_header(dest)["last_t"]
        return True, (f"✅ SV1 Master file update ho gayi — {n} naye candles jode gaye. "
                       f"Last candle: {_fmt_last_candle_date(new_last)}. (.bin me append.)")
    except Exception as e:
        _slog_exception("_replay_master_incremental_update", e)
        return False, f"SV1 master update fail: {e} (poori traceback debug block me EXCEPTION tag ke saath)"

def _append_new_bn_candles() -> tuple[bool, str]:
    """Fyers history (5m, direct — koi proxy nahi) se `src/sv2_bn_5m.bin` ke
    aakhri candle ke baad ka naya data fetch -> end me append -> SV2 TF bins
    background me rebuild."""
    g = _bin_append_guard()
    if g:
        return False, g
    creds = load_creds()
    if not creds.get("access_token"):
        return False, "Fyers login nahi hai — pehle Fyers se login karo, tabhi BankNifty data mil sakta hai."
    # FIX (purana): sirf access_token ki *presence* kaafi nahi — expire ho chuka
    # token file me rehta hai aur Fyers silently khaali candles deta hai.
    # is_session_active(update_cache=False) se live validity check; global
    # page-routing cache me kuch nahi likhta (warna button se poora app
    # "logged in" ho jaata — purana bug).
    if not is_session_active(update_cache=False):
        return False, "Fyers token expire ho chuka hai — sidebar se re-login karo, phir BankNifty Update Karo dabao."
    dest = os.path.join(_bin_root(), "src", "sv2_bn_5m.bin")
    try:
        h = _bin_read_header(dest)
    except Exception as e:
        return False, f"SV2 BankNifty .bin nahi mili/kharab ({e}) — pehle starting-page par 🧱 .bin Converter chalao."
    if not h["row_count"]:
        return False, "SV2 BankNifty .bin khaali hai — pehle .bin Converter theek se chalao."
    last_t_naive = h["last_t"]                            # IST-naive stored value
    last_t_real  = last_t_naive - _GZ_APPEND_OFFSET       # real UTC epoch
    from_d = datetime.datetime.fromtimestamp(last_t_real, tz=IST).strftime("%Y-%m-%d")
    to_d   = _ist_now().strftime("%Y-%m-%d")
    try:
        # raise_on_error=True: fetch fail aur "naya candle nahi" ab mix nahi hote.
        candles = _fyers_history_chunk("5", from_d, to_d, raise_on_error=True)   # [ [epoch_sec, o,h,l,c,v], ... ]
        if not candles:
            return True, f"Fyers se koi naya candle nahi mila (already latest, ya market band hai). Last candle: {_fmt_last_candle_date(last_t_real)}."
        now_s = int(time.time())
        new_rows = []
        for c in candles:
            t_real = c[0] // 1000 if c[0] > 10_000_000_000 else c[0]  # ms→s agar zaroorat ho
            if t_real + 300 > now_s:
                continue   # abhi ban rahi (forming) candle — append-only me nahi likhte
            t_naive = t_real + _GZ_APPEND_OFFSET
            if t_naive <= last_t_naive:
                continue   # already maujood
            new_rows.append({"t": t_naive, "o": c[1], "h": c[2], "l": c[3], "c": c[4]})
        if not new_rows:
            return True, f"BankNifty data already up-to-date hai. Last candle: {_fmt_last_candle_date(last_t_real)}."
        n = _bin_append_dicts(dest, new_rows)
        if n == 0:
            return True, f"BankNifty: koi naya candle append nahi hua (already latest). Last candle: {_fmt_last_candle_date(last_t_real)}."
        _bin_mark_diverged("sv2_bn_src", dest)
        _sv2_rebuild_start("bn")
        _bn_last_real = _bin_read_header(dest)["last_t"] - _GZ_APPEND_OFFSET
        return True, (f"BankNifty: {n} naye candles .bin me append hue. Last candle: {_fmt_last_candle_date(_bn_last_real)}. "
                      f"(SV2 TF bins background me rebuild ho rahe hain.)")
    except Exception as e:
        _slog_exception("BN_BTN_CLICK _append_new_bn_candles()", e)
        return False, f"BankNifty fetch/append fail — Fyers API call safal nahi hua: {e} (poori traceback debug block me EXCEPTION tag ke saath)"

# ─── Underlying stocks — poori (jitni available) daily history .gz ────────
# Index (NIFTYBANK-INDEX) nahi, balki individual stocks ka 1D candle data —
# ek hi .gz file mein, starting page se "File Banao" + "Download .gz" se.

# ── Nifty 500 symbol list: HF disk-cache ki `_index.json` se (NSE/live-CSV se nahi) ──
# Is file mein har symbol ke per-symbol data-file ka mapping hai
# ({"symbol":"NSE:XXX-EQ","file":"..."}) — already-loaded stocks ka accurate,
# single-read source.

@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_nifty500_symbols() -> list:
    """Nifty500 symbol list — SIRF HF `/data/sv3_cache/_index.json` disk-
    cache se. 1hr
    cache hai taaki har rerun par dobara disk-read na ho."""
    _idx_path = os.path.join(_SV3_CACHE_DIR, "_index.json")
    try:
        if not os.path.isfile(_idx_path):
            _slog(f"SV3 _index.json disk par nahi mili: {_idx_path} — "
                  f"pehle starting-page Migration card se daalo.", level="err")
            return []
        with open(_idx_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        symbols = [row["symbol"] for row in data if isinstance(row, dict) and row.get("symbol")]
        if len(symbols) >= 400:   # sanity check
            return symbols
    except Exception as e:
        _slog_exception("_fetch_nifty500_symbols (disk)", e)
    return []



def _fyers_history_symbol_chunk(symbol: str, resolution: str, from_date: str, to_date: str) -> list:
    """_fyers_history_chunk jaisa hi hai, bas symbol parameterized hai — kisi
    bhi NSE:XXXX-EQ symbol (BankNifty stocks) ka history layega, index ke
    hardcoded NIFTYBANK-INDEX ki jagah."""
    creds = load_creds()
    if not creds.get("access_token"):
        return []
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol": symbol, "resolution": resolution,
        "date_format": "1", "range_from": from_date, "range_to": to_date, "cont_flag": "1",
    }
    try:
        resp = requests.get("https://api-t1.fyers.in/data/history",
                             headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        res = resp.json()
        if res.get("s") == "ok":
            return [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]] for c in res.get("candles", [])]
        return []   # "no_data" (stock is period mein listed nahi tha) ya koi aur non-ok — dono skip
    except Exception:
        return []

def _normalize_daily_candles(all_candles: list) -> list:
    """Raw Fyers daily candles ([epoch_ms, o,h,l,c,v], ...) ko 9:15 AM IST
    ke consistent convention mein normalize karta hai (same jo
    _fetch_symbol_full_daily aur _append_new_bn_candles use karte hain)."""
    _IST_OFF, _NSE_OPEN = 19800, 33300
    normalized = []
    for c in all_candles:
        t_sec        = int(c[0]) // 1000
        ist_sec      = t_sec + _IST_OFF
        ist_midnight = ist_sec - (ist_sec % 86400)
        t_fixed      = (ist_midnight - _IST_OFF) + _NSE_OPEN
        normalized.append({"t": t_fixed, "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
    return normalized

def _fetch_symbol_full_daily(symbol: str, years: int = 30, max_workers: int = 3) -> list:
    """Ek stock ka poora available daily (1D) history — koi upper cap nahi,
    jitna bhi Fyers de utna poora (stock jitni purani listed hai utna hi
    milega; kam purani ho to kam hi aayega — koi error nahi).

    max_workers kam rakha gaya hai (aur caller symbols ke beech bhi thoda
    rukta hai) taaki Fyers ka per-second rate-limit na lage — pehle 6 workers
    x 14 symbols ek saath fire hone se end ke symbols ke liye Fyers silently
    empty/reject deta tha (list ke aakhri stocks ka data hi nahi aata tha)."""
    today = _ist_now()
    chunk_days  = 360   # Fyers daily-resolution max safe range per call
    start_limit = today - datetime.timedelta(days=years * 365)
    ranges = []
    end = today
    while end > start_limit:
        start = max(end - datetime.timedelta(days=chunk_days), start_limit)
        ranges.append((start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))
        end = start - datetime.timedelta(days=1)

    all_candles: list = []
    seen: set = set()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(lambda r: _fyers_history_symbol_chunk(symbol, "D", r[0], r[1]), ranges))

    # Kuch chunks khali aa sakte hain sirf rate-limit ki wajah se (na ki
    # genuinely "stock is period mein listed nahi tha") — unhe alag se,
    # dheere-dheere (serial + delay) dobara try karo. Baaki asli-empty
    # (listing-se-pehle wale) chunks dobara try karne par bhi khali hi
    # aayenge, koi harm nahi.
    empty_idxs = [i for i, chunk in enumerate(results) if not chunk]
    for i in empty_idxs:
        time.sleep(0.8)
        retry_chunk = _fyers_history_symbol_chunk(symbol, "D", ranges[i][0], ranges[i][1])
        if retry_chunk:
            results[i] = retry_chunk

    for chunk in results:
        for c in chunk:
            if c[0] not in seen:
                seen.add(c[0])
                all_candles.append(c)
    all_candles.sort(key=lambda x: x[0])
    return _normalize_daily_candles(all_candles)

def _fetch_symbol_incremental_daily(symbol: str, from_date: str, to_date: str) -> list:
    """Ek symbol ka daily data sirf ek chhoti date-range ke liye (naye/missing
    candles ke liye) — poore-30-saal wale _fetch_symbol_full_daily se alag,
    ye sirf gap fill karta hai isliye bahut halka/fast hai. 360-din se badi
    range ho (jaise pehli baar ya lambe gap ke baad) to zaroorat padne par
    chunks mein tod deta hai."""
    start = datetime.datetime.strptime(from_date, "%Y-%m-%d").date()
    end   = datetime.datetime.strptime(to_date,   "%Y-%m-%d").date()
    if start > end:
        return []
    chunk_days = 360
    ranges = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + datetime.timedelta(days=chunk_days), end)
        ranges.append((cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cur = chunk_end + datetime.timedelta(days=1)

    all_candles: list = []
    for r in ranges:
        all_candles.extend(_fyers_history_symbol_chunk(symbol, "D", r[0], r[1]))
        time.sleep(0.15)   # Fyers rate-limit ke liye halka sa gap
    return _normalize_daily_candles(all_candles)

def _nifty500_incremental_update(max_workers: int = 4) -> tuple[bool, str]:
    """Nifty500 ke SAARE stocks ka daily data — sirf MISSING/naye candles
    fetch karke HF `/data/sv3_cache/symbols/` disk-cache (per-symbol JSON
    files) par likhta hai. Poora rebuild NAHI karta (bahut slow/rate-limit-heavy hota),
    sirf har symbol ke last-available-date ke baad ka gap fill hota hai.

    Naye symbols (jinke abhi disk par koi file hi nahi) ke liye poori
    30-saal history fetch hoti hai (_fetch_symbol_full_daily) — ye sirf
    tab hoga jab NSE ki live list mein koi bilkul naya stock aaya ho,
    isliye rare hai.

    Fyers login zaroori hai (BankNifty jaisa hi)."""
    if not is_session_active(update_cache=False):
        return False, "Fyers login/token valid nahi hai — Nifty500 auto-update skip."

    symbols = _fetch_nifty500_symbols()
    if not symbols:
        return False, "Nifty500 symbol list nahi mil paayi (HF disk _index.json) — auto-update skip."

    # Step 3C: pehle yahan SAARE symbols RAM me ({symbol: rows}) padhe jaate the.
    # Ab har symbol apni `.bin` ka sirf header (last_t) padhta hai; naye candles
    # fetch hote hain aur `.bin` ke end me append hote hain.
    g = _bin_append_guard()
    if g:
        return False, g
    today_str = _ist_now().strftime("%Y-%m-%d")
    _now_ist = _ist_now()
    _today_open_ist = _now_ist.hour * 60 + _now_ist.minute < 15 * 60 + 30   # 15:30 se pehle aaj ka candle adhoora

    def _one(sym):
        _rows_last_t = _sv3_last_t(sym)
        try:
            if _rows_last_t is not None:
                last_date = datetime.datetime.utcfromtimestamp(_rows_last_t).date()   # IST-naive epoch
                from_d = (last_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                if from_d > today_str:
                    return sym, None, "up-to-date", _rows_last_t
                new_candles = _fetch_symbol_incremental_daily(sym, from_d, today_str)
            else:
                new_candles = _fetch_symbol_full_daily(sym, years=30)
            if new_candles and _today_open_ist:
                _today_naive_date = _now_ist.date()
                new_candles = [r for r in new_candles
                               if datetime.datetime.utcfromtimestamp(r["t"]).date() < _today_naive_date]   # aaj ka adhoora candle chhodo
            if not new_candles:
                return sym, None, "no-new", _rows_last_t
            return sym, new_candles, "updated", new_candles[-1]["t"]
        except Exception as e:
            return sym, None, f"error: {e}", _rows_last_t

    updated, failed, errored = 0, 0, 0
    _max_last_t = None
    _touched = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for sym, new_candles, status, sym_last_t in ex.map(_one, symbols):
            if status == "updated" and new_candles is not None:
                try:
                    _sv3_bin_apply(sym, new_candles, None)
                    _touched.append(sym)
                    updated += 1
                except Exception as _e_w:
                    _slog(f"SV3 .bin write fail {sym}: {_e_w}", level="err")
                    failed += 1
            elif status.startswith("error"):
                errored += 1
            if sym_last_t is not None and (_max_last_t is None or sym_last_t > _max_last_t):
                _max_last_t = sym_last_t

    # Underlying data badla — single-slot cache saaf, manifest mark.
    _sv3_after_update(_touched)

    _nifty_last_date_str = (str(datetime.datetime.utcfromtimestamp(_max_last_t).date())
                             if _max_last_t is not None else "n/a (koi symbol data nahi mila)")
    msg = (f"Nifty500 incremental update: {updated}/{len(symbols)} symbols updated, "
           f"{failed} disk-write-fail, {errored} fetch-error. "
           f"Latest candle date (across all symbols): {_nifty_last_date_str}.")
    return True, msg

## ── IST-naive timestamp constants ───────────────────────────────────────────
## .gz data mein timestamps IST-naive hain: 9:15 IST ko 09:15 UTC ki tarah
## store kiya gaya hai. LightweightCharts real UTC chahta hai (IST timezone ke
## sath display karta hai: UTC + 5:30). Fix: har output time se 19800 subtract karo.
_IST_NAIVE_OFFSET = 19800   # 5.5 * 3600 — IST-naive → real UTC conversion
_SESSION_START    = 33300   # 9:15 IST = 9*3600 + 15*60 seconds from midnight
_SESSION_END      = 55800   # 15:30 IST = 15*3600 + 30*60 seconds from midnight

def _sv2_fill_bn_gaps(rows: list) -> list:
    """BN 5m raw data mein missing 5-min slots (no-trade gaps) forward-fill karo.

    Vendor ka 5m data kai jagah beech mein slots miss karta hai (illiquid /
    no-trade moments). Agar poore 125m/etc bucket ke saare 5m-slots missing
    hon, to us bucket ka candle hi resample output se gayab ho jaata hai —
    chart mein genuine "candles ke beech gap" dikhta hai. Fix: har trading
    session (9:15–15:29:XX IST, 375 minutes/day = 75 slots of 5m) ke liye
    poori 5-min sequence banao — jo slot missing ho use pichle available
    close se flat candle (o=h=l=c=prev_close) se bhar do. Isse koi bhi
    downstream resample bucket kabhi khaali nahi rahega.

    NOTE: raw .gz ab 5m granularity hai (pehle 1m thi) — isliye step 300
    seconds (5 min) hai, 60 seconds (1 min) nahi.
    """
    if not rows:
        return rows
    by_day: dict = {}
    for r in rows:
        t   = r["t"]
        mod = t % 86400
        if mod < _SESSION_START or mod >= _SESSION_END:
            continue
        day_start = t - mod
        by_day.setdefault(day_start, {})[t] = r

    out = []
    prev_close = None
    for day_start in sorted(by_day.keys()):
        day_rows = by_day[day_start]
        for sec_off in range(_SESSION_START, _SESSION_END, 300):
            t = day_start + sec_off
            if t in day_rows:
                r = day_rows[t]
                out.append(r)
                prev_close = r["c"]
            elif prev_close is not None:
                out.append({"t": t, "o": prev_close, "h": prev_close,
                            "l": prev_close, "c": prev_close})
            # agar dataset ke bilkul shuru mein hi pehla slot missing ho
            # (prev_close abhi None hai), to use silently skip karo — us
            # point tak koi reference close available hi nahi hai.
    return out

def _sv2_resample_bn_intraday(rows: list, tf_min: int) -> list:
    """BN 5m data ko intraday TF mein resample karo.

    .gz timestamps IST-naive hain (9:15 IST stored as 09:15 UTC epoch).
    Per-day anchor: har din 9:15 IST se bucket 0 start hota hai.
    Output timestamps real UTC mein (LightweightCharts + IST timezone ke liye).
    Session filter: sirf 9:15–15:30 IST ke candles.

    NOTE: raw .gz ab 5m granularity hai (pehle 1m thi). Isliye passthrough
    (bina bucketing) sirf tf_min<=5 par hota hai — 125m ab yahin se actual
    bucket-resample hoke banta hai (375-min session / 125m = 3 buckets/din).
    """
    sec = tf_min * 60
    if tf_min <= 5:
        out = []
        for r in rows:
            mod = r["t"] % 86400
            if mod < _SESSION_START or mod >= _SESSION_END:
                continue
            out.append({"time": r["t"] - _IST_NAIVE_OFFSET,
                        "open": r["o"], "high": r["h"],
                        "low":  r["l"], "close": r["c"]})
        return out

    buckets: dict = {}
    for r in rows:
        t       = r["t"]
        mod     = t % 86400                        # seconds since IST midnight
        if mod < _SESSION_START or mod >= _SESSION_END:
            continue
        day_start   = t - mod                      # IST-naive midnight of this day
        since_open  = mod - _SESSION_START         # seconds elapsed since 9:15 IST
        bucket_idx  = since_open // sec            # which bucket (0-based per day)
        bucket_sec  = _SESSION_START + bucket_idx * sec  # seconds from midnight
        key_utc     = (day_start + bucket_sec) - _IST_NAIVE_OFFSET  # real UTC

        if key_utc not in buckets:
            buckets[key_utc] = {"time": key_utc,
                                "open": r["o"], "high": r["h"],
                                "low":  r["l"], "close": r["c"]}
        else:
            b = buckets[key_utc]
            b["high"]  = max(b["high"],  r["h"])
            b["low"]   = min(b["low"],   r["l"])
            b["close"] = r["c"]
    return sorted(buckets.values(), key=lambda x: x["time"])

def _sv2_resample_bn_daily(rows: list, n_days: int = 1) -> list:
    """BN 1m data ko daily / multi-day candles mein resample karo.

    Har trading day ka open = 9:15 IST (real UTC: 3:45 AM = 13500s from UTC midnight).
    .gz timestamps IST-naive hain — 19800 subtract karo real UTC ke liye.
    """
    day_buckets: dict = {}
    for r in rows:
        t   = r["t"]
        mod = t % 86400
        if mod < _SESSION_START or mod >= _SESSION_END:
            continue
        day_start = t - mod                              # IST-naive midnight
        key_utc   = (day_start + _SESSION_START) - _IST_NAIVE_OFFSET  # 3:45 UTC

        if key_utc not in day_buckets:
            day_buckets[key_utc] = {"time": key_utc,
                                    "open": r["o"], "high": r["h"],
                                    "low":  r["l"], "close": r["c"]}
        else:
            b = day_buckets[key_utc]
            b["high"]  = max(b["high"],  r["h"])
            b["low"]   = min(b["low"],   r["l"])
            b["close"] = r["c"]

    days = sorted(day_buckets.values(), key=lambda x: x["time"])
    if n_days <= 1:
        return days

    out = []
    for i in range(0, len(days), n_days):
        chunk = days[i:i + n_days]
        if not chunk:
            break
        out.append({
            "time":  chunk[0]["time"],
            "open":  chunk[0]["open"],
            "high":  max(c["high"] for c in chunk),
            "low":   min(c["low"]  for c in chunk),
            "close": chunk[-1]["close"],
        })
    return out

def _sv2_resample_btc(rows: list, tf_min: int) -> list:
    """BTC 5m data ko UTC-anchored TF mein resample karo (24/7 crypto).

    NOTE: sirf 8H (intraday, tf_min < 1440) ke liye use karo (160m band kar
    diya gaya hai). Daily+
    (1D/3D/9D/27D) ke liye _sv2_resample_btc_daily() use karo — wo epoch
    (1970) anchor ki jagah data ke apne Day-1 se index-based chunking
    karta hai, jisse 3D/9D/27D hamesha same date se sync start hote hain.

    NOTE: raw .gz 5m granularity hai (BankNifty 1m par hai, BTC 5m par
    wapas revert kar diya gaya hai). Isliye passthrough (bina bucketing)
    tf_min<=5 par hota hai.
    """
    if tf_min <= 5:
        return [{"time": r["t"], "open": r["o"], "high": r["h"],
                 "low": r["l"], "close": r["c"]} for r in rows]
    sec = tf_min * 60
    buckets: dict = {}
    for r in rows:
        key = (r["t"] // sec) * sec
        if key not in buckets:
            buckets[key] = {"time": key, "open": r["o"], "high": r["h"],
                            "low": r["l"], "close": r["c"]}
        else:
            b = buckets[key]
            b["high"]  = max(b["high"],  r["h"])
            b["low"]   = min(b["low"],   r["l"])
            b["close"] = r["c"]
    return sorted(buckets.values(), key=lambda x: x["time"])

def _sv2_resample_btc_daily(rows: list, n_days: int = 1) -> list:
    """BTC 5m data ko daily / multi-day candles mein resample karo.

    Crypto 24/7 hai (koi session/weekday filter nahi) — sirf UTC
    calendar-day buckets banao, phir un dailies ko INDEX se (BN ke
    _sv2_resample_bn_daily jaisa: array index-0 = data ka pehla din)
    groups of n_days mein chunk karo.

    Ye zaroori hai kyunki purana _sv2_resample_btc() epoch (1 Jan 1970)
    se seedha `(t // (n_days*86400)) * (n_days*86400)` karta tha — us
    approach mein 3D/9D/27D ke cycle-boundaries data-start (2017) se
    alag-alag remainder dete hain, isliye teeno TF alag-alag calendar
    dates se start hote the. Index-based chunking (yahan) sabko data ke
    Day-1 se hi sync rakhta hai — BN aur SV2 replay (_liveAggregateDailyPlus,
    jo already index-based hai) dono ke saath consistent.
    """
    day_buckets: dict = {}
    for r in rows:
        t = r["t"]
        day_start = (t // 86400) * 86400          # UTC calendar-day start
        if day_start not in day_buckets:
            day_buckets[day_start] = {"time": day_start,
                                       "open": r["o"], "high": r["h"],
                                       "low": r["l"], "close": r["c"]}
        else:
            b = day_buckets[day_start]
            b["high"]  = max(b["high"],  r["h"])
            b["low"]   = min(b["low"],   r["l"])
            b["close"] = r["c"]

    days = sorted(day_buckets.values(), key=lambda x: x["time"])
    if n_days <= 1:
        return days

    out = []
    for i in range(0, len(days), n_days):
        chunk = days[i:i + n_days]
        if not chunk:
            break
        out.append({
            "time":  chunk[0]["time"],
            "open":  chunk[0]["open"],
            "high":  max(c["high"] for c in chunk),
            "low":   min(c["low"]  for c in chunk),
            "close": chunk[-1]["close"],
        })
    return out

# ─── Mobile ke liye max candles per TF (chunked inject) ───────────────────────
# Ab BankNifty aur BTC ke liye ALAG-ALAG default hain (pehle "1D/3D/9D/27D"
# jaisi daily labels dono asset ke beech shared hoti thi — ab har asset ki
# apni settings hain). Ye "factory defaults" hain; asli effective values
# _sv2_get_max() se aati hain, jo isme user ke saved overrides (bottom-bar
# ke 📦 Chunk icon se set kiye gaye) merge karta hai.
_SV2_MAX_BN_DEFAULT = {
    "5m_raw": 12000,
}
_SV2_MAX_BTC_DEFAULT = {
    # (2026-09-21) Pehle sirf 5m_raw size-limited tha — 8H/1D/3D/9D/27D
    # poori FULL-HISTORY (untrimmed) jaate the, ye assume karke ki inki
    # candle-count kam hoti hai isliye hang nahi karega. Asal mein LIVE
    # path (_sv2_build_asset_tfs) mein
    # 5m_raw KHUD bhi kabhi trim nahi ho raha tha (wo trim sirf legacy/
    # dead `_build_sv2_data()` mein tha jo kahin call hi nahi ho raha) —
    # isliye BTC ka POORA ~2017 se ka 5m data (lakhon rows) ek hi JSON
    # blob mein calendar-date-select par browser ko jaata tha, yahi
    # mobile-hang ki asli wajah thi. Ab saare 6 TFs anchor-date ke
    # aas-paas bounded/sliced jaate hain (dekho _sv2_build_asset_tfs) —
    # SV1 (replay_symbols.py) jaisa hi "poori history nahi, ek bounded
    # window" pattern, bas grid-alignment ki zaroorat nahi (SV2 ke
    # 3D/9D/27D already FULL-history se ek-baar resample hoke cache ho
    # chuke hote hain — dekho neeche _sv2_build_asset_tfs docstring).
    "5m_raw": 64000,   # ~222 din — jo pehle se tha, ab bas SERVER-SIDE effective
    "8H":     2500,    # ~833 din
    "1D":     850,     # ~2.3 saal
    "3D":     300,     # ~2.5 saal
    "9D":     100,     # ~2.5 saal
    "27D":    35,      # ~2.6 saal
}
# Safe min/max bounds per label — user chahe jitna bhi likhe, isi range mein
# clamp ho jaayega (mobile hang / bahut kam data dono se bachne ke liye).
_SV2_MAX_BOUNDS = {
    "5m_raw": (500, 120000),
    "8H":     (200, 20000),
    "1D":     (100, 8000),
    "3D":     (50,  3000),
    "9D":     (20,  1200),
    "27D":    (10,  500),
}

SV2_CHUNK_SETTINGS_FILE = "sv2_chunk_settings.json"

def load_sv2_chunk_settings() -> dict:
    """Saved overrides load karo: {"bn": {...}, "btc": {...}}."""
    if os.path.exists(SV2_CHUNK_SETTINGS_FILE):
        try:
            with open(SV2_CHUNK_SETTINGS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_sv2_chunk_settings(d: dict):
    with open(SV2_CHUNK_SETTINGS_FILE, "w") as f:
        json.dump(d, f)

def _sv2_get_max(asset: str) -> dict:
    """Asset ('bn'/'btc') ki effective per-TF candle-count limits: factory
    default + user ke saved overrides (clamped to _SV2_MAX_BOUNDS). Session
    ke andar cache hota hai taaki har rerun pe file dobara na padhni pade."""
    cache_key = f"_sv2_max_eff_{asset}"
    if cache_key not in st.session_state:
        default = _SV2_MAX_BN_DEFAULT if asset == "bn" else _SV2_MAX_BTC_DEFAULT
        saved   = load_sv2_chunk_settings().get(asset, {})
        eff = dict(default)
        for k, v in saved.items():
            if k in eff:
                try:
                    lo, hi = _SV2_MAX_BOUNDS.get(k, (10, 200000))
                    eff[k] = max(lo, min(hi, int(v)))
                except Exception:
                    pass
        st.session_state[cache_key] = eff
    return st.session_state[cache_key]

def _sv2_trim(data: list, label: str, anchor_epoch: int = None, asset: str = "bn") -> list:
    """Chunk select karo (mobile hang prevention, per-asset per-TF limits).

    - anchor_epoch None  → purana default: sirf LAST N candles (recent data).
    - anchor_epoch diya  → us date ke aas-paas se window nikaalo: thoda
      history anchor se PEHLE ka (context ke liye) aur baaki (zyada hissa)
      anchor ke BAAD ka (kyunki replay ko aage badhne ke liye future candles
      chahiye) — total count wahi N jo _sv2_get_max(asset) se aata hai.
    """
    n = _sv2_get_max(asset).get(label, 2000)
    if len(data) <= n:
        return data
    # (2026-09-22) BTC 1D GRID-ALIGN FIX: client (chart.html:
    # _liveAggregateDailyPlus / _liveGetBucketTime) 9D/27D (aur 3D) replay
    # buckets SV2_BTC['1D'] ke index-0 se banata hai. Agar trimmed 1D ka
    # start FULL-history ke index-0 se 27 ke multiple par nahi hai, to
    # client ki grouping server ki pre-grouped 3D/9D/27D se shift ho jaati
    # hai. Isliye BTC 1D ka start hamesha 27 (= LCM of 1,3,9,27) ke multiple
    # index par snap karte hain — window <27 candles bada ho sakta hai.
    def _snap27(start_idx: int) -> int:
        if asset == "btc" and label == "1D":
            return max(0, start_idx - (start_idx % 27))
        return start_idx
    if anchor_epoch is None:
        return data[_snap27(len(data) - n):]
    # Binary search: pehla index jahan bar['time'] >= anchor_epoch
    lo, hi = 0, len(data)
    while lo < hi:
        mid = (lo + hi) // 2
        if data[mid]["time"] < anchor_epoch:
            lo = mid + 1
        else:
            hi = mid
    idx    = lo
    before = n // 4                       # ~25% budget anchor se pehle
    start  = max(0, idx - before)
    end    = min(len(data), start + n)
    start  = max(0, end - n)              # series ke end tak clip ho to peeche khiskao
    start  = _snap27(start)               # BTC 1D: 27-multiple grid par align
    return data[start:end]

# ═══════════════════════════════════════════════════════════════════════════
# 🧱 BIN BLOCK START — NO_PRELOAD_PLAN.md, STEP 1  (.bin store + converter)
# ═══════════════════════════════════════════════════════════════════════════
# (2026-10-02) Candle/OHLC data ke liye fixed-width, memory-mapped `.bin`
# format. Ye block STEP 1 hai — abhi koi bhi purana code `.bin` NAHI padhta,
# app ka behavior bilkul same hai. Is block me do cheezein hain:
#   (1) `.bin` STORE  — header/rows likhna, append, memmap se padhna,
#       binary-search window, manifest. (Ye PERMANENT hai — Step 2/3 me
#       app runtime par isi se candles padhegi.)
#   (2) CONVERTER     — purani .gz/.json files se ek-baar `/data/bin/` me
#       .bin copies banata hai (background thread, UI card se). Purani
#       files ko KABHI touch nahi karta (sirf padhta hai).
#
# FORMAT: 64-byte header + N rows x 40 bytes, little-endian.
#   row    = t int64 | o,h,l,c float64   (t source jaisa hi, koi shift nahi)
#   header = magic "MEBIN001" | version u32 | row_size u32 | row_count u64 |
#            first_t i64 | last_t i64 | tf_seconds u32 | flags u32 | pad->64
#   flags  = 0 raw/source rows, 1 derived TF rows (time/open/high/low/close)
# Rows hamesha t ke hisaab se STRICTLY increasing (duplicates: last wins).
#
# FOLDERS (/data/bin/):
#   _manifest.json                     — har dataset: source size+mtime, rows...
#   src/sv1_master_5m.bin              — /data/replay_cache/<master>.gz
#   src/sv2_btc_5m.bin, src/sv2_bn_5m.bin
#   sv2/btc_{5m_raw,8H,1D,3D,9D,27D}.bin, sv2/bn_{5m_raw,125m,1D,3D,9D,27D}.bin
#   sv3/symbols/<SYM>.bin              — /data/sv3_cache/symbols/<SYM>.json
#   td/td_<key>_5m.bin                 — /data/td_cache/td_<key>_5m.gz
try:
    import numpy as _np
except Exception:   # numpy na ho to converter button error dikhayega, app chalti rahegi
    _np = None
import struct as _struct
import random as _random
import gc as _gc

_BIN_MAGIC     = b"MEBIN001"
_BIN_VERSION   = 1
_BIN_HDR_SIZE  = 64
_BIN_HDR_FMT   = "<8sIIQqqII"
_BIN_ROW_SIZE  = 40
_BIN_DTYPE     = (_np.dtype([("t", "<i8"), ("o", "<f8"), ("h", "<f8"),
                             ("l", "<f8"), ("c", "<f8")])
                  if _np is not None else None)
_BIN_KEYS_SHORT = ("t", "o", "h", "l", "c")
_BIN_KEYS_LONG  = ("time", "open", "high", "low", "close")


# Process-wide singletons (plain module-level dict har Streamlit rerun par
# reset ho jaata hai — isliye st.cache_resource, jaise _GDRIVE_IMPORT_STATE).
@st.cache_resource
def _get_bin_locks() -> dict:
    return {"guard": threading.Lock(), "manifest": threading.Lock(), "files": {}}

@st.cache_resource
def _get_bin_state() -> dict:
    return {
        "running": False, "started_at": 0.0, "finished_at": 0.0,
        "ok": None,        # None = kabhi chala nahi, True/False = last result
        "done": 0, "total": 0, "current": "",
        "summary": "",     # BIN_CONVERT_SUMMARY ... line
        "log": [],         # poori progress log (UI expander ke liye)
    }

@st.cache_resource
def _get_bin_run_lock() -> threading.Lock:
    return threading.Lock()


def _bin_root() -> str:
    return os.path.join(_storage_root(), "bin")

def _bin_file_lock(path: str) -> threading.Lock:
    L = _get_bin_locks()
    with L["guard"]:
        lk = L["files"].get(path)
        if lk is None:
            lk = threading.Lock()
            L["files"][path] = lk
        return lk


# ── header ───────────────────────────────────────────────────────────────
def _bin_pack_header(row_count: int, first_t: int, last_t: int,
                     tf_seconds: int = 0, flags: int = 0) -> bytes:
    h = _struct.pack(_BIN_HDR_FMT, _BIN_MAGIC, _BIN_VERSION, _BIN_ROW_SIZE,
                     int(row_count), int(first_t), int(last_t),
                     int(tf_seconds), int(flags))
    return h + b"\x00" * (_BIN_HDR_SIZE - len(h))

def _bin_read_header(path: str) -> dict:
    """Header padhta hai. Corrupt/chhoti/truncated file par ValueError."""
    with open(path, "rb") as f:
        raw = f.read(_BIN_HDR_SIZE)
    if len(raw) < _BIN_HDR_SIZE:
        raise ValueError("header chhota hai")
    magic, ver, rsz, rc, ft, lt, tf, fl = _struct.unpack(
        _BIN_HDR_FMT, raw[:_struct.calcsize(_BIN_HDR_FMT)])
    if magic != _BIN_MAGIC:
        raise ValueError("magic galat (MEBIN001 nahi)")
    if ver != _BIN_VERSION or rsz != _BIN_ROW_SIZE:
        raise ValueError(f"version/row_size mismatch (v{ver}, row {rsz})")
    if os.path.getsize(path) < _BIN_HDR_SIZE + rc * _BIN_ROW_SIZE:
        raise ValueError("file truncated (row_count se chhoti)")
    return {"row_count": rc, "first_t": ft, "last_t": lt,
            "tf_seconds": tf, "flags": fl}


# ── write / append ───────────────────────────────────────────────────────
def _bin_is_strictly_increasing(arr) -> bool:
    if len(arr) < 2:
        return True
    t = arr["t"]
    return bool(_np.all(t[1:] > t[:-1]))

def _bin_write_new(path: str, arr, tf_seconds: int = 0, flags: int = 0) -> None:
    """Poori file ATOMICALLY likhta hai: tmp -> fsync -> os.replace. `arr`
    strictly increasing hona chahiye (warna ValueError)."""
    arr = _np.ascontiguousarray(arr, dtype=_BIN_DTYPE)
    if not _bin_is_strictly_increasing(arr):
        raise ValueError("rows strictly increasing nahi hain")
    n = len(arr)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with _bin_file_lock(path):
        with open(tmp, "wb") as f:
            f.write(_bin_pack_header(n, int(arr["t"][0]) if n else 0,
                                    int(arr["t"][-1]) if n else 0,
                                    tf_seconds, flags))
            if n:
                arr.tofile(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

def _bin_append_rows(path: str, new_arr) -> int:
    """Sirf END me naye rows jodta hai. Pehla naya t > last_t hona chahiye.
    Order: rows likho -> fsync -> TAB header (row_count/last_t) update -> fsync.
    Beech me crash ho to header purana rehta hai, reader ko adhoori
    append kabhi dikhti hi nahi (torn-write safe). Returns appended count."""
    new = _np.ascontiguousarray(new_arr, dtype=_BIN_DTYPE)
    n = len(new)
    if n == 0:
        return 0
    if not _bin_is_strictly_increasing(new):
        raise ValueError("naye rows strictly increasing nahi hain")
    with _bin_file_lock(path):
        h = _bin_read_header(path)
        rc = h["row_count"]
        if rc and int(new["t"][0]) <= h["last_t"]:
            raise ValueError(f"naya pehla t ({int(new['t'][0])}) <= last_t ({h['last_t']})")
        with open(path, "r+b") as f:
            f.truncate(_BIN_HDR_SIZE + rc * _BIN_ROW_SIZE)   # purani adhoori tail hatao
            f.seek(0, os.SEEK_END)
            new.tofile(f)
            f.flush()
            os.fsync(f.fileno())
            f.seek(0)
            f.write(_bin_pack_header(rc + n,
                                     h["first_t"] if rc else int(new["t"][0]),
                                     int(new["t"][-1]),
                                     h["tf_seconds"], h["flags"]))
            f.flush()
            os.fsync(f.fileno())
    return n


# ── read (memmap, koi parsing nahi) ──────────────────────────────────────
def _bin_open_rows(path: str):
    """Read-only np.memmap (structured dtype). 0 rows -> khaali array.
    Sirf header trust hota hai: row_count se aage ke bytes ignore."""
    h = _bin_read_header(path)
    rc = h["row_count"]
    if rc == 0:
        return _np.empty(0, dtype=_BIN_DTYPE)
    return _np.memmap(path, dtype=_BIN_DTYPE, mode="r",
                      offset=_BIN_HDR_SIZE, shape=(rc,))

def _bin_lower_bound(arr, x: int, right: bool = False) -> int:
    """Manual binary search — `t` column ke sirf ~20 pages touch hote hain
    (np.searchsorted strided column ki copy bana sakta tha = poori file
    padhna). right=False: pehla i jahan t >= x. right=True: pehla i jahan t > x."""
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        v = int(arr[mid]["t"])
        if (v <= x) if right else (v < x):
            lo = mid + 1
        else:
            hi = mid
    return lo

def _bin_index_range(arr, t_from=None, t_to=None):
    """(i0, i1) — dono t_from/t_to INCLUSIVE; None = open. arr[i0:i1]."""
    i0 = 0 if t_from is None else _bin_lower_bound(arr, int(t_from), right=False)
    i1 = len(arr) if t_to is None else _bin_lower_bound(arr, int(t_to), right=True)
    return i0, max(i0, i1)

def _bin_window(arr, t_from=None, t_to=None):
    i0, i1 = _bin_index_range(arr, t_from, t_to)
    return arr[i0:i1]   # view, copy nahi

def _bin_rows_to_dicts(win, long_keys: bool = False) -> list:
    """Sirf diye gaye (chhote) window ko list-of-dict me badalta hai —
    purane JSON shape jaisa (t/o/h/l/c ya time/open/high/low/close)."""
    ks = _BIN_KEYS_LONG if long_keys else _BIN_KEYS_SHORT
    k0, k1, k2, k3, k4 = ks
    return [{k0: t, k1: o, k2: h, k3: l, k4: c} for (t, o, h, l, c) in win.tolist()]


# ── list-of-dict  ->  numpy array ───────────────────────────────────────
def _bin_rows_to_array(rows: list, keys=_BIN_KEYS_SHORT):
    """Returns (arr, stats). Kharab rows (key missing / None / non-numeric /
    NaN / Inf / fractional t) skip hote hain aur stats["bad"] me gine jaate
    hain. Source me jo EXTRA keys hain (jaise volume) unke naam
    stats["extra"] me aate hain — kuch chupke se nahi khota."""
    k0, k1, k2, k3, k4 = keys
    isf = math.isfinite
    ts, os_, hs, ls, cs = [], [], [], [], []
    bad = 0
    extra: set = set()
    want = set(keys)
    for i, r in enumerate(rows):
        try:
            if i < 2000 and isinstance(r, dict):
                extra |= (set(r.keys()) - want)
            t = r[k0]
            ti = int(t)
            if ti != t:
                raise ValueError("t fractional/non-numeric")
            o = float(r[k1]); h = float(r[k2]); l = float(r[k3]); c = float(r[k4])
            if not (isf(o) and isf(h) and isf(l) and isf(c)):
                raise ValueError("non-finite price")
        except Exception:
            bad += 1
            continue
        ts.append(ti); os_.append(o); hs.append(h); ls.append(l); cs.append(c)
    arr = _np.empty(len(ts), dtype=_BIN_DTYPE)
    if len(ts):
        arr["t"] = ts; arr["o"] = os_; arr["h"] = hs; arr["l"] = ls; arr["c"] = cs
    return arr, {"bad": bad, "extra": extra}

def _bin_normalize_sorted(arr):
    """Strictly increasing banao: pehle stable sort, phir duplicate t me LAST
    wins (purane merge jaisa). Returns (arr, {"disordered": bool, "dups": n})."""
    n = len(arr)
    if n < 2 or _bin_is_strictly_increasing(arr):
        return arr, {"disordered": False, "dups": 0}
    t = arr["t"]
    disordered = not bool(_np.all(t[1:] >= t[:-1]))
    if disordered:
        arr = arr[_np.argsort(arr["t"], kind="stable")]
        t = arr["t"]
    keep = _np.append(t[1:] != t[:-1], True)     # run ka aakhri (= last-wins)
    dups = int(n - int(keep.sum()))
    return arr[keep], {"disordered": disordered, "dups": dups}

def _bin_verify_file(path: str):
    """(ok, msg) — header sahi, rows strictly increasing, first/last t match."""
    try:
        h = _bin_read_header(path)
        arr = _bin_open_rows(path)
        if len(arr) != h["row_count"]:
            return False, "row_count mismatch"
        if len(arr) and (int(arr[0]["t"]) != h["first_t"] or int(arr[-1]["t"]) != h["last_t"]):
            return False, "first_t/last_t header se match nahi"
        if not _bin_is_strictly_increasing(arr):
            return False, "t strictly increasing nahi"
        return True, f"ok rows={h['row_count']}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── manifest ─────────────────────────────────────────────────────────────
def _bin_manifest_path() -> str:
    return os.path.join(_bin_root(), "_manifest.json")

def _bin_manifest_load() -> dict:
    try:
        with open(_bin_manifest_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("datasets"), dict):
            return d
    except Exception:
        pass
    return {"version": 1, "datasets": {}}

def _bin_manifest_save(d: dict) -> None:
    path = _bin_manifest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with _get_bin_locks()["manifest"]:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


# ═══════════════════════════ CONVERTER (one-time) ═══════════════════════
_BIN_SV2_TF_SECONDS = {
    "5m_raw": 300, "8H": 28800, "125m": 7500, "1D": 86400,
    "3D": 259200, "9D": 777600, "27D": 2332800,
}

def _bin_log(msg: str, level: str = "info", to_slog: bool = True) -> None:
    st_ = _get_bin_state()
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    st_["log"].append(line)
    if len(st_["log"]) > 4000:
        del st_["log"][:len(st_["log"]) - 4000]
    if to_slog:
        _slog(f"[BinConvert] {msg}", level=level)

def _bin_src_sig(path: str) -> dict:
    s = os.stat(path)
    return {"path": path, "size": int(s.st_size), "mtime_ns": int(s.st_mtime_ns)}

def _bin_load_json_rows(path: str) -> list:
    """Source .gz ya .json (plain list ya {"data": [...]}) -> rows list.
    Sirf PADHTA hai."""
    if path.endswith(".gz"):
        with _gzip.open(path, "rb") as f:
            data = json.load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    if isinstance(data, dict):
        data = data.get("data", []) or []
    return data if isinstance(data, list) else []

def _bin_uptodate(man: dict, name: str, dest: str, sig: dict) -> bool:
    e = man["datasets"].get(name)
    if not e or e.get("source") != sig or not os.path.isfile(dest):
        return False
    try:
        h = _bin_read_header(dest)
        return h["row_count"] == e.get("rows")
    except Exception:
        return False   # corrupt .bin -> dobara banega

def _bin_write_and_verify(dest: str, rows: list, keys, tf_seconds: int,
                          flags: int) -> dict:
    """rows -> normalize -> atomic write -> verify. Returns manifest-entry
    (source baad me judta hai). Verify fail par exception."""
    arr, st_ = _bin_rows_to_array(rows, keys)
    arr, info = _bin_normalize_sorted(arr)
    warns = []
    if st_["bad"]:
        warns.append(f"{st_['bad']} kharab row skip")
    if st_["extra"]:
        warns.append(f"extra keys ignore: {sorted(st_['extra'])}")
    if info["disordered"]:
        warns.append("rows out-of-order the (sort kiya)")
    if info["dups"]:
        warns.append(f"{info['dups']} duplicate t (last wins)")
    if len(arr) == 0:
        warns.append("0 rows (khaali source)")
    _bin_write_new(dest, arr, tf_seconds, flags)

    ok, msg = _bin_verify_file(dest)
    if not ok:
        raise RuntimeError(f"verify fail: {msg}")
    n = len(arr)
    back = _bin_open_rows(dest)
    if n:
        if len(back) != n or not _np.array_equal(back.view(_np.uint8), arr.view(_np.uint8)):
            raise RuntimeError("disk se wapas padha hua data likhe hue se byte-identical nahi")
    clean = (not warns) and len(rows) == n
    if clean and n:
        k0, k1, k2, k3, k4 = keys
        for i in _random.sample(range(n), min(2000, n)):
            r, a = rows[i], back[i]
            if (int(r[k0]) != int(a["t"]) or float(r[k1]) != float(a["o"])
                    or float(r[k2]) != float(a["h"]) or float(r[k3]) != float(a["l"])
                    or float(r[k4]) != float(a["c"])):
                raise RuntimeError(f"sample row {i} source se match nahi")
    entry = {
        "rows": n,
        "first_t": int(arr["t"][0]) if n else 0,
        "last_t":  int(arr["t"][-1]) if n else 0,
        "tf_seconds": int(tf_seconds), "flags": int(flags),
        "bytes": _BIN_HDR_SIZE + n * _BIN_ROW_SIZE,
        "verify": "ok" + ("" if clean else " (warnings)"),
        "warnings": warns,
        "converted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    del back
    return entry


def _bin_sv2_builders(asset: str, rows: list) -> list:
    """[(label, lambda -> list-of-dict)] — PURANE `_sv2_build_asset_tfs` jaise
    HI functions (isliye output identical). BN: pehle _sv2_fill_bn_gaps."""
    if asset == "bn":
        bn = _sv2_fill_bn_gaps(rows)
        return [
            ("5m_raw", lambda: _sv2_resample_bn_intraday(bn, 5)),
            ("125m",   lambda: _sv2_resample_bn_intraday(bn, 125)),
            ("1D",     lambda: _sv2_resample_bn_daily(bn, 1)),
            ("3D",     lambda: _sv2_resample_bn_daily(bn, 3)),
            ("9D",     lambda: _sv2_resample_bn_daily(bn, 9)),
            ("27D",    lambda: _sv2_resample_bn_daily(bn, 27)),
        ]
    return [
        ("5m_raw", lambda: _sv2_resample_btc(rows, 5)),
        ("8H",     lambda: _sv2_resample_btc(rows, 480)),
        ("1D",     lambda: _sv2_resample_btc_daily(rows, 1)),
        ("3D",     lambda: _sv2_resample_btc_daily(rows, 3)),
        ("9D",     lambda: _sv2_resample_btc_daily(rows, 9)),
        ("27D",    lambda: _sv2_resample_btc_daily(rows, 27)),
    ]


def _bin_run(force: bool = False) -> dict:
    """Poora conversion (synchronous). Background thread isi ko chalata hai.
    Returns summary dict; `summary["line"]` = BIN_CONVERT_SUMMARY text."""
    t0 = time.time()
    root = _bin_root()
    os.makedirs(root, exist_ok=True)
    man = _bin_manifest_load()
    cnt = {"ok": 0, "skipped": 0, "failed": 0, "missing": 0, "rows": 0, "warned": 0}
    stt = _get_bin_state()
    src_before: dict = {}      # path -> (size, mtime_ns)

    # ── kaam ki list (kaun-si source file -> kaun-se datasets) ──────────
    sv1_path = _replay._MASTER_DISK_PATH
    btc_path = os.path.join(_SV2_CACHE_DIR, _SV2_BTC_FILENAME)
    bn_path  = os.path.join(_SV2_CACHE_DIR, _SV2_BN_FILENAME)
    td_files, sv3_files = [], []
    try:
        td_files = sorted(f for f in os.listdir(_td._TD_DIR)
                          if f.startswith("td_") and f.endswith("_5m.gz"))
    except Exception:
        pass
    try:
        sv3_files = sorted(f for f in os.listdir(_SV3_SYMBOLS_DIR)
                           if f.endswith(".json") and not f.startswith("_"))
    except Exception:
        pass
    stt["total"] = 1 + 7 + 7 + len(td_files) + len(sv3_files)
    stt["done"] = 0
    _bin_log(f"Shuru — force={force} | root={root} | TD files={len(td_files)} | "
             f"SV3 files={len(sv3_files)}")

    def _tick(label: str):
        stt["done"] += 1
        stt["current"] = label

    def _flush_manifest():
        try:
            _bin_manifest_save(man)
        except Exception as e:
            _bin_log(f"manifest save fail: {e}", "err")

    def _sig_or_none(path: str, label: str):
        if not os.path.isfile(path):
            return None
        sg = _bin_src_sig(path)
        src_before[path] = (sg["size"], sg["mtime_ns"])
        return sg

    def _record(name: str, entry: dict, sig: dict):
        entry = dict(entry)
        entry["source"] = sig
        man["datasets"][name] = entry
        cnt["ok"] += 1
        cnt["rows"] += entry["rows"]
        if entry["warnings"]:
            cnt["warned"] += 1
            _bin_log(f"⚠️ {name}: " + "; ".join(entry["warnings"]), "warn")

    def _fail(name: str, e: Exception):
        cnt["failed"] += 1
        _bin_log(f"❌ {name}: {type(e).__name__}: {e}", "err")

    # ── (A) asset-type sources: SV1 master, SV2 BTC, SV2 BN ──────────────
    assets = [
        # (label, src_path, src_name, src_dest, derived: None | "btc" | "bn")
        ("SV1 master", sv1_path, "sv1_master", os.path.join(root, "src", "sv1_master_5m.bin"), None),
        ("SV2 BTC", btc_path, "sv2_btc_src", os.path.join(root, "src", "sv2_btc_5m.bin"), "btc"),
        ("SV2 BN",  bn_path,  "sv2_bn_src",  os.path.join(root, "src", "sv2_bn_5m.bin"),  "bn"),
    ]
    for label, path, src_name, src_dest, derived in assets:
        n_ds = 1 + (6 if derived else 0)
        sig = _sig_or_none(path, label)
        if sig is None:
            cnt["missing"] += n_ds
            stt["done"] += n_ds
            _bin_log(f"{label}: source file nahi mili ({path}) — skip", "warn")
            continue
        # derived ke naam/dest
        plan = [(src_name, src_dest, None)]
        if derived:
            pre = "btc" if derived == "btc" else "bn"
            labels = (["5m_raw", "8H", "1D", "3D", "9D", "27D"] if derived == "btc"
                      else ["5m_raw", "125m", "1D", "3D", "9D", "27D"])
            for lb in labels:
                plan.append((f"sv2_{pre}_{lb}",
                             os.path.join(root, "sv2", f"{pre}_{lb}.bin"), lb))
        if (man["datasets"].get(src_name) or {}).get("diverged"):
            # Step 2 ke baad is asset me `.bin` me append ho chuka hai => `.bin` purani
            # gz se AAGE hai. Overwrite karte to appended data chala jaata (force me bhi).
            cnt["skipped"] += len(plan)
            stt["done"] += len(plan)
            _bin_log(f"{label}: .bin gz se aage hai (append ho chuka) — overwrite NAHI karunga, skip", "warn")
            continue
        todo = [p for p in plan if force or not _bin_uptodate(man, p[0], p[1], sig)]
        cnt["skipped"] += len(plan) - len(todo)
        stt["done"] += len(plan) - len(todo)
        if not todo:
            _bin_log(f"{label}: sab kuch up-to-date — skip")
            continue
        _bin_log(f"{label}: source padh raha hoon — {path} ({sig['size']/1e6:.1f} MB)")
        try:
            rows = _bin_load_json_rows(path)
        except Exception as e:
            for nm, _d, _lb in todo:
                _fail(nm, e)
                _tick(nm)
            continue
        _bin_log(f"{label}: {len(rows):,} rows load hui")
        builders = None
        for nm, dest, lb in todo:
            stt["current"] = nm
            try:
                if lb is None:
                    entry = _bin_write_and_verify(dest, rows, _BIN_KEYS_SHORT,
                                                  300, 0)
                else:
                    if builders is None:
                        builders = dict(_bin_sv2_builders(derived, rows))
                    out = builders[lb]()
                    entry = _bin_write_and_verify(dest, out, _BIN_KEYS_LONG,
                                                  _BIN_SV2_TF_SECONDS[lb], 1)
                    del out
                _record(nm, entry, sig)
                _bin_log(f"✅ {nm}: {entry['rows']:,} rows -> {dest}", "ok")
            except Exception as e:
                _fail(nm, e)
            _tick(nm)
        del rows, builders
        _gc.collect()
        _flush_manifest()

    # ── (B) Twelve-Data symbols ──────────────────────────────────────────
    for fn in td_files:
        path = os.path.join(_td._TD_DIR, fn)
        key = fn[3:-len("_5m.gz")]
        nm = f"td_{key}"
        dest = os.path.join(root, "td", f"td_{key}_5m.bin")
        try:
            sig = _sig_or_none(path, nm)
            if sig is None:
                cnt["missing"] += 1
            elif not force and _bin_uptodate(man, nm, dest, sig):
                cnt["skipped"] += 1
            else:
                rows = _bin_load_json_rows(path)
                entry = _bin_write_and_verify(dest, rows, _BIN_KEYS_SHORT, 300, 0)
                _record(nm, entry, sig)
                _bin_log(f"✅ {nm}: {entry['rows']:,} rows", "ok", to_slog=False)
                del rows
        except Exception as e:
            _fail(nm, e)
        _tick(nm)
    _flush_manifest()

    # ── (C) SV3 symbols (daily) ──────────────────────────────────────────
    for i, fn in enumerate(sv3_files, 1):
        path = os.path.join(_SV3_SYMBOLS_DIR, fn)
        stem = fn[:-len(".json")]
        nm = f"sv3_{stem}"
        dest = os.path.join(root, "sv3", "symbols", f"{stem}.bin")
        try:
            sig = _sig_or_none(path, nm)
            if sig is None:
                cnt["missing"] += 1
            elif (man["datasets"].get(nm) or {}).get("diverged"):
                cnt["skipped"] += 1      # Step 3C: .bin JSON se aage hai (append hua) — overwrite nahi
            elif not force and _bin_uptodate(man, nm, dest, sig):
                cnt["skipped"] += 1
            else:
                rows = _bin_load_json_rows(path)
                entry = _bin_write_and_verify(dest, rows, _BIN_KEYS_SHORT, 86400, 0)
                _record(nm, entry, sig)
                del rows
        except Exception as e:
            _fail(nm, e)
        _tick(nm)
        if i % 50 == 0:
            _bin_log(f"SV3: {i}/{len(sv3_files)} files", to_slog=False)
            _flush_manifest()
    _flush_manifest()

    # ── purani files touch nahi hui — double-check ───────────────────────
    untouched = True
    for p, (sz, mt) in src_before.items():
        try:
            s = os.stat(p)
            if int(s.st_size) != sz or int(s.st_mtime_ns) != mt:
                untouched = False
                _bin_log(f"❌ SOURCE BADAL GAYI: {p}", "err")
        except Exception:
            untouched = False
            _bin_log(f"❌ SOURCE GAYAB: {p}", "err")

    bin_bytes = 0
    for dp, _dn, fns in os.walk(root):
        for f in fns:
            if f.endswith(".bin"):
                try:
                    bin_bytes += os.path.getsize(os.path.join(dp, f))
                except Exception:
                    pass
    line = (f"BIN_CONVERT_SUMMARY ok={cnt['ok']} skipped={cnt['skipped']} "
            f"failed={cnt['failed']} missing={cnt['missing']} "
            f"warned={cnt['warned']} rows_written={cnt['rows']} "
            f"sources_untouched={untouched} bin_size_mb={bin_bytes/1e6:.1f} "
            f"elapsed_s={time.time()-t0:.1f}")
    _bin_log(line, "ok" if cnt["failed"] == 0 and untouched else "err")
    return {"line": line, "failed": cnt["failed"], "untouched": untouched, **cnt}


def _bin_convert_start(force: bool = False):
    """Converter ko background thread me start karta hai. (ok, msg)."""
    if _np is None:
        return False, "numpy install nahi hai — requirements.txt me `numpy` add karo."
    stt = _get_bin_state()
    lk = _get_bin_run_lock()
    with lk:
        if stt["running"]:
            return False, "Converter pehle se chal raha hai."
        stt.update(running=True, started_at=time.time(), finished_at=0.0,
                   ok=None, done=0, total=0, current="", summary="", log=[])

    def _worker():
        try:
            res = _bin_run(force)
            stt["summary"] = res["line"]
            stt["ok"] = (res["failed"] == 0 and res["untouched"])
        except Exception as e:
            import traceback as _tb
            stt["summary"] = f"BIN_CONVERT_SUMMARY CRASH {type(e).__name__}: {e}"
            stt["ok"] = False
            _bin_log("EXCEPTION converter: " + _tb.format_exc(), "err")
        finally:
            stt["finished_at"] = time.time()
            stt["running"] = False

    threading.Thread(target=_worker, daemon=True, name="bin-convert").start()
    return True, "Converter background me start ho gaya."

# ═══════════════════════════════════════════════════════════════════════════
# 🧱 BIN BLOCK END
# ═══════════════════════════════════════════════════════════════════════════

# (Step 2) env BIN_READ_FALLBACK (default "1"): `.bin` missing/kharab ho to
# request-time par purani gz se LAZY fallback. "0" => fallback band.
_BIN_READ_FALLBACK = os.environ.get("BIN_READ_FALLBACK", "1").strip() != "0"
# replay_symbols.py ko `.bin` root batao (local-dev me storage-root alag ho sakta hai).
try:
    _replay.set_bin_dir(_bin_root())
except Exception as _e_sbd:
    _slog(f"replay.set_bin_dir fail: {_e_sbd}", level="err")

# ─── Stack View 3: Nifty 500 stock — long-term MONTHLY replay ─────────────
# SV2 (BankNifty/BTC, days-based: 1D→3D→9D→27D) ka hi index-chunk resample
# pattern hai, bas ek DYNAMIC stock symbol par aur MONTHS ki unit mein:
# 1D (already nifty500 .gz mein hai) → calendar-month buckets (1M, internal
# reference table — user ko TF ke roop mein nahi dikhta) → phir index-chunk
# karke 3M/9M/27M/81M/243M (SV2 ke 1D→3D→9D→27D jaisa hi, Month-1 se hamesha
# sync, calendar-quarter/financial-year se independent).
SV3_LAST_SYMBOL_FILE = "sv3_last_symbol.json"
_SV3_CACHE: dict = {}   # single-slot in-memory cache: {"symbol":…, "data":{…}}
SV3_TF_MONTHS = {"3M": 3, "9M": 9, "27M": 27, "81M": 81, "243M": 243}

# ─── Cache stats / clear — top-header 🗄 icon (chart.html) ke liye ─────────
# NOTE: Yahan JAAN-BOOJHKAR sirf 5 store liye gaye hain — wo jo PURELY
# performance-cache hain (TTL ke andar dobara fetch skip karte hain, koi
# unique state nahi rakhte). Live-state singletons (WS
# connection state, thread heartbeats) aur disk par persist
# hone wala historical data (Nifty500/BTC SV3 .json files — authoritative
# store hain) is list mein NAHI hain — unhe clear
# karna live feed/WS todega ya real data delete karega, jo "cache clear"
# se expect nahi hota. Isliye clear karne ke baad bhi app turant kaam karti
# rehti hai — bas agla depth/option-chain/balance fetch ek baar fresh jayega.
def _cache_stats() -> dict:
    """Har clearable in-memory cache ka approx size (KB) + item-count."""
    def _kb(obj) -> float:
        try:
            return len(json.dumps(obj, default=str).encode("utf-8")) / 1024.0
        except Exception:
            return 0.0
    try:
        with _DEPTH_LOCK:
            _depth_snap = dict(_DEPTH_CACHE)
    except Exception:
        _depth_snap = {}
    try:
        with _OC_LOCK:
            _oc_snap = dict(_OC_CACHE)
    except Exception:
        _oc_snap = {}
    try:
        with _FYERS_META_LOCK:
            _fm_snap = dict(_FYERS_META_CACHE)
    except Exception:
        _fm_snap = {}
    _sv3_snap = dict(_SV3_CACHE)

    rows = [
        {"label": "Market Depth (order book)", "items": len(_depth_snap), "kb": round(_kb(_depth_snap), 1)},
        {"label": "Option Chain (Fyers)", "items": 1 if _oc_snap.get("data") is not None else 0, "kb": round(_kb(_oc_snap), 1)},
        {"label": "Fyers balance/strike", "items": 1 if _fm_snap.get("balance") is not None else 0, "kb": round(_kb(_fm_snap), 1)},
        {"label": "SV3 symbol data (RAM slot)", "items": 1 if _sv3_snap else 0, "kb": round(_kb(_sv3_snap), 1)},
    ]
    return {
        "total_kb": round(sum(r["kb"] for r in rows), 1),
        "total_items": sum(r["items"] for r in rows),
        "breakdown": rows,
        "ts": time.time(),
    }

def _clear_all_caches() -> dict:
    """Sirf upar wale 5 purely-performance caches clear karta hai — live
    WS/thread/proxy state ko haath nahi lagata."""
    before = _cache_stats()
    try:
        with _DEPTH_LOCK:
            _DEPTH_CACHE.clear()
    except Exception:
        pass
    try:
        with _OC_LOCK:
            _OC_CACHE.update({"data": None, "ts": 0.0})
    except Exception:
        pass
    try:
        with _FYERS_META_LOCK:
            _FYERS_META_CACHE.update({"balance": None, "strike": None, "ts": 0.0})
    except Exception:
        pass
    try:
        _SV3_CACHE.clear()
    except Exception:
        pass
    after = _cache_stats()
    return {"ok": True, "before_kb": before["total_kb"], "after_kb": after["total_kb"]}

def _sv3_month_key(t: int):
    """IST-naive epoch (jaise nifty500 .gz mein store hota hai) → (year,
    month) tuple — calendar-month bucket ki pehchan. Baaki .gz code (jaise
    upar _sv2_resample_btc_daily) jis IST-naive convention se calendar-din
    nikalta hai, wahi utcfromtimestamp() yahan bhi use kiya hai."""
    dt = datetime.datetime.utcfromtimestamp(t)
    return (dt.year, dt.month)

def _sv3_bucket_monthly(rows: list) -> list:
    """Raw 1D rows ({'t','o','h','l','c'} short-keys, nifty500 .gz format) ko
    calendar-month candles mein group karta hai — LWC {'time','open','high',
    'low','close'} long-keys output (jaisa _sv2_resample_btc_daily upar karta
    hai). Ek mahine ke saare trading days mil kar ek hi "1M" candle ban jaate
    hain (open=mahine ka pehla open, close=mahine ka last close, high/low=
    mahine ka max/min)."""
    month_buckets: dict = {}
    order: list = []
    for r in rows:
        try:
            t = int(r["t"]); o = float(r["o"]); h = float(r["h"])
            l = float(r["l"]); c = float(r["c"])
        except Exception:
            continue
        key = _sv3_month_key(t)
        if key not in month_buckets:
            month_buckets[key] = {"time": t, "open": o, "high": h, "low": l, "close": c}
            order.append(key)
        else:
            b = month_buckets[key]
            b["high"]  = max(b["high"], h)
            b["low"]   = min(b["low"],  l)
            b["close"] = c
    return [month_buckets[k] for k in order]

def _sv3_resample_monthly(monthly: list, n_months: int) -> list:
    """Pehle-se-bucketed 1M candles (_sv3_bucket_monthly ka output) ko
    groups of n_months mein INDEX-chunk karta hai — bilkul SV2 ke
    1D→3D/9D/27D wale index-chunk jaisa (Month-1 se hamesha sync, calendar
    quarter/financial-year se independent)."""
    if n_months <= 1:
        return monthly
    out = []
    for i in range(0, len(monthly), n_months):
        chunk = monthly[i:i + n_months]
        if not chunk:
            break
        out.append({
            "time":  chunk[0]["time"],
            "open":  chunk[0]["open"],
            "high":  max(c["high"] for c in chunk),
            "low":   min(c["low"]  for c in chunk),
            "close": chunk[-1]["close"],
        })
    return out

def _sv3_daily_to_lwc(rows: list) -> list:
    """Raw nifty500 .gz 1D rows (short-keys) → LWC long-keys, bina resample
    kiye — ye khud base/reveal series banti hai (SV2_BN['1D'] jaisa role,
    replay isi se ek-ek din reveal hoti hai)."""
    out = []
    for r in rows:
        try:
            out.append({
                "time":  int(r["t"]),   "open": float(r["o"]), "high": float(r["h"]),
                "low":   float(r["l"]), "close": float(r["c"]),
            })
        except Exception:
            continue
    return out

def _sv3_symbol_disk_filename(sym: str) -> str:
    """Per-symbol disk filename — SAME naming convention jo migration card
    use karta hai:
    'NSE:ABDL-EQ' → 'NSE_ABDL-EQ.json'."""
    return sym.replace(":", "_") + ".json"

# ── SV3 `.bin` helpers (NO_PRELOAD_PLAN Step 3C) ───────────────────────────
# Layout: /data/bin/sv3/symbols/<stem>.bin (stem = json filename minus ".json",
# jaise converter banata hai). Row = t,o,h,l,c (daily, IST-naive t). Source JSON
# me jo extra `v` (volume) key thi wo `.bin` me nahi hai — app use hi nahi karti.
def _sv3_bin_path(sym: str) -> str:
    stem = _sv3_symbol_disk_filename(sym)[:-len(".json")]
    return os.path.join(_bin_root(), "sv3", "symbols", stem + ".bin")

def _sv3_last_t(sym: str):
    """Symbol ka aakhri t (sirf 64-byte header padhta hai). Bin nahi/khaali
    ho to None => 'naya symbol' (poori history fetch hogi)."""
    try:
        h = _bin_read_header(_sv3_bin_path(sym))
        return h["last_t"] if h["row_count"] else None
    except Exception:
        return _sv3_last_t_from_json(sym)

def _sv3_read_json_rows(sym: str) -> list:
    """Purani JSON (LAZY fallback / bin-missing). Transient, cache nahi."""
    path = os.path.join(_SV3_SYMBOLS_DIR, _sv3_symbol_disk_filename(sym))
    try:
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("data", []) or []
        return data if isinstance(data, list) else []
    except Exception as e:
        _slog_exception(f"_sv3_read_json_rows[{sym}]", e)
        return []

def _sv3_last_t_from_json(sym: str):
    if not _BIN_READ_FALLBACK:
        return None
    rows = _sv3_read_json_rows(sym)
    try:
        return max(int(r["t"]) for r in rows) if rows else None
    except Exception:
        return None

def _sv3_read_symbol_from_disk(sym: str) -> list:
    """Ek symbol ka daily raw rows ({t,o,h,l,c}) — `.bin` se (memmap, sirf ye
    symbol). Bin missing/kharab + BIN_READ_FALLBACK!=0 => purani JSON se
    lazy fallback. Fail ho to khaali list, koi network fallback nahi."""
    err = None
    if _np is None:
        err = "numpy install nahi hai"
    else:
        path = _sv3_bin_path(sym)
        if os.path.isfile(path):
            try:
                arr = _bin_open_rows(path)
                return _bin_rows_to_dicts(arr, long_keys=False)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        else:
            err = "bin file nahi hai"
    if _BIN_READ_FALLBACK:
        rows = _sv3_read_json_rows(sym)
        if rows:
            _slog(f"SV3 [{sym}] .bin read fail ({err}) — purani JSON se lazy fallback", level="err")
        return rows
    return []

def _sv3_bin_apply(sym: str, new_rows: list, existing_rows) -> int:
    """`new_rows` ({t,o,h,l,c}) ko symbol ki `.bin` me jodta hai. Bin hai =>
    sirf last_t se AAGE wale rows end me append. Bin nahi hai => (purani JSON
    fallback + new) se poori nayi `.bin` (atomic). Returns naye rows ki ginti."""
    dest = _sv3_bin_path(sym)
    if os.path.isfile(dest):
        try:
            return _bin_append_dicts(dest, new_rows)
        except ValueError:
            raise
    base = existing_rows if existing_rows is not None else (
        _sv3_read_json_rows(sym) if _BIN_READ_FALLBACK else [])
    merged_map = {r["t"]: r for r in base}
    before = len(merged_map)
    for r in new_rows:
        merged_map[r["t"]] = r
    merged = sorted(merged_map.values(), key=lambda r: r["t"])
    _bin_write_and_verify(dest, merged, _BIN_KEYS_SHORT, 86400, 0)
    return len(merged_map) - before if before else len(merged)

def _sv3_after_update(syms: list) -> None:
    """Update ke baad: single-slot cache saaf, manifest me 'diverged' mark
    (converter in `.bin` ko purani JSON se overwrite na kare)."""
    try:
        _SV3_CACHE.clear()
    except Exception:
        pass
    if not syms:
        return
    try:
        man = _bin_manifest_load()
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for sym in syms:
            stem = _sv3_symbol_disk_filename(sym)[:-len(".json")]
            try:
                h = _bin_read_header(_sv3_bin_path(sym))
            except Exception:
                continue
            e = dict(man["datasets"].get(f"sv3_{stem}") or {})
            e.update({"rows": h["row_count"], "first_t": h["first_t"], "last_t": h["last_t"],
                      "tf_seconds": 86400, "flags": 0,
                      "bytes": _BIN_HDR_SIZE + h["row_count"] * _BIN_ROW_SIZE,
                      "diverged": True, "appended_at": stamp})
            e.setdefault("warnings", [])
            e.setdefault("verify", "ok")
            man["datasets"][f"sv3_{stem}"] = e
        _bin_manifest_save(man)
    except Exception as ex:
        _slog(f"SV3 manifest diverged-mark fail: {ex}", level="err")

def _sv3_symbol_list() -> list:
    """Top-left picker ki symbol-name list — Nifty500 ki list
    (_fetch_nifty500_symbols, 1hr-cached, sirf naam, koi price data nahi) se
    aati hai. Koi bhi symbol data-call nahi hoti jab tak user khud koi symbol
    select na kare.

    BTC_1D_SYMBOL yahan explicitly PINNED hai — Nifty500 ke _index.json
    (_fetch_nifty500_symbols) mein NAHI hai, isliye list ke aage manually
    add kiya jaata hai taaki SV3 symbol-search picker mein bhi dikhe. BTC
    ka data/update pipeline (_btc_sv3_*, Binance-based) Nifty500 ke Fyers-
    based pipeline se poori tarah independent hai."""
    try:
        syms = sorted(_fetch_nifty500_symbols())
    except Exception:
        syms = []
    return [BTC_1D_SYMBOL] + syms

def _build_sv3_data(symbol: str) -> dict:
    """Ek symbol ke liye 1D (base) + 1M (internal reference, UI mein nahi
    dikhta) + 3M/9M/27M/81M/243M (visible TFs) — sab ek saath resample karke
    return karta hai. Same symbol dobara maange to single-slot cache se
    turant milta hai (rerun-safe, Streamlit ke baar-baar top-se-bottom
    re-execute hone par bhi dobara compute nahi hota)."""
    if _SV3_CACHE.get("symbol") == symbol and _SV3_CACHE.get("data"):
        return _SV3_CACHE["data"]
    # Sirf isi symbol ki file fetch hoti hai (single request), baaki 499
    # symbols ko bilkul touch nahi kiya jaata.
    raw_rows = _sv3_read_symbol_from_disk(symbol) or []
    monthly  = _sv3_bucket_monthly(raw_rows)
    out = {"1D": _sv3_daily_to_lwc(raw_rows), "1M": monthly}
    for label, n in SV3_TF_MONTHS.items():
        out[label] = _sv3_resample_monthly(monthly, n)
    _SV3_CACHE["symbol"] = symbol
    _SV3_CACHE["data"]   = out
    return out

# (Cleanup Step B2) SV3 bulk (`_build_sv3_bulk_all_stocks`, `_sv3_bulk_build`, `_sv3_bulk_serve_bytes`,
# `/data/bin/sv3/bulk_tf.json`) hata diya — chart.html `/api/sv3_all_stocks` kabhi call nahi karta tha.

def _sv3_to_js(data: list) -> str:
    return json.dumps(data, separators=(",", ":"))

def load_sv3_last_symbol() -> str:
    """Pichli baar select kiya gaya symbol — disk se (naya browser session
    mein bhi wahi symbol default mile)."""
    if os.path.exists(SV3_LAST_SYMBOL_FILE):
        try:
            with open(SV3_LAST_SYMBOL_FILE) as f:
                return (json.load(f) or {}).get("symbol", "") or ""
        except Exception:
            pass
    return ""

def save_sv3_last_symbol(symbol: str):
    try:
        with open(SV3_LAST_SYMBOL_FILE, "w") as f:
            json.dump({"symbol": symbol}, f)
    except Exception:
        pass

# ═══ SV2 READ PATH — `.bin` (NO_PRELOAD_PLAN Step 2, 2B) ═══════════════════
# Pehle: gz parse -> saare TFs resample -> `_SV2_CACHE[*_tfs_full]` me
# hamesha RAM me + startup par BTC prewarm thread. Ab: precomputed TF bins
# (`/data/bin/sv2/<asset>_<TF>.bin`) memmap se khulte hain, sirf zaroori
# window list-of-dict banti hai (BTC: anchor-trim; BN: full, jaisa pehle).
# RAM me kuch nahi rehta, startup par kuch nahi chalta.
_SV2_BTC_LABELS = ("5m_raw", "8H", "1D", "3D", "9D", "27D")
_SV2_BN_LABELS  = ("5m_raw", "125m", "1D", "3D", "9D", "27D")

def _sv2_bin_path(asset: str, label: str) -> str:
    return os.path.join(_bin_root(), "sv2", f"{asset}_{label}.bin")

def _sv2_trim_range(arr, label: str, anchor_epoch: int = None, asset: str = "bn"):
    """`_sv2_trim` ka EXACT numpy/memmap version — list ki jagah (i0, i1)
    indices lautata hai (arr[i0:i1]). Logic ek-ek line wahi: n = effective
    limit; len<=n => poora; anchor None => last N; anchor => 25% pehle / 75%
    baad ki window; BTC 1D start 27-multiple par snap."""
    n = _sv2_get_max(asset).get(label, 2000)
    L = len(arr)
    if L <= n:
        return 0, L
    def _snap27(start_idx: int) -> int:
        if asset == "btc" and label == "1D":
            return max(0, start_idx - (start_idx % 27))
        return start_idx
    if anchor_epoch is None:
        return _snap27(L - n), L
    idx    = _bin_lower_bound(arr, int(anchor_epoch), right=False)   # pehla i jahan t >= anchor
    before = n // 4
    start  = max(0, idx - before)
    end    = min(L, start + n)
    start  = max(0, end - n)
    start  = _snap27(start)
    return start, end

def _sv2_build_asset_tfs_from_gz(asset_key: str, anchor_epoch: int = None) -> dict:
    """LAZY FALLBACK (env BIN_READ_FALLBACK != "0"): sv2 TF `.bin` missing/
    kharab ho to ISI REQUEST ke liye purani gz padh ke wahi purana compute.
    Kuch RAM me cache nahi hota. Startup par kabhi nahi chalta."""
    rows = _sv2_load_bn_gz() if asset_key == "bn" else _sv2_load_btc_gz()
    if not rows:
        return {}
    tfs_full = {lb: fn() for lb, fn in _bin_sv2_builders(asset_key, rows)}
    del rows
    if asset_key == "btc":
        return {k: _sv2_trim(v, k, anchor_epoch, "btc") for k, v in tfs_full.items()}
    return tfs_full

def _sv2_build_asset_tfs(asset_key: str, anchor_epoch: int = None) -> dict:
    """Ek asset ('bn' | 'btc') ke saare TFs — `.bin` se, on-demand.
    Output shape/values pehle jaise (list-of-dict, time/open/high/low/close).

    BTC: har TF `anchor_epoch` ke aas-paas `_sv2_trim` logic se bounded window
    (dekho `_sv2_trim_range`). BankNifty: jaan-bujh kar FULL untrimmed (user
    ka purana explicit ask). Bin missing/kharab => lazy gz fallback."""
    if asset_key not in ("bn", "btc"):
        return {}
    labels = _SV2_BTC_LABELS if asset_key == "btc" else _SV2_BN_LABELS
    err = None
    if _np is None:
        err = "numpy install nahi hai"
    else:
        try:
            out = {}
            for lb in labels:
                arr = _bin_open_rows(_sv2_bin_path(asset_key, lb))
                if asset_key == "btc":
                    i0, i1 = _sv2_trim_range(arr, lb, anchor_epoch, "btc")
                    win = arr[i0:i1]
                else:
                    win = arr
                out[lb] = _bin_rows_to_dicts(win, long_keys=True)
                del win, arr
            return out
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    _slog(f"SV2 [{asset_key}] .bin read fail ({err}) — "
          f"{'lazy gz fallback' if _BIN_READ_FALLBACK else 'fallback band (BIN_READ_FALLBACK=0)'}",
          level="err")
    if _BIN_READ_FALLBACK:
        try:
            return _sv2_build_asset_tfs_from_gz(asset_key, anchor_epoch)
        except Exception as e:
            _slog_exception(f"_sv2_build_asset_tfs_from_gz({asset_key})", e)
    return {}


# ── SV2 derived TF bins REBUILD (update ke baad, background) ───────────────
# Append sirf `src/sv2_<asset>_5m.bin` me hota hai. Derived TF bins (8H/1D/
# 3D/9D/27D …) ko waise hi PURANE resample functions se dobara banana padta
# hai (3D/9D/27D index-0 se group hote hain => sirf tail append nahi chalta).
# Ye transient RAM leta hai (poori src -> dicts), phir turant free. Chalte
# waqt readers purani (poori-consistent) bins dekhte hain — har bin atomic
# replace hoti hai.
@st.cache_resource
def _get_sv2_rebuild_state() -> dict:
    return {"lock": threading.Lock(),
            "btc": {"running": False, "again": False, "last": "", "at": 0.0},
            "bn":  {"running": False, "again": False, "last": "", "at": 0.0}}

def _sv2_rebuild_derived(asset: str) -> str:
    src = os.path.join(_bin_root(), "src", f"sv2_{asset}_5m.bin")
    arr = _bin_open_rows(src)
    rows = _bin_rows_to_dicts(arr, long_keys=False)
    del arr
    builders = dict(_bin_sv2_builders(asset, rows))
    entries = {}
    for lb in (_SV2_BTC_LABELS if asset == "btc" else _SV2_BN_LABELS):
        out = builders[lb]()
        entries[lb] = _bin_write_and_verify(_sv2_bin_path(asset, lb), out,
                                            _BIN_KEYS_LONG, _BIN_SV2_TF_SECONDS[lb], 1)
        del out
    del rows, builders
    _gc.collect()
    try:
        man = _bin_manifest_load()
        for lb, entry in entries.items():
            old = dict(man["datasets"].get(f"sv2_{asset}_{lb}") or {})
            old.update(entry)
            old["diverged"] = True
            man["datasets"][f"sv2_{asset}_{lb}"] = old
        _bin_manifest_save(man)
    except Exception as e:
        _slog(f"SV2 rebuild manifest save fail ({asset}): {e}", level="err")
    return f"{asset}: " + ", ".join(f"{lb}={e['rows']}" for lb, e in entries.items())

def _sv2_rebuild_start(asset: str) -> None:
    """Background thread me rebuild; chalte waqt dobara aaye to ek baar aur
    chalta hai (taaki last append zaroor reflect ho). Idempotent."""
    if asset not in ("btc", "bn"):
        return
    S = _get_sv2_rebuild_state()
    e = S[asset]
    with S["lock"]:
        if e["running"]:
            e["again"] = True
            return
        e["running"] = True
        e["again"] = False

    def _w():
        try:
            while True:
                t0 = time.time()
                try:
                    msg = _sv2_rebuild_derived(asset)
                    e["last"] = f"ok {time.time()-t0:.1f}s — {msg}"
                    _slog(f"SV2 TF bins rebuild done ({e['last']})", level="ok")
                except Exception as ex:
                    e["last"] = f"FAIL {type(ex).__name__}: {ex}"
                    _slog_exception(f"_sv2_rebuild_derived({asset})", ex)
                e["at"] = time.time()
                with S["lock"]:
                    if e["again"]:
                        e["again"] = False
                        continue
                    e["running"] = False
                    return
        except BaseException:
            with S["lock"]:
                e["running"] = False
            raise

    threading.Thread(target=_w, daemon=True, name=f"sv2-rebuild-{asset}").start()

# (Step 2) `sv2_prewarm_btc`, `_SV2_BTC_PREWARM_STATE`, startup trigger aur
# legacy/dead `_build_sv2_data()` HATA diye — startup par ab kuch preload nahi hota.

# ─── Fyers historical data ─────────────────────────────────────────────────────
def _fyers_history(resolution: str, from_date: str, to_date: str) -> list:
    creds = load_creds()
    if not creds.get("access_token"):
        return []
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol":     "NSE:NIFTYBANK-INDEX",
        "resolution": resolution,
        "date_format": "1",
        "range_from": from_date,
        "range_to":   to_date,
        "cont_flag":  "1",
    }
    try:
        res = requests.get(
            "https://api-t1.fyers.in/data/history",
            headers=headers, params=params, timeout=15,
        ).json()
        if res.get("s") == "ok":
            return [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]]
                    for c in res.get("candles", [])]
    except Exception:
        pass
    return []

def fetch_bn_intraday(interval_mins: int) -> list:
    # Fyers TF ke hisaab se max safe range:
    # 1m  → 10 days, 5m → 30 days, 15m → 60 days, 45m → 90 days
    _days = {1: 10, 5: 30, 15: 60, 45: 90}.get(interval_mins, 30)
    today  = _ist_now().strftime("%Y-%m-%d")
    from_d = (_ist_now() - datetime.timedelta(days=_days)).strftime("%Y-%m-%d")
    return _fyers_history(str(interval_mins), from_d, today)

def _fyers_history_chunk(resolution: str, from_date: str, to_date: str, raise_on_error: bool = False) -> list:
    """Same as _fyers_history but doesn't read creds again (for chunked calls).

    raise_on_error=False (default, existing callers like load_bn_daily()):
    silently returns [] on any failure — kept as-is kyunki wahan chunk-loop
    mein kuch chunks ka legitimately "no_data" hona normal hai (jaise future
    ke half-year mein abhi tak koi candle nahi bana), aur poora loop kisi ek
    chunk ki wajah se crash nahi hona chahiye.

    raise_on_error=True (naya, _append_new_bn_candles() jaisi jagah ke liye):
    agar API call hi fail ho (network/timeout/non-2xx/response "s" != "ok"
    aur != "no_data"), to Exception raise karta hai — taaki caller "koi naya
    candle nahi mila" (genuine) aur "fetch hi fail ho gaya" (fault) mein farak
    kar sake, aur silent false-success na dikhaye."""
    creds = load_creds()
    if not creds.get("access_token"):
        if raise_on_error:
            raise RuntimeError("access_token missing")
        return []
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol": "NSE:NIFTYBANK-INDEX", "resolution": resolution,
        "date_format": "1", "range_from": from_date, "range_to": to_date, "cont_flag": "1",
    }
    try:
        resp = requests.get("https://api-t1.fyers.in/data/history",
                           headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        res = resp.json()
        status = res.get("s")
        if status == "ok":
            return [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]] for c in res.get("candles", [])]
        if status == "no_data":
            return []   # genuinely koi candle nahi (market band / range mein data hi nahi) — ye fail nahi hai
        if raise_on_error:
            raise RuntimeError(f"Fyers history API error: {res}")
        return []
    except Exception:
        if raise_on_error:
            raise
        return []

def load_bn_daily() -> list:
    """Fetch BankNifty daily candles — last 1 year only (live-chart ke liye
    itna hi kaafi hai; poori multi-year history sirf Replay Mode ke .gz data
    se aati hai, wahan is function ka koi role nahi).

    FIX: agar disk pe purana (stale bhi) cache maujood hai, use hamesha
    fallback ke roop mein yaad rakho. Fresh Fyers fetch fail ho ya poori
    tarah khaali aaye (rate-limit / token expiry / network hiccup), to
    empty list return karke chart blank karne ke bajaye purana valid cache
    hi return karo. Isse 1D chart kabhi blank nahi dikhega — worst case
    thoda stale rahega, jab tak fresh fetch phir se successful na ho jaye.
    """
    stale_fallback: list = []
    if os.path.exists(BN_DAILY_CACHE):
        try:
            with open(BN_DAILY_CACHE) as f:
                cache = json.load(f)
            stale_fallback = cache.get("data", [])
            if time.time() - cache.get("ts", 0) < DAILY_CACHE_TTL:
                return stale_fallback
        except Exception:
            pass

    today     = _ist_now()
    today_str = today.strftime("%Y-%m-%d")
    from_str  = (today - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
    mid_str   = (today - datetime.timedelta(days=182)).strftime("%Y-%m-%d")

    # 2 chunks (Fyers allows max ~1yr range per call — 2 half-year chunks
    # ke andar hi last-1-year comfortably fit ho jaata hai, in parallel).
    chunks = [(from_str, mid_str), (mid_str, today_str)]

    all_candles: list = []
    seen_times: set  = set()
    with ThreadPoolExecutor(max_workers=2) as _ex:
        _results = list(_ex.map(lambda ch: _fyers_history_chunk("D", ch[0], ch[1]), chunks))
    for chunk in _results:
        for c in chunk:
            if c[0] not in seen_times:
                seen_times.add(c[0])
                all_candles.append(c)

    all_candles.sort(key=lambda x: x[0])

    # Normalize timestamps to 9:15 AM IST of each IST calendar day.
    # Fyers may return midnight UTC or any session-start epoch — normalize to
    # 3:45 AM UTC (= 9:15 AM IST) so chart.html resample() stays consistent.
    _IST_OFF   = 19800          # 5.5 * 3600
    _NSE_OPEN  = 33300          # 9:15 AM = 9*3600+15*60 seconds from IST midnight
    normalized = []
    for c in all_candles:
        t_ms   = int(c[0])
        t_sec  = t_ms // 1000
        ist_sec        = t_sec + _IST_OFF
        ist_midnight   = ist_sec - (ist_sec % 86400)   # IST midnight of that day
        t_fixed        = (ist_midnight - _IST_OFF) + _NSE_OPEN  # 9:15 AM IST in UTC epoch
        normalized.append([t_fixed * 1000] + list(c[1:]))
    all_candles = normalized

    if all_candles:
        try:
            with open(BN_DAILY_CACHE, "w") as f:
                json.dump({"ts": time.time(), "data": all_candles}, f)
        except Exception:
            pass
        return all_candles

    # Fresh fetch poori tarah fail ho gaya (empty) — purana cache hi wapas do
    # taaki 1D chart kabhi blank na dikhe.
    return stale_fallback

# ─── BTC (Binance) ────────────────────────────────────────────────────────────
def fetch_btc(interval: str = "1m", limit: int = 1000) -> list:
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval={interval}&limit={limit}",
            timeout=10,
        ).json()
        return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                 float(x[4]), float(x[5])] for x in r]
    except Exception:
        return []


def load_btc_daily() -> list:
    """Fetch BTC daily candles — last 1 year only (live-chart ke liye itna
    hi kaafi hai; poori multi-year history sirf Replay Mode ke .gz data se
    aati hai, wahan is function ka koi role nahi). Binance ek hi call mein
    1000 daily candles tak de deta hai, isliye 365 din ka data 1 hi request
    mein aa jaata hai — koi chunking/loop ki zaroorat nahi."""
    today_str = _ist_now().strftime("%Y-%m-%d")
    if os.path.exists(DAILY_CACHE_FILE):
        try:
            with open(DAILY_CACHE_FILE) as f:
                c = json.load(f)
            data = c.get("data", [])
            cache_ok = time.time() - c.get("ts", 0) < DAILY_CACHE_TTL
            if cache_ok and data:
                last_ts = data[-1][0] // 1000
                last_date = datetime.datetime.utcfromtimestamp(last_ts).strftime("%Y-%m-%d")
                if last_date < today_str:
                    cache_ok = False
            if cache_ok:
                return data
        except Exception:
            pass

    today   = _ist_now()
    from_ms = int((today - datetime.timedelta(days=365)).replace(
        tzinfo=datetime.timezone.utc).timestamp() * 1000)
    to_ms   = int(today.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) + 86400000

    all_candles: list = []
    seen_times: set   = set()
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval=1d&startTime={from_ms}&endTime={to_ms}&limit=1000",
            timeout=15,
        ).json()
        if isinstance(r, list):
            for x in r:
                ts = int(x[0])
                if ts not in seen_times:
                    seen_times.add(ts)
                    all_candles.append([ts, float(x[1]), float(x[2]),
                                        float(x[3]), float(x[4]), float(x[5])])
    except Exception:
        pass

    all_candles.sort(key=lambda x: x[0])

    # FIX: agar Binance fetch fail ho gaya ya khaali aaya (rate-limit, network
    # hiccup — jo rapid sv1↔sv2↔sv3 switching ke baad common hai kyunki
    # thode hi second mein multiple daily-klines calls chali jaati hain),
    # to khaali [] return karke daily+ panels (1D/3D/9D/27D) ko crash mat
    # karo. Iske bajaye purana cached data hi wapas de do (chahe stale ho) —
    # kam-se-kam history dikhti rahegi jab tak agla successful fetch na ho
    # jaaye. Sirf tabhi khaali [] jaayega jab disk par pehle se koi cache
    # hi kabhi nahi bani.
    if not all_candles:
        try:
            if os.path.exists(DAILY_CACHE_FILE):
                with open(DAILY_CACHE_FILE) as f:
                    _stale = json.load(f).get("data", [])
                if _stale:
                    return _stale
        except Exception:
            pass
        return all_candles   # sach mein kabhi cache nahi bani — tab hi []

    try:
        with open(DAILY_CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "data": all_candles}, f)
    except Exception:
        pass
    return all_candles

# ─── Fyers WebSocket (DataSocket) — live BankNifty ticks ─────────────────────
_ws_thread_started = False

def _on_ws_message(msg):
    try:
        if not isinstance(msg, dict):
            # DataSocket kabhi-kabhi non-dict bhi bhejta hai (e.g. status
            # strings) — pehle ye chup-chaap ignore ho jaata tha, ab kam se
            # kam ek baar type note kar dete hain taaki "WS bilkul kuch
            # nahi bhej raha" vs "WS bhej raha hai but format samajh nahi
            # aa raha" mein farak pata chale.
            with _BN_FEED_DEBUG_LOCK:
                _BN_FEED_DEBUG["ws_last_error"] = f"non-dict message ignored: {type(msg).__name__}"
            return
        # DataSocket sends list of ticks or single tick dict
        ticks = msg if isinstance(msg, list) else [msg]
        got_ltp = False
        got_opt = False
        for tick in ticks:
            if not isinstance(tick, dict):
                continue
            ltp = tick.get("ltp") or tick.get("LTP")
            if ltp is None:
                continue
            ltp = float(ltp)
            _sym = tick.get("symbol")
            if _sym and _sym != _BN_INDEX_SYM:
                # Option-chain leg ka tick — index ki _LIVE/candle state ko haath nahi lagana
                with _OC_LIVE_LOCK:
                    if _sym in _OC_LIVE["want"]:
                        _OC_LIVE["ltp"][_sym] = ltp
                        _OC_LIVE["dirty"] = True
                        _OC_LIVE["last_tick_ts"] = time.time()
                got_opt = True
                continue
            got_ltp = True
            with _LIVE_LOCK:
                _LIVE["ltp"]        = ltp
                _LIVE["prev_close"] = float(tick.get("prev_close_price") or tick.get("prev_close") or _LIVE.get("prev_close") or ltp)
                _LIVE["ts"]         = int(time.time())
                _LIVE["source"]     = "ws"
            # Build running 1-minute candle from raw LTP ticks
            _update_candle_ltp(ltp)
            _write_live_json()
        with _BN_FEED_DEBUG_LOCK:
            if got_ltp:
                _BN_FEED_DEBUG["ws_last_message_ts"] = time.time()
            elif not got_opt:
                # message aaya, but usme koi tick mein 'ltp'/'LTP' key hi
                # nahi mili — ye batayega ki Fyers ne format badal diya
                # ya symbol subscribe hi galat hua.
                _BN_FEED_DEBUG["ws_last_error"] = f"message had no ltp field: {str(msg)[:200]}"
    except Exception as e:
        with _BN_FEED_DEBUG_LOCK:
            _BN_FEED_DEBUG["ws_last_error"] = f"{type(e).__name__}: {e}"
        _slog_exception("_on_ws_message", e)

def _on_ws_error(msg):
    with _BN_FEED_DEBUG_LOCK:
        _BN_FEED_DEBUG["ws_connected"] = False
        _BN_FEED_DEBUG["ws_last_error"] = str(msg)
    _slog(f"BankNifty WS error: {msg}", level="err")

def _on_ws_close(msg):
    with _BN_FEED_DEBUG_LOCK:
        _BN_FEED_DEBUG["ws_connected"] = False
        _BN_FEED_DEBUG["ws_last_close"] = str(msg)
    _slog(f"BankNifty WS closed: {msg}", level="warn")

_BN_INDEX_SYM = "NSE:NIFTYBANK-INDEX"

# Option-chain legs ka live LTP (Fyers WS). want = chain me jo symbols dikh rahe hain,
# subs = is connection par actually subscribed. Fyers limit ~200 symbols/socket (hum ~85 use karte hain).
_OC_LIVE = {"want": set(), "subs": set(), "ltp": {}, "dirty": False}
_OC_LIVE_LOCK = threading.Lock()

def _oc_live_apply_subs() -> None:
    """want vs subs ka diff Fyers socket par lagao (connected ho tabhi)."""
    ws = globals().get("fyers_ws")
    with _BN_FEED_DEBUG_LOCK:
        connected = bool(_BN_FEED_DEBUG.get("ws_connected"))
    if ws is None or not connected:
        return
    with _OC_LIVE_LOCK:
        add = sorted(_OC_LIVE["want"] - _OC_LIVE["subs"])
        rem = sorted(_OC_LIVE["subs"] - _OC_LIVE["want"])
    try:
        for i in range(0, len(rem), 50):
            ws.unsubscribe(symbols=rem[i:i + 50], data_type="SymbolUpdate")
        for i in range(0, len(add), 50):
            ws.subscribe(symbols=add[i:i + 50], data_type="SymbolUpdate")
        with _OC_LIVE_LOCK:
            _OC_LIVE["subs"] = set(_OC_LIVE["want"])
            for s in list(_OC_LIVE["ltp"]):
                if s not in _OC_LIVE["want"]:
                    del _OC_LIVE["ltp"][s]
    except Exception as e:
        _slog_exception("_oc_live_apply_subs", e)

def _oc_live_sync_subs(payload) -> None:
    """Chain payload (current + next expiry) ke saare CE/PE symbols ko live-subscribe list me daalo."""
    if not isinstance(payload, dict) or payload.get("error"):
        return
    syms = set()
    for ch in (payload, payload.get("next")):
        if isinstance(ch, dict):
            for r in ch.get("rows") or []:
                for k in ("ce", "pe"):
                    leg = r.get(k) if isinstance(r, dict) else None
                    if isinstance(leg, dict) and leg.get("symbol"):
                        syms.add(leg["symbol"])
    if not syms:
        return
    with _OC_LIVE_LOCK:
        _OC_LIVE["want"] = syms
    _oc_live_apply_subs()

def _oc_live_push_loop() -> None:
    """Option LTP ticks ko ~250ms par coalesce karke ek push (bn_oc_ltp) — poora map, taaki snapshot bhi valid rahe."""
    while True:
        time.sleep(0.25)
        try:
            with _OC_LIVE_LOCK:
                if not _OC_LIVE["dirty"]:
                    continue
                _OC_LIVE["dirty"] = False
                snap = dict(_OC_LIVE["ltp"])
            _ws_publish("bn_oc_ltp", {"ts": time.time(), "ltp": snap})
        except Exception as e:
            _WS_HUB["stats"]["last_error"] = f"oc_live_push: {type(e).__name__}: {e}"

def _on_ws_connect():
    try:
        with _OC_LIVE_LOCK:
            _OC_LIVE["subs"] = set()      # naya connection — pichhli subscriptions gayi
        fyers_ws.subscribe(
            symbols=[_BN_INDEX_SYM],
            data_type="SymbolUpdate",
        )
        with _BN_FEED_DEBUG_LOCK:
            _BN_FEED_DEBUG["ws_connected"]       = True
            _BN_FEED_DEBUG["ws_last_connect_ts"] = time.time()
            _BN_FEED_DEBUG["ws_last_error"]      = None
        _slog("BankNifty WS connected + subscribed to NSE:NIFTYBANK-INDEX", level="ok")
        _oc_live_apply_subs()             # reconnect ke baad option legs dobara subscribe
        fyers_ws.keep_running()
    except Exception as e:
        with _BN_FEED_DEBUG_LOCK:
            _BN_FEED_DEBUG["ws_connected"]  = False
            _BN_FEED_DEBUG["ws_last_error"] = f"{type(e).__name__}: {e}"
        _slog_exception("_on_ws_connect (subscribe/keep_running)", e)

def _get_live_payload():
    """Build the latest live-tick payload straight from in-memory state — no
    disk I/O, so this is as fresh as the WS thread's last update. ts is a
    float (sub-second precision) so multiple ticks arriving within the same
    wall-clock second don't collapse into one (previously ts was int(time.time())
    which made the JS-side dedupe drop intra-second ticks)."""
    with _LIVE_LOCK:
        snap = dict(_LIVE)
    if snap["ltp"] is None:
        return None
    ltp = snap["ltp"]
    now        = time.time()
    minute_epoch = int(now // 60) * 60
    with _CANDLE_LOCK:
        if _CANDLE["minute"] == minute_epoch and _CANDLE["open"] is not None:
            o = _CANDLE["open"]
            h = _CANDLE["high"]
            l = _CANDLE["low"]
        else:
            o = h = l = ltp
    # FIX: pehle koi indication nahi tha ki ye ltp Fyers WebSocket push se
    # aayi hai ya 1s REST-poll fallback se — user ke liye "Live" claim
    # verify karna namumkin tha. Ab source + age explicitly bhejte hain,
    # jaisa Binance/BTC side pehle se karta hai (spot_source/spot_age_sec).
    tick_age = (now - snap["ts"]) if snap["ts"] else None
    with _BN_FEED_DEBUG_LOCK:
        feed_debug = dict(_BN_FEED_DEBUG)
    return {
        "ts":     now,
        "ltp":    ltp,
        "source": snap.get("source"),      # "ws" | "rest" | None
        "age_sec": round(tick_age, 1) if tick_age is not None else None,
        "candle": {
            "time":  minute_epoch,
            "open":  o,
            "high":  h,
            "low":   l,
            "close": ltp,
        },
        # WS/REST health — dekho ab har failure-path _slog() ke through
        # startup-debug-log (🐞 icon) mein bhi likha jaata hai, ye yahan
        # sirf JSON-consumers (chart.html future use) ke liye hai.
        "feed_debug": feed_debug,
    }

def _write_live_json():
    payload = _get_live_payload()
    if payload is None:
        return
    # WebSocket push (subscribed browsers ko turant)
    try:
        _ws_publish("bn_tick", payload)
    except Exception:
        pass

def _start_ws():
    global _ws_thread_started, fyers_ws
    if _ws_thread_started:
        return
    creds = load_creds()
    if not creds.get("access_token"):
        _slog("BankNifty WS start skip kiya: access_token missing hai creds mein.", level="warn")
        return
    try:
        from fyers_apiv3.FyersWebsocket import data_ws as fw
        access_token = f"{creds['app_id']}:{creds['access_token']}"
        fyers_ws = fw.FyersDataSocket(
            access_token=access_token,
            log_path="",
            litemode=True,
            write_to_file=False,
            reconnect=True,
            on_connect=_on_ws_connect,
            on_close=_on_ws_close,
            on_error=_on_ws_error,
            on_message=_on_ws_message,
        )
        t = threading.Thread(target=fyers_ws.connect, name="FyersWS", daemon=True)
        t.start()
        _ws_thread_started = True
        _slog("BankNifty WS thread (FyersWS) start ho gaya.", level="info")
    except Exception as e:
        with _BN_FEED_DEBUG_LOCK:
            _BN_FEED_DEBUG["ws_last_error"] = f"start failed: {type(e).__name__}: {e}"
        _slog_exception("_start_ws (thread launch)", e)

# ─── Background REST poller (fallback: polls Fyers 1m candles every 3s) ──────
# Har failure-path ab _BN_FEED_DEBUG mein record hoti hai aur err-level
# _slog line ke through startup-debug-log (🐞 icon) mein bhi dikhti hai —
# PEHLE ye saara `except: pass` mein chup jaata tha, isliye jab WS *aur*
# REST dono ek saath fail hote the, tick 82s+ stale ho jaata tha aur
# koi wajah kahin nahi milti thi.
def _rest_live_loop():
    while True:
        # WebSocket se fresh data aa raha hai to REST call skip karo
        with _LIVE_LOCK:
            ws_fresh = (time.time() - _LIVE["ts"]) < 8
        if not ws_fresh:
            creds = load_creds()
            if not creds.get("access_token"):
                with _BN_FEED_DEBUG_LOCK:
                    _BN_FEED_DEBUG["rest_last_error"] = "access_token missing in creds"
            else:
                today = _ist_now().strftime("%Y-%m-%d")
                headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
                params = {
                    "symbol": "NSE:NIFTYBANK-INDEX", "resolution": "1",
                    "date_format": "1", "range_from": today, "range_to": today, "cont_flag": "1",
                }
                with _BN_FEED_DEBUG_LOCK:
                    _BN_FEED_DEBUG["rest_last_attempt_ts"] = time.time()
                try:
                    res = requests.get(
                        "https://api-t1.fyers.in/data/history",
                        headers=headers, params=params, timeout=6,
                    ).json()
                    if res.get("s") == "ok":
                        candles = res.get("candles", [])
                        if candles:
                            last = candles[-1]
                            bar_epoch = int(last[0])
                            o, h, l, c = float(last[1]), float(last[2]), float(last[3]), float(last[4])
                            with _LIVE_LOCK:
                                if time.time() - _LIVE["ts"] > 5:
                                    _LIVE["ltp"]    = c
                                    _LIVE["ts"]     = int(time.time())
                                    _LIVE["source"] = "rest"
                            _set_candle_from_bar(bar_epoch, o, h, l, c)
                            _write_live_json()
                            with _BN_FEED_DEBUG_LOCK:
                                _BN_FEED_DEBUG["rest_last_success_ts"] = time.time()
                                _BN_FEED_DEBUG["rest_last_error"]      = None
                        else:
                            with _BN_FEED_DEBUG_LOCK:
                                _BN_FEED_DEBUG["rest_last_error"] = "response ok but candles list empty"
                    else:
                        # Fyers ne khud error diya (invalid token, rate
                        # limit, market closed symbol, etc) — poora
                        # response record karo taaki exact reason pata chale.
                        with _BN_FEED_DEBUG_LOCK:
                            _BN_FEED_DEBUG["rest_last_error"] = f"Fyers API error: {str(res)[:300]}"
                        _slog(f"BankNifty REST fallback: Fyers ne error diya: {str(res)[:300]}", level="err")
                except Exception as e:
                    with _BN_FEED_DEBUG_LOCK:
                        _BN_FEED_DEBUG["rest_last_error"] = f"{type(e).__name__}: {e}"
                    _slog_exception("_rest_live_loop (REST fallback call)", e)
        time.sleep(1)

def _ensure_fyers_threads():
    """Sirf Fyers se related background threads (BankNifty REST poll, token
    monitor, Fyers option-chain, Fyers WS). Binance ka koi thread yahan
    start nahi hota — 'Fyers Entry' mode isse hi call karta hai."""
    names = {t.name for t in threading.enumerate()}
    if "FyersRESTPoller" not in names:
        threading.Thread(target=_rest_live_loop, name="FyersRESTPoller", daemon=True).start()
    if "FyersTokenMonitor" not in names:
        threading.Thread(target=_token_monitor_loop, name="FyersTokenMonitor", daemon=True).start()
    if "OptionChainBG" not in names:
        threading.Thread(target=_option_chain_bg_loop, name="OptionChainBG", daemon=True).start()
    if "OCLivePush" not in names:
        threading.Thread(target=_oc_live_push_loop, name="OCLivePush", daemon=True).start()
    _start_ws()
    _register_api_route()   # shared side-server (bn_history/fyers_optionchain), idempotent

def _ensure_binance_threads():
    """Binance Entry mode ke liye — Fyers ka koi thread yahan start nahi hota."""
    _register_api_route()   # shared side-server (market_depth), idempotent

# ─── Tornado /api/bn_history handler — lazy historical data endpoint ──────────
# Streamlit internally uses Tornado. We inject our own route so chart.html's
# infinite-scroll loader can fetch older BN candles on demand without a page reload.

_HIST_ENDPOINT_REGISTERED = False

@st.cache_resource
def _get_api_register_lock() -> threading.Lock:
    return threading.Lock()

# In-memory cache per (resolution, from_date, to_date) — avoids repeat Fyers calls
_HIST_CACHE: dict = {}
_HIST_CACHE_TTL = 300  # 5 min


def _hist_cache_key(resolution: str, from_date: str, to_date: str) -> str:
    return f"{resolution}|{from_date}|{to_date}"


def _bn_history_handler_data(resolution: str, from_date: str, to_date: str) -> dict:
    """Fetch BN history (with in-memory cache). Returns {candles, cached, error}."""
    key = _hist_cache_key(resolution, from_date, to_date)
    now = time.time()
    if key in _HIST_CACHE:
        entry = _HIST_CACHE[key]
        if now - entry["ts"] < _HIST_CACHE_TTL:
            return {"candles": entry["data"], "cached": True}
    candles = _fyers_history(resolution, from_date, to_date)
    if candles is None:
        candles = []
    converted = []
    for c in candles:
        try:
            converted.append({
                "time":   int(c[0]) // 1000,
                "open":   round(float(c[1]), 2),
                "high":   round(float(c[2]), 2),
                "low":    round(float(c[3]), 2),
                "close":  round(float(c[4]), 2),
                "volume": round(float(c[5]), 2) if len(c) > 5 else 0,
            })
        except Exception:
            continue
    _HIST_CACHE[key] = {"ts": now, "data": converted}
    return {"candles": converted, "cached": False}


def _register_api_route():
    """Start a lightweight HTTP server on _API_PORT for /api/bn_history.

    Streamlit runs on port 8501 by default but its internal Tornado server
    is hard to hook into reliably across versions.  Instead we spin up our
    own plain HTTP server on a dedicated side-port (8502) inside the same
    Python process.  chart.html auto-detects the port at runtime.
    """
    global _HIST_ENDPOINT_REGISTERED
    try:
        _ws_hub_start()   # WebSocket hub (127.0.0.1:8503, nginx /ws) — idempotent
    except Exception as _e_wsh:
        _slog_exception("_ws_hub_start()", _e_wsh)
    # (2026-09-20 FIX) _HIST_ENDPOINT_REGISTERED plain global hai — har Streamlit
    # rerun par False ho jaata tha, isliye har rerun par ek NAYA server thread
    # agle free port (8502..8510) par chalu ho jaata tha (zombie servers + har
    # ek ka alag image cache). Ab guard process-wide hai: cache_resource lock +
    # live thread-name check (Fyers/Binance threads wala hi pattern).
    with _get_api_register_lock():
        if any(t.name == "BNHistoryAPI" and t.is_alive() for t in threading.enumerate()):
            return
        _HIST_ENDPOINT_REGISTERED = True   # set before thread starts — idempotent

    def _server_loop():
        import http.server, urllib.parse as _up

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass  # suppress stdout noise

            def do_OPTIONS(self):
                # CORS preflight — browser JS JSON POST (Content-Type: application/json) karta hai
                # with Content-Type: application/json, jo "non-simple" request
                # hai, isliye pehle ek OPTIONS preflight bhejta hai. Isko 204
                # + zaroori CORS headers ke saath jawab dena zaroori hai, warna
                # asli POST kabhi jaata hi nahi (browser hi block kar deta hai).
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Store-Token")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _post_json(self, code, obj):
                # JSON response helper (POST routes) — CORS + Content-Length ke saath
                body = json.dumps(obj, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _render_store_route(self, method, parsed):
                # /api/render_store/<rules|journal|log|log_telegram|ping> — logic _rs_handle() me (Render main.ts + chart.html Render Log tab)
                name = parsed.path[len("/api/render_store/"):].strip("/")
                raw = b""
                if method == "POST":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                    except Exception:
                        n = 0
                    if n > _RS_MAX_BODY:
                        return self._post_json(413, {"ok": False, "msg": "body bahut bada"})
                    raw = self.rfile.read(n) if n > 0 else b""
                code, obj = _rs_handle(method, name, _up.parse_qs(parsed.query), self.headers.get("X-Store-Token"), raw)
                return self._post_json(code, obj)

            def do_POST(self):
                parsed = _up.urlparse(self.path)

                if parsed.path.startswith("/api/render_store/"):
                    return self._render_store_route("POST", parsed)

                # ⏰ Routine email jawab-page submit (token body me; _routine_resp_post check karta hai)
                if parsed.path == "/api/routine_resp":
                    try:
                        _rn = int(self.headers.get("Content-Length") or 0)
                    except Exception:
                        _rn = 0
                    if _rn > 256 * 1024:
                        return self._post_json(413, {"ok": False, "msg": "body bahut bada"})
                    _rraw = self.rfile.read(_rn) if _rn > 0 else b""
                    _rc, _ro = _routine_resp_post(_rraw)
                    return self._post_json(_rc, _ro)

                # 🗓 Telegram relay (Render) "today plan" par yahan aata hai — header X-Relay-Secret == RELAY_SECRET (wahi jo send ke liye hai)
                if parsed.path == "/api/plan_now":
                    _given = str(self.headers.get("X-Relay-Secret") or "")
                    if not RELAY_SECRET or not hmac.compare_digest(_given.encode("utf-8"), RELAY_SECRET.encode("utf-8")):
                        return self._post_json(401, {"ok": False, "msg": "unauthorized"})
                    try:
                        return self._post_json(200, {"ok": True, "text": _plan_now_text()})
                    except Exception as _e_pn:
                        _slog_exception("plan_now", _e_pn)
                        return self._post_json(500, {"ok": False, "msg": "plan nahi ban paya"})

                # MYENGINE-STEP-10A (2026-10-03): 🤖 Telegram Remote — relay har message ka text + update_id yahan bhejta hai (padhne wale commands).
                # Secret check /api/plan_now jaisa hi. Parsing sab _tg_cmd_api me (app.py ke upar wale MYENGINE-STEP-10A block me).
                if parsed.path == "/api/cmd":
                    _given = str(self.headers.get("X-Relay-Secret") or "")
                    if not RELAY_SECRET or not hmac.compare_digest(_given.encode("utf-8"), RELAY_SECRET.encode("utf-8")):
                        return self._post_json(401, {"ok": False, "msg": "unauthorized"})
                    try:
                        _cn = int(self.headers.get("Content-Length") or 0)
                    except Exception:
                        _cn = 0
                    if _cn > 16 * 1024:
                        return self._post_json(413, {"ok": False, "msg": "body bahut bada"})
                    _craw = self.rfile.read(_cn) if _cn > 0 else b""
                    try:
                        _cc, _co = _tg_cmd_api(_craw)
                        return self._post_json(_cc, _co)
                    except Exception as _e_cmd:
                        _slog_exception("tg_cmd", _e_cmd)
                        return self._post_json(500, {"ok": False, "msg": "command nahi chala"})

                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                parsed = _up.urlparse(self.path)

                if parsed.path.startswith("/api/render_store/"):
                    return self._render_store_route("GET", parsed)

                # ⏰ Routine email jawab-page (HTML) — token ke bina 403
                if parsed.path == "/api/routine_resp":
                    _rq = _up.parse_qs(parsed.query)
                    try:
                        _rc, _rh = _routine_page((_rq.get("d") or [""])[0], (_rq.get("tok") or [""])[0])
                    except Exception as _e_rp:
                        _slog_exception("routine_resp page", _e_rp)
                        _rc, _rh = 500, "<body>server error</body>"
                    _rb = _rh.encode("utf-8")
                    self.send_response(_rc)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Length", str(len(_rb)))
                    self.end_headers()
                    try:
                        self.wfile.write(_rb)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return

                # ── Simple health-check — diagnostic panel aur kisi bhi
                # future monitoring ke liye. Koi upstream data nahi maangta,
                # bas confirm karta hai side-server zinda hai. ──
                # ── /api/server_status — ready indicator ka HTTP heartbeat + "Server (RAM)"
                # one-liner. Sirf already-recorded asli values (koi network call nahi).
                # Jawab aana hi "server zinda" ka signal hai (chart.html ~3s poll).
                if parsed.path == "/api/server_status":
                    try:
                        _ss_out = {"ok": True, "ts": time.time(), "status": _replay.replay_server_status()}
                    except Exception as _e_ss:
                        _slog_exception("server_status", _e_ss)
                        _ss_out = {"ok": False, "ts": time.time(), "status": None, "error": str(_e_ss)}
                    body = json.dumps(_ss_out).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path == "/api/ping":
                    body = json.dumps({"ok": True, "ts": time.time()}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # HF /data SAVE -> sirf WebSocket action "local_state_save" (@_ws_action, _wsa_local_state_save) -> _local_state_save()
                # LOAD -> build-time inject (_local_state_load_all() seedha chart.html mein likha jaata hai, koi HTTP call nahi).

                # ── Market Depth (5-level order book) — chart.html isi endpoint ko
                # poll karta hai jab koi strike ka depth-icon tap hota hai. Sirf
                # active/open symbol ke liye poll hota hai (background mein nahi),
                # isliye TTL-cache ke bawajood rate-limit par extra load nahi padta. ──
                if parsed.path == "/api/market_depth":
                    dqs = _up.parse_qs(parsed.query, keep_blank_values=False)
                    dsymbol = dqs.get("symbol", [""])[0]
                    try:
                        if not dsymbol:
                            body = json.dumps({"error": "symbol missing",
                                                "_debug": {"branch": "no_symbol_in_request"}}).encode()
                            self.send_response(400)
                        else:
                            dpayload = refresh_market_depth_cache(dsymbol)
                            body = json.dumps(dpayload).encode()
                            self.send_response(200)
                    except Exception as e:
                        # Ye wahi jagah hai jo pehle frontend ko generic
                        # "Connection failed" dikhwati thi agar iska response
                        # kabhi malformed/hang ho jaata — ab _debug.branch se
                        # exact pata chalega ki server-side crash hua tha.
                        import traceback
                        body = json.dumps({
                            "error": f"server exception: {e}",
                            "_debug": {"branch": "http_handler_exception",
                                       "symbol": dsymbol,
                                       "trace": traceback.format_exc()[-500:]},
                        }).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── Option chain payload — served directly from the same
                # in-memory cache the background loop already maintains. ──
                if parsed.path == "/api/fyers_optionchain":
                    try:
                        payload = get_cached_option_chain_payload()
                        body = json.dumps(payload).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"error": f"server exception: {e}"}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── Music player — playlist + file streaming ──────────────
                # /api/music_list  → /data/music/ ki disk-listing. Music panel
                #   isse fetch karke playlist banata hai, koi mobile folder
                #   pick karne ki zaroorat nahi.
                # /api/music/<filename> → asal audio bytes, sahi Content-Type
                #   ke saath + HTTP Range support (seek/scrub ke liye zaroori
                #   — Streamlit ka built-in static-serving ye range support
                #   nahi deta aur .mp3 ko galat text/plain bhej deta, isliye
                #   yahi side-port route use kar rahe hain).
                if parsed.path == "/api/music_list":
                    try:
                        body = json.dumps({"tracks": _music_sorted_names()}).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"tracks": [], "error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path.startswith("/api/music/"):
                    import re as _re_music
                    fname = _up.unquote(parsed.path[len("/api/music/"):])
                    # Path traversal guard — sirf flat filenames allowed,
                    # koi "../" ya sub-folder nahi.
                    if not fname or "/" in fname or "\\" in fname or not fname.lower().endswith(_MUSIC_EXTS):
                        self.send_response(404)
                        self.end_headers()
                        return
                    fpath = os.path.join(MUSIC_DIR, fname)
                    if not os.path.isfile(fpath):
                        self.send_response(404)
                        self.end_headers()
                        return
                    _mime = {
                        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
                        ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac",
                        ".opus": "audio/opus",
                    }.get(os.path.splitext(fname)[1].lower(), "application/octet-stream")
                    fsize = os.path.getsize(fpath)
                    range_header = self.headers.get("Range")
                    try:
                        if range_header:
                            # "bytes=START-END" — END optional
                            m = _re_music.match(r"bytes=(\d+)-(\d*)", range_header)
                            start = int(m.group(1)) if m else 0
                            end = int(m.group(2)) if (m and m.group(2)) else fsize - 1
                            end = min(end, fsize - 1)
                            length = end - start + 1
                            self.send_response(206)
                            self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
                            self.send_header("Content-Length", str(length))
                            self.send_header("Accept-Ranges", "bytes")
                            self.send_header("Content-Type", _mime)
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Cache-Control", "public, max-age=86400")
                            self.end_headers()
                            with open(fpath, "rb") as f:
                                f.seek(start)
                                remaining = length
                                while remaining > 0:
                                    chunk = f.read(min(262144, remaining))
                                    if not chunk:
                                        break
                                    self.wfile.write(chunk)
                                    remaining -= len(chunk)
                        else:
                            self.send_response(200)
                            self.send_header("Content-Length", str(fsize))
                            self.send_header("Accept-Ranges", "bytes")
                            self.send_header("Content-Type", _mime)
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Cache-Control", "public, max-age=86400")
                            self.end_headers()
                            with open(fpath, "rb") as f:
                                while True:
                                    chunk = f.read(262144)
                                    if not chunk:
                                        break
                                    self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # user ne seek/skip kiya, purana response cut hona normal hai
                    return

                # ── Video player — list + file streaming (music route ka bada bhai) ──
                # /api/video_list → {"videos":[rel paths], "sizes":{rel:bytes}}
                # /api/video/<rel/path> → asal video bytes, HTTP Range (seek ke liye zaroori),
                #   512KB chunks me stream (poori file memory me load nahi hoti).
                if parsed.path == "/api/video_list":
                    try:
                        _items = _video_list_items()
                        body = json.dumps({"videos": [x[0] for x in _items],
                                           "sizes": {x[0]: x[1] for x in _items},
                                           "mtimes": {x[0]: x[2] for x in _items}}).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"videos": [], "sizes": {}, "error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path.startswith("/api/video/"):
                    _vrel = _up.unquote(parsed.path[len("/api/video/"):])
                    _vpath = _video_resolve(_vrel)
                    if not _vpath:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    _vmime = _VIDEO_MIME.get(os.path.splitext(_vpath)[1].lower(), "application/octet-stream")
                    _vsize = os.path.getsize(_vpath)
                    _vcode, _vs, _ve = _video_parse_range(self.headers.get("Range"), _vsize)
                    if _vcode == 416:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{_vsize}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    _vlen = (_ve - _vs + 1) if _vsize > 0 else 0
                    try:
                        self.send_response(_vcode)
                        if _vcode == 206:
                            self.send_header("Content-Range", f"bytes {_vs}-{_ve}/{_vsize}")
                        self.send_header("Content-Length", str(_vlen))
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Type", _vmime)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Cache-Control", "public, max-age=3600")
                        self.end_headers()
                        with open(_vpath, "rb") as _vf:
                            _vf.seek(_vs)
                            _vleft = _vlen
                            while _vleft > 0:
                                _vchunk = _vf.read(min(524288, _vleft))
                                if not _vchunk:
                                    break
                                self.wfile.write(_vchunk)
                                _vleft -= len(_vchunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                        pass  # seek/skip/close par browser purani connection kaat deta hai — normal
                    return

                # ── Image viewer — list + file serving ─────────────────────
                # EXACT music route ka mirror. /api/image_list → /data/images/
                # ki disk-listing.
                # /api/image/<filename> → asal image bytes, sahi Content-Type.
                if parsed.path == "/api/image_list":
                    try:
                        _idx_data = {"images": _images_sorted_names()}
                        # meta = {name: [mtime, size]} -- viewer ke date-sort ke liye (asli os.stat)
                        _idx_data["meta"] = _images_meta(_idx_data.get("images") or [])
                        body = json.dumps(_idx_data).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"images": [], "error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path.startswith("/api/image/"):
                    fname = _up.unquote(parsed.path[len("/api/image/"):])
                    if not fname or "/" in fname or "\\" in fname or not fname.lower().endswith(_IMAGE_EXTS):
                        self.send_response(404)
                        self.end_headers()
                        return
                    fpath = os.path.join(IMAGES_DIR, fname)
                    try:
                        _ist = os.stat(fpath)
                        if not os.path.isfile(fpath):
                            raise FileNotFoundError(fpath)
                    except Exception:
                        self.send_response(404)
                        self.end_headers()
                        return
                    _img_mime = {
                        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                        ".svg": "image/svg+xml",
                    }.get(os.path.splitext(fname)[1].lower(), "application/octet-stream")
                    _etag = _image_etag(_ist)
                    try:
                        # Step 3A: RAM me kuch nahi — file disk se 64KB chunks me stream.
                        # ETag + If-None-Match => unchanged file par 304 (body nahi).
                        if self.headers.get("If-None-Match") == _etag:
                            self.send_response(304)
                            self.send_header("ETag", _etag)
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Cache-Control", "public, max-age=86400")
                            self.end_headers()
                            return
                        with open(fpath, "rb") as _imf:
                            self.send_response(200)
                            self.send_header("Content-Length", str(_ist.st_size))
                            self.send_header("Content-Type", _img_mime)
                            self.send_header("ETag", _etag)
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Cache-Control", "public, max-age=86400")
                            self.end_headers()
                            _left = _ist.st_size
                            while _left > 0:
                                _chunk = _imf.read(min(65536, _left))
                                if not _chunk:
                                    break
                                self.wfile.write(_chunk)
                                _left -= len(_chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return

                # /api/image_prefetch?batch=N — client kisi image par jump
                # kare (ya list load ho) to us image ki batch-index (N =
                # floor(idx/20)) bhejta hai; server poori 20-image batch
                # background thread me RAM-warm kar deta hai (fire-and-forget
                # — turant 200 return, actual loading async chalti hai).
                # Agli asal /api/image/<name> request tab RAM se instant
                # serve hoti hai. ?names=a.jpg,b.jpg bhi supported hai
                # (chhota ad-hoc set — backward compat).
                if parsed.path == "/api/image_prefetch":
                    # Step 3A: RAM-warm ab hai hi nahi => NO-OP. Route sirf isliye
                    # rakha hai ki chart.html (fire-and-forget fetch) toote nahi.
                    body = json.dumps({"ok": True, "queued": 0}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── (2026-09-28) Replay / SV2 / SV3 data — HTTP ONLY ──
                # Payload: kind/key|asset|sym + ok + data, taaki chart.html ka
                # waiter/inbox logic bina badle chale.
                if parsed.path in ("/api/replay_candles", "/api/sv2_data",
                                   "/api/sv3_symbol", "/api/sv3_symbol_list_data"):
                    _qs_d = _up.parse_qs(parsed.query)
                    _qd = lambda k, d="": (_qs_d.get(k) or [d])[0]
                    _kind_d = parsed.path[len("/api/"):].replace("sv3_symbol_list_data", "sv3_symbol_list")
                    try:
                        if parsed.path == "/api/replay_candles":
                            _k = _qd("key")
                            _res = _replay.replay_get_multi_tf(
                                _k, log_fn=_slog)
                            _payload = {"kind": "candles", "key": _k, **_res}
                            _slog(f"[http] replay_candles({_k}) — "
                                  f"{'ok' if _res.get('ok') else 'FAILED: ' + str(_res.get('error'))}",
                                  level="ok" if _res.get("ok") else "err")
                        elif parsed.path == "/api/sv2_data":
                            _asset = _qd("asset")
                            _anc_raw = _qd("anchor")
                            try:
                                _anc = int(_anc_raw) if _anc_raw not in ("", "null", "undefined") else None
                            except Exception:
                                _anc = None
                            _tfs = _sv2_build_asset_tfs(_asset, _anc if _asset == "btc" else None)
                            _payload = {"kind": "sv2_data", "asset": _asset, "ok": bool(_tfs), "tfs": _tfs or {}}
                            _slog(f"[http] sv2_data({_asset}) — {'ok' if _tfs else 'FAILED'}",
                                  level="ok" if _tfs else "err")
                        elif parsed.path == "/api/sv3_symbol":
                            _sym = _qd("sym")
                            _rows = _sv3_read_symbol_from_disk(_sym) if _sym else []
                            _payload = {"kind": "sv3_symbol", "sym": _sym, "ok": bool(_rows), "rows": _rows}
                            _slog(f"[http] sv3_symbol({_sym}) — {'ok' if _rows else 'FAILED'} ({len(_rows)} rows)",
                                  level="ok" if _rows else "err")
                        else:   # /api/sv3_symbol_list_data (purana bare-list /api/sv3_symbol_list alag hai, untouched)
                            _lst = _sv3_symbol_list()
                            _payload = {"kind": "sv3_symbol_list", "ok": bool(_lst), "list": _lst}
                            _slog(f"[http] sv3_symbol_list — {'ok' if _lst else 'FAILED'} ({len(_lst)} symbols)",
                                  level="ok" if _lst else "err")
                        _code_d = 200
                    except Exception as _e_d:
                        _slog_exception(f"[http] {parsed.path}", _e_d)
                        _payload = {"kind": {"replay_candles": "candles"}.get(_kind_d, _kind_d),
                                    "ok": False, "error": str(_e_d)}
                        if parsed.path == "/api/replay_candles":
                            _payload["key"] = _qd("key")
                        elif parsed.path == "/api/sv2_data":
                            _payload["asset"] = _qd("asset")
                        elif parsed.path == "/api/sv3_symbol":
                            _payload["sym"] = _qd("sym")
                        _code_d = 200   # error bhi JSON body me — chart.html ok:false padhta hai
                    try:
                        _txt = json.dumps(_payload, separators=(",", ":"), default=str, allow_nan=False)
                    except ValueError:
                        # NaN/Infinity JS JSON.parse me fail hote hain — null bana do
                        import re as _re_nan
                        _txt = _re_nan.sub(r"(?<![\w\"])-?(NaN|Infinity)(?![\w\"])", "null",
                                           json.dumps(_payload, separators=(",", ":"), default=str))
                    body = _txt.encode()
                    try:
                        self.send_response(_code_d)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return

                # ── (2026-09-28) cache_stats / cache_clear — HTTP ──
                if parsed.path in ("/api/cache_stats", "/api/cache_clear"):
                    _qs_s = _up.parse_qs(parsed.query)
                    try:
                        if parsed.path == "/api/cache_stats":
                            _o = {"kind": "cache_stats", "ok": True, "stats": _cache_stats()}
                        elif parsed.path == "/api/cache_clear":
                            _cc = _clear_all_caches()
                            _slog(f"[http] cache_clear — ok ({_cc.get('before_kb', '?')}KB → {_cc.get('after_kb', '?')}KB)", level="ok")
                            _o = {"kind": "cache_clear", "ok": True, "result": _cc}
                        body = json.dumps(_o, default=str).encode()
                        self.send_response(200)
                    except Exception as _e_s:
                        _slog_exception(f"[http] {parsed.path}", _e_s)
                        body = json.dumps({"ok": False, "error": str(_e_s)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── (2026-09-28) Update All — HTTP ──
                if parsed.path in ("/api/update_all", "/api/update_all_status"):
                    try:
                        _ua = _update_all_start() if parsed.path == "/api/update_all" else _update_all_status()
                        body = json.dumps(_ua).encode()
                        self.send_response(200)
                    except Exception as _e_ua:
                        _slog_exception(f"[http] {parsed.path}", _e_ua)
                        body = json.dumps({"ok": False, "error": str(_e_ua)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── SV3 symbol-SEARCH list — sirf naam (koi price data
                # nahi), LAZY: chart.html isko tabhi maangta hai jab user
                # khud SV3 symbol-search picker kholta hai (_openSv3SymbolPicker),
                # page-load par nahi. _fetch_nifty500_symbols() khud 1hr
                # cached hai (disk '_index.json' se), isliye baar-baar
                # search kholne par bhi dobara network-fetch nahi hoti. ──
                if parsed.path == "/api/sv3_symbol_list":
                    try:
                        body = json.dumps(_sv3_symbol_list(), separators=(",", ":")).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"error": f"server exception: {e}"}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── SV3 last-symbol save (fire-and-forget) — jab instant
                # bulk-switch se symbol badalta hai (koi Streamlit rerun
                # nahi hota us waqt), ye chhota GET call disk par
                # "pichla symbol" turant save kar deta hai — full page
                # reload ke bina bhi. ────────────────────────────────────
                if parsed.path == "/api/sv3_save_last_symbol":
                    sqs = _up.parse_qs(parsed.query, keep_blank_values=False)
                    ssym = sqs.get("symbol", [""])[0]
                    try:
                        if ssym:
                            save_sv3_last_symbol(ssym)
                        body = json.dumps({"ok": True}).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"ok": False, "error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── v3 — Replay symbols (btcusdt2/3/4/5) ON-DEMAND fetch.
                # chart.html isko call karta hai jab user in 4 mein se
                # koi symbol pehli baar select kare (page-load par ab
                # inka candle-data pehle se load nahi hota — sirf halka
                # metadata, dekho get_replay_symbols_metadata()). Yahan
                # master-file (/data/replay_cache) aur reveal_state.json se
                # data aata hai. ──
                if parsed.path == "/api/replay_symbol":
                    rqs = _up.parse_qs(parsed.query, keep_blank_values=False)
                    rkey = rqs.get("key", [""])[0]
                    try:
                        _rentry = next((e for e in _replay.REPLAY_SYMBOL_REGISTRY if e["key"] == rkey), None)
                        if not _rentry:
                            body = json.dumps({"error": f"unknown replay key: {rkey}"}).encode()
                            self.send_response(400)
                        else:
                            _rresult = _replay.replay_get_bucketed(
                                rkey, log_fn=_slog
                            )
                            body = json.dumps({
                                "key": rkey,
                                "label": _rentry["label"],
                                "bucket_min": 480,
                                "market": None,
                                "candles": _rresult["candles"],
                                "revealed_up_to_t": _rresult["revealed_up_to_t"],  # FIX: countdown-timer ke liye
                            }, separators=(",", ":")).encode()
                            self.send_response(200)
                    except Exception as e:
                        _slog_exception(f"/api/replay_symbol({rkey})", e)
                        body = json.dumps({"error": f"server exception: {e}"}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── v3 — Replay symbols: "abhi kaunsa symbol screen par
                # active hai" batata hai (fire-and-forget, jaisa
                # /api/sv3_save_last_symbol). Isse _live_data_pusher ko
                # pata chalta hai ki background 30s-poll mein SIRF isi
                # symbol ka live-push bhejna hai, baaki 3 ka nahi (jo load
                # hi nahi hain unka push bhejne ka koi fayda nahi). ──────
                if parsed.path == "/api/replay_set_active":
                    aqs = _up.parse_qs(parsed.query, keep_blank_values=False)
                    akey = aqs.get("key", [""])[0]
                    try:
                        _replay.replay_set_active_key(akey)
                        body = json.dumps({"ok": True, "active_key": _replay.replay_get_active_key()}).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"ok": False, "error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path != "/api/bn_history":
                    self.send_response(404)
                    self.end_headers()
                    return

                qs = _up.parse_qs(parsed.query, keep_blank_values=False)
                def _q(k, d=""): return qs.get(k, [d])[0]

                resolution = _q("resolution", "1")
                from_date  = _q("from", "")
                to_date    = _q("to", "")
                days_str   = _q("days", "10")

                if not from_date:
                    try:
                        days = int(days_str)
                    except ValueError:
                        days = 10
                    today_ist = _ist_now()
                    to_date   = today_ist.strftime("%Y-%m-%d")
                    from_date = (today_ist - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

                creds = load_creds()
                if not creds.get("access_token"):
                    body = b'{"error":"not_authenticated"}'
                    self.send_response(401)
                else:
                    result = _bn_history_handler_data(resolution, from_date, to_date)
                    body   = json.dumps(result).encode()
                    self.send_response(200)

                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        # Try ports 8502..8510 — pick whichever is free
        import socketserver
        for port in range(8502, 8511):
            try:
                srv = socketserver.ThreadingTCPServer(("0.0.0.0", port), _Handler)
                srv.daemon_threads = True
                # Write chosen port to a file so chart.html JS can read it via Streamlit component
                try:
                    with open(".api_port", "w") as _f:
                        _f.write(str(port))
                except Exception:
                    pass
                srv.serve_forever()
                break
            except OSError:
                continue   # port in use, try next

    threading.Thread(target=_server_loop, name="BNHistoryAPI", daemon=True).start()

# ─── Auto-startup: HF Secret "FYERS_LOGIN_URL" se login (Method B ka secret-
# based version — roz subah Google URL ko HF Space Settings → Variables and
# secrets mein paste karo, app boot hote hi khud auth_code exchange kar
# lega). Same auth_code dobara-dobara exchange na ho isliye load_creds()
# mein last-processed code yaad rakha jaata hai. ─────────────────────────────
if not st.session_state.get("_url_secret_login_done"):
    st.session_state["_url_secret_login_done"] = True
    _url_secret = _get_secret("FYERS_LOGIN_URL")
    if _url_secret and not is_session_active():
        _boot_creds2 = load_creds()
        _code2 = _extract_auth_code(_url_secret)
        if _code2 and _code2 != _boot_creds2.get("_last_url_secret_code", ""):
            _app_id2 = _boot_creds2.get("app_id", DEFAULT_APP_ID)
            _secret2 = _boot_creds2.get("secret_key", DEFAULT_SECRET)
            try:
                _ok3, _tok3, _resp3 = fyers_get_access_token(_app_id2, _secret2, _code2)
            except Exception as _e3:
                _slog_exception("LOGIN_URL_SECRET fyers_get_access_token()", _e3)
                _ok3, _tok3, _resp3 = False, str(_e3), {}
            if _ok3:
                save_creds({
                    **_boot_creds2,
                    "app_id": _app_id2, "secret_key": _secret2,
                    "client_id": DEFAULT_CLIENT_ID, "password": DEFAULT_PASSWORD,
                    "access_token": _tok3,
                    "_last_url_secret_code": _code2,   # dobara exchange skip karne ke liye
                })
                _sess_cache.update({"active": False, "ts": time.time()}); st.session_state["_force_active"] = False
                st.session_state["_login_success_msg"] = "🎉 Login successful (FYERS_LOGIN_URL secret se)!"
                _slog("🔐 LOGIN_URL_SECRET: access_token mil gaya, creds save ho gaye — SUCCESS.", level="ok")
            else:
                # Dobara-dobara retry na ho isliye failed code ko bhi "seen" mark kar dete hain
                save_creds({**_boot_creds2, "_last_url_secret_code": _code2})
                _slog(f"🔐 LOGIN_URL_SECRET: access_token exchange FAILED — {_tok3} | raw_response={_resp3}", level="err")
        st.rerun()

# ─── In-chart broker panel: handle query params from iframe form submits ───────
_qp = st.query_params

# Handler 1: Manual Google URL auth_code
if "fyers_code" in _qp:
    _code   = _qp.get("fyers_code",   "").strip()
    _app_id = _qp.get("fyers_app_id", DEFAULT_APP_ID).strip()
    _secret = _qp.get("fyers_secret", DEFAULT_SECRET).strip()
    st.query_params.clear()
    _slog(
        f"🔐 LOGIN_MANUAL: Google-redirect URL se query param mila. "
        f"code_present={'yes' if _code else 'NO'} (len={len(_code)}) "
        f"app_id={'yes' if _app_id else 'NO'} secret={'yes' if _secret else 'NO'}",
        level="info",
    )
    if _code:
        try:
            _ok, _tok, _resp = fyers_get_access_token(_app_id, _secret, _code)
        except Exception as _e_login_manual:
            _slog_exception("LOGIN_MANUAL fyers_get_access_token()", _e_login_manual)
            _ok, _tok, _resp = False, str(_e_login_manual), {}
        if _ok:
            save_creds({
                **load_creds(),
                "app_id": _app_id, "secret_key": _secret,
                "client_id": DEFAULT_CLIENT_ID, "password": DEFAULT_PASSWORD,
                "access_token": _tok,
            })
            _sess_cache.update({"active": False, "ts": time.time()}); st.session_state["_force_active"] = False
            st.session_state["_login_success_msg"] = "🎉 Login successful!"
            _slog("🔐 LOGIN_MANUAL: access_token mil gaya, creds save ho gaye — SUCCESS.", level="ok")
        else:
            _slog(f"🔐 LOGIN_MANUAL: access_token exchange FAILED — {_tok} | raw_response={_resp}", level="err")
    else:
        _slog("🔐 LOGIN_MANUAL: URL me auth_code hi nahi mila — token exchange attempt hi nahi hua.", level="warn")
    st.rerun()

# Handler 3: SV2 chunk / candle-count settings — Apply (bottom-bar 📦 icon)
if _qp.get("sv2_chunk_trigger") == "1":
    _new_bn, _new_btc = {}, {}
    for _k in _SV2_MAX_BN_DEFAULT.keys():
        _pk = f"sv2bn_{_k}"
        if _pk in _qp:
            try:
                _new_bn[_k] = int(_qp.get(_pk))
            except Exception:
                pass
    for _k in _SV2_MAX_BTC_DEFAULT.keys():
        _pk = f"sv2btc_{_k}"
        if _pk in _qp:
            try:
                _new_btc[_k] = int(_qp.get(_pk))
            except Exception:
                pass
    st.query_params.clear()
    if _new_bn or _new_btc:
        _cs_settings = load_sv2_chunk_settings()
        _cs_settings.setdefault("bn",  {}).update(_new_bn)
        _cs_settings.setdefault("btc", {}).update(_new_btc)
        save_sv2_chunk_settings(_cs_settings)
        # session cache invalidate karo taaki naya value turant lagu ho
        st.session_state.pop("_sv2_max_eff_bn",  None)
        st.session_state.pop("_sv2_max_eff_btc", None)
    st.rerun()

# Handler 3c: SV3 (Stack View 3 — stock replay) — top-left symbol-picker se
# naya symbol chuna gaya. SV2 jaisa hi lazy-load: jab tak koi symbol pick
# nahi hota, nifty500 .gz kabhi read hi nahi hota — sirf empty placeholders
# inject honge (neeche _build_chart_html mein). Symbol pick hote hi ye
# handler chalta hai: session_state mein save, disk par bhi remember
# (agli baar app khulne par wahi symbol default), aur rerun taaki naya
# resampled data turant inject ho jaaye.
if "sv3_symbol" in _qp:
    _sv3_sym = _qp.get("sv3_symbol", "").strip()
    st.query_params.clear()
    if _sv3_sym:
        st.session_state["_sv3_symbol"]        = _sv3_sym
        st.session_state["_sv3_data_requested"] = True
        save_sv3_last_symbol(_sv3_sym)
    st.rerun()

# Handler 3d: SV3 lazy-load fallback — jab Stack View 3 pehli baar ON
# toggle ho (grid-picker se) aur abhi tak koi symbol select nahi hua ho, JS
# in-chart se ?sv3_load=1 bhejta hai (bilkul sv2_load jaisa) taaki ek default
# symbol (pichli baar wala, ya list ka pehla) load ho jaaye.
if _qp.get("sv3_load") == "1":
    st.query_params.clear()
    if not st.session_state.get("_sv3_symbol"):
        _sv3_default_sym = load_sv3_last_symbol()
        if not _sv3_default_sym:
            _sv3_avail = _sv3_symbol_list()
            _sv3_default_sym = _sv3_avail[0] if _sv3_avail else ""
        st.session_state["_sv3_symbol"] = _sv3_default_sym
    st.session_state["_sv3_data_requested"] = True
    st.rerun()

# Handler 4: SV2 chunk / candle-count settings — Reset to default
if _qp.get("sv2_chunk_reset") == "1":
    st.query_params.clear()
    if os.path.exists(SV2_CHUNK_SETTINGS_FILE):
        try:
            os.remove(SV2_CHUNK_SETTINGS_FILE)
        except Exception:
            pass
    st.session_state.pop("_sv2_max_eff_bn",  None)
    st.session_state.pop("_sv2_max_eff_btc", None)
    st.rerun()

# ── Startup debug: is script-run se pehle process kitni purani hai — agar
# _STARTUP_LOG khaali hai to matlab ye is container/process ka BILKUL PEHLA
# run hai (fresh boot / cold start / restart). Agar pehle se lines hain to
# same process continue ho raha hai (sirf Streamlit rerun hua hai).
# NOTE: _is_fresh_boot yahan dobara compute NAHI karna — file ke bilkul
# shuru mein (_slog infra ke turant baad, kisi bhi _slog() call se pehle)
# ek hi baar sahi tarah capture ho chuka hai. Yahan dobara karne se hamesha
# False aata (kyunki beech mein SV2 diagnostic jaisi cheezein already
# _STARTUP_LOG mein likh chuki hoti hain isi run ke andar). ──────────────
_slog(f"▶ Script run start" + (" — FRESH PROCESS BOOT (cold start/restart)" if _is_fresh_boot else " (rerun, same process)"))

# ── Fresh boot par ek baar server-side storage diagnostic bhi log kar do —
# taaki startup log (debug icon se copy hota hai) mein hamesha latest
# /data mount status + saved-files snapshot maujood rahe, bina kisi
# client/JS trigger ke. ─────────────────────────────────────────────────
if _is_fresh_boot:
    try:
        _diag0 = _storage_diagnostic()
        _slog(
            "[StorageDiag] fresh-boot check — "
            f"/data mounted={_diag0['data_mount_exists']} writable={_diag0['data_mount_writable']} | "
            f"LOCAL_STATE_DIR={_diag0['local_state_dir']} (persistent={_diag0['is_persistent_path']}) | "
            f"write/read/delete test={_diag0['write_read_delete_ok']}"
            + (f" error={_diag0['write_read_delete_error']}" if _diag0['write_read_delete_error'] else "")
            + f" | files={_diag0['files']}",
            level="ok" if (_diag0["is_persistent_path"] and _diag0["write_read_delete_ok"]) else "err",
        )
    except Exception as _e_diag0:
        _slog_exception("[StorageDiag] fresh-boot check", _e_diag0)

creds      = load_creds()
_slog(
    "Creds loaded - fyers: app_id=%s secret=%s access_token=%s" % (
        "yes" if creds.get("app_id") else "NO",
        "yes" if creds.get("secret_key") else "NO",
        "yes" if creds.get("access_token") else "NO",
    )
)

try:
    sess_active = is_session_active()
except Exception as _e_sess:
    _slog_exception("is_session_active()", _e_sess)
    sess_active = False
_slog(f"sess_active (Fyers session valid) = {sess_active}")

# ── Teen entry-point flags (mutually exclusive) ─────────────────────────────
# Login page par ab 3 alag buttons hain: Fyers login ke niche "Fyers Entry",
# Binance login ke niche "Binance Entry", aur standalone "Replay Mode". Jo
# bhi ek dabaya jaata hai wahi True set hota hai, baaki do False kar diye
# jaate hain (dekho neeche button-handlers) — isliye ek samay me sirf ek hi
# entry mode active rehta hai, aur sirf usi se related background threads
# start hote hain (chart ka load kam rehta hai).
_fyers_entry_mode   = st.session_state.get("_fyers_entry_mode", False)
_binance_entry_mode = st.session_state.get("_binance_entry_mode", False)
_replay_mode        = st.session_state.get("_replay_mode", False)
# Chart tabhi render hota hai jab teeno mein se koi ek active ho.
_chart_active = _fyers_entry_mode or _binance_entry_mode or _replay_mode
_slog(
    f"_fyers_entry_mode={_fyers_entry_mode}  _binance_entry_mode={_binance_entry_mode}  "
    f"_replay_mode={_replay_mode}  _chart_active={_chart_active}"
)

# ── Update sources — saare 6 sources (bn/btc/nifty500/btc_sv3_1d/sv1_master/
# td_symbols) SIRF `_manual_update_all_sources()` se update hote hain, sirf 💾 debug
# panel ke "Update All Files Now" button se (/api/update_all HTTP route isi function
# ko call karta hai). Koi silent background thread nahi hai.
if _chart_active:
    # Finnhub LIVE WebSocket — Twelve Data symbols (Dow/GBP-USD/Apple/Amazon)
    # ka live price update ab yahan se aata hai, na ki Twelve Data ke slow
    # REST poll se. Chart active ho (Fyers/Binance/Replay, koi bhi) to yeh
    # chalta hai, kyunki TD symbols SV1 mein hamesha dikhte hain, entry-mode
    # se independent.
    try:
        _ensure_finnhub_ws_thread()
    except Exception as _e_fh:
        _slog_exception("_ensure_finnhub_ws_thread()", _e_fh)

# Sirf jo entry-mode active hai usi ke threads start honge — Replay Mode
# mein koi bhi live thread (Fyers ya Binance) start nahi hota, kyunki wo
# purana .gz data se chalta hai, dono APIs se independent.
if _fyers_entry_mode and sess_active:
    _slog(f"Fyers Entry active (sess_active={sess_active}) → calling _ensure_fyers_threads()")
    try:
        _ensure_fyers_threads()
        _running = sorted(t.name for t in threading.enumerate())
        _slog(f"_ensure_fyers_threads() done. Threads alive now: {_running}", level="ok")
    except Exception as _e_threads:
        _slog_exception("_ensure_fyers_threads()", _e_threads)

if _binance_entry_mode:
    _slog("Binance Entry active → calling _ensure_binance_threads()")
    try:
        _ensure_binance_threads()
        _running = sorted(t.name for t in threading.enumerate())
        _slog(f"_ensure_binance_threads() done. Threads alive now: {_running}", level="ok")
    except Exception as _e_threads:
        _slog_exception("_ensure_binance_threads()", _e_threads)

if _replay_mode:
    _slog("Replay Mode active → koi live thread (Fyers/Binance) start NAHI kiya gaya (jaan-boojh kar).")
    # Fyers/Binance data-threads jaan-boojh kar OFF hain, lekin side HTTP server
    # (replay_candles / sv2_data / sv3_symbol / images / music routes) chalna hi
    # zaroori hai — ye koi Fyers/Binance live-data thread nahi hai, sirf saved
    # `.bin` data se JSON serve karta hai.
    try:
        _register_api_route()
    except Exception as _e_api_replay:
        _slog_exception("_register_api_route() (replay mode)", _e_api_replay)

if not (_fyers_entry_mode or _binance_entry_mode or _replay_mode):
    _slog(
        "Koi entry mode active nahi (abhi login page par hai) → "
        "koi bhi background thread is run mein start nahi hua.",
        level="warn",
    )

# ─── Fetch all chart data ─────────────────────────────────────────────────────
# Cache key includes first 8 chars of token so new token → fresh fetch
@st.cache_data(ttl=HIST_CACHE_TTL, show_spinner=False)
def _get_chart_data(sess: bool, _tok: str = ""):
    _tasks = {
        # BTC: 1H is the smallest TF actually used (min TF = 8H, which is an
        # exact multiple of 60min) — client 1H → 8H/1D/3D/9D/27D resample karta hai.
        # BTC history ab BROWSER se aati hai (chart.html: _loadBTCHistory / buildCellChart,
        # Binance REST klines) — server yahan koi Binance call nahi karta (HF se blocked
        # tha, silent [] deta tha). Placeholders khali [] inject hote hain.
        # Live tick abhi bhi server WS hub (btc_kline/btc_trade) se hi.
        "btc_1h":  lambda: [],
        "btc_day": lambda: [],
        # BankNifty: 45m is the smallest TF actually used (min TF = 125m,
        # a multiple of 45m).
        "bn_45m":  (lambda: fetch_bn_intraday(45)) if sess else (lambda: []),
        "bn_day":  load_bn_daily if sess else (lambda: []),
    }
    with ThreadPoolExecutor(max_workers=8) as _ex:
        _futures = {k: _ex.submit(fn) for k, fn in _tasks.items()}
        _out = {k: f.result() for k, f in _futures.items()}
    return (_out["btc_1h"], _out["btc_day"], _out["bn_45m"], _out["bn_day"])

_tok_hint = creds.get("access_token", "")[:8] if sess_active else ""
btc_1h, btc_day, bn_45m, bn_day = _get_chart_data(sess_active, _tok_hint)

# ─── Chart HTML builder — injects live data directly into chart.html ──────────
def _build_chart_html(
    btc_1h, btc_day,
    bn_45m, bn_day,
    sess_active: bool
) -> str:
    """Read chart.html and replace all __PLACEHOLDERS__ with real data."""
    import os, json as _json

    # Load chart.html from same directory as app.py
    _html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart.html")
    if not os.path.exists(_html_path):
        return "<p style='color:red'>chart.html not found</p>"

    with open(_html_path, "r", encoding="utf-8") as _f:
        html = _f.read()

    def _to_lwc(candles: list) -> str:
        """Convert [[epoch_ms, o, h, l, c, v], ...] or [{time,open,...}] to LWC format."""
        out = []
        for b in candles:
            try:
                if isinstance(b, (list, tuple)):
                    t = int(b[0]) // 1000  # ms→sec
                    o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
                    v = float(b[5]) if len(b) > 5 else 0
                else:
                    t = int(b.get("time", 0))
                    o = float(b.get("open",  0))
                    h = float(b.get("high",  0))
                    l = float(b.get("low",   0))
                    c = float(b.get("close", 0))
                    v = float(b.get("volume", 0))
                out.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
            except Exception:
                continue
        # Deduplicate by time, keep last
        seen = {}
        for b in out:
            seen[b["time"]] = b
        return _json.dumps(sorted(seen.values(), key=lambda x: x["time"]))

    # ALLDATA[asset] fallback (__BTC_CANDLES__/__BN_CANDLES__) is only ever
    # touched if tf falls below the base TF, which never happens for either
    # asset in real usage — seed it with the base array itself (no extra
    # fetch) instead of leaving it empty, so the fallback stays harmless.
    html = html.replace("__BTC_CANDLES__", _to_lwc(btc_1h))
    html = html.replace("__BTC_1H__",      _to_lwc(btc_1h))
    html = html.replace("__BTC_DAILY__",   _to_lwc(btc_day))
    html = html.replace("__BN_CANDLES__",  _to_lwc(bn_45m))
    html = html.replace("__BN_45M__",      _to_lwc(bn_45m))
    html = html.replace("__BN_DAILY__",    _to_lwc(bn_day))

    # ── Stack View 2: Python kabhi bhi gz fetch/resample nahi karta.
    # Placeholders hamesha empty inject hote hain; asli data tab load hota hai jab
    # user in-chart calendar se date select/resume kare (dekho chart.html:
    # _sv2EnsureAssetLoaded/_sv2BuildAssetFull).
    _sv2_all_placeholders = [
        "__SV2_BN_5M_RAW__","__SV2_BN_125M__",
        "__SV2_BN_1D__","__SV2_BN_3D__","__SV2_BN_9D__","__SV2_BN_27D__",
        "__SV2_BTC_5M_RAW__","__SV2_BTC_8H__","__SV2_BTC_1D__",
        "__SV2_BTC_3D__","__SV2_BTC_9D__","__SV2_BTC_27D__",
    ]
    for _ph in _sv2_all_placeholders:
        html = html.replace(_ph, "[]")
    _sv2_data_loaded_ok = False
    _sv2_err_msg = "NOT_USED_ANYMORE_CLIENT_SIDE_LOAD"
    # Inject debug info + loaded-flag as JS variables (legacy flag — chart.html
    # ab primarily window.__SV2_LOADED{bn,btc} per-asset object use karta hai)
    _sv2_safe = _sv2_err_msg.replace("</", "<\\/")
    # Bottom-bar "📦 Chunk" icon panel ke liye: current effective candle-count
    # limits (BN + BTC), unki safe bounds, aur current chunk dates.
    _sv2_chunk_ui_info = {
        "bn":            _sv2_get_max("bn"),
        "btc":           _sv2_get_max("btc"),
        "bounds":        _SV2_MAX_BOUNDS,
        "bn_chunk_date":  str(st.session_state.get("_sv2_anchor_date_bn")  or ""),
        "btc_chunk_date": str(st.session_state.get("_sv2_anchor_date_btc") or ""),
    }

    # ── Stack View 3: Nifty500 stock — long-term monthly replay data ────────
    # LAZY LOAD, SV2 jaisa hi: jab tak koi symbol select nahi hota (top-left
    # picker se, ya pehli baar Stack View 3 ON toggle na ho), .gz kabhi
    # padha hi nahi jaata — sirf empty placeholders inject honge. 1D + saari
    # TFs FULL HISTORY jaati hain (ek stock ka 1D max ~30 saal ≈ 7500 rows —
    # BN ke 1D jaisa hi chhota, isliye SV2 ke 5m_raw jaisi anchor-date
    # trimming yahan zaroorat nahi — replay "start point" purely client-side,
    # already-loaded 1D array ke andar ek index hai.)
    _sv3_data_requested = bool(st.session_state.get("_sv3_data_requested"))
    _sv3_symbol_cur = st.session_state.get("_sv3_symbol", "") or ""
    _sv3_all_placeholders = [
        "__SV3_1D__", "__SV3_1M__", "__SV3_3M__", "__SV3_9M__",
        "__SV3_27M__", "__SV3_81M__", "__SV3_243M__",
    ]
    _sv3_err_msg = ""
    _sv3_data_loaded_ok = False
    if not _sv3_data_requested or not _sv3_symbol_cur:
        for _ph in _sv3_all_placeholders:
            html = html.replace(_ph, "[]")
        _sv3_err_msg = "NOT_REQUESTED_YET"
    else:
        try:
            _sv3d = _build_sv3_data(_sv3_symbol_cur)
            html = html.replace("__SV3_1D__",   _sv3_to_js(_sv3d["1D"]))
            html = html.replace("__SV3_1M__",   _sv3_to_js(_sv3d["1M"]))
            html = html.replace("__SV3_3M__",   _sv3_to_js(_sv3d["3M"]))
            html = html.replace("__SV3_9M__",   _sv3_to_js(_sv3d["9M"]))
            html = html.replace("__SV3_27M__",  _sv3_to_js(_sv3d["27M"]))
            html = html.replace("__SV3_81M__",  _sv3_to_js(_sv3d["81M"]))
            html = html.replace("__SV3_243M__", _sv3_to_js(_sv3d["243M"]))
            _sv3_err_msg = json.dumps({
                "symbol": _sv3_symbol_cur,
                "counts": {k: len(v) for k, v in _sv3d.items()},
            })
            _sv3_data_loaded_ok = True
        except Exception as _sv3_ex:
            _sv3_err_msg = f"EXCEPTION: {_sv3_ex} | cache={_SV3_CACHE.get('symbol')}"
            for _ph in _sv3_all_placeholders:
                html = html.replace(_ph, "[]")
    _sv3_safe = _sv3_err_msg.replace("</", "<\\/")
    # CHANGED (user ka explicit ask): pehle yahan _sv3_symbol_list() eagerly
    # call hota tha — matlab chart-page render hote hi Nifty500 naamon ki
    # list fetch ho jaati thi, chahe user symbol-search kholta ya nahi. Ab
    # ye khaali array hi bhejte hain — asli list tabhi fetch hogi jab user
    # khud SV3 symbol-search picker kholega (chart.html: _openSv3SymbolPicker
    # → /api/sv3_symbol_list, dekho neeche local API server mein).
    _sv3_symbol_list_json = "[]"

    # ── App-startup / login debug log — snapshot le lo taaki chart render
    # hone se pehle jitne bhi steps (creds, session check, thread launch,
    # koi exception) hue hain, header ke chhote debug icon se copy kiye
    # ja saken. Render ke turant baad ka bhi ek final marker line daal
    # rahe hain taaki pata chale ye poora startup trace hai.
    try:
        _slog(f"Chart HTML render ho raha hai — sess_active={sess_active} chart_active={_chart_active}", level="ok")
    except Exception:
        pass
    _startup_log_safe = json.dumps(_startup_log_snapshot()).replace("</", "<\\/")
    html = html.replace("</body>",
        f"<script>window.__STARTUP_LOG__={_startup_log_safe};"
        "try{ if (typeof _bootDebugRenderLog === 'function') _bootDebugRenderLog(); }catch(_){}"
        "</script>\n</body>", 1)

    html = html.replace("</body>",
        f"<script>window.__SV2_DEBUG={json.dumps(_sv2_safe)};"
        f"window.__SV2_DATA_LOADED={json.dumps(_sv2_data_loaded_ok)};"
        f"window.__SV2_CHUNK_SETTINGS={json.dumps(_sv2_chunk_ui_info)};</script>\n</body>", 1)

    html = html.replace("</body>",
        f"<script>window.__SV3_DEBUG={json.dumps(_sv3_safe)};"
        f"window.__SV3_DATA_LOADED={json.dumps(_sv3_data_loaded_ok)};"
        f"window.__SV3_SYMBOL={json.dumps(_sv3_symbol_cur)};"
        f"window.__SV3_SYMBOL_LIST={_sv3_symbol_list_json};</script>\n</body>", 1)

    # ── Auto-update status (BankNifty / BTC / Nifty500) — 💾 debug
    # panel mein dikhane ke liye. Fail-safe: status load hi na ho paaye to
    # bhi khaali dict bhej dete hain, panel "no data" dikha dega.
    try:
        _auto_update_status = _load_update_status()
    except Exception:
        _auto_update_status = {}
    _auto_update_today = _ist_now().strftime("%Y-%m-%d")
    html = html.replace("</body>",
        f"<script>window.__AUTO_UPDATE_STATUS={json.dumps(_auto_update_status)};"
        f"window.__AUTO_UPDATE_TODAY={json.dumps(_auto_update_today)};</script>\n</body>", 1)

    # ── Twelve Data external symbols (Gold/Dow Jones/...) — SV1 ke top-left
    # symbol switcher ke liye generic data. Registry (td_symbols.py) mein
    # jitne bhi symbols hon, ye poore inject ho jaate hain — chart.html
    # generically loop karke unhe ASSET_NAMES mein add karta hai, kahin
    # per-symbol hardcoded code nahi hai. Fail-safe: kuch bhi fail ho to
    # khaali dict — matlab koi external symbol nahi dikhega, BN/BTC untouched.
    try:
        _td_symbols_payload = _td.td_all_bucketed()
    except Exception as _e_td_inject:
        _slog_exception("td_all_bucketed (chart html inject)", _e_td_inject)
        _td_symbols_payload = {}
    # ── Replay symbols (btcusdt2/3/4/5) — HF disk (master file + reveal_state.json) se raw+reveal
    # data laata hai, TD symbols jaisa hi shape ({label,bucket_min,market,
    # candles}) deta hai, isliye seedha TD wale dict mein MERGE kar dete
    # hain — chart.html ka existing generic ext_<key> pipeline (ASSET_NAMES,
    # STACK config, symbol-switcher, 1D/3D/9D/27D resample) inhe automatically
    # handle kar lega, koi chart.html change nahi chahiye. Fail-safe: kuch
    # bhi fail ho to kuch add nahi hoga, TD symbols/BTC/BN untouched rahenge.
    # v3 — ON-DEMAND: page-load par ab candles NAHI bhejte (pehle charo
    # symbols ka poora data yahin ban ke chala jaata tha — ~67MB tak ka
    # payload ek symbol ke liye ho sakta tha, aur 1-saal pre-history cap
    # ki wajah se bade-timeframe anchoring bhi mismatch karti thi symbol-
    # se-symbol). Ab sirf halka metadata ({label,bucket_min,market},
    # candles: []) jaata hai — sirf itna kaafi hai symbol-switcher mein
    # naam dikhane ke liye. Jab user in 4 mein se koi symbol select karega,
    # chart.html naya /api/replay_symbol side-API route call karke us
    # symbol ka poora (anchor-consistent) data fresh fetch karega.
    try:
        _replay_payload = _replay.get_replay_symbols_metadata()
        _td_symbols_payload.update(_replay_payload)
    except Exception as _e_replay_inject:
        _slog_exception("replay_symbols metadata inject", _e_replay_inject)
    # (2026-09-19) Startup par restored (last-used) replay-symbol ka data
    # page ke saath hi embed — extra round-trip / 60s-timeout
    # ki zaroorat nahi. Last asset server ke apne local-state bundle
    # ("state".asset) se aata hai (chart.html wahi bundle localStorage par
    # chadhata hai). Master RAM mein warm na ho ya reveal_state na mile to
    # kuch inject nahi hota (page build kabhi block nahi hota) — chart.html
    # ka auto-poll fallback data laa deta hai. Max 4 symbols (asset + cells).
    try:
        _st_bundle = (_local_state_load_all() or {}).get("state")
        if isinstance(_st_bundle, dict):
            _want_assets = [_st_bundle.get("asset")]
            for _c in (_st_bundle.get("cells") or []):
                if isinstance(_c, dict):
                    _want_assets.append(_c.get("asset"))
            _label_to_key = {_v.get("label"): _k for _k, _v in _replay_payload.items()}
            _inj_keys = []
            for _a in _want_assets:
                _k = _label_to_key.get(_a)
                if _k and _k not in _inj_keys:
                    _inj_keys.append(_k)
            for _k in _inj_keys[:4]:
                _inj = _replay.replay_get_inject_payload(
                    _k, log_fn=_slog
                )
                if _inj:
                    _td_symbols_payload[_k].update(_inj)
    except Exception as _e_replay_pre:
        _slog_exception("replay restored-symbol pre-inject", _e_replay_pre)
    # NOTE: this must run BEFORE chart.html's main inline <script> (which builds
    # ASSET_NAMES from window.__TD_SYMBOLS_DATA__ at parse time) — so injected
    # right after <head>, not at </body>. Injecting at </body> made the data
    # arrive too late: ASSET_NAMES had already been built with an empty
    # fallback, so Gold/Dow never showed up in SV1's top-left symbol switcher,
    # only BankNifty/BTC.
    html = html.replace("<head>",
        f"<head>\n<script>window.__TD_SYMBOLS_DATA__={json.dumps(_td_symbols_payload)};</script>", 1)

    # ── Local persistent-storage LOAD — bundle build-time par yahan seedha inject
    # hota hai (bilkul __TD_SYMBOLS_DATA__ jaisa pattern): koi extra network
    # round-trip nahi, chart.html isi HTML ke saath aa jaata hai.
    try:
        _local_bundle = _local_state_load_all()
    except Exception as _e_local_bundle:
        _slog_exception("_local_state_load_all() inject", _e_local_bundle)
        _local_bundle = {}
    # (2026-09-21) diary me user ka free text hai — "</script>" / "<!--" jaisa kuch likha ho to inline <script> na tute,
    # isliye JSON ke andar `</` aur `<!--` escape karte hain (JS string me `<\/` == `</`).
    _bundle_js = json.dumps(_local_bundle).replace("</", "<\\/").replace("<!--", "<\\!--")
    html = html.replace("<head>",
        f"<head>\n<script>window.__LOCAL_STATE_BUNDLE__={_bundle_js};</script>", 1)

    # (2026-09-20) HF /data ke per-kind save-timestamps (file mtime, ms) —
    # chart.html restore-time par isse localStorage ke apne saved-at se
    # compare karke tay karta hai ki state/settings/layout kis taraf (HF ya
    # local) se restore karna hai, jo bhi zyada recent ho. Pehle rule
    # hardcoded "HF hamesha jeetega" tha, jisse HF save ack-pending/stale
    # ho to fresh local drawing overwrite ho jaati thi.
    try:
        _local_mtimes = _local_state_mtimes()
    except Exception as _e_local_mtimes:
        _slog_exception("_local_state_mtimes() inject", _e_local_mtimes)
        _local_mtimes = {}
    html = html.replace("<head>",
        f"<head>\n<script>window.__LOCAL_STATE_SAVED_AT__={json.dumps(_local_mtimes)};</script>", 1)

    # ── Inject side-API port so chart.html knows which port to call ──────────
    _api_port = 0
    try:
        if os.path.exists(".api_port"):
            with open(".api_port") as _pf:
                _api_port = int(_pf.read().strip())
    except Exception:
        _api_port = 0
    html = html.replace("__API_PORT__", str(_api_port))

    return html


# ─── Main area: embed chart directly (no separate API server needed) ─────────
st.markdown("## 📊 BankNifty Live Chart")

# ── Entry-mode flags (_fyers_entry_mode / _binance_entry_mode / _replay_mode)
# ── already defined early, right after sess_active. _chart_active bhi wahin
# defined hai — teeno mein se koi ek active ho to chart render hota hai.

# SV2 (BankNifty/BTC replay) data lazy-load hai — tabhi load hota hai jab user
# in-chart calendar se date select/resume kare (dekho chart.html:
# _sv2EnsureAssetLoaded / _sv2BuildAssetFull); yahan koi session_state flag set nahi hota.
_lt_replay_mode = bool(st.session_state.get("_lt_replay_mode"))

# SV3 (Nifty500 stocks) data bhi lazy-load hai — tabhi hota hai jab user top-left
# symbol-picker se symbol chune (dekho chart.html: _sv3SelectSymbol). "Enter Chart
# Mode" turant khulta hai; SV3 hissa picker-prompt state mein rehta hai.

if _chart_active:
    # (2026-10-02, Step 2) Purana "warming up" banner hata diya — master ab
    # RAM me load hota hi nahi (`.bin` memmap), isliye koi warm-up nahi.
    if not sess_active:
        if _replay_mode and _lt_replay_mode:
            st.success("🚀 Chart Mode active — BTC/BankNifty aur Nifty500 stocks, symbol select karte hi on-demand load honge")
        elif _replay_mode:
            st.success("📼 Replay Mode active — BTC/BankNifty data date/symbol select karte hi on-demand load hoga")
        elif _binance_entry_mode:
            st.info("🟡 Binance Chart mode — BankNifty data available nahi (Fyers login nahi hai)")

    _chart_html = _build_chart_html(
        btc_1h, btc_day,
        bn_45m, bn_day,
        sess_active,
    )
    components.html(_chart_html, height=950, scrolling=False)

    # ── Combined Live-Data Pusher (option chain, meta, balance, depth) ──────
    # Ek hi @st.fragment(run_every=1) sab data ikattha karta hai aur EK hi
    # <script> block mein saare postMessage bhej deta hai — sirf ek iframe
    # mount/sec. Har message apne 'type' ke saath aata hai. 5s-cadence wali
    # cheezein (fyers-meta, binance-meta) counter se skip hoti hain taaki
    # unki API-cost badhe nahi.
    st.session_state.setdefault("_live_pusher_tick", 0)

    if _chart_active:
        @st.fragment(run_every=1)
        def _live_data_pusher():
            st.session_state["_live_pusher_tick"] += 1
            _tick_n = st.session_state["_live_pusher_tick"]
            _do_5s  = (_tick_n % 5 == 0)   # har 5th run par hi 5s-cadence wali cheezein refresh

            _messages = []  # list of (type, payload_dict)

            # Fyers: option chain (1s) + balance/meta (5s)
            if sess_active:
                _oc = get_cached_option_chain_payload()
                if not _oc:
                    _oc = {"error": "Kuch data nahi mila (unknown reason)"}
                _messages.append(("option_chain", _oc))

                if _do_5s:
                    _messages.append(("fyers_meta", refresh_fyers_meta_cache()))

            # Market Depth (BTC/NSE) — backend yahan sirf heartbeat bhejta hai
            # (debug-panel/bridge health ke liye), koi Binance/Fyers call nahi.
            global _MD_PUSHER_DEBUG
            _MD_PUSHER_DEBUG["runs"] += 1
            _dsym = st.session_state.get("_active_depth_symbol")
            _MD_PUSHER_DEBUG["last_active_symbol"] = _dsym
            _MD_PUSHER_DEBUG["last_run_ts"] = time.time()
            _dpayload = {
                "_heartbeat": True,
                "active_symbol_on_backend": _dsym,
                "runs": _MD_PUSHER_DEBUG["runs"],
            }
            _messages.append(("market_depth", _dpayload))

            # ── Local persistent-storage SAVE ack — last save ka result postMessage
            # ke through JS ko bhejte hain (debug panel / save-health ke liye). ──
            # (2026-09-20) per-kind acks (seq + persistent ke saath), har tick repeat, 5 min tak — browser
            # ka save-health/exit-signal inhi se "HF ne sach me save kiya" confirm karta hai.
            _ls_acks = st.session_state.get("_local_save_acks") or {}
            _ls_now = time.time()
            for _ack in list(_ls_acks.values()):
                if _ls_now - float(_ack.get("ts", 0)) <= 300:
                    _messages.append(("local_save_ack", _ack))
            if not _ls_acks:
                _ls_last = st.session_state.get("_local_save_last")
                if _ls_last:
                    _messages.append(("local_save_ack", _ls_last))

            # ── Bridge diagnostic — har tick pe bhejte hain taaki client debug
            # panel Python → chart.html postMessage bridge ki health dikha sake. ──
            _messages.append(("bridge_debug", {
                "handler6_fires": st.session_state.get("_handler6_fire_count", 0),
                "pusher_ticks": _tick_n,
                # (2026-09-20) HF /data persistent hai ya ephemeral fallback — browser ka exit-signal isse red karta hai
                "data_persistent": bool(str(LOCAL_STATE_DIR).startswith("/data")),
            }))

            # ── SIMPLE HELLO TEST — sirf ye confirm karne ke liye ki
            # Python → chart.html postMessage channel (jo bridge_debug/
            # market_depth pehle se use karte hain) sahi kaam kar raha hai.
            # Isse app.py se text badal ke seedha debug panel mein dikhega,
            # koi save/restore logic ise touch nahi karti. ──
            _messages.append(("hello_test", {
                "text": "Hello from app.py 👋",
                "ts": time.time(),
                "tick": _tick_n,
            }))

            # ── Twelve Data external symbols (Gold/Dow Jones/...) — LIVE
            # ab Finnhub WebSocket se aata hai (dekho _finnhub_ws_loop),
            # yahan sirf jo keys abhi-abhi tick se update hui hain unhe
            # browser ko push karte hain — poori registry scan nahi karni
            # padti. ~2s cadence rakha hai (har tick pe nahi) taaki chart
            # rebuild (JS side full-candles replace + redraw) bahut zyada
            # baar-baar na ho — Finnhub push khud milliseconds-level hai,
            # display-refresh 2s kaafi hai "real live" feel ke liye.
            if _tick_n % 2 == 0:
                try:
                    for _td_key in _td.td_pop_dirty_keys():
                        _messages.append(("td_live", {
                            "key": _td_key,
                            "candles": _td.td_get_bucketed(_td_key),
                            # Update Source Tracer tag (2026-09-17) — chart.html
                            # ka top-header 🕵️ icon isi field se batata hai
                            # EXACTLY kaunsa app.py function/block ne push
                            # bheji. Twelve Data external symbols (Gold/Dow/…)
                            # ke liye normal hai — agar replay keys
                            # (btcusdt2-133) yahan dikhein to matlab wo galti
                            # se `_td` registry mein bhi register ho gayi hain.
                            "_src": "app.py:_live_data_pusher:td_pop_dirty_keys (Finnhub-WS-driven, 2s cadence)",
                        }))
                except Exception as _e_fh_push:
                    _slog_exception("td_pop_dirty_keys push", _e_fh_push)

            # ── Twelve Data REST poll — ab sirf SLOW BACKUP/reconciliation
            # hai (Finnhub WS drop/gap ho jaaye to bhi data stale na rahe).
            # auto-scaling interval (registry-size ke hisaab se, free-plan
            # rate-limit respect karte hue — dekho td_recommended_poll_
            # interval_sec). Har tick pe check, lekin actual API call sirf
            # jab interval poora ho. ──────────────────────────────────────
            _td_interval = _td.td_recommended_poll_interval_sec()
            if _tick_n % _td_interval == 0:
                try:
                    _td_updated = _td.td_live_poll_batch(TWELVEDATA_API_KEY, log_fn=_slog)
                    for _td_key in _td_updated:
                        _messages.append(("td_live", {
                            "key": _td_key,
                            "candles": _td.td_get_bucketed(_td_key),
                            # Update Source Tracer tag — TwelveData ka REST
                            # backup-poll (auto-scaling interval, slow
                            # reconciliation). Replay keys (btcusdt2-133)
                            # yahan tabhi dikhengi jab wo galti se `_td`
                            # registry mein bhi register ho gayi hon.
                            "_src": "app.py:_live_data_pusher:td_live_poll_batch (TwelveData REST backup poll)",
                        }))
                except Exception as _e_td:
                    _slog_exception("td_live_poll_batch", _e_td)

            # ── Ek hi script mein saare postMessage bhejo ────────────────────
            _posts = "\n".join(
                "  try { frames[i].contentWindow.postMessage(JSON.stringify(%s), '*'); } catch(e) {}"
                % json.dumps({"type": _mtype, "data": _mdata})
                for _mtype, _mdata in _messages
            )
            _combined_script = f"""
<script>
(function() {{
  var frames = window.parent.document.querySelectorAll('iframe');
  for (var i = 0; i < frames.length; i++) {{
{_posts}
  }}
}})();
</script>
"""
            components.html(_combined_script, height=0, scrolling=False)

        _live_data_pusher()

else:
    # ─── Main area inline Login Panel ─────────────────────────────────────────
    _creds_main = load_creds()
    _has_old    = bool(_creds_main.get("access_token")) and not sess_active
    _app_id_m   = _creds_main.get("app_id",    DEFAULT_APP_ID)
    _secret_m   = _creds_main.get("secret_key", DEFAULT_SECRET)
    import random as _rand
    _nonce_m = str(int(time.time())) + str(_rand.randint(1000, 9999))
    _auth_url_m = (
        f"https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={_app_id_m}"
        f"&redirect_uri=https%3A%2F%2Fwww.google.com"
        f"&response_type=code"
        f"&state={_nonce_m}"
        f"&nonce={_nonce_m}"
    )

    st.markdown("""
    <style>
    .login-card{
        background:#1e222d;border:1px solid #2a2e3e;border-radius:14px;
        padding:18px 22px;max-width:620px;margin:12px auto;
    }
    .login-title{color:#e0e3eb;font-size:1.5rem;font-weight:700;margin-bottom:4px;}
    .login-sub{color:#848da0;font-size:0.9rem;margin-bottom:24px;}
    .method-label{
        color:#a3aabf;font-size:0.78rem;font-weight:600;letter-spacing:.08em;
        text-transform:uppercase;margin-bottom:8px;
    }
    .step-badge{
        background:#1a73e8;color:#fff;border-radius:50%;
        width:22px;height:22px;display:inline-flex;align-items:center;
        justify-content:center;font-size:.75rem;font-weight:700;margin-right:8px;
    }
    /* Login page ke andar buttons/inputs ke beech ka default Streamlit gap
       kam karo — har card mein 5-10 widgets hote hain, default ~1rem gap
       se poori page bahut lambi ho jaati thi. */
    div[data-testid="stVerticalBlock"]{gap:0.5rem;}
    </style>
    """, unsafe_allow_html=True)

    # ── TOP "Enter" button — login page ke sabse upar. Fyers/Binance login
    # ho ya na ho, seedha chart mode mein le jaata hai.
    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">🚀 Enter</div>''', unsafe_allow_html=True)
    st.markdown(
        "<div style='color:#848da0;font-size:0.82rem;margin-bottom:14px;'>"
        "Login ho ya na ho — seedha chart mode kholo. BTC/BankNifty aur "
        "Nifty500 stocks, dono ka replay data chart ke andar hi available "
        "rahega.</div>",
        unsafe_allow_html=True,
    )
    if st.button("🚀 Enter Chart Mode", use_container_width=True, key="top_enter_chart_btn"):
        _slog("👉 'Enter Chart Mode' (top button) clicked → _replay_mode=True, _lt_replay_mode=True set kiya, st.rerun().")
        st.session_state["_replay_mode"]        = True
        st.session_state["_lt_replay_mode"]     = True
        st.session_state["_fyers_entry_mode"]   = False
        st.session_state["_binance_entry_mode"] = False
        # (2026-10-07) Common-button fix: chart abhi bhi Replay Mode me khulega
        # (UI/data-source nahi badla), lekin background me Fyers + Binance dono
        # ke live-data threads bhi yahin se start kar do — taaki is EK button se
        # dono tarah ka live data bhi chalta rahe. Fyers thread sirf tab start
        # hoga jab login session valid ho (sess_active) — ye hard requirement hai,
        # isse bypass nahi kiya ja sakta.
        try:
            if sess_active:
                _slog("'Enter Chart Mode' → sess_active=True → calling _ensure_fyers_threads() bhi")
                _ensure_fyers_threads()
            else:
                _slog("'Enter Chart Mode' → sess_active=False → Fyers threads SKIP (login session valid nahi)", level="warn")
        except Exception as _e_ecm_fy:
            _slog_exception("'Enter Chart Mode' _ensure_fyers_threads()", _e_ecm_fy)
        try:
            _slog("'Enter Chart Mode' → calling _ensure_binance_threads() bhi")
            _ensure_binance_threads()
        except Exception as _e_ecm_bn:
            _slog_exception("'Enter Chart Mode' _ensure_binance_threads()", _e_ecm_bn)
        st.rerun()
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── 🎥 YouTube → HF Storage (2026-10-05) — Enter card ke turant baad, top par ──
    # Backend: _ytdl_start / _ytdl_run (app.py me upar, Personal Backup helpers ke baad).
    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown(
        '''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">🎥 YouTube → HF Storage
        <span style="font-size:0.75rem;color:#555;font-weight:400;">(video seedha /data/video me — phone/PC par kuch download nahi)</span></div>''',
        unsafe_allow_html=True,
    )
    _ye = _ytdl_env()
    st.markdown(
        "<div style='color:#848da0;font-size:0.8rem;margin-bottom:8px;line-height:1.7;'>"
        f"{'✅' if _ye['ytdlp'] else '❌'} yt-dlp {_ye['ytdlp'] or '(installed nahi)'} &nbsp;|&nbsp; "
        f"{'✅' if _ye['ffmpeg'] else '⚠️'} ffmpeg{'' if _ye['ffmpeg'] else ' (nahi — sirf ~360p)'} &nbsp;|&nbsp; "
        f"{'✅' if _ye['js'] else '⚠️'} JS runtime {_ye['js'] or '(nahi — kuch formats gayab ho sakte)'} &nbsp;|&nbsp; "
        f"{'🍪 cookies laga hai' if _ye['cookies'] else '⚪ cookies nahi'} &nbsp;|&nbsp; "
        f"{'✅' if _ye['warp'] else '⚠️'} WARP{'' if _ye['warp'] else ' (installed nahi)'}"
        f"{' &nbsp;|&nbsp; ✅ paid proxy set' if _YTDL_PROXY else ''}</div>",
        unsafe_allow_html=True,
    )

    _yt_url = st.text_input("YouTube link (video ya playlist)", key="yt_dl_url",
                            placeholder="https://www.youtube.com/watch?v=....")
    _yt_folders = [f for f in _gd_list_dest_folders() if f == "video" or f.startswith("video/")] or ["video"]
    _yt_dest = st.selectbox("📂 Kahan save karu?", options=_yt_folders, index=0, key="yt_dl_dest")
    _yt_sub = st.text_input("➕ Naya sub-folder (optional)", key="yt_dl_sub", placeholder="e.g. punjabi_songs")
    _yt_c1, _yt_c2 = st.columns(2)
    with _yt_c1:
        _yt_q = st.selectbox("Quality", options=list(_YTDL_QUALITIES), index=0,
                             format_func=lambda h: f"{h}p", key="yt_dl_q")
    with _yt_c2:
        st.markdown("<div style='height:1.9rem;'></div>", unsafe_allow_html=True)
        _yt_pl = st.checkbox("Poori playlist", value=False, key="yt_dl_pl")

    if st.button("⬇️ Download Shuru Karo (background me chalega)", use_container_width=True,
                 type="primary", key="yt_dl_btn"):
        _slog(f"👉 'YouTube Download' clicked — dest={_yt_dest!r} sub={_yt_sub!r} {_yt_q}p playlist={_yt_pl} url={_yt_url}")
        _yt_ok, _yt_msg = _ytdl_start(_yt_url, _yt_dest, _yt_sub.strip(), int(_yt_q), bool(_yt_pl))
        if _yt_ok:
            st.success(f"✅ {_yt_msg}")
        else:
            st.warning(f"⚠️ {_yt_msg}")

    # ── Live status — fragment sirf tab har 2s refresh hota hai jab koi job queued/running ho ──
    _yt_any_active = any(j["state"] in ("queued", "running") for j in list(_YTDL["jobs"].values()))

    @st.fragment(run_every=(2 if _yt_any_active else None))
    def _yt_jobs_view():
        _jobs = sorted(list(_YTDL["jobs"].values()), key=lambda j: j["created"], reverse=True)
        _active_now = any(j["state"] in ("queued", "running") for j in _jobs)
        if not _jobs:
            st.caption("Abhi koi download nahi.")
        for _j in _jobs:
            _icon = {"queued": "🕒", "running": "⏳", "done": "✅", "error": "❌", "cancelled": "⛔"}.get(_j["state"], "•")
            _title = (_j.get("title") or _j["url"])[:70]
            _extra = ""
            if _j["state"] == "running":
                if _j.get("n"):
                    _extra += f" · video {_j.get('idx') or '?'}/{_j['n']}"
                if _j.get("speed"):
                    _extra += f" · {_fmt_bytes(int(_j['speed']))}/s"
                if _j.get("eta") is not None:
                    _extra += f" · ETA {int(_j['eta'])}s"
                if _j.get("via"):
                    _extra += f" · route: {_j['via']}"
            st.markdown(f"{_icon} **{_title}** — {_j['state']} · {_j['height']}p{_extra}")
            if _j["state"] == "running" and _j.get("pct") is not None:
                st.progress(int(max(0, min(100, _j["pct"]))))
            if _j["state"] in ("queued", "running"):
                if st.button("⛔ Cancel", key=f"yt_cancel_{_j['id']}"):
                    _ytdl_cancel(_j["id"])
            if _j["state"] == "done":
                st.caption(f"{_j['files']} file(s) → `{_j['dest_dir']}` — video player me 🔄 dabao"
                           + (f" · {_j['last_file']}" if _j.get("last_file") else "")
                           + (f" · ✅ route: {_j['via']}" if _j.get("via") else ""))
                if _j.get("tries"):
                    st.caption("Pehle fail hue: " + " | ".join(_j["tries"]))
            if _j.get("warn") and _j["state"] != "error":
                st.caption(f"⚠️ {_j['warn']}")
            if _j["state"] == "error":
                st.error(_j.get("error") or "download fail")
                for _tr in (_j.get("tries") or []):
                    st.caption("❌ " + _tr)
                if _j.get("detail"):
                    with st.expander("🔍 Exact error", expanded=False):
                        st.code(_j["detail"], language="text")
        if _yt_any_active and not _active_now:
            st.rerun(scope="app")      # sab job khatam — run_every band karne ke liye ek full rerun

    _yt_jobs_view()

    if st.button("🔬 Connectivity test (direct vs WARP) — bina download", key="yt_probe_btn"):
        with st.spinner("Test chal raha hai (~1 min)…"):
            _pr = _ytdl_probe()
        st.code("\n".join(_pr), language="text")

    with st.expander("🍪 Cookies (optional — sirf tab jab YouTube 'bot' bol ke rok de)"):
        st.caption("cookies.txt aapke Google account ka access hota hai. Sirf zaroorat par daalo, behtar hai alag/secondary account ki ho. "
                   "Browser extension (jaise 'Get cookies.txt LOCALLY') se youtube.com ki Netscape-format file export karo.")
        _yt_ck = st.file_uploader("cookies.txt", type=["txt"], key="yt_cookies_up")
        if _yt_ck is not None and st.button("💾 Cookies save karo", key="yt_cookies_save"):
            _ck_ok, _ck_msg = _ytdl_save_cookies(_yt_ck.getvalue())
            (st.success if _ck_ok else st.error)(("✅ " if _ck_ok else "❌ ") + _ck_msg)
        if _ye["cookies"] and st.button("🗑 Cookies hatao", key="yt_cookies_del"):
            _ytdl_delete_cookies()
            st.success("✅ Cookies hata di gayi.")

    try:
        _yt_du = shutil.disk_usage(_storage_root())
        st.markdown(
            f"<div style='color:#848da0;font-size:0.78rem;margin-top:6px;'>Disk free: <b>{_fmt_bytes(_yt_du.free)}</b> / total {_fmt_bytes(_yt_du.total)}</div>",
            unsafe_allow_html=True,
        )
    except Exception:
        pass
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Alert Test — Enter card ke turant baad, top par hi ──────────────────
    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">🔔 Alert Test</div>''', unsafe_allow_html=True)
    _ch_email_m = bool(BREVO_API_KEY and ALERT_EMAIL_TO and BREVO_SENDER_EMAIL)
    _ch_tg_m    = bool((TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) or (TELEGRAM_RELAY_URL and RELAY_SECRET))
    _ch_sms_m   = bool(FAST2SMS_KEY)
    st.markdown(
        "<div style='color:#848da0;font-size:0.82rem;margin-bottom:14px;'>"
        f"{'✅' if _ch_email_m else '⚪'} Email (Brevo)"
        + (f" → {ALERT_EMAIL_TO}" if _ch_email_m else " — secrets set nahi")
        + f" &nbsp;|&nbsp; {'✅' if _ch_tg_m else '⚪'} Telegram"
        + f" &nbsp;|&nbsp; {'✅' if _ch_sms_m else '⚪'} SMS"
        + "</div>",
        unsafe_allow_html=True,
    )
    if st.button("📨 Send Test Alert", use_container_width=True, key="test_alert_main_btn"):
        _ok_m = send_alert("Test", "Ye test alert hai. Agar ye mila to alert setup sahi chal raha hai.", force=True)
        if _ok_m:
            st.success("Bhej diya — email/Telegram check karo (Spam folder bhi).")
        else:
            st.error("Koi channel se nahi gaya.")
    _last_m = _ALERT_LAST_RESULT
    if _last_m.get("channels"):
        for _name_m, _res_m in _last_m["channels"].items():
            st.caption(f"{_name_m}: {_res_m}")
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Render Status dashboard — RenderPinger thread ki live state ─────────
    import html as _hm_rd
    def _rd_dur(sec: float) -> str:
        sec = int(max(0, sec))
        if sec < 60:
            return f"{sec}s"
        if sec < 3600:
            return f"{sec // 60}m {sec % 60}s"
        if sec < 86400:
            return f"{sec // 3600}h {(sec % 3600) // 60}m"
        return f"{sec // 86400}d {(sec % 86400) // 3600}h"
    _rd_alive = any(t.name == "RenderPinger" for t in threading.enumerate())
    _rd_rows = []
    for _rd_url in _render_ping_urls():
        _rd_st = _RENDER_PING_STATE.get(_rd_url)
        _rd_host = _hm_rd.escape(_rd_url.replace("https://", "").replace("http://", ""))
        if not _rd_st or _rd_st.get("ok") is None:
            _rd_rows.append(
                f"<div style='padding:8px 0;border-top:1px solid #2a2e39;'>⚪ <b>{_rd_host}</b>"
                "<div style='color:#848da0;font-size:0.78rem;'>Abhi pehli ping baaki hai</div></div>")
            continue
        _rd_up = bool(_rd_st["ok"])
        _rd_chk = datetime.datetime.fromtimestamp(_rd_st["last_check"], tz=IST).strftime("%I:%M:%S %p")
        _rd_pct = 100.0 * (_rd_st["total"] - _rd_st["bad"]) / max(1, _rd_st["total"])
        _rd_rows.append(
            f"<div style='padding:8px 0;border-top:1px solid #2a2e39;'>"
            f"{'🟢' if _rd_up else '🔴'} <b>{_rd_host}</b>"
            f"<div style='color:{'#26a69a' if _rd_up else '#ef5350'};font-size:0.8rem;'>"
            f"{'UP' if _rd_up else 'DOWN'} {_rd_dur(time.time() - _rd_st['since'])} se"
            f" &nbsp;|&nbsp; {_hm_rd.escape(_rd_st['why'])} &nbsp;|&nbsp; {_rd_st['ms']} ms</div>"
            f"<div style='color:#848da0;font-size:0.76rem;'>Aakhri check {_rd_chk}"
            f" &nbsp;|&nbsp; lagataar fail {_rd_st['fails']}"
            f" &nbsp;|&nbsp; success {_rd_pct:.1f}% ({_rd_st['total']} checks)</div></div>")
    # ── Pichle 7 din ka record + recent events (HF /data ki files se — restart ke baad bhi) ──
    _rd_hist = ""
    try:
        _rd_daily = _rp_read_daily()
        _rd_days = sorted(_rd_daily.keys())[-7:]
        _rd_hist_rows = []
        for _rd_url in _render_ping_urls():
            _tot = _bad = _okn = _msum = _slow = _outs = 0
            for _d in _rd_days:
                _b = _rd_daily.get(_d, {}).get(_rd_url)
                if not _b:
                    continue
                _tot += _b.get("total", 0); _bad += _b.get("bad", 0); _okn += _b.get("ok_n", 0)
                _msum += _b.get("ms_sum", 0); _slow += _b.get("slow", 0); _outs += _b.get("outages", 0)
            if _tot:
                _rd_hist_rows.append(
                    f"<div style='color:#848da0;font-size:0.76rem;padding:2px 0;'>"
                    f"{_hm_rd.escape(_rd_url.replace('https://', ''))}: success {100.0 * (_tot - _bad) / _tot:.2f}%"
                    f" &nbsp;|&nbsp; avg {int(_msum / max(1, _okn))} ms"
                    f" &nbsp;|&nbsp; {_outs} outage &nbsp;|&nbsp; {_slow} slow</div>")
        _rd_ev_rows = []
        for _ev in _rp_read_events(6):
            _k = _ev.get("kind", "")
            _ic = {"DOWN": "🔴", "UP": "🟢", "SLOW": "🐢", "PINGER_START": "🔁"}.get(_k, "•")
            _x = ""
            if _k == "UP" and _ev.get("down_for_sec") is not None:
                _x = f" ({_rd_dur(_ev['down_for_sec'])} down raha)"
            elif _k in ("DOWN", "SLOW"):
                _x = f" ({_hm_rd.escape(str(_ev.get('why', '')))}" + (f", {_ev['ms']} ms" if _k == "SLOW" else "") + ")"
            _rd_ev_rows.append(
                f"<div style='color:#848da0;font-size:0.74rem;'>{_ic} {_hm_rd.escape(str(_ev.get('ist', '')))} "
                f"{_k} {_hm_rd.escape(str(_ev.get('url', '')).replace('https://', ''))}{_x}</div>")
        if _rd_hist_rows or _rd_ev_rows:
            _rd_hist = ("<div style='padding:8px 0;border-top:1px solid #2a2e39;'>"
                        "<div style='font-size:0.8rem;'>📊 Pichle 7 din</div>" + "".join(_rd_hist_rows)
                        + ("<div style='font-size:0.8rem;margin-top:6px;'>📜 Recent events</div>" + "".join(_rd_ev_rows) if _rd_ev_rows else "")
                        + "</div>")
    except Exception:
        _rd_hist = ""
    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">🛰️ Render Status</div>''', unsafe_allow_html=True)
    st.markdown(
        "<div style='color:#848da0;font-size:0.82rem;margin-bottom:6px;'>"
        "Render ki free services 15 min idle rehne par so jaati hain. Ye HF app unhe har "
        f"{_RENDER_PING_EVERY_SEC}s ping karke jagaye rakhta hai. Kisi service se {_RENDER_DOWN_AFTER_SEC}s tak success na aaye to "
        "email + Telegram alert aata hai, wapas theek hone par bhi."
        f"<br>Pinger thread: {'✅ chal raha hai' if _rd_alive else '🔴 band hai'}"
        "</div>" + "".join(_rd_rows) + _rd_hist,
        unsafe_allow_html=True,
    )
    st.button("🔄 Refresh", use_container_width=True, key="render_status_refresh_btn")
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Network Diagnostic — SERVER-side (HF Space) ─────────────────────────
    # Phone-browser wale ws-test.html jaisa hi test, bas ye HF Space ke
    # SERVER se chalta hai (Render relay bypass karke seedha Binance REST +
    # WS domains hit karta hai). Dono result (phone vs server) compare karke
    # pata chalta hai block sirf phone/carrier-level hai ya HF-datacenter
    # level bhi.
    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">🔌 Network Diagnostic (Server-side)</div>''', unsafe_allow_html=True)
    st.markdown(
        "<div style='color:#848da0;font-size:0.82rem;margin-bottom:14px;'>"
        "Ye HF Space ke SERVER se test karta hai (phone-browser se nahi) — "
        "Render relay REST, aur seedha (Render-bypass) Binance REST + WS "
        "(spot/futures/options). Phone ke ws-test.html result se compare karo."
        "</div>",
        unsafe_allow_html=True,
    )
    if st.button("▶ Server-side Diagnostic Chalao", use_container_width=True, key="net_diag_btn"):
        with st.spinner("Test ho raha hai… (WS handshakes ke liye ~40s tak lag sakte hain)"):
            st.session_state["_net_diag_result"] = _run_network_diagnostic()
    if st.session_state.get("_net_diag_result"):
        st.markdown(st.session_state["_net_diag_result"])
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Binance Block Test (JSON) — "kya Binance HF se waqai blocked hai?" ──
    # DNS/TCP/TLS/HTTP/WS layer-by-layer + controls (Google, Bybit, OKX,
    # Coinbase, Kraken). Output pure JSON, sabse upar summary.likely_cause + verdict.
    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">🕵️ Binance Block Test (JSON)</div>''', unsafe_allow_html=True)
    st.markdown(
        "<div style='color:#848da0;font-size:0.82rem;margin-bottom:14px;'>"
        "HF server se seedha Binance (koi Render/relay nahi): spot live price, candles, spot depth, "
        "option chain WS, options depth WS — har ek ka WORKS/NOT + exact wajah (451 block / 404 galat URL / "
        "no-data), trading-readiness (fake key se), 15s feed stability, aur app ke apne source ke saare Binance/Render URLs ka audit. "
        "Controls (Google, Bybit, OKX…) saath me. ~50-80s lagte hain. "
        "Uske neeche 'Browser Test' phone se wahi cheezein check karta hai."
        "</div>",
        unsafe_allow_html=True,
    )
    if st.button("▶ Binance Block Test Chalao", use_container_width=True, key="bn_block_diag_btn"):
        _slog("👉 'Binance Block Test' clicked")
        with st.spinner("Binance block test ho raha hai… (REST + WebSocket, ~20-50s)"):
            try:
                st.session_state["_bn_block_diag"] = _run_binance_block_diagnostic()
            except Exception as _e_bbd:
                _slog_exception("Binance block diagnostic", _e_bbd)
                st.session_state["_bn_block_diag"] = {"summary": {"likely_cause": "DIAG_CRASH",
                                                                    "verdict": [f"{type(_e_bbd).__name__}: {_e_bbd}"]}}
    _bbd_res = st.session_state.get("_bn_block_diag")
    if _bbd_res:
        _bbd_txt = json.dumps(_bbd_res, indent=2, ensure_ascii=False, default=str)
        st.markdown("**Summary (verdict)**")
        st.json(_bbd_res.get("summary", {}), expanded=True)
        st.download_button("⬇ Poora JSON download", data=_bbd_txt, file_name="binance_block_test.json",
                           mime="application/json", use_container_width=True, key="bn_block_diag_dl")
        with st.expander("📋 Poora JSON (copy ke liye)"):
            st.code(_bbd_txt, language="json")
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Browser-side Binance Test — PHONE/BROWSER se seedha (server test ka jodidaar) ──
    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">🌐 Binance Browser Test (phone se)</div>''', unsafe_allow_html=True)
    components.html(_BBD_BROWSER_HTML, height=620, scrolling=True)
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Options HISTORY test via RENDER relay — backtest ("kal kharidta to aaj kitna profit") ke liye ──
    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">📈 Options History Test (Render relay se)</div>''', unsafe_allow_html=True)
    st.markdown(
        "<div style='color:#848da0;font-size:0.82rem;margin-bottom:14px;'>"
        "HF → Render relay → Binance eapi. Spot, chalu option list, expire contracts + settlement, option candles (premium history), "
        "abhi ka mark + bid/ask — sab ek saath. Do demo hisaab bhi: (A) expire ho chuka ATM call, (B) chalu ATM call (24h pehle kharidta to aaj). "
        "Sirf public data, koi key/order nahi. Render so raha ho to pehli baar 30-60s lag sakte hain."
        "</div>",
        unsafe_allow_html=True,
    )
    if st.button("▶ Options History Test Chalao (Render)", use_container_width=True, key="oh_relay_btn"):
        with st.spinner("Render se options history test ho raha hai… (~20-90s)"):
            try:
                st.session_state["_oh_relay_res"] = _oh_relay_test()
            except Exception as _e_oh2:
                st.session_state["_oh_relay_res"] = {"verdict": [f"CRASH {type(_e_oh2).__name__}: {_e_oh2}"], "backtests": [], "steps": []}
    _oh_res = st.session_state.get("_oh_relay_res")
    if _oh_res:
        st.markdown("**Verdict**")
        st.markdown("\n".join(f"- {v}" for v in _oh_res.get("verdict", [])))
        for _bt in _oh_res.get("backtests", []):
            st.json(_bt, expanded=True)
        _oh_txt = json.dumps(_oh_res, indent=2, ensure_ascii=False, default=str)
        st.download_button("⬇ Poora JSON download", data=_oh_txt, file_name="options_history_relay_test.json",
                           mime="application/json", use_container_width=True, key="oh_relay_dl")
        with st.expander("📋 Poora JSON (copy ke liye)"):
            st.code(_oh_txt, language="json")
    st.markdown("**Phone se (browser → Render) CORS check**")
    components.html(_OH_BROWSER_HTML.replace("__RELAY__", BINANCE_EAPI_URL.rstrip("/")), height=330, scrolling=True)
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Direct HTTP Readiness — login page par AUTO check (koi button dabana nahi) ──
    # Maksad: "browser seedha Python se HTTP se data le sakta hai ya nahi" — pehle
    # hi yahan dikh jaye. Server-side checks (Streamlit/Starlette version, side-port,
    # 8501 par /api route) + browser-side checks (asli phone browser se same-origin
    # fetch: /api/ping, /api/music_list, Range 206, 5 parallel).
    def _dh_ver_tuple(_v):
        _out = []
        for _p in str(_v).split(".")[:3]:
            _d = "".join(ch for ch in _p if ch.isdigit())
            _out.append(int(_d) if _d else 0)
        while len(_out) < 3:
            _out.append(0)
        return tuple(_out)

    def _dh_server_checks():
        import importlib.metadata as _md
        rows = []   # (ok True/False/None(warn), label, detail)
        try:
            _stv = _md.version("streamlit")
        except Exception:
            _stv = getattr(st, "__version__", "?")
        _stt = _dh_ver_tuple(_stv)
        _has_app = hasattr(st, "App")
        if not _has_app:
            try:
                import streamlit.starlette as _stl   # 1.53-1.56 me App yahin tha
                _has_app = hasattr(_stl, "App")
            except Exception:
                pass
        rows.append((_has_app, "Streamlit + st.App",
                     f"v{_stv} — " + ("st.App available" if _has_app else "st.App NAHI mila (1.53+ chahiye)")))
        try:
            _slv = _md.version("starlette")
            _old = _dh_ver_tuple(_slv) < (0, 39, 0)
            rows.append((None if _old else True, "Starlette",
                         f"v{_slv}" + (" — requirements.txt ka `starlette<0.39.0` pin Streamlit 1.57+ se conflict kar sakta hai, pin hatana padega"
                                        if _old else " — theek")))
        except Exception:
            rows.append((None, "Starlette", "installed nahi mila"))
        rows.append((True if _stt >= (1, 57, 0) else None, "Web server",
                     "Starlette/Uvicorn (Streamlit 1.57+)" if _stt >= (1, 57, 0) else "Tornado (1.57 se pehle wala)"))
        # Side server login page par pehle start nahi hota tha (sirf replay/optionchain/depth mode me).
        # `_register_api_route()` idempotent hai — yahan bula ke start kar do aur port file ka
        # chhota intezaar (max ~1s), taaki neeche ke checks (aur server.py ka /api proxy) sahi dikhen.
        try:
            _register_api_route()
        except Exception as _e_reg:
            rows.append((False, "Side server start", f"{type(_e_reg).__name__}: {str(_e_reg)[:80]}"))
        _port = 0
        for _try in range(10):
            try:
                if os.path.exists(".api_port"):
                    with open(".api_port") as _pf:
                        _port = int(_pf.read().strip())
            except Exception:
                _port = 0
            if _port:
                break
            time.sleep(0.1)
        if _port:
            try:
                _t0 = time.time()
                _r = requests.get(f"http://127.0.0.1:{_port}/api/music_list", timeout=1.5)
                rows.append((_r.status_code == 200, f"Side-port :{_port}",
                             f"/api/music_list → {_r.status_code} ({int((time.time() - _t0) * 1000)} ms)"))
            except Exception as _e:
                rows.append((False, f"Side-port :{_port}", f"nahi pahuncha: {type(_e).__name__}"))
        else:
            rows.append((False, "Side-port", ".api_port file nahi mili (side server start nahi hua)"))
        try:
            _main_port = int(st.get_option("server.port") or 8501)
        except Exception:
            _main_port = 8501
        for _path in ("/api/ping", "/api/music_list"):
            try:
                _r = requests.get(f"http://127.0.0.1:{_main_port}{_path}", timeout=1.5)
                _isj = "json" in _r.headers.get("content-type", "").lower()
                if _r.status_code in (502, 503, 504) and _isj:
                    rows.append((None, f"{_path} on :{_main_port}",
                                 f"proxy live hai par side server jawab nahi de raha (status {_r.status_code})"))
                    continue
                rows.append((_r.status_code == 200 and _isj, f"{_path} on :{_main_port}",
                             ("JSON mila" if _isj else "JSON nahi — Streamlit ka page aaya, route abhi live nahi")
                             + f" (status {_r.status_code})"))
            except Exception as _e:
                rows.append((False, f"{_path} on :{_main_port}", f"nahi pahuncha: {type(_e).__name__}"))
        return rows

    _DH_BROWSER_HTML = """
<div id="o" style="font:13px/1.6 ui-monospace,Menlo,monospace;color:#c9d1d9;background:#0d1117;padding:8px 10px;border-radius:8px;white-space:pre-wrap;word-break:break-word">Browser check chal raha hai...</div>
<script>
(function () {
  var o = document.getElementById('o'), lines = [];
  function show() { o.textContent = lines.join('\\n'); }
  function add(ok, t) { lines.push((ok === true ? '\\u2705 ' : ok === false ? '\\u274C ' : '\\u26A0\\uFE0F ') + t); show(); }
  var origin = '';
  try { origin = window.parent.location.origin; } catch (e) { origin = location.origin; }
  if (!origin || origin === 'null') { add(false, 'Browser origin nahi mila'); return; }
  lines.push('Browser origin: ' + origin); show();
  (async function () {
    var ms = function (t0) { return Math.round(performance.now() - t0); };
    // 1. /api/ping
    try {
      var t0 = performance.now();
      var r = await fetch(origin + '/api/ping', { cache: 'no-store' });
      var ct = r.headers.get('content-type') || '';
      if (r.ok && ct.indexOf('json') >= 0) add(true, '/api/ping OK (' + ms(t0) + ' ms)');
      else add(false, '/api/ping status ' + r.status + (ct.indexOf('json') < 0 ? ' — JSON nahi, route live nahi' : ''));
    } catch (e) { add(false, '/api/ping fail: ' + e.message); }
    // 2. /api/music_list
    var first = null;
    try {
      var t1 = performance.now();
      var r2 = await fetch(origin + '/api/music_list', { cache: 'no-store' });
      var ct2 = r2.headers.get('content-type') || '';
      if (r2.ok && ct2.indexOf('json') >= 0) {
        var j = await r2.json();
        var tr = (j && j.tracks) || [];
        if (tr.length) { var a = tr[0]; first = (typeof a === 'string') ? a : (a.name || a.file || a.filename || null); }
        add(true, '/api/music_list OK — ' + tr.length + ' tracks (' + ms(t1) + ' ms)');
      } else if (ct2.indexOf('json') >= 0 && r2.status >= 502) {
        add(null, '/api/music_list: proxy live hai par side server jawab nahi de raha (status ' + r2.status + ')');
      } else add(false, '/api/music_list status ' + r2.status + (ct2.indexOf('json') < 0 ? ' — JSON nahi, route live nahi' : ''));
    } catch (e) { add(false, '/api/music_list fail: ' + e.message); }
    // 3. Range request (seek ke liye)
    if (first) {
      try {
        var t3 = performance.now();
        var r3 = await fetch(origin + '/api/music/' + encodeURIComponent(first), { headers: { Range: 'bytes=0-99' } });
        var b3 = await r3.arrayBuffer();
        add(r3.status === 206, 'Range request: status ' + r3.status + ', ' + b3.byteLength + ' bytes (' + ms(t3) + ' ms)' +
            (r3.status === 206 ? ' — seek chalega' : ' — Range pass nahi hua'));
      } catch (e) { add(false, 'Range fail: ' + e.message); }
    } else { add(null, 'Range test skip (koi track nahi mila)'); }
    // 4. 5 parallel
    try {
      var t4 = performance.now();
      var rs = await Promise.all([1, 2, 3, 4, 5].map(function () {
        return fetch(origin + '/api/ping', { cache: 'no-store' }).then(function (x) { return x.ok; });
      }));
      var okc = rs.filter(Boolean).length;
      add(okc === 5, 'Parallel 5 fetch: ' + okc + '/5 ok, total ' + ms(t4) + ' ms');
    } catch (e) { add(false, 'Parallel fail: ' + e.message); }
  })();
})();
</script>
"""

    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">🔌 Direct HTTP Readiness (auto)</div>''', unsafe_allow_html=True)
    st.markdown(
        "<div style='color:#848da0;font-size:0.82rem;margin-bottom:10px;'>"
        "Browser seedha Python se HTTP se data le sakta hai ya nahi — apne aap check hota hai. "
        "Neeche 'Server' (HF ke andar se) aur 'Browser' (tumhare phone se) dono ke result aate hain."
        "</div>",
        unsafe_allow_html=True,
    )
    if "_dh_server_rows" not in st.session_state:
        try:
            st.session_state["_dh_server_rows"] = _dh_server_checks()
        except Exception as _e_dh:
            st.session_state["_dh_server_rows"] = [(False, "Check crash", str(_e_dh)[:160])]
    _dh_lines = []
    for _ok_dh, _lab_dh, _det_dh in st.session_state["_dh_server_rows"]:
        _ic_dh = "✅" if _ok_dh is True else ("❌" if _ok_dh is False else "⚠️")
        _dh_lines.append(f"{_ic_dh} **{_lab_dh}** — {_det_dh}")
    st.markdown("**Server (HF ke andar se)**")
    st.markdown("\n\n".join(_dh_lines))
    st.markdown("**Browser (tumhare phone se)**")
    components.html(_DH_BROWSER_HTML, height=230, scrolling=True)
    if st.button("🔄 Dobara check", use_container_width=True, key="dh_recheck_btn"):
        st.session_state.pop("_dh_server_rows", None)
        st.rerun()
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── WebSocket Readiness — login page par AUTO check (HTTP Readiness jaisa) ─────
    # Server side: hub thread / :8503 health / nginx /ws route / asli WS handshake+echo.
    # Browser side: tumhare phone se wss connect, echo, 5 parallel, 200KB, api-bridge, tick push.
    _WS_BROWSER_HTML = r"""
<div id="o" style="font:13px/1.6 ui-monospace,Menlo,monospace;color:#c9d1d9;background:#0d1117;padding:8px 10px;border-radius:8px;white-space:pre-wrap;word-break:break-word">WebSocket check chal raha hai...</div>
<script>
(function () {
  var o = document.getElementById('o'), lines = [];
  function show() { o.textContent = lines.join('\n'); }
  function add(ok, t) { lines.push((ok === true ? '\u2705 ' : ok === false ? '\u274C ' : '\u26A0\uFE0F ') + t); show(); }
  function ms(t0) { return Math.round(performance.now() - t0); }
  function sleep(n) { return new Promise(function (r) { setTimeout(r, n); }); }
  var origin = '';
  try { origin = window.parent.location.origin; } catch (e) { origin = location.origin; }
  if (!origin || origin === 'null') { add(false, 'Browser origin nahi mila'); return; }
  var url = origin.replace(/^http/, 'ws') + '/ws';
  lines.push('WS URL: ' + url); show();
  var ws, seq = 0, pend = {}, helloAt = 0, opened = false, done = false, ticks = [], tickRes = null;
  function call(action, data, tmo) {
    return new Promise(function (res, rej) {
      if (!ws || ws.readyState !== 1) { rej(new Error('socket open nahi')); return; }
      var id = 't' + (++seq);
      var t = setTimeout(function () { delete pend[id]; rej(new Error('timeout ' + action)); }, tmo || 8000);
      pend[id] = { res: res, rej: rej, t: t };
      ws.send(JSON.stringify({ id: id, action: action, data: data === undefined ? null : data }));
    });
  }
  var t0 = performance.now();
  try { ws = new WebSocket(url); } catch (e) { add(false, 'WebSocket() fail: ' + e.message); return; }
  ws.onmessage = function (ev) {
    var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.id != null && pend[m.id]) {
      var p = pend[m.id]; delete pend[m.id]; clearTimeout(p.t);
      if (m.ok) p.res(m.data); else p.rej(new Error(m.error || 'fail'));
      return;
    }
    if (m.type === 'hello') { helloAt = performance.now(); return; }
    if (m.type === 'push' && m.topic === 'bn_tick') { ticks.push(m); if (tickRes) { tickRes(m); tickRes = null; } }
  };
  ws.onclose = function (ev) {
    if (!opened) add(false, 'Connect nahi hua (code ' + ev.code + ') \u2014 nginx /ws route / hub / origin check dekho');
    else if (!done) add(null, 'Socket beech me band ho gaya (code ' + ev.code + ')');
  };
  ws.onopen = function () { opened = true; add(true, 'Connected (' + ms(t0) + ' ms) \u2014 wss handshake OK'); run(); };
  async function run() {
    await sleep(150);
    add(helloAt ? true : null, helloAt ? 'Server hello frame mila' : 'hello frame nahi aaya');
    try { var a = performance.now(); var r = await call('echo', { n: 1 });
      add(!!(r && r.n === 1), 'Echo round-trip ' + ms(a) + ' ms (browser \u21C4 server dono taraf)'); }
    catch (e) { add(false, 'Echo fail: ' + e.message); }
    try { var b = performance.now();
      var rs = await Promise.all([1, 2, 3, 4, 5].map(function (i) { return call('echo', { i: i }).then(function (x) { return !!(x && x.i === i); }); }));
      var okc = rs.filter(Boolean).length;
      add(okc === 5, 'Parallel 5 echo: ' + okc + '/5 sahi jawab (id-matching), total ' + ms(b) + ' ms'); }
    catch (e) { add(false, 'Parallel fail: ' + e.message); }
    try { var s = new Array(200001).join('x'); var c = performance.now(); var big = await call('echo', { s: s }, 15000);
      add(!!(big && big.s && big.s.length === 200000), 'Bada payload 200 KB echo OK (' + ms(c) + ' ms)'); }
    catch (e) { add(false, 'Bada payload fail: ' + e.message); }
    try { var d = performance.now(); var ap = await call('api', { method: 'GET', path: '/api/ping' }, 10000);
      var j = JSON.parse(ap.body);
      add(ap.status === 200 && j.ok === true, '/api/ping via WS bridge \u2192 ' + ap.status + ' (' + ms(d) + ' ms)'); }
    catch (e) { add(false, 'api bridge fail: ' + e.message); }
    try {
      var waitTick = new Promise(function (r) { tickRes = r; });
      await call('subscribe', { topics: ['bn_tick'] });
      var got = ticks.length ? ticks[ticks.length - 1] : await Promise.race([waitTick, sleep(4000).then(function () { return null; })]);
      if (got) add(true, 'Live tick push mila (' + (got.snapshot ? 'last snapshot' : 'fresh tick') + ')');
      else add(null, 'Subscribe OK par abhi tick nahi aaya (login se pehle / market band ho to normal)');
    } catch (e) { add(false, 'Tick subscribe fail: ' + e.message); }
    done = true; try { ws.close(); } catch (e) {}
  }
})();
</script>
"""

    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('<div class="login-card" style="padding:20px 24px;">', unsafe_allow_html=True)
    st.markdown('<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">📡 WebSocket Readiness (auto)</div>', unsafe_allow_html=True)
    st.markdown(
        "<div style='color:#848da0;font-size:0.82rem;margin-bottom:10px;'>"
        "Bidirectional WebSocket (browser ⇄ server) chal raha hai ya nahi — apne aap check hota hai. "
        "'Server' (HF ke andar se, nginx ke through) aur 'Browser' (tumhare phone se) dono ke result neeche."
        "</div>",
        unsafe_allow_html=True,
    )
    if "_ws_server_rows" not in st.session_state:
        try:
            st.session_state["_ws_server_rows"] = _ws_server_checks(8501)   # 8501 = nginx (public) port
        except Exception as _e_wsr:
            st.session_state["_ws_server_rows"] = [(False, "Check crash", str(_e_wsr)[:160])]
    _ws_lines = []
    for _ok_ws, _lab_ws, _det_ws in st.session_state["_ws_server_rows"]:
        _ic_ws = "✅" if _ok_ws is True else ("❌" if _ok_ws is False else "⚠️")
        _ws_lines.append(f"{_ic_ws} **{_lab_ws}** — {_det_ws}")
    st.markdown("**Server (HF ke andar se)**")
    st.markdown("\n\n".join(_ws_lines))
    st.markdown("**Browser (tumhare phone se)**")
    components.html(_WS_BROWSER_HTML, height=290, scrolling=True)
    if st.button("🔄 WS dobara check", use_container_width=True, key="ws_recheck_btn"):
        st.session_state.pop("_ws_server_rows", None)
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    if _has_old:
        st.error("🔴 Fyers token expire ho gaya — dobara login karo")

    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">🔑 Fyers Login</div>''', unsafe_allow_html=True)
    st.markdown('''<div class="login-sub">Login karo — phir live BankNifty chart khulega</div>''', unsafe_allow_html=True)

    # ── METHOD B: Google URL ───────────────────────────────────────────────────
    st.markdown('''<div class="method-label">🔗 Google URL Login</div>''', unsafe_allow_html=True)

    st.markdown(
        f'''<p style="margin:6px 0 10px;">'''
        f'''<span class="step-badge">1</span>'''
        f'''<a href="{_auth_url_m}" target="_blank" style="color:#1a73e8;font-weight:600;">'''
        f'''👉 Yahan click karo — Fyers Fresh Login Link</a></p>''',
        unsafe_allow_html=True,
    )
    st.caption("⚠️ Link click karo → Google page khulega → us page ka poora URL copy karo")

    _url_inp_m = st.text_input(
        "Step 2 → Poora Google URL ya sirf auth_code paste karo",
        placeholder="https://www.google.com/?s=ok&auth_code=eyJ...",
        key="main_url_inp",
    )

    if st.button("⚡ Connect", use_container_width=True, type="primary", key="main_url_connect"):
        _raw_m = _url_inp_m.strip()
        if _raw_m:
            _code_m = _extract_auth_code(_raw_m)
            _ok_u, _tok_u, _resp_u = fyers_get_access_token(_app_id_m, _secret_m, _code_m)
            if _ok_u:
                save_creds({
                    **_creds_main,
                    "app_id":       _app_id_m,
                    "secret_key":   _secret_m,
                    "client_id":    DEFAULT_CLIENT_ID,
                    "password":     DEFAULT_PASSWORD,
                    "access_token": _tok_u,
                })
                st.session_state["_force_active"] = False
                _sess_cache.update({"active": False, "ts": time.time()})
                st.success("🎉 Connected!")
            else:
                st.error(f"❌ Login Failed: {_tok_u}")
                with st.expander("Full Fyers Response"):
                    st.code(json.dumps(_resp_u, indent=2), language="json")
        else:
            st.warning("URL ya auth_code paste karo pehle")

    if sess_active:
        st.markdown("<div style='margin-top:14px;'></div>", unsafe_allow_html=True)
        if st.button("📈 Fyers Entry — Chart Kholo", use_container_width=True, key="fyers_entry_btn"):
            # Sirf Fyers-related threads chalenge (REST poller, token monitor,
            # Fyers option-chain, Fyers WS) — Binance ka koi thread nahi.
            _slog("👉 'Fyers Entry' clicked → _fyers_entry_mode=True set kiya (Binance/Replay off), st.rerun().")
            st.session_state["_fyers_entry_mode"]   = True
            st.session_state["_binance_entry_mode"] = False
            st.session_state["_replay_mode"]        = False
            st.session_state["_lt_replay_mode"]     = False
            st.rerun()
    else:
        st.markdown(
            "<div style='color:#555;font-size:0.8rem;margin-top:10px;'>"
            "Chart kholne ke liye pehle login karo.</div>",
            unsafe_allow_html=True,
        )

    st.markdown('''</div>''', unsafe_allow_html=True)

    # Binance Entry: trading browser se seedha Render ko jaati hai (HF pe koi Binance key/secret nahi).
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">Binance</div>''', unsafe_allow_html=True)
    st.markdown("<div style='margin-top:14px;'></div>", unsafe_allow_html=True)
    if st.button("📈 Binance Entry — Chart Kholo", use_container_width=True, key="binance_entry_btn"):
        # Sirf Binance mode chalega - Fyers ka koi thread nahi.
        _slog("👉 'Binance Entry' clicked → _binance_entry_mode=True set kiya (Fyers/Replay off), st.rerun().")
        st.session_state["_binance_entry_mode"] = True
        st.session_state["_fyers_entry_mode"]   = False
        st.session_state["_replay_mode"]        = False
        st.session_state["_lt_replay_mode"]     = False
        st.rerun()

    st.markdown('''</div>''', unsafe_allow_html=True)


    # ── Storage Diagnostic Card (100% server-side, no client/JS involved) ─────
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">🗄️ Storage Diagnostic <span style="font-size:0.75rem;color:#555;font-weight:400;">(sirf app.py se check hota hai — chart.html/browser ka koi role nahi)</span></div>''', unsafe_allow_html=True)

    if st.button("🔎 Check Storage Now", use_container_width=True, key="storage_diag_btn"):
        _slog("👉 'Check Storage Now' button clicked")
        try:
            _diag = _storage_diagnostic()
        except Exception as _e_diag:
            _slog_exception("[StorageDiag] manual check", _e_diag)
            _diag = None
            st.error(f"❌ Diagnostic khud crash ho gaya: {_e_diag}")

        if _diag is not None:
            _mount_ok = _diag["data_mount_exists"] and _diag["data_mount_writable"]
            _persist_ok = _diag["is_persistent_path"]
            _io_ok = _diag["write_read_delete_ok"]
            _all_ok = _mount_ok and _persist_ok and _io_ok

            if _all_ok:
                st.markdown(
                    "<div style='padding:8px 0;color:#26a69a;font-size:0.85rem;font-weight:600;'>"
                    "🟢 Storage sahi hai — HF ka /data volume mounted, writable, aur "
                    "app usi persistent path ko use kar raha hai. Agar phir bhi data "
                    "purana/missing lage, to bug client-side (chart.html) mein hai.</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    "<div style='padding:8px 0;color:#ef5350;font-size:0.85rem;font-weight:600;'>"
                    "🔴 Storage mein problem hai — neeche details dekho.</div>",
                    unsafe_allow_html=True,
                )

            _rows = [
                ("/data mount maujood hai?",      "✅" if _diag["data_mount_exists"]   else "❌"),
                ("/data likhne layak hai?",        "✅" if _diag["data_mount_writable"] else "❌"),
                ("App persistent path use kar raha hai?", "✅" if _persist_ok else "❌ (ephemeral fallback pe chal raha hai)"),
                ("Actual write+read+delete test",  "✅ pass" if _io_ok else f"❌ fail — {_diag['write_read_delete_error']}"),
            ]
            st.markdown(
                "<div style='font-family:monospace;font-size:0.8rem;color:#c9d1e0;line-height:1.9;margin-top:8px;'>"
                + "<br>".join(f"{k}: {v}" for k, v in _rows)
                + f"<br>LOCAL_STATE_DIR: {_diag['local_state_dir']}"
                + "</div>",
                unsafe_allow_html=True,
            )

            st.markdown("<div style='margin-top:10px;font-size:0.78rem;color:#848da0;'>Saved files:</div>", unsafe_allow_html=True)
            _file_rows = []
            for _kind, _info in _diag["files"].items():
                if _info.get("exists"):
                    _file_rows.append(f"• {_kind}: {_info.get('size_bytes','?')} bytes, last saved {_info.get('modified','?')}")
                else:
                    _file_rows.append(f"• {_kind}: koi file nahi mili (kabhi save hi nahi hua)")
            st.markdown(
                "<div style='font-family:monospace;font-size:0.8rem;color:#c9d1e0;line-height:1.7;'>"
                + "<br>".join(_file_rows) + "</div>",
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            "<div style='color:#848da0;font-size:0.82rem;'>Button dabao — /data mount, write-permission, "
            "actual path, aur saved files ka live status yahin dikhega.</div>",
            unsafe_allow_html=True,
        )

    st.markdown('''</div>''', unsafe_allow_html=True)

    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

    if st.button("📝 Test File Likho (Save karke check karo)", use_container_width=True, key="storage_write_test_btn"):
        _slog("👉 'Test File Likho' button clicked")
        _test_path = "/data/CLAUDE_TEST_FILE.txt"
        try:
            _now_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
            with open(_test_path, "w", encoding="utf-8") as f:
                f.write(f"Ye test file app.py (server-side) ne likhi hai.\nTimestamp: {_now_ist}\n")
            _slog(f"[StorageDiag] Test file likh di: {_test_path}", level="ok")
            st.success(f"✅ File likh di gayi: `{_test_path}` (timestamp: {_now_ist})")
            st.markdown(
                "<div style='color:#848da0;font-size:0.82rem;margin-top:6px;'>"
                "HF Space kholo → ⋮ menu → <b>Files</b> → <b>Krishan162627/Trade-storage</b> volume ke andar "
                "root mein <code>CLAUDE_TEST_FILE.txt</code> dhundo. Agar wahan dikh jaaye to storage 100% "
                "connected aur permanent hai.</div>",
                unsafe_allow_html=True,
            )
        except Exception as _e_write:
            _slog_exception("[StorageDiag] test file write", _e_write)
            st.error(f"❌ File likhne mein fail: {_e_write}")

    # ── Storage Manager Card — poori file list aur multi-select delete — sab 100% server-side,
    # /data mount par seedha operate karta hai (_storage_list_all_files / _storage_delete_files
    # functions upar defined hain). (2026-10-01) "Poora Storage Clean Karo" (Wipe All) permanently hata diya.
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">📂 Storage Manager <span style="font-size:0.75rem;color:#555;font-weight:400;">(saari files list karo, ya chuni hui delete karo)</span></div>''', unsafe_allow_html=True)

    if st.button("📋 Saari Files List Karo", use_container_width=True, key="storage_list_all_btn"):
        _slog("👉 'Saari Files List Karo' button clicked")
        try:
            st.session_state["_storage_all_files"] = _storage_list_all_files()
            _slog(f"[StorageManager] {len(st.session_state['_storage_all_files'])} file(s) mili", level="ok")
        except Exception as _e_list:
            _slog_exception("[StorageManager] list all files", _e_list)
            st.error(f"❌ List karne mein fail: {_e_list}")

    _all_files = st.session_state.get("_storage_all_files")

    if _all_files is not None:
        _total_bytes = sum(_f["size_bytes"] for _f in _all_files)
        st.markdown(
            f"<div style='padding:6px 0;color:#c9d1e0;font-size:0.85rem;'>"
            f"Total: <b>{len(_all_files)}</b> file(s), <b>{_total_bytes:,}</b> bytes</div>",
            unsafe_allow_html=True,
        )

        if _all_files:
            _labels = {
                f"{_f['rel_path']}  —  {_f['size_bytes']:,} bytes  —  {_f['modified']}": _f["rel_path"]
                for _f in _all_files
            }
            _selected_labels = st.multiselect(
                "Delete karne ke liye files chuno",
                options=list(_labels.keys()),
                key="storage_multiselect_del",
            )
            _selected_rels = [_labels[_l] for _l in _selected_labels]

            if st.button(
                f"🗑️ Selected Delete Karo ({len(_selected_rels)})",
                use_container_width=True,
                key="storage_delete_selected_btn",
                disabled=len(_selected_rels) == 0,
            ):
                _slog(f"👉 'Selected Delete Karo' clicked — {_selected_rels}")
                _del_ok, _del_msg = _storage_delete_files(_selected_rels)
                if _del_ok:
                    st.success(f"✅ {_del_msg}")
                    _slog(f"[StorageManager] delete selected: {_del_msg}", level="ok")
                else:
                    st.error(f"❌ {_del_msg}")
                    _slog(f"[StorageManager] delete selected FAILED: {_del_msg}", level="err")
                # List turant refresh karo taaki updated status dikhe
                st.session_state["_storage_all_files"] = _storage_list_all_files()
                st.rerun()
        else:
            st.markdown(
                "<div style='color:#848da0;font-size:0.82rem;'>Storage khaali hai — koi file nahi mili.</div>",
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            "<div style='color:#848da0;font-size:0.82rem;'>Button dabao — /data ke andar saari files "
            "(state/settings/layout + koi bhi stray/test file) list ho jaayengi.</div>",
            unsafe_allow_html=True,
        )

    # (2026-10-01) Danger Zone + confirm-checkbox + "🧹 Poora Storage Clean Karo" (Wipe All) button yahan se
    # PERMANENTLY hata diya — ye rules/journal/render_log samet poora /data uda deta tha. Wapas mat lagana.

    st.markdown('''</div>''', unsafe_allow_html=True)

    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

    # ── SV2/SV3 Migration Card — "Upload by URL" (URL → HF /data disk par
    # ek-baari data migrate kiya jaa sake). Har view ka apna alag folder hai
    # (_SV2_CACHE_DIR / _SV3_SYMBOLS_DIR) — kabhi mix nahi hote.
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown(
        '''<div class="login-title">📥 SV2 / SV3 — Migration (URL → HF Disk)
        <span style="font-size:0.75rem;color:#555;font-weight:400;">(ek-baari data yahan se HF persistent /data par daalo)</span></div>''',
        unsafe_allow_html=True,
    )

    st.markdown(
        "<div style='color:#848da0;font-size:0.8rem;margin-bottom:8px;'>"
        f"SV2 folder: <code>{_SV2_CACHE_DIR}</code> &nbsp;|&nbsp; "
        f"SV3 folder: <code>{_SV3_SYMBOLS_DIR}</code></div>",
        unsafe_allow_html=True,
    )

    # ── Generic single-URL uploader (SV1 ke purane "upload by URL" jaisa —
    # koi bhi ek-off/custom URL manually paste karke, sahi folder chun ke
    # save karne ke liye) ───────────────────────────────────────────────────
    st.markdown("<div style='font-size:0.85rem;color:#c9d1e0;margin:4px 0;'>Generic — Koi bhi ek URL manually daalo</div>", unsafe_allow_html=True)
    _mig_target = st.selectbox(
        "Target folder",
        options=["SV2 cache", "SV3 — _index.json", "SV3 — ek symbol file", "Twelve Data cache"],
        key="mig_generic_target",
    )
    _mig_url = st.text_input("URL", key="mig_generic_url")
    _mig_fname = st.text_input(
        "Filename (jis naam se save hogi, extension sahit)",
        key="mig_generic_fname",
        placeholder="e.g. NSE_ABDL-EQ.json" if _mig_target == "SV3 — ek symbol file" else ("e.g. td_gold_5m.gz" if _mig_target == "Twelve Data cache" else "e.g. banknifty_5m_csv_json.gz"),
    )
    if st.button("⬇️ Generic — Fetch & Save Karo", use_container_width=True, key="mig_generic_btn"):
        if not _mig_url or not _mig_fname:
            st.error("❌ URL aur Filename dono chahiye.")
        else:
            if _mig_target == "SV2 cache":
                _dest = os.path.join(_SV2_CACHE_DIR, _mig_fname)
            elif _mig_target == "SV3 — _index.json":
                _dest = os.path.join(_SV3_CACHE_DIR, _mig_fname)
            elif _mig_target == "Twelve Data cache":
                _dest = os.path.join(_td._TD_DIR, _mig_fname)
            else:
                _dest = os.path.join(_SV3_SYMBOLS_DIR, _mig_fname)
            _slog(f"👉 'Generic Migration' clicked — target={_mig_target} url={_mig_url}")
            _gen_ok, _gen_msg = _url_fetch_save_to_disk(_mig_url, _dest)
            _slog(f"[Migration][Generic][{_mig_target}] {_gen_msg}", level="ok" if _gen_ok else "err")
            if _gen_ok:
                st.success(f"✅ {_gen_msg}")
            else:
                st.error(f"❌ {_gen_msg}")

    st.markdown('''</div>''', unsafe_allow_html=True)

    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

    # ── 🧱 .bin Converter Card (NO_PRELOAD_PLAN Step 1) — purani .gz/.json se
    # /data/bin/ me ek-baari .bin copies banata hai (background thread).
    # Purani files ko sirf PADHTA hai, kabhi badalta/delete nahi karta.
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown(
        '''<div class="login-title">🧱 .bin Converter
        <span style="font-size:0.75rem;color:#555;font-weight:400;">(candle data .gz/.json → fast .bin, ek-baari; purani files backup rehti hain)</span></div>''',
        unsafe_allow_html=True,
    )
    _bn_st = _get_bin_state()
    st.markdown(
        f"<div style='color:#848da0;font-size:0.8rem;margin-bottom:8px;'>"
        f"Output folder: <code>{_bin_root()}</code></div>",
        unsafe_allow_html=True,
    )
    _bn_force = st.checkbox(
        "Force — sab dobara convert karo (unchanged files bhi)",
        key="bin_conv_force", disabled=_bn_st["running"],
    )
    if st.button("🧱 Convert to .bin", use_container_width=True, type="primary",
                 key="bin_conv_btn", disabled=_bn_st["running"]):
        _slog(f"👉 '.bin Converter' clicked — force={_bn_force}")
        _bn_ok, _bn_msg = _bin_convert_start(bool(_bn_force))
        if _bn_ok:
            st.success(f"✅ {_bn_msg} — status ke liye neeche 🔄 Refresh dabao.")
        else:
            st.warning(f"⚠️ {_bn_msg}")
    if st.button("🔄 Status Refresh", use_container_width=True, key="bin_conv_refresh"):
        st.rerun()
    if _bn_st["running"]:
        _bn_tot = max(1, int(_bn_st["total"]))
        st.progress(min(1.0, _bn_st["done"] / _bn_tot),
                    text=f"Chal raha hai… {_bn_st['done']}/{_bn_st['total']} — {_bn_st['current']}")
    elif _bn_st["summary"]:
        if _bn_st["ok"]:
            st.success("✅ Conversion khatam — neeche wali EK line copy karke bhej do:")
        else:
            st.error("❌ Conversion me problem aayi (failed>0 ya sources_untouched=False ya crash) — neeche line + log bhej do:")
        st.code(_bn_st["summary"], language=None)
    else:
        st.markdown(
            "<div style='color:#848da0;font-size:0.82rem;'>Abhi tak chalaya nahi gaya.</div>",
            unsafe_allow_html=True,
        )
    with st.expander("📜 Converter log", expanded=False):
        _bn_lines = _bn_st["log"][-400:]
        st.code("\n".join(_bn_lines) if _bn_lines else "(khaali)", language=None)
    st.markdown('''</div>''', unsafe_allow_html=True)

    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

    # ── Personal Backup Card — Google Drive → HF /data (alag folder,
    # app-data se poori tarah separate, sirf save/backup ke liye) ──────────
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown(
        '''<div class="login-title">📁 Personal Backup — Google Drive → HF Storage
        <span style="font-size:0.75rem;color:#555;font-weight:400;">(public Drive link daalo, personal_backup folder me persistent storage par save ho jayega)</span></div>''',
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='color:#848da0;font-size:0.8rem;margin-bottom:8px;'>"
        f"Base folder: <code>{_PERSONAL_BACKUP_DIR}</code> — files isi folder me aati hain (chaho to neeche optional subfolder naam do), app-data (SV1/SV2/SV3) se kabhi mix nahi hota.</div>",
        unsafe_allow_html=True,
    )

    _gd_url = st.text_input(
        "Google Drive link (file ya poora folder — dono chalega)",
        key="gdrive_import_url",
        placeholder="https://drive.google.com/drive/folders/....  ya  https://drive.google.com/file/d/..../view",
    )

    # ── Folder picker: user khud chunta hai kahan save ho (main folder ya koi bhi sub-folder) ──
    _gd_folders = _gd_list_dest_folders()
    _gd_default = "video/punjabi_songs" if "video/punjabi_songs" in _gd_folders else (_gd_folders[0] if _gd_folders else "personal_backup")
    _gd_dest_rel = st.selectbox(
        "📂 Kahan save karu? (folder chuno — main folder ya koi sub-folder)",
        options=_gd_folders or ["personal_backup"],
        index=(_gd_folders.index(_gd_default) if _gd_default in _gd_folders else 0),
        key="gdrive_import_dest",
    )
    _gd_sub = st.text_input(
        "➕ Naya sub-folder (optional — chuni hui jagah ke andar banega; a/b likho to andar-andar banega)",
        key="gdrive_import_subfolder",
        placeholder="e.g. marriage_video   (khaali = seedha upar chuna hua folder)",
    )
    _gd_flatten = st.checkbox(
        "Drive ke andar ke folders hata do — saari files seedha is folder me (video player ke liye best)",
        value=True, key="gdrive_import_flatten",
    )
    try:
        _gd_final = _gd_resolve_dest(_gd_dest_rel, _gd_sub)
        _gd_cur_b, _gd_cur_n = _dir_total_size(_gd_final) if _gd_final and os.path.isdir(_gd_final) else (0, 0)
        st.caption(f"Files yahan jayengi: `{_gd_final}`  |  abhi isme: {_gd_cur_n} file(s), {_fmt_bytes(_gd_cur_b)}")
    except Exception:
        pass

    _gd_state = _GDRIVE_IMPORT_STATE
    _gd_running = _gd_state["running"]

    if st.button(
        "⬇️ Import Start Karo (background me chalega)",
        use_container_width=True, type="primary",
        key="gdrive_import_btn", disabled=_gd_running,
    ):
        if not _gd_url.strip():
            st.error("❌ Pehle Google Drive link daalo.")
        else:
            _slog(f"👉 'Drive Import' clicked — dest={_gd_dest_rel!r} sub={_gd_sub!r} flatten={_gd_flatten} url={_gd_url}")
            _launch_ok, _launch_msg = _gdrive_start_background_import(_gd_url.strip(), _gd_dest_rel, _gd_sub.strip(), _gd_flatten)
            if _launch_ok:
                st.success(f"✅ {_launch_msg} — status neeche update hoti rahegi, page refresh/interact karke dekho.")
            else:
                st.warning(f"⚠️ {_launch_msg}")

    # ── Status display — jo bhi last/current import ka pata hai ────────────
    if _gd_running:
        _elapsed = int(time.time() - _gd_state["started_at"])
        st.info(f"⏳ Import chal raha hai… ({_elapsed}s se) → `{_gd_state['dest_dir']}`. Bade folders (10-20GB+) me kaafi der lag sakti hai — page band mat karo, background thread chalti rahegi, bas refresh karke status check karte raho.")
    elif _gd_state["ok"] is True:
        st.success(f"✅ Last import safal: {_gd_state['msg']}")
    elif _gd_state["ok"] is False:
        st.error(f"❌ Last import fail: {_gd_state['msg']}")
        if _gd_state.get("detail"):
            with st.expander("🔍 Exact error (full traceback)", expanded=True):
                st.code(_gd_state["detail"], language="text")
        st.caption(f"URL: {_gd_state.get('url','')}  |  Dest: {_gd_state.get('dest_dir','')}")

    # ── Disk-space snapshot — persistent storage kitni bhari hai ────────────
    try:
        _du = shutil.disk_usage(_storage_root())
        _pb_bytes, _pb_files = _dir_total_size(_PERSONAL_BACKUP_DIR)
        st.markdown(
            f"<div style='color:#848da0;font-size:0.78rem;margin-top:6px;'>"
            f"Personal Backup abhi tak: <b>{_pb_files} file(s), {_fmt_bytes(_pb_bytes)}</b> &nbsp;|&nbsp; "
            f"Disk free: <b>{_fmt_bytes(_du.free)}</b> / total {_fmt_bytes(_du.total)}</div>",
            unsafe_allow_html=True,
        )
    except Exception:
        pass

    st.markdown('''</div>''', unsafe_allow_html=True)

    st.markdown('''</div>''', unsafe_allow_html=True)

