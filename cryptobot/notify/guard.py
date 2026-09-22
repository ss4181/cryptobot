"""Structural guarantee that automation can never publish to a real host.

Two independent rules live here; both are enforced by the dispatcher *before*
any provider attempt, and both are recorded in the audit store as
``suppressed`` rows (never silent):

1. **Harness guard.**  The unittest suite, ``verify_all.py``,
   ``acceptance_check.py`` and ``notify_check.py`` set an in-process/env marker
   (:data:`HARNESS_ENV`).  While it is set, a network provider whose target is
   not a loopback address is refused.  A local ``127.0.0.1`` HTTP sink is still
   allowed -- that is how the offline self-checks exercise the real HTTP client
   without a real push provider ever being reachable.

2. **Replay/offline no-push.**  ``run --replay`` / ``run --offline`` walks
   historical (or cached) bars, often hundreds of them, and every one of those
   would otherwise be a real push.  Network sending is therefore off by default
   in those modes; ``run --notify-send`` is the single explicit opt-in.

Why a module of its own
-----------------------
The root cause of the historic notification flood was *not* a bug in the
providers or the retry logic: a persistent **user-level** ``CRYPTOBOT_NTFY_TOPIC``
made every Python process on the machine a potential sender, so running the
400+ test suite published hundreds of real pushes.  The only durable fix is a
guard the harness sets for itself, checked on every dispatch.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional
from urllib.parse import urlsplit

#: Environment marker set by every harness entry point (tests, verify_all,
#: acceptance_check, notify_check).  It lives in the ``CRYPTOBOT_*`` namespace
#: like the other notification knobs, and ``tests/__init__._ENV_KEEP`` makes sure
#: the suite's own environment isolation cannot accidentally clear it.
HARNESS_ENV = "CRYPTOBOT_NOTIFY_HARNESS"

#: Value written to :data:`HARNESS_ENV`.
HARNESS_VALUE = "1"

#: Hosts that never leave the machine; a provider pointed at one is still
#: "network-shaped" (it speaks HTTP) but cannot publish anything.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "[::1]"})

#: Audit reasons, kept in one place so the docs and the tests can name them.
REASON_HARNESS = "harness_guard"
REASON_REPLAY = "replay_no_push"
REASON_OFFLINE = "offline_no_push"
REASON_SUPERSEDED = "superseded_by_position_closed"


def harness_mode(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True when the current process is one of the offline harness entry points."""
    env = os.environ if environ is None else environ
    return str(env.get(HARNESS_ENV) or "").strip() in (HARNESS_VALUE, "true", "True", "yes")


def enter_harness_mode(environ: Optional[dict] = None) -> None:
    """Set the harness marker on the current process (idempotent)."""
    target = os.environ if environ is None else environ
    target[HARNESS_ENV] = HARNESS_VALUE


def exit_harness_mode(environ: Optional[dict] = None) -> None:
    """Clear the harness marker (used by tests that need real-send semantics)."""
    target = os.environ if environ is None else environ
    target.pop(HARNESS_ENV, None)


def target_host(url: object) -> str:
    """Best-effort hostname of a provider target (``http://127.0.0.1:9/x`` -> ``127.0.0.1``).

    Accepts a bare host too (``ntfy.sh``), because a provider may be configured
    with or without a scheme.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    candidate = text
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:  # pragma: no cover - malformed URL, treat as unsafe
        return text
    return host.strip().lower()


def host_is_local(url: object) -> bool:
    """True when ``url`` points at the loopback interface (no real network path)."""
    host = target_host(url)
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    # 127.0.0.0/8 is loopback; treat the whole block as local.
    if host.startswith("127."):
        return True
    return False


def network_guard_reason(
    target: object,
    *,
    requires_network: bool = True,
    network_send_allowed: bool = True,
    block_reason: str = REASON_REPLAY,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Reason a network provider must be refused, or ``""`` when it may send.

    The harness guard is checked first: inside the test suite / verification
    scripts the audit must say ``harness_guard`` even when the run also happens
    to be a replay, because that is the guarantee the harness is making.  The
    replay/offline rule then covers real operator CLI runs, where no harness
    marker is set.  Loopback targets are always allowed: they cannot reach the
    outside world.
    """
    if not requires_network:
        return ""
    host = target_host(target)
    if not host or host_is_local(target):
        return ""
    if harness_mode(environ):
        return REASON_HARNESS
    if not network_send_allowed:
        return str(block_reason or REASON_REPLAY)
    return ""


__all__ = [
    "HARNESS_ENV", "HARNESS_VALUE",
    "REASON_HARNESS", "REASON_REPLAY", "REASON_OFFLINE", "REASON_SUPERSEDED",
    "harness_mode", "enter_harness_mode", "exit_harness_mode",
    "target_host", "host_is_local", "network_guard_reason",
]
