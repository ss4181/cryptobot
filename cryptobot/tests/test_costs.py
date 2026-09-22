"""Net-vs-gross take-profit math and fill arithmetic (pure functions)."""

from __future__ import annotations

import unittest

from cryptobot.execution import costs
from cryptobot.execution.costs import (
    buy_leg,
    cost_drag_pct,
    entry_fill_price,
    exit_fill_price,
    fee_amount,
    max_affordable_qty,
    net_return_pct,
    required_gross_tp_pct,
    required_tp_price,
    round_trip,
    sell_leg,
    stop_price_from_pct,
)


class TestNetVersusGrossTarget(unittest.TestCase):
    """The whole point of the module: TP must net the configured target."""

    CASES = [
        (2.0, 0.1, 0.05),
        (2.0, 0.075, 0.0),   # Binance BNB/VIP-ish fee, no slippage
        (1.0, 0.1, 0.1),
        (5.0, 0.1, 0.05),
        (2.0, 0.0, 0.0),     # frictionless -> gross == net
        (0.5, 0.2, 0.15),
    ]

    def test_gross_level_nets_exactly_the_target(self):
        for target, fee, slip in self.CASES:
            with self.subTest(target=target, fee=fee, slip=slip):
                entry_fill = entry_fill_price(100.0, slip)
                tp = required_tp_price(entry_fill, target, fee, slip)
                realized = net_return_pct(entry_fill, tp, fee, slip)
                self.assertAlmostEqual(realized, target, places=9)

    def test_frictionless_case_is_identity(self):
        self.assertAlmostEqual(required_gross_tp_pct(2.0, 0.0, 0.0), 2.0, places=12)

    def test_gross_is_always_at_least_net_when_costs_exist(self):
        for target, fee, slip in self.CASES:
            gross = required_gross_tp_pct(target, fee, slip)
            self.assertGreaterEqual(gross, target - 1e-12)
            if fee or slip:
                self.assertGreater(gross, target)

    def test_cost_drag_is_the_difference(self):
        target, fee, slip = 2.0, 0.1, 0.05
        self.assertAlmostEqual(
            cost_drag_pct(target, fee, slip),
            required_gross_tp_pct(target, fee, slip) - target,
            places=12,
        )

    def test_documented_example_value(self):
        # Documented in the docstring / README: fee 0.1 %, slippage 0.05 %, target 2 %.
        self.assertAlmostEqual(required_gross_tp_pct(2.0, 0.1, 0.05), 2.2553318701392433, places=12)
        self.assertAlmostEqual(cost_drag_pct(2.0, 0.1, 0.05), 0.2553318701392433, places=12)

    def test_tp_price_is_above_entry_fill(self):
        entry = entry_fill_price(100.0, 0.05)
        self.assertGreater(required_tp_price(entry, 2.0, 0.1, 0.05), entry)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            required_gross_tp_pct(-1.0, 0.1, 0.05)
        with self.assertRaises(ValueError):
            required_gross_tp_pct(2.0, -0.1, 0.05)
        with self.assertRaises(ValueError):
            required_gross_tp_pct(2.0, 100.0, 0.05)
        with self.assertRaises(ValueError):
            required_gross_tp_pct(2.0, 0.1, 100.0)


class TestFillArithmetic(unittest.TestCase):
    def test_slippage_direction_is_always_against_us(self):
        self.assertGreater(entry_fill_price(100.0, 0.1), 100.0)
        self.assertLess(exit_fill_price(100.0, 0.1), 100.0)
        self.assertEqual(entry_fill_price(100.0, 0.0), 100.0)
        self.assertEqual(exit_fill_price(100.0, 0.0), 100.0)

    def test_fee_amount(self):
        self.assertAlmostEqual(fee_amount(1000.0, 0.1), 1.0, places=12)
        self.assertEqual(fee_amount(-1000.0, 0.1), 1.0)

    def test_buy_and_sell_leg_cash_flow_signs(self):
        buy = buy_leg(100.0, 1.0, 0.1, 0.05)
        sell = sell_leg(110.0, 1.0, 0.1, 0.05)
        self.assertLess(buy.cash_flow, 0)
        self.assertGreater(sell.cash_flow, 0)
        self.assertAlmostEqual(buy.cash_flow, -(buy.notional + buy.fee), places=12)
        self.assertAlmostEqual(sell.cash_flow, sell.notional - sell.fee, places=12)

    def test_round_trip_identity_gross_minus_costs(self):
        breakdown = round_trip(100.0, 105.0, 2.0, 0.1, 0.05)
        self.assertAlmostEqual(
            breakdown.net_pnl,
            breakdown.gross_pnl - breakdown.fees - breakdown.slippage_cost,
            places=10,
        )

    def test_round_trip_uses_supplied_entry_fill(self):
        with_derived = round_trip(100.0, 105.0, 1.0, 0.1, 0.05)
        self.assertAlmostEqual(with_derived.entry_fill_price, 100.05, places=9)
        # Paying more than the modelled slip-adjusted price must reduce net PnL.
        with_explicit = round_trip(100.0, 105.0, 1.0, 0.1, 0.05, entry_fill=100.5)
        self.assertAlmostEqual(with_explicit.entry_fill_price, 100.5, places=12)
        self.assertLess(with_explicit.net_pnl, with_derived.net_pnl)

    def test_round_trip_validation(self):
        with self.assertRaises(ValueError):
            round_trip(100.0, 105.0, 0.0, 0.1, 0.05)

    def test_stop_price_and_net_loss_when_stopped(self):
        entry_fill = entry_fill_price(100.0, 0.05)
        stop = stop_price_from_pct(entry_fill, 2.5)
        self.assertLess(stop, entry_fill)
        # The realised net loss at the stop is worse than the configured 2.5 %.
        realized = net_return_pct(entry_fill, stop, 0.1, 0.05)
        self.assertLess(realized, -2.5)

    def test_max_affordable_qty_spends_the_cash(self):
        cash, price, fee, slip = 50.0, 30_000.0, 0.1, 0.05
        qty = max_affordable_qty(cash, price, fee, slip)
        leg = buy_leg(price, qty, fee, slip)
        self.assertAlmostEqual(leg.notional + leg.fee, cash, places=9)
        self.assertLess(qty, cash / price)

    def test_entry_price_validation(self):
        with self.assertRaises(ValueError):
            entry_fill_price(0.0, 0.1)
        with self.assertRaises(ValueError):
            exit_fill_price(-5.0, 0.1)
        with self.assertRaises(ValueError):
            buy_leg(100.0, -1.0, 0.1, 0.05)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
