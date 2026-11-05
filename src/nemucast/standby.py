"""テレビ本体の電源を切るスクリプト（20:00 のタイマー用）。

Android TV Remote protocol v2 で POWER を送り、is_on の読み戻しで切れたことを確認する。
音量は触らない。切れなかった・見つからない・接続できない時は終了コード 1 で終わり、
DISCOVERY_ALERT_COMMAND（Discord 通知）で知らせる。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from nemucast.auto_standby import send_discovery_alert
from nemucast.cast_client import discover_chromecasts, stop_discovery
from nemucast.config import (
    CHROMECAST_NAME,
    LOG_DIR,
    LOG_ROTATION_BACKUP_COUNT,
    LOG_ROTATION_MAX_BYTES,
)
from nemucast.tv_power import (
    TvPowerError,
    load_cached_host,
    power_off,
    save_cached_host,
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
    parser = argparse.ArgumentParser(description="指定したテレビの電源を切る")
    parser.add_argument(
        "-n", "--name", type=str, default=CHROMECAST_NAME,
        help=f"対象 Cast 名。デフォルト: {CHROMECAST_NAME}",
    )  # fmt: skip
    return parser.parse_args(args)


def resolve_host(name: str) -> str:
    """Cast の発見結果から IP を得る。見えなければ最後に発見できた IP を使う。

    IP は DHCP で変わり得るので固定せず、毎回 mDNS で引き直す。
    """
    cast, browser = discover_chromecasts(name)
    try:
        if cast is not None:
            host = cast.cast_info.host
            save_cached_host(name, host)
            return host
    finally:
        stop_discovery(browser)
    cached = load_cached_host(name)
    if cached is None:
        raise TvPowerError(f"'{name}' が mDNS に見えず、前回の IP の記録もありません")
    logging.warning("'%s' が mDNS に見えないため前回の IP %s で到達確認します", name, cached)
    return cached


def main(args: list[str] | None = None) -> None:
    parsed = parse_args(args)
    setup_logging()
    logging.info("テレビ電源OFF 要求: device=%s", parsed.name)
    try:
        host = resolve_host(parsed.name)
        result = asyncio.run(power_off(host))
    except TvPowerError as exc:
        message = f"nemucast: テレビ '{parsed.name}' の電源を切れませんでした: {exc}"
        logging.error(message)
        send_discovery_alert(message)
        sys.exit(1)
    logging.info("完了: %s（device=%s）", result, parsed.name)


if __name__ == "__main__":
    main()
