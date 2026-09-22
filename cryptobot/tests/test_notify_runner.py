"""Notifications: engine->event mapping and end-to-end runner isolation."""

from __future__ import annotations

import dataclasses
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptobot.backtest.engine import BacktestEngine
from cryptobot.cli import _apply_notify_flags
from cryptobot.notify import NotifyConfig, Notifier
from cryptobot.notify import events
from cryptobot.notify.mapping import from_engine
from cryptobot.notify.store import NotificationStore
from cryptobot.notify.watcher import EquityDropMonitor
from cryptobot.runner import PaperRunner, RunnerConfig

from .fixtures import make_frame, seed_cache, tmp_config
from .notify_fixtures import BrokenProvider, CapturingNotifier, ExplodingNotifier, RecordingProvider


class TestEngineEventMapping(unittest.TestCase):
    def test_position_opened(self):
        event = from_engine({"type": "position_opened", "ts": 1, "pair": "BTC/USDT",
                             "entry_price": 100.0, "qty": 0.5, "notional": 50.0,
                             "stop_price": 97.5, "tp_price": 102.26}, run_id="r")
        self.assertEqual(event.type, "position_opened")
        self.assertEqual(event.resolved_severity(), "info")
        self.assertIn("BTCUSDT", event.title)
        self.assertIn("🟦 BTCUSDT · LONG (PAPER)", event.body)
        self.assertIn("100,00", event.body)
        self.assertIn("102,26", event.body)
        self.assertEqual(event.run_id, "r")

    def test_position_closed_carries_fees_and_pnl(self):
        event = from_engine({"type": "position_closed", "ts": 2, "pair": "ETH/USDT",
                             "entry_price": 2000.0, "exit_price": 2045.2, "qty": 0.02,
                             "fees": 0.09, "gross_pnl": 0.904, "net_pnl": 0.814,
                             "net_pnl_pct": 2.0, "trigger": "take_profit"}, run_id="r")
        self.assertEqual(event.type, "position_closed")
        # entry -> exit, fees, gross, net USDT and net %, why, how long.
        for fragment in ("2.000,00", "2.045,20", "0,0900", "+0,9040", "+0,8140",
                         "+2,00%", "hedef (take-profit)"):
            self.assertIn(fragment, event.body)

    def test_protective_exit_mapping_and_severity(self):
        payload = {"type": "take_profit_hit", "ts": 3, "pair": "BTC/USDT", "entry_price": 1.0,
                   "exit_price": 1.02, "net_pnl": 1.0, "net_pnl_pct": 2.0}
        self.assertEqual(from_engine(payload).resolved_severity(), "info")
        payload["type"] = "stop_loss_hit"
        payload["net_pnl"] = -1.25
        payload["net_pnl_pct"] = -2.74
        stop = from_engine(payload)
        self.assertEqual(stop.resolved_severity(), "warning")
        self.assertIn("STOP-LOSS", stop.title)

    def test_risk_events(self):
        halted = from_engine({"type": "risk_halted", "ts": 4, "reason": "daily loss limit hit",
                              "day_realized_net_pnl": -2.6}, run_id="r")
        cooldown = from_engine({"type": "cooldown_started", "ts": 5, "pair": "BTC/USDT",
                                "net_pnl": -0.3, "cooldown_minutes": 60}, run_id="r")
        fail_safe = from_engine({"type": "data_fail_safe", "ts": 6, "reason": "incomplete data"}, run_id="r")
        self.assertEqual(halted.resolved_severity(), "critical")
        self.assertIn("gunluk zarar limiti", halted.title)
        self.assertEqual(cooldown.resolved_severity(), "warning")
        self.assertEqual(fail_safe.resolved_severity(), "warning")

    def test_unknown_payload_is_ignored(self):
        self.assertIsNone(from_engine({"type": "order_filled"}, run_id="r"))
        self.assertIsNone(from_engine(None))


class TestEquityDropMonitor(unittest.TestCase):
    def test_triggers_once_per_episode_and_rearms_on_new_high(self):
        monitor = EquityDropMonitor(3.0)
        self.assertIsNone(monitor.update(50.0))                    # sets the peak
        self.assertIsNone(monitor.update(49.0))                    # -2%: below threshold
        triggered = monitor.update(48.0)                           # -4%: fires
        self.assertIsNotNone(triggered)
        peak, drop_pct = triggered
        self.assertAlmostEqual(peak, 50.0)
        self.assertAlmostEqual(drop_pct, 4.0)
        self.assertIsNone(monitor.update(47.0))                    # same episode: silent
        self.assertIsNone(monitor.update(51.0))                    # new high re-arms
        self.assertIsNotNone(monitor.update(49.0))

    def test_disabled_threshold_never_triggers(self):
        self.assertIsNone(EquityDropMonitor(0.0).update(10.0))
        self.assertIsNone(EquityDropMonitor(5.0, enabled=False).update(1.0))


