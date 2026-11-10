# AGENTS.md

AI エージェント（Claude Code / Codex 等）がこのリポジトリを操作する際の前提情報。
プロダクトの使い方や環境変数の意味は `README.md` / `.env.example` を参照すること。
ここではエージェント向けに「構成・規約・開発コマンド」を集約する。

## Quick Reference

| 項目 | 値 |
|------|-----|
| プロダクト | ねむキャス（Chromecast / Google TV の自動寝かしつけ・電源OFFツール） |
| 言語 | Python 3.11+ |
| パッケージマネージャ | uv（`pip` / ローカル `python` は使わない） |
| 主要ライブラリ | pychromecast, python-dotenv |
| テスト | pytest + pytest-mock |
| Lint / Format | ruff（`select = ["E", "F", "I", "N", "W", "UP"]`, line-length 100） |
| pre-commit | ruff-check --fix / ruff-format --check |
| CI | `.github/workflows/ci.yaml`（push/PR で ruff + pytest を実行） |
| エントリポイント | `nemucast` / `nemucast-standby` / `nemucast-auto` / `nemucast-tv-pair` |

## プロジェクト概要

リビングルームのテレビ（シャープ AQUOS-4KTVJ26-1）と Dell（モニターに挿した Chromecast）を、機器ごとに別の運用で制御する CLI ツール。
Chromecast の active / idle 判定には依存せず、`前回自動設定した音量` と `現在音量` の差分からユーザー活動を推定する。

## 機器ごとの役割（混ぜない）

- リビングルーム（テレビ）= 20:00 に電源を切る、それだけ。`nemucast-standby` / `nemucast-20`、対象は `CHROMECAST_NAME`（運用値 `リビングルーム`）。
- Dell（モニターの Chromecast）= 21:00〜06:00 に 15 分ごとに音量を下げ、下がり切ったら元音量に戻して電源を切る。`nemucast-auto`、対象は `AUTO_TARGET_NAME`（既定 `Dell`）。
- この 2 つを混ぜない。夜間窓の処理をリビングルームに向けず、20:00 の処理を Dell に向けない。対象を変える時は CEO に確認する。
- Dell は 2026-11-10 にペアリング済み。電源 OFF は 2026-11-10 22:13 の実機試験で `is_on` が True→False になることを確認済み（CEO も目視で確認）。Android TV Remote の POWER を使う。未ペアリング時は音量を下げ、元音量へ復元して `quit_app` を試すところまで。電源 OFF 不可を WARNING と通知で知らせる。
- auto はペアリング済みなら `cast.wait` 後、音量・生値ログ・アプリ判定より先に `read_power_state` で MAC を照合する（戻り値の `is_on` を視聴判定にも使い、追加接続はしない）。未ペアリングの機器は名前のみで選び、MAC を照合できない。
- auto はペアリング済みの `is_on=True`、または `cast.status.app_id` が None・空文字・Backdrop 以外なら視聴中とする。未ペアリングはアプリ起動中だけで判定する。`is_idle`・CEC フラグ・`player_state` は判定に使わず、どちらも満たさない時だけ `not_watching` として元音量があれば復元後に state を初期化する（手動で上げられた音量は保つ）。

### 2 つの運用（periodic-worker の別タスク。普段は両方 active）

| 運用 | コマンド / タスク | 役割 |
|------|-------------------|------|
| 20:00 の即 OFF | `nemucast-standby` / `nemucast-20`（cron `0 20 * * *` Asia/Tokyo） | リビングルームの音量に触れず、テレビ本体の電源を切る。読み戻しで確認し、失敗は exit 1 + Discord 通知 |
| 深夜の段階 OFF | `nemucast-auto` / `nemucast-auto`（15 分間隔） | Dell を対象に既定 21:00〜06:00 の窓内で開始し、新鮮な進行中セッションは窓外でも完走する。視聴中（ペア済みの点灯またはアプリ起動中）に音量を段階的に下げ、元の音量へ戻してから電源 OFF を試す（未ペアリングは `quit_app` まで）。窓外では新規セッションを開始せず、stale な進行中セッションは音量復元だけ行う |

- 片方だけ止める時は `periodic-worker update <タスク名> --status inactive`（戻す時は `active`）。専用の切替コマンドは作らない。
- 夜間窓は環境変数で変えられる（`AUTO_NIGHT_START` / `AUTO_NIGHT_END`。昼でも試す時は `AUTO_NIGHT_FORCE=1`）。

## テレビの電源制御（必読）

