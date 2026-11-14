"""auto_standby (15分間隔の最小ロジック) のテスト"""

from __future__ import annotations

import json
import logging
import runpy
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pychromecast import IDLE_APP_ID
from pychromecast.error import PyChromecastError

from nemucast.auto_standby import (
    TickResult,
    fresh_state,
    in_night_window,
    load_state,
    run_tick,
    save_state,
)
from tests.test_atv_volume import make_atv_rig


def make_cast(volume: float | None) -> Mock:
    cast = Mock()
    cast.status.volume_level = volume
    cast.status.app_id = "AndroidNativeApp"
    cast.status.display_name = "U-NEXT for TV"
    return cast


@pytest.fixture(autouse=True)
def isolated_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """実時刻や実機・ホームの identity に依存させない。"""
    from nemucast import auto_standby

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auto_standby, "AUTO_NIGHT_FORCE", True)
    monkeypatch.setattr(auto_standby, "AUTO_RESTORE_VOLUME", True)
    monkeypatch.setattr(auto_standby, "AUTO_STATE_FILE", tmp_path / "auto.json")
    monkeypatch.setattr(auto_standby, "DISCOVERY_ALERT_FILE", tmp_path / "lan_alert.json")
    monkeypatch.setattr(auto_standby, "AUTO_NOT_FOUND_ALERT_FILE", tmp_path / "missing_alert.json")
    monkeypatch.setattr(auto_standby, "AUTO_NOT_FOUND_ALERT_AFTER_TICKS", 4)
    monkeypatch.setattr(auto_standby, "load_identity", Mock(return_value="aa:bb"))
    monkeypatch.setattr(auto_standby, "AUTO_POWER_OFF_ALERT_FILE", tmp_path / "power_alert.json")
    monkeypatch.setattr(auto_standby, "power_off", AsyncMock(return_value="turned_off"))
    monkeypatch.setattr(auto_standby, "read_power_state", AsyncMock(return_value=True))
    # Cast が読めないケースも実際の ATV 接続・通知には到達させない。
    control = Mock(read=AsyncMock(return_value=None))
    monkeypatch.setattr(auto_standby, "AtvVolumeControl", Mock(return_value=control))
    monkeypatch.setattr(auto_standby, "AUTO_VOLUME_ALERT_FILE", tmp_path / "volume_alert.json")


class TestRunTick:
    def test_first_tick_saves_original(self) -> None:
        cast = make_cast(0.50)
        result, state = run_tick(cast, fresh_state("Dell"))
        assert result is TickResult.LOWERED
        assert state["original_volume"] == 0.50
        assert state["consecutive_lowered"] == 1
        cast.set_volume.assert_called_once_with(0.46)

    def test_third_lower_reaches_threshold(self) -> None:
        state = fresh_state("Dell")
        for count, volume in enumerate([0.50, 0.46, 0.42], start=1):
            cast = make_cast(volume)
            result, state = run_tick(cast, state)
            assert state["consecutive_lowered"] == count
        assert result is TickResult.STANDBY
        assert state["original_volume"] == 0.50
        cast.quit_app.assert_not_called()

    def test_manual_rise_updates_original_and_resets_count(self) -> None:
        state = fresh_state("Dell")
        state.update(consecutive_lowered=2, last_lowered_to=0.42, original_volume=0.5)
        result, state = run_tick(make_cast(0.60), state)
        assert result is TickResult.LOWERED
        assert state["consecutive_lowered"] == 1
        assert state["original_volume"] == 0.60

    @pytest.mark.parametrize("volume", [0.05, 0.02])
    def test_below_minimum_never_raises_volume(self, volume: float) -> None:
        cast = make_cast(volume)
        _, state = run_tick(cast, fresh_state("Dell"))
        cast.set_volume.assert_not_called()
        assert state["consecutive_lowered"] == 1
        assert state["last_lowered_to"] == volume

    def test_pending_power_off_does_not_misread_restored_volume(self) -> None:
        cast = make_cast(0.5)
        state = fresh_state("Dell")
        state.update(consecutive_lowered=3, original_volume=0.5, last_lowered_to=0.38)
        result, state = run_tick(cast, state)
        assert result is TickResult.STANDBY
        assert state["consecutive_lowered"] == 3
        cast.set_volume.assert_not_called()


@pytest.mark.parametrize(
    "clock,start,end,expected",
    [
        ("00:29:59", "00:30", "05:00", False),
        ("00:30:00", "00:30", "05:00", True),
        ("04:59:59", "00:30", "05:00", True),
        ("05:00:00", "00:30", "05:00", False),
        ("12:00:00", "00:30", "05:00", False),
        ("23:00:00", "23:00", "02:00", True),
        ("00:00:00", "23:00", "02:00", True),
        ("01:59:59", "23:00", "02:00", True),
        ("02:00:00", "23:00", "02:00", False),
        ("22:59:59", "23:00", "02:00", False),
        ("00:30:00", "00:30", "00:30", False),
    ],
)
def test_night_window(clock: str, start: str, end: str, expected: bool) -> None:
    assert in_night_window(datetime.fromisoformat(f"2026-11-05T{clock}"), start, end) is expected


