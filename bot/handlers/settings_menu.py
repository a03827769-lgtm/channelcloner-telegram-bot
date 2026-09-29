import asyncio
import logging
import re
import time
from typing import Any, Optional, Set

from aiogram import Router, F, Bot
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config.limits import (
    SIGNATURE_MAX_CHARS, BLACKLIST_MAX_CHARS, REPLACE_WORDS_MAX_CHARS,
    AFFILIATE_RULES_MAX_CHARS, WATERMARK_TEXT_MAX_CHARS, DRIP_DELAY_MAX_MINUTES
)
from database.db_manager import db_manager
from database.models import ChannelPair
from bot.filters import SETTINGS_INPUT, WIZARD_INPUT
from bot.access import can_manage_pair, has_admin_side
from bot.states.cloner_states import EditSettingsSG
from bot.keyboards.inline_buttons import (
    get_cancel_keyboard,
    get_pair_detail_keyboard,
    get_translate_lang_keyboard,
    get_video_watermark_keyboard,
    get_drip_feed_keyboard,
    get_ai_paraphrase_keyboard,
    get_backup_restore_keyboard,
    get_upgrade_prompt_keyboard,
    get_back_to_main_keyboard
)
from bot.handlers.cloner_menu import render_pair_detail
from services.channel_access import verify_destination_access, DESTINATION_ERROR_TEXTS
from services.disaster_recovery import disaster_recovery_service
from services.text_processor import TextProcessor
from services.custom_emojis import (
    TRANSLATE, IMAGE, MONEY, ROCKET, FLASH, REFRESH, STATS,
    SIGNATURE, ERROR, SUCCESS, PIN, LOCATION, LINK, HISTORY_CLOCK, SERVER_CPU, SAVE_BACKUP,
    WARN, NIGHT_MODE, SHIELD,
    ID_SUCCESS, ID_LOCATION, ID_TRASH, ID_BACK,
    ID_SIGNATURE, ID_DOCUMENT, ID_CHANNEL, ID_CLEAN
)

from bot.utils import (
    safe_answer, html_escape, parse_callback_id, preview, edit_or_send, show_in_place,
    is_repeated_album_part
)

logger = logging.getLogger(__name__)
router = Router(name="settings_menu_router")
# Pair settings are managed in the private chat with the bot only
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

# Accepted values — the same the Mini App API validates (services/api_routes.py)
WATERMARK_POSITIONS = frozenset({
    "top_left", "top_center", "top_right", "center_left", "center",
    "center_right", "bottom_left", "bottom_center", "bottom_right",
})
PARAPHRASE_MODES = frozenset({"off", "short", "hype", "formal", "luxury", "urgency", "conversational"})
NIGHT_MODES = ("off", "silent", "buffer")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
USERNAME_HANDLE_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,31}")

PAIR_NOT_FOUND_TEXT = "Kanal topilmadi!"
PAIR_FORBIDDEN_TEXT = "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas."
INVALID_VALUE_TEXT = "Noto'g'ri qiymat!"

_restore_tasks: Set[asyncio.Task] = set()

def safe_parse_id(data: str, index: int = -1) -> Optional[int]:
    """Safely extracts a positive integer ID from callback data string"""
    value = parse_callback_id(data, index)
    return value if value > 0 else None

async def _get_accessible_pair(callback: CallbackQuery, pair_id: Optional[int]) -> Optional[ChannelPair]:
    """The pair if the user may manage it; otherwise answers the callback with an alert (its only
    answer) and returns None."""
    pair = await db_manager.get_pair_by_id(pair_id) if pair_id else None
    if not pair:
        await safe_answer(callback, PAIR_NOT_FOUND_TEXT, show_alert=True)
        return None
    if not can_manage_pair(pair, callback.from_user.id):
        await safe_answer(callback, PAIR_FORBIDDEN_TEXT, show_alert=True)
        return None
    return pair

