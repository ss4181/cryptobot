"""Offline unittest suite (no network access).

Importing this package quiets the ``cryptobot`` logger hierarchy **only**: a
:class:`logging.NullHandler` is attached to the ``cryptobot`` logger with
``propagate=False``, so the suite's own records do not reach the root logger.

This deliberately replaces the old ``logging.disable(CRITICAL)`` switch.  That
call flips a *process-wide* flag, so any module that merely imported the test
package (e.g. ``scripts/capture_evidence.py`` for its fixtures) had all of its
logging silently suppressed.  Scoping the change to the ``cryptobot`` logger
leaves every other logger (third-party libraries included) untouched.

Tests that assert on logging re-enable propagation themselves -- see
``test_monitor.py``.  Set ``CRYPTOBOT_TEST_LOG=1`` to see the structured logs
while running the suite (no silencing at all in that mode).

The suite is also **environment-independent**: importing this package removes any
ambient ``CRYPTOBOT_*`` variables, so a real notification topic or data path in
the caller's environment can never change a test outcome.

**Run-directory isolation.**  ``cryptobot/runner.py`` resolves the cooperating
``paper_state.json`` / ``paper.stop`` / ``paper.pid`` files through
:func:`cryptobot.runner.run_dir`, which honours ``CRYPTOBOT_RUN_DIR`` and
otherwise falls back to the *real* ``cryptobot/run/``.  ``paper.stop`` is the
cooperative stop sentinel: a live ``paperbot.py run`` process polls it every
cycle and terminates itself when it appears, and ``_write_state`` overwrites the
live run's ``paper_state.json``.  A test that called ``request_stop()`` or ran a
:class:`~cryptobot.runner.PaperRunner` while ``CRYPTOBOT_RUN_DIR`` was unset
therefore reached into production state -- stopping a live bot.

Importing this package now (re)points ``CRYPTOBOT_RUN_DIR`` at a private,
per-process temporary directory, and adds three structural defences:

* :func:`isolate_environment` -- strips the ambient ``CRYPTOBOT_*`` namespace and
  then *sets* the private run directory, so the removal of a caller's override can
  never leave the suite pointing at the real one;
* :func:`assert_isolated_run_dir` -- a hard guard, evaluated at import time (i.e.
  at test collection), that fails loudly if the session is aimed at the real
  ``cryptobot/run/``;
* an audit hook recording every write this process makes inside the real run
  directory (:func:`real_run_dir_writes`), asserted empty by
  ``test_zz_run_dir_isolation.py`` and by ``scripts/verify_all.py``.

Finally, importing this package arms the **notification harness guard**
(``cryptobot.notify.guard.HARNESS_ENV``): for the whole process, the dispatcher
refuses to hand an event to a network provider pointed at a non-loopback host.
That is the structural fix for the historic flood -- a persistent *user-level*
``CRYPTOBOT_NTFY_TOPIC`` used to make every test run a real publisher.  The
guard is independent of ``isolate_environment`` on purpose, so it also covers
tests that deliberately set a real-looking topic with ``mock.patch.dict``.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from cryptobot.notify.guard import HARNESS_ENV, enter_harness_mode

_QUIET_LOGGER = "cryptobot"

#: Variables this package never removes (they control the suite itself).
#:
#: ``CRYPTOBOT_RUN_DIR`` is deliberately **not** listed here: keeping it would let
#: an ambient value (a developer's shell, a CI job) survive isolation and aim the
#: suite straight back at the real ``cryptobot/run/`` -- exactly the hazard this
#: module exists to prevent.  It is removed with the rest of the namespace and
#: then re-set to a private temp directory by :func:`isolate_environment`.
_ENV_KEEP = ("CRYPTOBOT_TEST_LOG", HARNESS_ENV)

#: Prefix of the project's configuration-override environment variables.
_ENV_PREFIX = "CRYPTOBOT_"

#: Name of the runner's run-directory override (see ``cryptobot/runner.py``).
RUN_DIR_ENV = "CRYPTOBOT_RUN_DIR"

#: The files ``runner.py`` keeps in the run directory -- the ones that both the
#: live bot and the audit hook care about.
RUN_DIR_FILES = ("paper_state.json", "paper.stop", "paper.pid")


def quiet_cryptobot_logging() -> None:
    """Stop ``cryptobot.*`` records reaching the root logger, and nothing else."""
    logger = logging.getLogger(_QUIET_LOGGER)
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    logger.propagate = False


def restore_cryptobot_logging() -> None:
    """Undo :func:`quiet_cryptobot_logging` (used by logging-aware tests)."""
    logger = logging.getLogger(_QUIET_LOGGER)
    for handler in [h for h in logger.handlers if isinstance(h, logging.NullHandler)]:
        logger.removeHandler(handler)
    logger.propagate = True


# --------------------------------------------------------------------------- #
# the real (production) run directory
# --------------------------------------------------------------------------- #
def real_run_dir() -> Path:
    """The repository's real run directory (``cryptobot/run``).

    Imported lazily so importing the test package stays cheap and cannot create a
    circular import: ``cryptobot.runner`` never imports ``cryptobot.tests``.
    """
    from cryptobot import runner as runner_module

    return Path(runner_module.__file__).resolve().parent / runner_module.RUN_DIR_NAME


# --------------------------------------------------------------------------- #
# private per-process run directory
# --------------------------------------------------------------------------- #
_SUITE_RUN_DIR: str | None = None


def suite_run_dir() -> Path:
    """A private temp run directory for this process, created on first use.

    Every runner write the suite performs (state / stop / pid) lands here instead
    of ``cryptobot/run/``.  Removed at interpreter exit; ``ignore_errors`` because
    Windows refuses to delete a directory whose log/sqlite handle is still open.

    The path is checked to be under the system temp directory *before* the cleanup
    is scheduled: the deletion below is unconditional, so a ``tempfile.mkdtemp``
    that somehow returned (or was made to return) a production path must abort the
    session rather than queue that path for removal.
    """
    global _SUITE_RUN_DIR
    if _SUITE_RUN_DIR is None:
        created = Path(tempfile.mkdtemp(prefix="cryptobot-tests-run-"))
        temp_root = Path(tempfile.gettempdir()).resolve()
        resolved = created.resolve()
        if not (resolved == temp_root or temp_root in resolved.parents):
            raise RuntimeError(
                "TEST ISOLATION BROKEN: the private run directory for the test session resolved "
                "to {!s}, which is not under the system temp directory {!s}. Refusing to continue: "
                "this path would be deleted at interpreter exit.".format(resolved, temp_root)
            )
        _SUITE_RUN_DIR = str(created)
        atexit.register(shutil.rmtree, _SUITE_RUN_DIR, ignore_errors=True)
    return Path(_SUITE_RUN_DIR)


# --------------------------------------------------------------------------- #
# write audit for the real run directory
# --------------------------------------------------------------------------- #
#: Writes this process performed inside the real run directory.  An empty list is
#: the suite's contract; anything else means a test escaped its isolation.
_REAL_RUN_DIR_WRITES: List[Dict[str, Any]] = []

_AUDIT_EVENTS = frozenset({
    "open", "os.mkdir", "os.rmdir", "os.remove", "os.unlink", "os.truncate",
    "os.rename", "os.replace", "os.link", "os.symlink",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.move", "shutil.rmtree",
})

_audit_installed = False


def _audit_open_is_write(mode: Any, flags: Any) -> bool:
    if isinstance(mode, str) and any(character in mode for character in "wax+"):
        return True
    if isinstance(flags, int):
        write_flags = (getattr(os, "O_WRONLY", 1) | getattr(os, "O_RDWR", 2)
                       | getattr(os, "O_CREAT", 64) | getattr(os, "O_APPEND", 8)
                       | getattr(os, "O_TRUNC", 512))
        return bool(flags & write_flags)
    return False


def _install_real_run_dir_audit() -> None:
    """Record any write this process makes inside the real run directory.

    The hook is intentionally cheap on the hot path: ``open`` is the most frequent
    audit event, so the path is only normalised once it contains one of the three
    runner filenames.  It never raises and never blocks a write -- it observes.
    """
    global _audit_installed
    if _audit_installed:
        return
    _audit_installed = True

    target = str(real_run_dir())
    prefix = target + os.sep
    #: Cheap pre-filters for the hot ``open`` path: either the path names the real
    #: run directory outright (the runner always builds absolute paths), or it uses
    #: one of the runner's filenames relative to a cwd that *is* the run directory.
    markers = (target, target.replace(os.sep, "/"))

    def _record(event: str, raw: Any) -> None:
        try:
            if isinstance(raw, str):
                text = raw
            elif isinstance(raw, os.PathLike):
                text = os.fspath(raw)
                if not isinstance(text, str):
                    return
            else:
                return
            if event == "open" and not (any(marker in text for marker in markers)
                                        or any(name in text for name in RUN_DIR_FILES)):
                return
            absolute = os.path.abspath(text)
            if absolute != target and not absolute.startswith(prefix):
                return
            _REAL_RUN_DIR_WRITES.append({"event": event, "path": absolute})
        except Exception:  # pragma: no cover - an audit hook must never raise
            return

    def hook(event: str, args: tuple) -> None:
        if event not in _AUDIT_EVENTS:
            return
        if not args:
            return
        if event == "open":
            if len(args) >= 3 and not _audit_open_is_write(args[1], args[2]):
                return
            _record(event, args[0])
            return
        _record(event, args[0])
        if event in ("os.rename", "os.replace", "os.link", "os.symlink", "shutil.move"):
            if len(args) > 1:
                _record(event, args[1])

    sys.addaudithook(hook)


def real_run_dir_writes() -> List[Dict[str, Any]]:
    """Writes this process made inside the real run directory (must stay empty)."""
    return list(_REAL_RUN_DIR_WRITES)


# --------------------------------------------------------------------------- #
# environment isolation
# --------------------------------------------------------------------------- #
def isolate_environment() -> None:
    """Drop ambient ``CRYPTOBOT_*`` overrides so the suite is environment-independent.

    The project's whole configuration-override namespace is removed: a
    developer's (or a CI runner's) real notification topic, cache dir or ledger
    path must never be able to change a test outcome.  Tests that need a value
    set it themselves with ``mock.patch.dict`` and restore it afterwards.

    Removing the namespace also removes ``CRYPTOBOT_RUN_DIR``.  Left that way,
    :func:`cryptobot.runner.run_dir` would fall back to the real
    ``cryptobot/run/`` and a test calling ``request_stop()`` would drop the
    cooperative stop sentinel next to a live bot.  The private directory is
    therefore (re)assigned *after* the strip, inside this function, so it holds
    however often the function runs -- including the extra calls the isolation
    tests make.
    """
    for name in [key for key in list(os.environ)
                 if key.startswith(_ENV_PREFIX) and key not in _ENV_KEEP]:
        os.environ.pop(name, None)
    os.environ[RUN_DIR_ENV] = str(suite_run_dir())


def assert_isolated_run_dir() -> None:
    """Hard guard: fail loudly if the session points at the real run directory.

    Called at import time, i.e. while unittest is *collecting* tests, so a
    misconfigured session aborts before any test body runs rather than silently
    writing a stop sentinel into a production path.
    """
    from cryptobot.runner import run_dir, stop_path

    real = real_run_dir().resolve()
    actual = Path(run_dir()).resolve()
    if actual == real or Path(stop_path()).resolve().parent == real:
        raise RuntimeError(
            "TEST ISOLATION BROKEN: the test suite resolves the runner run directory to the "
            "repository's real {real!s} (CRYPTOBOT_RUN_DIR={value!r}).\n"
            "Running the suite from there writes the cooperative stop sentinel {stop!s} and "
            "overwrites {state!s}, which would terminate or corrupt a live "
            "`paperbot.py run` process.\n"
            "Fix: let cryptobot.tests.isolate_environment() assign a private temp directory "
            "(do not pin CRYPTOBOT_RUN_DIR at the real path).".format(
                real=real, value=os.environ.get(RUN_DIR_ENV), stop=real / "paper.stop",
                state=real / "paper_state.json",
            )
        )


if os.environ.get("CRYPTOBOT_TEST_LOG") != "1":
    quiet_cryptobot_logging()
isolate_environment()
# Arm the write audit and the guard as soon as the private run directory exists, so
# every test body (and the collection phase itself) is covered.
_install_real_run_dir_audit()
assert_isolated_run_dir()
# Arm the structural network guard *after* isolate_environment: HARNESS_ENV is
# intentionally outside the CRYPTOBOT_* namespace so neither step undoes the other.
enter_harness_mode()


__all__ = [
    "quiet_cryptobot_logging", "restore_cryptobot_logging", "isolate_environment",
    "real_run_dir", "suite_run_dir", "assert_isolated_run_dir", "real_run_dir_writes",
    "RUN_DIR_ENV",
]
