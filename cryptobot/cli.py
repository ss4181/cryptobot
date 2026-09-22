"""Command line interface: ``python -m cryptobot <command>``.

Commands
--------
``download``  fetch public candles into ``data/cache`` (no key needed)
``backtest``  run the deterministic backtest and write JSON/MD/PNG reports
``run``       paper-trading loop (bounded: ``--cycles`` / ``--duration-seconds``)
``status``    show the last/current paper run state, risk limits and broker book
``report``    (re)generate the daily Markdown report from the ledger
``export``    export the ledger to CSV + JSON
``verify``    recompute balances from the ledger and reconcile against the broker
``stop``      request a cooperative stop of a running paper loop
``safety``    print the safety posture (paper-only, no keys, no live orders)
``panel``     one self-contained HTML view: notifications + trades + KPIs
``acceptance`` run every acceptance criterion offline and write the evidence

Exit codes: ``0`` ok, ``1`` failure / failed verification, ``2`` bad usage,
``3`` safety violation (live mode requested), ``4`` busy -- another bot instance
already owns the run directory, or another process holds the ledger's SQLite
write lock (``database is locked``).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import SAFETY_BANNER, __version__, safety
from .backtest.engine import BacktestEngine
from .backtest.report import report_basename, write_reports
from .config import Config, ConfigError, load_config
from .data.feed import (
    CacheCorrupted,
    FeedError,
    RequestsTransport,
    fetch_history,
    load_candles,
)
from .execution.paper_broker import FaultInjector
from .ledger.store import Ledger
from .monitor.daily_report import write_daily_report
from .monitor.logging_setup import setup_logging
from .notify import build_notifier
from .notify.events import EVENT_TYPES
from .notify.providers import build_providers
from .notify.render import MAX_LINES as RENDER_MAX_LINES
from .notify.samples import preview_events
from .notify.store import NotificationStore
from .panel import (
    DEFAULT_LIMIT as PANEL_DEFAULT_LIMIT,
    DEFAULT_PORT as PANEL_DEFAULT_PORT,
    PanelServer,
    build_panel,
    remote_references,
    render_panel,
    scheme_references,
)
from .risk.manager import utc_day
from .runner import (
    AlreadyRunningError,
    PaperRunner,
    RunnerConfig,
    clear_stale_state,
    describe_running_instance,
    detect_running_instance,
    read_state,
    request_stop,
    stop_path,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_SAFETY = 3
#: Resource busy: a live duplicate instance, or a ledger held by another process.
EXIT_BUSY = 4

log = logging.getLogger("cryptobot.cli")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="config.yaml yolu (varsayilan: cryptobot/config.yaml)")
    parser.add_argument("--pairs", type=str, default=None, help="virgulle ayrilmis pariteler, orn. BTC/USDT,ETH/USDT")
    parser.add_argument("--timeframe", type=str, default=None, help="1m|5m|15m|30m|1h|4h|1d")
    parser.add_argument("--mode", type=str, default=None, help="sadece 'paper' veya 'backtest' (live yoktur)")
    parser.add_argument("--fee-pct", type=float, default=None, help="bacak basina taker komisyon %%")
    parser.add_argument("--slippage-pct", type=float, default=None, help="bacak basina slippage %%")
    parser.add_argument("--net-target-pct", type=float, default=None, help="net kar hedefi %% (komisyon+slippage sonrasi)")
    parser.add_argument("--stop-loss-pct", type=float, default=None, help="stop-loss %%")
    parser.add_argument("--max-position-pct", type=float, default=None, help="tek pozisyon icin equity %%")
    parser.add_argument("--max-open-positions", type=int, default=None, help="eszamanli pozisyon siniri")
    parser.add_argument("--daily-loss-limit-pct", type=float, default=None, help="gunluk zarar limiti %%")
    parser.add_argument("--cooldown-minutes", type=float, default=None, help="zarar sonrasi bekleme (dk)")
    parser.add_argument("--initial-capital", type=float, default=None, help="baslangic sanal bakiye (USDT)")
    parser.add_argument("--db-path", type=str, default=None, help="ledger SQLite dosyasi")
    parser.add_argument("--log-level", type=str, default=None, help="DEBUG|INFO|WARNING|ERROR")


def _cli_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    pairs = None
    if getattr(args, "pairs", None):
        pairs = [p.strip().upper() for p in str(args.pairs).split(",") if p.strip()]
    return {
        "pairs": pairs,
        "timeframe": getattr(args, "timeframe", None),
        "mode": getattr(args, "mode", None),
        "fee_pct": getattr(args, "fee_pct", None),
        "slippage_pct": getattr(args, "slippage_pct", None),
        "net_profit_target_pct": getattr(args, "net_target_pct", None),
        "stop_loss_pct": getattr(args, "stop_loss_pct", None),
        "max_position_pct": getattr(args, "max_position_pct", None),
        "max_open_positions": getattr(args, "max_open_positions", None),
        "daily_loss_limit_pct": getattr(args, "daily_loss_limit_pct", None),
        "cooldown_minutes": getattr(args, "cooldown_minutes", None),
        "initial_capital_usdt": getattr(args, "initial_capital", None),
        "data": {"db_path": getattr(args, "db_path", None)} if getattr(args, "db_path", None) else None,
        "logging": {"level": getattr(args, "log_level", None)} if getattr(args, "log_level", None) else None,
    }


def _load(args: argparse.Namespace) -> Config:
    overrides = {k: v for k, v in _cli_overrides(args).items() if v is not None}
    return load_config(getattr(args, "config", None), cli_overrides=overrides)


def _prepare(config: Config, *, run_id: Optional[str] = None, quiet: bool = False) -> Optional[Path]:
    path = setup_logging(config.logs_dir, level=config.logging.level, run_id=run_id)
    if not quiet:
        print(SAFETY_BANNER)
        print("log dosyasi: {}".format(path))
    log.info("cli.start", extra={"event": "cli_start", "argv": sys.argv[1:], "version": __version__,
                                 "mode": config.mode})
    credentials = safety.audit_credentials()
    if credentials:
        print("UYARI: ortamda bulunan kimlik degiskenleri KULLANILMAYACAK: {}".format(", ".join(credentials)))
        log.warning("cli.credentials_ignored", extra={"event": "credentials_ignored", "names": list(credentials)})
    return path


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return "{:.{}f}".format(value, digits)
    return str(value)


def _apply_notify_flags(config: Config, args: argparse.Namespace) -> Config:
    """Apply ``--no-notify`` / ``--notify-dry-run`` on top of the loaded config.

    Done here (instead of via ``cli_overrides``) so the CLI flags compose with
    *and* win over the environment/``config.yaml`` values without wiping the
    notification dict the way a whole-key overlay would.
    """
    notifications = config.notifications
    if getattr(args, "no_notify", False):
        notifications = dataclasses.replace(notifications, enabled=False)
    elif getattr(args, "notify_dry_run", False):
        notifications = dataclasses.replace(notifications, enabled=True, dry_run=True)
    if notifications is config.notifications:
        return config
    return dataclasses.replace(config, notifications=notifications)


def is_ledger_lock_error(exc: sqlite3.Error) -> bool:
    """True for SQLite's "another connection holds the lock" failures."""
    name = str(getattr(exc, "sqlite_errorname", "") or "")
    if name in ("SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT", "SQLITE_LOCKED", "SQLITE_LOCKED_SHAREDCACHE"):
        return True
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def ledger_locked_message(exc: sqlite3.Error, *, db_path: Any = None) -> str:
    """Short, actionable Turkish message for a held ledger -- never a traceback."""
    where = "  - Kullanilan defter: {}\n".format(db_path) if db_path else ""
    error = "{}.{}: {}".format(type(exc).__module__, type(exc).__name__, exc)
    return (
        "LEDGER KILITLI: islem defterini baska bir surec kullaniyor "
        "(buyuk olasilikla calisan baska bir bot ornegi; bazen defteri acik tutan bir arac).\n"
        "Ne yapmali:\n"
        "  - Calisan diger botu durdurun: start-bot.cmd stop\n"
        "  - Ya da bu komutu farkli bir deftere yonlendirin: --db-path <dosya>\n"
        "  - Defteri acik tutan araclari kapatin (ornek: --serve panelleri).\n"
        "{where}Teknik ayrinti: {error}".format(where=where, error=error)
    )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_download(args: argparse.Namespace) -> int:
    config = _load(args)
    _prepare(config, quiet=args.quiet)
    days = int(args.days or config.data.history_days)
    transport = RequestsTransport()
    failed: List[str] = []
    for pair in config.pairs:
        print("indiriliyor: {} {} ({} gun)...".format(pair, config.timeframe, days))
        try:
            result = fetch_history(config, pair, days=days, transport=transport, force_network=args.force)
        except FeedError as exc:
            print("  HATA: {}: {}".format(type(exc).__name__, exc))
            log.error("download.failed", extra={"event": "download_failed", "pair": pair,
                                                "error": type(exc).__name__, "detail": str(exc)})
            failed.append(pair)
            continue
        print("  {} satir, kaynak={}, tam={}, ilk_ts={}, son_ts={}".format(
            result.rows, result.source, result.complete,
            int(result.frame["ts"].iloc[0]), int(result.frame["ts"].iloc[-1])))
        for warning in result.warnings:
            print("  uyari: {}".format(warning))
    if failed:
        print("Basarisiz pariteler: {}".format(", ".join(failed)))
        return EXIT_FAILURE
    return EXIT_OK


