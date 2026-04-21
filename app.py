import os
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# =========================
# ENV HELPERS
# =========================
def env_str(name, default=""):
    return os.getenv(name, default).strip()


def env_int(name, default=0):
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def env_float(name, default=0.0):
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def env_bool(name, default=False):
    v = os.getenv(name, str(default)).strip().lower()
    return v in ["1", "true", "yes", "on"]


# =========================
# ENV CONFIG
# =========================
OANDA_API_KEY = env_str("OANDA_API_KEY")
OANDA_ACCOUNT_ID = env_str("OANDA_ACCOUNT_ID")
OANDA_ENV = env_str("OANDA_ENV", "practice").lower()
WEBHOOK_PASSPHRASE = env_str("WEBHOOK_PASSPHRASE", "1234")

PAIR_LIST = [p.strip().upper() for p in env_str("PAIRS", "EUR_USD,GBP_USD,USD_JPY").split(",") if p.strip()]

RISK_PERCENT = env_float("RISK_PERCENT", 0.02)
FIXED_UNITS = env_int("FIXED_UNITS", 5000)
FALLBACK_UNITS = env_int("FALLBACK_UNITS", 100)

STOP_LOSS_PIPS = env_float("STOP_LOSS_PIPS", 20)
TAKE_PROFIT_PIPS = env_float("TAKE_PROFIT_PIPS", 80)
MAX_SPREAD_PIPS = env_float("MAX_SPREAD_PIPS", 12)

ENABLE_SPREAD_FILTER = env_bool("ENABLE_SPREAD_FILTER", True)
ENABLE_TREND_FILTER = env_bool("ENABLE_TREND_FILTER", False)
ENABLE_MOMENTUM_FILTER = env_bool("ENABLE_MOMENTUM_FILTER", False)
ENABLE_VOLATILITY_FILTER = env_bool("ENABLE_VOLATILITY_FILTER", False)
ENABLE_SESSION_FILTER = env_bool("ENABLE_SESSION_FILTER", False)
ENABLE_TRAILING = env_bool("ENABLE_TRAILING", True)
ENABLE_V21_MANAGER = env_bool("ENABLE_V21_MANAGER", True)
ENABLE_DAILY_LOSS_LIMIT = env_bool("ENABLE_DAILY_LOSS_LIMIT", True)
ALLOW_MULTIPAIR = env_bool("ALLOW_MULTIPAIR", True)

FAST_EMA_PERIOD = env_int("FAST_EMA_PERIOD", 20)
SLOW_EMA_PERIOD = env_int("SLOW_EMA_PERIOD", 20)
FAST_MA_PERIOD = env_int("FAST_MA_PERIOD", 20)
SLOW_MA_PERIOD = env_int("SLOW_MA_PERIOD", 50)
EMA_PERIOD = env_int("EMA_PERIOD", 20)

BREAK_EVEN_TRIGGER_PIPS = env_float("BREAK_EVEN_TRIGGER_PIPS", 15)
BREAK_EVEN_PLUS_PIPS = env_float("BREAK_EVEN_PLUS_PIPS", 2)

LOCK_1_TRIGGER_PIPS = env_float("LOCK_1_TRIGGER_PIPS", 18)
LOCK_2_TRIGGER_PIPS = env_float("LOCK_2_TRIGGER_PIPS", 30)
LOCK_3_TRIGGER_PIPS = env_float("LOCK_3_TRIGGER_PIPS", 45)

TRAILING_TRIGGER_PIPS = env_float("TRAILING_TRIGGER_PIPS", 22)
TRAILING_DISTANCE_PIPS = env_float("TRAILING_DISTANCE_PIPS", 12)

BUY_PULLBACK_PIPS = env_float("BUY_PULLBACK_PIPS", 1.0)
SELL_BOUNCE_PIPS = env_float("SELL_BOUNCE_PIPS", 1.0)
PULLBACK_PIPS = env_float("PULLBACK_PIPS", 0.5)
BOUNCE_PIPS = env_float("BOUNCE_PIPS", 0.8)
PULLBACK_DEPTH_PIPS = env_float("PULLBACK_DEPTH_PIPS", 5)

