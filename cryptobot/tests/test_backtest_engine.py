"""Backtest engine: determinism, cost impact, no look-ahead, data fail-safe."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from cryptobot.backtest.engine import TRADING_FAIL_SAFE, TRADING_OK, BacktestEngine
from cryptobot.backtest.report import report_basename, write_json_report, write_reports
from cryptobot.execution.paper_broker import FaultInjector

from .fixtures import make_frame, tmp_config

PAIRS = ["BTC/USDT", "ETH/USDT"]


def frames(n: int = 420) -> dict:
    return {
        "BTC/USDT": make_frame(n, make_entries=True, seed=11),
        "ETH/USDT": make_frame(n, start_price=2_000.0, make_entries=True, seed=12),
    }


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = tmp_config(Path(self.tmp.name), pairs=tuple(PAIRS))
        self.config = dataclasses.replace(
            self.config,
            strategy=dataclasses.replace(
                self.config.strategy, params={**self.config.strategy.params, "trend_sma_period": 100}
            ),
        )

    def engine(self, **kwargs) -> BacktestEngine:
        return BacktestEngine(self.config, run_id="t1", **kwargs)


class TestDeterminism(EngineTestCase):
    def test_two_runs_produce_identical_metrics_and_hash(self):
        first = self.engine().run(frames())
        second = self.engine().run(frames())
        self.assertEqual(first.metrics, second.metrics)
        self.assertEqual(first.determinism_hash, second.determinism_hash)
        self.assertEqual(
            json.dumps(first.metrics_payload(), sort_keys=True),
            json.dumps(second.metrics_payload(), sort_keys=True),
        )

    def test_trades_are_identical(self):
        first = self.engine().run(frames())
        second = self.engine().run(frames())
        self.assertEqual(
            [t["net_pnl"] for t in first.trades],
            [t["net_pnl"] for t in second.trades],
        )
        self.assertEqual([t["entry_ts"] for t in first.trades], [t["entry_ts"] for t in second.trades])

    def test_report_json_is_byte_identical(self):
        out = Path(self.tmp.name) / "reports"
        first = self.engine().run(frames())
        second = self.engine().run(frames())
        path_a = write_json_report(first, out, "one")
        path_b = write_json_report(second, out, "two")
        self.assertEqual(path_a.read_bytes(), path_b.read_bytes())

    def test_timestamps_are_absent_from_the_deterministic_payload(self):
        payload = self.engine().run(frames()).metrics_payload()
        blob = json.dumps(payload, sort_keys=True)
        self.assertNotIn("generated_at", blob)
        self.assertNotIn("run_id", blob)


class TestEngineBehaviour(EngineTestCase):
    def test_runs_and_trades(self):
        result = self.engine().run(frames())
        self.assertGreater(result.bars_processed, 0)
        self.assertEqual(result.metrics["trade_count"], len(result.trades))
        self.assertEqual(result.metrics["bars"], result.bars_processed)
        self.assertEqual(len(result.equity_curve), result.bars_processed)

    def test_costs_reduce_performance(self):
        free = dataclasses.replace(self.config, fee_pct=0.0, slippage_pct=0.0)
        costly = dataclasses.replace(self.config, fee_pct=0.2, slippage_pct=0.1)
        free_metrics = BacktestEngine(free, run_id="free").run(frames()).metrics
        costly_metrics = BacktestEngine(costly, run_id="costly").run(frames()).metrics
        self.assertEqual(free_metrics["trade_count"], costly_metrics["trade_count"])
        self.assertGreater(free_metrics["net_pnl_usdt"], costly_metrics["net_pnl_usdt"])
        self.assertEqual(free_metrics["total_fees_usdt"], 0.0)
        self.assertGreater(costly_metrics["total_fees_usdt"], 0.0)

    def test_net_target_is_wired_into_the_take_profit(self):
        result = self.engine().run(frames())
        self.assertAlmostEqual(result.final_snapshot["stats"]["submitted"] > 0, True)
        engine = self.engine()
        self.assertAlmostEqual(engine.risk.gross_tp_pct, 2.2553318701392433, places=9)

    def test_no_same_bar_exit(self):
        result = self.engine().run(frames())
        self.assertTrue(result.trades, "expected at least one trade")
        for trade in result.trades:
            self.assertNotEqual(trade["entry_ts"], trade["exit_ts"])
            self.assertGreater(trade["exit_ts"], trade["entry_ts"])
            self.assertGreaterEqual(trade["bars_held"], 1)

    def test_equity_matches_cash_plus_realized_when_flat(self):
        result = self.engine().run(frames())
        if result.final_snapshot["open_positions"] == 0:
            self.assertAlmostEqual(
                result.final_snapshot["equity"],
                result.final_snapshot["cash"] + result.final_snapshot["realized_net_pnl"] + 50.0
                - self.config.initial_capital_usdt,
                places=6,
            )

    def test_position_sizing_respects_max_position_pct(self):
        result = self.engine().run(frames())
        for trade in result.trades:
            notional = trade["entry_fill_price"] * trade["qty"]
            # never more than max_position_pct of the equity at entry (plus fee head-room)
            self.assertLessEqual(notional, self.config.initial_capital_usdt * 1.01)

    def test_stop_loss_and_take_profit_reasons_appear(self):
        result = self.engine().run(frames())
        reasons = {trade["exit_reason"].split(":")[0] for trade in result.trades}
        self.assertTrue(reasons <= {"stop_loss", "take_profit", "strategy"}, reasons)

    def test_fault_injection_rejections_do_not_break_the_run(self):
        faults = FaultInjector().enable()
        faults.reject_next("buy", 1).partial_next("buy", 0.5)
        result = self.engine(faults=faults).run(frames())
        self.assertGreater(result.final_snapshot["stats"]["rejected"], 0)
        self.assertGreater(result.final_snapshot["stats"]["partial"], 0)

    def test_missing_frame_raises(self):
        with self.assertRaises(KeyError):
            self.engine().run({"BTC/USDT": frames()["BTC/USDT"]})

    def test_empty_frame_raises(self):
        bad = frames()
        bad["ETH/USDT"] = bad["ETH/USDT"].iloc[:0]
        with self.assertRaises(ValueError):
            self.engine().run(bad)


class TestDataFailSafe(EngineTestCase):
    def test_incomplete_data_pauses_new_entries(self):
        meta = {pair: {"source": "cache-stale", "complete": False, "rows": 420} for pair in PAIRS}
        engine = self.engine()
        result = engine.run(frames(), data_meta=meta)
        self.assertEqual(engine.trading_state, TRADING_FAIL_SAFE)
        self.assertEqual(result.metrics["trade_count"], 0)
        self.assertTrue(result.warnings)
        self.assertTrue(any(event["code"] == "pause_new_entries" for event in result.events))

    def test_complete_data_trades_normally(self):
        meta = {pair: {"source": "network", "complete": True, "rows": 420} for pair in PAIRS}
        engine = self.engine()
        result = engine.run(frames(), data_meta=meta)
        self.assertEqual(engine.trading_state, TRADING_OK)
        self.assertGreater(result.metrics["trade_count"], 0)

    def test_pause_event_is_persisted_to_the_ledger(self):
        """The fail-safe must reach the ledger, not only the log / engine.events."""
        from cryptobot.ledger.store import Ledger

        ledger = Ledger(self.config.db_path, "engine-failsafe")
        self.addCleanup(ledger.close)
        meta = {pair: {"source": "cache-stale", "complete": False, "rows": 420} for pair in PAIRS}
        engine = BacktestEngine(self.config, ledger=ledger, run_id="engine-failsafe")
        engine.run(frames(), data_meta=meta)

        pause = [event for event in ledger.fetch_events() if event["code"] == "pause_new_entries"]
        self.assertEqual(len(pause), 1, ledger.fetch_events())
        self.assertEqual(pause[0]["category"], "engine")
        self.assertEqual(pause[0]["level"], "CRITICAL")


class TestReports(EngineTestCase):
    def test_all_report_artifacts_are_written(self):
        out = Path(self.tmp.name) / "reports"
        result = self.engine().run(frames())
        paths = write_reports(result, out, basename=report_basename(self.config))
        self.assertEqual(set(paths), {"json", "markdown", "chart"})
        for path in paths.values():
            self.assertTrue(path.exists(), path)
        markdown = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn("Backtest Raporu", markdown)
        self.assertIn("UYARI", markdown)
        self.assertIn("Model varsayimlari", markdown)
        payload = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertIn("metrics", payload)
        self.assertIn("determinism_hash", payload)
        self.assertEqual(payload["determinism_hash"], result.determinism_hash)

    def test_chart_can_be_skipped(self):
        out = Path(self.tmp.name) / "reports2"
        result = self.engine().run(frames())
        paths = write_reports(result, out, basename="nochart", with_chart=False)
        self.assertNotIn("chart", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
