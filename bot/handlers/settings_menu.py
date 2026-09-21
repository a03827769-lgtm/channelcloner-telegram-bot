import logging
import html
import re
from typing import Any, Optional
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from database.db_manager import db_manager
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
from config.settings import settings
from bot.handlers.cloner_menu import render_pair_detail
from services.disaster_recovery import disaster_recovery_service
from services.text_processor import TextProcessor
from services.custom_emojis import (
    TRANSLATE, IMAGE, MONEY, ROCKET, FLASH, REFRESH, CLEAN, CROWN, STARS, DIAMOND,
    SIGNATURE, ERROR, SUCCESS, PIN, LOCATION, LINK, HISTORY_CLOCK, SERVER_CPU, SAVE_BACKUP,
    WARN, NIGHT_MODE, SHIELD,
    ID_SUCCESS, ID_LOCATION, ID_TRASH, ID_BACK, ID_LINK,
    ID_SIGNATURE, ID_DOCUMENT, ID_CHANNEL, ID_CLEAN,
    clean_for_alert
)

from bot.utils import safe_answer

logger = logging.getLogger(__name__)
router = Router(name="settings_menu_router")

async def user_has_pair_access(pair, user_id: int, is_admin: bool = False) -> bool:
    if not pair:
        return False
    if pair.user_id == user_id or is_admin:
        return True
    return await db_manager.is_admin(user_id)

async def is_admin_user(user_id: int) -> bool:
    return user_id in settings.admin_ids or await db_manager.is_admin(user_id)

def safe_parse_id(data: str, index: int = -1) -> Optional[int]:
    """Safely extracts integer ID from callback data string"""
    try:
        parts = data.split("_")
        return int(parts[index])
    except (ValueError, IndexError, TypeError):
        return None


# --- AUTO-TRANSLATOR SETTINGS ---

@router.callback_query(F.data.startswith("pair_trans_menu_"))
async def cb_trans_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    curr_lang = pair.target_lang.upper() if pair.auto_translate else "O'chirilgan"
    text = f"""
{TRANSLATE} <b>Avto-Tarjima (Real-Time Auto-Translator)</b>

Manba kanaldagi xabarlar qaysi tilga avtomatik tarjima qilinsin?

{PIN} <b>Hozirgi holat:</b> {curr_lang}

<i>Kerakli tilni tanlang:</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_translate_lang_keyboard(pair_id, current_lang=pair.target_lang if pair.auto_translate else "off")
    )

@router.callback_query(F.data.startswith("trans_set_"))
async def cb_set_translate_lang(callback: CallbackQuery):
    parts = callback.data.split("_")
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None or len(parts) < 4:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    lang = parts[3]

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    if lang == "off":
        await db_manager.set_auto_translate(pair_id, enabled=False)
        await safe_answer(callback, "Avto-tarjima o'chirildi.")
    else:
        await db_manager.set_auto_translate(pair_id, enabled=True, target_lang=lang)
        await safe_answer(callback, f"Avto-tarjima yoqildi: {lang.upper()}")

    pair = await db_manager.get_pair_by_id(pair_id)
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
    await safe_answer(callback)
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    curr_wm = pair.image_watermark_text if pair.image_watermark_text else "<i>O'rnatilmagan</i>"
    curr_pos = pair.image_watermark_pos
    text = f"""
{IMAGE} <b>Rasmlarga Suv Belgisi (Watermark) Qo'yish</b>

Har bir rasm va videoga avtomatik ravishda brendingiz, kanal nomingiz yoki logotipingiz tushiriladi.

{PIN} <b>Hozirgi matn:</b> {curr_wm}
{LOCATION} <b>Joylashuvi:</b> <code>{curr_pos}</code>

<b>Yangi suv belgisi matnini yozing:</b>
<i>(Misol: <code>@mening_kanalim</code> yoki <code>Mening Brendim</code>)</i>

