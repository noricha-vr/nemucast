#!/usr/bin/env python3
"""
定期実行ごとに Chromecast / Google TV の音量を1段階下げる。

Chromecast の active / idle 判定には依存せず、
前回自動設定した音量と現在音量の差分からユーザーの活動を推定する。
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

import pychromecast
from dotenv import load_dotenv

load_dotenv()

CHROMECAST_NAME = os.getenv("CHROMECAST_NAME", "Dell")
STEP = float(os.getenv("STEP", "-0.04"))
MIN_LEVEL = float(os.getenv("MIN_LEVEL", "0.3"))
DEFAULT_INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "1200"))
INACTIVE_THRESHOLD = int(os.getenv("INACTIVE_THRESHOLD", "3"))
MANUAL_RISE_THRESHOLD = float(os.getenv("MANUAL_RISE_THRESHOLD", "0.01"))
DEFAULT_STATE_FILE = os.getenv("STATE_FILE", "logs/activity_state.json")
RUN_UNTIL_STANDBY = os.getenv("RUN_UNTIL_STANDBY", "0") == "1"
STATE_STALE_INTERVAL_MULTIPLIER = int(os.getenv("STATE_STALE_INTERVAL_MULTIPLIER", "2"))
CRON_20_NAME = os.getenv("CRON_20_NAME", CHROMECAST_NAME)
CRON_20_INTERVAL_SEC = int(os.getenv("CRON_20_INTERVAL_SEC", "60"))
CRON_20_INACTIVE_THRESHOLD = int(os.getenv("CRON_20_INACTIVE_THRESHOLD", "1"))
CRON_20_STEP = float(os.getenv("CRON_20_STEP", str(STEP)))
CRON_20_MIN_LEVEL = float(os.getenv("CRON_20_MIN_LEVEL", "0.05"))
CRON_20_STATE_FILE = os.getenv("CRON_20_STATE_FILE", "logs/activity_state_20.json")
CRON_0030_NAME = os.getenv("CRON_0030_NAME", CHROMECAST_NAME)
CRON_0030_INTERVAL_SEC = int(os.getenv("CRON_0030_INTERVAL_SEC", "900"))
CRON_0030_INACTIVE_THRESHOLD = int(os.getenv("CRON_0030_INACTIVE_THRESHOLD", "4"))
CRON_0030_STEP = float(os.getenv("CRON_0030_STEP", str(STEP)))
CRON_0030_MIN_LEVEL = float(os.getenv("CRON_0030_MIN_LEVEL", "0.35"))
CRON_0030_STATE_FILE = os.getenv("CRON_0030_STATE_FILE", "logs/activity_state_0030.json")
MAX_HISTORY_ENTRIES = 20


def parse_args(args=None, default_overrides: dict[str, Any] | None = None):
    """コマンドライン引数を解析する"""
    overrides = default_overrides or {}
    parser = argparse.ArgumentParser(
        description="Chromecast / Google TV の音量を定期実行ごとに下げるスクリプト"
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=overrides.get("interval", DEFAULT_INTERVAL_SEC),
        help=(
            "定期実行の間隔（秒）。デフォルト: "
            f"{overrides.get('interval', DEFAULT_INTERVAL_SEC)}秒"
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
        help=(
            "音量調整のステップ（負の値）。デフォルト: "
            f"{overrides.get('step', STEP)}"
        ),
    )
    parser.add_argument(
        "-m",
        "--min-level",
        type=float,
        default=overrides.get("min_level", MIN_LEVEL),
        help=(
            "最小音量レベル。デフォルト: "
            f"{overrides.get('min_level', MIN_LEVEL)}"
        ),
    )
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
    if STATE_STALE_INTERVAL_MULTIPLIER <= 0:
        parser.error("STATE_STALE_INTERVAL_MULTIPLIER は正の整数である必要があります")
    return parsed


def setup_logging() -> None:
    """ロギングの設定を行う"""
    log_dir = Path.cwd() / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "lower_cast_volume.log"

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="[%(asctime)s] %(levelname)s: %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=512 * 1024, backupCount=1, encoding="utf-8"
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


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


def load_state(state_file: Path) -> dict[str, Any] | None:
    """state JSON を読み込む"""
    if not state_file.exists():
        return None

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"state ファイルの JSON が壊れています: {state_file}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"state ファイルの形式が不正です: {state_file}")

    return data


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    """state JSON を保存する"""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clear_state(state_file: Path) -> None:
    """state JSON を削除する"""
    if state_file.exists():
        state_file.unlink()


def create_initial_state(device_name: str, current_volume: float, now_ts: float) -> dict[str, Any]:
    """新しいセッション用の state を作る"""
    return {
        "device_name": device_name,
        "last_auto_volume": current_volume,
        "inactive_streak": 0,
        "updated_at": now_ts,
        "history": [],
    }


def is_state_stale(
    state: dict[str, Any],
    device_name: str,
    interval_sec: int,
    now_ts: float,
) -> bool:
    """古い state かどうかを判定する"""
    if state.get("device_name") != device_name:
        return True

    updated_at = state.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return True

    return now_ts - float(updated_at) > interval_sec * STATE_STALE_INTERVAL_MULTIPLIER


def detect_manual_activity(
    current_volume: float,
    last_auto_volume: float | None,
    rise_threshold: float,
) -> bool:
    """前回自動設定した音量より十分に上がっていれば手動操作とみなす"""
    if last_auto_volume is None:
        return False

    return current_volume > last_auto_volume + rise_threshold


def append_history(state: dict[str, Any], entry: dict[str, Any]) -> None:
    """履歴を最大件数まで保持する"""
    history = state.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        state["history"] = history

    history.append(entry)
    if len(history) > MAX_HISTORY_ENTRIES:
        del history[:-MAX_HISTORY_ENTRIES]


def get_current_volume(cast) -> float:
    """現在音量を取得する"""
    current_volume = cast.status.volume_level
    if current_volume is None:
        raise RuntimeError("音量レベルを取得できませんでした。")
    return float(current_volume)


def calculate_next_volume(current_volume: float, step: float, min_level: float) -> float:
    """次に設定する音量を計算する"""
    if current_volume <= min_level:
        return current_volume

    next_volume = round(current_volume + step, 2)
    next_volume = max(min_level, next_volume)
    return min(1.0, max(0.0, next_volume))


def lower_volume_once(cast, current_volume: float, step: float, min_level: float) -> float:
    """1回分だけ音量を下げる"""
    next_volume = calculate_next_volume(current_volume, step, min_level)
    if next_volume < current_volume:
        cast.set_volume(next_volume)
        logging.info("音量を %.2f → %.2f へ変更しました", current_volume, next_volume)
    else:
        logging.info(
            "現在の音量 %.2f は最小音量 %.2f 以下のため維持します",
            current_volume,
            min_level,
        )
    return next_volume


def standby_device(cast) -> None:
    """Chromecast をスタンバイへ移行する"""
    logging.info("非アクティブ判定に達したため、Chromecastをスタンバイモードにします。")
    cast.quit_app()
    time.sleep(2)
    logging.info("Chromecastがスタンバイモードになりました。")


def run_volume_tick(
    cast,
    interval_sec: int,
    step: float,
    min_level: float,
    inactive_threshold: int,
    manual_rise_threshold: float,
    state_file: Path,
    device_name: str,
) -> str:
    """1回の定期実行ぶんの判定と音量操作を行う"""
    now_ts = time.time()
    current_volume = get_current_volume(cast)
    logging.info("現在の音量: %.2f", current_volume)

    state = load_state(state_file)
    if state and is_state_stale(state, device_name, interval_sec, now_ts):
        logging.info("既存の state が古いか別デバイスのため、新しいセッションとして開始します。")
        state = None

    if state is None:
        state = create_initial_state(device_name, current_volume, now_ts)

    last_auto_volume = state.get("last_auto_volume")
    manual_raise_detected = detect_manual_activity(
        current_volume=current_volume,
        last_auto_volume=last_auto_volume,
        rise_threshold=manual_rise_threshold,
    )

    if manual_raise_detected:
        inactive_streak = 0
        logging.info(
            (
                "手動で音量が上げられました "
                "(前回 %.2f → 現在 %.2f)。非アクティブ回数をリセットします。"
            ),
            last_auto_volume,
            current_volume,
        )
    else:
        inactive_streak = int(state.get("inactive_streak", 0)) + 1
        logging.info(
            "手動での音量上昇は未検出です。非アクティブ回数: %d/%d",
            inactive_streak,
            inactive_threshold,
        )

    history_entry = {
        "timestamp": now_ts,
        "observed_volume": round(current_volume, 2),
        "inactive_streak": inactive_streak,
        "manual_raise_detected": manual_raise_detected,
    }

    if inactive_streak >= inactive_threshold:
        append_history(
            state,
            {
                **history_entry,
                "applied_volume": round(current_volume, 2),
                "action": "standby",
            },
        )
        clear_state(state_file)
        standby_device(cast)
        return "standby"

    applied_volume = lower_volume_once(
        cast=cast,
        current_volume=current_volume,
        step=step,
        min_level=min_level,
    )

    state.update(
        {
            "device_name": device_name,
            "last_auto_volume": applied_volume,
            "inactive_streak": inactive_streak,
            "updated_at": now_ts,
        }
    )
    append_history(
        state,
        {
            **history_entry,
            "applied_volume": round(applied_volume, 2),
            "action": "volume_down" if applied_volume < current_volume else "keep",
        },
    )
    save_state(state_file, state)
    return "volume_down" if applied_volume < current_volume else "keep"


def run_volume_session(
    cast,
    interval_sec: int,
    step: float,
    min_level: float,
    inactive_threshold: int,
    manual_rise_threshold: float,
    state_file: Path,
    device_name: str,
    run_until_standby: bool,
) -> str:
    """1回または standby 到達までの連続セッションを実行する"""
    result = run_volume_tick(
        cast=cast,
        interval_sec=interval_sec,
        step=step,
        min_level=min_level,
        inactive_threshold=inactive_threshold,
        manual_rise_threshold=manual_rise_threshold,
        state_file=state_file,
        device_name=device_name,
    )

    while run_until_standby and result != "standby":
        logging.info("次の判定まで %d 秒待機します。", interval_sec)
        time.sleep(interval_sec)
        result = run_volume_tick(
            cast=cast,
            interval_sec=interval_sec,
            step=step,
            min_level=min_level,
            inactive_threshold=inactive_threshold,
            manual_rise_threshold=manual_rise_threshold,
            state_file=state_file,
            device_name=device_name,
        )

    return result


def run_with_args(args=None, default_overrides: dict[str, Any] | None = None) -> None:
    """CLI 実行本体"""
    args = parse_args(args=args, default_overrides=default_overrides)
    setup_logging()

    logging.info("定期実行の間隔: %d秒", args.interval)
    logging.info("Chromecast名: %s", args.name)
    logging.info("音量調整ステップ: %.2f", args.step)
    logging.info("最小音量レベル: %.2f", args.min_level)
    logging.info("非アクティブ判定回数: %d", args.inactive_threshold)
    logging.info("手動上昇の判定差分: %.2f", args.manual_rise_threshold)
    logging.info("state ファイル: %s", args.state_file)
    logging.info("standby まで継続実行: %s", args.run_until_standby)

    cast, browser = discover_chromecasts(args.name)
    if cast is None:
        stop_discovery(browser)
        raise SystemExit(1)

    try:
        logging.info("接続完了: %s (%s)", cast.cast_info.friendly_name, cast.cast_info.host)
        cast.wait()
        result = run_volume_session(
            cast=cast,
            interval_sec=args.interval,
            step=args.step,
            min_level=args.min_level,
            inactive_threshold=args.inactive_threshold,
            manual_rise_threshold=args.manual_rise_threshold,
            state_file=args.state_file,
            device_name=args.name,
            run_until_standby=args.run_until_standby,
        )
        logging.info("今回の実行結果: %s", result)
    except KeyboardInterrupt:
        logging.info("中断されました。")
        raise
    except Exception:
        clear_state(args.state_file)
        logging.exception("実行に失敗したため state を削除して終了します。")
        raise SystemExit(1)
    finally:
        stop_discovery(browser)


def build_schedule_defaults(profile_name: str) -> dict[str, Any]:
    """スケジュール別の推奨設定を返す"""
    if profile_name == "cron-20":
        return {
            "name": CRON_20_NAME,
            "interval": CRON_20_INTERVAL_SEC,
            "step": CRON_20_STEP,
            "min_level": CRON_20_MIN_LEVEL,
            "inactive_threshold": CRON_20_INACTIVE_THRESHOLD,
            "state_file": CRON_20_STATE_FILE,
            "run_until_standby": True,
        }

    if profile_name == "cron-0030":
        return {
            "name": CRON_0030_NAME,
            "interval": CRON_0030_INTERVAL_SEC,
            "step": CRON_0030_STEP,
            "min_level": CRON_0030_MIN_LEVEL,
            "inactive_threshold": CRON_0030_INACTIVE_THRESHOLD,
            "state_file": CRON_0030_STATE_FILE,
            "run_until_standby": True,
        }

    raise ValueError(f"未知のスケジュールプロファイルです: {profile_name}")


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
        print("\n中断しました。")
