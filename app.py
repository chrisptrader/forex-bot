import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def env_str(name, default=""):
    return os.getenv(name, default).strip()


def env_int(name, default=0):
    try:
        return int(float(os.getenv(name, str(default))))
    except:
        return default


def env_float(name, default=0.0):
    try:
        return float(os.getenv(name, str(default)))
    except:
        return default


def env_bool(name, default=False):
    v = os.getenv(name, str(default)).strip().lower()
    return v in ["1", "true", "yes", "on"]


OANDA_API_KEY = env_str("OANDA_API_KEY")
OANDA_ACCOUNT_ID = env_str("OANDA_ACCOUNT_ID")
OANDA_ENV = env_str("OANDA_ENV", "practice").lower()
WEBHOOK_PASSPHRASE = env_str("WEBHOOK_PASSPHRASE", "1234")

PAIR_LIST = [p.strip().upper() for p in env_str("PAIRS", "EUR_USD,GBP_USD,USD_JPY").split(",") if p.strip()]

RISK_PERCENT = env_float("RISK_PERCENT", 0.02)
FIXED_UNITS = env_int("FIXED_UNITS", 5000)
FALLBACK_UNITS = env_int("FALLBACK_UNITS", 100)

STOP_LOSS_PIPS = env_float("STOP_LOSS_PIPS", 20)
TAKE_PROFIT_PIPS = env_float("TAKE_PROFIT_PIPS", 80)
MAX_SPREAD_PIPS = env_float("MAX_SPREAD_PIPS", 12)

ENABLE_SPREAD_FILTER = env_bool("ENABLE_SPREAD_FILTER", True)
ENABLE_TREND_FILTER = env_bool("ENABLE_TREND_FILTER", False)
ENABLE_MOMENTUM_FILTER = env_bool("ENABLE_MOMENTUM_FILTER", False)
ENABLE_VOLATILITY_FILTER = env_bool("ENABLE_VOLATILITY_FILTER", False)
ENABLE_SESSION_FILTER = env_bool("ENABLE_SESSION_FILTER", False)
ENABLE_TRAILING = env_bool("ENABLE_TRAILING", True)
ENABLE_V21_MANAGER = env_bool("ENABLE_V21_MANAGER", True)
ENABLE_DAILY_LOSS_LIMIT = env_bool("ENABLE_DAILY_LOSS_LIMIT", True)
ALLOW_MULTIPAIR = env_bool("ALLOW_MULTIPAIR", True)

FAST_EMA_PERIOD = env_int("FAST_EMA_PERIOD", 20)
SLOW_EMA_PERIOD = env_int("SLOW_EMA_PERIOD", 20)
FAST_MA_PERIOD = env_int("FAST_MA_PERIOD", 20)
SLOW_MA_PERIOD = env_int("SLOW_MA_PERIOD", 50)
EMA_PERIOD = env_int("EMA_PERIOD", 20)

BREAK_EVEN_TRIGGER_PIPS = env_float("BREAK_EVEN_TRIGGER_PIPS", 15)
BREAK_EVEN_PLUS_PIPS = env_float("BREAK_EVEN_PLUS_PIPS", 2)

LOCK_1_TRIGGER_PIPS = env_float("LOCK_1_TRIGGER_PIPS", 18)
LOCK_2_TRIGGER_PIPS = env_float("LOCK_2_TRIGGER_PIPS", 30)
LOCK_3_TRIGGER_PIPS = env_float("LOCK_3_TRIGGER_PIPS", 45)

TRAILING_TRIGGER_PIPS = env_float("TRAILING_TRIGGER_PIPS", 22)
TRAILING_DISTANCE_PIPS = env_float("TRAILING_DISTANCE_PIPS", 12)

BUY_PULLBACK_PIPS = env_float("BUY_PULLBACK_PIPS", 1.0)
SELL_BOUNCE_PIPS = env_float("SELL_BOUNCE_PIPS", 1.0)
PULLBACK_PIPS = env_float("PULLBACK_PIPS", 0.5)
BOUNCE_PIPS = env_float("BOUNCE_PIPS", 0.8)
PULLBACK_DEPTH_PIPS = env_float("PULLBACK_DEPTH_PIPS", 5)

