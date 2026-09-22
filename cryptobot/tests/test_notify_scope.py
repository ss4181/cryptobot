"""Notification scope + the structural no-real-send guarantees.

These tests encode the two promises made to the user:

* the shipped scope is *trade events only* (position opens / closes), and
* automation (this suite, verify_all, acceptance_check, notify_check) can never
  publish to a real ntfy/telegram/webhook host again -- even when a persistent
  user-level ``CRYPTOBOT_NTFY_TOPIC`` is present in the environment.

The proof strategy is a recording transport: if the guard works, the transport
is *never called*, so a real socket can never be opened.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from cryptobot.config import DEFAULTS
from cryptobot.notify import NotifyConfig, Notifier, events
from cryptobot.notify.guard import (
    HARNESS_ENV,
    REASON_HARNESS,
    REASON_REPLAY,
    REASON_SUPERSEDED,
    harness_mode,
    host_is_local,
    network_guard_reason,
)
from cryptobot.notify.providers import NtfyProvider
from cryptobot.notify.store import NotificationStore
from cryptobot.tests.notify_fixtures import LocalSink, RecordingProvider


class TrackingTransport:
    """Records every POST; returns HTTP 200. A call means a real send happened."""

    def __init__(self) -> None:
        self.calls = []

    def post(self, url, body, headers=None):
        from cryptobot.notify.http import HttpResponse

        self.calls.append({"url": url, "body": body, "headers": dict(headers or {})})
        return HttpResponse(status=200, body="ok", headers={})


def runtime_config(tmp: Path, **overrides) -> NotifyConfig:
    base = NotifyConfig(
        enabled=True,
        providers=("ntfy",),
        notify_on=("position_opened", "position_closed"),
        dedupe_window_seconds=0,
        max_per_hour=1000,
        store_path=tmp / "notifications.jsonl",
    )
    return replace(base, **overrides)


def opened(**overrides):
    payload = dict(run_id="scope-1", pair="BTC/USDT", entry_price=100.0, qty=1.0,
                   notional=100.0, stop_price=98.0, tp_price=102.0, ts=1_700_000_000_000)
    payload.update(overrides)
    return events.position_opened(**payload)


class TestTradeOnlyScope(unittest.TestCase):
    def test_shipped_default_is_trade_only(self):
        self.assertEqual(list(DEFAULTS["notifications"]["notify_on"]),
                         ["position_opened", "position_closed"])

    def test_non_trade_events_are_filtered_by_default(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp), providers=[RecordingProvider()])
            for factory, kwargs in (
                (events.bot_started, {"run_id": "r", "mode": "paper", "pairs": ["BTC/USDT"],
                                      "timeframe": "1h", "initial_capital": 100.0}),
                (events.risk_halted, {"run_id": "r", "reason": "limit"}),
                (events.daily_summary, {"run_id": "r", "day": "2026-09-13", "equity": 100.0,
                                        "trades": 0, "wins": 0, "win_rate_pct": 0.0, "net_pnl": 0.0}),
            ):
                record = notifier.notify(factory(**kwargs))[0]
                self.assertEqual(record["status"], "suppressed", factory.__name__)
                self.assertEqual(record["reason"], "event_not_enabled", factory.__name__)

    def test_trade_events_still_deliver(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = Notifier(runtime_config(tmp), providers=[provider])
            record = notifier.notify(opened())[0]
            self.assertEqual(record["status"], "sent")
            self.assertEqual([event.type for event in provider.events], ["position_opened"])


class TestHarnessGuard(unittest.TestCase):
    """The decisive guarantee: automation cannot reach a real notification host."""

    REAL_TOPIC = "cryptobot-paper-real-looking-6c6de613"
    REAL_HOST = "ntfy.envs.net"

    def test_suite_process_is_marked_as_harness(self):
        self.assertTrue(harness_mode(), "tests/__init__ must arm the guard")

    def test_real_topic_in_env_produces_zero_network_sends(self):
        """Set a persistent-looking topic; the transport must never be touched."""
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(os.environ, {
                "CRYPTOBOT_NTFY_TOPIC": self.REAL_TOPIC,
                "CRYPTOBOT_NTFY_HOST": self.REAL_HOST}):
            tmp = Path(tmp_dir)
            transport = TrackingTransport()
            notifier = Notifier(runtime_config(tmp, ntfy_host=self.REAL_HOST,
                                               ntfy_topic=self.REAL_TOPIC),
                                transport=transport)
            records = notifier.notify(events.position_closed(
                run_id="r", pair="BTC/USDT", entry_price=100.0, exit_price=101.0, qty=1.0,
                fees=0.1, gross_pnl=1.0, net_pnl=0.9, net_pnl_pct=0.9, ts=1))
            notifier.notify(opened())

            self.assertEqual(transport.calls, [], "guard must prevent the HTTP POST")
            self.assertTrue(records)
            for record in records:
                self.assertEqual(record["status"], "suppressed")
                self.assertEqual(record["reason"], REASON_HARNESS)
            # Nothing in the audit store claims a network send.
            store = NotificationStore(tmp / "notifications.jsonl")
            self.assertFalse([r for r in store.read_all() if r.get("status") == "sent"])

    def test_loopback_sink_is_still_allowed(self):
        """The guard is host-based: the offline sink must keep working."""
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink() as sink:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp, ntfy_host=sink.url, ntfy_topic="local"))
            record = notifier.notify(opened())[0]
            self.assertEqual(record["status"], "sent")
            self.assertEqual(len(sink.requests), 1)

    def test_guard_reason_helper(self):
        self.assertEqual(network_guard_reason("https://ntfy.sh", environ={"CRYPTOBOT_NOTIFY_HARNESS": "1"}),
                         REASON_HARNESS)
        self.assertEqual(network_guard_reason("https://ntfy.sh", environ={}), "")
        self.assertEqual(network_guard_reason("http://127.0.0.1:9999",
                                              environ={"CRYPTOBOT_NOTIFY_HARNESS": "1"}), "")
        self.assertEqual(network_guard_reason("https://ntfy.sh", requires_network=False,
                                              environ={"CRYPTOBOT_NOTIFY_HARNESS": "1"}), "")
        self.assertTrue(host_is_local("localhost:8080"))
        self.assertTrue(host_is_local("127.0.0.12"))
        self.assertFalse(host_is_local("ntfy.envs.net"))

    def test_status_reports_that_real_sending_is_impossible(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp, ntfy_host=self.REAL_HOST,
                                               ntfy_topic=self.REAL_TOPIC),
                                transport=TrackingTransport())
            possible, reason = notifier.can_send_to_network()
            self.assertFalse(possible)
            self.assertEqual(reason, REASON_HARNESS)


class TestReplayNoPush(unittest.TestCase):
    """``run --replay``/``--offline`` must not push unless explicitly told to."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {HARNESS_ENV: ""})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_replay_blocks_a_real_host_and_audits_it(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            transport = TrackingTransport()
            notifier = Notifier(runtime_config(tmp, ntfy_host="ntfy.envs.net",
                                               ntfy_topic="real", network_send_allowed=False,
                                               network_block_reason=REASON_REPLAY),
                                transport=transport)
            record = notifier.notify(opened())[0]
            self.assertEqual(transport.calls, [])
            self.assertEqual(record["status"], "suppressed")
            self.assertEqual(record["reason"], REASON_REPLAY)

    def test_replay_opt_in_restores_sending(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            transport = TrackingTransport()
            notifier = Notifier(runtime_config(tmp, ntfy_host="ntfy.envs.net",
                                               ntfy_topic="real", network_send_allowed=True),
                                transport=transport)
            record = notifier.notify(opened())[0]
            self.assertEqual(record["status"], "sent")
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(transport.calls[0]["url"].startswith("https://ntfy.envs.net/"))


class TestSingleMessagePerClose(unittest.TestCase):
    """A TP/SL exit is one push: the close message, not the detail variant."""

    def test_variant_is_superseded_when_close_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink() as sink:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp, ntfy_host=sink.url, ntfy_topic="t",
                                               notify_on=("position_closed", "take_profit_hit")))
            payload = dict(run_id="r", pair="BTC/USDT", entry_price=100.0, exit_price=102.0,
                           net_pnl=1.5, net_pnl_pct=2.0, ts=7)
            close = notifier.notify(events.position_closed(qty=1.0, fees=0.1, gross_pnl=2.0,
                                                           **payload))[0]
            variant = notifier.notify(events.take_profit_hit(**payload))[0]

            self.assertEqual(close["status"], "sent")
            self.assertEqual(variant["status"], "suppressed")
            self.assertEqual(variant["reason"], REASON_SUPERSEDED)
            # Exactly one HTTP POST for one exit.
            self.assertEqual(len(sink.requests), 1)

    def test_variant_delivers_when_close_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink() as sink:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp, ntfy_host=sink.url, ntfy_topic="t",
                                               notify_on=("take_profit_hit",)))
            record = notifier.notify(events.take_profit_hit(
                run_id="r", pair="BTC/USDT", entry_price=100.0, exit_price=102.0,
                net_pnl=1.5, net_pnl_pct=2.0, ts=7))[0]
            self.assertEqual(record["status"], "sent")
            self.assertEqual(len(sink.requests), 1)

    def test_force_overrides_supersession(self):
        """`notify preview --send` / `notify test` are explicit: they always go out."""
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink() as sink:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp, ntfy_host=sink.url, ntfy_topic="t",
                                               notify_on=("position_closed", "take_profit_hit")))
            record = notifier.notify(events.take_profit_hit(
                run_id="r", pair="BTC/USDT", entry_price=100.0, exit_price=102.0,
                net_pnl=1.5, net_pnl_pct=2.0, ts=7), force=True)[0]
            self.assertEqual(record["status"], "sent")
            self.assertEqual(len(sink.requests), 1)


class TestProviderGuardTarget(unittest.TestCase):
    def test_guard_targets_are_the_configured_hosts(self):
        self.assertTrue(NtfyProvider(host="ntfy.envs.net", topic="t").guard_target()
                        .startswith("https://ntfy.envs.net"))
        self.assertEqual(NtfyProvider(host="http://127.0.0.1:5", topic="t").guard_target(),
                         "http://127.0.0.1:5")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
