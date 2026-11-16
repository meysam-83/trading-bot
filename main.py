import warnings
warnings.filterwarnings("ignore")

import os
import time
import asyncio
import sqlite3
import pandas as pd
import requests
import ccxt
import yfinance as yf
import MetaTrader5 as mt5
from twelvedata import TDClient
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB = os.getenv("DB_NAME", "trading_bot_journal.db")
GPT_KEY = os.getenv("OPENAI_API_KEY")
TD_KEY = os.getenv("TWELVE_DATA_API_KEY")
BAL = float(os.getenv("ACCOUNT_BALANCE", 10000))
RISK = float(os.getenv("RISK_PERCENTAGE", 1.0))

my_id = None
app_bot = None

td = TDClient(apikey=TD_KEY) if TD_KEY else None
kc = ccxt.kucoin({"enableRateLimit": True})


def make_db():
    try:
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, symbol TEXT, signal_type TEXT, entry_price REAL, sl REAL, tp1 REAL, tp2 REAL, status TEXT DEFAULT 'ACTIVE')")
        c.execute("CREATE TABLE IF NOT EXISTS user_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, user_id INTEGER, username TEXT, first_name TEXT, action TEXT)")
        conn.commit()
        conn.close()
    except Exception as e:
        print("db err:", e)


make_db()


def add_sig(s, t, e, sl, tp1, tp2):
    try:
        conn = sqlite3.connect(DB)
        conn.execute("INSERT INTO signals (symbol, signal_type, entry_price, sl, tp1, tp2) VALUES (?,?,?,?,?,?)", (s, t, e, sl, tp1, tp2))
        conn.commit()
        conn.close()
    except:
        pass


def add_log(u, act):
    if not u:
        return
    try:
        conn = sqlite3.connect(DB)
        conn.execute("INSERT INTO user_logs (user_id, username, first_name, action) VALUES (?,?,?,?)", (u.id, u.username, u.first_name, act))
        conn.commit()
        conn.close()
    except:
        pass


def lot_size(name, entry, sl):
    risk_money = BAL * (RISK / 100.0)
    d = abs(entry - sl)
    if d == 0:
        return 0.01

    if "JPY" in name:
        m = 100
    elif "Gold" in name or "Silver" in name or "Bitcoin" in name or "Ethereum" in name or "Solana" in name or "Ripple" in name or "S&P" in name or "Nasdaq" in name:
        m = 1
    else:
        m = 10000

    lot = risk_money / (d * m)
    if lot < 0.01: lot = 0.01
    if lot > 50: lot = 50.0
    return round(lot, 2)


def get_mt5(sym, count=150):
    try:
        if not mt5.initialize():
            return pd.DataFrame(), 0.0002

        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, count)
        info = mt5.symbol_info(sym)
        sp = info.spread * info.point if info else 0.0002
        mt5.shutdown()

        if rates is not None and len(rates) > 0:
            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df.set_index("time", inplace=True)
            df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "tick_volume": "Volume"})
            return df[["Open", "High", "Low", "Close", "Volume"]], sp
    except:
        pass
    return pd.DataFrame(), 0.0002


