# ねむキャス (Nemucast) 🔊😴

リビングルームのテレビを 20:00 に切り、Dell（モニターに挿した Chromecast）は夜間に音量を段階的に下げて電源 OFF を試すツールです。

Dell の自動制御はローカル時刻の 21:00〜06:00 に開始し、新鮮な進行中セッションは窓外でも完走します。手動の音量上昇で待ち時間を数え直し、電源を切る直前に元の音量へ戻します。Dell はペアリング済みなら Android TV Remote の POWER、未ペアリングなら `quit_app` を試すところまでです。リビングルームは 20:00 の即 OFF だけを行い、夜間窓の対象から外します。

Dell は 2026-11-10 にペアリング済み。電源 OFF の実機試験は成功と報告済み（`is_on` が True→False）です。

![alt text](images/assets_task_01jy97dj7yey59zsq6vq5zd7dx_1750509022_img_1.webp)

## 🎯 機能

- **活動判定を state で保持**: `last_auto_volume` と `inactive_streak` を JSON に保存します
- **手動上昇を検出**: 前回の自動設定音量より上がっていれば活動ありとみなします
- **定期実行にも連続実行にも対応**: 1回だけの tick 実行と、standby まで継続する session 実行の両方をサポートします
- **夜間に開始する自動制御**: ペアリング済みの点灯または Backdrop 以外のアプリ起動を確認してから音量を操作し、新鮮な進行中セッションは窓外でも完走します
- **2 つの運用を同時に動かす**: 20:00 の即 OFF（`nemucast-standby`）と、深夜の段階的 OFF（`nemucast-auto`）。別々の periodic-worker タスクです

## 📋 必要な環境

- Python 3.11 以上
- 同一ネットワーク上に Chromecast / Google TV デバイス
- `uv`

## 🚀 インストール

```bash
git clone https://github.com/noricha-vr/nemucast.git
cd nemucast
uv sync
cp .env.example .env
```

## ⚙️ 基本設定

| 環境変数 | 説明 | デフォルト |
|---------|------|-----------|
| `CHROMECAST_NAME` | 20:00 の `nemucast-standby`・ペアリング・手動実行の対象。運用では `リビングルーム` に設定 | `"Dell"`（従来どおり） |
| `STEP` | 1回で下げる音量幅 | `-0.04` |
| `MIN_LEVEL` | これ以上は下げない最小音量 | `0.3` |
| `INTERVAL_SEC` | 定期実行の想定間隔 | `1200` |
| `INACTIVE_THRESHOLD` | 連続非アクティブ回数のしきい値 | `3` |
| `MANUAL_RISE_THRESHOLD` | 手動上昇とみなす最小差分 | `0.01` |
| `STATE_FILE` | state JSON の保存先 | `logs/activity_state.json` |
| `STATE_STALE_INTERVAL_MULTIPLIER` | state を古いとみなす倍率 | `2` |
| `RUN_UNTIL_STANDBY` | standby まで interval ごとに継続実行するか | `0` |

state は `INTERVAL_SEC x STATE_STALE_INTERVAL_MULTIPLIER` より古いと破棄されます。

## 📖 使い方

### 1回だけ判定する通常実行

```bash
uv run nemucast
uv run nemucast --interval 900 --inactive-threshold 4
```

### standby まで継続する実行

```bash
uv run nemucast --interval 900 --inactive-threshold 4 --run-until-standby
```

## 🕘 タイマー用プロファイル

### テレビの電源を切る

- コマンド: `nemucast-standby`
- 動き:
  - Cast の発見結果から IP を得て（mDNS に出ない時は同じ名前で最後に見えた IP）、Android TV Remote v2 でテレビ本体の電源を切ります
  - POWER キーはトグルなので、`is_on` を読んで点いている時だけ送り、新規接続で `is_on=False` を読み戻して確認します（切れなければ最大 `TV_POWER_MAX_ATTEMPTS` 回）。元から切れていれば何も送らず成功です
  - 見つからない・接続できない・切れない時は終了コード 1 と `DISCOVERY_ALERT_COMMAND` の通知
  - 音量は触りません。state ファイルも使いません
