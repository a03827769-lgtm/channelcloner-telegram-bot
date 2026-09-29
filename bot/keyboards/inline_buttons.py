import os
import logging
from typing import List, Optional

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from config.settings import settings, PROJECT_ROOT
from config.limits import BACKFILL_MAX_MESSAGES
from database.models import ChannelPair
from services.custom_emojis import (
    ID_ROCKET, ID_REFRESH, ID_STARS, ID_BOOK, ID_STATS, ID_CROWN,
    ID_SUCCESS, ID_ERROR, ID_HOME, ID_BACK, ID_BACKUP,
    ID_TRASH, ID_CLEAN, ID_TRANSLATE, ID_IMAGE, ID_MONEY,
    ID_LOCK_UNLOCKED, ID_SIGNATURE, ID_DOCUMENT, ID_SPARKLE,
    ID_HISTORY_CLOCK, ID_SERVER_CPU, ID_FLASH,
    ID_FLAG_UZ, ID_FLAG_RU, ID_FLAG_EN, ID_FLAG_TR, ID_SETTINGS, ID_FORWARD
)

logger = logging.getLogger(__name__)

# Persistent reply-keyboard labels. Handlers match these texts exactly (never with `contains`), so a
# forwarded post or wizard input that merely mentions e.g. "Statistika" is not taken as a menu tap.
MENU_CLONER = "Kanal Kloner"
MENU_NEW_PAIR = "Yangi Kanal"
MENU_STORY = "Istoriya Kloner (VIP)"
MENU_STATS = "Mening Statistikam"
MENU_BILLING = "Tariflar & Obuna"
MENU_GUIDE = "Qo'llanma"
# The guide label is also typed by hand; accept the common apostrophe variants of "Qo'llanma"
MENU_GUIDE_VARIANTS = frozenset({MENU_GUIDE, "Qoʻllanma", "Qo’llanma", "Qoʼllanma", "Qo`llanma"})

MAIN_REPLY_MENU_LABELS = frozenset({
    MENU_CLONER, MENU_NEW_PAIR, MENU_STORY, MENU_STATS, MENU_BILLING
}) | MENU_GUIDE_VARIANTS


def get_active_webapp_url() -> str:
    """Reads live HTTPS Cloudflare tunnel URL or falls back to settings.WEBAPP_URL safely"""
    # 1. First priority: settings.WEBAPP_URL if permanent custom domain
    cfg_url = getattr(settings, "WEBAPP_URL", "").strip()
    if cfg_url.startswith("https://") and "trycloudflare.com" not in cfg_url and len(cfg_url) > 10:
        return cfg_url.rstrip('/')

    # 2. Second priority: explicit WEBAPP_URL environment variable
    env_url = os.getenv("WEBAPP_URL", "").strip()
    if env_url.startswith("https://") and "trycloudflare.com" not in env_url and len(env_url) > 10:
        return env_url.rstrip('/')

    # 3. Third priority: active_tunnel_url.txt file (dynamic tunnel URL)
    url_file = PROJECT_ROOT / "data" / "active_tunnel_url.txt"
    if url_file.exists():
        try:
            url = url_file.read_text(encoding="utf-8").strip()
            if url.startswith("https://") and len(url) > len("https://"):
                return url.rstrip('/')
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    if env_url.startswith("https://"):
        return env_url.rstrip('/')
    return cfg_url.rstrip('/') if cfg_url.startswith("https://") else ""

def get_main_reply_keyboard(is_admin: bool = False, *args, **kwargs) -> ReplyKeyboardMarkup:
    """Persistent bottom Reply Keyboard menu with modern Bot API 9.4 styles and custom animated emojis"""
    buttons = [
        [
            KeyboardButton(text=MENU_CLONER, style="primary", icon_custom_emoji_id=ID_REFRESH),
            KeyboardButton(text=MENU_NEW_PAIR, style="success", icon_custom_emoji_id=ID_ROCKET)
        ],
        [
            KeyboardButton(text=MENU_STORY, style="success", icon_custom_emoji_id=ID_FLASH),
            KeyboardButton(text=MENU_STATS, style="primary", icon_custom_emoji_id=ID_STATS)
        ],
        [
            KeyboardButton(text=MENU_BILLING, style="primary", icon_custom_emoji_id=ID_CROWN),
            KeyboardButton(text=MENU_GUIDE, style="primary", icon_custom_emoji_id=ID_BOOK)
        ]
    ]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True
    )

