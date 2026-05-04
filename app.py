import os
import time
import threading
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# =============================
# CONFIG
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

# ===== RISK / SIZE =====
FIXED_UNITS = int(os.getenv("FIXED_UNITS", 1000))

# ===== ENTRY PROTECTION =====
SL_PIPS = float(os.getenv("STOP_LOSS_PIPS", 15))
TP_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", 30))

# ===== MANAGEMENT =====
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

headers = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

# =============================
# HELPERS
# =============================
def get_price(pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"
    r = requests.get(url, headers=headers).json()
    return float(r['prices'][0]['bids'][0]['price'])

def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=headers).json()
    return r.get("trades", [])

def close_partial(trade_id, units):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/close"
    requests.put(url, headers=headers, json={"units": str(units)})

def modify_sl(trade_id, price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    requests.put(url, headers=headers, json={
        "stopLoss": {"price": str(round(price, 5))}
    })

# =============================
# TRADE MANAGER
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

                print(f"[MANAGER] {pair} | {round(pips,1)} pips")

                # ===== BREAK EVEN =====
                if USE_BREAK_EVEN and pips >= BREAK_EVEN_TRIGGER:
                    new_sl = entry + (BREAK_EVEN_PLUS / 10000 * direction)
                    modify_sl(trade_id, new_sl)
                    print(f"BE MOVED | {pair}")

                # ===== PARTIAL =====
                if USE_PARTIAL_CLOSE and pips >= PARTIAL_TRIGGER:
                    close_units = int(abs(units) * (PARTIAL_PERCENT / 100))
                    close_units = close_units if units > 0 else -close_units
                    close_partial(trade_id, close_units)
                    print(f"PARTIAL CLOSED | {pair}")

                # ===== TRAILING =====
                if USE_TRAILING_STOP and pips >= TRAILING_TRIGGER:
                    new_sl = price - (TRAILING_DISTANCE / 10000 * direction)
                    modify_sl(trade_id, new_sl)
                    print(f"TRAILING | {pair}")

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
        return "invalid"

    pair = data.get("pair")
    action = data.get("action")

    print(f"SIGNAL | {pair} | {action}")

    now = time.time()

    # ===== COOLDOWN =====
    if pair in last_trade_time and now - last_trade_time[pair] < COOLDOWN_SECONDS:
        print("BLOCKED | cooldown")
        return "cooldown"

    trades = get_open_trades()

    # ===== MAX TRADES =====
    if len(trades) >= MAX_OPEN_TRADES:
        print("BLOCKED | max trades")
        return "max"

    # ===== ONE PER PAIR =====
    if ONE_TRADE_PER_PAIR:
        for t in trades:
            if t["instrument"] == pair:
                print("BLOCKED | already open")
                return "duplicate"

    # ===== BUY/SELL FILTER =====
    if action == "buy" and not ALLOW_BUY:
        return "buy disabled"
    if action == "sell" and not ALLOW_SELL:
        return "sell disabled"

    # ===== EXECUTION =====
    price = get_price(pair)

    if action == "buy":
        sl = price - (SL_PIPS / 10000)
        tp = price + (TP_PIPS / 10000)
        units = FIXED_UNITS
    else:
        sl = price + (SL_PIPS / 10000)
        tp = price - (TP_PIPS / 10000)
        units = -FIXED_UNITS

    order = {
        "order": {
            "instrument": pair,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": str(round(sl, 5))
            },
            "takeProfitOnFill": {
                "price": str(round(tp, 5))
            }
        }
    }

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    r = requests.post(url, headers=headers, json=order)

    if r.status_code == 201:
        print(f"TRADE EXECUTED | {pair}")
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
