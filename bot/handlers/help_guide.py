import logging
from typing import Union
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from bot.keyboards.inline_buttons import get_back_to_main_keyboard
from services.custom_emojis import (
    BOOK, ROCKET, TRANSLATE, IMAGE, MONEY, STARS, WARN, NUM_1, NUM_2, NUM_3, NUM_4, NUM_5, NUM_6,
    FLAG_UZ, REFRESH, LINK, ARROW_RIGHT, VIDEO
)

from bot.utils import safe_answer

logger = logging.getLogger(__name__)
router = Router(name="help_guide_router")

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

<b>{NUM_6}. Video Watermark & Logo:</b>
• Videolarga matnli yoki logotip ko'rinishidagi suv belgisi qo'yish. FFmpeg dvigateli orqali videolaringiz burchagiga brendingiz avtomatik muhrlanadi.

---

<b>7. AI Paraphraser (Postlarni Qayta Yozish):</b>
• Xabarlarni o'ziga xos sarlavha va uslub bilan qayta tahrirlash. Postlar originalligini oshirish uchun qisqacha yoki to'liq rejimni tanlang.

---

<b>8. Drip Feed & Tungi Rejim:</b>
• Xabarlarni belgilangan daqiqali interval bilan navbat orqali jo'natish. Tungi vaqtda obunachilaringizni bezovta qilmaslik uchun "Silent" yoki "Buffer" rejimini yoqing.

---

<b>9. Zaxira & Tiklash (Disaster Recovery):</b>
• Kanalingizdagi barcha postlar bazada avtomatik arxivlanadi. Agar kanalingiz bloklansa yoki o'chib ketsa, birgina tugma orqali barcha arxivni yangi kanalga 100% tiklab olasiz.

---

<b>10. {STARS} Tariflar va Cheksiz Imkoniyatlar:</b>
• Bosh menyudagi <b>"{STARS} Tariflar & Obuna"</b> orqali Telegram Stars bilan Pro yoki VIP tarifga obuna bo'ling!

---

<b>11. {VIDEO} Real Estate Auto-Story Cloner (Faqat VIP Cheksiz):</b>
• Toshkent ko'chmas mulk kanallaridagi eng sara $700+ e'lonlarni avtomatik aniqlash.
• Playwright 4K kollaj, Ken Burns effekti, relaks musiqa va interaktiv post havolasi bilan 25 soniyalik hashamatli video Istoriya yaratish.
• Prime Time (09:00 - 22:00) navbati va 14 kunlik takrorlanishdan xotira himoyasi.
• <i>Eslatma: Ushbu funksiya faqat VIP Cheksiz obunachilar uchun faol!</i>
"""

GUIDE_SEC_1 = f"""
{BOOK} <b>Telegram Kloner — 1-Bo'lim: Boshlash & Asosiy Sozlamalar</b>

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
"""

GUIDE_SEC_2 = f"""
{BOOK} <b>Telegram Kloner — 2-Bo'lim: Media, Brending & Referal</b>

<b>{NUM_4}. {IMAGE} Rasmlarga Suv Belgisi (Watermark):</b>
• <b>"{IMAGE} Watermark"</b> bo'limida kanalingiz nomini (masalan: <code>@mening_kanalim</code>) yozing.
• Har bir rasm burchagiga brendingiz joylashtiriladi.

---

<b>{NUM_5}. {MONEY} Referal Havolalar (Affiliate Replacer):</b>
• Begona havolalarni o'zingizning daromadli havolalaringizga almashtirish uchun qoida kiriting:
• <code>domen=shaxsiy_referal_link</code>
• <i>Misol:</i> <code>uzum.uz=https://uzum.uz/?ref=my_id</code>

---

<b>{NUM_6}. Video Watermark & Logo:</b>
• Videolarga matnli yoki logotip ko'rinishidagi suv belgisi qo'yish. FFmpeg dvigateli orqali videolaringiz burchagiga brendingiz avtomatik muhrlanadi.
"""

GUIDE_SEC_3 = f"""
{BOOK} <b>Telegram Kloner — 3-Bo'lim: Pro & VIP Imkoniyatlar</b>

