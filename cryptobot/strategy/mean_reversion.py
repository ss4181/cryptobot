"""Mean-reversion strategy: Bollinger dip-buy with trend + RSI filters (long-only spot).

Entry (all must hold on the closed bar):

 1. **Dip**     -- close is at/below the lower Bollinger band,
 2. **Trend**   -- close is above the long SMA (do not catch knives in a downtrend),
 3. **Oversold**-- RSI(period) is at/below ``rsi_oversold``.

Exit:

 * the mean-reversion target is the middle band (``exit_at_middle_band``), and
 * the risk manager additionally enforces the net-profit take-profit and the
   stop-loss; the strategy itself never handles money.

Everything is long-only; the bot holds USDT when flat.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

import pandas as pd

from .base import ENTER, EXIT, HOLD, Series, Signal, Strategy, register_strategy
from .indicators import bollinger, rsi, sma

DEFAULT_PARAMS: Dict[str, Any] = {
    "bb_period": 20,
    "bb_std": 2.0,
    "rsi_period": 14,
    "rsi_oversold": 35.0,
    "trend_sma_period": 200,
    "exit_at_middle_band": True,
}


@register_strategy
class MeanReversionStrategy(Strategy):
    """Bollinger-band dip buyer with a trend filter and an RSI oversold filter."""

    name = "mean_reversion"

    def __init__(self, params: Optional[Mapping[str, Any]] = None) -> None:
        merged = dict(DEFAULT_PARAMS)
        merged.update(dict(params or {}))
        super().__init__(merged)
        self.bb_period = int(self.params["bb_period"])
        self.bb_std = float(self.params["bb_std"])
        self.rsi_period = int(self.params["rsi_period"])
        self.rsi_oversold = float(self.params["rsi_oversold"])
        self.trend_sma_period = int(self.params["trend_sma_period"])
        self.exit_at_middle_band = bool(self.params["exit_at_middle_band"])
        self._validate()

    def _validate(self) -> None:
        if self.bb_period < 2:
            raise ValueError("bb_period must be >= 2")
        if self.bb_std <= 0:
            raise ValueError("bb_std must be > 0")
        if self.rsi_period < 1:
            raise ValueError("rsi_period must be >= 1")
        if not 0 < self.rsi_oversold < 100:
            raise ValueError("rsi_oversold must be in (0, 100)")
        if self.trend_sma_period < 1:
            raise ValueError("trend_sma_period must be >= 1")

    # ------------------------------------------------------------------ setup
    @property
    def min_candles(self) -> int:
        return max(self.bb_period, self.rsi_period + 1, self.trend_sma_period) + 1

    @property
    def columns(self) -> Dict[str, str]:
        return {
            "sma_trend": "sma_{}".format(self.trend_sma_period),
            "bb_upper": "bb_upper",
            "bb_middle": "bb_middle",
            "bb_lower": "bb_lower",
            "rsi": "rsi",
        }

    def prepare(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Vectorised indicator computation on the closed-candle series."""
        out = frame.copy()
        close = out["close"].to_numpy(dtype=float)
        upper, middle, lower = bollinger(close, self.bb_period, self.bb_std)
        out["sma_trend"] = sma(close, self.trend_sma_period)
        out["bb_upper"] = upper
        out["bb_middle"] = middle
        out["bb_lower"] = lower
        out["rsi"] = rsi(close, self.rsi_period)
        return out

    # --------------------------------------------------------------- decision
    def decide(self, index: int, series: Series, *, has_position: bool) -> Signal:
        """Decision for one bar, using pre-extracted numpy series (no pandas)."""
        close = series["close"]
        price = float(close[index])
        snapshot = self._snapshot(series, index)

        trend = self._value(series, "sma_trend", index)
        lower = self._value(series, "bb_lower", index)
        rsi_value = self._value(series, "rsi", index)
        if index < self.min_candles - 1 or math.isnan(trend) or math.isnan(lower) or math.isnan(rsi_value):
            return Signal(HOLD, "warmup:indicators_not_ready", price, snapshot)

        if has_position:
            return self._evaluate_exit(series, index, price, snapshot)
        return self._evaluate_entry(price, trend, lower, rsi_value, snapshot)

    def _evaluate_exit(self, series: Series, index: int, price: float, snapshot: Dict[str, float]) -> Signal:
        if self.exit_at_middle_band:
            middle = self._value(series, "bb_middle", index)
            if not math.isnan(middle) and price >= middle:
                return Signal(EXIT, "exit:close_reached_middle_band", price, snapshot)
        return Signal(HOLD, "hold:waiting_for_mean_reversion", price, snapshot)

    def _evaluate_entry(
        self,
        price: float,
        trend: float,
        lower: float,
        rsi_value: float,
        snapshot: Dict[str, float],
    ) -> Signal:
        if price <= trend:
            return Signal(HOLD, "blocked:trend_filter(close_must_be_above_sma)", price, snapshot)
        if price > lower:
            return Signal(HOLD, "blocked:no_dip(close_above_lower_band)", price, snapshot)
        if rsi_value > self.rsi_oversold:
            return Signal(HOLD, "blocked:not_oversold", price, snapshot)
        return Signal(ENTER, "entry:bollinger_dip+trend+oversold", price, snapshot)

    @staticmethod
    def _value(series: Series, key: str, index: int) -> float:
        """Indicator value at ``index`` or ``NaN`` when unavailable/warm-up."""
        array = series.get(key)
        if array is None or index >= len(array) or index < 0:
            return float("nan")
        return float(array[index])

    def _snapshot(self, series: Series, index: int) -> Dict[str, float]:
        close = self._value(series, "close", index)
        return {
            "close": close,
            "bb_upper": self._value(series, "bb_upper", index),
            "bb_middle": self._value(series, "bb_middle", index),
            "bb_lower": self._value(series, "bb_lower", index),
            "rsi": self._value(series, "rsi", index),
            "sma_trend": self._value(series, "sma_trend", index),
        }


__all__ = ["MeanReversionStrategy", "DEFAULT_PARAMS"]
