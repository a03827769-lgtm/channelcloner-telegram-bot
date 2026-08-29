import aiosqlite
import os
import shutil
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any, Tuple
from database.models import User, ChannelPair, ClonedMessage, Subscription, Payment
from services.security_vault import security_vault
from services.cache_manager import cache_manager
from config.settings import settings

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_path: str = "database/cloner.db"):
        self.db_path = db_path

    @asynccontextmanager
    async def get_connection(self):
        """High-performance SQLite async connection manager with WAL mode and memory cache"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode = WAL;")
            await conn.execute("PRAGMA synchronous = NORMAL;")
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute("PRAGMA busy_timeout = 30000;")
            await conn.execute("PRAGMA cache_size = -64000;")
            await conn.execute("PRAGMA temp_store = MEMORY;")
            yield conn

    async def init_db(self):
        """Initialize database tables, subscriptions, payments, indexes and vault"""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        async with self.get_connection() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_admin INTEGER DEFAULT 0
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id INTEGER PRIMARY KEY,
                    tier TEXT DEFAULT 'free',
                    expires_at TIMESTAMP,
                    trial_expires_at TIMESTAMP,
                    trial_notified INTEGER DEFAULT 0,
                    stars_spent INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    telegram_payment_charge_id TEXT,
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
                    cloned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pair_id) REFERENCES channel_pairs (id) ON DELETE CASCADE
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS drip_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_id INTEGER NOT NULL,
                    msg_data_json TEXT NOT NULL,
                    scheduled_at TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'pending',
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pair_id) REFERENCES channel_pairs (id) ON DELETE CASCADE
                )
            """)

            # Migrations for dynamic columns
            sub_columns = [
                ("trial_expires_at", "TIMESTAMP"),
                ("trial_notified", "INTEGER DEFAULT 0")
            ]
            for col, col_type in sub_columns:
                try:
                    await db.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

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
                ("auto_cta_buttons", "INTEGER DEFAULT 0"),
                ("backup_enabled", "INTEGER DEFAULT 1")
            ]
            for col, col_type in pair_columns:
                try:
                    await db.execute(f"ALTER TABLE channel_pairs ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

            # Backfill 14-day trial_expires_at for existing users if null
            try:
                await db.execute("""
                    UPDATE subscriptions
                    SET trial_expires_at = datetime(created_at, '+14 days')
                    WHERE trial_expires_at IS NULL
                """)
            except Exception:
                pass

            try:
                await db.execute("ALTER TABLE cloned_messages ADD COLUMN media_type TEXT DEFAULT 'text'")
            except Exception:
                pass

            await db.execute("CREATE INDEX IF NOT EXISTS idx_pairs_user ON channel_pairs(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_pairs_active ON channel_pairs(is_active)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_lookup ON cloned_messages(pair_id, source_msg_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_media_group ON cloned_messages(pair_id, media_group_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cloned_time ON cloned_messages(cloned_at)")

            await db.commit()

            # Preload recent 50,000 cloned message IDs into in-memory deduplication cache
            cur = await db.execute("SELECT pair_id, source_msg_id FROM cloned_messages ORDER BY id DESC LIMIT 50000")
            rows = await cur.fetchall()
            for r in rows:
                await cache_manager.dedup_cache.add((r[0], r[1]))

            logger.info(f"Database initialized for 100k high-load. Preloaded {len(rows)} message IDs into LRU cache.")

    # --- SUBSCRIPTIONS & TELEGRAM STARS ---

    async def get_user_subscription(self, user_id: int) -> Subscription:
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
                
                # If trial_expires_at is null, set to created_at + 14 days
                if not trial_exp and row["created_at"]:
                    try:
                        c_date = datetime.fromisoformat(row["created_at"])
                        trial_exp = (c_date + timedelta(days=14)).isoformat()
                        await db.execute("UPDATE subscriptions SET trial_expires_at = ? WHERE user_id = ?", (trial_exp, user_id))
                        await db.commit()
                    except Exception:
                        pass

                sub = Subscription(
                    user_id=row["user_id"],
                    tier=row["tier"],
                    expires_at=row["expires_at"],
                    trial_expires_at=trial_exp,
                    trial_notified=trial_notified,
                    stars_spent=row["stars_spent"],
                    created_at=row["created_at"]
                )
                if sub.tier != "free" and not sub.is_active:
                    await db.execute("UPDATE subscriptions SET tier = 'free' WHERE user_id = ?", (user_id,))
                    await db.commit()
                    sub.tier = "free"
                await cache_manager.sub_cache.set(f"sub_{user_id}", sub)
                return sub
            else:
                now = datetime.utcnow()
                trial_exp = (now + timedelta(days=14)).isoformat()
                await db.execute(
                    "INSERT INTO subscriptions (user_id, tier, trial_expires_at) VALUES (?, 'free', ?)",
                    (user_id, trial_exp)
                )
                await db.commit()
                sub = Subscription(user_id=user_id, tier="free", trial_expires_at=trial_exp, created_at=now.isoformat())
                await cache_manager.sub_cache.set(f"sub_{user_id}", sub)
                return sub

    async def activate_subscription(
        self,
        user_id: int,
        tier: str,
        stars: int,
        charge_id: str,
        days: int = 30
    ) -> Subscription:
        await cache_manager.sub_cache.delete(f"sub_{user_id}")
        async with self.get_connection() as db:
            await db.execute(
                "INSERT INTO payments (user_id, telegram_payment_charge_id, amount, tier) VALUES (?, ?, ?, ?)",
                (user_id, charge_id, stars, tier)
            )

            cursor = await db.execute("SELECT expires_at, stars_spent, trial_expires_at FROM subscriptions WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()

            now = datetime.utcnow()
            new_exp = now + timedelta(days=days)
            old_spent = 0
            trial_exp = None

            if row:
                if row[0]:
                    try:
                        curr_exp = datetime.fromisoformat(row[0])
                        if curr_exp > now:
                            new_exp = curr_exp + timedelta(days=days)
                    except Exception:
                        pass
                old_spent = row[1] or 0
                trial_exp = row[2] if len(row) > 2 else None

            exp_str = new_exp.isoformat()
            total_spent = old_spent + stars

            await db.execute("""
                INSERT INTO subscriptions (user_id, tier, expires_at, trial_expires_at, stars_spent)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    tier = excluded.tier,
                    expires_at = excluded.expires_at,
                    stars_spent = excluded.stars_spent
            """, (user_id, tier, exp_str, trial_exp, total_spent))

            await db.commit()
            sub = Subscription(user_id=user_id, tier=tier, expires_at=exp_str, trial_expires_at=trial_exp, stars_spent=total_spent)
            await cache_manager.sub_cache.set(f"sub_{user_id}", sub)
            return sub

    async def revoke_subscription(self, user_id: int) -> Subscription:
        """Revokes paid subscription and resets user tier to free, invalidating cache"""
        await cache_manager.sub_cache.delete(f"sub_{user_id}")
        async with self.get_connection() as db:
            await db.execute("UPDATE subscriptions SET tier = 'free', expires_at = NULL WHERE user_id = ?", (user_id,))
            await db.commit()
        return await self.get_user_subscription(user_id)

    async def get_expired_trial_users_to_notify(self) -> List[Tuple[int, str]]:
        now_str = datetime.utcnow().isoformat()
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
        async with self.get_connection() as db:
            await db.execute("UPDATE subscriptions SET trial_notified = 1 WHERE user_id = ?", (user_id,))
            await db.commit()

    async def can_user_add_channel(self, user_id: int, is_admin: bool = False) -> Tuple[bool, int, int]:
        pairs = await self.get_user_channel_pairs(user_id)
        current_count = len(pairs)

        if is_admin or user_id in settings.admin_ids:
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
                await cache_manager.settings_cache.set(f"set_{key}", decrypted)
                return decrypted
            await cache_manager.settings_cache.set(f"set_{key}", val)
            return val

    async def set_setting(self, key: str, value: str):
        await cache_manager.settings_cache.delete(f"set_{key}")
        store_value = value
        if key == "telethon_session":
            store_value = security_vault.encrypt_secret(value)

        async with self.get_connection() as db:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                (key, store_value)
            )
            await db.commit()

    async def delete_setting(self, key: str):
        await cache_manager.settings_cache.delete(f"set_{key}")
        async with self.get_connection() as db:
            await db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            await db.commit()

    # --- USERS ---

    async def get_or_create_user(self, user_id: int, full_name: str, username: Optional[str] = None, is_admin: bool = False) -> User:
        admin_flag = 1 if (is_admin or user_id in settings.admin_ids) else 0
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                await db.execute(
                    "UPDATE users SET full_name = ?, username = ?, is_admin = ? WHERE user_id = ?",
                    (full_name, username, admin_flag, user_id)
                )
                await db.commit()
                return User(
                    user_id=row["user_id"],
                    full_name=full_name,
                    username=username,
                    created_at=row["created_at"],
                    is_admin=bool(admin_flag)
                )
            else:
                await db.execute(
                    "INSERT INTO users (user_id, full_name, username, is_admin) VALUES (?, ?, ?, ?)",
                    (user_id, full_name, username, admin_flag)
                )
                await db.commit()
                return User(
                    user_id=user_id,
                    full_name=full_name,
                    username=username,
                    is_admin=bool(admin_flag)
                )

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
                    is_admin=bool(row["is_admin"])
                )
            return None

    async def get_all_user_ids(self) -> List[int]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT user_id FROM users")
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def get_all_users(self) -> List[User]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
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
                       COUNT(p.id) as channel_count
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
            if clean_q.isdigit():
                cursor = await db.execute("""
                    SELECT u.user_id, u.full_name, u.username, u.created_at,
                           COALESCE(s.tier, 'free') as tier,
                           s.expires_at, s.trial_expires_at,
                           COUNT(p.id) as channel_count
                    FROM users u
                    LEFT JOIN subscriptions s ON u.user_id = s.user_id
                    LEFT JOIN channel_pairs p ON u.user_id = p.user_id
                    WHERE u.user_id = ?
                    GROUP BY u.user_id
                """, (int(clean_q),))
            else:
                pattern = f"%{clean_q}%"
                cursor = await db.execute("""
                    SELECT u.user_id, u.full_name, u.username, u.created_at,
                           COALESCE(s.tier, 'free') as tier,
                           s.expires_at, s.trial_expires_at,
                           COUNT(p.id) as channel_count
                    FROM users u
                    LEFT JOIN subscriptions s ON u.user_id = s.user_id
                    LEFT JOIN channel_pairs p ON u.user_id = p.user_id
                    WHERE u.username LIKE ? OR u.full_name LIKE ?
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

    async def add_channel_pair(
        self,
        user_id: int,
        source_channel: str,
        source_title: str,
        target_channel: str,
        target_title: str,
        source_id: Optional[int] = None,
        target_id: Optional[int] = None,
        clean_links: bool = True,
        custom_signature: str = "",
        blacklist_words: str = "",
        replace_words: str = "",
        clone_mode: str = "clean",
        auto_translate: bool = False,
        target_lang: str = "uz",
        image_watermark_type: str = "none",
        image_watermark_text: str = "",
        is_protected_source: bool = False,
        affiliate_rules: str = ""
    ) -> int:
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO channel_pairs (
                    user_id, source_channel, source_title, source_id, target_channel, target_title, target_id,
                    clean_links, custom_signature, blacklist_words, replace_words, clone_mode,
                    auto_translate, target_lang, image_watermark_type, image_watermark_text,
                    is_protected_source, affiliate_rules
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, source_channel, source_title, source_id, target_channel, target_title, target_id,
                1 if clean_links else 0, custom_signature, blacklist_words, replace_words, clone_mode,
                1 if auto_translate else 0, target_lang, image_watermark_type, image_watermark_text,
                1 if is_protected_source else 0, affiliate_rules
            ))
            await db.commit()
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
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT is_active FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["is_active"] else 1
            await db.execute("UPDATE channel_pairs SET is_active = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            return bool(new_status)

    async def toggle_clean_links(self, pair_id: int) -> Optional[bool]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT clean_links FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["clean_links"] else 1
            await db.execute("UPDATE channel_pairs SET clean_links = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            return bool(new_status)

    async def toggle_auto_translate(self, pair_id: int, target_lang: str = "uz") -> Optional[bool]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT auto_translate FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["auto_translate"] else 1
            await db.execute("UPDATE channel_pairs SET auto_translate = ?, target_lang = ? WHERE id = ?", (new_status, target_lang, pair_id))
            await db.commit()
            return bool(new_status)

    async def update_watermark_settings(self, pair_id: int, wm_type: str, text: str, pos: str = "bottom_right"):
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE channel_pairs SET image_watermark_type = ?, image_watermark_text = ?, image_watermark_pos = ? WHERE id = ?",
                (wm_type, text, pos, pair_id)
            )
            await db.commit()

    async def toggle_protected_mode(self, pair_id: int) -> Optional[bool]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT is_protected_source FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["is_protected_source"] else 1
            await db.execute("UPDATE channel_pairs SET is_protected_source = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            return bool(new_status)

    async def update_affiliate_rules(self, pair_id: int, rules: str):
        async with self.get_connection() as db:
            await db.execute("UPDATE channel_pairs SET affiliate_rules = ? WHERE id = ?", (rules, pair_id))
            await db.commit()

    async def set_auto_translate(self, pair_id: int, enabled: bool, target_lang: str = "uz"):
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE channel_pairs SET auto_translate = ?, target_lang = ? WHERE id = ?",
                (1 if enabled else 0, target_lang, pair_id)
            )
            await db.commit()

    async def update_pair_signature(self, pair_id: int, signature: str):
        async with self.get_connection() as db:
            await db.execute("UPDATE channel_pairs SET custom_signature = ? WHERE id = ?", (signature, pair_id))
            await db.commit()

    async def update_blacklist(self, pair_id: int, blacklist_words: str):
        async with self.get_connection() as db:
            await db.execute("UPDATE channel_pairs SET blacklist_words = ? WHERE id = ?", (blacklist_words, pair_id))
            await db.commit()

    async def update_pair_blacklist(self, pair_id: int, blacklist_words: str):
        await self.update_blacklist(pair_id, blacklist_words)

    async def update_replace_words(self, pair_id: int, replace_words: str):
        async with self.get_connection() as db:
            await db.execute("UPDATE channel_pairs SET replace_words = ? WHERE id = ?", (replace_words, pair_id))
            await db.commit()

    async def update_pair_source_id(self, pair_id: int, source_id: int):
        async with self.get_connection() as db:
            await db.execute("UPDATE channel_pairs SET source_id = ? WHERE id = ?", (source_id, pair_id))
            await db.commit()

    async def update_pair_ids(self, pair_id: int, source_id: Optional[int] = None, target_id: Optional[int] = None):
        async with self.get_connection() as db:
            if source_id is not None and target_id is not None:
                await db.execute("UPDATE channel_pairs SET source_id = ?, target_id = ? WHERE id = ?", (source_id, target_id, pair_id))
            elif source_id is not None:
                await db.execute("UPDATE channel_pairs SET source_id = ? WHERE id = ?", (source_id, pair_id))
            elif target_id is not None:
                await db.execute("UPDATE channel_pairs SET target_id = ? WHERE id = ?", (target_id, pair_id))
            await db.commit()

    async def delete_pair(self, pair_id: int) -> bool:
        async with self.get_connection() as db:
            await db.execute("DELETE FROM cloned_messages WHERE pair_id = ?", (pair_id,))
            cursor = await db.execute("DELETE FROM channel_pairs WHERE id = ?", (pair_id,))
            await db.commit()
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
        media_type: str = "text"
    ):
        await cache_manager.dedup_cache.add((pair_id, source_msg_id))
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO cloned_messages (pair_id, source_msg_id, target_msg_id, media_group_id, media_type)
                VALUES (?, ?, ?, ?, ?)
            """, (pair_id, source_msg_id, target_msg_id, media_group_id, media_type))
            await db.commit()

    async def get_pair_analytics(self, pair_id: int) -> Dict[str, Any]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            tot_cur = await db.execute("SELECT COUNT(*) as total FROM cloned_messages WHERE pair_id = ?", (pair_id,))
            total = (await tot_cur.fetchone())["total"]

            today_cur = await db.execute(
                "SELECT COUNT(*) as cnt FROM cloned_messages WHERE pair_id = ? AND cloned_at >= date('now', 'start of day')",
                (pair_id,)
            )
            today = (await today_cur.fetchone())["cnt"]

            photos_cur = await db.execute("SELECT COUNT(*) as cnt FROM cloned_messages WHERE pair_id = ? AND media_type = 'photo'", (pair_id,))
            photos = (await photos_cur.fetchone())["cnt"]

            videos_cur = await db.execute("SELECT COUNT(*) as cnt FROM cloned_messages WHERE pair_id = ? AND media_type = 'video'", (pair_id,))
            videos = (await videos_cur.fetchone())["cnt"]

            return {
                "total_cloned": total,
                "today_cloned": today,
                "photos_cloned": photos,
                "videos_cloned": videos
            }

    # --- DATABASE BACKUP EXPORTER ---

    async def create_backup_file(self) -> Optional[str]:
        """Creates a timestamped snapshot backup of the database"""
        if not os.path.exists(self.db_path):
            return None

        backup_dir = "temp_media"
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"backup_cloner_{timestamp}.db")

        try:
            # Safely create hot backup in WAL mode using VACUUM INTO
            abs_backup_path = os.path.abspath(backup_path).replace("\\", "/")
            async with self.get_connection() as db:
                await db.execute(f"VACUUM INTO '{abs_backup_path}';")
            return backup_path
        except Exception as e:
            logger.warning(f"VACUUM INTO failed ({e}), falling back to WAL checkpoint + copy...")
            try:
                async with self.get_connection() as db:
                    await db.execute("PRAGMA wal_checkpoint(FULL);")
                shutil.copy2(self.db_path, backup_path)
                return backup_path
            except Exception as e2:
                logger.error(f"Backup creation failed: {e2}")
                return None

    # --- STATS ---

    async def get_stats(self) -> Dict[str, Any]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            u_cur = await db.execute("SELECT COUNT(*) as cnt FROM users")
            total_users = (await u_cur.fetchone())["cnt"]

            p_cur = await db.execute("SELECT COUNT(*) as cnt FROM channel_pairs")
            total_pairs = (await p_cur.fetchone())["cnt"]

            ap_cur = await db.execute("SELECT COUNT(*) as cnt FROM channel_pairs WHERE is_active = 1")
            active_pairs = (await ap_cur.fetchone())["cnt"]

            m_cur = await db.execute("SELECT COUNT(*) as cnt FROM cloned_messages")
            total_cloned = (await m_cur.fetchone())["cnt"]

            pay_cur = await db.execute("SELECT SUM(amount) as total_stars FROM payments")
            stars_row = await pay_cur.fetchone()
            total_stars = stars_row["total_stars"] or 0

            return {
                "total_users": total_users,
                "total_pairs": total_pairs,
                "active_pairs": active_pairs,
                "total_cloned_messages": total_cloned,
                "total_stars_earned": total_stars
            }

    async def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Returns isolated personal channel and clone statistics for a specific user"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            p_cur = await db.execute(
                "SELECT COUNT(*) as total, SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) as active FROM channel_pairs WHERE user_id = ?",
                (user_id,)
            )
            p_row = await p_cur.fetchone()
            total_pairs = p_row["total"] or 0
            active_pairs = p_row["active"] or 0

            m_cur = await db.execute("""
                SELECT COUNT(*) as total
                FROM cloned_messages m
                JOIN channel_pairs p ON m.pair_id = p.id
                WHERE p.user_id = ?
            """, (user_id,))
            m_row = await m_cur.fetchone()
            total_cloned = m_row["total"] or 0

            today_cur = await db.execute("""
                SELECT COUNT(*) as today
                FROM cloned_messages m
                JOIN channel_pairs p ON m.pair_id = p.id
                WHERE p.user_id = ? AND m.cloned_at >= date('now', 'start of day')
            """, (user_id,))
            today_row = await today_cur.fetchone()
            today_cloned = today_row["today"] or 0

            sub = await self.get_user_subscription(user_id)

            return {
                "total_pairs": total_pairs,
                "active_pairs": active_pairs,
                "total_cloned": total_cloned,
                "today_cloned": today_cloned,
                "subscription": sub
            }

    async def toggle_premium_emojis(self, pair_id: int) -> Optional[bool]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT auto_premium_emojis FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["auto_premium_emojis"] else 1
            await db.execute("UPDATE channel_pairs SET auto_premium_emojis = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            return bool(new_status)

    async def update_video_watermark_settings(self, pair_id: int, wm_type: str, text: str, pos: str = "bottom_right"):
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE channel_pairs SET video_watermark_type = ?, video_watermark_text = ?, video_watermark_pos = ? WHERE id = ?",
                (wm_type, text, pos, pair_id)
            )
            await db.commit()

    async def update_drip_settings(self, pair_id: int, delay_minutes: int, night_mode: str = "off"):
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE channel_pairs SET drip_delay_minutes = ?, night_mode = ? WHERE id = ?",
                (delay_minutes, night_mode, pair_id)
            )
            await db.commit()

    async def update_ai_paraphrase_settings(self, pair_id: int, mode: str = "off"):
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE channel_pairs SET ai_paraphrase_mode = ? WHERE id = ?",
                (mode, pair_id)
            )
            await db.commit()

    async def toggle_auto_cta_buttons(self, pair_id: int) -> Optional[bool]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT auto_cta_buttons FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["auto_cta_buttons"] else 1
            await db.execute("UPDATE channel_pairs SET auto_cta_buttons = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            return bool(new_status)

    async def toggle_backup_enabled(self, pair_id: int) -> Optional[bool]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT backup_enabled FROM channel_pairs WHERE id = ?", (pair_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row["backup_enabled"] else 1
            await db.execute("UPDATE channel_pairs SET backup_enabled = ? WHERE id = ?", (new_status, pair_id))
            await db.commit()
            return bool(new_status)

    # --- DRIP FEED QUEUE ---

    async def add_drip_queue_item(self, pair_id: int, msg_data_json: str, scheduled_at: str) -> int:
        async with self.get_connection() as db:
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
                WHERE status = 'pending' AND scheduled_at <= datetime('now')
                ORDER BY scheduled_at ASC
                LIMIT 50
            """)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def mark_drip_item_done(self, item_id: int, status: str = "sent"):
        async with self.get_connection() as db:
            await db.execute("UPDATE drip_queue SET status = ? WHERE id = ?", (status, item_id))
            await db.commit()

    # --- CHANNEL BACKUPS & DISASTER RECOVERY ---

    async def save_channel_backup(
        self,
        pair_id: int,
        source_id: Optional[int],
        message_id: int,
        text: str = "",
        media_type: str = "none",
        media_file_id: Optional[str] = None,
        entities_json: str = ""
    ):
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO channel_backups (pair_id, source_id, message_id, text, media_type, media_file_id, entities_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (pair_id, source_id, message_id, text, media_type, media_file_id, entities_json))
            await db.commit()

    async def get_channel_backups(self, pair_id: int, limit: int = 500) -> List[Dict[str, Any]]:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM channel_backups WHERE pair_id = ? ORDER BY message_id ASC LIMIT ?",
                (pair_id, limit)
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
            auto_cta_buttons=bool(row["auto_cta_buttons"]) if "auto_cta_buttons" in keys else False,
            backup_enabled=bool(row["backup_enabled"]) if "backup_enabled" in keys else True,
            created_at=row["created_at"]
        )

db_manager = DatabaseManager()
