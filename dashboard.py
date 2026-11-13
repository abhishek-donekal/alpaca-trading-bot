"""Builds trader/dashboard.html from the crypto bot's logs. The bot calls build() after every run
and every trade; run `python dashboard.py` to rebuild it by hand. Open the file in any browser."""
import csv
import datetime as dt
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRADER = ROOT / "trader"
LOGS = TRADER / "logs"
OUT = TRADER / "dashboard.html"


def _jsonl(path):
    out = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _latest_rows():
    files = sorted(LOGS.glob("scan-*.csv"))
    if not files:
        return [], None
    rows = list(csv.DictReader(files[-1].open()))
    if not rows:
        return [], None
    last = rows[-1]["run_id"]
    return [r for r in rows if r["run_id"] == last], rows[-1]["time_utc"]


def _trades(events):
    """Trades in the order they opened; several can be open at once, so events are matched by symbol."""
    trades, live = [], {}
    for e in events:
        sym = e.get("symbol")
        if e["event"] == "entry" and e.get("status") == "filled":
            cur = dict(symbol=sym, opened=e["at"], entry=float(e["price"]), qty=e["qty"], status="open",
                       side=e.get("side", "long"))
            trades.append(cur)
            live[sym] = cur
            continue
        cur = live.get(sym)
        if not cur:
            continue
        if e["event"] == "let_it_run":
            cur["note"] = "let it run, stop raised to %s" % e.get("new_stop")
        elif e["event"] == "watch":
            cur["last"], cur["pnl_pct"] = e.get("price"), e.get("pnl_pct")
            cur["stop"], cur["target"] = e.get("stop"), e.get("target")
        elif e["event"] == "exit":
            cur.update(closed=e["at"], reason=e.get("reason"), exit=e.get("price"))
        elif e["event"] == "done":
            cur.update(status="closed", pnl_usd=e.get("pnl_usd"), pnl_pct=e.get("pnl_pct"))
            live.pop(sym, None)
    return trades


def e(x):
    return html.escape(str(x if x is not None else ""))


def _sunday(year, month, nth):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=nth - 1)


def et(ts, fmt="%m-%d %I:%M %p"):
    """UTC timestamp ('2026-11-12 13:35:57' or '2026-11-12T13:35:57Z') -> New York time, EST or EDT.
    Uses the US rule (2nd Sunday of March to 1st Sunday of November) since this PC has no tz database."""
    if not ts:
        return ""
    t = dt.datetime.fromisoformat(str(ts).replace("Z", "").replace("T", " ")[:19])
    dst_start = dt.datetime.combine(_sunday(t.year, 3, 2), dt.time(7))   # 2:00 EST = 07:00 UTC
    dst_end = dt.datetime.combine(_sunday(t.year, 11, 1), dt.time(6))    # 2:00 EDT = 06:00 UTC
    edt = dst_start <= t < dst_end
    return (t - dt.timedelta(hours=4 if edt else 5)).strftime(fmt) + (" EDT" if edt else " EST")


