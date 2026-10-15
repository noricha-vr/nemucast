# CHANGELOG

## [Unreleased]

### Fixed
- `nemucast-auto`: LAN 上の Chromecast が 1 台も見えない時（macOS のローカルネットワーク権限が外れた等）を、デバイス不在と区別して ERROR・終了コード 2 にした（`DISCOVERY_ALERT_AFTER_TICKS` 回続いた時。0 で無効）。`DISCOVERY_ALERT_COMMAND` を設定すると、この状態に入った時と回復した時に 1 回ずつ通知する

### Refactored
- main.pyを役割ごとに関数化してリファクタリング
  - setup_logging: ロギング設定
  - discover_chromecasts: デバイス検索
  - log_chromecast_status/log_active_app_status: 状態表示
  - get_initial_volume/adjust_volume/restore_volume_and_standby: 音量制御
  - volume_control_loop: メインループ
- 関数のドキュメントをdocs/functions.mdに追加
- リファクタリングされた関数の単体テストを追加（test_refactored_functions.py）

### Changed
- INFOログレベルでChromecastの状態情報を出力するように改善
  - 接続時にChromecastの状態（アクティブ/アイドル）を表示
  - メディアの再生状態（PLAYING/PAUSED等）を表示
- アプリ名の取得処理を削除（安定した取得が困難なため）

### Added
- Chromecastの起動状態チェック機能を追加
  - `is_chromecast_active()` メソッドを実装し、デバイスが実際にアクティブかどうかを確認
  - `app_id`でアプリの起動状態を判定（None、IDLE_APP_ID、Backdropの場合はアイドル）
  - メディアコントローラーの`player_state`も確認し、PLAYING/PAUSED/BUFFERINGの場合はアクティブと判定
  - 音量調整処理の前に起動状態をチェックし、アイドル状態の場合は処理をスキップ
  - 電源オフ処理（`quit_app()`）の前に起動状態をチェックし、既にアイドル状態の場合はスキップ
  - これにより、電源OFF時の意図しない電源ONを防止
- 環境変数`LOG_LEVEL`でログレベルを設定可能に（デバッグ時は`LOG_LEVEL=DEBUG`を設定）

### Fixed
- テストの期待値を更新: MIN_LEVEL のデフォルト値を 0.3 から 0.4 に変更 (`.env` ファイルでの設定値に合わせて修正)
  - `test_volume_control.py` の音量計算テストケースを更新
  - `test_args.py` のデフォルト引数テストケースを更新

## [Previous versions]

### Added
- 音量の自動復元機能を実装
- プロジェクトをNemucast（ねむキャス）にリブランディング
- コマンドライン引数にChromecast設定オプションを追加
- python-dotenv サポートを追加