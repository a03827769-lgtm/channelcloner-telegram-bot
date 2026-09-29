import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import Message, CallbackQuery, User as AiogramUser, Chat
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from aiogram import Bot

from config.settings import settings
from admin_bot.bot_instance import create_admin_router
from admin_bot.handlers.dashboard import (
    cmd_admin_start, cb_admin_dashboard, cb_restart_listener,
    cmd_admin_catchup, cmd_admin_help, cmd_admin_cancel, cb_admin_noop
)
from admin_bot.handlers.broadcast import (
    BroadcastStates, process_broadcast_message
)
from admin_bot.handlers.user_management import (
    AdminUserSG, process_user_search_query
)
from admin_bot.handlers.access_control import (
    AccessControlStates, process_support_user_input, process_whitelist_user_input
)
from admin_bot.handlers.mtproto_auth import (
    AuthStates, process_phone_input, process_code_input, process_2fa_input
)

SUPER_ADMIN_ID = 8881989487


@pytest.fixture(autouse=True)
def super_admin_configured():
    """The handlers under test are super-admin-only: the test user is the configured super admin."""
    with patch.object(settings, "ADMIN_IDS_RAW", str(SUPER_ADMIN_ID)), \
         patch.object(settings, "PRIMARY_SUPER_ADMIN_ID", 0):
        yield


@pytest.fixture
def memory_storage():
    return MemoryStorage()

@pytest.fixture
def fsm_context(memory_storage):
    bot = MagicMock(spec=Bot)
    bot.id = 12345
    key = StorageKey(bot_id=bot.id, chat_id=8881989487, user_id=8881989487)
    return FSMContext(storage=memory_storage, key=key)

def create_mock_message(user_id: int = 8881989487, text: str = "") -> Message:
    msg = AsyncMock(spec=Message)
    msg.message_id = 999
    msg.from_user = AiogramUser(id=user_id, is_bot=False, first_name="SuperAdmin", username="superadmin")
    msg.chat = Chat(id=user_id, type="private")
    msg.text = text
    msg.answer = AsyncMock()
    msg.delete = AsyncMock()
    return msg

def test_admin_router_has_all_expected_routes():
    router = create_admin_router(name="test_admin_router", is_dedicated=True)
    
    # Verify message handlers are registered
    message_handlers = router.message.handlers
    self_registered_funcs = [h.callback for h in message_handlers]
    
    assert cmd_admin_start in self_registered_funcs
    assert cmd_admin_help in self_registered_funcs
    assert cmd_admin_cancel in self_registered_funcs
    assert cmd_admin_catchup in self_registered_funcs
    assert process_broadcast_message in self_registered_funcs
    assert process_user_search_query in self_registered_funcs
    assert process_support_user_input in self_registered_funcs
    assert process_whitelist_user_input in self_registered_funcs
    assert process_phone_input in self_registered_funcs
    assert process_code_input in self_registered_funcs
    assert process_2fa_input in self_registered_funcs

    # Verify callback query handlers are registered
    callback_handlers = router.callback_query.handlers
    cb_funcs = [h.callback for h in callback_handlers]
    
    assert cb_admin_dashboard in cb_funcs
    assert cb_restart_listener in cb_funcs
    assert cb_admin_noop in cb_funcs

@pytest.mark.asyncio
async def test_broadcast_cancellation(fsm_context):
    await fsm_context.set_state(BroadcastStates.waiting_for_message)
    assert await fsm_context.get_state() == BroadcastStates.waiting_for_message.state
    
    msg = create_mock_message(8881989487, "/cancel")
    bot = MagicMock(spec=Bot)

    await process_broadcast_message(msg, fsm_context, bot)
    assert await fsm_context.get_state() is None
    msg.answer.assert_called_once()
    assert "bekor qilindi" in msg.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_user_search_cancellation(fsm_context):
    await fsm_context.set_state(AdminUserSG.waiting_for_user_query)
    assert await fsm_context.get_state() == AdminUserSG.waiting_for_user_query.state
    
    msg = create_mock_message(8881989487, "bekor qilish")

    await process_user_search_query(msg, fsm_context)
    assert await fsm_context.get_state() is None
    msg.answer.assert_called_once()
    assert "bekor qilindi" in msg.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_access_control_support_cancellation(fsm_context):
    await fsm_context.set_state(AccessControlStates.waiting_for_support_user)
    
    msg = create_mock_message(8881989487, "/cancel")

    await process_support_user_input(msg, fsm_context)
    assert await fsm_context.get_state() is None
    msg.answer.assert_called_once()
    assert "bekor qilindi" in msg.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_access_control_whitelist_cancellation(fsm_context):
    await fsm_context.set_state(AccessControlStates.waiting_for_whitelist_user)
    
    msg = create_mock_message(8881989487, "bekor qilish")

    await process_whitelist_user_input(msg, fsm_context)
    assert await fsm_context.get_state() is None
    msg.answer.assert_called_once()
    assert "bekor qilindi" in msg.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_auth_states_cancellation(fsm_context):
    # Phone
    await fsm_context.set_state(AuthStates.waiting_for_phone)
    msg_phone = create_mock_message(8881989487, "/cancel")
    await process_phone_input(msg_phone, fsm_context)
    assert await fsm_context.get_state() is None
    assert "bekor qilindi" in msg_phone.answer.call_args[0][0]
    
    # Code
    await fsm_context.set_state(AuthStates.waiting_for_code)
    msg_code = create_mock_message(8881989487, "/cancel")
    await process_code_input(msg_code, fsm_context)
    assert await fsm_context.get_state() is None
    assert "bekor qilindi" in msg_code.answer.call_args[0][0]

    # 2FA
    await fsm_context.set_state(AuthStates.waiting_for_2fa)
    msg_2fa = create_mock_message(8881989487, "/cancel")
    await process_2fa_input(msg_2fa, fsm_context)
    assert await fsm_context.get_state() is None
    assert "bekor qilindi" in msg_2fa.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_cb_admin_noop():
    cb = AsyncMock(spec=CallbackQuery)
    cb.answer = AsyncMock()
    await cb_admin_noop(cb)
    cb.answer.assert_called_once()

@pytest.mark.asyncio
async def test_cmd_admin_cancel_and_help(fsm_context):
    # Test cancel when state is active
    await fsm_context.set_state(BroadcastStates.waiting_for_message)
    msg_cancel = create_mock_message(8881989487, "/cancel")
    await cmd_admin_cancel(msg_cancel, fsm_context)
    assert await fsm_context.get_state() is None
    assert "bekor qilindi" in msg_cancel.answer.call_args[0][0]

    # Test help
    msg_help = create_mock_message(8881989487, "/help")
    await cmd_admin_help(msg_help, fsm_context)
    msg_help.answer.assert_called_once()
    assert "/admin" in msg_help.answer.call_args[1]["text"]
    assert "/catchup" in msg_help.answer.call_args[1]["text"]
    assert "/check_origin" in msg_help.answer.call_args[1]["text"]
