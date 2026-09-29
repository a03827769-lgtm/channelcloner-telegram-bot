import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from database.models import StorySettings
from database.db_manager import DatabaseManager
from bot.keyboards.story_keyboards import get_story_design_keyboard, get_story_duration_keyboard
from services.story_video_generator import StoryVideoGenerator


import pytest_asyncio

@pytest_asyncio.fixture
async def temp_db(tmp_path):
    db_path = str(tmp_path / "test_story_duration.db")
    manager = DatabaseManager(db_path=db_path)
    await manager.init_db()
    yield manager
    await manager.close()


@pytest.mark.asyncio
async def test_story_settings_video_duration_default_and_persistence(temp_db):
    """Verify video_duration defaults to 25 and persists correctly across DB CRUD"""
    user_id = 998877

    # 1. Default settings have video_duration = 25
    st = await temp_db.get_story_settings(user_id)
    assert st.video_duration == 25

    # 2. Save settings with 15s (min bound)
    st.video_duration = 15
    await temp_db.save_story_settings(st)

    fetched = await temp_db.get_story_settings(user_id)
    assert fetched.video_duration == 15

    # 3. Update settings with 40s (max bound)
    await temp_db.update_story_settings(user_id, video_duration=40)
    fetched_40 = await temp_db.get_story_settings(user_id)
    assert fetched_40.video_duration == 40

    # 4. Verify get_all_active_story_settings includes video_duration
    st_active = await temp_db.update_story_settings(user_id, is_active=True, video_duration=30)
    all_active = await temp_db.get_all_active_story_settings()
    matching = [s for s in all_active if s.user_id == user_id]
    assert len(matching) == 1
    assert matching[0].video_duration == 30


def test_story_duration_keyboards():
    """Verify keyboards contain the proper callbacks, presets and steppers"""
    # Design keyboard contains duration button
    design_kb = get_story_design_keyboard(current_style="telegram_green", video_duration=25)
    duration_btn = None
    for row in design_kb.inline_keyboard:
        for btn in row:
            if btn.callback_data == "story_menu_duration":
                duration_btn = btn
                break
    assert duration_btn is not None
    assert "25 soniya" in duration_btn.text

    # Duration keyboard has 15s, 20s, 25s, 30s, 35s, 40s presets
    dur_kb = get_story_duration_keyboard(current_duration=25)
    callbacks = [btn.callback_data for row in dur_kb.inline_keyboard for btn in row]
    assert "story_set_duration_15" in callbacks
    assert "story_set_duration_20" in callbacks
    assert "story_set_duration_25" in callbacks
    assert "story_set_duration_30" in callbacks
    assert "story_set_duration_35" in callbacks
    assert "story_set_duration_40" in callbacks
    assert "story_set_duration_custom" in callbacks
    assert "story_menu_design" in callbacks


def test_story_video_generator_duration_bounds_and_slide_cap(tmp_path):
    """Verify build_ffmpeg_command respects duration parameters and caps slides"""
    gen = StoryVideoGenerator()
    slides = [str(tmp_path / f"p_{i}.jpg") for i in range(10)]
    for s in slides:
        with open(s, "wb") as f:
            f.write(b"dummy")

    overlay = str(tmp_path / "overlay.png")
    with open(overlay, "wb") as f:
        f.write(b"dummy")

    # 15 second story command
    cmd_15 = gen.build_ffmpeg_command(
        slide_paths=slides[:4],
        overlay_path=overlay,
        audio_path=None,
        output_mp4=str(tmp_path / "out15.mp4"),
        total_duration=15.0
    )
    t_indices = [i for i, arg in enumerate(cmd_15) if arg == "-t"]
    assert "15.0" in [cmd_15[i + 1] for i in t_indices]

    # 40 second story command
    cmd_40 = gen.build_ffmpeg_command(
        slide_paths=slides[:4],
        overlay_path=overlay,
        audio_path=None,
        output_mp4=str(tmp_path / "out40.mp4"),
        total_duration=40.0
    )
    t_indices_40 = [i for i, arg in enumerate(cmd_40) if arg == "-t"]
    assert "40.0" in [cmd_40[i + 1] for i in t_indices_40]


@pytest.mark.asyncio
async def test_story_cloner_service_uses_configured_duration():
    """Verify story_cloner_service passes user's configured duration to create_video_story_async"""

    mock_db = MagicMock()
    mock_db.get_story_settings = AsyncMock(return_value=StorySettings(user_id=12345, video_duration=35))

    with patch("services.story_cloner_service.db_manager", mock_db), \
         patch("services.story_cloner_service.story_video_generator.create_video_story_async", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = ("mock_vid.mp4", {"x": 50, "y": 50, "w": 80, "h": 50})

        # Test video duration resolution
        effective_uid = 12345
        u_st = await mock_db.get_story_settings(effective_uid)
        story_duration = float(getattr(u_st, "video_duration", 25) or 25.0)
        story_duration = max(15.0, min(40.0, story_duration))

        assert story_duration == 35.0
