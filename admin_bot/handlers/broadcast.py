import asyncio
import time
import logging
import inspect
import os
import uuid
from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from database.db_manager import db_manager
from config.settings import settings
from services.custom_emojis import BROADCAST, SUCCESS, ERROR, WARN, LOADING, ID_ERROR, INFO

from bot.utils import safe_answer

logger = logging.getLogger(__name__)
router = Router(name="admin_broadcast_router")
_broadcast_lock = asyncio.Lock()

class BroadcastStates(StatesGroup):
    waiting_for_message = State()

@router.callback_query(F.data == "admin_broadcast_prompt")
@router.message(F.text.contains("Xabar Tarqatish"))
async def cb_broadcast_prompt(event: CallbackQuery | Message, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_for_message)
    text = f"""
{BROADCAST} <b>Barcha Foydalanuvchilarga Xabar Tarqatish:</b>

Barcha bot foydalanuvchilariga yubormoqchi bo'lgan xabaringizni (matn, rasm, video yoki havola) shu yerga yuboring:
"""
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Bekor qilish",
            icon_custom_emoji_id=ID_ERROR,
            style="danger",
            callback_data="admin_main_dashboard"
        )]
    ])

    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=cancel_kb)
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=cancel_kb)

@router.message(BroadcastStates.waiting_for_message)
async def process_broadcast_message(message: Message, state: FSMContext, bot: Bot):
    msg_text = getattr(message, "text", None)
    if isinstance(msg_text, str) and msg_text.strip().lower() in ("/cancel", "bekor qilish"):
        await state.clear()
        await message.answer(f"{INFO} Xabar tarqatish bekor qilindi.", reply_markup=get_back_to_admin_keyboard())
        return

    if _broadcast_lock.locked():
        await message.answer(
            f"{WARN} <b>Boshqa xabar tarqatish jarayoni allaqachon ketmoqda!</b>\n\nIltimos, oldingi jarayon tugashini kuting.",
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )
        await state.clear()
        return

    async with _broadcast_lock:
        await _execute_broadcast(message, state, bot)

