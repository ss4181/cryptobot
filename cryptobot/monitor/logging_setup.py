"""Logging: one JSON line per event into ``cryptobot/logs/*.log`` plus a readable console stream.

Structured records use the ``extra`` field ``event`` as the event name; every
non-standard attribute attached to a record is serialised into the JSON line,
which is what makes the ledger/risk evidence greppable after the fact.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"
_JSON_SINK_NAME = "cryptobot.file"
_CONSOLE_SINK_NAME = "cryptobot.console"

# LogRecord attributes that are not "extra" payload.
_STANDARD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module", "exc_info",
    "exc_text", "stack_info", "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "taskName", "message", "asctime",
})


class JsonLineFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, event, message + extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key == "event":
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ConsoleFormatter(logging.Formatter):
    """Human-readable console line, with the structured extras appended."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and key != "event"
        }
        event = getattr(record, "event", None)
        if event:
            extras = {"event": event, **extras}
        if extras:
            detail = " ".join("{}={}".format(k, _short(v)) for k, v in sorted(extras.items()))
            return "{} {}".format(base, detail)
        return base


def _short(value: Any, limit: int = 120) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _make_console_handler(level: int) -> logging.Handler:
    handler = logging.StreamHandler(stream=_safe_stdout())
    handler.setLevel(level)
    handler.setFormatter(ConsoleFormatter(DEFAULT_FORMAT))
    return handler


def _safe_stdout():
    """Console stream that will not explode on a non-UTF8 Windows code page."""
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    return stream


def setup_logging(
    log_dir: Path | str,
    *,
    level: str = "INFO",
    run_id: Optional[str] = None,
    filename: Optional[str] = None,
    console: bool = True,
    force: bool = True,
) -> Path:
    """Configure the root logger and return the log file path."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    name = filename or "{}{}.log".format(
        datetime.now(timezone.utc).strftime("%Y%m%d"), "_{}".format(run_id) if run_id else ""
    )
    path = log_dir / name

    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(numeric)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(numeric)
    file_handler.setFormatter(JsonLineFormatter())
    file_handler.set_name(_JSON_SINK_NAME)
    root.addHandler(file_handler)

    if console:
        handler = _make_console_handler(numeric)
        handler.set_name(_CONSOLE_SINK_NAME)
        root.addHandler(handler)

    # Keep third-party loggers from flooding our structured file.
    for noisy in ("urllib3", "requests", "matplotlib", "ccxt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "logging.ready", extra={"event": "logging_ready", "path": str(path), "level": level.upper(),
                                "run_id": run_id}
    )
    return path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, message: str, level: str = "info", **fields: Any) -> None:
    """Convenience wrapper: ``log_event(log, 'order_buy', 'filled', pair='BTC/USDT')``."""
    getattr(logger, level.lower(), logger.info)(
        event, extra={"event": event, "message_text": message, **fields}
    )


__all__ = ["setup_logging", "get_logger", "log_event", "JsonLineFormatter", "ConsoleFormatter"]
