"""
Offline historical backtest for the FTMO scalping bot.

Reuses the EXACT SAME StrategyEngine (scanner.py) and FTMOComplianceGuard
(compliance.py) that main.py uses live — this backtest is not a separate
reimplementation, so a strategy tweak or a risk-rule change is tested by
this file automatically without needing to update it in two places.

This ONLY READS historical data via mt5.copy_rates_range() and never sends
an order. It is safe to run against any account, including a live one,
since it makes no trade calls.

Must run on the Windows machine with the MT5 terminal open and logged in,
since historical data comes straight from the terminal.

Usage:
    python backtest.py --days 180
    python backtest.py --days 90 --symbols XAUUSD,EURUSD

IMPORTANT SIMPLIFICATIONS (read before trusting the numbers):
  - Trade management is simulated candle-by-candle: breakeven-at-1R and
    trailing-at-1.5R use only the PRIOR candle's information to decide the
    current stop level before checking whether the CURRENT candle's high/low
    breaches it — this avoids using a candle's own extreme to move the stop
    and then checking that same candle against it, which would be lookahead.
  - P&L is computed from wherever the trade actually exited (initial stop,
    breakeven, a trailed stop level, or the wide hard-cap), not a fixed
    reward:risk shortcut — this generalizes correctly now that exits are
    dynamic rather than always landing on a fixed TP or fixed SL.
  - Slippage is OFF by default (0 pips) — pass --slippage-pips to add it.
    When enabled, it only worsens fills on STOP exits (initial stop,
    breakeven, or trailed stop), not on hard-cap/take-profit hits or
    timeouts, since stop orders are what actually slip against you live;
    limit-style fills don't. Spread cost is applied separately either way.
  - Account equity/compliance bookkeeping books each trade's P&L at its
    ENTRY time (not its actual exit time, which may be hours later), so
    daily-loss-limit accounting is an approximation, not exact.
  - Multiple symbols are simulated in true chronological order (merged by
    candle timestamp), so the compliance guard's daily/drawdown state is
    shared correctly across symbols, not evaluated per-symbol in isolation.

This tool tells you roughly how the strategy would have performed and
roughly how close it would have come to FTMO's limits — treat it as a
sanity check before demo testing, not as a guarantee of live results.
"""
import argparse
import csv
import logging
import os
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import pandas as pd

import config
import indicators
from compliance import FTMOComplianceGuard
from mt5_connector import MT5Connector, TIMEFRAME_MAP
from scanner import StrategyEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest")

WARMUP_BARS = 120     # bars needed before ATR/ADX/EMA readings are trustworthy
MAX_HOLD_BARS = 200   # ~16.6 hours on 5M candles; force a timeout exit beyond this

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}


def closed_slice(df: pd.DataFrame, timeframe_key: str, current_time) -> pd.DataFrame:
    """
    Slices a higher-timeframe dataframe to only bars FULLY CLOSED as of
    current_time (an LTF timestamp). A bar labeled with open-time T covers
    [T, T + bar_duration) — plain `df["time"] <= current_time` wrongly
    includes a bar that's still forming from current_time's perspective
    whenever current_time falls inside that same bar's period, and since
    fetch_history() only ever returns already-closed historical bars, that
    row's high/low/close are already the bar's FINAL values — a lookahead
    leak. This subtracts the bar duration before comparing so only bars
    that have actually finished are visible.
    """
    bar_minutes = TIMEFRAME_MINUTES[timeframe_key]
    cutoff = current_time - timedelta(minutes=bar_minutes)
    return df[df["time"] <= cutoff]


