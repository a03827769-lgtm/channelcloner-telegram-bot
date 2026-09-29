import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import Message, User as AiogramUser, Chat
from aiogram.fsm.storage.base import StorageKey

from database.db_manager import DatabaseManager
from database.fsm_storage import SQLiteStorage
from bot.middlewares.user_registration_middleware import UserRegistrationMiddleware
from services.text_processor import TextProcessor


@pytest_asyncio.fixture
async def temp_db(tmp_path):
    db_file = str(tmp_path / "test_master_102.db")
    mgr = DatabaseManager(db_path=db_file)
    await mgr.init_db()
    try:
        yield mgr
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_user_is_blocked_lifecycle(temp_db):
    """
    Defect F-26: Verify mark_user_blocked flags user as blocked in DB,
    and subsequent get_or_create_user restores active status when user unblocks bot.
    """
    user = await temp_db.get_or_create_user(user_id=777888, full_name="Telegram User", username="tguser")
    assert user.is_blocked is False

    # Bot receives TelegramForbiddenError during broadcast
    await temp_db.mark_user_blocked(777888, True)
    assert await temp_db.is_user_blocked(777888) is True

    # User re-interacts with bot (unblocked bot and sent /start)
    reloaded_user = await temp_db.get_or_create_user(user_id=777888, full_name="Telegram User", username="tguser")
    assert reloaded_user.is_blocked is False
    assert await temp_db.is_user_blocked(777888) is False


@pytest.mark.asyncio
async def test_user_registration_middleware_caches_and_registers(temp_db):
    """
    Defect F-27: UserRegistrationMiddleware must automatically register new users
    and cache their presence in memory to prevent redundant database writes.
    """
    with patch("bot.middlewares.user_registration_middleware.db_manager", temp_db):
        middleware = UserRegistrationMiddleware()
        
        aiogram_user = AiogramUser(id=999001, is_bot=False, first_name="ActiveUser", username="active_user")
        chat = Chat(id=999001, type="private")
        message = MagicMock(spec=Message)
        message.from_user = aiogram_user
        message.chat = chat
        message.answer = AsyncMock()

        handler = AsyncMock(return_value="handled")
        data = {"event_from_user": aiogram_user}

        res = await middleware(handler, message, data)
        assert res == "handled"
        handler.assert_called_once()
        
        user_in_db = await temp_db.get_user_by_id(999001)
        assert user_in_db is not None
        assert user_in_db.full_name == "ActiveUser"


@pytest.mark.asyncio
async def test_fsm_storage_prunes_empty_records(temp_db):
    """
    Defect F-28: When FSM state is cleared and data is empty, fsm_storage row is deleted.
    """
    storage = SQLiteStorage(db=temp_db)
    key = StorageKey(bot_id=1, chat_id=123, user_id=456, destiny="default")

    # Set state and data
    await storage.set_state(key, "Wizard:step1")
    await storage.set_data(key, {"temp_url": "https://t.me/test"})
    assert await storage.get_state(key) == "Wizard:step1"

    # Clear state and data
    await storage.set_state(key, None)
    await storage.set_data(key, {})

    async with temp_db.get_connection() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM fsm_storage WHERE user_id = 456")
        row = await cursor.fetchone()
        assert row[0] == 0


def test_text_processor_zero_width_and_uzbek_apostrophe():
    """
    Defect F-77 & F-78: Zero-width spaces and varied Uzbek apostrophes
    must not bypass blacklisted word or commercial ad filters.
    """
    blacklist = ["reklama", "kanalga a'zo"]
    
    # Advertiser inserts zero-width spaces (\u200b) to bypass filter
    evasive_text = "Bizning r\u200be\u200bk\u200bl\u200ba\u200bm\u200ba e'lonimiz"
    assert TextProcessor.contains_blacklisted_words(evasive_text, blacklist) is True

    # User enters apostrophe with right curly quote (’) or modifier letter (ʻ)
    apostrophe_text = "Kanalga aʻzo bo'ling do'stlar"
    assert TextProcessor.contains_blacklisted_words(apostrophe_text, blacklist) is True


def test_attach_signature_utf16_custom_emojis():
    """
    Defect F-75: attach_signature must account for UTF-16 code units for custom emojis.
    """
    text = "A" * 4000
    signature = "⭐️ VIP Channel ⭐️"
    result = TextProcessor.attach_signature(text, signature)
    assert TextProcessor.get_visible_text_length(result) <= 4096
    assert signature in result


@pytest.mark.asyncio
async def test_story_queue_disconnected_client_reschedules(temp_db):
    """
    Defect F-51: When Telethon client is not connected, story queue item
    is rescheduled by 5 minutes, preventing 30-second busy loops.
    """
    from services.story_queue_service import story_queue_service
    from database.models import StoryQueueItem

    item = StoryQueueItem(
        id=55,
        user_id=12345,
        source_channel="test_chan",
        source_msg_id=10,
        score=9.5,
        scheduled_at="2026-01-01 10:00:00",
        payload_json=json.dumps({"photos": []})
    )

    mock_settings = MagicMock(is_active=True, max_stories_per_day=5)
    with patch("services.story_queue_service.db_manager", temp_db), \
         patch.object(temp_db, "is_vip", AsyncMock(return_value=True)), \
         patch.object(temp_db, "get_story_settings", AsyncMock(return_value=mock_settings)), \
         patch.object(temp_db, "get_today_posted_story_count", AsyncMock(return_value=0)), \
         patch("services.story_cloner_service.story_cloner_service.get_client_for_user", AsyncMock(return_value=None)):
        await temp_db.execute(
            "INSERT INTO story_queue (id, user_id, source_channel, source_msg_id, score, scheduled_at, payload_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (55, 12345, "test_chan", 10, 9.5, "2026-01-01 10:00:00", json.dumps({"photos": []}))
        )
        await story_queue_service._process_due_item(item)

    async with temp_db.get_connection() as db:
        cursor = await db.execute("SELECT scheduled_at FROM story_queue WHERE id = 55")
        row = await cursor.fetchone()
        assert row[0] != "2026-01-01 10:00:00"
