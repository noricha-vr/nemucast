"""Cast 0 固定の回帰と ATV 音量の安全性。全接続・通知をモックする。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from androidtvremote2 import ConnectionClosed

from nemucast import auto_standby as auto
from nemucast import tv_volume
from nemucast.tv_power import TvPowerError
from nemucast.tv_volume import AtvVolume, AtvVolumeControl


def make_atv_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:
    """接続単位の snapshot と非同期キー反映を持つモック TV。"""
    rig = Mock()
    rig.level, rig.maximum, rig.available, rig.stuck = 13, 30, True, False
    rig.events, rig.remotes = [], []
    rig.paired = True
    rig.send_error, rig.connect_error = None, None
    rig.silent = False
    monkeypatch.setattr(auto, "AtvVolumeControl", AtvVolumeControl)
    monkeypatch.setattr(auto, "AUTO_STATE_FILE", tmp_path / "session.json")
    monkeypatch.setattr(auto, "AUTO_PENDING_RESTORE_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(
        auto, "AUTO_VOLUME_ALERT_FILE", tmp_path / "volume-alert.json", raising=False
    )
    monkeypatch.setattr(auto, "AUTO_POWER_OFF_ALERT_FILE", tmp_path / "power-alert.json")
    monkeypatch.setattr(auto, "AUTO_RESTORE_VOLUME", True)
    monkeypatch.setattr(auto, "STEP", -0.04)
    monkeypatch.setattr(auto, "AUTO_MIN_LEVEL", 0.05)
    monkeypatch.setattr(tv_volume, "AUTO_ATV_VOLUME_WAIT_SEC", 0.02)
    monkeypatch.setattr(tv_volume, "POLL_SEC", 0.001)
    monkeypatch.setattr(auto, "send_discovery_alert", Mock(return_value=True))
    monkeypatch.setattr(
        auto, "load_identity", Mock(side_effect=lambda _name: "aa:bb" if rig.paired else None)
    )
    monkeypatch.setattr(auto, "read_power_state", AsyncMock(return_value=True))
    monkeypatch.setattr(tv_volume, "read_mac", AsyncMock(return_value="aa:bb"))

    async def off(_host: str, _mac: str) -> str:
        rig.events.append(("POWER", rig.level))
        return "turned_off"

    monkeypatch.setattr(auto, "power_off", AsyncMock(side_effect=off))

    def remote_factory(*_args: Any) -> Mock:
        remote = Mock(is_on=True)
        remote.callbacks = []
        remote.connected = False
        rig.remotes.append(remote)

        async def connect() -> None:
            if rig.connect_error:
                raise rig.connect_error
            remote.connected = True
            remote.volume_info = (
                {"level": rig.level, "max": rig.maximum, "muted": False} if rig.available else None
            )
            rig.events.append(("READ", rig.level))

        def send(key: str) -> None:
            assert remote.connected
            if rig.send_error:
                raise rig.send_error

            def apply() -> None:
                if not rig.stuck:
                    rig.level += 1 if key == "VOLUME_UP" else -1
                rig.events.append((key, rig.level))
                info = {"level": rig.level, "max": rig.maximum, "muted": False}
                for callback in list(remote.callbacks):
                    callback(info)

            if not rig.silent:
                asyncio.get_running_loop().call_soon(apply)

        remote.async_connect = AsyncMock(side_effect=connect)
        remote.add_volume_info_updated_callback.side_effect = remote.callbacks.append
        remote.remove_volume_info_updated_callback.side_effect = remote.callbacks.remove
        remote.send_key_command.side_effect = send
        remote.disconnect.side_effect = lambda: setattr(remote, "connected", False)
        return remote

    monkeypatch.setattr(tv_volume, "AndroidTVRemote", Mock(side_effect=remote_factory))
    rig.cast = Mock()
    rig.cast.status.volume_level = 0.0
    rig.cast.status.app_id = "AndroidNativeApp"
    rig.cast.status.display_name = "U-NEXT"
    rig.cast.cast_info.host = "mock-host"
    rig.tick = lambda cleanup=False: auto.process_device(
        rig.cast,
        "Dell",
        auto.load_state(auto.AUTO_STATE_FILE, "Dell") or auto.fresh_state("Dell"),
        cleanup=cleanup,
    )
    return rig


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:
    return make_atv_rig(tmp_path, monkeypatch)


def seed_atv(count: int = 1, original: int = 13, last: int = 12) -> dict[str, Any]:
    state = auto.fresh_state("Dell")
    state.update(
        volume_source="atv",
        volume_max=30,
        original_volume=original,
        last_lowered_to=last,
        consecutive_lowered=count,
    )
    auto.save_state(auto.AUTO_STATE_FILE, state)
    return state


def test_zero_cast_atv_three_ticks_defer_restore_before_power(
    rig: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    """(a) Cast 0 固定でも実音量を下げ、3 回目はキーを送らず OFF。"""
    with caplog.at_level(logging.INFO):
        for expected in (12, 11):
            rig.tick()
            assert rig.level == expected
            auto.power_off.assert_not_awaited()
        rig.tick()
    assert rig.level == 11
    assert [e for e in rig.events if e[0] != "READ"] == [
        ("VOLUME_DOWN", 12),
        ("VOLUME_DOWN", 11),
        ("POWER", 11),
    ]
    # 各キーの直後に新規接続の読み戻し。その接続を次のキーにも再利用する。
    for index, event in enumerate(rig.events):
        if event[0].startswith("VOLUME_"):
            assert rig.events[index + 1] == ("READ", event[1])
    assert len(rig.remotes) == 5  # 初回 3 接続 + 2 キーの読み戻し
    assert tv_volume.read_mac.await_count == 5
    assert all(not remote.connected for remote in rig.remotes)
    rig.cast.set_volume.assert_not_called()
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] is None
    summaries = [m for m in caplog.messages if m.startswith("tick 要約:")]
    assert len(summaries) == 3
    assert "count=3 result=turned_off volume_source=atv atv_level=11/30" in summaries[-1]
    assert "volume=0.0 volume_source=atv atv_level=11/30" in caplog.text
    assert "音量復元を保留" in caplog.text


def test_no_actual_decrease_does_not_count(rig: Mock, caplog: pytest.LogCaptureFixture) -> None:
    """(b) キー送信成功だけでは数えない。"""
    rig.stuck = True
    for _ in range(3):
        rig.tick()
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 0
    auto.power_off.assert_not_awaited()
    assert "カウントを進めません" in caplog.text


@pytest.mark.parametrize("paired", [True, False])
@pytest.mark.parametrize("cast_volume", [0.0, None, -0.1, float("nan"), float("inf"), True])
def test_unavailable_daily_alert_no_power(
    rig: Mock,
    paired: bool,
    cast_volume: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(c)(g) 不明値を 0 と扱い 3 回カウントだけ進めて突然切る回帰を防ぐ。"""
    rig.paired, rig.available = paired, False
    rig.cast.status.volume_level = cast_volume
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            rig.tick()
    auto.power_off.assert_not_awaited()
    rig.cast.set_volume.assert_not_called()
    rig.cast.quit_app.assert_not_called()
    auto.send_discovery_alert.assert_called_once()
    assert "volume_unavailable" in auto.send_discovery_alert.call_args.args[0]
    assert not auto.AUTO_STATE_FILE.exists()
    summaries = [m for m in caplog.messages if m.startswith("tick 要約:")]
    assert len(summaries) == 3
    assert all("count=0 result=volume_unavailable volume_source=none" in m for m in summaries)
    if not paired:
        tv_volume.AndroidTVRemote.assert_not_called()


