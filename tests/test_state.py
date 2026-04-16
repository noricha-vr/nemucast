"""state モジュールのテスト"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemucast.state import (
    append_history,
    clear_state,
    create_initial_state,
    detect_manual_activity,
    is_state_stale,
    load_state,
    save_state,
)


def test_load_state_round_trip(tmp_path: Path) -> None:
    """state の保存と読込"""
    state_file = tmp_path / "state.json"
    state = create_initial_state("Living Room", 0.55, 1234.0)

    save_state(state_file, state)
    loaded = load_state(state_file)

    assert loaded == state


def test_load_state_missing(tmp_path: Path) -> None:
    """state がなければ None"""
    assert load_state(tmp_path / "missing.json") is None


def test_load_state_invalid_json_raises(tmp_path: Path) -> None:
    """壊れた JSON は fail fast"""
    state_file = tmp_path / "state.json"
    state_file.write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError):
        load_state(state_file)


def test_clear_state(tmp_path: Path) -> None:
    """state ファイル削除"""
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")

    clear_state(state_file)

    assert not state_file.exists()


def test_is_state_stale_with_old_timestamp() -> None:
    """古い state はリセット対象"""
    state = create_initial_state("Living Room", 0.4, 100.0)
    assert is_state_stale(state, "Living Room", interval_sec=30, now_ts=200.0) is True


def test_is_state_stale_with_different_device() -> None:
    """別デバイスの state はリセット対象"""
    state = create_initial_state("Bedroom", 0.4, 100.0)
    assert is_state_stale(state, "Living Room", interval_sec=30, now_ts=120.0) is True


def test_detect_manual_activity() -> None:
    """前回自動設定音量より上がっていれば手動操作扱い"""
    assert detect_manual_activity(0.55, 0.5, 0.01) is True
    assert detect_manual_activity(0.5, 0.5, 0.01) is False
    assert detect_manual_activity(0.49, 0.5, 0.01) is False
    assert detect_manual_activity(0.505, 0.5, 0.01) is False


def test_append_history_keeps_latest_entries() -> None:
    """履歴は最大件数まで保持する"""
    state: dict = {"history": []}

    for i in range(25):
        append_history(state, {"index": i})

    assert len(state["history"]) == 20
    assert state["history"][0]["index"] == 5
    assert state["history"][-1]["index"] == 24


def test_state_file_is_json_serializable(tmp_path: Path) -> None:
    """保存される state は JSON として読める"""
    state_file = tmp_path / "state.json"
    state = create_initial_state("Living Room", 0.55, 1234.0)
    append_history(state, {"action": "volume_down", "applied_volume": 0.51})

    save_state(state_file, state)
    loaded = json.loads(state_file.read_text(encoding="utf-8"))

    assert loaded["device_name"] == "Living Room"
    assert loaded["history"][0]["action"] == "volume_down"
