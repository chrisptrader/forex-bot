import os
import requests
from flask import Flask, request, jsonify
from datetime import datetime
import pytz

app = Flask(__name__)

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234")

BASE_RISK_PERCENT = float(os.getenv("BASE_RISK_PERCENT", "2.0"))
MAX_RISK_PERCENT = float(os.getenv("MAX_RISK_PERCENT", "2.5"))
MIN_RISK_PERCENT = float(os.getenv("MIN_RISK_PERCENT", "1.2"))

STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "60"))

BREAK_EVEN_TRIGGER = float(os.getenv("BREAK_EVEN_TRIGGER", "6"))
BREAK_EVEN_PLUS_PIPS = float(os.getenv("BREAK_EVEN_PLUS_PIPS", "1"))

PARTIAL_TP_TRIGGER = float(os.getenv("PARTIAL_TP_TRIGGER", "12"))
PARTIAL_CLOSE_PERCENT = float(os.getenv("PARTIAL_CLOSE_PERCENT", "50"))

TRAIL_TRIGGER = float(os.getenv("TRAIL_TRIGGER", "15"))
TRAIL_DISTANCE_PIPS = float(os.getenv("TRAIL_DISTANCE_PIPS", "8"))

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "2"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "180"))

TIMEZONE = os.getenv("TIMEZONE", "America/New_York")
LONDON_START = int(os.getenv("LONDON_START", "3"))
LONDON_END = int(os.getenv("LONDON_END", "6"))
NY_START = int(os.getenv("NY_START", "8"))
NY_END = int(os.getenv("NY_END", "11"))

ALLOWED_PAIRS = os.getenv(
    "ALLOWED_PAIRS",
    "EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CHF"
).replace(" ", "").split(",")

MAX_SAME_BIAS_TRADES = int(os.getenv("MAX_SAME_BIAS_TRADES", "1"))

BASE = "https://api-fxtrade.oanda.com/v3" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com/v3"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

trade_count_today = 0
last_trade_time = None
last_day = None
current_risk = BASE_RISK_PERCENT
partial_done = set()


def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def round_price(pair, price):
    return round(price, 3) if "JPY" in pair else round(price, 5)


def get_price(pair):
    r = requests.get(
        f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/pricing",
        headers=HEADERS,
        params={"instruments": pair},
        timeout=10
    )
    data = r.json()["prices"][0]
    bid = float(data["bids"][0]["price"])
    ask = float(data["asks"][0]["price"])
    return bid, ask


def get_balance():
    r = requests.get(
        f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/summary",
        headers=HEADERS,
        timeout=10
    )
    return float(r.json()["account"]["balance"])


def get_open_trades():
    r = requests.get(
        f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/openTrades",
        headers=HEADERS,
        timeout=10
    )
    return r.json().get("trades", [])


def calculate_units(pair):
    balance = get_balance()
    risk_amount = balance * (current_risk / 100)

    if "JPY" in pair:
        pip_value_per_unit = 0.000064
    else:
        pip_value_per_unit = 0.0001

    units = risk_amount / (STOP_LOSS_PIPS * pip_value_per_unit)
    return int(units)


def usd_bias(pair, action):
    if pair.endswith("_USD"):
        return "USD_STRONG" if action == "sell" else "USD_WEAK"
    if pair.startswith("USD_"):
        return "USD_STRONG" if action == "buy" else "USD_WEAK"
    return "OTHER"


def correlation_block(pair, action):
    new_bias = usd_bias(pair, action)

    if new_bias == "OTHER":
        return False

    count = 0

    for trade in get_open_trades():
        p = trade["instrument"]
        u = int(float(trade["currentUnits"]))
        a = "buy" if u > 0 else "sell"

        if usd_bias(p, a) == new_bias:
            count += 1

    return count >= MAX_SAME_BIAS_TRADES


def in_session():
    tz = pytz.timezone(TIMEZONE)
    hour = datetime.now(tz).hour
    return (LONDON_START <= hour <= LONDON_END) or (NY_START <= hour <= NY_END)


def can_trade():
    global trade_count_today, last_trade_time, last_day

    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    if last_day != now.date():
        trade_count_today = 0
        last_day = now.date()

    if len(get_open_trades()) >= MAX_OPEN_TRADES:
        return False, "max open trades"

    if trade_count_today >= MAX_TRADES_PER_DAY:
        return False, "max trades per day"

    if last_trade_time:
        if (now - last_trade_time).total_seconds() < MIN_SECONDS_BETWEEN_TRADES:
            return False, "cooldown"

    if not in_session():
        return False, "outside session"

    return True, "ok"


def modify_sl(trade_id, pair, new_sl):
    r = requests.put(
        f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders",
        headers=HEADERS,
        json={
            "stopLoss": {
                "timeInForce": "GTC",
                "price": str(round_price(pair, new_sl))
            }
        },
        timeout=10
    )
    return r.json()


