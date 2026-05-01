import os
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
# ENV SETTINGS
# =========================

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")

FIXED_UNITS = int(os.getenv("FIXED_UNITS", "15000"))

STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "80"))

USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true").lower() == "true"
BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "8"))
BREAK_EVEN_PLUS_PIPS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", "1"))

USE_PARTIAL_TP = os.getenv("USE_PARTIAL_TP", "true").lower() == "true"
PARTIAL_TP_TRIGGER_PIPS = float(os.getenv("PARTIAL_TP_TRIGGER_PIPS", "15"))
PARTIAL_TP_PERCENT = float(os.getenv("PARTIAL_TP_PERCENT", "50"))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true").lower() == "true"
TRAIL_TRIGGER_PIPS = float(os.getenv("TRAIL_TRIGGER_PIPS", "20"))
TRAIL_DISTANCE_PIPS = float(os.getenv("TRAIL_DISTANCE_PIPS", "10"))

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
ONE_TRADE_PER_PAIR = os.getenv("ONE_TRADE_PER_PAIR", "true").lower() == "true"

# =========================
# OANDA URL
# =========================

if OANDA_ENV == "live":
    OANDA_URL = "https://api-fxtrade.oanda.com/v3"
else:
    OANDA_URL = "https://api-fxpractice.oanda.com/v3"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

# memory so partial close does not repeat
PARTIAL_DONE = set()


# =========================
# HELPERS
# =========================

def pip_size(pair):
    if "JPY" in pair:
        return 0.01
    return 0.0001


def round_price(pair, price):
    if "JPY" in pair:
        return round(price, 3)
    return round(price, 5)


def get_price(pair):
    url = f"{OANDA_URL}/accounts/{OANDA_ACCOUNT_ID}/pricing"
    params = {"instruments": pair}
    r = requests.get(url, headers=HEADERS, params=params, timeout=10)
    data = r.json()

    price_data = data["prices"][0]
    bid = float(price_data["bids"][0]["price"])
    ask = float(price_data["asks"][0]["price"])
    mid = (bid + ask) / 2

    return bid, ask, mid


def get_open_trades():
    url = f"{OANDA_URL}/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=HEADERS, timeout=10)
    data = r.json()
    return data.get("trades", [])


def has_trade_for_pair(pair):
    trades = get_open_trades()
    for trade in trades:
        if trade["instrument"] == pair:
            return True
    return False


def count_open_trades():
    return len(get_open_trades())


def calculate_sl_tp(pair, action, entry):
    pip = pip_size(pair)

    if action == "buy":
        sl = entry - STOP_LOSS_PIPS * pip
        tp = entry + TAKE_PROFIT_PIPS * pip
    else:
        sl = entry + STOP_LOSS_PIPS * pip
        tp = entry - TAKE_PROFIT_PIPS * pip

    return round_price(pair, sl), round_price(pair, tp)


def place_trade(pair, action):
    bid, ask, mid = get_price(pair)

    if action == "buy":
        units = FIXED_UNITS
        entry = ask
    elif action == "sell":
        units = -FIXED_UNITS
        entry = bid
    else:
        return {"error": "Invalid action"}

    sl, tp = calculate_sl_tp(pair, action, entry)

    order = {
        "order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "takeProfitOnFill": {
                "price": str(tp),
                "timeInForce": "GTC"
            },
            "stopLossOnFill": {
                "price": str(sl),
                "timeInForce": "GTC"
            }
        }
    }

    url = f"{OANDA_URL}/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=HEADERS, json=order, timeout=10)

    return r.json()


def close_partial(trade_id, units_to_close):
    url = f"{OANDA_URL}/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close"
    payload = {
        "units": str(abs(int(units_to_close)))
    }

    r = requests.put(url, headers=HEADERS, json=payload, timeout=10)
    return r.json()


def modify_trade_sl(trade_id, pair, new_sl):
    url = f"{OANDA_URL}/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders"

    payload = {
        "stopLoss": {
            "timeInForce": "GTC",
            "price": str(round_price(pair, new_sl))
        }
    }

    r = requests.put(url, headers=HEADERS, json=payload, timeout=10)
    return r.json()