async def _load_pair_from_state(message: Message, state: FSMContext) -> Optional[ChannelPair]:
    """The pair an editor state refers to, when the user still may manage it (else the state is cleared)."""
    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer(f"{WARN} Sessiya eskirgan. Qaytadan urinib ko'ring.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return None
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not can_manage_pair(pair, message.from_user.id):
        await state.clear()
        await message.answer(f"{ERROR} <b>Ruxsat berilmagan!</b> Ushbu kanal sizga tegishli emas.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return None
    return pair

async def _has_paid_plan(user_id: int, pair: ChannelPair) -> bool:
    """PRO/VIP (or an admin on either side) — the gate of the paid pair features (image/video watermark,
    affiliate rules, CTA buttons, drip feed, night mode, AI paraphrase), same as the Mini App."""
    if has_admin_side(user_id, pair):
        return True
    sub = await db_manager.get_user_subscription(user_id)
    return sub.tier in ("pro", "vip") and sub.is_active

async def _has_vip_plan(user_id: int, pair: ChannelPair) -> bool:
    """VIP (or an admin on either side) — premium emojis and protected-source mode."""
    if has_admin_side(user_id, pair):
        return True
    sub = await db_manager.get_user_subscription(user_id)
    return sub.tier == "vip" and sub.is_active

async def _tier_label(user_id: int) -> str:
    sub = await db_manager.get_user_subscription(user_id)
    if sub.tier in ("pro", "vip") and sub.is_active:
        return "VIP" if sub.tier == "vip" else "Pro"
    return "Free (Sinov)" if sub.is_active else "Sinov muddati tugagan"

async def _show_upgrade_prompt(callback: CallbackQuery, pair: ChannelPair, icon: str, title: str, description: str):
    text = f"""
{icon} <b>{title} — Pulli Funksiya!</b>

{description} faqat <b>PRO</b> va <b>VIP</b> tariflarida mavjud.

{PIN} <b>Sizning hozirgi tarifingiz:</b> <code>{html_escape(await _tier_label(callback.from_user.id))}</code>

<i>Tarifni PRO yoki VIP ga oshirish uchun quyidagi tugmani bosing:</i>
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_upgrade_prompt_keyboard(pair.id, "pro"))

def _target_handle(pair: ChannelPair) -> Optional[str]:
    """Public @username of the pair's target channel, if it has one."""
    channel = (pair.target_channel or "").strip()
    return channel if USERNAME_HANDLE_RE.fullmatch(channel) else None

def _default_watermark_text(pair: ChannelPair) -> str:
    """Watermark text used when none was typed: the target's @username, else its title — never a raw
    "-100…" chat id or an invite link."""
    handle = _target_handle(pair)
    if handle:
        return handle
    title = (pair.target_title or "").strip()
    if title and not re.fullmatch(r"-?\d+", title) and "t.me/" not in title:
        return title[:WATERMARK_TEXT_MAX_CHARS]
    return ""

def _too_long_text(limit: int, length: int) -> str:
    return f"{WARN} <b>Matn juda uzun!</b> Ko'pi bilan <b>{limit}</b> ta belgi kiritish mumkin (siz yubordingiz: {length} ta). Iltimos, qisqartirib qayta yuboring."


# --- AUTO-TRANSLATOR SETTINGS ---

@router.callback_query(F.data.startswith("pair_trans_menu_"))
async def cb_trans_menu(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return
    await safe_answer(callback)

    curr_lang = html_escape(pair.target_lang.upper()) if pair.auto_translate else "O'chirilgan"
    text = f"""
{TRANSLATE} <b>Avto-Tarjima (Real-Time Auto-Translator)</b>

Manba kanaldagi xabarlar qaysi tilga avtomatik tarjima qilinsin?

{PIN} <b>Hozirgi holat:</b> {curr_lang}

<i>Kerakli tilni tanlang:</i>
"""
    await edit_or_send(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=get_translate_lang_keyboard(pair.id, current_lang=pair.target_lang if pair.auto_translate else "off")
    )

@router.callback_query(F.data.startswith("trans_set_"))
async def cb_set_translate_lang(callback: CallbackQuery):
    match = re.fullmatch(r"trans_set_(\d+)_([A-Za-z-]{2,12})", callback.data or "")
    lang = match.group(2).lower() if match else ""
    if not match or (lang != "off" and not LANG_RE.match(lang)):
        await safe_answer(callback, INVALID_VALUE_TEXT, show_alert=True)
        return
    pair = await _get_accessible_pair(callback, int(match.group(1)))
    if not pair:
        return

    if lang == "off":
        await db_manager.set_auto_translate(pair.id, enabled=False)
        await safe_answer(callback, "Avto-tarjima o'chirildi.")
    else:
        await db_manager.set_auto_translate(pair.id, enabled=True, target_lang=lang)
        await safe_answer(callback, f"Avto-tarjima yoqildi: {lang.upper()}")

    pair = await db_manager.get_pair_by_id(pair.id)
    if pair:
        await render_pair_detail(pair, callback.message)

# --- IMAGE WATERMARK BADGE SETTINGS ---

def get_watermark_pos_keyboard(pair_id: int, current_pos: str = "") -> InlineKeyboardMarkup:
    def _btn(pos_name: str, label: str) -> InlineKeyboardButton:
        is_active = (current_pos == pos_name)
        return InlineKeyboardButton(
            text=f"{label} [Faol]" if is_active else label,
            style="success" if is_active else "primary",
            icon_custom_emoji_id=ID_SUCCESS if is_active else ID_LOCATION,
            callback_data=f"wm_pos_{pair_id}_{pos_name}",
        )

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _btn("bottom_right", "Pastda O'ngda"),
            _btn("bottom_left", "Pastda Chapda")
        ],
        [
            _btn("top_right", "Tepada O'ngda"),
            _btn("center", "Markazda")
        ],
        [
            InlineKeyboardButton(
                text="Suv belgisini o'chirish",
                style="danger",
                icon_custom_emoji_id=ID_TRASH,
                callback_data=f"wm_pos_{pair_id}_clear"
            )
        ],
        [
            InlineKeyboardButton(
                text="Orqaga",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data=f"pair_view_{pair_id}"
            )
        ]
    ])


@router.callback_query(F.data.startswith("pair_wm_menu_"))
async def cb_wm_menu(callback: CallbackQuery, state: FSMContext):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return
    await safe_answer(callback)
    await state.clear()
    # Without a paid plan the menu stays reachable only to switch an enabled watermark off
    if pair.image_watermark_type == "none" and not await _has_paid_plan(callback.from_user.id, pair):
        await _show_upgrade_prompt(
            callback, pair, IMAGE, "Rasmlarga Suv Belgisi (Watermark)",
            "Rasm va videolarga brendingiz yoki kanalingiz nomini avtomatik tushirish"
        )
        return

    curr_wm = f"<code>{html_escape(pair.image_watermark_text)}</code>" if pair.image_watermark_text else "<i>O'rnatilmagan</i>"
    text = f"""
{IMAGE} <b>Rasmlarga Suv Belgisi (Watermark) Qo'yish</b>

Har bir rasm va videoga avtomatik ravishda brendingiz, kanal nomingiz yoki logotipingiz tushiriladi.

{PIN} <b>Hozirgi matn:</b> {curr_wm}
{LOCATION} <b>Joylashuvi:</b> <code>{html_escape(pair.image_watermark_pos)}</code>

<b>Yangi suv belgisi matnini yozing</b> (ko'pi bilan {WATERMARK_TEXT_MAX_CHARS} ta belgi):
<i>(Misol: <code>@mening_kanalim</code> yoki <code>Mening Brendim</code>)</i>

<i>O'chirish uchun pastdagi tugmani bosing yoki <code>/clear</code> deb yozing:</i>
"""
    await state.update_data(pair_id=pair.id)
    await state.set_state(EditSettingsSG.waiting_for_wm_text)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_watermark_pos_keyboard(pair.id, pair.image_watermark_pos))

