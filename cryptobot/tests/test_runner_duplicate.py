"""Regression: a second bot instance is refused; a held ledger fails gracefully.

The defect (reproduced on the unfixed tree): with one bot running, starting a
second one crashed with a raw traceback -- ``runner.setup()`` -> ``Ledger.start_run``
-> ``sqlite3.OperationalError: database is locked``.  Two behaviours are pinned
here:

* **duplicate instances** -- ``run`` detects a *live* run in the pid/state files
  and exits with :data:`cryptobot.cli.EXIT_BUSY` and a readable Turkish message,
  without opening the ledger or touching the run state; a stale pid file (dead
  process, or a state file that no longer says ``running``) is cleared and the
  run proceeds;
* **a genuinely locked ledger** (held by any other process) is reported as a
  short, actionable Turkish message and a non-zero exit code -- never a traceback.

Every test points ``CRYPTOBOT_RUN_DIR`` at a private temp directory, so the
suite's own isolation contract (see ``test_zz_run_dir_isolation.py``) is kept.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptobot import cli, runner
from cryptobot.data.feed import cache_path
from cryptobot.ledger import store as ledger_store
from cryptobot.runner import (
    AlreadyRunningError,
    PaperRunner,
    RunnerConfig,
    clear_stale_state,
    describe_running_instance,
    detect_running_instance,
    pid_liveness,
    read_pid,
    read_state,
    state_path,
    pid_path,
    PID_ALIVE,
    PID_GONE,
    PID_UNKNOWN,
)

from .fixtures import make_frame, save_cache, tmp_config

PAIRS = ("BTC/USDT", "ETH/USDT")
FRAME_BARS = 600

#: A pid that cannot belong to a live process on either platform: Windows'
#: ``OpenProcess`` reports "invalid parameter" and POSIX ``kill`` reports ESRCH
#: (well above ``pid_max``).  Used for the "stale pid" cases.
DEAD_PID = 999_999_999

CONFIG_TEMPLATE = """\
mode: paper
initial_capital_usdt: 50.0
net_profit_target_pct: 2.0
stop_loss_pct: 2.5
max_position_pct: 90.0
max_open_positions: 1
daily_loss_limit_pct: 5.0
cooldown_minutes: 60
min_equity_usdt: 10.0
max_trades_per_day: 8
pairs:
  - BTC/USDT
  - ETH/USDT
timeframe: 15m
fee_pct: 0.1
slippage_pct: 0.05
strategy:
  name: mean_reversion
  params:
    bb_period: 20
    bb_std: 2.0
    rsi_period: 14
    rsi_oversold: 35.0
    trend_sma_period: 100
    exit_at_middle_band: true
data:
  api_base: https://api.binance.com
  history_days: 60
  cache_dir: {cache_dir}
  db_path: {db_path}
reports_dir: {reports_dir}
logging:
  level: WARNING
  dir: {logs_dir}
