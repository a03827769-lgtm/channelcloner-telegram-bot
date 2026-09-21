import re
import urllib.parse
import logging
from typing import Any, List, Optional, Tuple
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from services.custom_emojis import ID_LINK, ID_CART, ID_BOX, ID_DOCUMENT, ID_COIN, ID_GIFT

logger = logging.getLogger(__name__)

class DynamicAffiliateEngine:
    """
    Dynamic Affiliate & Smart CTA Button Engine.
    Detects product & promotional URLs in message texts, injects personalized affiliate tags,
    and constructs high-converting Inline Keyboard CTA buttons.
    """

    URL_REGEX = re.compile(
        r'https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)'
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

    def extract_and_convert_links(self, text: str, affiliate_rules: str = "") -> Tuple[str, List[Tuple]]:
        """
        Parses all URLs from the text, applies affiliate tag replacements,
        and returns the modified text along with a list of (button_label, target_url, icon_id).
        """
        if not text:
            return text, []

        found_links = self.URL_REGEX.findall(text)
        if not found_links:
            return text, []

        cta_buttons: List[Tuple] = []
        modified_text = text

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
            final_link = link
            if affiliate_rules:
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

            if final_link != link:
                pattern = re.compile(r'(^|[\s"\'\(])' + re.escape(link) + r'($|[\s"\'\)])')
                if pattern.search(modified_text):
                    modified_text = pattern.sub(lambda m, fl=final_link: f"{m.group(1)}{fl}{m.group(2)}", modified_text)
                else:
                    modified_text = modified_text.replace(link, final_link)

            cta_buttons.append((button_label, final_link, button_icon))

        # Deduplicate buttons by target URL while preserving order
        unique_buttons = []
        seen_urls = set()
        for btn in cta_buttons:
            btn_url = btn[1]
            if btn_url not in seen_urls:
                seen_urls.add(btn_url)
                unique_buttons.append(btn)

        return modified_text, unique_buttons

    def build_cta_keyboard(self, cta_buttons: List[Tuple]) -> Optional[InlineKeyboardMarkup]:
        """Constructs an Aiogram InlineKeyboardMarkup from extracted CTA links with Bot API 9.4 styles"""
        if not cta_buttons:
            return None

        keyboard = []
        # Limit to top 3 buttons to prevent oversized keyboards
        for item in cta_buttons[:3]:
            label = item[0]
            url = item[1]
            icon_id = item[2] if len(item) > 2 else ID_LINK
            if url and (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
                keyboard.append([InlineKeyboardButton(
                    text=label,
                    url=url,
                    style="primary",
                    icon_custom_emoji_id=icon_id
                )])

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
