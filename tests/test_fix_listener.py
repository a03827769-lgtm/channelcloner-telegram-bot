"""Tests for the F2 fixes: MTProto listener dispatch/sync/resolution, run.py lifecycle, log redaction,
subscription notifications and the small runtime services. Everything is mocked: no Telegram traffic, and
HTTP only against servers these tests start themselves on free local ports."""
import asyncio
import gc
import inspect
import io
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import pytest
from aiohttp import web
from telethon.errors import AuthKeyDuplicatedError, ChannelPrivateError
from telethon.tl.types import (
    Channel,
    ChatInvite,
    ChatInviteAlready,
    ChatPhotoEmpty,
    MessageActionChannelMigrateFrom,
    MessageActionChatMigrateTo,
    MessageReplyHeader,
    MessageService,
    PeerChannel,
    PeerChat,
    PhotoEmpty,
    UpdateNewChannelMessage,
    UpdateNewMessage,
)

import run
import services.subscription_watcher as sw_mod
import services.telethon_listener as tl_mod
from database.db_manager import DatabaseManager
from database.models import ChannelPair
from services.code_reloader import SourceCodeWatcher
from services.log_viewer import MemoryLogHandler, SecretRedactingFilter, sanitize_log_message
from services.media_handler import MediaGroupBuffer
from services.memory_supervisor import SystemSupervisor
from services.phone_utils import normalize_phone_number
from services.subscription_watcher import SubscriptionWatcher, paid_plan_days_left
from services.telethon_listener import (
    SOLD_KEYWORDS_RE,
    SOLD_TAG,
    TelethonListener,
    message_in_topic,
    message_topic_id,
    strip_tg_emoji,
    topic_iter_kwargs,
)
from services.text_processor import TextProcessor


# --------------------------------------------------------------------------- helpers

def make_msg(msg_id, text="post", grouped_id=None, out=False, post=False, via_bot_id=None, from_id=None,
             reply_to=None, edit_date=None, media=None):
    return SimpleNamespace(
        id=msg_id, text=text, message=text, media=media, action=None, grouped_id=grouped_id, out=out, post=post,
        via_bot_id=via_bot_id, from_id=from_id, reply_to=reply_to, edit_date=edit_date, entities=None,
    )


def make_event(message, chat_id, is_private=False, chat=None):
    event = SimpleNamespace(message=message, chat_id=chat_id, is_private=is_private)
    event.get_chat = AsyncMock(return_value=chat)
    return event


def make_pair(pair_id, source_id, target_id, user_id=1, **extra):
    return ChannelPair(
        id=pair_id, user_id=user_id, source_channel=str(source_id), source_id=source_id,
        target_channel=str(target_id), target_id=target_id, is_active=True, **extra
    )


def fake_db(pairs, own_posts=()):
    db = MagicMock()
    db.get_all_active_pairs = AsyncMock(return_value=list(pairs))
    db.get_pair_by_id = AsyncMock(side_effect=lambda pid: next((p for p in pairs if p.id == pid), None))
    db.is_own_clone_post = AsyncMock(side_effect=lambda chat, mid: (tl_mod.normalize_peer_id(chat), mid) in set(own_posts))
    return db


def channel(raw_id, left=False, username=None, title="Chan"):
    return Channel(id=raw_id, title=title, photo=ChatPhotoEmpty(), date=None, left=left, access_hash=raw_id * 7, username=username)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeTelethonClient:
    """Minimal async stand-in for TelegramClient used by the resolution tests."""

    def __init__(self, entities=None, dialogs=None, rpc=None):
        self.entities = entities or {}
        self.dialogs = dialogs or []
        self.rpc = rpc or {}
        self.get_entity_calls = []
        self.requests = []
        self.dialog_scans = []

    def is_connected(self):
        return True

    async def get_entity(self, ref):
        self.get_entity_calls.append(ref)
        key = int(f"-100{ref.id}") if isinstance(ref, Channel) else ref
        if key in self.entities:
            return self.entities[key]
        raise ValueError(f"Could not find the input entity for {ref!r}")

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(name)
        result = self.rpc.get(name)
        if isinstance(result, BaseException):
            raise result
        return result

    async def iter_dialogs(self, limit=None):
        self.dialog_scans.append(limit)
        for dialog in self.dialogs:
            yield dialog


@pytest.fixture
async def listener():
    lst = TelethonListener()
    yield lst
    lst._shutdown = True
    lst._reset_dispatch_state()
    pending = [t for t in list(lst._background_tasks) if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=2)


