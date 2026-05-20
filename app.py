import os, time, threading, requests
from flask import Flask, request, jsonify

app = Flask(__name__)

OANDA_API_KEY = (os.getenv("OANDA_API_KEY") or "").strip()
ACCOUNT_ID = (os.getenv("OANDA_ACCOUNT_ID") or os.getenv("ACCOUNT_ID") or "").strip()
OANDA_ENV = (os.getenv("OANDA_ENV") or "live").strip().lower()

BASE_URL = "https://api-fxtrade.oanda.com/v3" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com/v3"

WEBHOOK_PASSPHRASE = (os.getenv("WEBHOOK_PASSPHRASE") or "").strip()

ALLOW_BUY = os.getenv("ALLOW_BUY", "true").lower() == "true"
ALLOW_SELL = os.getenv("ALLOW_SELL", "true").lower() == "true"

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 2))
ONE_TRADE_PER_PAIR = os.getenv("ONE_TRADE_PER_PAIR", "true").lower() == "true"
COOLDOWN_SECONDS = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", 90))

FIXED_UNITS = int(os.getenv("FIXED_UNITS", 1000))

SL_PIPS = float(os.getenv("STOP_LOSS_PIPS", 10))
TP_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", 28))
MIN_MOVE_PIPS = float(os.getenv("MIN_MOVE_PIPS", 4))

USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true").lower() == "true"
BREAK_EVEN_TRIGGER = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", 6))
BREAK_EVEN_PLUS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", 1))

USE_PARTIAL_CLOSE = os.getenv("USE_PARTIAL_CLOSE", "true").lower() == "true"
PARTIAL_TRIGGER = float(os.getenv("PARTIAL_CLOSE_TRIGGER_PIPS", 18))
PARTIAL_PERCENT = float(os.getenv("PARTIAL_CLOSE_PERCENT", 50))

USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "true").lower() == "true"
TRAILING_TRIGGER = float(os.getenv("TRAILING_TRIGGER_PIPS", 14))
TRAILING_DISTANCE = float(os.getenv("TRAILING_DISTANCE_PIPS", 7))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", 10))

last_trade_time = {}
last_signal_price = {}
partial_closed = set()

session = requests.Session()
session.trust_env = False

headers = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001

def round_price(pair, price):
    return round(price, 3 if "JPY" in pair else 5)

def oanda_get(path, params=None):
    url = f"{BASE_URL}{path}"
    r = session.get(url, headers=headers, params=params, timeout=5)
    try:
        data = r.json()
    except Exception:
        data = {"error": r.text}
    if r.status_code >= 400:
        print("❌ OANDA GET ERROR:", r.status_code, data)
        return None
    return data

def oanda_post(path, payload):
    url = f"{BASE_URL}{path}"
    r = session.post(url, headers=headers, json=payload, timeout=8)
    try:
        data = r.json()
    except Exception:
        data = {"error": r.text}
    return r.status_code, data

def oanda_put(path, payload):
    url = f"{BASE_URL}{path}"
    r = session.put(url, headers=headers, json=payload, timeout=5)
    try:
        data = r.json()
    except Exception:
        data = {"error": r.text}
    return r.status_code, data

def get_price(pair):
    data = oanda_get(f"/accounts/{ACCOUNT_ID}/pricing", {"instruments": pair})
    if not data or "prices" not in data:
        print(f"❌ PRICE ERROR | {pair} | response={data}")
        return None
    return float(data["prices"][0]["bids"][0]["price"])

def get_open_trades():
    data = oanda_get(f"/accounts/{ACCOUNT_ID}/openTrades")
    if not data:
        return []
    return data.get("trades", [])

def close_partial(trade_id, units):
    return oanda_put(f"/accounts/{ACCOUNT_ID}/trades/{trade_id}/close", {
        "units": str(abs(int(units)))
    })

