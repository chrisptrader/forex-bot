import os
import time
import math
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234").strip()

PORT = int(os.getenv("PORT", "10000"))

# Risk / order settings
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.02"))
DEFAULT_SL_PIPS = float(os.getenv("DEFAULT_SL_PIPS", "20"))
DEFAULT_TP_PIPS = float(os.getenv("DEFAULT_TP_PIPS", "50"))
ENABLE_TRAILING = os.getenv("ENABLE_TRAILING", "true").strip().lower() == "true"

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_TOTAL_OPEN_TRADES = int(os.getenv("MAX_TOTAL_OPEN_TRADES", "2"))
ENABLE_SPREAD_FILTER = os.getenv("ENABLE_SPREAD_FILTER", "true").strip().lower() == "true"
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "12"))

MIN_CANDLE_RANGE_PIPS = float(os.getenv("MIN_CANDLE_RANGE_PIPS", "1"))
ENABLE_VOLATILITY_FILTER = os.getenv("ENABLE_VOLATILITY_FILTER", "true").strip().lower() == "true"
MIN_VOLATILITY_PIPS = float(os.getenv("MIN_VOLATILITY_PIPS", "1.5"))

ENABLE_TREND_FILTER = os.getenv("ENABLE_TREND_FILTER", "false").strip().lower() == "true"
ENABLE_MOMENTUM_FILTER = os.getenv("ENABLE_MOMENTUM_FILTER", "false").strip().lower() == "true"

EMA_PERIOD = int(os.getenv("EMA_PERIOD", "50"))
CANDLE_GRANULARITY = os.getenv("CANDLE_GRANULARITY", "M5").strip().upper()
CANDLE_LOOKBACK = int(os.getenv("CANDLE_LOOKBACK", "30"))

PULLBACK_PIPS = float(os.getenv("PULLBACK_PIPS", "0.8"))
BOUNCE_PIPS = float(os.getenv("BOUNCE_PIPS", "0.8"))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))

# V21 trade management
ENABLE_V21_MANAGER = os.getenv("ENABLE_V21_MANAGER", "true").strip().lower() == "true"

BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "10"))

LOCK_1_TRIGGER_PIPS = float(os.getenv("LOCK_1_TRIGGER_PIPS", "15"))
LOCK_1_LOCK_PIPS = float(os.getenv("LOCK_1_LOCK_PIPS", "5"))

LOCK_2_TRIGGER_PIPS = float(os.getenv("LOCK_2_TRIGGER_PIPS", "25"))
LOCK_2_LOCK_PIPS = float(os.getenv("LOCK_2_LOCK_PIPS", "15"))

LOCK_3_TRIGGER_PIPS = float(os.getenv("LOCK_3_TRIGGER_PIPS", "35"))
LOCK_3_LOCK_PIPS = float(os.getenv("LOCK_3_LOCK_PIPS", "25"))

# =========================
# OANDA URL
# =========================
if OANDA_ENV == "live":
    OANDA_BASE_URL = "https://api-fxtrade.oanda.com/v3"
else:
    OANDA_BASE_URL = "https://api-fxpractice.oanda.com/v3"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

# =========================
# HELPERS
# =========================
def log(msg: str) -> None:
    print(msg, flush=True)


def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair.upper() else 0.0001


def price_precision(pair: str) -> int:
    return 3 if "JPY" in pair.upper() else 5


