
from flask import Flask, request, jsonify
import requests
import os
import threading
import time

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.environ.get("OANDA_API_KEY", "").strip()
ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
BASE_URL = "https://api-fxpractice.oanda.com/v3"

# Fixed unit size from Render ENV
OANDA_UNITS = int(os.environ.get("OANDA_UNITS", "10000"))

# These are the OANDA-formatted pairs your bot accepts
PAIRS = ["EUR_USD", "GBP_USD", "XAU_USD"]

ENABLE_TRAILING = True

# =========================
# HELPERS
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


def oanda_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }


def get_account_summary():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/summary"
    response = requests.get(url, headers=oanda_headers(), timeout=20)

    try:
        data = response.json()
    except Exception:
        raise Exception(f"Could not decode OANDA summary response: {response.text}")

    print("Account summary response:", data)

    if response.status_code >= 300:
        raise Exception(f"OANDA account summary error: {data}")

    if "account" not in data:
        raise Exception(f"Missing 'account' in OANDA response: {data}")

    return data["account"]


def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    response = requests.get(url, headers=oanda_headers(), timeout=20)

    try:
        data = response.json()
    except Exception:
        print("Could not decode open trades response:", response.text)
        return []

    if response.status_code >= 300:
        print("Open trades error:", data)
        return []

    return data.get("trades", [])


def place_trade(signal: str, pair: str):
    pair = normalize_pair(pair)

    if pair not in PAIRS:
        return {"error": f"Pair not allowed: {pair}"}

    units = OANDA_UNITS
    if signal == "SELL":
        units = -units

    order_payload = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"

    print("Placing trade payload:", order_payload)

    response = requests.post(
        url,
        json=order_payload,
        headers=oanda_headers(),
        timeout=20
    )

    try:
        result = response.json()
    except Exception:
        result = {"raw_text": response.text}

    result["http_status"] = response.status_code
    print("OANDA order response:", result)

    return result


# =========================
# TRAILING MANAGER
# =========================
def manage_trades():
    while True:
        try:
            trades = get_open_trades()

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
                    response = requests.put(
                        url,
                        json=data,
                        headers=oanda_headers(),
                        timeout=20
                    )

                    try:
                        trailing_result = response.json()
                    except Exception:
                        trailing_result = {"raw_text": response.text}

                    print("Trailing update result:", trailing_result)

        except Exception as e:
            print("Error in trailing manager:", e)

        time.sleep(10)


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
        "pairs": PAIRS,
        "units": OANDA_UNITS,
        "trailing": ENABLE_TRAILING
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    print("Incoming data:", data)

    signal = str(data.get("signal", "")).upper().strip()
    pair = normalize_pair(data.get("pair", ""))

    print("Signal:", signal)
    print("Pair:", pair)

    if signal not in ["BUY", "SELL"]:
        return jsonify({"error": "Invalid signal"}), 400

    if pair not in PAIRS:
        return jsonify({"error": f"Pair not allowed: {pair}"}), 400

    if not OANDA_API_KEY or not ACCOUNT_ID:
        return jsonify({"error": "Missing OANDA credentials"}), 500

    try:
        result = place_trade(signal, pair)
        print("Trade result:", result)
        return jsonify(result), 200
    except Exception as e:
        print("Webhook execution error:", str(e))
        return jsonify({"error": str(e)}), 500


# =========================
# RUN FOR RENDER
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
