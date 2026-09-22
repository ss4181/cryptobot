"""Rehearsed rollback drill: deploy new parameters, then roll back and prove it.

This is the executable version of the rollback procedure documented in
``HANDOVER.md``.  It is safe: it only rewrites ``config.yaml`` and always
restores the baseline before exiting (also on failure).

Steps
-----
1. back up ``config.yaml`` to ``.repro/config.baseline.yaml``
2. run the backtest with the baseline parameters -> record its metrics hash
3. "deploy" a parameter change (``--net-target-pct`` / ``--max-position-pct``)
4. run again -> the hash must differ (the change really took effect)
5. roll back by restoring the backup
6. run again -> metrics and JSON must be byte-identical to step 2

Usage::

    python cryptobot/scripts/runbook_rollback.py
    python cryptobot/scripts/runbook_rollback.py --timeframe 15m --net-target-pct 1.5 --max-position-pct 50
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from cryptobot import SAFETY_BANNER  # noqa: E402
from cryptobot.backtest.engine import BacktestEngine  # noqa: E402
from cryptobot.backtest.report import report_basename, write_reports  # noqa: E402
from cryptobot.data.feed import load_candles  # noqa: E402
from cryptobot.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from cryptobot.notify.guard import enter_harness_mode  # noqa: E402

CONFIG_PATH = DEFAULT_CONFIG_PATH
BACKUP_PATH = PACKAGE_ROOT / ".repro" / "config.baseline.yaml"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_config(path: Path, changes: Dict[str, Any]) -> None:
    """Rewrite top-level ``key: value`` lines, preserving any trailing comment."""
    text = path.read_text(encoding="utf-8")
    for key, value in changes.items():
        pattern = re.compile(r"(?m)^({})(:)(\s*)([^#\n]*?)(\s*)(#.*)?$".format(re.escape(key)))
        if not pattern.search(text):
            raise SystemExit("config key {!r} not found in {}".format(key, path))

        def replacement(match: "re.Match[str]") -> str:
            comment = match.group(6) or ""
            return "{}{}{}{}{}{}".format(match.group(1), match.group(2), match.group(3), value,
                                         " " if comment else "", comment)

        text = pattern.sub(replacement, text, count=1)
    path.write_text(text, encoding="utf-8")


def run_backtest(timeframe: str, overrides: Dict[str, Any], label: str) -> Tuple[Dict[str, Any], Path]:
    config = load_config(CONFIG_PATH, cli_overrides={"timeframe": timeframe, **overrides})
    frames, meta = {}, {}
    for pair in config.pairs:
        result = load_candles(config, pair, allow_network=False)
        frames[pair] = result.frame
        meta[pair] = {"source": result.source, "complete": result.complete, "rows": result.rows}
    engine = BacktestEngine(config, run_id="rollback-{}".format(label))
    result = engine.run(frames, data_meta=meta)
    basename = "{}_{}".format(report_basename(config), label)
    paths = write_reports(result, PACKAGE_ROOT / "reports", basename=basename, with_chart=False)
    print("  {:<22} net={:<12} trades={:<4} gross_tp={:.4f}%  hash={}".format(
        label, result.metrics["net_pnl_usdt"], result.metrics["trade_count"],
        engine.risk.gross_tp_pct, result.determinism_hash[:16]))
    return result.metrics, paths["json"]


def main(argv: Optional[List[str]] = None) -> int:
    # Internal harness: never a real notification publisher (see notify/guard.py).
    enter_harness_mode()
    parser = argparse.ArgumentParser(prog="runbook_rollback.py",
                                     description="deploy + rollback drill with verification")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--net-target-pct", type=float, default=1.5)
    parser.add_argument("--max-position-pct", type=float, default=50.0)
    parser.add_argument("--keep-artifacts", action="store_true",
                        help="keep the deploy/rollback report files (default: keep)")
    args = parser.parse_args(argv)

    print(SAFETY_BANNER)
    print("rollback drill on {} (config: {})".format(args.timeframe, CONFIG_PATH))
    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    checks: List[str] = []

    try:
        # 1. back up
        shutil.copy2(CONFIG_PATH, BACKUP_PATH)
        print("\n[1] baseline config backed up -> {}".format(BACKUP_PATH))

        # 2. baseline run
        print("\n[2] baseline backtest")
        baseline_metrics, baseline_json = run_backtest(args.timeframe, {}, "baseline")
        baseline_hash = sha256(baseline_json)

        # 3. deploy changed parameters
        print("\n[3] deploying changed parameters: net_profit_target_pct={} max_position_pct={}".format(
            args.net_target_pct, args.max_position_pct))
        patch_config(CONFIG_PATH, {
            "net_profit_target_pct": args.net_target_pct,
            "max_position_pct": args.max_position_pct,
        })
        changed_metrics, changed_json = run_backtest(args.timeframe, {}, "deployed")
        changed_hash = sha256(changed_json)
        changed_effective = changed_metrics != baseline_metrics
        checks.append("deployed parameters change behaviour: {}".format(changed_effective))
        print("  -> metrics differ from baseline: {}".format(changed_effective))

        # 4. roll back
        print("\n[4] rolling back config.yaml from the backup")
        shutil.copy2(BACKUP_PATH, CONFIG_PATH)
        restored_metrics, restored_json = run_backtest(args.timeframe, {}, "rolled_back")
        restored_hash = sha256(restored_json)

        identical = restored_hash == baseline_hash
        metrics_identical = restored_metrics == baseline_metrics
        checks.append("rolled-back JSON byte-identical to baseline: {}".format(identical))
        checks.append("rolled-back metrics identical to baseline: {}".format(metrics_identical))

        print("\n" + "=" * 74)
        print("baseline   sha256={}  hash={}".format(baseline_hash[:32], baseline_metrics.get("trade_count")))
        print("deployed   sha256={}".format(changed_hash[:32]))
        print("rolledback sha256={}".format(restored_hash[:32]))
        print("-" * 74)
        for check in checks:
            print("  {}".format(check))
        ok = changed_effective and identical and metrics_identical
        print("=" * 74)
        print("ROLLBACK DRILL: {}".format("PASS" if ok else "FAIL"))
        print("reports: {}".format(", ".join(p.name for p in sorted((PACKAGE_ROOT / 'reports').glob('*_baseline.json')))))
        print("handoffDisposition: {}".format("native_fallback_allowed" if ok else "workspace_action_required"))
        return 0 if ok else 1
    finally:
        # Never leave the workspace with the experimental parameters in place.
        if BACKUP_PATH.exists():
            shutil.copy2(BACKUP_PATH, CONFIG_PATH)
            print("\nconfig.yaml restored to the baseline values (guaranteed by finally)")


if __name__ == "__main__":
    raise SystemExit(main())
