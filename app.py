import os
import json
import time
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================================================
# CONFIG
# =========================================================
OANDA_API_KEY = os.getenv("98969b4679d01a139e86d66ee8694bef-6f46ee09cb98d79db97096b393622766").strip()
OANDA_ACCOUNT_ID = os.getenv("101-001-37221732-001").strip()
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()

if OANDA_ENV == "live":
    OANDA_BASE = "https://api-fxtrade.oanda.com"
else:
    OANDA_BASE = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

# ---- main behavior ----
DEFAULT_RISK_PCT = float(os.getenv("DEFAULT_RISK_PCT", "1.5"))
MAX_RISK_PCT_PER_TRADE = float(os.getenv("MAX_RISK_PCT_PER_TRADE", "2.5"))
MAX_TOTAL_RISK_PCT = float(os.getenv("MAX_TOTAL_RISK_PCT", "6.0"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "6"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_TRADES_PER_PAIR", "1"))
PAIR_COOLDOWN_SEC = int(os.getenv("PAIR_COOLDOWN_SEC", "1800"))

# ---- management ----
ENABLE_BREAK_EVEN = os.getenv("ENABLE_BREAK_EVEN", "true").lower() == "true"
BREAK_EVEN_TRIGGER_R = float(os.getenv("BREAK_EVEN_TRIGGER_R", "1.0"))
ENABLE_TRAILING = os.getenv("ENABLE_TRAILING", "true").lower() == "true"
TRAILING_TRIGGER_R = float(os.getenv("TRAILING_TRIGGER_R", "1.5"))
TRAILING_LOCK_R = float(os.getenv("TRAILING_LOCK_R", "0.5"))

# ---- signal filters ----
REQUIRE_SESSION = os.getenv("REQUIRE_SESSION", "false").lower() == "true"
ALLOWED_SESSIONS = {
    s.strip().upper()
    for s in os.getenv("ALLOWED_SESSIONS", "LONDON,NEWYORK").split(",")
    if s.strip()
}

# ---- instruments ----
ALLOWED_INSTRUMENTS = {
    s.strip().upper()
    for s in os.getenv(
        "ALLOWED_INSTRUMENTS",
        "EUR_USD,GBP_USD,USD_JPY,USD_CHF,AUD_USD,NZD_USD,EUR_JPY,GBP_JPY"
    ).split(",")
    if s.strip()
}

# in-memory cooldowns for one Render service
last_trade_time = {}

# =========================================================
# HELPERS
# =========================================================
def log(message, data=None):
    ts = datetime.now(timezone.utc).isoformat()
    if data is None:
        print(f"[{ts}] {message}", flush=True)
    else:
        try:
            print(f"[{ts}] {message}: {json.dumps(data, default=str)}", flush=True)
        except Exception:
            print(f"[{ts}] {message}: {str(data)}", flush=True)

def now_utc():
    return datetime.now(timezone.utc)

def is_jpy_pair(instrument):
    return "JPY" in instrument.upper()

def pip_size(instrument):
    return 0.01 if is_jpy_pair(instrument) else 0.0001

def fmt_price(price, instrument):
    decimals = 3 if is_jpy_pair(instrument) else 5
    return f"{price:.{decimals}f}"

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default

def oanda_get(path):
    url = f"{OANDA_BASE}{path}"
    r = requests.get(url, headers=HEADERS, timeout=25)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return r.status_code, data

def oanda_post(path, payload):
    url = f"{OANDA_BASE}{path}"
    r = requests.post(url, headers=HEADERS, json=payload, timeout=25)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return r.status_code, data

def oanda_put(path, payload):
    url = f"{OANDA_BASE}{path}"
    r = requests.put(url, headers=HEADERS, json=payload, timeout=25)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return r.status_code, data

def get_account_summary():
    code, data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/summary")
    if code >= 300:
        log("Failed account summary", data)
        return None
    return data.get("account", {})

def get_nav():
    account = get_account_summary()
    if not account:
        return None
    return safe_float(account.get("NAV"), 0)

def get_open_trades():
    code, data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    if code >= 300:
        log("Failed open trades", data)
        return []
    return data.get("trades", [])

def get_pricing(instrument):
    code, data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/pricing?instruments={instrument}")
    if code >= 300:
        log("Failed pricing", data)
        return None
    prices = data.get("prices", [])
    if not prices:
        return None
    p = prices[0]
    return {
        "bid": safe_float(p["bids"][0]["price"]),
        "ask": safe_float(p["asks"][0]["price"]),
    }

def count_open_trades():
    return len(get_open_trades())

def count_open_trades_for_pair(instrument):
    return sum(1 for t in get_open_trades() if t.get("instrument") == instrument)

def in_cooldown(instrument):
    last = last_trade_time.get(instrument)
    if last is None:
        return False
    return (time.time() - last) < PAIR_COOLDOWN_SEC

def set_cooldown(instrument):
    last_trade_time[instrument] = time.time()

def estimate_open_risk_pct():
    # simple estimate based on count, keeps bot from overloading account
    return count_open_trades() * DEFAULT_RISK_PCT

def pips_to_price_distance(instrument, pips):
    return safe_float(pips) * pip_size(instrument)

def build_entry_sl_tp(instrument, side, pricing, sl_pips, tp_pips):
    sl_dist = pips_to_price_distance(instrument, sl_pips)
    tp_dist = pips_to_price_distance(instrument, tp_pips)

    if side == "BUY":
        entry = pricing["ask"]
        stop_loss = entry - sl_dist
        take_profit = entry + tp_dist
    else:
        entry = pricing["bid"]
        stop_loss = entry + sl_dist
        take_profit = entry - tp_dist

    return entry, stop_loss, take_profit

def units_from_risk(nav, risk_pct, entry, stop_loss, instrument):
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        return 0

    risk_amount = nav * (risk_pct / 100.0)

    # conservative sizing model for demo
    raw_units = risk_amount / stop_distance
    raw_units *= 0.01

    units = int(raw_units)
    return max(units, 0)

def session_allowed(payload):
    if not REQUIRE_SESSION:
        return True, "session not required"

    session_name = str(payload.get("session", "")).upper().strip()
    if not session_name:
        return False, "missing session"
    if session_name not in ALLOWED_SESSIONS:
        return False, f"session not allowed: {session_name}"
    return True, "ok"

def validate_payload(payload):
    required = ["instrument", "side", "stop_loss_pips", "take_profit_pips"]

    missing = [field for field in required if field not in payload]
    if missing:
        return False, f"missing fields: {missing}"

    instrument = str(payload["instrument"]).upper().strip()
    side = str(payload["side"]).upper().strip()

    if instrument not in ALLOWED_INSTRUMENTS:
        return False, f"instrument not allowed: {instrument}"

    if side not in {"BUY", "SELL"}:
        return False, "side must be BUY or SELL"

    try:
        float(payload["stop_loss_pips"])
        float(payload["take_profit_pips"])
    except Exception:
        return False, "stop_loss_pips and take_profit_pips must be numbers"

    ok, reason = session_allowed(payload)
    if not ok:
        return False, reason

    return True, "ok"

def place_market_order(instrument, side, units, stop_loss, take_profit):
    signed_units = str(units if side == "BUY" else -units)

    payload = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": signed_units,
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": fmt_price(stop_loss, instrument)
            },
            "takeProfitOnFill": {
                "price": fmt_price(take_profit, instrument)
            }
        }
    }

    code, data = oanda_post(f"/v3/accounts/{OANDA_ACCOUNT_ID}/orders", payload)
    return code, data, payload

