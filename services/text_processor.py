import re
import html
import unicodedata
import logging
from typing import Optional, List, Dict, Tuple, Any
from database.models import ChannelPair

logger = logging.getLogger(__name__)

# Common telegram link and username regex patterns
TG_USERNAME_PATTERN = re.compile(r'(?<![\w.-])@([a-zA-Z0-9_]{4,32})(?![\w.-]*\.[a-zA-Z]{2,})', re.IGNORECASE)
TG_LINK_PATTERN = re.compile(
    r'(https?:\/\/)?(www\.)?(t\.me|telegram\.me|telegram\.dog)\/([a-zA-Z0-9_+\/]{3,})',
    re.IGNORECASE
)
TG_JOINCHAT_PATTERN = re.compile(
    r'(https?:\/\/)?(www\.)?(t\.me|telegram\.me)\/(joinchat\/|\+)[a-zA-Z0-9_-]+',
    re.IGNORECASE
)
TG_DEEP_LINK_PATTERN = re.compile(
    r'tg:\/\/(?:resolve\?domain=|join\?invite=)[a-zA-Z0-9_+%-]+',
    re.IGNORECASE
)

PROMO_CTA_PATTERNS = [
    re.compile(r'^\s*(?:Bizning\s+kanal|Kanalimiz|Kanalga\s+obuna\s+bo[\'ʼ`]?ling|A[\'ʼ`]?zo\s+bo[\'ʼ`]?ling|Kanalga\s+qo[\'ʼ`]?shiling)\s*[:\-–—]?\s*(?:@\w+|https?:\/\/t\.me\/\S+)\s*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:Подписывайтесь|Наш\s+канал|Канал|Ссылка\s+на\s+канал|Присоединяйтесь)\s*[:\-–—]?\s*(?:@\w+|https?:\/\/t\.me\/\S+)\s*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:Subscribe|Join\s+channel|Our\s+channel)\s*[:\-–—]?\s*(?:@\w+|https?:\/\/t\.me\/\S+)\s*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:👉|➡️|🔗|📱)?\s*(?:Переходите\s+по\s+ссылке|Перейти\s+по\s+ссылке|Подробнее\s+по\s+ссылке|Связь|Контакты|Связаться|Aloqa|Murojaat\s+uchun|Bog[\'ʼ`]?lanish)\s*[:\-–—]?\s*(?:@\w+|https?:\/\/t\.me\/\S+)\s*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:👉|➡️|🔗|📱)?\s*@\w+\s*$', re.MULTILINE),
    re.compile(r'^\s*(?:👉|➡️|🔗|📱)?\s*https?:\/\/t\.me\/\S+\s*$', re.MULTILINE),
]

WEB_URL_PATTERN = re.compile(
    r'https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{2,10}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)',
    re.IGNORECASE
)

COMMERCIAL_AD_PATTERNS = [
    # Explicit ad tags & disclosures
    re.compile(r'(?:#reklama|#реклама|#ad\b|#advertisement|#sponsor|#hamkorlik|#promoted)', re.IGNORECASE),
    re.compile(r'^\s*(?:Реклама|На\s+правах\s+рекламы|Спонсорский\s+пост|Спонсор\s+показа|Hamkorlik\s+asosida|Tijoriy\s+reklama)\s*[:\-–—]?', re.IGNORECASE | re.MULTILINE),
    # Casino, betting, gambling brands & mechanics
    re.compile(r'\b(?:1xbet|1win|melbet|mostbet|pin-?up|linebet|aviator|vulkan|parimatch|betandyou|fonbet|olimpbet)\b', re.IGNORECASE),
    re.compile(r'\b(?:stavka\s+qiling|katta\s+yutuq|promokod|depozit|bonus\s*\d*%?|kazino|slotlar|frispin)\b', re.IGNORECASE),
    re.compile(r'\b(?:ставка\s+на\s+спорт|бонус\s+к\s+депозиту|выигрыш\s+в\s+казино|слоты|фриспины)\b', re.IGNORECASE),
    # Crypto / spam airdrop / bot schemes
    re.compile(r'\b(?:airdrop\s+token|bepul\s+ton|bepul\s+usdt|pul\s+ishlash\s+boti|kunlik\s+daromad\s+\d+%)\b', re.IGNORECASE),
    # Bulletin board passenger/freight spam
    re.compile(r'\b(?:moshina\s+bor|damas\s+bor|pochta\s+bor\s+odam\s+bor|yulovchi\s+kerak)\b', re.IGNORECASE),
]

