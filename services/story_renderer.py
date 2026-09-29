import os
import re
import time
import uuid
import base64
import html
import logging
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from config.settings import settings
from services.listing_analyzer import format_price_usd

logger = logging.getLogger(__name__)

# Tashkent has no DST: a fixed offset avoids a tzdata dependency on Windows / slim images
TASHKENT_TZ = timezone(timedelta(hours=5))
RU_MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

# Dedicated Chromium workers: every Playwright render runs on one of these threads (whatever thread asked for it),
# so at most _PLAYWRIGHT_MAX_WORKERS browsers exist and each worker reuses its own browser between cards.
_PLAYWRIGHT_MAX_WORKERS = 2
_PLAYWRIGHT_THREAD_PREFIX = "playwright_renderer"
_PLAYWRIGHT_POOL = ThreadPoolExecutor(max_workers=_PLAYWRIGHT_MAX_WORKERS, thread_name_prefix=_PLAYWRIGHT_THREAD_PREFIX)
_PLAYWRIGHT_RENDER_TIMEOUT = 90.0
# A browser idle for longer than this is restarted before the next render (guards against zombie browsers)
_PLAYWRIGHT_BROWSER_MAX_IDLE = 600.0
_pw_local = threading.local()

# Photos are decoded at most at this size (the collage is 880x920); full 12 MP decodes cost ~36 MB each
_COLLAGE_MAX_PHOTOS = 8
_DEFAULT_CARD_COORDS: Dict[str, float] = {"x": 50.0, "y": 50.0, "w": 81.5, "h": 68.0}

TOKEN_PATTERN = re.compile(
    r'(#[A-Za-z0-9_а-яА-ЯёЁ]+|[\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f]+|[^\s#\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f]+|\s+)'
)


def tashkent_date_label(dt: Optional[datetime] = None) -> str:
    """'29 сен, 14:05' in Tashkent time (naive datetimes are treated as UTC, like Telethon message dates)"""
    if dt is None:
        dt = datetime.now(TASHKENT_TZ)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(TASHKENT_TZ)
    else:
        dt = dt.astimezone(TASHKENT_TZ)
    return f"{dt.day} {RU_MONTHS[dt.month - 1]}, {dt.strftime('%H:%M')}"


def _temp_media_dir() -> str:
    temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def _close_thread_browser() -> None:
    """Closes the Chromium instance owned by the current Playwright worker thread"""
    browser = getattr(_pw_local, "browser", None)
    pw = getattr(_pw_local, "playwright", None)
    _pw_local.browser = None
    _pw_local.playwright = None
    if browser is not None:
        try:
            browser.close()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)


def _get_thread_browser():
    """Returns this worker thread's Chromium, launching (or relaunching a stale / dead) one when needed"""
    browser = getattr(_pw_local, "browser", None)
    if browser is not None:
        idle = time.time() - getattr(_pw_local, "last_used", 0.0)
        try:
            alive = browser.is_connected()
        except Exception:
            alive = False
        if alive and idle < _PLAYWRIGHT_BROWSER_MAX_IDLE:
            return browser
        _close_thread_browser()

    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
        )
    except Exception:
        try:
            pw.stop()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        raise
    _pw_local.playwright = pw
    _pw_local.browser = browser
    _pw_local.last_used = time.time()
    return browser


