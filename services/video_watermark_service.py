import os
import asyncio
import logging
import shutil
import tempfile
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

class VideoWatermarkService:
    """
    High-performance async FFmpeg video watermarking engine.
    Supports text watermark with drawtext and logo watermark with overlay.
    """

    def __init__(self, max_concurrent: int = 2):
        self.ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
        self.max_concurrent = max_concurrent
        self._semaphore: Optional[asyncio.Semaphore] = None

    @property
    def semaphore(self) -> asyncio.Semaphore:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._semaphore is None or getattr(self, "_semaphore_loop", None) != current_loop:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
            self._semaphore_loop = current_loop
        return self._semaphore

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

    def _escape_drawtext(self, text: str) -> str:
        """Properly escapes special characters for FFmpeg drawtext filter"""
        t = text.replace("\r", "").replace("\n", " ")
        t = t.replace("\\", "\\\\")
        t = t.replace("'", "\\'")  # Escaped apostrophe for FFmpeg drawtext single-quoted string
        t = t.replace(":", "\\:").replace("%", "\\%")
        t = t.replace("[", "\\[").replace("]", "\\]").replace(";", "\\;").replace(",", "\\,")
        return t

    def _find_default_fontfile(self) -> Optional[str]:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",
            "C:\\Windows\\Fonts\\segoeui.ttf",
            "/System/Library/Fonts/Helvetica.ttc"
        ]
        for p in candidates:
            if os.path.exists(p):
                return p.replace("\\", "/")
        return None

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
        watermark_text = (watermark_text or "").strip()[:100]
        if not os.path.exists(input_video_path) or not watermark_text:
            return None

        os.makedirs(output_dir, exist_ok=True)
        filename = f"wm_vid_{uuid.uuid4().hex[:8]}.mp4"
        output_path = os.path.join(output_dir, filename)

        # Sanitize text for FFmpeg drawtext filter
        safe_text = self._escape_drawtext(watermark_text)
        x, y = self._get_drawtext_coordinates(pos)

        font_arg = ""
        font_path = self._find_default_fontfile()
        if font_path:
            clean_font_path = font_path.replace("\\", "/").replace(":", "\\:")
            font_arg = f":fontfile={clean_font_path}"

        # Drawtext filter with shadow for high legibility on any background
        vf_filter = (
            f"drawtext=text='{safe_text}'{font_arg}:fontcolor=white@0.85:fontsize={font_size}:"
            f"x={x}:y={y}:shadowcolor=black@0.7:shadowx=2:shadowy=2"
        )

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-loglevel", "error",
            "-threads", "2",
            "-i", input_video_path,
            "-vf", vf_filter,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "faster",
            "-crf", "24",
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path
        ]

        async with self.semaphore:
            try:
                logger.info(f"Applying video text watermark '{watermark_text}' to {input_video_path}")
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                    logger.error(f"FFmpeg process timed out (>120s) for {input_video_path}")
                    return None

                if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    return output_path
                else:
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                    logger.warning(f"FFmpeg failed with returncode {proc.returncode}: {stderr.decode('utf-8', errors='ignore')[:300]}")
                    return None
            except Exception as e:
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
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
        filename = f"wmlogo_vid_{uuid.uuid4().hex[:8]}.mp4"
        output_path = os.path.join(output_dir, filename)

        x, y = self._get_overlay_coordinates(pos)
        filter_complex = f"[1:v]scale=trunc(iw*{scale_percent}/100/2)*2:-2[logo];[0:v][logo]overlay={x}:{y},format=yuv420p[vout]"

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-loglevel", "error",
            "-threads", "2",
            "-i", input_video_path,
            "-i", logo_image_path,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "faster",
            "-crf", "24",
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path
        ]

        async with self.semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                    logger.error(f"FFmpeg logo process timed out (>120s) for {input_video_path}")
                    return None

                if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    return output_path
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                return None
            except Exception as e:
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                logger.error(f"Error executing logo watermark: {e}")
                return None

    async def apply_video_watermark(
        self,
        input_video_path: str,
        watermark_text: str,
        pos: str = "bottom_right",
        font_size: int = 24,
        output_dir: str = "temp_media"
    ) -> Optional[str]:
        """Backward compatibility alias pointing to apply_video_text_watermark"""
        return await self.apply_video_text_watermark(
            input_video_path=input_video_path,
            watermark_text=watermark_text,
            pos=pos,
            font_size=font_size,
            output_dir=output_dir
        )

video_watermark_service = VideoWatermarkService()
