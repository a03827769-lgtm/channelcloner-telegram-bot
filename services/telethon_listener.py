import asyncio
import html
import inspect
import os
import logging
import re
import time
from collections import OrderedDict
from typing import Optional, List, Dict, Callable, Any, Tuple, Union, Iterable
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    Channel,
    Chat,
    User,
    Message as TelethonMessage,
    MessageService,
    MessageEmpty,
    UpdateNewMessage,
    UpdateNewChannelMessage,
    MessageActionChatMigrateTo,
    MessageActionChannelMigrateFrom,
    ChatInviteAlready,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.functions.updates import GetStateRequest
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PhoneNumberBannedError,
    PasswordHashInvalidError,
    UserAlreadyParticipantError,
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    UnauthorizedError,
)
from config.settings import settings
from database.db_manager import db_manager
from database.models import ChannelPair
from services.cache_manager import cache_manager
from services.cloner_engine import cloner_engine, extract_message_html
from services.media_handler import media_handler
from services.phone_utils import normalize_phone_number, mask_phone_number
from services.text_processor import TextProcessor

logger = logging.getLogger(__name__)

# Professional Branding metadata for Telegram Sessions
DEVICE_MODEL = "Klonla Bot Server"
SYSTEM_VERSION = "Linux Server 64bit"
APP_VERSION = "KlonlaBot Pro v3.0"
LANG_CODE = "uz"
SYSTEM_LANG_CODE = "uz-UZ"

# Connection supervision tuning
RECONNECT_STEP_TIMEOUT = 30.0        # upper bound for each connect/authorize/get_me step
SUPERVISOR_PROBE_INTERVAL = 40.0     # seconds between active GetState probes
PROBE_FAILURES_BEFORE_REBUILD = 2    # consecutive failed probes before the client is replaced by a fresh one
OTP_COOLDOWN_SECONDS = 60            # minimum interval between two login codes requested by one admin
CENTRAL_SESSION_DISABLED_KEY = "central_session_disabled"

# Real-time dispatch tuning
PAIRS_CACHE_TTL = 15.0               # active pairs are re-read from the database at most this often
PAIR_WORKER_IDLE_SECONDS = 120.0     # an idle per-pair delivery worker exits after this long
ALBUM_ASSEMBLY_TIMEOUT = 60.0        # a queued album gives up waiting for the media buffer after this long
OWN_POST_SETTLE_SECONDS = 3.0        # time for the engine to record a clone before a loop check is trusted
GENERAL_TOPIC_ID = 1                 # forum "General" topic (its messages carry no topic reply header)

# Entity resolution caches (StringSession keeps no entities across restarts)
ENTITY_CACHE_TTL = 3600.0
ENTITY_CACHE_MAX = 2000
DIALOGS_INDEX_TTL = 1800.0
ENTITY_PRUNE_THRESHOLD = 2000        # session entity rows kept before pruning to the pair endpoints

# Shutdown budget (the Docker stop grace period is 20 s)
SHUTDOWN_DRAIN_TIMEOUT = 8.0
SHUTDOWN_CANCEL_TIMEOUT = 3.0

SOLD_TAG = "🔴 <b>SOTILDI / YOPILGAN E'LON</b>\n\n"

SESSION_REVOKED_ALERT = (
    "⚠️ <b>MTProto sessiyasi bekor qilindi!</b>\n\n"
    "Markaziy Telegram hisobining sessiyasi Telegram tomonidan bekor qilindi (boshqa qurilmadan chiqarilgan "
    "yoki hisob cheklangan). Kanallarni real vaqtda kuzatish to'xtadi.\n\n"
    "<i>Admin botdagi «MTProto Hisob» bo'limi orqali hisobni qaytadan ulang.</i>"
)

# A pair whose source the central account can no longer read (kicked, banned, or the chat became private)
# is reported once per incident; the app_settings flag is cleared as soon as the source is readable again.
SOURCE_ACCESS_LOST_KEY_PREFIX = "source_access_lost:"
SOURCE_ACCESS_LOST_OWNER_TEXT = (
    "⚠️ <b>Manbaga kirish yo'qoldi</b> (juftlik #{pair_id})\n\n"
    "Bot «{source}» manbasini o'qiy olmayapti: manba uni chiqarib yuborgan yoki bloklagan, yoki manba yopiq "
    "bo'lib qolgan. Shu sababli yangi postlar ko'chirilmayapti.\n\n"
    "<i>Manba adminlaridan cheklovni olib tashlashni so'rang yoki /cloner menyusida boshqa manba tanlang.</i>"
)
SOURCE_ACCESS_LOST_ADMIN_TEXT = (
    "⚠️ <b>Manbaga kirish yo'qoldi</b>\n\n"
    "Juftlik #{pair_id} (egasi <code>{owner}</code>): markaziy MTProto akkaunt «{source}» manbasini o'qiy "
    "olmayapti ({error}). Juftlik egasiga xabar yuborildi."
)


def is_session_revoked_error(err: BaseException) -> bool:
    """True for errors meaning the stored authorization is permanently unusable (a new login is required).
    Transient failures (network, flood waits, Telegram 5xx) must never be treated as revocation."""
    if isinstance(err, SessionPasswordNeededError):
        return False
    if isinstance(err, (AuthKeyUnregisteredError, AuthKeyDuplicatedError, UnauthorizedError)):
        return True
    text = str(err).upper()
    return any(marker in text for marker in ("AUTH_KEY_UNREGISTERED", "AUTH_KEY_DUPLICATED", "SESSION_REVOKED", "USER_DEACTIVATED"))

# Edits containing these phrases mark a listing as sold/closed. Bare "zalog" is deliberately
# excluded: rental listings routinely mention the deposit ("zalog 500$"). Bare English "sold" is excluded
# too: it appears in ordinary news text ("tickets sold", "sold 1000 units").
SOLD_KEYWORDS_RE = re.compile(
    r"\b(sotildi|sotilgan|zalog\s+olindi|bron\s+qilindi|arxivga\s+olindi|sold\s+out|продано|сдано)\b",
    re.IGNORECASE,
)

_TG_EMOJI_RE = re.compile(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', re.DOTALL | re.IGNORECASE)
_TG_EMOJI_TAG_RE = re.compile(r'</?tg-emoji\b[^>]*>', re.IGNORECASE)
_TME_PREFIX_RE = re.compile(r'^https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/', re.IGNORECASE)
_USERNAME_RE = re.compile(r'[A-Za-z][A-Za-z0-9_]{3,31}')
_PUBLIC_TARGET_RE = re.compile(r'@[A-Za-z][A-Za-z0-9_]{3,31}')
_MARKED_CHANNEL_ID_RE = re.compile(r'-100\d{5,}')

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
    """True for service notifications (pin, join, leave, ...) and for messages with neither text nor
    media: they carry nothing to clone."""
    if isinstance(msg, (MessageService, MessageEmpty)):
        return True
    if getattr(msg, 'action', None) is not None:
        return True
    has_text = bool(getattr(msg, 'message', None) or getattr(msg, 'text', None))
    return not has_text and not getattr(msg, 'media', None)

def session_uses_ipv6(session: StringSession) -> bool:
    """Connect with the address family the session was created with. Switching families makes Telethon
    fall back to DC 2 with the stored auth key, which fails for accounts homed on any other DC.
    TELETHON_USE_IPV6=0/1 overrides the detection explicitly."""
    override = os.getenv("TELETHON_USE_IPV6")
    if override is not None and override.strip():
        return override.strip().lower() in ("1", "true", "yes")
    try:
        return ":" in (session.server_address or "")
    except Exception:
        return False


def strip_tg_emoji(text: str) -> str:
    """Replaces <tg-emoji> custom emoji with their plain fallback emoji (the Bot API rejects them for bots)."""
    if not text:
        return text
    return _TG_EMOJI_TAG_RE.sub('', _TG_EMOJI_RE.sub(r'\1', text))


def channel_username(reference: Any) -> Optional[str]:
    """Lower-case public username from '@name', 'name' or a t.me link; None for ids and invite links."""
    text = _TME_PREFIX_RE.sub('', str(reference or "").strip()).lstrip("@")
    if "/" in text:
        text = text.split("/")[0]
    return text.lower() if _USERNAME_RE.fullmatch(text) else None


def is_invite_reference(reference: Any) -> bool:
    text = _TME_PREFIX_RE.sub('', str(reference or "").strip())
    return text.startswith("+") or text.lower().startswith("joinchat/")


def message_topic_id(message: Any) -> int:
    """Forum topic of a message: the topic root id from its reply header, or the General topic (id 1),
    whose messages carry no forum reply header."""
    reply_to = getattr(message, "reply_to", None)
    if reply_to is not None and getattr(reply_to, "forum_topic", False) is True:
        top = getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)
        if isinstance(top, int) and top > 0:
            return top
    return GENERAL_TOPIC_ID


def message_in_topic(message: Any, topic_id: Optional[int]) -> bool:
    """True when the pair has no source topic filter or the message belongs to that forum topic."""
    if not topic_id:
        return True
    try:
        return message_topic_id(message) == int(topic_id)
    except (TypeError, ValueError):
        return True


def topic_iter_kwargs(topic_id: Optional[int]) -> Dict[str, Any]:
    """iter_messages() arguments restricting a history/catch-up scan to one forum topic. The General topic is
    not a reply thread, so it is filtered client-side instead."""
    try:
        topic = int(topic_id) if topic_id else 0
    except (TypeError, ValueError):
        topic = 0
    return {"reply_to": topic} if topic > GENERAL_TOPIC_ID else {}


