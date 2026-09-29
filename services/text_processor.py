import re
import html
import unicodedata
import logging
from typing import Optional, List, Dict, Tuple, Any, Callable, Set
from database.models import ChannelPair

logger = logging.getLogger(__name__)

# Common telegram link and username regex patterns.
# Every link pattern has a left boundary so that lookalike domains (about.me/john, site.com/t.me/x) are never cut.
_TG_HOSTS = r'(?:t\.me|telegram\.me|telegram\.dog)'
TG_USERNAME_PATTERN = re.compile(r'(?<![\w.-])@([a-zA-Z0-9_]{4,32})(?![\w.-]*\.[a-zA-Z]{2,})', re.IGNORECASE)
TG_LINK_PATTERN = re.compile(
    r'(?<![\w.@/-])(?:https?://)?(?:www\.)?'
    r'(?:[a-z0-9_]{3,32}\.' + _TG_HOSTS + r'(?:/[^\s<>"\'«»]*)?'   # username.t.me[/path]
    r'|' + _TG_HOSTS + r'/[^\s<>"\'«»]+)',                          # t.me/<anything incl. ?start=ref, +invite>
    re.IGNORECASE
)
TG_DEEP_LINK_PATTERN = re.compile(r'(?<![\w.-])tg://[^\s<>"\'«»]+', re.IGNORECASE)
_TRAILING_PUNCT_RE = re.compile(r'[.,!?:;)\]»]+$')
_TG_URL_RE = re.compile(
    r'^(?:https?://)?(?:www\.)?(?:[a-z0-9_-]{1,64}\.)?' + _TG_HOSTS + r'(?:[/?#]|$)|^tg:',
    re.IGNORECASE
)

WEB_URL_PATTERN = re.compile(
    r'https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{2,10}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=;]*)',
    re.IGNORECASE
)

# Promo / call-to-action lines. Evaluated with fullmatch() against ONE stripped, tag-free line at a time, so no
# pattern can span several lines (the previous MULTILINE ^\s*...\s*$ forms were cubic on runs of blank lines).
_CTA_LEAD = r'(?:(?:👉|➡️|➡|🔗|📱|👇|⬇️|⬇)\s*)?'
_CTA_SEP = r'\s*[:\-–—]?\s*'
_CTA_REF = r'(?:@\w+|(?:https?://)?(?:www\.)?(?:[\w-]{1,64}\.)?' + _TG_HOSTS + r'(?:/\S*)?|tg://\S+)'
PROMO_CTA_PATTERNS = [
    re.compile(_CTA_LEAD + r'(?:Bizning\s+kanal|Kanalimiz|Kanalga\s+obuna\s+bo\'?ling|A\'?zo\s+bo\'?ling|Kanalga\s+qo\'?shiling)' + _CTA_SEP + _CTA_REF, re.IGNORECASE),
    re.compile(_CTA_LEAD + r'(?:Подписывайтесь|Наш\s+канал|Канал|Ссылка\s+на\s+канал|Присоединяйтесь)' + _CTA_SEP + _CTA_REF, re.IGNORECASE),
    re.compile(_CTA_LEAD + r'(?:Subscribe|Join\s+(?:our\s+)?channel|Our\s+channel)' + _CTA_SEP + _CTA_REF, re.IGNORECASE),
    re.compile(_CTA_LEAD + r'(?:Переходите\s+по\s+ссылке|Перейти\s+по\s+ссылке|Подробнее\s+по\s+ссылке|Связь|Контакты|Связаться|Aloqa|Murojaat\s+uchun|Bog\'?lanish)' + _CTA_SEP + _CTA_REF, re.IGNORECASE),
    re.compile(_CTA_LEAD + r'@\w+'),
    re.compile(_CTA_LEAD + r'(?:https?://)?(?:www\.)?(?:[\w-]{1,64}\.)?' + _TG_HOSTS + r'/\S*', re.IGNORECASE),
]
# CTA labels left without their link/username once links were removed ("🔗 Переходите по ссылке:")
_DANGLING_LABEL_RE = re.compile(
    _CTA_LEAD + r'(?:Переходите\s+по\s+ссылке|Перейти\s+по\s+ссылке|Подробнее\s+по\s+ссылке|Ссылка\s+на\s+канал|Ссылка|Канал|Наш\s+канал|Подписывайтесь|Bizning\s+kanal|Kanalimiz|Kanal|Havola)\s*[:\-–—]?',
    re.IGNORECASE
)
# Lines longer than this are content, never a bare CTA line (also bounds the regex work per line)
_CTA_LINE_MAX_CHARS = 300

