"""Append-only notification audit store (``logs/notifications.jsonl``).

Why a JSONL file instead of a ``notifications`` table in the ledger SQLite?
--------------------------------------------------------------------------
1. **Failure isolation.**  The trading loop owns the single SQLite connection;
   a second connection writing on every push would risk ``database is locked``
   errors *inside the trading cycle*.  The whole point of the notification layer
   is that it can never affect trading, so it must not share that lock.
2. **Survives a crash mid-dispatch.**  One small ``append`` per attempt is
   atomic enough and never needs a schema migration.
3. **Works without a run.**  ``notify test`` / ``--dry-run`` have no ledger and
   must still be auditable.
4. **Trivial to review/export.**  ``notify log`` tails it, ``notify export``
   turns it into CSV/JSON, and ``grep`` works.

The same file doubles as the ``file`` provider's output: one line per dispatch
attempt *is* the local record of what was sent (or suppressed, or failed), so
there is exactly one write path and no duplicate copy to keep in sync.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

#: Field order used by ``notify log`` and the CSV/JSON exports.
RECORD_FIELDS = (
    "ts", "iso_utc", "event", "severity", "provider", "status", "reason",
    "http_status", "response", "latency_ms", "attempts", "dry_run", "title", "body",
    "dedupe_key", "target", "run_id",
)


class NotificationStore:
    """Append-only JSONL sink for dispatch attempts.

    Reads tolerate a truncated/garbled last line (a crash mid-write) instead of
    raising: an audit file must never be able to break ``notify log``.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ write
    def append(self, record: Dict[str, Any]) -> None:
        """Append one JSON line. Creates the parent directory on demand."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record), sort_keys=True, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # ------------------------------------------------------------------- read
    def exists(self) -> bool:
        return self.path.exists()

    def _iter_lines(self) -> Iterable[Dict[str, Any]]:
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue  # truncated line from a crash: skip, never raise
            if isinstance(item, dict):
                yield item

    def read_all(self, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return records oldest-first; ``limit`` keeps the newest N (0 -> empty)."""
        items = list(self._iter_lines())
        if limit is not None:
            if limit <= 0:
                return []
            items = items[-limit:]
        return items

    def read(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the newest ``limit`` records, oldest-first (what ``notify log`` prints)."""
        return self.read_all(limit=max(0, int(limit)))

    def count(self) -> int:
        return sum(1 for _ in self._iter_lines())

    # ----------------------------------------------------------------- export
    def export_json(self, target: Path | str) -> Path:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.read_all(), indent=2, sort_keys=True, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return target

    def export_csv(self, target: Path | str) -> Path:
        import csv

        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.read_all()
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(RECORD_FIELDS), lineterminator="\n",
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in RECORD_FIELDS})
        return target


__all__ = ["NotificationStore", "RECORD_FIELDS"]
