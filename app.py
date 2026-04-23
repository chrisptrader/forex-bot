import os
import time
import threading
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ================= CONFIG =================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234").strip()

BASE_URL = "https://api-fxpractice.oanda.com/v3" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com/v3"

PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "EUR_USD,GBP_USD").split(",")]

# Risk
USE_RISK_PERCENT = os.getenv("USE_RISK_PERCENT", "true").lower() == "true"
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "2"))
MAX_UNITS = int(os.getenv("MAX_UNITS", "10000"))
MIN_UNITS = int(os.getenv("MIN_UNITS", "1000"))

STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "10"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "12"))

# Trade control
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "1"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_TRADES_PER_PAIR", "1"))

# Management (FIXED SAFE BLOCK)
USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true").lower() == "true"
BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "4"))
BREAK_EVEN_PLUS_PIPS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", "1"))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true").lower() == "true"
TRAILING_TRIGGER_PIPS = float(os.getenv("TRAILING_TRIGGER_PIPS", "6"))
TRAILING_DISTANCE_PIPS = float(os.getenv("TRAILING_DISTANCE_PIPS", "3"))

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

# ================= HELPERS =================
def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001

def get_price(pair):
    url = f"{BASE_URL}/accounts/{OANDA_ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=HEADERS, params={"instruments": pair})
    data = r.json()
    p = data["prices"][0]
    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])
    return bid, ask

def get_balance():
    url = f"{BASE_URL}/accounts/{OANDA_ACCOUNT_ID}/summary"
    r = requests.get(url, headers=HEADERS)
    return float(r.json()["account"]["balance"])

def calculate_units(pair):
    if not USE_RISK_PERCENT:
        return MAX_UNITS

    balance = get_balance()
    risk = balance * (RISK_PERCENT / 100)

    units = int((risk / STOP_LOSS_PIPS) * 1000)
    units = max(MIN_UNITS, min(units, MAX_UNITS))
    return units

def open_trades():
    url = f"{BASE_URL}/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    return requests.get(url, headers=HEADERS).json().get("trades", [])

# ================= TRADE =================
def place_trade(pair, action):
    bid, ask = get_price(pair)
    ps = pip_size(pair)

    is_buy = action == "buy"
    price = ask if is_buy else bid

    units = calculate_units(pair)
    if action == "sell":
        units = -units

    sl = price - STOP_LOSS_PIPS * ps if is_buy else price + STOP_LOSS_PIPS * ps
    tp = price + TAKE_PROFIT_PIPS * ps if is_buy else price - TAKE_PROFIT_PIPS * ps

    payload = {
        "order": {
            "instrument": pair,
            "units": str(units),
            "type": "MARKET",
            "stopLossOnFill": {"price": str(sl)},
            "takeProfitOnFill": {"price": str(tp)},
        }
    }

    url = f"{BASE_URL}/accounts/{OANDA_ACCOUNT_ID}/orders"
    return requests.post(url, headers=HEADERS, json=payload).json()

# ================= ROUTES =================
@app.route("/")
def home():
    return "Bot Running FIXED V36 ✅"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return {"error": "bad passphrase"}

    pair = data.get("pair")
    action = data.get("action")

    if pair not in PAIRS:
        return {"error": "pair not allowed"}

    if len(open_trades()) >= MAX_OPEN_TRADES:
        return {"msg": "max trades reached"}

    return place_trade(pair, action)

# ================= START =================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
