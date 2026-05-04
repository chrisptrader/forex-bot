import os
import time
import threading
from flask import Flask, request
import requests

app = Flask(__name__)

# =============================
# CONFIG
# =============================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
BASE_URL = "https://api-fxpractice.oanda.com/v3"

WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE")

ALLOW_BUY = os.getenv("ALLOW_BUY", "true") == "true"
ALLOW_SELL = os.getenv("ALLOW_SELL", "true") == "true"

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 2))
ONE_TRADE_PER_PAIR = os.getenv("ONE_TRADE_PER_PAIR", "true") == "true"
COOLDOWN_SECONDS = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", 90))

FIXED_UNITS = int(os.getenv("FIXED_UNITS", 25000))

SL_PIPS = float(os.getenv("STOP_LOSS_PIPS", 15))
TP_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", 60))
MIN_MOVE_PIPS = float(os.getenv("MIN_MOVE_PIPS", 3))

USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true") == "true"
BREAK_EVEN_TRIGGER = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", 8))
BREAK_EVEN_PLUS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", 2))

USE_PARTIAL_CLOSE = os.getenv("USE_PARTIAL_CLOSE", "true") == "true"
PARTIAL_TRIGGER = float(os.getenv("PARTIAL_CLOSE_TRIGGER_PIPS", 18))
PARTIAL_PERCENT = float(os.getenv("PARTIAL_CLOSE_PERCENT", 50))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true") == "true"
TRAILING_TRIGGER = float(os.getenv("TRAILING_TRIGGER_PIPS", 15))
TRAILING_DISTANCE = float(os.getenv("TRAILING_DISTANCE_PIPS", 7))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", 10))

# =============================
# STATE
# =============================
last_trade_time = {}
last_signal_price = {}
partial_closed = set()

headers = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

# =============================
# HELPERS
# =============================
def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001

def round_price(pair, price):
    return round(price, 3 if "JPY" in pair else 5)

def get_price(pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"
    r = requests.get(url, headers=headers).json()
    return float(r["prices"][0]["bids"][0]["price"])

def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=headers).json()
    return r.get("trades", [])

def close_partial(trade_id, units):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/close"
    return requests.put(url, headers=headers, json={"units": str(abs(int(units)))})

def modify_sl(pair, trade_id, price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    return requests.put(url, headers=headers, json={
        "stopLoss": {"price": str(round_price(pair, price))}
    })

# =============================
# TRADE MANAGER
# =============================
def trade_manager():
    while True:
        try:
            trades = get_open_trades()

            for t in trades:
                trade_id = t["id"]
                pair = t["instrument"]
                entry = float(t["price"])
                units = float(t["currentUnits"])

                price = get_price(pair)
                direction = 1 if units > 0 else -1
                pips = ((price - entry) / pip_size(pair)) * direction

                print(f"[MANAGER] {pair} | {round(pips, 1)} pips")

                if USE_BREAK_EVEN and pips >= BREAK_EVEN_TRIGGER:
                    new_sl = entry + (BREAK_EVEN_PLUS * pip_size(pair) * direction)
                    modify_sl(pair, trade_id, new_sl)
                    print(f"[BE MOVED] {pair} | +{BREAK_EVEN_PLUS} pips")

                if USE_PARTIAL_CLOSE and pips >= PARTIAL_TRIGGER and trade_id not in partial_closed:
                    close_units = abs(int(units)) * (PARTIAL_PERCENT / 100)
                    close_partial(trade_id, close_units)
                    partial_closed.add(trade_id)
                    print(f"[PARTIAL CLOSED] {pair} | {PARTIAL_PERCENT}%")

                if USE_TRAILING_STOP and pips >= TRAILING_TRIGGER:
                    new_sl = price - (TRAILING_DISTANCE * pip_size(pair) * direction)
                    modify_sl(pair, trade_id, new_sl)
                    print(f"[TRAILING] {pair} | distance {TRAILING_DISTANCE} pips")

        except Exception as e:
            print("Manager error:", e)

        time.sleep(POLL_SECONDS)

# =============================
# WEBHOOK
# =============================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        print("❌ INVALID PASSPHRASE")
        return "invalid"

    pair = data.get("pair")
    action = data.get("action")

    print(f"📩 SIGNAL | {pair} | {action}")

    if not pair or action not in ["buy", "sell"]:
        print("❌ BLOCKED | bad signal format")
        return "bad signal"

    now = time.time()
    trades = get_open_trades()

    if pair in last_trade_time:
        remaining = COOLDOWN_SECONDS - (now - last_trade_time[pair])
        if remaining > 0:
            print(f"❌ BLOCKED | cooldown | {pair} | {round(remaining, 1)}s left")
            return "cooldown"

    if len(trades) >= MAX_OPEN_TRADES:
        print(f"❌ BLOCKED | max trades | open={len(trades)} max={MAX_OPEN_TRADES}")
        return "max trades"

    if ONE_TRADE_PER_PAIR:
        for t in trades:
            if t["instrument"] == pair:
                print(f"❌ BLOCKED | already open | {pair}")
                return "duplicate"

    if action == "buy" and not ALLOW_BUY:
        print("❌ BLOCKED | buy disabled")
        return "buy disabled"

    if action == "sell" and not ALLOW_SELL:
        print("❌ BLOCKED | sell disabled")
        return "sell disabled"

    price = get_price(pair)

    if pair in last_signal_price:
        move_pips = abs(price - last_signal_price[pair]) / pip_size(pair)

        if move_pips < MIN_MOVE_PIPS:
            print(f"❌ BLOCKED | no movement | {pair} | {round(move_pips, 1)} pips < {MIN_MOVE_PIPS}")
            last_signal_price[pair] = price
            return "no movement"

    last_signal_price[pair] = price

    if action == "buy":
        sl = price - (SL_PIPS * pip_size(pair))
        tp = price + (TP_PIPS * pip_size(pair))
        units = FIXED_UNITS
    else:
        sl = price + (SL_PIPS * pip_size(pair))
        tp = price - (TP_PIPS * pip_size(pair))
        units = -FIXED_UNITS

    order = {
        "order": {
            "instrument": pair,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": str(round_price(pair, sl))},
            "takeProfitOnFill": {"price": str(round_price(pair, tp))}
        }
    }

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    r = requests.post(url, headers=headers, json=order)

    if r.status_code == 201:
        print(f"✅ TRADE EXECUTED | {pair} | {action} | units={units}")
        last_trade_time[pair] = now
    else:
        print("❌ ORDER ERROR:", r.text)

    return "ok"

# =============================
# START
# =============================
threading.Thread(target=trade_manager, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
