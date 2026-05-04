import os
import time
import threading
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# =============================
# ENV VARIABLES
# =============================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
BASE_URL = "https://api-fxpractice.oanda.com/v3"

WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE")

ALLOW_BUY = os.getenv("ALLOW_BUY", "true") == "true"
ALLOW_SELL = os.getenv("ALLOW_SELL", "true") == "true"

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 3))
ONE_TRADE_PER_PAIR = os.getenv("ONE_TRADE_PER_PAIR", "true") == "true"
COOLDOWN_SECONDS = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", 300))

RISK_PERCENT = float(os.getenv("RISK_PERCENT", 1.0))

# ===== TRADE MANAGEMENT =====
USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true") == "true"
BREAK_EVEN_TRIGGER = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", 10))
BREAK_EVEN_PLUS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", 2))

USE_PARTIAL_CLOSE = os.getenv("USE_PARTIAL_CLOSE", "true") == "true"
PARTIAL_TRIGGER = float(os.getenv("PARTIAL_CLOSE_TRIGGER_PIPS", 12))
PARTIAL_PERCENT = float(os.getenv("PARTIAL_CLOSE_PERCENT", 50))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true") == "true"
TRAILING_TRIGGER = float(os.getenv("TRAILING_TRIGGER_PIPS", 12))
TRAILING_DISTANCE = float(os.getenv("TRAILING_DISTANCE_PIPS", 6))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", 10))

# =============================
# STATE
# =============================
last_trade_time = {}
open_trades = {}

headers = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

# =============================
# UTIL
# =============================
def get_price(pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"
    r = requests.get(url, headers=headers).json()
    price = float(r['prices'][0]['bids'][0]['price'])
    return price

def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=headers).json()
    return r.get("trades", [])

def close_partial(trade_id, units):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/close"
    data = {"units": str(units)}
    requests.put(url, headers=headers, json=data)

def modify_sl(trade_id, price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    data = {
        "stopLoss": {
            "price": str(price)
        }
    }
    requests.put(url, headers=headers, json=data)

# =============================
# TRADE MANAGER LOOP
# =============================
def trade_manager():
    while True:
        try:
            trades = get_open_trades()

            for t in trades:
                trade_id = t["id"]
                pair = t["instrument"]
                entry = float(t["price"])
                units = float(t["currentUnits"])

                price = get_price(pair)

                direction = 1 if units > 0 else -1
                pips = (price - entry) * 10000 * direction

                print(f"[MANAGER] {pair} | PIPS: {round(pips,1)}")

                # ===== BREAK EVEN =====
                if USE_BREAK_EVEN and pips >= BREAK_EVEN_TRIGGER:
                    new_sl = entry + (BREAK_EVEN_PLUS / 10000 * direction)
                    modify_sl(trade_id, new_sl)
                    print(f"BE MOVED | {pair} | SL: {new_sl}")

                # ===== PARTIAL CLOSE =====
                if USE_PARTIAL_CLOSE and pips >= PARTIAL_TRIGGER:
                    close_units = int(abs(units) * (PARTIAL_PERCENT / 100))
                    close_units = close_units if units > 0 else -close_units
                    close_partial(trade_id, close_units)
                    print(f"PARTIAL CLOSED | {pair} | {PARTIAL_PERCENT}%")

                # ===== TRAILING STOP =====
                if USE_TRAILING_STOP and pips >= TRAILING_TRIGGER:
                    new_sl = price - (TRAILING_DISTANCE / 10000 * direction)
                    modify_sl(trade_id, new_sl)
                    print(f"TRAILING | {pair} | SL: {new_sl}")

        except Exception as e:
            print("Manager error:", e)

        time.sleep(POLL_SECONDS)

# =============================
# WEBHOOK
# =============================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "Invalid passphrase"}), 403

    pair = data.get("pair")
    action = data.get("action")

    print(f"SIGNAL | {pair} | {action}")

    # ===== FILTERS =====
    now = time.time()

    if pair in last_trade_time and now - last_trade_time[pair] < COOLDOWN_SECONDS:
        print("BLOCKED | Cooldown")
        return "cooldown"

    trades = get_open_trades()

    if len(trades) >= MAX_OPEN_TRADES:
        print("BLOCKED | Max trades reached")
        return "max trades"

    if ONE_TRADE_PER_PAIR:
        for t in trades:
            if t["instrument"] == pair:
                print("BLOCKED | Trade already open")
                return "duplicate"

    if action == "buy" and not ALLOW_BUY:
        return "buy disabled"

    if action == "sell" and not ALLOW_SELL:
        return "sell disabled"

    # ===== EXECUTE TRADE =====
    units = 1000  # simple fixed size for now (we upgrade later)

    order = {
        "order": {
            "instrument": pair,
            "units": str(units if action == "buy" else -units),
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    r = requests.post(url, headers=headers, json=order)

    if r.status_code == 201:
        print(f"TRADE EXECUTED | {pair} | {action}")
        last_trade_time[pair] = now
    else:
        print("ORDER ERROR:", r.text)

    return "ok"

# =============================
# START
# =============================
threading.Thread(target=trade_manager, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
