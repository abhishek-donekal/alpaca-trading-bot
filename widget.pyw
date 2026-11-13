"""Desktop widget for the trading bot: small always-on-top card.
Bot status from trader/crypto_log.jsonl (every 10 s); holdings and cash from Alpaca (every 30 s, read-only).
Drag to move. Left-click = open dashboard. Right-click = menu."""
import datetime as dt
import json
import os
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "trader" / "crypto_log.jsonl"
HUD = ROOT / "trader" / "hud.html"
POS = ROOT / "trader" / "widget_pos.json"
STALE_MIN = 20          # no log line for this long = bot probably stopped

BG, CARD, FG, MUTED = "#0f172a", "#1e293b", "#f1f5f9", "#94a3b8"
GREEN, RED, AMBER, BLUE = "#4ade80", "#f87171", "#fbbf24", "#38bdf8"


def _sunday(year, month, nth):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=nth - 1)


def to_et(t):
    edt = (dt.datetime.combine(_sunday(t.year, 3, 2), dt.time(7)) <= t
           < dt.datetime.combine(_sunday(t.year, 11, 1), dt.time(6)))
    return t - dt.timedelta(hours=4 if edt else 5)


def parse(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def money(v):
    return ("+$%.2f" if v >= 0 else "-$%.2f") % abs(v)


def short(sym):
    return sym.replace("/USD", "")


def fmt_px(p):
    if p >= 1:
        return "$%.2f" % p
    return "$" + ("%.12f" % float("%.4g" % p)).rstrip("0")


def read_state():
    try:
        lines = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-3000:]
    except OSError:
        return None
    ev = []
    for l in lines:
        try:
            ev.append(json.loads(l))
        except ValueError:
            pass
    if not ev:
        return None
    now = dt.datetime.utcnow()
    today = to_et(now).date()
    st = dict(last=parse(ev[-1]["at"]), now=now, pnl=0.0, n=0, wins=0, trades={}, scan=None, closed=[])
    for e in ev:
        t = parse(e["at"])
        k = e.get("event")
        sym = e.get("symbol")
        tr = st["trades"].get(sym)
        if k in ("entry", "adopt") and (k == "adopt" or e.get("status") == "filled"):
            px = float(e.get("price") or e.get("entry") or 0)
            if k == "entry" or not tr:
                st["trades"][sym] = dict(sym=sym, side=e.get("side", "long"), qty=float(e.get("qty") or 0),
                                         entry=px, pct=None, price=None, stop=None, target=None, since=t)
            else:
                tr["qty"] = float(e.get("qty") or tr["qty"])
            if k == "entry" and to_et(t).date() == today:
                st["n"] += 1
        elif k == "watch" and tr:
            tr.update(pct=e.get("pnl_pct"), price=e.get("price"), stop=e.get("stop"),
                      target=e.get("target"), trailing=e.get("trailing"))
        elif k == "done":
            p = float(e.get("pnl_usd") or 0)
            if to_et(t).date() == today:
                st["pnl"] += p
                st["wins"] += p > 0
                st["closed"].append((short(e["symbol"]), p))
            st["trades"].pop(sym, None)
        elif k == "scan":
            st["scan"] = dict(at=t, best=e.get("best"), q=e.get("qualifying"), nxt=e.get("next"))
    return st


