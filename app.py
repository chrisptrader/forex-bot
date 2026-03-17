
from flask import Flask, request, jsonify
import os

app = Flask(__name__)

# ✅ Home route (REQUIRED for Render health check)
@app.route("/")
def home():
    return "Bot is running 🚀"


# ✅ Webhook route (TradingView hits this)
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    print("Raw webhook JSON:", data)

    if not data:
        return jsonify({"error": "No data received"}), 400

    signal = data.get("signal")
    pair = data.get("pair")

    print("Parsed signal:", signal)
    print("Parsed pair:", pair)

    # ✅ Convert TradingView format to OANDA format
    if pair == "EURUSD":
        pair = "EUR_USD"

    # 🔥 PLACEHOLDER (we’ll add real trade execution next)
    print(f"🔥 SIGNAL RECEIVED: {signal} on {pair}")

    return jsonify({
        "status": "success",
        "signal": signal,
        "pair": pair
    })


# ✅ REQUIRED for Render (VERY IMPORTANT)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