class TestRunnerNotificationFlags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = tmp_config(Path(self.tmp.name))

    def test_no_notify_disables_the_layer(self):
        updated = _apply_notify_flags(self.config, SimpleNamespace(no_notify=True, notify_dry_run=False))
        self.assertFalse(updated.notifications.enabled)

    def test_notify_dry_run_forces_dry_run_on(self):
        updated = _apply_notify_flags(self.config, SimpleNamespace(no_notify=False, notify_dry_run=True))
        self.assertTrue(updated.notifications.enabled)
        self.assertTrue(updated.notifications.dry_run)

    def test_no_flags_leaves_the_config_untouched(self):
        updated = _apply_notify_flags(self.config, SimpleNamespace())
        self.assertIs(updated, self.config)


class RunnerNotificationCase(unittest.TestCase):
    """Shared offline runner setup: one synthetic pair with a guaranteed entry."""

    BARS = 500

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._close_log_handlers)
        base = tmp_config(Path(self.tmp.name), pairs=("BTC/USDT",), timeframe="1h")
        self.config = dataclasses.replace(
            base,
            strategy=dataclasses.replace(base.strategy, params={
                "bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_oversold": 35.0,
                "trend_sma_period": 100, "exit_at_middle_band": True,
            }),
        )
        seed_cache(self.config, "BTC/USDT", make_frame(self.BARS, make_entries=True, seed=21))

    @staticmethod
    def _close_log_handlers() -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def runtime_config(self, **overrides) -> NotifyConfig:
        base = NotifyConfig(
            enabled=True, providers=("console",), notify_on=tuple(events.EVENT_TYPES),
            dedupe_window_seconds=0, max_per_hour=100000, retry_max=0,
            store_path=Path(self.tmp.name) / "logs" / "notifications.jsonl",
        )
        return dataclasses.replace(base, **overrides)

    def runner(self, **kwargs) -> PaperRunner:
        notifier = kwargs.pop("notifier", None)
        settings = dict(cycles=1, offline=True, replay=True,
                        replay_bars_per_cycle=self.BARS, interval_seconds=0.0)
        settings.update(kwargs)
        return PaperRunner(self.config, runner=RunnerConfig(**settings), run_id="notify-runner-test",
                           sleep_fn=lambda _s: None, setup_logs=True, notifier=notifier)


class TestRunnerEmitsLifecycleEvents(RunnerNotificationCase):
    def test_position_and_run_events_reach_the_notifier(self):
        capture = CapturingNotifier()
        summary = self.runner(notifier=capture).run()
        self.assertTrue(summary["verification"]["ok"], summary["verification"])
        types = set(capture.types())
        self.assertIn("bot_started", types)
        self.assertIn("bot_stopped", types)
        self.assertIn("position_opened", types, "the synthetic frame is designed to enter")

    def test_engine_sink_is_installed_only_by_the_runner(self):
        engine = BacktestEngine(self.config)
        self.assertIsNone(engine.event_sink)          # backtests stay silent by default
        runner = self.runner()
        try:
            runner.setup()
            self.assertIsNotNone(runner.engine.event_sink)
        finally:
            runner.ledger.close()


class TestBrokenNotifierCannotBreakACycle(RunnerNotificationCase):
    def test_notifier_that_raises_is_isolated(self):
        summary = self.runner(notifier=ExplodingNotifier()).run()
        self.assertTrue(summary["verification"]["ok"], summary["verification"])
        self.assertGreater(summary["bars_processed"], 0)

    def test_real_notifier_with_a_broken_provider_is_isolated(self):
        """The hard requirement: a totally broken provider cannot stop the loop."""
        notifier = Notifier(self.runtime_config(), providers=[BrokenProvider()])
        summary = self.runner(notifier=notifier).run()
        self.assertTrue(summary["verification"]["ok"], summary["verification"])
        self.assertGreater(summary["bars_processed"], 0)
        rows = NotificationStore(self.runtime_config().store_path).read_all()
        self.assertTrue(rows, "failed dispatches must still be audited")
        self.assertTrue(all(row["status"] == "failed" for row in rows))
        self.assertTrue(all(row["provider"] == "broken" for row in rows))

    def test_working_provider_still_delivers_next_to_a_broken_one(self):
        good = RecordingProvider()
        notifier = Notifier(self.runtime_config(), providers=[good, BrokenProvider()])
        summary = self.runner(notifier=notifier).run()
        self.assertTrue(summary["verification"]["ok"], summary["verification"])
        types = [event.type for event in good.events]
        self.assertIn("bot_started", types)
        self.assertIn("bot_stopped", types)

    def test_engine_sink_that_raises_is_swallowed(self):
        def boom(_payload):
            raise RuntimeError("sink exploded")

        engine = BacktestEngine(self.config, event_sink=boom)
        frames = {"BTC/USDT": make_frame(self.BARS, make_entries=True, seed=21)}
        meta = {"BTC/USDT": {"source": "test", "complete": True, "rows": self.BARS}}
        result = engine.run(frames, data_meta=meta)      # must not raise
        self.assertGreater(result.bars_processed, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
