# -*- coding: utf-8 -*-
import pytest
import pytest_asyncio
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

from aiogram.types import Message, CallbackQuery, User, Chat, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from config.settings import settings
from database.db_manager import DatabaseManager
from database.models import Subscription, StorySettings, StoryQueueItem
from services.story_cloner_service import story_cloner_service
from services.story_queue_service import story_queue_service
from bot.handlers.story_menu import (
    check_is_vip,
    get_story_vip_upgrade_text,
    get_story_vip_upgrade_text_and_keyboard,
    render_story_main_menu,
    show_story_menu,
    StoryVipMiddleware
)
from bot.keyboards.story_keyboards import get_story_vip_upgrade_keyboard
from bot.handlers.start import handle_story_button_text


@pytest_asyncio.fixture
async def test_db(tmp_path):
    db_file = tmp_path / "test_vip_restriction.db"
    db = DatabaseManager(db_path=str(db_file))
    await db.init_db()
    yield db
    await db.close()


def create_mock_callback(user_id: int, data: str):
    cb = AsyncMock(spec=CallbackQuery)
    cb.id = f"cb_{user_id}_{data}"
    cb.data = data
    cb.from_user = User(id=user_id, is_bot=False, first_name="TestUser", username="tester")
    cb.message = AsyncMock(spec=Message)
    cb.message.chat = Chat(id=user_id, type="private")
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def create_mock_message(user_id: int, text: str):
    msg = AsyncMock(spec=Message)
    msg.message_id = 12345
    msg.from_user = User(id=user_id, is_bot=False, first_name="TestUser", username="tester")
    msg.chat = Chat(id=user_id, type="private")
    msg.text = text
    msg.answer = AsyncMock()
    return msg


# ==========================================
# 1. SUBSCRIPTION MODEL & DB MANAGER TESTS
# ==========================================

@pytest.mark.asyncio
async def test_subscription_model_is_vip_property():
    """Verify is_vip behavior on Subscription dataclass across tiers and expiry"""
    future_date = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    past_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

    # Active VIP
    vip_sub = Subscription(user_id=1, tier="vip", expires_at=future_date)
    assert vip_sub.is_vip is True
    assert vip_sub.is_active is True

    # Expired VIP
    expired_vip = Subscription(user_id=2, tier="vip", expires_at=past_date)
    assert expired_vip.is_vip is False
    assert expired_vip.is_active is False

    # Pro tier
    pro_sub = Subscription(user_id=3, tier="pro", expires_at=future_date)
    assert pro_sub.is_vip is False
    assert pro_sub.is_active is True

    # Free tier
    free_sub = Subscription(user_id=4, tier="free", trial_expires_at=future_date)
    assert free_sub.is_vip is False


@pytest.mark.asyncio
async def test_db_manager_is_vip_and_revocation(test_db):
    """Verify db_manager.is_vip check and revocation downgrade"""
    free_user_id = 7001
    pro_user_id = 7002
    vip_user_id = 7003
    admin_user_id = 7004

    # Register users
    await test_db.get_or_create_user(free_user_id, "Free User", "freeuser")
    await test_db.get_or_create_user(pro_user_id, "Pro User", "prouser")
    await test_db.get_or_create_user(vip_user_id, "VIP User", "vipuser")
    await test_db.get_or_create_user(admin_user_id, "Admin User", "adminuser", is_admin=True)

    # Set subscriptions
    await test_db.activate_subscription(pro_user_id, "pro", 100, "ch_pro", days=30)
    await test_db.activate_subscription(vip_user_id, "vip", 300, "ch_vip", days=30)

    # Free user is not VIP
    assert await test_db.is_vip(free_user_id) is False

    # Pro user is not VIP
    assert await test_db.is_vip(pro_user_id) is False

    # VIP user is VIP
    assert await test_db.is_vip(vip_user_id) is True

    # Admin user is always VIP (via is_admin flag in DB)
    assert await test_db.is_vip(admin_user_id) is True

    # Configure story settings for VIP user
    await test_db.update_story_settings(vip_user_id, is_active=True, source_channel="@luxury_tashkent")
    st = await test_db.get_story_settings(vip_user_id)
    assert st.is_active is True

    # Revoke VIP user subscription
    await test_db.revoke_subscription(vip_user_id)
    assert await test_db.is_vip(vip_user_id) is False

    # Settings should be automatically deactivated
    st_revoked = await test_db.get_story_settings(vip_user_id)
    assert st_revoked.is_active is False


