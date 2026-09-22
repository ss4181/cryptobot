"""Safety-guard tests: paper/backtest only, no credentials, public hosts only."""

from __future__ import annotations

import unittest

from cryptobot import safety


class TestModeGuard(unittest.TestCase):
    def test_allowed_modes_pass(self):
        self.assertEqual(safety.assert_allowed_mode("paper"), "paper")
        self.assertEqual(safety.assert_allowed_mode("backtest"), "backtest")
        self.assertEqual(safety.assert_allowed_mode("PAPER"), "paper")
        self.assertEqual(safety.assert_allowed_mode(" Backtest "), "backtest")

    def test_live_like_modes_are_rejected(self):
        for mode in ("live", "LIVE", "Live-Trading", "live_trading", "real", "real-money", "realmoney",
                     "production", "prod", "mainnet", "trading", "authenticated", "signed"):
            with self.subTest(mode=mode):
                with self.assertRaises(safety.LiveTradingForbidden):
                    safety.assert_allowed_mode(mode)

    def test_empty_and_unknown_modes_fail_closed(self):
        for mode in (None, "", "   ", "sandbox", "demo?", "42"):
            with self.subTest(mode=mode):
                with self.assertRaises(safety.LiveTradingForbidden):
                    safety.assert_allowed_mode(mode)

    def test_guard_is_a_hard_error(self):
        with self.assertRaises(RuntimeError):
            safety.assert_allowed_mode("live")

    def test_assert_paper_only(self):
        safety.assert_paper_only("live", value=False)
        safety.assert_paper_only("live", value=None)
        with self.assertRaises(safety.LiveTradingForbidden):
            safety.assert_paper_only("CRYPTOBOT_LIVE", value=True)

    def test_live_trading_forbidden_is_a_runtime_error(self):
        self.assertTrue(issubclass(safety.LiveTradingForbidden, safety.SafetyViolation))
        self.assertTrue(issubclass(safety.SafetyViolation, RuntimeError))


class TestEndpointWhitelist(unittest.TestCase):
    def test_public_hosts_allowed(self):
        for url in ("https://api.binance.com/api/v3/klines", "https://data-api.binance.vision/api/v3/klines"):
            self.assertEqual(safety.assert_public_endpoint(url), url)

    def test_private_hosts_rejected(self):
        for url in ("https://api.binance.com/sapi/v1/order", "https://evil.example.com/api/v3/klines",
                    "http://localhost:1234/order", "https://api.binance.com.evil.test/x"):
            with self.subTest(url=url):
                with self.assertRaises(safety.SafetyViolation):
                    safety.assert_public_endpoint(url)

    def test_no_account_endpoints_in_whitelist_docs(self):
        # /sapi (signed) is never allowed, even on an allowed host.
        with self.assertRaises(safety.SafetyViolation):
            safety.assert_public_endpoint("https://api1.binance.com.evil/sapi/v1/account")


class TestCredentialAudit(unittest.TestCase):
    def test_no_credentials_required(self):
        self.assertEqual(safety.audit_credentials({}), ())

    def test_present_credentials_are_detected_but_only_for_warning(self):
        found = safety.audit_credentials({"BINANCE_API_KEY": "x", "UNRELATED": "y"})
        self.assertEqual(found, ("BINANCE_API_KEY",))

    def test_statement_mentions_paper_only(self):
        text = safety.safety_statement()
        self.assertIn("paper/backtest only", text)
        self.assertIn("absent", text)
        self.assertIn("credentials required: none", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
