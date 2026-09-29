"""
Regression tests for the public bot UI fixes (area F4): destination ownership checks, safe restores,
tenant-scoped inline search, private-chat-only routing, wizard inputs that cannot be hijacked, plan
gating, payment robustness and HTML safety.

Updates are fed through the real aiogram Dispatcher built by create_dispatcher(); the Bot talks to an
in-memory session that answers every Bot API call locally (no network). The shared db_manager works
on the throw-away database configured in tests/conftest.py.
"""
import asyncio
import itertools
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery, AnswerInlineQuery, EditMessageText, SendInvoice, SendMessage, TelegramMethod
)
from aiogram.types import (
    CallbackQuery, Chat, ChatFullInfo, ChatMemberAdministrator, ChatMemberMember, ChatMemberOwner,
    InaccessibleMessage, InlineQuery, Message, MessageOriginChannel, MessageOriginUser,
    RefundedPayment, SuccessfulPayment, Update, User
)

from config.limits import BACKFILL_MAX_MESSAGES, BACKFILL_MAX_MESSAGES_PRIVILEGED, SIGNATURE_MAX_CHARS
from config.settings import settings
from database.db_manager import db_manager

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS"
BOT_ID = 123456789
ADMIN_ID = 910000001
NOW = datetime.now(timezone.utc)

_ids = itertools.count(1)


def _uid() -> int:
    """A fresh user id per test so rows in the shared test database never collide."""
    return 920000000 + next(_ids) * 97


def _channel_id() -> int:
    return -1009300000000 - next(_ids) * 31


# ------------------------------------------------------------------------------------------------
# Fake Bot API
# ------------------------------------------------------------------------------------------------

class FakeSession(BaseSession):
    """Answers every Bot API call locally and records it. `chats` maps a chat reference (id or
    @username) to the chat returned by getChat; `members` maps (chat_id, user_id) to a member status."""

    def __init__(self):
        super().__init__()
        self.calls: List[TelegramMethod] = []
        self.chats: Dict[Any, Any] = {}
        self.members: Dict[tuple, str] = {}
        self.handlers: Dict[str, Callable[[TelegramMethod], Any]] = {}
        self._next_id = 5000

    async def close(self):
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - not used
        if False:
            yield b""

    def add_channel(self, chat_id: int, title: str = "Kanal", username: Optional[str] = None,
                    chat_type: str = "channel", linked_chat_id: Optional[int] = None):
        chat = ChatFullInfo.model_construct(
            id=chat_id, type=chat_type, title=title, username=username, linked_chat_id=linked_chat_id
        )
        self.chats[chat_id] = chat
        if username:
            self.chats[f"@{username}"] = chat
        return chat

    def set_member(self, chat_id: int, user_id: int, status: str):
        self.members[(chat_id, user_id)] = status

    def _member(self, chat_id: Any, user_id: int):
        status = self.members.get((chat_id, user_id), "member")
        user = User(id=user_id, is_bot=user_id == BOT_ID, first_name="M")
        if status == "creator":
            return ChatMemberOwner(user=user, is_anonymous=False)
        if status == "administrator":
            return ChatMemberAdministrator.model_construct(
                status="administrator", user=user, can_post_messages=True, can_be_edited=False
            )
        return ChatMemberMember(user=user)

    def _message(self, chat_id: Any, text: Optional[str] = None) -> Message:
        self._next_id += 1
        cid = chat_id if isinstance(chat_id, int) else 1
        return Message(message_id=self._next_id, date=NOW, chat=Chat(id=cid, type="private"), text=text)

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: Any = None):
        self.calls.append(method)
        name = type(method).__name__
        if name in self.handlers:
            result = self.handlers[name](method)
            if isinstance(result, Exception):
                raise result
            return result
        if name == "GetMe":
            return User(id=BOT_ID, is_bot=True, first_name="Cloner", username="test_cloner_bot")
        if name == "GetChat":
            chat = self.chats.get(method.chat_id)
            if chat is None:
                raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
            return chat
        if name == "GetChatMember":
            return self._member(method.chat_id, method.user_id)
        if name in ("SendMessage", "EditMessageText", "SendPhoto", "SendVideo", "SendDocument", "SendInvoice"):
            # Like the real session, returned messages are bound to the bot (handlers edit them later)
            return self._message(getattr(method, "chat_id", None), getattr(method, "text", None)).as_(bot)
        if name == "SendMediaGroup":
            return [self._message(method.chat_id).as_(bot)]
        return True

    def of(self, method_type) -> list:
        return [c for c in self.calls if isinstance(c, method_type)]

    def texts(self) -> List[str]:
        return [c.text for c in self.calls if isinstance(c, (SendMessage, EditMessageText)) and c.text]


def _user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name="Test", username=f"u{uid}")


def _message(uid: int, text: Optional[str] = None, chat_type: str = "private", chat_id: Optional[int] = None,
             message_id: int = 1, **extra) -> Message:
    chat = Chat(id=chat_id if chat_id is not None else (uid if chat_type == "private" else -1005550000001),
                type=chat_type, title=None if chat_type == "private" else "Guruh")
    return Message(message_id=message_id, date=NOW, chat=chat, from_user=_user(uid), text=text, **extra)


_update_ids = itertools.count(1)


def msg_update(uid: int, text: Optional[str] = None, **kwargs) -> Update:
    upd_id = next(_update_ids)
    return Update(update_id=upd_id, message=_message(uid, text, message_id=upd_id, **kwargs))


def cb_update(uid: int, data: str, message: Optional[Any] = None) -> Update:
    upd_id = next(_update_ids)
    if message is None:
        message = Message(message_id=77, date=NOW, chat=Chat(id=uid, type="private"), text="menu")
    return Update(update_id=upd_id, callback_query=CallbackQuery(
        id=str(upd_id), from_user=_user(uid), chat_instance="ci", data=data, message=message
    ))


def _answers(session: FakeSession) -> List[AnswerCallbackQuery]:
    return session.of(AnswerCallbackQuery)


# ------------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------------

def _detach_public_routers():
    """Module-level routers can belong to one dispatcher only; detach them from earlier ones."""
    import bot.bot_instance as bi
    for router in (bi.start_router, bi.stars_billing_router, bi.cloner_menu_router, bi.settings_menu_router,
                   bi.history_clone_router, bi.help_guide_router, bi.story_menu_router,
                   bi.inline_search_router, bi.comment_moderator_router):
        parent = router.parent_router
        if parent is not None:
            if router in parent.sub_routers:
                parent.sub_routers.remove(router)
            router._parent_router = None


@pytest.fixture
def env():
    from services.telethon_listener import telethon_listener
    with patch.object(settings, "ADMIN_IDS_RAW", str(ADMIN_ID)), \
         patch.object(settings, "PRIMARY_SUPER_ADMIN_ID", 0), \
         patch.object(telethon_listener, "client", None), \
         patch.object(telethon_listener, "_is_running", False):
        yield


@pytest.fixture
async def bot_env(env):
    from bot.bot_instance import create_dispatcher
    from services.cache_manager import cache_manager
    await cache_manager.sub_cache.clear()
    await cache_manager.settings_cache.clear()
    _detach_public_routers()
    dp = create_dispatcher(storage=MemoryStorage())
    session = FakeSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    try:
        yield dp, bot, session
    finally:
        for router in list(dp.sub_routers):
            router._parent_router = None
        dp.sub_routers.clear()


async def _make_user(uid: int, tier: Optional[str] = None, trial_active: bool = True):
    await db_manager.get_or_create_user(uid, "Test User", f"u{uid}")
    await db_manager.get_user_subscription(uid)
    if tier in ("pro", "vip"):
        await db_manager.activate_subscription(uid, tier, 100, f"test_charge_{uid}_{tier}", days=30)
    if not trial_active:
        past = (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None).isoformat()
        async with db_manager.write_transaction() as db:
            await db.execute("UPDATE subscriptions SET trial_expires_at = ? WHERE user_id = ?", (past, uid))
    from services.cache_manager import cache_manager
    await cache_manager.sub_cache.delete(f"sub_{uid}")


