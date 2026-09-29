"""
F5 fix verification: Mini App REST API (services/api_routes.py), supplier service and the Mini App URL
synchronizer. Everything runs against the throw-away test database from conftest; Telegram, Telethon and
the supplier panel are replaced by fakes, so no network access happens.
"""
import asyncio
import hashlib
import hmac
import itertools
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config.limits import (
    BACKFILL_MAX_MESSAGES,
    BACKFILL_MAX_MESSAGES_PRIVILEGED,
    DRIP_DELAY_MAX_MINUTES,
    STORY_MAX_PER_DAY_MAX,
    STORY_MAX_PER_DAY_MIN,
    STORY_VIDEO_DURATION_MAX,
    STORY_VIDEO_DURATION_MIN,
    WATERMARK_TEXT_MAX_CHARS,
)
from config.plans import PLANS
from config.settings import settings
from database.db_manager import DatabaseManager, db_manager
from database.models import StoreOrder, StoreProduct
from services import api_routes
from services.cache_manager import cache_manager
from services.supplier_service import SupplierService, TARGET_LINK_RE
from services.tunnel_sync_service import TunnelSyncService, is_permanent_webapp_url

_user_ids = itertools.count(930_001)


def new_uid() -> int:
    return next(_user_ids)


def make_init_data(user_id: int, token: str = None) -> str:
    """initData signed exactly like Telegram does, with the fake BOT_TOKEN from conftest."""
    token = token or settings.BOT_TOKEN
    user = {"id": user_id, "first_name": "Test", "username": f"u{user_id}"}
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def auth(user_id: int) -> dict:
    return {"X-Telegram-Init-Data": make_init_data(user_id)}


def json_headers(user_id: int) -> dict:
    return {**auth(user_id), "Content-Type": "application/json"}


class FakeBot:
    """Stands in for aiogram.Bot: chats and memberships are declared per test."""
    id = 123456789

    def __init__(self):
        self.chats = {}
        self.members = {}
        self.get_chat_calls = []
        self.on_get_chat = None
        self.create_invoice_link = AsyncMock(return_value="https://t.me/$test-invoice")
        self.send_message = AsyncMock()

    def add_channel(self, ref, chat_id, title="Kanal", username=None, chat_type="channel"):
        chat = SimpleNamespace(id=chat_id, type=chat_type, title=title, username=username)
        self.chats[ref] = chat
        self.chats[chat_id] = chat
        return chat

    def set_member(self, chat_id, user_id, status, can_post=True):
        self.members[(chat_id, user_id)] = SimpleNamespace(status=status, can_post_messages=can_post)

    async def get_chat(self, chat_id):
        self.get_chat_calls.append(chat_id)
        if self.on_get_chat is not None:
            await self.on_get_chat(chat_id)
        if chat_id not in self.chats:
            raise RuntimeError("Bad Request: chat not found")
        return self.chats[chat_id]

    async def get_chat_member(self, chat_id, user_id):
        member = self.members.get((chat_id, user_id))
        if member is None:
            raise RuntimeError("Bad Request: member not found")
        return member


@pytest.fixture
async def client():
    await db_manager.init_db()
    await db_manager.set_private_mode(False)
    app = web.Application()
    api_routes.register_api_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.fixture
def fake_bot():
    bot = FakeBot()
    with patch("services.cloner_engine.cloner_engine.bot", bot):
        yield bot


@pytest.fixture
async def temp_db(tmp_path):
    mgr = DatabaseManager(db_path=str(tmp_path / "f5_store.db"))
    await mgr.init_db()
    yield mgr
    await mgr.close()


async def make_pair(owner: int, src: str, dst: str, **fields) -> int:
    await db_manager.get_or_create_user(owner, "Owner")
    pair_id = await db_manager.add_channel_pair(user_id=owner, source_channel=src, target_channel=dst,
                                                source_title=src, target_title=dst)
    if fields:
        await db_manager.update_pair_fields(pair_id, fields)
    return pair_id


async def set_active(pair_id: int, active: bool) -> None:
    await db_manager.execute("UPDATE channel_pairs SET is_active = ? WHERE id = ?", (1 if active else 0, pair_id))


async def pair_flags(pair_id: int):
    async with db_manager.get_connection() as db:
        cur = await db.execute("SELECT is_active, paused_by_user FROM channel_pairs WHERE id = ?", (pair_id,))
        row = await cur.fetchone()
    return int(row[0]), int(row[1] or 0)


async def expire_trial(uid: int) -> None:
    await db_manager.get_or_create_user(uid, "Expired")
    await db_manager.get_user_subscription(uid)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None).isoformat()
    await db_manager.execute(
        "UPDATE subscriptions SET tier = 'free', expires_at = NULL, trial_expires_at = ? WHERE user_id = ?", (past, uid)
    )
    await cache_manager.sub_cache.delete(f"sub_{uid}")


async def grant(uid: int, tier: str) -> None:
    await db_manager.get_or_create_user(uid, "Payer")
    await db_manager.activate_subscription(uid, tier, PLANS[tier]["stars"], f"f5-test-{tier}-{uid}", days=30)


# ---------------------------------------------------------------------------------------------
# A-M2: pair creation checks the requesting user, loops and plan limits
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bot_status, user_status, user_can_post, code", [
    ("administrator", "member", True, "USER_NOT_ADMIN"),
    ("administrator", "administrator", False, "USER_NOT_ADMIN"),
    ("member", "creator", True, "BOT_NOT_ADMIN"),
])
async def test_create_pair_requires_bot_and_user_to_publish_in_target(client, fake_bot, bot_status, user_status,
                                                                      user_can_post, code):
    uid = new_uid()
    chat = fake_bot.add_channel(f"@victim_{uid}", -1009300000000 - uid, "Victim", f"victim_{uid}")
    fake_bot.set_member(chat.id, FakeBot.id, bot_status)
    fake_bot.set_member(chat.id, uid, user_status, can_post=user_can_post)

    resp = await client.post("/api/pairs", json={"source_channel": "@some_source_f5", "target_channel": f"@victim_{uid}"},
                             headers=auth(uid))

    assert resp.status == 403
    assert (await resp.json())["code"] == code
    assert await db_manager.get_user_channel_pairs(uid) == []


