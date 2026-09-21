import asyncio
import html
import logging
import time
from typing import Set
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from database.db_manager import db_manager
from config.settings import settings
from bot.keyboards.inline_buttons import get_history_count_keyboard, get_pair_detail_keyboard
from services.telethon_listener import telethon_listener
from services.custom_emojis import (
    REFRESH, SUCCESS, LOADING, STATS, LINK, PARTY, WARN, ERROR, ID_ERROR, ID_BACK, clean_for_alert
)

from bot.utils import safe_answer, html_escape

logger = logging.getLogger(__name__)
router = Router(name="history_clone_router")
_history_tasks: Set[asyncio.Task] = set()
_pending_start_pairs: Set[int] = set()

def user_has_pair_access(pair, user_id: int, is_admin: bool = False) -> bool:
    if not pair:
        return False
    return pair.user_id == user_id or is_admin or db_manager.is_admin_sync(user_id)

def get_cancel_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Ko'chirishni To'xtatish",
            style="danger",
            icon_custom_emoji_id=ID_ERROR,
            callback_data=f"hist_cancel_{pair_id}"
        )
    ]])

@router.callback_query(F.data.startswith("pair_history_"))
async def cb_history_menu(callback: CallbackQuery, state: FSMContext):
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not user_has_pair_access(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    if not pair.is_active:
        await safe_answer(callback, "Ushbu kanal juftligi to'xtatilgan! Tarixni ko'chirish uchun avval uni faollashtiring.", show_alert=True)
        return

    me = await telethon_listener.get_me()
    if not me or not telethon_listener.is_connected():
        await safe_answer(callback, "MTProto tinglovchi faol emas!", show_alert=True)
        return

    await safe_answer(callback)
    src_title = html_escape(pair.source_title or 'Manba')
    tgt_title = html_escape(pair.target_title or 'Maqsad')
    text = f"""
{REFRESH} <b>Tarixiy Postlarni Ko'chirish (History Backfill)</b>

Siz <b>{src_title}</b> kanalidagi eski postlarni <b>{tgt_title}</b> kanalingizga xronologik tartibda nusxalashingiz mumkin.

<i>Qancha post ko'chirilishini tanlang:</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_history_count_keyboard(pair_id)
    )

@router.callback_query(F.data.startswith("hist_cancel_"))
async def cb_cancel_history_clone(callback: CallbackQuery):
    pair_id = int(callback.data.split("_")[2])
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    user_id = getattr(getattr(callback, "from_user", None), "id", None)
    if isinstance(user_id, int) and not user_has_pair_access(pair, user_id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    await safe_answer(callback, "To'xtatilmoqda...")
    cancelled = telethon_listener.cancel_history_clone(pair_id)
    try:
        if cancelled:
            reply_markup = get_pair_detail_keyboard(pair) if pair else InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Orqaga", style="danger", icon_custom_emoji_id=ID_BACK, callback_data=f"pair_view_{pair_id}")]]
            )
            await callback.message.edit_text(
                f"{WARN} <b>Tarixni ko'chirish to'xtatildi.</b>\n\n"
                f"{STATS} Ko'chirilgan postlar saqlanib qoldi.\n\n"
                f"<i>Qaytadan boshlash uchun Tarix bo'limini oching.</i>",
                parse_mode="HTML",
                reply_markup=reply_markup
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

    if not pair.is_active:
        await safe_answer(callback, "Kanal juftligi to'xtatilgan! Tarixni ko'chirish uchun avval uni faollashtiring.", show_alert=True)
        return

    # Prevent double-start
    if pair_id in telethon_listener.active_history_tasks:
        existing = telethon_listener.active_history_tasks[pair_id]
        if existing and not existing.done():
            await safe_answer(callback, "Bu juftlik uchun tarix ko'chirish allaqachon davom etmoqda!", show_alert=True)
            return

    if pair_id in _pending_start_pairs:
        await safe_answer(callback, "Bu juftlik uchun tarix ko'chirish boshlanmoqda...", show_alert=True)
        return
    _pending_start_pairs.add(pair_id)

    try:
        await safe_answer(callback, "Jarayon orqa fonda boshlandi.")

        cancel_kb = get_cancel_keyboard(pair_id)

        src_title_initial = html_escape(pair.source_title or 'Manba')
        tgt_title_initial = html_escape(pair.target_title or 'Maqsad')
        status_msg = await callback.message.edit_text(
            f"{LOADING} <b>Tarixni ko'chirish boshlandi...</b>\n\n{LINK} Manba: {src_title_initial}\n{LINK} Maqsad: {tgt_title_initial}\n\n<i>Tayyorlanmoqda, iltimos kuting...</i>",
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

            pct = min(100, int((current / total * 100))) if total > 0 else 0
            filled = min(10, max(0, int(pct / 10)))
            bar = "▓" * filled + "░" * (10 - filled)

            src_title = html.escape(str(pair.source_title or 'Manba'))
            tgt_title = html.escape(str(pair.target_title or 'Maqsad'))

            try:
                if status == "running":
                    await status_msg.edit_text(
                        f"{LOADING} <b>Postlar ko'chirilmoqda:</b>\n\n"
                        f"[{bar}] {pct}%\n"
                        f"{STATS} <b>Jarayon:</b> {current} / {total} ta post\n"
                        f"{LINK} <b>Manba:</b> {src_title}\n"
                        f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                        f"<i>Jarayon davom etmoqda, iltimos kuting...</i>",
                        parse_mode="HTML",
                        reply_markup=cancel_kb
                    )
                elif status == "all_cloned":
                    await status_msg.edit_text(
                        f"{SUCCESS} <b>Barcha postlar allaqachon ko'chirilgan!</b>\n\n"
                        f"{STATS} Tanlangan manba kanalida yangi ko'chirilmagan postlar topilmadi.\n\n"
                        f"{LINK} <b>Manba:</b> {src_title}\n"
                        f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                        f"Barcha yangi postlar ham avtomatik uzatib boriladi!",
                        parse_mode="HTML",
                        reply_markup=get_pair_detail_keyboard(pair)
                    )
                elif status == "completed":
                    await status_msg.edit_text(
                        f"{PARTY} <b>Tarixni ko'chirish muvaffaqiyatli yakunlandi!</b>\n\n"
                        f"[{'▓' * 10}] 100%\n"
                        f"{STATS} <b>Ko'chirilgan postlar:</b> {current} ta\n"
                        f"{LINK} <b>Manba:</b> {src_title}\n"
                        f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                        f"Barcha yangi postlar ham avtomatik uzatib boriladi!",
                        parse_mode="HTML",
                        reply_markup=get_pair_detail_keyboard(pair)
                    )
                elif status == "cancelled":
                    await status_msg.edit_text(
                        f"{WARN} <b>Tarixni ko'chirish to'xtatildi.</b>\n\n"
                        f"[{bar}] {pct}%\n"
                        f"{STATS} <b>Ko'chirilgan postlar:</b> {current} ta\n"
                        f"{LINK} <b>Manba:</b> {src_title}\n"
                        f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                        f"<i>Qaytadan boshlash uchun Tarix bo'limini oching.</i>",
                        parse_mode="HTML",
                        reply_markup=get_pair_detail_keyboard(pair)
                    )
                elif status == "failed":
                    await status_msg.edit_text(
                        f"{ERROR} <b>Xatolik yuz berdi!</b>\n\n"
                        f"[{bar}] {pct}%\n"
                        f"{STATS} <b>Ko'chirilgan postlar:</b> {current} ta\n"
                        f"{LINK} <b>Manba:</b> {src_title}\n"
                        f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                        f"<i>Qaytadan urinib ko'ring yoki qo'llab-quvvatlash bilan bog'laning.</i>",
                        parse_mode="HTML",
                        reply_markup=get_pair_detail_keyboard(pair)
                    )
            except Exception as e:
                logger.debug(f"Error editing progress message: {e}")

        task = asyncio.create_task(telethon_listener.clone_history(pair, limit=limit, progress_callback=progress_update))
        telethon_listener.active_history_tasks[pair_id] = task
        _history_tasks.add(task)
        task.add_done_callback(_history_tasks.discard)
    finally:
        _pending_start_pairs.discard(pair_id)
