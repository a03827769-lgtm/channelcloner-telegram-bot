from typing import List, Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.models import StorySettings, StorySourceChannel
from services.custom_emojis import (
    ID_SUCCESS, ID_ERROR, ID_CROWN, ID_STARS, ID_HOME, ID_BACK, ID_CHANNEL,
    ID_FILTER, ID_TIMER, ID_PALETTE, ID_STATS, ID_ROCKET, ID_FLASH_GREEN,
    ID_PAUSE, ID_REFRESH, ID_FAQ, ID_LOGOUT, ID_LINK, ID_MONEY, ID_IMAGE,
    ID_BAN, ID_TAG, ID_CHECK_GREEN_SQUARE, ID_DIAMOND, ID_USER, ID_EDIT,
    ID_SWITCH_ON, ID_TRASH, ID_TARGET, ID_CLOCK, ID_CALENDAR, ID_QUEUE, ID_PIN
)


def get_story_main_menu_keyboard(is_auth: bool, is_active: bool, has_source: bool = False) -> InlineKeyboardMarkup:
    """
    Main Story Cloner control keyboard with official Bot API 9.4 button styles
    (primary, success, danger) and animated Telegram Premium custom emoji icons.
    """
    if is_auth:
        auth_btn = InlineKeyboardButton(
            text="Telegram Hisob (Ulangan)",
            style="success",
            icon_custom_emoji_id=ID_SUCCESS,
            callback_data="story_menu_auth"
        )
    else:
        auth_btn = InlineKeyboardButton(
            text="Telegram Hisob (Ulanmagan)",
            style="danger",
            icon_custom_emoji_id=ID_ERROR,
            callback_data="story_menu_auth"
        )

    if is_active:
        auto_btn = InlineKeyboardButton(
            text="Monitoring: YOQILGAN",
            style="success",
            icon_custom_emoji_id=ID_SUCCESS,
            callback_data="story_toggle_active"
        )
    else:
        auto_btn = InlineKeyboardButton(
            text="Monitoring: TO'XTATILGAN",
            style="danger",
            icon_custom_emoji_id=ID_PAUSE,
            callback_data="story_toggle_active"
        )

    keyboard = [
        [auth_btn],
        [
            InlineKeyboardButton(
                text="Manba Kanallar",
                style="primary",
                icon_custom_emoji_id=ID_CHANNEL,
                callback_data="story_menu_channels"
            ),
            InlineKeyboardButton(
                text="Narx & Filtrlar",
                style="primary",
                icon_custom_emoji_id=ID_FILTER,
                callback_data="story_menu_filters"
            )
        ],
        [
            InlineKeyboardButton(
                text="Navbat & Prime Time",
                style="primary",
                icon_custom_emoji_id=ID_TIMER,
                callback_data="story_menu_queue"
            ),
            InlineKeyboardButton(
                text="Istoriya Dizayni",
                style="primary",
                icon_custom_emoji_id=ID_PALETTE,
                callback_data="story_menu_design"
            )
        ],
        [
            InlineKeyboardButton(
                text="Statistika & Tarix",
                style="primary",
                icon_custom_emoji_id=ID_STATS,
                callback_data="story_menu_stats"
            ),
            InlineKeyboardButton(
                text="Test Istoriya Joylash",
                style="success",
                icon_custom_emoji_id=ID_ROCKET,
                callback_data="story_menu_test_post"
            )
        ],
        [auto_btn],
        [
            InlineKeyboardButton(
                text="Yangilash",
                style="primary",
                icon_custom_emoji_id=ID_REFRESH,
                callback_data="story_main_menu"
            ),
            InlineKeyboardButton(
                text="Qo'llanma",
                style="primary",
                icon_custom_emoji_id=ID_FAQ,
                callback_data="story_menu_help"
            )
        ],
        [
            InlineKeyboardButton(
                text="Asosiy Menyuga Qaytish",
                style="danger",
                icon_custom_emoji_id=ID_HOME,
                callback_data="menu_main"
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_auth_keyboard(is_auth: bool) -> InlineKeyboardMarkup:
    if is_auth:
        keyboard = [
            [
                InlineKeyboardButton(
                    text="Hisobdan Chiqish (Logout)",
                    style="danger",
                    icon_custom_emoji_id=ID_LOGOUT,
                    callback_data="story_auth_logout_confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Istoriya Menyusiga Qaytish",
                    style="primary",
                    icon_custom_emoji_id=ID_FLASH_GREEN,
                    callback_data="story_main_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Asosiy Menyuga Qaytish",
                    style="danger",
                    icon_custom_emoji_id=ID_BACK,
                    callback_data="menu_main"
                )
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton(
                    text="Telegram Hisobni Ulash (OTP)",
                    style="success",
                    icon_custom_emoji_id=ID_SUCCESS,
                    callback_data="story_auth_start"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Istoriya Menyusiga Qaytish",
                    style="primary",
                    icon_custom_emoji_id=ID_FLASH_GREEN,
                    callback_data="story_main_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Asosiy Menyuga Qaytish",
                    style="danger",
                    icon_custom_emoji_id=ID_BACK,
                    callback_data="menu_main"
                )
            ]
        ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_logout_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Ha, Hisobdan Chiqish (Logout)",
                style="danger",
                icon_custom_emoji_id=ID_ERROR,
                callback_data="story_auth_logout_yes"
            )
        ],
        [
            InlineKeyboardButton(
                text="Bekor Qilish (Orqaga)",
                style="primary",
                icon_custom_emoji_id=ID_BACK,
                callback_data="story_menu_auth"
            )
        ]
    ])


