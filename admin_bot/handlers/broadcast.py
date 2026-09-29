import asyncio
import logging
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity

from admin_bot.keyboards.admin_keyboards import (
    get_back_to_admin_keyboard,
    get_broadcast_confirm_keyboard,
    get_broadcast_stop_keyboard,
)
from admin_bot.permissions import ensure_super_admin
from admin_bot.public_bot import get_public_bot
from bot.utils import safe_answer
from database.db_manager import db_manager
from services.custom_emojis import BROADCAST, ERROR, ID_ERROR, INFO, LOADING, SUCCESS, WARN
from admin_bot.ui import is_cancel_text, show_screen

logger = logging.getLogger(__name__)

# Bot API limits
CAPTION_LIMIT = 1024
BOT_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024  # getFile cannot serve larger files
SEND_INTERVAL_SECONDS = 0.05                 # ~20 msg/s, below the 30 msg/s global broadcast limit
MAX_SEND_ATTEMPTS = 3
PROGRESS_EDIT_INTERVAL = 3.0

# Media kinds we can re-upload through the public bot, mapped to (Message attribute, Bot method, argument name).
_MEDIA_KINDS = {
    "photo": ("photo", "send_photo", "photo"),
    "video": ("video", "send_video", "video"),
    "animation": ("animation", "send_animation", "animation"),
    "document": ("document", "send_document", "document"),
    "audio": ("audio", "send_audio", "audio"),
    "voice": ("voice", "send_voice", "voice"),
    "video_note": ("video_note", "send_video_note", "video_note"),
    "sticker": ("sticker", "send_sticker", "sticker"),
}
_CAPTIONLESS_KINDS = {"video_note", "sticker"}

_broadcast_lock = asyncio.Lock()
_stop_requested = asyncio.Event()
_running_tasks: set = set()
_announced_media_groups: Dict[str, float] = {}


class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirm = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Bekor qilish",
            icon_custom_emoji_id=ID_ERROR,
            style="danger",
            callback_data="admin_broadcast_cancel"
        )
    ]])


async def _edit_or_answer(event: CallbackQuery | Message, text: str, reply_markup=None) -> None:
    await show_screen(event, text, reply_markup)


async def cb_broadcast_prompt(event: CallbackQuery | Message, state: FSMContext):
    if not await ensure_super_admin(event):
        return
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
    if _broadcast_lock.locked():
        await _edit_or_answer(
            event,
            f"{WARN} <b>Xabar tarqatish allaqachon davom etmoqda.</b>\n\nJarayon tugashini kuting yoki uni to'xtating.",
            reply_markup=get_back_to_admin_keyboard()
        )
        return
    await state.set_state(BroadcastStates.waiting_for_message)
    text = f"""
{BROADCAST} <b>Barcha Foydalanuvchilarga Xabar Tarqatish</b>

Yubormoqchi bo'lgan xabaringizni shu yerga yuboring: matn yoki <b>bitta</b> media (rasm, video, GIF, hujjat, audio, ovozli xabar, dumaloq video yoki stiker) izohi bilan.

{INFO} <i>Yuborishdan oldin xabar ko'rinishi va qabul qiluvchilar soni ko'rsatiladi — siz tasdiqlamaguningizcha hech kimga yuborilmaydi.</i>
"""
    await _edit_or_answer(event, text, reply_markup=_cancel_keyboard())


def _entities_to_payload(entities: Optional[List[MessageEntity]]) -> Optional[List[Dict[str, Any]]]:
    if not entities:
        return None
    return [e.model_dump(exclude_none=True, mode="json") for e in entities]


def _entities_from_payload(payload: Optional[List[Dict[str, Any]]]) -> Optional[List[MessageEntity]]:
    if not payload:
        return None
    return [MessageEntity.model_validate(item) for item in payload]


