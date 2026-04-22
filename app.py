from flask import Flask, request, jsonify
import requests
import os
import time
import threading

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("ACCOUNT_ID") or os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()

BASE_URL = "https://api-fxtrade.oanda.com/v3" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com/v3"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")
PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "EUR_USD,GBP_USD,USD_JPY").split(",") if p.strip()]

UNITS = int(os.getenv("UNITS", "5000"))
MAX_TOTAL_TRADES = int(os.getenv("MAX_TOTAL_TRADES", "2"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_TRADES_PER_PAIR", "1"))
SPREAD_LIMIT_PIPS = float(os.getenv("SPREAD_LIMIT_PIPS", "2.0"))

# V34.1 aggressive but controlled
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "10"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "20"))

BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "5"))
LOCK_1_TRIGGER_PIPS = float(os.getenv("LOCK_1_TRIGGER_PIPS", "8"))
LOCK_1_PIPS = float(os.getenv("LOCK_1_PIPS", "2"))
TRAILING_TRIGGER_PIPS = float(os.getenv("TRAILING_TRIGGER_PIPS", "10"))
TRAILING_DISTANCE_PIPS = float(os.getenv("TRAILING_DISTANCE_PIPS", "4"))

MANAGE_INTERVAL_SECONDS = int(os.getenv("MANAGE_INTERVAL_SECONDS", "10"))

SESSION_MODE = os.getenv("SESSION_MODE", "off").lower()
LONDON_START_HOUR_EST = int(os.getenv("LONDON_START_HOUR_EST", "3"))
LONDON_END_HOUR_EST = int(os.getenv("LONDON_END_HOUR_EST", "13"))
NY_START_HOUR_EST = int(os.getenv("NY_START_HOUR_EST", "8"))
NY_END_HOUR_EST = int(os.getenv("NY_END_HOUR_EST", "17"))

TRAILING_SET = set()


# =========================
# HELPERS
# =========================
def validate_config():
    missing = []
    if not OANDA_API_KEY:
        missing.append("OANDA_API_KEY")
    if not ACCOUNT_ID:
        missing.append("ACCOUNT_ID / OANDA_ACCOUNT_ID")

    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        return False
    return True


def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def price_precision(pair):
    return 3 if "JPY" in pair else 5


def fmt_price(pair, price):
    return f"{price:.{price_precision(pair)}f}"


def in_allowed_session():
    if SESSION_MODE == "off":
        return True

    # rough EST/EDT style approximation
    est_hour = time.gmtime(time.time() - 4 * 3600).tm_hour

    in_london = LONDON_START_HOUR_EST <= est_hour < LONDON_END_HOUR_EST
    in_ny = NY_START_HOUR_EST <= est_hour < NY_END_HOUR_EST

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
    for t in get_open_trades_for_pair(pair):
        units = float(t["currentUnits"])
        if action == "buy" and units > 0:
            return True
        if action == "sell" and units < 0:
            return True
    return False


def extract_existing_sl_price(trade):
    sl = trade.get("stopLossOrder")
    if sl and "price" in sl:
        return float(sl["price"])
    return None


def better_stop_for_buy(new_sl, old_sl):
    return old_sl is None or new_sl > old_sl


def better_stop_for_sell(new_sl, old_sl):
    return old_sl is None or new_sl < old_sl


# =========================
# ORDER BUILD / PLACE
# =========================
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
                "price": fmt_price(pair, sl_price)
            },
            "takeProfitOnFill": {
                "price": fmt_price(pair, tp_price)
            }
        }
    }


def place_trade(pair, action):
    order_data = build_order(pair, action)
    response = oanda_post(f"/accounts/{ACCOUNT_ID}/orders", order_data)
    print("TRADE RESPONSE:", response)
    return response


# =========================
# TRADE MANAGEMENT
# =========================
def set_stop_loss(trade_id, pair, sl_price):
    payload = {
        "stopLoss": {
            "price": fmt_price(pair, sl_price)
        }
    }
    response = oanda_put(f"/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders", payload)
    print(f"STOP LOSS UPDATED | trade={trade_id} pair={pair} sl={fmt_price(pair, sl_price)}")
    return response


def set_trailing_stop(trade_id, pair, distance_pips):
    distance = distance_pips * pip_size(pair)
    payload = {
        "trailingStopLoss": {
            "distance": fmt_price(pair, distance)
        }
    }
    response = oanda_put(f"/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders", payload)
    print(f"TRAILING STOP UPDATED | trade={trade_id} pair={pair} distance_pips={distance_pips}")
    return response


