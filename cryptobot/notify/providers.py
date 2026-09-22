"""Notification providers: ntfy (default), telegram, webhook, console, file.

Each provider is small and independent:

* :meth:`Provider.render` builds the exact request that would go out
  (method/url/headers/body) *without* sending it -- this is what ``--dry-run``
  shows and what the tests assert on.
* :meth:`Provider.send` performs the request through an injected transport.

Nothing here knows about exchanges or orders; the only outbound endpoints are a
notification service and an optional user-supplied webhook URL.  A provider that
is not configured reports itself inactive with a human reason, so
``notify status`` can explain *why* nothing is going to a phone.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from .events import NotificationEvent
from .http import UrllibHttpClient, retryable_status
from .redact import REDACTED, redact, redact_mapping
from .render import render_body

#: ntfy push priority per severity (1=min .. 5=max).
PRIORITY_BY_SEVERITY = {"info": 3, "warning": 4, "critical": 5}
#: ntfy tags per severity (the app renders these as an emoji).
TAGS_BY_SEVERITY = {"info": "information_source", "warning": "warning", "critical": "rotating_light"}

_MAX_TITLE = 200
_MAX_BODY = 3800
_MAX_DETAIL = 400
_MAX_RESPONSE = 300

_TR_TRANSLITERATION = str.maketrans({
    "ş": "s", "Ş": "S", "ğ": "g", "Ğ": "G", "ı": "i", "İ": "I",
    "ö": "o", "Ö": "O", "ü": "u", "Ü": "U", "ç": "c", "Ç": "C",
})


def ascii_header(value: object, *, limit: int = _MAX_TITLE) -> str:
    """Make a value safe for an HTTP header: ASCII, single line, truncated."""
    text = str(value).translate(_TR_TRANSLITERATION)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    text = text.encode("ascii", "replace").decode("ascii").replace("?", "?")
    return text[:limit]


def _clip(text: object, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: max(0, limit - 3)] + "..."


_console_logger = logging.getLogger("cryptobot.notify.console")


def _console_writer(line: str) -> None:
    """Default console sink: the project's readable console log stream."""
    _console_logger.info(line)


