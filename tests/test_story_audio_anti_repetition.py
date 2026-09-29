import os
import glob
import pytest
import threading
from unittest.mock import patch, MagicMock, AsyncMock

from services.story_video_generator import StoryVideoGenerator, story_video_generator
from database.db_manager import DatabaseManager


def test_audio_library_tracks_count_and_integrity():
    """Verify that assets/audio contains at least 20 premium tracks with valid file sizes"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    audio_dir = os.path.join(base_dir, "assets", "audio")
    assert os.path.exists(audio_dir), "assets/audio directory must exist"

    mp3_files = glob.glob(os.path.join(audio_dir, "*.mp3"))
    assert len(mp3_files) >= 20, f"Expected at least 20 audio tracks, found {len(mp3_files)}"

    for f in mp3_files:
        size = os.path.getsize(f)
        # Each 25-second 192kbps MP3 must be at least 400KB
        assert size > 400_000, f"Track {os.path.basename(f)} size {size} is too small (corrupted/incomplete)"
        assert os.path.basename(f).endswith(".mp3")


def test_consecutive_music_selection_never_repeats():
    """Verify that across consecutive video renders, tracks never repeat consecutively or within a window of 3"""
    story_video_generator.clear_history()
    selections = [story_video_generator.get_random_music_track() for _ in range(50)]

    assert len(selections) == 50
    assert all(s is not None for s in selections)

    # 1. Consecutive 2 videos NEVER use the same music
    for i in range(1, len(selections)):
        assert selections[i] != selections[i - 1], (
            f"Consecutive repetition violation at video #{i}: {selections[i]} == {selections[i - 1]}"
        )

    # 2. Consecutive 3 videos NEVER use the same music
    for i in range(len(selections) - 2):
        window = selections[i:i + 3]
        assert len(set(window)) == 3, f"Window of 3 videos contains duplicate: {window}"

    # 3. With 20 tracks, window of 5 must also be strictly unique
    for i in range(len(selections) - 4):
        window = selections[i:i + 5]
        assert len(set(window)) == 5, f"Window of 5 videos contains duplicate: {window}"


def test_anti_repetition_small_pool_three_tracks(tmp_path):
    """Verify anti-repetition engine works flawlessly with a small pool of 3 tracks"""
    for name in ("track1.mp3", "track2.mp3", "track3.mp3"):
        (tmp_path / name).write_bytes(b"x" * 1000)

    gen = StoryVideoGenerator(audio_dir=str(tmp_path), max_history=2)
    selected = [os.path.basename(gen.get_random_music_track()) for _ in range(12)]

    # Never repeat consecutive
    for i in range(1, len(selected)):
        assert selected[i] != selected[i - 1], f"Repeat at {i}: {selected[i]}"

    # Never repeat within 3 consecutive renders
    for i in range(len(selected) - 2):
        window = selected[i:i + 3]
        assert len(set(window)) == 3, f"Duplicate within 3 renders: {window}"


def test_anti_repetition_small_pool_two_tracks(tmp_path):
    """Verify anti-repetition engine strictly alternates when only 2 tracks exist"""
    for name in ("t_a.mp3", "t_b.mp3"):
        (tmp_path / name).write_bytes(b"x" * 1000)

    gen = StoryVideoGenerator(audio_dir=str(tmp_path), max_history=1)
    selected = [os.path.basename(gen.get_random_music_track()) for _ in range(10)]

    for i in range(1, len(selected)):
        assert selected[i] != selected[i - 1], f"Repeat at {i}: {selected[i]}"


def test_anti_repetition_single_track(tmp_path):
    """Verify single track in pool returns safely without infinite loop or error"""
    (tmp_path / "only_one.mp3").write_bytes(b"x" * 1000)
    gen = StoryVideoGenerator(audio_dir=str(tmp_path))
    res = gen.get_random_music_track()
    assert os.path.basename(res) == "only_one.mp3"


def test_anti_repetition_empty_pool(tmp_path):
    """Verify empty directory returns None cleanly without exceptions"""
    gen = StoryVideoGenerator(audio_dir=str(tmp_path))
    assert gen.get_random_music_track() is None


def test_user_specific_anti_repetition():
    """Verify per-user anti-repetition tracking when user_id is passed"""
    story_video_generator.clear_history()

    user_a = 1001
    user_b = 1002

    a_tracks = []
    b_tracks = []

    # Interleave video generations for user A and user B
    for _ in range(10):
        a_tracks.append(story_video_generator.get_random_music_track(user_id=user_a))
        b_tracks.append(story_video_generator.get_random_music_track(user_id=user_b))

    for i in range(1, len(a_tracks)):
        assert a_tracks[i] != a_tracks[i - 1], f"User A consecutive repeat at {i}: {a_tracks[i]}"
    for i in range(1, len(b_tracks)):
        assert b_tracks[i] != b_tracks[i - 1], f"User B consecutive repeat at {i}: {b_tracks[i]}"


def test_explicit_audio_path_recording():
    """Verify explicitly provided audio_path is recorded in history and avoided next turn"""
    story_video_generator.clear_history()
    tracks = story_video_generator.get_available_tracks()
    assert len(tracks) >= 5

    chosen_manually = tracks[0]
    story_video_generator.record_track_usage(chosen_manually)

    # Next automated call should not choose chosen_manually
    next_auto = story_video_generator.get_random_music_track()
    assert next_auto != chosen_manually
    assert os.path.basename(next_auto) != os.path.basename(chosen_manually)


@pytest.mark.asyncio
async def test_db_manager_persistent_story_music(tmp_path):
    """Verify DatabaseManager saves and retrieves persistent story music history in app_settings"""
    test_db_path = str(tmp_path / "test_music.db")
    db = DatabaseManager(db_path=test_db_path)
    await db.init_db()

    try:
        # Initially empty
        recent0 = await db.get_recent_story_music()
        assert recent0 == []

        # Record tracks
        await db.record_used_story_music("01_luxury_corporate.mp3")
        await db.record_used_story_music("02_deep_lounge.mp3")
        await db.record_used_story_music("03_ambient_piano.mp3")

        recent = await db.get_recent_story_music(limit=5)
        assert recent == ["01_luxury_corporate.mp3", "02_deep_lounge.mp3", "03_ambient_piano.mp3"]

        # Re-recording existing track moves it to latest position
        await db.record_used_story_music("01_luxury_corporate.mp3")
        recent2 = await db.get_recent_story_music(limit=5)
        assert recent2 == ["02_deep_lounge.mp3", "03_ambient_piano.mp3", "01_luxury_corporate.mp3"]
    finally:
        await db.close()


def test_thread_safety_concurrent_audio_selection():
    """Verify thread safety when multiple worker threads select music simultaneously"""
    story_video_generator.clear_history()
    results = []
    errors = []

    def worker():
        try:
            for _ in range(25):
                t = story_video_generator.get_random_music_track()
                results.append(t)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"Threading errors occurred: {errors}"
    assert len(results) == 200
    assert all(r is not None for r in results)


def test_create_video_story_anti_repetition_integration(tmp_path):
    """Verify create_video_story selects and records distinct tracks across consecutive calls"""
    story_video_generator.clear_history()
    dummy_photo = tmp_path / "p.jpg"
    dummy_photo.write_bytes(b"dummy")

    selected_audios = []

    def mock_build_ffmpeg(slide_paths, overlay_path, audio_path, output_mp4, **kwargs):
        selected_audios.append(audio_path)
        return ["ffmpeg", "-version"]

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0

    with patch.object(story_video_generator, "create_ambient_slide", return_value=str(dummy_photo)), \
         patch("services.story_renderer.story_card_renderer.render_card_overlay_png", return_value="overlay.png"), \
         patch.object(story_video_generator, "build_ffmpeg_command", side_effect=mock_build_ffmpeg), \
         patch("subprocess.run", mock_run):

        for i in range(6):
            out_file = str(tmp_path / f"out_{i}.mp4")
            with open(out_file, "wb") as f:
                f.write(b"mp4data")
            story_video_generator.create_video_story(
                photo_paths=[str(dummy_photo)],
                channel_title="Test Luxury",
                caption="Luxury Villa",
                output_path=out_file
            )

    assert len(selected_audios) == 6
    for i in range(1, len(selected_audios)):
        assert selected_audios[i] != selected_audios[i - 1], f"Repeat at render #{i}"
    for i in range(len(selected_audios) - 2):
        window = selected_audios[i:i + 3]
        assert len(set(window)) == 3, f"Duplicate within 3 renders: {window}"


@pytest.mark.asyncio
async def test_create_video_story_async_anti_repetition_integration(tmp_path):
    """Verify create_video_story_async selects and records distinct tracks across consecutive calls"""
    story_video_generator.clear_history()
    dummy_photo = tmp_path / "p_async.jpg"
    dummy_photo.write_bytes(b"dummy")

    selected_audios = []

    def mock_build_ffmpeg(slide_paths, overlay_path, audio_path, output_mp4, **kwargs):
        selected_audios.append(audio_path)
        return ["ffmpeg", "-version"]

    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0

    with patch.object(story_video_generator, "create_ambient_slide", return_value=str(dummy_photo)), \
         patch("services.story_renderer.story_card_renderer.render_card_overlay_png", return_value="overlay.png"), \
         patch.object(story_video_generator, "build_ffmpeg_command", side_effect=mock_build_ffmpeg), \
         patch("asyncio.create_subprocess_exec", return_value=mock_proc):

        for i in range(6):
            out_file = str(tmp_path / f"out_async_{i}.mp4")
            with open(out_file, "wb") as f:
                f.write(b"mp4data")
            await story_video_generator.create_video_story_async(
                photo_paths=[str(dummy_photo)],
                channel_title="Test Luxury Async",
                caption="Luxury Penthouse",
                output_path=out_file,
                user_id=777
            )

    assert len(selected_audios) == 6
    for i in range(1, len(selected_audios)):
        assert selected_audios[i] != selected_audios[i - 1], f"Repeat at async render #{i}"
    for i in range(len(selected_audios) - 2):
        window = selected_audios[i:i + 3]
        assert len(set(window)) == 3, f"Duplicate within 3 async renders: {window}"
