"""Pure cost/fill arithmetic shared by the broker, the risk manager and the backtester.

Everything here is a deterministic pure function (no I/O, no clock, no state),
which is what makes the backtest reproducible and the net-vs-gross target math
unit-testable.

Modelling convention
--------------------
* ``price``         -- reference market price (e.g. the close of a bar).
* ``slippage_pct``  -- one-sided slippage in percent, applied *against* us on
  both legs: buys fill above the reference, sells fill below it.
* ``fee_pct``       -- taker fee in percent, charged on the filled notional of
  both legs.
* ``entry_fill``    -- what we actually paid per unit, slippage included.
* Take-profit and stop-loss levels are derived from the **effective entry fill
  price**, i.e. from what we really paid, not from the pre-slippage reference.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "PCT",
    "BuyLeg",
    "SellLeg",
    "CostBreakdown",
    "entry_fill_price",
    "exit_fill_price",
    "fee_amount",
    "buy_leg",
    "sell_leg",
    "round_trip",
    "net_return_pct",
    "gross_return_pct",
    "required_gross_tp_pct",
    "required_tp_price",
    "cost_drag_pct",
    "stop_price_from_pct",
    "max_affordable_qty",
]

PCT = 100.0


def _fraction(pct: float) -> float:
    """Percent -> fraction, with validation."""
    value = float(pct)
    if value < 0:
        raise ValueError("percentage must be >= 0, got {!r}".format(pct))
    return value / PCT


def entry_fill_price(price: float, slippage_pct: float) -> float:
    """Buy fill price: reference price plus one-sided slippage."""
    price = float(price)
    if price <= 0:
        raise ValueError("price must be > 0, got {!r}".format(price))
    return price * (1.0 + _fraction(slippage_pct))


def exit_fill_price(price: float, slippage_pct: float) -> float:
    """Sell fill price: reference price minus one-sided slippage."""
    price = float(price)
    if price <= 0:
        raise ValueError("price must be > 0, got {!r}".format(price))
    return price * (1.0 - _fraction(slippage_pct))


def fee_amount(notional: float, fee_pct: float) -> float:
    """Taker fee charged on a filled notional."""
    return abs(float(notional)) * _fraction(fee_pct)


@dataclass(frozen=True)
class BuyLeg:
    """A simulated buy fill: cash leaves the account (notional + fee)."""

    reference_price: float
    fill_price: float
    qty: float
    notional: float
    fee: float

    @property
    def cash_flow(self) -> float:
        return -(self.notional + self.fee)


@dataclass(frozen=True)
class SellLeg:
    """A simulated sell fill: cash enters the account (notional - fee)."""

    reference_price: float
    fill_price: float
    qty: float
    notional: float
    fee: float

    @property
    def cash_flow(self) -> float:
        return self.notional - self.fee


def buy_leg(reference_price: float, qty: float, fee_pct: float, slippage_pct: float) -> BuyLeg:
    """Simulate a market buy at ``reference_price``."""
    if qty <= 0:
        raise ValueError("qty must be > 0, got {!r}".format(qty))
    fill = entry_fill_price(reference_price, slippage_pct)
    notional = fill * qty
    return BuyLeg(float(reference_price), fill, float(qty), notional, fee_amount(notional, fee_pct))


def sell_leg(reference_price: float, qty: float, fee_pct: float, slippage_pct: float) -> SellLeg:
    """Simulate a market sell at ``reference_price``."""
    if qty <= 0:
        raise ValueError("qty must be > 0, got {!r}".format(qty))
    fill = exit_fill_price(reference_price, slippage_pct)
    notional = fill * qty
    return SellLeg(float(reference_price), fill, float(qty), notional, fee_amount(notional, fee_pct))


@dataclass(frozen=True)
class CostBreakdown:
    """Gross vs net result of a completed long round trip."""

    entry_reference_price: float
    entry_fill_price: float
    exit_reference_price: float
    exit_fill_price: float
    qty: float
    gross_pnl: float
    fees: float
    slippage_cost: float
    net_pnl: float
    gross_pnl_pct: float
    net_pnl_pct: float


def round_trip(
    entry_reference_price: float,
    exit_reference_price: float,
    qty: float,
    fee_pct: float,
    slippage_pct: float,
    *,
    entry_fill: float | None = None,
) -> CostBreakdown:
    """Compute the full cost breakdown of a long round trip.

    ``entry_fill`` may be supplied when the entry was already filled (the
    broker's stored fill price); otherwise it is derived from the entry
    reference price and the configured slippage.
    """
    if qty <= 0:
        raise ValueError("qty must be > 0, got {!r}".format(qty))

    fill_in = entry_fill_price(entry_reference_price, slippage_pct) if entry_fill is None else float(entry_fill)
    fill_out = exit_fill_price(exit_reference_price, slippage_pct)

    notional_in = fill_in * qty
    notional_out = fill_out * qty
    fee_in = fee_amount(notional_in, fee_pct)
    fee_out = fee_amount(notional_out, fee_pct)

    gross_pnl = (float(exit_reference_price) - float(entry_reference_price)) * qty
    fees = fee_in + fee_out
    # Slippage cost vs. trading the reference prices on both legs.
    slippage_cost = ((fill_in - float(entry_reference_price)) + (float(exit_reference_price) - fill_out)) * qty
    net_pnl = (notional_out - fee_out) - (notional_in + fee_in)
    cost_basis = notional_in + fee_in

    return CostBreakdown(
        entry_reference_price=float(entry_reference_price),
        entry_fill_price=fill_in,
        exit_reference_price=float(exit_reference_price),
        exit_fill_price=fill_out,
        qty=float(qty),
        gross_pnl=gross_pnl,
        fees=fees,
        slippage_cost=slippage_cost,
        net_pnl=net_pnl,
        gross_pnl_pct=gross_return_pct(entry_reference_price, exit_reference_price),
        net_pnl_pct=(net_pnl / cost_basis * PCT) if cost_basis else 0.0,
    )


def gross_return_pct(entry_reference_price: float, exit_reference_price: float) -> float:
    """Gross (pre-cost) return in percent, on the entry reference price."""
    if entry_reference_price <= 0:
        raise ValueError("entry price must be > 0")
    return (float(exit_reference_price) / float(entry_reference_price) - 1.0) * PCT


def net_return_pct(
    entry_fill: float,
    exit_reference_price: float,
    fee_pct: float,
    slippage_pct: float,
) -> float:
    """Net return in percent of the cash actually committed by the buy leg.

    This is the quantity the ``net_profit_target_pct`` config knob refers to.
    """
    f = _fraction(fee_pct)
    s = _fraction(slippage_pct)
    if entry_fill <= 0:
        raise ValueError("entry fill must be > 0")
    exit_eff = exit_fill_price(exit_reference_price, slippage_pct)
    cost = float(entry_fill) * (1.0 + f)
    proceeds = exit_eff * (1.0 - f)
    return (proceeds - cost) / cost * PCT


def required_gross_tp_pct(net_target_pct: float, fee_pct: float, slippage_pct: float) -> float:
    """Gross take-profit level (in %) needed to net ``net_target_pct`` after costs.

    Derived by solving :func:`net_return_pct` for the exit price::

        exit_ref = entry_fill * (1 + fee) * (1 + target) / ((1 - slip) * (1 - fee))

    so the required gross move above the entry *fill* price is::

        gross = ((1 + fee) * (1 + target)) / ((1 - slip) * (1 - fee)) - 1

    Example (fee 0.1 %, slippage 0.05 %, target 2 %): 2.2553 % -- the bot must
    gain ~2.26 % gross (on the entry fill price) so that the round trip nets
    2 % of the cash committed.  ``net_return_pct`` inverts this exactly.
    """
    target = _fraction(net_target_pct)
    f = _fraction(fee_pct)
    s = _fraction(slippage_pct)
    if s >= 1.0 or f >= 1.0:
        raise ValueError("fee/slippage must be < 100%")
    return (((1.0 + f) * (1.0 + target)) / ((1.0 - s) * (1.0 - f)) - 1.0) * PCT


def required_tp_price(
    entry_fill: float,
    net_target_pct: float,
    fee_pct: float,
    slippage_pct: float,
) -> float:
    """Absolute take-profit reference price for a net ``net_target_pct`` gain."""
    return float(entry_fill) * (1.0 + required_gross_tp_pct(net_target_pct, fee_pct, slippage_pct) / PCT)


def cost_drag_pct(net_target_pct: float, fee_pct: float, slippage_pct: float) -> float:
    """How many percentage points the round trip's costs consume."""
    return required_gross_tp_pct(net_target_pct, fee_pct, slippage_pct) - float(net_target_pct)


def stop_price_from_pct(entry_fill: float, stop_loss_pct: float) -> float:
    """Stop-loss reference price for a long position.

    The *net* loss realised at that level is larger than ``stop_loss_pct``
    because the exit leg still pays fee and slippage; see ``RISK.md``.
    """
    return float(entry_fill) * (1.0 - _fraction(stop_loss_pct))


def max_affordable_qty(
    cash: float,
    reference_price: float,
    fee_pct: float,
    slippage_pct: float,
) -> float:
    """Largest quantity buyable with ``cash`` including fee and slippage."""
    fill = entry_fill_price(reference_price, slippage_pct)
    per_unit = fill * (1.0 + _fraction(fee_pct))
    if per_unit <= 0:
        return 0.0
    return max(0.0, float(cash) / per_unit)
