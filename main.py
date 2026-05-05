import os, time, json, logging, warnings, threading
import pandas as pd
import numpy as np
import ccxt
import yfinance as yf
import requests
from datetime import datetime
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler

warnings.filterwarnings("ignore")
load_dotenv()

ALPHA_KEY     = os.getenv("ALPHA_VANTAGE_KEY", "YOUR_KEY_HERE")
ACCOUNT_SIZE  = 50000
MAX_RISK_PCT  = 0.01
MAX_POSITIONS = 9999
SCAN_INTERVAL  = 60
TAKE_PROFIT_QUICK = 0.05   # quick flip target +5%
TAKE_PROFIT_LONG  = 0.15   # long swing target +15%
STOP_LOSS_PCT     = 0.025  # stop loss -2.5%
TAKE_PROFIT_1     = 0.05   # alias
TAKE_PROFIT_2     = 0.08   # alias
BASE_DIR      = os.path.expanduser("~/trading_bot")
LOG_FILE      = BASE_DIR + "/trades.csv"
PERF_FILE     = BASE_DIR + "/performance.json"
BOT_LOG       = BASE_DIR + "/bot.log"
PORT          = int(os.getenv("PORT", 8888))
os.makedirs(BASE_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(BOT_LOG), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

CRYPTO  = [
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "ADA/USDT","AVAX/USDT","DOGE/USDT","LINK/USDT","DOT/USDT",
    "MATIC/USDT","UNI/USDT","ATOM/USDT","LTC/USDT","NEAR/USDT",
    "APT/USDT","ARB/USDT","OP/USDT","INJ/USDT","SUI/USDT",
    "TRX/USDT","TON/USDT","SHIB/USDT","BCH/USDT","FIL/USDT",
    "HBAR/USDT","IMX/USDT","SAND/USDT","MANA/USDT","CRV/USDT",
]
STOCKS  = [
    "AAPL","MSFT","NVDA","AMD","TSLA","AMZN","GOOGL","META","ORCL",
    "AVGO","INTC","QCOM","MU","PLTR","CRM","NOW","NFLX","ADBE","UBER",
    "COIN","V","MA","PYPL","HOOD",
    "LLY","PFE","JNJ","ISRG","DXCM",
    "XOM","CVX","COP",
    "WMT","HD","MCD","SBUX",
    "SNOW","NET","CRWD","DDOG",
    "AMAT","LRCX","MRVL",
    "DIS","SPOT","RBLX",
    "F","GM",
]
ETFS    = [
    # S&P 500
    "SPY",   # S&P 500 ETF - tracks all 500 companies
    "SPXL",  # 3x leveraged S&P 500 bull
    "SPXS",  # 3x leveraged S&P 500 bear
    "VOO",   # Vanguard S&P 500
    "IVV",   # iShares S&P 500
    # NASDAQ
    "QQQ",   # NASDAQ 100 ETF
    "TQQQ",  # 3x leveraged NASDAQ bull
    "SQQQ",  # 3x leveraged NASDAQ bear
    "QQQM",  # NASDAQ mini
    # Small/Mid cap
    "IWM",   # Russell 2000
    "TNA",   # 3x Russell bull
    # Commodities
    "GLD","SLV","USO","UNG",
    # Bonds/Volatility
    "TLT","VXX","UVXY",
    # Sector ETFs
    "XLK",   # Technology
    "XLF",   # Financials
    "XLE",   # Energy
    "XLV",   # Healthcare
    "ARKK",  # ARK Innovation
]
FUTURES = [
    "ES=F",   # S&P 500 futures
    "NQ=F",   # NASDAQ 100 futures
    "YM=F",   # Dow Jones futures
    "RTY=F",  # Russell 2000 futures
    "GC=F",   # Gold futures
    "CL=F",   # Crude oil futures
    "SI=F",   # Silver futures
    "ZB=F",   # 30yr Bond futures
]
FOREX   = ["EURUSD=X","GBPUSD=X","USDJPY=X","AUDUSD=X","USDCAD=X"]

STATE = {
    "last_scan": "Not yet", "scan_count": 0, "scanning": False,
    "positions": {}, "results": [], "stats": {}, "trade_log": [],
    "weights": {}, "cash": ACCOUNT_SIZE, "invested": 0,
    "equity": ACCOUNT_SIZE, "pnl": 0, "pnl_pct": 0,
    "current_prices": {},
}


# ─────────────────────────────────────────────────────────────
#  LEARNING ENGINE
# ─────────────────────────────────────────────────────────────
class Learner:
    def __init__(self):
        self.w  = {"rsi": 1.0, "macd": 1.0, "momentum": 1.0,
                   "volume": 1.0, "sentiment": 1.0, "earnings": 1.0}
        self.sp = {}
        self._load()

    def _load(self):
        try:
            with open(PERF_FILE) as f:
                d = json.load(f)
                self.w  = d.get("weights", self.w)
                self.sp = d.get("symbol_perf", {})
        except:
            pass
        STATE["weights"] = self.w

    def save(self):
        with open(PERF_FILE, "w") as f:
            json.dump({"weights": self.w, "symbol_perf": self.sp,
                       "updated": str(datetime.now())}, f, indent=2)
        STATE["weights"] = self.w

    def update(self, sym, factors, pnl):
        lr = 0.05
        for k in factors:
            if k in self.w:
                if pnl > 0:
                    self.w[k] = min(2.0, self.w[k] + lr)
                else:
                    self.w[k] = max(0.1, self.w[k] - lr)
        if sym not in self.sp:
            self.sp[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
        self.sp[sym]["trades"] += 1
        self.sp[sym]["pnl"] = round(self.sp[sym]["pnl"] + pnl, 2)
        if pnl > 0:
            self.sp[sym]["wins"] += 1
        self.save()


# ─────────────────────────────────────────────────────────────
#  TRADE LOGGER
# ─────────────────────────────────────────────────────────────
class TradeLog:
    def __init__(self):
        if not os.path.exists(LOG_FILE):
            pd.DataFrame(columns=[
                "timestamp", "symbol", "market", "side", "entry", "stop",
                "target1", "target2", "shares", "capital", "status",
                "exit_price", "pnl", "score", "style", "reason"
            ]).to_csv(LOG_FILE, index=False)
        self._dedup()

    def _dedup(self):
        try:
            df = pd.read_csv(LOG_FILE)
            c  = df[df.status == "CLOSED"]
            o  = df[df.status == "OPEN"].drop_duplicates("symbol", keep="last")
            pd.concat([c, o]).sort_values("timestamp").reset_index(drop=True).to_csv(LOG_FILE, index=False)
        except:
            pass

    def cash_invested(self):
        try:
            df  = pd.read_csv(LOG_FILE)
            inv = df[df.status == "OPEN"].drop_duplicates("symbol", keep="last")["capital"].astype(float).sum()
            return round(ACCOUNT_SIZE - inv, 2), round(inv, 2)
        except:
            return ACCOUNT_SIZE, 0

    def add(self, trade):
        df = pd.read_csv(LOG_FILE)
        df = pd.concat([df, pd.DataFrame([trade])], ignore_index=True)
        df.to_csv(LOG_FILE, index=False)
        self.sync()

    def close(self, sym, price):
        df   = pd.read_csv(LOG_FILE)
        mask = (df.symbol == sym) & (df.status == "OPEN")
        if not mask.any():
            return 0
        i   = df[mask].index[-1]
        e   = float(df.loc[i, "entry"])
        sh  = float(df.loc[i, "shares"])
        side = df.loc[i, "side"]
        pnl = round((price - e) * sh if side == "BUY" else (e - price) * sh, 2)
        df.loc[i, "exit_price"] = price
        df.loc[i, "pnl"]        = pnl
        df.loc[i, "status"]     = "CLOSED"
        df.to_csv(LOG_FILE, index=False)
        self.sync()
        log.info("CLOSED %s @ %s  PnL=%.2f", sym, price, pnl)
        return pnl

    def sync(self):
        try:
            df   = pd.read_csv(LOG_FILE)
            c    = df[df.status == "CLOSED"]
            o    = df[df.status == "OPEN"].drop_duplicates("symbol", keep="last")
            inv  = round(o["capital"].astype(float).sum(), 2)
            pnl  = round(c["pnl"].astype(float).sum(), 2) if len(c) > 0 else 0
            cash = round(ACCOUNT_SIZE - inv, 2)
            wins = c[c["pnl"].astype(float) > 0]
            loss = c[c["pnl"].astype(float) <= 0]
            wr   = round(len(wins) / len(c) * 100, 1) if len(c) > 0 else 0
            log_dedup = df.drop_duplicates(
                subset=["symbol", "entry", "side"], keep="last"
            ).tail(30).to_dict("records")
            STATE.update({
                "trade_log": log_dedup,
                "invested":  inv,
                "cash":      cash,
                "equity":    round(ACCOUNT_SIZE + pnl, 2),
                "pnl":       pnl,
                "pnl_pct":   round(pnl / ACCOUNT_SIZE * 100, 2),
                "stats": {
                    "total":    len(c),
                    "wins":     len(wins),
                    "losses":   len(loss),
                    "win_rate": wr,
                    "total_pnl": pnl,
                    "best":     round(c["pnl"].astype(float).max(), 2) if len(c) > 0 else 0,
                    "worst":    round(c["pnl"].astype(float).min(), 2) if len(c) > 0 else 0,
                    "avg_win":  round(wins["pnl"].astype(float).mean(), 2) if len(wins) > 0 else 0,
                    "avg_loss": round(loss["pnl"].astype(float).mean(), 2) if len(loss) > 0 else 0,
                }
            })
        except Exception as ex:
            log.error("sync error: %s", ex)


# ─────────────────────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────────────────────
def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = -d.clip(upper=0).rolling(p).mean()
    return 100 - (100 / (1 + g / l))


def calc_macd(s):
    m   = s.ewm(span=12).mean() - s.ewm(span=26).mean()
    sig = m.ewm(span=9).mean()
    return m, sig, m - sig


def calc_bb(s, p=20):
    sma = s.rolling(p).mean()
    std = s.rolling(p).std()
    return sma + 2 * std, sma, sma - 2 * std


def calc_atr(df, p=14):
    hl = df.high - df.low
    hc = (df.high - df.close.shift()).abs()
    lc = (df.low  - df.close.shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(p).mean()


# ─────────────────────────────────────────────────────────────
#  DATA FETCHERS
# ─────────────────────────────────────────────────────────────
def get_yf(sym, period="3mo"):
    try:
        t  = yf.Ticker(sym)
        df = t.history(period=period)
        if df.empty:
            return None, None, []
        df.columns = [c.lower() for c in df.columns]
        news  = t.news or []
        heads = [n.get("content", {}).get("title", "") for n in news[:3]]
        return df, t.info, heads
    except:
        return None, None, []


def get_sentiment(sym):
    try:
        url = ("https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
               "&tickers=" + sym + "&limit=10&apikey=" + ALPHA_KEY)
        r = requests.get(url, timeout=10).json()
        scores = [
            float(ts["ticker_sentiment_score"])
            for item in r.get("feed", [])
            for ts in item.get("ticker_sentiment", [])
            if ts["ticker"] == sym
        ]
        return round(float(np.mean(scores)), 3) if scores else 0.0
    except:
        return 0.0


def get_earnings(sym):
    try:
        url = ("https://www.alphavantage.co/query?function=EARNINGS"
               "&symbol=" + sym + "&apikey=" + ALPHA_KEY)
        q = requests.get(url, timeout=10).json().get("quarterlyEarnings", [])
        if not q:
            return None
        e = q[0]
        return {
            "date":     e.get("reportedDate", "N/A"),
            "reported": float(e.get("reportedEPS", 0) or 0),
            "estimated":float(e.get("estimatedEPS", 0) or 0),
            "surprise": float(e.get("surprisePercentage", 0) or 0),
        }
    except:
        return None


def get_crypto(exchange, sym):
    try:
        raw = exchange.fetch_ohlcv(sym, "1d", limit=100)
        df  = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df.ts = pd.to_datetime(df.ts, unit="ms")
        return df.set_index("ts")
    except:
        return None


# ─────────────────────────────────────────────────────────────
#  SIGNAL SCORING
# ─────────────────────────────────────────────────────────────
def score_signal(rv, mv, sv, hv, m5, m20, price, ub, lb, vol, avgvol, sent, earn, w):
    sc = 0
    fac = []
    rea = []

    if rv < 70:
        sc += w.get("rsi", 1)
    if rv < 55:
        sc += w.get("rsi", 1)
        fac.append("rsi")
        rea.append("RSI %.1f healthy, not overbought" % rv)
    else:
        rea.append("RSI %.1f %s" % (rv, "OVERBOUGHT" if rv >= 70 else "neutral"))

    if mv > sv:
        sc += 2 * w.get("macd", 1)
        fac.append("macd")
        rea.append("MACD bullish crossover (%.3f > %.3f)" % (mv, sv))
    else:
        rea.append("MACD bearish (%.3f < %.3f)" % (mv, sv))

    if hv > 0:
        sc += w.get("macd", 1)
        rea.append("MACD histogram positive (%.3f)" % hv)

    if m5 > 0:
        sc += w.get("momentum", 1)
        fac.append("momentum")
        rea.append("5d momentum +%.2f%%" % m5)
    else:
        rea.append("5d momentum %.2f%%" % m5)

    if m20 > 0:
        sc += w.get("momentum", 1)
        rea.append("20d momentum +%.2f%%" % m20)

    br = ub - lb
    if br > 0:
        bp = (price - lb) / br
        if bp < 0.8:
            sc += 1
            rea.append("Price at %.0f%% of Bollinger band" % (bp * 100))
        else:
            rea.append("Near Bollinger top -- stretched")

    if avgvol > 0 and vol > avgvol * 1.2:
        sc += w.get("volume", 1)
        fac.append("volume")
        rea.append("Volume above average -- conviction")

    if sent > 0.15:
        sc += 2 * w.get("sentiment", 1)
        fac.append("sentiment")
        rea.append("News sentiment POSITIVE (%.3f)" % sent)
    elif sent > 0:
        sc += w.get("sentiment", 1)
        rea.append("News sentiment mildly positive (%.3f)" % sent)
    elif sent < -0.15:
        rea.append("News sentiment NEGATIVE (%.3f)" % sent)

    if earn and earn.get("surprise", 0) > 5:
        sc += w.get("earnings", 1)
        fac.append("earnings")
        rea.append("Earnings beat %.1f%%" % earn["surprise"])

    sc    = round(sc, 2)
    pct   = sc / 12
    label = ("STRONG BUY" if pct >= 0.75 else
             "BUY"        if pct >= 0.40 else
             "WATCH"      if pct >= 0.42 else "SKIP")
    return sc, label, fac, rea


def pick_style(vol_pct, m5):
    if vol_pct > 3 and abs(m5) > 5:
        return "SCALP"
    if vol_pct > 1.5 or abs(m5) > 2:
        return "DAY TRADE"
    return "SWING"


def calc_size(entry, stop, cash):
    r = abs(entry - stop)
    if r == 0:
        return 0, 0, 0
    sh   = int((ACCOUNT_SIZE * MAX_RISK_PCT) / r)
    cost = round(sh * entry, 2)
    # Hard cap: never more than 10% of account per position
    max_cost = ACCOUNT_SIZE * 0.10
    if cost > max_cost:
        sh   = int(max_cost / entry)
        cost = round(sh * entry, 2)
    if cost > cash:
        sh   = int(cash / entry)
        cost = round(sh * entry, 2)
    return sh, cost, round(cost / ACCOUNT_SIZE * 100, 1)


# ─────────────────────────────────────────────────────────────
#  ANALYZE + PRINT
# ─────────────────────────────────────────────────────────────
def analyze(sym, mkt, df, w, cash, sent=0.0, earn=None, heads=None, info=None):
    if df is None or len(df) < 30:
        return None
    try:
        cl  = df["close"]
        vol = (df["volume"] if "volume" in df.columns
               else pd.Series([1e6] * len(df), index=df.index))
        cur  = float(cl.iloc[-1])
        prev = float(cl.iloc[-2])
        chg  = (cur - prev) / prev * 100

        rv        = float(calc_rsi(cl).iloc[-1])
        mv, sv, hv = calc_macd(cl)
        mv = float(mv.iloc[-1]); sv = float(sv.iloc[-1]); hv = float(hv.iloc[-1])
        ub, mb, lb = calc_bb(cl)
        ub = float(ub.iloc[-1]); lb = float(lb.iloc[-1])
        at  = float(calc_atr(df).iloc[-1])
        av  = float(vol.rolling(20).mean().iloc[-1])
        cv  = float(vol.iloc[-1])
        m5  = (cur - float(cl.iloc[-6])) / float(cl.iloc[-6]) * 100
        m20 = (cur - float(cl.iloc[-21])) / float(cl.iloc[-21]) * 100
        sup = float(df["low"].tail(20).min())
        res = float(df["high"].tail(20).max())

        sc, label, fac, rea = score_signal(rv, mv, sv, hv, m5, m20, cur, ub, lb, cv, av, sent, earn, w)
        st  = pick_style(at / cur * 100, m5)
        stp = round(min(sup * 0.98, cur - at), 4)
        ent = round(cur - at * 0.3, 4)
        t1  = round(ent * (1 + TAKE_PROFIT_QUICK), 4)  # quick +5%
        t2  = round(ent * (1 + TAKE_PROFIT_LONG), 4)   # swing +15%
        stp = round(ent * (1 - STOP_LOSS_PCT), 4)       # stop -2.5%
        sh, cost, alloc = calc_size(ent, stp, cash)

        BAR = "=" * 56
        print("\n" + BAR)
        print("  %s  --  %s  --  %s" % (sym, mkt, datetime.now().strftime("%Y-%m-%d %H:%M")))
        print(BAR)
        print("\n  PRICE DATA")
        print("  Current:       $%.4f  (%+.2f%% vs yesterday)" % (cur, chg))
        print("  Support:       $%.4f  |  Resistance: $%.4f" % (sup, res))
        print("  ATR:           $%.4f  |  Vol: {:,.0f} (avg {:,.0f})".format(at, cv, av))

        if info:
            mc  = info.get("marketCap", 0)
            pe  = info.get("trailingPE", 0)
            sec = info.get("sector", "")
            if mc or pe or sec:
                print("\n  FUNDAMENTALS")
                if mc:  print("  Market Cap:    $%.1fB" % (mc / 1e9))
                if pe:  print("  P/E Ratio:     %.1f" % pe)
                if sec: print("  Sector:        %s" % sec)

        rsi_tag  = "OVERBOUGHT" if rv > 70 else "OVERSOLD" if rv < 30 else "NEUTRAL"
        macd_tag = "BULLISH" if mv > sv else "BEARISH"
        print("\n  TECHNICALS")
        print("  RSI (14):      %.1f  [%s]" % (rv, rsi_tag))
        print("  MACD:          %.4f  Sig: %.4f  [%s]" % (mv, sv, macd_tag))
        print("  MACD Hist:     %.4f" % hv)
        print("  Bollinger:     Upper $%.4f  Lower $%.4f" % (ub, lb))
        print("  Momentum 5d:   %+.2f%%  |  20d: %+.2f%%" % (m5, m20))

        sent_tag = "POSITIVE" if sent > 0.15 else "NEGATIVE" if sent < -0.15 else "NEUTRAL"
        print("\n  NEWS SENTIMENT: %.3f  [%s]" % (sent, sent_tag))
        if heads:
            for h in heads[:3]:
                if h:
                    print("  - %s" % h[:72])

        if earn:
            beat = "BEAT" if earn["surprise"] > 0 else "MISS"
            print("\n  EARNINGS (%s): $%.2f vs est $%.2f  (%+.1f%% %s)" % (
                earn["date"], earn["reported"], earn["estimated"], earn["surprise"], beat))

        print("\n  SIGNAL:  %s  (%.1f/12)" % (label, sc))
        for r in rea:
            print("  + %s" % r)

        if label in ("STRONG BUY", "BUY"):
            print("\n  TRADE PLAN")
            print("  Style:  %s" % st)
            print("  Entry:  $%.4f  |  Stop: $%.4f  (risk $%.4f/unit)" % (ent, stp, ent - stp))
            print("  T1:     $%.4f  |  T2: $%.4f" % (t1, t2))
            print("  Units:  %d  |  Capital: $%.2f (%.1f%%)  |  Cash after: $%.2f" % (
                sh, cost, alloc, cash - cost))

        print(BAR)

        return {
            "symbol": sym, "market": mkt,
            "price": round(cur, 4), "change": round(chg, 2),
            "rsi": round(rv, 1), "signal": label, "score": sc, "style": st,
            "entry": ent, "stop": stp, "target1": t1, "target2": t2,
            "shares": sh, "cost": cost, "alloc": alloc,
            "factors": fac, "reasons": rea,
            "reason_str": " | ".join(rea[:3]),
            "sentiment": round(sent, 3),
            "m5": round(m5, 2), "m20": round(m20, 2),
        }
    except Exception as ex:
        log.error("analyze %s: %s", sym, ex)
        return None


# ─────────────────────────────────────────────────────────────
#  POSITION TRACKER
# ─────────────────────────────────────────────────────────────
class Tracker:
    def __init__(self, logger):
        self.pos = {}
        self.log = logger
        try:
            df = pd.read_csv(LOG_FILE)
            for _, r in df[df.status == "OPEN"].drop_duplicates("symbol", keep="last").iterrows():
                self.pos[r["symbol"]] = r.to_dict()
            log.info("Reloaded %d open positions", len(self.pos))
        except:
            pass
        STATE["positions"] = self.pos

    def can_open(self):
        return len(self.pos) < MAX_POSITIONS

    def open(self, r, cash):
        sym  = r["symbol"]
        cost = r["cost"]
        if cost > cash:
            log.info("SKIP %s need $%.2f only $%.2f avail", sym, cost, cash)
            return False
        if cost <= 0 or r["shares"] <= 0:
            return False
        self.pos[sym] = r
        STATE["positions"] = self.pos
        self.log.add({
            "timestamp": str(datetime.now()), "symbol": sym,
            "market": r["market"], "side": "BUY",
            "entry": r["entry"], "stop": r["stop"],
            "target1": r["target1"], "target2": r["target2"],
            "shares": r["shares"], "capital": r["cost"],
            "status": "OPEN", "exit_price": None, "pnl": None,
            "score": r["score"], "style": r["style"],
            "reason": r.get("reason_str", ""),
        })
        BAR = "*" * 56
        print("\n  " + BAR)
        print("  TRADE OPENED: %s  [%s]  [%s]" % (sym, r["market"], r["style"]))
        print("  Score: %.1f/12  |  Entry: $%s  |  Stop: $%s" % (r["score"], r["entry"], r["stop"]))
        print("  T1: $%s  |  T2: $%s" % (r["target1"], r["target2"]))
        print("  Units: %d  |  Capital: $%.2f (%.1f%%)  |  Cash left: $%.2f" % (
            r["shares"], r["cost"], r["alloc"], cash - cost))
        print("  Why:")
        for x in r.get("reasons", [])[:4]:
            print("    + %s" % x)
        print("  " + BAR + "\n")
        return True

    def check_exits(self, prices, learner):
        done = []
        for sym, pos in self.pos.items():
            if sym not in prices:
                continue
            cur = prices[sym]
            try:
                ent = float(pos.get("entry",  0) or 0)
                stp = float(pos.get("stop",   0) or 0)
                tg1 = float(pos.get("target1",0) or 0)
                sh  = float(pos.get("shares", 0) or 0)
            except:
                continue
            gain_pct = (cur - ent) / ent * 100 if ent > 0 else 0
            loss_pct = (ent - cur) / ent * 100 if ent > 0 else 0
            hit = None
            if loss_pct >= 2.5:
                hit = "STOP-LOSS -2.5%"
            elif gain_pct >= 15.0:
                hit = "TARGET +15% SWING HIT"
            elif gain_pct >= 5.0:
                hit = "TARGET +5% QUICK HIT"
            if hit:
                pnl = self.log.close(sym, cur)
                learner.update(sym, pos.get("factors", []), pnl)
                done.append(sym)
                tag = "WIN" if pnl > 0 else "LOSS"
                BAR = "*" * 56
                print("\n  " + BAR)
                print("  TRADE CLOSED: %s  [%s]  [%s]" % (sym, hit, tag))
                print("  Exit: $%s  |  Entry: $%s  |  PnL: $%.2f  |  Move: %+.2f%%" % (cur, ent, pnl, gain_pct if pnl > 0 else -loss_pct))
                print("  " + BAR + "\n")
        for sym in done:
            del self.pos[sym]
        STATE["positions"] = self.pos


# ─────────────────────────────────────────────────────────────
#  DASHBOARD HTML BUILDER
# ─────────────────────────────────────────────────────────────
def build_html():
    s    = STATE
    st   = s.get("stats", {})
    pos  = s.get("positions", {})
    res  = s.get("results", [])
    logs = s.get("trade_log", [])
    wts  = s.get("weights", {})

    try:
        with open(PERF_FILE) as f:
            sp = json.load(f).get("symbol_perf", {})
    except:
        sp = {}

    cash   = s.get("cash", ACCOUNT_SIZE)
    inv    = s.get("invested", 0)
    equity = s.get("equity", ACCOUNT_SIZE)
    pnl    = s.get("pnl", 0)
    pnl_p  = s.get("pnl_pct", 0)
    cp     = round(max(cash, 0) / ACCOUNT_SIZE * 100, 1)
    ip     = round(min(inv, ACCOUNT_SIZE) / ACCOUNT_SIZE * 100, 1)

    def gc(v):
        return "#1D9E75" if float(v or 0) >= 0 else "#E24B4A"

    BADGE_MAP = {
        "CRYPTO":  ("#2a1f00", "#f0a500"),
        "STOCK":   ("#0d2035", "#4a9eff"),
        "ETF":     ("#1a1a2e", "#9b59b6"),
        "FOREX":   ("#0d2d1a", "#2ecc71"),
        "FUTURES": ("#2d1a0d", "#e67e22"),
    }

    def badge(m):
        bg, fg = BADGE_MAP.get(str(m), ("#1a1a1a", "#aaa"))
        return ("<span style='background:%s;color:%s;"
                "padding:2px 7px;border-radius:4px;"
                "font-size:10px;font-weight:500'>%s</span>" % (bg, fg, m))

    # open position rows
    pr = ""
    for sym, t in pos.items():
        try:
            e    = float(t.get("entry",   0) or 0)
            stp  = float(t.get("stop",    0) or 0)
            tg1  = float(t.get("target1", 0) or 0)
            tg2  = float(t.get("target2", 0) or 0)
            sh   = float(t.get("shares",  0) or 0)
            cap  = float(t.get("capital", 0) or 0)
            risk = round((e - stp) * sh, 2)
            rr   = round((tg1 - e) / (e - stp), 1) if (e - stp) > 0 else 0
            capp = round(cap / ACCOUNT_SIZE * 100, 1)
            pr += "<tr>"
            # Live P&L calculation
            cur_prices = s.get("current_prices", {})
            cur_price  = cur_prices.get(sym, 0)
            if cur_price and sh > 0:
                live_pnl     = round((cur_price - e) * sh, 2)
                live_pnl_pct = round((cur_price - e) / e * 100, 2) if e > 0 else 0
                pnl_col  = "#1D9E75" if live_pnl >= 0 else "#E24B4A"
                pnl_str  = "$%+.2f" % live_pnl
                pnl_p_str= "%+.2f%%" % live_pnl_pct
            else:
                pnl_col   = "#888"
                pnl_str   = "—"
                pnl_p_str = "—"
                cur_price = e
            pr += "<td><b style='color:#fff'>%s</b></td>" % sym
            pr += "<td>%s</td>" % badge(str(t.get("market", "")))
            pr += "<td style='color:#1D9E75;font-weight:500'>OPEN</td>"
            pr += "<td style='color:#fff'>$%s</td>" % e
            pr += "<td style='color:#aaa'>$%.4f</td>" % cur_price
            pr += "<td style='color:%s;font-weight:500'>%s</td>" % (pnl_col, pnl_str)
            pr += "<td style='color:%s;font-weight:500'>%s</td>" % (pnl_col, pnl_p_str)
            pr += "<td style='color:#E24B4A'>$%s</td>" % stp
            pr += "<td style='color:#1D9E75'>$%s</td>" % tg1
            pr += "<td style='color:#378ADD'>$%s</td>" % tg2
            pr += "<td>%d</td>" % int(sh)
            pr += "<td>$%.2f <span style='color:#444;font-size:10px'>(%.1f%%)</span></td>" % (cap, capp)
            pr += "<td style='color:#E24B4A'>-$%s</td>" % risk
            pr += "<td style='color:#888'>%s:1</td>" % rr
            pr += "<td>%s/12</td>" % t.get("score", "")
            pr += "<td style='color:#888'>" + str(t.get("style", "")) + "</td>"
            pr += "</tr>"
        except:
            pass

    # trade history rows
    tr = ""
    for t in reversed(logs[-25:]):
        pv  = t.get("pnl", "")
        try:
            pf = float(pv)
            ps = "$%+.2f" % pf
        except:
            pf = None
            ps = "—"
        sc2 = str(t.get("status", ""))
        stc = "#1D9E75" if sc2 == "OPEN" else (gc(pf) if pf is not None else "#888")
        e    = float(t.get("entry",  0) or 0)
        stp2 = float(t.get("stop",   0) or 0)
        sh2  = float(t.get("shares", 0) or 0)
        cap2 = float(t.get("capital",0) or 0)
        risk2= round((e - stp2) * sh2, 2)
        cap2p= round(cap2 / ACCOUNT_SIZE * 100, 1)
        mkt  = str(t.get("market", ""))
        tr += "<tr>"
        tr += "<td style='color:#555'>%s</td>" % str(t.get("timestamp", ""))[:16]
        tr += "<td><b style='color:#fff'>%s</b></td>" % t.get("symbol", "")
        tr += "<td>%s</td>" % badge(mkt)
        tr += "<td>%s</td>" % t.get("side", "")
        tr += "<td>$%s</td>" % e
        tr += "<td style='color:#E24B4A'>$%s</td>" % stp2
        tr += "<td style='color:#1D9E75'>$%s</td>" % t.get("target1", "")
        tr += "<td>%s</td>" % t.get("shares", "")
        tr += "<td>$%.2f <span style='color:#444;font-size:10px'>(%.1f%%)</span></td>" % (cap2, cap2p)
        tr += "<td style='color:%s;font-weight:500'>%s</td>" % (stc, sc2)
        tr += "<td style='color:%s;font-weight:500'>%s</td>" % (stc, ps)
        tr += "<td style='color:#E24B4A;font-size:11px'>-$%s</td>" % risk2
        tr += "<td style='font-size:10px;color:#666'>" + str(t.get("reason", ""))[:60].replace("&","&amp;") + "</td>"
        tr += "</tr>"

    # scan results rows
    sr = ""
    for r in sorted(res, key=lambda x: x.get("score", 0), reverse=True):
        sig  = r.get("signal", "")
        sc2  = ("#1D9E75" if sig == "STRONG BUY" else
                "#378ADD" if sig == "BUY" else
                "#BA7517" if sig == "WATCH" else "#555")
        cc   = "#1D9E75" if r.get("change", 0) > 0 else "#E24B4A"
        mc   = "#1D9E75" if r.get("m5", 0) > 0 else "#E24B4A"
        sr += "<tr>"
        sr += "<td><b style='color:#fff'>%s</b></td>" % r.get("symbol", "")
        sr += "<td>%s</td>" % badge(r.get("market", ""))
        sr += "<td>$%s</td>" % r.get("price", "")
        sr += "<td style='color:%s'>%+.2f%%</td>" % (cc, r.get("change", 0))
        sr += "<td style='color:%s;font-weight:500'>%s</td>" % (sc2, sig)
        sr += "<td>%s/12</td>" % r.get("score", "")
        sr += "<td style='color:#888'>" + str(r.get("style", "")) + "</td>"
        sr += "<td>%s</td>" % r.get("rsi", "")
        sr += "<td style='color:%s'>%+.2f%%</td>" % (mc, r.get("m5", 0))
        sr += "<td>$%s</td>" % r.get("entry", "")
        sr += "<td style='color:#E24B4A'>$%s</td>" % r.get("stop", "")
        sr += "<td style='color:#1D9E75'>$%s</td>" % r.get("target1", "")
        sr += "</tr>"

    # weight bars
    wb = ""
    for k, v in wts.items():
        v   = float(v)
        pct = min(100, int(v / 2 * 100))
        col = "#1D9E75" if v >= 1 else "#E24B4A"
        tag = "▲ trusted" if v > 1.1 else "▼ weak" if v < 0.9 else "neutral"
        wb += "<div style='display:flex;align-items:center;gap:10px;margin:7px 0'>"
        wb += "<span style='width:90px;font-size:12px;color:#aaa'>%s</span>" % k
        wb += "<div style='flex:1;height:7px;background:#1a1a1a;border-radius:4px'>"
        wb += "<div style='width:%d%%;height:7px;background:%s;border-radius:4px'>" % (pct, col)
        wb += "</div></div>"
        wb += "<span style='font-size:12px;color:%s;width:36px'>%.2f</span>" % (col, v)
        wb += "<span style='font-size:10px;color:#444'>%s</span></div>" % tag

    # symbol performance rows
    spr = ""
    for sym2, d in sorted(sp.items(), key=lambda x: x[1].get("pnl", 0), reverse=True)[:12]:
        t2  = d.get("trades", 0)
        w2  = d.get("wins", 0)
        p2  = d.get("pnl", 0)
        wr2 = round(w2 / t2 * 100, 0) if t2 > 0 else 0
        spr += "<tr>"
        spr += "<td><b>%s</b></td><td>%d</td>" % (sym2, t2)
        spr += "<td style='color:#1D9E75'>%d</td>" % w2
        spr += "<td style='color:#E24B4A'>%d</td>" % (t2 - w2)
        spr += "<td>%.0f%%</td>" % wr2
        spr += "<td style='color:%s;font-weight:500'>$%.2f</td>" % (gc(p2), p2)
        spr += "</tr>"

    scan_banner = ""
    if s.get("scanning"):
        scan_banner = ("<div style='background:#1a1a00;border:0.5px solid #3a3a00;"
                       "border-radius:8px;padding:10px 14px;font-size:12px;"
                       "color:#f0d060;margin-bottom:16px'>"
                       "Scanning markets now — results update when scan completes</div>")

    CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0a0a0a;color:#c8c8c8;padding:24px 28px}
h1{font-size:22px;font-weight:500;color:#fff;margin-bottom:3px}
h2{font-size:10px;font-weight:600;margin:22px 0 10px;color:#444;text-transform:uppercase;letter-spacing:.1em}
.sub{font-size:11px;color:#333;margin-bottom:20px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:10px}
.g6{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:22px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:22px}
.card{background:#111;border:0.5px solid #1c1c1c;border-radius:10px;padding:15px 17px}
.hl{border-color:#1D9E75}.dg{border-color:#E24B4A}
.cl{font-size:9px;color:#444;text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px}
.cv{font-size:24px;font-weight:500}
.cv-sm{font-size:17px;font-weight:500}
.cv-sub{font-size:11px;color:#333;margin-top:3px}
.pw{height:5px;background:#181818;border-radius:3px;margin-top:9px}
.pb{height:5px;border-radius:3px}
table{width:100%;border-collapse:collapse;background:#111;border-radius:10px;overflow:hidden;margin-bottom:20px}
th{background:#161616;padding:9px 11px;font-size:9px;text-align:left;color:#444;text-transform:uppercase;letter-spacing:.07em;font-weight:600;white-space:nowrap}
td{padding:9px 11px;font-size:12px;border-bottom:0.5px solid #161616;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:#131313}
"""

    EC  = "#1D9E75" if equity >= ACCOUNT_SIZE else "#E24B4A"
    WRC = "#1D9E75" if st.get("win_rate", 0) >= 50 else "#E24B4A"
    CHC = "#1D9E75" if cash >= ACCOUNT_SIZE * 0.1 else "#E24B4A"

    H = []
    H.append("<!DOCTYPE html><html><head>")
    H.append("<title>AI Quant Trader</title>")
    H.append('<meta http-equiv="refresh" content="15">')
    H.append("<style>%s</style></head><body>" % CSS)
    H.append("<h1>AI Quant Trader</h1>")
    H.append("<p class='sub'>Refreshes every 15s &nbsp;&middot;&nbsp; "
             "Last scan: %s &nbsp;&middot;&nbsp; Scan #%d &nbsp;&middot;&nbsp; Paper trading only</p>" % (
                 s.get("last_scan", "—"), s.get("scan_count", 0)))
    H.append(scan_banner)

    # account overview
    H.append("<h2>Account Overview</h2><div class='g4'>")
    H.append("<div class='card hl'><div class='cl'>Starting Capital</div>"
             "<div class='cv'>$%s</div><div class='cv-sub'>Hard limit</div></div>" % "{:,}".format(ACCOUNT_SIZE))
    H.append("<div class='card'><div class='cl'>Cash Available</div>"
             "<div class='cv' style='color:%s'>$%.2f</div>"
             "<div class='cv-sub'>%.1f%% free to deploy</div>"
             "<div class='pw'><div class='pb' style='width:%.1f%%;background:#1D9E75'></div></div></div>" % (
                 CHC, max(cash, 0), cp, max(min(cp, 100), 0)))
    H.append("<div class='card'><div class='cl'>Capital Invested</div>"
             "<div class='cv' style='color:#378ADD'>$%.2f</div>"
             "<div class='cv-sub'>%.1f%% across %d trades</div>"
             "<div class='pw'><div class='pb' style='width:%.1f%%;background:#378ADD'></div></div></div>" % (
                 inv, ip, len(pos), min(ip, 100)))
    H.append("<div class='card %s'><div class='cl'>Total Equity</div>"
             "<div class='cv' style='color:%s'>$%.2f</div>"
             "<div class='cv-sub' style='color:%s'>P&amp;L: $%+.2f (%.2f%%)</div></div>" % (
                 "hl" if equity >= ACCOUNT_SIZE else "dg", EC, equity, gc(pnl), pnl, pnl_p))
    H.append("</div>")

    # performance
    H.append("<h2>Performance</h2><div class='g6'>")
    H.append("<div class='card'><div class='cl'>Closed Trades</div>"
             "<div class='cv-sm'>%d</div><div class='cv-sub'>%d still open</div></div>" % (
                 st.get("total", 0), len(pos)))
    H.append("<div class='card'><div class='cl'>Win Rate</div>"
             "<div class='cv-sm' style='color:%s'>%s%%</div>"
             "<div class='cv-sub'>%dW %dL</div></div>" % (
                 WRC, st.get("win_rate", 0), st.get("wins", 0), st.get("losses", 0)))
    H.append("<div class='card'><div class='cl'>Realized P&amp;L</div>"
             "<div class='cv-sm' style='color:%s'>$%+.2f</div>"
             "<div class='cv-sub'>%.2f%%</div></div>" % (gc(pnl), pnl, pnl_p))
    H.append("<div class='card'><div class='cl'>Best Trade</div>"
             "<div class='cv-sm' style='color:#1D9E75'>$%+.2f</div></div>" % st.get("best", 0))
    H.append("<div class='card'><div class='cl'>Worst Trade</div>"
             "<div class='cv-sm' style='color:#E24B4A'>$%.2f</div></div>" % st.get("worst", 0))
    H.append("<div class='card'><div class='cl'>Avg Win / Loss</div>"
             "<div class='cv-sm' style='color:#1D9E75'>$%.2f</div>"
             "<div class='cv-sub' style='color:#E24B4A'>$%.2f avg loss</div></div>" % (
                 st.get("avg_win", 0), st.get("avg_loss", 0)))
    H.append("</div>")

    # open positions
    H.append("<h2>Open Positions (%d/%d) &nbsp;&middot;&nbsp; $%.2f of $%s deployed</h2>" % (
        len(pos), MAX_POSITIONS, inv, "{:,}".format(ACCOUNT_SIZE)))
    if pr:
        H.append("<table><tr><th>Symbol</th><th>Market</th><th>Status</th>"
                 "<th>Entry</th><th>Curr Price</th><th>P&amp;L</th><th>P&amp;L %</th>"
                 "<th>Stop Loss</th><th>Target 1</th><th>Target 2</th>"
                 "<th>Units</th><th>Capital Used</th><th>Max Loss</th><th>R:R</th>"
                 "<th>Score</th><th>Style</th></tr>" + pr + "</table>")
    else:
        H.append("<p style='color:#333;font-size:13px;padding:10px 0'>No open positions — scanning for signals</p>")

    # scan results
    H.append("<h2>Latest Market Scan — %d symbols</h2>" % len(res))
    if sr:
        H.append("<table><tr><th>Symbol</th><th>Market</th><th>Price</th>"
                 "<th>Change</th><th>Signal</th><th>Score</th><th>Style</th>"
                 "<th>RSI</th><th>M5%</th><th>Entry</th><th>Stop</th><th>Target 1</th>"
                 "</tr>" + sr + "</table>")
    else:
        H.append("<p style='color:#333;font-size:13px;padding:10px 0'>Waiting for first scan to complete...</p>")

    # trade history
    H.append("<h2>Trade History — Last 25</h2>")
    H.append("<table><tr><th>Time</th><th>Symbol</th><th>Market</th><th>Side</th>"
             "<th>Entry</th><th>Stop</th><th>Target</th><th>Units</th><th>Capital</th>"
             "<th>Status</th><th>P&amp;L</th><th>Max Risk</th><th>Why</th></tr>")
    if tr:
        H.append(tr)
    else:
        H.append("<tr><td colspan='13' style='color:#2a2a2a;text-align:center;padding:20px'>No trades yet</td></tr>")
    H.append("</table>")

    # weights + symbol perf
    H.append("<div class='g2'>")
    H.append("<div><h2>AI Learning Weights</h2><div class='card'>")
    H.append("<p style='font-size:11px;color:#333;margin-bottom:12px'>"
             "Adjust after every closed trade. Green=trusted. Red=unreliable.</p>")
    H.append(wb if wb else "<p style='color:#2a2a2a;font-size:12px'>Weights appear after first trade closes</p>")
    H.append("</div></div>")
    H.append("<div><h2>Symbol Performance</h2>")
    H.append("<table><tr><th>Symbol</th><th>Trades</th><th>Wins</th>"
             "<th>Losses</th><th>Win%</th><th>P&amp;L</th></tr>")
    H.append(spr if spr else
             "<tr><td colspan='6' style='color:#2a2a2a;text-align:center;padding:16px'>No closed trades yet</td></tr>")
    H.append("</table></div></div>")
    H.append("<p style='font-size:10px;color:#1c1c1c;padding-top:14px;"
             "border-top:0.5px solid #141414'>Paper trading only. Not financial advice.</p>")
    H.append("</body></html>")

    return "".join(H)


class DashHandler(BaseHTTPRequestHandler):
    def log_message(self, f, *a):
        pass

    def do_GET(self):
        html = build_html()
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())


def run_dashboard():
    HTTPServer(("0.0.0.0", PORT), DashHandler).serve_forever()


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────
def run():
    log.info("AI Quant Trader v4 starting...")
    tlog    = TradeLog()
    learner = Learner()
    tracker = Tracker(tlog)
    exch    = ccxt.binance({"enableRateLimit": True})

    threading.Thread(target=run_dashboard, daemon=True).start()
    tlog.sync()
    STATE["weights"] = learner.w

    print("\n  Dashboard: http://localhost:%d  -- open in Chrome!\n" % PORT)

    scan = 0
    while True:
        scan += 1
        STATE["scan_count"] = scan
        STATE["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        STATE["scanning"]   = True

        cash, inv = tlog.cash_invested()
        STATE["cash"]     = cash
        STATE["invested"] = inv

        BAR = "=" * 56
        print("\n%s\n  AI QUANT TRADER v4  --  Scan #%d" % (BAR, scan))
        print("  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print("  Cash: $%.2f  |  Invested: $%.2f  |  Positions: %d/%d" % (
            cash, inv, len(tracker.pos), MAX_POSITIONS))
        st = STATE.get("stats", {})
        print("  Closed: %d  |  Win rate: %s%%  |  P&L: $%s\n%s" % (
            st.get("total", 0), st.get("win_rate", 0), st.get("total_pnl", 0), BAR))

        res    = []
        prices = {}

        print("  Scanning %d crypto..." % len(CRYPTO))
        for sym in CRYPTO:
            df = get_crypto(exch, sym)
            r  = analyze(sym, "CRYPTO", df, learner.w, cash)
            if r:
                prices[sym] = r["price"]
                res.append(r)
            time.sleep(1)

        print("  Scanning %d stocks..." % len(STOCKS))
        for sym in STOCKS:
            df, info, heads = get_yf(sym)
            sent = get_sentiment(sym)
            earn = get_earnings(sym)
            r    = analyze(sym, "STOCK", df, learner.w, cash, sent, earn, heads, info)
            if r:
                prices[sym] = r["price"]
                res.append(r)
            time.sleep(13)

        print("  Scanning %d ETFs..." % len(ETFS))
        for sym in ETFS:
            df, info, heads = get_yf(sym)
            r = analyze(sym, "ETF", df, learner.w, cash, 0.0, None, heads, info)
            if r:
                prices[sym] = r["price"]
                res.append(r)
            time.sleep(3)

        print("  Scanning %d futures..." % len(FUTURES))
        for sym in FUTURES:
            df, info, heads = get_yf(sym)
            r = analyze(sym, "FUTURES", df, learner.w, cash, 0.0, None, heads, info)
            if r:
                prices[sym] = r["price"]
                res.append(r)
            time.sleep(3)

        print("  Scanning %d forex..." % len(FOREX))
        for sym in FOREX:
            df, info, _ = get_yf(sym, period="6mo")
            r = analyze(sym, "FOREX", df, learner.w, cash)
            if r:
                prices[sym] = r["price"]
                res.append(r)
            time.sleep(3)

        STATE["results"]  = res
        STATE["scanning"] = False
        STATE["current_prices"] = prices

        tracker.check_exits(prices, learner)

        cash, inv = tlog.cash_invested()
        STATE["cash"]     = cash
        STATE["invested"] = inv

        buys = [r for r in res if r["signal"] in ("STRONG BUY", "BUY")]
        buys.sort(key=lambda x: x["score"], reverse=True)

        print("\n%s\n  SCAN COMPLETE -- %d symbols | %d buy signals | Cash: $%.2f\n%s" % (
            BAR, len(res), len(buys), cash, BAR))
        print("  %-12s %-9s %-13s %-14s %-7s %-12s %-8s %s" % (
            "SYMBOL", "MARKET", "PRICE", "SIGNAL", "SCORE", "STYLE", "M5%", "RSI"))
        print("  " + "-" * 78)
        for r in sorted(res, key=lambda x: x.get("score", 0), reverse=True):
            flag = " <<< BEST" if buys and r == buys[0] else ""
            print("  %-12s %-9s $%-12s %-14s %-7s %-12s %+.1f%%   %s%s" % (
                r["symbol"], r["market"], r["price"], r["signal"],
                r["score"], r["style"], r["m5"], r["rsi"], flag))

        opened = 0
        for c in buys:
            if c["symbol"] in tracker.pos:
                continue
            cash, _ = tlog.cash_invested()
            if cash < 100:
                break
            if c["cost"] > cash:
                c["shares"] = max(1, int(cash * 0.9 / max(c["entry"], 0.0001)))
                c["cost"]   = round(c["shares"] * c["entry"], 2)
                c["alloc"]  = round(c["cost"] / ACCOUNT_SIZE * 100, 1)
            if c["cost"] <= 0 or c["shares"] <= 0:
                continue
            if tracker.open(c, cash):
                opened += 1

        if opened == 0:
            print("  No new positions opened this scan")

        tlog.sync()
        cash, inv = tlog.cash_invested()
        st = STATE.get("stats", {})
        print("\n  DONE: %d closed | %.1f%% win | P&L: $%.2f | Cash: $%.2f | Open: %s" % (
            st.get("total", 0), st.get("win_rate", 0),
            st.get("total_pnl", 0), cash, list(tracker.pos.keys())))
        print("  Next scan in %d min | http://localhost:%d\n%s\n" % (
            SCAN_INTERVAL // 60, PORT, "=" * 56))

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nBot stopped. All trades saved to ~/trading_bot/trades.csv")
