from flask import Flask, request, jsonify
import os
import requests

app = Flask(__name__)

# OANDA credentials from environment variables
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

# Trade size
OANDA_UNITS = int(os.getenv("OANDA_UNITS", "1000"))

# Risk management
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.0015"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.0030"))


@app.route("/")
def home():
    return "Bot is running!"


def place_oanda_market_order(signal, pair):

    instrument = pair.replace("/", "_")

    # BUY = positive units / SELL = negative units
    units = OANDA_UNITS if signal == "BUY" else -OANDA_UNITS

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"

    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    order_data = {
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

    response = requests.post(url, headers=headers, json=order_data)

    print("OANDA RESPONSE:", response.text)

    return response.json()


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
    
    


