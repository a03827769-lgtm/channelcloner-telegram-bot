import os
import pytest
from PIL import Image

from database.models import StorySettings
from database.db_manager import db_manager
from services.story_cloner_service import StoryClonerService
from telethon import types


@pytest.fixture
async def isolated_db_manager(tmp_path):
    """Points the shared db_manager at a temporary database for one test and restores it afterwards."""
    original_path = db_manager.db_path
    await db_manager.close()
    db_manager.db_path = str(tmp_path / "story_cloner.db")
    yield db_manager
    await db_manager.close()
    db_manager.db_path = original_path


class MockMessage:
    def __init__(self, text: str = "", has_photo: bool = False, has_video: bool = False, msg_id: int = 1):
        self.id = msg_id
        self.text = text
        self.message = text
        self.photo = object() if has_photo else None
        self.video = object() if has_video else None
        self.media = self.photo or self.video
        self.action = None


def test_price_extraction_basic():
    service = StoryClonerService()
    assert service.extract_price("Narxi: 750$") == 750.0
    assert service.extract_price("Narxi 700 $") == 700.0
    assert service.extract_price("Ijara narxi: $800") == 800.0
    assert service.extract_price("Цена: 1200 у.е.") == 1200.0
    assert service.extract_price("1500 USD / month") == 1500.0
    assert service.extract_price("700$ dan boshlab") == 700.0
    assert service.extract_price("Narxi: 1 200 $") == 1200.0


def test_price_extraction_distractors():
    service = StoryClonerService()
    # Phone numbers and room numbers should NOT trigger as price
    text = "Yunusobod 4-mavze, 2 xona, 4-qavat, tel: +998901234567. Narxi: 750$"
    assert service.extract_price(text) == 750.0

    # No price mentioned
    text_no_price = "2 xona evroremont, barcha qulayliklar bor, Yunusobod metrosi yonida."
    assert service.extract_price(text_no_price) is None


def test_demand_post_detection():
    service = StoryClonerService()
    # Demands (should be True)
    assert service.is_demand_post("Menga 2 xonali kvartira kerak, budjet 800$") is True
    assert service.is_demand_post("Ищу квартиру в Юнусабаде до 1000$") is True
    assert service.is_demand_post("Клиент бор, 3 хона керак, 1200$ гача") is True
    assert service.is_demand_post("Arenda kerak zudlik bilan") is True
    assert service.is_demand_post("Kvartira olmoqchiman oilamiz bilan") is True

    # Offers / Listings (should be False)
    assert service.is_demand_post("Yunusobod 6 mavze, 2 xona arendaga beriladi, 750$") is False
    assert service.is_demand_post("Сдается 2-комнатная квартира, цена 900$") is False
    assert service.is_demand_post("Kvartira sotiladi, 65000$") is False


def test_matches_filter_logic():
    service = StoryClonerService()
    st = StorySettings(user_id=1, min_price=700.0, require_photos=True, filter_demands=True, require_price=True)

    # 1. Valid listing with photo and $750 price
    msg_valid = MockMessage("Yunusobod 2 xona, narxi 750$", has_photo=True)
    ok, reason, price = service.matches_filter(msg_valid, st)
    assert ok is True
    assert price == 750.0

    # 2. Lower price ($600 < $700) -> Rejected
    msg_cheap = MockMessage("Chilonzor 1 xona, narxi 600$", has_photo=True)
    ok, reason, price = service.matches_filter(msg_cheap, st)
    assert ok is False
    assert "minimal chegara" in reason

    # 3. No photo -> Rejected
    msg_no_photo = MockMessage("Yunusobod 2 xona, narxi 850$", has_photo=False)
    ok, reason, price = service.matches_filter(msg_no_photo, st)
    assert ok is False
    assert "rasm yoki video yo'q" in reason

    # 4. Demand post with >= $700 -> Rejected
    msg_demand = MockMessage("Kvartira kerak 2 xona, 800$ gacha", has_photo=True)
    ok, reason, price = service.matches_filter(msg_demand, st)
    assert ok is False
    assert "talab" in reason.lower() or "qidiruv" in reason.lower()


def test_background_assets():
    service = StoryClonerService()
    bg_green = service.get_or_create_background(style="telegram_green")
    assert os.path.exists(bg_green)
    with Image.open(bg_green) as im:
        assert im.size == (1080, 1920)

    bg_dark = service.get_or_create_background(style="luxury_dark")
    assert os.path.exists(bg_dark)
    with Image.open(bg_dark) as im:
        assert im.size == (1080, 1920)


def test_story_tl_types_construction():
    """The post link area of a story covers the rendered card and points at the source post"""
    channel = types.InputChannel(channel_id=1234567, access_hash=987654321)
    area = StoryClonerService.post_link_area({"x": 50.0, "y": 47.5, "w": 81.5, "h": 60.0}, channel, 999)
    assert isinstance(area, types.InputMediaAreaChannelPost)
    assert area.msg_id == 999 and area.channel.channel_id == 1234567
    assert (area.coordinates.x, area.coordinates.y, area.coordinates.w, area.coordinates.h) == (50.0, 47.5, 81.5, 60.0)
    # Without a resolvable channel there is no area (the story is posted without the link)
    assert StoryClonerService.post_link_area({"x": 1}, None, 999) is None


