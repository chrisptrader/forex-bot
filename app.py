from flask import Flask, request, jsonify
import os
import requests
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================
# ENV SETTINGS
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))

COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))
DUPLICATE_SIGNAL_SECONDS = int(os.getenv("DUPLICATE_SIGNAL_SECONDS", "90"))

ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").lower() == "true"
SESSION_TIMEZONE = os.getenv("SESSION_TIMEZONE", "America/New_York")
SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "4"))
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "11"))

ENABLE_SPREAD_FILTER = os.getenv("ENABLE_SPREAD_FILTER", "true").lower() == "true"
ENABLE_VOLATILITY_FILTER = os.getenv("ENABLE_VOLATILITY_FILTER", "true").lower() == "true"
ENABLE_TREND_FILTER = os.getenv("ENABLE_TREND_FILTER", "true").lower() == "true"
ENABLE_CORRELATION_FILTER = os.getenv("ENABLE_CORRELATION_FILTER", "true").lower() == "true"

ALLOW_REVERSE_SIGNAL_CLOSE = os.getenv("ALLOW_REVERSE_SIGNAL_CLOSE", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

PAIR_CONFIG = {
    "EURUSD": {
        "instrument": "EUR_USD",
        "sl_distance": 0.0020,
        "tp_distance": 0.0040,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00025,
        "min_m15_range": 0.0008
    },
    "GBPUSD": {
        "instrument": "GBP_USD",
        "sl_distance": 0.0025,
        "tp_distance": 0.0050,
        "pip_value_per_unit": 0.0001,
        "max_units": 100000,
        "max_spread": 0.00035,
        "min_m15_range": 0.0012
    },
    "XAUUSD": {
        "instrument": "XAU_USD",
        "sl_distance": 10.0,
        "tp_distance": 20.0,
        "pip_value_per_unit": 1.0,
        "max_units": 100,
        "max_spread": 0.60,
        "min_m15_range": 8.0
    }
}

# simple correlation groups
CORRELATED_GROUPS = [
    {"EURUSD", "GBPUSD"}
]

STATE = {
    "daily_date": None,
    "daily_start_nav": None,
    "trades_today": 0,
    "last_signal_times": {},
    "last_signal_ids": {}
}


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200


@app.route("/status", methods=["GET"])
def status():
    try:
        reset_daily_state_if_needed()
        acct = safe_get_account_numbers()
        open_trades = get_open_trades_safe()

        return jsonify({
            "bot": "running",
            "dry_run": DRY_RUN,
            "risk_percent": RISK_PERCENT,
            "max_open_trades": MAX_OPEN_TRADES,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "max_daily_loss_percent": MAX_DAILY_LOSS_PERCENT,
            "cooldown_minutes": COOLDOWN_MINUTES,
            "duplicate_signal_seconds": DUPLICATE_SIGNAL_SECONDS,
            "session_filter": ENABLE_SESSION_FILTER,
            "session_timezone": SESSION_TIMEZONE,
            "session_start_hour": SESSION_START_HOUR,
            "session_end_hour": SESSION_END_HOUR,
            "spread_filter": ENABLE_SPREAD_FILTER,
            "volatility_filter": ENABLE_VOLATILITY_FILTER,
            "trend_filter": ENABLE_TREND_FILTER,
            "correlation_filter": ENABLE_CORRELATION_FILTER,
            "allow_reverse_signal_close": ALLOW_REVERSE_SIGNAL_CLOSE,
            "state": {
                "daily_date": str(STATE.get("daily_date")),
                "daily_start_nav": STATE.get("daily_start_nav"),
                "trades_today": STATE.get("trades_today")
            },
            "account": acct,
            "open_trades_count": len(open_trades),
            "open_pairs": [trade.get("instrument") for trade in open_trades]
        }), 200

    except Exception as e:
        return jsonify({
            "bot": "running",
            "status_error": str(e)
        }), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    print("WEBHOOK RECEIVED:", data)

    if not data:
        return jsonify({"error": "missing json body"}), 400

    signal = str(data.get("signal", "")).upper().strip()
    pair = str(data.get("pair", "")).upper().replace("/", "").strip()
    alert_id = str(data.get("id", "")).strip()

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
# TIME HELPERS
# =========================
def now_local():
    return datetime.now(ZoneInfo(SESSION_TIMEZONE))


def now_utc():
    return datetime.now(timezone.utc)


def reset_daily_state_if_needed():
    today = now_local().date()
    if STATE["daily_date"] != today:
        STATE["daily_date"] = today
        STATE["daily_start_nav"] = None
        STATE["trades_today"] = 0


def in_allowed_session():
    if not ENABLE_SESSION_FILTER:
        return True, {"reason": "session filter disabled"}

    local_now = now_local()
    weekday = local_now.weekday()

    if weekday >= 5:
        return False, {
            "reason": "weekend",
            "local_time": local_now.isoformat()
        }

    if not (SESSION_START_HOUR <= local_now.hour < SESSION_END_HOUR):
        return False, {
            "reason": "outside session",
            "local_time": local_now.isoformat()
        }

    return True, {
        "local_time": local_now.isoformat()
    }


# =========================
# OANDA HELPERS
# =========================
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


def get_account_numbers():
    summary = get_account_summary()
    account = summary.get("account", {})
    balance = float(account.get("balance", 0))
    nav = float(account.get("NAV", balance))
    margin_available = float(account.get("marginAvailable", 0))
    unrealized_pl = float(account.get("unrealizedPL", 0))
    return {
        "balance": round(balance, 2),
        "nav": round(nav, 2),
        "margin_available": round(margin_available, 2),
        "unrealized_pl": round(unrealized_pl, 2)
    }


def safe_get_account_numbers():
    try:
        return get_account_numbers()
    except Exception as e:
        return {"error": str(e)}


def get_open_trades():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    response = requests.get(url, headers=oanda_headers(), timeout=15)
    response.raise_for_status()
    return response.json().get("trades", [])


def get_open_trades_safe():
    try:
        return get_open_trades()
    except Exception:
        return []


def total_open_trades():
    return len(get_open_trades())


def find_open_trade_for_instrument(instrument):
    for trade in get_open_trades():
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


def get_pricing(pair):
    instrument = PAIR_CONFIG[pair]["instrument"]
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    params = {"instruments": instrument}
    response = requests.get(url, headers=oanda_headers(), params=params, timeout=15)
    response.raise_for_status()
    prices = response.json().get("prices", [])
    if not prices:
        raise Exception(f"no pricing returned for {pair}")
    return prices[0]


def get_recent_candles(pair, count=30, granularity="H1"):
    instrument = PAIR_CONFIG[pair]["instrument"]
    url = f"{OANDA_BASE_URL}/v3/instruments/{instrument}/candles"
    params = {
        "count": count,
        "price": "M",
        "granularity": granularity
    }
    response = requests.get(url, headers=oanda_headers(), params=params, timeout=15)
    response.raise_for_status()
    return response.json().get("candles", [])


# =========================
# PROTECTION FILTERS
# =========================
def enforce_daily_loss_stop():
    reset_daily_state_if_needed()
    acct = get_account_numbers()
    nav = acct["nav"]

    if STATE["daily_start_nav"] is None:
        STATE["daily_start_nav"] = nav

    start_nav = STATE["daily_start_nav"]
    if start_nav <= 0:
        return True, {"drawdown_percent": 0}

    drawdown_percent = ((start_nav - nav) / start_nav) * 100.0

    if drawdown_percent >= MAX_DAILY_LOSS_PERCENT:
        return False, {
            "reason": "daily loss stop hit",
            "start_nav": round(start_nav, 2),
            "current_nav": round(nav, 2),
            "drawdown_percent": round(drawdown_percent, 2)
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
            "reason": "daily trade limit reached",
            "trades_today": STATE["trades_today"],
            "max_trades_per_day": MAX_TRADES_PER_DAY
        }
    return True, {
        "trades_today": STATE["trades_today"],
        "max_trades_per_day": MAX_TRADES_PER_DAY
    }


def cooldown_active(pair, signal):
    key = f"{pair}:{signal}"
    last_time = STATE["last_signal_times"].get(key)
    if not last_time:
        return False, 0

    elapsed_minutes = (now_utc() - last_time).total_seconds() / 60.0
    remaining = max(0, COOLDOWN_MINUTES - elapsed_minutes)
    return elapsed_minutes < COOLDOWN_MINUTES, round(remaining, 2)


def set_cooldown(pair, signal):
    STATE["last_signal_times"][f"{pair}:{signal}"] = now_utc()


def duplicate_signal_seen(alert_id, pair, signal):
    key = f"id:{alert_id}" if alert_id else f"fallback:{pair}:{signal}"
    seen_at = STATE["last_signal_ids"].get(key)
    if not seen_at:
        return False
    age = (now_utc() - seen_at).total_seconds()
    return age < DUPLICATE_SIGNAL_SECONDS


def remember_signal(alert_id, pair, signal):
    key = f"id:{alert_id}" if alert_id else f"fallback:{pair}:{signal}"
    STATE["last_signal_ids"][key] = now_utc()


def spread_ok(pair):
    if not ENABLE_SPREAD_FILTER:
        return True, {"reason": "spread filter disabled"}

    price = get_pricing(pair)
    bids = price.get("bids", [])
    asks = price.get("asks", [])
    if not bids or not asks:
        raise Exception(f"missing bid/ask for {pair}")

    bid = float(bids[0]["price"])
    ask = float(asks[0]["price"])
    spread = ask - bid
    max_spread = PAIR_CONFIG[pair]["max_spread"]

    return spread <= max_spread, {
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "max_spread": max_spread
    }


def volatility_ok(pair):
    if not ENABLE_VOLATILITY_FILTER:
        return True, {"reason": "volatility filter disabled"}

    candles = get_recent_candles(pair, count=2, granularity="M15")
    if len(candles) < 2:
        return False, {"reason": "not enough candles"}

    last_closed = candles[-2]
    high = float(last_closed["mid"]["h"])
    low = float(last_closed["mid"]["l"])
    candle_range = high - low
    min_range = PAIR_CONFIG[pair]["min_m15_range"]

    if candle_range < min_range:
        return False, {
            "reason": "range too small",
            "range": candle_range,
            "min_range": min_range
        }

    return True, {
        "range": candle_range,
        "min_range": min_range
    }


def trend_ok(pair, signal):
    if not ENABLE_TREND_FILTER:
        return True, {"reason": "trend filter disabled"}

    candles = get_recent_candles(pair, count=20, granularity="H1")
    if len(candles) < 20:
        return False, {"reason": "not enough H1 candles"}

    closes = [float(c["mid"]["c"]) for c in candles[:-1]]
    if len(closes) < 19:
        return False, {"reason": "not enough closed H1 candles"}

    sma = sum(closes) / len(closes)
    last_close = closes[-1]

    if signal == "BUY" and last_close < sma:
        return False, {
            "reason": "trend filter blocked long",
            "last_close": last_close,
            "sma": sma
        }

    if signal == "SELL" and last_close > sma:
        return False, {
            "reason": "trend filter blocked short",
            "last_close": last_close,
            "sma": sma
        }

    return True, {
        "last_close": last_close,
        "sma": sma
    }


def pair_in_same_correlation_group(pair_a, pair_b):
    for group in CORRELATED_GROUPS:
        if pair_a in group and pair_b in group:
            return True
    return False


def correlation_blocked(pair, signal):
    if not ENABLE_CORRELATION_FILTER:
        return False, {"reason": "correlation filter disabled"}

    open_trades = get_open_trades()
    open_instruments = [trade.get("instrument") for trade in open_trades]

    pair_to_instrument = {p: cfg["instrument"] for p, cfg in PAIR_CONFIG.items()}
    instrument_to_pair = {v: k for k, v in pair_to_instrument.items()}

    for instrument in open_instruments:
        open_pair = instrument_to_pair.get(instrument)
        if not open_pair or open_pair == pair:
            continue

        if pair_in_same_correlation_group(pair, open_pair):
            open_trade = find_open_trade_for_instrument(instrument)
            if open_trade and signal_matches_open_side(open_trade, signal):
                return True, {
                    "reason": "correlated same-direction trade already open",
                    "pair": pair,
                    "blocking_pair": open_pair
                }

    return False, {"reason": "no correlation block"}


# =========================
# ORDER LOGIC
# =========================
def calculate_units(pair, signal):
    cfg = PAIR_CONFIG[pair]
    acct = get_account_numbers()
    risk_amount = acct["nav"] * (RISK_PERCENT / 100.0)

    raw_units = risk_amount / (cfg["sl_distance"] * cfg["pip_value_per_unit"])
    units = max(1, math.floor(raw_units))
    units = min(units, cfg["max_units"])

    if signal == "SELL":
        units = -units

    return units


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
        return {"dry_run": True, "payload": payload}, 200

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
# MAIN ENGINE
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
            "signal": signal
        }, 200

    remember_signal(alert_id, pair, signal)

    session_pass, session_info = in_allowed_session()
    if not session_pass:
        return {
            "status": "skipped",
            "reason": "session blocked",
            "details": session_info
        }, 200

    daily_pass, daily_info = enforce_daily_loss_stop()
    if not daily_pass:
        return {
            "status": "skipped",
            "reason": "daily loss stop hit",
            "details": daily_info
        }, 200

    trades_pass, trades_info = trades_today_ok()
    if not trades_pass:
        return {
            "status": "skipped",
            "reason": "daily trade cap hit",
            "details": trades_info
        }, 200

    cd_active, cd_remaining = cooldown_active(pair, signal)
    if cd_active:
        return {
            "status": "skipped",
            "reason": "cooldown active",
            "remaining_minutes": cd_remaining
        }, 200

    spread_pass, spread_info = spread_ok(pair)
    if not spread_pass:
        return {
            "status": "skipped",
            "reason": "spread too wide",
            "spread_info": spread_info
        }, 200

    vol_pass, vol_info = volatility_ok(pair)
    if not vol_pass:
        return {
            "status": "skipped",
            "reason": "volatility blocked",
            "volatility_info": vol_info
        }, 200

    trend_pass, trend_info = trend_ok(pair, signal)
    if not trend_pass:
        return {
            "status": "skipped",
            "reason": "trend filter blocked trade",
            "trend_info": trend_info
        }, 200

    corr_blocked, corr_info = correlation_blocked(pair, signal)
    if corr_blocked:
        return {
            "status": "skipped",
            "reason": "correlation filter blocked trade",
            "correlation_info": corr_info
        }, 200

    instrument = PAIR_CONFIG[pair]["instrument"]
    open_trade = find_open_trade_for_instrument(instrument)

    if open_trade:
        if signal_matches_open_side(open_trade, signal):
            return {
                "status": "skipped",
                "reason": "same-direction trade already open"
            }, 200

        if ALLOW_REVERSE_SIGNAL_CLOSE:
            close_result, close_code = close_trade(open_trade["id"])
            if close_code >= 300:
                return {
                    "status": "rejected",
                    "reason": "failed to close opposite trade",
                    "close_result": close_result
                }, 400
        else:
            return {
                "status": "skipped",
                "reason": "opposite trade already open"
            }, 200

    if total_open_trades() >= MAX_OPEN_TRADES:
        return {
            "status": "skipped",
            "reason": "max open trades reached"
        }, 200

    result, status_code = place_oanda_market_order(signal, pair)

    if 200 <= status_code < 300:
        set_cooldown(pair, signal)
        STATE["trades_today"] += 1

    return {
        "status": "processed" if 200 <= status_code < 300 else "rejected",
        "pair": pair,
        "signal": signal,
        "session_info": session_info,
        "daily_info": daily_info,
        "trades_info": trades_info,
        "spread_info": spread_info,
        "volatility_info": vol_info,
        "trend_info": trend_info,
        "correlation_info": corr_info,
        "result": result
    }, status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
