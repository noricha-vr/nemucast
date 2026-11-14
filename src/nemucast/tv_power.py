"""テレビ本体の電源制御 (Android TV Remote protocol v2)。

POWER キーはトグルなので、毎回 is_on を読んでから送り、送った後は読み戻して確認する。
既に切れているテレビには何も送らない（点けてしまう事故を避ける）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

from nemucast.config import (
    CONNECT_TIMEOUT_SEC,
    TV_CERT_DIR,
    TV_POWER_KEY,
    TV_POWER_MAX_ATTEMPTS,
    TV_POWER_WAIT_SEC,
)

CLIENT_NAME = "nemucast"
IS_ON_READ_SEC = 10
RECHECK_DELAY_SEC = 3
POLL_SEC = 0.25


class TvPowerError(Exception):
    """テレビの電源を切れなかった、または状態を確認できなかった。"""


def cert_paths() -> tuple[Path, Path]:
    """クライアント証明書と秘密鍵のパスを返す。"""
    return TV_CERT_DIR / "tv_cert.pem", TV_CERT_DIR / "tv_key.pem"


def host_cache_path() -> Path:
    """名前ごとに最後に発見できた IP の保存先（mDNS に出ない時の到達確認用）。"""
    return TV_CERT_DIR / "last_hosts.json"


def identity_path() -> Path:
    """名前ごとにペアリングしたテレビの MAC アドレス（接続先の本人確認用）の保存先。"""
    return TV_CERT_DIR / "identities.json"


def ensure_cert_dir() -> None:
    """証明書ディレクトリを 0700 で用意する。"""
    TV_CERT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TV_CERT_DIR, 0o700)


def _load_json_map(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_map(path: Path, name: str, value: str) -> None:
    ensure_cert_dir()
    data = _load_json_map(path)
    data[name] = value
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def load_cached_host(name: str) -> str | None:
    host = _load_json_map(host_cache_path()).get(name)
    return host if isinstance(host, str) and host else None


def save_cached_host(name: str, host: str) -> None:
    _save_json_map(host_cache_path(), name, host)


def load_identity(name: str) -> str | None:
    mac = _load_json_map(identity_path()).get(name)
    return mac if isinstance(mac, str) and mac else None


def save_identity(name: str, mac: str) -> None:
    _save_json_map(identity_path(), name, mac)


async def read_mac(host: str) -> str:
    """ペアリング用ポートの証明書からテレビの MAC アドレスを読む。"""
    cert, key = cert_paths()
    remote = AndroidTVRemote(CLIENT_NAME, str(cert), str(key), host)
    try:
        _, mac = await asyncio.wait_for(remote.async_get_name_and_mac(), CONNECT_TIMEOUT_SEC)
    except (CannotConnect, InvalidAuth, TimeoutError, OSError) as exc:
        raise TvPowerError(f"{host} の機器識別 (MAC) を読めません: {exc!r}") from exc
    return mac.lower()


async def _read_is_on(host: str) -> tuple[AndroidTVRemote, bool]:
    """接続して is_on を読む。失敗は TvPowerError。"""
    cert, key = cert_paths()
    remote = AndroidTVRemote(CLIENT_NAME, str(cert), str(key), host)
    try:
        await asyncio.wait_for(remote.async_connect(), timeout=CONNECT_TIMEOUT_SEC)
        for _ in range(int(IS_ON_READ_SEC / POLL_SEC)):
            if remote.is_on is not None:
                return remote, remote.is_on
            await asyncio.sleep(POLL_SEC)
    except InvalidAuth as exc:
        remote.disconnect()
        raise TvPowerError("ペアリングが無効です。nemucast-tv-pair をやり直してください") from exc
    except (CannotConnect, ConnectionClosed, TimeoutError, OSError) as exc:
        remote.disconnect()
        raise TvPowerError(f"{host} に接続できません: {exc!r}") from exc
    remote.disconnect()
    raise TvPowerError(f"{host} の電源状態 (is_on) を {IS_ON_READ_SEC} 秒以内に読めませんでした")


async def _wait_off(remote: AndroidTVRemote) -> bool:
    for _ in range(int(TV_POWER_WAIT_SEC / POLL_SEC)):
        if remote.is_on is False:
            return True
        await asyncio.sleep(POLL_SEC)
    return False


async def read_power_state(host: str, expected_mac: str | None) -> bool:
    """保存済み MAC と照合し、電源状態だけを読む。キーは送らない。

    Raises:
        TvPowerError: MAC 未登録・不一致、接続不可、電源状態が不明。
    """
    if not expected_mac:
        raise TvPowerError("テレビの MAC が未登録です。nemucast-tv-pair で登録してください")
    try:
        actual = await read_mac(host)
    except ConnectionClosed as exc:
        raise TvPowerError(f"{host} の機器識別中に切断されました") from exc
    if actual != expected_mac.lower():
        raise TvPowerError(f"{host} はペアリングしたテレビではありません (MAC {actual})")
    remote, is_on = await _read_is_on(host)
    try:
        if type(is_on) is not bool:
            raise TvPowerError(f"{host} の電源状態が不明です")
        volume_info = getattr(remote, "volume_info", None)
        logging.info(
            "電源状態の生値: is_on=%r current_app=%r volume_level=%r",
            is_on,
            getattr(remote, "current_app", None),
            volume_info.get("level") if isinstance(volume_info, dict) else None,
        )
        return is_on
    finally:
        remote.disconnect()


async def power_off(host: str, expected_mac: str | None = None) -> str:
    """テレビの電源を切る。切れたことを新規接続で読み戻して確認する。

    Returns:
        "already_off"（元から切れていた）、"turned_off"（切った）、
        "turned_on_again"（OFF 観測後に点け直された。再送しない）。

    Args:
        host: テレビの IP。
        expected_mac: ペアリング時に記録した MAC。指定時は送信前に照合する。

    Raises:
        TvPowerError: 接続できない・状態を読めない・リトライしても切れない。
    """
    if expected_mac is not None:
        actual = await read_mac(host)
        if actual != expected_mac.lower():
            raise TvPowerError(f"{host} はペアリングしたテレビではありません (MAC {actual})")
    else:
        logging.warning(
            "テレビの MAC が未登録のため機器照合を省略します（nemucast-tv-pair で登録）"
        )
    remote, is_on = await _read_is_on(host)
    if not is_on:
        remote.disconnect()
        logging.info("テレビ %s は既に電源OFFです（is_on=False）。何も送りません。", host)
        return "already_off"

    return await _power_off_attempts(host, remote)


async def _send_power(remote: AndroidTVRemote) -> bool:
    """POWER 後の OFF 通知を待ち、送信接続を必ず閉じる。"""
    try:
        remote.send_key_command(TV_POWER_KEY)
        return await _wait_off(remote)
    except ConnectionClosed as exc:
        raise TvPowerError(f"キー送信中に切断されました: {exc!r}") from exc
    finally:
        remote.disconnect()


async def _power_off_attempts(host: str, remote: AndroidTVRemote) -> str:
    """OFF を一度も観測していない場合だけ、追加読み戻し後に再送する。"""
    for attempt in range(1, TV_POWER_MAX_ATTEMPTS + 1):
        if attempt > 1:
            remote.disconnect()
            remote, is_on = await _read_is_on(host)
            logging.info("POWER 再送前の追加読み戻し: attempt=%d is_on=%s", attempt, is_on)
            if is_on is not True:
                remote.disconnect()
                if is_on is False:
                    return "turned_off"
                raise TvPowerError("POWER 再送前の電源状態が不明です")
        if remote.is_on is not True:
            remote.disconnect()
            logging.info("テレビ %s は送信直前に電源OFFになっていました。何も送りません。", host)
            return "already_off"
        logging.info("テレビ %s に %s を送信します（%d 回目）", host, TV_POWER_KEY, attempt)
        sent_off = await _send_power(remote)
        logging.info("POWER 送信後: attempt=%d sent_off=%s", attempt, sent_off)
        await asyncio.sleep(RECHECK_DELAY_SEC)
        remote, is_on = await _read_is_on(host)
        logging.info("POWER 読み戻し: attempt=%d sent_off=%s is_on=%s", attempt, sent_off, is_on)
        if is_on is False:
            remote.disconnect()
            logging.info("テレビ %s の電源OFFを読み戻しで確認しました", host)
            return "turned_off"
        if sent_off:
            remote.disconnect()
            logging.info("テレビ %s は点け直されました。POWER を再送しません。", host)
            return "turned_on_again"
        logging.warning(
            "送信後も is_on=%s のままです（%d 回目 sent_off=%s）", is_on, attempt, sent_off
        )
    remote.disconnect()
    raise TvPowerError(f"{TV_POWER_MAX_ATTEMPTS} 回送っても電源が切れませんでした")
