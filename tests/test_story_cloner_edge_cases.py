import os
import pytest
from PIL import Image
from services.story_cloner_service import StoryClonerService, story_cloner_service
from database.models import StorySettings, PostedStory
from database.db_manager import db_manager
from bot.handlers.story_menu import router as story_menu_router
from aiogram import Dispatcher


class MockMsg:
    def __init__(self, text: str = "", has_photo: bool = False, msg_id: int = 1, grouped_id: int = None):
        self.id = msg_id
        self.text = text
        self.message = text
        self.photo = object() if has_photo else None
        self.media = self.photo
        self.action = None
        self.grouped_id = grouped_id


def test_price_formats_and_cyrillic():
    s = StoryClonerService()
    # Cyrillic variants
    assert s.extract_price("Нархи: 700$") == 700.0
    assert s.extract_price("Нарх: 850 $") == 850.0
    assert s.extract_price("Нархи 900 у.е.") == 900.0
    assert s.extract_price("Цена 1000 доллар") == 1000.0
    assert s.extract_price("Нархи: 750 уе") == 750.0

    # Range format (should pick the first valid price >= 50)
    assert s.extract_price("700$ dan 1000$ gacha") == 700.0
    assert s.extract_price("Ijara 750-800$") == 750.0

    # Large values / Sales
    assert s.extract_price("Kvartira sotiladi: 65 000 $") == 65000.0


def test_thousands_dot_and_comma_separators():
    s = StoryClonerService()
    # Real estate listings with thousands separated by dot or comma
    assert s.extract_price("Narxi: 1.500$") == 1500.0
    assert s.extract_price("Ijara: 1,500 $") == 1500.0
    assert s.extract_price("Kvartira narxi: 65.000$") == 65000.0
    assert s.extract_price("65,000 $") == 65000.0
    assert s.extract_price("Arenda 2.000 у.е.") == 2000.0
    assert s.extract_price("120.000 USD") == 120000.0


def test_uzs_so_m_currency_filtering_and_conversion():
    s = StoryClonerService()
    st = StorySettings(user_id=1, min_price=700.0, require_photos=True, filter_demands=True, require_price=True)

    # 4 million UZS (~$312.5) -> parsed as ~$312.5, rejected because < $700!
    p_low = s.extract_price("Narxi: 4 000 000 so'm")
    assert p_low is not None and p_low < 700.0
    msg_low = MockMsg("Yunusobod 1 xona. Narxi: 4 000 000 so'm", has_photo=True)
    ok_low, _, _ = s.matches_filter(msg_low, st)
    assert ok_low is False

    # 15 million UZS (~$1171.9) -> parsed as ~$1171.9, accepted because >= $700!
    p_high = s.extract_price("Narxi: 15 000 000 so'm")
    assert p_high is not None and p_high >= 700.0
    msg_high = MockMsg("Yunusobod 3 xona. Narxi: 15 000 000 so'm", has_photo=True)
    ok_high, _, _ = s.matches_filter(msg_high, st)
    assert ok_high is True


def test_phone_number_and_square_meter_isolation():
    s = StoryClonerService()
    # Post with square meters, floors, phone numbers, and actual price
    text = """
🏢 Yunusobod 7-mavze
📐 77 kv.m, 3 xona
🏗 4/5/9 qavat
📞 Aloqa: +998901234567, 97-777-77-77
💵 Narxi: 850$ oylik to'lov
"""
    assert s.extract_price(text) == 850.0

    # Area directly next to arenda label should not become price
    text_arenda_area = "Arenda: 77 kv.m kvartira, narxi 800$"
    assert s.extract_price(text_arenda_area) == 800.0


def test_price_range_filtering():
    s = StoryClonerService()
    st = StorySettings(
        user_id=1,
        min_price=700.0,
        max_price=1500.0,
        require_photos=True,
        require_price=True,
        filter_demands=True
    )

    # Within range ($850) -> OK
    m_ok = MockMsg("Yunusobod 2 xona, narxi 850$", has_photo=True)
    ok, reason, price = s.matches_filter(m_ok, st)
    assert ok is True
    assert price == 850.0

    # Above max ($2000 > $1500) -> Rejected
    m_high = MockMsg("Luxury Penthouse, narxi 2000$", has_photo=True)
    ok, reason, price = s.matches_filter(m_high, st)
    assert ok is False
    assert "maksimal chegara" in reason

    # Below min ($650 < $700) -> Rejected
    m_low = MockMsg("1 xona arzon, narxi 650$", has_photo=True)
    ok, reason, price = s.matches_filter(m_low, st)
    assert ok is False
    assert "minimal chegara" in reason