async def _make_pair(uid: int, source: str = "@src_chan", target: str = "@tgt_chan",
                     target_id: Optional[int] = None, **fields) -> int:
    pair_id = await db_manager.add_channel_pair(
        user_id=uid, source_channel=source, source_title="Manba", target_channel=target,
        target_title="Maqsad", target_id=target_id
    )
    if fields:
        await db_manager.update_pair_fields(pair_id, fields)
    return pair_id


async def _state(dp, bot, uid: int):
    return dp.fsm.get_context(bot=bot, chat_id=uid, user_id=uid)


# ------------------------------------------------------------------------------------------------
# B-C1 / cycles / B-L11 / B-M9 — add-pair wizard, target step
# ------------------------------------------------------------------------------------------------

async def _prepare_target_step(dp, bot, uid: int, source: str = "@some_source", source_id: Optional[int] = None):
    from bot.states.cloner_states import AddChannelPairSG
    state = await _state(dp, bot, uid)
    await state.set_state(AddChannelPairSG.waiting_for_target_channel)
    await state.update_data(source_channel=source, source_title="Manba", source_id=source_id)
    return state


async def test_target_owned_by_someone_else_is_rejected(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    victim = _channel_id()
    session.add_channel(victim, "Begona kanal", username=f"victim{abs(victim) % 100000}")
    session.set_member(victim, BOT_ID, "administrator")  # the bot is an admin there, the user is not
    state = await _prepare_target_step(dp, bot, uid)

    await dp.feed_update(bot, msg_update(uid, str(victim)))

    assert await db_manager.get_user_channel_pairs(uid) == []
    assert any("egasi yoki xabar joylash huquqiga ega administratori emassiz" in t for t in session.texts())
    assert await state.get_state() is not None  # the user may send another channel


async def test_target_owned_by_user_creates_pair(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    own = _channel_id()
    session.add_channel(own, "Mening <kanalim>")
    session.set_member(own, BOT_ID, "administrator")
    session.set_member(own, uid, "creator")
    state = await _prepare_target_step(dp, bot, uid, source="@legit_source_chan")

    await dp.feed_update(bot, msg_update(uid, str(own)))

    pairs = await db_manager.get_user_channel_pairs(uid)
    assert len(pairs) == 1 and pairs[0].target_id == own
    assert await state.get_state() is None
    success = [t for t in session.texts() if "muvaffaqiyatli ulandi" in t]
    assert success and "Mening &lt;kanalim&gt;" in success[0]


async def test_pair_closing_a_cycle_is_rejected(bot_env):
    dp, bot, session = bot_env
    other, uid = _uid(), _uid()
    await _make_user(other)
    await _make_user(uid)
    chan_a, chan_b = _channel_id(), _channel_id()
    await _make_pair(other, source=str(chan_b), target=str(chan_a), target_id=chan_a)  # B -> A exists
    session.add_channel(chan_b, "B")
    session.set_member(chan_b, BOT_ID, "administrator")
    session.set_member(chan_b, uid, "creator")
    await _prepare_target_step(dp, bot, uid, source=str(chan_a), source_id=chan_a)

    await dp.feed_update(bot, msg_update(uid, str(chan_b)))  # A -> B would close the loop

    assert await db_manager.get_user_channel_pairs(uid) == []
    assert any("halqasini (loop)" in t for t in session.texts())


async def test_plan_limit_is_rechecked_before_insert(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    own = _channel_id()
    session.add_channel(own, "Kanal")
    session.set_member(own, BOT_ID, "administrator")
    session.set_member(own, uid, "creator")
    await _prepare_target_step(dp, bot, uid, source="@limit_source_chan")

    with patch("bot.handlers.cloner_menu.db_manager.can_user_add_channel", new=AsyncMock(return_value=(False, 1, 1))):
        await dp.feed_update(bot, msg_update(uid, str(own)))

    assert await db_manager.get_user_channel_pairs(uid) == []
    assert any("Kanal Limiti Yetib Keldi" in t for t in session.texts())


class _FakeTelethonClient:
    """Records MTProto requests; CheckChatInviteRequest answers with `invite` (None: expired link)."""

    def __init__(self, invite: Any):
        self.invite = invite
        self.requests: List[Any] = []

    def is_connected(self):
        return True

    async def __call__(self, request):
        self.requests.append(request)
        if type(request).__name__ == "CheckChatInviteRequest":
            if self.invite is None:
                raise ValueError("INVITE_HASH_EXPIRED")
            return self.invite
        raise AssertionError(f"unexpected MTProto request {type(request).__name__}")


async def test_invite_links_are_only_checked_never_joined(bot_env):
    from telethon.tl.types import ChatInvite
    from services.telethon_listener import telethon_listener
    from bot.states.cloner_states import AddChannelPairSG
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    invite = ChatInvite(title="Yopiq manba", photo=None, participants_count=10, color=0, channel=True, broadcast=True)
    fake_client = _FakeTelethonClient(invite)
    state = await _state(dp, bot, uid)
    await state.set_state(AddChannelPairSG.waiting_for_source_channel)

    with patch.object(telethon_listener, "client", fake_client), \
         patch.object(telethon_listener, "_is_running", True):
        # SOURCE: a private invite the userbot cannot peek into is accepted (joined only with the pair)
        await dp.feed_update(bot, msg_update(uid, "https://t.me/+AbCdEfGhIjKlMn"))
        assert await state.get_state() == AddChannelPairSG.waiting_for_target_channel.state
        requests_after_source = len(fake_client.requests)
        # TARGET: invite links are refused outright (no MTProto request at all)
        await dp.feed_update(bot, msg_update(uid, "https://t.me/+ZyXwVuTsRqPo"))

    request_types = {type(r).__name__ for r in fake_client.requests}
    assert request_types == {"CheckChatInviteRequest"}  # never ImportChatInviteRequest (joining)
    assert len(fake_client.requests) == requests_after_source
    assert await state.get_state() == AddChannelPairSG.waiting_for_target_channel.state
    assert any("taklif havolasi qabul qilinmaydi" in t for t in session.texts())
    assert (await state.get_data())["source_title"] == "Yopiq manba"


async def test_expired_invite_source_is_still_accepted(bot_env):
    from services.telethon_listener import telethon_listener
    from bot.states.cloner_states import AddChannelPairSG
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    state = await _state(dp, bot, uid)
    await state.set_state(AddChannelPairSG.waiting_for_source_channel)
    with patch.object(telethon_listener, "client", _FakeTelethonClient(None)), \
         patch.object(telethon_listener, "_is_running", True):
        await dp.feed_update(bot, msg_update(uid, "t.me/joinchat/AbCdEfGhIjKlMn"))
    assert await state.get_state() == AddChannelPairSG.waiting_for_target_channel.state


# ------------------------------------------------------------------------------------------------
# B-M7 / B-M12 / B-M1 / B-L14 — add-pair wizard, source step and input hijacking
# ------------------------------------------------------------------------------------------------

async def test_garbage_source_is_rejected_valid_source_is_escaped(bot_env):
    from bot.states.cloner_states import AddChannelPairSG
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    state = await _state(dp, bot, uid)
    await state.set_state(AddChannelPairSG.waiting_for_source_channel)

    await dp.feed_update(bot, msg_update(uid, "salom dunyo"))
    assert await state.get_state() == AddChannelPairSG.waiting_for_source_channel.state
    assert any("Noto'g'ri kanal manzili" in t for t in session.texts())

    forwarded = MessageOriginChannel(
        date=NOW, message_id=9, chat=Chat(id=-1001234567890, type="channel", title="<b>Yangiliklar</b>", username="yangiliklar_uz")
    )
    await dp.feed_update(bot, msg_update(uid, "post matni", forward_origin=forwarded))
    assert await state.get_state() == AddChannelPairSG.waiting_for_target_channel.state
    accepted = [t for t in session.texts() if "Manba kanal qabul qilindi" in t][-1]
    assert "&lt;b&gt;Yangiliklar&lt;/b&gt;" in accepted and "keyinroq" not in accepted.lower()
    assert "tekshirilmadi" in accepted  # MTProto is offline in tests: the user is told it is verified later


async def test_forward_from_a_person_is_not_a_source(bot_env):
    from bot.states.cloner_states import AddChannelPairSG
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    state = await _state(dp, bot, uid)
    await state.set_state(AddChannelPairSG.waiting_for_source_channel)
    origin = MessageOriginUser(date=NOW, sender_user=User(id=5, is_bot=False, first_name="Ali"))
    await dp.feed_update(bot, msg_update(uid, "salom", forward_origin=origin))
    assert await state.get_state() == AddChannelPairSG.waiting_for_source_channel.state
    assert any("kanaldan emas" in t for t in session.texts())


@pytest.mark.parametrize("text", ["Qo'llanma", "/help"])
async def test_menu_button_or_command_inside_wizard_navigates(bot_env, text):
    from bot.states.cloner_states import AddChannelPairSG
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    state = await _state(dp, bot, uid)
    await state.set_state(AddChannelPairSG.waiting_for_source_channel)

    await dp.feed_update(bot, msg_update(uid, text))

    assert await state.get_state() is None
    assert any("To'liq Qo'llanma" in t for t in session.texts())
    assert not any("Noto'g'ri kanal manzili" in t or "Kanal aniqlanmadi" in t for t in session.texts())


async def test_forwarded_post_mentioning_a_menu_label_is_not_a_menu_tap(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    origin = MessageOriginChannel(date=NOW, message_id=3, chat=Chat(id=-1001111111111, type="channel", title="X"))
    with patch("bot.handlers.start.db_manager.get_user_stats", new=AsyncMock()) as stats:
        await dp.feed_update(bot, msg_update(uid, "Mening Statistikam", forward_origin=origin))
        await dp.feed_update(bot, msg_update(uid, "Bugungi statistika: Mening Statistikam zo'r"))
        stats.assert_not_awaited()
        await dp.feed_update(bot, msg_update(uid, "Mening Statistikam"))
        stats.assert_awaited_once()


async def test_album_parts_do_not_answer_the_next_step(bot_env):
    from bot.states.cloner_states import AddChannelPairSG
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    state = await _state(dp, bot, uid)
    await state.set_state(AddChannelPairSG.waiting_for_source_channel)
    origin = MessageOriginChannel(date=NOW, message_id=4, chat=Chat(id=-1002222222222, type="channel", title="Albom", username="albom_kanal"))

    for _ in range(3):
        await dp.feed_update(bot, msg_update(uid, None, forward_origin=origin, media_group_id="grp-1", caption="rasm"))

    assert await state.get_state() == AddChannelPairSG.waiting_for_target_channel.state
    assert len([t for t in session.texts() if "Manba kanal qabul qilindi" in t]) == 1
    assert not any("bir xil bo'lishi mumkin emas" in t for t in session.texts())


# ------------------------------------------------------------------------------------------------
# B-H2 — private-chat-only routing and middlewares
# ------------------------------------------------------------------------------------------------

async def test_group_messages_do_not_reach_private_ui_or_register_users(bot_env):
    dp, bot, session = bot_env
    stranger = _uid()
    for text in ("Mening Statistikam", "/start", "Kanal Kloner", "/stars"):
        await dp.feed_update(bot, msg_update(stranger, text, chat_type="supergroup"))
    assert session.of(SendMessage) == []
    assert await db_manager.get_user_by_id(stranger) is None


async def test_private_mode_prompt_not_posted_in_groups_and_rate_limited(bot_env):
    dp, bot, session = bot_env
    newcomer = _uid()
    await db_manager.set_private_mode(True)
    try:
        await dp.feed_update(bot, msg_update(newcomer, "salom", chat_type="supergroup"))
        assert session.of(SendMessage) == []
        await dp.feed_update(bot, msg_update(newcomer, "/start"))
        await dp.feed_update(bot, msg_update(newcomer, "/start"))
        prompts = [t for t in session.texts() if "Yopiq" in t]
        assert len(prompts) == 1
    finally:
        await db_manager.set_private_mode(False)


async def test_successful_payment_passes_private_mode_and_throttling(bot_env):
    dp, bot, session = bot_env
    payer = _uid()
    await db_manager.set_private_mode(True)
    try:
        with patch("bot.handlers.stars_billing.db_manager.activate_subscription", new=AsyncMock(
                return_value=SimpleNamespace(tier="pro", expires_at="2099-01-01T00:00:00", trial_expires_at=None))) as activate:
            for i in range(8):  # a burst far above the throttle limit
                payment = SuccessfulPayment(
                    currency="XTR", total_amount=100, invoice_payload=json.dumps({"t": "pro", "u": payer}),
                    telegram_payment_charge_id=f"burst_{payer}_{i}", provider_payment_charge_id=""
                )
                await dp.feed_update(bot, msg_update(payer, None, successful_payment=payment))
        assert activate.await_count == 8
    finally:
        await db_manager.set_private_mode(False)


# ------------------------------------------------------------------------------------------------
# B-C2 — disaster recovery
# ------------------------------------------------------------------------------------------------

async def _archive(pair_id: int, count: int):
    from services.disaster_recovery import disaster_recovery_service
    for i in range(1, count + 1):
        await disaster_recovery_service.archive_message(pair_id, None, i, text=f"Post #{i}")


@pytest.fixture
def fast_restore():
    from services.disaster_recovery import DisasterRecoveryService
    with patch.object(DisasterRecoveryService, "SEND_INTERVAL_SECONDS", 0), \
         patch.object(DisasterRecoveryService, "PAGE_SIZE", 3):
        yield


async def test_restore_requires_requester_owning_the_destination(env, fast_restore):
    from services.disaster_recovery import DisasterRecoveryService
    service = DisasterRecoveryService()
    session = FakeSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@restore_src_a", target="@restore_tgt_a")
    await _archive(pair_id, 2)
    dest = _channel_id()
    session.add_channel(dest, "Dest")
    session.set_member(dest, BOT_ID, "administrator")

    assert (await service.restore_channel(bot, pair_id, str(dest)))["error"] == "requester_required"
    assert (await service.restore_channel(bot, pair_id, str(dest), requester_id=uid))["error"] == "user_not_admin"
    stranger = _uid()
    assert (await service.restore_channel(bot, pair_id, str(dest), requester_id=stranger))["error"] == "forbidden"
    group = _channel_id()
    session.add_channel(group, "Oddiy guruh", chat_type="group")
    assert (await service.restore_channel(bot, pair_id, str(group), requester_id=uid))["error"] == "wrong_type"
    assert session.of(SendMessage) == []


async def test_restore_streams_pages_in_order_and_survives_flood_wait(env, fast_restore):
    from services.disaster_recovery import DisasterRecoveryService
    service = DisasterRecoveryService()
    session = FakeSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@restore_src_b", target="@restore_tgt_b")
    await _archive(pair_id, 7)  # more than two pages of 3
    dest = _channel_id()
    session.add_channel(dest, "Dest")
    session.set_member(dest, BOT_ID, "administrator")
    session.set_member(dest, uid, "creator")

    flood = {"raised": False}

    def send_message(method):
        if not flood["raised"]:
            flood["raised"] = True
            return TelegramRetryAfter(method=method, message="Flood", retry_after=0)
        return session._message(method.chat_id, method.text)

    session.handlers["SendMessage"] = send_message
    progress: List[tuple] = []

    async def on_progress(done, total, status):
        progress.append((done, total, status))

    real_get_backups = db_manager.get_channel_backups
    page_requests: List[tuple] = []

    async def recording_get_backups(pair, limit=None, after_message_id=None):
        page_requests.append((limit, after_message_id))
        return await real_get_backups(pair, limit=limit, after_message_id=after_message_id)

    with patch("services.disaster_recovery.asyncio.sleep", new=AsyncMock()), \
         patch("services.disaster_recovery.db_manager.get_channel_backups", new=recording_get_backups):
        res = await service.restore_channel(bot, pair_id, str(dest), requester_id=uid, progress_callback=on_progress)

    assert res == {"total_archived": 7, "restored": 7, "failed": 0}
    sent = [c.text for c in session.of(SendMessage)]
    assert sent[-7:] == [f"Post #{i}" for i in range(1, 8)]
    assert progress[-1] == (7, 7, "completed")
    # The archive is streamed in pages of PAGE_SIZE, each page continuing after the previous one
    assert len(page_requests) == 3
    assert all(limit == 3 for limit, _ in page_requests)
    assert page_requests[0][1] is None
    assert page_requests[1][1] < page_requests[2][1]


async def test_only_one_restore_per_pair(env, fast_restore):
    from services.disaster_recovery import DisasterRecoveryService
    service = DisasterRecoveryService()
    session = FakeSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@restore_src_c", target="@restore_tgt_c")
    await _archive(pair_id, 2)
    dest = _channel_id()
    session.add_channel(dest, "Dest")
    session.set_member(dest, BOT_ID, "administrator")
    session.set_member(dest, uid, "creator")

    release = asyncio.Event()

    async def slow_send(*args, **kwargs):
        await release.wait()
        return True

    with patch.object(service, "_safe_send", new=slow_send):
        first = asyncio.create_task(service.restore_channel(bot, pair_id, str(dest), requester_id=uid))
        for _ in range(50):
            if service.is_restore_running(pair_id):
                break
            await asyncio.sleep(0.01)
        second = await service.restore_channel(bot, pair_id, str(dest), requester_id=uid)
        release.set()
        first_result = await first
    assert second["error"] == "already_running"
    assert first_result["restored"] == 2 and not service.is_restore_running(pair_id)


async def test_restore_handler_requires_destination_ownership(bot_env):
    from bot.states.cloner_states import EditSettingsSG
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@restore_src_d", target="@restore_tgt_d")
    dest = _channel_id()
    session.add_channel(dest, "Begona")
    session.set_member(dest, BOT_ID, "administrator")
    state = await _state(dp, bot, uid)
    await state.set_state(EditSettingsSG.waiting_for_restore_target)
    await state.update_data(pair_id=pair_id)

    with patch("bot.handlers.settings_menu.disaster_recovery_service.restore_channel", new=AsyncMock()) as restore:
        await dp.feed_update(bot, msg_update(uid, str(dest)))
    restore.assert_not_awaited()
    assert any("egasi yoki xabar joylash huquqiga ega administratori emassiz" in t for t in session.texts())


async def test_restore_cancel_button_clears_state(bot_env):
    from bot.states.cloner_states import EditSettingsSG
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@restore_src_e", target="@restore_tgt_e")
    state = await _state(dp, bot, uid)
    await state.set_state(EditSettingsSG.waiting_for_restore_target)
    await state.update_data(pair_id=pair_id)
    await dp.feed_update(bot, cb_update(uid, f"pair_backup_menu_{pair_id}"))
    assert await state.get_state() is None

    await state.set_state(EditSettingsSG.waiting_for_vwm_text)
    await dp.feed_update(bot, cb_update(uid, f"pair_vwm_menu_{pair_id}"))
    assert await state.get_state() is None


# ------------------------------------------------------------------------------------------------
# B-H1 — inline search
# ------------------------------------------------------------------------------------------------

def _inline_update(uid: int, query: str) -> Update:
    upd_id = next(_update_ids)
    return Update(update_id=upd_id, inline_query=InlineQuery(id=str(upd_id), from_user=_user(uid), query=query, offset=""))


async def test_inline_search_only_returns_own_listings(bot_env):
    dp, bot, session = bot_env
    owner, other = _uid(), _uid()
    await _make_user(owner)
    await _make_user(other)
    own_pair = await _make_pair(owner, source="@inl_src_1", target="@inl_tgt_1")
    other_pair = await _make_pair(other, source="@inl_src_2", target="@inl_tgt_2")
    await db_manager.record_cloned_message(pair_id=own_pair, source_msg_id=1, target_msg_id=11, last_caption="Chilonzor 3 xona MENIKI", price=50000.0)
    await db_manager.record_cloned_message(pair_id=other_pair, source_msg_id=1, target_msg_id=12, last_caption="Chilonzor 3 xona BEGONA", price=50000.0)

    await dp.feed_update(bot, _inline_update(owner, "Chilonzor"))
    answers = session.of(AnswerInlineQuery)
    assert len(answers) == 1
    descriptions = " ".join(r.description or "" for r in answers[0].results)
    assert "MENIKI" in descriptions and "BEGONA" not in descriptions


async def test_inline_search_short_query_and_private_mode(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    with patch("bot.handlers.inline_search.db_manager.can_user_access_bot", new=AsyncMock(return_value=True)), \
         patch("bot.handlers.inline_search.db_manager.get_connection") as conn:
        await dp.feed_update(bot, _inline_update(uid, "ab"))
        conn.assert_not_called()  # no archive scan for queries shorter than 3 characters
    assert session.of(AnswerInlineQuery)[-1].results[0].id == "search_help"

    newcomer = _uid()
    await db_manager.get_or_create_user(newcomer, "New", None)
    async with db_manager.write_transaction() as db:
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("UPDATE users SET created_at = ? WHERE user_id = ?", (future, newcomer))
    await db_manager.set_private_mode(True)
    try:
        await dp.feed_update(bot, _inline_update(newcomer, "Chilonzor"))
    finally:
        await db_manager.set_private_mode(False)
    assert session.of(AnswerInlineQuery)[-1].results == []


async def test_inline_search_is_rate_limited(bot_env):
    from bot.handlers import inline_search
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    for i in range(inline_search.INLINE_RATE_MAX_QUERIES + 5):
        await dp.feed_update(bot, _inline_update(uid, f"Yunusobod {i}"))
    assert len(session.of(AnswerInlineQuery)) == inline_search.INLINE_RATE_MAX_QUERIES


# ------------------------------------------------------------------------------------------------
# B-M2 / B-L2 / B-M11 / B-L21 — pair management
# ------------------------------------------------------------------------------------------------

async def test_resume_pair_without_subscription_is_refused(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid, trial_active=False)
    pair_id = await _make_pair(uid, source="@toggle_src", target="@toggle_tgt")
    await db_manager.set_pair_active_by_owner(pair_id, False)

    await dp.feed_update(bot, cb_update(uid, f"pair_toggle_{pair_id}"))

    assert not (await db_manager.get_pair_by_id(pair_id)).is_active
    edits = session.of(EditMessageText)
    assert edits and "Obunangiz faol emas" in edits[-1].text
    buttons = [b.callback_data for row in edits[-1].reply_markup.inline_keyboard for b in row]
    assert "menu_stars" in buttons


async def test_foreign_pair_view_answers_once_with_alert(bot_env):
    dp, bot, session = bot_env
    owner, intruder = _uid(), _uid()
    await _make_user(owner)
    await _make_user(intruder)
    pair_id = await _make_pair(owner, source="@view_src", target="@view_tgt")

    await dp.feed_update(bot, cb_update(intruder, f"pair_view_{pair_id}"))

    answers = _answers(session)
    assert len(answers) == 1 and answers[0].show_alert
    assert session.of(EditMessageText) == []


async def test_delete_pair_only_invalidates_listener_cache(bot_env):
    from services.telethon_listener import telethon_listener
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@del_src", target="@del_tgt")
    with patch.object(telethon_listener, "refresh_monitored_channels", new=AsyncMock()) as refresh, \
         patch.object(telethon_listener, "invalidate_pairs_cache") as invalidate, \
         patch.object(telethon_listener, "_is_running", True):
        await dp.feed_update(bot, cb_update(uid, f"pair_delete_yes_{pair_id}"))
    refresh.assert_not_awaited()
    invalidate.assert_called()
    assert await db_manager.get_pair_by_id(pair_id) is None


async def test_pair_panel_wires_remove_signature_and_catchup(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@wire_src", target="@wire_tgt")
    await dp.feed_update(bot, cb_update(uid, f"pair_toggle_remsig_{pair_id}"))
    await dp.feed_update(bot, cb_update(uid, f"pair_toggle_catchup_{pair_id}"))
    pair = await db_manager.get_pair_by_id(pair_id)
    assert pair.remove_signature is True and pair.auto_catchup is False
    keyboard = session.of(EditMessageText)[-1].reply_markup
    callbacks = {b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert {f"pair_toggle_remsig_{pair_id}", f"pair_toggle_catchup_{pair_id}"} <= callbacks


async def test_test_post_answers_immediately_and_checks_owner(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    target = _channel_id()
    pair_id = await _make_pair(uid, source="@tp_src", target=str(target), target_id=target)
    session.add_channel(target, "Kanal")
    session.set_member(target, BOT_ID, "administrator")  # legacy pair: the owner is not an admin there

    from bot.handlers import cloner_menu
    with patch("bot.handlers.cloner_menu.cloner_engine.send_test_post", new=AsyncMock(return_value=(True, "ok"))) as send:
        await dp.feed_update(bot, cb_update(uid, f"pair_test_post_{pair_id}"))
        await asyncio.gather(*list(cloner_menu._background_tasks), return_exceptions=True)
    send.assert_not_awaited()
    answers = _answers(session)
    assert len(answers) == 1 and not answers[0].show_alert and "yuborilmoqda" in answers[0].text
    assert any("administratori emassiz" in t for t in session.texts())


# ------------------------------------------------------------------------------------------------
# B-M3 / B-L10 / B-L1 — billing
# ------------------------------------------------------------------------------------------------

async def test_lower_tier_not_offered_or_sold_while_vip_active(bot_env):
    from config.plans import PLANS
    from bot.handlers import stars_billing
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid, tier="vip")
    assert stars_billing.PLANS is PLANS

    await dp.feed_update(bot, cb_update(uid, "menu_stars"))
    menu = session.of(EditMessageText)[-1]
    labels = [b.text for row in menu.reply_markup.inline_keyboard for b in row]
    assert not any("Pro" in label for label in labels)
    assert any("VIP tarifini uzaytirish" in label for label in labels)
    assert len(_answers(session)) == 1  # the menu callback is answered

    await dp.feed_update(bot, cb_update(uid, "buy_plan_pro"))
    assert session.of(SendInvoice) == []
    assert _answers(session)[-1].show_alert


async def test_refund_revokes_the_plan(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid, tier="pro")
    charge = f"test_charge_{uid}_pro"
    refund = RefundedPayment(total_amount=100, invoice_payload=json.dumps({"t": "pro", "u": uid}), telegram_payment_charge_id=charge)
    await dp.feed_update(bot, msg_update(uid, None, refunded_payment=refund))
    sub = await db_manager.get_user_subscription(uid)
    assert sub.tier == "free"
    assert any("qaytarildi" in t for t in session.texts())


async def test_refund_marks_store_order_refunded(bot_env):
    from database.models import StoreOrder
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    order_id = await db_manager.create_store_order(StoreOrder(user_id=uid, product_id=1, product_name="X", price_stars=40, status="awaiting_payment"))
    charge = f"store_charge_{uid}"
    assert await db_manager.mark_store_order_paid(order_id, uid, charge, 40)
    refund = RefundedPayment(total_amount=40, invoice_payload=json.dumps({"t": "store", "u": uid, "o": order_id}), telegram_payment_charge_id=charge)
    await dp.feed_update(bot, msg_update(uid, None, refunded_payment=refund))
    assert (await db_manager.get_store_order(order_id)).status == "refunded"


# ------------------------------------------------------------------------------------------------
# B-M5 / B-L12 — history backfill
# ------------------------------------------------------------------------------------------------

def test_history_keyboard_has_no_unlimited_option():
    from bot.keyboards.inline_buttons import get_history_count_keyboard
    for cap in (BACKFILL_MAX_MESSAGES, BACKFILL_MAX_MESSAGES_PRIVILEGED):
        data = [b.callback_data for row in get_history_count_keyboard(7, cap).inline_keyboard for b in row]
        assert "hist_start_7_all" not in data
        assert f"hist_start_7_{cap}" in data
        assert all(int(d.rsplit("_", 1)[1]) <= cap for d in data if d.startswith("hist_start_"))


async def test_backfill_limits_and_subscription(bot_env):
    from services.telethon_listener import telethon_listener
    dp, bot, session = bot_env
    uid, expired = _uid(), _uid()
    await _make_user(uid)
    await _make_user(expired, trial_active=False)
    pair_id = await _make_pair(uid, source="@hist_src", target="@hist_tgt")
    expired_pair = await _make_pair(expired, source="@hist_src2", target="@hist_tgt2")
    with patch.object(telethon_listener, "is_connected", return_value=True), \
         patch.object(telethon_listener, "clone_history", new=AsyncMock()) as clone:
        await dp.feed_update(bot, cb_update(uid, f"hist_start_{pair_id}_{BACKFILL_MAX_MESSAGES + 1}"))
        await dp.feed_update(bot, cb_update(expired, f"hist_start_{expired_pair}_10"))
    clone.assert_not_awaited()
    alerts = [a.text for a in _answers(session) if a.show_alert]
    assert len(alerts) == 2
    assert str(BACKFILL_MAX_MESSAGES) in alerts[0]
    # An expired plan suspends the user's pairs, so the refusal names either reason
    assert "faol obuna" in alerts[1] or "to'xtatilgan" in alerts[1]


async def test_backfill_early_result_is_rendered(bot_env):
    from services.telethon_listener import telethon_listener
    from services.channel_access import verify_destination_access  # noqa: F401 (patched below)
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@early_src", target="@early_tgt")
    with patch.object(telethon_listener, "is_connected", return_value=True), \
         patch("bot.handlers.history_clone.verify_destination_access", new=AsyncMock(return_value=(True, None, ""))), \
         patch.object(telethon_listener, "clone_history", new=AsyncMock(return_value={"status": "source_not_found"})):
        await dp.feed_update(bot, cb_update(uid, f"hist_start_{pair_id}_10"))
        from bot.handlers import history_clone
        await asyncio.gather(*list(history_clone._history_tasks), return_exceptions=True)
    final = session.of(EditMessageText)[-1].text
    assert "boshlanmadi" in final and "Manba kanal topilmadi" in final


async def test_only_one_backfill_per_user(bot_env):
    from services.telethon_listener import telethon_listener
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    first = await _make_pair(uid, source="@one_src", target="@one_tgt")
    second = await _make_pair(uid, source="@two_src", target="@two_tgt")
    running = asyncio.get_event_loop().create_future()
    with patch.dict(telethon_listener.active_history_tasks, {first: running}), \
         patch.object(telethon_listener, "is_connected", return_value=True), \
         patch.object(telethon_listener, "clone_history", new=AsyncMock()) as clone:
        await dp.feed_update(bot, cb_update(uid, f"hist_start_{second}_10"))
    running.cancel()
    clone.assert_not_awaited()
    assert "Boshqa kanalingiz" in _answers(session)[-1].text


# ------------------------------------------------------------------------------------------------
# B-M6 / B-L8 / B-L7 / B-L6 / B-L9 — settings screens
# ------------------------------------------------------------------------------------------------

async def test_long_values_are_shortened_in_the_panel(bot_env):
    from bot.handlers.cloner_menu import build_pair_detail_text
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@long_src", target="@long_tgt")
    await db_manager.update_pair_fields(pair_id, {
        "custom_signature": "<b>" + "S" * 1000 + "</b>",
        "replace_words": "a=b," * 1000,
        "blacklist_words": "spam, " * 600,
    })
    pair = await db_manager.get_pair_by_id(pair_id)
    text = build_pair_detail_text(pair)
    assert len(text) < 4096 and "…" in text
    assert "&lt;b&gt;" not in text and "<b>SSS" not in text  # tags stripped from the signature


async def test_too_long_signature_is_rejected(bot_env):
    from bot.states.cloner_states import EditSettingsSG
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    pair_id = await _make_pair(uid, source="@sig_src", target="@sig_tgt")
    state = await _state(dp, bot, uid)
    await state.set_state(EditSettingsSG.waiting_for_signature)
    await state.update_data(pair_id=pair_id)
    await dp.feed_update(bot, msg_update(uid, "x" * (SIGNATURE_MAX_CHARS + 1)))
    assert (await db_manager.get_pair_by_id(pair_id)).custom_signature == ""
    assert any("juda uzun" in t for t in session.texts())
    assert await state.get_state() == EditSettingsSG.waiting_for_signature.state


async def test_signature_presets_need_a_public_username(bot_env):
    from bot.handlers.settings_menu import get_signature_presets_keyboard
    from database.models import ChannelPair
    private_target = ChannelPair(id=5, user_id=1, source_channel="@s", target_channel="-1001234", target_title="Yopiq")
    public_target = ChannelPair(id=6, user_id=1, source_channel="@s", target_channel="@ochiq_kanal", target_title="Ochiq")
    private_data = [b.callback_data for row in get_signature_presets_keyboard(private_target).inline_keyboard for b in row]
    public_texts = [b.text for row in get_signature_presets_keyboard(public_target).inline_keyboard for b in row]
    assert not any(d.endswith(("_p1", "_p2", "_p3")) for d in private_data)
    assert "@ochiq_kanal" in public_texts and not any("-1001234" in t for t in public_texts)


async def test_paid_features_are_gated_for_trial_users(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)  # active trial, no paid plan
    pair_id = await _make_pair(uid, source="@gate_src", target="@gate_tgt")
    await dp.feed_update(bot, cb_update(uid, f"pair_wm_menu_{pair_id}"))
    await dp.feed_update(bot, cb_update(uid, f"pair_aff_{pair_id}"))
    await dp.feed_update(bot, cb_update(uid, f"pair_toggle_cta_{pair_id}"))
    await dp.feed_update(bot, cb_update(uid, f"drip_delay_{pair_id}_15"))
    edits = [e.text for e in session.of(EditMessageText)]
    assert len(edits) == 2 and all("Pulli Funksiya" in e for e in edits)
    pair = await db_manager.get_pair_by_id(pair_id)
    assert not pair.auto_cta_buttons and pair.drip_delay_minutes == 0
    assert all(a.show_alert for a in _answers(session)[-2:])


@pytest.mark.parametrize("tier", [None, "pro"])
async def test_premium_emojis_and_protected_mode_are_vip_only(bot_env, tier):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid, tier=tier)
    pair_id = await _make_pair(uid, source="@vip_src", target="@vip_tgt")
    await dp.feed_update(bot, cb_update(uid, f"pair_toggle_emoji_{pair_id}"))
    await dp.feed_update(bot, cb_update(uid, f"pair_toggle_prot_{pair_id}"))
    pair = await db_manager.get_pair_by_id(pair_id)
    assert not pair.auto_premium_emojis and not pair.is_protected_source
    assert all(a.show_alert for a in _answers(session))


async def test_switching_features_off_never_needs_a_plan(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)  # trial user whose pair still has paid features switched on
    pair_id = await _make_pair(uid, source="@off_src", target="@off_tgt")
    await db_manager.update_pair_fields(pair_id, {
        "auto_cta_buttons": True, "auto_premium_emojis": True,
        "drip_delay_minutes": 30, "night_mode": "silent", "ai_paraphrase_mode": "hype",
        "video_watermark_type": "text", "video_watermark_text": "@x",
    })
    assert await db_manager.toggle_protected_mode(pair_id) is True
    from bot.middlewares.throttling_middleware import ThrottlingMiddleware
    with patch.object(ThrottlingMiddleware, "_is_throttled", return_value=False):  # 7 taps in a row
        for data in (f"pair_toggle_cta_{pair_id}", f"pair_toggle_emoji_{pair_id}", f"pair_toggle_prot_{pair_id}",
                     f"drip_delay_{pair_id}_0", f"drip_toggle_night_{pair_id}", f"ai_set_{pair_id}_off",
                     f"vwm_toggle_{pair_id}"):
            await dp.feed_update(bot, cb_update(uid, data))
    pair = await db_manager.get_pair_by_id(pair_id)
    assert not pair.auto_cta_buttons and not pair.auto_premium_emojis and not pair.is_protected_source
    assert pair.drip_delay_minutes == 0 and pair.night_mode == "off"
    assert pair.ai_paraphrase_mode == "off" and pair.video_watermark_type == "none"
    assert not any(a.show_alert for a in _answers(session))


@pytest.mark.parametrize("data", ["wm_pos_{id}_evil_pos", "vwm_pos_{id}_x", "ai_set_{id}_jailbreak",
                                  "drip_delay_{id}_99999", "trans_set_{id}_<b>", "sig_set_{id}_p9"])
async def test_forged_callback_payloads_are_rejected(bot_env, data):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid, tier="vip")
    pair_id = await _make_pair(uid, source="@forge_src", target="@forge_tgt")
    before = await db_manager.get_pair_by_id(pair_id)
    await dp.feed_update(bot, cb_update(uid, data.format(id=pair_id)))
    after = await db_manager.get_pair_by_id(pair_id)
    assert (after.video_watermark_pos, after.ai_paraphrase_mode, after.drip_delay_minutes, after.target_lang,
            after.image_watermark_pos, after.custom_signature) == (
            before.video_watermark_pos, before.ai_paraphrase_mode, before.drip_delay_minutes, before.target_lang,
            before.image_watermark_pos, before.custom_signature)
    answers = _answers(session)
    assert len(answers) == 1 and answers[0].show_alert


# ------------------------------------------------------------------------------------------------
# B-L19 — comment moderation scope
# ------------------------------------------------------------------------------------------------

async def test_comment_moderator_only_acts_in_linked_discussion_groups(bot_env):
    from bot.handlers import comment_moderator
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)
    channel = _channel_id()
    await _make_pair(uid, source="@mod_src", target=str(channel), target_id=channel)
    moderated, unrelated = _channel_id(), _channel_id()
    session.add_channel(moderated, "Izohlar", chat_type="supergroup", linked_chat_id=channel)
    session.add_channel(unrelated, "Boshqa guruh", chat_type="supergroup")
    comment_moderator._moderated_groups.clear()
    spammer = _uid()

    await dp.feed_update(bot, msg_update(spammer, "1xbet bonus! t.me/+AbCdEfGh", chat_type="supergroup", chat_id=unrelated))
    assert [c for c in session.calls if type(c).__name__ == "DeleteMessage"] == []

    await dp.feed_update(bot, msg_update(spammer, "1xbet bonus! t.me/+AbCdEfGh", chat_type="supergroup", chat_id=moderated))
    assert len([c for c in session.calls if type(c).__name__ == "DeleteMessage"]) == 1


# ------------------------------------------------------------------------------------------------
# B-L20 / misc
# ------------------------------------------------------------------------------------------------

async def test_admin_filter_needs_no_database_query(env):
    from bot.filters import IsAdminFilter
    event = SimpleNamespace(from_user=SimpleNamespace(id=_uid()))
    with patch("bot.filters.admin_filter.db_manager.is_admin", new=AsyncMock(side_effect=AssertionError("no DB query"))):
        assert await IsAdminFilter()(event) is False
        assert await IsAdminFilter()(SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID))) is True


async def test_admin_panel_button_reaches_the_admin_router(bot_env):
    dp, bot, session = bot_env
    await dp.feed_update(bot, cb_update(ADMIN_ID, "menu_admin"))
    assert not any(a.show_alert for a in _answers(session))
    assert session.of(EditMessageText), "the admin dashboard should replace the menu"

    regular = _uid()
    await _make_user(regular)
    await dp.feed_update(bot, cb_update(regular, "menu_admin"))
    assert _answers(session)[-1].show_alert


async def test_edit_or_send_handles_inaccessible_messages(env):
    from bot.utils import edit_or_send
    session = FakeSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    uid = _uid()
    old_message = InaccessibleMessage(chat=Chat(id=uid, type="private"), message_id=3).as_(bot)
    callback = CallbackQuery(
        id="1", from_user=_user(uid), chat_instance="ci", data="x", message=old_message
    ).as_(bot)
    await edit_or_send(callback, "Salom", parse_mode="HTML")
    assert session.of(EditMessageText) == [] and session.of(SendMessage)[0].text == "Salom"


def test_user_facing_texts_have_no_implementation_jargon():
    from bot.handlers.start import get_welcome_text
    from bot.handlers.help_guide import GUIDE_TEXT, GUIDE_SEC_3
    assert all("Playwright" not in t for t in (get_welcome_text(), GUIDE_TEXT, GUIDE_SEC_3))


async def test_event_isolation_serializes_and_forgets_keys():
    from bot.bot_instance import PerUserEventIsolation
    isolation = PerUserEventIsolation()
    key = StorageKey(bot_id=1, chat_id=2, user_id=2)
    order: List[str] = []

    async def job(name: str):
        async with isolation.lock(key):
            order.append(f"{name}+")
            await asyncio.sleep(0.01)
            order.append(f"{name}-")

    await asyncio.gather(job("a"), job("b"))
    assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])
    assert isolation._locks == {}