@pytest.mark.asyncio
async def test_database_user_session_and_settings(isolated_db_manager):
    await db_manager.init_db()

    test_uid = 999999001
    test_session = "1Babcde_TEST_SESSION_STRING_FOR_VERIFICATION"

    # Clean up any previous test remnants
    async with db_manager.write_transaction() as db:
        await db.execute("DELETE FROM user_sessions WHERE user_id = ?", (test_uid,))
        await db.execute("DELETE FROM story_settings WHERE user_id = ?", (test_uid,))
        await db.execute("DELETE FROM posted_stories WHERE user_id = ?", (test_uid,))
        await db.commit()

    # Save session
    saved = await db_manager.save_user_session(
        user_id=test_uid,
        session_str=test_session,
        phone="+998901234567",
        first_name="Alisher",
        username="alisher_makler"
    )
    assert saved is True

    # Retrieve session decrypted
    retrieved = await db_manager.get_user_session(test_uid)
    assert retrieved == test_session

    info = await db_manager.get_user_session_info(test_uid)
    assert info is not None
    assert info["phone"] == "+998901234567"
    assert info["first_name"] == "Alisher"

    # Save & retrieve story settings
    st = StorySettings(
        user_id=test_uid,
        source_channel="@yunsabod_test",
        source_title="Yunusobod Test",
        min_price=700.0,
        require_photos=True,
        require_price=True,
        background_style="telegram_green",
        is_active=True
    )
    await db_manager.save_story_settings(st)

    fetched_st = await db_manager.get_story_settings(test_uid)
    assert fetched_st.source_channel == "@yunsabod_test"
    assert fetched_st.min_price == 700.0
    assert fetched_st.background_style == "telegram_green"

    # Check deduplication
    assert await db_manager.is_story_posted(test_uid, "@yunsabod_test", 101) is False
    await db_manager.record_posted_story(
        user_id=test_uid,
        source_channel="@yunsabod_test",
        source_id=None,
        source_msg_id=101,
        story_id=5555,
        price=850.0,
        caption_snippet="Test 850$ 2 xona"
    )
    assert await db_manager.is_story_posted(test_uid, "@yunsabod_test", 101) is True

    stats = await db_manager.get_story_stats(test_uid)
    assert stats["total_posted"] >= 1
    assert len(stats["recent"]) >= 1

    # Cleanup test user
    await db_manager.delete_user_session(test_uid)
    assert await db_manager.get_user_session(test_uid) is None


def test_story_card_renderer_output(tmp_path):
    from services.story_renderer import story_card_renderer
    service = StoryClonerService()
    bg_green = service.get_or_create_background(style="telegram_green")

    # Create 3 dummy photo files
    dummy_photos = []
    for i in range(3):
        p_file = tmp_path / f"photo_{i}.jpg"
        im = Image.new("RGB", (600, 400), (50 * i, 100, 150))
        im.save(str(p_file))
        dummy_photos.append(str(p_file))

    out_file = str(tmp_path / "test_out_composite.jpg")
    result_path = story_card_renderer.render_story_composite(
        bg_base_path=bg_green,
        channel_title="ARENDA UY",
        photo_paths=dummy_photos,
        caption="📍 Yunusobod 6-mavze, 2 xona\n💰 Narxi: 850 $",
        price=850.0,
        output_path=out_file
    )
    assert os.path.exists(result_path)
    with Image.open(result_path) as res_im:
        assert res_im.size == (1080, 1920)


@pytest.mark.asyncio
async def test_start_monitor_rebinds_event_handler_on_channel_update():
    from unittest.mock import AsyncMock, MagicMock, patch
    service = StoryClonerService()
    test_uid = 77766655
    mock_client = MagicMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_entity = AsyncMock(side_effect=lambda x: MagicMock(id=abs(hash(str(x))) % 100000))
    mock_client.add_event_handler = MagicMock()
    mock_client.remove_event_handler = MagicMock()

    service._user_clients[test_uid] = mock_client

    with patch.object(db_manager, "is_vip", AsyncMock(return_value=True)), \
         patch.object(db_manager, "get_story_settings", AsyncMock(return_value=StorySettings(user_id=test_uid, is_active=True, source_channel="@initial_chan"))), \
         patch.object(db_manager, "get_story_source_channels", AsyncMock(return_value=[])):
        ok1 = await service.start_monitor_for_user(test_uid)
        assert ok1 is True
        assert mock_client.add_event_handler.call_count == 1
        initial_handler = service._active_channel_handlers[test_uid]

        # Re-triggering start_monitor_for_user (as happens when channel is added) unbinds old handler and re-binds updated one
        ok2 = await service.start_monitor_for_user(test_uid)
        assert ok2 is True
        assert mock_client.remove_event_handler.call_count == 1
        mock_client.remove_event_handler.assert_called_with(initial_handler)
        assert mock_client.add_event_handler.call_count == 2