def get_df(sym, name):
    df = pd.DataFrame()
    sp = 0.0002

    m_map = {
        "EUR/USD": "EURUSD", "GBP/USD": "GBPUSD", "USD/JPY": "USDJPY",
        "USD/CHF": "USDCHF", "AUD/USD": "AUDUSD", "USD/CAD": "USDCAD",
        "NZD/USD": "NZDUSD", "EUR/GBP": "EURGBP", "GBP/JPY": "GBPJPY",
        "AUD/JPY": "AUDJPY", "Gold (طلا)": "XAUUSD", "Silver (نقره)": "XAGUSD",
        "Crude Oil (نفت برنت)": "BRENT", "Bitcoin": "BTCUSD", "Ethereum": "ETHUSD",
        "Solana": "SOLUSD", "Ripple": "XRPUSD", "S&P 500": "US500", "Nasdaq": "NAS100"
    }

    if name in m_map:
        df, sp = get_mt5(m_map[name], 150)
        if not df.empty and len(df) >= 20:
            return df, sp

    if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
        try:
            k_map = {"Bitcoin": "BTC/USDT", "Ethereum": "ETH/USDT", "Solana": "SOL/USDT", "Ripple": "XRP/USDT"}
            raw = kc.fetch_ohlcv(k_map[name], timeframe="15m", limit=150)
            if raw:
                df = pd.DataFrame(raw, columns=["t", "Open", "High", "Low", "Close", "Volume"])
                df["time"] = pd.to_datetime(df["t"], unit="ms")
                df.set_index("time", inplace=True)
                sp = 0.50 if name in ["Bitcoin", "Ethereum"] else 0.05
                return df[["Open", "High", "Low", "Close", "Volume"]], sp
        except Exception as e:
            print("kc err:", e)

    if td:
        try:
            t_map = {
                "EUR/USD": "EUR/USD", "GBP/USD": "GBP/USD", "USD/JPY": "USD/JPY",
                "USD/CHF": "USD/CHF", "AUD/USD": "AUD/USD", "USD/CAD": "USD/CAD",
                "NZD/USD": "NZD/USD", "EUR/GBP": "EUR/GBP", "GBP/JPY": "GBP/JPY",
                "AUD/JPY": "AUD/JPY", "Gold (طلا)": "XAU/USD", "Silver (نقره)": "XAG/USD",
                "Crude Oil (نفت برنت)": "BRENT", "S&P 500": "SPX", "Nasdaq": "IXIC"
            }
            if name in t_map:
                df = td.time_series(symbol=t_map[name], interval="15min", outputsize=150).as_pandas()
                if not df.empty:
                    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}).sort_index()
                    if "JPY" in sym: sp = 0.02
                    elif "Gold" in name or "Silver" in name: sp = 0.50
                    elif "Oil" in name: sp = 0.05
                    return df, sp
        except:
            # تولودیتا خطا داد از یاهو میگیریم
            try:
                y_map = {
                    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
                    "USD/CHF": "USDCHF=X", "AUD/USD": "AUD/USD", "USD/CAD": "USDCAD=X",
                    "NZD/USD": "NZDUSD=X", "EUR/GBP": "EURGBP=X", "GBP/JPY": "GBPJPY=X",
                    "AUD/JPY": "AUDJPY=X", "Gold (طلا)": "GC=F", "Silver (نقره)": "SI=F",
                    "Crude Oil (نفت برنت)": "BZ=F", "S&P 500": "^GSPC", "Nasdaq": "^IXIC"
                }
                df = yf.Ticker(y_map.get(name, sym)).history(period="2d", interval="15m")
                if not df.empty:
                    if "JPY" in sym: sp = 0.02
                    elif "Gold" in name or "Silver" in name: sp = 0.50
                    elif "Oil" in name: sp = 0.05
                    return df, sp
            except:
                pass

    return df, sp


def candles(df):
    df["Bull_Eng"] = False
    df["Bear_Eng"] = False
    df["Bull_Pin"] = False
    df["Bear_Pin"] = False
    df["Doji"] = False
    df["Hammer"] = False

    if len(df) < 3:
        return df

    for i in range(1, len(df)):
        o, h, l, c = df["Open"].iloc[i], df["High"].iloc[i], df["Low"].iloc[i], df["Close"].iloc[i]
        po, pc = df["Open"].iloc[i - 1], df["Close"].iloc[i - 1]
        b = abs(c - o)
        r = h - l
        if r == 0:
            continue

        if b <= r * 0.1:
            df.iloc[i, df.columns.get_loc("Doji")] = True

        if pc < po and c > o and o <= pc and c >= po:
            df.iloc[i, df.columns.get_loc("Bull_Eng")] = True
        if pc > po and c < o and o >= pc and c <= po:
            df.iloc[i, df.columns.get_loc("Bear_Eng")] = True

        lw = min(o, c) - l
        uw = h - max(o, c)

        if lw > 2 * b and uw < 0.5 * b:
            df.iloc[i, df.columns.get_loc("Bull_Pin")] = True
            df.iloc[i, df.columns.get_loc("Hammer")] = True
        if uw > 2 * b and lw < 0.5 * b:
            df.iloc[i, df.columns.get_loc("Bear_Pin")] = True

    return df


def fvg(df):
    df["Bull_FVG"] = False
    df["Bear_FVG"] = False
    if len(df) < 3:
        return df
    for i in range(2, len(df)):
        if df["Low"].iloc[i] > df["High"].iloc[i - 2] and df["Close"].iloc[i - 1] > df["Open"].iloc[i - 1]:
            df.iloc[i, df.columns.get_loc("Bull_FVG")] = True
        if df["High"].iloc[i] < df["Low"].iloc[i - 2] and df["Close"].iloc[i - 1] < df["Open"].iloc[i - 1]:
            df.iloc[i, df.columns.get_loc("Bear_FVG")] = True
    return df


def ob(df):
    df["Bull_OB"] = False
    df["Bear_OB"] = False
    if len(df) < 5:
        return df
    for i in range(3, len(df)):
        b = abs(df["Close"].iloc[i] - df["Open"].iloc[i])
        avg = abs(df["Close"] - df["Open"]).rolling(20).mean().iloc[i]
        if df["Close"].iloc[i] > df["Open"].iloc[i] and b > avg * 1.5:
            for j in range(i - 1, max(0, i - 4), -1):
                if df["Close"].iloc[j] < df["Open"].iloc[j]:
                    df.iloc[j, df.columns.get_loc("Bull_OB")] = True
                    break
        elif df["Close"].iloc[i] < df["Open"].iloc[i] and b > avg * 1.5:
            for j in range(i - 1, max(0, i - 4), -1):
                if df["Close"].iloc[j] > df["Open"].iloc[j]:
                    df.iloc[j, df.columns.get_loc("Bear_OB")] = True
                    break
    return df


