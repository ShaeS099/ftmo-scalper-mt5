"""
Entry point. Wires together data ingestion, strategy, compliance, risk
sizing, execution, and Telegram notifications into one polling loop.

Run with: python main.py
"""
import logging
import os
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

import config
import notifier
from compliance import FTMOComplianceGuard
from executor import OrderExecutor
from mt5_connector import MT5Connector
from scanner import StrategyEngine

# Added 2026-08-31: console-only logging meant every "why didn't it trade"
# question after the fact had to be reconstructed via a backtest replay,
# which can only approximate live-only conditions (real-time spread ticks,
# exact poll timing) rather than show what actually happened. This writes
# the exact same log lines to a dated file too, so a real live decision
# trail exists to check directly next time. UTC-dated filename for
# consistency with how this whole project reasons about time elsewhere
# (BROKER_UTC_OFFSET_HOURS conversions, trading window bounds, etc.).
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_log_file = os.path.join(LOG_DIR, f"bot_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(_log_file, encoding="utf-8")],
)
logger = logging.getLogger("main")
logger.info("Logging to console and %s", _log_file)

POLL_INTERVAL_SECONDS = 30

# Simple in-memory kill switch. Flip to True (or wire up a Telegram command
# handler that sets this) to immediately stop new order placement without
# killing the process.
KILL_SWITCH_ENGAGED = False

# Last closed LTF candle timestamp already evaluated per symbol. Polling
# every 30s would otherwise re-run evaluate() several times against the same
# closed bar before the next one forms — harmless for the open-position guard,
# but it means live isn't evaluating once-per-bar the way backtest.py's replay
# does. Gating on this makes the two match bar-for-bar.
_last_evaluated_bar = {}


def run_scan_cycle(connector: MT5Connector, strategy: StrategyEngine, guard: FTMOComplianceGuard,
                    executor: OrderExecutor):
    try:
        _run_scan_cycle(connector, strategy, guard, executor)
    except Exception:
        logger.exception("Error during scan cycle")


def _run_scan_cycle(connector: MT5Connector, strategy: StrategyEngine, guard: FTMOComplianceGuard,
                     executor: OrderExecutor):
    if KILL_SWITCH_ENGAGED:
        return

    account = connector.get_account_info()
    if account is None:
        logger.error("Could not read account info this cycle, skipping")
        return

    if config.COMPLIANCE_GUARD_ENABLED:
        allowed, reason = guard.check_trade_allowed(account.equity)
        if not allowed:
            logger.warning("Compliance guard blocking all trades: %s", reason)
            notifier.notify_compliance_block(reason)
            return
    else:
        logger.debug("Compliance guard disabled (config.COMPLIANCE_GUARD_ENABLED=False) — skipping check")

    guard.mark_trading_day_active()

    # FIXED_RR_ENABLED trades are a static SL+TP bracket placed at entry —
    # the broker closes them, nothing here should touch the stop afterward,
    # so use the closure-only check instead of manage_trailing_stops()
    # (breakeven + ATR trail), which would fight the bracket by moving
    # stops it shouldn't. manage_trailing_stops() is kept in executor.py as
    # a live fallback for if FIXED_RR_ENABLED is ever turned back off.
    if config.FIXED_RR_ENABLED:
        for event in executor.check_closed_positions():
            notifier.notify_trade_closed_ticket(event["symbol"], event["ticket"])
    else:
        trail_events = executor.manage_trailing_stops()
        for event in trail_events:
            if event["type"] == "breakeven":
                notifier.notify_stop_moved(event["symbol"], "breakeven", event["profit_r"])
            elif event["type"] == "trail":
                notifier.notify_stop_moved(event["symbol"], "trailing", event["profit_r"])
            elif event["type"] == "closed":
                notifier.notify_trade_closed_ticket(event["symbol"], event["ticket"])

    # LTF fetch must cover SPREAD_LOOKBACK_BARS (1440 = ~24h of M1) when the
    # spread guard is on, or detect_liquidity_sweep() silently rejects EVERY
    # signal forever (found live 2026-08-18: a confirmed-valid confluence
    # setup produced "no signal" all morning because the guard's rolling
    # window could never fill with only 100 candles — not a real spread
    # rejection, just missing data). +60 buffer for the sweep's own lookback.
    ltf_candles_needed = 100
    if config.SPREAD_PROTECTION_ENABLED:
        ltf_candles_needed = max(ltf_candles_needed, config.SPREAD_LOOKBACK_BARS + 60)

    for symbol in config.SYMBOLS:
        # Per-symbol overrides (SYMBOL_H4_TIMEFRAME_OVERRIDE /
        # SYMBOL_LIQUIDITY_LEVEL_TIMEFRAME_OVERRIDE) let one symbol in the
        # basket run different rules than the rest -- e.g. the 2026-08-26
        # XAUUSD trial (H1 bias, M15 liquidity level) alongside USDCAD
        # trading normal rules (H4 bias, M1's own liquidity level) in the
        # same basket. A symbol not listed in either override dict falls
        # back to the global default, unchanged from before these existed.
        h4_timeframe = config.SYMBOL_H4_TIMEFRAME_OVERRIDE.get(symbol, config.H4_TIMEFRAME)
        liquidity_level_timeframe = config.SYMBOL_LIQUIDITY_LEVEL_TIMEFRAME_OVERRIDE.get(symbol)

        df_htf = connector.get_candles(symbol, config.HTF_TIMEFRAME, num_candles=100)
        df_ltf = connector.get_candles(symbol, config.LTF_TIMEFRAME, num_candles=ltf_candles_needed)
        df_h4 = connector.get_candles(symbol, h4_timeframe, num_candles=50) \
            if config.H4_BIAS_FILTER_ENABLED else None
        df_liquidity_tf = connector.get_candles(symbol, liquidity_level_timeframe,
                                                  num_candles=config.LIQUIDITY_LEVEL_LOOKBACK + 10) \
            if liquidity_level_timeframe else None

        if df_ltf is None or df_ltf.empty:
            continue

        latest_closed_bar = df_ltf["time"].iloc[-1]
        if _last_evaluated_bar.get(symbol) == latest_closed_bar:
            continue  # already evaluated this closed candle; wait for the next one to close
        _last_evaluated_bar[symbol] = latest_closed_bar

        signal = strategy.evaluate(symbol, df_htf, df_ltf, df_h4, df_liquidity_tf=df_liquidity_tf)
        if signal is None:
            continue

        result = executor.place_order(signal, account.balance)
        if result["success"]:
            notifier.notify_trade_opened(signal, result["lots"], result)
        else:
            notifier.notify_trade_blocked(signal, result["reason"])