<i>O'chirish uchun pastdagi tugmani bosing yoki <code>/clear</code> deb yozing:</i>
"""
    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_wm_text)
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_watermark_pos_keyboard(pair_id, pair.image_watermark_pos)
    )

@router.callback_query(F.data.startswith("wm_pos_"))
async def cb_set_wm_pos(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pos = "_".join(parts[3:]) if len(parts) > 3 else "bottom_right"

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    if pos == "clear":
        await db_manager.update_watermark_settings(pair_id, wm_type="none", text="", pos="bottom_right")
        await safe_answer(callback, "Suv belgisi o'chirildi")
    else:
        current_text = pair.image_watermark_text or pair.target_channel
        await db_manager.update_watermark_settings(pair_id, wm_type="text", text=current_text, pos=pos)
        await safe_answer(callback, f"Joylashuv o'rnatildi: {pos}")

    await state.clear()
    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)

@router.message(EditSettingsSG.waiting_for_wm_text)
async def process_new_wm_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi suv belgisini yuboring.", parse_mode="HTML")
        return

    new_wm = message.text.strip()
    clean_wm = re.sub(r'[\u200b\u200c\u200d\ufeff\u2060]', '', new_wm).strip()
    if not clean_wm and new_wm != "/clear":
        await message.answer(f"{WARN} Suv belgisi matni bo'sh yoki ko'rinmas belgilardan iborat bo'lishi mumkin emas.", parse_mode="HTML")
        return
    if new_wm != "/clear":
        new_wm = clean_wm

    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer(f"{WARN} Sessiya eskirgan. Qaytadan urinib ko'ring.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, message.from_user.id):
        await state.clear()
        await message.answer(f"{ERROR} <b>Ruxsat berilmagan!</b> Ushbu kanal sizga tegishli emas.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return
    current_pos = pair.image_watermark_pos or "bottom_right"

    if new_wm == "/clear":
        await db_manager.update_watermark_settings(pair_id, wm_type="none", text="", pos=current_pos)
        msg = "Rasmlarga suv belgisi urish o'chirildi"
        icon = ERROR
    else:
        await db_manager.update_watermark_settings(pair_id, wm_type="text", text=new_wm, pos=current_pos)
        msg = "Rasmlarga suv belgisi muvaffaqiyatli o'rnatildi"
        icon = SUCCESS

    await state.clear()
    pair = await db_manager.get_pair_by_id(pair_id)
    await message.answer(
        text=f"{icon} <b>{msg}</b>",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- AFFILIATE / REFERRAL REPLACER SETTINGS ---

@router.callback_query(F.data.startswith("pair_aff_"))
async def cb_affiliate_menu(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_affiliate_rules)

    curr_rules = pair.affiliate_rules or "<i>O'rnatilmagan</i>"
    text = f"""
{MONEY} <b>Referal va Sheriklik Havolalari Almashtirgichi</b>

Manba kanaldagi begona havolalarni o'zingizning daromad keltiruvchi referal havolalaringizga almashtiring.

{PIN} <b>Hozirgi qoidalar:</b>
<code>{curr_rules}</code>

<i>Format: <code>domen=shaxsiy_link</code> (har birini yangi qatordan)</i>
<i>Misol:</i>
<code>aliexpress.com=https://s.click.aliexpress.com/e/_MY_AFF
uzum.uz=https://uzum.uz/?ref=my_ref_code
binance.com=https://binance.com/ref/12345678</code>

<i>Qoidalarni tozalash uchun <code>/clear</code> deb yozing.</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(f"pair_view_{pair_id}")
    )