# ==========================================
# 2. VIP UPGRADE UI & KEYBOARD TESTS
# ==========================================

def test_story_vip_upgrade_keyboard():
    """Verify VIP upgrade keyboard has buy_plan_vip, menu_stars, and menu_main buttons"""
    kb = get_story_vip_upgrade_keyboard()
    assert isinstance(kb, InlineKeyboardMarkup)

    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "buy_plan_vip" in callbacks
    assert "menu_stars" in callbacks
    assert "menu_main" in callbacks

    # Check that the direct upgrade button has buy_plan_vip
    vip_btn = [btn for row in kb.inline_keyboard for btn in row if btn.callback_data == "buy_plan_vip"][0]
    assert "300 Stars" in vip_btn.text


def test_story_vip_upgrade_text():
    """Verify VIP upgrade prompt includes clear explanations for both free and pro users"""
    free_text = get_story_vip_upgrade_text("free")
    assert "VIP Cheksiz Tarif Talab Qilinadi" in free_text
    assert "Real Estate Auto-Story Cloner ($700+)" in free_text
    assert "300 Stars" not in free_text or "VIP" in free_text

    pro_text = get_story_vip_upgrade_text("pro")
    assert "Pro Tarif" in pro_text
    assert "VIP Cheksiz" in pro_text


# ==========================================
# 3. HANDLER & MIDDLEWARE RESTRICTION TESTS
# ==========================================

@pytest.mark.asyncio
async def test_story_vip_middleware_blocks_non_vip(test_db):
    """Verify StoryVipMiddleware blocks non-VIP callback and message events and displays prompt"""
    free_user_id = 8001
    await test_db.get_or_create_user(free_user_id, "Free User", "freeuser")

    middleware = StoryVipMiddleware()
    mock_handler = AsyncMock(return_value="handler_called")

    # 1. Test CallbackQuery interception
    cb = create_mock_callback(free_user_id, "story_toggle_active")
    data = {"event_from_user": cb.from_user}

    with patch("bot.handlers.story_menu.db_manager", test_db):
        res = await middleware(mock_handler, cb, data)
        # Handler was NOT called
        mock_handler.assert_not_called()
        assert res is None
        # User received message edit with VIP upgrade prompt
        cb.message.edit_text.assert_called_once()
        call_kwargs = cb.message.edit_text.call_args[1]
        assert "VIP Cheksiz Tarif Talab Qilinadi" in call_kwargs["text"]
        assert isinstance(call_kwargs["reply_markup"], InlineKeyboardMarkup)

    # 2. Test Message interception
    mock_handler.reset_mock()
    msg = create_mock_message(free_user_id, "/story")
    data = {"event_from_user": msg.from_user}

    with patch("bot.handlers.story_menu.db_manager", test_db):
        res = await middleware(mock_handler, msg, data)
        mock_handler.assert_not_called()
        assert res is None
        # User received response message with VIP upgrade prompt
        msg.answer.assert_called_once()
        call_kwargs = msg.answer.call_args[1]
        assert "VIP Cheksiz Tarif Talab Qilinadi" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_story_vip_middleware_allows_vip(test_db):
    """Verify StoryVipMiddleware allows active VIP subscribers through to handler"""
    vip_user_id = 8002
    await test_db.get_or_create_user(vip_user_id, "VIP User", "vipuser")
    await test_db.activate_subscription(vip_user_id, "vip", 300, "ch_vip8002", days=30)

    middleware = StoryVipMiddleware()
    mock_handler = AsyncMock(return_value="success_vip_result")

    cb = create_mock_callback(vip_user_id, "story_main_menu")
    data = {"event_from_user": cb.from_user}

    with patch("bot.handlers.story_menu.db_manager", test_db):
        res = await middleware(mock_handler, cb, data)
        mock_handler.assert_called_once_with(cb, data)
        assert res == "success_vip_result"