def get_main_menu_keyboard(is_admin: bool = False, *args, **kwargs) -> InlineKeyboardMarkup:
    """Main dashboard inline keyboard with Bot API 9.4 styles and custom animated emojis"""
    admin_flag = is_admin or kwargs.get("is_admin", False)

    keyboard = [
        [
            InlineKeyboardButton(text="Tezkor Boshlash", callback_data="menu_quickstart", style="primary", icon_custom_emoji_id=ID_ROCKET),
            InlineKeyboardButton(text="Istoriya Kloner (VIP)", callback_data="story_main_menu", style="success", icon_custom_emoji_id=ID_FLASH)
        ],
        [
            InlineKeyboardButton(text="Kanal Kloner", callback_data="menu_cloner", style="primary", icon_custom_emoji_id=ID_REFRESH),
            InlineKeyboardButton(text="Tariflar & Obuna", callback_data="menu_stars", style="success", icon_custom_emoji_id=ID_CROWN)
        ],
        [
            InlineKeyboardButton(text="Mening Statistikam", callback_data="menu_stats", style="primary", icon_custom_emoji_id=ID_STATS),
            InlineKeyboardButton(text="Qo'llanma", callback_data="menu_guide", style="primary", icon_custom_emoji_id=ID_BOOK)
        ]
    ]
    if admin_flag:
        keyboard.append([
            InlineKeyboardButton(text="Admin Paneli", callback_data="menu_admin", style="danger", icon_custom_emoji_id=ID_SETTINGS)
        ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_quickstart_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Yangi Kanal Bog'lash", callback_data="cloner_add_pair", style="success", icon_custom_emoji_id=ID_ROCKET)],
        [InlineKeyboardButton(text="Tariflar & Obuna", callback_data="menu_stars", style="success", icon_custom_emoji_id=ID_STARS)],
        [InlineKeyboardButton(text="Asosiy Menyu", callback_data="menu_main", style="danger", icon_custom_emoji_id=ID_HOME)]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cloner_menu_keyboard(has_pairs: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(text="Yangi kanal ulash", callback_data="cloner_add_pair", style="success", icon_custom_emoji_id=ID_ROCKET)
        ]
    ]
    if has_pairs:
        keyboard.append([
            InlineKeyboardButton(text="Ulangan kanallar ro'yxati", callback_data="cloner_list_pairs", style="primary", icon_custom_emoji_id=ID_DOCUMENT)
        ])
    keyboard.append([
        InlineKeyboardButton(text="Asosiy menyu", callback_data="menu_main", style="danger", icon_custom_emoji_id=ID_HOME)
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def _short_label(value: Optional[str], limit: int = 24) -> str:
    """Channel title shortened for a button (titles may be up to 128 characters long)."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def get_pairs_list_keyboard(pairs: List[ChannelPair], page: int = 0, page_size: int = 6) -> InlineKeyboardMarkup:
    keyboard = []
    total = len(pairs)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    page_pairs = pairs[start_idx : start_idx + page_size]

    for idx, pair in enumerate(page_pairs, start=start_idx + 1):
        pair_style = "success" if pair.is_active else "danger"
        pair_icon = ID_SUCCESS if pair.is_active else ID_ERROR
        src_label = _short_label(pair.source_title or pair.source_channel)
        tgt_label = _short_label(pair.target_title or pair.target_channel)
        button_text = f"#{idx} {src_label} -> {tgt_label}"
        keyboard.append([
            InlineKeyboardButton(text=button_text, callback_data=f"pair_view_{pair.id}", style=pair_style, icon_custom_emoji_id=pair_icon)
        ])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="Oldingi", callback_data=f"cloner_pairs_page_{page - 1}", style="primary", icon_custom_emoji_id=ID_BACK))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop", style="primary", icon_custom_emoji_id=ID_DOCUMENT))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="Keyingi", callback_data=f"cloner_pairs_page_{page + 1}", style="primary", icon_custom_emoji_id=ID_FORWARD))
        keyboard.append(nav_row)

    keyboard.append([
        InlineKeyboardButton(text="Yangi Kanal", callback_data="cloner_add_pair", style="success", icon_custom_emoji_id=ID_ROCKET),
        InlineKeyboardButton(text="Orqaga", callback_data="menu_cloner", style="danger", icon_custom_emoji_id=ID_BACK)
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_pair_detail_keyboard(pair: ChannelPair) -> InlineKeyboardMarkup:
    """Modern structured 2-column control panel with Bot API 9.4 styles and custom emojis"""
    status_text = "To'xtatish" if pair.is_active else "Faollashtirish"
    status_style = "danger" if pair.is_active else "success"
    status_icon = ID_ERROR if pair.is_active else ID_SUCCESS

    clean_text = "Link Tozalash: ON" if pair.clean_links else "Link Tozalash: OFF"
    clean_style = "success" if pair.clean_links else "primary"

    trans_text = f"Tarjima: {pair.target_lang.upper()}" if pair.auto_translate else "Tarjima: OFF"
    trans_style = "success" if pair.auto_translate else "primary"

    wm_text = "Watermark: ON" if pair.image_watermark_type != "none" else "Watermark: OFF"
    wm_style = "success" if pair.image_watermark_type != "none" else "primary"

    prot_text = "Yopiq Kanal: ON" if pair.is_protected_source else "Yopiq Kanal: OFF"
    prot_style = "success" if pair.is_protected_source else "primary"

    emoji_text = "VIP Emojilar: ON" if pair.auto_premium_emojis else "VIP Emojilar: OFF"
    emoji_style = "success" if pair.auto_premium_emojis else "primary"

    video_wm_text = "Video WM: ON" if pair.video_watermark_type != "none" else "Video WM: OFF"
    video_wm_style = "success" if pair.video_watermark_type != "none" else "primary"

    drip_text = f"Drip: {pair.drip_delay_minutes}m" if pair.drip_delay_minutes > 0 else "Drip: OFF"
    drip_style = "success" if pair.drip_delay_minutes > 0 or pair.night_mode != "off" else "primary"

    ai_text = f"AI: {pair.ai_paraphrase_mode.upper()}" if pair.ai_paraphrase_mode != "off" else "AI Tone: OFF"
    ai_style = "success" if pair.ai_paraphrase_mode != "off" else "primary"

    cta_text = "CTA Tugma: ON" if pair.auto_cta_buttons else "CTA Tugma: OFF"
    cta_style = "success" if pair.auto_cta_buttons else "primary"

    remsig_text = "Manba Imzosini Tozalash: ON" if pair.remove_signature else "Manba Imzosini Tozalash: OFF"
    remsig_style = "success" if pair.remove_signature else "primary"

    catchup_text = "Oflayn Catch-Up: ON" if pair.auto_catchup else "Oflayn Catch-Up: OFF"
    catchup_style = "success" if pair.auto_catchup else "primary"

    keyboard = [
        [
            InlineKeyboardButton(text=status_text, callback_data=f"pair_toggle_{pair.id}", style=status_style, icon_custom_emoji_id=status_icon),
            InlineKeyboardButton(text=clean_text, callback_data=f"pair_toggle_clean_{pair.id}", style=clean_style, icon_custom_emoji_id=ID_CLEAN)
        ],
        [
            InlineKeyboardButton(text=trans_text, callback_data=f"pair_trans_menu_{pair.id}", style=trans_style, icon_custom_emoji_id=ID_TRANSLATE),
            InlineKeyboardButton(text=wm_text, callback_data=f"pair_wm_menu_{pair.id}", style=wm_style, icon_custom_emoji_id=ID_IMAGE)
        ],
        [
            InlineKeyboardButton(text=video_wm_text, callback_data=f"pair_vwm_menu_{pair.id}", style=video_wm_style, icon_custom_emoji_id=ID_ROCKET),
            InlineKeyboardButton(text=drip_text, callback_data=f"pair_drip_menu_{pair.id}", style=drip_style, icon_custom_emoji_id=ID_HISTORY_CLOCK)
        ],
        [
            InlineKeyboardButton(text=ai_text, callback_data=f"pair_ai_menu_{pair.id}", style=ai_style, icon_custom_emoji_id=ID_SERVER_CPU),
            InlineKeyboardButton(text=cta_text, callback_data=f"pair_toggle_cta_{pair.id}", style=cta_style, icon_custom_emoji_id=ID_MONEY)
        ],
        [
            InlineKeyboardButton(text=emoji_text, callback_data=f"pair_toggle_emoji_{pair.id}", style=emoji_style, icon_custom_emoji_id=ID_SPARKLE),
            InlineKeyboardButton(text=prot_text, callback_data=f"pair_toggle_prot_{pair.id}", style=prot_style, icon_custom_emoji_id=ID_LOCK_UNLOCKED)
        ],
        [
            InlineKeyboardButton(text=remsig_text, callback_data=f"pair_toggle_remsig_{pair.id}", style=remsig_style, icon_custom_emoji_id=ID_SIGNATURE),
            InlineKeyboardButton(text=catchup_text, callback_data=f"pair_toggle_catchup_{pair.id}", style=catchup_style, icon_custom_emoji_id=ID_HISTORY_CLOCK)
        ],
        [
            InlineKeyboardButton(text="Referal Linklar", callback_data=f"pair_aff_{pair.id}", style="primary", icon_custom_emoji_id=ID_MONEY),
            InlineKeyboardButton(text="Shaxsiy Imzo", callback_data=f"pair_edit_sig_{pair.id}", style="primary", icon_custom_emoji_id=ID_SIGNATURE)
        ],
        [
            InlineKeyboardButton(text="So'z/Raqam Almashtirish", callback_data=f"pair_edit_replace_{pair.id}", style="primary", icon_custom_emoji_id=ID_REFRESH),
            InlineKeyboardButton(text="Qora Ro'yxat", callback_data=f"pair_edit_black_{pair.id}", style="primary", icon_custom_emoji_id=ID_ERROR)
        ],
        [
            InlineKeyboardButton(text="Tarixni Ko'chirish", callback_data=f"pair_history_{pair.id}", style="primary", icon_custom_emoji_id=ID_REFRESH),
            InlineKeyboardButton(text="Zaxira & Qayta Tiklash", callback_data=f"pair_backup_menu_{pair.id}", style="primary", icon_custom_emoji_id=ID_BACKUP)
        ],
        [
            InlineKeyboardButton(text="Test Post Yuborish", callback_data=f"pair_test_post_{pair.id}", style="success", icon_custom_emoji_id=ID_ROCKET),
            InlineKeyboardButton(text="Statistika", callback_data=f"pair_stats_{pair.id}", style="primary", icon_custom_emoji_id=ID_STATS)
        ],
        [
            InlineKeyboardButton(text="Kanallar Ro'yxati", callback_data="cloner_list_pairs", style="primary", icon_custom_emoji_id=ID_DOCUMENT),
            InlineKeyboardButton(text="O'chirish", callback_data=f"pair_delete_confirm_{pair.id}", style="danger", icon_custom_emoji_id=ID_TRASH)
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_translate_lang_keyboard(pair_id: int, current_lang: Optional[str] = None) -> InlineKeyboardMarkup:
    def mark(lang_code: str, label: str) -> str:
        if current_lang and current_lang.lower() == lang_code.lower():
            return f"{label} [Faol]"
        return label

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=mark("uz", "O'zbekcha (UZ)"), callback_data=f"trans_set_{pair_id}_uz", style="primary", icon_custom_emoji_id=ID_FLAG_UZ),
            InlineKeyboardButton(text=mark("ru", "Русский (RU)"), callback_data=f"trans_set_{pair_id}_ru", style="primary", icon_custom_emoji_id=ID_FLAG_RU)
        ],
        [
            InlineKeyboardButton(text=mark("en", "English (EN)"), callback_data=f"trans_set_{pair_id}_en", style="primary", icon_custom_emoji_id=ID_FLAG_EN),
            InlineKeyboardButton(text=mark("tr", "Türkçe (TR)"), callback_data=f"trans_set_{pair_id}_tr", style="primary", icon_custom_emoji_id=ID_FLAG_TR)
        ],
        [
            InlineKeyboardButton(text=mark("off", "Tarjimani O'chirish"), callback_data=f"trans_set_{pair_id}_off", style="danger", icon_custom_emoji_id=ID_ERROR)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_BACK)
        ]
    ])

def get_history_count_keyboard(pair_id: int, max_count: int = BACKFILL_MAX_MESSAGES) -> InlineKeyboardMarkup:
    """Backfill sizes offered for a pair; `max_count` is the user's plan cap (there is no unlimited option)."""
    max_count = max(1, int(max_count))
    presets = [n for n in (10, 30, 50, 100) if n < max_count]
    rows = []
    for i in range(0, len(presets), 2):
        rows.append([
            InlineKeyboardButton(text=f"{n} ta post", callback_data=f"hist_start_{pair_id}_{n}", style="primary", icon_custom_emoji_id=ID_DOCUMENT)
            for n in presets[i:i + 2]
        ])
    rows.append([
        InlineKeyboardButton(text=f"Maksimal: {max_count} ta post", callback_data=f"hist_start_{pair_id}_{max_count}", style="success", icon_custom_emoji_id=ID_ROCKET)
    ])
    rows.append([
        InlineKeyboardButton(text="Bekor qilish", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_ERROR)
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_history_progress_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Ko'chirishni To'xtatish", callback_data=f"hist_cancel_{pair_id}", style="danger", icon_custom_emoji_id=ID_ERROR)
        ]
    ])

def get_delete_confirmation_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Ha, butunlay o'chirilsin", callback_data=f"pair_delete_yes_{pair_id}", style="danger", icon_custom_emoji_id=ID_TRASH),
            InlineKeyboardButton(text="Yo'q, bekor qilish", callback_data=f"pair_view_{pair_id}", style="success", icon_custom_emoji_id=ID_SUCCESS)
        ]
    ])

