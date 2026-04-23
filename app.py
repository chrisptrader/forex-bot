import os
import time
import threading
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234").strip()

BASE_URL = "https://api-fxpractice.oanda.com/v3" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com/v3"

PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "EUR_USD,GBP_USD,USD_JPY").split(",") if p.strip()]

FIXED_UNITS = int(os.getenv("FIXED_UNITS", "10000"))
MAX_UNITS = int(os.getenv("MAX_UNITS", "10000"))

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "1"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_TRADES_PER_PAIR", "1"))
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "900"))

STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "12"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "30"))
SPREAD_LIMIT_PIPS = float(os.getenv("SPREAD_LIMIT_PIPS", "2.5"))

USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true").lower() == "true"
BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "5"))
BREAK_EVEN_PLUS_PIPS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", "1"))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true").lower() == "true"
TRAILING_TRIGGER_PIPS = float(os.getenv("TRAILING_TRIGGER_PIPS", "8"))
TRAILING_DISTANCE_PIPS = float(os.getenv("TRAILING_DISTANCE_PIPS", "4"))

ALLOW_BUY = os.getenv("ALLOW_BUY", "true").lower() == "true"
ALLOW_SELL = os.getenv("ALLOW_SELL", "true").lower() == "true"

# Sniper filters
GRANULARITY = os.getenv("GRANULARITY", "M5")
EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "50"))
MIN_BODY_PIPS = float(os.getenv("MIN_BODY_PIPS", "3"))
MIN_IMPULSE_PIPS = float(os.getenv("MIN_IMPULSE_PIPS", "6"))
MAX_RANGE_PIPS = float(os.getenv("MAX_RANGE_PIPS", "18"))
LOOKBACK_CANDLES = int(os.getenv("LOOKBACK_CANDLES", "12"))

MANAGE_INTERVAL_SECONDS = int(os.getenv("MANAGE_INTERVAL_SECONDS", "10"))

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

lock = threading.Lock()
last_trade_time = {}


# =========================
# HELPERS
# =========================
def log(msg):
    print(msg, flush=True)


def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def fmt_price(pair, price):
    return f"{price:.3f}" if "JPY" in pair else f"{price:.5f}"


def oanda_get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=20)
    return r.json()


def oanda_post(path, payload):
    r = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=payload, timeout=20)
    return r.json()


def oanda_put(path, payload):
    r = requests.put(f"{BASE_URL}{path}", headers=HEADERS, json=payload, timeout=20)
    return r.json()


def get_open_trades():
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    return data.get("trades", [])


def total_open_trades():
    return len(get_open_trades())


def open_trades_for_pair(pair):
    return sum(1 for t in get_open_trades() if t.get("instrument") == pair)


def get_price(pair):
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/pricing", {"instruments": pair})
    prices = data.get("prices", [])
    if not prices:
        return None

    p = prices[0]
    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])

    return {
        "bid": bid,
        "ask": ask,
        "spread_pips": (ask - bid) / pip_size(pair),
    }


def get_candles(pair, count=80):
    data = oanda_get(
        f"/instruments/{pair}/candles",
        {
            "granularity": GRANULARITY,
            "count": count,
            "price": "M",
        },
    )

    candles = []
    for c in data.get("candles", []):
        if not c.get("complete"):
            continue

        mid = c["mid"]
        candles.append({
            "open": float(mid["o"]),
            "high": float(mid["h"]),
            "low": float(mid["l"]),
            "close": float(mid["c"]),
        })

    return candles


def ema(values, period):
    if len(values) < period:
        return None

    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period

    for price in values[period:]:
        ema_val = price * k + ema_val * (1 - k)

    return ema_val


def candle_body_pips(candle, pair):
    return abs(candle["close"] - candle["open"]) / pip_size(pair)


def candle_range_pips(candle, pair):
    return (candle["high"] - candle["low"]) / pip_size(pair)


def recent_range_pips(candles, pair, lookback):
    recent = candles[-lookback:]
    high = max(c["high"] for c in recent)
    low = min(c["low"] for c in recent)
    return (high - low) / pip_size(pair)


