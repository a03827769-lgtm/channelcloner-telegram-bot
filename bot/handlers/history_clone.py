import asyncio
import logging
import re
import time
from typing import Any, Dict, Set

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from config.limits import BACKFILL_MAX_MESSAGES, BACKFILL_MAX_MESSAGES_PRIVILEGED
from database.db_manager import db_manager
from bot.access import can_manage_pair, has_admin_side
from database.models import ChannelPair
from bot.keyboards.inline_buttons import get_history_count_keyboard, get_history_progress_keyboard, get_pair_detail_keyboard
from services.channel_access import verify_destination_access, DESTINATION_ERROR_TEXTS
from services.telethon_listener import telethon_listener
from services.custom_emojis import (
    REFRESH, SUCCESS, LOADING, STATS, LINK, PARTY, WARN, ERROR
)

from bot.utils import safe_answer, html_escape, parse_callback_id, preview, edit_or_send, show_in_place

logger = logging.getLogger(__name__)
router = Router(name="history_clone_router")
# History backfill is controlled from the private chat with the bot only
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

_history_tasks: Set[asyncio.Task] = set()
_pending_start_pairs: Set[int] = set()
_pending_start_users: Set[int] = set()

# Early results of clone_history() that never reach the progress callback
_EARLY_STATUS_TEXTS: Dict[str, str] = {
    "client_not_connected": "Tizimning MTProto ulanishi hozir faol emas. Birozdan so'ng qayta urinib ko'ring.",
    "source_not_found": "Manba kanal topilmadi yoki tizim unga kira olmaydi. Kanal ochiq ekanini yoki taklif havolasi amal qilishini tekshiring.",
    "already_running": "Bu juftlik uchun tarix ko'chirish allaqachon davom etmoqda.",
}

def get_cancel_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return get_history_progress_keyboard(pair_id)

async def _backfill_cap(requester_id: int, pair: ChannelPair) -> int:
    """Largest backfill allowed for the pair: VIP owners and admins get the privileged cap."""
    if has_admin_side(requester_id, pair):
        return BACKFILL_MAX_MESSAGES_PRIVILEGED
    sub = await db_manager.get_user_subscription(pair.user_id)
    return BACKFILL_MAX_MESSAGES_PRIVILEGED if sub.is_vip else BACKFILL_MAX_MESSAGES

async def _subscription_allows_backfill(requester_id: int, pair: ChannelPair) -> bool:
    if has_admin_side(requester_id, pair):
        return True
    sub = await db_manager.get_user_subscription(pair.user_id)
    return sub.is_active

async def _user_backfill_running(user_id: int) -> bool:
    """At most one backfill per customer: True while any of the user's pairs is being backfilled."""
    running = {pid for pid, task in telethon_listener.active_history_tasks.items() if task is not None and not task.done()}
    if not running:
        return False
    return any(p.id in running for p in await db_manager.get_user_channel_pairs(user_id))

