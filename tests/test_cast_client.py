"""cast_client モジュール（Chromecast discover / standby）のテスト"""

from __future__ import annotations

from unittest.mock import Mock, patch

from nemucast.cast_client import (
    discover_chromecasts,
    standby_device,
    stop_discovery,
)


def test_discover_chromecasts_found() -> None:
    """Chromecast検索のテスト（デバイスが見つかった場合）"""
    mock_cast = Mock()
    mock_cast.cast_info.friendly_name = "TestDevice"
    mock_browser = Mock()

    with patch("pychromecast.get_chromecasts", return_value=([mock_cast], mock_browser)):
        cast, browser = discover_chromecasts("TestDevice")

    assert cast == mock_cast
    assert browser == mock_browser


def test_discover_chromecasts_not_found() -> None:
    """Chromecast検索のテスト（デバイスが見つからない場合）"""
    mock_cast = Mock()
    mock_cast.cast_info.friendly_name = "OtherDevice"
    mock_browser = Mock()

    with patch("pychromecast.get_chromecasts", return_value=([mock_cast], mock_browser)):
        cast, browser = discover_chromecasts("TestDevice")

    assert cast is None
    assert browser == mock_browser


def test_stop_discovery_prefers_browser_method() -> None:
    """browser.stop_discovery があればそれを使う"""
    mock_browser = Mock()

    stop_discovery(mock_browser)

    mock_browser.stop_discovery.assert_called_once()


def test_standby_device() -> None:
    """スタンバイ移行のテスト"""
    mock_cast = Mock()

    with patch("nemucast.cast_client.time.sleep") as mock_sleep:
        standby_device(mock_cast)

    mock_cast.quit_app.assert_called_once()
    mock_sleep.assert_called_once_with(2)