def get_cancel_keyboard(callback_data: str = "cloner_list_pairs") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Bekor qilish", callback_data=callback_data, style="danger", icon_custom_emoji_id=ID_ERROR)]]
    )

def get_back_to_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Asosiy menyuga qaytish", callback_data="menu_main", style="danger", icon_custom_emoji_id=ID_HOME)]]
    )

def get_video_watermark_keyboard(pair_id: int, pair: ChannelPair) -> InlineKeyboardMarkup:
    status_btn_text = "O'chirish" if pair.video_watermark_type != "none" else "Yoqish"
    status_btn_style = "danger" if pair.video_watermark_type != "none" else "success"
    status_btn_icon = ID_ERROR if pair.video_watermark_type != "none" else ID_SUCCESS

    def _vpos_btn(pos: str, label: str):
        is_sel = (pair.video_watermark_pos == pos)
        return InlineKeyboardButton(
            text=f"{label} [Faol]" if is_sel else label,
            callback_data=f"vwm_pos_{pair_id}_{pos}",
            style="success" if is_sel else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_sel else ID_DOCUMENT
        )

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=status_btn_text, callback_data=f"vwm_toggle_{pair_id}", style=status_btn_style, icon_custom_emoji_id=status_btn_icon),
            InlineKeyboardButton(text="Matnni O'zgartirish", callback_data=f"vwm_set_text_{pair_id}", style="primary", icon_custom_emoji_id=ID_SIGNATURE)
        ],
        [
            _vpos_btn("bottom_right", "Pastki O'ng"),
            _vpos_btn("bottom_left", "Pastki Chap")
        ],
        [
            _vpos_btn("top_right", "Yuqori O'ng"),
            _vpos_btn("top_left", "Yuqori Chap")
        ],
        [
            _vpos_btn("center", "Markaz (Center)")
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_BACK)
        ]
    ])

