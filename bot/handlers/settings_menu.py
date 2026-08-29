import logging
from typing import Any
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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
from services.custom_emojis import (
    TRANSLATE, IMAGE, MONEY, ROCKET, FLASH,
    SIGNATURE, ERROR, SUCCESS, PIN, LOCATION, LINK, HISTORY_CLOCK, SERVER_CPU, SAVE_BACKUP,
    ID_IMAGE, ID_ERROR, ID_HOME, ID_SIGNATURE, ID_PIN, ID_LINK, ID_ROCKET, ID_REFRESH
)

logger = logging.getLogger(__name__)
router = Router(name="settings_menu_router")

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

def user_has_pair_access(pair, user_id: int) -> bool:
    if not pair:
        return False
    return pair.user_id == user_id or user_id in settings.admin_ids

# --- AUTO-TRANSLATOR SETTINGS ---

@router.callback_query(F.data.startswith("pair_trans_menu_"))
async def cb_trans_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
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
        reply_markup=get_translate_lang_keyboard(pair_id)
    )

@router.callback_query(F.data.startswith("trans_set_"))
async def cb_set_translate_lang(callback: CallbackQuery):
    parts = callback.data.split("_")
    pair_id = int(parts[2])
    lang = parts[3]

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
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

def get_watermark_pos_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Pastda O'ngda", callback_data=f"wm_pos_{pair_id}_bottom_right", style="primary", icon_custom_emoji_id=ID_IMAGE),
            InlineKeyboardButton(text="Pastda Chapda", callback_data=f"wm_pos_{pair_id}_bottom_left", style="primary", icon_custom_emoji_id=ID_IMAGE)
        ],
        [
            InlineKeyboardButton(text="Tepada O'ngda", callback_data=f"wm_pos_{pair_id}_top_right", style="primary", icon_custom_emoji_id=ID_IMAGE),
            InlineKeyboardButton(text="Markazda", callback_data=f"wm_pos_{pair_id}_center", style="primary", icon_custom_emoji_id=ID_IMAGE)
        ],
        [
            InlineKeyboardButton(text="Suv belgisini o'chirish", callback_data=f"wm_pos_{pair_id}_clear", style="danger", icon_custom_emoji_id=ID_ERROR)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair_id}", style="danger", icon_custom_emoji_id=ID_HOME)
        ]
    ])

@router.callback_query(F.data.startswith("pair_wm_menu_"))
async def cb_wm_menu(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan!", show_alert=True)
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
        reply_markup=get_watermark_pos_keyboard(pair_id)
    )

@router.callback_query(F.data.startswith("wm_pos_"))
async def cb_set_wm_pos(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    pair_id = int(parts[2])
    pos = "_".join(parts[3:])

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan!", show_alert=True)
        return

    if pos == "clear":
        await db_manager.update_watermark_settings(pair_id, wm_type="none", text="", pos="bottom_right")
        await safe_answer(callback, "Suv belgisi o'chirildi ❌")
    else:
        current_text = pair.image_watermark_text or pair.target_channel
        await db_manager.update_watermark_settings(pair_id, wm_type="text", text=current_text, pos=pos)
        await safe_answer(callback, f"Joylashuv o'rnatildi: {pos} ✅")

    await state.clear()
    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)

@router.message(EditSettingsSG.waiting_for_wm_text)
async def process_new_wm_text(message: Message, state: FSMContext):
    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer("⚠️ Sessiya eskirgan. Qaytadan urinib ko'ring.", reply_markup=get_back_to_main_keyboard())
        return

    new_wm = message.text.strip()

    if new_wm == "/clear":
        await db_manager.update_watermark_settings(pair_id, wm_type="none", text="", pos="bottom_right")
        msg = "Rasmlarga suv belgisi urish o'chirildi"
        icon = ERROR
    else:
        await db_manager.update_watermark_settings(pair_id, wm_type="text", text=new_wm, pos="bottom_right")
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
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan!", show_alert=True)
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
    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer("⚠️ Sessiya eskirgan. Qaytadan urinib ko'ring.", reply_markup=get_back_to_main_keyboard())
        return

    rules = message.text.strip()

    if rules == "/clear":
        rules = ""

    await db_manager.update_affiliate_rules(pair_id, rules)
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
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
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
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    # Check VIP subscription
    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = callback.from_user.id in settings.admin_ids
    if not is_admin and (sub.tier != "vip" or not sub.is_active):
        await safe_answer(
            callback,
            "Bu funksiya faqat VIP Cheksiz tarif egalari uchun! 'Tariflar' bo'limidan VIP ga o'ting.",
            show_alert=True
        )
        return

    new_status = await db_manager.toggle_premium_emojis(pair_id)
    msg = "Telegram Premium Emojilar rejimi yoqildi" if new_status else "Premium Emojilar rejimi o'chirildi"
    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)
    await safe_answer(callback, msg)

