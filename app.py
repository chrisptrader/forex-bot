import os
import time
import requests
import pandas as pd
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
# ENV VARIABLES
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
BASE_URL = "https://api-fxpractice.oanda.com/v3"

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.02"))

STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "80"))

BUY_PULLBACK_PIPS = float(os.getenv("BUY_PULLBACK_PIPS", "1.0"))
SELL_BOUNCE_PIPS = float(os.getenv("SELL_BOUNCE_PIPS", "1.0"))

BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "5"))
MOMENTUM_MIN_BODY_PIPS = float(os.getenv("MOMENTUM_MIN_BODY_PIPS", "1.2"))

ENABLE_TRAILING = os.getenv("ENABLE_TRAILING", "True") == "True"
BREAK_EVEN_TRIGGER = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "12"))
TRAILING_TRIGGER = float(os.getenv("TRAILING_TRIGGER_PIPS", "22"))
TRAILING_DISTANCE = float(os.getenv("TRAILING_DISTANCE_PIPS", "12"))

MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "20"))

last_trade_time = {}

# =========================
# HELPERS
# =========================
def get_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

def get_price(instrument):
    url = f"{BASE_URL}/accounts/{OANDA_ACCOUNT_ID}/pricing"
    params = {"instruments": instrument}
    r = requests.get(url, headers=get_headers(), params=params).json()
    return float(r["prices"][0]["bids"][0]["price"])

def get_candles(instrument, granularity="M5", count=20):
    url = f"{BASE_URL}/instruments/{instrument}/candles"
    params = {"granularity": granularity, "count": count}
    r = requests.get(url, headers=get_headers(), params=params).json()
    candles = r["candles"]

    data = []
    for c in candles:
        data.append({
            "open": float(c["mid"]["o"]),
            "high": float(c["mid"]["h"]),
            "low": float(c["mid"]["l"]),
            "close": float(c["mid"]["c"]),
        })
    return pd.DataFrame(data)

def get_pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001

# =========================
# ENTRY LOGIC (V23)
# =========================
def passes_buy_structure(df, pip):
    if len(df) < BREAKOUT_LOOKBACK + 3:
        return False

    recent = df.tail(BREAKOUT_LOOKBACK + 2)
    last = recent.iloc[-2]
    prev_high = recent.iloc[:-2]["high"].max()

    breakout = (last["close"] - prev_high) / pip
    body = abs(last["close"] - last["open"]) / pip
    pullback = abs(recent.iloc[-1]["low"] - last["close"]) / pip

    if breakout < 0.3:
        return False
    if body < MOMENTUM_MIN_BODY_PIPS:
        return False
    if pullback > BUY_PULLBACK_PIPS:
        return False

    return True

def passes_sell_structure(df, pip):
    if len(df) < BREAKOUT_LOOKBACK + 3:
        return False

    recent = df.tail(BREAKOUT_LOOKBACK + 2)
    last = recent.iloc[-2]
    prev_low = recent.iloc[:-2]["low"].min()

    breakdown = (prev_low - last["close"]) / pip
    body = abs(last["close"] - last["open"]) / pip
    bounce = abs(last["close"] - recent.iloc[-1]["high"]) / pip

    if breakdown < 0.3:
        return False
    if body < MOMENTUM_MIN_BODY_PIPS:
        return False
    if bounce > SELL_BOUNCE_PIPS:
        return False

    return True

# =========================
# ORDER EXECUTION
# =========================
def place_trade(pair, side):
    pip = get_pip_size(pair)
    price = get_price(pair)

    units = 5000  # keep simple for now

    if side == "BUY":
        sl = price - (STOP_LOSS_PIPS * pip)
        tp = price + (TAKE_PROFIT_PIPS * pip)
    else:
        sl = price + (STOP_LOSS_PIPS * pip)
        tp = price - (TAKE_PROFIT_PIPS * pip)

    order = {
        "order": {
            "instrument": pair,
            "units": str(units if side == "BUY" else -units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": str(round(sl, 5))},
            "takeProfitOnFill": {"price": str(round(tp, 5))}
        }
    }

    url = f"{BASE_URL}/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=get_headers(), json=order)
    print("TRADE:", pair, side, r.json())

# =========================
# WEBHOOK
# =========================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    pair = data.get("pair")
    side = data.get("side")

    now = time.time()

    if pair in last_trade_time:
        if now - last_trade_time[pair] < MIN_SECONDS_BETWEEN_TRADES:
            return jsonify({"msg": "cooldown active"})

    df = get_candles(pair)
    pip = get_pip_size(pair)

    if side == "BUY":
        if not passes_buy_structure(df, pip):
            return jsonify({"msg": "buy blocked"})
    else:
        if not passes_sell_structure(df, pip):
            return jsonify({"msg": "sell blocked"})

    place_trade(pair, side)
    last_trade_time[pair] = now

    return jsonify({"msg": "trade placed"})

# =========================
# RUN
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