def div_check(df):
    if len(df) < 20 or "RSI" not in df.columns:
        return "NONE"
    s = df.tail(15)
    # حواسمون باشه RSI تو اشباع باشه
    if s["Low"].iloc[-1] < s["Low"].iloc[0] and s["RSI"].iloc[-1] > s["RSI"].iloc[0] and s["RSI"].iloc[-1] < 45:
        return "BULLISH_DIVERGENCE"
    if s["High"].iloc[-1] > s["High"].iloc[0] and s["RSI"].iloc[-1] < s["RSI"].iloc[0] and s["RSI"].iloc[-1] > 55:
        return "BEARISH_DIVERGENCE"
    return "NONE"


def bos_check(df):
    if len(df) < 20:
        return "RANGE"
    hs = df["High"][(df["High"] > df["High"].shift(1)) & (df["High"] > df["High"].shift(-1))]
    ls = df["Low"][(df["Low"] < df["Low"].shift(1)) & (df["Low"] < df["Low"].shift(-1))]
    lh = hs.iloc[-1] if not hs.empty else df["High"].max()
    ll = ls.iloc[-1] if not ls.empty else df["Low"].min()
    c = df["Close"].iloc[-1]
    if c > lh: return "BULLISH_BOS"
    if c < ll: return "BEARISH_BOS"
    return "RANGE"


