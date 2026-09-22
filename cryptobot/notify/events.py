"""Notification events: the small, provider-agnostic message model.

An event carries two representations of the same message:

* ``data`` -- the structured facts (prices, quantities, margins, stats).  This is
  what the renderer consumes, and what a webhook/audit consumer can machine-read.
* ``content`` -- the shared :class:`~cryptobot.notify.render.MessageContent` model
  (headline + sections) rendered per carrier by
  :mod:`cryptobot.notify.render`.  ``body`` is its clean **plain-text** rendering,
  so the audit file and every non-formatting consumer stay readable.

Severity lives on a three-step ladder -- ``info < warning < critical`` -- and
every event type has a sensible default so a caller cannot forget one.

Nothing in this module talks to the network: it is the data model shared by the
engine/runner (producers) and the providers (consumers).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .render import MessageContent, build_content, to_plain

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

#: Low -> high.  ``min_severity`` filters anything below the configured step.
SEVERITIES: Tuple[str, ...] = (SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL)
SEVERITY_RANK: Dict[str, int] = {name: index for index, name in enumerate(SEVERITIES)}

#: Default severity per event type.  Keys are the canonical event names.
EVENT_SEVERITIES: Dict[str, str] = {
    "bot_started": SEVERITY_INFO,
    "bot_stopped": SEVERITY_INFO,
    "position_opened": SEVERITY_INFO,
    "position_closed": SEVERITY_INFO,
    "take_profit_hit": SEVERITY_INFO,
    "stop_loss_hit": SEVERITY_WARNING,
    "risk_halted": SEVERITY_CRITICAL,
    "cooldown_started": SEVERITY_WARNING,
    "feed_outage": SEVERITY_CRITICAL,
    "data_fail_safe": SEVERITY_WARNING,
    "daily_summary": SEVERITY_INFO,
    "equity_drop": SEVERITY_WARNING,
    "test": SEVERITY_INFO,
}

#: Every event type the notification layer understands.
EVENT_TYPES: Tuple[str, ...] = tuple(EVENT_SEVERITIES)

#: Trade-only shipping default: exactly the two events the user asked for.
#: Every other type stays implemented and previewable -- re-enable one with a
#: single ``notifications.notify_on`` line (see NOTIFICATIONS.md).
DEFAULT_NOTIFY_ON: Tuple[str, ...] = ("position_opened", "position_closed")

#: Detail variants of another event.  ``take_profit_hit``/``stop_loss_hit`` are
#: the *same* exit already reported by ``position_closed`` (the engine emits the
#: close first, then the variant for the same ``ts``/pair).  When the parent type
#: is enabled, the variant is therefore **not** pushed to a network provider: the
#: close message is the single source of truth, so one exit is one push.  The
#: variant is still recorded locally (console/file + audit ``suppressed`` row) and
#: is fully previewable with ``notify preview``.
SUPERSEDED_BY: Dict[str, str] = {
    "take_profit_hit": "position_closed",
    "stop_loss_hit": "position_closed",
}


def severity_rank(severity: str) -> int:
    """Rank of ``severity`` (unknown values rank lowest, i.e. most verbose)."""
    return SEVERITY_RANK.get(str(severity).strip().lower(), 0)


def severity_at_least(severity: str, minimum: str) -> bool:
    return severity_rank(severity) >= severity_rank(minimum)


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _compact(pair: Any) -> str:
    return str(pair or "").replace("/", "").strip()


@dataclass(frozen=True)
class NotificationEvent:
    """One message to push. Immutable and JSON-friendly."""

    type: str
    title: str
    body: str
    severity: str = SEVERITY_INFO
    ts: int = 0
    run_id: str = ""
    pair: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    #: Shared content model (headline + sections).  ``None`` means "render the
    #: generic fallback from title/body" -- see ``notify.render.build_content``.
    content: Optional[MessageContent] = None

    def resolved_severity(self) -> str:
        return self.severity if self.severity in SEVERITIES else EVENT_SEVERITIES.get(self.type, SEVERITY_INFO)

    def dedupe_key(self) -> str:
        """Identity used by ``dedupe_window_seconds``."""
        return "{}|{}|{}|{}".format(self.type, self.resolved_severity(), self.title, self.body)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "title": self.title,
            "body": self.body,
            "severity": self.resolved_severity(),
            "ts": int(self.ts),
            "run_id": self.run_id,
            "pair": self.pair,
            "data": dict(sorted(self.data.items())),
        }


def make_event(event_type: str, title: str, body: str, *, ts: int = 0, run_id: str = "",
               pair: str = "", severity: Optional[str] = None,
               data: Optional[Mapping[str, Any]] = None,
               content: Optional[MessageContent] = None) -> NotificationEvent:
    """Build an event, deriving severity from the event type when not given.

    ``content`` is filled in from ``data`` when the caller does not supply it;
    ``body`` is always kept exactly as given (callers that want the rendered
    plain text use the per-event factories below).
    """
    event = NotificationEvent(
        type=str(event_type),
        title=str(title),
        body=str(body),
        severity=severity or EVENT_SEVERITIES.get(str(event_type), SEVERITY_INFO),
        ts=int(ts or 0),
        run_id=str(run_id or ""),
        pair=str(pair or ""),
        data=dict(data or {}),
        content=content,
    )
    if event.content is None:
        try:
            event = replace(event, content=build_content(event))
        except Exception:  # pragma: no cover - rendering must never break construction
            event = replace(event, content=None)
    return event


def _finish(event_type: str, title: str, data: Mapping[str, Any], *, ts: int = 0,
            run_id: str = "", pair: str = "", severity: Optional[str] = None) -> NotificationEvent:
    """Factory tail: build the event, its content model and its plain-text body."""
    event = NotificationEvent(
        type=event_type,
        title=title,
        body="",
        severity=severity or EVENT_SEVERITIES.get(event_type, SEVERITY_INFO),
        ts=int(ts or 0),
        run_id=str(run_id or ""),
        pair=str(pair or ""),
        data=dict(data or {}),
        content=None,
    )
    content = build_content(event)
    return replace(event, content=content, body=to_plain(content))


# --------------------------------------------------------------------------- #
# factories used by the runner/engine (wording + layout live in one place)
# --------------------------------------------------------------------------- #
def bot_started(*, run_id: str, mode: str, pairs: Sequence[str], timeframe: str,
                initial_capital: float, dry_run: bool = False, ts: int = 0) -> NotificationEvent:
    data = {
        "mode": mode,
        "pairs": list(pairs or ()),
        "timeframe": timeframe,
        "initial_capital": initial_capital,
        "dry_run": bool(dry_run),
        "run_id": run_id,
    }
    return _finish("bot_started", "BOT BASLADI" + (" (dry-run)" if dry_run else ""),
                   data, ts=ts, run_id=run_id)


def bot_stopped(*, run_id: str, final_equity: float, net_pnl: float, net_pnl_pct: float,
                trades: int, win_rate_pct: float, mode: str = "paper",
                ts: int = 0) -> NotificationEvent:
    data = {
        "mode": mode, "final_equity": final_equity, "net_pnl": net_pnl,
        "net_pnl_pct": net_pnl_pct, "trades": trades, "win_rate_pct": win_rate_pct,
        "run_id": run_id,
    }
    return _finish("bot_stopped", "BOT DURDU", data, ts=ts, run_id=run_id)


def position_opened(*, run_id: str, pair: str, entry_price: float, qty: float, notional: float,
                    stop_price: float, tp_price: float, reason: str = "", ts: int = 0,
                    mode: str = "paper", timeframe: str = "", net_target_pct: Any = None,
                    stop_loss_pct: Any = None, indicators: Optional[Mapping[str, Any]] = None,
                    strategy_params: Optional[Mapping[str, Any]] = None,
                    volume_log_z: Any = None, history: Optional[Mapping[str, Any]] = None) -> NotificationEvent:
    data = {
        "mode": mode, "timeframe": timeframe, "entry_price": entry_price, "qty": qty,
        "notional": notional, "stop_price": stop_price, "tp_price": tp_price, "reason": reason,
        "net_target_pct": net_target_pct, "stop_loss_pct": stop_loss_pct,
        "volume_log_z": volume_log_z,
    }
    if indicators:
        data["indicators"] = dict(indicators)
    if strategy_params:
        data["strategy_params"] = dict(strategy_params)
    if history:
        data["history"] = dict(history)
    return _finish("position_opened", "POZISYON ACILDI: {}".format(_compact(pair)),
                   data, ts=ts, run_id=run_id, pair=pair)


def position_closed(*, run_id: str, pair: str, entry_price: float, exit_price: float, qty: float,
                    fees: float, gross_pnl: float, net_pnl: float, net_pnl_pct: float,
                    trigger: str = "", ts: int = 0, exit_reason: str = "", bars_held: Any = None,
                    held_seconds: Any = None, mode: str = "paper",
                    timeframe: str = "") -> NotificationEvent:
    data = {
        "mode": mode, "timeframe": timeframe, "entry_price": entry_price, "exit_price": exit_price,
        "qty": qty, "fees": fees, "gross_pnl": gross_pnl, "net_pnl": net_pnl,
        "net_pnl_pct": net_pnl_pct, "trigger": trigger, "exit_reason": exit_reason,
        "bars_held": bars_held, "held_seconds": held_seconds,
    }
    return _finish("position_closed", "POZISYON KAPANDI: {}".format(_compact(pair)),
                   data, ts=ts, run_id=run_id, pair=pair)


def take_profit_hit(*, run_id: str, pair: str, entry_price: float, exit_price: float,
                    net_pnl: float, net_pnl_pct: float, ts: int = 0, qty: Any = None,
                    fees: Any = None, gross_pnl: Any = None, exit_reason: str = "",
                    bars_held: Any = None, held_seconds: Any = None,
                    mode: str = "paper", timeframe: str = "") -> NotificationEvent:
    data = {
        "mode": mode, "timeframe": timeframe, "entry_price": entry_price, "exit_price": exit_price,
        "net_pnl": net_pnl, "net_pnl_pct": net_pnl_pct, "qty": qty, "fees": fees,
        "gross_pnl": gross_pnl, "exit_reason": exit_reason, "bars_held": bars_held,
        "held_seconds": held_seconds, "trigger": "take_profit",
    }
    return _finish("take_profit_hit", "TAKE-PROFIT: {}".format(_compact(pair)),
                   data, ts=ts, run_id=run_id, pair=pair)


def stop_loss_hit(*, run_id: str, pair: str, entry_price: float, exit_price: float,
                  net_pnl: float, net_pnl_pct: float, ts: int = 0, qty: Any = None,
                  fees: Any = None, gross_pnl: Any = None, exit_reason: str = "",
                  bars_held: Any = None, held_seconds: Any = None,
                  mode: str = "paper", timeframe: str = "") -> NotificationEvent:
    data = {
        "mode": mode, "timeframe": timeframe, "entry_price": entry_price, "exit_price": exit_price,
        "net_pnl": net_pnl, "net_pnl_pct": net_pnl_pct, "qty": qty, "fees": fees,
        "gross_pnl": gross_pnl, "exit_reason": exit_reason, "bars_held": bars_held,
        "held_seconds": held_seconds, "trigger": "stop_loss",
    }
    return _finish("stop_loss_hit", "STOP-LOSS: {}".format(_compact(pair)),
                   data, ts=ts, run_id=run_id, pair=pair)


def risk_halted(*, run_id: str, reason: str, day_realized_net_pnl: Any = None,
                ts: int = 0) -> NotificationEvent:
    data = {"reason": reason or "daily loss limit", "day_realized_net_pnl": day_realized_net_pnl}
    return _finish("risk_halted", "RISK DURDURDU: gunluk zarar limiti", data, ts=ts, run_id=run_id)


def cooldown_started(*, run_id: str, pair: str, net_pnl: float, cooldown_minutes: float,
                     ts: int = 0) -> NotificationEvent:
    data = {"net_pnl": net_pnl, "cooldown_minutes": cooldown_minutes}
    return _finish("cooldown_started", "COOLDOWN BASLADI: {}".format(_compact(pair)),
                   data, ts=ts, run_id=run_id, pair=pair)


def feed_outage(*, run_id: str, pair: str, error: str, detail: str = "",
                ts: int = 0) -> NotificationEvent:
    data = {"error": error or "feed error", "detail": detail or "veri yok"}
    return _finish("feed_outage", "VERI KESINTISI: {}".format(_compact(pair)),
                   data, ts=ts, run_id=run_id, pair=pair)


def data_fail_safe(*, run_id: str, reason: str, ts: int = 0) -> NotificationEvent:
    data = {"reason": reason or "eksik/bayat veri"}
    return _finish("data_fail_safe", "VERI FAIL-SAFE: yeni giris yok", data, ts=ts, run_id=run_id)


def daily_summary(*, run_id: str, day: str, equity: float, trades: int, wins: int,
                  win_rate_pct: float, net_pnl: float, ts: int = 0) -> NotificationEvent:
    data = {"day": day, "equity": equity, "trades": trades, "wins": wins,
            "win_rate_pct": win_rate_pct, "net_pnl": net_pnl}
    return _finish("daily_summary", "GUNLUK OZET {}".format(day), data, ts=ts, run_id=run_id)


def equity_drop(*, run_id: str, peak_equity: float, equity: float, drop_pct: float,
                threshold_pct: float, ts: int = 0) -> NotificationEvent:
    data = {"peak_equity": peak_equity, "equity": equity, "drop_pct": drop_pct,
            "threshold_pct": threshold_pct}
    return _finish("equity_drop", "EQUITY DUSUS UYARISI", data, ts=ts, run_id=run_id)


def test_event(*, message: str = "", dry_run: bool = False, ts: int = 0) -> NotificationEvent:
    data = {"message": message, "dry_run": bool(dry_run)}
    return _finish("test", "cryptobot test bildirimi" + (" (dry-run)" if dry_run else ""),
                   data, ts=ts)


__all__ = [
    "SEVERITIES", "SEVERITY_RANK", "EVENT_SEVERITIES", "EVENT_TYPES", "DEFAULT_NOTIFY_ON",
    "SUPERSEDED_BY",
    "SEVERITY_INFO", "SEVERITY_WARNING", "SEVERITY_CRITICAL",
    "NotificationEvent", "make_event", "severity_rank", "severity_at_least", "utc_now_ms",
    "bot_started", "bot_stopped", "position_opened", "position_closed", "take_profit_hit",
    "stop_loss_hit", "risk_halted", "cooldown_started", "feed_outage", "data_fail_safe",
    "daily_summary", "equity_drop", "test_event",
]
