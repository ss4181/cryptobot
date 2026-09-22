"""Acceptance verification harness: one command, machine-checkable evidence.

This harness proves the project's acceptance criteria end to end and writes two
artifacts::

    cryptobot/reports/acceptance.json   -- stable, deterministic, machine-readable
    cryptobot/reports/acceptance.md     -- the same result in human Turkish

Exit code is ``0`` **only** when every criterion and every individual check
passes; any failure (or an unexpected exception while measuring) yields ``1``.
There is no "pass by default": a criterion with zero recorded checks is FAIL.

Design rules
------------
* **Offline** -- no network call anywhere.  Cached candles under
  ``cryptobot/data/cache`` are copied into a private temp cache and reused; all
  HTTP paths are driven through scripted fake transports against the real
  ``data/feed.py``.
* **Deterministic** -- no ``random``, no wall-clock in the compared payload, no
  temp paths inside the payload (they differ between runs).  Two consecutive
  runs therefore produce a byte-identical ``acceptance.json``.
* **No side effects on the project** -- the real ``config.yaml``,
  ``data/ledger.sqlite``, ``reports/`` (other than the two artifacts) and
  existing tests are never modified.  All scratch state lives in a temp dir.

It is also reachable as a CLI subcommand::

    python cryptobot/scripts/paperbot.py acceptance
    python -m cryptobot acceptance
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# path bootstrap (same trick as scripts/paperbot.py and scripts/verify_all.py)
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve()
PACKAGE_ROOT = HERE.parents[1]
WORKSPACE_ROOT = HERE.parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

import yaml  # noqa: E402

from cryptobot import __version__, safety  # noqa: E402
from cryptobot.backtest.engine import TRADING_FAIL_SAFE, BacktestEngine  # noqa: E402
from cryptobot.backtest.report import write_reports  # noqa: E402
from cryptobot.cli import main as cli_main  # noqa: E402
from cryptobot.config import DEFAULT_CONFIG_PATH, Config, load_config  # noqa: E402
from cryptobot.data.feed import (  # noqa: E402
    CacheCorrupted,
    FeedMalformedResponse,
    FeedTimeout,
    HttpResponse,
    cache_path,
    fetch_history,
    fetch_klines_range,
    load_cache,
    load_candles,
)
from cryptobot.execution.costs import (  # noqa: E402
    required_gross_tp_pct,
    required_tp_price,
    stop_price_from_pct,
)
from cryptobot.execution.paper_broker import (  # noqa: E402
    PARTIAL,
    FaultInjector,
    PaperBroker,
)
from cryptobot.ledger.store import Ledger  # noqa: E402
from cryptobot.risk.manager import (  # noqa: E402
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    RiskManager,
)
from cryptobot.notify.guard import HARNESS_ENV, enter_harness_mode  # noqa: E402
from cryptobot.runner import PaperRunner, RunnerConfig  # noqa: E402
from cryptobot.tests.fixtures import (  # noqa: E402
    HOUR_MS,
    START_TS,
    FakeTransport,
    kline_row,
    klines_body,
    make_frame,
    seed_cache,
)
from cryptobot.tests.no_live_order_scan import scan, scan_file  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SAFETY = 3

#: Fixed market timestamp used by the pure risk-manager checks (no wall clock).
TS = 1_700_000_000_000
DAY_MS = 86_400_000

#: Criterion id -> (human Turkish label, evidence kinds).
CRITERIA_SPEC: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("paper-mode-only", "Yalnizca paper/backtest modu; canli emir yolu yok", ("check", "operation")),
    ("trade-ledger-persistence", "Islem defteri kaliciligi ve mutabakat (delta 0.0)", ("operation", "data", "file")),
    ("profit-target-logic", "Net kar hedefinden brut take-profit turetimi", ("check", "operation")),
    ("risk-controls-active", "Risk kontrolleri aktif, bloklayici ve kod uretiyor", ("check", "operation")),
    ("backtest-report", "Backtest raporlari (JSON/MD) uretiliyor", ("operation", "file", "data")),
    ("failure-path-handling", "Hata yollari guvenli (fail-safe, offline)", ("check", "operation")),
    ("config-and-run-docs", "Konfigurasyon ve calistirma dokumantasyonu", ("file", "check")),
    ("reproducible-results", "Tekrarlanabilir sonuclar (bayt-ayni metrik JSON)", ("operation", "file", "data")),
    ("deployment-handover", "Devir teslim / isletme runbook'u ve dagitim dosyalari", ("file",)),
    ("risk-disclosure", "Risk aciklamalari (kar garantisi yok, tavsiye degildir)", ("file", "check")),
)

REQUIRED_DOC_FILES: Tuple[str, ...] = (
    "README.md",
    "RISK.md",
    "HANDOVER.md",
    "config.yaml",
    ".env.example",
    "Dockerfile",
    "requirements.txt",
    ".github/workflows/paper-trade.yml",
)

TURKISH_CHARS = frozenset("şğıİöüçŞĞÖÜÇ")

#: Broker / feed failure codes that must be persisted and covered by tests.
PERSISTED_TEST_TOKENS: Tuple[str, ...] = (
    "synthetic_rejection",
    "no_open_position",
    "below_min_notional",
    "insufficient_balance",
    "FeedTimeout",
    "FeedMalformedResponse",
    "CacheCorrupted",
    "cache-stale",
    "daily_loss_limit_reached",
    "cooldown_active",
)


# --------------------------------------------------------------------------- #
# result recording
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    name: str
    command_or_target: str
    exit_status: int
    observed: Any
    expected: Any
    ok: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "command_or_target": self.command_or_target,
            "exit_status": int(self.exit_status),
            "observed": self.observed,
            "expected": self.expected,
            "ok": bool(self.ok),
        }


class Criterion:
    """Collects the checks proving one acceptance criterion."""

    def __init__(self, cid: str, label: str, evidence_kinds: Sequence[str]) -> None:
        self.id = cid
        self.label = label
        self.evidence_kinds = list(evidence_kinds)
        self.checks: List[CheckResult] = []

    def add(self, name: str, command_or_target: str, expected: Any, observed: Any,
            ok: bool, *, exit_status: Optional[int] = None) -> bool:
        self.checks.append(CheckResult(
            name=name,
            command_or_target=str(command_or_target),
            exit_status=int(exit_status) if exit_status is not None else (0 if ok else 1),
            observed=observed,
            expected=expected,
            ok=bool(ok),
        ))
        return bool(ok)

    @property
    def status(self) -> str:
        # Never "pass by default": no checks means the criterion is not proven.
        if self.checks and all(check.ok for check in self.checks):
            return PASS
        return FAIL

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "evidence_kinds": list(self.evidence_kinds),
            "checks": [check.as_dict() for check in self.checks],
        }


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
class AcceptanceHarness:
    """Runs every required check offline and writes the two artifacts."""

    def __init__(self, reports_dir: Path) -> None:
        # Structural network guarantee: the acceptance harness itself (and any
        # subprocess it spawns, via the cleaned env below) must be unable to
        # publish to a real notification host.  See cryptobot/notify/guard.py.
        enter_harness_mode()
        self.reports_dir = Path(reports_dir)
        self.root = Path(tempfile.mkdtemp(prefix="cryptobot-acceptance-"))
        self.cache_dir = self.root / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.results: Dict[str, Criterion] = {}

        # Never let a stray CRYPTOBOT_* env var change what we measure.
        self._clean_env = {k: v for k, v in os.environ.items() if not k.startswith("CRYPTOBOT_")}
        self._clean_env["CRYPTOBOT_RUN_DIR"] = str(self.root / "run")
        self._clean_env["PYTHONIOENCODING"] = "utf-8"
        # Re-arm the guard inside every CLI subprocess this harness spawns.
        self._clean_env[HARNESS_ENV] = "1"

        # The shipped config.yaml is the source of truth for the defaults we assert.
        self.base_config = load_config(DEFAULT_CONFIG_PATH, environ={})

        # Reuse the cached candles (offline, deterministic).
        source_cache = PACKAGE_ROOT / "data" / "cache"
        for csv in sorted(source_cache.glob("*.csv")):
            shutil.copy2(csv, self.cache_dir / csv.name)

        self._paper: Optional[Dict[str, Any]] = None
        self._backtests: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------ configs/paths
    def _tmp_config(self, *, cache_dir: Optional[Path] = None, db_name: str = "ledger.sqlite") -> Config:
        data = dataclasses.replace(
            self.base_config.data,
            cache_dir=Path(cache_dir or self.cache_dir),
            db_path=self.root / "data" / db_name,
        )
        return dataclasses.replace(
            self.base_config,
            data=data,
            logging=dataclasses.replace(self.base_config.logging, dir=self.root / "logs"),
            reports_dir=self.root / "reports",
        )

    @contextlib.contextmanager
    def _run_dir_env(self):
        """Point the runner's pid/state/stop files at the temp dir, then restore."""
        previous = os.environ.get("CRYPTOBOT_RUN_DIR")
        os.environ["CRYPTOBOT_RUN_DIR"] = str(self.root / "run")
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("CRYPTOBOT_RUN_DIR", None)
            else:
                os.environ["CRYPTOBOT_RUN_DIR"] = previous

    def _run_cli(self, argv: Sequence[str], *, env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        """Run the real CLI in a subprocess; return (exit_code, combined_output)."""
        completed = subprocess.run(
            [sys.executable, str(PACKAGE_ROOT / "scripts" / "paperbot.py"), *argv],
            cwd=str(WORKSPACE_ROOT),
            env=env or self._clean_env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return int(completed.returncode), (completed.stdout or "") + (completed.stderr or "")

    @staticmethod
    def _make_manager(config: Config, **overrides: Any) -> RiskManager:
        kwargs: Dict[str, Any] = dict(
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
        kwargs.update(overrides)
        return RiskManager(**kwargs)

    @staticmethod
    def _entry(manager: RiskManager, *, ts: int = TS, price: float = 100.0, equity: float = 50.0,
               cash: float = 50.0, open_positions: int = 0):
        return manager.evaluate_entry(ts=ts, price=price, equity=equity, cash=cash,
                                      open_positions=open_positions)

    # ------------------------------------------------------------------ A. mode
    def check_paper_mode_only(self, c: Criterion) -> None:
        scan_result = scan()
        c.add(
            "AST taramasi: runtime modullerinde canli emir / imzali istek yolu yok",
            "cryptobot/tests/no_live_order_scan.py::scan",
            {"module_count": ">=18", "finding_count": 0},
            {"module_count": len(scan_result.scanned_files), "finding_count": len(scan_result.findings)},
            ok=(len(scan_result.scanned_files) >= 18 and not scan_result.findings),
        )

        # Negative control: a broken scanner must not be able to pass silently.
        planted = (
            "import requests\n"
            "def boom(client):\n"
            "    return client.create_order('BTC/USDT', 'market', 'buy', 1)\n"
            "def leak():\n"
            "    return requests.post('https://api.binance.com/sapi/v1/order', json={})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            planted_path = Path(tmp) / "planted.py"
            planted_path.write_text(planted, encoding="utf-8")
            findings = scan_file(planted_path, "planted.py")
        rules = sorted({str(f["rule"]) for f in findings})
        c.add(
            "Negatif kontrol: taranan sahte canli-emir kodu ISARETLENMELI",
            "cryptobot/tests/no_live_order_scan.py::scan_file(<tmp>/planted.py)",
            {"finding_count": ">=1", "rules_include": ["attribute", "http-write", "credential-string"]},
            {"finding_count": len(findings), "rules": rules},
            ok=bool(findings) and {"attribute", "http-write", "credential-string"}.issubset(set(rules)),
        )

        args = ["backtest", "--offline", "--no-chart", "--quiet"]
        for mode in ("live", "real", "production"):
            code, blob = self._run_cli([*args, "--mode", mode])
            ok = code == EXIT_SAFETY and "GUVENLIK IHLALI" in blob
            c.add(
                "CLI --mode {} reddedildi".format(mode),
                "paperbot.py backtest --mode {} --offline".format(mode),
                {"exit_status": EXIT_SAFETY, "violation_message": True},
                {"exit_status": code, "violation_message": "GUVENLIK IHLALI" in blob,
                 "mode_mentioned": mode in blob},
                ok, exit_status=code,
            )

        for label, extra_env in (("CRYPTOBOT_MODE=live", {"CRYPTOBOT_MODE": "live"}),
                                 ("CRYPTOBOT_LIVE=1", {"CRYPTOBOT_LIVE": "1"})):
            env = dict(self._clean_env)
            env.update(extra_env)
            code, blob = self._run_cli(args, env=env)
            ok = code == EXIT_SAFETY and "GUVENLIK IHLALI" in blob
            c.add(
                "Ortam degiskeni {} reddedildi".format(label),
                "paperbot.py backtest (env {})".format(label),
                {"exit_status": EXIT_SAFETY, "violation_message": True},
                {"exit_status": code, "violation_message": "GUVENLIK IHLALI" in blob},
                ok, exit_status=code,
            )

        # A crafted config file cannot select live mode either.
        live_yaml = self.root / "live-mode.yaml"
        live_yaml.write_text("mode: live\n", encoding="utf-8")
        raised: Optional[str] = None
        try:
            load_config(live_yaml, environ={})
        except safety.LiveTradingForbidden:
            raised = "LiveTradingForbidden"
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            raised = type(exc).__name__
        c.add(
            "Gecici config (mode: live) yuklenmesi LiveTradingForbidden firlatir",
            "config.py::load_config(<tmp>/live-mode.yaml)",
            "LiveTradingForbidden",
            raised,
            ok=(raised == "LiveTradingForbidden"),
        )
        code, blob = self._run_cli(["backtest", "--config", str(live_yaml), "--offline", "--no-chart", "--quiet"])
        c.add(
            "Gecici config (mode: live) ile CLI cikisi 3 (safety)",
            "paperbot.py backtest --config <tmp>/live-mode.yaml",
            {"exit_status": EXIT_SAFETY},
            {"exit_status": code, "violation_message": "GUVENLIK IHLALI" in blob},
            ok=(code == EXIT_SAFETY and "GUVENLIK IHLALI" in blob), exit_status=code,
        )

        # Endpoint whitelist: public klines accepted, private/foreign rejected.
        urls = (
            ("https://api.binance.com/api/v3/klines", "accepted"),
            ("https://api.binance.com/sapi/v1/order", "rejected"),
            ("https://evil.example.com/api/v3/klines", "rejected"),
            ("https://api1.binance.com.evil/sapi/v1/account", "rejected"),
        )
        outcomes: Dict[str, str] = {}
        for url, _expected in urls:
            try:
                safety.assert_public_endpoint(url)
                outcomes[url] = "accepted"
            except safety.SafetyViolation:
                outcomes[url] = "rejected"
        expected_map = {url: expected for url, expected in urls}
        c.add(
            "assert_public_endpoint: yalnizca herkese acik klines kabul edilir",
            "safety.py::assert_public_endpoint",
            expected_map,
            outcomes,
            ok=(outcomes == expected_map),
        )

        # Credentials present in the environment are ignored and never read.
        creds = {"BINANCE_API_KEY": "acceptance-key-not-used",
                 "CRYPTOBOT_API_SECRET": "acceptance-secret-not-used"}
        previous = {name: os.environ.get(name) for name in creds}
        try:
            os.environ.update(creds)
            found = safety.audit_credentials()
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                safety_code = cli_main(["safety"])
            out = buffer.getvalue()
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        with_creds = load_config(DEFAULT_CONFIG_PATH, environ={**creds}).as_dict()
        without_creds = load_config(DEFAULT_CONFIG_PATH, environ={}).as_dict()
        reported = all(name in out for name in creds) and "kullanilmaz" in out
        ignored = set(found) >= set(creds)
        unchanged = with_creds == without_creds
        c.add(
            "Kimlik ortam degiskenleri sadece 'kullanilmaz' olarak bildirilir, config'i degistirmez",
            "safety.py::audit_credentials + cli safety + load_config",
            {"reported_ignored": True, "audit_finds_both": True, "config_unchanged": True,
             "cli_exit_status": EXIT_OK},
            {"reported_ignored": reported, "audit_finds_both": ignored, "config_unchanged": unchanged,
             "cli_exit_status": safety_code},
            ok=(reported and ignored and unchanged and safety_code == EXIT_OK), exit_status=safety_code,
        )

        # Bounded offline paper cycle starts at exactly the virtual capital.
        paper = self._paper_cycle()
        start = paper["start"]
        c.add(
            "Sinirli offline paper turu tam olarak sanal sermaye (50.0 USDT) ile baslar",
            "runner.py::PaperRunner(setup) broker baslangic durumu",
            {"initial_capital_usdt": self.base_config.initial_capital_usdt,
             "initial_cash": 50.0, "starting_cash": 50.0, "starting_equity": 50.0},
            start,
            ok=(start["initial_capital_usdt"] == 50.0 and start["initial_cash"] == 50.0
                and start["starting_cash"] == 50.0 and start["starting_equity"] == 50.0),
        )

        # No real-funds / order-submission side effect exists.
        summary = paper["summary"]
        cfg = paper["config"]
        run_mode = self._run_mode(cfg.db_path, "acceptance-paper")
        order_statuses = self._order_statuses(cfg.db_path, "acceptance-paper")
        ok = (summary.get("mode") == "paper" and run_mode == "paper" and not scan_result.findings
              and set(order_statuses) <= {"rejected", "partial"})
        c.add(
            "Gercek fon / emir gonderimi yan etkisi yok (tum durum 'paper', yalniz simule dolum)",
            "ledger runs table + paper broker + AST scan",
            {"run_mode": "paper", "ast_findings": 0, "order_statuses": ["partial", "rejected"]},
            {"run_mode": run_mode, "ast_findings": len(scan_result.findings),
             "order_statuses": sorted(order_statuses)},
            ok,
        )

    # ------------------------------------------------------------- B. risk
    def check_risk_controls_active(self, c: Criterion) -> None:
        cfg = self.base_config
        fee, slip = cfg.fee_pct, cfg.slippage_pct

        manager = self._make_manager(cfg)
        expected_gross = required_gross_tp_pct(cfg.net_profit_target_pct, fee, slip)
        c.add(
            "Turetilen brut TP == costs.required_gross_tp_pct(net_target)",
            "risk/manager.py::RiskManager.gross_tp_pct vs execution/costs.py",
            round(expected_gross, 12),
            round(manager.gross_tp_pct, 12),
            ok=(abs(manager.gross_tp_pct - expected_gross) <= 1e-12),
        )
        higher = self._make_manager(cfg, net_profit_target_pct=cfg.net_profit_target_pct + 1.0)
        expected_higher = required_gross_tp_pct(cfg.net_profit_target_pct + 1.0, fee, slip)
        c.add(
            "net_profit_target_pct degisince turetilen brut TP de degisir",
            "risk/manager.py::RiskManager.gross_tp_pct (net +1.0)",
            {"gross_net_x": round(expected_gross, 12), "gross_net_x_plus_1": round(expected_higher, 12),
             "changed": True},
            {"gross_net_x": round(manager.gross_tp_pct, 12),
             "gross_net_x_plus_1": round(higher.gross_tp_pct, 12),
             "changed": higher.gross_tp_pct != manager.gross_tp_pct},
            ok=(higher.gross_tp_pct != manager.gross_tp_pct
                and abs(higher.gross_tp_pct - expected_higher) <= 1e-12),
        )

        # stop-loss derivation + an actual position close at the stop
        decision = self._entry(manager)
        expected_stop = stop_price_from_pct(decision.entry_fill_estimate, cfg.stop_loss_pct)
        c.add(
            "Stop-loss seviyesi giris fill fiyatindan turetilir",
            "risk/manager.py::evaluate_entry -> stop_price",
            round(expected_stop, 12),
            round(decision.stop_price, 12),
            ok=(decision.approved and abs(decision.stop_price - expected_stop) <= 1e-12),
        )
        stop_trade = self._realise_exit(cfg, decision, trigger=EXIT_STOP_LOSS)
        c.add(
            "Stop-loss seviyesinde gercek pozisyon kapanisi net zarar uretir",
            "paper_broker.py::sell(stop_price) + risk.check_exit",
            {"trigger": EXIT_STOP_LOSS, "status": "filled", "net_pnl_sign": "negative"},
            {"trigger": stop_trade["trigger"], "status": stop_trade["status"],
             "net_pnl": round(stop_trade["net_pnl"], 10),
             "exit_reason_prefix": stop_trade["exit_reason"].split(":")[0]},
            ok=(stop_trade["trigger"] == EXIT_STOP_LOSS and stop_trade["status"] == "filled"
                and stop_trade["net_pnl"] < 0.0
                and stop_trade["exit_reason"].startswith("stop_loss")),
        )

        # take-profit derivation + an actual position close at the target
        expected_tp = required_tp_price(decision.entry_fill_estimate, cfg.net_profit_target_pct, fee, slip)
        c.add(
            "Take-profit seviyesi net hedeften turetilir",
            "risk/manager.py::evaluate_entry -> tp_price (costs.required_tp_price)",
            round(expected_tp, 12),
            round(decision.tp_price, 12),
            ok=(abs(decision.tp_price - expected_tp) <= 1e-12),
        )
        tp_trade = self._realise_exit(cfg, decision, trigger=EXIT_TAKE_PROFIT)
        c.add(
            "Take-profit seviyesinde kapanis net %hedefe ulasir (net_pnl_pct ~= 2.0)",
            "paper_broker.py::sell(tp_price) + risk.check_exit",
            {"trigger": EXIT_TAKE_PROFIT, "net_pnl_sign": "positive",
             "net_pnl_pct": round(cfg.net_profit_target_pct, 6)},
            {"trigger": tp_trade["trigger"], "status": tp_trade["status"],
             "net_pnl": round(tp_trade["net_pnl"], 10),
             "net_pnl_pct": round(tp_trade["net_pnl_pct"], 6)},
            ok=(tp_trade["trigger"] == EXIT_TAKE_PROFIT and tp_trade["net_pnl"] > 0.0
                and abs(tp_trade["net_pnl_pct"] - cfg.net_profit_target_pct) < 0.01),
        )

        # max_position_pct sizing bound
        sized = self._make_manager(cfg, max_position_pct=50.0)
        sized_decision = self._entry(sized)
        c.add(
            "max_position_pct boyut siniri uygulanir",
            "risk/manager.py::evaluate_entry (max_position_pct=50, equity=50)",
            {"approved": True, "notional": 25.0},
            {"approved": sized_decision.approved, "code": sized_decision.code,
             "notional": round(sized_decision.notional, 10)},
            ok=(sized_decision.approved and abs(sized_decision.notional - 25.0) <= 1e-9),
        )

        # every blocking limit: exact code + a second entry is still blocked
        def blocked_pair(manager: RiskManager, code: str, **entry_kwargs: Any) -> Dict[str, Any]:
            first = self._entry(manager, **entry_kwargs)
            second = self._entry(manager, **entry_kwargs)
            return {"first_code": first.code, "first_approved": first.approved,
                    "second_code": second.code, "second_approved": second.approved,
                    "ok": (first.code == code and not first.approved
                           and second.code == code and not second.approved)}

        m_open = self._make_manager(cfg)
        p_open = blocked_pair(m_open, "max_open_positions_reached", open_positions=cfg.max_open_positions)
        c.add(
            "max_open_positions_reached kodu uretilir ve yeni giris bloklanir",
            "risk/manager.py::evaluate_entry(open_positions=max)",
            {"first_code": "max_open_positions_reached", "second_code": "max_open_positions_reached"},
            {k: v for k, v in p_open.items() if k != "ok"}, p_open["ok"],
        )

        m_loss = self._make_manager(cfg, daily_loss_limit_pct=cfg.daily_loss_limit_pct)
        m_loss.roll_day(TS, cfg.initial_capital_usdt)
        m_loss.register_trade_result(-(cfg.initial_capital_usdt * cfg.daily_loss_limit_pct / 100.0) - 0.1, TS + 1000)
        p_loss = blocked_pair(m_loss, "daily_loss_limit_reached", ts=TS + 2000, equity=47.0, cash=47.0)
        halted = m_loss.equity_halted()
        c.add(
            "daily_loss_limit_reached kodu uretilir, halted=True olur ve yeni giris bloklanir",
            "risk/manager.py::register_trade_result -> _check_daily_loss -> evaluate_entry",
            {"halted": True, "first_code": "daily_loss_limit_reached",
             "second_code": "daily_loss_limit_reached"},
            {"halted": halted, "halt_reason_present": bool(m_loss.state.halt_reason),
             **{k: v for k, v in p_loss.items() if k != "ok"}},
            ok=(halted and bool(m_loss.state.halt_reason) and p_loss["ok"]),
        )

        m_trades = self._make_manager(cfg, max_trades_per_day=1)
        m_trades.note_entry_taken(TS, "BTC/USDT")
        p_trades = blocked_pair(m_trades, "max_trades_per_day_reached")
        c.add(
            "max_trades_per_day_reached kodu uretilir ve yeni giris bloklanir",
            "risk/manager.py::note_entry_taken -> evaluate_entry",
            {"first_code": "max_trades_per_day_reached", "second_code": "max_trades_per_day_reached"},
            {k: v for k, v in p_trades.items() if k != "ok"}, p_trades["ok"],
        )

        m_cool = self._make_manager(cfg, cooldown_minutes=60.0)
        m_cool.register_trade_result(-1.0, TS, pair="BTC/USDT")
        p_cool = blocked_pair(m_cool, "cooldown_active", ts=TS + 60_000)
        expires = self._entry(m_cool, ts=TS + 61 * 60_000)
        c.add(
            "Zararli islem sonrasi cooldown_active kodu uretilir, sure dolunca acilir",
            "risk/manager.py::register_trade_result -> evaluate_entry",
            {"during_cooldown": "cooldown_active", "after_cooldown": "approved"},
            {"during_cooldown": p_cool["first_code"], "second_code": p_cool["second_code"],
             "after_cooldown": expires.code},
            ok=(p_cool["ok"] and expires.code == "approved"),
        )

        m_min = self._make_manager(cfg, min_equity_usdt=0.0)
        p_min = blocked_pair(m_min, "below_min_notional", equity=5.0, cash=4.0)
        c.add(
            "below_min_notional kodu uretilir ve yeni giris bloklanir",
            "risk/manager.py::evaluate_entry (kucuk equity/cash)",
            {"first_code": "below_min_notional", "second_code": "below_min_notional"},
            {k: v for k, v in p_min.items() if k != "ok"}, p_min["ok"],
        )

        m_eq = self._make_manager(cfg, min_equity_usdt=cfg.initial_capital_usdt + 10.0)
        p_eq = blocked_pair(m_eq, "equity_below_minimum")
        c.add(
            "equity_below_minimum kodu uretilir ve yeni giris bloklanir",
            "risk/manager.py::evaluate_entry (equity < min_equity_usdt)",
            {"first_code": "equity_below_minimum", "second_code": "equity_below_minimum"},
            {k: v for k, v in p_eq.items() if k != "ok"}, p_eq["ok"],
        )

        both = self._position_and_manager(cfg)
        both_exit = both["manager"].check_exit(
            both["position"], high=both["position"].tp_price + 5.0,
            low=both["position"].stop_price - 5.0, close=both["position"].entry_reference_price,
        )
        c.add(
            "Tek bar hem stop hem TP'ye degerse STOP kazanir (kotumser varsayim)",
            "risk/manager.py::RiskManager.check_exit",
            {"trigger": EXIT_STOP_LOSS, "reference_price": round(both["position"].stop_price, 12)},
            {"trigger": both_exit.trigger if both_exit else None,
             "reference_price": round(both_exit.reference_price, 12) if both_exit else None},
            ok=(both_exit is not None and both_exit.trigger == EXIT_STOP_LOSS
                and abs(both_exit.reference_price - both["position"].stop_price) <= 1e-12),
        )

    # ------------------------------------------------------ C. failure paths
    def check_failure_path_handling(self, c: Criterion) -> None:
        cfg = self._tmp_config(cache_dir=self.root / "cache-synth", db_name="failure.sqlite")

        # repeated timeout -> bounded backoff -> FeedTimeout
        sleeps: List[float] = []
        transport = FakeTransport([FeedTimeout("t{}".format(i)) for i in range(10)])
        raised: Optional[str] = None
        try:
            fetch_klines_range(
                api_base="https://api.binance.com", symbol="BTCUSDT", interval="1h",
                start_ms=START_TS, end_ms=START_TS + 3 * HOUR_MS, transport=transport,
                timeout=5.0, max_retries=5, backoff_initial=1.0, backoff_max=30.0, sleep=sleeps.append,
            )
        except FeedTimeout:
            raised = "FeedTimeout"
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
        c.add(
            "Tekrarlanan timeout: sinirli backoff sonrasi FeedTimeout",
            "data/feed.py::fetch_klines_range (FakeTransport: 10x FeedTimeout)",
            {"raised": "FeedTimeout", "request_count": 6, "backoff_sleeps": [1.0, 2.0, 4.0, 8.0, 16.0]},
            {"raised": raised, "request_count": transport.call_count,
             "backoff_sleeps": [round(s, 6) for s in sleeps]},
            ok=(raised == "FeedTimeout" and transport.call_count == 6
                and sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]),
        )

        # malformed JSON body -> retries -> FeedMalformedResponse
        bad = FakeTransport([HttpResponse(200, "<html>not-json</html>") for _ in range(8)])
        raised = None
        try:
            fetch_klines_range(
                api_base="https://api.binance.com", symbol="BTCUSDT", interval="1h",
                start_ms=START_TS, end_ms=START_TS + 3 * HOUR_MS, transport=bad,
                timeout=5.0, max_retries=3, backoff_initial=1.0, backoff_max=30.0, sleep=lambda _s: None,
            )
        except FeedMalformedResponse:
            raised = "FeedMalformedResponse"
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
        c.add(
            "Bozuk JSON govdesi: tekrar denemeler sonrasi FeedMalformedResponse",
            "data/feed.py::fetch_klines_range (FakeTransport: HTML body, max_retries=3)",
            {"raised": "FeedMalformedResponse", "request_count": 4},
            {"raised": raised, "request_count": bad.call_count},
            ok=(raised == "FeedMalformedResponse" and bad.call_count == 4),
        )

        # outage with an existing cache -> cache-stale, and the engine refuses new entries
        frame_btc = make_frame(800, step_ms=HOUR_MS, make_entries=True, seed=101)
        frame_eth = make_frame(800, step_ms=HOUR_MS, start_price=2_000.0, make_entries=True, seed=102)
        seed_cache(cfg, "BTC/USDT", frame_btc)
        seed_cache(cfg, "ETH/USDT", frame_eth)

        outage = FakeTransport([FeedTimeout("down") for _ in range(40)])
        stale = fetch_history(cfg, "BTC/USDT", days=5, transport=outage, sleep=lambda _s: None,
                             end_ts_ms=int(frame_btc["ts"].iloc[-1]) + HOUR_MS)
        frames = {pair: load_cache(cache_path(cfg.cache_dir, pair, cfg.timeframe)) for pair in cfg.pairs}
        stale_meta = {pair: {"source": stale.source, "complete": stale.complete, "rows": stale.rows}
                      for pair in cfg.pairs}
        engine = BacktestEngine(cfg)
        engine.process(frames, data_meta=stale_meta)
        blocked = (engine.trading_state == TRADING_FAIL_SAFE
                   and engine.broker.open_positions == 0
                   and engine.broker.stats["submitted"] == 0)
        # Control: identical candles marked complete WOULD have produced entries,
        # so the fail-safe (not a lack of signal) is what blocked them.
        control = BacktestEngine(cfg)
        control.process(frames, data_meta={pair: {"source": "cache", "complete": True, "rows": 800}
                                           for pair in cfg.pairs})
        control_entered = control.broker.stats["submitted"] > 0
        c.add(
            "Kesinti + mevcut cache: complete=False/cache-stale, motor yeni giris acmaz",
            "data/feed.py::fetch_history + backtest/engine.py fail-safe",
            {"source": "cache-stale", "complete": False, "trading_state": TRADING_FAIL_SAFE,
             "entries_blocked": True, "control_would_have_entered": True},
            {"source": stale.source, "complete": stale.complete, "trading_state": engine.trading_state,
             "broker_submitted": engine.broker.stats["submitted"], "entries_blocked": blocked,
             "control_would_have_entered": control_entered},
            ok=(stale.source == "cache-stale" and stale.complete is False and blocked and control_entered),
        )

        # missing cache while offline -> CacheCorrupted
        empty = self.root / "empty-cache"
        empty.mkdir(parents=True, exist_ok=True)
        empty_cfg = self._tmp_config(cache_dir=empty, db_name="empty.sqlite")
        raised = None
        try:
            load_candles(empty_cfg, "BTC/USDT", allow_network=False)
        except CacheCorrupted:
            raised = "CacheCorrupted"
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
        c.add(
            "Offline + cache yok: CacheCorrupted (fail-closed)",
            "data/feed.py::load_candles(allow_network=False)",
            "CacheCorrupted", raised, ok=(raised == "CacheCorrupted"),
        )

        # corrupt cache file -> quarantine + rebuild
        corrupt_path = cache_path(cfg.cache_dir, "BTC/USDT", cfg.timeframe)
        corrupt_path.write_text("not,a,valid,cache\n", encoding="utf-8")
        rows = [kline_row(START_TS + i * HOUR_MS, 30_000.0 + i) for i in range(30)]
        rebuild = FakeTransport([HttpResponse(200, klines_body(rows))])
        rebuilt = fetch_history(cfg, "BTC/USDT", days=1, transport=rebuild, sleep=lambda _s: None,
                                end_ts_ms=START_TS + 30 * HOUR_MS)
        quarantined = sorted(p.name for p in cfg.cache_dir.iterdir() if p.name.endswith(".corrupt"))
        c.add(
            "Bozuk cache dosyasi karantinaya alinir ve yeniden insa edilir",
            "data/feed.py::fetch_history -> quarantine_cache + save_cache",
            {"source": "network", "quarantine_created": True, "cache_rebuilt": True},
            {"source": rebuilt.source, "quarantine_files": quarantined,
             "cache_rebuilt": corrupt_path.exists()},
            ok=(rebuilt.source == "network" and bool(quarantined) and corrupt_path.exists()),
        )

        # broker-level failure codes
        broker_codes, order_results = self._broker_failure_codes()
        expected_codes = {
            "synthetic_rejection": "synthetic_rejection",
            "partial_fill": PARTIAL,
            "insufficient_balance": "insufficient_balance",
            "below_min_notional": "below_min_notional",
            "no_open_position": "no_open_position",
        }
        ok = all(
            (broker_codes.get(key) == value if key != "insufficient_balance"
             else str(broker_codes.get(key, "")).startswith(value))
            for key, value in expected_codes.items()
        )
        c.add(
            "Broker seviyesi hata kodlari uretiliyor (rejection/partial/balance/min_notional/no_position)",
            "execution/paper_broker.py::buy/sell + FaultInjector",
            expected_codes,
            {k: broker_codes.get(k) for k in expected_codes},
            ok,
        )

        # persistence: the produced codes land in the ledger orders/events tables
        ledger_path = self.root / "data" / "acceptance-failure.sqlite"
        led = Ledger(ledger_path, "acceptance-failure")
        try:
            for result in order_results:
                led.record_order(result, mode="paper")
            led.record_events(
                [{"ts": TS, "code": code, "reason": "acceptance check"}
                 for code in ("daily_loss_limit_reached", "cooldown_active", "max_open_positions_reached")],
                category="risk",
            )
            orders = led.fetch_orders()
            events = led.fetch_events()
        finally:
            led.close()
        order_text = " ".join(str(o.get("reason", "")) for o in orders)
        event_codes = sorted({str(e.get("code", "")) for e in events})
        persisted = all(token in order_text or token in " ".join(event_codes)
                        for token in ("synthetic_rejection", "no_open_position", "below_min_notional",
                                      "insufficient_balance", "daily_loss_limit_reached", "cooldown_active"))
        c.add(
            "Uretilen hata olaylari deftere (orders/events) yazilir",
            "ledger/store.py::record_order + record_events -> orders/events tables",
            {"order_rows": 5, "events_persisted": True,
             "codes": ["cooldown_active", "daily_loss_limit_reached", "max_open_positions_reached"]},
            {"order_rows": len(orders), "order_reasons": sorted({str(o.get("reason", "")) for o in orders}),
             "event_codes": event_codes, "events_persisted": persisted},
            ok=(len(orders) == 5 and persisted
                and {"daily_loss_limit_reached", "cooldown_active"} <= set(event_codes)),
        )

        # matching regression tests must exist
        test_files = sorted((PACKAGE_ROOT / "tests").glob("test_*.py"))
        coverage: Dict[str, List[str]] = {}
        for token in PERSISTED_TEST_TOKENS:
            hits = [p.name for p in test_files if token in p.read_text(encoding="utf-8")]
            coverage[token] = hits
        missing = sorted(token for token, hits in coverage.items() if not hits)
        c.add(
            "Hata yollari icin regresyon testleri mevcut (cryptobot/tests)",
            "cryptobot/tests/test_*.py",
            {"missing_regression_tests": []},
            {"missing_regression_tests": missing, "coverage": coverage},
            ok=(not missing),
        )

    # ------------------------------------------------- D. remaining criteria
    def check_trade_ledger_persistence(self, c: Criterion) -> None:
        paper = self._paper_cycle()
        summary = paper["summary"]
        cfg = paper["config"]
        verification = summary.get("verification", {})
        checks = {check["name"]: check for check in verification.get("checks", [])}
        cash = checks.get("cash_from_ledger == broker.cash", {})
        realized = checks.get("realized_net_pnl", {})
        c.add(
            "Taze sinirli kosuda defter mutabakati delta 0.0 verir",
            "runner.py::PaperRunner.finalize -> ledger.verify",
            {"verification_ok": True, "cash_delta": 0.0, "realized_delta": 0.0},
            {"verification_ok": verification.get("ok"),
             "cash_delta": round(float(cash.get("delta", 1.0)), 12),
             "realized_delta": round(float(realized.get("delta", 1.0)), 12)},
            ok=(bool(verification.get("ok"))
                and abs(float(cash.get("delta", 1.0))) <= 1e-9
                and abs(float(realized.get("delta", 1.0))) <= 1e-9),
        )

        counts = summary.get("ledger_counts", {})
        c.add(
            "Defter tablolari doldurulur (equity == islenen bar, events > 0)",
            "ledger/store.py::counts",
            {"equity_equals_bars": True, "events": ">0", "ledger": ">=0"},
            {"counts": counts, "bars_processed": summary.get("bars_processed"),
             "equity_equals_bars": counts.get("equity") == summary.get("bars_processed")},
            ok=(counts.get("equity") == summary.get("bars_processed") and int(counts.get("events", 0)) > 0),
        )

        ledger = Ledger(cfg.db_path, "acceptance-paper")
        try:
            export_dir = self.root / "exports"
            ledger.export_csv(export_dir)
            ledger.export_json(export_dir / "ledger_acceptance-paper.json")
        finally:
            ledger.close()
        files = sorted(p.name for p in (self.root / "exports").iterdir())
        expected_files = ["equity.csv", "events.csv", "ledger.csv", "ledger_acceptance-paper.json",
                          "orders.csv", "trades.csv"]
        c.add(
            "Defter disa aktarimi CSV + JSON dosyalarini uretir",
            "ledger/store.py::export_csv + export_json (<tmp>/exports)",
            expected_files, files, ok=(files == expected_files),
        )

        persisted_codes = sorted({str(e.get("code", "")) for e in self._fetch_events(cfg.db_path, "acceptance-paper")})
        c.add(
            "Risk/ops olaylari defter 'events' tablosunda kalici",
            "ledger/store.py::fetch_events",
            {"paper_run_finished_present": True, "codes_present": True},
            {"event_count": len(persisted_codes), "codes": persisted_codes,
             "paper_run_finished_present": "paper_run_finished" in persisted_codes},
            ok=("paper_run_finished" in persisted_codes and len(persisted_codes) > 0),
        )

    def check_profit_target_logic(self, c: Criterion) -> None:
        cfg = self.base_config
        manager = self._make_manager(cfg)
        expected = required_gross_tp_pct(cfg.net_profit_target_pct, cfg.fee_pct, cfg.slippage_pct)
        drag = expected - cfg.net_profit_target_pct
        c.add(
            "Brut TP, net hedefi komisyon+slippage sonrasi saglayacak sekilde turetilir",
            "execution/costs.py::required_gross_tp_pct",
            {"net_target_pct": cfg.net_profit_target_pct, "gross_tp_pct": round(expected, 12),
             "cost_drag_pct": round(drag, 12)},
            {"net_target_pct": cfg.net_profit_target_pct, "gross_tp_pct": round(manager.gross_tp_pct, 12),
             "cost_drag_pct": round(manager.cost_drag_pct, 12)},
            ok=(abs(manager.gross_tp_pct - expected) <= 1e-12
                and abs(manager.cost_drag_pct - drag) <= 1e-12),
        )
        decision = self._entry(manager)
        trade = self._realise_exit(cfg, decision, trigger=EXIT_TAKE_PROFIT)
        c.add(
            "TP kapanisinda realize net yuzde hedefe esit (~2.0%)",
            "paper_broker.py round_trip net_pnl_pct",
            round(cfg.net_profit_target_pct, 6),
            round(trade["net_pnl_pct"], 6),
            ok=(abs(trade["net_pnl_pct"] - cfg.net_profit_target_pct) < 0.01),
        )

    def check_backtest_report(self, c: Criterion) -> None:
        backtests = self._backtests_run()
        result, paths = backtests["first"]
        names = sorted(p.name for p in paths.values())
        payload = json.loads((self.root / "bt1" / "acceptance_backtest.json").read_text(encoding="utf-8"))
        metrics = payload.get("metrics", {})
        required_metrics = ("trade_count", "win_rate_pct", "net_pnl_usdt", "max_drawdown_pct",
                            "profit_factor", "total_fees_usdt", "total_slippage_usdt")
        c.add(
            "Backtest JSON + Markdown raporlarini uretir ve metrikleri icerir",
            "backtest/report.py::write_reports (<tmp>/bt1)",
            {"files": ["acceptance_backtest.json", "acceptance_backtest.md"],
             "metrics_present": list(required_metrics), "determinism_hash_len": 64},
            {"files": names, "metrics_present": [k for k in required_metrics if k in metrics],
             "trade_count": metrics.get("trade_count"),
             "determinism_hash_len": len(str(payload.get("determinism_hash", ""))),
             "bars_processed": result.bars_processed},
            ok=(set(names) == {"acceptance_backtest.json", "acceptance_backtest.md"}
                and all(k in metrics for k in required_metrics)
                and len(str(payload.get("determinism_hash", ""))) == 64),
        )

    def check_config_and_run_docs(self, c: Criterion) -> None:
        present: Dict[str, bool] = {}
        for name in REQUIRED_DOC_FILES:
            present[name] = (PACKAGE_ROOT / name).exists()
        missing = sorted(name for name, ok in present.items() if not ok)
        c.add(
            "Gerekli dokuman/konfigurasyon/dagitim dosyalari mevcut",
            "cryptobot/{README,RISK,HANDOVER}.md, config.yaml, .env.example, Dockerfile, requirements.txt, workflow",
            {"missing": []},
            {"present": present, "missing": missing},
            ok=(not missing),
        )

        turkish: Dict[str, bool] = {}
        encoding_ok = True
        for name in ("README.md", "RISK.md", "HANDOVER.md"):
            try:
                text = (PACKAGE_ROOT / name).read_text(encoding="utf-8")
                turkish[name] = bool(set(text) & TURKISH_CHARS)
            except UnicodeDecodeError:
                encoding_ok = False
                turkish[name] = False
        c.add(
            "Dokumanlar gecerli UTF-8 ve Turkce karakter iceriyor",
            "README.md, RISK.md, HANDOVER.md (utf-8 decode)",
            {"valid_utf8": True, "turkish_chars": {"README.md": True, "RISK.md": True, "HANDOVER.md": True}},
            {"valid_utf8": encoding_ok, "turkish_chars": turkish},
            ok=(encoding_ok and all(turkish.values())),
        )

        raw = yaml.safe_load((PACKAGE_ROOT / "config.yaml").read_text(encoding="utf-8"))
        required_keys = ("mode", "initial_capital_usdt", "net_profit_target_pct", "stop_loss_pct",
                         "max_position_pct", "max_open_positions", "daily_loss_limit_pct",
                         "cooldown_minutes", "min_equity_usdt", "max_trades_per_day", "pairs",
                         "timeframe", "fee_pct", "slippage_pct", "strategy", "data")
        keys_missing = [k for k in required_keys if k not in raw]
        c.add(
            "config.yaml gecerli YAML, paper modu ve beklenen anahtarlari iceriyor",
            "cryptobot/config.yaml (yaml.safe_load)",
            {"mode": "paper", "missing_keys": []},
            {"mode": raw.get("mode"), "missing_keys": keys_missing},
            ok=(raw.get("mode") == "paper" and not keys_missing),
        )

        readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
        c.add(
            "README calistirma yollarini (paperbot.py launcher) ve cikis kodlarini belgeliyor",
            "cryptobot/README.md",
            {"documents_launcher": True, "documents_exit_codes": True, "documents_safety": True},
            {"documents_launcher": "paperbot.py" in readme,
             "documents_exit_codes": "exit" in readme.lower() or "çıkış" in readme.lower(),
             "documents_safety": "paper" in readme.lower() and "live" in readme.lower()},
            ok=("paperbot.py" in readme
                and ("exit" in readme.lower() or "çıkış" in readme.lower())
                and "paper" in readme.lower() and "live" in readme.lower()),
        )

    def check_reproducible_results(self, c: Criterion) -> None:
        backtests = self._backtests_run()
        first_json = (self.root / "bt1" / "acceptance_backtest.json").read_bytes()
        second_json = (self.root / "bt2" / "acceptance_backtest.json").read_bytes()
        sha_first = hashlib.sha256(first_json).hexdigest()
        sha_second = hashlib.sha256(second_json).hexdigest()
        hash_first = backtests["first"][0].determinism_hash
        hash_second = backtests["second"][0].determinism_hash
        c.add(
            "Ayni cache verisi uzerinde iki backtest bayt-ayni metrik JSON uretir",
            "backtest/engine.py x2 + backtest/report.py::write_json_report",
            {"byte_identical": True, "sha256_run1": sha_first, "sha256_run2": sha_first,
             "determinism_hash": hash_first},
            {"byte_identical": first_json == second_json, "sha256_run1": sha_first,
             "sha256_run2": sha_second, "determinism_hash_run1": hash_first,
             "determinism_hash_run2": hash_second},
            ok=(first_json == second_json and sha_first == sha_second and hash_first == hash_second),
        )

    def check_deployment_handover(self, c: Criterion) -> None:
        handover = (PACKAGE_ROOT / "HANDOVER.md").read_text(encoding="utf-8")
        dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")
        workflow = (PACKAGE_ROOT / ".github" / "workflows" / "paper-trade.yml").read_text(encoding="utf-8")
        rollback_script = (PACKAGE_ROOT / "scripts" / "runbook_rollback.py").exists()
        evidence = {
            "start": "run --" in handover,
            "status": "status" in handover,
            "stop": "stop" in handover,
            "verify": "verify" in handover,
            "rollback": ("rollback" in handover.lower()) and rollback_script,
            "dockerfile": dockerfile.startswith("#") and "FROM python" in dockerfile,
            "workflow": "paper-trade" in workflow and "python -m unittest" in workflow,
        }
        c.add(
            "HANDOVER runbook (baslat/izle/durdur/geri al), Dockerfile ve CI workflow mevcut",
            "HANDOVER.md, Dockerfile, .github/workflows/paper-trade.yml, scripts/runbook_rollback.py",
            {key: True for key in evidence},
            {**evidence, "rollback_script_exists": rollback_script},
            ok=all(evidence.values()),
        )

    def check_risk_disclosure(self, c: Criterion) -> None:
        risk = (PACKAGE_ROOT / "RISK.md").read_text(encoding="utf-8")
        disclosures = {
            "no_profit_guarantee": "Kâr garantisi yoktur" in risk,
            "not_investment_advice": "Yatırım tavsiyesi değildir" in risk,
            "past_performance": "Geçmiş performans gelecek performansı göstermez" in risk,
            "explicit_approval_for_real_money": "onay" in risk,
            "live_order_path_absent": "canlı emir göndermez" in risk,
            "simulation_warning": "simülasyon" in risk.lower(),
        }
        c.add(
            "RISK.md kar garantisi olmadigini ve canli emir yolunun bulunmadigini acikca yazar",
            "cryptobot/RISK.md",
            {key: True for key in disclosures},
            disclosures,
            ok=all(disclosures.values()),
        )

    # ------------------------------------------------------------- fixtures
    def _paper_cycle(self) -> Dict[str, Any]:
        if self._paper is not None:
            return self._paper
        cfg = self._tmp_config(db_name="acceptance-paper.sqlite")
        with self._run_dir_env():
            runner = PaperRunner(
                cfg,
                runner=RunnerConfig(cycles=5, offline=True, replay=True,
                                    replay_bars_per_cycle=500, interval_seconds=0.0),
                run_id="acceptance-paper",
                sleep_fn=lambda _seconds: None,
                setup_logs=False,
                log_path=self.root / "logs" / "acceptance-paper.log",
            )
            runner.setup()
            start = {
                "initial_capital_usdt": cfg.initial_capital_usdt,
                "initial_cash": runner.engine.broker.initial_cash,
                "starting_cash": runner.engine.broker.cash,
                "starting_equity": runner.engine.broker.equity(),
            }
            summary = runner.run()
        self._paper = {"config": cfg, "summary": summary, "start": start}
        return self._paper

    def _backtests_run(self) -> Dict[str, Any]:
        if self._backtests is not None:
            return self._backtests
        cfg = self._tmp_config(db_name="acceptance-backtest.sqlite")
        frames: Dict[str, Any] = {}
        meta: Dict[str, Any] = {}
        for pair in cfg.pairs:
            loaded = load_candles(cfg, pair, allow_network=False)
            frames[pair] = loaded.frame
            meta[pair] = {"source": loaded.source, "complete": loaded.complete, "rows": loaded.rows}

        def one(out_dir: Path):
            engine = BacktestEngine(cfg)
            result = engine.run(frames, data_meta=meta)
            paths = write_reports(result, out_dir, basename="acceptance_backtest", with_chart=False)
            return result, paths

        self._backtests = {"first": one(self.root / "bt1"), "second": one(self.root / "bt2"),
                           "config": cfg}
        return self._backtests

    def _realise_exit(self, cfg: Config, decision, *, trigger: str) -> Dict[str, Any]:
        """Open a real position via the real broker, then close it at the level ``trigger``."""
        broker = PaperBroker(cfg.initial_capital_usdt, cfg.fee_pct, cfg.slippage_pct,
                             max_open_positions=cfg.max_open_positions)
        order = broker.buy("BTC/USDT", 100.0, decision.qty, TS,
                           stop_price=decision.stop_price, tp_price=decision.tp_price, reason="acceptance")
        position = broker.positions["BTC/USDT"]
        if trigger == EXIT_STOP_LOSS:
            exit_decision = self._make_manager(cfg).check_exit(
                position, high=position.entry_reference_price + 1.0,
                low=position.stop_price, close=position.stop_price)
        else:
            exit_decision = self._make_manager(cfg).check_exit(
                position, high=position.tp_price, low=position.stop_price + 1.0, close=position.tp_price)
        sell = broker.sell("BTC/USDT", exit_decision.reference_price, TS + HOUR_MS,
                           reason="{}:{}".format(exit_decision.trigger, exit_decision.reason))
        trade = broker.closed_trades[-1]
        return {
            "trigger": exit_decision.trigger,
            "status": sell.status,
            "net_pnl": sell.net_pnl,
            "net_pnl_pct": float(trade.get("net_pnl_pct", 0.0)),
            "exit_reason": str(trade.get("exit_reason", "")),
            "buy_status": order.status,
        }

    def _position_and_manager(self, cfg: Config) -> Dict[str, Any]:
        manager = self._make_manager(cfg)
        decision = self._entry(manager)
        broker = PaperBroker(cfg.initial_capital_usdt, cfg.fee_pct, cfg.slippage_pct,
                             max_open_positions=cfg.max_open_positions)
        broker.buy("BTC/USDT", 100.0, decision.qty, TS,
                   stop_price=decision.stop_price, tp_price=decision.tp_price, reason="acceptance")
        return {"manager": manager, "position": broker.positions["BTC/USDT"]}

    @staticmethod
    def _broker_failure_codes() -> Tuple[Dict[str, Any], List[Any]]:
        results: List[Any] = []
        codes: Dict[str, Any] = {}

        reject_buy = PaperBroker(50.0, 0.1, 0.05, max_open_positions=2,
                                 faults=FaultInjector().enable().reject_next("buy", 1))
        result = reject_buy.buy("BTC/USDT", 30_000.0, 0.002, TS, stop_price=1.0, tp_price=2.0)
        results.append(result)
        codes["synthetic_rejection"] = result.reason

        partial_buy = PaperBroker(50.0, 0.1, 0.05, max_open_positions=2,
                                  faults=FaultInjector().enable().partial_next("buy", 0.5))
        result = partial_buy.buy("BTC/USDT", 30_000.0, 0.002, TS, stop_price=1.0, tp_price=2.0)
        results.append(result)
        codes["partial_fill"] = result.status

        blocked_cash = PaperBroker(1e9, 0.1, 0.05, max_open_positions=2,
                                   faults=FaultInjector().enable().block_cash(True))
        result = blocked_cash.buy("BTC/USDT", 30_000.0, 0.002, TS, stop_price=1.0, tp_price=2.0)
        results.append(result)
        codes["insufficient_balance"] = result.reason

        small = PaperBroker(50.0, 0.1, 0.05, max_open_positions=2)
        result = small.buy("BTC/USDT", 30_000.0, 0.0001, TS, stop_price=1.0, tp_price=2.0)
        results.append(result)
        codes["below_min_notional"] = result.reason

        result = small.sell("ETH/USDT", 2_000.0, TS)
        results.append(result)
        codes["no_open_position"] = result.reason
        return codes, results

    @staticmethod
    def _run_mode(db_path: Path, run_id: str) -> Optional[str]:
        ledger = Ledger(db_path, run_id)
        try:
            rows = ledger._query("SELECT mode FROM runs WHERE run_id = ?", (run_id,))  # noqa: SLF001
        finally:
            ledger.close()
        return str(rows[0]["mode"]) if rows else None

    @staticmethod
    def _order_statuses(db_path: Path, run_id: str) -> List[str]:
        ledger = Ledger(db_path, run_id)
        try:
            rows = ledger.fetch_orders()
        finally:
            ledger.close()
        return sorted({str(r.get("status", "")) for r in rows})

    @staticmethod
    def _fetch_events(db_path: Path, run_id: str) -> List[Dict[str, Any]]:
        ledger = Ledger(db_path, run_id)
        try:
            return ledger.fetch_events()
        finally:
            ledger.close()

    # -------------------------------------------------------------- execution
    def run(self) -> int:
        for cid, label, kinds in CRITERIA_SPEC:
            criterion = Criterion(cid, label, kinds)
            self.results[cid] = criterion
            method = getattr(self, "check_{}".format(cid.replace("-", "_")))
            try:
                method(criterion)
            except Exception as exc:  # noqa: BLE001 - a broken check must FAIL, not crash
                self._cleanup()
                import traceback

                criterion.add(
                    "harness exception while measuring",
                    "cryptobot/scripts/acceptance_check.py::{}".format(method.__name__),
                    "no exception",
                    {"error": "{}: {}".format(type(exc).__name__, exc),
                     "traceback_tail": traceback.format_exc().splitlines()[-3:]},
                    False,
                )
        payload = self._payload()
        self._write_artifacts(payload)
        self._cleanup()
        return EXIT_OK if payload["ok"] else EXIT_FAIL

    def _payload(self) -> Dict[str, Any]:
        criteria = [self.results[cid].as_dict() for cid, _label, _kinds in CRITERIA_SPEC]
        checks_total = sum(len(c["checks"]) for c in criteria)
        checks_passed = sum(1 for c in criteria for check in c["checks"] if check["ok"])
        failed = [
            {"criterion": c["id"], "check": check["name"], "exit_status": check["exit_status"]}
            for c in criteria for check in c["checks"] if not check["ok"]
        ]
        return {
            "schema": "cryptobot.acceptance/1",
            "version": __version__,
            "ok": all(c["status"] == PASS for c in criteria),
            "criteria": criteria,
            "summary": {
                "criteria_total": len(criteria),
                "criteria_passed": sum(1 for c in criteria if c["status"] == PASS),
                "checks_total": checks_total,
                "checks_passed": checks_passed,
                "failed": failed,
            },
        }

    def _write_artifacts(self, payload: Dict[str, Any]) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.reports_dir / "acceptance.json"
        json_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        (self.reports_dir / "acceptance.md").write_text(self._render_markdown(payload), encoding="utf-8")

    @staticmethod
    def _render_markdown(payload: Dict[str, Any]) -> str:
        summary = payload["summary"]
        lines: List[str] = []
        lines.append("# Kabul Dogrulama Raporu (acceptance)")
        lines.append("")
        lines.append("> Bu rapor makine tarafindan uretilmistir: `python cryptobot/scripts/paperbot.py acceptance`.")
        lines.append("> Sonuc: **{}**".format("TUM KRITERLER PASS" if payload["ok"] else "BASARISIZ"))
        lines.append("> Olcum tamamen cevrimdisi (offline) ve deterministiktir; gercek ag cagrisi yoktur.")
        lines.append("")
        lines.append("| Kriter | Durum | Kanit turleri | Kontrol | Gecen |")
        lines.append("| --- | --- | --- | --- | --- |")
        for criterion in payload["criteria"]:
            passed = sum(1 for check in criterion["checks"] if check["ok"])
            lines.append("| {} | {} | {} | {} | {} |".format(
                criterion["id"], criterion["status"], ", ".join(criterion["evidence_kinds"]),
                len(criterion["checks"]), passed))
        lines.append("")
        lines.append("Ozet: {criteria_passed}/{criteria_total} kriter, "
                     "{checks_passed}/{checks_total} kontrol PASS.".format(**summary))
        lines.append("")
        for criterion in payload["criteria"]:
            lines.append("## {} -- {}".format(criterion["id"], criterion["status"]))
            lines.append("")
            lines.append("_{}_".format(criterion["label"]))
            lines.append("")
            for check in criterion["checks"]:
                lines.append("- [{}] **{}**".format("OK" if check["ok"] else "FAIL", check["name"]))
                lines.append("  - hedef: `{}`".format(check["command_or_target"]))
                lines.append("  - beklenen: `{}`".format(_clip(check["expected"])))
                lines.append("  - gozlenen: `{}`".format(_clip(check["observed"])))
            lines.append("")
        if summary["failed"]:
            lines.append("## Basarisiz kontroller")
            lines.append("")
            for item in summary["failed"]:
                lines.append("- {} :: {} (exit_status={})".format(
                    item["criterion"], item["check"], item["exit_status"]))
            lines.append("")
        return "\n".join(lines)

    def _cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _clip(value: Any, limit: int = 480) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acceptance_check.py",
        description="cryptobot kabul kriterlerini makine-okunur kanitla dogrula (offline, deterministik)",
    )
    parser.add_argument("--reports-dir", type=Path, default=None,
                        help="cikti klasoru (varsayilan: cryptobot/reports)")
    parser.add_argument("--json-stdout", action="store_true", help="payload JSON'unu stdout'a da yaz")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    enter_harness_mode()
    reports_dir = Path(args.reports_dir) if args.reports_dir else (PACKAGE_ROOT / "reports")
    harness = AcceptanceHarness(reports_dir)
    code = harness.run()
    payload_path = reports_dir / "acceptance.json"
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"ok": False, "summary": {}, "criteria": []}
    summary = payload.get("summary", {})
    print("")
    print("=" * 78)
    print("cryptobot acceptance -- {}".format("PASS" if payload.get("ok") else "FAIL"))
    print("=" * 78)
    for criterion in payload.get("criteria", []):
        passed = sum(1 for check in criterion["checks"] if check["ok"])
        print("  [{:<4}] {:<28} {:>2}/{:<2}  {}".format(
            criterion["status"], criterion["id"], passed, len(criterion["checks"]), criterion["label"]))
    print("-" * 78)
    print("  {criteria_passed}/{criteria_total} kriter, {checks_passed}/{checks_total} kontrol PASS".format(
        criteria_passed=summary.get("criteria_passed", 0), criteria_total=summary.get("criteria_total", 0),
        checks_passed=summary.get("checks_passed", 0), checks_total=summary.get("checks_total", 0)))
    for item in summary.get("failed", []):
        print("  FAIL: {} :: {} (exit_status={})".format(item["criterion"], item["check"], item["exit_status"]))
    print("  rapor: {}".format(payload_path))
    print("  rapor: {}".format(reports_dir / "acceptance.md"))
    print("  exit : {}".format(code))
    if args.json_stdout:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    return code


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