@router.message(EditSettingsSG.waiting_for_affiliate_rules)
async def process_new_affiliate_rules(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi qoidalarni yuboring.", parse_mode="HTML")
        return

    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer(f"{WARN} Sessiya eskirgan. Qaytadan urinib ko'ring.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, message.from_user.id):
        await state.clear()
        await message.answer(f"{ERROR} <b>Ruxsat berilmagan!</b> Ushbu kanal sizga tegishli emas.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    rules = message.text.strip()

    if rules == "/clear":
        formatted_rules = ""
    else:
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

    await db_manager.update_affiliate_rules(pair_id, formatted_rules)
    await state.clear()

    pair = await db_manager.get_pair_by_id(pair_id)
    await message.answer(
        text=f"{SUCCESS} <b>Referal havolalar qoidalari muvaffaqiyatli saqlandi!</b>",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- PROTECTED CONTENT MODE TOGGLE ---

@router.callback_query(F.data.regexp(r"^pair_toggle_prot_(\d+)$"))
async def cb_toggle_protected(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    # Check VIP subscription or Admin
    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = callback.from_user.id in settings.admin_ids or await db_manager.is_admin(callback.from_user.id)
    if not is_admin and (sub.tier != "vip" or not sub.is_active):
        await safe_answer(
            callback,
            "Bu funksiya faqat VIP Cheksiz tarif egalari uchun! 'Tariflar' bo'limidan VIP ga o'ting.",
            show_alert=True
        )
        return

    new_status = await db_manager.toggle_protected_mode(pair_id)
    msg = "Himoyalangan (Protected) kanal rejimi yoqildi" if new_status else "Protected rejim o'chirildi"
    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)
    await safe_answer(callback, msg)

# --- VIP ANIMATED EMOJIS TOGGLE ---

@router.callback_query(F.data.regexp(r"^pair_toggle_emoji_(\d+)$"))
async def cb_toggle_emojis(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    # Check VIP/Pro/Trial subscription or admin
    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = callback.from_user.id in settings.admin_ids or await db_manager.is_admin(callback.from_user.id)
    if not is_admin and not (sub.is_active and (sub.tier in ["vip", "pro"] or sub.is_trial_active)):
        await safe_answer(
            callback,
            "Bu funksiya VIP/PRO obunachilar va 14 kunlik sinov muddati uchun! 'Tariflar' bo'limidan faollashtiring.",
            show_alert=True
        )
        return

    new_status = await db_manager.toggle_premium_emojis(pair_id)
    msg = "Telegram Premium Emojilar rejimi yoqildi" if new_status else "Premium Emojilar rejimi o'chirildi"
    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)
    await safe_answer(callback, msg)

# --- REMOVE SOURCE SIGNATURE TOGGLE ---

@router.callback_query(F.data.regexp(r"^pair_toggle_remsig_(\d+)$"))
async def cb_toggle_remove_signature(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    new_status = await db_manager.toggle_remove_signature(pair_id)
    msg = "Asl imzoni o'chirish rejimi yoqildi" if new_status else "Asl imzoni o'chirish rejimi o'chirildi"
    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)
    await safe_answer(callback, msg)

# --- SIGNATURE / WATERMARK EDITING ---

def get_signature_presets_keyboard(pair: Any) -> InlineKeyboardMarkup:
    tgt = pair.target_channel
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{tgt}",
                style="primary",
                icon_custom_emoji_id=ID_SIGNATURE,
                callback_data=f"sig_set_{pair.id}_p1"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Bizning kanal: {tgt}",
                style="primary",
                icon_custom_emoji_id=ID_DOCUMENT,
                callback_data=f"sig_set_{pair.id}_p2"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Obuna bo'ling: {tgt}",
                style="primary",
                icon_custom_emoji_id=ID_CHANNEL,
                callback_data=f"sig_set_{pair.id}_p3"
            )
        ],
        [
            InlineKeyboardButton(
                text="Imzoni tozalash",
                style="danger",
                icon_custom_emoji_id=ID_CLEAN,
                callback_data=f"sig_set_{pair.id}_clear"
            )
        ],
        [
            InlineKeyboardButton(
                text="Orqaga",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data=f"pair_view_{pair.id}"
            )
        ]
    ])

@router.callback_query(F.data.startswith("pair_edit_sig_"))
async def cb_edit_signature(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_signature)

    curr_sig = pair.custom_signature or "<i>O'rnatilmagan</i>"
    text = f"""
{SIGNATURE} <b>Matn Imzosi (Post Ostiga Matn Qo'shish)</b>

Ushbu kanalga tashlanadigan barcha postlar ostiga qo'shiladigan imzo matnini kiriting yoki tayyor shablonlardan birini tanlang.

{PIN} <b>Hozirgi imzo:</b>
{curr_sig}

<i>O'z imzo matningizni yozib yuborishingiz ham mumkin:</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_signature_presets_keyboard(pair)
    )

@router.callback_query(F.data.startswith("sig_set_"))
async def cb_set_preset_sig(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    pair_id = int(parts[2])
    preset_type = parts[3]

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    tgt = pair.target_channel
    if preset_type == "p1":
        new_sig = f"{PIN} {tgt}"
    elif preset_type == "p2":
        new_sig = f"{LINK} Bizning kanal: {tgt}"
    elif preset_type == "p3":
        new_sig = f"{LINK} Obuna bo'ling: {tgt}"
    else:
        new_sig = ""

    await db_manager.update_pair_signature(pair_id, new_sig)
    await state.clear()
    await safe_answer(callback, "Imzo muvaffaqiyatli saqlandi!")

    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)

@router.message(EditSettingsSG.waiting_for_signature)
async def process_new_signature(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi imzoni yuboring.", parse_mode="HTML")
        return

    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer(f"{WARN} Sessiya eskirgan. Qaytadan urinib ko'ring.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, message.from_user.id):
        await state.clear()
        await message.answer(f"{ERROR} <b>Ruxsat berilmagan!</b> Ushbu kanal sizga tegishli emas.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    raw_val = getattr(message, 'html_text', None) or message.text or ""
    new_sig = raw_val.strip()

    if new_sig == "/clear":
        new_sig = ""
    elif new_sig:
        from services.text_processor import TextProcessor
        new_sig = TextProcessor.ensure_closed_tags(new_sig)

    await db_manager.update_pair_signature(pair_id, new_sig)
    await state.clear()

    pair = await db_manager.get_pair_by_id(pair_id)
    disp_sig = html.escape(new_sig) if new_sig else "<i>Tozalandi</i>"
    await message.answer(
        text=f"{SUCCESS} <b>Shaxsiy imzo saqlandi:</b>\n{disp_sig}",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- BLACKLIST WORDS EDITING ---

@router.callback_query(F.data.startswith("pair_edit_black_"))
async def cb_edit_blacklist(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_blacklist)

    curr_bl = html.escape(pair.blacklist_words) if pair.blacklist_words else "<i>Bo'sh</i>"
    text = f"""
{ERROR} <b>Qora Ro'yxat (Stop So'zlar)</b>

