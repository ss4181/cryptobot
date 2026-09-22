"""Panel: KPIs, tables, self-containment, redaction, determinism, serving.

Everything here is offline and stdlib-only.  The ledger fixtures are created with
the *real* :class:`cryptobot.ledger.store.Ledger` (so the panel is tested against
the schema the runner actually writes) and the audit fixtures are plain JSONL
lines -- including deliberately broken ones, because a crashed writer must not be
able to break the panel.

The panel is a read-only view, so the tests also assert the negative: rendering
must not add a ledger row, and the served page must never list a directory or
answer a write request.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from cryptobot.ledger.store import Ledger, iso_utc
from cryptobot.notify.redact import REDACTED
from cryptobot.panel import (
    DEFAULT_LIMIT,
    HOST,
    PanelServer,
    build_panel,
    close_kind,
    reason_text,
    remote_references,
    render_panel,
    scheme_references,
    target_display,
    trade_status,
)
from cryptobot.panel.render import GENERATED_MARKER

T0 = 1_700_000_000_000
HOUR = 3_600_000
RUN = "panel-run"
AUDIT_TOPIC = "panel-secret-topic-9f2c"
CAPITAL = 100.0

#: (pair, entry, exit, fees, gross, net, net_pct, exit_reason)
TRADES = (
    ("BTC/USDT", T0, T0 + HOUR, 0.5, 10.0, 9.5, 9.5, "strategy:exit:close_reached_middle_band"),
    ("ETH/USDT", T0, T0 + 2 * HOUR, 0.5, -5.0, -5.5, -5.5, "stop_loss:stop-loss hit: low <= stop"),
    ("BTC/USDT", T0, T0 + 3 * HOUR, 0.5, 0.5, 0.0, 0.0, "time_exit:sure doldu"),
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def make_ledger(path: Path, *, run_id: str = RUN, trades=TRADES, equity=(), orphan_open=None,
                mode: str = "paper", config=None) -> Path:
    """Create a real ledger and fill it with exact, hand-computable rows."""
    ledger = Ledger(path, run_id)
    ledger.start_run(
        mode=mode, started_at=T0, version="test",
        config=config or {"mode": mode, "pairs": ["BTC/USDT", "ETH/USDT"], "timeframe": "1h",
                          "initial_capital_usdt": CAPITAL},
        data={"BTC/USDT": {"rows": 10, "source": "cache"}},
    )
    ledger.close()

    connection = sqlite3.connect(str(path))
    for index, row in enumerate(trades):
        pair, entry, exit_ts, fees, gross, net, net_pct, reason = row
        connection.execute(
            "INSERT INTO trades (run_id, position_id, pair, entry_ts, exit_ts, entry_iso_utc, "
            "exit_iso_utc, entry_reference_price, entry_fill_price, exit_reference_price, "
            "exit_fill_price, qty, entry_fee, exit_fee, fees, gross_pnl, net_pnl, net_pnl_pct, "
            "gross_pnl_pct, slippage_cost, entry_reason, exit_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "pos-{}".format(index), pair, entry, exit_ts, iso_utc(entry), iso_utc(exit_ts),
             100.0, 100.0, 110.0, 110.0, 1.0, fees / 2, fees / 2, fees, gross, net, net_pct,
             gross, fees / 4, "entry:bollinger_dip", reason),
        )
        connection.execute(
            "INSERT INTO ledger (run_id, position_id, event, ts, iso_utc, pair, side, "
            "reference_price, fill_price, qty, notional, fee, slippage_cost, gross_pnl, net_pnl, "
            "net_pnl_pct, reason, mode) VALUES (?,?,'OPEN',?,?,?,'buy',?,?,?,?,?,0,0,0,0,?,?)",
            (run_id, "pos-{}".format(index), entry, iso_utc(entry), pair, 100.0, 100.0, 1.0, 100.0,
             fees / 2, "entry:bollinger_dip", mode),
        )
        connection.execute(
            "INSERT INTO ledger (run_id, position_id, event, ts, iso_utc, pair, side, "
            "reference_price, fill_price, qty, notional, fee, slippage_cost, gross_pnl, net_pnl, "
            "net_pnl_pct, reason, mode) VALUES (?,?,'CLOSE',?,?,?,'sell',?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "pos-{}".format(index), exit_ts, iso_utc(exit_ts), pair, 110.0, 110.0, 1.0,
             110.0, fees / 2, fees / 4, gross, net, net_pct, reason, mode),
        )
    if orphan_open is not None:
        pair, ts, qty = orphan_open
        connection.execute(
            "INSERT INTO ledger (run_id, position_id, event, ts, iso_utc, pair, side, "
            "reference_price, fill_price, qty, notional, fee, slippage_cost, gross_pnl, net_pnl, "
            "net_pnl_pct, reason, mode) VALUES (?,?,'OPEN',?,?,?,'buy',?,?,?,?,?,0,0,0,0,?,?)",
            (run_id, "pos-open-1", ts, iso_utc(ts), pair, 100.0, 100.0, qty, 100.0 * qty, 0.05,
             "entry:bollinger_dip", mode),
        )
    for point in equity:
        ts, value = point
        connection.execute(
            "INSERT INTO equity (run_id, ts, iso_utc, cash, positions_value, equity, "
            "open_positions, realized_net_pnl) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, ts, iso_utc(ts), value, 0.0, value, 0, value - CAPITAL),
        )
    connection.commit()
    connection.close()
    return path


def audit_row(**overrides) -> dict:
    row = {
        "ts": T0, "iso_utc": iso_utc(T0), "run_id": RUN, "event": "position_opened",
        "severity": "info", "provider": "ntfy", "status": "sent", "reason": "",
        "http_status": 200, "latency_ms": 12.5, "attempts": 1, "dry_run": False,
        "title": "Pozisyon acildi", "body": "BTCUSDT · LONG (PAPER)", "dedupe_key": "k",
        "target": "https://ntfy.sh/{}".format(AUDIT_TOPIC), "response": "",
    }
    row.update(overrides)
    return row


def write_audit(path: Path, rows, *, trailing_garbage: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        if trailing_garbage:
            handle.write('{"ts": 1, "event": "position_closed", "stat')  # crashed mid-write
    return path


class PanelTestCase(unittest.TestCase):
    """Shared tmp workspace: one ledger, one audit file, both disposable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "ledger.sqlite"
        self.audit = self.tmp / "logs" / "notifications.jsonl"

    def build(self, *, run_id=None, limit=DEFAULT_LIMIT, rows=None, **kwargs):
        make_ledger(self.db, **kwargs)
        write_audit(self.audit, rows if rows is not None else [audit_row()])
        data = build_panel(db_path=self.db, audit_path=self.audit, run_id=run_id,
                           now=datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc))
        return data, render_panel(data, limit=limit)


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
class TradeStatusRuleTests(unittest.TestCase):
    def test_status_rule_is_by_sign(self):
        self.assertEqual(trade_status(0.01), ("kazanc", "Kazanç"))
        self.assertEqual(trade_status(-0.01), ("zarar", "Zarar"))
        self.assertEqual(trade_status(0.0), ("basabas", "Başabaş"))
        self.assertEqual(trade_status(None), ("bilinmiyor", "Bilinmiyor"))
        self.assertEqual(trade_status("x"), ("bilinmiyor", "Bilinmiyor"))

    def test_breakeven_needs_exactly_zero(self):
        # No tolerance: 1e-12 is a win, not a break-even.
        self.assertEqual(trade_status(1e-12)[0], "kazanc")
        self.assertEqual(trade_status(-1e-12)[0], "zarar")

    def test_close_kind_covers_the_four_documented_exits(self):
        self.assertEqual(close_kind("take_profit:hit")[1], "Kâr al (take-profit)")
        self.assertEqual(close_kind("stop_loss:hit")[1], "Zarar durdur (stop-loss)")
        self.assertEqual(close_kind("strategy:exit:x")[1], "Strateji çıkışı")
        for text in ("time:hold expired", "timeout", "time_exit:1h"):
            self.assertEqual(close_kind(text)[1], "Süre sonu (time exit)")
        self.assertEqual(close_kind("mystery:why"), ("diger", "Diğer"))
        self.assertEqual(close_kind(None), ("bilinmiyor", "Bilinmiyor"))

    def test_reason_text_translates_the_documented_codes(self):
        expected = {
            "event_not_enabled": "kapsam dışı olay",
            "max_per_hour": "saatlik ağ bütçesi doldu",
            "harness_guard": "test/doğrulama koruması",
            "replay_no_push": "replay modunda gönderim kapalı",
            "dedupe_window": "tekrar bastırıldı",
            "offline_no_push": "offline modunda gönderim kapalı",
        }
        for code, label in expected.items():
            self.assertEqual(reason_text(code), (label, True), code)
        # An unknown reason is passed through, never guessed.
        self.assertEqual(reason_text("TransportError: timed out"),
                         ("TransportError: timed out", False))
        self.assertEqual(reason_text("provider crashed: boom"), ("sağlayıcı çöktü: boom", True))
        self.assertEqual(reason_text(""), ("", False))

    def test_target_display_keeps_only_the_host(self):
        with mock.patch.dict(os.environ, {"CRYPTOBOT_NTFY_TOPIC": AUDIT_TOPIC}):
            host, full = target_display("https://ntfy.sh/{}/x".format(AUDIT_TOPIC))
        self.assertEqual(host, "ntfy.sh")
        self.assertNotIn(AUDIT_TOPIC, full)
        # Structural pattern: a Telegram bot token is redacted even with no env set.
        host, full = target_display("http://127.0.0.1:9/bot1234567:AAHsecretTOKEN/sendMessage")
        self.assertEqual(host, "127.0.0.1")
        self.assertNotIn("AAHsecretTOKEN", full)
        self.assertEqual(target_display(""), ("", ""))
        self.assertEqual(target_display("C:/tmp/notifications.jsonl")[0], "C:/tmp/notifications.jsonl")


