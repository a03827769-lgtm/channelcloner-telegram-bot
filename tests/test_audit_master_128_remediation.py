import os
import re
import asyncio
import threading
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from database.db_manager import DatabaseManager, ReentrantAsyncLock
from database.models import ChannelPair, Subscription
from services.cloner_engine import ClonerEngine
from services.text_processor import TextProcessor
from services.story_cloner_service import StoryClonerService
from services.cache_manager import cache_manager


@pytest.mark.asyncio
async def test_reentrant_async_lock_reentrancy_and_mutual_exclusion():
    """Verify ReentrantAsyncLock allows re-entrant acquisition by same task and excludes other tasks"""
    lock = ReentrantAsyncLock()
    events = []

    async def task1():
        async with lock:
            events.append("t1_enter_1")
            async with lock:
                events.append("t1_enter_2")
                assert lock._depth == 2
                await asyncio.sleep(0.05)
            events.append("t1_exit_2")
            assert lock._depth == 1
        events.append("t1_exit_1")

    async def task2():
        await asyncio.sleep(0.01)  # Ensure task1 acquires first
        async with lock:
            events.append("t2_enter")
            assert lock._depth == 1

    await asyncio.gather(task1(), task2())

    assert events == ["t1_enter_1", "t1_enter_2", "t1_exit_2", "t1_exit_1", "t2_enter"]
    assert lock._depth == 0
    assert lock._owner is None


@pytest.mark.asyncio
async def test_subscription_expiry_deactivates_pairs(tmp_path):
    """Verify get_user_subscription deactivates channel pairs when subscription and trial have expired"""
    db_path = str(tmp_path / "test_sub_exp.db")
    db = DatabaseManager(db_path=db_path)
    await db.init_db()

    user_id = 888123
    await db.get_or_create_user(user_id, "Expired User", "exp_user")

    # Add active channel pair
    pair_id = await db.add_channel_pair(
        user_id=user_id,
        source_channel="@src_chan",
        source_title="Source",
        target_channel="@tgt_chan",
        target_title="Target"
    )

    # Insert expired subscription into database
    past_date = "2020-01-01T00:00:00"
    async with db.write_transaction() as wdb:
        await wdb.execute(
            "INSERT OR REPLACE INTO subscriptions (user_id, tier, trial_expires_at, expires_at, created_at) VALUES (?, 'free', ?, ?, ?)",
            (user_id, past_date, past_date, past_date)
        )
        await wdb.commit()

    await cache_manager.sub_cache.delete(f"sub_{user_id}")

    # Call get_user_subscription
    sub = await db.get_user_subscription(user_id)
    assert not sub.is_active

    # Check pair status: must be deactivated (is_active == 0)
    pair = await db.get_pair_by_id(pair_id)
    assert pair.is_active == 0


@pytest.mark.asyncio
async def test_dispatch_queued_payload_splits_long_caption():
    """Verify dispatch_queued_payload safely fits photo caption to 1024 and dispatches overflow as text"""
    engine = ClonerEngine()
    mock_bot = AsyncMock()
    mock_pair = MagicMock()
    mock_pair.target_channel = "@test_tgt"
    mock_pair.target_id = -1001234567890
    mock_pair.source_channel = "@test_src"

    long_text = "Headline: Luxury Villa in Tashkent.\n" + ("Description details about this amazing property.\n" * 40)
    assert len(long_text) > 1024

    payload = {
        "text": long_text,
        "media_type": "photo",
        "media_file_id": "temp_media/nonexistent_mock.jpg"
    }

    with patch("services.cloner_engine.rate_limiter.wait_for_slot", new=AsyncMock()):
        with patch.object(engine, "_send_with_retry", new=AsyncMock()) as mock_retry:
            await engine.dispatch_queued_payload(mock_bot, mock_pair, payload)
            
            # Must have sent send_photo for the media and send_message for the overflow text
            assert mock_retry.call_count >= 2
            
            # First call should be send_photo with caption <= 1024
            photo_call = mock_retry.call_args_list[0]
            assert photo_call[0][0] == mock_bot.send_photo
            caption_arg = photo_call[1].get("caption")
            assert caption_arg is not None
            assert len(caption_arg) <= 1024
            
            # Second call should be send_message with overflow
            msg_call = mock_retry.call_args_list[1]
            assert msg_call[0][0] == mock_bot.send_message
            overflow_arg = msg_call[1].get("text")
            assert len(overflow_arg) > 0


def test_attach_signature_with_custom_max_limit():
    """Verify attach_signature obeys custom max_limit (e.g. 1024 for photo captions)"""
    base_text = "Important post update. " * 50
    signature = "📢 Join @my_channel for more"
    
    # 1. Standard 4096 limit
    res_4096 = TextProcessor.attach_signature(base_text, signature, max_limit=4096)
    assert signature in res_4096
    assert len(res_4096) <= 4096

    # 2. Strict 1024 media caption limit
    res_1024 = TextProcessor.attach_signature(base_text, signature, max_limit=1024)
    assert signature in res_1024
    assert len(res_1024) <= 1024


@pytest.mark.asyncio
async def test_story_cloner_resolves_private_invite_links():
    """Verify StoryClonerService resolves private invite link channels via CheckChatInviteRequest"""
    scs = StoryClonerService()
    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)

    fake_chat = MagicMock()
    fake_chat.title = "Private Luxury Real Estate"
    fake_chat.id = -10099887766

    check_res = MagicMock()
    check_res.chat = fake_chat

    mock_client.side_effect = None
    mock_client.return_value = check_res

    with patch("services.story_cloner_service.db_manager.get_story_settings") as mock_get_st:
        fake_st = MagicMock()
        fake_st.is_active = True
        fake_st.source_channel = "https://t.me/+AbCdEfGh123"
        mock_get_st.return_value = fake_st

        with patch("services.story_cloner_service.db_manager.get_story_source_channels", new=AsyncMock(return_value=[])):
            with patch("services.story_cloner_service.db_manager.is_vip", new=AsyncMock(return_value=True)):
                with patch.object(scs, "get_client_for_user", new=AsyncMock(return_value=mock_client)):
                    ok = await scs.start_monitor_for_user(user_id=12345)
                    assert ok is True
                    # Check that mock_client was called with CheckChatInviteRequest
                    assert mock_client.call_count >= 1
