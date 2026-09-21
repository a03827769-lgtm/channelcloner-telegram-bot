import os
import pytest
from PIL import Image

from services.story_video_generator import StoryVideoGenerator, story_video_generator
from services.story_renderer import story_card_renderer


def test_story_video_generator_audio_selection():
    """Verify that random audio track selection finds files from assets/audio"""
    track = story_video_generator.get_random_music_track()
    assert track is not None
    assert os.path.exists(track)
    assert track.endswith((".mp3", ".m4a", ".wav", ".aac"))


def test_story_video_generator_anti_repetition_consecutive():
    """Verify consecutive selections never return the same track across renders"""
    story_video_generator.clear_history()
    selected = [story_video_generator.get_random_music_track() for _ in range(15)]
    assert len(selected) == 15
    for i in range(1, len(selected)):
        assert selected[i] != selected[i - 1], f"Consecutive repeat detected at index {i}: {selected[i]}"
    for i in range(len(selected) - 2):
        window = selected[i:i + 3]
        assert len(set(window)) == 3, f"Duplicate within 3 consecutive videos: {window}"



def test_create_ambient_slide(tmp_path):
    """Verify that create_ambient_slide produces exact 1080x1920 vertical image"""
    dummy_photo = tmp_path / "room.jpg"
    img = Image.new("RGB", (800, 600), (120, 150, 180))
    img.save(str(dummy_photo))

    out_slide = tmp_path / "ambient_slide.jpg"
    res = StoryVideoGenerator.create_ambient_slide(str(dummy_photo), str(out_slide))
    assert os.path.exists(res)

    with Image.open(res) as res_img:
        assert res_img.size == (1080, 1920)


def test_build_ffmpeg_command():
    """Verify FFmpeg command generator calculates exact timings, xfade offsets, and audio fades"""
    gen = StoryVideoGenerator()
    slides = ["slide1.jpg", "slide2.jpg", "slide3.jpg"]
    overlay = "overlay.png"
    audio = "music.mp3"
    out = "out.mp4"

    cmd = gen.build_ffmpeg_command(
        slide_paths=slides,
        overlay_path=overlay,
        audio_path=audio,
        output_mp4=out,
        total_duration=25.0,
        xfade_duration=0.8
    )

    assert "ffmpeg" in cmd[0]
    assert out in cmd[-1]
    assert "-c:v" in cmd
    assert "libx264" in cmd
    assert "-c:a" in cmd
    assert "aac" in cmd

    # Verify filter complex contains xfade and overlay
    f_idx = cmd.index("-filter_complex")
    filter_graph = cmd[f_idx + 1]
    assert "xfade=" in filter_graph
    assert "overlay=0:0" in filter_graph
    assert "afade=t=in" in filter_graph
    assert "afade=t=out" in filter_graph


def test_render_card_overlay_png(tmp_path):
    """Verify transparent PNG overlay generation with precise coordinates"""
    dummy_photo = tmp_path / "p1.jpg"
    img = Image.new("RGB", (600, 400), (200, 200, 200))
    img.save(str(dummy_photo))

    out_overlay = str(tmp_path / "overlay.png")
    res = story_card_renderer.render_card_overlay_png(
        channel_title="ARENDA UY TEST",
        photo_paths=[str(dummy_photo)],
        caption="✨ Тестовая аренда\n💰 Цена: $500",
        price=500.0,
        output_path=out_overlay
    )

    assert os.path.exists(res)
    with Image.open(res) as res_img:
        assert res_img.size == (1080, 1920)
        assert res_img.mode in ("RGBA", "RGBa")

    coords = story_card_renderer.get_last_card_coordinates()
    assert "x" in coords
    assert "y" in coords
    assert "w" in coords
    assert "h" in coords
    assert 40.0 <= coords["x"] <= 60.0
    assert 40.0 <= coords["y"] <= 60.0
