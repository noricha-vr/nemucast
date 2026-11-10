"""tv_power（POWER トグルの冪等化と読み戻し）のテスト"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nemucast import tv_power
from nemucast.tv_power import TvPowerError, power_off


class FakeRemote:
    """送信ごとに is_on が反転する TV。stuck=True なら POWER が効かない。"""

    instances: list[FakeRemote] = []
    state = True
    stuck = False
    sent = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        FakeRemote.instances.append(self)

    @property
    def is_on(self) -> bool:
        return FakeRemote.state

    async def async_connect(self) -> None:
        return None

    def send_key_command(self, key: str) -> None:
        FakeRemote.sent += 1
        if not FakeRemote.stuck:
            FakeRemote.state = not FakeRemote.state

    def disconnect(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    FakeRemote.instances, FakeRemote.state, FakeRemote.stuck, FakeRemote.sent = [], True, False, 0
    monkeypatch.setattr(tv_power, "AndroidTVRemote", FakeRemote)
    monkeypatch.setattr(tv_power, "RECHECK_DELAY_SEC", 0)
    monkeypatch.setattr(tv_power, "TV_POWER_WAIT_SEC", 1)


def test_turns_off_when_on() -> None:
    assert asyncio.run(power_off("h")) == "turned_off"
    assert FakeRemote.sent == 1


def test_does_not_send_when_already_off() -> None:
    FakeRemote.state = False
    assert asyncio.run(power_off("h")) == "already_off"
    assert FakeRemote.sent == 0


def test_retries_then_fails_when_power_has_no_effect() -> None:
    FakeRemote.stuck = True
    with pytest.raises(TvPowerError):
        asyncio.run(power_off("h"))
    assert FakeRemote.sent == tv_power.TV_POWER_MAX_ATTEMPTS


def test_connection_failure_raises() -> None:
    async def fail(self: FakeRemote) -> None:
        raise tv_power.CannotConnect

    with patch.object(FakeRemote, "async_connect", fail):
        with pytest.raises(TvPowerError):
            asyncio.run(power_off("h"))


def test_mac_mismatch_refuses_to_send(monkeypatch) -> None:
    async def other_mac(host: str) -> str:
        return "aa:bb:cc:dd:ee:ff"

    monkeypatch.setattr(tv_power, "read_mac", other_mac)
    with pytest.raises(TvPowerError):
        asyncio.run(power_off("h", expected_mac="11:22:33:44:55:66"))
    assert FakeRemote.sent == 0


def test_mac_match_proceeds(monkeypatch) -> None:
    async def same_mac(host: str) -> str:
        return "11:22:33:44:55:66"

    monkeypatch.setattr(tv_power, "read_mac", same_mac)
    assert asyncio.run(power_off("h", expected_mac="11:22:33:44:55:66")) == "turned_off"


def test_never_sends_when_state_flips_off_before_send(monkeypatch) -> None:
    """読んだ後・送る前にテレビ側が False に変わったら POWER を送らない（点けない）"""
    real_read = tv_power._read_is_on

    async def read_then_flip(host: str):
        remote, is_on = await real_read(host)
        FakeRemote.state = False
        return remote, is_on

    monkeypatch.setattr(tv_power, "_read_is_on", read_then_flip)
    assert asyncio.run(power_off("h")) == "already_off"
    assert FakeRemote.sent == 0


def test_never_sends_when_state_cannot_be_read(monkeypatch) -> None:
    """is_on を確定できない（None のまま）なら POWER を送らずエラーにする"""
    monkeypatch.setattr(tv_power, "IS_ON_READ_SEC", 1)
    monkeypatch.setattr(FakeRemote, "is_on", property(lambda self: None))
    with pytest.raises(TvPowerError):
        asyncio.run(power_off("h"))
    assert FakeRemote.sent == 0


def test_never_sends_when_connect_fails_midway(monkeypatch) -> None:
    async def fail(self: FakeRemote) -> None:
        raise tv_power.ConnectionClosed

    monkeypatch.setattr(FakeRemote, "async_connect", fail)
    with pytest.raises(TvPowerError):
        asyncio.run(power_off("h"))
    assert FakeRemote.sent == 0


@pytest.mark.parametrize("is_on", [True, False])
def test_read_power_state_matches_mac_without_sending(is_on, monkeypatch) -> None:
    from unittest.mock import AsyncMock, Mock

    remote = Mock()
    monkeypatch.setattr(tv_power, "read_mac", AsyncMock(return_value="aa:bb"))
    monkeypatch.setattr(tv_power, "_read_is_on", AsyncMock(return_value=(remote, is_on)))
    assert asyncio.run(tv_power.read_power_state("h", "AA:BB")) is is_on
    remote.disconnect.assert_called_once()
    remote.send_key_command.assert_not_called()


@pytest.mark.parametrize("expected", [None, "different"])
def test_read_power_state_requires_identity_match(expected, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(tv_power, "read_mac", AsyncMock(return_value="aa:bb"))
    read_state = AsyncMock()
    monkeypatch.setattr(tv_power, "_read_is_on", read_state)
    with pytest.raises(TvPowerError):
        asyncio.run(tv_power.read_power_state("h", expected))
    read_state.assert_not_called()
    assert FakeRemote.sent == 0


@pytest.mark.parametrize("failure", ["connect", "unknown", "mac"])
def test_read_power_state_failure_never_sends(failure, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(tv_power, "read_mac", AsyncMock(return_value="aa:bb"))
    if failure == "connect":
        monkeypatch.setattr(
            FakeRemote, "async_connect", AsyncMock(side_effect=tv_power.CannotConnect)
        )
    elif failure == "mac":
        monkeypatch.setattr(tv_power, "read_mac", AsyncMock(side_effect=TvPowerError("no MAC")))
    else:
        monkeypatch.setattr(tv_power, "IS_ON_READ_SEC", 0)
        monkeypatch.setattr(FakeRemote, "is_on", property(lambda self: None))
    with pytest.raises(TvPowerError):
        asyncio.run(tv_power.read_power_state("h", "aa:bb"))
    assert FakeRemote.sent == 0


def test_read_power_state_disconnects_when_reader_returns_unknown(monkeypatch) -> None:
    from unittest.mock import AsyncMock, Mock

    remote = Mock()
    monkeypatch.setattr(tv_power, "read_mac", AsyncMock(return_value="aa:bb"))
    monkeypatch.setattr(tv_power, "_read_is_on", AsyncMock(return_value=(remote, None)))
    with pytest.raises(TvPowerError):
        asyncio.run(tv_power.read_power_state("h", "aa:bb"))
    remote.disconnect.assert_called_once()
    remote.send_key_command.assert_not_called()


def test_read_power_state_wraps_disconnect_during_identity_read(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(tv_power, "read_mac", AsyncMock(side_effect=tv_power.ConnectionClosed))
    with pytest.raises(TvPowerError):
        asyncio.run(tv_power.read_power_state("h", "aa:bb"))
    assert FakeRemote.sent == 0


@pytest.mark.parametrize("is_on", [True, False])
@pytest.mark.parametrize("metadata", [True, False])
def test_read_power_state_logs_raw_snapshot(
    is_on: bool,
    metadata: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """False の判定も生値を INFO 一行で追え、任意の情報がなくても読み取りは成功する。"""
    remote = Mock(spec=["disconnect", "send_key_command"])
    if metadata:
        remote.current_app = "com.example.player"
        remote.volume_info = {"level": 23, "max": 100, "muted": False}
    monkeypatch.setattr(tv_power, "read_mac", AsyncMock(return_value="test-mac"))
    monkeypatch.setattr(tv_power, "_read_is_on", AsyncMock(return_value=(remote, is_on)))
    with caplog.at_level(logging.INFO):
        result = asyncio.run(tv_power.read_power_state("test-host", "test-mac"))
    assert result is is_on
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert caplog.messages == [
        f"電源状態の生値: is_on={is_on!r} "
        f"current_app={('com.example.player' if metadata else None)!r} "
        f"volume_level={(23 if metadata else None)!r}"
    ]
    remote.disconnect.assert_called_once_with()
    remote.send_key_command.assert_not_called()
