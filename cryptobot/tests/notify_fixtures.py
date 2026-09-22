"""Fixtures for the notification tests: a real local HTTP sink and provider stubs.

Everything here is offline: the sink listens on ``127.0.0.1`` on an ephemeral
port, so the notification tests exercise the *real* HTTP client end to end
without leaving the machine.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence

from cryptobot.notify.providers import Provider, SendResult


class _SinkHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:  # pragma: no cover - our payloads are utf-8
            body = raw.decode("latin-1", "replace")
        self.server.requests.append({  # type: ignore[attr-defined]
            "path": self.path,
            "headers": {str(k): str(v) for k, v in self.headers.items()},
            "body": body,
        })
        plan: List[int] = self.server.responses  # type: ignore[attr-defined]
        status = plan.pop(0) if plan else 200
        payload = {"ok": status < 400, "sink": "cryptobot-tests", "status": status}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:  # silence the test console
        return


class LocalSink:
    """A real HTTP server on 127.0.0.1 that records every POST it receives.

    ``statuses`` scripts the response codes (e.g. ``[500, 500, 200]`` to prove a
    retry), after which it answers ``200``.
    """

    def __init__(self, statuses: Optional[Sequence[int]] = None) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SinkHandler)
        self.server.daemon_threads = True
        self.server.requests = []  # type: ignore[attr-defined]
        self.server.responses = list(statuses or [])  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # -------------------------------------------------------------- lifecycle
    def start(self) -> "LocalSink":
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            self._thread.join(timeout=5)

    def __enter__(self) -> "LocalSink":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------ access
    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return "http://{}:{}".format(host, port)

    @property
    def requests(self) -> List[Dict[str, Any]]:
        return list(self.server.requests)  # type: ignore[attr-defined]

    def last(self) -> Dict[str, Any]:
        return self.requests[-1]

    def paths(self) -> List[str]:
        return [item["path"] for item in self.requests]

    def bodies(self) -> List[str]:
        return [item["body"] for item in self.requests]

    def header(self, name: str, index: int = -1) -> Optional[str]:
        headers = self.requests[index]["headers"]
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None


# --------------------------------------------------------------------------- #
# provider stubs
# --------------------------------------------------------------------------- #
class RecordingProvider(Provider):
    """Always 'sends' successfully and keeps the events it saw."""

    name = "recording"
    requires_network = False

    def __init__(self) -> None:
        super().__init__()
        self.events: List[Any] = []

    def active(self):
        return True, ""

    def render(self, event):  # pragma: no cover - not used by these tests directly
        from cryptobot.notify.providers import RenderedRequest

        return RenderedRequest(provider=self.name, method="NONE", body=event.body, local=True)

    def send(self, event) -> SendResult:
        self.events.append(event)
        return SendResult(ok=True, status="sent", detail="recorded")


class NetworkRecordingProvider(RecordingProvider):
    """A *network* provider stub: records events and declares ``requires_network``.

    Same behaviour as :class:`RecordingProvider` (no socket is opened), but the
    dispatcher must treat it as a push: its successful sends are the ones that
    consume the hourly budget.
    """

    name = "recording-network"
    requires_network = True


class BrokenProvider(Provider):
    """Always raises a non-transport exception -- the worst case for isolation."""

    name = "broken"
    requires_network = False

    def active(self):
        return True, ""

    def render(self, event):  # pragma: no cover
        return None

    def send(self, event) -> SendResult:
        raise RuntimeError("provider exploded on purpose")


class FlakyProvider(Provider):
    """Fails ``fail_times`` with a retryable result, then succeeds."""

    name = "flaky"
    requires_network = False

    def __init__(self, fail_times: int = 1, *, retryable: bool = True) -> None:
        super().__init__()
        self.fail_times = int(fail_times)
        self.retryable = retryable
        self.calls = 0

    def active(self):
        return True, ""

    def render(self, event):  # pragma: no cover
        from cryptobot.notify.providers import RenderedRequest

        return RenderedRequest(provider=self.name, method="NONE", body=event.body, local=True)

    def send(self, event) -> SendResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return SendResult(ok=False, status="failed", http_status=500, latency_ms=1.0,
                              detail="flaky failure", retryable=self.retryable)
        return SendResult(ok=True, status="sent", http_status=200, latency_ms=1.0)


class ExplodingNotifier:
    """A notifier whose ``notify`` raises -- used to prove runner isolation."""

    class _Cfg:
        dry_run = False

    cfg = _Cfg()

    def notify(self, event, **kwargs: Any) -> None:
        raise RuntimeError("notifier exploded on purpose")

    def status(self) -> List[Dict[str, Any]]:
        return [{"provider": "exploding", "active": True, "reason": "", "network": False}]


class CapturingNotifier:
    """Minimal notifier double that records the events the runner emits."""

    class _Cfg:
        dry_run = False

    cfg = _Cfg()

    def __init__(self) -> None:
        self.events: List[Any] = []

    def notify(self, event, **kwargs: Any) -> List[Dict[str, Any]]:
        self.events.append(event)
        return []

    def status(self) -> List[Dict[str, Any]]:
        return [{"provider": "capturing", "active": True, "reason": "", "network": False}]

    def types(self) -> List[str]:
        return [event.type for event in self.events]


__all__ = [
    "LocalSink", "RecordingProvider", "NetworkRecordingProvider", "BrokenProvider", "FlakyProvider",
    "ExplodingNotifier", "CapturingNotifier",
]
