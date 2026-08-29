from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from services.custom_emojis import (
    ID_CROWN, ID_KEY, ID_BROADCAST, ID_BACKUP, ID_INFO, ID_HOME,
    ID_LOGOUT, ID_SUCCESS, ID_ERROR, ID_REFRESH, ID_DOCUMENT
)

def get_admin_reply_keyboard() -> ReplyKeyboardMarkup:
    """Bottom persistent keyboard for Super Admins"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Boshqaruv Paneli", style="primary", icon_custom_emoji_id=ID_CROWN),
                KeyboardButton(text="Tizim Holati & Server", style="primary", icon_custom_emoji_id=ID_INFO)
            ],
            [
                KeyboardButton(text="MTProto Hisob", style="primary", icon_custom_emoji_id=ID_KEY),
                KeyboardButton(text="Xabar Tarqatish", style="primary", icon_custom_emoji_id=ID_BROADCAST)
            ],
            [
                KeyboardButton(text="Foydalanuvchilar", style="primary", icon_custom_emoji_id=ID_DOCUMENT),
                KeyboardButton(text="Baza Nusxasi (Backup)", style="success", icon_custom_emoji_id=ID_BACKUP)
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def get_admin_dashboard_keyboard(is_auth: bool = False) -> InlineKeyboardMarkup:
    auth_label = "MTProto: Ulangan" if is_auth else "MTProto: Ulanmagan"
    auth_style = "success" if is_auth else "danger"
    auth_icon = ID_SUCCESS if is_auth else ID_ERROR

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=auth_label, callback_data="admin_auth_status", style=auth_style, icon_custom_emoji_id=auth_icon),
            InlineKeyboardButton(text="Server Holati", callback_data="admin_system_status", style="primary", icon_custom_emoji_id=ID_INFO)
        ],
        [
            InlineKeyboardButton(text="Xabar Tarqatish (Broadcast)", callback_data="admin_broadcast_prompt", style="primary", icon_custom_emoji_id=ID_BROADCAST),
            InlineKeyboardButton(text="Foydalanuvchilar & Obunalar", callback_data="admin_users_list", style="primary", icon_custom_emoji_id=ID_DOCUMENT)
        ],
        [
            InlineKeyboardButton(text="Baza Backup (.db)", callback_data="admin_download_backup", style="success", icon_custom_emoji_id=ID_BACKUP),
            InlineKeyboardButton(text="Tinglovchini Qayta Yuklash", callback_data="admin_restart_listener", style="primary", icon_custom_emoji_id=ID_REFRESH)
        ]
    ])

def get_auth_menu_keyboard(is_auth: bool = False) -> InlineKeyboardMarkup:
    if is_auth:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Akkauntni Uzish (Logout)", callback_data="auth_logout_confirm", style="danger", icon_custom_emoji_id=ID_LOGOUT)
            ],
            [
                InlineKeyboardButton(text="Boshqaruv Paneliga Qaytish", callback_data="admin_main_dashboard", style="primary", icon_custom_emoji_id=ID_HOME)
            ]
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Telefon raqam orqali kirish", callback_data="auth_start_phone", style="success", icon_custom_emoji_id=ID_KEY)
            ],
            [
                InlineKeyboardButton(text="Boshqaruv Paneliga Qaytish", callback_data="admin_main_dashboard", style="primary", icon_custom_emoji_id=ID_HOME)
            ]
        ])

def get_logout_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Ha, uzilsin", callback_data="auth_logout_yes", style="danger", icon_custom_emoji_id=ID_LOGOUT),
            InlineKeyboardButton(text="Yo'q, bekor qilish", callback_data="admin_auth_status", style="success", icon_custom_emoji_id=ID_SUCCESS)
        ]
    ])

def get_auth_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Bekor qilish", callback_data="admin_auth_status", style="danger", icon_custom_emoji_id=ID_ERROR)]
    ])

def get_back_to_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Boshqaruv Paneliga Qaytish", callback_data="admin_main_dashboard", style="primary", icon_custom_emoji_id=ID_HOME)]
    ])
