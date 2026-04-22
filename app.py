import os
import time
import threading
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import pytz

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()

if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com/v3"
else:
    BASE_URL = "https://api-fxpractice.oanda.com/v3"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")

PAIRS = [p.strip() for p in os.getenv("PAIRS", "EUR_USD,GBP_USD,USD_JPY").split(",")]

UNITS = int(os.getenv("UNITS", "5000"))
MAX_TOTAL_TRADES = int(os.getenv("MAX_TOTAL_TRADES", "2"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_TRADES_PER_PAIR", "1"))
SPREAD_LIMIT_PIPS = float(os.getenv("SPREAD_LIMIT_PIPS", "2.0"))

# Risk / trade management
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "15"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "30"))

BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "5"))
LOCK_1_TRIGGER_PIPS = float(os.getenv("LOCK_1_TRIGGER_PIPS", "8"))
LOCK_1_PIPS = float(os.getenv("LOCK_1_PIPS", "2"))
LOCK_2_TRIGGER_PIPS = float(os.getenv("LOCK_2_TRIGGER_PIPS", "12"))
LOCK_2_PIPS = float(os.getenv("LOCK_2_PIPS", "5"))
TRAILING_TRIGGER_PIPS = float(os.getenv("TRAILING_TRIGGER_PIPS", "15"))
TRAILING_DISTANCE_PIPS = float(os.getenv("TRAILING_DISTANCE_PIPS", "5"))

# Session filter
LONDON_START_HOUR_EST = int(os.getenv("LONDON_START_HOUR_EST", "3"))
LONDON_END_HOUR_EST = int(os.getenv("LONDON_END_HOUR_EST", "13"))
NEW_YORK_START_HOUR_EST = int(os.getenv("NY_START_HOUR_EST", "8"))
NEW_YORK_END_HOUR_EST = int(os.getenv("NY_END_HOUR_EST", "17"))
SESSION_MODE = os.getenv("SESSION_MODE", "london").lower()  
# options: london / ny / london_ny / off

MANAGE_INTERVAL_SECONDS = int(os.getenv("MANAGE_INTERVAL_SECONDS", "15"))

# =========================
# HELPERS
# =========================
def validate_config():
    missing = []
    if not OANDA_API_KEY:
        missing.append("OANDA_API_KEY")
    if not ACCOUNT_ID:
        missing.append("ACCOUNT_ID")
    if missing:
        logging.error(f"Missing required env vars: {', '.join(missing)}")
    return len(missing) == 0


def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def price_precision(pair: str) -> int:
    return 3 if "JPY" in pair else 5


def format_price(pair: str, price: float) -> str:
    return f"{price:.{price_precision(pair)}f}"


def now_est():
    est = pytz.timezone("America/New_York")
    return datetime.now(est)


def in_allowed_session():
    if SESSION_MODE == "off":
        return True

    hour = now_est().hour

    in_london = LONDON_START_HOUR_EST <= hour < LONDON_END_HOUR_EST
    in_ny = NEW_YORK_START_HOUR_EST <= hour < NEW_YORK_END_HOUR_EST

    if SESSION_MODE == "london":
        return in_london
    if SESSION_MODE == "ny":
        return in_ny
    if SESSION_MODE == "london_ny":
        return in_london or in_ny

    return True


def oanda_get(path, params=None):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def oanda_post(path, data):
    url = f"{BASE_URL}{path}"
    r = requests.post(url, headers=HEADERS, json=data, timeout=20)
    r.raise_for_status()
    return r.json()


def oanda_put(path, data):
    url = f"{BASE_URL}{path}"
    r = requests.put(url, headers=HEADERS, json=data, timeout=20)
    r.raise_for_status()
    return r.json()


def get_open_trades():
    data = oanda_get(f"/accounts/{ACCOUNT_ID}/openTrades")
    return data.get("trades", [])


def get_open_trades_for_pair(pair):
    return [t for t in get_open_trades() if t.get("instrument") == pair]


def total_open_trades():
    return len(get_open_trades())


def get_pricing(pair):
    data = oanda_get(f"/accounts/{ACCOUNT_ID}/pricing", params={"instruments": pair})
    prices = data["prices"][0]
    bid = float(prices["bids"][0]["price"])
    ask = float(prices["asks"][0]["price"])
    spread_pips = (ask - bid) / pip_size(pair)
    return bid, ask, spread_pips


def has_open_trade_same_direction(pair, action):
    trades = get_open_trades_for_pair(pair)
    for t in trades:
        units = float(t["currentUnits"])
        if action == "buy" and units > 0:
            return True
        if action == "sell" and units < 0:
            return True
    return False


