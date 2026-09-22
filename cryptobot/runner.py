"""Bounded paper-trading loop (simulation only).

``python -m cryptobot run`` drives this.  One *cycle*:

1. refresh public candles (cache-first; network only when needed),
2. feed every bar that closed since the previous cycle into the shared engine
   (same code path as the backtest -> strategy, risk manager, paper broker),
3. snapshot state to ``run/paper_state.json`` (heartbeat for ``status``),
4. sleep until the next cycle, unless ``--cycles`` / ``--duration-seconds`` is
   exhausted or a stop has been requested.

Stop is cooperative: ``python -m cryptobot stop`` drops a sentinel file that the
loop checks between cycles, so a run always finishes its current bar cleanly and
writes its reports.

**One bot per run directory.**  The pid/state files are the duplicate-instance
guard: :meth:`PaperRunner.setup` refuses to open the ledger while a *live* process
owns them (``paper.pid`` + ``paper_state.json`` with ``state=running``), raising
:class:`AlreadyRunningError`.  Liveness is checked portably -- ``os.kill(pid, 0)``
is *not* an option on Windows, where CPython maps it to ``TerminateProcess`` and
would kill the running bot; ``OpenProcess``/``GetExitCodeProcess`` is used there
instead (see :func:`pid_liveness`).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import __version__, safety
from .backtest.engine import TRADING_FAIL_SAFE, BacktestEngine, BacktestResult
from .config import Config
from .data.feed import (
    CacheCorrupted,
    FeedError,
    FeedResult,
    RequestsTransport,
    load_candles,
    now_ms,
)
from .execution.paper_broker import FaultInjector
from .ledger.store import Ledger
from .monitor.daily_report import write_daily_report
from .monitor.logging_setup import setup_logging
from .notify import events as notify_events
from .notify import build_notifier
from .notify.guard import REASON_OFFLINE, REASON_REPLAY
from .notify.mapping import from_engine as notify_from_engine
from .notify.watcher import EquityDropMonitor
from .risk.manager import utc_day

log = logging.getLogger(__name__)

RUN_DIR_NAME = "run"
STATE_FILE = "paper_state.json"
STOP_FILE = "paper.stop"
PID_FILE = "paper.pid"
HEARTBEAT_STALE_SECONDS = 600

#: Process-liveness outcomes (:func:`pid_liveness`).  ``unknown`` means "there may
#: be a process with this pid, but we are not allowed to inspect it" -- another
#: user's process took the pid after our run died, typically.  Only ``alive``
#: blocks a start; ``unknown`` proceeds and lets the ledger lock be the backstop,
#: so a stale pid that was recycled can never lock the operator out.
PID_ALIVE = "alive"
PID_GONE = "gone"
PID_UNKNOWN = "unknown"


@dataclass
class RunnerConfig:
    """Loop bounds / pacing (CLI-owned, not part of the trading config)."""

    cycles: Optional[int] = None
    duration_seconds: Optional[float] = None
    interval_seconds: float = 60.0
    offline: bool = False
    #: Walk-forward replay: expose ``replay_bars_per_cycle`` more cached bars per
    #: cycle instead of waiting for the wall clock.  Deterministic and offline;
    #: used by the smoke test and by `--replay`.
    replay: bool = False
    replay_bars_per_cycle: int = 1
    #: Explicit opt-in to network pushes in a replay/offline run.  Without it a
    #: ``--replay``/``--offline`` cycle records network dispatches as
    #: ``suppressed / replay_no_push`` instead of pushing (see notify/guard.py).
    #: Real paper mode (neither flag) always sends normally.
    notify_send: bool = False


@dataclass
class CycleReport:
    index: int
    ts: int
    bars_processed: int
    equity: float
    cash: float
    open_positions: int
    trades_closed: int
    halted: bool
    sources: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cycle": self.index, "ts": self.ts, "bars_processed": self.bars_processed,
            "equity": round(self.equity, 8), "cash": round(self.cash, 8),
            "open_positions": self.open_positions, "trades_closed": self.trades_closed,
            "halted": self.halted, "sources": dict(sorted(self.sources.items())),
            "warnings": list(self.warnings),
        }


def run_dir() -> Path:
    """Directory holding the pid/state/stop files (override with ``CRYPTOBOT_RUN_DIR``)."""
    override = os.environ.get("CRYPTOBOT_RUN_DIR")
    return Path(override) if override else Path(__file__).resolve().parent / RUN_DIR_NAME


def state_path() -> Path:
    return run_dir() / STATE_FILE


def stop_path() -> Path:
    return run_dir() / STOP_FILE


def pid_path() -> Path:
    return run_dir() / PID_FILE


def request_stop(reason: str = "cli stop") -> Path:
    """Create the cooperative stop sentinel. Returns its path."""
    path = stop_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"requested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason}),
        encoding="utf-8",
    )
    return path


def clear_stop() -> None:
    try:
        stop_path().unlink()
    except FileNotFoundError:
        pass


def stop_requested() -> bool:
    return stop_path().exists()


def read_state() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# duplicate-instance guard (pid + state files)
# --------------------------------------------------------------------------- #
class AlreadyRunningError(RuntimeError):
    """A live paper run already owns this run directory.

    Raised *before* the ledger is opened so a second instance can never contend
    for the SQLite write lock (which used to surface as a raw
    ``sqlite3.OperationalError: database is locked`` traceback).
    """

    def __init__(self, message: str, *, pid: Optional[int] = None,
                 run_id: Optional[str] = None, cycles: Any = None) -> None:
        super().__init__(message)
        self.pid = pid
        self.run_id = run_id
        self.cycles = cycles


def _windows_pid_liveness(pid: int) -> str:
    """``OpenProcess`` + ``GetExitCodeProcess`` -- the portable-enough Windows check.

    ``os.kill(pid, 0)`` must never be used here: on Windows every signal other than
    the console CTRL events is mapped to ``TerminateProcess``, so the liveness
    probe would *kill* the running bot.  Access denied means the pid exists but
    belongs to a process we may not inspect (another user), which is reported as
    ``unknown``.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87        # no such process (bad pid)
    ERROR_ACCESS_DENIED = 5             # exists, but not ours to query
    ERROR_INVALID_HANDLE = 6

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        error = ctypes.get_last_error()
        if error in (ERROR_INVALID_PARAMETER, ERROR_INVALID_HANDLE):
            return PID_GONE
        if error == ERROR_ACCESS_DENIED:
            return PID_UNKNOWN
        return PID_UNKNOWN
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return PID_UNKNOWN
        return PID_ALIVE if code.value == STILL_ACTIVE else PID_GONE
    finally:
        kernel32.CloseHandle(handle)


