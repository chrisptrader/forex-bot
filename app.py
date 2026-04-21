import os
import time
import math
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

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
# CONFIG
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
DEFAULT_SL_PIPS = env_float("DEFAULT_SL_PIPS", STOP_LOSS_PIPS)
DEFAULT_TP_PIPS = env_float("DEFAULT_TP_PIPS", TAKE_PROFIT_PIPS)

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

if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com"
else:
    BASE_URL = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

REQUEST_TIMEOUT = 5

last_trade_time = {}
daily_start_balance = None
daily_balance_date = None
manager_started = False
manager_lock = threading.Lock()


# =========================
# BASIC HELPERS
# =========================
def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def now_est():
    return datetime.now(ZoneInfo(TIMEZONE_NAME))


def in_session():
    if not ENABLE_SESSION_FILTER:
        return True
    h = now_est().hour
    london = LONDON_START <= h <= LONDON_END
    ny = NY_START <= h <= NY_END
    return london or ny


def safe_json(response):
    try:
        return response.json()
    except Exception:
        return {}


# =========================
# OANDA API
# =========================
def get_account_summary():
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_balance():
    data = get_account_summary()
    return float(data["account"]["balance"])


def reset_daily_balance_if_needed():
    global daily_start_balance, daily_balance_date
    today = now_est().date()
    if daily_balance_date != today:
        daily_balance_date = today
        try:
            daily_start_balance = get_balance()
        except Exception as e:
            logging.warning(f"Could not reset daily balance: {e}")


def daily_loss_limit_hit():
    if not ENABLE_DAILY_LOSS_LIMIT:
        return False

    reset_daily_balance_if_needed()

    if daily_start_balance in (None, 0):
        return False

    try:
        bal = get_balance()
        dd = ((daily_start_balance - bal) / daily_start_balance) * 100
        return dd >= MAX_DAILY_LOSS_PERCENT
    except Exception as e:
        logging.warning(f"Daily loss check failed: {e}")
        return False


def get_open_trades():
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json().get("trades", [])


def count_open_trades(pair=None):
    try:
        trades = get_open_trades()
        if pair:
            return sum(1 for t in trades if t.get("instrument") == pair)
        return len(trades)
    except Exception as e:
        logging.warning(f"count_open_trades failed: {e}")
        return 999999


def get_prices(pair):
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=HEADERS, params={"instruments": pair}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    prices = data.get("prices", [])
    if not prices:
        raise Exception(f"No prices returned for {pair}")
    return prices[0]


def get_bid_ask(pair):
    p = get_prices(pair)
    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])
    return bid, ask


def get_spread_pips(pair):
    bid, ask = get_bid_ask(pair)
    return (ask - bid) / pip_size(pair)


