from typing import Optional

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config.plans import PLANS, TIER_RANK
from database.models import Subscription
from services.custom_emojis import ID_STARS, ID_CROWN, ID_HOME


def active_paid_tier(sub: Optional[Subscription]) -> Optional[str]:
    """"pro" or "vip" while such a plan is active, otherwise None."""
    if sub is not None and sub.tier in ("pro", "vip") and sub.is_active:
        return sub.tier
    return None


def is_lower_tier_purchase(tier: str, sub: Optional[Subscription]) -> bool:
    """True when `tier` ranks below the plan that is currently active (the purchase could only be
    converted into extra time of the higher plan, so it is not offered)."""
    current = active_paid_tier(sub)
    return bool(current) and TIER_RANK.get(tier, 0) < TIER_RANK.get(current, 0)


def get_stars_plans_keyboard(sub: Subscription, is_admin: bool = False) -> InlineKeyboardMarkup:
    """Purchase buttons for the plans that make sense right now: a lower tier is never offered while a
    higher one is active, the active plan is offered as an extension and admins need no plan at all."""
    keyboard = []
    if not is_admin:
        current = active_paid_tier(sub)
        pro, vip = PLANS["pro"], PLANS["vip"]
        if not is_lower_tier_purchase("pro", sub):
            if current == "pro":
                pro_label = f"Pro tarifini uzaytirish — {pro['stars']} Stars (+{pro['days']} kun)"
            else:
                pro_label = f"Pro Tarif — {pro['stars']} Stars ({pro['days']} kun)"
            keyboard.append([
                InlineKeyboardButton(text=pro_label, callback_data="buy_plan_pro", style="primary", icon_custom_emoji_id=ID_STARS)
            ])
        if current == "vip":
            vip_label = f"VIP tarifini uzaytirish — {vip['stars']} Stars (+{vip['days']} kun)"
        elif current == "pro":
            vip_label = f"VIP tarifiga o'tish — {vip['stars']} Stars ({vip['days']} kun)"
        else:
            vip_label = f"VIP Cheksiz — {vip['stars']} Stars ({vip['days']} kun)"
        keyboard.append([
            InlineKeyboardButton(text=vip_label, callback_data="buy_plan_vip", style="success", icon_custom_emoji_id=ID_CROWN)
        ])
    keyboard.append([
        InlineKeyboardButton(text="Asosiy Menyu", callback_data="menu_main", style="danger", icon_custom_emoji_id=ID_HOME)
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
