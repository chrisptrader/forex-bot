import os
import math
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =========================
# ENV / CONFIG
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()

ALLOWED_PAIRS = [p.strip() for p in os.getenv("ALLOWED_PAIRS", "EUR_USD,GBP_USD,USD_JPY").split(",") if p.strip()]

FIXED_UNITS = int(os.getenv("FIXED_UNITS", "1000"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_TRADES_PER_PAIR", "1"))

ENABLE_SPREAD_FILTER = os.getenv("ENABLE_SPREAD_FILTER", "true").lower() == "true"
ENABLE_TREND_FILTER = os.getenv("ENABLE_TREND_FILTER", "true").lower() == "true"
ENABLE_VOLATILITY_FILTER = os.getenv("ENABLE_VOLATILITY_FILTER", "true").lower() == "true"
ENABLE_PULLBACK_FILTER = os.getenv("ENABLE_PULLBACK_FILTER", "true").lower() == "true"
ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "false").lower() == "true"

# v18 tuned a little looser than before
MIN_TREND_GAP_PIPS = float(os.getenv("MIN_TREND_GAP_PIPS", "0.0"))
MIN_VOLATILITY_PIPS = float(os.getenv("MIN_VOLATILITY_PIPS", "3.0"))
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "2.5"))

FAST_EMA = int(os.getenv("FAST_EMA", "9"))
SLOW_EMA = int(os.getenv("SLOW_EMA", "21"))
PULLBACK_CANDLES = int(os.getenv("PULLBACK_CANDLES", "3"))
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "60"))

PAIR_CFG = {
    "EUR_USD": {"pip": 0.0001, "default_sl": 15, "default_tp": 30},
    "GBP_USD": {"pip": 0.0001, "default_sl": 20, "default_tp": 40},
    "USD_JPY": {"pip": 0.01,   "default_sl": 20, "default_tp": 40},
}

last_trade_time = {}

if OANDA_ENV == "live":
    OANDA_BASE = "https://api-fxtrade.oanda.com"
else:
    OANDA_BASE = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

# =========================
# HELPERS
# =========================
def now_utc():
    return datetime.now(timezone.utc)

def log_filter(pair, side, result, reason):
    app.logger.info(f"FILTER | pair={pair} side={side} result={result} reason={reason}")

def oanda_get(path, params=None):
    url = f"{OANDA_BASE}{path}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def oanda_post(path, payload):
    url = f"{OANDA_BASE}{path}"
    r = requests.post(url, headers=HEADERS, json=payload, timeout=15)
    if not r.ok:
        app.logger.error(f"OANDA ERROR {r.status_code}: {r.text}")
    r.raise_for_status()
    return r.json()

def get_pricing(pair):
    data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/pricing", params={"instruments": pair})
    prices = data.get("prices", [])
    if not prices:
        raise ValueError(f"No pricing for {pair}")
    return prices[0]

def get_bid_ask(pair):
    p = get_pricing(pair)
    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])
    return bid, ask

def spread_pips(pair):
    bid, ask = get_bid_ask(pair)
    pip = PAIR_CFG[pair]["pip"]
    return (ask - bid) / pip

def get_candles(pair, count=50, granularity="M5"):
    data = oanda_get(
        f"/v3/instruments/{pair}/candles",
        params={"count": count, "price": "MBA", "granularity": granularity}
    )
    candles = []
    for c in data.get("candles", []):
        if not c.get("complete", False):
            continue
        mid = c["mid"]
        candles.append({
            "time": c["time"],
            "o": float(mid["o"]),
            "h": float(mid["h"]),
            "l": float(mid["l"]),
            "c": float(mid["c"]),
        })
    return candles

def ema(values, period):
    if not values or len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def trend_pass(pair, side):
    candles = get_candles(pair, count=max(SLOW_EMA + 10, 40))
    closes = [c["c"] for c in candles]
    fast = ema(closes, FAST_EMA)
    slow = ema(closes, SLOW_EMA)
    if fast is None or slow is None:
        log_filter(pair, side, False, "not enough data for EMA")
        return False
    gap_pips = abs(fast - slow) / PAIR_CFG[pair]["pip"]

    if side == "BUY":
        ok = fast > slow and gap_pips >= MIN_TREND_GAP_PIPS
        reason = f"BUY trend {'pass' if ok else 'blocked'} fast_ma={fast:.5f} slow_ma={slow:.5f} gap={gap_pips:.1f}"
    else:
        ok = slow > fast and gap_pips >= MIN_TREND_GAP_PIPS
        reason = f"SELL trend {'pass' if ok else 'blocked'} fast_ma={fast:.5f} slow_ma={slow:.5f} gap={gap_pips:.1f}"

    log_filter(pair, side, ok, reason)
    return ok

