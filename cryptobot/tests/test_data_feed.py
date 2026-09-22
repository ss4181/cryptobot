"""Data feed: retry/backoff, malformed payloads, cache corruption, outages (offline fixtures)."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from cryptobot.data.feed import (
    CacheCorrupted,
    FeedError,
    FeedMalformedResponse,
    FeedRateLimited,
    FeedRequestRejected,
    FeedTimeout,
    FeedUnavailable,
    HttpResponse,
    cache_path,
    exchange_symbol,
    fetch_history,
    fetch_klines_range,
    interval_to_ms,
    load_cache,
    load_candles,
    parse_klines,
    quarantine_cache,
    retry_call,
    rows_to_frame,
    save_cache,
    validate_frame,
)

from .fixtures import HOUR_MS, START_TS, FakeTransport, kline_row, klines_body, tmp_config


class TestHelpers(unittest.TestCase):
    def test_interval_ms(self):
        self.assertEqual(interval_to_ms("1h"), 3_600_000)
        self.assertEqual(interval_to_ms("15m"), 900_000)
        with self.assertRaises(FeedRequestRejected):
            interval_to_ms("7m")

    def test_exchange_symbol(self):
        self.assertEqual(exchange_symbol("BTC/USDT"), "BTCUSDT")

    def test_cache_path(self):
        path = cache_path(Path("data/cache"), "BTC/USDT", "1h")
        self.assertEqual(path.name, "BTCUSDT_1h.csv")


class TestRetryBackoff(unittest.TestCase):
    def test_exponential_backoff_and_eventual_success(self):
        sleeps: list = []
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise FeedTimeout("boom")
            return "ok"

        result = retry_call(flaky, max_retries=5, backoff_initial=1.0, backoff_max=30.0,
                            sleep=sleeps.append)
        self.assertEqual(result, "ok")
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(attempts["n"], 3)

    def test_backoff_is_capped(self):
        sleeps: list = []

        def always_timeout():
            raise FeedTimeout("boom")

        with self.assertRaises(FeedTimeout):
            retry_call(always_timeout, max_retries=6, backoff_initial=1.0, backoff_max=4.0,
                       sleep=sleeps.append)
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 4.0, 4.0, 4.0])

    def test_non_retryable_error_is_raised_immediately(self):
        sleeps: list = []
        calls = {"n": 0}

        def bad_request():
            calls["n"] += 1
            raise FeedRequestRejected("HTTP 400")

        with self.assertRaises(FeedRequestRejected):
            retry_call(bad_request, max_retries=5, backoff_initial=1.0, backoff_max=30.0,
                       sleep=sleeps.append)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(sleeps, [])

    def test_on_retry_callback(self):
        seen: list = []
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise FeedUnavailable("500")
            return 1

        retry_call(flaky, max_retries=3, backoff_initial=0.5, backoff_max=1.0, sleep=lambda _s: None,
                   on_retry=lambda attempt, wait, exc: seen.append((attempt, wait, type(exc).__name__)))
        self.assertEqual(seen, [(1, 0.5, "FeedUnavailable")])


class TestPayloadValidation(unittest.TestCase):
    def test_valid_payload(self):
        rows = parse_klines([kline_row(START_TS)], symbol="BTCUSDT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], START_TS)

    def test_error_object_is_not_retryable(self):
        with self.assertRaises(FeedRequestRejected):
            parse_klines({"code": -1121, "msg": "Invalid symbol."}, symbol="BTCUSDT")

    def test_non_list_payload(self):
        with self.assertRaises(FeedMalformedResponse):
            parse_klines("not-a-list", symbol="BTCUSDT")

    def test_short_row(self):
        with self.assertRaises(FeedMalformedResponse):
            parse_klines([[START_TS, "1", "2"]], symbol="BTCUSDT")

    def test_non_numeric_fields(self):
        with self.assertRaises(FeedMalformedResponse):
            parse_klines([[START_TS, "oops", "2", "1", "2", "3"]], symbol="BTCUSDT")

    def test_inconsistent_high_low(self):
        with self.assertRaises(FeedMalformedResponse):
            parse_klines([[START_TS, "100", "90", "80", "95", "10"]], symbol="BTCUSDT")

    def test_non_json_body(self):
        with self.assertRaises(FeedMalformedResponse):
            HttpResponse(200, "<html>gateway error</html>").json()

    def test_rows_to_frame_sorted_and_typed(self):
        frame = rows_to_frame([kline_row(START_TS), kline_row(START_TS + HOUR_MS)])
        self.assertEqual(list(frame.columns), ["ts", "open", "high", "low", "close", "volume"])
        self.assertEqual(str(frame["ts"].dtype), "int64")


class TestFrameValidation(unittest.TestCase):
    def _frame(self) -> pd.DataFrame:
        return rows_to_frame([kline_row(START_TS + i * HOUR_MS, 30_000 + i) for i in range(5)])

    def test_valid_frame_passes(self):
        validate_frame(self._frame(), context="test")

    def test_nan_rejected(self):
        frame = self._frame()
        frame.loc[2, "close"] = float("nan")
        with self.assertRaises(CacheCorrupted):
            validate_frame(frame, context="test")

    def test_duplicate_timestamp_rejected(self):
        frame = self._frame()
        frame.loc[3, "ts"] = frame.loc[2, "ts"]
        with self.assertRaises(CacheCorrupted):
            validate_frame(frame, context="test")

    def test_non_monotonic_rejected(self):
        frame = self._frame().iloc[::-1].reset_index(drop=True)
        with self.assertRaises(CacheCorrupted):
            validate_frame(frame, context="test")

    def test_high_below_close_rejected(self):
        frame = self._frame()
        frame.loc[1, "high"] = frame.loc[1, "close"] * 0.5
        with self.assertRaises(CacheCorrupted):
            validate_frame(frame, context="test")

    def test_non_positive_price_rejected(self):
        frame = self._frame()
        frame.loc[1, "low"] = 0.0
        with self.assertRaises(CacheCorrupted):
            validate_frame(frame, context="test")

    def test_missing_column_rejected(self):
        with self.assertRaises(CacheCorrupted):
            validate_frame(self._frame().drop(columns=["volume"]), context="test")

    def test_empty_rejected(self):
        with self.assertRaises(CacheCorrupted):
            validate_frame(self._frame().iloc[:0], context="test")


class TestPaginationAndOutage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = tmp_config(Path(self.tmp.name))
        self.end_ms = START_TS + 3 * HOUR_MS
        self.start_ms = START_TS

    def _range(self, transport, **kwargs):
        return fetch_klines_range(
            api_base="https://api.binance.com", symbol="BTCUSDT", interval="1h",
            start_ms=self.start_ms, end_ms=self.end_ms, transport=transport,
            timeout=5.0, max_retries=3, backoff_initial=1.0, backoff_max=4.0,
            sleep=lambda _s: None, **kwargs,
        )

    def test_pagination_collects_all_pages(self):
        page1 = [kline_row(START_TS + i * HOUR_MS) for i in range(2)]
        page2 = [kline_row(START_TS + 2 * HOUR_MS)]
        transport = FakeTransport([HttpResponse(200, klines_body(page1)),
                                   HttpResponse(200, klines_body(page2))])
        rows = self._range(transport)
        self.assertEqual(len(rows), 3)
        self.assertGreaterEqual(transport.call_count, 2)

    def test_open_bar_is_excluded(self):
        """A bar whose openTime is the current boundary is still forming."""
        rows = [kline_row(START_TS + i * HOUR_MS) for i in range(4)]  # includes ts == end_ms
        transport = FakeTransport([HttpResponse(200, klines_body(rows))])
        result = self._range(transport)
        self.assertEqual([row[0] for row in result], [START_TS, START_TS + HOUR_MS, START_TS + 2 * HOUR_MS])

    def test_timeout_then_success(self):
        """Two timeouts are retried with backoff; the page then succeeds.

        One extra request is expected: after a 1-row page the loop asks for the
        next page and receives the transport's empty default (end of data).
        """
        transport = FakeTransport([FeedTimeout("t1"), FeedTimeout("t2"),
                                   HttpResponse(200, klines_body([kline_row(START_TS)]))])
        rows = self._range(transport)
        self.assertEqual(len(rows), 1)
        self.assertEqual(transport.call_count, 4)

    def test_server_errors_exhaust_retries(self):
        transport = FakeTransport([HttpResponse(503, "unavailable") for _ in range(6)])
        with self.assertRaises(FeedUnavailable):
            self._range(transport)
        self.assertEqual(transport.call_count, 4)  # initial + 3 retries

    def test_rate_limit_is_retried(self):
        transport = FakeTransport([HttpResponse(429, "slow down"),
                                   HttpResponse(200, klines_body([kline_row(START_TS)]))])
        rows = self._range(transport)
        self.assertEqual(len(rows), 1)
        self.assertEqual(transport.call_count, 3)  # 429, ok, then empty next page

    def test_client_error_is_not_retried(self):
        transport = FakeTransport([HttpResponse(400, '{"msg":"bad symbol"}') for _ in range(3)])
        with self.assertRaises(FeedRequestRejected):
            self._range(transport)
        self.assertEqual(transport.call_count, 1)

    def test_malformed_body_is_retried_then_fails_safe(self):
        transport = FakeTransport([HttpResponse(200, "<html>") for _ in range(6)])
        with self.assertRaises(FeedMalformedResponse):
            self._range(transport)
        self.assertEqual(transport.call_count, 4)  # retried, not a one-shot failure

    def test_exchange_error_object_is_rejected_without_retry(self):
        transport = FakeTransport([HttpResponse(200, json.dumps({"code": -1121, "msg": "Invalid symbol."}))])
        with self.assertRaises(FeedRequestRejected):
            self._range(transport)
        self.assertEqual(transport.call_count, 1)

    def test_backoff_sleeps_are_recorded_by_fetch_history(self):
        sleeps: list = []
        attempts = {"n": 0}

        def flaky_get(url, params, timeout):
            attempts["n"] += 1
            if attempts["n"] <= 4:
                raise FeedTimeout("down")
            return HttpResponse(200, klines_body([kline_row(START_TS + i * HOUR_MS) for i in range(30)]))

        transport = type("T", (), {"get": staticmethod(flaky_get)})()
        result = fetch_history(self.config, "BTC/USDT", days=1, transport=transport,
                               sleep=sleeps.append, end_ts_ms=START_TS + 30 * HOUR_MS)
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 8.0])  # bounded exponential backoff
        self.assertEqual(result.source, "network")


class TestCacheHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = tmp_config(Path(self.tmp.name))
        self.frame = rows_to_frame([kline_row(START_TS + i * HOUR_MS, 30_000 + i) for i in range(10)])
        self.path = cache_path(self.config.data.cache_dir, "BTC/USDT", self.config.timeframe)

    def test_save_load_round_trip(self):
        save_cache(self.frame, self.path)
        loaded = load_cache(self.path)
        pd.testing.assert_frame_equal(loaded, self.frame)

    def test_cache_is_byte_stable(self):
        save_cache(self.frame, self.path)
        first = self.path.read_bytes()
        save_cache(load_cache(self.path), self.path)
        self.assertEqual(first, self.path.read_bytes())

    def test_missing_cache_raises(self):
        with self.assertRaises(CacheCorrupted):
            load_cache(self.path)

    def test_corrupt_csv_raises(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("ts,open,high\nnot-a-number,1,2\n", encoding="utf-8")
        with self.assertRaises(CacheCorrupted):
            load_cache(self.path)

    def test_corrupt_values_raise(self):
        frame = self.frame.copy()
        frame.loc[3, "high"] = 1.0
        frame.to_csv(self.path, index=False)
        with self.assertRaises(CacheCorrupted):
            load_cache(self.path)

    def test_quarantine_moves_the_file_aside(self):
        save_cache(self.frame, self.path)
        moved = quarantine_cache(self.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(moved.exists())
        self.assertTrue(str(moved).endswith(".corrupt"))

    def test_offline_load_without_cache_fails_closed(self):
        with self.assertRaises(CacheCorrupted):
            load_candles(self.config, "BTC/USDT", allow_network=False)

    def test_offline_load_from_cache(self):
        save_cache(self.frame, self.path)
        result = load_candles(self.config, "BTC/USDT", allow_network=False)
        self.assertEqual(result.source, "cache")
        self.assertEqual(result.rows, 10)

    def test_corrupt_cache_is_quarantined_and_refetched(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("garbage\n", encoding="utf-8")
        transport = FakeTransport([HttpResponse(200, klines_body(
            [kline_row(START_TS + i * HOUR_MS, 30_000 + i) for i in range(30)]))])
        result = fetch_history(self.config, "BTC/USDT", days=1, transport=transport,
                              sleep=lambda _s: None, end_ts_ms=START_TS + 30 * HOUR_MS)
        self.assertEqual(result.source, "network")
        self.assertTrue(any(p.name.endswith(".corrupt") for p in self.path.parent.iterdir()))
        self.assertTrue(self.path.exists())

    def test_network_outage_with_cache_returns_stale_fail_safe_result(self):
        saved = rows_to_frame([kline_row(START_TS + i * HOUR_MS, 30_000 + i) for i in range(40)])
        save_cache(saved, self.path)
        transport = FakeTransport([FeedTimeout("down") for _ in range(10)])
        result = fetch_history(self.config, "BTC/USDT", days=1, transport=transport,
                              sleep=lambda _s: None, end_ts_ms=START_TS + 40 * HOUR_MS)
        self.assertEqual(result.source, "cache-stale")
        self.assertFalse(result.complete)
        self.assertFalse(result.healthy)
        self.assertTrue(result.warnings)
        self.assertEqual(result.rows, 40)

    def test_network_outage_without_cache_raises(self):
        transport = FakeTransport([FeedTimeout("down") for _ in range(10)])
        with self.assertRaises(FeedError) as ctx:
            fetch_history(self.config, "BTC/USDT", days=1, transport=transport,
                          sleep=lambda _s: None, end_ts_ms=START_TS + HOUR_MS)
        self.assertIsInstance(ctx.exception, FeedTimeout)
        self.assertEqual(transport.call_count, self.config.data.max_retries + 1)

    def test_fallback_provider_is_used(self):
        transport = FakeTransport([FeedTimeout("down") for _ in range(10)])
        fallback = lambda config, pair: type("R", (), {"pair": pair, "frame": self.frame,
                                                       "source": "fallback", "complete": False,
                                                       "warnings": ["fallback"], "rows": len(self.frame)})()
        result = load_candles(self.config, "BTC/USDT", allow_network=True, transport=transport,
                              sleep=lambda _s: None, fallback_provider=fallback)
        self.assertEqual(result.source, "fallback")

    def test_unrealistic_history_marks_result_incomplete(self):
        transport = FakeTransport([HttpResponse(200, klines_body([kline_row(START_TS)]))])
        result = fetch_history(self.config, "BTC/USDT", days=180, transport=transport,
                              sleep=lambda _s: None, end_ts_ms=START_TS + 2 * HOUR_MS)
        self.assertFalse(result.complete)
        self.assertTrue(result.warnings)


class TestCacheStaleness(unittest.TestCase):
    """Real-time staleness bound: live mode must not trade an old cache.

    Backtest/replay deliberately run on historical candles, so the very same
    cache must stay ``complete=True`` when ``realtime=False``.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = tmp_config(Path(self.tmp.name))
        self.path = cache_path(self.config.data.cache_dir, "BTC/USDT", self.config.timeframe)
        self.frame = rows_to_frame([kline_row(START_TS + i * HOUR_MS, 30_000 + i) for i in range(60)])
        save_cache(self.frame, self.path)
        self.newest = int(self.frame["ts"].iloc[-1])

    def test_stale_cache_in_live_mode_is_incomplete_fail_safe(self):
        transport = FakeTransport([FeedTimeout("down") for _ in range(40)])
        result = load_candles(self.config, "BTC/USDT", allow_network=True, transport=transport,
                              sleep=lambda _s: None, realtime=True,
                              now_ts_ms=self.newest + 10 * HOUR_MS)
        self.assertEqual(result.source, "cache-stale")
        self.assertFalse(result.complete)
        self.assertFalse(result.healthy)
        self.assertTrue(result.warnings)
        self.assertEqual(result.rows, 60)
        # The stale cache must trigger a refresh attempt (not a silent short-circuit).
        self.assertGreater(transport.call_count, 0)

    def test_fresh_cache_in_live_mode_stays_complete_without_network(self):
        transport = FakeTransport([])
        result = load_candles(self.config, "BTC/USDT", allow_network=True, transport=transport,
                              sleep=lambda _s: None, realtime=True,
                              now_ts_ms=self.newest + HOUR_MS)
        self.assertEqual(result.source, "cache")
        self.assertTrue(result.complete)
        self.assertEqual(transport.call_count, 0, "fresh cache must not hit the network")

    def test_same_stale_cache_is_complete_for_backtest_and_replay(self):
        transport = FakeTransport([FeedTimeout("down") for _ in range(40)])
        result = load_candles(self.config, "BTC/USDT", allow_network=True, transport=transport,
                              sleep=lambda _s: None, realtime=False,
                              now_ts_ms=self.newest + 10_000 * HOUR_MS)
        self.assertEqual(result.source, "cache")
        self.assertTrue(result.complete)
        self.assertEqual(transport.call_count, 0)

    def test_staleness_bound_is_configurable(self):
        relaxed = dataclasses.replace(
            self.config,
            data=dataclasses.replace(self.config.data, max_cache_age_bars=100),
        )
        result = load_candles(relaxed, "BTC/USDT", allow_network=False, realtime=True,
                              now_ts_ms=self.newest + 10 * HOUR_MS)
        self.assertEqual(result.source, "cache")
        self.assertTrue(result.complete)

    def test_default_bound_is_three_bars(self):
        self.assertEqual(self.config.data.max_cache_age_bars, 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
