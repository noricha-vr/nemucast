"""活動判定用 state JSON の読み書きと整合性判定を担うモジュール。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nemucast.config import MAX_HISTORY_ENTRIES, STATE_STALE_INTERVAL_MULTIPLIER


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
    history.append(entry)
    if len(history) > MAX_HISTORY_ENTRIES:
        del history[:-MAX_HISTORY_ENTRIES]