def sniper_filter(pair, action):
    candles = get_candles(pair, 80)

    if len(candles) < 55:
        return False, "Not enough candles"

    closes = [c["close"] for c in candles]
    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)

    if fast is None or slow is None:
        return False, "EMA not ready"

    last = candles[-1]
    prev = candles[-2]
    ps = pip_size(pair)

    body = candle_body_pips(last, pair)
    rng = recent_range_pips(candles, pair, LOOKBACK_CANDLES)

    if rng < MAX_RANGE_PIPS:
        return False, f"Blocked chop/range: {rng:.1f} pips"

    if body < MIN_BODY_PIPS:
        return False, f"Body too small: {body:.1f} pips"

    # Trend lock
    bullish = fast > slow
    bearish = fast < slow

    if action == "buy" and not bullish:
        return False, "Blocked buy: trend not bullish"

    if action == "sell" and not bearish:
        return False, "Blocked sell: trend not bearish"

    # Continuation candle check
    if action == "buy":
        impulse = (last["close"] - last["open"]) / ps
        broke_prev = last["close"] > prev["high"]

        if impulse < MIN_IMPULSE_PIPS:
            return False, f"Buy impulse too weak: {impulse:.1f} pips"

        if not broke_prev:
            return False, "Buy blocked: no breakout above previous candle"

    if action == "sell":
        impulse = (last["open"] - last["close"]) / ps
        broke_prev = last["close"] < prev["low"]

        if impulse < MIN_IMPULSE_PIPS:
            return False, f"Sell impulse too weak: {impulse:.1f} pips"

        if not broke_prev:
            return False, "Sell blocked: no breakdown below previous candle"

    return True, "Sniper filter passed"


def safe_units(action):
    units = min(abs(FIXED_UNITS), MAX_UNITS)
    units = max(units, 1)
    return -units if action == "sell" else units


def place_trade(pair, action):
    px = get_price(pair)

    if not px:
        return {"ok": False, "reason": "No price returned"}

    if px["spread_pips"] > SPREAD_LIMIT_PIPS:
        return {"ok": False, "reason": f"Spread too high: {px['spread_pips']:.2f}"}

    ps = pip_size(pair)
    is_buy = action == "buy"
    entry = px["ask"] if is_buy else px["bid"]
    units = safe_units(action)

    sl = entry - STOP_LOSS_PIPS * ps if is_buy else entry + STOP_LOSS_PIPS * ps
    tp = entry + TAKE_PROFIT_PIPS * ps if is_buy else entry - TAKE_PROFIT_PIPS * ps

    payload = {
        "order": {
            "instrument": pair,
            "units": str(units),
            "type": "MARKET",
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": fmt_price(pair, sl)},
            "takeProfitOnFill": {"price": fmt_price(pair, tp)},
        }
    }

    resp = oanda_post(f"/accounts/{OANDA_ACCOUNT_ID}/orders", payload)
    log(f"UNITS USED: {units}")
    log(f"ORDER RESPONSE: {resp}")

    if "orderFillTransaction" in resp:
        return {"ok": True, "response": resp}

    return {"ok": False, "reason": resp}


def current_mid(pair):
    px = get_price(pair)
    if not px:
        return None
    return (px["bid"] + px["ask"]) / 2


def unrealized_pips(trade, mid):
    pair = trade["instrument"]
    entry = float(trade["price"])
    units = float(trade["currentUnits"])
    ps = pip_size(pair)

    return (mid - entry) / ps if units > 0 else (entry - mid) / ps


def set_trade_sl(trade_id, pair, price):
    payload = {"stopLoss": {"price": fmt_price(pair, price)}}
    return oanda_put(f"/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders", payload)