def _choice_button(label: str, callback_data: str, selected: bool, icon: str) -> InlineKeyboardButton:
    """Option button of a settings menu; the active option is marked (as in the watermark position menu)."""
    return InlineKeyboardButton(
        text=f"{label} [Faol]" if selected else label,
        callback_data=callback_data,
        style="success" if selected else "primary",
        icon_custom_emoji_id=ID_SUCCESS if selected else icon
    )

def get_drip_feed_keyboard(pair_id: int, pair: ChannelPair) -> InlineKeyboardMarkup:
    night_text = f"Tungi Rejim: {pair.night_mode.upper()}"

    def delay(minutes: int, label: str, icon: str) -> InlineKeyboardButton:
        return _choice_button(label, f"drip_delay_{pair_id}_{minutes}", pair.drip_delay_minutes == minutes, icon)

    return InlineKeyboardMarkup(inline_keyboard=[
        [delay(0, "Tezkor (0m)", ID_ROCKET), delay(5, "5 daqiqa", ID_HISTORY_CLOCK)],
        [delay(15, "15 daqiqa", ID_HISTORY_CLOCK), delay(30, "30 daqiqa", ID_HISTORY_CLOCK)],
        [
            InlineKeyboardButton(text=night_text, callback_data=f"drip_toggle_night_{pair_id}",
                                 style="success" if pair.night_mode != "off" else "primary", icon_custom_emoji_id=ID_LOCK_UNLOCKED)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_BACK)
        ]
    ])

