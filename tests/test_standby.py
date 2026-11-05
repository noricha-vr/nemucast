"""standby スクリプト（テレビ電源OFF）のテスト"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nemucast import standby
from nemucast.tv_power import TvPowerError


def _fake_discovery(host: str | None) -> tuple[object, MagicMock]:
    browser = MagicMock()
    cast = SimpleNamespace(cast_info=SimpleNamespace(host=host)) if host else None
    return cast, browser


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr("nemucast.tv_power.TV_CERT_DIR", tmp_path / "cert")
    monkeypatch.chdir(tmp_path)
    yield
    import logging

    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)
        h.close()


class TestResolveHost:
    def test_uses_discovered_host_and_caches_it(self) -> None:
        with patch.object(
            standby, "discover_chromecasts", return_value=_fake_discovery("10.0.0.5")
        ):
            with patch.object(standby, "stop_discovery") as stop:
                assert standby.resolve_host("Living") == "10.0.0.5"
        stop.assert_called_once()
        assert standby.load_cached_host("Living") == "10.0.0.5"

    def test_falls_back_to_cache_for_same_name_only(self) -> None:
        standby.save_cached_host("Living", "10.0.0.5")
        with patch.object(standby, "discover_chromecasts", return_value=_fake_discovery(None)):
            with patch.object(standby, "stop_discovery"):
                assert standby.resolve_host("Living") == "10.0.0.5"
                with pytest.raises(TvPowerError):
                    standby.resolve_host("Missing")


class TestMain:
    def test_success_does_not_alert(self) -> None:
        with (
            patch.object(standby, "resolve_host", return_value="10.0.0.5"),
            patch.object(standby, "power_off", new=MagicMock(return_value=_done("turned_off"))),
            patch.object(standby, "send_discovery_alert") as alert,
        ):
            standby.main(args=["--name", "Living"])
        alert.assert_not_called()

    def test_failure_exits_nonzero_and_alerts(self) -> None:
        with (
            patch.object(standby, "resolve_host", side_effect=TvPowerError("見つからない")),
            patch.object(standby, "send_discovery_alert") as alert,
        ):
            with pytest.raises(SystemExit) as exc:
                standby.main(args=["--name", "Missing"])
        assert exc.value.code == 1
        assert "Missing" in alert.call_args.args[0]

    def test_power_off_failure_exits_nonzero_and_alerts(self) -> None:
        async def boom(host: str, expected_mac: str | None = None) -> str:
            raise TvPowerError("切れない")

        with (
            patch.object(standby, "resolve_host", return_value="10.0.0.5"),
            patch.object(standby, "power_off", boom),
            patch.object(standby, "send_discovery_alert") as alert,
        ):
            with pytest.raises(SystemExit) as exc:
                standby.main(args=["--name", "Living"])
        assert exc.value.code == 1
        alert.assert_called_once()


async def _done(value: str) -> str:
    return value


def test_cache_save_failure_is_not_fatal() -> None:
    with (
        patch.object(standby, "discover_chromecasts", return_value=_fake_discovery("10.0.0.5")),
        patch.object(standby, "stop_discovery"),
        patch.object(standby, "save_cached_host", side_effect=OSError("denied")),
    ):
        assert standby.resolve_host("Living") == "10.0.0.5"


def test_oserror_exits_nonzero_and_alerts() -> None:
    with (
        patch.object(standby, "resolve_host", side_effect=OSError("boom")),
        patch.object(standby, "send_discovery_alert") as alert,
    ):
        with pytest.raises(SystemExit) as exc:
            standby.main(args=["--name", "Living"])
    assert exc.value.code == 1
    alert.assert_called_once()
