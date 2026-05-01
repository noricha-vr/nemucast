"""standby スクリプト（即時 quit_app）のテスト"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from nemucast.standby import main


class TestStandby:
    @patch("nemucast.standby.stop_discovery")
    @patch("nemucast.standby.standby_device")
    @patch("nemucast.standby.discover_chromecasts")
    def test_main_invokes_standby(
        self,
        mock_discover: Mock,
        mock_standby: Mock,
        mock_stop: Mock,
    ) -> None:
        mock_cast = Mock()
        mock_browser = Mock()
        mock_discover.return_value = (mock_cast, mock_browser)

        main(args=["--name", "Living Room"])

        mock_discover.assert_called_once_with("Living Room")
        mock_cast.wait.assert_called_once()
        mock_standby.assert_called_once_with(mock_cast)
        mock_stop.assert_called_once_with(mock_browser)

    @patch("nemucast.standby.stop_discovery")
    @patch("nemucast.standby.standby_device")
    @patch("nemucast.standby.discover_chromecasts")
    def test_main_exits_when_chromecast_missing(
        self,
        mock_discover: Mock,
        mock_standby: Mock,
        mock_stop: Mock,
    ) -> None:
        mock_browser = Mock()
        mock_discover.return_value = (None, mock_browser)

        with pytest.raises(SystemExit) as exc_info:
            main(args=["--name", "Missing"])

        assert exc_info.value.code == 1
        mock_standby.assert_not_called()
        mock_stop.assert_called_once_with(mock_browser)
