import asyncio
import logging
import os
import re
from typing import List, Optional, Union, Tuple, Dict, Any
from aiogram import Bot
from aiogram.types import (
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    Message as AiogramMessage
)
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError, TelegramBadRequest
from telethon.tl.types import Message as TelethonMessage
from telethon.extensions import html as telethon_html
from database.models import ChannelPair
from database.db_manager import db_manager
from services.text_processor import TextProcessor
from services.media_handler import media_handler
from services.translator_service import translator_service
from services.watermark_service import watermark_service
from services.video_watermark_service import video_watermark_service
from services.ai_paraphraser import ai_paraphraser
from services.dynamic_affiliate_engine import dynamic_affiliate_engine
from services.drip_feed_queue import drip_feed_service
from services.disaster_recovery import disaster_recovery_service
from services.affiliate_replacer import affiliate_replacer
from services.rate_limiter import rate_limiter
from services.cache_manager import cache_manager
from services.emoji_converter import emoji_converter
from config.settings import settings
from services.custom_emojis import (
    ROCKET, SUCCESS, ERROR, WARN, DOCUMENT, LINK, CLEAN, TRANSLATE, IMAGE, PARTY, STAR_SPARKLE
)

logger = logging.getLogger(__name__)

def extract_message_html(message: TelethonMessage) -> str:
    """Extracts rich-formatted HTML from TelethonMessage, preserving bold, italic, spoilers, and custom emojis"""
    if not message:
        return ""
    if hasattr(message, 'entities') and message.entities and hasattr(message, 'message') and message.message:
        try:
            return telethon_html.unparse(message.message, message.entities)
        except Exception:
            pass
    return getattr(message, 'text', '') or getattr(message, 'message', '') or ""

