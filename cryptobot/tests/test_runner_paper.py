"""Paper runner: bounded cycles, replay, ledger consistency, stop request, feed outages."""

from __future__ import annotations

import dataclasses
import logging
import tempfile
import unittest
from pathlib import Path

from cryptobot.backtest.engine import TRADING_FAIL_SAFE
from cryptobot.data.feed import FeedTimeout, HttpResponse
from cryptobot.ledger.store import Ledger
from cryptobot.runner import (
    PaperRunner,
    RunnerConfig,
    clear_stop,
    read_state,
    request_stop,
    state_path,
    stop_path,
    stop_requested,
)

from .fixtures import FakeTransport, kline_row, klines_body, make_frame, seed_cache, tmp_config

PAIRS = ("BTC/USDT", "ETH/USDT")
BARS = 1_200
REPLAY_STEP = 200


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # addCleanup is LIFO: the temp dir is removed last, after every file handle
        # (log file, sqlite connection) has been released -- required on Windows.
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._close_log_handlers)

        base = tmp_config(Path(self.tmp.name), pairs=PAIRS, timeframe="15m")
        self.config = dataclasses.replace(
            base,
            strategy=dataclasses.replace(base.strategy, params={
                "bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_oversold": 35.0,
                "trend_sma_period": 100, "exit_at_middle_band": True,
            }),
        )
        self.frames = {
            "BTC/USDT": make_frame(BARS, step_ms=900_000, make_entries=True, seed=21),
            "ETH/USDT": make_frame(BARS, step_ms=900_000, start_price=2_000.0, make_entries=True, seed=22),
        }
        for pair, frame in self.frames.items():
            seed_cache(self.config, pair, frame)
        clear_stop()
        self.addCleanup(clear_stop)

    @staticmethod
    def _close_log_handlers() -> None:
        """Release the log file: Windows cannot delete a directory with an open handle."""
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def make_runner(self, **runner_kwargs) -> PaperRunner:
        kwargs = dict(cycles=5, offline=True, replay=True,
                      replay_bars_per_cycle=REPLAY_STEP, interval_seconds=0.0)
        kwargs.update(runner_kwargs)
        return PaperRunner(self.config, runner=RunnerConfig(**kwargs), run_id="paper-test",
                           sleep_fn=lambda _s: None, setup_logs=True)

    def latest_ledger(self) -> Ledger:
        ledger = Ledger(self.config.db_path, "paper-test")
        self.addCleanup(ledger.close)
        return ledger


class TestBoundedReplayRun(RunnerTestCase):
    def test_bounded_run_completes_and_reconciles(self):
        summary = self.make_runner().run()
        self.assertEqual(summary["cycles"], 5)
        self.assertFalse(summary["stopped_early"])
        self.assertGreater(summary["bars_processed"], 0)
        self.assertEqual(summary["trading_state"], "trading")
        self.assertTrue(summary["verification"]["ok"], summary["verification"])
        for check in summary["verification"]["checks"]:
            self.assertTrue(check["ok"], check)

    def test_ledger_rows_and_balance_consistency(self):
        summary = self.make_runner(cycles=6).run()
        ledger = self.latest_ledger()

        counts = ledger.counts()
        self.assertGreater(counts["ledger"], 0, "expected at least one fill event")
        self.assertEqual(counts["equity"], summary["bars_processed"])
        self.assertGreater(counts["trades"], 0, "replay should complete whole round trips")

        # Recompute the broker balance purely from the ledger.
        broker_cash = read_state()["broker"]["cash"]
        self.assertAlmostEqual(ledger.cash_from_ledger(self.config.initial_capital_usdt),
                               broker_cash, places=9)
        self.assertAlmostEqual(ledger.realized_net_from_ledger(),
                               read_state()["broker"]["realized_net_pnl"], places=9)

    def test_full_trade_cycles_are_recorded(self):
        summary = self.make_runner(cycles=6).run()
        ledger = self.latest_ledger()
        opens = [row for row in ledger.fetch_ledger() if row["event"] == "OPEN"]
        closes = [row for row in ledger.fetch_ledger() if row["event"] == "CLOSE"]
        self.assertGreaterEqual(len(closes), 3, "expected several full trade cycles")
        # A trailing position may still be open when the replay window ends.
        self.assertGreaterEqual(len(opens), len(closes))
        unmatched = len(opens) - len(closes)
        self.assertLessEqual(unmatched, 1)
        self.assertEqual(unmatched, int(read_state()["broker"]["open_positions"]))
        for row in closes:
            for key in ("ts", "pair", "side", "reference_price", "fill_price", "qty",
                        "notional", "fee", "gross_pnl", "net_pnl"):
                self.assertIsNotNone(row[key], (key, row))
            self.assertEqual(row["side"], "sell")
            self.assertNotEqual(row["net_pnl"], 0.0)

    def test_reports_and_state_are_written(self):
        summary = self.make_runner().run()
        self.assertTrue(Path(summary["daily_report"]).exists())
        self.assertTrue(Path(summary["log_file"]).exists())
        state = read_state()
        self.assertEqual(state["state"], "finished")
        self.assertEqual(state["run_id"], "paper-test")
        self.assertIn("risk_limits", state)
        self.assertAlmostEqual(state["risk_limits"]["gross_take_profit_pct"], 2.255332, places=4)
        self.assertEqual(state["safety"].split("|")[0].strip(), "mode: paper/backtest only")

    def test_daily_report_content(self):
        summary = self.make_runner(cycles=6).run()
        text = Path(summary["daily_report"]).read_text(encoding="utf-8")
        self.assertIn("Gunluk Rapor", text)
        self.assertIn("Mutabakat", text)
        self.assertIn("PASS", text)
        self.assertIn("Simulasyon ozetidir", text)

    def test_replay_is_deterministic(self):
        first = self.make_runner().run()
        second = self.make_runner().run()
        self.assertEqual(first["bars_processed"], second["bars_processed"])
        self.assertEqual(first["trades_closed"], second["trades_closed"])
        self.assertAlmostEqual(first["final_equity"], second["final_equity"], places=9)

    def test_stop_loss_take_profit_and_risk_limits_are_observable(self):
        self.make_runner(cycles=6).run()
        ledger = self.latest_ledger()
        codes = {event["code"] for event in ledger.fetch_events()}
        self.assertIn("paper_run_finished", codes)
        self.assertTrue(codes & {"entry_approved", "entry_taken"}, codes)

    def test_status_payload_reports_the_safety_posture(self):
        runner = self.make_runner()
        runner.setup()
        self.addCleanup(runner.ledger.close)
        payload = runner.status_payload()
        self.assertEqual(payload["mode"], "paper")
        self.assertIn("paper/backtest only", payload["safety"])
        self.assertIn("risk_limits", payload)
        self.assertIn("broker", payload)