def _extract_draft(message: Message) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Builds a serializable broadcast draft from the admin's message, or returns a refusal reason."""
    if message.text:
        return {
            "kind": "text",
            "text": message.text,
            "entities": _entities_to_payload(message.entities),
        }, None

    for kind, (attr, _method, _arg) in _MEDIA_KINDS.items():
        media = getattr(message, attr, None)
        if not media:
            continue
        item = media[-1] if isinstance(media, list) else media
        file_size = getattr(item, "file_size", None) or 0
        if file_size > BOT_DOWNLOAD_LIMIT_BYTES:
            return None, (
                f"Fayl hajmi {file_size / (1024 * 1024):.1f} MB. Telegram botlari 20 MB dan katta faylni "
                "yuklab ololmaydi — kichikroq fayl yuboring."
            )
        caption = message.caption or ""
        if kind not in _CAPTIONLESS_KINDS and len(caption.encode("utf-16-le")) // 2 > CAPTION_LIMIT:
            return None, (
                f"Izoh juda uzun ({len(caption)} belgi). Bot xabarlarida izoh {CAPTION_LIMIT} belgidan oshmasligi kerak."
            )
        return {
            "kind": kind,
            "file_id": item.file_id,
            "file_name": getattr(item, "file_name", None),
            "caption": caption or None,
            "caption_entities": _entities_to_payload(message.caption_entities),
            "has_spoiler": bool(getattr(message, "has_media_spoiler", False)),
        }, None

    return None, "Bu turdagi xabarni tarqatib bo'lmaydi. Matn yoki bitta media yuboring."


async def process_broadcast_message(message: Message, state: FSMContext, bot: Bot):
    if not await ensure_super_admin(message):
        await state.clear()
        return
    if is_cancel_text(message):
        await state.clear()
        await message.answer(f"{INFO} Xabar tarqatish bekor qilindi.", reply_markup=get_back_to_admin_keyboard())
        return

    if getattr(message, "media_group_id", None):
        # An album arrives as several messages; answer only once per album.
        now = time.monotonic()
        for key, ts in list(_announced_media_groups.items()):
            if now - ts > 60:
                _announced_media_groups.pop(key, None)
        if message.media_group_id not in _announced_media_groups:
            _announced_media_groups[message.media_group_id] = now
            await message.answer(
                f"{WARN} <b>Albom (bir nechta media) tarqatilmaydi.</b>\n\nIltimos, bitta media yoki matn yuboring.",
                parse_mode="HTML",
                reply_markup=_cancel_keyboard()
            )
        return

    draft, refusal = _extract_draft(message)
    if refusal:
        await message.answer(f"{ERROR} {refusal}", parse_mode="HTML", reply_markup=_cancel_keyboard())
        return

    recipients = await db_manager.get_broadcast_recipient_ids()
    if not recipients:
        await state.clear()
        await message.answer(f"{ERROR} Bazada xabar yuborish mumkin bo'lgan foydalanuvchilar topilmadi.", reply_markup=get_back_to_admin_keyboard())
        return

    draft["source_chat_id"] = message.chat.id
    draft["source_message_id"] = message.message_id
    await state.update_data(broadcast_draft=draft)
    await state.set_state(BroadcastStates.waiting_for_confirm)
    await message.reply(
        f"{BROADCAST} <b>Yuqoridagi xabar {len(recipients)} ta foydalanuvchiga yuboriladi.</b>\n\n"
        f"Xabar ko'rinishini tekshiring va tasdiqlang:",
        parse_mode="HTML",
        reply_markup=get_broadcast_confirm_keyboard(len(recipients))
    )


async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    await _edit_or_answer(callback, f"{INFO} Xabar tarqatish bekor qilindi.", reply_markup=get_back_to_admin_keyboard())


async def cb_broadcast_stop(callback: CallbackQuery):
    if not await ensure_super_admin(callback):
        return
    if not _broadcast_lock.locked():
        await safe_answer(callback, "Faol xabar tarqatish jarayoni yo'q.", show_alert=True)
        return
    _stop_requested.set()
    await safe_answer(callback, "Xabar tarqatish to'xtatilmoqda...", show_alert=True)


