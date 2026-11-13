"""Sci-fi view of the bot in plain business language: trader/hud.html.
Rebuilt with the dashboard after every scan and trade; `python hud.py` rebuilds it by hand.
Add #rec to the URL to stop the 60-second auto-reload while screen recording."""
import datetime as dt
import json

import dashboard as D

OUT = D.TRADER / "hud.html"

# Same thresholds as crypto_bot.MK (kept here so the page never imports the bot)
RULES = {
    "crypto": dict(stop=2.0, target=4.0, max=6.0, rsi=(50, 72), atr=(0.15, 1.2)),
    "stock": dict(stop=1.0, target=2.0, max=3.0, rsi=(50, 72), atr=(0.08, 1.0)),
}
MIN_DECIDED = 4

ROLES = {
    "Scheduler": ("Market Hours", "Knows which markets are open right now"),
    "Crypto scanner": ("Crypto Analyst", "Reads every crypto chart, every 10 minutes, 24/7"),
    "Stock scanner": ("Stock Analyst", "Reads ~50 major US stocks while the market is open"),
    "Signal picker": ("Deal Picker", "Only passes assets that clear all 4 checks"),
    "Risk Officer": ("Risk Officer", ""),
    "Risk guard": ("Risk Officer", "Caps losses before any money moves"),
    "Order executor": ("Trader", "Sizes each trade by risk: stronger setups get more, capped per asset and sector"),
    "Position manager": ("Portfolio Manager", "Sells automatically at the profit target or loss limit"),
}


def _short(s):
    return (s or "").replace("/USD", "")


def _checks(r, m):
    """The bot's 4 buy checks, in plain words: (name, passed)."""
    p = RULES[m]
    be = p["stop"] / (p["stop"] + p["target"]) * 100
    decided = int(r.get("win", 0)) + int(r.get("loss", 0))
    return [("Rising trend", bool(r["trend"])),
            ("Healthy momentum", p["rsi"][0] <= r["rsi"] <= p["rsi"][1]),
            ("Normal price swings", p["atr"][0] <= r.get("atr", 0) <= p["atr"][1]),
            ("Winning track record", decided >= MIN_DECIDED and r["hit"] >= be)]


def _feed(events):
    out = []
    for x in events:
        ev, t = x["event"], D.et(x["at"], "%I:%M %p")[:8]
        sym = _short(x.get("symbol"))
        if ev == "scan":
            q = int(x.get("qualifying") or 0)
            line, c = "Checked %s assets: %s" % (x.get("pairs"), "%d ready to buy" % q if q else "none ready yet"), "scan"
        elif ev == "signal":
            line, c = "Buy signal on %s at $%s" % (sym, x.get("price")), "sig"
        elif ev == "entry":
            short = x.get("side") == "short"
            line, c = (("Shorted %s: %s shares at $%s (profits if the price falls)" if short else
                        "Bought %s: %s units at $%s") % (sym, x.get("qty"), x.get("price"))
                       if x.get("status") == "filled" else "%s of %s %s" % ("Short" if short else "Buy", sym,
                                                                          x.get("status"))), "buy"
        elif ev == "exit":
            line, c = "%s %s (%s)" % ("Bought back" if x.get("side") == "short" else "Sold", sym,
                                      x.get("reason")), "sig"
        elif ev == "done":
            p = float(x.get("pnl_usd") or 0)
            line, c = "Trade closed on %s: %s $%.2f" % (sym, "PROFIT" if p >= 0 else "LOSS", abs(p)), \
                ("buy" if p >= 0 else "err")
        elif ev == "let_it_run":
            line, c = "Target hit and still rising: letting the profit run", "buy"
        elif ev == "skip":
            line, c = "Skipped %s: %s" % (sym, x.get("reason")), "sig"
        elif ev == "auto_start":
            line, c = "System online", "scan"
        elif ev.endswith("error"):
            line, c = "Connection hiccup, retrying automatically", "err"
        else:
            continue
        out.append(dict(t=t, x=line, c=c))
    return out[-12:]


