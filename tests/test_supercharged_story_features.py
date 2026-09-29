# -*- coding: utf-8 -*-
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from database.db_manager import DatabaseManager
from database.models import StorySettings, StorySourceChannel
from bot.keyboards.story_keyboards import (
    get_story_main_menu_keyboard,
    get_story_channels_keyboard,
    get_story_queue_keyboard,
    get_story_cooldown_preset_keyboard,
    get_story_daily_limit_preset_keyboard,
    get_story_filters_keyboard,
    get_story_auth_keyboard,
    get_story_logout_confirm_keyboard,
    get_story_back_keyboard
)


@pytest_asyncio.fixture
async def test_db(tmp_path):
    db_file = tmp_path / "test_supercharged.db"
    db = DatabaseManager(db_path=str(db_file))
    await db.init_db()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_multi_channel_crud(test_db):
    user_id = 998877

    # Initially empty
    channels = await test_db.get_story_source_channels(user_id)
    assert len(channels) == 0

    # Add channel 1
    cid1 = await test_db.add_story_source_channel(user_id, "@uy_bor", "Uy Bor Kanal", 1001)
    assert cid1 is not None

    # Add channel 2
    cid2 = await test_db.add_story_source_channel(user_id, "@tashkent_rent", "Tashkent Rent", 1002)
    assert cid2 is not None

    # Duplicate should return None
    cid_dup = await test_db.add_story_source_channel(user_id, "@uy_bor", "Uy Bor Kanal Duplicate", 1001)
    assert cid_dup is None

    # Verify list
    channels = await test_db.get_story_source_channels(user_id)
    assert len(channels) == 2
    assert {c.channel_username for c in channels} == {"@uy_bor", "@tashkent_rent"}

    # Delete channel 1
    deleted = await test_db.delete_story_source_channel(user_id, cid1)
    assert deleted is True

    # Verify 1 remains
    channels = await test_db.get_story_source_channels(user_id)
    assert len(channels) == 1
    assert channels[0].channel_username == "@tashkent_rent"


@pytest.mark.asyncio
async def test_deduplication_14_day_window(test_db):
    user_id = 112233
    fp = "sample_fingerprint_hash_abc123"

    # Initially not duplicate
    is_dup = await test_db.is_listing_duplicate(user_id, fp, days=14)
    assert is_dup is False

    # Record hash
    recorded = await test_db.record_listing_hash(user_id, fp, "@channel_a", 501)
    assert recorded is True

    # Now should be detected as duplicate within 14 days
    is_dup = await test_db.is_listing_duplicate(user_id, fp, days=14)
    assert is_dup is True

    # Another user should not collide
    is_dup_user2 = await test_db.is_listing_duplicate(999999, fp, days=14)
    assert is_dup_user2 is False


@pytest.mark.asyncio
async def test_queue_enqueue_and_priority(test_db):
    user_id = 556677
    now = datetime.now(timezone.utc)
    due_time_str = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    future_time_str = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    # Item 1: Lower score (60)
    id1 = await test_db.enqueue_story(
        user_id=user_id,
        source_channel="@channel_1",
        source_msg_id=101,
        payload={"note": "item1"},
        scheduled_at_utc=due_time_str,
        price=800.0,
        district="Chilonzor",
        score=60
    )

    # Item 2: Higher score (95) - also due
    id2 = await test_db.enqueue_story(
        user_id=user_id,
        source_channel="@channel_2",
        source_msg_id=202,
        payload={"note": "item2"},
        scheduled_at_utc=due_time_str,
        price=1800.0,
        district="Mirobod",
        score=95
    )

    # Item 3: Future time (not due yet)
    id3 = await test_db.enqueue_story(
        user_id=user_id,
        source_channel="@channel_3",
        source_msg_id=303,
        payload={"note": "item3"},
        scheduled_at_utc=future_time_str,
        price=1200.0,
        district="Yunusobod",
        score=85
    )

    due_items = await test_db.get_due_story_queue_items()
    # Due items should contain id2 and id1, ordered by score DESC (id2 first!)
    due_ids = [item.id for item in due_items]
    assert id2 in due_ids
    assert id1 in due_ids
    assert id3 not in due_ids
    assert due_ids.index(id2) < due_ids.index(id1)

    # Mark id2 done
    done = await test_db.mark_story_queue_item_done(id2, "sent")
    assert done is True

    # Check pending queue
    pending = await test_db.get_user_story_queue(user_id)
    pending_ids = [p.id for p in pending]
    assert id2 not in pending_ids
    assert id1 in pending_ids
    assert id3 in pending_ids


