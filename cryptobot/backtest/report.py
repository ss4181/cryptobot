"""Backtest reporting: deterministic JSON, human Markdown and a PNG equity chart.

The JSON report is the reproducibility artifact: it contains the metrics block,
the config echo, the data echo and a SHA-256 over exactly those -- and **no**
timestamps, so two runs over identical data and config produce byte-identical
files.  Timestamps live only in the Markdown and the PNG.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from ..config import Config

log = logging.getLogger(__name__)

#: Metric key -> Turkish label for the Markdown / console table.
METRIC_LABELS: Sequence[tuple] = (
    ("trade_count", "Islem sayisi"),
    ("winning_trades", "Kazanan islem"),
    ("losing_trades", "Kaybeden islem"),
    ("win_rate_pct", "Kazanma orani (%)"),
    ("net_pnl_usdt", "Net kar (USDT)"),
    ("net_pnl_pct", "Net kar (%)"),
    ("gross_pnl_usdt", "Brut kar (USDT)"),
    ("final_equity_usdt", "Final equity (USDT)"),
    ("max_drawdown_pct", "Maks drawdown (%)"),
    ("max_drawdown_usdt", "Maks drawdown (USDT)"),
    ("profit_factor", "Profit factor"),
    ("avg_net_pnl_per_trade_usdt", "Islem basi ort. net (USDT)"),
    ("avg_net_pnl_pct_per_trade", "Islem basi ort. net (%)"),
    ("best_trade_usdt", "En iyi islem (USDT)"),
    ("worst_trade_usdt", "En kotu islem (USDT)"),
    ("avg_win_usdt", "Ort. kazanc (USDT)"),
    ("avg_loss_usdt", "Ort. kayip (USDT)"),
    ("avg_bars_in_trade", "Islem basi ort. bar"),
    ("sharpe_ratio", "Sharpe"),
    ("total_fees_usdt", "Toplam komisyon (USDT)"),
    ("total_slippage_usdt", "Toplam slippage (USDT)"),
    ("total_cost_drag_usdt", "Toplam maliyet yuku (USDT)"),
    ("bars", "Islenen bar"),
    ("timeframe", "Timeframe"),
    ("pairs", "Pariteler"),
)


def report_basename(config: Config) -> str:
    return "backtest_{}_{}".format(config.pair_slug(), config.timeframe)


def fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "{:.6f}".format(value).rstrip("0").rstrip(".") if value % 1 else "{:.2f}".format(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def write_json_report(result: Any, reports_dir: Path, basename: str, *, extra: Mapping[str, Any] | None = None) -> Path:
    """Write the byte-stable metrics JSON."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = result.metrics_payload(extra=extra)
    path = reports_dir / "{}.json".format(basename)
    # sort_keys + fixed separators + trailing newline => byte-identical across runs.
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
                    encoding="utf-8")
    log.info("report.json", extra={"event": "report_json", "path": str(path),
                                   "determinism_hash": payload["determinism_hash"]})
    return path


def metrics_table_markdown(metrics: Mapping[str, Any]) -> str:
    lines = ["| Metrik | Deger |", "| --- | --- |"]
    for key, label in METRIC_LABELS:
        if key in metrics:
            lines.append("| {} | {} |".format(label, fmt(metrics.get(key))))
    return "\n".join(lines)


def config_table_markdown(config_echo: Mapping[str, Any]) -> str:
    lines = ["| Parametre | Deger |", "| --- | --- |"]
    for key in sorted(config_echo):
        value = config_echo[key]
        if key == "strategy":
            lines.append("| strategy.name | {} |".format(value.get("name")))
            for param, param_value in sorted((value.get("params") or {}).items()):
                lines.append("| strategy.{} | {} |".format(param, fmt(param_value)))
        else:
            lines.append("| {} | {} |".format(key, fmt(value)))
    return "\n".join(lines)


