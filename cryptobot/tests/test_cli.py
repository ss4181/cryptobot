"""CLI: argument parsing, exit codes, safety rejection and end-to-end subcommands."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptobot import cli
from cryptobot.data.feed import cache_path
from cryptobot.ledger.store import Ledger

from .fixtures import make_frame, save_cache, tmp_config

EXIT_OK, EXIT_FAILURE, EXIT_USAGE, EXIT_SAFETY = 0, 1, 2, 3

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


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._close_handlers)

        self.root = Path(self.tmp.name)
        self.config = tmp_config(self.root, pairs=("BTC/USDT", "ETH/USDT"), timeframe="15m")
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(CONFIG_TEMPLATE.format(
            cache_dir=self.config.cache_dir,
            db_path=self.config.db_path,
            reports_dir=self.config.reports_dir,
            logs_dir=self.config.logs_dir,
        ), encoding="utf-8")

        # Keep the runner's pid/state/stop files out of the package directory.
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self._env = mock.patch.dict(os.environ, {"CRYPTOBOT_RUN_DIR": str(self.run_dir)})
        self._env.start()
        self.addCleanup(self._env.stop)

        for pair, seed in (("BTC/USDT", 31), ("ETH/USDT", 32)):
            frame = make_frame(900, step_ms=900_000, make_entries=True, seed=seed)
            save_cache(frame, cache_path(self.config.cache_dir, pair, "15m"))

    @staticmethod
    def _close_handlers() -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def run_cli(self, *argv):
        """Invoke the CLI in-process, capturing stdout/stderr and the exit code."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def cfg(self, *extra):
        return ("--config", str(self.config_path), *extra)


class TestParser(CliTestCase):
    def test_parser_builds_every_subcommand(self):
        parser = cli.build_parser()
        for command in ("download", "backtest", "run", "status", "report", "export", "verify", "stop", "safety"):
            with self.subTest(command=command):
                args = parser.parse_args([command])
                self.assertTrue(callable(args.func))

    def test_unknown_command_exits_with_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.build_parser().parse_args(["frobnicate"])
        self.assertEqual(ctx.exception.code, EXIT_USAGE)

    def test_run_bounds_are_parsed(self):
        args = cli.build_parser().parse_args(
            ["run", "--cycles", "3", "--duration-seconds", "5", "--replay", "--replay-bars", "100", "--faults", "demo"])
        self.assertEqual(args.cycles, 3)
        self.assertEqual(args.duration_seconds, 5.0)
        self.assertTrue(args.replay)
        self.assertEqual(args.replay_bars, 100)
        self.assertEqual(args.faults, "demo")


class TestSafetyCommands(CliTestCase):
    def test_safety_command_reports_the_posture(self):
        code, out, _ = self.run_cli("safety")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("paper/backtest only", out)
        self.assertIn("ENGELLENDI", out)

    def test_live_mode_is_rejected_with_the_safety_exit_code(self):
        for mode in ("live", "real", "production"):
            with self.subTest(mode=mode):
                code, _, err = self.run_cli("backtest", *self.cfg("--mode", mode))
                self.assertEqual(code, EXIT_SAFETY)
                self.assertIn("GUVENLIK IHLALI", err)

    def test_live_env_kill_switch_is_rejected(self):
        with mock.patch.dict(os.environ, {"CRYPTOBOT_LIVE": "1"}):
            code, _, err = self.run_cli("backtest", *self.cfg())
        self.assertEqual(code, EXIT_SAFETY)
        self.assertIn("GUVENLIK", err)

    def test_invalid_config_returns_usage_error(self):
        code, _, err = self.run_cli("backtest", *self.cfg("--timeframe", "9m"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("KONFIGURASYON HATASI", err)

    def test_credentials_in_the_environment_are_only_warned_about(self):
        with mock.patch.dict(os.environ, {"BINANCE_API_KEY": "not-used"}):
            code, out, _ = self.run_cli("safety")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("BINANCE_API_KEY", out)


class TestBacktestCommand(CliTestCase):
    def test_offline_backtest_writes_reports(self):
        code, out, err = self.run_cli("backtest", *self.cfg("--offline", "--no-chart", "--quiet"))
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("Backtest ozeti", out)
        reports = sorted(p.name for p in Path(self.config.reports_dir).iterdir())
        self.assertIn("backtest_BTCUSDT-ETHUSDT_15m.json", reports)
        self.assertIn("backtest_BTCUSDT-ETHUSDT_15m.md", reports)
        payload = json.loads((Path(self.config.reports_dir) / "backtest_BTCUSDT-ETHUSDT_15m.json")
                             .read_text(encoding="utf-8"))
        self.assertIn("determinism_hash", payload)
        self.assertIn("trade_count", payload["metrics"])

    def test_missing_cache_fails_closed_with_hint(self):
        empty = self.root / "empty"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"CRYPTOBOT_CACHE_DIR": str(empty)}):
            code, out, _ = self.run_cli("backtest", *self.cfg("--offline", "--no-chart", "--quiet"))
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("VERI HATASI", out)
        self.assertIn("download", out)

    def test_json_stdout_flag(self):
        code, out, err = self.run_cli("backtest", *self.cfg("--offline", "--no-chart", "--quiet", "--json-stdout"))
        self.assertEqual(code, EXIT_OK, err)
        blob = out[out.index("{"):]
        payload = json.loads(blob)
        self.assertEqual(payload["determinism_hash"], payload["determinism_hash"].lower())
        self.assertEqual(payload["metrics"]["pairs"], ["BTC/USDT", "ETH/USDT"])


