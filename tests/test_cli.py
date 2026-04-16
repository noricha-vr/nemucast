"""cli モジュール（ロギング設定 / スケジュールプロファイル / config 定数）のテスト"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from nemucast import config as config_module
from nemucast.cli import build_schedule_defaults, run_with_args, setup_logging


def test_setup_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ロギング設定のテスト"""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.chdir(tmp_path)
    logging.getLogger().handlers = []

    setup_logging()

    assert logging.getLogger().level == logging.DEBUG
    assert (tmp_path / "logs").exists()


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
def test_build_schedule_defaults(profile_name: str, expected: dict) -> None:
    """スケジュール別プロファイルの既定値"""
    defaults = build_schedule_defaults(profile_name)

    for key, value in expected.items():
        assert defaults[key] == value


def test_build_schedule_defaults_unknown_profile() -> None:
    """未知のプロファイル名は ValueError"""
    with pytest.raises(ValueError, match="未知のスケジュールプロファイル"):
        build_schedule_defaults("unknown-profile")


def test_build_schedule_defaults_returns_independent_copy() -> None:
    """返り値を変更しても SCHEDULE_PROFILES 側は汚染されない"""
    defaults = build_schedule_defaults("cron-20")
    defaults["interval"] = -1

    assert config_module.SCHEDULE_PROFILES["cron-20"]["interval"] != -1


def test_schedule_profiles_contains_expected_keys() -> None:
    """SCHEDULE_PROFILES には既知のプロファイルと必要なキーが揃っている"""
    required_keys = {
        "name",
        "interval",
        "step",
        "min_level",
        "inactive_threshold",
        "state_file",
        "run_until_standby",
    }

    assert set(config_module.SCHEDULE_PROFILES.keys()) == {"cron-20", "cron-0030"}
    for profile in config_module.SCHEDULE_PROFILES.values():
        assert required_keys <= set(profile.keys())


def test_log_dir_default() -> None:
    """LOG_DIR のデフォルトは 'logs'"""
    assert config_module.LOG_DIR == "logs"


def test_standby_wait_sec_default() -> None:
    """STANDBY_WAIT_SEC のデフォルトは 2 秒"""
    assert config_module.STANDBY_WAIT_SEC == 2


def test_log_rotation_defaults() -> None:
    """ログローテーション設定のデフォルトは 512KB / backup 1"""
    assert config_module.LOG_ROTATION_MAX_BYTES == 512 * 1024
    assert config_module.LOG_ROTATION_BACKUP_COUNT == 1


class TestRunWithArgs:
    """run_with_args の主要パスのテスト"""

    @patch("nemucast.cli.stop_discovery")
    @patch("nemucast.cli.run_volume_session", return_value="volume_down")
    @patch("nemucast.cli.discover_chromecasts")
    def test_run_with_args_happy_path(
        self,
        mock_discover: Mock,
        mock_run_session: Mock,
        mock_stop_discovery: Mock,
        tmp_path: Path,
    ) -> None:
        """正常系: discover → run_volume_session → stop_discovery が呼ばれる"""
        mock_cast = Mock()
        mock_cast.cast_info.friendly_name = "Living Room"
        mock_cast.cast_info.host = "192.168.1.2"
        mock_browser = Mock()
        mock_discover.return_value = (mock_cast, mock_browser)

        state_file = tmp_path / "activity_state.json"
        run_with_args(
            args=["--name", "Living Room", "--state-file", str(state_file)],
        )

        mock_cast.wait.assert_called_once()
        mock_run_session.assert_called_once()
        mock_stop_discovery.assert_called_once_with(mock_browser)

    @patch("nemucast.cli.stop_discovery")
    @patch("nemucast.cli.discover_chromecasts")
    def test_run_with_args_exits_when_chromecast_missing(
        self,
        mock_discover: Mock,
        mock_stop_discovery: Mock,
    ) -> None:
        """Chromecast 未検出時は SystemExit(1) で browser も停止する"""
        mock_browser = Mock()
        mock_discover.return_value = (None, mock_browser)

        with pytest.raises(SystemExit) as exc_info:
            run_with_args(args=[])

        assert exc_info.value.code == 1
        mock_stop_discovery.assert_called_once_with(mock_browser)

    @patch("nemucast.cli.stop_discovery")
    @patch("nemucast.cli.run_volume_session", side_effect=KeyboardInterrupt)
    @patch("nemucast.cli.discover_chromecasts")
    def test_run_with_args_keyboard_interrupt_preserves_state(
        self,
        mock_discover: Mock,
        mock_run_session: Mock,
        mock_stop_discovery: Mock,
        tmp_path: Path,
    ) -> None:
        """KeyboardInterrupt 時は state を残して再 raise する"""
        mock_cast = Mock()
        mock_cast.cast_info.friendly_name = "Living Room"
        mock_cast.cast_info.host = "192.168.1.2"
        mock_browser = Mock()
        mock_discover.return_value = (mock_cast, mock_browser)

        state_file = tmp_path / "activity_state.json"
        state_file.write_text("{}", encoding="utf-8")

        with pytest.raises(KeyboardInterrupt):
            run_with_args(
                args=["--name", "Living Room", "--state-file", str(state_file)],
            )

        # KeyboardInterrupt では state を削除しない（ユーザーの意図的な中断のため）
        assert state_file.exists()
        mock_stop_discovery.assert_called_once_with(mock_browser)

    @patch("nemucast.cli.stop_discovery")
    @patch("nemucast.cli.run_volume_session", side_effect=RuntimeError("boom"))
    @patch("nemucast.cli.discover_chromecasts")
    def test_run_with_args_clears_state_on_exception(
        self,
        mock_discover: Mock,
        mock_run_session: Mock,
        mock_stop_discovery: Mock,
        tmp_path: Path,
    ) -> None:
        """予期せぬ例外時は state を削除して SystemExit(1)"""
        mock_cast = Mock()
        mock_cast.cast_info.friendly_name = "Living Room"
        mock_cast.cast_info.host = "192.168.1.2"
        mock_browser = Mock()
        mock_discover.return_value = (mock_cast, mock_browser)

        state_file = tmp_path / "activity_state.json"
        state_file.write_text("{}", encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            run_with_args(
                args=["--name", "Living Room", "--state-file", str(state_file)],
            )

        assert exc_info.value.code == 1
        assert not state_file.exists()
        mock_stop_discovery.assert_called_once_with(mock_browser)