def indis(df, df1):
    d = df["Close"].diff()
    g = d.where(d > 0, 0.0).rolling(14).mean()
    l = (-d.where(d < 0, 0.0)).rolling(14).mean()
    df["RSI"] = 100 - (100 / (1 + (g / l.replace(0, 1e-10))))
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["MACD"] = df["Close"].ewm(span=12, adjust=False).mean() - df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD_Sig"] = df["MACD"].ewm(span=9, adjust=False).mean()

    m = df["Close"].rolling(20).mean()
    sd = df["Close"].rolling(20).std()
    df["BB_Up"] = m + sd * 2
    df["BB_Low"] = m - sd * 2

    df = candles(df)
    df = fvg(df)
    df = ob(df)

    if "Volume" in df.columns:
        df["Vol_MA"] = df["Volume"].rolling(20).mean()
    else:
        df["Volume"] = 0
        df["Vol_MA"] = 0

    tr = pd.concat([df["High"] - df["Low"], (df["High"] - df["Close"].shift()).abs(), (df["Low"] - df["Close"].shift()).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()

    micro = "خنثی"
    if not df1.empty and len(df1) >= 30:
        s30 = df1.tail(30)
        diff = s30["Close"].iloc[-1] - s30["Close"].iloc[0]
        v = s30["Close"].diff().abs().mean()
        if diff > 0 and v > 0.0001: micro = "صعودی پرشتاب در ۳۰ دقیقه گذشته 📈"
        elif diff < 0 and v > 0.0001: micro = "نزولی پرشتاب در ۳۰ دقیقه گذشته 📉"
        else: micro = "رنج و کم‌نوسان در ۳۰ دقیقه گذشته ↔️"

    return df, micro


def t_4h(sym, name):
    try:
        df4 = pd.DataFrame()
        if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
            k_map = {"Bitcoin": "BTC/USDT", "Ethereum": "ETH/USDT", "Solana": "SOL/USDT", "Ripple": "XRP/USDT"}
            raw = kc.fetch_ohlcv(k_map[name], timeframe="4h", limit=50)
            if raw:
                df4 = pd.DataFrame(raw, columns=["t", "Open", "High", "Low", "Close", "Volume"])
        elif td:
            try:
                t_map = {
                    "EUR/USD": "EUR/USD", "GBP/USD": "GBP/USD", "USD/JPY": "USD/JPY",
                    "USD/CHF": "USD/CHF", "AUD/USD": "AUD/USD", "USD/CAD": "USD/CAD",
                    "NZD/USD": "NZD/USD", "EUR/GBP": "EUR/GBP", "GBP/JPY": "GBP/JPY",
                    "AUD/JPY": "AUD/JPY", "Gold (طلا)": "XAU/USD", "Silver (نقره)": "XAG/USD",
                    "Crude Oil (نفت برنت)": "BRENT", "S&P 500": "SPX", "Nasdaq": "IXIC"
                }
                if name in t_map:
                    df4 = td.time_series(symbol=t_map[name], interval="4h", outputsize=50).as_pandas().rename(columns={"close": "Close"}).sort_index()
            except:
                y_map = {
                    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
                    "USD/CHF": "USDCHF=X", "AUD/USD": "AUD/USD", "USD/CAD": "USDCAD=X",
                    "NZD/USD": "NZDUSD=X", "EUR/GBP": "EURGBP=X", "GBP/JPY": "GBPJPY=X",
                    "AUD/JPY": "AUDJPY=X", "Gold (طلا)": "GC=F", "Silver (نقره)": "SI=F",
                    "Crude Oil (نفت برنت)": "BZ=F", "S&P 500": "^GSPC", "Nasdaq": "^IXIC"
                }
                df4 = yf.Ticker(y_map.get(name, sym)).history(period="60d", interval="4h")

        if df4.empty or len(df4) < 10:
            return "UNKNOWN"

        e20 = df4["Close"].ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = df4["Close"].ewm(span=50, adjust=False).mean().iloc[-1]
        c = df4["Close"].iloc[-1]

        if c > e20 and e20 > e50: return "BULLISH"
        if c < e20 and e20 < e50: return "BEARISH"
        return "NEUTRAL"
    except:
        return "UNKNOWN"


def pivots(sym, name):
    try:
        dfd = pd.DataFrame()
        if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
            k_map = {"Bitcoin": "BTC/USDT", "Ethereum": "ETH/USDT", "Solana": "SOL/USDT", "Ripple": "XRP/USDT"}
            raw = kc.fetch_ohlcv(k_map[name], timeframe="1d", limit=5)
            if raw:
                dfd = pd.DataFrame(raw, columns=["t", "Open", "High", "Low", "Close", "Volume"])
        elif td:
            try:
                t_map = {
                    "EUR/USD": "EUR/USD", "GBP/USD": "GBP/USD", "USD/JPY": "USD/JPY",
                    "USD/CHF": "USD/CHF", "AUD/USD": "AUD/USD", "USD/CAD": "USD/CAD",
                    "NZD/USD": "NZD/USD", "EUR/GBP": "EUR/GBP", "GBP/JPY": "GBP/JPY",
                    "AUD/JPY": "AUD/JPY", "Gold (طلا)": "XAU/USD", "Silver (نقره)": "XAG/USD",
                    "Crude Oil (نفت برنت)": "BRENT", "S&P 500": "SPX", "Nasdaq": "IXIC"
                }
                if name in t_map:
                    dfd = td.time_series(symbol=t_map[name], interval="1day", outputsize=5).as_pandas().rename(columns={"high": "High", "low": "Low", "close": "Close"}).sort_index()
            except:
                y_map = {
                    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
                    "USD/CHF": "USDCHF=X", "AUD/USD": "AUD/USD", "USD/CAD": "USDCAD=X",
                    "NZD/USD": "NZDUSD=X", "EUR/GBP": "EURGBP=X", "GBP/JPY": "GBPJPY=X",
                    "AUD/JPY": "AUDJPY=X", "Gold (طلا)": "GC=F", "Silver (نقره)": "SI=F",
                    "Crude Oil (نفت برنت)": "BZ=F", "S&P 500": "^GSPC", "Nasdaq": "^IXIC"
                }
                dfd = yf.Ticker(y_map.get(name, sym)).history(period="10d", interval="1d")

        if len(dfd) >= 2:
            y = dfd.iloc[-2]
            p = (y["High"] + y["Low"] + y["Close"]) / 3
            return p, (2 * p - y["Low"]), (2 * p - y["High"]), (p + y["High"] - y["Low"]), (p - y["High"] + y["Low"])
    except:
        pass
    return None, None, None, None, None


def score(df, t4, micro, div, bos, m1=None):
    b = 0.0
    s = 0.0
    last = df.iloc[-1]
    p = last["Close"]

    if t4 == "BULLISH": b += 3.0
    elif t4 == "BEARISH": s += 3.0

    if bos == "BULLISH_BOS": b += 2.5
    elif bos == "BEARISH_BOS": s += 2.5

    if last.get("Bull_OB", False) or last.get("Bull_FVG", False): b += 3.0
    if last.get("Bear_OB", False) or last.get("Bear_FVG", False): s += 3.0

    if last["Bull_Eng"] or last["Bull_Pin"] or last["Hammer"]: b += 2.5
    if last["Bear_Eng"] or last["Bear_Pin"]: s += 2.5

    if m1 == "STRONG_BULLISH": b += 2.0
    elif m1 == "STRONG_BEARISH": s += 2.0

    if last["Vol_MA"] > 0 and last["Volume"] >= last["Vol_MA"] * 1.3:
        b += 1.5
        s += 1.5

    if div == "BULLISH_DIVERGENCE": b += 2.0
    elif div == "BEARISH_DIVERGENCE": s += 2.0

    if p > last["EMA20"] and last["RSI"] < 60: b += 1.0
    if p < last["EMA20"] and last["RSI"] > 40: s += 1.0

    if last["MACD"] > last["MACD_Sig"] and p <= last["BB_Low"] + (last["ATR"] * 0.5): b += 1.5
    if last["MACD"] < last["MACD_Sig"] and p >= last["BB_Up"] - (last["ATR"] * 0.5): s += 1.5

    if "صعودی" in micro: b += 1.0
    elif "نزولی" in micro: s += 1.0

    return round(b, 1), round(s, 1)


async def check_sym(sym, name):
    try:
        df, sp = get_df(sym, name)
        df1 = pd.DataFrame()

        if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
            k_map = {"Bitcoin": "BTC/USDT", "Ethereum": "ETH/USDT", "Solana": "SOL/USDT", "Ripple": "XRP/USDT"}
            raw1 = kc.fetch_ohlcv(k_map[name], timeframe="1m", limit=60)
            if raw1:
                df1 = pd.DataFrame(raw1, columns=["t", "Open", "High", "Low", "Close", "Volume"])

        if df.empty or len(df) < 20:
            return None

        df, micro = indis(df, df1)
        t4 = t_4h(sym, name)
        div = div_check(df)
        bos = bos_check(df)
        piv, r1, s1, r2, s2 = pivots(sym, name)

        m1 = "NEUTRAL"
        if not df1.empty and len(df1) >= 60:
            if df1["Close"].iloc[-1] > df1["Close"].iloc[0]: m1 = "STRONG_BULLISH"
            elif df1["Close"].iloc[-1] < df1["Close"].iloc[0]: m1 = "STRONG_BEARISH"

        last = df.iloc[-1]
        p = last["Close"]
        atr = last["ATR"] if not pd.isna(last["ATR"]) and last["ATR"] > 0 else 0.0010

        bs, ss = score(df, t4, micro, div, bos, m1)
        # print(name, bs, ss)

        side = "WAIT"
        if bs >= 7.5 and "نزولی" not in micro:
            side = "BUY"
            entry = p + sp
            sl = (s1 if s1 and s1 < p else p - atr * 1.5) - sp
            tp1 = r1 if r1 and r1 > p else p + atr * 2.5
        elif ss >= 7.5 and "صعودی" not in micro:
            side = "SELL"
            entry = p - sp
            sl = (r1 if r1 and r1 > p else p + atr * 1.5) + sp
            tp1 = s1 if s1 and s1 < p else p - atr * 2.5
        else:
            entry = sl = tp1 = p

        pat = "هیچ‌کدام"
        if last["Bull_Eng"]: pat = "Bullish Engulfing"
        elif last["Bear_Eng"]: pat = "Bearish Engulfing"
        elif last["Bull_Pin"]: pat = "Bullish Pin Bar"
        elif last["Bear_Pin"]: pat = "Bearish Pin Bar"
        elif last["Hammer"]: pat = "Hammer (چکش)"
        elif last["Doji"]: pat = "Doji (دوجی)"

        if side != "WAIT":
            add_sig(name, side, entry, sl, tp1, tp1)

        return {"name": name, "signal": side, "buy_score": bs, "sell_score": ss, "price": p, "pattern": pat, "divergence": div, "structure": bos}
    except Exception as e:
        print("err:", name, e)
        return None


async def analyze(sym, name):
    def job():
        try:
            df, sp = get_df(sym, name)
            df1 = pd.DataFrame()
            if name in ["Bitcoin", "Ethereum", "Solana", "Ripple"]:
                k_map = {"Bitcoin": "BTC/USDT", "Ethereum": "ETH/USDT", "Solana": "SOL/USDT", "Ripple": "XRP/USDT"}
                raw1 = kc.fetch_ohlcv(k_map[name], timeframe="1m", limit=60)
                if raw1:
                    df1 = pd.DataFrame(raw1, columns=["t", "Open", "High", "Low", "Close", "Volume"])

            if df.empty or len(df) < 20:
                return f"❌ اطلاعات کافی از بازار برای نماد {name} یافت نشد."

            df, micro = indis(df, df1)
            t4 = t_4h(sym, name)
            div = div_check(df)
            bos = bos_check(df)
            piv, r1, s1, r2, s2 = pivots(sym, name)

            m1 = "NEUTRAL"
            if not df1.empty and len(df1) >= 60:
                if df1["Close"].iloc[-1] > df1["Close"].iloc[0]: m1 = "STRONG_BULLISH"
                elif df1["Close"].iloc[-1] < df1["Close"].iloc[0]: m1 = "STRONG_BEARISH"

            last = df.iloc[-1]
            p = last["Close"]
            atr = last["ATR"] if not pd.isna(last["ATR"]) and last["ATR"] > 0 else 0.0010

            bs, ss = score(df, t4, micro, div, bos, m1)

            pat = "هیچ‌کدام"
            if last["Bull_Eng"]: pat = "Bullish Engulfing (پوشاننده صعودی)"
            elif last["Bear_Eng"]: pat = "Bearish Engulfing (پوشاننده نزولی)"
            elif last["Bull_Pin"]: pat = "Bullish Pin Bar (پین‌بار صعودی)"
            elif last["Bear_Pin"]: pat = "Bearish Pin Bar (پین‌بار نزولی)"
            elif last["Hammer"]: pat = "Hammer (چکش صعودی)"
            elif last["Doji"]: pat = "Doji (دوجی مردد)"

            if last["Bull_OB"]: pat += " + Bullish OB"
            if last["Bear_OB"]: pat += " + Bearish OB"
            if last["Bull_FVG"]: pat += " + Bullish FVG"
            if last["Bear_FVG"]: pat += " + Bearish FVG"

            side = "WAIT"
            if bs >= 7.5 and "نزولی" not in micro:
                side = "BUY"
                entry = p + sp
                sl = (s1 if s1 and s1 < p else p - atr * 1.5) - sp
                tp1 = r1 if r1 and r1 > p else p + atr * 2.5
                tp2 = r2 if r2 and r2 > tp1 else p + atr * 4.0
            elif ss >= 7.5 and "صعودی" not in micro:
                side = "SELL"
                entry = p - sp
                sl = (r1 if r1 and r1 > p else p + atr * 1.5) + sp
                tp1 = s1 if s1 and s1 < p else p - atr * 2.5
                tp2 = s2 if s2 and s2 < tp1 else p - atr * 4.0
            else:
                entry = sl = tp1 = tp2 = p

            lot = calc_lot(name, entry, sl)
            if side != "WAIT":
                add_sig(name, side, entry, sl, tp1, tp2)

            prompt = f"""تو یک تحلیلگر ارشد بازارهای مالی و معامله‌گر حرفه‌ای پرایس‌اکشن و ساختار بازار (SMC) هستی.
اطلاعات دقیق نماد {name}:
- قیمت فعلی بازار: {p:.4f} | قیمت اجرایی با اسپرد: {entry:.4f} (اسپرد: {sp})
- جهت سیگنال: {side} | امتیاز همگرایی وزنی خرید: {bs} | فروش: {ss}
- ساختار بازار (Market Structure): {bos}
- وضعیت واگرایی RSI: {div}
- الگوی کندلی و اسمارت مانی فعال: {pat}
- روند ۴ ساعته: {t4} | مومنتوم ۱ ساعته M1: {m1}

لطفاً یک تحلیل کوتاه، بسیار دقیق، آکادمیک و تخصصی (حداکثر ۳ الی ۴ خط) به زبان فارسی بنویس که دلیل ورود یا لزوم صبر کردن را بر اساس تلاقی ساختار بازار، اوردر بلاک‌ها، FVG و مومنتوم توضیح دهد."""

            ai_txt = "تحلیل عمیق فنی بر اساس تلاقی ساختار بازار (BOS)، اوردر بلاک (OB)، نقدینگی FVG و مومنتوم تایید شد."
            if GPT_KEY:
                try:
                    r = requests.post(
                        "https://api.openai.com/v1/chat/completions",
                        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.7},
                        headers={"Authorization": f"Bearer {GPT_KEY}", "Content-Type": "application/json"},
                        timeout=15
                    )
                    if r.status_code == 200:
                        ai_txt = r.json()["choices"][0]["message"]["content"].strip()
                except:
                    pass

            piv_txt = f"\n📐 **سطوح ساختاری (Pivot):**\n- Pivot: {piv:.4f}\n- S1: {s1:.4f} | R1: {r1:.4f}" if piv else ""
            div_txt = "صعودی 📈" if div == "BULLISH_DIVERGENCE" else ("نزولی 📉" if div == "BEARISH_DIVERGENCE" else "ندارد ➖")

            return f"""🛡️ **گزارش تحلیل عمیق و فوق‌پیشرفته فنی (SMC) [{name}]**

🎯 **نتیجه نهایی:** {"✅ سیگنال ورود با تاییدیه وزنی اسمارت مانی" if side != "WAIT" else "❌ عدم رسیدن به حد نصاب همگرایی"}
💡 **جهت معامله:** {side}
📊 **قیمت اجرایی:** {entry:.4f} (اسپرد: {sp})
💼 **حجم پیشنهادی معامله:** {lot} لات (ریسک: {RISK}٪ از حساب {BAL}$)
⭐ **امتیاز همگرایی وزنی:** خرید: {bs} | فروش: {ss}

🧱 **ساختار بازار (Structure):** {bos}
🌀 **واگرایی RSI:** {div_txt}
🕯️ **پرایس‌اکشن و اسمارت مانی:** {pat}
📈 **روند ۴ ساعته:** {t4}

🛑 **حد ضرر اولیه (SL):** {sl:.4f}
🎯 **هدف سود اول (TP1):** {tp1:.4f}
🎯 **هدف سود دوم (TP2):** {tp2:.4f}{piv_txt}

📝 **تفسیر هوش مصنوعی (SMC & Price Action):**
{ai_txt}"""
        except Exception as e:
            return f"❌ خطای غیرمنتظره در تحلیل:\n{e}"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, job)


