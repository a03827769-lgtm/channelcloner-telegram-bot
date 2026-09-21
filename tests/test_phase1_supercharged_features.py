import asyncio
import os
import tempfile
import pytest
import numpy as np
import cv2
from unittest.mock import AsyncMock, MagicMock, patch

from services.image_hasher import calculate_phash, hamming_distance, image_hasher
from services.render_queue import HardwareAwareRenderQueue, render_queue
from database.db_manager import DatabaseManager
from database.models import ChannelPair, ClonedMessage


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_phase1.db"
    mgr = DatabaseManager(str(db_file))
    return mgr


@pytest.mark.asyncio
async def test_phash_computation_and_hamming():
    # 1. Create two identical realistic textured images (e.g. gradient + shapes)
    img1 = np.zeros((300, 300, 3), dtype=np.uint8)
    for i in range(300):
        img1[i, :] = int(i * 255 / 300)
    cv2.circle(img1, (150, 150), 50, (200, 50, 50), -1)
    cv2.rectangle(img1, (30, 30), (100, 200), (50, 200, 50), -1)

    img2 = img1.copy()

    h1 = calculate_phash(img1)
    h2 = calculate_phash(img2)
    assert h1 is not None and len(h1) == 16
    assert h2 is not None and len(h2) == 16
    assert hamming_distance(h1, h2) == 0

    # 2. Add subtle watermarking and slight compression (slight variance)
    noisy = cv2.resize(img1, (250, 250))
    noisy = cv2.resize(noisy, (300, 300))
    cv2.putText(noisy, "TEST_WM", (10, 290), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    h_noisy = calculate_phash(noisy)
    # pHash should be robust (distance <= 8)
    assert hamming_distance(h1, h_noisy) <= 8

    # 3. Create a totally different pattern
    checkerboard = np.zeros((300, 300, 3), dtype=np.uint8)
    checkerboard[::20, ::20] = 255
    checkerboard[10::20, 10::20] = 255
    h_diff = calculate_phash(checkerboard)
    assert hamming_distance(h1, h_diff) > 10


@pytest.mark.asyncio
async def test_duplicate_detection_and_price_arbitrage(temp_db):
    await temp_db.init_db()

    with patch("database.db_manager.db_manager", temp_db):
        # 1. Create synthetic photo hashes for listing A
        hashes_a = ["0123456789abcdef", "fedcba9876543210"]
        source_chan = "chilonzor_uylar"
        source_msg = 100
        initial_price = 60000.0

        await image_hasher.save_listing_hashes(
            hashes=hashes_a,
            source_channel=source_chan,
            source_msg_id=source_msg,
            pair_id=1,
            price=initial_price
        )

        # 2. Check incoming duplicate with same price from another channel
        is_dup, is_drop, match = await image_hasher.check_listing_duplicate(
            new_hashes=hashes_a,
            current_price=60000.0,
            pair_id=2,
            source_channel="boshqa_kanal"
        )
        assert is_dup is True
        assert is_drop is False
        assert match["matched_msg_id"] == 100
        assert match["previous_price"] == 60000.0

        # 3. Check incoming duplicate with LOWER price (Price Drop Arbitrage!)
        is_dup2, is_drop2, match2 = await image_hasher.check_listing_duplicate(
            new_hashes=hashes_a,
            current_price=55000.0,
            pair_id=2,
            source_channel="arzon_kanal"
        )
        assert is_dup2 is True
        assert is_drop2 is True
        assert match2["matched_msg_id"] == 100
        assert match2["previous_price"] == 60000.0
        assert match2["current_price"] == 55000.0


@pytest.mark.asyncio
async def test_db_cloned_message_metadata_and_sync_queries(temp_db):
    await temp_db.init_db()

    # Create dummy user and pair
    await temp_db.execute("INSERT INTO users (user_id, full_name) VALUES (1, 'Test User')")
    await temp_db.execute("""
        INSERT INTO channel_pairs (id, user_id, source_channel, target_channel, is_active)
        VALUES (10, 1, '@source_ch', '@target_ch', 1)
    """)

    # Record cloned message with full metadata
    await temp_db.record_cloned_message(
        pair_id=10,
        source_msg_id=555,
        target_msg_id=888,
        media_type="photo",
        source_channel="@source_ch",
        target_channel="@target_ch",
        story_id=42,
        status="active",
        price=75000.0,
        last_caption="Super 3 xonali uy"
    )

    # Query cloned message by source channel & message ID
    recs = await temp_db.get_cloned_messages_by_source("@source_ch", 555)
    assert len(recs) == 1
    assert recs[0]["target_msg_id"] == 888
    assert recs[0]["status"] == "active"
    assert recs[0]["price"] == 75000.0
    assert recs[0]["last_caption"] == "Super 3 xonali uy"

    # Update status to 'sold'
    await temp_db.update_cloned_message_status(recs[0]["id"], "sold")
    recs_updated = await temp_db.get_cloned_messages_by_source("@source_ch", 555)
    assert recs_updated[0]["status"] == "sold"

    # Update price on price drop
    await temp_db.update_cloned_message_price(recs[0]["id"], 72000.0)
    recs_price = await temp_db.get_cloned_messages_by_source("@source_ch", 555)
    assert recs_price[0]["price"] == 72000.0


@pytest.mark.asyncio
async def test_hardware_aware_render_queue():
    queue = HardwareAwareRenderQueue(max_concurrent=1, memory_threshold_percent=99.0)

    execution_order = []

    async def mock_render_task(task_id: int, sleep_s: float):
        execution_order.append(f"start_{task_id}")
        await asyncio.sleep(sleep_s)
        execution_order.append(f"end_{task_id}")
        return f"result_{task_id}"

    # Launch two tasks simultaneously through queue
    t1 = asyncio.create_task(queue.run_render_job(mock_render_task, 1, 0.05))
    t2 = asyncio.create_task(queue.run_render_job(mock_render_task, 2, 0.05))

    res1, res2 = await asyncio.gather(t1, t2)
    assert res1 == "result_1"
    assert res2 == "result_2"

    # Because max_concurrent=1, task 1 must start and end before task 2 starts!
    assert execution_order == ["start_1", "end_1", "start_2", "end_2"]

    status = queue.get_status()
    assert status["total_completed"] == 2
    assert status["active_renders"] == 0
    assert status["max_concurrent"] == 1


@pytest.mark.asyncio
async def test_story_cloner_service_source_delete():
    from services.story_cloner_service import story_cloner_service
    from database.db_manager import db_manager

    with patch.object(story_cloner_service, "delete_story_by_id", new=AsyncMock(return_value=True)) as mock_del:
        with patch.object(db_manager, "get_connection") as mock_conn:
            # Mock row found in posted_stories: id=1, user_id=123, story_id=999
            mock_cursor = AsyncMock()
            mock_cursor.fetchall.return_value = [(1, 123, 999, "self", "success")]
            mock_db = AsyncMock()
            mock_db.execute.return_value = mock_cursor

            # Context manager yield
            mock_conn.return_value.__aenter__.return_value = mock_db

            with patch.object(db_manager, "write_transaction") as mock_wtrans:
                mock_wdb = AsyncMock()
                mock_wtrans.return_value.__aenter__.return_value = mock_db

                await story_cloner_service.handle_source_message_deleted("test_channel", 50)
                mock_del.assert_called_once_with(user_id=123, story_id=999)


@pytest.mark.asyncio
async def test_telethon_listener_message_deleted_sync():
    from services.telethon_listener import telethon_listener
    from services.cloner_engine import cloner_engine
    from database.db_manager import db_manager

    event = MagicMock()
    event.deleted_ids = [101]
    mock_chat = MagicMock()
    mock_chat.username = "cityjoyestateuz"
    mock_chat.id = 12345
    event.get_chat = AsyncMock(return_value=mock_chat)

    mock_bot = AsyncMock()

    with patch.object(cloner_engine, "bot", mock_bot):
        with patch.object(db_manager, "get_cloned_messages_by_source", new=AsyncMock()) as mock_get_cloned:
            mock_get_cloned.return_value = [{
                "id": 7,
                "target_channel": "@my_dest_channel",
                "pair_target_channel": "@my_dest_channel",
                "target_msg_id": 888,
                "status": "active",
                "media_type": "photo",
                "last_caption": "Chiroyli 3 xonali uy"
            }]
            with patch.object(db_manager, "update_cloned_message_status", new=AsyncMock()) as mock_upd_status:
                with patch("services.story_cloner_service.story_cloner_service.handle_source_message_deleted", new=AsyncMock()) as mock_st_del:
                    await telethon_listener._handle_message_deleted(event)

                    # Verify destination message edited with SOLD tag
                    mock_bot.edit_message_caption.assert_called_once()
                    assert "🔴 <b>SOTILDI / YOPILGAN E'LON</b>" in mock_bot.edit_message_caption.call_args[1]["caption"]
                    # Verify status set to sold
                    mock_upd_status.assert_called_once_with(7, "sold")
                    # Verify story deleted
                    mock_st_del.assert_called_once_with(source_channel="cityjoyestateuz", source_msg_id=101)


@pytest.mark.asyncio
async def test_telethon_listener_message_edited_sold_sync():
    from services.telethon_listener import telethon_listener
    from services.cloner_engine import cloner_engine
    from database.db_manager import db_manager

    event = MagicMock()
    event.message = MagicMock()
    event.message.id = 102
    event.message.message = "Ushbu uy sotildi, rahmat barchaga!"
    mock_chat = MagicMock()
    mock_chat.username = "cityjoyestateuz"
    mock_chat.id = 12345
    event.get_chat = AsyncMock(return_value=mock_chat)

    mock_bot = AsyncMock()

    with patch.object(cloner_engine, "bot", mock_bot):
        with patch.object(db_manager, "get_cloned_messages_by_source", new=AsyncMock()) as mock_get_cloned:
            mock_get_cloned.return_value = [{
                "id": 8,
                "target_channel": "@my_dest_channel",
                "pair_target_channel": "@my_dest_channel",
                "target_msg_id": 889,
                "status": "active",
                "media_type": "text",
                "last_caption": "3 xonali uy"
            }]
            with patch.object(db_manager, "update_cloned_message_status", new=AsyncMock()) as mock_upd_status:
                with patch("services.story_cloner_service.story_cloner_service.handle_source_message_deleted", new=AsyncMock()):
                    await telethon_listener._handle_message_edited(event)

                    # Verify text updated with SOLD tag
                    mock_bot.edit_message_text.assert_called_once()
                    assert "🔴 <b>SOTILDI / YOPILGAN E'LON</b>" in mock_bot.edit_message_text.call_args[1]["text"]
                    mock_upd_status.assert_called_once_with(8, "sold")
