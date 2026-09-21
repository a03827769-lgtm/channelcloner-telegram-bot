import pytest
import os
import aiosqlite
from datetime import datetime, timezone, timedelta
from database.db_manager import DatabaseManager
from database.models import Subscription, ChannelPair
from services.text_processor import TextProcessor
from services.ai_paraphraser import ai_paraphraser
from services.watermark_service import watermark_service
from services.cloner_engine import cloner_engine

@pytest.mark.asyncio
async def test_paid_notified_lifecycle(tmp_path):
    db_file = str(tmp_path / "test_paid_notified.db")
    db = DatabaseManager(db_path=db_file)
    await db.init_db()

    # Create user and paid subscription
    await db.get_or_create_user(1001, "Test Paid User", "testpaid")
    await db.activate_subscription(1001, tier="pro", stars=100, charge_id="test_charge_1", days=2)

    sub = await db.get_user_subscription(1001)
    assert sub.tier == "pro"
    assert sub.paid_notified is False

    # Check expiring paid users to notify
    expiring = await db.get_expiring_paid_users_to_notify()
    user_ids = [u[0] for u in expiring]
    assert 1001 in user_ids

    # Mark as notified
    await db.mark_paid_sub_notified(1001)

    sub_after = await db.get_user_subscription(1001)
    assert sub_after.paid_notified is True
    # Trial notified must remain unaffected
    assert sub_after.trial_notified is False

    # Should no longer be returned in expiring notification list
    expiring_after = await db.get_expiring_paid_users_to_notify()
    assert 1001 not in [u[0] for u in expiring_after]

    # Re-activating should reset paid_notified to False
    await db.activate_subscription(1001, tier="vip", stars=300, charge_id="test_charge_2", days=30)
    sub_renewed = await db.get_user_subscription(1001)
    assert sub_renewed.paid_notified is False

    await db.close()

@pytest.mark.asyncio
async def test_get_users_detailed_distinct_channels(tmp_path):
    db_file = str(tmp_path / "test_distinct.db")
    db = DatabaseManager(db_path=db_file)
    await db.init_db()

    await db.get_or_create_user(2001, "Multi Channel User", "multichan")
    await db.add_channel_pair(2001, "@src1", "Source 1", "@tgt1", "Target 1")
    await db.add_channel_pair(2001, "@src2", "Source 2", "@tgt2", "Target 2")

    users = await db.get_users_detailed(limit=10)
    target = next((u for u in users if u["user_id"] == 2001), None)
    assert target is not None
    assert target["channel_count"] == 2

    # Search user
    search_res = await db.search_users("multichan")
    assert len(search_res) == 1
    assert search_res[0]["channel_count"] == 2

    await db.close()

def test_text_processor_match_case_title():
    # Multi-word title case preservation
    res1 = TextProcessor._match_case("Kun Uz", "Kanal Nomi")
    assert res1 == "Kanal Nomi"

    # All uppercase
    res2 = TextProcessor._match_case("KUN UZ", "kanal nomi")
    assert res2 == "KANAL NOMI"

    # Capitalized single word
    res3 = TextProcessor._match_case("Kun", "kanal")
    assert res3 == "Kanal"

def test_text_processor_void_tags_omitted_from_closing():
    html_text = "<b>Assalomu alaykum</b><br>Bu yangilik.<hr>Davomi bor."
    caption, overflow = TextProcessor.fit_caption_limit(html_text, max_limit=30)
    # Ensure neither </br> nor </hr> are appended
    assert "</br>" not in caption
    assert "</hr>" not in caption
    if overflow:
        assert "<br>" not in overflow
        assert "<hr>" not in overflow

def test_text_processor_attach_signature_entity_truncation():
    # Long text ending near boundary with incomplete entity
    base_text = "A" * 4000 + "<b>Test &amp; info</b>"
    sig = "\n\n@mychannel"
    result = TextProcessor.attach_signature(base_text, sig)
    assert len(result) <= 4096
    assert result.endswith(sig)
    assert "&am\n" not in result

def test_ai_paraphraser_hype_hash_diversity():
    h1 = "O'zbekistonda ob-havo keskin o'zgaradi"
    h2 = "Toshkentda yangi metro bekati ochildi"
    p1 = ai_paraphraser.paraphrase(h1, mode="hype")
    p2 = ai_paraphraser.paraphrase(h2, mode="hype")
    assert p1 != ""
    assert p2 != ""
    assert "<b>" in p1

def test_watermark_service_small_image_bounds(tmp_path):
    from PIL import Image
    # Create small 50x50 test image and 100x100 logo
    img_path = str(tmp_path / "small.jpg")
    logo_path = str(tmp_path / "logo.png")
    out_path = str(tmp_path / "wm_small.jpg")

    Image.new("RGB", (60, 60), (200, 200, 200)).save(img_path)
    Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(logo_path)

    res = watermark_service.apply_logo_watermark(img_path, logo_path, output_path=out_path)
    assert res == out_path
    assert os.path.exists(out_path)
    assert os.path.getsize(out_path) > 0
