import asyncio
import logging
import time
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from database.db_manager import db_manager
from config.settings import settings
from bot.keyboards.inline_buttons import get_history_count_keyboard, get_pair_detail_keyboard
from services.telethon_listener import telethon_listener
from services.custom_emojis import (
    REFRESH, SUCCESS, LOADING, STATS, LINK, PARTY
)

logger = logging.getLogger(__name__)
router = Router(name="history_clone_router")

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

def user_has_pair_access(pair, user_id: int) -> bool:
    if not pair:
        return False
    return pair.user_id == user_id or user_id in settings.admin_ids

def get_cancel_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⛔ Ko'chirishni To'xtatish", callback_data=f"hist_cancel_{pair_id}")
    ]])

@router.callback_query(F.data.startswith("pair_history_"))
async def cb_history_menu(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    me = await telethon_listener.get_me()
    if not me or not telethon_listener.is_connected():
        await safe_answer(callback, "MTProto tinglovchi faol emas!", show_alert=True)
        return

    text = f"""
{REFRESH} <b>Tarixiy Postlarni Ko'chirish (History Backfill)</b>

Siz <b>{pair.source_title}</b> kanalidagi eski postlarni <b>{pair.target_title}</b> kanalingizga xronologik tartibda nusxalashingiz mumkin.

<i>Qancha post ko'chirilishini tanlang:</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_history_count_keyboard(pair_id)
    )

@router.callback_query(F.data.startswith("hist_cancel_"))
async def cb_cancel_history_clone(callback: CallbackQuery):
    await safe_answer(callback, "To'xtatilmoqda...")
    pair_id = int(callback.data.split("_")[2])
    cancelled = telethon_listener.cancel_history_clone(pair_id)
    try:
        if cancelled:
            await callback.message.edit_text(
                f"⛔ <b>Tarixni ko'chirish to'xtatildi.</b>\n\n"
                f"{STATS} Ko'chirilgan postlar saqlanib qoldi.\n\n"
                f"<i>Qaytadan boshlash uchun Tarix bo'limini oching.</i>",
                parse_mode="HTML"
            )
        else:
            await callback.answer("Jarayon allaqachon tugagan yoki topilmadi.", show_alert=True)
    except Exception as e:
        logger.debug(f"Error editing cancel message: {e}")

@router.callback_query(F.data.startswith("hist_start_"))
async def cb_start_history_clone(callback: CallbackQuery):
    parts = callback.data.split("_")
    pair_id = int(parts[2])
    limit_str = parts[3]
    limit = None if limit_str == "all" else int(limit_str)

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    # Prevent double-start
    if pair_id in telethon_listener.active_history_tasks:
        existing = telethon_listener.active_history_tasks[pair_id]
        if existing and not existing.done():
            await safe_answer(callback, "Bu juftlik uchun tarix ko'chirish allaqachon davom etmoqda!", show_alert=True)
            return

    await safe_answer(callback, "Jarayon orqa fonda boshlandi.")

    cancel_kb = get_cancel_keyboard(pair_id)

    status_msg = await callback.message.edit_text(
        f"{LOADING} <b>Tarixni ko'chirish boshlandi...</b>\n\n{LINK} Manba: {pair.source_title}\n{LINK} Maqsad: {pair.target_title}\n\n<i>Tayyorlanmoqda, iltimos kuting...</i>",
        parse_mode="HTML",
        reply_markup=cancel_kb
    )

    last_edit_time = 0

    async def progress_update(current: int, total: int, status: str):
        nonlocal last_edit_time
        now = time.time()
        if now - last_edit_time < 3.0 and status == "running":
            return
        last_edit_time = now

        pct = int((current / total * 100)) if total > 0 else 0
        filled = int(pct / 10)
        bar = "▓" * filled + "░" * (10 - filled)

        try:
            if status == "running":
                await status_msg.edit_text(
                    f"{LOADING} <b>Postlar ko'chirilmoqda:</b>\n\n"
                    f"[{bar}] {pct}%\n"
                    f"{STATS} <b>Jarayon:</b> {current} / {total} ta post\n"
                    f"{LINK} <b>Manba:</b> {pair.source_title}\n"
                    f"{LINK} <b>Maqsad:</b> {pair.target_title}\n\n"
                    f"<i>Jarayon davom etmoqda, iltimos kuting...</i>",
                    parse_mode="HTML",
                    reply_markup=cancel_kb
                )
            elif status == "completed":
                await status_msg.edit_text(
                    f"{PARTY} <b>Tarixni ko'chirish muvaffaqiyatli yakunlandi!</b>\n\n"
                    f"[{'▓' * 10}] 100%\n"
                    f"{STATS} <b>Jami qayta ishlangan postlar:</b> {current} ta\n"
                    f"{LINK} <b>Manba:</b> {pair.source_title}\n"
                    f"{LINK} <b>Maqsad:</b> {pair.target_title}\n\n"
                    f"Barcha yangi postlar ham avtomatik uzatib boriladi!",
                    parse_mode="HTML",
                    reply_markup=get_pair_detail_keyboard(pair)
                )
            elif status == "cancelled":
                await status_msg.edit_text(
                    f"⛔ <b>Tarixni ko'chirish to'xtatildi.</b>\n\n"
                    f"[{bar}] {pct}%\n"
                    f"{STATS} <b>Ko'chirilgan postlar:</b> {current} ta\n"
                    f"{LINK} <b>Manba:</b> {pair.source_title}\n"
                    f"{LINK} <b>Maqsad:</b> {pair.target_title}\n\n"
                    f"<i>Qaytadan boshlash uchun Tarix bo'limini oching.</i>",
                    parse_mode="HTML",
                    reply_markup=get_pair_detail_keyboard(pair)
                )
            elif status == "failed":
                await status_msg.edit_text(
                    f"❌ <b>Xatolik yuz berdi!</b>\n\n"
                    f"[{bar}] {pct}%\n"
                    f"{STATS} <b>Ko'chirilgan postlar:</b> {current} ta\n"
                    f"{LINK} <b>Manba:</b> {pair.source_title}\n\n"
                    f"<i>Qaytadan urinib ko'ring yoki qo'llab-quvvatlash bilan bog'laning.</i>",
                    parse_mode="HTML",
                    reply_markup=get_pair_detail_keyboard(pair)
                )
        except Exception as e:
            logger.debug(f"Error editing progress message: {e}")

    asyncio.create_task(telethon_listener.clone_history(pair, limit=limit, progress_callback=progress_update))
