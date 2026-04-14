from flask import Flask, request, jsonify
import os
import time
import json
import threading
import requests

app = Flask(__name__)

# =========================================================
# CONFIG
# =========================================================
OANDA_API_KEY = os.environ.get("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.environ.get("OANDA_ENV", "practice").strip().lower()

if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com/v3"
else:
    BASE_URL = "https://api-fxpractice.oanda.com/v3"

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "XAU_USD"]

UNITS = int(os.environ.get("UNITS", "3000"))
AUTO_CHECK_SECONDS = int(os.environ.get("AUTO_CHECK_SECONDS", "10"))
MAX_TOTAL_OPEN_TRADES = int(os.environ.get("MAX_TOTAL_OPEN_TRADES", "2"))

STOP_LOSS = {
    "EUR_USD": 12,
    "GBP_USD": 14,
    "USD_JPY": 12,
    "XAU_USD": 250,
}

TAKE_PROFIT = {
    "EUR_USD": 24,
    "GBP_USD": 28,
    "USD_JPY": 20,
    "XAU_USD": 500,
}

BREAK_EVEN = {
    "EUR_USD": 8,
    "GBP_USD": 10,
    "USD_JPY": 7,
    "XAU_USD": 180,
}

TRAIL_AFTER = {
    "EUR_USD": 12,
    "GBP_USD": 14,
    "USD_JPY": 10,
    "XAU_USD": 250,
}

TRAIL_LOCK = {
    "EUR_USD": 6,
    "GBP_USD": 7,
    "USD_JPY": 5,
    "XAU_USD": 120,
}

last_trade_time = {}
managed_to_be = set()

# =========================================================
# HELPERS
# =========================================================
def log(message: str) -> None:
    print(message, flush=True)


def headers() -> dict:
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json",
    }


def normalize_pair(pair: str | None) -> str | None:
    if not pair:
        return None
    p = str(pair).strip().upper().replace("/", "_")
    mapping = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "USDJPY": "USD_JPY",
        "XAUUSD": "XAU_USD",
        "EUR_USD": "EUR_USD",
        "GBP_USD": "GBP_USD",
        "USD_JPY": "USD_JPY",
        "XAU_USD": "XAU_USD",
    }
    return mapping.get(p)


def normalize_signal(signal: str | None) -> str | None:
    if not signal:
        return None
    s = str(signal).strip().upper()
    return s if s in {"BUY", "SELL"} else None


def pip_size(pair: str) -> float:
    if pair == "USD_JPY":
        return 0.01
    if pair == "XAU_USD":
        return 0.01
    return 0.0001


def price_format(pair: str, price: float) -> str:
    if pair == "USD_JPY":
        return f"{price:.3f}"
    if pair == "XAU_USD":
        return f"{price:.2f}"
    return f"{price:.5f}"


