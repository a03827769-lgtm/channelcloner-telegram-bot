"""
Regression tests for the VIP real-estate story pipeline (area F3): price parsing (units, separators,
catastrophic backtracking), offer/demand classification, district detection, the Tashkent prime-time
window, listing fingerprints, Telegram error classification, story targets, story deletion and revoked
customer sessions.
"""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon import types
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, RpcCallFailError

from services.listing_analyzer import format_price_usd, listing_analyzer, normalize_listing_text
from services.story_cloner_service import (
    UZS_PER_USD,
    StoryClonerService,
    classify_story_error,
    story_cloner_service,
)
from services.story_queue_service import UZB_TZ, story_queue_service


# ---------------------------------------------------------------- prices (ST2, ST5, ST6)

@pytest.mark.parametrize("text, expected", [
    ("Narxi: 65 000$", 65000),
    ("Narxi: 700$", 700),
    ("Arenda 700 $ oyiga", 700),
    ("3 xonali kvartira, 7/9 qavat, 85 m² — 78 000 у.е.", 78000),
    ("65 m2 75 000$", 75000),                      # area and price are never glued together
    ("Цена 65 тыс $", 65000),                      # multipliers
    ("1.5 mln $", 1500000),
    ("Narxi: 4 500 000 so‘m", 4500000 / UZS_PER_USD),  # UZS amounts are converted to USD
    ("5 000 000 sum", 5000000 / UZS_PER_USD),
    ("Цена 1 800 000 сўм", 1800000 / UZS_PER_USD),
    ("Narxi 650 ming so'm", 650000 / UZS_PER_USD),
    ("12 mln so'm", 12000000 / UZS_PER_USD),
    ("Narxi: 2 500 000", 2500000 / UZS_PER_USD),   # a large bare number is a sum amount, not USD
    ("Tel: +998 90 123 45 67", None),              # phone numbers are not prices
    ("", None),
])
def test_extract_price(text, expected):
    got = StoryClonerService.extract_price(text)
    if expected is None:
        assert got is None
    else:
        assert got is not None and abs(got - expected) < 1.0


@pytest.mark.parametrize("adversarial", [
    "1 " * 2048, "1," * 2048, "9" * 4096, ("1 2 3 4 5 6 7 8 9 0 " * 205)[:4096], "$" + "1 " * 2000 + "x",
])
def test_extract_price_is_linear_on_adversarial_input(adversarial):
    started = time.perf_counter()
    StoryClonerService.extract_price(adversarial)
    assert time.perf_counter() - started < 0.2


def test_prices_are_formatted_with_separators_never_exponents():
    assert format_price_usd(4_500_000) == "$4,500,000"
    assert format_price_usd(850) == "$850"
    assert format_price_usd(None, "—") == "—"
    assert format_price_usd(float("nan"), "—") == "—"


# ---------------------------------------------------------------- offers vs. demands (ST17)

@pytest.mark.parametrize("text", [
    "Аренда 2-комнатной квартиры. Нужна предоплата за 2 месяца. 700$",
    "Kvartira ijaraga beriladi. Klient bor bo‘lsa yozing. 700$",
    "Квартира в аренду, 900$, запрос на показ по телефону",
])
def test_real_offers_are_not_demands(text):
    assert StoryClonerService.is_demand_post(text) is False


@pytest.mark.parametrize("text", [
    "Chilonzordan 2 xonali kvartira kerak, 500$ gacha",
    "Ищу квартиру в Юнусабаде",
    "Mijoz uchun uy qidiryapman",
])
def test_client_requests_are_demands(text):
    assert StoryClonerService.is_demand_post(text) is True


# ---------------------------------------------------------------- districts (ST12)

@pytest.mark.parametrize("text, district", [
    ("Яккасарай тумани 3 комнатная 65 000$", "Yakkasaroy"),
    ("Mirzo Ulugʻbek 2 xona 700$", "Mirzo Ulug'bek"),
    ("Mirzo Ulug‘bek tumani", "Mirzo Ulug'bek"),
    ("Buyuk Ipak Yo‘li, трёхкомнатная квартира", "Mirzo Ulug'bek"),
    ("Дўстлик массиви 2 хона", "Yashnobod"),
    ("Янгиҳаёт, 1 хона", "Yangihayot"),
    ("Панельный дом, жаркий день", None),           # generic words are not district markers
])
def test_district_detection_keeps_uzbek_letters(text, district):
    assert listing_analyzer.analyze(text).district == district


def test_normalization_keeps_composed_letters_and_unifies_apostrophes():
    assert normalize_listing_text("Ulugʻbek Ulug‘bek Ulug’bek") == "Ulug'bek Ulug'bek Ulug'bek"
    assert "й" in normalize_listing_text("Яккасарай")
    assert "ў" in normalize_listing_text("Дўстлик")


