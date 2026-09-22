"""Configuration loading, override precedence and validation.

Precedence (lowest to highest)::

    built-in defaults  <  config.yaml  <  CRYPTOBOT_* environment  <  CLI flags

The mode is validated by :func:`cryptobot.safety.assert_allowed_mode` on every
load, so no config file, env var or flag combination can select live trading.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import yaml

from .safety import ALLOWED_MODES, BACKTEST_MODE, PAPER_MODE, assert_allowed_mode

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

PAIR_RE = re.compile(r"^[A-Z0-9]{2,15}/[A-Z0-9]{2,10}$")
ALLOWED_TIMEFRAMES: Tuple[str, ...] = (
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d",
)
#: Bars per year, used for annualising Sharpe.
TIMEFRAME_BARS_PER_YEAR: Dict[str, float] = {
    "1m": 525_600.0, "3m": 175_200.0, "5m": 105_120.0, "15m": 35_040.0,
    "30m": 17_520.0, "1h": 8_760.0, "2h": 4_380.0, "4h": 2_190.0,
    "6h": 1_460.0, "8h": 1_095.0, "12h": 730.0, "1d": 365.0,
}

DEFAULTS: Dict[str, Any] = {
    "mode": PAPER_MODE,
    "initial_capital_usdt": 50.0,
    "net_profit_target_pct": 2.0,
    "gross_take_profit_pct": None,
    "stop_loss_pct": 2.5,
    "max_position_pct": 90.0,
    "max_open_positions": 1,
    "daily_loss_limit_pct": 5.0,
    "cooldown_minutes": 60.0,
    "min_equity_usdt": 10.0,
    "max_trades_per_day": 8,
    "pairs": ["BTC/USDT", "ETH/USDT"],
    "timeframe": "1h",
    "fee_pct": 0.1,
    "slippage_pct": 0.05,
    "reports_dir": "reports",
    "strategy": {
        "name": "mean_reversion",
        "params": {
            "bb_period": 20,
            "bb_std": 2.0,
            "rsi_period": 14,
            "rsi_oversold": 35,
            "trend_sma_period": 200,
            "exit_at_middle_band": True,
        },
    },
    "data": {
        "api_base": "https://api.binance.com",
        "history_days": 180,
        "request_timeout_seconds": 20,
        "max_retries": 5,
        "backoff_initial_seconds": 1.0,
        "backoff_max_seconds": 30.0,
        "cache_dir": "data/cache",
        "db_path": "data/ledger.sqlite",
        #: Real-time paper mode only: how many bars the newest cached candle may
        #: lag behind the wall clock before the cache is treated as stale
        #: (complete=False) and the runner refuses to open new positions.
        "max_cache_age_bars": 3,
    },
    "logging": {"level": "INFO", "dir": "logs"},
    #: Notification layer (mobile alerts).  Operational only: it is deliberately
    #: NOT part of ``Config.as_dict()``, so adding it cannot move the backtest
    #: determinism hash.  See ``cryptobot/notify/`` and ``NOTIFICATIONS.md``.
    "notifications": {
        "enabled": True,
        # console + file need nothing; ntfy becomes active as soon as a topic is
        # set (env CRYPTOBOT_NTFY_TOPIC).  telegram/webhook must be added here.
        "providers": ["console", "file", "ntfy"],
        "ntfy_host": "ntfy.sh",
        "ntfy_topic": None,
        "min_severity": "info",
        "dedupe_window_seconds": 300,
        "max_per_hour": 20,
        "quiet_hours": None,
        "dry_run": False,
        "timeout_seconds": 10.0,
        "retry_max": 2,
        "backoff_initial_seconds": 1.0,
        "backoff_max_seconds": 8.0,
        "equity_drop_pct": 3.0,
        # Trade-only shipping default: the user asked for a push when a position
        # OPENS and when it CLOSES -- nothing else.  Every other event type stays
        # implemented and previewable (`notify preview --all`); re-enable one by
        # adding its name here (or CRYPTOBOT_NOTIFY_ON=...).  See NOTIFICATIONS.md.
        "notify_on": [
            "position_opened", "position_closed",
        ],
    },
}

#: env var -> (dotted config key, caster)
ENV_OVERRIDES: Dict[str, Tuple[str, str]] = {
    "CRYPTOBOT_MODE": ("mode", "str"),
    "CRYPTOBOT_INITIAL_CAPITAL_USDT": ("initial_capital_usdt", "float"),
    "CRYPTOBOT_NET_PROFIT_TARGET_PCT": ("net_profit_target_pct", "float"),
    "CRYPTOBOT_GROSS_TAKE_PROFIT_PCT": ("gross_take_profit_pct", "opt_float"),
    "CRYPTOBOT_STOP_LOSS_PCT": ("stop_loss_pct", "float"),
    "CRYPTOBOT_MAX_POSITION_PCT": ("max_position_pct", "float"),
    "CRYPTOBOT_MAX_OPEN_POSITIONS": ("max_open_positions", "int"),
    "CRYPTOBOT_DAILY_LOSS_LIMIT_PCT": ("daily_loss_limit_pct", "float"),
    "CRYPTOBOT_COOLDOWN_MINUTES": ("cooldown_minutes", "float"),
    "CRYPTOBOT_MIN_EQUITY_USDT": ("min_equity_usdt", "float"),
    "CRYPTOBOT_MAX_TRADES_PER_DAY": ("max_trades_per_day", "int"),
    "CRYPTOBOT_PAIRS": ("pairs", "list"),
    "CRYPTOBOT_TIMEFRAME": ("timeframe", "str"),
    "CRYPTOBOT_FEE_PCT": ("fee_pct", "float"),
    "CRYPTOBOT_SLIPPAGE_PCT": ("slippage_pct", "float"),
    "CRYPTOBOT_REPORTS_DIR": ("reports_dir", "str"),
    "CRYPTOBOT_CACHE_DIR": ("data.cache_dir", "str"),
    "CRYPTOBOT_DB_PATH": ("data.db_path", "str"),
    "CRYPTOBOT_HISTORY_DAYS": ("data.history_days", "int"),
    "CRYPTOBOT_MAX_CACHE_AGE_BARS": ("data.max_cache_age_bars", "int"),
    "CRYPTOBOT_LOG_LEVEL": ("logging.level", "str"),
    "CRYPTOBOT_API_BASE": ("data.api_base", "str"),
    # --- notifications (non-secret knobs; secrets are read by cryptobot.notify) --
    "CRYPTOBOT_NOTIFY_ENABLED": ("notifications.enabled", "bool"),
    "CRYPTOBOT_NOTIFY_DRY_RUN": ("notifications.dry_run", "bool"),
    "CRYPTOBOT_NOTIFY_PROVIDERS": ("notifications.providers", "csv"),
    "CRYPTOBOT_NOTIFY_ON": ("notifications.notify_on", "csv"),
    "CRYPTOBOT_NOTIFY_MIN_SEVERITY": ("notifications.min_severity", "str"),
    "CRYPTOBOT_NOTIFY_DEDUPE_SECONDS": ("notifications.dedupe_window_seconds", "int"),
    "CRYPTOBOT_NOTIFY_MAX_PER_HOUR": ("notifications.max_per_hour", "int"),
    "CRYPTOBOT_NOTIFY_QUIET_HOURS": ("notifications.quiet_hours", "opt_str"),
    "CRYPTOBOT_NOTIFY_TIMEOUT_SECONDS": ("notifications.timeout_seconds", "float"),
    "CRYPTOBOT_NOTIFY_RETRY_MAX": ("notifications.retry_max", "int"),
    "CRYPTOBOT_NTFY_HOST": ("notifications.ntfy_host", "str"),
    "CRYPTOBOT_NTFY_TOPIC": ("notifications.ntfy_topic", "opt_str"),
}

#: Allowed notification providers (``cryptobot/notify/providers.py``).
ALLOWED_NOTIFY_PROVIDERS: Tuple[str, ...] = ("ntfy", "telegram", "webhook", "console", "file")

#: Severity ladder (low -> high); ``min_severity`` filters below the threshold.
ALLOWED_SEVERITIES: Tuple[str, ...] = ("info", "warning", "critical")

#: Values accepted as the boolean-ish ``live`` kill-switch (never enabled).
_LIVE_FLAG_ENV = ("CRYPTOBOT_LIVE", "CRYPTOBOT_REAL", "LIVE_TRADING")


class ConfigError(ValueError):
    """Raised when the configuration is missing, ill-typed or out of range."""


def _deep_merge(base: Dict[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(dict(out[key]), value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _cast(value: Any, kind: str, key: str) -> Any:
    try:
        if kind == "str":
            return str(value).strip()
        if kind == "float":
            return float(value)
        if kind == "opt_float":
            return None if value in (None, "", "null") else float(value)
        if kind == "opt_str":
            text = str(value).strip()
            return None if text in ("", "null", "None") else text
        if kind == "int":
            return int(float(value))
        if kind == "bool":
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off", ""}:
                return False
            raise ConfigError("invalid boolean for {}: {!r}".format(key, value))
        if kind == "csv":  # comma list, lower-cased and de-duplicated (order kept)
            items = value.split(",") if isinstance(value, str) else list(value)
            out: list = []
            for item in items:
                text = str(item).strip().lower()
                if text and text not in out:
                    out.append(text)
            return out
        if kind == "list":
            if isinstance(value, str):
                return [p.strip().upper() for p in value.split(",") if p.strip()]
            return [str(p).strip().upper() for p in value]
    except (TypeError, ValueError) as exc:
        raise ConfigError("invalid value for {}: {!r} ({})".format(key, value, exc)) from exc
    raise ConfigError("unknown caster {!r} for {}".format(kind, key))


def _set_dotted(cfg: Dict[str, Any], dotted: str, value: Any) -> None:
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _get_dotted(cfg: Mapping[str, Any], dotted: str) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _parse_hhmm(value: Any) -> Optional[int]:
    """``"22:00"`` -> ``1320`` minutes past local midnight, or ``None`` if invalid."""
    if value is None:
        return None
    text = str(value).strip()
    if ":" not in text:
        return None
    hour, _, minute = text.partition(":")
    try:
        h, m = int(hour), int(minute)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _parse_quiet_hours(value: Any) -> Optional[Tuple[int, int]]:
    """Accept ``null``, ``"22:00-07:00"`` or ``{start: ..., end: ...}``.

    Returns ``(start_minute, end_minute)``; a window may wrap past midnight.
    Raises :class:`ConfigError` for a non-empty but unparseable value.
    """
    if value is None or value == "" or value == "null":
        return None
    if isinstance(value, Mapping):
        start_raw, end_raw = value.get("start"), value.get("end")
    elif isinstance(value, str):
        if "-" not in value:
            raise ConfigError(
                "notifications.quiet_hours must look like '22:00-07:00' (or null), got {!r}".format(value)
            )
        start_raw, end_raw = value.split("-", 1)
    else:
        raise ConfigError("notifications.quiet_hours must be a string or a mapping, got {!r}".format(value))
    start, end = _parse_hhmm(start_raw), _parse_hhmm(end_raw)
    if start is None or end is None:
        raise ConfigError(
            "notifications.quiet_hours needs HH:MM start/end, got {!r}-{!r}".format(start_raw, end_raw)
        )
    return (start, end)


def apply_env_overrides(cfg: Dict[str, Any], environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Apply ``CRYPTOBOT_*`` environment overrides (pure, testable)."""
    env = os.environ if environ is None else environ
    out = copy.deepcopy(cfg)
    for env_name, (dotted, kind) in ENV_OVERRIDES.items():
        raw = env.get(env_name)
        if raw is None or raw == "":
            continue
        _set_dotted(out, dotted, _cast(raw, kind, env_name))
    return out


