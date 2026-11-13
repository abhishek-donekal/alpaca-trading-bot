# Trading desk runbook

Paper account, $100 budget, US stocks, long only, flat by 3:55 PM New York.
Every order needs someone to type **yes** for that exact ticket.

## The day (New York time)

| When | Command | What happens |
|---|---|---|
| Before 9:30 | `python trader.py morning` | Scans ~5,900 stocks for setups, keeps the ones a $95 order can buy, checks their news |
| 9:50 onward, every 5–10 min | `python trader.py hunt` | Breakout scan → entry → stop/target → 10,000-replay Time Machine → sizing → risk check. Stops at the first trade that passes everything |
| When a ticket is READY | `python trader.py place` | Shows the ticket, asks for **yes**, sends a bracket order (entry + stop-loss + take-profit) to Alpaca |
| Any time | `python trader.py score` | P&L on the $100 vs the 1%-per-30-min goal |
| 3:55 | `python trader.py flatten` | Closes anything still open (asks yes per stock) |

`python trader.py status` shows the market clock, the account and the scoreboard.
Faster morning scan: `python trader.py morning --limit 500`. Specific stocks: `hunt --symbols AMD,F,SOFI`.

## The rules (trader/config.json)

| Setting | Value | Meaning |
|---|---|---|
| budget | $100 | Money the system may use |
| max_loss_per_trade_usd | $2 | Loss if the stop fills at its price (2% of budget) |
| max_daily_loss_usd | $6 | No new trades once today's risk reaches this |
| max_trades_per_day | 6 | |
| min_reward_risk | 1.5 | Target must be at least 1.5× the risk; default target is 2R |
| no_entries_before / after | 9:45 / 15:30 | Entry window |
| paper | true | Keep it true |

Change limits only by editing that file, and agree on changes as a team first.

## What to expect

- **The 1%-per-30-min goal is a scoreboard, not a forecast.** It compounds to +13.8% a day. No strategy sustains that, and the scoreboard will mostly read BEHIND.
- **Many runs end with "Nothing passed every check."** That's the system working as designed.
- **One whole share has to fit in $95.** That rules out stocks like TSLA and NVDA.
- **Market data is free and delayed (Yahoo).** The broker's price is the one that fills, and a stop can fill worse than its price in a fast market.
- **Crypto isn't part of this pipeline.** Use `python crypto_order.py XRP/USD buy 10` separately.

## Using it through Claude

In Claude Code in this folder, ask in plain words, like "scan the market", "any news on AMD?", "run the time machine" or "what are the odds?". Claude uses the matching `/trader-*` skill. It still needs your typed yes before any order.