async def test_create_pair_unknown_target_and_invite_link_are_rejected(client, fake_bot):
    uid = new_uid()
    missing = await client.post("/api/pairs", json={"source_channel": "@src_f5_x", "target_channel": "@not_there_f5"},
                                headers=auth(uid))
    assert missing.status == 400
    assert (await missing.json())["code"] == "TARGET_NOT_FOUND"

    calls_before = len(fake_bot.get_chat_calls)
    invite = await client.post("/api/pairs", json={"source_channel": "@src_f5_x", "target_channel": "https://t.me/+AbCdEfGh1234"},
                               headers=auth(uid))
    assert invite.status == 400
    assert (await invite.json())["code"] == "TARGET_INVALID"
    assert len(fake_bot.get_chat_calls) == calls_before


async def test_create_pair_success_uses_verified_chat(client, fake_bot):
    uid = new_uid()
    chat = fake_bot.add_channel(f"@mine_{uid}", -1009310000000 - uid, "Mening kanalim", f"mine_{uid}")
    fake_bot.set_member(chat.id, FakeBot.id, "administrator")
    fake_bot.set_member(chat.id, uid, "creator")

    resp = await client.post("/api/pairs", json={"source_channel": "https://t.me/daryo_uz", "target_channel": f"@mine_{uid}",
                                                 "clean_links": False}, headers=auth(uid))

    assert resp.status == 201
    data = await resp.json()
    pair = await db_manager.get_pair_by_id(data["pair_id"])
    assert pair.user_id == uid and pair.target_id == chat.id and pair.target_title == "Mening kanalim"
    assert pair.source_channel == "@daryo_uz" and pair.clean_links is False


async def test_create_pair_rejects_cloning_loops(client, fake_bot):
    other, uid = new_uid(), new_uid()
    await make_pair(other, f"@cyc_b_{uid}", f"@cyc_a_{uid}")        # someone already clones B -> A
    chat = fake_bot.add_channel(f"@cyc_b_{uid}", -1009320000000 - uid, "B", f"cyc_b_{uid}")
    fake_bot.set_member(chat.id, FakeBot.id, "administrator")
    fake_bot.set_member(chat.id, uid, "creator")

    resp = await client.post("/api/pairs", json={"source_channel": f"@cyc_a_{uid}", "target_channel": f"@cyc_b_{uid}"},
                             headers=auth(uid))

    assert resp.status == 409
    assert (await resp.json())["code"] == "PAIR_CYCLE"
    assert await db_manager.get_user_channel_pairs(uid) == []


async def test_create_pair_plan_checks_happen_before_telegram_calls(client, fake_bot):
    limited = new_uid()
    await make_pair(limited, f"@pl_src_{limited}", f"@pl_dst_{limited}")  # trial allows one pair
    resp = await client.post("/api/pairs", json={"source_channel": "@pl_src_new", "target_channel": "@pl_dst_new"},
                             headers=auth(limited))
    assert resp.status == 409
    assert (await resp.json())["code"] == "PLAN_LIMIT"

    expired = new_uid()
    await expire_trial(expired)
    resp = await client.post("/api/pairs", json={"source_channel": "@pl_src_new", "target_channel": "@pl_dst_new"},
                             headers=auth(expired))
    assert resp.status == 402
    assert (await resp.json())["code"] == "SUBSCRIPTION_INACTIVE"
    assert fake_bot.get_chat_calls == []


async def test_create_pair_rechecks_plan_limit_right_before_insert(client, fake_bot):
    uid = new_uid()
    chat = fake_bot.add_channel(f"@race_{uid}", -1009330000000 - uid, "Race", f"race_{uid}")
    fake_bot.set_member(chat.id, FakeBot.id, "administrator")
    fake_bot.set_member(chat.id, uid, "creator")

    async def concurrent_insert(_chat_id):
        # Another request of the same user creates a pair while this one talks to Telegram
        fake_bot.on_get_chat = None
        await make_pair(uid, f"@race_other_src_{uid}", f"@race_other_dst_{uid}")

    fake_bot.on_get_chat = concurrent_insert
    resp = await client.post("/api/pairs", json={"source_channel": "@race_src_f5", "target_channel": f"@race_{uid}"},
                             headers=auth(uid))

    assert resp.status == 409
    assert (await resp.json())["code"] == "PLAN_LIMIT"
    assert len(await db_manager.get_user_channel_pairs(uid)) == 1


# ---------------------------------------------------------------------------------------------
# Toggle / is_active go through set_pair_active_by_owner
# ---------------------------------------------------------------------------------------------

async def test_toggle_pause_marks_owner_paused_and_resume_respects_plan(client):
    uid = new_uid()
    first = await make_pair(uid, f"@tg_src_a_{uid}", f"@tg_dst_a_{uid}")
    second = await make_pair(uid, f"@tg_src_b_{uid}", f"@tg_dst_b_{uid}")
    await set_active(first, True)
    await set_active(second, False)

    blocked = await client.post(f"/api/pairs/{second}/toggle", headers=auth(uid))
    assert blocked.status == 409
    assert (await blocked.json())["code"] == "PLAN_LIMIT"

    paused = await client.post(f"/api/pairs/{first}/toggle", headers=auth(uid))
    assert paused.status == 200
    assert await paused.json() == {"status": "ok", "is_active": False}
    assert await pair_flags(first) == (0, 1)

    resumed = await client.post(f"/api/pairs/{second}/toggle", headers=auth(uid))
    assert resumed.status == 200
    assert (await resumed.json())["is_active"] is True
    assert await pair_flags(second) == (1, 0)


async def test_toggle_requires_active_subscription(client):
    uid = new_uid()
    await expire_trial(uid)
    pid = await make_pair(uid, f"@exp_src_{uid}", f"@exp_dst_{uid}")
    await set_active(pid, False)
    resp = await client.post(f"/api/pairs/{pid}/toggle", headers=auth(uid))
    assert resp.status == 402
    assert (await resp.json())["code"] == "SUBSCRIPTION_INACTIVE"


