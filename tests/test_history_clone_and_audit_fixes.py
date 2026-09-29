import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from database.db_manager import DatabaseManager
from database.models import ChannelPair
from services.telethon_listener import TelethonListener
from services.cloner_engine import ClonerEngine
from services.ai_paraphraser import AIParaphraserService
from bot.handlers.history_clone import cb_history_menu, cb_start_history_clone


@pytest.mark.asyncio
async def test_history_clone_skips_already_cloned_and_reports_all_cloned(tmp_path):
    """
    Verifies that clone_history:
    1. Skips already-cloned messages/albums instead of counting them as failures.
    2. Reports 'all_cloned' status when no uncloned messages remain.
    """
    db = DatabaseManager(db_path=str(tmp_path / "test_hist_clone.db"))
    await db.init_db()
    await db.get_or_create_user(user_id=101, full_name="User1", username="user1")
    pair_id = await db.add_channel_pair(
        user_id=101,
        source_channel="@src_test",
        source_title="Source",
        target_channel="@tgt_test",
        target_title="Target"
    )
    pair = await db.get_pair_by_id(pair_id)

    # Record message 100 as already cloned
    await db.record_cloned_message(pair_id=pair_id, source_msg_id=100, target_msg_id=500, media_type="text")

    listener = TelethonListener()
    listener.client = MagicMock()
    listener.client.is_connected = MagicMock(return_value=True)
    listener.client.is_user_authorized = AsyncMock(return_value=True)

    # Mock source entity
    fake_entity = MagicMock()
    fake_entity.id = 12345
    listener.resolve_entity = AsyncMock(return_value=fake_entity)

    # Mock message 100 (already cloned)
    msg100 = MagicMock()
    msg100.id = 100
    msg100.grouped_id = None
    msg100.text = "Old post"
    msg100.media = None
    msg100.action = None

    # Mock iter_messages returning message 100
    async def mock_iter_messages(*args, **kwargs):
        yield msg100

    listener.client.iter_messages = mock_iter_messages

    progress_events = []
    async def on_progress(current, total, status):
        progress_events.append((current, total, status))

    with patch("services.telethon_listener.db_manager", db):
        res = await listener.clone_history(pair, limit=10, progress_callback=on_progress)

    assert res["status"] == "all_cloned"
    assert res["cloned"] == 0
    assert res["failed"] == 0
    assert (0, 0, "all_cloned") in progress_events


@pytest.mark.asyncio
async def test_history_clone_clones_uncloned_items(tmp_path):
    """
    Verifies that clone_history clones genuinely uncloned items and records success without failures.
    """
    db = DatabaseManager(db_path=str(tmp_path / "test_hist_uncloned.db"))
    await db.init_db()
    await db.get_or_create_user(user_id=102, full_name="User2", username="user2")
    pair_id = await db.add_channel_pair(
        user_id=102,
        source_channel="@src_test2",
        source_title="Source2",
        target_channel="@tgt_test2",
        target_title="Target2"
    )
    pair = await db.get_pair_by_id(pair_id)

    listener = TelethonListener()
    listener.client = MagicMock()
    listener.client.is_connected = MagicMock(return_value=True)
    listener.client.is_user_authorized = AsyncMock(return_value=True)

    fake_entity = MagicMock()
    fake_entity.id = 99999
    listener.resolve_entity = AsyncMock(return_value=fake_entity)

    msg1 = MagicMock()
    msg1.id = 1
    msg1.grouped_id = None
    msg1.text = "Post 1"
    msg1.media = None
    msg1.action = None

    msg2 = MagicMock()
    msg2.id = 2
    msg2.grouped_id = None
    msg2.text = "Post 2"
    msg2.media = None
    msg2.action = None

    async def mock_iter_messages(*args, **kwargs):
        yield msg2
        yield msg1

    listener.client.iter_messages = mock_iter_messages

    progress_events = []
    async def on_progress(current, total, status):
        progress_events.append((current, total, status))

    mock_engine = MagicMock()
    mock_engine.clone_single_message = AsyncMock(return_value=True)

    with patch("services.telethon_listener.db_manager", db), \
         patch("services.telethon_listener.cloner_engine", mock_engine):
        res = await listener.clone_history(pair, limit=2, progress_callback=on_progress)

    assert res["status"] == "completed"
    assert res["total"] == 2
    assert res["cloned"] == 2
    assert res["failed"] == 0
    assert mock_engine.clone_single_message.call_count == 2