- **`quit_app` ではこの機種の電源は切れない**（ホーム画面に戻るだけ。2026-11-05 に実機で確認）。電源OFFは `tv_power`（Android TV Remote v2、`androidtvremote2` 版固定）で POWER を送る。手動の `nemucast`（`volume.py`）は今も `cast_client.standby_device`（= `quit_app`）を使うため、電源は切れない。
- **POWER はトグル**。`is_on` を読めた時、かつ True の時だけ送る。状態が確定できない時（接続失敗・`is_on` を読めない・MAC 不一致）は絶対に送らない。送信直前にも `is_on` を再確認し、送信後は新規接続で `is_on=False` を読み戻す。`KEYCODE_SLEEP` / `SOFT_SLEEP` はこの機種で無反応。
- **音量の復元は読み戻しで確認してから電源を切る**。`cast.set_volume` は応答を待たず、復元の直後（約 0.1 秒）に POWER を送ると復元が反映されず、翌日点けた時の音量が 0 になった（2026-11-05 実機）。音量 0.23 のまま電源を切って点け直すと 0.23 が保たれる。
- 電源が切れていても mDNS・6466・8009 は残る（数分は実測）。「見えない」は「切れている」の根拠にならない。電源は発見結果ではなく `is_on` で判断する。
- 接続先は Cast の発見結果から毎回得る（IP 固定にしない）。見えない時は同名の前回 IP を使い、ペアリング時に記録したテレビの MAC と照合する。
- **ペアリング**（初回のみ。PIN はテレビ画面に出るので人の読み取りが要る）: `uv run --frozen nemucast-tv-pair --pin-file <PIN を書くファイル> --need-file <PIN 要求を書くファイル>`。`--need-file` に要求が書かれ、`--pin-file` に PIN が書かれるのを最大 10 分待つ。完了すると証明書・鍵・MAC・IP を保存する。
- Dell のペアリング手順: `uv run --frozen nemucast-tv-pair --name Dell --pin-file <PIN を書くファイル> --need-file <PIN 要求を書くファイル>`。
- **鍵の置き場所**: `TV_CERT_DIR`（既定 `~/.config/nemucast`、ディレクトリ 0700・ファイル 0600、Git 管理外）。`tv_cert.pem` / `tv_key.pem`（クライアント証明書と秘密鍵）、`identities.json`（名前→テレビの MAC）、`last_hosts.json`（名前→前回の IP）。値をログや Issue に書かない。

## uv の注意

- 実行・同期には `--frozen` を使う（`uv sync --frozen` / `uv run --frozen ...`）。グローバルの uv 設定（`exclude-newer = "7 days"`）が lock に記録されていないため、素の `uv run` / `uv sync` / `uv sync --locked` は lock を書き換える（実際に 2026-11-05 に uv.lock が大量の版更新で書き換わった）か失敗する。
- 依存を足す時だけ `uv lock --no-config` で最小差分にし、新規の推移依存の公開日が 7 日以上前か手で確認する。

## 実機テストの作法

- テレビの状態（点灯・消灯・音量）を変える操作の前に、必ず人に知らせる（誰も見ていない間に点けない。2026-11-05 に無断で点灯させて混乱させた）。
- 「切れた」「点いた」の完了判定は目視を使う。ログの `is_on` だけで完了としない。目視が要る時は依頼ファイルに「何を見てほしいか」を書いて回答を待つ。
- テストは対象のテレビだけに行い、periodic-worker の一回限りタスク（`--once --after`）で本番と同じ経路を通す。終わったらタスクを削除する。実行の前後は別接続で `is_on` を読んで記録する。

## ディレクトリ構成

