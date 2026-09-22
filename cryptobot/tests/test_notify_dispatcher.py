"""Notifications: filters, bounded retry, failure isolation, redaction, audit."""

from __future__ import annotations

import json
import logging
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from cryptobot.notify import NotifyConfig, Notifier
from cryptobot.notify import events
from cryptobot.notify.dispatcher import MAX_ATTEMPTS
from cryptobot.notify.http import TransportError
from cryptobot.notify.providers import ConsoleProvider, FileProvider, NtfyProvider
from cryptobot.notify.redact import REDACTED, redact, redact_mapping
from cryptobot.notify.store import NotificationStore
from cryptobot.tests.notify_fixtures import (
    BrokenProvider,
    FlakyProvider,
    LocalSink,
    NetworkRecordingProvider,
    RecordingProvider,
)


def runtime_config(tmp: Path, **overrides) -> NotifyConfig:
    base = NotifyConfig(
        enabled=True,
        providers=("console",),
        notify_on=tuple(events.EVENT_TYPES),
        dedupe_window_seconds=0,
        max_per_hour=1000,
        backoff_initial_seconds=1.0,
        backoff_max_seconds=8.0,
        retry_max=2,
        store_path=tmp / "notifications.jsonl",
    )
    return replace(base, **overrides)


def sample_event(**overrides):
    payload = dict(run_id="run-1", pair="BTC/USDT", entry_price=100.0, exit_price=102.0,
                   net_pnl=1.5, net_pnl_pct=2.0, ts=1_700_000_000_000)
    payload.update(overrides)
    return events.take_profit_hit(**payload)


