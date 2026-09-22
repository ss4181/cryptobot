"""One-command full verification of the paper-trading bot.

Steps
-----
1. ``safety``  -- static scan proving no live-order/authenticated code path exists
2. ``tests``   -- the complete offline unittest suite
3. ``data``    -- inventory of the cached Binance candles
4. ``backtest``-- runs the backtest twice and byte-compares the metrics JSON
                  (reproducibility proof), writing the reports to ``cryptobot/reports``
5. ``paper``   -- bounded paper-trading replay, then ledger reconciliation
6. ``ledger``  -- CSV/JSON export and the CLI ``verify`` command

Everything is offline except step 3 when ``--download`` is passed.

The verifier is **safe to run next to a live paper bot**.  By default it points the
ledger (``CRYPTOBOT_DB_PATH``) and the runner run directory (``CRYPTOBOT_RUN_DIR``)
at private temp files, so it neither contends for the shared
``data/ledger.sqlite`` write lock that a running ``paperbot.py run`` holds nor
drops a cooperative stop sentinel into the real ``cryptobot/run/``.  Use
``--db-path`` / ``--run-dir`` to aim it at explicit paths instead.

Usage::

    python cryptobot/scripts/verify_all.py
    python cryptobot/scripts/verify_all.py --timeframe 15m --cycles 20 --replay-bars 400
    python cryptobot/scripts/verify_all.py --quick          # skip tests + paper loop
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from cryptobot import SAFETY_BANNER, __version__  # noqa: E402
from cryptobot.backtest.engine import BacktestEngine  # noqa: E402
from cryptobot.backtest.report import report_basename, write_reports  # noqa: E402
from cryptobot.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from cryptobot.data.feed import cache_path, load_cache  # noqa: E402
from cryptobot.ledger.store import Ledger  # noqa: E402
from cryptobot.notify.guard import enter_harness_mode  # noqa: E402
from cryptobot.runner import PaperRunner, RunnerConfig  # noqa: E402
from cryptobot.tests.no_live_order_scan import scan  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def run_dir_fingerprint(directory: Path) -> Dict[str, Dict[str, Any]]:
    """``{relative name: {size, sha256}}`` for every file in ``directory``.

    Used to prove the verifier leaves the production run directory alone.  A live
    bot legitimately rewrites ``paper_state.json`` between the two snapshots, so a
    difference here is *reported*, never failed on: the authoritative check is the
    in-process write audit in :func:`Verifier.step_tests`.
    """
    fingerprint: Dict[str, Dict[str, Any]] = {}
    if not directory.is_dir():
        return fingerprint
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        try:
            payload = path.read_bytes()
        except OSError:  # a live writer may hold the file; never fail the harness
            continue
        fingerprint[path.relative_to(directory).as_posix()] = {
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return fingerprint


def isolate_harness_paths(args: argparse.Namespace) -> Path:
    """Aim the verifier's ledger + run directory at private temp files.

    Required, not cosmetic: the ambient ``CRYPTOBOT_*`` namespace was already
    stripped when this module imported ``cryptobot.tests`` (see that package's
    ``isolate_environment``), and a live ``paperbot.py run`` holds the write lock
    on the shared ``data/ledger.sqlite``.  Pointing at temp files lets the
    verifier reach 6/6 while that bot keeps running.
    """
    root = Path(tempfile.mkdtemp(prefix="cryptobot-verify-"))
    atexit.register(shutil.rmtree, str(root), ignore_errors=True)
    os.environ["CRYPTOBOT_DB_PATH"] = args.db_path or str(root / "ledger.sqlite")
    os.environ["CRYPTOBOT_RUN_DIR"] = args.run_dir or str(root / "run")
    args.isolated_root = str(root)
    return root


@dataclass
class Step:
    name: str
    status: str = SKIP
    detail: str = ""
    seconds: float = 0.0
    evidence: List[str] = field(default_factory=list)


class Verifier:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.steps: List[Step] = []
        overrides: Dict[str, Any] = {"timeframe": args.timeframe} if args.timeframe else {}
        if args.pairs:
            overrides["pairs"] = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
        self.config = load_config(DEFAULT_CONFIG_PATH, cli_overrides=overrides)
        self.reports_dir = self.config.reports_dir
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.frames: Dict[str, Any] = {}
        self.data_meta: Dict[str, Any] = {}

    # ------------------------------------------------------------------ helpers
    def run_step(self, name: str, fn: Callable[[Step], None]) -> Step:
        step = Step(name=name)
        started = time.time()
        try:
            fn(step)
        except Exception as exc:  # noqa: BLE001 - the verifier reports, never raises
            step.status = FAIL
            step.detail = "{}: {}".format(type(exc).__name__, exc)
            import traceback

            step.evidence.append(traceback.format_exc())
        step.seconds = round(time.time() - started, 2)
        self.steps.append(step)
        print("  [{:<4}] {:<24} {:>6.2f}s  {}".format(step.status, step.name, step.seconds, step.detail))
        return step

    # -------------------------------------------------------------------- steps
    def step_safety(self, step: Step) -> None:
        result = scan()
        step.evidence = result.render().splitlines()
        step.status = PASS if result.ok else FAIL
        step.detail = "{} runtime modules scanned, {} finding(s)".format(
            len(result.scanned_files), len(result.findings))

    def step_tests(self, step: Step) -> None:
        from cryptobot import tests as tests_package  # noqa: F401  (ensures path)

        # The suite must not write the cooperative stop sentinel into the real run
        # directory: a live `paperbot.py run` polls it and would terminate itself.
        real_run_dir = tests_package.real_run_dir()
        fingerprint_before = run_dir_fingerprint(real_run_dir)
        writes_before = len(tests_package.real_run_dir_writes())

        # This harness sets CRYPTOBOT_DB_PATH / CRYPTOBOT_RUN_DIR for its own later
        # steps, and cryptobot.tests ran its one-shot environment strip at import --
        # before those existed.  Re-apply the suite's own isolation contract here so
        # the harness paths cannot change a test outcome (a leaked CRYPTOBOT_DB_PATH
        # makes the "missing ledger" tests see an existing file), then put the
        # harness's own values back for the steps that follow.
        harness_env = {name: os.environ[name] for name in ("CRYPTOBOT_DB_PATH", "CRYPTOBOT_RUN_DIR")
                       if name in os.environ}
        loader = unittest.TestLoader()
        suite = loader.discover(str(WORKSPACE_ROOT / "cryptobot" / "tests"),
                                pattern="test_*.py", top_level_dir=str(WORKSPACE_ROOT))
        buffer = io.StringIO()
        runner = unittest.TextTestRunner(stream=buffer, verbosity=1)
        started = time.time()
        try:
            tests_package.isolate_environment()
            result = runner.run(suite)
        finally:
            os.environ.update(harness_env)
        elapsed = time.time() - started
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        step.detail = "{} tests, {} failures, {} errors, {:.2f}s".format(
            result.testsRun, len(result.failures), len(result.errors), elapsed)
        step.status = PASS if result.wasSuccessful() else FAIL
        step.evidence = lines[-6:]
        if not result.wasSuccessful():
            step.evidence.extend([failure[0].id() for failure in result.failures + result.errors])

        leaked = tests_package.real_run_dir_writes()[writes_before:]
        fingerprint_after = run_dir_fingerprint(real_run_dir)
        step.evidence.append(
            "real run dir {!s}: {} file(s), suite writes={}, unchanged by the suite={}".format(
                real_run_dir, len(fingerprint_after), len(leaked),
                fingerprint_after == fingerprint_before))
        if leaked:
            step.status = FAIL
            step.detail += " | SUITE WROTE THE REAL RUN DIR: {}".format(leaked)

    def step_data(self, step: Step) -> None:
        if self.args.download:
            from cryptobot.data.feed import RequestsTransport, fetch_history

            transport = RequestsTransport()
            for pair in self.config.pairs:
                result = fetch_history(self.config, pair, days=self.args.days, transport=transport)
                print("        downloaded {}: {} rows ({})".format(pair, result.rows, result.source))
        missing = []
        for pair in self.config.pairs:
            path = cache_path(self.config.cache_dir, pair, self.config.timeframe)
            if not path.exists():
                missing.append(str(path))
                continue
            frame = load_cache(path)
            self.frames[pair] = frame
            self.data_meta[pair] = {"source": "cache", "complete": True, "rows": int(len(frame))}
            step.evidence.append("{:<10} {:>6} bars  {} -> {}".format(
                pair, len(frame), int(frame["ts"].iloc[0]), int(frame["ts"].iloc[-1])))
        if missing:
            step.status = FAIL
            step.detail = "missing cache: {}".format(", ".join(missing))
            step.evidence.append("run: python -m cryptobot download --timeframe {}".format(self.config.timeframe))
        else:
            step.status = PASS
            step.detail = "{} pairs, {} bars cached ({})".format(
                len(self.frames), sum(len(f) for f in self.frames.values()), self.config.timeframe)

    def _run_backtest(self, out_dir: Path, basename: str) -> Any:
        ledger = Ledger(self.config.db_path, "verify-{}".format(basename))
        ledger.start_run(mode="backtest", started_at=int(time.time() * 1000), version=__version__,
                         config=self.config.as_dict(), data=self.data_meta)
        try:
            engine = BacktestEngine(self.config, ledger=ledger, run_id="verify-{}".format(basename))
            result = engine.run(self.frames, data_meta=self.data_meta)
            paths = write_reports(result, out_dir, basename=basename,
                                  with_chart=self.args.charts)
            result.report_paths = paths  # type: ignore[attr-defined]
            return result
        finally:
            ledger.close()

    def step_backtest(self, step: Step) -> None:
        if not self.frames:
            step.status = SKIP
            step.detail = "no cached data (run the data step first)"
            return
        basename = report_basename(self.config)
        first = self._run_backtest(self.reports_dir, basename)
        with tempfile.TemporaryDirectory() as tmp:
            second = self._run_backtest(Path(tmp), basename)
            first_json = (self.reports_dir / "{}.json".format(basename)).read_bytes()
            second_json = (Path(tmp) / "{}.json".format(basename)).read_bytes()
            identical = first_json == second_json
        metrics = first.metrics
        step.status = PASS if identical else FAIL
        step.detail = "net {} USDT | {} trades | win {}% | maxDD {}% | hash {}".format(
            metrics["net_pnl_usdt"], metrics["trade_count"], metrics["win_rate_pct"],
            metrics["max_drawdown_pct"], first.determinism_hash[:12])
        step.evidence = [
            "metrics JSON byte-identical across two runs: {}".format(identical),
            "determinism_hash: {}".format(first.determinism_hash),
            "trades={} win_rate={}% net_pnl={} USDT net_pnl_pct={}% max_dd={}% profit_factor={}".format(
                metrics["trade_count"], metrics["win_rate_pct"], metrics["net_pnl_usdt"],
                metrics["net_pnl_pct"], metrics["max_drawdown_pct"], metrics["profit_factor"]),
            "avg net/trade={} USDT sharpe={} fees={} USDT slippage={} USDT".format(
                metrics["avg_net_pnl_per_trade_usdt"], metrics["sharpe_ratio"],
                metrics["total_fees_usdt"], metrics["total_slippage_usdt"]),
            "reports: {}".format(", ".join(sorted(p.name for p in first.report_paths.values()))),
        ]

    def step_paper(self, step: Step) -> None:
        if not self.frames:
            step.status = SKIP
            step.detail = "no cached data"
            return
        run_id = "verify-paper"
        runner = PaperRunner(
            self.config,
            runner=RunnerConfig(cycles=self.args.cycles, offline=True, replay=True,
                                replay_bars_per_cycle=self.args.replay_bars, interval_seconds=0.0),
            run_id=run_id,
            sleep_fn=lambda _s: None,
        )
        summary = runner.run()
        ledger = Ledger(self.config.db_path, run_id)
        try:
            counts = ledger.counts()
            verification = ledger.verify(
                initial_cash=self.config.initial_capital_usdt,
                broker_cash=0.0, broker_realized_net_pnl=0.0,
                broker_equity=None, open_positions={}, mark_prices={},
                tolerance=1.0e9,  # only structural checks here; the runner did the real one
            )
        finally:
            ledger.close()
        step.status = PASS if summary["verification"]["ok"] else FAIL
        step.detail = "cycles={} bars={} trades={} equity={:.4f} reconcile={}".format(
            summary["cycles"], summary["bars_processed"], summary["trades_closed"],
            summary["final_equity"], "PASS" if summary["verification"]["ok"] else "FAIL")
        step.evidence = [
            "ledger rows: {}".format(summary["ledger_counts"]),
            "final equity: {:.6f} USDT | net pnl: {:.6f} USDT ({:.4f}%)".format(
                summary["final_equity"], summary["net_pnl_usdt"] or 0.0, summary["net_pnl_pct"] or 0.0),
            "trading state: {} | halted: {}".format(summary["trading_state"], summary["halted"]),
            "daily report: {}".format(summary["daily_report"]),
            "log file: {}".format(summary["log_file"]),
            "structural ledger checks: {}".format(
                "ok" if all(check["ok"] for check in verification.checks) else "failed"),
        ]
        for check in summary["verification"]["checks"]:
            step.evidence.append("  [{}] {} ledger={} broker={} delta={}".format(
                "ok" if check["ok"] else "FAIL", check["name"], check["ledger"], check["broker"],
                check["delta"]))
        _ = counts

    def step_ledger(self, step: Step) -> None:
        from cryptobot.cli import main as cli_main

        out_dir = self.reports_dir / "exports"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code_export = cli_main(["export", "--out", str(out_dir)])
            code_verify = cli_main(["verify"])
        step.status = PASS if code_export == 0 and code_verify == 0 else FAIL
        step.detail = "export rc={} verify rc={}".format(code_export, code_verify)
        exported = sorted(path.name for path in out_dir.glob("*")) if out_dir.exists() else []
        step.evidence = ["exported: {}".format(", ".join(exported) or "none")]
        tail = [line for line in buffer.getvalue().splitlines() if line.strip()]
        step.evidence.extend(tail[-8:])

    # --------------------------------------------------------------------- main
    def run(self) -> int:
        print(SAFETY_BANNER)
        print("cryptobot verify_all -- config: {}".format(DEFAULT_CONFIG_PATH))
        print("timeframe={} pairs={} initial_capital={} USDT net_target={}% fee={}% slippage={}%".format(
            self.config.timeframe, ",".join(self.config.pairs), self.config.initial_capital_usdt,
            self.config.net_profit_target_pct, self.config.fee_pct, self.config.slippage_pct))
        print("isolated ledger : {}".format(self.config.db_path))
        print("isolated run dir: {}  (temp root {})".format(
            os.environ.get("CRYPTOBOT_RUN_DIR"), getattr(self.args, "isolated_root", "-")))
        print("")

        self.run_step("safety scan", self.step_safety)
        if not self.args.skip_tests:
            self.run_step("unit tests", self.step_tests)
        self.run_step("data cache", self.step_data)
        self.run_step("backtest x2", self.step_backtest)
        if not self.args.skip_paper:
            self.run_step("paper replay", self.step_paper)
        self.run_step("ledger export", self.step_ledger)

        print("")
        print("=" * 78)
        failed = [step for step in self.steps if step.status == FAIL]
        for step in self.steps:
            print("{:<14} {:<4} {:>6.2f}s  {}".format(step.name, step.status, step.seconds, step.detail))
            for line in step.evidence:
                if self.args.verbose or step.status == FAIL:
                    for text in str(line).splitlines():
                        print("               | {}".format(text))
        print("=" * 78)
        print("{} of {} steps passed".format(len(self.steps) - len(failed), len(self.steps)))
        print("summary json: {}".format(self._write_summary()))
        print("handoffDisposition: {}".format(
            "native_fallback_allowed" if not failed else "workspace_action_required"))
        return 0 if not failed else 1

    def _write_summary(self) -> Path:
        payload = {
            "version": __version__,
            "config": self.config.as_dict(),
            "steps": [
                {"name": step.name, "status": step.status, "seconds": step.seconds,
                 "detail": step.detail, "evidence": step.evidence}
                for step in self.steps
            ],
            "ok": all(step.status != FAIL for step in self.steps),
        }
        path = self.reports_dir / "verify_all_summary.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verify_all.py",
                                     description="cryptobot: tek komutla tam dogrulama")
    parser.add_argument("--timeframe", default=None, help="1m|5m|15m|30m|1h|4h|1d (varsayilan: config)")
    parser.add_argument("--pairs", default=None, help="virgulle ayrilmis pariteler")
    parser.add_argument("--cycles", type=int, default=20, help="paper replay dongu sayisi")
    parser.add_argument("--replay-bars", type=int, default=400, help="dongu basina yeni bar")
    parser.add_argument("--days", type=int, default=None, help="--download icin gun sayisi")
    parser.add_argument("--download", action="store_true", help="once public veriyi indir/guncelle")
    parser.add_argument("--charts", action="store_true", help="PNG equity grafigi de uret")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-paper", action="store_true")
    parser.add_argument("--quick", action="store_true", help="testleri ve paper dongusunu atla")
    parser.add_argument("--verbose", action="store_true", help="tum kanit satirlarini yazdir")
    parser.add_argument("--db-path", default=None,
                        help="ledger dosyasi (varsayilan: izole gecici dosya)")
    parser.add_argument("--run-dir", default=None,
                        help="runner run dizini (varsayilan: izole gecici dizin)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Structural network guarantee: this harness can never publish to a real
    # notification host, even with a persistent CRYPTOBOT_NTFY_TOPIC in the
    # environment (see cryptobot/notify/guard.py).
    enter_harness_mode()
    if args.quick:
        args.skip_tests = True
        args.skip_paper = True
    # Isolate the ledger + run directory BEFORE the config is loaded, so a live bot
    # holding data/ledger.sqlite (and owning cryptobot/run/) cannot make steps fail.
    isolate_harness_paths(args)
    # Quieten the verifier's own output without using logging.disable(), which is a
    # global switch that would also silence the monitoring tests' log assertions.
    logging.getLogger().setLevel(logging.CRITICAL + 1)
    logging.getLogger("cryptobot").setLevel(logging.CRITICAL + 1)
    return Verifier(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