async def test_update_pair_is_active_never_uses_generic_updater(client):
    uid = new_uid()
    pid = await make_pair(uid, f"@upd_src_{uid}", f"@upd_dst_{uid}")
    await set_active(pid, True)
    with patch.object(db_manager, "update_pair_fields", wraps=db_manager.update_pair_fields) as spy:
        resp = await client.put(f"/api/pairs/{pid}", json={"is_active": False, "clean_links": False}, headers=auth(uid))
    assert resp.status == 200
    assert spy.call_count == 1
    assert "is_active" not in spy.call_args.args[1]
    assert await pair_flags(pid) == (0, 1)
    assert (await resp.json())["pair"]["is_active"] is False


async def test_admin_toggle_bypasses_plan_limit(client):
    uid = new_uid()
    await db_manager.get_or_create_user(uid, "Admin")
    await db_manager.set_admin_status(uid, True)
    first = await make_pair(uid, f"@adm_src_a_{uid}", f"@adm_dst_a_{uid}")
    second = await make_pair(uid, f"@adm_src_b_{uid}", f"@adm_dst_b_{uid}")
    await set_active(first, True)
    await set_active(second, False)
    resp = await client.post(f"/api/pairs/{second}/toggle", headers=auth(uid))
    assert resp.status == 200 and (await resp.json())["is_active"] is True


# ---------------------------------------------------------------------------------------------
# A-M1: rate limiting and backfill limits
# ---------------------------------------------------------------------------------------------

def test_sliding_window_limiter_counts_and_expires():
    limiter = api_routes.SlidingWindowRateLimiter()
    assert all(limiter.hit("k", 3, 60.0, now=100.0 + i) == 0 for i in range(3))
    retry = limiter.hit("k", 3, 60.0, now=110.0)
    assert retry == pytest.approx(50.0)
    assert limiter.hit("other", 3, 60.0, now=110.0) == 0
    assert limiter.hit("k", 3, 60.0, now=160.5) == 0  # the oldest hit left the window


def test_route_rate_limits_reference_registered_routes():
    app = web.Application()
    api_routes.register_api_routes(app)
    registered = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None}
    for key in api_routes.ROUTE_RATE_LIMITS:
        assert key in registered, key


async def test_general_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(api_routes, "GENERAL_RATE_LIMIT", 3)
    uid = new_uid()
    for _ in range(3):
        assert (await client.get("/api/feed", headers=auth(uid))).status == 200
    limited = await client.get("/api/feed", headers=auth(uid))
    assert limited.status == 429
    assert int(limited.headers["Retry-After"]) >= 1
    body = await limited.json()
    assert body["code"] == "RATE_LIMITED" and body["retry_after"] >= 1
    # Budgets are per user
    assert (await client.get("/api/feed", headers=auth(new_uid()))).status == 200


async def test_checkout_uses_plan_prices_and_is_rate_limited(client, fake_bot):
    uid = new_uid()
    for _ in range(5):
        resp = await client.post("/api/billing/checkout", json={"tier": "pro"}, headers=auth(uid))
        assert resp.status == 200
    kwargs = fake_bot.create_invoice_link.await_args.kwargs
    assert kwargs["prices"][0].amount == PLANS["pro"]["stars"]
    assert json.loads(kwargs["payload"]) == {"t": "pro", "u": uid}
    limited = await client.post("/api/billing/checkout", json={"tier": "pro"}, headers=auth(uid))
    assert limited.status == 429 and "Retry-After" in limited.headers


async def test_checkout_invoice_failure_is_502_not_500(client, fake_bot):
    fake_bot.create_invoice_link = AsyncMock(side_effect=RuntimeError("Bad Request"))
    resp = await client.post("/api/billing/checkout", json={"tier": "vip"}, headers=auth(new_uid()))
    assert resp.status == 502
    assert (await resp.json())["code"] == "INVOICE_FAILED"


async def test_backfill_single_running_job_per_user_and_route_budget(client, fake_bot, monkeypatch):
    from services.telethon_listener import telethon_listener
    uid = new_uid()
    await grant(uid, "vip")
    p1 = await make_pair(uid, f"@bf_src_a_{uid}", f"@bf_dst_a_{uid}")
    p2 = await make_pair(uid, f"@bf_src_b_{uid}", f"@bf_dst_b_{uid}")
    await set_active(p1, True)
    await set_active(p2, True)
    release = asyncio.Event()
    seen_limits = []

    async def fake_clone_history(pair, limit=None, progress_callback=None):
        seen_limits.append(limit)
        await release.wait()
        return {"total": 1, "cloned": 1, "failed": 0, "status": "completed"}

    monkeypatch.setattr(telethon_listener, "is_connected", lambda: True)
    monkeypatch.setattr(telethon_listener, "clone_history", fake_clone_history)
    try:
        started = await client.post(f"/api/pairs/{p1}/backfill", json={"count": BACKFILL_MAX_MESSAGES_PRIVILEGED},
                                    headers=auth(uid))
        assert started.status == 202
        other_pair = await client.post(f"/api/pairs/{p2}/backfill", json={"count": 10}, headers=auth(uid))
        assert other_pair.status == 409 and (await other_pair.json())["code"] == "ALREADY_RUNNING"
        same_pair = await client.post(f"/api/pairs/{p1}/backfill", json={"count": 10}, headers=auth(uid))
        assert same_pair.status == 409
        limited = await client.post(f"/api/pairs/{p2}/backfill", json={"count": 10}, headers=auth(uid))
        assert limited.status == 429 and (await limited.json())["code"] == "RATE_LIMITED"
    finally:
        release.set()
        for _ in range(20):
            await asyncio.sleep(0.01)
        telethon_listener.active_history_tasks.pop(p1, None)
    assert seen_limits == [BACKFILL_MAX_MESSAGES_PRIVILEGED]
    fake_bot.send_message.assert_awaited()
    assert "yakunlandi" in fake_bot.send_message.await_args.kwargs["text"]
    assert uid not in api_routes._user_backfills


