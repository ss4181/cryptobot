"""Paper broker: fills, fees, slippage, rejections, partial fills, fault injection."""

from __future__ import annotations

import unittest

from cryptobot.execution.costs import entry_fill_price, exit_fill_price, round_trip
from cryptobot.execution.paper_broker import (
    FILLED,
    PARTIAL,
    REJECTED,
    FaultInjector,
    PaperBroker,
)

PRICE = 30_000.0
FEE = 0.1
SLIP = 0.05
TS = 1_600_000_000_000


def make_broker(cash: float = 50.0, *, faults=None, max_open: int = 2, min_notional: float = 5.0) -> PaperBroker:
    return PaperBroker(cash, FEE, SLIP, faults=faults, max_open_positions=max_open,
                       min_notional_usdt=min_notional)


class TestBuy(unittest.TestCase):
    def test_fill_price_includes_slippage_and_fee(self):
        broker = make_broker()
        qty = 0.001
        result = broker.buy("BTC/USDT", PRICE, qty, TS, stop_price=29_000, tp_price=31_000, reason="test")
        self.assertEqual(result.status, FILLED)
        expected_fill = entry_fill_price(PRICE, SLIP)
        self.assertAlmostEqual(result.fill_price, expected_fill, places=9)
        expected_fee = expected_fill * qty * FEE / 100.0
        self.assertAlmostEqual(result.fee, expected_fee, places=12)
        self.assertAlmostEqual(broker.cash, 50.0 - expected_fill * qty - expected_fee, places=9)

    def test_position_bookkeeping(self):
        broker = make_broker()
        broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=29_000, tp_price=31_000, reason="dip")
        position = broker.positions["BTC/USDT"]
        self.assertEqual(position.qty, 0.001)
        self.assertEqual(position.pair, "BTC/USDT")
        self.assertEqual(position.entry_ts, TS)
        self.assertEqual(position.reason, "dip")
        self.assertTrue(position.position_id)
        self.assertAlmostEqual(position.cost_basis,
                               position.entry_fill_price * position.qty + position.entry_fee, places=12)
        self.assertEqual(broker.total_fees, position.entry_fee)

    def test_fee_and_slippage_reduce_equity_immediately(self):
        broker = make_broker()
        broker.mark_price("BTC/USDT", PRICE)
        broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=29_000, tp_price=31_000)
        self.assertLess(broker.equity(), 50.0)
        self.assertGreater(broker.total_slippage_cost, 0.0)

    def test_insufficient_balance_rejected(self):
        broker = make_broker(cash=50.0)
        result = broker.buy("BTC/USDT", PRICE, 1.0, TS, stop_price=29_000, tp_price=31_000)
        self.assertEqual(result.status, REJECTED)
        self.assertTrue(result.reason.startswith("insufficient_balance"), result.reason)
        self.assertIn("affordable_qty", result.reason)
        self.assertEqual(broker.cash, 50.0)
        self.assertEqual(broker.stats["rejected"], 1)

    def test_below_min_notional_rejected(self):
        broker = make_broker()
        result = broker.buy("BTC/USDT", PRICE, 0.0001, TS, stop_price=29_000, tp_price=31_000)
        self.assertEqual(result.status, REJECTED)
        self.assertEqual(result.reason, "below_min_notional")

    def test_invalid_quantity_rejected(self):
        broker = make_broker()
        for qty in (0.0, -1.0):
            with self.subTest(qty=qty):
                result = broker.buy("BTC/USDT", PRICE, qty, TS, stop_price=1, tp_price=2)
                self.assertEqual(result.status, REJECTED)
                self.assertEqual(result.reason, "invalid_quantity_or_price")

    def test_duplicate_pair_rejected(self):
        broker = make_broker()
        broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=1, tp_price=2)
        result = broker.buy("BTC/USDT", PRICE, 0.001, TS + 1, stop_price=1, tp_price=2)
        self.assertEqual(result.reason, "position_already_open")

    def test_max_open_positions_rejected(self):
        broker = make_broker(max_open=1)
        broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=1, tp_price=2)
        result = broker.buy("ETH/USDT", 2_000.0, 0.01, TS, stop_price=1, tp_price=2)
        self.assertEqual(result.reason, "max_open_positions_reached")

    def test_rejection_reasons_are_counted(self):
        broker = make_broker()
        broker.buy("BTC/USDT", PRICE, 1.0, TS, stop_price=1, tp_price=2)
        broker.buy("BTC/USDT", PRICE, 1.0, TS, stop_price=1, tp_price=2)
        broker.buy("BTC/USDT", PRICE, 1.0, TS, stop_price=1, tp_price=2)
        self.assertEqual(broker.stats["rejected"], 3)
        self.assertEqual(sum(broker.stats["rejected_reasons"].values()), 3)
        self.assertTrue(all(reason.startswith("insufficient_balance")
                            for reason in broker.stats["rejected_reasons"]))


