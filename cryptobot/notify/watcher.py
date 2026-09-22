"""Global equity-drawdown watcher.

Turns "the whole account is down X% from its high-water mark" into one
notification.  Deliberately tiny and stateful: it triggers **once per drawdown
episode** (a new equity high re-arms it) so a slow bleed cannot spam the phone.
"""

from __future__ import annotations

from typing import Optional, Tuple


class EquityDropMonitor:
    """Track the equity high-water mark and report threshold crossings."""

    def __init__(self, threshold_pct: float, *, enabled: bool = True) -> None:
        self.threshold_pct = float(threshold_pct)
        self.enabled = bool(enabled) and self.threshold_pct > 0
        self.peak: Optional[float] = None
        self._armed = True
        self._last_drop_pct: Optional[float] = None

    def update(self, equity: float, ts: int = 0) -> Optional[Tuple[float, float]]:
        """Feed one equity value.

        Returns ``(peak_equity, drop_pct)`` exactly once when the drawdown from
        the high-water mark first reaches the threshold, else ``None``.
        """
        if not self.enabled:
            return None
        try:
            value = float(equity)
        except (TypeError, ValueError):
            return None
        if value != value:  # NaN
            return None
        if self.peak is None or value > self.peak:
            self.peak = value
            self._armed = True
            self._last_drop_pct = None
            return None
        if self.peak <= 0:
            return None
        drop_pct = (self.peak - value) / self.peak * 100.0
        self._last_drop_pct = drop_pct
        if self._armed and drop_pct >= self.threshold_pct:
            self._armed = False
            return (self.peak, drop_pct)
        return None

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "threshold_pct": self.threshold_pct,
            "peak_equity": self.peak,
            "last_drop_pct": self._last_drop_pct,
            "armed": self._armed,
        }


__all__ = ["EquityDropMonitor"]
