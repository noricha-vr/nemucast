"""Chromecast デバイスの検索・制御を担うモジュール。"""

from __future__ import annotations

import logging
import time

import pychromecast


def discover_chromecasts(
    target_name: str,
) -> tuple[
    pychromecast.Chromecast | None,
    pychromecast.discovery.CastBrowser | None,
]:
    """指定された名前の Chromecast を検索する"""
    logging.info("Chromecast デバイスを検索しています...")
    chromecasts, browser = pychromecast.get_chromecasts()

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
    time.sleep(2)
    logging.info("Chromecastがスタンバイモードになりました。")
