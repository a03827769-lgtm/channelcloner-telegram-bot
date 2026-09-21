import os
import logging
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageOps

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

        if image_path.lower().endswith(".gif"):
            return image_path

        # Guard against excessively long watermark text
        clean_text = str(text or "").strip()
        if len(clean_text) > 100:
            clean_text = clean_text[:97] + "..."

        out_path = output_path or image_path
        tmp_save_path = f"{out_path}.wm_tmp"

        try:
            with Image.open(image_path) as loaded_img:
                if loaded_img.mode in ("P", "PA"):
                    loaded_img = loaded_img.convert("RGBA")
                transposed = ImageOps.exif_transpose(loaded_img)
                base_img = transposed.convert("RGBA").copy()
            width, height = base_img.size
            if width < 20 or height < 20:
                logger.warning(f"Image too small to watermark: {base_img.size}")
                return image_path

            # Dynamic font size relative to image width (2.5% to 4%)
            font_size = max(18, int(width * 0.035))
            font = None
            font_candidates = [
                "arial.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/calibri.ttf",
                "C:/Windows/Fonts/seguisb.ttf",
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
                try:
                    font = ImageFont.load_default(size=font_size)
                except TypeError:
                    font = ImageFont.load_default()

            # Create overlay canvas
            overlay = Image.new("RGBA", (width, height), (255, 255, 255, 0))
            draw = ImageDraw.Draw(overlay)

            # Measure text size
            bbox = draw.textbbox((0, 0), clean_text, font=font)
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

            x = max(0, x)
            y = max(0, y)

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
            draw.text((text_x, text_y), clean_text, font=font, fill=(255, 255, 255, 240))

            # Composite and save safely to avoid Windows file locks
            watermarked = Image.alpha_composite(base_img, overlay)
            if out_path.lower().endswith((".png", ".webp")):
                watermarked.save(tmp_save_path, format="PNG")
            else:
                if watermarked.mode in ("RGBA", "LA") or (watermarked.mode == "P" and "transparency" in watermarked.info):
                    bg = Image.new("RGB", watermarked.size, (255, 255, 255))
                    bg.paste(watermarked, mask=watermarked.split()[3])
                    watermarked_rgb = bg
                else:
                    watermarked_rgb = watermarked.convert("RGB")
                watermarked_rgb.save(tmp_save_path, format="JPEG", quality=92)

            if os.path.exists(tmp_save_path):
                os.replace(tmp_save_path, out_path)
            return out_path

        except Exception as e:
            logger.error(f"Error applying text watermark: {e}", exc_info=True)
            return image_path
        finally:
            if os.path.exists(tmp_save_path):
                try:
                    os.remove(tmp_save_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

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

        if image_path.lower().endswith(".gif"):
            return image_path

        out_path = output_path or image_path

        try:
            with Image.open(image_path) as loaded_img, Image.open(logo_path) as loaded_logo:
                if loaded_img.mode in ("P", "PA"):
                    loaded_img = loaded_img.convert("RGBA")
                if loaded_logo.mode in ("P", "PA"):
                    loaded_logo = loaded_logo.convert("RGBA")
                base_img = ImageOps.exif_transpose(loaded_img).convert("RGBA").copy()
                logo = ImageOps.exif_transpose(loaded_logo).convert("RGBA").copy()
            width, height = base_img.size
            if width < 20 or height < 20 or logo.size[0] <= 0 or logo.size[1] <= 0:
                logger.warning(f"Image or logo too small to watermark: img={base_img.size}, logo={logo.size}")
                return image_path

            # Resize logo to 15-18% of base image width with proportional bounds
            target_logo_w = max(10, min(int(width * 0.18), max(10, width - 10)))
            aspect = logo.size[1] / max(1, logo.size[0])
            target_logo_h = max(10, int(target_logo_w * aspect))
            if target_logo_h > height - 10:
                target_logo_h = max(10, height - 10)
                target_logo_w = max(10, int(target_logo_h / max(0.01, aspect)))

            logo = logo.resize((target_logo_w, target_logo_h), Image.Resampling.LANCZOS)

            # Adjust opacity
            if opacity < 1.0:
                r, g, b, a = logo.split()
                a = a.point(lambda p: int(p * opacity))
                logo = Image.merge("RGBA", (r, g, b, a))

            # Position coordinates with proportional margin
            margin = max(5, min(20, int(width * 0.03)))
            if position == "top_left":
                pos = (margin, margin)
            elif position == "top_right":
                pos = (width - target_logo_w - margin, margin)
            elif position == "bottom_left":
                pos = (margin, height - target_logo_h - margin)
            elif position == "center":
                pos = ((width - target_logo_w) // 2, (height - target_logo_h) // 2)
            else:
                pos = (width - target_logo_w - margin, height - target_logo_h - margin)

            pos = (max(0, pos[0]), max(0, pos[1]))

            # Paste with alpha mask
            base_img.paste(logo, pos, mask=logo)

            tmp_save_path = f"{out_path}.logo_tmp"
            if out_path.lower().endswith((".png", ".webp")):
                base_img.save(tmp_save_path, format="PNG")
            else:
                bg = Image.new("RGB", base_img.size, (255, 255, 255))
                bg.paste(base_img, mask=base_img.split()[3])
                bg.save(tmp_save_path, format="JPEG", quality=92)

            if os.path.exists(tmp_save_path):
                os.replace(tmp_save_path, out_path)
            return out_path

        except Exception as e:
            logger.error(f"Error applying logo watermark: {e}", exc_info=True)
            return image_path
        finally:
            if os.path.exists(tmp_save_path):
                try:
                    os.remove(tmp_save_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

    @staticmethod
    def apply_text_watermark_bytes(
        image_bytes: bytes,
        text: str = "",
        position: str = "bottom_right",
        watermark_text: Optional[str] = None,
        pos: Optional[str] = None
    ) -> bytes:
        """
        Applies a modern semi-transparent text watermark badge directly to image bytes in RAM.
        Zero disk I/O, optimized for ultra-fast (sub-50ms) throughput.
        """
        effective_text = watermark_text if watermark_text is not None else text
        effective_pos = pos if pos is not None else position
        if not effective_text or not image_bytes:
            return image_bytes
        import io
        clean_text = str(effective_text or "").strip()
        position = effective_pos
        if len(clean_text) > 100:
            clean_text = clean_text[:97] + "..."

        try:
            input_stream = io.BytesIO(image_bytes)
            with Image.open(input_stream) as loaded_img:
                if getattr(loaded_img, "format", "") == "GIF":
                    return image_bytes
                if loaded_img.mode in ("P", "PA"):
                    loaded_img = loaded_img.convert("RGBA")
                transposed = ImageOps.exif_transpose(loaded_img)
                base_img = transposed.convert("RGBA").copy()
                orig_format = getattr(loaded_img, "format", "JPEG") or "JPEG"

            width, height = base_img.size
            if width < 20 or height < 20:
                return image_bytes

            font_size = max(18, int(width * 0.035))
            font = None
            font_candidates = [
                "arial.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/calibri.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "DejaVuSans.ttf"
            ]
            for font_name in font_candidates:
                try:
                    font = ImageFont.truetype(font_name, font_size)
                    break
                except Exception:
                    continue
            if font is None:
                try:
                    font = ImageFont.load_default(size=font_size)
                except TypeError:
                    font = ImageFont.load_default()

            overlay = Image.new("RGBA", (width, height), (255, 255, 255, 0))
            draw = ImageDraw.Draw(overlay)
            bbox = draw.textbbox((0, 0), clean_text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            padding = int(font_size * 0.5)
            box_w = text_w + padding * 2
            box_h = text_h + padding * 2
            margin = int(width * 0.03)

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

            x = max(0, x)
            y = max(0, y)

            badge_bg = (0, 0, 0, 160)
            badge_radius = max(6, int(box_h * 0.3))
            draw.rounded_rectangle([x, y, x + box_w, y + box_h], radius=badge_radius, fill=badge_bg)
            text_x = x + padding
            text_y = y + padding - (bbox[1] if bbox[1] < 0 else 0)
            draw.text((text_x, text_y), clean_text, font=font, fill=(255, 255, 255, 240))

            watermarked = Image.alpha_composite(base_img, overlay)
            out_stream = io.BytesIO()
            if orig_format.upper() in ("PNG", "WEBP"):
                watermarked.save(out_stream, format="PNG")
            else:
                if watermarked.mode in ("RGBA", "LA") or (watermarked.mode == "P" and "transparency" in getattr(watermarked, "info", {})):
                    bg = Image.new("RGB", watermarked.size, (255, 255, 255))
                    bg.paste(watermarked, mask=watermarked.split()[3])
                    watermarked_rgb = bg
                else:
                    watermarked_rgb = watermarked.convert("RGB")
                watermarked_rgb.save(out_stream, format="JPEG", quality=92)

            return out_stream.getvalue()
        except Exception as e:
            logger.error(f"Error applying in-memory text watermark: {e}", exc_info=True)
            return image_bytes

watermark_service = WatermarkService()
