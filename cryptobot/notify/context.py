"""Honest measurements that may accompany a notification.

Two very different things live here, and both are deliberately labelled:

1. :func:`filter_margins` -- how far *this* signal cleared each strategy filter
   (RSI below its threshold, price below the lower Bollinger band in sigmas,
   price above the trend SMA in percent).  These are arithmetic measurements of
   the bar that triggered the entry.  They are **not** a probability of profit,
   and the rendered block says so.

2. :class:`HistoryStatsProvider` -- an *actual* backtest run over the locally
   cached candles, reduced to a few counts (trades, hit rate, median trade,
   share of trades closed by the take-profit / stop-loss trigger), together with
   the exact period, timeframe, pairs and the config hash.  If there is no usable
   cache the provider returns ``None`` and the caller **omits the whole block**
   instead of printing placeholders.

Nothing here sends anything or touches the network: the history run is an
offline replay of ``data/cache`` through the same engine the backtest uses.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from . import format as fmt

#: A block below this many trades is still shown (with its real count) but the
#: caller may choose to hide it; kept here so the policy is in one place.
MIN_TRADES_FOR_STATS = 1


# --------------------------------------------------------------------------- #
# 1. filter margins (measured on the triggering bar)
# --------------------------------------------------------------------------- #
def _get(mapping: Any, key: str) -> Optional[float]:
    if not isinstance(mapping, Mapping):
        return None
    return fmt.to_float(mapping.get(key))


def filter_margins(indicators: Any, params: Any) -> Optional[Dict[str, Any]]:
    """Arithmetic margins by which the entry cleared each filter.

    Returns ``None`` when even the regime cannot be determined (no close/trend);
    individual margin keys are omitted when their inputs are missing, so the
    caller never renders a made-up number.
    """
    close = _get(indicators, "close")
    trend = _get(indicators, "sma_trend")
    lower = _get(indicators, "bb_lower")
    upper = _get(indicators, "bb_upper")
    rsi_value = _get(indicators, "rsi")
    if close is None or trend is None:
        return None

    params = params if isinstance(params, Mapping) else {}
    rsi_threshold = fmt.to_float(params.get("rsi_oversold"))
    bb_std = fmt.to_float(params.get("bb_std"))
    sma_period = params.get("trend_sma_period")

    out: Dict[str, Any] = {
        "regime": "BULL" if close > trend else "BEAR",
        "sma_period": int(sma_period) if isinstance(sma_period, (int, float)) else None,
        "rsi_threshold": rsi_threshold,
        "close": close,
        "sma_trend": trend,
    }
    if trend > 0:
        out["trend_pct_above"] = (close - trend) / trend * 100.0
    if rsi_value is not None and rsi_threshold is not None:
        out["rsi_below"] = rsi_threshold - rsi_value
        out["rsi"] = rsi_value
    if lower is not None and bb_std and bb_std > 0 and upper is not None and upper > lower:
        sigma = (upper - lower) / (2.0 * bb_std)
        if sigma > 0:
            out["band_sigma_below"] = (lower - close) / sigma
    return out


def margin_summary(margins: Optional[Mapping[str, Any]]) -> str:
    """``RSI 4,0 altı · bant 0,32σ altı · trend 3,1% üstü`` (available parts only)."""
    if not margins:
        return ""
    parts = []
    rsi_below = fmt.to_float(margins.get("rsi_below"))
    if rsi_below is not None:
        parts.append("RSI {} {}".format(fmt.number(abs(rsi_below), 1),
                                        "altı" if rsi_below >= 0 else "üstü"))
    band = fmt.to_float(margins.get("band_sigma_below"))
    if band is not None:
        parts.append("bant {}σ {}".format(fmt.number(abs(band), 2),
                                          "altı" if band >= 0 else "üstü"))
    trend = fmt.to_float(margins.get("trend_pct_above"))
    if trend is not None:
        parts.append("trend {}% {}".format(fmt.number(abs(trend), 1),
                                           "üstü" if trend >= 0 else "altı"))
    return " · ".join(parts)


def regime_line(margins: Optional[Mapping[str, Any]]) -> str:
    """``BULL (fiyat > SMA200)`` or ``""`` when the regime is unknown."""
    if not margins:
        return ""
    regime = str(margins.get("regime") or "").strip()
    if not regime:
        return ""
    period = margins.get("sma_period")
    comparison = "fiyat > SMA{}".format(period) if regime == "BULL" else "fiyat < SMA{}".format(period)
    if period is None:
        comparison = "fiyat > SMA" if regime == "BULL" else "fiyat < SMA"
    return "{} ({})".format(regime, comparison)


# --------------------------------------------------------------------------- #
# 2. historical measurement (a real offline backtest over the cache)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HistoryStats:
    """Result of one real replay of the cached candles through the engine."""

    pairs: Tuple[str, ...]
    timeframe: str
    bars: int
    start_ts: Optional[int]
    end_ts: Optional[int]
    trades: int
    win_rate_pct: Optional[float]
    median_net_pnl_pct: Optional[float]
    tp_exit_pct: Optional[float]
    sl_exit_pct: Optional[float]
    net_target_pct: Optional[float]
    stop_loss_pct: Optional[float]
    config_hash: str
    source: str = "cache"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pairs": list(self.pairs),
            "timeframe": self.timeframe,
            "bars": int(self.bars),
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "days": self.days(),
            "trades": int(self.trades),
            "win_rate_pct": self.win_rate_pct,
            "median_net_pnl_pct": self.median_net_pnl_pct,
            "tp_exit_pct": self.tp_exit_pct,
            "sl_exit_pct": self.sl_exit_pct,
            "net_target_pct": self.net_target_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "config_hash": self.config_hash,
            "source": self.source,
        }

    def days(self) -> Optional[float]:
        if not self.start_ts or not self.end_ts or self.end_ts <= self.start_ts:
            return None
        return round((self.end_ts - self.start_ts) / 86_400_000.0, 1)


def _trigger_share(trades, prefix: str) -> Optional[float]:
    if not trades:
        return None
    hits = sum(1 for trade in trades if str(trade.get("exit_reason") or "").startswith(prefix))
    return round(hits / len(trades) * 100.0, 1)


def compute_history_stats(config: Any, *, engine_factory: Any = None) -> Optional[HistoryStats]:
    """Replay the cached candles offline and reduce the result to a stats block.

    ``None`` -- never a placeholder -- when the cache is missing/unusable or the
    replay cannot run.  Imports the engine lazily so the notification data model
    stays importable without the whole backtest stack.
    """
    from ..data.feed import FeedError, load_candles

    frames: Dict[str, Any] = {}
    try:
        for pair in config.pairs:
            loaded = load_candles(config, pair, allow_network=False)
            frames[pair] = loaded.frame
    except (FeedError, OSError, ValueError, KeyError):
        return None
    if not frames or any(getattr(frame, "empty", True) for frame in frames.values()):
        return None

    try:
        from ..backtest.engine import BacktestEngine
        from ..backtest.metrics import metrics_hash

        engine = engine_factory(config) if engine_factory is not None else BacktestEngine(config)
        result = engine.run(frames, data_meta={pair: {"source": "cache", "complete": True,
                                                     "rows": len(frame)}
                                              for pair, frame in frames.items()})
    except Exception:  # a stats block must never break the caller
        return None

    metrics = result.metrics or {}
    trades = result.trades or []
    net_pcts = [fmt.to_float(trade.get("net_pnl_pct")) for trade in trades]
    net_pcts = [value for value in net_pcts if value is not None]
    config_hash = hashlib.sha256(
        json.dumps(result.config_echo, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:12]
    trades_count = int(metrics.get("trade_count") or len(trades))
    if trades_count < MIN_TRADES_FOR_STATS and trades_count == 0 and not metrics.get("bars"):
        # Nothing measurable at all -> omit rather than print an empty shell.
        return None
    return HistoryStats(
        pairs=tuple(config.pairs),
        timeframe=str(config.timeframe),
        bars=int(result.bars_processed),
        start_ts=metrics.get("data_start_ts"),
        end_ts=metrics.get("data_end_ts"),
        trades=trades_count,
        win_rate_pct=fmt.to_float(metrics.get("win_rate_pct")),
        median_net_pnl_pct=round(statistics.median(net_pcts), 4) if net_pcts else None,
        tp_exit_pct=_trigger_share(trades, "take_profit"),
        sl_exit_pct=_trigger_share(trades, "stop_loss"),
        net_target_pct=fmt.to_float(getattr(config, "net_profit_target_pct", None)),
        stop_loss_pct=fmt.to_float(getattr(config, "stop_loss_pct", None)),
        config_hash=config_hash,
    )


class HistoryStatsProvider:
    """Memoised :func:`compute_history_stats` -- compute at most once per process."""

    def __init__(self, config: Any, *, engine_factory: Any = None) -> None:
        self.config = config
        self._engine_factory = engine_factory
        self._done = False
        self._value: Optional[HistoryStats] = None

    def get(self) -> Optional[HistoryStats]:
        if not self._done:
            self._done = True
            try:
                self._value = compute_history_stats(self.config, engine_factory=self._engine_factory)
            except Exception:  # pragma: no cover - defensive
                self._value = None
        return self._value

    def as_dict(self) -> Optional[Dict[str, Any]]:
        value = self.get()
        return value.as_dict() if value is not None else None


__all__ = [
    "MIN_TRADES_FOR_STATS", "filter_margins", "margin_summary", "regime_line",
    "HistoryStats", "HistoryStatsProvider", "compute_history_stats",
]
