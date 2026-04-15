import os
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
# ENVIRONMENT VARIABLES
# =========================
API_KEY = os.getenv("OANDA_API_KEY", "").strip()
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "").strip()

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1.0"))
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "50"))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true").strip().lower() == "true"
TRAILING_TRIGGER_PIPS = float(os.getenv("TRAILING_TRIGGER_PIPS", "15"))
TRAILING_DISTANCE_PIPS = float(os.getenv("TRAILING_DISTANCE_PIPS", "10"))

USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true").strip().lower() == "true"
BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "10"))
BREAK_EVEN_PLUS_PIPS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", "1"))

ALLOW_MULTIPAIR = os.getenv("ALLOW_MULTIPAIR", "true").strip().lower() == "true"
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "300"))

ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").strip().lower() == "true"
TIMEZONE_NAME = os.getenv("TIMEZONE_NAME", "America/New_York").strip()
LONDON_START = int(os.getenv("LONDON_START", "3"))
LONDON_END = int(os.getenv("LONDON_END", "6"))
NY_START = int(os.getenv("NY_START", "8"))
NY_END = int(os.getenv("NY_END", "11"))

ENABLE_TREND_FILTER = os.getenv("ENABLE_TREND_FILTER", "true").strip().lower() == "true"
FAST_MA_PERIOD = int(os.getenv("FAST_MA_PERIOD", "20"))
SLOW_MA_PERIOD = int(os.getenv("SLOW_MA_PERIOD", "50"))

ENABLE_VOLATILITY_FILTER = os.getenv("ENABLE_VOLATILITY_FILTER", "true").strip().lower() == "true"
MIN_CANDLE_RANGE_PIPS = float(os.getenv("MIN_CANDLE_RANGE_PIPS", "5"))
MAX_CANDLE_RANGE_PIPS = float(os.getenv("MAX_CANDLE_RANGE_PIPS", "35"))

ENABLE_SPREAD_FILTER = os.getenv("ENABLE_SPREAD_FILTER", "true").strip().lower() == "true"
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "2.0"))

ENABLE_DAILY_LOSS_LIMIT = os.getenv("ENABLE_DAILY_LOSS_LIMIT", "true").strip().lower() == "true"
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))

MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "10"))
FALLBACK_UNITS = int(os.getenv("FALLBACK_UNITS", "100"))

if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com"
else:
    BASE_URL = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

last_trade_time = {}
active_monitors = {}
daily_start_balance = {"date": None, "balance": None}


# =========================
# HELPERS
# =========================
def log(msg: str) -> None:
    print(f"[{datetime.utcnow().isoformat()} UTC] {msg}", flush=True)