@pytest.mark.asyncio
async def test_render_story_main_menu_gate(test_db):
    """Verify render_story_main_menu returns VIP prompt for non-VIP and real dashboard for VIP"""
    free_user_id = 8003
    vip_user_id = 8004

    await test_db.get_or_create_user(free_user_id, "Free User", "freeuser")
    await test_db.get_or_create_user(vip_user_id, "VIP User", "vipuser")
    await test_db.activate_subscription(vip_user_id, "vip", 300, "ch_vip8004", days=30)

    with patch("bot.handlers.story_menu.db_manager", test_db):
        # Non-VIP gets VIP upgrade prompt
        text_free, kb_free = await render_story_main_menu(free_user_id)
        assert "VIP Cheksiz Tarif Talab Qilinadi" in text_free
        callbacks_free = [btn.callback_data for row in kb_free.inline_keyboard for btn in row]
        assert "buy_plan_vip" in callbacks_free

        # VIP gets real dashboard
        text_vip, kb_vip = await render_story_main_menu(vip_user_id)
        assert "Real Estate Auto-Story Cloner — Boshqaruv Markazi" in text_vip
        callbacks_vip = [btn.callback_data for row in kb_vip.inline_keyboard for btn in row]
        assert "story_menu_channels" in callbacks_vip


@pytest.mark.asyncio
async def test_start_reply_button_handler_vip_gate(test_db):
    """Verify tapping reply button 'Istoriya Kloner' in start.py gates non-VIP users"""
    free_user_id = 8005
    await test_db.get_or_create_user(free_user_id, "Free User", "freeuser")

    msg = create_mock_message(free_user_id, "Istoriya Kloner")
    state = AsyncMock(spec=FSMContext)

    with patch("bot.handlers.start.db_manager", test_db), \
         patch("bot.handlers.story_menu.db_manager", test_db):
        await handle_story_button_text(msg, state)

        msg.answer.assert_called_once()
        call_kwargs = msg.answer.call_args[1]
        assert "VIP Cheksiz Tarif Talab Qilinadi" in call_kwargs["text"]
        callbacks = [btn.callback_data for row in call_kwargs["reply_markup"].inline_keyboard for btn in row]
        assert "buy_plan_vip" in callbacks


# ==========================================
# 4. BACKGROUND SERVICES & QUEUE VIP TESTS
# ==========================================

@pytest.mark.asyncio
async def test_story_cloner_service_vip_restrictions(test_db):
    """Verify story_cloner_service methods reject non-VIP users"""
    free_user_id = 9001
    await test_db.get_or_create_user(free_user_id, "Free User", "freeuser")
    await test_db.update_story_settings(free_user_id, is_active=True, source_channel="@tashkent_vip")

    with patch("services.story_cloner_service.db_manager", test_db):
        # 1. start_monitor_for_user returns False for non-VIP
        res = await story_cloner_service.start_monitor_for_user(free_user_id)
        assert res is False

        # 2. test_publish_latest_post returns False with VIP upgrade notice
        ok, msg, payload = await story_cloner_service.test_publish_latest_post(free_user_id)
        assert ok is False
        assert "VIP" in msg


