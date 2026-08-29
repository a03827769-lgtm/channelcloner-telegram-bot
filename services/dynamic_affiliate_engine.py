import re
import urllib.parse
from typing import List, Optional, Tuple
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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
        "uzum.uz": ("🛒 Uzum'da ko'rish", "uzum"),
        "aliexpress.com": ("🔥 AliExpress'da xarid qilish", "ali"),
        "amazon.com": ("📦 Amazon'dan buyurtma qilish", "amz"),
        "wildberries.ru": ("🛍 Wildberries'da ko'rish", "wb"),
        "olx.uz": ("📋 E'lonni ko'rish", "olx"),
        "binance.com": ("💰 Binance'da ro'yxatdan o'tish", "binance"),
        "bybit.com": ("🎁 Bybit bonusini olish", "bybit")
    }

    def extract_and_convert_links(self, text: str, affiliate_rules: str = "") -> Tuple[str, List[Tuple[str, str]]]:
        """
        Parses all URLs from the text, applies affiliate tag replacements,
        and returns the modified text along with a list of (button_label, target_url).
        """
        if not text:
            return text, []

        found_links = self.URL_REGEX.findall(text)
        if not found_links:
            return text, []

        cta_buttons: List[Tuple[str, str]] = []
        modified_text = text

        for link in found_links:
            parsed = urllib.parse.urlparse(link)
            domain = parsed.netloc.lower().replace("www.", "")

            # Determine button text based on domain
            button_label = "👉 Havolaga o'tish"
            for merchant_domain, (label, _) in self.MERCHANT_CTA_MAP.items():
                if merchant_domain in domain:
                    button_label = label
                    break

            # Process affiliate rules if provided
            final_link = link
            if affiliate_rules:
                for rule in affiliate_rules.split(","):
                    if "=" in rule:
                        k, v = rule.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k and k in domain:
                            # Append or replace ref param
                            sep = "&" if "?" in final_link else "?"
                            final_link = f"{final_link}{sep}ref={v}&utm_source=telegram_cloner"

            cta_buttons.append((button_label, final_link))

        return modified_text, cta_buttons

    def build_cta_keyboard(self, cta_buttons: List[Tuple[str, str]]) -> Optional[InlineKeyboardMarkup]:
        """Constructs an Aiogram InlineKeyboardMarkup from extracted CTA links"""
        if not cta_buttons:
            return None

        keyboard = []
        # Limit to top 3 buttons to prevent oversized keyboards
        for label, url in cta_buttons[:3]:
            keyboard.append([InlineKeyboardButton(text=label, url=url)])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

dynamic_affiliate_engine = DynamicAffiliateEngine()