def fetch_history(symbol: str, timeframe_key: str, days: int) -> pd.DataFrame | None:
    tf = TIMEFRAME_MAP[timeframe_key]
    info = mt5.symbol_info(symbol)
    if info is not None and not info.visible:
        mt5.symbol_select(symbol, True)  # copy_rates_range returns nothing for symbols not in Market Watch
    # copy_rates_range's date_from/date_to are compared against candle epochs
    # that were generated from the BROKER's server wall clock (see
    # config.BROKER_UTC_OFFSET_HOURS — confirmed ~3h ahead of true UTC), not
    # true UTC. A true-UTC "now" as the upper bound silently excludes
    # whatever's most recent — up to BROKER_UTC_OFFSET_HOURS worth of bars —
    # since the broker-clock-labeled epoch for "just now" reads as if it
    # were that many hours in the future relative to a true-UTC boundary.
    utc_to = datetime.now(timezone.utc) + timedelta(hours=config.BROKER_UTC_OFFSET_HOURS)
    utc_from = utc_to - timedelta(days=days)
    rates = mt5.copy_rates_range(symbol, tf, utc_from, utc_to)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def _classify_exit(exit_price: float, entry_price: float, direction: str) -> str:
    if direction == "BUY":
        if exit_price > entry_price:
            return "WIN"
        if exit_price < entry_price:
            return "LOSS"
        return "BREAKEVEN"
    else:
        if exit_price < entry_price:
            return "WIN"
        if exit_price > entry_price:
            return "LOSS"
        return "BREAKEVEN"


def simulate_trade_with_trailing(df_ltf: pd.DataFrame, entry_index: int, direction: str,
                                  entry_price: float, initial_stop: float, hard_cap: float,
                                  atr_series: pd.Series, symbol: str = None,
                                  slippage_distance: float = 0.0) -> tuple[str, int, float]:
    """
    Walks forward candle-by-candle simulating breakeven-then-trail
    management. Returns (outcome, exit_index, exit_price).

    To avoid lookahead, each candle is checked against the stop level
    decided using only PRIOR candles, and only AFTER that check does this
    candle's own extreme get folded into the trailing calculation for the
    NEXT candle.

    slippage_distance (a price distance, not pips) is subtracted from BUY
    stop exits / added to SELL stop exits — i.e. always worsened — since
    that's the only exit type modeled as a stop order here. hard_cap exits
    are unaffected.
    """
    risk = abs(entry_price - initial_stop)
    if risk <= 0:
        return "LOSS", entry_index, initial_stop

    current_stop = initial_stop
    breakeven_done = False
    high_water = entry_price  # best favorable price reached since entry
    end = min(entry_index + MAX_HOLD_BARS, len(df_ltf) - 1)

    for i in range(entry_index + 1, end + 1):
        row = df_ltf.iloc[i]

        # 1. Check exit using the stop decided from PRIOR candles only.
        if direction == "BUY":
            if row["low"] <= current_stop:
                exit_price = current_stop - slippage_distance
                return _classify_exit(exit_price, entry_price, direction), i, exit_price
            if row["high"] >= hard_cap:
                return "WIN", i, hard_cap
        else:
            if row["high"] >= current_stop:
                exit_price = current_stop + slippage_distance
                return _classify_exit(exit_price, entry_price, direction), i, exit_price
            if row["low"] <= hard_cap:
                return "WIN", i, hard_cap

        # 2. Fold this candle's extreme into the trailing state for NEXT candle.
        atr_i = atr_series.iloc[i] if i < len(atr_series) else None
        atr_i = atr_i if pd.notna(atr_i) else None

        if direction == "BUY":
            high_water = max(high_water, row["high"])
            profit_r = (high_water - entry_price) / risk
        else:
            high_water = min(high_water, row["low"])
            profit_r = (entry_price - high_water) / risk

        if not breakeven_done and profit_r >= config.BREAKEVEN_TRIGGER_R:
            current_stop = max(current_stop, entry_price) if direction == "BUY" else min(current_stop, entry_price)
            breakeven_done = True

        if profit_r >= config.TRAIL_TRIGGER_R and atr_i:
            trail_multiple = config.SYMBOL_TRAIL_ATR_MULTIPLE.get(symbol, config.TRAIL_ATR_MULTIPLE)
            if direction == "BUY":
                trail_stop = high_water - trail_multiple * atr_i
                current_stop = max(current_stop, trail_stop)
            else:
                trail_stop = high_water + trail_multiple * atr_i
                current_stop = min(current_stop, trail_stop)

    return "TIMEOUT", end, current_stop