@pytest.mark.asyncio
async def test_paused_channel_pair_history_clone_rejected(tmp_path):
    """
    Verifies that paused pairs are rejected from launching history clones
    and both clone_single_message and clone_media_group return False when inactive.
    """
    db = DatabaseManager(db_path=str(tmp_path / "test_paused_pair.db"))
    await db.init_db()
    await db.get_or_create_user(user_id=103, full_name="User3", username="user3")
    pair_id = await db.add_channel_pair(
        user_id=103,
        source_channel="@src_paused",
        source_title="Source",
        target_channel="@tgt_paused",
        target_title="Target"
    )
    # Pause the pair
    await db.set_pair_active_by_owner(pair_id, False)
    pair = await db.get_pair_by_id(pair_id)
    assert pair.is_active is False

    engine = ClonerEngine()
    engine.bot = MagicMock()

    # Test clone_single_message returns False when inactive
    mock_msg = MagicMock()
    res_single = await engine.clone_single_message(mock_msg, pair)
    assert res_single is False

    # Test clone_media_group returns False when inactive
    res_mg = await engine.clone_media_group([mock_msg], pair)
    assert res_mg is False

    # Test UI handlers reject inactive pair
    cb = MagicMock()
    cb.data = f"pair_history_{pair_id}"
    cb.from_user.id = 103
    cb.answer = AsyncMock()

    with patch("bot.handlers.history_clone.db_manager", db):
        await cb_history_menu(cb, state=MagicMock())
        cb.answer.assert_called()
        alert_text = cb.answer.call_args.kwargs.get("text", "") or (cb.answer.call_args[0][0] if cb.answer.call_args[0] else "")
        assert "to'xtatilgan" in alert_text.lower()

    cb_start = MagicMock()
    cb_start.data = f"hist_start_{pair_id}_10"
    cb_start.from_user.id = 103
    cb_start.answer = AsyncMock()

    with patch("bot.handlers.history_clone.db_manager", db):
        await cb_start_history_clone(cb_start)
        cb_start.answer.assert_called()
        alert_start_text = cb_start.answer.call_args.kwargs.get("text", "") or (cb_start.answer.call_args[0][0] if cb_start.answer.call_args[0] else "")
        assert "to'xtatilgan" in alert_start_text.lower()


@pytest.mark.asyncio
async def test_send_test_post_cleans_custom_emojis_on_fallback():
    """
    Verifies that send_test_post sanitizes <tg-emoji> tags before Bot API fallback
    so it does not crash with TelegramBadRequest: custom emoji is not allowed.
    """
    engine = ClonerEngine()
    engine.bot = MagicMock()
    sent_text = None

    async def mock_send_message(*args, **kwargs):
        nonlocal sent_text
        sent_text = kwargs.get("text", "")
        mock_ret = MagicMock()
        mock_ret.message_id = 999
        return mock_ret

    engine.bot.send_message = mock_send_message
    engine._telethon_send_post = AsyncMock(return_value=None)  # Telethon unavailable

    pair = ChannelPair(
        id=1,
        user_id=1,
        source_channel="@src",
        target_channel="@tgt",
        target_id=-1001234567890,
        auto_premium_emojis=True,
        is_active=True
    )

    ok, msg = await engine.send_test_post(pair)

    assert ok is True
    assert "999" in msg
    assert sent_text is not None
    assert "<tg-emoji" not in sent_text
    assert "</tg-emoji>" not in sent_text


def test_ai_paraphraser_strips_markdown_code_fences():
    """
    Verifies that AIParaphraserService removes ```markdown and ``` fences from LLM output.
    """
    service = AIParaphraserService()
    fenced_1 = "```markdown\nBu yangilangan post matni.\n```"
    cleaned_1 = service._strip_markdown_code_fences(fenced_1)
    assert cleaned_1 == "Bu yangilangan post matni."

    fenced_2 = "```html\n<b>Muhim xabar</b>: Bugungi e'lon!\n```"
    cleaned_2 = service._strip_markdown_code_fences(fenced_2)
    assert cleaned_2 == "<b>Muhim xabar</b>: Bugungi e'lon!"

    fenced_3 = "```\nOddiy matn\n```"
    cleaned_3 = service._strip_markdown_code_fences(fenced_3)
    assert cleaned_3 == "Oddiy matn"