async def settle(lst):
    """Let spawned listener tasks run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)
    pending = [t for t in list(lst._background_tasks) if not t.done() and t not in lst._pair_workers.values()]
    if pending:
        await asyncio.wait(pending, timeout=2)


# --------------------------------------------------------------------------- LS-L14 / M5: private chats, stories

async def test_private_chat_is_ignored_before_any_lookup(listener):
    pair_lookup = AsyncMock(return_value=[])
    story = AsyncMock()
    event = make_event(make_msg(1, post=True), chat_id=555)
    event.is_private = True
    with patch.object(listener, "_get_active_pairs", pair_lookup), patch.object(listener, "_dispatch_story_post", story):
        await listener._handle_new_message(event)
    pair_lookup.assert_not_called()
    story.assert_not_called()
    event.get_chat.assert_not_called()


async def test_story_dispatch_only_for_broadcast_channel_posts(listener):
    story = AsyncMock()
    with patch.object(tl_mod, "db_manager", fake_db([])), patch.object(listener, "_dispatch_story_post", story):
        await listener._handle_new_message(make_event(make_msg(5, post=True), chat_id=-100333))
        await listener._handle_new_message(make_event(make_msg(6, post=False), chat_id=-100444))
        await settle(listener)
    assert story.call_count == 1
    assert story.call_args.args[1].id == 5


# --------------------------------------------------------------------------- LS-H3: repost loops

async def test_own_clone_posts_in_a_loop_are_not_recloned(listener, monkeypatch):
    monkeypatch.setattr(tl_mod, "OWN_POST_SETTLE_SECONDS", 0.0)
    a_to_b = make_pair(1, -100111, -100222)
    b_to_a = make_pair(2, -100222, -100111)
    db = fake_db([a_to_b, b_to_a], own_posts={(222, 50)})
    clone = AsyncMock(return_value=True)
    with patch.object(tl_mod, "db_manager", db), patch.object(tl_mod.cloner_engine, "clone_single_message", clone):
        # Our clone #50 appears in B, which is also the source of B->A
        await listener._handle_new_message(make_event(make_msg(50), chat_id=-100222))
        # A genuine post in B
        await listener._handle_new_message(make_event(make_msg(51), chat_id=-100222))
        # A post our userbot made in B (premium-emoji path): skipped without any database lookup
        await listener._handle_new_message(make_event(make_msg(52, out=True), chat_id=-100222))
        await listener._drain_pair_queues(5)
    assert [c.args[0].id for c in clone.await_args_list] == [51]
    assert all(c.args[1].id == 2 for c in clone.await_args_list)
    assert (222, 52) not in [(tl_mod.normalize_peer_id(c.args[0]), c.args[1]) for c in db.is_own_clone_post.await_args_list]


async def test_bot_authored_posts_are_skipped_but_owner_posts_in_pure_sources_are_cloned(listener):
    pair = make_pair(3, -100555, -100666)
    clone = AsyncMock(return_value=True)
    with patch.object(TelethonListener, "_bot_user_id", staticmethod(lambda: 777)), \
         patch.object(tl_mod, "db_manager", fake_db([pair])), \
         patch.object(tl_mod.cloner_engine, "clone_single_message", clone):
        await listener._handle_new_message(make_event(make_msg(1, via_bot_id=777), chat_id=-100555))
        await listener._handle_new_message(make_event(make_msg(2, from_id=SimpleNamespace(user_id=777)), chat_id=-100555))
        # `out` in a chat that is no pair's destination is a real post of the account owner
        await listener._handle_new_message(make_event(make_msg(3, out=True), chat_id=-100555))
        await listener._drain_pair_queues(5)
    assert [c.args[0].id for c in clone.await_args_list] == [3]


# --------------------------------------------------------------------------- LS-M8: ordered per-pair delivery

async def test_album_then_text_are_delivered_in_source_order(listener):
    pair = make_pair(4, -100777, -100888)
    order = []

    async def fake_album(messages, p):
        order.append(("album", [m.id for m in messages]))
        return True

    async def fake_single(message, p):
        order.append(("single", message.id))
        return True

    with patch.object(tl_mod, "db_manager", fake_db([pair])), \
         patch.object(tl_mod.media_handler, "album_buffer", MediaGroupBuffer(debounce_delay=0.2, max_wait=1.0)), \
         patch.object(tl_mod.cloner_engine, "clone_media_group", fake_album), \
         patch.object(tl_mod.cloner_engine, "clone_single_message", fake_single):
        await listener._handle_new_message(make_event(make_msg(10, grouped_id=99, media=object()), chat_id=-100777))
        await listener._handle_new_message(make_event(make_msg(11, grouped_id=99, media=object()), chat_id=-100777))
        await listener._handle_new_message(make_event(make_msg(12), chat_id=-100777))
        await listener._drain_pair_queues(5)
    assert order == [("album", [10, 11]), ("single", 12)]


async def test_one_pair_is_delivered_sequentially_and_idle_worker_exits(listener, monkeypatch):
    monkeypatch.setattr(tl_mod, "PAIR_WORKER_IDLE_SECONDS", 0.05)
    pair = make_pair(5, -100901, -100902)
    active = 0
    peak = 0

    async def slow_single(message, p):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return True

    with patch.object(tl_mod, "db_manager", fake_db([pair])), \
         patch.object(tl_mod.cloner_engine, "clone_single_message", slow_single):
        for msg_id in range(1, 6):
            await listener._handle_new_message(make_event(make_msg(msg_id), chat_id=-100901))
        worker = listener._pair_workers[pair.id]
        await listener._drain_pair_queues(5)
        await asyncio.wait_for(worker, timeout=2)
    assert peak == 1
    assert pair.id not in listener._pair_workers
    assert pair.id not in listener._pair_queues


# --------------------------------------------------------------------------- LS-L10: shutdown order

async def test_stop_delivers_buffered_album_before_disconnecting(listener):
    pair = make_pair(6, -100123, -100124)
    log = []

    async def fake_album(messages, p):
        log.append(("clone", [m.id for m in messages]))
        return True

    async def fake_disconnect():
        log.append("disconnect")

    client = MagicMock()
    client.disconnect = MagicMock(side_effect=lambda: fake_disconnect())
    listener.client = client
    with patch.object(tl_mod, "db_manager", fake_db([pair])), \
         patch.object(tl_mod.media_handler, "album_buffer", MediaGroupBuffer(debounce_delay=30.0, max_wait=60.0)), \
         patch.object(tl_mod.cloner_engine, "clone_media_group", fake_album):
        await listener._handle_new_message(make_event(make_msg(20, grouped_id=5, media=object()), chat_id=-100123))
        await listener._handle_new_message(make_event(make_msg(21, grouped_id=5, media=object()), chat_id=-100123))
        await listener.stop()
    assert log == [("clone", [20, 21]), "disconnect"]
    assert client.remove_event_handler.called


async def test_stop_awaits_cancelled_clone_tasks(listener, monkeypatch):
    monkeypatch.setattr(tl_mod, "SHUTDOWN_DRAIN_TIMEOUT", 0.2)
    pair = make_pair(7, -100321, -100322)
    log = []
    started = asyncio.Event()

    async def hanging_clone(message, p):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            log.append("clone-finalized")

    client = MagicMock()
    client.disconnect = AsyncMock(side_effect=lambda: log.append("disconnect"))
    listener.client = client
    with patch.object(tl_mod, "db_manager", fake_db([pair])), \
         patch.object(tl_mod.cloner_engine, "clone_single_message", hanging_clone):
        await listener._handle_new_message(make_event(make_msg(1), chat_id=-100321))
        await asyncio.wait_for(started.wait(), timeout=2)
        await listener.stop()
    assert log == ["clone-finalized", "disconnect"]


# --------------------------------------------------------------------------- LS-H1 / LS-L13: start-up

async def test_start_listener_snapshots_watermarks_before_handlers_and_masks_phone(listener, monkeypatch, caplog):
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(b"\x01" * 256)
    session_string = session.save()
    order = []

    class FakeClient:
        def __init__(self, string_session, *args, **kwargs):
            self.session = string_session

        async def connect(self):
            order.append("connect")

        async def get_me(self):
            return SimpleNamespace(id=42, first_name="Central", username="central", phone="998901234567")

        def is_connected(self):
            return True

        def add_event_handler(self, callback, event):
            order.append("handler")

        def remove_event_handler(self, callback, event=None):
            return 0

        async def disconnect(self):
            order.append("disconnect")

    floors = {7: 100}

    async def snapshot():
        order.append("snapshot")
        return floors

    refresh = AsyncMock()
    monkeypatch.setattr(tl_mod.settings, "TELETHON_SESSION", None)
    with patch.object(tl_mod, "TelegramClient", FakeClient), \
         patch.object(listener, "get_active_session_string", AsyncMock(return_value=session_string)), \
         patch.object(listener, "_snapshot_catchup_floors", snapshot), \
         patch.object(listener, "refresh_monitored_channels", refresh), \
         patch.object(listener, "_connection_supervisor", AsyncMock()), \
         patch.object(tl_mod, "db_manager", MagicMock(set_setting=AsyncMock())):
        with caplog.at_level(logging.INFO, logger="services.telethon_listener"):
            await listener.start()
    assert order.index("snapshot") < order.index("handler")
    refresh.assert_awaited_once_with(catchup_floors=floors)
    assert "998901234567" not in caplog.text
    assert "****" in caplog.text
    assert listener._is_running is True


async def test_catch_up_starts_from_the_snapshot_floor(tmp_path, listener):
    db = DatabaseManager(str(tmp_path / "floor.db"))
    await db.init_db()
    try:
        await db.get_or_create_user(501, "Floor", "floor_user")
        pid = await db.add_channel_pair(user_id=501, source_channel="@src_floor", source_title="S",
                                        target_channel="@tgt_floor", target_title="T")
        await db.update_pair_last_seen_msg_id(pid, 880100)
        floor = await db.get_effective_last_source_msg_id(pid)
        # A real-time clone that happened after connecting advanced the watermark past the offline gap
        await db.record_cloned_message(pid, 880121, target_msg_id=9121)
        pair = await db.get_pair_by_id(pid)
        messages = [make_msg(i) for i in (880101, 880102, 880121)]
        seen_min_ids = []

        async def iter_messages(entity, min_id=None, limit=None, reverse=True, **kwargs):
            seen_min_ids.append(min_id)
            for m in messages:
                if m.id > min_id:
                    yield m

        client = MagicMock()
        client.is_connected.return_value = True
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_messages = AsyncMock(return_value=[SimpleNamespace(id=880121)])
        client.iter_messages = iter_messages
        listener.client = client
        clone = AsyncMock(return_value=True)
        with patch.object(tl_mod, "db_manager", db), \
             patch.object(tl_mod.cloner_engine, "clone_single_message", clone), \
             patch.object(listener, "resolve_entity", AsyncMock(return_value=SimpleNamespace(id=4242))):
            res = await listener.catch_up_pair_messages(pair, min_id_floor=floor)
        assert seen_min_ids == [880100]
        assert [c.args[0].id for c in clone.await_args_list] == [880101, 880102]
        assert res["status"] == "completed"
    finally:
        await db.close()


async def test_catch_up_stops_when_the_pair_is_paused(tmp_path, listener):
    db = DatabaseManager(str(tmp_path / "paused.db"))
    await db.init_db()
    try:
        await db.get_or_create_user(502, "Paused", "paused_user")
        pid = await db.add_channel_pair(user_id=502, source_channel="@src_paused", source_title="S",
                                        target_channel="@tgt_paused", target_title="T")
        await db.update_pair_last_seen_msg_id(pid, 881100)
        pair = await db.get_pair_by_id(pid)

        async def iter_messages(entity, min_id=None, limit=None, reverse=True, **kwargs):
            for i in (881101, 881102, 881103):
                yield make_msg(i)

        client = MagicMock()
        client.is_connected.return_value = True
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_messages = AsyncMock(return_value=[SimpleNamespace(id=881103)])
        client.iter_messages = iter_messages
        listener.client = client

        async def clone_then_pause(message, p):
            await db.set_pair_active_by_owner(pid, False)
            return True

        clone = AsyncMock(side_effect=clone_then_pause)
        with patch.object(tl_mod, "db_manager", db), \
             patch.object(tl_mod.cloner_engine, "clone_single_message", clone), \
             patch.object(listener, "resolve_entity", AsyncMock(return_value=SimpleNamespace(id=4343))):
            res = await listener.catch_up_pair_messages(pair)
        assert clone.await_count == 1
        assert res["status"] == "pair_inactive"
    finally:
        await db.close()


# --------------------------------------------------------------------------- LS-M1 / LS-L11: supervisor

async def test_supervisor_rebuilds_a_fresh_client_instead_of_reconnecting(listener, monkeypatch):
    monkeypatch.setattr(tl_mod, "SUPERVISOR_PROBE_INTERVAL", 0.01)
    client = MagicMock()
    client.is_connected.return_value = False
    client.connect = AsyncMock()
    listener.client = client
    listener._is_running = True
    rebuilt = asyncio.Event()

    async def fake_start_listener():
        rebuilt.set()

    with patch.object(listener, "start_listener", fake_start_listener):
        await asyncio.wait_for(listener._connection_supervisor(), timeout=2)
        await asyncio.wait_for(rebuilt.wait(), timeout=2)
    client.connect.assert_not_called()
    assert listener._is_running is False


async def test_supervisor_treats_duplicated_auth_key_as_revocation(listener, monkeypatch):
    monkeypatch.setattr(tl_mod, "SUPERVISOR_PROBE_INTERVAL", 0.01)

    async def probe(request):
        raise AuthKeyDuplicatedError(request=None)

    client = MagicMock(side_effect=probe)
    client.is_connected = MagicMock(return_value=True)
    listener.client = client
    listener._is_running = True
    revoked = AsyncMock()
    with patch.object(listener, "_handle_auth_revoked", revoked):
        await asyncio.wait_for(listener._connection_supervisor(), timeout=2)
    revoked.assert_awaited_once()


async def test_auth_revocation_purges_session_and_alerts_super_admins(listener):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    listener.set_alert_bot(bot)
    client = MagicMock()
    client.disconnect = AsyncMock()
    listener.client = client
    db = MagicMock(delete_setting=AsyncMock())
    with patch.object(tl_mod, "db_manager", db), \
         patch.object(tl_mod, "settings", SimpleNamespace(admin_ids={11, 22}, TELETHON_SESSION="x")):
        await listener._handle_auth_revoked()
        await settle(listener)
    db.delete_setting.assert_awaited_once_with("telethon_session")
    client.disconnect.assert_awaited_once()
    assert sorted(c.kwargs["chat_id"] for c in bot.send_message.await_args_list) == [11, 22]
    assert listener.client is None


async def test_safe_disconnect_always_disconnects_and_is_bounded():
    client = MagicMock()
    client.is_connected.return_value = False
    client.disconnect = AsyncMock()
    await TelethonListener._safe_disconnect(client)
    client.disconnect.assert_awaited_once()

    async def hang():
        await asyncio.sleep(10)

    slow = MagicMock()
    slow.disconnect = MagicMock(side_effect=lambda: hang())
    started = time.monotonic()
    await TelethonListener._safe_disconnect(slow, timeout=0.05)
    assert time.monotonic() - started < 2


# --------------------------------------------------------------------------- LS-L1 / LS-L2

async def test_migration_service_messages_reach_the_migration_handler(listener):
    migrate = AsyncMock()
    migrate_to = UpdateNewMessage(
        message=MessageService(id=1, peer_id=PeerChat(500), date=None, action=MessageActionChatMigrateTo(channel_id=1500)),
        pts=1, pts_count=1
    )
    migrate_from = UpdateNewChannelMessage(
        message=MessageService(id=1, peer_id=PeerChannel(1500), date=None, action=MessageActionChannelMigrateFrom(title="g", chat_id=500)),
        pts=1, pts_count=1
    )
    with patch.object(listener, "_handle_chat_migration", migrate):
        await listener._handle_service_update(migrate_to)
        await listener._handle_service_update(migrate_from)
        await listener._handle_service_update(UpdateNewMessage(message=make_msg(3), pts=1, pts_count=1))
        await settle(listener)
    assert migrate.await_args_list == [call(-500, 1500), call(-500, 1500)]


def test_raw_migration_handler_is_registered():
    lst = TelethonListener()
    registered = []
    lst.client = MagicMock()
    lst.client.add_event_handler = MagicMock(side_effect=lambda cb, ev: registered.append((cb, ev)))
    lst._register_event_handlers()
    raw = [ev for cb, ev in registered if isinstance(ev, tl_mod.events.Raw)]
    assert raw and set(raw[0].types) == {UpdateNewMessage, UpdateNewChannelMessage}


async def test_prune_entity_cache_keeps_pair_endpoints_and_self(listener):
    rows = {(int(f"-100{5000 + i}"), i, None, None, f"c{i}") for i in range(2500)}
    keep = {(-100111, 1, None, None, "src"), (-100222, 2, None, None, "tgt"), (42, 3, "me", None, "Me"),
            (-100333, 4, "tgt_name", None, "Named target")}
    rows |= keep
    listener.client = SimpleNamespace(session=SimpleNamespace(_entities=rows))
    listener._cached_me = SimpleNamespace(id=42)
    pairs = [
        make_pair(1, -100111, -100222),
        ChannelPair(id=2, user_id=1, source_channel="-100444", source_id=None, target_channel="@tgt_name", is_active=True),
    ]
    with patch.object(listener, "_get_active_pairs", AsyncMock(return_value=pairs)):
        await listener._prune_entity_cache()
    assert rows == keep


# --------------------------------------------------------------------------- LS-M4 / M4 / DB23: edit & delete sync

@pytest.fixture
async def sync_db(tmp_path):
    db = DatabaseManager(str(tmp_path / "sync.db"))
    await db.init_db()
    await db.get_or_create_user(601, "Sync", "sync_user")
    pid = await db.add_channel_pair(user_id=601, source_channel="@src_sync", source_title="S",
                                    target_channel="@tgt_sync", target_title="T")
    await db.update_pair_ids(pid, source_id=-100555000, target_id=-100777000)
    # Priced listing (text), unpriced news photo, priced album (caption on the first item only)
    await db.record_cloned_message(pid, 990010, target_msg_id=110, media_type="text", price=65000.0,
                                   source_channel="@src_sync", last_caption="Uy <b>65000$</b>")
    await db.record_cloned_message(pid, 990011, target_msg_id=111, media_type="photo", price=0.0,
                                   source_channel="@src_sync", last_caption="Yangiliklar")
    for src, tgt in ((990020, 120), (990021, 121)):
        await db.record_cloned_message(pid, src, target_msg_id=tgt, media_group_id="g1", media_type="media_group",
                                       price=900.0, source_channel="@src_sync", last_caption="Albom")
    yield db, pid
    await db.close()


async def _statuses(db, pid):
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT source_msg_id, status, last_caption FROM cloned_messages WHERE pair_id = ?", (pid,))
        return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}


def _edit_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.edit_message_caption = AsyncMock()
    return bot


async def test_source_delete_marks_only_priced_listings_and_one_album_item(sync_db, listener):
    db, pid = sync_db
    bot = _edit_bot()
    event = SimpleNamespace(deleted_ids=[990010, 990011, 990020, 990021], chat_id=-100555000,
                            get_chat=AsyncMock(return_value=SimpleNamespace(id=555000, username=None)))
    with patch.object(tl_mod, "db_manager", db), patch.object(tl_mod.cloner_engine, "bot", bot), \
         patch("services.story_cloner_service.story_cloner_service.handle_source_message_deleted", new=AsyncMock()):
        await listener._handle_message_deleted(event)
    text_call = bot.edit_message_text.await_args
    assert text_call.kwargs["chat_id"] == -100777000 and text_call.kwargs["message_id"] == 110
    assert text_call.kwargs["text"].startswith(SOLD_TAG)
    assert bot.edit_message_caption.await_count == 1
    assert bot.edit_message_caption.await_args.kwargs["message_id"] == 120
    statuses = await _statuses(db, pid)
    assert statuses[990010][0] == "sold" and statuses[990020][0] == "sold"
    assert statuses[990011][0] == "active" and statuses[990021][0] == "active"


async def test_delete_sync_skips_paused_pairs(sync_db, listener):
    db, pid = sync_db
    await db.set_pair_active_by_owner(pid, False)
    bot = _edit_bot()
    event = SimpleNamespace(deleted_ids=[990010], chat_id=-100555000, get_chat=AsyncMock(return_value=None))
    with patch.object(tl_mod, "db_manager", db), patch.object(tl_mod.cloner_engine, "bot", bot), \
         patch("services.story_cloner_service.story_cloner_service.handle_source_message_deleted", new=AsyncMock()):
        await listener._handle_message_deleted(event)
    bot.edit_message_text.assert_not_called()
    assert (await _statuses(db, pid))[990010][0] == "active"


async def test_source_edit_updates_caption_strips_custom_emoji_and_persists_it(sync_db, listener):
    db, pid = sync_db
    bot = _edit_bot()
    processed = 'Yangi narx <tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji> <b>60000$</b>'
    event = make_event(make_msg(990010, text="Yangi narx 60000$", edit_date=datetime(2026, 9, 29)), chat_id=-100555000)
    with patch.object(tl_mod, "db_manager", db), patch.object(tl_mod.cloner_engine, "bot", bot), \
         patch.object(tl_mod.cloner_engine, "process_post_text", AsyncMock(return_value=processed)):
        await listener._handle_message_edited(event)
        # The same edit delivered again (reaction / view counter update) is ignored
        await listener._handle_message_edited(event)
    assert bot.edit_message_text.await_count == 1
    sent = bot.edit_message_text.await_args.kwargs["text"]
    assert "tg-emoji" not in sent and "🔥" in sent
    assert (await _statuses(db, pid))[990010][1] == processed


async def test_edit_without_edit_date_is_a_reaction_update(sync_db, listener):
    db, _pid = sync_db
    bot = _edit_bot()
    process = AsyncMock(return_value="new")
    with patch.object(tl_mod, "db_manager", db), patch.object(tl_mod.cloner_engine, "bot", bot), \
         patch.object(tl_mod.cloner_engine, "process_post_text", process):
        await listener._handle_message_edited(make_event(make_msg(990010, edit_date=None), chat_id=-100555000))
    process.assert_not_called()
    bot.edit_message_text.assert_not_called()


async def test_sold_keyword_on_news_post_is_a_regular_edit_and_long_captions_are_fitted(sync_db, listener):
    db, pid = sync_db
    bot = _edit_bot()
    long_processed = "Chipta sotildi! " + "😀" * 700   # 1400 UTF-16 units of emoji alone
    event = make_event(make_msg(990011, text="Chipta sotildi!", edit_date=datetime(2026, 9, 29)), chat_id=-100555000)
    with patch.object(tl_mod, "db_manager", db), patch.object(tl_mod.cloner_engine, "bot", bot), \
         patch.object(tl_mod.cloner_engine, "process_post_text", AsyncMock(return_value=long_processed)):
        await listener._handle_message_edited(event)
    caption = bot.edit_message_caption.await_args.kwargs["caption"]
    assert not caption.startswith(SOLD_TAG)
    assert TextProcessor.get_visible_text_length(caption) <= 1024
    assert (await _statuses(db, pid))[990011][0] == "active"


async def test_sold_keyword_on_priced_listing_marks_it_sold(sync_db, listener):
    db, pid = sync_db
    bot = _edit_bot()
    event = make_event(make_msg(990010, text="Uy sotildi", edit_date=datetime(2026, 9, 30)), chat_id=-100555000)
    with patch.object(tl_mod, "db_manager", db), patch.object(tl_mod.cloner_engine, "bot", bot), \
         patch("services.story_cloner_service.story_cloner_service.handle_source_message_deleted", new=AsyncMock()):
        await listener._handle_message_edited(event)
    assert bot.edit_message_text.await_args.kwargs["text"].startswith(SOLD_TAG)
    assert (await _statuses(db, pid))[990010][0] == "sold"


def test_destination_addressing_never_uses_invite_links():
    target = TelethonListener._record_target_chat
    assert target({"pair_target_id": 777000}) == -100777000
    assert target({"pair_target_id": "-100777000"}) == -100777000
    assert target({"pair_target_id": None, "target_channel": "@dest_channel"}) == "@dest_channel"
    assert target({"pair_target_id": None, "target_channel": "-1001234567"}) == -1001234567
    assert target({"pair_target_id": None, "target_channel": "https://t.me/+AbCdEf", "pair_target_channel": "https://t.me/+AbCdEf"}) is None


def test_sold_keywords_and_text_helpers():
    assert not SOLD_KEYWORDS_RE.search("1000 tickets sold in a day")
    assert SOLD_KEYWORDS_RE.search("Sold out!")
    assert SOLD_KEYWORDS_RE.search("Uy sotildi")
    assert SOLD_KEYWORDS_RE.search("Квартира продано")
    assert strip_tg_emoji('<tg-emoji emoji-id="5">🔥</tg-emoji> hot') == "🔥 hot"
    fitted = TelethonListener._fit_for_edit("😀" * 600, is_text_message=False)
    assert TextProcessor.get_visible_text_length(fitted) <= 1024 and fitted.endswith("…")


# --------------------------------------------------------------------------- LS-M5: forum topics

def test_forum_topic_detection():
    general = make_msg(1)
    top_level = make_msg(2, reply_to=MessageReplyHeader(forum_topic=True, reply_to_msg_id=5))
    reply_in_topic = make_msg(3, reply_to=MessageReplyHeader(forum_topic=True, reply_to_msg_id=77, reply_to_top_id=5))
    reply_in_general = make_msg(4, reply_to=MessageReplyHeader(reply_to_msg_id=3))
    assert [message_topic_id(m) for m in (general, top_level, reply_in_topic, reply_in_general)] == [1, 5, 5, 1]
    assert message_in_topic(top_level, 5) and not message_in_topic(general, 5) and message_in_topic(general, None)
    assert topic_iter_kwargs(5) == {"reply_to": 5}
    assert topic_iter_kwargs(1) == {} and topic_iter_kwargs(None) == {}


async def test_realtime_clones_only_the_configured_source_topic(listener):
    pair = make_pair(8, -100321, -100654, source_topic_id=5)
    clone = AsyncMock(return_value=True)
    with patch.object(tl_mod, "db_manager", fake_db([pair])), \
         patch.object(tl_mod.cloner_engine, "clone_single_message", clone):
        await listener._handle_new_message(make_event(
            make_msg(1, reply_to=MessageReplyHeader(forum_topic=True, reply_to_msg_id=5)), chat_id=-100321))
        await listener._handle_new_message(make_event(
            make_msg(2, reply_to=MessageReplyHeader(forum_topic=True, reply_to_msg_id=9)), chat_id=-100321))
        await listener._handle_new_message(make_event(make_msg(3), chat_id=-100321))
        await listener._drain_pair_queues(5)
    assert [c.args[0].id for c in clone.await_args_list] == [1]


async def test_catch_up_and_history_scan_only_the_source_topic(tmp_path, listener):
    db = DatabaseManager(str(tmp_path / "topic.db"))
    await db.init_db()
    try:
        await db.get_or_create_user(503, "Topic", "topic_user")
        pid = await db.add_channel_pair(user_id=503, source_channel="@src_topic", source_title="S",
                                        target_channel="@tgt_topic", target_title="T")
        await db.update_pair_topics(pid, source_topic_id=5, target_topic_id=None)
        await db.update_pair_last_seen_msg_id(pid, 882100)
        pair = await db.get_pair_by_id(pid)
        calls = []

        async def iter_messages(entity, **kwargs):
            calls.append(kwargs)
            if False:
                yield None

        client = MagicMock()
        client.is_connected.return_value = True
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_messages = AsyncMock(return_value=[SimpleNamespace(id=882110)])
        client.iter_messages = iter_messages
        listener.client = client
        with patch.object(tl_mod, "db_manager", db), \
             patch.object(listener, "resolve_entity", AsyncMock(return_value=SimpleNamespace(id=4444))):
            await listener.catch_up_pair_messages(pair)
            await listener.clone_history(pair, limit=5)
        assert [c.get("reply_to") for c in calls] == [5, 5]
    finally:
        await db.close()


# --------------------------------------------------------------------------- LS-M9: entity resolution

async def test_resolved_entities_are_cached_with_their_access_hash(listener):
    chan = channel(123)
    client = FakeTelethonClient(entities={-100123: chan})
    listener.client = client
    assert await listener.resolve_entity("-100123") is chan
    assert await listener.resolve_entity(123) is chan
    assert client.get_entity_calls == [-100123]


async def test_unknown_ids_fall_back_to_one_unlimited_dialog_scan(listener):
    hidden, other = channel(456), channel(789)
    dialogs = [
        SimpleNamespace(id=-100456, entity=hidden, is_channel=True, is_group=False),
        SimpleNamespace(id=-100789, entity=other, is_channel=True, is_group=False),
    ]
    client = FakeTelethonClient(dialogs=dialogs)
    listener.client = client
    assert await listener.resolve_entity(-100456) is hidden
    assert await listener.resolve_entity("-100789") is other
    assert client.dialog_scans == [None]


async def test_a_failed_dialog_scan_is_not_cached(listener):
    class FlakyDialogsClient(FakeTelethonClient):
        async def iter_dialogs(self, limit=None):
            self.dialog_scans.append(limit)
            raise ConnectionError("network down")
            yield  # pragma: no cover - makes this an async generator

    client = FlakyDialogsClient()
    listener.client = client
    assert await listener.resolve_entity(-100456) is None
    assert await listener.resolve_entity(-100456) is None
    assert client.dialog_scans == [None, None]


async def test_invite_link_of_a_joined_chat_is_not_imported_again(listener):
    chan = channel(321)
    client = FakeTelethonClient(rpc={"CheckChatInviteRequest": ChatInviteAlready(chat=chan)})
    listener.client = client
    assert await listener.resolve_entity("https://t.me/+AbCdEf123") is chan
    assert await listener.resolve_entity("https://t.me/+AbCdEf123") is chan
    assert "ImportChatInviteRequest" not in client.requests
    assert client.requests.count("CheckChatInviteRequest") == 1


async def test_destination_invite_is_never_imported(listener):
    invite = ChatInvite(title="Private", photo=PhotoEmpty(id=0), participants_count=5, color=0)
    client = FakeTelethonClient(rpc={"CheckChatInviteRequest": invite})
    listener.client = client
    assert await listener.resolve_entity("+XyZ987", join_invite=False) is None
    assert "ImportChatInviteRequest" not in client.requests


async def test_failed_source_join_is_reported_with_the_pair(listener, caplog):
    client = FakeTelethonClient(entities={-100654: channel(654, left=True)},
                                rpc={"JoinChannelRequest": ChannelPrivateError(request=None)})
    listener.client = client
    with caplog.at_level(logging.WARNING, logger="services.telethon_listener"):
        assert await listener.join_and_monitor_channel("-100654", pair_id=7) is None
    assert "pair #7" in caplog.text
    assert 654 not in listener._monitored_channels


async def test_refresh_never_joins_destinations(listener):
    client = FakeTelethonClient(entities={-100111: channel(111), -100222: channel(222, left=True)})
    listener.client = client
    pair = ChannelPair(id=9, user_id=1, source_channel="@src", source_id=-100111, target_channel="-100222",
                       target_id=None, is_active=True)
    invite_pair = ChannelPair(id=10, user_id=1, source_channel="@src", source_id=-100111,
                              target_channel="https://t.me/+PrivDest", target_id=None, is_active=True)
    db = MagicMock(get_all_active_pairs=AsyncMock(return_value=[pair, invite_pair]),
                   update_pair_source_id=AsyncMock(), update_pair_target_id=AsyncMock())
    catch_up = AsyncMock(return_value={})
    with patch.object(tl_mod, "db_manager", db), patch.object(listener, "catch_up_all_active_pairs", catch_up):
        await listener.refresh_monitored_channels(catchup_floors={9: 50})
        await settle(listener)
    assert "JoinChannelRequest" not in client.requests
    assert "CheckChatInviteRequest" not in client.requests and "ImportChatInviteRequest" not in client.requests
    db.update_pair_target_id.assert_awaited_once_with(9, -100222)
    assert listener._monitored_channels == {111}
    catch_up.assert_called_once_with(floors={9: 50})


# --------------------------------------------------------------------------- run.py: polling, health, lock, signals

def test_polling_options_match_aiogram_3_signature():
    from aiogram import Dispatcher
    params = inspect.signature(Dispatcher.start_polling).parameters
    assert run.POLLING_KWARGS == {"handle_signals": False, "close_bot_session": False}
    assert all(name in params for name in run.POLLING_KWARGS)
    assert "handle_in_background" not in params
    source = inspect.getsource(run)
    assert "handle_in_background" not in source
    assert "MYSQL_HOST" not in source and "CommandPoller" not in source


async def test_bot_startup_retries_network_errors_with_backoff():
    bot = MagicMock()
    bot.delete_webhook = AsyncMock(side_effect=[OSError("offline"), OSError("offline"), True])
    bot.me = AsyncMock(return_value=SimpleNamespace(username="clone_bot", id=1))
    user = await run.prepare_bot_for_polling(bot, False, asyncio.Event(), initial_delay=0.01)
    assert user.username == "clone_bot"
    assert bot.delete_webhook.await_count == 3


async def test_bot_startup_stops_on_shutdown_and_raises_for_a_revoked_token():
    from aiogram.exceptions import TelegramUnauthorizedError
    stopped = asyncio.Event()
    stopped.set()
    assert await run.prepare_bot_for_polling(MagicMock(), False, stopped) is None
    bot = MagicMock()
    bot.delete_webhook = AsyncMock(side_effect=TelegramUnauthorizedError(method=MagicMock(), message="Unauthorized"))
    with pytest.raises(TelegramUnauthorizedError):
        await run.prepare_bot_for_polling(bot, False, asyncio.Event(), initial_delay=0.01)


async def test_admin_bot_failures_never_escape_and_polling_uses_safe_options(monkeypatch):
    monkeypatch.setattr(run, "runtime_state", run.RuntimeState())
    stop = asyncio.Event()
    admin_bot = MagicMock()
    admin_bot.delete_webhook = AsyncMock(side_effect=[RuntimeError("boom"), True])
    admin_bot.me = AsyncMock(return_value=SimpleNamespace(username="admin_bot", id=2))
    admin_dp = MagicMock()

    async def polling(bot, **kwargs):
        stop.set()

    admin_dp.start_polling = AsyncMock(side_effect=polling)
    await asyncio.wait_for(run.run_admin_polling(admin_bot, admin_dp, stop, False, initial_delay=0.01), timeout=5)
    admin_dp.start_polling.assert_awaited_once_with(admin_bot, **run.POLLING_KWARGS)
    assert run.runtime_state.admin_polling_alive is False


async def test_admin_bot_with_revoked_token_is_disabled_quietly(monkeypatch):
    from aiogram.exceptions import TelegramUnauthorizedError
    monkeypatch.setattr(run, "runtime_state", run.RuntimeState())
    admin_bot = MagicMock()
    admin_bot.delete_webhook = AsyncMock(side_effect=TelegramUnauthorizedError(method=MagicMock(), message="Unauthorized"))
    admin_dp = MagicMock(start_polling=AsyncMock())
    await asyncio.wait_for(run.run_admin_polling(admin_bot, admin_dp, asyncio.Event(), False, initial_delay=0.01), timeout=5)
    admin_dp.start_polling.assert_not_called()


def test_health_payload_reports_degraded_polling(monkeypatch):
    state = run.RuntimeState()
    monkeypatch.setattr(run, "runtime_state", state)
    payload = run.build_health_payload()
    assert payload["status"] == "ok" and payload["pid"] == os.getpid()
    assert payload["service"] == run.HEALTH_SERVICE_NAME
    state.db_ready, state.polling_alive = True, False
    payload = run.build_health_payload()
    assert payload["status"] == "degraded" and payload["bot"] == "stopped" and payload["polling_alive"] is False
    state.polling_alive = True
    assert run.build_health_payload()["status"] == "ok"


async def test_ready_endpoint_reports_readiness(monkeypatch):
    state = run.RuntimeState()
    monkeypatch.setattr(run, "runtime_state", state)
    port = free_port()
    monkeypatch.setenv("PORT", str(port))
    runner = await run.start_health_server()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/ready") as resp:
                assert resp.status == 503
            state.db_ready, state.polling_alive = True, True
            async with session.get(f"http://127.0.0.1:{port}/ready") as resp:
                assert resp.status == 200
                assert (await resp.json())["ready"] is True
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                assert (await resp.json())["pid"] == os.getpid()
    finally:
        await runner.cleanup()


@pytest.mark.skipif(os.name != "nt", reason="SO_EXCLUSIVEADDRUSE exists on Windows only")
async def test_http_port_cannot_be_taken_over_on_windows(monkeypatch):
    """While the bot listens on 0.0.0.0:<port>, nothing else may bind that port - not even on 127.0.0.1, which
    is what Docker does when it publishes a container port - so the watchdog's probes and the tunnel traffic
    can never be diverted to another process."""
    port = free_port()
    monkeypatch.setenv("PORT", str(port))
    runner = await run.start_health_server()
    try:
        assert [address[1] for address in runner.addresses] == [port]
        for host in ("127.0.0.1", "0.0.0.0"):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with pytest.raises(OSError):
                    probe.bind((host, port))
            finally:
                probe.close()
    finally:
        await runner.cleanup()
    # A restarted bot gets the same port straight away
    again = run.bind_http_socket("0.0.0.0", port)
    again.close()


async def test_http_server_uses_a_fallback_port_when_the_configured_one_is_taken(monkeypatch):
    port = free_port()
    monkeypatch.setenv("PORT", str(port))
    real_bind = run.bind_http_socket
    attempts = []

    def bind(host, candidate, backlog=128):
        attempts.append(candidate)
        if candidate == port:
            raise OSError(10048, "Only one usage of each socket address is normally permitted")
        return real_bind("127.0.0.1", 0, backlog)  # stand-in for the fallback port, never a well-known one

    monkeypatch.setattr(run, "bind_http_socket", bind)
    runner = await run.start_health_server()
    try:
        bound = [address[1] for address in runner.addresses]
        assert attempts[:2] == [port, 8000]
        assert len(bound) == 1 and bound[0] != port
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{bound[0]}/health") as resp:
                assert resp.status == 200
    finally:
        await runner.cleanup()


def test_instance_lock_is_exclusive_and_released(tmp_path):
    lock_file = str(tmp_path / "app.lock")
    first = run._lock_file_handle(lock_file)
    assert first is not None
    try:
        assert run._lock_file_handle(lock_file) is None
        # Another process cannot take it either (a real OS lock, not a check-then-write PID file)
        if os.name == "nt":
            code = ("import msvcrt,sys\nf=open(sys.argv[1],'a+b');f.seek(0)\n"
                    "try:\n msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)\nexcept OSError:\n sys.exit(3)\n")
        else:
            code = ("import fcntl,sys\nf=open(sys.argv[1],'a+b')\n"
                    "try:\n fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept OSError:\n sys.exit(3)\n")
        result = subprocess.run([sys.executable, "-c", code, lock_file], timeout=120)
        assert result.returncode == 3
    finally:
        run._unlock_file_handle(first)
    again = run._lock_file_handle(lock_file)
    assert again is not None
    run._unlock_file_handle(again)


def test_pid_lock_writes_pid_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_lock_handle", None)
    pid_file = str(tmp_path / "app.pid")
    lock_file = str(tmp_path / "app.lock")
    assert run.acquire_pid_lock(pid_file=pid_file, lock_file=lock_file) is True
    try:
        with open(pid_file, encoding="utf-8") as f:
            assert f.read() == str(os.getpid())
        assert run._lock_file_handle(lock_file) is None
    finally:
        run.release_pid_lock(pid_file=pid_file)
    assert not os.path.exists(pid_file)
    assert run._lock_handle is None
    assert run.acquire_pid_lock(pid_file=pid_file, lock_file=str(tmp_path / "missing" / "app.lock")) is False


async def test_probe_detects_another_instance_on_the_health_port():
    payload = {"service": run.HEALTH_SERVICE_NAME, "pid": os.getpid() + 1}

    async def health(request):
        return web.json_response(payload)

    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        assert (await run.probe_running_instance(port))["pid"] == os.getpid() + 1
        payload["pid"] = os.getpid()
        assert await run.probe_running_instance(port) is None
        payload.clear()
        payload["service"] = "another-service"
        assert await run.probe_running_instance(port) is None
        payload["service"] = run.HEALTH_SERVICE_NAME   # older version without "pid"
        assert await run.probe_running_instance(port) is not None
    finally:
        await runner.cleanup()
    assert await run.probe_running_instance(port, timeout=1.0) is None


async def test_exit_signal_handlers_route_to_one_callback_and_are_removed():
    loop = asyncio.get_running_loop()
    received = asyncio.Event()
    previous = signal.getsignal(signal.SIGINT)
    installed = run.install_exit_signal_handlers(loop, lambda sig: received.set())
    try:
        assert installed
        if sys.platform == "win32":
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler) and handler is not previous
            handler(signal.SIGINT, None)
        else:
            os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(received.wait(), timeout=5)
    finally:
        run.remove_exit_signal_handlers(loop, installed)
    if sys.platform == "win32":
        assert signal.getsignal(signal.SIGINT) is previous


# --------------------------------------------------------------------------- LS-L13 / AD19: log redaction

def test_every_log_handler_redacts_phones_and_tokens(monkeypatch):
    monkeypatch.setenv("CLONER_DISABLE_FILE_LOG", "1")
    handlers = run.build_log_handlers()
    assert len(handlers) == 1
    assert any(isinstance(f, SecretRedactingFilter) for f in handlers[0].filters)
    stream = io.StringIO()
    handlers[0].setStream(stream)
    handlers[0].setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "connected (phone: %s) with %s",
                               ("79261234567", "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ_1234567"), None)
    handlers[0].handle(record)
    out = stream.getvalue()
    assert "79261234567" not in out and "ABCdef" not in out
    assert "[REDACTED_PHONE]" in out and "[REDACTED_BOT_TOKEN]" in out
    assert any(isinstance(f, SecretRedactingFilter) for f in MemoryLogHandler().filters)


def test_redaction_covers_tracebacks_and_phone_formats():
    try:
        raise ValueError("bad session enc:gAAAAABmZxyz0123456789abcdefghijklmnopqrstuvwxyzABCDEFG==")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed for +998901234567", None, exc_info)
    SecretRedactingFilter().filter(record)
    formatted = logging.Formatter("%(message)s").format(record)
    assert "+998901234567" not in formatted and "gAAAAABmZ" not in formatted
    assert sanitize_log_message("user +79261234567 logged") == "user [REDACTED_PHONE] logged"
    assert sanitize_log_message("phone: 79261234567") == "phone: [REDACTED_PHONE]"
    assert "123-45-67" not in sanitize_log_message("tel +998 (90) 123-45-67")
    for harmless in ("chat -1001234567890 message 12345", "price 65000$ at UTC+05:00", "masked +99890****67"):
        assert sanitize_log_message(harmless) == harmless


# --------------------------------------------------------------------------- LS-L6: subscription watcher

def test_paid_plan_days_left():
    now = datetime(2026, 9, 29, 12, 0, 0)
    assert paid_plan_days_left("2026-09-29T11:00:00", now) == 0
    assert paid_plan_days_left("2026-09-30T11:00:00", now) == 1
    assert paid_plan_days_left("2026-10-01T13:00:00", now) == 3
    assert paid_plan_days_left("2026-10-01T13:00:00+05:00", now) == 2
    assert paid_plan_days_left(None, now) is None


async def test_paid_plan_notices_distinguish_expired_and_expiring():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        (1, "Ali", "pro", (now - timedelta(hours=2)).isoformat()),
        (2, "<b>Vali</b>", "vip", (now + timedelta(days=2, hours=1)).isoformat()),
    ]
    db = MagicMock(get_expiring_paid_users_to_notify=AsyncMock(return_value=rows),
                   mark_paid_sub_notified=AsyncMock(), mark_user_blocked=AsyncMock())
    bot = MagicMock(send_message=AsyncMock())
    with patch.object(sw_mod, "db_manager", db):
        await SubscriptionWatcher(bot).check_and_notify_expiring_paid_subs()
    texts = {c.kwargs["chat_id"]: c.kwargs["text"] for c in bot.send_message.await_args_list}
    assert "muddati tugadi" in texts[1] and "tugamoqda" not in texts[1]
    assert "3 kundan keyin" in texts[2] and "&lt;b&gt;Vali" in texts[2]
    assert db.mark_paid_sub_notified.await_count == 2
    db.mark_user_blocked.assert_not_called()


async def test_blocked_users_are_marked_blocked():
    from aiogram.exceptions import TelegramForbiddenError
    forbidden = TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = MagicMock(
        get_expiring_paid_users_to_notify=AsyncMock(return_value=[(7, "U", "pro", (now + timedelta(days=1)).isoformat())]),
        get_expired_trial_users_to_notify=AsyncMock(return_value=[(8, "T")]),
        mark_paid_sub_notified=AsyncMock(), mark_trial_notified=AsyncMock(), mark_user_blocked=AsyncMock(),
    )
    bot = MagicMock(send_message=AsyncMock(side_effect=forbidden))
    watcher = SubscriptionWatcher(bot)
    with patch.object(sw_mod, "db_manager", db):
        await watcher.check_and_notify_expiring_paid_subs()
        await watcher.check_and_notify_expired_trials()
    assert sorted(c.args[0] for c in db.mark_user_blocked.await_args_list) == [7, 8]
    # The plan has not ended yet: only the reminder stage is recorded
    db.mark_paid_sub_notified.assert_awaited_once_with(7, expired=False)
    db.mark_trial_notified.assert_awaited_once_with(8)


# --------------------------------------------------------------------------- runtime services

async def test_system_supervisor_aclose_and_rate_limited_lag_warnings(caplog):
    thresholds = gc.get_threshold()
    try:
        sup = SystemSupervisor(memory_interval=3600)
        sup.start()
        tasks = list(sup._tasks)
        await sup.aclose()
        assert all(t.done() for t in tasks)

        noisy = SystemSupervisor(lag_threshold_ms=-1000.0, lag_report_interval=3600.0)
        noisy._is_running = True
        with caplog.at_level(logging.WARNING, logger="SystemSupervisor"):
            task = asyncio.create_task(noisy._lag_monitor_loop())
            await asyncio.sleep(1.3)
            noisy._is_running = False
            task.cancel()
            await asyncio.wait([task], timeout=2)
        assert caplog.text.count("Event loop lag detected") == 1
    finally:
        gc.set_threshold(*thresholds)


async def test_code_reloader_detects_files_restored_with_an_older_timestamp(tmp_path):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    watched = bot_dir / "handler.py"
    watched.write_text("x = 1", encoding="utf-8")
    watcher = SourceCodeWatcher(base_dir=str(tmp_path), poll_interval=0.05, debounce_seconds=0.05)
    changed = []
    got = asyncio.Event()

    def on_change(changes):
        changed.extend(changes)
        got.set()

    watcher.add_reload_callback(on_change)
    watcher.start()
    try:
        older = os.path.getmtime(watched) - 3600
        os.utime(watched, (older, older))
        await asyncio.wait_for(got.wait(), timeout=10)
    finally:
        watcher.stop()
    assert any("handler.py" in c for c in changed)


def test_phone_numbers_longer_than_e164_are_rejected():
    assert normalize_phone_number("+1234567890123456")[0] is False
    assert normalize_phone_number("+998901234567")[1] == "+998901234567"


def test_session_pool_has_no_unused_heavy_imports():
    import services.session_pool as session_pool
    assert not hasattr(session_pool, "security_vault")
    assert not hasattr(session_pool, "StringSession")
