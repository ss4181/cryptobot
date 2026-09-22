"""SQLite ledger: every simulated open/close, order rejection, equity point and risk event.

Tables
------
``runs``    one row per process run (mode, config echo, data echo).
``ledger``  one row per *fill event*: an ``OPEN`` or a ``CLOSE`` row carrying the
            timestamp, pair, side, reference/fill price, quantity, fee, and -- for
            closes -- gross and net PnL.
``trades``  one row per closed round trip (entry + exit in the same row).
``orders``  rejected / partially filled orders and why.
``equity``  equity snapshots over time.
``events``  structured risk/ops events (limits hit, halts, feed outages...).

:meth:`Ledger.verify` recomputes cash and realized PnL from the ledger and
compares them with the broker's live numbers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

log = logging.getLogger(__name__)

DEFAULT_TOLERANCE = 1e-6

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    mode          TEXT NOT NULL,
    started_at    INTEGER NOT NULL,
    started_iso   TEXT NOT NULL,
    version       TEXT,
    config_json   TEXT,
    data_json     TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    position_id     TEXT,
    event           TEXT NOT NULL,          -- OPEN | CLOSE
    ts              INTEGER NOT NULL,
    iso_utc         TEXT NOT NULL,
    pair            TEXT NOT NULL,
    side            TEXT NOT NULL,          -- buy | sell
    reference_price REAL,
    fill_price      REAL,
    qty             REAL,
    notional        REAL,
    fee             REAL,
    slippage_cost   REAL,
    gross_pnl       REAL,
    net_pnl         REAL,
    net_pnl_pct     REAL,
    reason          TEXT,
    mode            TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    position_id     TEXT,
    pair            TEXT NOT NULL,
    entry_ts        INTEGER,
    exit_ts         INTEGER,
    entry_iso_utc   TEXT,
    exit_iso_utc    TEXT,
    entry_reference_price REAL,
    entry_fill_price      REAL,
    exit_reference_price  REAL,
    exit_fill_price       REAL,
    qty             REAL,
    entry_fee       REAL,
    exit_fee        REAL,
    fees            REAL,
    gross_pnl       REAL,
    net_pnl         REAL,
    net_pnl_pct     REAL,
    gross_pnl_pct   REAL,
    slippage_cost   REAL,
    entry_reason    TEXT,
    exit_reason     TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    iso_utc      TEXT NOT NULL,
    pair         TEXT,
    side         TEXT,
    status       TEXT NOT NULL,            -- rejected | partial
    requested_qty REAL,
    filled_qty   REAL,
    reference_price REAL,
    reason       TEXT,
    mode         TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    iso_utc         TEXT NOT NULL,
    cash            REAL,
    positions_value REAL,
    equity          REAL,
    open_positions  INTEGER,
    realized_net_pnl REAL
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    iso_utc      TEXT NOT NULL,
    level        TEXT,
    category     TEXT,
    code         TEXT,
    message      TEXT,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_run ON ledger(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_equity_run ON equity(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
"""


def iso_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class VerifyReport:
    """Result of reconciling the ledger against the broker."""

    ok: bool
    tolerance: float
    checks: List[Dict[str, Any]]
    notes: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "tolerance": self.tolerance, "checks": self.checks, "notes": self.notes}

    def render(self) -> str:
        lines = ["Ledger reconciliation: {}".format("PASS" if self.ok else "FAIL")]
        for check in self.checks:
            lines.append(
                "  [{}] {:<28} ledger={!r:<22} broker={!r:<22} delta={!r}".format(
                    "ok" if check["ok"] else "FAIL",
                    check["name"],
                    check.get("ledger"),
                    check.get("broker"),
                    check.get("delta"),
                )
            )
        for note in self.notes:
            lines.append("  note: {}".format(note))
        return "\n".join(lines)


