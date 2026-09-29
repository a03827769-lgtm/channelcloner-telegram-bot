import uuid
import pytest
from unittest.mock import AsyncMock, patch
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, User as TgUser
from database.db_manager import DatabaseManager
from database.fsm_storage import SQLiteStorage
from bot.handlers.history_clone import cb_cancel_history_clone
from admin_bot.handlers.broadcast import _broadcast_lock, BroadcastStates

import pytest_asyncio

class DemoStates(StatesGroup):
    step_one = State()
    step_two = State()

@pytest_asyncio.fixture
async def test_db(tmp_path):
    db_name = str(tmp_path / f"test_audit_{uuid.uuid4().hex[:8]}.db")
    db = DatabaseManager(db_name)
    await db.init_db()
    yield db
    from tests.test_utils import safe_cleanup_db
    await safe_cleanup_db(db_name, db)

@pytest.mark.asyncio
async def test_fsm_persistence_across_restart(test_db):
    """
    Verify FSM state and data survive across application restarts/dispatcher re-initialization.
    """
    storage1 = SQLiteStorage(test_db)
    key = StorageKey(bot_id=1001, chat_id=2002, user_id=3003)

    # 1. User enters state and fills data in dispatcher 1
    await storage1.set_state(key, DemoStates.step_one)
    await storage1.set_data(key, {"source_channel": "@my_source", "mode": "clean"})

    # 2. Verify in storage 1
    state1 = await storage1.get_state(key)
    data1 = await storage1.get_data(key)
    assert state1 == DemoStates.step_one.state
    assert data1["source_channel"] == "@my_source"

    # 3. Simulate process restart / new dispatcher instance with fresh storage
    storage2 = SQLiteStorage(test_db)
    state2 = await storage2.get_state(key)
    data2 = await storage2.get_data(key)

    # State and data must be fully preserved from SQLite!
    assert state2 == DemoStates.step_one.state
    assert data2["source_channel"] == "@my_source"
    assert data2["mode"] == "clean"

    # 4. Transition to next state and clear
    await storage2.set_state(key, DemoStates.step_two)
    assert await storage2.get_state(key) == DemoStates.step_two.state

    await storage2.set_state(key, None)
    assert await storage2.get_state(key) is None

@pytest.mark.asyncio
async def test_history_cancel_authorization_idor_prevention(test_db):
    """
    Verify cb_cancel_history_clone rejects unauthorized users trying to cancel someone else's task.
    """
    # Create user 111 first to satisfy foreign key constraint
    await test_db.get_or_create_user(111, "Legit Owner", "legit_owner")

    # Create pair owned by user 111
    pair_id = await test_db.add_channel_pair(
        user_id=111,
        source_channel="@src_chan",
        source_title="Source",
        target_channel="@tgt_chan",
        target_title="Target"
    )

    # 1. Attacker (user 222) attempts to cancel user 111's history clone
    mock_cb_attacker = AsyncMock(spec=CallbackQuery)
    mock_cb_attacker.data = f"hist_cancel_{pair_id}"
    mock_cb_attacker.from_user = TgUser(id=222, is_bot=False, first_name="Attacker")
    mock_cb_attacker.answer = AsyncMock()
    mock_cb_attacker.message = AsyncMock()

    with patch("bot.handlers.history_clone.db_manager", test_db), \
         patch("services.telethon_listener.telethon_listener.cancel_history_clone") as mock_cancel:
        await cb_cancel_history_clone(mock_cb_attacker)
        # Must NOT call telethon cancel
        mock_cancel.assert_not_called()
        # Must alert unauthorized
        mock_cb_attacker.answer.assert_called_with(text="Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)

    # 2. Legitimate owner (user 111) attempts to cancel
    mock_cb_owner = AsyncMock(spec=CallbackQuery)
    mock_cb_owner.data = f"hist_cancel_{pair_id}"
    mock_cb_owner.from_user = TgUser(id=111, is_bot=False, first_name="Owner")
    mock_cb_owner.answer = AsyncMock()
    mock_cb_owner.message = AsyncMock()

    with patch("bot.handlers.history_clone.db_manager", test_db), \
         patch("services.telethon_listener.telethon_listener.cancel_history_clone", return_value=True) as mock_cancel:
        await cb_cancel_history_clone(mock_cb_owner)
        # Must call telethon cancel for owner
        mock_cancel.assert_called_once_with(pair_id)

@pytest.mark.asyncio
async def test_payment_idempotency_constraint(test_db):
    """
    Verify payment activation is strictly idempotent under duplicate charge_id.
    """
    user_id = 998877
    await test_db.get_or_create_user(user_id, "Paid User", "paid_user")

    charge_id = "test_stars_unique_charge_999"
    # First activation
    sub1 = await test_db.activate_subscription(user_id, "pro", 100, charge_id, days=30)
    assert sub1.tier == "pro"
    assert sub1.stars_spent == 100

    # Second duplicate activation with same charge_id (e.g. retry from Telegram)
    sub2 = await test_db.activate_subscription(user_id, "pro", 100, charge_id, days=30)
    assert sub2.tier == "pro"
    assert sub2.stars_spent == 100  # Not double charged!
    assert sub2.expires_at == sub1.expires_at  # Expiry date not inflated!

@pytest.mark.asyncio
async def test_user_lifecycle_blocked_and_unblocked(test_db):
    """
    Verify users blocking the bot are excluded from broadcasts and unblocked on next interaction.
    """
    # Register two users
    await test_db.get_or_create_user(1001, "Active User", "active")
    await test_db.get_or_create_user(1002, "Blocked User", "blocked")

    all_users = await test_db.get_all_users(active_only=True)
    user_ids = [u.user_id for u in all_users]
    assert 1001 in user_ids
    assert 1002 in user_ids

    # Mark user 1002 as blocked (e.g. after TelegramForbiddenError)
    await test_db.mark_user_blocked(1002, True)

    active_users = await test_db.get_all_users(active_only=True)
    active_ids = [u.user_id for u in active_users]
    assert 1001 in active_ids
    assert 1002 not in active_ids

    # User 1002 is unblocked and interacts with bot again
    await test_db.mark_user_blocked(1002, False)
    await test_db.get_or_create_user(1002, "Blocked User", "blocked")
    restored_users = await test_db.get_all_users(active_only=True)
    restored_ids = [u.user_id for u in restored_users]
    assert 1001 in restored_ids
    assert 1002 in restored_ids

@pytest.mark.asyncio
async def test_broadcast_concurrency_lock():
    """
    A confirmed broadcast cannot start while another one is running; the draft is kept so the admin can
    confirm it again later.
    """
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage
    from admin_bot.handlers.broadcast import cb_broadcast_confirm

    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(BroadcastStates.waiting_for_confirm)
    await state.update_data(broadcast_draft={"kind": "text", "text": "E'lon matni"})

    callback = AsyncMock(spec=CallbackQuery)
    callback.from_user = TgUser(id=1, is_bot=False, first_name="Admin")
    callback.answer = AsyncMock()
    bot = AsyncMock()

    with patch("admin_bot.handlers.broadcast.ensure_super_admin", new=AsyncMock(return_value=True)):
        async with _broadcast_lock:
            await cb_broadcast_confirm(callback, state, bot)

    callback.answer.assert_called_once()
    assert "ketmoqda" in callback.answer.call_args.kwargs["text"]
    assert callback.answer.call_args.kwargs["show_alert"] is True
    bot.send_message.assert_not_called()
    assert await state.get_state() == BroadcastStates.waiting_for_confirm.state
    assert (await state.get_data())["broadcast_draft"]["text"] == "E'lon matni"