def test_demand_vs_offer_distinction():
    s = StoryClonerService()

    demands = [
        "Arendaga kvartira kerak Yunusoboddan",
        "Oilaga 2 xona qidiryapman",
        "Zudlik bilan arenda olaman 800$",
        "Kvartira qidirilmoqda 3 xona",
        "Сниму квартиру для семьи, бюджет 1000$",
        "Ищем 2-комнатную возле метро",
        "Клиент бор, 3 хона керак срочно",
        "Запрос на аренду квартиры 700$"
    ]
    for d in demands:
        assert s.is_demand_post(d) is True, f"Failed for demand: {d}"

    offers = [
        "Yunusobod 6 mavze, 2 xona arendaga beriladi, narxi 750$",
        "Kvartira ijaraga beriladi, 3 xona, 900$",
        "Сдается 2-комнатная квартира с ремонтом, 800$",
        "Sotiladi: 3 xona novostroyka, 70000$",
        "Yangi remont qilingan uy ijaraga topshiriladi"
    ]
    for o in offers:
        assert s.is_demand_post(o) is False, f"Failed for offer: {o}"


def test_offer_with_conditions_not_falsely_rejected():
    s = StoryClonerService()
    st = StorySettings(user_id=1, min_price=700.0, require_photos=True, filter_demands=True, require_price=True)

    # Landlord specifying family condition in a genuine listing
    text_listing = "Yunusobod 4-mavze, 2 xona. Ijaraga beriladi. Narxi: 750$. Faqat oila kerak."
    msg = MockMsg(text_listing, has_photo=True)
    ok, reason, price = s.matches_filter(msg, st)
    assert ok is True, f"Valid listing with condition was falsely rejected: {reason}"
    assert price == 750.0

    text_tenant = "2 xonali kvartira arendaga topshiriladi. Kvartirant kerak. Narxi: 850$"
    msg2 = MockMsg(text_tenant, has_photo=True)
    ok2, reason2, price2 = s.matches_filter(msg2, st)
    assert ok2 is True, f"Valid listing with kvartirant kerak was falsely rejected: {reason2}"
    assert price2 == 850.0


@pytest.mark.asyncio
async def test_grouped_id_album_deduplication():
    await db_manager.init_db()
    uid = 888777111
    chan = "@test_realestate_channel"
    gid = 998877665544

    # Clean up test user
    async with db_manager.write_transaction() as db:
        await db.execute("DELETE FROM posted_stories WHERE user_id = ?", (uid,))
        await db.commit()

    # Before posting: not posted
    assert await db_manager.is_story_posted(uid, chan, 101, grouped_id=gid) is False

    # Record photo 1 of album
    await db_manager.record_posted_story(
        user_id=uid,
        source_channel=chan,
        source_id=12345,
        source_msg_id=101,
        story_id=555,
        price=850.0,
        caption_snippet="Test album post",
        grouped_id=gid
    )

    # Photo 1 is posted
    assert await db_manager.is_story_posted(uid, chan, 101) is True
    # Photo 2 (msg 102) with SAME grouped_id is also recognized as posted!
    assert await db_manager.is_story_posted(uid, chan, 102, grouped_id=gid) is True


def test_authentic_wallpaper_file_specs():
    s = StoryClonerService()
    bg_path = s.get_or_create_background(style="telegram_green")
    assert os.path.exists(bg_path)
    with Image.open(bg_path) as im:
        assert im.size == (1080, 1920)
        assert im.mode == "RGB"


def test_story_router_integration():
    if story_menu_router.parent_router is None:
        dp = Dispatcher()
        dp.include_router(story_menu_router)
    # Check that routers and handlers are properly registered
    assert len(story_menu_router.message.handlers) > 0
    assert len(story_menu_router.callback_query.handlers) > 0


@pytest.mark.asyncio
async def test_process_channel_message_queue_photo_path_safe():
    """Verify that when prime_hours_enabled queues a message, photo_path is bound and does not raise UnboundLocalError (#1)"""
    from unittest.mock import AsyncMock, MagicMock
    s = StoryClonerService()
    user_id = 999111
    st = StorySettings(
        user_id=user_id,
        is_active=True,
        source_channel="@test_ch",
        target_type="user",
        min_price=100.0,
        prime_hours_enabled=True,
        prime_hours_start="10:00",
        prime_hours_end="11:00",
        drip_delay_minutes=60
    )

    msg = MockMsg(
        text="Yunusobod 2 xona shinam xonadon ijaraga beriladi. Narxi: 500$ oyiga. Telefon: +998901234567",
        has_photo=True,
        msg_id=777
    )
    chat = MagicMock()
    chat.id = 123456
    chat.username = "test_ch"
    chat.title = "Test Real Estate"

    mock_client = MagicMock()
    mock_client.download_media = AsyncMock(return_value=None)

    # Calling _process_channel_message should execute safely without UnboundLocalError
    try:
        await s._process_channel_message(mock_client, user_id, st, msg, chat)
    except UnboundLocalError as err:
        pytest.fail(f"Raised UnboundLocalError: {err}")

