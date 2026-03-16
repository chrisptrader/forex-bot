
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

ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").lower() == "true"
SESSION_TIMEZONE = os.getenv("SESSION_TIMEZONE", "America/New_York")
SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "3"))
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "17"))

ALLOW_REVERSE_SIGNAL_CLOSE = os.getenv("ALLOW_REVERSE_SIGNAL_CLOSE", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
DUPLICATE_SIGNAL_SECONDS = int(os.getenv("DUPLICATE_SIGNAL_SECONDS", "90"))

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

# =========================
# IN-MEMORY STATE
# Note: resets if Render restarts
# =========================
STATE = {
    "daily_date": None,
    "daily_start_nav": None,
    "trades_today": 0,
    "last_signal_times": {}, # key: "PAIR:SIGNAL"
    "last_signal_ids": {} # key: alert_id or synthetic key
}


# =========================
# BASIC ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200


@app.route("/status", methods=["GET"])
def status():
    today_local = now_local().date().isoformat()
    return jsonify({
        "bot": "running",
        "dry_run": DRY_RUN,
        "timezone": SESSION_TIMEZONE,
        "session_filter": ENABLE_SESSION_FILTER,
        "session_start_hour": SESSION_START_HOUR,
        "session_end_hour": SESSION_END_HOUR,
        "risk_percent": RISK_PERCENT,
        "max_open_trades": MAX_OPEN_TRADES,
        "cooldown_minutes": COOLDOWN_MINUTES,
        "max_daily_loss_percent": MAX_DAILY_LOSS_PERCENT,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "state": {
            "today_local": today_local,
            "daily_date": str(STATE["daily_date"]) if STATE["daily_date"] else None,
            "daily_start_nav": STATE["daily_start_nav"],
            "trades_today": STATE["trades_today"]
        }
    }), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    print("WEBHOOK RECEIVED:", data)

    if not data:
        return jsonify({"error": "missing json body"}), 400

    signal = str(data.get("signal", "")).upper().strip()
    pair = str(data.get("pair", "")).upper().replace("/", "").strip()
    alert_id = str(data.get("id", "")).strip() # optional TradingView/custom id

    if signal not in ["BUY", "SELL"]:
        return jsonify({"error": "signal must be BUY or SELL"}), 400

    if pair not in PAIR_CONFIG:
        return jsonify({"error": f"unsupported pair: {pair}"}), 400

    try:
        result, status_code = process_signal(signal, pair, alert_id)
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


def current_utc():
    return datetime.now(timezone.utc)


def reset_daily_state_if_needed():
    today = now_local().date()
    if STATE["daily_date"] != today:
        STATE["daily_date"] = today
        STATE["daily_start_nav"] = None
        STATE["trades_today"] = 0
        # keep cooldown dictionaries across same app run


def in_allowed_session():
    if not ENABLE_SESSION_FILTER:
        return True, None

    local_now = now_local()
    weekday = local_now.weekday() # Mon=0 Sun=6

    # Skip weekends
    if weekday >= 5:
        return False, {
            "reason": "weekend",
            "weekday": weekday,
            "local_time": local_now.isoformat()
        }

    hour = local_now.hour
    if not (SESSION_START_HOUR <= hour < SESSION_END_HOUR):
        return False, {
            "reason": "outside session hours",
            "hour": hour,
            "local_time": local_now.isoformat()
        }

    return True, None


def get_account_summary():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    response = requests.get(url, headers=oanda_headers(), timeout=15)
    response.raise_for_status()
    return response.json()


def get_account_numbers():
    summary = get_account_summary()
    account = summary.get("account", {})
    balance = float(account.get("balance", 0))
    nav = float(account.get("NAV", balance))
    unrealized_pl = float(account.get("unrealizedPL", 0))
    margin_available = float(account.get("marginAvailable", 0))
    return {
        "balance": balance,
        "nav": nav,
        "unrealized_pl": unrealized_pl,
        "margin_available": margin_available
    }


def enforce_daily_loss_stop():
    reset_daily_state_if_needed()

    acct = get_account_numbers()
    nav = acct["nav"]

    if STATE["daily_start_nav"] is None:
        STATE["daily_start_nav"] = nav

    start_nav = STATE["daily_start_nav"]
    if start_nav <= 0:
        return True, {"nav": nav, "start_nav": start_nav, "drawdown_percent": 0}

    drawdown_percent = ((start_nav - nav) / start_nav) * 100.0

    if drawdown_percent >= MAX_DAILY_LOSS_PERCENT:
        return False, {
            "reason": "daily loss stop hit",
            "start_nav": round(start_nav, 2),
            "current_nav": round(nav, 2),
            "drawdown_percent": round(drawdown_percent, 2),
            "max_daily_loss_percent": MAX_DAILY_LOSS_PERCENT
        }

    return True, {
        "start_nav": round(start_nav, 2),
        "current_nav": round(nav, 2),
        "drawdown_percent": round(drawdown_percent, 2)
    }


def trades_today_ok():
    reset_daily_state_if_needed()
    if STATE["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, {
            "reason": "max trades per day reached",
            "trades_today": STATE["trades_today"],
            "max_trades_per_day": MAX_TRADES_PER_DAY
        }
    return True, {
        "trades_today": STATE["trades_today"],
        "max_trades_per_day": MAX_TRADES_PER_DAY
    }


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


def signal_matches_open_side(open_trade, signal):
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
    acct = get_account_numbers()

    risk_amount = acct["nav"] * (RISK_PERCENT / 100.0)
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

    return {
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "max_spread": PAIR_CONFIG[pair]["max_spread"]
    }


def spread_ok(pair):
    info = get_current_spread(pair)
    return info["spread"] <= info["max_spread"], info


def cooldown_active(pair, signal):
    key = f"{pair}:{signal}"
    last_time = STATE["last_signal_times"].get(key)

    if not last_time:
        return False, 0

    elapsed_minutes = (current_utc() - last_time).total_seconds() / 60.0
    remaining = max(0, COOLDOWN_MINUTES - elapsed_minutes)

    return elapsed_minutes < COOLDOWN_MINUTES, round(remaining, 2)


def set_cooldown(pair, signal):
    key = f"{pair}:{signal}"
    STATE["last_signal_times"][key] = current_utc()


def duplicate_signal_seen(alert_id, pair, signal):
    if alert_id:
        key = f"id:{alert_id}"
    else:
        # fallback if no unique id is sent
        key = f"fallback:{pair}:{signal}"

    seen_at = STATE["last_signal_ids"].get(key)
    if not seen_at:
        return False

    age = (current_utc() - seen_at).total_seconds()
    return age < DUPLICATE_SIGNAL_SECONDS


def remember_signal(alert_id, pair, signal):
    if alert_id:
        key = f"id:{alert_id}"
    else:
        key = f"fallback:{pair}:{signal}"
    STATE["last_signal_ids"][key] = current_utc()


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

    if DRY_RUN:
        print("DRY RUN ORDER PAYLOAD:", payload)
        return {
            "dry_run": True,
            "payload": payload
        }, 200

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
def process_signal(signal, pair, alert_id=""):
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        return {
            "status": "rejected",
            "reason": "missing OANDA credentials"
        }, 400

    if duplicate_signal_seen(alert_id, pair, signal):
        return {
            "status": "skipped",
            "reason": "duplicate signal blocked",
            "pair": pair,
            "signal": signal,
            "alert_id": alert_id or None
        }, 200

    remember_signal(alert_id, pair, signal)

    session_pass, session_info = in_allowed_session()
    if not session_pass:
        return {
            "status": "skipped",
            "reason": "session filter blocked trade",
            "details": session_info
        }, 200

    daily_loss_pass, daily_loss_info = enforce_daily_loss_stop()
    if not daily_loss_pass:
        return {
            "status": "skipped",
            "reason": "daily loss stop blocked trade",
            "details": daily_loss_info
        }, 200

    trades_day_pass, trades_day_info = trades_today_ok()
    if not trades_day_pass:
        return {
            "status": "skipped",
            "reason": "daily trade limit blocked trade",
            "details": trades_day_info
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

    cfg = PAIR_CONFIG[pair]
    instrument = cfg["instrument"]
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
                "reason": "opposite trade already open",
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
        STATE["trades_today"] += 1

    return {
        "status": "processed" if 200 <= status_code < 300 else "rejected",
        "pair": pair,
        "signal": signal,
        "spread_info": spread_info,
        "daily_loss_info": daily_loss_info,
        "daily_trade_info": trades_day_info,
        "result": result
    }, status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
