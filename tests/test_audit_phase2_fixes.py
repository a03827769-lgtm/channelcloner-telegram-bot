import os
import asyncio
import datetime
from datetime import timezone, timedelta
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from database.models import ChannelPair
from services.video_watermark_service import VideoWatermarkService
from services.dynamic_affiliate_engine import DynamicAffiliateEngine
from services.drip_feed_queue import DripFeedQueueService, UZB_TZ
from services.disaster_recovery import DisasterRecoveryService
from services.translator_service import TranslatorService
from bot.handlers.settings_menu import process_new_wm_text
from bot.handlers.history_clone import cb_cancel_history_clone
from admin_bot.handlers.backup import cb_download_backup


@pytest.mark.asyncio
async def test_telethon_send_fallback_uses_parse_mode_html():
    """Verify that _telethon_send_fallback specifies parse_mode='html' so rich formatting is preserved"""
    from services.cloner_engine import ClonerEngine
    engine = ClonerEngine()
    
    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    mock_client.send_file = AsyncMock()
    
    with patch("services.telethon_listener.telethon_listener.client", mock_client), \
         patch("services.telethon_listener.telethon_listener.resolve_entity", new_callable=AsyncMock, return_value="entity_target"):
        await engine._telethon_send_fallback(
            target_chat_id=-100123456789,
            file_path="/tmp/fake_video.mp4",
            caption="<b>Bold Title</b> with <a href='https://t.me'>Link</a>"
        )
        
    mock_client.send_file.assert_called_once()
    kwargs = mock_client.send_file.call_args.kwargs
    assert kwargs.get("parse_mode") == "html", f"Expected parse_mode='html', got: {kwargs.get('parse_mode')}"
    assert kwargs.get("caption") == "<b>Bold Title</b> with <a href='https://t.me'>Link</a>"


@pytest.mark.asyncio
async def test_video_watermark_scale_uses_minus_2(tmp_path):
    """Verify that FFmpeg filter_complex uses scale=...:-2 to prevent libx264 odd-height crashes"""
    service = VideoWatermarkService()
    vid_file = tmp_path / "in.mp4"
    vid_file.write_bytes(b"0" * 100)
    logo_file = tmp_path / "logo.png"
    logo_file.write_bytes(b"0" * 100)
    
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc
        
        await service.apply_video_logo_watermark(
            input_video_path=str(vid_file),
            logo_image_path=str(logo_file),
            scale_percent=20
        )
        
        mock_exec.assert_called_once()
        cmd_args = list(mock_exec.call_args[0])
        fc_idx = cmd_args.index("-filter_complex")
        filter_complex = cmd_args[fc_idx + 1]
        assert ":-2[logo]" in filter_complex, f"Expected ':-2[logo]', got: {filter_complex}"


def test_dynamic_affiliate_deduplicates_cta_buttons():
    """Verify that DynamicAffiliateEngine deduplicates CTA buttons for repeated URLs/domains"""
    engine = DynamicAffiliateEngine()
    text = "Check out https://uzum.uz/item1 and also https://uzum.uz/item1 here!"
    rules = "uzum.uz=>aff123"
    
    modified_text, buttons = engine.extract_and_convert_links(text, rules)
    # Target URL is identical, so buttons must be deduplicated to exactly 1
    assert len(buttons) == 1
    assert "p=aff123" in buttons[0][1]


@pytest.mark.asyncio
async def test_drip_feed_staggers_night_buffer_posts():
    """Verify that buffered night posts are staggered instead of all scheduled at 08:00:00 UTC"""
    queue = DripFeedQueueService()
    
    pair = ChannelPair(
        id=101,
        user_id=1,
        source_channel="@src",
        source_title="Source",
        target_channel="@tgt",
        target_title="Target",
        night_mode="buffer",
        drip_delay_minutes=2
    )
    
    # Simulate night time at 03:00 Tashkent time
    night_tashkent = datetime.datetime(2026, 9, 10, 3, 0, 0, tzinfo=UZB_TZ)
    with patch.object(queue, "get_current_time", return_value=night_tashkent):
        # 1. When no existing posts in queue, scheduled for morning 08:00
        with patch("database.db_manager.db_manager.get_latest_scheduled_drip_time", new_callable=AsyncMock) as mock_get_latest:
            mock_get_latest.return_value = None
            t1 = await queue.calculate_scheduled_time(pair)
            assert t1.hour == 8 and t1.minute == 0
            
            # 2. When an existing post is already scheduled for 08:00, next must stagger (+2 min)
            t1_utc = t1.astimezone(timezone.utc)
            mock_get_latest.return_value = t1_utc
            t2 = await queue.calculate_scheduled_time(pair)
            assert t2 > t1
            diff_min = (t2 - t1).total_seconds() / 60
            assert diff_min == 2, f"Expected 2 minutes staggering, got: {diff_min} min"


