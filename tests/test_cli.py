"""cli モジュール（ロギング設定 / cron プロファイル / config 定数）のテスト"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from nemucast import config as config_module
from nemucast.cli import run_with_args, setup_logging
from nemucast.volume import VolumeSessionConfig


def test_setup_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ロギング設定のテスト"""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.chdir(tmp_path)
    logging.getLogger().handlers = []

    setup_logging()

    assert logging.getLogger().level == logging.DEBUG
    assert (tmp_path / "logs").exists()


def test_cron_20_overrides_defaults() -> None:
    """cron-20 プロファイルの既定値"""
    assert config_module.CRON_20_OVERRIDES["name"] == "Dell"
    assert config_module.CRON_20_OVERRIDES["interval"] == 60
    assert config_module.CRON_20_OVERRIDES["inactive_threshold"] == 1
    assert config_module.CRON_20_OVERRIDES["state_file"] == "logs/activity_state_20.json"
    assert config_module.CRON_20_OVERRIDES["run_until_standby"] is True


def test_cron_0030_overrides_defaults() -> None:
    """cron-0030 プロファイルの既定値"""
    assert config_module.CRON_0030_OVERRIDES["name"] == "Dell"
    assert config_module.CRON_0030_OVERRIDES["interval"] == 900
    assert config_module.CRON_0030_OVERRIDES["inactive_threshold"] == 4
    assert config_module.CRON_0030_OVERRIDES["state_file"] == "logs/activity_state_0030.json"
    assert config_module.CRON_0030_OVERRIDES["run_until_standby"] is True


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
    @patch("nemucast.cli.run_volume_session")
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
            args=[
                "--name",
                "Living Room",
                "--state-file",
                str(state_file),
                "--interval",
                "600",
                "--step",
                "-0.05",
                "--min-level",
                "0.2",
                "--inactive-threshold",
                "4",
                "--manual-rise-threshold",
                "0.03",
                "--run-until-standby",
            ],
        )

        mock_cast.wait.assert_called_once()
        mock_run_session.assert_called_once()
        mock_stop_discovery.assert_called_once_with(mock_browser)

        # 解析された引数が VolumeSessionConfig に正しくマップされる
        config = mock_run_session.call_args.kwargs["config"]
        assert isinstance(config, VolumeSessionConfig)
        assert config.interval_sec == 600
        assert config.device_name == "Living Room"
        assert config.step == -0.05
        assert config.min_level == 0.2
        assert config.inactive_threshold == 4
        assert config.manual_rise_threshold == 0.03
        assert config.state_file == state_file
        assert config.run_until_standby is True

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
