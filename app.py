from flask import Flask, request, jsonify
import os
import time
import json
import threading
import datetime as dt
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

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "XAU_USD"]

UNITS = int(os.environ.get("UNITS", "3000"))
ENTRY_GRANULARITY = os.environ.get("GRANULARITY", "M5").strip().upper()

AUTO_CHECK_SECONDS = int(os.environ.get("AUTO_CHECK_SECONDS", "10"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "900"))
REENTRY_BLOCK_SECONDS = int(os.environ.get("REENTRY_BLOCK_SECONDS", "1800"))

MAX_TOTAL_OPEN_TRADES = int(os.environ.get("MAX_TOTAL_OPEN_TRADES", "2"))
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "6"))
MAX_LOSSES_PER_DAY = int(os.environ.get("MAX_LOSSES_PER_DAY", "3"))

# UTC session hours
SESSION_START_UTC = int(os.environ.get("SESSION_START_UTC", "7"))
SESSION_END_UTC = int(os.environ.get("SESSION_END_UTC", "20"))

STOP_LOSS = {
    "EUR_USD": 12,
    "GBP_USD": 14,
    "USD_JPY": 12,
    "XAU_USD": 250,
}

TAKE_PROFIT = {
    "EUR_USD": 24,
    "GBP_USD": 28,
    "USD_JPY": 20,
    "XAU_USD": 500,
}

BREAK_EVEN = {
    "EUR_USD": 8,
    "GBP_USD": 10,
    "USD_JPY": 7,
    "XAU_USD": 180,
}

TRAIL_AFTER = {
    "EUR_USD": 12,
    "GBP_USD": 14,
    "USD_JPY": 10,
    "XAU_USD": 250,
}

TRAIL_LOCK = {
    "EUR_USD": 6,
    "GBP_USD": 7,
    "USD_JPY": 5,
    "XAU_USD": 120,
}

MAX_SPREAD = {
    "EUR_USD": 0.00025,
    "GBP_USD": 0.00040,
    "USD_JPY": 0.030,
    "XAU_USD": 1.50,
}

FAST_EMA = 20
MID_EMA = 50
SLOW_EMA = 200
CANDLE_COUNT = 260

last_trade_time = {}
blocked_until = {}
managed_to_be = set()

daily_stats = {
    "date": None,
    "trades": 0,
    "losses": 0,
    "last_seen_closed_ids": set(),
}

# =========================================================
# HELPERS
# =========================================================
def log(message: str) -> None:
    print(message, flush=True)


def utc_now() -> dt.datetime:
    return dt.datetime.utcnow()


def reset_daily_stats_if_needed() -> None:
    today = utc_now().date().isoformat()
    if daily_stats["date"] != today:
        daily_stats["date"] = today
        daily_stats["trades"] = 0
        daily_stats["losses"] = 0
        daily_stats["last_seen_closed_ids"] = set()


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
    mult = 2 / (period + 1)
    value = sum(values[:period]) / period
    for v in values[period:]:
        value = ((v - value) * mult) + value
    return value


def in_session() -> bool:
    hour = utc_now().hour
    return SESSION_START_UTC <= hour <= SESSION_END_UTC


def cooldown_active(pair: str) -> bool:
    last = last_trade_time.get(pair, 0)
    return (time.time() - last) < COOLDOWN_SECONDS


def pair_blocked(pair: str) -> bool:
    until = blocked_until.get(pair, 0)
    return time.time() < until


def open_trade_slots_available() -> bool:
    return len(get_open_trades()) < MAX_TOTAL_OPEN_TRADES


# =========================================================
# OANDA
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


def get_candles(pair: str, count: int, granularity: str) -> list[dict]:
    data = oanda_get(
        f"/instruments/{pair}/candles?price=M&granularity={granularity}&count={count}"
    )
    return data.get("candles", [])


def complete_closes(pair: str, granularity: str, count: int = CANDLE_COUNT) -> list[float]:
    candles = get_candles(pair, count=count, granularity=granularity)
    out = []
    for c in candles:
        if c.get("complete"):
            out.append(float(c["mid"]["c"]))
    return out


def last_complete_candle(pair: str, granularity: str) -> dict | None:
    candles = get_candles(pair, count=5, granularity=granularity)
    complete = [c for c in candles if c.get("complete")]
    if not complete:
        return None
    return complete[-1]


def get_recent_closed_trades() -> list[dict]:
    # latest closed trades/orders for daily loss tracking
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/trades?state=CLOSED&count=50")
    return data.get("trades", [])


# =========================================================
# STRATEGY FILTERS
# =========================================================
def htf_trend_ok(signal: str, pair: str) -> tuple[bool, str]:
    # 15m and 1h trend alignment
    closes_15 = complete_closes(pair, "M15")
    closes_h1 = complete_closes(pair, "H1")

    if len(closes_15) < SLOW_EMA or len(closes_h1) < SLOW_EMA:
        return False, "not enough HTF candles"

    fast_15 = ema(closes_15, FAST_EMA)
    mid_15 = ema(closes_15, MID_EMA)
    slow_15 = ema(closes_15, SLOW_EMA)

    fast_h1 = ema(closes_h1, FAST_EMA)
    mid_h1 = ema(closes_h1, MID_EMA)
    slow_h1 = ema(closes_h1, SLOW_EMA)

    last_15 = closes_15[-1]
    last_h1 = closes_h1[-1]

    if None in {fast_15, mid_15, slow_15, fast_h1, mid_h1, slow_h1}:
        return False, "ema unavailable"

    if signal == "BUY":
        cond = (
            last_15 > fast_15 > mid_15 > slow_15
            and last_h1 > fast_h1 > mid_h1 > slow_h1
        )
        return (True, "htf buy ok") if cond else (False, "htf blocked buy")

    cond = (
        last_15 < fast_15 < mid_15 < slow_15
        and last_h1 < fast_h1 < mid_h1 < slow_h1
    )
    return (True, "htf sell ok") if cond else (False, "htf blocked sell")


