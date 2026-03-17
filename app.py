from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.environ.get("OANDA_API_KEY", "").strip()
ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
BASE_URL = os.environ.get("OANDA_BASE_URL", "https://api-fxpractice.oanda.com/v3").strip()
OANDA_UNITS = int(os.environ.get("OANDA_UNITS", "1000"))

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


def place_trade(signal: str, pair: str):
    units = OANDA_UNITS if signal == "BUY" else -OANDA_UNITS

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    payload = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    print("Placing trade payload:", payload)

    response = requests.post(
        url,
        json=payload,
        headers=oanda_headers(),
        timeout=20
    )

    try:
        result = response.json()
    except Exception:
        result = {"raw_text": response.text}

    print("Trade response:", result)
    return result, response.status_code


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Bot is running 🚀"


@app.route("/status")
def status():
    return jsonify({
        "bot": "running",
        "account_id_set": bool(ACCOUNT_ID),
        "api_key_set": bool(OANDA_API_KEY),
        "base_url": BASE_URL,
        "units": OANDA_UNITS
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True)

        print("Raw webhook JSON:", data)

        if not data:
            return jsonify({"error": "No JSON body received"}), 400

        signal = str(data.get("signal", "")).upper().strip()
        pair = normalize_pair(data.get("pair", ""))

        print("Parsed signal:", signal)
        print("Parsed pair:", pair)

        if signal not in ["BUY", "SELL"]:
            return jsonify({"error": "Invalid signal"}), 400

        if pair not in ["EUR_USD", "GBP_USD", "XAU_USD"]:
            return jsonify({"error": f"Pair not allowed: {pair}"}), 400

        if not OANDA_API_KEY or not ACCOUNT_ID:
            return jsonify({"error": "Missing OANDA credentials"}), 500

        print(f"🔥 SIGNAL RECEIVED: {signal} on {pair}")

        result, status_code = place_trade(signal, pair)
        return jsonify(result), status_code

    except Exception as e:
        print("Webhook execution error:", str(e))
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
