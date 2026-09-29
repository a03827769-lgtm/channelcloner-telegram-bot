import pytest
import os
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch
from database.db_manager import DatabaseManager
from services.cloner_engine import ClonerEngine
from services.video_watermark_service import video_watermark_service
from services.media_handler import MediaHandler
from services.disaster_recovery import DisasterRecoveryService
from aiogram.exceptions import TelegramRetryAfter

import pytest_asyncio

@pytest_asyncio.fixture
async def db(tmp_path):
    db_file = str(tmp_path / "test_enterprise_complete.db")
    database = DatabaseManager(db_path=db_file)
    await database.init_db()
    yield database
    try:
        await database.close()
    except Exception:
        pass

@pytest.mark.asyncio
async def test_video_watermark_service_compatibility_alias():
    """Verify apply_video_watermark alias exists and is callable without AttributeError"""
    assert hasattr(video_watermark_service, "apply_video_watermark")
    assert callable(video_watermark_service.apply_video_watermark)

@pytest.mark.asyncio
async def test_cloner_engine_self_cloning_loop_normalization(db):
    """Verify that source and target matching with different channel ID prefixes is blocked"""
    engine = ClonerEngine()
    mock_bot = MagicMock()
    engine.set_bot(mock_bot)

    # 123456789 (Telethon style) vs -100123456789 (Bot API style)
    telethon_source_id = 123456789
    bot_api_target_id = -100123456789

    await db.get_or_create_user(user_id=999, full_name="Test User")
    pair_id = await db.add_channel_pair(
        user_id=999,
        source_channel="@src_test",
        source_title="Source",
        target_channel="@tgt_test",
        target_title="Target",
        source_id=telethon_source_id,
        target_id=bot_api_target_id
    )

    pair = await db.get_pair_by_id(pair_id)

    # Calling clone_single_message must abort immediately to prevent infinite loop
    mock_message = MagicMock()
    mock_message.id = 10
    result = await engine.clone_single_message(message=mock_message, pair=pair)

    assert result is False

@pytest.mark.asyncio
async def test_send_with_retry_final_floodwait():
    """Verify that TelegramRetryAfter on the last attempt waits and makes the final call"""
    engine = ClonerEngine()
    mock_func = AsyncMock()
    
    # Simulate FloodWait on attempt 1 and 2, success on attempt 3
    mock_func.side_effect = [
        TelegramRetryAfter(method=MagicMock(), message="Flood", retry_after=1),
        TelegramRetryAfter(method=MagicMock(), message="Flood", retry_after=1),
        "Success_Result"
    ]

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await engine._send_with_retry(mock_func, max_retries=3)
        assert res == "Success_Result"
        assert mock_func.call_count == 3
        assert mock_sleep.call_count == 2

@pytest.mark.asyncio
async def test_db_manager_rollback_on_write_transaction_exception(tmp_path):
    """Verify that write_transaction executes a rollback when an exception is encountered"""
    db_file = str(tmp_path / "test_rollback.db")
    database = DatabaseManager(db_path=db_file)
    await database.init_db()

    with pytest.raises(RuntimeError):
        async with database.write_transaction() as conn:
            await conn.execute("CREATE TABLE test_rollback (id INTEGER PRIMARY KEY);")
            await conn.execute("INSERT INTO test_rollback VALUES (1);")
            raise RuntimeError("Simulated crash during transaction")

    # Verify table does not exist or has no committed rows due to rollback
    async with database.get_connection() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='test_rollback';"
        )
        row = await cursor.fetchone()
        if row:
            count_cursor = await conn.execute("SELECT COUNT(*) FROM test_rollback;")
            count = await count_cursor.fetchone()
            assert count[0] == 0

    await database.close()

@pytest.mark.asyncio
async def test_db_manager_expiring_paid_users_query(db):
    """Verify get_expiring_paid_users_to_notify returns PRO/VIP users expiring within 3 days"""
    # 1. Active PRO user expiring in 2 days
    await db.get_or_create_user(user_id=101, full_name="User PRO")
    await db.activate_subscription(user_id=101, tier="pro", stars=100, charge_id="ch_101", days=2)

    # 2. Active VIP user expiring in 10 days (should NOT be returned)
    await db.get_or_create_user(user_id=102, full_name="User VIP")
    await db.activate_subscription(user_id=102, tier="vip", stars=200, charge_id="ch_102", days=10)

    # 3. Free user (should NOT be returned)
    await db.get_or_create_user(user_id=103, full_name="User Free")

    expiring = await db.get_expiring_paid_users_to_notify()
    expiring_ids = [u[0] for u in expiring]

    assert 101 in expiring_ids
    assert 102 not in expiring_ids
    assert 103 not in expiring_ids

@pytest.mark.asyncio
async def test_disaster_recovery_unlimited_limit(db):
    """Verify DisasterRecoveryService.restore_channel supports limit=None without crashing"""
    service = DisasterRecoveryService()
    mock_bot = AsyncMock()

    await db.get_or_create_user(user_id=555, full_name="Test User 555")
    pair_id = await db.add_channel_pair(
        user_id=555,
        source_channel="@src",
        source_title="Src",
        target_channel="@tgt",
        target_title="Tgt"
    )

    # Save 3 messages
    for i in range(1, 4):
        await service.archive_message(
            pair_id=pair_id,
            source_id=123,
            message_id=i,
            text=f"Post #{i}",
            media_type="none",
            db=db
        )

    destination = MagicMock(id=-100999999)
    with patch("services.disaster_recovery.verify_destination_access",
               new=AsyncMock(return_value=(True, destination, None))), \
         patch.object(DisasterRecoveryService, "SEND_INTERVAL_SECONDS", 0):
        res = await service.restore_channel(
            bot=mock_bot,
            pair_id=pair_id,
            new_target_channel="-100999999",
            limit=None,
            db=db,
            requester_id=555
        )

    assert res["total_archived"] == 3
    assert res["restored"] == 3
    assert res["failed"] == 0
    assert mock_bot.send_message.await_count == 3

@pytest.mark.asyncio
async def test_admin_backup_zip_creation(tmp_path):
    """Verify that creating backup and packaging into zip works properly"""
    test_db = str(tmp_path / "cloner.db")
    database = DatabaseManager(db_path=test_db)
    await database.init_db()

    backup_path = await database.create_backup_file()
    assert os.path.exists(backup_path)

    zip_path = f"{backup_path}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(backup_path, arcname=os.path.basename(backup_path))

    assert os.path.exists(zip_path)
    assert zipfile.is_zipfile(zip_path)
    assert os.path.getsize(zip_path) > 0

    # Cleanup
    os.remove(backup_path)
    os.remove(zip_path)
    await database.close()

@pytest.mark.asyncio
async def test_media_handler_stop_background_cleanup():
    """Verify stop_background_cleanup cancels the cleaner task without raising errors"""
    handler = MediaHandler(temp_dir="temp_media_test")
    handler.start_background_cleanup(interval=60, max_age=60)
    assert handler._cleanup_task is not None
    assert not handler._cleanup_task.done()

    await handler.stop_background_cleanup()
    assert handler._cleanup_task is None
