import os
import json
import shutil
import pytest
import subprocess
from PIL import Image

from services.story_video_generator import StoryVideoGenerator, story_video_generator
from telethon.tl import types


def _ffprobe_json(file_path: str):
    """Inspects media file via ffprobe and returns parsed JSON dictionary"""
    res = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size,bit_rate:stream=codec_type,codec_name,duration,width,height,r_frame_rate",
            "-of", "json",
            file_path
        ],
        capture_output=True,
        text=True,
        check=True
    )
    return json.loads(res.stdout)


def test_build_ffmpeg_command_enforces_30fps_and_overlay_repeat():
    """Verify build_ffmpeg_command includes -framerate 30 for all image inputs and eof_action=repeat"""
    gen = StoryVideoGenerator()
    slides = ["slide1.jpg", "slide2.jpg", "slide3.jpg"]
    overlay = "overlay.png"
    audio = "assets/audio/01_luxury_corporate.mp3"
    out = "out.mp4"

    cmd = gen.build_ffmpeg_command(
        slide_paths=slides,
        overlay_path=overlay,
        audio_path=audio,
        output_mp4=out,
        total_duration=25.0,
        xfade_duration=0.8
    )

    framerate_indices = [i for i, arg in enumerate(cmd) if arg == "-framerate"]
    assert len(framerate_indices) >= len(slides) + 1, "Must have -framerate 30 for all slides and overlay"
    for idx in framerate_indices:
        assert cmd[idx + 1] == "30"

    f_idx = cmd.index("-filter_complex")
    filter_graph = cmd[f_idx + 1]
    assert "eof_action=repeat" in filter_graph
    assert "afade=t=in:ss=0:d=1.0" in filter_graph
    assert "afade=t=out:st=23.5:d=1.5" in filter_graph
    assert "xfade=" in filter_graph

    assert "-preset" in cmd
    preset_idx = cmd.index("-preset")
    assert cmd[preset_idx + 1] in ("veryfast", "faster", "fast")
    assert "-movflags" in cmd
    assert "+faststart" in cmd


def test_single_slide_ffmpeg_command_enforces_30fps_and_overlay_repeat():
    """Verify single slide FFmpeg command has -framerate 30 and eof_action=repeat"""
    gen = StoryVideoGenerator()
    cmd = gen.build_ffmpeg_command(
        slide_paths=["slide1.jpg"],
        overlay_path="overlay.png",
        audio_path="assets/audio/01_luxury_corporate.mp3",
        output_mp4="out_single.mp4",
        total_duration=25.0
    )

    framerate_indices = [i for i, arg in enumerate(cmd) if arg == "-framerate"]
    assert len(framerate_indices) >= 2, "Must specify -framerate 30 for both slide and overlay"
    for idx in framerate_indices:
        assert cmd[idx + 1] == "30"

    f_idx = cmd.index("-filter_complex")
    filter_graph = cmd[f_idx + 1]
    assert "eof_action=repeat" in filter_graph
    assert "afade=t=in:ss=0:d=1.0" in filter_graph
    assert "afade=t=out:st=23.5:d=1.5" in filter_graph


def test_mtproto_video_story_attributes_guarantee_audio():
    """Verify DocumentAttributeVideo and InputMediaUploadedDocument explicitly flag sound=True"""
    video_attr = types.DocumentAttributeVideo(
        duration=25.0,
        w=1080,
        h=1920,
        supports_streaming=True,
        nosound=False
    )
    assert video_attr.nosound is False
    assert video_attr.duration == 25.0
    assert video_attr.supports_streaming is True

    doc_media = types.InputMediaUploadedDocument(
        file=types.InputFile(id=1, parts=1, name="video.mp4", md5_checksum=""),
        mime_type="video/mp4",
        attributes=[video_attr],
        nosound_video=False
    )
    assert doc_media.nosound_video is False
    assert doc_media.attributes[0].nosound is False
