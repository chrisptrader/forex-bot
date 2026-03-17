from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# =========================
# 🔑 PUT YOUR REAL INFO HERE
# =========================
OANDA_API_KEY = "98969b4679d01a139e86d66ee8694bef-6f46ee09cb98d79db97096b393622766"
ACCOUNT_ID = "101-001-37221732-001"

BASE_URL = "https://api-fxpractice.oanda.com/v3"
UNITS = 1000 # safe size

# =========================
# HELPER
# =========================
def place_trade(signal, pair):
    units = UNITS if signal == "BUY" else -UNITS

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"

    data = {
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

    print("🚀 Sending trade:", data)

    response = requests.post(url, json=data, headers=headers)

    try:
        result = response.json()
    except:
        result = response.text

    print("💰 Trade response:", result)

    return result

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Bot is LIVE 🚀"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    print("Raw webhook JSON:", data)

    signal = data.get("signal")
    pair = data.get("pair")

    # Fix format
    if pair == "EURUSD":
        pair = "EUR_USD"
    if pair == "GBPUSD":
        pair = "GBP_USD"
    if pair == "XAUUSD":
        pair = "XAU_USD"

    print(f"🔥 SIGNAL RECEIVED: {signal} on {pair}")

    result = place_trade(signal, pair)

    return jsonify(result)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
