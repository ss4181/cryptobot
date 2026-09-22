"""Ledger: rows, exports, derived balances and reconciliation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cryptobot.execution.paper_broker import PaperBroker, FaultInjector
from cryptobot.ledger.store import Ledger, iso_utc

PRICE = 30_000.0
FEE = 0.1
SLIP = 0.05
TS = 1_700_000_000_000
RUN_ID = "test-run-1"


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "ledger.sqlite"
        self.ledger = Ledger(self.db, RUN_ID)
        self.addCleanup(self.ledger.close)
        self.ledger.start_run(mode="paper", started_at=TS, version="test",
                              config={"initial_capital_usdt": 50.0}, data={"rows": 10})
        self.broker = PaperBroker(50.0, FEE, SLIP, max_open_positions=2)

    def _open(self, qty: float = 0.001, price: float = PRICE) -> None:
        result = self.broker.buy("BTC/USDT", price, qty, TS, stop_price=price * 0.97,
                                 tp_price=price * 1.03, reason="entry:bollinger_dip")
        self.ledger.record_open(result, mode="paper")

    def _close(self, price: float = PRICE * 1.03, reason: str = "take_profit") -> None:
        result = self.broker.sell("BTC/USDT", price, TS + 3_600_000, reason=reason)
        trade = dict(self.broker.closed_trades[-1])
        self.ledger.record_close(result, trade, mode="paper")

    def test_open_and_close_rows_are_written(self):
        self._open()
        self._close()
        rows = self.ledger.fetch_ledger()
        self.assertEqual([row["event"] for row in rows], ["OPEN", "CLOSE"])
        opened, closed = rows
        self.assertEqual(opened["pair"], "BTC/USDT")
        self.assertEqual(opened["side"], "buy")
        self.assertAlmostEqual(opened["qty"], 0.001, places=12)
        self.assertGreater(opened["fee"], 0.0)
        self.assertGreater(opened["fill_price"], opened["reference_price"])  # slippage on the buy
        self.assertEqual(closed["side"], "sell")
        self.assertLess(closed["fill_price"], closed["reference_price"])     # slippage on the sell
        self.assertNotEqual(closed["gross_pnl"], 0.0)
        self.assertNotEqual(closed["net_pnl"], 0.0)
        self.assertEqual(closed["reason"], "take_profit")
        self.assertEqual(opened["iso_utc"], iso_utc(TS))

    def test_trades_table_has_the_round_trip(self):
        self._open()
        self._close()
        trades = self.ledger.fetch_trades()
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(trade["pair"], "BTC/USDT")
        self.assertGreater(trade["entry_fill_price"], trade["entry_reference_price"])
        self.assertLess(trade["exit_fill_price"], trade["exit_reference_price"])
        self.assertAlmostEqual(trade["fees"], trade["entry_fee"] + trade["exit_fee"], places=9)
        self.assertAlmostEqual(trade["net_pnl"],
                               trade["gross_pnl"] - trade["fees"] - trade["slippage_cost"], places=9)

    def test_cash_recomputed_from_ledger_matches_broker(self):
        self._open()
        self._close()
        self.assertAlmostEqual(self.ledger.cash_from_ledger(50.0), self.broker.cash, places=9)

    def test_realized_pnl_recomputed_from_ledger_matches_broker(self):
        self._open()
        self._close()
        self.assertAlmostEqual(self.ledger.realized_net_from_ledger(), self.broker.realized_net_pnl, places=9)

    def test_open_quantity_derived_from_ledger(self):
        self._open()
        self.assertEqual(self.ledger.open_qty_from_ledger(), {"BTC/USDT": 0.001})
        self._close()
        self.assertEqual(self.ledger.open_qty_from_ledger(), {})

    def test_total_fees_from_ledger(self):
        self._open()
        self._close()
        self.assertAlmostEqual(self.ledger.total_fees_from_ledger(), self.broker.total_fees, places=9)

    def test_order_rows_for_rejections_and_partials(self):
        faults = FaultInjector().enable()
        faults.reject_next("buy", 1).partial_next("buy", 0.5)
        broker = PaperBroker(50.0, FEE, SLIP, faults=faults)
        rejected = broker.buy("BTC/USDT", PRICE, 0.001, TS, stop_price=1, tp_price=2)
        self.ledger.record_order(rejected, mode="paper")
        partial = broker.buy("BTC/USDT", PRICE, 0.003, TS + 1, stop_price=1, tp_price=2)
        self.ledger.record_order(partial, mode="paper")
        orders = self.ledger.fetch_orders()
        self.assertEqual(orders[0]["status"], "rejected")
        self.assertEqual(orders[0]["reason"], "synthetic_rejection")
        self.assertEqual(orders[1]["status"], "partial")
        self.assertAlmostEqual(orders[1]["requested_qty"], 0.003, places=12)
        self.assertAlmostEqual(orders[1]["filled_qty"], 0.0015, places=12)

    def test_events_are_recorded(self):
        self.ledger.record_event(TS, level="WARNING", category="risk", code="daily_loss_limit_reached",
                                 message="halted", payload={"day": "2023-11-14"})
        self.ledger.record_events([
            {"ts": TS, "code": "cooldown_started", "reason": "losing trade", "pair": "BTC/USDT"},
        ])
        events = self.ledger.fetch_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["code"], "daily_loss_limit_reached")
        self.assertEqual(events[1]["category"], "risk")
        self.assertIn("BTC/USDT", events[1]["payload_json"])

    def test_equity_rows(self):
        self.broker.mark_price("BTC/USDT", PRICE)
        point = self.broker.record_equity(TS)
        self.ledger.record_equity(point)
        rows = self.ledger.fetch_equity()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["equity"], 50.0, places=9)

    def test_counts(self):
        self._open()
        self._close()
        counts = self.ledger.counts()
        self.assertEqual(counts["ledger"], 2)
        self.assertEqual(counts["trades"], 1)

    def test_latest_run_id(self):
        self.assertEqual(self.ledger.latest_run_id(), RUN_ID)


class TestVerification(LedgerTestCase):
    def test_verification_passes_for_a_consistent_book(self):
        self._open()
        self._close()
        report = self.ledger.verify(
            initial_cash=50.0,
            broker_cash=self.broker.cash,
            broker_realized_net_pnl=self.broker.realized_net_pnl,
            broker_equity=self.broker.equity(),
            open_positions={},
            mark_prices={},
        )
        self.assertTrue(report.ok, report.render())
        self.assertEqual(len(report.checks), 4)

    def test_verification_passes_with_an_open_position(self):
        self._open()
        self.broker.mark_price("BTC/USDT", PRICE * 1.01)
        report = self.ledger.verify(
            initial_cash=50.0,
            broker_cash=self.broker.cash,
            broker_realized_net_pnl=self.broker.realized_net_pnl,
            broker_equity=self.broker.equity(),
            open_positions={pair: p.qty for pair, p in self.broker.positions.items()},
            mark_prices={"BTC/USDT": PRICE * 1.01},
        )
        self.assertTrue(report.ok, report.render())

    def test_equity_check_is_skipped_without_mark_prices(self):
        self._open()
        report = self.ledger.verify(
            initial_cash=50.0, broker_cash=self.broker.cash,
            broker_realized_net_pnl=self.broker.realized_net_pnl,
            open_positions={pair: p.qty for pair, p in self.broker.positions.items()},
        )
        self.assertTrue(report.ok, report.render())
        self.assertTrue(any("equity check skipped" in note for note in report.notes))

    def test_open_position_check_is_skipped_when_not_supplied(self):
        self._open()
        report = self.ledger.verify(initial_cash=50.0, broker_cash=self.broker.cash,
                                    broker_realized_net_pnl=self.broker.realized_net_pnl)
        self.assertTrue(report.ok, report.render())
        self.assertTrue(any("open-position check skipped" in note for note in report.notes))

    def test_verification_detects_a_tampered_ledger(self):
        self._open()
        self._close()
        # Inject a phantom close row that never happened in the broker.
        result = self.broker.sell("ETH/USDT", 2_000.0, TS)
        self.assertEqual(result.status, "rejected")
        self.ledger.record_close(
            type(result)(status="filled", pair="ETH/USDT", side="sell", requested_qty=1.0, filled_qty=1.0,
                         reference_price=2_000.0, fill_price=1_999.0, fee=2.0, notional=1_999.0,
                         reason="phantom", ts=TS + 5, position_id="phantom",
                         net_pnl=1_000.0, gross_pnl=1_000.0, slippage_cost=1.0),
            {"gross_pnl": 1_000.0, "net_pnl": 1_000.0, "net_pnl_pct": 50.0, "position_id": "phantom",
             "pair": "ETH/USDT", "entry_ts": TS, "exit_ts": TS + 5, "entry_reference_price": 1_000.0,
             "entry_fill_price": 1_000.0, "exit_reference_price": 2_000.0, "exit_fill_price": 1_999.0,
             "qty": 1.0, "entry_fee": 1.0, "exit_fee": 2.0, "fees": 3.0, "slippage_cost": 1.0,
             "gross_pnl_pct": 100.0, "entry_reason": "x", "exit_reason": "phantom"},
            mode="paper",
        )
        report = self.ledger.verify(initial_cash=50.0, broker_cash=self.broker.cash,
                                    broker_realized_net_pnl=self.broker.realized_net_pnl)
        self.assertFalse(report.ok)
        self.assertIn("FAIL", report.render())

    def test_tolerance_is_applied(self):
        self._open()
        open_positions = {pair: p.qty for pair, p in self.broker.positions.items()}
        report = self.ledger.verify(initial_cash=50.0, broker_cash=self.broker.cash + 1e-4,
                                    broker_realized_net_pnl=self.broker.realized_net_pnl,
                                    open_positions=open_positions, tolerance=1e-3)
        self.assertTrue(report.ok, report.render())
        strict = self.ledger.verify(initial_cash=50.0, broker_cash=self.broker.cash + 1e-4,
                                    broker_realized_net_pnl=self.broker.realized_net_pnl,
                                    open_positions=open_positions, tolerance=1e-9)
        self.assertFalse(strict.ok)


class TestExports(LedgerTestCase):
    def test_csv_exports(self):
        self._open()
        self._close()
        out = Path(self.tmp.name) / "csv"
        written = self.ledger.export_csv(out)
        self.assertEqual(set(written), {"ledger", "trades", "orders", "equity", "events"})
        for name, path in written.items():
            self.assertTrue(path.exists(), name)
        lines = (out / "ledger.csv").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 3)  # header + OPEN + CLOSE
        self.assertIn("OPEN", lines[1])

    def test_json_export(self):
        self._open()
        self._close()
        path = self.ledger.export_json(Path(self.tmp.name) / "ledger.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], RUN_ID)
        self.assertEqual(len(payload["ledger"]), 2)
        self.assertEqual(len(payload["trades"]), 1)
        self.assertIn("reconciliation", payload)
        self.assertEqual(payload["reconciliation"]["open_qty_from_ledger"], {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
