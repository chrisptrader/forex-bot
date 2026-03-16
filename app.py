from flask import Flask, request, jsonify
import os
import requests
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================
# ENV SETTINGS
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))

COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))
DUPLICATE_SIGNAL_SECONDS = int(os.getenv("DUPLICATE_SIGNAL_SECONDS", "90"))

ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").lower() == "true"
SESSION_TIMEZONE = os.getenv("SESSION_TIMEZONE", "America/New_York")
SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "4"))
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "11"))

ENABLE_SPREAD_FILTER = os.getenv("ENABLE_SPREAD_FILTER", "true").lower() == "true"
ENABLE_VOLATILITY_FILTER = os.getenv("ENABLE_VOLATILITY_FILTER", "true").lower() == "true"
ENABLE_TREND_FILTER = os.getenv("ENABLE_TREND_FILTER", "true").lower() == "true"

ALLOW_REVERSE_SIGNAL_CLOSE = os.getenv("ALLOW_REVERSE_SIGNAL_CLOSE", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

PAIR_CONFIG = {
    "EURUSD": {
        "instrument": "EUR_USD",
        "sl_distance": 0.0020,
        "tp_distance": 0.0040,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00025,
        "min_m15_range": 0.0008
    },
    "GBPUSD": {
        "instrument": "GBP_USD",
        "sl_distance": 0.0025,
        "tp_distance": 0.0050,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00035,
        "min_m15_range": 0.0012
    },
    "XAUUSD": {
        "instrument": "XAU_USD",
        "sl_distance": 10.0,
        "tp_distance": 20.0,
        "pip_value_per_unit": 1.0,
        "max_units": 100,
        "max_spread": 0.60,
        "min_m15_range": 8.0
    }
}

# =========================
# MEMORY STATE
# =========================
STATE = {
    "daily_date": None,
    "daily_start_nav": None,
    "trades_today": 0,
    "last_signal_times": {},
    "last_signal_ids": {}
}

# =========================
# BASIC ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200


@app.route("/status", methods=["GET"])
def status():

    try:

        reset_daily_state_if_needed()

        return jsonify({
            "bot": "running",
            "risk_percent": RISK_PERCENT,
            "max_open_trades": MAX_OPEN_TRADES,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "max_daily_loss_percent": MAX_DAILY_LOSS_PERCENT,
            "cooldown_minutes": COOLDOWN_MINUTES,
            "session_filter": ENABLE_SESSION_FILTER,
            "session_start_hour": SESSION_START_HOUR,
            "session_end_hour": SESSION_END_HOUR,
            "spread_filter": ENABLE_SPREAD_FILTER,
            "volatility_filter": ENABLE_VOLATILITY_FILTER,
            "trend_filter": ENABLE_TREND_FILTER,
            "state": {
                "daily_date": str(STATE.get("daily_date")),
                "daily_start_nav": STATE.get("daily_start_nav"),
                "trades_today": STATE.get("trades_today")
            }

        }), 200

    except Exception as e:

        return jsonify({
            "bot": "running",
            "status_error": str(e)
        }), 200


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json()

    print("WEBHOOK:", data)

    signal = str(data.get("signal")).upper()
    pair = str(data.get("pair")).upper()

    if pair not in PAIR_CONFIG:
        return {"error": "unsupported pair"}, 400

    result = process_signal(signal, pair)

    return jsonify(result), 200


# =========================
# TIME HELPERS
# =========================
def now_local():
    return datetime.now(ZoneInfo(SESSION_TIMEZONE))


def now_utc():
    return datetime.now(timezone.utc)


def reset_daily_state_if_needed():

    today = now_local().date()

    if STATE["daily_date"] != today:

        STATE["daily_date"] = today
        STATE["daily_start_nav"] = None
        STATE["trades_today"] = 0


# =========================
# OANDA HELPERS
# =========================
def oanda_headers():

    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }


def get_account():

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"

    r = requests.get(url, headers=oanda_headers())

    return r.json()["account"]


def get_pricing(pair):

    instrument = PAIR_CONFIG[pair]["instrument"]

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"

    r = requests.get(
        url,
        headers=oanda_headers(),
        params={"instruments": instrument}
    )

    return r.json()["prices"][0]


# =========================
# TRADE LOGIC
# =========================
def calculate_units(pair, signal):

    cfg = PAIR_CONFIG[pair]

    account = get_account()

    nav = float(account["NAV"])

    risk_amount = nav * (RISK_PERCENT / 100)

    units = risk_amount / (cfg["sl_distance"] * cfg["pip_value_per_unit"])

    units = min(int(units), cfg["max_units"])

    if signal == "SELL":
        units = -units

    return units


def place_trade(signal, pair):

    cfg = PAIR_CONFIG[pair]

    units = calculate_units(pair, signal)

    payload = {
        "order": {
            "instrument": cfg["instrument"],
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "distance": str(cfg["sl_distance"])
            },
            "takeProfitOnFill": {
                "distance": str(cfg["tp_distance"])
            }
        }
    }

    if DRY_RUN:
        return {"dry_run": payload}

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"

    r = requests.post(url, headers=oanda_headers(), json=payload)

    return r.json()


def process_signal(signal, pair):

    if ENABLE_SESSION_FILTER:

        now = now_local()

        if not (SESSION_START_HOUR <= now.hour < SESSION_END_HOUR):

            return {
                "status": "skipped",
                "reason": "outside session"
            }

    price = get_pricing(pair)

    bid = float(price["bids"][0]["price"])
    ask = float(price["asks"][0]["price"])

    spread = ask - bid

    if ENABLE_SPREAD_FILTER:

        if spread > PAIR_CONFIG[pair]["max_spread"]:

            return {
                "status": "skipped",
                "reason": "spread too high"
            }

    result = place_trade(signal, pair)

    STATE["trades_today"] += 1

    return {
        "status": "trade executed",
        "pair": pair,
        "signal": signal,
        "spread": spread,
        "result": result
    }


if __name__ == "__main__":

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


