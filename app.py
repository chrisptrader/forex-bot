from flask import Flask, request, jsonify
import os
import time
import json
import threading
import datetime
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

# Active pairs for this V8
PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "XAU_USD"]

# Safer starting size
UNITS = int(os.environ.get("UNITS", "3000"))

# Timeframe used for filters
GRANULARITY = os.environ.get("GRANULARITY", "M5").strip().upper()

# Risk / trade management
STOP_LOSS = {
    "EUR_USD": 12,
    "GBP_USD": 12,
    "USD_JPY": 12,
    "XAU_USD": 250,
}

TAKE_PROFIT = {
    "EUR_USD": 20,
    "GBP_USD": 20,
    "USD_JPY": 18,
    "XAU_USD": 400,
}

BREAK_EVEN = {
    "EUR_USD": 8,
    "GBP_USD": 8,
    "USD_JPY": 7,
    "XAU_USD": 180,
}

MAX_SPREAD = {
    "EUR_USD": 0.00025,
    "GBP_USD": 0.00035,
    "USD_JPY": 0.025,
    "XAU_USD": 1.20,
}

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "900"))
AUTO_CHECK_SECONDS = int(os.environ.get("AUTO_CHECK_SECONDS", "10"))
MAX_TOTAL_OPEN_TRADES = int(os.environ.get("MAX_TOTAL_OPEN_TRADES", "2"))

# Session filter in UTC
LONDON_START = 7
NEW_YORK_END = 20

# Trend filter settings
FAST_EMA = 50
SLOW_EMA = 200
CANDLE_COUNT = 220

last_trade_time = {}

# =========================================================
# HELPERS
# =========================================================
def log(msg: str) -> None:
    print(msg, flush=True)


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


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    ema_value = sum(values[:period]) / period

    for price in values[period:]:
        ema_value = (price - ema_value) * multiplier + ema_value

    return ema_value


def in_session() -> bool:
    hour = datetime.datetime.utcnow().hour
    return LONDON_START <= hour <= NEW_YORK_END


