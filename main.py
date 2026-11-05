import warnings

warnings.filterwarnings('ignore')

import os
import asyncio
import sqlite3
import pandas as pd
import requests
import ccxt
import yfinance as yf
import MetaTrader5 as mt5
from twelvedata import TDClient
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

# بارگذاری متغیرهای محیطی از فایل .env
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_NAME = os.getenv("DB_NAME", "trading_bot_journal.db")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", 10000))
RISK_PERCENTAGE = float(os.getenv("RISK_PERCENTAGE", 1.0))

ACTIVE_CHAT_ID = None
TELEGRAM_APP_INSTANCE = None

# راه‌اندازی کلاینت رسمی Twelve Data
td_client = TDClient(apikey=TWELVE_DATA_API_KEY) if TWELVE_DATA_API_KEY else None

# راه‌اندازی صرافی کوکوین از طریق ccxt
exchange = ccxt.kucoin({
    'enableRateLimit': True,
})


def init_db():
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                           CREATE TABLE IF NOT EXISTS signals
                           (
                               id
                               INTEGER
                               PRIMARY
                               KEY
                               AUTOINCREMENT,
                               timestamp
                               DATETIME
                               DEFAULT
                               CURRENT_TIMESTAMP,
                               symbol
                               TEXT,
                               signal_type
                               TEXT,
                               entry_price
                               REAL,
                               sl
                               REAL,
                               tp1
                               REAL,
                               tp2
                               REAL,
                               status
                               TEXT
                               DEFAULT
                               'ACTIVE'
                           )
                           ''')
            cursor.execute('''
                           CREATE TABLE IF NOT EXISTS user_logs
                           (
                               id
                               INTEGER
                               PRIMARY
                               KEY
                               AUTOINCREMENT,
                               timestamp
                               DATETIME
                               DEFAULT
                               CURRENT_TIMESTAMP,
                               user_id
                               INTEGER,
                               username
                               TEXT,
                               first_name
                               TEXT,
                               action
                               TEXT
                           )
                           ''')
            conn.commit()
    except Exception as e:
        print(f"Database Init Error: {e}")


init_db()


def log_signal(symbol, signal_type, entry, sl, tp1, tp2):
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                           INSERT INTO signals (symbol, signal_type, entry_price, sl, tp1, tp2)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ''', (symbol, signal_type, entry, sl, tp1, tp2))
            conn.commit()
    except Exception as e:
        print(f"Database Error: {e}")


