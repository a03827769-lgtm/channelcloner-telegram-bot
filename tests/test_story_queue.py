# -*- coding: utf-8 -*-
import pytest
from datetime import datetime, timezone, timedelta
from services.story_queue_service import story_queue_service, UZB_TZ
from database.models import StorySettings


def test_prime_hours_calculation():
    # 14:30 in Tashkent -> Prime hours (09:00 - 22:00)
    daytime = datetime(2026, 9, 17, 14, 30, tzinfo=UZB_TZ)
    assert story_queue_service.is_in_prime_hours(9, 22, daytime) is True

    # 23:15 in Tashkent -> Outside prime hours (Night)
    night = datetime(2026, 9, 17, 23, 15, tzinfo=UZB_TZ)
    assert story_queue_service.is_in_prime_hours(9, 22, night) is False

    # 04:00 in Tashkent -> Outside prime hours (Early morning)
    early_morning = datetime(2026, 9, 18, 4, 0, tzinfo=UZB_TZ)
    assert story_queue_service.is_in_prime_hours(9, 22, early_morning) is False

    # 09:00 in Tashkent -> Exactly start of prime hours
    start_time = datetime(2026, 9, 18, 9, 0, tzinfo=UZB_TZ)
    assert story_queue_service.is_in_prime_hours(9, 22, start_time) is True


@pytest.mark.asyncio
async def test_calculate_scheduled_time_prime_hours(monkeypatch):
    # Mock current Tashkent time to 23:30 (overnight)
    mock_now = datetime(2026, 9, 17, 23, 30, tzinfo=UZB_TZ)
    monkeypatch.setattr(story_queue_service, "get_uzb_now", lambda: mock_now)

    settings = StorySettings(
        user_id=999999,
        prime_hours_enabled=True,
        prime_hours_start=9,
        prime_hours_end=22,
        drip_delay_minutes=45
    )

    sched_utc = await story_queue_service.calculate_scheduled_time(settings)
    sched_uzb = sched_utc.astimezone(UZB_TZ)

    # Must be scheduled for tomorrow morning at 09:00 Tashkent time
    assert sched_uzb.day == 18
    assert sched_uzb.hour == 9
    assert sched_uzb.minute == 0


@pytest.mark.asyncio
async def test_calculate_scheduled_time_daytime(monkeypatch):
    # Mock current Tashkent time to 12:00 (midday)
    mock_now = datetime(2026, 9, 17, 12, 0, tzinfo=UZB_TZ)
    monkeypatch.setattr(story_queue_service, "get_uzb_now", lambda: mock_now)

    settings = StorySettings(
        user_id=999999,
        prime_hours_enabled=True,
        prime_hours_start=9,
        prime_hours_end=22,
        drip_delay_minutes=45
    )

    sched_utc = await story_queue_service.calculate_scheduled_time(settings)
    sched_uzb = sched_utc.astimezone(UZB_TZ)

    # During midday with no previous post, scheduled immediately (or now)
    assert sched_uzb.day == 17
    assert sched_uzb.hour == 12


