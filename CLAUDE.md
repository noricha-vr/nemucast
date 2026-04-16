# CLAUDE.md

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
| エントリポイント | `nemucast` / `nemucast-cron-20` / `nemucast-cron-0030` |

## プロジェクト概要

Chromecast / Google TV の音量を徐々に下げて、一定時間ユーザー操作がなければ standby にする CLI ツール。
Chromecast の active / idle 判定には依存せず、`前回自動設定した音量` と `現在音量` の差分からユーザー活動を推定する。

## ディレクトリ構成

| パス | 役割 |
|------|------|
| `src/nemucast/main.py` | 主要ロジック（CLI エントリポイント `main` / `main_cron_20` / `main_cron_0030`、音量制御、state 管理） |
| `src/nemucast/__main__.py` | `python -m nemucast` 用のエントリ |
| `tests/` | pytest テスト（`test_args.py`, `test_volume_control.py`, `test_refactored_functions.py`） |
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
uv run nemucast
uv run nemucast --interval 900 --inactive-threshold 4 --run-until-standby
```

## CLI エントリポイント

| コマンド | 用途 | 既定プロファイル |
|----------|------|------------------|
| `nemucast` | 汎用。1回の tick 実行または `--run-until-standby` で継続実行 | `INTERVAL_SEC=1200`, `INACTIVE_THRESHOLD=3`, `STATE_FILE=logs/activity_state.json` |
| `nemucast-cron-20` | 20:00 用。起動直後に即 standby する運用向け | `INTERVAL_SEC=60`, `INACTIVE_THRESHOLD=1`, `MIN_LEVEL=0.05`, `STATE_FILE=logs/activity_state_20.json` |
| `nemucast-cron-0030` | 00:30 用。15 分ごとに音量を下げながら 45 分で standby する運用向け | `INTERVAL_SEC=900`, `INACTIVE_THRESHOLD=4`, `MIN_LEVEL=0.35`, `STATE_FILE=logs/activity_state_0030.json` |

プロファイル別の上書きは `CRON_20_*` / `CRON_0030_*` 環境変数で行う。詳細は `.env.example`。

## state ファイル

活動判定は `logs/activity_state*.json` に保存した状態から算出する。

| キー | 意味 |
|------|------|
| `last_auto_volume` | 直近で自動設定した音量 |
| `inactive_streak` | 連続で非アクティブと判定された回数 |
| `updated_at` | 最終更新時刻（UNIX 秒） |

- stale 判定: `now - updated_at > INTERVAL_SEC * STATE_STALE_INTERVAL_MULTIPLIER`（既定 2 倍）を超えたら破棄して再スタート
- standby 実行時は state を削除し、次回起動時にクリーンな状態から開始する
- cron プロファイル間で state を共有しないよう、プロファイルごとにファイルを分ける

## 環境変数

全ての環境変数とデフォルト値は `.env.example` を source of truth とする。
追加・削除する場合は `.env.example` と `README.md` の表、および `src/nemucast/main.py` の `os.getenv` デフォルトをセットで更新する（重複記載の整合性に注意）。

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
