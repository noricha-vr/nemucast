"""コマンドライン引数のテスト"""

import pytest
from nemucast.main import parse_args


def test_parse_args_default():
    """デフォルト引数のテスト"""
    args = parse_args([])
    assert args.interval == 1200
    assert args.name == "Dell"
    assert args.step == -0.04
    assert args.min_level == 0.4
    assert args.timeout == 21600


def test_parse_args_custom():
    """カスタム引数のテスト"""
    args = parse_args([
        "--interval", "600",
        "--name", "Living Room",
        "--step", "-0.05",
        "--min-level", "0.2",
        "--timeout", "3600"
    ])
    assert args.interval == 600
    assert args.name == "Living Room"
    assert args.step == -0.05
    assert args.min_level == 0.2
    assert args.timeout == 3600


def test_parse_args_short():
    """短縮形引数のテスト"""
    args = parse_args([
        "-i", "300",
        "-n", "Bedroom",
        "-s", "-0.03",
        "-m", "0.25",
        "-t", "7200"
    ])
    assert args.interval == 300
    assert args.name == "Bedroom"
    assert args.step == -0.03
    assert args.min_level == 0.25
    assert args.timeout == 7200


def test_parse_args_mixed():
    """長短混合引数のテスト"""
    args = parse_args([
        "--interval", "900",
        "-n", "Kitchen",
        "--step", "-0.02"
    ])
    assert args.interval == 900
    assert args.name == "Kitchen"
    assert args.step == -0.02
    assert args.min_level == 0.4  # デフォルト値