"""Configuration: defaults, override precedence, validation and the live-mode guard."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cryptobot import safety
from cryptobot.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    apply_env_overrides,
    assert_no_live_flags,
    assert_no_secrets_in_config,
    load_config,
    validate,
)
from cryptobot.config import DEFAULTS


class TestDefaults(unittest.TestCase):
    def test_shipped_config_matches_the_documented_starting_point(self):
        config = load_config(environ={})
        self.assertEqual(config.mode, "paper")
        self.assertEqual(config.initial_capital_usdt, 50.0)
        self.assertEqual(config.net_profit_target_pct, 2.0)
        self.assertEqual(list(config.pairs), ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(config.timeframe, "1h")
        self.assertEqual(config.fee_pct, 0.1)
        self.assertEqual(config.slippage_pct, 0.05)
        self.assertEqual(config.max_open_positions, 1)
        self.assertEqual(config.daily_loss_limit_pct, 5.0)
        self.assertEqual(config.cooldown_minutes, 60.0)
        self.assertIsNone(config.gross_take_profit_pct)

    def test_derived_paths_are_absolute_inside_the_package(self):
        config = load_config(environ={})
        self.assertTrue(config.cache_dir.is_absolute())
        self.assertTrue(config.logs_dir.is_absolute())
        self.assertTrue(str(config.cache_dir).endswith("cache"))
        self.assertEqual(config.db_path.name, "ledger.sqlite")

    def test_bars_per_year_lookup(self):
        config = load_config(environ={})
        self.assertEqual(config.bars_per_year, 8760.0)
        fifteen = validate({**DEFAULTS, "timeframe": "15m"})
        self.assertEqual(fifteen.bars_per_year, 35040.0)

    def test_pair_slug(self):
        self.assertEqual(load_config(environ={}).pair_slug(), "BTCUSDT-ETHUSDT")

    def test_as_dict_is_deterministic(self):
        first = load_config(environ={}).as_dict()
        second = load_config(environ={}).as_dict()
        self.assertEqual(first, second)
        self.assertEqual(list(first["strategy"]["params"]), sorted(first["strategy"]["params"]))


class TestOverrides(unittest.TestCase):
    def test_env_override(self):
        import dataclasses

        config = load_config(environ={
            "CRYPTOBOT_TIMEFRAME": "15m",
            "CRYPTOBOT_PAIRS": "sol/usdt, btc/usdt",
            "CRYPTOBOT_NET_PROFIT_TARGET_PCT": "3.5",
            "CRYPTOBOT_MAX_OPEN_POSITIONS": "2",
            "CRYPTOBOT_MODE": "backtest",
        })
        self.assertEqual(config.timeframe, "15m")
        self.assertEqual(list(config.pairs), ["SOL/USDT", "BTC/USDT"])
        self.assertEqual(config.net_profit_target_pct, 3.5)
        self.assertEqual(config.max_open_positions, 2)
        self.assertEqual(config.mode, "backtest")

    def test_cli_override_beats_env(self):
        config = load_config(
            environ={"CRYPTOBOT_TIMEFRAME": "15m"},
            cli_overrides={"timeframe": "4h", "fee_pct": 0.075},
        )
        self.assertEqual(config.timeframe, "4h")
        self.assertEqual(config.fee_pct, 0.075)
        self.assertIn("fee_pct", config.overrides)

    def test_apply_env_overrides_is_pure(self):
        original = validate(dict(DEFAULTS))
        merged = apply_env_overrides(DEFAULTS, {"CRYPTOBOT_FEE_PCT": "0.2"})
        self.assertEqual(merged["fee_pct"], 0.2)
        self.assertEqual(DEFAULTS["fee_pct"], 0.1)
        self.assertEqual(original.fee_pct, 0.1)

    def test_override_from_a_temp_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("initial_capital_usdt: 123.0\npairs:\n  - SOL/USDT\n", encoding="utf-8")
            config = load_config(path, environ={})
            self.assertEqual(config.initial_capital_usdt, 123.0)
            self.assertEqual(list(config.pairs), ["SOL/USDT"])
            # Floats survive rounding from YAML.
            self.assertEqual(config.initial_capital_usdt, 123.0)

    def test_missing_explicit_config_is_an_error(self):
        with self.assertRaises(ConfigError):
            load_config(Path("does/not/exist.yaml"), environ={})


class TestValidation(unittest.TestCase):
    def _expect_error(self, overrides: dict, fragment: str):
        with self.assertRaises(ConfigError) as ctx:
            validate({**DEFAULTS, **overrides})
        self.assertIn(fragment, str(ctx.exception))

    def test_bad_values_are_rejected(self):
        cases = [
            ({"initial_capital_usdt": 0}, "initial_capital_usdt"),
            ({"net_profit_target_pct": 0}, "net_profit_target_pct"),
            ({"net_profit_target_pct": 99}, "net_profit_target_pct"),
            ({"stop_loss_pct": 0}, "stop_loss_pct"),
            ({"max_position_pct": 0}, "max_position_pct"),
            ({"max_position_pct": 150}, "max_position_pct"),
            ({"daily_loss_limit_pct": -1}, "daily_loss_limit_pct"),
            ({"cooldown_minutes": -5}, "cooldown_minutes"),
            ({"fee_pct": 10}, "fee_pct"),
            ({"slippage_pct": -1}, "slippage_pct"),
            ({"max_open_positions": 0}, "max_open_positions"),
            ({"max_trades_per_day": 0}, "max_trades_per_day"),
            ({"timeframe": "7m"}, "timeframe"),
            ({"pairs": []}, "pairs"),
            ({"pairs": ["BTCUSDT"]}, "invalid pair"),
            ({"gross_take_profit_pct": -1}, "gross_take_profit_pct"),
            ({"strategy": {"name": "", "params": {}}}, "strategy.name"),
            ({"logging": {"level": "CHATTY"}}, "logging.level"),
        ]
        for overrides, fragment in cases:
            with self.subTest(overrides=overrides):
                self._expect_error(overrides, fragment)

    def test_all_errors_are_reported_at_once(self):
        with self.assertRaises(ConfigError) as ctx:
            validate({**DEFAULTS, "timeframe": "9m", "fee_pct": 42})
        message = str(ctx.exception)
        self.assertIn("timeframe", message)
        self.assertIn("fee_pct", message)


class TestLiveModeGuard(unittest.TestCase):
    def test_live_mode_in_config_is_rejected(self):
        for mode in ("live", "real", "production", "trade"):
            with self.subTest(mode=mode):
                with self.assertRaises(safety.LiveTradingForbidden):
                    validate({**DEFAULTS, "mode": mode})

    def test_live_mode_via_env_is_rejected(self):
        with self.assertRaises(safety.LiveTradingForbidden):
            load_config(environ={"CRYPTOBOT_MODE": "live"})

    def test_live_kill_switch_flags_are_rejected(self):
        for name in ("CRYPTOBOT_LIVE", "CRYPTOBOT_REAL", "LIVE_TRADING"):
            with self.subTest(name=name):
                with self.assertRaises(safety.LiveTradingForbidden):
                    assert_no_live_flags({name: "1"})

    def test_falsy_kill_switch_is_fine(self):
        assert_no_live_flags({"CRYPTOBOT_LIVE": "0"})
        assert_no_live_flags({"CRYPTOBOT_LIVE": "false"})

    def test_live_mode_via_cli_override_is_rejected(self):
        with self.assertRaises(safety.LiveTradingForbidden):
            load_config(environ={}, cli_overrides={"mode": "live"})

    def test_paper_and_backtest_are_accepted(self):
        self.assertEqual(validate({**DEFAULTS, "mode": "paper"}).mode, "paper")
        self.assertEqual(validate({**DEFAULTS, "mode": "backtest"}).mode, "backtest")


class TestSecretDetection(unittest.TestCase):
    def test_secret_in_config_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("api_key: abc123\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                assert_no_secrets_in_config(path)

    def test_clean_config_passes(self):
        assert_no_secrets_in_config(DEFAULT_CONFIG_PATH)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
