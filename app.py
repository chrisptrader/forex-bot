
from flask import Flask, request, jsonify
import requests
import os
import time
from datetime import datetime, timezone

app = Flask(__name__)

# =========================
# BALANCED-AGGRESSIVE CONFIG
# =========================
OANDA_API_KEY = "98969b4679d01a139e86d66ee8694bef-6f46ee09cb98d79db97096b393622766"
ACCOUNT_ID = "101-001-37221732-001"
BASE_URL = "https://api-fxpractice.oanda.com/v3"

ALLOWED_PAIRS = ["EUR_USD"] # start with ONLY EURUSD
RISK_PERCENT = 0.75 # balanced-aggressive
MAX_OPEN_TRADES = 1
COOLDOWN_SECONDS = 300 # 5 min between entries
STOP_LOSS_PIPS = 20
TAKE_PROFIT_PIPS = 30
MAX_DAILY_LOSS_PERCENT = 2.0

# In-memory state
last_trade_time = {}
daily_start_balance = None
daily_date = None


# =========================
# BASIC HELPERS
# =========================
def now_utc_date():
    return datetime.now(timezone.utc).date().isoformat()


def normalize_pair(pair: str) -> str:
    pair = (pair or "").upper().strip()
    mapping = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "XAUUSD": "XAU_USD",
        "EUR_USD": "EUR_USD",
        "GBP_USD": "GBP_USD",
        "XAU_USD": "XAU_USD",
    }
    return mapping.get(pair, pair)


def oanda_headers():
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json"
    }


def pip_size(pair: str) -> float:
    if pair == "XAU_USD":
        return 0.1
    return 0.0001


def format_price(pair: str, price: float) -> str:
    if pair == "XAU_USD":
        return f"{price:.2f}"
    return f"{price:.5f}"


# =========================
# ACCOUNT / TRADES
# =========================
def get_account_summary():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/summary"
    response = requests.get(url, headers=oanda_headers(), timeout=20)
    data = response.json()
    print("Account summary response:", data)

    if response.status_code >= 300:
        raise Exception(f"Account summary error: {data}")

    return data["account"]


def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    response = requests.get(url, headers=oanda_headers(), timeout=20)
    data = response.json()
    print("Open trades response:", data)

    if response.status_code >= 300:
        raise Exception(f"Open trades error: {data}")

    return data.get("trades", [])


def count_open_trades():
    return len(get_open_trades())


def get_open_trade_for_pair(pair: str):
    trades = get_open_trades()
    for trade in trades:
        if trade.get("instrument") == pair:
            return trade
    return None


def close_trade(trade_id: str):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/close"
    response = requests.put(url, headers=oanda_headers(), timeout=20)
    data = response.json()
    print("Close trade response:", data)
    return data, response.status_code


def get_pricing(pair: str):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"
    response = requests.get(url, headers=oanda_headers(), timeout=20)
    data = response.json()
    print("Pricing response:", data)

    if response.status_code >= 300:
        raise Exception(f"Pricing error: {data}")

    prices = data.get("prices", [])
    if not prices:
        raise Exception(f"No pricing found for {pair}")

    bid = float(prices[0]["bids"][0]["price"])
    ask = float(prices[0]["asks"][0]["price"])
    return bid, ask


# =========================
# DAILY LOSS PROTECTION
# =========================
def refresh_daily_balance():
    global daily_start_balance, daily_date

    today = now_utc_date()
    if daily_date != today or daily_start_balance is None:
        acct = get_account_summary()
        daily_start_balance = float(acct["balance"])
        daily_date = today
        print(f"New daily baseline set: {daily_start_balance} on {daily_date}")


def daily_loss_blocked():
    refresh_daily_balance()
    acct = get_account_summary()
    current_balance = float(acct["balance"])
    drawdown = ((daily_start_balance - current_balance) / daily_start_balance) * 100.0

    print("Daily baseline:", daily_start_balance)
    print("Current balance:", current_balance)
    print("Daily drawdown %:", drawdown)

    return drawdown >= MAX_DAILY_LOSS_PERCENT, drawdown


# =========================
# RISK / POSITION SIZE
# =========================
def estimate_units(pair: str, signal: str):
    account = get_account_summary()
    balance = float(account["balance"])
    risk_amount = balance * (RISK_PERCENT / 100.0)

    bid, ask = get_pricing(pair)
    price = ask if signal == "BUY" else bid

    pip = pip_size(pair)

    # Very simple sizing estimate
    # For EUR/USD: pip value per 1,000 units ≈ $0.10
    # For GBP/USD: similar estimate
    # For XAU/USD: fallback small size
    if pair in ["EUR_USD", "GBP_USD"]:
        pip_value_per_1000 = 0.10
        thousands = risk_amount / (STOP_LOSS_PIPS * pip_value_per_1000)
        units = int(max(1000, round(thousands) * 1000))
    elif pair == "XAU_USD":
        units = 1
    else:
        units = 1000

    print("Estimated units:", units, "for balance:", balance, "risk_amount:", risk_amount, "price:", price)
    return units


