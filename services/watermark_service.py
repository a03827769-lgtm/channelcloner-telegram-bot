import os
import logging
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

logger = logging.getLogger(__name__)

class WatermarkService:
    @staticmethod
    def apply_text_watermark(
        image_path: str,
        text: str,
        position: str = "bottom_right",
        output_path: Optional[str] = None
    ) -> Optional[str]:
        """
        Applies a modern, semi-transparent text watermark badge onto an image.
        """
        if not text or not os.path.exists(image_path):
            return image_path

        out_path = output_path or image_path

        try:
            with Image.open(image_path) as base_img:
                # Convert to RGBA for alpha compositing
                base_img = base_img.convert("RGBA")
                width, height = base_img.size

                # Dynamic font size relative to image width (2.5% to 4%)
                font_size = max(18, int(width * 0.035))
                font = None
                font_candidates = [
                    "arial.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
                    "DejaVuSans.ttf"
                ]
                for font_name in font_candidates:
                    try:
                        font = ImageFont.truetype(font_name, font_size)
                        break
                    except Exception:
                        continue
                if font is None:
                    font = ImageFont.load_default()

                # Create overlay canvas
                overlay = Image.new("RGBA", (width, height), (255, 255, 255, 0))
                draw = ImageDraw.Draw(overlay)

                # Measure text size
                bbox = draw.textbbox((0, 0), text, font=font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]

                padding = int(font_size * 0.5)
                box_w = text_w + padding * 2
                box_h = text_h + padding * 2

                margin = int(width * 0.03)

                # Calculate coordinates
                if position == "bottom_right":
                    x = width - box_w - margin
                    y = height - box_h - margin
                elif position == "bottom_left":
                    x = margin
                    y = height - box_h - margin
                elif position == "top_right":
                    x = width - box_w - margin
                    y = margin
                elif position == "top_left":
                    x = margin
                    y = margin
                elif position == "center":
                    x = (width - box_w) // 2
                    y = (height - box_h) // 2
                else:
                    x = width - box_w - margin
                    y = height - box_h - margin

                # Draw modern semi-transparent dark rounded badge
                badge_bg = (0, 0, 0, 160)
                badge_radius = max(6, int(box_h * 0.3))
                draw.rounded_rectangle(
                    [x, y, x + box_w, y + box_h],
                    radius=badge_radius,
                    fill=badge_bg
                )

                # Draw white text
                text_x = x + padding
                text_y = y + padding - (bbox[1] if bbox[1] < 0 else 0)
                draw.text((text_x, text_y), text, font=font, fill=(255, 255, 255, 240))

                # Composite and save
                watermarked = Image.alpha_composite(base_img, overlay)
                watermarked = watermarked.convert("RGB")
                watermarked.save(out_path, format="JPEG", quality=92)
                return out_path

        except Exception as e:
            logger.error(f"Error applying text watermark: {e}", exc_info=True)
            return image_path

    @staticmethod
    def apply_logo_watermark(
        image_path: str,
        logo_path: str,
        position: str = "bottom_right",
        opacity: float = 0.85,
        output_path: Optional[str] = None
    ) -> Optional[str]:
        """
        Applies a PNG logo onto an image with alpha transparency.
        """
        if not os.path.exists(image_path) or not os.path.exists(logo_path):
            return image_path

        out_path = output_path or image_path

        try:
            with Image.open(image_path) as base_img, Image.open(logo_path) as logo:
                base_img = base_img.convert("RGBA")
                logo = logo.convert("RGBA")
                width, height = base_img.size

                # Resize logo to 15% of base image width
                target_logo_w = int(width * 0.15)
                aspect = logo.size[1] / logo.size[0]
                target_logo_h = int(target_logo_w * aspect)
                logo = logo.resize((target_logo_w, target_logo_h), Image.Resampling.LANCZOS)

                # Adjust opacity
                if opacity < 1.0:
                    r, g, b, a = logo.split()
                    a = ImageEnhance.Brightness(a).enhance(opacity)
                    logo.putalpha(a)

                margin = int(width * 0.03)

                if position == "bottom_right":
                    x = width - target_logo_w - margin
                    y = height - target_logo_h - margin
                elif position == "bottom_left":
                    x = margin
                    y = height - target_logo_h - margin
                elif position == "top_right":
                    x = width - target_logo_w - margin
                    y = margin
                elif position == "top_left":
                    x = margin
                    y = margin
                else:
                    x = width - target_logo_w - margin
                    y = height - target_logo_h - margin

                overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                overlay.paste(logo, (x, y), logo)

                watermarked = Image.alpha_composite(base_img, overlay)
                watermarked = watermarked.convert("RGB")
                watermarked.save(out_path, format="JPEG", quality=92)
                return out_path

        except Exception as e:
            logger.error(f"Error applying logo watermark: {e}", exc_info=True)
            return image_path

watermark_service = WatermarkService()