def cmd_backtest(args: argparse.Namespace) -> int:
    config = _load(args)
    _prepare(config, run_id=args.run_id, quiet=args.quiet)
    days = int(args.days or config.data.history_days)

    frames = {}
    data_meta: Dict[str, Any] = {}
    for pair in config.pairs:
        try:
            result = load_candles(config, pair, days=days, allow_network=not args.offline)
        except (FeedError, CacheCorrupted) as exc:
            print("VERI HATASI ({}): {}: {}".format(pair, type(exc).__name__, exc))
            print("Ipucu: once `python -m cryptobot download{}` calistirin.".format(
                " --offline" if args.offline else ""))
            return EXIT_FAILURE
        frames[pair] = result.frame
        data_meta[pair] = {"source": result.source, "complete": result.complete, "rows": result.rows}

    run_id = args.run_id or "backtest-{}-{}".format(config.pair_slug(), config.timeframe)
    ledger = Ledger(config.db_path, run_id)
    ledger.start_run(mode="backtest", started_at=int(datetime.now(timezone.utc).timestamp() * 1000),
                     version=__version__, config=config.as_dict(), data=data_meta)
    engine = BacktestEngine(config, ledger=ledger, run_id=run_id)
    engine.mode = "backtest"
    result = engine.run(frames, data_meta=data_meta)

    basename = args.out_name or report_basename(config)
    paths = write_reports(result, config.reports_dir, basename=basename, with_chart=not args.no_chart)
    ledger.close()

    print("")
    print("== Backtest ozeti: {} ==".format(basename))
    for key in ("trade_count", "win_rate_pct", "net_pnl_usdt", "net_pnl_pct", "max_drawdown_pct",
                "profit_factor", "avg_net_pnl_per_trade_usdt", "sharpe_ratio",
                "total_fees_usdt", "total_slippage_usdt", "final_equity_usdt"):
        print("  {:<28} {}".format(key, _fmt(result.metrics.get(key))))
    print("  {:<28} {}".format("determinism_hash", result.determinism_hash))
    for name, path in sorted(paths.items()):
        print("  {:<28} {}".format(name, path))
    if args.json_stdout:
        print(json.dumps(result.metrics_payload(), indent=2, sort_keys=True, ensure_ascii=False))
    return EXIT_OK


def _fault_injector(mode: str) -> Optional[FaultInjector]:
    """Build a deterministic fault injector for ``--faults demo`` (never random)."""
    if mode == "none":
        return None
    injector = FaultInjector().enable()
    injector.reject_next("buy", count=1).reject_next("sell", count=1).partial_next("buy", 0.5)
    return injector