BREAKOUT_LOOKBACK = env_int("BREAKOUT_LOOKBACK", 3)
MOMENTUM_LOOKBACK = env_int("MOMENTUM_LOOKBACK", 3)
MOMENTUM_CANDLES = env_int("MOMENTUM_CANDLES", 2)
MOMENTUM_MIN_BODY_PIPS = env_float("MOMENTUM_MIN_BODY_PIPS", 1.2)
CONFIRMATION_CANDLES = env_int("CONFIRMATION_CANDLES", 2)

MIN_CANDLE_RANGE_PIPS = env_float("MIN_CANDLE_RANGE_PIPS", 1)
MAX_CANDLE_RANGE_PIPS = env_float("MAX_CANDLE_RANGE_PIPS", 35)
MIN_VOLATILITY_PIPS = env_float("MIN_VOLATILITY_PIPS", 1.5)

MIN_TREND_GAP_PIPS = env_float("MIN_TREND_GAP_PIPS", 1)
STRONG_TREND_GAP_PIPS = env_float("STRONG_TREND_GAP_PIPS", 10)
TREND_STRENGTH_MIN = env_float("TREND_STRENGTH_MIN", 15)

MAX_DAILY_LOSS_PERCENT = env_float("MAX_DAILY_LOSS_PERCENT", 3)
MAX_OPEN_TRADES = env_int("MAX_OPEN_TRADES", 6)
MAX_TOTAL_OPEN_TRADES = env_int("MAX_TOTAL_OPEN_TRADES", 6)
MIN_SECONDS_BETWEEN_TRADES = env_int("MIN_SECONDS_BETWEEN_TRADES", 20)
TRADE_COOLDOWN = env_int("TRADE_COOLDOWN", 60)

POLL_SECONDS = env_int("POLL_SECONDS", 5)
AUTO_CHECK_SECONDS = env_int("AUTO_CHECK_SECONDS", 10)
MONITOR_INTERVAL = env_int("MONITOR_INTERVAL", 10)

TIMEZONE_NAME = env_str("TIMEZONE_NAME", "America/New_York")
LONDON_START = env_int("LONDON_START", 3)
LONDON_END = env_int("LONDON_END", 11)
NY_START = env_int("NY_START", 8)
NY_END = env_int("NY_END", 11)

DEFAULT_SL_PIPS = env_float("DEFAULT_SL_PIPS", STOP_LOSS_PIPS)
DEFAULT_TP_PIPS = env_float("DEFAULT_TP_PIPS", TAKE_PROFIT_PIPS)

if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com"
else:
    BASE_URL = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}


# =========================
# GLOBAL STATE
# =========================
last_trade_time = {}
daily_start_balance = None
daily_balance_date = None
manager_started = False


# =========================
# SMALL HELPERS
# =========================
def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def now_est():
    return datetime.now(ZoneInfo(TIMEZONE_NAME))


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def price_to_pips(pair, price_diff):
    return price_diff / pip_size(pair)


def pips_to_price(pair, pips):
    return pips * pip_size(pair)


def log_filter(pair, side, result, reason):
    logging.info(f"FILTER | pair={pair} side={side} result={result} reason={reason}")


# =========================
# OANDA API
# =========================
def get_account_summary():
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def get_balance():
    data = get_account_summary()
    return float(data["account"]["balance"])


def get_nav():
    data = get_account_summary()
    account = data["account"]
    return float(account.get("NAV", account.get("balance", 0)))


def get_open_trades():
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json().get("trades", [])


def get_prices(pair):
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=HEADERS, params={"instruments": pair}, timeout=20)
    r.raise_for_status()
    prices = r.json()
    return prices.get("prices", [])


def get_bid_ask(pair):
    prices = get_prices(pair)
    if not prices:
        raise ValueError(f"No price returned for {pair}")

    p = prices[0]
    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])
    return bid, ask


def get_spread_pips(pair):
    bid, ask = get_bid_ask(pair)
    return price_to_pips(pair, ask - bid)


