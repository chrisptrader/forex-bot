from flask import Flask, request, jsonify
import os
import time
import json
import threading
import requests

app = Flask(__name__)

# =========================================================
# CONFIG
# =========================================================
OANDA_API_KEY = os.environ.get("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.environ.get("OANDA_ENV", "practice").strip().lower()

if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com/v3"
else:
    BASE_URL = "https://api-fxpractice.oanda.com/v3"

DEFAULT_PAIRS = ["GBP_USD", "USD_JPY"]
PAIRS_ENV = os.environ.get("PAIRS", "").strip()
PAIRS = [p.strip().upper() for p in PAIRS_ENV.split(",") if p.strip()] if PAIRS_ENV else DEFAULT_PAIRS

UNITS = int(os.environ.get("UNITS", "3000"))
AUTO_CHECK_SECONDS = int(os.environ.get("AUTO_CHECK_SECONDS", "10"))
MAX_TOTAL_OPEN_TRADES = int(os.environ.get("MAX_TOTAL_OPEN_TRADES", "2"))

EMA_PERIOD = int(os.environ.get("EMA_PERIOD", "20"))
TREND_GRANULARITY = os.environ.get("TREND_GRANULARITY", "M5").strip().upper()
MOMENTUM_LOOKBACK = int(os.environ.get("MOMENTUM_LOOKBACK", "3"))
BREAKOUT_LOOKBACK = int(os.environ.get("BREAKOUT_LOOKBACK", "5"))

STOP_LOSS = {
    "EUR_USD": 10,
    "GBP_USD": 10,
    "USD_JPY": 10,
    "XAU_USD": 250,
}

TAKE_PROFIT = {
    "EUR_USD": 24,
    "GBP_USD": 24,
    "USD_JPY": 22,
    "XAU_USD": 500,
}

BREAK_EVEN = {
    "EUR_USD": 8,
    "GBP_USD": 8,
    "USD_JPY": 7,
    "XAU_USD": 180,
}

TRAIL_AFTER = {
    "EUR_USD": 12,
    "GBP_USD": 12,
    "USD_JPY": 10,
    "XAU_USD": 250,
}

TRAIL_LOCK = {
    "EUR_USD": 6,
    "GBP_USD": 6,
    "USD_JPY": 5,
    "XAU_USD": 120,
}

managed_to_be = set()

# =========================================================
# HELPERS
# =========================================================
def log(message: str) -> None:
    print(message, flush=True)


def headers() -> dict:
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json",
    }


def normalize_pair(pair: str | None) -> str | None:
    if not pair:
        return None
    p = str(pair).strip().upper().replace("/", "_")
    mapping = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "USDJPY": "USD_JPY",
        "XAUUSD": "XAU_USD",
        "EUR_USD": "EUR_USD",
        "GBP_USD": "GBP_USD",
        "USD_JPY": "USD_JPY",
        "XAU_USD": "XAU_USD",
    }
    return mapping.get(p)


def normalize_signal(signal: str | None) -> str | None:
    if not signal:
        return None
    s = str(signal).strip().upper()
    return s if s in {"BUY", "SELL"} else None


def pip_size(pair: str) -> float:
    if pair == "USD_JPY":
        return 0.01
    if pair == "XAU_USD":
        return 0.01
    return 0.0001


def price_format(pair: str, price: float) -> str:
    if pair == "USD_JPY":
        return f"{price:.3f}"
    if pair == "XAU_USD":
        return f"{price:.2f}"
    return f"{price:.5f}"


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values for EMA")
    k = 2 / (period + 1)
    current_ema = sum(values[:period]) / period
    for v in values[period:]:
        current_ema = (v * k) + (current_ema * (1 - k))
    return current_ema


