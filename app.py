from flask import Flask, request, jsonify
import os
import requests
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================
# ENV / SETTINGS
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# Session control
ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").lower() == "true"
SESSION_TIMEZONE = os.getenv("SESSION_TIMEZONE", "America/New_York")
SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "3")) # 3 AM NY
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "17")) # 5 PM NY

# Logging / behavior
ALLOW_REVERSE_SIGNAL_CLOSE = os.getenv("ALLOW_REVERSE_SIGNAL_CLOSE", "false").lower() == "true"

# Pair configuration
PAIR_CONFIG = {
    "EURUSD": {
        "instrument": "EUR_USD",
        "sl_distance": 0.0020,
        "tp_distance": 0.0040,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00025
    },
    "GBPUSD": {
        "instrument": "GBP_USD",
        "sl_distance": 0.0025,
        "tp_distance": 0.0050,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00035
    },
    "XAUUSD": {
        "instrument": "XAU_USD",
        "sl_distance": 10.0,
        "tp_distance": 20.0,
        "pip_value_per_unit": 1.0,
        "max_units": 100,
        "max_spread": 0.60
    }
}

# In-memory cooldown storage
last_signal_times = {}


# =========================
# BASIC ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
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
        result, status_code = process_signal(signal, pair)
        return jsonify(result), status_code
    except Exception as e:
        print("BOT ERROR:", str(e))
        return jsonify({"error": str(e)}), 500


# =========================
# HELPERS
# =========================
def oanda_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }


def now_local():
    return datetime.now(ZoneInfo(SESSION_TIMEZONE))


def in_allowed_session():
    if not ENABLE_SESSION_FILTER:
        return True

    hour = now_local().hour
    return SESSION_START_HOUR <= hour < SESSION_END_HOUR


def get_account_summary():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    response = requests.get(url, headers=oanda_headers(), timeout=15)
    response.raise_for_status()
    return response.json()


def get_balance():
    summary = get_account_summary()
    account = summary.get("account", {})
    return float(account.get("balance", 0))


def get_open_trades():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    response = requests.get(url, headers=oanda_headers(), timeout=15)
    response.raise_for_status()
    return response.json().get("trades", [])


def total_open_trades():
    return len(get_open_trades())


def find_open_trade_for_instrument(instrument):
    trades = get_open_trades()
    for trade in trades:
        if trade.get("instrument") == instrument:
            return trade
    return None


def pair_has_open_trade(instrument):
    return find_open_trade_for_instrument(instrument) is not None


def signal_matches_open_side(open_trade, signal):
    """
    OANDA units > 0 means long, < 0 means short
    """
    current_units = float(open_trade.get("currentUnits", "0"))
    if signal == "BUY" and current_units > 0:
        return True
    if signal == "SELL" and current_units < 0:
        return True
    return False


def close_trade(trade_id):
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close"
    response = requests.put(url, headers=oanda_headers(), json={}, timeout=20)
    print("CLOSE TRADE RESPONSE:", response.text)
    try:
        data = response.json()
    except Exception:
        data = {"raw_response": response.text}
    return data, response.status_code


def calculate_units(pair, signal):
    cfg = PAIR_CONFIG[pair]
    balance = get_balance()
    risk_amount = balance * (RISK_PERCENT / 100.0)

    sl_distance = cfg["sl_distance"]
    pip_value_per_unit = cfg["pip_value_per_unit"]

    raw_units = risk_amount / (sl_distance * pip_value_per_unit)
    units = max(1, math.floor(raw_units))
    units = min(units, cfg["max_units"])

    if signal == "SELL":
        units = -units

    return units


def get_current_spread(pair):
    cfg = PAIR_CONFIG[pair]
    instrument = cfg["instrument"]

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    params = {"instruments": instrument}
    response = requests.get(url, headers=oanda_headers(), params=params, timeout=15)
    response.raise_for_status()

    prices = response.json().get("prices", [])
    if not prices:
        raise Exception(f"no pricing returned for {pair}")

    price = prices[0]
    bids = price.get("bids", [])
    asks = price.get("asks", [])

    if not bids or not asks:
        raise Exception(f"missing bid/ask for {pair}")

    bid = float(bids[0]["price"])
    ask = float(asks[0]["price"])
    spread = ask - bid

    return spread, bid, ask


