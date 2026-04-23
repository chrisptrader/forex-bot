import os
import time
import threading
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "1234").strip()

BASE_URL = "https://api-fxpractice.oanda.com/v3" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com/v3"

PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "EUR_USD,GBP_USD,USD_JPY").split(",") if p.strip()]

# --- Sizing ---
USE_RISK_PERCENT = os.getenv("USE_RISK_PERCENT", "false").lower() == "true"
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1"))
FIXED_UNITS = int(os.getenv("FIXED_UNITS", "10000"))
FALLBACK_UNITS = int(os.getenv("FALLBACK_UNITS", "10000"))
MIN_UNITS = int(os.getenv("MIN_UNITS", "1000"))
MAX_UNITS = int(os.getenv("MAX_UNITS", "10000"))

# --- Trade control ---
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "1"))
MAX_TOTAL_OPEN_TRADES = int(os.getenv("MAX_TOTAL_OPEN_TRADES", "2"))
MAX_TRADES_PER_PAIR = int(os.getenv("MAX_TRADES_PER_PAIR", "1"))
MIN_SECONDS_BETWEEN_TRADES = int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "900"))
ALLOW_MULTIPAIR = os.getenv("ALLOW_MULTIPAIR", "true").lower() == "true"

# --- Risk management ---
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "10"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "12"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3"))
ENABLE_DAILY_LOSS_LIMIT = os.getenv("ENABLE_DAILY_LOSS_LIMIT", "true").lower() == "true"

# --- Filters ---
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "2.5"))
MIN_CANDLE_RANGE_PIPS = float(os.getenv("MIN_CANDLE_RANGE_PIPS", "2"))
TREND_STRENGTH_MIN = float(os.getenv("TREND_STRENGTH_MIN", "15"))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "5"))
CONFIRMATION_CANDLES = int(os.getenv("CONFIRMATION_CANDLES", "2"))
BUY_PULLBACK_PIPS = float(os.getenv("BUY_PULLBACK_PIPS", "1"))
SELL_PULLBACK_PIPS = float(os.getenv("SELL_PULLBACK_PIPS", "1"))
ALLOW_BUY = os.getenv("ALLOW_BUY", "true").lower() == "true"
ALLOW_SELL = os.getenv("ALLOW_SELL", "true").lower() == "true"

# --- Management ---
USE_BREAK_EVEN = os.getenv("USE_BREAK_EVEN", "true").lower() == "true"
BREAK_EVEN_TRIGGER_PIPS = float(os.getenv("BREAK_EVEN_TRIGGER_PIPS", "4"))
BREAK_EVEN_PLUS_PIPS = float(os.getenv("BREAK_E
