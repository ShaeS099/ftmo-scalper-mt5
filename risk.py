"""
Position sizing only. Account-level pass/fail decisions live in compliance.py —
this module assumes a trade has already been cleared and just answers
"how many lots".
"""
import logging

import MetaTrader5 as mt5

import config

logger = logging.getLogger("risk")


def calculate_scalp_lot_size(symbol: str, direction: str, symbol_info, account_balance: float,
                              entry: float, stop_loss: float,
                              risk_percent: float = config.RISK_PERCENT_PER_TRADE,
                              safety_margin: float = config.LOT_SIZING_SAFETY_MARGIN) -> float:
    """
    safety_margin (>=1.0) pads the stop distance used for SIZING only — the
    real sl sent to the broker is unaffected. Found live 2026-08-19: `entry`
    here is a tick price read moments before order_send(), but the actual
    fill commonly lands further from stop_loss than that snapshot (this
    strategy enters right after a sweep reclaim — a fast-moving instant —
    and price often keeps running in the trade's favor between the tick
    read and the fill). One real trade sized for an ~11pt stop distance and
    filled at ~16pt (a 1.45x overshoot: intended 0.75% risk realized as
    ~1.09%). Padding the sizing-time distance by safety_margin trades a
    SMALLER position for protection against that direction of surprise —
    the position ends up sized for a wider stop than assumed, so if the
    real fill lands further out (the observed failure mode), realized risk
    lands at or under target instead of over it.

    Real $-per-lot risk is derived via mt5.order_calc_profit() rather than
    symbol_info.trade_tick_value. Found live 2026-08-27: a real XAUUSD trade
    on this account was sized to 14.76 lots and risked $2,141.16 (4.27% of
    equity) against an intended 0.75% ($376.16) target — a ~5.7x overshoot.
    Root cause: symbol_info.trade_tick_value reported $0.10/point for
    XAUUSD, but order_calc_profit (MT5's own authoritative calculator)
    confirmed the real value is $0.7363/point — a stale/wrong static field
    for this symbol on this broker, off by 7.36x. GBPUSD/USDJPY/USDCAD's
    trade_tick_value all matched order_calc_profit to within 0.1% in the
    same check, so this isn't a universal problem, but trusting the static
    field with no cross-check let one bad symbol's metadata blow position
    size out by multiples. order_calc_profit asks the broker directly for
    the real answer instead of re-deriving it from metadata that can drift.
    """
    if symbol_info is None:
        logger.warning("No symbol_info available, falling back to minimum lot size")
        return 0.01

    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        logger.warning("Zero/negative stop distance, skipping trade sizing")
        return 0.0

    risk_amount = account_balance * (risk_percent / 100.0)
    lot_step = symbol_info.volume_step or 0.01
    sizing_distance = stop_distance * safety_margin
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    # Price move in the LOSING direction for this order type, at the padded
    # sizing distance — order_calc_profit returns a negative number here;
    # its magnitude is the real $ risk for 1.0 lot.
    sizing_far_price = entry - sizing_distance if direction == "BUY" else entry + sizing_distance
    risk_per_lot = mt5.order_calc_profit(order_type, symbol, 1.0, entry, sizing_far_price)

    if risk_per_lot is None or risk_per_lot == 0:
        logger.warning(
            "order_calc_profit failed for %s (%s), falling back to symbol_info.trade_tick_value "
            "-- this is the exact field that was found unreliable for XAUUSD, so treat a lot size "
            "from this fallback with suspicion and double check it.", symbol, mt5.last_error()
        )
        tick_value = symbol_info.trade_tick_value or 1.0
        stop_distance_points = stop_distance / symbol_info.point
        sizing_distance_points = stop_distance_points * safety_margin
        raw_lots = risk_amount / (sizing_distance_points * tick_value)
    else:
        raw_lots = risk_amount / abs(risk_per_lot)

    lots = round(raw_lots / lot_step) * lot_step
    lots = max(symbol_info.volume_min, min(lots, symbol_info.volume_max))
    logger.info(
        "Sized position: risk_amount=$%.2f stop_distance=%.5f (sized against %.5f with %.2fx margin) -> %.2f lots",
        risk_amount, stop_distance, sizing_distance, safety_margin, lots
    )
    return lots
