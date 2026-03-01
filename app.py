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
    price = float(f"{float(data.get('price')):.5f}")
    atr = float(f"{float(data.get('atr')):.5f}")

    print(f"ALERT RECEIVED: {signal} {pair} Price: {price} ATR: {atr}")
    
    return {"status": "received"}, 200
