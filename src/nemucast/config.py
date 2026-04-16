"""環境変数から読み込む設定定数を集約する。

.env を load し、モジュール読み込み時に各定数を確定させる。
CLI / state 管理 / Chromecast 接続の各モジュールはここを参照する。
"""

from __future__ import annotations

import os

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
