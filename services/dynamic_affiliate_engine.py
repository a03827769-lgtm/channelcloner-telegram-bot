import re
import html
import urllib.parse
import logging
from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from services.custom_emojis import ID_LINK, ID_CART, ID_BOX, ID_DOCUMENT, ID_COIN, ID_GIFT

logger = logging.getLogger(__name__)

_TAG_SPLIT_RE = re.compile(r'(<[^>]*>)')
_HREF_RE = re.compile(r'(\bhref\s*=\s*)(?:"([^"]*)"|\'([^\']*)\')', re.IGNORECASE)


class DynamicAffiliateEngine:
    """
    Dynamic Affiliate & Smart CTA Button Engine.
    Detects product & promotional URLs in message texts, injects personalized affiliate tags,
    and constructs high-converting Inline Keyboard CTA buttons.
    """

    # Applied to UNESCAPED text, so a query string is never cut at the ';' of an '&amp;' entity
    URL_REGEX = re.compile(
        r'https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=;]*)'
    )

    MERCHANT_CTA_MAP = {
        "uzum.uz": ("Uzum'da ko'rish", "uzum", ID_CART),
        "aliexpress.com": ("AliExpress'da xarid qilish", "ali", ID_CART),
        "amazon.com": ("Amazon'dan buyurtma qilish", "amz", ID_BOX),
        "wildberries.ru": ("Wildberries'da ko'rish", "wb", ID_CART),
        "olx.uz": ("E'lonni ko'rish", "olx", ID_DOCUMENT),
        "binance.com": ("Binance'da ro'yxatdan o'tish", "binance", ID_COIN),
        "bybit.com": ("Bybit bonusini olish", "bybit", ID_GIFT)
    }

    @staticmethod
    def _strip_trailing_punct(url: str) -> str:
        return re.sub(r'[.,!?:;\'")]+$', '', url)

    def _find_links(self, text: str) -> List[str]:
        """Plain (unescaped) URLs found in anchor hrefs and in the visible text, in order of appearance."""
        links: List[str] = []
        for token in _TAG_SPLIT_RE.split(text):
            if not token:
                continue
            if token.startswith("<") and token.endswith(">"):
                m = _HREF_RE.search(token)
                if m:
                    href = html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
                    if href.startswith(("http://", "https://")):
                        links.append(href)
                continue
            for url in self.URL_REGEX.findall(html.unescape(token)):
                links.append(self._strip_trailing_punct(url))
        return links

    def _apply_rules(self, link: str, domain: str, affiliate_rules: str) -> str:
        final_link = link
        for rule in re.split(r'[\r\n,]+', affiliate_rules):
            rule = rule.strip()
            if "=>" in rule:
                k, v = rule.split("=>", 1)
            elif "->" in rule:
                k, v = rule.split("->", 1)
            elif "=" in rule:
                k, v = rule.split("=", 1)
            else:
                continue
            k, v = k.strip(), v.strip()
            if k and k in domain:
                if v.startswith("http://") or v.startswith("https://"):
                    final_link = v
                else:
                    try:
                        p_url = urllib.parse.urlparse(final_link)
                        q_dict = urllib.parse.parse_qs(p_url.query)
                        param_key = "ref"
                        if "amazon." in domain:
                            param_key = "tag"
                        elif "aliexpress." in domain:
                            param_key = "aff_id"
                        elif "uzum." in domain:
                            param_key = "p"
                        q_dict[param_key] = [v]
                        q_dict['utm_source'] = ['telegram_cloner']
                        new_query = urllib.parse.urlencode(q_dict, doseq=True)
                        final_link = urllib.parse.urlunparse(p_url._replace(query=new_query))
                    except Exception as e:
                        logger.debug(f"Error parsing affiliate url: {e}")
        return final_link

    def extract_and_convert_links(self, text: str, affiliate_rules: str = "") -> Tuple[str, List[Tuple]]:
        """
        Parses all URLs from the (Telegram-HTML) text, applies affiliate tag replacements,
        and returns the modified text along with a list of (button_label, target_url, icon_id).
        URLs are unescaped before parsing; rewritten URLs are HTML-escaped when written back into the text,
        while the button URLs stay plain.
        """
        if not text:
            return text, []

        found_links = self._find_links(text)
        if not found_links:
            return text, []

        cta_buttons: List[Tuple] = []
        rewrites: Dict[str, str] = {}

        for link in found_links:
            parsed = urllib.parse.urlparse(link)
            domain = parsed.netloc.lower().replace("www.", "")

            # Determine button text based on domain
            button_label = "Havolaga o'tish"
            button_icon = ID_LINK
            for merchant_domain, item in self.MERCHANT_CTA_MAP.items():
                if merchant_domain in domain:
                    button_label = item[0]
                    button_icon = item[2]
                    break

            # Process affiliate rules if provided
            final_link = self._apply_rules(link, domain, affiliate_rules) if affiliate_rules else link
            if final_link != link:
                rewrites[link] = final_link

            cta_buttons.append((button_label, final_link, button_icon))

        modified_text = text
        if rewrites:
            modified_text = self._rewrite_text(text, rewrites)

        # Deduplicate buttons by target URL while preserving order
        unique_buttons = []
        seen_urls = set()
        for btn in cta_buttons:
            btn_url = btn[1]
            if btn_url not in seen_urls:
                seen_urls.add(btn_url)
                unique_buttons.append(btn)

        return modified_text, unique_buttons

    @staticmethod
    def _rewrite_text(text: str, rewrites: Dict[str, str]) -> str:
        """Writes rewritten URLs back into hrefs and visible text (escaped form in both)."""
        escaped_rewrites = {html.escape(old, quote=True): html.escape(new, quote=True) for old, new in rewrites.items()}
        escaped_rewrites.update({html.escape(old, quote=False): html.escape(new, quote=False) for old, new in rewrites.items()})
        tokens = _TAG_SPLIT_RE.split(text)
        for i, token in enumerate(tokens):
            if not token:
                continue
            if token.startswith("<") and token.endswith(">"):
                def _swap_href(m: re.Match) -> str:
                    raw = m.group(2) if m.group(2) is not None else m.group(3)
                    new = rewrites.get(html.unescape(raw))
                    if new is None:
                        return m.group(0)
                    return f'{m.group(1)}"{html.escape(new, quote=True)}"'
                tokens[i] = _HREF_RE.sub(_swap_href, token)
                continue
            for old, new in sorted(escaped_rewrites.items(), key=lambda kv: len(kv[0]), reverse=True):
                pattern = re.compile(r'(^|[\s"\'\(])' + re.escape(old) + r'(?=$|[\s"\'\).,!?:;])')
                token = pattern.sub(lambda m, fl=new: f"{m.group(1)}{fl}", token)
            tokens[i] = token
        return "".join(tokens)

    def build_cta_keyboard(self, cta_buttons: List[Tuple], with_icons: bool = True) -> Optional[InlineKeyboardMarkup]:
        """Constructs an Aiogram InlineKeyboardMarkup from extracted CTA links with Bot API 9.4 styles.

        with_icons=False omits icon_custom_emoji_id: bots may not use custom emoji button icons in channel posts."""
        if not cta_buttons:
            return None

        keyboard = []
        # Limit to top 3 buttons to prevent oversized keyboards
        for item in cta_buttons[:3]:
            label = item[0]
            url = item[1]
            icon_id = item[2] if len(item) > 2 else ID_LINK
            if url and (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
                button_kwargs: Dict[str, Any] = {"text": label, "url": url, "style": "primary"}
                if with_icons and icon_id:
                    button_kwargs["icon_custom_emoji_id"] = icon_id
                keyboard.append([InlineKeyboardButton(**button_kwargs)])

        return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None

    def build_telethon_buttons(self, cta_buttons: List[Tuple]) -> Optional[List[Any]]:
        """Constructs Telethon KeyboardButtonUrl list (stacked vertically in rows) from extracted CTA links"""
        if not cta_buttons:
            return None
        from telethon import Button
        button_rows = []
        for item in cta_buttons[:3]:
            label = item[0]
            url = item[1]
            if url and (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
                button_rows.append([Button.url(label, url)])
        return button_rows if button_rows else None

dynamic_affiliate_engine = DynamicAffiliateEngine()
