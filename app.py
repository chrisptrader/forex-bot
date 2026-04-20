import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

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

PAIRS = [p.strip() for p in os.getenv("PAIRS", "EUR_USD,GBP_USD,USD_JPY").split(",") if p.strip()]

TIMEZONE_NAME = os.getenv("TIMEZONE_NAME", "America/New_York")
AUTO_CHECK_SECONDS = int(os.getenv("AUTO_CHECK_SECONDS", "10"))

ALLOW_MULTIPAIR = os.getenv("ALLOW_MULTIPAIR", "true").lower() == "true"
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_TOTAL_OPEN_TRADES = int(os.getenv("MAX_TOTAL_OPEN_TRADES", "3"))

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "2"))
FIXED_UNITS = int(os.getenv("FIXED_UNITS", "1000"))
FALLBACK_UNITS = int(os.getenv("FALLBACK_UNITS", "100"))

STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "50"))

ENABLE_SPREAD_FILTER = os.getenv("ENABLE_SPREAD_FILTER", "true").lower() == "true"
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "2.0"))

ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "false").lower() == "true"
LONDON_START = int(os.getenv("LONDON_START", "3"))
LONDON_END = int(os.getenv("LONDON_END", "11"))

ENABLE_TREND_FILTER = os.getenv("ENABLE_TREND_FILTER", "false").lower() == "true"
FAST_MA_PERIOD = int(os.getenv("FAST_MA_PERIOD", "20"))
SLOW_MA_PERIOD = int(os.getenv("SLOW_MA_PERIOD", "50"))
MIN_TREND_GAP_PIPS = float(os.getenv("MIN_TREND_GAP_PIPS", "0.05"))

ENABLE_MOMENTUM_FILTER = os.getenv("ENABLE_MOMENTUM_FILTER", "false").lower() == "true"
MOMENTUM_CANDLES = int(os.getenv("MOMENTUM_CANDLES", "1"))
MOMENTUM_MIN_BODY_PIPS = float(os.getenv("MOMENTUM_MIN_BODY_PIPS", "0.8"))

MIN_CANDLE_RANGE_PIPS = float(os.getenv("MIN_CANDLE_RANGE_PIPS", "1.0"))
MAX_CANDLE_RANGE_PIPS = float(os.getenv("MAX_CANDLE_RANGE_PIPS", "35"))
MIN_VOLATILITY_PIPS = float(os.getenv("MIN_VOLATILITY_PIPS", "1.5"))

BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "3"))
BUY_PULLBACK_PIPS = float(os.getenv("BUY_PULLBACK_PIPS", "0.5"))
SELL_BOUNCE_PIPS = float(os.getenv("SELL_BOUNCE_PIPS", "0.5"))

USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true").lower() == "true"
BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "15"))
BREAK_EVEN_PLUS_PIPS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", "2"))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true").lower() == "true"
TRAILING_TRIGGER_PIPS = float(os.getenv("TRAILING_TRIGGER_PIPS", "15"))
TRAILING_DISTANCE_PIPS = float(os.getenv("TRAILING_DISTANCE_PIPS", "10"))

MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "10"))

# =========================
# OANDA
# =========================
if OANDA_ENV == "live":
    OANDA_BASE_URL = "https://api-fxtrade.oanda.com"
else:
    OANDA_BASE_URL = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

# =========================
# STATE
# =========================
last_trade_time_by_pair = {}
last_signal_time_by_pair = {}
recent_signals = []

# =========================
# HELPERS
# =========================
def now_ny():
    return datetime.now(ZoneInfo(TIMEZONE_NAME))


def log(msg: str):
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S +0000')}] {msg}", flush=True)


def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def price_precision(pair: str) -> int:
    return 3 if "JPY" in pair else 5


def side_normalize(value: str) -> str:
    value = str(value).strip().upper()
    if value in ("BUY", "LONG"):
        return "BUY"
    if value in ("SELL", "SHORT"):
        return "SELL"
    return ""


def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() == "true"


def trade_url():
    return f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"


def instruments_url():
    return f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"


def candles_url(pair: str, count: int = 60, granularity: str = "M5"):
    return f"{OANDA_BASE_URL}/v3/instruments/{pair}/candles?count={count}&price=M&granularity={granularity}"


