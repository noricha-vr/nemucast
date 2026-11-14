"""MAC 照合済みの Android TV 音量を整数段で操作し、新規接続で確認する。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

from nemucast.config import AUTO_ATV_VOLUME_WAIT_SEC, CONNECT_TIMEOUT_SEC
from nemucast.tv_power import CLIENT_NAME, POLL_SEC, TvPowerError, cert_paths, read_mac


class TvVolumeError(TvPowerError):
    """音量の取得・送信・読み戻しを確認できなかった。"""


class TvVolumePowerError(TvVolumeError):
    """点灯を確認できず、音量の読み値も復元の進捗も信用できない。"""


ATV_MIN_LEVEL = 1
ATV_RESTORE_TOLERANCE = 1


@dataclass(frozen=True)
class AtvVolume:
    """検証済みの音量値。未受信や不正値を 0 と混同しない。"""

    level: int
    maximum: int

    @classmethod
    def parse(cls, info: Any) -> AtvVolume | None:
        """ライブラリの volume_info を検証する。"""
        if not isinstance(info, dict):
            return None
        level, maximum = info.get("level"), info.get("max")
        if type(level) is not int or type(maximum) is not int:
            return None
        if maximum <= 0 or not 0 <= level <= maximum:
            return None
        return cls(level, maximum)


class AtvVolumeControl:
    """読み戻し接続を次の送信にも使い、キーごとに実際の変化を確認する。"""

    def __init__(self, host: str, expected_mac: str) -> None:
        self.host = host
        self.expected_mac = expected_mac
        self.remote: AndroidTVRemote | None = None
        self.volume: AtvVolume | None = None

    def disconnect(self) -> None:
        """現在の接続を閉じる。"""
        if self.remote is not None:
            self.remote.disconnect()
            self.remote = None

    async def read(self) -> AtvVolume:
        """MAC と点灯を確認し、新規接続の音量の push を有限時間待つ。"""
        self.disconnect()
        try:
            if not self.expected_mac or await read_mac(self.host) != self.expected_mac.lower():
                raise TvVolumeError("ATV 音量の接続先 MAC が一致しません")
            cert, key = cert_paths()
            self.remote = AndroidTVRemote(CLIENT_NAME, str(cert), str(key), self.host)
            await asyncio.wait_for(self.remote.async_connect(), CONNECT_TIMEOUT_SEC)
            deadline = asyncio.get_running_loop().time() + AUTO_ATV_VOLUME_WAIT_SEC
            while True:
                if self.remote.is_on is not True:
                    raise TvVolumePowerError("消灯中または電源不明のため ATV 音量を採用しません")
                volume = AtvVolume.parse(self.remote.volume_info)
                if volume is not None:
                    self.volume = volume
                    return volume
                if asyncio.get_running_loop().time() >= deadline:
                    raise TvVolumeError("ATV 音量の有効な level/max を取得できませんでした")
                await asyncio.sleep(POLL_SEC)
        except (CannotConnect, ConnectionClosed, InvalidAuth, TimeoutError, OSError) as exc:
            self.disconnect()
            raise TvVolumeError(f"ATV 音量に接続できません: {exc!r}") from exc
        except TvVolumeError:
            self.disconnect()
            raise
        except TvPowerError as exc:
            self.disconnect()
            raise TvVolumeError(str(exc)) from exc

    async def _send(self, key: str) -> None:
        """送信バッファを破棄しないよう push を待ってから読み戻しへ進む。"""
        if self.remote is None:
            raise TvVolumeError("ATV 音量の接続がありません")
        if self.remote.is_on is not True:
            raise TvVolumePowerError("消灯中または電源不明のため ATV 音量キーを送りません")
        updated = asyncio.Event()

        def on_volume(_info: Any) -> None:
            updated.set()

        self.remote.add_volume_info_updated_callback(on_volume)
        try:
            self.remote.send_key_command(key)
            try:
                await asyncio.wait_for(updated.wait(), AUTO_ATV_VOLUME_WAIT_SEC)
            except TimeoutError:
                # push が欠けても、新規接続の実値で送信結果を判断する。
                logging.warning("ATV 音量キーの更新通知を待ち切れませんでした: key=%s", key)
        except (ConnectionClosed, OSError) as exc:
            raise TvVolumeError(f"ATV 音量キー送信に失敗しました: {exc!r}") from exc
        finally:
            self.remote.remove_volume_info_updated_callback(on_volume)

    async def move_to(self, target: int) -> AtvVolume:
        """差分回数を上限にキーを送り、毎回新規接続で方向と最大値を確認する。"""
        if self.volume is None or self.remote is None:
            raise TvVolumeError("ATV 音量が未取得です")
        initial = self.volume
        target = max(ATV_MIN_LEVEL, min(target, initial.maximum))
        direction = 1 if target > initial.level else -1
        key = "VOLUME_UP" if direction > 0 else "VOLUME_DOWN"
        for _ in range(abs(target - initial.level)):
            before = self.volume
            if (target - before.level) * direction <= 0:
                break
            await self._send(key)
            after = await self.read()
            if after.maximum != initial.maximum:
                raise TvVolumeError("ATV 音量の max が操作中に変わりました")
            if (after.level - before.level) * direction <= 0:
                logging.warning(
                    "ATV 音量が変わりませんでした: key=%s before=%d after=%d",
                    key,
                    before.level,
                    after.level,
                )
                break
        return self.volume
