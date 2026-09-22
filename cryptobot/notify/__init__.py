"""Pluggable, failure-isolated mobile notifications for the paper bot.

Design in one paragraph: the trading engine and runner emit small *events*
(:mod:`cryptobot.notify.events`); :class:`~cryptobot.notify.dispatcher.Notifier`
applies anti-spam filters, fans the event out to the configured providers
(ntfy / telegram / webhook / console / file) with a per-provider timeout and a
bounded exponential-backoff retry, and appends one audit record per attempt to
``logs/notifications.jsonl``.  Any failure -- bad DNS, HTTP 500, a provider that
raises, a full disk -- is caught and recorded, never raised into the trading
loop, and never changes a trading decision.  There is no exchange/order code
path anywhere in this package.

Typical use::

    from cryptobot.notify import build_notifier, events
    notifier = build_notifier(config)                       # env provides secrets
    notifier.notify(events.position_opened(...))            # never raises
"""

from __future__ import annotations

from typing import Any, Optional

from .dispatcher import NotifyConfig, Notifier
from .events import (
    EVENT_SEVERITIES,
    EVENT_TYPES,
    SUPERSEDED_BY,
    DEFAULT_NOTIFY_ON,
    NotificationEvent,
    make_event,
    severity_at_least,
)
from .guard import (
    HARNESS_ENV,
    REASON_HARNESS,
    REASON_OFFLINE,
    REASON_REPLAY,
    REASON_SUPERSEDED,
    enter_harness_mode,
    exit_harness_mode,
    harness_mode,
    host_is_local,
)
from .providers import Provider, build_providers
from .redact import REDACTED, redact
from .render import MessageContent, Section, render_body
from .store import NotificationStore
from .watcher import EquityDropMonitor

DEFAULT_MIN_SEVERITY = "info"


def build_notifier(
    config: Any,
    *,
    environ: Optional[dict] = None,
    transport: Any = None,
    writer: Any = None,
    clock: Any = None,
    sleep: Any = None,
    mono: Any = None,
    logger: Any = None,
    network_send_allowed: bool = True,
    network_block_reason: str = REASON_REPLAY,
) -> Notifier:
    """Build a :class:`Notifier` from a validated :class:`cryptobot.config.Config`.

    ``network_send_allowed=False`` marks a replay/offline run in which network
    providers must not push (see :mod:`cryptobot.notify.guard`).
    """
    notify_config = NotifyConfig.from_config(
        config, environ=environ, network_send_allowed=network_send_allowed,
        network_block_reason=network_block_reason,
    )
    kwargs: dict = {}
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep
    if mono is not None:
        kwargs["mono"] = mono
    if logger is not None:
        kwargs["logger"] = logger
    return Notifier(notify_config, transport=transport, writer=writer, **kwargs)


__all__ = [
    "Notifier", "NotifyConfig", "NotificationEvent", "NotificationStore",
    "Provider", "EquityDropMonitor", "build_providers", "build_notifier",
    "make_event", "severity_at_least", "redact", "REDACTED",
    "EVENT_TYPES", "EVENT_SEVERITIES", "DEFAULT_NOTIFY_ON", "SUPERSEDED_BY",
    "DEFAULT_MIN_SEVERITY",
    "HARNESS_ENV", "REASON_HARNESS", "REASON_REPLAY", "REASON_OFFLINE",
    "REASON_SUPERSEDED", "enter_harness_mode", "exit_harness_mode",
    "harness_mode", "host_is_local",
    "MessageContent", "Section", "render_body",
]