class TextProcessor:
    @staticmethod
    def extract_channel_from_message(message: Any) -> Optional[Tuple[str, str, Optional[int]]]:
        """
        Extracts channel identifier, title, and ID from either:
        1. Forwarded message (forward_from_chat or forward_origin)
        2. Text containing link or @username
        Returns: (identifier, title, chat_id) or None
        """
        # Check standard forward_from_chat
        if getattr(message, "forward_from_chat", None):
            chat = message.forward_from_chat
            identifier = f"@{chat.username}" if chat.username else str(chat.id)
            title = chat.title or identifier
            return identifier, title, chat.id

        # Check Aiogram 3.x forward_origin
        forward_origin = getattr(message, "forward_origin", None)
        if forward_origin:
            chat = getattr(forward_origin, "chat", None)
            if chat:
                identifier = f"@{chat.username}" if getattr(chat, "username", None) else str(chat.id)
                title = getattr(chat, "title", identifier)
                return identifier, title, getattr(chat, "id", None)

        # Plain text extraction
        raw_text = getattr(message, "text", "") or ""
        normalized = TextProcessor.normalize_channel_input(raw_text)
        if normalized:
            return normalized, normalized, None

        return None

    @staticmethod
    def normalize_channel_input(raw_input: str) -> str:
        """
        Cleans and normalizes any user input for channel username/link/ID:
        - https://t.me/kunuzofficial/12345 -> @kunuzofficial
        - https://t.me/kunuzofficial -> @kunuzofficial
        - t.me/kunuzofficial -> @kunuzofficial
        - kunuzofficial -> @kunuzofficial
        - -1001234567890 -> -1001234567890
        - https://t.me/+joinlink -> https://t.me/+joinlink
        """
        if not raw_input:
            return ""
        
        s = raw_input.strip()
        
        # Numeric ID
        if s.startswith("-100") or (s.startswith("-") and s[1:].isdigit()) or s.isdigit():
            return s

        # Invite links
        if s.startswith("+"):
            return f"https://t.me/{s}"
        if "/+" in s or "/joinchat/" in s:
            if not s.startswith("http"):
                s = "https://" + s
            return s

        # Strip URL prefixes and message IDs
        s = re.sub(r'^https?:\/\/(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/', '', s, flags=re.IGNORECASE)
        s = re.sub(r'^(?:t\.me|telegram\.me|telegram\.dog)\/', '', s, flags=re.IGNORECASE)

        # Handle Telegram URI schemes: tg://resolve?domain=xxx
        tg_match = re.match(r'^tg:\/\/resolve\?domain=([a-zA-Z0-9_]{3,32})', s, flags=re.IGNORECASE)
        if tg_match:
            return f"@{tg_match.group(1)}"

        # Private channel/supergroup internal links: c/1234567890/123 -> -1001234567890
        m_c = re.match(r'^c\/(\d+)(?:\/\d+)*$', s, flags=re.IGNORECASE)
        if m_c:
            return f"-100{m_c.group(1)}"

        # Web preview links: s/channelname/123 -> channelname
        if s.lower().startswith("s/"):
            s = s[2:]

        # If has trailing post id like username/12345
        if "/" in s:
            s = s.split("/")[0]

        s = s.lstrip("@").strip()
        if s and not s.startswith("+"):
            if re.match(r'^[a-zA-Z0-9_]{3,32}$', s):
                return f"@{s}"
            return s
        return s

    @staticmethod
    def contains_blacklisted_words(text: str, blacklist: List[str]) -> bool:
        if not text or not blacklist:
            return False
        
        clean_text = re.sub(r'[\u200b\u200c\u200d\ufeff\u2060]', '', text)
        clean_text = re.sub(r"[’‘ʻʼ`]", "'", clean_text)
        normalized_text = unicodedata.normalize('NFKD', clean_text).casefold()
        for word in blacklist:
            if not word:
                continue
            w_clean = re.sub(r'[\u200b\u200c\u200d\ufeff\u2060]', '', word.strip())
            w_clean = re.sub(r"[’‘ʻʼ`]", "'", w_clean)
            w_clean = unicodedata.normalize('NFKD', w_clean).casefold()
            if not w_clean:
                continue
            # Match whole words respecting apostrophes, hyphens, and punctuation boundaries
            pattern = r'(?:(?<=[\s\.,!?;:()\[\]{}"\'`ʻʼ«»—–\-_/\\|])|^)' + re.escape(w_clean) + r'(?:(?=[\s\.,!?;:()\[\]{}"\'`ʻʼ«»—–\-_/\\|])|$)'
            if re.search(pattern, normalized_text):
                logger.info(f"Message blocked due to blacklisted word: '{word}'")
                return True
        return False

    @classmethod
    def is_commercial_ad(cls, text: str) -> bool:
        """
        Detects whether a post is a commercial advertisement, betting/casino promo,
        or explicit sponsored ad. Returns True if ad detected.
        """
        if not text:
            return False
        clean_text = re.sub(r'[\u200b\u200c\u200d\ufeff\u2060]', '', text)
        clean_text = re.sub(r"[’‘ʻʼ`]", "'", clean_text)
        normalized_text = unicodedata.normalize('NFKD', clean_text)
        for pat in COMMERCIAL_AD_PATTERNS:
            if pat.search(normalized_text):
                return True
        return False

    @staticmethod
    def clean_links_and_usernames(text: str, remove_web_urls: bool = False) -> str:
        if not text:
            return ""

        cleaned = re.sub(r'[\u200b\u200c\u200d\ufeff\u2060]', '', text)

        # 1. Clean HTML anchor tags pointing to Telegram links (preserve inner text if not purely CTA)
        cleaned = re.sub(
            r'<a\s+[^>]*href=["\']?(?:(?:https?:\/\/)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/[^"\'>\s]+|tg:\/\/[^"\'>\s]+)["\']?[^>]*>(.*?)<\/a>',
            r'\1',
            cleaned,
            flags=re.IGNORECASE | re.DOTALL
        )

        # 2. Clean Markdown formatted telegram links [text](https://t.me/...) or [text](tg://...)
        cleaned = re.sub(
            r'\[([^\]]+)\]\((?:(?:https?:\/\/)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/[^\)]+|tg:\/\/[^\)]+)\)',
            r'\1',
            cleaned,
            flags=re.IGNORECASE
        )

        # 3. Clean CTA promo lines
        for promo_pattern in PROMO_CTA_PATTERNS:
            cleaned = promo_pattern.sub('', cleaned)

        # 4. Clean raw Telegram links, invite links, usernames, and deep links (only outside HTML tags)
        tokens = re.split(r'(<[^>]+>)', cleaned)
        for i in range(0, len(tokens), 2):
            chunk = tokens[i]
            if chunk:
                chunk = TG_JOINCHAT_PATTERN.sub('', chunk)
                chunk = TG_LINK_PATTERN.sub('', chunk)
                chunk = TG_DEEP_LINK_PATTERN.sub('', chunk)
                chunk = TG_USERNAME_PATTERN.sub('', chunk)
                if remove_web_urls:
                    chunk = WEB_URL_PATTERN.sub('', chunk)
                tokens[i] = chunk
        cleaned = "".join(tokens)

        # 5. Clean residual empty or broken anchor tags
        cleaned = re.sub(r'<a\s+[^>]*href=["\']?\s*["\']?[^>]*>(.*?)<\/a>', r'\1', cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r'<a>(.*?)<\/a>', r'\1', cleaned, flags=re.IGNORECASE | re.DOTALL)

        # 6. Clean dangling promo CTA labels that now have no link/username
        cleaned = re.sub(
            r'^\s*(?:👉|➡️|🔗|📱)?\s*(?:Переходите\s+по\s+ссылке|Перейти\s+по\s+ссылке|Подробнее\s+по\s+ссылке|Ссылка\s+на\s+канал|Ссылка|Канал|Наш\s+канал|Подписывайтесь|Bizning\s+kanal|Kanalimiz|Kanal|Havola)\s*[:\-–—]?\s*$',
            '',
            cleaned,
            flags=re.IGNORECASE | re.MULTILINE
        )

        # Clean ad hashtags when web URLs cleaning requested
        if remove_web_urls:
            cleaned = re.sub(r'(?:#reklama|#реклама|#ad\b|#advertisement|#sponsor|#hamkorlik|#promoted)', '', cleaned, flags=re.IGNORECASE)

        # 7. Clean up dangling multiple line breaks
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()

    @staticmethod
    def _match_case(original: str, replacement: str) -> str:
        if not original or not replacement:
            return replacement
        # Never alter case of URLs, bot handles, invite links, or phone numbers
        if any(replacement.startswith(p) for p in ("http://", "https://", "t.me/", "@", "+")):
            return replacement
        if original.isupper():
            return replacement.upper()
        if original.istitle():
            return replacement.title()
        if original[0].isupper():
            return replacement[0].upper() + replacement[1:]
        return replacement

    @classmethod
    def apply_word_replacements(cls, text: str, replace_dict: Dict[str, str]) -> str:
        if not text or not replace_dict:
            return text

        # Tokenize by HTML tags so replacements are never applied inside HTML tags or attributes
        tokens = re.split(r'(<[^>]+>)', text)
        for i in range(0, len(tokens), 2):
            chunk = tokens[i]
            if not chunk:
                continue
            sorted_replacements = sorted(
                [(k, v) for k, v in replace_dict.items() if k],
                key=lambda x: len(x[0]),
                reverse=True
            )
            for old_val, new_val in sorted_replacements:
                pattern = r'(?:(?<=[\s\.,!?;:()\[\]{}"\'`ʻʼ«»—–\-_/\\|])|^)' + re.escape(old_val) + r'(?:(?=[\s\.,!?;:()\[\]{}"\'`ʻʼ«»—–\-_/\\|])|$)'
                chunk = re.sub(
                    pattern,
                    lambda m, v=new_val: cls._match_case(m.group(0), v),
                    chunk,
                    flags=re.IGNORECASE
                )
            tokens[i] = chunk
        return "".join(tokens)

    @classmethod
    def attach_signature(cls, text: str, signature: str, max_limit: int = 4096) -> str:
        if not signature:
            return text
        
        signature = signature.strip()
        if not text:
            return signature[:max_limit]
        
        if text.strip().endswith(signature):
            return text
        
        combined = f"{text}\n\n{signature}"
        sig_vis_len = cls.get_visible_text_length(signature)
        if len(combined) > max_limit or cls.get_visible_text_length(combined) > max_limit:
            max_text_len = max_limit - max(len(signature), sig_vis_len) - 2
            if max_text_len > 50:
                target_limit = max_text_len
                safe_cut, _ = cls.fit_caption_limit(text, max_limit=target_limit)
                while (len(safe_cut) + len(signature) + 2 > max_limit or cls.get_visible_text_length(safe_cut) + sig_vis_len + 2 > max_limit) and target_limit > 50:
                    overshoot = max((len(safe_cut) + len(signature) + 2) - max_limit, (cls.get_visible_text_length(safe_cut) + sig_vis_len + 2) - max_limit)
                    target_limit -= max(overshoot, 1)
                    safe_cut, _ = cls.fit_caption_limit(text, max_limit=target_limit)
                return f"{safe_cut}\n\n{signature}"
            safe_cut, _ = cls.fit_caption_limit(combined, max_limit=max_limit)
            return safe_cut
        return combined

    @staticmethod
    def strip_source_signature(text: str) -> str:
        """Removes source channel signatures, author handles, and footer links from post text"""
        if not text:
            return ""
        lines = text.rstrip().split("\n")
        if not lines:
            return text
        
        while lines:
            last_line = lines[-1].strip()
            if not last_line:
                lines.pop()
                continue
            line_body = re.sub(r'^(?:[\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f👉🔹📌✅📍▶️➡️🔗⚡️⭐️✨•\-\*]\s*)+', '', last_line).strip()
            check_target = line_body if line_body else last_line
            is_sig = bool(
                re.match(r'^(?:@[\w_]+|https?://t\.me/[\w_+/]+|(?:Manba|Kanal|Havola|Source|Channel|Подписаться|Канал|Bizning\s+kanal|Admin)\s*[:\-–—]?.*)$', check_target, flags=re.IGNORECASE) or
                re.match(r'^<a\s+[^>]*>.*?</a>$', check_target, flags=re.IGNORECASE) or
                re.match(r'^[—–\-_=*•#\s]{3,}$', last_line)
            )
            if is_sig:
                lines.pop()
            else:
                break
        return "\n".join(lines).rstrip()

    @classmethod
    def process_text(cls, raw_text: Optional[str], pair: ChannelPair) -> Optional[str]:
        if raw_text is None:
            raw_text = ""

        if pair.blacklist_list and cls.contains_blacklisted_words(raw_text, pair.blacklist_list):
            return None

        result = raw_text

        if (pair.clean_links or getattr(pair, "clone_mode", "clean") == "clean") and result:
            if cls.is_commercial_ad(result):
                logger.info("Message blocked: detected commercial advertisement/gambling post.")
                return None
            result = cls.clean_links_and_usernames(result)

        if pair.replace_dict and result:
            result = cls.apply_word_replacements(result, pair.replace_dict)

        if pair.remove_signature and result:
            result = cls.strip_source_signature(result)

        if pair.custom_signature and result:
            result = cls.attach_signature(result, pair.custom_signature)

        if result:
            result = cls.escape_raw_ampersands(result)

        return result

    @classmethod
    def escape_raw_ampersands(cls, text: str) -> str:
        """
        Escapes naked '&' characters to '&amp;' in HTML text without double-escaping
        valid HTML entities (e.g. &amp;, &lt;, &gt;, &quot;, &#39;, &#1234;).
        """
        if not text or '&' not in text:
            return text
        return re.sub(r'&(?!(?:[a-zA-Z0-9]+|#[0-9]+|#x[0-9a-fA-F]+);)', '&amp;', text)

    @staticmethod
    def get_visible_text_length(text: str) -> int:
        """
        Calculates the visible plain text length in UTF-16 code units (as Telegram API does)
        by removing all HTML tags and unescaping HTML entities.
        """
        if not text:
            return 0
        plain = re.sub(r'<[^>]+>', '', text)
        plain = html.unescape(plain)
        return len(plain.encode('utf-16-le')) // 2

    CONTAINER_TAGS = {
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote"
    }

    @classmethod
    def ensure_closed_tags(cls, html_text: str) -> str:
        """Ensures all opened HTML formatting tags are properly closed at the end of the text."""
        if not html_text:
            return html_text
        tag_stack: List[Tuple[str, str]] = []
        tag_pattern = re.compile(r'<(/)?([a-zA-Z0-9_\-]+)(\s+[^>]*)?>')
        for match in tag_pattern.finditer(html_text):
            is_closing = match.group(1) == '/'
            tag_name = match.group(2).lower()
            full_tag = match.group(0)
            if full_tag.endswith("/>") or tag_name not in cls.CONTAINER_TAGS:
                continue
            if is_closing:
                for idx in range(len(tag_stack) - 1, -1, -1):
                    if tag_stack[idx][0] == tag_name:
                        tag_stack.pop(idx)
                        break
            else:
                tag_stack.append((tag_name, full_tag))
        if not tag_stack:
            return html_text
        closing_suffix = "".join(f"</{tname}>" for tname, _ in reversed(tag_stack))
        return html_text + closing_suffix

    @classmethod
    def fit_caption_limit(cls, processed_text: str, max_limit: int = 1024, limit: Optional[int] = None) -> Tuple[str, Optional[str]]:
        """
        Fits caption text within Telegram's caption limit (1024 for standard Bot API, 2048 for Telegram Premium/Telethon).
        Only splits if the VISIBLE plain text length exceeds max_limit.
        Never splits when formatting tags (<b>, <a>, <tg-emoji>) inflate HTML size.
        """
        if limit is not None:
            max_limit = limit

        if not processed_text:
            return "", None

        if cls.get_visible_text_length(processed_text) <= max_limit:
            return cls.ensure_closed_tags(processed_text), None

        # Visible text exceeds limit: tokenize to locate split point within visible text
        tokens = re.split(r'(<[^>]+>)', processed_text)
        current_visible_len = 0
        cut_idx = len(processed_text)
        char_pos = 0

        for token in tokens:
            if not token:
                continue
            if token.startswith("<") and token.endswith(">"):
                char_pos += len(token)
            else:
                token_vis_len = len(html.unescape(token).encode('utf-16-le')) // 2
                if current_visible_len + token_vis_len > max_limit:
                    remaining_visible = max_limit - current_visible_len
                    # Slice entity-aware so entities like &quot; aren't sliced in half or dropped
                    accum_vis = 0
                    cut_in_token = 0
                    for part in re.split(r'(&[a-zA-Z0-9#]+;)', token):
                        if not part:
                            continue
                        part_vis = len(html.unescape(part).encode('utf-16-le')) // 2
                        if accum_vis + part_vis <= remaining_visible:
                            accum_vis += part_vis
                            cut_in_token += len(part)
                        else:
                            if part.startswith('&') and part.endswith(';'):
                                break
                            for ch in part:
                                ch_vis = len(ch.encode('utf-16-le')) // 2
                                if accum_vis + ch_vis <= remaining_visible:
                                    accum_vis += ch_vis
                                    cut_in_token += len(ch)
                                else:
                                    break
                            break
                    candidate = token[:cut_in_token]
                    nl_pos = candidate.rfind("\n")
                    sp_pos = candidate.rfind(" ")
                    # Pick whichever boundary is furthest to maximize caption usage
                    best_cut = max(nl_pos, sp_pos)
                    if best_cut > 0:
                        cut_idx = char_pos + best_cut
                    else:
                        cut_idx = char_pos + len(candidate)
                    break
                else:
                    current_visible_len += token_vis_len
                    char_pos += len(token)

        caption_part = processed_text[:cut_idx].strip()
        overflow_part = processed_text[cut_idx:].strip()

        # Check for unclosed HTML tags in caption_part and preserve original opening tags with attributes using a strict LIFO stack
        tag_stack: List[Tuple[str, str]] = []
        tag_pattern = re.compile(r'<(/)?([a-zA-Z0-9_\-]+)(\s+[^>]*)?>')
        for match in tag_pattern.finditer(caption_part):
            is_closing = match.group(1) == '/'
            tag_name = match.group(2).lower()
            full_tag = match.group(0)
            if full_tag.endswith("/>") or tag_name not in cls.CONTAINER_TAGS:
                continue
            if is_closing:
                for idx in range(len(tag_stack) - 1, -1, -1):
                    if tag_stack[idx][0] == tag_name:
                        tag_stack.pop(idx)
                        break
            else:
                tag_stack.append((tag_name, full_tag))

        # Close unclosed tags in caption (innermost first) and reopen in overflow (outermost first)
        closing_suffix = "".join(f"</{tname}>" for tname, _ in reversed(tag_stack))
        # Telegram strictly requires <tg-emoji> to contain only a single emoji character.
        # Never reopen <tg-emoji> across boundary splits to wrap general text.
        opening_prefix = "".join(full_str for tname, full_str in tag_stack if tname != "tg-emoji")

        caption = caption_part + closing_suffix
        if overflow_part:
            raw_overflow = opening_prefix + overflow_part
            overflow = cls.ensure_closed_tags(raw_overflow) if cls.get_visible_text_length(raw_overflow) > 0 else None
            if overflow and not re.sub(r'<[^>]+>', '', overflow).strip():
                overflow = None
        else:
            overflow = None

        return caption, overflow

    @classmethod
    def fit_text_limit(cls, processed_text: str, max_limit: int = 4096) -> List[str]:
        """
        Fits text message within Telegram's 4096-character limit.
        Splits into multiple chunks only if visible plain text length exceeds 4096.
        Ensures all chunks have properly closed HTML formatting tags.
        """
        if not processed_text:
            return [""]
        if cls.get_visible_text_length(processed_text) <= max_limit:
            return [cls.ensure_closed_tags(processed_text)]

        chunks = []
        remaining = processed_text
        while remaining:
            if cls.get_visible_text_length(remaining) <= max_limit:
                chunks.append(cls.ensure_closed_tags(remaining))
                break
            caption_part, overflow_part = cls.fit_caption_limit(remaining, max_limit=max_limit)
            if not caption_part:
                chunks.append(cls.ensure_closed_tags(remaining[:max_limit]))
                remaining = remaining[max_limit:]
            else:
                chunks.append(cls.ensure_closed_tags(caption_part))
                remaining = overflow_part or ""
        return [c for c in chunks if c.strip()] or [cls.ensure_closed_tags(processed_text)]
