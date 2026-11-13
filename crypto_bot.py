"""Crypto + stock bot for the Alpaca paper account.

    python crypto_bot.py scan                       rank every crypto */USD pair on its 15-minute charts
    python crypto_bot.py stocks                     rank the stock list on its 5-minute charts
    python crypto_bot.py enter SYMBOL --usd 100     market buy (asks for yes unless --yes)
    python crypto_bot.py manage SYMBOL              watch the position and exit by the rules below
    python crypto_bot.py auto --every-min 10        the loop: crypto 24/7, plus stocks while NYSE is open

Exit rules (the bot watches the price itself; Alpaca has no brackets for crypto or fractional shares):
              crypto              stocks
  stop-loss   -2%                 -1%
  target      +4%                 +2%     momentum faded -> sell; still rising -> let it run with a trailing stop
  hard max    +6%                 +3%
  stocks only: buys 9:45-15:30 New York time, anything still open is sold 5 minutes before the close
  stock SHORTS: the mirror image (downtrend, RSI 28-50, past short record above break-even): sell short whole
              shares worth up to $100, buy back at -2% (profit), +1% (stop), trailing, or before the close
              (crypto can't be shorted on Alpaca)
Daily limits from trader/config.json: max_trades_per_day, max_daily_loss_usd (New York day).
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from connect import load_env

MK = {
    "crypto": dict(stop=2.0, target=4.0, max=6.0, lock=3.0, trail=1.5, arm=2.0, floor=0.5, rsi=(50, 72), atr=(0.15, 1.2),
                   min_dvol=1000, tif="gtc"),
    "stock": dict(stop=1.0, target=2.0, max=3.0, lock=1.5, trail=0.75, rsi=(50, 72), atr=(0.08, 1.0),
                  min_dvol=50000, tif="day"),
}
STOP_PCT, TARGET_PCT = MK["crypto"]["stop"], MK["crypto"]["target"]
RULE = "crypto +%g/-%g, stocks +%g/-%g" % (MK["crypto"]["target"], MK["crypto"]["stop"],
                                           MK["stock"]["target"], MK["stock"]["stop"])
DATA = "https://data.alpaca.markets/v1beta3/crypto/us"
STOCK_DATA = "https://data.alpaca.markets/v2/stocks"
LOG = Path(__file__).with_name("trader") / "crypto_log.jsonl"
CONFIG = Path(__file__).with_name("trader") / "config.json"

# Liquid, volatile US names; the bot drops any that Alpaca can't trade in fractions.
STOCKS = ["TSLA", "NVDA", "AMD", "PLTR", "SOFI", "F", "NIO", "RIVN", "LCID", "MARA", "RIOT", "COIN", "HOOD",
          "AAPL", "AMZN", "META", "MSFT", "GOOGL", "INTC", "SNAP", "UBER", "SHOP", "SMCI", "MU", "AVGO", "NFLX",
          "BA", "DKNG", "RBLX", "AFRM", "UPST", "CVNA", "ROKU", "PYPL", "BABA", "CCL", "AAL", "DAL", "PFE",
          "SNOW", "CRWD", "NET", "ARM", "MSTR", "IONQ", "RKLB", "HIMS", "OKLO", "CLSK", "SPY", "QQQ"]


def mkt(sym):
    return "crypto" if "/" in sym else "stock"


def min_hit(m):
    p = MK[m]
    return p["stop"] / (p["stop"] + p["target"]) * 100   # break-even win rate: 33.3% for 2:1


def env():
    load_env()
    base = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
    h = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
         "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"], "Content-Type": "application/json"}
    return base, h


def call(method, url, h, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method, headers=h)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 + attempt * 3)
                continue
            raise RuntimeError("HTTP %s %s: %s" % (e.code, url, msg))
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(3)
                continue
            raise


_LOG_LOCK = threading.RLock()


def log(event, **kw):
    with _LOG_LOCK:
        _log(event, **kw)


def _log(event, **kw):
    LOG.parent.mkdir(exist_ok=True)
    rec = dict(at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), event=event, **kw)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("[%s] %s %s" % (rec["at"][11:19], event, " ".join("%s=%s" % kv for kv in kw.items())), flush=True)
    if event in ("scan", "scan_error", "entry", "exit", "done", "let_it_run", "watch", "manage"):
        try:
            import importlib
            import dashboard
            importlib.reload(dashboard)  # dashboard edits apply without restarting the bot
            dashboard.build()
        except Exception as e:  # the dashboard must never stop the bot
            print("dashboard error:", e, flush=True)


# ------------------------------------------------------------------ New York time (no tz database on this PC)

def _sunday(year, month, nth):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=nth - 1)


def to_et(t):
    """UTC datetime (aware or naive) -> naive New York datetime."""
    t = t.astimezone(dt.timezone.utc).replace(tzinfo=None) if t.tzinfo else t
    edt = (dt.datetime.combine(_sunday(t.year, 3, 2), dt.time(7)) <= t
           < dt.datetime.combine(_sunday(t.year, 11, 1), dt.time(6)))
    return t - dt.timedelta(hours=4 if edt else 5)


def parse_t(s):
    return dt.datetime.fromisoformat(s.replace("Z", "")[:19])


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


# ------------------------------------------------------------------ indicators

def ema(xs, n):
    k, out = 2 / (n + 1), []
    for x in xs:
        out.append(x if not out else out[-1] + k * (x - out[-1]))
    return out


def rsi(closes, n=14):
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr_pct(bars, n=14):
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) / p["c"] * 100
           for p, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(trs) / len(trs)


def trend_ok(closes):
    e20, e50 = ema(closes, 20), ema(closes, 50)
    return e20[-1] > e50[-1] and closes[-1] > e20[-1] and e20[-1] > e20[-4]


def replay(bars, horizon=96):
    """Crypto: every past bar where the trend filter held: did the target or the stop come first within
    24h (96 bars)? A bar touching both counts as the stop. Returns (signals, wins, losses)."""
    p = MK["crypto"]
    closes = [b["c"] for b in bars]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    sig = win = loss = 0
    i = 60
    while i < len(bars) - 1:
        if e20[i] > e50[i] and closes[i] > e20[i] and e20[i] > e20[i - 3]:
            entry = closes[i]
            stop, tgt = entry * (1 - p["stop"] / 100), entry * (1 + p["target"] / 100)
            sig += 1
            for b in bars[i + 1: i + 1 + horizon]:
                if b["l"] <= stop:
                    loss += 1
                    break
                if b["h"] >= tgt:
                    win += 1
                    break
            i += 8  # no overlapping trades: skip 2 hours
        else:
            i += 1
    return sig, win, loss


def trend_down(closes):
    e20, e50 = ema(closes, 20), ema(closes, 50)
    return e20[-1] < e50[-1] and closes[-1] < e20[-1] and e20[-1] < e20[-4]


SHORT_RSI = (28, 50)
BUDGET = 100      # set from `auto --usd`: crypto trades use this many dollars; stocks trade 1 share costing <= this


def replay_stock(bars, side="long"):
    """Stocks: trend signal on 5-minute bars, entries only 9:45-15:30, and the trade must hit the
    target or the stop the SAME day before 15:55 (otherwise it's flat at the close and not counted).
    side='short' mirrors it: downtrend signal, target 2% BELOW the entry, stop 1% above."""
    p = MK["stock"]
    closes = [b["c"] for b in bars]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    sig = win = loss = 0
    i = 60
    while i < len(bars) - 1:
        et = bars[i]["et"]
        mins = et.hour * 60 + et.minute
        if side == "long":
            signal = e20[i] > e50[i] and closes[i] > e20[i] and e20[i] > e20[i - 3]
        else:
            signal = e20[i] < e50[i] and closes[i] < e20[i] and e20[i] < e20[i - 3]
        if 585 <= mins <= 930 and signal:
            entry = closes[i]
            if side == "long":
                stop, tgt = entry * (1 - p["stop"] / 100), entry * (1 + p["target"] / 100)
            else:
                stop, tgt = entry * (1 + p["stop"] / 100), entry * (1 - p["target"] / 100)
            sig += 1
            for b in bars[i + 1:]:
                if b["et"].date() != et.date() or b["et"].hour * 60 + b["et"].minute >= 955:
                    break
                if (b["l"] <= stop) if side == "long" else (b["h"] >= stop):
                    loss += 1
                    break
                if (b["h"] >= tgt) if side == "long" else (b["l"] <= tgt):
                    win += 1
                    break
            i += 6  # skip 30 minutes
        else:
            i += 1
    return sig, win, loss


# ------------------------------------------------------------------ scan

def _fetch(url, q, h):
    """All pages of a multi-symbol bars request -> {symbol: [bars]}."""
    out, token = {}, None
    while True:
        if token:
            q["page_token"] = token
        r = call("GET", url + "?" + urllib.parse.urlencode(q), h)
        for s, bs in (r.get("bars") or {}).items():
            out.setdefault(s, []).extend(bs)
        token = r.get("next_page_token")
        if not token:
            return out


# Price history kept in memory between checks: the first check downloads 1-2 weeks, every later check
# only fetches the bars since the last one it has (usually 1-2 per symbol, one request).
_CACHE = {"crypto": {}, "stock": {}}


def _cached_bars(kind, symbols, h, days, url, extra, minutes, keep=None):
    cache = _CACHE[kind]
    now = now_utc()
    floor = now - dt.timedelta(days=days)
    have = [s for s in symbols if cache.get(s)]
    new = [s for s in symbols if not cache.get(s)]
    fetched = {}
    if new:
        q = dict(extra, symbols=",".join(new), start=floor.strftime("%Y-%m-%dT%H:%M:%SZ"), limit=10000)
        fetched.update(_fetch(url, q, h))
    if have:
        # from the oldest 'last bar' among cached symbols (refreshes any bar that was still in progress)
        since = max(min(parse_t(cache[s][-1]["t"]) for s in have), floor.replace(tzinfo=None))
        q = dict(extra, symbols=",".join(have), start=since.strftime("%Y-%m-%dT%H:%M:%SZ"), limit=10000)
        for s, bs in _fetch(url, q, h).items():
            fetched.setdefault(s, []).extend(bs)
    for s, bs in fetched.items():
        if keep:
            bs = [b for b in bs if keep(b)]
        if not bs:
            continue
        first = bs[0]["t"]
        cache[s] = [b for b in cache.get(s, []) if b["t"] < first] + bs
    cutoff = floor.strftime("%Y-%m-%dT%H:%M:%SZ")
    done = (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for s in symbols:
        bs = [b for b in cache.get(s, []) if b["t"] >= cutoff]
        cache[s] = bs
        # judge only finished candles: drop the one still forming
        out[s] = [b for b in bs if b["t"] <= done]
    return out


def get_bars(symbols, h, days=7):
    """15-minute crypto bars, finished candles only."""
    return _cached_bars("crypto", symbols, h, days, DATA + "/bars", {"timeframe": "15Min"}, 15)


def _regular_session(b):
    et = to_et(parse_t(b["t"]))
    if 570 <= et.hour * 60 + et.minute < 960:
        b["et"] = et
        return True
    return False


def get_stock_bars(symbols, h, days=14):
    """5-minute IEX bars (free feed), regular session 9:30-16:00 New York only, each tagged with b['et']."""
    return _cached_bars("stock", symbols, h, days, STOCK_DATA + "/bars",
                        {"timeframe": "5Min", "feed": "iex", "adjustment": "raw"}, 5, keep=_regular_session)


STABLE = {"USDC/USD", "USDT/USD", "USDG/USD", "DAI/USD", "PYUSD/USD"}
MIN_DECIDED = 4          # past trades that hit the target or the stop, needed to trust the hit rate
MIN_HIT = min_hit("crypto")
MAX_SPREAD_PCT = 0.25
# meme coins trade at tiny prices, so one price tick alone is ~0.2%; allow a wider spread
MEME = {"DOGE/USD", "SHIB/USD", "PEPE/USD", "BONK/USD", "WIF/USD", "TRUMP/USD"}
MEME_SPREAD_PCT = 0.6

# ------------------------------------------------------------------ portfolio: how much to put in each trade
# BUDGET is the pool the bot may use (set by `auto --usd`). Each trade risks ~RISK_PCT of the pool at its stop,
# scaled by conviction (the setup's past win rate, the bot's own real record, losing streaks, meme volatility),
# then capped per position, per sector and by what is left of the pool.
RISK_PCT = 0.5          # base risk per trade, % of the pool ($50 on $10,000)
MAX_POS_PCT = 25        # one position at most this % of the pool
MAX_SECTOR_PCT = 40     # one sector at most this % of the pool
MAX_OPEN = 4            # positions held at the same time
MIN_ORDER_USD = 25
SECTOR = {}
for _sec, _syms in {
        "EV & autos": "TSLA F NIO RIVN LCID",
        "Chips": "NVDA AMD INTC SMCI MU AVGO ARM",
        "Crypto stocks": "MARA RIOT COIN MSTR CLSK",
        "Fintech": "SOFI HOOD AFRM UPST PYPL",
        "Big tech": "AAPL AMZN META MSFT GOOGL NFLX",
        "Software": "PLTR SHOP SNOW CRWD NET",
        "Consumer internet": "SNAP UBER DKNG RBLX ROKU BABA CVNA",
        "Travel": "BA CCL AAL DAL",
        "Health": "PFE HIMS",
        "Frontier tech": "IONQ RKLB OKLO",
        "Index funds": "SPY QQQ"}.items():
    for _s in _syms.split():
        SECTOR[_s] = _sec
OPEN = {}               # symbol -> dict(usd, side, sector) for trades being managed right now
OPEN_LOCK = threading.Lock()


def sector(sym):
    if mkt(sym) == "crypto":
        return "Meme coins" if sym in MEME else "Crypto"
    return SECTOR.get(sym, "Other stocks")


def trade_history():
    """The bot's own closed trades, oldest first: dict(sym, side, mkt, pnl, at)."""
    out = []
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "done":
                out.append(dict(sym=ev["symbol"], side=ev.get("side", "long"), mkt=mkt(ev["symbol"]),
                                pnl=float(ev.get("pnl_usd") or 0), at=parse_t(ev["at"]).replace(tzinfo=dt.timezone.utc)))
    return out


def size_trade(r, side, px, held):
    """Dollars (and whole shares for stocks) for this trade, with the reasoning.
    held: OPEN snapshot {sym: dict(usd, sector)}."""
    m, sym = r["mkt"], r["symbol"]
    p = MK[m]
    rec = r if side == "long" else r["short"]
    be = min_hit(m)
    why, conf = [], 1.0
    # 1. the setup's own past record on this symbol
    f = max(0.5, min(1.5, 1 + (rec["hit"] - be) / 50))
    why.append("setup won %.0f%% (needs %.0f%%) x%.2f" % (rec["hit"], be, f))
    conf *= f
    if rec["win"] + rec["loss"] < 12:
        conf *= 0.75
        why.append("only %d past cases x0.75" % (rec["win"] + rec["loss"]))
    # 2. the bot's real results
    hist = trade_history()
    same = [t for t in hist if t["mkt"] == m and t["side"] == side][-20:]
    if len(same) >= 5:
        wr = sum(t["pnl"] > 0 for t in same) / len(same) * 100
        f = max(0.5, min(1.25, 1 + (wr - be) / 100))
        why.append("my last %d %s %s trades won %.0f%% x%.2f" % (len(same), m, side, wr, f))
        conf *= f
    streak = 0
    for t in reversed(hist):
        if t["pnl"] > 0:
            break
        streak += 1
    if streak >= 3:
        conf *= 0.5
        why.append("%d losses in a row x0.5" % streak)
    last = next((t for t in reversed(hist) if t["sym"] == sym), None)
    if last and last["pnl"] < 0 and now_utc() - last["at"] < dt.timedelta(hours=24):
        conf *= 0.5
        why.append("lost on %s in the last 24h x0.5" % sym.replace("/USD", ""))
    if sym in MEME:
        conf *= 0.5
        why.append("meme coin x0.5")
    conf = max(0.25, min(1.5, conf))
    risk = BUDGET * RISK_PCT / 100 * conf
    usd = risk / (p["stop"] / 100)
    # 3. caps: one position, one sector, the whole pool
    sec = sector(sym)
    used = sum(x["usd"] for x in held.values())
    used_sec = sum(x["usd"] for x in held.values() if x["sector"] == sec)
    caps = [(BUDGET * MAX_POS_PCT / 100, "position cap %d%%" % MAX_POS_PCT),
            (BUDGET * MAX_SECTOR_PCT / 100 - used_sec, "%s sector cap %d%%" % (sec, MAX_SECTOR_PCT)),
            (BUDGET - used, "pool left")]
    for c, name in caps:
        if usd > c:
            usd = max(0.0, c)
            why.append("capped by " + name)
    qty = int(usd // px) if m == "stock" else 0
    if m == "stock":
        usd = qty * px
    return usd, qty, dict(conf=round(conf, 2), risk=round(usd * p["stop"] / 100, 2), sector=sec,
                          text="; ".join(why))


def ok_now(r):
    p = MK[r["mkt"]]
    return (r["trend"] and p["rsi"][0] <= r["rsi"] <= p["rsi"][1] and p["atr"][0] <= r["atr"] <= p["atr"][1]
            and r["dvol"] >= p["min_dvol"])


def qualifies(r):
    return r["ok"] and (r["win"] + r["loss"]) >= MIN_DECIDED and r["hit"] >= min_hit(r["mkt"])


def qualifies_short(r):
    s = r.get("short")
    return bool(s and s["ok"] and s["win"] + s["loss"] >= MIN_DECIDED and s["hit"] >= min_hit("stock")
                and s["shortable"] and s["qty"] >= 1)


def short_reasons(r):
    s, p, out = r.get("short"), MK["stock"], []
    if not s:
        return ["not a stock"]
    if not s["trend"]:
        out.append("no downtrend")
    if not SHORT_RSI[0] <= r["rsi"] <= SHORT_RSI[1]:
        out.append("RSI %.0f outside %d-%d" % (r["rsi"], SHORT_RSI[0], SHORT_RSI[1]))
    if not p["atr"][0] <= r["atr"] <= p["atr"][1]:
        out.append("volatility %.2f%% outside range" % r["atr"])
    if not s["shortable"]:
        out.append("not easy to borrow")
    if s["qty"] < 1:
        out.append("1 share costs more than the $%g position cap" % (BUDGET * MAX_POS_PCT / 100))
    if s["win"] + s["loss"] < MIN_DECIDED:
        out.append("too few past shorts (%d)" % (s["win"] + s["loss"]))
    elif s["hit"] < min_hit("stock"):
        out.append("past short win rate %.0f%% < %.0f%%" % (s["hit"], min_hit("stock")))
    return out


def reasons(r):
    """Every check a symbol failed, in plain words. Empty list = it qualifies."""
    p, out = MK[r["mkt"]], []
    if not r["trend"]:
        out.append("no uptrend (%s EMA20 vs EMA50)" % ("15m" if r["mkt"] == "crypto" else "5m"))
    if not p["rsi"][0] <= r["rsi"] <= p["rsi"][1]:
        out.append("RSI %.0f outside %d-%d" % (r["rsi"], p["rsi"][0], p["rsi"][1]))
    if not p["atr"][0] <= r["atr"] <= p["atr"][1]:
        out.append("volatility %.2f%% outside %g-%g" % (r["atr"], p["atr"][0], p["atr"][1]))
    if r["dvol"] < p["min_dvol"]:
        out.append("thin volume $%.0f" % r["dvol"])
    if r["win"] + r["loss"] < MIN_DECIDED:
        out.append("too few past trades (%d)" % (r["win"] + r["loss"]))
    elif r["hit"] < min_hit(r["mkt"]):
        out.append("past win rate %.0f%% < %.0f%%" % (r["hit"], min_hit(r["mkt"])))
    return out


def score(r):
    r["ok"] = ok_now(r)
    conf = min(1.0, (r["win"] + r["loss"]) / 8)
    r["score"] = (r["hit"] - min_hit(r["mkt"])) * conf + (10 if r["ok"] else -100) + max(-5, min(5, r["chg4h"]))


RUN_FIELDS = ["run_id", "time_utc", "symbol", "rank", "price", "chg4h_pct", "chg24h_pct", "rsi", "atr_pct",
              "trend_up", "vol24h_usd", "past_wins", "past_losses", "past_hit_pct", "score", "decision", "reason"]


def log_run(rows, decision_for=None, stocks_open=False, next_utc=None, took=None):
    """One CSV row per symbol per run (trader/logs/scan-YYYY-MM-DD.csv) + one summary line (runs.jsonl)."""
    import csv
    now = now_utc()
    run_id = now.strftime("%Y%m%d-%H%M%S")
    d = LOG.parent / "logs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("scan-%s.csv" % now.strftime("%Y-%m-%d"))
    new = not path.exists()
    decision_for = decision_for or {}
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RUN_FIELDS)
        if new:
            w.writeheader()
        for i, r in enumerate(rows, 1):
            why = reasons(r)
            if why and qualifies_short(r):
                why, default = [], "qualifies (short)"
            else:
                default = "qualifies" if not why else "rejected"
            w.writerow(dict(run_id=run_id, time_utc=now.strftime("%Y-%m-%d %H:%M:%S"), symbol=r["symbol"], rank=i,
                            price="%.8g" % r["price"], chg4h_pct=round(r["chg4h"], 2),
                            chg24h_pct=round(r["chg24h"], 2), rsi=round(r["rsi"], 1), atr_pct=round(r["atr"], 3),
                            trend_up=r["trend"], vol24h_usd=round(r["dvol"]), past_wins=r["win"],
                            past_losses=r["loss"], past_hit_pct=round(r["hit"], 1), score=round(r["score"], 1),
                            decision=decision_for.get(r["symbol"], default),
                            reason="; ".join(why) or ("passed every short check" if default.endswith("(short)")
                                                      else "passed every check")))
    q = [r["symbol"] for r in rows if qualifies(r)]
    qs = [r["symbol"] for r in rows if qualifies_short(r)]
    summary = dict(run_id=run_id, time_utc=now.strftime("%Y-%m-%d %H:%M:%S"), pairs=len(rows), qualifying=q,
                   uptrend=[r["symbol"] for r in rows if r["trend"]],
                   best_record=max(rows, key=lambda r: r["hit"])["symbol"] if rows else None,
                   rule=RULE, decisions=decision_for, stocks_open=stocks_open, qualifying_short=qs,
                   downtrend=[r["symbol"] for r in rows if r.get("short") and r["short"]["trend"]],
                   next_utc=next_utc, took_s=took)
    with (d / "runs.jsonl").open("a") as f:
        f.write(json.dumps(summary) + "\n")
    return run_id


def scan(h, base):
    """Crypto: every tradable */USD pair except stablecoins."""
    assets = call("GET", base + "/v2/assets?asset_class=crypto&status=active", h)
    syms = sorted(x["symbol"] for x in assets
                  if x["tradable"] and x["symbol"].endswith("/USD") and x["symbol"] not in STABLE)
    bars = get_bars(syms, h)
    rows = []
    for s in syms:
        b = bars.get(s, [])
        if len(b) < 200:
            continue
        closes = [x["c"] for x in b]
        sig, win, loss = replay(b)
        decided = win + loss
        r = dict(symbol=s, mkt="crypto", price=closes[-1], bars=len(b),
                 chg4h=(closes[-1] / closes[-17] - 1) * 100, chg24h=(closes[-1] / closes[-97] - 1) * 100,
                 rsi=rsi(closes), atr=atr_pct(b), trend=trend_ok(closes), dvol=sum(x["v"] * x["vw"] for x in b[-96:]),
                 sig=sig, win=win, loss=loss, hit=(win / decided * 100) if decided else 0.0,
                 spark=[round(x, 8) for x in closes[-64:]])
        score(r)
        rows.append(r)
    rows.sort(key=lambda r: r["score"], reverse=True)
    Path("trader").mkdir(exist_ok=True)
    Path("trader/crypto_scan.json").write_text(json.dumps(rows, indent=1))
    return rows


_UNIVERSE = None
_SHORTABLE = set()
_UNIVERSE_DAY = None


def stock_universe(base, h):
    """Tradable, fractionable stocks; re-checked every New York day (easy-to-borrow changes daily)."""
    global _UNIVERSE, _SHORTABLE, _UNIVERSE_DAY
    day = to_et(now_utc()).date()
    if _UNIVERSE is None or _UNIVERSE_DAY != day:
        from concurrent.futures import ThreadPoolExecutor

        def one(s):
            try:
                return call("GET", base + "/v2/assets/" + s, h)
            except Exception:
                return None

        ok, short = [], set()
        with ThreadPoolExecutor(max_workers=10) as pool:   # ~8 s one by one -> ~1 s in parallel
            info = dict(zip(STOCKS, pool.map(one, STOCKS)))
        for s in STOCKS:
            a = info.get(s)
            if not a:
                continue
            if a.get("tradable") and a.get("fractionable"):
                ok.append(s)
                if a.get("shortable") and a.get("easy_to_borrow"):
                    short.add(s)
        _UNIVERSE, _SHORTABLE, _UNIVERSE_DAY = ok, short, day
    return _UNIVERSE


def scan_stocks(h, base):
    syms = stock_universe(base, h)
    bars = get_stock_bars(syms, h)
    rows = []
    for s in syms:
        b = bars.get(s, [])
        if len(b) < 200:
            continue
        closes = [x["c"] for x in b]
        sig, win, loss = replay_stock(b)
        decided = win + loss
        r = dict(symbol=s, mkt="stock", price=closes[-1], bars=len(b),
                 chg4h=(closes[-1] / closes[-49] - 1) * 100, chg24h=(closes[-1] / closes[-79] - 1) * 100,
                 rsi=rsi(closes), atr=atr_pct(b), trend=trend_ok(closes),
                 dvol=sum(x["v"] * x["vw"] for x in b[-78:]),
                 sig=sig, win=win, loss=loss, hit=(win / decided * 100) if decided else 0.0,
                 spark=[round(x, 8) for x in closes[-64:]])
        score(r)
        # the short side: whole shares only (no fractional shorts), easy-to-borrow only
        ssig, swin, sloss = replay_stock(b, "short")
        sd = swin + sloss
        sh = dict(trend=trend_down(closes), win=swin, loss=sloss, hit=(swin / sd * 100) if sd else 0.0,
                  shortable=s in _SHORTABLE, qty=1 if closes[-1] <= BUDGET * MAX_POS_PCT / 100 else 0)
        p = MK["stock"]
        sh["ok"] = (sh["trend"] and SHORT_RSI[0] <= r["rsi"] <= SHORT_RSI[1]
                    and p["atr"][0] <= r["atr"] <= p["atr"][1] and r["dvol"] >= p["min_dvol"])
        sh["score"] = ((sh["hit"] - min_hit("stock")) * min(1.0, sd / 8) + (10 if sh["ok"] else -100)
                       + max(-5, min(5, -r["chg4h"])))
        r["short"] = sh
        rows.append(r)
    rows.sort(key=lambda r: r["score"], reverse=True)
    Path("trader").mkdir(exist_ok=True)
    Path("trader/stock_scan.json").write_text(json.dumps([{k: v for k, v in r.items()} for r in rows], indent=1))
    return rows


def print_rows(rows):
    print("%-10s %12s %7s %7s %5s %5s %6s %12s %10s %6s %s" % (
        "symbol", "price", "4h%", "1d%", "RSI", "ATR%", "trend", "$vol", "past W/L", "hit%", "score"))
    for r in rows:
        print("%-10s %12.6g %+7.2f %+7.2f %5.0f %5.2f %6s %12s %4d/%-5d %6.0f %6.1f%s" % (
            r["symbol"], r["price"], r["chg4h"], r["chg24h"], r["rsi"], r["atr"], "up" if r["trend"] else "-",
            "{:,.0f}".format(r["dvol"]), r["win"], r["loss"], r["hit"], r["score"],
            "  <- QUALIFIES" if qualifies(r) else ("  <- trend ok, weak history" if r["ok"] else "")))


def cmd_scan(a):
    base, h = env()
    print_rows(scan(h, base))
    print("\nCrypto rule +%g/-%g, break-even hit rate %.0f%%. 'past' = same rule on the last 7 days of 15-min bars."
          % (MK["crypto"]["target"], MK["crypto"]["stop"], min_hit("crypto")))
    return 0


def cmd_stocks(a):
    base, h = env()
    print_rows(scan_stocks(h, base))
    print("\nStock rule +%g/-%g same day, break-even %.0f%%. 'past' = last ~10 sessions of 5-min IEX bars."
          % (MK["stock"]["target"], MK["stock"]["stop"], min_hit("stock")))
    return 0


# ------------------------------------------------------------------ prices and orders

def quote(sym, h):
    """(bid, ask). Stocks use the free IEX feed; a missing side comes back as 0."""
    if mkt(sym) == "crypto":
        q = call("GET", DATA + "/latest/quotes?symbols=" + urllib.parse.quote(sym, safe=""), h)["quotes"][sym]
    else:
        q = call("GET", STOCK_DATA + "/quotes/latest?feed=iex&symbols=" + sym, h)["quotes"][sym]
    return float(q["bp"]), float(q["ap"])


def price_now(sym, h):
    """The price exits are judged on: crypto bid; stocks last IEX trade (IEX quotes are often one-sided)."""
    if mkt(sym) == "crypto":
        return quote(sym, h)[0]
    return float(call("GET", STOCK_DATA + "/trades/latest?feed=iex&symbols=" + sym, h)["trades"][sym]["p"])


def position(sym, base, h):
    try:
        return call("GET", base + "/v2/positions/" + sym.replace("/", ""), h)
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


def _wait(o, base, h):
    for _ in range(20):
        time.sleep(1)
        o = call("GET", base + "/v2/orders/" + o["id"], h)
        if o["status"] in ("filled", "canceled", "rejected", "expired"):
            break
    return o


def buy_shares(sym, qty, base, h):
    """Buy a whole number of shares (stocks: the bot buys exactly 1 share of an eligible stock)."""
    o = call("POST", base + "/v2/orders", h, {"symbol": sym, "qty": str(int(qty)), "side": "buy",
                                             "type": "market", "time_in_force": "day"})
    o = _wait(o, base, h)
    log("entry", symbol=sym, market="stock", side="long", status=o["status"], qty=o["filled_qty"],
        price=o["filled_avg_price"], id=o["id"])
    return o


def buy(sym, usd, base, h):
    o = call("POST", base + "/v2/orders", h, {"symbol": sym, "notional": "%.2f" % usd, "side": "buy",
                                             "type": "market", "time_in_force": MK[mkt(sym)]["tif"]})
    o = _wait(o, base, h)
    log("entry", symbol=sym, market=mkt(sym), status=o["status"], qty=o["filled_qty"], price=o["filled_avg_price"],
        id=o["id"])
    return o


def short(sym, qty, base, h):
    """Sell short whole shares (Alpaca has no fractional short sales)."""
    o = call("POST", base + "/v2/orders", h, {"symbol": sym, "qty": str(int(qty)), "side": "sell",
                                             "type": "market", "time_in_force": "day"})
    o = _wait(o, base, h)
    log("entry", symbol=sym, market="stock", side="short", status=o["status"], qty=o["filled_qty"],
        price=o["filled_avg_price"], id=o["id"])
    return o


def sell_all(sym, qty, base, h, reason, side="long"):
    """Close the bot's position: sell a long, buy back (cover) a short.
    Uses Alpaca's own quantity string when closing the whole position: a float like 217320261.4379085
    can round a hair above the real 217320261.437908496 and get rejected as 'insufficient balance'."""
    q = str(qty)
    try:
        held = call("GET", base + "/v2/positions/" + sym.replace("/", ""), h)["qty"].lstrip("-")
        if float(qty) >= float(held) * 0.999999:
            q = held
    except Exception as e:
        log("position_lookup_error", symbol=sym, error=str(e)[:120])
    o = call("POST", base + "/v2/orders", h, {"symbol": sym, "qty": q, "side": "sell" if side == "long" else "buy",
                                             "type": "market", "time_in_force": MK[mkt(sym)]["tif"]})
    o = _wait(o, base, h)
    log("exit", symbol=sym, side=side, reason=reason, status=o["status"], qty=o["filled_qty"],
        price=o["filled_avg_price"])
    return o


# ------------------------------------------------------------------ safety stop at Alpaca
# The bot's own stop only works while this PC, the network and the bot are up. So every open trade also
# gets a resting stop order at Alpaca, BACKUP_GAP % beyond the bot's stop: normally the bot exits first;
# if the bot is down, Alpaca exits for it.
BACKUP_GAP = 0.5


def _tick(sym, base, h):
    if mkt(sym) == "stock":
        return 0.01
    try:
        return float(call("GET", base + "/v2/assets/" + sym, h).get("price_increment") or 1e-9)
    except Exception:
        return 1e-9


def _round_px(px, tick, up):
    n = px / tick
    n = math.ceil(n - 1e-6) if up else math.floor(n + 1e-6)
    dec = max(0, -int(math.floor(math.log10(tick) + 1e-9)))
    return ("%." + str(dec) + "f") % (n * tick)


def held_qty(sym, base, h):
    """Alpaca's exact quantity string for the position (no sign), or None if there is no position."""
    try:
        return call("GET", base + "/v2/positions/" + sym.replace("/", ""), h)["qty"].lstrip("-")
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None
        raise


def place_stop(sym, side, stop_px, qty, base, h, tick):
    close_side = "sell" if side == "long" else "buy"
    up = close_side == "buy"
    body = {"symbol": sym, "qty": qty, "side": close_side, "time_in_force": MK[mkt(sym)]["tif"],
            "stop_price": _round_px(stop_px, tick, up)}
    if mkt(sym) == "crypto":  # Alpaca crypto has no plain stop order: stop-limit with 1% room to fill in a fast drop
        body.update(type="stop_limit",
                    limit_price=_round_px(stop_px * (0.99 if close_side == "sell" else 1.01), tick, up))
    else:
        body["type"] = "stop"
    return call("POST", base + "/v2/orders", h, body)


def cancel_wait(oid, base, h):
    """Cancel an order and wait until Alpaca confirms; returns its final state (it may have filled first)."""
    try:
        call("DELETE", base + "/v2/orders/" + oid, h)
    except RuntimeError:
        pass
    o = None
    for _ in range(12):
        o = call("GET", base + "/v2/orders/" + oid, h)
        if o["status"] in ("filled", "canceled", "expired", "rejected"):
            break
        time.sleep(0.5)
    return o


def cancel_open(sym, base, h):
    """Cancel resting orders on sym left over from an earlier run (old safety stops)."""
    n = 0
    for o in call("GET", base + "/v2/orders?status=open&limit=500", h) or []:
        if o["symbol"].replace("/", "") == sym.replace("/", ""):
            cancel_wait(o["id"], base, h)
            n += 1
    if n:
        log("cancel_old_orders", symbol=sym, n=n)
    return n


def cmd_enter(a):
    base, h = env()
    p = MK[mkt(a.symbol)]
    bid, ask = quote(a.symbol, h)
    print("%s bid %s ask %s. Buy $%.2f at market, stop -%g%%, target +%g%% (trail if still rising)."
          % (a.symbol, bid, ask, a.usd, p["stop"], p["target"]))
    if not a.yes and input("Type yes to buy: ").strip().lower() != "yes":
        print("Nothing sent.")
        return 1
    o = buy(a.symbol, a.usd, base, h)
    return 0 if o["status"] == "filled" else 1


def momentum_up(sym, h, side="long"):
    """Still moving our way? Long: last 1-minute close above its 9-EMA and up over 5 minutes.
    Short: below the 9-EMA and down over 5 minutes."""
    start = (now_utc() - dt.timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = {"symbols": sym, "timeframe": "1Min", "start": start, "limit": 100}
    if mkt(sym) == "crypto":
        url = DATA + "/bars?" + urllib.parse.urlencode(q)
    else:
        q["feed"] = "iex"
        url = STOCK_DATA + "/bars?" + urllib.parse.urlencode(q)
    bs = (call("GET", url, h).get("bars") or {}).get(sym, [])
    if len(bs) < 12:
        return False
    c = [b["c"] for b in bs]
    if side == "short":
        return c[-1] < ema(c, 9)[-1] and c[-1] < c[-6]
    return c[-1] > ema(c, 9)[-1] and c[-1] > c[-6]


def cmd_manage(a):
    base, h = env()
    deadline = None
    if mkt(a.symbol) == "stock":
        ck = clock(base, h)
        deadline = ck["close"] - dt.timedelta(minutes=5) if ck["is_open"] else None
    if a.qty and a.entry:
        return manage(a.symbol, a.entry, a.qty, base, h, a.every, deadline, a.side)
    p = position(a.symbol, base, h)
    if not p:
        print("No %s position." % a.symbol)
        return 1
    side = "short" if float(p["qty"]) < 0 else "long"
    return manage(a.symbol, float(p["avg_entry_price"]), str(abs(float(p["qty"]))), base, h, a.every, deadline, side)


def manage(symbol, entry, qty, base, h, every, deadline=None, side="long", best0=0.0):
    """Hold `qty` opened at `entry` until the stop, the target, the trailing stop, or (stocks) the deadline.
    Works in 'gain %' so the same rules serve a long (price up = gain) and a short (price down = gain)."""
    p = MK[mkt(symbol)]
    d = 1 if side == "long" else -1

    def px_at(g):  # price at which the trade shows gain g%
        return entry * (1 + d * g / 100)

    stop_g, trailing, best, armed = -p["stop"], False, max(0.0, best0), False  # best0: high so far (restarts)
    log("manage", symbol=symbol, side=side, qty=qty, entry=float("%.6g" % entry), stop=float("%.6g" % px_at(stop_g)),
        target=float("%.6g" % px_at(p["target"])), deadline=deadline.strftime("%H:%M UTC") if deadline else None)
    last_note = 0
    tick = _tick(symbol, base, h)
    guard = dict(id=None, stop=None, checked=0.0, failed=0.0)
    try:
        cancel_open(symbol, base, h)
    except Exception as e:
        log("safety_stop_error", symbol=symbol, error=str(e)[:200])

    def guard_done(o):  # the safety stop at Alpaca closed the trade
        fill = float(o["filled_avg_price"])
        log("exit", symbol=symbol, side=side, reason="safety stop at Alpaca", status=o["status"],
            qty=o["filled_qty"], price=o["filled_avg_price"])
        log("done", symbol=symbol, side=side, pnl_usd=round(d * (fill - entry) * float(o["filled_qty"]), 2),
            pnl_pct=round(d * (fill / entry - 1) * 100, 2))
        return 0

    def guard_set(stop_px):
        """Keep the Alpaca safety stop at stop_px; returns the filled order if it already fired."""
        if guard["id"] and abs(stop_px / guard["stop"] - 1) < 0.001:
            return None
        if not guard["id"] and time.time() - guard["failed"] < 60:
            return None
        if guard["id"]:
            o = cancel_wait(guard["id"], base, h)
            guard.update(id=None, stop=None)
            if o and o["status"] == "filled":
                return o
        q = held_qty(symbol, base, h)
        if not q:
            return None
        try:
            o = place_stop(symbol, side, stop_px, q, base, h, tick)
            guard.update(id=o["id"], stop=stop_px)
            log("safety_stop", symbol=symbol, stop=o.get("stop_price"), limit=o.get("limit_price"), id=o["id"])
        except Exception as e:
            guard["failed"] = time.time()
            log("safety_stop_error", symbol=symbol, error=str(e)[:200])
        return None

    def guard_check():
        """Every 30 s: did the safety stop fire (or get cancelled/expire)?"""
        if not guard["id"] or time.time() - guard["checked"] < 30:
            return None
        guard["checked"] = time.time()
        o = call("GET", base + "/v2/orders/" + guard["id"], h)
        if o["status"] == "filled":
            return o
        if o["status"] in ("canceled", "expired", "rejected"):
            guard.update(id=None, stop=None)
        return None

    def close(px, reason):
        if guard["id"]:
            try:
                o = cancel_wait(guard["id"], base, h)
                guard.update(id=None, stop=None)
                if o and o["status"] == "filled":
                    return guard_done(o)
            except Exception as e:
                log("safety_stop_error", symbol=symbol, error=str(e)[:200])
        for attempt in range(1, 1000):  # a failed sell must never kill the bot while it holds the position
            try:
                o = sell_all(symbol, qty, base, h, reason, side)
                break
            except Exception as e:
                log("sell_error", symbol=symbol, attempt=attempt, error=str(e)[:200])
                time.sleep(min(60, every * attempt))
        fill = float(o["filled_avg_price"] or px)
        log("done", symbol=symbol, side=side, pnl_usd=round(d * (fill - entry) * float(qty), 2),
            pnl_pct=round(d * (fill / entry - 1) * 100, 2))
        return 0

    while True:
        try:
            px = price_now(symbol, h)
        except Exception as e:  # network blip: keep watching
            log("quote_error", error=str(e)[:120])
            time.sleep(every)
            continue
        g = d * (px / entry - 1) * 100
        best = max(best, g)
        if p.get("arm") and best >= p["arm"]:  # up +arm%: trail the stop 1.5% under the best, never below +floor%
            if not armed:
                log("trail_on", symbol=symbol, price=px, best_pct=round(best, 2))
            armed = True
            stop_g = max(stop_g, best - p["trail"], p["floor"])
        if trailing:
            stop_g = max(stop_g, best - p["trail"], p["lock"])
        try:
            o = guard_check() or guard_set(px_at(stop_g - BACKUP_GAP))
            if o:
                return guard_done(o)
        except Exception as e:  # network blip: the order at Alpaca (if any) keeps protecting the trade
            log("safety_stop_error", symbol=symbol, error=str(e)[:200])
        if deadline and now_utc() >= deadline:
            return close(px, "flat before the close")
        if g <= stop_g:
            return close(px, "trailing stop" if (trailing or armed) else "stop-loss -%g%%" % p["stop"])
        if g >= p["max"]:
            return close(px, "max target +%g%%" % p["max"])
        if not trailing and g >= p["target"]:
            if momentum_up(symbol, h, side):
                trailing = True
                stop_g = max(p["lock"], best - p["trail"])
                log("let_it_run", symbol=symbol, price=px, new_stop=float("%.6g" % px_at(stop_g)))
            else:
                return close(px, "target +%g%%" % p["target"])
        if time.time() - last_note > 300:
            log("watch", symbol=symbol, side=side, price=px, pnl_pct=round(g, 2), stop=float("%.6g" % px_at(stop_g)),
                target=float("%.6g" % px_at(p["target"])), trailing=trailing or armed)
            last_note = time.time()
        time.sleep(every)


# ------------------------------------------------------------------ the loop

def clock(base, h):
    c = call("GET", base + "/v2/clock", h)
    return dict(is_open=c["is_open"], close=dt.datetime.fromisoformat(c["next_close"]).astimezone(dt.timezone.utc))


def entry_window(ck):
    """Stock buys only 9:45 New York until 30 minutes before the close (handles 1 p.m. early closes)."""
    if not ck["is_open"]:
        return False, "market closed"
    et = to_et(now_utc())
    if et.hour * 60 + et.minute < 585:
        return False, "before 9:45"
    if now_utc() > ck["close"] - dt.timedelta(minutes=30):
        return False, "last 30 min before close"
    return True, ""


def next_check(stocks_open, offset=8):
    """Just after the next candle closes: every 5 minutes while stocks trade (5-min candles), else every
    15 minutes (crypto's 15-min candles). `offset` seconds gives the data feed time to publish the candle."""
    step = 5 if stocks_open else 15
    now = now_utc()
    m = (now.minute // step + 1) * step
    t = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(minutes=m, seconds=offset)
    return t


def today_stats():
    """Trades opened and P&L closed today (New York day), from the bot's own log."""
    today, n, pnl = to_et(now_utc()).date(), 0, 0.0
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if to_et(parse_t(ev["at"])).date() != today:
                continue
            if ev["event"] == "entry" and ev.get("status") == "filled":
                n += 1
            elif ev["event"] == "done":
                pnl += float(ev.get("pnl_usd") or 0)
    return n, pnl


def _run_trade(sym, entry, qty, base, h, every, deadline, side, best0=0.0):
    """Manage one trade in its own thread. If manage() ever fails, retry while the position still exists
    (the safety stop at Alpaca protects it in between)."""
    try:
        while True:
            try:
                manage(sym, entry, qty, base, h, every, deadline, side, best0)
                break
            except Exception as e:
                log("manage_error", symbol=sym, error=str(e)[:150])
                time.sleep(30)
                try:
                    if not held_qty(sym, base, h):
                        break
                except Exception:
                    pass
    finally:
        with OPEN_LOCK:
            OPEN.pop(sym, None)


def start_trade(sym, entry, qty, usd, side, base, h, every, deadline, best0=0.0):
    with OPEN_LOCK:
        OPEN[sym] = dict(usd=usd, side=side, sector=sector(sym))
    threading.Thread(target=_run_trade, args=(sym, entry, qty, base, h, every, deadline, side, best0),
                     daemon=True, name=sym).start()


def adopt_open_trades(base, h, every):
    """After a restart: every trade the bot opened that has no 'done' yet and still exists at Alpaca
    is managed again with the same rules (same entry, stop, target, close deadline)."""
    if not LOG.exists():
        return
    opened, best = {}, {}
    for line in LOG.read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev["event"] == "entry" and ev.get("status") == "filled":
            opened[ev["symbol"]] = ev
            best[ev["symbol"]] = 0.0
        elif ev["event"] == "done":
            opened.pop(ev.get("symbol"), None)
        elif ev["event"] in ("watch", "trail_on") and ev.get("symbol") in opened:  # remember the high so far
            best[ev["symbol"]] = max(best[ev["symbol"]], float(ev.get("pnl_pct", ev.get("best_pct")) or 0))
    ck = None
    for sym, ent in opened.items():
        side = ent.get("side", "long")
        pos = position(sym, base, h)
        if not pos or (float(pos["qty"]) < 0) != (side == "short"):
            log("adopt_skip", symbol=sym, reason="no matching position at Alpaca")
            continue
        qty = str(min(abs(float(pos["qty"])), abs(float(ent["qty"]))))
        deadline = None
        if mkt(sym) == "stock":
            ck = ck or clock(base, h)
            deadline = ck["close"] - dt.timedelta(minutes=5) if ck["is_open"] else now_utc()
        log("adopt", symbol=sym, side=side, qty=qty, entry=ent["price"], best_pct=round(best[sym], 2))
        start_trade(sym, float(ent["price"]), qty, abs(float(pos["cost_basis"])), side, base, h, every, deadline,
                    best[sym])


def cmd_auto(a):
    """Every N minutes: scan crypto (24/7) and, while NYSE is open, the stock list; buy the best symbol that
    qualifies; manage it to the exit; then keep going until a daily limit is hit."""
    base, h = env()
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    max_trades = int(cfg.get("max_trades_per_day", 6))
    max_loss = float(cfg.get("max_daily_loss_usd", 6))
    global BUDGET
    BUDGET = a.usd
    log("auto_start", every_min=("5 open / 15 closed" if a.align else a.every_min), usd=a.usd, rule=RULE,
        max_trades=max_trades, max_daily_loss=max_loss,
        sizing="risk %g%% of pool per trade, max %d%%/position, %d%%/sector, %d open"
               % (RISK_PCT, MAX_POS_PCT, MAX_SECTOR_PCT, MAX_OPEN))
    adopt_open_trades(base, h, a.every)
    while True:
        t0 = time.time()
        try:
            ck = clock(base, h)
        except Exception as e:
            log("scan_error", error="clock: " + str(e)[:150])
            ck = dict(is_open=False, close=now_utc())
        rows = []
        try:
            rows += scan(h, base)
        except Exception as e:
            log("scan_error", error="crypto: " + str(e)[:150])
        if ck["is_open"]:
            try:
                rows += scan_stocks(h, base)
            except Exception as e:
                log("scan_error", error="stocks: " + str(e)[:150])
        if not rows:
            time.sleep(60)
            continue
        rows.sort(key=lambda r: r["score"], reverse=True)
        in_window, why_not = entry_window(ck)
        n, pnl = today_stats()
        with OPEN_LOCK:
            held = {k: dict(v) for k, v in OPEN.items()}
        blocked = ("daily trade limit (%d)" % max_trades if n >= max_trades
                   else "daily loss limit ($%.2f)" % pnl if pnl <= -max_loss
                   else "%d positions open (max %d)" % (len(held), MAX_OPEN) if len(held) >= MAX_OPEN else None)
        # long candidates (crypto + stocks) and short candidates (stocks only), best score first
        cands = ([(r, "long", r["score"]) for r in rows if qualifies(r)]
                 + [(r, "short", r["short"]["score"]) for r in rows if qualifies_short(r)])
        cands.sort(key=lambda c: c[2], reverse=True)
        decisions, bought = {}, None
        for r, side, _ in cands:
            sym = r["symbol"]
            if sym in held:
                decisions[sym] = "holding"
                continue
            if blocked:
                decisions[sym] = "skipped: " + blocked
                continue
            if r["mkt"] == "stock" and not in_window:
                decisions[sym] = "skipped: " + why_not
                continue
            try:
                bid, ask = quote(sym, h)
            except Exception as e:
                decisions[sym] = "skipped: no quote"
                continue
            spread = (ask - bid) / ask * 100 if bid > 0 and ask > 0 else 0.0
            if spread > (MEME_SPREAD_PCT if sym in MEME else MAX_SPREAD_PCT):
                decisions[sym] = "skipped: spread %.2f%%" % spread
                log("skip", symbol=sym, reason="spread %.2f%%" % spread)
                continue
            rec = r if side == "long" else r["short"]
            px = (ask if side == "long" else bid) or r["price"]
            try:
                usd, qty, why = size_trade(r, side, px, held)
            except Exception as e:  # a sizing bug must never stop the bot
                decisions[sym] = "skipped: sizing error"
                log("size_error", symbol=sym, error=str(e)[:150])
                continue
            if usd < MIN_ORDER_USD or (r["mkt"] == "stock" and qty < 1):
                decisions[sym] = "skipped: no room (%s)" % why["text"]
                log("skip", symbol=sym, reason="size $%.0f too small: %s" % (usd, why["text"]))
                continue
            log("signal", symbol=sym, market=r["mkt"], side=side, price=(ask if side == "long" else bid) or r["price"],
                rsi=round(r["rsi"]), hit=round(rec["hit"]), record="%d/%d" % (rec["win"], rec["loss"]),
                chg4h=round(r["chg4h"], 2))
            log("size", symbol=sym, side=side, usd=round(usd, 2), qty=qty or None, risk_usd=why["risk"],
                conviction=why["conf"], sector=why["sector"], why=why["text"])
            try:
                if r["mkt"] == "crypto":
                    o = buy(sym, usd, base, h)
                else:
                    o = buy_shares(sym, qty, base, h) if side == "long" else short(sym, qty, base, h)
            except Exception as e:
                decisions[sym] = "%s failed: %s" % ("buy" if side == "long" else "short", str(e)[:80])
                log("buy_error", symbol=sym, side=side, error=str(e)[:150])
                continue
            if o["status"] != "filled":
                decisions[sym] = "%s %s" % ("buy" if side == "long" else "short", o["status"])
                continue
            decisions[sym] = "%s %s @ %s" % ("BOUGHT" if side == "long" else "SHORTED", o["filled_qty"],
                                             o["filled_avg_price"])
            bought = (sym, o, side, float(o["filled_qty"]) * float(o["filled_avg_price"]))
            break
        nxt = next_check(ck["is_open"]) if a.align else now_utc() + dt.timedelta(minutes=a.every_min)
        run_id = log_run(rows, decisions, stocks_open=ck["is_open"],
                         next_utc=nxt.strftime("%Y-%m-%d %H:%M:%S"), took=round(time.time() - t0, 1))
        top = rows[0]
        log("scan", run=run_id, pairs=len(rows), stocks="open" if ck["is_open"] else "closed",
            qualifying=len(cands), best=top["symbol"], best_hit=round(top["hit"]), best_trend=top["trend"],
            took_s=round(time.time() - t0, 1), next=nxt.strftime("%H:%M:%S"))
        if bought:  # manage it in the background; keep scanning for the next position
            sym, o, side, usd = bought
            deadline = ck["close"] - dt.timedelta(minutes=5) if mkt(sym) == "stock" else None
            start_trade(sym, float(o["filled_avg_price"]), o["filled_qty"], usd, side, base, h, a.every, deadline)
        time.sleep(max(1.0, (nxt - now_utc()).total_seconds()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("stocks")
    p = sub.add_parser("enter")
    p.add_argument("symbol")
    p.add_argument("--usd", type=float, default=100)
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("manage")
    p.add_argument("symbol")
    p.add_argument("--every", type=float, default=10, help="seconds between price checks")
    p.add_argument("--qty", help="manage only this many units (default: the whole position)")
    p.add_argument("--entry", type=float, help="their entry price (with --qty)")
    p.add_argument("--side", choices=["long", "short"], default="long", help="with --qty/--entry")
    p = sub.add_parser("auto")
    p.add_argument("--usd", type=float, default=100)
    p.add_argument("--every-min", type=float, default=10, help="minutes between scans (only with --no-align)")
    p.add_argument("--no-align", dest="align", action="store_false",
                   help="fixed --every-min gap instead of checking right after each candle closes")
    p.add_argument("--every", type=float, default=10, help="seconds between price checks once in")
    a = ap.parse_args()
    return {"scan": cmd_scan, "stocks": cmd_stocks, "enter": cmd_enter, "manage": cmd_manage,
            "auto": cmd_auto}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
