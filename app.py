from flask import Flask, request, jsonify
import requests
import os
import time

app = Flask(__name__)

# ========================
# ENV VARIABLES
# ========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
BASE_URL = "https://api-fxpractice.oanda.com/v3"

WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")

# Trading settings
FIXED_UNITS = int(os.getenv("FIXED_UNITS", 5000))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 3))

# Profit lock + runner
USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true") == "true"
BREAK_EVEN_TRIGGER = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", 8))
BREAK_EVEN_PLUS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", 1))

USE_TRAILING = os.getenv("USE_TRAILING_STOP", "true") == "true"
TRAILING_TRIGGER = float(os.getenv("TRAILING_TRIGGER_PIPS", 12))
TRAILING_DISTANCE = float(os.getenv("TRAILING_DISTANCE_PIPS", 8))

# ========================
# HELPERS
# ========================
def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    r = requests.get(url, headers=headers)
    return r.json().get("trades", [])


def get_price(pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    params = {"instruments": pair}
    r = requests.get(url, headers=headers, params=params)
    data = r.json()

    price = data["prices"][0]
    bid = float(price["bids"][0]["price"])
    ask = float(price["asks"][0]["price"])

    return bid, ask


def modify_trade(trade_id, stop_loss_price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "stopLoss": {
            "price": str(stop_loss_price)
        }
    }

    requests.put(url, headers=headers, json=data)


def create_order(pair, units, side):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    order = {
        "order": {
            "instrument": pair,
            "units": str(units if side == "buy" else -units),
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    r = requests.post(url, headers=headers, json=order)
    print("ORDER RESPONSE:", r.json())


# ========================
# TRADE MANAGEMENT (FIXED)
# ========================
def manage_trades():
    trades = get_open_trades()

    for trade in trades:
        try:
            trade_id = trade["id"]
            pair = trade["instrument"]
            entry = float(trade["price"])
            units = int(trade["currentUnits"])

            bid, ask = get_price(pair)

            # Correct price based on direction
            current = bid if units > 0 else ask

            pip_size = 0.01 if "JPY" in pair else 0.0001
            profit_pips = (current - entry) / pip_size if units > 0 else (entry - current) / pip_size

            print(f"{pair} | Profit: {profit_pips:.2f} pips")

            # ========================
            # BREAK EVEN
            # ========================
            if USE_BREAK_EVEN and profit_pips >= BREAK_EVEN_TRIGGER:
                new_sl = entry + (BREAK_EVEN_PLUS * pip_size if units > 0 else -BREAK_EVEN_PLUS * pip_size)
                print(f"Moving SL to BE: {new_sl}")
                modify_trade(trade_id, new_sl)

            # ========================
            # TRAILING STOP (RUNNER)
            # ========================
            if USE_TRAILING and profit_pips >= TRAILING_TRIGGER:
                if units > 0:
                    new_sl = current - (TRAILING_DISTANCE * pip_size)
                else:
                    new_sl = current + (TRAILING_DISTANCE * pip_size)

                print(f"Trailing SL: {new_sl}")
                modify_trade(trade_id, new_sl)

        except Exception as e:
            print("Manage trade error:", e)


# ========================
# WEBHOOK
# ========================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("WEBHOOK:", data)

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "unauthorized"}), 403

    pair = data.get("pair")
    action = data.get("action")

    trades = get_open_trades()

    if len(trades) >= MAX_OPEN_TRADES:
        print("BLOCKED: max trades")
        return jsonify({"status": "blocked"})

    create_order(pair, FIXED_UNITS, action)

    return jsonify({"status": "ok"})


# ========================
# LOOP
# ========================
def run_manager():
    while True:
        manage_trades()
        time.sleep(10)


import threading
threading.Thread(target=run_manager).start()

@app.route("/")
def home():
    return "Bot Running V_FINAL 🚀"
