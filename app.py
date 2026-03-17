from flask import Flask, request, jsonify
import requests
import os
import threading
import time

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.environ.get("OANDA_API_KEY")
ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
BASE_URL = "https://api-fxpractice.oanda.com/v3"

RISK_PERCENT = 0.02 # 2% risk

# OANDA instrument format
PAIRS = ["EUR_USD", "GBP_USD", "XAU_USD"]

# Trailing stop settings
ENABLE_TRAILING = True

# =========================
# HELPER FUNCTIONS
# =========================
def normalize_pair(pair: str) -> str:
    pair = (pair or "").upper().strip()
    mapping = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "XAUUSD": "XAU_USD",
        "EUR_USD": "EUR_USD",
        "GBP_USD": "GBP_USD",
        "XAU_USD": "XAU_USD",
    }
    return mapping.get(pair, pair)


def get_account_balance():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/summary"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}

    response = requests.get(url, headers=headers, timeout=20)
    data = response.json()

    if response.status_code >= 300:
        raise Exception(f"OANDA account summary error: {data}")

    if "account" not in data:
        raise Exception(f"Missing 'account' in OANDA response: {data}")

    return float(data["account"]["balance"])


def calculate_units(pair):
    balance = get_account_balance()
    risk_amount = balance * RISK_PERCENT

    # basic test sizing
    if pair == "XAU_USD":
        return max(1, int(risk_amount * 2))
    else:
        return max(1, int(risk_amount * 1000))


def place_trade(signal, pair):
    pair = normalize_pair(pair)

    if pair not in PAIRS:
        return {"error": f"Pair not allowed: {pair}"}

    units = calculate_units(pair)

    if signal == "SELL":
        units = -units

    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, json=data, headers=headers, timeout=20)

    try:
        result = response.json()
    except Exception:
        result = {"raw_text": response.text}

    result["http_status"] = response.status_code
    return result


def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}

    response = requests.get(url, headers=headers, timeout=20)

    try:
        data = response.json()
    except Exception:
        return []

    return data.get("trades", [])


# =========================
# TRAILING STOP MANAGER
# =========================
def manage_trades():
    while True:
        try:
            trades = get_open_trades()
            headers = {
                "Authorization": f"Bearer {OANDA_API_KEY}",
                "Content-Type": "application/json"
            }

            for trade in trades:
                trade_id = trade.get("id")
                current_price = trade.get("currentPrice")
                unrealized_pl = trade.get("unrealizedPL")

                if not trade_id or current_price is None or unrealized_pl is None:
                    continue

                current_price = float(current_price)
                unrealized_pl = float(unrealized_pl)

                # very simple trailing logic for testing
                if ENABLE_TRAILING and unrealized_pl > 0:
                    data = {
                        "stopLoss": {
                            "price": str(current_price)
                        }
                    }

                    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
                    requests.put(url, json=data, headers=headers, timeout=20)

        except Exception as e:
            print("Error in trailing manager:", e)

        time.sleep(10)


# Start background thread
threading.Thread(target=manage_trades, daemon=True).start()

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Forex bot is running!"


@app.route("/status")
def status():
    return jsonify({
        "bot": "running",
        "account_id_set": bool(ACCOUNT_ID),
        "api_key_set": bool(OANDA_API_KEY),
        "base_url": BASE_URL,
        "risk_percent": RISK_PERCENT,
        "pairs": PAIRS,
        "trailing": ENABLE_TRAILING
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    signal = str(data.get("signal", "")).upper().strip()
    pair = normalize_pair(data.get("pair", ""))

    if signal not in ["BUY", "SELL"]:
        return jsonify({"error": "Invalid signal"}), 400

    if pair not in PAIRS:
        return jsonify({"error": f"Pair not allowed: {pair}"}), 400

    if not OANDA_API_KEY or not ACCOUNT_ID:
        return jsonify({"error": "Missing OANDA credentials"}), 500

    result = place_trade(signal, pair)
    return jsonify(result), 200


# =========================
# RUN APP (RENDER)
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
