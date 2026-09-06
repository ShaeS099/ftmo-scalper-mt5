"""
OrderExecutor: the only module allowed to call mt5.order_send(). Enforces
the spread filter, per-symbol cooldown, and open-position guard (no
stacking a second position on a symbol that already has one live) before
every order, manages breakeven-then-trail stop adjustments on open
positions, and returns normalized results so notifier.py can report
everything to Telegram.
"""
import logging
from datetime import datetime, timedelta

import MetaTrader5 as mt5

import config
import indicators
from risk import calculate_scalp_lot_size

logger = logging.getLogger("executor")


class OrderExecutor:
    def __init__(self, connector, cooldown_minutes: int = config.SYMBOL_COOLDOWN_MINUTES):
        self.connector = connector
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self._last_trade_time: dict[str, datetime] = {}
        # Tracks metadata mt5 positions don't expose on their own (original
        # entry price and risk distance), keyed by ticket, so trailing
        # management knows how many R a position is currently ahead by.
        self._position_meta: dict[int, dict] = {}

    def _in_cooldown(self, symbol: str) -> bool:
        last = self._last_trade_time.get(symbol)
        return last is not None and (datetime.utcnow() - last) < self.cooldown

    def _has_open_position(self, symbol: str) -> bool:
        return len(self.connector.get_open_positions(symbol)) > 0

    def _spread_ok(self, symbol: str) -> bool:
        spread = self.connector.get_current_spread_points(symbol)
        if spread is None:
            logger.warning("Could not read spread for %s, blocking trade to be safe", symbol)
            return False
        max_spread = config.SYMBOL_MAX_SPREAD_POINTS.get(symbol, config.DEFAULT_MAX_SPREAD_POINTS)
        if spread > max_spread:
            logger.info("Spread too wide on %s: %.1f > %.1f points", symbol, spread, max_spread)
            return False
        return True

    def place_order(self, signal, account_balance: float) -> dict:
        symbol = signal.symbol

        if self._in_cooldown(symbol):
            return {"success": False, "reason": f"{symbol} is in cooldown, skipping duplicate signal"}

        if self._has_open_position(symbol):
            return {"success": False, "reason": f"{symbol} already has an open position, skipping to avoid stacking risk"}

        if not self._spread_ok(symbol):
            return {"success": False, "reason": f"Spread filter blocked {symbol}"}

        order_type = mt5.ORDER_TYPE_BUY if signal.direction == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"success": False, "reason": f"No tick data for {symbol}"}

        price = tick.ask if signal.direction == "BUY" else tick.bid

        # Sized against the LIVE fill price, not signal.entry_price. The signal's
        # entry price is a snapshot from the candle data scanner.py evaluated,
        # which can be a poll cycle (up to ~30s) stale by the time the order
        # actually reaches the market. Since this strategy enters right after a
        # liquidity-sweep reclaim, price commonly keeps running in the trade's
        # favor in that window, which widens the REAL distance from fill to
        # signal.stop_loss beyond what sizing against the stale price would
        # assume — sizing here instead of in main.py keeps position risk tied to
        # the stop distance that's actually going to be live.
        symbol_info = self.connector.get_symbol_info(symbol)
        lot_size = calculate_scalp_lot_size(symbol, signal.direction, symbol_info, account_balance,
                                             price, signal.stop_loss)
        if lot_size <= 0:
            return {"success": False, "reason": "Lot size calculated as 0"}

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot_size,
            "type": order_type,
            "price": price,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "deviation": 10,
            "magic": 990001,
            "comment": "ftmo-scalper-liquidity-sweep",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            reason = f"order_send failed: {getattr(result, 'retcode', 'no result')} {mt5.last_error()}"
            logger.error(reason)
            return {"success": False, "reason": reason}

        self._last_trade_time[symbol] = datetime.utcnow()
        # result.price is the ACTUAL fill, which can differ from the
        # pre-order tick snapshot (`price`) used for sizing/the request —
        # same live-tick-vs-fill gap noted in calculate_scalp_lot_size's
        # docstring. Bookkeeping (and the returned price) should reflect
        # what actually happened, not what was requested.
        fill_price = result.price if result.price else price
        logger.info("Order placed: %s %s %.2f lots @ %.5f (requested %.5f)",
                    symbol, signal.direction, lot_size, fill_price, price)

        # Correct TP for the real fill, keeping SL untouched. signal.take_profit
        # is a fixed price computed from the strategy's stale reference price
        # (the sweep candle's close) — when the real fill lands meaningfully
        # away from that (the same live-tick-vs-fill gap, common right after a
        # sweep reclaim since price is actively moving), the realized reward
        # window silently compresses or extends. Found live 2026-08-21: a
        # trade whose fill was 19pts better than the signal assumed opened
        # almost on top of its own fixed TP, netting ~0 gross P&L on what
        # should have been a full 1.5R winner. SL is deliberately left alone —
        # it's anchored to a real technical level (the swept wick), not an
        # arbitrary distance, so it shouldn't move just because the fill did;
        # the existing sizing safety margin already accounts for a wider
        # realized risk distance. TP has no such structural meaning — it's
        # purely "entry + R-multiple x risk", so recomputing it against the
        # REAL entry preserves the intended R-multiple instead of pinning
        # reward to a price level from before the position even opened.
        take_profit = signal.take_profit
        original_risk = abs(signal.entry_price - signal.stop_loss)
        # symbol_info can be None here (get_symbol_info logs its own error and
        # returns None on failure) -- calculate_scalp_lot_size already
        # tolerates that by falling back to a 0.01 lot, so the order above can
        # still have gone through successfully even without it. Re-using
        # symbol_info.point below without this guard would raise
        # AttributeError AFTER the position is already open, aborting
        # place_order() before it returns -- the caller never gets a result
        # back and notify_trade_opened() never fires for a trade that's
        # genuinely live. Skipping the correction (not the whole trade) when
        # symbol_info is missing keeps that failure mode from reaching here.
        if symbol_info is not None and original_risk > 0:
            rr_multiple = abs(signal.take_profit - signal.entry_price) / original_risk
            realized_risk = abs(fill_price - signal.stop_loss)
            corrected_tp = (fill_price + rr_multiple * realized_risk if signal.direction == "BUY"
                             else fill_price - rr_multiple * realized_risk)
            if abs(corrected_tp - signal.take_profit) > symbol_info.point:
                modify_request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": result.order,
                    "symbol": symbol,
                    "sl": signal.stop_loss,
                    "tp": corrected_tp,
                }
                modify_result = mt5.order_send(modify_request)
                if modify_result is None or modify_result.retcode != mt5.TRADE_RETCODE_DONE:
                    logger.error("Failed to correct TP for ticket %s: %s (keeping original %.5f)",
                                 result.order, mt5.last_error(), signal.take_profit)
                else:
                    logger.info("TP corrected for actual fill: %.5f -> %.5f", signal.take_profit, corrected_tp)
                    take_profit = corrected_tp

        risk_distance = abs(fill_price - signal.stop_loss)
        self._position_meta[result.order] = {
            "symbol": symbol,
            "direction": signal.direction,
            "entry_price": fill_price,
            "risk_distance": risk_distance,
            "high_water": fill_price,
            "breakeven_done": False,
        }

        return {"success": True, "price": fill_price, "lots": lot_size, "ticket": result.order,
                "take_profit": take_profit}

    def _modify_stop(self, ticket: int, symbol: str, new_sl: float, current_tp: float) -> bool:
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": symbol,
            "sl": new_sl,
            "tp": current_tp,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("Failed to modify stop for ticket %s: %s", ticket, mt5.last_error())
            return False
        return True

    def _detect_closures(self, open_tickets: dict) -> list[dict]:
        """Anything tracked in _position_meta that's no longer in open_tickets
        has closed (hit its SL/TP bracket, or was manually closed) since the
        last check — stop tracking it and emit a 'closed' event."""
        events = []
        for ticket in list(self._position_meta.keys()):
            if ticket not in open_tickets:
                meta = self._position_meta.pop(ticket)
                events.append({"type": "closed", "ticket": ticket, "symbol": meta["symbol"]})
        return events

    def check_closed_positions(self) -> list[dict]:
        """
        Lightweight, closure-detection-only check — safe to call every
        polling cycle regardless of config.FIXED_RR_ENABLED, since it never
        touches a still-open position's stop. This is what a FIXED_RR_ENABLED
        bot should call instead of manage_trailing_stops(): the SL+TP
        bracket does all the actual position management, this just notices
        when that bracket has closed the trade.

        Returns a list of {'type': 'closed', ...} event dicts.
        """
        open_positions = self.connector.get_open_positions()
        open_tickets = {pos.ticket: pos for pos in open_positions}
        return self._detect_closures(open_tickets)

    def manage_trailing_stops(self) -> list[dict]:
        """
        Call once per polling cycle INSTEAD OF check_closed_positions() (not
        in addition to — this does its own closure detection too). For every
        tracked open position: moves the stop to breakeven once price is
        ahead by config.BREAKEVEN_TRIGGER_R, then trails it behind price by
        config.TRAIL_ATR_MULTIPLE x the live 5M ATR once ahead by
        config.TRAIL_TRIGGER_R. The stop only ever moves in the position's
        favor, never back toward the entry. Only meaningful when
        config.FIXED_RR_ENABLED is False — see check_closed_positions().

        Returns a list of event dicts for notifier.py to report.
        """
        open_positions = self.connector.get_open_positions()
        open_tickets = {pos.ticket: pos for pos in open_positions}
        events = self._detect_closures(open_tickets)

        for ticket, pos in open_tickets.items():
            meta = self._position_meta.get(ticket)
            if meta is None:
                continue  # position not opened by this bot instance (or restarted) — leave it alone

            symbol = meta["symbol"]
            direction = meta["direction"]
            entry_price = meta["entry_price"]
            risk = meta["risk_distance"]
            if risk <= 0:
                continue

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue
            current_price = tick.bid if direction == "BUY" else tick.ask

            if direction == "BUY":
                meta["high_water"] = max(meta["high_water"], current_price)
                profit_r = (meta["high_water"] - entry_price) / risk
            else:
                meta["high_water"] = min(meta["high_water"], current_price)
                profit_r = (entry_price - meta["high_water"]) / risk

            new_stop = pos.sl

            if not meta["breakeven_done"] and profit_r >= config.BREAKEVEN_TRIGGER_R:
                candidate = entry_price
                if direction == "BUY" and candidate > pos.sl:
                    new_stop = candidate
                elif direction == "SELL" and (pos.sl == 0 or candidate < pos.sl):
                    new_stop = candidate
                meta["breakeven_done"] = True

            if profit_r >= config.TRAIL_TRIGGER_R:
                df_ltf = self.connector.get_candles(symbol, config.LTF_TIMEFRAME, num_candles=config.LTF_ATR_LOOKBACK + 5)
                if df_ltf is not None:
                    atr_series = indicators.atr(df_ltf["high"], df_ltf["low"], df_ltf["close"],
                                                 length=config.LTF_ATR_LOOKBACK)
                    current_atr = atr_series.iloc[-1] if not atr_series.empty else None
                    if current_atr and current_atr > 0:
                        trail_multiple = config.SYMBOL_TRAIL_ATR_MULTIPLE.get(symbol, config.TRAIL_ATR_MULTIPLE)
                        if direction == "BUY":
                            trail_candidate = meta["high_water"] - trail_multiple * current_atr
                            if trail_candidate > new_stop:
                                new_stop = trail_candidate
                        else:
                            trail_candidate = meta["high_water"] + trail_multiple * current_atr
                            if new_stop == 0 or trail_candidate < new_stop:
                                new_stop = trail_candidate

            if new_stop != pos.sl and new_stop != 0:
                if self._modify_stop(ticket, symbol, new_stop, pos.tp):
                    logger.info("Trailing stop updated: %s ticket=%s new_sl=%.5f (profit_r=%.2f)",
                                symbol, ticket, new_stop, profit_r)
                    events.append({
                        "type": "breakeven" if new_stop == entry_price else "trail",
                        "symbol": symbol, "ticket": ticket, "new_stop": new_stop, "profit_r": profit_r,
                    })

        return events

    def flatten_all_positions(self, reason: str = "manual/scheduled flatten"):
        positions = self.connector.get_open_positions()
        closed = []
        for pos in positions:
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                continue
            close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": pos.ticket,
                "price": price,
                "deviation": 10,
                "magic": 990001,
                "comment": f"flatten: {reason}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                closed.append(pos.symbol)
                self._position_meta.pop(pos.ticket, None)
            else:
                logger.error("Failed to flatten %s: %s", pos.symbol, mt5.last_error())
        return closed
