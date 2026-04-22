import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# =====================
# CONFIG
# =====================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_URL = "https://api-fxpractice.oanda.com/v3/accounts"

RISK = 0.02
STOP_LOSS_PIPS = 15
TAKE_PROFIT_PIPS = 30
LOT_SIZE = 1000

# =====================
# LOGGING
# =====================
logging.basicConfig(level=logging.INFO)

# =====================
# HELPER FUNCTIONS
# =====================

def is_market_sideways():
    # TEMP basic filter (we will improve in V29)
    return False


def has_confirmation(data):
    # Placeholder confirmation logic
    return True


def place_trade(pair, direction):
    url = f"{OANDA_URL}/{OANDA_ACCOUNT_ID}/orders"

    units = LOT_SIZE if direction == "BUY" else -LOT_SIZE

    order = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, json=order, headers=headers)

    logging.info(f"TRADE RESPONSE: {response.text}")
    return response.json()


# =====================
# WEBHOOK
# =====================

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if not data:
        return jsonify({"error": "No data"}), 400

    pair = data.get("pair")
    action = data.get("action")

    logging.info(f"WEBHOOK RECEIVED {pair} {action}")

    # FILTERS
    if is_market_sideways():
        logging.info("BLOCKED: market sideways")
        return jsonify({"status": "blocked sideways"})

    if not has_confirmation(data):
        logging.info("BLOCKED: no confirmation")
        return jsonify({"status": "blocked no confirmation"})

    # PLACE TRADE
    result = place_trade(pair, action)

    return jsonify(result)


# =====================
# HEALTH CHECK
# =====================

@app.route("/")
def home():
    return "Bot is running"


# =====================
# RUN
# =====================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
