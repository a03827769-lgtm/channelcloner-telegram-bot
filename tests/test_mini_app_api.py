"""Security and behaviour tests for the Mini App REST API (services/api_routes.py)."""
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config.settings import settings
from database.db_manager import db_manager
from services import api_routes
from services.api_routes import normalize_channel_ref, validate_init_data, ApiError

TEST_TOKEN = "123456:TEST-token-for-initdata"


def make_init_data(user_id: int, token: str = TEST_TOKEN, auth_date=None, **user_extra) -> str:
    user = {"id": user_id, "first_name": "Test", "username": f"u{user_id}", **user_extra}
    fields = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def auth(user_id: int) -> dict:
    return {"X-Telegram-Init-Data": make_init_data(user_id)}


@pytest.fixture
async def client(tmp_path):
    # Each test gets its own database; the shared db_manager is pointed back to the session DB afterwards
    original_path = db_manager.db_path
    await db_manager.close()
    db_manager.db_path = str(tmp_path / "mini_app_api.db")
    await db_manager.init_db()
    app = web.Application()
    api_routes.register_api_routes(app)
    try:
        with patch.object(settings, "BOT_TOKEN", TEST_TOKEN):
            async with TestClient(TestServer(app)) as c:
                yield c
    finally:
        await db_manager.close()
        db_manager.db_path = original_path


# --- initData validation -----------------------------------------------------------------

def test_valid_init_data_returns_user():
    user = validate_init_data(make_init_data(42), TEST_TOKEN)
    assert user and user["id"] == 42


def test_tampered_init_data_is_rejected():
    data = make_init_data(42).replace("u42", "u43")
    assert validate_init_data(data, TEST_TOKEN) is None


def test_init_data_signed_with_other_token_is_rejected():
    assert validate_init_data(make_init_data(42, token="999:other"), TEST_TOKEN) is None


def test_expired_init_data_is_rejected():
    old = make_init_data(42, auth_date=time.time() - 3 * 86400)
    assert validate_init_data(old, TEST_TOKEN) is None


def test_missing_hash_or_empty_is_rejected():
    assert validate_init_data("", TEST_TOKEN) is None
    assert validate_init_data("auth_date=1&user=%7B%7D", TEST_TOKEN) is None


# --- authentication ----------------------------------------------------------------------

async def test_requests_without_init_data_are_rejected(client):
    for path in ("/api/me", "/api/pairs", "/api/system", "/api/store/orders"):
        resp = await client.get(path)
        assert resp.status == 401, path
        assert (await resp.json())["code"] == "TELEGRAM_AUTH_REQUIRED"


async def test_spoofed_user_id_header_is_ignored(client):
    resp = await client.get("/api/me", headers={"X-User-Id": str(next(iter(settings.admin_ids), 1))})
    assert resp.status == 401


async def test_me_returns_profile_for_authenticated_user(client):
    resp = await client.get("/api/me", headers=auth(700001))
    assert resp.status == 200
    data = await resp.json()
    assert data["user"]["id"] == 700001
    assert "subscription" in data and "stats" in data
    assert len(data["stats"]["daily"]) == 7
    assert "success_rate" not in data["stats"]


# --- channel pair ownership (IDOR) -------------------------------------------------------

async def _make_pair(owner: int, src: str, dst: str) -> int:
    await db_manager.get_or_create_user(owner, "Owner")
    return await db_manager.add_channel_pair(user_id=owner, source_channel=src, target_channel=dst,
                                             source_title=src, target_title=dst)


async def test_user_cannot_access_foreign_pair(client):
    pair_id = await _make_pair(700010, "@idor_source_a", "@idor_target_a")
    attacker = auth(700011)
    assert (await client.get(f"/api/pairs/{pair_id}", headers=attacker)).status == 404
    assert (await client.put(f"/api/pairs/{pair_id}", json={"clean_links": False}, headers=attacker)).status == 404
    assert (await client.post(f"/api/pairs/{pair_id}/toggle", headers=attacker)).status == 404
    assert (await client.delete(f"/api/pairs/{pair_id}", headers=attacker)).status == 404
    assert await db_manager.get_pair_by_id(pair_id) is not None


async def test_owner_updates_pair_with_validation(client):
    pair_id = await _make_pair(700020, "@upd_source", "@upd_target")
    owner = auth(700020)
    bad = await client.put(f"/api/pairs/{pair_id}", json={"night_mode": "party"}, headers=owner)
    assert bad.status == 400
    # Night mode and drip feed are PRO/VIP features
    trial = await client.put(f"/api/pairs/{pair_id}", json={"night_mode": "silent", "drip_delay_minutes": 15}, headers=owner)
    assert trial.status == 402
    assert (await trial.json())["code"] == "PRO_REQUIRED"
    await db_manager.activate_subscription(700020, "pro", 100, "api_update_pro", days=30)
    ok = await client.put(f"/api/pairs/{pair_id}", json={"night_mode": "silent", "drip_delay_minutes": 15}, headers=owner)
    assert ok.status == 200
    pair = await db_manager.get_pair_by_id(pair_id)
    assert pair.night_mode == "silent" and pair.drip_delay_minutes == 15


async def test_logo_watermark_path_outside_assets_is_rejected(client):
    pair_id = await _make_pair(700021, "@logo_source", "@logo_target")
    resp = await client.put(
        f"/api/pairs/{pair_id}",
        json={"image_watermark_type": "logo", "image_watermark_text": "../.env"},
        headers=auth(700021),
    )
    assert resp.status == 400


