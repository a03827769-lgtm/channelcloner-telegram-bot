from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from config.settings import settings
from services.custom_emojis import (
    ID_SETTINGS, ID_SERVER_CPU, ID_KEY, ID_BROADCAST, ID_USERS, ID_BACKUP,
    ID_SUCCESS, ID_SUCCESS_V2, ID_ERROR, ID_DOCUMENT, ID_REFRESH, ID_BACK,
    ID_LOCK_LOCKED, ID_LOCK_UNLOCKED, ID_SUPPORT, ID_VERIFIED, ID_STARS,
    ID_ARROW_LEFT, ID_ARROW_RIGHT
)

def get_admin_reply_keyboard() -> ReplyKeyboardMarkup:
    """Bottom persistent keyboard for Super Admins with Bot API 9.4 styles"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Boshqaruv Paneli", style="primary", icon_custom_emoji_id=ID_SETTINGS),
                KeyboardButton(text="Tizim Holati & Server", style="primary", icon_custom_emoji_id=ID_SERVER_CPU)
            ],
            [
                KeyboardButton(text="MTProto Hisob", style="primary", icon_custom_emoji_id=ID_KEY),
                KeyboardButton(text="Xabar Tarqatish", style="primary", icon_custom_emoji_id=ID_BROADCAST)
            ],
            [
                KeyboardButton(text="Foydalanuvchilar", style="primary", icon_custom_emoji_id=ID_USERS),
                KeyboardButton(text="Baza Nusxasi (Backup)", style="success", icon_custom_emoji_id=ID_BACKUP)
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def get_admin_dashboard_keyboard(is_auth: bool = False, is_private: bool = False) -> InlineKeyboardMarkup:
    if is_auth:
        auth_btn = InlineKeyboardButton(
            text="MTProto: Ulangan",
            icon_custom_emoji_id=ID_SUCCESS,
            style="success",
            callback_data="admin_auth_status"
        )
    else:
        auth_btn = InlineKeyboardButton(
            text="MTProto: Ulanmagan",
            icon_custom_emoji_id=ID_ERROR,
            style="danger",
            callback_data="admin_auth_status"
        )

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            auth_btn,
            InlineKeyboardButton(
                text="Server Holati",
                icon_custom_emoji_id=ID_SERVER_CPU,
                style="primary",
                callback_data="admin_system_status"
            )
        ],
        [
            InlineKeyboardButton(
                text="Xabar Tarqatish (Broadcast)",
                icon_custom_emoji_id=ID_BROADCAST,
                style="primary",
                callback_data="admin_broadcast_prompt"
            ),
            InlineKeyboardButton(
                text="Foydalanuvchilar & Obunalar",
                icon_custom_emoji_id=ID_USERS,
                style="primary",
                callback_data="admin_users_list"
            )
        ],
        [
            InlineKeyboardButton(
                text="Baza Backup (.db)",
                icon_custom_emoji_id=ID_BACKUP,
                style="success",
                callback_data="admin_download_backup"
            ),
            InlineKeyboardButton(
                text="Jonli Loglar",
                icon_custom_emoji_id=ID_DOCUMENT,
                style="primary",
                callback_data="admin_view_logs"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Bot Rejimi: {'Yopiq (Private)' if is_private else 'Ommaviy (Public)'}",
                icon_custom_emoji_id=ID_LOCK_LOCKED if is_private else ID_LOCK_UNLOCKED,
                style="danger" if is_private else "success",
                callback_data="admin_bot_mode"
            )
        ],
        [
            InlineKeyboardButton(
                text="Tinglovchini Qayta Yuklash",
                icon_custom_emoji_id=ID_REFRESH,
                style="danger",
                callback_data="admin_restart_listener"
            )
        ]
    ])

def get_auth_menu_keyboard(is_auth: bool = False) -> InlineKeyboardMarkup:
    if is_auth:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Akkauntni Uzish (Logout)",
                    icon_custom_emoji_id=ID_ERROR,
                    style="danger",
                    callback_data="auth_logout_confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Boshqaruv Paneliga Qaytish",
                    icon_custom_emoji_id=ID_SETTINGS,
                    style="primary",
                    callback_data="admin_main_dashboard"
                )
            ]
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Telefon raqam orqali kirish",
                    icon_custom_emoji_id=ID_KEY,
                    style="success",
                    callback_data="auth_start_phone"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Boshqaruv Paneliga Qaytish",
                    icon_custom_emoji_id=ID_SETTINGS,
                    style="primary",
                    callback_data="admin_main_dashboard"
                )
            ]
        ])

def get_logout_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Ha, uzilsin",
                icon_custom_emoji_id=ID_ERROR,
                style="danger",
                callback_data="auth_logout_yes"
            ),
            InlineKeyboardButton(
                text="Yo'q, bekor qilish",
                icon_custom_emoji_id=ID_BACK,
                style="primary",
                callback_data="admin_auth_status"
            )
        ]
    ])

def get_auth_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Bekor qilish",
                icon_custom_emoji_id=ID_ERROR,
                style="danger",
                callback_data="admin_auth_status"
            )
        ]
    ])

def get_back_to_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Boshqaruv Paneliga Qaytish",
                icon_custom_emoji_id=ID_SETTINGS,
                style="primary",
                callback_data="admin_main_dashboard"
            )
        ]
    ])

def get_bot_mode_keyboard(is_private: bool, support_username: str) -> InlineKeyboardMarkup:
    toggle_text = "Ommaviyga o'tkazish (Public)" if is_private else "Yopiqqa o'tkazish (Private)"
    toggle_icon = ID_SUCCESS_V2 if is_private else ID_LOCK_LOCKED
    toggle_style = "success" if is_private else "danger"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=toggle_text,
                icon_custom_emoji_id=toggle_icon,
                style=toggle_style,
                callback_data="admin_toggle_bot_mode"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Admin Aloqa: @{support_username}",
                icon_custom_emoji_id=ID_SUPPORT,
                style="primary",
                callback_data="admin_set_support_user"
            )
        ],
        [
            InlineKeyboardButton(
                text="Whitelist Ro'yxati",
                icon_custom_emoji_id=ID_USERS,
                style="primary",
                callback_data="admin_whitelist_list"
            ),
            InlineKeyboardButton(
                text="Ruxsat berish",
                icon_custom_emoji_id=ID_VERIFIED,
                style="success",
                callback_data="admin_whitelist_add"
            )
        ],
        [
            InlineKeyboardButton(
                text="50 Stars To'laganlar",
                icon_custom_emoji_id=ID_STARS,
                style="primary",
                callback_data="admin_whitelist_stars"
            )
        ],
        [
            InlineKeyboardButton(
                text="Boshqaruv Paneliga",
                icon_custom_emoji_id=ID_BACK,
                style="primary",
                callback_data="admin_main_dashboard"
            )
        ]
    ])

def get_whitelist_pagination_keyboard(users: list, page: int = 1, page_size: int = 5, source: str = "") -> InlineKeyboardMarkup:
    total_users = len(users)
    total_pages = max(1, (total_users + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * page_size
    page_items = users[start_idx:start_idx + page_size]

    rows = []
    # Add revoke button for each user on this page
    for u in page_items:
        uid = u["user_id"]
        uname = u.get("username")
        display = f"@{uname}" if uname else f"ID: {uid}"
        rows.append([
            InlineKeyboardButton(
                text=f"O'chirish ({display})",
                style="danger",
                icon_custom_emoji_id=ID_ERROR,
                callback_data=f"adm_wl_rm_{uid}"
            )
        ])

    # Navigation buttons
    nav_row = []
    if page > 1:
        nav_row.append(
            InlineKeyboardButton(
                text="Oldingi",
                icon_custom_emoji_id=ID_ARROW_LEFT,
                style="primary",
                callback_data=f"adm_wl_page_{page - 1}_{source}"
            )
        )
    nav_row.append(
        InlineKeyboardButton(
            text=f"{page}/{total_pages}",
            icon_custom_emoji_id=ID_DOCUMENT,
            style="primary",
            callback_data="noop"
        )
    )
    if page < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="Keyingi",
                icon_custom_emoji_id=ID_ARROW_RIGHT,
                style="primary",
                callback_data=f"adm_wl_page_{page + 1}_{source}"
            )
        )
    if nav_row:
        rows.append(nav_row)

    # Back button
    rows.append([
        InlineKeyboardButton(
            text="Bot Rejimi Menusiga",
            icon_custom_emoji_id=ID_BACK,
            style="primary",
            callback_data="admin_bot_mode"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_cancel_whitelist_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Bekor qilish",
                style="danger",
                icon_custom_emoji_id=ID_ERROR,
                callback_data="admin_bot_mode"
            )
        ]
    ])