@pytest.mark.asyncio
async def test_story_queue_service_vip_restrictions(test_db):
    """Verify story_queue_service rejects enqueue and skips processing for non-VIP users"""
    free_user_id = 9002
    await test_db.get_or_create_user(free_user_id, "Free User", "freeuser")
    await test_db.update_story_settings(free_user_id, is_active=True, source_channel="@tashkent_rent")

    with patch("services.story_queue_service.db_manager", test_db):
        # 1. enqueue_listing raises PermissionError for non-VIP
        with pytest.raises(PermissionError):
            await story_queue_service.enqueue_listing(
                user_id=free_user_id,
                source_channel="@tashkent_rent",
                source_msg_id=55,
                payload={"test": 1},
                price=850.0,
                score=80
            )

        # 2. _process_due_item skips due items if user is not VIP
        # Enqueue directly into DB bypassing service
        item_id = await test_db.enqueue_story(
            user_id=free_user_id,
            source_channel="@tashkent_rent",
            source_msg_id=55,
            payload={"test": 1},
            scheduled_at_utc="2020-01-01 10:00:00",
            price=850.0,
            score=80
        )
        due_items = await test_db.get_due_story_queue_items()
        target_item = [it for it in due_items if it.id == item_id][0]

        mock_cloner = AsyncMock()
        mock_cloner.stop_monitor_for_user = MagicMock()
        await story_queue_service._process_due_item(target_item, mock_cloner)

        # Item should be marked 'skipped'
        async with test_db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT status, error_message FROM story_queue WHERE id = ?",
                (item_id,)
            )
            row = await cur.fetchone()
        assert row[0] == "skipped"
        assert "VIP" in row[1]


@pytest.mark.asyncio
async def test_start_all_active_monitors_skips_non_vip(test_db):
    """Verify start_all_active_monitors only starts monitoring for active VIP subscribers"""
    free_user = 9003
    vip_user = 9004

    await test_db.get_or_create_user(free_user, "Free User", "freeuser")
    await test_db.get_or_create_user(vip_user, "VIP User", "vipuser")
    await test_db.activate_subscription(vip_user, "vip", 300, "ch_vip9004", days=30)

    await test_db.update_story_settings(free_user, is_active=True, source_channel="@free_chan")
    await test_db.update_story_settings(vip_user, is_active=True, source_channel="@vip_chan")

    started_users = []

    async def mock_start_monitor(uid):
        started_users.append(uid)
        return True

    with patch("services.story_cloner_service.db_manager", test_db), \
         patch.object(story_cloner_service, "start_monitor_for_user", side_effect=mock_start_monitor), \
         patch("services.story_queue_service.story_queue_service.start_worker", return_value=None):

        await story_cloner_service.start_all_active_monitors()
        # Give asyncio tasks a tick to run
        await asyncio.sleep(0.05)

        # Only vip_user should have been started
        assert vip_user in started_users
        assert free_user not in started_users

        # Free user settings should have been deactivated
        free_st = await test_db.get_story_settings(free_user)
        assert free_st.is_active is False


@pytest.mark.asyncio
async def test_revoke_subscription_batch_skips_story_queue(test_db):
    """Verify revoking subscription automatically marks all pending story queue items as skipped"""
    vip_user = 9005
    await test_db.get_or_create_user(vip_user, "VIP User", "vipuser")
    await test_db.activate_subscription(vip_user, "vip", 300, "ch_vip9005", days=30)
    await test_db.update_story_settings(vip_user, is_active=True, source_channel="@vip_chan")

    # Enqueue multiple pending story items
    await test_db.enqueue_story(vip_user, "@vip_chan", 101, {"text": "1"}, "2026-09-18 10:00:00", 800.0, 90)
    await test_db.enqueue_story(vip_user, "@vip_chan", 102, {"text": "2"}, "2026-09-18 11:00:00", 900.0, 85)
    await test_db.enqueue_story(vip_user, "@vip_chan", 103, {"text": "3"}, "2026-09-18 12:00:00", 1200.0, 95)

    # Revoke subscription
    await test_db.revoke_subscription(vip_user)

    # All pending items should now be marked 'skipped'
    async with test_db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, status, error_message FROM story_queue WHERE user_id = ?",
            (vip_user,)
        )
        rows = await cur.fetchall()

    assert len(rows) == 3
    for r in rows:
        assert r[1] == "skipped"
        assert "VIP" in r[2] or "bekor" in r[2]


