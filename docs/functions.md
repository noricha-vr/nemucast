# Nemucast 関数一覧

## `src/nemucast/main.py`

### 設定・初期化

#### `parse_args(args=None, default_overrides=None)`
- CLI 引数を解析する
- スケジュールプロファイルからの既定値上書きに対応する

#### `setup_logging()`
- ログファイルと標準出力のロギングを設定する

### Chromecast 接続

#### `discover_chromecasts(target_name: str)`
- ネットワーク上の Chromecast を検索する

#### `stop_discovery(browser)`
- discovery を安全に停止する

### state 管理

#### `load_state(state_file: Path)`
- 活動判定用 state JSON を読み込む

#### `save_state(state_file: Path, state: dict[str, Any])`
- state JSON を保存する

#### `clear_state(state_file: Path)`
- state JSON を削除する

#### `create_initial_state(device_name, current_volume, now_ts)`
- 新しいセッション用の state を作る

#### `is_state_stale(state, device_name, interval_sec, now_ts)`
- 古い state かどうかを判定する
- 猶予は `interval_sec x STATE_STALE_INTERVAL_MULTIPLIER`

### 活動判定・音量制御

#### `detect_manual_activity(current_volume, last_auto_volume, rise_threshold)`
- 前回自動設定音量より十分に上がっていれば手動操作とみなす

#### `append_history(state, entry)`
- state 履歴を最大件数まで保持する

#### `get_current_volume(cast)`
- 現在音量を取得する

#### `calculate_next_volume(current_volume, step, min_level)`
- 次に設定する音量を計算する

#### `lower_volume_once(cast, current_volume, step, min_level)`
- 1回分だけ音量を下げる

#### `standby_device(cast)`
- Chromecast を standby に移行する

#### `run_volume_tick(...)`
- 1回分の判定と音量調整を実行する

#### `run_volume_session(...)`
- 1回だけ、または standby まで interval ごとに継続実行する

### CLI 実行

#### `run_with_args(args=None, default_overrides=None)`
- CLI 実行本体
- 通常実行とプロファイル実行の共通入口

#### `build_schedule_defaults(profile_name)`
- 20:00 用、00:30 用の推奨設定を返す

#### `main()`
- 通常の `nemucast` エントリーポイント

#### `main_cron_20()`
- `nemucast-cron-20` 用エントリーポイント
- 20:00 に即 standby させるプロファイル

#### `main_cron_0030()`
- `nemucast-cron-0030` 用エントリーポイント
- 15分ごとに判定を継続し、45分無操作なら standby にするプロファイル
