"""Notifications: the CLI surface (notify status / test / log / export)."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from cryptobot.cli import main as cli_main
from cryptobot.config import DEFAULT_CONFIG_PATH
from cryptobot.notify import events


def _temp_config(tmp: Path) -> Path:
    """The shipped config with logging/cache/db moved into the temp dir (no real writes)."""
    logs = tmp / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    cache = tmp / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    text = text.replace("  dir: logs", "  dir: {}".format(logs.as_posix()))
    text = text.replace("  cache_dir: data/cache", "  cache_dir: {}".format(cache.as_posix()))
    text = text.replace("  db_path: data/ledger.sqlite",
                        "  db_path: {}".format((data / "ledger.sqlite").as_posix()))
    path = tmp / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _run(argv, *, allow_exit: bool = False):
    """Run the CLI in-process, returning (exit_code, combined_output).

    ``allow_exit=True`` tolerates argparse's ``SystemExit`` (``--help``).
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        try:
            code = cli_main(argv)
        except SystemExit as exc:  # argparse --help / usage errors
            if not allow_exit:
                raise
            code = int(exc.code or 0)
    return code, buffer.getvalue()


class TestNotifyCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _temp_config(self.root)

    def test_status_lists_every_configured_provider(self):
        code, out = _run(["notify", "status", "--config", str(self.config)])
        self.assertEqual(code, 0, out)
        self.assertIn("bildirim durumu", out)
        for provider in ("console", "file", "ntfy"):
            self.assertIn(provider, out)
        self.assertIn("PASIF", out)                     # ntfy has no topic -> explain why
        self.assertIn("CRYPTOBOT_NTFY_TOPIC", out)

    def test_test_dry_run_writes_records_without_sending(self):
        code, out = _run(["notify", "test", "--dry-run", "--config", str(self.config)])
        self.assertEqual(code, 0, out)
        self.assertIn("DRY-RUN", out)
        store = self.root / "logs" / "notifications.jsonl"
        self.assertTrue(store.exists())
        self.assertIn("dry_run", store.read_text(encoding="utf-8"))
        self.assertNotIn("https://ntfy.sh", store.read_text(encoding="utf-8"))

    def test_log_prints_the_records(self):
        _run(["notify", "test", "--dry-run", "--config", str(self.config)])
        code, out = _run(["notify", "log", "--config", str(self.config), "--limit", "5"])
        self.assertEqual(code, 0, out)
        self.assertIn("bildirim kaydi", out)
        self.assertIn("test", out)
        self.assertIn("dry_run", out)

    def test_log_json(self):
        _run(["notify", "test", "--dry-run", "--config", str(self.config)])
        code, out = _run(["notify", "log", "--json", "--config", str(self.config)])
        self.assertEqual(code, 0, out)
        self.assertIn('"provider"', out)

    def test_log_on_empty_store_is_not_an_error(self):
        code, out = _run(["notify", "log", "--config", str(self.config)])
        self.assertEqual(code, 0, out)
        self.assertIn("kayit yok", out)

    def test_export_writes_csv_and_json(self):
        _run(["notify", "test", "--dry-run", "--config", str(self.config)])
        out_dir = self.root / "exports"
        code, out = _run(["notify", "export", "--config", str(self.config),
                          "--out", str(out_dir)])
        self.assertEqual(code, 0, out)
        self.assertTrue((out_dir / "notifications.csv").exists())
        self.assertTrue((out_dir / "notifications.json").exists())

    def test_test_without_any_active_provider_fails(self):
        """No topic and only ntfy configured -> a test must report failure, not silence."""
        text = self.config.read_text(encoding="utf-8").replace(
            "  providers:\n    - console\n    - file\n    - ntfy",
            "  providers:\n    - ntfy")
        self.config.write_text(text, encoding="utf-8")
        code, out = _run(["notify", "test", "--config", str(self.config)])
        self.assertEqual(code, 1, out)
        self.assertIn("no_active_provider", out)

    def test_notify_help_is_a_subcommand_group(self):
        code, out = _run(["notify", "--help"], allow_exit=True)
        self.assertEqual(code, 0, out)
        for command in ("test", "status", "log", "export", "preview"):
            self.assertIn(command, out)

    def test_preview_all_prints_every_event_type_without_sending(self):
        code, out = _run(["notify", "preview", "--all", "--config", str(self.config)])
        self.assertEqual(code, 0, out)
        for event_type in events.EVENT_TYPES:
            self.assertIn(event_type, out)
        self.assertIn("hicbir sey GONDERILMEDI", out)
        # A preview must not create an audit record: nothing was dispatched.
        self.assertFalse((self.root / "logs" / "notifications.jsonl").exists())
        # No cache in the temp config -> the history block is omitted, not faked.
        self.assertIn("gecmis blogu ATLANIR", out)

    def test_preview_single_event_renders_only_that_event(self):
        code, out = _run(["notify", "preview", "--event", "position_closed",
                          "--config", str(self.config)])
        self.assertEqual(code, 0, out)
        self.assertIn("position_closed", out)
        self.assertNotIn("bot_started", out)
        self.assertIn("Giriş → Çıkış", out)     # entry -> exit is spelled out

    def test_preview_carrier_ntfy_emits_markdown(self):
        code, out = _run(["notify", "preview", "--event", "stop_loss_hit",
                          "--carrier", "markdown", "--config", str(self.config)])
        self.assertEqual(code, 0, out)
        self.assertIn("**", out)

    def test_preview_rejects_an_unknown_event(self):
        code, out = _run(["notify", "preview", "--event", "nope", "--config", str(self.config)],
                         allow_exit=True)
        self.assertEqual(code, 2, out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
