import logging
import html
from typing import Union, Optional
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
    ROCKET, HISTORY_CLOCK, SHIELD, STAR_SPARKLE, AI, INBOX, OUTBOX, PIN, FORWARD,
    ID_STARS, ID_HOME, ID_SETTINGS, ID_ROCKET, ID_SERVER_CPU, ID_HISTORY_CLOCK, ID_BACKUP,
    ID_SUCCESS, ID_BACK, ID_CROWN, ID_DOCUMENT,
    clean_for_alert
)

from bot.utils import safe_answer

logger = logging.getLogger(__name__)
router = Router(name="cloner_menu_router")


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
        reply_markup=get_pairs_list_keyboard(pairs, page=0)
    )

@router.callback_query(F.data.startswith("cloner_pairs_page_"))
async def cb_pairs_page(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    try:
        page = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        page = 0
    user_id = callback.from_user.id
    pairs = await db_manager.get_user_channel_pairs(user_id)
    text = f"""
{DOCUMENT} <b>Sizning Ulangan Kanallaringiz ({len(pairs)} ta):</b>

Boshqarish uchun kerakli kanalni tanlang:
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_pairs_list_keyboard(pairs, page=page)
    )

@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await safe_answer(callback)

# --- ADD CHANNEL PAIR WIZARD ---

@router.callback_query(F.data == "cloner_add_pair")
@router.message(F.text.contains("Yangi Kanal"))
async def cb_start_add_pair(event: Union[CallbackQuery, Message], state: FSMContext):
    user_id = event.from_user.id
    is_admin = user_id in settings.admin_ids or await db_manager.is_admin(user_id)

    can_add, max_allowed, current_count = await db_manager.can_user_add_channel(user_id, is_admin=is_admin)
    if not can_add:
        text = f"""
{LOCK_LOCKED} <b>Kanal Limiti Yetib Keldi!</b>

Sizning hozirgi tarifingiz bo'yicha maksimal <b>{max_allowed} ta</b> kanal ulash mumkin (Hozir ulangan: {current_count} ta).

Ko'proq kanal ulash uchun <b>{STARS} Tariflar & Obuna</b> bo'limidan obunangizni oshiring!
"""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Tariflar & Obuna",
                style="success",
                icon_custom_emoji_id=ID_STARS,
                callback_data="menu_stars"
            )],
            [InlineKeyboardButton(
                text="Orqaga",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data="menu_cloner"
            )]
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
            if entity:
                from telethon.tl.types import User as TelethonUser
                if isinstance(entity, TelethonUser):
                    await message.answer(
                        f"{ERROR} <b>Xatolik:</b> Kiritilgan manba shaxsiy profil (foydalanuvchi) hisoblanadi!\n\n"
                        f"Bot faqat <b>kanallar</b>dan xabar nusxalay oladi. Iltimos, Telegram kanal username yoki havolasini kiriting.",
                        parse_mode="HTML",
                        reply_markup=get_cancel_keyboard("menu_cloner")
                    )
                    return
                if hasattr(entity, 'title'):
                    source_title = entity.title
                if hasattr(entity, 'id'):
                    source_id = entity.id
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

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

    chat_target = int(target_channel) if target_channel.lstrip("-").isdigit() else target_channel
    if isinstance(chat_target, int) and chat_target > 0:
        chat_target = int(f"-100{chat_target}")
        target_id = chat_target

    if isinstance(chat_target, str) and ("/+" in chat_target or "/joinchat/" in chat_target):
        if telethon_listener.client and telethon_listener.client.is_connected():
            try:
                entity = await telethon_listener.resolve_entity(chat_target)
                if entity and hasattr(entity, 'id'):
                    raw_id = entity.id
                    if isinstance(raw_id, int) and raw_id > 0:
                        raw_id = int(f"-100{raw_id}")
                    chat_target = raw_id
                    target_id = raw_id
                    if hasattr(entity, 'title'):
                        target_title = entity.title
            except Exception as e_res:
                logger.debug(f"Could not resolve private invite link via Telethon: {e_res}")
    try:
        chat = await bot.get_chat(chat_target)
        chat_type = str(getattr(chat, "type", "")).lower()
        if chat_type in ["private", "group"] or (isinstance(getattr(chat, "id", None), int) and chat.id > 0):
            await message.answer(
                f"{ERROR} <b>Xatolik:</b> Maqsadli chat faqat <b>Kanal</b> yoki <b>Superguruh</b> bo'lishi shart!\n\n"
                f"Siz shaxsiy profil yoki oddiy guruh kiritdingiz (<code>{chat_type}</code>). Bot xabarlarni faqat kanallarga nusxalaydi.",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard("menu_cloner")
            )
            return
        target_title = chat.title or str(target_channel)
        target_id = chat.id
        member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
        is_admin_or_creator = member.status in ["administrator", "creator"]
        has_post_perm = getattr(member, 'can_post_messages', True) is not False
        if not is_admin_or_creator or not has_post_perm:
            bot_info = await bot.get_me()
            bot_user = getattr(bot_info, 'username', 'klonlabot')
            await message.answer(
                f"{WARN} <b>Xatolik:</b> Bot ushbu kanalda administrator emas yoki xabar yozish (Post Messages) huquqi berilmagan!\n\n"
                f"1. Kanalingizga botimizni (<code>@{bot_user}</code>) <b>Administrator</b> qilib qo'shing va <b>Xabar yozish (Post Messages)</b> ruxsatini bering.\n"
                f"2. So'ngra kanalingiz username yoki havolasini qaytadan yuboring.",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard("menu_cloner")
            )
            return
    except Exception as e:
        logger.warning(f"Could not verify target channel admin status via Bot API: {e}")
        bot_info = await bot.get_me()
        bot_user = getattr(bot_info, 'username', 'klonlabot')
        await message.answer(
            f"{WARN} <b>Xatolik:</b> Bot maqsadli kanalni topa olmadi yoki kanalda administrator emas!\n\n"
            f"Iltimos:\n"
            f"1. Kanalingizga botimizni (<code>@{bot_user}</code>) <b>Administrator</b> qilib qo'shing.\n"
            f"2. Kanalingizdan biror xabarni shu yerga <b>FORWARD</b> qiling yoki username/ID yozing.",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard("menu_cloner")
        )
        return

    data = await state.get_data()
    source_channel = data["source_channel"]
    source_title = data.get("source_title", source_channel)
    source_id = data.get("source_id")

    # 1. Loop prevention check: Source and target cannot be the same channel
    norm_source = db_manager._normalize_channel_name(source_channel)
    norm_target = db_manager._normalize_channel_name(str(target_channel))
    is_same_channel = False
    if source_id is not None and target_id is not None and source_id == target_id:
        is_same_channel = True
    elif norm_source and norm_target and norm_source == norm_target:
        is_same_channel = True
    elif source_id is not None and norm_target == str(source_id):
        is_same_channel = True
    elif target_id is not None and norm_source == str(target_id):
        is_same_channel = True

    if is_same_channel:
        await message.answer(
            f"{WARN} <b>Xatolik:</b> Manba va Maqsad kanali bir xil bo'lishi mumkin emas!\n\n"
            f"Kanalni o'zidan o'ziga klonlash cheksiz takrorlanish (loop) xavfini keltirib chiqaradi. "
            f"Iltimos, boshqa maqsad kanalini yuboring yoki bekor qiling.",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard("menu_cloner")
        )
        return

    # 2. Duplicate pair check: Check if this pair already exists
    existing_pair = await db_manager.find_duplicate_pair(
        user_id=message.from_user.id,
        source_channel=source_channel,
        target_channel=str(target_channel),
        source_id=source_id,
        target_id=target_id
    )
    if existing_pair:
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"Kanalni boshqarish #{existing_pair.id}",
                style="primary",
                icon_custom_emoji_id=ID_SETTINGS,
                callback_data=f"pair_view_{existing_pair.id}"
            )],
            [InlineKeyboardButton(
                text="Kanallar ro'yxati",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data="menu_cloner"
            )]
        ])
        await message.answer(
            f"{WARN} <b>Ushbu kanal juftligi allaqachon mavjud!</b>\n\n"
            f"├ {LINK} <b>Manba:</b> {existing_pair.source_title} (<code>{existing_pair.source_channel}</code>)\n"
            f"├ {LINK} <b>Maqsad:</b> {existing_pair.target_title} (<code>{existing_pair.target_channel}</code>)\n"
            f"└ {SUCCESS} <b>ID:</b> #{existing_pair.id}\n\n"
            f"Bir xil manbadan bir xil maqsadga bir necha bor ulash mumkin emas. "
            f"Mavjud kanal sozlamalarini quyidagi tugma orqali boshqarishingiz mumkin:",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

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
        await telethon_listener.join_and_monitor_channel(source_channel)

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
    wm_icon = f"{SUCCESS} {html.escape(pair.image_watermark_text) or 'ON'}" if pair.image_watermark_type != "none" else f"{ERROR} O'chirilgan"
    vwm_icon = f"{SUCCESS} {html.escape(pair.video_watermark_text or 'ON')} ({pair.video_watermark_pos})" if pair.video_watermark_type != "none" else f"{ERROR} O'chirilgan"
    drip_status = f"{pair.drip_delay_minutes}m (Tun: {pair.night_mode.upper()})" if (pair.drip_delay_minutes > 0 or pair.night_mode != "off") else f"{ERROR} O'chirilgan"
    ai_status = f"{SUCCESS} {pair.ai_paraphrase_mode.upper()}" if pair.ai_paraphrase_mode != "off" else f"{ERROR} O'chirilgan"
    cta_status = f"{SUCCESS} Yoqilgan" if pair.auto_cta_buttons else f"{ERROR} O'chirilgan"
    prot_icon = f"{LOCK_UNLOCKED} Yoqilgan" if pair.is_protected_source else f"{LOCK_LOCKED} O'chirilgan"
    aff_icon = f"{SUCCESS} O'rnatilgan" if pair.affiliate_rules else f"{ERROR} O'rnatilmagan"
    emoji_icon = f"{SUCCESS} Yoqilgan (VIP)" if pair.auto_premium_emojis else f"{ERROR} O'chirilgan"
    backup_status = f"{SUCCESS} Yoqilgan" if pair.backup_enabled else f"{ERROR} O'chirilgan"
    catchup_status = f"{SUCCESS} Yoqilgan" if getattr(pair, "auto_catchup", True) else f"{ERROR} O'chirilgan"
    last_seen_str = f"#{pair.last_seen_msg_id}" if pair.last_seen_msg_id else "<i>Aniqlanmagan</i>"
    
    sig_text = f"<code>{html.escape(pair.custom_signature)}</code>" if pair.custom_signature else "<i>O'rnatilmagan</i>"
    rep_text = f"<code>{html.escape(pair.replace_words)}</code>" if pair.replace_words else "<i>Bo'sh</i>"
    bl_text = f"<code>{html.escape(pair.blacklist_words)}</code>" if pair.blacklist_words else "<i>Bo'sh</i>"
    src_title_safe = html.escape(pair.source_title or pair.source_channel)
    tgt_title_safe = html.escape(pair.target_title or pair.target_channel)

    text = f"""
{SETTINGS} <b>Kanal Juftligi Boshqaruvi (ID: #{pair.id}):</b>

├ {LINK} <b>Manba:</b> {src_title_safe} (<code>{pair.source_channel}</code>)
├ {LINK} <b>Maqsad:</b> {tgt_title_safe} (<code>{pair.target_channel}</code>)
├ {FLASH} <b>Holati:</b> {status_icon}
├ {CLEAN} <b>Linklarni tozalash:</b> {clean_icon}
├ {TRANSLATE} <b>Avto-Tarjima:</b> {trans_icon}
├ {IMAGE} <b>Rasmga Watermark:</b> {wm_icon}
├ {ROCKET} <b>Video Watermark:</b> {vwm_icon}
├ {HISTORY_CLOCK} <b>Drip Feed / Tungi Rejim:</b> {drip_status}
├ {AI} <b>AI Paraphrase:</b> {ai_status}
├ {MONEY} <b>CTA Tugmalar:</b> {cta_status}
├ {LINK} <b>Referal Almashtirgich:</b> {aff_icon}
├ {LOCK_UNLOCKED} <b>Protected Content Mode:</b> {prot_icon}
├ {STAR_SPARKLE} <b>Telegram Premium Emojilar:</b> {emoji_icon}
├ {SHIELD} <b>Avto-Zaxira:</b> {backup_status}
├ {REFRESH} <b>Oflayn Yetkazish (Catch-Up):</b> {catchup_status} (Oxirgi ID: {last_seen_str})
├ {SIGNATURE} <b>Matn imzosi:</b> {sig_text}
├ {REFRESH} <b>So'z/Raqam almashtirish:</b> {rep_text}
└ {ERROR} <b>Qora ro'yxat:</b> {bl_text}

Quyidagi tugmalar orqali sozlamalarni o'zgartiring:
"""
    try:
        await message_obj.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_pair_detail_keyboard(pair)
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Error editing pair detail message: {e}")

def safe_parse_id(data: str, index: int = -1) -> Optional[int]:
    """Safely extracts integer ID from callback data string"""
    try:
        parts = data.split("_")
        return int(parts[index])
    except (ValueError, IndexError, TypeError):
        return None

def user_has_pair_access(pair, user_id: int, is_admin: bool = False) -> bool:
    if not pair:
        return False
    return pair.user_id == user_id or is_admin or db_manager.is_admin_sync(user_id)

@router.callback_query(F.data.startswith("pair_view_"))
async def cb_view_pair(callback: CallbackQuery, state: FSMContext = None):
    await safe_answer(callback)
    if state:
        await state.clear()
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None:
        await safe_answer(callback, "Kanal juftligi topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)

    if not pair:
        await safe_answer(callback, "Kanal juftligi topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await render_pair_detail(pair, callback.message)

@router.callback_query(F.data.startswith("pair_stats_"))
async def cb_pair_stats(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await safe_answer(callback)
    analytics = await db_manager.get_pair_analytics(pair_id)
    src_title_safe = html.escape(pair.source_title or pair.source_channel)
    tgt_title_safe = html.escape(pair.target_title or pair.target_channel)

    text = f"""
{STATS} <b>Kanal Statistikasi (#{pair.id}):</b>

├ {LINK} <b>Manba:</b> {src_title_safe}
└ {LINK} <b>Maqsad:</b> {tgt_title_safe}

{STATS_GROWTH} <b>Ko'rsatkichlar:</b>
├ {DOCUMENT} <b>Jami ko'chirilgan postlar:</b> <code>{analytics['total_cloned']}</code> ta
├ {INFO} <b>Bugun ko'chirilgan postlar:</b> <code>{analytics['today_cloned']}</code> ta
├ {IMAGE} <b>Rasmli postlar:</b> <code>{analytics['photos_cloned']}</code> ta
└ {DOCUMENT} <b>Videoli postlar:</b> <code>{analytics['videos_cloned']}</code> ta
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Sozlamalarga Qaytish",
            style="primary",
            icon_custom_emoji_id=ID_BACK,
            callback_data=f"pair_view_{pair_id}"
        )]
    ])
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("pair_preview_"))
async def cb_preview_pair(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await safe_answer(callback)
    sample_source = "Yangi texnologik kashfiyot e'lon qilindi! Batafsil ma'lumot bizning kanalimizda: @eski_kanal va https://t.me/eski_kanal."
    processed = await cloner_engine.process_post_text(sample_source, pair)

    wm_disp = pair.image_watermark_text if pair.image_watermark_text else ("Faol" if pair.image_watermark_type != "none" else "O'chirilgan")

    preview_text = f"""
{INFO} <b>Post Ko'rinishi (Prevyu Simulyatori):</b>

{INBOX} <b>Asl xabar (Manbada):</b>
<i>\"{sample_source}\"</i>

━━━━━━━━━━━━━━━━━━━━
{OUTBOX} <b>Kanalingizga tushadigan ko'rinish:</b>
{processed or '<i>Matn tozalangan yoki bloklangan</i>'}
━━━━━━━━━━━━━━━━━━━━

{STAR_SPARKLE} <i>Suv belgisi: {wm_disp}</i>
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Sozlamalarga Qaytish",
            style="primary",
            icon_custom_emoji_id=ID_BACK,
            callback_data=f"pair_view_{pair_id}"
        )]
    ])
    await callback.message.edit_text(text=preview_text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("pair_test_post_"))
async def cb_test_post(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    success, msg = await cloner_engine.send_test_post(pair)
    await safe_answer(callback, msg, show_alert=True)

@router.callback_query(F.data.regexp(r"^pair_toggle_(\d+)$"))
async def cb_toggle_pair(callback: CallbackQuery, state: FSMContext):
    pair_id = safe_parse_id(callback.data, 2)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    new_status = await db_manager.toggle_pair_active(pair_id)
    msg = "Kanal klonlash faollashtirildi" if new_status else "Kanal klonlash to'xtatildi"

    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)
    await safe_answer(callback, msg)

@router.callback_query(F.data.regexp(r"^pair_toggle_clean_(\d+)$"))
async def cb_toggle_clean(callback: CallbackQuery, state: FSMContext):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    new_clean = await db_manager.toggle_clean_links(pair_id)
    msg = "Link tozalash yoqildi" if new_clean else "Link tozalash o'chirildi"

    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)
    await safe_answer(callback, msg)

@router.callback_query(F.data.regexp(r"^pair_toggle_catchup_(\d+)$"))
async def cb_toggle_catchup(callback: CallbackQuery, state: FSMContext):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    new_val = await db_manager.toggle_auto_catchup(pair_id)
    msg = "Avto-yetkazish (Catch-Up) yoqildi" if new_val else "Avto-yetkazish o'chirildi"

    pair = await db_manager.get_pair_by_id(pair_id)
    if pair:
        await render_pair_detail(pair, callback.message)
    await safe_answer(callback, msg)

@router.callback_query(F.data.regexp(r"^pair_catchup_now_(\d+)$"))
async def cb_catchup_now(callback: CallbackQuery, state: FSMContext):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    if not telethon_listener.is_connected():
        await safe_answer(callback, "Telethon MTProto ulanmagan! Avval hisobingizni ulang.", show_alert=True)
        return

    await safe_answer(callback, "Yetkazib berish boshlandi! O'tkazib yuborilgan postlar tekshirilmoqda...", show_alert=False)

    async def _run_manual_catchup():
        try:
            res = await telethon_listener.catch_up_pair_messages(pair)
            caught = res.get("caught_up", 0)
            status = res.get("status", "unknown")
            if status == "up_to_date":
                note = f"{INFO} #{pair.id}-juftlik: Barcha postlar allaqachon yetkazilgan, yangi xabar yo'q."
            elif status == "baseline_established":
                note = f"{PIN} #{pair.id}-juftlik: Boshlang'ich chegara (#{res.get('last_id')}) o'rnatildi."
            elif caught > 0:
                note = f"{SUCCESS} #{pair.id}-juftlik: {caught} ta o'tkazib yuborilgan post muvaffaqiyatli yetkazildi!"
            else:
                note = f"{INFO} #{pair.id}-juftlik yetkazish yakunlandi (holat: {status})."
            await callback.message.answer(note, parse_mode="HTML")
            updated_p = await db_manager.get_pair_by_id(pair.id)
            if updated_p:
                await render_pair_detail(updated_p, callback.message)
        except Exception as e:
            logger.error(f"Manual catchup error for pair #{pair.id}: {e}")

    telethon_listener._spawn_task(_run_manual_catchup())

@router.message(Command("catchup"))
async def cmd_catchup(message: Message):
    """Manually triggers catch-up for all active pairs belonging to the user"""
    user_id = message.from_user.id
    if not telethon_listener.is_connected():
        await message.answer(f"{WARN} <b>Telethon sessiyasi faol emas!</b>\nIltimos, avval hisobingizni /login orqali ulang.", parse_mode="HTML")
        return

    user_pairs = await db_manager.get_user_channel_pairs(user_id)
    active_user_pairs = [p for p in user_pairs if p.is_active]
    if not active_user_pairs:
        await message.answer(f"{INFO} <b>Sizda faol kanallar juftligi mavjud emas.</b>", parse_mode="HTML")
        return

    status_msg = await message.answer(
        f"{REFRESH} <b>Oflayn qolgan postlar tekshirilmoqda...</b>\n\n"
        f"Jami tekshiriladigan faol kanallar: <b>{len(active_user_pairs)} ta</b>\n"
        f"Iltimos, kuting...",
        parse_mode="HTML"
    )

    total_caught = 0
    details = []
    for p in active_user_pairs:
        try:
            res = await telethon_listener.catch_up_pair_messages(p)
            c = res.get("caught_up", 0)
            status = res.get("status", "unknown")
            total_caught += c
            src_name = p.source_title or p.source_channel
            tgt_name = p.target_title or p.target_channel
            if c > 0:
                details.append(f"• <b>{src_name} {FORWARD} {tgt_name}:</b> {c} ta post yetkazildi")
            elif status == "up_to_date":
                details.append(f"• <b>{src_name} {FORWARD} {tgt_name}:</b> yangi post yo'q (to'liq)")
            elif status == "baseline_established":
                details.append(f"• <b>{src_name} {FORWARD} {tgt_name}:</b> boshlang'ich chegara o'rnatildi")
            else:
                details.append(f"• <b>{src_name} {FORWARD} {tgt_name}:</b> {status}")
        except Exception as e:
            logger.error(f"Catchup error on pair #{p.id}: {e}")
            details.append(f"• #{p.id}: Xatolik ({e})")

    report = (
        f"{SUCCESS} <b>Oflayn xabarlarni yetkazish yakunlandi!</b>\n\n"
        f"{INBOX} Jami yetkazilgan postlar: <b>{total_caught} ta</b>\n\n"
        + "\n".join(details)
    )
    await status_msg.edit_text(report, parse_mode="HTML")

@router.callback_query(F.data.startswith("pair_delete_confirm_"))
async def cb_delete_confirm(callback: CallbackQuery):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await safe_answer(callback)
    src_title = html.escape(pair.source_title or pair.source_channel)
    tgt_title = html.escape(pair.target_title or pair.target_channel)
    text = (
        f"{WARN} <b>Haqiqatan ham #{pair_id} raqamli kanal juftligini o'chirmoqchimisiz?</b>\n\n"
        f"├ <b>Manba:</b> {src_title}\n"
        f"└ <b>Maqsad:</b> {tgt_title}"
    )
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_delete_confirmation_keyboard(pair_id)
    )

@router.callback_query(F.data.startswith("pair_delete_yes_"))
async def cb_delete_yes(callback: CallbackQuery, state: FSMContext):
    pair_id = safe_parse_id(callback.data, 3)
    if pair_id is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await safe_answer(callback, "Kanal muvaffaqiyatli o'chirildi")
    await db_manager.delete_pair(pair_id)
    try:
        telethon_listener.cancel_history_clone(pair_id)
    except Exception:
        logger.debug("Ignored exception", exc_info=True)
    if telethon_listener._is_running:
        await telethon_listener.refresh_monitored_channels()
    await cb_list_pairs(callback, state)


@router.message(F.migrate_to_chat_id)
async def handle_aiogram_chat_migration(message: Message):
    """
    Handles group to supergroup migration in Telegram Bot API.
    Updates target_id and source_id for all matching channel pairs.
    """
    old_chat_id = message.chat.id
    new_chat_id = message.migrate_to_chat_id
    if not new_chat_id:
        return
    logger.info(f"🔄 Aiogram group migration detected: {old_chat_id} -> {new_chat_id}")
    try:
        all_pairs = await db_manager.get_all_active_pairs()
        migrated_count = 0
        for p in all_pairs:
            if p.source_id == old_chat_id:
                await db_manager.update_pair_source_id(p.id, new_chat_id)
                migrated_count += 1
            if p.target_id == old_chat_id:
                await db_manager.update_pair_target_id(p.id, new_chat_id)
                migrated_count += 1

        if migrated_count > 0:
            telethon_listener.invalidate_pairs_cache()
            logger.info(f"Aiogram updated {migrated_count} channel pair(s) following migration from {old_chat_id} to {new_chat_id}")
    except Exception as e:
        logger.error(f"Error handling Aiogram chat migration: {e}", exc_info=True)