Agar manba kanaldagi postda ushbu so'zlardan biri qatnashsa, post <b>tashlanmaydi (bloklanadi)</b>.

{PIN} <b>Hozirgi stop so'zlar:</b>
<code>{curr_bl}</code>

<i>Taqiqlangan so'zlarni vergul bilan ajratib yozing:</i>
<i>Misol:</i> <code>reklama, aksiya, @begona_kanal, chegirma</code>

<i>Ro'yxatni tozalash uchun <code>/clear</code> deb yozing.</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(f"pair_view_{pair_id}")
    )

@router.message(EditSettingsSG.waiting_for_blacklist)
async def process_new_blacklist(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi so'zlarni yuboring.", parse_mode="HTML")
        return

    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer(f"{WARN} Sessiya eskirgan. Qaytadan urinib ko'ring.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, message.from_user.id):
        await state.clear()
        await message.answer(f"{ERROR} <b>Ruxsat berilmagan!</b> Ushbu kanal sizga tegishli emas.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    new_bl_raw = message.text.strip()

    if new_bl_raw == "/clear":
        new_bl_raw = ""

    await db_manager.update_pair_blacklist(pair_id, new_bl_raw)
    await state.clear()

    # Build display string from saved blacklist
    bl_words = [w.strip() for w in new_bl_raw.split(",") if w.strip()] if new_bl_raw else []
    bl_display = ", ".join(f"<code>{html.escape(w)}</code>" for w in bl_words) if bl_words else "<i>Bo'sh (hech narsa bloklashmasdan ishlaydi)</i>"

    pair = await db_manager.get_pair_by_id(pair_id)
    await message.answer(
        text=f"{SUCCESS} <b>Qora ro'yxat saqlandi:</b>\n{bl_display}",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else None
    )

# --- WORD & PHONE REPLACEMENTS EDITING ---

@router.callback_query(F.data.startswith("pair_edit_replace_"))
async def cb_edit_replacements(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_replacements)

    curr_rep = html.escape(pair.replace_words) if pair.replace_words else "<i>O'rnatilmagan</i>"
    text = f"""
{REFRESH} <b>So'z va Telefon Raqam Almashtirgich</b>

Manba kanaldagi begona so'zlar, telefon raqamlari yoki belgilarni o'zingizning ma'lumotlaringizga avtomatik almashtiring.

{PIN} <b>Hozirgi qoidalar:</b>
<code>{curr_rep}</code>

<i>Format: <code>eski_qiymat=yangi_qiymat</code> (vergul yoki yangi qator bilan)</i>
<i>Misol:</i>
<code>+998991112233=+998901234567
901234567=998887766
@begona_kanal=@bizning_kanal</code>

<i>Qoidalarni tozalash uchun <code>/clear</code> deb yozing.</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(f"pair_view_{pair_id}")
    )

@router.message(EditSettingsSG.waiting_for_replacements)
async def process_new_replacements(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi qoidalarni yuboring.", parse_mode="HTML")
        return

    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer(f"{WARN} Sessiya eskirgan. Qaytadan urinib ko'ring.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, message.from_user.id):
        await state.clear()
        await message.answer(f"{ERROR} <b>Ruxsat berilmagan!</b> Ushbu kanal sizga tegishli emas.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    raw_rep = message.text.strip()
    if raw_rep == "/clear":
        raw_rep = ""

    formatted_rep = ",".join(line.strip() for line in raw_rep.splitlines() if line.strip()) if "\n" in raw_rep else raw_rep

    await db_manager.update_replace_words(pair_id, formatted_rep)
    await state.clear()

    pair = await db_manager.get_pair_by_id(pair_id)
    safe_rep = html.escape(formatted_rep) if formatted_rep else "Tozalandi"
    await message.answer(
        text=f"{SUCCESS} <b>So'z va telefon raqam almashtirish qoidalari saqlandi:</b>\n<code>{safe_rep}</code>",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- VIDEO WATERMARK SETTINGS ---

@router.callback_query(F.data.startswith("pair_vwm_menu_"))
async def cb_vwm_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi yoki ruxsat yo'q!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi yoki ruxsat yo'q!", show_alert=True)
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)

    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        text = f"""
{ROCKET} <b>Smart Video Watermarking (FFmpeg) — Pulli Funksiya!</b>

Videolarga o'z logongiz yoki kanalingiz havolasini avtomatik tushirish funksiyasi <b>PRO</b> va <b>VIP</b> tariflarida mavjud.

{PIN} <b>Sizning hozirgi tarifingiz:</b> <code>Free (Sinov)</code>

<i>Tarifni PRO yoki VIP ga oshirish uchun quyidagi tugmani bosing:</i>
"""
        await callback.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_upgrade_prompt_keyboard(pair_id, "pro")
        )
        return

    curr_status = f"{SUCCESS} Yoqilgan" if pair.video_watermark_type != "none" else f"{ERROR} O'chirilgan"
    curr_text = html.escape(pair.video_watermark_text or pair.image_watermark_text or pair.target_channel)
    text = f"""
{ROCKET} <b>Smart Video Watermarking (FFmpeg)</b>

Videolarga brendingiz yoki kanalingiz havolasini avtomatik tushirish.

{PIN} <b>Holat:</b> {curr_status}
{SIGNATURE} <b>Matn:</b> <code>{curr_text}</code>
{LOCATION} <b>Pozitsiya:</b> <code>{pair.video_watermark_pos}</code>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_video_watermark_keyboard(pair_id, pair))

@router.callback_query(F.data.startswith("vwm_toggle_"))
async def cb_vwm_toggle(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi yoki ruxsat yo'q!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi yoki ruxsat yo'q!", show_alert=True)
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "Video Watermark faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)
        return

    new_type = "none" if pair.video_watermark_type != "none" else "text"
    wm_text = pair.video_watermark_text or pair.image_watermark_text or pair.target_channel
    await db_manager.update_video_watermark_settings(pair_id, new_type, wm_text, pair.video_watermark_pos)

    updated_pair = await db_manager.get_pair_by_id(pair_id)
    curr_status = f"{SUCCESS} Yoqilgan" if updated_pair.video_watermark_type != "none" else f"{ERROR} O'chirilgan"
    curr_text = html.escape(updated_pair.video_watermark_text or updated_pair.image_watermark_text or updated_pair.target_channel)
    text = f"""
{ROCKET} <b>Smart Video Watermarking (FFmpeg)</b>

Videolarga brendingiz yoki kanalingiz havolasini avtomatik tushirish.

{PIN} <b>Holat:</b> {curr_status}
{SIGNATURE} <b>Matn:</b> <code>{curr_text}</code>
{LOCATION} <b>Pozitsiya:</b> <code>{updated_pair.video_watermark_pos}</code>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_video_watermark_keyboard(pair_id, updated_pair))
    await safe_answer(callback, f"Video Watermark: {'Yoqildi' if new_type != 'none' else 'Ochirildi'}")

@router.callback_query(F.data.startswith("vwm_pos_"))
async def cb_vwm_pos(callback: CallbackQuery):
    parts = callback.data.split("_")
    pair_id = int(parts[2])
    new_pos = "_".join(parts[3:])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi yoki ruxsat yo'q!", show_alert=True)
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "Video Watermark faqat PRO va VIP tariflarida mavjud!", show_alert=True)
        return

    wm_text = pair.video_watermark_text or pair.image_watermark_text or pair.target_channel
    await db_manager.update_video_watermark_settings(pair_id, pair.video_watermark_type, wm_text, new_pos)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    curr_status = f"{SUCCESS} Yoqilgan" if updated_pair.video_watermark_type != "none" else f"{ERROR} O'chirilgan"
    curr_text = html.escape(updated_pair.video_watermark_text or updated_pair.image_watermark_text or updated_pair.target_channel)
    text = f"""
{ROCKET} <b>Smart Video Watermarking (FFmpeg)</b>

Videolarga brendingiz yoki kanalingiz havolasini avtomatik tushirish.

{PIN} <b>Holat:</b> {curr_status}
{SIGNATURE} <b>Matn:</b> <code>{curr_text}</code>
{LOCATION} <b>Pozitsiya:</b> <code>{updated_pair.video_watermark_pos}</code>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_video_watermark_keyboard(pair_id, updated_pair))
    await safe_answer(callback, f"Joylashuv: {new_pos}")

@router.callback_query(F.data.startswith("vwm_set_text_"))
async def cb_vwm_set_text(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "Video Watermark faqat PRO va VIP tariflarida mavjud!", show_alert=True)
        return

    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_vwm_text)
    await callback.message.edit_text(
        text=f"{SIGNATURE} <b>Videolarga tushiriladigan yangi matnni yozib yuboring:</b>\n<i>Misol:</i> <code>@mening_kanalim</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(f"pair_vwm_menu_{pair_id}")
    )

@router.message(EditSettingsSG.waiting_for_vwm_text)
async def process_new_vwm_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, faqat matn ko'rinishidagi suv belgisini yuboring.", parse_mode="HTML")
        return

    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer(f"{WARN} Sessiya eskirgan. Qaytadan urinib ko'ring.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    new_text = message.text.strip()
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, message.from_user.id):
        await state.clear()
        await message.answer(f"{ERROR} <b>Ruxsat berilmagan!</b> Ushbu kanal sizga tegishli emas.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    pos = pair.video_watermark_pos or "bottom_right"
    await db_manager.update_video_watermark_settings(pair_id, "text", new_text, pos)
    await state.clear()

    updated_pair = await db_manager.get_pair_by_id(pair_id)
    safe_text = html.escape(new_text)
    await message.answer(
        text=f"{SUCCESS} <b>Video watermark matni saqlandi:</b> <code>{safe_text}</code>",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(updated_pair) if updated_pair else get_back_to_main_keyboard()
    )

# --- DRIP FEED & NIGHT BUFFER SETTINGS ---

@router.callback_query(F.data.startswith("pair_drip_menu_"))
async def cb_drip_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)

    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        text = f"""
{HISTORY_CLOCK} <b>Intelligent Drip Feed & Tungi Rejim — Pulli Funksiya!</b>

Postlarni navbat bilan kechiktirib uzatish (Drip Feed) va tunda ovozsiz / buferda saqlash rejimlari faqat <b>PRO</b> va <b>VIP</b> tariflarida mavjud.

{PIN} <b>Sizning hozirgi tarifingiz:</b> <code>Free (Sinov)</code>

<i>Tarifni PRO yoki VIP ga oshirish uchun quyidagi tugmani bosing:</i>
"""
        await callback.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_upgrade_prompt_keyboard(pair_id, "pro")
        )
        return

    drip_status = f"{pair.drip_delay_minutes} daqiqa" if pair.drip_delay_minutes > 0 else "Tezkor (Kechiktirishsiz)"
    text = f"""
{HISTORY_CLOCK} <b>Intelligent Drip Feed & Tungi Rejim</b>

Auditoriyani spamlardan saqlash uchun postlarni navbat bilan vaqt oralig'ida tarqatish.

{PIN} <b>Kechiktirish oralig'i:</b> <code>{drip_status}</code>
{NIGHT_MODE} <b>Tungi rejim:</b> <code>{pair.night_mode.upper()}</code>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_drip_feed_keyboard(pair_id, pair))

@router.callback_query(F.data.startswith("drip_delay_"))
async def cb_drip_delay(callback: CallbackQuery):
    parts = callback.data.split("_")
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None or len(parts) < 4:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    try:
        delay_min = int(parts[3])
    except (ValueError, IndexError):
        delay_min = 0
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "Drip Feed faqat PRO/VIP tariflarida mavjud!", show_alert=True)
        return

    await db_manager.update_drip_settings(pair_id, delay_min, pair.night_mode)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    drip_status = f"{updated_pair.drip_delay_minutes} daqiqa" if updated_pair.drip_delay_minutes > 0 else "Tezkor (Kechiktirishsiz)"
    text = f"""
{HISTORY_CLOCK} <b>Intelligent Drip Feed & Tungi Rejim</b>

