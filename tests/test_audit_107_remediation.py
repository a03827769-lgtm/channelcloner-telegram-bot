import os
import json
import tempfile
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from database.db_manager import DatabaseManager
from database.models import ChannelPair
from services.ai_paraphraser import ai_paraphraser
from services.story_queue_service import story_queue_service
from bot.keyboards.inline_buttons import get_translate_lang_keyboard, get_video_watermark_keyboard
from bot.handlers.stars_billing import process_successful_payment


@pytest.mark.asyncio
async def test_stars_billing_json_and_legacy_payload(tmp_path):
    """Verify that both JSON {'t': 'pro', 'u': 123} and legacy stars_plan_ formats activate subscriptions"""
    mock_sub = MagicMock()
    mock_sub.expires_at = "2026-10-19T00:00:00Z"
    mock_db = MagicMock()
    mock_db.activate_subscription = AsyncMock(return_value=mock_sub)
    mock_db.is_admin = AsyncMock(return_value=False)
    mock_db.is_payment_processed = AsyncMock(return_value=False)
    mock_db.record_payment = AsyncMock()
    mock_db.get_user = AsyncMock(return_value={"user_id": 999, "full_name": "Test Payer"})

    # 1. JSON payload
    msg_json = MagicMock()
    msg_json.from_user.id = 999
    msg_json.from_user.full_name = "Test Payer"
    msg_json.successful_payment.invoice_payload = json.dumps({"t": "pro", "u": 999})
    msg_json.successful_payment.total_amount = 250
    msg_json.successful_payment.telegram_payment_charge_id = "ch_json_123"
    msg_json.answer = AsyncMock()

    with patch("bot.handlers.stars_billing.db_manager", mock_db):
        await process_successful_payment(msg_json)
        mock_db.activate_subscription.assert_called_with(
            user_id=999,
            tier="pro",
            stars=250,
            charge_id="ch_json_123",
            days=30
        )

    # 2. Legacy payload
    msg_legacy = MagicMock()
    msg_legacy.from_user.id = 888
    msg_legacy.from_user.full_name = "Legacy Payer"
    msg_legacy.successful_payment.invoice_payload = "stars_plan_vip_888_123456"
    msg_legacy.successful_payment.total_amount = 500
    msg_legacy.successful_payment.telegram_payment_charge_id = "ch_leg_456"
    msg_legacy.answer = AsyncMock()

    mock_db.activate_subscription.reset_mock()
    with patch("bot.handlers.stars_billing.db_manager", mock_db):
        await process_successful_payment(msg_legacy)
        mock_db.activate_subscription.assert_called_with(
            user_id=888,
            tier="vip",
            stars=500,
            charge_id="ch_leg_456",
            days=30
        )


@pytest.mark.asyncio
async def test_db_manager_backup_creation_and_removal(tmp_path):
    """Verify create_backup_file cleanly closes handles so file can be removed on Windows without PermissionError"""
    db_file = str(tmp_path / "test_backup.db")
    db = DatabaseManager(db_path=db_file)
    await db.init_db()

    backup_path = await db.create_backup_file()
    assert backup_path is not None
    assert os.path.exists(backup_path)
    assert os.path.getsize(backup_path) > 0

    # Must not raise PermissionError
    os.remove(backup_path)
    assert not os.path.exists(backup_path)
    await db.close()


@pytest.mark.asyncio
async def test_story_queue_service_worker_lifecycle():
    """Verify story queue worker starts and stops gracefully with monotonic timer"""
    worker_task = asyncio.create_task(story_queue_service.start_worker())
    await asyncio.sleep(0.02)
    assert story_queue_service._is_running is True

    story_queue_service.stop_worker()
    assert story_queue_service._is_running is False
    if not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


def test_ai_paraphraser_hype_mode_preserves_html():
    """Verify AI paraphraser hype mode output retains valid HTML tags like <b> and <i>"""
    text = "O'zbekistonda yangi texnologik park ochilishi kutilmoqda"
    paraphrased = ai_paraphraser.paraphrase(text, mode="hype")
    assert "<b>" in paraphrased
    assert "</b>" in paraphrased
    assert "&lt;b&gt;" not in paraphrased


