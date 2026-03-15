from flask import Flask, request, jsonify
import os
import requests

app = Flask(__name__)

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

# Demo position size
OANDA_UNITS = int(os.getenv("OANDA_UNITS", "1000"))

# Stop loss / take profit distance
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.0015"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.0030"))


@app.route("/")
def home():
    return "Bot is running"


def place_oanda_market_order(signal, pair):

    instrument = pair.replace("/", "_")

    units = OANDA_UNITS if signal == "BUY" else -OANDA_UNITS

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"

    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    order = {
        "order": {
            "instrument": instrument,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "distance": str(STOP_LOSS)
            },
            "takeProfitOnFill": {
                "distance": str(TAKE_PROFIT)
            }
        }
    }

    r = requests.post(url, headers=headers, json=order)

    print("OANDA RESPONSE:", r.text)

    return r.json()


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.json

    print("WEBHOOK RECEIVED:", data)

    signal = data.get("signal")
    pair = data.get("pair")

    if not signal or not pair:
        return jsonify({"error": "missing signal or pair"}), 400

    result = place_oanda_market_order(signal, pair)

    return jsonify({
        "status": "order sent",
        "oanda": result
    })            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    r = requests.post(url, headers=headers, json=data)

    print("OANDA RESPONSE:", r.text)

    return r.json()


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.json

    print("WEBHOOK RECEIVED:", data)

    signal = data.get("signal")
    pair = data.get("pair")

    if not signal or not pair:
        return jsonify({"error": "missing signal or pair"}), 400

    result = place_oanda_market_order(signal, pair)

    return jsonify({
        "status": "order sent",
        "oanda": result
    })