async def cb_broadcast_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await ensure_super_admin(callback):
        return
    data = await state.get_data()
    draft = data.get("broadcast_draft")
    current_state = await state.get_state()
    if not draft or current_state != BroadcastStates.waiting_for_confirm.state:
        await state.clear()
        await safe_answer(callback, "Tasdiqlanadigan xabar topilmadi. Qaytadan boshlang.", show_alert=True)
        return
    if _broadcast_lock.locked():
        # The draft is kept: the admin can confirm it again once the running broadcast has finished
        await safe_answer(callback, "Boshqa xabar tarqatish jarayoni ketmoqda. Tugashini kuting va qayta tasdiqlang.", show_alert=True)
        return
    await state.clear()
    await safe_answer(callback, "Xabar tarqatish boshlandi")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not remove broadcast confirmation keyboard", exc_info=True)

    status_msg = await bot.send_message(
        chat_id=callback.from_user.id,
        text=f"{LOADING} Xabar tarqatish tayyorlanmoqda...",
        reply_markup=get_broadcast_stop_keyboard()
    )
    task = asyncio.create_task(_run_broadcast(bot, draft, status_msg))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)


async def _prepare_media_file(admin_bot: Bot, draft: Dict[str, Any], work_dir: str) -> Optional[FSInputFile]:
    """Downloads the admin's media once so the public bot can upload it (file_ids are bot-specific)."""
    file_name = draft.get("file_name") or f"{draft['kind']}"
    safe_name = "".join(ch for ch in os.path.basename(file_name) if ch.isalnum() or ch in "._- ") or draft["kind"]
    path = os.path.join(work_dir, safe_name)
    await admin_bot.download(draft["file_id"], destination=path, timeout=120)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    return FSInputFile(path, filename=draft.get("file_name") or None)


def _extract_file_id(sent: Any, kind: str) -> Optional[str]:
    media = getattr(sent, _MEDIA_KINDS[kind][0], None)
    if isinstance(media, list):
        return media[-1].file_id if media else None
    return getattr(media, "file_id", None)


async def _send_once(public_bot: Bot, same_bot: bool, draft: Dict[str, Any], chat_id: int, media_ref: Dict[str, Any]) -> Any:
    kind = draft["kind"]
    if same_bot:
        # Same token: copy_message reproduces every content type exactly without re-uploading.
        return await public_bot.copy_message(
            chat_id=chat_id,
            from_chat_id=draft["source_chat_id"],
            message_id=draft["source_message_id"]
        )
    if kind == "text":
        return await public_bot.send_message(
            chat_id=chat_id,
            text=draft["text"],
            entities=_entities_from_payload(draft.get("entities")),
            parse_mode=None
        )

    _attr, method_name, arg_name = _MEDIA_KINDS[kind]
    payload = media_ref.get("file_id") or media_ref.get("input_file")
    kwargs: Dict[str, Any] = {"chat_id": chat_id, arg_name: payload}
    if kind not in _CAPTIONLESS_KINDS:
        kwargs["caption"] = draft.get("caption")
        kwargs["caption_entities"] = _entities_from_payload(draft.get("caption_entities"))
        kwargs["parse_mode"] = None
    if draft.get("has_spoiler") and kind in ("photo", "video", "animation"):
        kwargs["has_spoiler"] = True
    sent = await getattr(public_bot, method_name)(**kwargs)
    if not media_ref.get("file_id"):
        media_ref["file_id"] = _extract_file_id(sent, kind)
    return sent


