from flask import Flask, request, jsonify
import os
import requests
import math
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================
# ENV / SETTINGS
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com").strip()

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.5"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "2"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "4"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").lower() == "true"
SESSION_TIMEZONE = os.getenv("SESSION_TIMEZONE", "America/New_York")
SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "7"))
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "16"))

ENABLE_SPREAD_FILTER = os.getenv("ENABLE_SPREAD_FILTER", "true").lower() == "true"
ALLOW_REVERSE_SIGNAL_CLOSE = os.getenv("ALLOW_REVERSE_SIGNAL_CLOSE", "false").lower() == "true"

ENABLE_TRAILING_STOP = os.getenv("ENABLE_TRAILING_STOP", "true").lower() == "true"
ENABLE_BACKGROUND_MANAGER = os.getenv("ENABLE_BACKGROUND_MANAGER", "true").lower() == "true"
MANAGER_INTERVAL_SECONDS = int(os.getenv("MANAGER_INTERVAL_SECONDS", "60"))
TRAIL_TO_BREAKEVEN_R = float(os.getenv("TRAIL_TO_BREAKEVEN_R", "1.0"))
TRAIL_LOCK_R = float(os.getenv("TRAIL_LOCK_R", "1.5"))
TRAIL_LOCK_MULTIPLIER = float(os.getenv("TRAIL_LOCK_MULTIPLIER", "0.5"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PAIR_MAP = {
    "EURUSD": {
        "instrument": "EUR_USD",
        "sl_distance": 0.0020,
        "tp_distance": 0.0040,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00020,
        "price_precision": 5
    },
    "GBPUSD": {
        "instrument": "GBP_USD",
        "sl_distance": 0.0025,
        "tp_distance": 0.0050,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00030,
        "price_precision": 5
    },
    "XAUUSD": {
        "instrument": "XAU_USD",
        "sl_distance": 10.0,
        "tp_distance": 20.0,
        "pip_value_per_unit": 1.0,
        "max_units": 100,
        "max_spread": 1.00,
        "price_precision": 2
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
    "last_signal_time": None,
    "manager_started": False
}


# =========================
# BASIC HELPERS
# =========================
def now_local():
    return datetime.now(ZoneInfo(SESSION_TIMEZONE))


def reset_daily_state_if_needed():
    today = now_local().date().isoformat()
    if STATE["daily_date"] != today:
        STATE["daily_date"] = today
        STATE["daily_start_nav"] = None
        STATE["trades_today"] = 0
        STATE["last_signal_key"] = None
        STATE["last_signal_time"] = None


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def oanda_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }


def instrument_to_pair(instrument):
    for pair, info in PAIR_MAP.items():
        if info["instrument"] == instrument:
            return pair
    return None


# =========================
# OANDA HELPERS
# =========================
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
    return float(account["NAV"])


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


def pair_has_open_trade(pair):
    instrument = PAIR_MAP[pair]["instrument"]
    for trade in get_open_trades():
        if trade.get("instrument") == instrument:
            return True
    return False


def get_trade_details(trade_id):
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}"
    r = requests.get(url, headers=oanda_headers(), timeout=20)
    data = r.json()
    return data.get("trade", {})


def close_trade(trade_id):
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close"
    r = requests.put(url, headers=oanda_headers(), json={}, timeout=20)

    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw_response": r.text}


def replace_trade_stop_loss(trade_id, price):
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders"
    payload = {
        "stopLoss": {
            "price": str(price),
            "timeInForce": "GTC"
        }
    }

    r = requests.put(url, headers=oanda_headers(), json=payload, timeout=20)

    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw_response": r.text}


def get_pricing(instrument):
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    params = {"instruments": instrument}
    r = requests.get(url, headers=oanda_headers(), params=params, timeout=20)

    data = r.json()
    prices = data.get("prices", [])
    if not prices:
        raise Exception(f"No pricing found for {instrument}")

    return prices[0]


def get_current_spread(pair):
    instrument = PAIR_MAP[pair]["instrument"]
    price_data = get_pricing(instrument)

    bids = price_data.get("bids", [])
    asks = price_data.get("asks", [])

    if not bids or not asks:
        raise Exception(f"Missing bid/ask for {instrument}")

    bid = float(bids[0]["price"])
    ask = float(asks[0]["price"])
    spread = ask - bid

    return spread, bid, ask


# =========================
# RISK / FILTERS
# =========================
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


def session_allowed():
    if not ENABLE_SESSION_FILTER:
        return True

    hour = now_local().hour
    return SESSION_START_HOUR <= hour < SESSION_END_HOUR


# =========================
# TRAILING STOP MANAGER
# =========================
def trailing_stop_manager():
    if not ENABLE_TRAILING_STOP:
        return {"status": "disabled"}

    trades = get_open_trades()
    updates = []

    for trade in trades:
        try:
            trade_id = trade.get("id")
            instrument = trade.get("instrument")
            pair = instrument_to_pair(instrument)

            if not pair:
                continue

            pair_info = PAIR_MAP[pair]
            sl_distance = pair_info["sl_distance"]
            precision = pair_info["price_precision"]

            current_units = float(trade.get("currentUnits", "0"))
            if current_units == 0:
                continue

            side = "BUY" if current_units > 0 else "SELL"
            entry_price = float(trade.get("price"))

            pricing = get_pricing(instrument)
            bid = float(pricing["bids"][0]["price"])
            ask = float(pricing["asks"][0]["price"])
            current_price = bid if side == "BUY" else ask

            profit_distance = (current_price - entry_price) if side == "BUY" else (entry_price - current_price)
            current_r = profit_distance / sl_distance if sl_distance > 0 else 0

            trade_details = get_trade_details(trade_id)
            existing_sl = trade_details.get("stopLossOrder")
            existing_sl_price = None
            if existing_sl and existing_sl.get("price"):
                existing_sl_price = float(existing_sl["price"])

            new_sl_price = None

            # Step 1: move stop to breakeven
            if current_r >= TRAIL_TO_BREAKEVEN_R:
                breakeven = entry_price
                if side == "BUY":
                    if existing_sl_price is None or existing_sl_price < breakeven:
                        new_sl_price = breakeven
                else:
                    if existing_sl_price is None or existing_sl_price > breakeven:
                        new_sl_price = breakeven

            # Step 2: lock profit
            if current_r >= TRAIL_LOCK_R:
                lock_distance = sl_distance * TRAIL_LOCK_MULTIPLIER
                lock_price = entry_price + lock_distance if side == "BUY" else entry_price - lock_distance

                if side == "BUY":
                    if existing_sl_price is None or lock_price > existing_sl_price:
                        new_sl_price = lock_price
                else:
                    if existing_sl_price is None or lock_price < existing_sl_price:
                        new_sl_price = lock_price

            if new_sl_price is not None:
                rounded_price = round(new_sl_price, precision)
                status_code, result = replace_trade_stop_loss(trade_id, rounded_price)

                updates.append({
                    "trade_id": trade_id,
                    "pair": pair,
                    "side": side,
                    "current_r": round(current_r, 2),
                    "new_stop_loss": rounded_price,
                    "status_code": status_code,
                    "result": result
                })

        except Exception as e:
            updates.append({"error": str(e), "trade_id": trade.get("id")})

    return {"status": "ok", "updates": updates}


def background_manager_loop():
    while True:
        try:
            trailing_stop_manager()
        except Exception:
            pass
        time.sleep(MANAGER_INTERVAL_SECONDS)


def ensure_background_manager():
    if not ENABLE_BACKGROUND_MANAGER:
        return

    if STATE["manager_started"]:
        return

    STATE["manager_started"] = True
    t = threading.Thread(target=background_manager_loop, daemon=True)
    t.start()


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    ensure_background_manager()
    return "Forex bot is running!", 200


@app.route("/status", methods=["GET"])
def status():
    ensure_background_manager()
    reset_daily_state_if_needed()

    open_trades = get_open_trades()
    account_summary = get_account_summary()

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
        "enable_session_filter": ENABLE_SESSION_FILTER,
        "session_timezone": SESSION_TIMEZONE,
        "session_start_hour": SESSION_START_HOUR,
        "session_end_hour": SESSION_END_HOUR,
        "enable_spread_filter": ENABLE_SPREAD_FILTER,
        "allow_reverse_signal_close": ALLOW_REVERSE_SIGNAL_CLOSE,
        "enable_trailing_stop": ENABLE_TRAILING_STOP,
        "enable_background_manager": ENABLE_BACKGROUND_MANAGER,
        "manager_interval_seconds": MANAGER_INTERVAL_SECONDS,
        "trail_to_breakeven_r": TRAIL_TO_BREAKEVEN_R,
        "trail_lock_r": TRAIL_LOCK_R,
        "trail_lock_multiplier": TRAIL_LOCK_MULTIPLIER,
        "supported_pairs": list(PAIR_MAP.keys()),
        "trades_today": STATE["trades_today"],
        "open_trades_count": len(open_trades),
        "daily_start_nav": STATE["daily_start_nav"],
        "account_summary": account_summary
    }), 200


