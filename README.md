# FTMO Scalping Bot

Automated scalping bot for a $10,000 FTMO Phase 1 Challenge, trading
XAUUSD, XAGUSD, USDJPY, EURUSD, USDCHF, US500.cash, and US100.cash with
fully automatic execution via MT5, and live Telegram trade alerts.

Strategy: a 15-minute EMA(9/21) trend filter combined with a 5-minute
liquidity-sweep entry trigger. Only trades in the direction of the
higher-timeframe bias, and only when the 15M chart is both trending
(ADX >= `MIN_ADX`) and volatile enough (ATR >= `MIN_ATR_RATIO` of its
own recent average) — this filters out dead sessions and choppy/ranging
conditions, which is where this style of entry produces the most false
signals live.

## ⚠️ Before you do anything else

- This runs on a **Windows PC/VPS with the MT5 terminal installed** —
  MetaTrader5's Python package only works on Windows.
- Test on an **MT5 Demo account** for at least 2-4 weeks before ever
  pointing this at your real FTMO Challenge account.
- FTMO's exact rule percentages (daily loss, max drawdown, consistency
  rule wording) can change by account type — check your FTMO dashboard
  and update `config.py` if anything differs from the defaults below.
- This is a technical tool, not financial advice. Automated trading can
  lose money quickly, including the full cost of your challenge fee.

## Setup

1. Install Python 3.10+ on the Windows machine that has MT5 installed.
2. Create a virtual environment and install dependencies:
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in:
   - Your MT5 demo (then later, challenge) account login/password/server
   - Your Telegram bot token and chat ID (from BotFather)
4. Review every constant in `config.py`, especially:
   - `DAILY_LOSS_LIMIT_PCT`, `MAX_DRAWDOWN_PCT` — confirm against your
     current FTMO account terms
   - `RISK_PERCENT_PER_TRADE` — 0.35% is a starting point, not gospel
   - `SYMBOLS` — currently XAUUSD, XAGUSD, USDJPY, EURUSD, USDCHF,
     US500.cash, US100.cash. **Verify each name matches your broker's
     exact Market Watch spelling** — index/metal symbol names vary a lot
     between brokers (e.g. "US100.cash" vs "US100" vs "NAS100m")
   - `SYMBOL_MAX_SPREAD_POINTS` — these are illustrative starting values.
     Gold, silver, and index CFDs trade on a different point scale than
     forex majors, so pull up each symbol's typical live spread in MT5
     and calibrate before trusting these numbers

## Running

```
python main.py
```

The bot polls every 30 seconds, checks the FTMO compliance guard before
every trade, and sends a Telegram message for every trade opened, trade
blocked, compliance event, Friday flatten, and a daily summary at 21:00 UTC.

## File overview

| File | Responsibility |
|---|---|
| `config.py` | All tunable constants, including FTMO rule numbers |
| `mt5_connector.py` | MT5 login, account info, candle data |
| `scanner.py` | Trend bias, ATR/ADX volatility-trend filter, and liquidity sweep signal generation |
| `compliance.py` | FTMO daily loss / drawdown / consistency circuit breakers |
| `risk.py` | Position sizing (lots) for a cleared trade |
| `executor.py` | Spread filter, cooldown, order placement, flattening |
| `notifier.py` | Telegram messages for every event |
| `main.py` | Polling loop + scheduler (Friday flatten, daily summary) |
| `backtest.py` | Offline historical replay using the live strategy/compliance code |

## Testing protocol (do not skip)

**Stage 1 — Backtest (do this first, before touching demo or live):**

```
python backtest.py --days 180
```

This replays 180 days of real historical price data from your MT5
terminal through the *exact same* strategy and compliance code the live
bot uses — no orders are placed, it's read-only. It prints a report
(win rate, total P&L, worst single day vs. FTMO's actual daily loss
limit, max drawdown vs. FTMO's actual limit, whether the profit target
would've been reached) and writes every simulated trade to
`backtest_results.csv`.

Useful variations:
```
python backtest.py --days 90 --symbols XAUUSD,EURUSD
python backtest.py --days 365
```

Read the simplifications listed at the top of `backtest.py` before
trusting the numbers too literally — it uses a simplified fill model
(no partial fills, approximate spread cost, P&L booked at entry time
rather than exact exit time) so treat it as a sanity check on the
strategy and risk settings, not a guarantee of live results.

**Stage 2 — Demo forward test:**

1. Backtest the trend + liquidity-sweep logic on 6-12 months of history
   per symbol before any live testing (Stage 1 above).
2. Run on MT5 Demo for 2-4 weeks minimum.
3. Deliberately simulate a losing day on demo to confirm the daily-loss
   circuit breaker in `compliance.py` actually halts new trades.
4. Confirm Telegram alerts arrive correctly for opens, closes, and
   compliance blocks.
5. Only then consider pointing it at the real FTMO Challenge account —
   and start with reduced size until you trust it.

## Kill switch

`main.py` has a `KILL_SWITCH_ENGAGED` flag. Set it to `True` (or wire it
to a Telegram command handler) to immediately stop new order placement
without killing the process — existing positions are left alone but no
new trades will open.