class _CloneUnit:
    """One post waiting in a pair's delivery queue: a single message, or an album whose items are still being
    collected by the media buffer (album_future resolves with the messages once the buffer flushes)."""
    __slots__ = ("pair", "message", "album_key", "album_future", "chat_raw_id", "verify_not_own", "enqueued_at")

    def __init__(
        self,
        pair: ChannelPair,
        message: Any = None,
        album_key: Any = None,
        album_future: Optional[asyncio.Future] = None,
        chat_raw_id: Optional[int] = None,
        verify_not_own: bool = False,
        enqueued_at: float = 0.0,
    ):
        self.pair = pair
        self.message = message
        self.album_key = album_key
        self.album_future = album_future
        self.chat_raw_id = chat_raw_id
        self.verify_not_own = verify_not_own
        self.enqueued_at = enqueued_at


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
        # Indexes derived from the active pairs cache (rebuilt with it)
        self._pairs_by_source_id: Dict[int, List[ChannelPair]] = {}
        self._pairs_by_source_name: Dict[str, List[ChannelPair]] = {}
        self._target_raw_ids: set[int] = set()
        # OTP rate limiting: track last request time per user_id (60-second cooldown)
        self._otp_cooldowns: Dict[int, float] = {}
        # Track ongoing offline catch-up tasks per pair to avoid duplicates
        self._catchup_in_progress: set[int] = set()
        # Start/restart bookkeeping for the self-healing supervisor
        self._start_attempts = 0
        self._restart_pending = False
        self._shutdown = False
        # Per-pair sequential delivery (keeps target order equal to source order, bounds concurrency)
        self._pair_queues: Dict[int, asyncio.Queue] = {}
        self._pair_workers: Dict[int, asyncio.Task] = {}
        self._album_units: Dict[Any, asyncio.Future] = {}
        # Resolved entities with their access hash: raw id -> (monotonic time, entity)
        self._entity_cache: "OrderedDict[int, Tuple[float, Any]]" = OrderedDict()
        self._username_index: Dict[str, int] = {}
        self._invite_index: Dict[str, int] = {}
        self._dialogs_index: Optional[Dict[int, Any]] = None
        self._dialogs_index_time: float = 0.0
        self._dialogs_lock = asyncio.Lock()
        # Last source edit handled per (chat, message): MessageEdited also fires for reaction/view updates
        self._handled_edits: "OrderedDict[Tuple[int, int], Any]" = OrderedDict()
        # Bot used to alert super admins (the admin bot when configured); set by run.py
        self._alert_bot: Optional[Any] = None

    async def _handle_auth_revoked(self):
        """Cleanly halts the MTProto listener, purges the invalid revoked session and alerts the super admins"""
        self._is_running = False
        self._cached_me = None
        client, self.client = self.client, None
        if client:
            self._unregister_event_handlers(client)
            await self._safe_disconnect(client)
        self._reset_dispatch_state()
        self._clear_entity_caches()
        await db_manager.delete_setting("telethon_session")
        settings.TELETHON_SESSION = None
        logger.warning("Revoked Telethon session purged from database to prevent connection spam.")
        self._spawn_task(self._alert_super_admins(SESSION_REVOKED_ALERT))

    def set_alert_bot(self, bot: Any) -> None:
        """Bot used to alert the super admins about listener problems (the admin bot when one is configured)."""
        self._alert_bot = bot

    async def _alert_super_admins(self, text: str) -> None:
        """Best-effort alert to every super admin; failures are logged, never raised."""
        bot = self._alert_bot or cloner_engine.bot
        admin_ids = sorted(settings.admin_ids)
        if bot is None or not admin_ids:
            logger.error(f"Super admin alert could not be delivered (no bot or no admin ids configured): {text}")
            return
        delivered = 0
        for admin_id in admin_ids:
            try:
                await asyncio.wait_for(
                    bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML"),
                    timeout=RECONNECT_STEP_TIMEOUT
                )
                delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Could not alert super admin {admin_id}: {e!r}")
        if not delivered:
            logger.error(f"Super admin alert could not be delivered to anyone: {text}")

    async def _report_source_access_lost(self, pair: ChannelPair, error: BaseException) -> None:
        """Tells the pair owner and the super admins - once per incident - that the central account can no
        longer read the pair's source, so nobody waits for posts that will never be cloned."""
        key = f"{SOURCE_ACCESS_LOST_KEY_PREFIX}{pair.id}"
        try:
            if await db_manager.get_setting(key):
                return
            await db_manager.set_setting(key, str(int(time.time())))
        except Exception:
            logger.warning(f"Could not record the lost source of pair #{pair.id}", exc_info=True)
            return
        source = html.escape(str(pair.source_title or pair.source_channel or pair.source_id or "?"))
        bot = cloner_engine.bot
        if bot is not None and pair.user_id:
            try:
                await asyncio.wait_for(
                    bot.send_message(
                        chat_id=pair.user_id,
                        text=SOURCE_ACCESS_LOST_OWNER_TEXT.format(pair_id=pair.id, source=source),
                        parse_mode="HTML",
                    ),
                    timeout=RECONNECT_STEP_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Could not tell the owner of pair #{pair.id} about the lost source: {e!r}")
        await self._alert_super_admins(SOURCE_ACCESS_LOST_ADMIN_TEXT.format(
            pair_id=pair.id, owner=pair.user_id, source=source, error=html.escape(str(error)[:120])))

    async def _clear_source_access_lost(self, pair: ChannelPair) -> None:
        """The source is readable again: a later loss of access is reported anew."""
        key = f"{SOURCE_ACCESS_LOST_KEY_PREFIX}{pair.id}"
        try:
            if await db_manager.get_setting(key):
                await db_manager.delete_setting(key)
                logger.info(f"Source of pair #{pair.id} is readable again.")
        except Exception:
            logger.debug(f"Could not clear the lost-source flag of pair #{pair.id}", exc_info=True)

    def invalidate_pairs_cache(self):
        """Invalidates the in-memory active channel pairs cache"""
        self._active_pairs_cache_time = 0.0
        self._active_pairs_cache = []

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
        """The central listener session: the one stored by the admin login, or TELETHON_SESSION from .env.

        Customers' own accounts (user_sessions, used by the Story feature) are deliberately never used here:
        running the platform listener on a customer's personal account would make it join every tenant's
        channels and let an admin logout kill that customer's session."""
        from services.security_vault import security_vault
        raw_session = await db_manager.get_setting("telethon_session")
        if not raw_session and settings.TELETHON_SESSION:
            if await db_manager.get_setting(CENTRAL_SESSION_DISABLED_KEY) == "1":
                logger.warning("TELETHON_SESSION from .env ignored: the central account was logged out from the admin bot.")
                return None
            raw_session = settings.TELETHON_SESSION
        if not raw_session:
            return None
        if raw_session.startswith("enc:"):
            decrypted = security_vault.decrypt_secret(raw_session)
            if decrypted:
                return decrypted
            logger.error("Stored central MTProto session could not be decrypted (ENCRYPTION_KEY changed?). Log in again from the admin bot.")
            return None
        return raw_session

    async def start(self):
        """Starts Telethon MTProto listener and connection supervisor"""
        self._shutdown = False
        await self.start_listener()

    async def start_listener(self):
        if not self.is_configured():
            logger.warning("TELEGRAM_API_ID or TELEGRAM_API_HASH is not set. MTProto listener disabled.")
            return

        async with self._start_lock:
            if self._shutdown:
                return
            # If already running and authorized, avoid duplicate startup
            if self._is_running and self.client and self.client.is_connected():
                try:
                    if await self.client.is_user_authorized():
                        logger.info("Telethon MTProto listener is already active and authorized.")
                        return
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            try:
                session_str = await self.get_active_session_string()
            except Exception as e:
                logger.error(f"Could not load the central MTProto session: {e!r}")
                self._is_running = False
                self._schedule_start_retry()
                return
            if not session_str:
                logger.warning("No Telethon session found. MTProto listener awaiting login...")
                self._is_running = False
                return

            # Cancel old supervisor task if running (never the task that is executing this restart)
            if self._supervisor_task and not self._supervisor_task.done() and self._supervisor_task is not asyncio.current_task():
                self._supervisor_task.cancel()
            self._supervisor_task = None

            # Disconnect old client cleanly (bounded: Telethon's disconnect can hang on half-open sockets)
            if self.client:
                self._unregister_event_handlers(self.client)
                await self._safe_disconnect(self.client)
                self.client = None

            try:
                string_session = StringSession(session_str)
                self.client = TelegramClient(
                    string_session,
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
                    use_ipv6=session_uses_ipv6(string_session)
                )
            except Exception as e:
                logger.error(f"Failed to initialize Telethon StringSession: {e}")
                self._is_running = False
                return

            try:
                await asyncio.wait_for(self.client.connect(), timeout=RECONNECT_STEP_TIMEOUT)
                # get_me() returns None only for a definitive 401 (revoked/unregistered key). Every other
                # failure (network, flood wait, Telegram 5xx) raises and is retried below — a transient
                # error must never destroy a valid session.
                me = await asyncio.wait_for(self.client.get_me(), timeout=RECONNECT_STEP_TIMEOUT)
                if me is None:
                    logger.error("Central MTProto session is no longer authorized. Log in again from the admin bot.")
                    await self._handle_auth_revoked()
                    return
                if self._shutdown:
                    await self._safe_disconnect(self.client)
                    self.client = None
                    return
                if self._cached_me is not None and getattr(self._cached_me, "id", None) != getattr(me, "id", None):
                    # Access hashes are per account: entities resolved by another account are useless now
                    self._clear_entity_caches()
                self._cached_me = me
                name = me.first_name if me else "Unknown"
                logger.info(
                    f"Telethon MTProto connected successfully as: {name} (@{getattr(me, 'username', 'no_username')}, "
                    f"phone: {mask_phone_number(getattr(me, 'phone', None))})"
                )
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

                # Offline catch-up must start from the last post processed BEFORE real-time handlers exist:
                # real-time clones advance the watermark and would otherwise hide the offline gap.
                catchup_floors = await self._snapshot_catchup_floors()
                self._register_event_handlers()
                self._start_attempts = 0

                # Join all active pairs, then catch up from the snapshot
                await self.refresh_monitored_channels(catchup_floors=catchup_floors)

                # Start self-healing supervisor
                if not self._supervisor_task or self._supervisor_task.done():
                    self._supervisor_task = asyncio.create_task(self._connection_supervisor())

            except Exception as e:
                if is_session_revoked_error(e):
                    logger.error(f"CRITICAL: central MTProto session is revoked ({e!r}). Log in again via the admin bot.")
                    await self._handle_auth_revoked()
                    return
                logger.error(f"Failed to start Telethon client: {e!r}")
                self._is_running = False
                self._schedule_start_retry()

    async def _snapshot_catchup_floors(self) -> Dict[int, int]:
        """pair id -> highest source message id already processed, read before real-time delivery starts."""
        floors: Dict[int, int] = {}
        try:
            pairs = await db_manager.get_all_active_pairs()
        except Exception as e:
            logger.warning(f"Could not snapshot catch-up watermarks: {e!r}")
            return floors
        for pair in pairs:
            try:
                last_id = await db_manager.get_effective_last_source_msg_id(pair.id)
            except Exception:
                logger.debug(f"Watermark snapshot failed for pair #{pair.id}", exc_info=True)
                continue
            if last_id is not None:
                floors[pair.id] = last_id
        return floors

    def _event_handler_specs(self):
        return (
            (self._handle_new_message, events.NewMessage),
            (self._handle_message_deleted, events.MessageDeleted),
            (self._handle_message_edited, events.MessageEdited),
            # NewMessage skips service messages, so group -> supergroup migrations need a raw handler
            (self._handle_service_update, events.Raw(types=[UpdateNewMessage, UpdateNewChannelMessage])),
        )

    def _register_event_handlers(self):
        """(Re)registers real-time handlers exactly once on the current client"""
        for handler, event_type in self._event_handler_specs():
            try:
                self.client.remove_event_handler(handler, event_type)
            except Exception:
                logger.debug("Event handler was not registered yet", exc_info=True)
            self.client.add_event_handler(handler, event_type)

    def _unregister_event_handlers(self, client: Optional[TelegramClient] = None):
        """Stops a client from feeding updates into this listener (used before replacing or stopping it)."""
        client = client or self.client
        if client is None:
            return
        for handler, _event_type in self._event_handler_specs():
            try:
                client.remove_event_handler(handler)
            except Exception:
                logger.debug("Event handler removal skipped", exc_info=True)

    @staticmethod
    async def _safe_disconnect(client: Optional[TelegramClient], timeout: float = 15.0):
        """Disconnects a client without ever blocking the caller for longer than `timeout`. disconnect() is
        called even when the client reports itself disconnected: it is idempotent and also stops Telethon's
        own reconnect loop and releases borrowed senders and handler tasks."""
        if client is None:
            return
        try:
            result = client.disconnect()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=timeout)
        except Exception:
            logger.debug("Telethon disconnect did not complete cleanly", exc_info=True)

    def _schedule_start_retry(self):
        """Retries a failed listener start with exponential backoff (e.g. network down at boot)"""
        if self._restart_pending or self._shutdown:
            return
        self._start_attempts += 1
        delay = min(300.0, 10.0 * (2 ** min(self._start_attempts - 1, 5)))
        self._restart_pending = True

        async def _retry():
            try:
                await asyncio.sleep(delay)
            finally:
                self._restart_pending = False
            logger.info(f"Retrying Telethon listener start (attempt #{self._start_attempts + 1})...")
            await self.start_listener()

        logger.warning(f"Telethon listener start failed; retrying in {delay:.0f}s")
        self._spawn_task(_retry())

    async def _connection_supervisor(self):
        """Active MTProto watchdog: probes the DC via GetStateRequest. Short outages are left to Telethon's own
        auto-reconnect; when the connection stays broken the client is replaced by a fresh one built from the
        stored session (never reconnected in place, which would race Telethon's internal reconnect)."""
        failed_probes = 0
        prune_counter = 0

        while self._is_running and not self._shutdown:
            try:
                await asyncio.sleep(SUPERVISOR_PROBE_INTERVAL)
                client = self.client
                if client is None or not self._is_running or self._shutdown:
                    continue

                is_alive = False
                if client.is_connected():
                    try:
                        # Active RPC probe to detect half-open sockets
                        await asyncio.wait_for(client(GetStateRequest()), timeout=15.0)
                        is_alive = True
                    except FloodWaitError as fwe:
                        logger.warning(f"FloodWait on probe: sleeping {fwe.seconds}s")
                        await asyncio.sleep(min(fwe.seconds, 300) + 1)
                        is_alive = True
                    except asyncio.CancelledError:
                        raise
                    except Exception as probe_err:
                        if is_session_revoked_error(probe_err):
                            logger.error(f"CRITICAL: central MTProto session was revoked ({probe_err!r}). Halting supervisor.")
                            await self._handle_auth_revoked()
                            return
                        logger.warning(f"Active MTProto probe failed ({failed_probes + 1}): {probe_err!r}")

                if is_alive:
                    failed_probes = 0
                    # Periodic entity cache pruning keeps the session's entity table bounded
                    prune_counter += 1
                    if prune_counter >= 30:
                        prune_counter = 0
                        await self._prune_entity_cache()
                    continue

                failed_probes += 1
                if failed_probes < PROBE_FAILURES_BEFORE_REBUILD:
                    # A single slow probe is common while large media transfers saturate the link, and
                    # Telethon may still be reconnecting on its own.
                    continue

                logger.error("Telethon connection is stalled. Rebuilding the MTProto client from the stored session...")
                self._is_running = False
                self._spawn_task(self.start_listener())
                return

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Telethon connection supervisor: {e!r}")
                await asyncio.sleep(10)

    async def _prune_entity_cache(self):
        """Keeps the session's in-memory entity table bounded. StringSession/MemorySession stores it in
        client.session._entities as a set of (marked_id, access_hash, username, phone, name) rows; the rows of
        active pair endpoints, cached entities and the account itself are kept."""
        try:
            session = getattr(self.client, "session", None)
            rows = getattr(session, "_entities", None)
            if not isinstance(rows, set) or len(rows) <= ENTITY_PRUNE_THRESHOLD:
                return
            keep_ids: set[int] = set(self._entity_cache.keys())
            keep_names: set[str] = set()
            for pair in await self._get_active_pairs():
                for ref in (pair.source_id, pair.target_id, pair.source_channel, pair.target_channel):
                    raw = normalize_peer_id(ref)
                    if raw:
                        keep_ids.add(raw)
                for ref in (pair.source_channel, pair.target_channel):
                    name = channel_username(ref)
                    if name:
                        keep_names.add(name)
            me_id = getattr(self._cached_me, "id", None)
            if isinstance(me_id, int):
                keep_ids.add(me_id)
            before = len(rows)
            stale = {
                row for row in rows
                if normalize_peer_id(row[0]) not in keep_ids and not (row[2] and row[2] in keep_names)
            }
            rows.difference_update(stale)
            logger.info(f"Pruned Telethon session entity cache: {before} -> {len(rows)} rows retained.")
        except Exception as pe:
            logger.debug(f"Entity cache pruning skipped: {pe}")

    async def stop(self):
        """Graceful shutdown: stop ingesting updates, deliver what was already received (buffered albums and
        queued posts, bounded), then cancel and await the remaining tasks before disconnecting. Awaiting the
        cancelled clone tasks lets them record what they already sent, so nothing is reposted on restart."""
        self._shutdown = True
        self._is_running = False
        client = self.client
        self._unregister_event_handlers(client)
        current = asyncio.current_task()
        supervisor = self._supervisor_task
        self._supervisor_task = None
        if supervisor and not supervisor.done() and supervisor is not current:
            supervisor.cancel()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + SHUTDOWN_DRAIN_TIMEOUT
        try:
            await asyncio.wait_for(
                media_handler.flush_pending_albums(self._on_album_buffer_flushed),
                timeout=SHUTDOWN_DRAIN_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning("Shutdown: buffered albums were not flushed in time.")
        except Exception:
            logger.warning("Shutdown: flushing buffered albums failed", exc_info=True)
        await self._drain_pair_queues(max(0.5, deadline - loop.time()))

        pending = [
            t for t in list(self._background_tasks) + list(self.active_history_tasks.values()) + [supervisor]
            if t is not None and t is not current and not t.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            _done, still_running = await asyncio.wait(pending, timeout=SHUTDOWN_CANCEL_TIMEOUT)
            if still_running:
                logger.warning(f"{len(still_running)} listener task(s) did not finish within {SHUTDOWN_CANCEL_TIMEOUT:.0f}s of cancellation.")
        self._reset_dispatch_state()

        if client is not None:
            await self._safe_disconnect(client)
            logger.info("Telethon client disconnected.")

    async def _drain_pair_queues(self, timeout: float) -> None:
        """Waits (bounded) until every queued post has been processed."""
        queues = list(self._pair_queues.values())
        if not queues:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*(q.join() for q in queues)), timeout=timeout)
        except asyncio.TimeoutError:
            left = sum(q.qsize() for q in queues)
            logger.warning(
                f"Shutdown: {left} queued post(s) were not delivered within {timeout:.0f}s; "
                "offline catch-up delivers them on the next start."
            )

    def _reset_dispatch_state(self) -> None:
        """Drops all queued deliveries (used on stop, logout and session revocation)."""
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in list(self._pair_workers.values()):
            if not task.done() and task is not current:
                task.cancel()
        self._pair_workers.clear()
        self._pair_queues.clear()
        for fut in list(self._album_units.values()):
            if not fut.done():
                fut.cancel()
        self._album_units.clear()

    def _clear_entity_caches(self) -> None:
        self._entity_cache.clear()
        self._username_index.clear()
        self._invite_index.clear()
        self._dialogs_index = None
        self._dialogs_index_time = 0.0

    async def logout(self) -> bool:
        """Logs out of Telegram (revoking the authorization server-side), purges the stored session and
        stops the listener. Returns True only when Telegram confirmed the logout."""
        self._is_running = False
        self._cached_me = None
        settings.TELETHON_SESSION = None
        if self._supervisor_task and not self._supervisor_task.done() and self._supervisor_task is not asyncio.current_task():
            self._supervisor_task.cancel()
        self._supervisor_task = None
        for task in list(self._background_tasks) + list(self.active_history_tasks.values()):
            if not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._reset_dispatch_state()
        self._clear_entity_caches()

        revoked = False
        client, self.client = self.client, None
        if client is not None:
            try:
                if not client.is_connected():
                    await asyncio.wait_for(client.connect(), timeout=RECONNECT_STEP_TIMEOUT)
                revoked = bool(await asyncio.wait_for(client.log_out(), timeout=RECONNECT_STEP_TIMEOUT))
            except Exception:
                logger.warning("Telegram did not confirm the MTProto logout", exc_info=True)
            await self._safe_disconnect(client)

        self._monitored_channels.clear()
        self._channel_usernames.clear()
        await db_manager.delete_setting("telethon_session")
        # Keep a TELETHON_SESSION configured in .env from silently re-activating after an explicit logout.
        await db_manager.set_setting(CENTRAL_SESSION_DISABLED_KEY, "1")
        logger.info(f"Central MTProto session logged out (server confirmed: {revoked}); stored session purged.")
        return revoked

    async def get_me(self):
        """Returns the authenticated central account, or the last known value while temporarily disconnected."""
        if self.client and self.client.is_connected() and self._is_running:
            try:
                me = await asyncio.wait_for(self.client.get_me(), timeout=RECONNECT_STEP_TIMEOUT)
                if me:
                    self._cached_me = me
                    return me
            except Exception:
                logger.debug("get_me failed; returning cached account", exc_info=True)
        return self._cached_me if self._is_running else None

    async def prune_stale_login_sessions(self, max_age_seconds: int = 600):
        """Disconnects and cleans up abandoned in-flight OTP login clients to avoid socket leaks"""
        now = time.time()
        stale_uids = [uid for uid, sdata in list(self._login_sessions.items()) if now - sdata.get("created_at", 0) > max_age_seconds]
        for uid in stale_uids:
            await self.cancel_login(uid)

    def has_login_session(self, user_id: int) -> bool:
        """True while an OTP login started by `user_id` is waiting for the code or the 2FA password."""
        return user_id in self._login_sessions

    async def cancel_login(self, user_id: int) -> None:
        """Aborts an in-flight OTP login and releases its temporary client."""
        sess = self._login_sessions.pop(user_id, None)
        if sess:
            await self._safe_disconnect(sess.get("client"))

    def _login_use_ipv6(self) -> bool:
        """New login clients use IPv6 only when explicitly requested (TELETHON_USE_IPV6=1)."""
        return os.getenv("TELETHON_USE_IPV6", "").strip().lower() in ("1", "true", "yes")

    # --- IN-BOT AUTHENTICATION FLOW ---

    async def request_phone_code(self, user_id: int, phone: str) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "TELEGRAM_API_ID va TELEGRAM_API_HASH sozlanmagan!"

        # Rate limiting: prevent OTP spam — at most one successful code request per minute per admin
        now_ts = time.monotonic()
        last_req = self._otp_cooldowns.get(user_id, 0.0)
        if now_ts - last_req < OTP_COOLDOWN_SECONDS:
            remaining = int(OTP_COOLDOWN_SECONDS - (now_ts - last_req)) + 1
            return False, f"Juda tez! Iltimos {remaining} soniya kuting va qaytadan urinib ko'ring."

        await self.prune_stale_login_sessions()

        is_valid, phone_e164, _ = normalize_phone_number(phone)
        if not is_valid:
            return False, "Telefon raqam formati noto'g'ri!"

        await self.cancel_login(user_id)
        client = TelegramClient(
            StringSession(""),
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH,
            device_model=DEVICE_MODEL,
            system_version=SYSTEM_VERSION,
            app_version=APP_VERSION,
            lang_code=LANG_CODE,
            system_lang_code=SYSTEM_LANG_CODE,
            use_ipv6=self._login_use_ipv6()
        )
        try:
            await asyncio.wait_for(client.connect(), timeout=RECONNECT_STEP_TIMEOUT)
            sent_code = await asyncio.wait_for(client.send_code_request(phone_e164), timeout=RECONNECT_STEP_TIMEOUT)
        except FloodWaitError as e:
            await self._safe_disconnect(client)
            return False, f"Telegram cheklovi: Iltimos, {e.seconds} soniya kuting."
        except PhoneNumberInvalidError:
            await self._safe_disconnect(client)
            return False, "Telegram ushbu telefon raqamini qabul qilmadi. Raqamni tekshirib qayta kiriting."
        except PhoneNumberBannedError:
            await self._safe_disconnect(client)
            return False, "Ushbu telefon raqami Telegram tomonidan bloklangan."
        except Exception as e:
            await self._safe_disconnect(client)
            logger.error(f"Error requesting phone code: {e!r}")
            return False, "Kod yuborib bo'lmadi. Birozdan so'ng qayta urinib ko'ring."

        self._login_sessions[user_id] = {
            "client": client,
            "phone": phone_e164,
            "phone_code_hash": sent_code.phone_code_hash,
            "created_at": time.time()
        }
        self._otp_cooldowns[user_id] = time.monotonic()
        logger.info(f"OTP code requested for admin {user_id} ({mask_phone_number(phone_e164)})")
        return True, "Kod yuborildi."

    async def _activate_logged_in_client(self, user_id: int, client: TelegramClient) -> str:
        """Persists the freshly authorized session as the central session and restarts the listener."""
        me = await client.get_me()
        session_str = client.session.save()
        from services.security_vault import security_vault
        await db_manager.set_setting("telethon_session", security_vault.encrypt_secret(session_str))
        await db_manager.delete_setting(CENTRAL_SESSION_DISABLED_KEY)
        self._login_sessions.pop(user_id, None)
        await self._safe_disconnect(client)
        if self.client is not None:
            # The new account replaces the running one: without this start() would keep the old client
            self._is_running = False
            await self._safe_disconnect(self.client)
        await self.start()
        name = (me.first_name if me else None) or "Foydalanuvchi"
        logger.info(f"Admin {user_id} connected the central MTProto account ({name}).")
        return name

    async def submit_phone_code(self, user_id: int, code: str) -> Tuple[bool, str, str]:
        """Returns (ok, message, status) where status is one of:
        success | needs_2fa | invalid_code | expired | error."""
        session_data = self._login_sessions.get(user_id)
        if not session_data:
            return False, "Kirish sessiyasi topilmadi yoki muddati tugagan. Telefon raqamni qaytadan kiriting.", "expired"

        client: TelegramClient = session_data["client"]
        code_clean = "".join(c for c in code if c.isdigit())
        if not code_clean:
            return False, "Kod faqat raqamlardan iborat bo'lishi kerak.", "invalid_code"

        try:
            await client.sign_in(session_data["phone"], code_clean, phone_code_hash=session_data["phone_code_hash"])
            name = await self._activate_logged_in_client(user_id, client)
            return True, f"Hisob muvaffaqiyatli ulandi: {name}!", "success"
        except SessionPasswordNeededError:
            return False, "2-bosqichli parol (Two-Step Verification) talab qilinadi.", "needs_2fa"
        except PhoneCodeInvalidError:
            return False, "Tasdiqlash kodi noto'g'ri! Iltimos, qaytadan tekshirib kiriting.", "invalid_code"
        except PhoneCodeExpiredError:
            await self.cancel_login(user_id)
            return False, "Tasdiqlash kodi muddati o'tgan. Telefon raqamni qaytadan kiriting.", "expired"
        except FloodWaitError as e:
            await self.cancel_login(user_id)
            return False, f"Telegram cheklovi: Iltimos, {e.seconds} soniya kuting.", "expired"
        except Exception as e:
            logger.error(f"Sign in error: {e!r}")
            return False, "Kirishda kutilmagan xatolik. Qaytadan urinib ko'ring.", "error"

    async def submit_2fa_password(self, user_id: int, password: str) -> Tuple[bool, str]:
        session_data = self._login_sessions.get(user_id)
        if not session_data:
            return False, "Kirish sessiyasi topilmadi yoki muddati tugagan. Telefon raqamni qaytadan kiriting."

        client: TelegramClient = session_data["client"]
        try:
            await client.sign_in(password=password.strip())
            name = await self._activate_logged_in_client(user_id, client)
            return True, f"Hisob muvaffaqiyatli ulandi: {name}!"
        except PasswordHashInvalidError:
            return False, "2FA paroli noto'g'ri!"
        except FloodWaitError as e:
            await self.cancel_login(user_id)
            return False, f"Telegram cheklovi: Iltimos, {e.seconds} soniya kuting va qaytadan boshlang."
        except Exception as e:
            logger.error(f"2FA sign in error: {e!r}")
            return False, "Parolni tekshirishda kutilmagan xatolik. Qaytadan urinib ko'ring."

    async def sign_in_with_code(self, phone: str, code: str, user_id: int = 0) -> Tuple[str, str]:
        """Convenience alias for submit_phone_code. user_id must be non-zero."""
        if user_id == 0:
            logger.warning("sign_in_with_code called with user_id=0 — refusing to submit code to unknown session")
            return "ERROR", "Foydalanuvchi identifikatori noto'g'ri. Qaytadan urinib ko'ring."
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
            return False, "Foydalanuvchi identifikatori noto'g'ri. Qaytadan urinib ko'ring."
        return await self.submit_2fa_password(user_id=user_id, password=password)

    # --- CHANNEL RESOLUTION & MONITORING ---

    def _remember_entity(self, entity: Any) -> Any:
        """Caches a resolved entity (it carries the access hash needed to address it by id later)."""
        raw = normalize_peer_id(getattr(entity, "id", None))
        if raw is None:
            return entity
        self._entity_cache[raw] = (time.monotonic(), entity)
        self._entity_cache.move_to_end(raw)
        while len(self._entity_cache) > ENTITY_CACHE_MAX:
            self._entity_cache.popitem(last=False)
        username = getattr(entity, "username", None)
        if isinstance(username, str) and username:
            self._username_index[username.lower()] = raw
        for index in (self._username_index, self._invite_index):
            if len(index) > ENTITY_CACHE_MAX:
                for key in [k for k, v in index.items() if v not in self._entity_cache]:
                    index.pop(key, None)
        return entity

    def _cached_entity(self, raw_id: Optional[int], allow_stale: bool = False) -> Any:
        item = self._entity_cache.get(raw_id) if raw_id is not None else None
        if item is None:
            return None
        cached_at, entity = item
        if allow_stale or time.monotonic() - cached_at <= ENTITY_CACHE_TTL:
            self._entity_cache.move_to_end(raw_id)
            return entity
        return None

    async def resolve_entity(self, channel_identifier: Union[str, int], join_invite: bool = True):
        """Resolves a channel/group reference (@username, t.me link, numeric id in any form, invite link) to a
        Telethon entity. Resolved entities are cached in memory together with their access hash, so chats
        outside the first dialogs page stay resolvable by id. With join_invite=False an invite link is only
        inspected and never imported (destinations must not be joined by the userbot)."""
        if not self.client or not self.client.is_connected() or channel_identifier is None:
            return None

        clean_id = str(channel_identifier).strip()
        clean_id = re.sub(r'^https?:\/\/(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/', '', clean_id, flags=re.IGNORECASE)
        if "/" in clean_id and not clean_id.startswith("+") and not clean_id.startswith("joinchat/"):
            clean_id = clean_id.split("/")[0]

        try:
            if clean_id.startswith("+") or clean_id.startswith("joinchat/"):
                hash_val = clean_id.lstrip("+").replace("joinchat/", "").strip()
                return await self._resolve_invite(hash_val, join=join_invite)

            clean_digits = re.sub(r'^(-100|-)+', '', clean_id)
            if clean_digits.isdigit():
                return await self._resolve_numeric(int(clean_digits))

            return await self._resolve_username(clean_id)
        except Exception as e:
            logger.warning(f"Failed to resolve entity for '{channel_identifier}': {e!r}")
            return None

    async def _resolve_numeric(self, raw_id: int):
        cached = self._cached_entity(raw_id)
        if cached is not None:
            return cached
        stale = self._cached_entity(raw_id, allow_stale=True)
        if stale is not None:
            try:
                # Refreshes flags such as `left` with the access hash we already know
                return self._remember_entity(await self.client.get_entity(stale))
            except Exception:
                logger.debug(f"Refreshing cached entity {raw_id} failed", exc_info=True)
        # Channels / supergroups first, then basic groups. Positive ids are users and never valid here.
        for marked_id in (int(f"-100{raw_id}"), -raw_id):
            try:
                entity = await self.client.get_entity(marked_id)
            except Exception:
                continue
            if entity is not None:
                return self._remember_entity(entity)
        entity = (await self._get_dialogs_index()).get(raw_id)
        if entity is not None:
            return self._remember_entity(entity)
        return None

    async def _resolve_username(self, name: str):
        key = name.lstrip("@").lower()
        cached = self._cached_entity(self._username_index.get(key))
        if cached is not None and str(getattr(cached, "username", "") or "").lower() == key:
            return cached
        return self._remember_entity(await self.client.get_entity(name))

    async def _resolve_invite(self, hash_val: str, join: bool):
        cached = self._cached_entity(self._invite_index.get(hash_val))
        if cached is not None:
            return cached
        safe_hash = f"+{hash_val[:4]}…"
        chat_info = None
        try:
            chat_info = await self.client(CheckChatInviteRequest(hash_val))
        except Exception as e:
            logger.debug(f"Invite link check for {safe_hash} failed: {e!r}")
        chat = getattr(chat_info, "chat", None)
        if isinstance(chat_info, ChatInviteAlready) and chat is not None:
            # Already a participant: never import again (it would only fail or create a join request)
            raw = normalize_peer_id(getattr(chat, "id", None))
            if raw is not None:
                self._invite_index[hash_val] = raw
            return self._remember_entity(chat)
        if not join:
            return chat
        try:
            res = await self.client(ImportChatInviteRequest(hash_val))
            chats = getattr(res, "chats", None) or []
            if chats:
                entity = chats[0]
                raw = normalize_peer_id(getattr(entity, "id", None))
                if raw is not None:
                    self._invite_index[hash_val] = raw
                return self._remember_entity(entity)
        except UserAlreadyParticipantError:
            title = getattr(chat_info, "title", None) or getattr(chat, "title", None)
            if title:
                for entity in (await self._get_dialogs_index()).values():
                    if getattr(entity, "title", None) == title:
                        return self._remember_entity(entity)
        except Exception as e:
            logger.warning(f"Could not join invite link {safe_hash}: {e!r}")
        return None

    async def _get_dialogs_index(self) -> Dict[int, Any]:
        """raw id -> entity for every channel/group dialog; the expensive last-resort resolution path, so the
        scan is shared by concurrent callers and reused for DIALOGS_INDEX_TTL."""
        async with self._dialogs_lock:
            now = time.monotonic()
            if self._dialogs_index is not None and now - self._dialogs_index_time <= DIALOGS_INDEX_TTL:
                return self._dialogs_index
            client = self.client
            if client is None:
                return self._dialogs_index or {}
            index: Dict[int, Any] = {}
            try:
                async for dialog in client.iter_dialogs():
                    if not (getattr(dialog, "is_channel", False) or getattr(dialog, "is_group", False)):
                        continue
                    raw = normalize_peer_id(getattr(dialog, "id", None))
                    entity = getattr(dialog, "entity", None)
                    if raw is not None and entity is not None:
                        index[raw] = entity
            except Exception as e:
                # A failed (partial) scan is never cached: the next lookup scans again
                logger.warning(f"Dialog scan for entity resolution failed: {e!r}")
                return self._dialogs_index if self._dialogs_index is not None else index
            self._dialogs_index = index
            self._dialogs_index_time = now
            logger.info(f"Indexed {len(index)} channel/group dialogs for entity resolution.")
            return index

    async def join_and_monitor_channel(self, channel_identifier: str, pair_id: Optional[int] = None) -> Optional[int]:
        """Makes sure the central account receives the source chat's updates (joining it when needed) and
        returns its raw id, or None when the chat cannot be resolved or joined."""
        if not self.client or not self.client.is_connected():
            return None
        label = f"pair #{pair_id}" if pair_id else "manual"

        try:
            entity = await self.resolve_entity(channel_identifier, join_invite=True)
            if not entity:
                logger.warning(f"Source '{channel_identifier}' ({label}) could not be resolved by the MTProto account.")
                return None
            if isinstance(entity, User):
                logger.warning(f"Source '{channel_identifier}' ({label}) is a user account, not a channel or group.")
                return None

            migrated_to = getattr(entity, "migrated_to", None) if isinstance(entity, Chat) else None
            if migrated_to is not None:
                # The basic group was upgraded to a supergroup while we were not listening
                new_entity = self._remember_entity(await self.client.get_entity(migrated_to))
                new_raw = normalize_peer_id(getattr(new_entity, "id", None))
                if new_raw:
                    await self._handle_chat_migration(-int(entity.id), new_raw)
                    entity = new_entity

            if getattr(entity, "left", False) is True:
                if not isinstance(entity, Channel):
                    logger.warning(f"Source group '{channel_identifier}' ({label}) was left; it can only be re-joined through an invite link.")
                    return None
                try:
                    result = await self.client(JoinChannelRequest(entity))
                    refreshed = next((c for c in getattr(result, "chats", None) or [] if getattr(c, "id", None) == entity.id), None)
                    if refreshed is not None:
                        self._remember_entity(refreshed)
                    else:
                        self._entity_cache.pop(normalize_peer_id(entity.id), None)
                    logger.info(f"Joined source channel: {getattr(entity, 'title', channel_identifier)} ({label})")
                except UserAlreadyParticipantError:
                    pass
                except Exception as e:
                    logger.warning(f"Could not join source channel '{channel_identifier}' ({label}): {e!r}")
                    return None

            cid = getattr(entity, 'id', None)
            if cid:
                self._monitored_channels.add(cid)
                username = getattr(entity, 'username', None)
                if isinstance(username, str) and username:
                    self._channel_usernames.add(username.lower())
                return cid
        except Exception as e:
            logger.warning(f"Could not join/monitor channel '{channel_identifier}' ({label}): {e!r}")
        return None

    async def _cache_target_entity(self, pair: ChannelPair) -> None:
        """Resolves and caches the destination (used by the Telethon premium-emoji send path) without ever
        joining it, and fills a missing target_id of a channel/supergroup destination."""
        entity = None
        for ref in (pair.target_id, pair.target_channel):
            ref_text = str(ref).strip() if ref is not None else ""
            if not ref_text or is_invite_reference(ref_text):
                continue
            entity = await self.resolve_entity(ref_text, join_invite=False)
            if entity is not None:
                break
        if not isinstance(entity, Channel) or pair.target_id:
            return
        formatted_tid = int(f"-100{entity.id}")
        await db_manager.update_pair_target_id(pair.id, formatted_tid)
        pair.target_id = formatted_tid

    async def refresh_monitored_channels(self, catchup_floors: Optional[Dict[int, int]] = None):
        """Joins every active pair's source, caches its destination, then runs the offline catch-up
        (from `catchup_floors` when the caller snapshotted watermarks before real-time delivery started)."""
        active_pairs = await db_manager.get_all_active_pairs()
        logger.info(f"Refreshing monitored channels for {len(active_pairs)} active pairs...")
        monitored: set[int] = set()

        for pair in active_pairs:
            try:
                cid = None
                if pair.source_id:
                    cid = await self.join_and_monitor_channel(str(pair.source_id), pair_id=pair.id)
                if not cid and pair.source_channel:
                    cid = await self.join_and_monitor_channel(pair.source_channel, pair_id=pair.id)
                if not cid:
                    logger.warning(f"Pair #{pair.id}: source {pair.source_channel} is not joined; its new posts cannot be received.")
                    continue
                monitored.add(cid)
                if not pair.source_id:
                    await db_manager.update_pair_source_id(pair.id, cid)

                await self._cache_target_entity(pair)
                logger.info(f"Monitoring pair #{pair.id}: {pair.source_title or pair.source_channel} ({pair.source_channel}) -> {pair.target_channel}")
            except Exception as e:
                logger.error(f"Error joining/resolving channels for pair {pair.id}: {e!r}")

        self._monitored_channels = monitored
        # Trigger background offline catch-up for any missed posts while the bot/PC was offline
        self._spawn_task(self.catch_up_all_active_pairs(floors=catchup_floors))

    # --- REAL-TIME EVENT HANDLER ---

    async def _get_active_pairs(self) -> List[ChannelPair]:
        if self._active_pairs_cache_time and time.time() - self._active_pairs_cache_time <= PAIRS_CACHE_TTL:
            return self._active_pairs_cache
        pairs = await db_manager.get_all_active_pairs()
        self._index_active_pairs(pairs)
        return self._active_pairs_cache

    def _index_active_pairs(self, pairs: Iterable[ChannelPair]) -> None:
        by_id: Dict[int, List[ChannelPair]] = {}
        by_name: Dict[str, List[ChannelPair]] = {}
        targets: set[int] = set()
        cached: List[ChannelPair] = []
        for pair in pairs:
            cached.append(pair)
            src_raw = normalize_peer_id(pair.source_id) or normalize_peer_id(pair.source_channel)
            if src_raw:
                by_id.setdefault(src_raw, []).append(pair)
            else:
                # Only pairs whose source chat id was never resolved need the (mutable) username
                name = channel_username(pair.source_channel)
                if name:
                    by_name.setdefault(name, []).append(pair)
            tgt_raw = normalize_peer_id(pair.target_id) or normalize_peer_id(pair.target_channel)
            if tgt_raw:
                targets.add(tgt_raw)
        self._active_pairs_cache = cached
        self._pairs_by_source_id = by_id
        self._pairs_by_source_name = by_name
        self._target_raw_ids = targets
        self._active_pairs_cache_time = time.time()

    def _cached_pair(self, pair_id: Any) -> Optional[ChannelPair]:
        for pair in self._active_pairs_cache:
            if pair.id == pair_id:
                return pair
        return None

    @staticmethod
    def _bot_user_id() -> Optional[int]:
        """User id of our public bot (aiogram derives it from the token without a network call)."""
        candidate = getattr(cloner_engine.bot, "id", None)
        if isinstance(candidate, int):
            return candidate
        prefix = (settings.BOT_TOKEN or "").split(":", 1)[0]
        return int(prefix) if prefix.isdigit() else None

    def _is_own_outgoing(self, message: Any, chat_raw_id: Optional[int], chat_is_target: bool) -> bool:
        """Cheap checks for posts this system published itself (the database check runs in the worker)."""
        bot_id = self._bot_user_id()
        if bot_id:
            if getattr(message, "via_bot_id", None) == bot_id:
                return True
            if getattr(getattr(message, "from_id", None), "user_id", None) == bot_id:
                return True
        if chat_is_target:
            # In a chat that is also some pair's destination, our userbot's own posts are clones (premium
            # emoji path). Elsewhere `out` may be a real post of the account owner and is cloned normally.
            if getattr(message, "out", False) is True:
                return True
            recent = getattr(cloner_engine, "recent_sent_targets", None)
            if recent is not None and chat_raw_id is not None:
                try:
                    if (chat_raw_id, message.id) in recent:
                        return True
                except Exception:
                    logger.debug("recent_sent_targets lookup failed", exc_info=True)
        return False

    async def _event_chat_username(self, event: Any) -> Optional[str]:
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
        username = getattr(chat, "username", None)
        return username.lower().lstrip("@") if isinstance(username, str) and username else None

    async def _dispatch_story_post(self, event: Any, message: Any) -> None:
        """Hands a channel post to the Story auto-publisher (runs as its own task)."""
        try:
            chat = await event.get_chat()
            if not chat:
                return
            from services.story_cloner_service import story_cloner_service
            await story_cloner_service.handle_incoming_channel_message(message, chat)
        except asyncio.CancelledError:
            raise
        except Exception as se:
            logger.warning(f"Story cloner message check failed: {se!r}")

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
                new_raw = normalize_peer_id(new_supergroup_id)
                if new_raw:
                    self._monitored_channels.add(new_raw)
                logger.info(f"Successfully migrated {migrated_count} channel pair reference(s) to new supergroup ID {new_supergroup_id}")
        except Exception as e:
            logger.error(f"Error handling chat migration from {old_chat_id} to {new_channel_id}: {e}", exc_info=True)

    async def _handle_service_update(self, update: Any):
        """Raw handler for group -> supergroup migrations (service messages never reach NewMessage)."""
        try:
            message = getattr(update, "message", None)
            if not isinstance(message, MessageService):
                return
            action = message.action
            if isinstance(action, MessageActionChatMigrateTo):
                old_chat = getattr(message.peer_id, "chat_id", None)
                if old_chat and action.channel_id:
                    self._spawn_task(self._handle_chat_migration(-int(old_chat), action.channel_id))
            elif isinstance(action, MessageActionChannelMigrateFrom):
                new_channel = getattr(message.peer_id, "channel_id", None)
                if new_channel and action.chat_id:
                    self._spawn_task(self._handle_chat_migration(-int(action.chat_id), new_channel))
        except Exception as e:
            logger.error(f"Error in Telethon service update handler: {e!r}", exc_info=True)

    async def _handle_new_message(self, event: events.NewMessage.Event):
        try:
            if self._shutdown:
                return
            message = getattr(event, 'message', None)
            if not message:
                return
            # Sources are channels and groups only. Dropping private chats up front also stops a user whose
            # id equals a source channel's raw id (peer types are stripped for matching) from being cloned.
            if getattr(event, 'is_private', None) is True:
                return
            if is_ignorable_message(message):
                return

            # Story auto-publishing is driven by broadcast channel posts only
            if getattr(message, 'post', False) is True:
                self._spawn_task(self._dispatch_story_post(event, message))

            chat_raw_id = normalize_peer_id(getattr(event, 'chat_id', None))
            await self._get_active_pairs()
            if not self._active_pairs_cache:
                return

            chat_is_target = chat_raw_id is not None and chat_raw_id in self._target_raw_ids
            if self._is_own_outgoing(message, chat_raw_id, chat_is_target):
                logger.debug(f"Skipping message {message.id} in {chat_raw_id}: published by this system.")
                return

            matching_pairs: List[ChannelPair] = list(self._pairs_by_source_id.get(chat_raw_id, ())) if chat_raw_id else []
            if self._pairs_by_source_name:
                chat_username = await self._event_chat_username(event)
                if chat_username:
                    matching_pairs.extend(self._pairs_by_source_name.get(chat_username, ()))
            if not matching_pairs:
                return

            # Deduplicate by (user_id, target_key) so a single user never posts duplicates to the same destination,
            # while preventing cross-user pair collisions when multiple users/co-admins share a target channel.
            seen_pair_ids = set()
            seen_user_destinations = set()
            unique_matching_pairs: List[ChannelPair] = []
            for p in matching_pairs:
                if not p.is_active or (p.id is not None and p.id in seen_pair_ids):
                    continue
                if p.id is not None:
                    seen_pair_ids.add(p.id)
                # Hard architectural security: target cannot be a private user
                t_chan = str(p.target_channel or "").strip()
                if t_chan and not t_chan.startswith("@") and not t_chan.startswith("-100"):
                    if (p.target_id and p.target_id > 0 and not str(p.target_id).startswith("-")) or (not p.target_id and t_chan.isdigit() and len(t_chan) < 10):
                        continue
                # Forum pairs only clone the configured source topic
                if not message_in_topic(message, getattr(p, "source_topic_id", None)):
                    continue
                target_key = normalize_peer_id(p.target_id) if p.target_id else normalize_peer_id(p.target_channel)
                if not target_key:
                    target_key = t_chan.lstrip("@").lower()
                dest_key = (p.user_id, target_key)
                if dest_key not in seen_user_destinations:
                    seen_user_destinations.add(dest_key)
                    unique_matching_pairs.append(p)

            if not unique_matching_pairs:
                return

            logger.info(f"🚀 Dispatching message {message.id} to {len(unique_matching_pairs)} unique destination channels...")

            enqueued_at = time.monotonic()
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
                self._enqueue_message(pair, message, chat_raw_id, chat_is_target, enqueued_at)

        except Exception as e:
            logger.error(f"Error in Telethon _handle_new_message: {e}", exc_info=True)

    # --- PER-PAIR ORDERED DELIVERY ---

    def _enqueue_message(self, pair: ChannelPair, message: Any, chat_raw_id: Optional[int], verify_not_own: bool, enqueued_at: float) -> None:
        """Queues a post in arrival order. An album gets its queue slot when its FIRST item arrives; the slot
        resolves once the media buffer has collected the whole album, so a later text post waits for it."""
        if message.grouped_id:
            key = (pair.id, message.grouped_id)
            fut = self._album_units.get(key)
            if fut is None or fut.done():
                fut = asyncio.get_running_loop().create_future()
                self._album_units[key] = fut
                self._enqueue_unit(pair, _CloneUnit(
                    pair, album_key=key, album_future=fut, chat_raw_id=chat_raw_id,
                    verify_not_own=verify_not_own, enqueued_at=enqueued_at
                ))
            media_handler.album_buffer.add_message(key, message, self._on_album_buffer_flushed)
        else:
            self._enqueue_unit(pair, _CloneUnit(
                pair, message=message, chat_raw_id=chat_raw_id,
                verify_not_own=verify_not_own, enqueued_at=enqueued_at
            ))

    def _enqueue_unit(self, pair: ChannelPair, unit: _CloneUnit) -> None:
        queue = self._pair_queues.get(pair.id)
        if queue is None:
            queue = asyncio.Queue()
            self._pair_queues[pair.id] = queue
        queue.put_nowait(unit)
        worker = self._pair_workers.get(pair.id)
        if worker is None or worker.done():
            self._pair_workers[pair.id] = self._spawn_task(self._pair_worker(pair.id, queue))

    async def _pair_worker(self, pair_id: int, queue: asyncio.Queue) -> None:
        """Delivers one pair's posts strictly one after another; exits after being idle for a while."""
        try:
            while True:
                try:
                    unit = await asyncio.wait_for(queue.get(), timeout=PAIR_WORKER_IDLE_SECONDS)
                except asyncio.TimeoutError:
                    if queue.empty():
                        return
                    continue
                try:
                    await self._process_clone_unit(unit)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Delivery worker of pair #{pair_id} failed on a post: {e!r}", exc_info=True)
                finally:
                    queue.task_done()
        finally:
            if self._pair_workers.get(pair_id) is asyncio.current_task():
                self._pair_workers.pop(pair_id, None)
            if self._pair_queues.get(pair_id) is queue and queue.empty():
                self._pair_queues.pop(pair_id, None)

    async def _process_clone_unit(self, unit: _CloneUnit) -> None:
        pair = unit.pair
        if unit.album_future is not None:
            fut = unit.album_future
            try:
                messages = await asyncio.wait_for(fut, timeout=ALBUM_ASSEMBLY_TIMEOUT)
            except asyncio.TimeoutError:
                if self._album_units.get(unit.album_key) is fut:
                    self._album_units.pop(unit.album_key, None)
                logger.warning(
                    f"Album {unit.album_key[1]} of pair #{pair.id} was not assembled within "
                    f"{ALBUM_ASSEMBLY_TIMEOUT:.0f}s; it is delivered when the media buffer flushes."
                )
                return
            if not messages:
                return
            if unit.verify_not_own and await self._is_own_clone(unit.chat_raw_id, [m.id for m in messages], unit.enqueued_at):
                logger.info(f"Skipping album {unit.album_key[1]} for pair #{pair.id}: it is one of our own clone posts (loop).")
                return
            async with cache_manager.cloner_semaphore:
                await cloner_engine.clone_media_group(messages, pair)
            return

        message = unit.message
        if unit.verify_not_own and await self._is_own_clone(unit.chat_raw_id, [message.id], unit.enqueued_at):
            logger.info(f"Skipping message {message.id} for pair #{pair.id}: it is one of our own clone posts (loop).")
            return
        async with cache_manager.cloner_semaphore:
            await cloner_engine.clone_single_message(message, pair)

    async def _is_own_clone(self, chat_raw_id: Optional[int], message_ids: List[int], enqueued_at: float) -> bool:
        """Database check that a post in a chat that is also a destination was published by us. Waits until the
        engine had time to record a clone it has just sent (the update can arrive before the record)."""
        if chat_raw_id is None:
            return False
        settle = OWN_POST_SETTLE_SECONDS - (time.monotonic() - enqueued_at)
        if settle > 0:
            await asyncio.sleep(settle)
        recent = getattr(cloner_engine, "recent_sent_targets", None)
        for mid in message_ids:
            try:
                if recent is not None and (chat_raw_id, mid) in recent:
                    return True
            except Exception:
                logger.debug("recent_sent_targets lookup failed", exc_info=True)
            if await db_manager.is_own_clone_post(chat_raw_id, mid):
                return True
        return False

    async def _on_album_buffer_flushed(self, key: Any, messages: List[Any]) -> None:
        """Media buffer callback: hands the complete album to the queue slot waiting for it."""
        fut = self._album_units.pop(key, None)
        if fut is not None and not fut.done():
            fut.set_result(list(messages))
            return
        # Nobody waits for this album any more (its slot timed out or the listener was reset): clone it
        # directly so it is not lost.
        pair_id = key[0] if isinstance(key, tuple) and key else None
        if pair_id is None or not messages:
            return
        pair = self._cached_pair(pair_id) or await db_manager.get_pair_by_id(pair_id)
        if not pair or not pair.is_active:
            return
        async with cache_manager.cloner_semaphore:
            await cloner_engine.clone_media_group(list(messages), pair)

    # --- EDIT / DELETE SYNCHRONISATION ---

    async def _event_source_identity(self, event: Any) -> Tuple[Optional[int], Optional[str]]:
        """(raw chat id, lower-case username) of the chat an edit/delete event belongs to."""
        chat_raw_id = normalize_peer_id(getattr(event, "chat_id", None))
        chat = None
        try:
            chat = await event.get_chat()
        except Exception:
            logger.debug("Could not load the event chat", exc_info=True)
        username = None
        if chat is not None:
            if chat_raw_id is None:
                chat_raw_id = normalize_peer_id(getattr(chat, "id", None))
            uname = getattr(chat, "username", None)
            if isinstance(uname, str) and uname:
                username = uname.lower().lstrip("@")
        return chat_raw_id, username

    @staticmethod
    def _unique_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen, unique = set(), []
        for rec in records or []:
            rec_id = rec.get("id")
            if rec_id and rec_id not in seen:
                seen.add(rec_id)
                unique.append(rec)
        return unique

    @staticmethod
    def _record_is_syncable(rec: Dict[str, Any]) -> bool:
        """A published clone of an active pair that was not closed as sold."""
        return bool(rec.get("pair_is_active")) and bool(rec.get("target_msg_id")) and rec.get("status") != "sold"

    @staticmethod
    def _is_priced_listing(rec: Dict[str, Any]) -> bool:
        """Sold marking is a real-estate feature: listings carry a detected price, news posts do not."""
        try:
            return float(rec.get("price") or 0) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _record_target_chat(rec: Dict[str, Any]) -> Union[int, str, None]:
        """Destination chat of a clone record: the pair's numeric target id, else a public @username or
        -100 id stored as the target channel (never an invite link, which the Bot API cannot address)."""
        raw = normalize_peer_id(rec.get("pair_target_id"))
        if raw:
            return int(f"-100{raw}")
        for ref in (rec.get("target_channel"), rec.get("pair_target_channel")):
            text = str(ref or "").strip()
            if _PUBLIC_TARGET_RE.fullmatch(text):
                return text
            if _MARKED_CHANNEL_ID_RE.fullmatch(text):
                return int(text)
        return None

    @staticmethod
    def _fit_for_edit(text: str, is_text_message: bool) -> str:
        """Fits HTML text into Telegram's message (4096) / caption (1024) limit, counted in UTF-16 units."""
        limit = 4096 if is_text_message else 1024
        if TextProcessor.get_visible_text_length(text) <= limit:
            return TextProcessor.ensure_closed_tags(text)
        fitted, _overflow = TextProcessor.fit_caption_limit(text, max_limit=limit - 1)
        return fitted + "…"

    async def _is_caption_holder(self, rec: Dict[str, Any]) -> bool:
        """Only the first item of a cloned album carries the caption; the other items must not be edited."""
        if rec.get("media_type") != "media_group" or not rec.get("media_group_id"):
            return True
        try:
            async with db_manager.get_connection() as db:
                cursor = await db.execute(
                    "SELECT 1 FROM cloned_messages WHERE pair_id = ? AND media_group_id = ? AND source_msg_id < ? LIMIT 1",
                    (rec.get("pair_id"), rec.get("media_group_id"), rec.get("source_msg_id"))
                )
                return await cursor.fetchone() is None
        except Exception:
            logger.debug("Album caption holder lookup failed", exc_info=True)
            return False

    @staticmethod
    def _edit_bot():
        bot = cloner_engine.bot
        if bot is None:
            from admin_bot.public_bot import get_public_bot
            bot = get_public_bot()
        return bot

    async def _edit_destination(self, rec: Dict[str, Any], html_text: str) -> bool:
        """Replaces the text/caption of a destination post through the Bot API."""
        target = self._record_target_chat(rec)
        target_msg_id = rec.get("target_msg_id")
        bot = self._edit_bot()
        if target is None or not target_msg_id or bot is None:
            logger.info(f"Destination post of clone record #{rec.get('id')} cannot be addressed; edit skipped.")
            return False
        is_text = rec.get("media_type") == "text"
        new_text = self._fit_for_edit(strip_tg_emoji(html_text), is_text)
        try:
            if is_text:
                await bot.edit_message_text(chat_id=target, message_id=target_msg_id, text=new_text, parse_mode="HTML")
            else:
                await bot.edit_message_caption(chat_id=target, message_id=target_msg_id, caption=new_text, parse_mode="HTML")
            return True
        except Exception as ed_err:
            if "not modified" in str(ed_err).lower():
                return True
            logger.info(f"Could not edit destination post #{target_msg_id} in {target}: {ed_err}")
            return False

    async def _mark_record_sold(self, rec: Dict[str, Any], fallback_text: str = "") -> None:
        base = rec.get("last_caption") or (html.escape(fallback_text) if fallback_text else "")
        if await self._edit_destination(rec, SOLD_TAG + base):
            logger.info(f"Marked destination post #{rec.get('target_msg_id')} of pair #{rec.get('pair_id')} as SOLD")
        await db_manager.update_cloned_message_status(rec["id"], "sold")

    async def _handle_message_deleted(self, event: events.MessageDeleted.Event):
        """
        Handles message deletion from monitored source channels. A deleted priced listing is marked as
        SOLD/CLOSED on the destination (preserving channel stats); other destination posts are left
        untouched. Active Stories generated from the post are deleted.
        """
        try:
            if self._shutdown:
                return
            deleted_ids = [i for i in (getattr(event, 'deleted_ids', None) or []) if isinstance(i, int)]
            if not deleted_ids:
                return

            chat_raw_id, chat_username = await self._event_source_identity(event)
            if chat_raw_id is None and not chat_username:
                return  # deletions in private chats and basic groups carry no chat

            logger.info(f"🗑 Source messages {deleted_ids} deleted in {chat_username or chat_raw_id}. Syncing with destination channels...")

            for del_id in deleted_ids:
                records = await db_manager.get_cloned_messages_for_source(del_id, peer_id=chat_raw_id, username=chat_username)
                for rec in self._unique_records(records):
                    if not self._record_is_syncable(rec) or not await self._is_caption_holder(rec):
                        continue
                    if not self._is_priced_listing(rec):
                        logger.info(
                            f"Source post {del_id} deleted; destination post #{rec.get('target_msg_id')} of pair "
                            f"#{rec.get('pair_id')} left unchanged (not a priced listing)."
                        )
                        continue
                    await self._mark_record_sold(rec)

                # Delete active Story if one was generated
                try:
                    from services.story_cloner_service import story_cloner_service
                    await story_cloner_service.handle_source_message_deleted(
                        source_channel=chat_username or str(chat_raw_id or ""),
                        source_msg_id=del_id
                    )
                except Exception as se:
                    logger.debug(f"Story delete sync note: {se}")

        except Exception as e:
            logger.error(f"Error in Telethon _handle_message_deleted: {e}", exc_info=True)

    def _is_new_edit(self, chat_raw_id: Optional[int], message: Any) -> bool:
        """False when this exact edit was already handled (MessageEdited is repeated for reactions/views)."""
        key = (chat_raw_id or 0, getattr(message, "id", 0))
        edit_date = getattr(message, "edit_date", None)
        if key in self._handled_edits and self._handled_edits[key] == edit_date:
            return False
        self._handled_edits[key] = edit_date
        self._handled_edits.move_to_end(key)
        while len(self._handled_edits) > 2000:
            self._handled_edits.popitem(last=False)
        return True

    async def _handle_message_edited(self, event: events.MessageEdited.Event):
        """
        Handles message edits from monitored source channels.
        A priced listing edited to "sold" is marked SOLD on the destination; otherwise the caption is
        re-processed and the destination post is edited.
        """
        try:
            if self._shutdown:
                return
            message = getattr(event, 'message', None)
            if not message or is_ignorable_message(message):
                return
            if getattr(event, 'is_private', None) is True:
                return
            # Reaction / view-counter updates of never-edited posts arrive as "edits" without an edit date
            if getattr(message, 'edit_date', True) is None:
                return

            chat_raw_id, chat_username = await self._event_source_identity(event)
            if chat_raw_id is None and not chat_username:
                return
            if not self._is_new_edit(chat_raw_id, message):
                return

            records = [
                rec for rec in self._unique_records(
                    await db_manager.get_cloned_messages_for_source(message.id, peer_id=chat_raw_id, username=chat_username)
                )
                if self._record_is_syncable(rec)
            ]
            if not records:
                return

            text = getattr(message, 'message', '') or ''
            is_sold_keyword = bool(SOLD_KEYWORDS_RE.search(text))
            source_html: Optional[str] = None

            for rec in records:
                if not await self._is_caption_holder(rec):
                    continue

                if is_sold_keyword and self._is_priced_listing(rec):
                    await self._mark_record_sold(rec, fallback_text=text)
                    # Also delete story if marked as sold
                    try:
                        from services.story_cloner_service import story_cloner_service
                        await story_cloner_service.handle_source_message_deleted(
                            source_channel=chat_username or str(chat_raw_id or ""),
                            source_msg_id=message.id
                        )
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                    continue

                # Regular text edit
                pair = await db_manager.get_pair_by_id(rec["pair_id"])
                if not pair or not pair.is_active:
                    continue
                if source_html is None:
                    source_html = extract_message_html(message)
                processed = await cloner_engine.process_post_text(source_html, pair)
                if not processed or processed == rec.get("last_caption"):
                    continue
                if await self._edit_destination(rec, processed):
                    await db_manager.update_cloned_message_caption(rec["id"], processed)

        except Exception as e:
            logger.error(f"Error in Telethon _handle_message_edited: {e}", exc_info=True)

    # --- SHARED HELPERS FOR HISTORY / CATCH-UP ---

    async def _current_pair(self, pair: ChannelPair) -> Optional[ChannelPair]:
        """Fresh copy of the pair, or None once it was paused or deleted (long runs must stop then)."""
        try:
            fresh = await db_manager.get_pair_by_id(pair.id)
        except Exception:
            logger.debug(f"Could not re-read pair #{pair.id}; continuing with the snapshot", exc_info=True)
            return pair
        if fresh is None or not fresh.is_active:
            return None
        return fresh

    async def _own_post_checker(self, pair: ChannelPair, entity: Any) -> Callable[[Any], Any]:
        """Async predicate telling whether a message of the pair's source is one of our own clone posts. Only
        sources that are also a destination of an active pair (loops) need the per-message database check."""
        source_raw = normalize_peer_id(getattr(entity, "id", None)) or normalize_peer_id(pair.source_id)
        try:
            await self._get_active_pairs()
        except Exception:
            logger.debug("Active pairs unavailable for loop detection", exc_info=True)
        if not source_raw or source_raw not in self._target_raw_ids:
            async def _never(_msg: Any) -> bool:
                return False
            return _never

        async def _check(msg: Any) -> bool:
            return await db_manager.is_own_clone_post(source_raw, msg.id)
        return _check

    @staticmethod
    def _topic_filter(pair: ChannelPair) -> Tuple[Dict[str, Any], Optional[int]]:
        """(iter_messages kwargs, topic id to verify client-side) for the pair's source topic filter."""
        topic_id = getattr(pair, "source_topic_id", None)
        kwargs = topic_iter_kwargs(topic_id)
        return kwargs, (None if kwargs else topic_id)

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
        iter_kwargs, client_topic = self._topic_filter(pair)
        is_own_post = await self._own_post_checker(pair, entity)

        async def _wanted(msg: Any) -> bool:
            if is_ignorable_message(msg) or not message_in_topic(msg, client_topic):
                return False
            return not await is_own_post(msg)

        async def _pair_for_clone() -> Optional[ChannelPair]:
            fresh = await self._current_pair(pair)
            if fresh is None:
                logger.info(f"Pair #{pair.id} was paused or deleted; stopping its history clone.")
            return fresh

        async def _report_stopped() -> Dict[str, Any]:
            if progress_callback:
                try:
                    await progress_callback(cloned_count, cloned_count + failed_count, "cancelled")
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            return {
                "total": cloned_count + failed_count,
                "cloned": cloned_count,
                "failed": failed_count,
                "status": "cancelled"
            }

        try:
            # 1. If limit is specified (e.g. 10, 30, 50, 100):
            # Collect the latest `limit` logical posts (albums as a single post item, single messages as single item)
            if limit is not None:
                logical_posts: List[Union[TelethonMessage, List[TelethonMessage]]] = []
                current_album: List[TelethonMessage] = []
                current_group_id = None

                # Fetch messages from newest backwards (fetch enough raw messages to fill `limit` logical posts)
                raw_fetch_limit = max(limit * 30, 300)
                async for msg in self.client.iter_messages(entity, limit=raw_fetch_limit, reverse=False, **iter_kwargs):
                    if not await _wanted(msg):
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
                    active_pair = await _pair_for_clone()
                    if active_pair is None:
                        return await _report_stopped()
                    try:
                        if isinstance(item, list):
                            uncloned_items = [m for m in item if not await db_manager.is_message_cloned(pair.id, m.id)]
                            if not uncloned_items:
                                continue
                            ok = await cloner_engine.clone_media_group(uncloned_items, active_pair)
                            if ok:
                                cloned_count += 1
                            else:
                                failed_count += 1
                        else:
                            if await db_manager.is_message_cloned(pair.id, item.id):
                                continue
                            ok = await cloner_engine.clone_single_message(item, active_pair)
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
                                    ok = await cloner_engine.clone_media_group(uncloned_items, active_pair)
                                    if ok:
                                        cloned_count += 1
                                    else:
                                        failed_count += 1
                            else:
                                if not await db_manager.is_message_cloned(pair.id, item.id):
                                    ok = await cloner_engine.clone_single_message(item, active_pair)
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
                stopped = False

                async def flush_album_all():
                    nonlocal cloned_count, failed_count, logical_processed, stopped
                    if not pending_album:
                        return
                    album_items = list(pending_album)
                    pending_album.clear()

                    uncloned_items = [m for m in album_items if not await db_manager.is_message_cloned(pair.id, m.id)]
                    if not uncloned_items:
                        return

                    active_pair = await _pair_for_clone()
                    if active_pair is None:
                        stopped = True
                        return
                    logical_processed += 1
                    try:
                        ok = await cloner_engine.clone_media_group(uncloned_items, active_pair)
                        if ok:
                            cloned_count += 1
                        else:
                            failed_count += 1
                    except FloodWaitError as e:
                        wait_sec = min(e.seconds, 120)
                        logger.warning(f"FloodWaitError in clone_history album: sleeping {wait_sec}s")
                        await asyncio.sleep(wait_sec)
                        try:
                            ok = await cloner_engine.clone_media_group(uncloned_items, active_pair)
                            if ok:
                                cloned_count += 1
                            else:
                                failed_count += 1
                        except Exception:
                            failed_count += 1
                    except Exception as e:
                        logger.error(f"Error cloning history album: {e}")
                        failed_count += 1

                async for msg in self.client.iter_messages(entity, reverse=True, **iter_kwargs):
                    if not await _wanted(msg):
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

                        if not stopped and not await db_manager.is_message_cloned(pair.id, msg.id):
                            active_pair = await _pair_for_clone()
                            if active_pair is None:
                                stopped = True
                            else:
                                logical_processed += 1
                                try:
                                    ok = await cloner_engine.clone_single_message(msg, active_pair)
                                    if ok:
                                        cloned_count += 1
                                    else:
                                        failed_count += 1
                                except FloodWaitError as e:
                                    wait_sec = min(e.seconds, 120)
                                    logger.warning(f"FloodWaitError in clone_history: sleeping {wait_sec}s")
                                    await asyncio.sleep(wait_sec)
                                    try:
                                        ok = await cloner_engine.clone_single_message(msg, active_pair)
                                        if ok:
                                            cloned_count += 1
                                        else:
                                            failed_count += 1
                                    except Exception:
                                        failed_count += 1
                                except Exception as e:
                                    logger.error(f"Error cloning message #{msg.id}: {e}")
                                    failed_count += 1

                    if stopped:
                        return await _report_stopped()

                    if progress_callback and (logical_processed % 5 == 0 or logical_processed == 1):
                        try:
                            effective_total = max(total_raw, logical_processed)
                            await progress_callback(logical_processed, effective_total, "running")
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)

                    await asyncio.sleep(0.3)

                await flush_album_all()
                if stopped:
                    return await _report_stopped()

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
        max_messages: int = 150,
        min_id_floor: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Catches up on posts missed while the bot / host machine was offline or disconnected.
        Retrieves messages with id > last processed id in chronological order (oldest to newest),
        batches media groups (albums) properly, checks deduplication, respects rate limits,
        and safely updates pair's last_seen_msg_id. `min_id_floor` is the watermark snapshotted before
        real-time delivery started (real-time clones advance the stored watermark past the offline gap).
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
            await self._clear_source_access_lost(pair)
            if not latest_msgs:
                return {"status": "channel_empty", "caught_up": 0, "failed": 0}

            current_head_id = latest_msgs[0].id
            last_id = await db_manager.get_effective_last_source_msg_id(pair.id)
            if min_id_floor is not None and last_id is not None:
                last_id = min(last_id, min_id_floor)

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
            iter_kwargs, client_topic = self._topic_filter(pair)
            is_own_post = await self._own_post_checker(pair, entity)
            pair_stopped = False

            async def advance_watermark(msg_id: int):
                nonlocal highest_id_processed
                if msg_id > highest_id_processed:
                    highest_id_processed = msg_id
                    await db_manager.update_pair_last_seen_msg_id(pair.id, highest_id_processed)

            async def flush_album():
                nonlocal caught_up, failed, highest_id_processed, pair_stopped
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

                active_pair = await self._current_pair(pair)
                if active_pair is None:
                    pair_stopped = True
                    return
                try:
                    ok = await cloner_engine.clone_media_group(uncloned_items, active_pair)
                    if ok:
                        caught_up += len(uncloned_items)
                    else:
                        failed += len(uncloned_items)
                except FloodWaitError as fe:
                    wait_sec = min(fe.seconds, 120)
                    logger.warning(f"FloodWait in catch-up album: sleeping {wait_sec}s")
                    await asyncio.sleep(wait_sec)
                    try:
                        ok = await cloner_engine.clone_media_group(uncloned_items, active_pair)
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

            async for msg in self.client.iter_messages(entity, min_id=last_id, limit=max_messages, reverse=True, **iter_kwargs):
                if is_ignorable_message(msg) or not message_in_topic(msg, client_topic) or await is_own_post(msg):
                    # Nothing to deliver (service message, another forum topic or our own clone post)
                    await advance_watermark(msg.id)
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
                    if pair_stopped:
                        break

                    if await db_manager.is_message_cloned(pair.id, msg.id):
                        await advance_watermark(msg.id)
                        continue

                    active_pair = await self._current_pair(pair)
                    if active_pair is None:
                        pair_stopped = True
                        break
                    try:
                        ok = await cloner_engine.clone_single_message(msg, active_pair)
                        if ok:
                            caught_up += 1
                        else:
                            failed += 1
                    except FloodWaitError as fe:
                        wait_sec = min(fe.seconds, 120)
                        logger.warning(f"FloodWait in catch-up single msg: sleeping {wait_sec}s")
                        await asyncio.sleep(wait_sec)
                        try:
                            ok = await cloner_engine.clone_single_message(msg, active_pair)
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

                if pair_stopped:
                    break

            if not pair_stopped:
                await flush_album()
            if pair_stopped:
                logger.info(f"Catch-up for pair #{pair.id} stopped: the pair was paused or deleted meanwhile.")
                return {"status": "pair_inactive", "caught_up": caught_up, "failed": failed, "last_id": highest_id_processed}

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
        except Exception as e:
            if is_session_revoked_error(e):
                logger.error(f"CRITICAL: central MTProto session revoked during catch-up of pair #{pair.id} ({e!r}). Halting listener.")
                await self._handle_auth_revoked()
                return {
                    "status": "auth_revoked",
                    "caught_up": caught_up,
                    "failed": failed
                }
            if isinstance(e, ChannelPrivateError):
                logger.warning(f"Catch-up for pair #{pair.id}: the MTProto account cannot read the source "
                               f"{pair.source_channel} ({e})")
                await self._report_source_access_lost(pair, e)
                return {"status": "source_inaccessible", "caught_up": caught_up, "failed": failed}
            logger.error(f"Error during catch-up for pair #{pair.id}: {e}", exc_info=True)
            return {
                "status": f"error: {str(e)[:60]}",
                "caught_up": caught_up,
                "failed": failed
            }
        finally:
            self._catchup_in_progress.discard(pair.id)

    async def catch_up_all_active_pairs(self, max_messages: int = 150, floors: Optional[Dict[int, int]] = None) -> Dict[int, Any]:
        """
        Runs catch-up across all active channel pairs that have auto_catchup enabled.
        Safe for background invocation on bot startup and MTProto reconnection. `floors` maps pair ids to the
        watermarks snapshotted before real-time handlers were registered.
        """
        if not self.is_connected():
            logger.debug("Catch-up skipped: Telethon client not connected.")
            return {}

        active_pairs = await db_manager.get_all_active_pairs()
        logger.info(f"Running background offline gap catch-up for {len(active_pairs)} active pairs...")
        results = {}
        floors = floors or {}

        for pair in active_pairs:
            if self._shutdown:
                break
            if not getattr(pair, "auto_catchup", True):
                continue
            try:
                res = await self.catch_up_pair_messages(pair, max_messages=max_messages, min_id_floor=floors.get(pair.id))
                results[pair.id] = res
                if res.get("caught_up", 0) > 0:
                    logger.info(f"Pair #{pair.id} catch-up delivered {res['caught_up']} missed posts.")
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error catching up pair #{pair.id}: {e}")

        logger.info("Background offline gap catch-up finished across all pairs.")
        return results

telethon_listener = TelethonListener()
