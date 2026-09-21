import asyncio
import logging
import json
import traceback
import os
import aiomysql
from datetime import datetime

from database.db_manager import db_manager
from services.cloner_engine import cloner_engine

logger = logging.getLogger(__name__)

class CommandPoller:
    def __init__(self, bot):
        self.bot = bot
        self._running = False
        self._task = None
        self._db_pool = None

    async def _init_pool(self):
        if not self._db_pool:
            self._db_pool = await aiomysql.create_pool(
                host=os.getenv("MYSQL_HOST", "mysql"),
                port=int(os.getenv("MYSQL_PORT", 3306)),
                user=os.getenv("MYSQL_USER", "cloner_user"),
                password=os.getenv("MYSQL_PASSWORD", "cloner_pass_2026"),
                db=os.getenv("MYSQL_DATABASE", "channelcloner"),
                autocommit=True
            )

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("CommandPoller started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            logger.info("CommandPoller stopped")
        if self._db_pool:
            self._db_pool.close()

    async def _poll_loop(self):
        await self._init_pool()
        while self._running:
            try:
                await self._process_pending_commands()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in CommandPoller: {e}")
            
            # Poll every 2 seconds
            await asyncio.sleep(2)

    async def _process_pending_commands(self):
        try:
            async with self._db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT id, user_id, command, target_id, payload FROM bot_commands WHERE status = 'pending' ORDER BY id ASC")
                    commands = await cur.fetchall()
        except Exception as e:
            logger.error(f"Error reading commands from MySQL: {e}")
            return

        for cmd in commands:
            cmd_id = cmd[0]
            user_id = cmd[1]
            command = cmd[2]
            target_id = cmd[3]
            payload_str = cmd[4]

            logger.info(f"Processing bot command #{cmd_id}: {command} for user {user_id}")
            
            async with self._db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("UPDATE bot_commands SET status = 'processing' WHERE id = %s", (cmd_id,))

            try:
                payload = json.loads(payload_str) if payload_str else {}
                
                if command == "test_post":
                    await self._handle_test_post(user_id, target_id, payload)
                elif command == "backfill":
                    await self._handle_backfill(user_id, target_id, payload)
                else:
                    logger.warning(f"Unknown command {command}")

                async with self._db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("UPDATE bot_commands SET status = 'completed' WHERE id = %s", (cmd_id,))

            except Exception as e:
                logger.error(f"Error processing command #{cmd_id}: {e}\n{traceback.format_exc()}")
                async with self._db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("UPDATE bot_commands SET status = 'failed', error_msg = %s WHERE id = %s", (str(e), cmd_id))

    async def _resolve_pair(self, pair_id: int):
        pair = await db_manager.get_pair_by_id(pair_id)
        if pair:
            return pair
        # Fallback to MySQL channel_pairs
        try:
            async with self._db_pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM channel_pairs WHERE id = %s", (pair_id,))
                    row = await cur.fetchone()
                    if row:
                        from database.models import ChannelPair
                        return ChannelPair(
                            id=row.get('id'),
                            user_id=row.get('user_id', 0),
                            source_channel=str(row.get('source_channel', '')),
                            source_title=str(row.get('source_title') or row.get('source_channel', '')),
                            source_id=row.get('source_id'),
                            target_channel=str(row.get('target_channel', '')),
                            target_title=str(row.get('target_title') or row.get('target_channel', '')),
                            target_id=row.get('target_id'),
                            is_active=bool(row.get('is_active', 1)),
                            clean_links=bool(row.get('clean_links', 1)),
                            custom_signature=str(row.get('custom_signature') or ''),
                            clone_mode=str(row.get('clone_mode') or 'clean')
                        )
        except Exception as e:
            logger.warning(f"Error querying MySQL for pair {pair_id}: {e}")
        return None

    async def _handle_test_post(self, user_id: int, pair_id: int, payload: dict):
        if not pair_id:
            pair_id = payload.get("pair_id")
        if not pair_id:
            raise ValueError("Missing pair_id for test_post")
            
        pair = await self._resolve_pair(pair_id)
        if not pair:
            raise ValueError(f"Pair {pair_id} not found in SQLite or MySQL")
            
        # Send a test post using the bot
        try:
            test_message = f"🧪 <b>Test post</b> for pair #{pair.id}\nSource: {pair.source_channel}\nTarget: {pair.target_channel}"
            await self.bot.send_message(
                chat_id=pair.target_channel,
                text=test_message,
                parse_mode="HTML"
            )
        except Exception as e:
            raise Exception(f"Failed to send test post to target channel {pair.target_channel}: {e}")

    async def _handle_backfill(self, user_id: int, pair_id: int, payload: dict):
        if not pair_id:
            pair_id = payload.get("pair_id")
        limit = payload.get("limit", 10)
        
        pair = await self._resolve_pair(pair_id)
        if not pair:
            raise ValueError(f"Pair {pair_id} not found in SQLite or MySQL")
            
        # Perform backfill
        try:
            from services.telethon_listener import telethon_listener
            if not telethon_listener.is_connected():
                raise Exception("Telethon is not connected, cannot backfill.")
                
            client = telethon_listener.client
            messages = await client.get_messages(pair.source_channel, limit=limit)
            
            cloned_count = 0
            for msg in reversed(messages):
                success = await cloner_engine.process_and_clone_message(msg, pair)
                if success:
                    cloned_count += 1
                    
            logger.info(f"Backfilled {cloned_count}/{limit} messages for pair #{pair.id}")
            
            # Send notification to user
            await self.bot.send_message(
                chat_id=user_id,
                text=f"✅ Tarixiy xabarlarni yuklash yakunlandi.\nJami {limit} ta xabardan {cloned_count} tasi kanalga muvaffaqiyatli ko'chirildi."
            )
        except Exception as e:
            await self.bot.send_message(
                chat_id=user_id,
                text=f"❌ Tarixiy xabarlarni yuklashda xatolik yuz berdi: {e}"
            )
            raise