async def test_backfill_caps_count_and_requires_active_plan_and_pair(client, monkeypatch):
    from services.telethon_listener import telethon_listener
    monkeypatch.setattr(telethon_listener, "is_connected", lambda: True)

    uid = new_uid()
    pid = await make_pair(uid, f"@bfc_src_{uid}", f"@bfc_dst_{uid}")
    await set_active(pid, True)
    too_many = await client.post(f"/api/pairs/{pid}/backfill", json={"count": BACKFILL_MAX_MESSAGES + 1}, headers=auth(uid))
    assert too_many.status == 400 and (await too_many.json())["code"] == "INVALID_FIELD"
    bad_type = await client.post(f"/api/pairs/{pid}/backfill", json={"count": "ten"}, headers=auth(uid))
    assert bad_type.status == 400

    paused_owner = new_uid()
    paused = await make_pair(paused_owner, f"@bfp_src_{paused_owner}", f"@bfp_dst_{paused_owner}")
    await set_active(paused, False)
    resp = await client.post(f"/api/pairs/{paused}/backfill", json={"count": 5}, headers=auth(paused_owner))
    assert resp.status == 409 and (await resp.json())["code"] == "PAIR_INACTIVE"

    expired = new_uid()
    await expire_trial(expired)
    epid = await make_pair(expired, f"@bfe_src_{expired}", f"@bfe_dst_{expired}")
    resp = await client.post(f"/api/pairs/{epid}/backfill", json={"count": 5}, headers=auth(expired))
    assert resp.status == 402 and (await resp.json())["code"] == "SUBSCRIPTION_INACTIVE"


# ---------------------------------------------------------------------------------------------
# A-L1: paid pair features are gated server-side
# ---------------------------------------------------------------------------------------------

PAID_CHANGES = [
    {"image_watermark_type": "text", "image_watermark_text": "@brand"},
    {"video_watermark_type": "text"},
    {"ai_paraphrase_mode": "hype"},
    {"tone_of_voice": "luxury"},
    {"drip_delay_minutes": 15},
    {"night_mode": "silent"},
]


@pytest.mark.parametrize("changes", PAID_CHANGES)
async def test_paid_pair_features_need_pro_plan(client, changes):
    uid = new_uid()  # fresh user: active 14-day trial
    pid = await make_pair(uid, f"@gate_src_{uid}", f"@gate_dst_{uid}")
    resp = await client.put(f"/api/pairs/{pid}", json=changes, headers=auth(uid))
    assert resp.status == 402
    assert (await resp.json())["code"] == "PRO_REQUIRED"
    pair = await db_manager.get_pair_by_id(pid)
    for key, value in changes.items():
        assert getattr(pair, key) != value


@pytest.mark.parametrize("changes", PAID_CHANGES)
async def test_pro_plan_unlocks_paid_pair_features(client, changes):
    uid = new_uid()
    await grant(uid, "pro")
    pid = await make_pair(uid, f"@pro_src_{uid}", f"@pro_dst_{uid}")
    resp = await client.put(f"/api/pairs/{pid}", json=changes, headers=auth(uid))
    assert resp.status == 200
    pair = await db_manager.get_pair_by_id(pid)
    for key, value in changes.items():
        assert getattr(pair, key) == value


async def test_trial_features_and_switching_paid_features_off_stay_allowed(client):
    uid = new_uid()
    pid = await make_pair(uid, f"@trial_src_{uid}", f"@trial_dst_{uid}",
                          image_watermark_type="text", image_watermark_text="@old", drip_delay_minutes=30)
    resp = await client.put(f"/api/pairs/{pid}", json={"auto_translate": True, "target_lang": "ru", "ad_action": "drop"},
                            headers=auth(uid))
    assert resp.status == 200

    # Editing a paid feature that stays on is gated...
    edit = await client.put(f"/api/pairs/{pid}", json={"image_watermark_text": "@new"}, headers=auth(uid))
    assert edit.status == 402
    # ...switching it off is not
    off = await client.put(f"/api/pairs/{pid}", json={"image_watermark_type": "none", "drip_delay_minutes": 0},
                           headers=auth(uid))
    assert off.status == 200
    pair = await db_manager.get_pair_by_id(pid)
    assert pair.image_watermark_type == "none" and pair.drip_delay_minutes == 0 and pair.ad_action == "drop"


async def test_resending_an_unchanged_full_form_is_accepted(client):
    uid = new_uid()
    legacy_text = "@" + "w" * (WATERMARK_TEXT_MAX_CHARS + 20)  # longer than today's limit, stored earlier
    pid = await make_pair(uid, f"@form_src_{uid}", f"@form_dst_{uid}",
                          image_watermark_type="text", image_watermark_text=legacy_text, drip_delay_minutes=30)
    form = (await (await client.get(f"/api/pairs/{pid}", headers=auth(uid))).json())["pair"]
    form["clean_links"] = not form["clean_links"]
    resp = await client.put(f"/api/pairs/{pid}", json=form, headers=auth(uid))
    assert resp.status == 200
    pair = await db_manager.get_pair_by_id(pid)
    assert pair.clean_links == form["clean_links"] and pair.image_watermark_text == legacy_text

    too_long = await client.put(f"/api/pairs/{pid}", json={"image_watermark_type": "none",
                                                           "image_watermark_text": "x" * (WATERMARK_TEXT_MAX_CHARS + 1)},
                                headers=auth(uid))
    assert too_long.status == 400


async def test_update_without_known_fields_is_400_and_noop_is_200(client):
    uid = new_uid()
    pid = await make_pair(uid, f"@noop_src_{uid}", f"@noop_dst_{uid}")
    assert (await client.put(f"/api/pairs/{pid}", json={"id": 5, "user_id": 1}, headers=auth(uid))).status == 400
    pair = await db_manager.get_pair_by_id(pid)
    same = await client.put(f"/api/pairs/{pid}", json={"clean_links": pair.clean_links}, headers=auth(uid))
    assert same.status == 200


async def test_drip_delay_uses_shared_limit(client):
    uid = new_uid()
    await grant(uid, "pro")
    pid = await make_pair(uid, f"@drip_src_{uid}", f"@drip_dst_{uid}")
    ok = await client.put(f"/api/pairs/{pid}", json={"drip_delay_minutes": DRIP_DELAY_MAX_MINUTES}, headers=auth(uid))
    assert ok.status == 200
    bad = await client.put(f"/api/pairs/{pid}", json={"drip_delay_minutes": DRIP_DELAY_MAX_MINUTES + 1}, headers=auth(uid))
    assert bad.status == 400


