"""Pure technical indicators (no I/O, no state, inputs are never mutated).

All functions take a 1-D sequence of prices and return a float array of the
*same length*, left-padded with ``NaN`` during the warm-up window.  That makes
look-ahead bugs easy to spot: index ``i`` only ever depends on values ``<= i``.
"""

from __future__ import annotations

from typing import Sequence, Tuple

import numpy as np

__all__ = ["sma", "rolling_std", "bollinger", "rsi", "log_zscore"]


def _as_float_array(values: Sequence[float]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise ValueError("expected a 1-D series, got shape {}".format(array.shape))
    return array


def sma(values: Sequence[float], period: int) -> np.ndarray:
    """Simple moving average, ``NaN`` for the first ``period - 1`` slots."""
    array = _as_float_array(values)
    period = int(period)
    if period < 1:
        raise ValueError("period must be >= 1")
    out = np.full(array.shape, np.nan, dtype=float)
    if array.size < period:
        return out
    cumsum = np.cumsum(np.insert(array, 0, 0.0))
    out[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return out


def rolling_std(values: Sequence[float], period: int, ddof: int = 0) -> np.ndarray:
    """Rolling standard deviation (population by default, as Bollinger bands use)."""
    array = _as_float_array(values)
    period = int(period)
    if period < 2:
        raise ValueError("period must be >= 2")
    out = np.full(array.shape, np.nan, dtype=float)
    if array.size < period:
        return out
    for i in range(period - 1, array.size):
        window = array[i - period + 1: i + 1]
        out[i] = float(np.std(window, ddof=ddof))
    return out


def bollinger(values: Sequence[float], period: int, num_std: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger bands -> ``(upper, middle, lower)``."""
    num_std = float(num_std)
    if num_std <= 0:
        raise ValueError("num_std must be > 0")
    middle = sma(values, period)
    std = rolling_std(values, period, ddof=0)
    return middle + num_std * std, middle, middle - num_std * std


def rsi(values: Sequence[float], period: int = 14) -> np.ndarray:
    """Wilder's RSI, ``NaN`` until ``period`` deltas are available.

    A flat series yields 100.0 only when there are no losses at all; the
    both-zero case (perfectly flat) is defined as 50.0 to stay neutral.
    """
    array = _as_float_array(values)
    period = int(period)
    if period < 1:
        raise ValueError("period must be >= 1")
    out = np.full(array.shape, np.nan, dtype=float)
    if array.size <= period:
        return out

    delta = np.diff(array)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = float(np.mean(gain[:period]))
    avg_loss = float(np.mean(loss[:period]))
    out[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, array.size):
        avg_gain = (avg_gain * (period - 1) + gain[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i - 1]) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0 and avg_gain == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def log_zscore(values: Sequence[float], period: int = 100, ddof: int = 0) -> np.ndarray:
    """Z-score of ``log(value)`` against the **prior** ``period`` values.

    Index ``i`` is measured against ``values[i-period:i]`` -- the current value is
    excluded, so the score is not self-referential and index ``i`` only depends on
    data available at ``i`` (no look-ahead).  Non-positive values are treated as
    ``NaN`` (a log of 0 or a negative volume is undefined, not zero volume).
    """
    array = _as_float_array(values)
    period = int(period)
    if period < 2:
        raise ValueError("period must be >= 2")
    out = np.full(array.shape, np.nan, dtype=float)
    if array.size <= period:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        logged = np.where(array > 0, np.log(array), np.nan)
    for i in range(period, array.size):
        window = logged[i - period: i]
        window = window[np.isfinite(window)]
        if window.size < 2:
            continue
        std = float(np.std(window, ddof=ddof))
        if std <= 1e-12:
            continue
        current = logged[i]
        if not np.isfinite(current):
            continue
        out[i] = (float(current) - float(np.mean(window))) / std
    return out
