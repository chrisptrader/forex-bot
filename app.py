import os
import time
import math
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

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "2"))              # % risk per trade
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "50"))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true").strip().lower() == "true"
TRAILING_TRIGGER_PIPS = float(os.getenv("TRAILING_TRIGGER_PIPS", "15"))
TRAILING_DISTANCE_PIPS = float(os.getenv("TRAILING_DISTANCE_PIPS", "10"))

ALLOW_MULTIPAIR = os.getenv("ALLOW_MULTIPAIR", "true").strip().lower() == "true"
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))

ENABLE_SESSION_FILTER = os.getenv("ENABLE_SESSION_FILTER", "true").strip().lower() == "true"
TIMEZONE_NAME = os.getenv("TIMEZONE_NAME", "America/New_York").strip()

LONDON_START = int(os.getenv("LONDON_START", "3"))   # 3 AM NY time
LONDON_END = int(os.getenv("LONDON_END", "6"))       # 6 AM NY time
NY_START = int(os.getenv("NY_START", "8"))           # 8 AM NY time
NY_END = int(os.getenv("NY_END", "11"))              # 11 AM NY time

MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "10"))       # seconds
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "60"))

# Optional fixed fallback units if risk calc fails
FALLBACK_UNITS = int(os.getenv("FALLBACK_UNITS", "100"))

# =========================
# OANDA BASE URL
# =========================
if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com"
else:
    BASE_URL = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Keeps track of last signal time per pair
last_trade_time = {}
# Keeps track of active monitor threads per trade ID
active_monitors = {}


# =========================
# HELPERS
# =========================
def log(msg: str) -> None:
    print(f"[{datetime.utcnow().isoformat()} UTC] {msg}", flush=True)


def pip_size_for_pair(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def is_trading_session() -> bool:
    if not ENABLE_SESSION_FILTER:
        return True

    try:
        tz = ZoneInfo(TIMEZONE_NAME)
        now_local = datetime.now(tz)
        hour = now_local.hour

        london_open = LONDON_START <= hour < LONDON_END
        ny_open = NY_START <= hour < NY_END

        return london_open or ny_open
    except Exception as e:
        log(f"Session filter error, allowing trade: {e}")
        return True


def get_account_details():
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}"
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.json().get("account", {})


def get_account_balance() -> float:
    account = get_account_details()
    return float(account.get("balance", 0))