def _num(v):
    """Scan interval from the auto_start log line: a number, or '5 open / 15 closed' in aligned mode."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 5.0


def _today_et(ts):
    return et(ts, "%Y-%m-%d")[:10]


def _agent_cards(events, runs, rows, open_t):
    """One card per part of the bot: what it did on the last run and what it's doing now."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    last = runs[-1] if runs else {}
    t_last = last.get("time_utc")
    every = next((_num(x.get("every_min")) for x in reversed(events) if x["event"] == "auto_start"), 10.0)
    stocks_open = bool(last.get("stocks_open"))
    crypto = [r for r in rows if "/" in r["symbol"]]
    stocks = [r for r in rows if "/" not in r["symbol"]]
    q = last.get("qualifying") or []
    decisions = last.get("decisions") or {}
    cfg = json.loads((TRADER / "config.json").read_text()) if (TRADER / "config.json").exists() else {}
    max_t, max_l = int(cfg.get("max_trades_per_day", 6)), float(cfg.get("max_daily_loss_usd", 6))
    today = _today_et(now.strftime("%Y-%m-%d %H:%M:%S"))
    todays = [x for x in events if _today_et(x["at"]) == today]
    n_today = sum(1 for x in todays if x["event"] == "entry" and x.get("status") == "filled")
    pnl_today = sum(float(x.get("pnl_usd") or 0) for x in todays if x["event"] == "done")
    entries = [x for x in events if x["event"] == "entry"]
    dones = [x for x in events if x["event"] == "done"]
    watch = next((x for x in reversed(events) if x["event"] == "watch"), None)

    def short(s):
        return s.replace("/USD", "")

    def scanner(rs, what):
        if not rs:
            return "-", ""
        up = sum(1 for r in rs if r["trend_up"] == "True")
        best = rs[0]
        return ("%d %s scanned, %d trending up" % (len(rs), what, up),
                "Best: %s (past win %s%%), %s" % (short(best["symbol"]), best["past_hit_pct"],
                                                 best["decision"] if best["decision"] != "rejected" else best["reason"]))

    cards = []
    # 1 clock / scheduler
    if t_last:
        age = (now - dt.datetime.fromisoformat(t_last)).total_seconds() / 60
        nxt_utc = last.get("next_utc") or (dt.datetime.fromisoformat(t_last)
                                           + dt.timedelta(minutes=every)).strftime("%Y-%m-%d %H:%M:%S")
        gap = (dt.datetime.fromisoformat(nxt_utc) - dt.datetime.fromisoformat(t_last)).total_seconds() / 60
        nxt = et(nxt_utc, "%I:%M %p")
        if age > gap * 2 + 5 and not open_t:
            cards.append(("Scheduler", "blocked", "No scan for %d min" % age, "Is the bot still running?"))
        else:
            cards.append(("Scheduler", "on", "NYSE %s" % ("OPEN: crypto + stocks" if stocks_open
                                                         else "closed: crypto only"),
                          "Checks after each candle closes. Last %s, next %s" % (et(t_last, "%I:%M %p"), nxt)))
    else:
        cards.append(("Scheduler", "idle", "Not started", ""))
    # 2 crypto scanner
    h, d = scanner(crypto, "coins")
    cards.append(("Crypto scanner", "on" if crypto else "idle", h, d))
    # 3 stock scanner
    if stocks_open and stocks:
        h, d = scanner(stocks, "stocks")
        cards.append(("Stock scanner", "on", h, d + ". Buys 9:45 AM-3:30 PM ET"))
    else:
        cards.append(("Stock scanner", "idle", "Sleeping: market closed", "Wakes at 9:30 AM ET on trading days"))
    # 4 signal picker
    if q:
        cards.append(("Signal picker", "go", "%d qualified: %s" % (len(q), ", ".join(short(s) for s in q[:4])),
                      "Needs uptrend + RSI 50-72 + past win rate above break-even"))
    else:
        cards.append(("Signal picker", "wait", "Nothing qualified (0 of %d)" % len(rows),
                      "Needs uptrend + RSI 50-72 + past win rate above break-even"))
    # 5 risk guard
    blocked = n_today >= max_t or pnl_today <= -max_l
    skipped = [("%s: %s" % (short(k), v[9:])) for k, v in decisions.items() if v.startswith("skipped")]
    cards.append(("Risk guard", "blocked" if blocked else "on",
                  "Today: %d/%d trades, P&L $%+.2f (limit -$%g)" % (n_today, max_t, pnl_today, max_l),
                  ("Held back " + "; ".join(skipped[:2])) if skipped else
                  ("Daily limit hit: no new buys until tomorrow" if blocked else "Spread under 0.25%; crypto by budget, stocks 1 share")))
    # 6 order executor
    if entries:
        x = entries[-1]
        cards.append(("Order executor", "go" if open_t else "idle",
                      "Last: %s %s %s" % ("short" if x.get("side") == "short" else "buy", short(x["symbol"]),
                                          x.get("status")),
                      "%s @ %s, %s" % (x.get("qty"), x.get("price"), et(x["at"], "%m-%d %I:%M %p"))))
    else:
        cards.append(("Order executor", "idle", "No orders yet", "Market order when a signal passes risk"))
    # 7 position manager
    if open_t:
        t = open_t[-1]
        cards.append(("Position manager", "go", "Holding %s" % short(t["symbol"]),
                      ("Now %+.2f%%, stop %s, target %s" % (watch["pnl_pct"], watch.get("stop"), watch.get("target"))
                       if watch else "Entry %s, checking every 10 s" % t["entry"])))
    elif dones:
        x = dones[-1]
        cards.append(("Position manager", "idle", "Flat. Last: %s $%+.2f" % (short(x["symbol"]),
                                                                           float(x.get("pnl_usd") or 0)),
                      et(x["at"], "%m-%d %I:%M %p")))
    else:
        cards.append(("Position manager", "idle", "Flat: nothing to watch", "Sells at stop, target or trailing stop"))
    return cards


