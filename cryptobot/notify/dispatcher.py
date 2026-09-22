"""The dispatcher: filters, bounded retry, audit, and total failure isolation.

This is the only entry point the trading loop uses (``Notifier.notify``).  Its
contract is simple and absolute:

* it **never raises** -- a broken provider, a DNS failure, a full disk, a bug in
  a filter, all of it is caught and recorded;
* it **never changes a trading decision** -- it only reads the event it is given;
* it **never hangs the loop** -- every attempt has a timeout and the retry count
  is hard-capped, with exponential backoff;
* every attempt (sent / failed / suppressed / dry_run) is appended to the audit
  store with the reason.

Filter order: enabled -> event type -> min severity -> quiet hours -> dedupe ->
hourly rate limit (**criticals bypass the rate limit**; they are still deduped
and still audited).

The hourly rate limit is a **network push** budget: only successful sends by a
provider with ``requires_network = True`` consume it.  The local ``console`` /
``file`` providers mirror every message for free and are never counted -- neither
while running nor when the budget is re-seeded from the audit file -- so local
mirroring can never mute a real notification.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Tuple

from .events import (
    SEVERITIES,
    SEVERITY_CRITICAL,
    SUPERSEDED_BY,
    NotificationEvent,
    severity_at_least,
    test_event,
)
from .guard import (
    REASON_HARNESS,
    REASON_OFFLINE,
    REASON_REPLAY,
    REASON_SUPERSEDED,
    harness_mode,
    host_is_local,
    network_guard_reason,
    target_host,
)
from .providers import Provider, SendResult, build_providers
from .redact import redact, redact_mapping
from .store import RECORD_FIELDS, NotificationStore

log = logging.getLogger("cryptobot.notify")

#: Hard ceiling on attempts per provider (1 initial + retries), regardless of config.
MAX_ATTEMPTS = 10
_RATE_WINDOW_SECONDS = 3600.0


def _iso_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class NotifyConfig:
    """Runtime notification settings: config.yaml values + environment secrets.

    Secrets live here transiently but are only ever written through
    :func:`cryptobot.notify.redact.redact`; they are never persisted or logged.
    """

    enabled: bool = True
    providers: Tuple[str, ...] = ("console", "file")
    ntfy_host: str = "ntfy.sh"
    ntfy_topic: Optional[str] = None
    ntfy_token: Optional[str] = None
    ntfy_tags: Optional[str] = None
    ntfy_click: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_api_base: str = "https://api.telegram.org"
    webhook_url: Optional[str] = None
    min_severity: str = "info"
    dedupe_window_seconds: int = 300
    max_per_hour: int = 20
    quiet_hours: Optional[Tuple[int, int]] = None
    dry_run: bool = False
    timeout_seconds: float = 10.0
    retry_max: int = 2
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 8.0
    equity_drop_pct: float = 3.0
    notify_on: Tuple[str, ...] = ()
    store_path: Path = Path("logs") / "notifications.jsonl"
    #: Replay/offline runs walk historical bars, so a network provider must not
    #: push by default: ``run --replay`` / ``run --offline`` set this to False
    #: unless the operator passed ``--notify-send``.  Real paper mode is True.
    network_send_allowed: bool = True
    #: Audit reason recorded when :attr:`network_send_allowed` is False.
    network_block_reason: str = REASON_REPLAY

    # ------------------------------------------------------------------ build
    @classmethod
    def from_config(cls, config: Any, *, environ: Optional[Mapping[str, str]] = None,
                    network_send_allowed: bool = True,
                    network_block_reason: str = REASON_REPLAY) -> "NotifyConfig":
        """Build from a :class:`cryptobot.config.Config` plus environment secrets."""
        env = os.environ if environ is None else environ
        n = config.notifications

        def env_or(name: str, fallback: Optional[str]) -> Optional[str]:
            value = env.get(name)
            if value is None or str(value).strip() == "":
                return fallback
            return str(value).strip()

        store_path = Path(config.logs_dir) / "notifications.jsonl"
        telegram_base = env_or("CRYPTOBOT_TELEGRAM_API_BASE", "https://api.telegram.org")
        return cls(
            enabled=bool(n.enabled),
            providers=tuple(n.providers),
            ntfy_host=env_or("CRYPTOBOT_NTFY_HOST", n.ntfy_host) or "ntfy.sh",
            ntfy_topic=env_or("CRYPTOBOT_NTFY_TOPIC", n.ntfy_topic),
            ntfy_token=env_or("CRYPTOBOT_NTFY_TOKEN", None),
            ntfy_tags=env_or("CRYPTOBOT_NTFY_TAGS", None),
            ntfy_click=env_or("CRYPTOBOT_NTFY_CLICK", None),
            telegram_bot_token=env_or("CRYPTOBOT_TELEGRAM_BOT_TOKEN", None),
            telegram_chat_id=env_or("CRYPTOBOT_TELEGRAM_CHAT_ID", None),
            telegram_api_base=telegram_base,
            webhook_url=env_or("CRYPTOBOT_WEBHOOK_URL", None),
            min_severity=str(n.min_severity),
            dedupe_window_seconds=int(n.dedupe_window_seconds),
            max_per_hour=int(n.max_per_hour),
            quiet_hours=tuple(n.quiet_hours) if n.quiet_hours else None,
            dry_run=bool(n.dry_run),
            timeout_seconds=float(n.timeout_seconds),
            retry_max=int(n.retry_max),
            backoff_initial_seconds=float(n.backoff_initial_seconds),
            backoff_max_seconds=float(n.backoff_max_seconds),
            equity_drop_pct=float(n.equity_drop_pct),
            notify_on=tuple(n.notify_on),
            store_path=store_path,
            network_send_allowed=bool(network_send_allowed),
            network_block_reason=str(network_block_reason or REASON_REPLAY),
        )

    def secret_values(self) -> Tuple[str, ...]:
        """Configured secret strings, for redaction."""
        return tuple(v for v in (
            self.ntfy_token, self.ntfy_topic, self.telegram_bot_token,
            self.telegram_chat_id, self.webhook_url,
        ) if v)

    def changed(self, **overrides: Any) -> "NotifyConfig":
        return replace(self, **overrides)


class Notifier:
    """Failure-isolated fan-out to the configured providers."""

    def __init__(
        self,
        cfg: NotifyConfig,
        *,
        store: Optional[NotificationStore] = None,
        providers: Optional[Iterable[Provider]] = None,
        transport: Any = None,
        writer: Any = None,
        clock: Any = time.time,
        mono: Any = time.perf_counter,
        sleep: Any = time.sleep,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.cfg = cfg
        self.log = logger or log
        self.secrets = cfg.secret_values()
        self.store = store if store is not None else NotificationStore(cfg.store_path)
        self._clock = clock
        self._mono = mono
        self._sleep = sleep
        self.providers: List[Provider] = list(providers) if providers is not None else build_providers(
            cfg, transport=transport, mono=mono, writer=writer)
        #: Hourly **network** push timestamps (local console/file mirroring is not
        #: counted -- see :meth:`_record_counts_as_network_send`).
        self._sent_times: Deque[float] = deque()
        self._last_sent: Dict[str, float] = {}
        #: Provider name -> ``requires_network``, resolved from the attribute (never
        #: from the name string) so ``_seed_state`` can classify old audit rows.
        self._provider_is_network: Dict[str, bool] = {
            str(getattr(provider, "name", "")): self._requires_network(provider)
            for provider in self.providers}
        self._seed_state()

    # ------------------------------------------------------------------ state
    @staticmethod
    def _requires_network(provider: Any) -> bool:
        """Whether ``provider`` pushes over the network (its own declared attribute).

        Attribute-based, never name-based: a network provider pointed at a
        loopback sink is still a network provider and still honours the budget.
        """
        return bool(getattr(provider, "requires_network", False))

    def _record_counts_as_network_send(self, record: Mapping[str, Any]) -> bool:
        """Whether an audit row was a network push, so it consumes the hourly budget.

        Either signal is enough:

        * the row's ``target`` is an ``http(s)`` URL on a non-loopback host, or
        * the row names a configured provider whose ``requires_network`` is True.

        A local ``console``/``file`` row (no URL target, local provider) never
        counts, so a history full of local ``sent`` rows cannot mute the phone.
        """
        target = str(record.get("target") or "").strip()
        if target.startswith(("http://", "https://")) and not host_is_local(target):
            return True
        return bool(self._provider_is_network.get(str(record.get("provider") or ""), False))

    def _seed_state(self) -> None:
        """Rebuild rate/dedupe state from the audit file so restarts keep honouring it.

        Only **network** ``sent`` rows re-seed the hourly budget; dedupe state is
        seeded from every ``sent`` row (a restart must not re-alert).
        """
        try:
            for record in self.store.read_all(limit=5000):
                if str(record.get("status")) != "sent":
                    continue
                ts = float(record.get("ts") or 0) / 1000.0
                if ts <= 0:
                    continue
                if self._record_counts_as_network_send(record):
                    self._sent_times.append(ts)
                key = str(record.get("dedupe_key") or "")
                if key:
                    self._last_sent[key] = ts
        except Exception as exc:  # pragma: no cover - defensive
            self.log.debug("notify.seed_failed", extra={"event": "notify_seed_failed",
                                                        "detail": redact(exc, secrets=self.secrets)})
        self._prune_sent_times()

    def _prune_sent_times(self) -> None:
        """Drop budget entries that fell out of the trailing hour."""
        cutoff = self._clock() - _RATE_WINDOW_SECONDS
        while self._sent_times and self._sent_times[0] < cutoff:
            self._sent_times.popleft()

    def network_budget(self) -> Dict[str, Any]:
        """The hourly **network** push budget: cap, usage, remaining and verdict.

        Only successful sends by a ``requires_network`` provider are counted, so
        the numbers answer "can a trade notification still go out right now?".
        A critical event bypasses the cap, so only routine (info/warning) trade
        events are ever blocked by it.
        """
        self._prune_sent_times()
        cap = max(1, int(self.cfg.max_per_hour))
        used = len(self._sent_times)
        return {
            "max_per_hour": cap,
            "window_seconds": int(_RATE_WINDOW_SECONDS),
            "used": used,
            "remaining": max(0, cap - used),
            "blocks_trade_notification": used >= cap,
        }

    def status(self) -> List[Dict[str, Any]]:
        """Per-provider activation state and the reason it is inactive."""
        out: List[Dict[str, Any]] = []
        for provider in self.providers:
            try:
                active, reason = provider.active()
            except Exception as exc:  # pragma: no cover - defensive
                active, reason = False, "active() failed: {}".format(type(exc).__name__)
            out.append({"provider": provider.name, "active": bool(active), "reason": reason,
                        "network": bool(getattr(provider, "requires_network", False))})
        return out

    def active_providers(self) -> List[Provider]:
        active: List[Provider] = []
        for provider in self.providers:
            try:
                if provider.active()[0]:
                    active.append(provider)
            except Exception:  # pragma: no cover - defensive
                continue
        return active

    # ------------------------------------------------------------------ guard
    def _provider_target(self, provider: Provider) -> str:
        try:
            return str(getattr(provider, "guard_target", lambda: "")() or "")
        except Exception:  # pragma: no cover - defensive
            return ""

    def _network_guard_reason(self, provider: Provider) -> str:
        """Harness/replay refusal reason for one provider, or ``""`` when allowed."""
        return network_guard_reason(
            self._provider_target(provider),
            requires_network=bool(getattr(provider, "requires_network", False)),
            network_send_allowed=bool(self.cfg.network_send_allowed),
            block_reason=str(self.cfg.network_block_reason or REASON_REPLAY),
        )

    def _superseded_reason(self, event: NotificationEvent, provider: Provider,
                           *, force: bool) -> str:
        """The single-source-of-truth rule for TP/SL detail variants.

        ``take_profit_hit`` / ``stop_loss_hit`` describe the *same* exit that
        ``position_closed`` already reports (the engine emits the close first).
        When the parent type is enabled, the variant is not pushed to a network
        provider, so one close is exactly one push.  ``force=True`` (``notify
        test`` / ``notify preview --send``) deliberately overrides this: the
        operator asked for that exact message.
        """
        if force:
            return ""
        parent = SUPERSEDED_BY.get(str(event.type))
        if not parent:
            return ""
        if not bool(getattr(provider, "requires_network", False)):
            return ""
        if parent not in self.cfg.notify_on:
            return ""
        return REASON_SUPERSEDED

    def network_targets(self) -> List[Dict[str, Any]]:
        """Every network provider with its target host, activation and guard state."""
        out: List[Dict[str, Any]] = []
        for provider in self.providers:
            if not bool(getattr(provider, "requires_network", False)):
                continue
            target = self._provider_target(provider)
            try:
                active, reason = provider.active()
            except Exception as exc:  # pragma: no cover - defensive
                active, reason = False, "active() failed: {}".format(type(exc).__name__)
            out.append({
                "provider": provider.name,
                "host": target_host(target),
                "target": target,
                "local": host_is_local(target),
                "active": bool(active),
                "inactive_reason": "" if active else reason,
                "guard": self._network_guard_reason(provider),
            })
        return out

    def can_send_to_network(self) -> Tuple[bool, str]:
        """``(possible, reason)`` for the "is a real push possible right now?" line.

        Possible means: the layer is enabled, not dry-run, at least one network
        provider is active, and the harness/replay guard does not refuse it.
        """
        if not self.cfg.enabled:
            return False, "enabled=false"
        if self.cfg.dry_run:
            return False, "dry_run=true"
        network = self.network_targets()
        if not network:
            return False, "no_network_provider_configured"
        active = [item for item in network if item["active"]]
        if not active:
            return False, "no_active_network_provider"
        allowed = [item for item in active if not item["guard"]]
        if not allowed:
            return False, str(active[0]["guard"] or "blocked")
        return True, ""

    def log_send_capability(self) -> Tuple[bool, str]:
        """Emit the one-line startup record of whether a real push is possible."""
        possible, reason = self.can_send_to_network()
        fields = {
            "event": "notify_send_capability",
            "network_send_possible": bool(possible),
            "reason": reason,
            "dry_run": bool(self.cfg.dry_run),
            "enabled": bool(self.cfg.enabled),
            "harness_guard": bool(harness_mode()),
            "network_send_allowed": bool(self.cfg.network_send_allowed),
            "targets": ["{}:{}".format(item["provider"], item["host"] or "-")
                        for item in self.network_targets()],
        }
        try:
            self.log.info("notify.send_capability", extra=fields)
        except Exception:  # pragma: no cover - logging must not break the loop
            pass
        return possible, reason

    # --------------------------------------------------------------- filters
    def _local_minutes(self) -> int:
        now = datetime.fromtimestamp(self._clock())
        return now.hour * 60 + now.minute

    def _in_quiet_hours(self) -> bool:
        window = self.cfg.quiet_hours
        if not window:
            return False
        start, end = int(window[0]), int(window[1])
        if start == end:
            return False
        minute = self._local_minutes()
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end  # wraps past midnight

    def _suppression_reason(self, event: NotificationEvent, *, force: bool) -> str:
        if force:
            return ""
        if event.type not in self.cfg.notify_on:
            return "event_not_enabled"
        if not severity_at_least(event.resolved_severity(), self.cfg.min_severity):
            return "below_min_severity"
        if self._in_quiet_hours() and event.resolved_severity() != "critical":
            return "quiet_hours"
        window = max(0, int(self.cfg.dedupe_window_seconds))
        if window > 0:
            last = self._last_sent.get(event.dedupe_key())
            if last is not None and (self._clock() - last) < window:
                return "dedupe_window"
        # The hourly cap is an anti-spam guard for routine traffic.  It counts
        # **network** pushes only (`requires_network`); local console/file
        # mirroring is free and must never eat the budget.  A critical event
        # (risk halt, data fail-safe) carries information the operator must
        # receive, so it bypasses the cap -- it is still deduped and still
        # recorded, but it is never swallowed by a noisy hour.
        # ``network_budget`` prunes the trailing hour before counting.
        if event.resolved_severity() != SEVERITY_CRITICAL and \
                self.network_budget()["blocks_trade_notification"]:
            return "max_per_hour"
        return ""

    # ------------------------------------------------------------------ send
    def _backoff(self, attempt: int) -> float:
        initial = max(0.0, float(self.cfg.backoff_initial_seconds))
        if initial <= 0:
            return 0.0
        delay = initial * (2 ** max(0, attempt - 1))
        return min(delay, max(0.0, float(self.cfg.backoff_max_seconds)))

    def _attempt(self, provider: Provider, event: NotificationEvent) -> SendResult:
        """Send with bounded exponential backoff; returns the last result, never raises."""
        max_attempts = max(1, min(1 + max(0, int(self.cfg.retry_max)), MAX_ATTEMPTS))
        result: Optional[SendResult] = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = provider.send(event)
            except Exception as exc:
                result = SendResult(ok=False, status="failed", detail="{}: {}".format(
                    type(exc).__name__, redact(exc, secrets=self.secrets)), attempts=attempt, retryable=True)
            result.attempts = attempt
            if result.ok:
                return result
            if not result.retryable or attempt >= max_attempts:
                return result
            delay = self._backoff(attempt)
            if delay > 0:
                try:
                    self._sleep(delay)
                except Exception:  # pragma: no cover - a broken sleep must not break us
                    pass
        return result or SendResult(ok=False, status="failed", detail="no attempt made")

    # -------------------------------------------------------------- recording
    def _append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        safe = {key: record.get(key) for key in RECORD_FIELDS}
        safe["reason"] = redact(safe.get("reason"), secrets=self.secrets)
        safe["response"] = redact(safe.get("response"), secrets=self.secrets)
        safe["title"] = redact(safe.get("title"), secrets=self.secrets)
        safe["body"] = redact(safe.get("body"), secrets=self.secrets)
        safe["target"] = redact(safe.get("target"), secrets=self.secrets)
        if "preview" in record:
            preview = record.get("preview") or {}
            safe["preview"] = {
                "method": preview.get("method"),
                "url": redact(preview.get("url"), secrets=self.secrets),
                "headers": redact_mapping(preview.get("headers") or {}, secrets=self.secrets),
                "body": redact(preview.get("body"), secrets=self.secrets),
            }
        try:
            self.store.append(safe)
        except Exception as exc:
            self.log.warning("notify.audit_write_failed",
                             extra={"event": "notify_audit_write_failed",
                                    "detail": redact(exc, secrets=self.secrets)})
        return safe

    def _record(self, event: NotificationEvent, *, provider: str, status: str, reason: str = "",
                http_status: Optional[int] = None, response: str = "", latency_ms: float = 0.0,
                attempts: int = 0, target: str = "", preview: Optional[Mapping[str, Any]] = None,
                dry_run: Optional[bool] = None) -> Dict[str, Any]:
        ts_ms = int(self._clock() * 1000)
        record: Dict[str, Any] = {
            "ts": ts_ms,
            "iso_utc": _iso_utc(ts_ms),
            "run_id": event.run_id,
            "event": event.type,
            "severity": event.resolved_severity(),
            "provider": provider,
            "status": status,
            "reason": reason,
            "http_status": http_status,
            "response": response,
            "latency_ms": round(float(latency_ms), 3),
            "attempts": int(attempts),
            "dry_run": bool(self.cfg.dry_run if dry_run is None else dry_run),
            "title": event.title,
            "body": event.body,
            "dedupe_key": event.dedupe_key(),
            "target": target,
        }
        if preview is not None:
            record["preview"] = dict(preview)
        return self._append(record)

    def _log_dispatch(self, record: Mapping[str, Any], event: NotificationEvent) -> None:
        fields = {
            "event": "notify_dispatch",
            "notify_event": event.type,
            "severity": event.resolved_severity(),
            "provider": record.get("provider"),
            "status": record.get("status"),
            "reason": redact(record.get("reason"), secrets=self.secrets),
            "http_status": record.get("http_status"),
            "latency_ms": record.get("latency_ms"),
            "attempts": record.get("attempts"),
        }
        try:
            if str(record.get("status")) == "failed":
                self.log.warning("notify.failed", extra=fields)
            else:
                self.log.info("notify.dispatch", extra=fields)
        except Exception:  # pragma: no cover - logging must not break the loop
            pass

    # ----------------------------------------------------------------- public
    def notify(self, event: NotificationEvent, *, force: bool = False,
               dry_run: Optional[bool] = None) -> List[Dict[str, Any]]:
        """Deliver ``event``. ALWAYS returns the audit records; NEVER raises."""
        records: List[Dict[str, Any]] = []
        effective_dry_run = bool(self.cfg.dry_run if dry_run is None else dry_run)
        try:
            if not self.cfg.enabled and not force:
                return records
            if event is None:  # pragma: no cover - defensive
                return records
            reason = self._suppression_reason(event, force=force)
            if reason:
                record = self._record(event, provider="-", status="suppressed", reason=reason, dry_run=False)
                self._log_dispatch(record, event)
                return [record]

            providers = self.active_providers()
            if not providers:
                record = self._record(event, provider="-", status="suppressed",
                                      reason="no_active_provider", dry_run=False)
                self._log_dispatch(record, event)
                return [record]

            for provider in providers:
                try:
                    # Defence in depth, before any provider attempt: a network
                    # provider is refused outright inside the offline harness
                    # (harness_guard) and in replay/offline runs (replay_no_push),
                    # and a TP/SL variant is refused when the close already
                    # reports it (superseded_by_position_closed).  Each refusal
                    # is audited, so it is never silent.
                    blocked = self._network_guard_reason(provider) or \
                        self._superseded_reason(event, provider, force=force)
                    if blocked:
                        record = self._record(event, provider=provider.name, status="suppressed",
                                              reason=blocked, dry_run=False)
                        records.append(record)
                        self._log_dispatch(record, event)
                        continue
                    if effective_dry_run:
                        rendered = provider.render(event)
                        preview = rendered.safe(self.secrets) if rendered is not None else {}
                        record = self._record(event, provider=provider.name, status="dry_run",
                                              reason="", target=preview.get("url", ""), preview=preview,
                                              dry_run=True)
                    else:
                        result = self._attempt(provider, event)
                        status = "sent" if result.ok else "failed"
                        record = self._record(
                            event, provider=provider.name, status=status,
                            reason="" if result.ok else (result.detail or "provider failure"),
                            http_status=result.http_status, response=result.response_body,
                            latency_ms=result.latency_ms, attempts=result.attempts,
                            target=self._target_of(provider, event),
                        )
                        if result.ok:
                            now = self._clock()
                            # Only a network push consumes the hourly budget; the
                            # local console/file mirror is free (see network_budget).
                            if self._requires_network(provider):
                                self._sent_times.append(now)
                            self._last_sent[event.dedupe_key()] = now
                except Exception as exc:
                    # A provider must never be able to escape into the trading loop.
                    record = self._record(event, provider=getattr(provider, "name", "?"), status="failed",
                                          reason="provider crashed: {}: {}".format(
                                              type(exc).__name__, redact(exc, secrets=self.secrets)))
                records.append(record)
                self._log_dispatch(record, event)
            return records
        except Exception as exc:  # last line of defence
            try:
                record = self._record(event, provider="-", status="failed",
                                      reason="notifier error: {}: {}".format(
                                          type(exc).__name__, redact(exc, secrets=self.secrets)))
                records.append(record)
            except Exception:
                pass
            return records

    def _target_of(self, provider: Provider, event: NotificationEvent) -> str:
        try:
            rendered = provider.render(event)
            return rendered.url if rendered is not None else ""
        except Exception:  # pragma: no cover - defensive
            return ""

    def test(self, *, message: str = "", dry_run: Optional[bool] = None) -> List[Dict[str, Any]]:
        """Explicit test message (bypasses enabled/notify_on/dedupe/rate/quiet)."""
        effective_dry_run = bool(self.cfg.dry_run if dry_run is None else dry_run)
        return self.notify(test_event(message=message, dry_run=effective_dry_run,
                                      ts=int(self._clock() * 1000)),
                           force=True, dry_run=effective_dry_run)

    def rendered_preview(self, event: NotificationEvent) -> List[Dict[str, Any]]:
        """What each active provider would send, without sending anything."""
        out = []
        for provider in self.active_providers():
            try:
                rendered = provider.render(event)
            except Exception as exc:  # pragma: no cover - defensive
                out.append({"provider": provider.name, "error": redact(exc, secrets=self.secrets)})
                continue
            out.append(rendered.safe(self.secrets) if rendered is not None else {"provider": provider.name})
        return out


__all__ = ["NotifyConfig", "Notifier", "MAX_ATTEMPTS", "SEVERITIES"]
