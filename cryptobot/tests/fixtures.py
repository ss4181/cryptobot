"""Shared, fully offline test fixtures.

No test in this suite touches the network: candle data is either synthesised
deterministically or served by :class:`FakeTransport`.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from cryptobot.config import Config, load_config
from cryptobot.data.feed import CACHE_COLUMNS, FeedTimeout, HttpResponse, save_cache

HOUR_MS = 3_600_000
START_TS = 1_600_000_000_000


# --------------------------------------------------------------------------- #
# candle synthesis
# --------------------------------------------------------------------------- #
def make_frame(
    n: int = 400,
    *,
    start_ts: int = START_TS,
    step_ms: int = HOUR_MS,
    start_price: float = 30_000.0,
    drift: float = 0.0,
    amplitude: float = 400.0,
    period: float = 7.0,
    noise: float = 0.0,
    seed: int = 1,
    make_entries: bool = False,
) -> pd.DataFrame:
    """Deterministic OHLCV frame.

    ``make_entries=True`` produces a saw-tooth that repeatedly dips below the
    lower Bollinger band while staying above the long SMA, so the mean-reversion
    strategy actually enters -- useful for exercising full trade cycles offline.
    """
    rng = np.random.default_rng(seed)
    index = np.arange(n, dtype=float)
    if make_entries:
        base = start_price + drift * index + amplitude * np.sin(index / period)
        # Sharp periodic dips create the oversold condition.
        dips = 0.06 * start_price * np.clip(np.sin(index / (period * 2.5)), -1, 1) ** 3
        close = base - dips
    else:
        close = start_price + drift * index + amplitude * np.sin(index / period)
    if noise:
        close = close + rng.normal(0.0, noise, n)

    close = np.maximum(close, 1.0)
    open_ = np.concatenate([[close[0]], close[:-1]])
    span = close * 0.004
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    ts = start_ts + np.arange(n, dtype=np.int64) * step_ms
    return pd.DataFrame({
        "ts": ts,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.full(n, 12.5, dtype=float),
    })


def frame_with_entry(frame: pd.DataFrame, strategy: Any, *, lookback: int = 250) -> int:
    """Return the first index at which ``strategy`` would ENTER (or -1)."""
    prepared = strategy.prepare(frame)
    for index in range(max(lookback, strategy.warmup_bars()), len(prepared)):
        if strategy.evaluate(prepared, index, has_position=False).is_entry:
            return index
    return -1


# --------------------------------------------------------------------------- #
# HTTP fixtures
# --------------------------------------------------------------------------- #
def kline_row(
    ts: int,
    price: float = 30_000.0,
    *,
    high: Optional[float] = None,
    low: Optional[float] = None,
    close: Optional[float] = None,
    volume: float = 10.0,
) -> List[Any]:
    """One Binance-shaped kline row (12 fields)."""
    c = price if close is None else close
    h = max(price, c) * 1.001 if high is None else high
    low_ = min(price, c) * 0.999 if low is None else low
    return [int(ts), str(price), str(h), str(low_), str(c), str(volume),
            0, "0", 0, "0", "0", "0"]


def klines_body(rows: Sequence[Sequence[Any]]) -> str:
    return json.dumps([list(row) for row in rows])


class FakeTransport:
    """Scripted transport: returns queued responses, raises queued exceptions.

    When the script is exhausted it returns an empty kline page (end of data),
    so pagination loops terminate instead of spinning.
    """

    def __init__(self, script: Optional[Sequence[Any]] = None, *, default: Any = None) -> None:
        self.script: List[Any] = list(script or [])
        self.default = default if default is not None else HttpResponse(200, "[]")
        self.calls: List[Tuple[str, Dict[str, Any], float]] = []

    def get(self, url: str, params: Any, timeout: float) -> HttpResponse:
        self.calls.append((url, dict(params), timeout))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return self.default

    @property
    def call_count(self) -> int:
        return len(self.calls)


def timeout() -> FeedTimeout:
    return FeedTimeout("simulated timeout")


# --------------------------------------------------------------------------- #
# config fixtures
# --------------------------------------------------------------------------- #
def base_config(**overrides: Any) -> Config:
    """Default config loaded from the shipped ``config.yaml`` (no env influence)."""
    config = load_config(environ={})
    if overrides:
        config = dataclasses.replace(config, **overrides)
    return config


def tmp_config(tmp_dir: Path, **overrides: Any) -> Config:
    """Config whose cache/log/db paths live inside ``tmp_dir``."""
    tmp_dir = Path(tmp_dir)
    cache_dir = tmp_dir / "cache"
    logs_dir = tmp_dir / "logs"
    data_dir = tmp_dir / "data"
    for path in (cache_dir, logs_dir, data_dir):
        path.mkdir(parents=True, exist_ok=True)
    reports_dir = tmp_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    config = base_config()
    config = dataclasses.replace(
        config,
        data=dataclasses.replace(config.data, cache_dir=cache_dir, db_path=data_dir / "ledger.sqlite"),
        logging=dataclasses.replace(config.logging, dir=logs_dir),
        reports_dir=reports_dir,
    )
    if overrides:
        config = dataclasses.replace(config, **overrides)
    return config


def seed_cache(config: Config, pair: str, frame: pd.DataFrame) -> Path:
    from cryptobot.data.feed import cache_path

    path = cache_path(config.data.cache_dir, pair, config.timeframe)
    save_cache(frame, path)
    return path


def assert_frame_columns(frame: pd.DataFrame) -> None:
    assert tuple(frame.columns) == CACHE_COLUMNS, frame.columns


def sleep_recorder() -> Tuple[List[float], Any]:
    """Return ``(sleeps, sleep_fn)`` so backoff can be asserted without waiting."""
    sleeps: List[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return sleeps, sleep


__all__ = [
    "make_frame", "frame_with_entry", "kline_row", "klines_body", "FakeTransport", "timeout",
    "base_config", "tmp_config", "seed_cache", "sleep_recorder", "assert_frame_columns",
    "HOUR_MS", "START_TS",
]
