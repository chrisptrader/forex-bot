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
    atr_value = data.get("atr")

if atr_value is None:
    atr = 0.0005
else:
    atr = float(atr_value
    print(f"ALERT RECEIVED: {signal} {pair} Price: {price} ATR: {atr}")
    
    return {"status": "received"}, 200
