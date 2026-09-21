import os
import sys
import glob
import random
import subprocess
from PIL import Image, ImageFilter, ImageDraw

def prepare_slide_image(photo_path: str, out_slide_path: str):
    """
    Creates a luxury 1080x1920 vertical slide from any aspect ratio photo:
    - Background: blurred, darkened version of the photo covering full 1080x1920
    - Foreground: sharp original photo centered with subtle rounded corners and shadow
    """
    canvas_w, canvas_h = 1080, 1920
    orig = Image.open(photo_path).convert("RGB")
    orig_w, orig_h = orig.size

    # 1. Background: scale to cover canvas, blur and darken
    scale_bg = max(canvas_w / orig_w, canvas_h / orig_h)
    bg_w, bg_h = int(orig_w * scale_bg), int(orig_h * scale_bg)
    bg = orig.resize((bg_w, bg_h), Image.Resampling.LANCZOS)
    # Center crop to 1080x1920
    left = (bg_w - canvas_w) // 2
    top = (bg_h - canvas_h) // 2
    bg_cropped = bg.crop((left, top, left + canvas_w, top + canvas_h))
    # Gaussian blur & dim
    bg_blurred = bg_cropped.filter(ImageFilter.GaussianBlur(radius=28))
    # Darken slightly for elegant contrast
    dimmer = Image.new("RGB", (canvas_w, canvas_h), (20, 25, 22))
    canvas = Image.blend(bg_blurred, dimmer, alpha=0.35)

    # 2. Foreground: sharp image placed in the center (scale to fit max 940x1100)
    max_fg_w, max_fg_h = 960, 1120
    scale_fg = min(max_fg_w / orig_w, max_fg_h / orig_h, 1.0)
    fg_w, fg_h = int(orig_w * scale_fg), int(orig_h * scale_fg)
    fg = orig.resize((fg_w, fg_h), Image.Resampling.LANCZOS)

    # Rounded corners mask for foreground
    mask = Image.new("L", (fg_w, fg_h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), (fg_w, fg_h)], radius=20, fill=255)

    # Shadow behind foreground
    shadow = Image.new("RGBA", (fg_w + 40, fg_h + 40), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle([(20, 20), (fg_w + 20, fg_h + 20)], radius=20, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=14))

    # Paste shadow and foreground onto canvas
    fg_x = (canvas_w - fg_w) // 2
    fg_y = 280  # positioned behind where the card will sit or in the upper-middle area
    canvas.paste(shadow, (fg_x - 20, fg_y - 20), shadow)
    canvas.paste(fg, (fg_x, fg_y), mask)

    canvas.save(out_slide_path, quality=95)
    return out_slide_path

if __name__ == "__main__":
    os.makedirs("temp_media/test_slides", exist_ok=True)
    photos = sorted(glob.glob("temp_media/test_photos_real/p*.jpg"))
    print("Found photos:", len(photos))
    for i, p in enumerate(photos):
        out_p = f"temp_media/test_slides/slide_{i}.jpg"
        prepare_slide_image(p, out_p)
        print(f"Prepared slide {i} -> {out_p}")
