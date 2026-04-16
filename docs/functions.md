# Nemucast 関数一覧

`src/nemucast/` 以下のモジュール構成と公開関数をまとめる。依存方向は
`config` → `state` / `cast_client` → `volume` → `cli` の一方向。

## `src/nemucast/config.py`

環境変数から読み込む設定定数を集約する。`.env` を `load_dotenv()` する起点。

- `CHROMECAST_NAME`, `STEP`, `MIN_LEVEL`, `DEFAULT_INTERVAL_SEC`
- `INACTIVE_THRESHOLD`, `MANUAL_RISE_THRESHOLD`, `DEFAULT_STATE_FILE`
- `RUN_UNTIL_STANDBY`, `STATE_STALE_INTERVAL_MULTIPLIER`
- `CRON_20_*`（`NAME`, `INTERVAL_SEC`, `INACTIVE_THRESHOLD`, `STEP`, `MIN_LEVEL`, `STATE_FILE`）
- `CRON_0030_*`（同上）
- `MAX_HISTORY_ENTRIES`

## `src/nemucast/state.py`

活動判定用 state JSON の読み書きと整合性判定。

#### `load_state(state_file: Path)`
- 活動判定用 state JSON を読み込む（存在しなければ `None`）

#### `save_state(state_file: Path, state: dict[str, Any])`
- state JSON を保存する

#### `clear_state(state_file: Path)`
- state JSON を削除する

#### `create_initial_state(device_name, current_volume, now_ts)`
- 新しいセッション用の state を作る

#### `is_state_stale(state, device_name, interval_sec, now_ts)`
- 古い state かどうかを判定する
- 猶予は `interval_sec x STATE_STALE_INTERVAL_MULTIPLIER`

#### `detect_manual_activity(current_volume, last_auto_volume, rise_threshold)`
- 前回自動設定音量より十分に上がっていれば手動操作とみなす

#### `append_history(state, entry)`
- state 履歴を最大 `MAX_HISTORY_ENTRIES` 件まで保持する

## `src/nemucast/cast_client.py`

Chromecast デバイスの検索・接続・制御。`pychromecast` への依存はここに閉じる。

#### `discover_chromecasts(target_name: str)`
- ネットワーク上の Chromecast を検索する

#### `stop_discovery(browser)`
- discovery を安全に停止する

#### `get_current_volume(cast)`
- 現在音量を取得する

#### `standby_device(cast)`
- Chromecast を standby に移行する

## `src/nemucast/volume.py`

音量制御と 1 tick ぶんの判定ロジック。`state` / `cast_client` / `config` に依存する。

#### `calculate_next_volume(current_volume, step, min_level)`
- 次に設定する音量を計算する

#### `lower_volume_once(cast, current_volume, step, min_level)`
- 1回分だけ音量を下げる

#### `run_volume_tick(...)`
- 1回分の判定と音量調整を実行する

#### `run_volume_session(...)`
- 1回だけ、または standby まで interval ごとに継続実行する

## `src/nemucast/cli.py`

CLI エントリポイント、引数解析、ロギング設定、スケジュール別プロファイル。

#### `parse_args(args=None, default_overrides=None)`
- CLI 引数を解析する
- スケジュールプロファイルからの既定値上書きに対応する

#### `setup_logging()`
- ログファイル（`logs/lower_cast_volume.log`）と標準出力のロギングを設定する

#### `build_schedule_defaults(profile_name)`
- 20:00 用、00:30 用の推奨設定を返す

#### `run_with_args(args=None, default_overrides=None)`
- CLI 実行本体
- 通常実行とプロファイル実行の共通入口

#### `main()`
- 通常の `nemucast` エントリーポイント

#### `main_cron_20()`
- `nemucast-cron-20` 用エントリーポイント
- 20:00 に即 standby させるプロファイル

#### `main_cron_0030()`
- `nemucast-cron-0030` 用エントリーポイント
- 15分ごとに判定を継続し、45分無操作なら standby にするプロファイル
