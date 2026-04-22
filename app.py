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

# ========================
# CONFIG
# ========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()

if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com/v3"
else:
    BASE_URL = "https://api-fxpractice.oanda.com/v3"

WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY"]

UNITS = int(os.getenv("FIXED_UNITS", "1000"))
MAX_TOTAL_TRADES = int(os.getenv("MAX_TOTAL_OPEN_TRADES", "2"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_OPEN_TRADES", "1"))

MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "2.0"))

BREAK_EVEN_TRIGGER = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "5"))
LOCK1_TRIGGER = float(os.getenv("LOCK_1_TRIGGER_PIPS", "8"))
LOCK1_PIPS = float(os.getenv("LOCK_1_PIPS", "2"))

TRAILING_TRIGGER = float(os.getenv("TRAILING_TRIGGER_PIPS", "10"))
TRAILING_DISTANCE = float(os.getenv("TRAILING_DISTANCE_PIPS", "5"))

MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "15"))

# London session in New York time
LONDON_START_HOUR = int(os.getenv("LONDON_START_HOUR_EST", "3"))
LONDON_END_HOUR = int(os.getenv("LONDON_END_HOUR_EST", "11"))

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

# ========================
# HELPERS
# ========================
def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def price_precision(pair: str) -> int:
    return 3 if "JPY" in pair else 5


def fmt_price(pair: str, price: float) -> str:
    return f"{price:.{price_precision(pair)}f}"


def is_london_session():
    est = pytz.timezone("America/New_York")
    now = datetime.now(est)
    start = now.replace(hour=LONDON_START_HOUR, minute=0, second=0, microsecond=0)
    end = now.replace(hour=LONDON_END_HOUR, minute=0, second=0, microsecond=0)
    return start <= now <= end


def safe_json(response):
    try:
        return response.json()
    except Exception:
        return {"raw_text": response.text}


def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json().get("trades", [])


def count_trades():
    return len(get_open_trades())


def pair_trade_count(pair):
    trades = get_open_trades()
    return sum(1 for t in trades if t.get("instrument") == pair)


def pair_has_trade(pair):
    return pair_trade_count(pair) >= MAX_TRADES_PER_PAIR


def get_price(pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=HEADERS, params={"instruments": pair}, timeout=10)
    r.raise_for_status()
    data = r.json()

    if "prices" not in data or not data["prices"]:
        raise ValueError(f"No pricing returned for {pair}: {data}")

    price = data["prices"][0]
    bid = float(price["bids"][0]["price"])
    ask = float(price["asks"][0]["price"])
    spread_pips = (ask - bid) / pip_size(pair)
    return bid, ask, spread_pips


def get_current_sl(trade):
    sl = trade.get("stopLossOrder")
    if sl and "price" in sl:
        try:
            return float(sl["price"])
        except Exception:
            return None
    return None


def should_update_sl(current_sl, new_sl, units):
    if current_sl is None:
        return True
    if units > 0:
        return new_sl > current_sl
    return new_sl < current_sl


# ========================
# TRADE ENTRY
# ========================
def place_trade(pair, action):
    action = str(action).lower().strip()

    if action not in ["buy", "sell"]:
        logging.info(f"BLOCKED: invalid action {action}")
        return {"error": f"Invalid action: {action}"}

    bid, ask, spread = get_price(pair)

    if spread > MAX_SPREAD_PIPS:
        logging.info(f"BLOCKED: spread too high | {pair} | spread={spread:.2f}")
        return {"error": "Spread too high"}

    units = UNITS if action == "buy" else -UNITS

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    r = requests.post(url, headers=HEADERS, json=data, timeout=10)
    result = safe_json(r)
    logging.info(f"TRADE RESPONSE: {result}")
    return result


