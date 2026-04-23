
from flask import Flask, request, jsonify
import requests
import os
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================================================
# CONFIG
# =========================================================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
ACCOUNT_ID = (os.getenv("ACCOUNT_ID") or os.getenv("OANDA_ACCOUNT_ID") or "").strip()
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()

BASE_URL = (
    "https://api-fxtrade.oanda.com/v3"
    if OANDA_ENV == "live"
    else "https://api-fxpractice.oanda.com/v3"
)

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234").strip()

PAIRS = [
    p.strip().upper()
    for p in os.getenv("PAIRS", "EUR_USD,GBP_USD,USD_JPY").split(",")
    if p.strip()
]

# Sizing
FIXED_UNITS = int(os.getenv("FIXED_UNITS", "5000"))
FALLBACK_UNITS = int(os.getenv("FALLBACK_UNITS", "5000"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0"))

# Trade controls
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "1"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_TRADES_PER_PAIR", "1"))
ONE_TRADE_PER_PAIR = os.getenv("ONE_TRADE_PER_PAIR", "true").lower() == "true"
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "900"))

# Risk / exits
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "10"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "12"))

USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true").lower() == "true"
BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "3"))
BREAK_EVEN_PLUS_PIPS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", "1"))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true").lower() == "true"
TRAILING_TRIGGER_PIPS = float(os.getenv("TRAILING_TRIGGER_PIPS", "6"))
TRAILING_DISTANCE_PIPS = float(os.getenv("TRAILING_DISTANCE_PIPS", "3"))

MANAGE_INTERVAL_SECONDS = int(os.getenv("MANAGE_INTERVAL_SECONDS", "10"))

# Spread filter
SPREAD_LIMIT_PIPS = float(os.getenv("SPREAD_LIMIT_PIPS", "2.5"))

# Session filter
ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").lower() == "true"
TIMEZONE_NAME = os.getenv("TIMEZONE_NAME", "America/New_York")
LONDON_START = int(os.getenv("LONDON_START", "3"))
LONDON_END = int(os.getenv("LONDON_END", "6"))
NY_START = int(os.getenv("NY_START", "8"))
NY_END = int(os.getenv("NY_END", "11"))

# Daily loss
ENABLE_DAILY_LOSS_LIMIT = os.getenv("ENABLE_DAILY_LOSS_LIMIT", "true").lower() == "true"
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))

# Optional scaling toggle for future use
ALLOW_SCALING_IN = os.getenv("ALLOW_SCALING_IN", "false").lower() == "true"

# =========================================================
# STATE
# =========================================================
last_trade_time = {}
day_start_balance = None
day_start_date = None
state_lock = threading.Lock()


# =========================================================
# HELPERS
# =========================================================
def log(msg: str) -> None:
    print(msg, flush=True)


def is_config_ok() -> bool:
    return bool(OANDA_API_KEY and ACCOUNT_ID)


def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE_NAME))


def in_session() -> bool:
    if not ENABLE_SESSION_FILTER:
        return True
    hr = now_local().hour
    in_london = LONDON_START <= hr < LONDON_END
    in_ny = NY_START <= hr < NY_END
    return in_london or in_ny


def oanda_get(path: str, params=None):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    return r.json()


def oanda_put(path: str, payload: dict):
    url = f"{BASE_URL}{path}"
    r = requests.put(url, headers=HEADERS, json=payload, timeout=20)
    return r.json()


def oanda_post(path: str, payload: dict):
    url = f"{BASE_URL}{path}"
    r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    return r.json()


def get_account_summary():
    return oanda_get(f"/accounts/{ACCOUNT_ID}/summary")


def get_balance() -> float:
    data = get_account_summary()
    return float(data["account"]["balance"])


def refresh_day_start_balance():
    global day_start_balance, day_start_date
    today = now_local().date()
    if day_start_date != today or day_start_balance is None:
        day_start_date = today
        day_start_balance = get_balance()
        log(f"DAY RESET | start_balance={day_start_balance:.2f}")


def daily_loss_limit_hit() -> bool:
    if not ENABLE_DAILY_LOSS_LIMIT:
        return False