"""


class DuplicateInstanceTestCase(unittest.TestCase):
    """Per-test config + private run directory + a seeded offline cache."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._close_log_handlers)

        self.root = Path(self.tmp.name)
        self.config = tmp_config(self.root, pairs=PAIRS, timeframe="15m")
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(CONFIG_TEMPLATE.format(
            cache_dir=self.config.cache_dir,
            db_path=self.config.db_path,
            reports_dir=self.config.reports_dir,
            logs_dir=self.config.logs_dir,
        ), encoding="utf-8")

        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        patcher = mock.patch.dict(os.environ, {"CRYPTOBOT_RUN_DIR": str(self.run_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

        for pair, seed in (("BTC/USDT", 41), ("ETH/USDT", 42)):
            frame = make_frame(FRAME_BARS, step_ms=900_000, make_entries=True, seed=seed)
            save_cache(frame, cache_path(self.config.cache_dir, pair, "15m"))

    @staticmethod
    def _close_log_handlers() -> None:
        """Release log files: Windows cannot delete a directory with an open handle."""
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    # ------------------------------------------------------------- helpers
    def run_cli(self, *argv) -> tuple:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def cfg(self, *extra) -> tuple:
        return ("--config", str(self.config_path), *extra)

    def write_run_files(self, pid: int, state: str, *, state_pid: int | None = None,
                        run_id: str = "paper-live", cycles: int = 7) -> None:
        pid_path().write_text(str(pid), encoding="utf-8")
        payload = {
            "state": state,
            "pid": pid if state_pid is None else state_pid,
            "run_id": run_id,
            "cycles_run": cycles,
            "heartbeat": 1_700_000_000_000,
        }
        state_path().write_text(json.dumps(payload), encoding="utf-8")

    def ledger_paths(self) -> list:
        return sorted(p.name for p in Path(self.config.db_path).parent.iterdir())

    def make_runner(self, **kwargs) -> PaperRunner:
        settings = dict(cycles=2, offline=True, replay=True, replay_bars_per_cycle=150,
                        interval_seconds=0.0)
        settings.update(kwargs)
        return PaperRunner(self.config, runner=RunnerConfig(**settings), run_id="paper-regression",
                           sleep_fn=lambda _s: None, setup_logs=True)


# --------------------------------------------------------------------------- #
# 1. the liveness probe itself
# --------------------------------------------------------------------------- #
class TestPidLivenessProbe(unittest.TestCase):
    """``pid_liveness`` must be portable and must never raise.

    ``observed`` on Windows (Python 3.13): the checks below are the framework
    assert (a real child process, a dead pid, the protected System pid).
    """

    def test_own_process_is_alive(self):
        self.assertEqual(pid_liveness(os.getpid()), PID_ALIVE)

    def test_definitely_dead_pid_is_gone(self):
        self.assertEqual(pid_liveness(DEAD_PID), PID_GONE)

    def test_a_reaped_child_process_is_gone(self):
        import subprocess
        import sys

        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIn(pid_liveness(child.pid), (PID_GONE, PID_UNKNOWN),
                      "a finished process must never be reported alive")

    def test_garbage_and_reserved_pids_never_raise(self):
        cases = [0, -1, -99999, "not-a-pid", None, 3.5, DEAD_PID, 4]
        for value in cases:
            with self.subTest(pid=value):
                self.assertIn(pid_liveness(value), (PID_ALIVE, PID_GONE, PID_UNKNOWN))

    def test_a_protected_system_pid_never_crashes_or_claims_gone(self):
        """Windows: pid 4 is the System process -- another-user/protected path."""
        if os.name != "nt":  # pragma: no cover - POSIX runs have no pid 4 semantics
            self.skipTest("Windows-specific liveness path")
        verdict = pid_liveness(4)
        self.assertIn(verdict, (PID_ALIVE, PID_UNKNOWN),
                      "a live but inaccessible process must not be reported 'gone'")


# --------------------------------------------------------------------------- #
# 2. duplicate-instance refusal
# --------------------------------------------------------------------------- #
class TestDuplicateInstanceRefused(DuplicateInstanceTestCase):
    def test_live_pid_with_running_state_is_detected(self):
        self.write_run_files(os.getpid(), "running")
        info = detect_running_instance()
        self.assertIsNotNone(info)
        self.assertEqual(info["pid"], os.getpid())
        self.assertEqual(info["run_id"], "paper-live")
        self.assertEqual(info["cycles"], 7)

    def test_message_is_readable_and_actionable(self):
        self.write_run_files(os.getpid(), "running", run_id="paper-20260914T094349Z-21992", cycles=7)
        message = describe_running_instance(detect_running_instance())
        first_line = message.splitlines()[0]
        self.assertRegex(
            first_line,
            r"^Bu bot zaten calisiyor \(pid \d+, run_id \S+, dongu \d+\)\. "
            r"Once durdurun: start-bot\.cmd stop$",
        )
        self.assertIn("paper.pid", message)

    def test_runner_setup_refuses_before_opening_the_ledger(self):
        self.write_run_files(os.getpid(), "running")
        before = state_path().read_text(encoding="utf-8")
        runner_obj = self.make_runner()
        with self.assertRaises(AlreadyRunningError) as ctx:
            runner_obj.setup()
        self.assertIn("zaten calisiyor", str(ctx.exception))
        self.assertIsNone(runner_obj.ledger, "the ledger must not be constructed")
        self.assertFalse(Path(self.config.db_path).exists(), "the ledger file must not be created")
        self.assertEqual(state_path().read_text(encoding="utf-8"), before,
                         "the run state must not be touched")
        self.assertEqual(read_pid(), os.getpid(), "the live pid file must be left alone")

    def test_cli_run_exits_busy_with_the_message_and_no_traceback(self):
        self.write_run_files(os.getpid(), "running")
        code, out, err = self.run_cli("run", *self.cfg(
            "--offline", "--replay", "--replay-bars", "150", "--cycles", "2", "--interval-seconds", "0"))
        self.assertEqual(code, cli.EXIT_BUSY)
        self.assertIn("Bu bot zaten calisiyor (pid {}, run_id paper-live, dongu 7)".format(os.getpid()), err)
        self.assertIn("start-bot.cmd stop", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("sqlite3", err)
        # Nothing was opened: no ledger file, no bot output, no run summary.
        self.assertFalse(Path(self.config.db_path).exists())
        self.assertNotIn("Paper run ozeti", out)

    def test_state_stopped_with_a_live_pid_is_not_treated_as_a_duplicate(self):
        """The state file -- not the pid file -- decides; a stopped run is over."""
        self.write_run_files(os.getpid(), "stopped")
        self.assertIsNone(detect_running_instance())
        self.assertIn("canli pid", clear_stale_state())     # kept, but reported

    def test_a_recycled_pid_does_not_match_the_state_pid(self):
        """Pid file outliving its run: state names a different process."""
        self.write_run_files(os.getpid(), "running", state_pid=os.getpid() + 1)
        self.assertIsNone(detect_running_instance())

    def test_uninspectable_process_does_not_block_a_start(self):
        """Another user's process (or an unreadable probe) must never lock us out."""
        self.write_run_files(424242, "running")
        with mock.patch.object(runner, "pid_liveness", return_value=PID_UNKNOWN):
            self.assertIsNone(detect_running_instance())
            note = clear_stale_state()
        # Not provably alive -> the stale record is cleared and the run proceeds;
        # the ledger-lock path (exit 4) is the backstop if a bot really is running.
        self.assertIn("Eski durum temizlendi", note)
        self.assertFalse(pid_path().exists())

    def test_stop_still_works_while_a_bot_is_running(self):
        self.write_run_files(os.getpid(), "running")
        code, out, _ = self.run_cli("stop")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Stop istegi", out)
        self.assertTrue((self.run_dir / "paper.stop").exists())
        self.assertEqual(read_pid(), os.getpid(), "stop must not disturb the live pid file")


# --------------------------------------------------------------------------- #
# 3. stale-state recovery
# --------------------------------------------------------------------------- #
class TestStaleStateRecovery(DuplicateInstanceTestCase):
    def test_dead_pid_with_running_state_is_cleared(self):
        self.write_run_files(DEAD_PID, "running")
        self.assertIsNone(detect_running_instance())
        note = clear_stale_state()
        self.assertIn("Eski durum temizlendi", note)
        self.assertIn(str(DEAD_PID), note)
        self.assertFalse(pid_path().exists())

    def test_stopped_state_with_a_dead_pid_is_cleared(self):
        self.write_run_files(DEAD_PID, "stopped")
        note = clear_stale_state()
        self.assertIn("eski kosu kaydi", note)
        self.assertFalse(pid_path().exists())

    def test_garbage_pid_file_is_cleared(self):
        pid_path().write_text("not-a-pid", encoding="utf-8")
        state_path().write_text('{"state": "running"}', encoding="utf-8")
        note = clear_stale_state()
        self.assertIn("gecersiz pid kaydi", note)
        self.assertFalse(pid_path().exists())

    def test_a_stale_pid_does_not_stop_a_bounded_run(self):
        self.write_run_files(DEAD_PID, "running")
        summary = self.make_runner().run()
        self.assertEqual(summary["cycles"], 2)
        self.assertTrue(summary["verification"]["ok"], summary["verification"])
        self.assertEqual(read_state()["state"], "finished")
        self.assertFalse(pid_path().exists(), "a finished run removes its pid file")

    def test_cli_run_reports_clearing_stale_state_and_succeeds(self):
        self.write_run_files(DEAD_PID, "running")
        code, out, err = self.run_cli("run", *self.cfg(
            "--offline", "--replay", "--replay-bars", "150", "--cycles", "2", "--interval-seconds", "0"))
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("Eski durum temizlendi", out)
        self.assertIn("Paper run ozeti", out)
        self.assertIn("mutabakat            PASS", out)


# --------------------------------------------------------------------------- #
# 4. a genuinely locked ledger must fail gracefully
# --------------------------------------------------------------------------- #
class TestLedgerLockIsReportedGracefully(DuplicateInstanceTestCase):
    """The lock is held by an *unrelated* connection: the duplicate guard passes
    (no pid/state files at all) and only the SQLite layer can detect it."""

    @contextlib.contextmanager
    def held_ledger(self, *, timeout: float = 0.25):
        """Create the ledger, then hold its write lock until the block exits.

        ``timeout`` shortens SQLite's busy retry window for the bot under test
        (production uses the sqlite3 default); the failure mode is identical.
        """
        holder = ledger_store.Ledger(self.config.db_path, "__lock_holder__")
        holder.close()
        connection = sqlite3.connect(str(self.config.db_path))
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR REPLACE INTO runs (run_id, mode, started_at, started_iso, version, "
            "config_json, data_json) VALUES ('__lock_holder__', 'backtest', 0, "
            "'1970-01-01T00:00:00Z', 'test', '{}', '{}')")
        real_connect = sqlite3.connect

        def fast_connect(*args, **kwargs):
            kwargs.setdefault("timeout", timeout)
            return real_connect(*args, **kwargs)

        try:
            with mock.patch.object(ledger_store.sqlite3, "connect", fast_connect):
                yield connection
        finally:
            connection.rollback()
            connection.close()

    def test_run_reports_a_locked_ledger_and_exits_busy(self):
        with self.held_ledger():
            code, out, err = self.run_cli("run", *self.cfg(
                "--offline", "--replay", "--replay-bars", "150", "--cycles", "2", "--interval-seconds", "0"))
        self.assertEqual(code, cli.EXIT_BUSY)
        self.assertIn("LEDGER KILITLI", err)
        self.assertIn("start-bot.cmd stop", err)
        self.assertIn("--db-path", err)
        self.assertIn("sqlite3.OperationalError: database is locked", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Traceback", out)

    def test_the_other_ledger_writer_is_reported_the_same_way(self):
        """``backtest`` writes the same ledger and must not traceback either."""
        with self.held_ledger():
            code, _out, err = self.run_cli("backtest", *self.cfg("--offline", "--no-chart", "--quiet"))
        self.assertEqual(code, cli.EXIT_BUSY)
        self.assertIn("LEDGER KILITLI", err)
        self.assertNotIn("Traceback", err)

    def test_the_duplicate_guard_does_not_claim_a_locked_ledger(self):
        """The lock path is reached only when no live instance owns the run dir."""
        with self.held_ledger():
            self.assertIsNone(detect_running_instance())
            self.assertIsNone(clear_stale_state())

    def test_lock_error_classification(self):
        self.assertTrue(cli.is_ledger_lock_error(sqlite3.OperationalError("database is locked")))
        self.assertTrue(cli.is_ledger_lock_error(sqlite3.OperationalError("database is busy")))
        self.assertFalse(cli.is_ledger_lock_error(sqlite3.OperationalError("no such table: runs")))

    def test_a_non_lock_operational_error_is_a_plain_failure(self):
        """An unusable ledger path is still a clean message, not a stack trace."""
        broken = self.root / "not-a-database"
        broken.mkdir()
        config_path = self.root / "broken.yaml"
        config_path.write_text(CONFIG_TEMPLATE.format(
            cache_dir=self.config.cache_dir, db_path=broken,
            reports_dir=self.config.reports_dir, logs_dir=self.config.logs_dir,
        ), encoding="utf-8")
        code, _out, err = self.run_cli("run", "--config", str(config_path), "--offline", "--quiet")
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertIn("LEDGER HATASI", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
