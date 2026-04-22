
from flask import Flask, request, jsonify
import requests
import os
import time

app = Flask(__name__)

# ========================
# ENV VARIABLES
# ========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
BASE_URL = "https://api-fxpractice.oanda.com/v3"

WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")

# Trading settings
FIXED_UNITS = int(os.getenv("FIXED_UNITS", 5000))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 3))

# Profit lock + runner
USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true") == "true"
BREAK_EVEN_TRIGGER = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", 8))
BREAK_EVEN_PLUS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", 1))

USE_TRAILING = os.getenv("USE_TRAILING_STOP", "true") == "true"
TRAILING_TRIGGER = float(os.getenv("TRAILING_TRIGGER_PIPS", 12))
TRAILING_DISTANCE = float(os.getenv("TRAILING_DISTANCE_PIPS", 8))

# ========================
# HELPERS
# ========================
def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    r = requests.get(url, headers=headers)
    return r.json().get("trades", [])


def get_price(pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    params = {"instruments": pair}
    r = requests.get(url, headers=headers, params=params)
    data = r.json()

    price = data["prices"][0]
    bid = float(price["bids"][0]["price"])
    ask = float(price["asks"][0]["price"])

    return bid, ask


def modify_trade(trade_id, stop_loss_price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "stopLoss": {
            "price": str(stop_loss_price)
        }
    }

    requests.put(url, headers=headers, json=data)


def create_order(pair, units, side):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

    order = {
        "order": {
            "instrument": pair,
            "units": str(units if side == "buy" else -units),
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    r = requests.post(url, headers=headers, json=order)
    print("ORDER RESPONSE:", r.json())


# ========================
# TRADE MANAGEMENT (FIXED)
# ========================
def manage_trades():
    trades = get_open_trades()

    for trade in trades:
        try:
            trade_id = trade["id"]
            pair = trade["instrument"]
            entry = float(trade["price"])
            units = int(trade["currentUnits"])

            bid, ask = get_price(pair)

            # Correct price based on direction
            current = bid if units > 0 else ask

            pip_size = 0.01 if "JPY" in pair else 0.0001
            profit_pips = (current - entry) / pip_size if units > 0 else (entry - current) / pip_size

            print(f"{pair} | Profit: {profit_pips:.2f} pips")

            # ========================
            # BREAK EVEN
            # ========================
            if USE_BREAK_EVEN and profit_pips >= BREAK_EVEN_TRIGGER:
                new_sl = entry + (BREAK_EVEN_PLUS * pip_size if units > 0 else -BREAK_EVEN_PLUS * pip_size)
                print(f"Moving SL to BE: {new_sl}")
                modify_trade(trade_id, new_sl)

            # ========================
            # TRAILING STOP (RUNNER)
            # ========================
            if USE_TRAILING and profit_pips >= TRAILING_TRIGGER:
                if units > 0:
                    new_sl = current - (TRAILING_DISTANCE * pip_size)
                else:
                    new_sl = current + (TRAILING_DISTANCE * pip_size)

                print(f"Trailing SL: {new_sl}")
                modify_trade(trade_id, new_sl)

        except Exception as e:
            print("Manage trade error:", e)


# ========================
# WEBHOOK
# ========================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("WEBHOOK:", data)

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "unauthorized"}), 403

    pair = data.get("pair")
    action = data.get("action")

    trades = get_open_trades()

    if len(trades) >= MAX_OPEN_TRADES:
        print("BLOCKED: max trades")
        return jsonify({"status": "blocked"})

    create_order(pair, FIXED_UNITS, action)

    return jsonify({"status": "ok"})


# ========================
# LOOP
# ========================
def run_manager():
    while True:
        manage_trades()
        time.sleep(10)


import threading
threading.Thread(target=run_manager).start()

@app.route("/")
def home():
    return "Bot Running V_FINAL 🚀"
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
