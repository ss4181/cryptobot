"""Live-trading guard.

Design rule of this repository: **live trading is structurally absent, not
merely disabled by a flag**.  This module is the single choke point that turns
"someone asked for a real trading mode" into a hard, immediate error.

There is no ``create_order``/``createOrder``/signed-request helper anywhere in
this package, and no code path that reads an API key to authenticate a request.
:func:`assert_allowed_mode` is called by :mod:`cryptobot.config` on every load,
so even a crafted config file cannot select a live mode.
"""

from __future__ import annotations

import os
from typing import Final, Mapping, Optional, Tuple

PAPER_MODE: Final[str] = "paper"
BACKTEST_MODE: Final[str] = "backtest"

#: Every mode the bot is allowed to run in.
ALLOWED_MODES: Final[Tuple[str, ...]] = (PAPER_MODE, BACKTEST_MODE)

#: Tokens that indicate a request for real-money trading.  Matched after
#: normalisation (lowercase, separators stripped), so "Live-Trading",
#: "LIVE_TRADING" and "livetrading" all hit.
_FORBIDDEN_TOKENS: Final[frozenset] = frozenset(
    {
        "live",
        "real",
        "realtime",
        "prod",
        "production",
        "mainnet",
        "trading",
        "trade",
        "auth",
        "authenticated",
        "signed",
        "keys",
        "withdraw",
        "deposit",
        "margin",
        "futures",
        "leverage",
    }
)

#: Environment variables that would be needed for real trading.  They are
#: detected only so the bot can log that they are IGNORED.
_CREDENTIAL_ENV_HINTS: Final[Tuple[str, ...]] = (
    "BINANCE_API_KEY",
    "BINANCE_SECRET",
    "BINANCE_API_SECRET",
    "CRYPTOBOT_API_KEY",
    "CRYPTOBOT_API_SECRET",
    "API_KEY",
    "API_SECRET",
    "SECRET_KEY",
)

#: Public, unauthenticated market-data hosts the bot is allowed to call.
ALLOWED_DATA_HOSTS: Final[Tuple[str, ...]] = (
    "api.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api3.binance.com",
    "data-api.binance.vision",
    "api.binance.us",
)

#: Path fragments that indicate a signed / account / trading endpoint.  These are
#: refused even on an allowed host, so the whitelist cannot be abused to reach a
#: private API (e.g. ``https://api.binance.com/sapi/v1/order``).
_FORBIDDEN_PATH_TOKENS: Final[Tuple[str, ...]] = (
    "/sapi",
    "/order",
    "/account",
    "/userdata",
    "/usertrades",
    "/mytrades",
    "/withdraw",
    "/deposit",
    "/capital",
    "/margin",
    "/futures",
    "/batchorders",
    "/openorders",
    "/allorders",
)


class SafetyViolation(RuntimeError):
    """Base class for every safety-guard rejection."""


class LiveTradingForbidden(SafetyViolation):
    """Raised when anything asks for live / real-money trading."""


def normalize_mode(mode: object) -> str:
    """Return a canonical, comparable form of ``mode``."""
    if mode is None:
        return ""
    return str(mode).strip().lower().replace("-", "").replace("_", "").replace(" ", "")


def assert_allowed_mode(mode: object, *, source: str = "config") -> str:
    """Validate ``mode`` and return the canonical mode name.

    Raises :class:`LiveTradingForbidden` for anything that is not exactly
    ``paper`` or ``backtest`` -- including aliases such as ``live``,
    ``real``, ``production`` or an empty/unknown value.  Failing closed is
    deliberate: an unparseable mode is treated as an unsafe mode.
    """
    raw = "" if mode is None else str(mode).strip().lower()
    canonical = normalize_mode(mode)

    if canonical in ALLOWED_MODES:
        return canonical

    if not canonical:
        raise LiveTradingForbidden(
            "Empty trading mode is not allowed (source={}). "
            "Use 'paper' or 'backtest'. Live trading is not implemented.".format(source)
        )

    # Explicit, loud rejection for anything tokenising as live/real trading.
    if canonical in _FORBIDDEN_TOKENS or any(tok in canonical for tok in _FORBIDDEN_TOKENS):
        raise LiveTradingForbidden(
            "LIVE/REAL trading mode {!r} requested (source={}). This bot is paper-only: "
            "real order submission does not exist in this code base and cannot be enabled "
            "by configuration.".format(raw, source)
        )

    raise LiveTradingForbidden(
        "Unknown trading mode {!r} (source={}). Allowed modes: {}. "
        "Live trading is not implemented.".format(raw, source, ", ".join(ALLOWED_MODES))
    )


def assert_paper_only(flag_name: str = "live", *, value: object = None) -> None:
    """Reject any 'go live' style toggle, whatever it is called."""
    if value:
        raise LiveTradingForbidden(
            "{!r} is set. Live trading is structurally absent from this project; "
            "only --mode paper|backtest is supported.".format(flag_name)
        )


def audit_credentials(environ: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
    """Return the names of credential env vars found (they are never used).

    Presence of these variables is **not** an error -- the bot simply refuses
    to read them and logs that they are ignored, so users cannot accidentally
    point the bot at a funded account.
    """
    env = os.environ if environ is None else environ
    found = tuple(sorted(name for name in _CREDENTIAL_ENV_HINTS if env.get(name)))
    return found


def assert_public_endpoint(url: str) -> str:
    """Allow only known public, unauthenticated market-data endpoints.

    Both the host and the path are checked: a signed/account path such as
    ``/sapi/v1/order`` is refused even when the host is whitelisted.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_DATA_HOSTS:
        raise SafetyViolation(
            "Refusing to call non-whitelisted host {!r}. This bot only reads public "
            "market data from {}.".format(host, ", ".join(ALLOWED_DATA_HOSTS))
        )
    path = (parsed.path or "").lower()
    for token in _FORBIDDEN_PATH_TOKENS:
        if token in path:
            raise SafetyViolation(
                "Refusing to call {!r}: '{}' is a signed/private endpoint. This bot has no "
                "credentials and cannot trade.".format(url, token)
            )
    return url


def safety_statement() -> str:
    """Human-readable summary of the safety posture (used by ``status``)."""
    return (
        "mode: paper/backtest only | live order submission: absent | "
        "credentials required: none | allowed hosts: " + ", ".join(ALLOWED_DATA_HOSTS)
    )


__all__ = [
    "PAPER_MODE",
    "BACKTEST_MODE",
    "ALLOWED_MODES",
    "ALLOWED_DATA_HOSTS",
    "SafetyViolation",
    "LiveTradingForbidden",
    "normalize_mode",
    "assert_allowed_mode",
    "assert_paper_only",
    "assert_public_endpoint",
    "audit_credentials",
    "safety_statement",
]