def build():
    events = D._jsonl(D.TRADER / "crypto_log.jsonl")
    runs = D._jsonl(D.LOGS / "runs.jsonl")
    rows, _ = D._latest_rows()
    trades = D._trades(events)
    open_t = [t for t in trades if t["status"] == "open"]
    closed = [t for t in trades if t["status"] == "closed"]
    cards = D._agent_cards(events, runs, rows, open_t)
    last = runs[-1] if runs else {}
    qual = set(last.get("qualifying") or [])
    qual_s = set(last.get("qualifying_short") or [])
    stocks_open = bool(last.get("stocks_open"))
    every = next((D._num(x.get("every_min")) for x in reversed(events) if x["event"] == "auto_start"), 10.0)
    budget = next((float(x.get("usd") or 100) for x in reversed(events) if x["event"] == "auto_start"), 100.0)
    raw_every = next((x.get("every_min") for x in reversed(events) if x["event"] == "auto_start"), 10)
    if isinstance(raw_every, str):   # aligned mode: right after each candle closes
        cadence_long = "Repeats every 5 minutes while US stocks trade, every 15 minutes otherwise, around the clock."
        cadence_short = "Every 5 min (15 when stocks are closed)"
    else:
        cadence_long = "Repeats every %g minutes, around the clock." % every
        cadence_short = "Every %g minutes" % every
    cfg = json.loads((D.TRADER / "config.json").read_text()) if (D.TRADER / "config.json").exists() else {}
    max_t, max_l = int(cfg.get("max_trades_per_day", 6)), float(cfg.get("max_daily_loss_usd", 6))

    syms = []
    for f, m in (("crypto_scan.json", "crypto"), ("stock_scan.json", "stock")):
        p = D.TRADER / f
        try:
            data = json.loads(p.read_text()) if p.exists() else []
        except ValueError:
            data = []
        for r in data:
            ck = _checks(r, m)
            syms.append(dict(s=_short(r["symbol"]), m=m, p=r["price"], c4=round(r["chg4h"], 2),
                             up=bool(r["trend"]), hit=round(r["hit"]), w=r.get("win", 0), l=r.get("loss", 0),
                             sc=round(r["score"], 1), q=r["symbol"] in qual, ck=[c[1] for c in ck],
                             sq=r["symbol"] in qual_s,
                             sh=dict(w=r["short"]["win"], l=r["short"]["loss"], hit=round(r["short"]["hit"]),
                                     down=r["short"]["trend"]) if r.get("short") else None,
                             n=sum(c[1] for c in ck), sp=r.get("spark") or []))
    live = [s for s in syms if s["m"] == "crypto" or stocks_open]

    # today, New York
    now = dt.datetime.now(dt.timezone.utc)
    w, hh, mm = (int(x) for x in D.et(now.strftime("%Y-%m-%d %H:%M:%S"), "%w %H %M").split()[:3])
    today = D._today_et(now.strftime("%Y-%m-%d %H:%M:%S"))
    todays = [x for x in events if D._today_et(x["at"]) == today]
    n_today = sum(1 for x in todays if x["event"] == "entry" and x.get("status") == "filled")
    pnl_today = sum(float(x.get("pnl_usd") or 0) for x in todays if x["event"] == "done")

    # plain-English briefing
    brief = []
    if live:
        down = sum(1 for s in live if s["c4"] < 0)
        rising = sum(1 for s in live if s["up"])
        mood = "mostly FALLING" if down > len(live) * 0.6 else "mostly RISING" if down < len(live) * 0.4 else "MIXED"
        brief.append(("mood", "The market is %s: %d of %d assets are down over the last 4 hours, and only %d "
                              "are in a rising trend." % (mood, down, len(live), rising)))
    for t in open_t:
        r = RULES["crypto" if "/" in t["symbol"] else "stock"]
        if t.get("side") == "short":
            brief.append(("trade", "IN A TRADE: shorted %s at $%s, a bet that the price falls. It buys back "
                                   "automatically when the price is %g%% lower (profit) or %g%% higher (loss)."
                          % (_short(t["symbol"]), t["entry"], r["target"], r["stop"])))
        else:
            brief.append(("trade", "IN A TRADE: bought %s at $%s. It sells automatically at +%g%% profit or -%g%% "
                                   "loss." % (_short(t["symbol"]), t["entry"], r["target"], r["stop"])))
    if qual or qual_s:
        if qual:
            brief.append(("go", "BUY SIGNAL: %s passed all 4 checks." % ", ".join(_short(q) for q in qual)))
        if qual_s:
            brief.append(("go", "SHORT SIGNAL: %s are falling and passed all short checks, so the bot can profit "
                                "from the drop." % ", ".join(sorted(qual_s))))
    elif live and not open_t:
        best = max(live, key=lambda s: (s["n"], s["sc"]))
        brief.append(("wait", "No purchase yet, on purpose. The bot only buys an asset that is rising AND has made "
                              "money with this exact strategy in the past. Closest right now: %s (%d of 4 checks)."
                      % (best["s"], best["n"])))
    if stocks_open:
        brief.append(("clock", "US stock market is OPEN: stocks and crypto are checked every %g minutes. Stocks can "
                               "be bought (bet on a rise) or shorted (bet on a fall) from 9:45 AM to 3:30 PM ET; "
                               "every stock trade is closed before the 4 PM close." % every))
    else:
        when = ("today at 9:30 AM ET" if 1 <= w <= 5 and hh * 60 + mm < 570 else "next trading day at 9:30 AM ET")
        brief.append(("clock", "US stock market is CLOSED, so only crypto is traded (it never closes). Stocks, "
                               "including short selling, resume %s." % when))
    brief.append(("shield", "Safety limits: a $%s pool, up to 4 positions at once. Each trade risks about 0.5%% of the "
                            "pool, more for proven setups and less after losses or for meme coins; no position over "
                            "25%% and no sector over 40%% of the pool. A backup stop sits at Alpaca on every trade. "
                            "The bot stops for the day after %d trades or a $%g loss. Paper money only."
                  % ("{:,.0f}".format(budget), max_t, max_l)))

    # agents in business terms
    agents = []
    crypto = [s for s in syms if s["m"] == "crypto"]
    stocks = [s for s in syms if s["m"] == "stock"]
    for name, st, head, det in cards:
        title, role = ROLES.get(name, (name, ""))
        if name == "Scheduler":
            head = "Stock market OPEN: trading crypto + stocks" if stocks_open and st != "blocked" else \
                ("Stock market closed: crypto only" if st != "blocked" else head)
        elif name == "Crypto scanner" and crypto:
            head = "%d coins checked, %d rising" % (len(crypto), sum(s["up"] for s in crypto))
        elif name == "Stock scanner":
            head = ("%d stocks checked, %d rising" % (len(stocks), sum(s["up"] for s in stocks))
                    if stocks_open else "Resting until 9:30 AM ET")
        elif name == "Signal picker":
            parts = (["Buy: " + ", ".join(_short(q) for q in qual)] if qual else []) + \
                    (["Short: " + ", ".join(sorted(qual_s))] if qual_s else [])
            head = " · ".join(parts) or "Nothing has passed all checks yet"
        elif name == "Position manager" and open_t:
            t = open_t[-1]
            kind = "short" if t.get("side") == "short" else "long"
            head = ("Watching %s (%s): %+.2f%% so far" % (_short(t["symbol"]), kind, t["pnl_pct"])
                    if t.get("pnl_pct") is not None else "Watching %s (%s) every 10 seconds" % (_short(t["symbol"]), kind))
        agents.append(dict(n=title, role=role, st=st, h=head))

    next_ms = None
    if last.get("time_utc"):
        t = dt.datetime.fromisoformat(last["time_utc"]).replace(tzinfo=dt.timezone.utc)
        if last.get("next_utc"):
            t, every_td = dt.datetime.fromisoformat(last["next_utc"]).replace(tzinfo=dt.timezone.utc), dt.timedelta(0)
        else:
            every_td = dt.timedelta(minutes=every)
        next_ms = int((t + every_td).timestamp() * 1000)

    wins = sum(1 for t in closed if float(t.get("pnl_usd") or 0) > 0)
    tr = None
    if open_t:
        t = open_t[-1]
        tr = dict(s=_short(t["symbol"]), entry=t["entry"], pnl=t.get("pnl_pct"), side=t.get("side", "long"))
    opens = [dict(s=_short(t["symbol"]), pnl=t.get("pnl_pct"), side=t.get("side", "long")) for t in open_t]
    equity, run = [0.0], 0.0
    for t in closed:
        run += float(t.get("pnl_usd") or 0)
        equity.append(round(run, 2))
    data = dict(
        stocks_open=stocks_open, last=D.et(last.get("time_utc"), "%I:%M %p") if last else "--", next_ms=next_ms,
        budget=budget, cadence_long=cadence_long, cadence_short=cadence_short, agents=agents, brief=brief, syms=syms, qual=[_short(s) for s in qual], qual_s=sorted(qual_s), trade=tr, opens=opens,
        pnl=round(run, 2), pnl_today=round(pnl_today, 2), n_today=n_today, max_t=max_t, max_l=max_l,
        trades=len(trades), closed=len(closed), wins=wins, scans=len(runs), equity=equity,
        hist=[len(r.get("uptrend", [])) for r in runs[-72:]], feed=_feed(events))
    js = json.dumps(data).replace("</", "<\\/")
    OUT.write_text(TEMPLATE.replace("__DATA__", js), encoding="utf-8")
    return OUT


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trading Desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Bebas+Neue&display=swap" rel="stylesheet">
<style>
:root{--bg:#f4f6fa;--card:#fff;--ink:#0f172a;--ink2:#475569;--mute:#94a3b8;--line:#e5e9f0;
--blue:#2563eb;--blue-s:#eff4ff;--green:#16a34a;--green-s:#ecfdf3;--red:#dc2626;--red-s:#fef2f2;--amber:#d97706;--amber-s:#fffbeb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1280px;margin:0 auto;padding:24px 16px 40px}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:42px;height:42px;border-radius:10px;background:var(--blue);color:#fff;display:grid;place-items:center;font-weight:800;font-size:16px}
h1{font-size:22px;margin:0;font-weight:800;letter-spacing:-.01em}
.sub{color:var(--ink2);font-size:13.5px}
.hdr-r{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border-radius:99px;background:var(--card);border:1px solid var(--line);font-size:13px;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mute);flex:none}
.dot.g{background:var(--green)}.dot.r{background:var(--red)}.dot.a{background:var(--amber)}.dot.b{background:var(--blue)}
.clock{font-weight:700;font-variant-numeric:tabular-nums}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 1px 2px rgba(15,23,42,.04)}
h2{font-size:15px;margin:0 0 4px;font-weight:700}
.hint{color:var(--ink2);font-size:13.5px;margin:0 0 14px}
.status{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;border-left:5px solid var(--sc);background:var(--ss);margin-bottom:16px}
.status b{font-size:18px;color:var(--sc)}
.status .meta{color:var(--ink2);font-size:13.5px}
.status .meta span{font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums}
.row{display:grid;gap:16px;margin-bottom:16px}
.r-brief{grid-template-columns:minmax(0,1.7fr) minmax(0,1fr)}
.r-main{grid-template-columns:minmax(0,1fr) minmax(0,2fr)}
.r-two{grid-template-columns:1fr 1fr}
.bl{display:flex;gap:12px;align-items:flex-start;padding:8px 0;border-top:1px solid var(--line)}
.bl:first-of-type{border-top:0;padding-top:2px}
.bi{flex:none;width:28px;height:28px;border-radius:8px;display:grid;place-items:center;font-size:14px;background:var(--bs);color:var(--bc)}
.steps{list-style:none;margin:0;padding:0;counter-reset:s}
.steps li{display:flex;gap:12px;padding:8px 0;border-top:1px solid var(--line)}
.steps li:first-child{border-top:0;padding-top:2px}
.steps li::before{counter-increment:s;content:counter(s);flex:none;width:26px;height:26px;border-radius:50%;background:var(--blue-s);color:var(--blue);display:grid;place-items:center;font-weight:700;font-size:13px}
.steps b{display:block;font-size:14px}.steps span{color:var(--ink2);font-size:13.5px}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:16px}
.kpi small{display:block;color:var(--ink2);font-size:13px;font-weight:600}
.kpi b{display:block;font-size:28px;font-weight:800;margin:4px 0 2px;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.kpi em{font-style:normal;color:var(--mute);font-size:12.5px}
.pos{color:var(--green)}.neg{color:var(--red)}
.team{list-style:none;margin:0;padding:0}
.team li{display:grid;grid-template-columns:auto 1fr auto;gap:4px 10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.team li:first-child{border-top:0;padding-top:0}
.team .nm{font-weight:700;font-size:14px}
.team .role{grid-column:2/4;color:var(--mute);font-size:12.5px;margin-top:-4px}
.team .hd{grid-column:2/4;font-size:13.5px;color:var(--ink2)}
.tag{font-size:11.5px;font-weight:700;padding:2px 8px;border-radius:99px;background:var(--ts);color:var(--tc)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:12px;color:var(--ink2);font-weight:600;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px;border-bottom:1px solid var(--line);white-space:nowrap;font-size:14px}
tr:last-child td{border-bottom:0}
td.c,th.c{text-align:center} td.n,th.n{text-align:right}
.sym{font-weight:700}.type{color:var(--mute);font-size:12px;margin-left:6px}
.ok{color:var(--green);font-weight:800}.no{color:#cbd5e1;font-weight:800}
.badge{font-size:12px;font-weight:700;padding:3px 9px;border-radius:99px}
.badge.ready{background:var(--green-s);color:var(--green)}.badge.near{background:var(--amber-s);color:var(--amber)}.badge.far{background:#f1f5f9;color:var(--ink2)}
.spark{width:110px;height:30px;display:block}
.bars .br{display:grid;grid-template-columns:110px 1fr 44px;gap:10px;align-items:center;margin:9px 0;font-size:13.5px}
.bars .track{height:22px;background:#f1f5f9;border-radius:6px;overflow:hidden}
.bars .fill{height:100%;border-radius:6px;transform-origin:left;animation:grow .8s ease-out both}
.bars .v{font-weight:700;text-align:right}
@keyframes grow{from{transform:scaleX(0)}}
.hist{width:100%;height:170px;display:block}
.heat{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:6px}
.tile{padding:8px 6px;border-radius:8px;text-align:center;font-size:13px;font-weight:600}
.tile b{display:block;font-size:13px}
.tile.off{opacity:.55}
.tile.q{outline:2px solid var(--green);outline-offset:1px}
.sect{font-size:12px;font-weight:700;color:var(--ink2);margin:14px 0 8px;text-transform:uppercase;letter-spacing:.04em}
.log{list-style:none;margin:0;padding:0}
.log li{display:flex;gap:12px;padding:7px 0;border-top:1px solid var(--line);font-size:14px}
.log li:first-child{border-top:0}
.log .t{color:var(--mute);width:72px;flex:none;font-variant-numeric:tabular-nums}
.foot{color:var(--mute);font-size:12.5px;text-align:center;margin-top:8px}
.col{display:flex;flex-direction:column;gap:16px;min-width:0}
/* the AI team: isometric war room, everyone linked to the AI core */
.crew-card{margin-bottom:16px;color:#e2e8f0;border:1px solid #0f2f55;
 background:linear-gradient(180deg,#0b2e57 0%,#082040 55%,#061a33 100%)}
.crew-hd{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap}
.crew-hd h2{font-size:20px;margin:0;color:#fff}
.crew-card .hint{color:#9fc3e6}
.moods{display:flex;gap:12px;flex-wrap:wrap;font-size:12.5px;color:#cfe3f7}
.moods span{display:inline-flex;align-items:center;gap:5px}
.m{width:9px;height:9px;border-radius:50%;display:inline-block;box-shadow:0 0 8px currentColor}
.m.on{background:#38bdf8;color:#38bdf8}.m.go{background:#4ade80;color:#4ade80}.m.wait{background:#fbbf24;color:#fbbf24}.m.idle{background:#64748b;color:#64748b}.m.blocked{background:#f87171;color:#f87171}
.crew{margin-top:10px}
.room{display:block;width:100%;height:auto;border-radius:14px}
.room *{transform-box:fill-box}
.room .vb{transform-box:view-box}
.agent{cursor:pointer}
.agent:hover .lbl rect,.agent.on .lbl rect{stroke-width:2.2}
.agent:focus{outline:none}
.flow{stroke-dasharray:3 11;animation:flow 1.6s linear infinite}
.flow.fast{animation-duration:.7s}
@keyframes flow{to{stroke-dashoffset:-28}}
.ring{animation:ring 2.2s ease-in-out infinite}
@keyframes ring{50%{opacity:.35;transform:scale(1.15)}}
.chipglow{animation:chip 2.8s ease-in-out infinite}
@keyframes chip{50%{opacity:.45}}
.bobp{animation:bobp 3.6s ease-in-out infinite}
@keyframes bobp{50%{transform:translateY(-1.5px)}}
.gesture{animation:gest 1.4s ease-in-out infinite;transform-origin:0 0}
@keyframes gest{50%{transform:rotate(-8deg)}}
.lblf{animation:lblf 4s ease-in-out infinite}
@keyframes lblf{50%{transform:translateY(-3px)}}
.dim{opacity:.45}
.alarm{animation:alarm .8s infinite}
@keyframes alarm{50%{opacity:.25}}
.drawl{stroke-dasharray:1;stroke-dashoffset:1;animation:drawl 5s ease-out infinite}
@keyframes drawl{0%{stroke-dashoffset:1}55%,100%{stroke-dashoffset:0}}
.pad{animation:pad 3s ease-in-out infinite}
@keyframes pad{50%{opacity:.4}}
.roster{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;margin-top:14px}
.rm{display:grid;grid-template-columns:auto 1fr auto;gap:2px 10px;align-items:center;padding:10px 12px;border-radius:12px;
 background:rgba(255,255,255,.05);border:1px solid rgba(125,211,252,.18);border-left:3px solid var(--col);cursor:pointer;transition:background .2s}
.rm:hover,.rm.hl{background:rgba(255,255,255,.1)}
.rm .dot{width:10px;height:10px;border-radius:50%;background:var(--mc);box-shadow:0 0 8px var(--mc)}
.rm b{font-size:14px;color:#fff}.rm b small{font-weight:600;color:var(--col);font-size:12px;margin-left:6px;filter:brightness(1.5)}
.rm .mood{font-size:11.5px;font-weight:700;color:var(--mc)}
.rm .act{grid-column:2/4;font-size:12.5px;color:#cfe3f7;line-height:1.3}
.radar{width:100%;max-width:520px;display:block;margin:0 auto}
.radar .pt{cursor:pointer;transition:r .15s}
.radar .pt:hover,.radar .pt.on{stroke:#0f172a;stroke-width:2}
.legend{display:flex;gap:16px;justify-content:center;flex-wrap:wrap;font-size:13px;color:var(--ink2);margin-top:8px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.tile{cursor:pointer}
.tip{position:fixed;z-index:20;pointer-events:none;background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 14px;width:260px;
 box-shadow:0 10px 30px rgba(15,23,42,.14);font-size:13.5px;opacity:0;transform:translateY(4px);transition:opacity .12s,transform .12s}
.tip.show{opacity:1;transform:none}
.tip h3{margin:0;font-size:16px;display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.tip h3 small{font-size:12px;color:var(--mute);font-weight:600}
.tip .px{margin:2px 0 8px;color:var(--ink2)}
.tip ul{list-style:none;margin:0 0 8px;padding:0}
.tip li{display:flex;justify-content:space-between;padding:2px 0}
.tip .rec{color:var(--ink2);font-size:12.5px;border-top:1px solid var(--line);padding-top:7px}
.fade{animation:fade .5s ease-out both}@keyframes fade{from{opacity:0;transform:translateY(4px)}}
@media (max-width:1000px){.r-brief,.r-main,.r-two{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(3,1fr)}}
@media (max-width:600px){.kpis{grid-template-columns:repeat(2,1fr)}.kpi b{font-size:24px}.bars .br{grid-template-columns:90px 1fr 36px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head>
<body><div class="wrap">
<header>
 <div class="brand"><div class="logo">AI</div><div><h1>AI Trading Desk</h1><div class="sub">Automated trading · Crypto 24/7 + US stocks · Paper money (no real cash)</div></div></div>
 <div class="hdr-r"><span class="pill"><span class="dot g"></span>Crypto open</span><span class="pill" id="nyse"></span><span class="pill clock" id="clock"></span></div>
</header>

<div class="card status" id="status"><b id="stext"></b><div class="meta" id="meta">Next market check in <span id="cd">--:--</span> · Last check <span id="last"></span></div></div>

<div class="row r-brief">
 <div class="card"><h2>What's happening right now</h2><p class="hint">Updated after every market check.</p><div id="brief"></div></div>
 <div class="card"><h2>How it works</h2><p class="hint" id="cadence1">Repeats every 10 minutes, around the clock.</p>
  <ol class="steps">
   <li><div><b>Scan</b><span>Reads 80+ crypto and stock charts.</span></div></li>
   <li><div><b>Check</b><span>4 tests: rising trend, healthy momentum, normal price swings, winning track record.</span></div></li>
   <li><div><b>Protect</b><span>Risk limits are applied before any money moves.</span></div></li>
   <li><div><b>Trade</b><span id="step4">Sizes each trade by risk (stronger setups get more, capped per asset and sector), buys or shorts it, then closes automatically at the profit target or the loss limit.</span></div></li>
  </ol></div>
</div>

<div class="kpis">
 <div class="card kpi"><small>Profit / loss</small><b id="k-pnl">$0.00</b><em id="k-pnl2"></em></div>
 <div class="card kpi"><small>Trades today</small><b id="k-tr">0</b><em id="k-tr2"></em></div>
 <div class="card kpi"><small>Win rate</small><b id="k-wr">–</b><em id="k-wr2">No closed trades yet</em></div>
 <div class="card kpi"><small>Trading pool</small><b id="k-bud">$100</b><em id="k-bud2">Stocks: 1 share per trade</em></div>
 <div class="card kpi"><small>Market checks run</small><b id="k-sc">0</b><em id="cadence2">Every 10 minutes</em></div>
</div>

<div class="card crew-card">
 <div class="crew-hd"><div><h2>Meet your AI trading team</h2><p class="hint">Seven specialists around one AI core, sharing data in real time. Hover over anyone to see what they are doing right now.</p></div>
  <div class="moods"><span><i class="m on"></i>Working</span><span><i class="m go"></i>Taking action</span><span><i class="m wait"></i>Thinking</span><span><i class="m idle"></i>Standby</span><span><i class="m blocked"></i>Stopped</span></div></div>
 <div class="crew" id="crew"></div>
</div>
<div class="card" style="margin-bottom:16px"><h2>Closest to a buy</h2><p class="hint">All 4 tests must pass before the bot spends a dollar.</p>
  <div class="scroll"><table><thead><tr><th>Asset</th><th class="n">Price</th><th class="n">4h</th><th>Last hours</th><th class="c">Rising trend</th><th class="c">Momentum</th><th class="c">Normal swings</th><th class="c">Track record</th><th>Status</th></tr></thead><tbody id="sigs"></tbody></table></div>
  <p class="hint" style="margin:12px 0 0" id="sigfoot"></p></div>

<div class="row r-two">
 <div class="card"><h2>Opportunity radar</h2><p class="hint">Every dot is one coin or stock. The closer to the center, the more buy tests it passes; the green center means buy. Hover or tap a dot for details.</p>
  <svg class="radar" id="radar" viewBox="-200 -200 400 400" role="img" aria-label="Radar of every asset by how many buy tests it passes"></svg>
  <div class="legend"><span><span class="dot g"></span>Price rising</span><span><span class="dot r"></span>Price falling</span><span><span class="dot" style="background:#fff;border:2px solid var(--green)"></span>Ready to buy</span><span><span class="dot" style="opacity:.35"></span>Market closed</span></div></div>
 <div class="col">
 <div class="card"><h2>How close is the market to a buy?</h2><p class="hint" id="readyhint"></p><div class="bars" id="ready"></div></div>
 <div class="card"><h2>Market strength over time</h2><p class="hint">Number of assets in a rising trend at each check. Higher = more buying chances.</p><svg class="hist" id="hist" preserveAspectRatio="none"></svg></div>
 </div>
</div>
<div class="tip" id="tip" role="tooltip"></div>

<div class="card" style="margin-bottom:16px"><h2>Market map</h2><p class="hint" id="mapsum"></p><div id="heat"></div></div>
<div class="card" style="margin-bottom:16px"><h2>Activity log</h2><p class="hint">The latest actions, newest first.</p><ul class="log" id="log"></ul></div>
<div class="foot">Paper trading account: simulated money, real market prices. Past results do not guarantee future returns.</div>
</div>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtP=p=>p>=1000?p.toLocaleString('en-US',{maximumFractionDigits:0}):p>=1?p.toFixed(2):p.toPrecision(4);
const money=v=>(v<0?'-$':'$')+Math.abs(v).toFixed(2);
const ST={on:['Working','var(--blue-s)','var(--blue)','b'],go:['Acting','var(--green-s)','var(--green)','g'],wait:['Waiting','var(--amber-s)','var(--amber)','a'],
  idle:['Standby','#f1f5f9','var(--ink2)',''],blocked:['Stopped','var(--red-s)','var(--red)','r']};

$('nyse').innerHTML=D.stocks_open?'<span class="dot g"></span>US stocks open':'<span class="dot r"></span>US stocks closed';
$('last').textContent=D.last;
function tick(){const now=new Date();
  $('clock').textContent=now.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'})+' ET';
  if(D.next_ms&&$('cd')){const s=Math.max(0,Math.round((D.next_ms-now.getTime())/1000));
    $('cd').textContent=s>0?Math.floor(s/60)+':'+String(s%60).padStart(2,'0'):'now';}}
if(D.trade)$('meta').innerHTML='Scanning continues while positions are managed · Last check <span>'+esc(D.last)+'</span>';
$('cadence1').textContent=D.cadence_long;$('k-bud').textContent='$'+D.budget.toLocaleString('en-US');$('k-bud2').textContent='Max $'+(D.budget/4).toLocaleString('en-US')+' per position · up to 4 at once';$('cadence2').textContent=D.cadence_short;
setInterval(tick,1000);tick();

// status
const blocked=D.agents.some(a=>a.st==='blocked');let t,c,b;
if(D.trade){const os=D.opens&&D.opens.length?D.opens:[D.trade],tot=os.reduce((a,o)=>a+(o.pnl||0),0);
  const one=o=>esc(o.s)+(o.side==='short'?' (short)':'')+(o.pnl!=null?' '+(o.pnl>=0?'+':'')+o.pnl.toFixed(2)+'%':'');
  t=os.length>1?'In '+os.length+' trades: '+os.map(one).join(' · '):(D.trade.side==='short'?'In a short trade (betting on a fall): ':'In a trade: ')+one(os[0]);
  c=tot>=0?'var(--green)':'var(--red)';b=tot>=0?'var(--green-s)':'var(--red-s)';}
else if(blocked){t='Paused for today: safety limit reached';c='var(--red)';b='var(--red-s)';}
else if(D.qual.length||D.qual_s.length){t=[D.qual.length?'Buy signal: '+D.qual.map(esc).join(', '):'',D.qual_s.length?'Short signal: '+D.qual_s.map(esc).join(', '):''].filter(Boolean).join(' · ');c='var(--green)';b='var(--green-s)';}
else{t='Watching the market: waiting for a high-odds opportunity';c='var(--blue)';b='var(--blue-s)';}
$('stext').innerHTML=t;$('status').style.setProperty('--sc',c);$('status').style.setProperty('--ss',b);

// briefing
const BI={mood:['&#8645;','var(--blue)','var(--blue-s)'],trade:['&#9679;','var(--green)','var(--green-s)'],go:['&#10003;','var(--green)','var(--green-s)'],
  wait:['&#8987;','var(--amber)','var(--amber-s)'],clock:['&#9719;','var(--ink2)','#f1f5f9'],shield:['&#9960;','var(--blue)','var(--blue-s)']};
D.brief.forEach(([k,txt],i)=>{const [ic,cc,bg]=BI[k]||BI.mood,d=document.createElement('div');d.className='bl fade';d.style.animationDelay=(i*.08)+'s';
  d.innerHTML='<span class="bi" style="--bc:'+cc+';--bs:'+bg+'">'+ic+'</span><span>'+esc(txt).replace(/mostly (FALLING)/,'mostly <b class="neg">falling</b>').replace(/mostly (RISING)/,'mostly <b class="pos">rising</b>')
    .replace(/is (MIXED)/,'is <b>mixed</b>').replace(/is (OPEN)/,'is <b class="pos">open</b>').replace(/is (CLOSED)/,'is <b class="neg">closed</b>')
    .replace('BUY SIGNAL:','<b class="pos">Buy signal:</b>').replace('SHORT SIGNAL:','<b class="pos">Short signal:</b>').replace('IN A TRADE:','<b class="pos">In a trade:</b>')+'</span>';
  $('brief').appendChild(d);});

// KPIs
const pe=$('k-pnl');pe.textContent=money(D.pnl);pe.className=D.pnl>0?'pos':D.pnl<0?'neg':'';
$('k-pnl2').textContent='Today '+money(D.pnl_today)+' · daily stop at -$'+D.max_l;
$('k-tr').textContent=D.n_today+' / '+D.max_t;$('k-tr2').textContent=D.trades+' trades all-time';
$('k-sc').textContent=D.scans.toLocaleString('en-US');
if(D.closed){$('k-wr').textContent=Math.round(D.wins/D.closed*100)+'%';$('k-wr2').textContent=D.wins+' wins of '+D.closed+' trades';}

// the AI team: an isometric war room; seven people around the AI core, each linked to it by live data lines
const TEAM={
 'Market Hours':{nick:'Tempo',col:'#818cf8',sex:'m',seat:[215,192,.84,'front','tablet','L']},
 'Crypto Analyst':{nick:'Byte',col:'#fbbf24',sex:'f',seat:[305,158,.8,'front','point','L']},
 'Stock Analyst':{nick:'Ticker',col:'#38bdf8',sex:'m',seat:[458,158,.8,'front','lean','R']},
 'Deal Picker':{nick:'Pixel',col:'#c084fc',sex:'f',seat:[548,192,.84,'front','chin','R']},
 'Risk Officer':{nick:'Guardian',col:'#2dd4bf',sex:'m',seat:[186,338,1.05,'back','lean','B']},
 'Trader':{nick:'Swift',col:'#4ade80',sex:'f',seat:[576,338,1.05,'back','point','B']},
 'Portfolio Manager':{nick:'Atlas',col:'#f472b6',sex:'m',seat:[384,398,1.08,'back','tablet','R']}};
const MOOD={on:'Working',go:'Taking action',wait:'Thinking',idle:'Standby',blocked:'Stopped'};
const MC={on:'#38bdf8',go:'#4ade80',wait:'#fbbf24',idle:'#64748b',blocked:'#f87171'};
const CX=380,CY=246,RX=214,RY=92;   // table
const ST_=(x,y,s,fill,size,w,anchor)=>`<text x="${x}" y="${y}" fill="${fill}" font-size="${size}" font-weight="${w||700}" font-family="Inter,sans-serif"${anchor?` text-anchor="${anchor}"`:''}>${s}</text>`;

function wallSpark(sp,x,y,w,h,col){if(!sp||sp.length<2)return '';const mn=Math.min(...sp),mx=Math.max(...sp),rg=(mx-mn)||1;
  return `<path class="drawl" pathLength="1" d="M${sp.map((v,i)=>(x+i/(sp.length-1)*w).toFixed(1)+' '+(y+h-(v-mn)/rg*h).toFixed(1)).join('L')}" fill="none" stroke="${col}" stroke-width="1.6"/>`;}

function wall(){ // live screens on the back wall
  const cr=D.syms.filter(x=>x.m==='crypto').sort((a,b)=>b.sc-a.sc)[0],stk=D.syms.find(x=>D.trade&&x.s===D.trade.s)||D.syms.filter(x=>x.m==='stock').sort((a,b)=>b.sc-a.sc)[0];
  const panel=(x,w,title,inner)=>`<rect x="${x}" y="10" width="${w}" height="74" rx="4" fill="#0a3a6b" fill-opacity=".55" stroke="#38bdf8" stroke-opacity=".35"/>`+ST_(x+8,24,title,'#7dd3fc',9,800)+inner;
  const tr=D.trade,pnl=tr&&tr.pnl!=null?(tr.pnl>=0?'+':'')+tr.pnl.toFixed(2)+'%':'NO OPEN TRADE';
  return panel(30,200,cr?esc(cr.s)+' · CRYPTO':'CRYPTO',cr?wallSpark(cr.sp,40,32,180,42,cr.c4>=0?'#4ade80':'#f87171'):'')+
   panel(262,236,'LIVE TRADE',ST_(380,58,tr?esc(tr.s)+(tr.side==='short'?' SHORT':' LONG'):'—','#e0f2fe',15,900,'middle')+ST_(380,76,pnl,tr&&(tr.pnl||0)<0?'#f87171':'#4ade80',12,900,'middle'))+
   panel(530,200,stk?esc(stk.s)+' · STOCK':'STOCKS',stk?wallSpark(stk.sp,540,32,180,42,stk.c4>=0?'#4ade80':'#f87171'):'');}

function chip(){ // isometric gold AI chip on the table
  let pins='';for(let k=-3;k<=3;k++){const t=k*12;pins+=`<rect x="${t-3}" y="-58" width="6" height="9" rx="1" fill="#facc15"/><rect x="${t-3}" y="49" width="6" height="9" rx="1" fill="#facc15"/>`+
     `<rect x="-58" y="${t-3}" width="9" height="6" rx="1" fill="#facc15"/><rect x="49" y="${t-3}" width="9" height="6" rx="1" fill="#facc15"/>`;}
  const iso=`matrix(.72,.36,-.72,.36,${CX},${CY-14})`,isoB=`matrix(.72,.36,-.72,.36,${CX},${CY-6})`;
  return `<ellipse class="chipglow" cx="${CX}" cy="${CY}" rx="120" ry="52" fill="url(#rmChipGlow)"/>`+
   `<g transform="${isoB}"><rect x="-50" y="-50" width="100" height="100" rx="6" fill="#3f3a1c"/></g>`+
   `<g transform="${iso}">${pins}<rect x="-50" y="-50" width="100" height="100" rx="6" fill="#2b2f3a"/><rect x="-42" y="-42" width="84" height="84" rx="5" fill="url(#rmGold)"/>`+
   `<rect x="-32" y="-32" width="64" height="64" rx="4" fill="none" stroke="#7c5c06" stroke-width="2.5"/>`+
   `<text x="0" y="12" text-anchor="middle" font-size="34" font-weight="900" font-family="Inter,sans-serif" fill="#5b4304">AI</text></g>`;}

function rim(px,py){const dx=px-CX,dy=(py-CY)*RX/RY,l=Math.hypot(dx,dy)||1;return [CX+RX*.93*dx/l,CY+RY*.93*dy/l*RY/RY];}

function person(p,a,i){const [x,y,s,view,g]=p.seat,mc=MC[a.st],dir=x<CX?1:-1,f=p.sex==='f',back=view==='back';
  const suit='url(#rmSuit)',skin='#c79a76';
  let b='';
  // legs / skirt
  if(f)b+=`<path d="M-11 -46 L11 -46 L9 -22 L-9 -22Z" fill="#0d1a30"/><path d="M-6 -22 L-5 0 M5 -22 L5 0" stroke="#111d33" stroke-width="3.6" stroke-linecap="round"/>`;
  else b+=`<path d="M-9.5 -46 L-8.5 0 L-1.8 0 L0 -34 L1.8 0 L8.5 0 L9.5 -46Z" fill="#0b1426"/>`;
  b+=`<ellipse cx="-5" cy="0" rx="5" ry="1.8" fill="#05080f"/><ellipse cx="5" cy="0" rx="5" ry="1.8" fill="#05080f"/>`;
  // torso
  b+=f?`<path d="M-14 -84 Q0 -89 14 -84 L11 -64 Q12 -54 12 -44 L-12 -44 Q-12 -54 -11 -64Z" fill="${suit}"/>`
      :`<path d="M-17 -84 Q0 -90 17 -84 L14.5 -44 L-14.5 -44Z" fill="${suit}"/>`;
  if(!back)b+=`<path d="M-5 -86 L0 -73 L5 -86Z" fill="#e2e8f0"/>`+(f?'':`<path d="M-1.6 -84 L1.6 -84 L2.4 -62 L0 -58 L-2.4 -62Z" fill="${p.col}"/>`);
  else b+=`<path d="M0 -86 L0 -46" stroke="#ffffff10" stroke-width="1.2"/>`;
  // head + hair
  b+=`<rect x="-3.5" y="-90" width="7" height="6" fill="${back?'#1a2436':skin}"/><circle cy="-97" r="9" fill="${back?'#1a2436':skin}"/>`;
  if(f)b+=back?`<path d="M-10 -98 Q-10 -109 0 -109 Q10 -109 10 -98 L11 -80 Q0 -76 -11 -80Z" fill="#20130c"/>`
              :`<path d="M-10 -96 Q-10 -109 0 -109 Q10 -109 10 -96 L11 -82 Q8 -84 8 -96 Q4 -103 -2 -102 Q-8 -100 -8 -94 L-8 -82 Q-11 -84 -10 -96Z" fill="#20130c"/>`;
  else b+=back?`<path d="M-9.2 -95 Q-9.5 -107 0 -107 Q9.5 -107 9.2 -95 Q0 -91 -9.2 -95Z" fill="#0b0f1a"/>`
              :`<path d="M-9 -97 Q-9 -108 0 -108 Q9 -108 9 -97 Q6 -103 0 -102.5 Q-6 -103 -9 -97Z" fill="#0b0f1a"/>`;
  // arms by gesture
  const arm=(d)=>`<path d="${d}" stroke="#16233d" stroke-width="6.5" stroke-linecap="round" fill="none"/>`,hand=(hx,hy)=>`<circle cx="${hx}" cy="${hy}" r="2.8" fill="${back?'#2a3550':skin}"/>`;
  const sx=dir*15,ox=-dir*15;
  if(g==='point')b+=arm(`M${ox} -82 Q${ox-dir*3} -64 ${ox-dir*2} -50`)+hand(ox-dir*2,-49)+`<g class="${a.st==='go'?'gesture':''}">`+arm(`M${sx} -82 Q${sx+dir*12} -80 ${sx+dir*24} -74`)+hand(sx+dir*25,-74)+`</g>`;
  else if(g==='tablet')b+=arm(`M-15 -82 Q-16 -66 -6 -62`)+arm(`M15 -82 Q16 -66 6 -62`)+
     `<rect x="-10" y="-72" width="20" height="12" rx="2" fill="#0b1220" stroke="${p.col}" stroke-width="1"/><rect x="-8" y="-70" width="16" height="8" rx="1" fill="${p.col}" opacity=".55" class="${a.st==='idle'?'':'chipglow'}"/>`+hand(-6,-61)+hand(6,-61);
  else if(g==='lean')b+=arm(`M-15 -82 Q-22 -66 -21 -52`)+arm(`M15 -82 Q22 -66 21 -52`)+hand(-21,-51)+hand(21,-51);
  else if(g==='chin')b+=arm(`M${ox} -82 Q${ox-dir*2} -62 ${dir*8} -60`)+hand(dir*8,-60)+arm(`M${sx} -82 Q${sx+dir*4} -64 ${dir*4} -64 Q${dir*1} -78 ${dir*2} -88`)+hand(dir*2,-89);
  const glow=a.st==='idle'?'':`filter="url(#rmRim${i})"`;
  const ring=`<ellipse class="ring vb" style="transform-origin:${x}px ${y}px" cx="${x}" cy="${y}" rx="${26*s}" ry="${9*s}" fill="none" stroke="${mc}" stroke-width="2.2" filter="url(#rmGlow)"/>`+
    `<ellipse cx="${x}" cy="${y}" rx="${18*s}" ry="${6*s}" fill="${mc}" opacity=".25"/>`;
  const side=p.seat[5],hy=y-97*s,lx=side==='L'?x-22*s-92:side==='R'?x+22*s:x-46,ly=side==='B'?y+14:hy-16-(p.nick==='Ticker'?9:0);
  const lbl=`<g class="lbl lblf"><rect x="${lx}" y="${ly}" width="92" height="30" rx="8" fill="#071a33" fill-opacity=".92" stroke="${p.col}" stroke-width="1.3"/>`+
    `<circle cx="${lx+12}" cy="${ly+15}" r="4" fill="${mc}" filter="url(#rmGlow)"/>`+ST_(lx+21,ly+12.5,esc(p.nick),'#fff',10.5,800)+ST_(lx+21,ly+23,MOOD[a.st],mc,8,700)+`</g>`;
  return `<g class="agent${a.st==='idle'?' dim':''}" data-k="${i}" tabindex="0" role="button" aria-label="${esc(p.nick+', '+a.n+': '+a.h)}">${ring}`+
    `<g ${glow}><g transform="translate(${x} ${y}) scale(${s})"><g class="bobp">${b}</g></g></g>`+
    (a.st==='blocked'?`<circle class="alarm" cx="${x}" cy="${y-60*s}" r="${40*s}" fill="#ef4444" opacity=".2"/>`:'')+lbl+`</g>`;}

function link(p,a,i){const [x,y]=p.seat,[rx,ry]=rim(x,y),mc=MC[a.st],mx=(CX+rx)/2,my=(CY+ry)/2;
  const d=`M${CX} ${CY} Q${mx} ${CY+(ry-CY)*.2} ${rx.toFixed(1)} ${ry.toFixed(1)}`;
  return `<path d="${d}" stroke="${mc}" stroke-opacity=".25" stroke-width="3" fill="none"/>`+
    `<path class="flow${a.st==='go'?' fast':''}" d="${d}" stroke="${mc}" stroke-width="2.4" fill="none" filter="url(#rmGlow)" style="animation-delay:-${(i*.3).toFixed(1)}s"/>`+
    `<circle cx="${rx.toFixed(1)}" cy="${ry.toFixed(1)}" r="4" fill="${mc}" filter="url(#rmGlow)"/>`;}

function floorTraces(){let s='';const T=[[150,300,'M150 300 L110 320 L60 320 L40 330'],[170,310,'M170 312 L140 336 L140 372 L100 392'],[300,330,'M300 332 L270 356 L200 356 L180 368'],
   [460,330,'M460 332 L500 356 L560 356 L585 370'],[590,310,'M590 312 L620 336 L620 372 L660 392'],[610,300,'M612 300 L650 320 L700 320 L722 330'],[380,340,'M330 338 L300 360 L300 410'],[430,340,'M432 338 L460 360 L460 410']];
  T.forEach(([,,d],k)=>{s+=`<path d="${d}" stroke="#38bdf8" stroke-opacity=".35" stroke-width="2" fill="none"/><path class="flow" style="animation-delay:-${k*.4}s" d="${d}" stroke="#7dd3fc" stroke-width="1.6" fill="none"/>`;
    const e=d.trim().split(' ').slice(-2);s+=`<circle class="pad" style="animation-delay:-${k*.5}s" cx="${e[0].replace('L','')}" cy="${e[1]}" r="5" fill="none" stroke="#7dd3fc" stroke-width="1.6"/>`;});
  return s;}

(function(){const W=760,H=430;
  let defs=`<defs><linearGradient id="rmFloor" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0c3a6b"/><stop offset="1" stop-color="#05203f"/></linearGradient>`+
   `<radialGradient id="rmTable" cx=".5" cy=".45" r=".6"><stop offset="0" stop-color="#7dd3fc" stop-opacity=".55"/><stop offset=".7" stop-color="#0ea5e9" stop-opacity=".28"/><stop offset="1" stop-color="#0369a1" stop-opacity=".35"/></radialGradient>`+
   `<radialGradient id="rmChipGlow"><stop offset="0" stop-color="#fde047" stop-opacity=".55"/><stop offset="1" stop-color="#fde047" stop-opacity="0"/></radialGradient>`+
   `<linearGradient id="rmGold" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fef08a"/><stop offset=".5" stop-color="#facc15"/><stop offset="1" stop-color="#ca8a04"/></linearGradient>`+
   `<linearGradient id="rmSuit" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1f2c47"/><stop offset="1" stop-color="#0b1426"/></linearGradient>`+
   `<filter id="rmGlow" x="-60%" y="-60%" width="220%" height="220%"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>`;
  D.agents.forEach((a,i)=>{const p=TEAM[a.n];if(p)defs+=`<filter id="rmRim${i}" x="-50%" y="-30%" width="200%" height="160%"><feDropShadow dx="0" dy="0" stdDeviation="1.6" flood-color="${p.col}" flood-opacity=".95"/></filter>`;});
  defs+='</defs>';
  let s=defs+`<rect width="${W}" height="${H}" fill="url(#rmFloor)"/>`;
  for(let k=-10;k<=20;k++)s+=`<line x1="${k*44}" y1="${H}" x2="${k*44+300}" y2="${H-150}" stroke="#38bdf8" stroke-opacity=".06"/><line x1="${k*44+300}" y1="${H}" x2="${k*44}" y2="${H-150}" stroke="#38bdf8" stroke-opacity=".06"/>`;
  s+=wall()+floorTraces();
  const ppl=D.agents.map((a,i)=>[a,TEAM[a.n],i]).filter(x=>x[1]);
  ppl.filter(([,p])=>p.seat[3]==='front').forEach(([a,p,i])=>s+=person(p,a,i));             // behind the table
  s+=`<ellipse cx="${CX}" cy="${CY+14}" rx="${RX}" ry="${RY}" fill="#06345e"/><ellipse cx="${CX}" cy="${CY}" rx="${RX}" ry="${RY}" fill="url(#rmTable)" stroke="#7dd3fc" stroke-width="2" filter="url(#rmGlow)"/>`+
     `<ellipse cx="${CX}" cy="${CY}" rx="${RX-26}" ry="${RY-12}" fill="none" stroke="#bae6fd" stroke-opacity=".35" stroke-dasharray="4 8"/>`;
  ppl.forEach(([a,p,i])=>s+=link(p,a,i));
  s+=chip();
  ppl.filter(([,p])=>p.seat[3]==='back').sort((u,v)=>u[1].seat[1]-v[1].seat[1]).forEach(([a,p,i])=>s+=person(p,a,i)); // in front of the table
  const box=document.createElement('div');
  box.innerHTML=`<svg class="room" viewBox="0 0 ${W} ${H}" role="img" aria-label="The AI team around the AI core">${s}</svg><div class="roster" id="roster"></div>`;
  $('crew').appendChild(box);
  // roster + hover details
  const info=a=>{const p=TEAM[a.n];return `<h3>${esc(p.nick)}<small>${esc(a.n)}</small></h3><div class="px" style="color:${MC[a.st]};font-weight:700">${MOOD[a.st]}</div>`+
     `<div style="margin:4px 0 8px">${esc(a.h)}</div><div class="rec">${esc(a.role)}</div>`;};
  ppl.forEach(([a,p,i])=>{const r=document.createElement('div');r.className='rm';r.style.setProperty('--col',p.col);r.style.setProperty('--mc',MC[a.st]);r.dataset.k=i;
    r.innerHTML=`<span class="dot"></span><b>${esc(p.nick)}<small>${esc(a.n)}</small></b><span class="mood">${MOOD[a.st]}</span><div class="act">${esc(a.h)}</div>`;$('roster').appendChild(r);});
  const room=box.querySelector('.room');
  room.querySelectorAll('.agent').forEach(el=>{const a=D.agents[+el.dataset.k],row=$('roster').querySelector('[data-k="'+el.dataset.k+'"]');
    const show=e=>{tip.innerHTML=info(a);tip.classList.add('show');moveTip(e);row&&row.classList.add('hl');};
    const hide=()=>{tip.classList.remove('show');row&&row.classList.remove('hl');};
    el.addEventListener('mouseenter',show);el.addEventListener('mousemove',moveTip);el.addEventListener('mouseleave',hide);
    el.addEventListener('click',e=>{e.stopPropagation();show(e);});
    el.addEventListener('focus',()=>{const b=el.getBoundingClientRect();show({clientX:b.right,clientY:b.top});});el.addEventListener('blur',hide);});
})();

// closest to a buy
function spark(sp,up){if(!sp||sp.length<2)return '';const w=110,h=30,mn=Math.min(...sp),mx=Math.max(...sp),rg=(mx-mn)||1;
  const d='M'+sp.map((v,i)=>(i/(sp.length-1)*w).toFixed(1)+' '+(h-3-(v-mn)/rg*(h-6)).toFixed(1)).join('L');
  return '<svg class="spark" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><path d="'+d+'" fill="none" stroke="'+(up?'#16a34a':'#dc2626')+'" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>';}
const live=D.syms.filter(x=>x.m==='crypto'||D.stocks_open);
live.slice().sort((a,b)=>(b.q-a.q)||(b.n-a.n)||(b.sc-a.sc)).slice(0,7).forEach((x,i)=>{
  const up=x.sp&&x.sp.length>1?x.sp[x.sp.length-1]>=x.sp[0]:x.c4>=0,tr=document.createElement('tr');tr.className='fade';tr.style.animationDelay=(i*.05)+'s';
  const st=x.q?'<span class="badge ready">Ready to buy</span>':x.n>=3?'<span class="badge near">'+x.n+' of 4 passed</span>':'<span class="badge far">'+x.n+' of 4 passed</span>';
  tr.innerHTML='<td><span class="sym">'+esc(x.s)+'</span><span class="type">'+(x.m==='crypto'?'Crypto':'Stock')+'</span></td><td class="n">$'+fmtP(x.p)+'</td>'+
   '<td class="n '+(x.c4>=0?'pos':'neg')+'">'+(x.c4>=0?'+':'')+x.c4.toFixed(2)+'%</td><td>'+spark(x.sp,up)+'</td>'+
   x.ck.map(ok=>'<td class="c '+(ok?'ok':'no')+'">'+(ok?'&#10003;':'&#10005;')+'</td>').join('')+'<td>'+st+'</td>';
  $('sigs').appendChild(tr);});
$('sigfoot').textContent='Track record = how often this exact strategy made money on the asset in recent history. It needs to win at least 1 in 3 trades, because each win earns twice what a loss costs.';

// readiness distribution
(function(){const cnt=[0,0,0,0,0];live.forEach(x=>cnt[x.q?4:Math.min(3,x.n)]++);
  const mx=Math.max(1,...cnt),lab=['0 tests passed','1 test passed','2 tests passed','3 tests passed','All 4: buy'],col=['#cbd5e1','#94a3b8','#fbbf24','#f59e0b','#16a34a'];
  $('readyhint').textContent='How many of the '+live.length+' assets'+(D.stocks_open?'':' (crypto only while US stocks are closed)')+' pass each number of buy tests.';
  $('ready').innerHTML=cnt.map((v,i)=>'<div class="br"><span>'+lab[i]+'</span><div class="track"><div class="fill" style="width:'+(v/mx*100).toFixed(1)+'%;background:'+col[i]+';animation-delay:'+(i*.08)+'s"></div></div><span class="v">'+v+'</span></div>').join('');})();

// market strength over time
(function(){const H=D.hist,w=600,h=170;if(!H.length)return;const mx=Math.max(4,...H),bw=w/H.length,pad=18;
  let s='';for(let k=0;k<=4;k++){const y=pad+(h-pad-2)*k/4;s+='<line x1="0" x2="'+w+'" y1="'+y+'" y2="'+y+'" stroke="#eef1f6"/>';}
  s+='<text x="2" y="12" font-size="11" fill="#94a3b8" font-family="Inter">'+mx+' rising</text>';
  H.forEach((v,i)=>{const bh=Math.max(1.5,v/mx*(h-pad-2));s+='<rect x="'+(i*bw+1).toFixed(1)+'" y="'+(h-bh).toFixed(1)+'" width="'+Math.max(1,bw-2).toFixed(1)+'" height="'+bh.toFixed(1)+'" rx="2" fill="#2563eb" opacity=".85"><title>'+v+' assets rising</title></rect>';});
  const hs=$('hist');hs.setAttribute('viewBox','0 0 '+w+' '+h);hs.innerHTML=s;})();

// market map
(function(){let h='';const up=live.filter(x=>x.c4>=0).length;
  $('mapsum').innerHTML='Price change over the last 4 hours. Darker = bigger move. Right now <b class="pos">'+up+' up</b> and <b class="neg">'+(live.length-up)+' down</b>.';
  [['crypto','Crypto · trades 24/7'],['stock',D.stocks_open?'US stocks · live':'US stocks · market closed, last session shown']].forEach(([m,t])=>{
    const L=D.syms.filter(x=>x.m===m).sort((a,b)=>b.c4-a.c4);if(!L.length)return;
    h+='<div class="sect">'+t+'</div><div class="heat">';
    L.forEach(x=>{const a=Math.min(1,Math.abs(x.c4)/3),bg=x.c4>=0?'rgba(22,163,74,'+(.1+a*.55).toFixed(2)+')':'rgba(220,38,38,'+(.1+a*.55).toFixed(2)+')',fg=a>.55?'#fff':(x.c4>=0?'#14532d':'#7f1d1d');
      h+='<div class="tile'+(x.q?' q':'')+(m==='stock'&&!D.stocks_open?' off':'')+'" data-k="'+D.syms.indexOf(x)+'" style="background:'+bg+';color:'+fg+'"><b>'+esc(x.s)+'</b>'+(x.c4>=0?'+':'')+x.c4.toFixed(2)+'%</div>';});
    h+='</div>';});
  $('heat').innerHTML=h||'<p class="hint">Waiting for the first market check.</p>';
  $('heat').querySelectorAll('.tile').forEach(el=>bindTip(el,D.syms[+el.dataset.k]));})();

// hover card shared by the radar and the market map
const CK=['Rising trend','Healthy momentum','Normal price swings','Winning track record'];
const tip=$('tip');
function tipHtml(x){const dec=x.w+x.l,closed=x.m==='stock'&&!D.stocks_open;
  const st=x.q?'<b class="pos">Ready to buy</b>':closed?'US market closed, last session data':x.n+' of 4 tests passed';
  return '<h3>'+esc(x.s)+'<small>'+(x.m==='crypto'?'Crypto':'US stock')+'</small></h3>'+
   '<div class="px">$'+fmtP(x.p)+' · <span class="'+(x.c4>=0?'pos':'neg')+'">'+(x.c4>=0?'+':'')+x.c4.toFixed(2)+'%</span> in 4h</div>'+
   '<ul>'+x.ck.map((ok,k)=>'<li><span>'+CK[k]+'</span><span class="'+(ok?'ok':'no')+'">'+(ok?'&#10003;':'&#10005;')+'</span></li>').join('')+'</ul>'+
   '<div class="rec">'+(dec?'Past record with this strategy: won '+x.w+' of '+dec+' ('+x.hit+'%). Needs 33%+.':'Not enough history to judge yet.')+'<br>Status: '+st+
   (x.sh?'<br>Short side (bet on a fall): '+(x.sq?'<b class="pos">ready to short</b>':(x.sh.down?'falling':'not falling'))+(x.sh.w+x.sh.l?', past shorts won '+x.sh.w+' of '+(x.sh.w+x.sh.l)+' ('+x.sh.hit+'%)':''):'')+'</div>';}
function showTip(x,ev){tip.innerHTML=tipHtml(x);tip.classList.add('show');moveTip(ev);}
function moveTip(ev){const w=tip.offsetWidth,h=tip.offsetHeight;let l=ev.clientX+14,t=ev.clientY+14;
  if(l+w>innerWidth-8)l=ev.clientX-w-14;if(t+h>innerHeight-8)t=ev.clientY-h-14;tip.style.left=Math.max(8,l)+'px';tip.style.top=Math.max(8,t)+'px';}
function hideTip(){tip.classList.remove('show');document.querySelectorAll('.pt.on').forEach(e=>e.classList.remove('on'));}
function bindTip(el,x){el.addEventListener('mouseenter',e=>showTip(x,e));el.addEventListener('mousemove',moveTip);el.addEventListener('mouseleave',hideTip);
  el.addEventListener('click',e=>{e.stopPropagation();hideTip();el.classList.add('on');showTip(x,e);});}
document.addEventListener('click',hideTip);

// radar (light): band = tests passed, center = buy
(function(){const NS='http://www.w3.org/2000/svg',svg=$('radar'),R=185,BAND=[[130,182],[130,182],[90,130],[48,90],[0,48]];
  let s='<circle r="'+R+'" fill="#f8fafc" stroke="#e5e9f0"/><circle r="130" fill="#f3f6fb" stroke="#e5e9f0"/><circle r="90" fill="#eef3fb" stroke="#e5e9f0"/>'+
   '<circle r="48" fill="#ecfdf3" stroke="#86efac" stroke-width="1.5"/>'+
   '<line x1="-'+R+'" x2="'+R+'" stroke="#eef1f6"/><line y1="-'+R+'" y2="'+R+'" stroke="#eef1f6"/>';
  [[156,'0–1 tests'],[110,'2 tests'],[69,'3 tests']].forEach(([r,l])=>s+='<text y="'+(-r+4)+'" text-anchor="middle" font-size="10" fill="#94a3b8" font-family="Inter" font-weight="600">'+l+'</text>');
  s+='<text y="4" text-anchor="middle" font-size="11" fill="#16a34a" font-family="Inter" font-weight="700">'+(D.qual.length?'BUY':'BUY ZONE')+'</text>';
  svg.innerHTML=s;
  const L=D.syms.slice().sort((a,b)=>a.m.localeCompare(b.m)||a.s.localeCompare(b.s)),n=L.length||1;
  L.forEach((x,i)=>{const ang=(i/n*360-90)*Math.PI/180,band=x.q?4:Math.min(3,x.n),[lo,hi]=BAND[band];
    const jit=Math.min(1,Math.max(0,(x.sc+140)/185)),r=x.q?18+jit*16:lo+(hi-lo)*(0.78-0.56*jit);
    const c=document.createElementNS(NS,'circle'),closed=x.m==='stock'&&!D.stocks_open;
    c.setAttribute('cx',(r*Math.cos(ang)).toFixed(1));c.setAttribute('cy',(r*Math.sin(ang)).toFixed(1));
    c.setAttribute('r',x.q?7:5.5);c.setAttribute('class','pt');
    c.setAttribute('fill',x.q?'#fff':x.up?'#16a34a':'#dc2626');if(x.q){c.setAttribute('stroke','#16a34a');c.setAttribute('stroke-width','3');}
    if(closed)c.setAttribute('opacity','.35');
    c.setAttribute('tabindex','0');c.setAttribute('aria-label',x.s+', '+x.n+' of 4 tests');
    c.addEventListener('focus',()=>{const b=c.getBoundingClientRect();showTip(x,{clientX:b.right,clientY:b.bottom});});c.addEventListener('blur',hideTip);
    bindTip(c,x);svg.appendChild(c);});})();

// activity log
D.feed.slice().reverse().forEach(f=>{const li=document.createElement('li');
  const cl=f.c==='buy'?'pos':f.c==='err'?'neg':'';li.innerHTML='<span class="t">'+esc(f.t)+'</span><span class="'+cl+'">'+esc(f.x)+'</span>';$('log').appendChild(li);});

setTimeout(()=>{if(location.hash!=='#rec')location.reload();},60000);
</script>
</body></html>
"""

if __name__ == "__main__":
    print("Wrote", build())