@router.callback_query(F.data.startswith("wm_pos_"))
async def cb_set_wm_pos(callback: CallbackQuery, state: FSMContext):
    match = re.fullmatch(r"wm_pos_(\d+)_([a-z_]+)", callback.data or "")
    pos = match.group(2) if match else ""
    if not match or (pos != "clear" and pos not in WATERMARK_POSITIONS):
        await safe_answer(callback, INVALID_VALUE_TEXT, show_alert=True)
        return
    pair = await _get_accessible_pair(callback, int(match.group(1)))
    if not pair:
        return

    if pos == "clear":
        await db_manager.update_watermark_settings(pair.id, wm_type="none", text="", pos="bottom_right")
        await safe_answer(callback, "Suv belgisi o'chirildi")
    else:
        if not await _has_paid_plan(callback.from_user.id, pair):
            await safe_answer(callback, "Suv belgisi faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)
            return
        current_text = pair.image_watermark_text or _default_watermark_text(pair)
        if not current_text:
            await safe_answer(callback, "Avval suv belgisi matnini yozib yuboring, so'ng joylashuvni tanlang.", show_alert=True)
            return
        await db_manager.update_watermark_settings(pair.id, wm_type="text", text=current_text, pos=pos)
        await safe_answer(callback, f"Joylashuv o'rnatildi: {pos}")

    await state.clear()
    pair = await db_manager.get_pair_by_id(pair.id)
    if pair:
        await render_pair_detail(pair, callback.message)

@router.message(EditSettingsSG.waiting_for_wm_text, SETTINGS_INPUT)
async def process_new_wm_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi suv belgisini yuboring.", parse_mode="HTML")
        return

    new_wm = message.text.strip()
    clean_wm = re.sub(r'[​‌‍﻿⁠]', '', new_wm).strip()
    if not clean_wm and new_wm != "/clear":
        await message.answer(f"{WARN} Suv belgisi matni bo'sh yoki ko'rinmas belgilardan iborat bo'lishi mumkin emas.", parse_mode="HTML")
        return
    if new_wm != "/clear":
        new_wm = clean_wm
        if len(new_wm) > WATERMARK_TEXT_MAX_CHARS:
            await message.answer(_too_long_text(WATERMARK_TEXT_MAX_CHARS, len(new_wm)), parse_mode="HTML")
            return

    pair = await _load_pair_from_state(message, state)
    if not pair:
        return
    current_pos = pair.image_watermark_pos or "bottom_right"

    if new_wm == "/clear":
        await db_manager.update_watermark_settings(pair.id, wm_type="none", text="", pos=current_pos)
        msg = "Rasmlarga suv belgisi urish o'chirildi"
        icon = ERROR
    else:
        if not await _has_paid_plan(message.from_user.id, pair):
            await state.clear()
            await message.answer(f"{WARN} Suv belgisi faqat <b>PRO</b> va <b>VIP</b> tariflarida mavjud.", parse_mode="HTML", reply_markup=get_upgrade_prompt_keyboard(pair.id, "pro"))
            return
        await db_manager.update_watermark_settings(pair.id, wm_type="text", text=new_wm, pos=current_pos)
        msg = "Rasmlarga suv belgisi muvaffaqiyatli o'rnatildi"
        icon = SUCCESS

    await state.clear()
    pair = await db_manager.get_pair_by_id(pair.id)
    await message.answer(
        text=f"{icon} <b>{msg}</b>",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- AFFILIATE / REFERRAL REPLACER SETTINGS ---

@router.callback_query(F.data.startswith("pair_aff_"))
async def cb_affiliate_menu(callback: CallbackQuery, state: FSMContext):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 2))
    if not pair:
        return
    await safe_answer(callback)
    await state.clear()
    # Without a paid plan the editor stays reachable only to clear existing rules
    if not pair.affiliate_rules and not await _has_paid_plan(callback.from_user.id, pair):
        await _show_upgrade_prompt(
            callback, pair, MONEY, "Referal Havolalar Almashtirgichi",
            "Begona havolalarni o'zingizning referal havolalaringizga almashtirish"
        )
        return

    await state.update_data(pair_id=pair.id)
    await state.set_state(EditSettingsSG.waiting_for_affiliate_rules)

    curr_rules = f"<code>{preview(pair.affiliate_rules)}</code>" if pair.affiliate_rules else "<i>O'rnatilmagan</i>"
    text = f"""
{MONEY} <b>Referal va Sheriklik Havolalari Almashtirgichi</b>

Manba kanaldagi begona havolalarni o'zingizning daromad keltiruvchi referal havolalaringizga almashtiring.

{PIN} <b>Hozirgi qoidalar:</b>
{curr_rules}

<i>Format: <code>domen=shaxsiy_link</code> (har birini yangi qatordan, jami {AFFILIATE_RULES_MAX_CHARS} belgigacha)</i>
<i>Misol:</i>
<code>aliexpress.com=https://s.click.aliexpress.com/e/_MY_AFF
uzum.uz=https://uzum.uz/?ref=my_ref_code
binance.com=https://binance.com/ref/12345678</code>

<i>Qoidalarni tozalash uchun <code>/clear</code> deb yozing.</i>
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_cancel_keyboard(f"pair_view_{pair.id}"))

@router.message(EditSettingsSG.waiting_for_affiliate_rules, SETTINGS_INPUT)
async def process_new_affiliate_rules(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi qoidalarni yuboring.", parse_mode="HTML")
        return

    rules = message.text.strip()
    if len(rules) > AFFILIATE_RULES_MAX_CHARS:
        await message.answer(_too_long_text(AFFILIATE_RULES_MAX_CHARS, len(rules)), parse_mode="HTML")
        return

    pair = await _load_pair_from_state(message, state)
    if not pair:
        return

    if rules == "/clear":
        formatted_rules = ""
    else:
        if not await _has_paid_plan(message.from_user.id, pair):
            await state.clear()
            await message.answer(f"{WARN} Referal almashtirgich faqat <b>PRO</b> va <b>VIP</b> tariflarida mavjud.", parse_mode="HTML", reply_markup=get_upgrade_prompt_keyboard(pair.id, "pro"))
            return
        valid_rules = []
        for line in re.split(r'[\r\n]+', rules):
            line = line.strip()
            if not line:
                continue
            if "=" in line or "=>" in line:
                valid_rules.append(line)
        if not valid_rules:
            await message.answer(
                f"{WARN} <b>Noto'g'ri format!</b> Qoidada <code>=</code> yoki <code>=&gt;</code> belgisi bo'lishi kerak.\n\n"
                f"<i>Misol:</i>\n<code>uzum.uz=my_ref_tag\naliexpress.com=aff_id</code>",
                parse_mode="HTML"
            )
            return
        formatted_rules = "\n".join(valid_rules)

    await db_manager.update_affiliate_rules(pair.id, formatted_rules)
    await state.clear()

    pair = await db_manager.get_pair_by_id(pair.id)
    await message.answer(
        text=f"{SUCCESS} <b>Referal havolalar qoidalari muvaffaqiyatli saqlandi!</b>",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- PROTECTED CONTENT MODE TOGGLE ---

@router.callback_query(F.data.regexp(r"^pair_toggle_prot_(\d+)$"))
async def cb_toggle_protected(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return

    # Enabling needs VIP (or an admin); switching the mode off is always possible
    if not pair.is_protected_source and not await _has_vip_plan(callback.from_user.id, pair):
        await safe_answer(
            callback,
            "Bu funksiya faqat VIP Cheksiz tarif egalari uchun! 'Tariflar' bo'limidan VIP ga o'ting.",
            show_alert=True
        )
        return

    new_status = await db_manager.toggle_protected_mode(pair.id)
    await safe_answer(callback, "Himoyalangan (Protected) kanal rejimi yoqildi" if new_status else "Protected rejim o'chirildi")
    pair = await db_manager.get_pair_by_id(pair.id)
    if pair:
        await render_pair_detail(pair, callback.message)

# --- VIP ANIMATED EMOJIS TOGGLE ---

@router.callback_query(F.data.regexp(r"^pair_toggle_emoji_(\d+)$"))
async def cb_toggle_emojis(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return

    # Premium animated emojis are a VIP feature (as in the Mini App); switching them off is always possible
    if not pair.auto_premium_emojis and not await _has_vip_plan(callback.from_user.id, pair):
        await safe_answer(
            callback,
            "Telegram Premium emojilar faqat VIP Cheksiz tarifida mavjud! 'Tariflar' bo'limidan VIP ga o'ting.",
            show_alert=True
        )
        return

    new_status = await db_manager.toggle_premium_emojis(pair.id)
    await safe_answer(callback, "Telegram Premium Emojilar rejimi yoqildi" if new_status else "Premium Emojilar rejimi o'chirildi")
    pair = await db_manager.get_pair_by_id(pair.id)
    if pair:
        await render_pair_detail(pair, callback.message)

# --- REMOVE SOURCE SIGNATURE TOGGLE ---

@router.callback_query(F.data.regexp(r"^pair_toggle_remsig_(\d+)$"))
async def cb_toggle_remove_signature(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return

    new_status = await db_manager.toggle_remove_signature(pair.id)
    await safe_answer(callback, "Manba imzosini tozalash yoqildi" if new_status else "Manba imzosini tozalash o'chirildi")
    pair = await db_manager.get_pair_by_id(pair.id)
    if pair:
        await render_pair_detail(pair, callback.message)

# --- SIGNATURE / WATERMARK EDITING ---

def get_signature_presets_keyboard(pair: Any) -> InlineKeyboardMarkup:
    """Ready-made signatures need the target's public @username; without one only clearing is offered."""
    handle = _target_handle(pair)
    rows = []
    if handle:
        rows.extend([
            [InlineKeyboardButton(text=handle, style="primary", icon_custom_emoji_id=ID_SIGNATURE, callback_data=f"sig_set_{pair.id}_p1")],
            [InlineKeyboardButton(text=f"Bizning kanal: {handle}", style="primary", icon_custom_emoji_id=ID_DOCUMENT, callback_data=f"sig_set_{pair.id}_p2")],
            [InlineKeyboardButton(text=f"Obuna bo'ling: {handle}", style="primary", icon_custom_emoji_id=ID_CHANNEL, callback_data=f"sig_set_{pair.id}_p3")],
        ])
    rows.extend([
        [InlineKeyboardButton(text="Imzoni tozalash", style="danger", icon_custom_emoji_id=ID_CLEAN, callback_data=f"sig_set_{pair.id}_clear")],
        [InlineKeyboardButton(text="Orqaga", style="danger", icon_custom_emoji_id=ID_BACK, callback_data=f"pair_view_{pair.id}")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data.startswith("pair_edit_sig_"))
async def cb_edit_signature(callback: CallbackQuery, state: FSMContext):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return
    await safe_answer(callback)

    await state.update_data(pair_id=pair.id)
    await state.set_state(EditSettingsSG.waiting_for_signature)

    curr_sig = preview(pair.custom_signature, strip_tags=True) if pair.custom_signature else "<i>O'rnatilmagan</i>"
    presets_hint = "tayyor shablonlardan birini tanlang" if _target_handle(pair) else "uni yozib yuboring"
    text = f"""
{SIGNATURE} <b>Matn Imzosi (Post Ostiga Matn Qo'shish)</b>

Ushbu kanalga tashlanadigan barcha postlar ostiga qo'shiladigan imzo matnini kiriting yoki {presets_hint}.

{PIN} <b>Hozirgi imzo:</b>
{curr_sig}

<i>O'z imzo matningizni yozib yuborishingiz ham mumkin (ko'pi bilan {SIGNATURE_MAX_CHARS} ta belgi):</i>
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_signature_presets_keyboard(pair))

@router.callback_query(F.data.startswith("sig_set_"))
async def cb_set_preset_sig(callback: CallbackQuery, state: FSMContext):
    match = re.fullmatch(r"sig_set_(\d+)_(p1|p2|p3|clear)", callback.data or "")
    if not match:
        await safe_answer(callback, INVALID_VALUE_TEXT, show_alert=True)
        return
    pair = await _get_accessible_pair(callback, int(match.group(1)))
    if not pair:
        return
    preset_type = match.group(2)

    if preset_type == "clear":
        new_sig = ""
    else:
        handle = _target_handle(pair)
        if not handle:
            await safe_answer(callback, "Kanalingizda ochiq @username yo'q. Imzo matnini o'zingiz yozib yuboring.", show_alert=True)
            return
        if preset_type == "p1":
            new_sig = f"{PIN} {handle}"
        elif preset_type == "p2":
            new_sig = f"{LINK} Bizning kanal: {handle}"
        else:
            new_sig = f"{LINK} Obuna bo'ling: {handle}"

    await db_manager.update_pair_signature(pair.id, new_sig)
    await state.clear()
    await safe_answer(callback, "Imzo muvaffaqiyatli saqlandi!" if new_sig else "Imzo tozalandi")

    pair = await db_manager.get_pair_by_id(pair.id)
    if pair:
        await render_pair_detail(pair, callback.message)

@router.message(EditSettingsSG.waiting_for_signature, SETTINGS_INPUT)
async def process_new_signature(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi imzoni yuboring.", parse_mode="HTML")
        return

    raw_val = getattr(message, 'html_text', None) or message.text or ""
    new_sig = raw_val.strip()
    if new_sig != "/clear" and len(new_sig) > SIGNATURE_MAX_CHARS:
        await message.answer(_too_long_text(SIGNATURE_MAX_CHARS, len(new_sig)), parse_mode="HTML")
        return

    pair = await _load_pair_from_state(message, state)
    if not pair:
        return

    if new_sig == "/clear":
        new_sig = ""
    elif new_sig:
        new_sig = TextProcessor.ensure_closed_tags(new_sig)

    await db_manager.update_pair_signature(pair.id, new_sig)
    await state.clear()

    pair = await db_manager.get_pair_by_id(pair.id)
    disp_sig = preview(new_sig, strip_tags=True) if new_sig else "<i>Tozalandi</i>"
    await message.answer(
        text=f"{SUCCESS} <b>Shaxsiy imzo saqlandi:</b>\n{disp_sig}",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- BLACKLIST WORDS EDITING ---

@router.callback_query(F.data.startswith("pair_edit_black_"))
async def cb_edit_blacklist(callback: CallbackQuery, state: FSMContext):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return
    await safe_answer(callback)

    await state.update_data(pair_id=pair.id)
    await state.set_state(EditSettingsSG.waiting_for_blacklist)

    curr_bl = f"<code>{preview(pair.blacklist_words)}</code>" if pair.blacklist_words else "<i>Bo'sh</i>"
    text = f"""
{ERROR} <b>Qora Ro'yxat (Stop So'zlar)</b>

Agar manba kanaldagi postda ushbu so'zlardan biri qatnashsa, post <b>tashlanmaydi (bloklanadi)</b>.

{PIN} <b>Hozirgi stop so'zlar:</b>
{curr_bl}

<i>Taqiqlangan so'zlarni vergul bilan ajratib yozing (jami {BLACKLIST_MAX_CHARS} belgigacha):</i>
<i>Misol:</i> <code>reklama, aksiya, @begona_kanal, chegirma</code>

<i>Ro'yxatni tozalash uchun <code>/clear</code> deb yozing.</i>
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_cancel_keyboard(f"pair_view_{pair.id}"))

