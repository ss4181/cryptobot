"""Deterministic, event-driven backtest engine.

Model (no look-ahead, fully reproducible -- no randomness anywhere):

1. The strategy sees **only closed bars**; ``prepare()`` is vectorised per pair.
2. For every timestamp in the union of all pairs' bars, in ``config.pairs`` order:
   * protective exits are evaluated against that bar's ``high``/``low``
     (stop-loss before take-profit -- the pessimistic assumption),
   * then the strategy's own exit is evaluated on the bar close,
   * then entries are sized by the risk manager and filled **at that bar's
     close** with fee + slippage applied by the paper broker,
   * then an equity snapshot is recorded.
3. An entry made on bar *t* can only be exited from bar *t+1* onward.
4. A position still open at the end is left open and marked at the last price;
   it is visible in ``final_snapshot`` and in the equity curve.

Fees and slippage are always applied -- there is no "frictionless" mode.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd

from ..config import Config
from ..data.feed import interval_to_ms
from ..execution.paper_broker import PARTIAL, FaultInjector, OrderResult, PaperBroker
from ..risk.manager import EXIT_STOP_LOSS, EXIT_TAKE_PROFIT, RiskManager
from ..strategy.base import Strategy, get_strategy
from ..strategy.indicators import log_zscore
from .metrics import compute_metrics, metrics_hash

log = logging.getLogger(__name__)
TRADING_OK = "trading"
TRADING_FAIL_SAFE = "fail_safe_data_incomplete"


def volume_log_z(volume: Any, index: int, period: int = 100) -> Optional[float]:
    """Z-score of the entry bar's log volume against the prior ``period`` bars.

    ``None`` when it cannot be measured (short series, flat volume) so the
    notification omits the line instead of printing a fake number.
    """
    try:
        index = int(index)
        start = max(0, index - int(period))
        window = volume[start: index + 1]
        if window is None or len(window) < 3:
            return None
        scores = log_zscore(window, period=len(window) - 1)
        value = float(scores[-1])
        return value if math.isfinite(value) else None
    except Exception:
        return None


@dataclass
class BacktestResult:
    """Everything a caller (CLI, report writer, test) needs."""

    run_id: str
    mode: str
    metrics: Dict[str, Any]
    trades: List[Dict[str, Any]]
    equity_curve: List[Dict[str, Any]]
    events: List[Dict[str, Any]]
    final_snapshot: Dict[str, Any]
    config_echo: Dict[str, Any]
    data_echo: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    bars_processed: int = 0

    def metrics_payload(self, *, extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """The deterministic payload whose hash proves reproducibility.

        Contains **no** timestamps, paths or run ids of the current moment.
        """
        payload: Dict[str, Any] = {
            "metrics": self.metrics,
            "config": self.config_echo,
            "data": self.data_echo,
        }
        if extra:
            payload.update(dict(extra))
        payload["determinism_hash"] = metrics_hash(payload)
        return payload

    @property
    def determinism_hash(self) -> str:
        return self.metrics_payload()["determinism_hash"]


class BacktestEngine:
    """Replays cached candles through strategy -> risk -> broker -> ledger."""

    def __init__(
        self,
        config: Config,
        *,
        strategy: Optional[Strategy] = None,
        broker: Optional[PaperBroker] = None,
        risk: Optional[RiskManager] = None,
        ledger: Any = None,
        run_id: Optional[str] = None,
        faults: Optional[FaultInjector] = None,
        event_sink: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.strategy = strategy or get_strategy(config.strategy.name, config.strategy.params)
        self.broker = broker or PaperBroker(
            config.initial_capital_usdt,
            config.fee_pct,
            config.slippage_pct,
            max_open_positions=config.max_open_positions,
            faults=faults,
        )
        self.risk = risk or RiskManager(
            initial_equity=config.initial_capital_usdt,
            fee_pct=config.fee_pct,
            slippage_pct=config.slippage_pct,
            net_profit_target_pct=config.net_profit_target_pct,
            stop_loss_pct=config.stop_loss_pct,
            max_position_pct=config.max_position_pct,
            max_open_positions=config.max_open_positions,
            daily_loss_limit_pct=config.daily_loss_limit_pct,
            cooldown_minutes=config.cooldown_minutes,
            min_equity_usdt=config.min_equity_usdt,
            max_trades_per_day=config.max_trades_per_day,
            gross_take_profit_pct=config.gross_take_profit_pct,
        )
        self.ledger = ledger
        self.mode = "backtest"
        self.run_id = run_id or "bt-{}-{}".format(config.pair_slug(), config.timeframe)
        #: Optional notification hook. ``None`` (the default, and what the
        #: backtest CLI uses) keeps backtests completely silent. The paper runner
        #: installs one; the engine only ever hands it small primitive dicts, and
        #: ``_notify`` swallows any error so a notifier can never break trading.
        self.event_sink = event_sink
        self.warnings: List[str] = []
        self.events: List[Dict[str, Any]] = []
        self.trading_state = TRADING_OK
        self.bars_processed = 0
        self._entry_bars: Dict[str, int] = {}
        self._prepared: Dict[str, pd.DataFrame] = {}
        self._data_meta: Dict[str, Any] = {}
        self._incomplete: set = set()
        self._first_ts: Optional[int] = None
        self._last_ts: Optional[int] = None

    # ------------------------------------------------------------------ events
    def _event(self, ts: int, code: str, message: str, **payload: Any) -> None:
        record = {"ts": int(ts), "code": code, "message": message}
        record.update(payload)
        self.events.append(record)
        level = "warning" if "fail" in code or "limit" in code or "halt" in code else "info"
        log.log(getattr(logging, level.upper(), logging.INFO), "engine.event",
                extra={"event": "engine_event", "code": code, "detail": message, **payload})

    def _notify(self, **payload: Any) -> None:
        """Hand a lifecycle payload to the optional notification sink.

        Failure isolation: the sink is third-party-ish code (a notifier), so any
        exception it raises is swallowed here.  Notification can never alter a
        trading decision or stop the loop.
        """
        sink = self.event_sink
        if sink is None:
            return
        try:
            sink(dict(payload))
        except Exception as exc:  # pragma: no cover - defensive by design
            log.warning("engine.notify_sink_failed",
                        extra={"event": "notify_sink_failed", "error": type(exc).__name__})

    def _fail_safe(self, ts: int, reason: str) -> None:
        if self.trading_state == TRADING_FAIL_SAFE:
            return
        self.trading_state = TRADING_FAIL_SAFE
        self.warnings.append(reason)
        log.critical("engine.fail_safe", extra={"event": "engine_fail_safe", "detail": reason})
        self._event(ts, "pause_new_entries", reason, state=self.trading_state)
        self._notify(type="data_fail_safe", ts=int(ts), reason=reason)
        if self.ledger is not None:
            # Persist the pause so a data outage is auditable from the ledger alone
            # (the in-memory ``events`` list and the log are not enough).
            self.ledger.record_event(
                int(ts), level="CRITICAL", category="engine", code="pause_new_entries",
                message=reason, payload={"state": self.trading_state},
            )

    # --------------------------------------------------------------- position
    def _close_position(self, pair: str, reference_price: float, ts: int, trigger: str, reason: str) -> None:
        result: OrderResult = self.broker.sell(pair, reference_price, ts, reason="{}:{}".format(trigger, reason))
        if not result.ok:
            log.error("engine.close_failed", extra={"event": "close_failed", "pair": pair,
                                                    "reason": result.reason, "trigger": trigger})
            self._event(ts, "close_rejected", "sell rejected for {}: {}".format(pair, result.reason), pair=pair)
            if self.ledger is not None:
                self.ledger.record_order(result, mode=self.mode)
            return

        # Annotate the broker's own record (it is the canonical trade object used
        # by the metrics) with how long the position was held.
        trade = self.broker.closed_trades[-1]
        trade["bars_held"] = max(0, self.bars_processed - self._entry_bars.pop(pair, self.bars_processed))
        trade["trading_state"] = self.trading_state

        if self.ledger is not None:
            self.ledger.record_close(result, trade, mode=self.mode)
            if result.status == PARTIAL:
                # A partially closed position stays observable in the orders table.
                self.ledger.record_order(result, mode=self.mode)
        bars_held = trade.get("bars_held")
        held_seconds = None
        try:
            if bars_held is not None:
                held_seconds = float(bars_held) * interval_to_ms(self.config.timeframe) / 1000.0
        except (TypeError, ValueError):  # pragma: no cover - defensive
            held_seconds = None
        close_payload = {
            "pair": pair, "trigger": trigger, "entry_price": trade.get("entry_fill_price"),
            "exit_price": trade.get("exit_fill_price"), "qty": trade.get("qty"),
            "fees": trade.get("fees"), "gross_pnl": trade.get("gross_pnl"),
            "net_pnl": trade.get("net_pnl"), "net_pnl_pct": trade.get("net_pnl_pct"),
            "exit_reason": trade.get("exit_reason"), "bars_held": bars_held,
            "held_seconds": held_seconds, "timeframe": self.config.timeframe, "mode": self.mode,
        }
        self._notify(type="position_closed", ts=int(ts), **close_payload)
        if trigger == EXIT_TAKE_PROFIT:
            self._notify(type="take_profit_hit", ts=int(ts), **close_payload)
        elif trigger == EXIT_STOP_LOSS:
            self._notify(type="stop_loss_hit", ts=int(ts), **close_payload)
        self.risk.register_trade_result(result.net_pnl, ts, pair=pair)
        self._flush_risk_audit(ts)

    def _flush_risk_audit(self, ts: int) -> None:
        if self.ledger is None:
            return
        for item in self.risk.drain_audit():
            code = str(item.get("code", ""))
            self.ledger.record_event(
                int(item.get("ts", ts)),
                level="WARNING" if any(k in code for k in ("limit", "cooldown", "blocked")) else "INFO",
                category="risk", code=code, message=str(item.get("reason", "")),
                payload={k: v for k, v in item.items() if k not in {"ts", "code", "reason"}},
            )
            self._notify_risk_audit(item, ts)

    def _notify_risk_audit(self, item: Mapping[str, Any], ts: int) -> None:
        """Mirror the two risk events worth a push onto the notification sink."""
        code = str(item.get("code", ""))
        if code == "cooldown_started":
            self._notify(type="cooldown_started", ts=int(item.get("ts", ts)),
                         pair=str(item.get("pair", "")), net_pnl=item.get("net_pnl"),
                         cooldown_minutes=self.risk.cooldown_minutes)
        elif code == "daily_loss_limit_reached":
            self._notify(type="risk_halted", ts=int(item.get("ts", ts)),
                         reason=str(item.get("reason", "")),
                         day_realized_net_pnl=item.get("day_realized_net_pnl"))

    # -------------------------------------------------------------------- run
    def process(
        self,
        frames: Mapping[str, pd.DataFrame],
        *,
        data_meta: Optional[Mapping[str, Any]] = None,
        since_ts: Optional[Any] = None,
    ) -> int:
        """Replay bars newer than ``since_ts`` (pair -> OHLCV DataFrame, oldest first).

        ``since_ts`` is either a single timestamp or a ``{pair: ts}`` mapping (the
        paper runner uses the mapping so a lagging pair cannot cause bars to be
        skipped).  Returns the number of bars processed in this call.  State
        (broker, risk, open positions) is preserved between calls, so the paper
        runner can feed a growing window once per cycle without re-deciding old bars.
        """
        if not frames:
            raise ValueError("no candle frames supplied")

        prepared: Dict[str, pd.DataFrame] = {}
        series: Dict[str, Dict[str, Any]] = {}
        lookups: Dict[str, Dict[int, int]] = {}
        timeline: List[int] = []
        for pair in self.config.pairs:
            if pair not in frames:
                raise KeyError("missing candles for {}".format(pair))
            frame = frames[pair]
            if frame.empty:
                raise ValueError("empty candle frame for {}".format(pair))
            prepared[pair] = self.strategy.prepare(frame)
            # Numpy view of every column: the per-bar loop never touches pandas.
            series[pair] = self.strategy.series_from_frame(prepared[pair])
            lookups[pair] = {int(ts): idx for idx, ts in enumerate(prepared[pair]["ts"].to_numpy())}
            cutoff = self._cutoff_for(pair, since_ts)
            timeline.extend(int(ts) for ts in prepared[pair]["ts"].to_numpy() if cutoff is None or ts > cutoff)
        timeline = sorted(set(timeline))
        if not timeline:
            return 0
        if self._first_ts is None:
            self._first_ts = timeline[0]
        self._last_ts = timeline[-1]

        data_meta = data_meta or {}
        for pair in self.config.pairs:
            if not self._data_complete(data_meta.get(pair)):
                self._incomplete.add(pair)
        if self._incomplete:
            self._fail_safe(timeline[0], "incomplete candle data for {}: new entries paused".format(
                ", ".join(sorted(self._incomplete))))
        warmup = self.strategy.warmup_bars()
        processed = 0

        for ts in timeline:
            bars = {pair: lookups[pair][ts] for pair in self.config.pairs if ts in lookups[pair]}

            # --- A. mark prices + protective exits on this bar -----------------
            for pair in self.config.pairs:
                if pair not in bars:
                    continue
                index = bars[pair]
                values = series[pair]
                close = float(values["close"][index])
                self.broker.mark_price(pair, close)
                position = self.broker.positions.get(pair)
                if position is None or position.entry_ts >= ts:
                    continue
                decision = self.risk.check_exit(position, high=float(values["high"][index]),
                                                low=float(values["low"][index]), close=close)
                if decision is not None:
                    self._close_position(pair, decision.reference_price, ts, decision.trigger, decision.reason)

            # --- B. strategy exit on the closed bar ---------------------------
            for pair in self.config.pairs:
                if pair not in bars:
                    continue
                position = self.broker.positions.get(pair)
                if position is None or position.entry_ts >= ts:
                    continue
                signal = self.strategy.decide(bars[pair], series[pair], has_position=True)
                if signal.is_exit:
                    self._close_position(pair, float(signal.price), ts, "strategy", signal.reason)

            # --- C. day roll / equity ----------------------------------------
            equity = self.broker.equity()
            self.risk.note_equity(ts, equity)

            # --- D. entries ---------------------------------------------------
            if self.trading_state == TRADING_OK:
                for pair in self.config.pairs:
                    if pair not in bars or self.broker.has_position(pair):
                        continue
                    index = bars[pair]
                    if index < warmup:
                        continue
                    signal = self.strategy.decide(index, series[pair], has_position=False)
                    if not signal.is_entry:
                        continue
                    decision = self.risk.evaluate_entry(
                        ts=ts, price=float(signal.price), equity=self.broker.equity(),
                        cash=self.broker.cash, open_positions=self.broker.open_positions,
                    )
                    if not decision.approved:
                        self._flush_risk_audit(ts)
                        continue
                    result = self.broker.buy(
                        pair, float(signal.price), decision.qty, ts,
                        stop_price=decision.stop_price, tp_price=decision.tp_price, reason=signal.reason,
                    )
                    if result.ok:
                        self.risk.note_entry_taken(ts, pair)
                        self._entry_bars[pair] = self.bars_processed
                        self._notify(type="position_opened", ts=int(ts), pair=pair,
                                     entry_price=result.fill_price, qty=result.filled_qty,
                                     notional=result.notional, stop_price=decision.stop_price,
                                     tp_price=decision.tp_price, reason=signal.reason,
                                     indicators=dict(signal.indicators),
                                     strategy_params=dict(getattr(self.strategy, "params", {}) or {}),
                                     volume_log_z=volume_log_z(values.get("volume"), index),
                                     net_target_pct=self.config.net_profit_target_pct,
                                     stop_loss_pct=self.config.stop_loss_pct,
                                     timeframe=self.config.timeframe, mode=self.mode)
                        if self.ledger is not None:
                            self.ledger.record_open(result, mode=self.mode)
                            if result.status == PARTIAL:
                                # Keep a partially filled entry auditable as an order too.
                                self.ledger.record_order(result, mode=self.mode)
                    elif self.ledger is not None:
                        self.ledger.record_order(result, mode=self.mode)
                    self._flush_risk_audit(ts)

            # --- E. equity snapshot ------------------------------------------
            point = self.broker.record_equity(ts)
            if self.ledger is not None:
                self.ledger.record_equity(point)

            self.bars_processed += 1
            processed += 1

        self._prepared = prepared
        self._data_meta = dict(data_meta)
        self._flush_risk_audit(int(timeline[-1]))
        return processed

    def finalize(self) -> BacktestResult:
        """Build the result object (metrics, trades, equity curve, echoes)."""
        equity_series = [point.equity for point in self.broker.equity_history]
        trades = [dict(t) for t in self.broker.closed_trades]
        metrics = compute_metrics(
            trades,
            equity_series,
            initial_capital=self.config.initial_capital_usdt,
            bars_per_year=self.config.bars_per_year,
            timeframe=self.config.timeframe,
            pairs=list(self.config.pairs),
            bars=self.bars_processed,
            data_start_ts=self._first_ts,
            data_end_ts=self._last_ts,
        )
        return BacktestResult(
            run_id=self.run_id,
            mode=self.mode,
            metrics=metrics,
            trades=trades,
            equity_curve=[point.as_dict() for point in self.broker.equity_history],
            events=self.events,
            final_snapshot=self.broker.snapshot(),
            config_echo=self.config.as_dict(),
            data_echo=self._data_echo(self._prepared, self._data_meta),
            warnings=list(self.warnings),
            bars_processed=self.bars_processed,
        )

    def run(
        self,
        frames: Mapping[str, pd.DataFrame],
        *,
        data_meta: Optional[Mapping[str, Any]] = None,
    ) -> BacktestResult:
        """Replay ``frames`` end to end: :meth:`process` + :meth:`finalize`."""
        self.process(frames, data_meta=data_meta)
        result = self.finalize()
        if self.ledger is not None:
            self.ledger.record_event(
                self._last_ts or 0, level="INFO", category="engine", code="backtest_complete",
                message="processed {} bars, {} closed trades, net {:.4f} USDT".format(
                    result.bars_processed, len(result.trades), result.metrics["net_pnl_usdt"] or 0.0),
                payload={"final_equity": result.metrics["final_equity_usdt"],
                         "trading_state": self.trading_state},
            )
        return result

    # ------------------------------------------------------------------ meta
    @staticmethod
    def _cutoff_for(pair: str, since_ts: Optional[Any]) -> Optional[int]:
        if since_ts is None:
            return None
        if isinstance(since_ts, Mapping):
            value = since_ts.get(pair)
            return None if value is None else int(value)
        return int(since_ts)

    @staticmethod
    def _data_complete(meta: Optional[Mapping[str, Any]]) -> bool:
        if not meta:
            return True  # no metadata supplied -> assume usable
        return bool(meta.get("complete", True))

    def _data_echo(self, prepared: Mapping[str, pd.DataFrame], data_meta: Mapping[str, Any]) -> Dict[str, Any]:
        per_pair: Dict[str, Any] = {}
        for pair in self.config.pairs:
            frame = prepared.get(pair)
            meta = dict(data_meta.get(pair) or {})
            per_pair[pair] = {
                "rows": 0 if frame is None else int(len(frame)),
                "first_ts": None if frame is None or frame.empty else int(frame["ts"].iloc[0]),
                "last_ts": None if frame is None or frame.empty else int(frame["ts"].iloc[-1]),
                "source": meta.get("source", "unknown"),
            }
        return {
            "timeframe": self.config.timeframe,
            "pairs": per_pair,
            "interval_ms": interval_to_ms(self.config.timeframe),
        }


__all__ = ["BacktestEngine", "BacktestResult", "TRADING_OK", "TRADING_FAIL_SAFE"]
