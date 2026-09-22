"""Regression: the suite must never be able to touch the real ``cryptobot/run/``.

Hazard being pinned down (reproduced on the unfixed tree): ``test_runner_paper.py``
calls ``clear_stop()`` / ``request_stop()`` / ``read_state()`` without pointing
``CRYPTOBOT_RUN_DIR`` anywhere.  ``cryptobot/tests/__init__.py`` used to *delete*
that variable, so ``cryptobot.runner.run_dir()`` fell back to the real
``cryptobot/run/``: a suite run dropped ``run/paper.stop`` next to a live
``paperbot.py run`` process -- which polls that sentinel and stops itself -- and
overwrote the live run's ``paper_state.json``.

The assertions below cover both halves of the contract:

* structural -- every runner path the suite can reach resolves inside a private
  temp directory, and a per-test override (``test_cli.py``) still works;
* observational -- ``request_stop()`` creates the sentinel in the private
  directory and *nothing* in the real one, and the process-wide write audit
  (:func:`cryptobot.tests.real_run_dir_writes`) stays empty.

The file is named ``test_zz_*`` on purpose: ``unittest`` discovery walks the
package in sorted order, so this module -- and therefore
:class:`TestWholeSuiteRunDirAudit` -- runs *after* every other test module, which
turns "the real run directory is unchanged after the suite" into an assertion
instead of an assumption.  ``scripts/verify_all.py`` repeats that assertion after
it runs the suite in-process, so an extra later-added module cannot slip past it.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptobot import runner
from cryptobot import tests as tests_package
from cryptobot.runner import (
    clear_stop,
    pid_path,
    read_state,
    request_stop,
    run_dir,
    state_path,
    stop_path,
)


def _listing(directory: Path) -> list:
    """Sorted entries of ``directory``; ``[]`` when it does not exist yet."""
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir())


class RunDirIsolationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        clear_stop()
        self.addCleanup(clear_stop)

    @property
    def real(self) -> Path:
        return tests_package.real_run_dir().resolve()


class TestRunDirIsRedirected(RunDirIsolationTestCase):
    def test_run_dir_is_a_private_temp_directory_not_the_repository(self):
        real = self.real
        actual = Path(run_dir()).resolve()
        self.assertNotEqual(actual, real, "the suite must not resolve the repository run dir")
        temp_root = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(
            actual == temp_root or temp_root in actual.parents,
            "run_dir() must live under the system temp directory, got {!s}".format(actual),
        )

    def test_the_real_run_dir_is_the_repository_package_directory(self):
        expected = Path(runner.__file__).resolve().parent / runner.RUN_DIR_NAME
        self.assertEqual(self.real, expected.resolve())

    def test_state_stop_and_pid_paths_all_follow_the_redirect(self):
        private = Path(run_dir()).resolve()
        real = self.real
        for label, path in (("state", state_path()), ("stop", stop_path()), ("pid", pid_path())):
            with self.subTest(path=label):
                self.assertEqual(Path(path).resolve().parent, private)
                self.assertNotEqual(Path(path).resolve().parent, real)
                self.assertNotEqual(Path(path).resolve(), real / Path(path).name)

    def test_read_state_reads_the_private_state_file(self):
        target = state_path()
        marker = '{"state": "isolation-regression-marker"}'
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            previous = target.read_text(encoding="utf-8")
            self.addCleanup(target.write_text, previous, encoding="utf-8")
        else:
            self.addCleanup(target.unlink, True)
        target.write_text(marker, encoding="utf-8")
        state = read_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["state"], "isolation-regression-marker")


class TestStopSentinelStaysPrivate(RunDirIsolationTestCase):
    def test_request_stop_creates_the_sentinel_outside_the_real_run_dir(self):
        real = self.real
        before = _listing(real)
        real_stop = real / "paper.stop"
        stop_existed_before = real_stop.exists()

        path = request_stop("isolation regression: must not reach a live bot")
        try:
            self.assertTrue(path.exists())
            self.assertEqual(Path(path).resolve().parent, Path(run_dir()).resolve())
            self.assertNotEqual(Path(path).resolve().parent, real)
            self.assertEqual(Path(path).resolve(), stop_path().resolve())
            # The production path is untouched -- no sentinel, no new/removed entry.
            self.assertEqual(real_stop.exists(), stop_existed_before,
                             "request_stop() created the real stop sentinel")
            self.assertEqual(_listing(real), before, "the real run dir changed")
        finally:
            clear_stop()

    def test_stop_requested_never_sees_a_foreign_sentinel(self):
        clear_stop()
        self.assertFalse(runner.stop_requested())
        request_stop("private sentinel")
        self.assertTrue(runner.stop_requested())
        self.assertNotEqual(Path(stop_path()).resolve().parent, self.real)


class TestEnvironmentIsolationKeepsTheRedirect(RunDirIsolationTestCase):
    def test_ambient_run_dir_cannot_point_the_session_at_the_repository(self):
        real = self.real
        with mock.patch.dict(os.environ, {tests_package.RUN_DIR_ENV: str(real)}):
            tests_package.isolate_environment()
            self.assertNotEqual(Path(run_dir()).resolve(), real)

    def test_every_isolation_call_re_asserts_the_private_directory(self):
        # Tests call isolate_environment() themselves (see test_test_logging_hygiene);
        # it must never leave the session on the real path.
        tests_package.isolate_environment()
        self.assertEqual(
            Path(run_dir()).resolve(),
            tests_package.suite_run_dir().resolve(),
        )

    def test_a_per_test_override_still_wins(self):
        """``test_cli.py`` sets CRYPTOBOT_RUN_DIR per test; that must keep working."""
        real = self.real
        with tempfile.TemporaryDirectory() as tmp:
            per_test = Path(tmp) / "run"
            per_test.mkdir()
            with mock.patch.dict(os.environ, {tests_package.RUN_DIR_ENV: str(per_test)}):
                self.assertEqual(Path(run_dir()).resolve(), per_test.resolve())
                self.assertEqual(stop_path().resolve(), (per_test / "paper.stop"))
        # Whatever the ambient value was (this module's private dir, or a harness's own
        # temp dir when the suite runs inside scripts/verify_all.py), the redirect must
        # survive the per-test override and never land on the repository run dir.
        restored = Path(run_dir()).resolve()
        self.assertNotEqual(restored, real)
        temp_root = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(restored == temp_root or temp_root in restored.parents)

    def test_the_guard_rejects_a_session_aimed_at_the_real_run_dir(self):
        real = self.real
        with mock.patch.dict(os.environ, {tests_package.RUN_DIR_ENV: str(real)}):
            with self.assertRaises(RuntimeError) as ctx:
                tests_package.assert_isolated_run_dir()
        message = str(ctx.exception)
        self.assertIn("TEST ISOLATION BROKEN", message)
        self.assertIn("paper.stop", message)

    def test_the_guard_accepts_the_private_run_dir(self):
        tests_package.isolate_environment()
        tests_package.assert_isolated_run_dir()  # must not raise


class TestNoRealRunDirWritesSoFar(RunDirIsolationTestCase):
    def test_no_test_has_written_inside_the_real_run_dir(self):
        writes = tests_package.real_run_dir_writes()
        self.assertEqual(
            [], writes,
            "the suite wrote inside {!s}: {!r}".format(self.real, writes),
        )


class TestWholeSuiteRunDirAudit(RunDirIsolationTestCase):
    """Empty audit log after the whole suite == the real run dir is unchanged by it.

    The log is cumulative for the interpreter and records writes by *this* process
    only, so the assertion holds even while a live ``paperbot.py run`` keeps
    rewriting its own ``paper_state.json`` in the same directory.
    """

    def test_the_whole_suite_left_the_real_run_dir_alone(self):
        writes = tests_package.real_run_dir_writes()
        self.assertEqual(
            [], writes,
            "the test suite wrote inside the real run directory {!s}: {!r}".format(
                self.real, writes),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