class TestRunAndStatusCommands(CliTestCase):
    def test_bounded_replay_run_then_status(self):
        code, out, err = self.run_cli(
            "run", *self.cfg("--offline", "--replay", "--replay-bars", "150", "--cycles", "4",
                             "--interval-seconds", "0", "--quiet"))
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("Paper run ozeti", out)
        self.assertIn("mutabakat            PASS", out)

        code, out, err = self.run_cli("status", *self.cfg())
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("state          : finished", out)
        self.assertIn("-- risk limitleri --", out)
        self.assertIn("gross_take_profit_pct", out)

    def test_status_without_state_is_friendly(self):
        code, out, _ = self.run_cli("status", *self.cfg())
        self.assertEqual(code, EXIT_OK)
        self.assertIn("durumu bulunamadi", out)

    def test_stop_writes_the_sentinel(self):
        code, out, _ = self.run_cli("stop")
        self.assertEqual(code, EXIT_OK)
        self.assertTrue((self.run_dir / "paper.stop").exists())
        self.assertIn("Stop istegi", out)


class TestLedgerCommands(CliTestCase):
    def _backtest(self):
        code, _, err = self.run_cli("backtest", *self.cfg("--offline", "--no-chart", "--quiet"))
        self.assertEqual(code, EXIT_OK, err)

    def test_verify_passes_for_the_backtest_run(self):
        self._backtest()
        code, out, _ = self.run_cli("verify", *self.cfg())
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("Ledger reconciliation: PASS", out)

    def test_verify_reports_missing_ledger(self):
        code, out, _ = self.run_cli("verify", *self.cfg("--run-id", "nope"))
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("Ledger bulunamadi", out)

    def test_export_writes_csv_and_json(self):
        self._backtest()
        out_dir = self.root / "exports"
        code, out, err = self.run_cli("export", *self.cfg("--out", str(out_dir)))
        self.assertEqual(code, EXIT_OK, err)
        for name in ("ledger.csv", "trades.csv", "equity.csv", "events.csv", "orders.csv"):
            self.assertTrue((out_dir / name).exists(), name)
        json_files = list(out_dir.glob("ledger_*.json"))
        self.assertEqual(len(json_files), 1)
        payload = json.loads(json_files[0].read_text(encoding="utf-8"))
        self.assertIn("ledger", payload)

    def test_report_writes_the_daily_markdown(self):
        self._backtest()
        code, out, err = self.run_cli("report", *self.cfg())
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("gunluk rapor yazildi", out)
        reports = list(Path(self.config.reports_dir).glob("daily_*.md"))
        self.assertEqual(len(reports), 1)
        self.assertIn("Mutabakat", reports[0].read_text(encoding="utf-8"))

    def test_verify_uses_the_ledger_only(self):
        self._backtest()
        ledger = Ledger(self.config.db_path, "__check__")
        self.addCleanup(ledger.close)
        run_id = ledger.latest_run_id()
        self.assertTrue(run_id.startswith("backtest-"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
