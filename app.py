from flask import Flask, request, jsonify
import os
import requests

app = Flask(__name__)

OANDA_API_KEY = os.environ.get("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
OANDA_ENV = os.environ.get("OANDA_ENV", "practice")

BASE_URL = "https://api-fxpractice.oanda.com" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com"

@app.route("/")
def home():
    return "Bot Running V35 🚀"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("WEBHOOK RECEIVED:", data)

    if not data:
        return jsonify({"error": "No data"}), 400

    if data.get("passphrase") != "1234":
        return jsonify({"error": "Invalid passphrase"}), 403

    pair = data.get("pair")
    action = data.get("action")

    units = 5000

    if action == "sell":
        units = -units

    order = {
        "order": {
            "instrument": pair,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"

    response = requests.post(url, json=order, headers=headers)

    print("OANDA RESPONSE:", response.text)

    return jsonify(response.json())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
