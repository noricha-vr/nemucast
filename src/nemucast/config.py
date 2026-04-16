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

CRON_20_NAME = os.getenv("CRON_20_NAME", CHROMECAST_NAME)
CRON_20_INTERVAL_SEC = int(os.getenv("CRON_20_INTERVAL_SEC", "60"))
CRON_20_INACTIVE_THRESHOLD = int(os.getenv("CRON_20_INACTIVE_THRESHOLD", "1"))
CRON_20_STEP = float(os.getenv("CRON_20_STEP", str(STEP)))
CRON_20_MIN_LEVEL = float(os.getenv("CRON_20_MIN_LEVEL", "0.05"))
CRON_20_STATE_FILE = os.getenv("CRON_20_STATE_FILE", f"{LOG_DIR}/activity_state_20.json")

CRON_0030_NAME = os.getenv("CRON_0030_NAME", CHROMECAST_NAME)
CRON_0030_INTERVAL_SEC = int(os.getenv("CRON_0030_INTERVAL_SEC", "900"))
CRON_0030_INACTIVE_THRESHOLD = int(os.getenv("CRON_0030_INACTIVE_THRESHOLD", "4"))
CRON_0030_STEP = float(os.getenv("CRON_0030_STEP", str(STEP)))
CRON_0030_MIN_LEVEL = float(os.getenv("CRON_0030_MIN_LEVEL", "0.35"))
CRON_0030_STATE_FILE = os.getenv("CRON_0030_STATE_FILE", f"{LOG_DIR}/activity_state_0030.json")

# スケジュール別プロファイル。CLI から参照するときは SCHEDULE_PROFILES[profile_name] を使う。
# 既存の CRON_*_ 定数は後方互換のため保持し、ここでそれらを辞書として束ねる。
SCHEDULE_PROFILES: dict[str, dict[str, Any]] = {
    "cron-20": {
        "name": CRON_20_NAME,
        "interval": CRON_20_INTERVAL_SEC,
        "step": CRON_20_STEP,
        "min_level": CRON_20_MIN_LEVEL,
        "inactive_threshold": CRON_20_INACTIVE_THRESHOLD,
        "state_file": CRON_20_STATE_FILE,
        "run_until_standby": True,
    },
    "cron-0030": {
        "name": CRON_0030_NAME,
        "interval": CRON_0030_INTERVAL_SEC,
        "step": CRON_0030_STEP,
        "min_level": CRON_0030_MIN_LEVEL,
        "inactive_threshold": CRON_0030_INACTIVE_THRESHOLD,
        "state_file": CRON_0030_STATE_FILE,
        "run_until_standby": True,
    },
}

# Chromecast を quit_app した後、standby 完了を待つ秒数
STANDBY_WAIT_SEC = int(os.getenv("STANDBY_WAIT_SEC", "2"))

# ログファイルのローテーション設定
LOG_ROTATION_MAX_BYTES = int(os.getenv("LOG_ROTATION_MAX_BYTES", str(512 * 1024)))
LOG_ROTATION_BACKUP_COUNT = int(os.getenv("LOG_ROTATION_BACKUP_COUNT", "1"))

MAX_HISTORY_ENTRIES = 20
