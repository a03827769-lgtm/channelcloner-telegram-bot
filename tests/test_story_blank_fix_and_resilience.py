import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image

from services.story_renderer import story_card_renderer, StoryCardRenderer
from services.story_video_generator import story_video_generator
from services.story_cloner_service import StoryClonerService
from database.models import StorySettings


def test_create_photo_collage_empty_returns_none():
    res = story_card_renderer.create_photo_collage([])
    assert res is None

    res2 = story_card_renderer.create_photo_collage(["non_existent_path.jpg"])
    assert res2 is None


def test_render_story_composite_pil_text_only_is_compact(tmp_path):
    service = StoryClonerService()
    bg_green = service.get_or_create_background(style="telegram_green")
    out_file = str(tmp_path / "compact_text_card.jpg")

    result = story_card_renderer.render_story_composite_pil(
        bg_base_path=bg_green,
        channel_title="ARENDA UY",
        photo_paths=[],
        caption="Yunusobod 19-mavze, 3 xonali kvartira arendaga beriladi.\nNarxi: $850 / oy",
        price=850.0,
        output_path=out_file
    )
    assert os.path.exists(result)

    coords = story_card_renderer.get_last_card_coordinates()
    assert coords["h"] < 40.0
    assert 40.0 <= coords["y"] <= 60.0


def test_create_video_story_empty_photos_raises_value_error():
    with pytest.raises(ValueError, match="fotosurat talab qilinadi"):
        story_video_generator.create_video_story(
            photo_paths=[],
            channel_title="ARENDA UY",
            caption="Test caption"
        )


@pytest.mark.asyncio
async def test_create_video_story_async_empty_photos_raises_value_error():
    with pytest.raises(ValueError, match="fotosurat talab qilinadi"):
        await story_video_generator.create_video_story_async(
            photo_paths=[],
            channel_title="ARENDA UY",
            caption="Test caption"
        )


@pytest.mark.asyncio
async def test_post_story_from_channel_aborts_when_require_photos_active_and_no_photos(tmp_path):
    service = StoryClonerService()
    from telethon import types
    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)

    mock_msg = MagicMock()
    mock_msg.id = 123
    mock_msg.message = "Yunusobod 3 xona 800$"
    mock_msg.photo = None
    mock_msg.document = None
    mock_msg.grouped_id = None
    mock_msg.fwd_from = None
    mock_msg.date = None
    mock_client.get_messages.return_value = mock_msg

    dummy_ch = types.InputChannel(channel_id=12345, access_hash=67890)

    with patch("services.story_cloner_service.db_manager.is_vip", new_callable=AsyncMock) as mock_vip:
        mock_vip.return_value = True
        with patch("services.story_cloner_service.db_manager.get_story_settings", new_callable=AsyncMock) as mock_st:
            mock_st.return_value = StorySettings(user_id=1001, require_photos=True)

            ok, story_id, err_msg, _ = await service.post_story_from_channel(
                client=mock_client,
                channel_identifier=dummy_ch,
                msg_id=123,
                user_id=1001
            )
            assert ok is False
            assert "fotosuratlar topilmadi" in err_msg.lower()


def test_extract_story_lines_preserves_text_with_mixed_contacts():
    caption = "Yunusobod 19-mavze, 3 xonali kvartira arendaga beriladi.\nEvroremont qilingan, hamma jihozlari bor.\nNarxi: 850 $\nTel: +998901234567\nhttps://t.me/arenda_uy"
    lines = StoryCardRenderer.extract_story_lines(caption, price=850.0)
    assert len(lines) >= 2
    assert any("Yunusobod" in l for l in lines)
    assert any("850" in l for l in lines)
    assert not any("https://" in l for l in lines)