| パス | 役割 |
|------|------|
| `src/nemucast/cli.py` | `nemucast`（手動実行）のエントリポイント、argparse、ロギング設定 |
| `src/nemucast/standby.py` | `nemucast-standby` のエントリポイント。発見 → テレビ本体の電源OFF → 読み戻し確認。失敗は終了コード 1 + 通知 |
| `src/nemucast/tv_power.py` | Android TV Remote v2 の電源制御（`is_on` を見て POWER → 新規接続で読み戻し）、証明書パス、IP キャッシュ |
| `src/nemucast/tv_pair.py` | `nemucast-tv-pair`（初回ペアリング。PIN をファイル経由で受け取る） |
| `src/nemucast/auto_standby.py` | `nemucast-auto` のエントリポイント。1 tick 分の最小ロジック（下げる / 電源OFF / 何もしない） |
| `src/nemucast/config.py` | 環境変数から読み込む設定定数 |
| `src/nemucast/cast_client.py` | Chromecast の検索・接続・音量取得。`standby_device`（`quit_app`）は手動の `nemucast` 専用で電源は切れない |
| `src/nemucast/state.py` | 活動判定 state JSON の読み書き・整合性判定（`nemucast` 手動実行用） |
| `src/nemucast/volume.py` | 音量計算と 1 tick 分の制御ループ（`VolumeSessionConfig` / `TickResult`。`nemucast` 手動実行用） |
| `src/nemucast/__main__.py` | `python -m nemucast` 用のエントリ |
| `tests/` | pytest テスト。`conftest.py`（共通 fixture）と `test_args.py` / `test_volume_control.py` / `test_state.py` / `test_volume.py` / `test_cast_client.py` / `test_cli.py` / `test_standby.py` / `test_auto_standby.py` / `test_tv_power.py` に分割 |
| `logs/` | 実行ログ（`lower_cast_volume.log`）と state JSON。Git 管理外 |
| `docs/` | 永続ドキュメント。`docs/tmp/` は一時ドキュメント（Git 管理外） |
| `.github/workflows/ci.yaml` | ruff + pytest を実行する CI |
| `.env.example` | 環境変数の仕様書。追加／削除時は必ず同期する |

## 開発コマンド

```bash
# 初回セットアップ（dev グループも含める）
uv sync --all-groups
uv run pre-commit install

# テスト（1分以内で完了するはず）
uv run pytest -q

# Lint / Format
uv run ruff check
uv run ruff format          # 差分適用
uv run ruff format --check  # CI / pre-commit と同じチェック

# pre-commit を全ファイルに手動実行
uv run pre-commit run --all-files

# CLI 実行
uv run --frozen nemucast-auto          # 1 tick 分（夜間窓内で開始・進行中は完走。periodic-worker が 15 分間隔で叩く）
uv run --frozen nemucast-standby       # テレビ本体の電源OFF（読み戻し確認。点いていれば本当に消える）
uv run nemucast               # 手動デバッグ用
uv run nemucast --interval 900 --inactive-threshold 4 --run-until-standby
```

## CLI エントリポイント

| コマンド | 用途 | 主な設定 |
|----------|------|----------|
| `nemucast-auto` | Dell の視聴中（ペア済みの点灯またはアプリ起動中）にローカル時刻 21:00〜06:00 に 15 分間隔で音量を下げ、元音量へ復元して電源 OFF を試す。未ペアリングは `quit_app` と日次通知まで | `AUTO_TARGET_NAME=Dell`, `AUTO_POWER_OFF_ALERT_FILE=logs/auto_power_off_alert_state.json`, `AUTO_NIGHT_START=21:00`, `AUTO_NIGHT_END=06:00`, `AUTO_NIGHT_FORCE=0`, `AUTO_RESTORE_VOLUME=1`, `AUTO_RESTORE_WAIT_SEC=5`, `AUTO_MIN_LEVEL=0.05`, `AUTO_LOWERED_THRESHOLD=3`, `AUTO_STATE_FILE=logs/auto_standby_state.json`, `AUTO_STATE_STALE_SEC=1800`, `DISCOVERY_ALERT_COMMAND`（LAN 上 0 台が `DISCOVERY_ALERT_AFTER_TICKS=2` 回続いたら終了コード 2 と通知、回復時も 1 回通知） |
| `nemucast-standby` | 20:00 の即 OFF 用。テレビ本体の電源を切り、読み戻して確認する。音量制御も state も持たない。失敗は exit 1 + `DISCOVERY_ALERT_COMMAND` | `CHROMECAST_NAME`, `TV_CERT_DIR`, `TV_POWER_*` |
| `nemucast` | 手動デバッグ用。1回の tick 実行または `--run-until-standby` で継続実行 | `INTERVAL_SEC=1200`, `INACTIVE_THRESHOLD=3`, `STATE_FILE=logs/activity_state.json` |

## state ファイル

state を持つのは `nemucast-auto` と `nemucast`（手動）の 2 つで、ファイルは分ける。

`nemucast-auto`（`logs/auto_standby_state.json`）:

| キー | 意味 |
|------|------|
| `last_lowered_to` | 直近で自動設定した音量 |
| `consecutive_lowered` | 連続で音量を下げた回数。`AUTO_LOWERED_THRESHOLD` に達した tick で対象の電源 OFF を試す（未ペアリングは `quit_app`） |
| `original_volume` | セッション開始時の音量。手動上昇時に更新。セッション終了・視聴終了・時間外の後始末時の復元用 |
| `updated_at` | 最終更新時刻（UNIX 秒） |

`nemucast`（`logs/activity_state.json`）:

