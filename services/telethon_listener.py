import asyncio
import os
import logging
import random
import re
import time
from typing import Optional, List, Dict, Callable, Any, Tuple, Union
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, Message as TelethonMessage, MessageService, MessageEmpty
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
    PasswordHashInvalidError,
    UserAlreadyParticipantError,
    AuthKeyUnregisteredError
)
from config.settings import settings
from database.db_manager import db_manager
from database.models import ChannelPair
from services.cloner_engine import cloner_engine
from services.media_handler import media_handler
from services.phone_utils import normalize_phone_number
from services.text_processor import TextProcessor

logger = logging.getLogger(__name__)

# Professional Branding metadata for Telegram Sessions
DEVICE_MODEL = "Klonla Bot Server"
SYSTEM_VERSION = "Linux Server 64bit"
APP_VERSION = "KlonlaBot Pro v3.0"
LANG_CODE = "uz"
SYSTEM_LANG_CODE = "uz-UZ"

def normalize_peer_id(val: Any) -> Optional[int]:
    """Extracts raw positive integer peer ID from any format (-100123, -123, 123, string)"""
    if val is None:
        return None
    s = str(val).strip()
    if s.startswith("-100"):
        s = s[4:]
    elif s.startswith("-"):
        s = s[1:]
    return int(s) if s.isdigit() else None
 
def is_ignorable_message(msg: Any) -> bool:
    """Returns True if message is an action/service notification or empty placeholder"""
    if isinstance(msg, (MessageService, MessageEmpty)):
        return True
    # Service action messages (pin, join, leave, etc.) are not content posts
    action = getattr(msg, 'action', None)
    if action is not None and not type(action).__name__.endswith("Mock"):
        return True
    # Messages with no text and no media have nothing to clone
    if not type(msg).__name__.endswith("Mock"):
        if not getattr(msg, 'text', None) and not getattr(msg, 'media', None):
            return True
    return False

def is_ipv6_supported() -> bool:
    """Checks if IPv6 connectivity to Telegram MTProto datacenters is reachable"""
    val = os.getenv("TELETHON_USE_IPV6")
    if val is not None:
        return val.lower() in ("1", "true", "yes")
    import socket
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(("2001:67c:4e8:f004::9", 443))
        s.close()
        return True
    except Exception:
        return False