def get_live_trade_price(pair, units):
    bid, ask, _ = get_pricing(pair)
    return bid if units > 0 else ask


def manage_trade(trade):
    trade_id = trade["id"]
    pair = trade["instrument"]
    entry = float(trade["price"])
    units = float(trade["currentUnits"])
    ps = pip_size(pair)

    current = get_live_trade_price(pair, units)

    if units > 0:
        pips = (current - entry) / ps
    else:
        pips = (entry - current) / ps

    old_sl = extract_existing_sl_price(trade)
    desired_sl = None

    # break even
    if pips >= BREAK_EVEN_TRIGGER_PIPS:
        desired_sl = entry

    # lock some profit
    if pips >= LOCK_1_TRIGGER_PIPS:
        desired_sl = entry + (LOCK_1_PIPS * ps) if units > 0 else entry - (LOCK_1_PIPS * ps)

    if desired_sl is not None:
        if units > 0 and better_stop_for_buy(desired_sl, old_sl):
            set_stop_loss(trade_id, pair, desired_sl)
        elif units < 0 and better_stop_for_sell(desired_sl, old_sl):
            set_stop_loss(trade_id, pair, desired_sl)

    # trailing stop
    if pips >= TRAILING_TRIGGER_PIPS and trade_id not in TRAILING_SET:
        try:
            set_trailing_stop(trade_id, pair, TRAILING_DISTANCE_PIPS)
            TRAILING_SET.add(trade_id)
        except Exception as e:
            print(f"Trailing stop update failed for trade {trade_id}: {e}")


def manage_all_trades_loop():
    while True:
        try:
            if validate_config():
                open_trades = get_open_trades()
                open_trade_ids = {t["id"] for t in open_trades}

                stale_ids = [tid for tid in TRAILING_SET if tid not in open_trade_ids]
                for tid in stale_ids:
                    TRAILING_SET.discard(tid)

                for trade in open_trades:
                    manage_trade(trade)

        except Exception as e:
            print(f"Trade manager error: {e}")

        time.sleep(MANAGE_INTERVAL_SECONDS)


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Bot Running V34.1 🚀"


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if not validate_config():
            return jsonify({"status": "error", "message": "Missing required env vars"}), 500

        data = request.get_json(force=True, silent=True) or {}
        print("WEBHOOK RECEIVED:", data)

        passphrase = str(data.get("passphrase", ""))
        pair = str(data.get("pair", "")).upper().strip()
        action = str(data.get("action", "")).lower().strip()

        if passphrase != PASSPHRASE:
            print("BLOCKED: invalid passphrase")
            return jsonify({"status": "error", "message": "Invalid passphrase"}), 403

        if pair not in PAIRS:
            print(f"BLOCKED: invalid pair {pair}")
            return jsonify({"status": "error", "message": f"Invalid pair: {pair}"}), 400

        if action not in {"buy", "sell"}:
            print(f"BLOCKED: invalid action {action}")
            return jsonify({"status": "error", "message": f"Invalid action: {action}"}), 400

        if not in_allowed_session():
            print("BLOCKED: outside allowed session")
            return jsonify({"status": "blocked", "message": "Outside allowed session"}), 200

        if total_open_trades() >= MAX_TOTAL_TRADES:
            print("BLOCKED: max total trades reached")
            return jsonify({"status": "blocked", "message": "Max total trades reached"}), 200

        if len(get_open_trades_for_pair(pair)) >= MAX_TRADES_PER_PAIR:
            print(f"BLOCKED: max trades reached for {pair}")
            return jsonify({"status": "blocked", "message": f"Max trades reached for {pair}"}), 200

        if has_open_trade_same_direction(pair, action):
            print(f"BLOCKED: same direction trade already open on {pair}")
            return jsonify({"status": "blocked", "message": f"Same direction trade already open on {pair}"}), 200

        print("TRYING TO PLACE TRADE...")
        response = place_trade(pair, action)
        print("TRADE SUCCESS")

        return jsonify({"status": "ok", "message": "Trade placed", "response": response}), 200

    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200


# =========================
# START BACKGROUND MANAGER
# =========================
manager_thread = threading.Thread(target=manage_all_trades_loop, daemon=True)
manager_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
