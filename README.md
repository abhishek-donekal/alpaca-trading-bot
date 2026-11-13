# Alpaca trading bot

An automated trading bot for an [Alpaca](https://alpaca.markets) **paper** account. It trades crypto around the clock and US stocks during market hours, long and short. It comes with a live dashboard, an isometric "war room" HUD and a Windows desktop widget.

> Paper trading only. This is an experiment, not financial advice, and past results say nothing about future ones.

## What's here

| File | What it does |
|---|---|
| `crypto_bot.py` | The bot. Scans crypto and stocks, sizes each trade by risk, places orders and manages exits: stop, trailing stop and target. Every position also gets a backup stop order held at Alpaca. |
| `dashboard.py` | Builds `trader/dashboard.html` from the bot's log: trades, P&L and open positions. |
| `hud.py` | Builds `trader/hud.html`, an animated isometric view of the bot at work. |
| `widget.pyw` | Always-on-top desktop widget showing bot status and live holdings (Windows, tkinter). |
| `trader.py` | Manual day-trading pipeline: scan → entry → exit plan → simulation → sizing → risk check → order, with a "yes" before every order. See `RUNBOOK.md`. |
| `connect.py`, `crypto_order.py` | Connection test and one-off crypto orders. |
| `trader/config.json` | Budget and risk limits. |
| `.claude/skills/` | The trader-* skills that `trader.py` uses (MIT, see `DAY-TRADER-LICENSE`). |

## How the bot sizes trades

- Pool: `--usd` (default from `trader/config.json`).
- Each trade risks about 0.5% of the pool at its stop. That amount is scaled up or down by conviction: how often the setup has worked before, the bot's own recent record, losing streaks, a recent loss on the same symbol, and meme-coin status.
- Caps: 25% of the pool per position, 40% per sector, at most 4 positions open at once.

## Setup

```bash
# Python 3.10+, standard library only
cp .env.example .env          # then add your Alpaca paper keys
python connect.py             # check the connection
python crypto_bot.py auto --usd 10000
```

`.env` is git-ignored. Never commit API keys.