@router.message(EditSettingsSG.waiting_for_blacklist, SETTINGS_INPUT)
async def process_new_blacklist(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi so'zlarni yuboring.", parse_mode="HTML")
        return

    new_bl_raw = message.text.strip()
    if len(new_bl_raw) > BLACKLIST_MAX_CHARS:
        await message.answer(_too_long_text(BLACKLIST_MAX_CHARS, len(new_bl_raw)), parse_mode="HTML")
        return

    pair = await _load_pair_from_state(message, state)
    if not pair:
        return

    if new_bl_raw == "/clear":
        new_bl_raw = ""

    await db_manager.update_pair_blacklist(pair.id, new_bl_raw)
    await state.clear()

    bl_display = f"<code>{preview(new_bl_raw)}</code>" if new_bl_raw else "<i>Bo'sh (hech narsa bloklamasdan ishlaydi)</i>"

    pair = await db_manager.get_pair_by_id(pair.id)
    await message.answer(
        text=f"{SUCCESS} <b>Qora ro'yxat saqlandi:</b>\n{bl_display}",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- WORD & PHONE REPLACEMENTS EDITING ---

@router.callback_query(F.data.startswith("pair_edit_replace_"))
async def cb_edit_replacements(callback: CallbackQuery, state: FSMContext):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return
    await safe_answer(callback)

    await state.update_data(pair_id=pair.id)
    await state.set_state(EditSettingsSG.waiting_for_replacements)

    curr_rep = f"<code>{preview(pair.replace_words)}</code>" if pair.replace_words else "<i>O'rnatilmagan</i>"
    text = f"""
{REFRESH} <b>So'z va Telefon Raqam Almashtirgich</b>

Manba kanaldagi begona so'zlar, telefon raqamlari yoki belgilarni o'zingizning ma'lumotlaringizga avtomatik almashtiring.

{PIN} <b>Hozirgi qoidalar:</b>
{curr_rep}

<i>Format: <code>eski_qiymat=yangi_qiymat</code> (vergul yoki yangi qator bilan, jami {REPLACE_WORDS_MAX_CHARS} belgigacha)</i>
<i>Misol:</i>
<code>+998991112233=+998901234567
901234567=998887766
@begona_kanal=@bizning_kanal</code>

<i>Qoidalarni tozalash uchun <code>/clear</code> deb yozing.</i>
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_cancel_keyboard(f"pair_view_{pair.id}"))

@router.message(EditSettingsSG.waiting_for_replacements, SETTINGS_INPUT)
async def process_new_replacements(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi qoidalarni yuboring.", parse_mode="HTML")
        return

    raw_rep = message.text.strip()
    if len(raw_rep) > REPLACE_WORDS_MAX_CHARS:
        await message.answer(_too_long_text(REPLACE_WORDS_MAX_CHARS, len(raw_rep)), parse_mode="HTML")
        return

    pair = await _load_pair_from_state(message, state)
    if not pair:
        return

    if raw_rep == "/clear":
        raw_rep = ""

    formatted_rep = ",".join(line.strip() for line in raw_rep.splitlines() if line.strip()) if "\n" in raw_rep else raw_rep

    await db_manager.update_replace_words(pair.id, formatted_rep)
    await state.clear()

    pair = await db_manager.get_pair_by_id(pair.id)
    safe_rep = preview(formatted_rep) if formatted_rep else "Tozalandi"
    await message.answer(
        text=f"{SUCCESS} <b>So'z va telefon raqam almashtirish qoidalari saqlandi:</b>\n<code>{safe_rep}</code>",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- VIDEO WATERMARK SETTINGS ---

def _video_watermark_text(pair: ChannelPair) -> str:
    text = pair.video_watermark_text or pair.image_watermark_text or _default_watermark_text(pair)
    curr_status = f"{SUCCESS} Yoqilgan" if pair.video_watermark_type != "none" else f"{ERROR} O'chirilgan"
    curr_text = f"<code>{preview(text, WATERMARK_TEXT_MAX_CHARS)}</code>" if text else "<i>O'rnatilmagan</i>"
    return f"""
{ROCKET} <b>Smart Video Watermarking (FFmpeg)</b>

Videolarga brendingiz yoki kanalingiz havolasini avtomatik tushirish.

{PIN} <b>Holat:</b> {curr_status}
{SIGNATURE} <b>Matn:</b> {curr_text}
{LOCATION} <b>Pozitsiya:</b> <code>{html_escape(pair.video_watermark_pos)}</code>
"""

@router.callback_query(F.data.startswith("pair_vwm_menu_"))
async def cb_vwm_menu(callback: CallbackQuery, state: Optional[FSMContext] = None):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return
    await safe_answer(callback)
    if state:
        # "Bekor qilish" of the text editor leads here: the editor state must not survive it
        await state.clear()

    # Without a paid plan the menu stays reachable only to switch an enabled watermark off
    if pair.video_watermark_type == "none" and not await _has_paid_plan(callback.from_user.id, pair):
        await _show_upgrade_prompt(
            callback, pair, ROCKET, "Smart Video Watermarking (FFmpeg)",
            "Videolarga o'z logongiz yoki kanalingiz havolasini avtomatik tushirish funksiyasi"
        )
        return

    await edit_or_send(callback, _video_watermark_text(pair), parse_mode="HTML", reply_markup=get_video_watermark_keyboard(pair.id, pair))

@router.callback_query(F.data.startswith("vwm_toggle_"))
async def cb_vwm_toggle(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 2))
    if not pair:
        return

    enabling = pair.video_watermark_type == "none"
    if enabling and not await _has_paid_plan(callback.from_user.id, pair):
        await safe_answer(callback, "Video Watermark faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)
        return

    wm_text = pair.video_watermark_text or pair.image_watermark_text or _default_watermark_text(pair)
    if enabling and not wm_text:
        await safe_answer(callback, "Avval 'Matnni O'zgartirish' orqali video uchun matn kiriting.", show_alert=True)
        return

    new_type = "text" if enabling else "none"
    await db_manager.update_video_watermark_settings(pair.id, new_type, wm_text, pair.video_watermark_pos)
    await safe_answer(callback, "Video Watermark: Yoqildi" if enabling else "Video Watermark: O'chirildi")

    updated_pair = await db_manager.get_pair_by_id(pair.id)
    if updated_pair:
        await edit_or_send(callback, _video_watermark_text(updated_pair), parse_mode="HTML", reply_markup=get_video_watermark_keyboard(pair.id, updated_pair))

@router.callback_query(F.data.startswith("vwm_pos_"))
async def cb_vwm_pos(callback: CallbackQuery):
    match = re.fullmatch(r"vwm_pos_(\d+)_([a-z_]+)", callback.data or "")
    new_pos = match.group(2) if match else ""
    if not match or new_pos not in WATERMARK_POSITIONS:
        await safe_answer(callback, INVALID_VALUE_TEXT, show_alert=True)
        return
    pair = await _get_accessible_pair(callback, int(match.group(1)))
    if not pair:
        return

    if not await _has_paid_plan(callback.from_user.id, pair):
        await safe_answer(callback, "Video Watermark faqat PRO va VIP tariflarida mavjud!", show_alert=True)
        return

    wm_text = pair.video_watermark_text or pair.image_watermark_text or _default_watermark_text(pair)
    await db_manager.update_video_watermark_settings(pair.id, pair.video_watermark_type, wm_text, new_pos)
    await safe_answer(callback, f"Joylashuv: {new_pos}")

    updated_pair = await db_manager.get_pair_by_id(pair.id)
    if updated_pair:
        await edit_or_send(callback, _video_watermark_text(updated_pair), parse_mode="HTML", reply_markup=get_video_watermark_keyboard(pair.id, updated_pair))

@router.callback_query(F.data.startswith("vwm_set_text_"))
async def cb_vwm_set_text(callback: CallbackQuery, state: FSMContext):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return

    if not await _has_paid_plan(callback.from_user.id, pair):
        await safe_answer(callback, "Video Watermark faqat PRO va VIP tariflarida mavjud!", show_alert=True)
        return

    await safe_answer(callback)
    await state.update_data(pair_id=pair.id)
    await state.set_state(EditSettingsSG.waiting_for_vwm_text)
    await edit_or_send(
        callback,
        f"{SIGNATURE} <b>Videolarga tushiriladigan yangi matnni yozib yuboring</b> (ko'pi bilan {WATERMARK_TEXT_MAX_CHARS} ta belgi):\n"
        f"<i>Misol:</i> <code>@mening_kanalim</code>\n\n<i>Video suv belgisini o'chirish uchun <code>/clear</code> deb yozing.</i>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(f"pair_vwm_menu_{pair.id}")
    )

@router.message(EditSettingsSG.waiting_for_vwm_text, SETTINGS_INPUT)
async def process_new_vwm_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi suv belgisini yuboring.", parse_mode="HTML")
        return

    new_text = re.sub(r'[​‌‍﻿⁠]', '', message.text).strip()
    if not new_text:
        await message.answer(f"{WARN} Suv belgisi matni bo'sh yoki ko'rinmas belgilardan iborat bo'lishi mumkin emas.", parse_mode="HTML")
        return
    if new_text != "/clear" and len(new_text) > WATERMARK_TEXT_MAX_CHARS:
        await message.answer(_too_long_text(WATERMARK_TEXT_MAX_CHARS, len(new_text)), parse_mode="HTML")
        return

    pair = await _load_pair_from_state(message, state)
    if not pair:
        return

    pos = pair.video_watermark_pos or "bottom_right"
    if new_text == "/clear":
        await db_manager.update_video_watermark_settings(pair.id, "none", "", pos)
        result_text = f"{ERROR} <b>Video suv belgisi o'chirildi.</b>"
    else:
        if not await _has_paid_plan(message.from_user.id, pair):
            await state.clear()
            await message.answer(f"{WARN} Video Watermark faqat <b>PRO</b> va <b>VIP</b> tariflarida mavjud.", parse_mode="HTML", reply_markup=get_upgrade_prompt_keyboard(pair.id, "pro"))
            return
        await db_manager.update_video_watermark_settings(pair.id, "text", new_text, pos)
        result_text = f"{SUCCESS} <b>Video watermark matni saqlandi:</b> <code>{html_escape(new_text)}</code>"
    await state.clear()

    updated_pair = await db_manager.get_pair_by_id(pair.id)
    await message.answer(
        text=result_text,
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(updated_pair) if updated_pair else get_back_to_main_keyboard()
    )

# --- DRIP FEED & NIGHT BUFFER SETTINGS ---

def _drip_text(pair: ChannelPair) -> str:
    drip_status = f"{pair.drip_delay_minutes} daqiqa" if pair.drip_delay_minutes > 0 else "Tezkor (Kechiktirishsiz)"
    return f"""
{HISTORY_CLOCK} <b>Intelligent Drip Feed & Tungi Rejim</b>

Auditoriyani spamlardan saqlash uchun postlarni navbat bilan vaqt oralig'ida tarqatish.

{PIN} <b>Kechiktirish oralig'i:</b> <code>{drip_status}</code>
{NIGHT_MODE} <b>Tungi rejim:</b> <code>{html_escape(pair.night_mode.upper())}</code>
"""

@router.callback_query(F.data.startswith("pair_drip_menu_"))
async def cb_drip_menu(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return
    await safe_answer(callback)

    # Without a paid plan the menu stays reachable only to switch an enabled delay / night mode off
    drip_enabled = pair.drip_delay_minutes > 0 or pair.night_mode != "off"
    if not drip_enabled and not await _has_paid_plan(callback.from_user.id, pair):
        await _show_upgrade_prompt(
            callback, pair, HISTORY_CLOCK, "Intelligent Drip Feed & Tungi Rejim",
            "Postlarni navbat bilan kechiktirib uzatish (Drip Feed) va tunda ovozsiz / buferda saqlash rejimlari"
        )
        return

    await edit_or_send(callback, _drip_text(pair), parse_mode="HTML", reply_markup=get_drip_feed_keyboard(pair.id, pair))

@router.callback_query(F.data.startswith("drip_delay_"))
async def cb_drip_delay(callback: CallbackQuery):
    match = re.fullmatch(r"drip_delay_(\d+)_(\d{1,5})", callback.data or "")
    delay_min = int(match.group(2)) if match else -1
    if not match or not 0 <= delay_min <= DRIP_DELAY_MAX_MINUTES:
        await safe_answer(callback, INVALID_VALUE_TEXT, show_alert=True)
        return
    pair = await _get_accessible_pair(callback, int(match.group(1)))
    if not pair:
        return

    # A delay is a PRO/VIP feature; going back to instant delivery (0) is always possible
    if delay_min > 0 and not await _has_paid_plan(callback.from_user.id, pair):
        await safe_answer(callback, "Drip Feed faqat PRO/VIP tariflarida mavjud!", show_alert=True)
        return

    await db_manager.update_drip_settings(pair.id, delay_min, pair.night_mode)
    await safe_answer(callback, f"Drip Feed: {delay_min} daqiqa o'rnatildi")
    updated_pair = await db_manager.get_pair_by_id(pair.id)
    if updated_pair:
        await edit_or_send(callback, _drip_text(updated_pair), parse_mode="HTML", reply_markup=get_drip_feed_keyboard(pair.id, updated_pair))

@router.callback_query(F.data.startswith("drip_toggle_night_"))
async def cb_drip_toggle_night(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return

    if await _has_paid_plan(callback.from_user.id, pair):
        curr_idx = NIGHT_MODES.index(pair.night_mode) if pair.night_mode in NIGHT_MODES else 0
        next_mode = NIGHT_MODES[(curr_idx + 1) % len(NIGHT_MODES)]
    elif pair.night_mode != "off":
        # Without a paid plan the button only switches the night mode off
        next_mode = "off"
    else:
        await safe_answer(callback, "Tungi rejim faqat PRO/VIP tariflarida mavjud!", show_alert=True)
        return

    await db_manager.update_drip_settings(pair.id, pair.drip_delay_minutes, next_mode)
    await safe_answer(callback, f"Tungi rejim: {next_mode.upper()}")
    updated_pair = await db_manager.get_pair_by_id(pair.id)
    if updated_pair:
        await edit_or_send(callback, _drip_text(updated_pair), parse_mode="HTML", reply_markup=get_drip_feed_keyboard(pair.id, updated_pair))

# --- AI PARAPHRASER & TONE SHIFTER SETTINGS ---

def _ai_text(pair: ChannelPair) -> str:
    return f"""
{SERVER_CPU} <b>AI Content Paraphraser & Tone Shifter</b>

Postlarni yangi uslubda qayta yozish:
├ <b>Rasmiy:</b> Jiddiy va analitik maqola formati
├ <b>Hype:</b> Qaynoq va e'tibor tortuvchi sarlavhalar
└ <b>Qisqa:</b> Eng muhim 3-5 ta tezislar (TL;DR)

{PIN} <b>Hozirgi uslub:</b> <code>{html_escape(pair.ai_paraphrase_mode.upper())}</code>
"""

@router.callback_query(F.data.startswith("pair_ai_menu_"))
async def cb_ai_menu(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return
    await safe_answer(callback)

    # Without a paid plan the menu stays reachable only to switch an enabled style off
    if pair.ai_paraphrase_mode == "off" and not await _has_paid_plan(callback.from_user.id, pair):
        await _show_upgrade_prompt(
            callback, pair, SERVER_CPU, "AI Content Paraphraser & Tone Shifter",
            "Postlarni sun'iy intellekt (AI) yordamida rasmiy, qaynoq yoki qisqa tezis formatida qayta yozish"
        )
        return

    await edit_or_send(callback, _ai_text(pair), parse_mode="HTML", reply_markup=get_ai_paraphrase_keyboard(pair.id, pair))

@router.callback_query(F.data.startswith("ai_set_"))
async def cb_ai_set(callback: CallbackQuery):
    match = re.fullmatch(r"ai_set_(\d+)_([a-z]+)", callback.data or "")
    mode = match.group(2) if match else ""
    if not match or mode not in PARAPHRASE_MODES:
        await safe_answer(callback, INVALID_VALUE_TEXT, show_alert=True)
        return
    pair = await _get_accessible_pair(callback, int(match.group(1)))
    if not pair:
        return

    if mode != "off" and not await _has_paid_plan(callback.from_user.id, pair):
        await safe_answer(callback, "AI Content Paraphraser faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)
        return

    await db_manager.update_ai_paraphrase_settings(pair.id, mode)
    await safe_answer(callback, f"AI Uslubi: {mode.upper()} o'rnatildi")
    updated_pair = await db_manager.get_pair_by_id(pair.id)
    if updated_pair:
        await edit_or_send(callback, _ai_text(updated_pair), parse_mode="HTML", reply_markup=get_ai_paraphrase_keyboard(pair.id, updated_pair))

# --- DYNAMIC AFFILIATE & CTA BUTTONS ---

@router.callback_query(F.data.startswith("pair_toggle_cta_"))
async def cb_toggle_cta(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return

    # Turning the feature off is always possible; turning it on is a PRO/VIP feature
    if not pair.auto_cta_buttons and not await _has_paid_plan(callback.from_user.id, pair):
        await safe_answer(callback, "CTA tugmalar faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)
        return

    new_status = await db_manager.toggle_auto_cta_buttons(pair.id)
    await safe_answer(callback, "CTA Tugmalar: Yoqildi" if new_status else "CTA Tugmalar: O'chirildi")
    updated_pair = await db_manager.get_pair_by_id(pair.id)
    if updated_pair:
        await render_pair_detail(updated_pair, callback.message)

# --- BACKUP & DISASTER RECOVERY ---

def _backup_text(pair: ChannelPair, count: int) -> str:
    backup_status_str = f"{SUCCESS} Yoqilgan" if pair.backup_enabled else f"{ERROR} O'chirilgan"
    return f"""
{SAVE_BACKUP} <b>Channel Disaster Recovery & Instant Mirror</b>

Kanal bloklanganda yoki boshqa kanalga ko'chirish kerak bo'lganda barcha postlarni bir klikda tiklash.

{PIN} <b>Zaxiralangan postlar soni:</b> <code>{count}</code> ta
├ {SHIELD} <b>Avto-zaxira holati:</b> {backup_status_str}
"""

async def _backup_allowed(user_id: int, pair: ChannelPair) -> bool:
    """Backup and restore need an active subscription (trial or paid), or an admin."""
    if has_admin_side(user_id, pair):
        return True
    sub = await db_manager.get_user_subscription(user_id)
    return sub.is_active

@router.callback_query(F.data.startswith("pair_backup_menu_"))
async def cb_backup_menu(callback: CallbackQuery, state: Optional[FSMContext] = None):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return
    await safe_answer(callback)
    if state:
        # "Bekor qilish" of the restore prompt leads here: the restore state must not survive it
        await state.clear()

    count = await db_manager.get_channel_backup_count(pair.id)
    await edit_or_send(callback, _backup_text(pair, count), parse_mode="HTML", reply_markup=get_backup_restore_keyboard(pair.id, count, pair))

@router.callback_query(F.data.startswith("backup_toggle_"))
async def cb_backup_toggle(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 2))
    if not pair:
        return

    if not await _backup_allowed(callback.from_user.id, pair):
        await safe_answer(callback, "Zaxira funksiyasini boshqarish uchun faol obuna kerak! /stars", show_alert=True)
        return

    await db_manager.toggle_backup_enabled(pair.id)
    updated_pair = await db_manager.get_pair_by_id(pair.id)
    if not updated_pair:
        await safe_answer(callback, PAIR_NOT_FOUND_TEXT, show_alert=True)
        return
    await safe_answer(callback, "Avto-zaxira: Yoqildi" if updated_pair.backup_enabled else "Avto-zaxira: O'chirildi")
    count = await db_manager.get_channel_backup_count(pair.id)
    await edit_or_send(callback, _backup_text(updated_pair, count), parse_mode="HTML", reply_markup=get_backup_restore_keyboard(pair.id, count, updated_pair))

@router.callback_query(F.data.startswith("backup_restore_start_"))
async def cb_backup_restore_start(callback: CallbackQuery, state: FSMContext):
    pair = await _get_accessible_pair(callback, safe_parse_id(callback.data, 3))
    if not pair:
        return

    if not await _backup_allowed(callback.from_user.id, pair):
        await safe_answer(callback, "Zaxirani tiklash uchun faol obuna kerak! /stars", show_alert=True)
        return
    if disaster_recovery_service.is_restore_running(pair.id):
        await safe_answer(callback, "Ushbu juftlik uchun tiklash allaqachon davom etmoqda!", show_alert=True)
        return

    count = await db_manager.get_channel_backup_count(pair.id)
    if count == 0:
        await safe_answer(callback, "Ushbu juftlik uchun hali zaxira postlar mavjud emas!", show_alert=True)
        return

    await safe_answer(callback)
    await state.update_data(pair_id=pair.id)
    await state.set_state(EditSettingsSG.waiting_for_restore_target)
    await edit_or_send(
        callback,
        f"{SAVE_BACKUP} <b>Qayta Tiklash Rejimi:</b>\n\nBarcha <code>{count}</code> ta postlar qaysi yangi kanalga xronologik tiklansin?\n\n"
        f"<i>Yangi kanal username yoki ID sini yuboring yoki kanaldan xabar forward qiling:</i>\n<i>Misol:</i> <code>@yangi_kanalim</code>\n\n"
        f"{WARN} <i>Siz ushbu kanalning egasi yoki xabar joylash huquqiga ega administratori bo'lishingiz, bot esa kanalda administrator bo'lishi kerak.</i>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(f"pair_backup_menu_{pair.id}")
    )

RESTORE_ERROR_TEXTS = {
    "already_running": "Ushbu juftlik uchun tiklash allaqachon davom etmoqda.",
    "target_busy": "Ushbu kanalga boshqa tiklash jarayoni davom etmoqda. Tugashini kuting.",
    "private_target": "Arxivni shaxsiy chatga tiklab bo'lmaydi — faqat kanal yoki superguruhga.",
    "aborted": "Bot kanalga xabar yubora olmay qoldi (kanaldan chiqarilgan yoki huquqlari olingan). Tiklash to'xtatildi.",
    "forbidden": "Ruxsat berilmagan! Ushbu kanal juftligi sizga tegishli emas.",
    "pair_not_found": "Kanal juftligi topilmadi.",
}

def _restore_error_text(code: str) -> str:
    return RESTORE_ERROR_TEXTS.get(code) or DESTINATION_ERROR_TEXTS.get(code) or "Tiklashda kutilmagan xatolik yuz berdi."

async def _run_restore(bot: Bot, status_msg: Message, pair_id: int, target_chat_id: int, requester_id: int):
    """Background restore with a live status message (the handler returns immediately, so the per-user
    update lock is not held for the minutes a restore takes)."""
    last_edit = 0.0

    async def progress(done: int, total: int, status: str):
        nonlocal last_edit
        now = time.monotonic()
        if status == "running" and now - last_edit < 5.0:
            return
        last_edit = now
        try:
            await show_in_place(
                status_msg,
                f"{FLASH} <b>Zaxira qayta tiklanmoqda...</b>\n\n{STATS} <b>Jarayon:</b> {done} / {total} ta post\n\n"
                f"<i>Telegram cheklovlari sababli postlar daqiqasiga taxminan 20 tadan yuboriladi.</i>",
                parse_mode="HTML"
            )
        except Exception:
            logger.debug("Could not update restore progress", exc_info=True)

    try:
        res = await disaster_recovery_service.restore_channel(
            bot, pair_id, str(target_chat_id), requester_id=requester_id, progress_callback=progress
        )
    except Exception as e:
        logger.error(f"Restore of pair #{pair_id} failed: {e}", exc_info=True)
        res = {"total_archived": 0, "restored": 0, "failed": 0, "error": "internal"}

    error = res.get("error")
    if error and error != "aborted":
        text = f"{ERROR} <b>Qayta tiklash amalga oshmadi:</b> {html_escape(_restore_error_text(error))}"
    else:
        header = f"{SUCCESS} <b>Qayta Tiklash Yakunlandi!</b>" if not error else f"{WARN} <b>Qayta tiklash to'xtatildi:</b> {html_escape(_restore_error_text(error))}"
        text = f"""
{header}
├ <b>Jami arxiv:</b> <code>{res.get('total_archived', 0)}</code> ta
├ <b>Tiklandi:</b> <code>{res.get('restored', 0)}</code> ta
└ <b>Xatoliklar:</b> <code>{res.get('failed', 0)}</code> ta
"""
    pair = await db_manager.get_pair_by_id(pair_id)
    try:
        await show_in_place(status_msg, text, parse_mode="HTML", reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard())
    except Exception:
        logger.debug("Could not deliver the restore result", exc_info=True)

@router.message(EditSettingsSG.waiting_for_restore_target, WIZARD_INPUT)
async def process_restore_target(message: Message, state: FSMContext):
    if await is_repeated_album_part(message, state):
        return

    pair = await _load_pair_from_state(message, state)
    if not pair:
        return
    cancel_kb = get_cancel_keyboard(f"pair_backup_menu_{pair.id}")
    requester_id = message.from_user.id

    if not await _backup_allowed(requester_id, pair):
        await state.clear()
        await message.answer(f"{WARN} Zaxirani tiklash uchun faol obuna kerak! /stars", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    extracted = TextProcessor.extract_channel_from_message(message)
    if not extracted or not extracted[0]:
        await message.answer(f"{WARN} Iltimos, kanal username yoki ID sini yuboring yoki kanaldan xabar forward qiling.", parse_mode="HTML", reply_markup=cancel_kb)
        return

    new_target = str(extracted[0]).strip()
    chat_target: Any = int(new_target) if new_target.lstrip("-").isdigit() else new_target
    if isinstance(chat_target, int) and chat_target > 0:
        chat_target = int(f"-100{chat_target}")

    # The requester must own/administer the destination (the bot being an admin there is not enough)
    ok, chat, error_code = await verify_destination_access(message.bot, chat_target, requester_id)
    if not ok:
        await message.answer(
            f"{WARN} <b>Xatolik:</b> {html_escape(DESTINATION_ERROR_TEXTS.get(error_code, DESTINATION_ERROR_TEXTS['not_found']))}\n\n"
            f"Kanal username / ID sini qaytadan yuboring yoki bekor qiling.",
            parse_mode="HTML",
            reply_markup=cancel_kb
        )
        return

    if disaster_recovery_service.is_restore_running(pair.id):
        await state.clear()
        await message.answer(f"{WARN} {html_escape(RESTORE_ERROR_TEXTS['already_running'])}", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    await state.clear()
    safe_target = html_escape(getattr(chat, "title", None) or new_target)
    status_msg = await message.answer(f"{FLASH} <b>Zaxirani {safe_target} kanaliga qayta tiklash boshlandi...</b>", parse_mode="HTML")
    task = asyncio.create_task(_run_restore(message.bot, status_msg, pair.id, chat.id, requester_id))
    _restore_tasks.add(task)
    task.add_done_callback(_restore_tasks.discard)