async def _execute_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = await db_manager.get_all_users(active_only=True)
    total_users = len(users)

    if total_users == 0:
        await message.answer(f"{ERROR} Bazada foydalanuvchilar topilmadi.", reply_markup=get_back_to_admin_keyboard())
        return

    status_msg = await message.answer(f"{LOADING} Xabar tarqatish boshlandi... 0/{total_users}")

    sent = 0
    failed = 0
    last_status_edit = 0.0
    client_bot = None

    temp_media_path = None
    client_media_id = None
    media_kind = None

    try:
        client_bot = Bot(token=settings.BOT_TOKEN)
    except Exception as be:
        logger.error(f"Failed to initialize client_bot for broadcast: {be}")
        await status_msg.edit_text(f"{ERROR} Xabar tarqatish botini ishga tushirishda xatolik yuz berdi.")
        return

    if message.photo:
        media_kind = "photo"
        media_item = message.photo[-1]
    elif message.video:
        media_kind = "video"
        media_item = message.video
    elif message.animation:
        media_kind = "animation"
        media_item = message.animation
    elif message.sticker:
        media_kind = "sticker"
        media_item = message.sticker
    elif message.video_note:
        media_kind = "video_note"
        media_item = message.video_note
    elif message.document:
        media_kind = "document"
        media_item = message.document
    elif message.voice:
        media_kind = "voice"
        media_item = message.voice
    elif message.audio:
        media_kind = "audio"
        media_item = message.audio

    if media_kind and media_item:
        try:
            os.makedirs("temp_media", exist_ok=True)
            temp_media_path = os.path.join("temp_media", f"broadcast_{uuid.uuid4().hex[:8]}.tmp")
            await message.bot.download(media_item, destination=temp_media_path)
        except Exception as e:
            logger.error(f"Failed to download media for broadcast: {e}")

    async def _send_to_user(target_uid: int):
        nonlocal client_media_id
        if message.text:
            return await client_bot.send_message(
                chat_id=target_uid,
                text=message.text,
                entities=message.entities,
                parse_mode=None
            )
        elif media_kind and temp_media_path and os.path.exists(temp_media_path):
            media_payload = client_media_id or FSInputFile(temp_media_path)
            if media_kind == "photo":
                res = await client_bot.send_photo(
                    chat_id=target_uid,
                    photo=media_payload,
                    caption=message.caption,
                    caption_entities=message.caption_entities,
                    reply_markup=message.reply_markup,
                    parse_mode=None
                )
                if not client_media_id and hasattr(res, "photo") and res.photo:
                    client_media_id = res.photo[-1].file_id
                return res
            elif media_kind == "video":
                res = await client_bot.send_video(
                    chat_id=target_uid,
                    video=media_payload,
                    caption=message.caption,
                    caption_entities=message.caption_entities,
                    reply_markup=message.reply_markup,
                    parse_mode=None
                )
                if not client_media_id and hasattr(res, "video") and res.video:
                    client_media_id = res.video.file_id
                return res
            elif media_kind == "animation":
                res = await client_bot.send_animation(
                    chat_id=target_uid,
                    animation=media_payload,
                    caption=message.caption,
                    caption_entities=message.caption_entities,
                    reply_markup=message.reply_markup,
                    parse_mode=None
                )
                if not client_media_id and hasattr(res, "animation") and res.animation:
                    client_media_id = res.animation.file_id
                return res
            elif media_kind == "sticker":
                res = await client_bot.send_sticker(
                    chat_id=target_uid,
                    sticker=media_payload,
                    reply_markup=message.reply_markup
                )
                if not client_media_id and hasattr(res, "sticker") and res.sticker:
                    client_media_id = res.sticker.file_id
                return res
            elif media_kind == "video_note":
                res = await client_bot.send_video_note(
                    chat_id=target_uid,
                    video_note=media_payload,
                    reply_markup=message.reply_markup
                )
                if not client_media_id and hasattr(res, "video_note") and res.video_note:
                    client_media_id = res.video_note.file_id
                return res
            elif media_kind == "document":
                res = await client_bot.send_document(
                    chat_id=target_uid,
                    document=media_payload,
                    caption=message.caption,
                    caption_entities=message.caption_entities,
                    reply_markup=message.reply_markup,
                    parse_mode=None
                )
                if not client_media_id and hasattr(res, "document") and res.document:
                    client_media_id = res.document.file_id
                return res
            elif media_kind == "voice":
                res = await client_bot.send_voice(
                    chat_id=target_uid,
                    voice=media_payload,
                    caption=message.caption,
                    caption_entities=message.caption_entities,
                    reply_markup=message.reply_markup,
                    parse_mode=None
                )
                if not client_media_id and hasattr(res, "voice") and res.voice:
                    client_media_id = res.voice.file_id
                return res
            elif media_kind == "audio":
                res = await client_bot.send_audio(
                    chat_id=target_uid,
                    audio=media_payload,
                    caption=message.caption,
                    caption_entities=message.caption_entities,
                    reply_markup=message.reply_markup,
                    parse_mode=None
                )
                if not client_media_id and hasattr(res, "audio") and res.audio:
                    client_media_id = res.audio.file_id
                return res
        elif message.caption:
            return await client_bot.send_message(
                chat_id=target_uid,
                text=message.caption,
                entities=message.caption_entities,
                reply_markup=message.reply_markup,
                parse_mode=None
            )
        elif settings.BOT_TOKEN == getattr(message.bot, 'token', None):
            return await client_bot.copy_message(
                chat_id=target_uid,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=message.reply_markup
            )
        return None

    try:
        for idx, user in enumerate(users, 1):
            try:
                res = await _send_to_user(user.user_id)
                if res:
                    sent += 1
                else:
                    failed += 1
            except TelegramRetryAfter as e:
                wait_sec = e.retry_after
                logger.warning(f"Telegram FloodWait in broadcast: {wait_sec}s for user_id={user.user_id}")
                if wait_sec > 60:
                    logger.warning(f"RetryAfter {wait_sec}s is long, pausing for 60s before retrying")
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(wait_sec)
                try:
                    res = await _send_to_user(user.user_id)
                    if res:
                        sent += 1
                    else:
                        failed += 1
                except Exception as retry_err:
                    logger.warning(f"Broadcast retry failed for user_id={user.user_id}: {retry_err}")
                    failed += 1
            except TelegramForbiddenError:
                logger.warning(f"Broadcast user {user.user_id} blocked bot. Marking as inactive.")
                try:
                    await db_manager.mark_user_blocked(user.user_id, True)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
                failed += 1
            except TelegramBadRequest as e:
                err_msg = str(e).lower()
                if "chat not found" in err_msg or "user is deactivated" in err_msg:
                    logger.warning(f"Broadcast user {user.user_id} deactivated or chat not found. Marking as inactive.")
                    try:
                        await db_manager.mark_user_blocked(user.user_id, True)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                else:
                    logger.warning(f"Broadcast bad request for user_id={user.user_id}: {e}")
                failed += 1
            except Exception as e:
                logger.warning(f"Broadcast failed for user_id={user.user_id}: {e}")
                failed += 1

            now_t = time.time()
            if (idx % 20 == 0 and now_t - last_status_edit >= 3.0) or idx == total_users:
                last_status_edit = now_t
                try:
                    await status_msg.edit_text(
                        text=f"{LOADING} Xabar tarqatilmoqda: {idx}/{total_users}\n{SUCCESS} Yuborildi: {sent} | {ERROR} Yetib bormadi: {failed}",
                        parse_mode="HTML"
                    )
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            await asyncio.sleep(0.05)
    finally:
        if temp_media_path and os.path.exists(temp_media_path):
            try:
                os.remove(temp_media_path)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        if client_bot and getattr(client_bot, "session", None):
            try:
                close_res = client_bot.session.close()
                if inspect.isawaitable(close_res):
                    await close_res
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

    await status_msg.edit_text(
        text=f"{SUCCESS} <b>Xabar tarqatish yakunlandi!</b>\n\n├ Jami foydalanuvchilar: {total_users}\n├ {SUCCESS} Muvaffaqiyatli: {sent}\n└ {ERROR} Yetib bormadi (bloklagan): {failed}",
        parse_mode="HTML",
        reply_markup=get_back_to_admin_keyboard()
    )
