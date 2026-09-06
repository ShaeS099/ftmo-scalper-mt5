"""
StrategyEngine: combines a higher-timeframe EMA trend filter with a
lower-timeframe liquidity-sweep entry trigger. Only fires a signal when
both agree AND the market is trending with adequate volatility, which is
what keeps the false-positive rate manageable in live conditions.
"""
import logging
from dataclasses import dataclass

import pandas as pd

import config
import indicators

logger = logging.getLogger("scanner")


@dataclass
class Signal:
    symbol: str
    direction: str          # "BUY" or "SELL"
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence_score: float


class StrategyEngine:
    def __init__(self, lookback: int = config.LIQUIDITY_SWEEP_LOOKBACK):
        self.lookback = lookback

    @staticmethod
    def _hour_in_window(utc_hour: float, start: float, end: float) -> bool:
        if start < end:
            return start <= utc_hour < end
        return utc_hour >= start or utc_hour < end  # window wraps past midnight

    def in_trading_window(self, ts) -> bool:
        """
        True if the given candle timestamp falls inside an allowed trading
        window: the London open (TRADING_ALLOWED_*_HOUR_UTC), plus the
        London/NY overlap (NY_OVERLAP_*_HOUR_UTC) if NY_OVERLAP_ENABLED —
        see config.py. The Asian session and the NY-only session (after
        London closes) remain excluded either way. ts is expected to be a
        broker-server-clock timestamp (naive or tz-aware, doesn't matter —
        only its wall-clock hour/minute are read), so it's converted to true
        UTC via BROKER_UTC_OFFSET_HOURS before comparing.

        Window bounds are fractional hours (e.g. 10.5 = 10:30), not whole
        hours only — added 2026-08-28 so TRADING_ALLOWED_END_HOUR_UTC could
        express a 10:30 cutoff. utc_hour below includes the timestamp's own
        minutes for the same reason; comparing against a whole-hour-only
        clock would round away exactly the precision this exists for.
        """
        utc_hour = ((ts.hour + ts.minute / 60.0) - config.BROKER_UTC_OFFSET_HOURS) % 24
        if self._hour_in_window(utc_hour, config.TRADING_ALLOWED_START_HOUR_UTC,
                                 config.TRADING_ALLOWED_END_HOUR_UTC):
            return True
        if config.NY_OVERLAP_ENABLED and self._hour_in_window(
                utc_hour, config.NY_OVERLAP_START_HOUR_UTC, config.NY_OVERLAP_END_HOUR_UTC):
            return True
        return False

    def get_trend_bias(self, df_htf: pd.DataFrame) -> str | None:
        if df_htf is None or len(df_htf) < 25:
            return None
        ema_fast = df_htf["close"].ewm(span=9, adjust=False).mean()
        ema_slow = df_htf["close"].ewm(span=21, adjust=False).mean()
        return "UP" if ema_fast.iloc[-1] > ema_slow.iloc[-1] else "DOWN"

    def get_h4_bias(self, df_h4: pd.DataFrame) -> str | None:
        """Same EMA9/21 read as get_trend_bias, on the H4 timeframe. Kept as
        a separate method (not a shared helper) since the two are allowed to
        diverge independently — see H4_BIAS_FILTER_ENABLED in config.py."""
        if df_h4 is None or len(df_h4) < config.H4_BIAS_MIN_BARS:
            return None
        ema_fast = df_h4["close"].ewm(span=9, adjust=False).mean()
        ema_slow = df_h4["close"].ewm(span=21, adjust=False).mean()
        return "UP" if ema_fast.iloc[-1] > ema_slow.iloc[-1] else "DOWN"

    def structure_intact(self, df_htf: pd.DataFrame, bias: str,
                          lookback: int = None) -> bool:
        """
        Trend/reversal detector #1: market structure, not a lagging average.
        Splits the last 2*lookback HTF candles into two adjacent windows and
        compares their swing extremes. UP bias requires the recent window's
        swing low to be >= the prior window's swing low (no lower low yet --
        structure still supports an uptrend); DOWN bias requires the mirror
        image (no higher high yet). Returns True (structure intact) when
        there isn't enough data to judge -- absence of evidence shouldn't
        veto a trade the other gates already cleared.

        Added 2026-08-28 after diagnosing two real XAUUSD loss clusters (and
        an earlier GBPUSD one) where EMA9/21 bias and the ADX-decline
        exhaustion filter both stayed on the wrong side of a trend that was
        already reversing -- ADX declining as an old trend dies looks
        identical to ADX declining because a fresh counter-move is losing
        steam, and EMA9/21 lags a real flip by design. In both XAUUSD
        clusters, price had already made a lower low (bias=UP) or higher
        high (bias=DOWN) *before* the bad entries -- this reads that
        directly instead of waiting on a smoothed average to catch up.
        """
        # lookback default reads config at CALL time, not def time -- a
        # `= config.STRUCTURE_LOOKBACK` default is bound once when this
        # module is first imported and silently ignores every later change
        # to config.STRUCTURE_LOOKBACK (found 2026-08-30: a whole tuning
        # sweep of 5 different lookback values came back bit-for-bit
        # identical because of this exact bug).
        if lookback is None:
            lookback = config.STRUCTURE_LOOKBACK
        if df_htf is None or len(df_htf) < lookback * 2:
            return True
        recent = df_htf.iloc[-lookback:]
        prior = df_htf.iloc[-2 * lookback:-lookback]
        if bias == "UP":
            return recent["low"].min() >= prior["low"].min()
        return recent["high"].max() <= prior["high"].max()

    def momentum_diverging_against_bias(self, df_htf: pd.DataFrame, bias: str,
                                         lookback: int = None) -> bool:
        """
        Trend/reversal detector #2: RSI divergence. Compares the same two
        adjacent HTF windows as structure_intact(), but on RSI vs price
        instead of price alone. Bearish divergence (price makes a HIGHER
        high while RSI makes a LOWER high) warns an uptrend is losing real
        momentum even while still making fresh extremes -- vetoes a BUY.
        Bullish divergence (price lower low, RSI higher low) vetoes a SELL.
        Returns False (no divergence warning) when there isn't enough data.

        This is deliberately different information from the ADX-decline
        exhaustion filter: ADX measures trend STRENGTH only, so a strong
        trend that's just started cracking still reads as "still trending,
        barely declining" -- exactly the failure mode found in the Aug 5
        XAUUSD cluster (ADX 52->40 across three losing BUYs while price
        kept making lower highs). Divergence instead compares price action
        to momentum directly, which is what actually caught that kind of
        setup historically.
        """
        if lookback is None:
            lookback = config.STRUCTURE_LOOKBACK
        if df_htf is None or len(df_htf) < lookback * 2:
            return False
        rsi_series = indicators.rsi(df_htf["close"], length=config.RSI_LOOKBACK)
        if rsi_series is None or rsi_series.isna().all():
            return False
        recent_price = df_htf.iloc[-lookback:]
        prior_price = df_htf.iloc[-2 * lookback:-lookback]
        recent_rsi = rsi_series.iloc[-lookback:]
        prior_rsi = rsi_series.iloc[-2 * lookback:-lookback]
        if bias == "UP":
            return recent_price["high"].max() > prior_price["high"].max() and \
                recent_rsi.max() < prior_rsi.max()
        return recent_price["low"].min() < prior_price["low"].min() and \
            recent_rsi.min() > prior_rsi.min()

    def has_recent_gap(self, df_ltf: pd.DataFrame) -> bool:
        """
        Checks whether a large time gap (e.g. a weekend close/open) falls
        inside the lookback window used to compute the swing high/low.
        Such a gap can make the 'recent range' span pre-gap and post-gap
        prices, producing a stop distance that has nothing to do with
        actual market noise.
        """
        if df_ltf is None or "time" not in df_ltf.columns:
            return False  # can't check without timestamps; caller decides how to handle

        window = df_ltf["time"].iloc[-(self.lookback + 1):]
        if len(window) < 2:
            return False

        gaps = window.diff().dropna()
        if gaps.empty:
            return False

        return gaps.max() > pd.Timedelta(hours=config.GAP_SKIP_HOURS)

    def detect_liquidity_sweep(self, df_ltf: pd.DataFrame, bias: str,
                                df_level_tf: pd.DataFrame = None) -> dict | None:
        """
        Sweep candle: wicks through a recent swing high/low and closes back
        inside the range (the stop-hunt). If REQUIRE_CONFIRMATION_CANDLE is
        on, entry only triggers once the NEXT candle also closes further in
        the trade's favor than the sweep candle did BY AT LEAST
        CONFIRMATION_MIN_PROGRESS_ATR_MULTIPLE x ATR — not just any amount.
        Marginal reclaims that barely continue before reversing were a
        common loser pattern; requiring real follow-through filters those
        out rather than accepting any positive tick as confirmation.

        take_profit is a wide safety-net cap (HARD_CAP_R_MULTIPLE x risk),
        not the real exit target — the real exit is a breakeven-then-trail
        managed live by the executor (and simulated candle-by-candle in
        backtest.py), so winning trades aren't artificially capped at a
        fixed reward:risk multiple.

        df_level_tf (experimental, off by default): when given, the swept
        swing high/low is computed from THIS dataframe's last
        LIQUIDITY_LEVEL_LOOKBACK closed candles instead of df_ltf's — e.g.
        pass M15 data to sweep recent session structure while still
        detecting the wick-and-reclaim candle itself on df_ltf. Decoupled
        from `lookback` (df_ltf's own window) on purpose: reusing the same
        bar COUNT across timeframes doesn't preserve the same time SPAN —
        20 bars is 20 minutes on M1 but 5 hours on M15, and a single M1
        wick essentially never breaches a 5-hour extreme (empirically
        verified: 0 breaches in a 3,000-bar sample). LIQUIDITY_LEVEL_LOOKBACK
        defaults to a bar count sized for whatever timeframe df_level_tf
        actually is. Caller is responsible for pre-slicing df_level_tf to
        "as of now", same convention as df_htf/df_h4 in evaluate().
        """
        needs_confirmation = config.REQUIRE_CONFIRMATION_CANDLE
        extra = 1 if needs_confirmation else 0
        min_len = max(self.lookback + 2, config.LTF_ATR_LOOKBACK + 2) + extra
        if df_ltf is None or len(df_ltf) < min_len:
            return None

        if self.has_recent_gap(df_ltf):
            logger.debug("Skipping sweep detection: large time gap inside the lookback window")
            return None

        sweep_idx = -2 if needs_confirmation else -1
        sweep = df_ltf.iloc[sweep_idx]
        confirm = df_ltf.iloc[-1] if needs_confirmation else None

        if config.VOLUME_CONFIRMATION_ENABLED:
            vol_window = df_ltf.iloc[sweep_idx - config.VOLUME_MA_PERIOD: sweep_idx]
            if len(vol_window) < config.VOLUME_MA_PERIOD:
                return None
            vol_ma = vol_window["tick_volume"].mean()
            if sweep["tick_volume"] <= vol_ma:
                return None

        if config.SPREAD_PROTECTION_ENABLED:
            spread_window = df_ltf.iloc[sweep_idx - config.SPREAD_LOOKBACK_BARS: sweep_idx]
            if len(spread_window) < config.SPREAD_LOOKBACK_BARS:
                return None
            avg_spread = spread_window["spread"].mean()
            if sweep["spread"] > avg_spread * config.SPREAD_PROTECTION_MULTIPLE:
                return None

        if df_level_tf is not None:
            level_lookback = config.LIQUIDITY_LEVEL_LOOKBACK
            if len(df_level_tf) < level_lookback:
                return None
            recent = df_level_tf.iloc[-level_lookback:]
        else:
            window_end = sweep_idx  # exclude the sweep candle itself from the range calc
            recent = df_ltf.iloc[window_end - self.lookback: window_end]
        swing_high = recent["high"].max()
        swing_low = recent["low"].min()

        ltf_atr = indicators.atr(df_ltf["high"], df_ltf["low"], df_ltf["close"], length=config.LTF_ATR_LOOKBACK)
        current_atr = ltf_atr.iloc[-1] if ltf_atr is not None and not ltf_atr.empty else None
        min_stop_distance = (current_atr * config.MIN_STOP_ATR_MULTIPLE) if pd.notna(current_atr) else None
        min_confirmation_progress = (
            current_atr * config.CONFIRMATION_MIN_PROGRESS_ATR_MULTIPLE if pd.notna(current_atr) else 0.0
        )
        # FIXED_RR_ENABLED: real, reachable target (validated backtest result).
        # Otherwise HARD_CAP_R_MULTIPLE: a wide safety-net rarely meant to be hit,
        # since the (retired) trailing system was meant to close trades first.
        tp_r_multiple = config.FIXED_RR_MULTIPLE if config.FIXED_RR_ENABLED else config.HARD_CAP_R_MULTIPLE

        if bias == "UP" and sweep["low"] < swing_low and sweep["close"] > swing_low:
            if needs_confirmation:
                confirmation_progress = confirm["close"] - sweep["close"]
                if confirmation_progress < min_confirmation_progress:
                    return None  # next candle didn't progress far enough — no real confirmation
                entry_price = float(confirm["close"])
            else:
                # No next-candle wait, but still measure how decisively THIS
                # candle already reclaimed past the swept level, so the
                # confidence score's confirmation component reflects real
                # reclaim strength instead of a flat neutral guess.
                confirmation_progress = sweep["close"] - swing_low
                entry_price = float(sweep["close"])

            risk = entry_price - float(sweep["low"])
            if risk <= 0:
                return None
            if min_stop_distance and risk < min_stop_distance:
                risk = min_stop_distance
            return {
                "direction": "BUY",
                "entry": entry_price,
                "stop_loss": entry_price - risk,
                "take_profit": entry_price + tp_r_multiple * risk,
                "sweep_depth_atr": (swing_low - sweep["low"]) / current_atr if pd.notna(current_atr) and current_atr else 0.0,
                "confirmation_progress_atr": (
                    confirmation_progress / current_atr if confirmation_progress is not None and pd.notna(current_atr) and current_atr else None
                ),
            }

        if bias == "DOWN" and sweep["high"] > swing_high and sweep["close"] < swing_high:
            if needs_confirmation:
                confirmation_progress = sweep["close"] - confirm["close"]
                if confirmation_progress < min_confirmation_progress:
                    return None  # next candle didn't progress far enough — no real confirmation
                entry_price = float(confirm["close"])
            else:
                # Same-candle reclaim-strength substitute — see BUY branch above.
                confirmation_progress = swing_high - sweep["close"]
                entry_price = float(sweep["close"])

            risk = float(sweep["high"]) - entry_price
            if risk <= 0:
                return None
            if min_stop_distance and risk < min_stop_distance:
                risk = min_stop_distance
            return {
                "direction": "SELL",
                "entry": entry_price,
                "stop_loss": entry_price + risk,
                "take_profit": entry_price - tp_r_multiple * risk,
                "sweep_depth_atr": (sweep["high"] - swing_high) / current_atr if pd.notna(current_atr) and current_atr else 0.0,
                "confirmation_progress_atr": (
                    confirmation_progress / current_atr if confirmation_progress is not None and pd.notna(current_atr) and current_atr else None
                ),
            }

        return None

    def detect_momentum_breakout(self, df_ltf: pd.DataFrame) -> dict | None:
        """
        Second, independent signal type (see MOMENTUM_STRATEGY_ENABLED in
        config.py): trend-continuation instead of the sweep detector's
        reversal fade. Deliberately does NOT take a bias argument or check
        H4/M5 agreement — direction comes entirely from the move itself, so
        this can fire in either direction regardless of what the sweep
        strategy's bias filter would allow, by design (that's the whole
        point of adding it).

        V2: a pullback-then-resume structure, not a raw breakout chase (see
        config.py's comment on why V1 — enter the instant net move clears
        the ATR threshold — backtested as a clear loser: no exhaustion
        check meant it was buying tops/selling bottoms as often as catching
        real continuations). Requires, in order:
          1. Net move over MOMENTUM_LOOKBACK candles (as of the PULLBACK
             candle, not the entry candle) is between MOMENTUM_MIN_ATR_MULTIPLE
             and MOMENTUM_MAX_ATR_MULTIPLE x ATR — a real move, not yet
             likely exhausted.
          2. ADX (on LTF) clears MOMENTUM_MIN_ADX — genuine trending
             conditions, not chop that happened to net out directional.
          3. The candle just before entry is a COUNTER-move candle (pauses
             against the trend), and the entry candle resumes in the trend
             direction AND breaks past that pullback candle's extreme.
        """
        lookback = config.MOMENTUM_LOOKBACK
        min_len = max(lookback + 2, config.LTF_ATR_LOOKBACK + 2, config.ADX_LOOKBACK + 2)
        if df_ltf is None or len(df_ltf) < min_len:
            return None

        if self.has_recent_gap(df_ltf):
            return None

        current = df_ltf.iloc[-1]
        pullback = df_ltf.iloc[-2]
        past = df_ltf.iloc[-2 - lookback]
        net_move = float(pullback["close"] - past["close"])

        ltf_atr = indicators.atr(df_ltf["high"], df_ltf["low"], df_ltf["close"], length=config.LTF_ATR_LOOKBACK)
        current_atr = ltf_atr.iloc[-1] if ltf_atr is not None and not ltf_atr.empty else None
        if not pd.notna(current_atr) or current_atr <= 0:
            return None

        move_atr = abs(net_move) / current_atr
        if move_atr < config.MOMENTUM_MIN_ATR_MULTIPLE or move_atr > config.MOMENTUM_MAX_ATR_MULTIPLE:
            return None

        adx_series = indicators.adx(df_ltf["high"], df_ltf["low"], df_ltf["close"], length=config.ADX_LOOKBACK)
        current_adx = adx_series.iloc[-1] if adx_series is not None and not adx_series.empty else None
        if not pd.notna(current_adx) or current_adx < config.MOMENTUM_MIN_ADX:
            return None

        entry_price = float(current["close"])
        stop_distance = config.MOMENTUM_STOP_ATR_MULTIPLE * current_atr

        if net_move > 0:
            if not (pullback["close"] < pullback["open"]):
                return None  # no pullback candle to resume from
            if not (current["close"] > current["open"] and current["close"] > pullback["high"]):
                return None  # entry candle didn't resume + break the pullback's extreme
            direction = "BUY"
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + config.MOMENTUM_RR_MULTIPLE * stop_distance
        else:
            if not (pullback["close"] > pullback["open"]):
                return None
            if not (current["close"] < current["open"] and current["close"] < pullback["low"]):
                return None
            direction = "SELL"
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - config.MOMENTUM_RR_MULTIPLE * stop_distance

        return {
            "direction": direction,
            "entry": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "net_move_atr": move_atr,
        }

    def passes_volatility_and_trend_filter(self, df_htf: pd.DataFrame) -> tuple[bool, str]:
        """
        Blocks trading in two live-market failure modes:
        - Dead/low-volatility sessions (current ATR well below its own recent average)
        - Choppy/ranging conditions (ADX below the trending threshold)
        Both are computed on the higher timeframe, since that's the timeframe
        the trend bias itself comes from.
        """
        min_bars = max(config.ATR_LOOKBACK, config.ADX_LOOKBACK) + config.ATR_AVG_LOOKBACK
        if df_htf is None or len(df_htf) < min_bars:
            return False, "Not enough candle history for volatility/trend filter"

        atr = indicators.atr(df_htf["high"], df_htf["low"], df_htf["close"], length=config.ATR_LOOKBACK)
        if atr is None or atr.isna().all():
            return False, "ATR could not be computed"

        current_atr = atr.iloc[-1]
        avg_atr = atr.iloc[-config.ATR_AVG_LOOKBACK:].mean()
        if pd.isna(current_atr) or pd.isna(avg_atr) or avg_atr == 0:
            return False, "ATR data incomplete"

        if current_atr < avg_atr * config.MIN_ATR_RATIO:
            return False, (f"Volatility too low: ATR {current_atr:.5f} is below "
                            f"{config.MIN_ATR_RATIO:.0%} of its {config.ATR_AVG_LOOKBACK}-bar average "
                            f"({avg_atr:.5f})")

        adx_series = indicators.adx(df_htf["high"], df_htf["low"], df_htf["close"], length=config.ADX_LOOKBACK)
        if adx_series is None or adx_series.isna().all():
            return False, "ADX could not be computed"

        current_adx = adx_series.iloc[-1]
        if pd.isna(current_adx):
            return False, "ADX data incomplete"

        if current_adx < config.MIN_ADX:
            return False, f"Market is ranging/choppy: ADX {current_adx:.1f} is below the {config.MIN_ADX} threshold"

        return True, "OK"

    def get_htf_trend_strength(self, df_htf: pd.DataFrame) -> float | None:
        """Raw ADX value on the higher timeframe, used as the trend-strength
        component of confidence_score. Returns None if it can't be computed."""
        adx_series = indicators.adx(df_htf["high"], df_htf["low"], df_htf["close"], length=config.ADX_LOOKBACK)
        if adx_series is None or adx_series.empty or pd.isna(adx_series.iloc[-1]):
            return None
        return float(adx_series.iloc[-1])

    def _compute_confidence_score(self, setup: dict, current_adx: float | None) -> float:
        """
        Composite of three 0-1 components, averaged:
          - trend strength: ADX above the MIN_ADX floor, scaled up to ADX 40
          - sweep depth: how far price wicked past the swing level, in ATR units
          - confirmation strength: how far past the minimum required progress
            the confirmation candle went, or (when REQUIRE_CONFIRMATION_CANDLE
            is off) how decisively the sweep candle itself already reclaimed
            past the swept level in the same bar
        """
        trend_component = 0.5
        if current_adx is not None:
            trend_component = min(1.0, max(0.0, (current_adx - config.MIN_ADX) / 20.0))

        depth_component = min(1.0, max(0.0, setup.get("sweep_depth_atr", 0.0)))

        progress = setup.get("confirmation_progress_atr")
        if progress is None:
            confirm_component = 0.5
        else:
            confirm_component = min(1.0, max(0.0, progress / (config.CONFIRMATION_MIN_PROGRESS_ATR_MULTIPLE * 3)))

        return round((trend_component + depth_component + confirm_component) / 3, 2)

    def evaluate(self, symbol: str, df_htf: pd.DataFrame, df_ltf: pd.DataFrame,
                 df_h4: pd.DataFrame = None, df_liquidity_tf: pd.DataFrame = None) -> Signal | None:
        if df_ltf is not None and len(df_ltf) and not self.in_trading_window(df_ltf["time"].iloc[-1]):
            logger.info("%s: no signal (outside the allowed London-open trading window)", symbol)
            return None

        bias = self.get_trend_bias(df_htf)
        if bias is None:
            logger.info("%s: no signal (not enough HTF candle history for trend bias)", symbol)
            return None

        if config.H4_BIAS_FILTER_ENABLED:
            h4_bias = self.get_h4_bias(df_h4)
            if h4_bias is None:
                logger.info("%s: no signal (not enough H4 candle history for H4 bias)", symbol)
                return None
            if h4_bias != bias:
                logger.info("%s: no signal (H4 bias %s disagrees with M5 bias %s)", symbol, h4_bias, bias)
                return None

        if config.EXHAUSTION_FILTER_ENABLED:
            lookback = config.SYMBOL_EXHAUSTION_LOOKBACK_OVERRIDE.get(symbol, config.EXHAUSTION_ADX_LOOKBACK_BARS)
            adx_series = indicators.adx(df_htf["high"], df_htf["low"], df_htf["close"], length=config.ADX_LOOKBACK)
            if adx_series is None or len(adx_series) < lookback + 1:
                logger.info("%s: no signal (not enough HTF history for exhaustion check)", symbol)
                return None
            current_adx, past_adx = adx_series.iloc[-1], adx_series.iloc[-1 - lookback]
            if pd.isna(current_adx) or pd.isna(past_adx):
                logger.info("%s: no signal (ADX incomplete for exhaustion check)", symbol)
                return None
            if current_adx >= past_adx:
                logger.info("%s: no signal (trend not exhausted: ADX %.1f >= %.1f from %d bars ago)",
                            symbol, current_adx, past_adx, lookback)
                return None

        structure_lb = config.SYMBOL_STRUCTURE_LOOKBACK_OVERRIDE.get(symbol, config.STRUCTURE_LOOKBACK)
        if config.STRUCTURE_BREAK_FILTER_ENABLED and not self.structure_intact(df_htf, bias, lookback=structure_lb):
            logger.info("%s: no signal (market structure already broke against %s bias)", symbol, bias)
            return None

        if config.MOMENTUM_DIVERGENCE_FILTER_ENABLED and self.momentum_diverging_against_bias(df_htf, bias, lookback=structure_lb):
            logger.info("%s: no signal (momentum diverging against %s bias)", symbol, bias)
            return None

        # Down to 3 confluences by design: bias, sweep, confidence score
        # (plus the H4 bias agreement check above).
        # ADX/ATR regime strength no longer hard-rejects here — it still
        # feeds the confidence score's trend_component below, so a weak
        # trend/volatility regime drags the score down instead of vetoing
        # the trade outright.
        setup = self.detect_liquidity_sweep(df_ltf, bias, df_level_tf=df_liquidity_tf)
        if setup is None:
            logger.info("%s: no signal (no liquidity sweep setup this cycle, bias=%s)", symbol, bias)
            return None

        current_adx = self.get_htf_trend_strength(df_htf)
        confidence = self._compute_confidence_score(setup, current_adx)

        min_confidence = config.SYMBOL_MIN_CONFIDENCE_OVERRIDE.get(symbol, config.MIN_CONFIDENCE_SCORE)
        if confidence < min_confidence:
            logger.info("%s: no signal (confidence %.2f below threshold %.2f)",
                        symbol, confidence, min_confidence)
            return None

        logger.info("Signal generated: %s %s @ %.5f (bias=%s, confidence=%.2f)",
                    symbol, setup["direction"], setup["entry"], bias, confidence)

        return Signal(
            symbol=symbol,
            direction=setup["direction"],
            entry_price=setup["entry"],
            stop_loss=setup["stop_loss"],
            take_profit=setup["take_profit"],
            confidence_score=confidence,
        )

    def evaluate_momentum(self, symbol: str, df_ltf: pd.DataFrame) -> Signal | None:
        """
        Entry point for the second strategy (see detect_momentum_breakout).
        Separate from evaluate() on purpose — the two are meant to be run,
        tested, and (for now) enabled/disabled independently. Shares the
        same trading-window restriction as the sweep strategy for this first
        trial; doesn't check H4/M5 bias at all (see detect_momentum_breakout's
        docstring for why). confidence_score is fixed at 1.0 here since
        there's no equivalent composite score built for this strategy yet —
        Signal requires the field, but nothing downstream currently reads it
        as a threshold for momentum trades.
        """
        if df_ltf is not None and len(df_ltf) and not self.in_trading_window(df_ltf["time"].iloc[-1]):
            logger.info("%s: no momentum signal (outside the allowed London-open trading window)", symbol)
            return None

        setup = self.detect_momentum_breakout(df_ltf)
        if setup is None:
            logger.info("%s: no momentum signal this cycle", symbol)
            return None

        logger.info("Momentum signal generated: %s %s @ %.5f (net_move=%.2f ATR)",
                    symbol, setup["direction"], setup["entry"], setup["net_move_atr"])

        return Signal(
            symbol=symbol,
            direction=setup["direction"],
            entry_price=setup["entry"],
            stop_loss=setup["stop_loss"],
            take_profit=setup["take_profit"],
            confidence_score=1.0,
        )
