"""Read-only data layer behind the operator panel (``paperbot.py panel``).

The panel answers one question in one place: *what did the bot produce, did it
succeed, and what is the overall score?*  It therefore joins the two stores the
project already persists:

* ``logs/notifications.jsonl`` -- the append-only notification audit
  (:class:`cryptobot.notify.store.NotificationStore`), one row per dispatch
  attempt, and
* ``data/ledger.sqlite`` -- the trading ledger (``runs``/``trades``/``ledger``/
  ``equity``/``orders``/``events``).

Two properties are non-negotiable here and shape every function below:

**Read-only.**  The SQLite file is opened with the ``mode=ro`` URI flag, so a
stray write is impossible even by accident; the audit file is only read.  The
panel never dispatches a notification, never places an order and never appends a
ledger row.

**Honest.**  A metric that cannot be computed is ``None`` (rendered as ``—``),
never a fabricated ``0``.  A store that could not be *read* is distinguished from
a store that is legitimately *empty* (:attr:`LedgerRead.ok` /
:attr:`AuditRead.exists`): an unreadable ledger reports ``—`` and a visible
warning, an empty one honestly reports zero rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..backtest.metrics import max_drawdown, profit_factor
from ..notify.guard import (
    REASON_HARNESS,
    REASON_OFFLINE,
    REASON_REPLAY,
    REASON_SUPERSEDED,
    target_host,
)
from ..notify.format import to_float
from ..notify.redact import redact
from ..notify.store import NotificationStore

#: Rendered instead of a value that cannot be computed.  Never ``0``: a missing
#: measurement must not look like a measurement.
DASH = "\u2014"

#: The four statuses a dispatch attempt can have (``notify.store.RECORD_FIELDS``).
NOTIFY_STATUSES: Tuple[str, ...] = ("sent", "suppressed", "failed", "dry_run")

#: Audit status -> Turkish label shown in the panel.
STATUS_LABELS: Dict[str, str] = {
    "sent": "gönderildi",
    "suppressed": "bastırıldı",
    "failed": "hata",
    "dry_run": "prova",
}

#: Suppression codes emitted by the dispatcher / guard -> readable Turkish.
#: Every code is kept verbatim as a secondary ``title`` in the table, so the
#: machine-readable identifier the logs use is never lost.
REASON_LABELS: Dict[str, str] = {
    "event_not_enabled": "kapsam dışı olay",
    "below_min_severity": "önem derecesi eşiğin altında",
    "quiet_hours": "sessiz saatler",
    "dedupe_window": "tekrar bastırıldı",
    "max_per_hour": "saatlik ağ bütçesi doldu",
    "no_active_provider": "etkin sağlayıcı yok",
    REASON_HARNESS: "test/doğrulama koruması",
    REASON_REPLAY: "replay modunda gönderim kapalı",
    REASON_OFFLINE: "offline modunda gönderim kapalı",
    REASON_SUPERSEDED: "kapanış bildirimi ile geçersiz kılındı",
}

#: Free-text failure reasons keep their prefix, translated, with the detail intact.
REASON_PREFIX_LABELS: Tuple[Tuple[str, str], ...] = (
    ("provider crashed:", "sağlayıcı çöktü"),
    ("notifier error:", "bildirim katmanı hatası"),
)

#: Event type -> Turkish label (the canonical code stays visible next to it).
EVENT_LABELS: Dict[str, str] = {
    "bot_started": "Bot başladı",
    "bot_stopped": "Bot durdu",
    "position_opened": "Pozisyon açıldı",
    "position_closed": "Pozisyon kapandı",
    "take_profit_hit": "Take-profit tetiklendi",
    "stop_loss_hit": "Stop-loss tetiklendi",
    "risk_halted": "Risk duruşu",
    "cooldown_started": "Bekleme (cooldown) başladı",
    "feed_outage": "Veri kesintisi",
    "data_fail_safe": "Veri hatası (fail-safe)",
    "daily_summary": "Günlük özet",
    "equity_drop": "Equity düşüşü",
    "test": "Test bildirimi",
}

#: Exit trigger (the ``exit_reason`` prefix written by ``risk.manager``) -> label.
CLOSE_KIND_LABELS: Dict[str, str] = {
    "take_profit": "Kâr al (take-profit)",
    "stop_loss": "Zarar durdur (stop-loss)",
    "strategy": "Strateji çıkışı",
    "time": "Süre sonu (time exit)",
    "timeout": "Süre sonu (time exit)",
    "time_exit": "Süre sonu (time exit)",
    "fail_safe": "Güvenli duruş (fail-safe)",
    "runner_stop": "Döngü durdu",
}

#: Side label for a ledger ``OPEN`` row (this project only ever opens long).
SIDE_LABELS: Dict[str, str] = {"buy": "Long (alış)", "sell": "Short (satış)"}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def trade_status(net_pnl: Any) -> Tuple[str, str]:
    """Success status of a closed trade -- the rule the UI legend documents.

    ``Kazanç`` for ``net_pnl > 0``, ``Zarar`` for ``net_pnl < 0`` and ``Başabaş``
    only for an **exact** ``0.0`` (no tolerance: a trade that really broke even is
    rare enough to be interesting).  An unparseable/absent value is
    ``bilinmiyor`` -- never silently counted as a win.
    """
    value = to_float(net_pnl)
    if value is None:
        return ("bilinmiyor", "Bilinmiyor")
    if value > 0:
        return ("kazanc", "Kazanç")
    if value < 0:
        return ("zarar", "Zarar")
    return ("basabas", "Başabaş")


def close_kind(exit_reason: Any) -> Tuple[str, str]:
    """``(code, label)`` for how a position was closed.

    The ledger writes ``<trigger>:<detail>`` (``stop_loss:stop-loss hit: ...``),
    so the trigger prefix is what classifies the exit; an unknown trigger is
    ``diger`` and stays fully visible in the row tooltip.
    """
    text = str(exit_reason or "").strip()
    if not text:
        return ("bilinmiyor", "Bilinmiyor")
    trigger = text.split(":", 1)[0].strip().lower()
    label = CLOSE_KIND_LABELS.get(trigger)
    if label:
        return (trigger, label)
    return ("diger", "Diğer")


def reason_text(reason: Any) -> Tuple[str, bool]:
    """``(readable, translated)`` for a suppression/failure reason.

    A known code becomes Turkish; an unknown one is passed through unchanged
    (never guessed).  ``translated`` tells the renderer whether showing the raw
    code as a secondary line adds information.
    """
    text = str(reason or "").strip()
    if not text:
        return ("", False)
    known = REASON_LABELS.get(text)
    if known is not None:
        return (known, True)
    for prefix, label in REASON_PREFIX_LABELS:
        if text.startswith(prefix):
            return ("{}: {}".format(label, text[len(prefix):].strip()), True)
    return (text, False)


def event_label(event: Any) -> str:
    """Turkish label of an event type; unknown types stay as their code."""
    text = str(event or "").strip()
    return EVENT_LABELS.get(text, text)


def side_label(side: Any) -> str:
    text = str(side or "").strip().lower()
    return SIDE_LABELS.get(text, DASH if not text else text)


def target_display(raw: Any) -> Tuple[str, str]:
    """``(host, redacted_target)`` for an audit row target.

    The stored target is redacted with the project's own helper first and only
    then reduced to its host, so neither the ntfy topic nor a Telegram bot token
    nor a webhook path can reach the panel.  A non-URL target (the ``file``
    provider stores a local path) is returned as-is, redacted.
    """
    if raw is None or str(raw).strip() == "":
        return ("", "")
    safe = redact(str(raw))
    if safe.startswith(("http://", "https://")):
        return (target_host(safe) or "", safe)
    return (safe, safe)


def config_hash(config: Mapping[str, Any]) -> Optional[str]:
    """Stable 12-hex digest of a run's stored config (``None`` when absent)."""
    if not config:
        return None
    payload = json.dumps(dict(config), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _int(value: Any) -> Optional[int]:
    parsed = to_float(value)
    return None if parsed is None else int(parsed)


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _safe(value: Any) -> str:
    """A data-derived string, run through the project's redaction helper.

    Every string the panel *shows* comes from a store the bot wrote (an audit
    field, a ledger reason, a run id, a pair).  Redacting them at this boundary --
    not only the obvious title/body/target -- is what makes "no secret in the
    file" a property of the data path instead of a property of the template.
    """
    return redact(_str(value))


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RunInfo:
    """One ledger run, as far as it is known."""

    run_id: str
    mode: str
    started_ts: Optional[int]
    started_iso: str
    version: str
    timeframe: str
    pairs: Tuple[str, ...]
    initial_capital: Optional[float]
    cfg_hash: Optional[str]
    trades_closed: int
    net_pnl: Optional[float]
    in_runs_table: bool


@dataclass(frozen=True)
class TradeRow:
    """One round trip (closed) or one still-open position."""

    trade_id: Optional[int]
    run_id: str
    pair: str
    side: str
    entry_ts: Optional[int]
    exit_ts: Optional[int]
    entry_iso: str
    exit_iso: str
    entry_price: Optional[float]
    exit_price: Optional[float]
    qty: Optional[float]
    fees: Optional[float]
    gross_pnl: Optional[float]
    net_pnl: Optional[float]
    net_pnl_pct: Optional[float]
    slippage: Optional[float]
    exit_reason: str
    status_code: str
    status_label: str
    close_code: str
    close_label: str
    holding_seconds: Optional[float]
    is_open: bool


@dataclass(frozen=True)
class NotifyRow:
    """One dispatch attempt from the audit store."""

    seq: int
    ts: Optional[int]
    iso: str
    event: str
    event_label: str
    severity: str
    provider: str
    status: str
    status_label: str
    reason: str
    reason_label: str
    reason_translated: bool
    http_status: Optional[int]
    latency_ms: Optional[float]
    target_host: str
    target_full: str
    title: str
    body_excerpt: str
    run_id: str


@dataclass(frozen=True)
class LedgerRead:
    """Result of opening/reading the ledger (never raises)."""

    path: Path
    exists: bool
    ok: bool
    error: str = ""
    tables: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditRead:
    """Result of reading the audit store (never raises)."""

    path: Path
    exists: bool
    ok: bool
    error: str = ""
    lines: int = 0
    parsed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class PanelData:
    """Everything the renderer needs -- complete, un-truncated, already redacted.

    ``trades``/``notifications`` hold **every** row of the selection; ``--limit``
    is applied by the renderer for display only, so the KPI block can never be
    computed from a truncated table.
    """

    generated_iso: str
    selection: str
    selection_label: str
    runs: Tuple[RunInfo, ...]
    trades: Tuple[TradeRow, ...]
    open_positions: Tuple[TradeRow, ...]
    notifications: Tuple[NotifyRow, ...]
    stats: Mapping[str, Any]
    equity_series: Tuple[Tuple[int, float], ...]
    drawdown_basis: str
    drawdown_basis_label: str
    warnings: Tuple[str, ...]
    ledger: LedgerRead
    audit: AuditRead

    @property
    def limits_note(self) -> str:  # pragma: no cover - convenience for callers
        return "selection={} runs={} trades={} notifications={}".format(
            self.selection, len(self.runs), len(self.trades), len(self.notifications))


# --------------------------------------------------------------------------- #
# ledger access (read-only)
# --------------------------------------------------------------------------- #
_TRADE_COLUMNS = (
    "id", "run_id", "position_id", "pair", "entry_ts", "exit_ts",
    "entry_iso_utc", "exit_iso_utc", "entry_fill_price", "exit_fill_price",
    "qty", "fees", "gross_pnl", "net_pnl", "net_pnl_pct", "slippage_cost",
    "entry_reason", "exit_reason",
)


class _LedgerReader:
    """Read-only SQLite accessor that degrades instead of raising."""

    def __init__(self, path: Path, run_ids: Optional[Sequence[str]], warnings: List[str]) -> None:
        self.path = Path(path)
        self.run_ids = list(run_ids) if run_ids is not None else None
        self.warnings: List[str] = warnings
        self.error = ""
        self.tables: Tuple[str, ...] = ()
        self.exists = self.path.exists()
        self._conn: Optional[sqlite3.Connection] = None

    # -- connection ---------------------------------------------------------
    def __enter__(self) -> "_LedgerReader":
        if not self.exists:
            self.error = "defter dosyası yok: {}".format(self.path)
            return self
        try:
            self._conn = sqlite3.connect(
                "file:{}?mode=ro".format(self.path.resolve().as_posix()), uri=True)
            self._conn.row_factory = sqlite3.Row
            self.tables = tuple(sorted(
                row[0] for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")))
        except sqlite3.Error as exc:
            self.error = "{}: {}".format(type(exc).__name__, exc)
            self._close()
        return self

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()  # Windows keeps the file locked otherwise
            except sqlite3.Error:  # pragma: no cover - close must not mask a result
                pass
            self._conn = None

    def __exit__(self, *exc: Any) -> None:
        self._close()

    @property
    def ok(self) -> bool:
        return self._conn is not None and self.error == "" and self.exists

    # -- queries ------------------------------------------------------------
    def runs_clause(self, column: str) -> Tuple[str, List[Any]]:
        """``AND <column> IN (...)`` for the selected runs (``""`` when filtering all)."""
        if self.run_ids is None:
            return ("", [])
        if not self.run_ids:
            return (" AND 1 = 0", [])
        return (" AND {0} IN ({1})".format(column, ",".join("?" * len(self.run_ids))),
                list(self.run_ids))

    def query(self, table: str, columns: Sequence[str], *, order_by: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """``SELECT`` a table (optionally filtered to the selected runs)."""
        if self._conn is None or table not in self.tables:
            return []
        clause, params = self.runs_clause("run_id")
        sql = "SELECT {0} FROM {1} WHERE 1 = 1{2}".format(", ".join(columns), table, clause)
        if order_by:
            sql += " ORDER BY " + ", ".join(order_by)
        try:
            return [dict(row) for row in self._conn.execute(sql, params)]
        except sqlite3.Error as exc:
            self.warnings.append("defter okunamadı ({}): {}: {}".format(table, type(exc).__name__, exc))
            return []

    def query_raw(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        try:
            return [dict(row) for row in self._conn.execute(sql, params)]
        except sqlite3.Error as exc:
            self.warnings.append("defter sorgusu başarısız: {}: {}".format(type(exc).__name__, exc))
            return []

    def trade_rows(self) -> List[Dict[str, Any]]:
        return self.query("trades", _TRADE_COLUMNS, order_by=("exit_ts", "id"))


# --------------------------------------------------------------------------- #
# selection / runs
# --------------------------------------------------------------------------- #
def _run_ids_from_ledger(reader: _LedgerReader) -> List[str]:
    """Every run id the ledger mentions, even without a ``runs`` row."""
    ids: Dict[str, None] = {}
    for table in ("runs", "trades", "ledger", "equity", "events", "orders"):
        if table not in reader.tables:
            continue
        column = "run_id"
        for row in reader.query_raw("SELECT DISTINCT {0} AS run_id FROM {1}".format(column, table)):
            value = _str(row.get("run_id")).strip()
            if value:
                ids.setdefault(value, None)
    return sorted(ids)


def _load_runs(reader: _LedgerReader, warnings: List[str]) -> List[RunInfo]:
    run_ids = _run_ids_from_ledger(reader)
    if reader.run_ids is not None:
        run_ids = [rid for rid in run_ids if rid in set(reader.run_ids)] or list(reader.run_ids)

    rows = reader.query("runs", ("run_id", "mode", "started_at", "started_iso", "version",
                                 "config_json", "data_json"), order_by=("started_at", "run_id"))
    known = {_str(row.get("run_id")): row for row in rows}

    trades_per_run: Dict[str, List[Dict[str, Any]]] = {}
    for row in reader.trade_rows():
        if row.get("exit_ts") is None:
            continue
        trades_per_run.setdefault(_str(row.get("run_id")), []).append(row)

    infos: List[RunInfo] = []
    for run_id in sorted(set(run_ids) | set(known)):
        row = known.get(run_id)
        config: Dict[str, Any] = {}
        data_summary = ""
        if row:
            try:
                parsed = json.loads(_str(row.get("config_json")) or "{}")
                config = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError) as exc:
                warnings.append("koşu {} config_json okunamadı: {}".format(run_id, exc))
            try:
                data = json.loads(_str(row.get("data_json")) or "{}")
                if isinstance(data, dict):
                    data_summary = ", ".join(
                        "{}: {} bar".format(key, (value or {}).get("rows", "?"))
                        for key, value in sorted(data.items())
                        if isinstance(value, dict) and "rows" in value)
            except (TypeError, ValueError):
                data_summary = ""
        closed = trades_per_run.get(run_id, [])
        pnls = [to_float(item.get("net_pnl")) for item in closed]
        usable = [value for value in pnls if value is not None]
        pairs = config.get("pairs") if isinstance(config.get("pairs"), list) else []
        infos.append(RunInfo(
            run_id=_safe(run_id),
            mode=_safe(config.get("mode") or (row or {}).get("mode")),
            started_ts=_int((row or {}).get("started_at")),
            started_iso=_safe((row or {}).get("started_iso")),
            version=_safe((row or {}).get("version")),
            timeframe=_safe(config.get("timeframe")),
            pairs=tuple(_safe(pair) for pair in pairs),
            initial_capital=to_float(config.get("initial_capital_usdt")),
            cfg_hash=config_hash(config),
            trades_closed=len(closed),
            net_pnl=sum(usable) if usable else None,
            in_runs_table=row is not None,
        ))
    if reader.run_ids is not None:
        wanted = set(reader.run_ids)
        found = {info.run_id for info in infos if info.in_runs_table}
        for rid in sorted(wanted - found):
            warnings.append("seçili koşu 'runs' tablosunda yok (defterde başka kaydı olabilir): {}".format(rid))
    return infos


# --------------------------------------------------------------------------- #
# trades
# --------------------------------------------------------------------------- #
def _trade_from_row(row: Mapping[str, Any], *, side: str, is_open: bool) -> TradeRow:
    entry_ts = _int(row.get("entry_ts"))
    exit_ts = _int(row.get("exit_ts"))
    status_code, status_line = trade_status(row.get("net_pnl"))
    if is_open:
        status_code, status_line = ("acik", "Açık")
    close_code, close_line = close_kind(row.get("exit_reason"))
    holding = None
    if entry_ts is not None and exit_ts is not None:
        holding = max(0.0, (exit_ts - entry_ts) / 1000.0)
    return TradeRow(
        trade_id=_int(row.get("id")),
        run_id=_safe(row.get("run_id")),
        pair=_safe(row.get("pair")),
        side=side,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_iso=_safe(row.get("entry_iso_utc")),
        exit_iso=_safe(row.get("exit_iso_utc")),
        entry_price=to_float(row.get("entry_fill_price")),
        exit_price=to_float(row.get("exit_fill_price")),
        qty=to_float(row.get("qty")),
        fees=to_float(row.get("fees")),
        gross_pnl=to_float(row.get("gross_pnl")),
        net_pnl=to_float(row.get("net_pnl")),
        net_pnl_pct=to_float(row.get("net_pnl_pct")),
        slippage=to_float(row.get("slippage_cost")),
        exit_reason=_safe(row.get("exit_reason")),
        status_code=status_code,
        status_label=status_line,
        close_code=close_code,
        close_label=close_line,
        holding_seconds=holding,
        is_open=is_open,
    )


def _load_trades(reader: _LedgerReader) -> Tuple[List[TradeRow], List[TradeRow]]:
    """Closed round trips + still-open positions, each with its side from the ledger.

    ``trades`` carries no side column, so the position direction is joined from
    the ``ledger`` ``OPEN`` row of the same ``(run_id, position_id)`` (lowest row
    id wins, so the join is stable).  Positions the ``trades`` table does not
    carry yet -- an OPEN with no matching CLOSE -- are reconstructed from the
    ledger and marked as open.
    """
    closed_raw = [row for row in reader.trade_rows() if row.get("exit_ts") is not None]
    open_raw = [row for row in reader.query("trades", _TRADE_COLUMNS, order_by=("id",))
                if row.get("exit_ts") is None]

    sides: Dict[str, str] = {}
    for row in reader.query("ledger", ("run_id", "position_id", "side", "event"), order_by=("id",)):
        if _str(row.get("event")) == "OPEN":
            key = "{}|{}".format(_str(row.get("run_id")), _str(row.get("position_id")))
            sides.setdefault(key, _str(row.get("side")).lower())

    def side_of(row: Mapping[str, Any]) -> str:
        return sides.get("{}|{}".format(_str(row.get("run_id")), _str(row.get("position_id"))), "")

    closed = [_trade_from_row(row, side=side_of(row), is_open=False) for row in closed_raw]
    open_rows = [_trade_from_row(row, side=side_of(row), is_open=True) for row in open_raw]

    # Positions the trades table does not carry yet: OPEN without CLOSE.
    covered = {"{}|{}".format(_str(row.get("run_id")), _str(row.get("position_id")))
               for row in open_raw}
    clause, params = reader.runs_clause("o.run_id")
    sql = (
        "SELECT o.run_id, o.position_id, o.pair, o.side, o.ts, o.iso_utc, o.fill_price, o.qty, "
        "o.fee, o.reason FROM ledger o WHERE o.event = 'OPEN'{0} AND NOT EXISTS ("
        "SELECT 1 FROM ledger c WHERE c.event = 'CLOSE' AND c.run_id = o.run_id "
        "AND c.position_id = o.position_id) ORDER BY o.run_id, o.ts, o.id".format(clause)
    )
    for row in reader.query_raw(sql, params):
        key = "{}|{}".format(_str(row.get("run_id")), _str(row.get("position_id")))
        if key in covered:
            continue
        covered.add(key)
        open_rows.append(TradeRow(
            trade_id=None,
            run_id=_str(row.get("run_id")),
            pair=_str(row.get("pair")),
            side=_str(row.get("side")).lower(),
            entry_ts=_int(row.get("ts")),
            exit_ts=None,
            entry_iso=_str(row.get("iso_utc")),
            exit_iso="",
            entry_price=to_float(row.get("fill_price")),
            exit_price=None,
            qty=to_float(row.get("qty")),
            fees=to_float(row.get("fee")),
            gross_pnl=None,
            net_pnl=None,
            net_pnl_pct=None,
            slippage=None,
            exit_reason="",
            status_code="acik",
            status_label="Açık",
            close_code="acik",
            close_label="Açık",
            holding_seconds=None,
            is_open=True,
        ))
    open_rows.sort(key=lambda item: (item.run_id, item.entry_ts or 0, item.trade_id or 0))
    return closed, open_rows


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
def _notify_from_record(record: Mapping[str, Any], seq: int) -> NotifyRow:
    status = _str(record.get("status")).strip()
    raw_reason = _str(record.get("reason"))
    readable, translated = reason_text(raw_reason)
    host, full = target_display(record.get("target"))
    body = redact(_str(record.get("body")))
    body = " ".join(body.split())
    if len(body) > 240:
        body = body[:240] + "…"
    return NotifyRow(
        seq=seq,
        ts=_int(record.get("ts")),
        iso=_safe(record.get("iso_utc")),
        event=_safe(record.get("event")),
        event_label=event_label(_safe(record.get("event"))),
        severity=_safe(record.get("severity")),
        provider=_safe(record.get("provider")),
        status=status,
        status_label=STATUS_LABELS.get(status, status or DASH),
        reason=redact(raw_reason),
        reason_label=readable,
        reason_translated=translated,
        http_status=_int(record.get("http_status")),
        latency_ms=to_float(record.get("latency_ms")),
        target_host=host,
        target_full=full,
        title=redact(_str(record.get("title"))),
        body_excerpt=body,
        run_id=_safe(record.get("run_id")),
    )


def _load_audit(path: Path, run_id: Optional[str]) -> Tuple[AuditRead, List[NotifyRow]]:
    """Read the audit store; a missing file is empty, a broken one is reported."""
    store = NotificationStore(path)
    if not store.exists():
        return (AuditRead(path=path, exists=False, ok=True), [])
    try:
        records = store.read_all()
    except OSError as exc:  # pragma: no cover - the store already swallows these
        return (AuditRead(path=path, exists=True, ok=False, error=str(exc)), [])
    lines = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    lines += 1
    except OSError as exc:
        return (AuditRead(path=path, exists=True, ok=False, error=str(exc)), [])
    rows: List[NotifyRow] = []
    for index, record in enumerate(records):
        if run_id is not None and _str(record.get("run_id")) != run_id:
            continue
        rows.append(_notify_from_record(record, index))
    info = AuditRead(path=path, exists=True, ok=True, lines=lines, parsed=len(records),
                     skipped=max(0, lines - len(records)))
    return (info, rows)


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #
def _notification_stats(rows: Sequence[NotifyRow], audit: AuditRead) -> Dict[str, Any]:
    counts = {status: 0 for status in NOTIFY_STATUSES}
    other = 0
    for row in rows:
        if row.status in counts:
            counts[row.status] += 1
        else:
            other += 1
    total = len(rows) + other
    sent, failed = counts["sent"], counts["failed"]
    attempted = sent + failed
    latencies = [row.latency_ms for row in rows if row.latency_ms is not None]
    severities: Dict[str, int] = {}
    for row in rows:
        key = row.severity or "(yok)"
        severities[key] = severities.get(key, 0) + 1
    per_event: Dict[str, Dict[str, int]] = {}
    for row in rows:
        bucket = per_event.setdefault(row.event or "(yok)",
                                      {status: 0 for status in NOTIFY_STATUSES})
        if row.status in bucket:
            bucket[row.status] += 1
    breakdown = [(name, bucket) for name, bucket in
                 sorted(per_event.items(), key=lambda item: (-sum(item[1].values()), item[0]))]
    return {
        "readable": audit.ok,
        "exists": audit.exists,
        "file_lines": audit.lines,
        "total": total,
        "other_status": other,
        "sent": counts["sent"],
        "suppressed": counts["suppressed"],
        "failed": counts["failed"],
        "dry_run": counts["dry_run"],
        "attempted": attempted,
        # Defined in the legend as well: of the attempts actually handed to a
        # provider (sent + failed), how many came back ok.
        "delivery_rate_pct": (sent / attempted * 100.0) if attempted else None,
        "suppress_rate_pct": (counts["suppressed"] / total * 100.0) if total else None,
        "failure_rate_pct": (failed / attempted * 100.0) if attempted else None,
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
        "severities": dict(sorted(severities.items())),
        "by_event": breakdown,
        "parsed": audit.parsed,
        "skipped_lines": audit.skipped,
    }


def _equity_series(reader: _LedgerReader, runs: Sequence[RunInfo],
                   closed: Sequence[TradeRow]) -> Tuple[List[Tuple[int, float]], str, List[float]]:
    """The curve the chart and the drawdown metric both use.

    * one selected run with ledger equity rows -> the real equity series
      (``cash + positions``), the strongest available basis;
    * otherwise -> the cumulative **realized** net PnL of the closed trades, in
      exit order, anchored at the summed initial capital of the known runs.
      Concatenating several runs' equity timelines would invent drawdowns that
      never happened, which is why the fallback is realized PnL instead.
    """
    single = len(runs) == 1 and runs[0].run_id
    if single:
        rows = reader.query("equity", ("ts", "equity"), order_by=("ts", "id"))
        points = [(_int(row.get("ts")) or 0, to_float(row.get("equity")))
                  for row in rows if to_float(row.get("equity")) is not None]
        if len(points) >= 2:
            return (points, "ledger_equity",
                    [value for _ts, value in points])
    capital = sum(info.initial_capital or 0.0 for info in runs if info.initial_capital is not None)
    ordered = sorted((row for row in closed if row.exit_ts is not None and row.net_pnl is not None),
                     key=lambda row: (row.exit_ts or 0, row.trade_id or 0))
    if not ordered:
        return ([], "empty", [])
    # Anchor the curve at the initial capital *before* the first trade: equity was
    # really at that level, so a loss on the first trades is a real drawdown and the
    # chart starts on the capital line instead of at the first exit.
    first_exit = ordered[0].exit_ts or 0
    entries = [row.entry_ts for row in ordered if row.entry_ts is not None]
    anchor_ts = min(entries) if entries else first_exit
    series: List[Tuple[int, float]] = [(min(anchor_ts, first_exit), capital)]
    values: List[float] = [capital]
    running = capital
    for row in ordered:
        running += row.net_pnl or 0.0
        series.append((row.exit_ts or 0, running))
        values.append(running)
    return (series, "realized_cumulative", values)


def _trade_stats(closed: Sequence[TradeRow], open_positions: Sequence[TradeRow],
                 runs: Sequence[RunInfo], values: Sequence[float], basis: str,
                 readable: bool) -> Dict[str, Any]:
    pnls = [row.net_pnl for row in closed if row.net_pnl is not None]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    flats = [value for value in pnls if value == 0]
    gross = [row.gross_pnl for row in closed if row.gross_pnl is not None]
    fees = [row.fees for row in closed if row.fees is not None]
    slippage = [row.slippage for row in closed if row.slippage is not None]
    holdings = [row.holding_seconds for row in closed if row.holding_seconds is not None]
    capital = sum(info.initial_capital or 0.0 for info in runs if info.initial_capital is not None)
    has_capital = any(info.initial_capital is not None for info in runs)

    net_total = sum(pnls) if pnls else None
    factor = profit_factor(pnls) if pnls else None
    best = max(closed, key=lambda row: (row.net_pnl or 0.0, row.trade_id or 0)) if closed else None
    worst = min(closed, key=lambda row: (row.net_pnl or 0.0, row.trade_id or 0)) if closed else None
    pct_values = [row.net_pnl_pct for row in closed if row.net_pnl_pct is not None]

    drawdown = max_drawdown(list(values)) if values else None
    dd_pct = drawdown["pct"] if drawdown else None
    dd_usdt = drawdown["usdt"] if drawdown else None
    if drawdown and values and max(values) <= 0:
        # A drawdown relative to a non-positive peak is undefined: report "—".
        dd_pct = None

    return {
        "readable": readable,
        "opened": len(closed) + len(open_positions),
        "closed": len(closed),
        "open": len(open_positions),
        "winning": len(wins),
        "losing": len(losses),
        "breakeven": len(flats),
        "unknown_pnl": len(closed) - len(pnls),
        "win_rate_pct": (len(wins) / len(pnls) * 100.0) if pnls else None,
        "net_pnl_usdt": net_total,
        "net_pnl_pct": (net_total / capital * 100.0) if (net_total is not None and capital) else None,
        "capital_basis_usdt": capital if has_capital else None,
        "capital_known": has_capital,
        "gross_pnl_usdt": sum(gross) if gross else None,
        "fees_usdt": sum(fees) if fees else None,
        "slippage_usdt": sum(slippage) if slippage else None,
        "avg_net_pnl_usdt": (sum(pnls) / len(pnls)) if pnls else None,
        "avg_net_pnl_pct": (sum(pct_values) / len(pct_values)) if pct_values else None,
        "best": best,
        "worst": worst,
        "profit_factor": factor,
        "max_drawdown_usdt": dd_usdt,
        "max_drawdown_pct": dd_pct,
        "avg_holding_seconds": (sum(holdings) / len(holdings)) if holdings else None,
        "drawdown_basis": basis,
    }


def compute_stats(closed: Sequence[TradeRow], open_positions: Sequence[TradeRow],
                  notifications: Sequence[NotifyRow], runs: Sequence[RunInfo],
                  values: Sequence[float], basis: str, audit: AuditRead,
                  *, ledger_readable: bool = True) -> Dict[str, Any]:
    """The KPI block: notification success + trading outcome, ``None`` where unknown."""
    return {
        "notifications": _notification_stats(notifications, audit),
        "trades": _trade_stats(closed, open_positions, runs, values, basis, ledger_readable),
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def build_panel(*, db_path: Path, audit_path: Path, run_id: Optional[str] = None,
                now: Optional[datetime] = None) -> PanelData:
    """Read both stores and return the complete, un-truncated panel data.

    Never raises for missing/corrupt/empty inputs: a warning tuple travels with
    the data and the renderer surfaces it.  ``db_path``/``audit_path`` are read
    only.
    """
    warnings: List[str] = []
    stamp = now or datetime.now(timezone.utc)
    generated = stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    reader = _LedgerReader(db_path, [run_id] if run_id else None, warnings)
    with reader:
        ledger = LedgerRead(path=Path(db_path), exists=reader.exists, ok=reader.ok,
                            error=reader.error, tables=reader.tables)
        if reader.exists and not reader.ok:
            warnings.append("defter açılamadı ({}): {}".format(reader.path, reader.error or "bilinmeyen hata"))
        elif not reader.exists:
            warnings.append("defter dosyası yok: {}".format(db_path))
        runs = _load_runs(reader, warnings)
        closed, open_positions = _load_trades(reader)
        series, basis, values = _equity_series(reader, runs, closed)

    audit, notifications = _load_audit(Path(audit_path), run_id)
    if not audit.exists:
        warnings.append("bildirim denetim dosyası yok: {}".format(audit.path))
    elif not audit.ok:
        warnings.append("denetim dosyası okunamadı: {}".format(audit.error))
    elif audit.skipped:
        warnings.append("denetim dosyasında {} bozuk/kesik satır atlandı".format(audit.skipped))

    stats = compute_stats(closed, open_positions, notifications, runs, values, basis, audit,
                          ledger_readable=ledger.ok)

    return PanelData(
        generated_iso=generated,
        selection=_safe(run_id or ""),
        selection_label=_safe(run_id) if run_id else "tüm koşular",
        runs=tuple(runs),
        trades=tuple(closed),
        open_positions=tuple(open_positions),
        notifications=tuple(notifications),
        stats=stats,
        equity_series=tuple(series),
        drawdown_basis=basis,
        drawdown_basis_label=(
            "defter equity eğrisi (nakit + açık pozisyon değeri)" if basis == "ledger_equity"
            else "realize net PnL eğrisi (başlangıç sermayesinden kümülatif)" if basis == "realized_cumulative"
            else "veri yok"),
        warnings=tuple(redact(warning) for warning in warnings),
        ledger=ledger,
        audit=audit,
    )


__all__ = [
    "DASH", "NOTIFY_STATUSES", "STATUS_LABELS", "REASON_LABELS", "EVENT_LABELS",
    "CLOSE_KIND_LABELS", "SIDE_LABELS",
    "AuditRead", "LedgerRead", "NotifyRow", "PanelData", "RunInfo", "TradeRow",
    "build_panel", "close_kind", "compute_stats", "config_hash", "event_label",
    "reason_text", "side_label", "target_display", "trade_status",
]
