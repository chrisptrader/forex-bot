from flask import Flask, request

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    signal = data.get("signal")
    pair = data.get("pair")
    price = data.get("price")
    atr = data.get("atr")

    print(f"ALERT RECEIVED: {signal} {pair} Price: {price} ATR: {atr}")

    return {"status": "received"}, 200
    if __name__ == "__main__":
        app.run(host="0.0.0.0", port=10000)