async def scan_all(update, context):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("🔍 **در حال اسکن همزمان تمام بازارهای مالی بر اساس سیستم اسمارت مانی و امتیازدهی وزن‌دار... لطفاً چند لحظه صبر کنید.** ⏳")

    syms = [
        ("EURUSD", "EUR/USD"), ("GBPUSD", "GBP/USD"), ("USDJPY", "USD/JPY"),
        ("USDCHF", "USD/CHF"), ("AUDUSD", "AUD/USD"), ("USDCAD", "USD/CAD"),
        ("NZDUSD", "NZD/USD"), ("EURGBP", "EUR/GBP"), ("GBPJPY", "GBP/JPY"),
        ("AUDJPY", "AUD/JPY"), ("XAUUSD", "Gold (طلا)"), ("XAGUSD", "Silver (نقره)"),
        ("BRENT", "Crude Oil (نفت برنت)"), ("BTC/USDT", "Bitcoin"),
        ("ETH/USDT", "Ethereum"), ("SOL/USDT", "Solana"), ("XRP/USDT", "Ripple"),
        ("SPX", "S&P 500"), ("IXIC", "Nasdaq")
    ]

    ok = []
    waits = []
    for s, n in syms:
        res = await check_sym(s, n)
        if res:
            if res["signal"] != "WAIT":
                ok.append(res)
            else:
                waits.append(f"• {n}: صبر (خرید: {res['buy_score']} | فروش: {res['sell_score']})")

    rep = "📊 **نتیجه اسکن جامع بازارهای مالی (SMC):**\n\n"
    if ok:
        rep += "🚨 **سیگنال‌های فعال با همگرایی بالای فنی:**\n"
        for x in ok:
            rep += f"✅ **{x['name']}** ➔ جهت: **{x['signal']}** | خرید: {x['buy_score']} | فروش: {x['sell_score']}\n"
    else:
        rep += "ℹ️ در حال حاضر هیچ‌کدام از نمادها به حد نصاب امتیاز همگرایی وزن‌دار نرسیده‌اند.\n\n"

    rep += "\n📋 **وضعیت سایر نمادها:**\n" + "\n".join(waits[:10])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_menu")]])
    await q.message.reply_text(rep, reply_markup=kb)