def pip_size_for_pair(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def format_price(pair: str, price: float) -> str:
    decimals = 3 if "JPY" in pair else 5
    return f"{price:.{decimals}f}"


def is_trading_session() -> bool:
    if not ENABLE_SESSION_FILTER:
        return True
    try:
        now_local = datetime.now(ZoneInfo(TIMEZONE_NAME))
        hour = now_local.hour
        london_open = LONDON_START <= hour < LONDON_END
        ny_open = NY_START <= hour < NY_END
        return london_open or ny_open
    except Exception as e:
        log(f"Session filter error, allowing trade: {e}")
        return True


def get_account_details():
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json().get("account", {})


def get_account_balance() -> float:
    account = get_account_details()
    return float(account.get("balance", 0.0))


def refresh_daily_start_balance():
    today = datetime.now(ZoneInfo(TIMEZONE_NAME)).date().isoformat()
    if daily_start_balance["date"] != today:
        daily_start_balance["date"] = today
        daily_start_balance["balance"] = get_account_balance()
        log(f"New daily balance snapshot set: {daily_start_balance['balance']}")


def daily_loss_limit_hit() -> bool:
    if not ENABLE_DAILY_LOSS_LIMIT:
        return False
    refresh_daily_start_balance()
    start_balance = daily_start_balance["balance"]
    current_balance = get_account_balance()
    if not start_balance or start_balance <= 0:
        return False
    drawdown_pct = ((start_balance - current_balance) / start_balance) * 100.0
    if drawdown_pct >= MAX_DAILY_LOSS_PERCENT:
        log(f"Daily loss limit hit: {drawdown_pct:.2f}%")
        return True
    return False


def get_price(pair: str):
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/pricing"
    params = {"instruments": pair}
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    prices = r.json().get("prices", [])
    if not prices:
        raise ValueError(f"No pricing returned for {pair}")
    bid = float(prices[0]["bids"][0]["price"])
    ask = float(prices[0]["asks"][0]["price"])
    return bid, ask


def get_spread_pips(pair: str) -> float:
    bid, ask = get_price(pair)
    return round((ask - bid) / pip_size_for_pair(pair), 2)


def get_open_trades():
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json().get("trades", [])


def get_open_trade_for_pair(pair: str):
    for trade in get_open_trades():
        if trade.get("instrument") == pair:
            return trade
    return None


def get_trade_by_id(trade_id: str):
    for trade in get_open_trades():
        if str(trade.get("id")) == str(trade_id):
            return trade
    return None


def count_open_trades() -> int:
    return len(get_open_trades())


def pair_recently_traded(pair: str) -> bool:
    last_time = last_trade_time.get(pair)
    if not last_time:
        return False
    return (time.time() - last_time) < MIN_SECONDS_BETWEEN_TRADES


def get_candles(pair: str, granularity: str = "M5", count: int = 60):
    url = f"{BASE_URL}/v3/instruments/{pair}/candles"
    params = {
        "price": "M",
        "granularity": granularity,
        "count": count
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    candles = r.json().get("candles", [])
    closed = [c for c in candles if c.get("complete")]
    return closed


def simple_moving_average(values, period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def trend_filter_pass(pair: str, side: str) -> tuple[bool, str]:
    if not ENABLE_TREND_FILTER:
        return True, "trend filter off"

    candles = get_candles(pair, granularity="M5", count=max(SLOW_MA_PERIOD + 5, 60))
    closes = [float(c["mid"]["c"]) for c in candles]

    fast_ma = simple_moving_average(closes, FAST_MA_PERIOD)
    slow_ma = simple_moving_average(closes, SLOW_MA_PERIOD)

    if fast_ma is None or slow_ma is None:
        return False, "not enough candles for trend filter"

    if side == "BUY" and fast_ma > slow_ma:
        return True, f"BUY trend pass fast_ma={fast_ma:.5f} slow_ma={slow_ma:.5f}"
    if side == "SELL" and fast_ma < slow_ma:
        return True, f"SELL trend pass fast_ma={fast_ma:.5f} slow_ma={slow_ma:.5f}"

    return False, f"trend blocked fast_ma={fast_ma:.5f} slow_ma={slow_ma:.5f}"


def volatility_filter_pass(pair: str) -> tuple[bool, str]:
    if not ENABLE_VOLATILITY_FILTER:
        return True, "volatility filter off"

    candles = get_candles(pair, granularity="M5", count=3)
    if len(candles) < 2:
        return False, "not enough candles for volatility filter"

    last_closed = candles[-1]
    high = float(last_closed["mid"]["h"])
    low = float(last_closed["mid"]["l"])
    range_pips = (high - low) / pip_size_for_pair(pair)

    if range_pips < MIN_CANDLE_RANGE_PIPS:
        return False, f"volatility too low range={range_pips:.1f} pips"
    if range_pips > MAX_CANDLE_RANGE_PIPS:
        return False, f"volatility too high range={range_pips:.1f} pips"

    return True, f"volatility pass range={range_pips:.1f} pips"


def spread_filter_pass(pair: str) -> tuple[bool, str]:
    if not ENABLE_SPREAD_FILTER:
        return True, "spread filter off"
    spread = get_spread_pips(pair)
    if spread > MAX_SPREAD_PIPS:
        return False, f"spread too high spread={spread} pips"
    return True, f"spread pass spread={spread} pips"


def calculate_units(pair: str, stop_loss_pips: float) -> int:
    try:
        balance = get_account_balance()
        risk_amount = balance * (RISK_PERCENT / 100.0)
        pip_size = pip_size_for_pair(pair)
        bid, ask = get_price(pair)
        entry_price = ask
        stop_distance_price = stop_loss_pips * pip_size

        if stop_distance_price <= 0:
            return FALLBACK_UNITS

        raw_units = risk_amount / stop_distance_price
        units = max(1, min(int(raw_units), 100000))
        return units
    except Exception as e:
        log(f"Risk sizing failed for {pair}, using fallback units. Error: {e}")
        return FALLBACK_UNITS


def close_trade(trade_id: str):
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/trades/{trade_id}/close"
    r = requests.put(url, headers=HEADERS, json={}, timeout=20)
    r.raise_for_status()
    return r.json()


def update_trade_sl(trade_id: str, new_sl_price: float, pair: str):
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    payload = {
        "stopLoss": {
            "timeInForce": "GTC",
            "price": format_price(pair, new_sl_price)
        }
    }
    r = requests.put(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def create_market_order(pair: str, side: str):
    side = side.upper().strip()
    if side not in ["BUY", "SELL"]:
        raise ValueError("side must be BUY or SELL")

    units = calculate_units(pair, STOP_LOSS_PIPS)
    units = abs(units) if side == "BUY" else -abs(units)

    bid, ask = get_price(pair)
    pip_size = pip_size_for_pair(pair)
    entry_price = ask if units > 0 else bid

    if units > 0:
        sl_price = entry_price - (STOP_LOSS_PIPS * pip_size)
        tp_price = entry_price + (TAKE_PROFIT_PIPS * pip_size)
    else:
        sl_price = entry_price + (STOP_LOSS_PIPS * pip_size)
        tp_price = entry_price - (TAKE_PROFIT_PIPS * pip_size)

    payload = {
        "order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": format_price(pair, sl_price)
            },
            "takeProfitOnFill": {
                "price": format_price(pair, tp_price)
            }
        }
    }

    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/orders"
    r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()

    order_fill = data.get("orderFillTransaction", {})
    trade_opened = order_fill.get("tradeOpened", {})
    trade_id = trade_opened.get("tradeID")

    log(
        f"ORDER FILLED | pair={pair} side={side} units={units} "
        f"entry={format_price(pair, entry_price)} "
        f"SL={format_price(pair, sl_price)} TP={format_price(pair, tp_price)} "
        f"trade_id={trade_id}"
    )
    return data, str(trade_id) if trade_id else None


def calculate_pips(trade, bid: float, ask: float) -> float:
    pair = trade["instrument"]
    entry_price = float(trade["price"])
    current_units = float(trade["currentUnits"])
    pip_size = pip_size_for_pair(pair)

    if current_units > 0:
        current_price = bid
        pips = (current_price - entry_price) / pip_size
    else:
        current_price = ask
        pips = (entry_price - current_price) / pip_size

    return round(pips, 1)


def maybe_move_to_break_even(trade):
    if not USE_BREAK_EVEN:
        return

    trade_id = str(trade["id"])
    pair = trade["instrument"]
    units = float(trade["currentUnits"])
    entry_price = float(trade["price"])
    pip_size = pip_size_for_pair(pair)

    bid, ask = get_price(pair)
    pips = calculate_pips(trade, bid, ask)

    if pips < BREAK_EVEN_TRIGGER_PIPS:
        return

    existing_sl = trade.get("stopLossOrder")
    existing_sl_price = float(existing_sl["price"]) if existing_sl and existing_sl.get("price") else None

    if units > 0:
        be_sl = entry_price + (BREAK_EVEN_PLUS_PIPS * pip_size)
        if existing_sl_price is None or be_sl > existing_sl_price:
            update_trade_sl(trade_id, be_sl, pair)
            log(f"BREAK EVEN SET | pair={pair} trade_id={trade_id} be_sl={format_price(pair, be_sl)}")
    else:
        be_sl = entry_price - (BREAK_EVEN_PLUS_PIPS * pip_size)
        if existing_sl_price is None or be_sl < existing_sl_price:
            update_trade_sl(trade_id, be_sl, pair)
            log(f"BREAK EVEN SET | pair={pair} trade_id={trade_id} be_sl={format_price(pair, be_sl)}")


def maybe_trail_stop(trade):
    if not USE_TRAILING_STOP:
        return

    trade_id = str(trade["id"])
    pair = trade["instrument"]
    units = float(trade["currentUnits"])
    pip_size = pip_size_for_pair(pair)

    bid, ask = get_price(pair)
    pips = calculate_pips(trade, bid, ask)

    if pips < TRAILING_TRIGGER_PIPS:
        return

    current_price = bid if units > 0 else ask
    existing_sl = trade.get("stopLossOrder")
    existing_sl_price = float(existing_sl["price"]) if existing_sl and existing_sl.get("price") else None

    if units > 0:
        new_sl = current_price - (TRAILING_DISTANCE_PIPS * pip_size)
        if existing_sl_price is None or new_sl > existing_sl_price:
            update_trade_sl(trade_id, new_sl, pair)
            log(f"TRAILING SL UPDATED | pair={pair} trade_id={trade_id} new_sl={format_price(pair, new_sl)}")
    else:
        new_sl = current_price + (TRAILING_DISTANCE_PIPS * pip_size)
        if existing_sl_price is None or new_sl < existing_sl_price:
            update_trade_sl(trade_id, new_sl, pair)
            log(f"TRAILING SL UPDATED | pair={pair} trade_id={trade_id} new_sl={format_price(pair, new_sl)}")


def monitor_trade(trade_id: str):
    log(f"Started monitoring trade {trade_id}")

    while True:
        try:
            trade = get_trade_by_id(trade_id)
            if not trade:
                log(f"No open trade found for trade_id={trade_id}. Stopping monitor.")
                active_monitors.pop(trade_id, None)
                return

            pair = trade["instrument"]
            bid, ask = get_price(pair)
            pips = calculate_pips(trade, bid, ask)
            entry_price = float(trade["price"])
            current_units = float(trade["currentUnits"])
            current_price = bid if current_units > 0 else ask
            unrealized_pl = trade.get("unrealizedPL", "0.0")

            log(
                f"MONITOR | pair={pair} trade_id={trade_id} "
                f"entry={format_price(pair, entry_price)} "
                f"current={format_price(pair, current_price)} "
                f"pips={pips} unrealizedPL={unrealized_pl}"
            )

            maybe_move_to_break_even(trade)
            maybe_trail_stop(trade)

            time.sleep(MONITOR_INTERVAL)

        except Exception as e:
            log(f"Monitor error for trade {trade_id}: {e}")
            time.sleep(MONITOR_INTERVAL)


def start_monitor_thread(trade_id: str):
    if not trade_id:
        return
    if trade_id in active_monitors:
        log(f"Monitor already running for trade_id={trade_id}")
        return
    t = threading.Thread(target=monitor_trade, args=(trade_id,), daemon=True)
    active_monitors[trade_id] = t
    t.start()


def filters_pass(pair: str, side: str):
    checks = [
        spread_filter_pass(pair),
        volatility_filter_pass(pair),
        trend_filter_pass(pair, side),
    ]
    for passed, reason in checks:
        log(f"FILTER | pair={pair} side={side} result={passed} reason={reason}")
        if not passed:
            return False, reason
    return True, "all filters passed"


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "env": OANDA_ENV,
        "risk_percent": RISK_PERCENT,
        "sl_pips": STOP_LOSS_PIPS,
        "tp_pips": TAKE_PROFIT_PIPS,
        "session_filter": ENABLE_SESSION_FILTER,
        "trend_filter": ENABLE_TREND_FILTER,
        "volatility_filter": ENABLE_VOLATILITY_FILTER,
        "spread_filter": ENABLE_SPREAD_FILTER,
        "break_even": USE_BREAK_EVEN,
        "daily_loss_limit": ENABLE_DAILY_LOSS_LIMIT
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=False)
        if not data:
            return jsonify({"error": "No JSON received"}), 400

        passphrase = str(data.get("passphrase", "")).strip()
        if WEBHOOK_PASSPHRASE and passphrase != WEBHOOK_PASSPHRASE:
            return jsonify({"error": "Invalid passphrase"}), 403

        pair = str(data.get("pair", "")).strip().upper()
        side = str(data.get("action", "")).strip().upper()

        if not pair or side not in ["BUY", "SELL"]:
            return jsonify({"error": "Invalid pair or action"}), 400

        if "_" not in pair:
            return jsonify({"error": "Pair must look like EUR_USD"}), 400

        if daily_loss_limit_hit():
            return jsonify({"status": "blocked", "reason": "daily loss limit hit"}), 200

        if not is_trading_session():
            log(f"Blocked by session filter | pair={pair} action={side}")
            return jsonify({"status": "blocked", "reason": "outside trading session"}), 200

        if pair_recently_traded(pair):
            log(f"Blocked duplicate signal too soon | pair={pair}")
            return jsonify({"status": "blocked", "reason": "pair traded too recently"}), 200

        existing_pair_trade = get_open_trade_for_pair(pair)
        if existing_pair_trade:
            log(f"Blocked duplicate open trade on same pair | pair={pair}")
            return jsonify({"status": "blocked", "reason": "open trade already exists for pair"}), 200

        current_open_trades = count_open_trades()
        if current_open_trades >= MAX_OPEN_TRADES:
            log(f"Blocked by max open trades | current={current_open_trades} max={MAX_OPEN_TRADES}")
            return jsonify({"status": "blocked", "reason": "max open trades reached"}), 200

        if not ALLOW_MULTIPAIR and current_open_trades > 0:
            log("Blocked because multipair is disabled")
            return jsonify({"status": "blocked", "reason": "multipair disabled"}), 200

        passed, reason = filters_pass(pair, side)
        if not passed:
            return jsonify({"status": "blocked", "reason": reason}), 200

        order_result, trade_id = create_market_order(pair, side)
        last_trade_time[pair] = time.time()

        if trade_id:
            start_monitor_thread(trade_id)

        return jsonify({
            "status": "success",
            "pair": pair,
            "action": side,
            "trade_id": trade_id,
            "order_result": order_result
        }), 200

    except requests.HTTPError as e:
        try:
            error_body = e.response.json()
        except Exception:
            error_body = e.response.text if e.response is not None else str(e)
        log(f"OANDA HTTP ERROR: {error_body}")
        return jsonify({"error": "OANDA HTTP error", "details": error_body}), 500

    except Exception as e:
        log(f"WEBHOOK ERROR: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/open-trades", methods=["GET"])
def open_trades():
    try:
        trades = get_open_trades()
        return jsonify({"count": len(trades), "trades": trades}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/close-all", methods=["POST"])
def close_all():
    try:
        trades = get_open_trades()
        results = []
        for trade in trades:
            trade_id = str(trade["id"])
            result = close_trade(trade_id)
            results.append(result)
            log(f"Closed trade {trade_id}")
        return jsonify({"status": "success", "closed": len(results), "results": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    missing = []
    if not API_KEY:
        missing.append("OANDA_API_KEY")
    if not ACCOUNT_ID:
        missing.append("OANDA_ACCOUNT_ID")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    refresh_daily_start_balance()

    port = int(os.getenv("PORT", "5000"))
    log(f"Starting Forex Bot V13 on port {port} | env={OANDA_ENV}")
    app.run(host="0.0.0.0", port=port)
