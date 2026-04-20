import os
import math
import json
from datetime import datetime, time, timezone
from typing import Dict, Tuple, Optional, List

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
# ENV / CONFIG
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.getenv("OANDA_ENV", os.getenv("ENV", "practice")).strip().lower()

BASE_URL = (
    "https://api-fxpractice.oanda.com/v3"
    if OANDA_ENV != "live"
    else "https://api-fxtrade.oanda.com/v3"
)

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

PORT = int(os.getenv("PORT", "10000"))

# Risk / order sizing
DEFAULT_FIXED_UNITS = int(os.getenv("FIXED_UNITS", "1000"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_TRADES_PER_PAIR", "2"))

# Filters
ENABLE_SPREAD_FILTER = os.getenv("ENABLE_SPREAD_FILTER", "true").lower() == "true"
ENABLE_VOLATILITY_FILTER = os.getenv("ENABLE_VOLATILITY_FILTER", "true").lower() == "true"
ENABLE_TREND_FILTER = os.getenv("ENABLE_TREND_FILTER", "true").lower() == "true"
ENABLE_PULLBACK_FILTER = os.getenv("ENABLE_PULLBACK_FILTER", "true").lower() == "true"
ENABLE_MOMENTUM_FILTER = os.getenv("ENABLE_MOMENTUM_FILTER", "false").lower() == "true"
ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "false").lower() == "true"

FAST_EMA_PERIOD = int(os.getenv("FAST_EMA_PERIOD", "9"))
SLOW_EMA_PERIOD = int(os.getenv("SLOW_EMA_PERIOD", "20"))

MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "2.0"))
MIN_VOLATILITY_PIPS = float(os.getenv("MIN_VOLATILITY_PIPS", "4.0"))
MIN_TREND_GAP_PIPS = float(os.getenv("MIN_TREND_GAP_PIPS", "0.1"))

# Pullback / bounce logic
BUY_PULLBACK_PIPS = float(os.getenv("BUY_PULLBACK_PIPS", "1.0"))
SELL_BOUNCE_PIPS = float(os.getenv("SELL_BOUNCE_PIPS", "1.0"))

# Momentum logic
MOMENTUM_LOOKBACK = int(os.getenv("MOMENTUM_LOOKBACK", "3"))
MOMENTUM_MIN_PIPS = float(os.getenv("MOMENTUM_MIN_PIPS", "1.5"))

# Session filter (New York / London in UTC)
# Set these only if ENABLE_SESSION_FILTER=true
SESSION_START_UTC = os.getenv("SESSION_START_UTC", "12:00")  # example 12:00 UTC
SESSION_END_UTC = os.getenv("SESSION_END_UTC", "16:00")      # example 16:00 UTC

ALLOWED_PAIRS = {"EUR_USD", "GBP_USD", "USD_JPY"}

PAIR_CONFIG: Dict[str, Dict[str, float]] = {
    "EUR_USD": {"pip_size": 0.0001, "display_precision": 5},
    "GBP_USD": {"pip_size": 0.0001, "display_precision": 5},
    "USD_JPY": {"pip_size": 0.01, "display_precision": 3},
}

# =========================
# HELPERS
# =========================
def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f %Z")

def log(msg: str) -> None:
    print(f"[{now_utc()}] {msg}", flush=True)

def to_bool(val: str) -> bool:
    return str(val).strip().lower() == "true"

def parse_hhmm(hhmm: str) -> time:
    parts = hhmm.split(":")
    return time(hour=int(parts[0]), minute=int(parts[1]))

def in_session() -> bool:
    if not ENABLE_SESSION_FILTER:
        return True
    now = datetime.now(timezone.utc).time()
    start = parse_hhmm(SESSION_START_UTC)
    end = parse_hhmm(SESSION_END_UTC)
    return start <= now <= end

def pip_size(pair: str) -> float:
    return PAIR_CONFIG[pair]["pip_size"]

def price_to_pips(pair: str, price_diff: float) -> float:
    return price_diff / pip_size(pair)

def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def oanda_get(path: str, params: Optional[Dict] = None) -> Dict:
    url = f"{BASE_URL}{path}"
    resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()

