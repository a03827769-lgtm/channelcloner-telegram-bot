import re
import html
import logging
from typing import Optional, List, Dict, Tuple, Any
from database.models import ChannelPair

logger = logging.getLogger(__name__)

# Common telegram link and username regex patterns
TG_USERNAME_PATTERN = re.compile(r'(?<!\w)@([a-zA-Z0-9_]{4,32})', re.IGNORECASE)
TG_LINK_PATTERN = re.compile(
    r'(https?:\/\/)?(www\.)?(t\.me|telegram\.me|telegram\.dog)\/([a-zA-Z0-9_+\/]{3,})',
    re.IGNORECASE
)
TG_JOINCHAT_PATTERN = re.compile(
    r'(https?:\/\/)?(www\.)?(t\.me|telegram\.me)\/(joinchat\/|\+)[a-zA-Z0-9_-]+',
    re.IGNORECASE
)

PROMO_CTA_PATTERNS = [
    re.compile(r'^\s*(?:Bizning\s+kanal|Kanalimiz|Kanalga\s+obuna\s+bo[\'ʼ`]?ling|A[\'ʼ`]?zo\s+bo[\'ʼ`]?ling|Kanalga\s+qo[\'ʼ`]?shiling)\s*[:\-–—]?\s*(?:@\w+|https?:\/\/t\.me\/\S+)\s*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:Подписывайтесь|Наш\s+канал|Канал|Ссылка\s+на\s+канал|Присоединяйтесь)\s*[:\-–—]?\s*(?:@\w+|https?:\/\/t\.me\/\S+)\s*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:Subscribe|Join\s+channel|Our\s+channel)\s*[:\-–—]?\s*(?:@\w+|https?:\/\/t\.me\/\S+)\s*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:👉|➡️|🔗|📱)?\s*@\w+\s*$', re.MULTILINE),
    re.compile(r'^\s*(?:👉|➡️|🔗|📱)?\s*https?:\/\/t\.me\/\S+\s*$', re.MULTILINE),
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
        if "/+" in s or "/joinchat/" in s:
            if not s.startswith("http"):
                s = "https://" + s
            return s

        # Strip URL prefixes and message IDs
        s = re.sub(r'^https?:\/\/(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/', '', s, flags=re.IGNORECASE)
        s = re.sub(r'^(?:t\.me|telegram\.me|telegram\.dog)\/', '', s, flags=re.IGNORECASE)

        # If has trailing post id like username/12345
        if "/" in s:
            s = s.split("/")[0]

        s = s.lstrip("@").strip()
        if s and not s.startswith("+"):
            return f"@{s}"
        return s

    @staticmethod
    def contains_blacklisted_words(text: str, blacklist: List[str]) -> bool:
        if not text or not blacklist:
            return False
        
        lower_text = text.lower()
        for word in blacklist:
            if word and word.lower() in lower_text:
                logger.info(f"Message blocked due to blacklisted word: '{word}'")
                return True
        return False

    @staticmethod
    def clean_links_and_usernames(text: str) -> str:
        if not text:
            return ""

        cleaned = text

        # 1. Clean HTML anchor tags pointing to Telegram links (preserve inner text if not purely CTA)
        cleaned = re.sub(
            r'<a\s+[^>]*href=["\']?(?:https?:\/\/)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/[^"\'>\s]+["\']?[^>]*>(.*?)<\/a>',
            r'\1',
            cleaned,
            flags=re.IGNORECASE | re.DOTALL
        )

        # 2. Clean CTA promo lines
        for promo_pattern in PROMO_CTA_PATTERNS:
            cleaned = promo_pattern.sub('', cleaned)

        # 3. Clean raw Telegram links, invite links and usernames
        cleaned = TG_JOINCHAT_PATTERN.sub('', cleaned)
        cleaned = TG_LINK_PATTERN.sub('', cleaned)
        cleaned = TG_USERNAME_PATTERN.sub('', cleaned)

        # 4. Clean residual empty or broken anchor tags
        cleaned = re.sub(r'<a\s+[^>]*href=["\']?\s*["\']?[^>]*>(.*?)<\/a>', r'\1', cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r'<a>(.*?)<\/a>', r'\1', cleaned, flags=re.IGNORECASE | re.DOTALL)

        # 5. Clean up dangling multiple line breaks
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()

    @staticmethod
    def apply_word_replacements(text: str, replace_dict: Dict[str, str]) -> str:
        if not text or not replace_dict:
            return text

        result = text
        for old_val, new_val in replace_dict.items():
            if old_val:
                result = re.sub(re.escape(old_val), new_val, result, flags=re.IGNORECASE)
        return result

    @staticmethod
    def attach_signature(text: str, signature: str) -> str:
        if not signature:
            return text
        
        signature = signature.strip()
        if not text:
            return signature
        
        return f"{text}\n\n{signature}"

    @classmethod
    def process_text(cls, raw_text: Optional[str], pair: ChannelPair) -> Optional[str]:
        if raw_text is None:
            raw_text = ""

        if pair.blacklist_list and cls.contains_blacklisted_words(raw_text, pair.blacklist_list):
            return None

        result = raw_text

        if pair.clean_links and result:
            result = cls.clean_links_and_usernames(result)

        if pair.replace_dict and result:
            result = cls.apply_word_replacements(result, pair.replace_dict)

        if pair.custom_signature and not pair.remove_signature:
            result = cls.attach_signature(result, pair.custom_signature)

        return result

    @classmethod
    def fit_caption_limit(cls, processed_text: str, max_limit: int = 1020) -> Tuple[str, Optional[str]]:
        if not processed_text or len(processed_text) <= max_limit:
            return processed_text, None

        cut_idx = processed_text[:max_limit].rfind("\n")
        if cut_idx == -1 or cut_idx < max_limit // 2:
            cut_idx = processed_text[:max_limit].rfind(" ")
        if cut_idx == -1:
            cut_idx = max_limit

        caption_part = processed_text[:cut_idx].strip()
        overflow_part = processed_text[cut_idx:].strip()

        # Check for unclosed HTML tags in caption_part
        open_tags = re.findall(r'<([a-zA-Z0-9_\-]+)(?:\s+[^>]*)?>', caption_part)
        close_tags = re.findall(r'</([a-zA-Z0-9_\-]+)>', caption_part)
        
        # Self-closing or void tags don't need closing
        unclosed = []
        for tag in open_tags:
            if close_tags.count(tag) < open_tags.count(tag) and tag not in unclosed:
                unclosed.append(tag)

        # Close tags in caption
        closing_suffix = "".join(f"</{tag}>" for tag in reversed(unclosed))
        opening_prefix = "".join(f"<{tag}>" for tag in unclosed)

        caption = caption_part + closing_suffix
        overflow = opening_prefix + overflow_part if overflow_part else None

        return caption, overflow
