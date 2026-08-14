"""standby スクリプト（即時 quit_app）のテスト"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from nemucast.standby import main


class TestStandby:
    def test_main_puts_device_to_standby(self, fake_network: Callable[[list[str]], Any]) -> None:
        """対象デバイスが見つかれば quit_app して discovery を閉じる"""
        with fake_network(["Living Room"]) as network:
            with patch("nemucast.cast_client.time.sleep"):
                main(args=["--name", "Living Room"])

        network["casts"]["Living Room"].quit_app.assert_called_once()
        network["browser"].stop_discovery.assert_called_once()

    def test_main_succeeds_when_device_absent(
        self, fake_network: Callable[[list[str]], Any]
    ) -> None:
        """デバイス不在は失敗ではない（TV が消えている日に cron が偽アラートを出さないこと）"""
        with fake_network(["OtherDevice"]) as network:
            main(args=["--name", "Missing"])

        network["browser"].stop_discovery.assert_called_once()
