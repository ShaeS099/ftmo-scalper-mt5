"""
Minimal technical-indicator implementations using only pandas/numpy.

Deliberately avoids the pandas_ta package: its upstream PyPI package had
its release history wiped and ownership transferred to a different
maintainer in 2025, and the classic version this project originally used
was deleted from PyPI entirely. Rather than depend on an external TA
library with an unstable/uncertain supply chain, ATR and ADX are
implemented here directly with Wilder's smoothing (the standard method
used by most trading platforms, including MT5's own built-in indicators).
"""
import numpy as np
import pandas as pd


def wilder_smooth(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing = an EWM with alpha = 1/length."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1)
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return wilder_smooth(tr, length)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Returns the ADX line only (not +DI/-DI), which is all the filter needs."""
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    tr = true_range(high, low, close)
    smoothed_tr = wilder_smooth(tr, length)
    smoothed_plus_dm = wilder_smooth(plus_dm, length)
    smoothed_minus_dm = wilder_smooth(minus_dm, length)

    plus_di = 100 * (smoothed_plus_dm / smoothed_tr.replace(0, np.nan))
    minus_di = 100 * (smoothed_minus_dm / smoothed_tr.replace(0, np.nan))

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum

    return wilder_smooth(dx.fillna(0), length)


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's original RSI (the classic smoothing, same convention as atr/adx above)."""
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = wilder_smooth(gains, length)
    avg_loss = wilder_smooth(losses, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(100)