Auditoriyani spamlardan saqlash uchun postlarni navbat bilan vaqt oralig'ida tarqatish.

{PIN} <b>Kechiktirish oralig'i:</b> <code>{drip_status}</code>
{NIGHT_MODE} <b>Tungi rejim:</b> <code>{updated_pair.night_mode.upper()}</code>
"""
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_drip_feed_keyboard(pair_id, updated_pair))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await safe_answer(callback, f"Drip Feed: {delay_min} daqiqa o'rnatildi")

@router.callback_query(F.data.startswith("drip_toggle_night_"))
async def cb_drip_toggle_night(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "Tungi rejim faqat PRO/VIP tariflarida mavjud!", show_alert=True)
        return

    modes = ["off", "silent", "buffer"]
    curr_idx = modes.index(pair.night_mode) if pair.night_mode in modes else 0
    next_mode = modes[(curr_idx + 1) % len(modes)]

    await db_manager.update_drip_settings(pair_id, pair.drip_delay_minutes, next_mode)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    drip_status = f"{updated_pair.drip_delay_minutes} daqiqa" if updated_pair.drip_delay_minutes > 0 else "Tezkor (Kechiktirishsiz)"
    text = f"""
{HISTORY_CLOCK} <b>Intelligent Drip Feed & Tungi Rejim</b>