async def test_logo_watermark_must_be_an_image_inside_assets(client, tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    (assets / "logos").mkdir(parents=True)
    (assets / "logos" / "brand.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (assets / "notes.txt").write_text("x")
    (tmp_path / "secret.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(api_routes, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(api_routes, "ASSETS_DIR", str(assets))

    uid = new_uid()
    await grant(uid, "pro")
    pid = await make_pair(uid, f"@logo_src_{uid}", f"@logo_dst_{uid}")
    for bad in ("../secret.png", "assets/notes.txt", "assets/missing.png", "assets/lo\u0000go.png", ""):
        resp = await client.put(f"/api/pairs/{pid}", json={"image_watermark_type": "logo", "image_watermark_text": bad},
                                headers=auth(uid))
        assert resp.status == 400, bad
    # Switching to logo with the stored (plain text) watermark is rejected as well
    resp = await client.put(f"/api/pairs/{pid}", json={"image_watermark_type": "logo"}, headers=auth(uid))
    assert resp.status == 400

    ok = await client.put(f"/api/pairs/{pid}", json={"image_watermark_type": "logo",
                                                     "image_watermark_text": "assets/logos/brand.png"},
                          headers=auth(uid))
    assert ok.status == 200
    assert (await db_manager.get_pair_by_id(pid)).image_watermark_text == "assets/logos/brand.png"


# ---------------------------------------------------------------------------------------------
# Input validation: malformed bodies are 400 JSON, never 500
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw_body", [
    '{"night_mode": []}',
    '{"target_lang": {"x": 1}}',
    '{"clone_mode": null}',
    '{"drip_delay_minutes": 1e400}',
    '{"drip_delay_minutes": NaN}',
    '{"drip_delay_minutes": 2.5}',
    '{"drip_delay_minutes": true}',
    '{"source_topic_id": 99999999999999999999}',
    '{"custom_signature": 5}',
    '{"image_watermark_text": "a\\u0000b"}',
    'not json',
    '[1, 2]',
])
async def test_malformed_pair_updates_are_rejected_with_400(client, raw_body):
    uid = new_uid()
    pid = await make_pair(uid, f"@val_src_{uid}", f"@val_dst_{uid}")
    resp = await client.put(f"/api/pairs/{pid}", data=raw_body, headers=json_headers(uid))
    assert resp.status == 400
    assert (await resp.json())["code"] in ("INVALID_FIELD", "INVALID_JSON")


async def test_other_endpoints_reject_wrong_types_with_400(client, fake_bot):
    uid = new_uid()
    cases = [
        ("post", "/api/pairs", {"source_channel": 123, "target_channel": "@x_channel"}),
        ("post", "/api/pairs", {"source_channel": "@src_ok_f5", "target_channel": "@dst_ok_f5", "clean_links": "yes"}),
        ("post", "/api/store/buy", {"product_id": "abc"}),
        ("post", "/api/store/buy", {"product_id": 1, "quantity": True}),
        ("post", "/api/store/buy", {"product_id": 1, "quantity": 5, "target_link": ["@x"]}),
        ("post", "/api/billing/checkout", {"tier": ["pro"]}),
        ("get", "/api/store/quote?product_id=1&quantity=abc", None),
        ("get", "/api/store/quote?product_id=abc&quantity=1", None),
        ("get", "/api/store/products?category=" + "c" * 150, None),
    ]
    for method, path, body in cases:
        if method == "get":
            resp = await client.get(path, headers=auth(uid))
        else:
            resp = await client.post(path, json=body, headers=auth(uid))
        assert resp.status == 400, (path, body)
        assert "code" in await resp.json()


async def test_oversized_body_gets_json_413(client):
    uid = new_uid()
    pid = await make_pair(uid, f"@big_src_{uid}", f"@big_dst_{uid}")
    body = b'{"custom_signature": "' + b"a" * (1024 * 1024 + 16) + b'"}'
    resp = await client.put(f"/api/pairs/{pid}", data=body, headers=json_headers(uid))
    assert resp.status == 413
    assert (await resp.json())["code"] == "PAYLOAD_TOO_LARGE"


# ---------------------------------------------------------------------------------------------
# A-L2 system telemetry, profile billing catalogue
# ---------------------------------------------------------------------------------------------

async def test_system_status_hides_host_details_from_non_admins(client, tmp_path, monkeypatch):
    # Never read the production log file from a test
    monkeypatch.setattr(api_routes, "APP_LOG_FILE", str(tmp_path / "app.log"))
    resp = await client.get("/api/system", headers=auth(new_uid()))
    data = await resp.json()
    assert resp.status == 200
    assert set(data["telemetry"]) == {"mtproto_connected", "mtproto_status", "server_time"}
    assert data["logs"] == [] and data["is_admin"] is False

    admin = new_uid()
    await db_manager.get_or_create_user(admin, "Admin")
    await db_manager.set_admin_status(admin, True)
    admin_data = await (await client.get("/api/system", headers=auth(admin))).json()
    for key in ("db_size_mb", "ram_mb", "runtime_version", "keep_alive_port", "db_type"):
        assert key in admin_data["telemetry"]


async def test_me_exposes_plan_catalogue_from_config(client):
    data = await (await client.get("/api/me", headers=auth(new_uid()))).json()
    plans = {p["key"]: p for p in data["billing"]["plans"]}
    assert set(plans) == {"pro", "vip"}
    for key in ("pro", "vip"):
        assert plans[key]["stars"] == PLANS[key]["stars"] and plans[key]["days"] == PLANS[key]["days"]
    assert data["subscription"]["is_trial_active"] is True


# ---------------------------------------------------------------------------------------------
# A-L8 store prices come from the server
# ---------------------------------------------------------------------------------------------

async def test_store_catalogue_price_equals_quote_and_charged_price(client, fake_bot):
    await db_manager.upsert_synced_product(
        {"supplier_id": 1, "service": 930501, "name": "F5 Members", "category": "F5", "type": "Default",
         "rate": 2.2, "min": 100, "max": 5000},
        margin_percent=25.0,
    )
    uid = new_uid()
    products = (await (await client.get("/api/store/products", headers=auth(uid))).json())["products"]
    item = next(p for p in products if p["name"] == "F5 Members")
    cfg = await db_manager.get_supplier_config(1)
    expected = int((Decimal("2.2") * 50 * (100 + Decimal(str(cfg.margin_percent))) / 100).to_integral_value(ROUND_CEILING))
    assert item["display_quantity"] == 1000 and item["display_price_stars"] == expected

    quote = await client.get(f"/api/store/quote?product_id={item['id']}&quantity=1000", headers=auth(uid))
    assert quote.status == 200 and (await quote.json())["price_stars"] == expected
    out_of_range = await client.get(f"/api/store/quote?product_id={item['id']}&quantity=50", headers=auth(uid))
    assert out_of_range.status == 400 and (await out_of_range.json())["code"] == "INVALID_QUANTITY"

    buy = await client.post("/api/store/buy", json={"product_id": item["id"], "quantity": 1000, "target_link": "@f5_store"},
                            headers=auth(uid))
    assert buy.status == 200 and (await buy.json())["price_stars"] == expected
    missing = await client.post("/api/store/buy", json={"product_id": 999999999, "quantity": 1, "target_link": "@f5_store"},
                                headers=auth(uid))
    assert missing.status == 404 and (await missing.json())["code"] == "NOT_FOUND"


def test_quote_uses_exact_decimal_arithmetic():
    svc = SupplierService()
    package = StoreProduct(type="Package", supplier_rate=1.1, min_quantity=1, max_quantity=10)
    assert svc.quote_price_stars(package, 1, 0) == 55     # binary floats would round 55.00000000000001 up to 56
    assert svc.quote_price_stars(package, 3, 25) == 207   # 206.25 -> 207
    per_thousand = StoreProduct(type="Default", supplier_rate=0.15, min_quantity=100, max_quantity=100000)
    assert svc.display_quantity(per_thousand) == 1000
    assert svc.quote_price_stars(per_thousand, 1000, 25) == 10
    assert isinstance(svc.quote_price_stars(per_thousand, 100, 25), int)


# ---------------------------------------------------------------------------------------------
# Story settings use config.limits and partial updates
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"video_duration": STORY_VIDEO_DURATION_MIN - 1},
    {"video_duration": STORY_VIDEO_DURATION_MAX + 1},
    {"max_stories_per_day": STORY_MAX_PER_DAY_MIN - 1},
    {"max_stories_per_day": STORY_MAX_PER_DAY_MAX + 1},
    {"drip_delay_minutes": 1441},
    {"background_style": "luxury_black"},
    {"min_price": "abc"},
    {"max_price": -1},
    {"source_channel": "https://evil.com/x"},
])
async def test_story_settings_reject_values_outside_shared_limits(client, body):
    uid = new_uid()
    await grant(uid, "vip")
    resp = await client.post("/api/story/settings", json=body, headers=auth(uid))
    assert resp.status == 400, body


