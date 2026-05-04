from flask import Flask, request, jsonify
import os, time

app = Flask(__name__)

# ===== ENV =====
PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE")

ALLOW_BUY = os.getenv("ALLOW_BUY") == "true"
ALLOW_SELL = os.getenv("ALLOW_SELL") == "true"
ALLOW_MULTIPAIR = os.getenv("ALLOW_MULTIPAIR") == "true"

MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", 2))
ONE_TRADE_PER_PAIR = os.getenv("ONE_TRADE_PER_PAIR") == "true"
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", 300))

TREND_MIN = float(os.getenv("TREND_STRENGTH_MIN", 20))
CHOP_MAX = float(os.getenv("CHOP_RANGE_PIPS", 12))

DEBUG = os.getenv("DEBUG_BLOCK_REASONS") == "true"

# ===== MEMORY =====
last_trade_time = {}
open_trades = {}

# ===== MOCK FILTERS (replace with real data later) =====
def get_trend_strength(pair):
    return 18  # simulate

def get_chop_range(pair):
    return 10  # simulate

def get_open_trades():
    return len(open_trades)

# ===== LOGGING =====
def log_block(pair, action, reason, extra=""):
    if DEBUG:
        print(f"❌ BLOCKED | {pair} | {action} | {reason} {extra}")

def log_execute(pair, action):
    print(f"✅ TRADE EXECUTED | {pair} | {action}")

# ===== ROUTE =====
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if data.get("passphrase") != PASSPHRASE:
        return jsonify({"error": "unauthorized"}), 403

    pair = data.get("pair")
    action = data.get("action")

    print(f"\n📩 SIGNAL | {pair} | {action}")

    # ===== BUY/SELL FILTER =====
    if action == "buy" and not ALLOW_BUY:
        log_block(pair, action, "buy disabled")
        return "blocked"

    if action == "sell" and not ALLOW_SELL:
        log_block(pair, action, "sell disabled")
        return "blocked"

    # ===== MAX TRADES =====
    if get_open_trades() >= MAX_OPEN_TRADES:
        log_block(pair, action, "max trades reached")
        return "blocked"

    # ===== ONE TRADE PER PAIR =====
    if ONE_TRADE_PER_PAIR and pair in open_trades:
        log_block(pair, action, "already in trade")
        return "blocked"

    # ===== COOLDOWN =====
    now = time.time()
    if pair in last_trade_time:
        if now - last_trade_time[pair] < MIN_SECONDS_BETWEEN_TRADES:
            log_block(pair, action, "cooldown active")
            return "blocked"

    # ===== TREND FILTER =====
    trend = get_trend_strength(pair)
    if trend < TREND_MIN:
        log_block(pair, action, "weak trend", f"{trend} < {TREND_MIN}")
        return "blocked"

    # ===== CHOP FILTER =====
    chop = get_chop_range(pair)
    if chop > CHOP_MAX:
        log_block(pair, action, "choppy market", f"{chop} > {CHOP_MAX}")
        return "blocked"

    # ===== EXECUTE TRADE =====
    open_trades[pair] = True
    last_trade_time[pair] = now

    log_execute(pair, action)

    return jsonify({"status": "executed"})
    
# ===== START =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