def simulate_trade_fixed_rr(df_ltf: pd.DataFrame, entry_index: int, direction: str,
                             entry_price: float, initial_stop: float, rr_multiple: float,
                             slippage_distance: float = 0.0) -> tuple[str, int, float]:
    """
    Static stop, static take-profit at rr_multiple x initial risk — no
    breakeven, no trailing (contrast with simulate_trade_with_trailing).
    Returns (outcome, exit_index, exit_price). Same no-lookahead structure:
    each candle is checked against fixed, pre-determined levels.
    """
    risk = abs(entry_price - initial_stop)
    if risk <= 0:
        return "LOSS", entry_index, initial_stop

    take_profit = entry_price + rr_multiple * risk if direction == "BUY" else entry_price - rr_multiple * risk
    end = min(entry_index + MAX_HOLD_BARS, len(df_ltf) - 1)

    for i in range(entry_index + 1, end + 1):
        row = df_ltf.iloc[i]
        if direction == "BUY":
            if row["low"] <= initial_stop:
                exit_price = initial_stop - slippage_distance
                return _classify_exit(exit_price, entry_price, direction), i, exit_price
            if row["high"] >= take_profit:
                return "WIN", i, take_profit
        else:
            if row["high"] >= initial_stop:
                exit_price = initial_stop + slippage_distance
                return _classify_exit(exit_price, entry_price, direction), i, exit_price
            if row["low"] <= take_profit:
                return "WIN", i, take_profit

    # Timed out without hitting either level — mark to market at the last checked close.
    return "TIMEOUT", end, float(df_ltf.iloc[end]["close"])