def manage_trades():
    try:
        for trade in get_open_trades():
            pair = trade["instrument"]
            trade_id = trade["id"]
            entry = float(trade["price"])
            units = float(trade["currentUnits"])
            ps = pip_size(pair)

            mid = current_mid(pair)
            if mid is None:
                continue

            pips = unrealized_pips(trade, mid)

            existing_sl = None
            if trade.get("stopLossOrder"):
                existing_sl = float(trade["stopLossOrder"]["price"])

            if USE_BREAK_EVEN and pips >= BREAK_EVEN_TRIGGER_PIPS:
                be = entry + BREAK_EVEN_PLUS_PIPS * ps if units > 0 else entry - BREAK_EVEN_PLUS_PIPS * ps

                if existing_sl is None or (units > 0 and existing_sl < be) or (units < 0 and existing_sl > be):
                    resp = set_trade_sl(trade_id, pair, be)
                    log(f"BREAK EVEN SET | {pair} | {trade_id} | {resp}")

            if USE_TRAILING_STOP and pips >= TRAILING_TRIGGER_PIPS:
                trail = mid - TRAILING_DISTANCE_PIPS * ps if units > 0 else mid + TRAILING_DISTANCE_PIPS * ps

                if existing_sl is None or (units > 0 and trail > existing_sl) or (units < 0 and trail < existing_sl):
                    resp = set_trade_sl(trade_id, pair, trail)
                    log(f"TRAIL SET | {pair} | {trade_id} | {resp}")

    except Exception as e:
        log(f"MANAGE ERROR: {e}")


def manager_loop():
    while True:
        time.sleep(MANAGE_INTERVAL_SECONDS)
        if OANDA_API_KEY and OANDA_ACCOUNT_ID:
            manage_trades()


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Bot Running V37.1 Sniper Mode 🚀", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "version": "V37.1",
        "mode": "sniper",
        "pairs": PAIRS,
        "fixed_units": FIXED_UNITS,
        "max_units": MAX_UNITS,
    }), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        return jsonify({"ok": False, "error": "Missing OANDA config"}), 500

    data = request.get_json(silent=True) or {}
    log(f"WEBHOOK RECEIVED: {data}")

    if str(data.get("passphrase", "")).strip() != WEBHOOK_PASSPHRASE:
        return jsonify({"ok": False, "error": "Invalid passphrase"}), 403

    pair = str(data.get("pair", "")).strip().upper()
    action = str(data.get("action", "")).strip().lower()

    if pair not in PAIRS:
        return jsonify({"ok": False, "error": f"Pair not allowed: {pair}"}), 400

    if action not in {"buy", "sell"}:
        return jsonify({"ok": False, "error": f"Bad action: {action}"}), 400

    if action == "buy" and not ALLOW_BUY:
        log("BLOCKED: buy disabled")
        return jsonify({"ok": True, "message": "Buy disabled"}), 200

    if action == "sell" and not ALLOW_SELL:
        log("BLOCKED: sell disabled")
        return jsonify({"ok": True, "message": "Sell disabled"}), 200

    with lock:
        if total_open_trades() >= MAX_OPEN_TRADES:
            log("BLOCKED: max open trades reached")
            return jsonify({"ok": True, "message": "Max open trades reached"}), 200

        if open_trades_for_pair(pair) >= MAX_TRADES_PER_PAIR:
            log(f"BLOCKED: max trades reached for {pair}")
            return jsonify({"ok": True, "message": f"Max trades reached for {pair}"}), 200

        now_ts = time.time()
        last_ts = last_trade_time.get(pair, 0)

        if now_ts - last_ts < MIN_SECONDS_BETWEEN_TRADES:
            log(f"BLOCKED: cooldown active for {pair}")
            return jsonify({"ok": True, "message": "Cooldown active"}), 200

        passed, reason = sniper_filter(pair, action)

        if not passed:
            log(f"SNIPER BLOCKED | {pair} {action} | {reason}")
            return jsonify({"ok": True, "message": reason}), 200

        result = place_trade(pair, action)

        if result["ok"]:
            last_trade_time[pair] = now_ts
            log(f"TRADE SUCCESS | {pair} {action}")
            return jsonify({"ok": True, "result": result["response"]}), 200

        log(f"TRADE FAILED | {result['reason']}")
        return jsonify({"ok": False, "error": str(result["reason"])}), 200


# =========================
# START
# =========================
threading.Thread(target=manager_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
