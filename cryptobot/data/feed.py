"""Public Binance market data: download, retry/backoff, caching, outage handling.

Only unauthenticated ``GET /api/v3/klines`` is used.  No key, no signature, no
account endpoint -- see :mod:`cryptobot.safety` for the host whitelist.

The transport is injectable so the whole retry/failure matrix can be tested
offline with fixtures (see ``cryptobot/tests/test_data_feed.py``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from .. import safety
from ..config import Config, TIMEFRAME_BARS_PER_YEAR

log = logging.getLogger(__name__)

KLINES_PATH = "/api/v3/klines"
MAX_LIMIT = 1000
CACHE_COLUMNS: Tuple[str, ...] = ("ts", "open", "high", "low", "close", "volume")
_MINUTE_MS = 60_000
_INTERVAL_MS: Dict[str, int] = {
    "1m": _MINUTE_MS, "3m": 3 * _MINUTE_MS, "5m": 5 * _MINUTE_MS, "15m": 15 * _MINUTE_MS,
    "30m": 30 * _MINUTE_MS, "1h": 60 * _MINUTE_MS, "2h": 120 * _MINUTE_MS,
    "4h": 240 * _MINUTE_MS, "6h": 360 * _MINUTE_MS, "8h": 480 * _MINUTE_MS,
    "12h": 720 * _MINUTE_MS, "1d": 1440 * _MINUTE_MS,
}


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class FeedError(RuntimeError):
    """Base class for every data-feed problem."""

    retryable = False


class FeedTimeout(FeedError):
    """The HTTP request timed out."""

    retryable = True


class FeedUnavailable(FeedError):
    """The exchange is unreachable / returned 5xx after all retries."""

    retryable = True


class FeedRateLimited(FeedError):
    """HTTP 418/429 -- retry with backoff."""

    retryable = True


class FeedRequestRejected(FeedError):
    """The request itself is bad (4xx, unknown symbol, bad interval)."""

    retryable = False


class FeedMalformedResponse(FeedError):
    """The payload was not the shape we expect (JSON error, short rows, NaNs)."""

    retryable = True


class CacheCorrupted(FeedError):
    """A cached CSV is unreadable or fails integrity checks."""

    retryable = False


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HttpResponse:
    """Minimal transport-agnostic HTTP response."""

    status_code: int
    text: str

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise FeedMalformedResponse("response body is not JSON: {}".format(exc)) from exc


class RequestsTransport:
    """Default transport: ``requests`` with a hard timeout, no credentials."""

    def __init__(self, *, verify: bool = True) -> None:
        self.verify = verify
        self._session = None

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
            # Explicitly strip any auth-ish header defaults: this bot is anonymous.
            self._session.headers.update({"User-Agent": "cryptobot-paper/1.0 (public-data-only)"})
        return self._session

    def get(self, url: str, params: Mapping[str, Any], timeout: float) -> HttpResponse:
        safety.assert_public_endpoint(url)
        try:
            resp = self.session.get(url, params=dict(params), timeout=timeout, verify=self.verify)
        except Exception as exc:  # requests exceptions, DNS, SSL, proxy...
            name = type(exc).__name__
            if "Timeout" in name:
                raise FeedTimeout("request to {} timed out after {}s".format(url, timeout)) from exc
            raise FeedUnavailable("request to {} failed: {}".format(url, exc)) from exc
        return HttpResponse(status_code=resp.status_code, text=resp.text)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


class CcxtTransport:
    """Optional fallback transport backed by ``ccxt`` public endpoints."""

    def __init__(self, exchange_id: str = "binance") -> None:
        import ccxt  # imported lazily: only used if explicitly requested

        self._exchange = getattr(ccxt, exchange_id)({
            "enableRateLimit": True,
            "timeout": 20_000,
        })
        # Public endpoints only; credentials are deliberately never set.

    def get(self, url: str, params: Mapping[str, Any], timeout: float) -> HttpResponse:
        symbol = params.get("symbol", "")
        interval = params.get("interval", "1h")
        start = params.get("startTime")
        limit = int(params.get("limit", MAX_LIMIT))
        try:
            rows = self._exchange.fetch_ohlcv(
                "{}/{}".format(symbol[:-4], symbol[-4:]) if symbol.endswith("USDT") else symbol,
                timeframe=interval,
                since=start,
                limit=limit,
            )
        except Exception as exc:
            name = type(exc).__name__
            if "Timeout" in name or "NetworkError" in name:
                raise FeedTimeout("ccxt fetch_ohlcv failed: {}".format(exc)) from exc
            raise FeedUnavailable("ccxt fetch_ohlcv failed: {}".format(exc)) from exc
        # Normalise to Binance kline shape.
        payload = [
            [int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5]), 0, "0", 0, "0", "0", "0"]
            for r in rows
        ]
        return HttpResponse(200, json.dumps(payload))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def interval_to_ms(timeframe: str) -> int:
    try:
        return _INTERVAL_MS[timeframe]
    except KeyError as exc:
        raise FeedRequestRejected("unsupported timeframe {!r}".format(timeframe)) from exc


def exchange_symbol(pair: str) -> str:
    """``BTC/USDT`` -> ``BTCUSDT``."""
    return pair.replace("/", "").upper()


def cache_path(cache_dir: Path, pair: str, timeframe: str) -> Path:
    return Path(cache_dir) / "{}_{}.csv".format(exchange_symbol(pair), timeframe)


def default_sleep(seconds: float) -> None:
    time.sleep(seconds)


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# retry wrapper
# --------------------------------------------------------------------------- #
def retry_call(
    fn: Callable[[], Any],
    *,
    max_retries: int,
    backoff_initial: float,
    backoff_max: float,
    sleep: Callable[[float], None] = default_sleep,
    on_retry: Optional[Callable[[int, float, Exception], None]] = None,
    jitter: Callable[[float], float] = lambda d: d,
) -> Any:
    """Call ``fn`` with bounded exponential backoff.

    Non-retryable :class:`FeedError` subclasses propagate immediately.  Sleep is
    injectable so tests never actually wait.
    """
    attempt = 0
    delay = float(backoff_initial)
    while True:
        try:
            return fn()
        except FeedError as exc:
            if not exc.retryable or attempt >= max_retries:
                raise
            attempt += 1
            wait = jitter(min(delay, backoff_max))
            if on_retry is not None:
                on_retry(attempt, wait, exc)
            log.warning(
                "feed.retry",
                extra={"event": "feed_retry", "attempt": attempt, "sleep_seconds": round(wait, 3),
                       "error": type(exc).__name__, "detail": str(exc)},
            )
            sleep(wait)
            delay = min(delay * 2.0, backoff_max)


# --------------------------------------------------------------------------- #
# parsing / validation
# --------------------------------------------------------------------------- #
def parse_klines(payload: Any, *, symbol: str = "") -> List[List[float]]:
    """Validate and normalise a raw klines payload into numeric rows."""
    if isinstance(payload, Mapping):  # Binance error object, e.g. {"code":-1121,"msg":"Invalid symbol."}
        raise FeedRequestRejected(
            "exchange returned an error object for {}: {}".format(symbol or "?", payload.get("msg", payload))
        )
    if not isinstance(payload, list):
        raise FeedMalformedResponse("expected a list of klines for {}, got {}".format(symbol, type(payload).__name__))

    rows: List[List[float]] = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            raise FeedMalformedResponse("kline #{} for {} is not a 6+ element list".format(index, symbol))
        try:
            ts = int(raw[0])
            o, h, low, c, v = (float(raw[i]) for i in range(1, 6))
        except (TypeError, ValueError) as exc:
            raise FeedMalformedResponse("kline #{} for {} has non-numeric fields".format(index, symbol)) from exc
        if not (low > 0 and h > 0 and o > 0 and c > 0) or h < low or v < 0:
            raise FeedMalformedResponse(
                "kline #{} for {} is out of range (o={} h={} l={} c={})".format(index, symbol, o, h, low, c)
            )
        if h < max(o, c) or low > min(o, c):
            raise FeedMalformedResponse("kline #{} for {} has an inconsistent high/low".format(index, symbol))
        rows.append([ts, o, h, low, c, v])
    return rows


def rows_to_frame(rows: Sequence[Sequence[float]]) -> pd.DataFrame:
    """Build a typed OHLCV frame. Accepts raw 12-field kline rows (extra fields dropped)."""
    normalised = [list(row)[: len(CACHE_COLUMNS)] for row in rows]
    frame = pd.DataFrame(normalised, columns=list(CACHE_COLUMNS))
    if frame.empty:
        return frame.astype({"ts": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
    frame["ts"] = frame["ts"].astype("int64")
    for col in CACHE_COLUMNS[1:]:
        frame[col] = frame[col].astype(float)
    return frame.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def validate_frame(frame: pd.DataFrame, *, context: str) -> pd.DataFrame:
    """Integrity checks shared by network data and cache files."""
    missing = [c for c in CACHE_COLUMNS if c not in frame.columns]
    if missing:
        raise CacheCorrupted("{}: missing column(s) {}".format(context, ", ".join(missing)))
    if frame.empty:
        raise CacheCorrupted("{}: no rows".format(context))
    if frame[list(CACHE_COLUMNS)].isna().any().any():
        raise CacheCorrupted("{}: contains NaN".format(context))
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise CacheCorrupted("{}: contains non-positive prices".format(context))
    if (frame["high"] < frame[["open", "close"]].max(axis=1)).any():
        raise CacheCorrupted("{}: high < open/close on some row".format(context))
    if (frame["low"] > frame[["open", "close"]].min(axis=1)).any():
        raise CacheCorrupted("{}: low > open/close on some row".format(context))
    if (frame["volume"] < 0).any():
        raise CacheCorrupted("{}: negative volume".format(context))
    if not frame["ts"].is_monotonic_increasing:
        raise CacheCorrupted("{}: timestamps are not strictly increasing".format(context))
    if frame["ts"].duplicated().any():
        raise CacheCorrupted("{}: duplicate timestamps".format(context))
    return frame


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
def save_cache(frame: pd.DataFrame, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = frame.loc[:, list(CACHE_COLUMNS)].copy()
    out["ts"] = out["ts"].astype("int64")
    # Fixed float formatting keeps the cache byte-stable across runs.
    out.to_csv(path, index=False, float_format="%.8f", lineterminator="\n")
    log.info("cache.saved", extra={"event": "cache_saved", "path": str(path), "rows": int(len(out))})
    return path


def load_cache(path: Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise CacheCorrupted("cache file not found: {}".format(path))
    try:
        frame = pd.read_csv(path, dtype={"ts": "int64"})
    except Exception as exc:
        raise CacheCorrupted("could not parse {}: {}".format(path, exc)) from exc
    return validate_frame(frame, context=str(path))


def quarantine_cache(path: Path) -> Optional[Path]:
    """Move a corrupt cache file aside so it cannot be silently reused."""
    path = Path(path)
    if not path.exists():
        return None
    target = path.with_suffix(path.suffix + ".corrupt")
    counter = 1
    while target.exists():
        target = path.with_suffix(path.suffix + ".corrupt{}".format(counter))
        counter += 1
    path.replace(target)
    log.error("cache.quarantined", extra={"event": "cache_quarantined", "path": str(path), "moved_to": str(target)})
    return target


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def fetch_klines_range(
    *,
    api_base: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    transport: Any,
    timeout: float = 20.0,
    max_retries: int = 5,
    backoff_initial: float = 1.0,
    backoff_max: float = 30.0,
    sleep: Callable[[float], None] = default_sleep,
) -> List[List[float]]:
    """Page through ``GET /api/v3/klines`` until ``end_ms`` is covered."""
    step = interval_to_ms(interval)
    url = "{}{}".format(api_base.rstrip("/"), KLINES_PATH)
    rows: List[List[float]] = []
    cursor = int(start_ms)
    pages = 0
    max_pages = 500

    while cursor < end_ms and pages < max_pages:
        pages += 1
        params = {"symbol": symbol, "interval": interval, "startTime": cursor, "limit": MAX_LIMIT}

        def _once(_params: Dict[str, Any] = params) -> List[List[float]]:
            """One page: fetch *and* validate, so a malformed body is retried too."""
            response = transport.get(url, _params, timeout)
            if response.status_code in (418, 429):
                raise FeedRateLimited("rate limited (HTTP {})".format(response.status_code))
            if response.status_code >= 500:
                raise FeedUnavailable("server error HTTP {}".format(response.status_code))
            if response.status_code >= 400:
                raise FeedRequestRejected("HTTP {} for {}".format(response.status_code, symbol))
            return parse_klines(response.json(), symbol=symbol)

        page = retry_call(
            _once,
            max_retries=max_retries,
            backoff_initial=backoff_initial,
            backoff_max=backoff_max,
            sleep=sleep,
        )
        if not page:
            break
        rows.extend(page)
        last_ts = page[-1][0]
        next_cursor = last_ts + step
        if next_cursor <= cursor:  # defensive: never loop forever
            break
        cursor = next_cursor

    if pages >= max_pages:
        raise FeedUnavailable("pagination limit reached for {} ({} pages)".format(symbol, pages))
    # Keep only *closed* bars: Binance also returns the still-forming candle whose
    # openTime is the current boundary.  Using it would be look-ahead at runtime.
    return [row for row in rows if int(row[0]) < int(end_ms)]


@dataclass
class FeedResult:
    """Outcome of a data request, including staleness/health information."""

    pair: str
    frame: pd.DataFrame
    source: str  # "network" | "cache" | "cache-stale"
    complete: bool = True
    warnings: List[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.source == "network" or self.complete

    @property
    def rows(self) -> int:
        return 0 if self.frame is None else int(len(self.frame))


def fetch_history(
    config: Config,
    pair: str,
    *,
    days: Optional[int] = None,
    transport: Any = None,
    sleep: Callable[[float], None] = default_sleep,
    end_ts_ms: Optional[int] = None,
    force_network: bool = False,
) -> FeedResult:
    """Fetch ``days`` of candles for ``pair`` and refresh the cache.

    On a network outage the caller gets a fail-safe result: the last good cache
    is returned with ``complete=False`` and a warning, so the bot can keep
    observing but must stop opening positions.
    """
    days = int(days if days is not None else config.data.history_days)
    step = interval_to_ms(config.timeframe)
    end_ms = int(end_ts_ms if end_ts_ms is not None else now_ms())
    end_ms -= end_ms % step  # align to a closed bar
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    start_ms -= start_ms % step
    path = cache_path(config.data.cache_dir, pair, config.timeframe)

    cached: Optional[pd.DataFrame] = None
    if path.exists() and not force_network:
        try:
            cached = load_cache(path)
        except CacheCorrupted as exc:
            log.error(
                "cache.corrupt",
                extra={"event": "cache_corrupt", "path": str(path), "detail": str(exc)},
            )
            quarantine_cache(path)
            cached = None

    transport = transport or RequestsTransport()
    try:
        rows = fetch_klines_range(
            api_base=config.data.api_base,
            symbol=exchange_symbol(pair),
            interval=config.timeframe,
            start_ms=start_ms,
            end_ms=end_ms,
            transport=transport,
            timeout=config.data.request_timeout_seconds,
            max_retries=config.data.max_retries,
            backoff_initial=config.data.backoff_initial_seconds,
            backoff_max=config.data.backoff_max_seconds,
            sleep=sleep,
        )
    except FeedError as exc:
        log.error(
            "feed.unavailable",
            extra={"event": "feed_unavailable", "pair": pair, "error": type(exc).__name__, "detail": str(exc)},
        )
        if cached is not None:
            return FeedResult(
                pair=pair,
                frame=cached,
                source="cache-stale",
                complete=False,
                warnings=["network unavailable ({}); using stale cache from {}".format(type(exc).__name__, path.name)],
            )
        raise

    if not rows:
        if cached is not None:
            return FeedResult(pair, cached, "cache-stale", False, ["exchange returned no candles; using cache"])
        raise FeedUnavailable("no candles returned for {}".format(pair))

    frame = validate_frame(rows_to_frame(rows), context="network:{}".format(pair))

    warnings: List[str] = []
    complete = True
    first_ts = int(frame["ts"].iloc[0])
    last_ts = int(frame["ts"].iloc[-1])
    if abs(first_ts - start_ms) > step:
        complete = False
        warnings.append(
            "history starts later than requested ({} vs {}); exchange may not have that much data".format(
                first_ts, start_ms
            )
        )
    if end_ms - last_ts > 2 * step:
        complete = False
        warnings.append("history ends {}ms before now; last bar may not be closed yet".format(end_ms - last_ts))

    save_cache(frame, path)
    for warning in warnings:
        log.warning("feed.incomplete", extra={"event": "feed_incomplete", "pair": pair, "detail": warning})
    return FeedResult(pair=pair, frame=frame, source="network", complete=complete, warnings=warnings)


def cache_is_stale(config: Config, frame: pd.DataFrame, now_ts_ms: int) -> Optional[str]:
    """Return a warning string when the newest cached bar is older than allowed.

    Only meaningful in **real-time paper mode**; backtest/replay deliberately use
    historical candles and must never be flagged stale (see :func:`load_candles`).
    """
    if frame is None or frame.empty:
        return None
    step = interval_to_ms(config.timeframe)
    allowed = max(1, int(config.data.max_cache_age_bars)) * step
    newest = int(frame["ts"].iloc[-1])
    age = int(now_ts_ms) - newest
    if age <= allowed:
        return None
    return (
        "stale cache: newest cached bar is {:.1f} bars old ({:.1f} bars allowed by "
        "data.max_cache_age_bars={}); refusing to trade on stale data".format(
            age / step, allowed / step, int(config.data.max_cache_age_bars)
        )
    )


def load_candles(
    config: Config,
    pair: str,
    *,
    days: Optional[int] = None,
    allow_network: bool = True,
    transport: Any = None,
    sleep: Callable[[float], None] = default_sleep,
    fallback_provider: Optional[Callable[[Config, str], FeedResult]] = None,
    realtime: bool = False,
    now_ts_ms: Optional[int] = None,
) -> FeedResult:
    """Cache-first candle provider used by the backtester and the paper loop.

    ``allow_network=False`` makes the call fully offline (used by tests and by
    ``--offline`` backtests); a missing cache then raises :class:`CacheCorrupted`.

    ``realtime=True`` (real-time paper mode only -- replay off *and* offline off)
    enables the staleness bound: a cache whose newest bar is older than
    ``data.max_cache_age_bars`` is not trusted, so the provider tries to refresh
    it from the network.  If the exchange is unreachable, the stale cache is
    returned with ``source="cache-stale"`` and ``complete=False`` (the fail-safe
    that makes the engine pause new entries).  Backtest and replay always pass
    ``realtime=False`` so their historical caches stay ``complete=True``.
    """
    path = cache_path(config.data.cache_dir, pair, config.timeframe)
    if path.exists():
        try:
            frame = load_cache(path)
        except CacheCorrupted as exc:
            log.error("cache.corrupt", extra={"event": "cache_corrupt", "path": str(path), "detail": str(exc)})
            quarantine_cache(path)
            if not allow_network:
                raise
        else:
            needed_bars = int(
                (days or config.data.history_days) * 24 * 60 * 60 * 1000 / interval_to_ms(config.timeframe)
            )
            complete = len(frame) >= min(needed_bars, 50)
            source = "cache"
            warnings: List[str] = []
            if realtime:
                warning = cache_is_stale(
                    config, frame, int(now_ts_ms) if now_ts_ms is not None else now_ms()
                )
                if warning is not None:
                    complete = False
                    source = "cache-stale"
                    warnings.append(warning)
                    log.warning(
                        "feed.stale_cache",
                        extra={"event": "feed_stale_cache", "pair": pair, "detail": warning},
                    )
            # Only real-time mode may fall through to the network: a stale or
            # incomplete cache there must be refreshed so a outage surfaces as a
            # fail-safe result instead of being silently traded on.  Historic
            # (backtest/replay) caches are returned as-is.
            if not (realtime and allow_network and not complete):
                return FeedResult(pair, frame, source, complete, warnings)

    if not allow_network:
        raise CacheCorrupted(
            "no usable cache for {} ({}) and offline mode is active".format(pair, path)
        )

    try:
        return fetch_history(config, pair, days=days, transport=transport, sleep=sleep)
    except FeedError:
        if fallback_provider is not None:
            log.warning("feed.using_fallback", extra={"event": "feed_fallback", "pair": pair})
            return fallback_provider(config, pair)
        raise


__all__ = [
    "FeedError", "FeedTimeout", "FeedUnavailable", "FeedRateLimited",
    "FeedRequestRejected", "FeedMalformedResponse", "CacheCorrupted",
    "HttpResponse", "RequestsTransport", "CcxtTransport",
    "FeedResult", "retry_call", "parse_klines", "rows_to_frame", "validate_frame",
    "cache_path", "save_cache", "load_cache", "quarantine_cache", "cache_is_stale",
    "fetch_klines_range", "fetch_history", "load_candles",
    "interval_to_ms", "exchange_symbol", "now_ms", "CACHE_COLUMNS",
]
