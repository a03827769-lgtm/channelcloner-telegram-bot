from typing import List, Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config.limits import STORY_MAX_PER_DAY_MAX, STORY_MAX_PER_DAY_MIN, STORY_VIDEO_DURATION_MAX, STORY_VIDEO_DURATION_MIN
from config.plans import PLANS
from database.models import StorySettings, StorySourceChannel
from services.custom_emojis import (
    ID_SUCCESS, ID_ERROR, ID_CROWN, ID_STARS, ID_HOME, ID_BACK, ID_CHANNEL,
    ID_FILTER, ID_TIMER, ID_PALETTE, ID_STATS, ID_ROCKET, ID_FLASH_GREEN,
    ID_PAUSE, ID_REFRESH, ID_FAQ, ID_LOGOUT, ID_MONEY, ID_IMAGE,
    ID_TAG, ID_CHECK_GREEN_SQUARE, ID_DIAMOND, ID_USER, ID_EDIT,
    ID_TRASH, ID_CLOCK, ID_CALENDAR, ID_QUEUE, ID_PIN, ID_TARGET
)
from services.listing_analyzer import format_price_usd

# Background styles offered in the design menu (code, label, icon)
STORY_STYLES = (
    ("telegram_green", "Telegram Yashil (Nativ)", ID_PALETTE),
    ("listing_blur", "Xiralashgan Kvartira Rasmi", ID_IMAGE),
    ("luxury_dark", "To'q Lux Gradiyent", ID_CROWN),
    ("emerald", "Zumrad Yashil Gradiyent", ID_DIAMOND),
)
STORY_STYLE_CODES = frozenset(code for code, _, _ in STORY_STYLES)

STORY_PRICE_PRESETS = (500, 700, 800, 1000, 1200, 1500)
STORY_COOLDOWN_PRESETS = (15, 30, 45, 60, 90, 120)
# 3 is the daily limit of accounts without Telegram Premium
STORY_DAILY_LIMIT_PRESETS = (3, 5, 10, 20, 50, 100)
STORY_DURATION_PRESETS = (15, 20, 25, 30, 35, 40)


def _short(value: Optional[str], limit: int = 28) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _preset_rows(values, current, label, callback_prefix: str, icon: str, per_row: int = 3) -> List[List[InlineKeyboardButton]]:
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(0, len(values), per_row):
        row = []
        for value in values[i:i + per_row]:
            selected = value == current
            row.append(InlineKeyboardButton(
                text=f"{label(value)} (Faol)" if selected else label(value),
                style="success" if selected else "primary",
                icon_custom_emoji_id=ID_SUCCESS if selected else icon,
                callback_data=f"{callback_prefix}{value}"
            ))
        rows.append(row)
    return rows


def _back(callback_data: str, text: str = "Orqaga") -> List[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, style="danger", icon_custom_emoji_id=ID_BACK, callback_data=callback_data)]