# --------------------------------------------------------------------------- #
# model / KPIs
# --------------------------------------------------------------------------- #
class PanelModelTests(PanelTestCase):
    def test_kpis_are_exact_on_a_hand_computed_ledger(self):
        data, _html = self.build()
        trades, notif = data.stats["trades"], data.stats["notifications"]
        self.assertTrue(trades["readable"])
        self.assertEqual((trades["opened"], trades["closed"], trades["open"]), (3, 3, 0))
        self.assertEqual((trades["winning"], trades["losing"], trades["breakeven"]), (1, 1, 1))
        self.assertAlmostEqual(trades["win_rate_pct"], 100.0 / 3.0, places=9)
        self.assertAlmostEqual(trades["net_pnl_usdt"], 4.0, places=9)
        self.assertAlmostEqual(trades["gross_pnl_usdt"], 5.5, places=9)
        self.assertAlmostEqual(trades["fees_usdt"], 1.5, places=9)
        self.assertAlmostEqual(trades["slippage_usdt"], 0.375, places=9)
        self.assertAlmostEqual(trades["avg_net_pnl_usdt"], 4.0 / 3.0, places=9)
        self.assertAlmostEqual(trades["net_pnl_pct"], 4.0, places=9)
        self.assertAlmostEqual(trades["profit_factor"], 9.5 / 5.5, places=9)
        self.assertAlmostEqual(trades["avg_holding_seconds"], 2 * 3600.0, places=6)
        self.assertAlmostEqual(trades["best"].net_pnl, 9.5, places=9)
        self.assertAlmostEqual(trades["worst"].net_pnl, -5.5, places=9)
        # realized-cumulative basis: 100 -> 109.5 -> 104.0 -> 104.0
        self.assertEqual(data.drawdown_basis, "realized_cumulative")
        self.assertAlmostEqual(trades["max_drawdown_usdt"], 5.5, places=9)
        self.assertAlmostEqual(trades["max_drawdown_pct"], 5.5 / 109.5 * 100.0, places=6)
        self.assertTrue(notif["readable"])
        self.assertEqual((notif["total"], notif["sent"], notif["failed"]), (1, 1, 0))

    def test_equity_curve_of_a_single_run_uses_the_ledger_equity_rows(self):
        data, _html = self.build(equity=((T0, 100.0), (T0 + HOUR, 120.0), (T0 + 2 * HOUR, 90.0)))
        self.assertEqual(data.drawdown_basis, "ledger_equity")
        self.assertEqual([value for _ts, value in data.equity_series], [100.0, 120.0, 90.0])
        self.assertAlmostEqual(data.stats["trades"]["max_drawdown_pct"], 25.0, places=6)
        self.assertAlmostEqual(data.stats["trades"]["max_drawdown_usdt"], 30.0, places=6)

    def test_open_position_is_reported_separately_and_keeps_the_win_rate(self):
        data, _html = self.build(orphan_open=("BTC/USDT", T0 + HOUR, 0.5))
        trades = data.stats["trades"]
        self.assertEqual((trades["opened"], trades["closed"], trades["open"]), (4, 3, 1))
        self.assertEqual(trades["winning"] + trades["losing"] + trades["breakeven"], 3)
        self.assertEqual(len(data.open_positions), 1)
        position = data.open_positions[0]
        self.assertEqual(position.status_label, "Açık")
        self.assertIsNone(position.net_pnl)
        self.assertIsNone(position.exit_ts)
        self.assertEqual(position.side, "buy")

    def test_missing_audit_file_is_empty_not_fatal(self):
        make_ledger(self.db)
        data = build_panel(db_path=self.db, audit_path=self.audit / "does-not-exist.jsonl")
        self.assertFalse(data.audit.exists)
        self.assertTrue(data.audit.ok)
        self.assertEqual(data.stats["notifications"]["total"], 0)
        self.assertIsNone(data.stats["notifications"]["delivery_rate_pct"])
        self.assertTrue(any("denetim" in warning for warning in data.warnings))

    def test_truncated_last_audit_line_is_skipped_and_reported(self):
        make_ledger(self.db)
        write_audit(self.audit, [audit_row(), audit_row(status="failed", reason="boom")],
                    trailing_garbage=True)
        data = build_panel(db_path=self.db, audit_path=self.audit)
        self.assertTrue(data.audit.ok)
        self.assertEqual(data.audit.lines, 3)
        self.assertEqual(data.audit.parsed, 2)
        self.assertEqual(data.audit.skipped, 1)
        self.assertEqual(data.stats["notifications"]["total"], 2)
        self.assertTrue(any("bozuk" in warning for warning in data.warnings))

    def test_empty_ledger_reports_nothing_rather_than_zero_metrics(self):
        Ledger(self.db, RUN).close()  # schema only, no rows
        write_audit(self.audit, [])
        data = build_panel(db_path=self.db, audit_path=self.audit)
        trades = data.stats["trades"]
        self.assertTrue(trades["readable"])
        self.assertEqual(trades["closed"], 0)
        self.assertIsNone(trades["win_rate_pct"])
        self.assertIsNone(trades["net_pnl_usdt"])
        self.assertIsNone(trades["profit_factor"])
        self.assertIsNone(trades["avg_holding_seconds"])
        self.assertEqual(data.equity_series, ())

    def test_missing_ledger_file_is_reported_not_crashed(self):
        write_audit(self.audit, [audit_row()])
        data = build_panel(db_path=self.tmp / "nope.sqlite", audit_path=self.audit)
        self.assertFalse(data.ledger.exists)
        self.assertFalse(data.ledger.ok)
        self.assertFalse(data.stats["trades"]["readable"])
        self.assertTrue(any("defter" in warning for warning in data.warnings))

    def test_corrupt_ledger_is_reported_not_crashed(self):
        self.db.write_bytes(b"this is not a sqlite database at all")
        write_audit(self.audit, [audit_row()])
        data = build_panel(db_path=self.db, audit_path=self.audit)
        self.assertTrue(data.ledger.exists)
        self.assertFalse(data.ledger.ok)
        self.assertFalse(data.stats["trades"]["readable"])
        self.assertEqual(data.notifications and len(data.notifications), 1)

    def test_run_id_selection_filters_both_stores(self):
        make_ledger(self.db, run_id="run-a")
        ledger = Ledger(self.db, "run-b")
        ledger.start_run(mode="backtest", started_at=T0 + 1, version="test",
                         config={"initial_capital_usdt": 50.0}, data={})
        ledger.close()
        write_audit(self.audit, [audit_row(run_id="run-a"), audit_row(run_id="run-b", status="failed")])
        data = build_panel(db_path=self.db, audit_path=self.audit, run_id="run-b")
        self.assertEqual([run.run_id for run in data.runs], ["run-b"])
        self.assertEqual(data.stats["trades"]["closed"], 0)
        self.assertEqual(data.stats["notifications"]["total"], 1)
        self.assertEqual(data.stats["notifications"]["failed"], 1)
        self.assertEqual(data.selection_label, "run-b")

    def test_notification_status_breakdown_and_rates(self):
        make_ledger(self.db)
        rows = [audit_row(status="sent"), audit_row(status="sent", event="position_closed"),
                audit_row(status="failed", reason="TransportError: boom"),
                audit_row(status="suppressed", reason="max_per_hour"),
                audit_row(status="suppressed", reason="event_not_enabled"),
                audit_row(status="dry_run", dry_run=True, event="test")]
        write_audit(self.audit, rows)
        data = build_panel(db_path=self.db, audit_path=self.audit)
        notif = data.stats["notifications"]
        self.assertEqual((notif["total"], notif["sent"], notif["failed"], notif["suppressed"],
                          notif["dry_run"]), (6, 2, 1, 2, 1))
        self.assertAlmostEqual(notif["delivery_rate_pct"], 200.0 / 3.0, places=9)
        self.assertAlmostEqual(notif["suppress_rate_pct"], 200.0 / 6.0, places=9)
        self.assertAlmostEqual(notif["avg_latency_ms"], 12.5, places=9)
        self.assertEqual([item[0] for item in notif["by_event"]],
                         ["position_opened", "position_closed", "test"])

    def test_panel_never_writes_to_the_ledger(self):
        make_ledger(self.db, orphan_open=("BTC/USDT", T0 + HOUR, 0.5))
        write_audit(self.audit, [audit_row()])
        before = self.db.read_bytes()
        data = build_panel(db_path=self.db, audit_path=self.audit)
        render_panel(data)
        after = self.db.read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(self.db.stat().st_size, len(after))
        connection = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM trades").fetchone()[0], 3)
        finally:
            connection.close()


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
class PanelRenderTests(PanelTestCase):
    def test_document_is_self_contained(self):
        data, html = self.build()
        self.assertEqual(remote_references(html), [])
        self.assertEqual(scheme_references(html), [])
        for needle in ("<script src=", "<link", "@import", "src=", "href=", "http://", "https://"):
            self.assertNotIn(needle, html)
        self.assertIn("<style>", html)
        self.assertIn("<svg", html)
        self.assertNotIn("xmlns", html)  # inline SVG needs no namespace declaration

    def test_every_table_row_value_is_escaped(self):
        make_ledger(self.db)
        write_audit(self.audit, [audit_row(title='"><script>alert(1)</script>',
                                           body="<img onerror=x>", target="https://ntfy.sh/{}".format(AUDIT_TOPIC))])
        data = build_panel(db_path=self.db, audit_path=self.audit)
        html = render_panel(data)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img onerror", html)
        self.assertIn("&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_no_configured_secret_can_reach_the_file(self):
        make_ledger(self.db)
        write_audit(self.audit, [audit_row(),
                                 audit_row(status="failed", reason="token={}".format(AUDIT_TOPIC))])
        with mock.patch.dict(os.environ, {"CRYPTOBOT_NTFY_TOPIC": AUDIT_TOPIC,
                                          "CRYPTOBOT_WEBHOOK_URL":
                                              "https://hooks.example.com/services/{}/x".format(AUDIT_TOPIC)}):
            data = build_panel(db_path=self.db, audit_path=self.audit)
            html = render_panel(data)
        self.assertNotIn(AUDIT_TOPIC, html)
        self.assertIn(REDACTED, html)
        self.assertIn("ntfy.sh", html)

    def test_redaction_never_touches_the_panels_own_script(self):
        # Regression: a text-level redact() over the finished document rewrote
        # ``event.key === "Enter"`` into ``event.key=[REDACTED]`` -- valid HTML,
        # broken JavaScript.  Only verbatim configured values may be replaced.
        _data, html = self.build()
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn('event.key === "Enter"', script)
        self.assertNotIn(REDACTED, script)
        style = html.split("<style>", 2)[1].split("</style>", 1)[0]
        self.assertNotIn(REDACTED, style)

    def test_unavailable_amounts_render_as_a_bare_dash(self):
        make_ledger(self.db, trades=(), equity=())
        write_audit(self.audit, [])
        data = build_panel(db_path=self.db, audit_path=self.audit)
        html = render_panel(data)
        self.assertIn('<div class="label">Net kâr/zarar</div><div class="value">—</div>', html)
        self.assertNotIn("— USDT", html)
        self.assertNotIn("— %", html)

    def test_output_is_deterministic_apart_from_the_generated_line(self):
        data_first, first = self.build()
        make_ledger(self.db)
        write_audit(self.audit, [audit_row()])
        data_second = build_panel(db_path=self.db, audit_path=self.audit,
                                  now=datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
        second = render_panel(data_second)

        self.assertNotEqual(first, second)
        self.assertEqual(first.replace(data_first.generated_iso, "TS"),
                         second.replace(data_second.generated_iso, "TS"))
        # The timestamp lives on exactly one line, marked for exactly this purpose.
        stamped = [line for line in first.splitlines() if GENERATED_MARKER in line]
        self.assertEqual(len(stamped), 1)
        self.assertIn(data_first.generated_iso, stamped[0])
        self.assertIn('data-panel-generated="{}"'.format(data_first.generated_iso), stamped[0])

    def test_same_inputs_render_byte_identical(self):
        make_ledger(self.db)
        write_audit(self.audit, [audit_row()])
        stamp = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
        one = render_panel(build_panel(db_path=self.db, audit_path=self.audit, now=stamp))
        two = render_panel(build_panel(db_path=self.db, audit_path=self.audit, now=stamp))
        self.assertEqual(one, two)

    def test_limit_truncation_is_visible(self):
        make_ledger(self.db)
        write_audit(self.audit, [audit_row(ts=T0 + index) for index in range(5)])
        data = build_panel(db_path=self.db, audit_path=self.audit)
        html = render_panel(data, limit=2)
        self.assertIn("--limit 2", html)
        self.assertIn("kesildi", html)
        self.assertIn("3 bildirim satırı", html)
        self.assertIn('<span id="tbl-notify-shown">2</span>', html)
        self.assertIn('<span id="tbl-notify-total">5</span>', html)
        # The KPI block still counts every row.
        self.assertIn('<div class="label">Toplam bildirim üretildi</div><div class="value">5</div>', html)

    def test_limit_zero_shows_every_row_and_says_so(self):
        make_ledger(self.db)
        write_audit(self.audit, [audit_row(ts=T0 + index) for index in range(5)])
        data = build_panel(db_path=self.db, audit_path=self.audit)
        html = render_panel(data, limit=0)
        self.assertNotIn("kesildi", html)
        self.assertIn("kesilmedi", html)
        self.assertEqual(html.count('data-status="sent"'), 5)

    def test_multiple_runs_are_flagged_as_an_aggregate(self):
        make_ledger(self.db, run_id="run-a")
        second = Ledger(self.db, "run-b")
        second.start_run(mode="backtest", started_at=T0 + HOUR, version="test",
                         config={"initial_capital_usdt": 50.0, "pairs": ["BTC/USDT"],
                                 "timeframe": "1h", "mode": "backtest"}, data={})
        second.close()
        write_audit(self.audit, [])
        data = build_panel(db_path=self.db, audit_path=self.audit)
        html = render_panel(data)
        self.assertEqual(len(data.runs), 2)
        self.assertIn("bağımsız koşuların toplamıdır", html)

    def test_empty_everything_still_produces_a_valid_document(self):
        write_audit(self.audit, [])
        data = build_panel(db_path=self.tmp / "missing.sqlite", audit_path=self.tmp / "missing.jsonl")
        html = render_panel(data)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertTrue(html.rstrip().endswith("</html>"))
        self.assertIn("—", html)
        self.assertIn("Genel başarı istatistikleri", html)
        self.assertNotIn("Traceback", html)

    def test_a_readable_but_empty_selection_reports_zero_not_a_dash(self):
        # Zero rows is a *measurement*: 0 trades is honest, whereas the rates that
        # would divide by zero stay "—".
        make_ledger(self.db, trades=(), equity=())
        write_audit(self.audit, [])
        data = build_panel(db_path=self.db, audit_path=self.audit)
        html = render_panel(data)
        self.assertEqual(data.stats["trades"]["closed"], 0)
        self.assertIn('<div class="label">Kapanan işlem</div><div class="value">0</div>', html)
        self.assertIn('<div class="label">Kazanma oranı</div><div class="value">—</div>', html)
        self.assertIn('<div class="label">Profit factor</div><div class="value">—</div>', html)

    def test_an_unreadable_ledger_shows_dashes_for_every_trade_metric(self):
        self.db.write_bytes(b"not a database")
        write_audit(self.audit, [audit_row()])
        data = build_panel(db_path=self.db, audit_path=self.audit)
        html = render_panel(data)
        self.assertFalse(data.stats["trades"]["readable"])
        self.assertIn('<div class="label">Kapanan işlem</div><div class="value">—</div>', html)
        self.assertIn('<div class="label">Net kâr/zarar</div><div class="value">—</div>', html)
        self.assertIn("defter açılamadı", html)
        # The audit store is independent and still fully rendered.
        self.assertIn('<div class="label">Gönderildi (sent)</div><div class="value">1</div>', html)

    def test_legend_documents_the_rules(self):
        _data, html = self.build()
        for needle in ("Başarı durumu kuralı", "tam olarak", "take-profit", "stop-loss",
                       "Süre sonu", "Teslim başarı oranı", "saatlik ağ bütçesi doldu",
                       "kapsam dışı olay", "tekrar bastırıldı", "harness_guard"):
            self.assertIn(needle, html)
        self.assertIn("Kazanç", html)
        self.assertIn("Zarar", html)
        self.assertIn("Başabaş", html)


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class PanelServerTests(PanelTestCase):
    def _start(self, provider):
        server = PanelServer(provider, port=0)
        self.addCleanup(server.stop)
        server.warm()
        return server.start_background()

    def test_binds_loopback_only_and_serves_the_document(self):
        _data, html = self.build()
        server = self._start(lambda: html)
        self.assertEqual(server.address[0], HOST)
        with urllib.request.urlopen(server.url, timeout=10) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
            self.assertIn("Cache-Control", response.headers)
            self.assertEqual(response.headers["Cache-Control"], "no-store, must-revalidate")
        self.assertEqual(body, html)

    def test_head_works_and_other_paths_are_404(self):
        _data, html = self.build()
        server = self._start(lambda: html)
        request = urllib.request.Request(server.url, method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
        for path in ("/etc/passwd", "/../config.yaml", "/ledger.sqlite"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(server.url.rstrip("/") + path, timeout=10)
            self.assertEqual(caught.exception.code, 404)

    def test_write_methods_are_refused(self):
        _data, html = self.build()
        server = self._start(lambda: html)
        request = urllib.request.Request(server.url, data=b"nope", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 501)

    def test_page_is_re_generated_per_request_and_falls_back_to_the_last_good_one(self):
        calls = []

        def provider():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("ledger busy")
            return "<html>first</html>"

        server = self._start(provider)
        with urllib.request.urlopen(server.url, timeout=10) as response:
            self.assertIn(b"first", response.read())
        with urllib.request.urlopen(server.url, timeout=10) as response:
            self.assertEqual(response.read(), b"<html>first</html>")  # cached, no 500
        self.assertGreaterEqual(len(calls), 2)

    def test_binding_anything_but_loopback_is_refused(self):
        with self.assertRaises(ValueError):
            PanelServer(lambda: "", host="0.0.0.0")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class PanelCliTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        from cryptobot.cli import main

        self.main = main
        self.logs = self.tmp / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.reports = self.tmp / "reports"
        self.config_path = self.tmp / "config.yaml"
        self.config_path.write_text(yaml.safe_dump({
            "data": {"db_path": str(self.db)},
            "logging": {"dir": str(self.logs)},
            "reports_dir": str(self.reports),
        }), encoding="utf-8")
        self.addCleanup(self._reset_logging)

    @staticmethod
    def _reset_logging() -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
        root.setLevel(logging.WARNING)

    def run_cli(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = self.main(["panel"] + argv)
        return code, buffer.getvalue()

    def test_panel_command_writes_a_self_contained_file(self):
        make_ledger(self.db, orphan_open=("BTC/USDT", T0 + HOUR, 0.5))
        write_audit(self.logs / "notifications.jsonl", [audit_row(), audit_row(status="failed",
                                                                              reason="boom")])
        out = self.tmp / "out" / "panel.html"
        code, text = self.run_cli(["--config", str(self.config_path), "--out", str(out)])
        self.assertEqual(code, 0, text)
        self.assertTrue(out.exists())
        html = out.read_text(encoding="utf-8")
        self.assertEqual(remote_references(html), [])
        self.assertIn("Genel başarı istatistikleri", html)
        self.assertIn("uzak kaynak referansi 0", text)
        self.assertIn("dosya        : {}".format(out), text)

    def test_default_output_path_is_the_reports_dir(self):
        make_ledger(self.db)
        write_audit(self.logs / "notifications.jsonl", [])
        code, text = self.run_cli(["--config", str(self.config_path), "--quiet"])
        self.assertEqual(code, 0, text)
        expected = self.reports / "panel" / "index.html"
        self.assertTrue(expected.exists())

    def test_run_id_and_limit_flags_reach_the_document(self):
        make_ledger(self.db)
        write_audit(self.logs / "notifications.jsonl",
                    [audit_row(), audit_row(run_id="other-run", status="failed")])
        out = self.tmp / "one.html"
        code, text = self.run_cli(["--config", str(self.config_path), "--out", str(out),
                                   "--run-id", RUN, "--limit", "1", "--quiet"])
        self.assertEqual(code, 0, text)
        html = out.read_text(encoding="utf-8")
        self.assertIn("--limit 1", html)
        self.assertIn(RUN, html)
        self.assertNotIn("other-run", html)

    def test_missing_data_still_exits_zero_with_an_honest_page(self):
        out = self.tmp / "empty.html"
        code, text = self.run_cli(["--config", str(self.config_path), "--out", str(out), "--quiet"])
        self.assertEqual(code, 0, text)
        html = out.read_text(encoding="utf-8")
        self.assertIn("—", html)
        self.assertIn("defter dosyası yok", html)
        self.assertIn("denetim dosyası yok", html)
        self.assertNotIn("Traceback", html)

    def test_limit_default_matches_the_documented_value(self):
        self.assertEqual(DEFAULT_LIMIT, 500)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
