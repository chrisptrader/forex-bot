from flask import Flask, request
import requests
import os
import time

app = Flask(__name__)

# ====== CONFIG ======
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")

BASE_URL = "https://api-fxpractice.oanda.com/v3"

# STRATEGY SETTINGS (AGGRESSIVE)
STOP_LOSS_PIPS = 10
TAKE_PROFIT_PIPS = 20
BREAK_EVEN_TRIGGER = 5
TRAILING_START = 8
TRAILING_DISTANCE = 5
MAX_TRADES = 2

# =====================

def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    r = requests.get(url, headers=headers)
    return r.json().get("trades", [])

def count_trades():
    return len(get_open_trades())

def place_trade(pair, action):
    if count_trades() >= MAX_TRADES:
        print("BLOCKED: max total trades reached")
        return

    units = 5000 if action == "buy" else -5000

    price_url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    price_data = requests.get(price_url, headers=headers).json()

    price = float(price_data["prices"][0]["bids"][0]["price"])

    pip_value = 0.0001 if "JPY" not in pair else 0.01

    if action == "buy":
        sl = price - (STOP_LOSS_PIPS * pip_value)
        tp = price + (TAKE_PROFIT_PIPS * pip_value)
    else:
        sl = price + (STOP_LOSS_PIPS * pip_value)
        tp = price - (TAKE_PROFIT_PIPS * pip_value)

    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": str(round(sl, 5))},
            "takeProfitOnFill": {"price": str(round(tp, 5))}
        }
    }

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    r = requests.post(url, json=data, headers=headers)

    print("TRADE RESPONSE:", r.json())


def manage_trades():
    trades = get_open_trades()
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}

    for trade in trades:
        trade_id = trade["id"]
        pair = trade["instrument"]
        entry = float(trade["price"])
        current = float(trade["currentPrice"])

        direction = 1 if float(trade["currentUnits"]) > 0 else -1
        pips = (current - entry) * 10000 * direction

        # BREAK EVEN
        if pips >= BREAK_EVEN_TRIGGER:
            sl_price = entry
            url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
            data = {"stopLoss": {"price": str(round(sl_price, 5))}}
            requests.put(url, json=data, headers=headers)
            print(f"Moved SL to BE for {trade_id}")

        # TRAILING STOP
        if pips >= TRAILING_START:
            new_sl = current - (TRAILING_DISTANCE * 0.0001 * direction)
            url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
            data = {"stopLoss": {"price": str(round(new_sl, 5))}}
            requests.put(url, json=data, headers=headers)
            print(f"Trailing SL updated for {trade_id}")


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("WEBHOOK RECEIVED:", data)

    pair = data.get("pair")
    action = data.get("action")

    place_trade(pair, action)
    manage_trades()

    return "OK"


@app.route("/")
def home():
    return "V34 BOT RUNNING 🚀"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