def build_sl_tp(signal: str, pair: str):
    bid, ask = get_pricing(pair)
    entry = ask if signal == "BUY" else bid
    pip = pip_size(pair)

    if signal == "BUY":
        sl = entry - (STOP_LOSS_PIPS * pip)
        tp = entry + (TAKE_PROFIT_PIPS * pip)
    else:
        sl = entry + (STOP_LOSS_PIPS * pip)
        tp = entry - (TAKE_PROFIT_PIPS * pip)

    return format_price(pair, sl), format_price(pair, tp)


# =========================
# TRADE GUARDS
# =========================
def cooldown_active(pair: str):
    last_ts = last_trade_time.get(pair)
    if not last_ts:
        return False
    return (time.time() - last_ts) < COOLDOWN_SECONDS


def remember_trade(pair: str):
    last_trade_time[pair] = time.time()


def signal_matches_existing(signal: str, existing_trade):
    current_units = float(existing_trade.get("currentUnits", "0"))
    if signal == "BUY" and current_units > 0:
        return True
    if signal == "SELL" and current_units < 0:
        return True
    return False


# =========================
# ORDER PLACEMENT
# =========================
def place_trade(signal: str, pair: str):
    if pair not in ALLOWED_PAIRS:
        return {"blocked": True, "reason": f"{pair} not allowed right now"}, 400

    if signal not in ["BUY", "SELL"]:
        return {"blocked": True, "reason": "Invalid signal"}, 400

    blocked, dd = daily_loss_blocked()
    if blocked:
        return {"blocked": True, "reason": f"Daily loss cap hit ({dd:.2f}%)"}, 400

    if cooldown_active(pair):
        return {"blocked": True, "reason": f"Cooldown active for {pair}"}, 400

    existing_trade = get_open_trade_for_pair(pair)
    total_open = count_open_trades()

    if existing_trade:
        if signal_matches_existing(signal, existing_trade):
            return {"blocked": True, "reason": f"Same direction trade already open for {pair}"}, 400

        print("Opposite trade found, closing first:", existing_trade["id"])
        close_result, close_status = close_trade(existing_trade["id"])
        print("Close opposite result:", close_result)
        time.sleep(1)

    elif total_open >= MAX_OPEN_TRADES:
        return {"blocked": True, "reason": "Max open trades reached"}, 400

    units = estimate_units(pair, signal)
    signed_units = units if signal == "BUY" else -units
    sl_price, tp_price = build_sl_tp(signal, pair)

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    payload = {
        "order": {
            "units": str(signed_units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": sl_price
            },
            "takeProfitOnFill": {
                "price": tp_price
            }
        }
    }

    print("🚀 Sending trade:", payload)

    response = requests.post(
        url,
        json=payload,
        headers=oanda_headers(),
        timeout=20
    )

    try:
        result = response.json()
    except Exception:
        result = {"raw_text": response.text}

    print("💰 Trade response:", result)

    if response.status_code < 300:
        remember_trade(pair)

    return result, response.status_code


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Protected bot is LIVE 🚀"


@app.route("/status")
def status():
    refresh_daily_balance()
    return jsonify({
        "bot": "running",
        "allowed_pairs": ALLOWED_PAIRS,
        "risk_percent": RISK_PERCENT,
        "max_open_trades": MAX_OPEN_TRADES,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "stop_loss_pips": STOP_LOSS_PIPS,
        "take_profit_pips": TAKE_PROFIT_PIPS,
        "max_daily_loss_percent": MAX_DAILY_LOSS_PERCENT,
        "daily_start_balance": daily_start_balance,
        "daily_date": daily_date
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True)
        print("Raw webhook JSON:", data)

        if not data:
            return jsonify({"error": "No JSON body received"}), 400

        signal = str(data.get("signal", "")).upper().strip()
        pair = normalize_pair(data.get("pair", ""))

        print("Parsed signal:", signal)
        print("Parsed pair:", pair)
        print(f"🔥 SIGNAL RECEIVED: {signal} on {pair}")

        result, status_code = place_trade(signal, pair)
        return jsonify(result), status_code

    except Exception as e:
        print("Webhook execution error:", str(e))
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