class TestStopRequest(RunnerTestCase):
    def test_stop_sentinel_round_trip(self):
        self.assertFalse(stop_requested())
        path = request_stop("unit test")
        self.assertTrue(stop_requested())
        self.assertTrue(path.exists())
        self.assertIn("unit test", path.read_text(encoding="utf-8"))
        clear_stop()
        self.assertFalse(stop_requested())

    def test_running_loop_honours_a_pre_existing_stop_request(self):
        request_stop("stop before start")
        summary = self.make_runner(cycles=5).run()
        self.assertTrue(summary["stopped_early"])
        self.assertEqual(summary["cycles"], 0)
        self.assertEqual(read_state()["state"], "stopped")
        self.assertFalse(stop_path().exists(), "sentinel is cleared after the loop stops")

    def test_duration_bound_is_respected(self):
        summary = self.make_runner(cycles=None, duration_seconds=0.0001).run()
        self.assertLessEqual(summary["cycles"], 1)

    def test_state_file_is_written_even_with_no_cycles(self):
        request_stop()
        self.make_runner(cycles=3).run()
        self.assertTrue(state_path().exists())


class TestFeedOutage(RunnerTestCase):
    def test_total_outage_fails_safe_without_crashing(self):
        empty = Path(self.tmp.name) / "empty-cache"
        empty.mkdir()
        config = dataclasses.replace(self.config, data=dataclasses.replace(self.config.data, cache_dir=empty))
        runner = PaperRunner(
            config,
            runner=RunnerConfig(cycles=2, offline=False, interval_seconds=0.0),
            run_id="paper-outage",
            transport=FakeTransport([FeedTimeout("down") for _ in range(120)]),
            sleep_fn=lambda _s: None,
            setup_logs=False,
        )
        summary = runner.run()
        self.assertEqual(summary["cycles"], 2)
        self.assertEqual(summary["trades_closed"], 0)
        self.assertEqual(summary["bars_processed"], 0)
        self.assertTrue(summary["cycle_reports"])
        self.assertTrue(all(report["warnings"] for report in summary["cycle_reports"]))

    def test_corrupt_cache_is_quarantined_and_refetched(self):
        from cryptobot.data.feed import cache_path

        path = cache_path(self.config.data.cache_dir, "BTC/USDT", "15m")
        path.write_text("not,a,valid,cache\n", encoding="utf-8")
        frame = self.frames["BTC/USDT"].iloc[:400]
        transport = FakeTransport([HttpResponse(200, klines_body([
            kline_row(int(ts), float(close))
            for ts, close in zip(frame["ts"].tolist(), frame["close"].tolist())
        ]))])
        runner = PaperRunner(
            self.config,
            runner=RunnerConfig(cycles=1, offline=False, interval_seconds=0.0),
            run_id="paper-corrupt",
            transport=transport,
            sleep_fn=lambda _s: None,
            setup_logs=False,
        )
        summary = runner.run()
        self.assertEqual(summary["cycles"], 1)
        self.assertTrue(any(p.name.endswith(".corrupt") for p in path.parent.iterdir()))


class TestStaleCacheOutage(RunnerTestCase):
    """Defect 3: a stale cache combined with an outage must block new entries.

    The runner passes ``realtime=True`` for a live paper cycle, so the stale
    cache is refreshed: the simulated outage turns it into
    ``source='cache-stale', complete=False`` and the engine pauses entries.
    """

    def test_stale_cache_plus_outage_opens_no_positions_and_is_persisted(self):
        # The seeded frames end in 2020, so the real-time staleness bound trips.
        runner = PaperRunner(
            self.config,
            runner=RunnerConfig(cycles=2, offline=False, replay=False, interval_seconds=0.0),
            run_id="paper-stale",
            transport=FakeTransport([FeedTimeout("down") for _ in range(400)]),
            sleep_fn=lambda _s: None,
            setup_logs=False,
        )
        summary = runner.run()
        self.assertEqual(summary["cycles"], 2)
        self.assertEqual(summary["trades_closed"], 0)
        self.assertEqual(summary["trading_state"], TRADING_FAIL_SAFE)
        self.assertTrue(all("stale" in report["sources"][pair]
                            for report in summary["cycle_reports"] for pair in PAIRS))

        ledger = Ledger(self.config.db_path, "paper-stale")
        self.addCleanup(ledger.close)
        counts = ledger.counts()
        self.assertEqual(counts["ledger"], 0, "no OPEN/CLOSE fills may be written")
        codes = {event["code"] for event in ledger.fetch_events()}
        self.assertIn("pause_new_entries", codes)   # engine fail-safe is persisted
        self.assertIn("cache_stale", codes)         # runner feed event is persisted


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