def build_order(pair, action):
    bid, ask, spread_pips = get_pricing(pair)

    if spread_pips > SPREAD_LIMIT_PIPS:
        raise Exception(f"Spread too high on {pair}: {spread_pips:.2f} pips")

    units = UNITS if action == "buy" else -UNITS
    entry = ask if action == "buy" else bid
    ps = pip_size(pair)

    if action == "buy":
        sl_price = entry - (STOP_LOSS_PIPS * ps)
        tp_price = entry + (TAKE_PROFIT_PIPS * ps)
    else:
        sl_price = entry + (STOP_LOSS_PIPS * ps)
        tp_price = entry - (TAKE_PROFIT_PIPS * ps)

    return {
        "order": {
            "instrument": pair,
            "units": str(units),
            "type": "MARKET",
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


def place_trade(pair, action):
    order_data = build_order(pair, action)
    response = oanda_post(f"/accounts/{ACCOUNT_ID}/orders", order_data)
    logging.info(f"TRADE RESPONSE: {response}")
    return response


def extract_existing_sl_price(trade):
    sl = trade.get("stopLossOrder")
    if sl and "price" in sl:
        return float(sl["price"])
    return None


def set_stop_loss(trade_id, pair, sl_price):
    payload = {
        "stopLoss": {
            "price": format_price(pair, sl_price)
        }
    }
    response = oanda_put(f"/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders", payload)
    logging.info(f"Updated stop loss for trade {trade_id}: {response}")
    return response


def set_trailing_stop(trade_id, pair, distance_pips):
    distance = distance_pips * pip_size(pair)
    precision = price_precision(pair)
    payload = {
        "trailingStopLoss": {
            "distance": f"{distance:.{precision}f}"
        }
    }
    response = oanda_put(f"/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders", payload)
    logging.info(f"Updated trailing stop for trade {trade_id}: {response}")
    return response


def better_stop_for_buy(new_sl, old_sl):
    if old_sl is None:
        return True
    return new_sl > old_sl


def better_stop_for_sell(new_sl, old_sl):
    if old_sl is None:
        return True
    return new_sl < old_sl


def manage_trade(trade):
    trade_id = trade["id"]
    pair = trade["instrument"]
    entry = float(trade["price"])
    current = float(trade["currentPrice"])
    units = float(trade["currentUnits"])
    ps = pip_size(pair)

    if units > 0:
        pips = (current - entry) / ps
    else:
        pips = (entry - current) / ps

    old_sl = extract_existing_sl_price(trade)
    desired_sl = None

    if pips >= BREAK_EVEN_TRIGGER_PIPS:
        desired_sl = entry

    if pips >= LOCK_1_TRIGGER_PIPS:
        if units > 0:
            desired_sl = entry + (LOCK_1_PIPS * ps)
        else:
            desired_sl = entry - (LOCK_1_PIPS * ps)

    if pips >= LOCK_2_TRIGGER_PIPS:
        if units > 0:
            desired_sl = entry + (LOCK_2_PIPS * ps)
        else:
            desired_sl = entry - (LOCK_2_PIPS * ps)

    if desired_sl is not None:
        if units > 0 and better_stop_for_buy(desired_sl, old_sl):
            set_stop_loss(trade_id, pair, desired_sl)
        elif units < 0 and better_stop_for_sell(desired_sl, old_sl):
            set_stop_loss(trade_id, pair, desired_sl)

    if pips >= TRAILING_TRIGGER_PIPS:
        try:
            set_trailing_stop(trade_id, pair, TRAILING_DISTANCE_PIPS)
        except Exception as e:
            logging.warning(f"Trailing stop update failed for {trade_id}: {e}")


def manage_all_trades_loop():
    while True:
        try:
            if validate_config():
                trades = get_open_trades()
                for trade in trades:
                    manage_trade(trade)
        except Exception as e:
            logging.error(f"Trade manager error: {e}")
        time.sleep(MANAGE_INTERVAL_SECONDS)


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Bot Running V32 🚀"


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if not validate_config():
            return jsonify({"status": "error", "message": "Missing required env vars"}), 500

        data = request.get_json(force=True, silent=True) or {}
        passphrase = str(data.get("passphrase", ""))
        pair = str(data.get("pair", "")).upper().strip()
        action = str(data.get("action", "")).lower().strip()

        logging.info(f"WEBHOOK RECEIVED | pair={pair} action={action}")

        if passphrase != PASSPHRASE:
            return jsonify({"status": "error", "message": "Invalid passphrase"}), 403

        if pair not in PAIRS:
            return jsonify({"status": "error", "message": f"Invalid pair: {pair}"}), 400

        if action not in ["buy", "sell"]:
            return jsonify({"status": "error", "message": f"Invalid action: {action}"}), 400

        if not in_allowed_session():
            logging.info("BLOCKED: outside allowed session")
            return jsonify({"status": "blocked", "message": "Outside allowed session"}), 200

        if total_open_trades() >= MAX_TOTAL_TRADES:
            return jsonify({"status": "blocked", "message": "Max total trades reached"}), 200

        if len(get_open_trades_for_pair(pair)) >= MAX_TRADES_PER_PAIR:
            return jsonify({"status": "blocked", "message": f"Max trades reached for {pair}"}), 200

        if has_open_trade_same_direction(pair, action):
            return jsonify({"status": "blocked", "message": f"Same direction trade already open on {pair}"}), 200

        response = place_trade(pair, action)
        return jsonify({"status": "ok", "message": "Trade placed", "response": response}), 200

    except Exception as e:
        logging.error(f"WEBHOOK ERROR: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# =========================
# START BACKGROUND MANAGER
# =========================
manager_thread = threading.Thread(target=manage_all_trades_loop, daemon=True)
manager_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