def oanda_post(path: str, payload: Dict) -> Dict:
    url = f"{BASE_URL}{path}"
    resp = requests.post(url, headers=HEADERS, data=json.dumps(payload), timeout=20)
    if resp.status_code >= 400:
        raise Exception(f"OANDA ERROR {resp.status_code}: {resp.text}")
    return resp.json()

def get_pricing(pair: str) -> Tuple[float, float]:
    data = oanda_get("/accounts/{}/pricing".format(OANDA_ACCOUNT_ID), {"instruments": pair})
    prices = data.get("prices", [])
    if not prices:
        raise Exception(f"No pricing returned for {pair}")
    p = prices[0]
    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])
    return bid, ask

def get_candles(pair: str, count: int = 60, granularity: str = "M5") -> List[Dict]:
    data = oanda_get(
        f"/instruments/{pair}/candles",
        {
            "count": count,
            "price": "M",
            "granularity": granularity,
        },
    )
    candles = data.get("candles", [])
    return [c for c in candles if c.get("complete")]

def get_open_trades() -> List[Dict]:
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    return data.get("trades", [])

def count_open_for_pair(pair: str) -> int:
    return sum(1 for t in get_open_trades() if t.get("instrument") == pair)

def can_open_more(pair: str) -> Tuple[bool, str]:
    open_trades = get_open_trades()
    if len(open_trades) >= MAX_OPEN_TRADES:
        return False, f"max open trades reached ({len(open_trades)})"
    pair_count = sum(1 for t in open_trades if t.get("instrument") == pair)
    if pair_count >= MAX_TRADES_PER_PAIR:
        return False, f"max trades per pair reached ({pair_count})"
    return True, "ok"

def compute_indicators(pair: str) -> Dict[str, float]:
    candles = get_candles(pair, count=max(60, SLOW_EMA_PERIOD + 10), granularity="M5")
    closes = [float(c["mid"]["c"]) for c in candles]
    highs = [float(c["mid"]["h"]) for c in candles]
    lows = [float(c["mid"]["l"]) for c in candles]
    opens = [float(c["mid"]["o"]) for c in candles]

    if len(closes) < max(SLOW_EMA_PERIOD, MOMENTUM_LOOKBACK + 2):
        raise Exception("not enough candles")

    fast = ema(closes, FAST_EMA_PERIOD)
    slow = ema(closes, SLOW_EMA_PERIOD)
    if fast is None or slow is None:
        raise Exception("EMA calc failed")

    # Range on most recent completed candle
    last_range_pips = price_to_pips(pair, highs[-1] - lows[-1])

    # Pullback / bounce calculations:
    # BUY pullback = last close relative to recent swing high
    recent_high = max(highs[-5:])
    recent_low = min(lows[-5:])
    last_close = closes[-1]

    buy_pullback_pips = price_to_pips(pair, recent_high - last_close)
    sell_bounce_pips = price_to_pips(pair, last_close - recent_low)

    # Simple momentum: close[-1] - close[-1-lookback]
    momentum_pips = price_to_pips(pair, closes[-1] - closes[-1 - MOMENTUM_LOOKBACK])

    return {
        "fast_ma": fast,
        "slow_ma": slow,
        "trend_gap_pips": abs(price_to_pips(pair, fast - slow)),
        "last_range_pips": last_range_pips,
        "buy_pullback_pips": buy_pullback_pips,
        "sell_bounce_pips": sell_bounce_pips,
        "momentum_pips": momentum_pips,
        "last_close": last_close,
        "recent_high": recent_high,
        "recent_low": recent_low,
    }

