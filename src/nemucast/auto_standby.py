"""夜間だけ音量を段階的に下げ、テレビ本体の電源OFFを確認する。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

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
    AUTO_RESTORE_VOLUME,
    CHROMECAST_NAME,
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
from nemucast.tv_power import TvPowerError, load_identity, power_off, read_power_state

AUTO_MIN_LEVEL = float(os.getenv("AUTO_MIN_LEVEL", "0.05"))
AUTO_LOWERED_THRESHOLD = int(os.getenv("AUTO_LOWERED_THRESHOLD", "3"))
AUTO_STATE_FILE = Path(os.getenv("AUTO_STATE_FILE", f"{LOG_DIR}/auto_standby_state.json"))
# 実行間隔 15 分の 2 倍。これを超えて間が空いた state は連続カウントを信用しない
AUTO_STATE_STALE_SEC = int(os.getenv("AUTO_STATE_STALE_SEC", "1800"))
DISCOVERY_ALERT_TIMEOUT_SEC = 60
# LAN 上のデバイスが 0 台の時の終了コード。デバイス不在（成功扱い）と区別するため非ゼロにする
EXIT_NO_DEVICE_ON_LAN = 2


class TickResult(Enum):
    LOWERED = "lowered"
    STANDBY = "standby"


def fresh_state(device_name: str) -> dict[str, Any]:
    return {
        "device_name": device_name,
        "last_lowered_to": None,
        "consecutive_lowered": 0,
        "original_volume": None,
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
    for key in ("original_volume", "last_lowered_to"):
        value = data.get(key)
        if value is not None and (type(value) not in (int, float) or not 0 <= value <= 1):
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


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.time()
    # 書き込み途中で kill されても壊れた JSON を残さないよう、一時ファイル経由で置き換える
    tmp_file = state_file.with_name(f"{state_file.name}.tmp")
    tmp_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_file, state_file)


def in_night_window(now: datetime, start: str, end: str) -> bool:
    """ローカル時刻が夜間窓 [start, end) に入るか返す。同時刻なら空の窓。"""
    start_time = datetime.strptime(start, "%H:%M").time()
    end_time = datetime.strptime(end, "%H:%M").time()
    current = now.time().replace(tzinfo=None)
    if start_time <= end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


def run_tick(
    cast: Any,
    state: dict[str, Any],
    *,
    step: float = STEP,
    min_level: float = AUTO_MIN_LEVEL,
    lowered_threshold: int = AUTO_LOWERED_THRESHOLD,
    rise_threshold: float = MANUAL_RISE_THRESHOLD,
) -> tuple[TickResult, dict[str, Any]]:
    """点灯確認済みのテレビで音量を下げる。電源制御は呼び出し側で行う。"""
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
    """点灯確認済みのテレビをセッション開始時の音量に戻す。"""
    original = state.get("original_volume")
    if original is not None:
        cast.set_volume(original)
        logging.info("音量復元: original_volume=%.2f", original)


def process_device(cast: Any, name: str, state: dict[str, Any], *, cleanup: bool) -> None:
    """電源状態を先に確認し、点灯中だけ音量操作と電源OFFを行う。"""
    host = cast.cast_info.host
    expected_mac = load_identity(name)
    if not asyncio.run(read_power_state(host, expected_mac)):
        save_state(AUTO_STATE_FILE, fresh_state(name))
        logging.info("tick 結果: already_off device=%s", name)
        return
    cast.wait(timeout=CONNECT_TIMEOUT_SEC)
    if cleanup:
        last_lowered_to = state.get("last_lowered_to")
        current = get_current_volume(cast)
        if last_lowered_to is not None and current > last_lowered_to + MANUAL_RISE_THRESHOLD:
            # 窓の終わり際に手動で上げられた音量を、古い元音量で上書きしない
            logging.info("手動で音量が上げられているため復元しません: current=%.2f", current)
        else:
            restore_volume(cast, state)
        save_state(AUTO_STATE_FILE, fresh_state(name))
        logging.info("tick 結果: cleanup device=%s", name)
        return
    result, state = run_tick(cast, state)
    # 復元・電源OFF前に保存し、失敗や中断時も original_volume とカウントを残す
    save_state(AUTO_STATE_FILE, state)
    if result is TickResult.STANDBY:
        if AUTO_RESTORE_VOLUME:
            restore_volume(cast, state)
        outcome = asyncio.run(power_off(host, expected_mac))
        save_state(AUTO_STATE_FILE, fresh_state(name))
        logging.info("tick 結果: %s device=%s", outcome, name)
        return
    logging.info("tick 結果: %s count=%d", result.value, state["consecutive_lowered"])


def cleanup_session(name: str, state: dict[str, Any]) -> None:
    """時間外の進行中セッションだけ後始末する。失敗時は次 tick まで保持する。"""
    browser = None
    try:
        cast, browser = discover_chromecasts(name)
        if cast is None:
            logging.warning("時間外の音量復元を保留: device=%s が見つかりません", name)
            return
        process_device(cast, name, state, cleanup=True)
    except (TvPowerError, PyChromecastError, OSError, RuntimeError) as exc:
        logging.warning("時間外の音量復元を保留: %s", exc)
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
        "-n", "--name", type=str, default=CHROMECAST_NAME,
        help=f"対象 Chromecast 名。デフォルト: {CHROMECAST_NAME}",
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


def main(args: list[str] | None = None) -> None:
    parsed = parse_args(args)
    setup_logging()

    state = load_state(AUTO_STATE_FILE, parsed.name) or fresh_state(parsed.name)
    try:
        night = AUTO_NIGHT_FORCE or in_night_window(
            datetime.now(), AUTO_NIGHT_START, AUTO_NIGHT_END
        )
    except ValueError as exc:
        logging.error("夜間時刻の設定が不正です: %s", exc)
        sys.exit(1)
    if not night:
        if state.get("original_volume") is not None:
            cleanup_session(parsed.name, state)
        else:
            logging.info("tick 結果: outside_window")
        return

    cast, browser = discover_chromecasts(parsed.name)
    lan_has_devices = browser is not None and len(browser.devices) > 0
    if not lan_has_devices:
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
        # 一晩 15 分おきの通知で埋もれないよう、通知は進行中（音量を下げ始めた後）だけ
        if state.get("original_volume") is not None:
            send_discovery_alert(message)
        sys.exit(1)
    finally:
        stop_discovery(browser)


if __name__ == "__main__":
    main()
