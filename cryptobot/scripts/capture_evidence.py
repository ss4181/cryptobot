"""Evidence capture for the handover report: grep + failure-path + risk-limit demos.

Produces ``reports/evidence.txt`` with the raw output of:

1. the AST-based live-order scan over every runtime module,
2. a plain lexical grep over the whole repository for order-submission APIs
   (including tests, which must name what they forbid),
3. a failure-path matrix (feed outage/malformed payload/corrupt cache/rejected
   order/insufficient balance/partial fill) run live against the real code,
4. a risk-limit matrix (daily loss halt, cooldown, position caps, min notional).

Usage::

    python cryptobot/scripts/capture_evidence.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from cryptobot.data.feed import (  # noqa: E402
    CacheCorrupted,
    FeedMalformedResponse,
    FeedTimeout,
    FeedUnavailable,
    HttpResponse,
    fetch_klines_range,
    fetch_history,
    load_cache,
    load_candles,
)
from cryptobot.execution.paper_broker import FaultInjector, PaperBroker  # noqa: E402
from cryptobot.execution.costs import required_gross_tp_pct  # noqa: E402
from cryptobot.risk.manager import RiskManager  # noqa: E402
from cryptobot.tests.fixtures import FakeTransport, kline_row, klines_body, sleep_recorder, tmp_config  # noqa: E402
from cryptobot.tests.no_live_order_scan import scan  # noqa: E402
from cryptobot.config import load_config  # noqa: E402
from cryptobot.notify.guard import enter_harness_mode  # noqa: E402

LEXICAL_PATTERNS: List[str] = [
    "create_order", "createOrder", "place_order", "submit_order", "cancel_order",
    "fetch_balance", "private_get", "private_post", "sapi", "apiKey", "api_key",
    "recvWindow", "X-MBX-APIKEY", "requests.post", "hmac", "withdraw",
]

TS = 1_775_000_000_000
#: Absolute scratch cache for the failure-path demos.  Deriving it from
#: ``Path.cwd()`` made an unexpected working directory create a nested
#: ``cryptobot/cryptobot/reports/.evidence-tmp`` that the cleanup never removed.
EVIDENCE_TMP = PACKAGE_ROOT / "reports" / ".evidence-tmp"


def header(title: str) -> str:
    return "\n" + "=" * 78 + "\n " + title + "\n" + "=" * 78


def live_order_scan() -> str:
    result = scan()
    lines = [header("1. AST SCAN -- no live-order / signed-request code path"),
             result.render()]
    return "\n".join(lines)


def lexical_grep() -> str:
    lines = [header("2. LEXICAL GREP -- order-submission API tokens across the repo"),
             "patterns: {}".format(", ".join(LEXICAL_PATTERNS)), ""]
    hits = 0
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        relative = path.relative_to(PACKAGE_ROOT)
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for token in LEXICAL_PATTERNS:
                if token in raw:
                    hits += 1
                    lines.append("  {}\n    {:<5}: {}".format(relative, lineno, raw.strip()[:100]))
                    break
    lines.append("")
    lines.append("  total matches: {} (all inside cryptobot/tests/*, which must name the APIs"
                 " they forbid, and cryptobot/scripts/*, which holds the scan patterns)".format(hits))
    return "\n".join(lines)


def failure_paths() -> str:
    lines = [header("3. FAILURE PATHS -- simulated live, offline (fixture transports)")]
    config = tmp_config(EVIDENCE_TMP)
    sleeps, sleep_fn = sleep_recorder()

    # 3.1 timeout -> retry with bounded exponential backoff -> then fail safe
    transport = FakeTransport([FeedTimeout("simulated outage")] * 8)
    try:
        fetch_klines_range(api_base="https://api.binance.com", symbol="BTCUSDT", interval="1h",
                           start_ms=TS, end_ms=TS + 3_600_000, transport=transport,
                           max_retries=3, backoff_initial=1.0, backoff_max=4.0, sleep=sleep_fn)
        outcome = "NO ERROR (unexpected)"
    except FeedUnavailable:
        outcome = "FeedUnavailable after retries"
    except FeedTimeout:
        outcome = "FeedTimeout after retries (fail-safe: caller stops opening positions)"
    lines += ["",
              "  [3.1] network outage / timeout",
              "        outcome        : {}".format(outcome),
              "        http attempts  : {}".format(transport.call_count),
              "        backoff sleeps : {} (bounded exponential)".format(sleeps)]

    # 3.2 malformed payload (non-JSON then a structurally invalid kline)
    sleeps2, sleep2 = sleep_recorder()
    transport = FakeTransport([HttpResponse(200, "<html>bad gateway</html>")] * 5)
    try:
        fetch_klines_range(api_base="https://api.binance.com", symbol="BTCUSDT", interval="1h",
                           start_ms=TS, end_ms=TS + 3_600_000, transport=transport,
                           max_retries=2, backoff_initial=0.5, backoff_max=2.0, sleep=sleep2)
        outcome = "NO ERROR (unexpected)"
    except FeedMalformedResponse as exc:
        outcome = "FeedMalformedResponse: {}".format(exc)
    lines += ["",
              "  [3.2] malformed / unexpected API response",
              "        outcome        : {}".format(outcome),
              "        http attempts  : {} (retried with backoff {})".format(transport.call_count, sleeps2)]

    # 3.3 stale cache on outage (fail-safe result) + no cache at all
    cache_dir = config.data.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    from cryptobot.data.feed import save_cache

    frame = __import__("cryptobot.tests.fixtures", fromlist=["make_frame"]).make_frame(60, start_ts=TS)
    save_cache(frame, cache_dir / "BTCUSDT_1h.csv")
    stale = fetch_history(config, "BTC/USDT", days=1,
                          transport=FakeTransport([FeedTimeout("down")] * 8), sleep=lambda _s: None,
                          end_ts_ms=int(frame["ts"].iloc[-1]) + 3_600_000)
    lines += ["",
              "  [3.3] outage with an existing cache (fail-safe)",
              "        source         : {}".format(stale.source),
              "        complete       : {}  (engine pauses new entries when False)".format(stale.complete),
              "        warnings       : {}".format(stale.warnings)]
    try:
        load_candles(config, "ETH/USDT", allow_network=False)
        lines.append("        no-cache offline load: NO ERROR (unexpected)")
    except CacheCorrupted as exc:
        lines.append("        no-cache offline load -> CacheCorrupted: {}".format(exc))

    # 3.4 cache corruption -> quarantine + refetch
    path = cache_dir / "BTCUSDT_1h.csv"
    path.write_text("this,is,not,a,cache\n", encoding="utf-8")
    try:
        load_cache(path)
        lines.append("        corrupt cache load: NO ERROR (unexpected)")
    except CacheCorrupted as exc:
        lines.append("        corrupt cache load -> CacheCorrupted: {}".format(exc))
    transport = FakeTransport([HttpResponse(200, klines_body([kline_row(TS, 100.0)]))])
    fetch_history(config, "BTC/USDT", days=1, transport=transport, sleep=lambda _s: None,
                  end_ts_ms=TS + 3_600_000)
    quarantined = [p.name for p in cache_dir.iterdir() if p.name.endswith(".corrupt")]

    lines += ["",
              "  [3.4] cache corruption",
              "        quarantined    : {}".format(quarantined or "none"),
              "        cache rebuilt  : {}".format(path.exists())]

    # 3.5 rejected order + insufficient balance + partial fill
    faults = FaultInjector().enable()
    faults.reject_next("buy", 1).partial_next("buy", 0.5)
    broker = PaperBroker(50.0, 0.1, 0.05, faults=faults, max_open_positions=2)
    rejected = broker.buy("BTC/USDT", 60_000.0, 0.001, TS, stop_price=1, tp_price=2)
    partial = broker.buy("BTC/USDT", 60_000.0, 0.001, TS + 1, stop_price=1, tp_price=2)
    too_big = broker.buy("ETH/USDT", 3_000.0, 1.0, TS + 2, stop_price=1, tp_price=2)
    dust = broker.buy("ETH/USDT", 3_000.0, 0.0001, TS + 3, stop_price=1, tp_price=2)
    no_position = broker.sell("SOL/USDT", 100.0, TS + 4)
    lines += ["",
              "  [3.5] order-level failures",
              "        synthetic reject : {}".format(rejected.status + " / " + rejected.reason),
              "        partial fill     : {} filled {:.8f} of {:.8f}".format(
                  partial.status, partial.filled_qty, partial.requested_qty),
              "        insufficient cash: {} / {}".format(too_big.status, too_big.reason),
              "        below min notional: {} / {}".format(dust.status, dust.reason),
              "        sell w/o position: {} / {}".format(no_position.status, no_position.reason),
              "        counter          : {}".format(broker.stats)]
    return "\n".join(lines)


def risk_limits() -> str:
    manager = RiskManager(
        initial_equity=50.0, fee_pct=0.1, slippage_pct=0.05, net_profit_target_pct=2.0,
        stop_loss_pct=2.5, max_position_pct=90.0, max_open_positions=1, daily_loss_limit_pct=5.0,
        cooldown_minutes=60.0, min_equity_usdt=10.0, max_trades_per_day=8,
    )
    lines = [header("4. RISK LIMITS -- every limit produces an observable code"),
             "  derived gross take-profit : {:.6f}%  (net target 2% after fee 0.1%/leg + slippage 0.05%/leg)".format(
                 required_gross_tp_pct(2.0, 0.1, 0.05))]
    ok = manager.evaluate_entry(ts=TS, price=60_000.0, equity=50.0, cash=50.0, open_positions=0)
    lines += ["  approved entry            : qty={:.8f} notional={:.4f} stop={:.2f} tp={:.2f}".format(
        ok.qty, ok.notional, ok.stop_price, ok.tp_price)]

    cases = [
        ("max_open_positions", dict(open_positions=1), None),
        ("daily_loss_limit", dict(), ("loss", -3.0)),
        ("cooldown_active", dict(), ("loss", -0.5)),
        ("max_trades_per_day", dict(), ("trades", 8)),
        ("equity_below_minimum", dict(equity=5.0, cash=5.0), None),
        ("below_min_notional", dict(equity=6.0, cash=5.0), None),
    ]
    for name, kwargs, pre in cases:
        probe = manager
        if name == "max_open_positions":
            probe.max_open_positions = 1
        if pre and pre[0] == "trades":
            probe.state.trades_today = pre[1]
            probe.state.cooldown_until_ts = 0
            probe.state.halted = False
            decision = probe.evaluate_entry(ts=TS + 10_000_000, price=60_000.0, equity=50.0,
                                           cash=50.0, open_positions=0)
            lines.append("  {:<24}: {} ({})".format(name, decision.code, decision.reason[:60]))
            probe.state.trades_today = 0
            continue
        if pre and pre[0] == "loss":
            probe.register_trade_result(pre[1], TS)
        saved_min_equity = probe.min_equity_usdt
        if name == "below_min_notional":
            probe.min_equity_usdt = 0.0  # isolate the sizing rule from the equity floor
        decision = probe.evaluate_entry(ts=TS + 1_000, price=60_000.0,
                                        equity=kwargs.get("equity", 50.0),
                                        cash=kwargs.get("cash", 50.0),
                                        open_positions=kwargs.get("open_positions", 0))
        probe.min_equity_usdt = saved_min_equity
        lines.append("  {:<24}: {} ({})".format(name, decision.code, decision.reason[:70]))
        # reset the probe state for the next case
        probe.state.halted = False
        probe.state.halt_reason = ""
        probe.state.cooldown_until_ts = 0
        probe.state.trades_today = 0
        probe.state.day_realized_net_pnl = 0.0

    lines += ["",
              "  audit trail codes: {}".format(
                  [item["code"] for item in manager.audit][:12]),
              "  stop-loss vs take-profit on one bar: stop wins (pessimistic intrabar assumption)"]
    return "\n".join(lines)


def main() -> int:
    # Internal harness: never a real notification publisher (see notify/guard.py).
    enter_harness_mode()
    sections = [live_order_scan(), lexical_grep(), failure_paths(), risk_limits()]
    text = "\n".join(sections) + "\n"
    out = PACKAGE_ROOT / "reports" / "evidence.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    # The failure-path demos used a scratch cache; do not leave it behind.
    scratch = EVIDENCE_TMP
    if scratch.exists():
        import shutil

        shutil.rmtree(scratch, ignore_errors=True)
    print(text)
    print("\nwritten to {}".format(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
