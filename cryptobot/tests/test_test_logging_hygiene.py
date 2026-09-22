"""The test package must scope its logging silencing, never disable it globally.

Regression for the removed ``logging.disable(CRITICAL)`` call at import time,
which silenced every logger in any process that imported ``cryptobot.tests``
(for example ``scripts/capture_evidence.py``, which imports the fixtures).
"""

from __future__ import annotations

import logging
import os
import unittest
from unittest import mock

from cryptobot import tests as tests_package


class TestPackageLoggingHygiene(unittest.TestCase):
    def test_importing_the_suite_does_not_disable_logging_globally(self):
        self.assertEqual(logging.getLogger().manager.disable, logging.NOTSET)

    def test_unrelated_loggers_still_emit_records(self):
        records: list = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        logger = logging.getLogger("external.library")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        logger.info("third-party diagnostics must survive importing the suite")
        self.assertEqual(len(records), 1)

    def test_cryptobot_logger_is_scoped_quiet_unless_opted_in(self):
        propagate = logging.getLogger("cryptobot").propagate
        if os.environ.get("CRYPTOBOT_TEST_LOG") == "1":
            self.assertTrue(propagate, "the opt-in must keep logs flowing")
        else:
            self.assertFalse(propagate, "silencing must be scoped to the cryptobot logger")

    def test_silencing_helper_is_reversible(self):
        tests_package.restore_cryptobot_logging()
        self.addCleanup(tests_package.quiet_cryptobot_logging)
        self.assertTrue(logging.getLogger("cryptobot").propagate)
        tests_package.quiet_cryptobot_logging()
        self.assertFalse(logging.getLogger("cryptobot").propagate)

    def test_ambient_configuration_overrides_are_ignored_by_the_suite(self):
        """A real notification topic in the environment must not leak into tests."""
        with mock.patch.dict(os.environ, {"CRYPTOBOT_NTFY_TOPIC": "real-topic",
                                          "CRYPTOBOT_CACHE_DIR": "C:/real/cache"}):
            tests_package.isolate_environment()
            self.assertNotIn("CRYPTOBOT_NTFY_TOPIC", os.environ)
            self.assertNotIn("CRYPTOBOT_CACHE_DIR", os.environ)

    def test_the_opt_in_variable_survives_isolation(self):
        with mock.patch.dict(os.environ, {"CRYPTOBOT_TEST_LOG": "1"}):
            tests_package.isolate_environment()
            self.assertEqual(os.environ.get("CRYPTOBOT_TEST_LOG"), "1")