def get_story_main_menu_keyboard(is_auth: bool, is_active: bool, has_source: bool = False) -> InlineKeyboardMarkup:
    """Main Story Cloner control keyboard."""
    if is_auth:
        auth_btn = InlineKeyboardButton(text="Telegram Hisob (Ulangan)", style="success",
                                        icon_custom_emoji_id=ID_SUCCESS, callback_data="story_menu_auth")
    else:
        auth_btn = InlineKeyboardButton(text="Telegram Hisob (Ulanmagan)", style="danger",
                                        icon_custom_emoji_id=ID_ERROR, callback_data="story_menu_auth")

    if is_active:
        auto_btn = InlineKeyboardButton(text="Monitoring: YOQILGAN", style="success",
                                        icon_custom_emoji_id=ID_SUCCESS, callback_data="story_toggle_active")
    else:
        auto_btn = InlineKeyboardButton(text="Monitoring: TO'XTATILGAN", style="danger",
                                        icon_custom_emoji_id=ID_PAUSE, callback_data="story_toggle_active")

    keyboard = [
        [auth_btn],
        [
            InlineKeyboardButton(text="Manba Kanallar", style="primary" if has_source else "success",
                                 icon_custom_emoji_id=ID_CHANNEL, callback_data="story_menu_channels"),
            InlineKeyboardButton(text="Narx & Filtrlar", style="primary", icon_custom_emoji_id=ID_FILTER,
                                 callback_data="story_menu_filters")
        ],
        [
            InlineKeyboardButton(text="Navbat & Prime Time", style="primary", icon_custom_emoji_id=ID_TIMER,
                                 callback_data="story_menu_queue"),
            InlineKeyboardButton(text="Istoriya Dizayni", style="primary", icon_custom_emoji_id=ID_PALETTE,
                                 callback_data="story_menu_design")
        ],
        [
            InlineKeyboardButton(text="Statistika & Tarix", style="primary", icon_custom_emoji_id=ID_STATS,
                                 callback_data="story_menu_stats"),
            InlineKeyboardButton(text="Test Istoriya Joylash", style="success", icon_custom_emoji_id=ID_ROCKET,
                                 callback_data="story_menu_test_post")
        ],
        [auto_btn],
        [
            InlineKeyboardButton(text="Yangilash", style="primary", icon_custom_emoji_id=ID_REFRESH,
                                 callback_data="story_main_menu"),
            InlineKeyboardButton(text="Qo'llanma", style="primary", icon_custom_emoji_id=ID_FAQ,
                                 callback_data="story_menu_help")
        ],
        [InlineKeyboardButton(text="Asosiy Menyuga Qaytish", style="danger", icon_custom_emoji_id=ID_HOME,
                              callback_data="menu_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_auth_keyboard(is_auth: bool) -> InlineKeyboardMarkup:
    if is_auth:
        first = InlineKeyboardButton(text="Hisobdan Chiqish (Logout)", style="danger", icon_custom_emoji_id=ID_LOGOUT,
                                     callback_data="story_auth_logout_confirm")
    else:
        first = InlineKeyboardButton(text="Telegram Hisobni Ulash (OTP)", style="success", icon_custom_emoji_id=ID_SUCCESS,
                                     callback_data="story_auth_start")
    return InlineKeyboardMarkup(inline_keyboard=[
        [first],
        [InlineKeyboardButton(text="Istoriya Menyusiga Qaytish", style="primary", icon_custom_emoji_id=ID_FLASH_GREEN,
                              callback_data="story_main_menu")],
        [InlineKeyboardButton(text="Asosiy Menyuga Qaytish", style="danger", icon_custom_emoji_id=ID_BACK,
                              callback_data="menu_main")]
    ])


def get_story_logout_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ha, Hisobdan Chiqish (Logout)", style="danger", icon_custom_emoji_id=ID_ERROR,
                              callback_data="story_auth_logout_yes")],
        [InlineKeyboardButton(text="Bekor Qilish (Orqaga)", style="primary", icon_custom_emoji_id=ID_BACK,
                              callback_data="story_menu_auth")]
    ])


def get_story_filters_keyboard(st: StorySettings) -> InlineKeyboardMarkup:
    def toggle(on: bool, on_text: str, off_text: str, off_icon: str, callback_data: str) -> List[InlineKeyboardButton]:
        return [InlineKeyboardButton(
            text=on_text if on else off_text,
            style="success" if on else "primary",
            icon_custom_emoji_id=ID_SUCCESS if on else off_icon,
            callback_data=callback_data
        )]

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Minimal Narx: {format_price_usd(st.min_price, '$0')}", style="primary",
                              icon_custom_emoji_id=ID_MONEY, callback_data="story_filter_price_menu")],
        toggle(st.require_photos, "Rasmli Postlar: MAJBURIY", "Rasmli Postlar: Ixtiyoriy", ID_IMAGE, "story_toggle_photos"),
        toggle(st.require_price, "Narx Ko'rsatilishi: MAJBURIY", "Narx Ko'rsatilishi: Ixtiyoriy", ID_TAG, "story_toggle_price_req"),
        toggle(st.filter_demands, "Qidiruvlarni Filtrlash: YOQILGAN", "Qidiruvlarni Filtrlash: O'CHIQ", ID_FILTER, "story_toggle_demands"),
        toggle(st.enable_smart_badges, "Aqlli Badjlar: YOQILGAN", "Aqlli Badjlar: O'CHIQ", ID_CHECK_GREEN_SQUARE, "story_toggle_badges_f"),
        _back("story_main_menu")
    ])