def log_user_action(user, action):
    if not user:
        return
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                           INSERT INTO user_logs (user_id, username, first_name, action)
                           VALUES (?, ?, ?, ?)
                           ''', (user.id, user.username, user.first_name, action))
            conn.commit()
            print(f"📝 User Log -> ID: {user.id} | Username: @{user.username} | Action: {action}")
    except Exception as e:
        print(f"User Log Error: {e}")


def calculate_lot_size(symbol_name, entry_price, sl_price, balance, risk_pct):
    try:
        risk_amount = balance * (risk_pct / 100.0)
        price_risk_distance = abs(entry_price - sl_price)

        if price_risk_distance == 0:
            return 0.01

        if "JPY" in symbol_name:
            pip_value_factor = 100
        elif "Gold" in symbol_name or "Silver" in symbol_name:
            pip_value_factor = 1
        elif any(k in symbol_name for k in ["Bitcoin", "Ethereum", "Solana", "Ripple", "S&P", "Nasdaq"]):
            pip_value_factor = 1
        else:
            pip_value_factor = 10000

        lot_size = risk_amount / (price_risk_distance * pip_value_factor)
        lot_size = round(max(0.01, min(lot_size, 50.0)), 2)
        return lot_size
    except Exception as e:
        print(f"Lot Size Calculation Error: {e}")
        return 0.01


def get_metatrader_data(mt5_symbol, count=200):
    try:
        if not mt5.initialize():
            return pd.DataFrame(), 0.0

        rates = mt5.copy_rates_from_pos(mt5_symbol, mt5.TIMEFRAME_M15, 0, count)
        symbol_info = mt5.symbol_info(mt5_symbol)
        spread_value = 0.0002
        if symbol_info is not None:
            spread_value = symbol_info.spread * symbol_info.point

        mt5.shutdown()

        if rates is not None and len(rates) > 0:
            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
            df = df.rename(columns={
                'open': 'Open', 'high': 'High', 'low': 'Low',
                'close': 'Close', 'tick_volume': 'Volume'
            })
            return df[['Open', 'High', 'Low', 'Close', 'Volume']], spread_value
    except Exception as e:
        print(f"MetaTrader Fetch Error: {e}")

    return pd.DataFrame(), 0.0002


def get_market_data(symbol, name):
    df_15m = pd.DataFrame()
    spread = 0.0002

    mt5_mapping = {
        "EUR/USD": "EURUSD", "GBP/USD": "GBPUSD", "USD/JPY": "USDJPY",
        "USD/CHF": "USDCHF", "AUD/USD": "AUDUSD", "USD/CAD": "USDCAD",
        "NZD/USD": "NZDUSD", "EUR/GBP": "EURGBP", "GBP/JPY": "GBPJPY",
        "AUD/JPY": "AUDJPY", "Gold (طلا)": "XAUUSD", "Silver (نقره)": "XAGUSD",
        "Crude Oil (نفت برنت)": "BRENT", "Bitcoin": "BTCUSD", "Ethereum": "ETHUSD",
        "Solana": "SOLUSD", "Ripple": "XRPUSD", "S&P 500": "US500", "Nasdaq": "NAS100"
    }

    mt5_sym = mt5_mapping.get(name)
    if mt5_sym:
        df_15m, spread = get_metatrader_data(mt5_sym, count=150)
        if not df_15m.empty and len(df_15m) >= 20:
            return df_15m, spread

    if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
        try:
            ccxt_symbols = {
                "Bitcoin": "BTC/USDT",
                "Ethereum": "ETH/USDT",
                "Solana": "SOL/USDT",
                "Ripple": "XRP/USDT"
            }
            ccxt_symbol = ccxt_symbols.get(name, "BTC/USDT")
            ohlcv = exchange.fetch_ohlcv(ccxt_symbol, timeframe='15m', limit=150)
            if ohlcv:
                df_15m = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df_15m['time'] = pd.to_datetime(df_15m['timestamp'], unit='ms')
                df_15m.set_index('time', inplace=True)
                df_15m = df_15m[['Open', 'High', 'Low', 'Close', 'Volume']]
                spread = 0.50 if name in ["Bitcoin", "Ethereum"] else 0.05
                return df_15m, spread
        except Exception as e:
            print(f"❌ CCXT Error for {name}: {e}")

    if td_client:
        try:
            td_mapping = {
                "EUR/USD": "EUR/USD", "GBP/USD": "GBP/USD", "USD/JPY": "USD/JPY",
                "USD/CHF": "USD/CHF", "AUD/USD": "AUD/USD", "USD/CAD": "USD/CAD",
                "NZD/USD": "NZD/USD", "EUR/GBP": "EUR/GBP", "GBP/JPY": "GBP/JPY",
                "AUD/JPY": "AUD/JPY", "Gold (طلا)": "XAU/USD", "Silver (نقره)": "XAG/USD",
                "Crude Oil (نفت برنت)": "BRENT", "S&P 500": "SPX", "Nasdaq": "IXIC"
            }
            td_sym = td_mapping.get(name)
            if td_sym:
                ts = td_client.time_series(symbol=td_sym, interval="15min", outputsize=150)
                df_15m = ts.as_pandas()
                if not df_15m.empty:
                    df_15m = df_15m.rename(columns={
                        'open': 'Open', 'high': 'High', 'low': 'Low',
                        'close': 'Close', 'volume': 'Volume'
                    })
                    df_15m = df_15m.sort_index()

                    if "JPY" in symbol:
                        spread = 0.02
                    elif "Gold" in name or "Silver" in name:
                        spread = 0.50
                    elif "Oil" in name:
                        spread = 0.05
                    else:
                        spread = 0.0002
                    return df_15m, spread
        except Exception as e:
            try:
                yf_symbol_map = {
                    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
                    "USD/CHF": "USDCHF=X", "AUD/USD": "AUD/USD", "USD/CAD": "USDCAD=X",
                    "NZD/USD": "NZDUSD=X", "EUR/GBP": "EURGBP=X", "GBP/JPY": "GBPJPY=X",
                    "AUD/JPY": "AUDJPY=X", "Gold (طلا)": "GC=F", "Silver (نقره)": "SI=F",
                    "Crude Oil (نفت برنت)": "BZ=F", "S&P 500": "^GSPC", "Nasdaq": "^IXIC"
                }
                yf_sym = yf_symbol_map.get(name, symbol)
                ticker = yf.Ticker(yf_sym)
                df_15m = ticker.history(period="2d", interval="15m")
                if not df_15m.empty:
                    if "JPY" in symbol:
                        spread = 0.02
                    elif "Gold" in name or "Silver" in name:
                        spread = 0.50
                    elif "Oil" in name:
                        spread = 0.05
                    else:
                        spread = 0.0002
                    return df_15m, spread
            except Exception as yf_e:
                print(f"❌ yfinance Fallback Error for {name}: {yf_e}")

    return df_15m, spread


# --- سیستم جامع کندل‌خوانی و پرایس‌اکشن ---
def detect_candlestick_patterns(df_15m):
    df_15m['Bullish_Engulfing'] = False
    df_15m['Bearish_Engulfing'] = False
    df_15m['Bullish_Pinbar'] = False
    df_15m['Bearish_Pinbar'] = False
    df_15m['Doji'] = False
    df_15m['Hammer'] = False

    if len(df_15m) < 3:
        return df_15m

    for i in range(1, len(df_15m)):
        curr = df_15m.iloc[i]
        prev = df_15m.iloc[i - 1]

        c_open, c_high, c_low, c_close = curr['Open'], curr['High'], curr['Low'], curr['Close']
        p_open, p_high, p_low, p_close = prev['Open'], prev['High'], prev['Low'], prev['Close']

        c_body = abs(c_close - c_open)
        c_range = c_high - c_low
        if c_range == 0:
            continue

        if c_body <= (c_range * 0.1):
            df_15m.iloc[i, df_15m.columns.get_loc('Doji')] = True

        if p_close < p_open and c_close > c_open and c_open <= p_close and c_close >= p_open:
            df_15m.iloc[i, df_15m.columns.get_loc('Bullish_Engulfing')] = True
        if p_close > p_open and c_close < c_open and c_open >= p_close and c_close <= p_open:
            df_15m.iloc[i, df_15m.columns.get_loc('Bearish_Engulfing')] = True

        lower_shadow = min(c_open, c_close) - c_low
        upper_shadow = c_high - max(c_open, c_close)

        if lower_shadow > (2 * c_body) and upper_shadow < (0.5 * c_body):
            df_15m.iloc[i, df_15m.columns.get_loc('Bullish_Pinbar')] = True
            df_15m.iloc[i, df_15m.columns.get_loc('Hammer')] = True
        if upper_shadow > (2 * c_body) and lower_shadow < (0.5 * c_body):
            df_15m.iloc[i, df_15m.columns.get_loc('Bearish_Pinbar')] = True

    return df_15m


def detect_fvg(df):
    df['Bullish_FVG'] = False
    df['Bearish_FVG'] = False

    if len(df) < 3:
        return df

    for i in range(2, len(df)):
        if df['Low'].iloc[i] > df['High'].iloc[i - 2] and df['Close'].iloc[i - 1] > df['Open'].iloc[i - 1]:
            df.iloc[i, df.columns.get_loc('Bullish_FVG')] = True

        if df['High'].iloc[i] < df['Low'].iloc[i - 2] and df['Close'].iloc[i - 1] < df['Open'].iloc[i - 1]:
            df.iloc[i, df.columns.get_loc('Bearish_FVG')] = True

    return df


def detect_order_blocks(df):
    df['Bullish_OB'] = False
    df['Bearish_OB'] = False

    if len(df) < 5:
        return df

    for i in range(3, len(df)):
        body = abs(df['Close'].iloc[i] - df['Open'].iloc[i])
        avg_body = abs(df['Close'] - df['Open']).rolling(20).mean().iloc[i]

        if df['Close'].iloc[i] > df['Open'].iloc[i] and body > (avg_body * 1.5):
            for j in range(i - 1, max(0, i - 4), -1):
                if df['Close'].iloc[j] < df['Open'].iloc[j]:
                    df.iloc[j, df.columns.get_loc('Bullish_OB')] = True
                    break

        elif df['Close'].iloc[i] < df['Open'].iloc[i] and body > (avg_body * 1.5):
            for j in range(i - 1, max(0, i - 4), -1):
                if df['Close'].iloc[j] > df['Open'].iloc[j]:
                    df.iloc[j, df.columns.get_loc('Bearish_OB')] = True
                    break

    return df


def check_rsi_divergence(df):
    if len(df) < 20 or 'RSI' not in df.columns:
        return "NONE"

    recent = df.tail(15)
    price_lows = recent['Low']
    rsi_lows = recent['RSI']

    if price_lows.iloc[-1] < price_lows.iloc[0] and rsi_lows.iloc[-1] > rsi_lows.iloc[0] and rsi_lows.iloc[-1] < 45:
        return "BULLISH_DIVERGENCE"

    price_highs = recent['High']
    rsi_highs = recent['RSI']

    if price_highs.iloc[-1] > price_highs.iloc[0] and rsi_highs.iloc[-1] < rsi_highs.iloc[0] and rsi_highs.iloc[
        -1] > 55:
        return "BEARISH_DIVERGENCE"

    return "NONE"


def detect_market_structure(df):
    if len(df) < 20:
        return "RANGE"

    swing_highs = df['High'][(df['High'] > df['High'].shift(1)) & (df['High'] > df['High'].shift(-1))]
    swing_lows = df['Low'][(df['Low'] < df['Low'].shift(1)) & (df['Low'] < df['Low'].shift(-1))]

    last_high = swing_highs.iloc[-1] if not swing_highs.empty else df['High'].max()
    last_low = swing_lows.iloc[-1] if not swing_lows.empty else df['Low'].min()

    current_close = df['Close'].iloc[-1]

    if current_close > last_high:
        return "BULLISH_BOS"
    elif current_close < last_low:
        return "BEARISH_BOS"
    return "RANGE"


def calculate_advanced_indicators(df_15m, df_1m):
    delta = df_15m['Close'].diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, 1e-10)
    df_15m['RSI'] = 100 - (100 / (1 + rs))
    df_15m['EMA20'] = df_15m['Close'].ewm(span=20, adjust=False).mean()

    ema12 = df_15m['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df_15m['Close'].ewm(span=26, adjust=False).mean()
    df_15m['MACD'] = ema12 - ema26
    df_15m['MACD_Signal'] = df_15m['MACD'].ewm(span=9, adjust=False).mean()

    df_15m['BB_Middle'] = df_15m['Close'].rolling(window=20).mean()
    bb_std = df_15m['Close'].rolling(window=20).std()
    df_15m['BB_Upper'] = df_15m['BB_Middle'] + (bb_std * 2)
    df_15m['BB_Lower'] = df_15m['BB_Middle'] - (bb_std * 2)

    df_15m = detect_candlestick_patterns(df_15m)
    df_15m = detect_fvg(df_15m)
    df_15m = detect_order_blocks(df_15m)

    if 'Volume' in df_15m.columns:
        df_15m['Volume_SMA20'] = df_15m['Volume'].rolling(window=20).mean()
    else:
        df_15m['Volume_SMA20'] = 0
        df_15m['Volume'] = 0

    high_low = df_15m['High'] - df_15m['Low']
    high_close = (df_15m['High'] - df_15m['Close'].shift()).abs()
    low_close = (df_15m['Low'] - df_15m['Close'].shift()).abs()
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    df_15m['ATR'] = true_range.rolling(window=14).mean()

    micro_trend_score = "خنثی"
    if not df_1m.empty and len(df_1m) >= 30:
        last_30m = df_1m.tail(30)
        start_price_30m = last_30m['Close'].iloc[0]
        end_price_30m = last_30m['Close'].iloc[-1]
        price_diff_30m = end_price_30m - start_price_30m
        minute_volatility = last_30m['Close'].diff().abs().mean()

        if price_diff_30m > 0 and minute_volatility > 0.0001:
            micro_trend_score = "صعودی پرشتاب در ۳۰ دقیقه گذشته 📈"
        elif price_diff_30m < 0 and minute_volatility > 0.0001:
            micro_trend_score = "نزولی پرشتاب در ۳۰ دقیقه گذشته 📉"
        else:
            micro_trend_score = "رنج و کم‌نوسان در ۳۰ دقیقه گذشته ↔️"

    return df_15m, micro_trend_score


def get_higher_timeframe_trend(symbol, name):
    try:
        df_4h = pd.DataFrame()
        if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
            ccxt_symbols = {
                "Bitcoin": "BTC/USDT",
                "Ethereum": "ETH/USDT",
                "Solana": "SOL/USDT",
                "Ripple": "XRP/USDT"
            }
            ccxt_symbol = ccxt_symbols.get(name, "BTC/USDT")
            ohlcv = exchange.fetch_ohlcv(ccxt_symbol, timeframe='4h', limit=50)
            if ohlcv:
                df_4h = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df_4h.set_index('timestamp', inplace=True)
        elif td_client:
            try:
                td_mapping = {
                    "EUR/USD": "EUR/USD", "GBP/USD": "GBP/USD", "USD/JPY": "USD/JPY",
                    "USD/CHF": "USD/CHF", "AUD/USD": "AUD/USD", "USD/CAD": "USD/CAD",
                    "NZD/USD": "NZD/USD", "EUR/GBP": "EUR/GBP", "GBP/JPY": "GBP/JPY",
                    "AUD/JPY": "AUD/JPY", "Gold (طلا)": "XAU/USD", "Silver (نقره)": "XAG/USD",
                    "Crude Oil (نفت برنت)": "BRENT", "S&P 500": "SPX", "Nasdaq": "IXIC"
                }
                td_sym = td_mapping.get(name)
                if td_sym:
                    ts = td_client.time_series(symbol=td_sym, interval="4h", outputsize=50)
                    df_4h = ts.as_pandas().rename(
                        columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close',
                                 'volume': 'Volume'}).sort_index()
            except Exception:
                yf_symbol_map = {
                    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
                    "USD/CHF": "USDCHF=X", "AUD/USD": "AUD/USD", "USD/CAD": "USDCAD=X",
                    "NZD/USD": "NZDUSD=X", "EUR/GBP": "EURGBP=X", "GBP/JPY": "GBPJPY=X",
                    "AUD/JPY": "AUDJPY=X", "Gold (طلا)": "GC=F", "Silver (نقره)": "SI=F",
                    "Crude Oil (نفت برنت)": "BZ=F", "S&P 500": "^GSPC", "Nasdaq": "^IXIC"
                }
                yf_sym = yf_symbol_map.get(name, symbol)
                df_4h = yf.Ticker(yf_sym).history(period="60d", interval="4h")

        if df_4h.empty or len(df_4h) < 10:
            return "UNKNOWN"

        df_4h['EMA20'] = df_4h['Close'].ewm(span=20, adjust=False).mean()
        df_4h['EMA50'] = df_4h['Close'].ewm(span=50, adjust=False).mean()
        last_close = df_4h['Close'].iloc[-1]
        last_ema20 = df_4h['EMA20'].iloc[-1]
        last_ema50 = df_4h['EMA50'].iloc[-1]

        if last_close > last_ema20 and last_ema20 > last_ema50:
            return "BULLISH"
        elif last_close < last_ema20 and last_ema20 < last_ema50:
            return "BEARISH"
        return "NEUTRAL"
    except Exception as e:
        return "UNKNOWN"


def calculate_pivot_points(symbol, name):
    try:
        df_daily = pd.DataFrame()
        if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
            ccxt_symbols = {
                "Bitcoin": "BTC/USDT",
                "Ethereum": "ETH/USDT",
                "Solana": "SOL/USDT",
                "Ripple": "XRP/USDT"
            }
            ccxt_symbol = ccxt_symbols.get(name, "BTC/USDT")
            ohlcv = exchange.fetch_ohlcv(ccxt_symbol, timeframe='1d', limit=5)
            if ohlcv:
                df_daily = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        elif td_client:
            try:
                td_mapping = {
                    "EUR/USD": "EUR/USD", "GBP/USD": "GBP/USD", "USD/JPY": "USD/JPY",
                    "USD/CHF": "USD/CHF", "AUD/USD": "AUD/USD", "USD/CAD": "USD/CAD",
                    "NZD/USD": "NZD/USD", "EUR/GBP": "EUR/GBP", "GBP/JPY": "GBP/JPY",
                    "AUD/JPY": "AUD/JPY", "Gold (طلا)": "XAU/USD", "Silver (نقره)": "XAG/USD",
                    "Crude Oil (نفت برنت)": "BRENT", "S&P 500": "SPX", "Nasdaq": "IXIC"
                }
                td_sym = td_mapping.get(name)
                if td_sym:
                    ts = td_client.time_series(symbol=td_sym, interval="1day", outputsize=5)
                    df_daily = ts.as_pandas().rename(
                        columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close',
                                 'volume': 'Volume'}).sort_index()
            except Exception:
                yf_symbol_map = {
                    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
                    "USD/CHF": "USDCHF=X", "AUD/USD": "AUD/USD", "USD/CAD": "USDCAD=X",
                    "NZD/USD": "NZDUSD=X", "EUR/GBP": "EURGBP=X", "GBP/JPY": "GBPJPY=X",
                    "AUD/JPY": "AUDJPY=X", "Gold (طلا)": "GC=F", "Silver (نقره)": "SI=F",
                    "Crude Oil (نفت برنت)": "BZ=F", "S&P 500": "^GSPC", "Nasdaq": "^IXIC"
                }
                yf_sym = yf_symbol_map.get(name, symbol)
                df_daily = yf.Ticker(yf_sym).history(period="10d", interval="1d")

        if len(df_daily) >= 2:
            prev_day = df_daily.iloc[-2]
            high = prev_day['High']
            low = prev_day['Low']
            close = prev_day['Close']

            pivot = (high + low + close) / 3
            r1 = (2 * pivot) - low
            s1 = (2 * pivot) - high
            r2 = pivot + (high - low)
            s2 = pivot - (high - low)
            return pivot, r1, s1, r2, s2
    except Exception as e:
        pass
    return None, None, None, None, None


# --- سیستم امتیازدهی همگرایی وزن‌دار (بهینه‌شده با احتساب مومنتوم ۱ دقیقه) ---
def calculate_weighted_confluence_score(df_15m, trend_4h, micro_trend, divergence, structure, m1_direction=None):
    buy_score = 0.0
    sell_score = 0.0

    last_candle = df_15m.iloc[-1]
    current_price = last_candle['Close']
    ema20_val = last_candle['EMA20']
    rsi_val = last_candle['RSI']
    macd_val = last_candle['MACD']
    macd_signal = last_candle['MACD_Signal']
    current_vol = last_candle['Volume']
    vol_sma = last_candle['Volume_SMA20']
    bb_upper = last_candle['BB_Upper']
    bb_lower = last_candle['BB_Lower']
    atr_val = last_candle['ATR']

    # ۱. روند اصلی ۴ ساعته
    if trend_4h == "BULLISH":
        buy_score += 3.0
    elif trend_4h == "BEARISH":
        sell_score += 3.0

    # ۲. ساختار بازار (BOS)
    if structure == "BULLISH_BOS":
        buy_score += 2.5
    elif structure == "BEARISH_BOS":
        sell_score += 2.5

    # ۳. اوردر بلاک و FVG
    if last_candle.get('Bullish_OB', False) or last_candle.get('Bullish_FVG', False):
        buy_score += 3.0
    if last_candle.get('Bearish_OB', False) or last_candle.get('Bearish_FVG', False):
        sell_score += 3.0

    # ۴. الگوهای کندلی پرقدرت (اینگالف، پین‌بار، چکش)
    if last_candle['Bullish_Engulfing'] or last_candle['Bullish_Pinbar'] or last_candle['Hammer']:
        buy_score += 2.5
    if last_candle['Bearish_Engulfing'] or last_candle['Bearish_Pinbar']:
        sell_score += 2.5

    # ۵. تاثیر ویژه مومنتوم ۱ ساعت گذشته در تایم‌فریم ۱ دقیقه
    if m1_direction == "STRONG_BULLISH":
        buy_score += 2.0
    elif m1_direction == "STRONG_BEARISH":
        sell_score += 2.0

    # ۶. تاییدیه حجم (Volume Breakout)
    if vol_sma > 0 and current_vol >= (vol_sma * 1.3):
        buy_score += 1.5
        sell_score += 1.5

    # ۷. واگرایی و اندیکاتورها
    if divergence == "BULLISH_DIVERGENCE":
        buy_score += 2.0
    elif divergence == "BEARISH_DIVERGENCE":
        sell_score += 2.0

    if current_price > ema20_val and rsi_val < 60:
        buy_score += 1.0
    if current_price < ema20_val and rsi_val > 40:
        sell_score += 1.0

    if macd_val > macd_signal and current_price <= bb_lower + (atr_val * 0.5):
        buy_score += 1.5
    if macd_val < macd_signal and current_price >= bb_upper - (atr_val * 0.5):
        sell_score += 1.5

    # ۸. مومنتوم ۳۰ دقیقه‌ای
    if "صعودی" in micro_trend:
        buy_score += 1.0
    elif "نزولی" in micro_trend:
        sell_score += 1.0

    return round(buy_score, 1), round(sell_score, 1)


async def analyze_single_symbol(symbol, name):
    try:
        df_15m, spread = get_market_data(symbol, name)
        df_1m = pd.DataFrame()

        if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
            ccxt_symbols = {
                "Bitcoin": "BTC/USDT",
                "Ethereum": "ETH/USDT",
                "Solana": "SOL/USDT",
                "Ripple": "XRP/USDT"
            }
            ccxt_symbol = ccxt_symbols.get(name, "BTC/USDT")
            ohlcv_1m = exchange.fetch_ohlcv(ccxt_symbol, timeframe='1m', limit=60)
            if ohlcv_1m:
                df_1m = pd.DataFrame(ohlcv_1m, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df_1m.set_index('timestamp', inplace=True)

        if df_15m.empty or len(df_15m) < 20:
            return None

        df_15m, micro_trend = calculate_advanced_indicators(df_15m, df_1m)
        trend_4h = get_higher_timeframe_trend(symbol, name)
        divergence = check_rsi_divergence(df_15m)
        structure = detect_market_structure(df_15m)
        pivot, r1, s1, r2, s2 = calculate_pivot_points(symbol, name)

        # محاسبه جهت مومنتوم ۱ ساعت گذشته در تایم‌فریم ۱ دقیقه
        m1_direction = "NEUTRAL"
        if not df_1m.empty and len(df_1m) >= 60:
            m1_start_price = df_1m['Close'].iloc[0]
            m1_end_price = df_1m['Close'].iloc[-1]
            if m1_end_price > m1_start_price:
                m1_direction = "STRONG_BULLISH"
            elif m1_end_price < m1_start_price:
                m1_direction = "STRONG_BEARISH"

        last_candle = df_15m.iloc[-1]
        current_price = last_candle['Close']
        atr_val = last_candle['ATR'] if not pd.isna(last_candle['ATR']) and last_candle['ATR'] > 0 else 0.0010

        buy_score, sell_score = calculate_weighted_confluence_score(
            df_15m, trend_4h, micro_trend, divergence, structure, m1_direction
        )

        signal_type = "WAIT"
        required_score = 7.5

        if buy_score >= required_score and "نزولی" not in micro_trend:
            signal_type = "BUY"
            entry_exec = current_price + spread
            sl_price = (s1 if s1 and s1 < current_price else current_price - (atr_val * 1.5)) - spread
            tp1_price = (r1 if r1 and r1 > current_price else current_price + (atr_val * 2.5))
        elif sell_score >= required_score and "صعودی" not in micro_trend:
            signal_type = "SELL"
            entry_exec = current_price - spread
            sl_price = (r1 if r1 and r1 > current_price else current_price + (atr_val * 1.5)) + spread
            tp1_price = (s1 if s1 and s1 < current_price else current_price - (atr_val * 2.5))
        else:
            entry_exec, sl_price, tp1_price = current_price, current_price, current_price

        pattern_desc = "هیچ‌کدام"
        if last_candle['Bullish_Engulfing']:
            pattern_desc = "Bullish Engulfing"
        elif last_candle['Bearish_Engulfing']:
            pattern_desc = "Bearish Engulfing"
        elif last_candle['Bullish_Pinbar']:
            pattern_desc = "Bullish Pin Bar"
        elif last_candle['Bearish_Pinbar']:
            pattern_desc = "Bearish Pin Bar"
        elif last_candle['Hammer']:
            pattern_desc = "Hammer (چکش)"
        elif last_candle['Doji']:
            pattern_desc = "Doji (دوجی)"

        if signal_type != "WAIT":
            log_signal(name, signal_type, entry_exec, sl_price, tp1_price, tp1_price)

        return {
            "name": name,
            "signal": signal_type,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "price": current_price,
            "pattern": pattern_desc,
            "divergence": divergence,
            "structure": structure
        }
    except Exception as e:
        print(f"Analyze Single Symbol Error ({name}): {e}")
        return None


async def get_secure_high_profit_analysis(symbol, name):
    def fetch_and_generate():
        try:
            df_15m, spread = get_market_data(symbol, name)
            df_1m = pd.DataFrame()
            if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
                ccxt_symbols = {
                    "Bitcoin": "BTC/USDT",
                    "Ethereum": "ETH/USDT",
                    "Solana": "SOL/USDT",
                    "Ripple": "XRP/USDT"
                }
                ccxt_symbol = ccxt_symbols.get(name, "BTC/USDT")
                ohlcv_1m = exchange.fetch_ohlcv(ccxt_symbol, timeframe='1m', limit=60)
                if ohlcv_1m:
                    df_1m = pd.DataFrame(ohlcv_1m, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                    df_1m.set_index('timestamp', inplace=True)

            if df_15m.empty or len(df_15m) < 20:
                return f"❌ اطلاعات کافی از بازار برای نماد {name} یافت نشد."

            df_15m, micro_trend = calculate_advanced_indicators(df_15m, df_1m)
            trend_4h = get_higher_timeframe_trend(symbol, name)
            divergence = check_rsi_divergence(df_15m)
            structure = detect_market_structure(df_15m)
            pivot, r1, s1, r2, s2 = calculate_pivot_points(symbol, name)

            m1_direction = "NEUTRAL"
            if not df_1m.empty and len(df_1m) >= 60:
                m1_start_price = df_1m['Close'].iloc[0]
                m1_end_price = df_1m['Close'].iloc[-1]
                if m1_end_price > m1_start_price:
                    m1_direction = "STRONG_BULLISH"
                elif m1_end_price < m1_start_price:
                    m1_direction = "STRONG_BEARISH"

            last_candle = df_15m.iloc[-1]
            current_price = last_candle['Close']
            rsi_val = last_candle['RSI']
            atr_val = last_candle['ATR'] if not pd.isna(last_candle['ATR']) and last_candle['ATR'] > 0 else 0.0010
            macd_val = last_candle['MACD']
            macd_signal = last_candle['MACD_Signal']

            buy_score, sell_score = calculate_weighted_confluence_score(
                df_15m, trend_4h, micro_trend, divergence, structure, m1_direction
            )

            pattern_desc = "هیچ‌کدام"
            if last_candle['Bullish_Engulfing']:
                pattern_desc = "Bullish Engulfing (پوشاننده صعودی)"
            elif last_candle['Bearish_Engulfing']:
                pattern_desc = "Bearish Engulfing (پوشاننده نزولی)"
            elif last_candle['Bullish_Pinbar']:
                pattern_desc = "Bullish Pin Bar (پین‌بار صعودی)"
            elif last_candle['Bearish_Pinbar']:
                pattern_desc = "Bearish Pin Bar (پین‌بار نزولی)"
            elif last_candle['Hammer']:
                pattern_desc = "Hammer (چکش صعودی)"
            elif last_candle['Doji']:
                pattern_desc = "Doji (دوجی مردد)"

            if last_candle.get('Bullish_OB', False):
                pattern_desc += " + Bullish OB"
            if last_candle.get('Bearish_OB', False):
                pattern_desc += " + Bearish OB"
            if last_candle.get('Bullish_FVG', False):
                pattern_desc += " + Bullish FVG"
            if last_candle.get('Bearish_FVG', False):
                pattern_desc += " + Bearish FVG"

            signal_type = "WAIT"
            required_score = 7.5

            if buy_score >= required_score and "نزولی" not in micro_trend:
                signal_type = "BUY"
                entry_exec = current_price + spread
                sl_price = (s1 if s1 and s1 < current_price else current_price - (atr_val * 1.5)) - spread
                tp1_price = (r1 if r1 and r1 > current_price else current_price + (atr_val * 2.5))
                tp2_price = (r2 if r2 and r2 > tp1_price else current_price + (atr_val * 4.0))
            elif sell_score >= required_score and "صعودی" not in micro_trend:
                signal_type = "SELL"
                entry_exec = current_price - spread
                sl_price = (r1 if r1 and r1 > current_price else current_price + (atr_val * 1.5)) + spread
                tp1_price = (s1 if s1 and s1 < current_price else current_price - (atr_val * 2.5))
                tp2_price = (s2 if s2 and s2 < tp1_price else current_price - (atr_val * 4.0))
            else:
                entry_exec, sl_price, tp1_price, tp2_price = current_price, current_price, current_price, current_price

            recommended_lot = calculate_lot_size(name, entry_exec, sl_price, ACCOUNT_BALANCE, RISK_PERCENTAGE)

            if signal_type != "WAIT":
                log_signal(name, signal_type, entry_exec, sl_price, tp1_price, tp2_price)

            prompt = f"""
تو یک تحلیلگر ارشد بازارهای مالی و معامله‌گر حرفه‌ای پرایس‌اکشن و ساختار بازار (SMC) هستی.
اطلاعات دقیق نماد {name}:
- قیمت فعلی بازار: {current_price:.4f} | قیمت اجرایی با اسپرد: {entry_exec:.4f} (اسپرد: {spread})
- جهت سیگنال: {signal_type} | امتیاز همگرایی وزنی خرید: {buy_score} | فروش: {sell_score}
- ساختار بازار (Market Structure): {structure}
- وضعیت واگرایی RSI: {divergence}
- الگوی کندلی و اسمارت مانی فعال: {pattern_desc}
- روند ۴ ساعته: {trend_4h} | مومنتوم ۱ ساعته M1: {m1_direction}

لطفاً یک تحلیل کوتاه، بسیار دقیق، آکادمیک و تخصصی (حداکثر ۳ الی ۴ خط) به زبان فارسی بنویس که دلیل ورود یا لزوم صبر کردن را بر اساس تلاقی ساختار بازار، اوردر بلاک‌ها، FVG و مومنتوم توضیح دهد.
"""

            ai_reasoning = "تحلیل عمیق فنی بر اساس تلاقی ساختار بازار (BOS)، اوردر بلاک (OB)، نقدینگی FVG و مومنتوم تایید شد."
            if OPENAI_API_KEY:
                try:
                    headers = {
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7
                    }
                    response = requests.post(
                        "https://api.openai.com/v1/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=15
                    )
                    if response.status_code == 200:
                        data = response.json()
                        ai_reasoning = data["choices"][0]["message"]["content"].strip()
                except Exception as e:
                    print(f"OpenAI API Error: {e}")

            pivot_info = f"\n📐 **سطوح ساختاری (Pivot):**\n- Pivot: {pivot:.4f}\n- S1: {s1:.4f} | R1: {r1:.4f}" if pivot else ""
            div_text = "صعودی 📈" if divergence == "BULLISH_DIVERGENCE" else (
                "نزولی 📉" if divergence == "BEARISH_DIVERGENCE" else "ندارد ➖")

            result_message = f"""
🛡️ **گزارش تحلیل عمیق و فوق‌پیشرفته فنی (SMC) [{name}]**

🎯 **نتیجه نهایی:** {"✅ سیگنال ورود با تاییدیه وزنی اسمارت مانی" if signal_type != "WAIT" else "❌ عدم رسیدن به حد نصاب همگرایی"}
💡 **جهت معامله:** {signal_type}
📊 **قیمت اجرایی:** {entry_exec:.4f} (اسپرد: {spread})
💼 **حجم پیشنهادی معامله:** {recommended_lot} لات (ریسک: {RISK_PERCENTAGE}٪ از حساب {ACCOUNT_BALANCE}$)
⭐ **امتیاز همگرایی وزنی:** خرید: {buy_score} | فروش: {sell_score}

🧱 **ساختار بازار (Structure):** {structure}
🌀 **واگرایی RSI:** {div_text}
🕯️ **پرایس‌اکشن و اسمارت مانی:** {pattern_desc}
📈 **روند ۴ ساعته:** {trend_4h}

🛑 **حد ضرر اولیه (SL):** {sl_price:.4f}
🎯 **هدف سود اول (TP1):** {tp1_price:.4f}
🎯 **هدف سود دوم (TP2):** {tp2_price:.4f}
{pivot_info}

📝 **تفسیر هوش مصنوعی (SMC & Price Action):**
{ai_reasoning}
            """
            return result_message.strip()
        except Exception as e:
            return f"❌ خطای غیرمنتظره در تحلیل:\n{e}"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fetch_and_generate)


async def scan_all_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        "🔍 **در حال اسکن همزمان تمام بازارهای مالی بر اساس سیستم اسمارت مانی و امتیازدهی وزن‌دار... لطفاً چند لحظه صبر کنید.** ⏳")

    symbols_to_scan = [
        ("EURUSD", "EUR/USD"), ("GBPUSD", "GBP/USD"), ("USDJPY", "USD/JPY"),
        ("USDCHF", "USD/CHF"), ("AUDUSD", "AUD/USD"), ("USDCAD", "USD/CAD"),
        ("NZDUSD", "NZD/USD"), ("EURGBP", "EUR/GBP"), ("GBPJPY", "GBP/JPY"),
        ("AUDJPY", "AUD/JPY"), ("XAUUSD", "Gold (طلا)"), ("XAGUSD", "Silver (نقره)"),
        ("BRENT", "Crude Oil (نفت برنت)"), ("BTC/USDT", "Bitcoin"),
        ("ETH/USDT", "Ethereum"), ("SOL/USDT", "Solana"), ("XRP/USDT", "Ripple"),
        ("SPX", "S&P 500"), ("IXIC", "Nasdaq")
    ]

    active_signals = []
    waiting_summary = []

    for sym, name in symbols_to_scan:
        res = await analyze_single_symbol(sym, name)
        if res:
            if res["signal"] != "WAIT":
                active_signals.append(res)
            else:
                waiting_summary.append(f"• {name}: صبر (خرید: {res['buy_score']} | فروش: {res['sell_score']})")

    report = "📊 **نتیجه اسکن جامع بازارهای مالی (SMC):**\n\n"

    if active_signals:
        report += "🚨 **سیگنال‌های فعال با همگرایی بالای فنی:**\n"
        for sig in active_signals:
            report += f"✅ **{sig['name']}** ➔ جهت: **{sig['signal']}** | خرید: {sig['buy_score']} | فروش: {sig['sell_score']}\n"
    else:
        report += "ℹ️ در حال حاضر هیچ‌کدام از نمادها به حد نصاب امتیاز همگرایی وزن‌دار نرسیده‌اند.\n\n"

    report += "\n📋 **وضعیت سایر نمادها:**\n" + "\n".join(waiting_summary[:10])

    keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(report, reply_markup=reply_markup)


# --- مانیتورینگ پس‌زمینه و ارسال هشدار خودکار فرصت‌های معاملاتی ---
async def background_market_monitor():
    global ACTIVE_CHAT_ID, TELEGRAM_APP_INSTANCE

    symbols_to_scan = [
        ("EURUSD", "EUR/USD"), ("GBPUSD", "GBP/USD"), ("USDJPY", "USD/JPY"),
        ("USDCHF", "USD/CHF"), ("AUDUSD", "AUD/USD"), ("USDCAD", "USD/CAD"),
        ("NZDUSD", "NZD/USD"), ("EURGBP", "EUR/GBP"), ("GBPJPY", "GBP/JPY"),
        ("AUDJPY", "AUD/JPY"), ("XAUUSD", "Gold (طلا)"), ("XAGUSD", "Silver (نقره)"),
        ("BRENT", "Crude Oil (نفت برنت)"), ("BTC/USDT", "Bitcoin"),
        ("ETH/USDT", "Ethereum"), ("SOL/USDT", "Solana"), ("XRP/USDT", "Ripple"),
        ("SPX", "S&P 500"), ("IXIC", "Nasdaq")
    ]

    while True:
        # اسکن خودکار بازارها هر ۳۰ دقیقه یک‌بار (قابل تغییر به زمان دلخواه مثلاً 900 برای ۱۵ دقیقه)
        await asyncio.sleep(1800)

        if not ACTIVE_CHAT_ID or not TELEGRAM_APP_INSTANCE:
            continue

        try:
            hot_signals = []
            for sym, name in symbols_to_scan:
                res = await analyze_single_symbol(sym, name)
                if res and res["signal"] != "WAIT":
                    hot_signals.append(res)

            if hot_signals:
                alert_text = "🚨 **فرصت معاملاتی طلایی کشف شد!** 🎯\n\nربات بررسی کرد و نمادهای زیر شرایط مناسبی برای معامله دارند:\n\n"
                for sig in hot_signals:
                    alert_text += f"💎 **{sig['name']}**\n"
                    alert_text += f"🔹 جهت معامله: **{sig['signal']}**\n"
                    alert_text += f"⭐ امتیاز همگرایی: خرید ({sig['buy_score']}) | فروش ({sig['sell_score']})\n"
                    alert_text += f"🕯️ الگو: {sig['pattern']}\n"
                    alert_text += f"📊 قیمت لحظه‌ای: {sig['price']:.4f}\n\n"

                alert_text += "⚠️ حتماً پیش از ورود، مدیریت ریسک را رعایت کنید."

                await TELEGRAM_APP_INSTANCE.bot.send_message(
                    chat_id=ACTIVE_CHAT_ID,
                    text=alert_text,
                    parse_mode="Markdown"
                )
        except Exception as e:
            print(f"Auto-Scan Background Error: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_CHAT_ID
    ACTIVE_CHAT_ID = update.effective_chat.id

    log_user_action(update.effective_user, "COMMAND_START")

    keyboard = [
        [InlineKeyboardButton("🔍 اسکن و تحلیل همزمان همه بازارها", callback_data="scan_all")],
        [InlineKeyboardButton("💶 EUR/USD", callback_data="eurusd"),
         InlineKeyboardButton("💷 GBP/USD", callback_data="gbpusd")],
        [InlineKeyboardButton("💴 USD/JPY", callback_data="usdjpy"),
         InlineKeyboardButton("🇨🇭 USD/CHF", callback_data="usdchf")],
        [InlineKeyboardButton("🇦🇺 AUD/USD", callback_data="audusd"),
         InlineKeyboardButton("🇨🇦 USD/CAD", callback_data="usdcad")],
        [InlineKeyboardButton("🇳🇿 NZD/USD", callback_data="nzdusd"),
         InlineKeyboardButton("🇪🇺 EUR/GBP", callback_data="eurgbp")],
        [InlineKeyboardButton("💷 GBP/JPY", callback_data="gbpjpy"),
         InlineKeyboardButton("🇦🇺 AUD/JPY", callback_data="audjpy")],
        [InlineKeyboardButton("🥇 Gold", callback_data="gold"),
         InlineKeyboardButton("🥈 Silver", callback_data="silver")],
        [InlineKeyboardButton("🛢 Crude Oil", callback_data="oil"),
         InlineKeyboardButton("₿ Bitcoin", callback_data="btc")],
        [InlineKeyboardButton("Ξ Ethereum", callback_data="eth"),
         InlineKeyboardButton("🟣 Solana", callback_data="sol")],
        [InlineKeyboardButton("🔵 Ripple", callback_data="xrp"),
         InlineKeyboardButton("📈 S&P 500", callback_data="sp500")],
        [InlineKeyboardButton("📉 Nasdaq", callback_data="nasdaq")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_message = "سلام! ربات پیشرفته معاملاتی (همراه با تحلیل SMC و مومنتوم M1) آماده است. یکی از گزینه‌ها را انتخاب کنید:"

    if update.message:
        await update.message.reply_text(welcome_message, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(welcome_message, reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    log_user_action(update.effective_user, f"SELECT_SYMBOL_{query.data.upper()}")

    await query.message.reply_text(
        "⚡ **در حال تحلیل اسمارت مانی (OB & FVG)، پرایس‌اکشن، ساختار بازار و مومنتوم لحظه‌ای...** ⏳")

    routes = {
        "eurusd": ("EURUSD", "EUR/USD"),
        "gbpusd": ("GBPUSD", "GBP/USD"),
        "usdjpy": ("USDJPY", "USD/JPY"),
        "usdchf": ("USDCHF", "USD/CHF"),
        "audusd": ("AUDUSD", "AUD/USD"),
        "usdcad": ("USDCAD", "USD/CAD"),
        "nzdusd": ("NZDUSD", "NZD/USD"),
        "eurgbp": ("EURGBP", "EUR/GBP"),
        "gbpjpy": ("GBPJPY", "GBP/JPY"),
        "audjpy": ("AUDJPY", "AUD/JPY"),
        "gold": ("XAUUSD", "Gold (طلا)"),
        "silver": ("XAGUSD", "Silver (نقره)"),
        "oil": ("BRENT", "Crude Oil (نفت برنت)"),
        "btc": ("BTC/USDT", "Bitcoin"),
        "eth": ("ETH/USDT", "Ethereum"),
        "sol": ("SOL/USDT", "Solana"),
        "xrp": ("XRP/USDT", "Ripple"),
        "sp500": ("SPX", "S&P 500"),
        "nasdaq": ("IXIC", "Nasdaq")
    }

    if query.data in routes:
        sym, name = routes[query.data]
        result = await get_secure_high_profit_analysis(sym, name)
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(result, reply_markup=reply_markup)


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update.effective_user, "BACK_TO_MENU")
    await start(update, context)


async def post_init(application):
    global TELEGRAM_APP_INSTANCE
    TELEGRAM_APP_INSTANCE = application
    asyncio.create_task(background_market_monitor())


if __name__ == '__main__':
    if not TELEGRAM_BOT_TOKEN:
        print("خطا: توکن ربات تلگرام در فایل .env یافت نشد!")
        exit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(scan_all_markets, pattern="^scan_all$"))
    app.add_handler(CallbackQueryHandler(button_handler,
                                         pattern="^(eurusd|gbpusd|usdjpy|usdchf|audusd|usdcad|nzdusd|eurgbp|gbpjpy|audjpy|gold|silver|oil|btc|eth|sol|xrp|sp500|nasdaq)$"))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))

    print("ربات هوشمند معاملاتی مجهز به سیستم اسمارت مانی و اسکن خودکار روشن شد...")
    app.run_polling()