from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.models import Subscription
from services.custom_emojis import ID_STARS, ID_CROWN, ID_HOME

def get_stars_plans_keyboard(sub: Subscription) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(text="Pro Tarif — 100 Stars (30 kun)", callback_data="buy_plan_pro", style="primary", icon_custom_emoji_id=ID_STARS)
        ],
        [
            InlineKeyboardButton(text="VIP Cheksiz — 300 Stars (30 kun)", callback_data="buy_plan_vip", style="success", icon_custom_emoji_id=ID_CROWN)
        ],
        [
            InlineKeyboardButton(text="Asosiy Menyu", callback_data="menu_main", style="danger", icon_custom_emoji_id=ID_HOME)
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_back_to_stars_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Tariflarga Qaytish", callback_data="menu_stars", style="primary", icon_custom_emoji_id=ID_STARS)
        ]
    ])
