# AGENTS.md

AI エージェント（Claude Code / Codex 等）がこのリポジトリを操作する際の前提情報。
プロダクトの使い方や環境変数の意味は `README.md` / `.env.example` を参照すること。
ここではエージェント向けに「構成・規約・開発コマンド」を集約する。

## Quick Reference

| 項目 | 値 |
|------|-----|
| プロダクト | ねむキャス（Chromecast / Google TV 自動 standby ツール） |
| 言語 | Python 3.11+ |
| パッケージマネージャ | uv（`pip` / ローカル `python` は使わない） |
| 主要ライブラリ | pychromecast, python-dotenv |
| テスト | pytest + pytest-mock |
| Lint / Format | ruff（`select = ["E", "F", "I", "N", "W", "UP"]`, line-length 100） |
| pre-commit | ruff-check --fix / ruff-format --check |
| CI | `.github/workflows/ci.yaml`（push/PR で ruff + pytest を実行） |
| エントリポイント | `nemucast` / `nemucast-standby` / `nemucast-auto` |

## プロジェクト概要

Chromecast / Google TV の音量を徐々に下げて、一定時間ユーザー操作がなければ standby にする CLI ツール。
Chromecast の active / idle 判定には依存せず、`前回自動設定した音量` と `現在音量` の差分からユーザー活動を推定する。

## ディレクトリ構成

| パス | 役割 |
|------|------|
| `src/nemucast/cli.py` | `nemucast`（手動実行）のエントリポイント、argparse、ロギング設定 |
| `src/nemucast/standby.py` | `nemucast-standby` のエントリポイント。発見 → `quit_app` → 終了だけを行う |
| `src/nemucast/auto_standby.py` | `nemucast-auto` のエントリポイント。1 tick 分の最小ロジック（下げる / 電源OFF / 何もしない） |
| `src/nemucast/config.py` | 環境変数から読み込む設定定数 |
| `src/nemucast/cast_client.py` | Chromecast の検索・接続・standby 制御 |
| `src/nemucast/state.py` | 活動判定 state JSON の読み書き・整合性判定（`nemucast` 手動実行用） |
| `src/nemucast/volume.py` | 音量計算と 1 tick 分の制御ループ（`VolumeSessionConfig` / `TickResult`。`nemucast` 手動実行用） |
| `src/nemucast/__main__.py` | `python -m nemucast` 用のエントリ |
| `tests/` | pytest テスト。`conftest.py`（共通 fixture）と `test_args.py` / `test_volume_control.py` / `test_state.py` / `test_volume.py` / `test_cast_client.py` / `test_cli.py` / `test_standby.py` / `test_auto_standby.py` に分割 |
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
uv run nemucast-auto          # 1 tick 分の自動寝かしつけ（periodic-worker が 15 分間隔で叩く）
uv run nemucast-standby       # 発見して quit_app するだけ
uv run nemucast               # 手動デバッグ用
uv run nemucast --interval 900 --inactive-threshold 4 --run-until-standby
```

## CLI エントリポイント

| コマンド | 用途 | 主な設定 |
|----------|------|----------|
| `nemucast-auto` | 常用。periodic-worker から 15 分間隔・24 時間で実行し、音量を下げながら自動で電源OFFにする | `AUTO_MIN_LEVEL=0.05`, `AUTO_LOWERED_THRESHOLD=3`, `AUTO_STATE_FILE=logs/auto_standby_state.json`, `AUTO_STATE_STALE_SEC=1800` |
| `nemucast-standby` | 20:00 用。発見して `quit_app` するだけ。音量制御も state も持たない | `CHROMECAST_NAME` のみ |
| `nemucast` | 手動デバッグ用。1回の tick 実行または `--run-until-standby` で継続実行 | `INTERVAL_SEC=1200`, `INACTIVE_THRESHOLD=3`, `STATE_FILE=logs/activity_state.json` |

## state ファイル

state を持つのは `nemucast-auto` と `nemucast`（手動）の 2 つで、ファイルは分ける。

`nemucast-auto`（`logs/auto_standby_state.json`）:

| キー | 意味 |
|------|------|
| `last_lowered_to` | 直近で自動設定した音量 |
| `consecutive_lowered` | 連続で音量を下げた回数。`AUTO_LOWERED_THRESHOLD` に達したら `quit_app` |
| `powered_off` | 電源OFF済みか。true の間は音量上昇を検知するまで何もしない |
| `volume_at_power_off` | 電源OFF時点の音量。再開判定の基準値 |
| `updated_at` | 最終更新時刻（UNIX 秒） |

`nemucast`（`logs/activity_state.json`）:

| キー | 意味 |
|------|------|
| `last_auto_volume` | 直近で自動設定した音量 |
| `inactive_streak` | 連続で非アクティブと判定された回数 |
| `updated_at` | 最終更新時刻（UNIX 秒） |

- stale 判定
  - `nemucast`: `now - updated_at > INTERVAL_SEC * STATE_STALE_INTERVAL_MULTIPLIER`（既定 2 倍）を超えたら破棄して再スタート
  - `nemucast-auto`: `AUTO_STATE_STALE_SEC`（既定 1800 秒）を超えたら連続カウントを捨てる。ただし `powered_off` は維持する（「音量上昇 / active app まで何もしない」が不変条件のため）
- state が壊れて読めない場合は作り直して続行する（毎 tick 例外で落ちて自力復帰できなくなるのを防ぐ）。書き込みは一時ファイル + `os.replace` でアトミックに行う
- standby 実行時は state を削除し、次回起動時にクリーンな状態から開始する

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
