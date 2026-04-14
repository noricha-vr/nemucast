"""コマンドライン引数のテスト"""

import pytest

import nemucast.main as main_module
from nemucast.main import parse_args


def test_parse_args_default():
    """デフォルト引数のテスト"""
    args = parse_args([])
    assert args.interval == main_module.DEFAULT_INTERVAL_SEC
    assert args.name == main_module.CHROMECAST_NAME
    assert args.step == main_module.STEP
    assert args.min_level == main_module.MIN_LEVEL
    assert args.inactive_threshold == main_module.INACTIVE_THRESHOLD
    assert args.manual_rise_threshold == main_module.MANUAL_RISE_THRESHOLD
    assert args.state_file == main_module.Path(main_module.DEFAULT_STATE_FILE)
    assert args.run_until_standby is main_module.RUN_UNTIL_STANDBY


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
    assert args.state_file == main_module.Path("tmp/state.json")
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
    assert args.state_file == main_module.Path("logs/activity_state_0030.json")
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
