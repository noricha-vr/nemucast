"""音量制御と 1 tick 分の判定ロジックを担うモジュール。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pychromecast

from nemucast.cast_client import get_current_volume, standby_device
from nemucast.state import (
    append_history,
    clear_state,
    create_initial_state,
    detect_manual_activity,
    is_state_stale,
    load_state,
    save_state,
)


def calculate_next_volume(current_volume: float, step: float, min_level: float) -> float:
    """次に設定する音量を計算する"""
    if current_volume <= min_level:
        return current_volume

    next_volume = round(current_volume + step, 2)
    next_volume = max(min_level, next_volume)
    return min(1.0, max(0.0, next_volume))


def lower_volume_once(
    cast: pychromecast.Chromecast,
    current_volume: float,
    step: float,
    min_level: float,
) -> float:
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


def run_volume_tick(
    cast: pychromecast.Chromecast,
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
    cast: pychromecast.Chromecast,
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
