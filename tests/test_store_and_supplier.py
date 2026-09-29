import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from database.db_manager import DatabaseManager
from services.supplier_service import SupplierService

@pytest.fixture
async def temp_db(tmp_path):
    db_file = tmp_path / "test_store.db"
    mgr = DatabaseManager(db_path=str(db_file))
    await mgr.init_db()
    yield mgr
    await mgr.close()

@pytest.mark.asyncio
async def test_supplier_config_and_products_crud(temp_db):
    # Test default supplier seeded
    cfg = await temp_db.get_supplier_config()
    assert cfg is not None
    assert cfg.margin_percent == 25.0

    # Upsert a product from supplier
    svc_data = {
        "supplier_id": cfg.id,
        "service": 777,
        "name": "1000 Telegram Reactions",
        "category": "Reactions",
        "type": "Default",
        "rate": 0.50,
        "min": 100,
        "max": 5000
    }
    await temp_db.upsert_synced_product(svc_data, margin_percent=20.0)

    # Verify product in store
    products = await temp_db.get_store_products()
    assert len(products) == 1
    p = products[0]
    assert p.name == "1000 Telegram Reactions"
    assert p.supplier_service_id == 777
    assert p.stock_status == "in_stock"
    assert p.selling_price_stars > 0

    # Mark out of stock when unlisted
    depleted = await temp_db.mark_unlisted_products_out_of_stock(active_service_ids=set(), supplier_id=cfg.id)
    assert depleted == 1
    updated_p = await temp_db.get_store_product(p.id)
    assert updated_p.stock_status == "out_of_stock"

@pytest.mark.asyncio
async def test_supplier_order_routing_insufficient_balance(temp_db):
    svc = SupplierService()
    
    # Insert test product
    svc_data = {
        "supplier_id": 1,
        "service": 888,
        "name": "VIP Stars",
        "category": "Stars",
        "type": "Package",
        "rate": 10.0,
        "min": 1,
        "max": 10
    }
    await temp_db.upsert_synced_product(svc_data, margin_percent=25.0)
    products = await temp_db.get_store_products()
    prod = products[0]

    fake_bot = MagicMock()
    fake_bot.create_invoice_link = AsyncMock(return_value="https://t.me/$invoice")
    with patch("services.supplier_service.db_manager", temp_db),          patch.object(svc, "_notify_admins_about_pending_order", AsyncMock()) as notify:
        # Checkout only creates an unpaid order + Stars invoice; nothing is fulfilled yet
        checkout = await svc.create_checkout(fake_bot, 12345, prod.id, 1, "@my_channel")
        assert checkout["success"] is True
        assert checkout["status"] == "awaiting_payment"
        # Package priced per unit: 10.0 USD * 50 Stars/USD * 1.25 margin
        assert checkout["price_stars"] == 625
        notify.assert_not_called()

        paid = await temp_db.mark_store_order_paid(checkout["order_id"], 12345, "charge-xyz", 625)
        assert paid is True

        # Balance is 0.0, required is 10.0 -> paid order routes to admin escrow
        res = await svc.fulfill_paid_order(checkout["order_id"], "Test User", "testuser")
        assert res["success"] is True
        assert res["status"] == "pending_admin"
        assert res["requires_admin_contact"] is True
        assert "admin_username" in res
        notify.assert_awaited_once()

        orders = await temp_db.get_user_store_orders(12345)
        assert len(orders) == 1
        assert orders[0].status == "pending_admin"
        assert orders[0].admin_notified is True
