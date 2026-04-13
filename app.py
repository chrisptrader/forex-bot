import os
import json
import math
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# --------------------------------------------------
# LOGGING
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("oanda_runner_v8")

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
OANDA_API_KEY = os.environ.get("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.environ.get("OANDA_ENV", "practice").strip().lower()

if OANDA_ENV == "live":
    OANDA_BASE_URL = "https://api-fxtrade.oanda.com"
else:
    OANDA_BASE_URL = "https://api-fxpractice.oanda.com"

REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))

# Risk / trade controls
DEFAULT_RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.25"))
DEFAULT_UNITS = int(os.environ.get("DEFAULT_UNITS", "100"))
USE_RISK_SIZING = os.environ.get("USE_RISK_SIZING", "true").lower() == "true"

STOP_LOSS_PIPS = float(os.environ.get("STOP_LOSS_PIPS", "12"))
BREAK_EVEN_TRIGGER_PIPS = float(os.environ.get("BREAK_EVEN_TRIGGER_PIPS", "10"))
BREAK_EVEN_PLUS_PIPS = float(os.environ.get("BREAK_EVEN_PLUS_PIPS", "0"))
TRAILING_START_PIPS = float(os.environ.get("TRAILING_START_PIPS", "18"))
TRAILING_DISTANCE_PIPS = float(os.environ.get("TRAILING_DISTANCE_PIPS", "10"))

RUNNER_MODE = os.environ.get("RUNNER_MODE", "true").lower() == "true"
USE_BACKUP_TP = os.environ.get("USE_BACKUP_TP", "true").lower() == "true"
BACKUP_TP_PIPS = float(os.environ.get("BACKUP_TP_PIPS", "150"))

MAX_OPEN_TRADES_PER_PAIR = int(os.environ.get("MAX_OPEN_TRADES_PER_PAIR", "2"))
MAX_OPEN_TRADES_TOTAL = int(os.environ.get("MAX_OPEN_TRADES_TOTAL", "2"))

MAX_SPREAD_PIPS = float(os.environ.get("MAX_SPREAD_PIPS", "1.8"))
ALLOW_WEEKENDS = os.environ.get("ALLOW_WEEKENDS", "false").lower() == "true"

SESSION_FILTER_ENABLED = os.environ.get("SESSION_FILTER_ENABLED", "true").lower() == "true"
SESSION_START_UTC = int(os.environ.get("SESSION_START_UTC", "7"))
SESSION_END_UTC = int(os.environ.get("SESSION_END_UTC", "17"))

# News filter
NEWS_FILTER_ENABLED = os.environ.get("NEWS_FILTER_ENABLED", "true").lower() == "true"
NEWS_BLOCK_BEFORE_MINUTES = int(os.environ.get("NEWS_BLOCK_BEFORE_MINUTES", "45"))
NEWS_BLOCK_AFTER_MINUTES = int(os.environ.get("NEWS_BLOCK_AFTER_MINUTES", "45"))
NEWS_WINDOWS_JSON = os.environ.get("NEWS_WINDOWS_JSON", "").strip()
NEWS_FEED_URL = os.environ.get("NEWS_FEED_URL", "").strip()

# Trend filter
TREND_FILTER_ENABLED = os.environ.get("TREND_FILTER_ENABLED", "true").lower() == "true"

# Scale-in settings
SCALE_IN_ENABLED = os.environ.get("SCALE_IN_ENABLED", "true").lower() == "true"
SCALE_IN_TRIGGER_PIPS = float(os.environ.get("SCALE_IN_TRIGGER_PIPS", "15"))
SCALE_IN_RISK_MULTIPLIER = float(os.environ.get("SCALE_IN_RISK_MULTIPLIER", "0.5"))
MAX_SCALE_INS_PER_PAIR = int(os.environ.get("MAX_SCALE_INS_PER_PAIR", "1"))

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

# --------------------------------------------------
# BASIC GUARDS
# --------------------------------------------------
def validate_env() -> None:
    missing = []
    if not OANDA_API_KEY:
        missing.append("OANDA_API_KEY")
    if not OANDA_ACCOUNT_ID:
        missing.append("OANDA_ACCOUNT_ID")
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