def apply_filters(pair: str, side: str) -> Tuple[bool, List[str]]:
    reasons: List[str] = []

    # Session
    if ENABLE_SESSION_FILTER and not in_session():
        reasons.append(f"Blocked by session filter | pair={pair} action={side}")
        return False, reasons

    # Spread
    bid, ask = get_pricing(pair)
    spread_pips = price_to_pips(pair, ask - bid)
    if ENABLE_SPREAD_FILTER:
        spread_ok = spread_pips <= MAX_SPREAD_PIPS
        log(f"FILTER | pair={pair} side={side} result={spread_ok} reason=spread {'pass' if spread_ok else 'blocked'} spread={round(spread_pips, 1)} pips")
        if not spread_ok:
            reasons.append(f"spread blocked {round(spread_pips,1)} pips")
            return False, reasons

    ind = compute_indicators(pair)

    # Volatility
    if ENABLE_VOLATILITY_FILTER:
        vol_ok = ind["last_range_pips"] >= MIN_VOLATILITY_PIPS
        log(f"FILTER | pair={pair} side={side} result={vol_ok} reason=volatility {'pass' if vol_ok else 'blocked'} range={round(ind['last_range_pips'],1)} pips")
        if not vol_ok:
            reasons.append(f"volatility blocked {round(ind['last_range_pips'],1)} pips")
            return False, reasons

    # Trend
    if ENABLE_TREND_FILTER:
        if side == "BUY":
            trend_ok = ind["fast_ma"] > ind["slow_ma"] and ind["trend_gap_pips"] >= MIN_TREND_GAP_PIPS
            log(
                f"FILTER | pair={pair} side={side} result={trend_ok} "
                f"reason={'BUY trend pass' if trend_ok else 'trend blocked'} "
                f"fast_ma={ind['fast_ma']:.5f} slow_ma={ind['slow_ma']:.5f} gap={round(ind['trend_gap_pips'],1)}"
            )
            if not trend_ok:
                reasons.append(
                    f"trend blocked fast_ma={ind['fast_ma']:.5f} slow_ma={ind['slow_ma']:.5f} gap={round(ind['trend_gap_pips'],1)}"
                )
                return False, reasons
        else:
            trend_ok = ind["fast_ma"] < ind["slow_ma"] and ind["trend_gap_pips"] >= MIN_TREND_GAP_PIPS
            log(
                f"FILTER | pair={pair} side={side} result={trend_ok} "
                f"reason={'SELL trend pass' if trend_ok else 'trend blocked'} "
                f"fast_ma={ind['fast_ma']:.5f} slow_ma={ind['slow_ma']:.5f} gap={round(ind['trend_gap_pips'],1)}"
            )
            if not trend_ok:
                reasons.append(
                    f"trend blocked fast_ma={ind['fast_ma']:.5f} slow_ma={ind['slow_ma']:.5f} gap={round(ind['trend_gap_pips'],1)}"
                )
                return False, reasons

    # Momentum
    if ENABLE_MOMENTUM_FILTER:
        if side == "BUY":
            momentum_ok = ind["momentum_pips"] >= MOMENTUM_MIN_PIPS
        else:
            momentum_ok = ind["momentum_pips"] <= -MOMENTUM_MIN_PIPS
        log(f"FILTER | pair={pair} side={side} result={momentum_ok} reason={'momentum pass' if momentum_ok else 'not enough candles for momentum filter' if math.isnan(ind['momentum_pips']) else 'momentum blocked'}")
        if not momentum_ok:
            reasons.append("momentum blocked")
            return False, reasons
    else:
        log(f"FILTER | pair={pair} side={side} result=True reason=momentum filter off")

    # Pullback / bounce
    if ENABLE_PULLBACK_FILTER:
        if side == "BUY":
            pullback_ok = ind["buy_pullback_pips"] >= BUY_PULLBACK_PIPS
            log(
                f"FILTER | pair={pair} side={side} result={pullback_ok} "
                f"reason={'buy pullback pass' if pullback_ok else 'buy blocked no pullback'} "
                f"pullback={round(ind['buy_pullback_pips'],1)}"
            )
            if not pullback_ok:
                reasons.append(f"buy blocked no pullback pullback={round(ind['buy_pullback_pips'],1)}")
                return False, reasons
        else:
            bounce_ok = ind["sell_bounce_pips"] >= SELL_BOUNCE_PIPS
            log(
                f"FILTER | pair={pair} side={side} result={bounce_ok} "
                f"reason={'sell bounce pass' if bounce_ok else 'sell blocked no bounce'} "
                f"bounce={round(ind['sell_bounce_pips'],1)}"
            )
            if not bounce_ok:
                reasons.append(f"sell blocked no bounce bounce={round(ind['sell_bounce_pips'],1)}")
                return False, reasons

    return True, reasons

