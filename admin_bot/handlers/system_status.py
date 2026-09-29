import os
import json
import html
import logging
import platform
import asyncio
import tempfile
from typing import List
from aiogram.types import CallbackQuery, Message
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from admin_bot.permissions import ensure_super_admin
from database.db_manager import db_manager
from services.telethon_listener import telethon_listener
from services.custom_emojis import (
    INFO, SERVICE_24_7, TELEGRAM, SUCCESS, WARN, USERS_GROUP, LINK,
    ROCKET, STARS, SETTINGS, DOCUMENT, SHIELD
)

from bot.utils import safe_answer
from admin_bot.ui import show_screen

logger = logging.getLogger(__name__)

ANTI_RECLAIM_STATUS_FILE = "oracle_anti_reclaim_status.json"


def _read_anti_reclaim_status() -> str | None:
    """Reads the telemetry written by deploy/anti_reclaim.py. Returns None when the helper is not running
    (e.g. on Windows or PaaS hosts), so the status screen never claims a protection that is not active."""
    candidates = [os.path.join(tempfile.gettempdir(), ANTI_RECLAIM_STATUS_FILE)]
    if os.name != "nt":
        candidates.append(os.path.join("/tmp", ANTI_RECLAIM_STATUS_FILE))
    for status_file in candidates:
        if not os.path.isfile(status_file):
            continue
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            ram_pct = float(data.get("ram_percent", 0))
            cpu_pct = float(data.get("target_cpu_percent", 0))
            heartbeats = int(data.get("network_heartbeats", 0))
            return f"{SUCCESS} Faol (RAM: {ram_pct:.0f}%, CPU: {cpu_pct:.0f}%, Heartbeat: {heartbeats})"
        except Exception:
            logger.debug("Anti-reclaim status file unreadable", exc_info=True)
    return None


async def _edit_or_answer(event: CallbackQuery | Message, text: str) -> None:
    await show_screen(event, text, get_back_to_admin_keyboard())


async def cb_admin_system_status(event: CallbackQuery | Message):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)

    me = await telethon_listener.get_me()
    stats = await db_manager.get_stats()

    mtproto_status = f"{SUCCESS} Faol ({html.escape(me.first_name or '')})" if me else f"{WARN} Ulanmagan"

    anti_reclaim_text = await asyncio.to_thread(_read_anti_reclaim_status)
    anti_reclaim_line = f"├ {SHIELD} <b>Oracle Anti-Reclaim:</b> {anti_reclaim_text}\n" if anti_reclaim_text else ""

    os_name = html.escape(f"{platform.system()} {platform.release()}")
    python_ver = platform.python_version()

    supplier_cfg = await db_manager.get_supplier_config()
    supplier_bal_text = f"${supplier_cfg.balance:.2f} {html.escape(supplier_cfg.currency or '')}" if supplier_cfg else "—"
    store_prods = await db_manager.get_store_products()
    in_stock_count = sum(1 for p in store_prods if p.stock_status == "in_stock")
    out_of_stock_count = len(store_prods) - in_stock_count

    text = f"""
{INFO} <b>TIZIM HOLATI VA SERVER MONITORINGI:</b>
───────────────────────────
├ {SERVICE_24_7} <b>Bot API:</b> {SUCCESS} Faol
├ {TELEGRAM} <b>MTProto Tinglovchi:</b> {mtproto_status}
├ 🛒 <b>Ta'minotchi Balansi:</b> <code>{supplier_bal_text}</code>
├ 📦 <b>Do'kon Mahsulotlari:</b> <code>{len(store_prods)}</code> ta ({in_stock_count} faol, {out_of_stock_count} tugagan)
{anti_reclaim_line}├ {SETTINGS} <b>Operatsion Tizim:</b> <code>{os_name}</code>
├ {DOCUMENT} <b>Python Versiyasi:</b> <code>{python_ver}</code>
├ {USERS_GROUP} <b>Jami Foydalanuvchilar:</b> <code>{stats['total_users']}</code> nafar
├ {LINK} <b>Jami Ulangan Kanallar:</b> <code>{stats['total_pairs']}</code> ta
├ {SUCCESS} <b>Faol Ishlayotgan Kanallar:</b> <code>{stats['active_pairs']}</code> ta
├ {ROCKET} <b>Ko'chirilgan Postlar:</b> <code>{stats['total_cloned_messages']}</code> ta
└ {STARS} <b>Jami Stars Tushumi:</b> <code>{stats['total_stars_earned']}</code> Stars
───────────────────────────
"""
    await _edit_or_answer(event, text)


# Visible characters of the log tail shown in one message (Telegram allows 4096 after entity parsing)
LOG_TAIL_MAX_CHARS = 3000


def format_log_tail(lines: List[str], max_chars: int = LOG_TAIL_MAX_CHARS) -> str:
    """HTML-escaped tail of the log lines: at most `max_chars` characters, starting at a line boundary,
    escaped after cutting (so an entity such as &amp; is never cut in half)."""
    raw_text = "\n".join(lines)
    if len(raw_text) > max_chars:
        raw_text = raw_text[-max_chars:]
        if "\n" in raw_text:
            raw_text = raw_text.split("\n", 1)[1]
    return html.escape(raw_text)


async def cb_admin_view_logs(event: CallbackQuery | Message):
    # Logs can contain user identifiers and operational details — super admins only.
    if not await ensure_super_admin(event):
        return
    from services.log_viewer import log_viewer

    if isinstance(event, CallbackQuery):
        await safe_answer(event)

    logs = log_viewer.get_recent_logs(count=30)
    log_text = format_log_tail(logs) if logs else "Hozircha xotirada loglar mavjud emas."

    await _edit_or_answer(event, f"{DOCUMENT} <b>Jonli Tizim Loglari (So'nggi 30 ta qator):</b>\n\n<pre>{log_text}</pre>")


async def cmd_check_origin(message: Message):
    """Admin command to inspect and extract invisible steganography watermark from a photo"""
    from services.steganography_service import steganography_service

    target_msg = message.reply_to_message if message.reply_to_message else message
    if not target_msg.photo:
        await message.answer(
            f"{INFO} <b>Rasm Aslligi Tekshiruvi (Steganografiya):</b>\n\n"
            f"Ushbu buyruqni rasmga reply (javob) qilib yuboring yoki rasm izohiga <code>/check_origin</code> deb yozing.",
            parse_mode="HTML"
        )
        return

    photo = target_msg.photo[-1]
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await message.bot.download(photo, destination=tmp_path)
        payload = await asyncio.to_thread(steganography_service.extract_watermark, tmp_path)
        if payload:
            await message.answer(
                f"{SHIELD} <b>Rasmda ko'rinmas mualliflik belgisi topildi!</b>\n\n"
                f"├ <b>Ko'rinmas belgi:</b> <code>{html.escape(payload)}</code>\n"
                f"└ <i>Ushbu rasm bot tizimi orqali belgilangan.</i>",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                f"{WARN} <b>Ko'rinmas suv belgisi topilmadi.</b>\n"
                f"Rasm tizim orqali belgilanmagan yoki qayta siqilgan bo'lishi mumkin.",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.warning(f"check_origin failed: {e}")
        await message.answer(f"{WARN} Tekshirishda xatolik: <code>{html.escape(str(e)[:300])}</code>", parse_mode="HTML")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            logger.debug("Temp file cleanup failed", exc_info=True)