# =========================================================
# OANDA API
# =========================================================
def oanda_get(endpoint: str) -> dict:
    url = f"{BASE_URL}{endpoint}"
    r = requests.get(url, headers=headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def oanda_post(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    r = requests.post(url, headers=headers(), json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def oanda_put(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    r = requests.put(url, headers=headers(), json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def get_pricing(instruments: list[str]) -> dict[str, dict]:
    joined = ",".join(instruments)
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/pricing?instruments={joined}")
    out = {}

    for item in data.get("prices", []):
        instrument = item["instrument"]
        bid = float(item["bids"][0]["price"])
        ask = float(item["asks"][0]["price"])
        out[instrument] = {"bid": bid, "ask": ask}

    return out


def get_current_price(pair: str) -> dict:
    prices = get_pricing([pair])
    if pair not in prices:
        raise RuntimeError(f"No pricing returned for {pair}")
    return prices[pair]


def get_open_trades() -> list[dict]:
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    return data.get("trades", [])


def get_open_trade_for_pair(pair: str) -> dict | None:
    for trade in get_open_trades():
        if trade.get("instrument") == pair:
            return trade
    return None


def open_trade_slots_available() -> bool:
    return len(get_open_trades()) < MAX_TOTAL_OPEN_TRADES


# =========================================================
# WEBHOOK PARSER
# =========================================================
def parse_webhook_payload() -> dict:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    raw = request.get_data(as_text=True).strip()
    if not raw:
        raise ValueError("empty payload")

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            parsed2 = json.loads(parsed)
            if isinstance(parsed2, dict):
                return parsed2
    except Exception:
        pass

    cleaned = raw.strip().strip('"').replace('\\"', '"')
    parsed = json.loads(cleaned)
    if isinstance(parsed, dict):
        return parsed

    raise ValueError("invalid payload format")


# =========================================================
# FAST TRADE LOGIC
# =========================================================
def place_trade(signal: str, pair: str) -> dict:
    signal = normalize_signal(signal)
    pair = normalize_pair(pair)

    if signal is None or pair is None:
        return {"status": "skipped", "reason": "invalid input"}

    if pair not in PAIRS:
        return {"status": "skipped", "reason": "pair not enabled"}

    if not open_trade_slots_available():
        return {"status": "skipped", "reason": "max trades reached"}

    if get_open_trade_for_pair(pair):
        return {"status": "skipped", "reason": "trade already open"}

    try:
        px = get_current_price(pair)
        bid = px["bid"]
        ask = px["ask"]

        entry = ask if signal == "BUY" else bid

        pip = pip_size(pair)
        sl_pips = STOP_LOSS[pair]
        tp_pips = TAKE_PROFIT[pair]

        if signal == "BUY":
            units = UNITS
            sl_price = entry - (sl_pips * pip)
            tp_price = entry + (tp_pips * pip)
        else:
            units = -UNITS
            sl_price = entry + (sl_pips * pip)
            tp_price = entry - (tp_pips * pip)

        payload = {
            "order": {
                "instrument": pair,
                "units": str(units),
                "type": "MARKET",
                "positionFill": "DEFAULT",
                "timeInForce": "FOK",
                "stopLossOnFill": {
                    "price": price_format(pair, sl_price)
                },
                "takeProfitOnFill": {
                    "price": price_format(pair, tp_price)
                }
            }
        }

        log(f"FAST EXECUTION: {signal} {pair}")
        log(f"{pair} sending order: {payload}")

        response = oanda_post(f"/accounts/{OANDA_ACCOUNT_ID}/orders", payload)

        last_trade_time[pair] = time.time()

        log(f"{pair} trade placed")
        log(f"{pair} response: {response}")

        return {
            "status": "placed",
            "pair": pair,
            "signal": signal,
            "response": response,
        }

    except Exception as e:
        log(f"TRADE ERROR: {e}")
        return {"status": "error", "message": str(e)}


# =========================================================
# TRADE MANAGEMENT
# =========================================================
def manage_open_trades() -> None:
    trades = get_open_trades()
    if not trades:
        return

    pricing = get_pricing(PAIRS)

    for trade in trades:
        try:
            pair = trade["instrument"]
            trade_id = trade["id"]
            entry = float(trade["price"])
            units = float(trade["currentUnits"])

            px = pricing.get(pair)
            if not px:
                continue

            current = px["bid"] if units > 0 else px["ask"]
            pip = pip_size(pair)
            pips_profit = abs(current - entry) / pip

            log(f"{pair} pips in profit: {pips_profit}")

            if pips_profit >= BREAK_EVEN[pair] and trade_id not in managed_to_be:
                be_payload = {
                    "stopLoss": {
                        "price": price_format(pair, entry)
                    }
                }

                response = oanda_put(
                    f"/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders",
                    be_payload,
                )
                managed_to_be.add(trade_id)
                log(f"{pair} moved to breakeven: {response}")

            if pips_profit >= TRAIL_AFTER[pair]:
                if units > 0:
                    new_sl = current - (TRAIL_LOCK[pair] * pip)
                else:
                    new_sl = current + (TRAIL_LOCK[pair] * pip)

                trail_payload = {
                    "stopLoss": {
                        "price": price_format(pair, new_sl)
                    }
                }

                response = oanda_put(
                    f"/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders",
                    trail_payload,
                )
                log(f"{pair} trailing stop updated: {response}")

        except Exception as e:
            log(f"manage error: {e}")


def auto_loop() -> None:
    while True:
        try:
            manage_open_trades()
        except Exception as e:
            log(f"manage loop error: {e}")

        time.sleep(AUTO_CHECK_SECONDS)


# =========================================================
# ROUTES
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "bot": "OANDA Runner Fast Exec",
        "env": OANDA_ENV,
        "max_total_open_trades": MAX_TOTAL_OPEN_TRADES,
        "pairs": PAIRS,
        "status": "running",
        "units": UNITS,
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = parse_webhook_payload()
        signal = data.get("signal")
        pair = data.get("pair")

        result = place_trade(signal, pair)
        return jsonify(result), 200

    except Exception as e:
        log(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400


# =========================================================
# START
# =========================================================
threading.Thread(target=auto_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