def test_empty_or_emoji_only_captions_are_never_deduplicated():
    assert listing_analyzer.compute_fingerprint("", None, None, None, None) == ""
    assert listing_analyzer.compute_fingerprint("🔥🔥🔥", None, None, None, None) == ""
    fp = listing_analyzer.compute_fingerprint("Chilonzor 3 xona evroremont 78 m2", "Chilonzor", 3, 78.0, 65000.0)
    assert fp and fp == listing_analyzer.compute_fingerprint("Chilonzor 3 xona evroremont 78 m2", "Chilonzor", 3, 78.0, 65000.0)


# ---------------------------------------------------------------- prime time (ST34, ST18)

def test_prime_window_supports_windows_across_midnight():
    at = lambda hour: datetime(2026, 5, 1, hour, 30, tzinfo=UZB_TZ)  # noqa: E731
    assert story_queue_service.is_in_prime_hours(9, 22, at(10))
    assert not story_queue_service.is_in_prime_hours(9, 22, at(23))
    assert story_queue_service.is_in_prime_hours(20, 2, at(23))
    assert story_queue_service.is_in_prime_hours(20, 2, at(1))
    assert not story_queue_service.is_in_prime_hours(20, 2, at(12))
    # A UTC timestamp is judged in Tashkent time (UTC+5)
    assert story_queue_service.is_in_prime_hours(9, 22, datetime(2026, 5, 1, 5, 0, tzinfo=timezone.utc))
    assert not story_queue_service.is_in_prime_hours(9, 22, datetime(2026, 5, 1, 2, 0, tzinfo=timezone.utc))


# ---------------------------------------------------------------- Telegram errors (ST3)

def test_story_errors_are_classified_for_retry_or_give_up():
    from telethon.errors import FloodWaitError
    flood = FloodWaitError(request=None, capture=120)
    assert classify_story_error(flood)[:2] == ("flood", 120.0)
    kind, retry, _ = classify_story_error(Exception("STORIES_TOO_MUCH"))
    assert kind == "limit" and retry >= 3600
    assert classify_story_error(Exception("BOOSTS_REQUIRED"))[0] == "permanent"
    assert classify_story_error(Exception("CHAT_ADMIN_REQUIRED"))[0] == "permanent"
    assert classify_story_error(Exception("connection reset"))[0] == "transient"


# ---------------------------------------------------------------- targets & deletion (ST13, ST7)

async def test_story_target_check_refuses_unusable_channels():
    service = StoryClonerService()
    with patch.object(service, "get_client_for_user", new=AsyncMock(return_value=None)):
        entity, reason = await service.check_story_target(1, "@some_channel")
    assert entity is None and "ulanmagan" in reason

    client = MagicMock(is_connected=MagicMock(return_value=True))
    user = types.User(id=5, first_name="Not a channel")
    with patch.object(service, "get_client_for_user", new=AsyncMock(return_value=client)), \
         patch.object(service, "_resolve_channel_entity", new=AsyncMock(return_value=user)):
        entity, reason = await service.check_story_target(1, "@someone")
    assert entity is None and "kanal" in reason

    channel = types.Channel(id=1234567, title="Kanal", photo=types.ChatPhotoEmpty(), date=None, broadcast=True,
                            access_hash=1, username="kanal_uz")
    client.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(1234567, 1))
    with patch.object(service, "get_client_for_user", new=AsyncMock(return_value=client)), \
         patch.object(service, "_resolve_channel_entity", new=AsyncMock(return_value=channel)), \
         patch.object(service, "_check_can_send_story", new=AsyncMock(return_value=None)):
        entity, reason = await service.check_story_target(1, "@kanal_uz")
    assert entity is channel and reason is None
    assert service.channel_label(channel) == "@kanal_uz"

    # Telegram refuses stories there (stories.canSendStory): the reason is shown, nothing is saved
    from services.story_cloner_service import _fail
    refusal = _fail("Kanalga istoriya joylash huquqi yo'q", "permanent")
    with patch.object(service, "get_client_for_user", new=AsyncMock(return_value=client)), \
         patch.object(service, "_resolve_channel_entity", new=AsyncMock(return_value=channel)), \
         patch.object(service, "_check_can_send_story", new=AsyncMock(return_value=refusal)):
        entity, reason = await service.check_story_target(1, "@kanal_uz")
    assert entity is None and "huquqi" in reason


async def test_channel_target_never_falls_back_to_the_profile():
    service = StoryClonerService()
    client = MagicMock()
    peer, error = await service._resolve_target_peer(client, None, {"target_type": "channel", "target_channel": ""}, None)
    assert peer is None and error
    with patch.object(service, "_resolve_channel_entity", new=AsyncMock(return_value=None)):
        peer, error = await service._resolve_target_peer(client, None, {"target_type": "channel", "target_channel": "@gone"}, None)
    assert peer is None and "@gone" in error
    peer, error = await service._resolve_target_peer(client, None, {"target_type": "self"}, None)
    assert isinstance(peer, types.InputPeerSelf) and error is None


