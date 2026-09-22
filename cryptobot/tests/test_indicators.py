"""Pure indicator tests (hand-computed expectations, no I/O)."""

from __future__ import annotations

import unittest

import numpy as np

from cryptobot.strategy.indicators import bollinger, log_zscore, rolling_std, rsi, sma


class TestSma(unittest.TestCase):
    def test_hand_computed(self):
        out = sma([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        self.assertTrue(np.isnan(out[0]) and np.isnan(out[1]))
        np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0])

    def test_warmup_length(self):
        out = sma(list(range(10)), 4)
        self.assertEqual(int(np.isnan(out).sum()), 3)

    def test_too_short_input_is_all_nan(self):
        out = sma([1.0, 2.0], 5)
        self.assertTrue(np.isnan(out).all())

    def test_input_not_mutated(self):
        values = [1.0, 2.0, 3.0]
        sma(values, 2)
        self.assertEqual(values, [1.0, 2.0, 3.0])

    def test_validation(self):
        with self.assertRaises(ValueError):
            sma([1.0, 2.0], 0)
        with self.assertRaises(ValueError):
            sma(np.zeros((2, 2)), 2)


class TestRollingStd(unittest.TestCase):
    def test_population_std(self):
        out = rolling_std([1.0, 2.0, 4.0], 2)
        self.assertTrue(np.isnan(out[0]))
        self.assertAlmostEqual(out[1], 0.5, places=12)   # std([1,2])
        self.assertAlmostEqual(out[2], 1.0, places=12)   # std([2,4])

    def test_sample_std_option(self):
        out = rolling_std([1.0, 2.0, 4.0], 3, ddof=1)
        self.assertAlmostEqual(out[2], float(np.std([1.0, 2.0, 4.0], ddof=1)), places=12)

    def test_validation(self):
        with self.assertRaises(ValueError):
            rolling_std([1.0, 2.0], 1)


class TestBollinger(unittest.TestCase):
    def test_bands_geometry(self):
        values = np.array([10.0, 12.0, 11.0, 13.0, 12.5, 11.5])
        upper, middle, lower = bollinger(values, 3, 2.0)
        std = rolling_std(values, 3)
        np.testing.assert_allclose(middle, sma(values, 3), equal_nan=True)
        np.testing.assert_allclose(upper, middle + 2.0 * std, equal_nan=True)
        np.testing.assert_allclose(lower, middle - 2.0 * std, equal_nan=True)
        valid = ~np.isnan(middle)
        self.assertTrue((upper[valid] > middle[valid]).all())
        self.assertTrue((lower[valid] < middle[valid]).all())

    def test_flat_series_has_zero_width(self):
        upper, middle, lower = bollinger([5.0] * 10, 4, 2.0)
        np.testing.assert_allclose(upper[3:], 5.0)
        np.testing.assert_allclose(lower[3:], 5.0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            bollinger([1.0, 2.0, 3.0], 3, 0.0)


class TestRsi(unittest.TestCase):
    def test_monotonic_series(self):
        rising = rsi(list(range(1, 30)), 14)
        decreasing = rsi(list(range(30, 1, -1)), 14)
        self.assertAlmostEqual(rising[-1], 100.0, places=9)
        self.assertAlmostEqual(decreasing[-1], 0.0, places=9)

    def test_flat_series_is_neutral(self):
        out = rsi([5.0] * 20, 14)
        self.assertAlmostEqual(out[-1], 50.0, places=9)

    def test_bounds_and_warmup(self):
        rng = np.random.default_rng(3)
        values = 100 + np.cumsum(rng.normal(0, 1, 100))
        out = rsi(values, 14)
        self.assertEqual(int(np.isnan(out).sum()), 14)
        valid = out[~np.isnan(out)]
        self.assertTrue(((valid >= 0) & (valid <= 100)).all())

    def test_hand_computed_small_case(self):
        # deltas: +1,+1,-1,+1 ; period=4 -> first value at index 4
        values = [10.0, 11.0, 12.0, 11.0, 12.0]
        out = rsi(values, 4)
        gains = [1, 1, 0, 1]
        losses = [0, 0, 1, 0]
        avg_gain = sum(gains) / 4.0
        avg_loss = sum(losses) / 4.0
        expected = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        self.assertAlmostEqual(out[4], expected, places=9)

    def test_too_short_is_all_nan(self):
        self.assertTrue(np.isnan(rsi([1.0, 2.0], 14)).all())

    def test_validation(self):
        with self.assertRaises(ValueError):
            rsi([1.0, 2.0, 3.0], 0)


class TestLogZScore(unittest.TestCase):
    def test_measures_the_current_value_against_prior_bars_only(self):
        history = [10.0 + 0.1 * (index % 3) for index in range(20)]
        out = log_zscore(history + [100.0], period=20)
        # The window excludes the current bar, so a single spike is a large
        # positive score against a nearly flat history.
        self.assertTrue(np.isnan(out[:20]).all())
        self.assertGreater(out[-1], 5.0)

    def test_warmup_is_nan(self):
        out = log_zscore([1.0, 2.0, 3.0], period=5)
        self.assertTrue(np.isnan(out).all())

    def test_flat_series_has_no_defined_score(self):
        out = log_zscore([5.0] * 30, period=10)
        self.assertTrue(np.isnan(out[-1]))   # zero variance -> no fake number

    def test_non_positive_values_are_nan_not_negative_infinity(self):
        out = log_zscore([0.0] * 10 + [1.0] * 10, period=5)
        self.assertTrue(np.isfinite(out[~np.isnan(out)]).all())

    def test_validation(self):
        with self.assertRaises(ValueError):
            log_zscore([1.0, 2.0, 3.0], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
