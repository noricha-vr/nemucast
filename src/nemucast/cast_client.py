"""Chromecast デバイスの検索・制御を担うモジュール。"""

from __future__ import annotations

import logging
import time

import pychromecast
import zeroconf

from nemucast.config import STANDBY_WAIT_SEC


def create_zeroconf() -> zeroconf.Zeroconf:
    """loopback を bind しない zeroconf インスタンスを生成する。

    zeroconf の既定 (InterfaceChoice.All) は 127.0.0.1 にも mDNS の respond socket を
    bind するため、loopback:5353 を SO_REUSEPORT なしで占有するプロセス
    （Lima VM のポートフォワード等）があると OSError(48) で discovery ごと失敗する。
    LAN 上の Chromecast 探索に loopback は不要なので 0.0.0.0 だけを bind する。
    """
    return zeroconf.Zeroconf(interfaces=zeroconf.InterfaceChoice.Default)


def discover_chromecasts(
    target_name: str,
) -> tuple[
    pychromecast.Chromecast | None,
    pychromecast.discovery.CastBrowser | None,
]:
    """指定された名前の Chromecast を検索する。

    Returns:
        (見つかった Chromecast または None, 停止用の CastBrowser)。
        browser は呼び出し側が stop_discovery() で必ず閉じる。
    """
    logging.info("Chromecast デバイスを検索しています...")
    # get_chromecasts() は blocking パスで zeroconf_instance を捨てる（pychromecast 14.0.7）ため
    # 使わない。get_listed_chromecasts なら自前の zeroconf が効き、目的デバイスを見つけた時点で
    # 探索を打ち切るので、無関係なデバイスへの接続も待ち時間も発生しない。
    chromecasts, browser = pychromecast.get_listed_chromecasts(
        friendly_names=[target_name],
        zeroconf_instance=create_zeroconf(),
    )

    if not chromecasts:
        logging.error(
            "目的の Chromecast '%s' が見つかりませんでした。発見したデバイス: %s",
            target_name,
            [device.friendly_name for device in browser.devices.values()],
        )
        return None, browser

    logging.info("キャスト名: %s", chromecasts[0].cast_info.friendly_name)
    return chromecasts[0], browser


def stop_discovery(
    browser: pychromecast.discovery.CastBrowser | None,
) -> None:
    """Discovery を適切に停止する"""
    if browser is None:
        return

    stop_method = getattr(browser, "stop_discovery", None)
    if callable(stop_method):
        stop_method()
        return

    pychromecast.stop_discovery(browser)


def get_current_volume(cast: pychromecast.Chromecast) -> float:
    """現在音量を取得する"""
    current_volume = cast.status.volume_level
    if current_volume is None:
        raise RuntimeError("音量レベルを取得できませんでした。")
    return float(current_volume)


def standby_device(cast: pychromecast.Chromecast) -> None:
    """Chromecast をスタンバイへ移行する"""
    logging.info("非アクティブ判定に達したため、Chromecastをスタンバイモードにします。")
    cast.quit_app()
    time.sleep(STANDBY_WAIT_SEC)
    logging.info("Chromecastがスタンバイモードになりました。")