- 初回ペアリング（1 回だけ）: `nemucast-tv-pair --pin-file <PIN を書くファイル> --need-file <PIN 要求を書くファイル>`。テレビに出た PIN を `--pin-file` に書くと完了し、証明書・鍵を `TV_CERT_DIR`（既定 `~/.config/nemucast`、0600）に保存します
- 環境変数: `TV_CERT_DIR`（既定 `~/.config/nemucast`）/ `TV_POWER_KEY`（既定 `POWER`）/ `TV_POWER_WAIT_SEC`（既定 10）/ `TV_POWER_MAX_ATTEMPTS`（既定 3）
- オプション:
  - `--name`: 対象デバイス名（デフォルト: `CHROMECAST_NAME` 環境変数 / `Dell`）
- 想定用途: リビングルームを 20:00 に即 OFF（タスク `nemucast-20`、`CHROMECAST_NAME=リビングルーム`）。Dell には向けません

### 自動寝かしつけ（夜間のみ）

- コマンド: `uv run --frozen nemucast-auto`
- periodic-worker は従来どおり 15 分間隔で 24 時間起動します。自動制御を開始する時間はローカル時刻の **21:00 以上、06:00 未満** です。
- 対象は `AUTO_TARGET_NAME`（既定 `Dell`）で、`CHROMECAST_NAME` とは独立です。リビングルームに夜間窓の処理を向けません。対象を変える時は CEO に確認します。時間外に新しいセッションは開始しません。

1. 時間外は新しいセッションを開始しません。`original_volume` があり、最終更新から `AUTO_STATE_STALE_SEC` 以内なら通常どおり下げ・復元・電源 OFF まで続行します。stale なセッションは発見・接続・機器照合後、アプリの有無によらず音量を戻して state を初期化します。手動で前回の設定値より音量を上げていた場合は、その音量を保ちます。接続・照合できなければ警告して次 tick で再試行します。
2. Cast を発見し、`cast.wait` の後、ペアリング済みなら `read_power_state` で MAC を照合します。不一致や接続失敗時は、音量・生値ログ・アプリ判定より前に中断します（未ペアリング時は名前のみで選び、MAC を照合できません）。照合時に読んだ `is_on=True`、または status があり `app_id` が None・空文字・pychromecast の `IDLE_APP_ID`（Backdrop）以外なら視聴中です。追加の電源状態接続は行いません。未ペアリングはアプリ起動中だけで判定します。`is_on=False` でもアプリ起動中なら制御します。`is_idle`・`is_stand_by`・`is_active_input`・`player_state` は判定に使いません。どちらも満たさない時だけ `not_watching` と記録し、元音量が残っていれば時間外と同じ手順で復元し、state を初期化して終了します。
3. 視聴中（ペア済みの点灯またはアプリ起動中）だけ音量を `STEP` ずつ下げます。最初の音量を `original_volume` に保存します。前回の設定値より手動で音量が上がったら、連続カウントをリセットし、元の音量もその値に更新します。
4. 下限 `AUTO_MIN_LEVEL` 以下なら音量を変えずカウントだけ進めます。既定では **3 回目の tick で閾値に達し、その tick 内で電源 OFF を試します**（21:00 に開始すれば 21:15、21:30 と進み、21:30 に OFF 試行）。
5. 切る直前に `original_volume` へ音量を戻し、読み戻しで確認します。ペアリング済み（名前に対応する MAC 登録あり）なら Android TV Remote の `power_off` を実行します。POWER は `is_on=True` の時だけ送り、新規接続で OFF を読み戻します。失敗時はカウントと元音量を保持し、通知・終了コード 1 として次 tick で再試行します。
6. 未ペアリングなら `quit_app` を試します。失敗しても WARNING を記録して続行し、`power_off_unavailable` とペアリング方法をログと通知で知らせます。通知はローカル日付で 1 日 1 回まで、翌日は再通知し、通知失敗時は送信済み日を記録せず再送待ちだけを保持し、アプリ終了後や窓外でも次 tick で再送します。ペアリング済みになったら再送待ちを解除し、送信しません。壊れた通知ファイルは未通知として扱います。OFF 成功時と未ペアリングの分岐後は state を初期化します。Dell の初回・再ペアリング手順は `nemucast-tv-pair --name Dell --pin-file <PIN を書くファイル> --need-file <PIN 要求を書くファイル>` です。