# --- SIGNATURE / WATERMARK EDITING ---

def get_signature_presets_keyboard(pair: Any) -> InlineKeyboardMarkup:
    tgt = pair.target_channel
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{tgt}", callback_data=f"sig_set_{pair.id}_p1", style="primary", icon_custom_emoji_id=ID_PIN)
        ],
        [
            InlineKeyboardButton(text=f"Bizning kanal: {tgt}", callback_data=f"sig_set_{pair.id}_p2", style="primary", icon_custom_emoji_id=ID_SIGNATURE)
        ],
        [
            InlineKeyboardButton(text=f"Obuna bo'ling: {tgt}", callback_data=f"sig_set_{pair.id}_p3", style="primary", icon_custom_emoji_id=ID_LINK)
        ],
        [
            InlineKeyboardButton(text="Imzoni tozalash", callback_data=f"sig_set_{pair.id}_clear", style="danger", icon_custom_emoji_id=ID_ERROR)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data=f"pair_view_{pair.id}", style="danger", icon_custom_emoji_id=ID_HOME)
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
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan!", show_alert=True)
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
    if not user_has_pair_access(pair, callback.from_user.id):
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
    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer("⚠️ Sessiya eskirgan. Qaytadan urinib ko'ring.", reply_markup=get_back_to_main_keyboard())
        return

    new_sig = message.text.strip()

    if new_sig == "/clear":
        new_sig = ""

    await db_manager.update_pair_signature(pair_id, new_sig)
    await state.clear()

    pair = await db_manager.get_pair_by_id(pair_id)
    await message.answer(
        text=f"{SUCCESS} <b>Shaxsiy imzo saqlandi:</b>\n{new_sig or '<i>Tozalandi</i>'}",
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
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan!", show_alert=True)
        return

    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_blacklist)

    curr_bl = pair.blacklist_words or "<i>Bo'sh</i>"
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
    data = await state.get_data()
    pair_id = data["pair_id"]
    new_bl_raw = message.text.strip()

    if new_bl_raw == "/clear":
        new_bl_raw = ""

    await db_manager.update_pair_blacklist(pair_id, new_bl_raw)
    await state.clear()

    # Build display string from saved blacklist (BUG #1 fix: bl_display was undefined)
    bl_words = [w.strip() for w in new_bl_raw.split(",") if w.strip()] if new_bl_raw else []
    bl_display = ", ".join(f"<code>{w}</code>" for w in bl_words) if bl_words else "<i>Bo'sh (hech narsa bloklashmasdan ishlaydi)</i>"

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
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan!", show_alert=True)
        return

    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_replacements)

    curr_rep = pair.replace_words or "<i>O'rnatilmagan</i>"
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
    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer("⚠️ Sessiya eskirgan. Qaytadan urinib ko'ring.", reply_markup=get_back_to_main_keyboard())
        return

    raw_rep = message.text.strip()
    if raw_rep == "/clear":
        raw_rep = ""

    formatted_rep = ",".join(line.strip() for line in raw_rep.splitlines() if line.strip()) if "\n" in raw_rep else raw_rep

    await db_manager.update_replace_words(pair_id, formatted_rep)
    await state.clear()

    pair = await db_manager.get_pair_by_id(pair_id)
    await message.answer(
        text=f"{SUCCESS} <b>So'z va telefon raqam almashtirish qoidalari saqlandi:</b>\n<code>{formatted_rep or 'Tozalandi'}</code>",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_back_to_main_keyboard()
    )

# --- VIDEO WATERMARK SETTINGS ---