# --------------------------------------------------
# OANDA HTTP HELPERS
# --------------------------------------------------
def oanda_request(method: str, endpoint: str, payload: Optional[dict] = None) -> dict:
    url = f"{OANDA_BASE_URL}{endpoint}"
    try:
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=HEADERS,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )
        if response.status_code >= 400:
            logger.error("OANDA error %s | %s", response.status_code, response.text)
            response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.exception("OANDA request failed: %s %s", method, endpoint)
        raise RuntimeError(f"OANDA request failed: {e}") from e

def oanda_get(endpoint: str) -> dict:
    return oanda_request("GET", endpoint)

def oanda_post(endpoint: str, payload: dict) -> dict:
    return oanda_request("POST", endpoint, payload)

def oanda_put(endpoint: str, payload: dict) -> dict:
    return oanda_request("PUT", endpoint, payload)

# --------------------------------------------------
# TIME / PAIR HELPERS
# --------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def normalize_instrument(raw: str) -> str:
    s = raw.strip().upper().replace("/", "_")
    if "_" not in s and len(s) == 6:
        s = f"{s[:3]}_{s[3:]}"
    return s

def pair_currencies(instrument: str) -> Tuple[str, str]:
    parts = instrument.split("_")
    if len(parts) != 2:
        raise ValueError(f"Invalid instrument format: {instrument}")
    return parts[0], parts[1]

def pip_size(instrument: str) -> float:
    _, quote = pair_currencies(instrument)
    return 0.01 if quote == "JPY" else 0.0001

def price_precision(instrument: str) -> int:
    _, quote = pair_currencies(instrument)
    return 3 if quote == "JPY" else 5

def format_price(instrument: str, price: float) -> str:
    return f"{price:.{price_precision(instrument)}f}"

# --------------------------------------------------
# ACCOUNT / PRICING
# --------------------------------------------------
def get_account_summary() -> dict:
    data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/summary")
    return data["account"]

def get_account_balance() -> float:
    return float(get_account_summary()["balance"])

def get_account_currency() -> str:
    account = get_account_summary()
    return account.get("currency", "USD")

def get_pricing(instruments: List[str]) -> dict:
    joined = ",".join(instruments)
    data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/pricing?instruments={joined}")
    prices = data.get("prices", [])
    out = {}
    for p in prices:
        instrument = p["instrument"]
        bid = float(p["bids"][0]["price"])
        ask = float(p["asks"][0]["price"])
        out[instrument] = {
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2.0
        }
    return out

def get_current_price(instrument: str) -> Dict[str, float]:
    prices = get_pricing([instrument])
    if instrument not in prices:
        raise RuntimeError(f"No pricing returned for {instrument}")
    return prices[instrument]

def get_spread_pips(instrument: str) -> float:
    px = get_current_price(instrument)
    return (px["ask"] - px["bid"]) / pip_size(instrument)

# --------------------------------------------------
# OPEN TRADES
# --------------------------------------------------
def get_open_trades() -> List[dict]:
    data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    return data.get("trades", [])

def get_open_trades_for_instrument(instrument: str) -> List[dict]:
    return [t for t in get_open_trades() if t.get("instrument") == instrument]

# --------------------------------------------------
# CONVERSION FOR RISK SIZING
# --------------------------------------------------
def get_conversion_rate(from_ccy: str, to_ccy: str) -> float:
    if from_ccy == to_ccy:
        return 1.0

    direct = f"{from_ccy}_{to_ccy}"
    inverse = f"{to_ccy}_{from_ccy}"

    try:
        px = get_current_price(direct)
        return px["mid"]
    except Exception:
        pass

    try:
        px = get_current_price(inverse)
        if px["mid"] == 0:
            raise RuntimeError(f"Zero price for conversion pair {inverse}")
        return 1.0 / px["mid"]
    except Exception as e:
        raise RuntimeError(f"Unable to convert {from_ccy} to {to_ccy}: {e}") from e

def pip_value_per_unit_in_account_ccy(instrument: str, account_ccy: str) -> float:
    _, quote = pair_currencies(instrument)
    pip = pip_size(instrument)
    quote_to_account = get_conversion_rate(quote, account_ccy)
    return pip * quote_to_account