class Ledger:
    """Thin, explicit wrapper around a SQLite file.

    Writes are committed in batches (``commit_every`` statements) because each
    commit costs a disk sync: one commit per equity snapshot made a 1000-bar
    replay spend 6 s in ``sqlite3.commit``.  Reads on the same connection see
    uncommitted rows, and :meth:`flush`/:meth:`close` guarantee durability at
    every reporting boundary.
    """

    def __init__(self, path: Path | str, run_id: str, *, commit_every: int = 200) -> None:
        self.path = Path(path)
        self.run_id = str(run_id)
        self.commit_every = max(1, int(commit_every))
        self._pending = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ basics
    def flush(self) -> None:
        """Commit any pending writes."""
        if self._pending:
            self._conn.commit()
            self._pending = 0

    def close(self) -> None:
        try:
            self.flush()
            self._conn.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, tuple(params))
        self._pending += 1
        if self._pending >= self.commit_every:
            self.flush()
        return cursor

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, tuple(params)).fetchall()]

    # ------------------------------------------------------------------- write
    def start_run(self, *, mode: str, started_at: int, version: str,
                  config: Mapping[str, Any], data: Mapping[str, Any]) -> None:
        """Register the run, replacing any previous rows with the same run id.

        Re-using a run id (``--run-id``) must not silently duplicate ledger rows,
        which would break :meth:`verify`; the new run supersedes the old one.

        When the ledger is held by another process (``database is locked``) the
        connection is released before the error propagates: the run never started,
        and on Windows a half-open handle would keep the file undeletable for the
        retry the operator was just told to make.
        """
        try:
            existing = self._query("SELECT run_id FROM runs WHERE run_id = ?", (self.run_id,))
            if existing:
                log.warning("ledger.run_id_reused",
                            extra={"event": "run_id_reused", "run_id": self.run_id,
                                   "note": "previous rows for this run id are replaced"})
                for table in ("ledger", "trades", "orders", "equity", "events"):
                    self._exec("DELETE FROM {} WHERE run_id = ?".format(table), (self.run_id,))
            self._exec(
                "INSERT OR REPLACE INTO runs (run_id, mode, started_at, started_iso, version, config_json, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.run_id, mode, int(started_at), iso_utc(started_at), version,
                 json.dumps(dict(config), sort_keys=True), json.dumps(dict(data), sort_keys=True)),
            )
        except sqlite3.OperationalError:
            self.close()
            raise

    def record_open(self, result: Any, *, mode: str) -> None:
        self._exec(
            "INSERT INTO ledger (run_id, position_id, event, ts, iso_utc, pair, side, reference_price, "
            "fill_price, qty, notional, fee, slippage_cost, gross_pnl, net_pnl, net_pnl_pct, reason, mode) "
            "VALUES (?, ?, 'OPEN', ?, ?, ?, 'buy', ?, ?, ?, ?, ?, ?, 0.0, 0.0, 0.0, ?, ?)",
            (self.run_id, result.position_id, int(result.ts), iso_utc(result.ts), result.pair,
             result.reference_price, result.fill_price, result.filled_qty, result.notional,
             result.fee, 0.0, result.reason, mode),
        )

    def record_close(self, result: Any, trade: Mapping[str, Any], *, mode: str) -> None:
        self._exec(
            "INSERT INTO ledger (run_id, position_id, event, ts, iso_utc, pair, side, reference_price, "
            "fill_price, qty, notional, fee, slippage_cost, gross_pnl, net_pnl, net_pnl_pct, reason, mode) "
            "VALUES (?, ?, 'CLOSE', ?, ?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, result.position_id, int(result.ts), iso_utc(result.ts), result.pair,
             result.reference_price, result.fill_price, result.filled_qty, result.notional,
             result.fee, result.slippage_cost, trade.get("gross_pnl", 0.0), trade.get("net_pnl", 0.0),
             trade.get("net_pnl_pct", 0.0), result.reason, mode),
        )
        self._exec(
            "INSERT INTO trades (run_id, position_id, pair, entry_ts, exit_ts, entry_iso_utc, exit_iso_utc, "
            "entry_reference_price, entry_fill_price, exit_reference_price, exit_fill_price, qty, entry_fee, "
            "exit_fee, fees, gross_pnl, net_pnl, net_pnl_pct, gross_pnl_pct, slippage_cost, entry_reason, exit_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, trade.get("position_id"), trade.get("pair"), trade.get("entry_ts"), trade.get("exit_ts"),
             iso_utc(trade.get("entry_ts") or 0), iso_utc(trade.get("exit_ts") or 0),
             trade.get("entry_reference_price"), trade.get("entry_fill_price"),
             trade.get("exit_reference_price"), trade.get("exit_fill_price"), trade.get("qty"),
             trade.get("entry_fee"), trade.get("exit_fee"), trade.get("fees"), trade.get("gross_pnl"),
             trade.get("net_pnl"), trade.get("net_pnl_pct"), trade.get("gross_pnl_pct"),
             trade.get("slippage_cost"), trade.get("entry_reason"), trade.get("exit_reason")),
        )

    def record_order(self, result: Any, *, mode: str) -> None:
        self._exec(
            "INSERT INTO orders (run_id, ts, iso_utc, pair, side, status, requested_qty, filled_qty, "
            "reference_price, reason, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, int(result.ts), iso_utc(result.ts), result.pair, result.side, result.status,
             result.requested_qty, result.filled_qty, result.reference_price, result.reason, mode),
        )

    def record_equity(self, point: Any) -> None:
        self._exec(
            "INSERT INTO equity (run_id, ts, iso_utc, cash, positions_value, equity, open_positions, realized_net_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, int(point.ts), iso_utc(point.ts), point.cash, point.positions_value,
             point.equity, point.open_positions, point.realized_net_pnl),
        )

    def record_event(self, ts: int, *, level: str = "INFO", category: str = "ops",
                     code: str = "", message: str = "", payload: Optional[Mapping[str, Any]] = None) -> None:
        self._exec(
            "INSERT INTO events (run_id, ts, iso_utc, level, category, code, message, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, int(ts), iso_utc(ts), level, category, code, message,
             json.dumps(dict(payload or {}), sort_keys=True)),
        )

    def record_events(self, items: Iterable[Mapping[str, Any]], *, category: str = "risk") -> None:
        for item in items:
            self.record_event(
                int(item.get("ts", 0)), level="WARNING" if "limit" in str(item.get("code", "")) else "INFO",
                category=category, code=str(item.get("code", "")), message=str(item.get("reason", "")),
                payload={k: v for k, v in item.items() if k not in {"ts", "code", "reason"}},
            )

    # -------------------------------------------------------------------- read
    def fetch_ledger(self) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM ledger WHERE run_id = ? ORDER BY id", (self.run_id,))

    def fetch_trades(self) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM trades WHERE run_id = ? ORDER BY id", (self.run_id,))

    def fetch_orders(self) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM orders WHERE run_id = ? ORDER BY id", (self.run_id,))

    def fetch_equity(self) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM equity WHERE run_id = ? ORDER BY id", (self.run_id,))

    def fetch_events(self) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM events WHERE run_id = ? ORDER BY id", (self.run_id,))

    def runs(self) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM runs ORDER BY started_at DESC")

    def latest_run_id(self) -> Optional[str]:
        rows = self._query("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1")
        return rows[0]["run_id"] if rows else None

    # ---------------------------------------------------------- derived values
    def cash_from_ledger(self, initial_cash: float) -> float:
        """Recompute cash from scratch: initial cash + every fill's cash flow."""
        cash = float(initial_cash)
        for row in self.fetch_ledger():
            notional = float(row.get("notional") or 0.0)
            fee = float(row.get("fee") or 0.0)
            if row["event"] == "OPEN":
                cash -= notional + fee
            else:
                cash += notional - fee
        return cash

    def realized_net_from_ledger(self) -> float:
        return sum(float(r.get("net_pnl") or 0.0) for r in self.fetch_ledger() if r["event"] == "CLOSE")

    def open_qty_from_ledger(self) -> Dict[str, float]:
        """Open quantity per pair derived from OPEN/CLOSE rows."""
        qty: Dict[str, float] = {}
        for row in self.fetch_ledger():
            pair = row["pair"]
            if row["event"] == "OPEN":
                qty[pair] = qty.get(pair, 0.0) + float(row.get("qty") or 0.0)
            else:
                qty[pair] = qty.get(pair, 0.0) - float(row.get("qty") or 0.0)
        return {k: v for k, v in qty.items() if abs(v) > 1e-12}

    def total_fees_from_ledger(self) -> float:
        return sum(float(r.get("fee") or 0.0) for r in self.fetch_ledger())

    # ------------------------------------------------------------------- verify
    def verify(
        self,
        *,
        initial_cash: float,
        broker_cash: float,
        broker_realized_net_pnl: float,
        broker_equity: Optional[float] = None,
        open_positions: Optional[Mapping[str, float]] = None,
        mark_prices: Optional[Mapping[str, float]] = None,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> VerifyReport:
        """Reconcile ledger-derived balances with the broker.

        Tolerances: absolute ``tolerance`` USDT (default 1e-6) for cash and
        realized PnL.  The equity check compares ``cash + positions marked at
        ``mark_prices``'' -- open-position value depends on the mark price used,
        which is why the caller must supply it; ``mark_prices=None`` skips that
        check and adds a note instead (documented open-position tolerance).
        """
        checks: List[Dict[str, Any]] = []
        notes: List[str] = []

        ledger_cash = self.cash_from_ledger(initial_cash)
        delta = ledger_cash - float(broker_cash)
        checks.append({
            "name": "cash_from_ledger == broker.cash",
            "ok": abs(delta) <= tolerance, "ledger": round(ledger_cash, 10),
            "broker": round(float(broker_cash), 10), "delta": round(delta, 12),
        })

        ledger_realized = self.realized_net_from_ledger()
        delta_realized = ledger_realized - float(broker_realized_net_pnl)
        checks.append({
            "name": "realized_net_pnl",
            "ok": abs(delta_realized) <= max(tolerance, abs(float(broker_realized_net_pnl)) * 1e-9),
            "ledger": round(ledger_realized, 10), "broker": round(float(broker_realized_net_pnl), 10),
            "delta": round(delta_realized, 12),
        })

        ledger_open = self.open_qty_from_ledger()
        if open_positions is None:
            # Not supplied -> cannot compare; say so instead of failing the run.
            notes.append(
                "open-position check skipped: pass open_positions from the broker to include it."
            )
        else:
            broker_open = {k: float(v) for k, v in open_positions.items() if abs(float(v)) > 1e-12}
            pairs_to_check = sorted(set(ledger_open) | set(broker_open))
            qty_ok = True
            for pair in pairs_to_check:
                if abs(ledger_open.get(pair, 0.0) - broker_open.get(pair, 0.0)) > 1e-9:
                    qty_ok = False
            checks.append({
                "name": "open_positions_qty",
                "ok": qty_ok, "ledger": {k: round(v, 10) for k, v in sorted(ledger_open.items())},
                "broker": {k: round(v, 10) for k, v in sorted(broker_open.items())},
                "delta": "n/a" if qty_ok else "mismatch",
            })

        if broker_equity is not None and mark_prices is not None:
            broker_open = {k: float(v) for k, v in (open_positions or {}).items() if abs(float(v)) > 1e-12}
            marked = float(broker_cash) + sum(
                float(qty) * float(mark_prices.get(pair, 0.0)) for pair, qty in broker_open.items()
            )
            delta_equity = marked - float(broker_equity)
            checks.append({
                "name": "equity == cash + marked positions",
                "ok": abs(delta_equity) <= tolerance * 100,
                "ledger": round(marked, 10), "broker": round(float(broker_equity), 10),
                "delta": round(delta_equity, 12),
            })
        else:
            notes.append(
                "equity check skipped: open-position value depends on the mark price; "
                "pass mark_prices to include it (documented tolerance {} USDT).".format(tolerance * 100)
            )

        ok = all(bool(c["ok"]) for c in checks)
        log.info("ledger.verify", extra={"event": "ledger_verify", "ok": ok, "run_id": self.run_id,
                                         "checks": len(checks)})
        return VerifyReport(ok=ok, tolerance=tolerance, checks=checks, notes=notes)

    # ------------------------------------------------------------------ export
    def export_csv(self, out_dir: Path | str, *, prefix: str = "") -> Dict[str, Path]:
        import csv

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        written: Dict[str, Path] = {}
        tables = {
            "ledger": self.fetch_ledger(),
            "trades": self.fetch_trades(),
            "orders": self.fetch_orders(),
            "equity": self.fetch_equity(),
            "events": self.fetch_events(),
        }
        for name, rows in tables.items():
            path = out / "{}{}.csv".format(prefix, name)
            with path.open("w", newline="", encoding="utf-8") as handle:
                if rows:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), lineterminator="\n")
                    writer.writeheader()
                    for row in rows:
                        writer.writerow(row)
                else:
                    handle.write("")
            written[name] = path
        return written

    def export_json(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "run": self._query("SELECT * FROM runs WHERE run_id = ?", (self.run_id,)),
            "ledger": self.fetch_ledger(),
            "trades": self.fetch_trades(),
            "orders": self.fetch_orders(),
            "equity": self.fetch_equity(),
            "events": self.fetch_events(),
            "reconciliation": {
                "realized_net_pnl_from_ledger": round(self.realized_net_from_ledger(), 10),
                "total_fees_from_ledger": round(self.total_fees_from_ledger(), 10),
                "open_qty_from_ledger": {k: round(v, 10) for k, v in sorted(self.open_qty_from_ledger().items())},
            },
        }
        target.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return target

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for table in ("ledger", "trades", "orders", "equity", "events"):
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM {} WHERE run_id = ?".format(table), (self.run_id,)
            ).fetchone()
            out[table] = int(row["n"])
        return out


__all__ = ["Ledger", "VerifyReport", "SCHEMA", "iso_utc", "DEFAULT_TOLERANCE"]
