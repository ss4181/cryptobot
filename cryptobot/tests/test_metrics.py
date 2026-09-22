"""Metrics: hand-computed drawdown/Sharpe/profit factor plus determinism."""

from __future__ import annotations

import json
import unittest

from cryptobot.backtest.metrics import (
    canonical_json,
    compute_metrics,
    max_drawdown,
    metrics_hash,
    profit_factor,
    sharpe_ratio,
)


def trade(net: float, *, pct: float = 1.0, bars: int = 5, fees: float = 0.1, slip: float = 0.05) -> dict:
    return {"net_pnl": net, "gross_pnl": net + fees + slip, "net_pnl_pct": pct,
            "fees": fees, "slippage_cost": slip, "bars_held": bars}


def metrics_for(trades, equity, **kwargs):
    params = dict(initial_capital=50.0, bars_per_year=8760.0, timeframe="1h",
                  pairs=["BTC/USDT"], bars=len(equity))
    params.update(kwargs)
    return compute_metrics(trades, equity, **params)


class TestMaxDrawdown(unittest.TestCase):
    def test_hand_computed(self):
        result = max_drawdown([100.0, 120.0, 90.0, 110.0])
        self.assertAlmostEqual(result["pct"], 25.0, places=9)   # 90 vs peak 120
        self.assertAlmostEqual(result["usdt"], 30.0, places=9)
        self.assertEqual(result["peak_index"], 1)
        self.assertEqual(result["trough_index"], 2)

    def test_monotonic_equity_has_no_drawdown(self):
        result = max_drawdown([100.0, 101.0, 102.0])
        self.assertEqual(result["pct"], 0.0)

    def test_empty_series(self):
        result = max_drawdown([])
        self.assertEqual(result["pct"], 0.0)

    def test_deepest_drawdown_is_chosen(self):
        result = max_drawdown([100.0, 80.0, 100.0, 95.0])
        self.assertAlmostEqual(result["pct"], 20.0, places=9)


class TestProfitFactor(unittest.TestCase):
    def test_hand_computed(self):
        self.assertAlmostEqual(profit_factor([1.0, -1.0, 2.0]), 3.0, places=9)

    def test_no_losses_is_infinite_then_reported_as_none(self):
        self.assertTrue(profit_factor([1.0, 2.0]) == float("inf"))
        metrics = metrics_for([trade(1.0), trade(2.0)], [50.0, 51.0, 53.0])
        self.assertIsNone(metrics["profit_factor"])

    def test_no_trades(self):
        self.assertIsNone(profit_factor([]))


class TestSharpe(unittest.TestCase):
    def test_constant_returns_give_none(self):
        self.assertIsNone(sharpe_ratio([0.01] * 10, 8760.0))
        self.assertIsNone(sharpe_ratio([0.0] * 10, 8760.0))

    def test_too_short_series_returns_none(self):
        self.assertIsNone(sharpe_ratio([0.01, 0.02], 8760.0))
        self.assertIsNone(sharpe_ratio([], 8760.0))

    def test_known_value(self):
        import statistics

        returns = [0.01, -0.005, 0.02, 0.0, 0.015, -0.01, 0.005]
        expected = statistics.fmean(returns) / statistics.stdev(returns) * (8760.0 ** 0.5)
        self.assertAlmostEqual(sharpe_ratio(returns, 8760.0), expected, places=9)

    def test_nan_returns_are_ignored(self):
        self.assertIsNone(sharpe_ratio([float("nan")] * 5, 8760.0))


class TestComputeMetrics(unittest.TestCase):
    def setUp(self):
        self.trades = [trade(1.0), trade(-0.5), trade(2.0), trade(-1.0)]
        self.equity = [50.0, 51.0, 50.5, 52.5, 51.5]

    def test_core_counts(self):
        metrics = metrics_for(self.trades, self.equity)
        self.assertEqual(metrics["trade_count"], 4)
        self.assertEqual(metrics["winning_trades"], 2)
        self.assertEqual(metrics["losing_trades"], 2)
        self.assertAlmostEqual(metrics["win_rate_pct"], 50.0, places=9)
        self.assertAlmostEqual(metrics["net_pnl_usdt"], 1.5, places=9)
        self.assertAlmostEqual(metrics["net_pnl_pct"], 3.0, places=9)
        self.assertAlmostEqual(metrics["final_equity_usdt"], 51.5, places=9)
        self.assertAlmostEqual(metrics["avg_net_pnl_per_trade_usdt"], 0.375, places=9)
        self.assertAlmostEqual(metrics["best_trade_usdt"], 2.0, places=9)
        self.assertAlmostEqual(metrics["worst_trade_usdt"], -1.0, places=9)
        self.assertAlmostEqual(metrics["avg_win_usdt"], 1.5, places=9)
        self.assertAlmostEqual(metrics["avg_loss_usdt"], -0.75, places=9)

    def test_cost_totals(self):
        metrics = metrics_for(self.trades, self.equity)
        self.assertAlmostEqual(metrics["total_fees_usdt"], 0.4, places=9)
        self.assertAlmostEqual(metrics["total_slippage_usdt"], 0.2, places=9)
        self.assertAlmostEqual(metrics["total_cost_drag_usdt"], 0.6, places=9)

    def test_context_is_echoed(self):
        metrics = metrics_for(self.trades, self.equity, pairs=["BTC/USDT", "ETH/USDT"], timeframe="15m",
                              bars=1234, data_start_ts=1, data_end_ts=2)
        self.assertEqual(metrics["pairs"], ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(metrics["timeframe"], "15m")
        self.assertEqual(metrics["bars"], 1234)
        self.assertEqual(metrics["data_start_ts"], 1)

    def test_no_trades_is_all_zero_not_nan(self):
        metrics = metrics_for([], [50.0])
        self.assertEqual(metrics["trade_count"], 0)
        self.assertEqual(metrics["win_rate_pct"], 0.0)
        self.assertEqual(metrics["avg_net_pnl_per_trade_usdt"], 0.0)
        self.assertIsNone(metrics["sharpe_ratio"])
        json.dumps(metrics, allow_nan=False)  # must not contain NaN/Infinity

    def test_deterministic_key_order_and_hash(self):
        first = metrics_for(self.trades, self.equity)
        second = metrics_for(self.trades, self.equity)
        self.assertEqual(list(first), list(second))
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(metrics_hash(first), metrics_hash(second))

    def test_hash_changes_with_the_data(self):
        first = metrics_for(self.trades, self.equity)
        second = metrics_for(self.trades + [trade(0.25)], self.equity)
        self.assertNotEqual(metrics_hash(first), metrics_hash(second))

    def test_hash_is_order_independent_for_keys(self):
        self.assertEqual(metrics_hash({"a": 1, "b": 2}), metrics_hash({"b": 2, "a": 1}))

    def test_canonical_json_rejects_nan(self):
        with self.assertRaises(ValueError):
            canonical_json({"x": float("nan")})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