def get_open_trades():
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json().get("trades", [])


def count_open_trades(pair=None):
    try:
        trades = get_open_trades()
        if pair:
            return sum(1 for t in trades if t.get("instrument") == pair)
        return len(trades)
    except Exception as e:
        log(f"OPEN TRADES ERROR: {e}")
        return 0


def get_price(pair: str):
    params = {"instruments": pair}
    r = requests.get(instruments_url(), headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    prices = r.json().get("prices", [])
    if not prices:
        raise ValueError(f"No pricing for {pair}")
    p = prices[0]
    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])
    return bid, ask


def spread_pips(pair: str, bid: float, ask: float) -> float:
    return round((ask - bid) / pip_size(pair), 2)


def get_candles(pair: str, count: int = 60):
    r = requests.get(candles_url(pair, count=count), headers=HEADERS, timeout=15)
    r.raise_for_status()
    candles = r.json().get("candles", [])
    parsed = []
    for c in candles:
        if not c.get("complete"):
            continue
        mid = c["mid"]
        parsed.append({
            "o": float(mid["o"]),
            "h": float(mid["h"]),
            "l": float(mid["l"]),
            "c": float(mid["c"]),
            "time": c["time"],
        })
    return parsed


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def candle_range_pips(pair: str, candle: dict) -> float:
    return round((candle["h"] - candle["l"]) / pip_size(pair), 2)


def candle_body_pips(pair: str, candle: dict) -> float:
    return round(abs(candle["c"] - candle["o"]) / pip_size(pair), 2)


def session_pass():
    if not ENABLE_SESSION_FILTER:
        return True, "session filter off"
    hour = now_ny().hour
    if LONDON_START <= hour < LONDON_END:
        return True, f"within session {hour}"
    return False, f"blocked by session filter hour={hour}"


def trend_pass(pair: str, side: str, candles: list):
    if not ENABLE_TREND_FILTER:
        return True, "trend filter off"

    closes = [c["c"] for c in candles]
    fast = sma(closes, FAST_MA_PERIOD)
    slow = sma(closes, SLOW_MA_PERIOD)
    if fast is None or slow is None:
        return False, "not enough candles for trend filter"

    gap_pips = abs(fast - slow) / pip_size(pair)

    if side == "BUY":
        if fast > slow and gap_pips >= MIN_TREND_GAP_PIPS:
            return True, f"BUY trend pass fast_ma={fast:.5f} slow_ma={slow:.5f} gap={gap_pips:.1f}"
        return False, f"trend blocked fast_ma={fast:.5f} slow_ma={slow:.5f} gap={gap_pips:.1f}"

    if side == "SELL":
        if fast < slow and gap_pips >= MIN_TREND_GAP_PIPS:
            return True, f"SELL trend pass fast_ma={fast:.5f} slow_ma={slow:.5f} gap={gap_pips:.1f}"
        return False, f"trend blocked fast_ma={fast:.5f} slow_ma={slow:.5f} gap={gap_pips:.1f}"

    return False, "invalid side in trend filter"


def volatility_pass(pair: str, candles: list):
    if len(candles) < 3:
        return False, "not enough candles for volatility"
    recent = candles[-3:]
    avg_range = sum(candle_range_pips(pair, c) for c in recent) / len(recent)
    if avg_range >= MIN_VOLATILITY_PIPS:
        return True, f"volatility pass range={avg_range:.1f} pips"
    return False, f"volatility blocked range={avg_range:.1f} pips"


def candle_range_pass(pair: str, candles: list):
    latest = candles[-1]
    rng = candle_range_pips(pair, latest)
    if rng < MIN_CANDLE_RANGE_PIPS:
        return False, f"candle too small range={rng:.1f}"
    if rng > MAX_CANDLE_RANGE_PIPS:
        return False, f"candle too large range={rng:.1f}"
    return True, f"candle range pass={rng:.1f}"


def momentum_pass(pair: str, side: str, candles: list):
    if not ENABLE_MOMENTUM_FILTER:
        return True, "momentum filter off"

    if len(candles) < MOMENTUM_CANDLES:
        return False, "not enough candles for momentum"

    recent = candles[-MOMENTUM_CANDLES:]
    for c in recent:
        body = candle_body_pips(pair, c)
        if body < MOMENTUM_MIN_BODY_PIPS:
            return False, f"momentum blocked body={body:.1f}"
        if side == "BUY" and c["c"] <= c["o"]:
            return False, "momentum blocked non-bull candle"
        if side == "SELL" and c["c"] >= c["o"]:
            return False, "momentum blocked non-bear candle"

    return True, "momentum pass"