def test_unavailable_keeps_pending_state_even_after_three_ticks(rig: Mock) -> None:
    seed_atv(count=3)
    before = auto.AUTO_STATE_FILE.read_bytes()
    rig.available = False
    for _ in range(3):
        rig.tick()
    assert auto.AUTO_STATE_FILE.read_bytes() == before
    auto.power_off.assert_not_awaited()


def test_cast_still_uses_original_path_without_atv_connections(rig: Mock) -> None:
    """(d) 有効な Cast 音量は追加接続なしで従来どおり。"""
    rig.cast.status.volume_level = 0.50
    rig.tick()
    rig.cast.set_volume.assert_called_once_with(0.46)
    tv_volume.AndroidTVRemote.assert_not_called()
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["volume_source"] == "cast"


def test_manual_rise_updates_original_and_resets_count(rig: Mock) -> None:
    """(e) 1 段の手動上昇も比率閾値を超えれば活動として扱う。"""
    seed_atv(count=2, last=12)
    rig.level = 15
    rig.tick()
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert (state["original_volume"], state["last_lowered_to"], state["consecutive_lowered"]) == (
        15,
        14,
        1,
    )
    auto.power_off.assert_not_awaited()


@pytest.mark.parametrize("cleanup", [True, False])
def test_manual_rise_during_cleanup_never_restores(rig: Mock, cleanup: bool) -> None:
    seed_atv()
    rig.level = 15
    if not cleanup:
        auto.read_power_state.return_value = False
        rig.cast.status.app_id = None
    rig.tick(cleanup=cleanup)
    assert not [e for e in rig.events if e[0].startswith("VOLUME_")]
    assert rig.level == 15
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] is None


