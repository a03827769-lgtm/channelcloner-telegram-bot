import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Set
from database.db_manager import db_manager
from database.models import ChannelPair

logger = logging.getLogger(__name__)

# Standard Tashkent Timezone (UTC+5)
UZB_TZ = timezone(timedelta(hours=5))

# Night window of the "buffer" night mode (Tashkent time)
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 8
# A post is never scheduled further ahead than this; a larger backlog is compacted at the horizon (the rate limiter
# still spaces the actual sends), so queued media never waits for days.
MAX_SCHEDULE_AHEAD = timedelta(hours=24)
MAX_DISPATCH_RETRIES = 3


class DripFeedQueueService:
    """
    Intelligent Drip Feed & Night Buffer Manager.
    Paces post distribution to prevent channel audience spam and respects silent night hours.
    """

    def get_current_time(self) -> datetime:
        """Returns current timezone-aware Uzbekistan datetime"""
        return datetime.now(UZB_TZ)

    def is_night_time(self, start_hour: int = NIGHT_START_HOUR, end_hour: int = NIGHT_END_HOUR) -> bool:
        """Returns True if current UTC+5 (Tashkent time) is in night window"""
        return self._is_night_hour(self.get_current_time().hour, start_hour, end_hour)

    @staticmethod
    def _is_night_hour(hour: int, start_hour: int = NIGHT_START_HOUR, end_hour: int = NIGHT_END_HOUR) -> bool:
        if start_hour > end_hour:
            return hour >= start_hour or hour < end_hour
        return start_hour <= hour < end_hour

    def _clamp_out_of_night(self, target_time: datetime) -> datetime:
        """Moves a delivery time that falls into the night window to the next 08:00 (Tashkent time)."""
        local = target_time.astimezone(UZB_TZ)
        if not self._is_night_hour(local.hour):
            return target_time
        morning = local.replace(hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0)
        if morning <= local:
            morning += timedelta(days=1)
        return morning

    async def calculate_scheduled_time(self, pair: ChannelPair) -> datetime:
        """Calculates next delivery datetime based on drip delay and night mode, staggering posts to prevent avalanches"""
        now = self.get_current_time()
        drip_delay_min = max(0, pair.drip_delay_minutes)
        delay = timedelta(minutes=drip_delay_min)
        target_time = now + delay

        night_buffer = pair.night_mode == "buffer"
        if night_buffer and self.is_night_time():
            # Schedule for next morning 08:00 Tashkent time
            target_time = now.replace(hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0)
            if target_time <= now:
                target_time += timedelta(days=1)

        # Check if there are already pending posts in queue to prevent avalanche
        latest_utc = await db_manager.get_latest_scheduled_drip_time(pair.id)
        if latest_utc:
            latest_uzb = latest_utc.astimezone(UZB_TZ)
            stagger_interval = timedelta(minutes=max(1, drip_delay_min))
            if latest_uzb >= target_time:
                target_time = latest_uzb + stagger_interval

        # Cap the backlog: never further ahead than MAX_SCHEDULE_AHEAD
        horizon = now + MAX_SCHEDULE_AHEAD
        if target_time > horizon:
            target_time = horizon

        # The night buffer applies to the delivery time, not only to "now"
        if night_buffer:
            target_time = self._clamp_out_of_night(target_time)

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
    def payload_media_paths(payload: Optional[Dict[str, Any]]) -> List[str]:
        """Local media files referenced by a queued payload."""
        if not isinstance(payload, dict):
            return []
        paths: List[str] = []
        for key in ("media_path", "media_file_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
        for mf in payload.get("media_files", []) or []:
            if isinstance(mf, dict) and isinstance(mf.get("path"), str) and mf.get("path"):
                paths.append(mf["path"])
        return paths

    async def get_pending_media_paths(self) -> Set[str]:
        """Media files referenced by pending queue rows; the temp cleaner must keep them until the row is done."""
        paths: Set[str] = set()
        async with db_manager.get_connection() as db:
            cursor = await db.execute("SELECT msg_data_json FROM drip_queue WHERE status = 'pending'")
            rows = await cursor.fetchall()
        for row in rows:
            try:
                payload = json.loads(row[0])
            except Exception:
                continue
            paths.update(self.payload_media_paths(payload))
        return paths

    @classmethod
    def _cleanup_payload_files(cls, payload: Optional[Dict[str, Any]]):
        """Removes any temporary media files referenced in the queued payload"""
        for m_path in cls.payload_media_paths(payload):
            if os.path.isfile(m_path):
                try:
                    os.remove(m_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

    @staticmethod
    async def _release_undelivered(pair_id: int, payload: Optional[Dict[str, Any]]):
        """The queued source messages were never published: drop their "queued" records so catch-up / history
        cloning can pick them up again. Messages already delivered by an earlier step are kept."""
        if not isinstance(payload, dict):
            return
        source_ids = [int(m) for m in (payload.get("source_msg_ids") or []) if str(m).lstrip("-").isdigit()]
        progress = payload.get("progress") or {}
        if not source_ids or progress.get("delivered_target_id"):
            return
        try:
            await db_manager.forget_cloned_messages(pair_id, source_ids)
        except Exception:
            logger.debug("Could not release undelivered drip records", exc_info=True)

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
                            await self._release_undelivered(pair_id, payload)
                            logger.info(f"Drip item #{item_id} skipped: pair #{pair_id} inactive or not found")
                    except asyncio.CancelledError:
                        raise
                    except FileNotFoundError as missing:
                        # The queued media is gone and there is no text to publish: retrying cannot help
                        logger.error(f"Drip item #{item_id} cannot be delivered: {missing}")
                        await db_manager.mark_drip_item_done(item_id, "failed", error_message=str(missing)[:500])
                        self._cleanup_payload_files(payload)
                        await self._release_undelivered(pair_id, payload)
                    except Exception as e:
                        retries = (payload.get("retry_count", 0) if isinstance(payload, dict) else 0) + 1
                        if retries <= MAX_DISPATCH_RETRIES and isinstance(payload, dict):
                            # The engine records finished steps in payload["progress"], so a retry resumes after them
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
                            await self._release_undelivered(pair_id, payload)
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
