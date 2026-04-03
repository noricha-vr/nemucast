#!/usr/bin/env python3
"""
lower_cast_volume.py
--------------------
指定した Chromecast / Google TV の音量を 20 分ごとに 1 ステップ下げ続ける
* pychromecast 10.4.0 以上推奨
* Python 3.9+
"""

import os
import time
import logging
import logging.handlers
import sys
import argparse
from pathlib import Path
from typing import Optional, Tuple

import pychromecast
from dotenv import load_dotenv

# .envファイルを読み込む
load_dotenv()

# ========= 設定 =========
CHROMECAST_NAME = os.getenv("CHROMECAST_NAME", "Dell")
STEP = float(os.getenv("STEP", "-0.04"))
MIN_LEVEL = float(os.getenv("MIN_LEVEL", "0.3"))
DEFAULT_INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "1200"))
DEFAULT_TIMEOUT_SEC = int(os.getenv("TIMEOUT_SEC", "21600"))
# ========================


def parse_args(args=None):
    """コマンドライン引数を解析する"""
    parser = argparse.ArgumentParser(
        description="Chromecast / Google TV の音量を定期的に下げるスクリプト"
    )
    parser.add_argument(
        "-i", "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SEC,
        help=f"音量調整の間隔（秒）。デフォルト: {DEFAULT_INTERVAL_SEC}秒"
    )
    parser.add_argument(
        "-n", "--name",
        type=str,
        default=CHROMECAST_NAME,
        help=f"Chromecastの名前。デフォルト: {CHROMECAST_NAME}"
    )
    parser.add_argument(
        "-s", "--step",
        type=float,
        default=STEP,
        help=f"音量調整のステップ（負の値）。デフォルト: {STEP}"
    )
    parser.add_argument(
        "-m", "--min-level",
        type=float,
        default=MIN_LEVEL,
        help=f"最小音量レベル。デフォルト: {MIN_LEVEL}"
    )
    parser.add_argument(
        "-t", "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"アイドル状態での最大待機時間（秒）。デフォルト: {DEFAULT_TIMEOUT_SEC}秒"
    )
    parsed = parser.parse_args(args)
    if parsed.timeout <= 0:
        parser.error("--timeout は正の整数を指定してください")
    return parsed


def setup_logging() -> None:
    """ロギングの設定を行う"""
    log_dir = Path.cwd() / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "lower_cast_volume.log"

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="[%(asctime)s] %(levelname)s: %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=512 * 1024, backupCount=1, encoding="utf-8"
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def discover_chromecasts(target_name: str) -> Tuple[Optional[pychromecast.Chromecast], Optional[pychromecast.discovery.CastBrowser]]:
    """
    Chromecastデバイスを検索し、指定された名前のデバイスを返す
    
    Args:
        target_name: 検索対象のChromecast名
        
    Returns:
        (cast, browser): 見つかったChromecastとブラウザオブジェクト
    """
    logging.info("Chromecast デバイスを検索しています...")
    
    chromecasts, browser = pychromecast.get_chromecasts()
    
    if not chromecasts:
        logging.error("ネットワーク上で Chromecast が見つかりませんでした。")
        return None, browser

    logging.info("発見したデバイス: %s", [cc.cast_info.friendly_name for cc in chromecasts])

    # friendly_name で目的のデバイスを探す
    for cc in chromecasts:
        logging.info(f'キャスト名: {cc.cast_info.friendly_name}')
        if cc.cast_info.friendly_name == target_name:
            return cc, browser

    logging.error("目的の Chromecast '%s' が見つかりませんでした。", target_name)
    return None, browser




def log_chromecast_status(cast) -> None:
    """Chromecastの現在の状態をログ出力する"""
    if cast.status.app_id:
        logging.info("Chromecast状態: アクティブ")
        
        # メディアコントローラーの情報も確認
        try:
            cast.media_controller.update_status()
            if cast.media_controller.status:
                if cast.media_controller.status.player_state:
                    logging.info("メディア状態: %s", cast.media_controller.status.player_state)
        except:
            pass
    else:
        logging.info("Chromecast状態: アイドル")