@dataclass
class RenderedRequest:
    """The exact request a provider would issue (dry-run-visible)."""

    provider: str
    method: str
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    local: bool = False

    def safe(self, secrets: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
        """Redacted, JSON-friendly view of this request."""
        return {
            "provider": self.provider,
            "method": self.method,
            "url": redact(self.url, secrets=secrets),
            "headers": redact_mapping(self.headers, secrets=secrets),
            "body": redact(self.body, secrets=secrets),
        }


@dataclass
class SendResult:
    ok: bool
    status: str
    http_status: Optional[int] = None
    latency_ms: float = 0.0
    detail: str = ""
    response_body: str = ""
    attempts: int = 1
    retryable: bool = False


class Provider:
    """Base class. ``send`` is only ever called by the dispatcher (which retries)."""

    name = "base"
    requires_network = False

    def __init__(self, *, transport=None, mono: Optional[Callable[[], float]] = None) -> None:
        self.transport = transport if transport is not None else UrllibHttpClient()
        self.mono = mono or time.perf_counter

    # ------------------------------------------------------------- interface
    def active(self) -> Tuple[bool, str]:
        """``(active, reason_when_inactive)``."""
        raise NotImplementedError

    def guard_target(self) -> str:
        """URL/host this provider would contact, or ``""`` when it is purely local.

        Consumed by :mod:`cryptobot.notify.guard` to decide whether a dispatch
        may leave the machine at all (harness guard / replay no-push).  A local
        provider returns ``""``.
        """
        return ""

    def render(self, event: NotificationEvent) -> Optional[RenderedRequest]:
        raise NotImplementedError

    def send(self, event: NotificationEvent) -> SendResult:
        rendered = self.render(event)
        if rendered is None:  # pragma: no cover - active() is checked first
            return SendResult(ok=False, status="failed", detail="provider rendered nothing")
        started = self.mono()
        if rendered.local:
            self._local_send(event, rendered)
            return SendResult(ok=True, status="sent", latency_ms=(self.mono() - started) * 1000.0,
                              detail="local")
        response = self.transport.post(rendered.url, rendered.body, rendered.headers)
        latency = (self.mono() - started) * 1000.0
        ok = response.ok
        return SendResult(
            ok=ok,
            status="sent" if ok else "failed",
            http_status=response.status,
            latency_ms=latency,
            detail="" if ok else _clip(redact(response.body), _MAX_DETAIL),
            response_body=_clip(redact(response.body), _MAX_RESPONSE),
            retryable=retryable_status(response.status),
        )

    def _local_send(self, event: NotificationEvent, rendered: RenderedRequest) -> None:  # pragma: no cover
        return None


# --------------------------------------------------------------------------- #
# ntfy (default; free, no account)
# --------------------------------------------------------------------------- #
class NtfyProvider(Provider):
    """Publishes to ``https://<host>/<topic>``; the phone app subscribes to the topic."""

    name = "ntfy"
    requires_network = True

    def __init__(self, *, host: str, topic: Optional[str], token: Optional[str] = None,
                 tags: Optional[str] = None, click: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.host = str(host or "ntfy.sh").strip()
        self.topic = (topic or "").strip()
        self.token = (token or "").strip()
        self.tags = (tags or "").strip()
        self.click = (click or "").strip()

    def active(self) -> Tuple[bool, str]:
        if not self.topic:
            return False, "ntfy_topic ayarli degil (ortam: CRYPTOBOT_NTFY_TOPIC)"
        if not self.host:
            return False, "ntfy_host bos"
        return True, ""

    def _base(self) -> str:
        host = self.host.rstrip("/")
        if host.startswith("http://") or host.startswith("https://"):
            return host
        return "https://" + host

    def guard_target(self) -> str:
        return self._base()

    def render(self, event: NotificationEvent) -> Optional[RenderedRequest]:
        active, _reason = self.active()
        if not active:
            return None
        severity = event.resolved_severity()
        headers = {
            "Title": ascii_header(event.title, limit=_MAX_TITLE),
            "Priority": str(PRIORITY_BY_SEVERITY.get(severity, 3)),
            "Tags": self.tags or TAGS_BY_SEVERITY.get(severity, "information_source"),
            # ntfy renders **bold** headings only when Markdown is enabled.
            "Markdown": "yes",
        }
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        if self.click:
            headers["Click"] = self.click
        return RenderedRequest(
            provider=self.name,
            method="POST",
            url="{}/{}".format(self._base(), quote(self.topic, safe="")),
            headers=headers,
            body=_clip(render_body(event, "markdown"), _MAX_BODY),
        )


# --------------------------------------------------------------------------- #
# telegram (only active when both env values exist)
# --------------------------------------------------------------------------- #
class TelegramProvider(Provider):
    """Sends via ``{api_base}/bot<token>/sendMessage`` (HTML parse mode)."""

    name = "telegram"
    requires_network = True

    def __init__(self, *, bot_token: Optional[str], chat_id: Optional[str],
                 api_base: str = "https://api.telegram.org", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.api_base = str(api_base or "https://api.telegram.org").rstrip("/")

    def active(self) -> Tuple[bool, str]:
        missing = []
        if not self.bot_token:
            missing.append("CRYPTOBOT_TELEGRAM_BOT_TOKEN")
        if not self.chat_id:
            missing.append("CRYPTOBOT_TELEGRAM_CHAT_ID")
        if missing:
            return False, "ortam degiskeni yok: {}".format(", ".join(missing))
        return True, ""

    def guard_target(self) -> str:
        return self.api_base

    def render(self, event: NotificationEvent) -> Optional[RenderedRequest]:
        active, _reason = self.active()
        if not active:
            return None
        # ``render_body(..., "html")`` escapes every dynamic value itself, so a
        # reason string containing "<" cannot break Telegram's parser.
        text = render_body(event, "html")[: _MAX_BODY]
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        return RenderedRequest(
            provider=self.name,
            method="POST",
            url="{}/bot{}/sendMessage".format(self.api_base, self.bot_token),
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )


# --------------------------------------------------------------------------- #
# generic webhook (Discord / Slack / Zapier / n8n)
# --------------------------------------------------------------------------- #
class WebhookProvider(Provider):
    """POSTs a JSON envelope that carries both ``text`` (Slack) and ``content`` (Discord)."""

    name = "webhook"
    requires_network = True

    def __init__(self, *, url: Optional[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.url = (url or "").strip()

    def active(self) -> Tuple[bool, str]:
        if not self.url:
            return False, "adres yok (ortam: CRYPTOBOT_WEBHOOK_URL)"
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            return False, "adres http(s) ile baslamali"
        return True, ""

    def guard_target(self) -> str:
        return self.url

    def render(self, event: NotificationEvent) -> Optional[RenderedRequest]:
        active, _reason = self.active()
        if not active:
            return None
        severity = event.resolved_severity()
        text = render_body(event, "webhook")
        payload: Dict[str, Any] = {
            "source": "cryptobot",
            "event": event.type,
            "severity": severity,
            "title": event.title,
            "text": text,
            "content": text,          # Discord-compatible
            "ts": int(event.ts),
            "run_id": event.run_id,
            "pair": event.pair,
            "data": dict(sorted(event.data.items())),
        }
        return RenderedRequest(
            provider=self.name,
            method="POST",
            url=self.url,
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )


# --------------------------------------------------------------------------- #
# local fallbacks (no network at all)
# --------------------------------------------------------------------------- #
class ConsoleProvider(Provider):
    """Always available: writes one human-readable line to the console.

    The line goes through the project's logging setup (``cryptobot.notify.console``
    -> root console handler) instead of ``print`` so that ``--log-level`` controls
    it and the offline unittest suite -- which scopes its silencing to the
    ``cryptobot`` logger -- stays quiet.
    """

    name = "console"

    def __init__(self, *, writer: Optional[Callable[[str], None]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.writer = writer or _console_writer

    def active(self) -> Tuple[bool, str]:
        return True, ""

    def render(self, event: NotificationEvent) -> RenderedRequest:
        text = render_body(event, "console")
        return RenderedRequest(provider=self.name, method="NONE", url="", headers={}, body=text, local=True)

    def _local_send(self, event: NotificationEvent, rendered: RenderedRequest) -> None:
        try:
            self.writer(rendered.body)
        except Exception:  # a broken console must not break the loop
            pass


class FileProvider(Provider):
    """Always available: its output file *is* the audit store (``logs/notifications.jsonl``).

    The dispatcher appends one JSON line per dispatch attempt to that store, so
    enabling ``file`` guarantees the local record exists even when every network
    provider is down or unconfigured.
    """

    name = "file"

    def __init__(self, *, path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        from pathlib import Path

        self.path = Path(path)

    def active(self) -> Tuple[bool, str]:
        return True, ""

    def render(self, event: NotificationEvent) -> RenderedRequest:
        return RenderedRequest(
            provider=self.name,
            method="APPEND",
            url=str(self.path),
            headers={},
            body=render_body(event, "file"),
            local=True,
        )


def build_providers(cfg: Any, *, transport=None, mono: Optional[Callable[[], float]] = None,
                    writer: Optional[Callable[[str], None]] = None) -> List[Provider]:
    """Instantiate every provider named in ``cfg.providers`` (active or not)."""
    transport = transport if transport is not None else UrllibHttpClient(timeout=cfg.timeout_seconds)
    factory = {
        "ntfy": lambda: NtfyProvider(host=cfg.ntfy_host, topic=cfg.ntfy_topic, token=cfg.ntfy_token,
                                     tags=cfg.ntfy_tags, click=cfg.ntfy_click,
                                     transport=transport, mono=mono),
        "telegram": lambda: TelegramProvider(bot_token=cfg.telegram_bot_token, chat_id=cfg.telegram_chat_id,
                                             api_base=cfg.telegram_api_base,
                                             transport=transport, mono=mono),
        "webhook": lambda: WebhookProvider(url=cfg.webhook_url, transport=transport, mono=mono),
        "console": lambda: ConsoleProvider(writer=writer, mono=mono),
        "file": lambda: FileProvider(path=cfg.store_path, mono=mono),
    }
    return [factory[name]() for name in cfg.providers if name in factory]


__all__ = [
    "Provider", "RenderedRequest", "SendResult", "NtfyProvider", "TelegramProvider",
    "WebhookProvider", "ConsoleProvider", "FileProvider", "build_providers",
    "PRIORITY_BY_SEVERITY", "TAGS_BY_SEVERITY", "ascii_header", "REDACTED",
]
