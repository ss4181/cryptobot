"""Daily paper-trading summary written to ``cryptobot/reports/daily_<YYYY-MM-DD>.md``."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..config import Config

log = logging.getLogger(__name__)


def _fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return "{:.{}f}".format(value, digits)
    return str(value)


def _rows_for_day(rows: Sequence[Mapping[str, Any]], day: str) -> List[Mapping[str, Any]]:
    return [row for row in rows if str(row.get("iso_utc", "")).startswith(day)]


def build_daily_report(
    *,
    day: str,
    config: Config | None,
    broker_snapshot: Mapping[str, Any],
    trades: Sequence[Mapping[str, Any]],
    equity_points: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    reconciliation: Optional[Mapping[str, Any]] = None,
    risk_state: Optional[Mapping[str, Any]] = None,
    run_id: str = "",
) -> str:
    """Render the Markdown daily summary (Turkish, user-facing)."""
    day_trades = _rows_for_day(trades, day)
    day_events = _rows_for_day(events, day)
    day_orders = _rows_for_day(orders, day)
    day_equity = _rows_for_day(equity_points, day)

    net = sum(float(t.get("net_pnl") or 0.0) for t in day_trades)
    wins = sum(1 for t in day_trades if float(t.get("net_pnl") or 0.0) > 0)
    fees = sum(float(t.get("fees") or 0.0) for t in day_trades)
    start_equity = float(day_equity[0]["equity"]) if day_equity else float(broker_snapshot.get("equity") or 0.0)
    end_equity = float(day_equity[-1]["equity"]) if day_equity else float(broker_snapshot.get("equity") or 0.0)

    lines: List[str] = []
    lines.append("# Gunluk Rapor -- {} (paper trading)".format(day))
    lines.append("")
    lines.append("> Simulasyon ozetidir. Gercek emir yok, API anahtari yok. Yatirim tavsiyesi degildir.")
    lines.append("")
    lines.append("- Run id: `{}`".format(run_id))
    lines.append("- Olusturma (UTC): `{}`".format(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")))
    if config is not None:
        lines.append("- Mod: `{}` | Pariteler: {} | Timeframe: `{}`".format(
            config.mode, ", ".join(config.pairs), config.timeframe))
        lines.append("- Hedef: net %{:.2f} (komisyon %{:.3f}/bacak, slippage %{:.3f}/bacak)".format(
            config.net_profit_target_pct, config.fee_pct, config.slippage_pct))
    lines.append("")

    lines.append("## Ozet")
    lines.append("")
    lines.append("| Alan | Deger |")
    lines.append("| --- | --- |")
    lines.append("| Gun basi equity | {} |".format(_fmt(start_equity, 4)))
    lines.append("| Gun sonu equity | {} |".format(_fmt(end_equity, 4)))
    lines.append("| Gun net PnL (kapanan islemler) | {} |".format(_fmt(net, 4)))
    lines.append("| Kapanan islem | {} |".format(len(day_trades)))
    lines.append("| Kazanan islem | {} |".format(wins))
    lines.append("| Kazanma orani | {} |".format(
        "{:.2f}%".format(wins / len(day_trades) * 100) if day_trades else "n/a"))
    lines.append("| Odenen komisyon | {} |".format(_fmt(fees, 4)))
    lines.append("| Acik pozisyon | {} |".format(broker_snapshot.get("open_positions")))
    lines.append("| Nakit | {} |".format(_fmt(broker_snapshot.get("cash"), 4)))
    lines.append("| Equity (broker) | {} |".format(_fmt(broker_snapshot.get("equity"), 4)))
    lines.append("| Realize net PnL (kumulatif) | {} |".format(_fmt(broker_snapshot.get("realized_net_pnl"), 4)))
    lines.append("| Gorunmeyen (unrealized) net PnL | {} |".format(_fmt(broker_snapshot.get("unrealized_net_pnl"), 4)))
    lines.append("")

    if risk_state:
        lines.append("## Risk durumu")
        lines.append("")
        lines.append("| Alan | Deger |")
        lines.append("| --- | --- |")
        for key in sorted(risk_state):
            lines.append("| {} | {} |".format(key, _fmt(risk_state[key])))
        lines.append("")

    lines.append("## Gunun islemleri")
    lines.append("")
    if day_trades:
        lines.append("| # | Parite | Giris ts | Cikis ts | Miktar | Brut | Net | Net % | Cikis nedeni |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for index, trade in enumerate(day_trades, start=1):
            lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                index, trade.get("pair"), trade.get("entry_ts"), trade.get("exit_ts"), _fmt(trade.get("qty")),
                _fmt(trade.get("gross_pnl")), _fmt(trade.get("net_pnl")), _fmt(trade.get("net_pnl_pct")),
                trade.get("exit_reason")))
    else:
        lines.append("_Bugun kapanan islem yok._")
    lines.append("")

    if day_orders:
        lines.append("## Reddedilen / kismi emirler")
        lines.append("")
        lines.append("| ts | Parite | Yon | Durum | Istenen | Dolan | Neden |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for order in day_orders:
            lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
                order.get("ts"), order.get("pair"), order.get("side"), order.get("status"),
                _fmt(order.get("requested_qty"), 8), _fmt(order.get("filled_qty"), 8), order.get("reason")))
        lines.append("")

    if day_events:
        lines.append("## Olaylar (risk/limit/feed)")
        lines.append("")
        lines.append("| ts | Seviye | Kategori | Kod | Mesaj |")
        lines.append("| --- | --- | --- | --- | --- |")
        for event in day_events:
            lines.append("| {} | {} | {} | {} | {} |".format(
                event.get("ts"), event.get("level"), event.get("category"),
                event.get("code"), str(event.get("message")).replace("|", "/")))
        lines.append("")

    if reconciliation is not None:
        lines.append("## Mutabakat (ledger <-> broker)")
        lines.append("")
        lines.append("- Sonuc: **{}**".format("PASS" if reconciliation.get("ok") else "FAIL"))
        lines.append("- Tolerans: {} USDT".format(reconciliation.get("tolerance")))
        for check in reconciliation.get("checks", []):
            lines.append("- [{}] {}: ledger={} broker={} delta={}".format(
                "ok" if check.get("ok") else "FAIL", check.get("name"),
                check.get("ledger"), check.get("broker"), check.get("delta")))
        lines.append("")

    lines.append("Ayrintili limitler ve uyarilar icin `RISK.md`; kurulum/devir teslim icin `HANDOVER.md`.")
    lines.append("")
    return "\n".join(lines)


def write_daily_report(
    reports_dir: Path | str,
    *,
    day: str,
    config: Config | None,
    broker_snapshot: Mapping[str, Any],
    trades: Sequence[Mapping[str, Any]],
    equity_points: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    reconciliation: Optional[Mapping[str, Any]] = None,
    risk_state: Optional[Mapping[str, Any]] = None,
    run_id: str = "",
) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    text = build_daily_report(
        day=day, config=config, broker_snapshot=broker_snapshot, trades=trades,
        equity_points=equity_points, events=events, orders=orders,
        reconciliation=reconciliation, risk_state=risk_state, run_id=run_id,
    )
    path = reports_dir / "daily_{}.md".format(day)
    path.write_text(text, encoding="utf-8")
    log.info("daily_report.written", extra={"event": "daily_report_written", "path": str(path), "day": day})
    return path


__all__ = ["build_daily_report", "write_daily_report"]
