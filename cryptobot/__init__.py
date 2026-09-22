"""cryptobot -- paper-trading (simulation only) crypto bot.

This package is a *simulation harness*.  It downloads public market data,
runs a strategy against it and records simulated fills in a local ledger.

It cannot trade real money: there are no API keys, no signed requests and no
order-submission code path anywhere in the package (see ``cryptobot/safety.py``
and the ``no-live-order`` grep check in ``scripts/verify_all.py``).
"""

from __future__ import annotations

__version__ = "1.0.0"

#: Short banner printed by the CLI on every run.
SAFETY_BANNER = (
    "cryptobot v{} -- PAPER TRADING / SIMULATION ONLY. "
    "No exchange keys. No real orders. Not investment advice."
).format(__version__)

__all__ = ["__version__", "SAFETY_BANNER"]
