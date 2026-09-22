"""Monitoring: structured JSON logs and the daily Markdown report."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from cryptobot.monitor.daily_report import build_daily_report, write_daily_report
from cryptobot.monitor.logging_setup import JsonLineFormatter, get_logger, log_event, setup_logging

from .fixtures import tmp_config


class TestStructuredLogging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # LIFO cleanups: close the log file handles before the temp dir is removed.
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._close_handlers)
        # Re-enable logging for this module: the test package scopes its silencing
        # to the cryptobot logger (NullHandler + propagate=False) instead of the
        # old process-wide logging.disable().
        self._saved_level = logging.getLogger("cryptobot").level
        self._saved_propagate = logging.getLogger("cryptobot").propagate
        logging.getLogger("cryptobot").setLevel(logging.NOTSET)
        logging.getLogger("cryptobot").propagate = True

    def _close_handlers(self):
        logger = logging.getLogger("cryptobot")
        logger.setLevel(self._saved_level)
        logger.propagate = self._saved_propagate
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def test_setup_logging_writes_json_lines(self):
        log_dir = Path(self.tmp.name) / "logs"
        path = setup_logging(log_dir, level="INFO", run_id="unit", console=False)
        self.assertTrue(path.exists())
        logger = get_logger("cryptobot.tests.monitor")
        logger.info("order.buy", extra={"event": "order_buy", "pair": "BTC/USDT", "qty": 0.001,
                                        "fill_price": 30_000.5})
        log_event(logger, "risk_limit", "daily loss limit hit", level="warning", code="daily_loss_limit_reached")

        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2)
        records = [json.loads(line) for line in lines]
        self.assertEqual(records[0]["event"], "logging_ready")
        buy = next(record for record in records if record.get("event") == "order_buy")
        self.assertEqual(buy["pair"], "BTC/USDT")
        self.assertAlmostEqual(buy["fill_price"], 30_000.5)
        self.assertEqual(buy["level"], "INFO")
        limit = next(record for record in records if record.get("event") == "risk_limit")
        self.assertEqual(limit["level"], "WARNING")
        self.assertEqual(limit["code"], "daily_loss_limit_reached")

    def test_formatter_serialises_extras_and_exceptions(self):
        formatter = JsonLineFormatter()
        record = logging.LogRecord("cryptobot.test", logging.ERROR, __file__, 10,
                                   "boom %s", ("now",), None)
        record.event = "engine_event"
        record.code = "close_rejected"
        payload = json.loads(formatter.format(record))
        self.assertEqual(payload["event"], "engine_event")
        self.assertEqual(payload["message"], "boom now")
        self.assertEqual(payload["code"], "close_rejected")
        self.assertEqual(payload["level"], "ERROR")

    def test_console_handler_can_be_enabled_and_disabled(self):
        log_dir = Path(self.tmp.name) / "logs3"
        with_console = setup_logging(log_dir, level="INFO", filename="with_console.log", console=True)
        names = {handler.get_name() for handler in logging.getLogger().handlers}
        self.assertIn("cryptobot.console", names)
        self.assertIn("cryptobot.file", names)
        without = setup_logging(log_dir, level="INFO", filename="no_console.log", console=False)
        names = {handler.get_name() for handler in logging.getLogger().handlers}
        self.assertNotIn("cryptobot.console", names)
        self.assertTrue(with_console.exists() and without.exists())

    def test_log_file_is_valid_utf8_json(self):
        path = setup_logging(Path(self.tmp.name) / "logs2", level="DEBUG", console=False)
        get_logger("cryptobot.tests.monitor").debug(
            "unicode", extra={"event": "unicode_event", "detail": "kar / zarar - %2 hedef"}
        )
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)


class TestDailyReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = tmp_config(Path(self.tmp.name))
        self.day = "2024-05-01"
        self.trades = [
            {"pair": "BTC/USDT", "entry_ts": 1_714_536_000_000, "exit_ts": 1_714_539_600_000,
             "qty": 0.001, "gross_pnl": 0.75, "net_pnl": 0.69, "net_pnl_pct": 2.3, "fees": 0.06,
             "iso_utc": "2024-05-01T10:00:00Z", "exit_reason": "take_profit:take-profit hit"},
            {"pair": "ETH/USDT", "entry_ts": 1_714_540_000_000, "exit_ts": 1_714_543_600_000,
             "qty": 0.02, "gross_pnl": -1.1, "net_pnl": -1.2, "net_pnl_pct": -2.6, "fees": 0.1,
             "iso_utc": "2024-05-01T14:00:00Z", "exit_reason": "stop_loss:stop-loss hit"},
        ]
        self.equity = [
            {"ts": 1_714_536_000_000, "iso_utc": "2024-05-01T01:00:00Z", "cash": 20.0,
             "positions_value": 30.0, "equity": 50.0, "open_positions": 1, "realized_net_pnl": 0.0},
            {"ts": 1_714_570_000_000, "iso_utc": "2024-05-01T23:00:00Z", "cash": 49.49,
             "positions_value": 0.0, "equity": 49.49, "open_positions": 0, "realized_net_pnl": -0.51},
        ]
        self.events = [
            {"ts": 1_714_550_000_000, "iso_utc": "2024-05-01T16:00:00Z", "level": "WARNING",
             "category": "risk", "code": "cooldown_started",
             "message": "losing trade -> no new entry for 60 min"},
            {"ts": 1_714_550_100_000, "iso_utc": "2024-05-01T16:00:01Z", "level": "INFO",
             "category": "runner", "code": "paper_run_finished", "message": "done"},
        ]
        self.orders = [
            {"ts": 1_714_551_000_000, "iso_utc": "2024-05-01T16:10:00Z", "pair": "BTC/USDT",
             "side": "buy", "status": "rejected", "requested_qty": 1.0, "filled_qty": 0.0,
             "reason": "insufficient_balance"},
        ]
        self.broker = {"cash": 49.49, "equity": 49.49, "open_positions": 0,
                       "realized_net_pnl": -0.51, "unrealized_net_pnl": 0.0}

    def _build(self) -> str:
        return build_daily_report(
            day=self.day, config=self.config, broker_snapshot=self.broker, trades=self.trades,
            equity_points=self.equity, events=self.events, orders=self.orders,
            reconciliation={"ok": True, "tolerance": 1e-6,
                            "checks": [{"name": "cash_from_ledger == broker.cash", "ok": True,
                                        "ledger": 49.49, "broker": 49.49, "delta": 0.0}]},
            risk_state={"halted": False, "day": self.day, "trades_today": 2},
            run_id="paper-unit",
        )

    def test_report_contains_every_section(self):
        text = self._build()
        for expected in ("# Gunluk Rapor -- 2024-05-01", "## Ozet", "## Risk durumu",
                         "## Gunun islemleri", "## Reddedilen / kismi emirler", "## Olaylar",
                         "## Mutabakat", "BTC/USDT", "ETH/USDT", "take_profit", "stop_loss",
                         "insufficient_balance", "cooldown_started", "PASS"):
            self.assertIn(expected, text)

    def test_summary_numbers(self):
        text = self._build()
        self.assertIn("| Kapanan islem | 2 |", text)
        self.assertIn("| Kazanan islem | 1 |", text)
        self.assertIn("| Kazanma orani | 50.00% |", text)

    def test_only_the_requested_day_is_included(self):
        other = dict(self.trades[0])
        other["iso_utc"] = "2024-05-02T10:00:00Z"
        text = build_daily_report(
            day=self.day, config=self.config, broker_snapshot=self.broker,
            trades=self.trades + [other], equity_points=self.equity, events=[], orders=[],
        )
        self.assertIn("| Kapanan islem | 2 |", text)

    def test_writes_the_file(self):
        path = write_daily_report(
            self.config.reports_dir, day=self.day, config=self.config, broker_snapshot=self.broker,
            trades=self.trades, equity_points=self.equity, events=self.events, orders=self.orders,
        )
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "daily_2024-05-01.md")
        self.assertIn("paper trading", path.read_text(encoding="utf-8"))

    def test_empty_day_is_handled(self):
        text = build_daily_report(
            day="2030-01-01", config=self.config, broker_snapshot=self.broker, trades=[],
            equity_points=[], events=[], orders=[],
        )
        self.assertIn("_Bugun kapanan islem yok._", text)
        self.assertIn("| Kazanma orani | n/a |", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