def estimate_units_from_risk(
    instrument: str,
    balance: float,
    risk_percent: float,
    stop_loss_pips: float,
    account_ccy: str
) -> int:
    if balance <= 0 or risk_percent <= 0 or stop_loss_pips <= 0:
        return 0

    risk_amount = balance * (risk_percent / 100.0)
    pip_value_per_unit = pip_value_per_unit_in_account_ccy(instrument, account_ccy)

    if pip_value_per_unit <= 0:
        return 0

    units = risk_amount / (stop_loss_pips * pip_value_per_unit)
    return max(1, int(math.floor(units)))

# --------------------------------------------------
# SESSION / SPREAD FILTERS
# --------------------------------------------------
def session_allowed() -> bool:
    if not SESSION_FILTER_ENABLED:
        return True

    current = now_utc()

    if not ALLOW_WEEKENDS and current.weekday() >= 5:
        return False

    hour = current.hour
    if SESSION_START_UTC <= SESSION_END_UTC:
        return SESSION_START_UTC <= hour < SESSION_END_UTC

    return hour >= SESSION_START_UTC or hour < SESSION_END_UTC

def spread_allowed(instrument: str) -> Tuple[bool, float]:
    spread = get_spread_pips(instrument)
    return spread <= MAX_SPREAD_PIPS, spread

# --------------------------------------------------
# NEWS FILTER
# --------------------------------------------------
def parse_iso_utc(ts: str) -> datetime:
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def load_manual_news_events() -> List[dict]:
    if not NEWS_WINDOWS_JSON:
        return []

    try:
        raw = json.loads(NEWS_WINDOWS_JSON)
        events = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if "currency" not in item or "time" not in item:
                continue
            events.append({
                "currency": str(item["currency"]).upper(),
                "title": str(item.get("title", "Manual News Window")),
                "impact": str(item.get("impact", "high")).lower(),
                "time": parse_iso_utc(str(item["time"]))
            })
        return events
    except Exception as e:
        logger.error("Failed to parse NEWS_WINDOWS_JSON: %s", e)
        return []

def load_feed_news_events() -> List[dict]:
    if not NEWS_FEED_URL:
        return []

    try:
        response = requests.get(NEWS_FEED_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        raw = response.json()

        events = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if "currency" not in item or "time" not in item:
                continue
            events.append({
                "currency": str(item["currency"]).upper(),
                "title": str(item.get("title", "Feed News Window")),
                "impact": str(item.get("impact", "high")).lower(),
                "time": parse_iso_utc(str(item["time"]))
            })
        return events
    except Exception as e:
        logger.error("Failed to load NEWS_FEED_URL: %s", e)
        return []

def get_news_events() -> List[dict]:
    events = []
    events.extend(load_manual_news_events())
    events.extend(load_feed_news_events())
    return events

def is_news_blocked(instrument: str) -> Tuple[bool, Optional[dict]]:
    if not NEWS_FILTER_ENABLED:
        return False, None

    base, quote = pair_currencies(instrument)
    relevant = {base, quote}
    current = now_utc()

    for event in get_news_events():
        if event.get("impact", "high").lower() != "high":
            continue
        if event["currency"] not in relevant:
            continue

        event_time = event["time"]
        start = event_time - timedelta(minutes=NEWS_BLOCK_BEFORE_MINUTES)
        end = event_time + timedelta(minutes=NEWS_BLOCK_AFTER_MINUTES)

        if start <= current <= end:
            return True, event

    return False, None

# --------------------------------------------------
# TREND FILTER
# --------------------------------------------------
def trend_filter_passed(action: str, payload: dict) -> Tuple[bool, str]:
    if not TREND_FILTER_ENABLED:
        return True, "trend filter disabled"

    trend_ok = payload.get("trend_ok", None)
    trend_direction = str(payload.get("trend_direction", "")).strip().lower()

    if trend_ok is False:
        return False, "trend_ok=false"

    if trend_direction and trend_direction != action:
        return False, f"trend direction mismatch ({trend_direction})"

    ema_fast = payload.get("ema_fast")
    ema_slow = payload.get("ema_slow")

    try:
        if ema_fast is not None and ema_slow is not None:
            ema_fast = float(ema_fast)
            ema_slow = float(ema_slow)

            if action == "buy" and ema_fast <= ema_slow:
                return False, "ema_fast <= ema_slow for buy"

            if action == "sell" and ema_fast >= ema_slow:
                return False, "ema_fast >= ema_slow for sell"
    except Exception:
        return False, "invalid ema values"

    return True, "trend filter passed"

# --------------------------------------------------
# ORDER BUILDERS
# --------------------------------------------------
def build_market_order_payload(
    instrument: str,
    side: str,
    units: int,
    sl_pips: float,
    runner_mode: bool,
    use_backup_tp: bool,
    backup_tp_pips: float
) -> dict:
    px = get_current_price(instrument)
    pip = pip_size(instrument)

    if side == "buy":
        entry = px["ask"]
        stop_loss = entry - (sl_pips * pip)
        order = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(abs(units)),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": format_price(instrument, stop_loss)
            }
        }
        if runner_mode and use_backup_tp:
            take_profit = entry + (backup_tp_pips * pip)
            order["takeProfitOnFill"] = {
                "price": format_price(instrument, take_profit)
            }

    elif side == "sell":
        entry = px["bid"]
        stop_loss = entry + (sl_pips * pip)
        order = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(-abs(units)),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": format_price(instrument, stop_loss)
            }
        }
        if runner_mode and use_backup_tp:
            take_profit = entry - (backup_tp_pips * pip)
            order["takeProfitOnFill"] = {
                "price": format_price(instrument, take_profit)
            }
    else:
        raise ValueError("side must be 'buy' or 'sell'")

    return {"order": order}

