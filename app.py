from flask import Flask, request, jsonify
import os
import requests

app = Flask(__name__)

OANDA_API_KEY = os.environ.get("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
OANDA_ENV = os.environ.get("OANDA_ENV", "practice")

RISK_PERCENT = float(os.environ.get("RISK_PERCENT", 1.0))
STOP_LOSS_PIPS = float(os.environ.get("STOP_LOSS_PIPS", 10))
FALLBACK_UNITS = int(os.environ.get("FALLBACK_UNITS", 5000))

BASE_URL = "https://api-fxpractice.oanda.com" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com"

@app.route("/")
def home():
    return "Bot Running V35 PRO 🚀"

def get_account_balance():
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}"
    }
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    response = requests.get(url, headers=headers)
    data = response.json()
    return float(data["account"]["balance"])

def calculate_units(pair):
    try:
        balance = get_account_balance()

        risk_amount = balance * (RISK_PERCENT / 100)

        # pip value estimate (simple but effective)
        pip_value_per_1000 = 0.1 if "JPY" in pair else 0.1

        units = int((risk_amount / (STOP_LOSS_PIPS * pip_value_per_1000)) * 1000)

        if units <= 0:
            return FALLBACK_UNITS

        return units

    except:
        return FALLBACK_UNITS

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

    units = calculate_units(pair)

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

    print("UNITS USED:", units)
    print("OANDA RESPONSE:", response.text)

    return jsonify(response.json())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