def run_backtest(days: int, symbols: list[str], slippage_pips: float = 0.0,
                  liquidity_level_timeframe: str = None,
                  trade_management: str = None, rr_multiple: float = None,
                  strategy_mode: str = "sweep"):
    """
    liquidity_level_timeframe (experimental, off by default): when set (e.g.
    "M15"), the liquidity-sweep's swing high/low is computed from that
    timeframe instead of LTF_TIMEFRAME — see detect_liquidity_sweep's
    df_level_tf parameter in scanner.py.

    trade_management: "trailing" (breakeven then ATR trail) or "fixed_rr"
    (static stop + static TP at rr_multiple x risk, no breakeven/trailing).
    Defaults to whatever config.FIXED_RR_ENABLED says live will actually do
    (and rr_multiple defaults to config.FIXED_RR_MULTIPLE), so calling this
    with no override always mirrors the current live config — pass these
    explicitly only to test a variant AGAINST that default.

    strategy_mode: "sweep" (default, the liquidity-sweep reversal strategy —
    everything below behaves exactly as before) or "momentum" (the second,
    independent trend-continuation strategy — see evaluate_momentum() /
    detect_momentum_breakout() in scanner.py). Momentum mode ignores H4/H4
    bias, HTF, and liquidity-level data entirely (it doesn't use them) and
    always trades fixed-RR at config.MOMENTUM_RR_MULTIPLE regardless of
    trade_management/rr_multiple, since it has no trailing-mode equivalent.
    """
    if trade_management is None:
        trade_management = "fixed_rr" if config.FIXED_RR_ENABLED else "trailing"
    if rr_multiple is None:
        rr_multiple = config.FIXED_RR_MULTIPLE
    if trade_management == "fixed_rr" and not rr_multiple:
        raise ValueError("rr_multiple is required when trade_management='fixed_rr'")
    if strategy_mode == "momentum":
        trade_management = "fixed_rr"
        rr_multiple = config.MOMENTUM_RR_MULTIPLE
    connector = MT5Connector()
    if not connector.connect():
        logger.error("Could not connect to MT5. Backtest requires the MT5 terminal to be running and logged in.")
        return

    strategy = StrategyEngine()
    # A dedicated, always-fresh state file — NEVER the live default
    # (compliance.py's DEFAULT_STATE_FILE). Found 2026-08-19: every prior
    # backtest run here shared that default with main.py's live guard, so a
    # backtest breach got persisted into the file the LIVE bot reads (and,
    # separately, one backtest run's leftover breach could silently block
    # EVERY trade in the next unrelated backtest run before it ever reached
    # strategy evaluation — compliance checks run before signal evaluation
    # in the loop below). Deleting first guarantees each run starts clean,
    # independent of whatever any previous backtest run left behind.
    backtest_state_file = "backtest_compliance_state.json"
    if os.path.exists(backtest_state_file):
        os.remove(backtest_state_file)
    guard = FTMOComplianceGuard(starting_balance=config.STARTING_BALANCE, state_file=backtest_state_file)
    equity = config.STARTING_BALANCE

    htf_data, ltf_data, h4_data, level_data, ltf_atr_data, spread_cost, slippage_distance = {}, {}, {}, {}, {}, {}, {}

    for symbol in symbols:
        logger.info("Fetching %d days of history for %s ...", days, symbol)
        df_htf = fetch_history(symbol, config.HTF_TIMEFRAME, days)
        df_ltf = fetch_history(symbol, config.LTF_TIMEFRAME, days)
        if df_htf is None or df_ltf is None or len(df_ltf) < WARMUP_BARS + 10:
            logger.warning(
                "No/insufficient historical data for %s — skipping. "
                "Check the symbol name matches your broker's Market Watch exactly.", symbol
            )
            continue

        symbol_info = connector.get_symbol_info(symbol)
        spread_pts = config.SYMBOL_MAX_SPREAD_POINTS.get(symbol, config.DEFAULT_MAX_SPREAD_POINTS)
        if symbol_info:
            spread_cost[symbol] = spread_pts * symbol_info.point * symbol_info.trade_tick_value
            # 1 pip = 10 points on 5-/3-digit-quoted symbols (the same convention
            # SYMBOL_MAX_SPREAD_POINTS's "~X pips" comments in config.py use).
            slippage_distance[symbol] = slippage_pips * 10 * symbol_info.point
        else:
            spread_cost[symbol] = 0.0
            slippage_distance[symbol] = 0.0

        htf_data[symbol] = df_htf
        ltf_data[symbol] = df_ltf
        if config.H4_BIAS_FILTER_ENABLED:
            # Per-symbol override lets one symbol run a different bias
            # timeframe than the rest of the basket (e.g. the 2026-08-26
            # XAUUSD trial's H1 vs USDCAD's normal H4) -- falls back to the
            # global H4_TIMEFRAME for any symbol not listed there.
            h4_data[symbol] = fetch_history(symbol, config.SYMBOL_H4_TIMEFRAME_OVERRIDE.get(symbol, config.H4_TIMEFRAME), days)
        # Same idea for the liquidity level timeframe: a per-symbol config
        # override takes priority; the function's own liquidity_level_timeframe
        # argument is the fallback for any symbol not in that override dict
        # (keeps existing callers that pass it directly working unchanged).
        symbol_level_tf = config.SYMBOL_LIQUIDITY_LEVEL_TIMEFRAME_OVERRIDE.get(symbol, liquidity_level_timeframe)
        if symbol_level_tf:
            level_data[symbol] = fetch_history(symbol, symbol_level_tf, days)
        # Precompute once per symbol so the trailing simulation doesn't
        # recompute ATR on every candle it walks through.
        ltf_atr_data[symbol] = indicators.atr(df_ltf["high"], df_ltf["low"], df_ltf["close"],
                                               length=config.LTF_ATR_LOOKBACK)

    if not ltf_data:
        logger.error("No usable symbol data fetched. Aborting backtest.")
        connector.shutdown()
        return

    # Build one chronologically-sorted event list across all symbols so the
    # compliance guard's shared daily/drawdown state advances in true time
    # order, not one symbol fully processed before the next.
    events = []
    for symbol, df_ltf in ltf_data.items():
        for idx in range(WARMUP_BARS, len(df_ltf)):
            events.append((df_ltf.iloc[idx]["time"], symbol, idx))
    events.sort(key=lambda e: e[0])

    next_available_idx = {symbol: WARMUP_BARS for symbol in ltf_data}
    last_trade_time = {}
    cooldown = timedelta(minutes=config.SYMBOL_COOLDOWN_MINUTES)
    trade_log = []
    blocked_events = 0

    for current_time, symbol, idx in events:
        if idx < next_available_idx[symbol]:
            continue  # still inside a previous trade's hold window for this symbol

        df_ltf = ltf_data[symbol]
        df_htf = htf_data[symbol]

        allowed, reason = guard.check_trade_allowed(equity, now=current_time.to_pydatetime())
        if not allowed:
            blocked_events += 1
            continue

        last = last_trade_time.get(symbol)
        if last is not None and (current_time - last) < cooldown:
            continue

        ltf_slice = df_ltf.iloc[: idx + 1]

        if strategy_mode == "momentum":
            signal = strategy.evaluate_momentum(symbol, ltf_slice)
        else:
            htf_slice = closed_slice(df_htf, config.HTF_TIMEFRAME, current_time)
            h4_slice = None
            if config.H4_BIAS_FILTER_ENABLED:
                df_h4 = h4_data.get(symbol)
                symbol_h4_tf = config.SYMBOL_H4_TIMEFRAME_OVERRIDE.get(symbol, config.H4_TIMEFRAME)
                h4_slice = closed_slice(df_h4, symbol_h4_tf, current_time) if df_h4 is not None else None
            level_slice = None
            symbol_level_tf = config.SYMBOL_LIQUIDITY_LEVEL_TIMEFRAME_OVERRIDE.get(symbol, liquidity_level_timeframe)
            if symbol_level_tf:
                df_level = level_data.get(symbol)
                level_slice = closed_slice(df_level, symbol_level_tf, current_time) if df_level is not None else None
            signal = strategy.evaluate(symbol, htf_slice, ltf_slice, h4_slice, df_liquidity_tf=level_slice)

        if signal is None:
            continue

        risk_amount = equity * (config.RISK_PERCENT_PER_TRADE / 100.0)
        risk_distance = abs(signal.entry_price - signal.stop_loss)
        if trade_management == "fixed_rr":
            outcome, exit_idx, exit_price = simulate_trade_fixed_rr(
                df_ltf, idx, signal.direction, signal.entry_price, signal.stop_loss,
                rr_multiple, slippage_distance=slippage_distance.get(symbol, 0.0)
            )
        else:
            outcome, exit_idx, exit_price = simulate_trade_with_trailing(
                df_ltf, idx, signal.direction, signal.entry_price, signal.stop_loss,
                signal.take_profit, ltf_atr_data[symbol], symbol=symbol,
                slippage_distance=slippage_distance.get(symbol, 0.0)
            )

        cost = spread_cost.get(symbol, 0.0)
        if signal.direction == "BUY":
            price_move = exit_price - signal.entry_price
        else:
            price_move = signal.entry_price - exit_price
        pnl = risk_amount * (price_move / risk_distance) - cost

        equity += pnl
        guard.mark_trading_day_active()
        last_trade_time[symbol] = current_time
        next_available_idx[symbol] = exit_idx + 1

        trade_log.append({
            "symbol": symbol, "time": current_time, "direction": signal.direction,
            "entry": round(signal.entry_price, 5), "stop_loss": round(signal.stop_loss, 5),
            "take_profit": round(signal.take_profit, 5), "outcome": outcome,
            "pnl": round(pnl, 2), "equity_after": round(equity, 2),
        })

    connector.shutdown()
    print_report(trade_log, blocked_events, days, symbols, slippage_pips)
    write_csv(trade_log)