# =========================================================
# OANDA API
# =========================================================
def oanda_get(endpoint: str) -> dict:
    url = f"{BASE_URL}{endpoint}"
    r = requests.get(url, headers=headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def oanda_post(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    r = requests.post(url, headers=headers(), json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def oanda_put(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    r = requests.put(url, headers=headers(), json=payload, timeout=20)
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


def get_spread(pair: str) -> float:
    px = get_current_price(pair)
    return px["ask"] - px["bid"]


def spread_allowed(pair: str) -> tuple[bool, float]:
    spread = get_spread(pair)
    return spread <= MAX_SPREAD[pair], spread


def get_open_trades() -> list[dict]:
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    return data.get("trades", [])


def get_open_trade_for_pair(pair: str) -> dict | None:
    for trade in get_open_trades():
        if trade.get("instrument") == pair:
            return trade
    return None


def get_candles(pair: str, count: int = CANDLE_COUNT, granularity: str = GRANULARITY) -> list[dict]:
    data = oanda_get(
        f"/instruments/{pair}/candles?price=M&granularity={granularity}&count={count}"
    )
    return data.get("candles", [])


# =========================================================
# STRATEGY FILTERS
# =========================================================
def candle_closes(pair: str) -> list[float]:
    candles = get_candles(pair)
    closes = []

    for c in candles:
        if c.get("complete"):
            closes.append(float(c["mid"]["c"]))

    return closes


def last_two_complete_candles(pair: str) -> tuple[dict | None, dict | None]:
    candles = get_candles(pair)
    complete = [c for c in candles if c.get("complete")]

    if len(complete) < 2:
        return None, None

    return complete[-2], complete[-1]


def trend_ok(signal: str, pair: str) -> tuple[bool, str]:
    closes = candle_closes(pair)

    if len(closes) < SLOW_EMA + 5:
        return False, "not enough candles"

    fast = ema(closes, FAST_EMA)
    slow = ema(closes, SLOW_EMA)
    last_close = closes[-1]

    if fast is None or slow is None:
        return False, "ema unavailable"

    # Direction filter
    if signal == "BUY":
        if not (last_close > fast > slow):
            return False, f"trend blocked buy last={last_close} fast={fast} slow={slow}"
    else:
        if not (last_close < fast < slow):
            return False, f"trend blocked sell last={last_close} fast={fast} slow={slow}"

    # Momentum confirmation: last closed candle direction
    prev_candle, last_candle = last_two_complete_candles(pair)
    if not prev_candle or not last_candle:
        return False, "candle confirmation unavailable"

    o = float(last_candle["mid"]["o"])
    c = float(last_candle["mid"]["c"])

    if signal == "BUY" and c <= o:
        return False, "last candle not bullish"
    if signal == "SELL" and c >= o:
        return False, "last candle not bearish"

    return True, "trend ok"


def cooldown_active(pair: str) -> bool:
    last = last_trade_time.get(pair, 0)
    return (time.time() - last) < COOLDOWN_SECONDS


def open_trade_slots_available() -> bool:
    return len(get_open_trades()) < MAX_TOTAL_OPEN_TRADES


# =========================================================
# WEBHOOK PARSING
# =========================================================
def parse_webhook_payload() -> dict:
    # 1) direct JSON object
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    # 2) raw body
    raw = request.get_data(as_text=True).strip()

    if not raw:
        raise ValueError("empty payload")

    # 3) normal JSON object in string body
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed

        # 4) JSON string containing JSON
        if isinstance(parsed, str):
            parsed2 = json.loads(parsed)
            if isinstance(parsed2, dict):
                return parsed2
    except Exception:
        pass

    # 5) manual cleanup for weird wrapped strings
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

    if signal is None:
        return {"status": "skipped", "reason": "invalid signal"}

    if pair is None:
        return {"status": "skipped", "reason": "invalid pair"}

    if pair not in PAIRS:
        return {"status": "skipped", "reason": f"pair not enabled: {pair}"}

    if not in_session():
        return {"status": "skipped", "reason": "outside session"}

    if not open_trade_slots_available():
        return {"status": "skipped", "reason": "max total open trades reached"}

    if get_open_trade_for_pair(pair):
        return {"status": "skipped", "reason": f"{pair} already has open trade"}

    if cooldown_active(pair):
        return {"status": "skipped", "reason": f"{pair} cooldown active"}

    spread_ok, spread = spread_allowed(pair)
    if not spread_ok:
        return {"status": "skipped", "reason": f"{pair} spread too high: {spread}"}

    trend_pass, trend_reason = trend_ok(signal, pair)
    if not trend_pass:
        return {"status": "skipped", "reason": trend_reason}

    px = get_current_price(pair)
    bid = px["bid"]
    ask = px["ask"]
    entry = ask if signal == "BUY" else bid

    sl_pips = STOP_LOSS[pair]
    tp_pips = TAKE_PROFIT[pair]
    pip = pip_size(pair)

    if signal == "BUY":
        units = UNITS
        sl_price = entry - (sl_pips * pip)
        tp_price = entry + (tp_pips * pip)
    else:
        units = -UNITS
        sl_price = entry + (sl_pips * pip)
        tp_price = entry - (tp_pips * pip)

    order = {
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

    log(f"Webhook accepted: signal={signal} pair={pair}")
    log(f"{pair} Sending: {order}")

    response = oanda_post(f"/accounts/{OANDA_ACCOUNT_ID}/orders", order)
    log(f"{pair} Response: {response}")

    last_trade_time[pair] = time.time()

    return {"status": "placed", "pair": pair, "signal": signal, "response": response}


# =========================================================
# TRADE MANAGEMENT
# =========================================================
def check_breakeven() -> None:
    trades = get_open_trades()
    if not trades:
        return

    prices = get_pricing(PAIRS)

    for trade in trades:
        try:
            pair = trade["instrument"]
            if pair not in BREAK_EVEN:
                continue

            trade_id = trade["id"]
            entry = float(trade["price"])
            units = float(trade["currentUnits"])

            px = prices.get(pair)
            if not px:
                continue

            current = px["bid"] if units > 0 else px["ask"]
            pip = pip_size(pair)
            pips_profit = abs(current - entry) / pip

            log(f"{pair} pips in profit: {pips_profit}")

            if pips_profit >= BREAK_EVEN[pair]:
                payload = {
                    "stopLoss": {
                        "price": price_format(pair, entry)
                    }
                }

                response = oanda_put(
                    f"/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders",
                    payload,
                )
                log(f"🔥 {pair} moved to breakeven: {response}")

        except Exception as e:
            log(f"BE error on trade: {e}")


def auto_loop() -> None:
    while True:
        try:
            check_breakeven()
        except Exception as e:
            log(f"BE loop error: {e}")

        time.sleep(AUTO_CHECK_SECONDS)


# =========================================================
# ROUTES
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "bot": "OANDA Runner Real V8",
        "env": OANDA_ENV,
        "pairs": PAIRS,
        "status": "running",
        "units": UNITS,
        "granularity": GRANULARITY,
        "max_total_open_trades": MAX_TOTAL_OPEN_TRADES,
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = parse_webhook_payload()

        signal = data.get("signal")
        pair = data.get("pair")

        result = place_trade(signal, pair)

        code = 200 if result["status"] in {"placed", "skipped"} else 400
        return jsonify(result), code

    except Exception as e:
        log(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400


# =========================================================
# START
# =========================================================
threading.Thread(target=auto_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
