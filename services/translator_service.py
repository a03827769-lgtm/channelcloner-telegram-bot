import re
import logging
import asyncio
from typing import Optional, Dict
from deep_translator import GoogleTranslator

logger = logging.getLogger(__name__)

class TranslatorService:
    def __init__(self):
        self._cache: Dict[str, str] = {}
        self._translators: Dict[str, GoogleTranslator] = {}

    def _get_translator(self, source: str = "auto", target: str = "uz") -> GoogleTranslator:
        key = f"{source}_{target}"
        if key not in self._translators:
            self._translators[key] = GoogleTranslator(source=source, target=target)
        return self._translators[key]

    async def translate_text(self, text: str, target_lang: str = "uz", source_lang: str = "auto") -> str:
        """
        Translates text while protecting links, @usernames, and code blocks with placeholders.
        """
        if not text or not text.strip():
            return text

        cache_key = f"{source_lang}_{target_lang}_{hash(text)}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 1. Mask URLs, @mentions, and HTML tags with placeholders
        placeholders: Dict[str, str] = {}
        counter = 0

        def mask_match(match):
            nonlocal counter
            tag = f"___TAG_{counter}___"
            counter += 1
            placeholders[tag] = match.group(0)
            return tag

        # Mask HTML tags
        masked_text = re.sub(r'<[^>]+>', mask_match, text)
        # Mask URLs
        masked_text = re.sub(r'https?://[^\s]+', mask_match, masked_text)
        # Mask Telegram links & usernames
        masked_text = re.sub(r'@[a-zA-Z0-9_]{4,32}', mask_match, masked_text)
        masked_text = re.sub(r't\.me/[^\s]+', mask_match, masked_text)

        # 2. Perform translation in thread pool
        loop = asyncio.get_running_loop()
        translator = self._get_translator(source=source_lang, target=target_lang)

        try:
            # deep-translator handles up to 5000 chars per chunk
            translated = await loop.run_in_executor(None, translator.translate, masked_text)
            if not translated:
                translated = masked_text
        except Exception as e:
            logger.error(f"Translation error ({source_lang} -> {target_lang}): {e}")
            return text

        # 3. Restore placeholders
        for tag, original in placeholders.items():
            # In case translator added spaces around tags
            translated = re.sub(re.escape(tag), original, translated, flags=re.IGNORECASE)
            translated = translated.replace(tag, original)

        if len(self._cache) > 2000:
            self._cache.clear()

        self._cache[cache_key] = translated
        return translated

translator_service = TranslatorService()