Auditoriyani spamlardan saqlash uchun postlarni navbat bilan vaqt oralig'ida tarqatish.

{PIN} <b>Kechiktirish oralig'i:</b> <code>{drip_status}</code>
{NIGHT_MODE} <b>Tungi rejim:</b> <code>{updated_pair.night_mode.upper()}</code>
"""
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_drip_feed_keyboard(pair_id, updated_pair))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await safe_answer(callback, f"Tungi rejim: {next_mode.upper()}")

# --- AI PARAPHRASER & TONE SHIFTER SETTINGS ---

@router.callback_query(F.data.startswith("pair_ai_menu_"))
async def cb_ai_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)

    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        text = f"""
{SERVER_CPU} <b>AI Content Paraphraser & Tone Shifter — Pulli Funksiya!</b>

Postlarni sun'iy intellekt (AI) yordamida rasmiy, qaynoq yoki qisqa tezis formatida qayta yozish faqat <b>PRO</b> va <b>VIP</b> tariflarida mavjud.

{PIN} <b>Sizning hozirgi tarifingiz:</b> <code>Free (Sinov)</code>

<i>Tarifni PRO yoki VIP ga oshirish uchun quyidagi tugmani bosing:</i>
"""
        await callback.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_upgrade_prompt_keyboard(pair_id, "pro")
        )
        return

    text = f"""
{SERVER_CPU} <b>AI Content Paraphraser & Tone Shifter</b>

