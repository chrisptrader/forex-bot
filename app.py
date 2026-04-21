import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ================= CONFIG =================

def env(name, default):
    return os.getenv(name, str(default))

OANDA_API_KEY = env("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = env("OANDA_ACCOUNT_ID", "")
OANDA_ENV = env("OANDA_ENV", "practice").lower()
WEBHOOK_PASSPHRASE = env("WEBHOOK_PASSPHRASE", "1234")

RISK_PERCENT = float(env("RISK_PERCENT", 0.02))
FIXED_UNITS = int(env("FIXED_UNITS", 5000))

STOP_LOSS_PIPS = float(env("STOP_LOSS_PIPS", 20))
TAKE_PROFIT_PIPS = float(env("TAKE_PROFIT_PIPS", 80))

MAX_SPREAD_PIPS = float(env("MAX_SPREAD_PIPS", 10))

TIMEZONE = env("TIMEZONE_NAME", "America/New_York")

BASE_URL = "https://api-fxpractice.oanda.com" if OANDA_ENV != "live" else "https://api-fxtrade.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

last_trade_time = {}
trade_cooldown = 60  # seconds

# ================= HELPERS =================

def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001

def now():
    return datetime.now(ZoneInfo(TIMEZONE))

def get_price(pair):
    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=HEADERS, params={"instruments": pair})
    data = r.json()["prices"][0]
    bid = float(data["bids"][0]["price"])
    ask = float(data["asks"][0]["price"])
    spread = abs(ask - bid)
    return bid, ask, spread

def spread_ok(pair):
    bid, ask, spread = get_price(pair)
    spread_pips = spread / pip_size(pair)
    if spread_pips > MAX_SPREAD_PIPS:
        logging.info(f"BLOCKED spread too high: {spread_pips}")
        return False
    return True

def get_candles(pair):
    url = f"{BASE_URL}/v3/instruments/{pair}/candles"
    params = {"granularity": "M1", "count": 5}
    r = requests.get(url, headers=HEADERS, params=params)
    candles = r.json()["candles"]
    return candles

# ================= STRATEGY =================

def is_trending(pair):
    candles = get_candles(pair)
    closes = [float(c["mid"]["c"]) for c in candles if c["complete"]]

    if len(closes) < 3:
        return False

    move = abs(closes[-1] - closes[0]) / pip_size(pair)

    # 🔥 Skip sideways
    return move > 3  # must move at least 3 pips

def confirm_entry(pair, side):
    candles = get_candles(pair)

    if len(candles) < 3:
        return False

    last = candles[-1]
    prev = candles[-2]

    last_close = float(last["mid"]["c"])
    last_open = float(last["mid"]["o"])
    prev_close = float(prev["mid"]["c"])
    prev_open = float(prev["mid"]["o"])

    # 🔥 confirmation logic
    if side == "buy":
        return last_close > last_open and last_close > prev_close
    else:
        return last_close < last_open and last_close < prev_close

# ================= EXECUTION =================

def place_trade(pair, side):
    if not spread_ok(pair):
        return

    if not is_trending(pair):
        logging.info("BLOCKED: market sideways")
        return

    if not confirm_entry(pair, side):
        logging.info("BLOCKED: no confirmation")
        return

    now_time = time.time()
    if pair in last_trade_time and now_time - last_trade_time[pair] < trade_cooldown:
        logging.info("BLOCKED: cooldown")
        return

    last_trade_time[pair] = now_time

    units = FIXED_UNITS if side == "buy" else -FIXED_UNITS

    bid, ask, _ = get_price(pair)
    price = ask if side == "buy" else bid

    pip = pip_size(pair)

    sl = price - STOP_LOSS_PIPS * pip if side == "buy" else price + STOP_LOSS_PIPS * pip
    tp = price + TAKE_PROFIT_PIPS * pip if side == "buy" else price - TAKE_PROFIT_PIPS * pip

    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{sl:.5f}"},
            "takeProfitOnFill": {"price": f"{tp:.5f}"}
        }
    }

    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=HEADERS, json=data)

    if r.status_code == 201:
        logging.info(f"TRADE OPENED {pair} {side}")
    else:
        logging.error(f"ORDER FAILED {r.text}")

# ================= WEBHOOK =================

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "unauthorized"}), 403

    pair = data.get("pair")
    side = data.get("side")

    logging.info(f"WEBHOOK RECEIVED {pair} {side}")

    place_trade(pair, side)

    return jsonify({"status": "ok"})

@app.route("/")
def home():
    return "BOT RUNNING"

# ================= RUN =================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
