import re
import hashlib
import logging
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, List, Tuple
from deep_translator import GoogleTranslator

logger = logging.getLogger(__name__)

# deep_translator performs requests.get() without a timeout. Every call runs in this small dedicated pool (so a
# stalled HTTP request can never exhaust the default executor) and is abandoned after TRANSLATE_TIMEOUT_SECONDS.
TRANSLATE_TIMEOUT_SECONDS = 15.0
TRANSLATE_MAX_WORKERS = 4


class TranslatorService:
    def __init__(self):
        self._cache: OrderedDict[str, str] = OrderedDict()
        # Kept for API compatibility; translators are no longer shared (see _get_translator)
        self._translators: OrderedDict[str, GoogleTranslator] = OrderedDict()
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore_loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_call_time: float = 0.0
        self._executor: Optional[ThreadPoolExecutor] = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None
        if self._semaphore is None or self._semaphore_loop != curr_loop:
            self._semaphore = asyncio.Semaphore(2)
            self._semaphore_loop = curr_loop
        return self._semaphore

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=TRANSLATE_MAX_WORKERS, thread_name_prefix="translator")
        return self._executor

    def _get_translator(self, source: str = "auto", target: str = "uz") -> GoogleTranslator:
        """A fresh translator per call: GoogleTranslator.translate() stores the text in the instance's URL
        parameters, so one instance shared between worker threads could send another post's text."""
        return GoogleTranslator(source=source, target=target)

    async def translate_text(self, text: str, target_lang: str = "uz", source_lang: str = "auto") -> str:
        """
        Translates text while protecting links, @usernames, and HTML tags with robust placeholders.
        """
        if not text or not text.strip():
            return text

        text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()[:16]
        cache_key = f"{source_lang}_{target_lang}_{text_hash}"
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]

        # 1. Mask URLs, @mentions, and HTML tags with unmodifiable token placeholders
        placeholders: Dict[str, str] = {}
        counter = 0

        def mask_match(match):
            nonlocal counter
            tag = f"⟦99{counter:04d}⟧"
            counter += 1
            placeholders[tag] = match.group(0)
            return tag

        # Mask HTML tags
        masked_text = re.sub(r'<[^>]+>', mask_match, text)
        # Protected affiliate link placeholders must survive translation untouched
        masked_text = re.sub(r'___AFF_PROT_\d+___', mask_match, masked_text)
        # Mask URLs (excluding trailing sentence punctuation)
        def mask_url(m):
            nonlocal counter
            url = m.group(1)
            punct = m.group(2)
            tag = f"⟦99{counter:04d}⟧"
            counter += 1
            placeholders[tag] = url
            return tag + punct

        masked_text = re.sub(r'(https?://[^\s<>"\'\)]+?)([.,!?:;)]*)(?=\s|$)', mask_url, masked_text)
        # Mask Emails (must precede Telegram usernames so domain is not split)
        masked_text = re.sub(r'\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\b', mask_match, masked_text)
        # Mask Telegram links & usernames (support 3-32 character handles)
        masked_text = re.sub(r'@[a-zA-Z0-9_]{3,32}', mask_match, masked_text)
        masked_text = re.sub(r'(t\.me/[^\s<>"\'\)]+?)([.,!?:;)]*)(?=\s|$)', mask_url, masked_text)

        # 2. Perform translation in thread pool with automatic chunking for long texts
        loop = asyncio.get_running_loop()
        sem = self._get_semaphore()
        executor = self._get_executor()
        failed = False

        async def _translate_chunk(chunk_str: str) -> str:
            nonlocal failed
            if not chunk_str or not chunk_str.strip():
                return chunk_str
            async with sem:
                now = loop.time()
                elapsed = now - self._last_call_time
                if elapsed < 0.35:
                    await asyncio.sleep(0.35 - elapsed)
                self._last_call_time = loop.time()

                for attempt in range(3):
                    try:
                        translator = self._get_translator(source=source_lang, target=target_lang)
                        res = await asyncio.wait_for(
                            loop.run_in_executor(executor, translator.translate, chunk_str),
                            timeout=TRANSLATE_TIMEOUT_SECONDS
                        )
                        if not res:
                            failed = True
                        return res if res else chunk_str
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"Google Translate did not answer within {TRANSLATE_TIMEOUT_SECONDS:.0f}s "
                            f"({source_lang}->{target_lang}); keeping the original text."
                        )
                        break
                    except Exception as ex:
                        err_str = str(ex).lower()
                        if ("too many requests" in err_str or "5 requests" in err_str or "quota" in err_str) and attempt < 2:
                            await asyncio.sleep(1.2 * (attempt + 1))
                            continue
                        logger.warning(f"Google Translate chunk notice ({source_lang}->{target_lang}, attempt {attempt+1}): {ex}")
                        break
                failed = True
                return chunk_str

        def _split_into_chunks(txt: str, max_chunk_size: int = 3500) -> Tuple[List[str], str]:
            if len(txt) <= max_chunk_size:
                return [txt], ""
            if "\n\n" in txt:
                subparts, delim = txt.split("\n\n"), "\n\n"
            elif "\n" in txt:
                subparts, delim = txt.split("\n"), "\n"
            elif ". " in txt:
                subparts, delim = txt.split(". "), ". "
            else:
                subparts, delim = txt.split(" "), " "

            chunks = []
            curr = []
            curr_len = 0
            for part in subparts:
                if curr_len + len(part) + len(delim) > max_chunk_size and curr:
                    chunks.append(delim.join(curr))
                    curr = [part]
                    curr_len = len(part)
                else:
                    curr.append(part)
                    curr_len += len(part) + len(delim)
            if curr:
                chunks.append(delim.join(curr))
            return chunks, delim

        try:
            if len(masked_text) <= 4000:
                translated = await _translate_chunk(masked_text)
            else:
                chunks, delim = _split_into_chunks(masked_text, max_chunk_size=3500)
                translated_chunks = await asyncio.gather(*[_translate_chunk(c) for c in chunks])
                translated = delim.join(translated_chunks)
        except Exception as e:
            logger.warning(f"Translation notice ({source_lang} -> {target_lang}): {e}")
            return text

        # 3. Restore placeholders, tolerating spaces Google Translate may add inside brackets
        for tag, original in placeholders.items():
            translated = translated.replace(tag, original)

        # Robust regex fallback for modified whitespace or altered brackets: ⟦ 990001 ⟧, [99 0001], {990001}
        def _restore_fuzzy(m):
            digits = re.sub(r'\s+', '', m.group(1))
            return placeholders.get(f"⟦99{digits}⟧", m.group(0))

        translated = re.sub(
            r'[⟦\[\{]\s*99([\s\d]{4,8})\s*[⟧\]\}]',
            _restore_fuzzy,
            translated
        )

        # A chunk that timed out or failed stays untranslated; such results are not cached so the next post retries
        if not failed:
            self._cache[cache_key] = translated
            if len(self._cache) > 2000:
                self._cache.popitem(last=False)
        return translated

translator_service = TranslatorService()