def pid_liveness(pid: Any) -> str:
    """``alive`` / ``gone`` / ``unknown`` for ``pid``.  Never raises.

    Portable across Windows and POSIX without any dependency: ``OpenProcess`` on
    Windows (``os.kill`` would terminate the process there) and ``os.kill(pid, 0)``
    elsewhere.  A process we may not inspect -- another user's -- is ``unknown``
    rather than ``alive``: the pid file can outlive its run and be recycled, and a
    ``unknown`` verdict must never lock the operator out of starting the bot.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return PID_UNKNOWN
    if pid <= 0:
        return PID_GONE
    try:
        if os.name == "nt":
            return _windows_pid_liveness(pid)
        os.kill(pid, 0)
        return PID_ALIVE
    except ProcessLookupError:
        return PID_GONE
    except PermissionError:      # POSIX: exists, but not inspectable by us
        return PID_UNKNOWN
    except OSError:
        return PID_UNKNOWN
    except Exception:            # pragma: no cover - a probe must never crash a start
        return PID_UNKNOWN


def read_pid() -> Optional[int]:
    """Pid recorded in ``run/paper.pid``, or ``None`` when missing/garbage."""
    try:
        raw = pid_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def detect_running_instance() -> Optional[Dict[str, Any]]:
    """Describe a *live* duplicate instance, or return ``None``.

    All three must agree before a start is refused: the pid file exists, that
    process is alive, and the state file says ``running`` with a pid matching the
    pid file (a mismatch means the pid file outlived its run and was recycled).
    """
    pid = read_pid()
    if pid is None:
        return None
    state = read_state() or {}
    if state.get("state") != "running":
        return None
    state_pid = state.get("pid")
    if state_pid is not None:
        try:
            if int(state_pid) != pid:
                return None
        except (TypeError, ValueError):
            return None
    liveness = pid_liveness(pid)
    if liveness != PID_ALIVE:
        # DEBUG, not WARNING: the caller turns this into the operator-facing
        # "clearing stale state" note, and this probe also runs before the CLI has
        # configured logging (a WARNING would reach the root last-resort handler
        # as a bare line).
        log.debug("runner.pid_not_alive",
                  extra={"event": "pid_not_alive", "pid": pid, "liveness": liveness,
                         "note": "stale run state will be cleared"})
        return None
    return {
        "pid": pid,
        "run_id": state.get("run_id"),
        "cycles": state.get("cycles_run"),
        "heartbeat": state.get("heartbeat"),
        "state_state": state.get("state"),
        "state_path": str(state_path()),
        "pid_path": str(pid_path()),
    }


def describe_running_instance(info: Mapping[str, Any]) -> str:
    """The operator-facing Turkish message for a detected duplicate."""
    cycles = info.get("cycles")
    return (
        "Bu bot zaten calisiyor (pid {pid}, run_id {run_id}, dongu {cycles}). "
        "Once durdurun: start-bot.cmd stop\n"
        "Ayni anda tek bot calistirilabilir; bu surec deftere ve durum dosyasina dokunmadi.\n"
        "Durum: {state_path}\n"
        "Not: bu pid baska bir surece aitse (pid yeniden kullanilmis olabilir) ve calisan bot "
        "olmadigindan eminseniz su dosyayi silip tekrar baslatin: {pid_path}"
    ).format(
        pid=info.get("pid"),
        run_id=info.get("run_id") or "?",
        cycles="?" if cycles is None else cycles,
        state_path=info.get("state_path") or state_path(),
        pid_path=info.get("pid_path") or pid_path(),
    )


def clear_stale_state() -> Optional[str]:
    """Remove a pid file left behind by a run that is not alive any more.

    Returns a one-line note when something was cleared, else ``None``.  Never
    touches the ledger and never removes the cooperative stop sentinel (a
    requested stop must survive a restart attempt).  A pid file whose process *is*
    alive is kept even when the state file does not say ``running`` -- it may be a
    run that just started -- and only reported.
    """
    path = pid_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if detect_running_instance() is not None:
        return None                      # a live run owns it: caller must not proceed
    pid = read_pid()
    state = read_state() or {}
    if pid is None:
        liveness, reason = PID_UNKNOWN, "gecersiz pid kaydi"
    elif state.get("state") == "running":
        liveness, reason = pid_liveness(pid), "olu pid kaydi"
    elif pid_liveness(pid) == PID_ALIVE:
        liveness, reason = PID_ALIVE, "canli pid kaydi (state={})".format(state.get("state") or "yok")
    else:
        liveness = PID_UNKNOWN
        reason = "eski kosu kaydi (state={})".format(state.get("state") or "yok")
    if liveness == PID_ALIVE:
        return "Not: {} - canli pid korundu, kosu devam ediyor olabilir.".format(reason)
    try:
        path.unlink()
    except OSError:                      # pragma: no cover - Windows may hold the file
        return "Not: eski pid kaydi temizlenemedi: {}".format(path)
    return "Eski durum temizlendi: {} (pid {}), kosu yeniden baslatiliyor.".format(reason, raw or "?")


def guard_against_duplicate_instance() -> Optional[str]:
    """Refuse a duplicate start, else clear stale state and return its note.

    Raises :class:`AlreadyRunningError` when a live run owns the run directory.
    """
    live = detect_running_instance()
    if live is not None:
        log.warning("runner.duplicate_start_refused",
                    extra={"event": "duplicate_start_refused", "pid": live.get("pid"),
                           "run_id": live.get("run_id")})
        raise AlreadyRunningError(describe_running_instance(live), pid=live.get("pid"),
                                  run_id=live.get("run_id"), cycles=live.get("cycles"))
    return clear_stale_state()


class PaperRunner:
    """Runs the paper bot in bounded cycles with full ledger + logging."""

    def __init__(
        self,
        config: Config,
        *,
        runner: Optional[RunnerConfig] = None,
        faults: Optional[FaultInjector] = None,
        run_id: Optional[str] = None,
        transport: Any = None,
        sleep_fn: Any = time.sleep,
        clock: Any = now_ms,
        setup_logs: bool = True,
        log_path: Optional[Path] = None,
        notifier: Any = None,
    ) -> None:
        self.config = config
        self.runner = runner or RunnerConfig()
        self.faults = faults
        self.run_id = run_id or "paper-{}-{}".format(
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), os.getpid()
        )
        self.transport = transport or RequestsTransport()
        self.sleep = sleep_fn
        self.clock = clock
        self.setup_logs = setup_logs
        #: Set by setup() when this runner configures logging itself; otherwise the
        #: caller (CLI) passes the path it already created.
        self.log_path: Optional[Path] = Path(log_path) if log_path else None

        self.ledger: Optional[Ledger] = None
        self.engine: Optional[BacktestEngine] = None
        self.cycles: List[CycleReport] = []
        self.started_at = 0
        self.finished_at: Optional[int] = None
        #: Notification layer (mobile alerts). Built in setup() from config + env
        #: unless a test injects one. Notifications never affect trading: every
        #: call goes through _notify_event, which swallows anything it raises.
        self.notifier: Any = notifier
        self._equity_monitor: Optional[EquityDropMonitor] = None
        #: Historical measurement attached to entry notifications: one offline
        #: backtest over the cached candles, computed once in setup() so the
        #: trading loop never pays for it.  ``None`` (no cache, stats failure)
        #: simply means the notification omits its history block.
        self._history_stats: Optional[Dict[str, Any]] = None
        self._last_day: Optional[str] = None
        self._source_by_pair: Dict[str, str] = {}
        self._cursor: Dict[str, int] = {}
        self._last_processed_ts: Dict[str, int] = {}

    # ------------------------------------------------------------- replay glue
    def current_ts(self) -> int:
        """Market time of the last processed bar, falling back to wall clock."""
        if self.engine is not None and getattr(self.engine, "_last_ts", None):
            return int(self.engine._last_ts)  # noqa: SLF001 - engine exposes market time
        return int(self.clock())

    def _truncate_for_replay(self, pair: str, frame: "Any", warmup: int) -> "Any":
        """Expose a progressively longer window of cached bars (walk-forward)."""
        total = len(frame)
        cursor = self._cursor.get(pair)
        if cursor is None:
            cursor = min(total, max(warmup + 1, self.runner.replay_bars_per_cycle))
        else:
            cursor = min(total, cursor + max(1, self.runner.replay_bars_per_cycle))
        self._cursor[pair] = cursor
        return frame.iloc[:cursor]

    # ------------------------------------------------------------------ setup
    def setup(self) -> None:
        # Duplicate-instance guard, *before* the ledger is opened: a second bot
        # writing the same ledger is what produced the raw `database is locked`
        # traceback this guards against.  Stale pid/state files are cleared here.
        guard_against_duplicate_instance()
        if self.setup_logs:
            self.log_path = setup_logging(
                self.config.logs_dir, level=self.config.logging.level, run_id=self.run_id
            )
        self.started_at = int(self.clock())
        self.ledger = Ledger(self.config.db_path, self.run_id)
        self.ledger.start_run(
            mode=self.config.mode, started_at=self.started_at, version=__version__,
            config=self.config.as_dict(),
            data={"runner": {"cycles": self.runner.cycles, "duration_seconds": self.runner.duration_seconds,
                             "interval_seconds": self.runner.interval_seconds, "offline": self.runner.offline}},
        )
        self.engine = BacktestEngine(self.config, ledger=self.ledger, run_id=self.run_id, faults=self.faults)
        self.engine.mode = "paper"
        self._last_day = utc_day(self.started_at)

        # Notification layer: config + environment only. A failure to build it is
        # contained here so a bad notification setting can never stop the bot.
        try:
            if self.notifier is None:
                # Replay/offline walks historical bars: network providers must not
                # push unless the operator explicitly opted in with --notify-send.
                network_send = self.runner.notify_send or not (
                    self.runner.replay or self.runner.offline)
                block_reason = REASON_OFFLINE if self.runner.offline and not self.runner.replay \
                    else REASON_REPLAY
                self.notifier = build_notifier(
                    self.config, network_send_allowed=network_send,
                    network_block_reason=block_reason,
                )
            self._equity_monitor = EquityDropMonitor(
                self.config.notifications.equity_drop_pct,
                enabled=self.config.notifications.enabled,
            )
            self.engine.event_sink = self._on_engine_event
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("runner.notifier_setup_failed",
                        extra={"event": "notifier_setup_failed", "error": type(exc).__name__})
            self.notifier = None
            self._equity_monitor = None

        # Historical measurement block (real offline backtest over data/cache).
        # Contained and bounded: a missing cache or a failed replay only means the
        # entry notification has no history block.
        try:
            if self.notifier is not None and self.config.notifications.enabled:
                from .notify.context import HistoryStatsProvider

                stats = HistoryStatsProvider(self.config).get()
                self._history_stats = stats.as_dict() if stats is not None else None
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("runner.history_stats_failed",
                        extra={"event": "history_stats_failed", "error": type(exc).__name__})
            self._history_stats = None

        run_dir().mkdir(parents=True, exist_ok=True)
        pid_path().write_text(str(os.getpid()), encoding="utf-8")
        credentials = safety.audit_credentials()
        # "Can this process actually push to a phone right now?" -- one explicit
        # startup line, so the operator never has to guess (harness guard,
        # replay no-push, dry-run and missing credentials all show up here).
        try:
            if self.notifier is not None and hasattr(self.notifier, "log_send_capability"):
                self.notifier.log_send_capability()
        except Exception:  # pragma: no cover - logging must not break setup
            pass
        log.info(
            "runner.setup",
            extra={"event": "runner_setup", "run_id": self.run_id, "mode": self.config.mode,
                   "pairs": list(self.config.pairs), "timeframe": self.config.timeframe,
                   "initial_capital": self.config.initial_capital_usdt,
                   "gross_tp_pct": round(self.engine.risk.gross_tp_pct, 6),
                   "credentials_found_but_ignored": list(credentials),
                   "live_trading": "structurally absent"},
        )
        self._notify_event(notify_events.bot_started(
            run_id=self.run_id, mode=self.config.mode, pairs=self.config.pairs,
            timeframe=self.config.timeframe, initial_capital=self.config.initial_capital_usdt,
            dry_run=bool(getattr(self.notifier, "cfg", None) and self.notifier.cfg.dry_run),
            ts=self.started_at,
        ))

    # ------------------------------------------------------------ notification
    def _notify_event(self, event: Any) -> None:
        """Deliver one event. Never raises, never affects trading."""
        if self.notifier is None or event is None:
            return
        try:
            self.notifier.notify(event)
        except Exception as exc:  # pragma: no cover - Notifier already never raises
            log.warning("runner.notify_failed",
                        extra={"event": "notify_failed", "error": type(exc).__name__})

    def _on_engine_event(self, payload: Mapping[str, Any]) -> None:
        """Adapter installed as ``engine.event_sink``."""
        try:
            event = notify_from_engine(payload, run_id=self.run_id, stats=self._history_stats)
        except Exception:  # pragma: no cover - defensive
            return
        self._notify_event(event)

    def _day_summary(self, day: str) -> Dict[str, Any]:
        """Equity / trades / win-rate / net PnL for one UTC day, from the ledger."""
        assert self.ledger is not None and self.engine is not None
        trades = [t for t in self.ledger.fetch_trades() if str(t.get("exit_iso_utc") or "")[:10] == day]
        net = sum(float(t.get("net_pnl") or 0.0) for t in trades)
        wins = sum(1 for t in trades if float(t.get("net_pnl") or 0.0) > 0)
        win_rate = (wins / len(trades) * 100.0) if trades else 0.0
        points = [e for e in self.ledger.fetch_equity() if str(e.get("iso_utc") or "")[:10] == day]
        equity = float(points[-1]["equity"]) if points else float(self.engine.broker.equity())
        return {"day": day, "equity": equity, "trades": len(trades), "wins": wins,
                "win_rate_pct": win_rate, "net_pnl": net}

    def _notify_daily_summary(self, day: str) -> None:
        assert self.engine is not None
        try:
            summary = self._day_summary(day)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("runner.day_summary_failed",
                        extra={"event": "day_summary_failed", "error": type(exc).__name__})
            return
        self._notify_event(notify_events.daily_summary(run_id=self.run_id, ts=self.current_ts(), **summary))

    # -------------------------------------------------------------- one cycle
    def _load_pair(self, pair: str) -> Optional[FeedResult]:
        # Real-time paper mode only: replay uses historical bars on purpose and
        # offline mode has no network, so neither may treat old candles as stale.
        realtime = (not self.runner.replay) and (not self.runner.offline)
        try:
            result = load_candles(
                self.config, pair,
                allow_network=not self.runner.offline,
                transport=None if self.runner.offline else self.transport,
                sleep=self.sleep,
                realtime=realtime,
            )
            label = result.source + ("" if result.complete else " (incomplete)")
            if self.runner.replay:
                total = len(result.frame)
                warmup = self.engine.strategy.warmup_bars() if self.engine else 0
                frame = self._truncate_for_replay(pair, result.frame, warmup)
                result = FeedResult(pair=pair, frame=frame, source=result.source + "+replay",
                                    complete=result.complete, warnings=list(result.warnings))
                label = "{}+replay ({} of {} bars)".format(result.source.rsplit("+replay", 1)[0], len(frame), total)
            self._source_by_pair[pair] = label
            if not result.complete:
                # Fail-safe visibility: a stale/incomplete feed is recorded in the
                # ledger so a data outage is auditable from the ledger alone.
                self._record_incomplete_data(pair, result)
            return result
        except (FeedError, CacheCorrupted) as exc:
            # Fail-safe: no data for this pair this cycle -> no new entries.
            message = "no usable data for {} this cycle: {}: {}".format(pair, type(exc).__name__, exc)
            log.error("runner.feed_error", extra={"event": "feed_error", "pair": pair,
                                                 "error": type(exc).__name__, "detail": str(exc)})
            if self.ledger is not None:
                self.ledger.record_event(int(self.clock()), level="ERROR", category="feed",
                                         code="feed_unavailable", message=message, payload={"pair": pair})
            self._notify_event(notify_events.feed_outage(
                run_id=self.run_id, pair=pair, error=type(exc).__name__, detail=str(exc),
                ts=int(self.clock())))
            self._source_by_pair[pair] = "unavailable"
            return None

    def _record_incomplete_data(self, pair: str, result: FeedResult) -> None:
        """Persist the fail-safe trigger so the outage survives in the ledger."""
        code = "cache_stale" if "stale" in result.source else "data_incomplete"
        detail = "; ".join(result.warnings) or "feed marked incomplete"
        log.warning(
            "runner.incomplete_data",
            extra={"event": "incomplete_data", "pair": pair, "source": result.source, "detail": detail},
        )
        if self.ledger is not None:
            self.ledger.record_event(
                int(self.clock()), level="WARNING", category="feed", code=code,
                message="new entries paused for {}: {}".format(pair, detail),
                payload={"pair": pair, "source": result.source, "warnings": list(result.warnings)},
            )

    def cycle(self, index: int) -> Optional[CycleReport]:
        assert self.engine is not None and self.ledger is not None

        frames: Dict[str, Any] = {}
        data_meta: Dict[str, Any] = {}
        warnings: List[str] = []
        for pair in self.config.pairs:
            result = self._load_pair(pair)
            if result is None:
                warnings.append("feed unavailable for {}".format(pair))
                continue
            frames[pair] = result.frame
            data_meta[pair] = {"source": result.source, "complete": result.complete, "rows": result.rows}
            warnings.extend(result.warnings)

        since = {pair: self._last_processed_ts.get(pair) for pair in frames}
        processed = 0
        if len(frames) == len(self.config.pairs):
            processed = self.engine.process(frames, data_meta=data_meta, since_ts=since)
        else:
            self.engine._fail_safe(  # noqa: SLF001 - deliberate fail-safe on partial data
                int(self.clock()), "partial data ({} of {} pairs): new entries paused".format(
                    len(frames), len(self.config.pairs))
            )
            if frames:
                processed = self.engine.process(frames, data_meta=data_meta, since_ts=since)
        for pair, frame in frames.items():
            if len(frame):
                self._last_processed_ts[pair] = int(frame["ts"].iloc[-1])

        self._roll_daily_report()

        snapshot = self.engine.broker.snapshot()
        report = CycleReport(
            index=index,
            ts=self.current_ts(),
            bars_processed=processed,
            equity=snapshot["equity"],
            cash=snapshot["cash"],
            open_positions=snapshot["open_positions"],
            trades_closed=snapshot["closed_trades"],
            halted=self.engine.risk.equity_halted(),
            sources=dict(self._source_by_pair),
            warnings=warnings,
        )
        self.cycles.append(report)
        self._write_state("running")
        if self._equity_monitor is not None:
            triggered = self._equity_monitor.update(snapshot["equity"], report.ts)
            if triggered is not None:
                peak, drop_pct = triggered
                self._notify_event(notify_events.equity_drop(
                    run_id=self.run_id, peak_equity=peak, equity=snapshot["equity"],
                    drop_pct=drop_pct, threshold_pct=self.config.notifications.equity_drop_pct,
                    ts=report.ts))
        log.info(
            "runner.cycle",
            extra={"event": "runner_cycle", **{k: v for k, v in report.as_dict().items() if k != "sources"}},
        )
        return report

    def _roll_daily_report(self) -> None:
        assert self.ledger is not None and self.engine is not None
        today = utc_day(self.current_ts())
        if self._last_day and today != self._last_day:
            log.info("runner.day_rollover", extra={"event": "day_rollover", "from": self._last_day, "to": today})
            self.write_daily_report(day=self._last_day)
            self._notify_daily_summary(self._last_day)
            self._last_day = today

    # ---------------------------------------------------------------- helpers
    def verification(self) -> Dict[str, Any]:
        assert self.ledger is not None and self.engine is not None
        broker = self.engine.broker
        report = self.ledger.verify(
            initial_cash=self.config.initial_capital_usdt,
            broker_cash=broker.cash,
            broker_realized_net_pnl=broker.realized_net_pnl,
            broker_equity=broker.equity(),
            open_positions={pair: position.qty for pair, position in broker.positions.items()},
            mark_prices={pair: broker.price_of(pair) for pair in broker.positions},
        )
        return report.as_dict()

    def status_payload(self) -> Dict[str, Any]:
        assert self.engine is not None
        broker = self.engine.broker
        return {
            "run_id": self.run_id,
            "mode": self.config.mode,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "heartbeat": int(self.clock()),
            "cycles_run": len(self.cycles),
            "runner": {
                "cycles": self.runner.cycles, "duration_seconds": self.runner.duration_seconds,
                "interval_seconds": self.runner.interval_seconds, "offline": self.runner.offline,
            },
            "trading_state": self.engine.trading_state,
            "halted": self.engine.risk.equity_halted(),
            "risk_state": self.engine.risk.state_snapshot(int(self.clock())),
            "risk_limits": self.engine.risk.limits_snapshot(),
            "broker": broker.snapshot(),
            "config": self.config.as_dict(),
            "safety": safety.safety_statement(),
            "log_file": str(self.log_path) if self.log_path else None,
            "db_path": str(self.config.db_path),
            "notifications": self._notifier_status(),
        }

    def _notifier_status(self) -> Any:
        if self.notifier is None:
            return None
        try:
            payload: Dict[str, Any] = {
                "dry_run": bool(self.notifier.cfg.dry_run),
                "providers": self.notifier.status(),
                "notify_on": list(self.config.notifications.notify_on),
            }
            if hasattr(self.notifier, "can_send_to_network"):
                possible, reason = self.notifier.can_send_to_network()
                payload["network_send_possible"] = bool(possible)
                payload["network_block_reason"] = reason
            return payload
        except Exception:  # pragma: no cover - defensive
            return None

    def _write_state(self, state: str) -> None:
        payload = dict(self.status_payload())
        payload["state"] = state
        payload["cycles"] = [c.as_dict() for c in self.cycles]
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    def write_daily_report(self, day: Optional[str] = None) -> Path:
        assert self.ledger is not None and self.engine is not None
        target_day = day or utc_day(self.current_ts())
        return write_daily_report(
            self.config.reports_dir,
            day=target_day,
            config=self.config,
            broker_snapshot=self.engine.broker.snapshot(),
            trades=self.ledger.fetch_trades(),
            equity_points=self.ledger.fetch_equity(),
            events=self.ledger.fetch_events(),
            orders=self.ledger.fetch_orders(),
            reconciliation=self.verification(),
            risk_state=self.engine.risk.state_snapshot(int(self.clock())),
            run_id=self.run_id,
        )

    # -------------------------------------------------------------------- run
    def run(self) -> Dict[str, Any]:
        """Run bounded cycles until a bound or a stop request is reached."""
        if self.engine is None:
            self.setup()
        assert self.engine is not None and self.ledger is not None

        bounds = []
        if self.runner.cycles is not None:
            bounds.append("cycles={}".format(self.runner.cycles))
        if self.runner.duration_seconds is not None:
            bounds.append("duration={}s".format(self.runner.duration_seconds))
        if not bounds:
            bounds.append("unbounded (Ctrl+C or `python -m cryptobot stop`)")
        log.info("runner.start", extra={"event": "runner_start", "bounds": bounds,
                                        "interval_seconds": self.runner.interval_seconds})

        deadline = None
        if self.runner.duration_seconds is not None:
            deadline = time.monotonic() + float(self.runner.duration_seconds)

        stopped_early = False
        index = 0
        try:
            while True:
                if stop_requested():
                    stopped_early = True
                    log.warning("runner.stop_requested", extra={"event": "stop_requested", "cycle": index})
                    break
                if self.runner.cycles is not None and index >= self.runner.cycles:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self.cycle(index + 1)
                index += 1
                if self.runner.cycles is not None and index >= self.runner.cycles:
                    break
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.sleep(min(self.runner.interval_seconds, remaining))
                else:
                    self.sleep(self.runner.interval_seconds)
        except KeyboardInterrupt:  # pragma: no cover - interactive
            stopped_early = True
            log.warning("runner.interrupted", extra={"event": "runner_interrupted"})
        finally:
            self.finished_at = int(self.clock())
            self._write_state("stopped" if stopped_early else "finished")
            clear_stop()
            try:
                pid_path().unlink()
            except FileNotFoundError:
                pass

        result = self.finalize()
        result["stopped_early"] = stopped_early
        return result

    def finalize(self) -> Dict[str, Any]:
        """Write reports, verify the ledger and return the run summary."""
        assert self.engine is not None and self.ledger is not None
        self.ledger.record_event(
            self.finished_at or int(self.clock()), level="INFO", category="runner", code="paper_run_finished",
            message="paper run finished after {} cycles".format(len(self.cycles)),
        )
        try:
            backtest_result: BacktestResult = self.engine.finalize()
            daily = self.write_daily_report(self._last_day or utc_day(self.current_ts()))
            verification = self.verification()
            counts = self.ledger.counts()

            self.ledger.record_event(
                self.finished_at or int(self.clock()), level="INFO", category="runner", code="paper_run_summary",
                message="{} cycles, {} bars, {} closed trades".format(
                    len(self.cycles), backtest_result.bars_processed, len(backtest_result.trades)),
                payload={"verification_ok": verification["ok"]},
            )

            summary = {
                "run_id": self.run_id,
                "mode": "paper",
                "cycles": len(self.cycles),
                "bars_processed": backtest_result.bars_processed,
                "trades_closed": len(backtest_result.trades),
                "final_equity": backtest_result.metrics["final_equity_usdt"],
                "net_pnl_usdt": backtest_result.metrics["net_pnl_usdt"],
                "net_pnl_pct": backtest_result.metrics["net_pnl_pct"],
                "win_rate_pct": backtest_result.metrics["win_rate_pct"],
                "trading_state": self.engine.trading_state,
                "halted": self.engine.risk.equity_halted(),
                "ledger_counts": counts,
                "verification": verification,
                "daily_report": str(daily),
                "log_file": str(self.log_path) if self.log_path else None,
                "db_path": str(self.config.db_path),
                "cycle_reports": [c.as_dict() for c in self.cycles],
                "warnings": backtest_result.warnings,
                "metrics": backtest_result.metrics,
            }
            log.info("runner.finished", extra={"event": "runner_finished", "run_id": self.run_id,
                                               "cycles": len(self.cycles),
                                               "verification_ok": verification["ok"],
                                               "final_equity": summary["final_equity"]})
            self._notify_daily_summary(self._last_day or utc_day(self.current_ts()))
            self._notify_event(notify_events.bot_stopped(
                run_id=self.run_id, final_equity=summary["final_equity"],
                net_pnl=summary["net_pnl_usdt"], net_pnl_pct=summary["net_pnl_pct"],
                trades=summary["trades_closed"], win_rate_pct=summary["win_rate_pct"],
                ts=self.finished_at or int(self.clock())))
            return summary
        finally:
            # Always release the SQLite handle, even if reporting failed.
            self.ledger.close()


__all__ = [
    "PaperRunner", "RunnerConfig", "CycleReport", "request_stop", "clear_stop", "stop_requested",
    "read_state", "state_path", "stop_path", "pid_path", "TRADING_FAIL_SAFE",
    "AlreadyRunningError", "clear_stale_state", "describe_running_instance",
    "detect_running_instance", "guard_against_duplicate_instance", "pid_liveness", "read_pid",
    "PID_ALIVE", "PID_GONE", "PID_UNKNOWN",
]