def update_trade_stop(trade_id, instrument, new_sl):
    payload = {
        "stopLoss": {
            "timeInForce": "GTC",
            "price": fmt_price(new_sl, instrument)
        }
    }
    return oanda_put(f"/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders", payload)

def calculate_r_multiple(trade, pricing):
    current_units = safe_float(trade.get("currentUnits"))
    entry_price = safe_float(trade.get("price"))
    stop_order = trade.get("stopLossOrder")

    if not stop_order:
        return None, None, None

    old_sl = safe_float(stop_order.get("price"))
    if old_sl <= 0:
        return None, None, None

    if current_units > 0:
        current_price = pricing["bid"]
    else:
        current_price = pricing["ask"]

    risk_per_unit = abs(entry_price - old_sl)
    if risk_per_unit <= 0:
        return None, None, None

    reward_now = abs(current_price - entry_price)
    r_multiple = reward_now / risk_per_unit

    return r_multiple, entry_price, old_sl

def manage_trade_break_even_and_trailing(trade):
    try:
        instrument = trade["instrument"]
        trade_id = trade["id"]
        current_units = safe_float(trade.get("currentUnits"))
        if current_units == 0:
            return

        pricing = get_pricing(instrument)
        if not pricing:
            return

        r_multiple, entry_price, old_sl = calculate_r_multiple(trade, pricing)
        if r_multiple is None:
            return

        # Long trade
        if current_units > 0:
            if ENABLE_BREAK_EVEN and r_multiple >= BREAK_EVEN_TRIGGER_R:
                new_sl = entry_price
                if new_sl > old_sl:
                    code, data = update_trade_stop(trade_id, instrument, new_sl)
                    log(f"{instrument} long -> breakeven", {"trade_id": trade_id, "status": code, "resp": data})

            if ENABLE_TRAILING and r_multiple >= TRAILING_TRIGGER_R:
                risk_per_unit = abs(entry_price - old_sl)
                new_sl = entry_price + (risk_per_unit * TRAILING_LOCK_R)
                if new_sl > old_sl:
                    code, data = update_trade_stop(trade_id, instrument, new_sl)
                    log(f"{instrument} long -> trailing lock", {"trade_id": trade_id, "status": code, "resp": data})

        # Short trade
        else:
            if ENABLE_BREAK_EVEN and r_multiple >= BREAK_EVEN_TRIGGER_R:
                new_sl = entry_price
                if new_sl < old_sl:
                    code, data = update_trade_stop(trade_id, instrument, new_sl)
                    log(f"{instrument} short -> breakeven", {"trade_id": trade_id, "status": code, "resp": data})

            if ENABLE_TRAILING and r_multiple >= TRAILING_TRIGGER_R:
                risk_per_unit = abs(entry_price - old_sl)
                new_sl = entry_price - (risk_per_unit * TRAILING_LOCK_R)
                if new_sl < old_sl:
                    code, data = update_trade_stop(trade_id, instrument, new_sl)
                    log(f"{instrument} short -> trailing lock", {"trade_id": trade_id, "status": code, "resp": data})

    except Exception as e:
        log("manage trade error", {"error": str(e), "trade": trade})