def spread_ok(pair):
    spread, bid, ask = get_current_spread(pair)
    max_spread = PAIR_CONFIG[pair]["max_spread"]
    return spread <= max_spread, {
        "spread": spread,
        "max_spread": max_spread,
        "bid": bid,
        "ask": ask
    }


def cooldown_active(pair, signal):
    key = f"{pair}:{signal}"
    last_time = last_signal_times.get(key)
    if not last_time:
        return False, 0

    now_utc = datetime.now(timezone.utc)
    elapsed_minutes = (now_utc - last_time).total_seconds() / 60.0
    remaining = max(0, COOLDOWN_MINUTES - elapsed_minutes)

    return elapsed_minutes < COOLDOWN_MINUTES, round(remaining, 2)


def set_cooldown(pair, signal):
    key = f"{pair}:{signal}"
    last_signal_times[key] = datetime.now(timezone.utc)


def place_oanda_market_order(signal, pair):
    cfg = PAIR_CONFIG[pair]
    instrument = cfg["instrument"]
    units = calculate_units(pair, signal)

    payload = {
        "order": {
            "instrument": instrument,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "distance": str(cfg["sl_distance"])
            },
            "takeProfitOnFill": {
                "distance": str(cfg["tp_distance"])
            }
        }
    }

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    response = requests.post(url, headers=oanda_headers(), json=payload, timeout=20)

    print("ORDER PAYLOAD:", payload)
    print("OANDA RESPONSE:", response.text)

    try:
        data = response.json()
    except Exception:
        data = {"raw_response": response.text}

    return data, response.status_code


# =========================
# MAIN SIGNAL PROCESSING
# =========================
def process_signal(signal, pair):
    cfg = PAIR_CONFIG[pair]
    instrument = cfg["instrument"]

    # Safety checks
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        return {
            "status": "rejected",
            "reason": "missing OANDA credentials"
        }, 400

    if not in_allowed_session():
        return {
            "status": "skipped",
            "reason": "outside allowed session",
            "timezone": SESSION_TIMEZONE,
            "current_hour": now_local().hour
        }, 200

    active, remaining = cooldown_active(pair, signal)
    if active:
        return {
            "status": "skipped",
            "reason": "cooldown active",
            "pair": pair,
            "signal": signal,
            "remaining_minutes": remaining
        }, 200

    spread_pass, spread_info = spread_ok(pair)
    if not spread_pass:
        return {
            "status": "skipped",
            "reason": "spread too wide",
            "pair": pair,
            "signal": signal,
            "spread_info": spread_info
        }, 200

    open_trade = find_open_trade_for_instrument(instrument)

    if open_trade:
        if signal_matches_open_side(open_trade, signal):
            return {
                "status": "skipped",
                "reason": "same-direction trade already open",
                "pair": pair,
                "signal": signal
            }, 200

        if ALLOW_REVERSE_SIGNAL_CLOSE:
            close_result, close_code = close_trade(open_trade["id"])
            if close_code >= 300:
                return {
                    "status": "rejected",
                    "reason": "failed to close opposite trade",
                    "pair": pair,
                    "signal": signal,
                    "close_result": close_result
                }, 400
        else:
            return {
                "status": "skipped",
                "reason": "opposite trade open and reverse close disabled",
                "pair": pair,
                "signal": signal
            }, 200

    if total_open_trades() >= MAX_OPEN_TRADES:
        return {
            "status": "skipped",
            "reason": "max open trades reached",
            "max_open_trades": MAX_OPEN_TRADES
        }, 200

    result, status_code = place_oanda_market_order(signal, pair)

    if 200 <= status_code < 300:
        set_cooldown(pair, signal)

    return {
        "status": "processed" if 200 <= status_code < 300 else "rejected",
        "pair": pair,
        "signal": signal,
        "spread_info": spread_info,
        "result": result
    }, status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

Chris Pena <insulfion@gmail.com>
8:13 PM (0 minutes ago)
to me

d3a5f2c43094302b7abdae263dee13a9-bf1766bb45dd106b108ccb6dc139e522

