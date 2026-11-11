"""環境変数から読み込む設定定数を集約する。

.env を load し、モジュール読み込み時に各定数を確定させる。
CLI / state 管理 / Chromecast 接続の各モジュールはここを参照する。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ログ出力先ディレクトリ（実行ログ・state JSON の保存先）
LOG_DIR = os.getenv("LOG_DIR", "logs")

CHROMECAST_NAME = os.getenv("CHROMECAST_NAME", "Dell")
STEP = float(os.getenv("STEP", "-0.04"))
MIN_LEVEL = float(os.getenv("MIN_LEVEL", "0.3"))
DEFAULT_INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "1200"))
INACTIVE_THRESHOLD = int(os.getenv("INACTIVE_THRESHOLD", "3"))
MANUAL_RISE_THRESHOLD = float(os.getenv("MANUAL_RISE_THRESHOLD", "0.01"))
DEFAULT_STATE_FILE = os.getenv("STATE_FILE", f"{LOG_DIR}/activity_state.json")
RUN_UNTIL_STANDBY = os.getenv("RUN_UNTIL_STANDBY", "0") == "1"
STATE_STALE_INTERVAL_MULTIPLIER = int(os.getenv("STATE_STALE_INTERVAL_MULTIPLIER", "2"))

# ローカル時刻の夜間窓。終了時刻は含めない。FORCE は昼間の動作確認用
AUTO_TARGET_NAME = os.getenv("AUTO_TARGET_NAME", "Dell")
AUTO_NIGHT_START = os.getenv("AUTO_NIGHT_START", "21:00")
AUTO_NIGHT_END = os.getenv("AUTO_NIGHT_END", "06:00")
AUTO_NIGHT_FORCE = os.getenv("AUTO_NIGHT_FORCE", "0") == "1"
AUTO_RESTORE_VOLUME = os.getenv("AUTO_RESTORE_VOLUME", "1") == "1"
# 音量復元後、テレビが反映した値を読み戻して確かめる待ち時間と許容差。set_volume は応答を待たず、
# 反映前に電源を切ると翌日の音量が 0 になった（2026-11-05 実機）
AUTO_RESTORE_WAIT_SEC = float(os.getenv("AUTO_RESTORE_WAIT_SEC", "5"))
AUTO_RESTORE_TOLERANCE = float(os.getenv("AUTO_RESTORE_TOLERANCE", "0.015"))
# ATV の初回音量 push と、音量キー送信後の更新通知を待つ秒数
AUTO_ATV_VOLUME_WAIT_SEC = float(os.getenv("AUTO_ATV_VOLUME_WAIT_SEC", "5"))
AUTO_VOLUME_ALERT_FILE = Path(
    os.getenv("AUTO_VOLUME_ALERT_FILE", f"{LOG_DIR}/auto_volume_alert_state.json")
)

# Chromecast を quit_app した後、standby 完了を待つ秒数
STANDBY_WAIT_SEC = int(os.getenv("STANDBY_WAIT_SEC", "2"))

# 接続確立（cast.wait）を待つ秒数。省略すると無限待ちになるため必ず指定する
CONNECT_TIMEOUT_SEC = int(os.getenv("CONNECT_TIMEOUT_SEC", "30"))

# LAN 上に Chromecast が 1 台も見えない状態に入った時・抜けた時に 1 回ずつ実行する通知コマンド。
# 本文を末尾の引数として渡す。空なら通知しない
DISCOVERY_ALERT_COMMAND = os.getenv("DISCOVERY_ALERT_COMMAND", "")
DISCOVERY_ALERT_FILE = Path(
    os.getenv("DISCOVERY_ALERT_FILE", f"{LOG_DIR}/discovery_alert_state.json")
)
# LAN 上 0 台が何 tick 続いたら障害とみなすか（スリープ復帰直後の一瞬の 0 台で誤報しないため）。
# 0 なら判定しない（対象の Chromecast が LAN に 1 台しかない家庭向け）
DISCOVERY_ALERT_AFTER_TICKS = int(os.getenv("DISCOVERY_ALERT_AFTER_TICKS", "2"))

# 発見一覧には目的名があるのに接続候補が空だった時の追加探索回数と待ち秒数
DISCOVERY_MAX_RETRIES = int(os.getenv("DISCOVERY_MAX_RETRIES", "3"))
DISCOVERY_RETRY_WAIT_SEC = float(os.getenv("DISCOVERY_RETRY_WAIT_SEC", "1"))

# 夜間窓内の目的デバイス不在を、LAN 全体の 0 台検知とは別に記録する
AUTO_NOT_FOUND_ALERT_AFTER_TICKS = int(os.getenv("AUTO_NOT_FOUND_ALERT_AFTER_TICKS", "4"))
AUTO_NOT_FOUND_ALERT_FILE = Path(
    os.getenv("AUTO_NOT_FOUND_ALERT_FILE", f"{LOG_DIR}/auto_not_found_alert_state.json")
)

# 未ペアリングで電源を切れない時の通知日（ローカル日付、1 日 1 回まで）
AUTO_POWER_OFF_ALERT_FILE = Path(
    os.getenv("AUTO_POWER_OFF_ALERT_FILE", f"{LOG_DIR}/auto_power_off_alert_state.json")
)

# ログファイルのローテーション設定
LOG_ROTATION_MAX_BYTES = int(os.getenv("LOG_ROTATION_MAX_BYTES", str(512 * 1024)))
LOG_ROTATION_BACKUP_COUNT = int(os.getenv("LOG_ROTATION_BACKUP_COUNT", "1"))

MAX_HISTORY_ENTRIES = 20

# Android TV Remote v2 (テレビ本体の電源制御) のクライアント証明書・鍵の置き場所。
# ペアリング結果の秘密鍵なので Git 管理外のホームに 0600 で置く
TV_CERT_DIR = Path(os.getenv("TV_CERT_DIR", str(Path.home() / ".config" / "nemucast"))).expanduser()

# テレビ電源 OFF の送信キー。KEYCODE_SLEEP / SOFT_SLEEP はこの機種 (AQUOS-4KTVJ26-1) で無反応、
# POWER が効く（2026-11-05 実機確認）。POWER はトグルなので必ず is_on を見てから送る
TV_POWER_KEY = os.getenv("TV_POWER_KEY", "POWER")
# 送信後に is_on=False を待つ秒数と、送信の最大回数（リトライ込み）
TV_POWER_WAIT_SEC = int(os.getenv("TV_POWER_WAIT_SEC", "10"))
TV_POWER_MAX_ATTEMPTS = int(os.getenv("TV_POWER_MAX_ATTEMPTS", "3"))
