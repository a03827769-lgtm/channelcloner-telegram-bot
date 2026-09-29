import logging
from typing import Optional, Union
from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from bot.keyboards.inline_buttons import MENU_GUIDE_VARIANTS
from config.limits import STORY_VIDEO_DURATION_MAX, STORY_VIDEO_DURATION_MIN
from config.plans import PLANS, TRIAL_DAYS
from services.custom_emojis import (
    BOOK, ROCKET, TRANSLATE, IMAGE, MONEY, STARS, WARN, NUM_1, NUM_2, NUM_3, NUM_4, NUM_5, NUM_6,
    FLAG_UZ, REFRESH, ARROW_RIGHT, VIDEO,
    ID_HOME, ID_ROCKET, ID_PALETTE, ID_FLASH, ID_DOCUMENT
)

from bot.utils import safe_answer, edit_or_send

logger = logging.getLogger(__name__)
router = Router(name="help_guide_router")
# The guide is shown in the private chat with the bot only
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

# Each section body is written once; the "all sections" page and the per-section pages are built from them
_SECTION_1_BODY = f"""
<b>{NUM_1}. {ROCKET} Yangi Kanal Ulash:</b>
1. Bosh menyudan <b>"{REFRESH} Kanal Kloner"</b> {ARROW_RIGHT} <b>"{ROCKET} Yangi kanal ulash"</b> tugmasini bosing.
2. Manba kanalning username'ini (masalan: <code>@kunuzofficial</code>) yozing yoki undan xabar uzating.
3. O'zingizning kanalingiz username'ini yozing yoki undan xabar uzating.
4. {WARN} <i>Muhim: siz kanal egasi yoki post joylash huquqiga ega administratori bo'lishingiz, botni esa kanalga administrator qilib, "Xabar yuborish" huquqini berishingiz kerak.</i>

---

<b>{NUM_2}. {ROCKET} Test Post Yuborish:</b>
• Kanal sozlamalarida <b>"{ROCKET} Test Post Yuborish"</b> tugmasini bosing.
• Bot kanalingizga sinov xabarini yuborib, ulanish to'g'ri ekanini tekshirib beradi.

---

<b>{NUM_3}. {TRANSLATE} Avto-Tarjima (Auto-Translator):</b>
• Xorijiy kanallarni o'zbek tiliga real vaqtda o'girish uchun <b>"{TRANSLATE} Tarjima"</b> menyusidan <b>UZ {FLAG_UZ}</b> ni tanlang.
"""

_SECTION_2_BODY = f"""
<b>{NUM_4}. {IMAGE} Rasmlarga Suv Belgisi (Watermark) — PRO/VIP:</b>
• <b>"{IMAGE} Watermark"</b> bo'limida kanalingiz nomini (masalan: <code>@mening_kanalim</code>) yozing.
• Har bir rasm burchagiga brendingiz joylashtiriladi.

---

<b>{NUM_5}. {MONEY} Referal Havolalar (Affiliate Replacer) — PRO/VIP:</b>
• Begona havolalarni o'zingizning daromadli havolalaringizga almashtirish uchun qoida kiriting:
• <code>domen=shaxsiy_referal_link</code>
• <i>Misol:</i> <code>uzum.uz=https://uzum.uz/?ref=my_id</code>

---

<b>{NUM_6}. Video Watermark — PRO/VIP:</b>
• Videolarga matnli suv belgisi qo'yish: FFmpeg dvigateli orqali brendingiz video burchagiga avtomatik muhrlanadi.
"""