def test_inline_listing_links_never_embed_invite_links():
    from bot.handlers.inline_search import listing_post_url
    assert listing_post_url({"target_msg_id": 5, "target_channel": "@my_chan"}) == "https://t.me/my_chan/5"
    # A destination saved as an invite link is addressed through the pair's numeric id
    assert listing_post_url({"target_msg_id": 5, "target_channel": "https://t.me/+AbCdEf",
                             "pair_target_id": -1001234567890}) == "https://t.me/c/1234567890/5"
    assert listing_post_url({"target_msg_id": 5, "target_channel": "https://t.me/+AbCdEf"}) is None
    assert listing_post_url({"target_msg_id": None, "target_channel": "@my_chan"}) is None


async def test_inline_search_filters_by_price_range_within_own_pairs(env):
    uid, other = _uid(), _uid()
    await _make_user(uid)
    await _make_user(other)
    pair_id = await _make_pair(uid, source="@inline_price_src", target="@inline_price_dst")
    other_pair = await _make_pair(other, source="@inline_other_src", target="@inline_other_dst")
    for pid, msg_id, price, caption in ((pair_id, 1, 50000.0, "Yunusobod 2 xona"),
                                       (pair_id, 2, 90000.0, "Yunusobod 4 xona"),
                                       (other_pair, 3, 52000.0, "Yunusobod begona")):
        await db_manager.record_cloned_message(pair_id=pid, source_msg_id=msg_id, target_msg_id=msg_id + 100,
                                               media_type="photo", price=price, last_caption=caption)
    rows = await db_manager.search_user_listings(uid, text="Yunusobod", price_range=(35000, 65000))
    assert [r["last_caption"] for r in rows] == ["Yunusobod 2 xona"]
    # LIKE wildcards typed by the user are matched literally
    assert await db_manager.search_user_listings(uid, text="%") == []


