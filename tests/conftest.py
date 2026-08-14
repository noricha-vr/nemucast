"""tests 共通の fixture 定義。"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from nemucast.state import save_state
from nemucast.volume import VolumeSessionConfig


@pytest.fixture
def fake_network() -> Callable[[list[str]], Any]:
    """指定した名前の Chromecast が LAN 上にある状態を再現するファクトリ fixture。

    差し替えるのは pychromecast / zeroconf（プロセス外境界）のみで、
    nemucast 内部の関数は patch しない。with 文が返す dict の
    casts / browser / zeroconf で呼び出し内容を検証できる。
    """

    @contextmanager
    def _fake_network(device_names: list[str]) -> Generator[dict[str, Any]]:
        browser = Mock()
        browser.devices = {}
        casts: dict[str, Mock] = {}
        for index, name in enumerate(device_names):
            cast = Mock()
            cast.cast_info.friendly_name = name
            casts[name] = cast
            browser.devices[index] = cast.cast_info

        def get_listed(friendly_names: list[str], **_kwargs: Any) -> tuple[list[Mock], Mock]:
            return [casts[name] for name in friendly_names if name in casts], browser

        with (
            patch("pychromecast.get_listed_chromecasts", side_effect=get_listed) as get_listed_mock,
            patch("zeroconf.Zeroconf") as zeroconf_mock,
        ):
            yield {
                "browser": browser,
                "casts": casts,
                "get_listed": get_listed_mock,
                "zeroconf": zeroconf_mock,
            }

    return _fake_network


@pytest.fixture
def mock_cast() -> Mock:
    """デフォルトの音量 0.5 を持つ Chromecast mock。"""
    cast = Mock()
    cast.status.volume_level = 0.5
    cast.name = "Living Room"
    return cast


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    """テスト用の state ファイルパス（tmp_path 配下）。"""
    return tmp_path / "state.json"


@pytest.fixture
def seeded_state(state_file: Path) -> Path:
    """初期 state を書き込んだ state ファイル。

    device_name=Living Room, last_auto_volume=0.4,
    inactive_streak=2, updated_at=100.0。
    """
    save_state(
        state_file,
        {
            "device_name": "Living Room",
            "last_auto_volume": 0.4,
            "inactive_streak": 2,
            "updated_at": 100.0,
            "history": [],
        },
    )
    return state_file


@pytest.fixture
def make_tick_config(state_file: Path):
    """run_volume_tick テスト用の VolumeSessionConfig ファクトリ。"""

    def _make(**overrides: Any) -> VolumeSessionConfig:
        defaults = dict(
            interval_sec=60,
            step=-0.04,
            min_level=0.3,
            inactive_threshold=3,
            manual_rise_threshold=0.01,
            state_file=state_file,
            device_name="Living Room",
            run_until_standby=False,
        )
        defaults.update(overrides)
        return VolumeSessionConfig(**defaults)

    return _make