BREAKOUT_LOOKBACK = env_int("BREAKOUT_LOOKBACK", 3)
MOMENTUM_LOOKBACK = env_int("MOMENTUM_LOOKBACK", 3)
MOMENTUM_CANDLES = env_int("MOMENTUM_CANDLES", 2)
MOMENTUM_MIN_BODY_PIPS = env_float("MOMENTUM_MIN_BODY_PIPS", 1.2)
CONFIRMATION_CANDLES = env_int("CONFIRMATION_CANDLES", 2)

MIN_CANDLE_RANGE_PIPS = env_float("MIN_CANDLE_RANGE_PIPS", 1)
MAX_CANDLE_RANGE_PIPS = env_float("MAX_CANDLE_RANGE_PIPS", 35)
MIN_VOLATILITY_PIPS = env_float("MIN_VOLATILITY_PIPS", 1.5)

MIN_TREND_GAP_PIPS = env_float("MIN_TREND_GAP_PIPS", 1)
STRONG_TREND_GAP_PIPS = env_float("STRONG_TREND_GAP_PIPS", 10)
TREND_STRENGTH_MIN = env_float("TREND_STRENGTH_MIN", 15)

MAX_DAILY_LOSS_PERCENT = env_float("MAX_DAILY_LOSS_PERCENT", 3)
MAX_OPEN_TRADES = env_int("MAX_OPEN_TRADES", 6)
MAX_TOTAL_OPEN_TRADES = env_int("MAX_TOTAL_OPEN_TRADES", 6)
MIN_SECONDS_BETWEEN_TRADES = env_int("MIN_SECONDS_BETWEEN_TRADES", 20)
TRADE_COOLDOWN = env_int("TRADE_COOLDOWN", 60)

POLL_SECONDS = env_int("POLL_SECONDS", 5)
AUTO_CHECK_SECONDS = env_int("AUTO_CHECK_SECONDS", 10)
MONITOR_INTERVAL = env_int("MONITOR_INTERVAL", 10)

TIMEZONE_NAME = env_str("TIMEZONE_NAME", "America/New_York")
LONDON_START = env_int("LONDON_START", 3)
LONDON_END = env_int("LONDON_END", 11)
NY_START = env_int("NY_START", 8)
NY_END = env_int("NY_END", 11)

DEFAULT_SL_PIPS = env_float("DEFAULT_SL_PIPS", STOP_LOSS_PIPS)
DEFAULT_TP_PIPS = env_float("DEFAULT_TP_PIPS", TAKE_PROFIT_PIPS)

if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com"
else:
    BASE_URL = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

last_trade_time = {}
daily_start_balance = None
daily_balance_date = None


def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def now_est():
    return datetime.now(ZoneInfo(TIMEZONE_NAME))


def in_session():
    if not ENABLE_SESSION_FILTER:
        return True
    h = now_est().hour
    london = LONDON_START <= h <= LONDON_END
    ny = NY_START <= h <= NY_END
    return london or ny


def get_account_summary():
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_balance():
    data = get_account_summary()
    return float(data["account"]["balance"])


def reset_daily_balance_if_needed():
    global daily_start_balance, daily_balance_date
    today = now_est().date()
    if daily_balance_date != today:
        daily_balance_date = today
        daily_start_balance = get_balance()


def daily_loss_limit_hit():
    if not ENABLE_DAILY_LOSS_LIMIT:
        return False
    reset_daily_balance_if_needed()
    bal = get_balance()
    dd = ((daily_start_balance - bal) / daily_start_balance) * 100 if daily_start_balance else 0
    return dd >= MAX_DAILY_LOSS_PERCENT


def get_open_trades():
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json().get("trades", [])


def count_open_trades(pair=None):
    trades = get_open_trades()
    if pair:
        return sum(1 for t in trades if t["instrument"] == pair)
    return len(trades)


def get_prices(pair):
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=HEADERS, params={"instruments": pair}, timeout=15)
    r.raise_for_status()
    prices = r.json()