# ------------------------------------------------------------------------------------------------
# Story Cloner UI (story_menu.py)
# ------------------------------------------------------------------------------------------------

def test_story_channel_reference_normalization():
    from bot.handlers.story_menu import normalize_channel_ref
    assert normalize_channel_ref("@Toshkent_Uylar") == "@Toshkent_Uylar"
    assert normalize_channel_ref("toshkent_uylar") == "@toshkent_uylar"
    assert normalize_channel_ref("https://t.me/toshkent_uylar/15") == "@toshkent_uylar"
    assert normalize_channel_ref("t.me/s/toshkent_uylar") == "@toshkent_uylar"
    assert normalize_channel_ref("-1001234567890") == "-1001234567890"
    assert normalize_channel_ref("1234567890") == "-1001234567890"
    # Invite links stay invite links (the old code turned t.me/joinchat/HASH into "@joinchat")
    assert normalize_channel_ref("https://t.me/joinchat/AbCdEfGh123") == "https://t.me/+AbCdEfGh123"
    assert normalize_channel_ref("t.me/+AbCdEfGh123") == "https://t.me/+AbCdEfGh123"
    for bad in ("", "Qo'llanma", "two words", "@abc", "https://example.com/x", "t.me/+short"):
        assert normalize_channel_ref(bad) is None, bad


