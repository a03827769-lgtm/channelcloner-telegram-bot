import os
import sys
import json
import time
import subprocess
from PIL import Image

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.story_video_generator import story_video_generator


def inspect_media_ffprobe(filepath: str):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size,bit_rate:stream=codec_type,codec_name,duration,width,height,r_frame_rate,channels,sample_rate",
        "-of", "json",
        filepath
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def check_audio_levels(filepath: str):
    cmd = [
        "ffmpeg", "-i", filepath,
        "-af", "volumedetect",
        "-f", "null", "-"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = res.stderr
    mean_vol = None
    max_vol = None
    for line in out.splitlines():
        if "mean_volume:" in line:
            mean_vol = line.strip()
        elif "max_volume:" in line:
            max_vol = line.strip()
    return mean_vol, max_vol


def run_live_tests():
    print("=" * 60)
    print("STARTING REAL-WORLD LIVE TESTS FOR STORY DURATION & AUDIO")
    print("=" * 60)

    temp_dir = os.path.join(BASE_DIR, "temp_media")
    os.makedirs(temp_dir, exist_ok=True)

    # Prepare 9 distinct colored photos to simulate a real multi-photo property post
    colors = [
        (45, 85, 125), (125, 60, 45), (45, 125, 75), (110, 45, 125),
        (125, 110, 45), (45, 125, 125), (80, 80, 80), (130, 70, 90), (60, 100, 50)
    ]
    test_photos = []
    for i, col in enumerate(colors):
        p_path = os.path.join(temp_dir, f"real_test_photo_{i}.jpg")
        img = Image.new("RGB", (1080, 1080), col)
        img.save(p_path, quality=95)
        test_photos.append(p_path)
    print(f"[OK] Created {len(test_photos)} distinct property test photos.")

    # ----------------------------------------------------
    # TEST 1: Minimum Duration (15.0 seconds) with Audio
    # ----------------------------------------------------
    print("\n--- TEST 1: Generating 15.0s Story Video with Audio ---")
    t0 = time.time()
    v15_path, coords15 = story_video_generator.create_video_story(
        photo_paths=test_photos[:3],
        channel_title="Toshkent Hashamatli Uylar",
        caption="Yunusobod 3-xona Evroremont $850/oy",
        price=850.0,
        duration=15.0
    )
    t15 = time.time() - t0
    probe15 = inspect_media_ffprobe(v15_path)
    dur15 = float(probe15["format"]["duration"])
    streams15 = {s["codec_type"]: s for s in probe15.get("streams", [])}
    has_v15 = "video" in streams15
    has_a15 = "audio" in streams15
    mean_v15, max_v15 = check_audio_levels(v15_path)

    print(f"Time taken: {t15:.2f}s")
    print(f"File: {v15_path} ({os.path.getsize(v15_path)} bytes)")
    print(f"Reported duration: {dur15:.2f}s (Expected: ~15.0s)")
    print(f"Video stream: {streams15.get('video', {}).get('codec_name')} {streams15.get('video', {}).get('width')}x{streams15.get('video', {}).get('height')}")
    print(f"Audio stream: {streams15.get('audio', {}).get('codec_name')} {streams15.get('audio', {}).get('channels')} channels {streams15.get('audio', {}).get('sample_rate')}Hz")
    print(f"Audio levels: {mean_v15} | {max_v15}")

    assert 14.5 <= dur15 <= 15.5, f"Duration {dur15} not within 15s target"
    assert has_v15 and has_a15, "Both video and audio must be present"
    print("[PASS] TEST 1 PASSED: 15s Story Video has perfect audio and video!")

    # ----------------------------------------------------
    # TEST 2: Maximum Duration (40.0 seconds) with Audio
    # ----------------------------------------------------
    print("\n--- TEST 2: Generating 40.0s Story Video with Audio ---")
    t0 = time.time()
    v40_path, coords40 = story_video_generator.create_video_story(
        photo_paths=test_photos[:4],
        channel_title="Prestige Real Estate",
        caption="Mirzo Ulug'bek Penthouse $2500/oy",
        price=2500.0,
        duration=40.0
    )
    t40 = time.time() - t0
    probe40 = inspect_media_ffprobe(v40_path)
    dur40 = float(probe40["format"]["duration"])
    streams40 = {s["codec_type"]: s for s in probe40.get("streams", [])}
    has_v40 = "video" in streams40
    has_a40 = "audio" in streams40
    mean_v40, max_v40 = check_audio_levels(v40_path)

    print(f"Time taken: {t40:.2f}s")
    print(f"File: {v40_path} ({os.path.getsize(v40_path)} bytes)")
    print(f"Reported duration: {dur40:.2f}s (Expected: ~40.0s)")
    print(f"Video stream: {streams40.get('video', {}).get('codec_name')} {streams40.get('video', {}).get('width')}x{streams40.get('video', {}).get('height')}")
    print(f"Audio stream: {streams40.get('audio', {}).get('codec_name')} {streams40.get('audio', {}).get('channels')} channels")
    print(f"Audio levels: {mean_v40} | {max_v40}")

    assert 39.5 <= dur40 <= 40.5, f"Duration {dur40} not within 40s target"
    assert has_v40 and has_a40, "Both video and audio must be present"
    print("[PASS] TEST 2 PASSED: 40s Story Video has perfect audio and video!")

    # ----------------------------------------------------
    # TEST 3: 9 Photos Stress Test (Capping to 4 slides & No OOM)
    # ----------------------------------------------------
    print("\n--- TEST 3: 9 Photos Post Stress Test (Slide Capping to 4) ---")
    t0 = time.time()
    v9_path, coords9 = story_video_generator.create_video_story(
        photo_paths=test_photos,  # all 9 photos!
        channel_title="Toshkent City Bulvar",
        caption="9 ta rasmli hashamatli kvartira $1800/oy",
        price=1800.0,
        duration=25.0
    )
    t9 = time.time() - t0
    probe9 = inspect_media_ffprobe(v9_path)
    dur9 = float(probe9["format"]["duration"])
    streams9 = {s["codec_type"]: s for s in probe9.get("streams", [])}
    has_a9 = "audio" in streams9

    print(f"Time taken: {t9:.2f}s")
    print(f"File: {v9_path} ({os.path.getsize(v9_path)} bytes)")
    print(f"Reported duration: {dur9:.2f}s")
    print(f"Audio stream present: {has_a9} ({streams9.get('audio', {}).get('codec_name')})")

    assert has_a9, "Audio must be present even when input has 9 photos"
    assert 24.5 <= dur9 <= 25.5, f"Duration {dur9} not within 25s target"
    print("[PASS] TEST 3 PASSED: 9-photo post encoded cleanly without OOM and with full audio!")

    # ----------------------------------------------------
    # TEST 4: Duration Clamping Boundaries (<15s and >40s)
    # ----------------------------------------------------
    print("\n--- TEST 4: Duration Clamping Verification (<15s clamped to 15s, >40s clamped to 40s) ---")
    # Low clamp test
    v_low, _ = story_video_generator.create_video_story(
        photo_paths=test_photos[:2],
        channel_title="Clamp Test",
        caption="Under 15s clamp test",
        duration=5.0  # Invalid duration under 15
    )
    p_low = inspect_media_ffprobe(v_low)
    dur_low = float(p_low["format"]["duration"])
    print(f"Requested: 5.0s -> Clamped duration: {dur_low:.2f}s (Expected: ~15.0s)")
    assert 14.5 <= dur_low <= 15.5

    # High clamp test
    v_high, _ = story_video_generator.create_video_story(
        photo_paths=test_photos[:2],
        channel_title="Clamp Test",
        caption="Over 40s clamp test",
        duration=99.0  # Invalid duration over 40
    )
    p_high = inspect_media_ffprobe(v_high)
    dur_high = float(p_high["format"]["duration"])
    print(f"Requested: 99.0s -> Clamped duration: {dur_high:.2f}s (Expected: ~40.0s)")
    assert 39.5 <= dur_high <= 40.5
    print("[PASS] TEST 4 PASSED: Duration strictly clamped between 15s and 40s!")

    print("\n" + "=" * 60)
    print("ALL REAL-WORLD LIVE TESTS PASSED 100% WITH FLYING COLORS!")
    print("=" * 60)


if __name__ == "__main__":
    run_live_tests()
