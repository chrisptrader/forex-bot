from flask import Flask, request, jsonify
import os
import requests
import math

app = Flask(__name__)

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "2"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))


PAIR_CONFIG = {
    "EURUSD": {
        "instrument": "EUR_USD",
        "sl_distance": 0.0020, # 20 pips
        "tp_distance": 0.0040, # 40 pips
        "pip_value_per_unit": 0.0001
    },
    "GBPUSD": {
        "instrument": "GBP_USD",
        "sl_distance": 0.0025, # 25 pips
        "tp_distance": 0.0050, # 50 pips
        "pip_value_per_unit": 0.0001
    },
    "XAUUSD": {
        "instrument": "XAU_USD",
        "sl_distance": 10.0, # gold dollars
        "tp_distance": 20.0,
        "pip_value_per_unit": 1.0
    }
}


@app.route("/")
def home():
    return "Bot is running!"


def oanda_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }


def get_account_summary():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    response = requests.get(url, headers=oanda_headers(), timeout=15)
    response.raise_for_status()
    return response.json()


def get_open_trades():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    response = requests.get(url, headers=oanda_headers(), timeout=15)
    response.raise_for_status()
    return response.json().get("trades", [])


def pair_has_open_trade(instrument):
    trades = get_open_trades()
    for trade in trades:
        if trade.get("instrument") == instrument:
            return True
    return False


def total_open_trades():
    return len(get_open_trades())


def get_balance():
    summary = get_account_summary()
    account = summary.get("account", {})
    return float(account.get("balance", 0))


def calculate_units(pair, signal):
    config = PAIR_CONFIG[pair]
    balance = get_balance()
    risk_amount = balance * (RISK_PERCENT / 100.0)

    sl_distance = config["sl_distance"]
    pip_value_per_unit = config["pip_value_per_unit"]

    if sl_distance <= 0 or pip_value_per_unit <= 0:
        raise ValueError("Invalid pair risk config")

    raw_units = risk_amount / (sl_distance * pip_value_per_unit)

    units = max(1, math.floor(raw_units))

    if pair in ["EURUSD", "GBPUSD"]:
        units = min(units, 100000)
    elif pair == "XAUUSD":
        units = min(units, 100)

    if signal == "SELL":
        units = -units

    return units


def place_oanda_market_order(signal, pair):
    if pair not in PAIR_CONFIG:
        return {"error": f"unsupported pair: {pair}"}, 400

    config = PAIR_CONFIG[pair]
    instrument = config["instrument"]

    if total_open_trades() >= MAX_OPEN_TRADES:
        return {"error": "max open trades reached"}, 400

    if pair_has_open_trade(instrument):
        return {"error": f"trade already open for {pair}"}, 400

    units = calculate_units(pair, signal)

    order_payload = {
        "order": {
            "instrument": instrument,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "distance": str(config["sl_distance"])
            },
            "takeProfitOnFill": {
                "distance": str(config["tp_distance"])
            }
        }
    }

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    response = requests.post(url, headers=oanda_headers(), json=order_payload, timeout=20)

    print("ORDER PAYLOAD:", order_payload)
    print("OANDA RESPONSE:", response.text)

    try:
        data = response.json()
    except Exception:
        data = {"raw_response": response.text}

    return data, response.status_code


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("WEBHOOK RECEIVED:", data)

    if not data:
        return jsonify({"error": "missing json body"}), 400

    signal = str(data.get("signal", "")).upper().strip()
    pair = str(data.get("pair", "")).upper().replace("/", "").strip()

    if signal not in ["BUY", "SELL"]:
        return jsonify({"error": "signal must be BUY or SELL"}), 400

    if pair not in PAIR_CONFIG:
        return jsonify({"error": f"unsupported pair: {pair}"}), 400

    try:
        result, status_code = place_oanda_market_order(signal, pair)
        return jsonify({
            "status": "processed",
            "signal": signal,
            "pair": pair,
            "result": result
        }), status_code
    except Exception as e:
        print("BOT ERROR:", str(e))
        return jsonify({"error": str(e)}), 500
