from flask import Flask, request, jsonify
import os
import requests
import math
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================
# ENV
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com").strip()

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "o.5"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))
SESSION_TIMEZONE = os.getenv("SESSION_TIMEZONE", "America/New_York")

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

# =========================
# IN-MEMORY STATE
# =========================
STATE = {
    "daily_date": None,
    "daily_start_nav": None,
    "trades_today": 0,
    "last_signal_key": None,
    "last_signal_time": None
}


# =========================
# HELPERS
# =========================
def now_local():
    return datetime.now(ZoneInfo(SESSION_TIMEZONE))


def reset_daily_state_if_needed():
    today = now_local().date().isoformat()
    if STATE["daily_date"] != today:
        STATE["daily_date"] = today
        STATE["daily_start_nav"] = None
        STATE["trades_today"] = 0


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
    account = data.get("account")

    if not account:
        raise Exception(f"OANDA account error: {data}")

    nav = float(account["NAV"])
    return nav


def get_open_trades():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=oanda_headers(), timeout=20)

    try:
        data = r.json()
    except Exception:
        return []

    return data.get("trades", [])


def total_open_trades():
    return len(get_open_trades())


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


def duplicate_signal(signal, pair):
    key = f"{signal}:{pair}"
    if STATE["last_signal_key"] != key or STATE["last_signal_time"] is None:
        return False

    elapsed = (now_local() - STATE["last_signal_time"]).total_seconds() / 60.0
    return elapsed < COOLDOWN_MINUTES


def remember_signal(signal, pair):
    STATE["last_signal_key"] = f"{signal}:{pair}"
    STATE["last_signal_time"] = now_local()


def daily_loss_hit():
    reset_daily_state_if_needed()

    nav = get_account_nav()

    if STATE["daily_start_nav"] is None:
        STATE["daily_start_nav"] = nav
        return False, 0.0

    start_nav = STATE["daily_start_nav"]
    if start_nav <= 0:
        return False, 0.0

    drawdown_percent = ((start_nav - nav) / start_nav) * 100.0
    return drawdown_percent >= MAX_DAILY_LOSS_PERCENT, drawdown_percent


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Forex bot is running!", 200


@app.route("/status", methods=["GET"])
def status():
    reset_daily_state_if_needed()

    account_summary = get_account_summary()
    open_trades = get_open_trades()

    return jsonify({
        "bot": "running",
        "account_id_set": bool(OANDA_ACCOUNT_ID),
        "api_key_set": bool(OANDA_API_KEY),
        "base_url": OANDA_BASE_URL,
        "risk_percent": RISK_PERCENT,
        "max_open_trades": MAX_OPEN_TRADES,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "max_daily_loss_percent": MAX_DAILY_LOSS_PERCENT,
        "cooldown_minutes": COOLDOWN_MINUTES,
        "supported_pairs": list(PAIR_MAP.keys()),
        "trades_today": STATE["trades_today"],
        "open_trades_count": len(open_trades),
        "daily_start_nav": STATE["daily_start_nav"],
        "account_summary": account_summary
    }), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    reset_daily_state_if_needed()

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

    # safety 1: duplicate/cooldown protection
    if duplicate_signal(signal, pair):
        return jsonify({
            "status": "blocked",
            "reason": "Duplicate signal / cooldown active"
        }), 200

    # safety 2: max open trades
    open_count = total_open_trades()
    if open_count >= MAX_OPEN_TRADES:
        return jsonify({
            "status": "blocked",
            "reason": "Max open trades reached",
            "open_trades_count": open_count
        }), 200

    # safety 3: max trades per day
    if STATE["trades_today"] >= MAX_TRADES_PER_DAY:
        return jsonify({
            "status": "blocked",
            "reason": "Max trades per day reached",
            "trades_today": STATE["trades_today"]
        }), 200

    # safety 4: daily loss stop
    loss_hit, drawdown_percent = daily_loss_hit()
    if loss_hit:
        return jsonify({
            "status": "blocked",
            "reason": "Daily loss limit hit",
            "drawdown_percent": round(drawdown_percent, 2)
        }), 200

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

    if r.status_code < 300:
        STATE["trades_today"] += 1
        remember_signal(signal, pair)

    return jsonify({
        "status_code": r.status_code,
        "pair": pair,
        "signal": signal,
        "risk_percent": RISK_PERCENT,
        "units": units,
        "stop_loss_distance": sl_distance,
        "take_profit_distance": tp_distance,
        "trades_today": STATE["trades_today"],
        "result": result
    }), r.status_code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