On Sun, Mar 15, 2026, 7:49 PM Chris Pena <insulfion@gmail.com> wrote:
from flask import Flask, request, jsonify
import os
import requests
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================
# ENV / SETTINGS
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# Session control
ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").lower() == "true"
SESSION_TIMEZONE = os.getenv("SESSION_TIMEZONE", "America/New_York")
SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "3")) # 3 AM NY
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "17")) # 5 PM NY

# Logging / behavior
ALLOW_REVERSE_SIGNAL_CLOSE = os.getenv("ALLOW_REVERSE_SIGNAL_CLOSE", "false").lower() == "true"

# Pair configuration
PAIR_CONFIG = {
    "EURUSD": {
        "instrument": "EUR_USD",
        "sl_distance": 0.0020,
        "tp_distance": 0.0040,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00025
    },
    "GBPUSD": {
        "instrument": "GBP_USD",
        "sl_distance": 0.0025,
        "tp_distance": 0.0050,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00035
    },
    "XAUUSD": {
        "instrument": "XAU_USD",
        "sl_distance": 10.0,
        "tp_distance": 20.0,
        "pip_value_per_unit": 1.0,
        "max_units": 100,
        "max_spread": 0.60
    }
}

# In-memory cooldown storage
last_signal_times = {}


# =========================
# BASIC ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
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
        result, status_code = process_signal(signal, pair)
        return jsonify(result), status_code
    except Exception as e:
        print("BOT ERROR:", str(e))
        return jsonify({"error": str(e)}), 500


# =========================
# HELPERS
# =========================
def oanda_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }


def now_local():
    return datetime.now(ZoneInfo(SESSION_TIMEZONE))


def in_allowed_session():
    if not ENABLE_SESSION_FILTER:
        return True

    hour = now_local().hour
    return SESSION_START_HOUR <= hour < SESSION_END_HOUR


def get_account_summary():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    response = requests.get(url, headers=oanda_headers(), timeout=15)
    response.raise_for_status()
    return response.json()


def get_balance():
    summary = get_account_summary()
    account = summary.get("account", {})
    return float(account.get("balance", 0))


def get_open_trades():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    response = requests.get(url, headers=oanda_headers(), timeout=15)
    response.raise_for_status()
    return response.json().get("trades", [])


def total_open_trades():
    return len(get_open_trades())


def find_open_trade_for_instrument(instrument):
    trades = get_open_trades()
    for trade in trades:
        if trade.get("instrument") == instrument:
            return trade
    return None


def pair_has_open_trade(instrument):
    return find_open_trade_for_instrument(instrument) is not None


def signal_matches_open_side(open_trade, signal):
    """
    OANDA units > 0 means long, < 0 means short
    """
    current_units = float(open_trade.get("currentUnits", "0"))
    if signal == "BUY" and current_units > 0:
        return True
    if signal == "SELL" and current_units < 0:
        return True
    return False


def close_trade(trade_id):
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close"
    response = requests.put(url, headers=oanda_headers(), json={}, timeout=20)
    print("CLOSE TRADE RESPONSE:", response.text)
    try:
        data = response.json()
    except Exception:
        data = {"raw_response": response.text}
    return data, response.status_code


def calculate_units(pair, signal):
    cfg = PAIR_CONFIG[pair]
    balance = get_balance()
    risk_amount = balance * (RISK_PERCENT / 100.0)

    sl_distance = cfg["sl_distance"]
    pip_value_per_unit = cfg["pip_value_per_unit"]

    raw_units = risk_amount / (sl_distance * pip_value_per_unit)
    units = max(1, math.floor(raw_units))
    units = min(units, cfg["max_units"])

    if signal == "SELL":
        units = -units

    return units


def get_current_spread(pair):
    cfg = PAIR_CONFIG[pair]
    instrument = cfg["instrument"]

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    params = {"instruments": instrument}
    response = requests.get(url, headers=oanda_headers(), params=params, timeout=15)
    response.raise_for_status()

    prices = response.json().get("prices", [])
    if not prices:
        raise Exception(f"no pricing returned for {pair}")

    price = prices[0]
    bids = price.get("bids", [])
    asks = price.get("asks", [])

    if not bids or not asks:
        raise Exception(f"missing bid/ask for {pair}")

    bid = float(bids[0]["price"])
    ask = float(asks[0]["price"])
    spread = ask - bid

    return spread, bid, ask