def ltf_entry_ok(signal: str, pair: str) -> tuple[bool, str]:
    closes = complete_closes(pair, ENTRY_GRANULARITY)
    if len(closes) < SLOW_EMA:
        return False, "not enough entry candles"

    fast = ema(closes, FAST_EMA)
    mid = ema(closes, MID_EMA)
    slow = ema(closes, SLOW_EMA)
    last_close = closes[-1]

    if None in {fast, mid, slow}:
        return False, "entry ema unavailable"

    candle = last_complete_candle(pair, ENTRY_GRANULARITY)
    if not candle:
        return False, "entry candle unavailable"

    o = float(candle["mid"]["o"])
    h = float(candle["mid"]["h"])
    l = float(candle["mid"]["l"])
    c = float(candle["mid"]["c"])
    body = abs(c - o)
    rng = max(h - l, 0.0000001)

    # Require decent candle body relative to range
    body_ratio = body / rng

    if signal == "BUY":
        cond = last_close > fast > mid > slow and c > o and body_ratio >= 0.35
        return (True, "ltf buy ok") if cond else (False, "ltf blocked buy")

    cond = last_close < fast < mid < slow and c < o and body_ratio >= 0.35
    return (True, "ltf sell ok") if cond else (False, "ltf blocked sell")


def daily_limits_ok() -> tuple[bool, str]:
    reset_daily_stats_if_needed()

    if daily_stats["trades"] >= MAX_TRADES_PER_DAY:
        return False, "daily trade limit reached"

    if daily_stats["losses"] >= MAX_LOSSES_PER_DAY:
        return False, "daily loss limit reached"

    return True, "daily limits ok"


# =========================================================
# WEBHOOK PARSING
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
# TRADE ACTIONS
# =========================================================
def place_trade(signal: str, pair: str) -> dict:
    reset_daily_stats_if_needed()

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

    limits_ok, limits_reason = daily_limits_ok()
    if not limits_ok:
        return {"status": "skipped", "reason": limits_reason}

    if not open_trade_slots_available():
        return {"status": "skipped", "reason": "max total open trades reached"}

    if get_open_trade_for_pair(pair):
        return {"status": "skipped", "reason": f"{pair} already has open trade"}

    if cooldown_active(pair):
        return {"status": "skipped", "reason": f"{pair} cooldown active"}

    if pair_blocked(pair):
        return {"status": "skipped", "reason": f"{pair} reentry blocked"}

    spread_ok, spread = spread_allowed(pair)
    if not spread_ok:
        return {"status": "skipped", "reason": f"{pair} spread too high: {spread}"}

    htf_ok, htf_reason = htf_trend_ok(signal, pair)
    if not htf_ok:
        return {"status": "skipped", "reason": htf_reason}

    ltf_ok, ltf_reason = ltf_entry_ok(signal, pair)
    if not ltf_ok:
        return {"status": "skipped", "reason": ltf_reason}

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

    log(f"Webhook accepted: signal={signal} pair={pair}")
    log(f"{pair} Sending: {payload}")

    response = oanda_post(f"/accounts/{OANDA_ACCOUNT_ID}/orders", payload)
    log(f"{pair} Response: {response}")

    last_trade_time[pair] = time.time()
    daily_stats["trades"] += 1

    return {"status": "placed", "pair": pair, "signal": signal, "response": response}


# =========================================================
# TRADE MANAGEMENT
# =========================================================
def update_daily_loss_count() -> None:
    reset_daily_stats_if_needed()
    closed = get_recent_closed_trades()

    for trade in closed:
        trade_id = trade.get("id")
        if trade_id in daily_stats["last_seen_closed_ids"]:
            continue

        daily_stats["last_seen_closed_ids"].add(trade_id)

        instrument = trade.get("instrument")
        realized_pl = float(trade.get("realizedPL", "0"))

        if realized_pl < 0:
            daily_stats["losses"] += 1
            if instrument:
                blocked_until[instrument] = time.time() + REENTRY_BLOCK_SECONDS
                log(f"🛑 {instrument} reentry blocked after loss")


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

            current = px["bid"] if units > 0 else px["ask"]
            pip = pip_size(pair)
            pips_profit = abs(current - entry) / pip

            log(f"{pair} pips in profit: {pips_profit}")

            # Move to breakeven once
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
                log(f"🔥 {pair} moved to breakeven: {response}")

            # Trail after profit threshold
            if pips_profit >= TRAIL_AFTER[pair]:
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
                log(f"📈 {pair} trailing stop updated: {response}")

        except Exception as e:
            log(f"manage error: {e}")


def auto_loop() -> None:
    while True:
        try:
            update_daily_loss_count()
        except Exception as e:
            log(f"daily stats error: {e}")

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
    reset_daily_stats_if_needed()
    return jsonify({
        "bot": "OANDA Runner V9 Sniper",
        "env": OANDA_ENV,
        "granularity": ENTRY_GRANULARITY,
        "max_total_open_trades": MAX_TOTAL_OPEN_TRADES,
        "pairs": PAIRS,
        "status": "running",
        "units": UNITS,
        "daily_trades": daily_stats["trades"],
        "daily_losses": daily_stats["losses"],
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
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
