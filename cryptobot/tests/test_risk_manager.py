"""Risk manager: sizing, stop-loss, net take-profit, daily loss limit, cooldown."""

from __future__ import annotations

import unittest

from cryptobot.execution.costs import (
    entry_fill_price,
    required_gross_tp_pct,
    required_tp_price,
    stop_price_from_pct,
)
from cryptobot.execution.paper_broker import Position
from cryptobot.risk.manager import (
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    RiskManager,
    utc_day,
)

TS = 1_700_000_000_000  # 2023-11-14T22:13:20Z
DAY_MS = 86_400_000


def make_manager(**overrides) -> RiskManager:
    kwargs = dict(
        initial_equity=50.0,
        fee_pct=0.1,
        slippage_pct=0.05,
        net_profit_target_pct=2.0,
        stop_loss_pct=2.5,
        max_position_pct=90.0,
        max_open_positions=1,
        daily_loss_limit_pct=5.0,
        cooldown_minutes=60.0,
        min_equity_usdt=10.0,
        max_trades_per_day=8,
    )
    kwargs.update(overrides)
    return RiskManager(**kwargs)


def make_position(entry_fill: float = 100.0, stop: float = 97.5, tp: float = 102.26) -> Position:
    return Position("BTC/USDT", 1.0, entry_fill, entry_fill, 0.1, TS, stop, tp, "test", "pos-1")


class TestTargets(unittest.TestCase):
    def test_gross_tp_is_derived_from_net_target(self):
        manager = make_manager()
        self.assertAlmostEqual(manager.gross_tp_pct, required_gross_tp_pct(2.0, 0.1, 0.05), places=12)
        self.assertFalse(manager.gross_tp_overridden)

    def test_explicit_gross_override_is_used(self):
        manager = make_manager(gross_take_profit_pct=3.0)
        self.assertEqual(manager.gross_tp_pct, 3.0)
        self.assertTrue(manager.gross_tp_overridden)
        decision = manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.tp_price, decision.entry_fill_estimate * 1.03, places=9)

    def test_limits_snapshot_is_observable(self):
        snapshot = make_manager().limits_snapshot()
        for key in ("net_profit_target_pct", "gross_take_profit_pct", "cost_drag_pct", "stop_loss_pct",
                    "max_position_pct", "max_open_positions", "daily_loss_limit_pct",
                    "cooldown_minutes", "min_equity_usdt", "max_trades_per_day"):
            self.assertIn(key, snapshot)


class TestSizing(unittest.TestCase):
    def test_size_is_capped_by_max_position_pct(self):
        manager = make_manager(max_position_pct=50.0)
        decision = manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.notional, 25.0, delta=0.2)

    def test_size_is_capped_by_cash(self):
        manager = make_manager(max_position_pct=100.0)
        decision = manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=20.0, open_positions=0)
        self.assertTrue(decision.approved)
        self.assertLessEqual(decision.notional, 20.0)

    def test_stop_and_tp_bracket_the_entry(self):
        decision = make_manager().evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertLess(decision.stop_price, decision.entry_fill_estimate)
        self.assertGreater(decision.tp_price, decision.entry_fill_estimate)
        self.assertAlmostEqual(decision.stop_price,
                               stop_price_from_pct(decision.entry_fill_estimate, 2.5), places=9)
        self.assertAlmostEqual(decision.tp_price,
                               required_tp_price(decision.entry_fill_estimate, 2.0, 0.1, 0.05), places=9)

    def test_below_min_notional_blocked(self):
        manager = make_manager(min_equity_usdt=0.0)
        decision = manager.evaluate_entry(ts=TS, price=100.0, equity=5.0, cash=4.0, open_positions=0)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "below_min_notional")

    def test_invalid_price_blocked(self):
        decision = make_manager().evaluate_entry(ts=TS, price=0.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "invalid_price")