@pytest.mark.asyncio
async def test_settings_menu_preserves_watermark_position():
    """Verify that process_new_wm_text preserves existing watermark position rather than hardcoding bottom_right"""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
    
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=123, user_id=123)
    state = FSMContext(storage=storage, key=key)
    await state.update_data(pair_id=42)
    
    mock_pair = ChannelPair(
        id=42,
        user_id=123,
        source_channel="@src",
        source_title="Source",
        target_channel="@tgt",
        target_title="Target",
        image_watermark_pos="top_left"
    )
    
    mock_message = AsyncMock()
    mock_message.text = "My Brand Watermark"
    mock_message.from_user.id = 123
    mock_message.answer = AsyncMock()
    
    with patch("database.db_manager.db_manager.get_pair_by_id", new_callable=AsyncMock) as mock_get_pair, \
         patch("database.db_manager.db_manager.update_watermark_settings", new_callable=AsyncMock) as mock_update_wm:
        mock_get_pair.return_value = mock_pair
        
        await process_new_wm_text(mock_message, state)
        
        mock_update_wm.assert_called_once()
        pos_arg = mock_update_wm.call_args.kwargs.get("pos") or mock_update_wm.call_args.kwargs.get("position")
        assert pos_arg == "top_left", f"Expected position 'top_left' to be preserved, got '{pos_arg}'"


@pytest.mark.asyncio
async def test_backup_delete_wait_msg_resilient():
    """Verify that cb_download_backup does not alert failure if wait_msg.delete() throws"""
    mock_event = AsyncMock()
    mock_wait_msg = AsyncMock()
    mock_wait_msg.delete.side_effect = Exception("Cannot delete message")
    mock_wait_msg.edit_text = AsyncMock()
    mock_event.message.answer.return_value = mock_wait_msg
    mock_event.from_user.id = 12345
    
    with patch("database.db_manager.db_manager.create_backup_file", return_value="/tmp/test_backup.db"), \
         patch("zipfile.ZipFile"), \
         patch("os.path.exists", return_value=True), \
         patch("os.path.getsize", return_value=1024), \
         patch("admin_bot.handlers.backup.FSInputFile"):
        await cb_download_backup(mock_event)
        
    mock_wait_msg.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_history_cancel_keyboard_attached():
    """Verify that cb_cancel_history_clone provides reply_markup on cancellation"""
    mock_callback = AsyncMock()
    mock_callback.data = "hist_cancel_77"
    mock_callback.message.edit_text = AsyncMock()
    
    mock_pair = ChannelPair(
        id=77,
        user_id=999,
        source_channel="@src",
        source_title="Source",
        target_channel="@tgt",
        target_title="Target"
    )
    
    with patch("services.telethon_listener.telethon_listener.cancel_history_clone", return_value=True), \
         patch("database.db_manager.db_manager.get_pair_by_id", new_callable=AsyncMock) as mock_get_pair:
        mock_get_pair.return_value = mock_pair
        
        await cb_cancel_history_clone(mock_callback)
        
        mock_callback.message.edit_text.assert_called_once()
        kwargs = mock_callback.message.edit_text.call_args.kwargs
        assert "reply_markup" in kwargs
        assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_disaster_recovery_safe_send_resilience():
    """Verify that DisasterRecoveryService._safe_send retries after FloodWait and strips parse_mode on bad parse"""
    service = DisasterRecoveryService()
    
    calls = []
    async def mock_send(*args, **kwargs):
        calls.append(kwargs.copy())
        if len(calls) == 1:
            raise TelegramBadRequest(method="sendMessage", message="Bad Request: can't parse entities in message")
        return "SUCCESS"
        
    res = await service._safe_send(mock_send, parse_mode="HTML")
    assert res == "SUCCESS"
    assert len(calls) == 2
    assert calls[0].get("parse_mode") == "HTML"
    assert calls[1].get("parse_mode") is None


def test_video_watermark_escape_drawtext_apostrophe():
    """Verify that _escape_drawtext properly escapes apostrophes for FFmpeg without breaking syntax"""
    service = VideoWatermarkService()
    text = "O'zbekiston: Yangiliklar [2026] & 100%"
    escaped = service._escape_drawtext(text)
    assert "'\\''" not in escaped, "Shell-style quote escaping should not be present"
    assert r"O\'zbekiston" in escaped
    assert r"\:" in escaped
    assert r"\%" in escaped
    assert r"\[" in escaped and r"\]" in escaped