def test_story_price_input_parsing():
    from bot.handlers.story_menu import parse_price_input
    assert parse_price_input("700") == 700
    assert parse_price_input("$1,500") == 1500
    assert parse_price_input("1.500") == 1500  # thousands separator, not 1.5
    assert parse_price_input("1 500") == 1500
    assert parse_price_input("850.50") == 850.5
    for bad in ("", "nan", "inf", "-5", "0", "abc", "1e9", "99999999"):
        assert parse_price_input(bad) is None, bad


async def test_story_channel_target_is_set_only_with_a_usable_channel(bot_env):
    from bot.states.story_states import StorySettingsSG
    from services.story_cloner_service import story_cloner_service
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid, tier="vip")
    state = await _state(dp, bot, uid)

    await dp.feed_update(bot, cb_update(uid, "story_toggle_target"))
    assert await state.get_state() == StorySettingsSG.waiting_for_target_channel.state
    assert (await db_manager.get_story_settings(uid)).target_type == "self"

    refused = AsyncMock(return_value=(None, "Kanalga istoriya joylash huquqi yo'q."))
    with patch.object(story_cloner_service, "check_story_target", new=refused):
        await dp.feed_update(bot, msg_update(uid, "@my_story_channel"))
    assert await state.get_state() == StorySettingsSG.waiting_for_target_channel.state
    assert (await db_manager.get_story_settings(uid)).target_type == "self"
    assert any("joylab bo'lmaydi" in t for t in session.texts())

    channel = SimpleNamespace(id=1778899001, username="my_story_channel", title="Mening <kanalim>")
    with patch.object(story_cloner_service, "check_story_target", new=AsyncMock(return_value=(channel, None))):
        await dp.feed_update(bot, msg_update(uid, "https://t.me/my_story_channel"))
    st = await db_manager.get_story_settings(uid)
    assert (st.target_type, st.target_channel, st.target_id) == ("channel", "@my_story_channel", 1778899001)
    assert await state.get_state() is None
    assert any("Mening &lt;kanalim&gt;" in t for t in session.texts())


