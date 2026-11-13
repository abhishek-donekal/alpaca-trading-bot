"""One command line for the whole day-trader skill pack, wired to the Alpaca paper account.

    python trader.py status                 market clock, account, today's scoreboard
    python trader.py morning [--limit N]    pre-market scan + news on the affordable hits
    python trader.py hunt [--symbols A,B]   breakout scan -> entry -> exit -> time machine -> size -> risk
    python trader.py place --index 1        show the ticket, ask for a typed yes, send it to Alpaca
    python trader.py flatten                close every open stock position (asks yes per symbol)
    python trader.py score                  P&L on the $ budget vs the 1%-per-30-min goal

Every order needs a typed "yes" for that exact ticket. Paper account only unless .env says otherwise.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from connect import load_env

ROOT = Path(__file__).resolve().parent
SKILLS = ROOT / ".claude" / "skills"
TRADER = ROOT / "trader"
CONFIG = TRADER / "config.json"


# ------------------------------------------------------------------ helpers

def cfg():
    return json.loads(CONFIG.read_text())


def skill(name, script, *args, show=True):
    """Run one of the skill tools from the project root; return (exit code, output)."""
    cmd = [sys.executable, str(SKILLS / name / script), *[str(a) for a in args]]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if show and out.strip():
        print(out.rstrip())
    return r.returncode, out


def banner(text):
    print("\n" + "=" * 8 + " " + text + " " + "=" * max(0, 60 - len(text)))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


class Alpaca:
    def __init__(self):
        load_env()
        self.base = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
        self.h = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
                  "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"],
                  "Content-Type": "application/json"}
        self.paper = "paper-api" in self.base

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError("Alpaca %s %s -> HTTP %s: %s" % (method, path, e.code,
                                                                e.read().decode(errors="replace")))


def ny_now():
    """New York time without tz files: EST/EDT from the US DST rule."""
    utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    y = utc.year
    mar = datetime.datetime(y, 3, 8 + (6 - datetime.date(y, 3, 8).weekday()) % 7, 7)   # 2nd Sun Mar, 2am EST
    nov = datetime.datetime(y, 11, 1 + (6 - datetime.date(y, 11, 1).weekday()) % 7, 6)  # 1st Sun Nov, 2am EDT
    return utc + datetime.timedelta(hours=-4 if mar <= utc < nov else -5)


def ask_yes(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


# ------------------------------------------------------------------ commands

def cmd_status(a):
    banner("Market clock")
    skill("trader-alerts", "alerts.py", "clock")
    banner("Alpaca account")
    api = Alpaca()
    acct = api.call("GET", "/v2/account")
    print("%s account %s (%s), equity $%s" % ("PAPER" if api.paper else "LIVE", acct["account_number"],
                                              acct["status"], format(float(acct["equity"]), ",.2f")))
    cmd_score(a)


def cmd_morning(a):
    c = cfg()
    banner("Pre-market scan (daily closes)")
    args = ["daily", "--show", 25]
    if a.symbols:
        args += ["--symbols", a.symbols]
    elif a.limit:
        args += ["--limit", a.limit]
    if a.now:
        args += ["--now", a.now]
    if not (TRADER / "universe.txt").exists() and not a.symbols:
        print("First run: building the stock universe (about 5,900 tickers)...")
        skill("trader-scanner", "scan.py", "universe")
    skill("trader-scanner", "scan.py", *args)
    wl = read_json(TRADER / "watchlist.json", {})
    hits = wl.get("hits", [])
    cap = float(c["max_order_usd"])
    affordable = [h for h in hits if h.get("price") and float(h["price"]) <= cap]
    print("\n%d hits, %d priced at or under your $%.0f max order (one whole share must fit)."
          % (len(hits), len(affordable), cap))
    if not affordable:
        return 1
    wl["hits"] = affordable
    (TRADER / "watchlist.json").write_text(json.dumps(wl, indent=2))
    top = [h["symbol"] for h in affordable[: a.news]]
    banner("News on the top %d: %s" % (len(top), ", ".join(top)))
    skill("trader-news", "news.py", "--tickers", *top, "--hours", 18, "--no-sec")
    print("\nNext, after 9:50 AM New York: python trader.py hunt")
    return 0


def _verdict_ok(text):
    return not re.search(r"toward the stop", text, re.I)


def cmd_hunt(a):
    c = cfg()
    cap = float(c["max_order_usd"])
    banner("Opening-range breakout scan")
    args = ["orb", "--show", 30]
    if a.symbols:
        args += ["--symbols", a.symbols]
    elif (TRADER / "watchlist.json").exists() and not a.whole_market:
        args += ["--watchlist", "trader/watchlist.json"]
    for k in ("now", "offline"):
        if getattr(a, k):
            args += ["--" + k, getattr(a, k)]
    code, _ = skill("trader-scanner", "scan.py", *args)
    hits = [h for h in read_json(TRADER / "watchlist.json", {}).get("hits", [])
            if "orb" in h.get("setups", []) and h.get("price") and float(h["price"]) <= cap]
    if code != 0 or not hits:
        print("\nNo affordable breakouts right now. Run hunt again in 5-10 minutes.")
        return 1
    print("\n%d affordable breakouts. Working them one by one until one passes every check." % len(hits))

    clock = ["--now", a.now] if a.now else []
    for h in hits[: a.max]:
        sym = h["symbol"]
        banner("%s  $%s" % (sym, h["price"]))
        off = ["--offline", a.offline] if a.offline else []
        code, _ = skill("trader-entry", "entry.py", "check", sym, "--write", *clock, *off)
        if code != 0:
            print("-> skip %s: no confirmed entry." % sym)
            continue
        code, _ = skill("trader-exit", "exit.py", "plan", "--stop", a.stop, "--target", a.target,
                        "--write", *clock, *off)
        if code != 0:
            print("-> skip %s: exit plan refused." % sym)
            continue
        code, out = skill("trader-timemachine", "timemachine.py", "run", "--write", "--until",
                          c.get("flat_by", "15:55"), *clock, *off)
        if code == 0 and not _verdict_ok(out):
            print("-> skip %s: the Time Machine leans toward the stop." % sym)
            continue
        code, _ = skill("trader-budget", "size.py", "--write", *clock)
        if code != 0:
            print("-> skip %s: does not fit the $%s budget / $%s max loss." %
                  (sym, c["budget"], c["max_loss_per_trade_usd"]))
            continue
        code, _ = skill("trader-risk", "check.py", *clock)
        if code != 0:
            print("-> skip %s: blocked by the risk rules." % sym)
            continue
        banner("READY TICKET")
        skill("trader-broker", "ticket.py", "show", *clock)
        print("\nTo send it: python trader.py place --index 1   (you will be asked to type yes)")
        print("Then watch it: python trader.py score")
        return 0
    print("\nNothing passed every check. That is the system working: no trade is better than a bad one.")
    return 1


def cmd_place(a):
    clock = ["--now", a.now] if a.now else []
    code, _ = skill("trader-broker", "ticket.py", "show", "--index", a.index, *clock)
    if code != 0:
        return 1
    api = Alpaca()
    reply = a.reply or ask_yes("\n%s account. Place this order? Type yes or no: " %
                               ("PAPER" if api.paper else "*** LIVE ***"))
    if not reply.lower().startswith("y"):
        skill("trader-broker", "ticket.py", "decline", "--index", a.index, "--reply", reply or "no", *clock)
        return 1
    code, out = skill("trader-broker", "ticket.py", "approve", "--index", a.index, "--reply", reply, *clock)
    m = re.search(r"place_stock_order with exactly:\s*(\{.*?\n\})", out, re.S)
    if code != 0 or not m:
        return 1
    order = json.loads(m.group(1))
    try:
        res = api.call("POST", "/v2/orders", order)
    except RuntimeError as e:
        print(e)
        skill("trader-broker", "ticket.py", "result", "--index", a.index, "--status", "failed",
              "--note", str(e)[:200], *clock)
        return 1
    print("Sent to Alpaca: order %s (%s)" % (res["id"], res["status"]))
    for _ in range(15):
        time.sleep(1)
        res = api.call("GET", "/v2/orders/" + res["id"])
        if res["status"] in ("filled", "canceled", "rejected", "expired"):
            break
    extra = []
    if res.get("filled_qty") and float(res["filled_qty"]) > 0:
        extra = ["--filled-qty", res["filled_qty"], "--filled-price", res["filled_avg_price"]]
    skill("trader-broker", "ticket.py", "result", "--index", a.index, "--status", "placed",
          "--broker-id", res["id"], *extra, *clock)
    sym = order["symbol"]
    skill("trader-alerts", "alerts.py", "add", "--from-plan")
    print("Status: %s. Stop-loss and take-profit are live at Alpaca. Flat by %s: python trader.py flatten"
          % (res["status"], cfg().get("flat_by", "15:55")))
    return 0


def cmd_flatten(a):
    api = Alpaca()
    pos = [p for p in api.call("GET", "/v2/positions") if p.get("asset_class") == "us_equity"]
    if not pos:
        print("No open stock positions. You are flat.")
        return 0
    for p in pos:
        sym, qty = p["symbol"], p["qty"]
        reply = ask_yes("Close %s %s (now $%s, P&L $%s)? Type yes or no: " %
                        (qty, sym, p["current_price"], p["unrealized_pl"]))
        if not reply.lower().startswith("y"):
            continue
        for o in api.call("GET", "/v2/orders?status=open&nested=true&symbols=" + sym):
            api.call("DELETE", "/v2/orders/" + o["id"])
        time.sleep(1)
        res = api.call("DELETE", "/v2/positions/" + sym)
        print("Closing %s: order %s (%s)" % (sym, res["id"], res["status"]))
        time.sleep(2)
        res = api.call("GET", "/v2/orders/" + res["id"])
        if res["status"] == "filled":
            skill("trader-broker", "ticket.py", "fill", "--symbol", sym, "--side", "sell",
                  "--qty", res["filled_qty"], "--price", res["filled_avg_price"])
    return 0


def cmd_score(a):
    c = cfg()
    budget = float(c["budget"])
    goal = float(c.get("goal_pct_per_30min", 1.0))
    api = Alpaca()
    now = ny_now()
    day = a.date or now.strftime("%Y-%m-%d")
    fills = api.call("GET", "/v2/account/activities/FILL?date=" + day) or []
    fills = [f for f in fills if "/" not in f["symbol"] and not f["symbol"].endswith("USD")]
    cash = 0.0
    held = {}
    for f in fills:
        q, px = float(f["qty"]), float(f["price"])
        sign = 1 if f["side"] == "buy" else -1
        cash -= sign * q * px
        held[f["symbol"]] = held.get(f["symbol"], 0) + sign * q
    open_val = 0.0
    for p in api.call("GET", "/v2/positions"):
        if p.get("asset_class") == "us_equity" and held.get(p["symbol"]):
            open_val += held[p["symbol"]] * float(p["current_price"])
    pnl = cash + open_val
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    mins = max(0, (min(now, end) - start).total_seconds() / 60) if day == now.strftime("%Y-%m-%d") else 390
    periods = mins / 30
    goal_pct = ((1 + goal / 100) ** periods - 1) * 100
    banner("Scoreboard %s" % day)
    print("Start            $%.2f" % budget)
    print("Now              $%.2f   (%+.2f, %+.2f%%)" % (budget + pnl, pnl, pnl / budget * 100))
    print("Trades (fills)   %d" % len(fills))
    print("Goal so far      %+.2f%%  (%.0f%% compounded every 30 min x %.1f periods)" % (goal_pct, goal, periods))
    print("vs goal          %s" % ("AHEAD" if pnl / budget * 100 >= goal_pct else "BEHIND"))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--date")
    p = sub.add_parser("morning")
    p.add_argument("--symbols")
    p.add_argument("--limit", type=int)
    p.add_argument("--news", type=int, default=8, help="how many hits get a news check")
    p.add_argument("--now")
    p = sub.add_parser("hunt")
    p.add_argument("--symbols")
    p.add_argument("--whole-market", action="store_true", help="ignore the morning watchlist")
    p.add_argument("--max", type=int, default=10, help="most candidates to work through")
    p.add_argument("--stop", default="or-mid")
    p.add_argument("--target", default="2R")
    p.add_argument("--now", help='replay a moment, New York time: "2026-10-19 10:15"')
    p.add_argument("--offline", help="saved Yahoo data instead of the network (for testing)")
    p = sub.add_parser("place")
    p.add_argument("--index", type=int, default=1)
    p.add_argument("--reply", help="your yes, word for word (otherwise you are asked)")
    p.add_argument("--now")
    sub.add_parser("flatten")
    p = sub.add_parser("score")
    p.add_argument("--date")
    a = ap.parse_args()
    TRADER.mkdir(exist_ok=True)
    return {"status": cmd_status, "morning": cmd_morning, "hunt": cmd_hunt, "place": cmd_place,
            "flatten": cmd_flatten, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main() or 0)
