"""Realistic sample events for ``notify preview``.

The goal is that an operator can see exactly what each notification will look
like on a phone **without waiting for a live trade**.  Numbers are realistic
(BTC ~71.7k, a 50 USDT account), and the historical block -- when the caller
passes one -- is the *real* measurement produced by
:class:`cryptobot.notify.context.HistoryStatsProvider`, never a fake.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import events as ev

#: The order ``notify preview`` prints event types in.
PREVIEW_ORDER: Sequence[str] = ev.EVENT_TYPES

# BTC/ETH sample levels (internally consistent: TP/stop/slippage all derived).
SAMPLE_ENTRY = 71679.5118
SAMPLE_QTY = 0.00062779
SAMPLE_STOP = 69887.5227
SAMPLE_TP = 73296.1227
SAMPLE_FEE = 0.1043
SAMPLE_TP_CLOSE = 73296.1227
SAMPLE_SL_CLOSE = 69887.5227


def _close_math(entry: float, exit_price: float, qty: float, fee: float) -> Dict[str, Any]:
    gross = (exit_price - entry) * qty
    net = gross - fee
    notional = entry * qty
    return {
        "gross_pnl": round(gross, 6),
        "net_pnl": round(net, 6),
        "net_pnl_pct": round(net / notional * 100.0, 4) if notional else 0.0,
    }


def sample_events(*, config: Any = None, history: Optional[Mapping[str, Any]] = None,
                  ts: int = 0) -> Dict[str, ev.NotificationEvent]:
    """Build one realistic event per type, keyed by event type."""
    pairs: Sequence[str] = tuple(getattr(config, "pairs", ()) or ("BTC/USDT", "ETH/USDT"))
    timeframe = str(getattr(config, "timeframe", "1h") or "1h")
    net_target = getattr(config, "net_profit_target_pct", 2.0)
    stop_loss = getattr(config, "stop_loss_pct", 2.5)
    initial = getattr(config, "initial_capital_usdt", 50.0)
    run_id = "paper-20260913T193456Z-4242"

    indicators = {
        "close": SAMPLE_ENTRY,
        "bb_upper": 72543.5118,
        "bb_middle": 72143.5118,
        "bb_lower": 71743.5118,
        "rsi": 31.0,
        "sma_trend": 69524.0,
    }
    strategy_params = {
        "bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_oversold": 35.0,
        "trend_sma_period": 200, "exit_at_middle_band": True,
    }

    tp_math = _close_math(SAMPLE_ENTRY, SAMPLE_TP_CLOSE, SAMPLE_QTY, SAMPLE_FEE)
    sl_math = _close_math(SAMPLE_ENTRY, SAMPLE_SL_CLOSE, SAMPLE_QTY, SAMPLE_FEE)
    strategy_exit_price = 71980.0
    strategy_math = _close_math(SAMPLE_ENTRY, strategy_exit_price, SAMPLE_QTY, SAMPLE_FEE)

    out: Dict[str, ev.NotificationEvent] = {}

    out["bot_started"] = ev.bot_started(
        run_id=run_id, mode="paper", pairs=pairs, timeframe=timeframe,
        initial_capital=initial, ts=ts)

    out["bot_stopped"] = ev.bot_stopped(
        run_id=run_id, final_equity=47.4787, net_pnl=-2.5213, net_pnl_pct=-5.0426,
        trades=18, win_rate_pct=50.0, mode="paper", ts=ts)

    out["position_opened"] = ev.position_opened(
        run_id=run_id, pair="BTC/USDT", entry_price=SAMPLE_ENTRY, qty=SAMPLE_QTY,
        notional=round(SAMPLE_ENTRY * SAMPLE_QTY, 4), stop_price=SAMPLE_STOP,
        tp_price=SAMPLE_TP, reason="entry:bollinger_dip+trend+oversold", ts=ts,
        mode="paper", timeframe=timeframe, net_target_pct=net_target,
        stop_loss_pct=stop_loss, indicators=indicators, strategy_params=strategy_params,
        volume_log_z=1.84, history=history)

    out["position_closed"] = ev.position_closed(
        run_id=run_id, pair="BTC/USDT", entry_price=SAMPLE_ENTRY, exit_price=strategy_exit_price,
        qty=SAMPLE_QTY, fees=SAMPLE_FEE, gross_pnl=strategy_math["gross_pnl"],
        net_pnl=strategy_math["net_pnl"], net_pnl_pct=strategy_math["net_pnl_pct"],
        trigger="strategy", ts=ts, exit_reason="strategy:exit:close_reached_middle_band",
        bars_held=4, held_seconds=4 * 3600.0, mode="paper", timeframe=timeframe)

    out["take_profit_hit"] = ev.take_profit_hit(
        run_id=run_id, pair="BTC/USDT", entry_price=SAMPLE_ENTRY, exit_price=SAMPLE_TP_CLOSE,
        net_pnl=tp_math["net_pnl"], net_pnl_pct=tp_math["net_pnl_pct"], ts=ts,
        qty=SAMPLE_QTY, fees=SAMPLE_FEE, gross_pnl=tp_math["gross_pnl"],
        exit_reason="take_profit:net target reached", bars_held=6, held_seconds=6 * 3600.0,
        mode="paper", timeframe=timeframe)

    out["stop_loss_hit"] = ev.stop_loss_hit(
        run_id=run_id, pair="BTC/USDT", entry_price=SAMPLE_ENTRY, exit_price=SAMPLE_SL_CLOSE,
        net_pnl=sl_math["net_pnl"], net_pnl_pct=sl_math["net_pnl_pct"], ts=ts,
        qty=SAMPLE_QTY, fees=SAMPLE_FEE, gross_pnl=sl_math["gross_pnl"],
        exit_reason="stop_loss:protective exit", bars_held=3, held_seconds=3 * 3600.0,
        mode="paper", timeframe=timeframe)

    out["risk_halted"] = ev.risk_halted(
        run_id=run_id, reason="gunluk zarar limiti asildi (%5,00)", day_realized_net_pnl=-2.6034,
        ts=ts)

    out["cooldown_started"] = ev.cooldown_started(
        run_id=run_id, pair="BTC/USDT", net_pnl=-0.3102, cooldown_minutes=60, ts=ts)

    out["feed_outage"] = ev.feed_outage(
        run_id=run_id, pair="BTC/USDT", error="FeedTimeout",
        detail="api.binance.com yanit vermedi (3 deneme)", ts=ts)

    out["data_fail_safe"] = ev.data_fail_safe(
        run_id=run_id, reason="eksik mum verisi: BTCUSDT (cache-stale)", ts=ts)

    out["daily_summary"] = ev.daily_summary(
        run_id=run_id, day="2026-09-12", equity=47.4787, trades=14, wins=7,
        win_rate_pct=50.0, net_pnl=-2.5213, ts=ts)

    out["equity_drop"] = ev.equity_drop(
        run_id=run_id, peak_equity=49.85, equity=47.4787, drop_pct=4.76, threshold_pct=3.0,
        ts=ts)

    out["test"] = ev.test_event(ts=ts)

    return out


def preview_events(only: Optional[str] = None, *, config: Any = None,
                   history: Optional[Mapping[str, Any]] = None,
                   ts: int = 0) -> List[ev.NotificationEvent]:
    """Ordered sample events: one (``only``) or all (``only=None``)."""
    events = sample_events(config=config, history=history, ts=ts)
    if only:
        key = str(only).strip().lower()
        return [events[key]] if key in events else []
    return [events[name] for name in PREVIEW_ORDER if name in events]


__all__ = ["PREVIEW_ORDER", "sample_events", "preview_events"]
