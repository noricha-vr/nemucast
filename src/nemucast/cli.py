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
    DEFAULT_INTERVAL_SEC,
    DEFAULT_STATE_FILE,
    INACTIVE_THRESHOLD,
    LOG_DIR,
    LOG_ROTATION_BACKUP_COUNT,
    LOG_ROTATION_MAX_BYTES,
    MANUAL_RISE_THRESHOLD,
    MIN_LEVEL,
    RUN_UNTIL_STANDBY,
    SCHEDULE_PROFILES,
    STATE_STALE_INTERVAL_MULTIPLIER,
    STEP,
)
from nemucast.state import clear_state
from nemucast.volume import VolumeSessionConfig, run_volume_session


def _add_volume_arguments(parser: argparse.ArgumentParser, overrides: dict[str, Any]) -> None:
    """音量・デバイス関連のオプションを parser に追加する。"""
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=overrides.get("interval", DEFAULT_INTERVAL_SEC),
        help=(
            f"定期実行の間隔（秒）。デフォルト: {overrides.get('interval', DEFAULT_INTERVAL_SEC)}秒"
        ),
    )
    parser.add_argument(
        "-n",
        "--name",
        type=str,
        default=overrides.get("name", CHROMECAST_NAME),
        help=f"Chromecastの名前。デフォルト: {overrides.get('name', CHROMECAST_NAME)}",
    )
    parser.add_argument(
        "-s",
        "--step",
        type=float,
        default=overrides.get("step", STEP),
        help=(f"音量調整のステップ（負の値）。デフォルト: {overrides.get('step', STEP)}"),
    )
    parser.add_argument(
        "-m",
        "--min-level",
        type=float,
        default=overrides.get("min_level", MIN_LEVEL),
        help=(f"最小音量レベル。デフォルト: {overrides.get('min_level', MIN_LEVEL)}"),
    )


def _add_schedule_arguments(parser: argparse.ArgumentParser, overrides: dict[str, Any]) -> None:
    """活動判定・セッション制御関連のオプションを parser に追加する。"""
    parser.add_argument(
        "--inactive-threshold",
        type=int,
        default=overrides.get("inactive_threshold", INACTIVE_THRESHOLD),
        help=(
            "非アクティブ判定までの連続回数。デフォルト: "
            f"{overrides.get('inactive_threshold', INACTIVE_THRESHOLD)}"
        ),
    )
    parser.add_argument(
        "--manual-rise-threshold",
        type=float,
        default=overrides.get("manual_rise_threshold", MANUAL_RISE_THRESHOLD),
        help=(
            "手動で音量が上がったとみなす最小差分。"
            f"デフォルト: {overrides.get('manual_rise_threshold', MANUAL_RISE_THRESHOLD)}"
        ),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(overrides.get("state_file", DEFAULT_STATE_FILE)),
        help=(
            "活動判定用 state JSON の保存先。デフォルト: "
            f"{overrides.get('state_file', DEFAULT_STATE_FILE)}"
        ),
    )
    parser.add_argument(
        "--run-until-standby",
        action="store_true",
        default=bool(overrides.get("run_until_standby", RUN_UNTIL_STANDBY)),
        help="standby になるまで interval ごとに判定と音量調整を繰り返します。",
    )


def _build_parser(overrides: dict[str, Any]) -> argparse.ArgumentParser:
    """引数パーサを組み立てる。"""
    parser = argparse.ArgumentParser(
        description="Chromecast / Google TV の音量を定期実行ごとに下げるスクリプト"
    )
    _add_volume_arguments(parser, overrides)
    _add_schedule_arguments(parser, overrides)
    return parser


def _validate_arguments(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> None:
    """解析済み引数と関連する設定定数の妥当性を検証する。"""
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
    if STATE_STALE_INTERVAL_MULTIPLIER <= 0:
        parser.error("STATE_STALE_INTERVAL_MULTIPLIER は正の整数である必要があります")


def parse_args(
    args: list[str] | None = None,
    default_overrides: dict[str, Any] | None = None,
) -> argparse.Namespace:
    """コマンドライン引数を解析する"""
    overrides = default_overrides or {}
    parser = _build_parser(overrides)
    parsed = parser.parse_args(args)
    _validate_arguments(parser, parsed)
    return parsed


def setup_logging() -> None:
    """ロギングの設定を行う"""
    log_dir = Path.cwd() / LOG_DIR
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "lower_cast_volume.log"

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="[%(asctime)s] %(levelname)s: %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=LOG_ROTATION_MAX_BYTES,
                backupCount=LOG_ROTATION_BACKUP_COUNT,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def build_schedule_defaults(profile_name: str) -> dict[str, Any]:
    """スケジュール別の推奨設定を返す

    Args:
        profile_name: ``SCHEDULE_PROFILES`` に登録されたプロファイル名。

    Returns:
        プロファイルに紐づく既定値の辞書。呼び出し元で変更しても config 側の
        定義に影響しないよう、毎回コピーを返す。

    Raises:
        ValueError: 未知のプロファイル名が渡された場合。
    """
    profile = SCHEDULE_PROFILES.get(profile_name)
    if profile is None:
        raise ValueError(f"未知のスケジュールプロファイルです: {profile_name}")
    return dict(profile)


def _log_session_parameters(args: argparse.Namespace) -> None:
    """セッション開始時のパラメータをログ出力する。"""
    logging.info("定期実行の間隔: %d秒", args.interval)
    logging.info("Chromecast名: %s", args.name)
    logging.info("音量調整ステップ: %.2f", args.step)
    logging.info("最小音量レベル: %.2f", args.min_level)
    logging.info("非アクティブ判定回数: %d", args.inactive_threshold)
    logging.info("手動上昇の判定差分: %.2f", args.manual_rise_threshold)
    logging.info("state ファイル: %s", args.state_file)
    logging.info("standby まで継続実行: %s", args.run_until_standby)


def _build_session_config(args: argparse.Namespace) -> VolumeSessionConfig:
    """解析済み引数から ``VolumeSessionConfig`` を組み立てる。"""
    return VolumeSessionConfig(
        interval_sec=args.interval,
        step=args.step,
        min_level=args.min_level,
        inactive_threshold=args.inactive_threshold,
        manual_rise_threshold=args.manual_rise_threshold,
        state_file=args.state_file,
        device_name=args.name,
        run_until_standby=args.run_until_standby,
    )


def run_with_args(
    args: list[str] | None = None,
    default_overrides: dict[str, Any] | None = None,
) -> None:
    """CLI 実行本体"""
    parsed = parse_args(args=args, default_overrides=default_overrides)
    setup_logging()
    _log_session_parameters(parsed)

    cast, browser = discover_chromecasts(parsed.name)
    if cast is None:
        stop_discovery(browser)
        raise SystemExit(1)

    try:
        logging.info("接続完了: %s (%s)", cast.cast_info.friendly_name, cast.cast_info.host)
        cast.wait()
        result = run_volume_session(cast=cast, config=_build_session_config(parsed))
        logging.info("今回の実行結果: %s", result)
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
    run_with_args(default_overrides=build_schedule_defaults("cron-20"))


def main_cron_0030() -> None:
    """00:30 用の 15 分間隔プロファイル"""
    run_with_args(default_overrides=build_schedule_defaults("cron-0030"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("中断しました。")