def get_profit_pips(trade, current_bid, current_ask):
    pair = trade["instrument"]
    pip = pip_size(pair)

    entry = float(trade["price"])
    units = int(float(trade["currentUnits"]))

    if units > 0:
        return (current_bid - entry) / pip
    else:
        return (entry - current_ask) / pip


def manage_trades():
    trades = get_open_trades()
    results = []

    for trade in trades:
        trade_id = trade["id"]
        pair = trade["instrument"]
        units = int(float(trade["currentUnits"]))
        entry = float(trade["price"])

        if units == 0:
            continue

        bid, ask, mid = get_price(pair)
        pip = pip_size(pair)
        profit_pips = get_profit_pips(trade, bid, ask)

        direction = "buy" if units > 0 else "sell"

        # =========================
        # BREAK EVEN
        # =========================

        if USE_BREAK_EVEN and profit_pips >= BREAK_EVEN_TRIGGER_PIPS:
            if direction == "buy":
                new_sl = entry + BREAK_EVEN_PLUS_PIPS * pip
            else:
                new_sl = entry - BREAK_EVEN_PLUS_PIPS * pip

            response = modify_trade_sl(trade_id, pair, new_sl)

            results.append({
                "trade": trade_id,
                "pair": pair,
                "action": "break_even",
                "profit_pips": round(profit_pips, 1),
                "new_sl": round_price(pair, new_sl),
                "response": response
            })

        # =========================
        # PARTIAL TAKE PROFIT
        # =========================

        if USE_PARTIAL_TP and profit_pips >= PARTIAL_TP_TRIGGER_PIPS:
            if trade_id not in PARTIAL_DONE:
                close_units = abs(units) * (PARTIAL_TP_PERCENT / 100)
                close_units = int(close_units)

                if close_units >= 1:
                    response = close_partial(trade_id, close_units)
                    PARTIAL_DONE.add(trade_id)

                    results.append({
                        "trade": trade_id,
                        "pair": pair,
                        "action": "partial_close",
                        "profit_pips": round(profit_pips, 1),
                        "closed_units": close_units,
                        "response": response
                    })

        # =========================
        # TRAILING STOP
        # =========================

        if USE_TRAILING_STOP and profit_pips >= TRAIL_TRIGGER_PIPS:
            if direction == "buy":
                trail_sl = bid - TRAIL_DISTANCE_PIPS * pip
            else:
                trail_sl = ask + TRAIL_DISTANCE_PIPS * pip

            response = modify_trade_sl(trade_id, pair, trail_sl)

            results.append({
                "trade": trade_id,
                "pair": pair,
                "action": "trailing_stop",
                "profit_pips": round(profit_pips, 1),
                "new_sl": round_price(pair, trail_sl),
                "response": response
            })

    return results


# =========================
# ROUTES
# =========================

@app.route("/", methods=["GET"])
def home():
    return "V39 appy running ✅"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "version": "v39",
        "env": OANDA_ENV
    })


@app.route("/manage", methods=["GET", "POST"])
def manage():
    results = manage_trades()
    return jsonify({
        "status": "managed",
        "results": results
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)

    print("WEBHOOK RECEIVED:", data, flush=True)

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "bad passphrase"}), 403

    pair = data.get("pair")
    action = data.get("action")

    if not pair or not action:
        return jsonify({"error": "missing pair or action"}), 400

    pair = pair.upper()
    action = action.lower()

    # manage existing trades first
    manage_results = manage_trades()

    if count_open_trades() >= MAX_OPEN_TRADES:
        return jsonify({
            "status": "blocked",
            "reason": "max open trades reached",
            "manage_results": manage_results
        })

    if ONE_TRADE_PER_PAIR and has_trade_for_pair(pair):
        return jsonify({
            "status": "blocked",
            "reason": "already have open trade for this pair",
            "pair": pair,
            "manage_results": manage_results
        })

    response = place_trade(pair, action)

    print("ORDER RESPONSE:", response, flush=True)

    return jsonify({
        "status": "trade_sent",
        "pair": pair,
        "action": action,
        "units": FIXED_UNITS,
        "manage_results": manage_results,
        "order_response": response
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
