import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from database.db_manager import db_manager
from database.models import ChannelPair

logger = logging.getLogger(__name__)

# Standard Tashkent Timezone (UTC+5)
UZB_TZ = timezone(timedelta(hours=5))

class DripFeedQueueService:
    """
    Intelligent Drip Feed & Night Buffer Manager.
    Paces post distribution to prevent channel audience spam and respects silent night hours.
    """

    def get_current_time(self) -> datetime:
        """Returns current timezone-aware Uzbekistan datetime"""
        return datetime.now(UZB_TZ)

    def is_night_time(self, start_hour: int = 23, end_hour: int = 8) -> bool:
        """Returns True if current UTC+5 (Tashkent time) is in night window"""
        now = self.get_current_time()
        current_hour = now.hour
        if start_hour > end_hour:
            return current_hour >= start_hour or current_hour < end_hour
        return start_hour <= current_hour < end_hour

    async def calculate_scheduled_time(self, pair: ChannelPair) -> datetime:
        """Calculates next delivery datetime based on drip delay and night mode, staggering posts to prevent avalanches"""
        now = self.get_current_time()
        drip_delay_min = max(0, pair.drip_delay_minutes)
        delay = timedelta(minutes=drip_delay_min)
        target_time = now + delay

        if pair.night_mode == "buffer" and self.is_night_time():
            # Schedule for next morning 08:00 Tashkent time
            target_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if target_time <= now:
                target_time += timedelta(days=1)

        # Check if there are already pending posts in queue to prevent avalanche
        latest_utc = await db_manager.get_latest_scheduled_drip_time(pair.id)
        if latest_utc:
            latest_uzb = latest_utc.astimezone(UZB_TZ)
            stagger_interval = timedelta(minutes=max(1, drip_delay_min))
            if latest_uzb >= target_time:
                target_time = latest_uzb + stagger_interval

        return target_time

    async def enqueue_post(self, pair: ChannelPair, msg_payload: Dict[str, Any]) -> int:
        """Queues a message payload into the SQLite persistent queue in UTC format"""
        sched_uzb = await self.calculate_scheduled_time(pair)
        sched_utc = sched_uzb.astimezone(timezone.utc)
        scheduled_at = sched_utc.strftime("%Y-%m-%d %H:%M:%S")
        payload_json = json.dumps(msg_payload, ensure_ascii=False)
        item_id = await db_manager.add_drip_queue_item(pair.id, payload_json, scheduled_at)
        logger.info(f"Enqueued drip post #{item_id} for pair #{pair.id} at {scheduled_at} (UTC)")
        return item_id

    @staticmethod
    def _cleanup_payload_files(payload: Optional[Dict[str, Any]]):
        """Removes any temporary media files referenced in the queued payload"""
        if not isinstance(payload, dict):
            return
        m_path = payload.get("media_file_id")
        if isinstance(m_path, str) and os.path.exists(m_path):
            try:
                os.remove(m_path)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        for mf in payload.get("media_files", []):
            if isinstance(mf, dict):
                mf_path = mf.get("path")
                if isinstance(mf_path, str) and os.path.exists(mf_path):
                    try:
                        os.remove(mf_path)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)

    async def start_worker(self, bot_instance, cloner_engine):
        """Asynchronous background worker polling and dispatching due drip posts"""
        logger.info("Intelligent Drip Feed & Night Buffer Worker started.")
        while True:
            try:
                due_items = await db_manager.get_due_drip_items()
                for item in due_items:
                    item_id = item["id"]
                    pair_id = item["pair_id"]
                    payload = None
                    try:
                        try:
                            payload = json.loads(item["msg_data_json"])
                        except Exception as json_err:
                            logger.error(f"Drip item #{item_id} has corrupt JSON payload: {json_err}")
                            await db_manager.mark_drip_item_done(item_id, "failed", error_message=f"Corrupt JSON payload: {str(json_err)[:400]}")
                            continue

                        pair = await db_manager.get_pair_by_id(pair_id)
                        if pair and pair.is_active:
                            disable_notify = (pair.night_mode == "silent" and self.is_night_time())
                            await cloner_engine.dispatch_queued_payload(bot_instance, pair, payload, disable_notify)
                            await db_manager.mark_drip_item_done(item_id, "sent")
                        else:
                            # Pair deleted or deactivated — mark as skipped, not sent, and clean up temporary files
                            await db_manager.mark_drip_item_done(item_id, "skipped")
                            self._cleanup_payload_files(payload)
                            logger.info(f"Drip item #{item_id} skipped: pair #{pair_id} inactive or not found")
                    except Exception as e:
                        retries = (payload.get("retry_count", 0) if isinstance(payload, dict) else 0) + 1
                        if retries <= 3 and isinstance(payload, dict):
                            payload["retry_count"] = retries
                            logger.warning(f"Drip item #{item_id} transient error ({e}), rescheduling retry #{retries} in {30 * retries}s...")
                            new_sched = (datetime.now(timezone.utc) + timedelta(seconds=30 * retries)).strftime("%Y-%m-%d %H:%M:%S")
                            await db_manager.execute(
                                "UPDATE drip_queue SET msg_data_json = ?, scheduled_at = ?, error_message = ? WHERE id = ?",
                                (json.dumps(payload, ensure_ascii=False), new_sched, f"Retry #{retries}: {str(e)[:400]}", item_id)
                            )
                        else:
                            logger.error(f"Failed to dispatch drip item #{item_id} after {retries} attempts: {e}")
                            await db_manager.mark_drip_item_done(item_id, "failed", error_message=str(e)[:500])
                            self._cleanup_payload_files(payload)
            except asyncio.CancelledError:
                logger.info("Drip feed worker cancelled; shutting down cleanly.")
                break
            except Exception as e:
                logger.error(f"Error in drip feed worker loop: {e}")

            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break

drip_feed_service = DripFeedQueueService()