def log_active_app_status(cast) -> None:
    """アクティブなアプリの状態をログ出力する"""
    if hasattr(cast.media_controller, 'status') and cast.media_controller.status:
        if cast.media_controller.status.player_state:
            logging.info("再生状態: %s", cast.media_controller.status.player_state)


def is_chromecast_active(cast) -> bool:
    """
    Chromecastが実際にアクティブかどうかを確認する
    
    Returns:
        bool: アクティブならTrue、アイドル/スタンバイ状態ならFalse
    """
    try:
        # デバッグ情報を表示
        logging.debug(f"Cast status - app_id: {cast.status.app_id}, "
                     f"is_active_input: {cast.status.is_active_input}, "
                     f"is_stand_by: {cast.status.is_stand_by}")
        
        # app_idがNoneの場合はアイドル状態
        if cast.status.app_id is None:
            logging.debug("Chromecast is idle (no app running)")
            return False
            
        # IDLE_APP_IDまたはBackdropアプリの場合はアイドル状態
        if cast.status.app_id in [pychromecast.IDLE_APP_ID, 'E8C28D3C', 'Backdrop']:
            logging.debug(f"Chromecast is idle (app_id: {cast.status.app_id})")
            return False
        
        # メディアコントローラーの状態も確認
        try:
            cast.media_controller.update_status()
            if hasattr(cast.media_controller, 'status') and cast.media_controller.status:
                player_state = cast.media_controller.status.player_state
                logging.debug(f"Media player state: {player_state}")
                # メディアが再生中または一時停止中の場合はアクティブ
                if player_state in ['PLAYING', 'PAUSED', 'BUFFERING']:
                    return True
        except:
            pass
        
        # 何かアプリが起動している（AndroidNativeApp、YouTube、Netflixなど）
        logging.debug(f"Chromecast is active - app_id: {cast.status.app_id}")
        return True
        
    except Exception as e:
        logging.warning(f"Chromecastの状態確認に失敗しました: {e}")
        # エラーの場合は動作を継続するためTrueを返す
        return True


def wait_for_active(cast, timeout_sec: int, poll_interval: int = 60) -> bool:
    """Chromecastがアクティブになるまで待機する

    Args:
        cast: Chromecastオブジェクト
        timeout_sec: タイムアウト秒数
        poll_interval: ポーリング間隔（秒）

    Returns:
        アクティブになったらTrue、タイムアウトならFalse
    """
    elapsed = 0
    while elapsed < timeout_sec:
        if is_chromecast_active(cast):
            return True
        time.sleep(poll_interval)
        elapsed += poll_interval
    logging.info("待機タイムアウト: %d秒経過してもアクティブになりませんでした。", timeout_sec)
    return False


def get_initial_volume(cast) -> float:
    """起動時の音量を取得する"""
    initial_volume = cast.status.volume_level
    if initial_volume is None:
        logging.warning("起動時の音量を取得できませんでした。0.5を使用します。")
        initial_volume = 0.5
    else:
        logging.info("起動時の音量を保存しました: %.2f", initial_volume)
    return initial_volume


def adjust_volume(cast, current_volume: float, step: float, min_level: float) -> Optional[float]:
    """
    音量を調整する
    
    Args:
        cast: Chromecastオブジェクト
        current_volume: 現在の音量
        step: 音量調整ステップ
        min_level: 最小音量レベル
        
    Returns:
        新しい音量レベル、または最小レベルに達した場合はNone
    """
    if current_volume <= min_level:
        logging.info("最小音量に到達 (%.2f)。", current_volume)
        return None
    
    new_level = max(min_level-1, round(current_volume + step, 2))
    cast.set_volume(new_level)
    logging.info("音量を %.2f → %.2f へ変更しました", current_volume, new_level)
    return new_level


