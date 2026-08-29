import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from database.db_manager import db_manager
from database.models import ChannelPair

logger = logging.getLogger(__name__)

class DripFeedQueueService:
    """
    Intelligent Drip Feed & Night Buffer Manager.
    Paces post distribution to prevent channel audience spam and respects silent night hours.
    """

    def is_night_time(self, start_hour: int = 23, end_hour: int = 8) -> bool:
        """Returns True if current UTC+5 (or server time) is in night window"""
        now = datetime.now()
        current_hour = now.hour
        if start_hour > end_hour:
            return current_hour >= start_hour or current_hour < end_hour
        return start_hour <= current_hour < end_hour

    def calculate_scheduled_time(self, pair: ChannelPair) -> datetime:
        """Calculates next delivery datetime based on drip delay and night mode"""
        now = datetime.now()
        delay = timedelta(minutes=max(0, pair.drip_delay_minutes))
        target_time = now + delay

        if pair.night_mode == "buffer" and self.is_night_time():
            # Schedule for next morning 08:00
            target_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
            # BUG #10 fix: if 08:00 today is already past (e.g., it's 00:30 and today's 08:00 hasn't passed yet)
            # Actually if current time is in night window (23:00-08:00), 08:00 TODAY might be in the past
            # So always add 1 day if current target_time <= now
            if target_time <= now:
                target_time += timedelta(days=1)

        return target_time

    async def enqueue_post(self, pair: ChannelPair, msg_payload: Dict[str, Any]) -> int:
        """Queues a message payload into the SQLite persistent queue"""
        scheduled_at = self.calculate_scheduled_time(pair).strftime("%Y-%m-%d %H:%M:%S")
        payload_json = json.dumps(msg_payload, ensure_ascii=False)
        item_id = await db_manager.add_drip_queue_item(pair.id, payload_json, scheduled_at)
        logger.info(f"Enqueued drip post #{item_id} for pair #{pair.id} at {scheduled_at}")
        return item_id

    async def start_worker(self, bot_instance, cloner_engine):
        """Asynchronous background worker polling and dispatching due drip posts"""
        logger.info("Intelligent Drip Feed & Night Buffer Worker started.")
        while True:
            try:
                due_items = await db_manager.get_due_drip_items()
                for item in due_items:
                    item_id = item["id"]
                    pair_id = item["pair_id"]
                    try:
                        payload = json.loads(item["msg_data_json"])
                        pair = await db_manager.get_pair_by_id(pair_id)
                        if pair and pair.is_active:
                            disable_notify = (pair.night_mode == "silent" and self.is_night_time())
                            await cloner_engine.dispatch_queued_payload(bot_instance, pair, payload, disable_notify)
                        await db_manager.mark_drip_item_done(item_id, "sent")
                    except Exception as e:
                        logger.error(f"Failed to dispatch drip item #{item_id}: {e}")
                        await db_manager.mark_drip_item_done(item_id, "failed")
            except Exception as e:
                logger.error(f"Error in drip feed worker loop: {e}")

            await asyncio.sleep(10)

drip_feed_service = DripFeedQueueService()