def structure_pass(pair: str, side: str, candles: list):
    if len(candles) < BREAKOUT_LOOKBACK + 2:
        return False, "not enough candles for structure"

    latest = candles[-1]
    prev_block = candles[-(BREAKOUT_LOOKBACK + 1):-1]
    highest_high = max(c["h"] for c in prev_block)
    lowest_low = min(c["l"] for c in prev_block)

    if side == "BUY":
        pullback = (highest_high - latest["l"]) / pip_size(pair)
        if latest["c"] > highest_high or pullback <= BUY_PULLBACK_PIPS:
            return True, f"buy structure pass hh={highest_high:.5f} pullback={pullback:.1f}"
        return False, f"buy blocked no breakout/pullback pullback={pullback:.1f}"

    if side == "SELL":
        bounce = (latest["h"] - lowest_low) / pip_size(pair)
        if latest["c"] < lowest_low or bounce <= SELL_BOUNCE_PIPS:
            return True, f"sell structure pass ll={lowest_low:.5f} bounce={bounce:.1f}"
        return False, f"sell blocked no bounce breakdown bounce={bounce:.1f}"

    return False, "invalid side in structure"


def spread_filter_pass(pair: str, bid: float, ask: float):
    if not ENABLE_SPREAD_FILTER:
        return True, "spread filter off"
    sp = spread_pips(pair, bid, ask)
    if sp <= MAX_SPREAD_PIPS:
        return True, f"spread pass spread={sp} pips"
    return False, f"spread blocked spread={sp} pips"


def seconds_since_last_trade(pair: str):
    last_ts = last_trade_time_by_pair.get(pair, 0)
    return time.time() - last_ts


def can_trade_now(pair: str):
    if seconds_since_last_trade(pair) < MIN_SECONDS_BETWEEN_TRADES:
        return False, f"cooldown active {seconds_since_last_trade(pair):.1f}s"

    if count_open_trades() >= MAX_TOTAL_OPEN_TRADES:
        return False, f"max total open trades reached {MAX_TOTAL_OPEN_TRADES}"

    if count_open_trades(pair) >= MAX_OPEN_TRADES:
        return False, f"max open trades reached for {pair}"

    return True, "trade allowed"


def build_order(pair: str, side: str, units: int, bid: float, ask: float, sl_pips: float, tp_pips: float):
    px = ask if side == "BUY" else bid
    pip = pip_size(pair)
    precision = price_precision(pair)

    if side == "BUY":
        sl = round(px - sl_pips * pip, precision)
        tp = round(px + tp_pips * pip, precision)
        units = abs(units)
    else:
        sl = round(px + sl_pips * pip, precision)
        tp = round(px - tp_pips * pip, precision)
        units = -abs(units)

    order = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": f"{sl:.{precision}f}"
            },
            "takeProfitOnFill": {
                "price": f"{tp:.{precision}f}"
            }
        }
    }
    return order


def submit_order(order_payload: dict):
    r = requests.post(trade_url(), headers=HEADERS, json=order_payload, timeout=20)
    if r.status_code >= 400:
        raise ValueError(f"OANDA order failed {r.status_code}: {r.text}")
    return r.json()


def choose_units():
    return FIXED_UNITS if FIXED_UNITS > 0 else FALLBACK_UNITS


def record_signal(payload: dict):
    recent_signals.append({
        "time": datetime.utcnow().isoformat(),
        "payload": payload
    })
    if len(recent_signals) > 50:
        recent_signals.pop(0)


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "status": "running",
        "env": OANDA_ENV,
        "allowed_pairs": PAIRS,
        "fixed_units": FIXED_UNITS,
        "max_open_trades": MAX_OPEN_TRADES,
        "max_trades_per_pair": MAX_OPEN_TRADES,
        "spread_filter": ENABLE_SPREAD_FILTER,
        "session_filter": ENABLE_SESSION_FILTER,
        "trend_filter": ENABLE_TREND_FILTER,
        "momentum_filter": ENABLE_MOMENTUM_FILTER,
        "volatility_filter": True,
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200


