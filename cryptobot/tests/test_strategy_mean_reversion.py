"""Mean-reversion strategy: filters, exit rule, purity and a no-look-ahead proof."""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from cryptobot.strategy.base import ENTER, EXIT, HOLD, Signal, get_strategy
from cryptobot.strategy.mean_reversion import MeanReversionStrategy

from .fixtures import make_frame


class TestStrategyInterface(unittest.TestCase):
    def test_registry_returns_the_strategy(self):
        strategy = get_strategy("mean_reversion", {"bb_period": 10})
        self.assertIsInstance(strategy, MeanReversionStrategy)
        self.assertEqual(strategy.bb_period, 10)

    def test_unknown_strategy_raises(self):
        with self.assertRaises(KeyError):
            get_strategy("does_not_exist")

    def test_invalid_signal_action_rejected(self):
        with self.assertRaises(ValueError):
            Signal("teleport", "nope", 1.0)

    def test_parameter_validation(self):
        for params in ({"bb_period": 1}, {"bb_std": 0}, {"rsi_period": 0},
                       {"rsi_oversold": 0}, {"rsi_oversold": 100}, {"trend_sma_period": 0}):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    MeanReversionStrategy(params)

    def test_min_candles_covers_all_indicators(self):
        strategy = MeanReversionStrategy({"bb_period": 20, "rsi_period": 14, "trend_sma_period": 200})
        self.assertEqual(strategy.min_candles, 201)


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.frame = make_frame(120, make_entries=True)
        self.strategy = MeanReversionStrategy({"trend_sma_period": 50})

    def test_adds_indicator_columns(self):
        prepared = self.strategy.prepare(self.frame)
        for column in ("sma_trend", "bb_upper", "bb_middle", "bb_lower", "rsi"):
            self.assertIn(column, prepared.columns)

    def test_does_not_mutate_input(self):
        original = self.frame.copy(deep=True)
        self.strategy.prepare(self.frame)
        pd.testing.assert_frame_equal(self.frame, original)

    def test_bands_are_ordered(self):
        prepared = self.strategy.prepare(self.frame)
        valid = prepared.dropna(subset=["bb_upper", "bb_middle", "bb_lower"])
        self.assertTrue((valid["bb_upper"] >= valid["bb_middle"]).all())
        self.assertTrue((valid["bb_middle"] >= valid["bb_lower"]).all())

    def test_no_look_ahead_indicators_are_stable(self):
        """Indicator values at bar i must not change when later bars appear."""
        full = self.strategy.prepare(self.frame)
        for index in (60, 80, 100, 119):
            prefix = self.strategy.prepare(self.frame.iloc[: index + 1])
            for column in ("sma_trend", "bb_upper", "bb_middle", "bb_lower", "rsi"):
                expected = full[column].iloc[index]
                actual = prefix[column].iloc[index]
                if np.isnan(expected):
                    self.assertTrue(np.isnan(actual), "{}@{}".format(column, index))
                else:
                    self.assertAlmostEqual(actual, expected, places=12, msg="{}@{}".format(column, index))


class TestDecisions(unittest.TestCase):
    def setUp(self):
        self.frame = make_frame(320, make_entries=True)
        self.strategy = MeanReversionStrategy({"trend_sma_period": 100})
        self.prepared = self.strategy.prepare(self.frame)

    def test_warmup_never_enters(self):
        for index in range(0, self.strategy.min_candles - 1):
            signal = self.strategy.evaluate(self.prepared, index, has_position=False)
            self.assertEqual(signal.action, HOLD)
            self.assertTrue(signal.reason.startswith("warmup"), signal.reason)

    def test_entry_requires_all_three_filters(self):
        entries = [i for i in range(len(self.prepared))
                   if self.strategy.evaluate(self.prepared, i, has_position=False).is_entry]
        self.assertTrue(entries, "synthetic frame should produce at least one entry")
        for index in entries:
            row = self.prepared.iloc[index]
            self.assertLessEqual(row["close"], row["bb_lower"])
            self.assertGreater(row["close"], row["sma_trend"])
            self.assertLessEqual(row["rsi"], self.strategy.rsi_oversold)

    def test_no_entry_when_not_oversold(self):
        strict = MeanReversionStrategy({"trend_sma_period": 100, "rsi_oversold": 0.5})
        prepared = strict.prepare(self.frame)
        actions = {strict.evaluate(prepared, i, has_position=False).action for i in range(len(prepared))}
        self.assertNotIn(ENTER, actions)

    def test_trend_filter_blocks_below_sma(self):
        blocked = [i for i in range(len(self.prepared))
                   if self.prepared["close"].iloc[i] <= self.prepared["sma_trend"].iloc[i]][5:]
        self.assertTrue(blocked)
        index = blocked[0]
        signal = self.strategy.evaluate(self.prepared, index, has_position=False)
        self.assertNotEqual(signal.action, ENTER)
        if not signal.reason.startswith("warmup"):
            self.assertIn("trend_filter", signal.reason)

    def test_exit_at_middle_band(self):
        above = [i for i in range(len(self.prepared))
                 if not pd.isna(self.prepared["bb_middle"].iloc[i])
                 and self.prepared["close"].iloc[i] >= self.prepared["bb_middle"].iloc[i]]
        self.assertTrue(above)
        signal = self.strategy.evaluate(self.prepared, above[-1], has_position=True)
        self.assertEqual(signal.action, EXIT)
        self.assertIn("middle_band", signal.reason)

    def test_hold_while_waiting_for_reversion(self):
        below = [i for i in range(len(self.prepared))
                 if not pd.isna(self.prepared["bb_middle"].iloc[i])
                 and self.prepared["close"].iloc[i] < self.prepared["bb_middle"].iloc[i]]
        self.assertTrue(below)
        signal = self.strategy.evaluate(self.prepared, below[-1], has_position=True)
        self.assertEqual(signal.action, HOLD)
        self.assertIn("waiting_for_mean_reversion", signal.reason)

    def test_exit_rule_can_be_disabled(self):
        strategy = MeanReversionStrategy({"trend_sma_period": 100, "exit_at_middle_band": False})
        prepared = strategy.prepare(self.frame)
        actions = {strategy.evaluate(prepared, i, has_position=True).action for i in range(len(prepared))}
        self.assertNotIn(EXIT, actions)

    def test_signal_carries_price_and_indicator_snapshot(self):
        signal = self.strategy.evaluate(self.prepared, 200, has_position=False)
        self.assertAlmostEqual(signal.price, float(self.prepared["close"].iloc[200]), places=12)
        for key in ("close", "bb_upper", "bb_middle", "bb_lower", "rsi", "sma_trend"):
            self.assertIn(key, signal.indicators)

    def test_describe(self):
        described = self.strategy.describe()
        self.assertEqual(described["name"], "mean_reversion")
        self.assertEqual(described["params"], dict(sorted(self.strategy.params.items())))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
