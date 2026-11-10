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

from nemucast.auto_standby import (
    TickResult,
    fresh_state,
    in_night_window,
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
    monkeypatch.setattr(auto_standby, "read_power_state", AsyncMock(return_value=True))
    monkeypatch.setattr(auto_standby, "power_off", AsyncMock(return_value="turned_off"))


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
        auto_standby.read_power_state.assert_not_called()
        auto_standby.power_off.assert_not_called()

    def test_force_bypasses_window(self, fake_network, monkeypatch) -> None:
        from nemucast import auto_standby

        window = Mock(side_effect=AssertionError("force bypass"))
        monkeypatch.setattr(auto_standby, "in_night_window", window)
        with fake_network(["Dell"]) as net:
            net["casts"]["Dell"].status.volume_level = 0.5
            auto_standby.main(["--name", "Dell"])
        window.assert_not_called()
        auto_standby.read_power_state.assert_awaited_once()

    @pytest.mark.parametrize("power", [False, "unknown"])
    def test_off_or_unknown_never_changes_volume_or_sends_power(
        self, power, fake_network, monkeypatch
    ) -> None:
        from nemucast import auto_standby
        from nemucast.tv_power import TvPowerError

        reader = auto_standby.read_power_state
        reader.return_value = power
        if power == "unknown":
            reader.side_effect = TvPowerError("unknown")
        monkeypatch.setattr(auto_standby, "send_discovery_alert", Mock())
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            if power == "unknown":
                with pytest.raises(SystemExit) as exc:
                    auto_standby.main(["--name", "Dell"])
                assert exc.value.code == 1
            else:
                auto_standby.main(["--name", "Dell"])
                assert load_state(auto_standby.AUTO_STATE_FILE, "Dell")["original_volume"] is None
        cast.set_volume.assert_not_called()
        cast.wait.assert_not_called()
        cast.quit_app.assert_not_called()
        auto_standby.power_off.assert_not_called()
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
            calls.attach_mock(auto_standby.read_power_state, "read")
            calls.attach_mock(cast.wait, "wait")
            calls.attach_mock(auto_standby.power_off, "off")
            auto_standby.main(["--name", "Dell"])
        assert [call[0] for call in calls.mock_calls] == ["read", "wait", "volume", "volume", "off"]
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

    @pytest.mark.parametrize("power", [True, False, "unknown"])
    def test_cleanup_after_window(self, outside, power, fake_network, monkeypatch) -> None:
        from nemucast import auto_standby
        from nemucast.tv_power import TvPowerError

        self.seed_session(stale=True)
        if power == "unknown":
            auto_standby.read_power_state.side_effect = TvPowerError("unknown")
        else:
            auto_standby.read_power_state.return_value = power
        with fake_network(["Dell"]) as net:
            cast = net["casts"]["Dell"]
            cast.status.volume_level = 0.42
            auto_standby.main(["--name", "Dell"])
        if power is True:
            cast.set_volume.assert_called_once_with(0.5)
        else:
            cast.set_volume.assert_not_called()
        auto_standby.power_off.assert_not_called()
        state = load_state(auto_standby.AUTO_STATE_FILE, "Dell")
        assert state["original_volume"] == (0.5 if power == "unknown" else None)

    def test_cleanup_does_not_overwrite_manual_volume_rise(self, outside, fake_network) -> None:
        """窓の終わり際に手動で上げられた音量を、古い元音量で上書きしない"""
        from nemucast import auto_standby

        self.seed_session(stale=True)
        auto_standby.read_power_state.return_value = True
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
        auto_standby.read_power_state.assert_not_called()

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


def test_unreadable_power_state_alerts_only_when_session_in_progress(tmp_path, monkeypatch) -> None:
    """状態不明の失敗通知は、音量を下げ始めた後だけ（夜通しの通知で埋もれない）"""
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
        auto_standby.read_power_state.assert_not_called()

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
