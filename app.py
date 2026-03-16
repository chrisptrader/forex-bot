
from flask import Flask, request, jsonify
import os
import requests
import math
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

# OANDA SETTINGS
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL")

# RISK SETTINGS
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))

# SESSION SETTINGS
ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").lower() == "true"
SESSION_TIMEZONE = os.getenv("SESSION_TIMEZONE", "America/New_York")
SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "4"))
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "11"))

# PROTECTIONS
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))
ENABLE_VOLATILITY_FILTER = os.getenv("ENABLE_VOLATILITY_FILTER", "true").lower() == "true"
DUPLICATE_SIGNAL_SECONDS = int(os.getenv("DUPLICATE_SIGNAL_SECONDS", "90"))

STATE = {
    "trades_today": 0,
    "last_trade_time": {},
    "last_signal": {}
}

PAIR_CONFIG = {
    "EURUSD": {
        "instrument": "EUR_USD",
        "sl": 0.002,
        "tp": 0.004,
        "pip_value": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00025
    },
    "GBPUSD": {
        "instrument": "GBP_USD",
        "sl": 0.0025,
        "tp": 0.005,
        "pip_value": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00035
    },
    "XAUUSD": {
        "instrument": "XAU_USD",
        "sl": 10,
        "tp": 20,
        "pip_value": 1,
        "max_units": 100,
        "max_spread": 0.60
    }
}


@app.route("/")
def home():
    return "Bot is running!", 200


@app.route("/status")
def status():
    return {
        "bot": "running",
        "risk_percent": RISK_PERCENT,
        "max_open_trades": MAX_OPEN_TRADES,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "daily_loss_limit": MAX_DAILY_LOSS_PERCENT,
        "session_filter": ENABLE_SESSION_FILTER,
        "session_start": SESSION_START_HOUR,
        "session_end": SESSION_END_HOUR,
        "volatility_filter": ENABLE_VOLATILITY_FILTER
    }, 200


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.json

    signal = data.get("signal")
    pair = data.get("pair")

    if pair not in PAIR_CONFIG:
        return {"error": "unsupported pair"}, 400

    if ENABLE_SESSION_FILTER:
        now = datetime.now(ZoneInfo(SESSION_TIMEZONE))
        if not (SESSION_START_HOUR <= now.hour < SESSION_END_HOUR):
            return {"status": "outside trading session"}, 200

    if STATE["trades_today"] >= MAX_TRADES_PER_DAY:
        return {"status": "daily trade limit reached"}, 200

    spread_ok, spread = check_spread(pair)
    if not spread_ok:
        return {"status": "spread too high", "spread": spread}, 200

    units = calculate_units(pair, signal)

    result = place_trade(pair, signal, units)

    STATE["trades_today"] += 1

    return result


def oanda_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }


def check_spread(pair):

    instrument = PAIR_CONFIG[pair]["instrument"]

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"

    r = requests.get(
        url,
        headers=oanda_headers(),
        params={"instruments": instrument}
    )

    data = r.json()["prices"][0]

    bid = float(data["bids"][0]["price"])
    ask = float(data["asks"][0]["price"])

    spread = ask - bid

    return spread <= PAIR_CONFIG[pair]["max_spread"], spread


def calculate_units(pair, signal):

    cfg = PAIR_CONFIG[pair]

    account = requests.get(
        f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary",
        headers=oanda_headers()
    ).json()

    nav = float(account["account"]["NAV"])

    risk_amount = nav * (RISK_PERCENT / 100)

    units = risk_amount / (cfg["sl"] * cfg["pip_value"])

    units = min(int(units), cfg["max_units"])

    if signal == "SELL":
        units = -units

    return units


def place_trade(pair, signal, units):

    cfg = PAIR_CONFIG[pair]

    order = {
        "order": {
            "instrument": cfg["instrument"],
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "distance": str(cfg["sl"])
            },
            "takeProfitOnFill": {
                "distance": str(cfg["tp"])
            }
        }
    }

    r = requests.post(
        f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders",
        headers=oanda_headers(),
        json=order
    )

    return r.json()


if __name__ == "__main__":
    app.run()