@pytest.mark.asyncio
async def test_image_hasher_multi_user_isolation():
    """
    Verifies that when User A (pair 10) clones a post and saves image hashes,
    User B (pair 28) is NOT blocked from cloning the same listing into their own channel.
    """
    from services.image_hasher import image_hasher

    h1 = "1234567890abcdef"
    h2 = "abcdef1234567890"

    # User A (pair 10) saved hashes for message 169255
    dummy_records = [
        {
            "pair_id": 10,
            "source_channel": "cityjoyestateuz",
            "source_msg_id": 169255,
            "price": 500.0,
            "phash": h1
        },
        {
            "pair_id": 10,
            "source_channel": "cityjoyestateuz",
            "source_msg_id": 169255,
            "price": 500.0,
            "phash": h2
        }
    ]

    with patch("database.db_manager.db_manager.get_recent_image_hashes", AsyncMock(return_value=dummy_records)):
        # When User B (pair 28) receives the same message 169255 from @cityjoyestateuz:
        is_dup, is_drop, match_info = await image_hasher.check_listing_duplicate(
            new_hashes=[h1, h2],
            current_price=500.0,
            pair_id=28,  # User B's pair
            source_channel="@cityjoyestateuz",
            source_msg_id=169255
        )
        # MUST NOT be marked as duplicate for pair 28!
        assert is_dup is False

        # Even for User A (pair 10), the EXACT SAME source message #169255 must NOT match itself
        is_dup_same, _, _ = await image_hasher.check_listing_duplicate(
            new_hashes=[h1, h2],
            current_price=500.0,
            pair_id=10,
            source_channel="@cityjoyestateuz",
            source_msg_id=169255
        )
        assert is_dup_same is False

        # Only a DIFFERENT message #169300 for the SAME pair 10 should be detected as a repost duplicate
        is_dup_repost, _, _ = await image_hasher.check_listing_duplicate(
            new_hashes=[h1, h2],
            current_price=500.0,
            pair_id=10,
            source_channel="@cityjoyestateuz",
            source_msg_id=169300
        )
        assert is_dup_repost is True


@pytest.mark.asyncio
async def test_history_clone_no_self_blocking_when_task_pre_registered(tmp_path):
    """
    Verifies that when history_clone.py pre-registers the asyncio Task into
    telethon_listener.active_history_tasks, clone_history does NOT detect itself
    as an external collision and successfully runs.
    """
    db = DatabaseManager(db_path=str(tmp_path / "test_self_block.db"))
    await db.init_db()
    await db.get_or_create_user(user_id=200, full_name="User200", username="user200")
    pair_id = await db.add_channel_pair(
        user_id=200,
        source_channel="@src_test",
        source_title="Source",
        target_channel="@tgt_test",
        target_title="Target"
    )
    pair = await db.get_pair_by_id(pair_id)

    listener = TelethonListener()
    listener.client = MagicMock()
    listener.client.is_connected = MagicMock(return_value=True)
    listener.client.is_user_authorized = AsyncMock(return_value=True)

    fake_entity = MagicMock()
    fake_entity.id = 55555
    listener.resolve_entity = AsyncMock(return_value=fake_entity)

    msg = MagicMock()
    msg.id = 1
    msg.grouped_id = None
    msg.text = "Hello"
    msg.media = None
    msg.action = None

    async def mock_iter(*args, **kwargs):
        yield msg

    listener.client.iter_messages = mock_iter

    mock_engine = MagicMock()
    mock_engine.clone_single_message = AsyncMock(return_value=True)

    async def _launch():
        task = asyncio.current_task()
        # Simulate history_clone.py pre-registering the task
        listener.active_history_tasks[pair_id] = task
        with patch("services.telethon_listener.db_manager", db), \
             patch("services.telethon_listener.cloner_engine", mock_engine):
            return await listener.clone_history(pair, limit=1)

    result = await _launch()
    # Must NOT return 'already_running'!
    assert result["status"] == "completed"
    assert result["cloned"] == 1
    # After completion, active_history_tasks must be cleared
    assert pair_id not in listener.active_history_tasks
