from flask import Flask, request, jsonify
import os
import time
import threading
import requests

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.environ.get("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.environ.get("OANDA_ENV", "practice").strip().lower()

if OANDA_ENV == "live":
    BASE_URL = "https://api-fxtrade.oanda.com/v3"
else:
    BASE_URL = "https://api-fxpractice.oanda.com/v3"

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "XAU_USD"]

UNITS = int(os.environ.get("UNITS", "3000"))

STOP_LOSS = {
    "EUR_USD": 12,
    "GBP_USD": 12,
    "USD_JPY": 12,
    "XAU_USD": 200,
}

TAKE_PROFIT = {
    "EUR_USD": 18,
    "GBP_USD": 18,
    "USD_JPY": 18,
    "XAU_USD": 300,
}

BREAK_EVEN = {
    "EUR_USD": 8,
    "GBP_USD": 8,
    "USD_JPY": 8,
    "XAU_USD": 120,
}

MAX_SPREAD = {
    "EUR_USD": 0.00025,
    "GBP_USD": 0.00035,
    "USD_JPY": 0.025,
    "XAU_USD": 0.80,
}

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "900"))
AUTO_CHECK_SECONDS = int(os.environ.get("AUTO_CHECK_SECONDS", "10"))

last_trade_time = {}

# =========================
# HELPERS
# =========================
def headers() -> dict:
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json",
    }


def log(msg: str) -> None:
    print(msg, flush=True)


def normalize_pair(pair: str | None) -> str | None:
    if not pair:
        return None
    p = pair.strip().upper().replace("/", "_")
    aliases = {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "USDJPY": "USD_JPY",
        "XAUUSD": "XAU_USD",
        "EUR_USD": "EUR_USD",
        "GBP_USD": "GBP_USD",
        "USD_JPY": "USD_JPY",
        "XAU_USD": "XAU_USD",
    }
    return aliases.get(p)


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
    prices = {}
    for item in data.get("prices", []):
        instrument = item["instrument"]
        bid = float(item["bids"][0]["price"])
        ask = float(item["asks"][0]["price"])
        prices[instrument] = {"bid": bid, "ask": ask}
    return prices


def get_current_price(pair: str) -> dict:
    prices = get_pricing([pair])
    if pair not in prices:
        raise RuntimeError(f"No pricing returned for {pair}")
    return prices[pair]


def get_spread(pair: str) -> float:
    px = get_current_price(pair)
    return px["ask"] - px["bid"]


def spread_allowed(pair: str) -> tuple[bool, float]:
    spread = get_spread(pair)
    allowed = spread <= MAX_SPREAD[pair]
    return allowed, spread


def get_open_trades() -> list[dict]:
    data = oanda_get(f"/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    return data.get("trades", [])


def get_open_trade_for_pair(pair: str) -> dict | None:
    for trade in get_open_trades():
        if trade.get("instrument") == pair:
            return trade
    return None


def cooldown_active(pair: str) -> bool:
    last = last_trade_time.get(pair, 0)
    return (time.time() - last) < COOLDOWN_SECONDS


# =========================
# TRADE LOGIC
# =========================
def place_trade(signal: str, pair: str) -> None:
    signal = signal.upper().strip()

    if signal not in {"BUY", "SELL"}:
        log(f"Invalid signal: {signal}")
        return

    if pair not in PAIRS:
        log(f"Pair not allowed: {pair}")
        return

    if get_open_trade_for_pair(pair):
        log(f"{pair} already has trade - skipping")
        return

    if cooldown_active(pair):
        log(f"{pair} cooldown active - skipping")
        return

    spread_ok, spread = spread_allowed(pair)
    if not spread_ok:
        log(f"{pair} spread too high: {spread}")
        return

    px = get_current_price(pair)
    bid = px["bid"]
    ask = px["ask"]
    entry = ask if signal == "BUY" else bid

    sl_pips = STOP_LOSS[pair]
    tp_pips = TAKE_PROFIT[pair]
    pip = pip_size(pair)

    if signal == "BUY":
        units = UNITS
        sl_price = entry - (sl_pips * pip)
        tp_price = entry + (tp_pips * pip)
    else:
        units = -UNITS
        sl_price = entry + (sl_pips * pip)
        tp_price = entry - (tp_pips * pip)

    order = {
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
            },
        }
    }

    log(f"Webhook: {{'signal': '{signal}', 'pair': '{pair}'}}")
    log(f"{pair} Sending: {order}")

    try:
        response = oanda_post(f"/accounts/{OANDA_ACCOUNT_ID}/orders", order)
        log(f"{pair} Response: {response}")
        last_trade_time[pair] = time.time()
    except Exception as e:
        log(f"{pair} order failed: {e}")


def check_breakeven() -> None:
    trades = get_open_trades()
    if not trades:
        return

    prices = get_pricing(PAIRS)

    for trade in trades:
        try:
            pair = trade["instrument"]
            if pair not in BREAK_EVEN:
                continue

            trade_id = trade["id"]
            entry = float(trade["price"])
            units = float(trade["currentUnits"])
            px = prices.get(pair)

            if not px:
                continue

            current = px["bid"] if units > 0 else px["ask"]
            pip = pip_size(pair)
            pips_profit = abs(current - entry) / pip

            log(f"{pair} pips in profit: {pips_profit}")

            if pips_profit >= BREAK_EVEN[pair]:
                payload = {
                    "stopLoss": {
                        "price": price_format(pair, entry)
                    }
                }
                response = oanda_put(
                    f"/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders",
                    payload,
                )
                log(f"🔥 {pair} moved to breakeven: {response}")
        except Exception as e:
            log(f"BE error on trade: {e}")


def auto_loop() -> None:
    while True:
        try:
            check_breakeven()
        except Exception as e:
            log(f"BE loop error: {e}")
        time.sleep(AUTO_CHECK_SECONDS)


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "bot": "OANDA Runner V8",
        "env": OANDA_ENV,
        "status": "running",
        "pairs": PAIRS,
        "units": UNITS,
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        signal = str(data.get("signal", "")).upper().strip()
        pair = normalize_pair(data.get("pair"))

        if signal not in {"BUY", "SELL"} or not pair:
            return jsonify({"status": "error", "message": "invalid signal or pair"}), 400

        place_trade(signal, pair)
        return jsonify({"status": "ok", "signal": signal, "pair": pair})
    except Exception as e:
        log(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# =========================
# START
# =========================
threading.Thread(target=auto_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
