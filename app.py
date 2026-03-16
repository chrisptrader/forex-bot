from flask import Flask, request, jsonify
import os
import requests

app = Flask(__name__)

# =========================
# ENV VARIABLES
# =========================

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))

COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "4"))
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "11"))
SESSION_TIMEZONE = os.getenv("SESSION_TIMEZONE", "America/New_York")

# =========================
# HOME ROUTE
# =========================

@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200


# =========================
# STATUS ROUTE
# =========================

@app.route("/status", methods=["GET"])
def status():
    try:

        account_info = get_account()

        return jsonify({
            "bot": "running",
            "account": account_info,
            "risk_percent": RISK_PERCENT,
            "max_open_trades": MAX_OPEN_TRADES,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "max_daily_loss_percent": MAX_DAILY_LOSS_PERCENT,
            "cooldown_minutes": COOLDOWN_MINUTES,
            "session_start_hour": SESSION_START_HOUR,
            "session_end_hour": SESSION_END_HOUR,
            "session_timezone": SESSION_TIMEZONE
        })

    except Exception as e:
        return jsonify({
            "bot": "running",
            "error": str(e)
        })


# =========================
# OANDA ACCOUNT
# =========================

def get_account():

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"

    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}"
    }

    r = requests.get(url, headers=headers)

    data = r.json()

    account = data["account"]

    return {
        "balance": account["balance"],
        "NAV": account["NAV"],
        "currency": account["currency"]
    }


# =========================
# WEBHOOK ROUTE
# =========================

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.json

    if not data:
        return {"error": "No JSON"}, 400

    pair = data.get("pair")
    signal = data.get("signal")

    if not pair or not signal:
        return {"error": "Missing pair or signal"}, 400

    print("Signal received:", pair, signal)

    try:

        order = place_trade(pair, signal)

        return {
            "status": "order_sent",
            "pair": pair,
            "signal": signal,
            "order": order
        }

    except Exception as e:

        return {"error": str(e)}


# =========================
# PLACE TRADE
# =========================

def place_trade(pair, signal):

    instrument = pair.replace("/", "_")

    units = 1000

    if signal.upper() == "SELL":
        units = -units

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"

    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "order": {
            "instrument": instrument,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    r = requests.post(url, headers=headers, json=payload)

    return r.json()


# =========================
# RUN APP
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