class TestSell(unittest.TestCase):
    def setUp(self):
        self.broker = make_broker()
        self.qty = 0.001
        self.buy = self.broker.buy("BTC/USDT", PRICE, self.qty, TS, stop_price=29_000, tp_price=31_000)

    def test_sell_realizes_net_pnl_matching_cost_model(self):
        exit_price = PRICE * 1.03
        result = self.broker.sell("BTC/USDT", exit_price, TS + 3_600_000, reason="take_profit")
        self.assertEqual(result.status, FILLED)
        expected = round_trip(PRICE, exit_price, self.qty, FEE, SLIP,
                              entry_fill=self.buy.fill_price)
        self.assertAlmostEqual(result.net_pnl, expected.net_pnl, places=9)
        self.assertAlmostEqual(result.gross_pnl, expected.gross_pnl, places=9)
        self.assertAlmostEqual(self.broker.realized_net_pnl, expected.net_pnl, places=9)
        self.assertNotIn("BTC/USDT", self.broker.positions)

    def test_sell_fill_price_is_below_reference(self):
        result = self.broker.sell("BTC/USDT", PRICE, TS + 1)
        self.assertAlmostEqual(result.fill_price, exit_fill_price(PRICE, SLIP), places=9)
        self.assertLess(result.fill_price, PRICE)

    def test_round_trip_equity_equals_cash_plus_realized(self):
        exit_price = PRICE * 1.04
        self.broker.sell("BTC/USDT", exit_price, TS + 1)
        self.assertAlmostEqual(self.broker.equity(), 50.0 + self.broker.realized_net_pnl, places=9)

    def test_losing_round_trip(self):
        result = self.broker.sell("BTC/USDT", PRICE * 0.97, TS + 1, reason="stop_loss")
        self.assertLess(result.net_pnl, 0.0)
        self.assertLess(self.broker.realized_net_pnl, 0.0)

    def test_sell_without_position_rejected(self):
        result = self.broker.sell("ETH/USDT", 2_000.0, TS)
        self.assertEqual(result.status, REJECTED)
        self.assertEqual(result.reason, "no_open_position")

    def test_sell_more_than_held_rejected(self):
        result = self.broker.sell("BTC/USDT", PRICE, TS, qty=self.qty * 2)
        self.assertEqual(result.reason, "insufficient_position")
        self.assertIn("BTC/USDT", self.broker.positions)

    def test_closed_trade_record_is_complete(self):
        self.broker.sell("BTC/USDT", PRICE * 1.02, TS + 1, reason="strategy:exit")
        trade = self.broker.closed_trades[-1]
        for key in ("pair", "position_id", "entry_ts", "exit_ts", "entry_reference_price",
                    "entry_fill_price", "exit_reference_price", "exit_fill_price", "qty",
                    "entry_fee", "exit_fee", "fees", "gross_pnl", "net_pnl", "net_pnl_pct",
                    "slippage_cost", "exit_reason", "status"):
            self.assertIn(key, trade)
        self.assertEqual(trade["exit_reason"], "strategy:exit")
        self.assertAlmostEqual(trade["fees"], trade["entry_fee"] + trade["exit_fee"], places=9)


