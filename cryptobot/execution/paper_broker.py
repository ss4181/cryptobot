"""Simulated broker: fees + slippage, cash accounting, rejections, fault injection.

This is the only thing in the project that "fills" anything, and it fills
against a price the caller passes in -- there is no exchange client here, no
authenticated request helper and no order-submission API.  See the
``no-live-order`` check in ``scripts/verify_all.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .costs import (
    PCT,
    CostBreakdown,
    buy_leg,
    entry_fill_price,
    max_affordable_qty,
    round_trip,
    sell_leg,
)

log = logging.getLogger(__name__)

FILLED = "filled"
PARTIAL = "partial"
REJECTED = "rejected"

#: Binance spot rejects dust orders; keep the simulation honest about it.
DEFAULT_MIN_NOTIONAL_USDT = 5.0


@dataclass
class Position:
    """An open long spot position."""

    pair: str
    qty: float
    entry_reference_price: float
    entry_fill_price: float
    entry_fee: float
    entry_ts: int
    stop_price: float
    tp_price: float
    reason: str = ""
    position_id: str = ""

    @property
    def cost_basis(self) -> float:
        """Cash committed by the entry leg, fee included."""
        return self.entry_fill_price * self.qty + self.entry_fee

    @property
    def notional_at_fill(self) -> float:
        return self.entry_fill_price * self.qty

    def unrealized_pnl(self, price: float, fee_pct: float, slippage_pct: float) -> float:
        """Net PnL if the position were closed at ``price`` right now."""
        breakdown = round_trip(
            self.entry_reference_price, price, self.qty, fee_pct, slippage_pct, entry_fill=self.entry_fill_price
        )
        return breakdown.net_pnl

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pair": self.pair,
            "qty": self.qty,
            "entry_reference_price": self.entry_reference_price,
            "entry_fill_price": self.entry_fill_price,
            "entry_fee": self.entry_fee,
            "entry_ts": self.entry_ts,
            "stop_price": self.stop_price,
            "tp_price": self.tp_price,
            "cost_basis": self.cost_basis,
            "reason": self.reason,
            "position_id": self.position_id,
        }


@dataclass(frozen=True)
class OrderResult:
    """Outcome of a simulated order."""

    status: str
    pair: str
    side: str
    requested_qty: float
    filled_qty: float
    reference_price: float
    fill_price: float
    fee: float
    notional: float
    reason: str
    ts: int
    position_id: Optional[str] = None
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    slippage_cost: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in (FILLED, PARTIAL)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "pair": self.pair, "side": self.side,
            "requested_qty": self.requested_qty, "filled_qty": self.filled_qty,
            "reference_price": self.reference_price, "fill_price": self.fill_price,
            "fee": self.fee, "notional": self.notional, "reason": self.reason,
            "ts": self.ts, "position_id": self.position_id,
            "net_pnl": self.net_pnl, "gross_pnl": self.gross_pnl,
            "slippage_cost": self.slippage_cost,
        }


@dataclass
class EquityPoint:
    ts: int
    cash: float
    positions_value: float
    equity: float
    open_positions: int
    realized_net_pnl: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts, "cash": self.cash, "positions_value": self.positions_value,
            "equity": self.equity, "open_positions": self.open_positions,
            "realized_net_pnl": self.realized_net_pnl,
        }


@dataclass
class FaultInjector:
    """Deterministic, opt-in failure injection for tests and demos.

    Disabled unless :meth:`enable` is called, so normal runs cannot be affected
    by it.  Every injection is deterministic (a countdown), never random, so
    tests and backtests stay reproducible.
    """

    enabled: bool = False
    force_insufficient_cash: bool = False
    _reject_buy: int = 0
    _reject_sell: int = 0
    _partial_buy: List[float] = field(default_factory=list)
    _partial_sell: List[float] = field(default_factory=list)
    injected: List[str] = field(default_factory=list)

    def enable(self) -> "FaultInjector":
        self.enabled = True
        return self

    def reject_next(self, side: str, count: int = 1) -> "FaultInjector":
        if side == "buy":
            self._reject_buy += int(count)
        else:
            self._reject_sell += int(count)
        return self

    def partial_next(self, side: str, factor: float) -> "FaultInjector":
        if not 0 < float(factor) < 1:
            raise ValueError("partial fill factor must be in (0, 1)")
        (self._partial_buy if side == "buy" else self._partial_sell).append(float(factor))
        return self

    def block_cash(self, flag: bool = True) -> "FaultInjector":
        self.force_insufficient_cash = bool(flag)
        return self

    # ---------------------------------------------------------------- consume
    def take_reject(self, side: str) -> bool:
        if not self.enabled:
            return False
        bucket = "_reject_buy" if side == "buy" else "_reject_sell"
        if getattr(self, bucket) > 0:
            setattr(self, bucket, getattr(self, bucket) - 1)
            self.injected.append("reject:{}".format(side))
            return True
        return False

    def take_partial(self, side: str) -> Optional[float]:
        if not self.enabled:
            return None
        queue = self._partial_buy if side == "buy" else self._partial_sell
        if queue:
            factor = queue.pop(0)
            self.injected.append("partial:{}:{:.2f}".format(side, factor))
            return factor
        return None


class PaperBroker:
    """Cash + positions book for simulated fills."""

    def __init__(
        self,
        initial_cash: float,
        fee_pct: float,
        slippage_pct: float,
        *,
        min_notional_usdt: float = DEFAULT_MIN_NOTIONAL_USDT,
        max_open_positions: int = 1,
        faults: Optional[FaultInjector] = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self.min_notional_usdt = float(min_notional_usdt)
        self.max_open_positions = int(max_open_positions)
        self.faults = faults if faults is not None else FaultInjector()

        self.positions: Dict[str, Position] = {}
        self.last_prices: Dict[str, float] = {}
        self.equity_history: List[EquityPoint] = []

        self.realized_gross_pnl = 0.0
        self.realized_net_pnl = 0.0
        self.total_fees = 0.0
        self.total_slippage_cost = 0.0
        self.closed_trades: List[Dict[str, Any]] = []

        self.stats = {
            "submitted": 0, "filled": 0, "partial": 0, "rejected": 0,
            "rejected_reasons": {},
        }

    # ------------------------------------------------------------------ state
    def mark_price(self, pair: str, price: float) -> None:
        if price <= 0:
            raise ValueError("price must be > 0")
        self.last_prices[pair] = float(price)

    def price_of(self, pair: str) -> Optional[float]:
        """Last marked price, falling back to the entry fill price."""
        if pair in self.last_prices:
            return self.last_prices[pair]
        if pair in self.positions:
            return self.positions[pair].entry_fill_price
        return None

    @property
    def open_positions(self) -> int:
        return len(self.positions)

    def has_position(self, pair: str) -> bool:
        return pair in self.positions

    def positions_value(self) -> float:
        total = 0.0
        for pair, position in self.positions.items():
            price = self.price_of(pair) or position.entry_fill_price
            total += price * position.qty
        return total

    def equity(self) -> float:
        """Cash plus open positions marked at the last price."""
        return self.cash + self.positions_value()

    def unrealized_pnl(self) -> float:
        return sum(
            p.unrealized_pnl(self.price_of(pair) or p.entry_fill_price, self.fee_pct, self.slippage_pct)
            for pair, p in self.positions.items()
        )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "cash": self.cash,
            "positions_value": self.positions_value(),
            "equity": self.equity(),
            "open_positions": self.open_positions,
            "realized_net_pnl": self.realized_net_pnl,
            "realized_gross_pnl": self.realized_gross_pnl,
            "unrealized_net_pnl": self.unrealized_pnl(),
            "total_fees": self.total_fees,
            "total_slippage_cost": self.total_slippage_cost,
            "closed_trades": len(self.closed_trades),
            "stats": {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.stats.items()},
            "positions": {pair: p.as_dict() for pair, p in sorted(self.positions.items())},
            "last_prices": dict(sorted(self.last_prices.items())),
        }

    def record_equity(self, ts: int) -> EquityPoint:
        point = EquityPoint(
            ts=int(ts),
            cash=self.cash,
            positions_value=self.positions_value(),
            equity=self.equity(),
            open_positions=self.open_positions,
            realized_net_pnl=self.realized_net_pnl,
        )
        self.equity_history.append(point)
        return point

    # ------------------------------------------------------------------ stats
    def _reject(self, side: str, pair: str, qty: float, price: float, ts: int, reason: str) -> OrderResult:
        self.stats["rejected"] += 1
        self.stats["rejected_reasons"][reason] = self.stats["rejected_reasons"].get(reason, 0) + 1
        log.warning(
            "order.rejected",
            extra={"event": "order_rejected", "pair": pair, "side": side,
                   "requested_qty": qty, "reason": reason},
        )
        return OrderResult(REJECTED, pair, side, float(qty), 0.0, float(price), 0.0, 0.0, 0.0, reason, int(ts))

    def _count(self, status: str) -> None:
        self.stats["submitted"] += 1
        if status == FILLED:
            self.stats["filled"] += 1
        elif status == PARTIAL:
            self.stats["partial"] += 1

    # -------------------------------------------------------------------- buy
    def buy(
        self,
        pair: str,
        reference_price: float,
        qty: float,
        ts: int,
        *,
        stop_price: float,
        tp_price: float,
        reason: str = "",
        position_id: str = "",
        min_notional_usdt: Optional[float] = None,
    ) -> OrderResult:
        """Simulate a market buy. Never talks to an exchange."""
        min_notional = self.min_notional_usdt if min_notional_usdt is None else float(min_notional_usdt)

        if self.faults.take_reject("buy"):
            return self._reject("buy", pair, qty, reference_price, ts, "synthetic_rejection")
        if qty <= 0 or reference_price <= 0:
            return self._reject("buy", pair, qty, reference_price, ts, "invalid_quantity_or_price")
        if self.has_position(pair):
            return self._reject("buy", pair, qty, reference_price, ts, "position_already_open")
        if self.open_positions >= self.max_open_positions:
            return self._reject("buy", pair, qty, reference_price, ts, "max_open_positions_reached")

        requested = float(qty)
        factor = self.faults.take_partial("buy")
        filled_qty = requested * factor if factor else requested

        leg = buy_leg(reference_price, filled_qty, self.fee_pct, self.slippage_pct)
        if leg.notional < min_notional:
            return self._reject("buy", pair, requested, reference_price, ts, "below_min_notional")
        if leg.notional + leg.fee > self.cash + 1e-12 or self.faults.force_insufficient_cash:
            affordable = max_affordable_qty(self.cash, reference_price, self.fee_pct, self.slippage_pct)
            return self._reject(
                "buy", pair, requested, reference_price, ts,
                "insufficient_balance(affordable_qty={:.8f})".format(affordable),
            )

        self.cash -= leg.notional + leg.fee
        self.total_fees += leg.fee
        self.total_slippage_cost += (leg.fill_price - leg.reference_price) * filled_qty
        position = Position(
            pair=pair,
            qty=filled_qty,
            entry_reference_price=leg.reference_price,
            entry_fill_price=leg.fill_price,
            entry_fee=leg.fee,
            entry_ts=int(ts),
            stop_price=float(stop_price),
            tp_price=float(tp_price),
            reason=reason,
            position_id=position_id or "pos-{}-{}".format(pair.replace("/", ""), ts),
        )
        self.positions[pair] = position

        status = PARTIAL if factor else FILLED
        self._count(status)
        log.info(
            "order.buy",
            extra={"event": "order_buy", "pair": pair, "status": status, "qty": filled_qty,
                   "reference_price": leg.reference_price, "fill_price": leg.fill_price,
                   "fee": leg.fee, "notional": leg.notional, "reason": reason},
        )
        return OrderResult(
            status=status, pair=pair, side="buy", requested_qty=requested, filled_qty=filled_qty,
            reference_price=leg.reference_price, fill_price=leg.fill_price, fee=leg.fee,
            notional=leg.notional, reason=reason, ts=int(ts), position_id=position.position_id,
        )

    # ------------------------------------------------------------------- sell
    def sell(
        self,
        pair: str,
        reference_price: float,
        ts: int,
        *,
        qty: Optional[float] = None,
        reason: str = "",
    ) -> OrderResult:
        """Simulate a market sell of an existing long position."""
        if self.faults.take_reject("sell"):
            return self._reject("sell", pair, qty or 0.0, reference_price, ts, "synthetic_rejection")
        position = self.positions.get(pair)
        if position is None:
            return self._reject("sell", pair, qty or 0.0, reference_price, ts, "no_open_position")

        requested = position.qty if qty is None else float(qty)
        if requested <= 0 or reference_price <= 0:
            return self._reject("sell", pair, requested, reference_price, ts, "invalid_quantity_or_price")
        if requested > position.qty + 1e-12:
            return self._reject("sell", pair, requested, reference_price, ts, "insufficient_position")

        factor = self.faults.take_partial("sell")
        filled_qty = requested * factor if factor else requested

        leg = sell_leg(reference_price, filled_qty, self.fee_pct, self.slippage_pct)
        if leg.notional < self.min_notional_usdt:
            return self._reject("sell", pair, requested, reference_price, ts, "below_min_notional")

        breakdown: CostBreakdown = round_trip(
            position.entry_reference_price, reference_price, filled_qty,
            self.fee_pct, self.slippage_pct, entry_fill=position.entry_fill_price,
        )

        self.cash += leg.notional - leg.fee
        self.total_fees += leg.fee
        self.total_slippage_cost += (leg.reference_price - leg.fill_price) * filled_qty
        self.realized_gross_pnl += breakdown.gross_pnl
        self.realized_net_pnl += breakdown.net_pnl

        remaining = position.qty - filled_qty
        original_qty = position.qty
        status = PARTIAL if remaining > 1e-12 else FILLED
        # Pro-rate the entry fee when only part of the position is closed.
        allocated_entry_fee = position.entry_fee * (filled_qty / original_qty) if original_qty else 0.0

        trade = {
            "pair": pair,
            "position_id": position.position_id,
            "entry_ts": position.entry_ts,
            "exit_ts": int(ts),
            "entry_reference_price": position.entry_reference_price,
            "entry_fill_price": position.entry_fill_price,
            "exit_reference_price": leg.reference_price,
            "exit_fill_price": leg.fill_price,
            "qty": filled_qty,
            "entry_fee": allocated_entry_fee,
            "exit_fee": leg.fee,
            "fees": breakdown.fees,
            "gross_pnl": breakdown.gross_pnl,
            "net_pnl": breakdown.net_pnl,
            "net_pnl_pct": breakdown.net_pnl_pct,
            "gross_pnl_pct": breakdown.gross_pnl_pct,
            "slippage_cost": breakdown.slippage_cost,
            "exit_reason": reason,
            "entry_reason": position.reason,
            "status": status,
        }
        self.closed_trades.append(trade)

        if remaining > 1e-12:
            position.qty = remaining
        else:
            del self.positions[pair]

        self._count(status)
        log.info(
            "order.sell",
            extra={"event": "order_sell", "pair": pair, "status": status, "qty": filled_qty,
                   "reference_price": leg.reference_price, "fill_price": leg.fill_price,
                   "net_pnl": breakdown.net_pnl, "net_pnl_pct": round(breakdown.net_pnl_pct, 4),
                   "reason": reason},
        )
        return OrderResult(
            status=status, pair=pair, side="sell", requested_qty=requested, filled_qty=filled_qty,
            reference_price=leg.reference_price, fill_price=leg.fill_price, fee=leg.fee,
            notional=leg.notional, reason=reason, ts=int(ts), position_id=position.position_id,
            net_pnl=breakdown.net_pnl, gross_pnl=breakdown.gross_pnl, slippage_cost=breakdown.slippage_cost,
        )

    # -------------------------------------------------------------- reporting
    def summary(self) -> Dict[str, Any]:
        trades = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t["net_pnl"] > 0)
        return {
            "initial_cash": self.initial_cash,
            "cash": round(self.cash, 8),
            "equity": round(self.equity(), 8),
            "positions_value": round(self.positions_value(), 8),
            "open_positions": self.open_positions,
            "closed_trades": trades,
            "winning_trades": wins,
            "win_rate_pct": round(wins / trades * PCT, 4) if trades else 0.0,
            "realized_gross_pnl": round(self.realized_gross_pnl, 8),
            "realized_net_pnl": round(self.realized_net_pnl, 8),
            "unrealized_net_pnl": round(self.unrealized_pnl(), 8),
            "total_fees": round(self.total_fees, 8),
            "total_slippage_cost": round(self.total_slippage_cost, 8),
            "total_cost_drag": round(self.total_fees + self.total_slippage_cost, 8),
            "orders": dict(self.stats),
        }


__all__ = [
    "FILLED", "PARTIAL", "REJECTED", "DEFAULT_MIN_NOTIONAL_USDT",
    "Position", "OrderResult", "EquityPoint", "FaultInjector", "PaperBroker",
]