async def test_story_source_change_replaces_the_old_channel_id(bot_env):
    from bot.states.story_states import StorySettingsSG
    from services.story_cloner_service import story_cloner_service
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid, tier="vip")
    await db_manager.update_story_settings(uid, source_channel="@old_source", source_id=555)
    state = await _state(dp, bot, uid)

    await dp.feed_update(bot, cb_update(uid, "story_edit_channel"))
    assert await state.get_state() == StorySettingsSG.waiting_for_source_channel.state

    with patch.object(story_cloner_service, "resolve_channel_for_user", new=AsyncMock(return_value=(None, "not_found"))):
        await dp.feed_update(bot, msg_update(uid, "@missing_source"))
    assert (await db_manager.get_story_settings(uid)).source_channel == "@old_source"
    assert await state.get_state() == StorySettingsSG.waiting_for_source_channel.state

    await dp.feed_update(bot, msg_update(uid, "not a channel at all"))
    assert any("Noto'g'ri format" in t for t in session.texts())

    with patch.object(story_cloner_service, "resolve_channel_for_user", new=AsyncMock(return_value=(None, "not_connected"))):
        await dp.feed_update(bot, msg_update(uid, "https://t.me/new_source"))
    st = await db_manager.get_story_settings(uid)
    assert (st.source_channel, st.source_id) == ("@new_source", None)
    assert await state.get_state() is None