def scheduled_friday_flatten(executor: OrderExecutor):
    now = datetime.now(timezone.utc)
    if now.weekday() == 4 and now.hour == config.FRIDAY_FLATTEN_HOUR_UTC:
        closed = executor.flatten_all_positions(reason="Friday weekend-hold rule")
        notifier.notify_flatten_event(closed, "Friday weekend-hold rule")


def scheduled_daily_summary(guard: FTMOComplianceGuard, connector: MT5Connector):
    account = connector.get_account_info()
    if account is None:
        return
    notifier.notify_daily_summary(guard.status_summary(account.equity))


def main():
    connector = MT5Connector()
    if not connector.connect():
        notifier.notify_bot_status("❌ Failed to connect to MT5. Bot did not start.")
        return

    account = connector.get_account_info()
    # starting_balance is always the TRUE evaluation baseline
    # (config.STARTING_BALANCE), never account.balance — using live balance
    # for THAT would silently redefine the profit target/drawdown cap off
    # whatever the balance happens to be at boot, forgetting any P&L from
    # before this run (see compliance.py's persisted-state loading, which
    # restores everything else across restarts using this same fixed
    # baseline). current_equity IS the live balance — it only seeds
    # day_start_equity on a genuinely fresh start (no state file yet), so
    # "today's loss" isn't computed against the wrong baseline (bug found
    # live 2026-08-19 — see compliance.py).
    guard = FTMOComplianceGuard(
        starting_balance=config.STARTING_BALANCE,
        current_equity=account.balance if account else None,
    )

    strategy = StrategyEngine()
    executor = OrderExecutor(connector)

    status_msg = (
        f"✅ FTMO scalping bot started.\nSymbols: {', '.join(config.SYMBOLS)}\n"
        f"Evaluation starting balance: ${config.STARTING_BALANCE:.2f}"
    )
    if account:
        status_msg += f"\nCurrent account balance: ${account.balance:.2f}"
    notifier.notify_bot_status(status_msg)

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(scheduled_friday_flatten, "interval", minutes=15, args=[executor])
    scheduler.add_job(scheduled_daily_summary, "cron", hour=21, minute=0, args=[guard, connector])
    scheduler.add_job(
        run_scan_cycle, "interval", seconds=POLL_INTERVAL_SECONDS,
        args=[connector, strategy, guard, executor],
        id="scan_cycle", max_instances=1, next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    finally:
        scheduler.shutdown(wait=False)
        connector.shutdown()
        notifier.notify_bot_status("⏹ Bot stopped.")


if __name__ == "__main__":
    main()
