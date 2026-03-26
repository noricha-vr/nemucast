"""リファクタリングされた関数のテスト"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import logging
import sys
from pathlib import Path

# srcディレクトリをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nemucast.main import (
    setup_logging,
    discover_chromecasts,
    log_chromecast_status,
    log_active_app_status,
    is_chromecast_active,
    get_initial_volume,
    adjust_volume,
    restore_volume_and_standby,
)
import pychromecast


class TestRefactoredFunctions:
    """リファクタリングされた関数のテストクラス"""

    def test_setup_logging(self, tmp_path, monkeypatch):
        """ロギング設定のテスト"""
        # LOG_LEVELを設定
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.chdir(tmp_path)

        # ロギングをリセット
        logging.getLogger().handlers = []

        setup_logging()

        # ログレベルがDEBUGに設定されているか確認
        assert logging.getLogger().level == logging.DEBUG

        # ログディレクトリが作成されているか確認
        log_dir = tmp_path / "logs"
        assert log_dir.exists()

    def test_discover_chromecasts_found(self):
        """Chromecast検索のテスト（デバイスが見つかった場合）"""
        mock_cast = Mock()
        mock_cast.cast_info.friendly_name = "TestDevice"
        
        mock_browser = Mock()
        
        with patch("pychromecast.get_chromecasts", return_value=([mock_cast], mock_browser)):
            cast, browser = discover_chromecasts("TestDevice")
            
            assert cast == mock_cast
            assert browser == mock_browser

    def test_discover_chromecasts_not_found(self):
        """Chromecast検索のテスト（デバイスが見つからない場合）"""
        mock_cast = Mock()
        mock_cast.cast_info.friendly_name = "OtherDevice"
        
        mock_browser = Mock()
        
        with patch("pychromecast.get_chromecasts", return_value=([mock_cast], mock_browser)):
            cast, browser = discover_chromecasts("TestDevice")
            
            assert cast is None
            assert browser == mock_browser

    def test_log_chromecast_status_active(self, caplog):
        """Chromecast状態ログのテスト（アクティブ状態）"""
        mock_cast = Mock()
        mock_cast.status.app_id = "SomeApp"
        mock_cast.media_controller.status.player_state = "PLAYING"
        
        with caplog.at_level(logging.INFO):
            log_chromecast_status(mock_cast)
            
        assert "Chromecast状態: アクティブ" in caplog.text
        assert "メディア状態: PLAYING" in caplog.text

    def test_log_chromecast_status_idle(self, caplog):
        """Chromecast状態ログのテスト（アイドル状態）"""
        mock_cast = Mock()
        mock_cast.status.app_id = None
        
        with caplog.at_level(logging.INFO):
            log_chromecast_status(mock_cast)
            
        assert "Chromecast状態: アイドル" in caplog.text

    def test_log_active_app_status(self, caplog):
        """アクティブアプリ状態ログのテスト"""
        mock_cast = Mock()
        mock_cast.media_controller.status.player_state = "PAUSED"
        
        with caplog.at_level(logging.INFO):
            log_active_app_status(mock_cast)
            
        assert "再生状態: PAUSED" in caplog.text

    def test_is_chromecast_active_with_app(self):
        """Chromecastアクティブ判定のテスト（アプリ起動中）"""
        mock_cast = Mock()
        mock_cast.status.app_id = "AndroidNativeApp"
        mock_cast.status.is_active_input = True
        mock_cast.status.is_stand_by = False
        
        assert is_chromecast_active(mock_cast) == True

    def test_is_chromecast_active_idle(self):
        """Chromecastアクティブ判定のテスト（アイドル状態）"""
        mock_cast = Mock()
        mock_cast.status.app_id = None
        
        assert is_chromecast_active(mock_cast) == False

    def test_is_chromecast_active_backdrop(self):
        """Chromecastアクティブ判定のテスト（Backdrop表示中）"""
        mock_cast = Mock()
        mock_cast.status.app_id = "E8C28D3C"
        
        assert is_chromecast_active(mock_cast) == False

    def test_get_initial_volume(self):
        """初期音量取得のテスト"""
        mock_cast = Mock()
        mock_cast.status.volume_level = 0.75
        
        volume = get_initial_volume(mock_cast)
        
        assert volume == 0.75
        mock_cast.media_controller.update_status.assert_called_once()

    def test_get_initial_volume_none(self, caplog):
        """初期音量取得のテスト（取得失敗時）"""
        mock_cast = Mock()
        mock_cast.status.volume_level = None
        
        with caplog.at_level(logging.WARNING):
            volume = get_initial_volume(mock_cast)
            
        assert volume == 0.5
        assert "起動時の音量を取得できませんでした" in caplog.text

    def test_adjust_volume_normal(self):
        """音量調整のテスト（通常）"""
        mock_cast = Mock()
        
        new_volume = adjust_volume(mock_cast, 0.6, -0.04, 0.4)
        
        assert new_volume == 0.56
        mock_cast.set_volume.assert_called_once_with(0.56)

    def test_adjust_volume_min_reached(self):
        """音量調整のテスト（最小値到達）"""
        mock_cast = Mock()
        
        new_volume = adjust_volume(mock_cast, 0.4, -0.04, 0.4)
        
        assert new_volume is None

    def test_restore_volume_and_standby_active(self):
        """音量復元とスタンバイのテスト（アクティブ状態）"""
        mock_cast = Mock()
        
        with patch("nemucast.main.is_chromecast_active", return_value=True):
            restore_volume_and_standby(mock_cast, 0.7)
            
        mock_cast.set_volume.assert_called_once_with(0.7)
        mock_cast.quit_app.assert_called_once()

    def test_restore_volume_and_standby_already_idle(self):
        """音量復元とスタンバイのテスト（既にアイドル状態）"""
        mock_cast = Mock()
        
        with patch("nemucast.main.is_chromecast_active", return_value=False):
            restore_volume_and_standby(mock_cast, 0.7)
            
        mock_cast.set_volume.assert_called_once_with(0.7)
        mock_cast.quit_app.assert_not_called()