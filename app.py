from flask import Flask, request, jsonify
import os
import requests

app = Flask(__name__)

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")
OANDA_UNITS = int(os.getenv("OANDA_UNITS", "100"))

@app.route("/")
def home():
    return "Bot is running!"

def place_oanda_market_order(signal: str, pair: str):
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        raise ValueError("Missing OANDA env vars")

    instrument = pair.replace("/", "_")
    if instrument == "EURUSD":
        instrument = "EUR_USD"

    units = OANDA_UNITS if signal == "BUY" else -OANDA_UNITS

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT"
        }
    }

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    return r.status_code, r.text

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)

    signal = str(data.get("signal", "")).upper()
    pair = str(data.get("pair", "EUR_USD"))
    price = data.get("price")
    atr = data.get("atr")

    print(f"ALERT RECEIVED: {signal} {pair} Price: {price} ATR: {atr}")

    if signal not in {"BUY", "SELL"}:
        return jsonify({"error": "signal must be BUY or SELL"}), 400

    try:
        status_code, response_text = place_oanda_market_order(signal, pair)
        print(f"OANDA RESPONSE {status_code}: {response_text}")
        return jsonify({"status": "received", "oanda_status": status_code}), 200
    except Exception as e:
        print(f"OANDA ERROR: {e}")
        return jsonify({"error": str(e)}), 500

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