def assert_no_live_flags(environ: Optional[Mapping[str, str]] = None) -> None:
    """Reject truthy ``*_LIVE`` / ``*_REAL`` env kill-switches outright."""
    from .safety import assert_paper_only

    env = os.environ if environ is None else environ
    for name in _LIVE_FLAG_ENV:
        assert_paper_only(name, value=str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"})


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataConfig:
    api_base: str = "https://api.binance.com"
    history_days: int = 180
    request_timeout_seconds: float = 20.0
    max_retries: int = 5
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    cache_dir: Path = PROJECT_ROOT / "data" / "cache"
    db_path: Path = PROJECT_ROOT / "data" / "ledger.sqlite"
    #: Real-time paper mode: max bar-age of the newest cached candle before the
    #: cache is declared stale.  Ignored by backtest/replay (old candles are the point).
    max_cache_age_bars: int = 3


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    dir: Path = PROJECT_ROOT / "logs"


@dataclass(frozen=True)
class NotificationsConfig:
    """Mobile notification settings (operational; never part of the metric echo).

    Secrets (topic, bearer token, telegram credentials, webhook URL) are **not**
    stored here: they are read from the environment by ``cryptobot.notify`` and
    are never written to ``config.yaml`` nor logged.
    """

    enabled: bool = True
    providers: Tuple[str, ...] = ("console", "file", "ntfy")
    ntfy_host: str = "ntfy.sh"
    ntfy_topic: Optional[str] = None
    min_severity: str = "info"
    dedupe_window_seconds: int = 300
    max_per_hour: int = 20
    #: ``(start_minute, end_minute)`` in local time, or ``None`` when disabled.
    quiet_hours: Optional[Tuple[int, int]] = None
    dry_run: bool = False
    timeout_seconds: float = 10.0
    retry_max: int = 2
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 8.0
    equity_drop_pct: float = 3.0
    notify_on: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """Validated, immutable runtime configuration."""

    mode: str
    initial_capital_usdt: float
    net_profit_target_pct: float
    gross_take_profit_pct: Optional[float]
    stop_loss_pct: float
    max_position_pct: float
    max_open_positions: int
    daily_loss_limit_pct: float
    cooldown_minutes: float
    min_equity_usdt: float
    max_trades_per_day: int
    pairs: Tuple[str, ...]
    timeframe: str
    fee_pct: float
    slippage_pct: float
    strategy: StrategyConfig
    data: DataConfig
    logging: LoggingConfig
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    reports_dir: Path = PROJECT_ROOT / "reports"
    source_path: Optional[Path] = None
    overrides: Tuple[str, ...] = ()

    # ------------------------------------------------------------------ paths
    @property
    def cache_dir(self) -> Path:
        return self.data.cache_dir

    @property
    def logs_dir(self) -> Path:
        return self.logging.dir

    @property
    def db_path(self) -> Path:
        return self.data.db_path

    @property
    def bars_per_year(self) -> float:
        return TIMEFRAME_BARS_PER_YEAR.get(self.timeframe, 8_760.0)

    def pair_slug(self) -> str:
        """Filename-safe slug for the configured pairs, e.g. ``BTCUSDT-ETHUSDT``."""
        return "-".join(p.replace("/", "") for p in self.pairs)

    def as_dict(self) -> Dict[str, Any]:
        """Deterministic dict echo (used inside the reproducibility payload)."""
        return {
            "mode": self.mode,
            "initial_capital_usdt": self.initial_capital_usdt,
            "net_profit_target_pct": self.net_profit_target_pct,
            "gross_take_profit_pct": self.gross_take_profit_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "max_position_pct": self.max_position_pct,
            "max_open_positions": self.max_open_positions,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "cooldown_minutes": self.cooldown_minutes,
            "min_equity_usdt": self.min_equity_usdt,
            "max_trades_per_day": self.max_trades_per_day,
            "pairs": list(self.pairs),
            "timeframe": self.timeframe,
            "fee_pct": self.fee_pct,
            "slippage_pct": self.slippage_pct,
            "strategy": {"name": self.strategy.name, "params": dict(sorted(self.strategy.params.items()))},
        }


def validate(raw: Mapping[str, Any], *, source: str = "config.yaml") -> Config:
    """Validate a raw mapping and build a :class:`Config`."""
    errors = []

    def num(key: str) -> float:
        try:
            return float(raw[key])
        except (KeyError, TypeError, ValueError):
            errors.append("{}: {} must be a number, got {!r}".format(source, key, raw.get(key)))
            return float("nan")

    mode = assert_allowed_mode(raw.get("mode", PAPER_MODE), source=source)

    initial_capital = num("initial_capital_usdt")
    net_target = num("net_profit_target_pct")
    stop_loss = num("stop_loss_pct")
    max_position = num("max_position_pct")
    daily_loss = num("daily_loss_limit_pct")
    cooldown = num("cooldown_minutes")
    min_equity = num("min_equity_usdt")
    fee = num("fee_pct")
    slippage = num("slippage_pct")

    gross_override = raw.get("gross_take_profit_pct")
    if gross_override not in (None, "", "null"):
        try:
            gross_override = float(gross_override)
            if gross_override <= 0:
                errors.append("gross_take_profit_pct must be > 0 when set")
        except (TypeError, ValueError):
            errors.append("gross_take_profit_pct must be a number or null")
            gross_override = None
    else:
        gross_override = None

    try:
        max_open = int(raw["max_open_positions"])
    except (KeyError, TypeError, ValueError):
        errors.append("max_open_positions must be an integer, got {!r}".format(raw.get("max_open_positions")))
        max_open = 1
    try:
        max_trades = int(raw["max_trades_per_day"])
    except (KeyError, TypeError, ValueError):
        errors.append("max_trades_per_day must be an integer, got {!r}".format(raw.get("max_trades_per_day")))
        max_trades = 8

    pairs_raw = raw.get("pairs") or []
    if isinstance(pairs_raw, str):
        pairs_raw = [p.strip() for p in pairs_raw.split(",") if p.strip()]
    pairs = tuple(str(p).strip().upper() for p in pairs_raw)
    if not pairs:
        errors.append("pairs must contain at least one market, e.g. BTC/USDT")
    for pair in pairs:
        if not PAIR_RE.match(pair):
            errors.append("invalid pair {!r}; expected e.g. BTC/USDT".format(pair))

    timeframe = str(raw.get("timeframe", "1h")).strip()
    if timeframe not in ALLOWED_TIMEFRAMES:
        errors.append("timeframe {!r} not supported; choose one of {}".format(timeframe, ", ".join(ALLOWED_TIMEFRAMES)))

    if not 0 < initial_capital <= 1_000_000:
        errors.append("initial_capital_usdt must be in (0, 1e6], got {}".format(initial_capital))
    if not 0 < net_target <= 50:
        errors.append("net_profit_target_pct must be in (0, 50], got {}".format(net_target))
    if not 0 < stop_loss <= 50:
        errors.append("stop_loss_pct must be in (0, 50], got {}".format(stop_loss))
    if not 0 < max_position <= 100:
        errors.append("max_position_pct must be in (0, 100], got {}".format(max_position))
    if not 0 <= daily_loss <= 100:
        errors.append("daily_loss_limit_pct must be in [0, 100], got {}".format(daily_loss))
    if cooldown < 0:
        errors.append("cooldown_minutes must be >= 0, got {}".format(cooldown))
    if min_equity < 0:
        errors.append("min_equity_usdt must be >= 0, got {}".format(min_equity))
    if not 0 <= fee < 5:
        errors.append("fee_pct must be in [0, 5), got {}".format(fee))
    if not 0 <= slippage < 5:
        errors.append("slippage_pct must be in [0, 5), got {}".format(slippage))
    if max_open < 1:
        errors.append("max_open_positions must be >= 1, got {}".format(max_open))
    if max_trades < 1:
        errors.append("max_trades_per_day must be >= 1, got {}".format(max_trades))

    strat_raw = raw.get("strategy") or {}
    if not isinstance(strat_raw, Mapping) or not strat_raw.get("name"):
        errors.append("strategy.name is required")
        strat_raw = {"name": "mean_reversion", "params": {}}
    params = dict(strat_raw.get("params") or {})

    data_raw = dict(raw.get("data") or {})
    log_raw = dict(raw.get("logging") or {})
    if log_raw.get("level", "INFO").upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        errors.append("logging.level must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL")

    try:
        max_cache_age_bars = int(data_raw.get("max_cache_age_bars", DEFAULTS["data"]["max_cache_age_bars"]))
    except (TypeError, ValueError):
        errors.append("data.max_cache_age_bars must be an integer, got {!r}".format(
            data_raw.get("max_cache_age_bars")))
        max_cache_age_bars = int(DEFAULTS["data"]["max_cache_age_bars"])
    if max_cache_age_bars < 1:
        errors.append("data.max_cache_age_bars must be >= 1, got {}".format(max_cache_age_bars))

    # --- notifications --------------------------------------------------------
    # Re-merge with the defaults so a partial overlay (e.g. a CLI flag) keeps the
    # documented defaults for every key it does not name.
    notif_raw = _deep_merge(DEFAULTS["notifications"], dict(raw.get("notifications") or {}))

    providers_raw = notif_raw.get("providers") or []
    if isinstance(providers_raw, str):
        providers_raw = [p.strip() for p in providers_raw.split(",") if p.strip()]
    providers = tuple(dict.fromkeys(str(p).strip().lower() for p in providers_raw if str(p).strip()))
    if not providers:
        errors.append("notifications.providers must list at least one provider (e.g. console, file)")
    for name in providers:
        if name not in ALLOWED_NOTIFY_PROVIDERS:
            errors.append("notifications.providers: unknown provider {!r}; choose from {}".format(
                name, ", ".join(ALLOWED_NOTIFY_PROVIDERS)))

    min_severity = str(notif_raw.get("min_severity", "info")).strip().lower()
    if min_severity not in ALLOWED_SEVERITIES:
        errors.append("notifications.min_severity must be one of {}, got {!r}".format(
            "/".join(ALLOWED_SEVERITIES), min_severity))

    notify_on_raw = notif_raw.get("notify_on") or []
    if isinstance(notify_on_raw, str):
        notify_on_raw = [e.strip() for e in notify_on_raw.split(",") if e.strip()]
    notify_on = tuple(dict.fromkeys(str(e).strip().lower() for e in notify_on_raw if str(e).strip()))
    if not notify_on:
        errors.append("notifications.notify_on must list at least one event type")

    try:
        dedupe_window = int(notif_raw.get("dedupe_window_seconds", 300))
    except (TypeError, ValueError):
        errors.append("notifications.dedupe_window_seconds must be an integer")
        dedupe_window = 300
    try:
        max_per_hour = int(notif_raw.get("max_per_hour", 20))
    except (TypeError, ValueError):
        errors.append("notifications.max_per_hour must be an integer")
        max_per_hour = 20
    try:
        retry_max = int(notif_raw.get("retry_max", 2))
    except (TypeError, ValueError):
        errors.append("notifications.retry_max must be an integer")
        retry_max = 2
    try:
        timeout_seconds = float(notif_raw.get("timeout_seconds", 10.0))
    except (TypeError, ValueError):
        errors.append("notifications.timeout_seconds must be a number")
        timeout_seconds = 10.0
    try:
        backoff_initial = float(notif_raw.get("backoff_initial_seconds", 1.0))
        backoff_max = float(notif_raw.get("backoff_max_seconds", 8.0))
    except (TypeError, ValueError):
        errors.append("notifications.backoff_initial_seconds/backoff_max_seconds must be numbers")
        backoff_initial, backoff_max = 1.0, 8.0
    try:
        equity_drop_pct = float(notif_raw.get("equity_drop_pct", 3.0))
    except (TypeError, ValueError):
        errors.append("notifications.equity_drop_pct must be a number")
        equity_drop_pct = 3.0

    if dedupe_window < 0:
        errors.append("notifications.dedupe_window_seconds must be >= 0, got {}".format(dedupe_window))
    if max_per_hour < 1:
        errors.append("notifications.max_per_hour must be >= 1, got {}".format(max_per_hour))
    if not 0 <= retry_max <= 10:
        errors.append("notifications.retry_max must be in [0, 10], got {}".format(retry_max))
    if timeout_seconds <= 0:
        errors.append("notifications.timeout_seconds must be > 0, got {}".format(timeout_seconds))
    if backoff_initial < 0 or backoff_max < 0:
        errors.append("notifications backoff values must be >= 0")
    if not 0 <= equity_drop_pct <= 100:
        errors.append("notifications.equity_drop_pct must be in [0, 100], got {}".format(equity_drop_pct))

    try:
        quiet_hours = _parse_quiet_hours(notif_raw.get("quiet_hours"))
    except ConfigError as exc:
        errors.append(str(exc))
        quiet_hours = None

    ntfy_topic = notif_raw.get("ntfy_topic")
    ntfy_topic = None if ntfy_topic in (None, "", "null") else str(ntfy_topic).strip()
    ntfy_host = str(notif_raw.get("ntfy_host", "ntfy.sh")).strip() or "ntfy.sh"

    if errors:
        raise ConfigError("invalid configuration ({}):\n  - {}".format(source, "\n  - ".join(errors)))

    cache_dir = Path(data_raw.get("cache_dir", DEFAULTS["data"]["cache_dir"]))
    if not cache_dir.is_absolute():
        cache_dir = PROJECT_ROOT / cache_dir
    db_path = Path(data_raw.get("db_path", DEFAULTS["data"]["db_path"]))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    logs_dir = Path(log_raw.get("dir", DEFAULTS["logging"]["dir"]))
    if not logs_dir.is_absolute():
        logs_dir = PROJECT_ROOT / logs_dir
    reports_dir = Path(raw.get("reports_dir", DEFAULTS["reports_dir"]))
    if not reports_dir.is_absolute():
        reports_dir = PROJECT_ROOT / reports_dir

    return Config(
        mode=mode,
        initial_capital_usdt=initial_capital,
        net_profit_target_pct=net_target,
        gross_take_profit_pct=gross_override,
        stop_loss_pct=stop_loss,
        max_position_pct=max_position,
        max_open_positions=max_open,
        daily_loss_limit_pct=daily_loss,
        cooldown_minutes=cooldown,
        min_equity_usdt=min_equity,
        max_trades_per_day=max_trades,
        pairs=pairs,
        timeframe=timeframe,
        fee_pct=fee,
        slippage_pct=slippage,
        strategy=StrategyConfig(name=str(strat_raw["name"]), params=params),
        data=DataConfig(
            api_base=str(data_raw.get("api_base", DEFAULTS["data"]["api_base"])).rstrip("/"),
            history_days=int(data_raw.get("history_days", 180)),
            request_timeout_seconds=float(data_raw.get("request_timeout_seconds", 20)),
            max_retries=int(data_raw.get("max_retries", 5)),
            backoff_initial_seconds=float(data_raw.get("backoff_initial_seconds", 1.0)),
            backoff_max_seconds=float(data_raw.get("backoff_max_seconds", 30.0)),
            cache_dir=cache_dir,
            db_path=db_path,
            max_cache_age_bars=max_cache_age_bars,
        ),
        logging=LoggingConfig(level=log_raw.get("level", "INFO").upper(), dir=logs_dir),
        notifications=NotificationsConfig(
            enabled=bool(notif_raw.get("enabled", True)),
            providers=providers,
            ntfy_host=ntfy_host,
            ntfy_topic=ntfy_topic,
            min_severity=min_severity,
            dedupe_window_seconds=dedupe_window,
            max_per_hour=max_per_hour,
            quiet_hours=quiet_hours,
            dry_run=bool(notif_raw.get("dry_run", False)),
            timeout_seconds=timeout_seconds,
            retry_max=retry_max,
            backoff_initial_seconds=backoff_initial,
            backoff_max_seconds=backoff_max,
            equity_drop_pct=equity_drop_pct,
            notify_on=notify_on,
        ),
        reports_dir=reports_dir,
        source_path=DEFAULT_CONFIG_PATH,
    )


def load_config(
    path: Optional[os.PathLike | str] = None,
    *,
    cli_overrides: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Config:
    """Load, merge, validate and return the effective configuration."""
    assert_no_live_flags(environ)
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: Dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError("could not parse {}: {}".format(config_path, exc)) from exc
        if not isinstance(loaded, Mapping):
            raise ConfigError("{} must contain a YAML mapping at the top level".format(config_path))
        raw = dict(loaded)
    elif path is not None:
        raise ConfigError("config file not found: {}".format(config_path))

    merged = _deep_merge(DEFAULTS, raw)
    merged = apply_env_overrides(merged, environ)

    applied: list[str] = []
    for key, value in (cli_overrides or {}).items():
        if value is None:
            continue
        merged[key] = value
        applied.append(key)

    cfg = validate(merged, source=str(config_path))
    if applied:
        cfg = Config(**{**cfg.__dict__, "overrides": tuple(sorted(applied))})
    return cfg


SENSITIVE_KEYS: Sequence[str] = ("api_key", "secret", "password", "token", "private")


def assert_no_secrets_in_config(path: Optional[Path] = None) -> None:
    """Fail if a user pastes credentials into ``config.yaml``.

    The bot never needs them; finding them is treated as a misconfiguration
    because it implies the user intends to trade real money.
    """
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    if not target.exists():
        return
    text = target.read_text(encoding="utf-8").lower()
    for key in SENSITIVE_KEYS:
        if key in text:
            raise ConfigError(
                "{} appears to contain a secret ('{}'). This bot never needs API keys; "
                "remove it. Live trading is not implemented.".format(target.name, key)
            )


__all__ = [
    "Config",
    "ConfigError",
    "StrategyConfig",
    "DataConfig",
    "LoggingConfig",
    "NotificationsConfig",
    "DEFAULTS",
    "PROJECT_ROOT",
    "DEFAULT_CONFIG_PATH",
    "ALLOWED_TIMEFRAMES",
    "ALLOWED_NOTIFY_PROVIDERS",
    "ALLOWED_SEVERITIES",
    "TIMEFRAME_BARS_PER_YEAR",
    "load_config",
    "validate",
    "apply_env_overrides",
    "assert_no_live_flags",
    "assert_no_secrets_in_config",
]