def get_story_price_preset_keyboard(current_price: float) -> InlineKeyboardMarkup:
    rows = _preset_rows(
        STORY_PRICE_PRESETS,
        next((p for p in STORY_PRICE_PRESETS if abs(p - (current_price or 0)) < 0.1), None),
        lambda p: format_price_usd(p),
        "story_set_price_", ID_MONEY
    )
    rows.append([InlineKeyboardButton(text="Boshqa Narx Kiritish...", style="primary", icon_custom_emoji_id=ID_EDIT,
                                      callback_data="story_set_price_custom")])
    rows.append(_back("story_menu_filters"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_story_design_keyboard(current_style: str, video_duration: int = 25) -> InlineKeyboardMarkup:
    keyboard = []
    for code, label, fallback_icon in STORY_STYLES:
        selected = code == current_style
        keyboard.append([InlineKeyboardButton(
            text=f"{label} (Faol)" if selected else label,
            style="success" if selected else "primary",
            icon_custom_emoji_id=ID_SUCCESS if selected else fallback_icon,
            callback_data=f"story_set_style_{code}"
        )])
    keyboard.append([InlineKeyboardButton(text=f"Video Davomiyligi: {video_duration} soniya", style="primary",
                                          icon_custom_emoji_id=ID_TIMER, callback_data="story_menu_duration")])
    keyboard.append([InlineKeyboardButton(text="Musiqa: Luxury Treklar (Avtomatik)", style="success",
                                          icon_custom_emoji_id=ID_ROCKET, callback_data="story_music_info")])
    keyboard.append(_back("story_main_menu", "Bosh Menyuga Qaytish"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_duration_keyboard(current_duration: int = 25) -> InlineKeyboardMarkup:
    """Video duration presets, -5s/+5s steppers and a custom value (always within the story limits)."""
    rows = _preset_rows(STORY_DURATION_PRESETS, current_duration, lambda d: f"{d}s", "story_set_duration_", ID_TIMER)
    prev_val = max(STORY_VIDEO_DURATION_MIN, current_duration - 5)
    next_val = min(STORY_VIDEO_DURATION_MAX, current_duration + 5)
    rows.append([
        InlineKeyboardButton(text=f"-5s ({prev_val}s)", style="primary", icon_custom_emoji_id=ID_TIMER,
                             callback_data=f"story_set_duration_{prev_val}"),
        InlineKeyboardButton(text=f"+5s ({next_val}s)", style="primary", icon_custom_emoji_id=ID_TIMER,
                             callback_data=f"story_set_duration_{next_val}")
    ])
    rows.append([InlineKeyboardButton(
        text=f"Boshqa Davomiylik ({STORY_VIDEO_DURATION_MIN}-{STORY_VIDEO_DURATION_MAX}s)...", style="primary",
        icon_custom_emoji_id=ID_EDIT, callback_data="story_set_duration_custom"
    )])
    rows.append(_back("story_menu_design"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_story_channels_keyboard(
    primary_channel: Optional[str],
    extra_channels: List[StorySourceChannel],
    target_type: str = "self",
    target_channel: Optional[str] = None
) -> InlineKeyboardMarkup:
    keyboard = []

    if primary_channel:
        keyboard.append([
            InlineKeyboardButton(text=f"Asosiy: {_short(primary_channel, 22)}", style="primary",
                                 icon_custom_emoji_id=ID_CHANNEL, callback_data="story_channel_primary_info"),
            InlineKeyboardButton(text="O'zgartirish", style="primary", icon_custom_emoji_id=ID_EDIT,
                                 callback_data="story_edit_channel")
        ])
    else:
        keyboard.append([InlineKeyboardButton(text="Asosiy Kanalni Kiritish", style="success",
                                              icon_custom_emoji_id=ID_CHANNEL, callback_data="story_edit_channel")])

    for ch in extra_channels:
        label = _short(ch.channel_title or ch.channel_username, 22)
        if ch.is_active:
            ch_btn = InlineKeyboardButton(text=label, style="primary", icon_custom_emoji_id=ID_CHANNEL,
                                          callback_data=f"story_toggle_src_{ch.id}")
        else:
            ch_btn = InlineKeyboardButton(text=f"{label} (Pauza)", style="danger", icon_custom_emoji_id=ID_PAUSE,
                                          callback_data=f"story_toggle_src_{ch.id}")
        keyboard.append([ch_btn, InlineKeyboardButton(text="O'chirish", style="danger", icon_custom_emoji_id=ID_TRASH,
                                                      callback_data=f"story_del_src_{ch.id}")])

    keyboard.append([InlineKeyboardButton(text="Yangi Kanal Qo'shish (Multi-Manba)", style="success",
                                          icon_custom_emoji_id=ID_ROCKET, callback_data="story_add_extra_channel")])

    if target_type == "channel":
        keyboard.append([InlineKeyboardButton(text="Joylash: Kanal Istoriyasi", style="primary",
                                              icon_custom_emoji_id=ID_CHANNEL, callback_data="story_toggle_target")])
        keyboard.append([InlineKeyboardButton(
            text=f"Istoriya kanali: {_short(target_channel, 20)}" if target_channel else "Istoriya kanalini kiritish",
            style="primary" if target_channel else "success", icon_custom_emoji_id=ID_TARGET,
            callback_data="story_set_target_channel"
        )])
    else:
        keyboard.append([InlineKeyboardButton(text="Joylash: Shaxsiy Profil", style="success",
                                              icon_custom_emoji_id=ID_USER, callback_data="story_toggle_target")])

    keyboard.append(_back("story_main_menu", "Bosh Menyuga Qaytish"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_story_queue_keyboard(st: StorySettings, pending_count: int = 0) -> InlineKeyboardMarkup:
    window = f"{st.prime_hours_start:02d}:00 - {st.prime_hours_end:02d}:00"
    prime_btn = InlineKeyboardButton(
        text=f"Prime Time ({window}): YOQILGAN" if st.prime_hours_enabled else "Prime Time: O'CHIQ (24/7)",
        style="success" if st.prime_hours_enabled else "primary",
        icon_custom_emoji_id=ID_SUCCESS if st.prime_hours_enabled else ID_CLOCK,
        callback_data="story_toggle_prime_hours"
    )
    badges_btn = InlineKeyboardButton(
        text="Aqlli Badjlar: YOQILGAN" if st.enable_smart_badges else "Aqlli Badjlar: O'CHIQ",
        style="success" if st.enable_smart_badges else "primary",
        icon_custom_emoji_id=ID_SUCCESS if st.enable_smart_badges else ID_CHECK_GREEN_SQUARE,
        callback_data="story_toggle_badges_q"
    )
    pin_btn = InlineKeyboardButton(
        text="Profilga Saqlash: YOQILGAN" if st.pin_to_profile else "Profilga Saqlash: O'CHIQ (Arxiv)",
        style="success" if st.pin_to_profile else "primary",
        icon_custom_emoji_id=ID_SUCCESS if st.pin_to_profile else ID_PIN,
        callback_data="story_toggle_pin"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [prime_btn],
        [
            InlineKeyboardButton(text=f"Oraliq: {st.drip_delay_minutes} daq", style="primary", icon_custom_emoji_id=ID_TIMER,
                                 callback_data="story_menu_cooldown"),
            InlineKeyboardButton(text=f"Kunlik: max {st.max_stories_per_day} ta", style="primary",
                                 icon_custom_emoji_id=ID_CALENDAR, callback_data="story_menu_daily_limit")
        ],
        [badges_btn],
        [pin_btn],
        [InlineKeyboardButton(text=f"Navbatdagi E'lonlar ({pending_count} ta)", style="primary", icon_custom_emoji_id=ID_QUEUE,
                              callback_data="story_view_queue_list")],
        _back("story_main_menu", "Bosh Menyuga Qaytish")
    ])


def get_story_cooldown_preset_keyboard(current_cooldown: int) -> InlineKeyboardMarkup:
    rows = _preset_rows(STORY_COOLDOWN_PRESETS, current_cooldown, lambda cd: f"{cd} daqiqa", "story_set_cooldown_", ID_TIMER)
    rows.append(_back("story_menu_queue"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_story_daily_limit_preset_keyboard(current_limit: int) -> InlineKeyboardMarkup:
    rows = _preset_rows(STORY_DAILY_LIMIT_PRESETS, current_limit, lambda n: f"{n} ta", "story_set_limit_", ID_CALENDAR)
    rows.append([InlineKeyboardButton(
        text=f"Boshqa miqdor ({STORY_MAX_PER_DAY_MIN} - {STORY_MAX_PER_DAY_MAX} ta)", style="primary",
        icon_custom_emoji_id=ID_EDIT, callback_data="story_custom_daily_limit"
    )])
    rows.append(_back("story_menu_queue"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_story_cancel_keyboard(callback_data: str = "story_main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Bekor Qilish", style="danger", icon_custom_emoji_id=ID_ERROR, callback_data=callback_data)]
    ])


def get_story_back_keyboard(back_target: str = "story_main_menu") -> InlineKeyboardMarkup:
    buttons = []
    if back_target != "menu_main":
        buttons.append([InlineKeyboardButton(text="Istoriya Menyusiga Qaytish", style="primary",
                                             icon_custom_emoji_id=ID_FLASH_GREEN, callback_data=back_target)])
    buttons.append([InlineKeyboardButton(text="Asosiy Menyuga Qaytish", style="danger", icon_custom_emoji_id=ID_BACK,
                                         callback_data="menu_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_story_vip_upgrade_keyboard() -> InlineKeyboardMarkup:
    """Direct VIP purchase button (Telegram Stars) and navigation."""
    vip = PLANS["vip"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"VIP Cheksiz Tarifga O'tish ({vip['stars']} Stars)", style="success",
                              icon_custom_emoji_id=ID_CROWN, callback_data="buy_plan_vip")],
        [InlineKeyboardButton(text="Barcha Tariflar va Imkoniyatlar", style="primary", icon_custom_emoji_id=ID_STARS,
                              callback_data="menu_stars")],
        [InlineKeyboardButton(text="Asosiy Menyu", style="danger", icon_custom_emoji_id=ID_HOME, callback_data="menu_main")]
    ])
