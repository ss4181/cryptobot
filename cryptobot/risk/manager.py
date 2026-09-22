"""Risk manager: sizing, stop-loss, net take-profit, daily loss limit, cooldown.

Every decision this class makes is returned as a small immutable record with a
machine-readable ``code`` and a human ``reason``.  The runner logs them and
mirrors them into the ledger's ``events`` table, so each limit is observable in
both places (requirement: "every limit must be observable in logs and in the
ledger").

The take-profit is derived from the *net* target: ``required_tp_price`` solves
for the exit price that nets ``net_profit_target_pct`` after paying the taker
fee on both legs and slippage on both legs.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..execution.costs import (
    PCT,
    cost_drag_pct,
    entry_fill_price,
    max_affordable_qty,
    required_gross_tp_pct,
    required_tp_price,
    stop_price_from_pct,
)
from ..execution.paper_broker import DEFAULT_MIN_NOTIONAL_USDT, Position

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
MINUTE_MS = 60_000

EXIT_STOP_LOSS = "stop_loss"
EXIT_TAKE_PROFIT = "take_profit"
EXIT_STRATEGY = "strategy"


def utc_day(ts_ms: int) -> str:
    """ISO date of the UTC day containing ``ts_ms`` (the ledger's day boundary)."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class EntryDecision:
    """Result of asking the risk manager whether a new position may be opened."""

    approved: bool
    code: str
    reason: str
    qty: float = 0.0
    notional: float = 0.0
    entry_fill_estimate: float = 0.0
    stop_price: float = 0.0
    tp_price: float = 0.0
    gross_tp_pct: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExitDecision:
    """Result of checking a protective exit against one bar."""

    should_exit: bool
    trigger: str
    reference_price: float
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RiskState:
    """Mutable per-run risk state."""

    day: Optional[str] = None
    day_start_equity: float = 0.0
    day_realized_net_pnl: float = 0.0
    trades_today: int = 0
    halted: bool = False
    halt_reason: str = ""
    cooldown_until_ts: int = 0
    losses_after_cooldown: int = 0

    def as_dict(self, now_ts: Optional[int] = None) -> Dict[str, Any]:
        out = {
            "day": self.day,
            "day_start_equity": round(self.day_start_equity, 8),
            "day_realized_net_pnl": round(self.day_realized_net_pnl, 8),
            "trades_today": self.trades_today,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "cooldown_until_ts": self.cooldown_until_ts,
        }
        if now_ts is not None:
            out["cooldown_active"] = now_ts < self.cooldown_until_ts
            out["cooldown_remaining_seconds"] = max(0, (self.cooldown_until_ts - now_ts) // 1000)
        return out


class RiskManager:
    """Enforces every per-trade and per-day limit."""

    def __init__(
        self,
        *,
        initial_equity: float,
        fee_pct: float,
        slippage_pct: float,
        net_profit_target_pct: float,
        stop_loss_pct: float,
        max_position_pct: float,
        max_open_positions: int,
        daily_loss_limit_pct: float,
        cooldown_minutes: float,
        min_equity_usdt: float,
        max_trades_per_day: int,
        gross_take_profit_pct: Optional[float] = None,
        min_notional_usdt: float = DEFAULT_MIN_NOTIONAL_USDT,
    ) -> None:
        if initial_equity <= 0:
            raise ValueError("initial_equity must be > 0")
        self.initial_equity = float(initial_equity)
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self.net_profit_target_pct = float(net_profit_target_pct)
        self.stop_loss_pct = float(stop_loss_pct)
        self.max_position_pct = float(max_position_pct)
        self.max_open_positions = int(max_open_positions)
        self.daily_loss_limit_pct = float(daily_loss_limit_pct)
        self.cooldown_minutes = float(cooldown_minutes)
        self.min_equity_usdt = float(min_equity_usdt)
        self.max_trades_per_day = int(max_trades_per_day)
        self.min_notional_usdt = float(min_notional_usdt)

        self.gross_tp_pct = (
            float(gross_take_profit_pct)
            if gross_take_profit_pct is not None
            else required_gross_tp_pct(self.net_profit_target_pct, self.fee_pct, self.slippage_pct)
        )
        #: True when the user pinned the gross target instead of deriving it.
        self.gross_tp_overridden = gross_take_profit_pct is not None
        self.cost_drag_pct = cost_drag_pct(self.net_profit_target_pct, self.fee_pct, self.slippage_pct)
        self.state = RiskState(day_start_equity=self.initial_equity)
        self.audit: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ audit
    def _record(self, ts: int, code: str, reason: str, **extra: Any) -> None:
        entry = {"ts": int(ts), "code": code, "reason": reason}
        entry.update(extra)
        self.audit.append(entry)

    def _limit_hit(self, ts: int, code: str, reason: str, **extra: Any) -> EntryDecision:
        self._record(ts, code, reason, **extra)
        log.warning("risk.limit", extra={"event": "risk_limit", "code": code, "reason": reason, **extra})
        return EntryDecision(False, code, reason)

    # -------------------------------------------------------------- day logic
    def roll_day(self, ts: int, equity: float) -> bool:
        """Reset the daily counters when the UTC day changes. Returns True on a new day.

        ``trades_today`` (the count of new *entries* taken today) is reset to 0
        so the ``max_trades_per_day`` budget is replenished for the new UTC day.
        """
        day = utc_day(ts)
        if self.state.day == day:
            return False
        previous = self.state.day
        self.state.day = day
        self.state.day_start_equity = float(equity)
        self.state.day_realized_net_pnl = 0.0
        self.state.trades_today = 0
        if self.state.halted:
            self.state.halted = False
            self.state.halt_reason = ""
            log.info(
                "risk.daily_reset",
                extra={"event": "risk_daily_reset", "day": day, "equity": round(equity, 8),
                       "previous_day": previous, "note": "halt cleared for the new UTC day"},
            )
        else:
            log.info("risk.daily_reset", extra={"event": "risk_daily_reset", "day": day,
                                                "equity": round(equity, 8), "previous_day": previous})
        return True

    def note_equity(self, ts: int, equity: float) -> bool:
        """Feed the manager the current equity; rolls the day if needed."""
        return self.roll_day(ts, equity)

    def register_trade_result(self, net_pnl: float, ts: int, *, pair: str = "") -> None:
        """Called whenever a position is closed; drives cooldown and daily loss limit.

        Closing a position must **not** consume the daily entry budget:
        ``trades_today`` counts *entries* only and is incremented exclusively by
        :meth:`note_entry_taken`.  Counting the close here as well would halve
        the documented ``max_trades_per_day`` (one increment per entry plus one
        per close).
        """
        self.state.day_realized_net_pnl += float(net_pnl)

        if net_pnl < 0:
            self.state.cooldown_until_ts = int(ts) + int(self.cooldown_minutes * MINUTE_MS)
            self.state.losses_after_cooldown += 1
            log.info(
                "risk.cooldown_started",
                extra={"event": "risk_cooldown_started", "pair": pair, "net_pnl": round(net_pnl, 8),
                       "cooldown_minutes": self.cooldown_minutes, "cooldown_until_ts": self.state.cooldown_until_ts},
            )
            self._record(int(ts), "cooldown_started",
                         "losing trade -> no new entry for {:.0f} min".format(self.cooldown_minutes),
                         pair=pair, net_pnl=round(float(net_pnl), 8))

        self._check_daily_loss(int(ts))

    def _check_daily_loss(self, ts: int) -> None:
        if self.daily_loss_limit_pct <= 0 or self.state.halted:
            return
        limit = -abs(self.daily_loss_limit_pct) / PCT * self.state.day_start_equity
        if self.state.day_realized_net_pnl <= limit:
            self.state.halted = True
            self.state.halt_reason = (
                "daily_loss_limit: realized {:.4f} USDT <= limit {:.4f} USDT ({:.2f}% of {:.4f}) on {}".format(
                    self.state.day_realized_net_pnl, limit, self.daily_loss_limit_pct,
                    self.state.day_start_equity, self.state.day,
                )
            )
            log.critical(
                "risk.halted",
                extra={"event": "risk_halted", "code": "daily_loss_limit_reached",
                       "detail": self.state.halt_reason, "day": self.state.day},
            )
            self._record(ts, "daily_loss_limit_reached", self.state.halt_reason,
                         day_realized_net_pnl=round(self.state.day_realized_net_pnl, 8))

    def equity_halted(self) -> bool:
        return self.state.halted

    # ------------------------------------------------------------ entry gate
    def entry_blocked_reason(self, ts: int, *, equity: float, cash: float, open_positions: int) -> Optional[Tuple[str, str]]:
        """Return ``(code, reason)`` if a new entry is not allowed, else ``None``."""
        if self.state.halted:
            return "daily_loss_limit_reached", self.state.halt_reason or "trading halted for the day"
        if open_positions >= self.max_open_positions:
            return "max_open_positions_reached", "already holding {} of {} allowed positions".format(
                open_positions, self.max_open_positions
            )
        if equity < self.min_equity_usdt:
            return "equity_below_minimum", "equity {:.4f} < minimum {:.4f} USDT".format(equity, self.min_equity_usdt)
        if self.state.trades_today >= self.max_trades_per_day:
            return "max_trades_per_day_reached", "{} entries already taken today (cap {})".format(
                self.state.trades_today, self.max_trades_per_day
            )
        if ts < self.state.cooldown_until_ts:
            remaining = (self.state.cooldown_until_ts - ts) / 60000.0
            return "cooldown_active", "cooldown from a losing trade, {:.1f} min remaining".format(remaining)
        if cash <= 0:
            return "no_cash", "no free cash available"
        return None

    def evaluate_entry(
        self,
        *,
        ts: int,
        price: float,
        equity: float,
        cash: float,
        open_positions: int,
    ) -> EntryDecision:
        """Size a candidate entry and apply every limit."""
        blocked = self.entry_blocked_reason(ts, equity=equity, cash=cash, open_positions=open_positions)
        if blocked is not None:
            code, reason = blocked
            return self._limit_hit(ts, code, reason)

        if price <= 0:
            return self._limit_hit(ts, "invalid_price", "reference price must be > 0")

        fill_estimate = entry_fill_price(price, self.slippage_pct)
        allowance = equity * self.max_position_pct / PCT
        qty_by_equity = allowance / fill_estimate if fill_estimate > 0 else 0.0
        qty_by_cash = max_affordable_qty(cash, price, self.fee_pct, self.slippage_pct)
        qty = min(qty_by_equity, qty_by_cash)

        notional = qty * fill_estimate
        if qty <= 0 or notional < self.min_notional_usdt:
            return self._limit_hit(
                ts, "below_min_notional",
                "sized notional {:.4f} USDT < exchange minimum {:.4f} USDT (equity {:.4f}, cash {:.4f})".format(
                    notional, self.min_notional_usdt, equity, cash
                ),
            )

        stop_price = stop_price_from_pct(fill_estimate, self.stop_loss_pct)
        if self.gross_tp_overridden:
            tp_price = fill_estimate * (1.0 + self.gross_tp_pct / PCT)
        else:
            tp_price = required_tp_price(fill_estimate, self.net_profit_target_pct, self.fee_pct, self.slippage_pct)

        decision = EntryDecision(
            approved=True,
            code="approved",
            reason="size={:.8f} notional={:.4f} stop={:.4f} tp={:.4f} (net target {:.2f}% / gross {:.4f}%)".format(
                qty, notional, stop_price, tp_price, self.net_profit_target_pct, self.gross_tp_pct
            ),
            qty=qty,
            notional=notional,
            entry_fill_estimate=fill_estimate,
            stop_price=stop_price,
            tp_price=tp_price,
            gross_tp_pct=self.gross_tp_pct,
        )
        self._record(ts, "entry_approved", decision.reason, qty=qty, notional=round(notional, 8),
                     stop_price=stop_price, tp_price=tp_price)
        return decision

    def note_entry_taken(self, ts: int, pair: str) -> None:
        """Count an accepted entry against ``max_trades_per_day``.

        This is the **only** place ``trades_today`` is incremented: the daily cap
        is a cap on new *entries*, not on round trips.  A close never touches it.
        """
        self.state.trades_today += 1
        self._record(ts, "entry_taken", "position opened on {}".format(pair), pair=pair,
                     trades_today=self.state.trades_today)

    # ------------------------------------------------------------- exit gate
    def check_exit(
        self,
        position: Position,
        *,
        high: float,
        low: float,
        close: float,
    ) -> Optional[ExitDecision]:
        """Protective exits for one bar. Stop-loss is checked before take-profit.

        Checking the stop first is the pessimistic (conservative) assumption:
        if a single bar touches both levels we book the loss, never the win.
        """
        if low <= position.stop_price:
            return ExitDecision(
                True, EXIT_STOP_LOSS, position.stop_price,
                "stop-loss hit: low {:.8f} <= stop {:.8f}".format(low, position.stop_price),
            )
        if high >= position.tp_price:
            return ExitDecision(
                True, EXIT_TAKE_PROFIT, position.tp_price,
                "take-profit hit: high {:.8f} >= tp {:.8f} (net target {:.2f}%)".format(
                    high, position.tp_price, self.net_profit_target_pct
                ),
            )
        return None

    def strategy_exit(self, close: float, reason: str) -> ExitDecision:
        return ExitDecision(True, EXIT_STRATEGY, float(close), reason)

    # ---------------------------------------------------------------- reports
    def state_snapshot(self, now_ts: Optional[int] = None) -> Dict[str, Any]:
        return self.state.as_dict(now_ts)

    def limits_snapshot(self) -> Dict[str, Any]:
        return {
            "net_profit_target_pct": self.net_profit_target_pct,
            "gross_take_profit_pct": round(self.gross_tp_pct, 6),
            "cost_drag_pct": round(self.cost_drag_pct, 6),
            "stop_loss_pct": self.stop_loss_pct,
            "max_position_pct": self.max_position_pct,
            "max_open_positions": self.max_open_positions,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "cooldown_minutes": self.cooldown_minutes,
            "min_equity_usdt": self.min_equity_usdt,
            "max_trades_per_day": self.max_trades_per_day,
            "min_notional_usdt": self.min_notional_usdt,
        }

    def drain_audit(self) -> List[Dict[str, Any]]:
        """Return and clear the audit trail (the runner mirrors it into the ledger)."""
        items, self.audit = self.audit, []
        return items


__all__ = [
    "RiskManager", "RiskState", "EntryDecision", "ExitDecision",
    "EXIT_STOP_LOSS", "EXIT_TAKE_PROFIT", "EXIT_STRATEGY", "utc_day",
]