# ========================
# TRADE MANAGEMENT
# ========================
def update_stop_loss(trade_id, sl_price, pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    data = {
        "stopLoss": {
            "price": fmt_price(pair, sl_price)
        }
    }
    r = requests.put(url, headers=HEADERS, json=data, timeout=10)
    logging.info(f"SL UPDATE RESPONSE: {safe_json(r)}")


def update_trailing_stop(trade_id, pair):
    distance_price = TRAILING_DISTANCE * pip_size(pair)
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    data = {
        "trailingStopLoss": {
            "distance": fmt_price(pair, distance_price)
        }
    }
    r = requests.put(url, headers=HEADERS, json=data, timeout=10)
    logging.info(f"TRAIL UPDATE RESPONSE: {safe_json(r)}")


def manage_trades():
    trades = get_open_trades()

    for t in trades:
        try:
            trade_id = t["id"]
            pair = t["instrument"]
            entry = float(t["price"])
            units = float(t["currentUnits"])
            current_sl = get_current_sl(t)

            bid, ask, spread = get_price(pair)
            current = bid if units > 0 else ask
            psize = pip_size(pair)

            if units > 0:
                pips = (current - entry) / psize
            else:
                pips = (entry - current) / psize

            logging.info(
                f"MANAGE | {pair} | trade_id={trade_id} | pips={pips:.2f} | spread={spread:.2f}"
            )

            new_sl = None

            # STEP 1: Break even
            if pips >= BREAK_EVEN_TRIGGER:
                new_sl = entry

            # STEP 2: Lock some profit
            if pips >= LOCK1_TRIGGER:
                if units > 0:
                    new_sl = entry + (LOCK1_PIPS * psize)
                else:
                    new_sl = entry - (LOCK1_PIPS * psize)

            if new_sl is not None and should_update_sl(current_sl, new_sl, units):
                logging.info(f"UPDATING SL | {pair} | {trade_id} | new_sl={new_sl}")
                update_stop_loss(trade_id, new_sl, pair)

            # STEP 3: Trailing stop
            if pips >= TRAILING_TRIGGER:
                logging.info(f"START TRAILING | {pair} | {trade_id}")
                update_trailing_stop(trade_id, pair)

        except Exception as e:
            logging.error(f"manage_trades error on trade {t.get('id', 'unknown')}: {e}")


def manager_loop():
    while True:
        try:
            manage_trades()
        except Exception as e:
            logging.error(f"manager_loop error: {e}")
        time.sleep(MONITOR_INTERVAL)


# ========================
# ROUTES
# ========================
@app.route("/")
def home():
    return "Bot Running V32 🚀", 200


@app.route("/manage", methods=["GET"])
def manual_manage():
    try:
        manage_trades()
        return jsonify({"status": "managed"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json or {}

        passphrase = str(data.get("passphrase", "")).strip()
        pair = str(data.get("pair", "")).strip()
        action = str(data.get("action", "")).lower().strip()

        logging.info(f"WEBHOOK RECEIVED | pair={pair} action={action}")

        if passphrase != WEBHOOK_PASSPHRASE:
            return jsonify({"error": "bad passphrase"}), 403

        if pair not in PAIRS:
            return jsonify({"error": "Invalid pair"}), 400

        if action not in ["buy", "sell"]:
            return jsonify({"error": "Invalid action"}), 400

        # London session only
        if not is_london_session():
            logging.info("BLOCKED: outside London session")
            return jsonify({"status": "Outside London session"}), 200

        if count_trades() >= MAX_TOTAL_TRADES:
            logging.info("BLOCKED: max total trades reached")
            return jsonify({"status": "Max trades reached"}), 200

        if pair_has_trade(pair):
            logging.info(f"BLOCKED: trade already open on {pair}")
            return jsonify({"status": "Trade already open on pair"}), 200

        result = place_trade(pair, action)
        return jsonify({"status": "Trade processed", "result": result}), 200

    except Exception as e:
        logging.error(f"WEBHOOK ERROR: {e}")
        return jsonify({"error": str(e)}), 500


# ========================
# START
# ========================
if __name__ == "__main__":
    thread = threading.Thread(target=manager_loop, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