# =========================================================
# OANDA API
# =========================================================
def oanda_get(endpoint: str) -> dict:
    url = f"{BASE_URL}{endpoint}"
    r = requests.get(url, headers=headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def oanda_post(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    r = requests.post(url, headers=headers(), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def oanda_put(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    r = requests.put(url, headers=headers(), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def get_pricing(instruments: list[str]) -> dict[str, dict]:
    joined = ",".join(instruments)
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/pricing?instruments={joined}")
    out = {}
    for item in data.get("prices", []):
        instrument = item["instrument"]
        bid = float(item["bids"][0]["price"])
        ask = float(item["asks"][0]["price"])
        out[instrument] = {"bid": bid, "ask": ask}
    return out


def get_current_price(pair: str) -> dict:
    prices = get_pricing([pair])
    if pair not in prices:
        raise RuntimeError(f"No pricing returned for {pair}")
    return prices[pair]


def get_open_trades() -> list[dict]:
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    return data.get("trades", [])


def get_open_trade_for_pair(pair: str) -> dict | None:
    for trade in get_open_trades():
        if trade.get("instrument") == pair:
            return trade
    return None


def open_trade_slots_available() -> bool:
    return len(get_open_trades()) < MAX_TOTAL_OPEN_TRADES


def get_candles_raw(pair: str, granularity: str = "M5", count: int = 60) -> list[dict]:
    endpoint = f"/instruments/{pair}/candles?price=M&granularity={granularity}&count={count}"
    data = oanda_get(endpoint)

    candles = []
    for candle in data.get("candles", []):
        if candle.get("complete"):
            candles.append({
                "o": float(candle["mid"]["o"]),
                "h": float(candle["mid"]["h"]),
                "l": float(candle["mid"]["l"]),
                "c": float(candle["mid"]["c"]),
                "time": candle.get("time"),
            })
    return candles


# =========================================================
# FILTERS
# =========================================================
def trend_allowed(pair: str, signal: str) -> tuple[bool, dict]:
    candles = get_candles_raw(pair, granularity=TREND_GRANULARITY, count=max(EMA_PERIOD + 15, 50))
    closes = [c["c"] for c in candles]

    if len(closes) < EMA_PERIOD:
        return False, {"reason": "not enough candle data"}

    current_ema = ema(closes, EMA_PERIOD)
    last_close = closes[-1]

    info = {
        "pair": pair,
        "signal": signal,
        "last_close": round(last_close, 6),
        "ema": round(current_ema, 6),
        "granularity": TREND_GRANULARITY,
        "ema_period": EMA_PERIOD,
    }

    if signal == "BUY":
        allowed = last_close > current_ema
    else:
        allowed = last_close < current_ema

    return allowed, info


def momentum_allowed(pair: str, signal: str) -> tuple[bool, dict]:
    candles = get_candles_raw(pair, granularity=TREND_GRANULARITY, count=max(MOMENTUM_LOOKBACK + 3, 8))
    if len(candles) < MOMENTUM_LOOKBACK:
        return False, {"reason": "not enough momentum candles"}

    recent = candles[-MOMENTUM_LOOKBACK:]
    last = recent[-1]

    bullish_count = sum(1 for c in recent if c["c"] > c["o"])
    bearish_count = sum(1 for c in recent if c["c"] < c["o"])

    last_body = abs(last["c"] - last["o"])
    last_range = max(last["h"] - last["l"], 1e-9)
    body_ratio_ok = (last_body / last_range) >= 0.45

    info = {
        "pair": pair,
        "signal": signal,
        "lookback": MOMENTUM_LOOKBACK,
        "last_open": round(last["o"], 6),
        "last_close": round(last["c"], 6),
        "last_body": round(last_body, 6),
        "last_range": round(last_range, 6),
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
    }

    if signal == "BUY":
        direction_ok = last["c"] > last["o"] and bullish_count >= 2
    else:
        direction_ok = last["c"] < last["o"] and bearish_count >= 2

    return direction_ok and body_ratio_ok, info


def breakout_allowed(pair: str, signal: str) -> tuple[bool, dict]:
    candles = get_candles_raw(pair, granularity=TREND_GRANULARITY, count=max(BREAKOUT_LOOKBACK + 3, 10))
    if len(candles) < BREAKOUT_LOOKBACK + 1:
        return False, {"reason": "not enough breakout candles"}

    recent_closed = candles[-(BREAKOUT_LOOKBACK + 1):-1]
    last = candles[-1]

    recent_high = max(c["h"] for c in recent_closed)
    recent_low = min(c["l"] for c in recent_closed)

    info = {
        "pair": pair,
        "signal": signal,
        "lookback": BREAKOUT_LOOKBACK,
        "last_close": round(last["c"], 6),
        "recent_high": round(recent_high, 6),
        "recent_low": round(recent_low, 6),
    }

    if signal == "BUY":
        allowed = last["c"] > recent_high
    else:
        allowed = last["c"] < recent_low

    return allowed, info


# =========================================================
# WEBHOOK PARSER
# =========================================================
def parse_webhook_payload() -> dict:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    raw = request.get_data(as_text=True).strip()
    if not raw:
        raise ValueError("empty payload")

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            parsed2 = json.loads(parsed)
            if isinstance(parsed2, dict):
                return parsed2
    except Exception:
        pass

    cleaned = raw.strip().strip('"').replace('\\"', '"')
    parsed = json.loads(cleaned)
    if isinstance(parsed, dict):
        return parsed

    raise ValueError("invalid payload format")


# =========================================================
# TRADE LOGIC
# =========================================================
def place_trade(signal: str, pair: str) -> dict:
    signal = normalize_signal(signal)
    pair = normalize_pair(pair)

    if signal is None or pair is None:
        return {"status": "skipped", "reason": "invalid input"}

    if pair not in PAIRS:
        return {"status": "skipped", "reason": "pair not enabled"}

    if not open_trade_slots_available():
        return {"status": "skipped", "reason": "max trades reached"}

    if get_open_trade_for_pair(pair):
        return {"status": "skipped", "reason": "trade already open"}

    try:
        trend_ok, trend_info = trend_allowed(pair, signal)
        log(f"TREND CHECK: {trend_info}")
        if not trend_ok:
            log(f"SKIPPED {pair} {signal}: trend filter blocked")
            return {"status": "skipped", "reason": "trend filter blocked", "trend": trend_info}

        momentum_ok, momentum_info = momentum_allowed(pair, signal)
        log(f"MOMENTUM CHECK: {momentum_info}")
        if not momentum_ok:
            log(f"SKIPPED {pair} {signal}: momentum filter blocked")
            return {
                "status": "skipped",
                "reason": "momentum filter blocked",
                "trend": trend_info,
                "momentum": momentum_info,
            }

        breakout_ok, breakout_info = breakout_allowed(pair, signal)
        log(f"BREAKOUT CHECK: {breakout_info}")
        if not breakout_ok:
            log(f"SKIPPED {pair} {signal}: breakout filter blocked")
            return {
                "status": "skipped",
                "reason": "breakout filter blocked",
                "trend": trend_info,
                "momentum": momentum_info,
                "breakout": breakout_info,
            }

        px = get_current_price(pair)
        bid = px["bid"]
        ask = px["ask"]
        entry = ask if signal == "BUY" else bid

        pip = pip_size(pair)
        sl_pips = STOP_LOSS[pair]
        tp_pips = TAKE_PROFIT[pair]

        if signal == "BUY":
            units = UNITS
            sl_price = entry - (sl_pips * pip)
            tp_price = entry + (tp_pips * pip)
        else:
            units = -UNITS
            sl_price = entry + (sl_pips * pip)
            tp_price = entry - (tp_pips * pip)

        payload = {
            "order": {
                "instrument": pair,
                "units": str(units),
                "type": "MARKET",
                "positionFill": "DEFAULT",
                "timeInForce": "FOK",
                "stopLossOnFill": {
                    "price": price_format(pair, sl_price)
                },
                "takeProfitOnFill": {
                    "price": price_format(pair, tp_price)
                }
            }
        }

        log(f"V11 EXECUTION: {signal} {pair}")
        log(f"{pair} sending order: {payload}")

        response = oanda_post(f"/accounts/{OANDA_ACCOUNT_ID}/orders", payload)

        log(f"{pair} trade placed")
        log(f"{pair} response: {response}")

        return {
            "status": "placed",
            "pair": pair,
            "signal": signal,
            "trend": trend_info,
            "momentum": momentum_info,
            "breakout": breakout_info,
            "response": response,
        }

    except Exception as e:
        log(f"TRADE ERROR: {e}")
        return {"status": "error", "message": str(e)}


# =========================================================
# TRADE MANAGEMENT
# =========================================================
def manage_open_trades() -> None:
    trades = get_open_trades()
    if not trades:
        return

    pricing = get_pricing(PAIRS)

    for trade in trades:
        try:
            pair = trade["instrument"]
            trade_id = trade["id"]
            entry = float(trade["price"])
            units = float(trade["currentUnits"])

            px = pricing.get(pair)
            if not px:
                continue

            if units > 0:
                current = px["bid"]
                pips_profit = (current - entry) / pip_size(pair)
            else:
                current = px["ask"]
                pips_profit = (entry - current) / pip_size(pair)

            log(f"{pair} pips in profit: {pips_profit}")

            if pips_profit >= BREAK_EVEN[pair] and trade_id not in managed_to_be:
                be_payload = {
                    "stopLoss": {
                        "price": price_format(pair, entry)
                    }
                }

                response = oanda_put(
                    f"/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders",
                    be_payload,
                )
                managed_to_be.add(trade_id)
                log(f"{pair} moved to breakeven: {response}")

            if pips_profit >= TRAIL_AFTER[pair]:
                pip = pip_size(pair)

                if units > 0:
                    new_sl = current - (TRAIL_LOCK[pair] * pip)
                else:
                    new_sl = current + (TRAIL_LOCK[pair] * pip)

                trail_payload = {
                    "stopLoss": {
                        "price": price_format(pair, new_sl)
                    }
                }

                response = oanda_put(
                    f"/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders",
                    trail_payload,
                )
                log(f"{pair} trailing stop updated: {response}")

        except Exception as e:
            log(f"manage error: {e}")


def auto_loop() -> None:
    while True:
        try:
            manage_open_trades()
        except Exception as e:
            log(f"manage loop error: {e}")
        time.sleep(AUTO_CHECK_SECONDS)


# =========================================================
# ROUTES
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "bot": "OANDA Runner V11 Final",
        "env": OANDA_ENV,
        "pairs": PAIRS,
        "trend_granularity": TREND_GRANULARITY,
        "ema_period": EMA_PERIOD,
        "momentum_lookback": MOMENTUM_LOOKBACK,
        "breakout_lookback": BREAKOUT_LOOKBACK,
        "max_total_open_trades": MAX_TOTAL_OPEN_TRADES,
        "status": "running",
        "units": UNITS,
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = parse_webhook_payload()
        signal = data.get("signal")
        pair = data.get("pair")
        result = place_trade(signal, pair)
        return jsonify(result), 200
    except Exception as e:
        log(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400


# =========================================================
# START
# =========================================================
threading.Thread(target=auto_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
