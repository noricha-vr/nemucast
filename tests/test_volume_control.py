"""main 周辺のテスト"""

from unittest.mock import Mock, patch

import pytest

from nemucast.cli import main, main_cron_0030, main_cron_20


class TestMainFlow:
    """main のテストクラス"""

    @patch("pychromecast.get_chromecasts")
    def test_main_exits_when_chromecast_missing(self, mock_get_chromecasts):
        """Chromecastが見つからない場合は終了コード1"""
        mock_browser = Mock()
        mock_get_chromecasts.return_value = ([], mock_browser)

        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["nemucast"]):
                main()

        assert exc_info.value.code == 1
        mock_browser.stop_discovery.assert_called_once()

    @patch("nemucast.cli.run_volume_session", side_effect=RuntimeError("boom"))
    @patch("pychromecast.get_chromecasts")
    def test_main_clears_state_when_tick_fails(
        self,
        mock_get_chromecasts,
        mock_run_volume_session,
        tmp_path,
    ):
        """tick失敗時は state を削除して終了"""
        state_file = tmp_path / "activity_state.json"
        state_file.write_text("{}", encoding="utf-8")

        mock_cast = Mock()
        mock_cast.cast_info.friendly_name = "Living Room"
        mock_cast.cast_info.host = "192.168.1.2"
        mock_browser = Mock()
        mock_get_chromecasts.return_value = ([mock_cast], mock_browser)

        with pytest.raises(SystemExit) as exc_info:
            with patch(
                "sys.argv",
                ["nemucast", "--name", "Living Room", "--state-file", str(state_file)],
            ):
                main()

        assert exc_info.value.code == 1
        assert not state_file.exists()
        mock_cast.wait.assert_called_once()
        mock_run_volume_session.assert_called_once()

    @patch("nemucast.cli.run_with_args")
    def test_main_cron_20_uses_schedule_defaults(self, mock_run_with_args):
        """20時プロファイルを使う"""
        main_cron_20()

        overrides = mock_run_with_args.call_args.kwargs["default_overrides"]
        assert overrides["interval"] == 60
        assert overrides["inactive_threshold"] == 1
        assert overrides["run_until_standby"] is True

    @patch("nemucast.cli.run_with_args")
    def test_main_cron_0030_uses_schedule_defaults(self, mock_run_with_args):
        """00:30プロファイルを使う"""
        main_cron_0030()

        overrides = mock_run_with_args.call_args.kwargs["default_overrides"]
        assert overrides["interval"] == 900
        assert overrides["inactive_threshold"] == 4
        assert overrides["run_until_standby"] is True