def spread_ok(pair):
    spread, bid, ask = get_current_spread(pair)
    max_spread = PAIR_CONFIG[pair]["max_spread"]
    return spread <= max_spread, {
        "spread": spread,
        "max_spread": max_spread,
        "bid": bid,
        "ask": ask
    }


def cooldown_active(pair, signal):
    key = f"{pair}:{signal}"
    last_time = last_signal_times.get(key)
    if not last_time:
        return False, 0

    now_utc = datetime.now(timezone.utc)
    elapsed_minutes = (now_utc - last_time).total_seconds() / 60.0
    remaining = max(0, COOLDOWN_MINUTES - elapsed_minutes)

    return elapsed_minutes < COOLDOWN_MINUTES, round(remaining, 2)


def set_cooldown(pair, signal):
    key = f"{pair}:{signal}"
    last_signal_times[key] = datetime.now(timezone.utc)


def place_oanda_market_order(signal, pair):
    cfg = PAIR_CONFIG[pair]
    instrument = cfg["instrument"]
    units = calculate_units(pair, signal)

    payload = {
        "order": {
            "instrument": instrument,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "distance": str(cfg["sl_distance"])
            },
            "takeProfitOnFill": {
                "distance": str(cfg["tp_distance"])
            }
        }
    }

    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    response = requests.post(url, headers=oanda_headers(), json=payload, timeout=20)

    print("ORDER PAYLOAD:", payload)
    print("OANDA RESPONSE:", response.text)

    try:
        data = response.json()
    except Exception:
        data = {"raw_response": response.text}

    return data, response.status_code


# =========================
# MAIN SIGNAL PROCESSING
# =========================
def process_signal(signal, pair):
    cfg = PAIR_CONFIG[pair]
    instrument = cfg["instrument"]

    # Safety checks
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        return {
            "status": "rejected",
            "reason": "missing OANDA credentials"
        }, 400

    if not in_allowed_session():
        return {
            "status": "skipped",
            "reason": "outside allowed session",
            "timezone": SESSION_TIMEZONE,
            "current_hour": now_local().hour
        }, 200

    active, remaining = cooldown_active(pair, signal)
    if active:
        return {
            "status": "skipped",
            "reason": "cooldown active",
            "pair": pair,
            "signal": signal,
            "remaining_minutes": remaining
        }, 200

    spread_pass, spread_info = spread_ok(pair)
    if not spread_pass:
        return {
            "status": "skipped",
            "reason": "spread too wide",
            "pair": pair,
            "signal": signal,
            "spread_info": spread_info
        }, 200

    open_trade = find_open_trade_for_instrument(instrument)

    if open_trade:
        if signal_matches_open_side(open_trade, signal):
            return {
                "status": "skipped",
                "reason": "same-direction trade already open",
                "pair": pair,
                "signal": signal
            }, 200

        if ALLOW_REVERSE_SIGNAL_CLOSE:
            close_result, close_code = close_trade(open_trade["id"])
            if close_code >= 300:
                return {
                    "status": "rejected",
                    "reason": "failed to close opposite trade",
                    "pair": pair,
                    "signal": signal,
                    "close_result": close_result
                }, 400
        else:
            return {
                "status": "skipped",
                "reason": "opposite trade open and reverse close disabled",
                "pair": pair,
                "signal": signal
            }, 200

    if total_open_trades() >= MAX_OPEN_TRADES:
        return {
            "status": "skipped",
            "reason": "max open trades reached",
            "max_open_trades": MAX_OPEN_TRADES
        }, 200

    result, status_code = place_oanda_market_order(signal, pair)

    if 200 <= status_code < 300:
        set_cooldown(pair, signal)

    return {
        "status": "processed" if 200 <= status_code < 300 else "rejected",
        "pair": pair,
        "signal": signal,
        "spread_info": spread_info,
        "result": result
    }, status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