@app.route("/recent-signals", methods=["GET"])
def recent():
    return jsonify(recent_signals[-20:]), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=False)

        if not isinstance(data, dict):
            log("WEBHOOK ERROR: payload not object")
            return jsonify({"ok": False, "error": "payload must be json object"}), 400

        passphrase = str(data.get("passphrase", "")).strip()
        if passphrase != WEBHOOK_PASSPHRASE:
            log("WEBHOOK ERROR: bad passphrase")
            return jsonify({"ok": False, "error": "bad passphrase"}), 403

        pair = str(data.get("pair", "")).strip().upper()
        if pair not in PAIRS:
            log(f"WEBHOOK ERROR: pair not allowed {pair}")
            return jsonify({"ok": False, "error": f"pair not allowed: {pair}"}), 400

        side = side_normalize(data.get("side") or data.get("action"))
        if side not in ("BUY", "SELL"):
            log("WEBHOOK ERROR: invalid side")
            return jsonify({"ok": False, "error": "invalid side"}), 400

        risk = float(data.get("risk", RISK_PERCENT / 100 if RISK_PERCENT > 1 else RISK_PERCENT))
        sl_pips = float(data.get("sl_pips", STOP_LOSS_PIPS))
        tp_pips = float(data.get("tp_pips", TAKE_PROFIT_PIPS))
        trailing = parse_bool(data.get("trailing"), USE_TRAILING_STOP)

        record_signal(data)
        log(f"WEBHOOK RECEIVED | pair={pair} side={side} risk={risk} sl={sl_pips} tp={tp_pips} trailing={trailing}")

        ok, reason = session_pass()
        if not ok:
            log(f"Blocked by session filter | pair={pair} action={side}")
            return jsonify({"ok": True, "blocked": True, "reason": reason}), 200

        ok, reason = can_trade_now(pair)
        if not ok:
            log(f"TRADE BLOCKED | pair={pair} side={side} reason={reason}")
            return jsonify({"ok": True, "blocked": True, "reason": reason}), 200

        bid, ask = get_price(pair)

        ok, reason = spread_filter_pass(pair, bid, ask)
        log(f"FILTER | pair={pair} side={side} result={ok} reason={reason}")
        if not ok:
            return jsonify({"ok": True, "blocked": True, "reason": reason}), 200

        candles = get_candles(pair, count=max(SLOW_MA_PERIOD + 5, 60))

        ok, reason = candle_range_pass(pair, candles)
        log(f"FILTER | pair={pair} side={side} result={ok} reason={reason}")
        if not ok:
            return jsonify({"ok": True, "blocked": True, "reason": reason}), 200

        ok, reason = volatility_pass(pair, candles)
        log(f"FILTER | pair={pair} side={side} result={ok} reason={reason}")
        if not ok:
            return jsonify({"ok": True, "blocked": True, "reason": reason}), 200

        ok, reason = trend_pass(pair, side, candles)
        log(f"FILTER | pair={pair} side={side} result={ok} reason={reason}")
        if not ok:
            return jsonify({"ok": True, "blocked": True, "reason": reason}), 200

        ok, reason = momentum_pass(pair, side, candles)
        log(f"FILTER | pair={pair} side={side} result={ok} reason={reason}")
        if not ok:
            return jsonify({"ok": True, "blocked": True, "reason": reason}), 200

        ok, reason = structure_pass(pair, side, candles)
        log(f"FILTER | pair={pair} side={side} result={ok} reason={reason}")
        if not ok:
            return jsonify({"ok": True, "blocked": True, "reason": reason}), 200

        units = choose_units()
        order_payload = build_order(pair, side, units, bid, ask, sl_pips, tp_pips)
        order_result = submit_order(order_payload)

        last_trade_time_by_pair[pair] = time.time()
        last_signal_time_by_pair[pair] = time.time()

        log(f"TRADE OPENED | pair={pair} side={side} units={units}")

        return jsonify({
            "ok": True,
            "pair": pair,
            "side": side,
            "units": units,
            "order_result": order_result
        }), 200

    except Exception as e:
        log(f"WEBHOOK ERROR: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