def test_keyboards_selected_active_markers():
    """Verify video watermark and translation keyboards show [Faol] without leading static emoji"""
    pair = ChannelPair(
        id=55,
        user_id=123,
        source_channel="-1001",
        target_channel="-1002",
        video_watermark_pos="center",
        target_lang="uz",
        auto_translate=True
    )

    # 1. Video watermark keyboard
    vwm_kb = get_video_watermark_keyboard(pair_id=55, pair=pair)
    flat_vwm = [b for row in vwm_kb.inline_keyboard for b in row]
    center_btn = [b for b in flat_vwm if "Center" in b.text][0]
    assert "[Faol]" in center_btn.text
    assert not center_btn.text.startswith("✅")
    assert center_btn.style == "success"

    # 2. Translate keyboard
    trans_kb = get_translate_lang_keyboard(pair_id=55, current_lang="uz")
    flat_trans = [b for row in trans_kb.inline_keyboard for b in row]
    uz_btn = [b for b in flat_trans if "O'zbekcha" in b.text][0]
    assert "[Faol]" in uz_btn.text
    assert not uz_btn.text.startswith("✅")


@pytest.mark.asyncio
async def test_db_composite_indexes_exist(tmp_path):
    """Verify all new composite indexes exist in database schema"""
    db_file = str(tmp_path / "test_indexes.db")
    db = DatabaseManager(db_path=db_file)
    await db.init_db()

    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in await cur.fetchall()}

    expected_indexes = [
        "idx_posted_stories_user_status",
        "idx_story_sources_user_active",
        "idx_story_queue_status_sched_score"
    ]
    for idx_name in expected_indexes:
        assert idx_name in indexes, f"Missing index: {idx_name}"

    await db.close()


def test_anti_reclaim_temp_dir_resolution():
    """Verify anti reclaim daemon uses system temp dir on any platform"""
    from deploy.anti_reclaim import STATUS_FILE
    expected_dir = os.path.abspath(tempfile.gettempdir())
    actual_dir = os.path.abspath(os.path.dirname(STATUS_FILE))
    assert actual_dir == expected_dir


def test_normalize_channel_input_extended_formats():
    """Verify normalize_channel_input parses private c/ links, web preview s/ links, and tg:// schemes"""
    from services.text_processor import TextProcessor
    assert TextProcessor.normalize_channel_input("https://t.me/c/1234567890/123") == "-1001234567890"
    assert TextProcessor.normalize_channel_input("t.me/c/987654321/42/99") == "-100987654321"
    assert TextProcessor.normalize_channel_input("https://t.me/c/555666777") == "-100555666777"
    assert TextProcessor.normalize_channel_input("https://t.me/s/kunuzofficial/123") == "@kunuzofficial"
    assert TextProcessor.normalize_channel_input("https://t.me/s/kunuzofficial") == "@kunuzofficial"
    assert TextProcessor.normalize_channel_input("tg://resolve?domain=kunuzofficial") == "@kunuzofficial"


def test_listing_analyzer_auto_price_extraction():
    """Verify ListingAnalyzer.analyze automatically parses price when existing_price is omitted"""
    from services.listing_analyzer import listing_analyzer
    text = "Chilonzor 9-kvartalda 2 xonali toza uy ijaraga beriladi. Narxi: 650$"
    meta = listing_analyzer.analyze(text)
    assert meta.price == 650.0
    assert meta.district == "Chilonzor"
    assert meta.rooms == 2


@pytest.mark.asyncio
async def test_chat_migration_handlers(tmp_path):
    """Verify both Telethon and Aiogram migration handlers update database pairs when groups upgrade"""
    from services.telethon_listener import telethon_listener
    from bot.handlers.cloner_menu import handle_aiogram_chat_migration

    mock_db = MagicMock()
    mock_db.get_all_active_pairs = AsyncMock(return_value=[
        ChannelPair(id=101, user_id=1, source_channel="-500", source_id=-500, target_channel="-600", target_id=-600),
        ChannelPair(id=102, user_id=2, source_channel="-600", source_id=-600, target_channel="@my_target", target_id=-100999)
    ])
    mock_db.update_pair_source_id = AsyncMock()
    mock_db.update_pair_target_id = AsyncMock()

    # 1. Telethon _handle_chat_migration: group -500 migrated to supergroup 1500 -> -1001500
    with patch("services.telethon_listener.db_manager", mock_db):
        await telethon_listener._handle_chat_migration(old_chat_id=-500, new_channel_id=1500)
        mock_db.update_pair_source_id.assert_called_with(101, -1001500)

    # 2. Aiogram handle_aiogram_chat_migration: group -600 migrated to supergroup -1001600
    msg_mock = MagicMock()
    msg_mock.chat.id = -600
    msg_mock.migrate_to_chat_id = -1001600

    mock_db.update_pair_source_id.reset_mock()
    mock_db.update_pair_target_id.reset_mock()

    with patch("bot.handlers.cloner_menu.db_manager", mock_db):
        await handle_aiogram_chat_migration(msg_mock)
        # Pair 101 has target_id=-600, Pair 102 has source_id=-600
        mock_db.update_pair_target_id.assert_called_with(101, -1001600)
        mock_db.update_pair_source_id.assert_called_with(102, -1001600)