def print_report(trade_log: list, blocked_events: int, days: int, symbols: list[str],
                  slippage_pips: float = 0.0):
    print("\n" + "=" * 60)
    print(f"BACKTEST REPORT — {days} days — {', '.join(symbols)}")
    print(f"Slippage on stop exits: {slippage_pips:.2f} pips" if slippage_pips
          else "Slippage on stop exits: none (fills assumed exact)")
    print("=" * 60)

    if not trade_log:
        print("No trades were generated. Either the filters are too strict for "
              "this period, or the symbol data didn't load. Check the log above.")
        return

    df = pd.DataFrame(trade_log)
    total = len(df)
    wins = (df["outcome"] == "WIN").sum()
    losses = (df["outcome"] == "LOSS").sum()
    breakevens = (df["outcome"] == "BREAKEVEN").sum()
    timeouts = (df["outcome"] == "TIMEOUT").sum()
    win_rate = wins / total * 100 if total else 0
    total_pnl = df["pnl"].sum()
    final_equity = config.STARTING_BALANCE + total_pnl

    df["date"] = pd.to_datetime(df["time"]).dt.date
    daily_pnl = df.groupby("date")["pnl"].sum()
    worst_day = daily_pnl.min() if not daily_pnl.empty else 0
    best_day = daily_pnl.max() if not daily_pnl.empty else 0

    running_equity = config.STARTING_BALANCE + df["pnl"].cumsum()
    running_peak = running_equity.cummax()
    max_drawdown = (running_peak - running_equity).max()

    # Only used below for .target_reached() -- never calls anything that
    # writes state, but keep it off the live default file regardless.
    guard = FTMOComplianceGuard(starting_balance=config.STARTING_BALANCE,
                                 state_file="backtest_compliance_state.json")

    print(f"Total trades:        {total}  (Wins: {wins}  Losses: {losses}  "
          f"Breakeven: {breakevens}  Timeouts: {timeouts})")
    print(f"Win rate:            {win_rate:.1f}%")
    print(f"Total P&L:           ${total_pnl:+.2f}")
    print(f"Final equity:        ${final_equity:,.2f}  (starting ${config.STARTING_BALANCE:,.2f})")
    print(f"Best single day:     ${best_day:+.2f}")
    print(f"Worst single day:    ${worst_day:+.2f}  (official FTMO daily loss limit: "
          f"${config.STARTING_BALANCE * config.DAILY_LOSS_LIMIT_PCT / 100:.2f})")
    print(f"Max drawdown seen:   ${max_drawdown:.2f}  (official FTMO max drawdown: "
          f"${config.STARTING_BALANCE * config.MAX_DRAWDOWN_PCT / 100:.2f})")
    print(f"Compliance blocks:   {blocked_events} scan events where the guard paused trading")
    print(f"Profit target ({config.PROFIT_TARGET_PCT:.0f}%): "
          f"{'REACHED' if guard.target_reached(final_equity) else 'not reached'} "
          f"(${config.STARTING_BALANCE * config.PROFIT_TARGET_PCT / 100:.2f} target)")

    if abs(worst_day) >= config.STARTING_BALANCE * config.DAILY_LOSS_LIMIT_PCT / 100:
        print("\nWARNING: At least one simulated day's loss reached or exceeded FTMO's ACTUAL daily "
              "loss limit — remember the live guard trips at 80% of that limit, so this should "
              "not happen live, but it flags this period had a rough day worth reviewing.")

    print("\nFull trade log written to backtest_results.csv")
    print("=" * 60 + "\n")


def write_csv(trade_log: list):
    if not trade_log:
        return
    with open("backtest_results.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(trade_log[0].keys()))
        writer.writeheader()
        writer.writerows(trade_log)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the FTMO scalping bot against MT5 historical data.")
    parser.add_argument("--days", type=int, default=180, help="How many days of history to test (default: 180)")
    parser.add_argument("--symbols", type=str, default=None,
                         help="Comma-separated symbol override, e.g. XAUUSD,EURUSD (default: config.SYMBOLS)")
    parser.add_argument("--slippage-pips", type=float, default=0.0,
                         help="Extra adverse slippage (in pips) applied to stop exits only "
                              "(initial stop, breakeven, trailed stop) — not take-profit/hard-cap "
                              "hits or timeouts. Default: 0 (no slippage).")
    args = parser.parse_args()

    symbol_list = args.symbols.split(",") if args.symbols else config.SYMBOLS
    run_backtest(days=args.days, symbols=[s.strip() for s in symbol_list],
                 slippage_pips=args.slippage_pips)