class TestFailureIsolation(unittest.TestCase):
    def test_broken_provider_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp), providers=[BrokenProvider()])
            records = notifier.notify(sample_event())          # must not raise
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "failed")
            self.assertIn("RuntimeError", records[0]["reason"])

    def test_all_providers_broken_still_returns_records(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp), providers=[BrokenProvider(), BrokenProvider()])
            records = notifier.notify(sample_event())
            self.assertEqual(len(records), 2)
            self.assertTrue(all(r["status"] == "failed" for r in records))

    def test_filter_exception_is_contained(self):
        """Even a bug in a filter must surface as a failed record, not an exception."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp), providers=[RecordingProvider()])
            notifier._suppression_reason = lambda event, force: (_ for _ in ()).throw(RuntimeError("boom"))
            records = notifier.notify(sample_event())
            self.assertEqual(records[0]["status"], "failed")
            self.assertIn("boom", records[0]["reason"])

    def test_transport_error_is_recorded_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)

            class ExplodingTransport:
                def post(self, url, body, headers=None):
                    raise TransportError("URLError: name resolution failed")

            # Loopback host: the injected transport raises before any connection,
            # and the harness guard only refuses non-loopback targets.  Opt in to
            # the TP variant alone so the close message does not supersede it.
            notifier = Notifier(runtime_config(tmp, providers=("ntfy",),
                                               ntfy_host="http://127.0.0.1:9",
                                               ntfy_topic="t", notify_on=("take_profit_hit",)),
                                transport=ExplodingTransport())
            records = notifier.notify(sample_event())
            self.assertEqual(records[0]["status"], "failed")
            self.assertIn("TransportError", records[0]["reason"])


class TestRetryAndBackoff(unittest.TestCase):
    def test_retry_then_success_with_exponential_backoff(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sleeps = []
            provider = FlakyProvider(fail_times=2)
            notifier = Notifier(runtime_config(tmp, retry_max=2), providers=[provider],
                                sleep=sleeps.append)
            records = notifier.notify(sample_event())
            self.assertEqual(records[0]["status"], "sent")
            self.assertEqual(provider.calls, 3)
            self.assertEqual(records[0]["attempts"], 3)
            self.assertEqual(sleeps, [1.0, 2.0])          # 1s then 2s, capped at 8s

    def test_backoff_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sleeps = []
            provider = FlakyProvider(fail_times=4)
            notifier = Notifier(runtime_config(tmp, retry_max=4, backoff_initial_seconds=1.0,
                                               backoff_max_seconds=3.0),
                                providers=[provider], sleep=sleeps.append)
            notifier.notify(sample_event())
            self.assertEqual(sleeps, [1.0, 2.0, 3.0, 3.0])

    def test_retry_count_is_hard_capped(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = FlakyProvider(fail_times=99)
            notifier = Notifier(runtime_config(tmp, retry_max=50), providers=[provider],
                                sleep=lambda _s: None)
            records = notifier.notify(sample_event())
            self.assertEqual(records[0]["status"], "failed")
            self.assertEqual(provider.calls, MAX_ATTEMPTS)

    def test_non_retryable_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = FlakyProvider(fail_times=1, retryable=False)
            notifier = Notifier(runtime_config(tmp, retry_max=3), providers=[provider],
                                sleep=lambda _s: None)
            records = notifier.notify(sample_event())
            self.assertEqual(provider.calls, 1)
            self.assertEqual(records[0]["attempts"], 1)

    def test_real_http_500_is_retried_against_the_sink(self):
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink([500, 503, 200]) as sink:
            tmp = Path(tmp_dir)
            # Opt in to the TP variant alone: with position_closed also enabled
            # the variant is superseded by the close message (one push per exit).
            notifier = Notifier(runtime_config(tmp, providers=("ntfy",), ntfy_host=sink.url,
                                               ntfy_topic="retry-topic", retry_max=3,
                                               notify_on=("take_profit_hit",)),
                                sleep=lambda _s: None)
            records = notifier.notify(sample_event())
            self.assertEqual(records[0]["status"], "sent")
            self.assertEqual(records[0]["attempts"], 3)
            self.assertEqual(records[0]["http_status"], 200)
            self.assertEqual(len(sink.requests), 3)

    def test_real_http_404_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp_dir, LocalSink([404]) as sink:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp, providers=("ntfy",), ntfy_host=sink.url,
                                               ntfy_topic="gone", retry_max=3,
                                               notify_on=("take_profit_hit",)),
                                sleep=lambda _s: None)
            records = notifier.notify(sample_event())
            self.assertEqual(records[0]["status"], "failed")
            self.assertEqual(records[0]["http_status"], 404)
            self.assertEqual(len(sink.requests), 1)


class TestAntiSpam(unittest.TestCase):
    def _notifier(self, tmp: Path, provider: RecordingProvider, **overrides):
        return Notifier(runtime_config(tmp, **overrides), providers=[provider], sleep=lambda _s: None)

    def test_dedupe_suppresses_identical_repeats(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = self._notifier(tmp, provider, dedupe_window_seconds=300)
            first = notifier.notify(sample_event())
            second = notifier.notify(sample_event())
            self.assertEqual(first[0]["status"], "sent")
            self.assertEqual(second[0]["status"], "suppressed")
            self.assertEqual(second[0]["reason"], "dedupe_window")
            self.assertEqual(len(provider.events), 1)

    def test_dedupe_disabled_lets_repeats_through(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = self._notifier(tmp, provider, dedupe_window_seconds=0)
            notifier.notify(sample_event())
            second = notifier.notify(sample_event())
            self.assertEqual(second[0]["status"], "sent")
            self.assertEqual(len(provider.events), 2)

    def test_rate_limit_suppresses_after_max_per_hour(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = NetworkRecordingProvider()   # the cap covers network pushes
            notifier = self._notifier(tmp, provider, max_per_hour=2,
                                      notify_on=("take_profit_hit",))
            statuses = [notifier.notify(sample_event(net_pnl_pct=float(i)))[0]["status"]
                        for i in range(3)]
            self.assertEqual(statuses, ["sent", "sent", "suppressed"])
            self.assertEqual(notifier.notify(sample_event(net_pnl_pct=9.0))[0]["reason"], "max_per_hour")

    def test_critical_events_bypass_the_hourly_cap(self):
        """A noisy hour must never swallow a risk halt / data fail-safe."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = NetworkRecordingProvider()
            notifier = self._notifier(tmp, provider, max_per_hour=1,
                                      notify_on=("take_profit_hit", "risk_halted"))
            self.assertEqual(notifier.notify(sample_event())[0]["status"], "sent")
            # The cap is now full: routine traffic is suppressed ...
            capped = notifier.notify(sample_event(net_pnl_pct=3.0))[0]
            self.assertEqual(capped["reason"], "max_per_hour")
            # ... but a critical event still goes out and is recorded as sent.
            critical = notifier.notify(events.risk_halted(run_id="r", reason="daily loss limit",
                                                          day_realized_net_pnl=-5.0, ts=2))
            self.assertEqual(critical[0]["status"], "sent")
            self.assertIn(critical[0]["severity"], ("critical",))
            self.assertEqual([e.type for e in provider.events].count("risk_halted"), 1)

    def test_critical_events_are_still_deduped_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = NetworkRecordingProvider()
            notifier = self._notifier(tmp, provider, dedupe_window_seconds=300, max_per_hour=1)
            first = notifier.notify(events.risk_halted(run_id="r", reason="daily loss limit", ts=1))
            second = notifier.notify(events.risk_halted(run_id="r", reason="daily loss limit", ts=1))
            self.assertEqual(first[0]["status"], "sent")
            self.assertEqual(second[0]["status"], "suppressed")
            self.assertEqual(second[0]["reason"], "dedupe_window")
            rows = NotificationStore(runtime_config(tmp).store_path).read_all()
            # Both the sent and the suppressed critical are audited.
            self.assertEqual([row["event"] for row in rows], ["risk_halted", "risk_halted"])

    def test_min_severity_filter(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = self._notifier(tmp, provider, min_severity="warning")
            info = notifier.notify(sample_event())                       # take_profit = info
            warning = notifier.notify(events.make_event("equity_drop", "d", "b", severity="warning"))
            self.assertEqual(info[0]["reason"], "below_min_severity")
            self.assertEqual(warning[0]["status"], "sent")

    def test_notify_on_filter(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = self._notifier(tmp, provider, notify_on=("risk_halted",))
            record = notifier.notify(sample_event())[0]
            self.assertEqual(record["status"], "suppressed")
            self.assertEqual(record["reason"], "event_not_enabled")

    def test_quiet_hours_suppresses_non_critical_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = self._notifier(tmp, provider, quiet_hours=(22 * 60, 7 * 60))
            notifier._local_minutes = lambda: 23 * 60      # inside 22:00-07:00
            info = notifier.notify(sample_event())
            critical = notifier.notify(events.make_event("risk_halted", "h", "b", severity="critical"))
            self.assertEqual(info[0]["reason"], "quiet_hours")
            self.assertEqual(critical[0]["status"], "sent")

    def test_quiet_hours_outside_window_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = self._notifier(tmp, provider, quiet_hours=(22 * 60, 7 * 60))
            notifier._local_minutes = lambda: 12 * 60
            self.assertEqual(notifier.notify(sample_event())[0]["status"], "sent")

    def test_disabled_notifier_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = self._notifier(tmp, provider, enabled=False)
            self.assertEqual(notifier.notify(sample_event()), [])
            self.assertEqual(provider.events, [])
            self.assertFalse((tmp / "notifications.jsonl").exists())

    def test_no_active_provider_is_recorded_as_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp, providers=("ntfy",), ntfy_topic=None))
            record = notifier.notify(sample_event())[0]
            self.assertEqual(record["status"], "suppressed")
            self.assertEqual(record["reason"], "no_active_provider")

    def test_state_is_seeded_from_the_audit_file(self):
        """A restart must keep honouring dedupe instead of re-alerting."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = self._notifier(tmp, provider, dedupe_window_seconds=600)
            notifier.notify(sample_event())
            restarted = Notifier(runtime_config(tmp, dedupe_window_seconds=600),
                                 providers=[RecordingProvider()], sleep=lambda _s: None)
            record = restarted.notify(sample_event())[0]
            self.assertEqual(record["reason"], "dedupe_window")


class TestNetworkBudget(unittest.TestCase):
    """The hourly cap is a **network push** budget.

    ``console``/``file`` are local mirrors: they must never consume the budget,
    neither while running nor when the state is re-seeded from the audit file,
    while the cap must still stop the (N+1)th *network* push.
    """

    def _mixed(self, tmp: Path, *, lines: list, mirror: Path, network, max_per_hour: int = 1000) -> Notifier:
        """console + file + one network provider, injected so no socket is opened."""
        return Notifier(
            runtime_config(tmp, providers=("console", "file", "ntfy"), max_per_hour=max_per_hour),
            providers=[ConsoleProvider(writer=lines.append), FileProvider(path=mirror), network],
            sleep=lambda _s: None,
        )

    @staticmethod
    def _opened(index: int):
        """A distinct ``position_opened`` (a new price makes a new dedupe key)."""
        return events.position_opened(run_id="budget-1", pair="BTC/USDT",
                                      entry_price=100.0 + index, qty=1.0,
                                      notional=100.0 + index, stop_price=98.0, tp_price=102.0,
                                      ts=1_700_000_000_000 + index)

    def test_local_console_and_file_burst_never_consumes_the_network_budget(self):
        """30 local mirrors with the cap at 2 leave the network budget untouched."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            lines: list = []
            inactive = NtfyProvider(host="ntfy.example.invalid", topic=None)  # no topic -> inactive
            notifier = self._mixed(tmp, lines=lines, mirror=tmp / "mirror.jsonl", network=inactive,
                                   max_per_hour=2)
            for index in range(30):
                record = notifier.notify(self._opened(index))[0]
                self.assertEqual(record["status"], "sent", "local mirror was capped: {}".format(record))
                self.assertEqual(record["reason"], "")
            self.assertEqual(len(lines), 30)
            budget = notifier.network_budget()
            self.assertEqual(budget["used"], 0)
            self.assertEqual(budget["remaining"], budget["max_per_hour"])
            self.assertFalse(budget["blocks_trade_notification"])

    def test_local_mirroring_does_not_inflate_the_network_budget(self):
        """With cap 5, five events fan out to three providers but cost five units."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            lines: list = []
            network = NetworkRecordingProvider()
            notifier = self._mixed(tmp, lines=lines, mirror=tmp / "mirror.jsonl", network=network,
                                   max_per_hour=5)
            for index in range(5):
                self.assertEqual(notifier.notify(self._opened(index))[0]["status"], "sent")
            # 5 events x 3 providers = 15 'sent' audit rows, but only 5 budget units.
            self.assertEqual(len(lines), 5)
            self.assertEqual(len(network.events), 5)
            self.assertEqual(notifier.network_budget()["used"], 5)
            rows = NotificationStore(tmp / "notifications.jsonl").read_all()
            self.assertEqual(len([r for r in rows if r["status"] == "sent"]), 15)

    def test_seeded_local_sent_rows_do_not_suppress_a_network_send(self):
        """A history full of local 'sent' rows must not mute the phone."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            store = tmp / "notifications.jsonl"
            now_ms = int(time.time() * 1000)
            seeded = []
            for index in range(40):                      # twice the default cap
                seeded.append({"ts": now_ms - index * 1000, "status": "sent", "event": "position_opened",
                               "provider": "console" if index % 2 else "file",
                               "target": "" if index % 2 else str(store),
                               "dedupe_key": "seed-local-{}|info|x|y".format(index)})
            store.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in seeded),
                             encoding="utf-8")
            network = NetworkRecordingProvider()
            notifier = Notifier(runtime_config(tmp, providers=("ntfy",)), providers=[network],
                                sleep=lambda _s: None)
            self.assertEqual(notifier.network_budget()["used"], 0, "local rows must not seed the budget")
            record = notifier.notify(self._opened(1))[0]
            self.assertEqual(record["status"], "sent", record["reason"])
            self.assertEqual(len(network.events), 1)

    def test_seeded_network_rows_do_still_count(self):
        """The trailing hour of real pushes is honoured across a restart."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            store = tmp / "notifications.jsonl"
            now_ms = int(time.time() * 1000)
            seeded = [
                # outside the trailing hour -> ignored
                {"ts": now_ms - 7_200_000, "status": "sent", "provider": "ntfy",
                 "target": "https://ntfy.sh/legacy", "dedupe_key": "seed-old|info|x|y"},
                # http(s) target on a real host -> network row
                {"ts": now_ms - 20_000, "status": "sent", "provider": "unknown-history",
                 "target": "https://ntfy.sh/legacy", "dedupe_key": "seed-net-1|info|x|y"},
                # requires_network provider, even when pointed at loopback -> network row
                {"ts": now_ms - 10_000, "status": "sent", "provider": "recording-network",
                 "target": "http://127.0.0.1:9/topic", "dedupe_key": "seed-net-2|info|x|y"},
                # loopback target from an unknown provider -> NOT a network row
                {"ts": now_ms - 9_000, "status": "sent", "provider": "unknown-history",
                 "target": "http://127.0.0.1:9/topic", "dedupe_key": "seed-local-x|info|x|y"},
                # local mirror rows -> NOT network rows
                {"ts": now_ms - 8_000, "status": "sent", "provider": "console", "target": "",
                 "dedupe_key": "seed-local-1|info|x|y"},
                {"ts": now_ms - 7_000, "status": "sent", "provider": "file", "target": str(store),
                 "dedupe_key": "seed-local-2|info|x|y"},
                {"ts": now_ms - 6_000, "status": "failed", "provider": "ntfy",
                 "target": "https://ntfy.sh/legacy", "dedupe_key": "seed-failed|info|x|y"},
            ]
            # The store is append-only, so its rows are in the order they were written.
            seeded.sort(key=lambda row: row["ts"])
            store.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in seeded),
                             encoding="utf-8")
            notifier = Notifier(runtime_config(tmp, providers=("console", "ntfy")),
                                providers=[RecordingProvider(), NetworkRecordingProvider()],
                                sleep=lambda _s: None)
            self.assertEqual(notifier.network_budget()["used"], 2)

    def test_cap_still_blocks_the_next_network_send(self):
        """The fix must not remove the cap: the (N+1)th network push is suppressed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            network = NetworkRecordingProvider()
            notifier = Notifier(
                runtime_config(tmp, providers=("console", "ntfy"), max_per_hour=3,
                               dedupe_window_seconds=0),
                providers=[ConsoleProvider(writer=lambda _line: None), network],
                sleep=lambda _s: None)
            statuses = [notifier.notify(self._opened(index))[0]["status"] for index in range(4)]
            self.assertEqual(statuses, ["sent", "sent", "sent", "suppressed"])
            blocked = notifier.notify(self._opened(99))[0]
            self.assertEqual(blocked["reason"], "max_per_hour")
            self.assertEqual(len(network.events), 3)       # the 4th never reached the provider
            budget = notifier.network_budget()
            self.assertEqual((budget["used"], budget["remaining"]), (3, 0))
            self.assertTrue(budget["blocks_trade_notification"])

    def test_critical_still_bypasses_the_network_cap(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            network = NetworkRecordingProvider()
            notifier = Notifier(runtime_config(tmp, providers=("ntfy",), max_per_hour=1),
                                providers=[network], sleep=lambda _s: None)
            self.assertEqual(notifier.notify(self._opened(0))[0]["status"], "sent")
            self.assertTrue(notifier.network_budget()["blocks_trade_notification"])
            critical = notifier.notify(events.risk_halted(run_id="r", reason="daily loss limit", ts=2))
            self.assertEqual(critical[0]["status"], "sent")
            self.assertEqual([event.type for event in network.events],
                             ["position_opened", "risk_halted"])

    def test_dedupe_still_applies_to_network_sends(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            network = NetworkRecordingProvider()
            notifier = Notifier(runtime_config(tmp, providers=("ntfy",), dedupe_window_seconds=300),
                                providers=[network], sleep=lambda _s: None)
            self.assertEqual(notifier.notify(self._opened(0))[0]["status"], "sent")
            repeat = notifier.notify(self._opened(0))[0]
            self.assertEqual(repeat["status"], "suppressed")
            self.assertEqual(repeat["reason"], "dedupe_window")
            self.assertEqual(len(network.events), 1)


class TestDryRunDispatch(unittest.TestCase):
    def test_dry_run_calls_no_provider_but_records_the_preview(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = Notifier(runtime_config(tmp, dry_run=True), providers=[provider])
            records = notifier.notify(sample_event())
            self.assertEqual(provider.events, [])
            self.assertEqual(records[0]["status"], "dry_run")
            self.assertIn("preview", records[0])
            self.assertTrue((tmp / "notifications.jsonl").exists())

    def test_cli_style_dry_run_override(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = RecordingProvider()
            notifier = Notifier(runtime_config(tmp), providers=[provider])
            records = notifier.notify(sample_event(), dry_run=True)
            self.assertEqual(records[0]["status"], "dry_run")
            self.assertEqual(provider.events, [])


class TestAuditPersistence(unittest.TestCase):
    def test_every_attempt_is_persisted_with_the_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            provider = NetworkRecordingProvider()
            notifier = Notifier(runtime_config(tmp, max_per_hour=1, notify_on=("take_profit_hit",)),
                                providers=[provider])
            notifier.notify(sample_event())
            notifier.notify(sample_event(net_pnl_pct=3.0))       # suppressed by rate limit
            rows = NotificationStore(tmp / "notifications.jsonl").read_all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["status"], "sent")
            self.assertEqual(rows[1]["status"], "suppressed")
            for row in rows:
                for field in ("ts", "iso_utc", "event", "severity", "provider", "status",
                              "reason", "http_status", "latency_ms", "attempts"):
                    self.assertIn(field, row)
                self.assertEqual(row["event"], "take_profit_hit")
                self.assertEqual(row["severity"], "info")

    def test_export_csv_and_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            notifier = Notifier(runtime_config(tmp), providers=[RecordingProvider()])
            notifier.notify(sample_event())
            store = NotificationStore(tmp / "notifications.jsonl")
            csv_path = store.export_csv(tmp / "out" / "n.csv")
            json_path = store.export_json(tmp / "out" / "n.json")
            self.assertIn("take_profit_hit", csv_path.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["status"], "sent")

    def test_truncated_last_line_does_not_break_reads(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            store = NotificationStore(tmp / "notifications.jsonl")
            store.append({"ts": 1, "event": "x", "status": "sent"})
            with (tmp / "notifications.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('{"ts": 2, "event": "trunc')
            rows = store.read_all()
            self.assertEqual(len(rows), 1)


class TestSecretRedaction(unittest.TestCase):
    def test_redact_helper(self):
        secrets = ("tk-secret-123", "topic-abcdef")
        text = "POST https://ntfy.sh/topic-abcdef with Bearer tk-secret-123"
        cleaned = redact(text, secrets=secrets)
        self.assertNotIn("tk-secret-123", cleaned)
        self.assertNotIn("topic-abcdef", cleaned)
        self.assertIn(REDACTED, cleaned)
        self.assertEqual(redact_mapping({"Authorization": "Bearer tk-secret-123"},
                                        secrets=secrets)["Authorization"], "Bearer " + REDACTED)

    def test_telegram_url_pattern_is_masked(self):
        self.assertNotIn("AAbbccddeeff", redact("https://api.telegram.org/bot123456:AAbbccddeeff/sendMessage"))

    def test_token_never_reaches_store_logs_or_error_message(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            token = "123456:SECRET-TOKEN-VALUE"
            captured = []

            class CaptureHandler(logging.Handler):
                def emit(self, record):
                    captured.append(self.format(record))

            handler = CaptureHandler()
            logger = logging.getLogger("cryptobot.notify")
            logger.addHandler(handler)
            old_level = logger.level
            logger.setLevel(logging.DEBUG)
            try:
                class LeakyTransport:
                    def post(self, url, body, headers=None):
                        raise TransportError("connect failed for {}".format(url))

                notifier = Notifier(
                    runtime_config(tmp, providers=("telegram",), telegram_bot_token=token,
                                   telegram_chat_id="42",
                                   telegram_api_base="http://127.0.0.1:9",
                                   notify_on=("take_profit_hit",)),
                    transport=LeakyTransport(),
                )
                records = notifier.notify(sample_event())
                records += notifier.notify(sample_event(net_pnl_pct=1.0), dry_run=True)
            finally:
                logger.removeHandler(handler)
                logger.setLevel(old_level)

            blob = json.dumps(records) + "".join(captured)
            audit = (tmp / "notifications.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("SECRET-TOKEN-VALUE", blob)
            self.assertNotIn("SECRET-TOKEN-VALUE", audit)
            self.assertEqual(records[0]["status"], "failed")   # still recorded, without the token


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
