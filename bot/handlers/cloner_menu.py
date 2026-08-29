import logging
from typing import Union
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from config.settings import settings
from database.db_manager import db_manager
from bot.states.cloner_states import AddChannelPairSG
from bot.keyboards.inline_buttons import (
    get_cloner_menu_keyboard,
    get_pairs_list_keyboard,
    get_pair_detail_keyboard,
    get_delete_confirmation_keyboard,
    get_cancel_keyboard
)
from services.telethon_listener import telethon_listener
from services.cloner_engine import cloner_engine
from services.text_processor import TextProcessor
from services.custom_emojis import (
    REFRESH, SETTINGS, LOCK_LOCKED, LOCK_UNLOCKED, STARS, SUCCESS, ERROR, WARN,
    DOCUMENT, LINK, CLEAN, TRANSLATE, IMAGE, MONEY, SIGNATURE,
    STATS, STATS_GROWTH, PARTY, FLASH, INFO, NUM_1, NUM_2,
    ID_STARS, ID_HOME, ID_SETTINGS
)

logger = logging.getLogger(__name__)
router = Router(name="cloner_menu_router")

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

# --- ROUTE HANDLERS ---

@router.message(Command("cloner"))
@router.callback_query(F.data == "menu_cloner")
@router.message(F.text.contains("Kanal Kloner"))
async def show_cloner_menu(event: Union[CallbackQuery, Message], state: FSMContext):
    await state.clear()
    user_id = event.from_user.id
    pairs = await db_manager.get_user_channel_pairs(user_id)

    text = f"""
{REFRESH} <b>Kanal Kloner Boshqaruv Markazi</b>

Siz ulagan kanallar soni: <b>{len(pairs)} ta</b>

Quyidagi amallardan birini tanlang:
"""
    reply_markup = get_cloner_menu_keyboard(has_pairs=len(pairs) > 0)

    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=reply_markup)

