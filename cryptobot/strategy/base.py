"""Strategy interface + registry.

A strategy is a pure decision function over *already closed* candles:

* :meth:`Strategy.prepare` adds indicator columns (vectorised, no look-ahead),
* :meth:`Strategy.decide` looks at one bar index inside pre-extracted numpy
  series and returns a :class:`Signal` -- ``enter``, ``exit`` or ``hold`` -- plus
  the reason and the indicator snapshot that produced it,
* :meth:`Strategy.evaluate` is a convenience wrapper that extracts the series
  from a DataFrame (used by tests and ad-hoc analysis).

The numpy-series path exists so the backtest loop never touches pandas per bar,
which keeps a 180-day 15m replay fast; logic lives in exactly one place
(``decide``), so both paths always agree.

The strategy never sizes positions, never touches prices for execution and never
knows about money: that is the risk manager's and the broker's job, which keeps
the components independently testable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

import numpy as np
import pandas as pd

ENTER = "enter"
EXIT = "exit"
HOLD = "hold"
ACTIONS = (ENTER, EXIT, HOLD)

#: Columns copied into the numpy series dict by :meth:`Strategy.series_from_frame`.
PRICE_COLUMNS = ("open", "high", "low", "close", "volume")

Series = Mapping[str, np.ndarray]


@dataclass(frozen=True)
class Signal:
    """A decision for one bar."""

    action: str
    reason: str
    price: float
    indicators: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError("unknown action {!r}; expected one of {}".format(self.action, ACTIONS))

    @property
    def is_entry(self) -> bool:
        return self.action == ENTER

    @property
    def is_exit(self) -> bool:
        return self.action == EXIT

    def as_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "indicators": dict(self.indicators)}


class Strategy(ABC):
    """Base class every strategy must implement."""

    #: registry key, e.g. ``mean_reversion``
    name: str = "base"

    def __init__(self, params: Optional[Mapping[str, Any]] = None) -> None:
        self.params: Dict[str, Any] = dict(params or {})

    # ------------------------------------------------------------------ hooks
    @property
    @abstractmethod
    def min_candles(self) -> int:
        """Number of bars required before the first signal can be produced."""

    @abstractmethod
    def prepare(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of ``frame`` with the indicator columns added."""

    @abstractmethod
    def decide(self, index: int, series: Series, *, has_position: bool) -> Signal:
        """Decide what to do at bar ``index`` using pre-extracted numpy series."""

    # -------------------------------------------------------------- helpers
    def evaluate(self, frame: pd.DataFrame, index: int, *, has_position: bool) -> Signal:
        """DataFrame convenience wrapper around :meth:`decide`."""
        return self.decide(index, self.series_from_frame(frame), has_position=has_position)

    @staticmethod
    def series_from_frame(frame: pd.DataFrame) -> Dict[str, np.ndarray]:
        """Extract every column as a float numpy array (indicators included)."""
        return {column: frame[column].to_numpy(dtype=float) for column in frame.columns}

    def warmup_bars(self) -> int:
        """Bars to skip at the start of a backtest."""
        return max(0, int(self.min_candles))

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "params": dict(sorted(self.params.items()))}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "{}({})".format(type(self).__name__, self.params)


_REGISTRY: Dict[str, type] = {}


def register_strategy(cls: type) -> type:
    """Class decorator adding a strategy to the registry."""
    key = getattr(cls, "name", None)
    if not key or key == "base":
        raise ValueError("strategy class {} must define a unique 'name'".format(cls))
    _REGISTRY[key] = cls
    return cls


def available_strategies() -> tuple:
    return tuple(sorted(_REGISTRY))


def get_strategy(name: str, params: Optional[Mapping[str, Any]] = None) -> Strategy:
    """Instantiate a registered strategy by name."""
    try:
        cls = _REGISTRY[str(name)]
    except KeyError as exc:
        raise KeyError("unknown strategy {!r}; available: {}".format(name, ", ".join(available_strategies()))) from exc
    return cls(params)


__all__ = [
    "ENTER", "EXIT", "HOLD", "ACTIONS", "PRICE_COLUMNS", "Series",
    "Signal", "Strategy", "register_strategy", "get_strategy", "available_strategies",
]