| 環境変数 | 説明 | 既定値 |
|----------|------|--------|
| `AUTO_TARGET_NAME` | 夜間窓の対象。`CHROMECAST_NAME` と独立。対象変更は CEO に確認 | `Dell` |
| `AUTO_POWER_OFF_ALERT_FILE` | 未ペアリングによる電源 OFF 不可の通知日（YYYY-MM-DD）と再送待ち | `logs/auto_power_off_alert_state.json` |
| `AUTO_NIGHT_START` | 夜間窓の開始（ローカル時刻 HH:MM、含む） | `21:00` |
| `AUTO_NIGHT_END` | 夜間窓の終了（HH:MM、含まない）。`23:00`〜`02:00` のような日付またぎにも対応。同時刻は空の窓 | `06:00` |
| `AUTO_NIGHT_FORCE` | `1` なら時間帯を無視して動作。昼間の試験用 | `0` |
| `AUTO_RESTORE_VOLUME` | `1` なら電源 OFF 直前に元の音量へ戻す。時間外の後始末ではこの設定にかかわらず戻す | `1` |
| `AUTO_RESTORE_WAIT_SEC` | 音量復元後、テレビが反映した値を読み戻して確かめる待ち時間（秒）。`set_volume` は応答を待たず、反映前に電源を切ると翌日の音量が 0 になった（実機）ため、確認してから電源を切る。確認できなくても電源OFFは続行し WARNING を出す | `5` |
| `AUTO_RESTORE_TOLERANCE` | 読み戻しの許容差 | `0.015` |
| `AUTO_LOWERED_THRESHOLD` | 電源 OFF までの連続下げ回数 | `3` |
| `AUTO_MIN_LEVEL` | 下限音量 | `0.05` |
| `AUTO_STATE_FILE` | 自動制御の state 保存先 | `logs/auto_standby_state.json` |
| `AUTO_STATE_STALE_SEC` | 間隔が空いた時に連続カウントを捨てる秒数 | `1800` |
| `DISCOVERY_ALERT_COMMAND` | 通知コマンド。本文を末尾の引数で渡す。空なら通知しない | 空 |
| `DISCOVERY_ALERT_FILE` | LAN 0 台検知の通知済みフラグ | `logs/discovery_alert_state.json` |
| `DISCOVERY_ALERT_AFTER_TICKS` | LAN 0 台が連続した時の障害閾値。`0` で無効 | `2` |
| `DISCOVERY_MAX_RETRIES` | 発見一覧に目的名があるのに接続候補が空の時の追加探索回数（初回を除く） | `3` |
| `DISCOVERY_RETRY_WAIT_SEC` | 追加探索前の待ち秒数 | `1` |
| `AUTO_NOT_FOUND_ALERT_AFTER_TICKS` | 窓内の目的名不在が連続した時の通知閾値。15 分間隔で約 1 時間。`0` で無効 | `4` |
| `AUTO_NOT_FOUND_ALERT_FILE` | 目的名の不在回数・通知済みフラグ（LAN 0 台検知とは別） | `logs/auto_not_found_alert_state.json` |

