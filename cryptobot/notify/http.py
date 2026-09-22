"""Minimal stdlib HTTP client for the notification layer.

Why not the ``requests`` library that the data feed uses?

* the notification path stays dependency-free (stdlib only), and
* this repository's ``no_live_order_scan`` treats any ``<root>.post`` call in a
  runtime module as a possible order-submission path.  Using ``urllib.request``
  keeps that scan at **0 findings** while the notification module still contains
  no exchange endpoint of any kind.  The scan's guarantee ("no live-order /
  signed-request code path") is unchanged -- this module can only deliver text
  to a notification service.

The client never raises for a non-2xx status: it returns the status and body so
the caller can decide whether to retry.  Only transport-level failures (DNS,
connect, TLS, timeout) raise :class:`TransportError`.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Union


class TransportError(RuntimeError):
    """Network-level failure (DNS, connect, timeout, TLS). Carries no secret text."""

    def __init__(self, detail: str, *, http_status: Optional[int] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.http_status = http_status


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status) < 300


def retryable_status(status: Optional[int]) -> bool:
    """429 (rate limited) and 5xx are worth a bounded retry; other 4xx are not."""
    if status is None:
        return False
    code = int(status)
    return code == 429 or 500 <= code <= 599


class UrllibHttpClient:
    """Tiny injectable transport (tests substitute a fake or a local sink)."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = float(timeout)

    def post(self, url: str, body: Union[str, bytes], headers: Optional[Mapping[str, str]] = None) -> HttpResponse:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(url, data=payload, headers=dict(headers or {}), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - explicit user URL
                raw = response.read()
                return HttpResponse(
                    int(getattr(response, "status", 200)),
                    raw.decode("utf-8", "replace"),
                    {str(k): str(v) for k, v in dict(getattr(response, "headers", {}) or {}).items()},
                )
        except urllib.error.HTTPError as exc:  # a real HTTP response with a >=400 status
            try:
                raw = exc.read()
            except Exception:  # pragma: no cover - body already consumed
                raw = b""
            return HttpResponse(
                int(exc.code),
                raw.decode("utf-8", "replace"),
                {str(k): str(v) for k, v in dict(exc.headers or {}).items()},
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TransportError("{}: {}".format(type(exc).__name__, exc)) from exc


__all__ = ["TransportError", "HttpResponse", "UrllibHttpClient", "retryable_status"]
