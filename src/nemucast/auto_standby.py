"""15分間隔の最小ロジックで Chromecast を寝かしつけるスクリプト。

ロジック:
  1. 電源OFF状態 (powered_off=True) なら、音量上昇または active app を再開として扱う。
     どちらも見えなければ何もしない。
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
import shlex
import subprocess
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


def is_state_stale(state: dict[str, Any], stale_sec: int = AUTO_STATE_STALE_SEC) -> bool:
    """前回更新から stale_sec 以上空いたか判定する。"""
    updated_at = state.get("updated_at")
    if not isinstance(updated_at, int | float):
        return True
    return time.time() - updated_at > stale_sec


def load_state(state_file: Path, device_name: str) -> dict[str, Any] | None:
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # 壊れた state を放置すると毎 tick 例外で落ち続け、15 分間隔のジョブが自力復帰できない
        logging.warning("state を読めなかったため作り直します: %s", exc)
        return None
    if not isinstance(data, dict) or data.get("device_name") != device_name:
        return None
    if is_state_stale(data):
        # powered_off は「音量上昇 / active app まで何もしない」という不変条件なので跨いで維持する
        if data.get("powered_off"):
            return data
        # スケジューラ停止（スリープ等）を挟むと連続カウントが実態とずれ、
        # 視聴再開直後に quit_app してしまうため、カウントは捨てて数え直す
        logging.info("前回更新から時間が空いたため連続カウントをリセットします。")
        return fresh_state(device_name)
    return data


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


def get_active_app_label(cast: Any) -> str | None:
    """Return the Chromecast app label when the device appears active."""
    status = getattr(cast, "status", None)
    if status is None:
        return None

    for attr in ("app_id", "display_name"):
        value = getattr(status, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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
        active_app = get_active_app_label(cast)
        volume_rose = baseline is not None and current > baseline + rise_threshold
        if not volume_rose and active_app is None:
            logging.info("電源OFF状態を維持。何もしません。")
            return TickResult.SKIP, state
        if volume_rose:
            logging.info(
                "音量上昇を検知 (%.2f → %.2f)。state をリセットして再開します。",
                baseline,
                current,
            )
        else:
            logging.info("active app (%s) を検知。state をリセットして再開します。", active_app)
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
    except (OSError, json.JSONDecodeError):
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
        # timeout なしだと discovery 後に落ちたデバイス相手に無限待ちになり、
        # 15分ごとにプロセスが積み上がる
        cast.wait(timeout=CONNECT_TIMEOUT_SEC)
        state = load_state(AUTO_STATE_FILE, parsed.name) or fresh_state(parsed.name)
        result, new_state = run_tick(cast=cast, state=state)
        save_state(AUTO_STATE_FILE, new_state)
        logging.info("tick 結果: %s", result.value)
    finally:
        stop_discovery(browser)


if __name__ == "__main__":
    main()