@router.callback_query(F.data.startswith("pair_vwm_menu_"))
async def cb_vwm_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Kanal topilmadi yoki ruxsat yo'q!", show_alert=True)
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = callback.from_user.id in settings.admin_ids or (pair.user_id in settings.admin_ids)

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
    curr_text = pair.video_watermark_text or pair.image_watermark_text or pair.target_channel
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
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = callback.from_user.id in settings.admin_ids or (pair.user_id in settings.admin_ids)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "🔒 Video Watermark faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)
        return

    new_type = "none" if pair.video_watermark_type != "none" else "text"
    wm_text = pair.video_watermark_text or pair.image_watermark_text or pair.target_channel
    await db_manager.update_video_watermark_settings(pair_id, new_type, wm_text, pair.video_watermark_pos)

    updated_pair = await db_manager.get_pair_by_id(pair_id)
    await callback.message.edit_reply_markup(reply_markup=get_video_watermark_keyboard(pair_id, updated_pair))

@router.callback_query(F.data.startswith("vwm_pos_"))
async def cb_vwm_pos(callback: CallbackQuery):
    await safe_answer(callback)
    parts = callback.data.split("_")
    pair_id = int(parts[2])
    new_pos = "_".join(parts[3:])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = callback.from_user.id in settings.admin_ids or (pair.user_id in settings.admin_ids)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "🔒 Video Watermark faqat PRO va VIP tariflarida mavjud!", show_alert=True)
        return

    wm_text = pair.video_watermark_text or pair.image_watermark_text or pair.target_channel
    await db_manager.update_video_watermark_settings(pair_id, pair.video_watermark_type, wm_text, new_pos)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    await callback.message.edit_reply_markup(reply_markup=get_video_watermark_keyboard(pair_id, updated_pair))

@router.callback_query(F.data.startswith("vwm_set_text_"))
async def cb_vwm_set_text(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = callback.from_user.id in settings.admin_ids or (pair.user_id in settings.admin_ids)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "🔒 Video Watermark faqat PRO va VIP tariflarida mavjud!", show_alert=True)
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
    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer("⚠️ Sessiya eskirgan. Qaytadan urinib ko'ring.", reply_markup=get_back_to_main_keyboard())
        return

    new_text = message.text.strip()
    pair = await db_manager.get_pair_by_id(pair_id)
    pos = pair.video_watermark_pos if pair else "bottom_right"
    await db_manager.update_video_watermark_settings(pair_id, "text", new_text, pos)
    await state.clear()

    updated_pair = await db_manager.get_pair_by_id(pair_id)
    await message.answer(
        text=f"{SUCCESS} <b>Video watermark matni saqlandi:</b> <code>{new_text}</code>",
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(updated_pair) if updated_pair else get_back_to_main_keyboard()
    )

# --- DRIP FEED & NIGHT BUFFER SETTINGS ---

