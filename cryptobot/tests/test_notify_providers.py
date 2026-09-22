"""Notifications: provider payload shapes, proven against a real local HTTP sink."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cryptobot.notify import NotifyConfig, Notifier
from cryptobot.notify import events
from cryptobot.notify import render as render_mod
from cryptobot.notify.providers import (
    ConsoleProvider,
    FileProvider,
    NtfyProvider,
    PRIORITY_BY_SEVERITY,
    TAGS_BY_SEVERITY,
    TelegramProvider,
    WebhookProvider,
)
from cryptobot.tests.notify_fixtures import LocalSink

render_body = render_mod.render_body


def runtime_config(tmp: Path, **overrides) -> NotifyConfig:
    base = NotifyConfig(
        enabled=True,
        providers=("console",),
        notify_on=tuple(events.EVENT_TYPES),
        dedupe_window_seconds=0,
        max_per_hour=1000,
        store_path=tmp / "notifications.jsonl",
    )
    return replace(base, **overrides)


class TestNtfyProvider(unittest.TestCase):
    def test_round_trip_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink() as sink:
            tmp = Path(tmp_dir)
            cfg = runtime_config(
                tmp, providers=("ntfy",), ntfy_host=sink.url, ntfy_topic="kripto-test-2026",
                ntfy_token="tk-secret-123", ntfy_click="https://example.invalid/app",
                # stop_loss_hit is a position_closed variant: opt in to it alone,
                # otherwise the close message supersedes it (one push per exit).
                notify_on=("stop_loss_hit",),
            )
            notifier = Notifier(cfg)
            event = events.stop_loss_hit(run_id="r1", pair="BTC/USDT", entry_price=100.0,
                                         exit_price=97.5, net_pnl=-1.25, net_pnl_pct=-2.74, ts=1)
            records = notifier.notify(event)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "sent")
            self.assertEqual(records[0]["http_status"], 200)

            self.assertEqual(sink.paths(), ["/kripto-test-2026"])
            self.assertEqual(sink.header("Title"), "STOP-LOSS: BTCUSDT")
            self.assertEqual(sink.header("Priority"), "4")           # warning
            self.assertEqual(sink.header("Tags"), "warning")
            self.assertEqual(sink.header("Authorization"), "Bearer tk-secret-123")
            self.assertEqual(sink.header("Click"), "https://example.invalid/app")
            # ntfy only renders the bold sections when Markdown is enabled.
            self.assertEqual(sink.header("Markdown"), "yes")
            body = sink.bodies()[0]
            self.assertTrue(body.startswith("**"), body)
            self.assertIn("**🟨 BTCUSDT · STOP ✗ (PAPER)**", body)
            self.assertIn("Net:", body)

    def test_priority_and_tags_follow_severity(self):
        provider = NtfyProvider(host="ntfy.sh", topic="t", token="x", mono=lambda: 0.0)
        info = provider.render(events.make_event("daily_summary", "t", "b", severity="info"))
        critical = provider.render(events.make_event("risk_halted", "t", "b", severity="critical"))
        self.assertEqual(info.headers["Priority"], str(PRIORITY_BY_SEVERITY["info"]))
        self.assertEqual(critical.headers["Priority"], str(PRIORITY_BY_SEVERITY["critical"]))
        self.assertEqual(critical.headers["Tags"], TAGS_BY_SEVERITY["critical"])
        self.assertIn("Bearer x", info.headers["Authorization"])

    def test_title_header_is_ascii_and_single_line(self):
        provider = NtfyProvider(host="ntfy.sh", topic="t", mono=lambda: 0.0)
        rendered = provider.render(events.make_event("x", "UYARI şğü\nBAŞLIK", "b"))
        self.assertNotIn("\n", rendered.headers["Title"])
        rendered.headers["Title"].encode("ascii")  # must not raise

    def test_inactive_without_topic(self):
        active, reason = NtfyProvider(host="ntfy.sh", topic=None).active()
        self.assertFalse(active)
        self.assertIn("CRYPTOBOT_NTFY_TOPIC", reason)

    def test_self_hosted_scheme_is_preserved(self):
        provider = NtfyProvider(host="http://127.0.0.1:8080", topic="t", mono=lambda: 0.0)
        self.assertTrue(provider.render(events.make_event("x", "t", "b")).url.startswith("http://127.0.0.1:8080/"))


class TestTelegramProvider(unittest.TestCase):
    def test_round_trip_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink() as sink:
            tmp = Path(tmp_dir)
            cfg = runtime_config(
                tmp, providers=("telegram",), telegram_bot_token="123456:AAbbccdd",
                telegram_chat_id="4242", telegram_api_base=sink.url,
            )
            notifier = Notifier(cfg)
            event = events.position_opened(run_id="r2", pair="ETH/USDT", entry_price=2300.0, qty=0.02,
                                           notional=46.0, stop_price=2242.5, tp_price=2351.9, ts=2)
            records = notifier.notify(event)

            self.assertEqual(records[0]["status"], "sent")
            self.assertEqual(sink.paths(), ["/bot123456:AAbbccdd/sendMessage"])
            payload = json.loads(sink.bodies()[0])
            self.assertEqual(payload["chat_id"], "4242")
            self.assertEqual(payload["parse_mode"], "HTML")
            # Telegram gets the HTML carrier: bold sections, no markdown asterisks.
            self.assertIn("<b>", payload["text"])
            self.assertIn("ETHUSDT", payload["text"])
            self.assertIn("LONG (PAPER)", payload["text"])
            self.assertNotIn("**", payload["text"])
            # The audit record must never carry the bot token.
            self.assertNotIn("123456:AAbbccdd", json.dumps(records[0]))
            self.assertNotIn("123456:AAbbccdd", (tmp / "notifications.jsonl").read_text(encoding="utf-8"))

    def test_inactive_without_credentials(self):
        provider = TelegramProvider(bot_token="", chat_id="42")
        active, reason = provider.active()
        self.assertFalse(active)
        self.assertIn("CRYPTOBOT_TELEGRAM_BOT_TOKEN", reason)


class TestWebhookProvider(unittest.TestCase):
    def test_round_trip_envelope(self):
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink() as sink:
            tmp = Path(tmp_dir)
            cfg = runtime_config(tmp, providers=("webhook",), webhook_url=sink.url + "/hook")
            notifier = Notifier(cfg)
            event = events.daily_summary(run_id="r3", day="2026-09-12", equity=48.5, trades=4,
                                         wins=3, win_rate_pct=75.0, net_pnl=1.25, ts=3)
            records = notifier.notify(event)

            self.assertEqual(records[0]["status"], "sent")
            self.assertEqual(sink.paths(), ["/hook"])
            envelope = json.loads(sink.bodies()[0])
            for key in ("source", "event", "severity", "title", "text", "content", "ts", "run_id", "data"):
                self.assertIn(key, envelope)
            self.assertEqual(envelope["source"], "cryptobot")
            self.assertEqual(envelope["event"], "daily_summary")
            self.assertEqual(envelope["severity"], "info")
            self.assertEqual(envelope["content"], envelope["text"])   # Discord-compatible

    def test_inactive_without_url(self):
        self.assertFalse(WebhookProvider(url="").active()[0])
        self.assertFalse(WebhookProvider(url="ftp://nope").active()[0])


class TestLocalProviders(unittest.TestCase):
    def test_console_writes_the_plain_rendering(self):
        lines = []
        provider = ConsoleProvider(writer=lines.append)
        event = events.equity_drop(run_id="r", peak_equity=50.0, equity=47.5, drop_pct=5.0,
                                   threshold_pct=3.0, ts=4)
        result = provider.send(event)
        self.assertTrue(result.ok)
        self.assertEqual(len(lines), 1)
        text = lines[0]
        self.assertIn("EQUITY DÜŞÜŞÜ", text)
        self.assertIn("Sermaye uyarısı", text)
        self.assertIn("-%5,00", text)
        # The console carrier is clean plain text: no markdown/HTML markup.
        self.assertNotIn("**", text)
        self.assertNotIn("<b>", text)

    def test_file_provider_writes_the_audit_line(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            cfg = runtime_config(tmp, providers=("file",))
            notifier = Notifier(cfg)
            notifier.notify(events.bot_started(run_id="r4", mode="paper", pairs=["BTC/USDT"],
                                               timeframe="1h", initial_capital=50.0, ts=5))
            store = tmp / "notifications.jsonl"
            self.assertTrue(store.exists())
            rows = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["provider"], "file")
            self.assertEqual(rows[0]["status"], "sent")
            self.assertEqual(rows[0]["event"], "bot_started")
            self.assertIn("BOT BASLADI", rows[0]["title"])

    def test_console_and_file_are_always_active(self):
        self.assertEqual(ConsoleProvider().active(), (True, ""))
        self.assertEqual(FileProvider(path=Path("x")).active(), (True, ""))


class TestDryRunRendering(unittest.TestCase):
    def test_dry_run_renders_exactly_what_would_be_sent(self):
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink() as sink:
            tmp = Path(tmp_dir)
            cfg = runtime_config(tmp, providers=("ntfy",), ntfy_host=sink.url,
                                 ntfy_topic="dry", ntfy_token="tk-dry-secret", dry_run=True)
            notifier = Notifier(cfg)
            event = events.bot_stopped(run_id="r5", final_equity=47.4, net_pnl=-2.57, net_pnl_pct=-5.13,
                                       trades=18, win_rate_pct=50.0, ts=6)
            records = notifier.notify(event)

            self.assertEqual(sink.requests, [])                       # nothing was sent
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["status"], "dry_run")
            self.assertTrue(record["dry_run"])
            preview = record["preview"]
            self.assertEqual(preview["method"], "POST")
            self.assertTrue(preview["url"].endswith("/dry"))
            self.assertEqual(preview["body"], render_body(event, "markdown"))
            self.assertIn("**", preview["body"])
            self.assertIn("Markdown", preview["headers"])
            self.assertEqual(preview["headers"]["Markdown"], "yes")
            self.assertIn("Priority", preview["headers"])
            # secrets are masked even in the preview
            self.assertNotIn("tk-dry-secret", json.dumps(preview))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
