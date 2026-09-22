"""Operator panel: one self-contained HTML view of notifications + trades.

``python cryptobot/scripts/paperbot.py panel`` renders every persisted signal the
bot produced -- the notification audit (``logs/notifications.jsonl``) and the
trading ledger (``data/ledger.sqlite``) -- into a single offline HTML file with
the overall success statistics, the trades table, the notification table and an
inline SVG equity curve.

The package is read-only over both stores: it never dispatches a notification,
never places an order and never writes to the ledger.

* :mod:`cryptobot.panel.model` -- reading + KPI computation
* :mod:`cryptobot.panel.render` -- the HTML/CSS/JS/SVG document
* :mod:`cryptobot.panel.server` -- optional ``--serve`` loopback viewer
"""

from __future__ import annotations

from .model import (
    DASH,
    AuditRead,
    LedgerRead,
    NotifyRow,
    PanelData,
    RunInfo,
    TradeRow,
    build_panel,
    close_kind,
    config_hash,
    event_label,
    reason_text,
    side_label,
    target_display,
    trade_status,
)
from .render import DEFAULT_LIMIT, GENERATED_MARKER, remote_references, render_panel, scheme_references
from .server import DEFAULT_PORT, HOST, PanelServer

__all__ = [
    "DASH", "DEFAULT_LIMIT", "DEFAULT_PORT", "GENERATED_MARKER", "HOST",
    "AuditRead", "LedgerRead", "NotifyRow", "PanelData", "PanelServer", "RunInfo", "TradeRow",
    "build_panel", "close_kind", "config_hash", "event_label", "reason_text",
    "remote_references", "render_panel", "scheme_references", "side_label",
    "target_display", "trade_status",
]
