"""
Regression tests for the admin bot rework: private-chat only routing, wizard inputs that cannot be
hijacked by menu buttons, super-admin-only operations and the broadcast confirmation step.
Updates are fed through a real aiogram Dispatcher; the Bot uses an in-memory session (no network).
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, List
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from config.settings import settings
from database.db_manager import db_manager

SUPER_ADMIN_ID = 111000111
DELEGATED_ADMIN_ID = 222000222
ADMIN_BOT_TOKEN = "987654321:TEST_ADMIN_BOT_TOKEN_FOR_UNIT_TESTS"


class RecordingSession(BaseSession):
    """Answers every Bot API call locally and records it."""

    def __init__(self):
        super().__init__()
        self.calls: List[TelegramMethod] = []
        self._next_id = 1000

    async def close(self):
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - not used
        if False:
            yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: Any = None):
        self.calls.append(method)
        name = type(method).__name__
        if name in ("SendMessage", "EditMessageText", "SendDocument", "CopyMessage", "SendPhoto"):
            self._next_id += 1
            chat_id = getattr(method, "chat_id", None) or 1
            return Message(
                message_id=self._next_id,
                date=datetime.now(timezone.utc),
                chat=Chat(id=chat_id, type="private"),
                text=getattr(method, "text", None),
            ) if name != "CopyMessage" else {"message_id": self._next_id}
        return True

    def names(self) -> List[str]:
        return [type(c).__name__ for c in self.calls]


def _user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name="Admin")


def _message_update(uid: int, text: str, chat_type: str = "private", update_id: int = 1) -> Update:
    chat = Chat(id=uid if chat_type == "private" else -100555, type=chat_type)
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(timezone.utc), chat=chat, from_user=_user(uid), text=text
    ))


def _callback_update(uid: int, data: str, update_id: int = 1) -> Update:
    msg = Message(message_id=50, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"), text="menu")
    return Update(update_id=update_id, callback_query=CallbackQuery(
        id=str(update_id), from_user=_user(uid), chat_instance="ci", data=data, message=msg
    ))


@pytest.fixture
def admin_env():
    with patch.object(settings, "ADMIN_IDS_RAW", str(SUPER_ADMIN_ID)), \
         patch.object(settings, "PRIMARY_SUPER_ADMIN_ID", 0), \
         patch.object(settings, "ADMIN_BOT_TOKEN", ADMIN_BOT_TOKEN):
        yield


@pytest.fixture
def dispatcher_and_bot(admin_env):
    from admin_bot.bot_instance import create_admin_dispatcher
    session = RecordingSession()
    bot = Bot(token=ADMIN_BOT_TOKEN, session=session)
    dp = create_admin_dispatcher(storage=MemoryStorage())
    return dp, bot, session


async def test_admin_commands_are_ignored_in_groups(dispatcher_and_bot):
    dp, bot, session = dispatcher_and_bot
    with patch("admin_bot.handlers.user_management.db_manager.get_users_detailed_page", new=AsyncMock(return_value=([], 0))) as page:
        await dp.feed_update(bot, _message_update(SUPER_ADMIN_ID, "/users", chat_type="supergroup"))
    page.assert_not_awaited()
    assert session.calls == []


async def test_menu_button_in_broadcast_state_is_not_broadcast(dispatcher_and_bot):
    from admin_bot.handlers.broadcast import BroadcastStates
    dp, bot, session = dispatcher_and_bot
    state = dp.fsm.get_context(bot=bot, chat_id=SUPER_ADMIN_ID, user_id=SUPER_ADMIN_ID)
    await state.set_state(BroadcastStates.waiting_for_message)

    with patch("admin_bot.handlers.broadcast.db_manager.get_broadcast_recipient_ids", new=AsyncMock(return_value=[1, 2])) as recipients, \
         patch("admin_bot.handlers.user_management.db_manager.get_users_detailed_page", new=AsyncMock(return_value=([], 0))):
        await dp.feed_update(bot, _message_update(SUPER_ADMIN_ID, "Foydalanuvchilar"))

    recipients.assert_not_awaited()
    assert await state.get_state() is None  # navigation cleared the wizard


async def test_broadcast_requires_confirmation_then_sends(dispatcher_and_bot):
    from admin_bot.handlers import broadcast as bc
    dp, bot, session = dispatcher_and_bot
    public_session = RecordingSession()
    public_bot = Bot(token="123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS", session=public_session)
    state = dp.fsm.get_context(bot=bot, chat_id=SUPER_ADMIN_ID, user_id=SUPER_ADMIN_ID)
    await state.set_state(bc.BroadcastStates.waiting_for_message)

    with patch("admin_bot.handlers.broadcast.db_manager.get_broadcast_recipient_ids", new=AsyncMock(return_value=[501, 502, 503])), \
         patch("admin_bot.handlers.broadcast.get_public_bot", return_value=public_bot), \
         patch.object(bc, "SEND_INTERVAL_SECONDS", 0):
        await dp.feed_update(bot, _message_update(SUPER_ADMIN_ID, "Yangilik: bot yangilandi", update_id=2))
        # Nothing was delivered yet — only a preview/confirmation for the admin
        assert public_session.calls == []
        assert await state.get_state() == bc.BroadcastStates.waiting_for_confirm.state

        await dp.feed_update(bot, _callback_update(SUPER_ADMIN_ID, "admin_broadcast_confirm", update_id=3))
        for _ in range(50):
            if not bc._running_tasks:
                break
            await asyncio.sleep(0.02)

    delivered = [c for c in public_session.calls if type(c).__name__ == "SendMessage"]
    assert sorted(c.chat_id for c in delivered) == [501, 502, 503]
    assert all(c.text == "Yangilik: bot yangilandi" for c in delivered)


async def test_delegated_admin_cannot_download_backup(dispatcher_and_bot):
    dp, bot, session = dispatcher_and_bot
    await db_manager.set_admin_status(DELEGATED_ADMIN_ID, True)
    try:
        with patch("admin_bot.handlers.backup.db_manager.create_backup_file", new=AsyncMock()) as backup:
            await dp.feed_update(bot, _callback_update(DELEGATED_ADMIN_ID, "admin_download_backup"))
        backup.assert_not_awaited()
        answers = [c for c in session.calls if isinstance(c, AnswerCallbackQuery)]
        assert answers and "Super Admin" in (answers[0].text or "")
    finally:
        await db_manager.set_admin_status(DELEGATED_ADMIN_ID, False)


async def test_strangers_are_dropped_silently(dispatcher_and_bot):
    dp, bot, session = dispatcher_and_bot
    await dp.feed_update(bot, _message_update(333000333, "/start"))
    assert session.calls == []


async def test_grant_rejects_forged_payloads(dispatcher_and_bot):
    dp, bot, session = dispatcher_and_bot
    with patch("admin_bot.handlers.user_management.db_manager.activate_subscription", new=AsyncMock()) as activate:
        await dp.feed_update(bot, _callback_update(SUPER_ADMIN_ID, "adm_grant_555_vip_99999"))
        await dp.feed_update(bot, _callback_update(SUPER_ADMIN_ID, "adm_grant_555_gold_30", update_id=2))
    activate.assert_not_awaited()


async def test_support_username_validation(dispatcher_and_bot):
    from admin_bot.handlers.access_control import AccessControlStates
    dp, bot, session = dispatcher_and_bot
    state = dp.fsm.get_context(bot=bot, chat_id=SUPER_ADMIN_ID, user_id=SUPER_ADMIN_ID)
    await state.set_state(AccessControlStates.waiting_for_support_user)
    with patch("admin_bot.handlers.access_control.db_manager.set_support_username", new=AsyncMock()) as setter:
        await dp.feed_update(bot, _message_update(SUPER_ADMIN_ID, "ab<b>", update_id=5))
        setter.assert_not_awaited()
        assert await state.get_state() == AccessControlStates.waiting_for_support_user.state
        await dp.feed_update(bot, _message_update(SUPER_ADMIN_ID, "@support_team", update_id=6))
        setter.assert_awaited_once_with("support_team")