<b>7. AI Paraphraser (Postlarni Qayta Yozish):</b>
• Xabarlarni o'ziga xos sarlavha va uslub bilan qayta tahrirlash. Postlar originalligini oshirish uchun qisqacha yoki to'liq rejimni tanlang.

---

<b>8. Drip Feed & Tungi Rejim:</b>
• Xabarlarni belgilangan daqiqali interval bilan navbat orqali jo'natish. Tungi vaqtda obunachilaringizni bezovta qilmaslik uchun "Silent" yoki "Buffer" rejimini yoqing.

---

<b>9. Zaxira & Tiklash (Disaster Recovery):</b>
• Kanalingizdagi barcha postlar bazada avtomatik arxivlanadi. Agar kanalingiz bloklansa yoki o'chib ketsa, birgina tugma orqali barcha arxivni yangi kanalga 100% tiklab olasiz.

---

<b>10. {STARS} Tariflar va Cheksiz Imkoniyatlar:</b>
• Bosh menyudagi <b>"{STARS} Tariflar & Obuna"</b> orqali Telegram Stars bilan Pro yoki VIP tarifga obuna bo'ling!

---

<b>11. {VIDEO} Real Estate Auto-Story Cloner (Faqat VIP Cheksiz):</b>
• Toshkent ko'chmas mulk kanallaridagi eng sara $700+ e'lonlarni avtomatik aniqlash.
• Playwright 4K kollaj, Ken Burns effekti, relaks musiqa va interaktiv post havolasi bilan 25 soniyalik hashamatli video Istoriya yaratish.
• Prime Time (09:00 - 22:00) navbati va 14 kunlik takrorlanishdan xotira himoyasi.
• <i>Eslatma: Ushbu funksiya faqat VIP Cheksiz obunachilar uchun faol!</i>
"""

from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from services.custom_emojis import ID_HOME, ID_ROCKET, ID_PALETTE, ID_FLASH, ID_DOCUMENT


def get_guide_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Boshlash (1-3)", callback_data="guide_sec_1", style="primary", icon_custom_emoji_id=ID_ROCKET),
            InlineKeyboardButton(text="Media & Brend (4-6)", callback_data="guide_sec_2", style="primary", icon_custom_emoji_id=ID_PALETTE),
        ],
        [
            InlineKeyboardButton(text="Pro & VIP (7-11)", callback_data="guide_sec_3", style="success", icon_custom_emoji_id=ID_FLASH),
            InlineKeyboardButton(text="Barcha Qo'llanmalar", callback_data="guide_sec_all", style="primary", icon_custom_emoji_id=ID_DOCUMENT),
        ],
        [
            InlineKeyboardButton(text="Asosiy Menyu", callback_data="menu_main", style="danger", icon_custom_emoji_id=ID_HOME)
        ]
    ])


@router.message(Command("help"))
@router.callback_query(F.data == "menu_guide")
@router.message(F.text.regexp(r"Qo['ʻ’ʼ`]llanma"))
async def cb_guide(event: Union[CallbackQuery, Message]):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        try:
            await event.message.edit_text(
                text=GUIDE_TEXT,
                parse_mode="HTML",
                reply_markup=get_guide_keyboard()
            )
        except TelegramBadRequest:
            pass
    else:
        await event.answer(
            text=GUIDE_TEXT,
            parse_mode="HTML",
            reply_markup=get_guide_keyboard()
        )


@router.callback_query(F.data.startswith("guide_sec_"))
async def cb_guide_section(callback: CallbackQuery):
    await safe_answer(callback)
    sec = callback.data.replace("guide_sec_", "")
    text = GUIDE_TEXT
    if sec == "1":
        text = GUIDE_SEC_1
    elif sec == "2":
        text = GUIDE_SEC_2
    elif sec == "3":
        text = GUIDE_SEC_3
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_guide_keyboard())
    except TelegramBadRequest:
        pass
