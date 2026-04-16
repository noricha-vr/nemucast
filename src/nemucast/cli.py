"""コマンドライン引数の解析、ロギング設定、CLI エントリポイントを提供する。"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

from nemucast.cast_client import discover_chromecasts, stop_discovery
from nemucast.config import (
    CHROMECAST_NAME,
    CRON_0030_OVERRIDES,
    CRON_20_OVERRIDES,
    DEFAULT_INTERVAL_SEC,
    DEFAULT_STATE_FILE,
    INACTIVE_THRESHOLD,
    LOG_DIR,
    LOG_ROTATION_BACKUP_COUNT,
    LOG_ROTATION_MAX_BYTES,
    MANUAL_RISE_THRESHOLD,
    MIN_LEVEL,
    RUN_UNTIL_STANDBY,
    STEP,
)
from nemucast.state import clear_state
from nemucast.volume import VolumeSessionConfig, run_volume_session


def parse_args(
    args: list[str] | None = None,
    default_overrides: dict[str, Any] | None = None,
) -> argparse.Namespace:
    """コマンドライン引数を解析する。

    ``default_overrides`` は cron プロファイルなどが各オプションの既定値を差し替えるために使う。
    """
    ov = default_overrides or {}

    def d(key: str, fallback: Any) -> Any:
        return ov.get(key, fallback)

    parser = argparse.ArgumentParser(
        description="Chromecast / Google TV の音量を定期実行ごとに下げるスクリプト"
    )
    parser.add_argument(
        "-i", "--interval", type=int, default=d("interval", DEFAULT_INTERVAL_SEC),
        help=f"定期実行の間隔（秒）。デフォルト: {d('interval', DEFAULT_INTERVAL_SEC)}秒",
    )  # fmt: skip
    parser.add_argument(
        "-n", "--name", type=str, default=d("name", CHROMECAST_NAME),
        help=f"Chromecastの名前。デフォルト: {d('name', CHROMECAST_NAME)}",
    )  # fmt: skip
    parser.add_argument(
        "-s", "--step", type=float, default=d("step", STEP),
        help=f"音量調整のステップ（負の値）。デフォルト: {d('step', STEP)}",
    )  # fmt: skip
    parser.add_argument(
        "-m", "--min-level", type=float, default=d("min_level", MIN_LEVEL),
        help=f"最小音量レベル。デフォルト: {d('min_level', MIN_LEVEL)}",
    )  # fmt: skip
    inactive = d("inactive_threshold", INACTIVE_THRESHOLD)
    manual_rise = d("manual_rise_threshold", MANUAL_RISE_THRESHOLD)
    state_file = d("state_file", DEFAULT_STATE_FILE)
    parser.add_argument(
        "--inactive-threshold",
        type=int, default=inactive,
        help=f"非アクティブ判定までの連続回数。デフォルト: {inactive}",
    )  # fmt: skip
    parser.add_argument(
        "--manual-rise-threshold",
        type=float, default=manual_rise,
        help=f"手動で音量が上がったとみなす最小差分。デフォルト: {manual_rise}",
    )  # fmt: skip
    parser.add_argument(
        "--state-file",
        type=Path, default=Path(state_file),
        help=f"活動判定用 state JSON の保存先。デフォルト: {state_file}",
    )  # fmt: skip
    parser.add_argument(
        "--run-until-standby",
        action="store_true",
        default=bool(d("run_until_standby", RUN_UNTIL_STANDBY)),
        help="standby になるまで interval ごとに判定と音量調整を繰り返します。",
    )

    parsed = parser.parse_args(args)

    if parsed.interval <= 0:
        parser.error("--interval は正の整数を指定してください")
    if parsed.step >= 0:
        parser.error("--step は負の値を指定してください")
    if not 0 <= parsed.min_level <= 1:
        parser.error("--min-level は 0.0 から 1.0 の範囲で指定してください")
    if parsed.inactive_threshold <= 0:
        parser.error("--inactive-threshold は正の整数を指定してください")
    if parsed.manual_rise_threshold < 0:
        parser.error("--manual-rise-threshold は 0 以上を指定してください")

    return parsed


def setup_logging() -> None:
    """ロギングの設定を行う"""
    log_dir = Path.cwd() / LOG_DIR
    log_dir.mkdir(exist_ok=True)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="[%(asctime)s] %(levelname)s: %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                log_dir / "lower_cast_volume.log",
                maxBytes=LOG_ROTATION_MAX_BYTES,
                backupCount=LOG_ROTATION_BACKUP_COUNT,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def run_with_args(
    args: list[str] | None = None,
    default_overrides: dict[str, Any] | None = None,
) -> None:
    """CLI 実行本体"""
    parsed = parse_args(args=args, default_overrides=default_overrides)
    setup_logging()

    logging.info(
        "設定: interval=%ds name=%s step=%.2f min_level=%.2f "
        "inactive_threshold=%d manual_rise=%.2f state=%s run_until_standby=%s",
        parsed.interval,
        parsed.name,
        parsed.step,
        parsed.min_level,
        parsed.inactive_threshold,
        parsed.manual_rise_threshold,
        parsed.state_file,
        parsed.run_until_standby,
    )

    cast, browser = discover_chromecasts(parsed.name)
    if cast is None:
        stop_discovery(browser)
        raise SystemExit(1)

    try:
        logging.info("接続完了: %s (%s)", cast.cast_info.friendly_name, cast.cast_info.host)
        cast.wait()
        config = VolumeSessionConfig(
            interval_sec=parsed.interval,
            step=parsed.step,
            min_level=parsed.min_level,
            inactive_threshold=parsed.inactive_threshold,
            manual_rise_threshold=parsed.manual_rise_threshold,
            state_file=parsed.state_file,
            device_name=parsed.name,
            run_until_standby=parsed.run_until_standby,
        )
        result = run_volume_session(cast=cast, config=config)
        logging.info("今回の実行結果: %s", result.value)
    except KeyboardInterrupt:
        logging.info("中断されました。")
        raise
    except Exception:
        # 予期せぬ例外時は state を破棄してクリーンな状態から再開できるようにし、
        # 原因特定のため stack trace をログに残したうえで再 raise する。
        clear_state(parsed.state_file)
        logging.exception("実行に失敗したため state を削除して終了します。")
        raise SystemExit(1) from None
    finally:
        stop_discovery(browser)


def main() -> None:
    run_with_args()


def main_cron_20() -> None:
    """20:00 用の即時 standby プロファイル"""
    run_with_args(default_overrides=CRON_20_OVERRIDES)


def main_cron_0030() -> None:
    """00:30 用の 15 分間隔プロファイル"""
    run_with_args(default_overrides=CRON_0030_OVERRIDES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("中断しました。")
