"""auto_standby (15分間隔の最小ロジック) のテスト"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from nemucast.auto_standby import (
    TickResult,
    fresh_state,
    get_active_app_label,
    load_state,
    run_tick,
    save_state,
)


def make_cast(volume: float) -> Mock:
    cast = Mock()
    cast.status.volume_level = volume
    cast.status.app_id = None
    cast.status.display_name = None
    return cast


class TestRunTick:
    def test_lower_volume_first_tick(self) -> None:
        cast = make_cast(0.50)
        state = fresh_state("Dell")
        result, new_state = run_tick(cast=cast, state=state, step=-0.04, min_level=0.05)

        assert result == TickResult.LOWERED
        cast.set_volume.assert_called_once_with(0.46)
        assert new_state["last_lowered_to"] == 0.46
        assert new_state["consecutive_lowered"] == 1
        assert new_state["powered_off"] is False

    def test_three_consecutive_lowered_triggers_standby(self) -> None:
        cast = make_cast(0.10)
        state = fresh_state("Dell")
        state["consecutive_lowered"] = 3
        state["last_lowered_to"] = 0.10

        result, new_state = run_tick(cast=cast, state=state, step=-0.04, min_level=0.05)

        assert result == TickResult.STANDBY
        cast.quit_app.assert_called_once()
        cast.set_volume.assert_not_called()
        assert new_state["powered_off"] is True
        assert new_state["volume_at_power_off"] == 0.10

    def test_manual_rise_resets_counter(self) -> None:
        cast = make_cast(0.50)  # 手動で上げた値
        state = fresh_state("Dell")
        state["consecutive_lowered"] = 2
        state["last_lowered_to"] = 0.42  # script が下げた直近値

        result, new_state = run_tick(cast=cast, state=state, step=-0.04, min_level=0.05)

        assert result == TickResult.LOWERED
        assert new_state["consecutive_lowered"] == 1  # 0 にリセット → +1 で 1
        cast.set_volume.assert_called_once_with(0.46)

    def test_powered_off_with_no_rise_does_nothing(self) -> None:
        cast = make_cast(0.10)
        state = fresh_state("Dell")
        state["powered_off"] = True
        state["volume_at_power_off"] = 0.10

        result, new_state = run_tick(cast=cast, state=state, step=-0.04, min_level=0.05)

        assert result == TickResult.SKIP
        cast.set_volume.assert_not_called()
        cast.quit_app.assert_not_called()
        assert new_state["powered_off"] is True

    def test_powered_off_with_rise_resumes_session(self) -> None:
        cast = make_cast(0.40)  # ユーザーが復帰、音量上昇
        state = fresh_state("Dell")
        state["powered_off"] = True
        state["volume_at_power_off"] = 0.10

        result, new_state = run_tick(cast=cast, state=state, step=-0.04, min_level=0.05)

        assert result == TickResult.LOWERED
        assert new_state["powered_off"] is False
        assert new_state["volume_at_power_off"] is None
        assert new_state["consecutive_lowered"] == 1
        cast.set_volume.assert_called_once_with(0.36)

    def test_powered_off_with_active_app_resumes_session_without_volume_rise(self) -> None:
        cast = make_cast(0.10)
        cast.status.app_id = "AndroidNativeApp"
        state = fresh_state("Dell")
        state["powered_off"] = True
        state["volume_at_power_off"] = 0.10

        result, new_state = run_tick(cast=cast, state=state, step=-0.04, min_level=0.05)

        assert result == TickResult.LOWERED
        assert new_state["powered_off"] is False
        assert new_state["consecutive_lowered"] == 1
        cast.set_volume.assert_called_once_with(0.06)

    def test_min_level_does_not_set_volume_but_increments_count(self) -> None:
        cast = make_cast(0.05)
        state = fresh_state("Dell")
        state["last_lowered_to"] = 0.05
        state["consecutive_lowered"] = 1

        result, new_state = run_tick(cast=cast, state=state, step=-0.04, min_level=0.05)

        assert result == TickResult.LOWERED
        cast.set_volume.assert_not_called()
        assert new_state["consecutive_lowered"] == 2

    def test_full_cycle_three_lowers_then_standby(self) -> None:
        """tick を 4 回回して 4 回目で quit_app に到達することを確認"""
        state = fresh_state("Dell")
        volumes = [0.50, 0.46, 0.42, 0.38]

        # tick 1〜3: lower
        for i, v in enumerate(volumes[:3], start=1):
            cast = make_cast(v)
            result, state = run_tick(cast=cast, state=state, step=-0.04, min_level=0.05)
            assert result == TickResult.LOWERED, f"tick {i}"
            assert state["consecutive_lowered"] == i

        # tick 4: standby
        cast = make_cast(volumes[3])
        result, state = run_tick(cast=cast, state=state, step=-0.04, min_level=0.05)
        assert result == TickResult.STANDBY
        cast.quit_app.assert_called_once()
        assert state["powered_off"] is True
        assert state["volume_at_power_off"] == 0.38


class TestActiveAppLabel:
    def test_returns_app_id_first(self) -> None:
        cast = make_cast(0.10)
        cast.status.app_id = "E8C28D3C"
        cast.status.display_name = "YouTube"

        assert get_active_app_label(cast) == "E8C28D3C"

    def test_uses_display_name_when_app_id_missing(self) -> None:
        cast = make_cast(0.10)
        cast.status.display_name = "YouTube"

        assert get_active_app_label(cast) == "YouTube"

    def test_ignores_mock_placeholder_values(self) -> None:
        cast = Mock()
        cast.status.volume_level = 0.10

        assert get_active_app_label(cast) is None


class TestStatePersistence:
    def test_load_state_returns_none_when_missing(self, tmp_path: Path) -> None:
        assert load_state(tmp_path / "missing.json", "Dell") is None

    def test_load_state_returns_none_for_other_device(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"device_name": "OtherTV"}), encoding="utf-8")
        assert load_state(path, "Dell") is None

    def test_save_and_reload_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = fresh_state("Dell")
        state["consecutive_lowered"] = 2
        save_state(path, state)

        loaded = load_state(path, "Dell")
        assert loaded is not None
        assert loaded["consecutive_lowered"] == 2
        assert loaded["device_name"] == "Dell"

    def test_save_state_updates_timestamp(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = fresh_state("Dell")
        original_ts = state["updated_at"]
        save_state(path, state)
        loaded = load_state(path, "Dell")
        assert loaded is not None
        assert loaded["updated_at"] >= original_ts

    def test_broken_state_starts_over_instead_of_crashing(self, tmp_path: Path) -> None:
        """壊れた state を読んでも例外を投げない（毎 tick クラッシュして永久停止しないこと）"""
        path = tmp_path / "state.json"
        path.write_text('{"device_name": "Dell", ', encoding="utf-8")

        assert load_state(path, "Dell") is None

    def test_save_state_leaves_no_temp_file(self, tmp_path: Path) -> None:
        """一時ファイル経由で置き換えても後片付けされる"""
        path = tmp_path / "state.json"
        save_state(path, fresh_state("Dell"))

        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_stale_state_resets_lowered_count(self, tmp_path: Path) -> None:
        """長時間空いた state は連続カウントを捨てる（再開直後に quit_app しないこと）"""
        path = tmp_path / "state.json"
        state = fresh_state("Dell")
        state["consecutive_lowered"] = 2
        state["last_lowered_to"] = 0.4
        save_state(path, state)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["updated_at"] = time.time() - 3600
        path.write_text(json.dumps(state), encoding="utf-8")

        loaded = load_state(path, "Dell")

        assert loaded is not None
        assert loaded["consecutive_lowered"] == 0
        assert loaded["last_lowered_to"] is None

    def test_stale_state_keeps_powered_off(self, tmp_path: Path) -> None:
        """電源OFF状態は時間が空いても維持する（勝手に音量を下げ直さないこと）"""
        path = tmp_path / "state.json"
        state = fresh_state("Dell")
        state["powered_off"] = True
        state["volume_at_power_off"] = 0.5
        state["updated_at"] = time.time() - 86400
        path.write_text(json.dumps(state), encoding="utf-8")

        loaded = load_state(path, "Dell")

        assert loaded is not None
        assert loaded["powered_off"] is True
        assert loaded["volume_at_power_off"] == 0.5


class TestMain:
    def test_main_succeeds_when_chromecast_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_network: Callable[[list[str]], Any],
    ) -> None:
        """デバイス不在は失敗ではない（15分ごとの偽アラートを出さないこと）"""
        from nemucast import auto_standby

        monkeypatch.chdir(tmp_path)
        with fake_network(["OtherDevice"]) as network:
            auto_standby.main(args=["--name", "Missing"])

        network["browser"].stop_discovery.assert_called_once()

    def test_main_persists_tick_result(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_network: Callable[[list[str]], Any],
    ) -> None:
        """cron から叩かれる経路（探索 → tick → state 書き出し）が繋がっている"""
        from nemucast import auto_standby

        monkeypatch.chdir(tmp_path)
        state_file = tmp_path / "auto_state.json"
        monkeypatch.setattr(auto_standby, "AUTO_STATE_FILE", state_file)

        with fake_network(["Dell"]) as network:
            network["casts"]["Dell"].status.volume_level = 0.5
            auto_standby.main(args=["--name", "Dell"])

        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert saved["device_name"] == "Dell"
        assert saved["consecutive_lowered"] == 1
        assert saved["last_lowered_to"] == pytest.approx(0.46)