@router.callback_query(F.data == "cloner_list_pairs")
async def cb_list_pairs(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id
    pairs = await db_manager.get_user_channel_pairs(user_id)

    if not pairs:
        await callback.message.edit_text(
            text=f"{DOCUMENT} <b>Hozircha hech qanday kanal ulanmagan.</b>\n\nYangi kanal qo'shish uchun quyidagi tugmani bosing:",
            parse_mode="HTML",
            reply_markup=get_cloner_menu_keyboard(has_pairs=False)
        )
        return

    text = f"""
{DOCUMENT} <b>Sizning Ulangan Kanallaringiz ({len(pairs)} ta):</b>

Boshqarish uchun kerakli kanalni tanlang:
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_pairs_list_keyboard(pairs)
    )

# --- ADD CHANNEL PAIR WIZARD ---

@router.callback_query(F.data == "cloner_add_pair")
@router.message(F.text.contains("Yangi Kanal"))
async def cb_start_add_pair(event: Union[CallbackQuery, Message], state: FSMContext):
    user_id = event.from_user.id
    is_admin = user_id in settings.admin_ids

    can_add, max_allowed, current_count = await db_manager.can_user_add_channel(user_id, is_admin=is_admin)
    if not can_add:
        text = f"""
{LOCK_LOCKED} <b>Kanal Limiti Yetib Keldi!</b>

Sizning hozirgi tarifingiz bo'yicha maksimal <b>{max_allowed} ta</b> kanal ulash mumkin (Hozir ulangan: {current_count} ta).

Ko'proq kanal ulash uchun <b>{STARS} Tariflar & Obuna</b> bo'limidan obunangizni oshiring!
"""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Tariflar & Obuna", callback_data="menu_stars", style="success", icon_custom_emoji_id=ID_STARS)],
            [InlineKeyboardButton(text="Orqaga", callback_data="menu_cloner", style="danger", icon_custom_emoji_id=ID_HOME)]
        ])
        if isinstance(event, CallbackQuery):
            await safe_answer(event)
            await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
        else:
            await event.answer(text=text, parse_mode="HTML", reply_markup=kb)
        return

    await state.set_state(AddChannelPairSG.waiting_for_source_channel)

    text = f"""
{DOCUMENT} <b>{NUM_1}-QADAM: Manba kanalni kiriting yoki xabar uzating</b>

Postlari ko'chirilishi kerak bo'lgan kanalni tanlashning 2 ta oson yo'li:

1. <b>Eng osoni:</b> O'sha kanaldan <b>istalgan bitta xabarni</b> ushbu botga <b>FORWARD (uzatish)</b> qiling {DOCUMENT}
2. Yoki kanal username/havolasini yozib yuboring:
   <i>Misol:</i> <code>@yangiliklar_kanali</code> yoki <code>https://t.me/yangiliklar_kanali</code>
"""
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await event.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard("menu_cloner")
        )
    else:
        await event.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard("menu_cloner")
        )

@router.message(AddChannelPairSG.waiting_for_source_channel)
async def process_source_channel(message: Message, state: FSMContext):
    extracted = TextProcessor.extract_channel_from_message(message)
    if not extracted or not extracted[0]:
        await message.answer(f"{ERROR} Kanal aniqlanmadi. Iltimos, manba kanaldan biror xabarni forward qiling yoki username yozing.", parse_mode="HTML")
        return

    source_channel, source_title, source_id = extracted

    if telethon_listener.client and telethon_listener.client.is_connected():
        try:
            entity = await telethon_listener.resolve_entity(source_channel)
            if entity and hasattr(entity, 'title'):
                source_title = entity.title
            if entity and hasattr(entity, 'id'):
                source_id = entity.id
        except Exception:
            pass

    await state.update_data(
        source_channel=str(source_channel),
        source_title=source_title or str(source_channel),
        source_id=source_id
    )
    await state.set_state(AddChannelPairSG.waiting_for_target_channel)

    text = f"""
{SUCCESS} <b>Manba kanal qabul qilindi:</b>
{LINK} <b>Nomi:</b> {source_title} (<code>{source_channel}</code>)

━━━━━━━━━━━━━━━━━━━━
{DOCUMENT} <b>{NUM_2}-QADAM: Maqsadli kanalni kiriting yoki xabar uzating</b>

Postlar qaysi kanalingizga tashlanishi kerak?

1. O'z kanalingizdan <b>istalgan bitta xabarni</b> shu yerga <b>FORWARD</b> qiling {DOCUMENT}
2. Yoki kanalingiz username yoki ID raqamini yozing.

{WARN} <b>Muhim:</b> Ushbu bot kanalingizda <b>administrator</b> bo'lishi kerak!
"""
    await message.answer(text=text, parse_mode="HTML", reply_markup=get_cancel_keyboard("menu_cloner"))

@router.message(AddChannelPairSG.waiting_for_target_channel)
async def process_target_channel(message: Message, state: FSMContext, bot: Bot):
    extracted = TextProcessor.extract_channel_from_message(message)
    if not extracted or not extracted[0]:
        await message.answer(f"{ERROR} Kanal aniqlanmadi. Iltimos, kanalingizdan xabar forward qiling yoki username yozing.", parse_mode="HTML")
        return

    target_channel, target_title, target_id = extracted

    try:
        chat = await bot.get_chat(target_channel)
        target_title = chat.title or target_channel
        target_id = chat.id
        member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
        if member.status not in ["administrator", "creator"]:
            await message.answer(
                f"{WARN} <b>Xatolik:</b> Bot ushbu kanalda administrator emas!\nIltimos, avval botni kanalingizga admin qiling va qayta urinib ko'ring.",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard("menu_cloner")
            )
            return
    except Exception as e:
        logger.warning(f"Could not verify target channel admin status via Bot API: {e}")

    data = await state.get_data()
    source_channel = data["source_channel"]
    source_title = data.get("source_title", source_channel)
    source_id = data.get("source_id")

    pair_id = await db_manager.add_channel_pair(
        user_id=message.from_user.id,
        source_channel=source_channel,
        source_title=source_title,
        source_id=source_id,
        target_channel=str(target_channel),
        target_title=target_title,
        target_id=target_id,
        clean_links=True,
        custom_signature="",
        blacklist_words="",
        clone_mode="clean"
    )

    if telethon_listener.is_connected():
        await telethon_listener.refresh_monitored_channels()

    await state.clear()
    pair = await db_manager.get_pair_by_id(pair_id)

    text = f"""
{PARTY} <b>Kanal juftligi muvaffaqiyatli ulandi!</b>

├ {LINK} <b>Manba:</b> {source_title} (<code>{source_channel}</code>)
├ {LINK} <b>Maqsad:</b> {target_title} (<code>{target_channel}</code>)
├ {SUCCESS} <b>Holat:</b> Faol (Avtomatik kuzatuv yoqildi)
└ {CLEAN} <b>Reklama tozalash:</b> Yoqilgan

<i>Endi manba kanaldagi yangi xabarlar avtomatik ravishda yetib keladi!</i>
"""
    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair)
    )

# --- PAIR DETAILS & PREVIEW ---

async def render_pair_detail(pair, message_obj):
    status_icon = f"{SUCCESS} Faol" if pair.is_active else f"{ERROR} To'xtatilgan"
    clean_icon = f"{SUCCESS} Yoqilgan" if pair.clean_links else f"{ERROR} O'chirilgan"
    trans_icon = f"{SUCCESS} {pair.target_lang.upper()}" if pair.auto_translate else f"{ERROR} O'chirilgan"
    wm_icon = f"{SUCCESS} {pair.image_watermark_text or 'ON'}" if pair.image_watermark_type != "none" else f"{ERROR} O'chirilgan"
    prot_icon = f"{LOCK_UNLOCKED} Yoqilgan" if pair.is_protected_source else f"{LOCK_LOCKED} O'chirilgan"
    aff_icon = f"{SUCCESS} O'rnatilgan" if pair.affiliate_rules else f"{ERROR} O'rnatilmagan"
    emoji_icon = f"{SUCCESS} Yoqilgan (VIP)" if pair.auto_premium_emojis else f"{ERROR} O'chirilgan"
    sig_text = f"<code>{pair.custom_signature}</code>" if pair.custom_signature else "<i>O'rnatilmagan</i>"
    bl_text = f"<code>{pair.blacklist_words}</code>" if pair.blacklist_words else "<i>Bo'sh</i>"

    text = f"""
{SETTINGS} <b>Kanal Juftligi Boshqaruvi (ID: #{pair.id}):</b>

├ {LINK} <b>Manba:</b> {pair.source_title} (<code>{pair.source_channel}</code>)
├ {LINK} <b>Maqsad:</b> {pair.target_title} (<code>{pair.target_channel}</code>)
├ {FLASH} <b>Holati:</b> {status_icon}
├ {CLEAN} <b>Linklarni tozalash:</b> {clean_icon}
├ {TRANSLATE} <b>Avto-Tarjima (Translator):</b> {trans_icon}
├ {IMAGE} <b>Rasmga Watermark:</b> {wm_icon}
├ {MONEY} <b>Referal Almashtirgich:</b> {aff_icon}
├ {LOCK_UNLOCKED} <b>Protected Content Mode:</b> {prot_icon}
├ ✨ <b>Telegram Premium Emojilar:</b> {emoji_icon}
├ {SIGNATURE} <b>Matn imzosi:</b> {sig_text}
└ {ERROR} <b>Qora ro'yxat:</b> {bl_text}

Quyidagi tugmalar orqali sozlamalarni o'zgartiring:
"""
    await message_obj.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair)
    )

def user_has_pair_access(pair, user_id: int) -> bool:
    if not pair:
        return False
    return pair.user_id == user_id or user_id in settings.admin_ids

@router.callback_query(F.data.startswith("pair_view_"))
async def cb_view_pair(callback: CallbackQuery, state: FSMContext = None):
    await safe_answer(callback)
    if state:
        await state.clear()
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)

    if not pair:
        await safe_answer(callback, "Kanal juftligi topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await render_pair_detail(pair, callback.message)

@router.callback_query(F.data.startswith("pair_stats_"))
async def cb_pair_stats(callback: CallbackQuery):
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await safe_answer(callback)
    analytics = await db_manager.get_pair_analytics(pair_id)

    text = f"""
{STATS} <b>Kanal Statistikasi (#{pair.id}):</b>

├ {LINK} <b>Manba:</b> {pair.source_title}
└ {LINK} <b>Maqsad:</b> {pair.target_title}

{STATS_GROWTH} <b>Ko'rsatkichlar:</b>
├ {DOCUMENT} <b>Jami ko'chirilgan postlar:</b> <code>{analytics['total_cloned']}</code> ta
├ {INFO} <b>Bugun ko'chirilgan postlar:</b> <code>{analytics['today_cloned']}</code> ta
├ {IMAGE} <b>Rasmli postlar:</b> <code>{analytics['photos_cloned']}</code> ta
└ {DOCUMENT} <b>Videoli postlar:</b> <code>{analytics['videos_cloned']}</code> ta
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Sozlamalarga Qaytish", callback_data=f"pair_view_{pair_id}", style="primary", icon_custom_emoji_id=ID_SETTINGS)]
    ])
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("pair_preview_"))
async def cb_preview_pair(callback: CallbackQuery):
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await safe_answer(callback)
    sample_source = "🔥 Yangi texnologik kashfiyot e'lon qilindi! Batafsil ma'lumot bizning kanalimizda: @eski_kanal va https://t.me/eski_kanal."
    processed = await cloner_engine.process_post_text(sample_source, pair)

    wm_disp = pair.image_watermark_text if pair.image_watermark_text else ("Faol" if pair.image_watermark_type != "none" else "O'chirilgan")

    preview_text = f"""
{INFO} <b>Post Ko'rinishi (Prevyu Simulyatori):</b>

📥 <b>Asl xabar (Manbada):</b>
<i>\"{sample_source}\"</i>

━━━━━━━━━━━━━━━━━━━━
📤 <b>Kanalingizga tushadigan ko'rinish:</b>
{processed or '<i>Matn tozalangan yoki bloklangan</i>'}
━━━━━━━━━━━━━━━━━━━━

✨ <i>Suv belgisi: {wm_disp}</i>
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Sozlamalarga Qaytish", callback_data=f"pair_view_{pair_id}", style="primary", icon_custom_emoji_id=ID_SETTINGS)]
    ])
    await callback.message.edit_text(text=preview_text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("pair_test_post_"))
async def cb_test_post(callback: CallbackQuery):
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "⛔️ Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await safe_answer(callback, "Test xabari yuborilmoqda... ⏳")
    success, msg = await cloner_engine.send_test_post(pair)
    await safe_answer(callback, msg, show_alert=True)

@router.callback_query(F.data.regexp(r"^pair_toggle_(\d+)$"))
async def cb_toggle_pair(callback: CallbackQuery, state: FSMContext):
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    new_status = await db_manager.toggle_pair_active(pair_id)
    msg = "Kanal klonlash faollashtirildi." if new_status else "Kanal klonlash to'xtatildi."

    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)
    await safe_answer(callback, msg)

@router.callback_query(F.data.regexp(r"^pair_toggle_clean_(\d+)$"))
async def cb_toggle_clean(callback: CallbackQuery, state: FSMContext):
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    new_clean = await db_manager.toggle_clean_links(pair_id)
    msg = "Reklama tozalash yoqildi." if new_clean else "Linklarni tozalash o'chirildi."

    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)
    await safe_answer(callback, msg)

@router.callback_query(F.data.startswith("pair_delete_confirm_"))
async def cb_delete_confirm(callback: CallbackQuery):
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await safe_answer(callback)
    text = f"{WARN} <b>Haqiqatan ham #{pair_id} raqamli kanal juftligini o'chirmoqchimisiz?</b>"
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_delete_confirmation_keyboard(pair_id)
    )

@router.callback_query(F.data.startswith("pair_delete_yes_"))
async def cb_delete_yes(callback: CallbackQuery, state: FSMContext):
    pair_id = int(callback.data.split("_")[3])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await db_manager.delete_pair(pair_id)
    if telethon_listener._is_running:
        await telethon_listener.refresh_monitored_channels()
    await safe_answer(callback, "Kanal juftligi muvaffaqiyatli o'chirildi.", show_alert=True)
    await cb_list_pairs(callback, state)