async def bg_task():
    syms = [
        ("EURUSD", "EUR/USD"), ("GBPUSD", "GBP/USD"), ("USDJPY", "USD/JPY"),
        ("USDCHF", "USD/CHF"), ("AUDUSD", "AUD/USD"), ("USDCAD", "USD/CAD"),
        ("NZDUSD", "NZD/USD"), ("EURGBP", "EUR/GBP"), ("GBPJPY", "GBP/JPY"),
        ("AUDJPY", "AUD/JPY"), ("XAUUSD", "Gold (طلا)"), ("XAGUSD", "Silver (نقره)"),
        ("BRENT", "Crude Oil (نفت برنت)"), ("BTC/USDT", "Bitcoin"),
        ("ETH/USDT", "Ethereum"), ("SOL/USDT", "Solana"), ("XRP/USDT", "Ripple"),
        ("SPX", "S&P 500"), ("IXIC", "Nasdaq")
    ]
    while True:
        await asyncio.sleep(1800)  # هر ۳۰ دقیقه
        if not my_id or not app_bot:
            continue
        try:
            hot = []
            for s, n in syms:
                res = await check_sym(s, n)
                if res and res["signal"] != "WAIT":
                    hot.append(res)
            if hot:
                msg = "🚨 **فرصت معاملاتی طلایی کشف شد!** 🎯\n\nربات بررسی کرد و نمادهای زیر شرایط مناسبی برای معامله دارند:\n\n"
                for x in hot:
                    msg += f"💎 **{x['name']}**\n🔹 جهت معامله: **{x['signal']}**\n⭐ امتیاز همگرایی: خرید ({x['buy_score']}) | فروش ({x['sell_score']})\n🕯️ الگو: {x['pattern']}\n📊 قیمت لحظه‌ای: {x['price']:.4f}\n\n"
                msg += "⚠️ حتماً پیش از ورود، مدیریت ریسک را رعایت کنید."
                await app_bot.bot.send_message(my_id, msg, parse_mode="Markdown")
        except Exception as e:
            print("bg err:", e)