async def test_non_numeric_pair_id_is_bad_request(client):
    resp = await client.get("/api/pairs/abc", headers=auth(700022))
    assert resp.status == 400


async def test_activation_respects_plan_limit(client):
    owner = 700030
    first = await _make_pair(owner, "@limit_src_1", "@limit_dst_1")
    second = await _make_pair(owner, "@limit_src_2", "@limit_dst_2")
    assert await db_manager.set_pair_active_by_owner(second, False) == (False, None)
    assert (await db_manager.get_pair_by_id(first)).is_active
    resp = await client.post(f"/api/pairs/{second}/toggle", headers=auth(owner))
    assert resp.status == 409  # free trial allows exactly one active pair
    assert (await resp.json())["code"] == "PLAN_LIMIT"
    assert not (await db_manager.get_pair_by_id(second)).is_active


# --- audio ------------------------------------------------------------------------------

async def test_audio_preview_is_public_but_traversal_blocked(client):
    listing = await client.get("/api/audio-tracks", headers=auth(700040))
    assert listing.status == 200
    tracks = (await listing.json())["tracks"]
    assert tracks
    ok = await client.get(f"/api/audio-tracks/{tracks[0]['filename']}")
    assert ok.status == 200
    for bad in ("..%2F..%2F.env", "evil.txt", "..%5Crun.py"):
        assert (await client.get(f"/api/audio-tracks/{bad}")).status == 404


# --- system -----------------------------------------------------------------------------

async def test_system_logs_hidden_from_non_admin(client):
    resp = await client.get("/api/system", headers=auth(700050))
    assert resp.status == 200
    data = await resp.json()
    assert data["logs"] == []
    assert "mtproto_connected" in data["telemetry"]


# --- store checkout ---------------------------------------------------------------------

async def test_store_purchase_requires_payment_before_fulfillment(client):
    from services.supplier_service import supplier_service
    await db_manager.upsert_synced_product(
        {"supplier_id": 1, "service": 5501, "name": "Test Members", "category": "Test",
         "type": "Default", "rate": 2.0, "min": 100, "max": 5000},
        margin_percent=25.0,
    )
    product = next(p for p in await db_manager.get_store_products() if p.supplier_service_id == 5501)
    fake_bot = MagicMock()
    fake_bot.create_invoice_link = AsyncMock(return_value="https://t.me/$invoice-test")
    with patch("services.cloner_engine.cloner_engine.bot", fake_bot):
        resp = await client.post(
            "/api/store/buy",
            json={"product_id": product.id, "quantity": 1000, "target_link": "https://t.me/test_channel"},
            headers=auth(700060),
        )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "awaiting_payment"
    assert data["invoice_link"] == "https://t.me/$invoice-test"
    # 1000 units of a $2.00-per-1000 service: 2.0 * 50 Stars/USD * 1.25 margin = 125 Stars
    assert data["price_stars"] == 125
    order = await db_manager.get_store_order(data["order_id"])
    assert order.status == "awaiting_payment"

    # Fulfillment is refused until the payment is recorded
    assert (await supplier_service.fulfill_paid_order(order.id))["success"] is False
    assert await db_manager.mark_store_order_paid(order.id, 700060, "charge-test-1", 125) is True
    # Replay of the same charge (or a second payment) is rejected
    assert await db_manager.mark_store_order_paid(order.id, 700060, "charge-test-1", 125) is False
    with patch.object(supplier_service, "_notify_admins_about_pending_order", AsyncMock()):
        result = await supplier_service.fulfill_paid_order(order.id)
    assert result["status"] == "pending_admin"


async def test_store_rejects_invalid_quantity_and_link(client):
    await db_manager.upsert_synced_product(
        {"supplier_id": 1, "service": 5502, "name": "Test Views", "category": "Test",
         "type": "Default", "rate": 0.1, "min": 100, "max": 1000},
        margin_percent=25.0,
    )
    product = next(p for p in await db_manager.get_store_products() if p.supplier_service_id == 5502)
    fake_bot = MagicMock()
    fake_bot.create_invoice_link = AsyncMock(return_value="https://t.me/$x")
    with patch("services.cloner_engine.cloner_engine.bot", fake_bot):
        low = await client.post("/api/store/buy", headers=auth(700061),
                                json={"product_id": product.id, "quantity": 5, "target_link": "@test_channel"})
        bad_link = await client.post("/api/store/buy", headers=auth(700061),
                                     json={"product_id": product.id, "quantity": 200, "target_link": "javascript:alert(1)"})
    assert low.status == 400 and bad_link.status == 400
    fake_bot.create_invoice_link.assert_not_called()


async def test_supplier_admin_endpoints_require_admin(client):
    assert (await client.get("/api/store/balance", headers=auth(700070))).status == 403
    assert (await client.post("/api/store/sync", headers=auth(700070))).status == 403


# --- input normalisation ----------------------------------------------------------------

def test_normalize_channel_ref_variants():
    assert normalize_channel_ref("https://t.me/daryo_uz") == ("@daryo_uz", None)
    assert normalize_channel_ref("@Daryo_uz") == ("@Daryo_uz", None)
    assert normalize_channel_ref("-1001234567890") == ("-1001234567890", -1001234567890)
    assert normalize_channel_ref("1234567890") == ("-1001234567890", -1001234567890)
    assert normalize_channel_ref("https://t.me/+AbCdEfGh1234")[0] == "https://t.me/+AbCdEfGh1234"
    for bad in ("", "a", "https://evil.com/x", "@bad name"):
        with pytest.raises(ApiError):
            normalize_channel_ref(bad)
