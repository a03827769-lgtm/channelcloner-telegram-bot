import os
import sys
import glob

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.story_video_generator import story_video_generator
from services.listing_analyzer import listing_analyzer

def main():
    photos = sorted(glob.glob("temp_media/test_photos_real/p*.jpg"))
    if not photos:
        print("No test photos found in temp_media/test_photos_real")
        return

    sample_text = """
    Mirobod tumani, Oybek metrosi yaqinida
    3 xonali hashamatli yangi kvartira beriladi!
    Maydoni 110 kv.m, 6/14 etaj.
    Mualliflik dizayni asosida to'liq jihozlangan.
    Narxi: 1800$ / oy
    Aloqa uchun: @realtor_abdulloh
    """

    meta = listing_analyzer.analyze(sample_text, photo_count=len(photos), existing_price=1800.0)
    print(f"Detected District: {meta.district}")
    print(f"Detected Rooms: {meta.rooms}")
    print(f"Detected Area: {meta.area} m2")
    print(f"Quality Score: {meta.quality_score}/100")
    print(f"Smart Badges: {meta.smart_badges}")

    print("Generating 25-second luxury video story with smart badges...")
    video_path, coords = story_video_generator.create_video_story(
        photo_paths=photos,
        channel_title="Realtor Abdulloh | Toshkent",
        caption=sample_text,
        price=1800.0,
        date_str="17 сен, 19:30",
        avatar_path="temp_media/test_photos_real/avatar.jpg" if os.path.exists("temp_media/test_photos_real/avatar.jpg") else None,
        forward_title="Realtor Abdulloh",
        badges=meta.smart_badges,
        duration=25.0
    )

    print(f"Generated Video: {video_path}")
    print(f"Card Coordinates: {coords}")
    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    print(f"Video File Size: {size_mb:.2f} MB")
    assert os.path.exists(video_path), "Video file must exist"
    assert size_mb > 0.5, "Video file must be non-empty"
    print("SUCCESS: 25-second luxury video with badges generated perfectly!")

if __name__ == "__main__":
    main()