async def test_story_settings_callbacks_validate_values_and_redraw_the_screen(bot_env):
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid, tier="vip")
    before = await db_manager.get_story_settings(uid)

    from bot.middlewares.throttling_middleware import ThrottlingMiddleware
    # Six taps in a row would hit the anti-flood limit (5 per 2 s); it is not what this test is about
    with patch.object(ThrottlingMiddleware, "_is_throttled", return_value=False):
        for data in ("story_set_cooldown_7", "story_set_limit_0", "story_set_limit_101", "story_set_duration_90",
                     "story_set_price_123", "story_set_style_neon"):
            await dp.feed_update(bot, cb_update(uid, data))
    after = await db_manager.get_story_settings(uid)
    assert (after.drip_delay_minutes, after.max_stories_per_day, after.video_duration, after.min_price,
            after.background_style) == (before.drip_delay_minutes, before.max_stories_per_day,
                                        before.video_duration, before.min_price, before.background_style)
    assert sum(1 for a in _answers(session) if a.show_alert) == 6

    await dp.feed_update(bot, cb_update(uid, "story_toggle_badges_f"))
    assert (await db_manager.get_story_settings(uid)).enable_smart_badges is (not before.enable_smart_badges)
    # The filters screen (text and keyboard) is redrawn, not the queue keyboard
    last_edit = session.of(EditMessageText)[-1]
    assert "Narx va Sifat Filtrlari" in last_edit.text