@router.callback_query(F.data.startswith("pair_drip_menu_"))
async def cb_drip_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    drip_status = f"{pair.drip_delay_minutes} daqiqa" if pair.drip_delay_minutes > 0 else "Tezkor (Kechiktirishsiz)"
    text = f"""
{HISTORY_CLOCK} <b>Intelligent Drip Feed & Tungi Rejim</b>

Auditoriyani spamlardan saqlash uchun postlarni navbat bilan vaqt oralig'ida tarqatish.

{PIN} <b>Kechiktirish oralig'i:</b> <code>{drip_status}</code>
🌙 <b>Tungi rejim:</b> <code>{pair.night_mode.upper()}</code>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_drip_feed_keyboard(pair_id, pair))

@router.callback_query(F.data.startswith("drip_delay_"))
async def cb_drip_delay(callback: CallbackQuery):
    await safe_answer(callback)
    parts = callback.data.split("_")
    pair_id = int(parts[2])
    delay_min = int(parts[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    await db_manager.update_drip_settings(pair_id, delay_min, pair.night_mode)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    await callback.message.edit_reply_markup(reply_markup=get_drip_feed_keyboard(pair_id, updated_pair))

@router.callback_query(F.data.startswith("drip_toggle_night_"))
async def cb_drip_toggle_night(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    modes = ["off", "silent", "buffer"]
    curr_idx = modes.index(pair.night_mode) if pair.night_mode in modes else 0
    next_mode = modes[(curr_idx + 1) % len(modes)]

    await db_manager.update_drip_settings(pair_id, pair.drip_delay_minutes, next_mode)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    await callback.message.edit_reply_markup(reply_markup=get_drip_feed_keyboard(pair_id, updated_pair))

# --- AI PARAPHRASER & TONE SHIFTER SETTINGS ---

@router.callback_query(F.data.startswith("pair_ai_menu_"))
async def cb_ai_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = callback.from_user.id in settings.admin_ids or (pair.user_id in settings.admin_ids)

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
    await safe_answer(callback)
    parts = callback.data.split("_")
    pair_id = int(parts[2])
    mode = parts[3]
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    sub = await db_manager.get_user_subscription(callback.from_user.id)
    is_admin = callback.from_user.id in settings.admin_ids or (pair.user_id in settings.admin_ids)
    if not is_admin and (sub.tier not in ["pro", "vip"] or not sub.is_active):
        await safe_answer(callback, "🔒 AI Content Paraphraser faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)
        return

    await db_manager.update_ai_paraphrase_settings(pair_id, mode)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    await callback.message.edit_reply_markup(reply_markup=get_ai_paraphrase_keyboard(pair_id, updated_pair))

# --- DYNAMIC AFFILIATE & CTA BUTTONS ---

@router.callback_query(F.data.startswith("pair_toggle_cta_"))
async def cb_toggle_cta(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    new_status = await db_manager.toggle_auto_cta_buttons(pair_id)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    await callback.message.edit_reply_markup(reply_markup=get_pair_detail_keyboard(updated_pair))
    await safe_answer(callback, f"CTA Tugmalar: {'Yoqildi' if new_status else 'Ochirildi'}", show_alert=False)

# --- BACKUP & DISASTER RECOVERY ---

@router.callback_query(F.data.startswith("pair_backup_menu_"))
async def cb_backup_menu(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    count = await db_manager.get_channel_backup_count(pair_id)
    text = f"""
{SAVE_BACKUP} <b>Channel Disaster Recovery & Instant Mirror</b>

Kanal bloklanganda yoki boshqa kanalga ko'chirish kerak bo'lganda barcha postlarni bir klikda tiklash.

{PIN} <b>Zaxiralangan postlar soni:</b> <code>{count}</code> ta
🛡 <b>Avto-zaxira holati:</b> {'Yoqilgan' if pair.backup_enabled else 'Ochirildi'}
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_backup_restore_keyboard(pair_id, count, pair))

@router.callback_query(F.data.startswith("backup_toggle_"))
async def cb_backup_toggle(callback: CallbackQuery):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    await db_manager.toggle_backup_enabled(pair_id)
    updated_pair = await db_manager.get_pair_by_id(pair_id)
    count = await db_manager.get_channel_backup_count(pair_id)
    await callback.message.edit_reply_markup(reply_markup=get_backup_restore_keyboard(pair_id, count, updated_pair))

@router.callback_query(F.data.startswith("backup_restore_start_"))
async def cb_backup_restore_start(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair or not user_has_pair_access(pair, callback.from_user.id):
        return

    count = await db_manager.get_channel_backup_count(pair_id)
    if count == 0:
        await safe_answer(callback, "Ushbu juftlik uchun hali zaxira postlar mavjud emas!", show_alert=True)
        return

    await state.update_data(pair_id=pair_id)
    await state.set_state(EditSettingsSG.waiting_for_restore_target)
    await callback.message.edit_text(
        text=f"{SAVE_BACKUP} <b>Qayta Tiklash Rejimi:</b>\n\nBarcha <code>{count}</code> ta postlar qaysi yangi kanalga xronologik tiklansin?\n\n<i>Yangi kanal username yoki ID sini yuboring:</i>\n<i>Misol:</i> <code>@yangi_kanalim</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(f"pair_backup_menu_{pair_id}")
    )

@router.message(EditSettingsSG.waiting_for_restore_target)
async def process_restore_target(message: Message, state: FSMContext):
    data = await state.get_data()
    pair_id = data.get("pair_id")
    if not pair_id:
        await state.clear()
        await message.answer("⚠️ Sessiya eskirgan. Qaytadan urinib ko'ring.", reply_markup=get_back_to_main_keyboard())
        return

    new_target = message.text.strip()
    pair = await db_manager.get_pair_by_id(pair_id)
    await state.clear()

    status_msg = await message.answer(f"{FLASH} <b>Zaxirani {new_target} kanaliga qayta tiklash boshlandi...</b>", parse_mode="HTML")
    res = await disaster_recovery_service.restore_channel(message.bot, pair_id, new_target)

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