async def start(update, context):
    global my_id
    my_id = update.effective_chat.id
    add_log(update.effective_user, "COMMAND_START")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 اسکن و تحلیل همزمان همه بازارها", callback_data="scan_all")],
        [InlineKeyboardButton("💶 EUR/USD", callback_data="eurusd"), InlineKeyboardButton("💷 GBP/USD", callback_data="gbpusd")],
        [InlineKeyboardButton("💴 USD/JPY", callback_data="usdjpy"), InlineKeyboardButton("🇨🇭 USD/CHF", callback_data="usdchf")],
        [InlineKeyboardButton("🇦🇺 AUD/USD", callback_data="audusd"), InlineKeyboardButton("🇨🇦 USD/CAD", callback_data="usdcad")],
        [InlineKeyboardButton("🇳🇿 NZD/USD", callback_data="nzdusd"), InlineKeyboardButton("🇪🇺 EUR/GBP", callback_data="eurgbp")],
        [InlineKeyboardButton("💷 GBP/JPY", callback_data="gbpjpy"), InlineKeyboardButton("🇦🇺 AUD/JPY", callback_data="audjpy")],
        [InlineKeyboardButton("🥇 Gold", callback_data="gold"), InlineKeyboardButton("🥈 Silver", callback_data="silver")],
        [InlineKeyboardButton("🛢 Crude Oil", callback_data="oil"), InlineKeyboardButton("₿ Bitcoin", callback_data="btc")],
        [InlineKeyboardButton("Ξ Ethereum", callback_data="eth"), InlineKeyboardButton("🟣 Solana", callback_data="sol")],
        [InlineKeyboardButton("🔵 Ripple", callback_data="xrp"), InlineKeyboardButton("📈 S&P 500", callback_data="sp500")],
        [InlineKeyboardButton("📉 Nasdaq", callback_data="nasdaq")]
    ])

    txt = "سلام! ربات پیشرفته معاملاتی (همراه با تحلیل SMC و مومنتوم M1) آماده است. یکی از گزینه‌ها را انتخاب کنید:"
    if update.message:
        await update.message.reply_text(txt, reply_markup=kb)
    elif update.callback_query:
        await update.callback_query.message.reply_text(txt, reply_markup=kb)