class ClonerEngine:
    def __init__(self, bot: Optional[Bot] = None):
        self.bot = bot

    def set_bot(self, bot: Bot):
        self.bot = bot

    async def send_test_post(self, pair: ChannelPair) -> Tuple[bool, str]:
        """Sends a verification test message to the target channel with animated emojis"""
        if not self.bot:
            return False, "Bot ishga tushmagan!"

        target_chat_id = self._normalize_chat_id(pair.target_channel)
        
        clean_status = f"{SUCCESS} Yoqilgan" if pair.clean_links else f"{ERROR} O'chirilgan"
        trans_status = f"{SUCCESS} {pair.target_lang.upper()}" if pair.auto_translate else f"{ERROR} O'chirilgan"
        wm_status = pair.image_watermark_text if pair.image_watermark_text else (f"{SUCCESS} Yoqilgan" if pair.image_watermark_type != "none" else f"{ERROR} O'chirilgan")
        emoji_status = f"{SUCCESS} Yoqilgan (VIP)" if pair.auto_premium_emojis else f"{ERROR} O'chirilgan"

        sample_text = f"""
{ROCKET} <b>Telegram Kloner — Test Xabari!</b>

Kanalingiz botga muvaffaqiyatli ulandi va sozlamalar tekshirildi.

{DOCUMENT} <b>Juftlik ma'lumotlari:</b>
├ {LINK} <b>Manba kanal:</b> {pair.source_title} (<code>{pair.source_channel}</code>)
├ {LINK} <b>Maqsadli kanal:</b> {pair.target_title} (<code>{pair.target_channel}</code>)
├ {CLEAN} <b>Reklama tozalash:</b> {clean_status}
├ {TRANSLATE} <b>Avto-Tarjima:</b> {trans_status}
├ {IMAGE} <b>Suv belgisi (Watermark):</b> {wm_status}
└ {STAR_SPARKLE} <b>Telegram Premium Emojilar:</b> {emoji_status}

<i>Endi manba kanaldagi yangi xabarlar to'g'ridan-to'g'ri shu yerga nusxalanadi!</i>
"""
        if pair.custom_signature and not pair.remove_signature:
            sample_text = TextProcessor.attach_signature(sample_text, pair.custom_signature)

        try:
            sent = await self.bot.send_message(
                chat_id=target_chat_id,
                text=sample_text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            msg_id = getattr(sent, 'message_id', None) or getattr(sent, 'id', 'OK')
            return True, f"Test xabari {pair.target_title} kanaliga muvaffaqiyatli yuborildi! (ID: {msg_id})"
        except TelegramBadRequest as e:
            return False, f"Xatolik: Bot kanalda administrator emas yoki ruxsat yetarli emas! ({e})"
        except Exception as e:
            return False, f"Xatolik: {e}"

    async def process_post_text(self, raw_text: str, pair: ChannelPair) -> Optional[str]:
        if raw_text is None:
            raw_text = ""

        if pair.blacklist_list and TextProcessor.contains_blacklisted_words(raw_text, pair.blacklist_list):
            return None

        current_text = raw_text

        if pair.clean_links and current_text.strip():
            current_text = TextProcessor.clean_links_and_usernames(current_text)

        if pair.auto_translate and current_text.strip():
            try:
                async with cache_manager.translate_semaphore:
                    current_text = await translator_service.translate_text(
                        current_text,
                        target_lang=pair.target_lang or "uz",
                        source_lang=pair.source_lang or "auto"
                    )
            except Exception as e:
                logger.error(f"Auto-translation failed: {e}")

        if pair.affiliate_rules and current_text.strip():
            current_text = affiliate_replacer.replace_affiliate_links(current_text, pair.affiliate_rules)

        # AI Paraphraser & VIP Premium Emojis (Subscription-gated)
        need_sub_check = (pair.ai_paraphrase_mode and pair.ai_paraphrase_mode != "off") or pair.auto_premium_emojis
        if need_sub_check and current_text.strip():
            sub = await db_manager.get_user_subscription(pair.user_id)
            is_admin = pair.user_id in settings.admin_ids

            # AI Paraphraser & Tone Shifter (Pro/VIP exclusive with active status)
            if pair.ai_paraphrase_mode and pair.ai_paraphrase_mode != "off":
                if is_admin or (sub.is_active and sub.tier in ["pro", "vip"]):
                    current_text = ai_paraphraser.paraphrase(current_text, mode=pair.ai_paraphrase_mode)

            # VIP Exclusive: Convert standard Unicode emojis into animated Telegram Premium custom emojis
            if pair.auto_premium_emojis:
                if is_admin or (sub.is_active and sub.tier == "vip"):
                    current_text = emoji_converter.convert_to_premium_emojis(current_text)

        if pair.custom_signature and not pair.remove_signature:
            current_text = TextProcessor.attach_signature(current_text, pair.custom_signature)

        return current_text

    async def clone_single_message(self, message: TelethonMessage, pair: ChannelPair) -> bool:
        if not self.bot:
            logger.error("Bot instance not set in ClonerEngine.")
            return False

        # Verify active subscription / 14-day trial for channel owner
        sub = await db_manager.get_user_subscription(pair.user_id)
        is_admin = pair.user_id in settings.admin_ids
        if not is_admin and not sub.is_active:
            logger.warning(f"Subscription or 14-day trial expired for user {pair.user_id}. Skipping post {message.id} for pair #{pair.id}")
            return False

        if await db_manager.is_message_cloned(pair.id, message.id):
            logger.debug(f"Message {message.id} already cloned for pair {pair.id}. Skipping.")
            return False

        raw_text = extract_message_html(message)
        processed_text = await self.process_post_text(raw_text, pair)

        if processed_text is None and raw_text:
            logger.info(f"Message {message.id} blocked by blacklist for pair {pair.id}.")
            return False

        # Build dynamic CTA buttons if enabled
        cta_markup = None
        if pair.auto_cta_buttons and processed_text:
            processed_text, cta_links = dynamic_affiliate_engine.extract_and_convert_links(processed_text, pair.affiliate_rules)
            cta_markup = dynamic_affiliate_engine.build_cta_keyboard(cta_links)

        # Apply smart rate limiter delay
        await rate_limiter.wait_for_slot(pair.target_channel)

        target_chat_id = self._normalize_chat_id(pair.target_channel)
        media_type = media_handler.get_media_type(message)
        logger.info(f"Processing message {message.id} ({media_type}) for pair {pair.id} -> {target_chat_id}")

        files_to_cleanup: List[str] = []
        sent_msg: Optional[AiogramMessage] = None
        overflow_text: Optional[str] = None

        try:
            if media_type == "text":
                if not processed_text:
                    logger.debug("Empty text message after cleaning. Skipping.")
                    return False
                sent_msg = await self._send_with_retry(
                    self.bot.send_message,
                    chat_id=target_chat_id,
                    text=processed_text,
                    parse_mode="HTML",
                    reply_markup=cta_markup,
                    disable_web_page_preview=False
                )

            elif media_type in ["photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation"]:
                async with cache_manager.media_semaphore:
                    temp_file = await media_handler.download_telethon_media(message)
                    if not temp_file or not os.path.exists(temp_file):
                        logger.error(f"Failed to download media for message {message.id}")
                        return False
                    files_to_cleanup.append(temp_file)

                    if media_type == "photo" and pair.image_watermark_type != "none":
                        wm_text = pair.image_watermark_text or pair.custom_signature or pair.target_channel
                        if wm_text:
                            temp_file = watermark_service.apply_text_watermark(
                                image_path=temp_file,
                                text=wm_text,
                                position=pair.image_watermark_pos or "bottom_right"
                            )

                    elif media_type == "video" and pair.video_watermark_type != "none":
                        if is_admin or sub.tier in ["pro", "vip"]:
                            wm_text = pair.video_watermark_text or pair.image_watermark_text or pair.target_channel
                            if wm_text:
                                wm_video = await video_watermark_service.apply_video_text_watermark(
                                    input_video_path=temp_file,
                                    watermark_text=wm_text,
                                    pos=pair.video_watermark_pos or "bottom_right"
                                )
                                if wm_video and os.path.exists(wm_video):
                                    files_to_cleanup.append(wm_video)
                                    temp_file = wm_video

                    caption, overflow = TextProcessor.fit_caption_limit(processed_text or "", max_limit=1020)
                    overflow_text = overflow
                    input_file = FSInputFile(temp_file)
                    file_size_mb = os.path.getsize(temp_file) / (1024 * 1024)

                    if file_size_mb > 49.0:
                        sent_msg = await self._telethon_send_fallback(target_chat_id, temp_file, caption)
                    else:
                        if media_type == "photo":
                            sent_msg = await self._send_with_retry(
                                self.bot.send_photo,
                                chat_id=target_chat_id,
                                photo=input_file,
                                caption=caption or None,
                                parse_mode="HTML",
                                reply_markup=cta_markup
                            )
                        elif media_type == "video":
                            sent_msg = await self._send_with_retry(
                                self.bot.send_video,
                                chat_id=target_chat_id,
                                video=input_file,
                                caption=caption or None,
                                parse_mode="HTML",
                                reply_markup=cta_markup,
                                supports_streaming=True
                            )
                        elif media_type == "voice":
                            sent_msg = await self._send_with_retry(
                                self.bot.send_voice,
                                chat_id=target_chat_id,
                                voice=input_file,
                                caption=caption or None,
                                parse_mode="HTML"
                            )
                        elif media_type == "video_note":
                            sent_msg = await self._send_with_retry(
                                self.bot.send_video_note,
                                chat_id=target_chat_id,
                                video_note=input_file
                            )
                        elif media_type == "audio":
                            sent_msg = await self._send_with_retry(
                                self.bot.send_audio,
                                chat_id=target_chat_id,
                                audio=input_file,
                                caption=caption or None,
                                parse_mode="HTML"
                            )
                        elif media_type == "sticker":
                            sent_msg = await self._send_with_retry(
                                self.bot.send_sticker,
                                chat_id=target_chat_id,
                                sticker=input_file
                            )
                        elif media_type == "animation":
                            sent_msg = await self._send_with_retry(
                                self.bot.send_animation,
                                chat_id=target_chat_id,
                                animation=input_file,
                                caption=caption or None,
                                parse_mode="HTML"
                            )
                        elif media_type == "document":
                            sent_msg = await self._send_with_retry(
                                self.bot.send_document,
                                chat_id=target_chat_id,
                                document=input_file,
                                caption=caption or None,
                                parse_mode="HTML"
                            )

            elif media_type == "poll":
                # BUG #3 fix: message.media is MessageMediaPoll, message.media.poll is the Poll object
                poll_media = message.media
                if not poll_media or not hasattr(poll_media, 'poll'):
                    logger.warning(f"Could not extract poll from message {message.id}")
                    return False
                poll = poll_media.poll
                # BUG #4 fix: use getattr for safe access to poll attributes
                answers = [ans.text for ans in (poll.answers or [])]
                question = getattr(poll, 'question', '') or ''
                is_anonymous = not getattr(poll, 'public_voters', False)
                multiple_answers = getattr(poll, 'multiple_choice', False)
                sent_msg = await self._send_with_retry(
                    self.bot.send_poll,
                    chat_id=target_chat_id,
                    question=question,
                    options=answers,
                    is_anonymous=is_anonymous,
                    allows_multiple_answers=multiple_answers
                )
            elif media_type == "contact" and message.media:
                c = message.media
                sent_msg = await self._send_with_retry(
                    self.bot.send_contact,
                    chat_id=target_chat_id,
                    phone_number=getattr(c, 'phone_number', '') or '',
                    first_name=getattr(c, 'first_name', '') or '',
                    last_name=getattr(c, 'last_name', '') or None
                )

            elif media_type == "location" and message.media:
                geo = getattr(message.media, 'geo', None)
                if hasattr(message.media, 'title') and hasattr(message.media, 'address'):
                    # Venue
                    sent_msg = await self._send_with_retry(
                        self.bot.send_venue,
                        chat_id=target_chat_id,
                        latitude=geo.lat if geo else 0.0,
                        longitude=geo.long if geo else 0.0,
                        title=getattr(message.media, 'title', ''),
                        address=getattr(message.media, 'address', '')
                    )
                elif geo:
                    sent_msg = await self._send_with_retry(
                        self.bot.send_location,
                        chat_id=target_chat_id,
                        latitude=geo.lat,
                        longitude=geo.long
                    )

            if sent_msg:
                if overflow_text:
                    try:
                        await self._send_with_retry(
                            self.bot.send_message,
                            chat_id=target_chat_id,
                            text=overflow_text,
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.warning(f"Could not send overflow text: {e}")

                target_id = getattr(sent_msg, 'message_id', None) or getattr(sent_msg, 'id', None)
                await db_manager.record_cloned_message(
                    pair_id=pair.id,
                    source_msg_id=message.id,
                    target_msg_id=target_id,
                    media_type=media_type
                )

                if pair.backup_enabled:
                    # BUG #24 & #41 fix: Extract true Telegram file_id for restoration
                    real_file_id = None
                    if hasattr(sent_msg, 'photo') and sent_msg.photo:
                        real_file_id = sent_msg.photo[-1].file_id
                    elif hasattr(sent_msg, 'video') and sent_msg.video:
                        real_file_id = sent_msg.video.file_id
                    elif hasattr(sent_msg, 'document') and sent_msg.document:
                        real_file_id = sent_msg.document.file_id
                    elif hasattr(sent_msg, 'audio') and sent_msg.audio:
                        real_file_id = sent_msg.audio.file_id
                    elif hasattr(sent_msg, 'voice') and sent_msg.voice:
                        real_file_id = sent_msg.voice.file_id
                    elif hasattr(sent_msg, 'animation') and sent_msg.animation:
                        real_file_id = sent_msg.animation.file_id
                    elif hasattr(sent_msg, 'sticker') and sent_msg.sticker:
                        real_file_id = sent_msg.sticker.file_id

                    await disaster_recovery_service.archive_message(
                        pair_id=pair.id,
                        source_id=pair.source_id,
                        message_id=message.id,
                        text=processed_text or "",
                        media_type=media_type,
                        media_file_id=real_file_id
                    )

                logger.info(f"Successfully cloned message {message.id} -> {target_chat_id} (msg {target_id})")
                return True

        except Exception as e:
            logger.error(f"Error cloning message {message.id} to {target_chat_id}: {e}", exc_info=True)
            return False
        finally:
            if files_to_cleanup:
                await media_handler.cleanup_files(files_to_cleanup)

        return False

    async def dispatch_queued_payload(self, bot: Bot, pair: ChannelPair, payload: Dict[str, Any], disable_notification: bool = False):
        """Dispatches an enqueued message payload for drip feed or night buffer delivery"""
        target_chat_id = self._normalize_chat_id(pair.target_channel)
        text = payload.get("text", "")
        media_type = payload.get("media_type", "text")
        media_file_id = payload.get("media_file_id")

        if media_type == "text" and text:
            await bot.send_message(chat_id=target_chat_id, text=text, parse_mode="HTML", disable_notification=disable_notification)
        elif media_type == "photo" and media_file_id:
            await bot.send_photo(chat_id=target_chat_id, photo=media_file_id, caption=text or None, parse_mode="HTML", disable_notification=disable_notification)
        elif media_type == "video" and media_file_id:
            await bot.send_video(chat_id=target_chat_id, video=media_file_id, caption=text or None, parse_mode="HTML", disable_notification=disable_notification)
        elif text:
            await bot.send_message(chat_id=target_chat_id, text=text, parse_mode="HTML", disable_notification=disable_notification)

    async def clone_media_group(self, messages: List[TelethonMessage], pair: ChannelPair) -> bool:
        if not self.bot or not messages:
            return False

        # Verify active subscription / 14-day trial for channel owner
        sub = await db_manager.get_user_subscription(pair.user_id)
        is_admin = pair.user_id in settings.admin_ids
        if not is_admin and not sub.is_active:
            logger.warning(f"Subscription or 14-day trial expired for user {pair.user_id}. Skipping media group for pair #{pair.id}")
            return False

        uncloned = [m for m in messages if not await db_manager.is_message_cloned(pair.id, m.id)]
        if not uncloned:
            return False

        # Apply rate limiter
        await rate_limiter.wait_for_slot(pair.target_channel)

        target_chat_id = self._normalize_chat_id(pair.target_channel)

        raw_caption = ""
        for msg in uncloned:
            msg_html = extract_message_html(msg)
            if msg_html:
                raw_caption = msg_html
                break

        processed_caption = await self.process_post_text(raw_caption, pair)
        if processed_caption is None and raw_caption:
            logger.info(f"Media group blocked by blacklist for pair {pair.id}.")
            return False

        caption, overflow_text = TextProcessor.fit_caption_limit(processed_caption or "", max_limit=1020)
        downloaded_files: List[str] = []
        media_group_items = []

        try:
            async with cache_manager.media_semaphore:
                for idx, msg in enumerate(uncloned):
                    temp_path = await media_handler.download_telethon_media(msg)
                    if not temp_path or not os.path.exists(temp_path):
                        continue

                    m_type = media_handler.get_media_type(msg)

                    if m_type == "photo" and pair.image_watermark_type != "none":
                        wm_text = pair.image_watermark_text or pair.custom_signature or pair.target_channel
                        if wm_text:
                            temp_path = watermark_service.apply_text_watermark(
                                image_path=temp_path,
                                text=wm_text,
                                position=pair.image_watermark_pos or "bottom_right"
                            )

                    downloaded_files.append(temp_path)
                    input_file = FSInputFile(temp_path)
                    item_caption = (caption if idx == 0 and caption else None)

                    if m_type == "photo":
                        media_group_items.append(InputMediaPhoto(media=input_file, caption=item_caption, parse_mode="HTML"))
                    elif m_type == "video":
                        media_group_items.append(InputMediaVideo(media=input_file, caption=item_caption, parse_mode="HTML"))
                    elif m_type == "audio":
                        media_group_items.append(InputMediaAudio(media=input_file, caption=item_caption, parse_mode="HTML"))
                    elif m_type == "document":
                        media_group_items.append(InputMediaDocument(media=input_file, caption=item_caption, parse_mode="HTML"))

                if not media_group_items:
                    return False

                # Telegram Bot API enforces 2 to 10 items per send_media_group call
                chunk_size = 10
                # Send each chunk and track resulting message IDs
                sent_message_ids: Dict[int, Optional[int]] = {}  # source_msg_id -> target_msg_id
                for i in range(0, len(media_group_items), chunk_size):
                    chunk = media_group_items[i:i + chunk_size]
                    chunk_msgs = uncloned[i:i + chunk_size]
                    if len(chunk) == 1:
                        item = chunk[0]
                        src_msg = chunk_msgs[0]
                        result_msg = None
                        if isinstance(item, InputMediaPhoto):
                            result_msg = await self._send_with_retry(
                                self.bot.send_photo,
                                chat_id=target_chat_id,
                                photo=item.media,
                                caption=item.caption,
                                parse_mode=item.parse_mode
                            )
                        elif isinstance(item, InputMediaVideo):
                            result_msg = await self._send_with_retry(
                                self.bot.send_video,
                                chat_id=target_chat_id,
                                video=item.media,
                                caption=item.caption,
                                parse_mode=item.parse_mode
                            )
                        elif isinstance(item, InputMediaAudio):
                            result_msg = await self._send_with_retry(
                                self.bot.send_audio,
                                chat_id=target_chat_id,
                                audio=item.media,
                                caption=item.caption,
                                parse_mode=item.parse_mode
                            )
                        else:
                            result_msg = await self._send_with_retry(
                                self.bot.send_document,
                                chat_id=target_chat_id,
                                document=item.media,
                                caption=item.caption,
                                parse_mode=item.parse_mode
                            )
                        if result_msg:
                            tid = getattr(result_msg, 'message_id', None) or getattr(result_msg, 'id', None)
                            sent_message_ids[src_msg.id] = tid
                    else:
                        result_msgs = await self._send_with_retry(
                            self.bot.send_media_group,
                            chat_id=target_chat_id,
                            media=chunk
                        )
                        if result_msgs and isinstance(result_msgs, (list, tuple)):
                            for j, r_msg in enumerate(result_msgs):
                                if j < len(chunk_msgs):
                                    tid = getattr(r_msg, 'message_id', None) or getattr(r_msg, 'id', None)
                                    sent_message_ids[chunk_msgs[j].id] = tid

                if overflow_text:
                    try:
                        await self._send_with_retry(
                            self.bot.send_message,
                            chat_id=target_chat_id,
                            text=overflow_text,
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                group_id_str = str(messages[0].grouped_id or "")
                for msg in uncloned:
                    # BUG #5 fix: include target_msg_id from tracking dict
                    target_msg_id = sent_message_ids.get(msg.id)
                    await db_manager.record_cloned_message(
                        pair_id=pair.id,
                        source_msg_id=msg.id,
                        target_msg_id=target_msg_id,
                        media_group_id=group_id_str,
                        media_type="media_group"
                    )

                logger.info(f"Successfully cloned media group ({len(media_group_items)} items) to {target_chat_id}")
                return True

        except Exception as e:
            logger.error(f"Error cloning media group to {target_chat_id}: {e}", exc_info=True)
            return False
        finally:
            await media_handler.cleanup_files(downloaded_files)

    @staticmethod
    def _normalize_chat_id(target_chat: str) -> Union[int, str]:
        target_chat = target_chat.strip()
        if target_chat.startswith("-100") or (target_chat.startswith("-") and target_chat[1:].isdigit()):
            return int(target_chat)
        elif target_chat.isdigit():
            return int(f"-100{target_chat}")
        return target_chat

    async def _telethon_send_fallback(self, target_chat_id: Union[int, str], file_path: str, caption: Optional[str]):
        from services.telethon_listener import telethon_listener
        if telethon_listener.client and telethon_listener.client.is_connected():
            try:
                target_entity = await telethon_listener.resolve_entity(str(target_chat_id))
                if target_entity:
                    msg = await telethon_listener.client.send_file(
                        target_entity,
                        file=file_path,
                        caption=caption or ""
                    )
                    return msg
            except Exception as e:
                logger.error(f"Telethon large file fallback failed: {e}")
        return None

    async def _send_with_retry(self, send_func, *args, max_retries: int = 3, **kwargs):
        for attempt in range(1, max_retries + 1):
            try:
                return await send_func(*args, **kwargs)
            except TelegramRetryAfter as e:
                wait_time = e.retry_after
                logger.warning(f"Telegram FloodWait: waiting {wait_time}s (attempt {attempt}/{max_retries})")
                await asyncio.sleep(wait_time + 1)
            except TelegramBadRequest as e:
                err_str = str(e).lower()
                if ("parse" in err_str or "tag" in err_str or "unsupported" in err_str) and ("parse_mode" in kwargs or any(hasattr(a, "parse_mode") for a in args)):
                    logger.warning(f"Telegram parse entity error ({e}), retrying without formatting tags...")
                    clean_kwargs = dict(kwargs)
                    clean_kwargs.pop('parse_mode', None)
                    if 'text' in clean_kwargs and isinstance(clean_kwargs['text'], str):
                        clean_kwargs['text'] = re.sub(r'<[^>]+>', '', clean_kwargs['text'])
                    if 'caption' in clean_kwargs and isinstance(clean_kwargs['caption'], str):
                        clean_kwargs['caption'] = re.sub(r'<[^>]+>', '', clean_kwargs['caption'])
                    try:
                        return await send_func(*args, **clean_kwargs)
                    except Exception as retry_err:
                        logger.error(f"Fallback plain text send also failed: {retry_err}")
                logger.error(f"Telegram BadRequest: {e}")
                raise
            except TelegramAPIError as e:
                logger.error(f"Telegram API Error (attempt {attempt}): {e}")
                if attempt == max_retries:
                    raise
                await asyncio.sleep(2 * attempt)
            except Exception as e:
                logger.error(f"Unexpected error sending message: {e}")
                if attempt == max_retries:
                    raise
                await asyncio.sleep(1)
        return None

cloner_engine = ClonerEngine()