@pytest.mark.parametrize("cleanup", [True, False])
def test_atv_cleanup_defers_with_same_source(rig: Mock, cleanup: bool) -> None:
    seed_atv()
    rig.level = 12
    if not cleanup:
        auto.read_power_state.return_value = False
        rig.cast.status.app_id = None
    rig.tick(cleanup=cleanup)
    assert not [e for e in rig.events if e[0].startswith("VOLUME_")]
    assert auto.load_pending_restore("Dell")["level"] == 13
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] is None
    auto.power_off.assert_not_awaited()


def test_old_state_defaults_to_cast(rig: Mock) -> None:
    """(f) 追加キーのない state を比率として復元する。"""
    data = dict(
        device_name="Dell",
        original_volume=0.5,
        last_lowered_to=0.46,
        consecutive_lowered=1,
        updated_at=time.time(),
    )
    auto.AUTO_STATE_FILE.write_text(json.dumps(data))
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert state["volume_source"] == "cast"
    assert state["volume_max"] is None
    assert state["original_volume"] == 0.5


@pytest.mark.parametrize(
    "bad",
    [
        {"volume_source": "unknown"},
        {"volume_max": 0},
        {"volume_max": True},
        {"original_volume": 31},
        {"original_volume": 1.5},
        {"last_lowered_to": -1},
    ],
)
def test_invalid_atv_state_rejected(rig: Mock, bad: dict[str, Any]) -> None:
    data = seed_atv()
    data.update(bad)
    assert not auto.valid_session_state(data)


@pytest.mark.parametrize("level", [0, 1, 2])
def test_actual_atv_minimum_counts_without_down_or_zeroing(rig: Mock, level: int) -> None:
    rig.level = level
    rig.tick()
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert state["consecutive_lowered"] == 1
    assert not [e for e in rig.events if e[0].startswith("VOLUME_")]


def test_atv_minimum_ceil_and_step_round(rig: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto, "STEP", -0.10)
    rig.level = 4
    rig.tick()
    assert rig.level == 2  # ceil(0.05 * 30)。0 まで下げない。
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 1


@pytest.mark.parametrize(
    "info",
    [
        None,
        {},
        {"level": 13, "max": 0},
        {"level": -1, "max": 30},
        {"level": 31, "max": 30},
        {"level": True, "max": 30},
        {"level": 1.0, "max": 30},
        {"level": 1, "max": True},
    ],
)
def test_invalid_volume_info_not_zero(info: Any) -> None:
    assert AtvVolume.parse(info) is None


def test_direct_cast_zero_tick_never_reaches_standby(rig: Mock) -> None:
    """旧 run_tick を直接通しても元の count-only 回帰が赤になる。"""
    state = auto.fresh_state("Dell")
    for _ in range(3):
        result, state = auto.run_tick(rig.cast, state)
        assert result.value == "volume_unavailable"
        assert state["consecutive_lowered"] == 0


@pytest.mark.parametrize(
    "error", [tv_volume.ConnectionClosed(), tv_volume.CannotConnect(), tv_volume.InvalidAuth()]
)
def test_connect_failure_is_recoverable(rig: Mock, error: Exception) -> None:
    rig.connect_error = error
    rig.tick()
    auto.power_off.assert_not_awaited()
    auto.send_discovery_alert.assert_called_once()
    assert all(not remote.connected for remote in rig.remotes)
    rig.connect_error = None
    rig.tick()
    assert rig.level == 12


