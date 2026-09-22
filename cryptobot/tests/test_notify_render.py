"""Notification rendering: Turkish number formatting, sectioned templates, honesty.

These tests pin the properties the redesign is required to keep:

* Turkish number conventions and a sign on every PnL,
* one shared content model rendered per carrier (markdown / HTML / plain),
* a **measurement** block that never claims a probability,
* a historical block only when a real backtest produced it, always with the
  period/timeframe/pairs/config hash and a "not a promise" disclaimer,
* a message that stays phone-readable (bounded content lines),
* rendering that never raises, whatever the data.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cryptobot.notify import context, events, render, samples
from cryptobot.notify import format as fmt
from cryptobot.tests.fixtures import make_frame, seed_cache, tmp_config


class TestTurkishNumberFormatting(unittest.TestCase):
    def test_thousands_and_decimal_separators(self):
        self.assertEqual(fmt.price(71679.5118), "71.679,51")
        self.assertEqual(fmt.usdt(45.0, 2), "45,00")
        self.assertEqual(fmt.number(1234567.891, 2), "1.234.567,89")

    def test_pnl_always_carries_a_sign(self):
        self.assertEqual(fmt.pct(2.0), "+2,00")
        self.assertEqual(fmt.pct(-2.5), "-2,50")
        self.assertEqual(fmt.usdt(0.814, 4, signed=True), "+0,8140")
        self.assertEqual(fmt.usdt(-1.2293, 4, signed=True), "-1,2293")

    def test_price_decimals_follow_magnitude(self):
        self.assertEqual(fmt.price(0.5), "0,5000")
        self.assertEqual(fmt.price(0.0001234), "0,000123")

    def test_qty_trims_trailing_zeros(self):
        self.assertEqual(fmt.qty(0.00062779), "0,00062779")
        self.assertEqual(fmt.qty(0.5), "0,5")
        self.assertEqual(fmt.qty(1.0), "1")
        self.assertEqual(fmt.qty(None), "n/a")

    def test_pct_label_is_compact(self):
        self.assertEqual(fmt.pct_label(2.0), "2")
        self.assertEqual(fmt.pct_label(2.5), "2,5")

    def test_duration_is_human(self):
        self.assertEqual(fmt.duration(42), "42sn")
        self.assertEqual(fmt.duration(720), "12dk")
        self.assertEqual(fmt.duration(4 * 3600), "4s")
        self.assertEqual(fmt.duration(3 * 3600 + 720), "3s 12dk")

    def test_pair_compaction(self):
        self.assertEqual(fmt.pair_compact("BTC/USDT"), "BTCUSDT")
        self.assertEqual(fmt.pairs_compact(("BTC/USDT", "ETH/USDT")), "BTCUSDT/ETHUSDT")

    def test_missing_values_are_never_zero(self):
        self.assertEqual(fmt.number(None), "n/a")
        self.assertEqual(fmt.number(float("nan")), "n/a")
        self.assertEqual(fmt.number(float("inf")), "n/a")


class TestFilterMargins(unittest.TestCase):
    INDICATORS = {"close": 100.0, "bb_upper": 109.0, "bb_middle": 105.0, "bb_lower": 101.0,
                  "rsi": 30.0, "sma_trend": 95.0}
    PARAMS = {"bb_std": 2.0, "rsi_oversold": 35.0, "trend_sma_period": 200}

    def test_margins_are_arithmetic_not_a_probability(self):
        margins = context.filter_margins(self.INDICATORS, self.PARAMS)
        self.assertAlmostEqual(margins["rsi_below"], 5.0)
        # sigma = (upper - lower) / (2 * bb_std) = 8/4 = 2 -> 1.0 price below band = 0.5 sigma
        self.assertAlmostEqual(margins["band_sigma_below"], 0.5)
        self.assertAlmostEqual(margins["trend_pct_above"], (100.0 - 95.0) / 95.0 * 100.0)
        self.assertEqual(margins["regime"], "BULL")

    def test_summary_reads_like_the_reference(self):
        summary = context.margin_summary(context.filter_margins(self.INDICATORS, self.PARAMS))
        self.assertIn("RSI 5,0 altı", summary)
        self.assertIn("bant 0,50σ altı", summary)
        self.assertIn("trend 5,3% üstü", summary)

    def test_regime_line(self):
        margins = context.filter_margins(self.INDICATORS, self.PARAMS)
        self.assertEqual(context.regime_line(margins), "BULL (fiyat > SMA200)")

    def test_unmeasurable_inputs_yield_none_or_missing_keys(self):
        self.assertIsNone(context.filter_margins({}, self.PARAMS))
        margins = context.filter_margins({"close": 100.0, "sma_trend": 95.0}, self.PARAMS)
        self.assertNotIn("rsi_below", margins)
        self.assertEqual(context.margin_summary(margins), "trend 5,3% üstü")


class TestContentModel(unittest.TestCase):
    def _event(self, event_type: str):
        return samples.sample_events()[event_type]

    def test_every_event_type_has_a_template_and_stays_phone_sized(self):
        all_samples = samples.sample_events()
        self.assertEqual(set(all_samples), set(events.EVENT_TYPES), "one sample per event type")
        for event_type, event in all_samples.items():
            content = render.build_content(event)
            self.assertTrue(content.headline.strip(), event_type)
            self.assertIn(render.SEVERITY_SQUARE[event.resolved_severity()], content.headline, event_type)
            lines = content.as_lines()
            filled = [line for line in lines if line.strip()]
            self.assertLessEqual(len(filled), render.MAX_LINES, event_type)

    def test_context_block_is_labelled_a_measurement_not_a_probability(self):
        event = self._event("position_opened")
        body = render.to_plain(event.content)
        self.assertIn("Sinyal bağlamı (ölçüm, olasılık değil)", body)
        self.assertIn("Filtre marjları:", body)
        self.assertNotIn("güven", body.lower())
        self.assertNotIn("olasılık:", body.lower())

    def test_context_block_is_omitted_when_nothing_was_measured(self):
        event = events.position_opened(run_id="r", pair="BTC/USDT", entry_price=100.0, qty=1.0,
                                       notional=100.0, stop_price=97.5, tp_price=102.0)
        body = render.to_plain(event.content)
        self.assertNotIn("Sinyal bağlamı", body)

    def test_history_block_states_scope_hash_and_disclaimer(self):
        history = {
            "pairs": ["BTC/USDT", "ETH/USDT"], "timeframe": "1h", "days": 180.0,
            "trades": 18, "win_rate_pct": 50.0, "median_net_pnl_pct": 0.0,
            "tp_exit_pct": 41.0, "sl_exit_pct": 33.0, "config_hash": "aa5725245536",
            "net_target_pct": 2.0, "stop_loss_pct": 2.5, "start_ts": 1, "end_ts": 2,
        }
        event = events.position_opened(run_id="r", pair="BTC/USDT", entry_price=71679.51,
                                       qty=0.00063, notional=45.0, stop_price=69887.52,
                                       tp_price=73296.10, net_target_pct=2.0, stop_loss_pct=2.5,
                                       indicators={"close": 71679.51, "bb_upper": 72543.51,
                                                   "bb_lower": 71743.51, "rsi": 31.0,
                                                   "sma_trend": 69524.0},
                                       strategy_params={"bb_std": 2.0, "rsi_oversold": 35.0,
                                                        "trend_sma_period": 200},
                                       history=history)
        body = render.to_plain(event.content)
        self.assertIn("Geçmiş ölçüm (180 gün · 1h · BTCUSDT/ETHUSDT)", body)
        self.assertIn("Config referansı: aa5725245536", body)
        self.assertIn("Tarihsel ölçüm; kâr garantisi değil", body)
        self.assertIn("hedefe dokunma: %41", body)

    def test_history_block_is_absent_without_a_measurement(self):
        plain = events.position_opened(run_id="r", pair="BTC/USDT", entry_price=100.0,
                                       qty=1.0, notional=100.0, stop_price=97.5, tp_price=102.0)
        self.assertNotIn("Geçmiş ölçüm", render.to_plain(plain.content))

    def test_position_closed_shows_every_required_field(self):
        event = samples.sample_events()["position_closed"]
        body = render.to_plain(event.content)
        for fragment in ("Giriş → Çıkış", "Miktar", "Komisyon", "Brüt", "Net", "Neden", "Süre"):
            self.assertIn(fragment, body)

    def test_unrenderable_data_never_raises(self):
        weird = events.make_event("position_opened", "t", "b", data={"entry_price": "x",
                                                                    "qty": object(),
                                                                    "indicators": "not-a-mapping"})
        render.build_content(weird)
        render.render_body(weird, "markdown")
        render.render_body(events.make_event("unknown_type", "t", "b"), "html")


class TestCarriers(unittest.TestCase):
    def setUp(self):
        self.event = samples.sample_events()["position_opened"]

    def test_markdown_bolds_headline_and_sections(self):
        text = render.to_markdown(self.event.content)
        self.assertTrue(text.startswith("**"), text)
        self.assertIn("**📊 Sinyal bağlamı (ölçüm, olasılık değil)**", text)

    def test_plain_has_no_markup(self):
        text = render.to_plain(self.event.content)
        self.assertNotIn("**", text)
        self.assertNotIn("<b>", text)

    def test_html_bolds_and_escapes(self):
        event = events.position_closed(run_id="r", pair="BTC/USDT", entry_price=1.0, exit_price=0.9,
                                       qty=1.0, fees=0.01, gross_pnl=-0.1, net_pnl=-0.11,
                                       net_pnl_pct=-11.0, trigger="strategy",
                                       exit_reason="<script>alert(1)</script>")
        text = render.to_html(event.content)
        self.assertIn("<b>", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_all_carriers_render_the_same_headline(self):
        for carrier in render.CARRIERS:
            text = render.render_body(self.event, carrier)
            self.assertIn("BTCUSDT · LONG (PAPER)", text, carrier)


class TestHistoryStatsProvider(unittest.TestCase):
    def _config(self, tmp: Path):
        return tmp_config(tmp, pairs=("BTC/USDT",), timeframe="1h")

    def test_real_backtest_over_the_cache_is_reduced_to_stats(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self._config(Path(tmp_dir))
            seed_cache(config, "BTC/USDT", make_frame(700, make_entries=True, seed=21))
            stats = context.HistoryStatsProvider(config).get()
            self.assertIsNotNone(stats)
            payload = stats.as_dict()
            self.assertIsInstance(payload["trades"], int)
            self.assertGreater(payload["bars"], 0)
            self.assertEqual(len(payload["config_hash"]), 12)
            self.assertEqual(payload["pairs"], ["BTC/USDT"])
            self.assertEqual(payload["timeframe"], "1h")

    def test_no_cache_means_no_stats_block(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self._config(Path(tmp_dir))
            self.assertIsNone(context.HistoryStatsProvider(config).get())

    def test_provider_is_memoised(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self._config(Path(tmp_dir))
            seed_cache(config, "BTC/USDT", make_frame(700, make_entries=True, seed=21))
            provider = context.HistoryStatsProvider(config)
            first = provider.get()
            second = provider.get()
            self.assertIs(first, second)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
