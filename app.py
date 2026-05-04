from flask import Flask, request, jsonify
import os
import time
import requests

app = Flask(__name__)

# =========================
# ENV
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")

BASE_URL = "https://api-fxpractice.oanda.com" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com"

ALLOW_BUY = os.getenv("ALLOW_BUY", "true").lower() == "true"
ALLOW_SELL = os.getenv("ALLOW_SELL", "true").lower() == "true"
ALLOW_MULTIPAIR = os.getenv("ALLOW_MULTIPAIR", "true").lower() == "true"
ALLOWED_PAIRS = [p.strip() for p in os.getenv("ALLOWED_PAIRS", "EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CHF").split(",")]

USE_RISK_PERCENT = os.getenv("USE_RISK_PERCENT", "false").lower() == "true"
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1.0"))
FIXED_UNITS = int(float(os.getenv("FIXED_UNITS", "15000")))

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "2"))
ONE_TRADE_PER_PAIR = os.getenv("ONE_TRADE_PER_PAIR", "true").lower() == "true"
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "300"))
DUPLICATE_SECONDS = int(os.getenv("DUPLICATE_SECONDS", "5"))

STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "60"))

DEBUG_BLOCK_REASONS = os.getenv("DEBUG_BLOCK_REASONS", "true").lower() == "true"

last_signal_time = {}
last_trade_time = {}

# =========================
# HELPERS
# =========================
def log(msg):
    print(msg, flush=True)

def block(pair, action, reason):
    if DEBUG_BLOCK_REASONS:
        log(f"❌ BLOCKED | {pair} | {action} | {reason}")
    return jsonify({"status": "blocked", "reason": reason}), 200

def pip_size(pair):
    return 0.01 if pair.endswith("_JPY") else 0.0001

def headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

def get_price(pair):
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=headers(), params={"instruments": pair}, timeout=10)
    r.raise_for_status()
    data = r.json()["prices"][0]
    bid = float(data["bids"][0]["price"])
    ask = float(data["asks"][0]["price"])
    return bid, ask

def get_open_positions():
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openPositions"
    r = requests.get(url, headers=headers(), timeout=10)
    r.raise_for_status()
    return r.json().get("positions", [])

def has_open_pair(pair):
    for pos in get_open_positions():
        if pos.get("instrument") == pair:
            long_units = float(pos.get("long", {}).get("units", 0))
            short_units = float(pos.get("short", {}).get("units", 0))
            if long_units != 0 or short_units != 0:
                return True
    return False

def open_trade_count():
    count = 0
    for pos in get_open_positions():
        long_units = float(pos.get("long", {}).get("units", 0))
        short_units = float(pos.get("short", {}).get("units", 0))
        if long_units != 0 or short_units != 0:
            count += 1
    return count

def get_account_balance():
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    r = requests.get(url, headers=headers(), timeout=10)
    r.raise_for_status()
    return float(r.json()["account"]["balance"])

def calc_units(pair):
    if not USE_RISK_PERCENT:
        return FIXED_UNITS

    balance = get_account_balance()
    risk_dollars = balance * (RISK_PERCENT / 100)

    # Simple estimate. Safer than oversizing. JPY still handled.
    if pair.endswith("_JPY"):
        units = int((risk_dollars / STOP_LOSS_PIPS) * 100)
    else:
        units = int((risk_dollars / STOP_LOSS_PIPS) * 10000)

    return max(1000, units)

def place_trade(pair, action):
    bid, ask = get_price(pair)
    pip = pip_size(pair)

    entry = ask if action == "buy" else bid

    if action == "buy":
        units = abs(calc_units(pair))
        sl = entry - (STOP_LOSS_PIPS * pip)
        tp = entry + (TAKE_PROFIT_PIPS * pip)
    else:
        units = -abs(calc_units(pair))
        sl = entry + (STOP_LOSS_PIPS * pip)
        tp = entry - (TAKE_PROFIT_PIPS * pip)

    precision = 3 if pair.endswith("_JPY") else 5

    order = {
        "order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": str(round(sl, precision))
            },
            "takeProfitOnFill": {
                "price": str(round(tp, precision))
            }
        }
    }

    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=headers(), json=order, timeout=10)

    log(f"ORDER RESPONSE: {r.text}")

    if r.status_code not in [200, 201]:
        log(f"❌ TRADE FAILED | {pair} | {action}")
        return False, r.text

    log(f"✅ TRADE EXECUTED | {pair} | {action} | units={units}")
    return True, r.json()

# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return "V51 running"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)

        pair = str(data.get("pair", "")).upper().strip()
        action = str(data.get("action", "")).lower().strip()
        passphrase = str(data.get("passphrase", ""))

        log(f"📩 SIGNAL | {pair} | {action}")

        if passphrase != WEBHOOK_PASSPHRASE:
            return block(pair, action, "bad passphrase")

        if pair not in ALLOWED_PAIRS:
            return block(pair, action, "pair not allowed")

        if action not in ["buy", "sell"]:
            return block(pair, action, "bad action")

        if action == "buy" and not ALLOW_BUY:
            return block(pair, action, "buy disabled")

        if action == "sell" and not ALLOW_SELL:
            return block(pair, action, "sell disabled")

        now = time.time()
        signal_key = f"{pair}_{action}"

        # duplicate protection
        if signal_key in last_signal_time:
            if now - last_signal_time[signal_key] < DUPLICATE_SECONDS:
                return block(pair, action, "duplicate signal")

        last_signal_time[signal_key] = now

        # cooldown protection
        if pair in last_trade_time:
            if now - last_trade_time[pair] < MIN_SECONDS_BETWEEN_TRADES:
                return block(pair, action, "cooldown active")

        # open trade protection
        current_open = open_trade_count()

        if current_open >= MAX_OPEN_TRADES:
            return block(pair, action, f"max open trades reached {current_open}/{MAX_OPEN_TRADES}")

        if ONE_TRADE_PER_PAIR and has_open_pair(pair):
            return block(pair, action, "already open on this pair")

        if not ALLOW_MULTIPAIR and current_open > 0:
            return block(pair, action, "multipair disabled and trade already open")

        success, response = place_trade(pair, action)

        if success:
            last_trade_time[pair] = now
            last_trade_time[signal_key] = now
            return jsonify({"status": "executed", "pair": pair, "action": action}), 200

        return jsonify({"status": "failed", "response": str(response)}), 200

    except Exception as e:
        log(f"❌ ERROR: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# =========================
# START
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