class TestMarkingAndSnapshots(unittest.TestCase):
    def test_equity_follows_mark_price(self):
        broker = make_broker()
        broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=1, tp_price=2)
        equity_before = broker.equity()
        broker.mark_price("BTC/USDT", PRICE * 1.10)
        self.assertGreater(broker.equity(), equity_before)
        self.assertGreater(broker.unrealized_pnl(), 0.0)

    def test_mark_price_validation(self):
        broker = make_broker()
        with self.assertRaises(ValueError):
            broker.mark_price("BTC/USDT", 0.0)

    def test_equity_history_and_snapshot(self):
        broker = make_broker()
        broker.mark_price("BTC/USDT", PRICE)
        point = broker.record_equity(TS)
        self.assertAlmostEqual(point.equity, 50.0, places=9)
        snapshot = broker.snapshot()
        self.assertEqual(snapshot["open_positions"], 0)
        self.assertEqual(snapshot["closed_trades"], 0)
        self.assertIn("stats", snapshot)
        self.assertIn("positions", snapshot)
        for key in ("cash", "positions_value", "equity", "realized_net_pnl", "total_fees", "stats",
                    "unrealized_net_pnl", "total_slippage_cost", "last_prices"):
            self.assertIn(key, snapshot)

    def test_summary_win_rate(self):
        broker = make_broker(cash=200.0)
        broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=1, tp_price=2)
        broker.sell("BTC/USDT", PRICE * 1.05, TS + 1)
        broker.buy("BTC/USDT", PRICE, 0.001, TS + 2, stop_price=1, tp_price=2)
        broker.sell("BTC/USDT", PRICE * 0.95, TS + 3)
        summary = broker.summary()
        self.assertEqual(summary["closed_trades"], 2)
        self.assertEqual(summary["winning_trades"], 1)
        self.assertAlmostEqual(summary["win_rate_pct"], 50.0, places=6)


class TestFaultInjection(unittest.TestCase):
    def test_disabled_by_default(self):
        faults = FaultInjector()
        faults.reject_next("buy", 5)
        broker = make_broker(faults=faults)
        result = broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=1, tp_price=2)
        self.assertEqual(result.status, FILLED)
        self.assertEqual(broker.stats["rejected"], 0)

    def test_synthetic_rejection_when_enabled(self):
        faults = FaultInjector().enable()
        faults.reject_next("buy", 1)
        broker = make_broker(faults=faults)
        rejected = broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=1, tp_price=2)
        self.assertEqual(rejected.status, REJECTED)
        self.assertEqual(rejected.reason, "synthetic_rejection")
        accepted = broker.buy("BTC/USDT", PRICE, 0.001, TS + 1, stop_price=1, tp_price=2)
        self.assertEqual(accepted.status, FILLED)
        self.assertIn("reject:buy", faults.injected)

    def test_partial_fill(self):
        faults = FaultInjector().enable()
        faults.partial_next("buy", 0.5)
        broker = make_broker(faults=faults)
        result = broker.buy("BTC/USDT", PRICE, 0.002, TS, stop_price=1, tp_price=2)
        self.assertEqual(result.status, PARTIAL)
        self.assertAlmostEqual(result.requested_qty, 0.002, places=12)
        self.assertAlmostEqual(result.filled_qty, 0.001, places=12)
        self.assertAlmostEqual(broker.positions["BTC/USDT"].qty, 0.001, places=12)
        self.assertEqual(broker.stats["partial"], 1)

    def test_partial_close_leaves_residual_position(self):
        faults = FaultInjector().enable()
        broker = make_broker(cash=200.0, faults=faults)
        buy = broker.buy("BTC/USDT", PRICE, 0.002, TS, stop_price=1, tp_price=2)
        self.assertEqual(buy.status, FILLED)
        original_entry_fee = broker.positions["BTC/USDT"].entry_fee

        faults.partial_next("sell", 0.5)
        result = broker.sell("BTC/USDT", PRICE * 1.02, TS + 1)
        self.assertEqual(result.status, PARTIAL)
        self.assertAlmostEqual(result.filled_qty, 0.001, places=12)
        self.assertAlmostEqual(broker.positions["BTC/USDT"].qty, 0.001, places=12)
        # The entry fee is pro-rated over the closed part, never double counted.
        trade = broker.closed_trades[-1]
        self.assertAlmostEqual(trade["entry_fee"], original_entry_fee * 0.5, places=12)
        self.assertAlmostEqual(trade["qty"], result.filled_qty, places=12)

    def test_forced_insufficient_cash(self):
        faults = FaultInjector().enable().block_cash(True)
        broker = make_broker(cash=1e9, faults=faults)
        result = broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=1, tp_price=2)
        self.assertEqual(result.status, REJECTED)
        self.assertTrue(result.reason.startswith("insufficient_balance"))

    def test_partial_factor_validation(self):
        with self.assertRaises(ValueError):
            FaultInjector().partial_next("buy", 1.5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