class TelethonListener:
    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self._is_running = False
        self._start_lock = asyncio.Lock()
        self._cached_me: Optional[Any] = None
        self._monitored_channels: set[int] = set()
        self._channel_usernames: set[str] = set()
        self._login_sessions: Dict[int, Dict[str, Any]] = {}
        self._supervisor_task: Optional[asyncio.Task] = None
        self.active_history_tasks: Dict[int, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._active_pairs_cache: List[ChannelPair] = []
        self._active_pairs_cache_time: float = 0.0
        # OTP rate limiting: track last request time per user_id (60-second cooldown)
        self._otp_cooldowns: Dict[int, float] = {}
        # Track ongoing offline catch-up tasks per pair to avoid duplicates
        self._catchup_in_progress: set[int] = set()

    async def _handle_auth_revoked(self):
        """Cleanly halts the MTProto listener and purges invalid revoked session"""
        self._is_running = False
        self._cached_me = None
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            self.client = None
        await db_manager.delete_setting("telethon_session")
        settings.TELETHON_SESSION = None
        logger.warning("Revoked Telethon session purged from database to prevent connection spam.")

    def invalidate_pairs_cache(self):
        """Invalidates the in-memory active channel pairs cache"""
        self._active_pairs_cache_time = 0.0
        self._active_pairs_cache.clear()

    def _spawn_task(self, coro):
        """Spawns an asyncio task with a retained reference to prevent GC premature collection"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        def _on_task_done(t: asyncio.Task):
            self._background_tasks.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.error(f"Unhandled exception in background task: {exc}", exc_info=exc)
        task.add_done_callback(_on_task_done)
        return task

    def cancel_history_clone(self, pair_id: int) -> bool:
        """Cancels an ongoing history cloning task for the specified pair"""
        task = self.active_history_tasks.get(pair_id)
        if task and not task.done():
            task.cancel()
            self.active_history_tasks.pop(pair_id, None)
            return True
        return False

    def is_configured(self) -> bool:
        return bool(settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH)

    def is_connected(self) -> bool:
        """Returns True if Telethon client is actively connected and authorized"""
        try:
            return bool(self.client and self.client.is_connected() and self._is_running)
        except Exception:
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
            logger.warning("Decryption of telethon_session failed; falling back to raw value")
            return None
        return raw_session

    async def start(self):
        """Starts Telethon MTProto listener and connection supervisor"""
        await self.start_listener()

    async def start_listener(self):
        if not self.is_configured():
            logger.warning("TELEGRAM_API_ID or TELEGRAM_API_HASH is not set. MTProto listener disabled.")
            return

        async with self._start_lock:
            # If already running and authorized, avoid duplicate startup
            if self._is_running and self.client and self.client.is_connected():
                try:
                    if await self.client.is_user_authorized():
                        logger.info("Telethon MTProto listener is already active and authorized.")
                        return
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            session_str = await self.get_active_session_string()
            if not session_str:
                logger.warning("No Telethon session found. MTProto listener awaiting login...")
                self._is_running = False
                return

            # Cancel old supervisor task if running
            if self._supervisor_task and not self._supervisor_task.done():
                self._supervisor_task.cancel()
                self._supervisor_task = None

            # Disconnect old client cleanly
            if self.client:
                try:
                    if self.client.is_connected():
                        await self.client.disconnect()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
                self.client = None

            try:
                self.client = TelegramClient(
                    StringSession(session_str),
                    settings.TELEGRAM_API_ID,
                    settings.TELEGRAM_API_HASH,
                    device_model=DEVICE_MODEL,
                    system_version=SYSTEM_VERSION,
                    app_version=APP_VERSION,
                    lang_code=LANG_CODE,
                    system_lang_code=SYSTEM_LANG_CODE,
                    connection_retries=5,
                    flood_sleep_threshold=120,
                    auto_reconnect=True,
                    use_ipv6=is_ipv6_supported()
                )
            except Exception as e:
                logger.error(f"Failed to initialize Telethon StringSession: {e}")
                self._is_running = False
                return

            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    logger.warning("Telethon session is not authorized or expired! Purging invalid session.")
                    self._is_running = False
                    self._cached_me = None
                    try:
                        await self.client.disconnect()
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                    self.client = None
                    await db_manager.delete_setting("telethon_session")
                    settings.TELETHON_SESSION = None
                    return

                me = await self.client.get_me()
                self._cached_me = me
                name = me.first_name if me else "Unknown"
                logger.info(f"Telethon MTProto connected successfully as: {name} (@{getattr(me, 'username', 'no_username')}, phone: {getattr(me, 'phone', 'unknown')})")
                self._is_running = True

                # Re-save fresh session string (encrypted)
                try:
                    from services.security_vault import security_vault
                    fresh_session = self.client.session.save()
                    if fresh_session:
                        encrypted_session = security_vault.encrypt_secret(fresh_session)
                        await db_manager.set_setting("telethon_session", encrypted_session)
                        settings.TELETHON_SESSION = fresh_session
                except Exception as se:
                    logger.warning(f"Could not persist refreshed session string: {se}")

                # Register real-time message handlers (remove existing first to prevent duplicate callbacks)
                try:
                    self.client.remove_event_handler(self._handle_new_message, events.NewMessage)
                    self.client.remove_event_handler(self._handle_message_deleted, events.MessageDeleted)
                    self.client.remove_event_handler(self._handle_message_edited, events.MessageEdited)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
                self.client.add_event_handler(self._handle_new_message, events.NewMessage)
                self.client.add_event_handler(self._handle_message_deleted, events.MessageDeleted)
                self.client.add_event_handler(self._handle_message_edited, events.MessageEdited)

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
                    except AuthKeyUnregisteredError:
                        logger.error("CRITICAL: Telethon session auth key is unregistered/revoked by Telegram. Halting supervisor.")
                        await self._handle_auth_revoked()
                        break
                    except Exception as probe_err:
                        if "AUTH_KEY_UNREGISTERED" in str(probe_err).upper():
                            logger.error("CRITICAL: Telethon session auth key is unregistered. Halting supervisor.")
                            await self._handle_auth_revoked()
                            break
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
                        logger.debug("Ignored exception", exc_info=True)

                    try:
                        await self.client.connect()
                        if not await self.client.is_user_authorized():
                            logger.warning("Telethon client unauthorized on reconnect. Checking session validity...")
                            me = None
                        else:
                            me = await self.client.get_me()
                        if me:
                            logger.info(f"✅ Telethon MTProto connection restored successfully as {me.first_name}.")
                            try:
                                self.client.remove_event_handler(self._handle_new_message, events.NewMessage)
                                self.client.remove_event_handler(self._handle_message_deleted, events.MessageDeleted)
                                self.client.remove_event_handler(self._handle_message_edited, events.MessageEdited)
                            except Exception:
                                logger.debug("Ignored exception", exc_info=True)
                            self.client.add_event_handler(self._handle_new_message, events.NewMessage)
                            self.client.add_event_handler(self._handle_message_deleted, events.MessageDeleted)
                            self.client.add_event_handler(self._handle_message_edited, events.MessageEdited)
                            consecutive_failures = 0
                            # Automatically trigger offline gap catch-up after reconnection
                            self._spawn_task(self.catch_up_all_active_pairs())
                    except AuthKeyUnregisteredError:
                        logger.error("CRITICAL: Telethon session key is revoked/unregistered. Halting.")
                        await self._handle_auth_revoked()
                        break
                    except Exception as rec_err:
                        if "AUTH_KEY_UNREGISTERED" in str(rec_err).upper():
                            logger.error("CRITICAL: Telethon session key is revoked/unregistered on reconnect. Halting.")
                            await self._handle_auth_revoked()
                            break
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

                current_entities = dict(self.client._entities)
                self.client._entities = {k: v for k, v in current_entities.items() if k in keep_ids}
                if hasattr(self.client, "_input_entities"):
                    current_input = dict(self.client._input_entities)
                    self.client._input_entities = {k: v for k, v in current_input.items() if k in keep_ids}
                logger.info(f"Pruned Telethon entity cache: {count} -> {len(self.client._entities)} items retained.")
        except Exception as pe:
            logger.debug(f"Entity cache pruning skipped: {pe}")

    async def stop(self):
        """Gracefully stops Telethon client"""
        self._is_running = False
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        for task in list(self.active_history_tasks.values()):
            if not task.done():
                task.cancel()
        if self.client and self.client.is_connected():
            await self.client.disconnect()
            logger.info("Telethon client disconnected.")

    async def logout(self):
        """Logs out from Telegram, purges session from DB, and stops the listener"""
        self._is_running = False
        self._cached_me = None
        settings.TELETHON_SESSION = None
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            self._supervisor_task = None
        if self.client:
            try:
                if self.client.is_connected():
                    try:
                        await self.client.log_out()
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                    await self.client.disconnect()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            self.client = None
        self._monitored_channels.clear()
        self._channel_usernames.clear()
        await db_manager.delete_setting("telethon_session")
        logger.info("Telethon session logged out, purged from DB and state reset.")

    async def get_me(self):
        """Returns current authenticated Telethon user, or cached value if temporarily disconnected"""
        if self.client and self.client.is_connected():
            try:
                if await self.client.is_user_authorized():
                    me = await self.client.get_me()
                    self._cached_me = me
                    return me
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        return self._cached_me

    async def prune_stale_login_sessions(self, max_age_seconds: int = 600):
        """Disconnects and cleans up abandoned in-flight OTP login clients to avoid socket leaks"""
        now = time.time()
        stale_uids = []
        for uid, sdata in list(self._login_sessions.items()):
            if now - sdata.get("created_at", 0) > max_age_seconds:
                stale_uids.append(uid)
        for uid in stale_uids:
            sess = self._login_sessions.pop(uid, None)
            if sess:
                c = sess.get("client")
                if c and c.is_connected():
                    try:
                        await c.disconnect()
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)

    # --- IN-BOT AUTHENTICATION FLOW ---

    async def request_phone_code(self, user_id: int, phone: str) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "TELEGRAM_API_ID va TELEGRAM_API_HASH sozlanmagan!"

        # Rate limiting: prevent OTP spam — 60 seconds between requests per user
        import time as _time
        now_ts = _time.monotonic()
        last_req = self._otp_cooldowns.get(user_id, 0.0)
        cooldown_secs = 60
        if now_ts - last_req < cooldown_secs:
            remaining = int(cooldown_secs - (now_ts - last_req))
            return False, f"Juda tez! Iltimos {remaining} soniya kuting va qaytadan urinib ko'ring."

        await self.prune_stale_login_sessions()

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
                        logger.debug("Ignored exception", exc_info=True)

            client = TelegramClient(
                StringSession(""),
                settings.TELEGRAM_API_ID,
                settings.TELEGRAM_API_HASH,
                device_model=DEVICE_MODEL,
                system_version=SYSTEM_VERSION,
                app_version=APP_VERSION,
                lang_code=LANG_CODE,
                system_lang_code=SYSTEM_LANG_CODE,
                use_ipv6=is_ipv6_supported()
            )
            await client.connect()

            sent_code = await client.send_code_request(phone_e164)
            self._login_sessions[user_id] = {
                "client": client,
                "phone": phone_e164,
                "phone_code_hash": sent_code.phone_code_hash,
                "created_at": time.time()
            }
            # Record cooldown start time to prevent OTP spam
            import time as _time
            self._otp_cooldowns[user_id] = _time.monotonic()
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
            from services.security_vault import security_vault
            encrypted_session = security_vault.encrypt_secret(session_str)
            await db_manager.set_setting("telethon_session", encrypted_session)
            try:
                await client.disconnect()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            self._login_sessions.pop(user_id, None)

            # Start listener with new session
            await self.start()
            name = me.first_name if me else "Foydalanuvchi"
            logger.info(f"User {user_id} successfully authenticated Telethon session as: {name}")
            return True, f"Hisob muvaffaqiyatli ulandi: {name}!", "success"

        except SessionPasswordNeededError:
            return False, "2-bosqichli parol (Two-Step Verification) talab qilinadi.", "needs_2fa"
        except PhoneCodeInvalidError:
            return False, "Tasdiqlash kodi noto'g'ri! Iltimos, qaytadan tekshirib kiriting.", "error"
        except PhoneCodeExpiredError:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            self._login_sessions.pop(user_id, None)
            return False, "Tasdiqlash kodi muddati o'tgan! Qaytadan kod so'rang.", "error"
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
            from services.security_vault import security_vault
            encrypted_session = security_vault.encrypt_secret(session_str)
            await db_manager.set_setting("telethon_session", encrypted_session)
            try:
                await client.disconnect()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
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

    async def sign_in_with_code(self, phone: str, code: str, user_id: int = 0) -> Tuple[str, str]:
        """Convenience alias for submit_phone_code. user_id must be non-zero."""
        if user_id == 0:
            logger.warning("sign_in_with_code called with user_id=0 — refusing to submit code to unknown session")
            return "ERROR", "Foydalanuvchi identifikatori noto'g'ri. Qaytadan /login bosing."
        ok, msg, status = await self.submit_phone_code(user_id=user_id, code=code)
        if status == "success":
            return "SUCCESS", msg
        elif status == "needs_2fa":
            return "2FA_REQUIRED", msg
        else:
            return "ERROR", msg

    async def sign_in_with_2fa(self, password: str, user_id: int = 0) -> Tuple[bool, str]:
        """Convenience alias for submit_2fa_password. user_id must be non-zero."""
        if user_id == 0:
            logger.warning("sign_in_with_2fa called with user_id=0 — refusing to submit 2FA to unknown session")
            return False, "Foydalanuvchi identifikatori noto'g'ri. Qaytadan /login bosing."
        return await self.submit_2fa_password(user_id=user_id, password=password)

    # --- CHANNEL RESOLUTION & MONITORING ---

    async def resolve_entity(self, channel_identifier: Union[str, int]):
        if not self.client or not self.client.is_connected() or channel_identifier is None:
            return None

        clean_id = str(channel_identifier).strip()
        clean_id = re.sub(r'^https?:\/\/(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/', '', clean_id, flags=re.IGNORECASE)
        if "/" in clean_id and not clean_id.startswith("+") and not clean_id.startswith("joinchat/"):
            clean_id = clean_id.split("/")[0]

        try:
            if clean_id.startswith("+") or clean_id.startswith("joinchat/"):
                hash_val = clean_id.lstrip("+").replace("joinchat/", "").strip()
                chat_info = None
                try:
                    res = await self.client(CheckChatInviteRequest(hash_val))
                    if hasattr(res, 'chat') and res.chat:
                        return res.chat
                    chat_info = res
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
                try:
                    res = await self.client(ImportChatInviteRequest(hash_val))
                    if hasattr(res, 'chats') and res.chats:
                        return res.chats[0]
                except UserAlreadyParticipantError:
                    target_title = getattr(chat_info, 'title', None)
                    if not target_title and hasattr(chat_info, 'chat'):
                        target_title = getattr(chat_info.chat, 'title', None)
                    try:
                        async for dialog in self.client.iter_dialogs(limit=500):
                            if (dialog.is_channel or dialog.is_group):
                                if target_title and getattr(dialog, 'title', None) == target_title:
                                    return dialog.entity
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                except Exception as e:
                    logger.debug(f"Invite link import result for {hash_val}: {e}")
                return None

            clean_digits = re.sub(r'^(-100|-)+', '', clean_id)
            if clean_digits.isdigit():
                canon_id = int(f"-100{clean_digits}")
                raw_id = int(clean_digits)
                try:
                    return await self.client.get_entity(canon_id)
                except Exception:
                    try:
                        return await self.client.get_entity(raw_id)
                    except Exception:
                        try:
                            async for dialog in self.client.iter_dialogs(limit=200):
                                if dialog.id == canon_id or dialog.id == raw_id or normalize_peer_id(dialog.id) == raw_id:
                                    return dialog.entity
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
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
                # 1. Join and monitor source channel
                cid = None
                if pair.source_id:
                    cid = await self.join_and_monitor_channel(str(pair.source_id))
                if not cid:
                    cid = await self.join_and_monitor_channel(pair.source_channel)
                if cid and not pair.source_id:
                    await db_manager.update_pair_source_id(pair.id, cid)

                # 2. Pre-resolve and cache target channel so Telethon can post without entity lookup errors
                tid = None
                if pair.target_id:
                    tid = await self.join_and_monitor_channel(str(pair.target_id))
                if not tid:
                    tid = await self.join_and_monitor_channel(pair.target_channel)
                if tid and not pair.target_id:
                    formatted_tid = tid if tid < 0 else int(f"-100{tid}")
                    await db_manager.update_pair_target_id(pair.id, formatted_tid)
                    pair.target_id = formatted_tid

                logger.info(f"Monitoring pair #{pair.id}: {pair.source_title or pair.source_channel} ({pair.source_channel}) -> {pair.target_channel}")
            except Exception as e:
                logger.error(f"Error joining/resolving channels for pair {pair.id}: {e}")

        # Trigger background offline catch-up for any missed posts while the bot/PC was offline
        self._spawn_task(self.catch_up_all_active_pairs())

    # --- REAL-TIME EVENT HANDLER ---

    async def _handle_chat_migration(self, old_chat_id: int, new_channel_id: int):
        """
        Updates database and in-memory caches when a basic Telegram group migrates to a supergroup.
        """
        try:
            old_raw = normalize_peer_id(old_chat_id)
            new_supergroup_id = int(f"-100{new_channel_id}") if not str(new_channel_id).startswith("-100") else int(new_channel_id)
            logger.info(f"🔄 Group migration detected: old {old_chat_id} (raw: {old_raw}) -> supergroup {new_supergroup_id}")

            all_pairs = await db_manager.get_all_active_pairs()
            migrated_count = 0
            for p in all_pairs:
                p_src_raw = normalize_peer_id(p.source_id) or normalize_peer_id(p.source_channel)
                if p_src_raw and p_src_raw == old_raw:
                    await db_manager.update_pair_source_id(p.id, new_supergroup_id)
                    p.source_id = new_supergroup_id
                    migrated_count += 1
                p_tgt_raw = normalize_peer_id(p.target_id) or normalize_peer_id(p.target_channel)
                if p_tgt_raw and p_tgt_raw == old_raw:
                    await db_manager.update_pair_target_id(p.id, new_supergroup_id)
                    p.target_id = new_supergroup_id
                    migrated_count += 1

            if migrated_count > 0:
                self.invalidate_pairs_cache()
                logger.info(f"Successfully migrated {migrated_count} channel pair reference(s) to new supergroup ID {new_supergroup_id}")
        except Exception as e:
            logger.error(f"Error handling chat migration from {old_chat_id} to {new_channel_id}: {e}", exc_info=True)

    async def _handle_new_message(self, event: events.NewMessage.Event):
        try:
            message = getattr(event, 'message', None)
            if not message:
                return

            action = getattr(message, 'action', None)
            if action is not None and type(action).__name__ == "MessageActionChatMigrateTo":
                new_channel_id = getattr(action, 'channel_id', None)
                chat = await event.get_chat()
                old_chat_id = getattr(chat, 'id', None) or getattr(event, 'chat_id', None)
                if old_chat_id and new_channel_id:
                    self._spawn_task(self._handle_chat_migration(old_chat_id, new_channel_id))
                return

            if is_ignorable_message(message):
                return

            chat = await event.get_chat()
            if not chat:
                return

            chat_id = getattr(chat, 'id', None)
            event_chat_id = getattr(event, 'chat_id', None)
            chat_username = getattr(chat, 'username', None)
            if chat_username:
                chat_username = chat_username.lower().lstrip("@")

            event_raw_id = normalize_peer_id(event_chat_id)
            chat_raw_id = normalize_peer_id(chat_id)

            # Story Cloner Auto-Publishing Dispatch
            try:
                from services.story_cloner_service import story_cloner_service
                self._spawn_task(story_cloner_service.handle_incoming_channel_message(message, chat))
            except Exception as se:
                logger.debug(f"Story cloner message check skipped: {se}")

            # Match active pairs from memory cache (15s TTL)
            now_t = time.time()
            if now_t - self._active_pairs_cache_time > 15.0 or not self._active_pairs_cache:
                self._active_pairs_cache = await db_manager.get_all_active_pairs()
                self._active_pairs_cache_time = now_t
            active_pairs = self._active_pairs_cache
            matching_pairs: List[ChannelPair] = []
            seen_pair_ids = set()

            for p in active_pairs:
                if not p.is_active or (p.id is not None and p.id in seen_pair_ids):
                    continue
                # Hard architectural security: target cannot be a private user
                t_chan = str(p.target_channel or "").strip()
                if t_chan and not t_chan.startswith("@") and not t_chan.startswith("-100"):
                    if (p.target_id and p.target_id > 0 and not str(p.target_id).startswith("-")) or (not p.target_id and t_chan.isdigit() and len(t_chan) < 10):
                        continue
                clean_src = p.source_channel.lstrip("@").lower().strip()
                pair_src_id = normalize_peer_id(p.source_id)
                clean_src_num = normalize_peer_id(clean_src)

                is_match = False
                if pair_src_id and (pair_src_id == event_raw_id or pair_src_id == chat_raw_id):
                    is_match = True
                elif clean_src_num and (clean_src_num == event_raw_id or clean_src_num == chat_raw_id):
                    is_match = True
                elif chat_username and clean_src == chat_username:
                    is_match = True
                elif p.source_id and (p.source_id == chat_id or p.source_id == event_chat_id):
                    is_match = True

                if is_match:
                    if p.id is not None:
                        seen_pair_ids.add(p.id)
                    matching_pairs.append(p)

            if not matching_pairs:
                return

            # Deduplicate by (user_id, target_key) so a single user never posts duplicates to the same destination,
            # while preventing cross-user pair collisions when multiple users/co-admins share a target channel.
            seen_user_destinations = set()
            unique_matching_pairs: List[ChannelPair] = []
            for p in matching_pairs:
                target_key = normalize_peer_id(p.target_id) if p.target_id else normalize_peer_id(p.target_channel)
                if not target_key:
                    target_key = p.target_channel.lstrip("@").lower().strip()
                dest_key = (p.user_id, target_key)
                if dest_key not in seen_user_destinations:
                    seen_user_destinations.add(dest_key)
                    unique_matching_pairs.append(p)

            if not unique_matching_pairs:
                return

            message = event.message
            if not message:
                return

            logger.info(f"🚀 Dispatching message {message.id} to {len(unique_matching_pairs)} unique destination channels...")

            for pair in unique_matching_pairs:
                if pair.target_id and pair.target_id > 0:
                    if pair.target_channel and (pair.target_channel.startswith("@") or "t.me" in pair.target_channel or pair.target_channel.startswith("-")):
                        corrected_tid = int(f"-100{pair.target_id}")
                        logger.info(f"Auto-normalizing channel target_id {pair.target_id} -> {corrected_tid} for pair #{pair.id}")
                        pair.target_id = corrected_tid
                        self._spawn_task(db_manager.update_pair_target_id(pair.id, corrected_tid))
                    else:
                        logger.warning(f"SECURITY: Skipping pair #{pair.id} with positive user target_id {pair.target_id}")
                        continue
                if message.grouped_id:
                    buffer_key = (pair.id, message.grouped_id)
                    media_handler.album_buffer.add_message(
                        buffer_key,
                        message,
                        lambda k, msgs, p=pair: self._spawn_task(cloner_engine.clone_media_group(msgs, p))
                    )
                else:
                    self._spawn_task(cloner_engine.clone_single_message(message, pair))

        except Exception as e:
            logger.error(f"Error in Telethon _handle_new_message: {e}", exc_info=True)

    async def _handle_message_deleted(self, event: events.MessageDeleted.Event):
        """
        Handles message deletion from monitored source channels.
        Marks destination post as SOLD/CLOSED (preserving channel stats) and deletes active Story.
        """
        try:
            deleted_ids = getattr(event, 'deleted_ids', None) or []
            if not deleted_ids:
                return

            chat = await event.get_chat()
            if not chat:
                return

            chat_username = getattr(chat, 'username', None)
            chat_id = getattr(chat, 'id', None)
            raw_id = normalize_peer_id(chat_id)

            logger.info(f"🗑 Source messages {deleted_ids} deleted in {chat_username or raw_id}. Syncing with destination channels...")

            for del_id in deleted_ids:
                # 1. Fetch matching cloned destination posts
                records = []
                if chat_username:
                    records.extend(await db_manager.get_cloned_messages_by_source(chat_username, del_id))
                if raw_id:
                    records.extend(await db_manager.get_cloned_messages_by_source(str(raw_id), del_id))

                seen_rec_ids = set()
                for rec in records:
                    rec_id = rec.get("id")
                    if not rec_id or rec_id in seen_rec_ids:
                        continue
                    seen_rec_ids.add(rec_id)

                    target_chat = rec.get("target_channel") or rec.get("pair_target_channel")
                    target_msg_id = rec.get("target_msg_id")
                    status = rec.get("status")

                    if target_chat and target_msg_id and status != "sold":
                        try:
                            bot = cloner_engine.bot
                            if bot is None:
                                from bot.bot_instance import create_bot
                                bot = create_bot()
                            sold_tag = "🔴 <b>SOTILDI / YOPILGAN E'LON</b>\n\n"
                            orig_caption = rec.get("last_caption") or ""
                            new_text = sold_tag + orig_caption
                            if rec.get("media_type") == "text":
                                if len(new_text) > 4096:
                                    cap, _ = TextProcessor.fit_caption_limit(new_text, limit=4090)
                                    new_text = cap + "..."
                                await bot.edit_message_text(chat_id=target_chat, message_id=target_msg_id, text=new_text, parse_mode="HTML")
                            else:
                                if len(new_text) > 1024:
                                    cap, _ = TextProcessor.fit_caption_limit(new_text, limit=1020)
                                    new_text = cap + "..."
                                await bot.edit_message_caption(chat_id=target_chat, message_id=target_msg_id, caption=new_text, parse_mode="HTML")
                            logger.info(f"Marked destination post #{target_msg_id} in {target_chat} as SOLD")
                        except Exception as ed_err:
                            logger.debug(f"Notice editing destination msg #{target_msg_id} to sold: {ed_err}")

                        await db_manager.update_cloned_message_status(rec_id, "sold")

                # 2. Delete active Story if one was generated
                try:
                    from services.story_cloner_service import story_cloner_service
                    await story_cloner_service.handle_source_message_deleted(
                        source_channel=chat_username or str(raw_id or ""),
                        source_msg_id=del_id
                    )
                except Exception as se:
                    logger.debug(f"Story delete sync note: {se}")

        except Exception as e:
            logger.error(f"Error in Telethon _handle_message_deleted: {e}", exc_info=True)

    async def _handle_message_edited(self, event: events.MessageEdited.Event):
        """
        Handles message edits from monitored source channels.
        If marked as sold/zalog in source, updates destination post as SOLD.
        Otherwise re-cleans caption and edits destination channel message.
        """
        try:
            message = getattr(event, 'message', None)
            if not message or is_ignorable_message(message):
                return

            chat = await event.get_chat()
            if not chat:
                return

            chat_username = getattr(chat, 'username', None)
            chat_id = getattr(chat, 'id', None)
            raw_id = normalize_peer_id(chat_id)

            text = getattr(message, 'message', '') or ''
            is_sold_keyword = any(w in text.lower() for w in ["sotildi", "zalog olindi", "zalog", "sold", "bron qilindi", "arxive"])

            records = []
            if chat_username:
                records.extend(await db_manager.get_cloned_messages_by_source(chat_username, message.id))
            if raw_id:
                records.extend(await db_manager.get_cloned_messages_by_source(str(raw_id), message.id))

            seen_rec_ids = set()
            for rec in records:
                rec_id = rec.get("id")
                if not rec_id or rec_id in seen_rec_ids:
                    continue
                seen_rec_ids.add(rec_id)

                target_chat = rec.get("target_channel") or rec.get("pair_target_channel")
                target_msg_id = rec.get("target_msg_id")
                if not target_chat or not target_msg_id:
                    continue

                bot = cloner_engine.bot
                if bot is None:
                    from bot.bot_instance import create_bot
                    bot = create_bot()

                if is_sold_keyword and rec.get("status") != "sold":
                    try:
                        sold_tag = "🔴 <b>SOTILDI / YOPILGAN E'LON</b>\n\n"
                        orig_caption = rec.get("last_caption") or text
                        new_text = sold_tag + orig_caption
                        if rec.get("media_type") == "text":
                            if len(new_text) > 4096:
                                cap, _ = TextProcessor.fit_caption_limit(new_text, limit=4090)
                                new_text = cap + "..."
                            await bot.edit_message_text(chat_id=target_chat, message_id=target_msg_id, text=new_text, parse_mode="HTML")
                        else:
                            if len(new_text) > 1024:
                                cap, _ = TextProcessor.fit_caption_limit(new_text, limit=1020)
                                new_text = cap + "..."
                            await bot.edit_message_caption(chat_id=target_chat, message_id=target_msg_id, caption=new_text, parse_mode="HTML")
                    except Exception as ed_err:
                        logger.debug(f"Notice updating edited post as sold: {ed_err}")
                    await db_manager.update_cloned_message_status(rec_id, "sold")

                    # Also delete story if marked as sold
                    try:
                        from services.story_cloner_service import story_cloner_service
                        await story_cloner_service.handle_source_message_deleted(
                            source_channel=chat_username or str(raw_id or ""),
                            source_msg_id=message.id
                        )
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                else:
                    # Regular text edit
                    try:
                        pair = await db_manager.get_pair_by_id(rec["pair_id"])
                        if pair:
                            processed = await cloner_engine.process_post_text(text, pair)
                            if processed:
                                if rec.get("media_type") == "text":
                                    await bot.edit_message_text(chat_id=target_chat, message_id=target_msg_id, text=processed, parse_mode="HTML")
                                else:
                                    if len(processed) <= 1024:
                                        await bot.edit_message_caption(chat_id=target_chat, message_id=target_msg_id, caption=processed, parse_mode="HTML")
                    except Exception as ed_err:
                        logger.debug(f"Notice editing cloned message text: {ed_err}")

        except Exception as e:
            logger.error(f"Error in Telethon _handle_message_edited: {e}", exc_info=True)

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
        existing_task = self.active_history_tasks.get(pair.id)
        if existing_task is not None and existing_task is not current_task and not existing_task.done():
            logger.warning(f"History clone already in progress for pair #{pair.id}. Ignoring duplicate trigger.")
            return {"total": 0, "cloned": 0, "failed": 0, "status": "already_running"}

        self.active_history_tasks[pair.id] = current_task

        entity = None
        if pair.source_id:
            try:
                entity = await self.resolve_entity(pair.source_id)
            except Exception:
                entity = None
        if not entity:
            entity = await self.resolve_entity(pair.source_channel)
        if not entity:
            self.active_history_tasks.pop(pair.id, None)
            return {"total": 0, "cloned": 0, "failed": 0, "status": "source_not_found"}

        logger.info(f"Starting history clone for pair #{pair.id}: {pair.source_channel} -> {pair.target_channel} (limit: {limit or 'ALL'})")

        cloned_count = 0
        failed_count = 0

        try:
            # 1. If limit is specified (e.g. 10, 30, 50, 100):
            # Collect the latest `limit` logical posts (albums as a single post item, single messages as single item)
            if limit is not None:
                logical_posts: List[Union[TelethonMessage, List[TelethonMessage]]] = []
                current_album: List[TelethonMessage] = []
                current_group_id = None
                
                # Fetch messages from newest backwards (fetch enough raw messages to fill `limit` logical posts)
                raw_fetch_limit = max(limit * 30, 300)
                async for msg in self.client.iter_messages(entity, limit=raw_fetch_limit, reverse=False):
                    if is_ignorable_message(msg):
                        continue
                    if msg.grouped_id:
                        if current_group_id is None:
                            current_album = [msg]
                            current_group_id = msg.grouped_id
                        elif current_group_id == msg.grouped_id:
                            current_album.append(msg)
                        else:
                            if current_album:
                                current_album.reverse()
                                is_album_cloned = all([await db_manager.is_message_cloned(pair.id, m.id) for m in current_album])
                                if not is_album_cloned:
                                    logical_posts.append(current_album)
                                    if len(logical_posts) >= limit:
                                        current_album = []
                                        break
                            current_album = [msg]
                            current_group_id = msg.grouped_id
                    else:
                        if current_album:
                            current_album.reverse()
                            is_album_cloned = all([await db_manager.is_message_cloned(pair.id, m.id) for m in current_album])
                            if not is_album_cloned:
                                logical_posts.append(current_album)
                                if len(logical_posts) >= limit:
                                    current_album = []
                                    break
                            current_album = []
                            current_group_id = None
                        if not await db_manager.is_message_cloned(pair.id, msg.id):
                            logical_posts.append(msg)
                            if len(logical_posts) >= limit:
                                break

                if current_album and len(logical_posts) < limit:
                    current_album.reverse()
                    is_album_cloned = all([await db_manager.is_message_cloned(pair.id, m.id) for m in current_album])
                    if not is_album_cloned:
                        logical_posts.append(current_album)

                # Reverse logical posts so they are sent in chronological order (oldest to newest)
                logical_posts.reverse()
                total_target = len(logical_posts)

                if total_target == 0:
                    if progress_callback:
                        try:
                            await progress_callback(0, 0, "all_cloned")
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                    return {
                        "total": 0,
                        "cloned": 0,
                        "failed": 0,
                        "status": "all_cloned"
                    }

                for idx, item in enumerate(logical_posts, 1):
                    try:
                        if isinstance(item, list):
                            uncloned_items = [m for m in item if not await db_manager.is_message_cloned(pair.id, m.id)]
                            if not uncloned_items:
                                continue
                            ok = await cloner_engine.clone_media_group(uncloned_items, pair)
                            if ok:
                                cloned_count += 1
                            else:
                                failed_count += 1
                        else:
                            if await db_manager.is_message_cloned(pair.id, item.id):
                                continue
                            ok = await cloner_engine.clone_single_message(item, pair)
                            if ok:
                                cloned_count += 1
                            else:
                                failed_count += 1
                    except FloodWaitError as fe:
                        logger.warning(f"FloodWait in history clone: waiting {fe.seconds}s")
                        await asyncio.sleep(fe.seconds + 1)
                        try:
                            if isinstance(item, list):
                                uncloned_items = [m for m in item if not await db_manager.is_message_cloned(pair.id, m.id)]
                                if uncloned_items:
                                    ok = await cloner_engine.clone_media_group(uncloned_items, pair)
                                    if ok:
                                        cloned_count += 1
                                    else:
                                        failed_count += 1
                            else:
                                if not await db_manager.is_message_cloned(pair.id, item.id):
                                    ok = await cloner_engine.clone_single_message(item, pair)
                                    if ok:
                                        cloned_count += 1
                                    else:
                                        failed_count += 1
                        except Exception:
                            failed_count += 1
                    except Exception as e:
                        logger.error(f"Error cloning history item: {e}")
                        failed_count += 1

                    if progress_callback:
                        try:
                            await progress_callback(idx, total_target, "running")
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)

                    await asyncio.sleep(0.5)

                final_status = "completed" if failed_count == 0 else ("failed" if cloned_count == 0 else "completed")
                if progress_callback:
                    try:
                        await progress_callback(cloned_count, total_target, final_status)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)

                return {
                    "total": total_target,
                    "cloned": cloned_count,
                    "failed": failed_count,
                    "status": final_status
                }

            else:
                # 2. Limit is None: clone ALL messages in chronological order (from oldest to newest)
                total_msgs = await self.client.get_messages(entity, limit=0)
                raw_val = getattr(total_msgs, "total", None)
                total_raw = raw_val if raw_val is not None else 0
                if total_raw == 0:
                    if progress_callback:
                        try:
                            await progress_callback(0, 0, "all_cloned")
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                    return {
                        "total": 0,
                        "cloned": 0,
                        "failed": 0,
                        "status": "all_cloned"
                    }
                
                logical_processed = 0
                pending_album: List[TelethonMessage] = []
                last_group_id = None

                async def flush_album_all():
                    nonlocal cloned_count, failed_count, logical_processed
                    if not pending_album:
                        return
                    album_items = list(pending_album)
                    pending_album.clear()
                    
                    uncloned_items = [m for m in album_items if not await db_manager.is_message_cloned(pair.id, m.id)]
                    if not uncloned_items:
                        return

                    logical_processed += 1
                    try:
                        ok = await cloner_engine.clone_media_group(uncloned_items, pair)
                        if ok:
                            cloned_count += 1
                        else:
                            failed_count += 1
                    except FloodWaitError as e:
                        wait_sec = min(e.seconds, 120)
                        logger.warning(f"FloodWaitError in clone_history album: sleeping {wait_sec}s")
                        await asyncio.sleep(wait_sec)
                        try:
                            ok = await cloner_engine.clone_media_group(uncloned_items, pair)
                            if ok:
                                cloned_count += 1
                            else:
                                failed_count += 1
                        except Exception:
                            failed_count += 1
                    except Exception as e:
                        logger.error(f"Error cloning history album: {e}")
                        failed_count += 1

                async for msg in self.client.iter_messages(entity, reverse=True):
                    if is_ignorable_message(msg):
                        continue
                    if msg.grouped_id:
                        if last_group_id is None or last_group_id == msg.grouped_id:
                            pending_album.append(msg)
                            last_group_id = msg.grouped_id
                        else:
                            await flush_album_all()
                            pending_album.append(msg)
                            last_group_id = msg.grouped_id
                    else:
                        await flush_album_all()
                        last_group_id = None

                        if await db_manager.is_message_cloned(pair.id, msg.id):
                            continue

                        logical_processed += 1
                        try:
                            ok = await cloner_engine.clone_single_message(msg, pair)
                            if ok:
                                cloned_count += 1
                            else:
                                failed_count += 1
                        except FloodWaitError as e:
                            wait_sec = min(e.seconds, 120)
                            logger.warning(f"FloodWaitError in clone_history: sleeping {wait_sec}s")
                            await asyncio.sleep(wait_sec)
                            try:
                                ok = await cloner_engine.clone_single_message(msg, pair)
                                if ok:
                                    cloned_count += 1
                                else:
                                    failed_count += 1
                            except Exception:
                                failed_count += 1
                        except Exception as e:
                            logger.error(f"Error cloning message #{msg.id}: {e}")
                            failed_count += 1

                    if progress_callback and (logical_processed % 5 == 0 or logical_processed == 1):
                        try:
                            effective_total = max(total_raw, logical_processed)
                            await progress_callback(logical_processed, effective_total, "running")
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)

                    await asyncio.sleep(0.3)

                await flush_album_all()

                if logical_processed == 0 and cloned_count == 0 and failed_count == 0:
                    final_status = "all_cloned"
                elif cloned_count == 0 and failed_count > 0:
                    final_status = "failed"
                else:
                    final_status = "completed"

                if progress_callback:
                    try:
                        await progress_callback(cloned_count, logical_processed or total_raw, final_status)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)

                return {
                    "total": logical_processed,
                    "cloned": cloned_count,
                    "failed": failed_count,
                    "status": final_status
                }

        except asyncio.CancelledError:
            logger.info(f"History clone for pair #{pair.id} cancelled by user.")
            if progress_callback:
                try:
                    await progress_callback(cloned_count, cloned_count + failed_count, "cancelled")
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Error during history clone: {e}", exc_info=True)
            if progress_callback:
                try:
                    await progress_callback(cloned_count, cloned_count + failed_count, "failed")
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            return {
                "total": cloned_count + failed_count,
                "cloned": cloned_count,
                "failed": failed_count,
                "status": f"error: {str(e)[:50]}"
            }
        finally:
            self.active_history_tasks.pop(pair.id, None)

    # --- OFFLINE GAP CATCH-UP ENGINE ---

    async def catch_up_pair_messages(
        self,
        pair: ChannelPair,
        max_messages: int = 150
    ) -> Dict[str, Any]:
        """
        Catches up on posts missed while the bot / host machine was offline or disconnected.
        Retrieves messages with id > last_seen_msg_id in chronological order (oldest to newest),
        batches media groups (albums) properly, checks deduplication, respects rate limits,
        and safely updates pair's last_seen_msg_id.
        """
        if not self.client or not self.client.is_connected() or not await self.client.is_user_authorized():
            return {"status": "not_connected", "caught_up": 0, "failed": 0}

        if not pair.is_active or not getattr(pair, "auto_catchup", True):
            return {"status": "disabled", "caught_up": 0, "failed": 0}

        if pair.id in self._catchup_in_progress:
            logger.info(f"Catch-up already running for pair #{pair.id}. Skipping concurrent run.")
            return {"status": "already_running", "caught_up": 0, "failed": 0}

        self._catchup_in_progress.add(pair.id)
        caught_up = 0
        failed = 0
        latest_seen_id = None

        try:
            # 1. Resolve source channel entity
            entity = None
            if pair.source_id:
                try:
                    entity = await self.resolve_entity(pair.source_id)
                except Exception:
                    entity = None
            if not entity:
                entity = await self.resolve_entity(pair.source_channel)
            if not entity:
                logger.warning(f"Catch-up cannot resolve source entity for pair #{pair.id} ({pair.source_channel})")
                return {"status": "source_not_found", "caught_up": 0, "failed": 0}

            # 2. Inspect latest message in source channel
            latest_msgs = await self.client.get_messages(entity, limit=1)
            if not latest_msgs:
                return {"status": "channel_empty", "caught_up": 0, "failed": 0}

            current_head_id = latest_msgs[0].id
            last_id = await db_manager.get_effective_last_source_msg_id(pair.id)

            # 3. If pair has never tracked any messages (brand new pair or fresh DB):
            # Establish baseline to current head without dumping all historical years of posts.
            if last_id is None:
                await db_manager.update_pair_last_seen_msg_id(pair.id, current_head_id)
                logger.info(f"Established initial catch-up baseline for pair #{pair.id}: last_seen_msg_id={current_head_id}")
                return {"status": "baseline_established", "caught_up": 0, "failed": 0, "last_id": current_head_id}

            # 4. Check if there is an actual gap
            if current_head_id <= last_id:
                return {"status": "up_to_date", "caught_up": 0, "failed": 0, "last_id": last_id}

            gap_size = current_head_id - last_id
            logger.info(
                f"Offline gap detected for pair #{pair.id} ({pair.source_channel} -> {pair.target_channel}): "
                f"last_seen={last_id}, current_head={current_head_id} (~{gap_size} potential posts). "
                f"Catching up (max {max_messages})..."
            )

            # 5. Fetch missed messages chronologically (oldest to newest) where id > min_id
            pending_album: List[TelethonMessage] = []
            last_group_id = None
            highest_id_processed = last_id

            async def flush_album():
                nonlocal caught_up, failed, highest_id_processed
                if not pending_album:
                    return
                album_items = list(pending_album)
                pending_album.clear()

                uncloned_items = []
                for item in album_items:
                    if not await db_manager.is_message_cloned(pair.id, item.id):
                        uncloned_items.append(item)

                if not uncloned_items:
                    return

                try:
                    ok = await cloner_engine.clone_media_group(uncloned_items, pair)
                    if ok:
                        caught_up += len(uncloned_items)
                    else:
                        failed += len(uncloned_items)
                except FloodWaitError as fe:
                    wait_sec = min(fe.seconds, 120)
                    logger.warning(f"FloodWait in catch-up album: sleeping {wait_sec}s")
                    await asyncio.sleep(wait_sec)
                    try:
                        ok = await cloner_engine.clone_media_group(uncloned_items, pair)
                        if ok:
                            caught_up += len(uncloned_items)
                        else:
                            failed += len(uncloned_items)
                    except Exception:
                        failed += len(uncloned_items)
                except Exception as e:
                    logger.error(f"Error in catch-up clone_media_group: {e}")
                    failed += len(uncloned_items)

                for item in album_items:
                    if item.id > highest_id_processed:
                        highest_id_processed = item.id
                await db_manager.update_pair_last_seen_msg_id(pair.id, highest_id_processed)
                await asyncio.sleep(1.2)

            async for msg in self.client.iter_messages(entity, min_id=last_id, limit=max_messages, reverse=True):
                if is_ignorable_message(msg):
                    if msg.id > highest_id_processed:
                        highest_id_processed = msg.id
                        await db_manager.update_pair_last_seen_msg_id(pair.id, highest_id_processed)
                    continue

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

                    if await db_manager.is_message_cloned(pair.id, msg.id):
                        if msg.id > highest_id_processed:
                            highest_id_processed = msg.id
                            await db_manager.update_pair_last_seen_msg_id(pair.id, highest_id_processed)
                        continue

                    try:
                        ok = await cloner_engine.clone_single_message(msg, pair)
                        if ok:
                            caught_up += 1
                        else:
                            failed += 1
                    except FloodWaitError as fe:
                        wait_sec = min(fe.seconds, 120)
                        logger.warning(f"FloodWait in catch-up single msg: sleeping {wait_sec}s")
                        await asyncio.sleep(wait_sec)
                        try:
                            ok = await cloner_engine.clone_single_message(msg, pair)
                            if ok:
                                caught_up += 1
                            else:
                                failed += 1
                        except Exception:
                            failed += 1
                    except Exception as e:
                        logger.error(f"Error in catch-up clone_single_message #{msg.id}: {e}")
                        failed += 1

                    if msg.id > highest_id_processed:
                        highest_id_processed = msg.id
                    await db_manager.update_pair_last_seen_msg_id(pair.id, highest_id_processed)
                    await asyncio.sleep(1.2)

            await flush_album()
            latest_seen_id = highest_id_processed
            logger.info(f"Catch-up completed for pair #{pair.id}: {caught_up} caught up, {failed} failed. New last_seen_msg_id={latest_seen_id}")

            return {
                "status": "completed",
                "caught_up": caught_up,
                "failed": failed,
                "last_id": latest_seen_id
            }

        except asyncio.CancelledError:
            logger.info(f"Catch-up for pair #{pair.id} cancelled.")
            raise
        except (AuthKeyUnregisteredError, Exception) as e:
            if isinstance(e, AuthKeyUnregisteredError) or "AUTH_KEY_UNREGISTERED" in str(e).upper():
                logger.error("CRITICAL: AuthKeyUnregisteredError in catch_up_pair_messages. Halting listener.")
                await self._handle_auth_revoked()
                return {
                    "status": "auth_revoked",
                    "caught_up": caught_up,
                    "failed": failed
                }
            logger.error(f"Error during catch-up for pair #{pair.id}: {e}", exc_info=True)
            return {
                "status": f"error: {str(e)[:60]}",
                "caught_up": caught_up,
                "failed": failed
            }
        finally:
            self._catchup_in_progress.discard(pair.id)

    async def catch_up_all_active_pairs(self, max_messages: int = 150) -> Dict[int, Any]:
        """
        Runs catch-up across all active channel pairs that have auto_catchup enabled.
        Safe for background invocation on bot startup and MTProto reconnection.
        """
        if not self.is_connected():
            logger.debug("Catch-up skipped: Telethon client not connected.")
            return {}

        active_pairs = await db_manager.get_all_active_pairs()
        logger.info(f"Running background offline gap catch-up for {len(active_pairs)} active pairs...")
        results = {}

        for pair in active_pairs:
            if not getattr(pair, "auto_catchup", True):
                continue
            try:
                res = await self.catch_up_pair_messages(pair, max_messages=max_messages)
                results[pair.id] = res
                if res.get("caught_up", 0) > 0:
                    logger.info(f"Pair #{pair.id} catch-up delivered {res['caught_up']} missed posts.")
                await asyncio.sleep(1.0)
            except Exception as e:
                logger.error(f"Error catching up pair #{pair.id}: {e}")

        logger.info("Background offline gap catch-up finished across all pairs.")
        return results

telethon_listener = TelethonListener()
