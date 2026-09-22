"""Local read-only HTTP view of the generated panel (``panel --serve``).

Design constraints, all of them deliberate:

* **Loopback only.**  The listening address is the constant :data:`HOST`
  (``127.0.0.1``); there is no option to widen it, because the panel contains a
  trading history and an audit trail and must never be reachable from the LAN.
* **Read-only.**  Only ``GET``/``HEAD`` are implemented and only the single
  generated document is served -- no directory listing, no file serving from
  disk, no request body is ever read, nothing is written anywhere.
* **Live-ish.**  A provider callable re-renders on every request, so a page
  reload picks up new ledger/audit rows.  If a re-render fails, the last good
  page is served instead of an error: an operator dashboard that dies because a
  log rotated is worse than a slightly stale one.

Stop it with ``Ctrl+C`` in the terminal that started it (documented in the
command output and in ``README.md``).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, Tuple

#: The only address the panel server ever binds to.
HOST = "127.0.0.1"

#: Default port for ``panel --serve``.
DEFAULT_PORT = 8765

#: Paths that serve the panel document.
PANEL_PATHS = ("/", "/index.html", "/panel.html")


class PanelServer:
    """Serve one HTML document (re-rendered on demand) on the loopback interface."""

    def __init__(self, provider: Callable[[], str], *, port: int = DEFAULT_PORT,
                 host: str = HOST) -> None:
        if host != HOST:  # pragma: no cover - defensive: never expose the panel
            raise ValueError("panel server yalnızca {} üzerinde dinleyebilir".format(HOST))
        self.provider = provider
        self.host = host
        self.port = int(port)
        self._cached: bytes = b""
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- content ------------------------------------------------------------
    def document(self) -> bytes:
        """Current document: fresh render when possible, last good one otherwise."""
        try:
            rendered = self.provider()
        except Exception:  # noqa: BLE001 - a broken render must not kill the view
            return self._cached
        if isinstance(rendered, bytes):
            self._cached = rendered
        else:
            self._cached = str(rendered).encode("utf-8")
        return self._cached

    def warm(self) -> bytes:
        """Render once up front so the first request cannot fail."""
        self._cached = b""
        return self.document()

    # -- lifecycle ----------------------------------------------------------
    @property
    def server(self) -> ThreadingHTTPServer:
        if self._httpd is None:
            handler = _handler_for(self.document)
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
            self._httpd.daemon_threads = True
        return self._httpd

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self.server.server_address[0], self.server.server_address[1]
        return (str(host), int(port))

    @property
    def url(self) -> str:
        host, port = self.address
        return "http://{}:{}/".format(host, port)

    def serve_forever(self) -> None:  # pragma: no cover - blocking loop
        self.server.serve_forever(poll_interval=0.2)

    def start_background(self) -> "PanelServer":
        """Bind and serve in a daemon thread (embedding/tests); stop with :meth:`stop`."""
        if self._thread is not None:
            return self
        self.server  # bind + listen before the thread starts
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def _handler_for(document: Callable[[], bytes]) -> type:
    """Request handler class bound to a document provider."""

    class PanelHandler(BaseHTTPRequestHandler):
        server_version = "cryptobot-panel"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def _send(self, status: int, body: bytes, content_type: str, *, head_only: bool) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _dispatch(self, *, head_only: bool) -> None:
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if path in PANEL_PATHS:
                body = document()
                if not body:
                    body = ("<!DOCTYPE html><html lang=\"tr\"><head><meta charset=\"utf-8\">"
                            "<title>cryptobot panel</title></head><body><p>Panel henüz "
                            "üretilemedi.</p></body></html>").encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8", head_only=head_only)
                return
            if path in ("/favicon.ico", "/robots.txt"):
                self._send(404, b"", "text/plain; charset=utf-8", head_only=head_only)
                return
            body = ("<!DOCTYPE html><html lang=\"tr\"><head><meta charset=\"utf-8\">"
                    "<title>404</title></head><body><p>Yalnızca panel sayfası sunulur: "
                    "<code>/</code></p></body></html>").encode("utf-8")
            self._send(404, body, "text/html; charset=utf-8", head_only=head_only)

        def do_GET(self) -> None:  # noqa: N802 - name mandated by BaseHTTPRequestHandler
            self._dispatch(head_only=False)

        def do_HEAD(self) -> None:  # noqa: N802 - name mandated by BaseHTTPRequestHandler
            self._dispatch(head_only=True)

        def log_message(self, fmt: str, *args: object) -> None:  # keep stdout clean
            return

    return PanelHandler


__all__ = ["DEFAULT_PORT", "HOST", "PANEL_PATHS", "PanelServer"]
