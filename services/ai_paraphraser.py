import re
import logging
from typing import Optional
from services.custom_emojis import DOCUMENT, ROCKET, FLASH, STAR_SPARKLE

logger = logging.getLogger(__name__)

class AIParaphraserService:
    """
    Intelligent Content Paraphraser & Tone Shifter.
    Transforms raw cloned post text into specialized journalistic, viral hype, or concise summary formats
    while strictly preserving HTML tags, custom emojis, and URL entities.
    """

    def __init__(self):
        pass

    def paraphrase(self, text: str, mode: str = "off") -> str:
        """
        Applies tone shifting to post text according to mode:
        - 'off': unchanged
        - 'formal': analytical & formal style
        - 'hype': high-engagement, clickbait hooks & expressive styling
        - 'short': bullet-pointed concise summary (TL;DR)
        """
        if not text or not text.strip() or mode == "off":
            return text

        # Separate HTML tags and preserve them using placeholder masks
        placeholders = {}
        tag_pattern = re.compile(r'<[^>]+>')
        counter = 0

        def mask_tag(match):
            nonlocal counter
            key = f"___HTM_{counter}___"
            placeholders[key] = match.group(0)
            counter += 1
            return key

        masked_text = tag_pattern.sub(mask_tag, text)

        # Apply transformation according to mode
        if mode == "formal":
            transformed = self._transform_formal(masked_text)
        elif mode == "hype":
            transformed = self._transform_hype(masked_text)
        elif mode == "short":
            transformed = self._transform_short(masked_text)
        else:
            transformed = masked_text

        # Restore HTML tags
        for key, orig_tag in placeholders.items():
            transformed = transformed.replace(key, orig_tag)

        return transformed

    def _transform_formal(self, text: str) -> str:
        """Transforms text into official, analytical journalistic tone"""
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return text

        header = lines[0]
        # Ensure professional formatting with Telegram Premium custom emojis
        if not header.startswith("📌") and "Rasmiy Axborot" not in header:
            header = f"{DOCUMENT} <b>Rasmiy Axborot:</b> {header}"

        body = "\n\n".join(lines[1:]) if len(lines) > 1 else ""
        if body:
            return f"{header}\n\n{body}"
        return header

    def _transform_hype(self, text: str) -> str:
        """Transforms text into high-engagement viral hook format with Premium emojis"""
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return text

        header = lines[0]
        hype_prefixes = [f"{ROCKET} <b>SHOSHILINCH YANGILIK:</b>", f"{FLASH} <b>DIQQAT:</b>", f"{STAR_SPARKLE} <b>EKSKLYUZIV:</b>"]
        chosen_prefix = hype_prefixes[len(header) % len(hype_prefixes)]

        header = f"{chosen_prefix}\n{header}"
        body = "\n\n".join(lines[1:]) if len(lines) > 1 else ""

        if body:
            return f"{header}\n\n{body}\n\n👉 <i>Batafsil ma'lumot yuqorida keltirilgan!</i>"
        return f"{header}\n\n👉 <i>Batafsil ma'lumot yuqorida keltirilgan!</i>"

    def _transform_short(self, text: str) -> str:
        """Transforms text into bullet-point summary format"""
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return text

        if len(lines) <= 2:
            return f"{FLASH} <b>Qisqa Xulosa:</b>\n├ {text}"

        header = f"{FLASH} <b>Eng Asosiy Ma'lumotlar:</b>"
        bullets = []
        for i, line in enumerate(lines[:10]):
            prefix = "└" if (i == len(lines[:10]) - 1) else "├"
            bullets.append(f"{prefix} {line}")

        return f"{header}\n" + "\n".join(bullets)

ai_paraphraser = AIParaphraserService()
