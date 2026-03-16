from flask import Flask, request, jsonify
import os
import requests
import math

app = Flask(__name__)

OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com").strip()
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "2"))

PAIR_MAP = {
    "EURUSD": {
        "instrument": "EUR_USD",
        "sl_distance": 0.0020,
        "tp_distance": 0.0040,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000
    },
    "GBPUSD": {
        "instrument": "GBP_USD",
        "sl_distance": 0.0025,
        "tp_distance": 0.0050,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000
    },
    "XAUUSD": {
        "instrument": "XAU_USD",
        "sl_distance": 10.0,
        "tp_distance": 20.0,
        "pip_value_per_unit": 1.0,
        "max_units": 100
    }
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
    r = requests.get(url, headers=oanda_headers(), timeout=20)

    try:
        return r.json()
    except Exception:
        return {"error": "Could not parse response"}


def get_account_nav():
    data = get_account_summary()

    if "account" not in data:
        raise Exception(f"OANDA account error: {data}")

    nav = float(data["account"]["NAV"])
    return nav


def calculate_units(pair, signal):
    pair_info = PAIR_MAP[pair]

    nav = get_account_nav()
    risk_amount = nav * (RISK_PERCENT / 100.0)

    sl_distance = pair_info["sl_distance"]
    pip_value_per_unit = pair_info["pip_value_per_unit"]

    raw_units = risk_amount / (sl_distance * pip_value_per_unit)
    units = math.floor(raw_units)

    if units < 1:
        units = 1

    if units > pair_info["max_units"]:
        units = pair_info["max_units"]

    if signal == "SELL":
        units = -units

    return units


@app.route("/", methods=["GET"])
def home():
    return "Forex bot is running!", 200


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "bot": "running",
        "account_id_set": bool(OANDA_ACCOUNT_ID),
        "api_key_set": bool(OANDA_API_KEY),
        "base_url": OANDA_BASE_URL,
        "risk_percent": RISK_PERCENT,
        "supported_pairs": list(PAIR_MAP.keys()),
        "pair_settings": {
            pair: {
                "instrument": info["instrument"],
                "sl_distance": info["sl_distance"],
                "tp_distance": info["tp_distance"],
                "max_units": info["max_units"]
            }
            for pair, info in PAIR_MAP.items()
        }
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
        return jsonify({
            "error": "Unsupported pair",
            "supported_pairs": list(PAIR_MAP.keys())
        }), 400

    if not OANDA_ACCOUNT_ID or not OANDA_API_KEY:
        return jsonify({"error": "Missing OANDA credentials"}), 400

    pair_info = PAIR_MAP[pair]
    instrument = pair_info["instrument"]
    sl_distance = pair_info["sl_distance"]
    tp_distance = pair_info["tp_distance"]

    try:
        units = calculate_units(pair, signal)
    except Exception as e:
        return jsonify({"error": f"Position sizing failed: {str(e)}"}), 500

    payload = {
        "order": {
            "units": str(units),
            "instrument": instrument,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "distance": str(sl_distance)
            },
            "takeProfitOnFill": {
                "distance": str(tp_distance)
            }
        }
    }

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=oanda_headers(), json=payload, timeout=20)

    try:
        result = r.json()
    except Exception:
        result = {"raw_response": r.text}

    return jsonify({
        "status_code": r.status_code,
        "pair": pair,
        "signal": signal,
        "risk_percent": RISK_PERCENT,
        "units": units,
        "stop_loss_distance": sl_distance,
        "take_profit_distance": tp_distance,
        "result": result
    }), r.status_code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

