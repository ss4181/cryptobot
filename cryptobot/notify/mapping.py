"""Map internal engine/runner payloads onto :class:`NotificationEvent` objects.

The trading engine never constructs notification text.  It emits small
*dictionaries* through an optional ``event_sink`` (``backtest/engine.py``), and
the runner forwards them here.  Keeping the mapping in one place means:

* the engine stays free of presentation logic and of any network reference, and
* a backtest that never installs a sink stays completely silent (the default).

All wording and layout live in :mod:`cryptobot.notify.render`; this module only
decides which fields of an engine payload feed which factory, defensively -- a
missing field yields ``None``/``n/a`` instead of an exception, because this runs
in the trading loop.

``stats`` is an optional *historical measurement* (already computed by an offline
backtest over the cache, see :mod:`cryptobot.notify.context`) attached to entry
events.  It may be a mapping or a zero-argument callable; either way a failure to
resolve it only means the history block is omitted.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Union

from . import events as ev
from .events import NotificationEvent

#: Engine payload ``type`` -> notification event type.
ENGINE_TYPES = (
    "position_opened",
    "position_closed",
    "take_profit_hit",
    "stop_loss_hit",
    "data_fail_safe",
    "cooldown_started",
    "risk_halted",
)

StatsSource = Union[Mapping[str, Any], Callable[[], Optional[Mapping[str, Any]]], None]


def _resolve_stats(stats: StatsSource) -> Optional[Mapping[str, Any]]:
    if stats is None:
        return None
    try:
        value = stats() if callable(stats) else stats
    except Exception:  # a stats lookup must never break an event
        return None
    return value if isinstance(value, Mapping) else None


def from_engine(payload: Mapping[str, Any], *, run_id: str = "",
                stats: StatsSource = None) -> Optional[NotificationEvent]:
    """Translate one engine payload; ``None`` when the payload is not notifiable."""
    if not isinstance(payload, Mapping):
        return None
    kind = str(payload.get("type", "")).strip().lower()
    if kind not in ENGINE_TYPES:
        return None
    ts = int(payload.get("ts") or 0)
    pair = str(payload.get("pair") or "")
    rid = str(payload.get("run_id") or run_id or "")
    trigger = str(payload.get("trigger") or "")
    mode = str(payload.get("mode") or "paper")
    timeframe = str(payload.get("timeframe") or "")

    if kind == "position_opened":
        return ev.position_opened(
            run_id=rid, pair=pair, entry_price=payload.get("entry_price"),
            qty=payload.get("qty"), notional=payload.get("notional"),
            stop_price=payload.get("stop_price"), tp_price=payload.get("tp_price"),
            reason=str(payload.get("reason") or ""), ts=ts, mode=mode, timeframe=timeframe,
            net_target_pct=payload.get("net_target_pct"),
            stop_loss_pct=payload.get("stop_loss_pct"),
            indicators=payload.get("indicators"),
            strategy_params=payload.get("strategy_params"),
            volume_log_z=payload.get("volume_log_z"),
            history=_resolve_stats(stats),
        )

    if kind == "position_closed":
        return ev.position_closed(
            run_id=rid, pair=pair, entry_price=payload.get("entry_price"),
            exit_price=payload.get("exit_price"), qty=payload.get("qty"),
            fees=payload.get("fees"), gross_pnl=payload.get("gross_pnl"),
            net_pnl=payload.get("net_pnl"), net_pnl_pct=payload.get("net_pnl_pct"),
            trigger=trigger, ts=ts, exit_reason=str(payload.get("exit_reason") or ""),
            bars_held=payload.get("bars_held"), held_seconds=payload.get("held_seconds"),
            mode=mode, timeframe=timeframe,
        )

    if kind == "take_profit_hit":
        return ev.take_profit_hit(
            run_id=rid, pair=pair, entry_price=payload.get("entry_price"),
            exit_price=payload.get("exit_price"), net_pnl=payload.get("net_pnl"),
            net_pnl_pct=payload.get("net_pnl_pct"), ts=ts, qty=payload.get("qty"),
            fees=payload.get("fees"), gross_pnl=payload.get("gross_pnl"),
            exit_reason=str(payload.get("exit_reason") or ""),
            bars_held=payload.get("bars_held"), held_seconds=payload.get("held_seconds"),
            mode=mode, timeframe=timeframe,
        )

    if kind == "stop_loss_hit":
        return ev.stop_loss_hit(
            run_id=rid, pair=pair, entry_price=payload.get("entry_price"),
            exit_price=payload.get("exit_price"), net_pnl=payload.get("net_pnl"),
            net_pnl_pct=payload.get("net_pnl_pct"), ts=ts, qty=payload.get("qty"),
            fees=payload.get("fees"), gross_pnl=payload.get("gross_pnl"),
            exit_reason=str(payload.get("exit_reason") or ""),
            bars_held=payload.get("bars_held"), held_seconds=payload.get("held_seconds"),
            mode=mode, timeframe=timeframe,
        )

    if kind == "data_fail_safe":
        return ev.data_fail_safe(run_id=rid, reason=str(payload.get("reason") or ""), ts=ts)

    if kind == "cooldown_started":
        return ev.cooldown_started(
            run_id=rid, pair=pair, net_pnl=payload.get("net_pnl"),
            cooldown_minutes=payload.get("cooldown_minutes"), ts=ts,
        )

    return ev.risk_halted(
        run_id=rid, reason=str(payload.get("reason") or ""),
        day_realized_net_pnl=payload.get("day_realized_net_pnl"), ts=ts,
    )


__all__ = ["ENGINE_TYPES", "from_engine"]
