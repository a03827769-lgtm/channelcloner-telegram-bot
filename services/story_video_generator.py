import os
import sys
import time
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

logger = logging.getLogger(__name__)


class StoryVideoGenerator:
    """
    Generates 25-second luxury vertical MP4 video stories (1080x1920 Full HD)
    for Telegram Stories. Features:
    - Ambient room slideshow with smooth 0.8s crossfade transitions (xfade)
    - 1:1 Telegram native repost card overlay (Playwright transparent PNG)
    - Curated luxury lounge & chill music with synchronized audio fade-in/fade-out
    - Strict anti-repetition engine: consecutive video generations never repeat music
    - Precise bounding box coordinates for InputMediaAreaChannelPost
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

    def get_available_tracks(self) -> List[str]:
        """Returns sorted list of audio track filepaths found in self.audio_dir or fallbacks"""
        target_dir = self.audio_dir
        if not os.path.exists(target_dir):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            fallbacks = [
                os.path.join(base_dir, "assets", "audio"),
                os.path.join(os.getcwd(), "assets", "audio"),
                "/app/assets/audio",
                r"c:\Users\victus\Desktop\channelcloner\assets\audio"
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
    def create_ambient_slide(
        photo_path: str,
        out_path: str,
        width: int = 1080,
        height: int = 1920
    ) -> str:
        """
        Creates an ambient blurred vertical slide from any aspect ratio photo:
        - Scales and center-crops to 1080x1920
        - Gaussian blur (radius 32)
        - Subtle luxury darkening for elegant contrast behind the repost card
        """
        orig = Image.open(photo_path).convert("RGB")
        orig_w, orig_h = orig.size

        scale = max(width / orig_w, height / orig_h)
        nw, nh = int(orig_w * scale), int(orig_h * scale)
        resized = orig.resize((nw, nh), Image.Resampling.LANCZOS)

        left = (nw - width) // 2
        top = (nh - height) // 2
        cropped = resized.crop((left, top, left + width, top + height))

        blurred = cropped.filter(ImageFilter.GaussianBlur(radius=32))
        dimmer = Image.new("RGB", (width, height), (15, 20, 18))
        final_bg = Image.blend(blurred, dimmer, alpha=0.30)
        final_bg.save(out_path, quality=95)
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
        volume: float = 1.0
    ) -> List[str]:
        """Constructs the optimized FFmpeg command line for crossfade, overlay, audio muxing and AI voice ducking"""
        n = len(slide_paths)
        if n == 0:
            raise ValueError("At least one slide image is required")

        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg_bin, "-y"]
        has_voice = bool(voiceover_path and os.path.exists(voiceover_path))

        threads_val = str(min(4, os.cpu_count() or 2))

        # 1. Single slide case
        if n == 1:
            fade_dur = 1.5 if total_duration >= 3.0 else min(1.5, max(0.2, total_duration / 3.0))
            fade_out_start = max(0.0, total_duration - fade_dur)
            cmd.extend([
                "-loop", "1", "-framerate", "30", "-t", str(total_duration), "-i", slide_paths[0],
                "-loop", "1", "-framerate", "30", "-t", str(total_duration), "-i", overlay_path
            ])
            if audio_path and os.path.exists(audio_path):
                cmd.extend(["-i", audio_path])
                if has_voice:
                    cmd.extend(["-i", voiceover_path])
                    filter_str = (
                        f"[0:v]scale=1188:2112,zoompan=z='min(zoom+0.0002,1.05)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,setsar=1[bg];"
                        f"[bg][1:v]overlay=0:0:format=auto:eof_action=repeat[vout];"
                        f"[2:a]aloop=loop=-1:size=2e+09,afade=t=in:ss=0:d=1.0,afade=t=out:st={fade_out_start:.1f}:d={fade_dur:.1f}[bg_music];"
                        f"[3:a]volume=1.3,asplit=2[voice_sc][voice_mix];"
                        f"[bg_music][voice_sc]sidechaincompress=threshold=0.08:ratio=5:attack=100:release=500[ducked_bg];"
                        f"[ducked_bg][voice_mix]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                    )
                else:
                    filter_str = (
                        f"[0:v]scale=1188:2112,zoompan=z='min(zoom+0.0002,1.05)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,setsar=1[bg];"
                        f"[bg][1:v]overlay=0:0:format=auto:eof_action=repeat[vout];"
                        f"[2:a]aloop=loop=-1:size=2e+09,volume={volume:.2f},afade=t=in:ss=0:d=1.0,afade=t=out:st={fade_out_start:.1f}:d={fade_dur:.1f}[aout]"
                    )
                cmd.extend([
                    "-filter_complex", filter_str,
                    "-map", "[vout]",
                    "-map", "[aout]",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-ar", "44100",
                    "-ac", "2"
                ])
            else:
                # No audio track: generate silent stream or use voice only
                if has_voice:
                    cmd.extend(["-i", voiceover_path])
                    filter_str = (
                        "[0:v]scale=1188:2112,zoompan=z='min(zoom+0.0002,1.05)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,setsar=1[bg];"
                        "[bg][1:v]overlay=0:0:format=auto:eof_action=repeat[vout];"
                        f"[2:a]volume=1.2,afade=t=in:ss=0:d=0.5,afade=t=out:st={fade_out_start:.1f}:d={fade_dur:.1f}[aout]"
                    )
                else:
                    filter_str = (
                        "[0:v]scale=1188:2112,zoompan=z='min(zoom+0.0002,1.05)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,setsar=1[bg];"
                        "[bg][1:v]overlay=0:0:format=auto:eof_action=repeat[vout];"
                        f"[2:a]afade=t=in:ss=0:d=1.0,afade=t=out:st={fade_out_start:.1f}:d={fade_dur:.1f}[aout]"
                    )
                    cmd.extend(["-f", "lavfi", "-t", str(total_duration), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])

                cmd.extend([
                    "-filter_complex", filter_str,
                    "-map", "[vout]",
                    "-map", "[aout]",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-ar", "44100",
                    "-ac", "2"
                ])

            cmd.extend([
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-threads", threads_val,
                "-crf", "22",
                "-pix_fmt", "yuv420p",
                "-t", str(total_duration),
                "-movflags", "+faststart",
                output_mp4
            ])
            return cmd

        # 2. Multi-slide crossfade case (N >= 2)
        s_duration = (total_duration + (n - 1) * xfade_duration) / n
        step = s_duration - xfade_duration

        for p in slide_paths:
            cmd.extend(["-loop", "1", "-framerate", "30", "-t", f"{s_duration:.3f}", "-i", p])

        overlay_idx = n
        cmd.extend(["-loop", "1", "-framerate", "30", "-t", str(total_duration), "-i", overlay_path])

        has_audio = audio_path and os.path.exists(audio_path)
        if has_audio:
            audio_idx = n + 1
            cmd.extend(["-i", audio_path])
        else:
            audio_idx = n + 1
            cmd.extend(["-f", "lavfi", "-t", str(total_duration), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])

        if has_voice:
            voice_idx = audio_idx + 1
            cmd.extend(["-i", voiceover_path])

        # Build filtergraph with cinematic ambient zoom
        filters = []
        for i in range(n):
            filters.append(
                f"[{i}:v]scale=1188:2112,zoompan=z='min(zoom+0.0003,1.06)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,setsar=1[v{i}]"
            )

        last_v = "v0"
        for i in range(n - 1):
            next_v = f"v{i + 1}"
            out_v = f"xf{i}" if i < n - 2 else "bg_final"
            offset = (i + 1) * step
            filters.append(f"[{last_v}][{next_v}]xfade=transition=fade:duration={xfade_duration:.2f}:offset={offset:.3f}[{out_v}]")
            last_v = out_v

        # Overlay card PNG
        filters.append(f"[{last_v}][{overlay_idx}:v]overlay=0:0:format=auto:eof_action=repeat[vout]")

        # Audio fade in/out with continuous looping and voiceover sidechain ducking
        fade_dur = 1.5 if total_duration >= 3.0 else min(1.5, max(0.2, total_duration / 3.0))
        fade_out_start = max(0.0, total_duration - fade_dur)

        if has_voice:
            filters.append(
                f"[{audio_idx}:a]aloop=loop=-1:size=2e+09,afade=t=in:ss=0:d=1.0,afade=t=out:st={fade_out_start:.1f}:d={fade_dur:.1f}[bg_music];"
                f"[{voice_idx}:a]volume=1.3,asplit=2[voice_sc][voice_mix];"
                f"[bg_music][voice_sc]sidechaincompress=threshold=0.08:ratio=5:attack=100:release=500[ducked_bg];"
                f"[ducked_bg][voice_mix]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
        else:
            filters.append(f"[{audio_idx}:a]aloop=loop=-1:size=2e+09,volume={volume:.2f},afade=t=in:ss=0:d=1.0,afade=t=out:st={fade_out_start:.1f}:d={fade_dur:.1f}[aout]")

        filter_complex_str = ";".join(filters)

        cmd.extend([
            "-filter_complex", filter_complex_str,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-threads", threads_val,
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            "-t", str(total_duration),
            "-movflags", "+faststart",
            output_mp4
        ])

        return cmd

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
        Creates a complete 25-second luxury video story ready for MTProto stories.sendStory.
        Returns (video_path, card_coordinates_dict).
        """
        temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
        os.makedirs(temp_dir, exist_ok=True)
        timestamp = int(time.time() * 1000)

        # 1. Render transparent card overlay PNG
        overlay_path = os.path.join(temp_dir, f"overlay_{timestamp}.png")
        story_card_renderer.render_card_overlay_png(
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
        card_coords = story_card_renderer.get_last_card_coordinates()

        # Clamp duration to 15s - 40s (strict Telegram Story standards)
        duration = max(15.0, min(40.0, float(duration or 25.0)))

        # 2. Prepare ambient background slides
        valid_photos = [p for p in photo_paths if p and os.path.exists(p)]
        if not valid_photos:
            raise ValueError("Kamida bitta fotosurat talab qilinadi (video istoriya fotosuratsiz yaratilmaydi)")

        # Reorder photos using aesthetic visual scorer: sharpest, most vibrant photo becomes Slide 1!
        from services.aesthetic_scorer import aesthetic_scorer
        valid_photos = aesthetic_scorer.reorder_photos_by_aesthetic(valid_photos)

        # Cap ambient background slides to at most 4 photos (best 4 photos)
        # Keeps FFmpeg RAM under 150MB, prevents OOM, and ensures smooth, elegant transitions!
        valid_photos = valid_photos[:4]

        slide_paths = []
        for i, p in enumerate(valid_photos):
            sp = os.path.join(temp_dir, f"slide_{timestamp}_{i}.jpg")
            self.create_ambient_slide(p, sp)
            slide_paths.append(sp)

        # 3. Select music track with anti-repetition
        if audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            chosen_audio = audio_path
            self.record_track_usage(chosen_audio, user_id=user_id)
        else:
            chosen_audio = self.get_random_music_track(user_id=user_id)

        # Video stories have NO voiceover narration ("gapiradigan narsa olib tashlangan")
        voiceover_path = None

        # 4. Define final output MP4 path
        if not output_path:
            output_path = os.path.join(temp_dir, f"video_story_{timestamp}.mp4")

        # 5. Build and execute FFmpeg command
        cmd = self.build_ffmpeg_command(
            slide_paths=slide_paths,
            overlay_path=overlay_path,
            audio_path=chosen_audio,
            output_mp4=output_path,
            total_duration=duration,
            xfade_duration=0.8,
            voiceover_path=None,
            volume=volume
        )

        logger.info(f"Encoding {duration}s video story with {len(slide_paths)} slides and audio {os.path.basename(chosen_audio) if chosen_audio else 'none'}...")
        t0 = time.time()
        try:
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg video story encoding timed out after 120s")
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            for sp in slide_paths:
                if os.path.exists(sp):
                    try:
                        os.remove(sp)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
            if os.path.exists(overlay_path):
                try:
                    os.remove(overlay_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            raise TimeoutError("FFmpeg video encoding timed out")

        elapsed = time.time() - t0

        if res.returncode != 0:
            err_msg = f"FFmpeg video encoding failed (code {res.returncode}): {res.stderr[-500:]}"
            logger.warning(f"{err_msg} - Attempting resilient single-slide audio video fallback...")
            # Resilient fallback: if multi-slide failed, retry with single slide
            if len(slide_paths) > 1 and valid_photos:
                try:
                    fallback_cmd = self.build_ffmpeg_command(
                        slide_paths=[slide_paths[0]],
                        overlay_path=overlay_path,
                        audio_path=chosen_audio,
                        output_mp4=output_path,
                        total_duration=duration,
                        xfade_duration=0.8,
                        volume=volume
                    )
                    res_fb = subprocess.run(
                        fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30
                    )
                    if res_fb.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                        logger.info(f"Resilient single-slide audio video succeeded -> {output_path} ({os.path.getsize(output_path)} bytes)")
                        if os.path.exists(overlay_path):
                            try: os.remove(overlay_path)
                            except Exception: logger.debug("Ignored exception", exc_info=True)
                        for sp in slide_paths:
                            if os.path.exists(sp):
                                try: os.remove(sp)
                                except Exception: logger.debug("Ignored exception", exc_info=True)
                        return output_path, card_coords
                except Exception as fb_err:
                    logger.error(f"Fallback single-slide encoding also failed: {fb_err}")
            raise RuntimeError(err_msg)

        logger.info(f"Video story encoded successfully in {elapsed:.2f}s -> {output_path} ({os.path.getsize(output_path)} bytes)")

        # 6. Clean up temporary slide & overlay files
        try:
            if os.path.exists(overlay_path):
                os.remove(overlay_path)
            for sp in slide_paths:
                if os.path.exists(sp):
                    os.remove(sp)
        except Exception as cl_err:
            logger.debug(f"Note on temp cleanup: {cl_err}")

        # 7. Maintain preview copy for live inspection
        try:
            preview_copy = os.path.join(temp_dir, "real_realtor_story_preview.mp4")
            with open(output_path, "rb") as src, open(preview_copy, "wb") as dst:
                dst.write(src.read())
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

        return output_path, card_coords

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
        Asynchronously creates a complete 25-second luxury video story using non-blocking FFmpeg.
        Guarantees the asyncio event loop remains 100% responsive.
        """
        temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
        os.makedirs(temp_dir, exist_ok=True)
        timestamp = int(time.time() * 1000)

        # 1. Render transparent card overlay PNG in thread
        overlay_path = os.path.join(temp_dir, f"overlay_{timestamp}.png")
        await asyncio.to_thread(
            story_card_renderer.render_card_overlay_png,
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
        card_coords = story_card_renderer.get_last_card_coordinates()

        # Clamp duration to 15s - 40s (strict Telegram Story standards)
        duration = max(15.0, min(40.0, float(duration or 25.0)))

        # 2. Prepare ambient background slides
        valid_photos = [p for p in photo_paths if p and os.path.exists(p)]
        if not valid_photos:
            raise ValueError("Kamida bitta fotosurat talab qilinadi (video istoriya fotosuratsiz yaratilmaydi)")

        # Reorder photos in worker thread using aesthetic visual scorer: sharpest, most vibrant photo becomes Slide 1!
        from services.aesthetic_scorer import aesthetic_scorer
        valid_photos = await asyncio.to_thread(aesthetic_scorer.reorder_photos_by_aesthetic, valid_photos)

        # Cap ambient background slides to at most 4 photos (best 4 photos)
        # Keeps FFmpeg RAM under 150MB, prevents OOM, and ensures smooth, elegant transitions!
        valid_photos = valid_photos[:4]

        slide_paths = []
        for i, p in enumerate(valid_photos):
            sp = os.path.join(temp_dir, f"slide_{timestamp}_{i}.jpg")
            await asyncio.to_thread(self.create_ambient_slide, p, sp)
            slide_paths.append(sp)

        # 3. Select music track with anti-repetition
        if audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            chosen_audio = audio_path
            self.record_track_usage(chosen_audio, user_id=user_id)
        else:
            chosen_audio = self.get_random_music_track(user_id=user_id)

        # Asynchronously record to persistent DB if db_manager is available
        if chosen_audio:
            try:
                from database.db_manager import db_manager
                try:
                    await db_manager.record_used_story_music(chosen_audio)
                except Exception:
                    asyncio.create_task(db_manager.record_used_story_music(chosen_audio))
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        # Video stories have NO voiceover narration ("gapiradigan narsa olib tashlangan")
        voiceover_path = None
        voice_file_to_clean = None

        # 4. Define final output MP4 path
        if not output_path:
            output_path = os.path.join(temp_dir, f"video_story_{timestamp}.mp4")

        # 5. Build FFmpeg command
        cmd = self.build_ffmpeg_command(
            slide_paths=slide_paths,
            overlay_path=overlay_path,
            audio_path=chosen_audio,
            output_mp4=output_path,
            total_duration=duration,
            xfade_duration=0.8,
            voiceover_path=None,
            volume=volume
        )

        logger.info(f"Async encoding {duration}s video story with {len(slide_paths)} slides and audio {os.path.basename(chosen_audio) if chosen_audio else 'none'}...")
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                proc.kill()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            for sp in slide_paths:
                if os.path.exists(sp):
                    try:
                        os.remove(sp)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
            if os.path.exists(overlay_path):
                try:
                    os.remove(overlay_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            if isinstance(exc, asyncio.TimeoutError):
                raise TimeoutError("FFmpeg async video encoding timed out after 120s")
            raise

        elapsed = time.time() - t0
        if proc.returncode != 0:
            err_text = stderr.decode('utf-8', errors='ignore')[-500:] if stderr else ''
            err_msg = f"FFmpeg video encoding failed (code {proc.returncode}): {err_text}"
            logger.warning(f"{err_msg} - Attempting resilient single-slide audio video fallback...")

            # Resilient fallback: If multi-slide failed, retry with single slide
            if len(slide_paths) > 1 and valid_photos:
                try:
                    fallback_cmd = self.build_ffmpeg_command(
                        slide_paths=[slide_paths[0]],
                        overlay_path=overlay_path,
                        audio_path=chosen_audio,
                        output_mp4=output_path,
                        total_duration=duration,
                        xfade_duration=0.8,
                        voiceover_path=None,
                        volume=volume
                    )
                    proc_fb = await asyncio.create_subprocess_exec(
                        *fallback_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc_fb.communicate(), timeout=30.0)
                    if proc_fb.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                        logger.info(f"Resilient single-slide audio video succeeded -> {output_path} ({os.path.getsize(output_path)} bytes)")
                        if os.path.exists(overlay_path):
                            try: os.remove(overlay_path)
                            except Exception: logger.debug("Ignored exception", exc_info=True)
                        for sp in slide_paths:
                            if os.path.exists(sp):
                                try: os.remove(sp)
                                except Exception: logger.debug("Ignored exception", exc_info=True)
                        if voice_file_to_clean and os.path.exists(voice_file_to_clean):
                            try: os.remove(voice_file_to_clean)
                            except Exception: logger.debug("Ignored exception", exc_info=True)
                        return output_path, card_coords
                except Exception as fb_err:
                    logger.error(f"Fallback single-slide encoding also failed: {fb_err}")

            raise RuntimeError(err_msg)

        logger.info(f"Video story async encoded in {elapsed:.2f}s -> {output_path} ({os.path.getsize(output_path)} bytes)")

        # 6. Clean up temporary slide, overlay, and generated voice files
        try:
            if os.path.exists(overlay_path):
                os.remove(overlay_path)
            for sp in slide_paths:
                if os.path.exists(sp):
                    os.remove(sp)
            if voice_file_to_clean and os.path.exists(voice_file_to_clean):
                os.remove(voice_file_to_clean)
        except Exception as cl_err:
            logger.debug(f"Note on temp cleanup: {cl_err}")

        return output_path, card_coords


story_video_generator = StoryVideoGenerator()
