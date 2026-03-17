
from flask import Flask, request, jsonify
import requests
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.environ.get("OANDA_API_KEY", "").strip()
ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
BASE_URL = os.environ.get("OANDA_BASE_URL", "https://api-fxpractice.oanda.com/v3").strip()

# Safer default size
OANDA_UNITS = int(os.environ.get("OANDA_UNITS", "1000"))

# Allowed pairs
PAIRS = ["EUR_USD", "GBP_USD", "XAU_USD"]

# Risk / management
MAX_OPEN_TRADES = int(os.environ.get("MAX_OPEN_TRADES", "1"))
PAIR_COOLDOWN_SECONDS = int(os.environ.get("PAIR_COOLDOWN_SECONDS", "300"))

# Trailing stop
ENABLE_TRAILING = os.environ.get("TRAILING_STOP", "true").lower() == "true"
TRAILING_PIPS = float(os.environ.get("TRAILING_PIPS", "15"))

# SL / TP
STOP_LOSS_PIPS = float(os.environ.get("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.environ.get("TAKE_PROFIT_PIPS", "40"))

# Session filter
SESSION_TIMEZONE = os.environ.get("SESSION_TIMEZONE", "America/New_York")
SESSION_START_HOUR = int(os.environ.get("SESSION_START_HOUR", "7"))
SESSION_END_HOUR = int(os.environ.get("SESSION_END_HOUR", "17"))

# Memory
last_trade_time_by_pair = {}

# =========================
# HELPERS
# =========================
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


def is_session_open() -> bool:
    try:
        now_local = datetime.now(ZoneInfo(SESSION_TIMEZONE))
        return SESSION_START_HOUR <= now_local.hour < SESSION_END_HOUR
    except Exception as e:
        print("Session timezone error:", str(e))
        return True # fail open so timezone issue doesn't kill bot


def pip_size(pair: str) -> float:
    if pair == "XAU_USD":
        return 0.1
    return 0.0001


def format_price(pair: str, price: float) -> str:
    if pair == "XAU_USD":
        return f"{price:.2f}"
    return f"{price:.5f}"


def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    response = requests.get(url, headers=oanda_headers(), timeout=20)

    try:
        data = response.json()
    except Exception:
        print("Could not decode open trades response:", response.text)
        return []

    if response.status_code >= 300:
        print("Open trades error:", data)
        return []

    return data.get("trades", [])


def count_open_trades():
    return len(get_open_trades())


def get_open_trade_for_pair(pair: str):
    trades = get_open_trades()
    for trade in trades:
        if trade.get("instrument") == pair:
            return trade
    return None


def get_pricing(pair: str):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"
    response = requests.get(url, headers=oanda_headers(), timeout=20)

    try:
        data = response.json()
    except Exception:
        raise Exception(f"Could not decode pricing response: {response.text}")

    print("Pricing response:", data)

    if response.status_code >= 300:
        raise Exception(f"OANDA pricing error: {data}")

    prices = data.get("prices", [])
    if not prices:
        raise Exception(f"No pricing returned for {pair}")

    bid = float(prices[0]["bids"][0]["price"])
    ask = float(prices[0]["asks"][0]["price"])
    return bid, ask


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


def should_block_for_cooldown(pair: str):
    now_ts = time.time()
    last_ts = last_trade_time_by_pair.get(pair)
    if last_ts is None:
        return False
    return (now_ts - last_ts) < PAIR_COOLDOWN_SECONDS


def remember_trade_time(pair: str):
    last_trade_time_by_pair[pair] = time.time()


def close_trade(trade_id: str):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/close"
    response = requests.put(url, headers=oanda_headers(), timeout=20)

    try:
        result = response.json()
    except Exception:
        result = {"raw_text": response.text}

    print("Close trade result:", result)
    return result


def should_trade(signal: str, pair: str):
    if pair not in PAIRS:
        return False, "Pair not allowed"

    if signal not in ["BUY", "SELL"]:
        return False, "Invalid signal"

    if not is_session_open():
        return False, "Outside trading session"

    return True, "OK"


def place_trade(signal: str, pair: str):
    pair = normalize_pair(pair)

    allowed, reason = should_trade(signal, pair)
    if not allowed:
        return {"blocked": True, "reason": reason}

    if should_block_for_cooldown(pair):
        return {"blocked": True, "reason": f"Cooldown active for {pair}"}

    total_open = count_open_trades()
    existing_trade = get_open_trade_for_pair(pair)

    if total_open >= MAX_OPEN_TRADES and not existing_trade:
        return {"blocked": True, "reason": "Max open trades reached"}

    if existing_trade:
        current_units = float(existing_trade.get("currentUnits", "0"))

        # Same direction already open
        if (signal == "BUY" and current_units > 0) or (signal == "SELL" and current_units < 0):
            return {"blocked": True, "reason": f"Same direction trade already open for {pair}"}

        # Opposite trade exists -> close first
        print("Opposite trade found, closing first:", existing_trade.get("id"))
        close_result = close_trade(existing_trade["id"])
        print("Closed opposite trade result:", close_result)
        time.sleep(1)

    units = OANDA_UNITS if signal == "BUY" else -OANDA_UNITS
    sl_price, tp_price = build_sl_tp(signal, pair)

    order_payload = {
        "order": {
            "units": str(units),
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

    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    print("Placing trade payload:", order_payload)

    response = requests.post(
        url,
        json=order_payload,
        headers=oanda_headers(),
        timeout=20
    )

    try:
        result = response.json()
    except Exception:
        result = {"raw_text": response.text}

    result["http_status"] = response.status_code
    print("OANDA order response:", result)

    if response.status_code < 300:
        remember_trade_time(pair)

    return result


# =========================
# TRAILING MANAGER
# =========================
def manage_trades():
    while True:
        try:
            if ENABLE_TRAILING:
                trades = get_open_trades()

                for trade in trades:
                    trade_id = trade.get("id")
                    pair = trade.get("instrument")
                    current_units = float(trade.get("currentUnits", "0"))
                    unrealized_pl = float(trade.get("unrealizedPL", "0"))

                    if not trade_id or not pair:
                        continue

                    if unrealized_pl <= 0:
                        continue

                    bid, ask = get_pricing(pair)
                    pip = pip_size(pair)

                    if current_units > 0:
                        new_sl = bid - (TRAILING_PIPS * pip)
                    else:
                        new_sl = ask + (TRAILING_PIPS * pip)

                    payload = {
                        "stopLoss": {
                            "price": format_price(pair, new_sl)
                        }
                    }

                    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
                    response = requests.put(
                        url,
                        json=payload,
                        headers=oanda_headers(),
                        timeout=20
                    )

                    try:
                        trailing_result = response.json()
                    except Exception:
                        trailing_result = {"raw_text": response.text}

                    print("Trailing update result:", trailing_result)

        except Exception as e:
            print("Error in trailing manager:", str(e))

        time.sleep(20)


threading.Thread(target=manage_trades, daemon=True).start()

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Forex bot is running!"


@app.route("/status")
def status():
    return jsonify({
        "bot": "running",
        "account_id_set": bool(ACCOUNT_ID),
        "api_key_set": bool(OANDA_API_KEY),
        "base_url": BASE_URL,
        "pairs": PAIRS,
        "units": OANDA_UNITS,
        "trailing": ENABLE_TRAILING,
        "trailing_pips": TRAILING_PIPS,
        "stop_loss_pips": STOP_LOSS_PIPS,
        "take_profit_pips": TAKE_PROFIT_PIPS,
        "max_open_trades": MAX_OPEN_TRADES,
        "cooldown_seconds": PAIR_COOLDOWN_SECONDS,
        "session_timezone": SESSION_TIMEZONE,
        "session_start_hour": SESSION_START_HOUR,
        "session_end_hour": SESSION_END_HOUR
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

        if signal not in ["BUY", "SELL"]:
            return jsonify({"error": "Invalid signal"}), 400

        if pair not in PAIRS:
            return jsonify({"error": f"Pair not allowed: {pair}"}), 400

        if not OANDA_API_KEY or not ACCOUNT_ID:
            return jsonify({"error": "Missing OANDA credentials"}), 500

        result = place_trade(signal, pair)
        print("Trade result:", result)
        return jsonify(result), 200

    except Exception as e:
        print("Webhook execution error:", str(e))
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
