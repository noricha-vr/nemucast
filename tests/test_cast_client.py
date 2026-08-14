"""cast_client モジュール（Chromecast discover / standby）のテスト"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock, patch

import zeroconf

from nemucast.cast_client import (
    discover_chromecasts,
    standby_device,
    stop_discovery,
)


@contextmanager
def patched_discovery(friendly_names: list[str]) -> Generator[dict[str, Any]]:
    """指定した名前のデバイスが見つかる discovery をモックする。

    yield する dict の browser / zeroconf / casts で呼び出し内容を検証できる。
    """
    browser = Mock()
    casts = []
    for name in friendly_names:
        cast = Mock()
        cast.cast_info.friendly_name = name
        casts.append(cast)

    with (
        patch("pychromecast.discovery.discover_chromecasts") as mock_discover,
        patch("pychromecast.get_chromecast_from_cast_info", side_effect=casts) as mock_from_info,
        patch("nemucast.cast_client.zeroconf.Zeroconf") as mock_zeroconf,
    ):
        mock_discover.return_value = ([cast.cast_info for cast in casts], browser)
        yield {
            "browser": browser,
            "casts": casts,
            "discover": mock_discover,
            "from_info": mock_from_info,
            "zeroconf": mock_zeroconf,
        }


def test_discover_chromecasts_found() -> None:
    """Chromecast検索のテスト（デバイスが見つかった場合）"""
    with patched_discovery(["OtherDevice", "TestDevice"]) as mocks:
        cast, browser = discover_chromecasts("TestDevice")

    assert cast == mocks["casts"][1]
    assert browser == mocks["browser"]


def test_discover_chromecasts_not_found() -> None:
    """Chromecast検索のテスト（デバイスが見つからない場合）"""
    with patched_discovery(["OtherDevice"]) as mocks:
        cast, browser = discover_chromecasts("TestDevice")

    assert cast is None
    assert browser == mocks["browser"]


def test_discover_chromecasts_skips_loopback_interface() -> None:
    """loopback を bind しない zeroconf で discovery する（mDNS 5353 衝突の再発防止）"""
    with patched_discovery(["TestDevice"]) as mocks:
        discover_chromecasts("TestDevice")

    mocks["zeroconf"].assert_called_once_with(interfaces=zeroconf.InterfaceChoice.Default)
    assert mocks["discover"].call_args.kwargs["zeroconf_instance"] == mocks["zeroconf"].return_value


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
