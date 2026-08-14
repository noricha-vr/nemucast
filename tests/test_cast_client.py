"""cast_client モジュール（Chromecast discover / standby）のテスト"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import Mock, patch

import zeroconf

from nemucast.cast_client import (
    discover_chromecasts,
    standby_device,
    stop_discovery,
)


def test_discover_chromecasts_found(fake_network: Callable[[list[str]], Any]) -> None:
    """Chromecast検索のテスト（デバイスが見つかった場合）"""
    with fake_network(["OtherDevice", "TestDevice"]) as network:
        cast, browser = discover_chromecasts("TestDevice")

    assert cast == network["casts"]["TestDevice"]
    assert browser == network["browser"]


def test_discover_chromecasts_not_found(fake_network: Callable[[list[str]], Any]) -> None:
    """Chromecast検索のテスト（デバイスが見つからない場合）"""
    with fake_network(["OtherDevice"]) as network:
        cast, browser = discover_chromecasts("TestDevice")

    assert cast is None
    assert browser == network["browser"]


def test_discover_chromecasts_skips_loopback_interface(
    fake_network: Callable[[list[str]], Any],
) -> None:
    """loopback を bind しない zeroconf で探索する（mDNS 5353 衝突の再発防止）"""
    with fake_network(["TestDevice"]) as network:
        discover_chromecasts("TestDevice")

    network["zeroconf"].assert_called_once_with(interfaces=zeroconf.InterfaceChoice.Default)
    assert (
        network["get_listed"].call_args.kwargs["zeroconf_instance"]
        == network["zeroconf"].return_value
    )


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