def get_candles(pair, count=60, granularity="M1"):
    url = f"{BASE_URL}/v3/instruments/{pair}/candles"
    params = {
        "count": count,
        "granularity": granularity,
        "price": "M"
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    candles = r.json().get("candles", [])
    return [c for c in candles if c.get("complete")]


def price_from_candle(c):
    return float(c["mid"]["c"])


def high_from_candle(c):
    return float(c["mid"]["h"])


def low_from_candle(c):
    return float(c["mid"]["l"])


def open_from_candle(c):
    return float(c["mid"]["o"])


def body_pips(c, pair):
    return abs(price_from_candle(c) - open_from_candle(c)) / pip_size(pair)


def range_pips(c, pair):
    return abs(high_from_candle(c) - low_from_candle(c)) / pip_size(pair)


def sma(values, period):
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema(values, period):
    if len(values) < period or period <= 0:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = (v * k) + (e * (1 - k))
    return e


def get_trade_price_now(pair, side):
    bid, ask = get_bid_ask(pair)
    return ask if side == "buy" else bid


def units_for_pair(pair):
    return FIXED_UNITS if FIXED_UNITS > 0 else FALLBACK_UNITS


def trade_exists_on_pair(pair):
    try:
        return count_open_trades(pair) > 0
    except Exception:
        return True


def can_trade_pair(pair):
    now_ts = time.time()

    if daily_loss_limit_hit():
        return False, "daily loss limit hit"

    if not in_session():
        return False, "outside session"

    last_ts = last_trade_time.get(pair, 0)
    if now_ts - last_ts < max(MIN_SECONDS_BETWEEN_TRADES, TRADE_COOLDOWN):
        return False, "pair cooldown active"

    total_open = count_open_trades()
    if total_open >= MAX_TOTAL_OPEN_TRADES:
        return False, "max total open trades reached"

    pair_open = count_open_trades(pair)
    if pair_open >= MAX_OPEN_TRADES:
        return False, "max open trades on pair reached"

    if not ALLOW_MULTIPAIR:
        if total_open > 0 and pair_open == 0:
            return False, "multipair disabled"

    return True, "ok"


def place_market_order(pair, side, units, sl_pips, tp_pips):
    price_now = get_trade_price_now(pair, side)
    ps = pip_size(pair)

    if side == "buy":
        sl_price = round(price_now - (sl_pips * ps), 5 if ps == 0.0001 else 3)
        tp_price = round(price_now + (tp_pips * ps), 5 if ps == 0.0001 else 3)
        order_units = abs(units)
    else:
        sl_price = round(price_now + (sl_pips * ps), 5 if ps == 0.0001 else 3)
        tp_price = round(price_now - (tp_pips * ps), 5 if ps == 0.0001 else 3)
        order_units = -abs(units)

    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    payload = {
        "order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(order_units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "takeProfitOnFill": {
                "price": str(tp_price),
                "timeInForce": "GTC"
            },
            "stopLossOnFill": {
                "price": str(sl_price),
                "timeInForce": "GTC",
                "triggerMode": "TOP_OF_BOOK"
            }
        }
    }

    r = requests.post(url, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def replace_trade_sl(trade_id, new_sl_price):
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders"
    payload = {
        "stopLoss": {
            "timeInForce": "GTC",
            "price": str(new_sl_price),
            "triggerMode": "TOP_OF_BOOK"
        }
    }
    r = requests.put(url, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


# =========================
# FILTERS
# =========================
def trend_filter_pass(pair, side):
    if not ENABLE_TREND_FILTER:
        return True, "trend filter off"

    candles = get_candles(pair, count=max(SLOW_MA_PERIOD + 5, SLOW_EMA_PERIOD + 5, 60))
    closes = [price_from_candle(c) for c in candles]

    fast_val = ema(closes, FAST_EMA_PERIOD) if FAST_EMA_PERIOD > 0 else None
    slow_val = ema(closes, SLOW_EMA_PERIOD) if SLOW_EMA_PERIOD > 0 else None

    if fast_val is None or slow_val is None:
        return False, "trend data insufficient"

    gap_pips = abs(fast_val - slow_val) / pip_size(pair)
    if gap_pips < MIN_TREND_GAP_PIPS:
        return False, "trend gap too weak"

    if side == "buy" and fast_val <= slow_val:
        return False, "trend not bullish"
    if side == "sell" and fast_val >= slow_val:
        return False, "trend not bearish"

    return True, "trend ok"


def momentum_filter_pass(pair, side):
    if not ENABLE_MOMENTUM_FILTER:
        return True, "momentum filter off"

    candles = get_candles(pair, count=max(MOMENTUM_CANDLES + 3, 10))
    check = candles[-MOMENTUM_CANDLES:]

    for c in check:
        o = open_from_candle(c)
        cl = price_from_candle(c)
        bp = body_pips(c, pair)

        if bp < MOMENTUM_MIN_BODY_PIPS:
            return False, "momentum body too small"

        if side == "buy" and cl <= o:
            return False, "not bullish momentum"
        if side == "sell" and cl >= o:
            return False, "not bearish momentum"

    return True, "momentum ok"


def volatility_filter_pass(pair):
    if not ENABLE_VOLATILITY_FILTER:
        return True, "volatility filter off"

    candles = get_candles(pair, count=10)
    if not candles:
        return False, "no candles"

    rp = range_pips(candles[-1], pair)

    if rp < MIN_VOLATILITY_PIPS:
        return False, "volatility too low"
    if rp > MAX_CANDLE_RANGE_PIPS:
        return False, "candle too large"

    return True, "volatility ok"


def spread_filter_pass(pair):
    if not ENABLE_SPREAD_FILTER:
        return True, "spread filter off"

    sp = get_spread_pips(pair)
    if sp > MAX_SPREAD_PIPS:
        return False, f"spread too high ({sp:.2f})"

    return True, "spread ok"


def all_filters_pass(pair, side):
    ok, msg = spread_filter_pass(pair)
    if not ok:
        return False, msg

    ok, msg = trend_filter_pass(pair, side)
    if not ok:
        return False, msg

    ok, msg = momentum_filter_pass(pair, side)
    if not ok:
        return False, msg

    ok, msg = volatility_filter_pass(pair)
    if not ok:
        return False, msg

    ok, msg = can_trade_pair(pair)
    if not ok:
        return False, msg

    return True, "ok"


# =========================
# TRADE MANAGEMENT
# =========================
def trade_side(trade):
    units = float(trade.get("currentUnits", trade.get("initialUnits", trade.get("units", 0))))
    return "buy" if units > 0 else "sell"


def trade_units(trade):
    return abs(float(trade.get("currentUnits", trade.get("initialUnits", trade.get("units", 0)))))


def trade_entry_price(trade):
    return float(trade["price"])


def trade_unrealized_pl(trade):
    return float(trade.get("unrealizedPL", 0))


def get_trade_pips_profit(trade):
    pair = trade["instrument"]
    side = trade_side(trade)
    entry = trade_entry_price(trade)
    current = get_trade_price_now(pair, side)
    ps = pip_size(pair)

    if side == "buy":
        return (current - entry) / ps
    return (entry - current) / ps


def current_sl_price(trade):
    sl = trade.get("stopLossOrder")
    if not sl:
        return None
    try:
        return float(sl["price"])
    except Exception:
        return None


def round_price_for_pair(pair, price):
    return round(price, 3 if "JPY" in pair else 5)


def better_sl(side, current_sl, new_sl):
    if current_sl is None:
        return True
    if side == "buy":
        return new_sl > current_sl
    return new_sl < current_sl


def maybe_move_stop(trade):
    pair = trade["instrument"]
    side = trade_side(trade)
    entry = trade_entry_price(trade)
    trade_id = trade["id"]
    ps = pip_size(pair)
    pips_profit = get_trade_pips_profit(trade)
    current_sl = current_sl_price(trade)

    target_sl = None

    if pips_profit >= LOCK_3_TRIGGER_PIPS:
        if side == "buy":
            target_sl = entry + (LOCK_2_TRIGGER_PIPS * ps)
        else:
            target_sl = entry - (LOCK_2_TRIGGER_PIPS * ps)

    elif pips_profit >= LOCK_2_TRIGGER_PIPS:
        if side == "buy":
            target_sl = entry + (LOCK_1_TRIGGER_PIPS * ps)
        else:
            target_sl = entry - (LOCK_1_TRIGGER_PIPS * ps)

    elif pips_profit >= LOCK_1_TRIGGER_PIPS:
        if side == "buy":
            target_sl = entry + (BREAK_EVEN_PLUS_PIPS * ps)
        else:
            target_sl = entry - (BREAK_EVEN_PLUS_PIPS * ps)

    elif pips_profit >= BREAK_EVEN_TRIGGER_PIPS:
        if side == "buy":
            target_sl = entry + (BREAK_EVEN_PLUS_PIPS * ps)
        else:
            target_sl = entry - (BREAK_EVEN_PLUS_PIPS * ps)

    if ENABLE_TRAILING and pips_profit >= TRAILING_TRIGGER_PIPS:
        current_price = get_trade_price_now(pair, side)
        if side == "buy":
            trailing_sl = current_price - (TRAILING_DISTANCE_PIPS * ps)
        else:
            trailing_sl = current_price + (TRAILING_DISTANCE_PIPS * ps)

        if target_sl is None:
            target_sl = trailing_sl
        else:
            if side == "buy":
                target_sl = max(target_sl, trailing_sl)
            else:
                target_sl = min(target_sl, trailing_sl)

    if target_sl is None:
        return

    target_sl = round_price_for_pair(pair, target_sl)

    if not better_sl(side, current_sl, target_sl):
        return

    try:
        replace_trade_sl(trade_id, target_sl)
        logging.info(f"SL moved | pair={pair} trade_id={trade_id} side={side} new_sl={target_sl}")
    except Exception as e:
        logging.warning(f"Failed SL move | pair={pair} trade_id={trade_id} error={e}")


def manager_loop():
    logging.info("Trade manager started")
    while True:
        try:
            if ENABLE_V21_MANAGER or ENABLE_TRAILING:
                trades = get_open_trades()
                for trade in trades:
                    try:
                        maybe_move_stop(trade)
                    except Exception as trade_err:
                        logging.warning(f"Manager trade error: {trade_err}")
        except Exception as e:
            logging.warning(f"Manager loop error: {e}")

        time.sleep(max(5, MONITOR_INTERVAL))


def start_manager_once():
    global manager_started
    with manager_lock:
        if manager_started:
            return
        t = threading.Thread(target=manager_loop, daemon=True)
        t.start()
        manager_started = True


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "service": "forex-bot",
        "env": OANDA_ENV,
        "pairs": PAIR_LIST
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/webhook", methods=["POST"])
def webhook():
    start_manager_once()

    try:
        data = request.get_json(force=True, silent=True) or {}

        passphrase = str(data.get("passphrase", "")).strip()
        if WEBHOOK_PASSPHRASE and passphrase != WEBHOOK_PASSPHRASE:
            return jsonify({"ok": False, "reason": "bad passphrase"}), 401

        pair = str(data.get("pair", data.get("symbol", ""))).upper().strip()
        side = str(data.get("side", data.get("action", ""))).lower().strip()

        if pair not in PAIR_LIST:
            return jsonify({"ok": False, "reason": f"pair not allowed: {pair}"}), 200

        if side not in ["buy", "sell"]:
            return jsonify({"ok": False, "reason": "side must be buy or sell"}), 200

        sl_pips = float(data.get("sl", data.get("stop_loss_pips", DEFAULT_SL_PIPS)))
        tp_pips = float(data.get("tp", data.get("take_profit_pips", DEFAULT_TP_PIPS)))
        units = int(float(data.get("units", units_for_pair(pair))))

        logging.info(
            f"WEBHOOK RECEIVED | pair={pair} side={side} "
            f"risk={RISK_PERCENT} sl={sl_pips} tp={tp_pips} trailing={ENABLE_TRAILING}"
        )

        allowed, reason = all_filters_pass(pair, side)
        if not allowed:
            logging.info(f"TRADE BLOCKED | pair={pair} side={side} reason={reason}")
            return jsonify({"ok": False, "reason": reason}), 200

        result = place_market_order(pair, side, units, sl_pips, tp_pips)
        last_trade_time[pair] = time.time()

        logging.info(f"TRADE OPENED | pair={pair} side={side} units={units}")
        return jsonify({
            "ok": True,
            "pair": pair,
            "side": side,
            "units": units,
            "result": result
        }), 200

    except requests.exceptions.Timeout:
        logging.exception("Webhook timeout talking to OANDA")
        return jsonify({"ok": False, "reason": "oanda timeout"}), 200

    except requests.exceptions.RequestException as e:
        logging.exception(f"Webhook request error: {e}")
        return jsonify({"ok": False, "reason": f"request error: {str(e)}"}), 200

    except Exception as e:
        logging.exception(f"Webhook unexpected error: {e}")
        return jsonify({"ok": False, "reason": f"unexpected error: {str(e)}"}), 200


# =========================
# STARTUP
# =========================
start_manager_once()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