@pytest.mark.asyncio
async def test_stop_monitor_cleans_up_telethon_event_handler():
    """Verify stop_monitor_for_user removes event handler from client and clears cache"""
    user_id = 9006
    mock_client = MagicMock()
    mock_handler = MagicMock()
    mock_task = MagicMock()
    mock_task.done.return_value = False

    story_cloner_service._user_clients[user_id] = mock_client
    story_cloner_service._active_channel_handlers[user_id] = mock_handler
    story_cloner_service._user_poll_tasks[user_id] = mock_task

    story_cloner_service.stop_monitor_for_user(user_id)

    mock_task.cancel.assert_called_once()
    mock_client.remove_event_handler.assert_called_once_with(mock_handler)
    assert user_id not in story_cloner_service._active_channel_handlers
    assert user_id not in story_cloner_service._user_poll_tasks


@pytest.mark.asyncio
async def test_post_story_from_channel_aborts_if_not_vip(test_db):
    """Verify post_story_from_channel aborts with error if user is not VIP"""
    free_user = 9007
    await test_db.get_or_create_user(free_user, "Free User", "freeuser")

    mock_client = AsyncMock()
    mock_client.is_connected.return_value = True

    with patch("services.story_cloner_service.db_manager", test_db):
        ok, story_id, res_msg, story_url = await story_cloner_service.post_story_from_channel(
            client=mock_client,
            channel_identifier="@test_chan",
            msg_id=1,
            user_id=free_user
        )
        assert ok is False
        assert story_id is None
        assert "VIP" in res_msg


def test_help_guide_documents_story_cloner_vip():
    """Verify help guide explicitly documents Real Estate Story Cloner and VIP requirement"""
    from bot.handlers.help_guide import GUIDE_TEXT
    assert "Real Estate Auto-Story Cloner" in GUIDE_TEXT
    assert "VIP Cheksiz" in GUIDE_TEXT


def test_stars_billing_vip_success_keyboard():
    """Verify process_successful_payment presents direct story launcher button for VIP tier"""
    from bot.handlers.stars_billing import process_successful_payment
    from aiogram.types import SuccessfulPayment

    msg = create_mock_message(9008, "")
    msg.successful_payment = MagicMock(spec=SuccessfulPayment)
    msg.successful_payment.invoice_payload = "stars_plan_vip_9008"
    msg.successful_payment.total_amount = 300
    msg.successful_payment.telegram_payment_charge_id = "ch_test_vip"
    msg.bot = AsyncMock()

    mock_db = AsyncMock()
    mock_db.is_payment_processed.return_value = False
    mock_sub = MagicMock()
    mock_sub.expires_at = "2026-10-18T00:00:00"
    mock_db.activate_subscription.return_value = mock_sub

    with patch("bot.handlers.stars_billing.db_manager", mock_db), \
         patch("bot.handlers.stars_billing.format_uz_date", return_value="18-Oktabr, 2026-yil"):
        asyncio.run(process_successful_payment(msg))

        msg.answer.assert_called_once()
        call_kwargs = msg.answer.call_args[1]
        kb = call_kwargs["reply_markup"]
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert "story_main_menu" in callbacks


@pytest.mark.asyncio
async def test_plain_text_not_intercepted_by_story_router(test_db):
    """Verify plain text messages (e.g. 'Salom') do not match any handler in story_router"""
    from bot.handlers.story_menu import router as story_router

    free_user = 9009
    await test_db.get_or_create_user(free_user, "Free User", "freeuser")

    real_msg = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=free_user, type="private"),
        from_user=User(id=free_user, is_bot=False, first_name="FreeUser"),
        text="Salom bot, yordam kerak"
    )

    # Check each registered message handler on story_router
    # None of them should match this plain text message without state
    matched_handlers = []
    bot_mock = AsyncMock()
    for handler in story_router.message.handlers:
        all_match = True
        for f in handler.filters:
            try:
                res = await f.call(real_msg, bot=bot_mock)
                if not res:
                    all_match = False
                    break
            except Exception:
                all_match = False
                break
        if all_match:
            matched_handlers.append(handler)

    assert len(matched_handlers) == 0, f"Unintended handler matched plain text message: {matched_handlers}"