def restore_volume_and_standby(cast, initial_volume: float) -> None:
    """音量を初期値に戻してスタンバイモードにする"""
    # ボリュームを初期値に戻す
    cast.set_volume(initial_volume)
    logging.info("音量を初期値 %.2f に戻しました。", initial_volume)
    time.sleep(2)  # 音量設定が反映されるまで待機
    
    # Chromecastの電源を切る（スタンバイモードにする）
    # 既にスタンバイ状態でないかチェック
    if is_chromecast_active(cast):
        logging.info("Chromecastをスタンバイモードにします。")
        cast.quit_app()
        time.sleep(2)  # 処理が完了するまで待機
        logging.info("Chromecastがスタンバイモードになりました。")
    else:
        logging.info("Chromecastは既にスタンバイ状態です。")


def volume_control_loop(cast, interval_sec: int, step: float, min_level: float, initial_volume: float) -> None:
    """メインの音量制御ループ"""
    while True:
        # Chromecastがアクティブかどうかチェック
        if not is_chromecast_active(cast):
            logging.info("Chromecastがアイドル状態に戻りました。音量を初期値に戻して終了します。")
            restore_volume_and_standby(cast, initial_volume)
            break
        
        # アクティブな場合、起動中のアプリをログ出力
        log_active_app_status(cast)
        
        # 最新のステータスを更新して取得
        cast.media_controller.update_status()
        cur = cast.status.volume_level
        if cur is None:
            logging.warning("音量レベルを取得できませんでした。再試行します。")
            time.sleep(5)
            continue
            
        logging.info("現在の音量: %.2f", cur)
        
        # 音量を調整
        new_volume = adjust_volume(cast, cur, step, min_level)
        if new_volume is None:
            # 最小音量に到達した場合
            restore_volume_and_standby(cast, initial_volume)
            logging.info("プログラムを終了します。")
            break

        time.sleep(interval_sec)


def main() -> None:
    # コマンドライン引数を解析
    args = parse_args()
    interval_sec = args.interval
    chromecast_name = args.name
    step = args.step
    min_level = args.min_level
    
    # ロギングの設定
    setup_logging()
    
    logging.info(f"音量調整間隔: {interval_sec}秒")
    logging.info(f"Chromecast名: {chromecast_name}")
    logging.info(f"音量調整ステップ: {step}")
    logging.info(f"最小音量レベル: {min_level}")

    # Chromecastを検索
    cast, browser = discover_chromecasts(chromecast_name)
    if cast is None:
        if browser:
            pychromecast.stop_discovery(browser)
        sys.exit(1)

    try:
        logging.info("接続完了: %s (%s)", cast.cast_info.friendly_name, cast.cast_info.host)
        cast.wait()  # ソケット接続確立を待つ

        # Chromecastの状態をログ出力
        log_chromecast_status(cast)

        # アイドルならアクティブになるまで待機
        initial_volume = None
        if not is_chromecast_active(cast):
            logging.info("Chromecastはアイドル状態です。アクティブになるまで待機します...")
            if not wait_for_active(cast, args.timeout):
                logging.info("タイムアウトしました。プログラムを終了します。")
                return  # finally で stop_discovery が呼ばれる
            logging.info("Chromecastがアクティブになりました。")

        # ここに来た時点でアクティブ
        initial_volume = get_initial_volume(cast)

        # 音量制御ループを開始
        volume_control_loop(cast, interval_sec, step, min_level, initial_volume)
        
    except KeyboardInterrupt:
        logging.info("\n中断されました。音量を初期値に戻します...")
        try:
            if initial_volume is not None:
                cast.set_volume(initial_volume)
                logging.info("音量を初期値 %.2f に戻しました。", initial_volume)
                time.sleep(1)  # 音量設定が反映されるまで待機
        except Exception as e:
            logging.error("音量の復元に失敗しました: %s", e)
        raise
    finally:
        # Discoveryを適切に停止
        pychromecast.stop_discovery(browser)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n中断しました。")