@router.callback_query(F.data.startswith("pair_history_"))
async def cb_history_menu(callback: CallbackQuery, state: FSMContext):
    pair_id = parse_callback_id(callback.data, 2)
    pair = await db_manager.get_pair_by_id(pair_id) if pair_id > 0 else None
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not can_manage_pair(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    if not pair.is_active:
        await safe_answer(callback, "Ushbu kanal juftligi to'xtatilgan! Tarixni ko'chirish uchun avval uni faollashtiring.", show_alert=True)
        return

    if not await _subscription_allows_backfill(callback.from_user.id, pair):
        await safe_answer(callback, "Tarixni ko'chirish uchun faol obuna kerak! 'Tariflar & Obuna' bo'limidan tarif tanlang.", show_alert=True)
        return

    me = await telethon_listener.get_me()
    if not me or not telethon_listener.is_connected():
        await safe_answer(callback, "MTProto tinglovchi faol emas!", show_alert=True)
        return

    await safe_answer(callback)
    cap = await _backfill_cap(callback.from_user.id, pair)
    src_title = preview(pair.source_title or 'Manba', 100)
    tgt_title = preview(pair.target_title or 'Maqsad', 100)
    text = f"""
{REFRESH} <b>Tarixiy Postlarni Ko'chirish (History Backfill)</b>

Siz <b>{src_title}</b> kanalidagi eski postlarni <b>{tgt_title}</b> kanalingizga xronologik tartibda nusxalashingiz mumkin.

<i>Qancha post ko'chirilishini tanlang (tarifingiz bo'yicha bir martada ko'pi bilan {cap} ta):</i>
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_history_count_keyboard(pair.id, cap))

@router.callback_query(F.data.startswith("hist_cancel_"))
async def cb_cancel_history_clone(callback: CallbackQuery):
    pair_id = parse_callback_id(callback.data, 2)
    pair = await db_manager.get_pair_by_id(pair_id) if pair_id > 0 else None
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not can_manage_pair(pair, callback.from_user.id):
        await safe_answer(callback, "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas.", show_alert=True)
        return

    cancelled = telethon_listener.cancel_history_clone(pair.id)
    if not cancelled:
        await safe_answer(callback, "Jarayon allaqachon tugagan yoki topilmadi.", show_alert=True)
        return

    await safe_answer(callback, "To'xtatilmoqda...")
    try:
        await edit_or_send(
            callback,
            f"{WARN} <b>Tarixni ko'chirish to'xtatildi.</b>\n\n"
            f"{STATS} Ko'chirilgan postlar saqlanib qoldi.\n\n"
            f"<i>Qaytadan boshlash uchun Tarix bo'limini oching.</i>",
            parse_mode="HTML",
            reply_markup=get_pair_detail_keyboard(pair)
        )
    except Exception as e:
        logger.debug(f"Error editing cancel message: {e}")

async def _run_backfill(pair: ChannelPair, limit: int, status_msg: Any):
    """Runs clone_history() for the pair and keeps `status_msg` up to date. Results that come back
    without any progress report (not connected, source not found...) are rendered here, so the status
    message never stays at "Tayyorlanmoqda..."."""
    cancel_kb = get_cancel_keyboard(pair.id)
    src_title = preview(pair.source_title or 'Manba', 100)
    tgt_title = preview(pair.target_title or 'Maqsad', 100)
    last_edit_time = 0.0
    progress_seen = False

    async def edit_status(text: str, reply_markup: InlineKeyboardMarkup):
        try:
            await show_in_place(status_msg, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as e:
            logger.debug(f"Error editing progress message: {e}")

    async def progress_update(current: int, total: int, status: str):
        nonlocal last_edit_time, progress_seen
        progress_seen = True
        now = time.time()
        if now - last_edit_time < 3.0 and status == "running":
            return
        last_edit_time = now

        pct = min(100, int((current / total * 100))) if total > 0 else 0
        filled = min(10, max(0, int(pct / 10)))
        bar = "▓" * filled + "░" * (10 - filled)

        if status == "running":
            await edit_status(
                f"{LOADING} <b>Postlar ko'chirilmoqda:</b>\n\n"
                f"[{bar}] {pct}%\n"
                f"{STATS} <b>Jarayon:</b> {current} / {total} ta post\n"
                f"{LINK} <b>Manba:</b> {src_title}\n"
                f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                f"<i>Jarayon davom etmoqda, iltimos kuting...</i>",
                cancel_kb
            )
        elif status == "all_cloned":
            await edit_status(
                f"{SUCCESS} <b>Barcha postlar allaqachon ko'chirilgan!</b>\n\n"
                f"{STATS} Tanlangan manba kanalida yangi ko'chirilmagan postlar topilmadi.\n\n"
                f"{LINK} <b>Manba:</b> {src_title}\n"
                f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                f"Barcha yangi postlar ham avtomatik uzatib boriladi!",
                get_pair_detail_keyboard(pair)
            )
        elif status == "completed":
            await edit_status(
                f"{PARTY} <b>Tarixni ko'chirish muvaffaqiyatli yakunlandi!</b>\n\n"
                f"[{'▓' * 10}] 100%\n"
                f"{STATS} <b>Ko'chirilgan postlar:</b> {current} ta\n"
                f"{LINK} <b>Manba:</b> {src_title}\n"
                f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                f"Barcha yangi postlar ham avtomatik uzatib boriladi!",
                get_pair_detail_keyboard(pair)
            )
        elif status == "cancelled":
            await edit_status(
                f"{WARN} <b>Tarixni ko'chirish to'xtatildi.</b>\n\n"
                f"[{bar}] {pct}%\n"
                f"{STATS} <b>Ko'chirilgan postlar:</b> {current} ta\n"
                f"{LINK} <b>Manba:</b> {src_title}\n"
                f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                f"<i>Qaytadan boshlash uchun Tarix bo'limini oching.</i>",
                get_pair_detail_keyboard(pair)
            )
        elif status == "failed":
            await edit_status(
                f"{ERROR} <b>Xatolik yuz berdi!</b>\n\n"
                f"[{bar}] {pct}%\n"
                f"{STATS} <b>Ko'chirilgan postlar:</b> {current} ta\n"
                f"{LINK} <b>Manba:</b> {src_title}\n"
                f"{LINK} <b>Maqsad:</b> {tgt_title}\n\n"
                f"<i>Qaytadan urinib ko'ring yoki qo'llab-quvvatlash bilan bog'laning.</i>",
                get_pair_detail_keyboard(pair)
            )

    try:
        result = await telethon_listener.clone_history(pair, limit=limit, progress_callback=progress_update)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"History backfill for pair #{pair.id} crashed: {e}", exc_info=True)
        result = {"status": "failed"}

    if not progress_seen:
        status = str((result or {}).get("status") or "failed")
        reason = _EARLY_STATUS_TEXTS.get(status, "Tarixni ko'chirishni boshlab bo'lmadi. Birozdan so'ng qayta urinib ko'ring.")
        await edit_status(
            f"{ERROR} <b>Tarixni ko'chirish boshlanmadi.</b>\n\n"
            f"{html_escape(reason)}\n\n"
            f"{LINK} <b>Manba:</b> {src_title}\n"
            f"{LINK} <b>Maqsad:</b> {tgt_title}",
            get_pair_detail_keyboard(pair)
        )

@router.callback_query(F.data.startswith("hist_start_"))
async def cb_start_history_clone(callback: CallbackQuery):
    match = re.fullmatch(r"hist_start_(\d+)_(\d{1,6}|all)", callback.data or "")
    if not match:
        await safe_answer(callback, "Noto'g'ri so'rov!", show_alert=True)
        return
    pair_id = int(match.group(1))
    user_id = callback.from_user.id

    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    if not can_manage_pair(pair, user_id):
        await safe_answer(callback, "Ruxsat berilmagan!", show_alert=True)
        return

    if not pair.is_active:
        await safe_answer(callback, "Kanal juftligi to'xtatilgan! Tarixni ko'chirish uchun avval uni faollashtiring.", show_alert=True)
        return

    if not await _subscription_allows_backfill(user_id, pair):
        await safe_answer(callback, "Tarixni ko'chirish uchun faol obuna kerak! 'Tariflar & Obuna' bo'limidan tarif tanlang.", show_alert=True)
        return

    # "all" (old keyboards) means the plan maximum — there is no unlimited backfill
    cap = await _backfill_cap(user_id, pair)
    limit = cap if match.group(2) == "all" else int(match.group(2))
    if not 1 <= limit <= cap:
        await safe_answer(callback, f"Bir martada ko'pi bilan {cap} ta post ko'chirish mumkin.", show_alert=True)
        return

    if not telethon_listener.is_connected():
        await safe_answer(callback, "MTProto tinglovchi faol emas! Birozdan so'ng qayta urinib ko'ring.", show_alert=True)
        return

    # Prevent double-start (checked and reserved without an await in between)
    existing = telethon_listener.active_history_tasks.get(pair_id)
    if (existing is not None and not existing.done()) or pair_id in _pending_start_pairs:
        await safe_answer(callback, "Bu juftlik uchun tarix ko'chirish allaqachon davom etmoqda!", show_alert=True)
        return
    if pair.user_id in _pending_start_users:
        await safe_answer(callback, "Boshqa kanalingiz uchun tarix ko'chirish boshlanmoqda. Iltimos, kuting.", show_alert=True)
        return
    _pending_start_pairs.add(pair_id)
    _pending_start_users.add(pair.user_id)
    try:
        if await _user_backfill_running(pair.user_id):
            await safe_answer(callback, "Boshqa kanalingiz uchun tarix ko'chirish davom etmoqda. Avval u tugashini kuting yoki uni to'xtating.", show_alert=True)
            return

        # Pairs created before destination checks existed are re-verified against their owner, so a
        # backfill can never publish into a channel the owner does not manage
        ok, _, error_code = await verify_destination_access(callback.bot, pair.target_id or pair.target_channel, pair.user_id)
        if not ok:
            await safe_answer(callback, DESTINATION_ERROR_TEXTS.get(error_code, DESTINATION_ERROR_TEXTS["not_found"]), show_alert=True)
            return

        await safe_answer(callback, "Jarayon orqa fonda boshlandi.")

        src_title_initial = preview(pair.source_title or 'Manba', 100)
        tgt_title_initial = preview(pair.target_title or 'Maqsad', 100)
        status_msg = await edit_or_send(
            callback,
            f"{LOADING} <b>Tarixni ko'chirish boshlandi...</b>\n\n{LINK} Manba: {src_title_initial}\n{LINK} Maqsad: {tgt_title_initial}\n\n<i>Tayyorlanmoqda, iltimos kuting...</i>",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard(pair_id)
        )

        task = asyncio.create_task(_run_backfill(pair, limit, status_msg))
        telethon_listener.active_history_tasks[pair_id] = task
        _history_tasks.add(task)
        task.add_done_callback(_history_tasks.discard)
    finally:
        _pending_start_pairs.discard(pair_id)
        _pending_start_users.discard(pair.user_id)