`STEP`・`MANUAL_RISE_THRESHOLD` は他コマンドと共通です。auto の対象は `AUTO_TARGET_NAME` で独立しています。夜間・テレビ電源の設定は `.env.example` と `config.py` に同じ既定値で載せています。

- 対象 Cast だけが見つからない時は正常終了します。窓内で不在が既定 4 tick 続いたら一度だけ通知し、発見できたら回復も一度通知します。通知失敗時は次 tick で再送します。未通知の連続回数は stale でリセットし、通知済みの途切れは回復まで保持します。窓外では不在・回復とも記録を更新しません。LAN 全体で 0 台が続いた時は従来どおり ERROR・終了コード 2 とし、障害確定時と回復時に 1 回ずつ通知します。夜間窓の中だけこの検知を行います。
- launchd 配下では、そのプロセスに macOS の「プライバシーとセキュリティ > ローカルネットワーク」の許可が必要です。
- 音量に触れず視聴中の判定が続く場合も、夜間窓の中では 3 tick で電源 OFF を試します。mDNS・CEC 値は視聴中の判定に使いません。ペアリング後に実際の電源を切る時は `power_off` 内で `is_on` を確認します。
- stale な state は連続カウントを捨てますが、復元用 `original_volume` は保持します。すでに閾値に達した電源 OFF の再試行も保持します。旧 `powered_off` / `volume_at_power_off` は使用しません。壊れた JSON は作り直して続行し、保存は一時ファイルと置き換えで行います。

### 2 つの運用と止め方

- リビングルームの 20:00 即 OFF = `nemucast-standby`（タスク `nemucast-20`、`CHROMECAST_NAME`）、Dell の夜間段階的 OFF = `nemucast-auto`（タスク `nemucast-auto`、`AUTO_TARGET_NAME`）。2 つは独立で、普段は両方 active のままにします。
- 片方だけ止めたい時は periodic-worker の status で足ります: `periodic-worker update nemucast-auto --status inactive`（戻す時は `active`）。

### periodic-worker 登録例

```bash
# 20:00 に即 standby
periodic-worker register \
  --name nemucast-20 --cron "0 20 * * *" --tz Asia/Tokyo \
  --cwd /path/to/nemucast \
  --command "/path/to/.venv/bin/nemucast-standby"

# 24h、15分間隔で起動（夜間窓内に開始・進行中は完走）
periodic-worker register \
  --name nemucast-auto --every 15m \
  --cwd /path/to/nemucast \
  --command "/path/to/.venv/bin/nemucast-auto"
```

## 🔧 nemucast-auto の動作の仕組み

```mermaid
flowchart TD
    Start([15分タイマー]) --> Night{夜間窓の中または FORCE?}
    Night -->|No| Session{original_volume がある?}
    Session -->|No| End([終了])
    Session -->|Yes| Fresh{state が stale でない?}
    Fresh -->|Yes| Discover
    Fresh -->|No| Cleanup[発見・接続・ペアリング済みなら MAC 照合]
    Cleanup -->|照合成功・未ペアリング| RestoreCleanup[手動上昇がなければ元音量へ復元し state 初期化]
    Cleanup -->|不明・見つからない| Defer[警告・state 保持で次回再試行]
    RestoreCleanup --> End
    Clear[state 初期化] --> End
    Defer --> End
    Night -->|Yes| Discover[Cast 発見・窓内だけ不在と LAN 0 台検知]
    Discover --> Identity[接続・ペアリング済みなら MAC 照合]
    Identity -->|成功・未ペアリング| Watching{ペア済みの is_on=True またはアプリ起動中?}
    Identity -->|失敗| Fail
    Watching -->|No| RestoreCleanup
    Watching -->|Yes| Pending{電源OFFの再試行待ち?}
    Pending -->|No| Manual{手動音量上昇?}
    Manual -->|Yes| Reset[カウントをリセット・original_volume 更新]
    Manual -->|No| Lower
    Reset --> Lower[初回の音量保存・STEP 分下げる・count 加算]
    Lower --> Save[state 保存]
    Save --> Threshold{count が閾値以上?}
    Threshold -->|No| End
    Pending -->|Yes| Restore
    Threshold -->|Yes| Restore[設定が有効なら元の音量へ復元]
    Restore --> Paired{ペアリング済み?}
    Paired -->|Yes| Off[Android TV Remote の power_off・読み戻し]
    Paired -->|No| Unpaired[quit_app 試行・WARNING・電源 OFF 不可を日次通知]
    Unpaired --> Clear
    Off -->|成功| Clear
    Off -->|失敗| Fail[state 保持・通知・exit 1]
```