def get_price(pair: str):
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/pricing"
    params = {"instruments": pair}
    response = requests.get(url, headers=HEADERS, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()

    prices = data.get("prices", [])
    if not prices:
        raise ValueError(f"No pricing returned for {pair}")

    price_info = prices[0]
    bid = float(price_info["bids"][0]["price"])
    ask = float(price_info["asks"][0]["price"])
    return bid, ask


def get_open_trades():
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/openTrades"
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.json().get("trades", [])


def get_open_trade_for_pair(pair: str):
    trades = get_open_trades()
    for trade in trades:
        if trade.get("instrument") == pair:
            return trade
    return None


def get_trade_by_id(trade_id: str):
    trades = get_open_trades()
    for trade in trades:
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


def calculate_units(pair: str, stop_loss_pips: float) -> int:
    """
    Simple risk model:
    risk_amount = balance * risk%
    units = risk_amount / price_distance

    This is a simplified approximation and works well enough to keep size controlled.
    """
    try:
        balance = get_account_balance()
        risk_amount = balance * (RISK_PERCENT / 100.0)

        bid, ask = get_price(pair)
        entry_price = ask

        pip_size = pip_size_for_pair(pair)
        stop_distance_price = stop_loss_pips * pip_size

        if stop_distance_price <= 0:
            return FALLBACK_UNITS

        raw_units = risk_amount / stop_distance_price

        # cap crazy sizes for safety
        units = max(1, min(int(raw_units), 100000))
        return units
    except Exception as e:
        log(f"Risk sizing failed for {pair}, using fallback units. Error: {e}")
        return FALLBACK_UNITS


def close_trade(trade_id: str):
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/trades/{trade_id}/close"
    response = requests.put(url, headers=HEADERS, json={}, timeout=20)
    response.raise_for_status()
    return response.json()


def update_trade_sl(trade_id: str, new_sl_price: float):
    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    payload = {
        "stopLoss": {
            "timeInForce": "GTC",
            "price": f"{new_sl_price:.5f}"
        }
    }
    response = requests.put(url, headers=HEADERS, json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def create_market_order(pair: str, side: str):
    """
    side: BUY or SELL
    """
    side = side.upper().strip()
    if side not in ["BUY", "SELL"]:
        raise ValueError("side must be BUY or SELL")

    units = calculate_units(pair, STOP_LOSS_PIPS)
    if side == "SELL":
        units = -abs(units)
    else:
        units = abs(units)

    bid, ask = get_price(pair)
    pip_size = pip_size_for_pair(pair)

    entry_price = ask if units > 0 else bid
    sl_price = entry_price - (STOP_LOSS_PIPS * pip_size) if units > 0 else entry_price + (STOP_LOSS_PIPS * pip_size)
    tp_price = entry_price + (TAKE_PROFIT_PIPS * pip_size) if units > 0 else entry_price - (TAKE_PROFIT_PIPS * pip_size)

    payload = {
        "order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": f"{sl_price:.5f}"
            },
            "takeProfitOnFill": {
                "price": f"{tp_price:.5f}"
            }
        }
    }

    url = f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/orders"
    response = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()

    order_fill = data.get("orderFillTransaction", {})
    trade_opened = order_fill.get("tradeOpened", {})
    trade_id = trade_opened.get("tradeID")

    log(
        f"ORDER FILLED | pair={pair} side={side} units={units} "
        f"entry={entry_price:.5f} SL={sl_price:.5f} TP={tp_price:.5f} trade_id={trade_id}"
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


def maybe_trail_stop(trade):
    if not USE_TRAILING_STOP:
        return

    trade_id = str(trade["id"])
    pair = trade["instrument"]
    units = float(trade["currentUnits"])
    entry_price = float(trade["price"])
    pip_size = pip_size_for_pair(pair)

    bid, ask = get_price(pair)
    pips = calculate_pips(trade, bid, ask)

    if pips < TRAILING_TRIGGER_PIPS:
        return

    current_price = bid if units > 0 else ask
    existing_sl = trade.get("stopLossOrder")
    existing_sl_price = None

    if existing_sl and existing_sl.get("price"):
        existing_sl_price = float(existing_sl["price"])

    if units > 0:
        new_sl = current_price - (TRAILING_DISTANCE_PIPS * pip_size)
        # only move SL upward
        if existing_sl_price is None or new_sl > existing_sl_price:
            update_trade_sl(trade_id, new_sl)
            log(f"TRAILING SL UPDATED | pair={pair} trade_id={trade_id} new_sl={new_sl:.5f}")
    else:
        new_sl = current_price + (TRAILING_DISTANCE_PIPS * pip_size)
        # only move SL downward for sell trades
        if existing_sl_price is None or new_sl < existing_sl_price:
            update_trade_sl(trade_id, new_sl)
            log(f"TRAILING SL UPDATED | pair={pair} trade_id={trade_id} new_sl={new_sl:.5f}")


def monitor_trade(trade_id: str):
    log(f"Started monitoring trade {trade_id}")

    while True:
        try:
            trade = get_trade_by_id(trade_id)

            # FIXES GHOST TRACKING BUG
            if not trade:
                log(f"No open trade found for trade_id={trade_id}. Stopping monitor.")
                active_monitors.pop(trade_id, None)
                return

            pair = trade["instrument"]
            bid, ask = get_price(pair)
            pips = calculate_pips(trade, bid, ask)
            unrealized_pl = trade.get("unrealizedPL", "0.0")
            entry_price = float(trade["price"])
            current_units = float(trade["currentUnits"])
            current_price = bid if current_units > 0 else ask

            log(
                f"MONITOR | pair={pair} trade_id={trade_id} "
                f"entry={entry_price:.5f} current={current_price:.5f} "
                f"pips={pips} unrealizedPL={unrealized_pl}"
            )

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

    thread = threading.Thread(target=monitor_trade, args=(trade_id,), daemon=True)
    active_monitors[trade_id] = thread
    thread.start()


# =========================
# WEBHOOK ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "env": OANDA_ENV,
        "session_filter": ENABLE_SESSION_FILTER,
        "risk_percent": RISK_PERCENT,
        "sl_pips": STOP_LOSS_PIPS,
        "tp_pips": TAKE_PROFIT_PIPS
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

        if count_open_trades() >= MAX_OPEN_TRADES:
            log(f"Blocked by max open trades | current={count_open_trades()} max={MAX_OPEN_TRADES}")
            return jsonify({"status": "blocked", "reason": "max open trades reached"}), 200

        if not ALLOW_MULTIPAIR and count_open_trades() > 0:
            log("Blocked because multipair is disabled")
            return jsonify({"status": "blocked", "reason": "multipair disabled"}), 200

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


# =========================
# START
# =========================
if __name__ == "__main__":
    missing = []
    if not API_KEY:
        missing.append("OANDA_API_KEY")
    if not ACCOUNT_ID:
        missing.append("OANDA_ACCOUNT_ID")

    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    port = int(os.getenv("PORT", "5000"))
    log(f"Starting Forex Bot V12 on port {port} | env={OANDA_ENV}")
    app.run(host="0.0.0.0", port=port)