async def test_story_test_post_runs_in_background_with_cooldown(bot_env):
    import bot.handlers.story_menu as story_menu
    from services.story_cloner_service import story_cloner_service
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid, tier="vip")
    await db_manager.update_story_settings(uid, source_channel="@listing_source")

    publish = AsyncMock(return_value=(True, "ok", {"story_id": 77, "msg_id": 5, "price": 950.0,
                                                   "source_channel": "@listing_source", "story_url": None}))
    with patch.object(story_cloner_service, "test_publish_latest_post", new=publish):
        await dp.feed_update(bot, cb_update(uid, "story_menu_test_post"))
        await asyncio.gather(*list(story_menu._background_tasks), return_exceptions=True)
        publish.assert_awaited_once_with(uid)
        assert any("Test istoriya joylandi" in t for t in session.texts())

        await dp.feed_update(bot, cb_update(uid, "story_menu_test_post"))
        publish.assert_awaited_once()
        assert _answers(session)[-1].show_alert and "soniyadan keyin" in _answers(session)[-1].text


async def test_story_menu_is_vip_only_and_ends_story_login(bot_env):
    from bot.states.story_states import StoryAuthSG
    from services.story_cloner_service import story_cloner_service
    dp, bot, session = bot_env
    uid = _uid()
    await _make_user(uid)  # trial user, no VIP
    state = await _state(dp, bot, uid)
    await state.set_state(StoryAuthSG.waiting_for_code)

    with patch.object(story_cloner_service, "cancel_login", new=AsyncMock()) as cancel_login:
        await dp.feed_update(bot, msg_update(uid, "1 2 3 4 5"))
    cancel_login.assert_awaited_once_with(uid)
    assert await state.get_state() is None
    assert any("VIP Cheksiz Tarif Talab Qilinadi" in t for t in session.texts())


# ------------------------------------------------------------------------------------------------
# Every button reachable from the menus is routed (dispatcher-level crawl)
# ------------------------------------------------------------------------------------------------

# Callbacks that change state irreversibly or start background work are pressed after the crawl
_CRAWL_DEFERRED_PREFIXES = (
    "pair_delete_yes_", "story_auth_logout_yes", "auth_logout_yes", "adm_revoke_ok_", "adm_admin_revoke_",
    "admin_broadcast_confirm", "admin_toggle_bot_mode", "adm_wl_rm_", "story_del_src_", "admin_download_backup",
    "admin_restart_listener", "story_menu_test_post", "pair_test_post_", "hist_start_", "backup_restore_start_",
    "buy_plan_", "private_unlock_stars_50",
)


def _callbacks_in(method) -> List[str]:
    markup = getattr(method, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or []
    return [b.callback_data for row in rows for b in row if getattr(b, "callback_data", None)]


async def test_every_button_reachable_from_the_menus_has_a_handler(bot_env):
    from aiogram.dispatcher.event.bases import UNHANDLED
    from aiogram.methods import EditMessageReplyMarkup
    from services.telethon_listener import telethon_listener
    dp, bot, session = bot_env
    admin = ADMIN_ID
    await _make_user(admin)
    pair_id = await _make_pair(admin, source="@crawl_source_chan", target="@crawl_target_chan")
    await db_manager.update_story_settings(admin, source_channel="@crawl_story_src")

    seeds = ["menu_main", "menu_admin", "story_main_menu", f"pair_view_{pair_id}", "menu_guide", "menu_stars",
             "cloner_list_pairs", "menu_cloner", "menu_stats", "menu_quickstart"]
    queue, seen, deferred = list(seeds), set(seeds), []
    unhandled: List[str] = []
    errors: List[tuple] = []

    async def press(data: str):
        before = len(session.calls)
        try:
            result = await dp.feed_update(bot, cb_update(admin, data))
        except Exception as e:  # a handler ran but failed
            errors.append((data, repr(e)))
            result = None
        if result is UNHANDLED:
            unhandled.append(data)
        await (await _state(dp, bot, admin)).clear()
        found = []
        for method in session.calls[before:]:
            if isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup)):
                found.extend(_callbacks_in(method))
        return found

    with patch.object(telethon_listener, "start", new=AsyncMock()), \
         patch.object(telethon_listener, "is_connected", return_value=False):
        steps = 0
        while queue and steps < 400:
            data = queue.pop(0)
            steps += 1
            if data.startswith(_CRAWL_DEFERRED_PREFIXES):
                deferred.append(data)
                continue
            for new in await press(data):
                if new not in seen:
                    seen.add(new)
                    queue.append(new)
        for data in deferred:
            await press(data)
        # The crawl toggled the bot mode (and possibly other shared settings): restore the public mode
        await db_manager.set_private_mode(False)
        import bot.handlers.cloner_menu as cloner_menu_module
        for task in list(cloner_menu_module._background_tasks):
            task.cancel()

    assert not unhandled, f"buttons without a handler: {unhandled}"
    assert not errors, f"buttons whose handler raised: {errors}"
    assert len(seen) >= 80, f"crawl reached only {len(seen)} buttons"
