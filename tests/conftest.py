"""tests 共通の fixture 定義。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from nemucast.state import save_state


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