def volatility_pass(pair, side):
    candles = get_candles(pair, count=6)
    if len(candles) < 2:
        log_filter(pair, side, False, "not enough candles")
        return False
    rng = candles[-2]["h"] - candles[-2]["l"]
    rng_pips = rng / PAIR_CFG[pair]["pip"]
    ok = rng_pips >= MIN_VOLATILITY_PIPS
    reason = f"{'volatility pass' if ok else 'volatility blocked'} range={rng_pips:.1f} pips"
    log_filter(pair, side, ok, reason)
    return ok

def pullback_pass(pair, side):
    candles = get_candles(pair, count=max(PULLBACK_CANDLES + 3, 6))
    closes = [c["c"] for c in candles]
    recent = candles[-PULLBACK_CANDLES:]

    # simple pullback/bounce logic, not too strict
    if side == "BUY":
        highest_recent = max(c["h"] for c in recent)
        last_close = candles[-1]["c"]
        bounce = (highest_recent - last_close) / PAIR_CFG[pair]["pip"]
        ok = bounce >= -3.0
        reason = f"{'buy pullback pass' if ok else 'buy blocked no pullback'} bounce={bounce:.1f}"
    else:
        lowest_recent = min(c["l"] for c in recent)
        last_close = candles[-1]["c"]
        bounce = (last_close - lowest_recent) / PAIR_CFG[pair]["pip"]
        ok = bounce >= -3.0
        reason = f"{'sell bounce pass' if ok else 'sell blocked no bounce'} bounce={bounce:.1f}"

    log_filter(pair, side, ok, reason)
    return ok

def session_pass(pair, side):
    if not ENABLE_SESSION_FILTER:
        return True
    hour = now_utc().hour
    # loose London/NY overlap style
    ok = 7 <= hour <= 17
    reason = "session pass" if ok else "Blocked by session filter"
    log_filter(pair, side, ok, reason)
    return ok

def open_trades():
    data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    return data.get("trades", [])

def open_trade_count_for_pair(pair):
    return sum(1 for t in open_trades() if t.get("instrument") == pair)

def total_open_trades():
    return len(open_trades())

def can_trade_now(pair):
    ts = last_trade_time.get(pair)
    if ts is None:
        return True
    elapsed = (now_utc() - ts).total_seconds()
    return elapsed >= MIN_SECONDS_BETWEEN_TRADES

def build_order(pair, side, units, sl_pips, tp_pips, trailing):
    bid, ask = get_bid_ask(pair)
    pip = PAIR_CFG[pair]["pip"]

    if side == "BUY":
        entry = ask
        sl_price = entry - sl_pips * pip
        tp_price = entry + tp_pips * pip
        signed_units = str(abs(units))
    else:
        entry = bid
        sl_price = entry + sl_pips * pip
        tp_price = entry - tp_pips * pip
        signed_units = str(-abs(units))

    order = {
        "order": {
            "instrument": pair,
            "units": signed_units,
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": f"{sl_price:.5f}" if pip < 0.01 else f"{sl_price:.3f}"
            },
            "takeProfitOnFill": {
                "price": f"{tp_price:.5f}" if pip < 0.01 else f"{tp_price:.3f}"
            }
        }
    }

    if trailing:
        distance = sl_pips * pip
        order["order"]["trailingStopLossOnFill"] = {
            "distance": f"{distance:.5f}" if pip < 0.01 else f"{distance:.3f}"
        }
        # remove fixed SL if trailing used
        order["order"].pop("stopLossOnFill", None)

    return order

# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "env": OANDA_ENV,
        "allowed_pairs": ALLOWED_PAIRS,
        "fixed_units": FIXED_UNITS,
        "max_open_trades": MAX_OPEN_TRADES,
        "max_trades_per_pair": MAX_TRADES_PER_PAIR,
        "spread_filter": ENABLE_SPREAD_FILTER,
        "trend_filter": ENABLE_TREND_FILTER,
        "volatility_filter": ENABLE_VOLATILITY_FILTER,
        "pullback_filter": ENABLE_PULLBACK_FILTER,
        "session_filter": ENABLE_SESSION_FILTER,
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        app.logger.error(f"WEBHOOK ERROR: 400 Bad Request: {e}")
        return jsonify({"ok": False, "error": "bad json"}), 400

    pair = str(data.get("pair", "")).strip()
    side = str(data.get("side", "")).strip().upper()
    risk = float(data.get("risk", 0.02))
    trailing = bool(data.get("trailing", True))

    if pair not in ALLOWED_PAIRS:
        return jsonify({"ok": False, "error": f"pair not allowed: {pair}"}), 400

    if side not in ("BUY", "SELL"):
        app.logger.error("WEBHOOK ERROR: invalid side")
        return jsonify({"ok": False, "error": "invalid side"}), 400

    default_sl = PAIR_CFG[pair]["default_sl"]
    default_tp = PAIR_CFG[pair]["default_tp"]
    sl_pips = float(data.get("sl_pips", default_sl))
    tp_pips = float(data.get("tp_pips", default_tp))

    app.logger.info(
        f"WEBHOOK RECEIVED | pair={pair} side={side} risk={risk} sl={sl_pips} tp={tp_pips} trailing={trailing}"
    )

    if total_open_trades() >= MAX_OPEN_TRADES:
        app.logger.info(f"TRADE BLOCKED | pair={pair} side={side} reason=max open trades hit")
        return jsonify({"ok": True, "blocked": "max open trades"}), 200

    if open_trade_count_for_pair(pair) >= MAX_TRADES_PER_PAIR:
        app.logger.info(f"TRADE BLOCKED | pair={pair} side={side} reason=max trades per pair hit")
        return jsonify({"ok": True, "blocked": "max per pair"}), 200

    if not can_trade_now(pair):
        app.logger.info(f"TRADE BLOCKED | pair={pair} side={side} reason=min seconds between trades")
        return jsonify({"ok": True, "blocked": "cooldown"}), 200

    if ENABLE_SESSION_FILTER and not session_pass(pair, side):
        return jsonify({"ok": True, "blocked": "session filter"}), 200

    if ENABLE_SPREAD_FILTER:
        sp = spread_pips(pair)
        ok = sp <= MAX_SPREAD_PIPS
        log_filter(pair, side, ok, f"{'spread pass' if ok else 'spread blocked'} spread={sp:.1f} pips")
        if not ok:
            app.logger.info(f"TRADE BLOCKED | pair={pair} side={side} reason=spread blocked {sp:.1f} pips")
            return jsonify({"ok": True, "blocked": "spread"}), 200

    if ENABLE_VOLATILITY_FILTER and not volatility_pass(pair, side):
        candles = get_candles(pair, count=2)
        rng = (candles[-1]["h"] - candles[-1]["l"]) / PAIR_CFG[pair]["pip"] if candles else 0
        app.logger.info(f"TRADE BLOCKED | pair={pair} side={side} reason=volatility blocked {rng:.1f} pips")
        return jsonify({"ok": True, "blocked": "volatility"}), 200

    if ENABLE_TREND_FILTER and not trend_pass(pair, side):
        app.logger.info(f"TRADE BLOCKED | pair={pair} side={side} reason=trend blocked")
        return jsonify({"ok": True, "blocked": "trend"}), 200

    if ENABLE_PULLBACK_FILTER and not pullback_pass(pair, side):
        app.logger.info(f"TRADE BLOCKED | pair={pair} side={side} reason=pullback/bounce blocked")
        return jsonify({"ok": True, "blocked": "pullback"}), 200

    order = build_order(pair, side, FIXED_UNITS, sl_pips, tp_pips, trailing)

    try:
        resp = oanda_post(f"/v3/accounts/{OANDA_ACCOUNT_ID}/orders", order)
        last_trade_time[pair] = now_utc()
        app.logger.info(f"ORDER PLACED | pair={pair} side={side}")
        return jsonify({"ok": True, "result": resp}), 200
    except Exception as e:
        app.logger.error(f"ORDER ERROR | pair={pair} side={side} error={e}")
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
