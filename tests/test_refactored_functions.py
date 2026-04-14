"""主要関数のテスト"""

import json
import logging
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nemucast.main import (  # noqa: E402
    append_history,
    build_schedule_defaults,
    calculate_next_volume,
    clear_state,
    create_initial_state,
    detect_manual_activity,
    discover_chromecasts,
    is_state_stale,
    load_state,
    run_volume_session,
    run_volume_tick,
    save_state,
    setup_logging,
    standby_device,
    stop_discovery,
)


class TestRefactoredFunctions:
    """主要関数のテストクラス"""

    def test_setup_logging(self, tmp_path, monkeypatch):
        """ロギング設定のテスト"""
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.chdir(tmp_path)
        logging.getLogger().handlers = []

        setup_logging()

        assert logging.getLogger().level == logging.DEBUG
        assert (tmp_path / "logs").exists()

    def test_discover_chromecasts_found(self):
        """Chromecast検索のテスト（デバイスが見つかった場合）"""
        mock_cast = Mock()
        mock_cast.cast_info.friendly_name = "TestDevice"
        mock_browser = Mock()

        with patch("pychromecast.get_chromecasts", return_value=([mock_cast], mock_browser)):
            cast, browser = discover_chromecasts("TestDevice")

        assert cast == mock_cast
        assert browser == mock_browser

    def test_discover_chromecasts_not_found(self):
        """Chromecast検索のテスト（デバイスが見つからない場合）"""
        mock_cast = Mock()
        mock_cast.cast_info.friendly_name = "OtherDevice"
        mock_browser = Mock()

        with patch("pychromecast.get_chromecasts", return_value=([mock_cast], mock_browser)):
            cast, browser = discover_chromecasts("TestDevice")

        assert cast is None
        assert browser == mock_browser

    def test_stop_discovery_prefers_browser_method(self):
        """browser.stop_discovery があればそれを使う"""
        mock_browser = Mock()

        stop_discovery(mock_browser)

        mock_browser.stop_discovery.assert_called_once()

    def test_load_state_round_trip(self, tmp_path):
        """state の保存と読込"""
        state_file = tmp_path / "state.json"
        state = create_initial_state("Living Room", 0.55, 1234.0)

        save_state(state_file, state)
        loaded = load_state(state_file)

        assert loaded == state

    def test_load_state_missing(self, tmp_path):
        """state がなければ None"""
        assert load_state(tmp_path / "missing.json") is None

    def test_load_state_invalid_json_raises(self, tmp_path):
        """壊れた JSON は fail fast"""
        state_file = tmp_path / "state.json"
        state_file.write_text("{broken", encoding="utf-8")

        with pytest.raises(RuntimeError):
            load_state(state_file)

    def test_clear_state(self, tmp_path):
        """state ファイル削除"""
        state_file = tmp_path / "state.json"
        state_file.write_text("{}", encoding="utf-8")

        clear_state(state_file)

        assert not state_file.exists()

    def test_is_state_stale_with_old_timestamp(self):
        """古い state はリセット対象"""
        state = create_initial_state("Living Room", 0.4, 100.0)
        assert is_state_stale(state, "Living Room", interval_sec=30, now_ts=200.0) is True

    def test_is_state_stale_with_different_device(self):
        """別デバイスの state はリセット対象"""
        state = create_initial_state("Bedroom", 0.4, 100.0)
        assert is_state_stale(state, "Living Room", interval_sec=30, now_ts=120.0) is True

    def test_detect_manual_activity(self):
        """前回自動設定音量より上がっていれば手動操作扱い"""
        assert detect_manual_activity(0.55, 0.5, 0.01) is True
        assert detect_manual_activity(0.5, 0.5, 0.01) is False
        assert detect_manual_activity(0.49, 0.5, 0.01) is False
        assert detect_manual_activity(0.505, 0.5, 0.01) is False

    def test_append_history_keeps_latest_entries(self):
        """履歴は最大件数まで保持する"""
        state = {"history": []}

        for i in range(25):
            append_history(state, {"index": i})

        assert len(state["history"]) == 20
        assert state["history"][0]["index"] == 5
        assert state["history"][-1]["index"] == 24

    def test_calculate_next_volume(self):
        """音量計算のテスト"""
        assert calculate_next_volume(0.6, -0.04, 0.3) == 0.56
        assert calculate_next_volume(0.31, -0.04, 0.3) == 0.3
        assert calculate_next_volume(0.2, -0.04, 0.3) == 0.2

    def test_standby_device(self):
        """スタンバイ移行のテスト"""
        mock_cast = Mock()

        with patch("nemucast.main.time.sleep") as mock_sleep:
            standby_device(mock_cast)

        mock_cast.quit_app.assert_called_once()
        mock_sleep.assert_called_once_with(2)

    def test_run_volume_tick_detects_manual_raise_and_resets_streak(self, tmp_path):
        """手動で音量が上がっていれば streak をリセットして継続"""
        state_file = tmp_path / "state.json"
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
        mock_cast = Mock()
        mock_cast.status.volume_level = 0.5

        with patch("nemucast.main.time.time", return_value=110.0):
            result = run_volume_tick(
                cast=mock_cast,
                interval_sec=60,
                step=-0.04,
                min_level=0.3,
                inactive_threshold=3,
                manual_rise_threshold=0.01,
                state_file=state_file,
                device_name="Living Room",
            )

        saved = load_state(state_file)
        assert result == "volume_down"
        assert saved["inactive_streak"] == 0
        assert saved["last_auto_volume"] == 0.46
        assert saved["history"][-1]["manual_raise_detected"] is True
        mock_cast.set_volume.assert_called_once_with(0.46)

    def test_run_volume_tick_reaches_inactive_threshold_and_standby(self, tmp_path):
        """しきい値に達したら state を消して standby"""
        state_file = tmp_path / "state.json"
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
        mock_cast = Mock()
        mock_cast.status.volume_level = 0.4

        with patch("nemucast.main.time.time", return_value=110.0), patch(
            "nemucast.main.time.sleep"
        ):
            result = run_volume_tick(
                cast=mock_cast,
                interval_sec=60,
                step=-0.04,
                min_level=0.3,
                inactive_threshold=3,
                manual_rise_threshold=0.01,
                state_file=state_file,
                device_name="Living Room",
            )

        assert result == "standby"
        assert not state_file.exists()
        mock_cast.quit_app.assert_called_once()
        mock_cast.set_volume.assert_not_called()

    def test_run_volume_tick_resets_stale_state(self, tmp_path):
        """古い state は新しいセッションとして扱う"""
        state_file = tmp_path / "state.json"
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
        mock_cast = Mock()
        mock_cast.status.volume_level = 0.6

        with patch("nemucast.main.time.time", return_value=200.0):
            result = run_volume_tick(
                cast=mock_cast,
                interval_sec=60,
                step=-0.04,
                min_level=0.3,
                inactive_threshold=3,
                manual_rise_threshold=0.01,
                state_file=state_file,
                device_name="Living Room",
            )

        saved = load_state(state_file)
        assert result == "volume_down"
        assert saved["inactive_streak"] == 1
        assert saved["last_auto_volume"] == 0.56

    def test_run_volume_tick_keeps_volume_when_at_min_level(self, tmp_path):
        """最小音量以下なら据え置きで state だけ進める"""
        state_file = tmp_path / "state.json"
        mock_cast = Mock()
        mock_cast.status.volume_level = 0.25

        with patch("nemucast.main.time.time", return_value=100.0):
            result = run_volume_tick(
                cast=mock_cast,
                interval_sec=60,
                step=-0.04,
                min_level=0.3,
                inactive_threshold=3,
                manual_rise_threshold=0.01,
                state_file=state_file,
                device_name="Living Room",
            )

        saved = load_state(state_file)
        assert result == "keep"
        assert saved["inactive_streak"] == 1
        assert saved["last_auto_volume"] == 0.25
        mock_cast.set_volume.assert_not_called()

    def test_state_file_is_json_serializable(self, tmp_path):
        """保存される state は JSON として読める"""
        state_file = tmp_path / "state.json"
        state = create_initial_state("Living Room", 0.55, 1234.0)
        append_history(state, {"action": "volume_down", "applied_volume": 0.51})

        save_state(state_file, state)
        loaded = json.loads(state_file.read_text(encoding="utf-8"))

        assert loaded["device_name"] == "Living Room"
        assert loaded["history"][0]["action"] == "volume_down"

    def test_run_volume_session_one_shot(self):
        """1回実行モードでは sleep せずに1回だけ tick する"""
        mock_cast = Mock()

        with patch("nemucast.main.run_volume_tick", return_value="volume_down") as mock_tick, patch(
            "nemucast.main.time.sleep"
        ) as mock_sleep:
            result = run_volume_session(
                cast=mock_cast,
                interval_sec=900,
                step=-0.04,
                min_level=0.3,
                inactive_threshold=4,
                manual_rise_threshold=0.01,
                state_file=Path("logs/state.json"),
                device_name="Living Room",
                run_until_standby=False,
            )

        assert result == "volume_down"
        assert mock_tick.call_count == 1
        mock_sleep.assert_not_called()

    def test_run_volume_session_until_standby(self):
        """standby 到達まで interval ごとに繰り返す"""
        mock_cast = Mock()

        with patch(
            "nemucast.main.run_volume_tick",
            side_effect=["volume_down", "keep", "standby"],
        ) as mock_tick, patch("nemucast.main.time.sleep") as mock_sleep:
            result = run_volume_session(
                cast=mock_cast,
                interval_sec=900,
                step=-0.04,
                min_level=0.35,
                inactive_threshold=4,
                manual_rise_threshold=0.01,
                state_file=Path("logs/activity_state_0030.json"),
                device_name="Dell",
                run_until_standby=True,
            )

        assert result == "standby"
        assert mock_tick.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(900)

    @pytest.mark.parametrize(
        ("profile_name", "expected"),
        [
            (
                "cron-20",
                {
                    "name": "Dell",
                    "interval": 60,
                    "inactive_threshold": 1,
                    "state_file": "logs/activity_state_20.json",
                    "run_until_standby": True,
                },
            ),
            (
                "cron-0030",
                {
                    "name": "Dell",
                    "interval": 900,
                    "inactive_threshold": 4,
                    "state_file": "logs/activity_state_0030.json",
                    "run_until_standby": True,
                },
            ),
        ],
    )
    def test_build_schedule_defaults(self, profile_name, expected):
        """スケジュール別プロファイルの既定値"""
        defaults = build_schedule_defaults(profile_name)

        for key, value in expected.items():
            assert defaults[key] == value