### コマンド別の対比

| 項目 | `nemucast-standby` | `nemucast-auto` | `nemucast`（手動） |
|------|--------------------|------------------|--------------------|
| 対象 | リビングルーム / `CHROMECAST_NAME` | Dell / `AUTO_TARGET_NAME` | `CHROMECAST_NAME` |
| 想定スケジュール | 時刻指定（例: 20:00） | 15分間隔・夜間に開始して完走 | 任意のタイミングで手動実行 |
| 音量制御 | しない（テレビ本体の電源を切る） | する | する |
| state | 持たない | `auto_standby_state.json` | `activity_state.json` |
| 電源OFF条件 | 即時 | 連続 3 tick 下げた時点 | 連続 N tick 非アクティブ |
| 用途 | ハードな時刻カットオフ | 寝落ち検知の自動運転 | デバッグ・手動投入 |

## 📝 ログと state

- 即時 standby ログ: `logs/standby.log`
- 自動寝かしつけログ: `logs/auto_standby.log`。`tick 結果: lowered / outside_window / cleanup / not_watching / turned_off / not_found / power_off_unavailable` と音量復元・カウントを記録します。接続・機器照合後は毎 tick、`対象機器の生値: device=... is_on=... app_id=... display_name=... is_idle=... is_stand_by=... is_active_input=... volume=...` を INFO に 1 行出します。`app_id` / `display_name` は制御文字を escape し、各 64 文字以内に制限します。ペアリング後の OFF 試行時にすでに消灯なら `power_off` の戻り値 `already_off` も記録します
- 確認例: `rg "tick 結果:|tick 要約:|対象機器の生値:|音量復元|power_off_unavailable|ERROR" logs/auto_standby.log`。初回の夜間運転後、復元が OFF 確認より前に出ていること、窓の外で新規セッションが始まっていないこと（進行中なら `lowered` は正常）を確認します
- 各機器処理の正常終了時に `tick 要約:` を 1 行出します。`grep "tick 要約" logs/auto_standby.log` で、操作前の `is_on` / `app_id`、`volume_before`（開始音量）/ `volume_after`（終了時の目標音量）、`count`、`result` を確認できます。未ペアの `is_on` と不明な音量は `None` です。音量を変えなければ前後は同値です。`count` はその tick の連続回数で、`not_watching` / `cleanup` は初期化後の 0、OFF 結果は初期化前の回数です。
- 手動実行ログ: `logs/lower_cast_volume.log`
- state:
  - `nemucast-auto` → `logs/auto_standby_state.json`
  - 目的名不在通知 → `logs/auto_not_found_alert_state.json`（LAN 0 台検知は `logs/discovery_alert_state.json`）
  - 電源 OFF 不可の通知日 → `logs/auto_power_off_alert_state.json`
  - `nemucast`（手動） → `logs/activity_state.json`

## ✅ テスト

```bash
uv run pytest -q
uv run ruff check
```

## 🪝 pre-commit フック

コミット前に `ruff check` と `ruff format --check` が自動で走るよう pre-commit フックを用意しています。
初回セットアップ時に以下を実行してください。

```bash
uv sync --all-groups
uv run pre-commit install
```

手動で全ファイルに対して実行する場合:

```bash
uv run pre-commit run --all-files
```