# --- Advertising / gambling lexicon (matched on normalized plain text, see normalize_for_matching) ---
# Explicit disclosures: the whole post is an advertisement
_AD_HASHTAG_RE = re.compile(r'(?<!\w)#(?:reklama|реклама|ad|ads|advertisement|sponsor|sponsored|hamkorlik|promoted|спонсор)(?!\w)', re.IGNORECASE)
_AD_HEADER_RE = re.compile(
    r'^[ \t]*(?:реклама|на[ \t]+правах[ \t]+рекламы|спонсорский[ \t]+пост|спонсор[ \t]+показа|hamkorlik[ \t]+asosida|tijoriy[ \t]+reklama|reklama)(?!\w)[ \t]*(?:[:\-–—][ \t]*(?P<value>[^\n]*)|$)',
    re.IGNORECASE | re.MULTILINE
)
# "Reklama va hamkorlik uchun: @admin" — the source channel's ad-contact footer (only that line is an ad).
# Contact items must be separated explicitly, so a run of digits can never be split ambiguously (no backtracking blow-up).
_CONTACT_ITEM = r'(?:@\w{1,32}|(?:https?://)?(?:www\.)?(?:[\w-]{1,64}\.)?' + _TG_HOSTS + r'/[^\s,;]+|\+?\d[\d \t()-]{5,20}\d)'
_CONTACT_LIST = _CONTACT_ITEM + r'(?:[ \t]*(?:,|;|/|\byoki\b|\bили\b)[ \t]*' + _CONTACT_ITEM + r')*'
_AD_CONTACT_LINE_RE = re.compile(
    r'(?:(?:reklama|реклама)(?:\s+(?:va|и)\s+(?:hamkorlik|сотрудничеств\w*))?(?:\s+(?:uchun|joyi|bo\'yicha|masalasida))?'
    r'|hamkorlik\s+uchun|по\s+вопросам\s+(?:рекламы|сотрудничества)|по\s+рекламе)'
    r'[ \t]*[:\-–—]?[ \t]*(?:' + _CONTACT_LIST + r')?',
    re.IGNORECASE
)
_CONTACT_VALUE_RE = re.compile(_CONTACT_LIST, re.IGNORECASE)
# Gambling operators (strong signal, still needs a second signal unless two brands appear)
_GAMBLING_BRAND_RE = re.compile(
    r'(?<!\w)(?:1\s?x\s?bet\w*|1win\w*|melbet\w*|mostbet\w*|pin-?up\w*|linebet\w*|pari-?match\w*|betandyou\w*|fonbet\w*'
    r'|olimpbet\w*|betwinner\w*|winline\w*|vavada\w*|22bet\w*|888starz\w*|megapari\w*|leonbet\w*|1xslots\w*|joycasino\w*)(?!\w)',
    re.IGNORECASE
)
# Casino game names — generic words on their own ("aviator", "mines")
_GAMBLING_GAME_RE = re.compile(
    r'(?<!\w)(?:aviator\w*|lucky\s?jet\w*|jetx|mines|plinko|sweet\s+bonanza|gates\s+of\s+olympus|crash\s+(?:o\'yin\w*|game\w*))(?!\w)',
    re.IGNORECASE
)
# Unambiguous gambling mechanics
_GAMBLING_TERM_RE = re.compile(
    r'(?<!\w)(?:kazino\w*|casino\w*|казино\w*|bukmeker\w*|букмекер\w*|frispin\w*|free\s?spin\w*|фриспин\w*|slotlar\w*|слоты|слотов'
    r'|игровые\s+автоматы|stavka\s+qiling|stavka\s+qo\'y\w*|pul\s+tik\w*|ставки\s+на\s+спорт|ставка\s+на\s+спорт|yutuqni\s+yechi\w*'
    r'|бонус\s+к\s+депозиту|выигрыш\s+в\s+казино)(?!\w)',
    re.IGNORECASE
)
# Generic commercial words: only ever counted in combination with other signals
_WEAK_PROMO_PATTERNS: Dict[str, re.Pattern] = {
    name: re.compile(r'(?<!\w)(?:' + pattern + r')(?!\w)', re.IGNORECASE) for name, pattern in {
        "bonus": r'bonus\w*|бонус\w*',
        "deposit": r'depozit\w*|депозит\w*',
        "promo_code": r'promo\s?kod\w*|promo\s?code\w*|промо\s?код\w*',
        "stake": r'stavka\w*|ставк\w*',
        "earn": r'pul\s+ishla\w*|заработ\w*|daromad\w*|доход\w*',
        "win": r'yutuq\w*|yutib\s+ol\w*|выигр\w*|jackpot\w*|джекпот\w*',
        "guarantee": r'kafolat\w*|гарант\w*',
        "signal": r'signal\w*|сигнал\w*',
        "insider": r'insayd\w*|инсайд\w*',
        "percent_bonus": r'\d+\s?%\s?bonus\w*|bonus[^\W\d_]*\s?\d+\s?%|\+\d+\s?%',
    }.items()
}
_SCAM_PHRASE_RE = re.compile(
    r'(?<!\w)(?:airdrop\w*|bepul\s+(?:ton|usdt|kripto\w*)|pul\s+ishlash\s+bot\w*|kunlik\s+daromad\s+\d+\s?%|ежедневн\w+\s+доход\s+\d+\s?%)(?!\w)',
    re.IGNORECASE
)
# Passenger / freight bulletin-board spam
_BULLETIN_SPAM_RE = re.compile(r'(?<!\w)(?:moshina\s+bor|damas\s+bor|pochta\s+bor\s+odam\s+bor|(?:yo\'?|yu)lovchi\s+kerak)(?!\w)', re.IGNORECASE)
_AD_LINK_RE = re.compile(r'(?:https?://|www\.)\S+|(?<![\w.@/-])' + _TG_HOSTS + r'/\S+|(?<![\w.-])@\w{4,32}', re.IGNORECASE)
_REFERRAL_RE = re.compile(r'(?<!\w)(?:referal\w*|referral\w*|реферал\w*)(?!\w)|[?&](?:start|ref|aff|partner)=\w', re.IGNORECASE)
# Applied to each link found by _AD_LINK_RE (plain substring search, linear)
_GAMBLING_URL_KEYWORD_RE = re.compile(r'bet|casino|kazino|stavk|1win|1xbet', re.IGNORECASE)