def test_mac_mismatch_never_connects_or_sends(rig: Mock) -> None:
    tv_volume.read_mac.return_value = "other-mac"
    rig.tick()
    tv_volume.AndroidTVRemote.assert_not_called()
    auto.power_off.assert_not_awaited()


def test_disconnect_during_key_preserves_original_and_count(rig: Mock) -> None:
    rig.send_error = tv_volume.ConnectionClosed()
    rig.tick()
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert state["original_volume"] == 13
    assert state["consecutive_lowered"] == 0
    auto.power_off.assert_not_awaited()
    assert all(not remote.connected for remote in rig.remotes)


def test_no_push_and_no_actual_change_never_counts(rig: Mock) -> None:
    rig.silent = True
    rig.tick()
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 0
    auto.power_off.assert_not_awaited()


def test_off_does_not_attempt_restore_even_when_volume_stuck(
    rig: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    seed_atv(count=3, original=15, last=12)
    rig.level, rig.stuck = 12, True
    rig.tick()
    auto.power_off.assert_awaited_once()
    assert not [e for e in rig.events if e[0].startswith("VOLUME_")]
    assert "音量復元を読み戻しで確認できませんでした" not in caplog.text


def test_cleanup_keeps_restore_in_pending_file(rig: Mock) -> None:
    seed_atv(original=15)
    rig.level, rig.stuck = 12, True
    rig.tick(cleanup=True)
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] is None
    assert auto.load_pending_restore("Dell")["level"] == 15
    auto.power_off.assert_not_awaited()


def test_cleanup_restore_progress_survives_disconnect_and_retry_finishes(rig: Mock) -> None:
    """復元の途中で切れても、次の後始末が自分の UP を手動上昇と誤認せず完走する。"""
    seed_atv(original=15, last=10)
    stale = json.loads(auto.AUTO_STATE_FILE.read_text(encoding="utf-8"))
    stale["updated_at"] = time.time() - auto.AUTO_STATE_STALE_SEC - 600
    auto.AUTO_STATE_FILE.write_text(json.dumps(stale), encoding="utf-8")
    rig.level = 10
    original_send = rig.send_error
    calls = {"n": 0}
    real_factory = tv_volume.AndroidTVRemote.side_effect

    def flaky(*args: Any) -> Mock:
        remote = real_factory(*args)
        inner = remote.send_key_command.side_effect

        def send(key: str) -> None:
            calls["n"] += 1
            if calls["n"] == 3:
                raise ConnectionClosed("lost")
            inner(key)

        remote.send_key_command.side_effect = send
        return remote

    tv_volume.AndroidTVRemote.side_effect = flaky
    rig.tick(cleanup=True)
    assert rig.level == 10
    rig.tick()
    assert rig.level == 12
    assert auto.load_pending_restore("Dell")["left_at"] == 12
    assert auto.load_pending_restore("Dell")["level"] == 15
    tv_volume.AndroidTVRemote.side_effect = real_factory
    rig.send_error = original_send
    rig.tick()
    assert rig.level == 15
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] is None


def test_power_failure_propagates_and_retries_without_lowering(rig: Mock) -> None:
    seed_atv(count=3, original=13, last=10)
    rig.level = 10
    auto.power_off.side_effect = TvPowerError("off failed")
    with pytest.raises(TvPowerError):
        rig.tick()
    assert rig.level == 10
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 3
    auto.power_off.side_effect = None
    auto.power_off.return_value = "turned_off"
    rig.tick()
    assert not [e for e in rig.events if e[0] == "VOLUME_DOWN"]
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] is None