| キー | 意味 |
|------|------|
| `last_auto_volume` | 直近で自動設定した音量 |
| `inactive_streak` | 連続で非アクティブと判定された回数 |
| `updated_at` | 最終更新時刻（UNIX 秒） |

- stale 判定
  - `nemucast`: `now - updated_at > INTERVAL_SEC * STATE_STALE_INTERVAL_MULTIPLIER`（既定 2 倍）を超えたら破棄して再スタート
  - `nemucast-auto`: `AUTO_STATE_STALE_SEC`（既定 1800 秒）を超えたら連続カウントを捨てる。ただし復元用 `original_volume` と、閾値到達後の電源 OFF 再試行は保持する
- state が壊れて読めない場合は作り直して続行する（毎 tick 例外で落ちて自力復帰できなくなるのを防ぐ）。書き込みは一時ファイル + `os.replace` でアトミックに行う
- `nemucast-auto` は電源 OFF 成功時・未ペアリング時のアプリ終了試行後・視聴終了の音量復元後・時間外の後始末完了時に state を初期化する。視聴終了と時間外の復元は手動の音量上昇を上書きしない。`nemucast-standby` は state を操作しない
- 窓外でも `original_volume` があり state が stale でなければ通常制御を続行する。stale なら音量を復元して終了する
- 目的名不在通知は `AUTO_NOT_FOUND_ALERT_FILE=logs/auto_not_found_alert_state.json` で管理し、窓内の不在が `AUTO_NOT_FOUND_ALERT_AFTER_TICKS=4` 回続いたら一度通知、回復も一度通知する。通知失敗時は次 tick で再送する。LAN 0 台検知とは別管理で、壊れたファイルは初期化する
- 電源 OFF 不可の通知日（ローカル日付 YYYY-MM-DD）は `AUTO_POWER_OFF_ALERT_FILE=logs/auto_power_off_alert_state.json` に保存する。同日は一度まで、翌日は再通知し、通知失敗時は送信日を記録せず再送待ちだけ保持し、セッション終了後や窓外でも次 tick で再送する。ペアリング済みなら再送待ちを解除して送らない。壊れたファイルは未通知として扱う。
- auto のログ語彙は `outside_window / not_watching / not_found / lowered / turned_off / power_off_unavailable / cleanup`（ペアリング後の OFF 時にすでに消灯なら `power_off` の戻り値 `already_off`）。毎 tick、接続・機器照合後に `対象機器の生値: device=... is_on=... app_id=... display_name=... is_idle=... is_stand_by=... is_active_input=... volume=...` を INFO で記録する。`app_id` / `display_name` は制御文字を escape し、各 64 文字以内に制限する。
- 各機器処理の正常終了時に `tick 要約: device=Dell is_on=True app_id=None volume_before=0.44 volume_after=0.40 count=1 result=lowered` を INFO で 1 行出す。`grep "tick 要約" logs/auto_standby.log` で確認できる。`is_on` / `app_id` は操作前（未ペアの `is_on` は `None`）、音量は開始時と終了時の目標値（不明は `None`）。`count` はその tick の連続回数（`not_watching` / `cleanup` は初期化後の 0、OFF 結果は初期化前）。機器名・アプリ ID は制御文字を escape し、各 64 文字以内に制限する。
- 発見一覧に目的名があるのに接続候補が空の時は `DISCOVERY_RETRY_WAIT_SEC=1` 秒待ち、初回に加えて最大 `DISCOVERY_MAX_RETRIES=3` 回再探索する

## 環境変数

全ての環境変数とデフォルト値は `.env.example` を source of truth とする。
追加・削除する場合は `.env.example` と `README.md` の表、および `src/nemucast/config.py` の `os.getenv` デフォルトをセットで更新する（重複記載の整合性に注意）。

## コード規約

- Python 3.11+ / 型ヒント必須 / Google Style docstring
- `print` 禁止 → `logger` を使う（`logs/lower_cast_volume.log` に RotatingFileHandler で出力済み）
- マジックナンバー禁止。環境変数化できるものはモジュールトップで `os.getenv` を通す
- 早期リターンでネストを減らす。1 関数 40 行以下を目安
- バグ修正時は再発防止テストを追加（`tests/test_*.py`）
- 絵文字はコード・コミットメッセージに入れない（README 内の既存絵文字は保持）

## CI / レビュー

- CI（`.github/workflows/ci.yaml`）は push / PR で `uv run ruff check` と `uv run pytest -q` を実行
- PR 作成前に `uv run ruff format` と `uv run pytest -q` をローカルで通すこと
- ロジック変更を含む PR は `/review` + `/security-review` を通過させてからマージする
