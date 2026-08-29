import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from database.db_manager import db_manager
from config.settings import settings
from services.custom_emojis import BROADCAST, SUCCESS, ERROR, LOADING, ID_ERROR

logger = logging.getLogger(__name__)
router = Router(name="admin_broadcast_router")

class BroadcastStates(StatesGroup):
    waiting_for_message = State()

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

@router.callback_query(F.data == "admin_broadcast_prompt")
@router.message(F.text.contains("Xabar Tarqatish"))
async def cb_broadcast_prompt(event: CallbackQuery | Message, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_for_message)
    text = f"""
{BROADCAST} <b>Barcha Foydalanuvchilarga Xabar Tarqatish:</b>

Barcha bot foydalanuvchilariga yubormoqchi bo'lgan xabaringizni (matn, rasm, video yoki havola) shu yerga yuboring:
"""
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Bekor qilish", callback_data="admin_main_dashboard", style="danger", icon_custom_emoji_id=ID_ERROR)]
    ])

    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=cancel_kb)
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=cancel_kb)

@router.message(BroadcastStates.waiting_for_message)
async def process_broadcast_message(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = await db_manager.get_all_users()
    total_users = len(users)

    if total_users == 0:
        await message.answer(f"{ERROR} Bazada foydalanuvchilar topilmadi.", reply_markup=get_back_to_admin_keyboard())
        return

    status_msg = await message.answer(f"{LOADING} Xabar tarqatish boshlandi... 0/{total_users}")

    sent = 0
    failed = 0
    client_bot = Bot(token=settings.BOT_TOKEN)

    try:
        for idx, user in enumerate(users, 1):
            try:
                if message.text:
                    await client_bot.send_message(
                        chat_id=user.user_id,
                        text=message.text,
                        entities=message.entities,
                        parse_mode=None
                    )
                elif message.photo:
                    await client_bot.send_photo(
                        chat_id=user.user_id,
                        photo=message.photo[-1].file_id,
                        caption=message.caption,
                        caption_entities=message.caption_entities,
                        parse_mode=None
                    )
                elif message.video:
                    await client_bot.send_video(
                        chat_id=user.user_id,
                        video=message.video.file_id,
                        caption=message.caption,
                        caption_entities=message.caption_entities,
                        parse_mode=None
                    )
                elif message.document:
                    await client_bot.send_document(
                        chat_id=user.user_id,
                        document=message.document.file_id,
                        caption=message.caption,
                        caption_entities=message.caption_entities,
                        parse_mode=None
                    )
                elif message.voice:
                    await client_bot.send_voice(
                        chat_id=user.user_id,
                        voice=message.voice.file_id,
                        caption=message.caption,
                        caption_entities=message.caption_entities,
                        parse_mode=None
                    )
                elif message.audio:
                    await client_bot.send_audio(
                        chat_id=user.user_id,
                        audio=message.audio.file_id,
                        caption=message.caption,
                        caption_entities=message.caption_entities,
                        parse_mode=None
                    )
                else:
                    await message.copy_to(chat_id=user.user_id)
                sent += 1
            except Exception as e:
                logger.warning(f"Broadcast failed for user_id={user.user_id}: {e}")
                failed += 1

            if idx % 20 == 0 or idx == total_users:
                try:
                    await status_msg.edit_text(
                        text=f"{LOADING} Xabar tarqatilmoqda: {idx}/{total_users}\n{SUCCESS} Yuborildi: {sent} | {ERROR} Yetib bormadi: {failed}",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.05)
    finally:
        await client_bot.session.close()

    await status_msg.edit_text(
        text=f"{SUCCESS} <b>Xabar tarqatish yakunlandi!</b>\n\n├ Jami foydalanuvchilar: {total_users}\n├ {SUCCESS} Muvaffaqiyatli: {sent}\n└ {ERROR} Yetib bormadi (bloklagan): {failed}",
        parse_mode="HTML",
        reply_markup=get_back_to_admin_keyboard()
    )