def _env():
    out = {}
    for l in (ROOT / ".env").read_text().splitlines():
        if "=" in l and not l.lstrip().startswith("#"):
            k, v = l.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def fetch_holdings():
    """Read-only: account cash and every open position. Crypto is valued at the live bid (what a sale gets now),
    because Alpaca values thin coins like PEPE at their last trade, which can be hours old."""
    k = _env()
    h = {"APCA-API-KEY-ID": k["APCA_API_KEY_ID"], "APCA-API-SECRET-KEY": k["APCA_API_SECRET_KEY"]}
    base = k.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

    def get(u):
        with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=15) as r:
            return json.load(r)
    acct, pos = get(base + "/v2/account"), get(base + "/v2/positions")
    coins = [p["symbol"][:-3] + "/USD" for p in pos if p.get("asset_class") == "crypto"]
    bids = {}
    if coins:
        try:
            q = get("https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols="
                    + urllib.parse.quote(",".join(coins)))
            bids = {s: float(v["bp"]) for s, v in q.get("quotes", {}).items() if float(v.get("bp") or 0) > 0}
        except Exception:
            pass
    rows = []
    for p in pos:
        qty, cost = float(p["qty"]), float(p["cost_basis"])
        if p.get("asset_class") == "crypto":
            name = p["symbol"][:-3]
            val = qty * bids.get(name + "/USD", float(p["current_price"]))
        else:
            name, val = p["symbol"], float(p["market_value"])
        pl = val - cost  # shorts: both negative, so a falling price gives a positive P&L
        rows.append(dict(name=name, qty=qty, val=val, pl=pl, pct=pl / abs(cost) * 100 if cost else 0.0,
                         short=p.get("side") == "short"))
    rows.sort(key=lambda r: -abs(r["val"]))
    cash = float(acct["cash"])
    return dict(rows=rows, cash=cash, total=cash + sum(r["val"] for r in rows), at=time.time())


def fmt_qty(q):
    q = abs(q)
    if q >= 1e9:
        return "%.2fB" % (q / 1e9)
    if q >= 1e6:
        return "%.1fM" % (q / 1e6)
    if q >= 1000:
        return "{:,.0f}".format(q)
    return ("%.4f" % q).rstrip("0").rstrip(".")


