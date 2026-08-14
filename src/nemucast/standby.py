"""Chromecast を即座に standby にするだけのシンプルなスクリプト。

20:00 のタイマーなど「即電源OFF相当」を行いたい場面で使う。
音量制御や state 管理は行わない。
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from nemucast.cast_client import discover_chromecasts, standby_device, stop_discovery
from nemucast.config import (
    CHROMECAST_NAME,
    CONNECT_TIMEOUT_SEC,
    LOG_DIR,
    LOG_ROTATION_BACKUP_COUNT,
    LOG_ROTATION_MAX_BYTES,
)


def setup_logging() -> None:
    log_dir = Path.cwd() / LOG_DIR
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                log_dir / "standby.log",
                maxBytes=LOG_ROTATION_MAX_BYTES,
                backupCount=LOG_ROTATION_BACKUP_COUNT,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="指定した Chromecast / Google TV を即座に standby にする"
    )
    parser.add_argument(
        "-n", "--name", type=str, default=CHROMECAST_NAME,
        help=f"対象 Chromecast 名。デフォルト: {CHROMECAST_NAME}",
    )  # fmt: skip
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    parsed = parse_args(args)
    setup_logging()
    logging.info("standby 要求: device=%s", parsed.name)

    cast, browser = discover_chromecasts(parsed.name)
    if cast is None:
        # デバイスが mDNS に出ない = 既に消えている。standby させる対象がないので成功扱いにする
        # （毎日 20:00 の cron が TV OFF の日に失敗通知を出すのを防ぐ）
        logging.warning("デバイス '%s' が見つからないため何もしません。", parsed.name)
        stop_discovery(browser)
        return

    try:
        # timeout なしだと接続確立を無限に待つ
        cast.wait(timeout=CONNECT_TIMEOUT_SEC)
        standby_device(cast)
    finally:
        stop_discovery(browser)


if __name__ == "__main__":
    main()
