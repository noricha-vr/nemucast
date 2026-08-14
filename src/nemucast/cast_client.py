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
    """指定された名前の Chromecast を検索する"""
    logging.info("Chromecast デバイスを検索しています...")
    # pychromecast.get_chromecasts() は blocking パスで zeroconf_instance を捨てるため
    # （pychromecast 14.0.7）、discovery API を直接呼んで自前の zeroconf を渡す。
    devices, browser = pychromecast.discovery.discover_chromecasts(
        zeroconf_instance=create_zeroconf()
    )
    chromecasts = []
    for device in devices:
        try:
            chromecasts.append(pychromecast.get_chromecast_from_cast_info(device, browser.zc))
        except pychromecast.ChromecastConnectionError:
            logging.warning("接続できないデバイスをスキップします: %s", device.friendly_name)

    if not chromecasts:
        logging.error("ネットワーク上で Chromecast が見つかりませんでした。")
        return None, browser

    logging.info("発見したデバイス: %s", [cc.cast_info.friendly_name for cc in chromecasts])

    for cc in chromecasts:
        logging.info("キャスト名: %s", cc.cast_info.friendly_name)
        if cc.cast_info.friendly_name == target_name:
            return cc, browser

    logging.error("目的の Chromecast '%s' が見つかりませんでした。", target_name)
    return None, browser


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
