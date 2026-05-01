import os
import requests
from flask import Flask, request, jsonify
from datetime import datetime
import pytz

app = Flask(__name__)

# =========================
# ENV
# =========================

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")

# 🔥 NEW (V45)
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "2"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))

STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "80"))

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
ONE_TRADE_PER_PAIR = True

TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

LONDON_START = int(os.getenv("LONDON_START", "3"))
LONDON_END = int(os.getenv("LONDON_END", "6"))
NY_START = int(os.getenv("NY_START", "8"))
NY_END = int(os.getenv("NY_END", "11"))

MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "6"))
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "120"))

# =========================

if OANDA_ENV == "live":
    BASE = "https://api-fxtrade.oanda.com/v3"
else:
    BASE = "https://api-fxpractice.oanda.com/v3"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

trade_count_today = 0
last_trade_time = None
last_reset_day = None
daily_loss = 0

# =========================
# HELPERS
# =========================

def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def get_price(pair):
    r = requests.get(
        f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/pricing",
        headers=HEADERS,
        params={"instruments": pair}
    )
    data = r.json()["prices"][0]
    return float(data["bids"][0]["price"]), float(data["asks"][0]["price"])


def get_balance():
    r = requests.get(f"{BASE}/accounts/{OANDA_ACCOUNT_ID}", headers=HEADERS)
    return float(r.json()["account"]["balance"])


def calculate_units(pair):
    balance = get_balance()
    risk_amount = balance * (RISK_PERCENT / 100)

    pip = pip_size(pair)

    # approximate pip value
    pip_value_per_unit = 0.0001 if "JPY" not in pair else 0.01

    units = risk_amount / (STOP_LOSS_PIPS * pip_value_per_unit)

    return int(units)


def in_session():
    tz = pytz.timezone(TIMEZONE)
    hour = datetime.now(tz).hour
    return (LONDON_START <= hour <= LONDON_END) or (NY_START <= hour <= NY_END)


def can_trade():
    global trade_count_today, last_trade_time, last_reset_day, daily_loss

    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    if last_reset_day != now.date():
        trade_count_today = 0
        daily_loss = 0
        last_reset_day = now.date()

    balance = get_balance()
    max_loss = balance * (MAX_DAILY_LOSS_PERCENT / 100)

    if daily_loss >= max_loss:
        return False, "daily loss limit hit"

    if trade_count_today >= MAX_TRADES_PER_DAY:
        return False, "max trades per day"

    if last_trade_time:
        seconds = (now - last_trade_time).total_seconds()
        if seconds < MIN_SECONDS_BETWEEN_TRADES:
            return False, "cooldown"

    if not in_session():
        return False, "outside session"

    return True, "ok"


def place_trade(pair, action):
    global trade_count_today, last_trade_time

    bid, ask = get_price(pair)
    pip = pip_size(pair)

    units = calculate_units(pair)

    if action == "buy":
        entry = ask
        sl = entry - STOP_LOSS_PIPS * pip
        tp = entry + TAKE_PROFIT_PIPS * pip
    else:
        units = -units
        entry = bid
        sl = entry + STOP_LOSS_PIPS * pip
        tp = entry - TAKE_PROFIT_PIPS * pip

    order = {
        "order": {
            "instrument": pair,
            "units": str(units),
            "type": "MARKET",
            "stopLossOnFill": {"price": str(round(sl, 5))},
            "takeProfitOnFill": {"price": str(round(tp, 5))}
        }
    }

    r = requests.post(
        f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/orders",
        headers=HEADERS,
        json=order
    )

    trade_count_today += 1
    last_trade_time = datetime.now(pytz.timezone(TIMEZONE))

    return r.json()

# =========================
# ROUTES
# =========================

@app.route("/")
def home():
    return "V45 running 💰"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return {"error": "bad passphrase"}, 403

    pair = data.get("pair")
    action = data.get("action")

    allowed, reason = can_trade()
    if not allowed:
        return {"blocked": reason}

    result = place_trade(pair, action)

    return {
        "status": "trade_sent",
        "pair": pair,
        "action": action,
        "result": result
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
