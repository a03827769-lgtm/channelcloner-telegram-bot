from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from database.models import ChannelPair
from typing import List, Optional
from services.custom_emojis import (
    ID_ROCKET, ID_REFRESH, ID_STARS, ID_BOOK, ID_STATS, ID_INFO, ID_CROWN,
    ID_KEY, ID_SUCCESS, ID_ERROR, ID_WARN, ID_HOME, ID_BACK, ID_BROADCAST, ID_BACKUP,
    ID_LOGOUT, ID_TRASH, ID_CLEAN, ID_TRANSLATE, ID_IMAGE, ID_MONEY,
    ID_LOCK_UNLOCKED, ID_LOCK_LOCKED, ID_SIGNATURE, ID_DOCUMENT, ID_SPARKLE,
    ID_HISTORY_CLOCK, ID_SERVER_CPU, ID_FLASH
)

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom Reply Keyboard menu with modern Bot API 9.4 styles and custom emojis"""
    buttons = [
        [
            KeyboardButton(text="Kanal Kloner", style="primary", icon_custom_emoji_id=ID_REFRESH),
            KeyboardButton(text="Yangi Kanal", style="success", icon_custom_emoji_id=ID_ROCKET)
        ],
        [
            KeyboardButton(text="Tariflar & Obuna", style="success", icon_custom_emoji_id=ID_STARS),
            KeyboardButton(text="Mening Statistikam", style="primary", icon_custom_emoji_id=ID_STATS)
        ],
        [
            KeyboardButton(text="Qo'llanma", style="primary", icon_custom_emoji_id=ID_BOOK)
        ]
    ]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True
    )

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main dashboard inline keyboard with Bot API 9.4 styles and custom emojis"""
    keyboard = [
        [
            InlineKeyboardButton(text="Tezkor Boshlash (Qo'llanma)", callback_data="menu_quickstart", style="primary", icon_custom_emoji_id=ID_ROCKET)
        ],
        [
            InlineKeyboardButton(text="Kanal Kloner", callback_data="menu_cloner", style="primary", icon_custom_emoji_id=ID_REFRESH),
            InlineKeyboardButton(text="Tariflar & Obuna", callback_data="menu_stars", style="success", icon_custom_emoji_id=ID_STARS)
        ],
        [
            InlineKeyboardButton(text="Mening Statistikam", callback_data="menu_stats", style="primary", icon_custom_emoji_id=ID_STATS),
            InlineKeyboardButton(text="Qo'llanma", callback_data="menu_guide", style="primary", icon_custom_emoji_id=ID_BOOK)
        ]
    ]
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

def get_pairs_list_keyboard(pairs: List[ChannelPair]) -> InlineKeyboardMarkup:
    keyboard = []
    for idx, pair in enumerate(pairs, 1):
        pair_style = "success" if pair.is_active else "danger"
        pair_icon = ID_SUCCESS if pair.is_active else ID_ERROR
        src_label = pair.source_title or pair.source_channel
        tgt_label = pair.target_title or pair.target_channel
        button_text = f"#{idx} {src_label} -> {tgt_label}"
        keyboard.append([
            InlineKeyboardButton(text=button_text, callback_data=f"pair_view_{pair.id}", style=pair_style, icon_custom_emoji_id=pair_icon)
        ])
    
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
            InlineKeyboardButton(text="Referal Linklar", callback_data=f"pair_aff_{pair.id}", style="primary", icon_custom_emoji_id=ID_MONEY),
            InlineKeyboardButton(text="Shaxsiy Imzo", callback_data=f"pair_edit_sig_{pair.id}", style="primary", icon_custom_emoji_id=ID_SIGNATURE)
        ],
        [
            InlineKeyboardButton(text="Qora Ro'yxat", callback_data=f"pair_edit_black_{pair.id}", style="primary", icon_custom_emoji_id=ID_ERROR),
            InlineKeyboardButton(text="Tarixni Ko'chirish", callback_data=f"pair_history_{pair.id}", style="primary", icon_custom_emoji_id=ID_REFRESH)
        ],
        [
            InlineKeyboardButton(text="Zaxira & Qayta Tiklash", callback_data=f"pair_backup_menu_{pair.id}", style="primary", icon_custom_emoji_id=ID_BACKUP),
            InlineKeyboardButton(text="Test Post Yuborish", callback_data=f"pair_test_post_{pair.id}", style="success", icon_custom_emoji_id=ID_ROCKET)
        ],
        [
            InlineKeyboardButton(text="Statistika", callback_data=f"pair_stats_{pair.id}", style="primary", icon_custom_emoji_id=ID_STATS),
            InlineKeyboardButton(text="Kanallar Ro'yxati", callback_data="cloner_list_pairs", style="primary", icon_custom_emoji_id=ID_DOCUMENT)
        ],
        [
            InlineKeyboardButton(text="O'chirish", callback_data=f"pair_delete_confirm_{pair.id}", style="danger", icon_custom_emoji_id=ID_TRASH)
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_translate_lang_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="O'zbekcha (UZ)", callback_data=f"trans_set_{pair_id}_uz", style="success", icon_custom_emoji_id=ID_TRANSLATE),
            InlineKeyboardButton(text="Русский (RU)", callback_data=f"trans_set_{pair_id}_ru", style="primary", icon_custom_emoji_id=ID_TRANSLATE)
        ],
        [
            InlineKeyboardButton(text="English (EN)", callback_data=f"trans_set_{pair_id}_en", style="primary", icon_custom_emoji_id=ID_TRANSLATE),
            InlineKeyboardButton(text="Türkçe (TR)", callback_data=f"trans_set_{pair_id}_tr", style="primary", icon_custom_emoji_id=ID_TRANSLATE)
        ],
        [
            InlineKeyboardButton(text="Tarjimani O'chirish", callback_data=f"trans_set_{pair_id}_off", style="danger", icon_custom_emoji_id=ID_ERROR)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_BACK)
        ]
    ])

