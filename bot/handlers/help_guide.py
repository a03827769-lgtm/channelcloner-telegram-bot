import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery
from bot.keyboards.inline_buttons import get_back_to_main_keyboard
from services.custom_emojis import (
    BOOK, ROCKET, TRANSLATE, IMAGE, MONEY, STARS, WARN, NUM_1, NUM_2, NUM_3, NUM_4, NUM_5, NUM_6,
    FLAG_UZ, REFRESH, LINK, ARROW_RIGHT
)

logger = logging.getLogger(__name__)
router = Router(name="help_guide_router")

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

GUIDE_TEXT = f"""
{BOOK} <b>Telegram Kloner — To'liq Qo'llanma & Yo'riqnoma</b>

Ushbu bot Telegram kanallardan postlarni avtomatlashtirish, reklamasiz tozalash va brendingiz ostida qayta nashr qilish uchun professional vositadir.

---

<b>{NUM_1}. {ROCKET} Yangi Kanal Ulash:</b>
1. Bosh menyudan <b>"{REFRESH} Kanal Kloner"</b> {ARROW_RIGHT} <b>"{ROCKET} Yangi kanal ulash"</b> tugmasini bosing.
2. Manba kanalning username (masalan: <code>@kunuzofficial</code>) yoki xabarini uzating.
3. O'zingizning kanalingiz xabarini uzating yoki username kiriting.
4. {WARN} <i>Muhim: Botni o'z kanalingizga administrator qilib, "Xabar yuborish" huquqini bering!</i>

---

<b>{NUM_2}. {ROCKET} Test Post Yuborish:</b>
• Kanal sozlamalarida <b>"{ROCKET} Test Post Yuborish"</b> tugmasini bosing.
• Bot kanalingizga sinov xabarini yuborib, ulanish 100% to'g'ri ekanini tekshirib beradi.

---

<b>{NUM_3}. {TRANSLATE} Avto-Tarjima (Auto-Translator):</b>
• Xorijiy kanallarni o'zbek tiliga real vaqtda o'girish uchun <b>"{TRANSLATE} Tarjima"</b> menyusidan <b>UZ {FLAG_UZ}</b> ni tanlang.

---

<b>{NUM_4}. {IMAGE} Rasmlarga Suv Belgisi (Watermark):</b>
• <b>"{IMAGE} Watermark"</b> bo'limida kanalingiz nomini (masalan: <code>@mening_kanalim</code>) yozing.
• Har bir rasm burchagiga brendingiz joylashtiriladi.

---

<b>{NUM_5}. {MONEY} Referal Havolalar (Affiliate Replacer):</b>
• Begona havolalarni o'zingizning daromadli havolalaringizga almashtirish uchun qoida kiriting:
• <code>domen=shaxsiy_referal_link</code>
• <i>Misol:</i> <code>uzum.uz=https://uzum.uz/?ref=my_id</code>

---

<b>{NUM_6}. {STARS} Tariflar va Cheksiz Imkoniyatlar:</b>
• Bosh menyudagi <b>"{STARS} Tariflar & Obuna"</b> orqali Telegram Stars bilan Pro yoki VIP tarifga obuna bo'ling!
"""

from typing import Union
from aiogram.types import Message

@router.callback_query(F.data == "menu_guide")
@router.message(F.text.contains("Qo'llanma"))
async def cb_guide(event: Union[CallbackQuery, Message]):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await event.message.edit_text(
            text=GUIDE_TEXT,
            parse_mode="HTML",
            reply_markup=get_back_to_main_keyboard()
        )
    else:
        await event.answer(
            text=GUIDE_TEXT,
            parse_mode="HTML",
            reply_markup=get_back_to_main_keyboard()
        )
