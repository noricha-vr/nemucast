"""tv_power（POWER トグルの冪等化と読み戻し）のテスト"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

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
