from flask import Flask, request, jsonify
import requests
import os
import time
from datetime import datetime, timezone

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = "98969b4679d01a139e86d66ee8694bef-6f46ee09cb98d79db97096b393622766"
ACCOUNT_ID = "101-001-37221732-001"
BASE_URL = "https://api-fxpractice.oanda.com/v3"

ALLOWED_PAIRS = ["EUR_USD"]
RISK_PERCENT = 0.75
MAX_OPEN_TRADES = 1
COOLDOWN_SECONDS = 300

STOP_LOSS_PIPS = 20
TAKE_PROFIT_PIPS = 30
BREAK_EVEN_PIPS = 10

MAX_DAILY_LOSS_PERCENT = 2.0

last_trade_time = {}
daily_start_balance = None
daily_date = None

# =========================
# HELPERS
# =========================
def now_utc_date():
    return datetime.now(timezone.utc).date().isoformat()

def normalize_pair(pair):
    return {
        "EURUSD": "EUR_USD"
    }.get(pair, pair)

def oanda_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

def pip_size(pair):
    return 0.0001

def format_price(pair, price):
    return f"{price:.5f}"

# =========================
# OANDA FUNCTIONS
# =========================
def get_account():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/summary"
    return requests.get(url, headers=oanda_headers()).json()["account"]

def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    return requests.get(url, headers=oanda_headers()).json().get("trades", [])

def get_price(pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"
    data = requests.get(url, headers=oanda_headers()).json()
    bid = float(data["prices"][0]["bids"][0]["price"])
    ask = float(data["prices"][0]["asks"][0]["price"])
    return bid, ask

# =========================
# BREAK EVEN LOGIC
# =========================
def move_sl_to_be(trade_id, price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"

    data = {
        "stopLoss": {
            "price": price
        }
    }

    res = requests.put(url, json=data, headers=oanda_headers())
    print("🔒 BE moved:", res.json())

def check_breakeven():
    trades = get_open_trades()

    for trade in trades:
        entry = float(trade["price"])
        units = float(trade["currentUnits"])
        trade_id = trade["id"]
        pair = trade["instrument"]

        bid, ask = get_price(pair)
        current = ask if units > 0 else bid

        pips = abs(current - entry) / pip_size(pair)

        print("Pips in profit:", pips)

        if pips >= BREAK_EVEN_PIPS:
            move_sl_to_be(trade_id, format_price(pair, entry))

# =========================
# RISK + TRADE
# =========================
def place_trade(signal, pair):
    account = get_account()
    balance = float(account["balance"])
    risk = balance * (RISK_PERCENT / 100)

    units = int(risk / 2)
    units = max(units, 1000)

    if signal == "SELL":
        units *= -1

    bid, ask = get_price(pair)
    entry = ask if signal == "BUY" else bid

    pip = pip_size(pair)

    if signal == "BUY":
        sl = entry - STOP_LOSS_PIPS * pip
        tp = entry + TAKE_PROFIT_PIPS * pip
    else:
        sl = entry + STOP_LOSS_PIPS * pip
        tp = entry - TAKE_PROFIT_PIPS * pip

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"

    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": format_price(pair, sl)},
            "takeProfitOnFill": {"price": format_price(pair, tp)}
        }
    }

    print("🚀 Sending:", data)

    res = requests.post(url, json=data, headers=oanda_headers())
    print("💰 Response:", res.json())

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Bot running with BE 🔥"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    print("Webhook:", data)

    signal = data.get("signal")
    pair = normalize_pair(data.get("pair"))

    place_trade(signal, pair)

    # 👇 THIS IS THE NEW MAGIC
    check_breakeven()

    return jsonify({"status": "ok"})

# =========================
# RUN
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