def get_ai_paraphrase_keyboard(pair_id: int, pair: ChannelPair) -> InlineKeyboardMarkup:
    def mode(code: str, label: str, icon: str) -> InlineKeyboardButton:
        return _choice_button(label, f"ai_set_{pair_id}_{code}", pair.ai_paraphrase_mode == code, icon)

    off_selected = pair.ai_paraphrase_mode == "off"
    return InlineKeyboardMarkup(inline_keyboard=[
        [mode("formal", "Jurnalistik / Rasmiy", ID_DOCUMENT), mode("hype", "Qaynoq / Hype", ID_ROCKET)],
        [
            mode("short", "Qisqa Tezis / TL;DR", ID_FLASH),
            InlineKeyboardButton(text="O'chirish (Asl nusxa)" + (" [Faol]" if off_selected else ""),
                                 callback_data=f"ai_set_{pair_id}_off", style="danger", icon_custom_emoji_id=ID_ERROR)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_BACK)
        ]
    ])

def get_backup_restore_keyboard(pair_id: int, count: int, pair: ChannelPair) -> InlineKeyboardMarkup:
    backup_toggle_text = "Zaxira: ON" if pair.backup_enabled else "Zaxira: OFF"
    backup_toggle_style = "success" if pair.backup_enabled else "primary"

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=backup_toggle_text, callback_data=f"backup_toggle_{pair_id}", style=backup_toggle_style, icon_custom_emoji_id=ID_BACKUP)
        ],
        [
            InlineKeyboardButton(text=f"Qayta Tiklash ({count} ta post)", callback_data=f"backup_restore_start_{pair_id}", style="success", icon_custom_emoji_id=ID_REFRESH)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_BACK)
        ]
    ])

def get_upgrade_prompt_keyboard(pair_id: int, target_plan: str = "pro") -> InlineKeyboardMarkup:
    plan_name = "VIP Cheksiz" if target_plan == "vip" else "PRO"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"Tarifni {plan_name} ga Oshirish", callback_data="menu_stars", style="success", icon_custom_emoji_id=ID_STARS)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_BACK)
        ]
    ])
