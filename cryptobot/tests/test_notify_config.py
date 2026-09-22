"""Notifications: config parsing, validation, env overrides and secret hygiene."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cryptobot.config import (
    ALLOWED_NOTIFY_PROVIDERS,
    DEFAULT_CONFIG_PATH,
    DEFAULTS,
    ConfigError,
    assert_no_secrets_in_config,
    load_config,
    validate,
)
from cryptobot.notify import NotifyConfig


def _cfg(**notifications):
    raw = dict(DEFAULTS)
    raw["notifications"] = {**DEFAULTS["notifications"], **notifications}
    return validate(raw)


class TestNotificationDefaults(unittest.TestCase):
    def test_shipped_config_defaults(self):
        config = load_config(environ={})
        n = config.notifications
        self.assertTrue(n.enabled)
        self.assertIn("console", n.providers)
        self.assertIn("file", n.providers)
        self.assertIn("ntfy", n.providers)
        self.assertIsNone(n.ntfy_topic)          # ntfy stays inactive until set
        self.assertEqual(n.ntfy_host, "ntfy.sh")
        self.assertEqual(n.min_severity, "info")
        self.assertEqual(n.dedupe_window_seconds, 300)
        self.assertEqual(n.max_per_hour, 20)
        self.assertIsNone(n.quiet_hours)
        self.assertFalse(n.dry_run)
        self.assertEqual(n.timeout_seconds, 10.0)
        self.assertEqual(n.retry_max, 2)
        self.assertGreater(n.equity_drop_pct, 0)
        # Trade-only shipping default: exactly the two trade events, so the phone
        # only buzzes when a position opens and when it closes.
        self.assertEqual(tuple(n.notify_on), ("position_opened", "position_closed"))
        self.assertIn("position_closed", n.notify_on)
        # Everything else stays implemented but filtered out by default.
        for event_type in ("bot_started", "bot_stopped", "risk_halted", "cooldown_started",
                           "feed_outage", "data_fail_safe", "daily_summary", "equity_drop",
                           "take_profit_hit", "stop_loss_hit"):
            self.assertNotIn(event_type, n.notify_on)

    def test_notifications_are_not_part_of_the_metric_echo(self):
        """Adding the layer must not move the backtest determinism hash."""
        config = load_config(environ={})
        self.assertNotIn("notifications", config.as_dict())

    def test_shipped_config_still_has_no_secrets(self):
        assert_no_secrets_in_config(DEFAULT_CONFIG_PATH)


class TestNotificationValidation(unittest.TestCase):
    def test_quiet_hours_string(self):
        self.assertEqual(_cfg(quiet_hours="22:00-07:00").notifications.quiet_hours, (1320, 420))

    def test_quiet_hours_mapping(self):
        self.assertEqual(
            _cfg(quiet_hours={"start": "01:30", "end": "05:00"}).notifications.quiet_hours, (90, 300))

    def test_quiet_hours_null(self):
        self.assertIsNone(_cfg(quiet_hours=None).notifications.quiet_hours)

    def test_bad_quiet_hours_rejected(self):
        for value in ("nonsense", "25:00-07:00", "22:00"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    _cfg(quiet_hours=value)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            _cfg(providers=["console", "carrier-pigeon"])
        self.assertIn("carrier-pigeon", str(ctx.exception))

    def test_empty_providers_rejected(self):
        with self.assertRaises(ConfigError):
            _cfg(providers=[])

    def test_bad_severity_rejected(self):
        with self.assertRaises(ConfigError):
            _cfg(min_severity="loud")

    def test_numeric_bounds(self):
        with self.assertRaises(ConfigError):
            _cfg(max_per_hour=0)
        with self.assertRaises(ConfigError):
            _cfg(retry_max=99)
        with self.assertRaises(ConfigError):
            _cfg(timeout_seconds=0)
        with self.assertRaises(ConfigError):
            _cfg(dedupe_window_seconds=-1)

    def test_allowed_provider_list_is_what_the_code_implements(self):
        self.assertEqual(set(ALLOWED_NOTIFY_PROVIDERS), {"ntfy", "telegram", "webhook", "console", "file"})


class TestNotificationEnvOverrides(unittest.TestCase):
    def test_env_overrides(self):
        config = load_config(environ={
            "CRYPTOBOT_NOTIFY_ENABLED": "0",
            "CRYPTOBOT_NOTIFY_DRY_RUN": "1",
            "CRYPTOBOT_NOTIFY_PROVIDERS": "file, console",
            "CRYPTOBOT_NOTIFY_MIN_SEVERITY": "warning",
            "CRYPTOBOT_NOTIFY_MAX_PER_HOUR": "5",
            "CRYPTOBOT_NOTIFY_DEDUPE_SECONDS": "0",
            "CRYPTOBOT_NOTIFY_QUIET_HOURS": "23:00-06:00",
            "CRYPTOBOT_NTFY_TOPIC": "my-topic",
            "CRYPTOBOT_NTFY_HOST": "ntfy.example.org",
        })
        n = config.notifications
        self.assertFalse(n.enabled)
        self.assertTrue(n.dry_run)
        self.assertEqual(n.providers, ("file", "console"))
        self.assertEqual(n.min_severity, "warning")
        self.assertEqual(n.max_per_hour, 5)
        self.assertEqual(n.dedupe_window_seconds, 0)
        self.assertEqual(n.quiet_hours, (1380, 360))
        self.assertEqual(n.ntfy_topic, "my-topic")
        self.assertEqual(n.ntfy_host, "ntfy.example.org")

    def test_runtime_config_reads_secrets_from_env_only(self):
        config = load_config(environ={})
        runtime = NotifyConfig.from_config(config, environ={
            "CRYPTOBOT_NTFY_TOPIC": "topic-abc",
            "CRYPTOBOT_NTFY_TOKEN": "tk-123456",
            "CRYPTOBOT_TELEGRAM_BOT_TOKEN": "123456:AAbbcc",
            "CRYPTOBOT_TELEGRAM_CHAT_ID": "42",
            "CRYPTOBOT_WEBHOOK_URL": "https://example.invalid/hook",
        })
        self.assertEqual(runtime.ntfy_topic, "topic-abc")
        self.assertEqual(runtime.ntfy_token, "tk-123456")
        self.assertEqual(runtime.telegram_bot_token, "123456:AAbbcc")
        self.assertEqual(runtime.telegram_chat_id, "42")
        self.assertEqual(runtime.webhook_url, "https://example.invalid/hook")
        self.assertEqual(runtime.store_path, Path(config.logs_dir) / "notifications.jsonl")
        self.assertIn("tk-123456", runtime.secret_values())

    def test_secrets_are_not_in_the_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
            path.write_text(text, encoding="utf-8")
            assert_no_secrets_in_config(path)  # raises if a forbidden word sneaks in


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