class TestEntryLimits(unittest.TestCase):
    def test_max_open_positions_blocks(self):
        manager = make_manager(max_open_positions=1)
        decision = manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0, open_positions=1)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "max_open_positions_reached")

    def test_min_equity_blocks(self):
        manager = make_manager(min_equity_usdt=60.0)
        decision = manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertEqual(decision.code, "equity_below_minimum")

    def test_no_cash_blocks(self):
        manager = make_manager(min_equity_usdt=0.0)
        decision = manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=0.0, open_positions=0)
        self.assertEqual(decision.code, "no_cash")

    def test_max_trades_per_day_blocks(self):
        manager = make_manager(max_trades_per_day=1)
        manager.note_entry_taken(TS, "BTC/USDT")
        decision = manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertEqual(decision.code, "max_trades_per_day_reached")

    def test_trades_today_counts_entries_not_closes(self):
        """Regression: a close must not consume the daily entry budget.

        The old code incremented ``trades_today`` on both the entry and the close,
        so ``max_trades_per_day: 8`` only allowed ~4 round trips.  One full
        entry -> close -> entry cycle must advance the counter by exactly 1 per
        entry (and by 0 on the close).
        """
        manager = make_manager(max_trades_per_day=2)
        manager.roll_day(TS, 50.0)
        self.assertEqual(manager.state.trades_today, 0)

        manager.note_entry_taken(TS, "BTC/USDT")           # entry 1
        self.assertEqual(manager.state.trades_today, 1)
        manager.register_trade_result(1.0, TS + 1)         # close 1 -> not counted
        self.assertEqual(manager.state.trades_today, 1)

        manager.note_entry_taken(TS + 2, "BTC/USDT")       # entry 2
        self.assertEqual(manager.state.trades_today, 2)
        manager.register_trade_result(1.0, TS + 3)         # close 2 -> not counted
        self.assertEqual(manager.state.trades_today, 2)

        blocked = manager.evaluate_entry(ts=TS + 4, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertFalse(blocked.approved)
        self.assertEqual(blocked.code, "max_trades_per_day_reached")

    def test_closing_a_trade_does_not_consume_the_daily_entry_budget(self):
        manager = make_manager(max_trades_per_day=1)
        manager.register_trade_result(1.0, TS)  # a profitable close on its own
        self.assertEqual(manager.state.trades_today, 0)
        self.assertTrue(manager.evaluate_entry(ts=TS, price=100.0, equity=50.0,
                                               cash=50.0, open_positions=0).approved)

    def test_daily_entry_budget_resets_on_the_next_utc_day(self):
        manager = make_manager(max_trades_per_day=1)
        manager.roll_day(TS, 50.0)
        manager.note_entry_taken(TS, "BTC/USDT")
        blocked = manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertEqual(blocked.code, "max_trades_per_day_reached")
        later = TS + DAY_MS
        self.assertTrue(manager.roll_day(later, 50.0))
        self.assertEqual(manager.state.trades_today, 0)
        self.assertTrue(manager.evaluate_entry(ts=later, price=100.0, equity=50.0,
                                               cash=50.0, open_positions=0).approved)

    def test_cooldown_after_losing_trade(self):
        manager = make_manager(cooldown_minutes=60.0)
        manager.register_trade_result(-1.0, TS, pair="BTC/USDT")
        blocked = manager.evaluate_entry(ts=TS + 600_000, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertFalse(blocked.approved)
        self.assertEqual(blocked.code, "cooldown_active")
        allowed = manager.evaluate_entry(ts=TS + 61 * 60_000, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        self.assertTrue(allowed.approved, allowed.reason)

    def test_profitable_trade_does_not_start_cooldown(self):
        manager = make_manager()
        manager.register_trade_result(1.0, TS)
        self.assertEqual(manager.state.cooldown_until_ts, 0)
        self.assertTrue(manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0,
                                              open_positions=0).approved)

    def test_cooldown_is_logged_in_the_audit_trail(self):
        manager = make_manager()
        manager.register_trade_result(-0.5, TS, pair="BTC/USDT")
        codes = [item["code"] for item in manager.drain_audit()]
        self.assertIn("cooldown_started", codes)


class TestDailyLossLimit(unittest.TestCase):
    def test_limit_halts_new_entries(self):
        manager = make_manager(daily_loss_limit_pct=5.0)  # 5 % of 50 = 2.5 USDT
        manager.roll_day(TS, 50.0)
        manager.register_trade_result(-2.6, TS + 1000)
        self.assertTrue(manager.state.halted)
        self.assertIn("daily_loss_limit", manager.state.halt_reason)

        decision = manager.evaluate_entry(ts=TS + 2000, price=100.0, equity=47.4, cash=47.4, open_positions=0)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "daily_loss_limit_reached")

    def test_limit_is_labelled_and_audited(self):
        manager = make_manager(daily_loss_limit_pct=5.0)
        manager.roll_day(TS, 50.0)
        manager.register_trade_result(-2.6, TS + 1000)
        codes = [item["code"] for item in manager.drain_audit()]
        self.assertIn("cooldown_started", codes)
        self.assertIn("daily_loss_limit_reached", codes)

    def test_losses_below_the_limit_do_not_halt(self):
        manager = make_manager(daily_loss_limit_pct=10.0)
        manager.roll_day(TS, 50.0)
        manager.register_trade_result(-1.0, TS + 1000)
        self.assertFalse(manager.state.halted)

    def test_halt_clears_on_the_next_utc_day(self):
        manager = make_manager(daily_loss_limit_pct=5.0)
        manager.roll_day(TS, 50.0)
        manager.register_trade_result(-3.0, TS + 1000)
        self.assertTrue(manager.state.halted)
        later = TS + DAY_MS + 1000
        manager.roll_day(later, 47.0)
        self.assertFalse(manager.state.halted)
        decision = manager.evaluate_entry(ts=later, price=100.0, equity=47.0, cash=47.0, open_positions=0)
        self.assertTrue(decision.approved, decision.reason)

    def test_daily_pnl_is_tracked_per_day(self):
        manager = make_manager(daily_loss_limit_pct=50.0)
        manager.roll_day(TS, 50.0)
        manager.register_trade_result(1.0, TS + 1)
        manager.register_trade_result(-0.5, TS + 2)
        self.assertAlmostEqual(manager.state.day_realized_net_pnl, 0.5, places=9)
        manager.roll_day(TS + DAY_MS, 50.5)
        self.assertAlmostEqual(manager.state.day_realized_net_pnl, 0.0, places=9)


class TestExitChecks(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()
        self.position = make_position(entry_fill=100.0, stop=97.5, tp=102.26)

    def test_stop_loss_hit(self):
        decision = self.manager.check_exit(self.position, high=101.0, low=97.0, close=98.0)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.trigger, EXIT_STOP_LOSS)
        self.assertAlmostEqual(decision.reference_price, 97.5, places=9)

    def test_take_profit_hit(self):
        decision = self.manager.check_exit(self.position, high=103.0, low=99.0, close=102.5)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.trigger, EXIT_TAKE_PROFIT)
        self.assertAlmostEqual(decision.reference_price, 102.26, places=9)

    def test_stop_wins_when_both_levels_are_touched(self):
        """Pessimistic intrabar assumption: the loss is booked, not the win."""
        decision = self.manager.check_exit(self.position, high=105.0, low=96.0, close=100.0)
        self.assertEqual(decision.trigger, EXIT_STOP_LOSS)

    def test_no_exit_inside_the_bracket(self):
        self.assertIsNone(self.manager.check_exit(self.position, high=101.5, low=98.5, close=100.0))

    def test_strategy_exit(self):
        decision = self.manager.strategy_exit(101.0, "exit:close_reached_middle_band")
        self.assertTrue(decision.should_exit)
        self.assertIn("middle_band", decision.reason)


class TestAuditObservability(unittest.TestCase):
    def test_approval_is_recorded(self):
        manager = make_manager()
        manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0, open_positions=0)
        audit = manager.drain_audit()
        self.assertEqual([item["code"] for item in audit], ["entry_approved"])
        self.assertEqual(manager.drain_audit(), [])

    def test_blocked_limits_are_recorded(self):
        manager = make_manager(max_open_positions=1, max_position_pct=100.0)
        manager.evaluate_entry(ts=TS, price=100.0, equity=50.0, cash=50.0, open_positions=1)
        audit = manager.drain_audit()
        self.assertEqual(audit[0]["code"], "max_open_positions_reached")
        self.assertIn("reason", audit[0])

    def test_state_snapshot_exposes_cooldown(self):
        manager = make_manager()
        manager.register_trade_result(-0.2, TS)
        snapshot = manager.state_snapshot(TS + 30_000)
        self.assertTrue(snapshot["cooldown_active"])
        self.assertGreater(snapshot["cooldown_remaining_seconds"], 0)

    def test_utc_day_boundary(self):
        self.assertEqual(utc_day(TS), "2023-11-14")
        self.assertEqual(utc_day(TS + DAY_MS), "2023-11-15")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