async def test_story_deletion_passes_the_peer():
    service = StoryClonerService()
    client = AsyncMock()
    client.is_connected = MagicMock(return_value=True)
    with patch.object(service, "get_client_for_user", new=AsyncMock(return_value=client)):
        assert await service.delete_story_by_id(7, 99) is True
    request = client.await_args.args[0]
    assert isinstance(request.peer, types.InputPeerSelf) and request.id == [99]


def test_story_job_directories_are_isolated_and_swept(tmp_path):
    with patch("services.story_cloner_service._temp_media_dir", return_value=str(tmp_path)):
        job_dir = StoryClonerService._new_job_dir()
        assert job_dir.startswith(str(tmp_path)) and "story_job_" in job_dir
        old = tmp_path / "story_job_old"
        old.mkdir()
        stale = time.time() - 7 * 3600
        import os
        os.utime(old, (stale, stale))
        assert StoryClonerService.cleanup_stale_job_dirs() >= 1
        assert not old.exists()


def test_watchdog_ignores_listings_older_than_its_window():
    from services.story_cloner_service import WATCHDOG_MAX_MESSAGE_AGE
    assert WATCHDOG_MAX_MESSAGE_AGE <= timedelta(hours=3)


async def test_ui_helpers_are_available_on_the_shared_service():
    assert callable(story_cloner_service.schedule_monitor_start)
    with patch.object(story_cloner_service, "_discard_login_session", new=AsyncMock()) as discard:
        await story_cloner_service.cancel_login(42)
    discard.assert_awaited_once_with(42)


# ---------------------------------------------------------------- revoked customer sessions

def _session_string() -> str:
    """A syntactically valid StringSession with a random auth key (never used against Telegram)."""
    import os
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(os.urandom(256))
    return session.save()


class _FakeStoryClient:
    """Stands in for TelegramClient: connect/disconnect bookkeeping, and every request answers with
    `answer` (an RPC error) or succeeds."""
    answer = None
    instances = []

    def __init__(self, *args, **kwargs):
        self.connected = False
        _FakeStoryClient.instances.append(self)

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False

    async def __call__(self, request):
        if _FakeStoryClient.answer is not None:
            raise _FakeStoryClient.answer
        return object()


@pytest.fixture
def fake_story_client(monkeypatch):
    _FakeStoryClient.answer = None
    _FakeStoryClient.instances = []
    monkeypatch.setattr("services.story_cloner_service.TelegramClient", _FakeStoryClient)
    return _FakeStoryClient


async def test_revoked_customer_session_is_deleted_and_the_user_told(fake_story_client):
    service = StoryClonerService()
    fake_story_client.answer = AuthKeyUnregisteredError(request=None)
    with patch("services.story_cloner_service.db_manager.get_user_session",
               new=AsyncMock(return_value=_session_string())), \
         patch("services.story_cloner_service.db_manager.delete_user_session",
               new=AsyncMock(return_value=True)) as delete, \
         patch.object(service, "_notify", new=AsyncMock()) as notify:
        assert await service.get_client_for_user(501) is None
    delete.assert_awaited_once_with(501)
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == 501 and "qaytadan ulang" in notify.await_args.args[1]
    assert not fake_story_client.instances[0].is_connected()
    assert 501 not in service._user_clients


@pytest.mark.parametrize("error", [
    pytest.param(FloodWaitError(request=None, capture=30), id="flood-wait"),
    pytest.param(RpcCallFailError(request=None), id="telegram-5xx"),
])
async def test_temporary_failures_never_delete_a_customer_session(fake_story_client, error):
    service = StoryClonerService()
    fake_story_client.answer = error
    with patch("services.story_cloner_service.db_manager.get_user_session",
               new=AsyncMock(return_value=_session_string())), \
         patch("services.story_cloner_service.db_manager.delete_user_session", new=AsyncMock()) as delete, \
         patch.object(service, "_notify", new=AsyncMock()) as notify:
        assert await service.get_client_for_user(502) is None
    delete.assert_not_awaited()
    notify.assert_not_awaited()
    assert not fake_story_client.instances[0].is_connected()


async def test_authorized_customer_session_is_pooled(fake_story_client):
    service = StoryClonerService()
    with patch("services.story_cloner_service.db_manager.get_user_session",
               new=AsyncMock(return_value=_session_string())), \
         patch("services.story_cloner_service.db_manager.delete_user_session", new=AsyncMock()) as delete:
        client = await service.get_client_for_user(503)
    assert client is fake_story_client.instances[0] and client.is_connected()
    assert service._user_clients[503] is client
    delete.assert_not_awaited()