# Invisible characters used to obfuscate links/words. ZWJ is kept when it joins two emoji (👨‍💻, ❤️‍🔥).
_INVISIBLE_RE = re.compile(
    r'[​‌⁠﻿]'
    r'|(?<![\U0001F000-\U0001FAFF☀-➿️])‍'
    r'|‍(?![\U0001F000-\U0001FAFF☀-➿])'
)
_ALL_INVISIBLE_RE = re.compile(r'[­​-‍⁠﻿]')
_APOSTROPHES_RE = re.compile(r"[’‘ʻʼ`´]")
_TAG_SPLIT_RE = re.compile(r'(<[^>]*>)')
_TAG_NAME_RE = re.compile(r'<\s*(/)?\s*([a-zA-Z][a-zA-Z0-9_\-]*)')
_ANCHOR_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r'\bhref\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.IGNORECASE)
# Anchors whose href is missing or truly empty (never anchors carrying a URL or an ___AFF_PROT_n___ placeholder)
_EMPTY_ANCHOR_RE = re.compile(r'<a\b(?![^>]*\bhref\s*=\s*["\']?[^"\'\s>])[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_MD_TG_LINK_RE = re.compile(
    r'\[([^\]\n]{1,200})\]\((?:(?:https?://)?(?:www\.)?(?:[\w-]{1,64}\.)?' + _TG_HOSTS + r'/[^)\s]*|tg://[^)\s]*)\)',
    re.IGNORECASE
)
_EMPTY_TAG_PAIR_RE = re.compile(
    r'<(b|strong|i|em|u|ins|s|strike|del|span|tg-spoiler|a|tg-emoji|code|pre|blockquote)\b[^>]*></\1\s*>',
    re.IGNORECASE
)
# URLs, e-mails, @mentions and protected affiliate placeholders must never be touched by word replacements
_REPLACEMENT_MASK_RE = re.compile(
    r'(?:https?://|www\.|tg://)[^\s<>"\'«»]+'
    r'|(?<![\w.@/-])(?:[\w-]{1,64}\.)?' + _TG_HOSTS + r'/[^\s<>"\'«»]*'
    r'|___AFF_PROT_\d+___'
    r'|(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}\.[\w.-]{2,63}'
    r'|(?<![\w.-])@\w+',
    re.IGNORECASE
)
_SIG_KEYWORDS = r'(?:Manba|Kanal|Kanalimiz|Havola|Source|Channel|Подписаться|Подписывайтесь|Канал|Источник|Bizning\s+kanal|Admin|Админ)'
_SOURCE_SIGNATURE_RE = re.compile(
    r'(?:@\w{4,32}'
    r'|(?:https?://)?(?:www\.)?(?:[\w-]{1,64}\.)?' + _TG_HOSTS + r'/\S*'
    r'|' + _SIG_KEYWORDS + r'(?!\w)\s*[:\-–—]\s*\S.*'
    r'|' + _SIG_KEYWORDS + r'(?!\w)\s*(?:@\w+|(?:https?://)?' + _TG_HOSTS + r'/\S*))',
    re.IGNORECASE
)
_SIG_PREFIX_RE = re.compile(r'^(?:[\U00010000-\U0010ffff☀-➿⭐⌚-⏳▪-◾‍️•\-\*]\s*)+')
_SEPARATOR_LINE_RE = re.compile(r'[—–\-_=*•#\s]{3,}')


def utf16_len(text: Optional[str]) -> int:
    """Length in UTF-16 code units — the unit Telegram uses for every text limit."""
    if not text:
        return 0
    return len(text.encode('utf-16-le')) // 2


def truncate_utf16(text: Optional[str], limit: int, suffix: str = "") -> str:
    """Truncates plain text to at most `limit` UTF-16 code units (suffix included) without splitting a surrogate pair."""
    if not text:
        return ""
    if utf16_len(text) <= limit:
        return text
    budget = max(0, limit - utf16_len(suffix))
    out = []
    used = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if used + width > budget:
            break
        out.append(ch)
        used += width
    return "".join(out).rstrip() + suffix


def _is_tag(token: str) -> bool:
    return token.startswith("<") and token.endswith(">")


