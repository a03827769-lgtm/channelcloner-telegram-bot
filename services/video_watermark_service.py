import os
import re
import html
import asyncio
import logging
import shutil
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# FFmpeg time budget: never less than 2 minutes, scaled with the file size / duration, capped at 30 minutes
FFMPEG_MIN_TIMEOUT = 120.0
FFMPEG_MAX_TIMEOUT = 1800.0
FFMPEG_SECONDS_PER_MB = 8.0
FFMPEG_SECONDS_PER_VIDEO_SECOND = 3.0


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

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        """Escapes an option value (e.g. a file path) for an FFmpeg filtergraph.

        Two escaping levels apply: inside the option value \\ ' : are special (escaped with a backslash), and the
        whole value is single-quoted for the filtergraph level, where a quote is written as '\\'' (close, escaped
        quote, reopen). Works for Windows drive letters and paths containing apostrophes, commas or brackets."""
        v = str(value).replace("\\", "/")
        v = v.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
        return "'" + v.replace("'", "'\\''") + "'"

    def _prepare_watermark_text(self, text: str) -> str:
        """Plain watermark text (tags stripped, entities unescaped, single line). The text itself is handed to
        FFmpeg through textfile= with expansion=none, so no filtergraph escaping of the text is needed."""
        t = html.unescape(re.sub(r'<[^>]*>', '', str(text or "")))
        t = t.replace("\r", "").replace("\n", " ")
        return re.sub(r'\s+', ' ', t).strip()

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

    @staticmethod
    def _timeout_for(input_video_path: str, duration: Optional[float] = None) -> float:
        """FFmpeg time budget scaled with the input size and, when known, the video duration."""
        try:
            size_mb = os.path.getsize(input_video_path) / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        budget = size_mb * FFMPEG_SECONDS_PER_MB
        if duration:
            try:
                budget = max(budget, float(duration) * FFMPEG_SECONDS_PER_VIDEO_SECOND + 60.0)
            except (TypeError, ValueError):
                pass
        return max(FFMPEG_MIN_TIMEOUT, min(FFMPEG_MAX_TIMEOUT, budget))

    @staticmethod
    def _default_output_dir() -> str:
        from services.media_handler import media_handler
        return media_handler.temp_dir

    @staticmethod
    def _remove_quietly(path: Optional[str]):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

    async def _run_ffmpeg(self, cmd: list, output_path: str, timeout: float, input_video_path: str, kind: str) -> Optional[str]:
        """Runs FFmpeg under the concurrency semaphore; returns output_path on success, None otherwise."""
        from services.media_handler import media_handler
        async with self.semaphore:
            # The output is written progressively; keep it away from the temp cleaner while FFmpeg runs
            with media_handler.holding(output_path, input_video_path):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    try:
                        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                            await proc.wait()
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                        self._remove_quietly(output_path)
                        logger.warning(
                            f"FFmpeg {kind} watermark timed out after {timeout:.0f}s for {input_video_path}; "
                            f"the video is published without a watermark."
                        )
                        return None

                    if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                        return output_path
                    self._remove_quietly(output_path)
                    logger.warning(f"FFmpeg {kind} watermark failed with returncode {proc.returncode}: {stderr.decode('utf-8', errors='ignore')[:300]}")
                    return None
                except Exception as e:
                    self._remove_quietly(output_path)
                    logger.error(f"Error executing {kind} watermark: {e}")
                    return None

    async def apply_video_text_watermark(
        self,
        input_video_path: str,
        watermark_text: str,
        pos: str = "bottom_right",
        font_size: int = 24,
        output_dir: Optional[str] = None,
        duration: Optional[float] = None
    ) -> Optional[str]:
        """
        Applies a clean text watermark onto a video file using async FFmpeg.
        Returns the path to the watermarked video, or None if failed.
        """
        watermark_text = self._prepare_watermark_text(watermark_text)[:100]
        if not os.path.exists(input_video_path) or not watermark_text:
            return None

        output_dir = output_dir or self._default_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        token = uuid.uuid4().hex[:8]
        output_path = os.path.join(output_dir, f"wm_vid_{token}.mp4")
        # The text goes through a UTF-8 text file: apostrophes, colons, % and brackets need no filter escaping
        text_file = os.path.join(output_dir, f"wm_text_{token}.txt")
        try:
            with open(text_file, "w", encoding="utf-8") as fh:
                fh.write(watermark_text)
        except OSError as e:
            logger.error(f"Could not write watermark text file: {e}")
            return None

        x, y = self._get_drawtext_coordinates(pos)

        font_arg = ""
        font_path = self._find_default_fontfile()
        if font_path:
            font_arg = f":fontfile={self._escape_filter_value(font_path)}"

        # Drawtext filter with shadow for high legibility on any background
        vf_filter = (
            f"drawtext=textfile={self._escape_filter_value(os.path.abspath(text_file))}:expansion=none{font_arg}:"
            f"fontcolor=white@0.85:fontsize={font_size}:"
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

        logger.info(f"Applying video text watermark '{watermark_text}' to {input_video_path}")
        try:
            return await self._run_ffmpeg(cmd, output_path, self._timeout_for(input_video_path, duration), input_video_path, "text")
        finally:
            self._remove_quietly(text_file)

    async def apply_video_logo_watermark(
        self,
        input_video_path: str,
        logo_image_path: str,
        pos: str = "bottom_right",
        scale_percent: int = 15,
        output_dir: Optional[str] = None,
        duration: Optional[float] = None
    ) -> Optional[str]:
        """
        Overlays a transparent PNG logo onto a video file using async FFmpeg.
        """
        if not os.path.exists(input_video_path) or not logo_image_path or not os.path.exists(logo_image_path):
            return None

        output_dir = output_dir or self._default_output_dir()
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

        return await self._run_ffmpeg(cmd, output_path, self._timeout_for(input_video_path, duration), input_video_path, "logo")

    async def apply_video_watermark(
        self,
        input_video_path: str,
        watermark_text: str,
        pos: str = "bottom_right",
        font_size: int = 24,
        output_dir: Optional[str] = None
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
