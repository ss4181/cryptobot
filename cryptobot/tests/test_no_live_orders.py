"""Static proof that no live-order / authenticated code path exists.

The scanner itself lives in :mod:`cryptobot.tests.no_live_order_scan` and is
shared with ``cryptobot/scripts/verify_all.py`` so the test evidence and the
verification evidence are the same measurement.

``cryptobot/tests`` and ``cryptobot/scripts`` are excluded from the scan (they
must name the APIs they forbid); the exclusions are asserted below so the scan
cannot be silently widened into "scans nothing".
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from .no_live_order_scan import EXCLUDED_PARTS, PACKAGE_ROOT, runtime_files, scan

EXPECTED_MODULES = (
    "safety.py", "config.py", "cli.py", "runner.py", "__main__.py",
    "execution/paper_broker.py", "execution/costs.py", "backtest/engine.py",
    "backtest/metrics.py", "backtest/report.py", "ledger/store.py", "data/feed.py",
    "risk/manager.py", "strategy/base.py", "strategy/mean_reversion.py",
    "strategy/indicators.py", "monitor/logging_setup.py", "monitor/daily_report.py",
)


class TestNoLiveOrderCodePath(unittest.TestCase):
    maxDiff = None

    def test_scan_covers_every_runtime_module(self):
        scanned = {relative for _, relative in runtime_files()}
        missing = [name for name in EXPECTED_MODULES if name not in scanned]
        self.assertEqual(missing, [], "scan must cover: {}".format(missing))
        self.assertGreaterEqual(len(scanned), len(EXPECTED_MODULES))

    def test_scan_excludes_only_tests_and_scripts(self):
        self.assertEqual(set(EXCLUDED_PARTS), {"tests", "scripts", "__pycache__"})
        # No top-level runtime module may be excluded.
        real = {p.relative_to(PACKAGE_ROOT).parts[0] for p in PACKAGE_ROOT.glob("*.py")}
        self.assertTrue(real.isdisjoint(EXCLUDED_PARTS))

    def test_no_live_order_or_private_api_usage(self):
        result = scan()
        self.assertEqual(
            result.findings, [],
            "live-order / signed-request code path detected:\n" + result.render(limit=100),
        )

    def test_feed_only_uses_the_public_klines_endpoint(self):
        from cryptobot.data import feed

        self.assertEqual(feed.KLINES_PATH, "/api/v3/klines")
        text = (PACKAGE_ROOT / "data" / "feed.py").read_text(encoding="utf-8")
        # Code-level absence (prose in the docstring is allowed and expected).
        for token in ("/api/v3/order", "/sapi", "/api/v3/account", "userTrades", "signature=", "hmac"):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_ccxt_transport_sets_no_credentials(self):
        text = (PACKAGE_ROOT / "data" / "feed.py").read_text(encoding="utf-8")
        block = text.split("class CcxtTransport", 1)[1].split("def get", 1)[0]
        for token in ("apiKey", "api_key", "secret", "password", "uid", "sign"):
            self.assertNotIn(token, block)

    def test_safety_module_is_the_single_choke_point(self):
        text = (PACKAGE_ROOT / "config.py").read_text(encoding="utf-8")
        self.assertIn("assert_allowed_mode", text)
        safety_text = (PACKAGE_ROOT / "safety.py").read_text(encoding="utf-8")
        self.assertIn("_FORBIDDEN_PATH_TOKENS", safety_text)

    def test_scanner_actually_detects_a_violation(self):
        """Negative control: the scanner must flag planted offending code."""
        import tempfile

        from .no_live_order_scan import scan_file

        planted = (
            "import requests\n"
            "def boom(client, apiKey, secret):\n"
            "    return client.create_order('BTC/USDT', 'market', 'buy', 1)\n"
            "def leak():\n"
            "    return requests.post('https://api.binance.com/sapi/v1/order')\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "planted.py"
            path.write_text(planted, encoding="utf-8")
            findings = scan_file(path, "planted.py")
        rules = {finding["rule"] for finding in findings}
        self.assertTrue(findings, "negative control produced no findings")
        self.assertIn("attribute", rules)          # client.create_order
        self.assertIn("param", rules)              # apiKey / secret parameters
        self.assertIn("http-write", rules)         # requests.post
        self.assertIn("credential-string", rules)  # /sapi endpoint

    def test_scanner_ignores_documentation_and_denylists(self):
        """Positive control: prose and deny-list constants are not findings."""
        import tempfile

        from .no_live_order_scan import scan_file

        text = (
            '"""Docs may mention create_order and /sapi/v1/order."""\n'
            "_FORBIDDEN_TOKENS = ('create_order', '/sapi', 'apiKey')\n"
            "SENSITIVE_KEYS = ('api_key', 'secret')\n"
            "def harmless():\n"
            "    return 'this message mentions a secret but is not a finding'\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clean.py"
            path.write_text(text, encoding="utf-8")
            findings = scan_file(path, "clean.py")
        self.assertEqual(findings, [], findings)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
