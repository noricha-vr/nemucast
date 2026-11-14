"""夜間に視聴中の機器の音量を段階的に下げ、ペアリング済みなら電源を切る。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import math
import os
import shlex
import subprocess
import sys
import time
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pychromecast import IDLE_APP_ID
from pychromecast.error import PyChromecastError

from nemucast.cast_client import (
    discover_chromecasts,
    get_current_volume,
    stop_discovery,
)
from nemucast.config import (
    AUTO_NIGHT_END,
    AUTO_NIGHT_FORCE,
    AUTO_NIGHT_START,
    AUTO_NOT_FOUND_ALERT_AFTER_TICKS,
    AUTO_NOT_FOUND_ALERT_FILE,
    AUTO_PENDING_RESTORE_FILE,
    AUTO_POWER_OFF_ALERT_FILE,
    AUTO_RESTORE_TOLERANCE,
    AUTO_RESTORE_VOLUME,
    AUTO_RESTORE_WAIT_SEC,
    AUTO_TARGET_NAME,
    AUTO_VOLUME_ALERT_FILE,
    CONNECT_TIMEOUT_SEC,
    DISCOVERY_ALERT_AFTER_TICKS,
    DISCOVERY_ALERT_COMMAND,
    DISCOVERY_ALERT_FILE,
    LOG_DIR,
    LOG_ROTATION_BACKUP_COUNT,
    LOG_ROTATION_MAX_BYTES,
    MANUAL_RISE_THRESHOLD,
    STEP,
)
from nemucast.tv_power import (
    PowerOffUnconfirmedError,
    TvPowerError,
    load_identity,
    power_off,
    read_power_state,
)
from nemucast.tv_volume import (
    ATV_MIN_LEVEL,
    ATV_RESTORE_TOLERANCE,
    AtvVolume,
    AtvVolumeControl,
    TvVolumeError,
)

AUTO_MIN_LEVEL = float(os.getenv("AUTO_MIN_LEVEL", "0.05"))
AUTO_LOWERED_THRESHOLD = int(os.getenv("AUTO_LOWERED_THRESHOLD", "3"))
AUTO_STATE_FILE = Path(os.getenv("AUTO_STATE_FILE", f"{LOG_DIR}/auto_standby_state.json"))
# 実行間隔 15 分の 2 倍。これを超えて間が空いた state は連続カウントを信用しない
AUTO_STATE_STALE_SEC = int(os.getenv("AUTO_STATE_STALE_SEC", "1800"))
DISCOVERY_ALERT_TIMEOUT_SEC = 60
# LAN 上のデバイスが 0 台の時の終了コード。デバイス不在（成功扱い）と区別するため非ゼロにする
EXIT_NO_DEVICE_ON_LAN = 2
STATUS_LOG_VALUE_MAX_LENGTH = 64
CAST_ZERO_TOLERANCE = 1e-6


class TickResult(Enum):
    LOWERED = "lowered"
    STANDBY = "standby"
    VOLUME_UNAVAILABLE = "volume_unavailable"
    VOLUME_UNCHANGED = "volume_unchanged"


def fresh_state(device_name: str) -> dict[str, Any]:
    return {
        "device_name": device_name,
        "last_lowered_to": None,
        "consecutive_lowered": 0,
        "original_volume": None,
        "volume_source": "cast",
        "volume_max": None,
        "atv_lowering": None,
        "updated_at": time.time(),
    }


def is_state_stale(state: dict[str, Any], stale_sec: int = AUTO_STATE_STALE_SEC) -> bool:
    """前回更新から stale_sec 以上空いたか判定する。"""
    updated_at = state.get("updated_at")
    if not isinstance(updated_at, int | float):
        return True
    return time.time() - updated_at > stale_sec


def valid_session_state(data: dict[str, Any]) -> bool:
    """不正なカウントや復元音量を操作に使わない。旧 state の欠落キーは許容する。"""
    count = data.get("consecutive_lowered", 0)
    if type(count) is not int or count < 0:
        return False
    source = data.get("volume_source", "cast")
    maximum = data.get("volume_max")
    if source not in ("cast", "atv"):
        return False
    if source == "atv" and (type(maximum) is not int or maximum <= 0):
        return False
    for key in ("original_volume", "last_lowered_to"):
        value = data.get(key)
        if value is None:
            continue
        if source == "atv":
            if type(value) is not int or not 0 <= value <= maximum:
                return False
        elif type(value) not in (int, float) or not 0 <= value <= 1:
            return False
    lowering = data.get("atv_lowering")
    if lowering is not None:
        if source != "atv" or not isinstance(lowering, dict):
            return False
        start, target = lowering.get("from"), lowering.get("to")
        if type(start) is not int or type(target) is not int:
            return False
        if not 0 <= target < start <= maximum:
            return False
    return True


def load_state(state_file: Path, device_name: str) -> dict[str, Any] | None:
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        # 壊れた state を放置すると毎 tick 例外で落ち続け、15 分間隔のジョブが自力復帰できない
        logging.warning("state を読めなかったため作り直します: %s", exc)
        return None
    if not isinstance(data, dict) or data.get("device_name") != device_name:
        return None
    if "powered_off" in data:
        # 旧形式（powered_off=True, consecutive_lowered=3 等）を引き継ぐと、視聴再開後でも
        # 初回 tick で即 OFF になる。新形式の「電源OFF待ち」と区別するため作り直す
        logging.info("旧形式の state を破棄して作り直します。")
        return None
    if not valid_session_state(data):
        logging.warning("state のカウントまたは音量が不正なため作り直します。")
        return None
    state = fresh_state(device_name)
    state.update({key: data[key] for key in state if key in data})
    state["updated_at"] = data.get("updated_at")
    if is_state_stale(state) and state["consecutive_lowered"] < AUTO_LOWERED_THRESHOLD:
        # 音量復元の責任は時間切れでも消さない。電源OFF待ちも次 tick で再試行する
        logging.info("前回更新から時間が空いたため連続カウントをリセットします。")
        # last_lowered_to は残す: 停止中に手動で上げられた音量を次 tick で検出するため
        state["consecutive_lowered"] = 0
    return state


def save_state(state_file: Path, state: dict[str, Any], *, touch: bool = True) -> None:
    """state を保存する。touch=False なら updated_at を更新しない（stale 判定を保つ）。"""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if touch:
        state["updated_at"] = time.time()
    # 書き込み途中で kill されても壊れた JSON を残さないよう、一時ファイル経由で置き換える
    tmp_file = state_file.with_name(f"{state_file.name}.tmp")
    tmp_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_file, state_file)


def load_pending_restore(name: str) -> dict[str, Any] | None:
    """保留音量を読む。破損・別機器・不正な整数段は操作に使わない。"""
    try:
        data = json.loads(AUTO_PENDING_RESTORE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("device_name") != name:
        return None
    volume = AtvVolume.parse({"level": data.get("level"), "max": data.get("max")})
    left = AtvVolume.parse({"level": data.get("left_at"), "max": data.get("max")})
    left_min = AtvVolume.parse(
        {"level": data.get("left_min", data.get("left_at")), "max": data.get("max")}
    )
    created = data.get("created_at")
    if volume is None or left is None or left_min is None:
        return None
    if not left_min.level <= left.level <= volume.level or volume.level <= left_min.level:
        return None
    try:
        valid_created = type(created) in (int, float) and math.isfinite(created)
    except OverflowError:
        valid_created = False
    if not valid_created:
        return None
    return data


def defer_atv_restore(name: str, state: dict[str, Any], *, cleanup: bool = False) -> None:
    """キーを送らず復元責任をアトミックに保存する。保存失敗時は OFF に進まない。"""
    original, last = state.get("original_volume"), state.get("last_lowered_to")
    if (not AUTO_RESTORE_VOLUME and not cleanup) or state.get("volume_source") != "atv":
        return
    lowering = state.get("atv_lowering")
    if lowering is not None:
        last, left_min = lowering["from"], lowering["to"]
    else:
        left_min = last
    if original is None or last is None or original < last or original <= left_min:
        return
    pending = {
        "device_name": name,
        "level": original,
        "left_at": last,
        "max": state["volume_max"],
        "created_at": time.time(),
    }
    if lowering is not None:
        pending["left_min"] = left_min
    save_state(
        AUTO_PENDING_RESTORE_FILE,
        pending,
        touch=False,
    )
    logging.info(
        "音量復元を保留: device=%s level=%s left_at=%s left_min=%s", name, original, last, left_min
    )


def discard_pending_restore(reason: str) -> None:
    """復元済みまたは人の操作により不要になった保留を消す。"""
    AUTO_PENDING_RESTORE_FILE.unlink(missing_ok=True)
    logging.info("保留復元を破棄: reason=%s", reason)


def log_atv_decision(
    branch: str, is_on: bool | None, level: int | None, state: dict[str, Any]
) -> None:
    """キー送信前の判断材料を毎 tick 一行に残す。"""
    updated = state.get("updated_at")
    elapsed = time.time() - updated if type(updated) in (int, float) else None
    logging.info(
        "判断: branch=%s is_on=%s level=%s last_lowered_to=%s original=%s "
        "count=%s pending=%s since_last_update=%s",
        branch,
        is_on,
        level,
        state.get("last_lowered_to"),
        state.get("original_volume"),
        state["consecutive_lowered"],
        "あり" if load_pending_restore(state["device_name"]) else "なし",
        elapsed,
    )


def in_night_window(now: datetime, start: str, end: str) -> bool:
    """ローカル時刻が夜間窓 [start, end) に入るか返す。同時刻なら空の窓。"""
    start_time = datetime.strptime(start, "%H:%M").time()
    end_time = datetime.strptime(end, "%H:%M").time()
    current = now.time().replace(tzinfo=None)
    if start_time <= end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


def is_session_continuable(state: dict[str, Any]) -> bool:
    """窓外でも続行してよい新鮮な進行中セッションか。NaN・無限大・未来時刻は新鮮と見なさない。"""
    updated_at = state.get("updated_at")
    if type(updated_at) not in (int, float) or not math.isfinite(updated_at):
        return False
    return 0 <= time.time() - updated_at <= AUTO_STATE_STALE_SEC


def run_tick(
    cast: Any,
    state: dict[str, Any],
    *,
    step: float = STEP,
    min_level: float = AUTO_MIN_LEVEL,
    lowered_threshold: int = AUTO_LOWERED_THRESHOLD,
    rise_threshold: float = MANUAL_RISE_THRESHOLD,
) -> tuple[TickResult, dict[str, Any]]:
    """視聴中の機器で音量を下げる。電源制御は呼び出し側で行う。"""
    if not usable_cast_volume(getattr(cast.status, "volume_level", None)):
        return TickResult.VOLUME_UNAVAILABLE, state
    # 前 tick で復元後に電源OFFが失敗した場合、復元音量を手動上昇と誤認しない
    if state.get("consecutive_lowered", 0) >= lowered_threshold:
        return TickResult.STANDBY, state
    current = get_current_volume(cast)
    last_lowered_to = state.get("last_lowered_to")
    if last_lowered_to is not None and current > last_lowered_to + rise_threshold:
        logging.info("手動音量上昇: previous=%.2f current=%.2f", last_lowered_to, current)
        state["consecutive_lowered"] = 0
        state["original_volume"] = current
    if state.get("original_volume") is None:
        state["original_volume"] = current
    next_volume = round(max(current + step, min_level), 2)
    if next_volume < current:
        cast.set_volume(next_volume)
        logging.info("音量変更: previous=%.2f current=%.2f", current, next_volume)
    else:
        logging.info("音量下限のためカウントのみ進めます: current=%.2f", current)
    state["last_lowered_to"] = min(current, next_volume)
    state["consecutive_lowered"] = state.get("consecutive_lowered", 0) + 1
    result = (
        TickResult.STANDBY
        if state["consecutive_lowered"] >= lowered_threshold
        else TickResult.LOWERED
    )
    return result, state


def restore_volume(cast: Any, state: dict[str, Any]) -> None:
    """機器をセッション開始時の音量に戻し、読み戻しで確認する。"""
    original = state.get("original_volume")
    if original is None:
        return
    cast.set_volume(original)
    deadline = time.monotonic() + AUTO_RESTORE_WAIT_SEC
    while time.monotonic() < deadline:
        level = getattr(cast.status, "volume_level", None)
        if isinstance(level, int | float) and abs(level - original) <= AUTO_RESTORE_TOLERANCE:
            logging.info("音量復元を読み戻しで確認: original_volume=%.2f", original)
            return
        time.sleep(0.2)
    # 電源を切ることを優先して続行する（切れない方が実害が大きい）。ただし翌日の音量は要確認
    logging.warning("音量復元を読み戻しで確認できませんでした: original_volume=%.2f", original)


def restore_session_volume(cast: Any, state: dict[str, Any]) -> float | None:
    """手動の音量上昇を尊重して後始末し、終了時の目標音量を返す。"""
    current = getattr(cast.status, "volume_level", None)
    if state.get("original_volume") is None:
        return current
    if current is None:
        raise PyChromecastError("音量復元に必要な機器状態を取得できませんでした")
    last_lowered_to = state.get("last_lowered_to")
    current = get_current_volume(cast)
    if last_lowered_to is not None and current > last_lowered_to + MANUAL_RISE_THRESHOLD:
        logging.info("手動で音量が上げられているため復元しません: current=%.2f", current)
        return current
    restore_volume(cast, state)
    return state["original_volume"]


def is_watching(cast: Any, is_on: bool | None = None) -> bool:
    """MAC 照合済みの点灯、または Backdrop 以外のアプリ起動を視聴中とする。"""
    status = cast.status
    return is_on is True or (status is not None and status.app_id not in (None, "", IDLE_APP_ID))


def status_log_value(value: Any, *, quoted: bool = True) -> str:
    """制御文字を escape し、ログに出す値の長さを制限する。"""
    escaped = repr(value)
    if isinstance(value, str) and not quoted:
        escaped = escaped[1:-1]
    return escaped[:STATUS_LOG_VALUE_MAX_LENGTH]


def log_device_status(
    cast: Any,
    name: str,
    is_on: bool | None = None,
    *,
    volume_source: str = "cast",
    atv: AtvVolume | None = None,
) -> None:
    """視聴判定に使う生値と、判定に使わない CEC 値を 1 行で記録する。"""
    status = cast.status
    logging.info(
        "対象機器の生値: device=%s is_on=%s app_id=%s display_name=%s is_idle=%s "
        "is_stand_by=%s is_active_input=%s volume=%s volume_source=%s%s",
        status_log_value(name, quoted=False),
        is_on,
        status_log_value(getattr(status, "app_id", None)),
        status_log_value(getattr(status, "display_name", None)),
        cast.is_idle,
        getattr(status, "is_stand_by", None),
        getattr(status, "is_active_input", None),
        getattr(status, "volume_level", None),
        volume_source,
        f" atv_level={atv.level}/{atv.maximum}" if atv is not None else "",
    )


def log_tick_summary(
    name: str,
    is_on: bool | None,
    app_id: str | None,
    volume_before: float | None,
    volume_after: float | None,
    count: int,
    result: str,
    *,
    volume_source: str = "cast",
    atv: AtvVolume | None = None,
) -> None:
    """判定時の生値と終了時の目標音量を、tick ごとに 1 行で記録する。"""
    logging.info(
        "tick 要約: device=%s is_on=%s app_id=%s volume_before=%s "
        "volume_after=%s count=%d result=%s volume_source=%s%s",
        status_log_value(name, quoted=False),
        is_on,
        status_log_value(app_id),
        "None" if volume_before is None else f"{volume_before:.2f}",
        "None" if volume_after is None else f"{volume_after:.2f}",
        count,
        result,
        volume_source,
        f" atv_level={atv.level}/{atv.maximum}" if atv is not None else "",
    )


def finish_session(cast: Any, name: str) -> str:
    """ペアリング済みなら電源 OFF、未ペアリングならアプリ終了と日次通知を試す。"""
    expected_mac = load_identity(name)
    if expected_mac is not None:
        return asyncio.run(power_off(cast.cast_info.host, expected_mac))
    try:
        cast.quit_app()
    except Exception as exc:
        # 未ペアリング時の best effort。アプリ終了の失敗でも通知と state 初期化を行う
        logging.warning("アプリ終了に失敗: device=%s error=%s", name, exc)
    message = (
        f"power_off_unavailable device={name} reason=not_paired"
        f"（nemucast-tv-pair --name {name} でペアリングすると電源を切れます）"
    )
    logging.warning(message)
    notify_power_off_unavailable(name, message)
    return "power_off_unavailable"


def usable_cast_volume(value: Any) -> bool:
    """0 固定・未受信・不正な Cast 値を音量として信用しない。"""
    return type(value) in (int, float) and CAST_ZERO_TOLERANCE < value <= 1


def prepare_volume_state(state: dict[str, Any], source: str, maximum: int | None = None) -> None:
    """単位の違う original や連続カウントを引き継がない。旧形式は cast とする。"""
    if state.get("volume_source", "cast") != source or (
        source == "atv" and state.get("volume_max") != maximum
    ):
        log = (
            logging.warning
            if (state.get("original_volume") is not None or state.get("consecutive_lowered", 0) > 0)
            else logging.info
        )
        log("音量源または max の変更によりセッションを初期化: source=%s", source)
        state.update(fresh_state(state["device_name"]))
    state.update(volume_source=source, volume_max=maximum)


def reconcile_atv_lowering(state: dict[str, Any], current: int) -> bool:
    """読み戻せなかった自分の下降範囲なら、復元先と count を保って実値を採用する。"""
    lowering = state.get("atv_lowering")
    if lowering is None:
        return False
    state["atv_lowering"] = None
    if not lowering["to"] <= current <= lowering["from"]:
        return False
    state["last_lowered_to"] = current
    logging.info(
        "ATV 下降途中の読み戻しを回復: from=%d to=%d current=%d count=%d",
        lowering["from"],
        lowering["to"],
        current,
        state["consecutive_lowered"],
    )
    return True


async def run_atv_tick(control: AtvVolumeControl, state: dict[str, Any]) -> TickResult:
    """整数段で下げ、実際に下がった tick または確認済みの下限だけを数える。"""
    current = control.volume.level
    maximum = control.volume.maximum
    lowering = state.get("atv_lowering")
    if reconcile_atv_lowering(state, current):
        # 復旧 tick は実値の確定だけを行い、二重に下降・カウントしない。
        return TickResult.VOLUME_UNCHANGED
    last = state.get("last_lowered_to")
    if lowering is not None or (last is not None and current != last):
        logging.info("手動音量変更: previous=%s current=%d volume_source=atv", last, current)
        discard_pending_restore("manual_volume")
        state.update(consecutive_lowered=0, original_volume=current)
    if state["consecutive_lowered"] >= AUTO_LOWERED_THRESHOLD:
        return TickResult.STANDBY
    if state.get("original_volume") is None:
        state["original_volume"] = current
    # 送信途中で失敗しても復元先を失わない。
    save_state(AUTO_STATE_FILE, state)
    # OFF を試す tick では UP/DOWN とも送らず、直前の音量のまま切る。
    if state["consecutive_lowered"] + 1 >= AUTO_LOWERED_THRESHOLD:
        state["consecutive_lowered"] += 1
        return TickResult.STANDBY
    minimum = max(ATV_MIN_LEVEL, math.ceil(AUTO_MIN_LEVEL * maximum))
    steps = max(ATV_MIN_LEVEL, round(-STEP * maximum))
    target = min(current, max(minimum, current - steps))
    if target < current:
        state["atv_lowering"] = {"from": current, "to": target}
        save_state(AUTO_STATE_FILE, state)
        after = await control.move_to(target)
        state["atv_lowering"] = None
        if after.level >= current:
            logging.warning("ATV 音量低下を確認できないためカウントを進めません")
            return TickResult.VOLUME_UNCHANGED
        logging.info("音量変更: previous=%d current=%d volume_source=atv", current, after.level)
    else:
        logging.info("音量下限のためカウントのみ進めます: current=%d volume_source=atv", current)
    state["last_lowered_to"] = control.volume.level
    state["consecutive_lowered"] += 1
    return (
        TickResult.STANDBY
        if state["consecutive_lowered"] >= AUTO_LOWERED_THRESHOLD
        else TickResult.LOWERED
    )


async def restore_atv_volume(control: AtvVolumeControl, pending: dict[str, Any]) -> bool:
    """送信前に復元範囲を保存し、読み戻せなくても自分の上昇を保留に含める。"""
    if control.volume.level < pending["level"]:
        pending.update(left_min=control.volume.level, left_at=pending["level"])
        save_state(AUTO_PENDING_RESTORE_FILE, pending, touch=False)
    try:
        after = await control.move_to(pending["level"])
        if abs(after.level - pending["level"]) > ATV_RESTORE_TOLERANCE:
            raise TvVolumeError("ATV 音量の復元先との差が 1 段を超えています")
    except TvPowerError as exc:
        logging.warning("音量復元を読み戻しで確認できませんでした: %s", exc)
        return False
    logging.info("音量復元を読み戻しで確認: original_volume=%d volume_source=atv", pending["level"])
    discard_pending_restore("restored")
    return True


async def handle_pending_restore(name: str, control: AtvVolumeControl) -> str | None:
    """保留が現在の機器・音量に一致する時だけ復元し、その tick を終える。"""
    pending = load_pending_restore(name)
    if pending is None:
        return None
    if pending["max"] != control.volume.maximum:
        discard_pending_restore("max_changed")
        return None
    left_min = pending.get("left_min", pending["left_at"])
    if not left_min <= control.volume.level <= pending["left_at"]:
        discard_pending_restore("manual_volume")
        return None
    return "restored" if await restore_atv_volume(control, pending) else "restore_pending"


def volume_unavailable(cast: Any, name: str, state: dict[str, Any], is_on: bool | None) -> None:
    """不明な音量でカウントや電源を操作せず、日次通知と要約を残す。"""
    message = (
        f"volume_unavailable device={status_log_value(name, quoted=False)} "
        "音量を取得できないため電源を切りません"
    )
    logging.warning(message)
    today = datetime.now().date().isoformat()
    if load_power_off_alert(AUTO_VOLUME_ALERT_FILE, name) != today:
        delivered = send_discovery_alert(message)
        alert = {"device_name": name, "pending": not delivered}
        if delivered:
            alert["alerted_on"] = today
        try:
            save_state(AUTO_VOLUME_ALERT_FILE, alert)
        except OSError as exc:
            logging.warning("音量取得不可通知 state を更新できませんでした: %s", exc)
    logging.info("tick 結果: volume_unavailable device=%s", status_log_value(name, quoted=False))
    log_tick_summary(
        name,
        is_on,
        getattr(cast.status, "app_id", None),
        None,
        None,
        state["consecutive_lowered"],
        "volume_unavailable",
        volume_source="none",
    )


async def process_atv_device(
    cast: Any,
    name: str,
    state: dict[str, Any],
    is_on: bool | None,
    expected_mac: str | None,
    *,
    cleanup: bool,
    pending_only: bool = False,
) -> None:
    """ATV の保留があるか Cast 音量を信用できない時に使い、取得不能なら保留する。"""
    # 消灯中の volume_info は 0/1 になり得る。読まず、保存済みの段だけを引き継ぐ。
    if is_on is False and expected_mac is not None:
        if state.get("volume_source", "cast") != "atv" and state.get("original_volume") is not None:
            log_atv_decision("volume_unavailable", is_on, None, state)
            volume_unavailable(cast, name, state, is_on)
            return
        log_atv_decision("cleanup" if cleanup else "not_watching", is_on, None, state)
        if not pending_only:
            defer_atv_restore(name, state, cleanup=True)
            save_state(AUTO_STATE_FILE, fresh_state(name))
        log_device_status(cast, name, is_on, volume_source="none")
        logging.info(
            "tick 結果: %s device=%s", "outside_window" if pending_only else "not_watching", name
        )
        log_tick_summary(
            name,
            is_on,
            getattr(cast.status, "app_id", None),
            None,
            None,
            0,
            "outside_window" if pending_only else "not_watching",
            volume_source="none",
        )
        return
    if not is_watching(cast, is_on) and state.get("original_volume") is None:
        log_atv_decision("not_watching", is_on, None, state)
        log_device_status(cast, name, is_on, volume_source="none")
        save_state(AUTO_STATE_FILE, fresh_state(name))
        logging.info("tick 結果: not_watching device=%s", name)
        log_tick_summary(
            name,
            is_on,
            getattr(cast.status, "app_id", None),
            None,
            None,
            0,
            "not_watching",
            volume_source="none",
        )
        return
    control = AtvVolumeControl(cast.cast_info.host, expected_mac) if expected_mac else None
    volume = None
    try:
        volume = await control.read() if control is not None else None
        log_device_status(cast, name, is_on, volume_source="atv" if volume else "none", atv=volume)
        if volume is None:
            log_atv_decision("volume_unavailable", is_on, None, state)
            volume_unavailable(cast, name, state, is_on)
            return
        prepare_volume_state(state, "atv", volume.maximum)
        await complete_atv_tick(
            cast, name, state, is_on, control, cleanup=cleanup, pending_only=pending_only
        )
    except TvVolumeError as exc:
        logging.warning("ATV 音量制御を保留: %s", exc)
        if volume is None:
            log_atv_decision("volume_unavailable", is_on, None, state)
            log_device_status(cast, name, is_on, volume_source="none")
        volume_unavailable(cast, name, state, is_on)
    finally:
        if control is not None:
            control.disconnect()


async def complete_atv_tick(
    cast: Any,
    name: str,
    state: dict[str, Any],
    is_on: bool | None,
    control: AtvVolumeControl,
    *,
    cleanup: bool,
    pending_only: bool = False,
) -> None:
    """ATV の判断と終了時の実値を一行ずつ記録する。"""
    before = control.volume.level
    branch = atv_branch(state, is_on, before, cleanup=cleanup)
    log_atv_decision(branch, is_on, before, state)
    outcome, count = await execute_atv_tick(
        cast, name, state, is_on, control, cleanup=cleanup, pending_only=pending_only
    )
    logging.info("tick 結果: %s count=%d", outcome, count)
    log_tick_summary(
        name,
        is_on,
        getattr(cast.status, "app_id", None),
        before,
        control.volume.level,
        count,
        outcome,
        volume_source="atv",
        atv=control.volume,
    )


def atv_branch(state: dict[str, Any], is_on: bool | None, level: int, *, cleanup: bool) -> str:
    """通常処理・人の変更・OFF 再試行・保留復元を区別する。"""
    if cleanup or is_on is not True:
        return "cleanup" if cleanup else "not_watching"
    lowering = state.get("atv_lowering")
    if lowering is not None:
        return "normal" if lowering["to"] <= level <= lowering["from"] else "restart_manual"
    last = state.get("last_lowered_to")
    if state["consecutive_lowered"] >= AUTO_LOWERED_THRESHOLD:
        return "restart_manual" if last is not None and level != last else "pending_retry"
    if load_pending_restore(state["device_name"]):
        return "pending_restore"
    return "restart_manual" if last is not None and level != last else "normal"


async def execute_atv_tick(
    cast: Any,
    name: str,
    state: dict[str, Any],
    is_on: bool | None,
    control: AtvVolumeControl,
    *,
    cleanup: bool,
    pending_only: bool,
) -> tuple[str, int]:
    """点灯確認済みの通常 tick 以外では音量を下げず、OFF 前にも復元しない。"""
    if cleanup or is_on is not True:
        lowering = state.get("atv_lowering")
        recovered = reconcile_atv_lowering(state, control.volume.level)
        last = state.get("last_lowered_to")
        manual = (lowering is not None and not recovered) or (
            last is not None and control.volume.level != last
        )
        if is_on is True and manual:
            discard_pending_restore("manual_volume")
        else:
            defer_atv_restore(name, state, cleanup=True)
        save_state(AUTO_STATE_FILE, fresh_state(name))
        return ("cleanup" if cleanup else "not_watching"), 0
    # OFF 失敗後は復元より先に人の操作を判定し、同値ならキーなしで再試行する。
    retry = state["consecutive_lowered"] >= AUTO_LOWERED_THRESHOLD
    if not retry or pending_only:
        restored = await handle_pending_restore(name, control)
        if restored is not None:
            save_state(AUTO_STATE_FILE, fresh_state(name))
            return restored, 0
    if pending_only:
        return "outside_window", 0
    result = await run_atv_tick(control, state)
    save_state(AUTO_STATE_FILE, state)
    count = state["consecutive_lowered"]
    if result is not TickResult.STANDBY:
        return result.value, count
    defer_atv_restore(name, state)
    control.disconnect()
    try:
        outcome = await power_off(cast.cast_info.host, control.expected_mac)
    except PowerOffUnconfirmedError as exc:
        if not exc.off_observed:
            raise
        logging.info("OFF 観測後の状態が不明のため、点け直しの可能性を優先して終了: %s", exc)
        outcome = "turned_on_again"
    save_state(AUTO_STATE_FILE, fresh_state(name))
    return ("woken" if outcome == "turned_on_again" else outcome), count


def process_device(cast: Any, name: str, state: dict[str, Any], *, cleanup: bool) -> None:
    """ペアリング済みなら機器照合後に制御し、終了時は音量を復元する。"""
    cast.wait(timeout=CONNECT_TIMEOUT_SEC)
    expected_mac = load_identity(name)
    is_on = None
    if expected_mac is not None:
        is_on = asyncio.run(read_power_state(cast.cast_info.host, expected_mac))
    pending_atv = expected_mac is not None and load_pending_restore(name) is not None
    if pending_atv or not usable_cast_volume(getattr(cast.status, "volume_level", None)):
        asyncio.run(process_atv_device(cast, name, state, is_on, expected_mac, cleanup=cleanup))
        return
    prepare_volume_state(state, "cast")
    log_device_status(cast, name, is_on)
    volume_before = getattr(cast.status, "volume_level", None)
    app_id = getattr(cast.status, "app_id", None)
    watching = is_watching(cast, is_on)
    if not watching or cleanup:
        volume_after = restore_session_volume(cast, state)
        save_state(AUTO_STATE_FILE, fresh_state(name))
        outcome = "cleanup" if watching else "not_watching"
        logging.info("tick 結果: %s device=%s", outcome, name)
        log_tick_summary(name, is_on, app_id, volume_before, volume_after, 0, outcome)
        return
    pending = state.get("consecutive_lowered", 0) >= AUTO_LOWERED_THRESHOLD
    result, state = run_tick(cast, state)
    if result is TickResult.VOLUME_UNAVAILABLE:
        volume_unavailable(cast, name, state, is_on)
        return
    volume_after = volume_before if pending else state["last_lowered_to"]
    # 復元・電源OFF前に保存し、失敗や中断時も original_volume とカウントを残す
    save_state(AUTO_STATE_FILE, state)
    if result is TickResult.STANDBY:
        if AUTO_RESTORE_VOLUME:
            restore_volume(cast, state)
            if state.get("original_volume") is not None:
                volume_after = state["original_volume"]
        outcome = finish_session(cast, name)
        save_state(AUTO_STATE_FILE, fresh_state(name))
        logging.info("tick 結果: %s device=%s", outcome, name)
    else:
        outcome = result.value
        logging.info("tick 結果: %s count=%d", result.value, state["consecutive_lowered"])
    log_tick_summary(
        name, is_on, app_id, volume_before, volume_after, state["consecutive_lowered"], outcome
    )


def cleanup_session(name: str, state: dict[str, Any]) -> None:
    """時間外の進行中セッションだけ後始末する。失敗時は次 tick まで保持する。"""
    browser = None
    try:
        cast, browser = discover_chromecasts(name)
        if cast is None:
            logging.info("tick 結果: not_found device=%s", name)
            logging.warning("時間外の音量復元を保留: device=%s が見つかりません", name)
            return
        process_device(cast, name, state, cleanup=True)
    except (TvPowerError, PyChromecastError, OSError, RuntimeError) as exc:
        logging.warning("時間外の音量復元を保留: %s", exc)
        log_error_summary(name, state)
    finally:
        stop_discovery(browser)


def log_error_summary(name: str, state: dict[str, Any]) -> None:
    """確定できない観測値を None として例外 tick も集計に残す。"""
    log_tick_summary(
        name,
        None,
        None,
        None,
        None,
        state["consecutive_lowered"],
        "error",
        volume_source=state.get("volume_source", "none"),
    )


def restore_pending_outside_window(name: str, state: dict[str, Any]) -> None:
    """窓外・進行中セッションなしなら、点灯した対象の保留復元だけ試す。"""
    browser = None
    try:
        cast, browser = discover_chromecasts(name)
        if cast is None:
            logging.info("tick 結果: not_found device=%s", name)
            return
        cast.wait(timeout=CONNECT_TIMEOUT_SEC)
        expected_mac = load_identity(name)
        if expected_mac is None:
            return
        is_on = asyncio.run(read_power_state(cast.cast_info.host, expected_mac))
        asyncio.run(
            process_atv_device(
                cast, name, state, is_on, expected_mac, cleanup=False, pending_only=True
            )
        )
    except (TvPowerError, PyChromecastError, OSError, RuntimeError) as exc:
        logging.warning("時間外の保留復元を保留: %s", exc)
        log_error_summary(name, state)
    finally:
        stop_discovery(browser)


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
        description=("15分間隔で Chromecast の音量を下げ、3 回連続で下げ続けたら電源OFF にする")
    )
    parser.add_argument(
        "-n", "--name", type=str, default=AUTO_TARGET_NAME,
        help=f"対象 Chromecast 名。デフォルト: {AUTO_TARGET_NAME}",
    )  # fmt: skip
    return parser.parse_args(args)


def send_discovery_alert(message: str, command: str | None = None) -> bool:
    """通知コマンドに本文を渡して実行し、届いたかを返す。失敗しても寝かしつけ本体は止めない。"""
    command = DISCOVERY_ALERT_COMMAND if command is None else command
    if not command:
        return True
    try:
        subprocess.run(
            [*shlex.split(command), message],
            check=True,
            timeout=DISCOVERY_ALERT_TIMEOUT_SEC,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        logging.warning("通知コマンドが失敗しました: %s stderr=%s", exc, exc.stderr)
        return False
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # ValueError は DISCOVERY_ALERT_COMMAND の引用符が閉じていない等の書式ミス
        logging.warning("通知コマンドを実行できませんでした: %s", exc)
        return False
    return True


def load_power_off_alert(alert_file: Path, name: str) -> str | None:
    """未ペアリング通知の送信日を読む。破損・対象変更時は未通知として扱う。"""
    try:
        data = json.loads(alert_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("device_name") != name:
            return None
        alerted_on = data.get("alerted_on")
        if (
            not isinstance(alerted_on, str)
            or date.fromisoformat(alerted_on).isoformat() != alerted_on
        ):
            return None
        return alerted_on
    except (OSError, ValueError):
        return None


def notify_power_off_unavailable(name: str, message: str) -> None:
    """同日の通知成功後だけ抑制する。通知・保存の失敗で本体を止めない。"""
    today = datetime.now().date().isoformat()
    if load_power_off_alert(AUTO_POWER_OFF_ALERT_FILE, name) == today:
        return
    delivered = send_discovery_alert(message)
    alert = {"device_name": name, "pending": not delivered}
    if delivered:
        alert["alerted_on"] = today
    try:
        save_state(AUTO_POWER_OFF_ALERT_FILE, alert)
    except OSError as exc:
        logging.warning("電源 OFF 不可通知 state を更新できませんでした: %s", exc)


def retry_power_off_alert(name: str) -> None:
    """セッション初期化後も前 tick で届かなかった通知だけを再送する。"""
    try:
        alert = json.loads(AUTO_POWER_OFF_ALERT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if (
        not isinstance(alert, dict)
        or alert.get("device_name") != name
        or alert.get("pending") is not True
    ):
        return
    if load_identity(name) is not None:
        alert["pending"] = False
        try:
            save_state(AUTO_POWER_OFF_ALERT_FILE, alert)
        except OSError as exc:
            logging.warning("電源 OFF 不可通知 state を更新できませんでした: %s", exc)
        return
    notify_power_off_unavailable(
        name,
        f"power_off_unavailable device={name} reason=not_paired"
        f"（nemucast-tv-pair --name {name} でペアリングすると電源を切れます）",
    )


def load_discovery_alert(alert_file: Path) -> dict[str, Any]:
    """通知フラグを読む。無い・壊れている時は初期値に戻す（毎 tick 落ち続けないため）。"""
    default: dict[str, Any] = {"zero_ticks": 0, "alerted": False}
    try:
        data = json.loads(alert_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # JSONDecodeError・UnicodeDecodeError を含む
        return default
    if (
        not isinstance(data, dict)
        # bool は int のサブクラスなので type で比べる。負数は本物の障害を長く隠すため弾く
        or type(data.get("zero_ticks")) is not int
        or data["zero_ticks"] < 0
        or not isinstance(data.get("alerted"), bool)
    ):
        return default
    return data


def update_discovery_alert(
    lan_has_devices: bool,
    alert_file: Path | None = None,
    after_ticks: int | None = None,
) -> bool:
    """LAN 上 0 台の連続回数を数え、障害と確定したかを返す。

    通知は障害に入った時と回復した時の 1 回ずつ。通知が届かなかった時は
    フラグを進めず、次の tick で送り直す。
    """
    alert_file = DISCOVERY_ALERT_FILE if alert_file is None else alert_file
    after_ticks = DISCOVERY_ALERT_AFTER_TICKS if after_ticks is None else after_ticks
    if after_ticks <= 0:
        return False
    alert = load_discovery_alert(alert_file)

    if lan_has_devices:
        if not alert_file.exists():
            return False
        if alert["alerted"]:
            logging.info("LAN 上の Chromecast が再び見えるようになりました。")
            if not send_discovery_alert(
                "nemucast: LAN 上の Chromecast が再び見えるようになりました。"
            ):
                return False
        alert_file.unlink(missing_ok=True)
        return False

    if not alert["alerted"] and is_state_stale(alert):
        # スリープ等で間が空いた前回の 0 台は連続と数えない
        # （復帰直後の一瞬の 0 台で誤報しないため）。
        # 通知済みの障害は継続中として扱い、カウントを戻さない
        alert["zero_ticks"] = 0
    alert["zero_ticks"] += 1
    confirmed = alert["zero_ticks"] >= after_ticks
    if confirmed and not alert["alerted"]:
        alert["alerted"] = send_discovery_alert(
            "nemucast: LAN 上の Chromecast が 1 台も見えません。寝かしつけが止まっています。"
            "periodic-worker のローカルネットワーク権限（システム設定 > プライバシーとセキュリティ"
            " > ローカルネットワーク）か、ネットワークを確認してください。"
        )
    save_state(alert_file, alert)
    return confirmed


def load_not_found_alert(alert_file: Path, name: str) -> dict[str, Any]:
    """目的名の不在通知 state を読む。壊れている時や対象変更時は初期化する。"""
    default: dict[str, Any] = {"device_name": name, "missing_ticks": 0, "alerted": False}
    try:
        data = json.loads(alert_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    if (
        not isinstance(data, dict)
        or data.get("device_name") != name
        or type(data.get("missing_ticks")) is not int
        or data["missing_ticks"] < 0
        or type(data.get("alerted")) is not bool
    ):
        return default
    return data


def update_not_found_alert(name: str, found: bool) -> None:
    """窓内だけで不在を数え、途切れごとの障害・回復を一度ずつ通知する。"""
    if AUTO_NOT_FOUND_ALERT_AFTER_TICKS <= 0:
        return
    alert = load_not_found_alert(AUTO_NOT_FOUND_ALERT_FILE, name)
    if found:
        if alert["alerted"] and not send_discovery_alert(
            f"nemucast: 目的の Chromecast '{name}' が再び見えるようになりました。"
        ):
            return
        AUTO_NOT_FOUND_ALERT_FILE.unlink(missing_ok=True)
        return
    if not alert["alerted"] and is_state_stale(alert):
        alert["missing_ticks"] = 0
    alert["missing_ticks"] += 1
    if alert["missing_ticks"] >= AUTO_NOT_FOUND_ALERT_AFTER_TICKS and not alert["alerted"]:
        alert["alerted"] = send_discovery_alert(
            f"nemucast: 目的の Chromecast '{name}' が夜間窓内で "
            f"{alert['missing_ticks']} tick 連続で見つかりません。寝かしつけが止まっています。"
        )
    save_state(AUTO_NOT_FOUND_ALERT_FILE, alert)


def main(args: list[str] | None = None) -> None:
    parsed = parse_args(args)
    setup_logging()

    state = load_state(AUTO_STATE_FILE, parsed.name) or fresh_state(parsed.name)
    retry_power_off_alert(parsed.name)
    try:
        night = AUTO_NIGHT_FORCE or in_night_window(
            datetime.now(), AUTO_NIGHT_START, AUTO_NIGHT_END
        )
    except ValueError as exc:
        logging.error("夜間時刻の設定が不正です: %s", exc)
        sys.exit(1)
    if not night:
        if state.get("original_volume") is None:
            if load_pending_restore(parsed.name):
                restore_pending_outside_window(parsed.name, state)
                return
            logging.info("tick 結果: outside_window")
            return
        if not is_session_continuable(state):
            cleanup_session(parsed.name, state)
            return
        logging.info("夜間窓外の進行中セッションを続行: device=%s", parsed.name)

    cast, browser = discover_chromecasts(parsed.name)
    if night:
        try:
            update_not_found_alert(parsed.name, found=cast is not None)
        except OSError as exc:
            # 通知 state の I/O 失敗で寝かしつけ本体を止めない
            logging.warning("不在通知 state を更新できませんでした: %s", exc)
    if cast is None:
        logging.info("tick 結果: not_found device=%s", parsed.name)
    if not night and cast is None:
        stop_discovery(browser)
        return
    lan_has_devices = browser is not None and len(browser.devices) > 0
    if night and not lan_has_devices:
        stop_discovery(browser)
        # 目的デバイスが寝ていても他の Chromecast は常時 mDNS に出るので、
        # 0 台が続くのは環境側の障害。
        # macOS のローカルネットワーク権限が外れると LAN 宛 sendto が EHOSTUNREACH になり、
        # 例外にならず 0 台として返ってくるため、ここで失敗として表に出す
        if update_discovery_alert(lan_has_devices=False):
            logging.error(
                "LAN 上の Chromecast が 1 台も見えません。ローカルネットワーク権限か"
                "ネットワークを確認してください。"
            )
            sys.exit(EXIT_NO_DEVICE_ON_LAN)
        logging.warning("LAN 上の Chromecast が 1 台も見えません（一時的な可能性あり）。")
        return
    if night:
        update_discovery_alert(lan_has_devices=True)
    if cast is None:
        # 24時間 15分間隔で回すため、デバイスが mDNS に出ない時間帯の方が長い。
        # 「寝かしつける対象がない = やることがない」は成功扱いにしないと、
        # 1日最大96回の失敗通知で本物の障害が埋もれる。
        logging.warning("デバイス '%s' が見つからないため何もしません。", parsed.name)
        stop_discovery(browser)
        return

    try:
        process_device(cast, parsed.name, state, cleanup=False)
    except (TvPowerError, PyChromecastError) as exc:
        message = f"nemucast: テレビ '{parsed.name}' の夜間制御に失敗しました: {exc}"
        logging.error(message)
        log_error_summary(parsed.name, state)
        # 一晩 15 分おきの通知で埋もれないよう、通知は進行中（音量を下げ始めた後）だけ
        if state.get("original_volume") is not None:
            send_discovery_alert(message)
        sys.exit(1)
    finally:
        stop_discovery(browser)


if __name__ == "__main__":
    main()