async def _deliver_to_user(public_bot: Bot, same_bot: bool, draft: Dict[str, Any], user_id: int, media_ref: Dict[str, Any]) -> str:
    """Sends the broadcast to one user. Returns "sent", "blocked" (user unreachable) or "failed"."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            await _send_once(public_bot, same_bot, draft, user_id, media_ref)
            return "sent"
        except TelegramRetryAfter as e:
            logger.warning(f"Broadcast flood control: waiting {e.retry_after}s (user {user_id}, attempt {attempt})")
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            await _mark_unreachable(user_id)
            return "blocked"
        except TelegramBadRequest as e:
            err = str(e).lower()
            if "chat not found" in err or "user is deactivated" in err:
                await _mark_unreachable(user_id)
                return "blocked"
            logger.warning(f"Broadcast bad request for user {user_id}: {e}")
            return "failed"
        except Exception as e:
            logger.warning(f"Broadcast failed for user {user_id}: {e}")
            return "failed"
    return "failed"


async def _run_broadcast(admin_bot: Bot, draft: Dict[str, Any], status_msg: Message) -> None:
    async with _broadcast_lock:
        _stop_requested.clear()
        work_dir = tempfile.mkdtemp(prefix="cloner_broadcast_")
        sent = failed = blocked = 0
        stopped = False
        total = 0
        try:
            public_bot = get_public_bot(admin_bot)
            if public_bot is None:
                await status_msg.edit_text(f"{ERROR} BOT_TOKEN sozlanmagan — xabar tarqatib bo'lmaydi.", reply_markup=get_back_to_admin_keyboard())
                return
            same_bot = getattr(public_bot, "token", None) == getattr(admin_bot, "token", None)

            media_ref: Dict[str, Any] = {}
            if draft["kind"] != "text" and not same_bot:
                try:
                    media_ref["input_file"] = await _prepare_media_file(admin_bot, draft, work_dir)
                except Exception as e:
                    logger.error(f"Broadcast media download failed: {e}", exc_info=True)
                    media_ref["input_file"] = None
                if media_ref["input_file"] is None:
                    await status_msg.edit_text(
                        f"{ERROR} Media faylni yuklab bo'lmadi. Xabar tarqatish boshlanmadi.",
                        reply_markup=get_back_to_admin_keyboard()
                    )
                    return

            recipients = await db_manager.get_broadcast_recipient_ids()
            total = len(recipients)
            last_edit = 0.0
            for idx, user_id in enumerate(recipients, 1):
                if _stop_requested.is_set():
                    stopped = True
                    break
                outcome = await _deliver_to_user(public_bot, same_bot, draft, user_id, media_ref)
                if outcome == "sent":
                    sent += 1
                elif outcome == "blocked":
                    blocked += 1
                else:
                    failed += 1
                now_t = time.monotonic()
                if now_t - last_edit >= PROGRESS_EDIT_INTERVAL or idx == total:
                    last_edit = now_t
                    try:
                        await status_msg.edit_text(
                            text=f"{LOADING} Xabar tarqatilmoqda: {idx}/{total}\n"
                                 f"{SUCCESS} Yetkazildi: {sent} | {WARN} Bloklagan: {blocked} | {ERROR} Xato: {failed}",
                            parse_mode="HTML",
                            reply_markup=get_broadcast_stop_keyboard()
                        )
                    except Exception:
                        logger.debug("Broadcast progress edit skipped", exc_info=True)
                await asyncio.sleep(SEND_INTERVAL_SECONDS)
        except Exception as e:
            logger.error(f"Broadcast aborted: {e}", exc_info=True)
            try:
                await status_msg.edit_text(f"{ERROR} Xabar tarqatish xatolik bilan to'xtadi. Tafsilotlar logda.", reply_markup=get_back_to_admin_keyboard())
            except Exception:
                logger.debug("Broadcast error report failed", exc_info=True)
            return
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            _stop_requested.clear()

        title = f"{WARN} <b>Xabar tarqatish to'xtatildi!</b>" if stopped else f"{SUCCESS} <b>Xabar tarqatish yakunlandi!</b>"
        try:
            await status_msg.edit_text(
                text=f"{title}\n\n"
                     f"├ Jami qabul qiluvchilar: {total}\n"
                     f"├ {SUCCESS} Yetkazildi: {sent}\n"
                     f"├ {WARN} Botni bloklagan / o'chirilgan: {blocked}\n"
                     f"└ {ERROR} Boshqa xatolar: {failed}",
                parse_mode="HTML",
                reply_markup=get_back_to_admin_keyboard()
            )
        except Exception:
            logger.debug("Broadcast summary edit failed", exc_info=True)


async def _mark_unreachable(user_id: int) -> None:
    try:
        await db_manager.mark_user_blocked(user_id, True)
    except Exception:
        logger.debug("Could not mark user as blocked", exc_info=True)
