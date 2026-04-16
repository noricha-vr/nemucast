"""コマンドライン引数のテスト"""

import argparse
import typing
from pathlib import Path

import pytest

from nemucast import cli as cli_module
from nemucast import config as config_module
from nemucast.cli import parse_args
from nemucast.volume import VolumeSessionConfig


def test_parse_args_default():
    """デフォルト引数のテスト"""
    args = parse_args([])
    assert args.interval == config_module.DEFAULT_INTERVAL_SEC
    assert args.name == config_module.CHROMECAST_NAME
    assert args.step == config_module.STEP
    assert args.min_level == config_module.MIN_LEVEL
    assert args.inactive_threshold == config_module.INACTIVE_THRESHOLD
    assert args.manual_rise_threshold == config_module.MANUAL_RISE_THRESHOLD
    assert args.state_file == Path(config_module.DEFAULT_STATE_FILE)
    assert args.run_until_standby is config_module.RUN_UNTIL_STANDBY


def test_parse_args_custom():
    """カスタム引数のテスト"""
    args = parse_args(
        [
            "--interval",
            "600",
            "--name",
            "Living Room",
            "--step",
            "-0.05",
            "--min-level",
            "0.2",
            "--inactive-threshold",
            "4",
            "--manual-rise-threshold",
            "0.03",
            "--state-file",
            "tmp/state.json",
            "--run-until-standby",
        ]
    )
    assert args.interval == 600
    assert args.name == "Living Room"
    assert args.step == -0.05
    assert args.min_level == 0.2
    assert args.inactive_threshold == 4
    assert args.manual_rise_threshold == 0.03
    assert args.state_file == Path("tmp/state.json")
    assert args.run_until_standby is True


def test_parse_args_with_overrides():
    """スケジュール用デフォルト上書きのテスト"""
    args = parse_args(
        [],
        default_overrides={
            "interval": 900,
            "name": "Dell",
            "inactive_threshold": 4,
            "state_file": "logs/activity_state_0030.json",
            "run_until_standby": True,
        },
    )

    assert args.interval == 900
    assert args.name == "Dell"
    assert args.inactive_threshold == 4
    assert args.state_file == Path("logs/activity_state_0030.json")
    assert args.run_until_standby is True


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--interval", "0"], "--interval は正の整数を指定してください"),
        (["--step", "0.1"], "--step は負の値を指定してください"),
        (["--min-level", "1.2"], "--min-level は 0.0 から 1.0 の範囲で指定してください"),
        (["--inactive-threshold", "0"], "--inactive-threshold は正の整数を指定してください"),
        (
            ["--manual-rise-threshold", "-0.1"],
            "--manual-rise-threshold は 0 以上を指定してください",
        ),
    ],
)
def test_parse_args_invalid(argv, message, capsys):
    """不正な引数はエラーになる"""
    with pytest.raises(SystemExit):
        parse_args(argv)

    captured = capsys.readouterr()
    assert message in captured.err


def test_cli_module_exposes_entrypoints():
    """後方互換の sanity check: CLI エントリが cli モジュールから参照できる"""
    assert callable(cli_module.main)
    assert callable(cli_module.main_cron_20)
    assert callable(cli_module.main_cron_0030)


def test_parse_args_return_annotation():
    """parse_args の戻り値型は argparse.Namespace"""
    hints = typing.get_type_hints(parse_args)
    assert hints["return"] is argparse.Namespace


def test_build_session_config_maps_all_fields():
    """解析済み引数が VolumeSessionConfig に正しくマップされる"""
    parsed = parse_args(
        [
            "--interval",
            "600",
            "--name",
            "Living Room",
            "--step",
            "-0.05",
            "--min-level",
            "0.2",
            "--inactive-threshold",
            "4",
            "--manual-rise-threshold",
            "0.03",
            "--state-file",
            "tmp/state.json",
            "--run-until-standby",
        ]
    )

    config = cli_module._build_session_config(parsed)

    assert isinstance(config, VolumeSessionConfig)
    assert config.interval_sec == 600
    assert config.device_name == "Living Room"
    assert config.step == -0.05
    assert config.min_level == 0.2
    assert config.inactive_threshold == 4
    assert config.manual_rise_threshold == 0.03
    assert config.state_file == Path("tmp/state.json")
    assert config.run_until_standby is True