def _tag_name(token: str) -> Tuple[bool, str]:
    m = _TAG_NAME_RE.match(token)
    if not m:
        return False, ""
    return m.group(1) == "/", m.group(2).lower()


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

    # ------------------------------------------------------------------
    # Plain-text helpers (filters work on what the reader actually sees)
    # ------------------------------------------------------------------

    @staticmethod
    def html_to_plain(text: Optional[str]) -> str:
        """Visible text of a Telegram-HTML string: tags removed, entities unescaped."""
        if not text:
            return ""
        return html.unescape(re.sub(r'<[^>]*>', '', text))

    @staticmethod
    def normalize_for_matching(text: Optional[str], collapse_newlines: bool = False) -> str:
        """Canonical form for keyword matching: invisible characters removed, apostrophe variants unified,
        NFKC-normalized (NFKD would split letters such as 'й' and break Cyrillic keywords) and case-folded."""
        if not text:
            return ""
        s = _ALL_INVISIBLE_RE.sub('', text)
        s = _APOSTROPHES_RE.sub("'", s)
        s = unicodedata.normalize('NFKC', s).casefold()
        if collapse_newlines:
            return re.sub(r'\s+', ' ', s).strip()
        return re.sub(r'[ \t ]+', ' ', s)

    @staticmethod
    def utf16_len(text: Optional[str]) -> int:
        return utf16_len(text)

    @staticmethod
    def truncate_utf16(text: Optional[str], limit: int, suffix: str = "") -> str:
        return truncate_utf16(text, limit, suffix)

    @staticmethod
    def is_telegram_url(url: Optional[str]) -> bool:
        """True for t.me / telegram.me / telegram.dog links (incl. username.t.me) and tg:// deep links."""
        if not url:
            return False
        return bool(_TG_URL_RE.match(html.unescape(url).strip()))

    @staticmethod
    def _anchor_href(attrs: str) -> Optional[str]:
        m = _HREF_RE.search(attrs or "")
        if not m:
            return None
        return next((g for g in m.groups() if g is not None), "")

    @staticmethod
    def _split_html_lines(html_text: str) -> List[Tuple[List[str], bool]]:
        """Splits Telegram-HTML into lines of tokens (tags and text pieces). The flag tells whether the line lies
        (partly) inside a <pre> block. Tags never span lines in Telegram HTML, so every token belongs to one line."""
        lines: List[Tuple[List[str], bool]] = []
        current: List[str] = []
        pre_depth = 0
        current_in_pre = False
        for token in _TAG_SPLIT_RE.split(html_text):
            if not token:
                continue
            if _is_tag(token):
                current.append(token)
                closing, name = _tag_name(token)
                if name == "pre":
                    pre_depth = max(0, pre_depth - 1) if closing else pre_depth + 1
                    current_in_pre = True
                continue
            parts = token.split("\n")
            for idx, part in enumerate(parts):
                if idx:
                    lines.append((current, current_in_pre))
                    current = []
                    current_in_pre = pre_depth > 0
                if part:
                    current.append(part)
        lines.append((current, current_in_pre))
        return lines

    @classmethod
    def drop_empty_tags(cls, html_text: str) -> str:
        """Removes formatting pairs left without any content (e.g. <b></b>, <a href="x"></a>, <tg-emoji ...></tg-emoji>)."""
        if not html_text or "</" not in html_text:
            return html_text
        previous = None
        while previous != html_text:
            previous = html_text
            html_text = _EMPTY_TAG_PAIR_RE.sub('', html_text)
        return html_text

    @classmethod
    def remove_lines(cls, html_text: str, predicate: Callable[[str], bool], skip_pre: bool = True) -> str:
        """Removes every line whose visible text satisfies `predicate` while keeping the line's tags (attached to a
        neighbouring line), so removing a line can never leave an unbalanced tag behind. Lines inside <pre> are
        skipped by default (code, not promo)."""
        if not html_text:
            return html_text
        lines = cls._split_html_lines(html_text)
        out: List[str] = []
        carry = ""
        changed = False
        for tokens, in_pre in lines:
            visible = "".join(t for t in tokens if not _is_tag(t))
            plain = html.unescape(visible).strip()
            if plain and not (skip_pre and in_pre) and predicate(plain):
                tags = "".join(t for t in tokens if _is_tag(t))
                changed = True
                if out:
                    out[-1] += tags
                else:
                    carry += tags
                continue
            line_html = "".join(tokens)
            if carry:
                line_html = carry + line_html
                carry = ""
            out.append(line_html)
        if not changed:
            return html_text
        result = "\n".join(out) + carry
        return cls.drop_empty_tags(result)

    @staticmethod
    def _sub_outside_tags(html_text: str, func: Callable[[str], str], skip_pre: bool = True) -> str:
        """Applies func to text chunks only (never to tags or attributes) and, by default, not inside <pre>."""
        tokens = _TAG_SPLIT_RE.split(html_text)
        pre_depth = 0
        for i, token in enumerate(tokens):
            if not token:
                continue
            if _is_tag(token):
                closing, name = _tag_name(token)
                if name == "pre":
                    pre_depth = max(0, pre_depth - 1) if closing else pre_depth + 1
                continue
            if skip_pre and pre_depth:
                continue
            tokens[i] = func(token)
        return "".join(tokens)

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------

    @classmethod
    def contains_blacklisted_words(cls, text: str, blacklist: List[str]) -> bool:
        """Whole-word match on the visible text (tags stripped, entities unescaped, NFKC + casefold), with
        Unicode word boundaries: '<b>casino</b>', '🎰casino' and "o&#x27;yin" can no longer slip through."""
        if not text or not blacklist:
            return False
        haystack = cls.normalize_for_matching(cls.html_to_plain(text), collapse_newlines=True)
        if not haystack:
            return False
        for word in blacklist:
            if not word:
                continue
            needle = cls.normalize_for_matching(str(word), collapse_newlines=True)
            if not needle:
                continue
            if re.search(r'(?<!\w)' + re.escape(needle) + r'(?!\w)', haystack):
                logger.info(f"Message blocked due to blacklisted word: '{word}'")
                return True
        return False

    @staticmethod
    def collect_ad_signals(norm_text: str) -> Dict[str, Set[str]]:
        """Ad/gambling signals found in text already passed through normalize_for_matching()."""
        signals: Dict[str, Set[str]] = {
            "disclosure": set(), "contact": set(), "brands": set(), "games": set(), "strong": set(),
            "weak": set(), "scam": set(), "bulletin": set(), "links": set(), "referral": set(), "gambling_links": set(),
        }
        if not norm_text:
            return signals
        signals["disclosure"].update(m.group(0) for m in _AD_HASHTAG_RE.finditer(norm_text))
        for m in _AD_HEADER_RE.finditer(norm_text):
            value = (m.group("value") or "").strip()
            # "Реклама: @admin" is a contact footer, "Реклама: <offer text>" discloses an advertisement
            if value and len(value) <= _CTA_LINE_MAX_CHARS and _CONTACT_VALUE_RE.fullmatch(value):
                signals["contact"].add(m.group(0).strip())
            else:
                signals["disclosure"].add(m.group(0).strip())
        signals["brands"].update(m.group(0).replace(" ", "") for m in _GAMBLING_BRAND_RE.finditer(norm_text))
        signals["games"].update(m.group(0) for m in _GAMBLING_GAME_RE.finditer(norm_text))
        signals["strong"].update(m.group(0) for m in _GAMBLING_TERM_RE.finditer(norm_text))
        for name, pattern in _WEAK_PROMO_PATTERNS.items():
            if pattern.search(norm_text):
                signals["weak"].add(name)
        signals["scam"].update(m.group(0) for m in _SCAM_PHRASE_RE.finditer(norm_text))
        signals["bulletin"].update(m.group(0) for m in _BULLETIN_SPAM_RE.finditer(norm_text))
        signals["links"].update(m.group(0) for m in _AD_LINK_RE.finditer(norm_text))
        signals["referral"].update(m.group(0) for m in _REFERRAL_RE.finditer(norm_text))
        signals["gambling_links"].update(link for link in signals["links"] if _GAMBLING_URL_KEYWORD_RE.search(link))
        return signals

    @classmethod
    def classify_ad(cls, norm_text: str, signals: Optional[Dict[str, Set[str]]] = None) -> Optional[str]:
        """Returns the ad category of a normalized text ("sponsor", "bulletin", "casino", "crypto_spam",
        "referral") or None. Generic commercial words (bonus, depozit, promokod, stavka, aviator, ...) never
        decide alone: an ad needs an explicit disclosure or several independent signals."""
        s = signals if signals is not None else cls.collect_ad_signals(norm_text)
        if s["disclosure"]:
            return "sponsor"
        if s["bulletin"]:
            return "bulletin"
        has_cta = bool(s["links"] or s["referral"])
        if len(s["brands"]) >= 2:
            return "casino"
        if s["brands"] and (s["games"] or s["strong"] or s["weak"] or has_cta or s["gambling_links"]):
            return "casino"
        if s["gambling_links"] and (s["games"] or s["strong"] or s["weak"]):
            return "casino"
        if s["games"] and (s["strong"] or s["weak"]):
            return "casino"
        if s["strong"] and (len(s["strong"]) >= 2 or s["weak"] or has_cta):
            return "casino"
        if s["scam"] and (has_cta or s["weak"]):
            return "crypto_spam"
        if len(s["weak"]) >= 3:
            return "casino"
        if s["weak"] and s["referral"]:
            return "referral"
        return None

    @staticmethod
    def is_ad_contact_line(norm_line: str) -> bool:
        """True for a single normalized line that is only the source's ad-contact footer."""
        line = (norm_line or "").strip()
        if not line or len(line) > _CTA_LINE_MAX_CHARS:
            return False
        return bool(_AD_CONTACT_LINE_RE.fullmatch(line))

    @classmethod
    def is_commercial_ad(cls, text: str) -> bool:
        """
        Detects whether a post is a commercial advertisement, betting/casino promo,
        or explicit sponsored ad. Returns True if ad detected.
        """
        if not text:
            return False
        normalized_text = cls.normalize_for_matching(cls.html_to_plain(text))
        return cls.classify_ad(normalized_text) is not None

    # ------------------------------------------------------------------
    # Link cleaning
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_invisible(text: str) -> str:
        return _INVISIBLE_RE.sub('', text)

    @staticmethod
    def _cta_candidate(plain_line: str) -> Optional[str]:
        if len(plain_line) > _CTA_LINE_MAX_CHARS:
            return None
        s = _APOSTROPHES_RE.sub("'", _ALL_INVISIBLE_RE.sub('', plain_line))
        return unicodedata.normalize('NFKC', s).strip()

    @classmethod
    def _is_promo_cta_line(cls, plain_line: str) -> bool:
        line = cls._cta_candidate(plain_line)
        return bool(line) and any(p.fullmatch(line) for p in PROMO_CTA_PATTERNS)

    @classmethod
    def _is_dangling_label_line(cls, plain_line: str) -> bool:
        line = cls._cta_candidate(plain_line)
        return bool(line) and bool(_DANGLING_LABEL_RE.fullmatch(line))

    @staticmethod
    def _cut_link_keep_punct(match: re.Match) -> str:
        tail = _TRAILING_PUNCT_RE.search(match.group(0))
        return tail.group(0) if tail else ""

    @classmethod
    def _unwrap_telegram_anchor(cls, match: re.Match) -> str:
        href = cls._anchor_href(match.group(1))
        if href is not None and cls.is_telegram_url(href):
            return match.group(2)
        return match.group(0)

    @classmethod
    def clean_links_and_usernames(cls, text: str, remove_web_urls: bool = False) -> str:
        if not text:
            return ""

        cleaned = cls._strip_invisible(text)
        # Runs of blank lines never carry content; collapsing first keeps every later step linear
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

        # 1. Unwrap HTML anchors pointing to Telegram (t.me, telegram.me/.dog, username.t.me, tg://); keep inner text
        cleaned = _ANCHOR_RE.sub(cls._unwrap_telegram_anchor, cleaned)

        # 2. Clean Markdown formatted telegram links [text](https://t.me/...) or [text](tg://...)
        cleaned = _MD_TG_LINK_RE.sub(r'\1', cleaned)

        # 3. Clean CTA promo lines (one line at a time on its visible text; tags of removed lines are kept)
        cleaned = cls.remove_lines(cleaned, cls._is_promo_cta_line)

        # 4. Clean raw Telegram links, invite links, usernames, and deep links (only outside HTML tags and <pre>)
        def _clean_chunk(chunk: str) -> str:
            new_chunk = TG_LINK_PATTERN.sub(cls._cut_link_keep_punct, chunk)
            new_chunk = TG_DEEP_LINK_PATTERN.sub(cls._cut_link_keep_punct, new_chunk)
            new_chunk = TG_USERNAME_PATTERN.sub('', new_chunk)
            if remove_web_urls:
                new_chunk = WEB_URL_PATTERN.sub('', new_chunk)
            if new_chunk != chunk:
                new_chunk = re.sub(r'[ \t]{2,}', ' ', new_chunk)
            return new_chunk

        cleaned = cls._sub_outside_tags(cleaned, _clean_chunk)

        # 5. Unwrap anchors whose href is missing or truly empty (real links and affiliate placeholders stay)
        cleaned = _EMPTY_ANCHOR_RE.sub(r'\1', cleaned)

        # 6. Clean dangling promo CTA labels that now have no link/username
        cleaned = cls.remove_lines(cleaned, cls._is_dangling_label_line)

        # Clean ad hashtags when web URLs cleaning requested
        if remove_web_urls:
            cleaned = cls._sub_outside_tags(cleaned, lambda chunk: _AD_HASHTAG_RE.sub('', chunk))

        # 7. Clean up dangling multiple line breaks and emptied formatting pairs
        cleaned = cls.drop_empty_tags(cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()

    @classmethod
    def clean_for_clone(cls, text: str) -> Tuple[bool, str]:
        """Link-cleaning stage of the clone pipeline: (is_commercial_ad, cleaned_text). CPU-bound and synchronous,
        meant to run in a worker thread."""
        if cls.is_commercial_ad(text):
            return True, text
        return False, cls.clean_links_and_usernames(text)

    # ------------------------------------------------------------------
    # Word replacements & signatures
    # ------------------------------------------------------------------

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
        """Replaces whole words in the visible text only: never inside HTML tags/attributes, URLs, e-mails,
        @mentions or protected affiliate placeholders. Matching happens on unescaped text and the result is
        re-escaped, so 'AT&amp;T' matches 'AT&T' and a replacement containing '<' or '&' cannot break the HTML."""
        if not text or not replace_dict:
            return text

        sorted_replacements = sorted(
            [(k, v) for k, v in replace_dict.items() if k],
            key=lambda x: len(x[0]),
            reverse=True
        )
        if not sorted_replacements:
            return text
        compiled = [
            (
                re.compile(
                    r'(?:(?<=[\s\.,!?;:()\[\]{}"\'`ʻʼ«»—–\-_/\\|])|^)' + re.escape(old_val) + r'(?:(?=[\s\.,!?;:()\[\]{}"\'`ʻʼ«»—–\-_/\\|])|$)',
                    re.IGNORECASE
                ),
                new_val
            )
            for old_val, new_val in sorted_replacements
        ]

        def _replace_chunk(chunk: str) -> str:
            plain = html.unescape(chunk)
            masked: List[str] = []

            def _mask(m: re.Match) -> str:
                masked.append(m.group(0))
                return f"{chr(0xE000 + len(masked) - 1)}"

            work = _REPLACEMENT_MASK_RE.sub(_mask, plain)
            for pattern, new_val in compiled:
                work = pattern.sub(lambda m, v=new_val: cls._match_case(m.group(0), v), work)
            if masked:
                work = re.sub(r'(.)', lambda m: masked[ord(m.group(1)) - 0xE000], work)
            if work == plain:
                return chunk
            return html.escape(work, quote=False)

        # Tokenize by HTML tags so replacements are never applied inside HTML tags or attributes
        tokens = _TAG_SPLIT_RE.split(text)
        for i, token in enumerate(tokens):
            if token and not _is_tag(token):
                tokens[i] = _replace_chunk(token)
        return "".join(tokens)

    @classmethod
    def attach_signature(cls, text: str, signature: str, max_limit: int = 4096) -> str:
        """Appends the channel signature. The post body is never truncated: splitting into caption + overflow or
        several messages is done later by fit_caption_limit / fit_text_limit on the visible UTF-16 length.
        (`max_limit` is accepted for backward compatibility and no longer truncates anything.)"""
        if not signature:
            return text

        signature = signature.strip()
        if not signature:
            return text
        if not text or not text.strip():
            return signature

        if text.strip().endswith(signature):
            return text

        return f"{text.rstrip()}\n\n{signature}"

    @classmethod
    def strip_source_signature(cls, text: str) -> str:
        """Removes source channel signatures, author handles, and footer links from the end of the post text.
        A keyword line ("Kanal", "Admin", ...) only counts as a signature when it is followed by a separator or a
        handle/link, so ordinary last lines such as "Kanalizatsiya ta'mirlandi" or "Admin panel yangilandi" stay."""
        if not text:
            return ""
        lines = cls._split_html_lines(text.rstrip())
        removed_tags = ""
        while lines:
            tokens, in_pre = lines[-1]
            line_html = "".join(tokens)
            visible = "".join(t for t in tokens if not _is_tag(t))
            plain_line = html.unescape(visible).strip()
            if not plain_line:
                removed_tags = "".join(t for t in tokens if _is_tag(t)) + removed_tags
                lines.pop()
                continue
            if in_pre:
                break
            body = _SIG_PREFIX_RE.sub('', plain_line).strip()
            check_target = body or plain_line
            without_anchors = _ANCHOR_RE.sub('', line_html)
            anchor_only = (
                bool(_ANCHOR_RE.search(line_html))
                and not _SIG_PREFIX_RE.sub('', html.unescape(re.sub(r'<[^>]*>', '', without_anchors)).strip()).strip()
            )
            is_sig = (
                bool(_SOURCE_SIGNATURE_RE.fullmatch(check_target))
                or anchor_only
                or bool(_SEPARATOR_LINE_RE.fullmatch(plain_line))
            )
            if not is_sig:
                break
            removed_tags = "".join(t for t in tokens if _is_tag(t)) + removed_tags
            lines.pop()
        result = "\n".join("".join(tokens) for tokens, _ in lines).rstrip()
        if removed_tags:
            # Tags of removed lines are re-attached so formatting opened earlier is still closed properly
            result = cls.drop_empty_tags(result + removed_tags)
        return result

    @classmethod
    def process_text(cls, raw_text: Optional[str], pair: ChannelPair) -> Optional[str]:
        """Synchronous subset of ClonerEngine.process_post_text with the same order and the same gates:
        blacklist -> link/ad cleaning (only when the pair's "Link Tozalash" toggle is on) -> source signature
        removal -> word replacements -> own signature."""
        if raw_text is None:
            raw_text = ""

        if pair.blacklist_list and cls.contains_blacklisted_words(raw_text, pair.blacklist_list):
            return None

        result = raw_text

        if pair.clean_links and result.strip():
            is_ad, result = cls.clean_for_clone(result)
            if is_ad:
                logger.info("Message blocked: detected commercial advertisement/gambling post.")
                return None

        if pair.remove_signature and result:
            result = cls.strip_source_signature(result)

        if pair.replace_dict and result:
            result = cls.apply_word_replacements(result, pair.replace_dict)

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

    # Paragraph, line and sentence boundaries are preferred as the split point only while the part before
    # them keeps at least this share of the limit; otherwise the last space is used (a boundary near the
    # start would waste most of the caption)
    _MIN_SOFT_SPLIT_SHARE = 0.5
    # Split point kinds in order of preference
    _SPLIT_PATTERNS = (
        ("para", re.compile(r'\n[ \t]*\n')),
        ("line", re.compile(r'\n')),
        ("sentence", re.compile(r'(?<=[.!?…])[ \t]')),
        ("space", re.compile(r'[ \t]')),
    )
    _SENTENCE_END_RE = re.compile(r'[.!?…]\s*$')
    _LEADING_CLOSING_TAGS_RE = re.compile(r'(?:\s*</[a-zA-Z][^>]*>)+')

    @staticmethod
    def _visible_units(fragment: str) -> int:
        """Visible UTF-16 length of an HTML text fragment without tags."""
        return len(html.unescape(fragment).encode('utf-16-le')) // 2

    @classmethod
    def _hard_cut_in_token(cls, token: str, remaining_visible: int) -> int:
        """Longest prefix of a text token (in characters) whose visible length fits `remaining_visible`.
        HTML entities such as &quot; are never cut in half."""
        accum_vis = 0
        cut = 0
        for part in re.split(r'(&[a-zA-Z0-9#]+;)', token):
            if not part:
                continue
            part_vis = cls._visible_units(part)
            if accum_vis + part_vis <= remaining_visible:
                accum_vis += part_vis
                cut += len(part)
                continue
            if not (part.startswith('&') and part.endswith(';')):
                for ch in part:
                    ch_vis = len(ch.encode('utf-16-le')) // 2
                    if accum_vis + ch_vis > remaining_visible:
                        break
                    accum_vis += ch_vis
                    cut += len(ch)
            break
        return cut

    @classmethod
    def _find_split_index(cls, processed_text: str, max_limit: int) -> int:
        """Character index at which `processed_text` (visible length > max_limit) is split.

        Preference: the last paragraph break, line break or sentence end (each only while the first part
        keeps at least _MIN_SOFT_SPLIT_SHARE of the limit), then the last space, then an exact cut at the
        limit. Split points are only searched in the text tokens up to the limit, so a cut never falls
        inside a tag or an HTML entity."""
        tokens = re.split(r'(<[^>]+>)', processed_text)
        min_soft_visible = int(max_limit * cls._MIN_SOFT_SPLIT_SHARE)
        visible = 0
        char_pos = 0
        # kind -> (char index, visible length before it) of the last boundary of that kind seen so far
        best: Dict[str, Tuple[int, int]] = {}
        hard_cut = len(processed_text)

        for token in tokens:
            if not token:
                continue
            if token.startswith("<") and token.endswith(">"):
                char_pos += len(token)
                continue
            token_vis = cls._visible_units(token)
            fits = visible + token_vis <= max_limit
            usable = token if fits else token[:cls._hard_cut_in_token(token, max_limit - visible)]
            for kind, pattern in cls._SPLIT_PATTERNS:
                last = None
                for last in pattern.finditer(usable):
                    pass
                if last is not None:
                    best[kind] = (char_pos + last.start(), visible + cls._visible_units(usable[:last.start()]))
            if fits and cls._SENTENCE_END_RE.search(token):
                # A sentence that ends right before a tag ("...tugadi.</b>") is a sentence boundary too
                best["sentence"] = (char_pos + len(token), visible + token_vis)
            if not fits:
                hard_cut = char_pos + len(usable)
                break
            visible += token_vis
            char_pos += len(token)

        for kind in ("para", "line", "sentence"):
            if kind in best and best[kind][1] >= min_soft_visible:
                return best[kind][0]
        if "space" in best and best["space"][1] > 0:
            return best["space"][0]
        return hard_cut

    @classmethod
    def fit_caption_limit(cls, processed_text: str, max_limit: int = 1024, limit: Optional[int] = None) -> Tuple[str, Optional[str]]:
        """
        Fits caption text within Telegram's caption limit (1024 for standard Bot API, 2048 for Telegram Premium/Telethon).
        Only splits if the VISIBLE plain text length (UTF-16 units, tags excluded) exceeds max_limit, so
        formatting tags (<b>, <a>, <tg-emoji>) never cause a split. The split point is chosen by
        _find_split_index (paragraph > line > word > exact cut); formatting open at the split point is closed
        in the caption and reopened in the overflow.
        """
        if limit is not None:
            max_limit = limit

        if not processed_text:
            return "", None

        if cls.get_visible_text_length(processed_text) <= max_limit:
            return cls.ensure_closed_tags(processed_text), None

        cut_idx = cls._find_split_index(processed_text, max_limit)
        # Closing tags right after the cut end formatting of the caption's text: they stay in the caption
        # (otherwise the overflow would start with an empty re-opened element such as <b></b>)
        closing = cls._LEADING_CLOSING_TAGS_RE.match(processed_text, cut_idx)
        if closing:
            cut_idx = closing.end()
        # Opening tags right before the cut carry no text of the caption: they move to the overflow
        # (an empty <tg-emoji></tg-emoji> or <a></a> in the caption would be rejected by Telegram)
        dangling = re.search(r'(?:<[a-zA-Z][^>]*>\s*)+$', processed_text[:cut_idx])
        if dangling and dangling.start() > 0:
            cut_idx = dangling.start()

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
        Splits into multiple chunks only if visible plain text length exceeds 4096 (same split rules as
        fit_caption_limit). Ensures all chunks have properly closed HTML formatting tags.
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
            if not caption_part or overflow_part == remaining:
                # No progress possible (should not happen): keep the rest as one last chunk
                chunks.append(cls.ensure_closed_tags(remaining))
                break
            chunks.append(cls.ensure_closed_tags(caption_part))
            remaining = overflow_part or ""
        return [c for c in chunks if c.strip()] or [cls.ensure_closed_tags(processed_text)]