def get_candles(pair, count=60, granularity="M2"):
    url = f"{BASE_URL}/v3/instruments/{pair}/candles"
    params = {
        "count": count,
        "price": "M",
        "granularity": granularity,
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    candles = r.json().get("candles", [])
    cleaned = []

    for c in candles:
        if not c.get("complete", False):
            continue
        mid = c["mid"]
        cleaned.append({
            "time": c["time"],
            "open": float(mid["o"]),
            "high": float(mid["h"]),
            "low": float(mid["l"]),
            "close": float(mid["c"]),
        })

    return cleaned


def close_trade(trade_id):
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close"
    r = requests.put(url, headers=HEADERS, json={}, timeout=20)
    r.raise_for_status()
    return r.json()


def replace_trade_sl(trade_id, price):
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders"
    payload = {
        "stopLoss": {
            "price": f"{price:.5f}" if price < 20 else f"{price:.3f}",
            "timeInForce": "GTC"
        }
    }
    r = requests.put(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


# =========================
# ACCOUNT RISK / LIMITS
# =========================
def reset_daily_balance_if_needed():
    global daily_start_balance, daily_balance_date
    today = now_est().date()
    if daily_balance_date != today:
        daily_balance_date = today
        daily_start_balance = get_balance()


def daily_loss_limit_hit():
    if not ENABLE_DAILY_LOSS_LIMIT:
        return False

    reset_daily_balance_if_needed()
    bal = get_balance()

    if not daily_start_balance:
        return False

    dd = ((daily_start_balance - bal) / daily_start_balance) * 100
    logging.info(f"DAILY LOSS CHECK | start={daily_start_balance:.2f} current={bal:.2f} dd={dd:.2f}%")
    return dd >= MAX_DAILY_LOSS_PERCENT


def count_open_trades(pair=None):
    trades = get_open_trades()
    if pair:
        return sum(1 for t in trades if t["instrument"] == pair)
    return len(trades)


def can_trade_pair(pair):
    if not ALLOW_MULTIPAIR and count_open_trades() > 0:
        log_filter(pair, "ANY", False, "multipair disabled")
        return False

    if count_open_trades() >= MAX_TOTAL_OPEN_TRADES:
        log_filter(pair, "ANY", False, "max total open trades")
        return False

    if count_open_trades(pair) >= MAX_OPEN_TRADES:
        log_filter(pair, "ANY", False, "max pair open trades")
        return False

    last_ts = last_trade_time.get(pair, 0)
    if time.time() - last_ts < MIN_SECONDS_BETWEEN_TRADES:
        log_filter(pair, "ANY", False, "cooldown active")
        return False

    if daily_loss_limit_hit():
        log_filter(pair, "ANY", False, "daily loss limit hit")
        return False

    return True


# =========================
# SIGNAL FILTERS
# =========================
def in_session():
    if not ENABLE_SESSION_FILTER:
        return True

    h = now_est().hour
    london = LONDON_START <= h <= LONDON_END
    ny = NY_START <= h <= NY_END
    return london or ny


def candle_range_pips(pair, candle):
    return price_to_pips(pair, candle["high"] - candle["low"])


def candle_body_pips(pair, candle):
    return price_to_pips(pair, abs(candle["close"] - candle["open"]))


def simple_sma(values, period):
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def trend_ok(pair, side, candles):
    if not ENABLE_TREND_FILTER:
        log_filter(pair, side, True, "trend filter off")
        return True

    closes = [c["close"] for c in candles]
    fast = simple_sma(closes, FAST_MA_PERIOD)
    slow = simple_sma(closes, SLOW_MA_PERIOD)

    if fast is None or slow is None:
        log_filter(pair, side, False, "not enough candles for trend")
        return False

    gap_pips = price_to_pips(pair, abs(fast - slow))

    if gap_pips < MIN_TREND_GAP_PIPS:
        log_filter(pair, side, False, f"trend gap too small={gap_pips:.1f}")
        return False

    if side == "BUY" and fast <= slow:
        log_filter(pair, side, False, "buy trend fail")
        return False

    if side == "SELL" and fast >= slow:
        log_filter(pair, side, False, "sell trend fail")
        return False

    log_filter(pair, side, True, f"trend pass gap={gap_pips:.1f}")
    return True


def momentum_ok(pair, side, candles):
    if not ENABLE_MOMENTUM_FILTER:
        log_filter(pair, side, True, "momentum filter off")
        return True

    recent = candles[-MOMENTUM_CANDLES:]
    if len(recent) < MOMENTUM_CANDLES:
        log_filter(pair, side, False, "not enough candles for momentum")
        return False

    for c in recent:
        body = candle_body_pips(pair, c)
        if body < MOMENTUM_MIN_BODY_PIPS:
            log_filter(pair, side, False, f"body too small={body:.1f}")
            return False

        if side == "BUY" and c["close"] <= c["open"]:
            log_filter(pair, side, False, "buy momentum candle not bullish")
            return False

        if side == "SELL" and c["close"] >= c["open"]:
            log_filter(pair, side, False, "sell momentum candle not bearish")
            return False

    log_filter(pair, side, True, "momentum pass")
    return True


def volatility_ok(pair, side, candles):
    if not ENABLE_VOLATILITY_FILTER:
        log_filter(pair, side, True, "volatility filter off")
        return True

    recent = candles[-BREAKOUT_LOOKBACK:]
    if len(recent) < BREAKOUT_LOOKBACK:
        log_filter(pair, side, False, "not enough candles for volatility")
        return False

    avg_range = sum(candle_range_pips(pair, c) for c in recent) / len(recent)
    if avg_range < MIN_VOLATILITY_PIPS:
        log_filter(pair, side, False, f"volatility too low={avg_range:.1f}")
        return False

    log_filter(pair, side, True, f"volatility pass={avg_range:.1f}")
    return True


def candle_range_ok(pair, side, candles):
    if not candles:
        log_filter(pair, side, False, "no candles")
        return False

    last = candles[-1]
    rng = candle_range_pips(pair, last)

    if rng < MIN_CANDLE_RANGE_PIPS:
        log_filter(pair, side, False, f"candle range too small={rng:.1f}")
        return False

    if rng > MAX_CANDLE_RANGE_PIPS:
        log_filter(pair, side, False, f"candle range too large={rng:.1f}")
        return False

    log_filter(pair, side, True, f"candle range pass={rng:.1f}")
    return True


def spread_ok(pair, side):
    if not ENABLE_SPREAD_FILTER:
        log_filter(pair, side, True, "spread filter off")
        return True

    spread = get_spread_pips(pair)
    if spread > MAX_SPREAD_PIPS:
        log_filter(pair, side, False, f"spread too high={spread:.1f}")
        return False

    log_filter(pair, side, True, f"spread pass spread={spread:.1f}")
    return True


def breakout_pullback_ok(pair, side, candles):
    if len(candles) < BREAKOUT_LOOKBACK + 2:
        log_filter(pair, side, False, "not enough candles for structure")
        return False

    lookback = candles[-(BREAKOUT_LOOKBACK + 1):-1]
    last = candles[-1]
    ph = max(c["high"] for c in lookback)
    pl = min(c["low"] for c in lookback)

    if side == "BUY":
        pullback = price_to_pips(pair, ph - last["close"])
        if last["close"] >= ph or pullback <= BUY_PULLBACK_PIPS:
            log_filter(pair, side, True, f"buy structure pass ph={ph:.5f} pullback={pullback:.1f}")
            return True
        log_filter(pair, side, False, f"buy pullback fail ph={ph:.5f} pullback={pullback:.1f}")
        return False

    bounce = price_to_pips(pair, last["close"] - pl)
    if last["close"] <= pl or bounce <= SELL_BOUNCE_PIPS:
        log_filter(pair, side, True, f"sell structure pass pl={pl:.5f} bounce={bounce:.1f}")
        return True
    log_filter(pair, side, False, f"sell bounce fail pl={pl:.5f} bounce={bounce:.1f}")
    return False


def all_filters_pass(pair, side):
    if pair not in PAIR_LIST:
        log_filter(pair, side, False, "pair not enabled")
        return False, "pair not enabled"

    if not in_session():
        log_filter(pair, side, False, "session blocked")
        return False, "session blocked"

    if not can_trade_pair(pair):
        return False, "trade limits blocked"

    candles = get_candles(pair, count=max(80, SLOW_MA_PERIOD + 5), granularity="M2")

    checks = [
        spread_ok(pair, side),
        candle_range_ok(pair, side, candles),
        trend_ok(pair, side, candles),
        momentum_ok(pair, side, candles),
        volatility_ok(pair, side, candles),
        breakout_pullback_ok(pair, side, candles),
    ]

    if all(checks):
        return True, "all filters passed"

    return False, "one or more filters failed"


# =========================
# ORDER LOGIC
# =========================
def compute_units(pair):
    # For now keep it simple and stable
    if FIXED_UNITS > 0:
        return FIXED_UNITS
    if FALLBACK_UNITS > 0:
        return FALLBACK_UNITS
    return 100


def place_market_order(pair, side, units=None, sl_pips=None, tp_pips=None):
    side = side.upper().strip()

    if side not in ["BUY", "SELL"]:
        raise ValueError("side must be BUY or SELL")

    if units is None:
        units = compute_units(pair)

    sl_pips = safe_float(sl_pips, DEFAULT_SL_PIPS)
    tp_pips = safe_float(tp_pips, DEFAULT_TP_PIPS)

    bid, ask = get_bid_ask(pair)
    entry = ask if side == "BUY" else bid
    pip = pip_size(pair)

    if side == "BUY":
        sl_price = entry - (sl_pips * pip)
        tp_price = entry + (tp_pips * pip)
        signed_units = units
    else:
        sl_price = entry + (sl_pips * pip)
        tp_price = entry - (tp_pips * pip)
        signed_units = -units

    price_fmt = "{:.3f}" if "JPY" in pair else "{:.5f}"

    payload = {
        "order": {
            "instrument": pair,
            "units": str(signed_units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": price_fmt.format(sl_price),
                "timeInForce": "GTC",
                "triggerMode": "TOP_OF_BOOK",
            },
            "takeProfitOnFill": {
                "price": price_fmt.format(tp_price),
                "timeInForce": "GTC",
            },
        }
    }

    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()

    logging.info(f"TRADE: {pair} {side}")
    logging.info(data)

    last_trade_time[pair] = time.time()
    return data


# =========================
# TRADE MANAGER
# =========================
def unrealized_pips(trade, bid, ask):
    pair = trade["instrument"]
    entry = float(trade["price"])
    current = bid if float(trade["currentUnits"]) > 0 else ask
    diff = current - entry if float(trade["currentUnits"]) > 0 else entry - current
    return price_to_pips(pair, diff)


def manage_trade(trade):
    pair = trade["instrument"]
    trade_id = trade["id"]
    units = float(trade["currentUnits"])
    entry = float(trade["price"])
    pip = pip_size(pair)

    bid, ask = get_bid_ask(pair)
    upips = unrealized_pips(trade, bid, ask)

    desired_sl = None

    # break even
    if upips >= BREAK_EVEN_TRIGGER_PIPS:
        if units > 0:
            desired_sl = entry + (BREAK_EVEN_PLUS_PIPS * pip)
        else:
            desired_sl = entry - (BREAK_EVEN_PLUS_PIPS * pip)

    # lock levels
    if upips >= LOCK_1_TRIGGER_PIPS:
        if units > 0:
            desired_sl = max(desired_sl or -999999, entry + (5 * pip))
        else:
            desired_sl = min(desired_sl or 999999, entry - (5 * pip))

    if upips >= LOCK_2_TRIGGER_PIPS:
        if units > 0:
            desired_sl = max(desired_sl or -999999, entry + (12 * pip))
        else:
            desired_sl = min(desired_sl or 999999, entry - (12 * pip))

    if upips >= LOCK_3_TRIGGER_PIPS:
        if units > 0:
            desired_sl = max(desired_sl or -999999, entry + (20 * pip))
        else:
            desired_sl = min(desired_sl or 999999, entry - (20 * pip))

    # trailing
    if ENABLE_TRAILING and upips >= TRAILING_TRIGGER_PIPS:
        if units > 0:
            trailing_sl = bid - (TRAILING_DISTANCE_PIPS * pip)
            desired_sl = max(desired_sl or -999999, trailing_sl)
        else:
            trailing_sl = ask + (TRAILING_DISTANCE_PIPS * pip)
            desired_sl = min(desired_sl or 999999, trailing_sl)

    if desired_sl is None:
        return

    current_sl = None
    sl_obj = trade.get("stopLossOrder")
    if sl_obj and sl_obj.get("price"):
        current_sl = float(sl_obj["price"])

    should_update = False
    if current_sl is None:
        should_update = True
    elif units > 0 and desired_sl > current_sl:
        should_update = True
    elif units < 0 and desired_sl < current_sl:
        should_update = True

    if should_update:
        logging.info(f"MANAGER | updating SL | trade={trade_id} pair={pair} upips={upips:.1f} new_sl={desired_sl}")
        try:
            replace_trade_sl(trade_id, desired_sl)
        except Exception as e:
            logging.warning(f"MANAGER | failed to update SL for {trade_id}: {e}")


def monitor_loop():
    while True:
        try:
            if ENABLE_V21_MANAGER:
                trades = get_open_trades()
                for trade in trades:
                    manage_trade(trade)
        except Exception as e:
            logging.warning(f"MONITOR LOOP ERROR: {e}")

        time.sleep(max(3, MONITOR_INTERVAL))


def start_manager_once():
    global manager_started
    if manager_started:
        return
    manager_started = True
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    logging.info("Trade manager started")


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "message": "bot is live"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "env": OANDA_ENV,
        "pairs": PAIR_LIST,
        "manager": ENABLE_V21_MANAGER,
    }), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}

        passphrase = str(data.get("passphrase", "")).strip()
        if passphrase != WEBHOOK_PASSPHRASE:
            return jsonify({"ok": False, "error": "bad passphrase"}), 403

        pair = str(data.get("pair", "")).strip().upper()
        side = str(data.get("side", "")).strip().upper()

        if not pair or side not in ["BUY", "SELL"]:
            return jsonify({"ok": False, "error": "pair/side invalid"}), 400

        sl = safe_float(data.get("sl", DEFAULT_SL_PIPS), DEFAULT_SL_PIPS)
        tp = safe_float(data.get("tp", DEFAULT_TP_PIPS), DEFAULT_TP_PIPS)
        risk = safe_float(data.get("risk", RISK_PERCENT), RISK_PERCENT)

        logging.info(
            f"WEBHOOK RECEIVED | pair={pair} side={side} risk={risk} sl={sl} tp={tp} trailing={ENABLE_TRAILING}"
        )

        allowed, reason = all_filters_pass(pair, side)
        if not allowed:
            return jsonify({"ok": False, "reason": reason}), 200

        result = place_market_order(pair=pair, side=side, units=FIXED_UNITS, sl_pips=sl, tp_pips=tp)
        return jsonify({"ok": True, "result": result}), 200

    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            pass
        logging.exception("HTTP ERROR")
        return jsonify({"ok": False, "error": str(e), "body": body}), 500

    except Exception as e:
        logging.exception("WEBHOOK ERROR")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/close_all", methods=["POST"])
def close_all():
    try:
        data = request.get_json(force=True, silent=True) or {}
        passphrase = str(data.get("passphrase", "")).strip()
        if passphrase != WEBHOOK_PASSPHRASE:
            return jsonify({"ok": False, "error": "bad passphrase"}), 403

        results = []
        for trade in get_open_trades():
            try:
                results.append(close_trade(trade["id"]))
            except Exception as e:
                results.append({"trade_id": trade["id"], "error": str(e)})

        return jsonify({"ok": True, "closed": results}), 200

    except Exception as e:
        logging.exception("CLOSE ALL ERROR")
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================
# STARTUP
# =========================
start_manager_once()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
