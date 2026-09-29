import os
import time
import uuid
import asyncio
import glob
import random
import logging
import subprocess
import shutil
import threading
from typing import List, Optional, Dict, Any, Tuple
from PIL import Image, ImageFilter

from services.story_renderer import story_card_renderer
from services.render_queue import render_queue

logger = logging.getLogger(__name__)

STORY_FPS = 30
FULL_HD_SIZE = (1080, 1920)
# Telegram's own clients upload video stories at 720x1280; used on small hosts (OOM / slow CPU)
LOW_MEMORY_SIZE = (720, 1280)
MIN_DURATION = 15.0
MAX_DURATION = 40.0
# Blurred ambient slides are computed at this fraction of the output size (the blur hides the difference)
_SLIDE_WORK_SCALE = 2


def _temp_media_dir() -> str:
    temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def _remove_quietly(path: Optional[str]) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)


class StoryVideoGenerator:
    """
    Generates luxury vertical MP4 video stories (1080x1920, or 720x1280 on low-memory hosts)
    for Telegram Stories. Features:
    - Ambient room slideshow with smooth 0.8s crossfade transitions (xfade)
    - 1:1 Telegram native repost card overlay (transparent PNG)
    - Curated luxury lounge & chill music with synchronized audio fade-in/fade-out
    - Strict anti-repetition engine: consecutive video generations never repeat music (history survives restarts)
    - Precise bounding box coordinates for InputMediaAreaChannelPost
    Every slide is decoded and converted once (looped in memory), so encoding cost is dominated by x264 only.
    """

    def __init__(self, audio_dir: Optional[str] = None, max_history: int = 10):
        if audio_dir:
            self.audio_dir = audio_dir
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self.audio_dir = os.path.join(base_dir, "assets", "audio")
        self.max_history = max(3, max_history)
        self._history_lock = threading.RLock()
        self._recent_tracks: List[str] = []
        self._user_recent_tracks: Dict[int, List[str]] = {}
        self._history_seeded = False

    def get_available_tracks(self) -> List[str]:
        """Returns sorted list of audio track filepaths found in self.audio_dir or fallbacks"""
        target_dir = self.audio_dir
        if not os.path.exists(target_dir):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            fallbacks = [
                os.path.join(base_dir, "assets", "audio"),
                os.path.join(os.getcwd(), "assets", "audio"),
                "/app/assets/audio"
            ]
            for fb in fallbacks:
                if os.path.exists(fb):
                    self.audio_dir = fb
                    target_dir = fb
                    break
        if not os.path.exists(target_dir):
            return []
        tracks = []
        for ext in ("*.mp3", "*.m4a", "*.wav", "*.aac"):
            tracks.extend(glob.glob(os.path.join(target_dir, ext)))
        valid = [t for t in tracks if os.path.exists(t) and os.path.getsize(t) > 1000]
        return sorted(valid) if valid else sorted(tracks)

    def record_track_usage(self, track_path: str, user_id: Optional[int] = None) -> None:
        """Records a track in the anti-repetition history (thread-safe)"""
        if not track_path:
            return
        track_name = os.path.basename(track_path)
        with self._history_lock:
            if track_name in self._recent_tracks:
                self._recent_tracks.remove(track_name)
            self._recent_tracks.append(track_name)
            if len(self._recent_tracks) > max(self.max_history * 2, 25):
                self._recent_tracks = self._recent_tracks[-self.max_history:]

            if user_id is not None:
                user_list = self._user_recent_tracks.setdefault(user_id, [])
                if track_name in user_list:
                    user_list.remove(track_name)
                user_list.append(track_name)
                if len(user_list) > max(self.max_history * 2, 25):
                    self._user_recent_tracks[user_id] = user_list[-self.max_history:]

    def get_recent_tracks(self, user_id: Optional[int] = None) -> List[str]:
        """Returns the list of recently used track filenames"""
        with self._history_lock:
            if user_id is not None and user_id in self._user_recent_tracks:
                return list(self._user_recent_tracks[user_id])
            return list(self._recent_tracks)

    def clear_history(self) -> None:
        """Clears in-memory recent tracks history"""
        with self._history_lock:
            self._recent_tracks.clear()
            self._user_recent_tracks.clear()

    def set_max_history(self, limit: int) -> None:
        """Sets max anti-repetition history window size"""
        with self._history_lock:
            self.max_history = max(1, limit)

    async def seed_history_from_db(self) -> None:
        """Loads the persisted music history once per process, so a restart does not replay the same tracks"""
        if self._history_seeded:
            return
        self._history_seeded = True
        try:
            from database.db_manager import db_manager
            persisted = await db_manager.get_recent_story_music(limit=self.max_history)
        except Exception:
            logger.debug("Could not load persisted story music history", exc_info=True)
            return
        with self._history_lock:
            older = [name for name in persisted if name and name not in self._recent_tracks]
            self._recent_tracks = older + self._recent_tracks

    def get_random_music_track(
        self,
        user_id: Optional[int] = None,
        exclude_recent: bool = True
    ) -> Optional[str]:
        """
        Selects a music track from the curated luxury real estate audio collection.
        Enforces strict anti-repetition: consecutive 2 or 3 (up to max_history)
        video generations will never use the same music.
        """
        tracks = self.get_available_tracks()
        if not tracks:
            logger.warning(f"No audio tracks found in {self.audio_dir}")
            return None

        if len(tracks) == 1:
            self.record_track_usage(tracks[0], user_id=user_id)
            return tracks[0]

        with self._history_lock:
            # Determine history window: at least 1, up to len(tracks) - 1, bounded by max_history
            window_size = max(1, min(len(tracks) - 1, self.max_history))

            if not exclude_recent:
                chosen = random.choice(tracks)
                self.record_track_usage(chosen, user_id=user_id)
                return chosen

            # Global and user-specific exclusion lists
            recent_global = self._recent_tracks[-window_size:] if self._recent_tracks else []
            recent_user = self._user_recent_tracks.get(user_id, [])[-window_size:] if user_id is not None else []

            # Exclude tracks from recent history
            excluded_set = set(recent_global) | set(recent_user)
            candidates = [t for t in tracks if os.path.basename(t) not in excluded_set and t not in excluded_set]

            # If over-constrained (e.g. pool is small or user+global overlap excluded all),
            # gracefully fall back to excluding at least the last 2 tracks (or last 1)
            if not candidates:
                min_safe_window = min(len(tracks) - 1, 2)
                last_few = set(recent_global[-min_safe_window:])
                candidates = [t for t in tracks if os.path.basename(t) not in last_few and t not in last_few]

            # Guarantee never repeating the immediate previous track
            if not candidates and recent_global:
                last_single = recent_global[-1]
                candidates = [t for t in tracks if os.path.basename(t) != last_single and t != last_single]

            if not candidates:
                candidates = tracks

            chosen = random.choice(candidates)
            self.record_track_usage(chosen, user_id=user_id)
            logger.info(
                f"Selected story music track: {os.path.basename(chosen)} "
                f"(pool: {len(tracks)}, candidates: {len(candidates)}, user: {user_id})"
            )
            return chosen

    @staticmethod
    def choose_render_profile() -> Dict[str, Any]:
        """Output size / x264 preset for this host: 720x1280 when memory is tight, 1080x1920 otherwise"""
        try:
            low_memory = render_queue.is_low_memory_host()
        except Exception:
            low_memory = False
        width, height = LOW_MEMORY_SIZE if low_memory else FULL_HD_SIZE
        return {"width": width, "height": height, "preset": "veryfast", "low_memory": low_memory}

    @staticmethod
    def encode_timeout(duration: float, width: int = FULL_HD_SIZE[0]) -> float:
        """FFmpeg time budget derived from the story length (a 25 s full HD story needs ~1-2.5 min on a busy host)"""
        per_second = 6.0 if width >= FULL_HD_SIZE[0] else 3.0
        return 60.0 + float(duration) * per_second

    @staticmethod
    def create_ambient_slide(
        photo_path: str,
        out_path: str,
        width: int = 1080,
        height: int = 1920
    ) -> str:
        """
        Creates an ambient blurred vertical slide from any aspect ratio photo:
        - Scales and center-crops to width x height
        - Gaussian blur (radius 32 at 1080 px width)
        - Subtle luxury darkening for elegant contrast behind the repost card
        The blur is computed at half resolution and upscaled: visually identical, a quarter of the memory.
        """
        work_w = max(1, width // _SLIDE_WORK_SCALE)
        work_h = max(1, height // _SLIDE_WORK_SCALE)
        with Image.open(photo_path) as src:
            src.draft("RGB", (work_w, work_h))
            orig = src.convert("RGB")
        orig_w, orig_h = orig.size

        scale = max(work_w / orig_w, work_h / orig_h)
        nw, nh = max(work_w, int(round(orig_w * scale))), max(work_h, int(round(orig_h * scale)))
        resized = orig.resize((nw, nh), Image.Resampling.BILINEAR)
        del orig

        left = (nw - work_w) // 2
        top = (nh - work_h) // 2
        cropped = resized.crop((left, top, left + work_w, top + work_h))

        blur_radius = max(4, int(round(32 * width / 1080.0 / _SLIDE_WORK_SCALE)))
        blurred = cropped.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        dimmer = Image.new("RGB", (work_w, work_h), (15, 20, 18))
        final_bg = Image.blend(blurred, dimmer, alpha=0.30)
        final_bg = final_bg.resize((width, height), Image.Resampling.BICUBIC)
        final_bg.save(out_path, format="JPEG", quality=92)
        return out_path

    def build_ffmpeg_command(
        self,
        slide_paths: List[str],
        overlay_path: str,
        audio_path: Optional[str],
        output_mp4: str,
        total_duration: float = 25.0,
        xfade_duration: float = 0.8,
        voiceover_path: Optional[str] = None,
        volume: float = 1.0,
        width: int = FULL_HD_SIZE[0],
        height: int = FULL_HD_SIZE[1],
        preset: str = "veryfast"
    ) -> List[str]:
        """Constructs the FFmpeg command (argument list, no shell) for crossfade, overlay and audio muxing.
        Each slide / the overlay is a single decoded frame looped in memory; no per-frame zoom or re-decoding."""
        n = len(slide_paths)
        if n == 0:
            raise ValueError("At least one slide image is required")

        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
        has_voice = bool(voiceover_path and os.path.exists(voiceover_path))
        threads_val = str(min(4, os.cpu_count() or 2))
        fps = STORY_FPS

        if n == 1:
            slide_durations = [float(total_duration)]
            step = 0.0
        else:
            s_duration = (total_duration + (n - 1) * xfade_duration) / n
            slide_durations = [s_duration] * n
            step = s_duration - xfade_duration

        # 1. Inputs: slides and overlay are single frames, audio / silence afterwards
        for p in slide_paths:
            cmd.extend(["-framerate", str(fps), "-i", p])
        overlay_idx = n
        cmd.extend(["-framerate", str(fps), "-i", overlay_path])

        has_audio = bool(audio_path and os.path.exists(audio_path))
        audio_idx = n + 1
        voice_idx = None
        if has_audio:
            cmd.extend(["-i", audio_path])
            if has_voice:
                voice_idx = audio_idx + 1
                cmd.extend(["-i", voiceover_path])
        elif has_voice:
            voice_idx = audio_idx
            cmd.extend(["-i", voiceover_path])
        else:
            cmd.extend(["-f", "lavfi", "-t", str(total_duration), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])

        # 2. Video graph
        filters = []
        for i, dur in enumerate(slide_durations):
            frames = max(1, int(round(dur * fps)) + (1 if n == 1 else 0))
            filters.append(
                f"[{i}:v]scale={width}:{height}:flags=bicubic,format=yuv420p,"
                f"loop=loop={frames - 1}:size=1:start=0,setpts=N/{fps}/TB,setsar=1[v{i}]"
            )

        last_v = "v0"
        for i in range(n - 1):
            next_v = f"v{i + 1}"
            out_v = f"xf{i}" if i < n - 2 else "bg_final"
            offset = (i + 1) * step
            filters.append(f"[{last_v}][{next_v}]xfade=transition=fade:duration={xfade_duration:.2f}:offset={offset:.3f}[{out_v}]")
            last_v = out_v

        overlay_src = f"[{overlay_idx}:v]"
        if (width, height) != FULL_HD_SIZE:
            filters.append(f"{overlay_src}scale={width}:{height}:flags=bicubic[ov_scaled]")
            overlay_src = "[ov_scaled]"
        # The overlay is one frame: eof_action=repeat keeps it on screen for the whole story
        filters.append(f"[{last_v}]{overlay_src}overlay=0:0:format=yuv420:eof_action=repeat[vout]")

        # 3. Audio graph: fade in/out, continuous looping, optional voiceover sidechain ducking
        fade_dur = 1.5 if total_duration >= 3.0 else min(1.5, max(0.2, total_duration / 3.0))
        fade_out_start = max(0.0, total_duration - fade_dur)

        if has_audio and has_voice:
            filters.append(
                f"[{audio_idx}:a]aloop=loop=-1:size=2e+09,afade=t=in:ss=0:d=1.0,afade=t=out:st={fade_out_start:.1f}:d={fade_dur:.1f}[bg_music];"
                f"[{voice_idx}:a]volume=1.3,asplit=2[voice_sc][voice_mix];"
                f"[bg_music][voice_sc]sidechaincompress=threshold=0.08:ratio=5:attack=100:release=500[ducked_bg];"
                f"[ducked_bg][voice_mix]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
        elif has_voice:
            filters.append(f"[{voice_idx}:a]volume=1.2,afade=t=in:ss=0:d=0.5,afade=t=out:st={fade_out_start:.1f}:d={fade_dur:.1f}[aout]")
        else:
            filters.append(
                f"[{audio_idx}:a]aloop=loop=-1:size=2e+09,volume={volume:.2f},"
                f"afade=t=in:ss=0:d=1.0,afade=t=out:st={fade_out_start:.1f}:d={fade_dur:.1f}[aout]"
            )

        cmd.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264",
            "-preset", preset,
            "-threads", threads_val,
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            "-t", str(total_duration),
            "-movflags", "+faststart",
            output_mp4
        ])
        return cmd

    # ------------------------------------------------------------------
    # Rendering pipeline
    # ------------------------------------------------------------------

    def _prepare_visuals(
        self,
        job_dir: str,
        photo_paths: List[str],
        channel_title: str,
        caption: str,
        price: Optional[float],
        date_str: Optional[str],
        avatar_path: Optional[str],
        forward_title: Optional[str],
        badges: Optional[List[str]],
        width: int,
        height: int
    ) -> Tuple[str, Dict[str, float], List[str]]:
        """CPU work done in a worker thread: card overlay, cover photo selection, blurred slides"""
        overlay_path = os.path.join(job_dir, "overlay.png")
        overlay_path, card_coords = story_card_renderer.render_card_overlay_png_with_coords(
            channel_title=channel_title,
            photo_paths=photo_paths,
            caption=caption,
            price=price,
            date_str=date_str,
            avatar_path=avatar_path,
            forward_title=forward_title,
            badges=badges,
            output_path=overlay_path
        )

        # Reorder photos using aesthetic visual scorer: sharpest, most vibrant photo becomes Slide 1!
        from services.aesthetic_scorer import aesthetic_scorer
        valid_photos = aesthetic_scorer.reorder_photos_by_aesthetic(photo_paths)

        # Cap ambient background slides to at most 4 photos (best 4 photos)
        valid_photos = valid_photos[:4]

        slide_paths = []
        for i, p in enumerate(valid_photos):
            sp = os.path.join(job_dir, f"slide_{i}.jpg")
            self.create_ambient_slide(p, sp, width=width, height=height)
            slide_paths.append(sp)
        return overlay_path, card_coords, slide_paths

    def _choose_audio(self, audio_path: Optional[str], user_id: Optional[int]) -> Optional[str]:
        if audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            self.record_track_usage(audio_path, user_id=user_id)
            return audio_path
        return self.get_random_music_track(user_id=user_id)

    @staticmethod
    def _new_job_dir() -> str:
        # "story_" prefix: protected work directory of the story pipeline (removed by this class when done)
        job_dir = os.path.join(_temp_media_dir(), f"story_job_{uuid.uuid4().hex}")
        os.makedirs(job_dir, exist_ok=True)
        return job_dir

    @staticmethod
    def _output_ok(path: str) -> bool:
        return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0

    def create_video_story(
        self,
        photo_paths: List[str],
        channel_title: str,
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[str]] = None,
        audio_path: Optional[str] = None,
        duration: float = 25.0,
        output_path: Optional[str] = None,
        user_id: Optional[int] = None,
        voiceover_path: Optional[str] = None,
        enable_ai_voice: bool = False,
        volume: float = 1.0
    ) -> Tuple[str, Dict[str, float]]:
        """
        Creates a complete video story ready for MTProto stories.sendStory (blocking; scripts / worker threads).
        Returns (video_path, card_coordinates_dict); the dict also carries video_w / video_h.
        Voice narration is not supported for stories (voiceover_path / enable_ai_voice are ignored).
        """
        valid_photos = [p for p in photo_paths if p and os.path.exists(p)]
        if not valid_photos:
            raise ValueError("Kamida bitta fotosurat talab qilinadi (video istoriya fotosuratsiz yaratilmaydi)")

        # Clamp duration to 15s - 40s (strict Telegram Story standards)
        duration = max(MIN_DURATION, min(MAX_DURATION, float(duration or 25.0)))
        profile = self.choose_render_profile()
        width, height = profile["width"], profile["height"]
        if not output_path:
            output_path = os.path.join(_temp_media_dir(), f"video_story_{uuid.uuid4().hex}.mp4")

        job_dir = self._new_job_dir()
        succeeded = False
        try:
            overlay_path, card_coords, slide_paths = self._prepare_visuals(
                job_dir, valid_photos, channel_title, caption, price, date_str, avatar_path, forward_title, badges, width, height
            )
            chosen_audio = self._choose_audio(audio_path, user_id)
            timeout = self.encode_timeout(duration, width)

            attempts = [slide_paths] if len(slide_paths) <= 1 else [slide_paths, slide_paths[:1]]
            last_error = ""
            for idx, slides in enumerate(attempts):
                cmd = self.build_ffmpeg_command(
                    slide_paths=slides,
                    overlay_path=overlay_path,
                    audio_path=chosen_audio,
                    output_mp4=output_path,
                    total_duration=duration,
                    xfade_duration=0.8,
                    volume=volume,
                    width=width,
                    height=height,
                    preset=profile["preset"]
                )
                logger.info(f"Encoding {duration}s {width}x{height} video story with {len(slides)} slides and audio {os.path.basename(chosen_audio) if chosen_audio else 'none'}...")
                t0 = time.time()
                try:
                    res = subprocess.run(
                        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout
                    )
                except subprocess.TimeoutExpired:
                    # subprocess.run() kills and reaps the child before raising
                    logger.error(f"FFmpeg video story encoding timed out after {timeout:.0f}s")
                    raise TimeoutError("FFmpeg video encoding timed out")
                if res.returncode == 0 and self._output_ok(output_path):
                    logger.info(f"Video story encoded in {time.time() - t0:.2f}s -> {output_path} ({os.path.getsize(output_path)} bytes)")
                    succeeded = True
                    coords = dict(card_coords)
                    coords.update({"video_w": width, "video_h": height})
                    return output_path, coords
                err_bytes = res.stderr if isinstance(res.stderr, (bytes, bytearray)) else b""
                last_error = f"FFmpeg video encoding failed (code {res.returncode}): {err_bytes.decode('utf-8', 'ignore')[-500:]}"
                if idx + 1 < len(attempts):
                    logger.warning(f"{last_error} - Attempting resilient single-slide video fallback...")
            raise RuntimeError(last_error or "FFmpeg video encoding failed")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
            if not succeeded:
                _remove_quietly(output_path)

    async def _run_ffmpeg_async(self, cmd: List[str], timeout: float) -> Tuple[int, str]:
        """Runs FFmpeg without blocking the loop; on timeout / cancellation the process is killed AND reaped"""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await self._kill_process(proc)
            raise
        err_text = stderr.decode("utf-8", errors="ignore")[-500:] if isinstance(stderr, (bytes, bytearray)) else ""
        return proc.returncode, err_text

    @staticmethod
    async def _kill_process(proc) -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=15.0)
        except Exception:
            logger.warning("FFmpeg process did not exit after kill()", exc_info=True)

    async def create_video_story_async(
        self,
        photo_paths: List[str],
        channel_title: str,
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[str]] = None,
        audio_path: Optional[str] = None,
        duration: float = 25.0,
        output_path: Optional[str] = None,
        user_id: Optional[int] = None,
        voiceover_path: Optional[str] = None,
        enable_ai_voice: bool = False,
        volume: float = 1.0
    ) -> Tuple[str, Dict[str, float]]:
        """
        Asynchronously creates a complete video story using non-blocking FFmpeg.
        Returns (video_path, card_coordinates_dict); the dict also carries video_w / video_h.
        Voice narration is not supported for stories (voiceover_path / enable_ai_voice are ignored).
        """
        valid_photos = [p for p in photo_paths if p and os.path.exists(p)]
        if not valid_photos:
            raise ValueError("Kamida bitta fotosurat talab qilinadi (video istoriya fotosuratsiz yaratilmaydi)")

        # Clamp duration to 15s - 40s (strict Telegram Story standards)
        duration = max(MIN_DURATION, min(MAX_DURATION, float(duration or 25.0)))
        profile = self.choose_render_profile()
        width, height = profile["width"], profile["height"]
        if not output_path:
            output_path = os.path.join(_temp_media_dir(), f"video_story_{uuid.uuid4().hex}.mp4")

        job_dir = self._new_job_dir()
        succeeded = False
        try:
            overlay_path, card_coords, slide_paths = await asyncio.to_thread(
                self._prepare_visuals,
                job_dir, valid_photos, channel_title, caption, price, date_str, avatar_path, forward_title, badges, width, height
            )

            # Music selection with anti-repetition (persisted history survives restarts)
            await self.seed_history_from_db()
            chosen_audio = self._choose_audio(audio_path, user_id)
            if chosen_audio:
                try:
                    from database.db_manager import db_manager
                    await db_manager.record_used_story_music(chosen_audio)
                except Exception:
                    logger.debug("Could not persist story music usage", exc_info=True)

            timeout = self.encode_timeout(duration, width)
            attempts = [slide_paths] if len(slide_paths) <= 1 else [slide_paths, slide_paths[:1]]
            last_error = ""
            for idx, slides in enumerate(attempts):
                cmd = self.build_ffmpeg_command(
                    slide_paths=slides,
                    overlay_path=overlay_path,
                    audio_path=chosen_audio,
                    output_mp4=output_path,
                    total_duration=duration,
                    xfade_duration=0.8,
                    volume=volume,
                    width=width,
                    height=height,
                    preset=profile["preset"]
                )
                logger.info(f"Async encoding {duration}s {width}x{height} video story with {len(slides)} slides and audio {os.path.basename(chosen_audio) if chosen_audio else 'none'}...")
                t0 = time.time()
                try:
                    returncode, err_text = await self._run_ffmpeg_async(cmd, timeout)
                except asyncio.TimeoutError:
                    logger.error(f"FFmpeg async video encoding timed out after {timeout:.0f}s")
                    raise TimeoutError(f"FFmpeg async video encoding timed out after {timeout:.0f}s")
                if returncode == 0 and self._output_ok(output_path):
                    logger.info(f"Video story async encoded in {time.time() - t0:.2f}s -> {output_path} ({os.path.getsize(output_path)} bytes)")
                    succeeded = True
                    coords = dict(card_coords)
                    coords.update({"video_w": width, "video_h": height})
                    return output_path, coords
                last_error = f"FFmpeg video encoding failed (code {returncode}): {err_text}"
                if idx + 1 < len(attempts):
                    logger.warning(f"{last_error} - Attempting resilient single-slide video fallback...")
            raise RuntimeError(last_error or "FFmpeg video encoding failed")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
            if not succeeded:
                _remove_quietly(output_path)


story_video_generator = StoryVideoGenerator()