def update_trade_stop_loss(trade_id: str, instrument: str, new_sl_price: float) -> dict:
    payload = {
        "stopLoss": {
            "timeInForce": "GTC",
            "price": format_price(instrument, new_sl_price)
        }
    }
    return oanda_put(f"/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders", payload)

# --------------------------------------------------
# TRADE STATE HELPERS
# --------------------------------------------------
def get_trade_unrealized_pips(trade: dict) -> Tuple[float, float]:
    instrument = trade["instrument"]
    px = get_current_price(instrument)
    pip = pip_size(instrument)

    entry = float(trade["price"])
    current_units = float(trade["currentUnits"])

    if current_units > 0:
        current_price = px["bid"]
        pips_profit = (current_price - entry) / pip
    else:
        current_price = px["ask"]
        pips_profit = (entry - current_price) / pip

    return pips_profit, current_price

def get_existing_stop_loss(trade: dict) -> Optional[float]:
    slo = trade.get("stopLossOrder")
    if slo and slo.get("price") is not None:
        return float(slo["price"])
    return None

def get_trade_side(trade: dict) -> str:
    current_units = float(trade["currentUnits"])
    return "buy" if current_units > 0 else "sell"

def count_profitable_trades_same_direction(instrument: str, side: str, min_pips: float) -> int:
    count = 0
    for trade in get_open_trades_for_instrument(instrument):
        if get_trade_side(trade) != side:
            continue
        try:
            pips_profit, _ = get_trade_unrealized_pips(trade)
            if pips_profit >= min_pips:
                count += 1
        except Exception:
            continue
    return count

def count_same_direction_trades(instrument: str, side: str) -> int:
    count = 0
    for trade in get_open_trades_for_instrument(instrument):
        if get_trade_side(trade) == side:
            count += 1
    return count

def is_scale_in_allowed(instrument: str, side: str) -> Tuple[bool, str]:
    if not SCALE_IN_ENABLED:
        return False, "scale-in disabled"

    same_direction_count = count_same_direction_trades(instrument, side)

    if same_direction_count == 0:
        return False, "no existing trade to scale into"

    if same_direction_count > MAX_SCALE_INS_PER_PAIR:
        return False, "max scale-ins reached"

    profitable_count = count_profitable_trades_same_direction(
        instrument=instrument,
        side=side,
        min_pips=SCALE_IN_TRIGGER_PIPS
    )

    if profitable_count < 1:
        return False, "existing trade not yet profitable enough"

    return True, "scale-in allowed"