def get_history_count_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="10 ta post", callback_data=f"hist_start_{pair_id}_10", style="primary", icon_custom_emoji_id=ID_DOCUMENT),
            InlineKeyboardButton(text="30 ta post", callback_data=f"hist_start_{pair_id}_30", style="primary", icon_custom_emoji_id=ID_DOCUMENT)
        ],
        [
            InlineKeyboardButton(text="50 ta post", callback_data=f"hist_start_{pair_id}_50", style="primary", icon_custom_emoji_id=ID_DOCUMENT),
            InlineKeyboardButton(text="100 ta post", callback_data=f"hist_start_{pair_id}_100", style="primary", icon_custom_emoji_id=ID_DOCUMENT)
        ],
        [
            InlineKeyboardButton(text="Barcha mavjud tarixni ko'chirish", callback_data=f"hist_start_{pair_id}_all", style="success", icon_custom_emoji_id=ID_ROCKET)
        ],
        [
            InlineKeyboardButton(text="Bekor qilish", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_ERROR)
        ]
    ])

def get_history_progress_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⛔ Ko'chirishni To'xtatish", callback_data=f"hist_cancel_{pair_id}", style="danger", icon_custom_emoji_id=ID_ERROR)
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
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=status_btn_text, callback_data=f"vwm_toggle_{pair_id}", style=status_btn_style, icon_custom_emoji_id=status_btn_icon),
            InlineKeyboardButton(text="Matnni O'zgartirish", callback_data=f"vwm_set_text_{pair_id}", style="primary", icon_custom_emoji_id=ID_SIGNATURE)
        ],
        [
            InlineKeyboardButton(text="Pastki O'ng", callback_data=f"vwm_pos_{pair_id}_bottom_right", style="primary", icon_custom_emoji_id=ID_DOCUMENT),
            InlineKeyboardButton(text="Pastki Chap", callback_data=f"vwm_pos_{pair_id}_bottom_left", style="primary", icon_custom_emoji_id=ID_DOCUMENT)
        ],
        [
            InlineKeyboardButton(text="Yuqori O'ng", callback_data=f"vwm_pos_{pair_id}_top_right", style="primary", icon_custom_emoji_id=ID_DOCUMENT),
            InlineKeyboardButton(text="Yuqori Chap", callback_data=f"vwm_pos_{pair_id}_top_left", style="primary", icon_custom_emoji_id=ID_DOCUMENT)
        ],
        [
            InlineKeyboardButton(text="Markaz (Center)", callback_data=f"vwm_pos_{pair_id}_center", style="primary", icon_custom_emoji_id=ID_DOCUMENT)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_BACK)
        ]
    ])

def get_drip_feed_keyboard(pair_id: int, pair: ChannelPair) -> InlineKeyboardMarkup:
    night_text = f"Tungi Rejim: {pair.night_mode.upper()}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Tezkor (0m)", callback_data=f"drip_delay_{pair_id}_0", style="primary", icon_custom_emoji_id=ID_ROCKET),
            InlineKeyboardButton(text="5 daqiqa", callback_data=f"drip_delay_{pair_id}_5", style="primary", icon_custom_emoji_id=ID_HISTORY_CLOCK)
        ],
        [
            InlineKeyboardButton(text="15 daqiqa", callback_data=f"drip_delay_{pair_id}_15", style="primary", icon_custom_emoji_id=ID_HISTORY_CLOCK),
            InlineKeyboardButton(text="30 daqiqa", callback_data=f"drip_delay_{pair_id}_30", style="primary", icon_custom_emoji_id=ID_HISTORY_CLOCK)
        ],
        [
            InlineKeyboardButton(text=night_text, callback_data=f"drip_toggle_night_{pair_id}", style="success", icon_custom_emoji_id=ID_LOCK_UNLOCKED)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_BACK)
        ]
    ])

def get_ai_paraphrase_keyboard(pair_id: int, pair: ChannelPair) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Jurnalistik / Rasmiy", callback_data=f"ai_set_{pair_id}_formal", style="primary", icon_custom_emoji_id=ID_DOCUMENT),
            InlineKeyboardButton(text="Qaynoq / Hype", callback_data=f"ai_set_{pair_id}_hype", style="primary", icon_custom_emoji_id=ID_ROCKET)
        ],
        [
            InlineKeyboardButton(text="Qisqa Tezis / TL;DR", callback_data=f"ai_set_{pair_id}_short", style="primary", icon_custom_emoji_id=ID_FLASH),
            InlineKeyboardButton(text="O'chirish (Asl nusxa)", callback_data=f"ai_set_{pair_id}_off", style="danger", icon_custom_emoji_id=ID_ERROR)
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

