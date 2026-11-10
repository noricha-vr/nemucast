"""cast_client モジュール（Chromecast discover / standby）のテスト"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import Mock, patch

import pytest
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


@pytest.mark.parametrize("succeeds", [True, False])
def test_discovery_retries_when_target_is_visible(
    succeeds: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一覧に目的名がある競合だけ再探索し、各 browser を一度ずつ閉じる。"""
    from nemucast import cast_client

    monkeypatch.setattr(cast_client, "DISCOVERY_MAX_RETRIES", 3)
    monkeypatch.setattr(cast_client, "DISCOVERY_RETRY_WAIT_SEC", 0.25)
    attempts = 2 if succeeds else 4
    browsers = [Mock() for _ in range(attempts)]
    for browser in browsers:
        browser.devices = {"target": Mock(friendly_name="リビングルーム")}
    target = Mock()
    results = [([], browser) for browser in browsers]
    if succeeds:
        results[-1] = ([target], browsers[-1])
    with (
        patch.object(
            cast_client.pychromecast, "get_listed_chromecasts", side_effect=results
        ) as get,
        patch.object(cast_client, "create_zeroconf") as create,
        patch.object(cast_client.time, "sleep") as sleep,
    ):
        cast, browser = discover_chromecasts("リビングルーム")

    assert cast is (target if succeeds else None)
    assert browser is browsers[-1]
    assert get.call_count == create.call_count == attempts
    assert all(call.kwargs["friendly_names"] == ["リビングルーム"] for call in get.call_args_list)
    assert sleep.call_count == attempts - 1
    assert all(call.args == (0.25,) for call in sleep.call_args_list)
    for retired in browsers[:-1]:
        retired.stop_discovery.assert_called_once_with()
    browser.stop_discovery.assert_not_called()
    stop_discovery(browser)
    browser.stop_discovery.assert_called_once_with()


def test_discovery_does_not_retry_when_target_absent(
    fake_network: Callable[[list[str]], Any],
) -> None:
    """普通の不在では再探索の待ち時間を増やさない。"""
    with (
        fake_network(["OtherDevice"]) as network,
        patch("nemucast.cast_client.time.sleep") as sleep,
    ):
        cast, _ = discover_chromecasts("リビングルーム")
    assert cast is None
    network["get_listed"].assert_called_once()
    sleep.assert_not_called()