# --------------------------------------------------
# ORDER PLACEMENT
# --------------------------------------------------
def place_trade(
    instrument: str,
    side: str,
    payload_data: Optional[dict] = None,
    forced_units: Optional[int] = None,
    risk_percent: Optional[float] = None
) -> dict:
    instrument = normalize_instrument(instrument)
    payload_data = payload_data or {}

    trend_ok, trend_reason = trend_filter_passed(side, payload_data)
    if not trend_ok:
        return {"status": "blocked", "reason": trend_reason}

    if not session_allowed():
        return {"status": "blocked", "reason": "outside allowed session"}

    news_blocked, news_event = is_news_blocked(instrument)
    if news_blocked:
        return {
            "status": "blocked",
            "reason": "news filter active",
            "event": {
                "currency": news_event["currency"],
                "title": news_event["title"],
                "time": news_event["time"].isoformat()
            }
        }

    spread_ok, spread = spread_allowed(instrument)
    if not spread_ok:
        return {
            "status": "blocked",
            "reason": "spread too wide",
            "spread_pips": round(spread, 2),
            "max_spread_pips": MAX_SPREAD_PIPS
        }

    open_trades = get_open_trades()
    per_pair = [t for t in open_trades if t.get("instrument") == instrument]

    if len(open_trades) >= MAX_OPEN_TRADES_TOTAL:
        return {"status": "blocked", "reason": "max total open trades reached"}

    if len(per_pair) >= MAX_OPEN_TRADES_PER_PAIR:
        return {"status": "blocked", "reason": "max open trades reached for pair"}

    same_direction_count = count_same_direction_trades(instrument, side)
    is_scale_in = same_direction_count >= 1

    if is_scale_in:
        scale_allowed, scale_reason = is_scale_in_allowed(instrument, side)
        if not scale_allowed:
            return {"status": "blocked", "reason": scale_reason}

    if forced_units is not None and forced_units > 0:
        units = int(forced_units)
        sizing_mode = "forced_units"
    elif USE_RISK_SIZING:
        balance = get_account_balance()
        acct_ccy = get_account_currency()
        rpct = DEFAULT_RISK_PERCENT if risk_percent is None else float(risk_percent)

        if is_scale_in:
            rpct = rpct * SCALE_IN_RISK_MULTIPLIER

        units = estimate_units_from_risk(
            instrument=instrument,
            balance=balance,
            risk_percent=rpct,
            stop_loss_pips=STOP_LOSS_PIPS,
            account_ccy=acct_ccy
        )
        sizing_mode = "risk_percent_scale_in" if is_scale_in else "risk_percent"
    else:
        units = DEFAULT_UNITS
        if is_scale_in:
            units = max(1, int(math.floor(units * SCALE_IN_RISK_MULTIPLIER)))
        sizing_mode = "default_units"

    if units <= 0:
        return {"status": "blocked", "reason": "calculated units <= 0"}

    order_payload = build_market_order_payload(
        instrument=instrument,
        side=side,
        units=units,
        sl_pips=STOP_LOSS_PIPS,
        runner_mode=RUNNER_MODE,
        use_backup_tp=USE_BACKUP_TP,
        backup_tp_pips=BACKUP_TP_PIPS
    )

    response = oanda_post(f"/v3/accounts/{OANDA_ACCOUNT_ID}/orders", order_payload)

    return {
        "status": "ok",
        "instrument": instrument,
        "side": side,
        "units": units,
        "spread_pips": round(spread, 2),
        "trend_reason": trend_reason,
        "sizing_mode": sizing_mode,
        "is_scale_in": is_scale_in,
        "response": response
    }