def _agents(events, runs, rows, open_t):
    cards = _agent_cards(events, runs, rows, open_t)
    label = {"on": "running", "go": "acting", "wait": "waiting", "idle": "idle", "blocked": "blocked"}
    out = ""
    for i, (name, st, head, det) in enumerate(cards, 1):
        out += ('<div class="agent %s"><div class="ag-top"><span class="dot"></span><b>%d. %s</b>'
                '<span class="pill">%s</span></div><div class="ag-head">%s</div><div class="muted ag-det">%s</div>'
                '</div>' % (st, i, e(name), label[st], e(head), e(det)))
    return '<div class="flow">%s</div>' % out


def build():
    events = _jsonl(TRADER / "crypto_log.jsonl")
    runs = _jsonl(LOGS / "runs.jsonl")
    rows, last_time = _latest_rows()
    trades = _trades(events)
    closed = [t for t in trades if t["status"] == "closed"]
    open_t = [t for t in trades if t["status"] == "open"]
    pnl = sum(float(t.get("pnl_usd") or 0) for t in closed)
    wins = sum(1 for t in closed if float(t.get("pnl_usd") or 0) > 0)
    rule = runs[-1]["rule"] if runs else "+4/-2"
    now = et(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "%Y-%m-%d %I:%M %p")
    # only errors newer than the last good scan: old, already-fixed problems stay off the page
    last_ok = max((i for i, x in enumerate(events) if x["event"] == "scan"), default=-1)
    errors = [x for x in events[last_ok + 1:] if x["event"].endswith("error")]

    if open_t:
        t = open_t[-1]
        status = "IN A TRADE: %s, entry %s%s" % (t["symbol"], t["entry"],
                                                  ", now %+.2f%%" % t["pnl_pct"] if t.get("pnl_pct") is not None else "")
        status_cls = "live"
    elif runs:
        status, status_cls = "WAITING for a setup: nothing qualified on the last scan", "wait"
    else:
        status, status_cls = "No runs logged yet", "wait"

    # history chart: uptrend count and qualifying count per run (last 96 runs = 24h)
    hist = runs[-96:]
    bars = ""
    if hist:
        n, w, hgt = len(hist), 720, 120
        bw = w / max(n, 1)
        top = max(max(len(r.get("uptrend", [])) for r in hist), 1)
        for i, r in enumerate(hist):
            up, q = len(r.get("uptrend", [])), len(r.get("qualifying", []))
            uh = up / top * (hgt - 16)
            bars += ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" class="b-up"><title>%s: %d uptrend, '
                     '%d qualifying</title></rect>' % (i * bw + 1, hgt - uh, max(bw - 2, 1), uh, e(et(r["time_utc"])), up, q))
            if q:
                bars += '<circle cx="%.1f" cy="8" r="4" class="b-q"><title>%d qualifying</title></circle>' % (
                    i * bw + bw / 2, q)
        chart = ('<svg viewBox="0 0 %d %d" preserveAspectRatio="none" class="chart" role="img" '
                 'aria-label="Coins in an uptrend per scan">%s</svg>'
                 '<div class="axis"><span>%s</span><span>%s</span></div>'
                 % (w, hgt, bars, e(et(hist[0]["time_utc"])), e(et(hist[-1]["time_utc"]))))
    else:
        chart = '<p class="muted">The chart fills in after the first few scans.</p>'

    trade_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td class=num>%s</td><td class=num>%s</td><td>%s</td><td>%s</td>"
        "<td class='num %s'>%s</td></tr>" % (
            e(et(t["opened"])), e(t["symbol"]), e(t["qty"]), e(t["entry"]),
            e(et(t.get("closed")) or "open"), e(t.get("reason") or t.get("note") or ""),
            "pos" if float(t.get("pnl_usd") or t.get("pnl_pct") or 0) > 0 else "neg",
            ("$%+.2f (%+.2f%%)" % (float(t["pnl_usd"]), float(t["pnl_pct"]))) if t.get("pnl_usd") is not None
            else ("%+.2f%%" % t["pnl_pct"] if t.get("pnl_pct") is not None else "")) for t in reversed(trades)
    ) or '<tr><td colspan=7 class="muted">No trades yet.</td></tr>'

    coin_rows = ""
    for r in rows:
        d = r["decision"]
        cls = "buy" if d.startswith("BOUGHT") else ("ok" if d == "qualifies" else "")
        coin_rows += ("<tr class='%s'><td>%s</td><td class=num>%s</td><td class='num %s'>%s</td>"
                      "<td class='num %s'>%s</td><td class=num>%s</td><td>%s</td><td class=num>%s-%s</td>"
                      "<td class=num>%s%%</td><td>%s</td></tr>") % (
            cls, e(r["symbol"].replace("/USD", "")), e(r["price"]),
            "pos" if float(r["chg4h_pct"]) >= 0 else "neg", e(r["chg4h_pct"]),
            "pos" if float(r["chg24h_pct"]) >= 0 else "neg", e(r["chg24h_pct"]), e(r["rsi"]),
            "up" if r["trend_up"] == "True" else "-", e(r["past_wins"]), e(r["past_losses"]),
            e(r["past_hit_pct"]), e(d.upper() if cls else r["reason"]))
    if not coin_rows:
        coin_rows = '<tr><td colspan=9 class="muted">No scan logged yet.</td></tr>'

    run_rows = "".join(
        "<tr><td>%s</td><td class=num>%s</td><td>%s</td><td>%s</td></tr>" % (
            e(et(r["time_utc"])), e(len(r.get("uptrend", []))),
            e(", ".join(s.replace("/USD", "") for s in r.get("qualifying", [])) or "none"),
            e("; ".join("%s: %s" % kv for kv in (r.get("decisions") or {}).items()) or "waited"))
        for r in reversed(runs[-30:])) or '<tr><td colspan=4 class="muted">No runs yet.</td></tr>'

    err_html = ""
    if errors:
        err_html = '<div class="alert">Recent errors: %s</div>' % e(errors[-1].get("error", "")[:160])

    page = TEMPLATE.format(
        now=e(now), status=e(status), status_cls=status_cls, rule=e(rule), runs=len(runs),
        last=e(et(last_time) or "never"), ntrades=len(trades), wins=wins, nclosed=len(closed),
        pnl="%+.2f" % pnl, pnl_cls="pos" if pnl > 0 else ("neg" if pnl < 0 else ""), chart=chart,
        agents=_agents(events, runs, rows, open_t), trade_rows=trade_rows, coin_rows=coin_rows, run_rows=run_rows, err=err_html)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    try:  # the sci-fi view, trader/hud.html
        import importlib
        import hud
        importlib.reload(hud)
        hud.build()
    except Exception as ex:
        print("hud error:", ex)
    return OUT


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Trading Bot Dashboard</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e4e4df;
  --pos:#0f7b4a; --neg:#b42318; --accent:#2f5bd3; --live-bg:#e8f4ee; --wait-bg:#f1f1ec; --hi:#eef3ff; --buy:#e8f4ee; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#141413; --card:#1e1e1c;
  --ink:#ecebe6; --muted:#9c9b95; --line:#2e2e2b; --pos:#4cc38a; --neg:#f2716a; --accent:#7c9cff;
  --live-bg:#17301f; --wait-bg:#232320; --hi:#1d2540; --buy:#17301f; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif }}