def cmd_run(args: argparse.Namespace) -> int:
    # Duplicate-instance guard, first thing and before any output: the pid/state
    # files are the run directory's ownership marker, and a second bot would fight
    # the first one for them and for the ledger's write lock (`database is locked`).
    live = detect_running_instance()
    if live is not None:
        # Deliberately no logger call here: this runs before _prepare() configures
        # logging, so a record would only reach the root logger's last-resort
        # handler as a bare line.  The runner's own guard logs the refusal.
        print(describe_running_instance(live), file=sys.stderr)
        return EXIT_BUSY
    config = _apply_notify_flags(_load(args), args)
    # One status line, always (not gated by --quiet, which only hides the banner and
    # the log path): recovering from a dead run is operationally relevant.
    stale_note = clear_stale_state()
    if stale_note:
        print(stale_note)
    log_path = _prepare(config, run_id=args.run_id, quiet=args.quiet)
    faults = _fault_injector(args.faults)
    # Replay walks history as fast as it can; a real-time loop waits between cycles.
    interval = args.interval_seconds
    if interval is None:
        interval = 0.0 if args.replay else 60.0
    # Say out loud what the notification layer is scoped to (and whether a real
    # push is possible in this mode), so "why did/didn't I get a message" is
    # answerable from the console alone.
    if not args.quiet:
        sends = args.notify_send or not (args.replay or args.offline)
        print("bildirim: notify_on={} | ag gonderimi: {}".format(
            ",".join(config.notifications.notify_on) or "(bos)",
            "ACIK" if sends else "KAPALI (replay/offline; acmak icin --notify-send)"))
    runner = PaperRunner(
        config,
        runner=RunnerConfig(
            cycles=args.cycles,
            duration_seconds=args.duration_seconds,
            interval_seconds=interval,
            offline=args.offline,
            replay=args.replay,
            replay_bars_per_cycle=args.replay_bars,
            notify_send=args.notify_send,
        ),
        faults=faults,
        run_id=args.run_id,
        setup_logs=False,  # logging already configured above
        log_path=log_path,
    )
    summary = runner.run()
    print("")
    print("== Paper run ozeti ==")
    print("  run_id               {}".format(summary["run_id"]))
    print("  cycle                {}".format(summary["cycles"]))
    print("  islenen bar          {}".format(summary["bars_processed"]))
    print("  kapanan islem        {}".format(summary["trades_closed"]))
    print("  final equity (USDT)  {}".format(_fmt(summary["final_equity"])))
    print("  net PnL (USDT)       {}".format(_fmt(summary["net_pnl_usdt"])))
    print("  net PnL (%)          {}".format(_fmt(summary["net_pnl_pct"])))
    print("  kazanma orani (%)    {}".format(_fmt(summary["win_rate_pct"])))
    print("  trading durumu       {}".format(summary["trading_state"]))
    print("  ledger satirlari     {}".format(summary["ledger_counts"]))
    print("  mutabakat            {}".format("PASS" if summary["verification"]["ok"] else "FAIL"))
    print("  gunluk rapor         {}".format(summary["daily_report"]))
    print("  log dosyasi          {}".format(summary["log_file"]))
    for check in summary["verification"]["checks"]:
        print("    [{}] {} delta={}".format("ok" if check["ok"] else "FAIL", check["name"], check["delta"]))
    if args.json_stdout:
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    return EXIT_OK if summary["verification"]["ok"] else EXIT_FAILURE


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    state = read_state()
    stopped = stop_path().exists()
    print(SAFETY_BANNER)
    print("")
    if not state:
        print("Kayitli paper run durumu bulunamadi ({}).".format(str(Path(config.db_path).parent)))
        print("Baslatmak icin: python -m cryptobot run --cycles 5 --replay --replay-bars 500")
        return EXIT_OK

    from .data.feed import now_ms

    age = max(0, (now_ms() - int(state.get("heartbeat", 0))) / 1000.0)
    running_hint = "calisiyor olabilir" if age <= 600 else "durmus gorunuyor"
    print("state          : {} ({}, heartbeat {:.0f}s once)".format(state.get("state"), running_hint, age))
    print("run_id         : {}".format(state.get("run_id")))
    print("mode           : {} | trading_state: {}".format(state.get("mode"), state.get("trading_state")))
    print("cycles_run     : {}".format(state.get("cycles_run")))
    print("halted         : {} ({})".format(state.get("halted"), (state.get("risk_state") or {}).get("halt_reason")))
    print("stop sentinel  : {}".format(stopped))

    broker = state.get("broker") or {}
    print("")
    print("-- broker --")
    for key in ("cash", "positions_value", "equity", "realized_net_pnl", "unrealized_net_pnl",
                "open_positions", "closed_trades", "total_fees", "total_slippage_cost"):
        print("  {:<20} {}".format(key, _fmt(broker.get(key))))
    positions = broker.get("positions") or {}
    if positions:
        print("  acik pozisyonlar:")
        for pair, position in sorted(positions.items()):
            print("    {} qty={} giris={} stop={} tp={}".format(
                pair, _fmt(position.get("qty"), 8), _fmt(position.get("entry_fill_price"), 8),
                _fmt(position.get("stop_price"), 8), _fmt(position.get("tp_price"), 8)))
    stats = broker.get("stats") or {}
    print("  emirler: {}".format({k: v for k, v in sorted(stats.items()) if k != "rejected_reasons"}))
    if stats.get("rejected_reasons"):
        print("  red nedenleri: {}".format(stats["rejected_reasons"]))

    print("")
    print("-- risk limitleri --")
    for key, value in sorted((state.get("risk_limits") or {}).items()):
        print("  {:<24} {}".format(key, _fmt(value, 6)))
    print("-- risk durumu --")
    for key, value in sorted((state.get("risk_state") or {}).items()):
        print("  {:<24} {}".format(key, _fmt(value)))
    print("")
    print("safety: {}".format(state.get("safety")))
    print("db    : {}".format(state.get("db_path")))
    print("log   : {}".format(state.get("log_file")))
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    config = _load(args)
    _prepare(config, quiet=args.quiet)
    path = Path(config.db_path)
    if not path.exists():
        print("Ledger bulunamadi: {}".format(path))
        return EXIT_FAILURE
    with Ledger(path, "__report__") as ledger_reader:
        run_id = args.run_id or ledger_reader.latest_run_id()
        if not run_id:
            print("Ledger bos: once `python -m cryptobot backtest` veya `run` calistirin.")
            return EXIT_FAILURE
        ledger = Ledger(path, run_id)
        trades = ledger.fetch_trades()
        equity = ledger.fetch_equity()
        events = ledger.fetch_events()
        orders = ledger.fetch_orders()
        run_row = (ledger._query("SELECT * FROM runs WHERE run_id = ?", (run_id,)) or [{}])[0]  # noqa: SLF001
        day = args.day or (str(run_row.get("started_iso", ""))[:10] or utc_day(int(datetime.now(timezone.utc).timestamp() * 1000)))
        if args.day == "latest" and equity:
            day = str(equity[-1]["iso_utc"])[:10]
        realized = ledger.realized_net_from_ledger()
        snapshot = {
            "equity": float(equity[-1]["equity"]) if equity else config.initial_capital_usdt,
            "cash": float(equity[-1]["cash"]) if equity else config.initial_capital_usdt,
            "open_positions": int(equity[-1]["open_positions"]) if equity else 0,
            "realized_net_pnl": realized,
            "unrealized_net_pnl": None,
        }
        out = write_daily_report(
            config.reports_dir, day=day, config=config, broker_snapshot=snapshot, trades=trades,
            equity_points=equity, events=events, orders=orders, run_id=run_id,
            reconciliation=ledger.verify(
                initial_cash=config.initial_capital_usdt,
                broker_cash=float(equity[-1]["cash"]) if equity else config.initial_capital_usdt,
                broker_realized_net_pnl=realized,
            ).as_dict(),
        )
        ledger.close()
    print("gunluk rapor yazildi: {}".format(out))
    print("run_id: {} | kapanan islem: {} | ledger satiri: {}".format(run_id, len(trades), len(equity)))
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    config = _load(args)
    _prepare(config, quiet=args.quiet)
    path = Path(config.db_path)
    if not path.exists():
        print("Ledger bulunamadi: {}".format(path))
        return EXIT_FAILURE
    out_dir = Path(args.out) if args.out else (config.reports_dir / "exports")
    with Ledger(path, "__export__") as reader:
        run_id = args.run_id or reader.latest_run_id()
    if not run_id:
        print("Ledger bos.")
        return EXIT_FAILURE
    ledger = Ledger(path, run_id)
    written = ledger.export_csv(out_dir)
    json_path = ledger.export_json(out_dir / "ledger_{}.json".format(run_id))
    ledger.close()
    print("run_id: {}".format(run_id))
    for name, target in sorted(written.items()):
        print("  csv  {:<8} {}".format(name, target))
    print("  json          {}".format(json_path))
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    config = _load(args)
    _prepare(config, quiet=args.quiet)
    path = Path(config.db_path)
    if not path.exists():
        print("Ledger bulunamadi: {}".format(path))
        return EXIT_FAILURE
    with Ledger(path, "__verify__") as reader:
        run_id = args.run_id or reader.latest_run_id()
    if not run_id:
        print("Ledger bos.")
        return EXIT_FAILURE
    ledger = Ledger(path, run_id)
    equity = ledger.fetch_equity()
    start = float(equity[0]["cash"]) if equity else config.initial_capital_usdt
    broker_cash = float(equity[-1]["cash"]) if equity else config.initial_capital_usdt
    realized = ledger.realized_net_from_ledger()
    report = ledger.verify(
        initial_cash=config.initial_capital_usdt,
        broker_cash=broker_cash,
        broker_realized_net_pnl=realized,
        tolerance=args.tolerance,
    )
    print("run_id: {}".format(run_id))
    print(report.render())
    print("ledger satirlari: {}".format(ledger.counts()))
    print("ilk equity snapshot cash: {} | son: {}".format(start, broker_cash))
    ledger.close()
    return EXIT_OK if report.ok else EXIT_FAILURE


