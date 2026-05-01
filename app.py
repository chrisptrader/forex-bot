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

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1.5"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))

STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "60"))

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "2"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "180"))

TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

LONDON_START = int(os.getenv("LONDON_START", "3"))
LONDON_END = int(os.getenv("LONDON_END", "6"))
NY_START = int(os.getenv("NY_START", "8"))
NY_END = int(os.getenv("NY_END", "11"))

ALLOWED_PAIRS = os.getenv(
    "ALLOWED_PAIRS",
    "EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CHF"
).replace(" ", "").split(",")

MAX_SAME_BIAS_TRADES = int(os.getenv("MAX_SAME_BIAS_TRADES", "1"))

# Trade management
BREAK_EVEN_TRIGGER = 8
PARTIAL_TP_TRIGGER = 15
TRAIL_TRIGGER = 20

# =========================

BASE = "https://api-fxtrade.oanda.com/v3" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com/v3"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

trade_count_today = 0
last_trade_time = None
last_day = None

partial_done = set()

# =========================
# HELPERS
# =========================

def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def get_price(pair):
    r = requests.get(f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/pricing",
                     headers=HEADERS, params={"instruments": pair})
    data = r.json()["prices"][0]
    return float(data["bids"][0]["price"]), float(data["asks"][0]["price"])


def get_balance():
    r = requests.get(f"{BASE}/accounts/{OANDA_ACCOUNT_ID}", headers=HEADERS)
    return float(r.json()["account"]["balance"])


def calculate_units(pair):
    balance = get_balance()
    risk = balance * (RISK_PERCENT / 100)
    pip = pip_size(pair)
    pip_value = 0.0001 if "JPY" not in pair else 0.01
    return int(risk / (STOP_LOSS_PIPS * pip_value))


def usd_bias(pair, action):
    if pair.endswith("_USD"):
        return "USD_STRONG" if action == "sell" else "USD_WEAK"
    if pair.startswith("USD_"):
        return "USD_STRONG" if action == "buy" else "USD_WEAK"
    return "OTHER"


def correlation_block(pair, action):
    new_bias = usd_bias(pair, action)
    if new_bias == "OTHER":
        return False

    count = 0
    trades = get_open_trades()

    for t in trades:
        p = t["instrument"]
        u = int(float(t["currentUnits"]))
        a = "buy" if u > 0 else "sell"

        if usd_bias(p, a) == new_bias:
            count += 1

    return count >= MAX_SAME_BIAS_TRADES


def get_open_trades():
    r = requests.get(f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/openTrades", headers=HEADERS)
    return r.json().get("trades", [])


def in_session():
    tz = pytz.timezone(TIMEZONE)
    hour = datetime.now(tz).hour
    return (LONDON_START <= hour <= LONDON_END) or (NY_START <= hour <= NY_END)


def can_trade():
    global trade_count_today, last_trade_time, last_day

    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    if last_day != now.date():
        trade_count_today = 0
        last_day = now.date()

    if trade_count_today >= MAX_TRADES_PER_DAY:
        return False, "daily trade cap"

    if last_trade_time:
        if (now - last_trade_time).total_seconds() < MIN_SECONDS_BETWEEN_TRADES:
            return False, "cooldown"

    if not in_session():
        return False, "outside session"

    return True, "ok"


# =========================
# TRADE MANAGEMENT
# =========================

def manage_trades():
    trades = get_open_trades()

    for t in trades:
        trade_id = t["id"]
        pair = t["instrument"]
        units = int(float(t["currentUnits"]))
        entry = float(t["price"])

        bid, ask = get_price(pair)
        pip = pip_size(pair)

        if units > 0:
            profit = (bid - entry) / pip
        else:
            profit = (entry - ask) / pip

        # BREAK EVEN
        if profit >= BREAK_EVEN_TRIGGER:
            new_sl = entry + pip if units > 0 else entry - pip
            requests.put(f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders",
                         headers=HEADERS,
                         json={"stopLoss": {"price": str(round(new_sl, 5))}})

        # PARTIAL CLOSE
        if profit >= PARTIAL_TP_TRIGGER and trade_id not in partial_done:
            close_units = int(abs(units) * 0.5)
            if close_units > 0:
                requests.put(f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close",
                             headers=HEADERS,
                             json={"units": str(close_units)})
                partial_done.add(trade_id)

        # TRAILING
        if profit >= TRAIL_TRIGGER:
            trail = bid - 10 * pip if units > 0 else ask + 10 * pip
            requests.put(f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders",
                         headers=HEADERS,
                         json={"stopLoss": {"price": str(round(trail, 5))}})


# =========================
# ROUTES
# =========================

@app.route("/")
def home():
    return "V46 LIVE 🔥"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return {"error": "bad passphrase"}, 403

    pair = data.get("pair")
    action = data.get("action")

    manage_trades()

    if pair not in ALLOWED_PAIRS:
        return {"blocked": "pair not allowed"}

    ok, reason = can_trade()
    if not ok:
        return {"blocked": reason}

    if correlation_block(pair, action):
        return {"blocked": "correlation"}

    units = calculate_units(pair)

    bid, ask = get_price(pair)
    pip = pip_size(pair)

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

    requests.post(f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/orders",
                  headers=HEADERS,
                  json=order)

    return {"status": "trade placed", "pair": pair}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
