"""Notification self-check: one command, offline, machine-checkable evidence.

Mirrors ``scripts/acceptance_check.py`` in spirit: it measures the notification
layer end to end and exits non-zero if anything is off.  Everything is offline --
the "network" providers are pointed at a real ``http.server`` sink bound to
127.0.0.1, so the HTTP client, retry logic, payload shapes and audit trail are all
exercised for real, just without leaving the machine.

Steps
-----
1. **config**    -- the shipped ``config.yaml`` has a valid ``notifications:``
                    section and still contains no secret-looking value.
2. **scan**      -- the AST scan for live-order/signed-request code is still clean
                    (the notification layer adds no exchange code path).
3. **tests**     -- the offline notification unittest modules pass.
4. **sink run**  -- a bounded replay paper run with notifications enabled against
                    the local sink; captured HTTP request bodies are printed.
5. **redaction** -- the test token never appears in the audit file or any log file.

Usage::

    python cryptobot/scripts/notify_check.py
    python cryptobot/scripts/notify_check.py --keep-temp     # keep the scratch dir
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# path bootstrap (same trick as scripts/paperbot.py / acceptance_check.py)
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve()
PACKAGE_ROOT = HERE.parents[1]
WORKSPACE_ROOT = HERE.parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from cryptobot.config import DEFAULT_CONFIG_PATH, assert_no_secrets_in_config, load_config  # noqa: E402
from cryptobot.notify import events  # noqa: E402
from cryptobot.notify.guard import enter_harness_mode  # noqa: E402
from cryptobot.notify.redact import REDACTED, redact  # noqa: E402
from cryptobot.notify.store import NotificationStore  # noqa: E402
from cryptobot.runner import PaperRunner, RunnerConfig  # noqa: E402
from cryptobot.tests import restore_cryptobot_logging  # noqa: E402
from cryptobot.tests.fixtures import tmp_config  # noqa: E402
from cryptobot.tests.no_live_order_scan import scan  # noqa: E402
from cryptobot.tests.notify_fixtures import LocalSink  # noqa: E402

#: Fake secrets for this check: they must never survive into a file or a log.
CHECK_TOPIC = "notify-check-topic"
CHECK_NTFY_TOKEN = "ntfy-check-token-9f2c"
CHECK_TELEGRAM_TOKEN = "123456:CHECKTELEGRAMTOKEN"
CHECK_CHAT_ID = "424242"
CHECK_WEBHOOK_PATH = "/webhook"

EXIT_OK = 0
EXIT_FAIL = 1


class Step:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = True
        self.detail: List[str] = []

    def note(self, text: str) -> None:
        self.detail.append(text)

    def fail(self, text: str) -> None:
        self.ok = False
        self.detail.append("FAIL: " + text)


class NotifyCheck:
    def __init__(self, *, keep_temp: bool = False) -> None:
        # Structural network guarantee: even with a persistent real
        # CRYPTOBOT_NTFY_TOPIC in the environment, this self-check can only
        # reach the loopback sink it starts itself (see notify/guard.py).
        enter_harness_mode()
        self.keep_temp = keep_temp
        self.root = Path(tempfile.mkdtemp(prefix="cryptobot-notify-check-"))
        self.steps: List[Step] = []
        self._clean_env_backup: Dict[str, Optional[str]] = {}

    # ------------------------------------------------------------------ helpers
    def _step(self, name: str) -> Step:
        step = Step(name)
        self.steps.append(step)
        return step

    def _set_env(self, values: Dict[str, str]) -> None:
        for key, value in values.items():
            self._clean_env_backup[key] = os.environ.get(key)
            os.environ[key] = value

    def _restore_env(self) -> None:
        for key, value in self._clean_env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._clean_env_backup.clear()

    # -------------------------------------------------------------------- steps
    def step_config(self) -> None:
        step = self._step("config: notifications section + secret hygiene")
        config = load_config(DEFAULT_CONFIG_PATH, environ={})
        n = config.notifications
        step.note("enabled={} providers={} ntfy_host={} min_severity={}".format(
            n.enabled, ",".join(n.providers), n.ntfy_host, n.min_severity))
        step.note("dedupe={}s max_per_hour={} quiet_hours={} retry_max={} timeout={}s".format(
            n.dedupe_window_seconds, n.max_per_hour, n.quiet_hours, n.retry_max, n.timeout_seconds))
        step.note("notify_on={} olay".format(len(n.notify_on)))
        if not n.enabled:
            step.fail("notifications.enabled is false in the shipped config")
        for provider in ("console", "file", "ntfy"):
            if provider not in n.providers:
                step.fail("provider {} missing from the default providers list".format(provider))
        try:
            assert_no_secrets_in_config(DEFAULT_CONFIG_PATH)
            step.note("config.yaml: no secret-looking value (api_key/secret/password/token/private)")
        except Exception as exc:  # noqa: BLE001
            step.fail("config.yaml secret check: {}".format(exc))

    def step_scan(self) -> None:
        step = self._step("scan: no live-order / signed-request code path")
        result = scan()
        step.note("scanned {} runtime modules, {} finding(s)".format(
            len(result.scanned_files), len(result.findings)))
        if result.findings:
            for finding in result.findings[:10]:
                step.fail("{file}:{line} [{rule}] {snippet}".format(**finding))

    def step_tests(self) -> None:
        step = self._step("tests: offline notification unittest modules")
        command = [sys.executable, "-m", "unittest", "discover",
                   "-s", "cryptobot/tests", "-t", ".", "-p", "test_notify_*.py"]
        completed = subprocess.run(command, cwd=str(WORKSPACE_ROOT), capture_output=True,
                                   text=True, encoding="utf-8", errors="replace")
        output = (completed.stdout or "") + (completed.stderr or "")
        tail = [line for line in output.splitlines() if line.strip().startswith(("Ran ", "OK", "FAILED"))]
        step.note("exit={} | {}".format(completed.returncode, " | ".join(tail[-3:])))
        if completed.returncode != 0 or "OK" not in output:
            step.fail("notification unittest modules failed")
            step.note(output[-1500:])

    def step_sink_run(self) -> None:
        step = self._step("sink run: bounded replay paper run with notifications against 127.0.0.1")
        # Cache is required and read from the shipped data/cache (offline).
        config = tmp_config(self.root / "run")
        source_cache = PACKAGE_ROOT / "data" / "cache"
        copied = 0
        for csv in sorted(source_cache.glob("*.csv")):
            shutil.copy2(csv, config.cache_dir / csv.name)
            copied += 1
        if copied == 0:
            step.fail("no cached candles under {}".format(source_cache))
            return
        step.note("copied {} cached candle file(s) into a private cache".format(copied))

        with LocalSink() as sink:
            notifications = dataclasses.replace(
                config.notifications,
                enabled=True,
                providers=("ntfy", "telegram", "webhook", "console", "file"),
                ntfy_host=sink.url,
                ntfy_topic=CHECK_TOPIC,
                min_severity="info",
                dedupe_window_seconds=0,
                max_per_hour=100000,
                timeout_seconds=5.0,
                retry_max=0,
                equity_drop_pct=3.0,
            )
            run_config = dataclasses.replace(config, notifications=notifications)
            self._set_env({
                "CRYPTOBOT_NTFY_TOKEN": CHECK_NTFY_TOKEN,
                "CRYPTOBOT_TELEGRAM_BOT_TOKEN": CHECK_TELEGRAM_TOKEN,
                "CRYPTOBOT_TELEGRAM_CHAT_ID": CHECK_CHAT_ID,
                "CRYPTOBOT_TELEGRAM_API_BASE": sink.url,
                "CRYPTOBOT_WEBHOOK_URL": sink.url + CHECK_WEBHOOK_PATH,
                "CRYPTOBOT_RUN_DIR": str(self.root / "runner-state"),
            })
            try:
                runner = PaperRunner(
                    run_config,
                    runner=RunnerConfig(cycles=6, offline=True, replay=True,
                                        replay_bars_per_cycle=800, interval_seconds=0.0),
                    run_id="notify-check",
                    sleep_fn=lambda _seconds: None,
                    setup_logs=True,
                )
                summary = runner.run()
            finally:
                self._restore_env()

            requests = sink.requests
            store = NotificationStore(run_config.logs_dir / "notifications.jsonl")
            records = store.read_all()
            by_provider: Dict[str, int] = {}
            by_event: Dict[str, int] = {}
            for record in records:
                by_provider[str(record.get("provider"))] = by_provider.get(str(record.get("provider")), 0) + 1
                by_event[str(record.get("event"))] = by_event.get(str(record.get("event")), 0) + 1

            step.note("paper run: cycles={} bars={} trades={} verification={}".format(
                summary["cycles"], summary["bars_processed"], summary["trades_closed"],
                "PASS" if summary["verification"]["ok"] else "FAIL"))
            step.note("sink captured {} HTTP POST(s); audit rows={} by provider={}".format(
                len(requests), len(records), json.dumps(by_provider, sort_keys=True)))
            step.note("events delivered: {}".format(json.dumps(by_event, sort_keys=True)))

            self._print_captured(step, requests)
            self._check_sink_evidence(step, requests, records, by_event, summary)
            self._check_redaction(step, run_config, records)

    def _print_captured(self, step: Step, requests: Sequence[Dict[str, Any]]) -> None:
        step.note("--- captured HTTP requests (bodies verbatim, secrets masked) ---")
        for index, request in enumerate(requests[:14], start=1):
            path = redact(request["path"], secrets=(CHECK_TELEGRAM_TOKEN, CHECK_TOPIC, CHECK_NTFY_TOKEN))
            body = redact(request["body"], secrets=(CHECK_TELEGRAM_TOKEN, CHECK_NTFY_TOKEN))
            header_bits = []
            for name in ("Title", "Priority", "Tags", "Authorization", "Content-Type"):
                value = next((v for k, v in request["headers"].items() if k.lower() == name.lower()), None)
                if value:
                    header_bits.append("{}={}".format(
                        name, redact(value, secrets=(CHECK_NTFY_TOKEN,))))
            step.note("  [{}] POST {} | {}".format(index, path, " ".join(header_bits)))
            step.note("      body: {}".format(body[:320] + ("..." if len(body) > 320 else "")))
        if len(requests) > 14:
            step.note("  ... {} more request(s) not printed".format(len(requests) - 14))

    def _check_sink_evidence(self, step: Step, requests, records, by_event, summary) -> None:
        paths = [request["path"] for request in requests]
        if not any(path == "/{}".format(CHECK_TOPIC) for path in paths):
            step.fail("ntfy request to /{} not captured".format(CHECK_TOPIC))
        if not any(path.startswith("/bot{}".format(CHECK_TELEGRAM_TOKEN)) for path in paths):
            step.fail("telegram request to /bot<token>/sendMessage not captured")
        if not any(path == CHECK_WEBHOOK_PATH for path in paths):
            step.fail("webhook request to {} not captured".format(CHECK_WEBHOOK_PATH))
        if not summary["verification"]["ok"]:
            step.fail("paper run did not reconcile: {}".format(summary["verification"]))
        for event_type in ("bot_started", "bot_stopped", "position_opened", "position_closed"):
            if event_type not in by_event:
                step.fail("event {} never reached the notification layer".format(event_type))
        sent = [record for record in records if record.get("status") == "sent"]
        if not sent:
            step.fail("no dispatch was recorded as sent")
        providers_sent = {str(record.get("provider")) for record in sent}
        for provider in ("ntfy", "telegram", "webhook"):
            if provider not in providers_sent:
                step.fail("provider {} never sent successfully".format(provider))
        for record in sent:
            if record.get("provider") in ("ntfy", "telegram", "webhook") and record.get("http_status") != 200:
                step.fail("provider {} got HTTP {} (expected 200)".format(
                    record.get("provider"), record.get("http_status")))
                break

    def _check_redaction(self, step: Step, config, records) -> None:
        secrets = (CHECK_NTFY_TOKEN, CHECK_TELEGRAM_TOKEN)
        store_path = config.logs_dir / "notifications.jsonl"
        blob = store_path.read_text(encoding="utf-8")
        if any(secret in blob for secret in secrets):
            step.fail("a test token leaked into {}".format(store_path.name))
        else:
            step.note("audit file: tokens absent ({} bytes, {} rows)".format(len(blob), len(records)))
        log_files = sorted(config.logs_dir.glob("*.log"))
        for log_file in log_files:
            text = log_file.read_text(encoding="utf-8", errors="replace")
            for secret in secrets:
                if secret in text:
                    step.fail("token leaked into log {}".format(log_file.name))
        step.note("checked {} log file(s) for leaked tokens".format(len(log_files)))

    # -------------------------------------------------------------------- runner
    def run(self) -> int:
        restore_cryptobot_logging()   # the tests package mutes cryptobot logging; undo it here
        for method in (self.step_config, self.step_scan, self.step_tests, self.step_sink_run):
            try:
                method()
            except Exception as exc:  # noqa: BLE001 - a broken step must FAIL, not crash
                step = self._step(method.__name__)
                step.fail("{}: {}".format(type(exc).__name__, exc))
        ok = all(step.ok for step in self.steps)
        self._report(ok)
        if not self.keep_temp:
            with contextlib.suppress(Exception):
                shutil.rmtree(self.root, ignore_errors=True)
        return EXIT_OK if ok else EXIT_FAIL

    def _report(self, ok: bool) -> None:
        print("")
        print("=" * 78)
        print("cryptobot notify_check -- {}".format("PASS" if ok else "FAIL"))
        print("=" * 78)
        for step in self.steps:
            print("[{}] {}".format("PASS" if step.ok else "FAIL", step.name))
            for line in step.detail:
                print("       {}".format(line))
        print("-" * 78)
        print("  {}/{} adim PASS".format(sum(1 for s in self.steps if s.ok), len(self.steps)))
        print("  exit: {}".format(EXIT_OK if ok else EXIT_FAIL))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notify_check.py",
        description="Bildirim katmanini cevrimdisi dogrula (yerel HTTP sink, ag yok)",
    )
    parser.add_argument("--keep-temp", action="store_true", help="gecici klasoru silme")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return NotifyCheck(keep_temp=args.keep_temp).run()


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