main {{ max-width:1080px; margin:0 auto; padding:20px 16px 40px }}
h1 {{ font-size:20px; margin:0 }} h2 {{ font-size:15px; margin:0 0 10px }}
header {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:14px }}
.muted {{ color:var(--muted) }}
.status {{ padding:12px 14px; border-radius:10px; font-weight:600; margin-bottom:14px }}
.status.live {{ background:var(--live-bg) }} .status.wait {{ background:var(--wait-bg) }}
.alert {{ padding:10px 14px; border-radius:10px; border:1px solid var(--neg); color:var(--neg); margin-bottom:14px }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:14px }}
.kpi, .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px }}
.kpi b {{ display:block; font-size:22px; font-variant-numeric:tabular-nums }}
.card {{ margin-bottom:14px }}
.scroll {{ overflow-x:auto }}
table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap }}
th {{ font-size:12px; color:var(--muted); font-weight:600 }}
td.num, th.num {{ text-align:right }}
.pos {{ color:var(--pos) }} .neg {{ color:var(--neg) }}
tr.ok {{ background:var(--hi) }} tr.buy {{ background:var(--buy); font-weight:600 }}
.chart {{ width:100%; height:120px; display:block }}
.b-up {{ fill:var(--accent); opacity:.55 }} .b-q {{ fill:var(--pos) }}
.flow {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px }}
.agent {{ border:1px solid var(--line); border-left:4px solid var(--muted); border-radius:10px; padding:10px 12px }}
.agent.on {{ border-left-color:var(--accent) }} .agent.go {{ border-left-color:var(--pos) }}
.agent.wait {{ border-left-color:#c98a00 }} .agent.blocked {{ border-left-color:var(--neg) }}
.ag-top {{ display:flex; align-items:center; gap:6px; font-size:13px }}
.ag-top .pill {{ margin-left:auto; font-size:11px; color:var(--muted); border:1px solid var(--line); border-radius:99px; padding:0 7px }}
.dot {{ width:8px; height:8px; border-radius:50%; background:var(--muted); flex:none }}
.on .dot {{ background:var(--accent) }} .go .dot {{ background:var(--pos); animation:pulse 1.4s infinite }}
.wait .dot {{ background:#c98a00 }} .blocked .dot {{ background:var(--neg) }}
@keyframes pulse {{ 50% {{ opacity:.25 }} }}
@media (prefers-reduced-motion: reduce) {{ .go .dot {{ animation:none }} }}
.ag-head {{ font-weight:600; margin:6px 0 2px; white-space:normal }}
.ag-det {{ font-size:12px; white-space:normal }}
.axis {{ display:flex; justify-content:space-between; font-size:12px; color:var(--muted) }}
</style></head>
<body><main>
<header><h1>Trading bot</h1><span class="muted">Paper account · {rule} · updated {now} · refreshes every minute · <a href="hud.html" style="color:var(--accent)">HUD view</a></span></header>
<div class="status {status_cls}">{status}</div>
<div class="card"><h2>Agents <span class="muted">(what each part of the bot is doing, in order)</span></h2>{agents}</div>
{err}
<div class="kpis">
  <div class="kpi"><span class="muted">Scans run</span><b>{runs}</b></div>
  <div class="kpi"><span class="muted">Last scan</span><b style="font-size:15px">{last}</b></div>
  <div class="kpi"><span class="muted">Trades</span><b>{ntrades}</b></div>
  <div class="kpi"><span class="muted">Wins / closed</span><b>{wins} / {nclosed}</b></div>
  <div class="kpi"><span class="muted">Realized P&amp;L</span><b class="{pnl_cls}">${pnl}</b></div>
</div>
<div class="card"><h2>Symbols in an uptrend, per scan <span class="muted">(green dot = a coin qualified)</span></h2>{chart}</div>
<div class="card"><h2>Trades</h2><div class="scroll"><table>
<tr><th>Opened (New York)</th><th>Coin</th><th class=num>Qty</th><th class=num>Entry</th><th>Closed</th><th>Why</th><th class=num>P&amp;L</th></tr>
{trade_rows}</table></div></div>
<div class="card"><h2>Last scan: every coin and stock, and why</h2><div class="scroll"><table>
<tr><th>Symbol</th><th class=num>Price</th><th class=num>4h %</th><th class=num>1 day %</th><th class=num>RSI</th><th>Trend</th><th class=num>Past W-L</th><th class=num>Win %</th><th>Decision / reason</th></tr>
{coin_rows}</table></div></div>
<div class="card"><h2>Recent runs</h2><div class="scroll"><table>
<tr><th>Time (New York)</th><th class=num>Uptrend</th><th>Qualified</th><th>Action</th></tr>
{run_rows}</table></div></div>
<p class="muted">Paper trading. Past win rates come from the last 7 days of 15-minute bars and are not a forecast.</p>
</main></body></html>
"""

if __name__ == "__main__":
    print("Wrote", build())
