"""main 周辺のテスト"""

from unittest.mock import patch

import pytest

from nemucast.cli import main


class TestMainFlow:
    """main のテストクラス"""

    def test_main_exits_when_chromecast_missing(self, fake_network):
        """Chromecastが見つからない場合は終了コード1"""
        with fake_network(["OtherDevice"]) as network:
            with pytest.raises(SystemExit) as exc_info:
                with patch("sys.argv", ["nemucast"]):
                    main()

        assert exc_info.value.code == 1
        network["browser"].stop_discovery.assert_called_once()

    @patch("nemucast.cli.run_volume_session", side_effect=RuntimeError("boom"))
    def test_main_clears_state_when_tick_fails(
        self,
        mock_run_volume_session,
        fake_network,
        tmp_path,
    ):
        """tick失敗時は state を削除して終了"""
        state_file = tmp_path / "activity_state.json"
        state_file.write_text("{}", encoding="utf-8")

        with fake_network(["Living Room"]) as network:
            with pytest.raises(SystemExit) as exc_info:
                with patch(
                    "sys.argv",
                    ["nemucast", "--name", "Living Room", "--state-file", str(state_file)],
                ):
                    main()

        assert exc_info.value.code == 1
        assert not state_file.exists()
        network["casts"]["Living Room"].wait.assert_called_once()
        mock_run_volume_session.assert_called_once()
