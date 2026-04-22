
    import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------- CONFIG ----------------
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENV = os.getenv("OANDA_ENV", "practice")
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")

PAIR_LIST = ["EUR_USD", "GBP_USD", "USD_JPY"]

BASE_URL = "https://api-fxpractice.oanda.com"
if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

last_trade_time = {}

# ---------------- HELPERS ----------------
def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001

def safe_request(method, url, **kwargs):
    return requests.request(method, url, timeout=8, **kwargs)

# ---------------- PRICE FIX (KEY PART) ----------------
def get_prices(pair):
    try:
        url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
        r = safe_request("GET", url, headers=HEADERS, params={"instruments": pair})

        data = r.json()

        if "prices" not in data or len(data["prices"]) == 0:
            logging.error(f"NO PRICE DATA: {data}")
            return None

        return data["prices"][0]

    except Exception as e:
        logging.error(f"PRICE ERROR: {e}")
        return None

def get_bid_ask(pair):
    p = get_prices(pair)
    if not p:
        return None, None

    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])
    return bid, ask

def get_spread(pair):
    bid, ask = get_bid_ask(pair)
    if bid is None or ask is None:
        return 999
    return (ask - bid) / pip_size(pair)

# ---------------- MARKET LOGIC ----------------
def get_candles(pair):
    url = f"{BASE_URL}/v3/instruments/{pair}/candles"
    r = safe_request("GET", url, headers=HEADERS, params={"count": 20, "granularity": "M5"})
    data = r.json()

    candles = []
    for c in data.get("candles", []):
        mid = c["mid"]
        candles.append({
            "o": float(mid["o"]),
            "h": float(mid["h"]),
            "l": float(mid["l"]),
            "c": float(mid["c"])
        })
    return candles

def is_sideways(pair):
    candles = get_candles(pair)
    if len(candles) < 10:
        return True

    highs = [c["h"] for c in candles[-10:]]
    lows = [c["l"] for c in candles[-10:]]

    range_pips = (max(highs) - min(lows)) / pip_size(pair)

    return range_pips < 12

def pullback_confirmation(pair, side):
    candles = get_candles(pair)
    if len(candles) < 4:
        return False

    c1, c2, c3, c4 = candles[-4:]

    if side == "buy":
        return c4["c"] > c3["h"]

    if side == "sell":
        return c4["c"] < c3["l"]

    return False

# ---------------- TRADE ----------------
def place_trade(pair, side):
    bid, ask = get_bid_ask(pair)

    if bid is None or ask is None:
        logging.error("BLOCKED: no price data")
        return

    units = 5000
    sl_pips = 20
    tp_pips = 80

    p = pip_size(pair)

    if side == "buy":
        entry = ask
        sl = entry - sl_pips * p
        tp = entry + tp_pips * p
        units = abs(units)
    else:
        entry = bid
        sl = entry + sl_pips * p
        tp = entry - tp_pips * p
        units = -abs(units)

    body = {
        "order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{sl:.5f}"},
            "takeProfitOnFill": {"price": f"{tp:.5f}"}
        }
    }

    url = f"{BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = safe_request("POST", url, headers=HEADERS, json=body)
    logging.info(f"TRADE SENT: {r.json()}")

# ---------------- WEBHOOK ----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()

        if data.get("passphrase") != WEBHOOK_PASSPHRASE:
            return jsonify({"error": "bad passphrase"}), 403

        pair = data.get("pair")
        side = data.get("side")

        logging.info(f"WEBHOOK RECEIVED {pair} {side}")

        if pair not in PAIR_LIST:
            return jsonify({"error": "pair not allowed"}), 200

        # spread filter
        if get_spread(pair) > 12:
            logging.info("BLOCKED: spread")
            return jsonify({"blocked": "spread"}), 200

        # sideways filter
        if is_sideways(pair):
            logging.info("BLOCKED: market sideways")
            return jsonify({"blocked": "sideways"}), 200

        # pullback confirmation
        if not pullback_confirmation(pair, side):
            logging.info("BLOCKED: no confirmation")
            return jsonify({"blocked": "confirmation"}), 200

        place_trade(pair, side)

        return jsonify({"ok": True}), 200

    except Exception as e:
        logging.error(f"ERROR: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------- HOME ----------------
@app.route("/")
def home():
    return {"status": "running v28 stable"}

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
