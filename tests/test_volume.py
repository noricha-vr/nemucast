"""volume モジュール（TickResult / 音量計算 / run_volume_tick / run_volume_session）のテスト"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from nemucast.state import load_state
from nemucast.volume import (
    TickResult,
    VolumeSessionConfig,
    calculate_next_volume,
    lower_volume_once,
    run_volume_session,
    run_volume_tick,
)


def test_calculate_next_volume() -> None:
    """音量計算のテスト"""
    assert calculate_next_volume(0.6, -0.04, 0.3) == 0.56
    assert calculate_next_volume(0.31, -0.04, 0.3) == 0.3
    assert calculate_next_volume(0.2, -0.04, 0.3) == 0.2


def test_lower_volume_once_applies_next_volume() -> None:
    """通常ケースでは set_volume が呼ばれて戻り値が更新される"""
    mock_cast = Mock()

    result = lower_volume_once(
        cast=mock_cast,
        current_volume=0.5,
        step=-0.1,
        min_level=0.1,
    )

    assert result == 0.4
    mock_cast.set_volume.assert_called_once_with(0.4)


def test_lower_volume_once_clamps_to_min_level() -> None:
    """min_level 未満にはならない"""
    mock_cast = Mock()

    result = lower_volume_once(
        cast=mock_cast,
        current_volume=0.15,
        step=-0.1,
        min_level=0.1,
    )

    assert result == 0.1
    mock_cast.set_volume.assert_called_once_with(0.1)


def test_lower_volume_once_skips_set_volume_when_at_min_level() -> None:
    """既に最小音量なら set_volume は呼ばれない"""
    mock_cast = Mock()

    result = lower_volume_once(
        cast=mock_cast,
        current_volume=0.1,
        step=-0.1,
        min_level=0.1,
    )

    assert result == 0.1
    mock_cast.set_volume.assert_not_called()


def test_tick_result_values() -> None:
    """TickResult は state.history に残す文字列値を持つ"""
    assert TickResult.STANDBY.value == "standby"
    assert TickResult.VOLUME_DOWN.value == "volume_down"
    assert TickResult.KEEP.value == "keep"


def test_run_volume_tick_detects_manual_raise_and_resets_streak(
    seeded_state: Path, mock_cast: Mock, make_tick_config
) -> None:
    """手動で音量が上がっていれば streak をリセットして継続"""
    mock_cast.status.volume_level = 0.5

    with patch("nemucast.volume.time.time", return_value=110.0):
        result = run_volume_tick(cast=mock_cast, config=make_tick_config(state_file=seeded_state))

    saved = load_state(seeded_state)
    assert result == TickResult.VOLUME_DOWN
    assert saved["inactive_streak"] == 0
    assert saved["last_auto_volume"] == 0.46
    assert saved["history"][-1]["manual_raise_detected"] is True
    mock_cast.set_volume.assert_called_once_with(0.46)


def test_run_volume_tick_reaches_inactive_threshold_and_standby(
    seeded_state: Path, mock_cast: Mock, make_tick_config
) -> None:
    """しきい値に達したら state を消して standby"""
    mock_cast.status.volume_level = 0.4

    with (
        patch("nemucast.volume.time.time", return_value=110.0),
        patch("nemucast.cast_client.time.sleep"),
    ):
        result = run_volume_tick(cast=mock_cast, config=make_tick_config(state_file=seeded_state))

    assert result == TickResult.STANDBY
    assert not seeded_state.exists()
    mock_cast.quit_app.assert_called_once()
    mock_cast.set_volume.assert_not_called()


def test_run_volume_tick_resets_stale_state(
    state_file: Path, mock_cast: Mock, make_tick_config
) -> None:
    """古い state は新しいセッションとして扱う"""
    from nemucast.state import save_state

    save_state(
        state_file,
        {
            "device_name": "Living Room",
            "last_auto_volume": 0.7,
            "inactive_streak": 5,
            "updated_at": 0.0,
            "history": [],
        },
    )
    mock_cast.status.volume_level = 0.6

    with patch("nemucast.volume.time.time", return_value=200.0):
        result = run_volume_tick(cast=mock_cast, config=make_tick_config())

    saved = load_state(state_file)
    assert result == TickResult.VOLUME_DOWN
    assert saved["inactive_streak"] == 1
    assert saved["last_auto_volume"] == 0.56


def test_run_volume_tick_keeps_volume_when_at_min_level(
    state_file: Path, mock_cast: Mock, make_tick_config
) -> None:
    """最小音量以下なら据え置きで state だけ進める"""
    mock_cast.status.volume_level = 0.25

    with patch("nemucast.volume.time.time", return_value=100.0):
        result = run_volume_tick(cast=mock_cast, config=make_tick_config())

    saved = load_state(state_file)
    assert result == TickResult.KEEP
    assert saved["inactive_streak"] == 1
    assert saved["last_auto_volume"] == 0.25
    mock_cast.set_volume.assert_not_called()


def test_run_volume_session_one_shot() -> None:
    """1回実行モードでは sleep せずに1回だけ tick する"""
    mock_cast = Mock()
    config = VolumeSessionConfig(
        interval_sec=900,
        step=-0.04,
        min_level=0.3,
        inactive_threshold=4,
        manual_rise_threshold=0.01,
        state_file=Path("logs/state.json"),
        device_name="Living Room",
        run_until_standby=False,
    )

    with (
        patch("nemucast.volume.run_volume_tick", return_value=TickResult.VOLUME_DOWN) as mock_tick,
        patch("nemucast.volume.time.sleep") as mock_sleep,
    ):
        result = run_volume_session(cast=mock_cast, config=config)

    assert result == TickResult.VOLUME_DOWN
    assert mock_tick.call_count == 1
    mock_sleep.assert_not_called()


def test_run_volume_session_until_standby() -> None:
    """standby 到達まで interval ごとに繰り返す"""
    mock_cast = Mock()
    config = VolumeSessionConfig(
        interval_sec=900,
        step=-0.04,
        min_level=0.35,
        inactive_threshold=4,
        manual_rise_threshold=0.01,
        state_file=Path("logs/activity_state_0030.json"),
        device_name="Dell",
        run_until_standby=True,
    )

    with (
        patch(
            "nemucast.volume.run_volume_tick",
            side_effect=[TickResult.VOLUME_DOWN, TickResult.KEEP, TickResult.STANDBY],
        ) as mock_tick,
        patch("nemucast.volume.time.sleep") as mock_sleep,
    ):
        result = run_volume_session(cast=mock_cast, config=config)

    assert result == TickResult.STANDBY
    assert mock_tick.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(900)


def test_volume_session_config_is_frozen() -> None:
    """VolumeSessionConfig は不変（frozen）"""
    config = VolumeSessionConfig(
        interval_sec=900,
        step=-0.04,
        min_level=0.3,
        inactive_threshold=4,
        manual_rise_threshold=0.01,
        state_file=Path("logs/state.json"),
        device_name="Dell",
        run_until_standby=False,
    )

    with pytest.raises(Exception):
        config.interval_sec = 100  # type: ignore[misc]
