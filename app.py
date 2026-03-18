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

PAIRS = ["EUR_USD", "GBP_USD", "XAU_USD", "USD_JPY"]

STOP_LOSS = {
    "EUR_USD": 20,
    "GBP_USD": 20,
    "XAU_USD": 200,
    "USD_JPY": 20
}

TAKE_PROFIT = {
    "EUR_USD": 30,
    "GBP_USD": 30,
    "XAU_USD": 300,
    "USD_JPY": 30
}

BREAK_EVEN = {
    "EUR_USD": 10,
    "GBP_USD": 10,
    "XAU_USD": 100,
    "USD_JPY": 10
}

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
    pair = pair.upper()
    mapping = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "XAUUSD": "XAU_USD",
        "USDJPY": "USD_JPY"
    }
    return mapping.get(pair)

def pip(pair):
    if pair == "XAU_USD":
        return 0.1
    if pair.endswith("JPY"):
        return 0.01
    return 0.0001

def get_price(pair):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"
    r = requests.get(url, headers=headers()).json()
    bid = float(r["prices"][0]["bids"][0]["price"])
    ask = float(r["prices"][0]["asks"][0]["price"])
    return bid, ask

def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=headers()).json()
    return r.get("trades", [])

def get_trade(pair):
    for t in get_open_trades():
        if t["instrument"] == pair:
            return t
    return None

# ================= TRADE =================
def place_trade(signal, pair):
    if pair not in PAIRS:
        print("Pair not allowed:", pair)
        return

    if pair in last_trade_time:
        if time.time() - last_trade_time[pair] < COOLDOWN:
            print(f"{pair} cooldown active")
            return

    if get_trade(pair):
        print(f"{pair} already has trade — skipping")
        return

    bid, ask = get_price(pair)
    entry = ask if signal == "BUY" else bid

    sl_pips = STOP_LOSS[pair]
    tp_pips = TAKE_PROFIT[pair]

    if signal == "BUY":
        sl = entry - sl_pips * pip(pair)
        tp = entry + tp_pips * pip(pair)
        units = 1000
    else:
        sl = entry + sl_pips * pip(pair)
        tp = entry - tp_pips * pip(pair)
        units = -1000

    price_format = "{:.3f}" if pair.endswith("JPY") else "{:.5f}"

    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": price_format.format(sl)},
            "takeProfitOnFill": {"price": price_format.format(tp)}
        }
    }

    print(f"🚀 {pair} Sending:", data)

    r = requests.post(
        f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders",
        headers=headers(),
        json=data
    )

    print("💰 Response:", r.json())
    last_trade_time[pair] = time.time()

# ================= BREAK EVEN =================
def check_be():
    trades = get_open_trades()

    for t in trades:
        pair = t["instrument"]
        entry = float(t["price"])
        units = float(t["currentUnits"])
        trade_id = t["id"]

        bid, ask = get_price(pair)
        current = ask if units > 0 else bid

        pips_profit = abs(current - entry) / pip(pair)
        print(pair, "pips:", pips_profit)

        if pips_profit >= BREAK_EVEN.get(pair, 10):
            url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
            price_format = "{:.3f}" if pair.endswith("JPY") else "{:.5f}"

            data = {
                "stopLoss": {
                    "price": price_format.format(entry)
                }
            }

            requests.put(url, headers=headers(), json=data)
            print(f"🔒 {pair} moved to BE")

def auto_loop():
    while True:
        check_be()
        time.sleep(AUTO_CHECK)

# ================= ROUTES =================
@app.route("/")
def home():
    return "MULTI PAIR BOT LIVE 🚀"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("Webhook:", data)

    signal = data.get("signal")
    pair = normalize(data.get("pair"))

    if signal in ["BUY", "SELL"] and pair:
        place_trade(signal, pair)

    return jsonify({"status": "ok"})

# ================= START =================
threading.Thread(target=auto_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