def get_story_filters_keyboard(st: StorySettings) -> InlineKeyboardMarkup:
    price_btn = InlineKeyboardButton(
        text=f"Minimal Narx: ${st.min_price:g}",
        style="primary",
        icon_custom_emoji_id=ID_MONEY,
        callback_data="story_filter_price_menu"
    )

    photos_style = "success" if st.require_photos else "primary"
    photos_icon = ID_SUCCESS if st.require_photos else ID_IMAGE
    photos_btn = InlineKeyboardButton(
        text="Rasmli Postlar: MAJBURIY" if st.require_photos else "Rasmli Postlar: Ixtiyoriy",
        style=photos_style,
        icon_custom_emoji_id=photos_icon,
        callback_data="story_toggle_photos"
    )

    price_req_style = "success" if st.require_price else "primary"
    price_req_icon = ID_SUCCESS if st.require_price else ID_TAG
    price_req_btn = InlineKeyboardButton(
        text="Narx Ko'rsatilishi: MAJBURIY" if st.require_price else "Narx Ko'rsatilishi: Ixtiyoriy",
        style=price_req_style,
        icon_custom_emoji_id=price_req_icon,
        callback_data="story_toggle_price_req"
    )

    demand_style = "success" if st.filter_demands else "primary"
    demand_icon = ID_SUCCESS if st.filter_demands else ID_FILTER
    demand_btn = InlineKeyboardButton(
        text="Qidiruvlarni Filtrlash: YOQILGAN" if st.filter_demands else "Qidiruvlarni Filtrlash: O'CHIQ",
        style=demand_style,
        icon_custom_emoji_id=demand_icon,
        callback_data="story_toggle_demands"
    )

    badges_style = "success" if st.enable_smart_badges else "primary"
    badges_icon = ID_SUCCESS if st.enable_smart_badges else ID_CHECK_GREEN_SQUARE
    badges_btn = InlineKeyboardButton(
        text="Aqlli Badjlar: YOQILGAN" if st.enable_smart_badges else "Aqlli Badjlar: O'CHIQ",
        style=badges_style,
        icon_custom_emoji_id=badges_icon,
        callback_data="story_toggle_badges"
    )

    back_btn = InlineKeyboardButton(
        text="Orqaga",
        style="danger",
        icon_custom_emoji_id=ID_BACK,
        callback_data="story_main_menu"
    )

    keyboard = [
        [price_btn],
        [photos_btn],
        [price_req_btn],
        [demand_btn],
        [badges_btn],
        [back_btn]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_price_preset_keyboard(current_price: float) -> InlineKeyboardMarkup:
    presets = [500, 700, 800, 1000, 1200, 1500]
    row1 = []
    row2 = []
    for p in presets[:3]:
        is_sel = abs(p - current_price) < 0.1
        row1.append(InlineKeyboardButton(
            text=f"${p} (Tanlangan)" if is_sel else f"${p}",
            style="success" if is_sel else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_sel else ID_MONEY,
            callback_data=f"story_set_price_{p}"
        ))
    for p in presets[3:]:
        is_sel = abs(p - current_price) < 0.1
        row2.append(InlineKeyboardButton(
            text=f"${p} (Tanlangan)" if is_sel else f"${p}",
            style="success" if is_sel else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_sel else ID_MONEY,
            callback_data=f"story_set_price_{p}"
        ))

    keyboard = [
        row1,
        row2,
        [
            InlineKeyboardButton(
                text="Boshqa Narx Kiritish...",
                style="primary",
                icon_custom_emoji_id=ID_EDIT,
                callback_data="story_set_price_custom"
            )
        ],
        [
            InlineKeyboardButton(
                text="Orqaga",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data="story_menu_filters"
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_design_keyboard(current_style: str, video_duration: int = 25, enable_ai_voice: bool = True) -> InlineKeyboardMarkup:
    styles = [
        ("telegram_green", "Telegram Yashil (Nativ)", ID_PALETTE),
        ("listing_blur", "Xiralashgan Kvartira Rasmi", ID_IMAGE),
        ("luxury_dark", "To'q Lux Gradiyent", ID_CROWN),
        ("emerald", "Zumrad Yashil Gradiyent", ID_DIAMOND)
    ]
    keyboard = []
    for code, label, fallback_icon in styles:
        is_active = (code == current_style)
        keyboard.append([
            InlineKeyboardButton(
                text=f"{label} (Faol)" if is_active else label,
                style="success" if is_active else "primary",
                icon_custom_emoji_id=ID_SUCCESS if is_active else fallback_icon,
                callback_data=f"story_set_style_{code}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text=f"Video Davomiyligi: {video_duration} soniya",
            style="primary",
            icon_custom_emoji_id=ID_TIMER,
            callback_data="story_menu_duration"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            text="Musiqa: 20 ta Luxury Trek (Faol)",
            style="success",
            icon_custom_emoji_id=ID_ROCKET,
            callback_data="story_music_info"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            text="Bosh Menyuga Qaytish",
            style="danger",
            icon_custom_emoji_id=ID_BACK,
            callback_data="story_main_menu"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_duration_keyboard(current_duration: int = 25) -> InlineKeyboardMarkup:
    """
    Renders video duration selector with presets (15s, 20s, 25s, 30s, 35s, 40s),
    stepper buttons (-5s, +5s), and custom input option (strictly 15s - 40s).
    """
    presets = [15, 20, 25, 30, 35, 40]
    row1 = []
    row2 = []
    for d in presets[:3]:
        is_sel = (d == current_duration)
        row1.append(InlineKeyboardButton(
            text=f"✅ {d}s (Faol)" if is_sel else f"{d}s",
            style="success" if is_sel else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_sel else ID_TIMER,
            callback_data=f"story_set_duration_{d}"
        ))
    for d in presets[3:]:
        is_sel = (d == current_duration)
        row2.append(InlineKeyboardButton(
            text=f"✅ {d}s (Faol)" if is_sel else f"{d}s",
            style="success" if is_sel else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_sel else ID_TIMER,
            callback_data=f"story_set_duration_{d}"
        ))

    prev_val = max(15, current_duration - 5)
    next_val = min(40, current_duration + 5)
    stepper_row = [
        InlineKeyboardButton(
            text=f"-5s ({prev_val}s)",
            style="primary",
            icon_custom_emoji_id=ID_TIMER,
            callback_data=f"story_set_duration_{prev_val}"
        ),
        InlineKeyboardButton(
            text=f"+5s ({next_val}s)",
            style="primary",
            icon_custom_emoji_id=ID_TIMER,
            callback_data=f"story_set_duration_{next_val}"
        )
    ]

    custom_btn = [
        InlineKeyboardButton(
            text="Boshqa Davomiylik (15-40s)...",
            style="primary",
            icon_custom_emoji_id=ID_EDIT,
            callback_data="story_set_duration_custom"
        )
    ]

    back_btn = [
        InlineKeyboardButton(
            text="Orqaga",
            style="danger",
            icon_custom_emoji_id=ID_BACK,
            callback_data="story_menu_design"
        )
    ]

    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, stepper_row, custom_btn, back_btn])


def get_story_channel_keyboard(current_source: str, target_type: str) -> InlineKeyboardMarkup:
    target_label = "Joylash: Shaxsiy Profil" if target_type == "self" else "Joylash: Kanal Istoriyasi"
    target_icon = ID_USER if target_type == "self" else ID_CHANNEL
    target_style = "success" if target_type == "self" else "primary"

    keyboard = [
        [
            InlineKeyboardButton(
                text="Manba Kanalni O'zgartirish",
                style="primary",
                icon_custom_emoji_id=ID_EDIT,
                callback_data="story_edit_channel"
            )
        ],
        [
            InlineKeyboardButton(
                text=target_label,
                style=target_style,
                icon_custom_emoji_id=target_icon,
                callback_data="story_toggle_target"
            )
        ],
        [
            InlineKeyboardButton(
                text="Bosh Menyuga Qaytish",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data="story_main_menu"
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_channels_keyboard(
    primary_channel: Optional[str],
    extra_channels: List[StorySourceChannel],
    target_type: str = "self"
) -> InlineKeyboardMarkup:
    keyboard = []

    # Primary channel row
    if primary_channel:
        keyboard.append([
            InlineKeyboardButton(
                text=f"Asosiy: {primary_channel}",
                style="primary",
                icon_custom_emoji_id=ID_CHANNEL,
                callback_data="story_channel_primary_info"
            ),
            InlineKeyboardButton(
                text="O'zgartirish",
                style="primary",
                icon_custom_emoji_id=ID_EDIT,
                callback_data="story_edit_channel"
            )
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(
                text="Asosiy Kanalni Kiritish",
                style="success",
                icon_custom_emoji_id=ID_CHANNEL,
                callback_data="story_edit_channel"
            )
        ])

    # Extra channels rows
    for ch in extra_channels:
        if ch.is_active:
            ch_btn = InlineKeyboardButton(
                text=f"{ch.channel_username}",
                style="primary",
                icon_custom_emoji_id=ID_CHANNEL,
                callback_data=f"story_toggle_src_{ch.id}"
            )
        else:
            ch_btn = InlineKeyboardButton(
                text=f"{ch.channel_username} (Pauza)",
                style="danger",
                icon_custom_emoji_id=ID_PAUSE,
                callback_data=f"story_toggle_src_{ch.id}"
            )

        del_btn = InlineKeyboardButton(
            text="O'chirish",
            style="danger",
            icon_custom_emoji_id=ID_TRASH,
            callback_data=f"story_del_src_{ch.id}"
        )
        keyboard.append([ch_btn, del_btn])

    # Add extra channel button
    keyboard.append([
        InlineKeyboardButton(
            text="Yangi Kanal Qo'shish (Multi-Manba)",
            style="success",
            icon_custom_emoji_id=ID_ROCKET,
            callback_data="story_add_extra_channel"
        )
    ])

    # Target toggle
    target_label = "Joylash: Shaxsiy Profil" if target_type == "self" else "Joylash: Kanal Istoriyasi"
    target_icon = ID_USER if target_type == "self" else ID_CHANNEL
    target_style = "success" if target_type == "self" else "primary"
    keyboard.append([
        InlineKeyboardButton(
            text=target_label,
            style=target_style,
            icon_custom_emoji_id=target_icon,
            callback_data="story_toggle_target"
        )
    ])

    # Back
    keyboard.append([
        InlineKeyboardButton(
            text="Bosh Menyuga Qaytish",
            style="danger",
            icon_custom_emoji_id=ID_BACK,
            callback_data="story_main_menu"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_queue_keyboard(st: StorySettings, pending_count: int = 0) -> InlineKeyboardMarkup:
    prime_style = "success" if st.prime_hours_enabled else "primary"
    prime_icon = ID_SUCCESS if st.prime_hours_enabled else ID_CLOCK
    prime_text = "Prime Time (09:00 - 22:00): YOQILGAN" if st.prime_hours_enabled else "Prime Time: O'CHIQ (24/7)"
    prime_btn = InlineKeyboardButton(
        text=prime_text,
        style=prime_style,
        icon_custom_emoji_id=prime_icon,
        callback_data="story_toggle_prime_hours"
    )

    badges_style = "success" if st.enable_smart_badges else "primary"
    badges_icon = ID_SUCCESS if st.enable_smart_badges else ID_CHECK_GREEN_SQUARE
    badges_text = "Aqlli Badjlar: YOQILGAN" if st.enable_smart_badges else "Aqlli Badjlar: O'CHIQ"
    badges_btn = InlineKeyboardButton(
        text=badges_text,
        style=badges_style,
        icon_custom_emoji_id=badges_icon,
        callback_data="story_toggle_badges"
    )

    pin_style = "success" if st.pin_to_profile else "primary"
    pin_icon = ID_SUCCESS if st.pin_to_profile else ID_PIN
    pin_text = "Profilga Saqlash: YOQILGAN" if st.pin_to_profile else "Profilga Saqlash: O'CHIQ (Arxiv)"
    pin_btn = InlineKeyboardButton(
        text=pin_text,
        style=pin_style,
        icon_custom_emoji_id=pin_icon,
        callback_data="story_toggle_pin"
    )

    keyboard = [
        [prime_btn],
        [
            InlineKeyboardButton(
                text=f"Oraliq: {st.drip_delay_minutes} daq",
                style="primary",
                icon_custom_emoji_id=ID_TIMER,
                callback_data="story_menu_cooldown"
            ),
            InlineKeyboardButton(
                text=f"Kunlik: max {st.max_stories_per_day} ta",
                style="primary",
                icon_custom_emoji_id=ID_CALENDAR,
                callback_data="story_menu_daily_limit"
            )
        ],
        [badges_btn],
        [pin_btn],
        [
            InlineKeyboardButton(
                text=f"Navbatdagi E'lonlar ({pending_count} ta)",
                style="primary",
                icon_custom_emoji_id=ID_QUEUE,
                callback_data="story_view_queue_list"
            )
        ],
        [
            InlineKeyboardButton(
                text="Bosh Menyuga Qaytish",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data="story_main_menu"
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_cooldown_preset_keyboard(current_cooldown: int) -> InlineKeyboardMarkup:
    presets = [15, 30, 45, 60, 90, 120]
    row1 = []
    row2 = []
    for cd in presets[:3]:
        is_sel = (cd == current_cooldown)
        row1.append(InlineKeyboardButton(
            text=f"{cd} daqiqa (Faol)" if is_sel else f"{cd} daqiqa",
            style="success" if is_sel else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_sel else ID_TIMER,
            callback_data=f"story_set_cooldown_{cd}"
        ))
    for cd in presets[3:]:
        is_sel = (cd == current_cooldown)
        row2.append(InlineKeyboardButton(
            text=f"{cd} daqiqa (Faol)" if is_sel else f"{cd} daqiqa",
            style="success" if is_sel else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_sel else ID_TIMER,
            callback_data=f"story_set_cooldown_{cd}"
        ))
    return InlineKeyboardMarkup(inline_keyboard=[
        row1,
        row2,
        [
            InlineKeyboardButton(
                text="Orqaga",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data="story_menu_queue"
            )
        ]
    ])


def get_story_daily_limit_preset_keyboard(current_limit: int) -> InlineKeyboardMarkup:
    presets = [5, 10, 20, 35, 50, 100]
    row1 = []
    row2 = []
    for l in presets[:3]:
        is_sel = (l == current_limit)
        row1.append(InlineKeyboardButton(
            text=f"{l} ta (Faol)" if is_sel else f"{l} ta",
            style="success" if is_sel else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_sel else ID_CALENDAR,
            callback_data=f"story_set_limit_{l}"
        ))
    for l in presets[3:]:
        is_sel = (l == current_limit)
        row2.append(InlineKeyboardButton(
            text=f"{l} ta (Faol)" if is_sel else f"{l} ta",
            style="success" if is_sel else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_sel else ID_CALENDAR,
            callback_data=f"story_set_limit_{l}"
        ))
    return InlineKeyboardMarkup(inline_keyboard=[
        row1,
        row2,
        [
            InlineKeyboardButton(
                text="Boshqa miqdor (1 - 100 ta)",
                style="primary",
                icon_custom_emoji_id=ID_EDIT,
                callback_data="story_custom_daily_limit"
            )
        ],
        [
            InlineKeyboardButton(
                text="Orqaga",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data="story_menu_queue"
            )
        ]
    ])


def get_story_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Bekor Qilish",
                style="danger",
                icon_custom_emoji_id=ID_ERROR,
                callback_data="story_main_menu"
            )
        ]
    ])


def get_story_back_keyboard(back_target: str = "story_main_menu") -> InlineKeyboardMarkup:
    buttons = []
    if back_target != "menu_main":
        buttons.append([
            InlineKeyboardButton(
                text="Istoriya Menyusiga Qaytish",
                style="primary",
                icon_custom_emoji_id=ID_FLASH_GREEN,
                callback_data=back_target
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="Asosiy Menyuga Qaytish",
            style="danger",
            icon_custom_emoji_id=ID_BACK,
            callback_data="menu_main"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_story_vip_upgrade_keyboard() -> InlineKeyboardMarkup:
    """
    Renders the VIP upgrade keyboard with a direct button to purchase/upgrade to VIP
    via Telegram Stars (300 Stars) and navigation options.
    """
    keyboard = [
        [
            InlineKeyboardButton(
                text="VIP Cheksiz Tarifga O'tish (300 Stars)",
                style="success",
                icon_custom_emoji_id=ID_CROWN,
                callback_data="buy_plan_vip"
            )
        ],
        [
            InlineKeyboardButton(
                text="Barcha Tariflar va Imkoniyatlar",
                style="primary",
                icon_custom_emoji_id=ID_STARS,
                callback_data="menu_stars"
            )
        ],
        [
            InlineKeyboardButton(
                text="Asosiy Menyu",
                style="danger",
                icon_custom_emoji_id=ID_HOME,
                callback_data="menu_main"
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