class StoryCardRenderer:
    """
    Renders 1080x1920 Telegram Story composite images that replicate
    Telegram's native repost card format with 1:1 pixel accuracy.
    Uses high-performance Pillow (PIL) typography rendering, typographic line
    wrapping, and CSS-style photo collage layouts.
    Card coordinates (for InputMediaAreaChannelPost) are returned by the *_with_coords entry points; the legacy
    get_last_card_coordinates() only reports renders made on the calling thread, never another job's card.
    """

    def __init__(self):
        self._local = threading.local()

    def _set_card_coords(self, coords: Dict[str, float]) -> None:
        """Remembers the coordinates of the last card rendered by the current thread"""
        self._local.last_card_coords = dict(coords)

    def get_last_card_coordinates(self) -> Dict[str, float]:
        """Percentage coordinates of the last card rendered on THIS thread (defaults when none)"""
        coords = getattr(self._local, "last_card_coords", None)
        return dict(coords) if coords else dict(_DEFAULT_CARD_COORDS)

    @property
    def last_card_coords(self) -> Dict[str, float]:
        return self.get_last_card_coordinates()

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
                im.draft("RGB", (max_dim, max_dim))
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

        raw_lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in caption.split('\n')]
        non_empty = [cls._clean_text_for_rendering(line) for line in raw_lines if line.strip()]

        cleaned = []
        price_line = None

        for line in non_empty:
            low = line.lower()
            if any(k in low for k in ['цена', 'нарх', 'narxi', 'стоимость', 'ijara']) and any(c.isdigit() for c in line):
                if not price_line:
                    price_line = line
                continue
            # Filter strictly lines that are purely contacts, URLs, or handles without content
            is_contact_only = bool(
                re.match(r'^(?:https?:\/\/|t\.me\/|@)', low) or
                re.match(r'^(?:тел|tel|aloqa|контакт|contact|админ|admin)\s*[:\-–—]?\s*(?:\+?[0-9\s\-\(\)]+|@[a-z0-9_]+)?$', low) or
                re.match(r'^\+?[0-9\s\-\(\)]{7,}$', low)
            )
            if is_contact_only:
                continue
            cleaned.append(line)

        display = cleaned[:4]

        if price_line and price_line not in display:
            display.append(price_line)
        elif price:
            price_markers = ('$', 'usd', 'нарх', 'цена', 'narx')
            if not any(marker in line.lower() for line in display for marker in price_markers):
                display.append(f"Narxi: {format_price_usd(price)}")

        if not display and non_empty:
            display = non_empty[:3]

        return display[:5] if display else ["Yangi e'lon"]

    @classmethod
    def _build_caption_html(cls, caption: str, price: Optional[float] = None) -> str:
        """Constructs HTML for caption with native emojis, blue hashtags, and More button"""
        display_lines = cls.extract_story_lines(caption, price)
        raw_lines = [line.strip() for line in caption.split('\n') if line.strip()] if caption else []

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
            escaped = html.escape(str(b))
            cls_name = "badge-pill"
            if "PREMYUM" in str(b) or "💎" in str(b):
                cls_name += " badge-luxury"
            elif "BIZNES" in str(b) or "⭐" in str(b):
                cls_name += " badge-business"
            elif "SARA" in str(b) or "🔥" in str(b):
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
        """Runs a Playwright job on the dedicated worker pool (bounded concurrency, reused browsers).
        Jobs already running on a pool thread execute inline."""
        if threading.current_thread().name.startswith(_PLAYWRIGHT_THREAD_PREFIX):
            return fn(*args, **kwargs)
        future = _PLAYWRIGHT_POOL.submit(fn, *args, **kwargs)
        return future.result(timeout=_PLAYWRIGHT_RENDER_TIMEOUT)

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
        out_path, coords = self._execute_playwright(
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
        self._set_card_coords(coords)
        return out_path

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
    ) -> Tuple[str, Dict[str, float]]:
        """
        Internal implementation: Renders the story using Playwright Chromium for 100% native Telegram styling.
        Guarantees flawless emoji alignment, typography, and precise bounding boxes.
        When transparent=True, renders without background as RGBA PNG for video overlay.
        Returns (output_path, card_coordinates).
        """
        bg_uri = self._image_to_base64_uri(bg_base_path) if bg_base_path else None
        if not bg_uri and not transparent:
            # Fallback inline SVG green background
            bg_uri = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='1080' height='1920'><rect width='1080' height='1920' fill='%2387b878'/></svg>"

        avatar_uri = self._image_to_base64_uri(avatar_path)
        if not avatar_uri:
            # Fallback avatar badge with initial letter
            initial = html.escape((channel_title[:1] if channel_title else "A").upper())
            avatar_uri = f"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='72' height='72'><circle cx='36' cy='36' r='36' fill='%238f327e'/><text x='36' y='46' font-size='32' font-family='sans-serif' font-weight='bold' fill='white' text-anchor='middle'>{initial}</text></svg>"

        photo_uris = []
        for p in photo_paths[:_COLLAGE_MAX_PHOTOS]:
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

        time_text = html.escape(self._clean_text_for_rendering(date_str or tashkent_date_label()))

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
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}
.forward-info {{
    color: #8e8e93;
    font-size: 21px;
    margin-top: 4px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
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
            suffix = "png" if transparent else "jpg"
            prefix = "overlay" if transparent else "story_card"
            output_path = os.path.join(_temp_media_dir(), f"{prefix}_{uuid.uuid4().hex}.{suffix}")

        coords = dict(_DEFAULT_CARD_COORDS)
        browser = _get_thread_browser()
        page = None
        try:
            page = browser.new_page(viewport={'width': 1080, 'height': 1920})
            # "load" waits for the data-URI images; the extra check covers decoding of large embedded photos
            page.set_content(html_content, wait_until="load", timeout=20000)
            try:
                page.wait_for_function(
                    "() => Array.from(document.images).every(img => img.complete && img.naturalWidth > 0)",
                    timeout=10000
                )
            except Exception:
                logger.debug("Story card images did not all finish decoding in time", exc_info=True)

            card_el = page.query_selector('#card')
            if card_el:
                box = card_el.bounding_box()
                if box:
                    coords = {
                        "x": round((box['x'] + box['width'] / 2.0) / 1080.0 * 100.0, 1),
                        "y": round((box['y'] + box['height'] / 2.0) / 1920.0 * 100.0, 1),
                        "w": round(box['width'] / 1080.0 * 100.0, 1),
                        "h": round(box['height'] / 1920.0 * 100.0, 1)
                    }

            if transparent:
                page.screenshot(path=output_path, omit_background=True, type='png', timeout=15000)
            else:
                page.screenshot(path=output_path, quality=95, type='jpeg', timeout=15000)
        except Exception:
            # A crashed / disconnected browser is relaunched by the next render on this worker
            _close_thread_browser()
            raise
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            _pw_local.last_used = time.time()

        logger.info(f"Story composite card rendered via Playwright: {output_path} (coords: {coords})")
        return output_path, coords

    # ==========================================
    # --- RESILIENT PILLOW (PIL) FALLBACK ---
    # ==========================================

    @staticmethod
    def _fit_and_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
        """Scales and center-crops an image to exact dimensions"""
        sw, sh = img.size
        scale = max(target_w / sw, target_h / sh)
        nw, nh = max(target_w, int(round(sw * scale))), max(target_h, int(round(sh * scale)))
        res = img.resize((nw, nh), Image.Resampling.LANCZOS)
        cx, cy = (nw - target_w) // 2, (nh - target_h) // 2
        return res.crop((cx, cy, cx + target_w, cy + target_h))

    @staticmethod
    def _load_image_scaled(path: str, max_w: int, max_h: int) -> Optional[Image.Image]:
        """Opens a photo decoded at (roughly) the size it will be shown at: JPEG draft decoding scales by 1/2..1/8
        while decoding, so a 12 MP photo never becomes a 36 MB bitmap"""
        try:
            with Image.open(path) as im:
                im.draft("RGB", (max_w, max_h))
                img = im.convert("RGB")
            if img.width > max_w * 2 or img.height > max_h * 2:
                img.thumbnail((max_w * 2, max_h * 2), Image.Resampling.BILINEAR)
            return img
        except Exception:
            logger.debug(f"Could not open story photo {path}", exc_info=True)
            return None

    @classmethod
    def create_photo_collage(cls, photo_paths: List[str], width: int = 880, height: int = 920) -> Optional[Image.Image]:
        """Creates an authentic Telegram-style photo collage matching the reference layout. Returns None if no valid photos."""
        valid_imgs = []
        for p in photo_paths or []:
            if len(valid_imgs) >= _COLLAGE_MAX_PHOTOS:
                break
            if p and os.path.exists(p):
                img = cls._load_image_scaled(p, width, height)
                if img is not None:
                    valid_imgs.append(img)

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
            collage.paste(cls._fit_and_crop(valid_imgs[1], width - col_w - gap, height), (col_w + gap, 0))
            return collage

        if n == 3:
            h1 = int(height * 0.55)
            h2 = height - h1 - gap
            col_w = (width - gap) // 2
            collage.paste(cls._fit_and_crop(valid_imgs[0], width, h1), (0, 0))
            collage.paste(cls._fit_and_crop(valid_imgs[1], col_w, h2), (0, h1 + gap))
            collage.paste(cls._fit_and_crop(valid_imgs[2], width - col_w - gap, h2), (col_w + gap, h1 + gap))
            return collage

        if n == 4:
            col_w = (width - gap) // 2
            row_h = (height - gap) // 2
            collage.paste(cls._fit_and_crop(valid_imgs[0], col_w, row_h), (0, 0))
            collage.paste(cls._fit_and_crop(valid_imgs[1], width - col_w - gap, row_h), (col_w + gap, 0))
            collage.paste(cls._fit_and_crop(valid_imgs[2], col_w, height - row_h - gap), (0, row_h + gap))
            collage.paste(cls._fit_and_crop(valid_imgs[3], width - col_w - gap, height - row_h - gap), (col_w + gap, row_h + gap))
            return collage

        def paste_row(images: List[Image.Image], y: int, row_h: int) -> None:
            count = len(images)
            cell_w = (width - (count - 1) * gap) // count
            x = 0
            for idx, img in enumerate(images):
                # The last cell absorbs the rounding remainder so every row spans the full width
                w = cell_w if idx < count - 1 else width - x
                collage.paste(cls._fit_and_crop(img, w, row_h), (x, y))
                x += w + gap

        if n == 5:
            # 2 + 3 layout filling the whole area (no empty band)
            h1 = int(height * 0.55)
            paste_row(valid_imgs[0:2], 0, h1)
            paste_row(valid_imgs[2:5], h1 + gap, height - h1 - gap)
            return collage

        # 6 or more photos: 2 + 3 + (1..3)
        h1 = int(height * 0.42)
        h2 = int(height * 0.29)
        h3 = height - h1 - h2 - (2 * gap)
        paste_row(valid_imgs[0:2], 0, h1)
        paste_row(valid_imgs[2:5], h1 + gap, h2)
        paste_row(valid_imgs[5:8], h1 + gap + h2 + gap, h3)
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

    @staticmethod
    def _text_width(font: ImageFont.ImageFont, text: str) -> float:
        try:
            return float(font.getlength(text))
        except Exception:
            bbox = font.getbbox(text)
            return float(bbox[2] - bbox[0])

    @classmethod
    def _fit_line(cls, font: ImageFont.ImageFont, text: str, max_width: float) -> str:
        """Truncates a single line with an ellipsis so it fits max_width"""
        if cls._text_width(font, text) <= max_width:
            return text
        ellipsis = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if cls._text_width(font, text[:mid].rstrip() + ellipsis) <= max_width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + ellipsis

    @classmethod
    def _wrap_text(cls, font: ImageFont.ImageFont, text: str, max_width: float) -> List[str]:
        """Word-wraps one paragraph by measured pixel width; words longer than a line are split"""
        words = text.split()
        lines: List[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            if cls._text_width(font, candidate) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            # Hard-split a single word that does not fit on an empty line
            while cls._text_width(font, word) > max_width and len(word) > 1:
                cut = len(word)
                while cut > 1 and cls._text_width(font, word[:cut]) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        if current:
            lines.append(current)
        return lines

    @classmethod
    def _layout_caption(cls, font: ImageFont.ImageFont, paragraphs: List[str], max_width: float, max_lines: int) -> List[str]:
        """Wraps all caption paragraphs and caps the total number of lines (last line gets an ellipsis)"""
        lines: List[str] = []
        truncated = False
        for para in paragraphs:
            wrapped = cls._wrap_text(font, para, max_width)
            for ln in wrapped:
                if len(lines) >= max_lines:
                    truncated = True
                    break
                lines.append(ln)
            if truncated:
                break
        if truncated and lines:
            lines[-1] = cls._fit_line(font, lines[-1] + " …", max_width)
        return lines

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
            clean_text = cls._fit_line(font, clean_text, collage.width - 48 - 50)

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
        out_path, coords = self._render_pil_internal(
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
        self._set_card_coords(coords)
        return out_path

    def _render_pil_internal(
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
    ) -> Tuple[str, Dict[str, float]]:
        """Renders the card with Pillow. Returns (output_path, card_coordinates)."""
        target_w, target_h = 1080, 1920

        if transparent:
            bg_rgba = Image.new('RGBA', (target_w, target_h), (0, 0, 0, 0))
        elif bg_base_path and os.path.isfile(bg_base_path):
            try:
                with Image.open(bg_base_path) as bg_file:
                    bg_file.draft("RGB", (target_w, target_h))
                    bg = bg_file.convert('RGB')
                if bg.size != (target_w, target_h):
                    bg = bg.resize((target_w, target_h), Image.Resampling.LANCZOS)
                bg_rgba = bg.convert('RGBA')
            except Exception:
                bg_rgba = self._create_gradient_background('telegram_green', target_w, target_h)
        else:
            style_name = bg_base_path if bg_base_path in ['luxury_dark', 'emerald', 'telegram_green'] else 'telegram_green'
            bg_rgba = self._create_gradient_background(style_name, target_w, target_h)

        font_title = self._get_system_font(28, bold=True)
        font_sub = self._get_system_font(20, bold=False)
        font_time = self._get_system_font(20, bold=False)
        font_badge = self._get_system_font(18, bold=True)

        card_w = 880
        card_r = 24
        text_x = 32
        text_max_w = card_w - 2 * text_x
        outer_margin = 60

        raw_display = self.extract_story_lines(caption, price)
        paragraphs = [self._clean_text_for_rendering(line, strip_emoji=True) for line in raw_display if line.strip()]
        paragraphs = [p for p in paragraphs if p]

        collage_h = 920
        collage = self.create_photo_collage(photo_paths, width=card_w, height=collage_h)
        has_collage = collage is not None

        if not has_collage:
            font_text = self._get_system_font(28, bold=False)
            line_h = 42
            caption_padding_top = 28
            max_lines = 14
            header_h = 96 if forward_title else 68
            collage_h = 0
        else:
            font_text = self._get_system_font(24, bold=False)
            line_h = 34
            caption_padding_top = 20
            max_lines = 8
            header_h = 86 if forward_title else 58

        text_lines = self._layout_caption(font_text, paragraphs, text_max_w, max_lines)
        date_line_h = 30
        caption_padding_bottom = 18
        caption_h = caption_padding_top + (len(text_lines) * line_h) + 6 + date_line_h + caption_padding_bottom

        # Keep the whole card inside the 9:16 frame: shrink the collage when the caption is long
        if has_collage:
            overflow = header_h + collage_h + caption_h - (target_h - 2 * outer_margin)
            if overflow > 0:
                collage_h = max(420, collage_h - overflow)
                collage = self._fit_and_crop(collage, card_w, collage_h)

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

        c_title_clean = self._clean_text_for_rendering(channel_title or "ARENDA UY", strip_emoji=True) or "ARENDA UY"
        card_draw.text((text_x, 16 if not forward_title else 14), self._fit_line(font_title, c_title_clean, text_max_w), font=font_title, fill=(155, 61, 125))
        if forward_title:
            fwd_clean = self._clean_text_for_rendering(forward_title, strip_emoji=True)
            card_draw.text((text_x, 52), self._fit_line(font_sub, f"Manba: {fwd_clean}", text_max_w), font=font_sub, fill=(198, 125, 50))

        if has_collage:
            card_img.paste(collage, (0, header_h))

        y_text = header_h + collage_h + caption_padding_top
        for line in text_lines:
            card_draw.text((text_x, y_text), line, font=font_text, fill=(28, 28, 30))
            y_text += line_h

        d_text = self._clean_text_for_rendering(date_str or tashkent_date_label(), strip_emoji=True)
        d_bbox = font_time.getbbox(d_text)
        w_date = d_bbox[2] - d_bbox[0]
        card_draw.text((card_w - w_date - text_x, card_h - caption_padding_bottom - date_line_h), d_text, font=font_time, fill=(142, 142, 147))

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

        av_loaded = None
        if avatar_path and os.path.exists(avatar_path):
            try:
                with Image.open(avatar_path) as av_file:
                    av_file.draft("RGB", (136, 136))
                    av_loaded = av_file.convert('RGBA').resize((68, 68), Image.Resampling.LANCZOS)
            except Exception as av_err:
                logger.debug(f"Avatar paste note: {av_err}")
                av_loaded = None
        if av_loaded is not None:
            mask = Image.new('L', (68, 68), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 68, 68), fill=255)
            av_bordered = Image.new('RGBA', (74, 74), (0, 0, 0, 0))
            ImageDraw.Draw(av_bordered).ellipse((0, 0, 74, 74), fill=(255, 255, 255, 255))
            av_bordered.paste(av_loaded, (3, 3), mask)
            bg_rgba.paste(av_bordered, (card_left - 20, card_top + card_h - 48), av_bordered)
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
            name = f"overlay_{uuid.uuid4().hex}.png" if transparent else f"story_card_{uuid.uuid4().hex}.jpg"
            output_path = os.path.join(_temp_media_dir(), name)

        if transparent or (output_path and output_path.lower().endswith('.png')):
            bg_rgba.save(output_path, format='PNG')
        else:
            out_res = bg_rgba.convert('RGB')
            out_res.save(output_path, format='JPEG', quality=95)

        center_y_pct = (card_top + card_h / 2.0) / float(target_h) * 100.0
        coords = {
            "x": 50.0,
            "y": round(center_y_pct, 1),
            "w": round(float(card_w) / float(target_w) * 100.0, 1),
            "h": round(card_h / float(target_h) * 100.0, 1)
        }

        logger.info(f"Story composite card rendered via PIL fallback: {output_path} (coords: {coords})")
        return output_path, coords

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
        Designed specifically for compositing onto video slideshows via FFmpeg.
        """
        if not output_path:
            output_path = os.path.join(_temp_media_dir(), f"overlay_{uuid.uuid4().hex}.png")

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

    def render_story_composite_with_coords(self, **kwargs) -> Tuple[str, Dict[str, float]]:
        """render_story_composite() plus the coordinates of THIS card (render and read happen on one thread)"""
        self._local.last_card_coords = None
        path = self.render_story_composite(**kwargs)
        return path, self.get_last_card_coordinates()

    def render_card_overlay_png_with_coords(self, **kwargs) -> Tuple[str, Dict[str, float]]:
        """render_card_overlay_png() plus the coordinates of THIS card (render and read happen on one thread)"""
        self._local.last_card_coords = None
        path = self.render_card_overlay_png(**kwargs)
        return path, self.get_last_card_coordinates()

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
        path, _coords = await self.render_story_composite_with_coords_async(
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
        return path

    async def render_story_composite_with_coords_async(self, **kwargs) -> Tuple[str, Dict[str, float]]:
        """Renders a static story card in a worker thread and returns (path, card_coordinates)"""
        return await asyncio.to_thread(lambda: self.render_story_composite_with_coords(**kwargs))

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
        path, _coords = await asyncio.to_thread(
            lambda: self.render_card_overlay_png_with_coords(
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
        )
        return path


story_card_renderer = StoryCardRenderer()


def shutdown_playwright_pool(wait: bool = False, cancel_futures: bool = True) -> None:
    """Cleanly shuts down the dedicated Playwright thread pool during application stop"""
    try:
        _PLAYWRIGHT_POOL.shutdown(wait=wait, cancel_futures=cancel_futures)
        logger.info("Playwright renderer thread pool shut down cleanly.")
    except Exception as e:
        logger.warning(f"Error shutting down Playwright thread pool: {e}")
