"""テレビ本体の電源制御 (Android TV Remote v2) のペアリング用コマンド。

ペアリングするとテレビ画面に PIN が出る。人が読んで PIN ファイルへ書く運用に合わせ、
`--need-file` に「PIN が要る」旨を 1 行書いてから `--pin-file` が現れるのを待つ。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

from nemucast.cast_client import discover_chromecasts, stop_discovery
from nemucast.config import CHROMECAST_NAME
from nemucast.tv_power import (
    CLIENT_NAME,
    TvPowerError,
    cert_paths,
    ensure_cert_dir,
    read_mac,
    save_cached_host,
    save_identity,
)

PIN_WAIT_SEC = 600
PIN_POLL_SEC = 2


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="テレビの Android TV Remote をペアリングする")
    parser.add_argument("-n", "--name", default=CHROMECAST_NAME)
    parser.add_argument("--host", help="IP を直接指定する（省略時は Cast の発見結果）")
    parser.add_argument("--pin-file", type=Path, required=True)
    parser.add_argument("--need-file", type=Path, required=True)
    return parser.parse_args(args)


def _write_need(path: Path, message: str) -> None:
    logging.warning("need-pin: %s", message)
    path.write_text(message + "\n", encoding="utf-8")


def _resolve_host(name: str) -> str | None:
    cast, browser = discover_chromecasts(name)
    try:
        return cast.cast_info.host if cast else None
    finally:
        stop_discovery(browser)


def _wait_pin(pin_file: Path) -> str | None:
    deadline = time.monotonic() + PIN_WAIT_SEC
    while time.monotonic() < deadline:
        if pin_file.exists():
            pin = pin_file.read_text(encoding="utf-8").strip()
            if pin:
                return pin
        time.sleep(PIN_POLL_SEC)
    return None


async def pair(name: str, host: str, pin_file: Path, need_file: Path) -> int:
    cert, key = cert_paths()
    ensure_cert_dir()
    remote = AndroidTVRemote(CLIENT_NAME, str(cert), str(key), host)
    await remote.async_generate_cert_if_missing()
    for path in (cert, key):
        path.chmod(0o600)
    try:
        await remote.async_start_pairing()
    except (CannotConnect, ConnectionClosed) as exc:
        _write_need(need_file, f"{host} とペアリング開始できません（電源OFF?）: {exc!r}")
        return 1
    _write_need(need_file, f"テレビ ({host}) の画面に出た PIN を {pin_file} に書いてください")
    pin = await asyncio.get_running_loop().run_in_executor(None, _wait_pin, pin_file)
    if pin is None:
        remote.disconnect()
        logging.error("PIN が %d 秒以内に届きませんでした", PIN_WAIT_SEC)
        return 1
    try:
        await remote.async_finish_pairing(pin)
    except (InvalidAuth, ConnectionClosed) as exc:
        logging.error("ペアリング失敗（PIN 違い・取消し）: %s", type(exc).__name__)
        return 1
    finally:
        pin_file.unlink(missing_ok=True)
    try:
        save_identity(name, await read_mac(host))
        save_cached_host(name, host)
    except (TvPowerError, OSError) as exc:
        logging.warning("機器識別/IP の記録に失敗: %s", exc)
    logging.info("ペアリング完了: %s", cert)
    return 0


def main(args: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    parsed = parse_args(args)
    parsed.pin_file.unlink(missing_ok=True)
    host = parsed.host or _resolve_host(parsed.name)
    if host is None:
        _write_need(parsed.need_file, f"'{parsed.name}' が見つからず進めません（電源OFF/LAN外?）")
        sys.exit(1)
    sys.exit(asyncio.run(pair(parsed.name, host, parsed.pin_file, parsed.need_file)))


if __name__ == "__main__":
    main()
