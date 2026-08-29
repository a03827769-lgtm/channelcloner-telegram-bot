import os
import asyncio
import logging
import shutil
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

class VideoWatermarkService:
    """
    High-performance async FFmpeg video watermarking engine.
    Supports text watermark with drawtext and logo watermark with overlay.
    """

    def __init__(self):
        self.ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"

    def _get_drawtext_coordinates(self, pos: str) -> tuple[str, str]:
        """Returns FFmpeg x, y expressions for position"""
        padding = 30
        if pos == "top_left":
            return str(padding), str(padding)
        elif pos == "top_right":
            return f"w-tw-{padding}", str(padding)
        elif pos == "bottom_left":
            return str(padding), f"h-th-{padding}"
        elif pos == "center":
            return "(w-tw)/2", "(h-th)/2"
        else:  # bottom_right default
            return f"w-tw-{padding}", f"h-th-{padding}"

    def _get_overlay_coordinates(self, pos: str) -> tuple[str, str]:
        """Returns FFmpeg overlay coordinates for logo"""
        padding = 30
        if pos == "top_left":
            return str(padding), str(padding)
        elif pos == "top_right":
            return f"main_w-overlay_w-{padding}", str(padding)
        elif pos == "bottom_left":
            return str(padding), f"main_h-overlay_h-{padding}"
        elif pos == "center":
            return "(main_w-overlay_w)/2", "(main_h-overlay_h)/2"
        else:  # bottom_right default
            return f"main_w-overlay_w-{padding}", f"main_h-overlay_h-{padding}"

    async def apply_video_text_watermark(
        self,
        input_video_path: str,
        watermark_text: str,
        pos: str = "bottom_right",
        font_size: int = 24,
        output_dir: str = "temp_media"
    ) -> Optional[str]:
        """
        Applies a clean text watermark onto a video file using async FFmpeg.
        Returns the path to the watermarked video, or None if failed.
        """
        if not os.path.exists(input_video_path) or not watermark_text.strip():
            return None

        os.makedirs(output_dir, exist_ok=True)
        filename = f"wm_vid_{os.path.basename(input_video_path)}"
        output_path = os.path.join(output_dir, filename)

        # Sanitize text for FFmpeg drawtext filter
        safe_text = watermark_text.replace("'", "").replace(":", "\\:").replace("%", "\\%")
        x, y = self._get_drawtext_coordinates(pos)

        # Drawtext filter with shadow for high legibility on any background
        vf_filter = (
            f"drawtext=text='{safe_text}':fontcolor=white@0.85:fontsize={font_size}:"
            f"x={x}:y={y}:shadowcolor=black@0.7:shadowx=2:shadowy=2"
        )

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", input_video_path,
            "-vf", vf_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "copy",
            output_path
        ]

        try:
            logger.info(f"Applying video text watermark '{watermark_text}' to {input_video_path}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return output_path
            else:
                logger.warning(f"FFmpeg failed with returncode {proc.returncode}: {stderr.decode('utf-8', errors='ignore')[:300]}")
                return None
        except Exception as e:
            logger.error(f"Error executing video watermark: {e}")
            return None

    async def apply_video_logo_watermark(
        self,
        input_video_path: str,
        logo_image_path: str,
        pos: str = "bottom_right",
        scale_percent: int = 15,
        output_dir: str = "temp_media"
    ) -> Optional[str]:
        """
        Overlays a transparent PNG logo onto a video file using async FFmpeg.
        """
        if not os.path.exists(input_video_path) or not os.path.exists(logo_image_path):
            return None

        os.makedirs(output_dir, exist_ok=True)
        filename = f"wmlogo_vid_{os.path.basename(input_video_path)}"
        output_path = os.path.join(output_dir, filename)

        x, y = self._get_overlay_coordinates(pos)
        filter_complex = f"[1:v]scale=iw*{scale_percent}/100:-1[logo];[0:v][logo]overlay={x}:{y}"

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", input_video_path,
            "-i", logo_image_path,
            "-filter_complex", filter_complex,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "copy",
            output_path
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return output_path
            return None
        except Exception as e:
            logger.error(f"Error executing logo watermark: {e}")
            return None

video_watermark_service = VideoWatermarkService()