def test_volume_alert_retry_and_next_day(rig: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.available = False
    clock = Mock()
    clock.now.return_value = datetime(2026, 11, 12, 23, 0)
    monkeypatch.setattr(auto, "datetime", clock)
    auto.send_discovery_alert.side_effect = [False, True, True]
    rig.tick()
    assert "alerted_on" not in json.loads(auto.AUTO_VOLUME_ALERT_FILE.read_text())
    rig.tick()
    rig.tick()
    assert auto.send_discovery_alert.call_count == 2
    clock.now.return_value = datetime(2026, 11, 13, 0, 0)
    rig.tick()
    assert auto.send_discovery_alert.call_count == 3


def test_volume_alert_corrupt_file_and_write_error_do_not_crash(
    rig: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.available = False
    auto.AUTO_VOLUME_ALERT_FILE.write_bytes(b"\xff\xfe")
    rig.tick()
    auto.send_discovery_alert.assert_called_once()
    monkeypatch.setattr(auto, "AUTO_VOLUME_ALERT_FILE", auto.AUTO_VOLUME_ALERT_FILE.parent)
    rig.tick()
    auto.power_off.assert_not_awaited()


def test_source_change_discards_incompatible_units_and_count(rig: Mock) -> None:
    state = auto.fresh_state("Dell")
    state.update(original_volume=0.5, last_lowered_to=0.0, consecutive_lowered=3)
    auto.save_state(auto.AUTO_STATE_FILE, state)
    rig.tick()
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert (state["volume_source"], state["original_volume"], state["consecutive_lowered"]) == (
        "atv",
        13,
        1,
    )
    auto.power_off.assert_not_awaited()
    rig.cast.status.volume_level = 0.6
    rig.tick()
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert (state["volume_source"], state["original_volume"], state["consecutive_lowered"]) == (
        "cast",
        0.6,
        1,
    )


def test_max_change_discards_incompatible_atv_session(rig: Mock) -> None:
    seed_atv(count=2)
    rig.maximum = 20
    rig.tick()
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert state["volume_max"] == 20
    assert state["original_volume"] == 13
    assert state["consecutive_lowered"] == 1
    auto.power_off.assert_not_awaited()


def test_max_zero_is_unavailable_and_recovers(rig: Mock) -> None:
    rig.maximum = 0
    rig.tick()
    auto.power_off.assert_not_awaited()
    assert not auto.AUTO_STATE_FILE.exists()
    rig.maximum = 30
    rig.tick()
    assert rig.level == 12


def test_new_readback_loses_volume_preserves_original_without_off(rig: Mock) -> None:
    original_factory = tv_volume.AndroidTVRemote.side_effect

    def factory(*args: Any) -> Mock:
        if rig.remotes:
            rig.available = False
        return original_factory(*args)

    tv_volume.AndroidTVRemote.side_effect = factory
    rig.tick()
    state = auto.load_state(auto.AUTO_STATE_FILE, "Dell")
    assert state["original_volume"] == 13
    assert state["consecutive_lowered"] == 0
    assert rig.level == 12
    auto.power_off.assert_not_awaited()
    auto.send_discovery_alert.assert_called_once()


def test_read_waits_for_delayed_initial_volume_push(rig: Mock) -> None:
    remote = Mock(volume_info=None, is_on=True)

    async def connect() -> None:
        asyncio.get_running_loop().call_soon(
            setattr, remote, "volume_info", {"level": 13, "max": 30, "muted": False}
        )

    remote.async_connect = AsyncMock(side_effect=connect)
    tv_volume.AndroidTVRemote.side_effect = None
    tv_volume.AndroidTVRemote.return_value = remote

    async def read() -> AtvVolume:
        control = AtvVolumeControl("mock-host", "aa:bb")
        try:
            return await control.read()
        finally:
            control.disconnect()

    assert asyncio.run(read()) == AtvVolume(13, 30)
    remote.disconnect.assert_called_once()


def test_mac_change_at_readback_never_sends_second_key(
    rig: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auto, "STEP", -0.1)
    tv_volume.read_mac.side_effect = ["aa:bb", "different"]
    rig.tick()
    assert [e[0] for e in rig.events if e[0].startswith("VOLUME_")] == ["VOLUME_DOWN"]
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["consecutive_lowered"] == 0
    auto.power_off.assert_not_awaited()


def test_small_step_always_lowers_at_least_one_stage(rig: Mock) -> None:
    rig.maximum, rig.level = 5, 3
    rig.tick()
    assert rig.level == 2


def test_invalid_atv_state_file_is_rebuilt(rig: Mock) -> None:
    state = seed_atv()
    state["original_volume"] = 100
    auto.AUTO_STATE_FILE.write_text(json.dumps(state))
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell") is None
    rig.tick()
    assert auto.load_state(auto.AUTO_STATE_FILE, "Dell")["original_volume"] == 13
