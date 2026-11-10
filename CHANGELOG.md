# CHANGELOG

## [Unreleased]

### Changed
- `nemucast-auto`: 元の音量への復元をテレビが反映するまで読み戻して確認してから電源を切る（`AUTO_RESTORE_WAIT_SEC` / `AUTO_RESTORE_TOLERANCE`）。反映前に切ると翌日の音量が 0 になった
- `nemucast-auto`: 既定 21:00〜06:00 だけ段階的に音量を下げ、3 回目の tick で Android TV Remote によるテレビ本体の電源 OFF を読み戻して確認する。点灯中に元の音量へ戻してから切り、消灯・状態不明なら音量にも POWER にも触れない。時間外は進行中セッションだけ後始末し、失敗時の元音量と電源 OFF 待ちは次回へ保持する
- `nemucast-standby`: `quit_app`（ホーム画面に戻るだけで電源は切れていなかった）をやめ、Android TV Remote v2 の POWER でテレビ本体の電源を切る。`is_on` を見て点いている時だけ送り、読み戻しで確認する。見つからない・接続できない・切れない時は終了コード 1 と Discord 通知（従来は WARNING のまま成功扱い）
- `nemucast-auto`: 夜間窓の既定を 00:30〜05:00 から 21:00〜06:00 に広げた（21 時ごろに点けた時と朝方に音量が下がらなかったため）。窓の終わりをまたぐ進行中セッションは、打ち切らず完走させる（状態が `AUTO_STATE_STALE_SEC` 以内の時。古い時は従来通り音量を戻して終える）
- ログの「スタンバイモードになりました」は実際に読み戻せた時だけ出す

### Added
- 夜間設定 `AUTO_NIGHT_START` / `AUTO_NIGHT_END` / `AUTO_NIGHT_FORCE` / `AUTO_RESTORE_VOLUME` と を README に記載
- `nemucast-tv-pair`: テレビとの初回ペアリング。環境変数 `TV_CERT_DIR` / `TV_POWER_KEY` / `TV_POWER_WAIT_SEC` / `TV_POWER_MAX_ATTEMPTS`
- 依存に `androidtvremote2==0.3.2` を追加

### Fixed
- `nemucast-auto`: 発見一覧に目的名があるのに見つからない時は探索を再試行する（`DISCOVERY_MAX_RETRIES` / `DISCOVERY_RETRY_WAIT_SEC`）。窓の中で目的デバイスが `AUTO_NOT_FOUND_ALERT_AFTER_TICKS`（既定 4 tick）連続で見つからない時は、途切れごとに 1 回だけ通知し、見つかったら回復も 1 回通知する（従来は WARNING のまま無通知）。見つからない tick は `tick 結果: not_found` として集計できる
- `nemucast-auto`: 電源状態を読んだ時に生の値（`is_on` / `current_app` / `volume_level`）をログに残す。点灯中に消灯と判定された時の原因を後から確定するため
- `nemucast-auto`: スリープ等で `AUTO_STATE_STALE_SEC` より間が空いた前回の 0 台は、連続回数に数えないようにした（復帰直後の誤通知を防ぐ）
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