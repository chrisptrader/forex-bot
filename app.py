from flask import Flask, request, jsonify
import requests
import os
import time
import threading

app = Flask(__name__)

# ================= CONFIG =================
OANDA_API_KEY = "98969b4679d01a139e86d66ee8694bef-6f46ee09cb98d79db97096b393622766"
ACCOUNT_ID = "101-001-37221732-001"
BASE_URL = "https://api-fxpractice.oanda.com/v3"

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY"]

STOP_LOSS = {
    "EUR_USD": 20,
    "GBP_USD": 20,
    "USD_JPY": 20,
}

TAKE_PROFIT = {
    "EUR_USD": 30,
    "GBP_USD": 30,
    "USD_JPY": 30,
}

BREAK_EVEN = {
    "EUR_USD": 10,
    "GBP_USD": 10,
    "USD_JPY": 10,
}

UNITS = 5000
COOLDOWN = 300
AUTO_CHECK = 5

last_trade_time = {}

# ================= HELPERS =================
def headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }

def normalize(pair):
    if not pair:
        return None

    pair = pair.upper().replace("/", "").replace("_", "")

    mapping = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "USDJPY": "USD_JPY",
    }
    return mapping.get(pair)

def pip(pair):
    if pair.endswith("JPY"):
        return 0.01
    return 0.0001

def price_format(pair, value):
    if pair.endswith("JPY"):
        return f"{value:.3f}"
    return f"{value:.5f}"

def get_price(pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"
    r = requests.get(url, headers=headers(), timeout=15)
    data = r.json()

    prices = data.get("prices", [])
    if not prices:
        raise ValueError(f"No price data for {pair}: {data}")

    bid = float(prices[0]["bids"][0]["price"])
    ask = float(prices[0]["asks"][0]["price"])
    return bid, ask

def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=headers(), timeout=15)
    data = r.json()
    return data.get("trades", [])

def get_trade(pair):
    for t in get_open_trades():
        if t.get("instrument") == pair:
            return t
    return None

# ================= TRADE =================
def place_trade(signal, pair):
    if pair not in PAIRS:
        print("Pair not allowed:", pair)
        return

    now = time.time()
    if pair in last_trade_time and now - last_trade_time[pair] < COOLDOWN:
        print(f"{pair} cooldown active")
        return

    existing_trade = get_trade(pair)
    if existing_trade:
        print(f"{pair} already has trade - skipping")
        return

    bid, ask = get_price(pair)
    entry = ask if signal == "BUY" else bid

    sl_pips = STOP_LOSS[pair]
    tp_pips = TAKE_PROFIT[pair]

    if signal == "BUY":
        units = UNITS
        sl = entry - (sl_pips * pip(pair))
        tp = entry + (tp_pips * pip(pair))
    else:
        units = -UNITS
        sl = entry + (sl_pips * pip(pair))
        tp = entry - (tp_pips * pip(pair))

    order = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "timeInForce": "FOK",
            "stopLossOnFill": {
                "price": price_format(pair, sl)
            },
            "takeProfitOnFill": {
                "price": price_format(pair, tp)
            }
        }
    }

    print(f"🚀 {pair} Sending:", order)

    r = requests.post(
        f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders",
        headers=headers(),
        json=order,
        timeout=15
    )

    response_json = r.json()
    print("🔥 Response:", response_json)

    last_trade_time[pair] = now

# ================= BREAK EVEN =================
def check_be():
    trades = get_open_trades()

    for t in trades:
        pair = t.get("instrument")
        if pair not in BREAK_EVEN:
            continue

        entry = float(t["price"])
        units = float(t["currentUnits"])
        trade_id = t["id"]

        bid, ask = get_price(pair)
        current = bid if units < 0 else ask

        pips_profit = abs(current - entry) / pip(pair)
        print(f"{pair} pips in profit: {pips_profit}")

        if pips_profit >= BREAK_EVEN[pair]:
            url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"

            data = {
                "stopLoss": {
                    "price": price_format(pair, entry)
                }
            }

            r = requests.put(url, headers=headers(), json=data, timeout=15)
            print(f"🔒 {pair} moved to breakeven:", r.json())

def auto_loop():
    while True:
        try:
            check_be()
        except Exception as e:
            print("BE loop error:", str(e))
        time.sleep(AUTO_CHECK)

# ================= ROUTES =================
@app.route("/")
def home():
    return "FOREX BOT LIVE 🚀"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("Webhook:", data)

    signal = data.get("signal")
    pair = normalize(data.get("pair"))

    if signal in ["BUY", "SELL"] and pair:
        try:
            place_trade(signal, pair)
        except Exception as e:
            print("Trade error:", str(e))
            return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "ok"})

# ================= START =================
threading.Thread(target=auto_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