def build_order_payload(pair: str, side: str, units: int, sl_pips: float, tp_pips: float, trailing: bool) -> Dict:
    bid, ask = get_pricing(pair)
    entry_price = ask if side == "BUY" else bid
    pip = pip_size(pair)

    if side == "BUY":
        sl_price = entry_price - (sl_pips * pip)
        tp_price = entry_price + (tp_pips * pip)
        signed_units = abs(units)
    else:
        sl_price = entry_price + (sl_pips * pip)
        tp_price = entry_price - (tp_pips * pip)
        signed_units = -abs(units)

    precision = int(PAIR_CONFIG[pair]["display_precision"])
    payload = {
        "order": {
            "instrument": pair,
            "units": str(signed_units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{sl_price:.{precision}f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.{precision}f}"},
        }
    }

    if trailing:
        # simple trailing stop distance = SL distance
        distance = sl_pips * pip
        payload["order"]["trailingStopLossOnFill"] = {"distance": f"{distance:.{precision}f}"}

    return payload

# =========================
# ROUTES
# =========================
@app.get("/")
def health():
    return jsonify({
        "status": "running",
        "env": OANDA_ENV,
        "allowed_pairs": sorted(list(ALLOWED_PAIRS)),
        "trend_filter": ENABLE_TREND_FILTER,
        "session_filter": ENABLE_SESSION_FILTER,
        "spread_filter": ENABLE_SPREAD_FILTER,
        "volatility_filter": ENABLE_VOLATILITY_FILTER,
        "momentum_filter": ENABLE_MOMENTUM_FILTER,
        "pullback_filter": ENABLE_PULLBACK_FILTER,
        "fixed_units": DEFAULT_FIXED_UNITS,
        "max_open_trades": MAX_OPEN_TRADES,
        "max_trades_per_pair": MAX_TRADES_PER_PAIR,
    }), 200

@app.post("/webhook")
def webhook():
    try:
        data = request.get_json(force=True, silent=False)
        if not isinstance(data, dict):
            raise ValueError("Invalid JSON payload")

        pair = str(data.get("pair", "")).strip().upper()
        side = str(data.get("side", "")).strip().upper()

        if pair not in ALLOWED_PAIRS:
            log(f"WEBHOOK ERROR: invalid pair {pair}")
            return jsonify({"ok": False, "error": f"invalid pair {pair}"}), 400

        if side not in {"BUY", "SELL"}:
            log(f"WEBHOOK ERROR: invalid side {side}")
            return jsonify({"ok": False, "error": f"invalid side {side}"}), 400

        risk = float(data.get("risk", 0.02))
        sl_pips = float(data.get("sl_pips", 15))
        tp_pips = float(data.get("tp_pips", 30))
        trailing = bool(data.get("trailing", True))

        log(f"WEBHOOK RECEIVED | pair={pair} side={side} risk={risk} sl={sl_pips} tp={tp_pips} trailing={trailing}")

        ok_slot, slot_reason = can_open_more(pair)
        if not ok_slot:
            log(f"TRADE BLOCKED | pair={pair} side={side} reason={slot_reason}")
            return jsonify({"ok": False, "blocked": slot_reason}), 200

        passed, reasons = apply_filters(pair, side)
        if not passed:
            reason = reasons[0] if reasons else "blocked by filters"
            log(f"TRADE BLOCKED | pair={pair} side={side} reason={reason}")
            return jsonify({"ok": False, "blocked": reason}), 200

        units = DEFAULT_FIXED_UNITS
        payload = build_order_payload(pair, side, units, sl_pips, tp_pips, trailing)

        log(f"PLACING ORDER | pair={pair} side={side} units={units}")
        result = oanda_post(f"/accounts/{OANDA_ACCOUNT_ID}/orders", payload)
        log(f"ORDER SUCCESS | pair={pair} side={side} response={json.dumps(result)[:500]}")

        return jsonify({"ok": True, "pair": pair, "side": side, "result": result}), 200

    except Exception as e:
        log(f"WEBHOOK ERROR: {str(e)}")
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    log(f"Starting Flask app on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