@pytest.mark.asyncio
async def test_post_story_from_channel_finds_historical_album_siblings(tmp_path):
    """Verify that when a message is from the past, surrounding IDs fetch finds all album siblings and photos."""
    service = StoryClonerService()
    from telethon import types
    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)

    target_msg = MagicMock()
    target_msg.id = 5818
    target_msg.grouped_id = 999888
    target_msg.message = None  # Caption is on sibling #5815
    target_msg.photo = MagicMock()
    target_msg.document = None
    target_msg.fwd_from = None
    target_msg.date = None

    sibling_with_caption = MagicMock()
    sibling_with_caption.id = 5815
    sibling_with_caption.grouped_id = 999888
    sibling_with_caption.message = "✨✨✨ СДАЁТСЯ ✨✨✨\n\nРайон: Яшнабад\nАдрес: Фаргона йули\nЦена: 650 $"
    sibling_with_caption.photo = MagicMock()
    sibling_with_caption.document = None

    # When get_messages is called with ids=5818: return target_msg
    # When get_messages is called with ids=surrounding_ids: return [sibling_with_caption, target_msg]
    async def fake_get_messages(entity, ids=None, limit=None, offset_id=None):
        if ids == 5818:
            return target_msg
        if isinstance(ids, list):
            return [sibling_with_caption, target_msg]
        if limit:
            # Older naive method returns recent 30 (which has nothing from 5818)
            return []
        return []

    mock_client.get_messages = AsyncMock(side_effect=fake_get_messages)

    fake_photo_file = str(tmp_path / "photo_5818.jpg")
    with open(fake_photo_file, "wb") as f:
        img = Image.new("RGB", (400, 400), (100, 150, 200))
        img.save(f, format="JPEG")

    mock_client.download_media = AsyncMock(return_value=fake_photo_file)

    dummy_ch = types.InputChannel(channel_id=12345, access_hash=67890)

    with patch("services.story_cloner_service.db_manager.is_vip", new_callable=AsyncMock) as mock_vip:
        mock_vip.return_value = True
        with patch("services.story_cloner_service.db_manager.get_story_settings", new_callable=AsyncMock) as mock_st:
            mock_st.return_value = StorySettings(user_id=1001, require_photos=True)
            with patch("services.story_video_generator.story_video_generator.create_video_story_async", new_callable=AsyncMock) as mock_vid:
                fake_vid = str(tmp_path / "story_video.mp4")
                with open(fake_vid, "wb") as f:
                    f.write(b"video")
                mock_vid.return_value = (fake_vid, {"x": 50, "y": 50, "w": 80, "h": 60})
                mock_client.upload_file = AsyncMock(return_value=MagicMock())
                mock_client.return_value = MagicMock(updates=[types.UpdateStoryID(id=99, random_id=123)])

                ok, story_id, res_info, _ = await service.post_story_from_channel(
                    client=mock_client,
                    channel_identifier=dummy_ch,
                    msg_id=5818,
                    user_id=1001
                )
                assert ok is True
                assert story_id == 99
                # Verify video story was called with photos and the sibling's caption
                mock_vid.assert_called_once()
                call_kwargs = mock_vid.call_args.kwargs
                assert len(call_kwargs["photo_paths"]) >= 1
                assert "Яшнабад" in call_kwargs["caption"]
                assert call_kwargs["price"] == 650.0


@pytest.mark.asyncio
async def test_daily_limit_presets_and_custom_limit_up_to_100():
    from bot.keyboards.story_keyboards import get_story_daily_limit_preset_keyboard
    from bot.handlers.story_menu import cb_story_set_limit, process_custom_daily_limit
    from database.db_manager import db_manager

    kb = get_story_daily_limit_preset_keyboard(current_limit=50)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "story_set_limit_5" in callbacks
    assert "story_set_limit_20" in callbacks
    assert "story_set_limit_50" in callbacks
    assert "story_set_limit_100" in callbacks
    assert "story_custom_daily_limit" in callbacks

    # Test cb_story_set_limit with 100
    mock_cb = AsyncMock()
    mock_cb.from_user.id = 999111
    mock_cb.data = "story_set_limit_100"
    mock_state = AsyncMock()

    with patch("bot.handlers.story_menu.cb_story_menu_queue", new_callable=AsyncMock) as mock_menu:
        await cb_story_set_limit(mock_cb, mock_state)
        mock_cb.answer.assert_called()
        st = await db_manager.get_story_settings(999111)
        assert st.max_stories_per_day == 100

    # Test process_custom_daily_limit with valid 75
    mock_msg = AsyncMock()
    mock_msg.from_user.id = 999111
    mock_msg.text = "75"
    await process_custom_daily_limit(mock_msg, mock_state)
    st = await db_manager.get_story_settings(999111)
    assert st.max_stories_per_day == 75

    # Test process_custom_daily_limit with out of bounds 150
    mock_msg.text = "150"
    await process_custom_daily_limit(mock_msg, mock_state)
    assert "Cheklovdan oshib ketdi" in mock_msg.answer.call_args[0][0]


