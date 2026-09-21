import asyncio
import aiosqlite
import os
import shutil
import logging
import json
import threading
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any, Tuple, Set
from database.models import (
    User, ChannelPair, ClonedMessage, Subscription, Payment,
    StorySettings, PostedStory, StorySourceChannel, StoryQueueItem
)
from services.security_vault import security_vault
from services.cache_manager import cache_manager
from config.settings import settings

logger = logging.getLogger(__name__)

class ReentrantAsyncLock:
    """Async re-entrant lock allowing the same asyncio Task to acquire the lock multiple times."""
    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._depth: int = 0

    def _get_current_identity(self):
        task = asyncio.current_task()
        if task is not None:
            return task
        return f"thread_{threading.get_ident()}"

    async def acquire(self):
        caller_id = self._get_current_identity()
        if self._owner == caller_id:
            self._depth += 1
            return True
        await self._lock.acquire()
        self._owner = caller_id
        self._depth = 1
        return True

    def release(self):
        caller_id = self._get_current_identity()
        if self._owner != caller_id:
            raise RuntimeError("Cannot release unowned lock")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()

class DatabaseManager:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or getattr(settings, "DB_PATH", "database/cloner.db")
        self._conn: Optional[aiosqlite.Connection] = None
        self._init_lock = asyncio.Lock()
        self._write_lock = ReentrantAsyncLock()
        self._admin_cache: Set[int] = set()

    def _notify_pair_cache_invalidated(self):
        try:
            from services.telethon_listener import telethon_listener
            telethon_listener.invalidate_pairs_cache()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    async def _ensure_connected(self) -> aiosqlite.Connection:
        """Ensures a single persistent connection exists. Lock only guards initialization."""
        if self._conn is None:
            async with self._init_lock:
                # Double-check after acquiring lock
                if self._conn is None:
                    os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
                    self._conn = await aiosqlite.connect(self.db_path, timeout=60.0)
                    self._conn.row_factory = aiosqlite.Row
                    try:
                        await self._conn.execute("PRAGMA journal_mode = WAL;")
                        await self._conn.execute("PRAGMA synchronous = NORMAL;")
                    except Exception as e:
                        logger.warning(f"Journal mode PRAGMA skipped on filesystem ({e}), falling back to DELETE")
                        try:
                            await self._conn.execute("PRAGMA journal_mode = DELETE;")
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)

                    try:
                        await self._conn.execute("PRAGMA foreign_keys = ON;")
                        await self._conn.execute("PRAGMA busy_timeout = 60000;")
                        await self._conn.execute("PRAGMA cache_size = -64000;")
                        await self._conn.execute("PRAGMA temp_store = MEMORY;")
                        await self._conn.execute("PRAGMA mmap_size = 268435456;")
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
        return self._conn

    @asynccontextmanager
    async def get_connection(self):
        """Yields the shared persistent connection. No lock held during yield — no deadlock."""
        conn = await self._ensure_connected()
        yield conn

    @asynccontextmanager
    async def write_transaction(self):
        """Yields the connection guarded by ReentrantAsyncLock to guarantee serialized SQLite writes."""
        async with self._write_lock:
            conn = await self._ensure_connected()
            try:
                yield conn
            except Exception:
                try:
                    await conn.rollback()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
                raise

    async def close(self):
        """Closes the underlying persistent SQLite connection safely waiting for write lock"""
        async with self._write_lock:
            async with self._init_lock:
                if self._conn is not None:
                    try:
                        await self._conn.close()
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                    self._conn = None

    async def execute(self, sql: str, params: Tuple = ()):
        """Executes a single SQL query and commits transaction under write lock"""
        async with self.write_transaction() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor

    async def checkpoint(self):
        """Performs a non-blocking WAL checkpoint safely under write lock"""
        try:
            async with self.write_transaction() as db:
                await db.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except Exception as e:
            logger.warning(f"WAL checkpoint non-fatal notice: {e}")

    async def init_db(self):
        """Initialize database tables, subscriptions, payments, indexes and vault"""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        async with self.write_transaction() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_admin INTEGER DEFAULT 0,
                    is_blocked INTEGER DEFAULT 0
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id INTEGER PRIMARY KEY,
                    tier TEXT DEFAULT 'free',
                    expires_at TIMESTAMP,
                    trial_expires_at TIMESTAMP,
                    trial_notified INTEGER DEFAULT 0,
                    paid_notified INTEGER DEFAULT 0,
                    stars_spent INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    telegram_payment_charge_id TEXT UNIQUE,
                    amount INTEGER NOT NULL,
                    tier TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS channel_pairs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    source_channel TEXT NOT NULL,
                    source_title TEXT,
                    source_id INTEGER,
                    target_channel TEXT NOT NULL,
                    target_title TEXT,
                    target_id INTEGER,
                    is_active INTEGER DEFAULT 1,
                    clean_links INTEGER DEFAULT 1,
                    custom_signature TEXT DEFAULT '',
                    remove_signature INTEGER DEFAULT 0,
                    blacklist_words TEXT DEFAULT '',
                    replace_words TEXT DEFAULT '',
                    clone_mode TEXT DEFAULT 'clean',
                    auto_translate INTEGER DEFAULT 0,
                    target_lang TEXT DEFAULT 'uz',
                    source_lang TEXT DEFAULT 'auto',
                    image_watermark_type TEXT DEFAULT 'none',
                    image_watermark_text TEXT DEFAULT '',
                    image_watermark_pos TEXT DEFAULT 'bottom_right',
                    is_protected_source INTEGER DEFAULT 0,
                    affiliate_rules TEXT DEFAULT '',
                    auto_premium_emojis INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS cloned_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_id INTEGER NOT NULL,
                    source_msg_id INTEGER NOT NULL,
                    target_msg_id INTEGER,
                    media_group_id TEXT,
                    media_type TEXT DEFAULT 'text',
                    source_channel TEXT,
                    target_channel TEXT,
                    story_id INTEGER,
                    status TEXT DEFAULT 'active',
                    price REAL DEFAULT 0.0,
                    last_caption TEXT,
                    cloned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pair_id) REFERENCES channel_pairs (id) ON DELETE CASCADE
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_messages_pair_src ON cloned_messages (pair_id, source_msg_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_messages_src_chan_msg ON cloned_messages (source_channel, source_msg_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_messages_cloned_at ON cloned_messages (cloned_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_channel_pairs_user ON channel_pairs (user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_channel_pairs_source ON channel_pairs (source_channel)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS drip_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_id INTEGER NOT NULL,
                    msg_data_json TEXT NOT NULL,
                    scheduled_at TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pair_id) REFERENCES channel_pairs (id) ON DELETE CASCADE
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS channel_backups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_id INTEGER NOT NULL,
                    source_id INTEGER,
                    message_id INTEGER NOT NULL,
                    text TEXT DEFAULT '',
                    media_type TEXT DEFAULT 'none',
                    media_file_id TEXT,
                    entities_json TEXT DEFAULT '',
                    media_group_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pair_id) REFERENCES channel_pairs (id) ON DELETE CASCADE
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS fsm_storage (
                    bot_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    thread_id INTEGER DEFAULT 0,
                    destiny TEXT DEFAULT 'default',
                    state TEXT,
                    data_json TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (bot_id, chat_id, user_id, thread_id, destiny)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS whitelisted_users (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'admin',
                    note TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    user_id INTEGER PRIMARY KEY,
                    session_encrypted TEXT NOT NULL,
                    phone TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    username TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS story_settings (
                    user_id INTEGER PRIMARY KEY,
                    source_channel TEXT NOT NULL,
                    source_title TEXT DEFAULT '',
                    source_id INTEGER,
                    target_type TEXT DEFAULT 'self',
                    target_channel TEXT DEFAULT '',
                    target_id INTEGER,
                    min_price REAL DEFAULT 700.0,
                    max_price REAL DEFAULT 0.0,
                    require_photos INTEGER DEFAULT 1,
                    require_price INTEGER DEFAULT 1,
                    filter_demands INTEGER DEFAULT 1,
                    background_style TEXT DEFAULT 'telegram_green',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS posted_stories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    source_channel TEXT NOT NULL,
                    source_id INTEGER,
                    source_msg_id INTEGER NOT NULL,
                    grouped_id INTEGER DEFAULT NULL,
                    story_id INTEGER,
                    price REAL,
                    caption_snippet TEXT,
                    target_type TEXT DEFAULT 'self',
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'success'
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_posted_stories_check ON posted_stories (user_id, source_id, source_msg_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_posted_stories_chan_msg ON posted_stories (source_channel, source_msg_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_posted_stories_user_status ON posted_stories (user_id, status, id)")

            # Dynamic column migrations for posted_stories grouped_id
            try:
                await db.execute("ALTER TABLE posted_stories ADD COLUMN grouped_id INTEGER DEFAULT NULL")
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            try:
                await db.execute("CREATE INDEX IF NOT EXISTS idx_posted_stories_group ON posted_stories (user_id, grouped_id)")
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            # Multi-channel source subscriptions
            await db.execute("""
                CREATE TABLE IF NOT EXISTS story_source_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel_username TEXT NOT NULL,
                    channel_title TEXT DEFAULT '',
                    channel_id INTEGER,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_story_sources_user ON story_source_channels (user_id)")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_story_sources_user_ch ON story_source_channels (user_id, channel_username)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_story_sources_user_active ON story_source_channels (user_id, is_active)")

            # Listing fingerprint deduplication cache (prevents duplicate cross-channel listings)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS story_dedup_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    listing_hash TEXT NOT NULL,
                    source_channel TEXT DEFAULT '',
                    source_msg_id INTEGER,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_story_dedup ON story_dedup_hashes (user_id, listing_hash)")

            # Smart Drip Queue with Prime Hours & Quality Score sorting
            await db.execute("""
                CREATE TABLE IF NOT EXISTS story_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    source_channel TEXT NOT NULL,
                    source_msg_id INTEGER NOT NULL,
                    price REAL,
                    district TEXT DEFAULT '',
                    rooms INTEGER DEFAULT 0,
                    area REAL DEFAULT 0.0,
                    score INTEGER DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    scheduled_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    error_message TEXT
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_story_queue_sched ON story_queue (status, scheduled_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_story_queue_user ON story_queue (user_id, status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_story_queue_status_sched_score ON story_queue (status, scheduled_at, score)")

            # Perceptual Image Hashes for cross-channel deduplication and price drop arbitrage
            await db.execute("""
                CREATE TABLE IF NOT EXISTS image_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_id INTEGER,
                    source_channel TEXT NOT NULL,
                    source_msg_id INTEGER NOT NULL,
                    phash TEXT NOT NULL,
                    price REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_image_hashes_phash ON image_hashes (phash)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_image_hashes_source ON image_hashes (source_channel, source_msg_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_image_hashes_created ON image_hashes (created_at)")

            # Dynamic column migrations for story_settings
            story_settings_cols = [
                ("prime_hours_enabled", "INTEGER DEFAULT 1"),
                ("prime_hours_start", "INTEGER DEFAULT 9"),
                ("prime_hours_end", "INTEGER DEFAULT 22"),
                ("drip_delay_minutes", "INTEGER DEFAULT 45"),
                ("max_stories_per_day", "INTEGER DEFAULT 5"),
                ("enable_smart_badges", "INTEGER DEFAULT 1"),
                ("pin_to_profile", "INTEGER DEFAULT 1"),
                ("video_duration", "INTEGER DEFAULT 25"),
                ("enable_ai_voice", "INTEGER DEFAULT 1")
            ]
            for col, col_type in story_settings_cols:
                try:
                    await db.execute(f"ALTER TABLE story_settings ADD COLUMN {col} {col_type}")
                except Exception as e_col:
                    if "duplicate column" not in str(e_col).lower():
                        pass

            # Dynamic column migrations for users
            try:
                await db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
            except Exception as e_bl:
                if "duplicate column" not in str(e_bl).lower():
                    logger.debug(f"Column is_blocked migration note: {e_bl}")

            # Migrations for dynamic columns
            sub_columns = [
                ("trial_expires_at", "TIMESTAMP"),
                ("trial_notified", "INTEGER DEFAULT 0"),
                ("paid_notified", "INTEGER DEFAULT 0")
            ]
            for col, col_type in sub_columns:
                try:
                    await db.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {col_type}")
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        logger.debug(f"Column {col} migration note: {e}")

            pair_columns = [
                ("source_id", "INTEGER"),
                ("target_id", "INTEGER"),
                ("auto_translate", "INTEGER DEFAULT 0"),
                ("target_lang", "TEXT DEFAULT 'uz'"),
                ("source_lang", "TEXT DEFAULT 'auto'"),
                ("image_watermark_type", "TEXT DEFAULT 'none'"),
                ("image_watermark_text", "TEXT DEFAULT ''"),
                ("image_watermark_pos", "TEXT DEFAULT 'bottom_right'"),
                ("is_protected_source", "INTEGER DEFAULT 0"),
                ("affiliate_rules", "TEXT DEFAULT ''"),
                ("auto_premium_emojis", "INTEGER DEFAULT 0"),
                ("video_watermark_type", "TEXT DEFAULT 'none'"),
                ("video_watermark_text", "TEXT DEFAULT ''"),
                ("video_watermark_pos", "TEXT DEFAULT 'bottom_right'"),
                ("drip_delay_minutes", "INTEGER DEFAULT 0"),
                ("night_mode", "TEXT DEFAULT 'off'"),
                ("ai_paraphrase_mode", "TEXT DEFAULT 'off'"),
                ("tone_of_voice", "TEXT DEFAULT 'standard'"),
                ("enable_invisible_watermark", "INTEGER DEFAULT 1"),
                ("auto_cta_buttons", "INTEGER DEFAULT 0"),
                ("backup_enabled", "INTEGER DEFAULT 1"),
                ("last_seen_msg_id", "INTEGER DEFAULT NULL"),
                ("auto_catchup", "INTEGER DEFAULT 1"),
                ("source_topic_id", "INTEGER DEFAULT NULL"),
                ("target_topic_id", "INTEGER DEFAULT NULL"),
                ("ad_action", "TEXT DEFAULT 'clean'"),
                ("show_caption_above", "INTEGER DEFAULT 0")
            ]
            for col, col_type in pair_columns:
                try:
                    await db.execute(f"ALTER TABLE channel_pairs ADD COLUMN {col} {col_type}")
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        logger.debug(f"Column {col} migration note: {e}")

            # Backfill 14-day trial_expires_at for existing users if null
            try:
                await db.execute("""
                    UPDATE subscriptions
                    SET trial_expires_at = datetime(created_at, '+14 days')
                    WHERE trial_expires_at IS NULL
                """)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            try:
                await db.execute("ALTER TABLE cloned_messages ADD COLUMN media_type TEXT DEFAULT 'text'")
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            cloned_msg_cols = [
                ("source_channel", "TEXT"),
                ("target_channel", "TEXT"),
                ("story_id", "INTEGER"),
                ("status", "TEXT DEFAULT 'active'"),
                ("price", "REAL DEFAULT 0.0"),
                ("last_caption", "TEXT")
            ]
            for col, col_type in cloned_msg_cols:
                try:
                    await db.execute(f"ALTER TABLE cloned_messages ADD COLUMN {col} {col_type}")
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            try:
                await db.execute("ALTER TABLE drip_queue ADD COLUMN error_message TEXT")
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            try:
                await db.execute("ALTER TABLE channel_backups ADD COLUMN media_group_id TEXT")
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            try:
                await db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            await db.execute("CREATE INDEX IF NOT EXISTS idx_pairs_user ON channel_pairs(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_pairs_active ON channel_pairs(is_active)")
            cur_cloned_idx = await db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_cloned_unique_pair_msg'")
            if not await cur_cloned_idx.fetchone():
                try:
                    await db.execute("""
                        DELETE FROM cloned_messages
                        WHERE id NOT IN (
                            SELECT MAX(id)
                            FROM cloned_messages
                            GROUP BY pair_id, source_msg_id
                        )
                    """)
                    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cloned_unique_pair_msg ON cloned_messages(pair_id, source_msg_id)")
                except Exception as e:
                    logger.warning(f"Failed to create idx_cloned_unique_pair_msg: {e}")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_media_group ON cloned_messages(pair_id, media_group_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_time ON cloned_messages(cloned_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_source_chan_msg ON cloned_messages(source_channel, source_msg_id)")
            cur_bkp_idx = await db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_backups_pair_msg'")
            if not await cur_bkp_idx.fetchone():
                try:
                    await db.execute("""
                        DELETE FROM channel_backups
                        WHERE id NOT IN (
                            SELECT MAX(id)
                            FROM channel_backups
                            GROUP BY pair_id, message_id
                        )
                    """)
                    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_backups_pair_msg ON channel_backups(pair_id, message_id)")
                except Exception as e:
                    logger.warning(f"Failed to create idx_backups_pair_msg: {e}")
            try:
                await db.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_charge_id 
                    ON payments(telegram_payment_charge_id) 
                    WHERE telegram_payment_charge_id IS NOT NULL 
                      AND telegram_payment_charge_id != ''
                      AND telegram_payment_charge_id NOT LIKE 'admin_manual_grant_%'
                """)
            except Exception as e_pay_idx:
                logger.warning(f"Failed to create idx_payments_charge_id: {e_pay_idx}")
            # Ensure all configured admins have permanent VIP tier and is_admin=1
            for admin_id in settings.admin_ids:
                try:
                    await db.execute(
                        "INSERT INTO users (user_id, full_name, username, is_admin) VALUES (?, 'Super Admin', 'admin', 1) "
                        "ON CONFLICT(user_id) DO UPDATE SET is_admin = 1",
                        (admin_id,)
                    )
                    await db.execute(
                        "INSERT INTO subscriptions (user_id, tier, expires_at) VALUES (?, 'vip', '2099-12-31T23:59:59') "
                        "ON CONFLICT(user_id) DO UPDATE SET tier = 'vip', expires_at = '2099-12-31T23:59:59'",
                        (admin_id,)
                    )
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            await db.commit()

            # Populate admin cache
            self._admin_cache = set(settings.admin_ids)
            try:
                cur_admins = await db.execute("SELECT user_id FROM users WHERE is_admin = 1")
                admin_rows = await cur_admins.fetchall()
                for ar in admin_rows:
                    self._admin_cache.add(ar[0])
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            # Automatically deduplicate redundant channel pairs and clean invalid loop pairs
            await self._deduplicate_existing_pairs(db)

            # Preload recent 100,000 cloned message IDs into in-memory deduplication cache
            cur = await db.execute("SELECT pair_id, source_msg_id FROM cloned_messages ORDER BY id DESC LIMIT 100000")
            rows = await cur.fetchall()
            if rows:
                await cache_manager.dedup_cache.add_batch((r[0], r[1]) for r in rows)

            logger.info(f"Database initialized for 100k high-load. Preloaded {len(rows)} message IDs into LRU cache.")

    # --- SUBSCRIPTIONS & TELEGRAM STARS ---

    async def is_vip(self, user_id: int) -> bool:
        """
        Checks whether the user has an active VIP subscription or is an administrator.
        Super Admins always have permanent VIP privileges.
        """
        if user_id in settings.admin_ids or self.is_admin_sync(user_id) or await self.is_admin(user_id):
            return True
        sub = await self.get_user_subscription(user_id)
        return bool(sub and sub.is_vip)

    async def get_user_subscription(self, user_id: int) -> Subscription:
        if user_id in settings.admin_ids or self.is_admin_sync(user_id):
            return Subscription(
                user_id=user_id,
                tier="vip",
                expires_at="2099-12-31T23:59:59",
                trial_expires_at=None,
                trial_notified=False,
                stars_spent=0,
                created_at="2020-01-01T00:00:00"
            )

        cached = await cache_manager.sub_cache.get(f"sub_{user_id}")
        if cached:
            return cached

        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()

        if row:
            keys = row.keys()
            trial_exp = row["trial_expires_at"] if "trial_expires_at" in keys else None
            trial_notified = bool(row["trial_notified"]) if "trial_notified" in keys else False
            paid_notified = bool(row["paid_notified"]) if "paid_notified" in keys else False
            
            # If trial_expires_at is null, set to created_at + 14 days
            if not trial_exp and row["created_at"]:
                try:
                    c_date = datetime.fromisoformat(row["created_at"].replace(" ", "T"))
                    trial_exp = (c_date + timedelta(days=14)).isoformat()
                    async with self.write_transaction() as wdb:
                        await wdb.execute("UPDATE subscriptions SET trial_expires_at = ? WHERE user_id = ?", (trial_exp, user_id))
                        await wdb.commit()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            sub = Subscription(
                user_id=row["user_id"],
                tier=row["tier"],
                expires_at=row["expires_at"],
                trial_expires_at=trial_exp,
                trial_notified=trial_notified,
                paid_notified=paid_notified,
                stars_spent=row["stars_spent"],
                created_at=row["created_at"]
            )
            if sub.tier != "free" and not sub.is_active:
                async with self.write_transaction() as wdb:
                    await wdb.execute("UPDATE subscriptions SET tier = 'free' WHERE user_id = ?", (user_id,))
                    await wdb.execute("UPDATE channel_pairs SET is_active = 0 WHERE user_id = ?", (user_id,))
                    await wdb.commit()
                self._notify_pair_cache_invalidated()
                sub.tier = "free"
            elif not sub.is_active:
                async with self.write_transaction() as wdb:
                    await wdb.execute("UPDATE channel_pairs SET is_active = 0 WHERE user_id = ?", (user_id,))
                    await wdb.commit()
                self._notify_pair_cache_invalidated()
            await cache_manager.sub_cache.set(f"sub_{user_id}", sub)
            return sub
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            trial_exp = (now + timedelta(days=14)).isoformat()
            async with self.write_transaction() as wdb:
                await wdb.execute(
                    "INSERT OR IGNORE INTO users (user_id, full_name) VALUES (?, ?)",
                    (user_id, f"User {user_id}")
                )
                await wdb.execute(
                    "INSERT INTO subscriptions (user_id, tier, trial_expires_at) VALUES (?, 'free', ?) "
                    "ON CONFLICT(user_id) DO NOTHING",
                    (user_id, trial_exp)
                )
                await wdb.commit()
            sub = Subscription(user_id=user_id, tier="free", trial_expires_at=trial_exp, created_at=now.isoformat())
            await cache_manager.sub_cache.set(f"sub_{user_id}", sub)
            return sub

    async def is_payment_processed(self, charge_id: str) -> bool:
        """Checks whether a Telegram Stars payment charge_id has already been recorded in payments"""
        if not charge_id or charge_id.startswith("admin_manual_grant_"):
            return False
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT 1 FROM payments WHERE telegram_payment_charge_id = ?", (charge_id,))
            return bool(await cursor.fetchone())

    async def activate_subscription(
        self,
        user_id: int,
        tier: str,
        stars: int,
        charge_id: str,
        days: int = 30
    ) -> Subscription:
        await cache_manager.sub_cache.delete(f"sub_{user_id}")
        is_duplicate = False
        if charge_id and not charge_id.startswith("admin_manual_grant_"):
            async with self.get_connection() as db_ro:
                cur_chk = await db_ro.execute("SELECT 1 FROM payments WHERE telegram_payment_charge_id = ?", (charge_id,))
                if await cur_chk.fetchone():
                    logger.warning(f"Payment charge_id {charge_id} already processed. Skipping duplicate activation.")
                    return await self.get_user_subscription(user_id)

        try:
            async with self.write_transaction() as db:
                try:
                    await db.execute(
                        "INSERT INTO payments (user_id, telegram_payment_charge_id, amount, tier) VALUES (?, ?, ?, ?)",
                        (user_id, charge_id, stars, tier)
                    )
                except Exception as e_pay_ins:
                    if "unique" in str(e_pay_ins).lower() or "constraint" in str(e_pay_ins).lower():
                        logger.warning(f"Payment charge_id {charge_id} duplicate caught by unique index. Returning existing sub.")
                        is_duplicate = True
                    else:
                        raise

                if is_duplicate:
                    pass
                else:
                    cursor = await db.execute("SELECT tier, expires_at, stars_spent, trial_expires_at FROM subscriptions WHERE user_id = ?", (user_id,))
                    row = await cursor.fetchone()

                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    new_exp = now + timedelta(days=days)
                    old_spent = 0
                    trial_exp = None
                    final_tier = tier

                    if row:
                        curr_tier = row[0]
                        curr_exp = None
                        if row[1]:
                            try:
                                curr_exp = Subscription._parse_iso_to_utc_naive(row[1])
                                if curr_exp and curr_exp > now:
                                    new_exp = curr_exp + timedelta(days=days)
                            except Exception:
                                logger.debug("Ignored exception", exc_info=True)
                        old_spent = row[2] or 0
                        trial_exp = row[3] if len(row) > 3 else None

                    exp_str = new_exp.isoformat()
                    if final_tier == "free":
                        trial_exp = exp_str
                    total_spent = old_spent + stars

                    await db.execute("""
                        INSERT INTO subscriptions (user_id, tier, expires_at, trial_expires_at, stars_spent, trial_notified, paid_notified)
                        VALUES (?, ?, ?, ?, ?, 0, 0)
                        ON CONFLICT(user_id) DO UPDATE SET
                            tier = excluded.tier,
                            expires_at = excluded.expires_at,
                            stars_spent = excluded.stars_spent,
                            trial_notified = 0,
                            paid_notified = 0
                    """, (user_id, final_tier, exp_str, trial_exp, total_spent))

                    sub = Subscription(user_id=user_id, tier=final_tier, expires_at=exp_str, trial_expires_at=trial_exp, trial_notified=False, paid_notified=False, stars_spent=total_spent)
                    limit = sub.max_channels
                    if limit > 0:
                        await db.execute("""
                            UPDATE channel_pairs
                            SET is_active = 1
                            WHERE id IN (
                                SELECT id FROM channel_pairs
                                WHERE user_id = ?
                                ORDER BY id ASC
                                LIMIT ?
                            )
                        """, (user_id, limit))

                    await db.commit()
                    await cache_manager.sub_cache.set(f"sub_{user_id}", sub)
                    self._notify_pair_cache_invalidated()
                    return sub
        except Exception as e_act:
            if not is_duplicate:
                raise

        if is_duplicate:
            return await self.get_user_subscription(user_id)

    async def revoke_subscription(self, user_id: int) -> Subscription:
        """Revokes paid subscription, resets user tier to free, deactivates active pairs, and invalidates cache"""
        await cache_manager.sub_cache.delete(f"sub_{user_id}")
        async with self.write_transaction() as db:
            await db.execute("UPDATE subscriptions SET tier = 'free', expires_at = NULL, paid_notified = 0 WHERE user_id = ?", (user_id,))
            await db.execute("UPDATE channel_pairs SET is_active = 0 WHERE user_id = ?", (user_id,))
            try:
                await db.execute("UPDATE story_settings SET is_active = 0 WHERE user_id = ?", (user_id,))
                await db.execute("UPDATE story_queue SET status = 'skipped', error_message = 'VIP obunasi bekor qilingan' WHERE user_id = ? AND status = 'pending'", (user_id,))
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            await db.commit()
        self._notify_pair_cache_invalidated()
        return await self.get_user_subscription(user_id)

    async def get_expired_trial_users_to_notify(self) -> List[Tuple[int, str]]:
        now_str = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT s.user_id, u.full_name
                FROM subscriptions s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.tier = 'free'
                  AND s.trial_expires_at IS NOT NULL
                  AND s.trial_expires_at <= ?
                  AND s.trial_notified = 0
            """, (now_str,))
            rows = await cursor.fetchall()
            return [(r["user_id"], r["full_name"]) for r in rows]

    async def mark_trial_notified(self, user_id: int):
        await cache_manager.sub_cache.delete(f"sub_{user_id}")
        async with self.write_transaction() as db:
            await db.execute("""
                INSERT INTO subscriptions (user_id, tier, trial_notified)
                VALUES (?, 'free', 1)
                ON CONFLICT(user_id) DO UPDATE SET trial_notified = 1
            """, (user_id,))
            await db.commit()

    async def get_expiring_paid_users_to_notify(self) -> List[Tuple[int, str, str, str]]:
        """Returns users whose PRO/VIP subscription expires within 3 days or has expired and needs notification"""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        three_days_later = (now + timedelta(days=3)).isoformat()
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT s.user_id, u.full_name, s.tier, s.expires_at
                FROM subscriptions s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.tier IN ('pro', 'vip')
                  AND s.expires_at IS NOT NULL
                  AND s.expires_at <= ?
                  AND s.paid_notified = 0
            """, (three_days_later,))
            rows = await cursor.fetchall()
            return [(r["user_id"], r["full_name"], r["tier"], r["expires_at"]) for r in rows]

    async def mark_paid_sub_notified(self, user_id: int):
        await cache_manager.sub_cache.delete(f"sub_{user_id}")
        async with self.write_transaction() as db:
            await db.execute("""
                UPDATE subscriptions SET paid_notified = 1 WHERE user_id = ?
            """, (user_id,))
            await db.commit()

    async def can_user_add_channel(self, user_id: int, is_admin: bool = False) -> Tuple[bool, int, int]:
        pairs = await self.get_user_channel_pairs(user_id)
        current_count = len(pairs)

        if is_admin or await self.is_admin(user_id):
            return True, 999, current_count

        sub = await self.get_user_subscription(user_id)
        max_allowed = sub.max_channels

        can_add = sub.is_active and (current_count < max_allowed)
        return can_add, max_allowed, current_count

    # --- APP SETTINGS & ENCRYPTED VAULT ---

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        cached = await cache_manager.settings_cache.get(f"set_{key}")
        if cached is not None:
            return cached

        async with self.get_connection() as db:
            cursor = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
            if not row:
                return default
            val = row[0]
            if key == "telethon_session":
                decrypted = security_vault.decrypt_secret(val)
                if not decrypted:
                    return default
                await cache_manager.settings_cache.set(f"set_{key}", decrypted)
                return decrypted
            await cache_manager.settings_cache.set(f"set_{key}", val)
            return val

    async def set_setting(self, key: str, value: str):
        await cache_manager.settings_cache.delete(f"set_{key}")
        store_value = value
        if key == "telethon_session":
            store_value = security_vault.encrypt_secret(value)

        async with self.write_transaction() as db:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                (key, store_value)
            )
            await db.commit()

    async def delete_setting(self, key: str):
        await cache_manager.settings_cache.delete(f"set_{key}")
        async with self.write_transaction() as db:
            await db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            await db.commit()

    # --- BOT ACCESS MODE & WHITELIST ---

    async def is_private_mode(self) -> bool:
        """Returns True if the bot is in private/whitelisted mode, False if public"""
        mode = await self.get_setting("bot_access_mode", "public")
        return mode == "private"

    async def set_private_mode(self, enabled: bool):
        """Sets bot access mode to private or public and records activation timestamp"""
        mode_str = "private" if enabled else "public"
        await self.set_setting("bot_access_mode", mode_str)
        if enabled:
            existing_ts = await self.get_setting("private_mode_enabled_at")
            if not existing_ts:
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                await self.set_setting("private_mode_enabled_at", now_str)
        else:
            await self.delete_setting("private_mode_enabled_at")

    async def get_support_username(self) -> str:
        """Returns configured admin support username with fallback"""
        custom = await self.get_setting("support_username")
        if custom and custom.strip():
            return custom.strip().lstrip("@")
        if getattr(settings, "SUPPORT_USERNAME", None):
            return settings.SUPPORT_USERNAME.strip().lstrip("@")
        return "admin"

    async def set_support_username(self, username: str):
        """Sets admin support username in database"""
        clean_user = username.strip().lstrip("@")
        await self.set_setting("support_username", clean_user)

    async def is_user_whitelisted(self, user_id: int) -> bool:
        """Checks if a user is in the whitelist"""
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT 1 FROM whitelisted_users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return row is not None

    async def add_user_to_whitelist(self, user_id: int, added_by: int = 0, source: str = "admin", note: str = "") -> bool:
        """Adds a user to the whitelist table"""
        async with self.write_transaction() as db:
            await db.execute(
                "INSERT INTO whitelisted_users (user_id, added_by, source, note, created_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(user_id) DO UPDATE SET source = excluded.source, note = excluded.note",
                (user_id, added_by, source, note)
            )
            await db.commit()
            return True

    async def remove_user_from_whitelist(self, user_id: int) -> bool:
        """Removes a user from the whitelist"""
        async with self.write_transaction() as db:
            cursor = await db.execute("DELETE FROM whitelisted_users WHERE user_id = ?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def get_whitelisted_users(self, source: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns list of whitelisted users joined with user details"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            if source:
                query = """
                    SELECT w.user_id, w.added_by, w.source, w.note, w.created_at,
                           u.full_name, u.username
                    FROM whitelisted_users w
                    LEFT JOIN users u ON w.user_id = u.user_id
                    WHERE w.source = ?
                    ORDER BY w.created_at DESC
                """
                cursor = await db.execute(query, (source,))
            else:
                query = """
                    SELECT w.user_id, w.added_by, w.source, w.note, w.created_at,
                           u.full_name, u.username
                    FROM whitelisted_users w
                    LEFT JOIN users u ON w.user_id = u.user_id
                    ORDER BY w.created_at DESC
                """
                cursor = await db.execute(query)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def can_user_access_bot(self, user_id: int) -> bool:
        """
        Determines whether a user is permitted to use the bot.
        Returns True if:
        1. Bot is in public mode
        2. User is super admin or admin
        3. User is explicitly whitelisted
        4. "Hozir obunasi borlar": User has an active subscription (Pro, VIP, or active trial)
        5. "Hozirgacha botimizni ishlatayotganlar": User already has connected channels / pairs
        6. User was registered before private mode was enabled (existing legacy user)
        """
        if not await self.is_private_mode():
            return True
        if user_id in settings.admin_ids or await self.is_admin(user_id):
            return True
        if await self.is_user_whitelisted(user_id):
            return True

        # Active paid subscribers (Pro or VIP) always retain access
        sub = await self.get_user_subscription(user_id)
        if sub and sub.is_active and sub.tier in ("pro", "vip"):
            return True

        # Existing users with configured channels
        pairs = await self.get_user_channel_pairs(user_id)
        if pairs:
            return True

        # Grandfather users who joined before private mode was activated
        private_enabled_at = await self.get_setting("private_mode_enabled_at")
        if private_enabled_at:
            user_data = await self.get_user_by_id(user_id)
            if user_data and user_data.created_at:
                if str(user_data.created_at) < str(private_enabled_at):
                    return True
        else:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            await self.set_setting("private_mode_enabled_at", now_str)

        return False

    # --- USERS ---

    async def get_or_create_user(self, user_id: int, full_name: str, username: Optional[str] = None, is_admin: Optional[bool] = None) -> User:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                final_username = username if username else row["username"]
                admin_flag = 1 if (is_admin is True or user_id in settings.admin_ids or bool(row["is_admin"])) else 0

                if admin_flag:
                    self._admin_cache.add(user_id)
                else:
                    self._admin_cache.discard(user_id)

                await db.execute(
                    "UPDATE users SET full_name = ?, username = ?, is_admin = ?, is_blocked = 0 WHERE user_id = ?",
                    (full_name, final_username, admin_flag, user_id)
                )
                await db.commit()
                return User(
                    user_id=row["user_id"],
                    full_name=full_name,
                    username=final_username,
                    created_at=row["created_at"],
                    is_admin=bool(admin_flag),
                    is_blocked=False
                )
            else:
                admin_flag = 1 if ((is_admin is True) or user_id in settings.admin_ids) else 0
                if admin_flag:
                    self._admin_cache.add(user_id)
                else:
                    self._admin_cache.discard(user_id)

                await db.execute(
                    "INSERT INTO users (user_id, full_name, username, is_admin, is_blocked) VALUES (?, ?, ?, ?, 0)",
                    (user_id, full_name, username, admin_flag)
                )
                await db.commit()
                return User(
                    user_id=user_id,
                    full_name=full_name,
                    username=username,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    is_admin=bool(admin_flag),
                    is_blocked=False
                )

    add_user = get_or_create_user

    async def set_admin_status(self, user_id: int, is_admin: bool) -> bool:
        """Sets or revokes admin privileges for user_id. Super admins in settings.admin_ids cannot be revoked."""
        if not is_admin and (user_id == settings.PRIMARY_SUPER_ADMIN_ID or user_id in settings.admin_ids):
            logger.warning(f"Super admin {user_id} cannot be revoked.")
            return False

        flag = 1 if is_admin else 0
        if is_admin:
            self._admin_cache.add(user_id)
        else:
            self._admin_cache.discard(user_id)

        async with self.write_transaction() as db:
            await db.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (flag, user_id))
            await db.commit()
        return True

    async def mark_user_blocked(self, user_id: int, is_blocked: bool = True):
        """Marks user as blocked/deactivated or unblocked in database"""
        await cache_manager.seen_users_cache.delete(f"seen_{user_id}")
        async with self.write_transaction() as db:
            await db.execute("UPDATE users SET is_blocked = ? WHERE user_id = ?", (1 if is_blocked else 0, user_id))
            await db.commit()

    async def is_user_blocked(self, user_id: int) -> bool:
        """Checks if user is marked blocked in database"""
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return bool(row[0]) if row and row[0] is not None else False

    async def get_user_by_id(self, user_id: int) -> Optional[User]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                return User(
                    user_id=row["user_id"],
                    full_name=row["full_name"],
                    username=row["username"],
                    created_at=row["created_at"],
                    is_admin=bool(row["is_admin"]),
                    is_blocked=bool(row["is_blocked"]) if "is_blocked" in row.keys() else False
                )
            return None

    get_user = get_user_by_id

    async def get_user_by_username(self, username: str) -> Optional[User]:
        clean_user = username.strip().lstrip("@").lower()
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE LOWER(username) = ?", (clean_user,))
            row = await cursor.fetchone()
            if row:
                return User(
                    user_id=row["user_id"],
                    full_name=row["full_name"],
                    username=row["username"],
                    created_at=row["created_at"],
                    is_admin=bool(row["is_admin"]),
                    is_blocked=bool(row["is_blocked"]) if "is_blocked" in row.keys() else False
                )
            return None

    def is_admin_sync(self, user_id: Any) -> bool:
        try:
            uid = int(user_id)
        except (ValueError, TypeError):
            return False
        return uid == settings.PRIMARY_SUPER_ADMIN_ID or uid in settings.admin_ids or uid in self._admin_cache

    async def is_admin(self, user_id: Any) -> bool:
        try:
            uid = int(user_id)
        except (ValueError, TypeError):
            return False
        if uid == settings.PRIMARY_SUPER_ADMIN_ID or uid in settings.admin_ids:
            return True
        user = await self.get_user_by_id(uid)
        if user and user.is_admin:
            self._admin_cache.add(uid)
            return True
        self._admin_cache.discard(uid)
        return False

    async def get_all_user_ids(self) -> List[int]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT user_id FROM users")
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def get_all_users(self, active_only: bool = True, limit: Optional[int] = None) -> List[User]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT * FROM users WHERE is_blocked = 0 ORDER BY created_at DESC" if active_only else "SELECT * FROM users ORDER BY created_at DESC"
            if limit:
                sql += f" LIMIT {int(limit)}"
            cursor = await db.execute(sql)
            rows = await cursor.fetchall()
            return [
                User(
                    user_id=r["user_id"],
                    full_name=r["full_name"],
                    username=r["username"],
                    created_at=r["created_at"],
                    is_admin=bool(r["is_admin"])
                ) for r in rows
            ]

    async def get_users_detailed(self, limit: int = 500) -> List[Dict[str, Any]]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT u.user_id, u.full_name, u.username, u.created_at,
                       COALESCE(s.tier, 'free') as tier,
                       s.expires_at, s.trial_expires_at,
                       COUNT(DISTINCT p.id) as channel_count
                FROM users u
                LEFT JOIN subscriptions s ON u.user_id = s.user_id
                LEFT JOIN channel_pairs p ON u.user_id = p.user_id
                GROUP BY u.user_id
                ORDER BY u.created_at DESC
                LIMIT ?
            """, (limit,))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def search_users(self, query: str) -> List[Dict[str, Any]]:
        """Searches users by numeric user_id, @username, or full name"""
        clean_q = query.strip().lstrip("@")
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            if clean_q.lstrip("-").isdigit():
                cursor = await db.execute("""
                    SELECT u.user_id, u.full_name, u.username, u.created_at,
                           COALESCE(s.tier, 'free') as tier,
                           s.expires_at, s.trial_expires_at,
                           COUNT(DISTINCT p.id) as channel_count
                    FROM users u
                    LEFT JOIN subscriptions s ON u.user_id = s.user_id
                    LEFT JOIN channel_pairs p ON u.user_id = p.user_id
                    WHERE u.user_id = ?
                    GROUP BY u.user_id
                """, (int(clean_q),))
            else:
                # Escape LIKE wildcards to prevent LIKE injection
                escaped_q = clean_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped_q}%"
                cursor = await db.execute("""
                    SELECT u.user_id, u.full_name, u.username, u.created_at,
                           COALESCE(s.tier, 'free') as tier,
                           s.expires_at, s.trial_expires_at,
                           COUNT(DISTINCT p.id) as channel_count
                    FROM users u
                    LEFT JOIN subscriptions s ON u.user_id = s.user_id
                    LEFT JOIN channel_pairs p ON u.user_id = p.user_id
                    WHERE u.username LIKE ? ESCAPE '\\' OR u.full_name LIKE ? ESCAPE '\\'
                    GROUP BY u.user_id
                    LIMIT 20
                """, (pattern, pattern))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


    async def is_user_admin(self, user_id: int) -> bool:
        if user_id in settings.admin_ids:
            return True
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

    # --- CHANNEL PAIRS ---

    @staticmethod
    def _normalize_channel_name(channel: Optional[str]) -> str:
        if not channel:
            return ""
        ch = str(channel).strip()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if ch.startswith(prefix):
                ch = ch[len(prefix):]
                break
        ch = ch.strip()
        if ch.startswith("+") or ch.startswith("joinchat/"):
            # Private invite hashes are case-sensitive Base64 - preserve case
            return ch
        ch = ch.lstrip("@").strip()
        return ch.lower()

    async def _deduplicate_existing_pairs(self, db):
        """
        Scans channel_pairs for duplicate configurations for the same user,
        keeps the earliest (canonical) pair, and cleans up redundant duplicates.
        Also cleans up any accidental self-cloning loop pairs.
        """
        try:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM channel_pairs ORDER BY id ASC")
            all_pairs = await cursor.fetchall()
            if not all_pairs:
                return

            seen_user_pairs = {}
            pairs_to_delete = []

            for row in all_pairs:
                user_id = row["user_id"]
                pair_id = row["id"]
                s_ch = self._normalize_channel_name(row["source_channel"] or "")
                t_ch = self._normalize_channel_name(row["target_channel"] or "")
                s_id = row["source_id"]
                t_id = row["target_id"]

                # Check if self-loop pair
                is_self_loop = False
                if s_id is not None and t_id is not None and s_id == t_id:
                    is_self_loop = True
                elif s_ch and t_ch and s_ch == t_ch:
                    is_self_loop = True

                if is_self_loop:
                    pairs_to_delete.append(pair_id)
                    continue

                src_key = f"id:{s_id}" if s_id else f"ch:{s_ch}"
                tgt_key = f"id:{t_id}" if t_id else f"ch:{t_ch}"
                group_key = (user_id, src_key, tgt_key)
                alt_group_key = (user_id, f"ch:{s_ch}", f"ch:{t_ch}") if (s_ch and t_ch) else None

                if group_key in seen_user_pairs or (alt_group_key and alt_group_key in seen_user_pairs):
                    pairs_to_delete.append(pair_id)
                else:
                    seen_user_pairs[group_key] = pair_id
                    if alt_group_key:
                        seen_user_pairs[alt_group_key] = pair_id

            if pairs_to_delete:
                logger.info(f"Removing {len(pairs_to_delete)} duplicate/invalid channel pairs from database: {pairs_to_delete}")
                params = [(p,) for p in pairs_to_delete]
                await db.executemany("DELETE FROM cloned_messages WHERE pair_id = ?", params)
                await db.executemany("DELETE FROM channel_backups WHERE pair_id = ?", params)
                await db.executemany("DELETE FROM drip_queue WHERE pair_id = ?", params)
                await db.executemany("DELETE FROM channel_pairs WHERE id = ?", params)
                await db.commit()
        except Exception as e:
            logger.error(f"Error during channel pair deduplication: {e}", exc_info=True)

    async def find_duplicate_pair(
        self,
        user_id: int,
        source_channel: str,
        target_channel: str,
        source_id: Optional[int] = None,
        target_id: Optional[int] = None
    ) -> Optional[ChannelPair]:
        """
        Finds an existing active or inactive channel pair for this user that connects
        the same source to the same target. Matches either by Telegram numeric chat IDs
        or normalized channel usernames/links.
        """
        pairs = await self.get_user_channel_pairs(user_id)
        norm_s = self._normalize_channel_name(source_channel)
        norm_t = self._normalize_channel_name(target_channel)

        for p in pairs:
            # Check numeric ID match if available
            src_id_match = False
            if source_id is not None and p.source_id is not None and source_id == p.source_id:
                src_id_match = True

            tgt_id_match = False
            if target_id is not None and p.target_id is not None and target_id == p.target_id:
                tgt_id_match = True

            # Check normalized name / handle match
            p_norm_s = self._normalize_channel_name(p.source_channel)
            p_norm_t = self._normalize_channel_name(p.target_channel)

            name_s_match = bool(norm_s and p_norm_s and norm_s == p_norm_s)
            name_t_match = bool(norm_t and p_norm_t and norm_t == p_norm_t)

            # Check if source_channel / target_channel matches numeric ID stored as string
            if source_id is not None and p_norm_s == str(source_id):
                src_id_match = True
            if target_id is not None and p_norm_t == str(target_id):
                tgt_id_match = True
            if p.source_id is not None and norm_s == str(p.source_id):
                src_id_match = True
            if p.target_id is not None and norm_t == str(p.target_id):
                tgt_id_match = True

            source_matches = src_id_match or name_s_match
            target_matches = tgt_id_match or name_t_match

            if source_matches and target_matches:
                return p

        return None

    async def add_channel_pair(
        self,
        user_id: Any,
        source_channel: Optional[str] = None,
        source_title: Optional[str] = None,
        target_channel: Optional[str] = None,
        target_title: Optional[str] = None,
        source_id: Optional[int] = None,
        target_id: Optional[int] = None,
        clean_links: bool = True,
        custom_signature: str = "",
        remove_signature: bool = False,
        blacklist_words: str = "",
        replace_words: str = "",
        clone_mode: str = "clean",
        auto_translate: bool = False,
        target_lang: str = "uz",
        image_watermark_type: str = "none",
        image_watermark_text: str = "",
        image_watermark_pos: str = "bottom_right",
        is_protected_source: bool = False,
        affiliate_rules: str = "",
        auto_premium_emojis: bool = False,
        video_watermark_type: str = "none",
        video_watermark_text: str = "",
        video_watermark_pos: str = "bottom_right",
        drip_delay_minutes: int = 0,
        night_mode: str = "off",
        ai_paraphrase_mode: str = "off",
        auto_cta_buttons: bool = False,
        backup_enabled: bool = True,
        source_topic_id: Optional[int] = None,
        target_topic_id: Optional[int] = None,
        ad_action: str = "clean",
        show_caption_above: bool = False
    ) -> int:
        if hasattr(user_id, 'source_channel'):
            pair_obj = user_id
            user_id = pair_obj.user_id
            source_channel = pair_obj.source_channel
            source_title = pair_obj.source_title or pair_obj.source_channel
            target_channel = pair_obj.target_channel
            target_title = pair_obj.target_title or pair_obj.target_channel
            source_id = getattr(pair_obj, 'source_id', source_id)
            target_id = getattr(pair_obj, 'target_id', target_id)
            clean_links = getattr(pair_obj, 'clean_links', clean_links)
            custom_signature = getattr(pair_obj, 'custom_signature', custom_signature)
            remove_signature = getattr(pair_obj, 'remove_signature', remove_signature)
            blacklist_words = getattr(pair_obj, 'blacklist_words', blacklist_words)
            replace_words = getattr(pair_obj, 'replace_words', replace_words)
            clone_mode = getattr(pair_obj, 'clone_mode', clone_mode)
            auto_translate = getattr(pair_obj, 'auto_translate', auto_translate)
            target_lang = getattr(pair_obj, 'target_lang', target_lang)
            image_watermark_type = getattr(pair_obj, 'image_watermark_type', image_watermark_type)
            image_watermark_text = getattr(pair_obj, 'image_watermark_text', image_watermark_text)
            image_watermark_pos = getattr(pair_obj, 'image_watermark_pos', image_watermark_pos)
            is_protected_source = getattr(pair_obj, 'is_protected_source', is_protected_source)
            affiliate_rules = getattr(pair_obj, 'affiliate_rules', affiliate_rules)
            auto_premium_emojis = getattr(pair_obj, 'auto_premium_emojis', auto_premium_emojis)
            video_watermark_type = getattr(pair_obj, 'video_watermark_type', video_watermark_type)
            video_watermark_text = getattr(pair_obj, 'video_watermark_text', video_watermark_text)
            video_watermark_pos = getattr(pair_obj, 'video_watermark_pos', video_watermark_pos)
            drip_delay_minutes = getattr(pair_obj, 'drip_delay_minutes', drip_delay_minutes)
            night_mode = getattr(pair_obj, 'night_mode', night_mode)
            ai_paraphrase_mode = getattr(pair_obj, 'ai_paraphrase_mode', ai_paraphrase_mode)
            auto_cta_buttons = getattr(pair_obj, 'auto_cta_buttons', auto_cta_buttons)
            backup_enabled = getattr(pair_obj, 'backup_enabled', backup_enabled)
            source_topic_id = getattr(pair_obj, 'source_topic_id', source_topic_id)
            target_topic_id = getattr(pair_obj, 'target_topic_id', target_topic_id)
            ad_action = getattr(pair_obj, 'ad_action', ad_action)
            show_caption_above = getattr(pair_obj, 'show_caption_above', show_caption_above)

        existing = await self.find_duplicate_pair(user_id, source_channel, target_channel, source_id, target_id)
        if existing:
            if (source_id and not existing.source_id) or (target_id and not existing.target_id):
                await self.update_pair_ids(existing.id, source_id or existing.source_id, target_id or existing.target_id)
            if (source_title and source_title != existing.source_title) or (target_title and target_title != existing.target_title):
                async with self.write_transaction() as db:
                    await db.execute(
                        "UPDATE channel_pairs SET source_title = COALESCE(?, source_title), target_title = COALESCE(?, target_title) WHERE id = ?",
                        (source_title or None, target_title or None, existing.id)
                    )
                    await db.commit()
            self._notify_pair_cache_invalidated()
            return existing.id

        async with self.write_transaction() as db:

            cursor = await db.execute("""
                INSERT INTO channel_pairs (
                    user_id, source_channel, source_title, source_id, target_channel, target_title, target_id,
                    clean_links, custom_signature, remove_signature, blacklist_words, replace_words, clone_mode,
                    auto_translate, target_lang, image_watermark_type, image_watermark_text, image_watermark_pos,
                    is_protected_source, affiliate_rules, auto_premium_emojis, video_watermark_type, video_watermark_text,
                    video_watermark_pos, drip_delay_minutes, night_mode, ai_paraphrase_mode,
                    auto_cta_buttons, backup_enabled, source_topic_id, target_topic_id, ad_action, show_caption_above
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, source_channel, source_title, source_id, target_channel, target_title, target_id,
                1 if clean_links else 0, custom_signature, 1 if remove_signature else 0, blacklist_words, replace_words, clone_mode,
                1 if auto_translate else 0, target_lang, image_watermark_type, image_watermark_text, image_watermark_pos,
                1 if is_protected_source else 0, affiliate_rules, 1 if auto_premium_emojis else 0, video_watermark_type, video_watermark_text,
                video_watermark_pos, drip_delay_minutes, night_mode, ai_paraphrase_mode,
                1 if auto_cta_buttons else 0, 1 if backup_enabled else 0,
                source_topic_id, target_topic_id, ad_action, 1 if show_caption_above else 0
            ))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return cursor.lastrowid

    async def get_user_channel_pairs(self, user_id: int) -> List[ChannelPair]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM channel_pairs WHERE user_id = ? ORDER BY id DESC", (user_id,))
            rows = await cursor.fetchall()
            return [self._row_to_pair(row) for row in rows]

    async def get_pair_by_id(self, pair_id: int) -> Optional[ChannelPair]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            return self._row_to_pair(row) if row else None

    async def get_all_active_pairs(self) -> List[ChannelPair]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM channel_pairs WHERE is_active = 1")
            rows = await cursor.fetchall()
            return [self._row_to_pair(row) for row in rows]

    async def toggle_pair_active(self, pair_id: int) -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT is_active FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["is_active"] else 1
            await db.execute("UPDATE channel_pairs SET is_active = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def toggle_clean_links(self, pair_id: int) -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT clean_links FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["clean_links"] else 1
            await db.execute("UPDATE channel_pairs SET clean_links = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def toggle_auto_catchup(self, pair_id: int) -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT auto_catchup FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            curr = 1 if row["auto_catchup"] is None else row["auto_catchup"]
            new_status = 0 if curr else 1
            await db.execute("UPDATE channel_pairs SET auto_catchup = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def toggle_auto_translate(self, pair_id: int, target_lang: str = "uz") -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT auto_translate FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["auto_translate"] else 1
            await db.execute("UPDATE channel_pairs SET auto_translate = ?, target_lang = ? WHERE id = ?", (new_status, target_lang, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def update_watermark_settings(self, pair_id: int, wm_type: str, text: str, pos: str = "bottom_right"):
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE channel_pairs SET image_watermark_type = ?, image_watermark_text = ?, image_watermark_pos = ? WHERE id = ?",
                (wm_type, text, pos, pair_id)
            )
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def toggle_protected_mode(self, pair_id: int) -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT is_protected_source FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["is_protected_source"] else 1
            await db.execute("UPDATE channel_pairs SET is_protected_source = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def toggle_remove_signature(self, pair_id: int) -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT remove_signature FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["remove_signature"] else 1
            await db.execute("UPDATE channel_pairs SET remove_signature = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def update_affiliate_rules(self, pair_id: int, rules: str):
        async with self.write_transaction() as db:
            await db.execute("UPDATE channel_pairs SET affiliate_rules = ? WHERE id = ?", (rules, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def set_auto_translate(self, pair_id: int, enabled: bool, target_lang: str = "uz"):
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE channel_pairs SET auto_translate = ?, target_lang = ? WHERE id = ?",
                (1 if enabled else 0, target_lang, pair_id)
            )
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_pair_signature(self, pair_id: int, signature: str):
        async with self.write_transaction() as db:
            await db.execute("UPDATE channel_pairs SET custom_signature = ? WHERE id = ?", (signature, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_blacklist(self, pair_id: int, blacklist_words: str):
        async with self.write_transaction() as db:
            await db.execute("UPDATE channel_pairs SET blacklist_words = ? WHERE id = ?", (blacklist_words, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_pair_blacklist(self, pair_id: int, blacklist_words: str):
        await self.update_blacklist(pair_id, blacklist_words)

    async def update_replace_words(self, pair_id: int, replace_words: str):
        async with self.write_transaction() as db:
            await db.execute("UPDATE channel_pairs SET replace_words = ? WHERE id = ?", (replace_words, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_pair_source_id(self, pair_id: int, source_id: int):
        async with self.write_transaction() as db:
            await db.execute("UPDATE channel_pairs SET source_id = ? WHERE id = ?", (source_id, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_pair_target_id(self, pair_id: int, target_id: int):
        async with self.write_transaction() as db:
            await db.execute("UPDATE channel_pairs SET target_id = ? WHERE id = ?", (target_id, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_pair_ids(self, pair_id: int, source_id: Optional[int] = None, target_id: Optional[int] = None):
        if source_id is None and target_id is None:
            return
        async with self.write_transaction() as db:
            if source_id is not None and target_id is not None:
                await db.execute("UPDATE channel_pairs SET source_id = ?, target_id = ? WHERE id = ?", (source_id, target_id, pair_id))
            elif source_id is not None:
                await db.execute("UPDATE channel_pairs SET source_id = ? WHERE id = ?", (source_id, pair_id))
            elif target_id is not None:
                await db.execute("UPDATE channel_pairs SET target_id = ? WHERE id = ?", (target_id, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_pair_topics(self, pair_id: int, source_topic_id: Optional[int] = None, target_topic_id: Optional[int] = None):
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE channel_pairs SET source_topic_id = ?, target_topic_id = ? WHERE id = ?",
                (source_topic_id, target_topic_id, pair_id)
            )
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_pair_ad_action(self, pair_id: int, ad_action: str = "clean"):
        if ad_action not in ("clean", "drop", "swap", "off"):
            ad_action = "clean"
        async with self.write_transaction() as db:
            await db.execute("UPDATE channel_pairs SET ad_action = ? WHERE id = ?", (ad_action, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def toggle_show_caption_above(self, pair_id: int) -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT show_caption_above FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            curr = 1 if row["show_caption_above"] else 0
            new_status = 0 if curr else 1
            await db.execute("UPDATE channel_pairs SET show_caption_above = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def delete_pair(self, pair_id: int) -> bool:
        async with self.write_transaction() as db:
            await db.execute("DELETE FROM cloned_messages WHERE pair_id = ?", (pair_id,))
            await db.execute("DELETE FROM channel_backups WHERE pair_id = ?", (pair_id,))
            await db.execute("DELETE FROM drip_queue WHERE pair_id = ?", (pair_id,))
            cursor = await db.execute("DELETE FROM channel_pairs WHERE id = ?", (pair_id,))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return cursor.rowcount > 0

    delete_channel_pair = delete_pair

    # --- CLONED MESSAGES TRACKING & ANALYTICS ---

    async def is_message_cloned(self, pair_id: int, source_msg_id: int) -> bool:
        """High-speed in-memory LRU deduplication with SQLite fallback"""
        if await cache_manager.dedup_cache.contains((pair_id, source_msg_id)):
            return True

        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT 1 FROM cloned_messages WHERE pair_id = ? AND source_msg_id = ? LIMIT 1",
                (pair_id, source_msg_id)
            )
            is_found = bool(await cursor.fetchone())
            if is_found:
                await cache_manager.dedup_cache.add((pair_id, source_msg_id))
            return is_found

    async def record_cloned_message(
        self,
        pair_id: int,
        source_msg_id: int,
        target_msg_id: Optional[int] = None,
        media_group_id: Optional[str] = None,
        media_type: str = "text",
        source_channel: Optional[str] = None,
        target_channel: Optional[str] = None,
        story_id: Optional[int] = None,
        status: str = "active",
        price: float = 0.0,
        last_caption: Optional[str] = None
    ):
        await cache_manager.dedup_cache.add((pair_id, source_msg_id))
        async with self.write_transaction() as db:
            await db.execute("""
                INSERT INTO cloned_messages (
                    pair_id, source_msg_id, target_msg_id, media_group_id, media_type,
                    source_channel, target_channel, story_id, status, price, last_caption
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair_id, source_msg_id) DO UPDATE SET
                    target_msg_id = COALESCE(excluded.target_msg_id, cloned_messages.target_msg_id),
                    media_group_id = COALESCE(excluded.media_group_id, cloned_messages.media_group_id),
                    media_type = excluded.media_type,
                    source_channel = COALESCE(excluded.source_channel, cloned_messages.source_channel),
                    target_channel = COALESCE(excluded.target_channel, cloned_messages.target_channel),
                    story_id = COALESCE(excluded.story_id, cloned_messages.story_id),
                    status = excluded.status,
                    price = CASE WHEN excluded.price > 0 THEN excluded.price ELSE cloned_messages.price END,
                    last_caption = COALESCE(excluded.last_caption, cloned_messages.last_caption)
            """, (pair_id, source_msg_id, target_msg_id, media_group_id, media_type,
                  source_channel, target_channel, story_id, status, price, last_caption))
            await db.execute(
                "UPDATE channel_pairs SET last_seen_msg_id = MAX(COALESCE(last_seen_msg_id, 0), ?) WHERE id = ?",
                (source_msg_id, pair_id)
            )
            await db.commit()

    async def save_image_hashes(
        self,
        pair_id: Optional[int],
        source_channel: str,
        source_msg_id: int,
        hashes: List[str],
        price: float = 0.0
    ):
        """Persists 64-bit pHashes for duplicate detection and arbitrage"""
        clean_chan = (source_channel or "").lstrip("@").lower().strip()
        async with self.write_transaction() as db:
            for h in hashes:
                if h and len(h) == 16:
                    await db.execute("""
                        INSERT INTO image_hashes (pair_id, source_channel, source_msg_id, phash, price)
                        VALUES (?, ?, ?, ?, ?)
                    """, (pair_id, clean_chan, source_msg_id, h, price))
            await db.commit()

    async def get_recent_image_hashes(self, days: int = 30) -> List[Dict[str, Any]]:
        """Retrieves image hashes for the last N days to compare incoming listings"""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT pair_id, source_channel, source_msg_id, phash, price, created_at
                FROM image_hashes
                WHERE created_at >= datetime('now', '-' || ? || ' days')
            """, (int(days),))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_cloned_messages_by_source(self, source_channel: str, source_msg_id: int) -> List[Dict[str, Any]]:
        """Finds all destination posts cloned from a specific source message across all pairs"""
        clean_chan = (source_channel or "").lstrip("@").lower().strip()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT cm.*, cp.target_channel as pair_target_channel, cp.target_id as pair_target_id
                FROM cloned_messages cm
                JOIN channel_pairs cp ON cm.pair_id = cp.id
                WHERE (LOWER(TRIM(REPLACE(cp.source_channel, '@', ''))) = ?
                       OR LOWER(TRIM(REPLACE(COALESCE(cm.source_channel, ''), '@', ''))) = ?)
                  AND cm.source_msg_id = ?
            """, (clean_chan, clean_chan, source_msg_id))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def update_cloned_message_status(self, cloned_id: int, status: str):
        """Updates cloned message status (e.g. 'sold', 'edited')"""
        async with self.write_transaction() as db:
            await db.execute("UPDATE cloned_messages SET status = ? WHERE id = ?", (status, cloned_id))
            await db.commit()

    async def update_cloned_message_price(self, cloned_id: int, new_price: float):
        """Updates listing price on price drop arbitrage"""
        async with self.write_transaction() as db:
            await db.execute("UPDATE cloned_messages SET price = ? WHERE id = ?", (new_price, cloned_id))
            await db.commit()

    async def get_effective_last_source_msg_id(self, pair_id: int) -> Optional[int]:
        """Returns the highest known source message ID either from cloned_messages or pair.last_seen_msg_id"""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT MAX(source_msg_id) FROM cloned_messages WHERE pair_id = ?",
                (pair_id,)
            )
            row = await cursor.fetchone()
            if row and row[0] is not None:
                return int(row[0])
            cur2 = await db.execute("SELECT last_seen_msg_id FROM channel_pairs WHERE id = ?", (pair_id,))
            row2 = await cur2.fetchone()
            if row2 and row2[0] is not None:
                return int(row2[0])
            return None

    async def update_pair_last_seen_msg_id(self, pair_id: int, last_seen_msg_id: int):
        """Updates last_seen_msg_id for a channel pair safely under write lock"""
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE channel_pairs SET last_seen_msg_id = MAX(COALESCE(last_seen_msg_id, 0), ?) WHERE id = ?",
                (last_seen_msg_id, pair_id)
            )
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def clean_old_cloned_messages(self, days: int = 180) -> Tuple[int, int]:
        """Prunes ancient deduplication records from cloned_messages and expired drip queue items to keep DB size compact"""
        async with self.write_transaction() as db:
            cur1 = await db.execute("DELETE FROM cloned_messages WHERE cloned_at < datetime('now', '-' || ? || ' days')", (str(days),))
            deleted_cloned = cur1.rowcount if cur1.rowcount is not None else 0
            # Clean finished or failed drip queue items older than 14 days
            cur2 = await db.execute("DELETE FROM drip_queue WHERE status IN ('sent', 'failed', 'skipped') AND (created_at < datetime('now', '-14 days') OR scheduled_at < datetime('now', '-14 days'))")
            deleted_drip = cur2.rowcount if cur2.rowcount is not None else 0
            # Clean finished, failed or skipped story queue items older than 30 days
            await db.execute("DELETE FROM story_queue WHERE status IN ('sent', 'completed', 'failed', 'skipped') AND (created_at < datetime('now', '-30 days') OR scheduled_at < datetime('now', '-30 days'))")
            await db.commit()
            return deleted_cloned, deleted_drip

    async def get_pair_analytics(self, pair_id: int) -> Dict[str, Any]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN cloned_at >= date('now', 'start of day') THEN 1 ELSE 0 END) as today,
                    SUM(CASE WHEN media_type = 'photo' THEN 1 ELSE 0 END) as photos,
                    SUM(CASE WHEN media_type = 'video' THEN 1 ELSE 0 END) as videos
                FROM cloned_messages
                WHERE pair_id = ?
            """, (pair_id,))
            row = await cursor.fetchone()
            return {
                "total_cloned": row["total"] if row and row["total"] is not None else 0,
                "today_cloned": row["today"] if row and row["today"] is not None else 0,
                "photos_cloned": row["photos"] if row and row["photos"] is not None else 0,
                "videos_cloned": row["videos"] if row and row["videos"] is not None else 0
            }

    # --- DATABASE BACKUP EXPORTER ---

    async def create_backup_file(self) -> Optional[str]:
        """Creates a timestamped snapshot backup of the database using SQLite online backup or VACUUM INTO"""
        if not os.path.exists(self.db_path):
            return None

        backup_dir = os.path.join("data", "backups")
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"backup_cloner_{timestamp}.db")

        try:
            # Check available disk space (need at least 2x db_size + 50MB)
            try:
                total, used, free = shutil.disk_usage(os.path.abspath(backup_dir))
                db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
                if free < (db_size * 2 + 50 * 1024 * 1024):
                    logger.error(f"Insufficient disk space for backup: {free / (1024*1024):.1f}MB free")
                    return None
            except Exception as e_disk:
                logger.warning(f"Unable to verify disk usage before backup ({e_disk}), proceeding with backup attempt")

            if os.path.exists(backup_path):
                os.remove(backup_path)

            # Flush WAL checkpoint before backup
            try:
                await self.checkpoint()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            # Primary: Native Python sqlite3 online backup API with strict cleanup
            def _do_online_backup():
                import sqlite3
                src = sqlite3.connect(self.db_path, timeout=60.0)
                dest = sqlite3.connect(backup_path, timeout=60.0)
                try:
                    src.backup(dest)
                finally:
                    dest.close()
                    src.close()

            await asyncio.to_thread(_do_online_backup)
            return backup_path
        except Exception as e:
            logger.error(f"Backup creation failed: {e}")
            return None

    # --- STATS ---

    async def get_stats(self) -> Dict[str, Any]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT
                    (SELECT COUNT(*) FROM users) as total_users,
                    (SELECT COUNT(*) FROM channel_pairs) as total_pairs,
                    (SELECT COUNT(*) FROM channel_pairs WHERE is_active = 1) as active_pairs,
                    (SELECT COUNT(*) FROM cloned_messages) as total_cloned,
                    (SELECT COALESCE(SUM(amount), 0) FROM payments) as total_stars
            """)
            row = await cur.fetchone()
            return {
                "total_users": row["total_users"] if row else 0,
                "total_pairs": row["total_pairs"] if row else 0,
                "active_pairs": row["active_pairs"] if row else 0,
                "total_cloned_messages": row["total_cloned"] if row else 0,
                "total_stars_earned": row["total_stars"] if row else 0
            }

    async def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Returns isolated personal channel and clone statistics for a specific user"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT
                    (SELECT COUNT(*) FROM channel_pairs WHERE user_id = ?) as total_pairs,
                    (SELECT COUNT(*) FROM channel_pairs WHERE user_id = ? AND is_active = 1) as active_pairs,
                    (SELECT COUNT(*) FROM cloned_messages m JOIN channel_pairs p ON m.pair_id = p.id WHERE p.user_id = ?) as total_cloned,
                    (SELECT COUNT(*) FROM cloned_messages m JOIN channel_pairs p ON m.pair_id = p.id WHERE p.user_id = ? AND m.cloned_at >= date('now', 'start of day')) as today_cloned
            """, (user_id, user_id, user_id, user_id))
            row = await cur.fetchone()
            total_pairs = row["total_pairs"] if row else 0
            active_pairs = row["active_pairs"] if row else 0
            total_cloned = row["total_cloned"] if row else 0
            today_cloned = row["today_cloned"] if row else 0

            sub = await self.get_user_subscription(user_id)

            return {
                "total_pairs": total_pairs,
                "active_pairs": active_pairs,
                "total_cloned": total_cloned,
                "today_cloned": today_cloned,
                "subscription": sub
            }

    async def toggle_premium_emojis(self, pair_id: int) -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT auto_premium_emojis FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["auto_premium_emojis"] else 1
            await db.execute("UPDATE channel_pairs SET auto_premium_emojis = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def update_video_watermark_settings(self, pair_id: int, wm_type: str, text: str, pos: str = "bottom_right"):
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE channel_pairs SET video_watermark_type = ?, video_watermark_text = ?, video_watermark_pos = ? WHERE id = ?",
                (wm_type, text, pos, pair_id)
            )
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_drip_settings(self, pair_id: int, delay_minutes: int, night_mode: str = "off"):
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE channel_pairs SET drip_delay_minutes = ?, night_mode = ? WHERE id = ?",
                (delay_minutes, night_mode, pair_id)
            )
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def update_ai_paraphrase_settings(self, pair_id: int, mode: str = "off"):
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE channel_pairs SET ai_paraphrase_mode = ? WHERE id = ?",
                (mode, pair_id)
            )
            await db.commit()
            self._notify_pair_cache_invalidated()

    async def toggle_auto_cta_buttons(self, pair_id: int) -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT auto_cta_buttons FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["auto_cta_buttons"] else 1
            await db.execute("UPDATE channel_pairs SET auto_cta_buttons = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def toggle_backup_enabled(self, pair_id: int) -> Optional[bool]:
        async with self.write_transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT backup_enabled FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["backup_enabled"] else 1
            await db.execute("UPDATE channel_pairs SET backup_enabled = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            self._notify_pair_cache_invalidated()
            return bool(new_status)

    async def update_pair_topic_settings(self, pair_id: int, source_topic_id: Optional[int], target_topic_id: Optional[int]):
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE channel_pairs SET source_topic_id = ?, target_topic_id = ? WHERE id = ?",
                (source_topic_id, target_topic_id, pair_id)
            )
            await db.commit()
            self._notify_pair_cache_invalidated()

    # --- DRIP FEED QUEUE ---

    async def add_drip_queue_item(self, pair_id: int, msg_data_json: str, scheduled_at: str) -> int:
        async with self.write_transaction() as db:
            cursor = await db.execute(
                "INSERT INTO drip_queue (pair_id, msg_data_json, scheduled_at, status) VALUES (?, ?, ?, 'pending')",
                (pair_id, msg_data_json, scheduled_at)
            )
            await db.commit()
            return cursor.lastrowid

    async def get_due_drip_items(self) -> List[Dict[str, Any]]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM drip_queue
                WHERE status = 'pending' AND datetime(scheduled_at) <= datetime('now')
                ORDER BY scheduled_at ASC
                LIMIT 50
            """)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def mark_drip_item_done(self, item_id: int, status: str = "sent", error_message: Optional[str] = None):
        async with self.write_transaction() as db:
            if error_message is not None:
                await db.execute("UPDATE drip_queue SET status = ?, error_message = ? WHERE id = ?", (status, error_message, item_id))
            else:
                await db.execute("UPDATE drip_queue SET status = ? WHERE id = ?", (status, item_id))
            await db.commit()

    async def get_latest_scheduled_drip_time(self, pair_id: int) -> Optional[datetime]:
        """Returns the latest scheduled_at timestamp for pending posts belonging to pair_id in UTC"""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT MAX(scheduled_at) FROM drip_queue WHERE pair_id = ? AND status = 'pending'",
                (pair_id,)
            )
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    dt_str = row[0].replace(" ", "T")
                    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
        return None

    # --- CHANNEL BACKUPS & DISASTER RECOVERY ---

    async def save_channel_backup(
        self,
        pair_id: int,
        source_id: Optional[int],
        message_id: int,
        text: str = "",
        media_type: str = "none",
        media_file_id: Optional[str] = None,
        entities_json: str = "",
        media_group_id: Optional[str] = None
    ):
        async with self.write_transaction() as db:
            await db.execute("""
                INSERT INTO channel_backups (pair_id, source_id, message_id, text, media_type, media_file_id, entities_json, media_group_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair_id, message_id) DO UPDATE SET
                    source_id = excluded.source_id,
                    text = excluded.text,
                    media_type = excluded.media_type,
                    media_file_id = excluded.media_file_id,
                    entities_json = excluded.entities_json,
                    media_group_id = excluded.media_group_id
            """, (pair_id, source_id, message_id, text, media_type, media_file_id, entities_json, media_group_id))
            await db.commit()

    async def get_channel_backups(self, pair_id: int, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            if limit is not None:
                cursor = await db.execute(
                    "SELECT * FROM channel_backups WHERE pair_id = ? ORDER BY message_id ASC LIMIT ?",
                    (pair_id, limit)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM channel_backups WHERE pair_id = ? ORDER BY message_id ASC",
                    (pair_id,)
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_channel_backup_count(self, pair_id: int) -> int:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM channel_backups WHERE pair_id = ?", (pair_id,))
            row = await cursor.fetchone()
            return row["cnt"] if row else 0

    @staticmethod
    def _row_to_pair(row: Any) -> ChannelPair:
        keys = row.keys() if hasattr(row, 'keys') else []
        return ChannelPair(
            id=row["id"],
            user_id=row["user_id"],
            source_channel=row["source_channel"],
            source_title=row["source_title"] or row["source_channel"],
            source_id=row["source_id"] if "source_id" in keys else None,
            target_channel=row["target_channel"],
            target_title=row["target_title"] or row["target_channel"],
            target_id=row["target_id"] if "target_id" in keys else None,
            is_active=bool(row["is_active"]),
            clean_links=bool(row["clean_links"]),
            custom_signature=row["custom_signature"] or "",
            remove_signature=bool(row["remove_signature"]),
            blacklist_words=row["blacklist_words"] or "",
            replace_words=row["replace_words"] or "",
            clone_mode=row["clone_mode"] or "clean",
            auto_translate=bool(row["auto_translate"]) if "auto_translate" in keys else False,
            target_lang=row["target_lang"] if "target_lang" in keys else "uz",
            source_lang=row["source_lang"] if "source_lang" in keys else "auto",
            image_watermark_type=row["image_watermark_type"] if "image_watermark_type" in keys else "none",
            image_watermark_text=row["image_watermark_text"] if "image_watermark_text" in keys else "",
            image_watermark_pos=row["image_watermark_pos"] if "image_watermark_pos" in keys else "bottom_right",
            is_protected_source=bool(row["is_protected_source"]) if "is_protected_source" in keys else False,
            affiliate_rules=row["affiliate_rules"] if "affiliate_rules" in keys else "",
            auto_premium_emojis=bool(row["auto_premium_emojis"]) if "auto_premium_emojis" in keys else False,
            video_watermark_type=row["video_watermark_type"] if "video_watermark_type" in keys else "none",
            video_watermark_text=row["video_watermark_text"] if "video_watermark_text" in keys else "",
            video_watermark_pos=row["video_watermark_pos"] if "video_watermark_pos" in keys else "bottom_right",
            drip_delay_minutes=row["drip_delay_minutes"] if "drip_delay_minutes" in keys else 0,
            night_mode=row["night_mode"] if "night_mode" in keys else "off",
            ai_paraphrase_mode=row["ai_paraphrase_mode"] if "ai_paraphrase_mode" in keys else "off",
            tone_of_voice=row["tone_of_voice"] if "tone_of_voice" in keys and row["tone_of_voice"] else "standard",
            enable_invisible_watermark=bool(row["enable_invisible_watermark"]) if "enable_invisible_watermark" in keys else True,
            auto_cta_buttons=bool(row["auto_cta_buttons"]) if "auto_cta_buttons" in keys else False,
            backup_enabled=bool(row["backup_enabled"]) if "backup_enabled" in keys else True,
            last_seen_msg_id=row["last_seen_msg_id"] if "last_seen_msg_id" in keys and row["last_seen_msg_id"] is not None else None,
            auto_catchup=bool(row["auto_catchup"]) if "auto_catchup" in keys and row["auto_catchup"] is not None else True,
            source_topic_id=row["source_topic_id"] if "source_topic_id" in keys and row["source_topic_id"] is not None else None,
            target_topic_id=row["target_topic_id"] if "target_topic_id" in keys and row["target_topic_id"] is not None else None,
            ad_action=row["ad_action"] if "ad_action" in keys and row["ad_action"] else "clean",
            show_caption_above=bool(row["show_caption_above"]) if "show_caption_above" in keys and row["show_caption_above"] is not None else False,
            created_at=row["created_at"]
        )

    # --- PERSISTENT FSM STORAGE METHODS ---

    async def set_fsm_state(
        self,
        bot_id: int,
        chat_id: int,
        user_id: int,
        thread_id: Optional[int],
        destiny: str,
        state: Optional[str]
    ):
        """Persists or clears FSM state in SQLite table, pruning empty rows"""
        t_id = thread_id or 0
        dest = destiny or "default"
        async with self.write_transaction() as db:
            if state is None:
                cursor = await db.execute(
                    "SELECT data_json FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND thread_id = ? AND destiny = ?",
                    (bot_id, chat_id, user_id, t_id, dest)
                )
                row = await cursor.fetchone()
                if row and (not row[0] or row[0] in ("{}", "null", '""')):
                    await db.execute(
                        "DELETE FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND thread_id = ? AND destiny = ?",
                        (bot_id, chat_id, user_id, t_id, dest)
                    )
                    await db.commit()
                    return
            await db.execute("""
                INSERT INTO fsm_storage (bot_id, chat_id, user_id, thread_id, destiny, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(bot_id, chat_id, user_id, thread_id, destiny) DO UPDATE SET
                    state = excluded.state,
                    updated_at = CURRENT_TIMESTAMP
            """, (bot_id, chat_id, user_id, t_id, dest, state))
            await db.commit()

    async def get_fsm_state(
        self,
        bot_id: int,
        chat_id: int,
        user_id: int,
        thread_id: Optional[int],
        destiny: str
    ) -> Optional[str]:
        """Retrieves persistent FSM state from SQLite table"""
        t_id = thread_id or 0
        dest = destiny or "default"
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT state FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND thread_id = ? AND destiny = ?",
                (bot_id, chat_id, user_id, t_id, dest)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_fsm_data(
        self,
        bot_id: int,
        chat_id: int,
        user_id: int,
        thread_id: Optional[int],
        destiny: str,
        data: Dict[str, Any]
    ):
        """Persists FSM data dictionary as JSON in SQLite table, pruning empty rows"""
        t_id = thread_id or 0
        dest = destiny or "default"
        data_str = json.dumps(data)
        async with self.write_transaction() as db:
            if not data or data == {}:
                cursor = await db.execute(
                    "SELECT state FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND thread_id = ? AND destiny = ?",
                    (bot_id, chat_id, user_id, t_id, dest)
                )
                row = await cursor.fetchone()
                if not row:
                    return
                if not row[0]:
                    await db.execute(
                        "DELETE FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND thread_id = ? AND destiny = ?",
                        (bot_id, chat_id, user_id, t_id, dest)
                    )
                    await db.commit()
                    return
            await db.execute("""
                INSERT INTO fsm_storage (bot_id, chat_id, user_id, thread_id, destiny, data_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(bot_id, chat_id, user_id, thread_id, destiny) DO UPDATE SET
                    data_json = excluded.data_json,
                    updated_at = CURRENT_TIMESTAMP
            """, (bot_id, chat_id, user_id, t_id, dest, data_str))
            await db.commit()

    async def get_fsm_data(
        self,
        bot_id: int,
        chat_id: int,
        user_id: int,
        thread_id: Optional[int],
        destiny: str
    ) -> Dict[str, Any]:
        """Retrieves FSM data dictionary from SQLite table"""
        t_id = thread_id or 0
        dest = destiny or "default"
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT data_json FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND thread_id = ? AND destiny = ?",
                (bot_id, chat_id, user_id, t_id, dest)
            )
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except Exception:
                    return {}
            return {}

    async def clear_fsm(
        self,
        bot_id: int,
        chat_id: int,
        user_id: int,
        thread_id: Optional[int],
        destiny: str
    ):
        """Clears both FSM state and data for specified key"""
        t_id = thread_id or 0
        dest = destiny or "default"
        async with self.write_transaction() as db:
            await db.execute(
                "DELETE FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND thread_id = ? AND destiny = ?",
                (bot_id, chat_id, user_id, t_id, dest)
            )
            await db.commit()

    async def prune_database(self, max_age_days: int = 30) -> Dict[str, int]:
        """Safely prunes old completed drip queue records and stale clone logs to prevent database bloating"""
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%d %H:%M:%S")
        res = {"deleted_drip_items": 0, "deleted_cloned_messages": 0}
        async with self.write_transaction() as db:
            cur_drip = await db.execute(
                "DELETE FROM drip_queue WHERE status IN ('sent', 'skipped', 'failed') AND created_at < ?",
                (cutoff_date,)
            )
            res["deleted_drip_items"] = cur_drip.rowcount if cur_drip and cur_drip.rowcount > 0 else 0

            cur_logs = await db.execute(
                "DELETE FROM cloned_messages WHERE cloned_at < ?",
                (cutoff_date,)
            )
            res["deleted_cloned_messages"] = cur_logs.rowcount if cur_logs and cur_logs.rowcount > 0 else 0

            # Prune abandoned FSM conversations older than 7 days
            cur_fsm = await db.execute(
                "DELETE FROM fsm_storage WHERE updated_at < datetime('now', '-7 days')"
            )
            res["deleted_fsm_rows"] = cur_fsm.rowcount if cur_fsm and cur_fsm.rowcount > 0 else 0

            # Prune old image hashes older than 60 days
            cur_hashes = await db.execute(
                "DELETE FROM image_hashes WHERE created_at < datetime('now', '-60 days')"
            )
            res["deleted_image_hashes"] = cur_hashes.rowcount if cur_hashes and cur_hashes.rowcount > 0 else 0

            await db.commit()
        return res
    # ==========================================
    # --- USER TELETHON SESSIONS & STORY SYSTEM ---
    # ==========================================

    async def save_user_session(
        self,
        user_id: int,
        session_str: str,
        phone: str = "",
        first_name: str = "",
        last_name: str = "",
        username: str = ""
    ) -> bool:
        """Encrypts and stores a per-user Telethon StringSession"""
        encrypted_session = security_vault.encrypt_secret(session_str)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with self.write_transaction() as db:
            await db.execute("""
                INSERT INTO user_sessions (user_id, session_encrypted, phone, first_name, last_name, username, is_active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    session_encrypted = excluded.session_encrypted,
                    phone = excluded.phone,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    username = excluded.username,
                    is_active = 1,
                    updated_at = excluded.updated_at
            """, (user_id, encrypted_session, phone, first_name, last_name, username, now))
            await db.commit()
            return True

    async def get_user_session(self, user_id: int) -> Optional[str]:
        """Returns decrypted Telethon StringSession for the user, or None if inactive/not found"""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT session_encrypted, is_active FROM user_sessions WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[1]:  # is_active
                    return security_vault.decrypt_secret(row[0])
        return None

    async def get_user_session_info(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Returns metadata about the user's connected Telethon account"""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT user_id, phone, first_name, last_name, username, is_active, created_at, updated_at FROM user_sessions WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "user_id": row[0],
                        "phone": row[1],
                        "first_name": row[2],
                        "last_name": row[3],
                        "username": row[4],
                        "is_active": bool(row[5]),
                        "created_at": row[6],
                        "updated_at": row[7]
                    }
        return None

    async def delete_user_session(self, user_id: int) -> bool:
        """Deletes user's Telethon session from database and disables active story monitoring"""
        async with self.write_transaction() as db:
            await db.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
            try:
                await db.execute("UPDATE story_settings SET is_active = 0 WHERE user_id = ?", (user_id,))
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            await db.commit()
            return True

    async def get_all_active_user_sessions(self) -> List[Dict[str, Any]]:
        """Returns all active user sessions with decrypted session string"""
        results = []
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT user_id, session_encrypted, phone, first_name, username FROM user_sessions WHERE is_active = 1"
            ) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    decrypted = security_vault.decrypt_secret(r[1])
                    if decrypted:
                        results.append({
                            "user_id": r[0],
                            "session": decrypted,
                            "phone": r[2],
                            "first_name": r[3],
                            "username": r[4]
                        })
        return results

    async def get_story_settings(self, user_id: int) -> StorySettings:
        """Retrieves story cloner settings for the user, or creates default"""
        async with self.get_connection() as db:
            async with db.execute(
                """SELECT user_id, source_channel, source_title, source_id, target_type, target_channel, target_id,
                          min_price, max_price, require_photos, require_price, filter_demands, background_style,
                          is_active, prime_hours_enabled, prime_hours_start, prime_hours_end,
                          drip_delay_minutes, max_stories_per_day, enable_smart_badges, pin_to_profile,
                          video_duration, enable_ai_voice, created_at, updated_at
                   FROM story_settings WHERE user_id = ?""",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return StorySettings(
                        user_id=row[0],
                        source_channel=row[1] or "",
                        source_title=row[2] or "",
                        source_id=row[3],
                        target_type=row[4] or "self",
                        target_channel=row[5] or "",
                        target_id=row[6],
                        min_price=float(row[7]) if row[7] is not None else 700.0,
                        max_price=float(row[8]) if row[8] is not None else 0.0,
                        require_photos=bool(row[9]),
                        require_price=bool(row[10]),
                        filter_demands=bool(row[11]),
                        background_style=row[12] or "telegram_green",
                        is_active=bool(row[13]),
                        prime_hours_enabled=bool(row[14]) if row[14] is not None else True,
                        prime_hours_start=int(row[15]) if row[15] is not None else 9,
                        prime_hours_end=int(row[16]) if row[16] is not None else 22,
                        drip_delay_minutes=int(row[17]) if row[17] is not None else 45,
                        max_stories_per_day=int(row[18]) if row[18] is not None else 5,
                        enable_smart_badges=bool(row[19]) if row[19] is not None else True,
                        pin_to_profile=bool(row[20]) if row[20] is not None else True,
                        video_duration=int(row[21]) if row[21] is not None else 25,
                        enable_ai_voice=bool(row[22]) if row[22] is not None else True,
                        created_at=row[23],
                        updated_at=row[24]
                    )
        return StorySettings(user_id=user_id)

    async def save_story_settings(self, st: StorySettings) -> bool:
        """Saves or updates story cloner settings for a user"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with self.write_transaction() as db:
            await db.execute("""
                INSERT INTO story_settings (
                    user_id, source_channel, source_title, source_id, target_type, target_channel, target_id,
                    min_price, max_price, require_photos, require_price, filter_demands, background_style,
                    is_active, prime_hours_enabled, prime_hours_start, prime_hours_end,
                    drip_delay_minutes, max_stories_per_day, enable_smart_badges, pin_to_profile, video_duration, enable_ai_voice, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    source_channel = excluded.source_channel,
                    source_title = excluded.source_title,
                    source_id = excluded.source_id,
                    target_type = excluded.target_type,
                    target_channel = excluded.target_channel,
                    target_id = excluded.target_id,
                    min_price = excluded.min_price,
                    max_price = excluded.max_price,
                    require_photos = excluded.require_photos,
                    require_price = excluded.require_price,
                    filter_demands = excluded.filter_demands,
                    background_style = excluded.background_style,
                    is_active = excluded.is_active,
                    prime_hours_enabled = excluded.prime_hours_enabled,
                    prime_hours_start = excluded.prime_hours_start,
                    prime_hours_end = excluded.prime_hours_end,
                    drip_delay_minutes = excluded.drip_delay_minutes,
                    max_stories_per_day = excluded.max_stories_per_day,
                    enable_smart_badges = excluded.enable_smart_badges,
                    pin_to_profile = excluded.pin_to_profile,
                    video_duration = excluded.video_duration,
                    enable_ai_voice = excluded.enable_ai_voice,
                    updated_at = excluded.updated_at
            """, (
                st.user_id, st.source_channel, st.source_title, st.source_id,
                st.target_type, st.target_channel, st.target_id,
                st.min_price, st.max_price, int(st.require_photos), int(st.require_price),
                int(st.filter_demands), st.background_style, int(st.is_active),
                int(st.prime_hours_enabled), st.prime_hours_start, st.prime_hours_end,
                st.drip_delay_minutes, st.max_stories_per_day, int(st.enable_smart_badges),
                int(st.pin_to_profile), int(getattr(st, "video_duration", 25) or 25),
                int(getattr(st, "enable_ai_voice", True)), now
            ))
            await db.commit()
            return True

    async def update_story_settings(self, user_id: int, **kwargs) -> StorySettings:
        """Helper to partially update and save story settings for a user"""
        st = await self.get_story_settings(user_id)
        for k, v in kwargs.items():
            if hasattr(st, k):
                setattr(st, k, v)
        await self.save_story_settings(st)
        return st

    async def get_all_active_story_settings(self) -> List[StorySettings]:
        """Returns list of all active story settings across all users"""
        settings_list = []
        async with self.get_connection() as db:
            async with db.execute(
                """SELECT user_id, source_channel, source_title, source_id, target_type, target_channel, target_id,
                          min_price, max_price, require_photos, require_price, filter_demands, background_style,
                          is_active, prime_hours_enabled, prime_hours_start, prime_hours_end,
                          drip_delay_minutes, max_stories_per_day, enable_smart_badges, pin_to_profile,
                          video_duration, created_at, updated_at
                   FROM story_settings WHERE is_active = 1"""
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    settings_list.append(StorySettings(
                        user_id=row[0],
                        source_channel=row[1] or "",
                        source_title=row[2] or "",
                        source_id=row[3],
                        target_type=row[4] or "self",
                        target_channel=row[5] or "",
                        target_id=row[6],
                        min_price=float(row[7]) if row[7] is not None else 700.0,
                        max_price=float(row[8]) if row[8] is not None else 0.0,
                        require_photos=bool(row[9]),
                        require_price=bool(row[10]),
                        filter_demands=bool(row[11]),
                        background_style=row[12] or "telegram_green",
                        is_active=bool(row[13]),
                        prime_hours_enabled=bool(row[14]) if row[14] is not None else True,
                        prime_hours_start=int(row[15]) if row[15] is not None else 9,
                        prime_hours_end=int(row[16]) if row[16] is not None else 22,
                        drip_delay_minutes=int(row[17]) if row[17] is not None else 45,
                        max_stories_per_day=int(row[18]) if row[18] is not None else 5,
                        enable_smart_badges=bool(row[19]) if row[19] is not None else True,
                        pin_to_profile=bool(row[20]) if row[20] is not None else True,
                        video_duration=int(row[21]) if row[21] is not None else 25,
                        created_at=row[22],
                        updated_at=row[23]
                    ))
        return settings_list

    # ====================================================
    # --- MULTI-SOURCE CHANNELS & SMART DEDUP & QUEUE ---
    # ====================================================

    async def get_story_source_channels(self, user_id: int) -> List[StorySourceChannel]:
        """Returns all configured source channels for user"""
        channels = []
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT id, user_id, channel_username, channel_title, channel_id, is_active, created_at FROM story_source_channels WHERE user_id = ? ORDER BY id ASC",
                (user_id,)
            ) as cur:
                rows = await cur.fetchall()
                for r in rows:
                    channels.append(StorySourceChannel(
                        id=r[0],
                        user_id=r[1],
                        channel_username=r[2],
                        channel_title=r[3] or "",
                        channel_id=r[4],
                        is_active=bool(r[5]),
                        created_at=r[6]
                    ))
        return channels

    async def add_story_source_channel(
        self,
        user_id: int,
        channel_username: str,
        channel_title: str = "",
        channel_id: Optional[int] = None
    ) -> Optional[int]:
        """Adds a new source channel for multi-channel story cloner. Returns channel id or None if already exists."""
        clean_user = channel_username.strip()
        if not clean_user.startswith("@") and not clean_user.startswith("-100") and not clean_user.startswith("+"):
            clean_user = f"@{clean_user.lstrip('@')}"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with self.write_transaction() as db:
            async with db.execute(
                "SELECT id, is_active FROM story_source_channels WHERE user_id = ? AND channel_username = ?",
                (user_id, clean_user)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    if not row[1]:  # re-activate if paused
                        await db.execute(
                            "UPDATE story_source_channels SET channel_title = ?, channel_id = ?, is_active = 1 WHERE id = ?",
                            (channel_title, channel_id, row[0])
                        )
                        await db.commit()
                        return row[0]
                    return None  # Already actively exists

            cur = await db.execute("""
                INSERT INTO story_source_channels (user_id, channel_username, channel_title, channel_id, is_active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
            """, (user_id, clean_user, channel_title, channel_id, now))
            await db.commit()
            return cur.lastrowid

    async def delete_story_source_channel(self, user_id: int, channel_id: int) -> bool:
        """Deletes a source channel entry by ID"""
        async with self.write_transaction() as db:
            await db.execute("DELETE FROM story_source_channels WHERE user_id = ? AND id = ?", (user_id, channel_id))
            await db.commit()
            return True

    async def is_listing_duplicate(self, user_id: int, listing_hash: str, days: int = 14) -> bool:
        """
        Checks if an identical or near-identical real estate listing was posted
        across any monitored source channel within the given window (default 14 days).
        """
        if not listing_hash:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT 1 FROM story_dedup_hashes WHERE user_id = ? AND listing_hash = ? AND posted_at >= ? LIMIT 1",
                (user_id, listing_hash, cutoff)
            ) as cur:
                return await cur.fetchone() is not None

    async def record_listing_hash(
        self,
        user_id: int,
        listing_hash: str,
        source_channel: str,
        source_msg_id: int
    ) -> bool:
        """Records a listing's deterministic fingerprint for multi-channel deduplication"""
        if not listing_hash:
            return False
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with self.write_transaction() as db:
            await db.execute("""
                INSERT INTO story_dedup_hashes (user_id, listing_hash, source_channel, source_msg_id, posted_at)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, listing_hash, source_channel, source_msg_id, now))
            await db.commit()
            return True

    async def enqueue_story(
        self,
        user_id: int,
        source_channel: str,
        source_msg_id: int,
        payload: Dict[str, Any],
        scheduled_at_utc: str,
        price: Optional[float] = None,
        district: str = "",
        rooms: Optional[int] = None,
        area: Optional[float] = None,
        score: int = 0
    ) -> int:
        """Adds a candidate listing into the persistent Smart Drip-Feed queue"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        payload_str = json.dumps(payload, ensure_ascii=False)
        async with self.write_transaction() as db:
            cur = await db.execute("""
                INSERT INTO story_queue (
                    user_id, source_channel, source_msg_id, price, district, rooms,
                    area, score, payload_json, status, scheduled_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (
                user_id, source_channel, source_msg_id, price, district,
                rooms or 0, area or 0.0, score, payload_str, scheduled_at_utc, now
            ))
            await db.commit()
            return cur.lastrowid

    async def get_due_story_queue_items(self) -> List[StoryQueueItem]:
        """Retrieves pending queue items whose scheduled time has arrived, sorted by quality score DESC"""
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        items = []
        async with self.get_connection() as db:
            async with db.execute("""
                SELECT id, user_id, source_channel, source_msg_id, price, district, rooms,
                       area, score, payload_json, status, scheduled_at, created_at, error_message
                FROM story_queue
                WHERE status = 'pending' AND scheduled_at <= ?
                ORDER BY score DESC, scheduled_at ASC
                LIMIT 10
            """, (now_utc,)) as cur:
                rows = await cur.fetchall()
                for r in rows:
                    items.append(StoryQueueItem(
                        id=r[0],
                        user_id=r[1],
                        source_channel=r[2],
                        source_msg_id=r[3],
                        price=r[4],
                        district=r[5] or "",
                        rooms=r[6],
                        area=r[7],
                        score=r[8],
                        payload_json=r[9],
                        status=r[10],
                        scheduled_at=r[11],
                        created_at=r[12],
                        error_message=r[13]
                    ))
        return items

    async def get_user_story_queue(self, user_id: int) -> List[StoryQueueItem]:
        """Returns all pending queue items for user"""
        items = []
        async with self.get_connection() as db:
            async with db.execute("""
                SELECT id, user_id, source_channel, source_msg_id, price, district, rooms,
                       area, score, payload_json, status, scheduled_at, created_at, error_message
                FROM story_queue
                WHERE user_id = ? AND status = 'pending'
                ORDER BY scheduled_at ASC, score DESC
            """, (user_id,)) as cur:
                rows = await cur.fetchall()
                for r in rows:
                    items.append(StoryQueueItem(
                        id=r[0],
                        user_id=r[1],
                        source_channel=r[2],
                        source_msg_id=r[3],
                        price=r[4],
                        district=r[5] or "",
                        rooms=r[6],
                        area=r[7],
                        score=r[8],
                        payload_json=r[9],
                        status=r[10],
                        scheduled_at=r[11],
                        created_at=r[12],
                        error_message=r[13]
                    ))
        return items

    async def mark_story_queue_item_done(
        self,
        item_id: int,
        status: str = "sent",
        error_message: Optional[str] = None
    ) -> bool:
        """Updates queue item state (sent / skipped / failed)"""
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE story_queue SET status = ?, error_message = ? WHERE id = ?",
                (status, error_message, item_id)
            )
            await db.commit()
            return True

    async def get_last_posted_story_time(self, user_id: int) -> Optional[datetime]:
        """Returns the UTC datetime of the most recently published story for user"""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT posted_at FROM posted_stories WHERE user_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                (user_id,)
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    try:
                        return datetime.fromisoformat(row[0].replace(' ', 'T')).replace(tzinfo=timezone.utc)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
        return None

    async def get_today_posted_story_count(self, user_id: int) -> int:
        """Returns count of stories successfully posted today (UTC)"""
        today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM posted_stories WHERE user_id = ? AND status = 'success' AND posted_at >= ?",
                (user_id, today_start)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def is_story_posted(
        self,
        user_id: int,
        source_channel: str,
        source_msg_id: int,
        source_id: Optional[int] = None,
        grouped_id: Optional[int] = None
    ) -> bool:
        """Checks if this channel post has already been posted to Stories by user"""
        async with self.get_connection() as db:
            if grouped_id:
                async with db.execute(
                    "SELECT 1 FROM posted_stories WHERE user_id = ? AND grouped_id = ? LIMIT 1",
                    (user_id, grouped_id)
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        return True

            if source_id:
                async with db.execute(
                    "SELECT 1 FROM posted_stories WHERE user_id = ? AND (source_id = ? OR source_channel = ?) AND source_msg_id = ? LIMIT 1",
                    (user_id, source_id, source_channel, source_msg_id)
                ) as cursor:
                    return await cursor.fetchone() is not None
            else:
                async with db.execute(
                    "SELECT 1 FROM posted_stories WHERE user_id = ? AND source_channel = ? AND source_msg_id = ? LIMIT 1",
                    (user_id, source_channel, source_msg_id)
                ) as cursor:
                    return await cursor.fetchone() is not None

    async def record_posted_story(
        self,
        user_id: int,
        source_channel: str,
        source_id: Optional[int],
        source_msg_id: int,
        story_id: Optional[int],
        price: Optional[float] = None,
        caption_snippet: str = "",
        target_type: str = "self",
        status: str = "success",
        grouped_id: Optional[int] = None
    ) -> bool:
        """Records a successfully posted story for idempotency and audit"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with self.write_transaction() as db:
            await db.execute("""
                INSERT INTO posted_stories (
                    user_id, source_channel, source_id, source_msg_id, grouped_id, story_id, price, caption_snippet, target_type, posted_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, source_channel, source_id, source_msg_id, grouped_id, story_id,
                price, caption_snippet[:200] if caption_snippet else "", target_type, now, status
            ))
            await db.commit()
            return True

    async def get_story_stats(self, user_id: int) -> Dict[str, Any]:
        """Calculates story posting statistics for a user"""
        today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
        stats = {
            "total_posted": 0,
            "today_posted": 0,
            "recent": []
        }
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM posted_stories WHERE user_id = ? AND status = 'success'",
                (user_id,)
            ) as cur_total:
                row = await cur_total.fetchone()
                stats["total_posted"] = row[0] if row else 0

            async with db.execute(
                "SELECT COUNT(*) FROM posted_stories WHERE user_id = ? AND status = 'success' AND posted_at >= ?",
                (user_id, today_start)
            ) as cur_today:
                row = await cur_today.fetchone()
                stats["today_posted"] = row[0] if row else 0

            async with db.execute(
                "SELECT source_channel, source_msg_id, story_id, price, caption_snippet, posted_at FROM posted_stories WHERE user_id = ? AND status = 'success' ORDER BY id DESC LIMIT 5",
                (user_id,)
            ) as cur_recent:
                rows = await cur_recent.fetchall()
                for r in rows:
                    stats["recent"].append({
                        "source_channel": r[0],
                        "source_msg_id": r[1],
                        "story_id": r[2],
                        "price": r[3],
                        "caption_snippet": r[4],
                        "posted_at": r[5]
                    })
        return stats

    async def get_app_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieves a persistent setting value by key from app_settings"""
        async with self.get_connection() as db:
            async with db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
                return row[0] if row else default

    async def set_app_setting(self, key: str, value: str) -> None:
        """Saves or updates a persistent setting value by key in app_settings"""
        async with self.write_transaction() as db:
            await db.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """, (key, value))
            await db.commit()

    async def get_recent_story_music(self, limit: int = 10) -> List[str]:
        """Gets recently played story music filenames to avoid repetition across restarts"""
        raw = await self.get_app_setting("recent_story_music", "[]")
        try:
            items = json.loads(raw or "[]")
            return items[-limit:] if isinstance(items, list) else []
        except Exception:
            return []

    async def record_used_story_music(self, track_name: str, max_history: int = 20) -> None:
        """Records a used music track into persistent history to avoid repeats across restarts"""
        recent = await self.get_recent_story_music(limit=max_history)
        clean_name = os.path.basename(track_name)
        if clean_name in recent:
            recent.remove(clean_name)
        recent.append(clean_name)
        await self.set_app_setting("recent_story_music", json.dumps(recent[-max_history:]))


db_manager = DatabaseManager()