def format_price(pair: str, price: float) -> str:
    return f"{price:.{price_precision(pair)}f}"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def oanda_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{OANDA_BASE_URL}{path}"
    response = requests.get(url, headers=HEADERS, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def oanda_put(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{OANDA_BASE_URL}{path}"
    response = requests.put(url, headers=HEADERS, json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def oanda_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{OANDA_BASE_URL}{path}"
    response = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def get_account_balance() -> float:
    try:
        data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/summary")
        return safe_float(data["account"]["balance"])
    except Exception as e:
        log(f"BALANCE ERROR: {e}")
        return 0.0


def get_open_trades() -> List[Dict[str, Any]]:
    try:
        data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/openTrades")
        return data.get("trades", [])
    except Exception as e:
        log(f"OPEN TRADES ERROR: {e}")
        return []


def get_open_trades_for_pair(pair: str) -> List[Dict[str, Any]]:
    pair = pair.upper()
    return [t for t in get_open_trades() if t.get("instrument") == pair]


def get_candles(pair: str, count: int = 30, granularity: str = "M5") -> List[Dict[str, Any]]:
    data = oanda_get(
        f"/instruments/{pair}/candles",
        params={"count": count, "price": "MBA", "granularity": granularity},
    )
    return data.get("candles", [])


def get_bid_ask(pair: str) -> Tuple[float, float]:
    data = oanda_get(
        f"/accounts/{OANDA_ACCOUNT_ID}/pricing",
        params={"instruments": pair},
    )
    prices = data.get("prices", [])
    if not prices:
        raise ValueError(f"No pricing returned for {pair}")

    price = prices[0]
    bid = safe_float(price["bids"][0]["price"])
    ask = safe_float(price["asks"][0]["price"])
    return bid, ask


def get_mid_price(pair: str) -> float:
    bid, ask = get_bid_ask(pair)
    return (bid + ask) / 2.0


def spread_in_pips(pair: str) -> float:
    bid, ask = get_bid_ask(pair)
    return abs(ask - bid) / pip_size(pair)


def latest_completed_candle_range_pips(pair: str) -> float:
    candles = get_candles(pair, count=3, granularity=CANDLE_GRANULARITY)
    completed = [c for c in candles if c.get("complete")]

    if not completed:
        return 0.0

    c = completed[-1]
    high = safe_float(c["mid"]["h"])
    low = safe_float(c["mid"]["l"])
    return abs(high - low) / pip_size(pair)


def recent_volatility_pips(pair: str, lookback: int = 5) -> float:
    candles = get_candles(pair, count=max(lookback + 2, 10), granularity=CANDLE_GRANULARITY)
    completed = [c for c in candles if c.get("complete")]
    if len(completed) < lookback:
        return 0.0

    recent = completed[-lookback:]
    highs = [safe_float(c["mid"]["h"]) for c in recent]
    lows = [safe_float(c["mid"]["l"]) for c in recent]

    if not highs or not lows:
        return 0.0

    return (max(highs) - min(lows)) / pip_size(pair)


def ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / max(len(values), 1)

    k = 2 / (period + 1)
    ema_value = values[0]
    for v in values[1:]:
        ema_value = v * k + ema_value * (1 - k)
    return ema_value


def trend_filter_pass(pair: str, side: str) -> bool:
    if not ENABLE_TREND_FILTER:
        log(f"FILTER | pair={pair} side={side} result=True reason=trend filter off")
        return True

    candles = get_candles(pair, count=max(EMA_PERIOD + 5, 60), granularity=CANDLE_GRANULARITY)
    completed = [c for c in candles if c.get("complete")]
    closes = [safe_float(c["mid"]["c"]) for c in completed]

    if not closes:
        log(f"FILTER | pair={pair} side={side} result=False reason=no closes for trend")
        return False

    ema_value = ema(closes, EMA_PERIOD)
    current = closes[-1]

    if side == "BUY":
        result = current > ema_value
    else:
        result = current < ema_value

    log(f"FILTER | pair={pair} side={side} result={result} reason=trend ema={format_price(pair, ema_value)} close={format_price(pair, current)}")
    return result


def momentum_filter_pass(pair: str, side: str) -> bool:
    if not ENABLE_MOMENTUM_FILTER:
        log(f"FILTER | pair={pair} side={side} result=True reason=momentum filter off")
        return True

    candles = get_candles(pair, count=5, granularity=CANDLE_GRANULARITY)
    completed = [c for c in candles if c.get("complete")]
    if len(completed) < 2:
        log(f"FILTER | pair={pair} side={side} result=False reason=not enough candles momentum")
        return False

    prev_close = safe_float(completed[-2]["mid"]["c"])
    last_close = safe_float(completed[-1]["mid"]["c"])

    if side == "BUY":
        result = last_close > prev_close
    else:
        result = last_close < prev_close

    log(f"FILTER | pair={pair} side={side} result={result} reason=momentum prev={format_price(pair, prev_close)} last={format_price(pair, last_close)}")
    return result


def structure_filter_pass(pair: str, side: str) -> bool:
    candles = get_candles(pair, count=CANDLE_LOOKBACK, granularity=CANDLE_GRANULARITY)
    completed = [c for c in candles if c.get("complete")]
    if len(completed) < 6:
        log(f"FILTER | pair={pair} side={side} result=False reason=not enough candles structure")
        return False

    last = completed[-1]
    previous = completed[-6:-1]

    last_close = safe_float(last["mid"]["c"])
    hh = max(safe_float(c["mid"]["h"]) for c in previous)
    ll = min(safe_float(c["mid"]["l"]) for c in previous)

    pips = pip_size(pair)

    if side == "BUY":
        pullback = (hh - last_close) / pips
        result = last_close >= hh - (PULLBACK_PIPS * pips)
        if result:
            log(f"FILTER | pair={pair} side={side} result=True reason=buy structure pass hh={format_price(pair, hh)} pullback={round(pullback, 1)}")
        else:
            log(f"FILTER | pair={pair} side={side} result=False reason=buy blocked no breakout/pullback pullback={round(pullback, 1)}")
        return result

    bounce = (last_close - ll) / pips
    result = last_close <= ll + (BOUNCE_PIPS * pips)
    if result:
        log(f"FILTER | pair={pair} side={side} result=True reason=sell structure pass ll={format_price(pair, ll)} bounce={round(bounce, 1)}")
    else:
        log(f"FILTER | pair={pair} side={side} result=False reason=sell blocked no bounce breakdown bounce={round(bounce, 1)}")
    return result


def count_total_open_trades() -> int:
    return len(get_open_trades())


def has_opposite_trade_on_pair(pair: str, side: str) -> bool:
    trades = get_open_trades_for_pair(pair)
    for t in trades:
        units = safe_float(t.get("currentUnits"))
        existing_side = "BUY" if units > 0 else "SELL"
        if existing_side != side:
            return True
    return False


def same_side_trade_exists(pair: str, side: str) -> bool:
    trades = get_open_trades_for_pair(pair)
    for t in trades:
        units = safe_float(t.get("currentUnits"))
        existing_side = "BUY" if units > 0 else "SELL"
        if existing_side == side:
            return True
    return False


def calculate_units(pair: str, side: str, sl_pips: float) -> int:
    balance = get_account_balance()
    risk_amount = balance * RISK_PERCENT

    # Simple pip value approximation for small demo sizing
    if "JPY" in pair:
        pip_value_per_1k = 0.63
    else:
        pip_value_per_1k = 0.10

    if sl_pips <= 0:
        sl_pips = DEFAULT_SL_PIPS

    units_in_thousands = risk_amount / max(sl_pips * pip_value_per_1k, 0.0001)
    units = int(max(1, round(units_in_thousands)) * 1000)

    # Keep it controlled for your current setup
    units = min(units, 5000)
    return units if side == "BUY" else -units


def build_order_payload(pair: str, side: str, units: int, sl_pips: float, tp_pips: float) -> Dict[str, Any]:
    bid, ask = get_bid_ask(pair)
    entry = ask if side == "BUY" else bid
    pips = pip_size(pair)

    if side == "BUY":
        sl_price = entry - (sl_pips * pips)
        tp_price = entry + (tp_pips * pips)
    else:
        sl_price = entry + (sl_pips * pips)
        tp_price = entry - (tp_pips * pips)

    return {
        "order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": format_price(pair, sl_price),
                "timeInForce": "GTC",
            },
            "takeProfitOnFill": {
                "price": format_price(pair, tp_price),
                "timeInForce": "GTC",
            },
        }
    }


def place_trade(pair: str, side: str, sl_pips: float, tp_pips: float) -> Dict[str, Any]:
    units = calculate_units(pair, side, sl_pips)
    payload = build_order_payload(pair, side, units, sl_pips, tp_pips)
    result = oanda_post(f"/accounts/{OANDA_ACCOUNT_ID}/orders", payload)
    log(f"TRADE OPENED | pair={pair} side={side} units={abs(units)}")
    return result


def replace_sl(trade_id: str, pair: str, new_sl: float) -> None:
    payload = {
        "stopLoss": {
            "timeInForce": "GTC",
            "price": format_price(pair, new_sl),
        }
    }
    try:
        oanda_put(f"/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders", payload)
    except Exception as e:
        log(f"V21 SL UPDATE ERROR | trade_id={trade_id} error={e}")


def unrealized_pips_for_trade(trade: Dict[str, Any]) -> float:
    pair = trade.get("instrument", "")
    pips = pip_size(pair)

    units = safe_float(trade.get("currentUnits"))
    if units == 0:
        return 0.0

    entry = safe_float(trade.get("price"))
    bid, ask = get_bid_ask(pair)

    if units > 0:
        current = bid
        return (current - entry) / pips
    else:
        current = ask
        return (entry - current) / pips


def get_trade_side(trade: Dict[str, Any]) -> str:
    units = safe_float(trade.get("currentUnits"))
    return "BUY" if units > 0 else "SELL"


def get_trade_sl_price(trade: Dict[str, Any]) -> Optional[float]:
    sl_order = trade.get("stopLossOrder")
    if not sl_order:
        return None
    return safe_float(sl_order.get("price"), 0.0)


def compute_locked_sl_price(pair: str, side: str, entry: float, lock_pips: float) -> float:
    pips = pip_size(pair)
    if side == "BUY":
        return entry + (lock_pips * pips)
    return entry - (lock_pips * pips)


def v21_manage_trade(trade: Dict[str, Any]) -> None:
    pair = trade.get("instrument", "")
    side = get_trade_side(trade)
    trade_id = trade.get("id")
    entry = safe_float(trade.get("price"))
    current_sl = get_trade_sl_price(trade)
    profit_pips = unrealized_pips_for_trade(trade)

    if not trade_id or not pair or entry <= 0:
        return

    new_target_sl = None

    if profit_pips >= LOCK_3_TRIGGER_PIPS:
        new_target_sl = compute_locked_sl_price(pair, side, entry, LOCK_3_LOCK_PIPS)
    elif profit_pips >= LOCK_2_TRIGGER_PIPS:
        new_target_sl = compute_locked_sl_price(pair, side, entry, LOCK_2_LOCK_PIPS)
    elif profit_pips >= LOCK_1_TRIGGER_PIPS:
        new_target_sl = compute_locked_sl_price(pair, side, entry, LOCK_1_LOCK_PIPS)
    elif profit_pips >= BREAK_EVEN_TRIGGER_PIPS:
        new_target_sl = entry

    if new_target_sl is None:
        return

    should_update = False
    if current_sl is None or current_sl == 0:
        should_update = True
    else:
        if side == "BUY" and new_target_sl > current_sl:
            should_update = True
        elif side == "SELL" and new_target_sl < current_sl:
            should_update = True

    if should_update:
        replace_sl(str(trade_id), pair, new_target_sl)
        log(
            f"V21 MANAGER | pair={pair} trade_id={trade_id} side={side} "
            f"profit_pips={round(profit_pips,1)} new_sl={format_price(pair, new_target_sl)}"
        )


def manager_loop() -> None:
    while True:
        try:
            if ENABLE_V21_MANAGER:
                trades = get_open_trades()
                for trade in trades:
                    v21_manage_trade(trade)
        except Exception as e:
            log(f"MANAGER LOOP ERROR: {e}")
        time.sleep(POLL_SECONDS)

# =========================
# VALIDATION / FILTERS
# =========================
def validate_payload(data: Dict[str, Any]) -> Tuple[bool, str]:
    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return False, "invalid passphrase"

    pair = str(data.get("pair", "")).upper().strip()
    side = str(data.get("side", "")).upper().strip()

    if not pair:
        return False, "missing pair"
    if side not in {"BUY", "SELL"}:
        return False, "side must be BUY or SELL"

    return True, "ok"


def passes_filters(pair: str, side: str) -> Tuple[bool, str]:
    try:
        # Max total trades
        total_open = count_total_open_trades()
        if total_open >= MAX_TOTAL_OPEN_TRADES:
            reason = f"max total open trades reached {MAX_TOTAL_OPEN_TRADES}"
            log(f"TRADE BLOCKED | pair={pair} side={side} reason={reason}")
            return False, reason

        # Per pair cap
        pair_open = len(get_open_trades_for_pair(pair))
        if pair_open >= MAX_OPEN_TRADES:
            reason = f"max open trades reached {MAX_OPEN_TRADES}"
            log(f"TRADE BLOCKED | pair={pair} side={side} reason={reason}")
            return False, reason

        # Avoid FIFO / opposite-side conflict
        if has_opposite_trade_on_pair(pair, side):
            reason = "opposite trade already open on pair"
            log(f"TRADE BLOCKED | pair={pair} side={side} reason={reason}")
            return False, reason

        # Spread filter
        current_spread = spread_in_pips(pair)
        if ENABLE_SPREAD_FILTER:
            spread_ok = current_spread <= MAX_SPREAD_PIPS
            log(f"FILTER | pair={pair} side={side} result={spread_ok} reason={'spread pass' if spread_ok else 'spread blocked'} spread={round(current_spread,1)} pips")
            if not spread_ok:
                return False, f"spread blocked spread={round(current_spread,1)} pips"

        # Candle range filter
        candle_range = latest_completed_candle_range_pips(pair)
        candle_ok = candle_range >= MIN_CANDLE_RANGE_PIPS
        log(f"FILTER | pair={pair} side={side} result={candle_ok} reason={'candle range pass' if candle_ok else 'candle too small'}={round(candle_range,1)}")
        if not candle_ok:
            return False, f"candle too small range={round(candle_range,1)}"

        # Volatility filter
        if ENABLE_VOLATILITY_FILTER:
            vol = recent_volatility_pips(pair, lookback=5)
            vol_ok = vol >= MIN_VOLATILITY_PIPS
            log(f"FILTER | pair={pair} side={side} result={vol_ok} reason={'volatility pass' if vol_ok else 'volatility blocked'} range={round(vol,1)} pips")
            if not vol_ok:
                return False, f"volatility blocked range={round(vol,1)} pips"

        if not trend_filter_pass(pair, side):
            return False, "trend blocked"

        if not momentum_filter_pass(pair, side):
            return False, "momentum blocked"

        if not structure_filter_pass(pair, side):
            return False, "structure blocked"

        return True, "all filters passed"

    except Exception as e:
        log(f"FILTER ERROR | pair={pair} side={side} error={e}")
        return False, f"filter error: {e}"

# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "service": "forex-bot-v21",
        "env": OANDA_ENV,
        "manager": ENABLE_V21_MANAGER,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        valid, message = validate_payload(data)
        if not valid:
            return jsonify({"ok": False, "error": message}), 400

        pair = str(data.get("pair", "")).upper().strip()
        side = str(data.get("side", "")).upper().strip()
        sl_pips = safe_float(data.get("sl", DEFAULT_SL_PIPS), DEFAULT_SL_PIPS)
        tp_pips = safe_float(data.get("tp", DEFAULT_TP_PIPS), DEFAULT_TP_PIPS)
        trailing = bool(data.get("trailing", ENABLE_TRAILING))

        log(f"WEBHOOK RECEIVED | pair={pair} side={side} risk={RISK_PERCENT} sl={sl_pips} tp={tp_pips} trailing={trailing}")

        allowed, reason = passes_filters(pair, side)
        if not allowed:
            return jsonify({"ok": True, "blocked": True, "reason": reason}), 200

        result = place_trade(pair, side, sl_pips, tp_pips)
        return jsonify({"ok": True, "placed": True, "pair": pair, "side": side, "result": result}), 200

    except requests.HTTPError as e:
        text = ""
        try:
            text = e.response.text
        except Exception:
            pass
        log(f"WEBHOOK HTTP ERROR: {e} | body={text}")
        return jsonify({"ok": False, "error": str(e), "body": text}), 500

    except Exception as e:
        log(f"WEBHOOK ERROR: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================
# MAIN
# =========================
def validate_startup() -> None:
    missing = []
    if not OANDA_API_KEY:
        missing.append("OANDA_API_KEY")
    if not OANDA_ACCOUNT_ID:
        missing.append("OANDA_ACCOUNT_ID")

    if missing:
        log(f"STARTUP WARNING: missing env vars: {', '.join(missing)}")
    else:
        log("STARTUP OK: OANDA credentials loaded")


if __name__ == "__main__":
    validate_startup()

    manager_thread = threading.Thread(target=manager_loop, daemon=True)
    manager_thread.start()

    app.run(host="0.0.0.0", port=PORT)
