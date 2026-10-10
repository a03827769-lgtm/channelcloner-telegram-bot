from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.handlers import channel_membership as cm
from database.models import ChannelPair

CHANNEL_ID = -1001234567890


def _chat(username="mychan"):
    return SimpleNamespace(id=CHANNEL_ID, type="channel", title="My <Chan>", username=username)


def _user(user_id=5912577878):
    return SimpleNamespace(id=user_id, is_bot=False, full_name="Owner", username="owner")


def _event(old, new, *, can_post=True, user=None):
    return SimpleNamespace(
        chat=_chat(),
        from_user=user or _user(),
        old_chat_member=SimpleNamespace(status=old),
        new_chat_member=SimpleNamespace(status=new, can_post_messages=can_post),
    )


@pytest.fixture
def admins(monkeypatch):
    monkeypatch.setattr(cm, "settings", SimpleNamespace(admin_ids={111, 222}))


def _forbidden():
    return TelegramForbiddenError(
        method=SendMessage(chat_id=1, text="x"),
        message="Forbidden: bot can't initiate conversation with a user",
    )


async def test_promotion_tells_the_owner_how_to_connect_the_channel(admins):
    bot = SimpleNamespace(send_message=AsyncMock())

    await cm.on_bot_channel_status(_event("left", "administrator"), bot)

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 5912577878
    assert "Yangi kanal ulash" in kwargs["text"] and "My &lt;Chan&gt;" in kwargs["text"]
    assert "Post messages" not in kwargs["text"]


async def test_promotion_without_post_right_warns_the_owner(admins):
    bot = SimpleNamespace(send_message=AsyncMock())

    await cm.on_bot_channel_status(_event("left", "administrator", can_post=False), bot)

    assert "Post messages" in bot.send_message.await_args.kwargs["text"]


async def test_owner_who_never_started_the_bot_is_reported_to_admins(admins):
    async def send_message(chat_id, text, parse_mode):
        if chat_id == 5912577878:
            raise _forbidden()

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))

    await cm.on_bot_channel_status(_event("left", "administrator"), bot)

    targets = [c.kwargs["chat_id"] for c in bot.send_message.await_args_list]
    assert targets == [5912577878, 111, 222]
    alert = bot.send_message.await_args_list[-1].kwargs["text"]
    assert "<code>5912577878</code>" in alert and f"<code>{CHANNEL_ID}</code>" in alert and "@mychan" in alert


async def test_rights_change_while_already_admin_is_ignored(admins):
    bot = SimpleNamespace(send_message=AsyncMock())
    await cm.on_bot_channel_status(_event("administrator", "administrator"), bot)
    bot.send_message.assert_not_awaited()


async def test_losing_admin_rights_notifies_owners_of_affected_pairs(admins, monkeypatch):
    pairs = [
        ChannelPair(id=1, user_id=10, target_channel=str(CHANNEL_ID)),
        ChannelPair(id=2, user_id=10, target_channel="@MyChan"),
        ChannelPair(id=3, user_id=20, target_channel="other", target_id=CHANNEL_ID),
        ChannelPair(id=4, user_id=30, target_channel="@unrelated"),
    ]
    monkeypatch.setattr(cm, "db_manager", SimpleNamespace(get_all_active_pairs=AsyncMock(return_value=pairs)))
    bot = SimpleNamespace(send_message=AsyncMock())

    await cm.on_bot_channel_status(_event("administrator", "kicked"), bot)

    sent = {c.kwargs["chat_id"]: c.kwargs["text"] for c in bot.send_message.await_args_list}
    assert set(sent) == {10, 20}
    assert "#1, #2" in sent[10] and "#3" in sent[20]


def test_main_dispatcher_receives_my_chat_member_updates():
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.bot_instance import create_dispatcher

    dp = create_dispatcher(storage=MemoryStorage())
    assert "my_chat_member" in dp.resolve_used_update_types()