def write_markdown_report(
    result: Any,
    reports_dir: Path,
    basename: str,
    *,
    generated_at: str | None = None,
    chart_name: str | None = None,
) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    metrics = result.metrics
    trades: List[Mapping[str, Any]] = result.trades

    parts: List[str] = []
    parts.append("# Backtest Raporu -- {}".format(basename))
    parts.append("")
    parts.append("> **UYARI:** Bu bir simülasyondur. Gercek emir gönderilmez, API anahtari kullanilmaz.")
    parts.append("> Gecmis performans gelecek performansi garanti etmez. Yatirim tavsiyesi degildir.")
    parts.append("")
    parts.append("- Olusturma zamani (UTC): `{}`".format(stamp))
    parts.append("- Determinizm hash: `{}`".format(result.determinism_hash))
    parts.append("- Run id: `{}`".format(result.run_id))
    parts.append("- Islenen bar: {}".format(result.bars_processed))
    parts.append("- Trading durumu: `{}`".format("NORMAL" if not result.warnings else "FAIL-SAFE (yeni giris yok)"))
    parts.append("")

    if result.warnings:
        parts.append("## Uyarilar")
        parts.append("")
        for warning in result.warnings:
            parts.append("- {}".format(warning))
        parts.append("")

    parts.append("## Ozet metrikler")
    parts.append("")
    parts.append(metrics_table_markdown(metrics))
    parts.append("")

    if chart_name:
        parts.append("## Equity egrisi")
        parts.append("")
        parts.append("![equity curve]({})".format(chart_name))
        parts.append("")

    parts.append("## Veri")
    parts.append("")
    parts.append("| Parite | Bar | Ilk ts | Son ts | Kaynak |")
    parts.append("| --- | --- | --- | --- | --- |")
    for pair, meta in sorted((result.data_echo.get("pairs") or {}).items()):
        parts.append("| {} | {} | {} | {} | {} |".format(
            pair, meta.get("rows"), meta.get("first_ts"), meta.get("last_ts"), meta.get("source")))
    parts.append("")

    parts.append("## Kapanan islemler (ilkan 50)")
    parts.append("")
    if trades:
        parts.append("| # | Parite | Giris ts | Cikis ts | Giris (fill) | Cikis (fill) | Miktar | Brut | Net | Net % | Cikis nedeni |")
        parts.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for index, trade in enumerate(trades[:50], start=1):
            parts.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                index, trade.get("pair"), trade.get("entry_ts"), trade.get("exit_ts"),
                fmt(trade.get("entry_fill_price")), fmt(trade.get("exit_fill_price")), fmt(trade.get("qty")),
                fmt(trade.get("gross_pnl")), fmt(trade.get("net_pnl")), fmt(trade.get("net_pnl_pct")),
                trade.get("exit_reason"),
            ))
    else:
        parts.append("_Bu kosuda hic islem kapanmadi._")
    parts.append("")

    parts.append("## Konfigurasyon")
    parts.append("")
    parts.append(config_table_markdown(result.config_echo))
    parts.append("")

    parts.append("## Model varsayimlari")
    parts.append("")
    parts.append("- Sinyal kapanmis bar uzerinde uretilir, giris ayni barin kapanisinda komisyon + slippage ile dolar.")
    parts.append("- Bar *t*'de acilan pozisyon en erken bar *t+1*'de kapatilabilir (look-ahead yok).")
    parts.append("- Ayni bar hem stop hem take-profit seviyesine degerse **stop** islenir (kotumser varsayim).")
    parts.append("- Acik kalan pozisyon kapanis fiyatiyla isaretlenir; kapatilmaz, final equity'de görunur.")
    parts.append("- Komisyon: her iki bacakta `fee_pct`, slippage: her iki bacakta `slippage_pct`.")
    parts.append("")
    parts.append("Ayrintili risk notlari icin `RISK.md` dosyasina bakin.")
    parts.append("")

    path = reports_dir / "{}.md".format(basename)
    path.write_text("\n".join(parts), encoding="utf-8")
    log.info("report.markdown", extra={"event": "report_markdown", "path": str(path)})
    return path


def write_equity_chart(result: Any, reports_dir: Path, basename: str, *, dpi: int = 110) -> Path | None:
    """PNG equity + drawdown chart. Returns ``None`` if matplotlib is unavailable."""
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless / CI safe
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - matplotlib is a declared dependency
        log.warning("report.chart_skipped", extra={"event": "chart_skipped", "detail": str(exc)})
        return None

    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    curve = result.equity_curve
    if not curve:
        return None

    times = [point["ts"] for point in curve]
    equity = [point["equity"] for point in curve]
    peaks: List[float] = []
    drawdowns: List[float] = []
    running = float("-inf")
    for value in equity:
        running = max(running, value)
        peaks.append(running)
        drawdowns.append((value - running) / running * 100.0 if running else 0.0)

    figure, (top, bottom) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                         gridspec_kw={"height_ratios": [3, 1]})
    top.plot(times, equity, linewidth=1.2, label="Equity (USDT)")
    top.axhline(result.metrics.get("initial_capital_usdt") or 0.0, linestyle="--", linewidth=1.0,
                color="grey", label="Baslangic sermayesi")
    top.set_title("{} | {} | {} -- {} islem, net {} USDT".format(
        ", ".join(result.metrics.get("pairs") or []), result.metrics.get("timeframe"),
        "PAPER/BACKTEST", result.metrics.get("trade_count"), fmt(result.metrics.get("net_pnl_usdt"))))
    top.set_ylabel("Equity (USDT)")
    top.grid(alpha=0.3)
    top.legend(loc="best", fontsize=8)

    bottom.fill_between(times, drawdowns, 0, color="#c0392b", alpha=0.35)
    bottom.set_ylabel("Drawdown (%)")
    bottom.set_xlabel("Zaman")
    bottom.grid(alpha=0.3)

    figure.tight_layout()
    path = reports_dir / "{}.png".format(basename)
    figure.savefig(path, dpi=dpi)
    plt.close(figure)
    log.info("report.chart", extra={"event": "report_chart", "path": str(path)})
    return path


def write_reports(
    result: Any,
    reports_dir: Path,
    *,
    basename: str | None = None,
    generated_at: str | None = None,
    with_chart: bool = True,
) -> Dict[str, Path]:
    """Write JSON + Markdown (+ PNG) and return the created paths."""
    name = basename or report_basename_from_echo(result.config_echo)
    json_path = write_json_report(result, reports_dir, name)
    chart_path = write_equity_chart(result, reports_dir, name) if with_chart else None
    md_path = write_markdown_report(
        result, reports_dir, name, generated_at=generated_at,
        chart_name=chart_path.name if chart_path else None,
    )
    out = {"json": json_path, "markdown": md_path}
    if chart_path is not None:
        out["chart"] = chart_path
    return out


def report_basename_from_echo(config_echo: Mapping[str, Any]) -> str:
    pairs = "-".join(str(p).replace("/", "") for p in (config_echo.get("pairs") or []))
    return "backtest_{}_{}".format(pairs, config_echo.get("timeframe"))


__all__ = [
    "report_basename", "report_basename_from_echo", "write_json_report", "write_markdown_report",
    "write_equity_chart", "write_reports", "metrics_table_markdown", "config_table_markdown", "fmt",
    "METRIC_LABELS",
]
