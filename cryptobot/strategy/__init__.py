"""Trading strategies: pure decision functions over closed candles.

Importing this package registers every shipped strategy, so
``cryptobot.strategy.base.get_strategy(name)`` works without the caller having to
import the concrete module.
"""

from __future__ import annotations

from .base import (  # noqa: F401  (re-exported for convenience)
    ACTIONS,
    ENTER,
    EXIT,
    HOLD,
    Signal,
    Strategy,
    available_strategies,
    get_strategy,
    register_strategy,
)
from .mean_reversion import MeanReversionStrategy  # noqa: F401  (self-registers)

__all__ = [
    "ACTIONS", "ENTER", "EXIT", "HOLD",
    "Signal", "Strategy", "register_strategy", "get_strategy", "available_strategies",
    "MeanReversionStrategy",
]