@pytest.mark.asyncio
async def test_process_due_item_retry_on_failure():
    import uuid
    import json
    from unittest.mock import AsyncMock, MagicMock, patch
    from database.db_manager import DatabaseManager
    from database.models import StoryQueueItem, StorySettings
    from tests.test_utils import safe_cleanup_db

    db_path = f"temp_media/test_sq_retry_{uuid.uuid4().hex[:8]}.db"
    db = DatabaseManager(db_path)
    try:
        await db.init_db()

        user_id = 771122
        await db.get_or_create_user(user_id, "Test User", "testuser")
        await db.activate_subscription(user_id, "vip", 0, "test_charge", days=30)
        await db.update_story_settings(user_id, is_active=True, source_channel="@vip_chan")

        item_id = await db.enqueue_story(
            user_id=user_id,
            source_channel="@vip_chan",
            source_msg_id=101,
            payload={"photos": ["p1.jpg"], "caption": "Super Lux", "retries": 0},
            scheduled_at_utc="2026-09-17 12:00:00",
            price=1200.0,
            district="Yunusobod",
            score=85
        )

        items = await db.get_due_story_queue_items()
        assert len(items) >= 1
        item = items[0]
        assert item.id == item_id

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True

        mock_cloner = AsyncMock()
        mock_cloner.get_client_for_user = AsyncMock(return_value=mock_client)
        mock_cloner.post_story_from_channel = AsyncMock(return_value=(False, None, "Telegram Network Timeout"))

        with patch("services.story_queue_service.db_manager", db):
            # Attempt 1: Should retry (status pending, retries=1)
            await story_queue_service._process_due_item(item, mock_cloner)

            async with db.get_connection() as conn:
                cur = await conn.execute("SELECT status, payload_json, error_message FROM story_queue WHERE id = ?", (item_id,))
                row = await cur.fetchone()
                assert row[0] == "pending"
                payload = json.loads(row[1])
                assert payload.get("retries") == 1
                assert "Retry #1" in row[2]

            # Attempt 2: Should retry (status pending, retries=2)
            item.payload_json = json.dumps(payload)
            await story_queue_service._process_due_item(item, mock_cloner)

            async with db.get_connection() as conn:
                cur = await conn.execute("SELECT status, payload_json, error_message FROM story_queue WHERE id = ?", (item_id,))
                row = await cur.fetchone()
                assert row[0] == "pending"
                payload = json.loads(row[1])
                assert payload.get("retries") == 2
                assert "Retry #2" in row[2]

            # Attempt 3: Should retry (status pending, retries=3)
            item.payload_json = json.dumps(payload)
            await story_queue_service._process_due_item(item, mock_cloner)

            async with db.get_connection() as conn:
                cur = await conn.execute("SELECT status, payload_json, error_message FROM story_queue WHERE id = ?", (item_id,))
                row = await cur.fetchone()
                assert row[0] == "pending"
                payload = json.loads(row[1])
                assert payload.get("retries") == 3

            # Attempt 4 (Exceeded): Marks permanently failed
            item.payload_json = json.dumps(payload)
            await story_queue_service._process_due_item(item, mock_cloner)

            async with db.get_connection() as conn:
                cur = await conn.execute("SELECT status FROM story_queue WHERE id = ?", (item_id,))
                row = await cur.fetchone()
                assert row[0] == "failed"
    finally:
        await safe_cleanup_db(db_path, db)


@pytest.mark.asyncio
async def test_clean_old_cloned_messages_prunes_story_queue():
    import uuid
    from database.db_manager import DatabaseManager
    from tests.test_utils import safe_cleanup_db

    db_path = f"temp_media/test_sq_prune_{uuid.uuid4().hex[:8]}.db"
    db = DatabaseManager(db_path)
    try:
        await db.init_db()

        user_id = 882233
        await db.get_or_create_user(user_id, "Prune User", "pruneuser")

        # Add old completed story queue item (40 days ago)
        async with db.write_transaction() as conn:
            await conn.execute("""
                INSERT INTO story_queue (user_id, source_channel, source_msg_id, payload_json, status, scheduled_at, created_at)
                VALUES (?, ?, ?, '{}', 'sent', datetime('now', '-40 days'), datetime('now', '-40 days'))
            """, (user_id, "@old_chan", 555))
            # Add recent completed story queue item (2 days ago)
            await conn.execute("""
                INSERT INTO story_queue (user_id, source_channel, source_msg_id, payload_json, status, scheduled_at, created_at)
                VALUES (?, ?, ?, '{}', 'sent', datetime('now', '-2 days'), datetime('now', '-2 days'))
            """, (user_id, "@new_chan", 666))
            await conn.commit()

        async with db.get_connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM story_queue")
            assert (await cur.fetchone())[0] == 2

        # Run cleanup
        await db.clean_old_cloned_messages(days=180)

        # Ancient record should be pruned, recent one preserved
        async with db.get_connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM story_queue")
            assert (await cur.fetchone())[0] == 1
            cur2 = await conn.execute("SELECT source_msg_id FROM story_queue")
            assert (await cur2.fetchone())[0] == 666
    finally:
        await safe_cleanup_db(db_path, db)
