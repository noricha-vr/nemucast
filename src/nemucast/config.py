"""環境変数から読み込む設定定数を集約する。

.env を load し、モジュール読み込み時に各定数を確定させる。
CLI / state 管理 / Chromecast 接続の各モジュールはここを参照する。
"""

from __future__ import annotations

import os
from typing import Any

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

# cron 起動用プロファイルの上書き値。CLI の default_overrides にそのまま渡す。
CRON_20_OVERRIDES: dict[str, Any] = {
    "name": os.getenv("CRON_20_NAME", CHROMECAST_NAME),
    "interval": int(os.getenv("CRON_20_INTERVAL_SEC", "60")),
    "step": float(os.getenv("CRON_20_STEP", str(STEP))),
    "min_level": float(os.getenv("CRON_20_MIN_LEVEL", "0.05")),
    "inactive_threshold": int(os.getenv("CRON_20_INACTIVE_THRESHOLD", "1")),
    "state_file": os.getenv("CRON_20_STATE_FILE", f"{LOG_DIR}/activity_state_20.json"),
    "run_until_standby": True,
}

CRON_0030_OVERRIDES: dict[str, Any] = {
    "name": os.getenv("CRON_0030_NAME", CHROMECAST_NAME),
    "interval": int(os.getenv("CRON_0030_INTERVAL_SEC", "900")),
    "step": float(os.getenv("CRON_0030_STEP", str(STEP))),
    "min_level": float(os.getenv("CRON_0030_MIN_LEVEL", "0.35")),
    "inactive_threshold": int(os.getenv("CRON_0030_INACTIVE_THRESHOLD", "4")),
    "state_file": os.getenv("CRON_0030_STATE_FILE", f"{LOG_DIR}/activity_state_0030.json"),
    "run_until_standby": True,
}

# Chromecast を quit_app した後、standby 完了を待つ秒数
STANDBY_WAIT_SEC = int(os.getenv("STANDBY_WAIT_SEC", "2"))

# ログファイルのローテーション設定
LOG_ROTATION_MAX_BYTES = int(os.getenv("LOG_ROTATION_MAX_BYTES", str(512 * 1024)))
LOG_ROTATION_BACKUP_COUNT = int(os.getenv("LOG_ROTATION_BACKUP_COUNT", "1"))

MAX_HISTORY_ENTRIES = 20
