"""15分間隔の最小ロジックで Chromecast を寝かしつけるスクリプト。

ロジック:
  1. 電源OFF状態 (powered_off=True) なら、音量が上がっていない限り何もしない。
     音量が上がっていれば state をリセットして通常フローに入る。
  2. 直前 script が下げた値より現在音量が上がっていれば、連続下げカウントを 0 に戻す。
  3. 連続下げカウントが AUTO_LOWERED_THRESHOLD 以上なら quit_app を呼んで電源OFF。
  4. それ以外は音量を STEP だけ下げ、カウントを +1 する。

state ファイルは tick 間で読み書きされる単純な JSON。`volume.py` / `state.py`
の機構には依存しない。
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any

from nemucast.cast_client import (
    discover_chromecasts,
    get_current_volume,
    standby_device,
    stop_discovery,
)
from nemucast.config import (
    CHROMECAST_NAME,
    LOG_DIR,
    LOG_ROTATION_BACKUP_COUNT,
    LOG_ROTATION_MAX_BYTES,
    MANUAL_RISE_THRESHOLD,
    STEP,
)

AUTO_MIN_LEVEL = float(os.getenv("AUTO_MIN_LEVEL", "0.05"))
AUTO_LOWERED_THRESHOLD = int(os.getenv("AUTO_LOWERED_THRESHOLD", "3"))
AUTO_STATE_FILE = Path(os.getenv("AUTO_STATE_FILE", f"{LOG_DIR}/auto_standby_state.json"))


class TickResult(Enum):
    LOWERED = "lowered"
    STANDBY = "standby"
    SKIP = "skip"


def fresh_state(device_name: str) -> dict[str, Any]:
    return {
        "device_name": device_name,
        "last_lowered_to": None,
        "consecutive_lowered": 0,
        "powered_off": False,
        "volume_at_power_off": None,
        "updated_at": time.time(),
    }


def load_state(state_file: Path, device_name: str) -> dict[str, Any] | None:
    if not state_file.exists():
        return None
    data = json.loads(state_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("device_name") != device_name:
        return None
    return data


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.time()
    state_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_tick(
    cast: Any,
    state: dict[str, Any],
    *,
    step: float = STEP,
    min_level: float = AUTO_MIN_LEVEL,
    lowered_threshold: int = AUTO_LOWERED_THRESHOLD,
    rise_threshold: float = MANUAL_RISE_THRESHOLD,
) -> tuple[TickResult, dict[str, Any]]:
    """1 tick 分の判定と操作を行い、(結果, 更新後 state) を返す。"""
    current = get_current_volume(cast)
    logging.info("現在の音量: %.2f (state=%s)", current, state)

    # 1. 電源OFF状態の継続処理
    if state.get("powered_off"):
        baseline = state.get("volume_at_power_off")
        if baseline is None or current <= baseline + rise_threshold:
            logging.info("電源OFF状態を維持。何もしません。")
            return TickResult.SKIP, state
        logging.info("音量上昇を検知 (%.2f → %.2f)。state をリセットして再開します。", baseline, current)
        state = fresh_state(state["device_name"])

    # 2. 手動上昇の検知 → カウントリセット
    last_lowered_to = state.get("last_lowered_to")
    if last_lowered_to is not None and current > last_lowered_to + rise_threshold:
        logging.info(
            "手動で音量が上げられました (%.2f → %.2f)。連続カウントをリセット。",
            last_lowered_to,
            current,
        )
        state["consecutive_lowered"] = 0

    # 3. 閾値到達なら電源OFF
    if state.get("consecutive_lowered", 0) >= lowered_threshold:
        standby_device(cast)
        state["powered_off"] = True
        state["volume_at_power_off"] = current
        return TickResult.STANDBY, state

    # 4. 音量を下げる（最小値以下なら下げないがカウントは進める）
    next_volume = round(max(current + step, min_level), 2)
    if next_volume < current:
        cast.set_volume(next_volume)
        logging.info("音量を %.2f → %.2f に変更しました。", current, next_volume)
        state["last_lowered_to"] = next_volume
    else:
        logging.info(
            "現在音量 %.2f は最小値 %.2f 以下のため下げません。カウントのみ進めます。",
            current,
            min_level,
        )
    state["consecutive_lowered"] = state.get("consecutive_lowered", 0) + 1
    return TickResult.LOWERED, state


def setup_logging() -> None:
    log_dir = Path.cwd() / LOG_DIR
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                log_dir / "auto_standby.log",
                maxBytes=LOG_ROTATION_MAX_BYTES,
                backupCount=LOG_ROTATION_BACKUP_COUNT,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "15分間隔で Chromecast の音量を下げ、3 回連続で下げ続けたら電源OFF にする"
        )
    )
    parser.add_argument(
        "-n", "--name", type=str, default=CHROMECAST_NAME,
        help=f"対象 Chromecast 名。デフォルト: {CHROMECAST_NAME}",
    )  # fmt: skip
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    parsed = parse_args(args)
    setup_logging()

    cast, browser = discover_chromecasts(parsed.name)
    if cast is None:
        stop_discovery(browser)
        raise SystemExit(1)

    try:
        cast.wait()
        state = load_state(AUTO_STATE_FILE, parsed.name) or fresh_state(parsed.name)
        result, new_state = run_tick(cast=cast, state=state)
        save_state(AUTO_STATE_FILE, new_state)
        logging.info("tick 結果: %s", result.value)
    finally:
        stop_discovery(browser)


if __name__ == "__main__":
    main()