@app.route("/manage-trailing", methods=["GET", "POST"])
def manage_trailing():
    ensure_background_manager()
    result = trailing_stop_manager()
    return jsonify(result), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    ensure_background_manager()
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

    if not session_allowed():
        return jsonify({
            "status": "blocked",
            "reason": "Outside trading session"
        }), 200

    if duplicate_signal(signal, pair):
        return jsonify({
            "status": "blocked",
            "reason": "Duplicate signal / cooldown active"
        }), 200

    if pair_has_open_trade(pair):
        return jsonify({
            "status": "blocked",
            "reason": "Trade already open for this pair"
        }), 200

    open_count = total_open_trades()
    if open_count >= MAX_OPEN_TRADES:
        return jsonify({
            "status": "blocked",
            "reason": "Max open trades reached",
            "open_trades_count": open_count
        }), 200

    if STATE["trades_today"] >= MAX_TRADES_PER_DAY:
        return jsonify({
            "status": "blocked",
            "reason": "Max trades per day reached",
            "trades_today": STATE["trades_today"]
        }), 200

    loss_hit, drawdown_percent = daily_loss_hit()
    if loss_hit:
        send_telegram(f"Bot blocked: daily loss limit hit ({round(drawdown_percent, 2)}%)")
        return jsonify({
            "status": "blocked",
            "reason": "Daily loss limit hit",
            "drawdown_percent": round(drawdown_percent, 2)
        }), 200

    spread = None
    bid = None
    ask = None

    if ENABLE_SPREAD_FILTER:
        try:
            spread, bid, ask = get_current_spread(pair)
            max_spread = PAIR_MAP[pair]["max_spread"]

            if spread > max_spread:
                return jsonify({
                    "status": "blocked",
                    "reason": "Spread too high",
                    "spread": spread,
                    "max_spread": max_spread
                }), 200
        except Exception as e:
            return jsonify({
                "status": "blocked",
                "reason": f"Spread check failed: {str(e)}"
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

        message = (
            f"Trade opened\n"
            f"Pair: {pair}\n"
            f"Signal: {signal}\n"
            f"Units: {units}\n"
            f"Risk: {RISK_PERCENT}%\n"
            f"SL distance: {sl_distance}\n"
            f"TP distance: {tp_distance}"
        )

        if spread is not None:
            message += f"\nSpread: {spread}"

        send_telegram(message)

    return jsonify({
        "status_code": r.status_code,
        "pair": pair,
        "signal": signal,
        "risk_percent": RISK_PERCENT,
        "units": units,
        "stop_loss_distance": sl_distance,
        "take_profit_distance": tp_distance,
        "spread": spread,
        "bid": bid,
        "ask": ask,
        "trades_today": STATE["trades_today"],
        "result": result
    }), r.status_code


if __name__ == "__main__":
    ensure_background_manager()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