@pytest.fixture
def config_defaults(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """個人の .env に触れず、環境変数未指定時の既定値を検証する。"""
    from nemucast import config

    for key in (
        "AUTO_TARGET_NAME",
        "AUTO_POWER_OFF_ALERT_FILE",
        "AUTO_NIGHT_START",
        "AUTO_NIGHT_END",
        "DISCOVERY_MAX_RETRIES",
        "DISCOVERY_RETRY_WAIT_SEC",
        "AUTO_NOT_FOUND_ALERT_AFTER_TICKS",
        "AUTO_NOT_FOUND_ALERT_FILE",
        "LOG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    return runpy.run_path(config.__file__)


def test_new_config_defaults(config_defaults: dict[str, Any]) -> None:
    assert config_defaults["AUTO_TARGET_NAME"] == "Dell"
    assert config_defaults["AUTO_POWER_OFF_ALERT_FILE"] == Path(
        "logs/auto_power_off_alert_state.json"
    )
    assert config_defaults["AUTO_NIGHT_START"] == "21:00"
    assert config_defaults["AUTO_NIGHT_END"] == "06:00"
    assert config_defaults["DISCOVERY_MAX_RETRIES"] == 3
    assert config_defaults["DISCOVERY_RETRY_WAIT_SEC"] == 1
    assert config_defaults["AUTO_NOT_FOUND_ALERT_AFTER_TICKS"] == 4
    assert config_defaults["AUTO_NOT_FOUND_ALERT_FILE"] == Path(
        "logs/auto_not_found_alert_state.json"
    )


@pytest.mark.parametrize(
    "clock,expected",
    [
        ("20:59:00", False),
        ("21:00:00", True),
        ("23:59:00", True),
        ("00:00:00", True),
        ("05:59:00", True),
        ("06:00:00", False),
        ("12:00:00", False),
    ],
)
def test_default_night_window_boundaries(
    clock: str, expected: bool, config_defaults: dict[str, Any]
) -> None:
    assert (
        in_night_window(
            datetime.fromisoformat(f"2026-11-10T{clock}"),
            config_defaults["AUTO_NIGHT_START"],
            config_defaults["AUTO_NIGHT_END"],
        )
        is expected
    )


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
        # 停止中の手動上昇を次 tick で検出できるよう残す
        assert loaded["last_lowered_to"] == 0.4

    @pytest.mark.parametrize("count", [2, 3])
    def test_stale_state_keeps_original_and_pending_shutdown(
        self, tmp_path: Path, count: int
    ) -> None:
        path = tmp_path / "state.json"
        state = fresh_state("Dell")
        state.update(consecutive_lowered=count, original_volume=0.5)
        state["updated_at"] = time.time() - 86400
        path.write_text(json.dumps(state), encoding="utf-8")
        loaded = load_state(path, "Dell")
        assert loaded["original_volume"] == 0.5
        assert loaded["consecutive_lowered"] == (3 if count == 3 else 0)


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


class TestDiscoveryAlert:
    @pytest.fixture
    def notify(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:
        from nemucast import auto_standby

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(auto_standby, "DISCOVERY_ALERT_COMMAND", "notify --message")
        monkeypatch.setattr(auto_standby, "DISCOVERY_ALERT_AFTER_TICKS", 2)
        # このクラスは LAN 全体の障害通知だけを検証する。目的名不在は別テストで検証
        monkeypatch.setattr(auto_standby, "AUTO_NOT_FOUND_ALERT_AFTER_TICKS", 0)
        run = Mock()
        monkeypatch.setattr(auto_standby.subprocess, "run", run)
        return run

    def test_zero_devices_confirmed_after_threshold_and_alerts_once(
        self, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        """LAN 上 0 台が続いたら非ゼロ終了。1 回目は様子見、通知は確定した時の 1 回だけ"""
        from nemucast import auto_standby

        with fake_network([]) as network:
            auto_standby.main(args=["--name", "Dell"])
        network["browser"].stop_discovery.assert_called_once()
        notify.assert_not_called()

        for _ in range(2):
            with fake_network([]), pytest.raises(SystemExit) as exc:
                auto_standby.main(args=["--name", "Dell"])
            assert exc.value.code == auto_standby.EXIT_NO_DEVICE_ON_LAN

        notify.assert_called_once()
        assert notify.call_args.args[0][:2] == ["notify", "--message"]
        assert "1 台も見えません" in notify.call_args.args[0][-1]

    def test_failed_alert_is_retried_next_tick(
        self, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        """通知が届かなかったら次の tick で送り直す（障害を黙って見逃さない）"""
        from nemucast import auto_standby

        notify.side_effect = [subprocess.CalledProcessError(1, "notify", stderr="boom"), None]
        with fake_network([]):
            auto_standby.main(args=["--name", "Dell"])
        for _ in range(3):
            with fake_network([]), pytest.raises(SystemExit):
                auto_standby.main(args=["--name", "Dell"])

        assert notify.call_count == 2

    def test_recovery_alerts_once_and_clears_flag(
        self, tmp_path: Path, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        """再び見えたら回復を 1 回だけ通知し、以後は鳴らさない"""
        from nemucast import auto_standby

        with fake_network([]):
            auto_standby.main(args=["--name", "Dell"])
        with fake_network([]), pytest.raises(SystemExit):
            auto_standby.main(args=["--name", "Dell"])
        for _ in range(2):
            with fake_network(["キッチン"]):
                auto_standby.main(args=["--name", "Dell"])

        assert notify.call_count == 2
        assert "再び見える" in notify.call_args.args[0][-1]
        assert not auto_standby.DISCOVERY_ALERT_FILE.exists()

    def test_single_transient_zero_does_not_alert(
        self, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        """一瞬だけ 0 台（スリープ復帰直後等）は障害にも回復にも数えない"""
        from nemucast import auto_standby

        with fake_network([]):
            auto_standby.main(args=["--name", "Dell"])
        with fake_network(["キッチン"]):
            auto_standby.main(args=["--name", "Dell"])
        with fake_network([]):
            auto_standby.main(args=["--name", "Dell"])

        notify.assert_not_called()

    def test_disabled_when_after_ticks_is_zero(
        self,
        notify: Mock,
        monkeypatch: pytest.MonkeyPatch,
        fake_network: Callable[[list[str]], Any],
    ) -> None:
        """LAN に対象 1 台だけの家庭向けに判定を切れる"""
        from nemucast import auto_standby

        monkeypatch.setattr(auto_standby, "DISCOVERY_ALERT_AFTER_TICKS", 0)
        for _ in range(3):
            with fake_network([]):
                auto_standby.main(args=["--name", "Dell"])

        notify.assert_not_called()

    @pytest.mark.parametrize(
        "content",
        [
            "[]",
            "null",
            '{"zero_ticks": "x", "alerted": false}',
            "{",
            '{"zero_ticks": true, "alerted": false}',
            '{"zero_ticks": -5, "alerted": false}',
            b"\xff\xfe",
        ],
    )
    def test_corrupted_alert_file_is_rebuilt(
        self,
        tmp_path: Path,
        notify: Mock,
        content: str | bytes,
        fake_network: Callable[[list[str]], Any],
    ) -> None:
        """壊れたフラグファイルで毎 tick 落ち続けない"""
        from nemucast import auto_standby

        flag = tmp_path / "lan_alert.json"
        if isinstance(content, bytes):
            flag.write_bytes(content)
        else:
            flag.write_text(content, encoding="utf-8")
        with fake_network([]):
            auto_standby.main(args=["--name", "Dell"])
        with fake_network(["Dell"]) as network:
            network["casts"]["Dell"].status.volume_level = 0.5
            auto_standby.main(args=["--name", "Dell"])

        assert not flag.exists()

    @pytest.mark.parametrize("command", ["no-such-command-xyz", 'notify "unclosed'])
    def test_broken_alert_command_does_not_raise(self, command: str) -> None:
        """通知コマンドが無い・書式ミスでも寝かしつけ本体は止めない"""
        from nemucast import auto_standby

        assert auto_standby.send_discovery_alert("msg", command=command) is False

    @staticmethod
    def _age_alert_file(tmp_path: Path, seconds: float) -> None:
        flag = tmp_path / "lan_alert.json"
        data = json.loads(flag.read_text(encoding="utf-8"))
        data["updated_at"] -= seconds
        flag.write_text(json.dumps(data), encoding="utf-8")

    def test_zero_count_expires_across_scheduler_gap(
        self, tmp_path: Path, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        """0 台 → スリープで長い間隔 → 復帰直後に 0 台、を連続と数えない（#31）"""
        from nemucast import auto_standby

        with fake_network([]):
            auto_standby.main(args=["--name", "Dell"])
        self._age_alert_file(tmp_path, auto_standby.AUTO_STATE_STALE_SEC + 60)
        with fake_network([]):
            auto_standby.main(args=["--name", "Dell"])

        notify.assert_not_called()

    def test_alerted_outage_continues_across_scheduler_gap(
        self, tmp_path: Path, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        """通知済みの障害は間隔が空いても継続扱い（再通知もしない）"""
        from nemucast import auto_standby

        with fake_network([]):
            auto_standby.main(args=["--name", "Dell"])
        with fake_network([]), pytest.raises(SystemExit):
            auto_standby.main(args=["--name", "Dell"])
        self._age_alert_file(tmp_path, auto_standby.AUTO_STATE_STALE_SEC + 60)
        with fake_network([]), pytest.raises(SystemExit) as exc:
            auto_standby.main(args=["--name", "Dell"])

        assert exc.value.code == auto_standby.EXIT_NO_DEVICE_ON_LAN
        notify.assert_called_once()


class TestNightMain:
    @pytest.fixture
    def outside(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nemucast import auto_standby

        monkeypatch.setattr(auto_standby, "AUTO_NIGHT_FORCE", False)
        monkeypatch.setattr(auto_standby, "in_night_window", Mock(return_value=False))

    def test_outside_does_not_discover_or_connect(self, outside: None, monkeypatch) -> None:
        from nemucast import auto_standby

        discover = Mock(side_effect=AssertionError("network forbidden"))
        monkeypatch.setattr(auto_standby, "discover_chromecasts", discover)
        auto_standby.main(["--name", "Dell"])
        discover.assert_not_called()
        auto_standby.power_off.assert_not_called()

    def test_force_bypasses_window(self, fake_network, monkeypatch) -> None:
        from nemucast import auto_standby

        window = Mock(side_effect=AssertionError("force bypass"))
        monkeypatch.setattr(auto_standby, "in_night_window", window)
        with fake_network(["Dell"]) as net:
            net["casts"]["Dell"].status.volume_level = 0.5
            auto_standby.main(["--name", "Dell"])
        window.assert_not_called()
        net["casts"]["Dell"].set_volume.assert_called_once_with(0.46)

    @pytest.mark.parametrize("app_id", [None, "", IDLE_APP_ID])
    def test_without_power_or_app_never_changes_volume_or_sends_power(
        self,
        app_id: str | None,
        fake_network: Callable[[list[str]], Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from nemucast import auto_standby

        auto_standby.read_power_state.return_value = False
        with fake_network(["Dell"]) as net, caplog.at_level(logging.INFO):
            cast = net["casts"]["Dell"]
            cast.status.volume_level = 0.44
            cast.status.app_id = app_id
            auto_standby.main(["--name", "Dell"])
        state = load_state(auto_standby.AUTO_STATE_FILE, "Dell")
        assert state["original_volume"] is None
        assert state["consecutive_lowered"] == 0
        cast.set_volume.assert_not_called()
        cast.wait.assert_called_once_with(timeout=auto_standby.CONNECT_TIMEOUT_SEC)
        cast.quit_app.assert_not_called()
        auto_standby.power_off.assert_not_called()
        auto_standby.load_identity.assert_called_once_with("Dell")
        assert "tick 結果: not_watching device=Dell" in caplog.messages
        net["browser"].stop_discovery.assert_called_once()

    @staticmethod
    def seed_session(count: int = 2, *, stale: bool = False) -> None:
        from nemucast import auto_standby

        state = fresh_state("Dell")
        state.update(original_volume=0.5, last_lowered_to=0.42, consecutive_lowered=count)
        save_state(auto_standby.AUTO_STATE_FILE, state)
        if stale:
            state["updated_at"] -= auto_standby.AUTO_STATE_STALE_SEC + 1
            auto_standby.AUTO_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    def test_restore_before_power_off_and_reset(self, fake_network, monkeypatch) -> None:
        from nemucast import auto_standby

        self.seed_session()
        calls = Mock()
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            cast.status.volume_level = 0.42
            calls.attach_mock(cast.set_volume, "volume")
            calls.attach_mock(cast.wait, "wait")
            calls.attach_mock(auto_standby.read_power_state, "read")
            calls.attach_mock(auto_standby.power_off, "off")
            auto_standby.main(["--name", "Dell"])
        assert [call[0] for call in calls.mock_calls] == ["wait", "read", "volume", "volume", "off"]
        assert calls.mock_calls[2].args == (0.38,)
        assert calls.mock_calls[3].args == (0.5,)
        assert auto_standby.power_off.await_args.args == (
            cast.cast_info.host,
            auto_standby.load_identity.return_value,
        )
        state = load_state(auto_standby.AUTO_STATE_FILE, "Dell")
        assert state["original_volume"] is None
        assert state["consecutive_lowered"] == 0

    def test_power_failure_preserves_state_and_retries(self, fake_network, monkeypatch) -> None:
        from nemucast import auto_standby
        from nemucast.tv_power import TvPowerError

        self.seed_session()
        auto_standby.power_off.side_effect = TvPowerError("not off")
        notify = Mock()
        monkeypatch.setattr(auto_standby, "send_discovery_alert", notify)
        with fake_network(["Dell"]) as net:
            net["casts"]["Dell"].status.volume_level = 0.42
            with pytest.raises(SystemExit) as exc:
                auto_standby.main(["--name", "Dell"])
        assert exc.value.code == 1
        state = load_state(auto_standby.AUTO_STATE_FILE, "Dell")
        assert state["consecutive_lowered"] == 3
        assert state["original_volume"] == 0.5
        notify.assert_called_once()
        auto_standby.power_off.side_effect = None
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            cast.status.volume_level = 0.5  # 失敗前に戻した音量
            auto_standby.main(["--name", "Dell"])
        cast.set_volume.assert_called_once_with(0.5)
        assert auto_standby.power_off.await_count == 2
        assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["original_volume"] is None

    def test_restore_can_be_disabled(self, fake_network, monkeypatch) -> None:
        from nemucast import auto_standby

        self.seed_session()
        monkeypatch.setattr(auto_standby, "AUTO_RESTORE_VOLUME", False)
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            cast.status.volume_level = 0.42
            auto_standby.main(["--name", "Dell"])
        cast.set_volume.assert_called_once_with(0.38)
        auto_standby.power_off.assert_awaited_once()

    @pytest.mark.parametrize("watching", [True, False, "unknown"])
    def test_cleanup_after_window(self, outside, watching, fake_network, monkeypatch) -> None:
        from nemucast import auto_standby

        self.seed_session(stale=True)
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            cast.status.volume_level = 0.42
            if watching == "unknown":
                cast.wait.side_effect = PyChromecastError("unavailable")
            elif not watching:
                cast.status.app_id = IDLE_APP_ID
            auto_standby.main(["--name", "Dell"])
        if watching != "unknown":
            cast.set_volume.assert_called_once_with(0.5)
        else:
            cast.set_volume.assert_not_called()
        cast.quit_app.assert_not_called()
        auto_standby.power_off.assert_not_called()
        state = load_state(auto_standby.AUTO_STATE_FILE, "Dell")
        assert state["original_volume"] == (0.5 if watching == "unknown" else None)

    def test_cleanup_does_not_overwrite_manual_volume_rise(self, outside, fake_network) -> None:
        """窓の終わり際に手動で上げられた音量を、古い元音量で上書きしない"""
        from nemucast import auto_standby

        self.seed_session(stale=True)
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            cast.status.volume_level = 0.9
            auto_standby.main(["--name", "Dell"])
        cast.set_volume.assert_not_called()
        assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["original_volume"] is None

    def test_missing_device_cleanup_keeps_session(self, outside, fake_network) -> None:
        from nemucast import auto_standby

        self.seed_session(stale=True)
        with fake_network([]):
            auto_standby.main(["--name", "Dell"])
        assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["original_volume"] == 0.5

    def test_invalid_window_fails_before_network(self, outside, monkeypatch) -> None:
        from nemucast import auto_standby

        monkeypatch.setattr(auto_standby, "in_night_window", Mock(side_effect=ValueError("bad")))
        discover = Mock()
        monkeypatch.setattr(auto_standby, "discover_chromecasts", discover)
        with pytest.raises(SystemExit) as exc:
            auto_standby.main(["--name", "Dell"])
        assert exc.value.code == 1
        discover.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [
        {"consecutive_lowered": "3"},
        {"consecutive_lowered": True},
        {"consecutive_lowered": -1},
        {"original_volume": "0.5"},
        {"original_volume": float("nan")},
        {"last_lowered_to": 2.0},
    ],
)
def test_invalid_session_state_is_rebuilt(tmp_path, bad) -> None:
    path = tmp_path / "state.json"
    state = fresh_state("Dell")
    state.update(bad)
    path.write_text(json.dumps(state))
    assert load_state(path, "Dell") is None


def test_non_utf8_state_is_rebuilt(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"\xff\xfe")
    assert load_state(path, "Dell") is None


def test_legacy_state_drops_obsolete_flags(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = fresh_state("Dell")
    state.pop("original_volume")
    state.update(powered_off=True, volume_at_power_off=0.5)
    save_state(path, state)
    # 旧形式は引き継がず作り直す（フラグだけ捨ててカウントを残すと初回 tick で即 OFF になる）
    assert load_state(path, "Dell") is None


def test_control_failure_alerts_only_when_session_in_progress(tmp_path, monkeypatch) -> None:
    """制御失敗の通知は、音量を下げ始めた後だけ（夜通しの通知で埋もれない）"""
    from nemucast import auto_standby as module

    for original_volume, expected_calls in ((None, 0), (0.5, 1)):
        state = module.fresh_state("Living")
        state["original_volume"] = original_volume
        state_file = tmp_path / f"s{expected_calls}.json"
        monkeypatch.setattr(module, "AUTO_STATE_FILE", state_file)
        module.save_state(state_file, state)
        monkeypatch.setattr(module, "AUTO_NIGHT_FORCE", True)
        browser = type("B", (), {"devices": {"x": 1}, "stop_discovery": lambda self: None})()
        cast = type("C", (), {"cast_info": type("I", (), {"host": "h"})()})()
        monkeypatch.setattr(module, "discover_chromecasts", lambda name: (cast, browser))
        monkeypatch.setattr(module, "update_discovery_alert", lambda **kw: False)

        def boom(*args, **kwargs):
            raise module.TvPowerError("unreadable")

        monkeypatch.setattr(module, "process_device", boom)
        calls: list[str] = []
        monkeypatch.setattr(module, "send_discovery_alert", lambda msg: calls.append(msg))
        monkeypatch.chdir(tmp_path)
        import pytest

        with pytest.raises(SystemExit) as exc:
            module.main(args=["--name", "Living"])
        assert exc.value.code == 1
        assert len(calls) == expected_calls


def test_legacy_powered_off_state_is_discarded(tmp_path) -> None:
    """旧形式 state（powered_off）を引き継いで、視聴再開後に初回 tick で即 OFF しない"""
    import json

    from nemucast import auto_standby as module

    state_file = tmp_path / "s.json"
    state_file.write_text(
        json.dumps(
            {
                "device_name": "Living",
                "powered_off": True,
                "consecutive_lowered": 3,
                "last_lowered_to": 0.1,
                "updated_at": 0,
            }
        )
    )
    assert module.load_state(state_file, "Living") is None


def test_stale_state_keeps_last_lowered_to_so_manual_rise_is_detected(tmp_path) -> None:
    import json

    from nemucast import auto_standby as module

    state_file = tmp_path / "s.json"
    state_file.write_text(
        json.dumps(
            {
                "device_name": "Living",
                "consecutive_lowered": 2,
                "last_lowered_to": 0.42,
                "original_volume": 0.5,
                "updated_at": 0,
            }
        )
    )
    state = module.load_state(state_file, "Living")
    assert state["consecutive_lowered"] == 0
    assert state["last_lowered_to"] == 0.42
    assert state["original_volume"] == 0.5


def test_huge_integer_volume_does_not_crash_load_state(tmp_path) -> None:
    import json

    from nemucast import auto_standby as module

    state_file = tmp_path / "s.json"
    state_file.write_text(
        '{"device_name": "Living", "original_volume": ' + "1" + "0" * 400 + ', "updated_at": 0}'
    )
    assert module.load_state(state_file, "Living") is None
    del json


@pytest.fixture(autouse=True)
def _no_restore_wait(monkeypatch):
    from nemucast import auto_standby

    monkeypatch.setattr(auto_standby, "AUTO_RESTORE_WAIT_SEC", 0)


def test_restore_volume_waits_until_tv_reports_original(monkeypatch, caplog) -> None:
    """set_volume は応答を待たない。反映（読み戻し）を確認してから次へ進む"""
    from types import SimpleNamespace

    from nemucast import auto_standby

    monkeypatch.setattr(auto_standby, "AUTO_RESTORE_WAIT_SEC", 3)
    monkeypatch.setattr(auto_standby.time, "sleep", lambda s: None)
    reads = iter([0.11, 0.11, 0.23])

    class FakeStatus:
        @property
        def volume_level(self) -> float:
            return next(reads)

    cast = SimpleNamespace(status=FakeStatus(), set_volume=Mock())
    with caplog.at_level("INFO"):
        auto_standby.restore_volume(cast, {"original_volume": 0.23})
    cast.set_volume.assert_called_once_with(0.23)
    assert "読み戻しで確認" in caplog.text


def test_restore_volume_warns_but_continues_when_not_confirmed(caplog) -> None:
    from types import SimpleNamespace

    from nemucast import auto_standby

    cast = SimpleNamespace(status=SimpleNamespace(volume_level=0.11), set_volume=Mock())
    with caplog.at_level("WARNING"):
        auto_standby.restore_volume(cast, {"original_volume": 0.23})
    assert "確認できませんでした" in caplog.text


class TestNotFoundAlert:
    @pytest.fixture
    def notify(self, monkeypatch: pytest.MonkeyPatch) -> Mock:
        from nemucast import auto_standby

        notify = Mock(return_value=True)
        monkeypatch.setattr(auto_standby, "send_discovery_alert", notify)
        return notify

    def test_threshold_alerts_once_and_logs_each_missing_tick(
        self,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from nemucast import auto_standby

        with caplog.at_level(logging.INFO):
            for count in range(1, 7):
                with fake_network(["OtherDevice"]):
                    assert auto_standby.main(["--name", "Dell"]) is None
                saved = json.loads(auto_standby.AUTO_NOT_FOUND_ALERT_FILE.read_text())
                assert saved["missing_ticks"] == count
                assert saved["alerted"] is (count >= 4)
                assert notify.call_count == (1 if count >= 4 else 0)
        assert "4 tick" in notify.call_args.args[0]
        assert caplog.messages.count("tick 結果: not_found device=Dell") == 6
        assert not auto_standby.DISCOVERY_ALERT_FILE.exists()
        assert not auto_standby.AUTO_STATE_FILE.exists()

    def test_recovery_once_then_new_outage_can_alert(
        self, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        from nemucast import auto_standby

        for _ in range(4):
            with fake_network(["OtherDevice"]):
                auto_standby.main(["--name", "Dell"])
        for _ in range(2):
            with fake_network(["Dell"]) as net:
                net["casts"]["Dell"].status.volume_level = 0.5
                auto_standby.main(["--name", "Dell"])
        assert notify.call_count == 2
        assert "再び見える" in notify.call_args.args[0]
        assert not auto_standby.AUTO_NOT_FOUND_ALERT_FILE.exists()
        for _ in range(4):
            with fake_network(["OtherDevice"]):
                auto_standby.main(["--name", "Dell"])
        assert notify.call_count == 3
        assert "4 tick" in notify.call_args.args[0]

    def test_failed_outage_and_recovery_notifications_retry_next_tick(
        self, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        from nemucast import auto_standby

        notify.side_effect = [False, True, False, True]
        for count in range(1, 7):
            with fake_network(["OtherDevice"]):
                auto_standby.main(["--name", "Dell"])
            saved = json.loads(auto_standby.AUTO_NOT_FOUND_ALERT_FILE.read_text())
            assert saved["alerted"] is (count >= 5)
        assert notify.call_count == 2
        for count in range(1, 4):
            with fake_network(["Dell"]) as net:
                net["casts"]["Dell"].status.volume_level = 0.5
                auto_standby.main(["--name", "Dell"])
            assert auto_standby.AUTO_NOT_FOUND_ALERT_FILE.exists() is (count == 1)
        assert notify.call_count == 4

    @pytest.mark.parametrize(
        "content",
        [
            b"\xff\xfe",
            "{",
            "[]",
            "null",
            '{"device_name":"OtherDevice","missing_ticks":4,"alerted":true}',
            '{"device_name":"Dell","missing_ticks":true,"alerted":false}',
            '{"device_name":"Dell","missing_ticks":-1,"alerted":false}',
            '{"device_name":"Dell","missing_ticks":"4","alerted":false}',
            '{"device_name":"Dell","missing_ticks":4,"alerted":"true"}',
        ],
    )
    def test_corrupt_or_other_device_state_starts_over(
        self, content: str | bytes, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        from nemucast import auto_standby

        path = auto_standby.AUTO_NOT_FOUND_ALERT_FILE
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        with fake_network(["OtherDevice"]):
            auto_standby.main(["--name", "Dell"])
        assert json.loads(path.read_text())["missing_ticks"] == 1
        with fake_network(["Dell"]) as net:
            net["casts"]["Dell"].status.volume_level = 0.5
            auto_standby.main(["--name", "Dell"])
        assert not path.exists()
        notify.assert_not_called()

    @pytest.mark.parametrize("alerted", [True, False])
    def test_scheduler_gap_resets_only_unnotified_outage(
        self, alerted: bool, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        from nemucast import auto_standby

        path = auto_standby.AUTO_NOT_FOUND_ALERT_FILE
        path.write_text(
            json.dumps(
                {
                    "device_name": "Dell",
                    "missing_ticks": 3,
                    "alerted": alerted,
                    "updated_at": time.time() - auto_standby.AUTO_STATE_STALE_SEC - 1,
                }
            )
        )
        with fake_network(["OtherDevice"]):
            auto_standby.main(["--name", "Dell"])
        data = json.loads(path.read_text())
        assert data["missing_ticks"] == (4 if alerted else 1)
        assert data["alerted"] is alerted
        notify.assert_not_called()

    def test_disabled_threshold(
        self,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nemucast import auto_standby

        monkeypatch.setattr(auto_standby, "AUTO_NOT_FOUND_ALERT_AFTER_TICKS", 0)
        for _ in range(5):
            with fake_network(["OtherDevice"]):
                auto_standby.main(["--name", "Dell"])
        notify.assert_not_called()
        assert not auto_standby.AUTO_NOT_FOUND_ALERT_FILE.exists()

    def test_lan_zero_counter_is_independent(
        self,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nemucast import auto_standby

        monkeypatch.setattr(auto_standby, "DISCOVERY_ALERT_AFTER_TICKS", 0)
        for _ in range(4):
            with fake_network([]):
                auto_standby.main(["--name", "Dell"])
        notify.assert_called_once()
        assert json.loads(auto_standby.AUTO_NOT_FOUND_ALERT_FILE.read_text())["missing_ticks"] == 4
        assert not auto_standby.DISCOVERY_ALERT_FILE.exists()

    @pytest.mark.parametrize("session", ["none", "fresh", "stale"])
    @pytest.mark.parametrize("found", [True, False])
    def test_outside_neither_counts_nor_recovers(
        self,
        session: str,
        found: bool,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nemucast import auto_standby

        monkeypatch.setattr(auto_standby, "AUTO_NIGHT_FORCE", False)
        monkeypatch.setattr(auto_standby, "in_night_window", Mock(return_value=False))
        lan_update = Mock()
        monkeypatch.setattr(auto_standby, "update_discovery_alert", lan_update)
        alert_file = auto_standby.AUTO_NOT_FOUND_ALERT_FILE
        save_state(alert_file, {"device_name": "Dell", "missing_ticks": 4, "alerted": True})
        before = alert_file.read_bytes()
        if session != "none":
            TestNightMain.seed_session(count=1, stale=session == "stale")
        with fake_network(["Dell"] if found else []) as net:
            if found:
                net["casts"]["Dell"].status.volume_level = 0.42
            auto_standby.main(["--name", "Dell"])
        assert alert_file.read_bytes() == before
        notify.assert_not_called()
        lan_update.assert_not_called()


@pytest.mark.parametrize("elapsed", [900, 1800])
def test_fresh_session_completes_across_window_end(
    elapsed: int, fake_network: Callable[[list[str]], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """終了時刻直前の初回から、窓外の下げ・復元・OFF まで同じセッションを完走する。"""
    from nemucast import auto_standby

    clock = Mock(return_value=10000.0)
    monkeypatch.setattr(auto_standby.time, "time", clock)
    monkeypatch.setattr(auto_standby, "AUTO_NIGHT_FORCE", False)
    monkeypatch.setattr(auto_standby, "AUTO_NIGHT_START", "21:00")
    monkeypatch.setattr(auto_standby, "AUTO_NIGHT_END", "06:00")
    now = Mock()
    monkeypatch.setattr(auto_standby, "datetime", now)
    # in_night_window の時刻解析には本物の datetime を使う
    now.strptime = datetime.strptime
    for index, (hour, volume) in enumerate((("05:45", 0.5), ("06:00", 0.46), ("06:15", 0.42))):
        now.now.return_value = datetime.fromisoformat(f"2026-11-10T{hour}:00")
        clock.return_value = 10000.0 + index * elapsed
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            cast.status.volume_level = volume
            auto_standby.main(["--name", "Dell"])
        if index < 2:
            cast.set_volume.assert_called_once_with(round(volume - 0.04, 2))
            assert (
                load_state(auto_standby.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == index + 1
            )
            auto_standby.power_off.assert_not_called()
        else:
            assert [call.args for call in cast.set_volume.call_args_list] == [(0.38,), (0.5,)]
            auto_standby.power_off.assert_awaited_once()
            assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["original_volume"] is None
        net["browser"].stop_discovery.assert_called_once()


@pytest.mark.parametrize(
    ("updated_at", "expected"),
    [
        (100000.0, True),
        (98200.0, True),
        (98199.0, False),
        (100001.0, False),
        (float("nan"), False),
        (float("inf"), False),
        (float("-inf"), False),
        (None, False),
        ("x", False),
    ],
)
def test_is_session_continuable_boundaries(
    updated_at: Any, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """窓外継続は有限で未来でなく stale 以内の updated_at だけ許す（now=100000, 上限 1800 秒）。"""
    from nemucast import auto_standby

    monkeypatch.setattr(auto_standby.time, "time", lambda: 100000.0)
    assert auto_standby.is_session_continuable({"updated_at": updated_at}) is expected


def test_not_found_alert_io_error_does_not_stop_main(
    fake_network: Callable[[list[str]], Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """通知 state の保存に失敗しても、寝かしつけ本体と探索の解放は続く。"""
    from nemucast import auto_standby

    monkeypatch.setattr(auto_standby, "AUTO_NIGHT_FORCE", True)
    monkeypatch.setattr(auto_standby, "save_state", Mock(side_effect=OSError("denied")))
    with fake_network(["OtherDevice"]) as net, caplog.at_level(logging.WARNING):
        auto_standby.main(["--name", "Dell"])
    assert "不在通知 state を更新できませんでした" in caplog.text
    net["browser"].stop_discovery.assert_called_once()


def test_future_updated_at_session_outside_window_is_cleaned_up_not_continued(
    fake_network: Callable[[list[str]], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """未来時刻の state は新鮮扱いにせず、窓外では続行せず後始末に回す。"""
    from nemucast import auto_standby

    monkeypatch.setattr(auto_standby, "AUTO_NIGHT_FORCE", False)
    monkeypatch.setattr(auto_standby, "AUTO_NIGHT_START", "21:00")
    monkeypatch.setattr(auto_standby, "AUTO_NIGHT_END", "06:00")
    now = Mock()
    now.strptime = datetime.strptime
    now.now.return_value = datetime.fromisoformat("2026-11-10T12:00:00")
    monkeypatch.setattr(auto_standby, "datetime", now)
    state = fresh_state("Dell")
    state.update(original_volume=0.5, last_lowered_to=0.42, consecutive_lowered=1)
    state["updated_at"] = time.time() + 3600
    auto_standby.AUTO_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    auto_standby.AUTO_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    cleanup = Mock()
    monkeypatch.setattr(auto_standby, "cleanup_session", cleanup)
    with fake_network(["Dell"]):
        auto_standby.main(["--name", "Dell"])
    cleanup.assert_called_once()
    auto_standby.power_off.assert_not_awaited()


def test_dell_app_lowers_despite_idle_cec_and_logs_raw_values(
    fake_network: Callable[[list[str]], Any], caplog: pytest.LogCaptureFixture
) -> None:
    """実機と同じ CEC 値・UNKNOWN 再生状態でも、起動中アプリを根拠に下げる。"""
    from nemucast import auto_standby

    with fake_network(["Dell"]) as net, caplog.at_level(logging.INFO):
        cast = net["casts"]["Dell"]
        cast.status.volume_level = 0.44
        auto_standby.main(["--name", "Dell"])
    cast.set_volume.assert_called_once_with(0.40)
    auto_standby.load_identity.assert_called_once_with("Dell")
    auto_standby.power_off.assert_not_awaited()
    assert (
        caplog.messages.count(
            "対象機器の生値: device=Dell is_on=True app_id='AndroidNativeApp' "
            "display_name='U-NEXT for TV' "
            "is_idle=True is_stand_by=True is_active_input=False volume=0.44 volume_source=cast"
        )
        == 1
    )


def test_missing_cast_status_is_not_watching(
    fake_network: Callable[[list[str]], Any], caplog: pytest.LogCaptureFixture
) -> None:
    from nemucast import auto_standby

    auto_standby.read_power_state.return_value = False
    with fake_network(["Dell"]) as net, caplog.at_level(logging.INFO):
        cast = net["casts"]["Dell"]
        cast.status = None
        auto_standby.main(["--name", "Dell"])
    cast.set_volume.assert_not_called()
    auto_standby.power_off.assert_not_awaited()
    assert "tick 結果: not_watching device=Dell" in caplog.messages
    assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["original_volume"] is None


@pytest.mark.parametrize("missing_status", [False, True])
def test_not_watching_keeps_restore_state_when_volume_unavailable(
    missing_status: bool,
    fake_network: Callable[[list[str]], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """元音量があっても現在音量を読めない時は、復元情報を消さず再試行に回す。"""
    from nemucast import auto_standby

    auto_standby.read_power_state.return_value = False
    TestNightMain.seed_session()
    before = auto_standby.AUTO_STATE_FILE.read_bytes()
    monkeypatch.setattr(auto_standby, "send_discovery_alert", Mock(return_value=True))
    with fake_network(["Dell"]) as net:
        cast = net["casts"]["Dell"]
        cast.status.app_id = None
        cast.status.volume_level = None
        if missing_status:
            cast.status = None
        auto_standby.main(["--name", "Dell"])
    cast.set_volume.assert_not_called()
    auto_standby.power_off.assert_not_awaited()
    assert auto_standby.AUTO_STATE_FILE.read_bytes() == before


@pytest.mark.parametrize("app_id", [None, "", IDLE_APP_ID])
@pytest.mark.parametrize("volume", [0.46, 0.60])
def test_not_watching_restores_session_unless_manually_raised(
    app_id: str | None,
    volume: float,
    fake_network: Callable[[list[str]], Any],
) -> None:
    """再生終了後は元音量に戻して初期化し、手動上昇時は現在の音量を保つ。"""
    from nemucast import auto_standby

    auto_standby.read_power_state.return_value = False
    state = fresh_state("Dell")
    state.update(original_volume=0.50, last_lowered_to=0.46, consecutive_lowered=1)
    save_state(auto_standby.AUTO_STATE_FILE, state)
    with fake_network(["Dell"]) as net:
        cast = net["casts"]["Dell"]
        cast.status.app_id = app_id
        cast.status.volume_level = volume
        auto_standby.main(["--name", "Dell"])
    if volume == 0.46:
        cast.set_volume.assert_called_once_with(0.50)
    else:
        cast.set_volume.assert_not_called()
    assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["original_volume"] is None
    cast.quit_app.assert_not_called()
    auto_standby.power_off.assert_not_awaited()


@pytest.mark.parametrize("cleanup", [False, True])
@pytest.mark.parametrize("is_on", [False, True])
def test_paired_identity_checked_before_status_and_volume(
    cleanup: bool,
    is_on: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAC 照合は生値・視聴判定・通常/cleanup の音量操作より先に 1 回だけ行う。"""
    from nemucast import auto_standby

    cast = make_cast(0.46)
    state = fresh_state("Dell")
    state.update(original_volume=0.50, last_lowered_to=0.46, consecutive_lowered=1)
    calls = Mock()
    auto_standby.read_power_state.return_value = is_on
    for name, mock in (
        ("wait", cast.wait),
        ("identity", auto_standby.load_identity),
        ("read", auto_standby.read_power_state),
        ("volume", cast.set_volume),
    ):
        calls.attach_mock(mock, name)
    for name in ("log_device_status", "is_watching"):
        mock = Mock(wraps=getattr(auto_standby, name))
        monkeypatch.setattr(auto_standby, name, mock)
        calls.attach_mock(mock, name)
    auto_standby.process_device(cast, "Dell", state, cleanup=cleanup)
    assert [call[0] for call in calls.mock_calls] == [
        "wait",
        "identity",
        "read",
        "log_device_status",
        "is_watching",
        "volume",
    ]
    auto_standby.read_power_state.assert_awaited_once_with(cast.cast_info.host, "aa:bb")
    cast.set_volume.assert_called_once_with(0.50 if cleanup else 0.42)


@pytest.mark.parametrize("cleanup", [False, True])
@pytest.mark.parametrize("reason", ["MAC mismatch", "connection unavailable"])
def test_identity_failure_preserves_session_without_status_or_volume(
    cleanup: bool,
    reason: str,
    fake_network: Callable[[list[str]], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """照合エラーは既存ハンドラに渡し、復元情報を残して全機器操作を止める。"""
    from nemucast import auto_standby
    from nemucast.tv_power import TvPowerError

    TestNightMain.seed_session(stale=cleanup)
    before = auto_standby.AUTO_STATE_FILE.read_bytes()
    monkeypatch.setattr(auto_standby, "AUTO_NIGHT_FORCE", not cleanup)
    monkeypatch.setattr(auto_standby, "in_night_window", Mock(return_value=not cleanup))
    status_log = Mock()
    watching = Mock()
    monkeypatch.setattr(auto_standby, "log_device_status", status_log)
    monkeypatch.setattr(auto_standby, "is_watching", watching)
    monkeypatch.setattr(auto_standby, "send_discovery_alert", Mock(return_value=True))
    auto_standby.read_power_state.side_effect = TvPowerError(reason)
    with fake_network(["Dell"]) as net:
        cast = net["casts"]["Dell"]
        cast.status.volume_level = 0.42
        if cleanup:
            auto_standby.main(["--name", "Dell"])
        else:
            with pytest.raises(SystemExit) as exc:
                auto_standby.main(["--name", "Dell"])
            assert exc.value.code == 1
    auto_standby.read_power_state.assert_awaited_once_with(cast.cast_info.host, "aa:bb")
    status_log.assert_not_called()
    watching.assert_not_called()
    cast.set_volume.assert_not_called()
    cast.quit_app.assert_not_called()
    auto_standby.power_off.assert_not_awaited()
    assert auto_standby.AUTO_STATE_FILE.read_bytes() == before
    net["browser"].stop_discovery.assert_called_once()


@pytest.mark.parametrize("cleanup", [False, True])
def test_unpaired_device_skips_identity_read(cleanup: bool) -> None:
    from nemucast import auto_standby

    auto_standby.load_identity.return_value = None
    cast = make_cast(0.46)
    state = fresh_state("Dell")
    state.update(original_volume=0.50, last_lowered_to=0.46, consecutive_lowered=1)
    auto_standby.process_device(cast, "Dell", state, cleanup=cleanup)
    auto_standby.read_power_state.assert_not_awaited()
    cast.set_volume.assert_called_once_with(0.50 if cleanup else 0.42)


def test_device_status_log_escapes_controls_and_limits_length(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """機器由来の改行・制御文字・長い値を 64 文字以内の 1 行に収める。"""
    from nemucast import auto_standby

    cast = make_cast(0.46)
    cast.status.app_id = "app\n\r\t\x00\x1b\u2028" + "a" * 100
    cast.status.display_name = "title\n\r\t\x00\x1b\u2029" + "b" * 100
    with caplog.at_level(logging.INFO):
        auto_standby.log_device_status(cast, "Dell")
    message = caplog.messages[0]
    assert len(message.splitlines()) == 1
    for field, value in (
        ("app_id", cast.status.app_id),
        ("display_name", cast.status.display_name),
    ):
        logged_value = message.split(f"{field}=", 1)[1].split(" ", 1)[0]
        assert logged_value == repr(value)[:64]
        assert len(logged_value) == 64
        assert "\\n\\r\\t\\x00\\x1b\\u202" in logged_value
    assert len(caplog.records) == 1


@pytest.mark.parametrize(
    "paired,is_on,app_id,watching",
    [
        (True, True, None, True),
        (True, False, "AndroidNativeApp", True),
        (True, False, None, False),
        (False, None, "AndroidNativeApp", True),
        (False, None, None, False),
    ],
)
def test_process_device_power_or_app_watching(
    paired: bool,
    is_on: bool | None,
    app_id: str | None,
    watching: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """点灯 OR アプリで制御し、照合時の電源状態を追加接続なしで生値に記録する。"""
    from nemucast import auto_standby

    auto_standby.load_identity.return_value = "aa:bb" if paired else None
    auto_standby.read_power_state.return_value = is_on
    cast = make_cast(0.44)
    cast.status.app_id = app_id
    with caplog.at_level(logging.INFO):
        auto_standby.process_device(cast, "Dell", fresh_state("Dell"), cleanup=False)
    if watching:
        cast.set_volume.assert_called_once_with(0.40)
    else:
        cast.set_volume.assert_not_called()
    if paired:
        auto_standby.read_power_state.assert_awaited_once_with(cast.cast_info.host, "aa:bb")
    else:
        auto_standby.read_power_state.assert_not_awaited()
    raw = [msg for msg in caplog.messages if msg.startswith("対象機器の生値:")]
    assert len(raw) == 1
    assert f"is_on={is_on}" in raw[0]
    outcome = "lowered count=1" if watching else "not_watching device=Dell"
    assert f"tick 結果: {outcome}" in caplog.messages


@pytest.mark.parametrize(
    "result,before,after,count,original,cleanup,paired,restore",
    [
        ("lowered", 0.44, 0.40, 0, None, False, True, True),
        ("lowered", 0.05, 0.05, 0, None, False, True, True),
        ("lowered", 0.02, 0.02, 0, None, False, True, True),
        ("not_watching", 0.44, 0.44, 0, None, False, True, True),
        ("not_watching", None, None, 0, None, False, True, True),
        ("not_watching", 0.44, 0.50, 1, 0.50, False, True, True),
        ("not_watching", 0.60, 0.60, 1, 0.50, False, True, True),
        ("cleanup", 0.44, 0.50, 1, 0.50, True, True, True),
        ("cleanup", 0.60, 0.60, 1, 0.50, True, True, True),
        ("cleanup", 0.44, 0.44, 0, None, True, True, True),
        ("turned_off", 0.44, 0.50, 2, 0.50, False, True, True),
        ("already_off", 0.44, 0.50, 2, 0.50, False, True, True),
        ("turned_off", 0.44, 0.40, 2, 0.50, False, True, False),
        ("turned_off", 0.50, 0.50, 3, 0.50, False, True, False),
        ("power_off_unavailable", 0.44, 0.50, 2, 0.50, False, False, True),
        ("power_off_unavailable", 0.44, 0.40, 2, 0.50, False, False, False),
    ],
)
def test_process_device_tick_summary_each_exit(
    result: str,
    before: float | None,
    after: float | None,
    count: int,
    original: float | None,
    cleanup: bool,
    paired: bool,
    restore: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """全正常終了経路で開始音量と最終目標音量を含む要約が正確に 1 行出る。"""
    from nemucast import auto_standby

    monkeypatch.setattr(auto_standby, "AUTO_RESTORE_WAIT_SEC", 0)
    monkeypatch.setattr(auto_standby, "AUTO_RESTORE_VOLUME", restore)
    monkeypatch.setattr(auto_standby, "send_discovery_alert", Mock(return_value=True))
    auto_standby.load_identity.return_value = "aa:bb" if paired else None
    auto_standby.read_power_state.return_value = result not in ("not_watching", "already_off")
    auto_standby.power_off.return_value = result
    cast = make_cast(before)
    cast.status.app_id = None if result == "not_watching" else "AndroidNativeApp"
    state = fresh_state("Dell")
    state.update(consecutive_lowered=count, original_volume=original, last_lowered_to=0.44)
    with caplog.at_level(logging.INFO):
        auto_standby.process_device(cast, "Dell", state, cleanup=cleanup)
    is_on = auto_standby.read_power_state.return_value if paired else None
    expected_count = 0 if result in ("not_watching", "cleanup") else min(count + 1, 3)
    expected_before = "None" if before is None else f"{before:.2f}"
    expected_after = "None" if after is None else f"{after:.2f}"
    summaries = [msg for msg in caplog.messages if "tick 要約:" in msg]
    assert summaries == [
        f"tick 要約: device=Dell is_on={is_on} app_id={cast.status.app_id!r} "
        f"volume_before={expected_before} volume_after={expected_after} "
        f"count={expected_count} result={result} "
        f"volume_source={'none' if before is None else 'cast'}"
    ]
    assert len([msg for msg in caplog.messages if msg.startswith("tick 結果:")]) == 1
    if after != before:
        assert cast.set_volume.call_args.args == (after,)
    else:
        cast.set_volume.assert_not_called()


def test_tick_summary_escapes_device_and_app_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """長い機器名・アプリ ID の改行と制御文字を escape し 1 tick を 1 行に保つ。"""
    from nemucast import auto_standby

    name = "Dell\n\r\t\x00\x1b\u2028" + "d" * 100
    app_id = "app\n\r\t\x00\x1b\u2029" + "a" * 100
    cast = make_cast(0.44)
    cast.status.app_id = app_id
    with caplog.at_level(logging.INFO):
        auto_standby.process_device(cast, name, fresh_state(name), cleanup=False)
    summaries = [msg for msg in caplog.messages if "tick 要約:" in msg]
    assert len(summaries) == 1
    assert len(summaries[0].splitlines()) == 1
    assert f"device={repr(name)[1:-1][:64]} is_on=True" in summaries[0]
    assert f"app_id={repr(app_id)[:64]} volume_before=0.44" in summaries[0]


@pytest.mark.parametrize("target", [None, "OtherCast"])
def test_auto_target_config_and_parser_are_independent_of_chromecast_name(
    target: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config の環境変数から実際の parser への接続を検証する。"""
    from nemucast import auto_standby, config

    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("CHROMECAST_NAME", "リビングルーム")
    monkeypatch.delenv("AUTO_TARGET_NAME", raising=False)
    if target is not None:
        monkeypatch.setenv("AUTO_TARGET_NAME", target)
    values = runpy.run_path(config.__file__)
    monkeypatch.setattr(config, "CHROMECAST_NAME", values["CHROMECAST_NAME"])
    monkeypatch.setattr(config, "AUTO_TARGET_NAME", values["AUTO_TARGET_NAME"])
    module = runpy.run_path(auto_standby.__file__)
    assert values["AUTO_TARGET_NAME"] == (target or "Dell")
    assert module["parse_args"]([]).name == (target or "Dell")
    assert module["parse_args"](["--name", "Explicit"]).name == "Explicit"


def test_default_auto_only_touches_dell(
    monkeypatch: pytest.MonkeyPatch, fake_network: Callable[[list[str]], Any]
) -> None:
    from nemucast import auto_standby, config

    monkeypatch.setattr(config, "CHROMECAST_NAME", "リビングルーム")
    monkeypatch.setattr(auto_standby, "AUTO_TARGET_NAME", "Dell")
    with fake_network(["リビングルーム", "Dell"]) as net:
        dell = net["casts"]["Dell"]
        living = net["casts"]["リビングルーム"]
        dell.status.volume_level = 0.44
        auto_standby.main([])
    dell.set_volume.assert_called_once_with(0.40)
    living.wait.assert_not_called()
    living.set_volume.assert_not_called()
    living.quit_app.assert_not_called()
    assert net["get_listed"].call_args.kwargs["friendly_names"] == ["Dell"]


def test_power_alert_file_uses_configured_log_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    from nemucast import config

    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("LOG_DIR", "custom-logs")
    monkeypatch.delenv("AUTO_POWER_OFF_ALERT_FILE", raising=False)
    assert runpy.run_path(config.__file__)["AUTO_POWER_OFF_ALERT_FILE"] == Path(
        "custom-logs/auto_power_off_alert_state.json"
    )
    monkeypatch.setenv("AUTO_POWER_OFF_ALERT_FILE", "custom-alert.json")
    assert runpy.run_path(config.__file__)["AUTO_POWER_OFF_ALERT_FILE"] == Path("custom-alert.json")


class TestUnpairedPowerOff:
    @pytest.fixture
    def notify(self, monkeypatch: pytest.MonkeyPatch) -> Mock:
        from nemucast import auto_standby

        auto_standby.load_identity.return_value = None
        notify = Mock(return_value=True)
        monkeypatch.setattr(auto_standby, "send_discovery_alert", notify)
        return notify

    @staticmethod
    def complete_session(
        fake_network: Callable[[list[str]], Any], error: Exception | None = None
    ) -> Mock:
        from nemucast import auto_standby

        TestNightMain.seed_session()
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            cast.status.volume_level = 0.42
            cast.quit_app.side_effect = error
            auto_standby.main(["--name", "Dell"])
        return cast

    def test_restores_then_quits_alerts_and_resets(
        self,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from nemucast import auto_standby

        with caplog.at_level(logging.INFO):
            cast = self.complete_session(fake_network)
        actions = [
            (call[0], call.args)
            for call in cast.mock_calls
            if call[0] in ("set_volume", "quit_app")
        ]
        assert actions == [("set_volume", (0.38,)), ("set_volume", (0.5,)), ("quit_app", ())]
        assert auto_standby.load_identity.call_count == 2
        auto_standby.read_power_state.assert_not_awaited()
        auto_standby.power_off.assert_not_awaited()
        notify.assert_called_once()
        assert "power_off_unavailable device=Dell reason=not_paired" in notify.call_args.args[0]
        assert "nemucast-tv-pair --name Dell" in notify.call_args.args[0]
        assert "tick 結果: power_off_unavailable device=Dell" in caplog.messages
        state = load_state(auto_standby.AUTO_STATE_FILE, "Dell")
        assert state["original_volume"] is None
        assert state["consecutive_lowered"] == 0
        saved = json.loads(auto_standby.AUTO_POWER_OFF_ALERT_FILE.read_text())
        assert saved["alerted_on"] == datetime.now().date().isoformat()

    def test_same_day_suppressed_next_day_notified(
        self,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nemucast import auto_standby

        clock = Mock()
        clock.now.return_value = datetime(2026, 11, 10, 23, 0)
        monkeypatch.setattr(auto_standby, "datetime", clock)
        for _ in range(2):
            cast = self.complete_session(fake_network)
            cast.quit_app.assert_called_once()
        notify.assert_called_once()
        clock.now.return_value = datetime(2026, 11, 11, 0, 0)
        self.complete_session(fake_network)
        assert notify.call_count == 2
        assert (
            json.loads(auto_standby.AUTO_POWER_OFF_ALERT_FILE.read_text())["alerted_on"]
            == "2026-11-11"
        )

    def test_failed_notification_not_recorded_and_retried(
        self, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        from nemucast import auto_standby

        notify.side_effect = [False, True]
        self.complete_session(fake_network)
        pending = json.loads(auto_standby.AUTO_POWER_OFF_ALERT_FILE.read_text())
        assert "alerted_on" not in pending
        assert pending["pending"] is True
        assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 0
        self.complete_session(fake_network)
        self.complete_session(fake_network)
        assert notify.call_count == 2
        assert auto_standby.AUTO_POWER_OFF_ALERT_FILE.exists()

    @pytest.mark.parametrize("outside", [False, True])
    def test_pairing_cancels_pending_notification(
        self,
        outside: bool,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """通知失敗後のペアリングで再送待ちを解除し、窓内・窓外とも誤通知しない。"""
        from nemucast import auto_standby

        notify.return_value = False
        self.complete_session(fake_network)
        assert json.loads(auto_standby.AUTO_POWER_OFF_ALERT_FILE.read_text())["pending"] is True
        notify.reset_mock()
        auto_standby.load_identity.return_value = "aa:bb"
        monkeypatch.setattr(auto_standby, "AUTO_NIGHT_FORCE", not outside)
        monkeypatch.setattr(auto_standby, "in_night_window", Mock(return_value=not outside))
        with fake_network(["Dell"]) as net:
            net["casts"]["Dell"].status.app_id = None
            net["casts"]["Dell"].status.volume_level = 0.44
            auto_standby.main(["--name", "Dell"])
        notify.assert_not_called()
        saved = json.loads(auto_standby.AUTO_POWER_OFF_ALERT_FILE.read_text())
        assert saved["pending"] is False
        assert "alerted_on" not in saved

    @pytest.mark.parametrize("outside", [False, True])
    def test_failed_notification_retries_next_tick_without_new_session(
        self,
        outside: bool,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """アプリ終了後や窓外でも、次 tick は送信成功日を残して再送を終える。"""
        from nemucast import auto_standby

        notify.side_effect = [False, True]
        self.complete_session(fake_network)
        if outside:
            monkeypatch.setattr(auto_standby, "AUTO_NIGHT_FORCE", False)
            monkeypatch.setattr(auto_standby, "in_night_window", Mock(return_value=False))
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            cast.status.app_id = None
            cast.status.volume_level = 0.44
            auto_standby.main(["--name", "Dell"])
        cast.set_volume.assert_not_called()
        assert notify.call_count == 2
        saved = json.loads(auto_standby.AUTO_POWER_OFF_ALERT_FILE.read_text())
        assert saved["alerted_on"] == datetime.now().date().isoformat()
        assert saved["pending"] is False

    @pytest.mark.parametrize(
        "error",
        [PyChromecastError("quit failed"), OSError("quit failed"), RuntimeError("quit failed")],
    )
    def test_quit_failure_still_warns_notifies_and_resets(
        self,
        error: Exception,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from nemucast import auto_standby

        with caplog.at_level(logging.WARNING):
            self.complete_session(fake_network, error)
        assert "アプリ終了に失敗: device=Dell" in caplog.text
        assert "power_off_unavailable device=Dell reason=not_paired" in caplog.text
        notify.assert_called_once()
        assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["original_volume"] is None

    @pytest.mark.parametrize(
        "content",
        [
            b"\xff\xfe",
            "{",
            "[]",
            "null",
            "{}",
            '{"device_name":"Dell","alerted_on":true}',
            '{"device_name":"Dell","alerted_on":"2026-11-99"}',
            '{"device_name":"Dell","alerted_on":"20261110"}',
            '{"device_name":"Other","alerted_on":"2026-11-10"}',
        ],
    )
    def test_corrupted_alert_state_is_rebuilt(
        self, content: str | bytes, notify: Mock, fake_network: Callable[[list[str]], Any]
    ) -> None:
        from nemucast import auto_standby

        path = auto_standby.AUTO_POWER_OFF_ALERT_FILE
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        self.complete_session(fake_network)
        notify.assert_called_once()
        assert json.loads(path.read_text())["alerted_on"] == datetime.now().date().isoformat()

    def test_alert_state_write_failure_does_not_block_reset(
        self,
        notify: Mock,
        fake_network: Callable[[list[str]], Any],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from nemucast import auto_standby

        # ディレクトリを書き込み先にして I/O エラーを発生させる（実通知はモック）。
        monkeypatch.setattr(auto_standby, "AUTO_POWER_OFF_ALERT_FILE", Path.cwd())
        with caplog.at_level(logging.WARNING):
            self.complete_session(fake_network)
        notify.assert_called_once()
        assert "電源 OFF 不可通知 state を更新できませんでした" in caplog.text
        assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 0


@pytest.fixture
def atv_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:
    return make_atv_rig(tmp_path, monkeypatch)


def pending_atv(level: int = 13, left: int = 10, maximum: int = 30) -> None:
    """期限のない保留を作り、復元以外の state と分離する。"""
    from nemucast import auto_standby as auto

    auto.save_state(
        auto.AUTO_PENDING_RESTORE_FILE,
        {
            "device_name": "Dell",
            "level": level,
            "left_at": left,
            "max": maximum,
            "created_at": 1,
        },
        touch=False,
    )


def test_standby_has_no_volume_keys_and_saves_pending_before_power(atv_rig: Mock) -> None:
    from nemucast import auto_standby as auto
    from tests.test_atv_volume import seed_atv

    seed_atv(count=2, original=13, last=11)
    atv_rig.level = 11

    async def off(_host: str, _mac: str) -> str:
        assert auto.load_pending_restore("Dell") == {
            "device_name": "Dell",
            "level": 13,
            "left_at": 11,
            "max": 30,
            "created_at": auto.load_pending_restore("Dell")["created_at"],
        }
        assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 3
        assert not [e for e in atv_rig.events if e[0].startswith("VOLUME_")]
        return "turned_off"

    auto.power_off.side_effect = off
    atv_rig.tick()
    auto.power_off.assert_awaited_once()
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] is None


@pytest.mark.parametrize("level", [10, 9, 11])
def test_pending_restore_only_at_untouched_level(atv_rig: Mock, level: int, caplog) -> None:
    from nemucast import auto_standby as auto

    pending_atv()
    atv_rig.level = level
    with caplog.at_level(logging.INFO):
        atv_rig.tick()
    assert auto.load_pending_restore("Dell") is None
    if level == 10:
        assert atv_rig.level == 13
        assert not [e for e in atv_rig.events if e[0] == "VOLUME_DOWN"]
        state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
        assert state["consecutive_lowered"] == 0
        assert state["original_volume"] is None
        assert "result=restored" in caplog.text
        atv_rig.events.clear()
        atv_rig.tick()
        assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] == 13
        assert atv_rig.level == 12
    else:
        assert "reason=manual_volume" in caplog.text
        assert not [e for e in atv_rig.events if e[0] == "VOLUME_UP"]
        assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] == level
    auto.power_off.assert_not_awaited()


@pytest.mark.parametrize("keys_before_off", [1, 2])
@pytest.mark.parametrize("is_on,off_level", [(False, 0), (False, 1), (None, 1)])
def test_pending_restore_power_loss_preserves_original_left_at(
    atv_rig: Mock, keys_before_off: int, is_on: bool | None, off_level: int, caplog
) -> None:
    from nemucast import auto_standby as auto
    from nemucast import tv_volume

    pending_atv()
    pending_before = auto.AUTO_PENDING_RESTORE_FILE.read_bytes()
    atv_rig.level = 10
    original_factory = tv_volume.AndroidTVRemote.side_effect

    def factory(*args: Any) -> Mock:
        remote = original_factory(*args)
        if len(atv_rig.remotes) > keys_before_off:
            connect = remote.async_connect.side_effect

            async def connect_off() -> None:
                await connect()
                remote.is_on = is_on
                remote.volume_info = {"level": off_level, "max": atv_rig.maximum}

            remote.async_connect.side_effect = connect_off
        return remote

    tv_volume.AndroidTVRemote.side_effect = factory
    with caplog.at_level(logging.INFO):
        atv_rig.tick()
    assert auto.AUTO_PENDING_RESTORE_FILE.read_bytes() == pending_before
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert state["last_lowered_to"] is None
    assert state["original_volume"] is None
    assert state["consecutive_lowered"] == 0
    keys = [event[0] for event in atv_rig.events if event[0].startswith("VOLUME_")]
    assert keys == ["VOLUME_UP"] * keys_before_off
    assert len(atv_rig.remotes) == keys_before_off + 1
    auto.read_power_state.assert_awaited_once()
    auto.power_off.assert_not_awaited()
    assert "音量復元を読み戻しで確認できませんでした" in caplog.text
    assert "result=restore_pending" in caplog.text


@pytest.mark.parametrize("is_on", [False, None])
def test_pending_restore_power_loss_before_next_key_preserves_left_at(
    atv_rig: Mock, monkeypatch: pytest.MonkeyPatch, is_on: bool | None, caplog
) -> None:
    from nemucast import auto_standby as auto
    from nemucast.tv_volume import AtvVolume, AtvVolumeControl

    pending_atv()
    pending_before = auto.AUTO_PENDING_RESTORE_FILE.read_bytes()
    atv_rig.level = 10
    original_read = AtvVolumeControl.read

    async def read_then_off(control: AtvVolumeControl) -> AtvVolume:
        volume = await original_read(control)
        if len(atv_rig.remotes) == 2:
            control.remote.is_on = is_on
        return volume

    monkeypatch.setattr(AtvVolumeControl, "read", read_then_off)
    with caplog.at_level(logging.INFO):
        atv_rig.tick()
    assert auto.AUTO_PENDING_RESTORE_FILE.read_bytes() == pending_before
    assert atv_rig.level == 11
    assert [event[0] for event in atv_rig.events] == ["READ", "VOLUME_UP", "READ"]
    assert "result=restore_pending" in caplog.text
    assert "音量復元を読み戻しで確認できませんでした" in caplog.text
    auto.power_off.assert_not_awaited()


@pytest.mark.parametrize("cast_volume", [0.2, 0.5])
@pytest.mark.parametrize("stuck", [False, True])
def test_pending_restore_precedes_positive_cast_volume(
    atv_rig: Mock, cast_volume: float, stuck: bool, caplog
) -> None:
    from nemucast import auto_standby as auto

    pending_atv()
    atv_rig.level = 10
    atv_rig.cast.status.volume_level = cast_volume
    atv_rig.stuck = stuck
    with caplog.at_level(logging.INFO):
        atv_rig.tick()
    assert atv_rig.level == (10 if stuck else 13)
    atv_rig.cast.set_volume.assert_not_called()
    auto.power_off.assert_not_awaited()
    assert not [event for event in atv_rig.events if event[0] == "VOLUME_DOWN"]
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert state["consecutive_lowered"] == 0
    assert state["original_volume"] is None
    assert (auto.load_pending_restore("Dell") is not None) == stuck
    assert f"result={'restore_pending' if stuck else 'restored'}" in caplog.text


@pytest.mark.parametrize("cleanup,is_on", [(False, False), (True, False), (True, True)])
def test_cleanup_and_off_never_send_keys_or_trust_off_volume(
    atv_rig: Mock, cleanup: bool, is_on: bool, caplog
) -> None:
    from nemucast import auto_standby as auto
    from tests.test_atv_volume import seed_atv

    seed_atv(original=13, last=10)
    atv_rig.level = 10 if is_on else 0
    atv_rig.cast.status.app_id = "stale-app"  # OFF 中でもアプリ情報が残り得る
    auto.read_power_state.return_value = is_on
    with caplog.at_level(logging.INFO):
        atv_rig.tick(cleanup=cleanup)
    assert not [e for e in atv_rig.events if e[0].startswith("VOLUME_")]
    assert auto.load_pending_restore("Dell")["left_at"] == 10
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] is None
    if not is_on:
        assert not atv_rig.remotes  # 消灯中の level 0 は読まない
    assert "判断: branch=" in caplog.text
    auto.power_off.assert_not_awaited()


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("level", [9, 11])
def test_manual_volume_in_both_directions_restarts_even_after_failed_off(
    atv_rig: Mock, count: int, level: int, caplog
) -> None:
    from nemucast import auto_standby as auto
    from tests.test_atv_volume import seed_atv

    seed_atv(count=count, original=13, last=10)
    if count == 3:
        pending_atv()
    atv_rig.level = level
    with caplog.at_level(logging.INFO):
        atv_rig.tick()
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert state["consecutive_lowered"] == 1
    assert state["original_volume"] == level
    assert state["last_lowered_to"] == level - 1
    assert auto.load_pending_restore("Dell") is None
    assert "判断: branch=restart_manual" in caplog.text
    assert "last_lowered_to=10 original=13" in caplog.text
    assert "since_last_update=" in caplog.text
    auto.power_off.assert_not_awaited()


def test_unchanged_volume_after_failed_off_retries_without_any_keys(atv_rig: Mock, caplog) -> None:
    from nemucast import auto_standby as auto
    from tests.test_atv_volume import seed_atv

    seed_atv(count=3, original=13, last=10)
    atv_rig.level = 10
    auto.power_off.side_effect = auto.TvPowerError("failed")
    with pytest.raises(auto.TvPowerError):
        atv_rig.tick()
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 3
    auto.power_off.side_effect = None
    with caplog.at_level(logging.INFO):
        atv_rig.tick()
    assert auto.power_off.await_count == 2
    assert "判断: branch=pending_retry" in caplog.text
    assert not [e for e in atv_rig.events if e[0].startswith("VOLUME_")]
    assert auto.load_pending_restore("Dell")["level"] == 13


def test_woken_resets_session_and_keeps_pending_without_notification(atv_rig: Mock, caplog) -> None:
    from nemucast import auto_standby as auto
    from tests.test_atv_volume import seed_atv

    seed_atv(count=3, last=10)
    atv_rig.level = 10
    auto.power_off.return_value = "turned_on_again"
    auto.power_off.side_effect = None
    with caplog.at_level(logging.INFO):
        atv_rig.tick()
    assert "result=woken" in caplog.text
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 0
    assert auto.load_pending_restore("Dell")["level"] == 13
    auto.send_discovery_alert.assert_not_called()


@pytest.mark.parametrize("is_on,level", [(False, 0), (True, 10), (True, 11)])
def test_outside_window_only_restores_pending(
    atv_rig: Mock, monkeypatch, is_on: bool, level: int, caplog
) -> None:
    from nemucast import auto_standby as auto

    pending_atv()
    monkeypatch.setattr(auto, "AUTO_NIGHT_FORCE", False)
    monkeypatch.setattr(auto, "in_night_window", Mock(return_value=False))
    monkeypatch.setattr(auto, "discover_chromecasts", Mock(return_value=(atv_rig.cast, Mock())))
    monkeypatch.setattr(auto, "stop_discovery", Mock())
    auto.read_power_state.return_value = is_on
    atv_rig.level = level
    with caplog.at_level(logging.INFO):
        auto.main([])
    assert not [e for e in atv_rig.events if e[0] == "VOLUME_DOWN"]
    auto.power_off.assert_not_awaited()
    if not is_on:
        assert auto.load_pending_restore("Dell") is not None
        assert not atv_rig.remotes
    elif level == 10:
        assert atv_rig.level == 13
        assert "result=restored" in caplog.text
    else:
        assert atv_rig.level == 11
        assert auto.load_pending_restore("Dell") is None
    auto.stop_discovery.assert_called_once()


@pytest.mark.parametrize("failure", ["stuck", "readback"])
def test_pending_restore_failure_keeps_pending_and_never_lowers(
    atv_rig: Mock, failure: str, monkeypatch, caplog
) -> None:
    from nemucast import auto_standby as auto
    from nemucast import tv_volume

    pending_atv()
    atv_rig.level = 10
    if failure == "stuck":
        atv_rig.stuck = True
    else:
        original = tv_volume.AndroidTVRemote.side_effect

        def factory(*args):
            if atv_rig.remotes:
                atv_rig.available = False
            return original(*args)

        monkeypatch.setattr(tv_volume.AndroidTVRemote, "side_effect", factory)
    atv_rig.tick()
    assert auto.load_pending_restore("Dell") is not None
    assert "音量復元を読み戻しで確認できませんでした" in caplog.text
    assert not [e for e in atv_rig.events if e[0] == "VOLUME_DOWN"]
    auto.power_off.assert_not_awaited()


@pytest.mark.parametrize("error_kind", ["cast", "power"])
def test_exception_tick_has_summary(monkeypatch, caplog, error_kind: str) -> None:
    from nemucast import auto_standby as auto

    failure = auto.TvPowerError("failed") if error_kind == "power" else PyChromecastError("failed")
    cast, browser = make_cast(0.0), Mock(devices={"d": 1})
    monkeypatch.setattr(auto, "discover_chromecasts", Mock(return_value=(cast, browser)))
    monkeypatch.setattr(auto, "stop_discovery", Mock())
    monkeypatch.setattr(auto, "process_device", Mock(side_effect=failure))
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
        auto.main([])
    assert exc.value.code == 1
    assert len([m for m in caplog.messages if m.startswith("tick 要約:")]) == 1
    assert "result=error" in caplog.text


@pytest.mark.parametrize("active", [False, True])
def test_source_change_only_warns_when_resetting_active_state(active: bool, caplog) -> None:
    from nemucast import auto_standby as auto

    state = auto.fresh_state("Dell")
    if active:
        state.update(original_volume=0.5, consecutive_lowered=1)
    with caplog.at_level(logging.INFO):
        auto.prepare_volume_state(state, "atv", 30)
    records = [r for r in caplog.records if "音量源または max" in r.message]
    assert len(records) == 1
    assert records[0].levelno == (logging.WARNING if active else logging.INFO)


@pytest.mark.parametrize("bad", [b"{}", b"[]", b"\xff", b"{broken"])
def test_corrupt_pending_is_ignored(tmp_path, monkeypatch, bad: bytes) -> None:
    from nemucast import auto_standby as auto

    path = tmp_path / "pending.json"
    monkeypatch.setattr(auto, "AUTO_PENDING_RESTORE_FILE", path)
    path.write_bytes(bad)
    assert auto.load_pending_restore("Dell") is None


def test_pending_max_mismatch_is_discarded_without_restoring(atv_rig: Mock) -> None:
    from nemucast import auto_standby as auto

    pending_atv(maximum=25)
    atv_rig.level = 10
    atv_rig.tick()
    assert auto.load_pending_restore("Dell") is None
    assert not [e for e in atv_rig.events if e[0] == "VOLUME_UP"]


def test_pending_save_failure_never_sends_power(atv_rig: Mock, monkeypatch, tmp_path) -> None:
    from nemucast import auto_standby as auto
    from tests.test_atv_volume import seed_atv

    seed_atv(count=3, last=10)
    atv_rig.level = 10
    monkeypatch.setattr(auto, "AUTO_PENDING_RESTORE_FILE", tmp_path)
    with pytest.raises(OSError):
        atv_rig.tick()
    auto.power_off.assert_not_awaited()
    assert not [e for e in atv_rig.events if e[0].startswith("VOLUME_")]


@pytest.mark.parametrize("power_state", [False, None])
def test_volume_key_guard_stops_if_power_changed_after_initial_read(
    atv_rig: Mock, power_state: bool | None
) -> None:
    from nemucast import auto_standby as auto
    from nemucast import tv_volume

    real_factory = tv_volume.AndroidTVRemote.side_effect

    def factory(*args):
        remote = real_factory(*args)
        remote.is_on = power_state
        return remote

    tv_volume.AndroidTVRemote.side_effect = factory
    atv_rig.tick()
    assert not [e for e in atv_rig.events if e[0].startswith("VOLUME_")]
    auto.power_off.assert_not_awaited()


def test_cleanup_still_preserves_restore_when_off_restore_disabled(
    atv_rig: Mock, monkeypatch
) -> None:
    from nemucast import auto_standby as auto
    from tests.test_atv_volume import seed_atv

    monkeypatch.setattr(auto, "AUTO_RESTORE_VOLUME", False)
    seed_atv(original=13, last=10)
    atv_rig.level = 10
    atv_rig.tick(cleanup=True)
    assert auto.load_pending_restore("Dell")["level"] == 13
    assert not [e for e in atv_rig.events if e[0].startswith("VOLUME_")]


@pytest.mark.parametrize(
    "field,value",
    [
        ("level", True),
        ("left_at", -1),
        ("max", 0),
        ("created_at", float("nan")),
        ("created_at", float("inf")),
        ("created_at", 10**400),
        ("device_name", "Living"),
    ],
)
def test_invalid_pending_never_restores(atv_rig: Mock, field: str, value: Any) -> None:
    from nemucast import auto_standby as auto

    pending_atv()
    data = json.loads(auto.AUTO_PENDING_RESTORE_FILE.read_text())
    data[field] = value
    auto.AUTO_PENDING_RESTORE_FILE.write_text(json.dumps(data))
    assert auto.load_pending_restore("Dell") is None
