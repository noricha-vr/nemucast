"""main 周辺のテスト"""

from unittest.mock import Mock, patch

import pytest

from nemucast.cli import main


class TestMainFlow:
    """main のテストクラス"""

    @patch("nemucast.cli.discover_chromecasts")
    def test_main_exits_when_chromecast_missing(self, mock_discover):
        """Chromecastが見つからない場合は終了コード1"""
        mock_browser = Mock()
        mock_discover.return_value = (None, mock_browser)

        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["nemucast"]):
                main()

        assert exc_info.value.code == 1
        mock_browser.stop_discovery.assert_called_once()

    @patch("nemucast.cli.run_volume_session", side_effect=RuntimeError("boom"))
    @patch("nemucast.cli.discover_chromecasts")
    def test_main_clears_state_when_tick_fails(
        self,
        mock_discover,
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
        mock_discover.return_value = (mock_cast, mock_browser)

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