def test_keyboards_structure():
    st = StorySettings(
        user_id=123,
        source_channel="@test_ch",
        min_price=900.0,
        prime_hours_enabled=True,
        drip_delay_minutes=45,
        max_stories_per_day=5,
        enable_smart_badges=True
    )

    # Main menu keyboard
    kb_main = get_story_main_menu_keyboard(is_auth=True, is_active=True, has_source=True)
    callbacks = [b.callback_data for row in kb_main.inline_keyboard for b in row]
    assert "story_menu_channels" in callbacks
    assert "story_menu_queue" in callbacks
    assert "story_menu_filters" in callbacks
    assert "story_menu_test_post" in callbacks

    # Channels keyboard
    extra_channels = [
        StorySourceChannel(id=1, user_id=123, channel_username="@extra1", channel_title="Extra 1", is_active=1)
    ]
    kb_channels = get_story_channels_keyboard("@main_channel", extra_channels, target_type="self")
    ch_callbacks = [b.callback_data for row in kb_channels.inline_keyboard for b in row]
    assert "story_add_extra_channel" in ch_callbacks
    assert "story_del_src_1" in ch_callbacks
    assert "story_toggle_src_1" in ch_callbacks

    # Queue keyboard
    kb_queue = get_story_queue_keyboard(st, pending_count=3)
    q_callbacks = [b.callback_data for row in kb_queue.inline_keyboard for b in row]
    assert "story_toggle_prime_hours" in q_callbacks
    assert "story_menu_cooldown" in q_callbacks
    assert "story_menu_daily_limit" in q_callbacks
    # Each screen has its own badges toggle, so the screen it was tapped on is redrawn
    assert "story_toggle_badges_q" in q_callbacks
    assert "story_toggle_badges_f" in [b.callback_data for row in get_story_filters_keyboard(st).inline_keyboard for b in row]
    assert "story_toggle_pin" in q_callbacks
    assert "story_view_queue_list" in q_callbacks

    # Cooldown presets
    kb_cd = get_story_cooldown_preset_keyboard(45)
    cd_callbacks = [b.callback_data for row in kb_cd.inline_keyboard for b in row]
    assert "story_set_cooldown_45" in cd_callbacks

    # Daily limit presets
    kb_dl = get_story_daily_limit_preset_keyboard(5)
    dl_callbacks = [b.callback_data for row in kb_dl.inline_keyboard for b in row]
    assert "story_set_limit_5" in dl_callbacks


@pytest.mark.asyncio
async def test_story_settings_pin_to_profile(test_db):
    user_id = 777888
    # Default is True
    st_default = await test_db.get_story_settings(user_id)
    assert st_default.pin_to_profile is True

    # Update to False
    st_default.pin_to_profile = False
    await test_db.save_story_settings(st_default)

    st_updated = await test_db.get_story_settings(user_id)
    assert st_updated.pin_to_profile is False

    # Update back to True
    await test_db.update_story_settings(user_id, pin_to_profile=True)
    st_final = await test_db.get_story_settings(user_id)
    assert st_final.pin_to_profile is True


def test_story_main_menu_red_back_button():
    """Verify red danger back button exists at the very bottom of Story Main Menu"""
    kb = get_story_main_menu_keyboard(is_auth=True, is_active=True, has_source=True)
    assert len(kb.inline_keyboard) >= 6
    last_row = kb.inline_keyboard[-1]
    assert len(last_row) == 1
    back_btn = last_row[0]
    assert back_btn.callback_data == "menu_main"
    assert back_btn.style == "danger"
    assert "Asosiy Menyuga Qaytish" in back_btn.text


def test_story_auth_keyboards_and_logout_flow():
    """Verify auth keyboards and logout confirmation flow buttons"""
    # 1. Connected state keyboard
    kb_auth_true = get_story_auth_keyboard(is_auth=True)
    all_buttons = [btn for row in kb_auth_true.inline_keyboard for btn in row]
    callbacks = [btn.callback_data for btn in all_buttons]
    assert "story_auth_logout_confirm" in callbacks
    logout_btn = next(b for b in all_buttons if b.callback_data == "story_auth_logout_confirm")
    assert logout_btn.style == "danger"

    # 2. Logout confirmation keyboard
    kb_confirm = get_story_logout_confirm_keyboard()
    confirm_buttons = [btn for row in kb_confirm.inline_keyboard for btn in row]
    confirm_callbacks = [btn.callback_data for btn in confirm_buttons]
    assert "story_auth_logout_yes" in confirm_callbacks
    assert "story_menu_auth" in confirm_callbacks
    yes_btn = next(b for b in confirm_buttons if b.callback_data == "story_auth_logout_yes")
    assert yes_btn.style == "danger"
    cancel_btn = next(b for b in confirm_buttons if b.callback_data == "story_menu_auth")
    assert cancel_btn.style == "primary"

    # 3. Disconnected state keyboard
    kb_auth_false = get_story_auth_keyboard(is_auth=False)
    false_callbacks = [b.callback_data for row in kb_auth_false.inline_keyboard for b in row]
    assert "story_auth_start" in false_callbacks

    # 4. Universal back keyboard
    kb_back = get_story_back_keyboard()
    back_buttons = [btn for row in kb_back.inline_keyboard for btn in row]
    assert any(b.callback_data == "story_main_menu" and b.style == "primary" for b in back_buttons)
    assert any(b.callback_data == "menu_main" and b.style == "danger" for b in back_buttons)


@pytest.mark.asyncio
async def test_session_delete_and_story_deactivation(test_db):
    """Verify deleting a user session automatically disables active story monitoring"""
    user_id = 999111
    # Setup active story settings
    await test_db.update_story_settings(user_id, is_active=True, source_channel="@test_ch")
    st_before = await test_db.get_story_settings(user_id)
    assert st_before.is_active is True

    # Save session
    await test_db.save_user_session(
        user_id=user_id,
        session_str="mock_session_123",
        phone="+998901234567",
        first_name="Tester",
        last_name="",
        username="tester_vip"
    )
    sess = await test_db.get_user_session_info(user_id)
    assert sess is not None
    assert sess["phone"] == "+998901234567"

    # Delete session
    await test_db.delete_user_session(user_id)

    # Verify session is deleted
    sess_after = await test_db.get_user_session_info(user_id)
    assert sess_after is None

    # Verify story settings monitoring was automatically deactivated
    st_after = await test_db.get_story_settings(user_id)
    assert st_after.is_active is False