async def test_story_settings_first_save_is_partial_and_never_activates(client):
    uid = new_uid()
    await grant(uid, "vip")
    initial = await (await client.get("/api/story/settings", headers=auth(uid))).json()
    assert initial["settings"]["is_active"] is False

    body = {"min_price": 900, "max_price": 0, "video_duration": STORY_VIDEO_DURATION_MAX,
            "max_stories_per_day": STORY_MAX_PER_DAY_MAX, "background_style": "listing_blur"}
    with patch.object(db_manager, "update_story_settings", wraps=db_manager.update_story_settings) as spy:
        resp = await client.post("/api/story/settings", json=body, headers=auth(uid))
    assert resp.status == 200
    assert spy.call_args.args == (uid,)
    assert spy.call_args.kwargs == {"min_price": 900.0, "video_duration": STORY_VIDEO_DURATION_MAX,
                                    "max_stories_per_day": STORY_MAX_PER_DAY_MAX, "background_style": "listing_blur",
                                    "is_active": False}
    saved = (await resp.json())["settings"]
    assert saved["is_active"] is False and saved["background_style"] == "listing_blur"
    assert uid not in [s.user_id for s in await db_manager.get_all_active_story_settings()]


async def test_story_activation_requires_account_and_controls_monitor(client):
    from services.story_cloner_service import story_cloner_service
    uid = new_uid()
    await grant(uid, "vip")
    no_session = await client.post("/api/story/settings", json={"source_channel": "@story_src_f5", "is_active": True},
                                   headers=auth(uid))
    assert no_session.status == 409 and (await no_session.json())["code"] == "STORY_SESSION_REQUIRED"

    start = AsyncMock(return_value=True)
    stop = MagicMock()
    with patch.object(db_manager, "get_user_session_info", AsyncMock(return_value={"is_active": True})), \
            patch.object(story_cloner_service, "start_monitor_for_user", start), \
            patch.object(story_cloner_service, "stop_monitor_for_user", stop):
        no_source = await client.post("/api/story/settings", json={"is_active": True}, headers=auth(uid))
        assert no_source.status == 400 and (await no_source.json())["code"] == "STORY_SOURCE_REQUIRED"

        on = await client.post("/api/story/settings", json={"source_channel": "https://t.me/story_src_f5", "is_active": True},
                               headers=auth(uid))
        assert on.status == 200
        settings_on = (await on.json())["settings"]
        assert settings_on["is_active"] is True and settings_on["source_channel"] == "@story_src_f5"
        for _ in range(20):
            await asyncio.sleep(0.01)
        start.assert_awaited_with(uid)

        off = await client.post("/api/story/settings", json={"is_active": False}, headers=auth(uid))
        assert off.status == 200 and (await off.json())["settings"]["is_active"] is False
        stop.assert_called_with(uid)


async def test_story_settings_require_vip(client):
    resp = await client.post("/api/story/settings", json={"min_price": 800}, headers=auth(new_uid()))
    assert resp.status == 402 and (await resp.json())["code"] == "VIP_REQUIRED"


# ---------------------------------------------------------------------------------------------
# supplier_service: SSRF guard, catalogue validation, idempotent fulfilment, admin notification
# ---------------------------------------------------------------------------------------------

class FakeHttpResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeHttpSession:
    """Replacement for aiohttp.ClientSession (used as the factory and as the session)."""

    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, allow_redirects=True):
        self.calls.append({"url": url, "data": dict(data or {}), "allow_redirects": allow_redirects})
        if self.exc is not None:
            raise self.exc
        return FakeHttpResponse(self.payload)


@pytest.mark.parametrize("url", [
    "http://panel.example.com/api/v2",
    "https://127.0.0.1/api",
    "https://10.1.2.3/api",
    "https://169.254.169.254/latest",
    "https://[::1]/api",
    "https://localhost/api",
    "https://user:pw@panel.example.com/api",
    "ftp://panel.example.com/api",
    "https://panel.example.com:99999/api",
    "",
])
def test_supplier_endpoint_rejects_unsafe_urls(url):
    assert SupplierService._api_endpoint(SimpleNamespace(api_url=url)) is None


def test_supplier_endpoint_accepts_public_https():
    url = "https://panel.example.com/api/v2"
    assert SupplierService._api_endpoint(SimpleNamespace(api_url=url)) == url


async def _configure_supplier(temp_db, api_url="https://panel.example.com/api/v2", api_key="k"):
    cfg = await temp_db.get_supplier_config()
    cfg.api_key = api_key
    cfg.api_url = api_url
    await temp_db.save_supplier_config(cfg)
    return cfg


async def _paid_order(temp_db) -> int:
    cfg = await _configure_supplier(temp_db)
    await temp_db.upsert_synced_product(
        {"supplier_id": cfg.id, "service": 4242, "name": "Idem", "category": "T", "type": "Default",
         "rate": 1.0, "min": 10, "max": 10000},
        margin_percent=25.0,
    )
    product = (await temp_db.get_store_products())[0]
    order_id = await temp_db.create_store_order(StoreOrder(
        user_id=555, product_id=product.id, product_name=product.name, quantity=1000, price_stars=63,
        target_link="@idem_channel", status="awaiting_payment"))
    assert await temp_db.mark_store_order_paid(order_id, 555, f"charge-{order_id}", 63)
    return order_id


async def test_sync_catalog_refuses_unsafe_url_without_http(temp_db):
    await _configure_supplier(temp_db, api_url="http://169.254.169.254/latest")
    session = FakeHttpSession(payload=[])
    with patch("services.supplier_service.db_manager", temp_db), \
            patch("services.supplier_service.aiohttp.ClientSession", session):
        result = await SupplierService().sync_catalog()
    assert result["status"] == "error"
    assert session.calls == []


async def test_sync_catalog_skips_invalid_supplier_entries(temp_db):
    await _configure_supplier(temp_db)
    services = [
        {"service": 1, "name": "Free", "rate": "0", "min": 1, "max": 10},
        {"service": 2, "name": "Broken", "rate": "1.5", "min": 10, "max": 5},
        "junk",
        {"service": 3, "name": "Negative", "rate": "-2", "min": 1, "max": 10},
        {"service": 4, "name": "Good", "rate": "0.5", "min": 10, "max": 1000},
    ]
    session = FakeHttpSession(payload=services)
    with patch("services.supplier_service.db_manager", temp_db), \
            patch("services.supplier_service.aiohttp.ClientSession", session):
        result = await SupplierService().sync_catalog()
    assert result["status"] == "ok" and result["synced_count"] == 1 and result["skipped_count"] == 4
    assert [p.name for p in await temp_db.get_store_products()] == ["Good"]
    assert all(call["allow_redirects"] is False for call in session.calls)


async def test_paid_order_is_sent_to_supplier_only_once(temp_db):
    svc = SupplierService()
    order_id = await _paid_order(temp_db)

    async def slow_place(*args, **kwargs):
        await asyncio.sleep(0.05)
        return True, 777, "Supplier order: 777"

    place = AsyncMock(side_effect=slow_place)
    with patch("services.supplier_service.db_manager", temp_db), \
            patch.object(svc, "fetch_balance", AsyncMock(return_value=1000.0)), \
            patch.object(svc, "_place_supplier_order", place), \
            patch.object(svc, "_notify_admins_about_pending_order", AsyncMock(return_value=True)):
        first, second = await asyncio.gather(svc.fulfill_paid_order(order_id), svc.fulfill_paid_order(order_id))
        again = await svc.fulfill_paid_order(order_id)

    assert place.await_count == 1
    assert sorted([first["success"], second["success"]]) == [False, True]
    assert again["success"] is False
    order = await temp_db.get_store_order(order_id)
    assert order.status == "completed" and order.supplier_order_id == 777


async def test_supplier_timeout_routes_order_to_admin_with_reason(temp_db):
    svc = SupplierService()
    order_id = await _paid_order(temp_db)
    notify = AsyncMock(return_value=True)
    with patch("services.supplier_service.db_manager", temp_db), \
            patch.object(svc, "fetch_balance", AsyncMock(return_value=1000.0)), \
            patch("services.supplier_service.aiohttp.ClientSession", FakeHttpSession(exc=asyncio.TimeoutError())), \
            patch.object(svc, "_notify_admins_about_pending_order", notify):
        result = await svc.fulfill_paid_order(order_id)
    assert result["status"] == "pending_admin"
    assert "timed out" in notify.await_args.kwargs["reason"]
    order = await temp_db.get_store_order(order_id)
    assert order.status == "pending_admin" and order.admin_notified is True


async def test_supplier_order_with_non_numeric_id_is_completed(temp_db):
    svc = SupplierService()
    order_id = await _paid_order(temp_db)
    session = FakeHttpSession(payload={"order": "A-17"})
    with patch("services.supplier_service.db_manager", temp_db), \
            patch.object(svc, "fetch_balance", AsyncMock(return_value=1000.0)), \
            patch("services.supplier_service.aiohttp.ClientSession", session):
        result = await svc.fulfill_paid_order(order_id)
    assert result["status"] == "completed"
    order = await temp_db.get_store_order(order_id)
    assert order.status == "completed" and order.supplier_order_id is None and "A-17" in order.note
    assert session.calls[0]["data"]["action"] == "add" and session.calls[0]["allow_redirects"] is False


