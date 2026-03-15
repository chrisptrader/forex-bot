from flask import Flask, request, jsonify
import os
import requests
import time

app = Flask(__name__)

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

# risk settings
RISK_UNITS = int(os.getenv("OANDA_UNITS", "100"))

# trade cooldown protection
last_trade_time = 0
TRADE_COOLDOWN = 20


@app.route("/")
def home():
    return "Bot is running!"


def convert_pair(pair):
    mapping = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "XAUUSD": "XAU_USD",
        "US30": "US30_USD"
    }

    pair = pair.replace("/", "").upper()

    return mapping.get(pair)


def place_oanda_market_order(signal, pair):

    instrument = convert_pair(pair)

    if not instrument:
        return {"error": "unsupported pair"}

    units = RISK_UNITS if signal == "BUY" else -RISK_UNITS

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"

    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "order": {
            "instrument": instrument,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    response = requests.post(url, headers=headers, json=data)

    print("OANDA RESPONSE:", response.text)

    return response.json()


@app.route("/webhook", methods=["POST"])
def webhook():

    global last_trade_time

    data = request.json

    print("WEBHOOK RECEIVED:", data)

    signal = data.get("signal")
    pair = data.get("pair")

    if not signal or not pair:
        return jsonify({"error": "missing signal or pair"}), 400

    # cooldown protection
    if time.time() - last_trade_time < TRADE_COOLDOWN:
        return jsonify({"status": "cooldown active"})

    result = place_oanda_market_order(signal, pair)

    last_trade_time = time.time()

    return jsonify({
        "status": "order sent",
        "pair": pair,
        "signal": signal,
        "oanda": result
    })
    
    


