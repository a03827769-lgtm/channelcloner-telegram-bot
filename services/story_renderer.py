import os
import re
import time
import base64
import html
import logging
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Optional, Dict, Any
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from config.settings import settings

logger = logging.getLogger(__name__)

_PLAYWRIGHT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="playwright_renderer")

TOKEN_PATTERN = re.compile(
    r'(#[A-Za-z0-9_а-яА-ЯёЁ]+|[\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f]+|[^\s#\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f]+|\s+)'
)


class StoryCardRenderer:
    """
    Renders 1080x1920 Telegram Story composite images that replicate
    Telegram's native repost card format with 1:1 pixel accuracy.
    Uses high-performance Pillow (PIL) typography rendering, typographic line
    wrapping, and CSS-style photo collage layouts with thread-safe coordinate tracking.
    """

    def __init__(self):
        self._local = threading.local()
        self._lock = threading.Lock()
        self.last_card_coords: Dict[str, float] = {
            "x": 50.0,
            "y": 50.0,
            "w": 81.5,
            "h": 68.0
        }

    def _set_card_coords(self, coords: Dict[str, float]) -> None:
        """Stores coordinates in thread-local storage and thread-safely in instance state"""
        c = dict(coords)
        self._local.last_card_coords = c
        with self._lock:
            self.last_card_coords = c

    def get_last_card_coordinates(self) -> Dict[str, float]:
        """Returns the exact percentage coordinates of the last rendered card for InputMediaAreaChannelPost"""
        if hasattr(self._local, "last_card_coords"):
            return dict(self._local.last_card_coords)
        with self._lock:
            return dict(self.last_card_coords)

    @classmethod
    def _clean_text_for_rendering(cls, text: str, strip_emoji: bool = False) -> str:
        """Sanitizes text, normalizes fancy mathematical/styled fonts to standard unicode, strips invisible controls and tofu emojis"""
        if not text:
            return ""
        import unicodedata
        # 1. Normalize fancy font styles (Mathematical Bold, Italic, Sans-serif, Fraktur, Fullwidth, etc.)
        normalized = unicodedata.normalize('NFKC', text)
        # 2. Strip non-printable and invisible control chars
        cleaned = re.sub(r'[\u200b-\u200f\ufeff\u202a-\u202e\u2060-\u206f]', '', normalized)
        # 3. Normalize single quotation marks / Uzbek apostrophes
        cleaned = re.sub(r'[`´ʻʼʽ\u02bb\u02bc\u2018\u2019]', "'", cleaned)
        # 4. Strip emojis for raster text rendering (avoids hollow tofu rectangles on Linux)
        if strip_emoji:
            cleaned = re.sub(
                r'[\U00010000-\U0010ffff]|'
                r'[\u2600-\u27bf]|'
                r'[\u2300-\u23ff]|'
                r'[\u2b50\u2b55]|'
                r'[\u203c\u2049\u2139]|'
                r'[\u2194-\u21aa]',
                '',
                cleaned
            )
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        return cleaned.strip()

    @staticmethod
    def _image_to_base64_uri(file_path: Optional[str], max_dim: int = 1200) -> Optional[str]:
        """Reads an image file, downsamples if large, and converts into data URI for fast HTML embedding"""
        if not file_path or not os.path.exists(file_path):
            return None
        try:
            if os.path.getsize(file_path) < 300 * 1024:
                ext = os.path.splitext(file_path)[1].lower().replace('.', '')
                mime = "image/png" if ext == "png" else "image/jpeg"
                with open(file_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("utf-8")
                return f"data:{mime};base64,{encoded}"

            import io
            with Image.open(file_path) as im:
                im_format = im.format or "JPEG"
                mime = "image/png" if im_format == "PNG" else "image/jpeg"
                w, h = im.size
                if max(w, h) > max_dim:
                    scale = max_dim / float(max(w, h))
                    nw, nh = int(w * scale), int(h * scale)
                    im = im.resize((nw, nh), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                if mime == "image/png":
                    im.save(buf, format="PNG", optimize=True)
                else:
                    im.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
                encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
                return f"data:{mime};base64,{encoded}"
        except Exception as e:
            logger.debug(f"Error converting image {file_path} to base64: {e}")
            return None

    @classmethod
    def extract_story_lines(cls, caption: str, price: Optional[float] = None) -> List[str]:
        """
        Faithfully extracts the top 4-5 natural lines from the author's post.
        Preserves original listing content and price while filtering pure link/contact footers.
        """
        if not caption:
            return ["✨ Yangi e'lon"]

        raw_lines = [re.sub(r'[ \t]+', ' ', l).strip() for l in caption.split('\n')]
        non_empty = [cls._clean_text_for_rendering(l) for l in raw_lines if l.strip()]

        cleaned = []
        price_line = None

        for l in non_empty:
            low = l.lower()
            if any(k in low for k in ['цена', 'нарх', 'narxi', 'стоимость', 'ijara']) and any(c.isdigit() for c in l):
                if not price_line:
                    price_line = l
                continue
            # Filter strictly lines that are purely contacts, URLs, or handles without content
            is_contact_only = bool(
                re.match(r'^(?:https?:\/\/|t\.me\/|@)', low) or
                re.match(r'^(?:тел|tel|aloqa|контакт|contact|админ|admin)\s*[:\-–—]?\s*(?:\+?[0-9\s\-\(\)]+|@[a-z0-9_]+)?$', low) or
                re.match(r'^\+?[0-9\s\-\(\)]{7,}$', low)
            )
            if is_contact_only:
                continue
            cleaned.append(l)

        display = []
        for l in cleaned:
            if len(display) >= 4:
                break
            display.append(l)

        if price_line and price_line not in display:
            display.append(price_line)
        elif price:
            if not any('$' in l or 'usd' in l.lower() or 'нарх' in l.lower() or 'cena' in l.lower() or 'narx' in l.lower() for l in display):
                display.append(f"Narxi: ${price:g}")

        if not display and non_empty:
            display = non_empty[:3]

        return display[:5] if display else ["Yangi e'lon"]

    @classmethod
    def _build_caption_html(cls, caption: str, price: Optional[float] = None) -> str:
        """Constructs HTML for caption with native emojis, blue hashtags, and More button"""
        display_lines = cls.extract_story_lines(caption, price)
        raw_lines = [l.strip() for l in caption.split('\n') if l.strip()] if caption else []

        lines_html = []
        for i, line in enumerate(display_lines):
            escaped = html.escape(line, quote=False)
            # Blue hashtags (#example)
            escaped = re.sub(r'(#[A-Za-z0-9_а-яА-ЯёЁ]+)', r'<span class="hashtag">\1</span>', escaped)
            # On the last line, append '... Подробнее' if original text was longer
            if i == len(display_lines) - 1 and len(raw_lines) > len(display_lines):
                escaped += ' ... <span class="more-btn">Подробнее</span>'
            lines_html.append(escaped)

        return '<br>'.join(lines_html)

    @staticmethod
    def _build_badges_html(badges: Optional[List[str]]) -> str:
        """Generates native badge pills for property highlights (district, rooms, luxury tier)"""
        if not badges:
            return ""
        items = []
        for b in badges:
            escaped = html.escape(b)
            cls_name = "badge-pill"
            if "PREMYUM" in b or "💎" in b:
                cls_name += " badge-luxury"
            elif "BIZNES" in b or "⭐" in b:
                cls_name += " badge-business"
            elif "SARA" in b or "🔥" in b:
                cls_name += " badge-sara"
            items.append(f'<span class="{cls_name}">{escaped}</span>')
        return f'<div class="badges-container">{"".join(items)}</div>'

    @staticmethod
    def _build_collage_html(photo_uris: List[str]) -> str:
        """Generates responsive CSS Flexbox collage matching Telegram's exact photo layouts"""
        n = len(photo_uris)
        if n == 0:
            return ""

        if n == 1:
            return f'''
            <div class="collage" style="width: 860px; height: 560px;">
                <img class="collage-img" style="width: 100%; height: 560px;" src="{photo_uris[0]}">
            </div>'''

        if n == 2:
            return f'''
            <div class="collage" style="width: 860px;">
                <div class="collage-row" style="height: 480px;">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 480px;" src="{photo_uris[0]}">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 480px;" src="{photo_uris[1]}">
                </div>
            </div>'''

        if n == 3:
            return f'''
            <div class="collage" style="width: 860px;">
                <div class="collage-row" style="height: 400px;">
                    <img class="collage-img" style="width: 100%; height: 400px;" src="{photo_uris[0]}">
                </div>
                <div class="collage-row" style="height: 280px; margin-top: 2px;">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 280px;" src="{photo_uris[1]}">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 280px;" src="{photo_uris[2]}">
                </div>
            </div>'''

        if n == 4:
            return f'''
            <div class="collage" style="width: 860px;">
                <div class="collage-row" style="height: 340px;">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 340px;" src="{photo_uris[0]}">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 340px;" src="{photo_uris[1]}">
                </div>
                <div class="collage-row" style="height: 340px; margin-top: 2px;">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 340px;" src="{photo_uris[2]}">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 340px;" src="{photo_uris[3]}">
                </div>
            </div>'''

        if n == 5:
            return f'''
            <div class="collage" style="width: 860px;">
                <div class="collage-row" style="height: 360px;">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 360px;" src="{photo_uris[0]}">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 360px;" src="{photo_uris[1]}">
                </div>
                <div class="collage-row" style="height: 260px; margin-top: 2px;">
                    <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 260px;" src="{photo_uris[2]}">
                    <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 260px;" src="{photo_uris[3]}">
                    <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 260px;" src="{photo_uris[4]}">
                </div>
            </div>'''

        if n == 6:
            return f'''
            <div class="collage" style="width: 860px;">
                <div class="collage-row" style="height: 380px;">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 380px;" src="{photo_uris[0]}">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 380px;" src="{photo_uris[1]}">
                </div>
                <div class="collage-row" style="height: 250px; margin-top: 2px;">
                    <img class="collage-img" style="width: calc(25% - 1.5px); height: 250px;" src="{photo_uris[2]}">
                    <img class="collage-img" style="width: calc(25% - 1.5px); height: 250px;" src="{photo_uris[3]}">
                    <img class="collage-img" style="width: calc(25% - 1.5px); height: 250px;" src="{photo_uris[4]}">
                    <img class="collage-img" style="width: calc(25% - 1.5px); height: 250px;" src="{photo_uris[5]}">
                </div>
            </div>'''

        if n == 7:
            return f'''
            <div class="collage" style="width: 860px;">
                <div class="collage-row" style="height: 340px;">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 340px;" src="{photo_uris[0]}">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 340px;" src="{photo_uris[1]}">
                </div>
                <div class="collage-row" style="height: 250px; margin-top: 2px;">
                    <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 250px;" src="{photo_uris[2]}">
                    <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 250px;" src="{photo_uris[3]}">
                    <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 250px;" src="{photo_uris[4]}">
                </div>
                <div class="collage-row" style="height: 250px; margin-top: 2px;">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 250px;" src="{photo_uris[5]}">
                    <img class="collage-img" style="width: calc(50% - 1px); height: 250px;" src="{photo_uris[6]}">
                </div>
            </div>'''

        # 8 or more photos (authentic 2-3-3 grid matching reference)
        return f'''
        <div class="collage" style="width: 860px;">
            <div class="collage-row" style="height: 380px;">
                <img class="collage-img" style="width: calc(50% - 1px); height: 380px;" src="{photo_uris[0]}">
                <img class="collage-img" style="width: calc(50% - 1px); height: 380px;" src="{photo_uris[1]}">
            </div>
            <div class="collage-row" style="height: 260px; margin-top: 2px;">
                <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 260px;" src="{photo_uris[2]}">
                <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 260px;" src="{photo_uris[3]}">
                <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 260px;" src="{photo_uris[4]}">
            </div>
            <div class="collage-row" style="height: 260px; margin-top: 2px;">
                <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 260px;" src="{photo_uris[5]}">
                <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 260px;" src="{photo_uris[6]}">
                <img class="collage-img" style="width: calc(33.333% - 1.33px); height: 260px;" src="{photo_uris[7]}">
            </div>
        </div>'''

    def _execute_playwright(self, fn, *args, **kwargs):
        """Executes Playwright synchronously, safely dispatching to dedicated worker pool if inside an asyncio loop"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            future = _PLAYWRIGHT_POOL.submit(fn, *args, **kwargs)
            return future.result()
        else:
            return fn(*args, **kwargs)

    def render_story_composite_playwright(
        self,
        bg_base_path: Optional[str],
        channel_title: str,
        photo_paths: List[str],
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[str]] = None,
        output_path: Optional[str] = None,
        transparent: bool = False
    ) -> str:
        """Dispatches Playwright rendering safely across synchronous or asynchronous contexts"""
        return self._execute_playwright(
            self._render_playwright_internal,
            bg_base_path=bg_base_path,
            channel_title=channel_title,
            photo_paths=photo_paths,
            caption=caption,
            price=price,
            date_str=date_str,
            avatar_path=avatar_path,
            forward_title=forward_title,
            badges=badges,
            output_path=output_path,
            transparent=transparent
        )

    async def render_story_composite_playwright_async(
        self,
        bg_base_path: Optional[str],
        channel_title: str,
        photo_paths: List[str],
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[str]] = None,
        output_path: Optional[str] = None,
        transparent: bool = False
    ) -> str:
        """Asynchronously executes Playwright rendering in dedicated thread pool without blocking event loop"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _PLAYWRIGHT_POOL,
            lambda: self._render_playwright_internal(
                bg_base_path=bg_base_path,
                channel_title=channel_title,
                photo_paths=photo_paths,
                caption=caption,
                price=price,
                date_str=date_str,
                avatar_path=avatar_path,
                forward_title=forward_title,
                badges=badges,
                output_path=output_path,
                transparent=transparent
            )
        )

    def _render_playwright_internal(
        self,
        bg_base_path: Optional[str],
        channel_title: str,
        photo_paths: List[str],
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[str]] = None,
        output_path: Optional[str] = None,
        transparent: bool = False
    ) -> str:
        """
        Internal implementation: Renders the story using Playwright Chromium for 100% native Telegram styling.
        Guarantees flawless emoji alignment, typography, and precise bounding boxes.
        When transparent=True, renders without background as RGBA PNG for video overlay.
        """
        from playwright.sync_api import sync_playwright

        bg_uri = self._image_to_base64_uri(bg_base_path) if bg_base_path else None
        if not bg_uri and not transparent:
            # Fallback inline SVG green background
            bg_uri = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='1080' height='1920'><rect width='1080' height='1920' fill='%2387b878'/></svg>"

        avatar_uri = self._image_to_base64_uri(avatar_path)
        if not avatar_uri:
            # Fallback avatar badge with initial letter
            initial = (channel_title[:1] if channel_title else "A").upper()
            avatar_uri = f"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='72' height='72'><circle cx='36' cy='36' r='36' fill='%238f327e'/><text x='36' y='46' font-size='32' font-family='sans-serif' font-weight='bold' fill='white' text-anchor='middle'>{initial}</text></svg>"

        photo_uris = []
        for p in photo_paths:
            uri = self._image_to_base64_uri(p)
            if uri:
                photo_uris.append(uri)

        collage_html = self._build_collage_html(photo_uris)
        caption_html = self._build_caption_html(caption, price)
        badges_html = self._build_badges_html(badges)
        clean_title = html.escape(self._clean_text_for_rendering(channel_title or "ARENDA UY"))

        forward_html = ""
        if forward_title:
            fwd_escaped = html.escape(self._clean_text_for_rendering(forward_title))
            forward_html = f'<div class="forward-info">Переслано от <span class="forward-channel">{fwd_escaped}</span></div>'

        time_text = html.escape(self._clean_text_for_rendering(date_str or time.strftime("%d сен, %H:%M")))

        body_bg_css = "background: transparent !important;" if transparent else f"background: url('{bg_uri}') no-repeat center center; background-size: cover;"
        card_shadow = "filter: drop-shadow(0 20px 40px rgba(0, 0, 0, 0.45));" if transparent else "filter: drop-shadow(0 16px 36px rgba(0, 0, 0, 0.22));"

        html_content = f'''<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{
    width: 1080px;
    height: 1920px;
    {body_bg_css}
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    font-family: "Segoe UI", "Arial", "Roboto", "Tahoma", "Helvetica Neue", "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
    overflow: hidden;
}}
.card-outer {{
    position: relative;
    width: 860px;
    margin-left: 70px;
    {card_shadow}
}}
.card {{
    background: #ffffff;
    border-radius: 24px;
    overflow: hidden;
}}
.header {{
    padding: 18px 24px 14px 24px;
}}
.channel-title {{
    color: #8f327e;
    font-size: 28px;
    font-weight: 700;
    letter-spacing: -0.2px;
}}
.forward-info {{
    color: #8e8e93;
    font-size: 21px;
    margin-top: 4px;
}}
.forward-channel {{
    color: #c67d32;
    font-weight: 600;
}}
.collage {{
    width: 860px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    background: #ffffff;
}}
.collage-row {{
    display: flex;
    gap: 2px;
    width: 100%;
}}
.collage-img {{
    object-fit: cover;
    display: block;
}}
.caption-box {{
    padding: 20px 24px 18px 24px;
}}
.badges-container {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 14px;
}}
.badge-pill {{
    background: #f0f2f5;
    color: #2c3e50;
    font-size: 20px;
    font-weight: 600;
    padding: 4px 14px;
    border-radius: 12px;
    display: inline-flex;
    align-items: center;
    letter-spacing: -0.2px;
}}
.badge-luxury {{
    background: linear-gradient(135deg, #fff8e1, #ffecb3);
    color: #9c6500;
    border: 1.5px solid #ffd54f;
    font-weight: 700;
}}
.badge-business {{
    background: #e3f2fd;
    color: #1565c0;
    border: 1.5px solid #90caf9;
    font-weight: 700;
}}
.badge-sara {{
    background: #fbe9e7;
    color: #d84315;
    border: 1.5px solid #ffab91;
    font-weight: 700;
}}
.caption-text {{
    font-size: 25px;
    line-height: 1.42;
    color: #000000;
    word-break: break-word;
}}
.hashtag {{
    color: #2481cc;
    font-weight: 500;
}}
.more-btn {{
    color: #2481cc;
    font-weight: 500;
}}
.footer {{
    display: flex;
    justify-content: flex-end;
    align-items: center;
    margin-top: 8px;
}}
.timestamp {{
    color: #8e8e93;
    font-size: 19px;
}}
.tail-svg {{
    position: absolute;
    bottom: 0px;
    left: -27px;
    z-index: 10;
}}
.avatar-badge {{
    position: absolute;
    bottom: -6px;
    left: -96px;
    width: 84px;
    height: 84px;
    border-radius: 50%;
    border: 3.5px solid #ffffff;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18);
    z-index: 20;
    object-fit: cover;
    background: #ffffff;
}}
</style>
</head>
<body>
<div class="card-outer" id="card-outer">
    <div class="card" id="card">
        <div class="header">
            <div class="channel-title">{clean_title}</div>
            {forward_html}
        </div>
        {collage_html}
        <div class="caption-box">
            {badges_html}
            <div class="caption-text">
                {caption_html}
            </div>
            <div class="footer">
                <span class="timestamp">{time_text}</span>
            </div>
        </div>
    </div>
    <svg class="tail-svg" width="28" height="24" viewBox="0 0 28 24">
        <path d="M 28 0 C 26 12 14 20 0 24 L 28 24 Z" fill="#ffffff"/>
    </svg>
    <img class="avatar-badge" src="{avatar_uri}">
</div>
</body>
</html>'''

        if not output_path:
            temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
            os.makedirs(temp_dir, exist_ok=True)
            output_path = os.path.join(temp_dir, f"story_card_{int(time.time() * 1000)}.jpg")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )
            try:
                page = browser.new_page(viewport={'width': 1080, 'height': 1920})
                page.set_content(html_content, wait_until="domcontentloaded", timeout=15000)

                card_el = page.query_selector('#card')
                if card_el:
                    box = card_el.bounding_box()
                    if box:
                        center_x_pct = (box['x'] + box['width'] / 2.0) / 1080.0 * 100.0
                        center_y_pct = (box['y'] + box['height'] / 2.0) / 1920.0 * 100.0
                        w_pct = box['width'] / 1080.0 * 100.0
                        h_pct = box['height'] / 1920.0 * 100.0
                        self._set_card_coords({
                            "x": round(center_x_pct, 1),
                            "y": round(center_y_pct, 1),
                            "w": round(w_pct, 1),
                            "h": round(h_pct, 1)
                        })

                if transparent:
                    page.screenshot(path=output_path, omit_background=True, type='png', timeout=15000)
                else:
                    page.screenshot(path=output_path, quality=95, type='jpeg', timeout=15000)
            finally:
                if 'page' in locals() and page:
                    try:
                        page.close()
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                try:
                    browser.close()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

        # Keep a persistent preview copy for real-time inspection
        try:
            preview_dir = os.path.dirname(output_path)
            preview_file = os.path.join(preview_dir, "real_realtor_story_preview.jpg")
            with open(output_path, 'rb') as fsrc, open(preview_file, 'wb') as fdst:
                fdst.write(fsrc.read())
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

        logger.info(f"Story composite card rendered via Playwright: {output_path} (coords: {self.last_card_coords})")
        return output_path

    # ==========================================
    # --- RESILIENT PILLOW (PIL) FALLBACK ---
    # ==========================================

    @staticmethod
    def _fit_and_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
        """Scales and center-crops an image to exact dimensions"""
        sw, sh = img.size
        scale = max(target_w / sw, target_h / sh)
        nw, nh = int(sw * scale), int(sh * scale)
        res = img.resize((nw, nh), Image.Resampling.LANCZOS)
        cx, cy = (nw - target_w) // 2, (nh - target_h) // 2
        return res.crop((cx, cy, cx + target_w, cy + target_h))

    @classmethod
    def create_photo_collage(cls, photo_paths: List[str], width: int = 880, height: int = 920) -> Optional[Image.Image]:
        """Creates an authentic Telegram-style photo collage matching the reference layout. Returns None if no valid photos."""
        valid_imgs = []
        for p in photo_paths:
            if p and os.path.exists(p):
                try:
                    valid_imgs.append(Image.open(p).convert('RGB'))
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

        n = len(valid_imgs)
        gap = 2

        if n == 0:
            return None

        collage = Image.new('RGB', (width, height), (255, 255, 255))

        if n == 1:
            return cls._fit_and_crop(valid_imgs[0], width, height)

        if n == 2:
            col_w = (width - gap) // 2
            collage.paste(cls._fit_and_crop(valid_imgs[0], col_w, height), (0, 0))
            collage.paste(cls._fit_and_crop(valid_imgs[1], col_w, height), (col_w + gap, 0))
            return collage

        if n == 3:
            h1 = int(height * 0.55)
            h2 = height - h1 - gap
            col_w = (width - gap) // 2
            collage.paste(cls._fit_and_crop(valid_imgs[0], width, h1), (0, 0))
            collage.paste(cls._fit_and_crop(valid_imgs[1], col_w, h2), (0, h1 + gap))
            collage.paste(cls._fit_and_crop(valid_imgs[2], col_w, h2), (col_w + gap, h1 + gap))
            return collage

        if n == 4:
            col_w = (width - gap) // 2
            row_h = (height - gap) // 2
            collage.paste(cls._fit_and_crop(valid_imgs[0], col_w, row_h), (0, 0))
            collage.paste(cls._fit_and_crop(valid_imgs[1], col_w, row_h), (col_w + gap, 0))
            collage.paste(cls._fit_and_crop(valid_imgs[2], col_w, row_h), (0, row_h + gap))
            collage.paste(cls._fit_and_crop(valid_imgs[3], col_w, row_h), (col_w + gap, row_h + gap))
            return collage

        # 5 or more photos
        h1 = int(height * 0.42)
        h2 = int(height * 0.29)
        h3 = height - h1 - h2 - (2 * gap)

        col_w2 = (width - gap) // 2
        collage.paste(cls._fit_and_crop(valid_imgs[0], col_w2, h1), (0, 0))
        collage.paste(cls._fit_and_crop(valid_imgs[1], col_w2, h1), (col_w2 + gap, 0))

        col_w3 = (width - 2 * gap) // 3
        y2 = h1 + gap
        collage.paste(cls._fit_and_crop(valid_imgs[2], col_w3, h2), (0, y2))
        collage.paste(cls._fit_and_crop(valid_imgs[3], col_w3, h2), (col_w3 + gap, y2))
        collage.paste(cls._fit_and_crop(valid_imgs[4], col_w3, h2), (2 * (col_w3 + gap), y2))

        rem_count = min(n - 5, 3)
        if rem_count > 0:
            col_w_rem = (width - (rem_count - 1) * gap) // rem_count
            y3 = y2 + h2 + gap
            for idx in range(rem_count):
                x = idx * (col_w_rem + gap)
                collage.paste(cls._fit_and_crop(valid_imgs[5 + idx], col_w_rem, h3), (x, y3))

        return collage

    @classmethod
    def _get_system_font(cls, size: int, bold: bool = False) -> ImageFont.ImageFont:
        """Finds the best available TrueType font on Windows or Linux with Cyrillic & Latin support"""
        font_dir = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts')
        candidates = [
            os.path.join(font_dir, 'segoeuib.ttf' if bold else 'segoeui.ttf'),
            os.path.join(font_dir, 'arialbd.ttf' if bold else 'arial.ttf'),
            os.path.join(font_dir, 'tahomabd.ttf' if bold else 'tahoma.ttf'),
            os.path.join(font_dir, 'calibrib.ttf' if bold else 'calibri.ttf'),
            f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
            f"/usr/share/fonts/truetype/liberation/LiberationSans{'-Bold' if bold else '-Regular'}.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
        ]
        for p in candidates:
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    continue
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    @classmethod
    def _create_gradient_background(cls, style: str, w: int = 1080, h: int = 1920) -> Image.Image:
        """Creates an ultra-clean vertical gradient for story backgrounds in pure PIL"""
        style_gradients = {
            'telegram_green': ((32, 65, 60), (14, 32, 29)),
            'luxury_dark': ((26, 31, 40), (10, 12, 16)),
            'emerald': ((6, 78, 59), (2, 44, 34)),
        }
        top_rgb, bot_rgb = style_gradients.get(style, style_gradients['telegram_green'])
        col = Image.new('RGB', (1, h))
        pixels = []
        for y in range(h):
            t = y / max(1, h - 1)
            r = int(top_rgb[0] * (1 - t) + bot_rgb[0] * t)
            g = int(top_rgb[1] * (1 - t) + bot_rgb[1] * t)
            b = int(top_rgb[2] * (1 - t) + bot_rgb[2] * t)
            pixels.append((r, g, b))
        col.putdata(pixels)
        return col.resize((w, h), Image.Resampling.BILINEAR).convert('RGBA')

    @classmethod
    def _draw_badges_on_collage(cls, collage: Image.Image, badges: Optional[List[Any]], font: ImageFont.ImageFont):
        """Renders frosted luxury pill tags with colored glowing status dots on top of the collage"""
        if not badges:
            return
        draw = ImageDraw.Draw(collage, 'RGBA')
        x_offset = 24
        y_offset = 24

        color_map = {
            'district': (56, 189, 248),   # Cyan
            'rooms': (192, 132, 252),      # Purple
            'area': (251, 191, 36),        # Amber
            'price': (52, 211, 153),       # Emerald
            'default': (255, 255, 255)
        }

        for b in badges:
            if isinstance(b, dict):
                b_text = b.get('text', '')
                b_type = b.get('type', 'default')
            else:
                b_text = str(b)
                b_type = 'default'

            clean_text = cls._clean_text_for_rendering(b_text, strip_emoji=True).strip().upper()
            if not clean_text:
                continue

            dot_color = color_map.get(b_type, color_map['default'])

            bbox = font.getbbox(clean_text)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            dot_r = 4
            dot_gap = 10
            pad_x = 16
            pad_y = 10

            chip_w = pad_x + (dot_r * 2) + dot_gap + text_w + pad_x
            chip_h = (pad_y * 2) + text_h

            if x_offset + chip_w > collage.width - 24:
                x_offset = 24
                y_offset += chip_h + 10
                if y_offset + chip_h > collage.height - 24:
                    break

            # Draw chip background (dark frosted pill with subtle border)
            draw.rounded_rectangle(
                [x_offset, y_offset, x_offset + chip_w, y_offset + chip_h],
                radius=12,
                fill=(15, 23, 42, 215),
                outline=(255, 255, 255, 75),
                width=1
            )

            # Draw glowing status dot
            dot_cx = x_offset + pad_x + dot_r
            dot_cy = y_offset + (chip_h // 2)
            draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=dot_color)

            # Draw text
            text_x = dot_cx + dot_r + dot_gap
            text_y = y_offset + pad_y - bbox[1]
            draw.text((text_x, text_y), clean_text, font=font, fill=(255, 255, 255, 245))

            x_offset += chip_w + 12

    def render_story_composite_pil(
        self,
        bg_base_path: Optional[str],
        channel_title: str,
        photo_paths: List[str],
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[Any]] = None,
        output_path: Optional[str] = None,
        transparent: bool = False
    ) -> str:
        """Pillow fallback implementation if Playwright is unavailable with badges and gradients"""
        target_w, target_h = 1080, 1920

        if transparent:
            bg_rgba = Image.new('RGBA', (target_w, target_h), (0, 0, 0, 0))
        elif bg_base_path and os.path.isfile(bg_base_path):
            try:
                bg = Image.open(bg_base_path).convert('RGB')
                if bg.size != (target_w, target_h):
                    bg = bg.resize((target_w, target_h), Image.Resampling.LANCZOS)
                bg_rgba = bg.convert('RGBA')
            except Exception:
                bg_rgba = self._create_gradient_background('telegram_green', target_w, target_h)
        elif bg_base_path == 'listing_blur' and photo_paths and os.path.exists(photo_paths[0]):
            try:
                im = Image.open(photo_paths[0]).convert('RGB')
                im_ratio = im.width / im.height
                target_ratio = target_w / target_h
                if im_ratio > target_ratio:
                    new_w = int(im.height * target_ratio)
                    left = (im.width - new_w) // 2
                    im = im.crop((left, 0, left + new_w, im.height))
                else:
                    new_h = int(im.width / target_ratio)
                    top = (im.height - new_h) // 2
                    im = im.crop((0, top, im.width, top + new_h))
                im = im.resize((target_w, target_h), Image.Resampling.BILINEAR)
                im = im.filter(ImageFilter.GaussianBlur(35))
                im = ImageEnhance.Brightness(im).enhance(0.7)
                bg_rgba = im.convert('RGBA')
            except Exception:
                bg_rgba = self._create_gradient_background('telegram_green', target_w, target_h)
        else:
            style_name = bg_base_path if bg_base_path in ['luxury_dark', 'emerald', 'telegram_green'] else 'telegram_green'
            bg_rgba = self._create_gradient_background(style_name, target_w, target_h)

        font_title = self._get_system_font(28, bold=True)
        font_sub = self._get_system_font(20, bold=False)
        font_text = self._get_system_font(24, bold=False)
        font_time = self._get_system_font(20, bold=False)
        font_badge = self._get_system_font(18, bold=True)

        card_w = 880
        card_r = 24

        collage = self.create_photo_collage(photo_paths, width=card_w, height=920)
        has_collage = collage is not None
        collage_h = 920 if has_collage else 0

        raw_display = self.extract_story_lines(caption, price)
        display_lines = [self._clean_text_for_rendering(l, strip_emoji=True) for l in raw_display if l.strip()]
        display_lines = [l for l in display_lines if l]

        if not has_collage:
            font_text = self._get_system_font(28, bold=False)
            line_h = 42
            caption_padding_top = 28
            caption_padding_bottom = 32
            header_h = 96 if forward_title else 68
        else:
            font_text = self._get_system_font(24, bold=False)
            line_h = 34
            caption_padding_top = 20
            caption_padding_bottom = 24
            header_h = 86 if forward_title else 58

        caption_h = caption_padding_top + (len(display_lines) * line_h) + caption_padding_bottom
        card_h = header_h + collage_h + caption_h
        if not has_collage:
            card_h = max(card_h, 340)

        card_left = (target_w - card_w) // 2
        card_top = (target_h - card_h) // 2

        if has_collage and badges:
            self._draw_badges_on_collage(collage, badges, font_badge)

        card_img = Image.new('RGBA', (card_w, card_h), (0, 0, 0, 0))
        card_draw = ImageDraw.Draw(card_img)
        card_draw.rounded_rectangle([0, 0, card_w, card_h], radius=card_r, fill=(255, 255, 255, 255))

        c_title_clean = self._clean_text_for_rendering(channel_title or "ARENDA UY", strip_emoji=True)
        card_draw.text((32, 16 if not forward_title else 14), c_title_clean, font=font_title, fill=(155, 61, 125))
        if forward_title:
            fwd_clean = self._clean_text_for_rendering(forward_title, strip_emoji=True)
            card_draw.text((32, 52), f"Manba: {fwd_clean}", font=font_sub, fill=(198, 125, 50))

        if has_collage:
            card_img.paste(collage, (0, header_h))

        y_text = header_h + collage_h + caption_padding_top
        for idx, line in enumerate(display_lines):
            card_draw.text((32, y_text), line, font=font_text, fill=(28, 28, 30))
            y_text += line_h

        now_dt = datetime.now()
        m_names = ['yan', 'fev', 'mar', 'apr', 'may', 'iyn', 'iyl', 'avg', 'sen', 'okt', 'noy', 'dek']
        default_date = f"{now_dt.day} {m_names[now_dt.month - 1]}, {now_dt.strftime('%H:%M')}"
        d_text = self._clean_text_for_rendering(date_str or default_date, strip_emoji=True)
        w_date = font_time.getbbox(d_text)[2] - font_time.getbbox(d_text)[0]
        card_draw.text((card_w - w_date - 32, card_h - 36), d_text, font=font_time, fill=(142, 142, 147))

        shadow = Image.new('RGBA', (card_w + 30, card_h + 30), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle([15, 15, card_w + 15, card_h + 15], radius=card_r, fill=(0, 0, 0, 45))
        shadow = shadow.filter(ImageFilter.GaussianBlur(12))

        bg_rgba.paste(shadow, (card_left - 15, card_top - 8), shadow)

        card_mask = Image.new('L', (card_w, card_h), 0)
        ImageDraw.Draw(card_mask).rounded_rectangle([0, 0, card_w, card_h], radius=card_r, fill=255)
        bg_rgba.paste(card_img, (card_left, card_top), card_mask)

        tail_poly = [
            (card_left + 14, card_top + card_h - 22),
            (card_left - 10, card_top + card_h - 10),
            (card_left + 28, card_top + card_h)
        ]
        ImageDraw.Draw(bg_rgba).polygon(tail_poly, fill=(255, 255, 255, 255))

        if avatar_path and os.path.exists(avatar_path):
            try:
                av = Image.open(avatar_path).convert('RGBA').resize((68, 68), Image.Resampling.LANCZOS)
                mask = Image.new('L', (68, 68), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, 68, 68), fill=255)
                av_bordered = Image.new('RGBA', (74, 74), (0, 0, 0, 0))
                ImageDraw.Draw(av_bordered).ellipse((0, 0, 74, 74), fill=(255, 255, 255, 255))
                av_bordered.paste(av, (3, 3), mask)
                bg_rgba.paste(av_bordered, (card_left - 20, card_top + card_h - 48), av_bordered)
            except Exception as av_err:
                logger.debug(f"Avatar paste note: {av_err}")
        else:
            initial = (c_title_clean[:1] if c_title_clean else "A").upper()
            av_bordered = Image.new('RGBA', (74, 74), (0, 0, 0, 0))
            av_draw = ImageDraw.Draw(av_bordered)
            av_draw.ellipse((0, 0, 74, 74), fill=(255, 255, 255, 255))
            av_draw.ellipse((3, 3, 71, 71), fill=(143, 50, 126, 255))
            font_av = self._get_system_font(30, bold=True)
            bbox_av = font_av.getbbox(initial)
            av_w = bbox_av[2] - bbox_av[0]
            av_h = bbox_av[3] - bbox_av[1]
            av_draw.text(((74 - av_w) // 2, (74 - av_h) // 2 - 2), initial, font=font_av, fill=(255, 255, 255, 255))
            bg_rgba.paste(av_bordered, (card_left - 20, card_top + card_h - 48), av_bordered)

        if not output_path:
            temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
            os.makedirs(temp_dir, exist_ok=True)
            output_path = os.path.join(temp_dir, f"story_card_{int(time.time() * 1000)}.jpg" if not transparent else f"card_overlay_{int(time.time() * 1000)}.png")

        if transparent or (output_path and output_path.lower().endswith('.png')):
            bg_rgba.save(output_path, format='PNG')
        else:
            out_res = bg_rgba.convert('RGB')
            out_res.save(output_path, format='JPEG', quality=95)

        center_y_pct = (card_top + card_h / 2.0) / float(target_h) * 100.0
        h_pct = card_h / float(target_h) * 100.0
        w_pct = float(card_w) / float(target_w) * 100.0
        self._set_card_coords({
            "x": 50.0,
            "y": round(center_y_pct, 1),
            "w": round(w_pct, 1),
            "h": round(h_pct, 1)
        })

        logger.info(f"Story composite card rendered via PIL fallback: {output_path} (coords: {self.last_card_coords})")
        return output_path

    # ==========================================
    # --- PRIMARY ENTRY POINTS ---
    # ==========================================

    def render_card_overlay_png(
        self,
        channel_title: str,
        photo_paths: List[str],
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[str]] = None,
        output_path: Optional[str] = None
    ) -> str:
        """
        Renders the repost card as a 1080x1920 transparent PNG overlay with
        natural drop shadow, tail, avatar badge, and smart property tags.
        Designed specifically for compositing onto 25s video slideshows via FFmpeg.
        """
        if not output_path:
            temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
            os.makedirs(temp_dir, exist_ok=True)
            output_path = os.path.join(temp_dir, f"card_overlay_{int(time.time() * 1000)}.png")

        if getattr(settings, "ENABLE_PLAYWRIGHT", True):
            try:
                return self.render_story_composite_playwright(
                    bg_base_path=None,
                    channel_title=channel_title,
                    photo_paths=photo_paths,
                    caption=caption,
                    price=price,
                    date_str=date_str,
                    avatar_path=avatar_path,
                    forward_title=forward_title,
                    badges=badges,
                    output_path=output_path,
                    transparent=True
                )
            except Exception as pw_err:
                logger.warning(f"Playwright overlay rendering failed ({pw_err}), using PIL fallback.")
        else:
            logger.info("Playwright disabled via settings (ENABLE_PLAYWRIGHT=False); using PIL overlay renderer directly.")

        return self.render_story_composite_pil(
            bg_base_path=None,
            channel_title=channel_title,
            photo_paths=photo_paths,
            caption=caption,
            price=price,
            date_str=date_str,
            avatar_path=avatar_path,
            forward_title=forward_title,
            badges=badges,
            output_path=output_path,
            transparent=True
        )

    def render_story_composite(
        self,
        bg_base_path: str,
        channel_title: str,
        photo_paths: List[str],
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[str]] = None,
        output_path: Optional[str] = None
    ) -> str:
        """
        Primary entry point for static stories: Attempts Playwright rendering first for 100% native Telegram fidelity,
        and automatically falls back to Pillow if Playwright fails, is unavailable, or disabled for memory optimization.
        """
        if getattr(settings, "ENABLE_PLAYWRIGHT", True):
            try:
                return self.render_story_composite_playwright(
                    bg_base_path=bg_base_path,
                    channel_title=channel_title,
                    photo_paths=photo_paths,
                    caption=caption,
                    price=price,
                    date_str=date_str,
                    avatar_path=avatar_path,
                    forward_title=forward_title,
                    badges=badges,
                    output_path=output_path,
                    transparent=False
                )
            except Exception as pw_err:
                logger.warning(f"Playwright rendering failed ({pw_err}), falling back to PIL renderer.")
        else:
            logger.info("Playwright disabled via settings (ENABLE_PLAYWRIGHT=False); using PIL story renderer directly.")

        return self.render_story_composite_pil(
            bg_base_path=bg_base_path,
            channel_title=channel_title,
            photo_paths=photo_paths,
            caption=caption,
            price=price,
            date_str=date_str,
            avatar_path=avatar_path,
            forward_title=forward_title,
            badges=badges,
            output_path=output_path,
            transparent=False
        )

    async def render_story_composite_async(
        self,
        bg_base_path: str,
        channel_title: str,
        photo_paths: List[str],
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[str]] = None,
        output_path: Optional[str] = None
    ) -> str:
        """Non-blocking async wrapper executing composite rendering in a worker thread"""
        return await asyncio.to_thread(
            self.render_story_composite,
            bg_base_path=bg_base_path,
            channel_title=channel_title,
            photo_paths=photo_paths,
            caption=caption,
            price=price,
            date_str=date_str,
            avatar_path=avatar_path,
            forward_title=forward_title,
            badges=badges,
            output_path=output_path
        )

    async def render_card_overlay_png_async(
        self,
        channel_title: str,
        photo_paths: List[str],
        caption: str,
        price: Optional[float] = None,
        date_str: Optional[str] = None,
        avatar_path: Optional[str] = None,
        forward_title: Optional[str] = None,
        badges: Optional[List[str]] = None,
        output_path: Optional[str] = None
    ) -> str:
        """Non-blocking async wrapper executing card overlay rendering in a worker thread"""
        return await asyncio.to_thread(
            self.render_card_overlay_png,
            channel_title=channel_title,
            photo_paths=photo_paths,
            caption=caption,
            price=price,
            date_str=date_str,
            avatar_path=avatar_path,
            forward_title=forward_title,
            badges=badges,
            output_path=output_path
        )


story_card_renderer = StoryCardRenderer()


def shutdown_playwright_pool(wait: bool = False, cancel_futures: bool = True) -> None:
    """Cleanly shuts down the dedicated Playwright thread pool during application stop"""
    global _PLAYWRIGHT_POOL
    try:
        if _PLAYWRIGHT_POOL:
            _PLAYWRIGHT_POOL.shutdown(wait=wait, cancel_futures=cancel_futures)
            logger.info("Playwright renderer thread pool shut down cleanly.")
    except Exception as e:
        logger.warning(f"Error shutting down Playwright thread pool: {e}")
