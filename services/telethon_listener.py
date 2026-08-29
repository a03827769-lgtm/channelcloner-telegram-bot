import asyncio
import logging
import random
from typing import Optional, List, Dict, Callable, Any, Tuple
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, Message as TelethonMessage
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.functions.updates import GetStateRequest
from telethon.errors import (
    FloodWaitError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError
)
from config.settings import settings
from database.db_manager import db_manager
from database.models import ChannelPair
from services.cloner_engine import cloner_engine
from services.media_handler import media_handler
from services.phone_utils import normalize_phone_number

logger = logging.getLogger(__name__)

# Professional Branding metadata for Telegram Sessions
DEVICE_MODEL = "Klonla Bot Server"
SYSTEM_VERSION = "Linux Server 64bit"
APP_VERSION = "KlonlaBot Pro v3.0"
LANG_CODE = "uz"
SYSTEM_LANG_CODE = "uz-UZ"

class TelethonListener:
    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self._is_running = False
        self._monitored_channels: set[int] = set()
        self._channel_usernames: set[str] = set()
        self._login_sessions: Dict[int, Dict[str, Any]] = {}
        self._supervisor_task: Optional[asyncio.Task] = None
        self.active_history_tasks: Dict[int, asyncio.Task] = {}

    def cancel_history_clone(self, pair_id: int) -> bool:
        """Cancels an ongoing history cloning task for the specified pair"""
        task = self.active_history_tasks.get(pair_id)
        if task and not task.done():
            task.cancel()
            self.active_history_tasks.pop(pair_id, None)
            return True
        return False

    async def get_active_session_string(self) -> Optional[str]:
        from services.security_vault import security_vault
        raw_session = await db_manager.get_setting("telethon_session")
        if not raw_session:
            raw_session = settings.TELETHON_SESSION or None
        if not raw_session:
            return None
        
        # If encrypted with enc:, decrypt transparently
        if raw_session.startswith("enc:"):
            decrypted = security_vault.decrypt_secret(raw_session)
            if decrypted:
                return decrypted
        return raw_session

    async def start(self):
        """Starts Telethon client and initiates self-healing connection supervisor"""
        if not self.is_configured():
            logger.warning("Telethon API_ID/API_HASH not configured. MTProto listener paused.")
            return

        session_str = await self.get_active_session_string()
        if not session_str:
            logger.warning("No Telethon session found. Please login via bot or setup_wizard.py.")
            return

        if self.client and self.client.is_connected():
            try:
                await self.client.disconnect()
            except Exception:
                pass

        try:
            self.client = TelegramClient(
                StringSession(session_str),
                settings.TELEGRAM_API_ID,
                settings.TELEGRAM_API_HASH,
                device_model=DEVICE_MODEL,
                system_version=SYSTEM_VERSION,
                app_version=APP_VERSION,
                lang_code=LANG_CODE,
                system_lang_code=SYSTEM_LANG_CODE
            )
        except Exception as e:
            logger.error(f"Failed to initialize Telethon StringSession: {e}")
            self._is_running = False
            return

        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.warning("Telethon session is not authorized or expired!")
                self._is_running = False
                return

            me = await self.client.get_me()
            name = me.first_name if me else "Unknown"
            logger.info(f"Telethon MTProto connected successfully as: {name} (@{getattr(me, 'username', 'no_username')}, phone: {getattr(me, 'phone', 'unknown')})")
            self._is_running = True

            # Re-save fresh session string
            fresh_session = self.client.session.save()
            if fresh_session:
                await db_manager.set_setting("telethon_session", fresh_session)

            # Register real-time message handler (remove existing first to prevent duplicate callbacks)
            try:
                self.client.remove_event_handler(self._handle_new_message, events.NewMessage)
            except Exception:
                pass
            self.client.add_event_handler(self._handle_new_message, events.NewMessage)

            # Join all active pairs
            await self.refresh_monitored_channels()

            # Start self-healing supervisor
            if not self._supervisor_task or self._supervisor_task.done():
                self._supervisor_task = asyncio.create_task(self._connection_supervisor())

        except Exception as e:
            logger.error(f"Failed to start Telethon client: {e}")
            self._is_running = False

    async def _connection_supervisor(self):
        """Active MTProto Watchdog: Pings Telegram DC via GetStateRequest & auto-reconnects with jitter"""
        consecutive_failures = 0
        base_delay = 2.0
        max_delay = 60.0
        prune_counter = 0

        while self._is_running:
            try:
                await asyncio.sleep(40)
                if not self.client:
                    continue

                is_alive = False
                if self.client.is_connected():
                    try:
                        # Active RPC probe to detect half-open sockets
                        await asyncio.wait_for(self.client(GetStateRequest()), timeout=10.0)
                        is_alive = True
                        consecutive_failures = 0
                    except (asyncio.TimeoutError, ConnectionError, OSError) as net_err:
                        logger.warning(f"Active MTProto ping probe timed out/failed: {net_err}")
                    except FloodWaitError as fwe:
                        logger.warning(f"FloodWait on probe: sleeping {fwe.seconds}s")
                        await asyncio.sleep(fwe.seconds + 1)
                        is_alive = True
                    except Exception:
                        is_alive = bool(self.client.is_connected())

                if not is_alive:
                    consecutive_failures += 1
                    backoff = min(max_delay, base_delay * (2 ** min(consecutive_failures, 5)))
                    jittered_delay = random.uniform(backoff * 0.5, backoff * 1.5)
                    logger.warning(f"Telethon connection stalled (fail #{consecutive_failures}). Reconnecting in {jittered_delay:.1f}s...")
                    await asyncio.sleep(jittered_delay)

                    try:
                        if self.client.is_connected():
                            await self.client.disconnect()
                    except Exception:
                        pass

                    try:
                        await self.client.connect()
                        if await self.client.is_user_authorized():
                            logger.info("✅ Telethon MTProto connection restored successfully.")
                            try:
                                self.client.remove_event_handler(self._handle_new_message, events.NewMessage)
                            except Exception:
                                pass
                            self.client.add_event_handler(self._handle_new_message, events.NewMessage)
                            consecutive_failures = 0
                    except Exception as rec_err:
                        logger.error(f"Reconnection attempt #{consecutive_failures} failed: {rec_err}")

                # Periodic Entity Cache Pruning to keep memory footprint < 200MB
                prune_counter += 1
                if prune_counter >= 30:
                    prune_counter = 0
                    await self._prune_entity_cache()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Telethon connection supervisor: {e}")
                await asyncio.sleep(10)

    async def _prune_entity_cache(self):
        """Prunes old Telethon cached entities to keep container RAM usage minimal (<200MB)"""
        try:
            if not self.client or not hasattr(self.client, "_entities"):
                return
            count = len(self.client._entities)
            if count > 2000:
                active_pairs = await db_manager.get_all_active_pairs()
                keep_ids = set()
                for p in active_pairs:
                    if p.source_id:
                        keep_ids.add(p.source_id)
                    if p.target_id:
                        keep_ids.add(p.target_id)
                me = await self.get_me()
                if me:
                    keep_ids.add(me.id)

                self.client._entities = {k: v for k, v in self.client._entities.items() if k in keep_ids}
                if hasattr(self.client, "_input_entities"):
                    self.client._input_entities = {k: v for k, v in self.client._input_entities.items() if k in keep_ids}
                logger.info(f"Pruned Telethon entity cache: {count} -> {len(self.client._entities)} items retained.")
        except Exception as pe:
            logger.debug(f"Entity cache pruning skipped: {pe}")

    async def stop(self):
        """Gracefully stops Telethon client"""
        self._is_running = False
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
        if self.client and self.client.is_connected():
            await self.client.disconnect()
            logger.info("Telethon client disconnected.")

    def is_connected(self) -> bool:
        """Returns True if Telethon client is active and connected"""
        return bool(self._is_running and self.client and self.client.is_connected())

    async def get_me(self):
        """Returns current authenticated Telethon user or None"""
        if not self.client or not self.client.is_connected():
            return None
        try:
            if await self.client.is_user_authorized():
                return await self.client.get_me()
        except Exception:
            pass
        return None

    # --- IN-BOT AUTHENTICATION FLOW ---

    async def request_phone_code(self, user_id: int, phone: str) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "TELEGRAM_API_ID va TELEGRAM_API_HASH sozlanmagan!"

        is_valid, phone_e164, _ = normalize_phone_number(phone)
        if not is_valid:
            return False, "Telefon raqam formati noto'g'ri!"

        try:
            if user_id in self._login_sessions:
                old_client: TelegramClient = self._login_sessions[user_id].get("client")
                if old_client and old_client.is_connected():
                    try:
                        await old_client.disconnect()
                    except Exception:
                        pass

            client = TelegramClient(
                StringSession(""),
                settings.TELEGRAM_API_ID,
                settings.TELEGRAM_API_HASH,
                device_model=DEVICE_MODEL,
                system_version=SYSTEM_VERSION,
                app_version=APP_VERSION,
                lang_code=LANG_CODE,
                system_lang_code=SYSTEM_LANG_CODE
            )
            await client.connect()

            sent_code = await client.send_code_request(phone_e164)
            self._login_sessions[user_id] = {
                "client": client,
                "phone": phone_e164,
                "phone_code_hash": sent_code.phone_code_hash
            }
            logger.info(f"OTP code requested successfully for user {user_id} ({phone_e164})")
            return True, "Kod yuborildi."

        except FloodWaitError as e:
            return False, f"Telegram cheklovi: Iltimos, {e.seconds} soniya kuting."
        except Exception as e:
            logger.error(f"Error requesting phone code: {e}")
            return False, f"Xatolik: {e}"

    async def submit_phone_code(self, user_id: int, code: str) -> Tuple[bool, str, str]:
        if user_id not in self._login_sessions:
            return False, "Sessiya topilmadi! Telefon raqamni qaytadan kiriting.", "error"

        session_data = self._login_sessions[user_id]
        client: TelegramClient = session_data["client"]
        phone = session_data["phone"]
        phone_code_hash = session_data["phone_code_hash"]

        code_clean = "".join(c for c in code if c.isdigit())

        try:
            await client.sign_in(phone, code_clean, phone_code_hash=phone_code_hash)
            me = await client.get_me()
            session_str = client.session.save()
            await db_manager.set_setting("telethon_session", session_str)
            self._login_sessions.pop(user_id, None)

            # Start listener with new session
            await self.start()
            name = me.first_name if me else "Foydalanuvchi"
            logger.info(f"User {user_id} successfully authenticated Telethon session as: {name}")
            return True, f"Hisob muvaffaqiyatli ulandi: {name}!", "success"

        except SessionPasswordNeededError:
            return False, "2-bosqichli parol (Two-Step Verification) talab qilinadi.", "needs_2fa"
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            return False, "Tasdiqlash kodi noto'g'ri yoki muddati o'tgan!", "error"
        except Exception as e:
            logger.error(f"Sign in error: {e}")
            return False, f"Xatolik: {e}", "error"

    async def submit_2fa_password(self, user_id: int, password: str) -> Tuple[bool, str]:
        if user_id not in self._login_sessions:
            return False, "Sessiya topilmadi! Qaytadan urinib ko'ring."

        session_data = self._login_sessions[user_id]
        client: TelegramClient = session_data["client"]

        try:
            await client.sign_in(password=password.strip())
            me = await client.get_me()
            session_str = client.session.save()
            await db_manager.set_setting("telethon_session", session_str)
            self._login_sessions.pop(user_id, None)

            await self.start()
            name = me.first_name if me else "Foydalanuvchi"
            logger.info(f"User {user_id} 2FA authenticated Telethon session as: {name}")
            return True, f"Hisob muvaffaqiyatli ulandi: {name}!"
        except PasswordHashInvalidError:
            return False, "2FA paroli noto'g'ri!"
        except Exception as e:
            logger.error(f"2FA sign in error: {e}")
            return False, f"Xatolik: {e}"

    async def logout(self):
        """Logs out and deletes active session"""
        await self.stop()
        await db_manager.delete_setting("telethon_session")
        self.client = None
        self._is_running = False
        logger.info("Telethon session deleted.")

    # --- CHANNEL RESOLUTION & MONITORING ---

    async def resolve_entity(self, channel_identifier: str):
        if not self.client or not self.client.is_connected():
            return None

        clean_id = channel_identifier.strip()
        if clean_id.startswith("https://t.me/"):
            clean_id = clean_id.replace("https://t.me/", "")
            if "/" in clean_id and not clean_id.startswith("+") and not clean_id.startswith("joinchat/"):
                clean_id = clean_id.split("/")[0]

        try:
            if clean_id.startswith("+") or clean_id.startswith("joinchat/"):
                hash_val = clean_id.lstrip("+").replace("joinchat/", "").strip()
                try:
                    res = await self.client(CheckChatInviteRequest(hash_val))
                    if hasattr(res, 'chat') and res.chat:
                        return res.chat
                except Exception:
                    pass
                try:
                    res = await self.client(ImportChatInviteRequest(hash_val))
                    if hasattr(res, 'chats') and res.chats:
                        return res.chats[0]
                except Exception as e:
                    logger.debug(f"Invite link import result for {hash_val}: {e}")

            if clean_id.startswith("-100") or (clean_id.startswith("-") and clean_id[1:].isdigit()):
                return await self.client.get_entity(int(clean_id))
            elif clean_id.isdigit():
                return await self.client.get_entity(int(f"-100{clean_id}"))
            else:
                return await self.client.get_entity(clean_id)
        except Exception as e:
            logger.error(f"Failed to resolve entity for '{channel_identifier}': {e}")
            return None

    async def join_and_monitor_channel(self, channel_identifier: str) -> Optional[int]:
        if not self.client or not self.client.is_connected():
            return None

        try:
            entity = await self.resolve_entity(channel_identifier)
            if not entity:
                return None

            if isinstance(entity, Channel) and getattr(entity, 'left', False):
                try:
                    await self.client(JoinChannelRequest(entity))
                    logger.info(f"Joined source channel: {getattr(entity, 'title', channel_identifier)}")
                except Exception as e:
                    logger.debug(f"Join channel notice: {e}")

            cid = getattr(entity, 'id', None)
            if cid:
                self._monitored_channels.add(cid)
                username = getattr(entity, 'username', None)
                if username:
                    self._channel_usernames.add(username.lower())
                return cid
        except Exception as e:
            logger.warning(f"Could not join/monitor channel '{channel_identifier}': {e}")
        return None

    async def refresh_monitored_channels(self):
        active_pairs = await db_manager.get_all_active_pairs()
        logger.info(f"Refreshing monitored channels for {len(active_pairs)} active pairs...")

        for pair in active_pairs:
            try:
                cid = await self.join_and_monitor_channel(pair.source_channel)
                if cid and not pair.source_id:
                    await db_manager.update_pair_source_id(pair.id, cid)
                logger.info(f"Monitoring pair #{pair.id}: {pair.source_title or pair.source_channel} ({pair.source_channel}) -> {pair.target_channel}")
            except Exception as e:
                logger.error(f"Error joining channel for pair {pair.id}: {e}")

    # --- REAL-TIME EVENT HANDLER ---

    async def _handle_new_message(self, event: events.NewMessage.Event):
        try:
            chat = await event.get_chat()
            if not chat:
                return

            chat_id = getattr(chat, 'id', None)
            chat_username = getattr(chat, 'username', None)
            if chat_username:
                chat_username = chat_username.lower()
            chat_title = getattr(chat, 'title', '')

            # Match active pairs (guaranteed unique per pair.id)
            active_pairs = await db_manager.get_all_active_pairs()
            matching_pairs: List[ChannelPair] = []
            seen_pair_ids = set()

            for p in active_pairs:
                if not p.is_active or (p.id is not None and p.id in seen_pair_ids):
                    continue
                clean_src = p.source_channel.lstrip("@").lower()
                is_match = False
                if p.source_id and chat_id and p.source_id == chat_id:
                    is_match = True
                elif chat_username and clean_src == chat_username:
                    is_match = True
                elif clean_src == str(chat_id):
                    is_match = True

                if is_match:
                    if p.id is not None:
                        seen_pair_ids.add(p.id)
                    matching_pairs.append(p)

            if not matching_pairs:
                return

            message = event.message
            if not message:
                return

            logger.info(f"🚀 Dispatching message {message.id} to {len(matching_pairs)} destination channels...")

            for pair in matching_pairs:
                if message.grouped_id:
                    buffer_key = (pair.id, message.grouped_id)
                    media_handler.album_buffer.add_message(
                        buffer_key,
                        message,
                        lambda k, msgs, p=pair: asyncio.create_task(cloner_engine.clone_media_group(msgs, p))
                    )
                else:
                    asyncio.create_task(cloner_engine.clone_single_message(message, pair))

        except Exception as e:
            logger.error(f"Error in Telethon _handle_new_message: {e}", exc_info=True)

    # --- HISTORY CLONE ---

    async def clone_history(
        self,
        pair: ChannelPair,
        limit: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int, str], Any]] = None
    ) -> Dict[str, Any]:
        if not self.client or not self.client.is_connected() or not await self.client.is_user_authorized():
            return {"total": 0, "cloned": 0, "failed": 0, "status": "client_not_connected"}

        current_task = asyncio.current_task()
        self.active_history_tasks[pair.id] = current_task

        entity = await self.resolve_entity(pair.source_channel)
        if not entity:
            self.active_history_tasks.pop(pair.id, None)
            return {"total": 0, "cloned": 0, "failed": 0, "status": "source_not_found"}

        logger.info(f"Starting streaming history clone for pair {pair.id}: {pair.source_channel} -> {pair.target_channel}")

        total_available = 0
        try:
            total_msgs = await self.client.get_messages(entity, limit=0)
            total_available = getattr(total_msgs, "total", 0) or 0
        except Exception as e:
            logger.warning(f"Could not get total message count for {pair.source_channel}: {e}")

        target_total = min(limit, total_available) if (limit and total_available > 0) else (limit or total_available or 100)

        cloned_count = 0
        failed_count = 0
        processed_count = 0
        status = "completed"

        pending_album: List[TelethonMessage] = []
        last_group_id = None

        async def flush_album():
            nonlocal cloned_count, failed_count
            if not pending_album:
                return
            try:
                ok = await cloner_engine.clone_media_group(list(pending_album), pair)
                if ok:
                    cloned_count += len(pending_album)
                else:
                    failed_count += len(pending_album)
            except Exception as e:
                logger.error(f"Error cloning history album: {e}")
                failed_count += len(pending_album)
            finally:
                pending_album.clear()

        try:
            # Stream messages chronologically from oldest to newest
            async for msg in self.client.iter_messages(entity, limit=limit, reverse=True):
                processed_count += 1
                if target_total < processed_count:
                    target_total = processed_count

                if msg.grouped_id:
                    if last_group_id is None or last_group_id == msg.grouped_id:
                        pending_album.append(msg)
                        last_group_id = msg.grouped_id
                    else:
                        await flush_album()
                        pending_album.append(msg)
                        last_group_id = msg.grouped_id
                else:
                    await flush_album()
                    last_group_id = None
                    try:
                        ok = await cloner_engine.clone_single_message(msg, pair)
                        if ok:
                            cloned_count += 1
                    except FloodWaitError as fe:
                        logger.warning(f"FloodWait during history clone: sleeping {fe.seconds}s")
                        await asyncio.sleep(fe.seconds + 1)
                        try:
                            ok = await cloner_engine.clone_single_message(msg, pair)
                            if ok:
                                cloned_count += 1
                        except Exception:
                            failed_count += 1
                    except Exception as e:
                        logger.error(f"Error cloning single message #{msg.id}: {e}")
                        failed_count += 1

                if progress_callback and (processed_count % 2 == 0 or processed_count == 1):
                    try:
                        await progress_callback(processed_count, target_total, "running")
                    except Exception:
                        pass

                await asyncio.sleep(0.3)

            await flush_album()

            if progress_callback:
                try:
                    await progress_callback(processed_count, processed_count, "completed")
                except Exception:
                    pass

        except asyncio.CancelledError:
            logger.info(f"History clone for pair {pair.id} cancelled by user.")
            status = "cancelled"
            if progress_callback:
                try:
                    await progress_callback(processed_count, target_total, "cancelled")
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.error(f"Error during history clone: {e}", exc_info=True)
            status = f"error: {str(e)[:50]}"
            if progress_callback:
                try:
                    await progress_callback(processed_count, target_total, "failed")
                except Exception:
                    pass
        finally:
            self.active_history_tasks.pop(pair.id, None)

        return {
            "total": target_total,
            "cloned": cloned_count,
            "failed": failed_count,
            "status": status
        }

telethon_listener = TelethonListener()