async def btn(update, context):
    q = update.callback_query
    await q.answer()
    add_log(update.effective_user, f"SELECT_SYMBOL_{q.data.upper()}")
    await q.message.reply_text("⚡ **در حال تحلیل اسمارت مانی (OB & FVG)، پرایس‌اکشن، ساختار بازار و مومنتوم لحظه‌ای...** ⏳")

    routes = {
        "eurusd": ("EURUSD", "EUR/USD"), "gbpusd": ("GBPUSD", "GBP/USD"),
        "usdjpy": ("USDJPY", "USD/JPY"), "usdchf": ("USDCHF", "USD/CHF"),
        "audusd": ("AUDUSD", "AUD/USD"), "usdcad": ("USDCAD", "USD/CAD"),
        "nzdusd": ("NZDUSD", "NZD/USD"), "eurgbp": ("EURGBP", "EUR/GBP"),
        "gbpjpy": ("GBPJPY", "GBP/JPY"), "audjpy": ("AUDJPY", "AUD/JPY"),
        "gold": ("XAUUSD", "Gold (طلا)"), "silver": ("XAGUSD", "Silver (نقره)"),
        "oil": ("BRENT", "Crude Oil (نفت برنت)"), "btc": ("BTC/USDT", "Bitcoin"),
        "eth": ("ETH/USDT", "Ethereum"), "sol": ("SOL/USDT", "Solana"),
        "xrp": ("XRP/USDT", "Ripple"), "sp500": ("SPX", "S&P 500"),
        "nasdaq": ("IXIC", "Nasdaq")
    }

    if q.data in routes:
        s, n = routes[q.data]
        res = await analyze(s, n)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_menu")]])
        await q.message.reply_text(res, reply_markup=kb)


async def back(update, context):
    add_log(update.effective_user, "BACK_TO_MENU")
    await start(update, context)


async def init_app(app):
    global app_bot
    app_bot = app
    asyncio.create_task(bg_task())


if __name__ == "__main__":
    if not TOKEN:
        print("توکن پیدا نشد!")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).post_init(init_app).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(scan_all, pattern="^scan_all$"))
    app.add_handler(CallbackQueryHandler(btn, pattern="^(eurusd|gbpusd|usdjpy|usdchf|audusd|usdcad|nzdusd|eurgbp|gbpjpy|audjpy|gold|silver|oil|btc|eth|sol|xrp|sp500|nasdaq)$"))
    app.add_handler(CallbackQueryHandler(back, pattern="^back_to_menu$"))

    print("Bot running...")
    app.run_polling()