async def test_checkout_invoice_failure_marks_order_failed(temp_db):
    await temp_db.upsert_synced_product(
        {"supplier_id": 1, "service": 4343, "name": "Pack", "category": "T", "type": "Package",
         "rate": 1.0, "min": 1, "max": 10},
        margin_percent=25.0,
    )
    product = (await temp_db.get_store_products())[0]
    bot = SimpleNamespace(create_invoice_link=AsyncMock(side_effect=RuntimeError("Bad Request")))
    with patch("services.supplier_service.db_manager", temp_db):
        result = await SupplierService().create_checkout(bot, 12345, product.id, 1, "@my_channel")
    assert result["success"] is False and result["code"] == "INVOICE_FAILED"
    orders = await temp_db.get_user_store_orders(12345)
    assert orders[0].status == "failed"


def test_target_link_must_match_completely():
    assert TARGET_LINK_RE.fullmatch("@my_channel")
    assert TARGET_LINK_RE.fullmatch("https://t.me/my_channel/15")
    assert not TARGET_LINK_RE.fullmatch("@my_channel\n")
    assert not TARGET_LINK_RE.fullmatch("https://t.me/x y")
    assert not TARGET_LINK_RE.fullmatch("javascript:alert(1)")


async def test_admin_notification_falls_back_to_public_bot_and_escapes_html():
    svc = SupplierService()
    admin_bot = SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError("chat not found"), None]),
                                session=SimpleNamespace(close=AsyncMock()))
    public_bot = SimpleNamespace(send_message=AsyncMock())
    fake_settings = SimpleNamespace(admin_ids={11, 22}, ADMIN_BOT_TOKEN="1:admin", BOT_TOKEN="2:public")
    with patch("services.supplier_service.settings", fake_settings), \
            patch("admin_bot.bot_instance.create_admin_bot", return_value=admin_bot), \
            patch("admin_bot.public_bot.get_public_bot", return_value=public_bot):
        delivered = await svc._notify_admins_about_pending_order(
            order_id=9, user_id=7, user_full_name="<b>Eve</b>", user_username="eve", product_name="Pack <i>",
            quantity=1, price_stars=10, target_link="@chan", supplier_cost=1.0, current_balance=0.0,
            reason="Provider response: <err>")
    assert delivered is True
    # Admin 11 could not be reached through the admin bot and got the message from the public bot
    assert public_bot.send_message.await_count == 1
    assert public_bot.send_message.await_args.kwargs["chat_id"] == 11
    text = admin_bot.send_message.await_args_list[1].kwargs["text"]
    assert "&lt;b&gt;Eve&lt;/b&gt;" in text and "Pack &lt;i&gt;" in text and "&lt;err&gt;" in text
    admin_bot.session.close.assert_awaited()


async def test_admin_notification_reports_failure_without_admins():
    with patch("services.supplier_service.settings", SimpleNamespace(admin_ids=set(), ADMIN_BOT_TOKEN="", BOT_TOKEN="")):
        delivered = await SupplierService()._notify_admins_about_pending_order(
            order_id=1, user_id=1, user_full_name="", user_username="", product_name="P", quantity=1,
            price_stars=1, target_link="@c", supplier_cost=0.0, current_balance=0.0)
    assert delivered is False


# ---------------------------------------------------------------------------------------------
# A-M3: Mini App URL synchronizer
# ---------------------------------------------------------------------------------------------

def test_tunnel_sync_has_no_mysql_bridge_or_default_credentials():
    import services.tunnel_sync_service as module
    source = Path(module.__file__).read_text(encoding="utf-8")
    for marker in ("pymysql", "cloner_pass_2026", "MYSQL_", "bot_settings"):
        assert marker not in source


def test_permanent_url_detection():
    assert is_permanent_webapp_url("https://app.example.com")
    assert not is_permanent_webapp_url("https://abc-def.trycloudflare.com")
    assert not is_permanent_webapp_url("http://localhost:8080")
    assert not is_permanent_webapp_url("")


async def test_tunnel_sync_uses_webapp_url_for_menu_button(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBAPP_URL", "")
    svc = TunnelSyncService(base_dir=tmp_path)
    bot = SimpleNamespace(set_chat_menu_button=AsyncMock())
    metrics = AsyncMock(return_value="https://stale.trycloudflare.com")
    with patch.object(settings, "WEBAPP_URL", "https://app.example.com/"):
        svc._configured_url = svc.configured_url()
        with patch.object(svc, "fetch_url_from_metrics", metrics), \
                patch.object(svc, "probe_endpoint", AsyncMock(return_value=True)):
            url = await svc.sync_once(bot)
            await svc.sync_once(bot)
        assert settings.WEBAPP_URL == "https://app.example.com/"
    assert url == "https://app.example.com"
    metrics.assert_not_awaited()
    menu_calls = bot.set_chat_menu_button.await_args_list
    assert menu_calls  # global default (+ admins), only once for an unchanged URL
    assert all(c.kwargs["menu_button"].web_app.url.startswith("https://app.example.com?v=") for c in menu_calls)
    first_sync_calls = len(menu_calls)
    await svc.sync_menu_button(url, bot)
    assert len(bot.set_chat_menu_button.await_args_list) == first_sync_calls
    assert not svc.url_file.exists()
    status = json.loads((tmp_path / "logs" / "tunnel_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "healthy" and status["mode"] == "permanent"


async def test_tunnel_sync_quick_tunnel_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBAPP_URL", "")
    svc = TunnelSyncService(base_dir=tmp_path)
    bot = SimpleNamespace(set_chat_menu_button=AsyncMock())
    with patch.object(settings, "WEBAPP_URL", "http://localhost:8080"):
        svc._configured_url = svc.configured_url()
        assert svc._configured_url is None
        with patch.object(svc, "fetch_url_from_metrics", AsyncMock(return_value="https://abc.trycloudflare.com")), \
                patch.object(svc, "probe_endpoint", AsyncMock(return_value=False)):
            url = await svc.sync_once(bot)
        assert settings.WEBAPP_URL == "https://abc.trycloudflare.com"
    assert url == "https://abc.trycloudflare.com"
    assert svc.url_file.read_text(encoding="utf-8") == "https://abc.trycloudflare.com"
    status = json.loads((tmp_path / "data" / "tunnel_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "degraded" and status["mode"] == "quick_tunnel"
