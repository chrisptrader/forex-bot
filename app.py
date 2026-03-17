from flask import Flask, request, jsonify
import requests
import os
import threading
import time

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.environ.get("OANDA_API_KEY")
ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
BASE_URL = "https://api-fxpractice.oanda.com/v3"

RISK_PERCENT = 0.02 # 2% risk
PAIRS = ["EURUSD", "GBPUSD", "XAUUSD"]

# Trailing stop settings
ENABLE_TRAILING = True
TRAIL_TO_BREAKEVEN_R = 1.0

# =========================
# HELPER FUNCTIONS
# =========================
def get_account_balance():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/summary"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    r = requests.get(url, headers=headers).json()
    return float(r["account"]["balance"])


def calculate_units(pair):
    balance = get_account_balance()
    risk_amount = balance * RISK_PERCENT

    # Simple lot logic (can upgrade later)
    if "XAU" in pair:
        return int(risk_amount * 2)
    else:
        return int(risk_amount * 1000)


def place_trade(signal, pair):
    units = calculate_units(pair)

    if signal == "SELL":
        units = -units

    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, json=data, headers=headers)
    return response.json()


# =========================
# TRAILING STOP MANAGER
# =========================
def manage_trades():
    while True:
        try:
            url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
            headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
            trades = requests.get(url, headers=headers).json().get("trades", [])

            for trade in trades:
                trade_id = trade["id"]
                price = float(trade["price"])
                current = float(trade["currentPrice"])
                pl = float(trade["unrealizedPL"])

                # Simple trailing logic
                if ENABLE_TRAILING and pl > 0:
                    new_sl = current

                    data = {
                        "stopLoss": {
                            "price": str(new_sl)
                        }
                    }

                    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
                    requests.put(url, json=data, headers=headers)

        except Exception as e:
            print("Error in trailing:", e)

        time.sleep(10)


# Start background thread
threading.Thread(target=manage_trades, daemon=True).start()

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Forex bot is running!"


@app.route("/status")
def status():
    return jsonify({
        "bot": "running",
        "risk_percent": RISK_PERCENT,
        "pairs": PAIRS,
        "trailing": ENABLE_TRAILING
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    signal = data.get("signal")
    pair = data.get("pair")

    if pair not in PAIRS:
        return jsonify({"error": "Pair not allowed"}), 400

    result = place_trade(signal, pair)
    return jsonify(result)


# =========================
# RUN APP (FIXED FOR RENDER)
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