def manage_all_trades():
    trades = get_open_trades()
    for trade in trades:
        manage_trade_break_even_and_trailing(trade)

# =========================================================
# ROUTES
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "service": "forex-bot",
        "env": OANDA_ENV,
        "allowed_instruments": sorted(list(ALLOWED_INSTRUMENTS)),
        "max_open_trades": MAX_OPEN_TRADES,
        "max_trades_per_pair": MAX_TRADES_PER_PAIR,
        "default_risk_pct": DEFAULT_RISK_PCT
    })

@app.route("/health", methods=["GET"])
def health():
    nav = get_nav()
    return jsonify({
        "ok": nav is not None,
        "env": OANDA_ENV,
        "account_id_present": bool(OANDA_ACCOUNT_ID),
        "api_key_present": bool(OANDA_API_KEY),
        "nav": nav
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log("Webhook received", payload)

        valid, msg = validate_payload(payload)
        if not valid:
            return jsonify({"ok": False, "error": msg}), 400

        instrument = str(payload["instrument"]).upper().strip()
        side = str(payload["side"]).upper().strip()
        sl_pips = safe_float(payload["stop_loss_pips"])
        tp_pips = safe_float(payload["take_profit_pips"])
        risk_pct = safe_float(payload.get("risk_pct", DEFAULT_RISK_PCT), DEFAULT_RISK_PCT)

        if risk_pct <= 0:
            return jsonify({"ok": False, "error": "risk_pct must be greater than 0"}), 400

        if risk_pct > MAX_RISK_PCT_PER_TRADE:
            risk_pct = MAX_RISK_PCT_PER_TRADE

        if count_open_trades() >= MAX_OPEN_TRADES:
            return jsonify({"ok": False, "error": "max open trades reached"}), 409

        if count_open_trades_for_pair(instrument) >= MAX_TRADES_PER_PAIR:
            return jsonify({"ok": False, "error": f"max trades reached for {instrument}"}), 409

        if in_cooldown(instrument):
            return jsonify({"ok": False, "error": f"{instrument} cooldown active"}), 409

        estimated_total_risk = estimate_open_risk_pct() + risk_pct
        if estimated_total_risk > MAX_TOTAL_RISK_PCT:
            return jsonify({"ok": False, "error": "portfolio risk cap reached"}), 409

        nav = get_nav()
        if nav is None or nav <= 0:
            return jsonify({"ok": False, "error": "could not get account NAV"}), 500

        pricing = get_pricing(instrument)
        if not pricing:
            return jsonify({"ok": False, "error": "could not get live price"}), 500

        entry, stop_loss, take_profit = build_entry_sl_tp(
            instrument=instrument,
            side=side,
            pricing=pricing,
            sl_pips=sl_pips,
            tp_pips=tp_pips
        )

        units = units_from_risk(
            nav=nav,
            risk_pct=risk_pct,
            entry=entry,
            stop_loss=stop_loss,
            instrument=instrument
        )

        if units < 1:
            return jsonify({"ok": False, "error": "computed units too small"}), 400

        code, data, sent_payload = place_market_order(
            instrument=instrument,
            side=side,
            units=units,
            stop_loss=stop_loss,
            take_profit=take_profit
        )

        log("Order response", {
            "status": code,
            "response": data,
            "sent_payload": sent_payload
        })

        if code >= 300:
            return jsonify({
                "ok": False,
                "status": code,
                "response": data
            }), 500

        set_cooldown(instrument)

        return jsonify({
            "ok": True,
            "instrument": instrument,
            "side": side,
            "risk_pct": risk_pct,
            "units": units,
            "entry_estimate": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "response": data
        }), 200

    except Exception as e:
        log("Webhook fatal error", {"error": str(e)})
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/manage", methods=["GET", "POST"])
def manage():
    try:
        manage_all_trades()
        return jsonify({"ok": True, "message": "trade management complete"}), 200
    except Exception as e:
        log("Manage fatal error", {"error": str(e)})
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