# --------------------------------------------------
# TRADE MANAGEMENT
# --------------------------------------------------
def manage_open_trades() -> dict:
    trades = get_open_trades()
    results = []

    for trade in trades:
        trade_id = trade["id"]
        instrument = trade["instrument"]
        entry = float(trade["price"])
        current_units = float(trade["currentUnits"])
        pip = pip_size(instrument)
        existing_sl = get_existing_stop_loss(trade)

        try:
            pips_profit, current_price = get_trade_unrealized_pips(trade)
            action_taken = "none"

            if current_units > 0:
                if pips_profit >= BREAK_EVEN_TRIGGER_PIPS:
                    be_price = entry + (BREAK_EVEN_PLUS_PIPS * pip)
                    if existing_sl is None or existing_sl < be_price:
                        update_trade_stop_loss(trade_id, instrument, be_price)
                        existing_sl = be_price
                        action_taken = f"break_even_to_{format_price(instrument, be_price)}"

                if RUNNER_MODE and pips_profit >= TRAILING_START_PIPS:
                    new_sl = current_price - (TRAILING_DISTANCE_PIPS * pip)
                    if existing_sl is None or new_sl > existing_sl:
                        update_trade_stop_loss(trade_id, instrument, new_sl)
                        existing_sl = new_sl
                        action_taken = f"trail_to_{format_price(instrument, new_sl)}"

            elif current_units < 0:
                if pips_profit >= BREAK_EVEN_TRIGGER_PIPS:
                    be_price = entry - (BREAK_EVEN_PLUS_PIPS * pip)
                    if existing_sl is None or existing_sl > be_price:
                        update_trade_stop_loss(trade_id, instrument, be_price)
                        existing_sl = be_price
                        action_taken = f"break_even_to_{format_price(instrument, be_price)}"

                if RUNNER_MODE and pips_profit >= TRAILING_START_PIPS:
                    new_sl = current_price + (TRAILING_DISTANCE_PIPS * pip)
                    if existing_sl is None or new_sl < existing_sl:
                        update_trade_stop_loss(trade_id, instrument, new_sl)
                        existing_sl = new_sl
                        action_taken = f"trail_to_{format_price(instrument, new_sl)}"

            results.append({
                "trade_id": trade_id,
                "instrument": instrument,
                "pips_profit": round(pips_profit, 2),
                "existing_sl": None if existing_sl is None else format_price(instrument, existing_sl),
                "action": action_taken
            })

        except Exception as e:
            logger.exception("Error managing trade %s", trade_id)
            results.append({
                "trade_id": trade_id,
                "instrument": instrument,
                "error": str(e)
            })

    return {
        "status": "ok",
        "managed_count": len(trades),
        "results": results
    }

# --------------------------------------------------
# ROUTES
# --------------------------------------------------
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "bot": "OANDA Runner V8",
        "env": OANDA_ENV
    })

@app.route("/health", methods=["GET"])
def health():
    try:
        validate_env()
        account = get_account_summary()
        return jsonify({
            "status": "ok",
            "env": OANDA_ENV,
            "account_id": OANDA_ACCOUNT_ID,
            "balance": account.get("balance"),
            "currency": account.get("currency"),
            "session_allowed": session_allowed(),
            "news_filter_enabled": NEWS_FILTER_ENABLED,
            "trend_filter_enabled": TREND_FILTER_ENABLED,
            "scale_in_enabled": SCALE_IN_ENABLED
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/manage", methods=["GET", "POST"])
def manage_route():
    try:
        validate_env()
        result = manage_open_trades()
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        validate_env()

        data = request.get_json(force=True, silent=False)
        if not data:
            return jsonify({"status": "error", "error": "No JSON payload received"}), 400

        logger.info("Webhook received: %s", data)

        action = str(data.get("action", "")).strip().lower()
        instrument = normalize_instrument(str(data.get("pair", "EUR_USD")))
        units = data.get("units")
        risk_percent = data.get("risk_percent")

        if units is not None:
            try:
                units = int(units)
            except Exception:
                return jsonify({"status": "error", "error": "units must be an integer"}), 400

        if risk_percent is not None:
            try:
                risk_percent = float(risk_percent)
            except Exception:
                return jsonify({"status": "error", "error": "risk_percent must be numeric"}), 400

        if action in ["buy", "long", "core_buy"]:
            result = place_trade(
                instrument=instrument,
                side="buy",
                payload_data=data,
                forced_units=units,
                risk_percent=risk_percent
            )
            return jsonify(result)

        if action in ["sell", "short", "core_sell"]:
            result = place_trade(
                instrument=instrument,
                side="sell",
                payload_data=data,
                forced_units=units,
                risk_percent=risk_percent
            )
            return jsonify(result)

        if action == "manage":
            result = manage_open_trades()
            return jsonify(result)

        if action == "status":
            open_trades = get_open_trades()
            return jsonify({
                "status": "ok",
                "open_trades_total": len(open_trades),
                "open_trades": open_trades
            })

        return jsonify({
            "status": "error",
            "error": f"Unknown action: {action}"
        }), 400

    except Exception as e:
        logger.exception("Webhook error")
        return jsonify({"status": "error", "error": str(e)}), 500

# --------------------------------------------------
# MAIN
# --------------------------------------------------
if __name__ == "__main__":
    validate_env()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