def cmd_stop(args: argparse.Namespace) -> int:
    path = request_stop("cli stop")
    print("Stop istegi yazildi: {}".format(path))
    print("Calisan paper dongusu dongu sonunda temiz sekilde duracak ve raporlarini yazacak.")
    return EXIT_OK


def cmd_panel(args: argparse.Namespace) -> int:
    """One self-contained HTML view of the notifications *and* the trades.

    Read-only over ``logs/notifications.jsonl`` and ``data/ledger.sqlite``: the
    panel never dispatches a notification, never places an order and never writes
    to the ledger.  ``--serve`` additionally publishes the same document on
    ``127.0.0.1`` only.
    """
    config = _load(args)
    _prepare(config, quiet=args.quiet)
    db_path = Path(config.db_path)
    audit_path = _notify_store_path(config)
    limit = int(args.limit if args.limit is not None else PANEL_DEFAULT_LIMIT)
    run_id = args.run_id or None

    def provider() -> str:
        return render_panel(build_panel(db_path=db_path, audit_path=audit_path, run_id=run_id),
                            limit=limit)

    data = build_panel(db_path=db_path, audit_path=audit_path, run_id=run_id)
    document = render_panel(data, limit=limit)

    out = Path(args.out) if args.out else (Path(config.reports_dir) / "panel" / "index.html")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(document, encoding="utf-8")
    except OSError as exc:
        print("PANEL YAZILAMADI: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return EXIT_FAILURE

    trades = data.stats["trades"]
    notif = data.stats["notifications"]
    factor = trades["profit_factor"]
    print("")
    print("== operator paneli (bildirim + islem) ==")
    print("  dosya        : {} ({} bayt)".format(out, out.stat().st_size))
    print("  secim        : {} | kosu: {} | islem: {} kapanan / {} acik | bildirim: {} satir".format(
        data.selection_label, len(data.runs), len(data.trades), len(data.open_positions),
        len(data.notifications)))
    print("  KPI          : teslim {}/{} gonderildi ({} hata) | net {} USDT | kazanma {} | PF {}".format(
        notif["sent"], notif["total"], notif["failed"],
        "n/a" if trades["net_pnl_usdt"] is None else "{:.4f}".format(trades["net_pnl_usdt"]),
        "n/a" if trades["win_rate_pct"] is None else "{:.2f}%".format(trades["win_rate_pct"]),
        "n/a" if factor is None else ("inf" if factor == float("inf") else "{:.3f}".format(factor))))
    print("  kisitlama    : --limit {} ({})".format(limit, "sinirsiz" if limit <= 0 else "tablo basina satir"))
    for item in data.warnings:
        print("  UYARI        : {}".format(item))
    remote = remote_references(document)
    print("  bagimsizlik  : uzak kaynak referansi {} | dosyada gecen http(s):// {}".format(
        len(remote), len(scheme_references(document))))
    for item in remote:  # pragma: no cover - a self-containment bug, reported loudly
        print("    !! {}".format(item))
    print("  redaksiyon   : dosyada {} adet [REDACTED] isareti".format(document.count("[REDACTED]")))
    sys.stdout.flush()
    if remote:
        print("PANEL HATASI: uretilen dosya kendine yeterli degil (uzak referans var).", file=sys.stderr)
        return EXIT_FAILURE

    if args.serve:
        server = PanelServer(provider, port=int(args.port))
        try:
            server.warm()
            print("")
            print("  yerel gorunum: {}".format(server.url))
            print("  yalnizca 127.0.0.1 dinlenir, salt okunur, canli yenilenir (her istekte yeniden uretilir).")
            print("  DURDURMAK ICIN: bu terminalde Ctrl+C (veya pencereyi kapatin).")
            sys.stdout.flush()  # the URL must be visible even when stdout is redirected
            server.serve_forever()
        except KeyboardInterrupt:
            print("")
            print("  panel sunucusu durduruldu.")
        except OSError as exc:
            print("PANEL SUNULAMADI: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
            return EXIT_FAILURE
        finally:
            server.stop()
    return EXIT_OK


def cmd_safety(args: argparse.Namespace) -> int:
    print(SAFETY_BANNER)
    print("")
    print("safety durumu      : {}".format(safety.safety_statement()))
    print("izinli modlar      : {}".format(", ".join(safety.ALLOWED_MODES)))
    print("izinli veri hostlari: {}".format(", ".join(safety.ALLOWED_DATA_HOSTS)))
    credentials = safety.audit_credentials()
    print("ortamdaki kimlikler : {} (kullanilmaz)".format(", ".join(credentials) if credentials else "yok"))
    try:
        safety.assert_allowed_mode("live")
    except safety.LiveTradingForbidden as exc:
        print("canli mod denemesi : ENGELLENDI -> {}".format(exc))
        return EXIT_OK
    return EXIT_FAILURE


def cmd_acceptance(args: argparse.Namespace) -> int:
    """Run the acceptance harness (all criteria) and return its exit code."""
    from .scripts import acceptance_check

    argv = ["--json-stdout"] if getattr(args, "json_stdout", False) else []
    if getattr(args, "reports_dir", None):
        argv += ["--reports-dir", str(args.reports_dir)]
    return acceptance_check.main(argv)


# --------------------------------------------------------------------------- #
# notify (mobile alert layer)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _quiet_cryptobot_logs():
    """Silence the ``cryptobot`` logger hierarchy for the duration of a block.

    Used by ``notify preview``: the historical measurement replays the cache
    through the engine (thousands of INFO records) and the preview must show the
    messages, not the log.  Scoped to the ``cryptobot`` logger only.
    """
    logger = logging.getLogger("cryptobot")
    previous = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        logger.setLevel(previous)


def _notify_config(args: argparse.Namespace) -> Config:
    config = _apply_notify_flags(_load(args), args)
    if getattr(args, "dry_run", False):
        config = dataclasses.replace(
            config, notifications=dataclasses.replace(config.notifications, enabled=True, dry_run=True))
    return config


def cmd_notify_status(args: argparse.Namespace) -> int:
    config = _notify_config(args)
    notifier = build_notifier(config)
    n = config.notifications
    print(SAFETY_BANNER)
    print("")
    print("== bildirim durumu (notify status) ==")
    print("  enabled        : {}".format(n.enabled))
    print("  dry_run        : {}   (true: hicbir sey GONDERILMEZ)".format(n.dry_run))
    print("  min_severity   : {}".format(n.min_severity))
    print("  dedupe_window  : {} sn".format(n.dedupe_window_seconds))
    print("  max_per_hour   : {}   (saatlik AG gonderim siniri; yerel console/file sayilmaz)".format(
        n.max_per_hour))
    budget = notifier.network_budget()
    print("  ag butcesi     : {}/{} kullanildi | kalan {} (son {} dk)".format(
        budget["used"], budget["max_per_hour"], budget["remaining"],
        budget["window_seconds"] // 60))
    if budget["blocks_trade_notification"]:
        print("  butce etkisi   : trade bildirimi ENGELLENIR (max_per_hour dolu;")
        print("                   critical olaylar bu sinira TAKILMAZ, yine gonderilir)")
    else:
        print("  butce etkisi   : trade bildirimi GONDERILIR (butce musait)")
    print("  quiet_hours    : {}".format(
        "kapali" if not n.quiet_hours else "{:02d}:{:02d}-{:02d}:{:02d} (yerel)".format(
            n.quiet_hours[0] // 60, n.quiet_hours[0] % 60,
            n.quiet_hours[1] // 60, n.quiet_hours[1] % 60)))
    active_events = list(n.notify_on)
    filtered_events = [event for event in EVENT_TYPES if event not in n.notify_on and event != "test"]
    print("  notify_on      : {} olay AKTIF -> {}".format(
        len(active_events), ", ".join(active_events) or "(bos)"))
    print("  filtrelenen    : {} olay PASIF -> {}".format(
        len(filtered_events), ", ".join(filtered_events) or "(yok)"))
    print("  (pasif olaylar uygulanmis ve `notify preview --all` ile onizlenebilir;")
    print("   geri acmak icin config.yaml notify_on listesine tek satir ekleyin)")
    print("  timeout/retry  : {} sn / 1+{} deneme".format(n.timeout_seconds, n.retry_max))
    print("  store          : {}".format(_notify_store_path(config)))
    print("")
    print("  provider'lar (config sirasi: {}):".format(", ".join(n.providers)))
    active_count = 0
    for item in notifier.status():
        mark = "AKTIF " if item["active"] else "PASIF "
        if item["active"]:
            active_count += 1
        detail = "" if item["active"] else "  -> {}".format(item["reason"])
        kind = "ag" if item["network"] else "yerel"
        print("    [{}] {:<9} ({}){}".format(mark, item["provider"], kind, detail))
    print("")
    possible, reason = notifier.can_send_to_network()
    print("  GERCEK GONDERIM : {}".format(
        "MUMKUN" if possible else "MUMKUN DEGIL ({})".format(reason or "kapali")))
    for item in notifier.network_targets():
        if not item["active"]:
            continue
        scope = "yerel (loopback)" if item["local"] else "AG: {}".format(item["host"])
        guard = "  [ENGELLI: {}]".format(item["guard"]) if item["guard"] else ""
        print("    - {:<9} hedef={}{}".format(item["provider"], scope, guard))
    if os.environ.get("CRYPTOBOT_NOTIFY_HARNESS"):
        print("  NOT: CRYPTOBOT_NOTIFY_HARNESS={} -> ag saglayicilari yapisal olarak engelli.".format(
            os.environ.get("CRYPTOBOT_NOTIFY_HARNESS")))
    store = NotificationStore(_notify_store_path(config))
    print("")
    print("  kayitli deneme : {} satir".format(store.count()))
    if not n.enabled:
        print("  NOT: enabled=false -> hicbir bildirim uretilmez/denenmez.")
    elif active_count == 0:
        print("  UYARI: enabled=true ama hicbir saglayici aktif degil; hicbir sey gonderilemez.")
    if not n.dry_run and any(i["provider"] == "ntfy" and i["active"] for i in notifier.status()):
        print("  NOT: konu adini bilen herkes mesaji okuyabilir (bkz. NOTIFICATIONS.md).")
    return EXIT_OK


def _notify_store_path(config: Config) -> Path:
    """Audit file for this run (config.logs_dir/notifications.jsonl)."""
    return Path(config.logs_dir) / "notifications.jsonl"


def cmd_notify_test(args: argparse.Namespace) -> int:
    config = _notify_config(args)
    notifier = build_notifier(config)
    print(SAFETY_BANNER)
    print("")
    print("== notify test {}=={}".format("(DRY-RUN) " if config.notifications.dry_run else "",
                                         "live" if not config.notifications.dry_run else ""))
    records = notifier.test(message=args.message or "")
    delivered = 0
    for record in records:
        status = record.get("status")
        if status in ("sent", "dry_run"):
            delivered += 1
        detail = ""
        if status == "failed":
            detail = " | {} {}".format(record.get("http_status") or "-", record.get("reason") or "")
        elif status == "suppressed":
            detail = " | sebep: {}".format(record.get("reason"))
        elif status == "dry_run" and record.get("target"):
            detail = " | hedef: {}".format(record.get("target"))
        print("  [{}] {:<9} {:<8}{}".format(
            "ok" if status in ("sent", "dry_run") else "!!", record.get("provider"),
            status, detail))
    print("")
    print("  deneme kaydi : {} satir (dosya: {})".format(len(records), _notify_store_path(config)))
    if delivered == 0:
        print("  SONUC: hicbir saglayici mesaj gonderemedi/denemedi.")
        return EXIT_FAILURE
    print("  SONUC: {}".format(
        "dry-run: {} saglayici icin ne gonderilecegi gosterildi".format(delivered)
        if config.notifications.dry_run else
        "{} saglayiciya gonderildi".format(delivered)))
    return EXIT_OK


def cmd_notify_log(args: argparse.Namespace) -> int:
    config = _notify_config(args)
    path = _notify_store_path(config)
    store = NotificationStore(path)
    rows = store.read(limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        return EXIT_OK
    print("== bildirim kaydi (son {} / toplam {}) ==".format(len(rows), store.count()))
    print("  dosya: {}".format(path))
    if not rows:
        print("  (kayit yok; `notify test` calistirin)")
        return EXIT_OK
    header = "{:<20} {:<17} {:<8} {:<9} {:<10} {:<5} {:>8} {}".format(
        "zaman (UTC)", "olay", "sev", "saglayici", "durum", "http", "gecikme", "sebep/hedef")
    print(header)
    print("-" * len(header))
    for row in rows:
        target = row.get("target") or ""
        reason = row.get("reason") or ""
        note = reason if reason else target
        print("{:<20} {:<17} {:<8} {:<9} {:<10} {:<5} {:>7.0f}ms {}".format(
            str(row.get("iso_utc") or "")[:20], str(row.get("event") or "")[:17],
            str(row.get("severity") or "")[:8], str(row.get("provider") or "")[:9],
            str(row.get("status") or "")[:10], str(row.get("http_status") or "-")[:5],
            float(row.get("latency_ms") or 0.0), note[:70]))
    return EXIT_OK


def cmd_notify_export(args: argparse.Namespace) -> int:
    config = _notify_config(args)
    store = NotificationStore(_notify_store_path(config))
    if store.count() == 0:
        print("Kayitli bildirim denemesi yok ({}).".format(_notify_store_path(config)))
        return EXIT_FAILURE
    out = Path(args.out) if args.out else Path(config.reports_dir) / "notifications"
    out.mkdir(parents=True, exist_ok=True)
    written = []
    if args.format in ("csv", "both"):
        written.append(("csv", store.export_csv(out / "notifications.csv")))
    if args.format in ("json", "both"):
        written.append(("json", store.export_json(out / "notifications.json")))
    print("bildirim kaydi disa aktarildi: {} satir".format(store.count()))
    for kind, path in written:
        print("  {:<5} {}".format(kind, path))
    return EXIT_OK


def cmd_notify_preview(args: argparse.Namespace) -> int:
    """Print the exact rendered message for sample events (sends nothing by default)."""
    config = _apply_notify_flags(_load(args), args)
    # A console that cannot encode an emoji must degrade to "?" instead of crashing.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # pragma: no cover - non-reconfigurable stream
        pass

    history = None
    try:
        from .notify.context import HistoryStatsProvider

        # The history block replays the whole cache through the engine, which
        # logs heavily; that noise would drown the preview, so it is silenced
        # for the duration of the measurement only.
        with _quiet_cryptobot_logs():
            stats = HistoryStatsProvider(config).get()
        history = stats.as_dict() if stats is not None else None
    except Exception:  # pragma: no cover - preview must work without stats
        history = None

    selected = preview_events(args.event, config=config, history=history,
                              ts=int(datetime.now(timezone.utc).timestamp() * 1000))
    if not selected:
        print("bilinmeyen olay: {!r}".format(args.event), file=sys.stderr)
        return EXIT_USAGE

    carrier = args.carrier
    print("== notify preview ({} olay · carrier: {}) ==".format(len(selected), carrier))
    print("  config : timeframe={} pairs={} net hedef=%{} stop=%{}".format(
        config.timeframe, ",".join(config.pairs), config.net_profit_target_pct, config.stop_loss_pct))
    if history:
        print("  history: GERCEK backtest -> {} gun · {} · {} · {} islem".format(
            history.get("days"), history.get("timeframe"),
            "/".join(str(p).replace("/", "") for p in history.get("pairs", [])),
            history.get("trades")))
    else:
        print("  history: yok (cache bulunamadi/olculemedi) -> gecmis blogu ATLANIR")
    if not args.send:
        print("  NOT    : hicbir sey GONDERILMEDI (gondermek icin --send)")
    for index, event in enumerate(selected, start=1):
        content = getattr(event, "content", None)
        if content is not None:
            from .notify.render import render_content

            text = render_content(content, carrier)
        else:  # pragma: no cover - every factory event carries content
            from .notify.render import render_body

            text = render_body(event, carrier)
        lines = text.splitlines()
        content_lines = sum(1 for line in lines if line.strip())
        too_long = content_lines > RENDER_MAX_LINES
        print("")
        print("-" * 78)
        print("[{}/{}] {}  ·  severity={}  ·  {} icerik satiri{}".format(
            index, len(selected), event.type, event.resolved_severity(), content_lines,
            "  (UYARI: > {} satir!)".format(RENDER_MAX_LINES) if too_long else ""))
        print("-" * 78)
        print(text)

    if not args.send:
        return EXIT_OK

    print("")
    print("== notify preview --send: gercek gonderim ==")
    notifier = build_notifier(config)
    delivered = failed = 0
    for event in selected:
        records = notifier.notify(event, force=True)
        for record in records:
            status = str(record.get("status"))
            if status in ("sent", "dry_run"):
                delivered += 1
            elif status == "failed":
                failed += 1
            print("  [{}] {:<10} {:<9} {}{}".format(
                "ok" if status in ("sent", "dry_run") else "!!", str(record.get("provider")),
                status, "http={} ".format(record.get("http_status") or "-"),
                str(record.get("reason") or "")[:80]))
    print("  gonderim: {} basarili/denendi, {} basarisiz".format(delivered, failed))
    return EXIT_OK if failed == 0 and delivered > 0 else EXIT_FAILURE


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cryptobot",
        description="Paper-trading (simulation only) crypto bot. Gercek emir yok, API anahtari yok.",
    )
    parser.add_argument("--version", action="version", version="cryptobot {}".format(__version__))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("download", help="public mum verisini indir (anahtar gerekmez)")
    _add_common(p)
    p.add_argument("--days", type=int, default=None, help="kac gunluk gecmis (varsayilan config)")
    p.add_argument("--force", action="store_true", help="cache'i yok say, yeniden indir")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("backtest", help="deterministik backtest calistir ve raporlari yaz")
    _add_common(p)
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--offline", action="store_true", help="sadece cache'ten oku, aga cikma")
    p.add_argument("--no-chart", action="store_true", help="PNG equity grafigi uretme")
    p.add_argument("--out-name", type=str, default=None, help="rapor dosya adi oneki")
    p.add_argument("--run-id", type=str, default=None)
    p.add_argument("--json-stdout", action="store_true", help="metrik JSON'unu stdout'a da yaz")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("run", help="paper trading dongusu (sinirli calistirilabilir)")
    _add_common(p)
    p.add_argument("--cycles", type=int, default=None, help="kac dongu calis (sinir)")
    p.add_argument("--duration-seconds", type=float, default=None, help="azami sure (sinir)")
    p.add_argument("--interval-seconds", type=float, default=None,
                   help="donguler arasi bekleme (varsayilan: replay'de 0, gercek modda 60)")
    p.add_argument("--offline", action="store_true", help="sadece cache'ten oku (aga cikma)")
    p.add_argument("--replay", action="store_true", help="cache uzerinde ileri-yuruyus tekrari (deterministik)")
    p.add_argument("--replay-bars", type=int, default=1, help="replay modunda dongu basina yeni bar")
    p.add_argument("--faults", choices=["none", "demo"], default="none",
                   help="deterministik ariza enjeksiyonu (red/kismi dolum/yetersiz bakiye)")
    p.add_argument("--notify-dry-run", action="store_true",
                   help="bildirimleri GONDERMEDEN ne gonderilecegini goster/kaydet")
    p.add_argument("--notify-send", action="store_true",
                   help="replay/offline kosusunda ag bildirimini ACIKCA ac (varsayilan: kapali)")
    p.add_argument("--no-notify", action="store_true",
                   help="bu kosu icin bildirim katmanini tamamen kapat")
    p.add_argument("--run-id", type=str, default=None)
    p.add_argument("--json-stdout", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="paper run durumunu goster")
    _add_common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="gunluk Markdown raporunu uret")
    _add_common(p)
    p.add_argument("--day", type=str, default=None, help="YYYY-MM-DD veya 'latest'")
    p.add_argument("--run-id", type=str, default=None)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("export", help="ledger'i CSV + JSON olarak disa aktar")
    _add_common(p)
    p.add_argument("--out", type=str, default=None, help="cikti klasoru")
    p.add_argument("--run-id", type=str, default=None)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("verify", help="ledger ile broker bakiyesini mutabakata et")
    _add_common(p)
    p.add_argument("--run-id", type=str, default=None)
    p.add_argument("--tolerance", type=float, default=1e-6, help="mutlak tolerans (USDT)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("stop", help="calisan paper dongusune durma istegi gonder")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("safety", help="guvenlik durumunu yazdir (canli mod yok)")
    p.set_defaults(func=cmd_safety)

    p = sub.add_parser("panel", help="bildirim + islem paneli (tek dosya, cevrimdisi HTML)")
    _add_common(p)
    p.add_argument("--out", type=str, default=None,
                   help="cikti HTML yolu (varsayilan: <reports_dir>/panel/index.html)")
    p.add_argument("--run-id", type=str, default=None, help="yalnizca bu kosuyu goster")
    p.add_argument("--limit", type=int, default=None,
                   help="tablo basina azami satir (varsayilan {}; 0 = sinirsiz)".format(PANEL_DEFAULT_LIMIT))
    p.add_argument("--serve", action="store_true",
                   help="panel'i 127.0.0.1 uzerinde yerel olarak sun (salt okunur; Ctrl+C ile durur)")
    p.add_argument("--port", type=int, default=PANEL_DEFAULT_PORT,
                   help="--serve portu (varsayilan {})".format(PANEL_DEFAULT_PORT))
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("acceptance", help="kabul kriterlerini makine-okunur kanitla dogrula (offline)")
    p.add_argument("--reports-dir", type=Path, default=None,
                   help="cikti klasoru (varsayilan: cryptobot/reports)")
    p.add_argument("--json-stdout", action="store_true", help="acceptance JSON'unu stdout'a da yaz")
    p.set_defaults(func=cmd_acceptance)

    # --- notify: mobile alert layer ------------------------------------------
    p = sub.add_parser("notify", help="mobil bildirim katmani (test/status/log/export/preview)")
    notify_sub = p.add_subparsers(dest="notify_command", required=True)

    def _notify_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--config", type=Path, default=None, help="config.yaml yolu")
        parser.add_argument("--log-level", type=str, default=None, help="DEBUG|INFO|WARNING|ERROR")

    q = notify_sub.add_parser("test", help="etkin saglayicilara canli test bildirimi gonder")
    _notify_common(q)
    q.add_argument("--dry-run", action="store_true", help="gondermeden yalnizca goster/kaydet")
    q.add_argument("--message", type=str, default=None, help="ozel mesaj metni")
    q.set_defaults(func=cmd_notify_test)

    q = notify_sub.add_parser("status", help="yapilandirilmis saglayicilari ve aktiflik nedenini goster")
    _notify_common(q)
    q.set_defaults(func=cmd_notify_status)

    q = notify_sub.add_parser("log", help="son bildirim deneme kayitlarini yazdir")
    _notify_common(q)
    q.add_argument("--limit", type=int, default=20, help="gosterilecek kayit sayisi (varsayilan 20)")
    q.add_argument("--json", action="store_true", help="ham JSON olarak yazdir")
    q.set_defaults(func=cmd_notify_log)

    q = notify_sub.add_parser("export", help="bildirim kayitlarini CSV/JSON olarak disa aktar")
    _notify_common(q)
    q.add_argument("--out", type=str, default=None, help="cikti klasoru (varsayilan reports/notifications)")
    q.add_argument("--format", choices=["csv", "json", "both"], default="both")
    q.set_defaults(func=cmd_notify_export)

    q = notify_sub.add_parser(
        "preview", help="ornek verilerle her olayin tam mesajini yazdir (varsayilan: GONDERMEZ)")
    _notify_common(q)
    q.add_argument("--event", type=str, default=None, choices=list(EVENT_TYPES),
                   help="yalnizca bu olay turunu goster")
    q.add_argument("--all", dest="all_events", action="store_true", help="tum olay turleri (varsayilan)")
    q.add_argument("--carrier", choices=["plain", "markdown", "html", "ntfy", "telegram"],
                   default="plain",
                   help="plain=duz metin, markdown/ntfy=ntfy.govdesi, html/telegram=HTML govde")
    q.add_argument("--send", action="store_true",
                   help="GERCEKTEN gonder (varsayilan: yalnizca gosterir, hicbir sey gondermez)")
    q.set_defaults(func=cmd_notify_preview)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AlreadyRunningError as exc:
        # Raised by PaperRunner.setup() when the check in cmd_run raced a start
        # that won the run directory in the meantime.
        print(str(exc), file=sys.stderr)
        return EXIT_BUSY
    except safety.LiveTradingForbidden as exc:
        print("GUVENLIK IHLALI: {}".format(exc), file=sys.stderr)
        return EXIT_SAFETY
    except ConfigError as exc:
        print("KONFIGURASYON HATASI: {}".format(exc), file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("Kesildi (Ctrl+C).", file=sys.stderr)
        return EXIT_FAILURE
    except FeedError as exc:
        print("VERI HATASI: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return EXIT_FAILURE
    except sqlite3.OperationalError as exc:
        # A held ledger (`database is locked`) must never surface as a traceback:
        # the ledger is shared state and the operator needs the way out, not a
        # stack.  Any other SQLite operational error is still a plain failure.
        if is_ledger_lock_error(exc):
            db_path = getattr(args, "db_path", None) or os.environ.get("CRYPTOBOT_DB_PATH")
            print(ledger_locked_message(exc, db_path=db_path), file=sys.stderr)
            return EXIT_BUSY
        print("LEDGER HATASI: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return EXIT_FAILURE


__all__ = ["main", "build_parser"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