def close_partial(trade_id, units):
    r = requests.put(
        f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close",
        headers=HEADERS,
        json={"units": str(abs(int(units)))},
        timeout=10
    )
    return r.json()


def manage_trades():
    results = []

    for trade in get_open_trades():
        trade_id = trade["id"]
        pair = trade["instrument"]
        units = int(float(trade["currentUnits"]))
        entry = float(trade["price"])

        if units == 0:
            continue

        bid, ask = get_price(pair)
        pip = pip_size(pair)

        if units > 0:
            profit_pips = (bid - entry) / pip
            direction = "buy"
        else:
            profit_pips = (entry - ask) / pip
            direction = "sell"

        # Break even earlier
        if profit_pips >= BREAK_EVEN_TRIGGER:
            new_sl = entry + BREAK_EVEN_PLUS_PIPS * pip if direction == "buy" else entry - BREAK_EVEN_PLUS_PIPS * pip
            res = modify_sl(trade_id, pair, new_sl)
            results.append({"trade": trade_id, "action": "break_even", "pips": round(profit_pips, 1), "sl": round_price(pair, new_sl), "response": res})

        # Auto partial close at +12 pips
        if profit_pips >= PARTIAL_TP_TRIGGER and trade_id not in partial_done:
            close_units = int(abs(units) * (PARTIAL_CLOSE_PERCENT / 100))
            if close_units > 0:
                res = close_partial(trade_id, close_units)
                partial_done.add(trade_id)
                results.append({"trade": trade_id, "action": "partial_close", "pips": round(profit_pips, 1), "closed_units": close_units, "response": res})

        # Trail tighter after +15
        if profit_pips >= TRAIL_TRIGGER:
            if direction == "buy":
                trail_sl = bid - TRAIL_DISTANCE_PIPS * pip
            else:
                trail_sl = ask + TRAIL_DISTANCE_PIPS * pip

            res = modify_sl(trade_id, pair, trail_sl)
            results.append({"trade": trade_id, "action": "trailing_stop", "pips": round(profit_pips, 1), "sl": round_price(pair, trail_sl), "response": res})

    return results


def place_trade(pair, action):
    global trade_count_today, last_trade_time

    bid, ask = get_price(pair)
    pip = pip_size(pair)
    units = calculate_units(pair)

    if action == "buy":
        entry = ask
        sl = entry - STOP_LOSS_PIPS * pip
        tp = entry + TAKE_PROFIT_PIPS * pip
    else:
        units = -units
        entry = bid
        sl = entry + STOP_LOSS_PIPS * pip
        tp = entry - TAKE_PROFIT_PIPS * pip

    order = {
        "order": {
            "instrument": pair,
            "units": str(units),
            "type": "MARKET",
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": str(round_price(pair, sl)),
                "timeInForce": "GTC"
            },
            "takeProfitOnFill": {
                "price": str(round_price(pair, tp)),
                "timeInForce": "GTC"
            }
        }
    }

    r = requests.post(
        f"{BASE}/accounts/{OANDA_ACCOUNT_ID}/orders",
        headers=HEADERS,
        json=order,
        timeout=10
    )

    trade_count_today += 1
    last_trade_time = datetime.now(pytz.timezone(TIMEZONE))

    return r.json()


@app.route("/")
def home():
    return "V48 running 🔥 auto partial + tighter profit lock"


@app.route("/health")
def health():
    return jsonify({
        "version": "v48",
        "risk": current_risk,
        "allowed_pairs": ALLOWED_PAIRS,
        "partial_trigger": PARTIAL_TP_TRIGGER,
        "trail_trigger": TRAIL_TRIGGER
    })


@app.route("/manage", methods=["GET", "POST"])
def manage():
    return jsonify({
        "status": "managed",
        "results": manage_trades()
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)

    print("WEBHOOK RECEIVED:", data, flush=True)

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "bad passphrase"}), 403

    pair = data.get("pair", "").upper()
    action = data.get("action", "").lower()

    manage_results = manage_trades()

    if pair not in ALLOWED_PAIRS:
        return jsonify({"blocked": "pair not allowed", "pair": pair})

    if action not in ["buy", "sell"]:
        return jsonify({"blocked": "bad action", "action": action})

    ok, reason = can_trade()
    if not ok:
        return jsonify({"blocked": reason, "manage_results": manage_results})

    if correlation_block(pair, action):
        return jsonify({"blocked": "correlation", "manage_results": manage_results})

    result = place_trade(pair, action)

    return jsonify({
        "status": "trade placed",
        "version": "v48",
        "pair": pair,
        "action": action,
        "risk": current_risk,
        "manage_results": manage_results,
        "order_response": result
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
