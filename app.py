from flask import Flask, request, jsonify
import os
import requests

app = Flask(__name__)

OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com").strip()
OANDA_UNITS = int(os.getenv("OANDA_UNITS", "1000"))

PAIR_MAP = {
    "EURUSD": "EUR_USD",
    "GBPUSD": "GBP_USD",
    "XAUUSD": "XAU_USD"
}

def oanda_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

def get_account_summary():
    if not OANDA_ACCOUNT_ID or not OANDA_API_KEY:
        return {"error": "Missing API credentials"}

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    r = requests.get(url, headers=oanda_headers())

    try:
        return r.json()
    except:
        return {"error": "Could not parse response"}

@app.route("/")
def home():
    return "Forex bot is running!"

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "bot": "running",
        "account_id_set": bool(OANDA_ACCOUNT_ID),
        "api_key_set": bool(OANDA_API_KEY),
        "base_url": OANDA_BASE_URL,
        "units": OANDA_UNITS,
        "supported_pairs": list(PAIR_MAP.keys())
    }), 200

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    signal = str(data.get("signal", "")).upper().strip()
    pair = str(data.get("pair", "")).upper().replace("/", "").strip()

    if signal not in ["BUY", "SELL"]:
        return jsonify({"error": "Signal must be BUY or SELL"}), 400

    if pair not in PAIR_MAP:
        return jsonify({"error": "Unsupported pair"}), 400

    instrument = PAIR_MAP[pair]
    units = OANDA_UNITS if signal == "BUY" else -OANDA_UNITS

    payload = {
        "order": {
            "units": str(units),
            "instrument": instrument,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"

    r = requests.post(url, headers=oanda_headers(), json=payload)

    try:
        result = r.json()
    except:
        result = {"raw_response": r.text}

    return jsonify({
        "status_code": r.status_code,
        "pair": pair,
        "signal": signal,
        "units": units,
        "result": result
    }), r.status_code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