Postlarni yangi uslubda qayta yozish:
├ <b>Rasmiy:</b> Jiddiy va analitik maqola formati
├ <b>Hype:</b> Qaynoq va e'tibor tortuvchi sarlavhalar
└ <b>Qisqa:</b> Eng muhim 3-5 ta tezislar (TL;DR)

{PIN} <b>Hozirgi uslub:</b> <code>{pair.ai_paraphrase_mode.upper()}</code>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_ai_paraphrase_keyboard(pair_id, pair))

@router.callback_query(F.data.startswith("ai_set_"))
async def cb_ai_set(callback: CallbackQuery):
    parts = callback.data.split("_")
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None or len(parts) < 4:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    mode = parts[3]
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "AI Content Paraphraser faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)
        return

    await db_manager.update_ai_paraphrase_settings(pair_id, mode)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    text = f"""
{SERVER_CPU} <b>AI Content Paraphraser & Tone Shifter</b>

Postlarni yangi uslubda qayta yozish:
├ <b>Rasmiy:</b> Jiddiy va analitik maqola formati
├ <b>Hype:</b> Qaynoq va e'tibor tortuvchi sarlavhalar
└ <b>Qisqa:</b> Eng muhim 3-5 ta tezislar (TL;DR)

{PIN} <b>Hozirgi uslub:</b> <code>{updated_pair.ai_paraphrase_mode.upper()}</code>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_ai_paraphrase_keyboard(pair_id, updated_pair))
    await safe_answer(callback, f"AI Uslubi: {mode.upper()} o'rnatildi")

# --- DYNAMIC AFFILIATE & CTA BUTTONS ---

@router.callback_query(F.data.startswith("pair_toggle_cta_"))
async def cb_toggle_cta(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi yoki ruxsat yo'q!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi yoki ruxsat yo'q!", show_alert=True)
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)
    if not is_admin and not sub.is_active:
        await safe_answer(callback, "Ushbu imkoniyatdan foydalanish uchun faol obuna kerak! /stars", show_alert=True)
        return

    new_status = await db_manager.toggle_auto_cta_buttons(pair_id)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    if updated_pair:
        await render_pair_detail(updated_pair, callback.message)
    await safe_answer(callback, f"CTA Tugmalar: {'Yoqildi' if new_status else 'Ochirildi'}")

# --- BACKUP & DISASTER RECOVERY ---

@router.callback_query(F.data.startswith("pair_backup_menu_"))
async def cb_backup_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        return

    count = await db_manager.get_channel_backup_count(pair_id)
    backup_status_str = f"{SUCCESS} Yoqilgan" if pair.backup_enabled else f"{ERROR} O'chirilgan"
    text = f"""
{SAVE_BACKUP} <b>Channel Disaster Recovery & Instant Mirror</b>

Kanal bloklanganda yoki boshqa kanalga ko'chirish kerak bo'lganda barcha postlarni bir klikda tiklash.

{PIN} <b>Zaxiralangan postlar soni:</b> <code>{count}</code> ta
├ {SHIELD} <b>Avto-zaxira holati:</b> {backup_status_str}
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_backup_restore_keyboard(pair_id, count, pair))

