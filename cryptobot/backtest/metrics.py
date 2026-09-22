"""Backtest performance metrics -- pure functions over trades and an equity curve.

Everything returned here is derived only from the trade list and the equity
series.  No timestamps of "now" and no paths are included, which is what makes
two runs over identical data + config produce byte-identical JSON
(requirement 8).  Volatile run metadata lives in ``report.py`` / the ``.md``.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

__all__ = [
    "compute_metrics",
    "max_drawdown",
    "sharpe_ratio",
    "profit_factor",
    "metrics_hash",
    "canonical_json",
]

_ROUND = 10


def _r(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, _ROUND)


def canonical_json(payload: Any) -> str:
    """Canonical JSON: sorted keys, fixed separators, no NaN/Infinity."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def metrics_hash(payload: Mapping[str, Any]) -> str:
    """Stable SHA-256 over the deterministic payload (used to prove reproducibility)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def profit_factor(net_pnls: Sequence[float]) -> Optional[float]:
    """Gross profit / gross loss. ``None`` when there are no losing trades."""
    gains = sum(p for p in net_pnls if p > 0)
    losses = -sum(p for p in net_pnls if p < 0)
    if losses == 0:
        return None if gains == 0 else float("inf")
    return gains / losses


def sharpe_ratio(returns: Sequence[float], bars_per_year: float, ddof: int = 1) -> Optional[float]:
    """Annualised Sharpe of per-bar equity returns (risk-free rate = 0).

    Returns ``None`` when there is not enough data or the return series has zero
    variance -- better an explicit null than a fake number.
    """
    array = np.asarray(list(returns), dtype=float)
    array = array[np.isfinite(array)]
    if array.size < 3:
        return None
    std = float(np.std(array, ddof=ddof))
    # Constant returns can leave a denormal ~1e-18 instead of a clean zero.
    if std <= 1e-12:
        return None
    mean = float(np.mean(array))
    value = mean / std * math.sqrt(float(bars_per_year))
    return value if math.isfinite(value) else None


def max_drawdown(equity: Sequence[float]) -> Dict[str, Any]:
    """Maximum drawdown of an equity series.

    ``pct`` and ``usdt`` are reported as **positive magnitudes** (a 3.5 means the
    equity fell 3.5 % from its running peak).
    """
    if not equity:
        return {"pct": 0.0, "usdt": 0.0, "peak": None, "trough": None, "peak_index": None, "trough_index": None}
    series = np.asarray(list(equity), dtype=float)
    running_peak = np.maximum.accumulate(series)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = np.where(running_peak > 0, (series - running_peak) / running_peak, 0.0)
    trough_index = int(np.argmin(drawdown))
    worst = float(drawdown[trough_index])
    if worst >= 0:
        return {"pct": 0.0, "usdt": 0.0, "peak": _r(series[0]), "trough": _r(series[trough_index]),
                "peak_index": 0, "trough_index": trough_index}
    peak_index = int(np.argmax(series[: trough_index + 1])) if trough_index > 0 else 0
    return {
        "pct": _r(abs(worst) * 100.0),
        "usdt": _r(abs(series[trough_index] - series[peak_index])),
        "peak": _r(series[peak_index]),
        "trough": _r(series[trough_index]),
        "peak_index": peak_index,
        "trough_index": trough_index,
    }


def compute_metrics(
    trades: Sequence[Mapping[str, Any]],
    equity_curve: Sequence[float],
    *,
    initial_capital: float,
    bars_per_year: float,
    timeframe: str,
    pairs: Sequence[str],
    bars: int,
    data_start_ts: Optional[int] = None,
    data_end_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the deterministic metrics block.

    ``trades`` are closed round trips with at least ``net_pnl``.
    """
    net_pnls = [float(t.get("net_pnl", 0.0)) for t in trades]
    gross_pnls = [float(t.get("gross_pnl", 0.0)) for t in trades]
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    flats = [p for p in net_pnls if p == 0]
    count = len(net_pnls)

    equity = list(equity_curve) or [float(initial_capital)]
    final_equity = float(equity[-1])
    net_pnl = final_equity - float(initial_capital)

    returns: List[float] = []
    for previous, current in zip(equity[:-1], equity[1:]):
        if previous:
            returns.append((current - previous) / previous)

    drawdown = max_drawdown(equity)
    sharpe = sharpe_ratio(returns, bars_per_year)

    total_fees = sum(float(t.get("fees", 0.0)) for t in trades)
    total_slippage = sum(float(t.get("slippage_cost", 0.0)) for t in trades)

    metrics: Dict[str, Any] = {
        # --- context (deterministic echo of what was measured) ---
        "pairs": list(pairs),
        "timeframe": timeframe,
        "bars": int(bars),
        "data_start_ts": int(data_start_ts) if data_start_ts is not None else None,
        "data_end_ts": int(data_end_ts) if data_end_ts is not None else None,
        "initial_capital_usdt": _r(initial_capital),
        # --- trades ---
        "trade_count": count,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": len(flats),
        "win_rate_pct": _r(len(wins) / count * 100.0) if count else 0.0,
        # --- profit ---
        "gross_pnl_usdt": _r(sum(gross_pnls)),
        "net_pnl_usdt": _r(net_pnl),
        "net_pnl_pct": _r(net_pnl / float(initial_capital) * 100.0) if initial_capital else None,
        "final_equity_usdt": _r(final_equity),
        "equity_multiple": _r(final_equity / float(initial_capital)) if initial_capital else None,
        # --- costs ---
        "total_fees_usdt": _r(total_fees),
        "total_slippage_usdt": _r(total_slippage),
        "total_cost_drag_usdt": _r(total_fees + total_slippage),
        # --- risk ---
        "max_drawdown_pct": drawdown["pct"],
        "max_drawdown_usdt": drawdown["usdt"],
        "profit_factor": _r(profit_factor(net_pnls)),
        # --- per trade ---
        "avg_net_pnl_per_trade_usdt": _r(sum(net_pnls) / count) if count else 0.0,
        "avg_win_usdt": _r(sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss_usdt": _r(sum(losses) / len(losses)) if losses else 0.0,
        "best_trade_usdt": _r(max(net_pnls)) if count else 0.0,
        "worst_trade_usdt": _r(min(net_pnls)) if count else 0.0,
        "avg_net_pnl_pct_per_trade": _r(sum(float(t.get("net_pnl_pct", 0.0)) for t in trades) / count) if count else 0.0,
        "avg_bars_in_trade": _r(
            sum(float(t.get("bars_held", 0.0)) for t in trades) / count
        ) if count else 0.0,
        # --- risk-adjusted ---
        "sharpe_ratio": _r(sharpe),
        "sharpe_bars_per_year": _r(bars_per_year),
    }
    return metrics


__all__ += ["compute_metrics", "max_drawdown", "sharpe_ratio", "profit_factor", "metrics_hash", "canonical_json"]