class Widget(tk.Tk):
    def __init__(self):
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.95)
        self.configure(bg=BG)
        self.top = tk.BooleanVar(value=True)
        x, y = self._load_pos()
        self.geometry("+%d+%d" % (x, y))

        f = tk.Frame(self, bg=CARD, padx=14, pady=10, highlightthickness=1, highlightbackground="#334155")
        f.pack()
        hd = tk.Frame(f, bg=CARD)
        hd.pack(fill="x")
        self.dot = tk.Label(hd, text="●", bg=CARD, fg=GREEN, font=("Segoe UI", 11))
        self.dot.pack(side="left")
        tk.Label(hd, text=" Trading Bot", bg=CARD, fg=FG, font=("Segoe UI Semibold", 11)).pack(side="left")
        self.clock = tk.Label(hd, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
        self.clock.pack(side="right")
        self.status = tk.Label(f, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 9), anchor="w", justify="left")
        self.status.pack(fill="x", pady=(2, 8))

        box = tk.Frame(f, bg=BG, padx=10, pady=8)
        box.pack(fill="x")
        self.t1 = tk.Label(box, text="", bg=BG, fg=FG, font=("Segoe UI Semibold", 10), anchor="w")
        self.t1.pack(fill="x")
        self.t2 = tk.Label(box, text="", bg=BG, fg=GREEN, font=("Segoe UI", 18, "bold"), anchor="w")
        self.t2.pack(fill="x")
        self.t3 = tk.Label(box, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w", justify="left")
        self.t3.pack(fill="x")

        ft = tk.Frame(f, bg=CARD)
        ft.pack(fill="x", pady=(8, 0))
        tk.Label(ft, text="Today", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        self.today = tk.Label(ft, text="", bg=CARD, fg=FG, font=("Segoe UI Semibold", 10))
        self.today.pack(side="left", padx=(6, 0))
        self.count = tk.Label(ft, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
        self.count.pack(side="right")
        self.recent = tk.Label(f, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w")
        self.recent.pack(fill="x")

        tk.Frame(f, bg="#334155", height=1).pack(fill="x", pady=(8, 6))
        hh = tk.Frame(f, bg=CARD)
        hh.pack(fill="x")
        tk.Label(hh, text="Holdings", bg=CARD, fg=FG, font=("Segoe UI Semibold", 10)).pack(side="left")
        self.hold_meta = tk.Label(hh, text="loading...", bg=CARD, fg=MUTED, font=("Segoe UI", 8))
        self.hold_meta.pack(side="right")
        self.hold = tk.Frame(f, bg=CARD)
        self.hold.pack(fill="x", pady=(2, 0))
        self.totals = tk.Label(f, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 9), anchor="w")
        self.totals.pack(fill="x", pady=(4, 0))
        self.holdings, self.hold_err, self._hold_sig = None, None, None
        threading.Thread(target=self._hold_loop, daemon=True).start()

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Open dashboard", command=self.open_hud)
        self.menu.add_checkbutton(label="Always on top", variable=self.top,
                                  command=lambda: self.attributes("-topmost", self.top.get()))
        self.menu.add_command(label="Refresh now", command=self.refresh)
        self.menu.add_separator()
        self.menu.add_command(label="Close widget", command=self.destroy)

        for w in self._all(self):
            self._hook(w)
        self._moved = False
        self.refresh()

    def _hook(self, w):
        w.bind("<ButtonPress-1>", self._press)
        w.bind("<B1-Motion>", self._drag)
        w.bind("<ButtonRelease-1>", self._release)
        w.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

    def _hold_loop(self):
        while True:
            try:
                self.holdings, self.hold_err = fetch_holdings(), None
            except Exception as e:
                self.hold_err = str(e)[:60]
            time.sleep(30)

    def _render_holdings(self):
        hd = self.holdings
        if self.hold_err:
            self.hold_meta.config(text="offline: retrying", fg=AMBER)
        elif hd:
            self.hold_meta.config(text="updated %ds ago" % (time.time() - hd["at"]), fg=MUTED)
        if not hd:
            return
        sig = [(r["name"], round(r["val"], 2), round(r["pl"], 2)) for r in hd["rows"]]
        if sig != self._hold_sig:
            self._hold_sig = sig
            for c in self.hold.winfo_children():
                c.destroy()
            if not hd["rows"]:
                lb = tk.Label(self.hold, text="Nothing held: all cash", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
                lb.grid(row=0, column=0, sticky="w")
                self._hook(lb)
            for i, r in enumerate(hd["rows"]):
                col = GREEN if r["pl"] >= 0 else RED
                cells = [("%s%s" % (r["name"], " (short)" if r["short"] else ""), FG, "w", 0),
                         (fmt_qty(r["qty"]), MUTED, "e", 1),
                         ("${:,.2f}".format(abs(r["val"])), FG, "e", 2),
                         ("%s (%+.1f%%)" % (money(r["pl"]), r["pct"]), col, "e", 3)]
                for text, fg, anchor, c in cells:
                    lb = tk.Label(self.hold, text=text, bg=CARD, fg=fg, font=("Segoe UI", 9), anchor=anchor)
                    lb.grid(row=i, column=c, sticky="we", padx=(0 if c == 0 else 8, 0))
                    self._hook(lb)
            self.hold.grid_columnconfigure(0, weight=1)
        inv = sum(abs(r["val"]) for r in hd["rows"])
        self.totals.config(text="Invested ${:,.0f}  ·  Cash ${:,.0f}  ·  Total ${:,.2f}".format(
            inv, hd["cash"], hd["total"]))

    def _all(self, w):
        out = [w]
        for c in w.winfo_children():
            out += self._all(c)
        return out

    def _press(self, e):
        self._dx, self._dy, self._moved = e.x_root - self.winfo_x(), e.y_root - self.winfo_y(), False

    def _drag(self, e):
        self._moved = True
        self.geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))

    def _release(self, e):
        if self._moved:
            try:
                POS.write_text(json.dumps([self.winfo_x(), self.winfo_y()]))
            except OSError:
                pass
        else:
            self.open_hud()

    def _load_pos(self):
        try:
            x, y = json.loads(POS.read_text())
            try:  # all monitors together, not just the main one
                import ctypes
                m = ctypes.windll.user32.GetSystemMetrics
                vx, vy, vw, vh = m(76), m(77), m(78), m(79)
            except Exception:
                vx, vy, vw, vh = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()
            if vx <= x < vx + vw - 50 and vy <= y < vy + vh - 50:
                return x, y
        except (OSError, ValueError, TypeError):
            pass
        return self.winfo_screenwidth() - 330, 40

    def open_hud(self):
        if HUD.exists():
            os.startfile(str(HUD))

    def refresh(self):
        try:
            self._render(read_state())
        except Exception as e:  # never let the widget die
            self.status.config(text="Error reading log: %s" % e, fg=RED)
        try:
            self._render_holdings()
        except Exception as e:
            self.hold_meta.config(text="error: %s" % str(e)[:40], fg=RED)
        self.after(10000, self.refresh)

    def _render(self, st):
        now_et = to_et(dt.datetime.utcnow())
        self.clock.config(text=now_et.strftime("%I:%M %p ET").lstrip("0"))
        if not st:
            self.dot.config(fg=RED)
            self.status.config(text="No bot log found", fg=RED)
            return
        age = (st["now"] - st["last"]).total_seconds() / 60
        alive = age < STALE_MIN
        self.dot.config(fg=GREEN if alive else RED)
        last = to_et(st["last"]).strftime("%I:%M %p").lstrip("0")
        self.status.config(text=("Running · last check %s" % last) if alive else
                           ("NOT RUNNING? No activity since %s" % last), fg=MUTED if alive else RED)

        trades = list(st["trades"].values())
        tr = trades[0] if len(trades) == 1 else None
        if len(trades) > 1:
            tot = sum((t["pct"] or 0) / 100 * t["entry"] * t["qty"] for t in trades)
            self.t1.config(text="%d open trades" % len(trades))
            self.t2.config(text=money(tot), fg=GREEN if tot >= 0 else RED)
            self.t3.config(text="\n".join("%s %s  %s (%+.2f%%)%s" % (
                "SHORT" if t["side"] == "short" else "LONG", short(t["sym"]),
                money((t["pct"] or 0) / 100 * t["entry"] * t["qty"]), t["pct"] or 0,
                "  trailing" if t.get("trailing") else "") for t in trades))
        elif tr:
            pct = tr["pct"]
            usd = (pct or 0) / 100 * tr["entry"] * tr["qty"]
            col = GREEN if (pct or 0) >= 0 else RED
            self.t1.config(text="%s %s  ·  in trade since %s" % (
                "SHORT" if tr["side"] == "short" else "LONG", short(tr["sym"]),
                to_et(tr["since"]).strftime("%I:%M %p").lstrip("0")))
            self.t2.config(text=("%s  (%+.2f%%)" % (money(usd), pct)) if pct is not None else "waiting for price…",
                           fg=col)
            lines = ["Entry %s  →  now %s" % (fmt_px(tr["entry"]), fmt_px(tr["price"]) if tr["price"] else "…")]
            if tr["stop"] and tr["target"]:
                lines.append("Stop %s   Target %s%s" % (fmt_px(tr["stop"]), fmt_px(tr["target"]),
                                                        "   (trailing)" if tr.get("trailing") else ""))
            self.t3.config(text="\n".join(lines))
        else:
            sc = st["scan"]
            self.t1.config(text="No open trade")
            self.t2.config(text="Scanning", fg=BLUE)
            if sc:
                nxt = "?"
                try:
                    h, m_, s_ = map(int, sc["nxt"].split(":"))
                    t = sc["at"].replace(hour=h, minute=m_, second=s_)
                    t += dt.timedelta(days=1) if t < sc["at"] else dt.timedelta(0)
                    nxt = to_et(t).strftime("%I:%M %p").lstrip("0") + " ET"
                except (AttributeError, ValueError):
                    pass
                self.t3.config(text="%s setup(s) qualify · best %s\nNext check %s" % (
                    sc["q"], short(sc["best"] or "-"), nxt))
            else:
                self.t3.config(text="")

        self.today.config(text=money(st["pnl"]), fg=GREEN if st["pnl"] > 0 else RED if st["pnl"] < 0 else FG)
        self.count.config(text="%d trades · %d won" % (st["n"], st["wins"]))
        self.recent.config(text="  ".join("%s %s" % (s, money(p)) for s, p in st["closed"][-4:]))


if __name__ == "__main__":
    Widget().mainloop()
