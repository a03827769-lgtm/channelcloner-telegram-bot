import asyncio
import aiosqlite
import os
import shutil
import logging
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any, Tuple, Set
from database.models import (
    User, ChannelPair, Subscription, StorySettings, StorySourceChannel, StoryQueueItem,
    SupplierConfig, StoreProduct, StoreOrder
)
from services.security_vault import security_vault
from services.cache_manager import cache_manager
from config.settings import settings, PROJECT_ROOT
from config.plans import TIER_RANK, TRIAL_DAYS, daily_price

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
    # Bumped whenever init_db gains a migration; stored in PRAGMA user_version.
    SCHEMA_VERSION = 3

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or getattr(settings, "DB_PATH", "database/cloner.db")
        self._conn: Optional[aiosqlite.Connection] = None
        self._init_lock = asyncio.Lock()
        self._write_lock = ReentrantAsyncLock()
        self._admin_cache: Set[int] = set()
        self._closed_final = False

    def _notify_pair_cache_invalidated(self):
        try:
            from services.telethon_listener import telethon_listener
            telethon_listener.invalidate_pairs_cache()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    async def _ensure_connected(self) -> aiosqlite.Connection:
        """Ensures a single persistent connection exists. Lock only guards initialization."""
        if self._closed_final:
            raise RuntimeError("Database manager has been shut down")
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
        """Serialized write transaction on the shared connection.

        Commits when the block exits normally and rolls back on any exception, so no statement is ever
        left pending on the shared connection (where a later rollback by another writer would discard it).
        Nested use inside the same task joins the outer transaction; only the outermost block commits."""
        async with self._write_lock:
            conn = await self._ensure_connected()
            outermost = self._write_lock._depth == 1
            try:
                yield conn
            except BaseException:
                if outermost:
                    try:
                        await conn.rollback()
                    except Exception:
                        logger.debug("Rollback on write_transaction exception failed or not needed", exc_info=True)
                raise
            else:
                if outermost and conn.in_transaction:
                    await conn.commit()

    async def close(self, final: bool = False):
        """Closes the shared SQLite connection. With final=True (application shutdown) any later database
        call raises instead of silently reopening a connection whose worker thread would keep the
        process alive."""
        async with self._write_lock:
            async with self._init_lock:
                if final:
                    self._closed_final = True
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

    @staticmethod
    async def _ensure_columns(db: aiosqlite.Connection, table: str, columns: List[Tuple[str, str]]) -> None:
        """Adds the missing columns of `table` (checked through PRAGMA table_info, so nothing fails on reruns)."""
        cursor = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cursor.fetchall()}
        for col, col_type in columns:
            if col not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                existing.add(col)

    @staticmethod
    async def _create_unique_index(db: aiosqlite.Connection, name: str, table: str, columns: str, dedupe_sql: str) -> None:
        """Creates a UNIQUE index required by ON CONFLICT upserts, removing legacy duplicates first.
        Failure is fatal: without the index every upsert on that table would raise at runtime."""
        cur = await db.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,))
        if await cur.fetchone():
            return
        await db.execute(dedupe_sql)
        await db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}({columns})")

    @staticmethod
    async def _get_schema_version(db: aiosqlite.Connection) -> int:
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

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

            await self._ensure_columns(db, "posted_stories", [("grouped_id", "INTEGER DEFAULT NULL")])
            await db.execute("CREATE INDEX IF NOT EXISTS idx_posted_stories_group ON posted_stories (user_id, grouped_id)")

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
            await self._ensure_columns(db, "story_settings", story_settings_cols)

            # users: is_blocked + admin_source ('granted' for admins promoted from the admin bot;
            # environment super admins are recognised dynamically and never persisted)
            await self._ensure_columns(db, "users", [("is_blocked", "INTEGER DEFAULT 0"), ("admin_source", "TEXT DEFAULT NULL")])

            # Migrations for dynamic columns
            sub_columns = [
                ("trial_expires_at", "TIMESTAMP"),
                ("trial_notified", "INTEGER DEFAULT 0"),
                ("paid_notified", "INTEGER DEFAULT 0")
            ]
            await self._ensure_columns(db, "subscriptions", sub_columns)

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
                ("show_caption_above", "INTEGER DEFAULT 0"),
                # 1 = the owner paused the pair; 0 = active or suspended only for billing reasons.
                # Renewals re-activate billing-suspended pairs but never ones the owner paused.
                ("paused_by_user", "INTEGER DEFAULT 0"),
            ]
            await self._ensure_columns(db, "channel_pairs", pair_columns)

            # Backfill 14-day trial_expires_at for existing users if null
            try:
                await db.execute("""
                    UPDATE subscriptions
                    SET trial_expires_at = datetime(created_at, '+14 days')
                    WHERE trial_expires_at IS NULL
                """)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            await self._ensure_columns(db, "cloned_messages", [
                ("media_type", "TEXT DEFAULT 'text'"),
                ("source_channel", "TEXT"),
                ("target_channel", "TEXT"),
                ("story_id", "INTEGER"),
                ("status", "TEXT DEFAULT 'active'"),
                ("price", "REAL DEFAULT 0.0"),
                ("last_caption", "TEXT"),
            ])
            await self._ensure_columns(db, "drip_queue", [("error_message", "TEXT")])
            await self._ensure_columns(db, "channel_backups", [("media_group_id", "TEXT")])

            await db.execute("CREATE INDEX IF NOT EXISTS idx_pairs_user ON channel_pairs(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_pairs_active ON channel_pairs(is_active)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_pairs_source_id ON channel_pairs(source_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_pairs_target_id ON channel_pairs(target_id)")

            # UNIQUE indexes backing the ON CONFLICT upserts — required, so failures abort init.
            await self._create_unique_index(
                db, "idx_cloned_unique_pair_msg", "cloned_messages", "pair_id, source_msg_id",
                "DELETE FROM cloned_messages WHERE id NOT IN (SELECT MAX(id) FROM cloned_messages GROUP BY pair_id, source_msg_id)"
            )
            await self._create_unique_index(
                db, "idx_backups_pair_msg", "channel_backups", "pair_id, message_id",
                "DELETE FROM channel_backups WHERE id NOT IN (SELECT MAX(id) FROM channel_backups GROUP BY pair_id, message_id)"
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_media_group ON cloned_messages(pair_id, media_group_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_time ON cloned_messages(cloned_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_source_chan_msg ON cloned_messages(source_channel, source_msg_id)")
            # Edit/delete sync and own-post (loop) detection look messages up by the target post id
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_target_msg ON cloned_messages(target_msg_id)")
            # Legacy duplicates of the indexes above (same columns under another name) only slowed down inserts
            for legacy_index in ("idx_cloned_messages_pair_src", "idx_cloned_lookup", "idx_cloned_messages_src_chan_msg",
                                 "idx_cloned_messages_cloned_at", "idx_channel_pairs_user"):
                await db.execute(f"DROP INDEX IF EXISTS {legacy_index}")

            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_charge_id
                ON payments(telegram_payment_charge_id)
                WHERE telegram_payment_charge_id IS NOT NULL
                  AND telegram_payment_charge_id != ''
                  AND telegram_payment_charge_id NOT LIKE 'admin_manual_grant_%'
            """)

            # Environment super admins are recognised dynamically (settings.admin_ids) and get a synthetic
            # VIP plan from get_user_subscription(); nothing about them is persisted any more. Rows written by
            # the old seeding (is_admin = 1 and a 2099 VIP plan) must not outlive a removal from ADMIN_IDS.
            env_admin_ids = sorted(settings.admin_ids)
            # An empty IN () list is invalid SQL; an empty sub-select keeps NOT IN / IN semantics correct.
            env_placeholders = ",".join("?" * len(env_admin_ids)) if env_admin_ids else "SELECT NULL WHERE 0"
            if await self._get_schema_version(db) < 3:
                # One-time: admins that exist in the DB but not in the environment were promoted in the admin bot.
                await db.execute(
                    f"UPDATE users SET admin_source = 'granted' WHERE is_admin = 1 AND admin_source IS NULL "
                    f"AND user_id NOT IN ({env_placeholders})",
                    env_admin_ids
                )
                await db.execute(
                    f"UPDATE users SET admin_source = 'env' WHERE is_admin = 1 AND admin_source IS NULL "
                    f"AND user_id IN ({env_placeholders})",
                    env_admin_ids
                )
            # Former environment admins (seeded, not granted) lose the admin flag and the seeded lifetime VIP plan.
            await db.execute(
                f"UPDATE users SET is_admin = 0, admin_source = NULL WHERE admin_source = 'env' "
                f"AND user_id NOT IN ({env_placeholders})",
                env_admin_ids
            )
            await db.execute(
                f"UPDATE subscriptions SET tier = 'free', expires_at = NULL "
                f"WHERE expires_at = '2099-12-31T23:59:59' AND user_id NOT IN ({env_placeholders})",
                env_admin_ids
            )

            await db.commit()

            # Admin cache: delegated (database-granted) admins only; environment super admins are checked
            # against settings.admin_ids directly, so removing one from the configuration takes effect
            self._admin_cache = set()
            try:
                cur_admins = await db.execute("SELECT user_id FROM users WHERE is_admin = 1")
                admin_rows = await cur_admins.fetchall()
                for ar in admin_rows:
                    self._admin_cache.add(ar[0])
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            # --- SUPPLIER & STORE TABLES ---
            await db.execute("""
                CREATE TABLE IF NOT EXISTS supplier_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider_name TEXT NOT NULL DEFAULT 'Standard SMM/Reseller API',
                    api_url TEXT NOT NULL DEFAULT 'https://justanotherpanel.com/api/v2',
                    api_key TEXT NOT NULL DEFAULT '',
                    margin_percent REAL DEFAULT 25.0,
                    balance REAL DEFAULT 0.0,
                    currency TEXT DEFAULT 'USD',
                    last_synced_at TIMESTAMP,
                    is_active INTEGER DEFAULT 1
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS store_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    supplier_id INTEGER NOT NULL DEFAULT 1,
                    supplier_service_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT DEFAULT 'Telegram',
                    type TEXT DEFAULT 'Default',
                    supplier_rate REAL DEFAULT 0.0,
                    selling_price_stars INTEGER DEFAULT 50,
                    min_quantity INTEGER DEFAULT 10,
                    max_quantity INTEGER DEFAULT 10000,
                    is_available INTEGER DEFAULT 1,
                    stock_status TEXT DEFAULT 'in_stock',
                    description TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_store_products_supplier ON store_products(supplier_id, supplier_service_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_store_products_avail ON store_products(is_available, stock_status)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS store_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    product_name TEXT DEFAULT '',
                    quantity INTEGER DEFAULT 1,
                    price_stars INTEGER DEFAULT 0,
                    target_link TEXT DEFAULT '',
                    supplier_order_id INTEGER DEFAULT NULL,
                    status TEXT DEFAULT 'completed',
                    admin_notified INTEGER DEFAULT 0,
                    note TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_store_orders_user ON store_orders(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_store_orders_status ON store_orders(status)")

            # Seed default supplier config if empty
            cur_sup = await db.execute("SELECT id FROM supplier_configs LIMIT 1")
            if not await cur_sup.fetchone():
                await db.execute("""
                    INSERT INTO supplier_configs (provider_name, api_url, api_key, margin_percent, balance, is_active)
                    VALUES ('Standard Reseller API', 'https://justanotherpanel.com/api/v2', '', 25.0, 0.0, 1)
                """)

            # Automatically deduplicate redundant channel pairs and clean invalid loop pairs
            await self._deduplicate_existing_pairs(db)

            # Preload recent 100,000 cloned message IDs into in-memory deduplication cache
            cur = await db.execute("SELECT pair_id, source_msg_id FROM cloned_messages ORDER BY id DESC LIMIT 100000")
            rows = await cur.fetchall()
            if rows:
                await cache_manager.dedup_cache.add_batch((r[0], r[1]) for r in rows)

            await db.execute(f"PRAGMA user_version = {int(self.SCHEMA_VERSION)}")
            logger.info(f"Database initialized (schema v{self.SCHEMA_VERSION}). Preloaded {len(rows)} message IDs into the dedup cache.")

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

    @staticmethod
    def _row_to_subscription(row: Any) -> Subscription:
        keys = row.keys()
        return Subscription(
            user_id=row["user_id"],
            tier=row["tier"] or "free",
            expires_at=row["expires_at"],
            trial_expires_at=row["trial_expires_at"] if "trial_expires_at" in keys else None,
            trial_notified=bool(row["trial_notified"]) if "trial_notified" in keys else False,
            paid_notified=bool(row["paid_notified"]) if "paid_notified" in keys else False,
            stars_spent=row["stars_spent"] or 0,
            created_at=row["created_at"]
        )

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

        for _attempt in range(3):
            async with self.get_connection() as db:
                cursor = await db.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,))
                row = await cursor.fetchone()

            if not row:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                trial_exp = (now + timedelta(days=TRIAL_DAYS)).isoformat()
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
                continue  # re-read what is actually stored (another task may have created it first)

            sub = self._row_to_subscription(row)

            if not sub.trial_expires_at and sub.created_at:
                c_date = Subscription._parse_iso_to_utc_naive(sub.created_at)
                if c_date:
                    trial_exp = (c_date + timedelta(days=TRIAL_DAYS)).isoformat()
                    async with self.write_transaction() as wdb:
                        await wdb.execute(
                            "UPDATE subscriptions SET trial_expires_at = ? WHERE user_id = ? AND trial_expires_at IS NULL",
                            (trial_exp, user_id)
                        )
                    sub.trial_expires_at = trial_exp

            if not sub.is_active:
                # Lazily suspend an expired plan. Compare-and-set against the row we read: when a renewal
                # committed in between, nothing is changed and the fresh row is read again.
                if not await self._suspend_expired_subscription(user_id, row):
                    continue
                sub.tier = "free"

            await cache_manager.sub_cache.set(f"sub_{user_id}", sub)
            return sub

        # Extremely contended: return the current state without caching it
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
        return self._row_to_subscription(row) if row else Subscription(user_id=user_id)

    async def _suspend_expired_subscription(self, user_id: int, observed_row: Any) -> bool:
        """Downgrades an expired paid plan to free and suspends the user's pairs (billing suspension —
        pairs the owner paused keep paused_by_user = 1). Returns False when the subscription changed
        after `observed_row` was read, in which case nothing is modified."""
        async with self.write_transaction() as wdb:
            cursor = await wdb.execute(
                "SELECT tier, expires_at, trial_expires_at FROM subscriptions WHERE user_id = ?", (user_id,)
            )
            current = await cursor.fetchone()
            if not current or (current[0], current[1], current[2]) != (
                observed_row["tier"], observed_row["expires_at"], observed_row["trial_expires_at"]
            ):
                return False
            if current[0] != "free":
                await wdb.execute("UPDATE subscriptions SET tier = 'free' WHERE user_id = ?", (user_id,))
            cur_pairs = await wdb.execute(
                "UPDATE channel_pairs SET is_active = 0 WHERE user_id = ? AND is_active = 1", (user_id,)
            )
            deactivated = (cur_pairs.rowcount or 0) > 0
        if deactivated:
            self._notify_pair_cache_invalidated()
        return True

    async def is_payment_processed(self, charge_id: str) -> bool:
        """Checks whether a Telegram Stars payment charge_id has already been recorded in payments"""
        if not charge_id or charge_id.startswith("admin_manual_grant_"):
            return False
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT 1 FROM payments WHERE telegram_payment_charge_id = ?", (charge_id,))
            return bool(await cursor.fetchone())

    @staticmethod
    def _plan_period_after_purchase(
        now: datetime,
        current_tier: str,
        current_exp: Optional[datetime],
        bought_tier: str,
        days: int
    ) -> Tuple[str, datetime]:
        """Tier and expiry after buying `days` of `bought_tier`.

        * nothing active / same tier: time is added on top of what is left;
        * upgrade (Pro -> VIP): the upgrade starts now and the unused Pro time is converted to VIP time
          at the price ratio, so nobody gets VIP days for Pro money;
        * lower tier bought while a higher one is active: the higher tier is kept and the purchased time
          is converted at the price ratio — a purchase never downgrades an active plan.
        """
        active = current_tier in ("pro", "vip") and current_exp is not None and current_exp > now
        if not active:
            return bought_tier, now + timedelta(days=days)
        if current_tier == bought_tier:
            return bought_tier, current_exp + timedelta(days=days)
        current_rate, bought_rate = daily_price(current_tier), daily_price(bought_tier)
        if TIER_RANK.get(bought_tier, 0) > TIER_RANK.get(current_tier, 0):
            remaining = current_exp - now
            credit = remaining * (current_rate / bought_rate) if bought_rate else timedelta(0)
            return bought_tier, now + timedelta(days=days) + credit
        credit_days = days * (bought_rate / current_rate) if current_rate else 0.0
        return current_tier, current_exp + timedelta(days=credit_days)

    async def _apply_plan_limits(self, db: aiosqlite.Connection, user_id: int, max_channels: int) -> None:
        """Inside an open write transaction: re-activates billing-suspended pairs up to the plan limit
        (oldest first, never pairs the owner paused) and suspends active pairs above the limit."""
        if max_channels > 0:
            await db.execute("""
                UPDATE channel_pairs SET is_active = 1
                WHERE id IN (
                    SELECT id FROM channel_pairs
                    WHERE user_id = ? AND COALESCE(paused_by_user, 0) = 0
                    ORDER BY id ASC LIMIT ?
                )
            """, (user_id, max_channels))
        await db.execute("""
            UPDATE channel_pairs SET is_active = 0
            WHERE user_id = ? AND is_active = 1 AND id NOT IN (
                SELECT id FROM channel_pairs WHERE user_id = ? AND is_active = 1 ORDER BY id ASC LIMIT ?
            )
        """, (user_id, user_id, max(0, max_channels)))

    async def enforce_plan_limits(self, user_id: int) -> None:
        """Brings the number of active pairs in line with the user's current plan."""
        if user_id in settings.admin_ids or await self.is_admin(user_id):
            return
        sub = await self.get_user_subscription(user_id)
        async with self.write_transaction() as db:
            await self._apply_plan_limits(db, user_id, sub.max_channels)
        self._notify_pair_cache_invalidated()

    async def activate_subscription(
        self,
        user_id: int,
        tier: str,
        stars: int,
        charge_id: str,
        days: int = 30
    ) -> Subscription:
        """Records a payment (idempotent on charge_id) and extends the user's plan.
        tier "free" is the paid private-mode unlock: it extends the trial window only."""
        if tier not in ("free", "pro", "vip"):
            raise ValueError(f"Unknown subscription tier: {tier!r}")
        days = max(1, int(days))
        is_admin_grant = bool(charge_id) and charge_id.startswith("admin_manual_grant_")

        async with self.write_transaction() as db:
            await db.execute("INSERT OR IGNORE INTO users (user_id, full_name) VALUES (?, ?)", (user_id, f"User {user_id}"))
            try:
                await db.execute(
                    "INSERT INTO payments (user_id, telegram_payment_charge_id, amount, tier) VALUES (?, ?, ?, ?)",
                    (user_id, charge_id, stars, tier)
                )
            except sqlite3.IntegrityError:
                cur_dup = await db.execute(
                    "SELECT 1 FROM payments WHERE telegram_payment_charge_id = ?", (charge_id,)
                )
                if not is_admin_grant and await cur_dup.fetchone():
                    logger.warning(f"Payment charge_id {charge_id} already processed. Skipping duplicate activation.")
                    sub = None
                else:
                    raise
            else:
                cursor = await db.execute(
                    "SELECT tier, expires_at, stars_spent, trial_expires_at FROM subscriptions WHERE user_id = ?", (user_id,)
                )
                row = await cursor.fetchone()
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cur_tier = (row[0] if row else None) or "free"
                cur_exp = Subscription._parse_iso_to_utc_naive(row[1]) if row and row[1] else None
                old_spent = (row[2] if row else 0) or 0
                trial_exp_str = row[3] if row else None

                if tier == "free":
                    current_trial = Subscription._parse_iso_to_utc_naive(trial_exp_str) if trial_exp_str else None
                    trial_base = current_trial if current_trial and current_trial > now else now
                    trial_exp_str = (trial_base + timedelta(days=days)).isoformat()
                    final_tier = cur_tier if cur_tier in ("pro", "vip") and cur_exp and cur_exp > now else "free"
                    exp_str = row[1] if final_tier != "free" and row else None
                else:
                    final_tier, new_exp = self._plan_period_after_purchase(now, cur_tier, cur_exp, tier, days)
                    exp_str = new_exp.isoformat(timespec="seconds")

                total_spent = old_spent + max(0, int(stars))
                await db.execute("""
                    INSERT INTO subscriptions (user_id, tier, expires_at, trial_expires_at, stars_spent, trial_notified, paid_notified)
                    VALUES (?, ?, ?, ?, ?, 0, 0)
                    ON CONFLICT(user_id) DO UPDATE SET
                        tier = excluded.tier,
                        expires_at = excluded.expires_at,
                        trial_expires_at = COALESCE(excluded.trial_expires_at, subscriptions.trial_expires_at),
                        stars_spent = excluded.stars_spent,
                        paid_notified = 0
                """, (user_id, final_tier, exp_str, trial_exp_str, total_spent))

                sub = Subscription(
                    user_id=user_id, tier=final_tier, expires_at=exp_str, trial_expires_at=trial_exp_str,
                    trial_notified=False, paid_notified=False, stars_spent=total_spent
                )
                await self._apply_plan_limits(db, user_id, sub.max_channels)

        if sub is None:
            await cache_manager.sub_cache.delete(f"sub_{user_id}")
            return await self.get_user_subscription(user_id)
        await cache_manager.sub_cache.set(f"sub_{user_id}", sub)
        self._notify_pair_cache_invalidated()
        return sub

    async def revoke_subscription(self, user_id: int) -> Subscription:
        """Revokes paid subscription, resets user tier to free, suspends pairs and story automation"""
        async with self.write_transaction() as db:
            await db.execute("UPDATE subscriptions SET tier = 'free', expires_at = NULL, paid_notified = 0 WHERE user_id = ?", (user_id,))
            await db.execute("UPDATE channel_pairs SET is_active = 0 WHERE user_id = ?", (user_id,))
            await db.execute("UPDATE story_settings SET is_active = 0 WHERE user_id = ?", (user_id,))
            await db.execute(
                "UPDATE story_queue SET status = 'skipped', error_message = 'VIP obunasi bekor qilingan' WHERE user_id = ? AND status = 'pending'",
                (user_id,)
            )
        await cache_manager.sub_cache.delete(f"sub_{user_id}")
        self._notify_pair_cache_invalidated()
        return await self.get_user_subscription(user_id)

    async def get_expired_trial_users_to_notify(self) -> List[Tuple[int, str]]:
        """Free users whose trial just ended and who never bought a paid plan (ex-customers get a different message)."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT s.user_id, u.full_name
                FROM subscriptions s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.tier = 'free'
                  AND s.trial_expires_at IS NOT NULL
                  AND datetime(s.trial_expires_at) <= datetime('now')
                  AND s.trial_notified = 0
                  AND u.is_blocked = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM payments p WHERE p.user_id = s.user_id AND p.tier IN ('pro', 'vip')
                  )
            """)
            rows = await cursor.fetchall()
            return [(r["user_id"], r["full_name"]) for r in rows]

    async def mark_trial_notified(self, user_id: int):
        async with self.write_transaction() as db:
            await db.execute("""
                INSERT INTO subscriptions (user_id, tier, trial_notified)
                VALUES (?, 'free', 1)
                ON CONFLICT(user_id) DO UPDATE SET trial_notified = 1
            """, (user_id,))
        await cache_manager.sub_cache.delete(f"sub_{user_id}")

    async def get_expiring_paid_users_to_notify(self) -> List[Tuple[int, str, str, str]]:
        """Paid-plan notifications in two stages (subscriptions.paid_notified: 0 none, 1 reminded, 2 expired):
        * PRO/VIP plans ending within 3 days that were not reminded yet;
        * plans that ended in the last 7 days and whose "expired" notice was not sent yet — including plans
          already suspended to 'free' by get_user_subscription (their tier is taken from the last payment).
        The caller tells the two apart by comparing expires_at with the current UTC time."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT s.user_id, u.full_name,
                       CASE WHEN s.tier IN ('pro', 'vip') THEN s.tier
                            ELSE COALESCE((SELECT p.tier FROM payments p
                                           WHERE p.user_id = s.user_id AND p.tier IN ('pro', 'vip')
                                           ORDER BY p.id DESC LIMIT 1), 'pro')
                       END AS plan_tier,
                       s.expires_at
                FROM subscriptions s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.expires_at IS NOT NULL
                  AND u.is_blocked = 0
                  AND (
                        (s.tier IN ('pro', 'vip')
                         AND datetime(s.expires_at) > datetime('now')
                         AND datetime(s.expires_at) <= datetime('now', '+3 days')
                         AND COALESCE(s.paid_notified, 0) = 0)
                     OR (datetime(s.expires_at) <= datetime('now')
                         AND datetime(s.expires_at) >= datetime('now', '-7 days')
                         AND COALESCE(s.paid_notified, 0) < 2)
                  )
            """)
            rows = await cursor.fetchall()
            return [(r["user_id"], r["full_name"], r["plan_tier"], r["expires_at"]) for r in rows]

    async def mark_paid_sub_notified(self, user_id: int, expired: bool = False):
        """Records the reminder (stage 1) or the "plan expired" notice (stage 2) for the current paid period."""
        async with self.write_transaction() as db:
            if expired:
                await db.execute("UPDATE subscriptions SET paid_notified = 2 WHERE user_id = ?", (user_id,))
            else:
                await db.execute(
                    "UPDATE subscriptions SET paid_notified = MAX(COALESCE(paid_notified, 0), 1) WHERE user_id = ?",
                    (user_id,)
                )
        await cache_manager.sub_cache.delete(f"sub_{user_id}")

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
        store_value = value
        if key == "telethon_session":
            store_value = security_vault.encrypt_secret(value)

        async with self.write_transaction() as db:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                (key, store_value)
            )
        # Invalidate after commit so a concurrent reader cannot re-cache the old value
        await cache_manager.settings_cache.delete(f"set_{key}")

    async def delete_setting(self, key: str):
        async with self.write_transaction() as db:
            await db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        await cache_manager.settings_cache.delete(f"set_{key}")

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
        """Registers the user or refreshes their profile (name/username) from a real Telegram update.

        Admin rights are never derived from `is_admin` here: environment super admins are recognised
        dynamically through settings.admin_ids, and delegated admins only through set_admin_status()."""
        async with self.write_transaction() as db:
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                final_username = username if username else row["username"]
                if row["full_name"] != full_name or row["username"] != final_username or row["is_blocked"]:
                    await db.execute(
                        "UPDATE users SET full_name = ?, username = ?, is_blocked = 0 WHERE user_id = ?",
                        (full_name, final_username, user_id)
                    )
                db_admin = bool(row["is_admin"])
                created_at = row["created_at"]
            else:
                await db.execute(
                    "INSERT INTO users (user_id, full_name, username, is_admin, is_blocked) VALUES (?, ?, ?, 0, 0)",
                    (user_id, full_name, username)
                )
                final_username = username
                db_admin = False
                created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if db_admin:
            self._admin_cache.add(user_id)
        else:
            self._admin_cache.discard(user_id)
        is_admin_now = db_admin or user_id in settings.admin_ids
        return User(
            user_id=user_id,
            full_name=full_name,
            username=final_username,
            created_at=created_at,
            is_admin=is_admin_now,
            is_blocked=False
        )

    add_user = get_or_create_user

    async def set_admin_status(self, user_id: int, is_admin: bool) -> bool:
        """Sets or revokes admin privileges for user_id. Super admins in settings.admin_ids cannot be revoked."""
        if not is_admin and user_id in settings.admin_ids:
            logger.warning(f"Super admin {user_id} cannot be revoked.")
            return False

        flag = 1 if is_admin else 0
        async with self.write_transaction() as db:
            await db.execute("INSERT OR IGNORE INTO users (user_id, full_name) VALUES (?, ?)", (user_id, f"User {user_id}"))
            await db.execute(
                "UPDATE users SET is_admin = ?, admin_source = ? WHERE user_id = ?",
                (flag, "granted" if is_admin else None, user_id)
            )
        if is_admin:
            self._admin_cache.add(user_id)
        else:
            self._admin_cache.discard(user_id)
        await cache_manager.sub_cache.delete(f"sub_{user_id}")
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
        """Admin check without a database query: environment super admins (settings.admin_ids, which
        includes PRIMARY_SUPER_ADMIN_ID) and the cached delegated admins."""
        try:
            uid = int(user_id)
        except (ValueError, TypeError):
            return False
        return uid > 0 and (uid in settings.admin_ids or uid in self._admin_cache)

    async def is_admin(self, user_id: Any) -> bool:
        try:
            uid = int(user_id)
        except (ValueError, TypeError):
            return False
        if uid <= 0:
            return False
        if uid in settings.admin_ids:
            return True
        user = await self.get_user_by_id(uid)
        if user and user.is_admin:
            self._admin_cache.add(uid)
            return True
        self._admin_cache.discard(uid)
        return False

    async def get_broadcast_recipient_ids(self) -> List[int]:
        """Reachable private-chat users only: not blocked and a positive (user) id, never a group/channel id."""
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT user_id FROM users WHERE is_blocked = 0 AND user_id > 0 ORDER BY user_id")
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

    async def get_users_detailed_page(self, offset: int = 0, limit: int = 10) -> Tuple[List[Dict[str, Any]], int]:
        """One page of users (newest first) with plan and channel count, plus the total user count."""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        async with self.get_connection() as db:
            cur_total = await db.execute("SELECT COUNT(*) FROM users")
            total_row = await cur_total.fetchone()
            total = int(total_row[0]) if total_row else 0
            cursor = await db.execute("""
                SELECT u.user_id, u.full_name, u.username, u.created_at,
                       COALESCE(s.tier, 'free') as tier,
                       s.expires_at, s.trial_expires_at,
                       (SELECT COUNT(*) FROM channel_pairs p WHERE p.user_id = u.user_id) as channel_count
                FROM users u
                LEFT JOIN subscriptions s ON u.user_id = s.user_id
                ORDER BY u.created_at DESC, u.user_id DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows], total

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
        Deactivates (never deletes) self-loop pairs and exact duplicates of an earlier pair of the same user.
        Two pairs are duplicates only when both endpoints match by chat id, or — when an id is unknown on
        either side — by normalized username. A username alone never overrides differing chat ids.
        They are also marked paused_by_user, so a later plan purchase (_apply_plan_limits) never switches
        them back on; the owner can delete them, and resuming one is refused (set_pair_active_by_owner).
        """
        try:
            cursor = await db.execute(
                "SELECT id, user_id, source_channel, target_channel, source_id, target_id, is_active, "
                "COALESCE(paused_by_user, 0) AS paused FROM channel_pairs ORDER BY id ASC"
            )
            all_pairs = await cursor.fetchall()
            seen: Dict[Tuple[int, str, str], int] = {}
            to_deactivate: List[int] = []
            for row in all_pairs:
                src_key = self.pair_endpoint_key(row["source_channel"], row["source_id"])
                tgt_key = self.pair_endpoint_key(row["target_channel"], row["target_id"])
                needs_update = bool(row["is_active"]) or not row["paused"]
                if src_key and tgt_key and src_key == tgt_key:
                    if needs_update:
                        to_deactivate.append(row["id"])
                    continue
                key = (row["user_id"], src_key, tgt_key)
                if src_key and tgt_key and key in seen:
                    if needs_update:
                        to_deactivate.append(row["id"])
                else:
                    seen[key] = row["id"]
            if to_deactivate:
                logger.warning(f"Deactivating {len(to_deactivate)} duplicate or self-loop channel pairs: {to_deactivate}")
                await db.executemany(
                    "UPDATE channel_pairs SET is_active = 0, paused_by_user = 1 WHERE id = ?",
                    [(pid,) for pid in to_deactivate]
                )
        except Exception as e:
            logger.error(f"Error during channel pair deduplication: {e}", exc_info=True)

    @staticmethod
    def normalize_peer_id(value: Any) -> Optional[int]:
        """Raw positive chat id from any stored form (-1001234, -1234, 1234, '1234'); None when not numeric."""
        if value is None:
            return None
        text = str(value).strip()
        if text.startswith("-100") and len(text) > 4:
            text = text[4:]
        elif text.startswith("-"):
            text = text[1:]
        return int(text) if text.isdigit() else None

    @classmethod
    def pair_endpoint_key(cls, channel: Optional[str], peer_id: Any) -> str:
        """Canonical identity of a pair endpoint: 'id:<raw id>' when known, else 'ch:<normalized name>'."""
        raw_id = cls.normalize_peer_id(peer_id)
        if raw_id is None:
            raw_id = cls.normalize_peer_id(channel)
        if raw_id is not None:
            return f"id:{raw_id}"
        name = cls._normalize_channel_name(channel)
        return f"ch:{name}" if name else ""

    async def would_create_cycle(
        self,
        source_channel: str,
        target_channel: str,
        source_id: Optional[int] = None,
        target_id: Optional[int] = None,
        exclude_pair_id: Optional[int] = None
    ) -> bool:
        """True when a pair source -> target would close a loop with existing pairs of any user
        (A->B with B->A, or A->B->C->A): every cloned post would then be re-cloned forever."""
        src = self.pair_endpoint_key(source_channel, source_id)
        tgt = self.pair_endpoint_key(target_channel, target_id)
        if not src or not tgt:
            return False
        if src == tgt:
            return True
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT id, source_channel, source_id, target_channel, target_id FROM channel_pairs")
            rows = await cursor.fetchall()
        graph: Dict[str, Set[str]] = {}
        for r in rows:
            if exclude_pair_id is not None and r["id"] == exclude_pair_id:
                continue
            a = self.pair_endpoint_key(r["source_channel"], r["source_id"])
            b = self.pair_endpoint_key(r["target_channel"], r["target_id"])
            if a and b:
                graph.setdefault(a, set()).add(b)
        # A cycle appears iff the new target already reaches the new source
        stack, visited = [tgt], set()
        while stack:
            node = stack.pop()
            if node == src:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(graph.get(node, ()))
        return False

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

        # The duplicate check and the insert run under the same write lock, so two concurrent submissions
        # (double tap, forwarded album) cannot both create the pair.
        async with self.write_transaction() as db:
            existing = await self.find_duplicate_pair(user_id, source_channel, target_channel, source_id, target_id)
            if existing:
                if (source_id and not existing.source_id) or (target_id and not existing.target_id):
                    await self.update_pair_ids(existing.id, source_id or existing.source_id, target_id or existing.target_id)
                if (source_title and source_title != existing.source_title) or (target_title and target_title != existing.target_title):
                    await db.execute(
                        "UPDATE channel_pairs SET source_title = COALESCE(?, source_title), target_title = COALESCE(?, target_title) WHERE id = ?",
                        (source_title or None, target_title or None, existing.id)
                    )
                self._notify_pair_cache_invalidated()
                return existing.id


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

    async def set_pair_active_by_owner(self, pair_id: int, active: bool, bypass_limits: bool = False) -> Tuple[Optional[bool], Optional[str]]:
        """Owner pauses or resumes a pair.

        Returns (new_is_active, None) on success, or (current_is_active, error_code) where error_code is
        "not_found", "self_loop" (source and target are the same chat), "cycle" (resuming would close a
        loop with other pairs), "duplicate" (another active pair of the owner connects the same chats),
        "subscription_inactive" (plan/trial expired) or "plan_limit" (active pairs already at the plan
        maximum). Admin-owned pairs and bypass_limits=True skip only the plan checks, never the loop and
        duplicate checks: those would make every post be cloned twice or forever."""
        pair = await self.get_pair_by_id(pair_id)
        if not pair:
            return None, "not_found"
        if not active:
            async with self.write_transaction() as db:
                await db.execute("UPDATE channel_pairs SET is_active = 0, paused_by_user = 1 WHERE id = ?", (pair_id,))
            self._notify_pair_cache_invalidated()
            return False, None

        src_key = self.pair_endpoint_key(pair.source_channel, pair.source_id)
        tgt_key = self.pair_endpoint_key(pair.target_channel, pair.target_id)
        if src_key and src_key == tgt_key:
            return pair.is_active, "self_loop"
        if await self.would_create_cycle(pair.source_channel, pair.target_channel, pair.source_id, pair.target_id,
                                         exclude_pair_id=pair_id):
            return pair.is_active, "cycle"

        owner_is_admin = self.is_admin_sync(pair.user_id) or await self.is_admin(pair.user_id)
        if not (bypass_limits or owner_is_admin):
            sub = await self.get_user_subscription(pair.user_id)
            if not sub.is_active:
                return pair.is_active, "subscription_inactive"
        async with self.write_transaction() as db:
            cur = await db.execute(
                "SELECT source_channel, source_id, target_channel, target_id FROM channel_pairs "
                "WHERE user_id = ? AND is_active = 1 AND id != ?",
                (pair.user_id, pair_id)
            )
            for other in await cur.fetchall():
                if (self.pair_endpoint_key(other["source_channel"], other["source_id"]) == src_key
                        and self.pair_endpoint_key(other["target_channel"], other["target_id"]) == tgt_key):
                    return pair.is_active, "duplicate"
            if not (bypass_limits or owner_is_admin):
                cur = await db.execute(
                    "SELECT COUNT(*) FROM channel_pairs WHERE user_id = ? AND is_active = 1 AND id != ?",
                    (pair.user_id, pair_id)
                )
                active_count = (await cur.fetchone())[0]
                if active_count >= sub.max_channels:
                    return pair.is_active, "plan_limit"
            await db.execute("UPDATE channel_pairs SET is_active = 1, paused_by_user = 0 WHERE id = ?", (pair_id,))
        self._notify_pair_cache_invalidated()
        return True, None

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
        """Stores the resolved source chat id. When the source chat really changes (e.g. a group was
        migrated to a supergroup) the old chat's message ids no longer mean anything, so the catch-up
        watermark is reset."""
        async with self.write_transaction() as db:
            cursor = await db.execute("SELECT source_id FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return
            old_raw = self.normalize_peer_id(row[0])
            new_raw = self.normalize_peer_id(source_id)
            if old_raw is not None and new_raw is not None and old_raw != new_raw:
                await db.execute(
                    "UPDATE channel_pairs SET source_id = ?, last_seen_msg_id = NULL WHERE id = ?", (source_id, pair_id)
                )
            else:
                await db.execute("UPDATE channel_pairs SET source_id = ? WHERE id = ?", (source_id, pair_id))
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

    async def get_recent_image_hashes(self, days: int = 30, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Image hashes of the last N days; restricted to one tenant's pairs when user_id is given."""
        async with self.get_connection() as db:
            if user_id is not None:
                cursor = await db.execute("""
                    SELECT ih.pair_id, ih.source_channel, ih.source_msg_id, ih.phash, ih.price, ih.created_at
                    FROM image_hashes ih
                    JOIN channel_pairs cp ON cp.id = ih.pair_id
                    WHERE cp.user_id = ? AND ih.created_at >= datetime('now', ?)
                """, (user_id, f"-{int(days)} days"))
            else:
                cursor = await db.execute("""
                    SELECT pair_id, source_channel, source_msg_id, phash, price, created_at
                    FROM image_hashes
                    WHERE created_at >= datetime('now', ?)
                """, (f"-{int(days)} days",))
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

    async def get_cloned_messages_for_source(
        self,
        source_msg_id: int,
        peer_id: Optional[int] = None,
        username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Destination posts cloned from one source message, across every pair watching that source.

        Pairs are matched by the source chat id in all of its stored forms (raw / -100 prefixed), or by
        username for pairs whose chat id was never resolved. Uses the (pair_id, source_msg_id) index."""
        raw_id = self.normalize_peer_id(peer_id)
        clean_name = (username or "").lstrip("@").lower().strip()
        clauses, params = [], [source_msg_id]
        if raw_id is not None:
            clauses.append("cp.source_id IN (?, ?)")
            params.extend([raw_id, int(f"-100{raw_id}")])
        if clean_name:
            clauses.append("(cp.source_id IS NULL AND LOWER(TRIM(REPLACE(cp.source_channel, '@', ''))) = ?)")
            params.append(clean_name)
        if not clauses:
            return []
        async with self.get_connection() as db:
            cursor = await db.execute(f"""
                SELECT cm.*, cp.target_channel AS pair_target_channel, cp.target_id AS pair_target_id,
                       cp.user_id AS pair_user_id, cp.is_active AS pair_is_active
                FROM channel_pairs cp
                JOIN cloned_messages cm ON cm.pair_id = cp.id AND cm.source_msg_id = ?
                WHERE {" OR ".join(clauses)}
            """, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def is_own_clone_post(self, chat_peer_id: Any, message_id: int) -> bool:
        """True when message `message_id` in chat `chat_peer_id` is a post this system published as a clone
        (used to break A->B->A repost loops)."""
        raw_id = self.normalize_peer_id(chat_peer_id)
        if raw_id is None or not message_id:
            return False
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT 1 FROM cloned_messages cm
                JOIN channel_pairs cp ON cm.pair_id = cp.id
                WHERE cm.target_msg_id = ? AND cp.target_id IN (?, ?)
                LIMIT 1
            """, (message_id, raw_id, int(f"-100{raw_id}")))
            return await cursor.fetchone() is not None

    async def search_user_listings(
        self,
        user_id: int,
        text: str = "",
        price_range: Optional[Tuple[float, float]] = None,
        limit: int = 15
    ) -> List[Dict[str, Any]]:
        """Published clones of the user's own pairs whose caption contains `text` (case-insensitive for
        ASCII) and, when given, whose detected price lies within `price_range`. Newest first. Each row
        carries the pair's target (pair_target_channel, pair_target_id) for building post links."""
        sql = """
            SELECT cm.id, cm.target_channel, cm.target_msg_id, cm.status, cm.price, cm.last_caption,
                   cp.target_channel AS pair_target_channel, cp.target_id AS pair_target_id
            FROM cloned_messages cm
            JOIN channel_pairs cp ON cp.id = cm.pair_id
            WHERE cp.user_id = ?
              AND cm.last_caption IS NOT NULL AND TRIM(cm.last_caption) != ''
        """
        params: List[Any] = [user_id]
        if price_range is not None:
            sql += " AND cm.price > 0 AND cm.price BETWEEN ? AND ?"
            params.extend([float(price_range[0]), float(price_range[1])])
        if text:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND cm.last_caption LIKE ? ESCAPE '\\'"
            params.append(f"%{escaped}%")
        sql += " ORDER BY cm.id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        async with self.get_connection() as db:
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_cloned_message_caption(self, cloned_id: int, caption: Optional[str]):
        """Stores the caption that is currently shown on the destination post."""
        async with self.write_transaction() as db:
            await db.execute("UPDATE cloned_messages SET last_caption = ? WHERE id = ?", (caption, cloned_id))

    async def mark_cloned_message_delivered(
        self,
        pair_id: int,
        source_msg_id: int,
        target_msg_id: Optional[int],
        target_channel: Optional[str] = None,
        last_caption: Optional[str] = None
    ):
        """Completes the mapping of a drip-queued post once it has actually been published."""
        async with self.write_transaction() as db:
            await db.execute("""
                UPDATE cloned_messages
                SET target_msg_id = COALESCE(?, target_msg_id),
                    target_channel = COALESCE(?, target_channel),
                    last_caption = COALESCE(?, last_caption),
                    status = 'active'
                WHERE pair_id = ? AND source_msg_id = ?
            """, (target_msg_id, target_channel, last_caption, pair_id, source_msg_id))

    async def forget_cloned_messages(self, pair_id: int, source_msg_ids: List[int]):
        """Removes processing records (e.g. of a drip item that could not be delivered) so catch-up and
        history cloning can pick those source messages up again."""
        if not source_msg_ids:
            return
        async with self.write_transaction() as db:
            await db.executemany(
                "DELETE FROM cloned_messages WHERE pair_id = ? AND source_msg_id = ? AND target_msg_id IS NULL",
                [(pair_id, mid) for mid in source_msg_ids]
            )
        for mid in source_msg_ids:
            await cache_manager.dedup_cache.discard((pair_id, mid))

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
        """Highest source message id known for the pair (cloned records or the stored watermark)."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT MAX(COALESCE((SELECT MAX(source_msg_id) FROM cloned_messages WHERE pair_id = ?), -1),
                           COALESCE(last_seen_msg_id, -1))
                FROM channel_pairs WHERE id = ?
            """, (pair_id, pair_id))
            row = await cursor.fetchone()
            if row and row[0] is not None and row[0] >= 0:
                return int(row[0])
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
        """Retention for clone records. The (pair_id, source_msg_id) rows are the deduplication ledger that
        history cloning relies on, so they are kept for two years; only the stored caption text is dropped
        after `days`. Finished drip and story queue items are removed after 14 / 30 days."""
        async with self.write_transaction() as db:
            await db.execute(
                "UPDATE cloned_messages SET last_caption = NULL WHERE last_caption IS NOT NULL AND cloned_at < datetime('now', ?)",
                (f"-{int(days)} days",)
            )
            cur1 = await db.execute("DELETE FROM cloned_messages WHERE cloned_at < datetime('now', '-730 days')")
            deleted_cloned = cur1.rowcount if cur1.rowcount is not None else 0
            cur2 = await db.execute(
                "DELETE FROM drip_queue WHERE status IN ('sent', 'failed', 'skipped') "
                "AND (created_at < datetime('now', '-14 days') OR scheduled_at < datetime('now', '-14 days'))"
            )
            deleted_drip = cur2.rowcount if cur2.rowcount is not None else 0
            await db.execute(
                "DELETE FROM story_queue WHERE status IN ('sent', 'completed', 'failed', 'skipped') "
                "AND (created_at < datetime('now', '-30 days') OR scheduled_at < datetime('now', '-30 days'))"
            )
            return deleted_cloned, deleted_drip

    async def run_maintenance(self) -> Dict[str, int]:
        """Daily housekeeping with one consistent retention policy (called by run.py)."""
        deleted_cloned, deleted_drip = await self.clean_old_cloned_messages(days=180)
        stats = {"deleted_cloned_messages": deleted_cloned, "deleted_drip_items": deleted_drip}
        async with self.write_transaction() as db:
            for name, sql in (
                # Abandoned wizard conversations
                ("deleted_fsm_rows", "DELETE FROM fsm_storage WHERE updated_at < datetime('now', '-7 days')"),
                # Visual duplicate detection only looks back 30 days
                ("deleted_image_hashes", "DELETE FROM image_hashes WHERE created_at < datetime('now', '-45 days')"),
                # Listing fingerprints are compared over a 14 day window
                ("deleted_listing_hashes", "DELETE FROM story_dedup_hashes WHERE posted_at < datetime('now', '-30 days')"),
                ("deleted_posted_stories", "DELETE FROM posted_stories WHERE posted_at < datetime('now', '-180 days')"),
                ("deleted_store_orders", "DELETE FROM store_orders WHERE status = 'awaiting_payment' AND created_at < datetime('now', '-2 days')"),
            ):
                cur = await db.execute(sql)
                stats[name] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        await self.checkpoint()
        return stats

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

    def _backup_dir(self) -> str:
        """data/backups inside the project for the project database; a sibling 'backups' folder otherwise."""
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        project_root = str(PROJECT_ROOT)
        try:
            inside_project = os.path.commonpath([db_dir, project_root]) == project_root
        except ValueError:
            inside_project = False
        return os.path.join(project_root, "data", "backups") if inside_project else os.path.join(db_dir, "backups")

    async def create_backup_file(self) -> Optional[str]:
        """Creates a timestamped snapshot backup of the database using SQLite online backup or VACUUM INTO"""
        if not os.path.exists(self.db_path):
            return None

        backup_dir = self._backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        # A random suffix keeps concurrent backups (double tap, two admins) from sharing one file
        backup_path = os.path.join(backup_dir, f"backup_cloner_{timestamp}_{os.urandom(3).hex()}.db")

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

    async def get_user_daily_clone_counts(self, user_id: int, days: int = 7) -> List[Dict[str, Any]]:
        """Per-day cloned message counts (UTC) for the user's pairs, oldest day first, zero-filled."""
        days = max(1, min(int(days), 90))
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT date(m.cloned_at) AS day, COUNT(*) AS cnt
                FROM cloned_messages m
                JOIN channel_pairs p ON m.pair_id = p.id
                WHERE p.user_id = ? AND m.target_msg_id IS NOT NULL
                  AND m.cloned_at >= date('now', ?)
                GROUP BY date(m.cloned_at)
            """, (user_id, f"-{days - 1} days"))
            rows = await cursor.fetchall()
        counts = {r[0]: r[1] for r in rows}
        today = datetime.now(timezone.utc).date()
        result = []
        for offset in range(days - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            result.append({"date": day, "count": int(counts.get(day, 0))})
        return result

    async def get_user_recent_clones(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Most recent successfully delivered posts across the user's pairs (activity feed)."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT m.id, m.pair_id, m.source_msg_id, m.target_msg_id, m.media_type, m.cloned_at,
                       p.source_channel AS cp_src, p.target_channel AS cp_tgt
                FROM cloned_messages m
                JOIN channel_pairs p ON m.pair_id = p.id
                WHERE p.user_id = ? AND m.target_msg_id IS NOT NULL
                ORDER BY m.id DESC
                LIMIT ?
            """, (user_id, max(1, min(int(limit), 50))))
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

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

    # is_active is not updatable here: pausing/resuming goes through set_pair_active_by_owner (plan limits,
    # paused_by_user, loop and duplicate checks)
    UPDATABLE_PAIR_COLUMNS = frozenset({
        "clean_links", "clone_mode", "custom_signature", "remove_signature",
        "blacklist_words", "replace_words", "auto_translate", "target_lang", "source_lang",
        "image_watermark_type", "image_watermark_text", "image_watermark_pos",
        "video_watermark_type", "video_watermark_text", "video_watermark_pos",
        "drip_delay_minutes", "night_mode", "ai_paraphrase_mode", "tone_of_voice", "ad_action",
        "source_topic_id", "target_topic_id", "show_caption_above", "auto_premium_emojis",
        "auto_cta_buttons", "backup_enabled", "auto_catchup",
    })

    async def update_pair_fields(self, pair_id: int, fields: Dict[str, Any]) -> bool:
        """Updates a whitelisted subset of channel pair columns in one statement."""
        clean = {k: v for k, v in fields.items() if k in self.UPDATABLE_PAIR_COLUMNS}
        if not clean:
            return False
        columns = sorted(clean)
        assignments = ", ".join(f"{col} = ?" for col in columns)
        values = [int(clean[c]) if isinstance(clean[c], bool) else clean[c] for c in columns]
        async with self.write_transaction() as db:
            cursor = await db.execute(f"UPDATE channel_pairs SET {assignments} WHERE id = ?", (*values, pair_id))
        self._notify_pair_cache_invalidated()
        return cursor.rowcount > 0

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

    async def get_channel_backups(self, pair_id: int, limit: Optional[int] = None,
                                  after_message_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Archived posts of a pair in message order. `after_message_id` continues a previous page (keyset
        pagination), so large archives can be read page by page instead of all at once."""
        query = "SELECT * FROM channel_backups WHERE pair_id = ?"
        params: List[Any] = [pair_id]
        if after_message_id is not None:
            query += " AND message_id > ?"
            params.append(after_message_id)
        query += " ORDER BY message_id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))
        async with self.get_connection() as db:
            cursor = await db.execute(query, params)
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
                if not row:
                    return
                if not row[0] or row[0] in ("{}", "null", '""'):
                    await db.execute(
                        "DELETE FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND thread_id = ? AND destiny = ?",
                        (bot_id, chat_id, user_id, t_id, dest)
                    )
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
        """Backward-compatible alias of run_maintenance(). The retention periods are fixed by policy there;
        `max_age_days` is accepted for compatibility but no longer shortens the clone deduplication ledger
        (deleting it made history cloning re-post old messages)."""
        return await self.run_maintenance()

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

    STORY_SETTINGS_COLUMNS = frozenset({
        "source_channel", "source_title", "source_id", "target_type", "target_channel", "target_id",
        "min_price", "max_price", "require_photos", "require_price", "filter_demands", "background_style",
        "is_active", "prime_hours_enabled", "prime_hours_start", "prime_hours_end", "drip_delay_minutes",
        "max_stories_per_day", "enable_smart_badges", "pin_to_profile", "video_duration", "enable_ai_voice",
    })

    async def update_story_settings(self, user_id: int, **kwargs) -> StorySettings:
        """Partially updates story settings with a column-level UPDATE, so concurrent writers (e.g. a
        subscription revoke switching is_active off) are never overwritten by a stale full-row save."""
        clean = {k: v for k, v in kwargs.items() if k in self.STORY_SETTINGS_COLUMNS}
        if clean:
            async with self.write_transaction() as db:
                cursor = await db.execute("SELECT 1 FROM story_settings WHERE user_id = ?", (user_id,))
                if not await cursor.fetchone():
                    st = StorySettings(user_id=user_id)
                    for k, v in clean.items():
                        setattr(st, k, v)
                    await self.save_story_settings(st)
                else:
                    columns = sorted(clean)
                    values = [int(clean[c]) if isinstance(clean[c], bool) else clean[c] for c in columns]
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    await db.execute(
                        f"UPDATE story_settings SET {', '.join(f'{c} = ?' for c in columns)}, updated_at = ? WHERE user_id = ?",
                        (*values, now, user_id)
                    )
        return await self.get_story_settings(user_id)

    async def get_all_active_story_settings(self) -> List[StorySettings]:
        """Returns list of all active story settings across all users"""
        settings_list = []
        async with self.get_connection() as db:
            async with db.execute(
                """SELECT user_id, source_channel, source_title, source_id, target_type, target_channel, target_id,
                          min_price, max_price, require_photos, require_price, filter_demands, background_style,
                          is_active, prime_hours_enabled, prime_hours_start, prime_hours_end,
                          drip_delay_minutes, max_stories_per_day, enable_smart_badges, pin_to_profile,
                          video_duration, created_at, updated_at, enable_ai_voice
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
                        updated_at=row[23],
                        enable_ai_voice=bool(row[24]) if row[24] is not None else True
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
        """Adds a new source channel for multi-channel story cloner. Returns channel id or None if already exists.
        `channel_username` is '@name' (a bare name gets the '@'), a '-100<id>' channel id or an invite link."""
        clean_user = channel_username.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", clean_user):
            clean_user = f"@{clean_user}"
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
        """Deletes a source channel entry of the user; False when the user has no such entry."""
        async with self.write_transaction() as db:
            cursor = await db.execute("DELETE FROM story_source_channels WHERE user_id = ? AND id = ?", (user_id, channel_id))
            return cursor.rowcount > 0

    async def set_story_source_channel_active(self, user_id: int, channel_id: int, active: bool) -> bool:
        """Pauses or resumes one of the user's extra story source channels; False when it does not exist."""
        async with self.write_transaction() as db:
            cursor = await db.execute(
                "UPDATE story_source_channels SET is_active = ? WHERE id = ? AND user_id = ?",
                (1 if active else 0, channel_id, user_id)
            )
            return cursor.rowcount > 0

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
                WHERE status = 'pending' AND datetime(scheduled_at) <= datetime(?)
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

    # --- SUPPLIER & STORE MANAGEMENT ---

    async def get_supplier_config(self, supplier_id: int = 1) -> Optional[SupplierConfig]:
        """Fetches active supplier configuration (API URL, API Key, Margin)"""
        async with self.get_connection() as db:
            async with db.execute("""
                SELECT id, provider_name, api_url, api_key, margin_percent, balance, currency, last_synced_at, is_active
                FROM supplier_configs WHERE id = ?
            """, (supplier_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return SupplierConfig(
                    id=row[0],
                    provider_name=row[1],
                    api_url=row[2],
                    api_key=row[3],
                    margin_percent=row[4],
                    balance=row[5],
                    currency=row[6],
                    last_synced_at=row[7],
                    is_active=bool(row[8])
                )

    async def save_supplier_config(self, config: SupplierConfig) -> None:
        """Saves or updates supplier API configuration"""
        async with self.write_transaction() as db:
            if config.id:
                await db.execute("""
                    UPDATE supplier_configs
                    SET provider_name = ?, api_url = ?, api_key = ?, margin_percent = ?, balance = ?, currency = ?, is_active = ?
                    WHERE id = ?
                """, (config.provider_name, config.api_url, config.api_key, config.margin_percent, config.balance, config.currency, int(config.is_active), config.id))
            else:
                await db.execute("""
                    INSERT INTO supplier_configs (provider_name, api_url, api_key, margin_percent, balance, currency, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (config.provider_name, config.api_url, config.api_key, config.margin_percent, config.balance, config.currency, int(config.is_active)))
            await db.commit()

    async def update_supplier_balance_and_sync_time(self, supplier_id: int, balance: float) -> None:
        """Updates supplier cached balance and last synced timestamp"""
        async with self.write_transaction() as db:
            await db.execute("""
                UPDATE supplier_configs
                SET balance = ?, last_synced_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (balance, supplier_id))
            await db.commit()

    async def get_store_products(self, category: Optional[str] = None, only_available: bool = False) -> List[StoreProduct]:
        """Lists products from store catalogue with optional category filter"""
        async with self.get_connection() as db:
            sql = """
                SELECT id, supplier_id, supplier_service_id, name, category, type, supplier_rate,
                       selling_price_stars, min_quantity, max_quantity, is_available, stock_status,
                       description, created_at, updated_at
                FROM store_products
                WHERE 1=1
            """
            params: List[Any] = []
            if category:
                sql += " AND category = ?"
                params.append(category)
            if only_available:
                sql += " AND is_available = 1 AND stock_status = 'in_stock'"
            sql += " ORDER BY category ASC, id ASC"

            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
                products = []
                for r in rows:
                    products.append(StoreProduct(
                        id=r[0],
                        supplier_id=r[1],
                        supplier_service_id=r[2],
                        name=r[3],
                        category=r[4],
                        type=r[5],
                        supplier_rate=r[6],
                        selling_price_stars=r[7],
                        min_quantity=r[8],
                        max_quantity=r[9],
                        is_available=bool(r[10]),
                        stock_status=r[11],
                        description=r[12],
                        created_at=r[13],
                        updated_at=r[14]
                    ))
                return products

    async def get_store_product(self, product_id: int) -> Optional[StoreProduct]:
        """Fetches single product by ID"""
        async with self.get_connection() as db:
            async with db.execute("""
                SELECT id, supplier_id, supplier_service_id, name, category, type, supplier_rate,
                       selling_price_stars, min_quantity, max_quantity, is_available, stock_status,
                       description, created_at, updated_at
                FROM store_products WHERE id = ?
            """, (product_id,)) as cur:
                r = await cur.fetchone()
                if not r:
                    return None
                return StoreProduct(
                    id=r[0],
                    supplier_id=r[1],
                    supplier_service_id=r[2],
                    name=r[3],
                    category=r[4],
                    type=r[5],
                    supplier_rate=r[6],
                    selling_price_stars=r[7],
                    min_quantity=r[8],
                    max_quantity=r[9],
                    is_available=bool(r[10]),
                    stock_status=r[11],
                    description=r[12],
                    created_at=r[13],
                    updated_at=r[14]
                )

    async def upsert_synced_product(self, p_data: Dict[str, Any], margin_percent: float = 25.0) -> None:
        """Inserts or updates product from supplier API sync"""
        supplier_rate = float(p_data.get("rate") or 0.0)
        selling_stars = max(1, int(round(supplier_rate * 50 * (1.0 + margin_percent / 100.0))))

        async with self.write_transaction() as db:
            cur = await db.execute("""
                SELECT id FROM store_products
                WHERE supplier_id = ? AND supplier_service_id = ?
            """, (p_data.get("supplier_id", 1), p_data["service"]))
            row = await cur.fetchone()
            if row:
                await db.execute("""
                    UPDATE store_products
                    SET name = ?, category = ?, type = ?, supplier_rate = ?, selling_price_stars = ?,
                        min_quantity = ?, max_quantity = ?, is_available = 1, stock_status = 'in_stock',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (
                    p_data.get("name", ""),
                    p_data.get("category", "General"),
                    p_data.get("type", "Default"),
                    supplier_rate,
                    selling_stars,
                    int(p_data.get("min", 10)),
                    int(p_data.get("max", 10000)),
                    row[0]
                ))
            else:
                await db.execute("""
                    INSERT INTO store_products (
                        supplier_id, supplier_service_id, name, category, type, supplier_rate,
                        selling_price_stars, min_quantity, max_quantity, is_available, stock_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'in_stock')
                """, (
                    p_data.get("supplier_id", 1),
                    p_data["service"],
                    p_data.get("name", ""),
                    p_data.get("category", "General"),
                    p_data.get("type", "Default"),
                    supplier_rate,
                    selling_stars,
                    int(p_data.get("min", 10)),
                    int(p_data.get("max", 10000))
                ))
            await db.commit()

    async def mark_unlisted_products_out_of_stock(self, active_service_ids: Set[int], supplier_id: int = 1) -> int:
        """Marks any products not present in supplier API as 'out_of_stock' (Qolmadi)"""
        async with self.write_transaction() as db:
            cur = await db.execute("SELECT id, supplier_service_id FROM store_products WHERE supplier_id = ?", (supplier_id,))
            rows = await cur.fetchall()
            updated_count = 0
            for r_id, s_id in rows:
                if s_id not in active_service_ids:
                    await db.execute("UPDATE store_products SET stock_status = 'out_of_stock' WHERE id = ?", (r_id,))
                    updated_count += 1
            await db.commit()
            return updated_count

    async def create_store_order(self, order: StoreOrder) -> int:
        """Creates a new store order record"""
        async with self.write_transaction() as db:
            cur = await db.execute("""
                INSERT INTO store_orders (
                    user_id, product_id, product_name, quantity, price_stars,
                    target_link, supplier_order_id, status, admin_notified, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order.user_id, order.product_id, order.product_name, order.quantity,
                order.price_stars, order.target_link, order.supplier_order_id,
                order.status, int(order.admin_notified), order.note or ""
            ))
            await db.commit()
            return cur.lastrowid

    async def get_store_order(self, order_id: int) -> Optional[StoreOrder]:
        async with self.get_connection() as db:
            async with db.execute("""
                SELECT id, user_id, product_id, product_name, quantity, price_stars,
                       target_link, supplier_order_id, status, admin_notified, note, created_at
                FROM store_orders WHERE id = ?
            """, (order_id,)) as cur:
                r = await cur.fetchone()
        if not r:
            return None
        return StoreOrder(
            id=r[0], user_id=r[1], product_id=r[2], product_name=r[3], quantity=r[4],
            price_stars=r[5], target_link=r[6], supplier_order_id=r[7], status=r[8],
            admin_notified=bool(r[9]), note=r[10], created_at=r[11]
        )

    STORE_ORDER_UPDATABLE = frozenset({"status", "supplier_order_id", "admin_notified", "note"})

    async def update_store_order(self, order_id: int, **fields) -> None:
        clean = {k: v for k, v in fields.items() if k in self.STORE_ORDER_UPDATABLE}
        if not clean:
            return
        columns = sorted(clean)
        values = [int(clean[c]) if isinstance(clean[c], bool) else clean[c] for c in columns]
        async with self.write_transaction() as db:
            await db.execute(
                f"UPDATE store_orders SET {', '.join(f'{c} = ?' for c in columns)} WHERE id = ?",
                (*values, order_id)
            )
            await db.commit()

    async def mark_store_order_paid(self, order_id: int, user_id: int, charge_id: str, amount: int) -> bool:
        """Atomically records the Stars payment and moves the order from awaiting_payment to paid.
        Returns False for replays (charge already recorded) or orders that are not awaiting payment."""
        async with self.write_transaction() as db:
            cur = await db.execute(
                "SELECT status, user_id, price_stars FROM store_orders WHERE id = ?", (order_id,)
            )
            row = await cur.fetchone()
            if not row or row[0] != "awaiting_payment" or row[1] != user_id or int(amount) < int(row[2]):
                return False
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, full_name) VALUES (?, ?)", (user_id, f"User {user_id}")
            )
            try:
                await db.execute(
                    "INSERT INTO payments (user_id, telegram_payment_charge_id, amount, tier) VALUES (?, ?, ?, 'store')",
                    (user_id, charge_id, amount)
                )
            except Exception as e_pay:
                if "unique" in str(e_pay).lower():
                    await db.rollback()
                    return False
                raise
            await db.execute("UPDATE store_orders SET status = 'paid' WHERE id = ?", (order_id,))
            await db.commit()
            return True

    async def get_user_store_orders(self, user_id: int) -> List[StoreOrder]:
        """Gets user order history"""
        async with self.get_connection() as db:
            async with db.execute("""
                SELECT id, user_id, product_id, product_name, quantity, price_stars,
                       target_link, supplier_order_id, status, admin_notified, note, created_at
                FROM store_orders WHERE user_id = ? ORDER BY id DESC LIMIT 50
            """, (user_id,)) as cur:
                rows = await cur.fetchall()
                orders = []
                for r in rows:
                    orders.append(StoreOrder(
                        id=r[0],
                        user_id=r[1],
                        product_id=r[2],
                        product_name=r[3],
                        quantity=r[4],
                        price_stars=r[5],
                        target_link=r[6],
                        supplier_order_id=r[7],
                        status=r[8],
                        admin_notified=bool(r[9]),
                        note=r[10],
                        created_at=r[11]
                    ))
                return orders


db_manager = DatabaseManager()