@router.callback_query(F.data.startswith("backup_toggle_"))
async def cb_backup_toggle(callback: CallbackQuery):
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = await is_admin_user(callback.from_user.id) or await is_admin_user(pair.user_id)
    if not is_admin and not sub.is_active:
        await safe_answer(callback, "Zaxira funksiyasini boshqarish uchun faol obuna kerak! /stars", show_alert=True)
        return

    await db_manager.toggle_backup_enabled(pair_id)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    count = await db_manager.get_channel_backup_count(pair_id)
    backup_status_str = f"{SUCCESS} Yoqilgan" if updated_pair.backup_enabled else f"{ERROR} O'chirilgan"
    text = f"""
{SAVE_BACKUP} <b>Channel Disaster Recovery & Instant Mirror</b>

Kanal bloklanganda yoki boshqa kanalga ko'chirish kerak bo'lganda barcha postlarni bir klikda tiklash.

{PIN} <b>Zaxiralangan postlar soni:</b> <code>{count}</code> ta
├ {SHIELD} <b>Avto-zaxira holati:</b> {backup_status_str}
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_backup_restore_keyboard(pair_id, count, updated_pair))
    await safe_answer(callback, f"Avto-zaxira: {'Yoqildi' if updated_pair.backup_enabled else 'Ochirildi'}")

@router.callback_query(F.data.startswith("backup_restore_start_"))
async def cb_backup_restore_start(callback: CallbackQuery, state: FSMContext):
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return

    count = await db_manager.get_channel_backup_count(pair_id)
    if count == 0:
        await safe_answer(callback, "Ushbu juftlik uchun hali zaxira postlar mavjud emas!", show_alert=True)
        return

    await safe_answer(callback)
    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_restore_target)
    await callback.message.edit_text(
        text=f"{SAVE_BACKUP} <b>Qayta Tiklash Rejimi:</b>\n\nBarcha <code>{count}</code> ta postlar qaysi yangi kanalga xronologik tiklansin?\n\n<i>Yangi kanal username yoki ID sini yuboring:</i>\n<i>Misol:</i> <code>@yangi_kanalim</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(f"pair_backup_menu_{pair_id}")
    )

@router.message(EditSettingsSG.waiting_for_restore_target)
async def process_restore_target(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, kanal username yoki ID sini matn sifatida yuboring.", parse_mode="HTML")
        return

    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer(f"{WARN} Sessiya eskirgan. Qaytadan urinib ko'ring.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    extracted = TextProcessor.extract_channel_from_message(message)
    if not extracted or not extracted[0]:
        await message.answer(f"{WARN} Iltimos, kanal username yoki ID sini yuboring yoki kanaldan xabar forward qiling.", parse_mode="HTML")
        return

    new_target, _, extracted_id = extracted
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not await user_has_pair_access(pair, message.from_user.id):
        await state.clear()
        await message.answer(f"{ERROR} <b>Ruxsat berilmagan!</b> Ushbu kanal sizga tegishli emas.", parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
        return

    chat_target = int(new_target) if new_target.lstrip("-").isdigit() else new_target
    if isinstance(chat_target, int) and chat_target > 0:
        chat_target = int(f"-100{chat_target}")

    # Verify target channel access and bot administrator status
    try:
        chat = await message.bot.get_chat(chat_target)
        member = await message.bot.get_chat_member(chat_id=chat.id, user_id=message.bot.id)
        if member.status not in ["administrator", "creator"]:
            bot_info = await message.bot.get_me()
            bot_user = getattr(bot_info, 'username', 'klonlabot')
            await message.answer(
                f"{WARN} <b>Xatolik:</b> Bot ushbu kanalda administrator emas!\n\n"
                f"1. Kanalingizga botimizni (<code>@{bot_user}</code>) <b>Administrator</b> qilib qo'shing (xabar yozish ruxsati bilan).\n"
                f"2. So'ngra kanalingiz username yoki ID sini qaytadan yuboring.",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard(f"pair_backup_menu_{pair_id}")
            )
            return
    except Exception as e:
        logger.warning(f"Could not verify target channel for restore: {e}")
        bot_info = await message.bot.get_me()
        bot_user = getattr(bot_info, 'username', 'klonlabot')
        await message.answer(
            f"{WARN} <b>Xatolik:</b> Bot yangi kanalni topa olmadi yoki kanalda administrator emas!\n\n"
            f"1. Kanalingizga botimizni (<code>@{bot_user}</code>) <b>Administrator</b> qilib qo'shing.\n"
            f"2. Yangi kanal username yoki ID sini qaytadan yuboring.",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard(f"pair_backup_menu_{pair_id}")
        )
        return

    await state.clear()
    safe_target = html.escape(str(new_target))
    status_msg = await message.answer(f"{FLASH} <b>Zaxirani {safe_target} kanaliga qayta tiklash boshlandi...</b>", parse_mode="HTML")
    res = await disaster_recovery_service.restore_channel(message.bot, pair_id, str(chat_target))

    await status_msg.edit_text(
        text=f"""
{SUCCESS} <b>Qayta Tiklash Yakunlandi!</b>
├ <b>Jami arxiv:</b> <code>{res['total_archived']}</code> ta
├ <b>Tiklandi:</b> <code>{res['restored']}</code> ta
└ <b>Xatoliklar:</b> <code>{res['failed']}</code> ta
""",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )


