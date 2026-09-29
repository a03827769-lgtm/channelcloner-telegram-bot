import os
import pytest
import numpy as np
from PIL import Image

from services.aesthetic_scorer import aesthetic_scorer
from services.steganography_service import steganography_service
from services.tts_narrator_service import tts_narrator_service
from services.story_video_generator import story_video_generator
from database.db_manager import db_manager


def test_aesthetic_scorer_scoring_and_reordering(tmp_path):
    # 1. Create a flat, blurry image (low contrast, low colorfulness, low variance)
    blurry_img = Image.new("RGB", (300, 300), color=(128, 128, 128))
    blurry_path = str(tmp_path / "blurry.jpg")
    blurry_img.save(blurry_path)

    # 2. Create a sharp, vibrant, high-contrast image (colored blocks with high gradients)
    arr = np.zeros((300, 300, 3), dtype=np.uint8)
    arr[:150, :150] = [255, 0, 0]     # Red
    arr[150:, :150] = [0, 255, 0]     # Green
    arr[:150, 150:] = [0, 0, 255]     # Blue
    arr[150:, 150:] = [255, 255, 0]   # Yellow
    sharp_img = Image.fromarray(arr)
    sharp_path = str(tmp_path / "sharp.jpg")
    sharp_img.save(sharp_path)

    score_blurry = aesthetic_scorer.score_image(blurry_path)
    score_sharp = aesthetic_scorer.score_image(sharp_path)

    assert score_sharp > score_blurry

    # Reorder test: blurry first in input list, sharp second
    input_list = [blurry_path, sharp_path]
    reordered = aesthetic_scorer.reorder_photos_by_aesthetic(input_list)
    # sharp image must be promoted to Slide 1 (index 0)
    assert reordered[0] == sharp_path
    assert reordered[1] == blurry_path


def test_steganography_embed_and_extract(tmp_path):
    # Create textured test photo
    arr = np.zeros((240, 240, 3), dtype=np.uint8)
    for y in range(240):
        for x in range(240):
            arr[y, x] = [(x * 2) % 256, (y * 2) % 256, ((x + y)) % 256]
    orig_path = str(tmp_path / "orig.png")
    Image.fromarray(arr).save(orig_path)

    watermarked_path = str(tmp_path / "watermarked.png")
    payload = "UID_9988_CH_uy_bozori_TS_1773829100"

    success = steganography_service.embed_watermark(orig_path, watermarked_path, payload)
    assert success is True
    assert os.path.exists(watermarked_path)

    # Extract watermark
    extracted = steganography_service.extract_watermark(watermarked_path)
    assert extracted == payload

    # Unwatermarked image should return None
    no_watermark = steganography_service.extract_watermark(orig_path)
    assert no_watermark is None


def test_tts_narrator_teaser_script_generation():
    caption = """
    Yangi kvartira sotiladi!
    Chilonzor 9-mavzeda 3 xonali shinam xonadon.
    Ta'mirlangan, mebellari bilan birga.
    Narxi: $65 000 dollar.
    Murojaat uchun: @realtor_uz
    """
    script = tts_narrator_service.generate_teaser_script(caption, price=65000.0)
    assert "3 xonali" in script
    assert "Chilonzor" in script
    assert "65 ming dollar" in script
    assert "To'liq ma'lumot kanalimizda mavjud" in script


def test_story_video_generator_ffmpeg_audio_ducking(tmp_path):
    slides = [str(tmp_path / "s1.jpg"), str(tmp_path / "s2.jpg")]
    for s in slides:
        Image.new("RGB", (100, 100)).save(s)
    overlay = str(tmp_path / "ov.png")
    Image.new("RGBA", (100, 100)).save(overlay)
    audio = str(tmp_path / "bg.mp3")
    with open(audio, "w") as f:
        f.write("dummy audio")
    voice = str(tmp_path / "voice.mp3")
    with open(voice, "w") as f:
        f.write("dummy voice")

    out_mp4 = str(tmp_path / "out.mp4")

    cmd = story_video_generator.build_ffmpeg_command(
        slide_paths=slides,
        overlay_path=overlay,
        audio_path=audio,
        output_mp4=out_mp4,
        total_duration=25.0,
        voiceover_path=voice
    )

    cmd_str = " ".join(cmd)
    # Check that voiceover is included as an input
    assert voice in cmd
    # Check that sidechaincompress and amix audio ducking are in filter_complex
    assert "sidechaincompress" in cmd_str
    assert "asplit=2" in cmd_str
    assert "amix=inputs=2" in cmd_str


@pytest.mark.asyncio
async def test_story_settings_enable_ai_voice_db(tmp_path):
    orig_path = db_manager.db_path
    # Close the shared connection first, otherwise it keeps serving the previous database file
    await db_manager.close()
    try:
        test_db = str(tmp_path / "test_voice_settings.db")
        db_manager.db_path = test_db
        await db_manager.init_db()

        user_id = 771122

        # Verify default has enable_ai_voice = True
        st = await db_manager.get_story_settings(user_id)
        assert st.enable_ai_voice is True

        # Disable AI voice and save
        st.enable_ai_voice = False
        await db_manager.save_story_settings(st)

        # Reload from DB
        loaded = await db_manager.get_story_settings(user_id)
        assert loaded.enable_ai_voice is False

        # Toggle back on
        await db_manager.update_story_settings(user_id, enable_ai_voice=True)
        reloaded = await db_manager.get_story_settings(user_id)
        assert reloaded.enable_ai_voice is True
    finally:
        await db_manager.close()
        db_manager.db_path = orig_path
        await db_manager.init_db()