_SECTION_3_BODY = f"""
<b>7. AI Paraphraser (Postlarni Qayta Yozish) — PRO/VIP:</b>
• Xabarlarni rasmiy, qaynoq (hype) yoki qisqa tezis uslubida qayta yozish.

---

<b>8. Drip Feed & Tungi Rejim — PRO/VIP:</b>
• Xabarlarni belgilangan daqiqali interval bilan navbat orqali jo'natish. Tunda obunachilaringizni bezovta qilmaslik uchun "Silent" yoki "Buffer" rejimini yoqing.

---

<b>9. Zaxira & Tiklash (Disaster Recovery):</b>
• Kanalingizga ko'chirilgan postlar bazada arxivlanadi. Kanalingiz bloklansa yoki o'chib ketsa, arxivni o'zingiz boshqaradigan yangi kanalga tugma orqali tiklab olasiz (faol obuna kerak).

---

<b>10. {STARS} Tariflar:</b>
• Yangi foydalanuvchilarga {TRIAL_DAYS} kunlik bepul sinov (1 ta kanal juftligi).
• <b>Pro</b> — {PLANS['pro']['stars']} Stars / {PLANS['pro']['days']} kun, <b>VIP Cheksiz</b> — {PLANS['vip']['stars']} Stars / {PLANS['vip']['days']} kun.
• Bosh menyudagi <b>"{STARS} Tariflar & Obuna"</b> bo'limidan Telegram Stars bilan to'lang.

---

<b>11. {VIDEO} Real Estate Auto-Story Cloner — faqat VIP:</b>
• Ko'chmas mulk kanallaridagi belgilangan narxdan (standart $700) qimmat e'lonlarni avtomatik aniqlash.
• Kollaj, Ken Burns effekti, musiqa va post havolasi bilan {STORY_VIDEO_DURATION_MIN}–{STORY_VIDEO_DURATION_MAX} soniyalik video Istoriya yaratish.
• Prime Time navbati va 14 kunlik takrorlanishdan himoya.
• Premium animatsion emojilar va himoyalangan (yopiq) kanallarni ko'chirish ham VIP tarifiga kiradi.
"""

GUIDE_TEXT = (
    f"\n{BOOK} <b>Telegram Kloner — To'liq Qo'llanma & Yo'riqnoma</b>\n\n"
    "Ushbu bot Telegram kanallardan postlarni avtomatlashtirish, reklamasiz tozalash va brendingiz ostida "
    "qayta nashr qilish uchun professional vositadir.\n\n---\n"
    + _SECTION_1_BODY + "\n---\n" + _SECTION_2_BODY + "\n---\n" + _SECTION_3_BODY
)
GUIDE_SEC_1 = f"\n{BOOK} <b>Telegram Kloner — 1-Bo'lim: Boshlash & Asosiy Sozlamalar</b>\n" + _SECTION_1_BODY
GUIDE_SEC_2 = f"\n{BOOK} <b>Telegram Kloner — 2-Bo'lim: Media, Brending & Referal</b>\n" + _SECTION_2_BODY
GUIDE_SEC_3 = f"\n{BOOK} <b>Telegram Kloner — 3-Bo'lim: Pro & VIP Imkoniyatlar</b>\n" + _SECTION_3_BODY


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


GUIDE_SECTIONS = {"1": GUIDE_SEC_1, "2": GUIDE_SEC_2, "3": GUIDE_SEC_3, "all": GUIDE_TEXT}


@router.message(Command("help"))
@router.callback_query(F.data == "menu_guide")
@router.message(F.text.in_(MENU_GUIDE_VARIANTS), ~F.forward_origin)
async def cb_guide(event: Union[CallbackQuery, Message], state: Optional[FSMContext] = None):
    # Opening a menu ends any unfinished wizard
    if state is not None:
        await state.clear()
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await edit_or_send(event, GUIDE_TEXT, parse_mode="HTML", reply_markup=get_guide_keyboard())
    else:
        await event.answer(
            text=GUIDE_TEXT,
            parse_mode="HTML",
            reply_markup=get_guide_keyboard()
        )


@router.callback_query(F.data.startswith("guide_sec_"))
async def cb_guide_section(callback: CallbackQuery):
    await safe_answer(callback)
    text = GUIDE_SECTIONS.get(callback.data.replace("guide_sec_", ""), GUIDE_TEXT)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_guide_keyboard())