def modify_sl(pair, trade_id, price):
    return oanda_put(f"/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders", {
        "stopLoss": {"price": str(round_price(pair, price))}
    })

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
                if price is None:
                    continue

                direction = 1 if units > 0 else -1
                pips = ((price - entry) / pip_size(pair)) * direction

                print(f"[MANAGER] {pair} | {round(pips, 1)} pips")

                if USE_BREAK_EVEN and pips >= BREAK_EVEN_TRIGGER:
                    new_sl = entry + (BREAK_EVEN_PLUS * pip_size(pair) * direction)
                    modify_sl(pair, trade_id, new_sl)
                    print(f"[BE MOVED] {pair}")

                if USE_PARTIAL_CLOSE and pips >= PARTIAL_TRIGGER and trade_id not in partial_closed:
                    close_units = abs(int(units)) * (PARTIAL_PERCENT / 100)
                    close_partial(trade_id, close_units)
                    partial_closed.add(trade_id)
                    print(f"[PARTIAL CLOSED] {pair}")

                if USE_TRAILING_STOP and pips >= TRAILING_TRIGGER:
                    new_sl = price - (TRAILING_DISTANCE * pip_size(pair) * direction)
                    modify_sl(pair, trade_id, new_sl)
                    print(f"[TRAILING] {pair}")

        except Exception as e:
            print("Manager error:", e)

        time.sleep(POLL_SECONDS)

@app.route("/", methods=["GET"])
def home():
    return "LIVE BOT ONLINE"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        print("❌ INVALID PASSPHRASE")
        return jsonify({"status": "invalid passphrase"}), 403

    pair = data.get("pair")
    action = data.get("action")

    print(f"📩 SIGNAL | {pair} | {action}")

    if not pair or action not in ["buy", "sell"]:
        return jsonify({"status": "bad signal"}), 400

    if action == "buy" and not ALLOW_BUY:
        return jsonify({"status": "buy disabled"}), 200

    if action == "sell" and not ALLOW_SELL:
        return jsonify({"status": "sell disabled"}), 200

    now = time.time()

    if pair in last_trade_time:
        remaining = COOLDOWN_SECONDS - (now - last_trade_time[pair])
        if remaining > 0:
            print(f"❌ BLOCKED | cooldown | {round(remaining,1)}s")
            return jsonify({"status": "cooldown"}), 200

    trades = get_open_trades()

    if len(trades) >= MAX_OPEN_TRADES:
        print("❌ BLOCKED | max trades")
        return jsonify({"status": "max trades"}), 200

    if ONE_TRADE_PER_PAIR:
        for t in trades:
            if t["instrument"] == pair:
                print("❌ BLOCKED | duplicate pair")
                return jsonify({"status": "duplicate"}), 200

    price = get_price(pair)
    if price is None:
        return jsonify({"status": "price error"}), 200

    if pair in last_signal_price:
        move_pips = abs(price - last_signal_price[pair]) / pip_size(pair)
        if move_pips < MIN_MOVE_PIPS:
            last_signal_price[pair] = price
            print(f"❌ BLOCKED | no movement | {round(move_pips,1)} pips")
            return jsonify({"status": "no movement"}), 200

    last_signal_price[pair] = price

    if action == "buy":
        units = FIXED_UNITS
        sl = price - SL_PIPS * pip_size(pair)
        tp = price + TP_PIPS * pip_size(pair)
    else:
        units = -FIXED_UNITS
        sl = price + SL_PIPS * pip_size(pair)
        tp = price - TP_PIPS * pip_size(pair)

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

    status, result = oanda_post(f"/accounts/{ACCOUNT_ID}/orders", order)

    if status in [200, 201]:
        print(f"✅ TRADE EXECUTED | {pair} | {action} | units={units}")
        last_trade_time[pair] = now
        return jsonify({"status": "executed", "pair": pair, "action": action}), 200

    print("❌ ORDER ERROR:", status, result)
    return jsonify({"status": "order error", "details": result}), 200

threading.Thread(target=trade_manager, daemon=True).start()
