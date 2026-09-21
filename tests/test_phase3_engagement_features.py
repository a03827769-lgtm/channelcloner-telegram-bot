import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.ai_paraphraser import ai_paraphraser
from bot.handlers.comment_moderator import CommentModerator
from services.market_analytics import market_analytics_service
from database.db_manager import db_manager
from database.models import ChannelPair


def test_ai_paraphraser_new_tones_and_html_preservation():
    text = "Kvartira sotiladi <b>markazda</b>. Narxi: <i>$80 000</i>."

    # 1. Luxury tone
    lux = ai_paraphraser.paraphrase(text, mode="luxury")
    assert "PRESTIJLI KO'CHMAS MULK TAKLIFI" in lux
    assert "<b>markazda</b>" in lux
    assert "<i>$80 000</i>" in lux
    assert "Eksklyuziv shinamlik" in lux

    # 2. Urgency tone
    urg = ai_paraphraser.paraphrase(text, mode="urgency")
    assert "QAYNOQ TAKLIF" in urg
    assert "<b>markazda</b>" in urg
    assert "Tezkor xaridor" in urg

    # 3. Conversational tone
    conv = ai_paraphraser.paraphrase(text, mode="conversational")
    assert "Assalomu alaykum" in conv
    assert "<b>markazda</b>" in conv
    assert "Savollaringiz bo'lsa" in conv


@pytest.mark.asyncio
async def test_cloner_engine_process_post_text_tone_of_voice():
    from services.cloner_engine import cloner_engine

    pair = ChannelPair(
        id=1,
        user_id=111,
        source_channel="@src",
        target_channel="@dst",
        tone_of_voice="luxury"
    )

    raw_html = "Yunusobod 4-mavze 2 xona <b>yaxshi holatda</b>."
    with patch.object(db_manager, "get_user_subscription", new=AsyncMock()) as mock_sub:
        mock_sub.return_value.is_active = True
        mock_sub.return_value.tier = "vip"

        processed = await cloner_engine.process_post_text(raw_html, pair)
        assert "PRESTIJLI KO'CHMAS MULK TAKLIFI" in processed
        assert "<b>yaxshi holatda</b>" in processed


def test_comment_moderator_detection():
    # 1. Spam detection
    spam1 = "Do'stlar, 1win ga kiring 500% bonus oling: https://t.me/+AbCdEf"
    is_spam, reason = CommentModerator.is_spam(spam1)
    assert is_spam is True

    spam2 = "Aviator signallari 100% kafolat kunlik daromad"
    is_spam, reason = CommentModerator.is_spam(spam2)
    assert is_spam is True

    # 2. Clean comment
    clean = "Assalomu alaykum, kvartirani borib ko'rsa bo'ladimi?"
    is_spam, _ = CommentModerator.is_spam(clean)
    assert is_spam is False

    # 3. Price inquiries
    inq1 = "Salom, narxi qancha ekan?"
    assert CommentModerator.is_price_inquiry(inq1) is True

    inq2 = "Ijaraga nechpul beriladi?"
    assert CommentModerator.is_price_inquiry(inq2) is True

    inq3 = "Qayerda joylashgan?"
    assert CommentModerator.is_price_inquiry(inq3) is False


@pytest.mark.asyncio
async def test_inline_search_query_execution(tmp_path):
    test_db = str(tmp_path / "test_inline.db")
    db_manager.db_path = test_db
    await db_manager.init_db()

    await db_manager.get_or_create_user(user_id=1, full_name="Tester", username="tester")
    pair_id = await db_manager.add_channel_pair(user_id=1, source_channel="@tashkent_src", target_channel="@tashkent_dst")

    # Insert sample cloned listings into database
    await db_manager.record_cloned_message(
        pair_id=pair_id,
        source_msg_id=10,
        target_msg_id=100,
        media_type="photo",
        source_channel="@tashkent_src",
        target_channel="@tashkent_dst",
        price=55000.0,
        last_caption="Chilonzor 7-mavze 3 xonali xonadon $55 000"
    )
    await db_manager.record_cloned_message(
        pair_id=pair_id,
        source_msg_id=11,
        target_msg_id=101,
        media_type="photo",
        source_channel="@tashkent_src",
        target_channel="@tashkent_dst",
        price=120000.0,
        last_caption="Mirobod tumani hashamatli novostroyka $120 000"
    )

    from bot.handlers.inline_search import inline_real_estate_search
    mock_iq = MagicMock()
    mock_iq.query = "Chilonzor"
    mock_iq.from_user.username = "testuser"
    mock_iq.answer = AsyncMock()

    await inline_real_estate_search(mock_iq)

    mock_iq.answer.assert_called_once()
    results = mock_iq.answer.call_args[1]["results"]
    assert len(results) >= 1
    assert "55000" in results[0].title or "55 000" in results[0].description

    await db_manager.close()


@pytest.mark.asyncio
async def test_market_analytics_daily_briefing(tmp_path):
    test_db = str(tmp_path / "test_analytics.db")
    db_manager.db_path = test_db
    await db_manager.init_db()

    await db_manager.get_or_create_user(user_id=1, full_name="Tester", username="tester")
    pair_id = await db_manager.add_channel_pair(user_id=1, source_channel="@tashkent_src", target_channel="@tashkent_dst")

    # Seed listings
    await db_manager.record_cloned_message(
        pair_id=pair_id, source_msg_id=1, target_msg_id=1, price=60000.0,
        last_caption="Chilonzor 2 xona shinam uy"
    )
    await db_manager.record_cloned_message(
        pair_id=pair_id, source_msg_id=2, target_msg_id=2, price=90000.0,
        last_caption="Chilonzor 3 xona evroremont"
    )

    stats = await market_analytics_service.get_daily_market_stats(days=1)
    assert stats["total_listings"] == 2
    assert stats["avg_price"] == 75000.0
    assert stats["min_price"] == 60000.0
    assert stats["max_price"] == 90000.0
    assert stats["top_district"] == "Chilonzor"

    # Text digest formatting
    digest = market_analytics_service.format_digest_text(stats)
    assert "KUNLIK KO'CHMAS MULK BOZORI TAHLILI" in digest
    assert "$75000" in digest
    assert "Chilonzor" in digest

    # Voice script generation
    script = market_analytics_service.generate_briefing_voice_script(stats)
    assert "Bugun jami 2 ta" in script
    assert "75 ming dollar" in script

    # Test send_daily_briefing with mocked bot and tts
    mock_bot = AsyncMock()
    with patch.object(market_analytics_service, "generate_audio_podcast", new=AsyncMock(return_value=None)):
        await market_analytics_service.send_daily_briefing(mock_bot, 999)
        mock_bot.send_message.assert_called_once()
        assert "KUNLIK KO'CHMAS MULK BOZORI TAHLILI" in mock_bot.send_message.call_args[1]["text"]

    await db_manager.close()
