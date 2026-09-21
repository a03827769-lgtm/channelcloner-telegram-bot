import asyncio
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple

from database.db_manager import db_manager
from database.models import StorySettings, StoryQueueItem
from services.custom_emojis import STAR_SPARKLE, TROPHY, CHANNEL, MONEY, LOCATION, TIMER, LINK

logger = logging.getLogger(__name__)


# Standard Tashkent Timezone (UTC+5)
UZB_TZ = timezone(timedelta(hours=5))


class StoryQueueService:
    """
    Intelligent Story Drip-Feed Queue & Prime Hours Scheduler.
    Paces story distribution during viewer prime hours (09:00 to 22:00 Tashkent time),
    enforces cooldown intervals between stories (default 45 min), and orders publications
    by listing quality score (0-100) so the best apartments appear first.
    """

    def __init__(self):
        self._worker_task: Optional[asyncio.Task] = None
        self._is_running: bool = False

    @staticmethod
    def get_uzb_now() -> datetime:
        """Returns current timezone-aware Tashkent (UTC+5) datetime"""
        return datetime.now(UZB_TZ)

    @staticmethod
    def is_in_prime_hours(start_hour: Any = 9, end_hour: Any = 22, dt: Optional[datetime] = None) -> bool:
        """
        Returns True if dt (default: now) is within daytime viewer prime hours (e.g. 09:00 - 22:00).
        """
        now = dt or datetime.now(UZB_TZ)
        hour = now.hour
        try:
            sh = int(str(start_hour).split(":")[0])
        except (ValueError, TypeError):
            sh = 9
        try:
            eh = int(str(end_hour).split(":")[0])
        except (ValueError, TypeError):
            eh = 22
        return sh <= hour < eh

    async def calculate_scheduled_time(self, settings: StorySettings) -> datetime:
        """
        Calculates the earliest valid UTC datetime to publish the next story for this user.
        Takes into account:
        1. Prime hours window (09:00 - 22:00 Tashkent time)
        2. Cooldown since last published story (default 45 mins)
        3. Already pending items in queue (staggered spacing)
        """
        now_uzb = self.get_uzb_now()
        cooldown_min = max(15, settings.drip_delay_minutes)
        cooldown = timedelta(minutes=cooldown_min)

        target_uzb = now_uzb

        # 1. Check last posted story time
        last_posted_utc = await db_manager.get_last_posted_story_time(settings.user_id)
        if last_posted_utc:
            last_posted_uzb = last_posted_utc.astimezone(UZB_TZ)
            if now_uzb < last_posted_uzb + cooldown:
                target_uzb = last_posted_uzb + cooldown

        # 2. Check pending queue items for user
        user_pending = await db_manager.get_user_story_queue(settings.user_id)
        if user_pending:
            latest_queued_str = max(q.scheduled_at for q in user_pending)
            try:
                latest_utc = datetime.fromisoformat(latest_queued_str.replace(' ', 'T')).replace(tzinfo=timezone.utc)
                latest_uzb = latest_utc.astimezone(UZB_TZ)
                if latest_uzb + cooldown > target_uzb:
                    target_uzb = latest_uzb + cooldown
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        # 3. Check Prime Hours constraint
        if settings.prime_hours_enabled:
            try:
                sh = int(str(settings.prime_hours_start).split(":")[0])
            except (ValueError, TypeError):
                sh = 9
            try:
                eh = int(str(settings.prime_hours_end).split(":")[0])
            except (ValueError, TypeError):
                eh = 22

            if not self.is_in_prime_hours(sh, eh, target_uzb):
                # If late night (>= eh): push to next morning at sh (e.g. 09:00)
                if target_uzb.hour >= eh:
                    target_uzb = (target_uzb + timedelta(days=1)).replace(hour=sh, minute=0, second=0, microsecond=0)
                # If early morning (< sh): push to today at sh (e.g. 09:00)
                elif target_uzb.hour < sh:
                    target_uzb = target_uzb.replace(hour=sh, minute=0, second=0, microsecond=0)

        return target_uzb.astimezone(timezone.utc)

    async def enqueue_listing(
        self,
        user_id: int,
        source_channel: str,
        source_msg_id: int,
        payload: Dict[str, Any],
        price: Optional[float] = None,
        district: str = "",
        rooms: Optional[int] = None,
        area: Optional[float] = None,
        score: int = 0
    ) -> Tuple[int, str]:
        """
        Calculates optimal scheduled time and stores listing into the persistent queue.
        Returns: (queue_item_id, scheduled_at_iso_str)
        """
        if not await db_manager.is_vip(user_id):
            logger.warning(f"Rejecting story enqueue for user {user_id}: VIP subscription not active")
            raise PermissionError(f"User {user_id} does not have an active VIP subscription")

        st = await db_manager.get_story_settings(user_id)
        sched_utc = await self.calculate_scheduled_time(st)
        sched_str = sched_utc.strftime("%Y-%m-%d %H:%M:%S")

        item_id = await db_manager.enqueue_story(
            user_id=user_id,
            source_channel=source_channel,
            source_msg_id=source_msg_id,
            payload=payload,
            scheduled_at_utc=sched_str,
            price=price,
            district=district,
            rooms=rooms,
            area=area,
            score=score
        )
        logger.info(f"Enqueued story listing #{item_id} for user {user_id} (score: {score}) scheduled at {sched_str} UTC")
        return item_id, sched_str

    async def start_worker(self, story_cloner_service=None):
        """
        Background daemon checking the queue every 30 seconds and dispatching due stories.
        """
        if story_cloner_service is None:
            from services.story_cloner_service import story_cloner_service as default_scs
            story_cloner_service = default_scs

        if self._is_running:
            return
        self._is_running = True
        self._worker_task = asyncio.current_task()
        logger.info("Story Drip-Feed Queue Worker started.")

        try:
            interval = 30.0
            next_tick = time.monotonic() + interval
            while self._is_running:
                try:
                    now = time.monotonic()
                    sleep_time = max(1.0, next_tick - now)
                    await asyncio.sleep(sleep_time)
                    next_tick = max(time.monotonic(), next_tick + interval)

                    due_items = await db_manager.get_due_story_queue_items()
                    for item in due_items:
                        await self._process_due_item(item, story_cloner_service)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in story queue worker loop: {e}", exc_info=True)
                    next_tick = time.monotonic() + 10.0
                    await asyncio.sleep(10.0)
        finally:
            self._is_running = False
            self._worker_task = None

    async def _process_due_item(self, item: StoryQueueItem, story_cloner_service=None):
        """Processes a single due queue item"""
        if story_cloner_service is None:
            from services.story_cloner_service import story_cloner_service as default_scs
            story_cloner_service = default_scs
        # 0. Strict VIP restriction
        if not await db_manager.is_vip(item.user_id):
            logger.warning(f"Skipping queue items for user {item.user_id}: VIP subscription not active")
            await db_manager.mark_story_queue_item_done(item.id, "skipped", "VIP subscription required or expired")
            try:
                await db_manager.execute(
                    "UPDATE story_queue SET status = 'skipped', error_message = 'VIP subscription required or expired' WHERE user_id = ? AND status = 'pending'",
                    (item.user_id,)
                )
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            story_cloner_service.stop_monitor_for_user(item.user_id)
            return

        st = await db_manager.get_story_settings(item.user_id)
        if not st.is_active:
            await db_manager.mark_story_queue_item_done(item.id, "skipped", "Auto-monitoring turned off")
            return

        # Check daily limit
        today_count = await db_manager.get_today_posted_story_count(item.user_id)
        if today_count >= st.max_stories_per_day:
            # Postpone to next morning
            now_uzb = self.get_uzb_now()
            try:
                sh = int(str(st.prime_hours_start).split(":")[0])
            except (ValueError, TypeError):
                sh = 9
            next_morning = (now_uzb + timedelta(days=1)).replace(hour=sh, minute=0, second=0, microsecond=0)
            next_utc = next_morning.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            await db_manager.execute(
                "UPDATE story_queue SET scheduled_at = ? WHERE id = ?",
                (next_utc, item.id)
            )
            logger.info(f"Queue item #{item.id} postponed to {next_utc} (daily limit {st.max_stories_per_day} reached)")
            return

        # Parse payload
        try:
            payload = json.loads(item.payload_json) if item.payload_json else {}
        except Exception as err:
            await db_manager.mark_story_queue_item_done(item.id, "failed", f"Corrupt payload: {err}")
            return

        client = await story_cloner_service.get_client_for_user(item.user_id)
        if not client or not client.is_connected():
            retries = payload.get("client_retries", 0) + 1
            if retries >= 10:
                logger.warning(f"User {item.user_id} client permanently disconnected for #{item.id}; marking failed.")
                await db_manager.mark_story_queue_item_done(item.id, "failed", "Telethon client not connected after 10 attempts")
                return

            payload["client_retries"] = retries
            logger.warning(f"User {item.user_id} Telethon client not connected for queue #{item.id} (attempt {retries}/10); postponing 5 minutes.")
            next_retry = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            await db_manager.execute(
                "UPDATE story_queue SET scheduled_at = ?, payload_json = ? WHERE id = ?",
                (next_retry, json.dumps(payload), item.id)
            )
            return

        # Publish story
        try:
            target_ch = item.source_channel
            if not str(target_ch).startswith("@") and not str(target_ch).startswith("-100") and not str(target_ch).lstrip("-").isdigit():
                if payload.get("channel_id"):
                    target_ch = payload.get("channel_id")
                elif payload.get("source_id"):
                    target_ch = payload.get("source_id")

            logger.info(f"Publishing queued story #{item.id} (score {item.score}) for user {item.user_id} via channel {target_ch}...")
            ok, story_id, res_msg, story_url = await story_cloner_service.post_story_from_channel(
                client=client,
                channel_identifier=target_ch,
                msg_id=item.source_msg_id,
                bg_style=st.background_style,
                source_photo_path=payload.get("source_photo_path"),
                badges=payload.get("badges"),
                user_id=item.user_id,
                pinned=st.pin_to_profile
            )
            if ok:
                await db_manager.mark_story_queue_item_done(item.id, "sent")
                await db_manager.record_posted_story(
                    user_id=item.user_id,
                    source_channel=item.source_channel,
                    source_id=payload.get("source_id"),
                    source_msg_id=item.source_msg_id,
                    story_id=story_id,
                    price=item.price,
                    caption_snippet=payload.get("caption_snippet", ""),
                    target_type=st.target_type,
                    status="success"
                )

                # Record fingerprint for deduplication
                fp = payload.get("fingerprint")
                if fp:
                    await db_manager.record_listing_hash(
                        user_id=item.user_id,
                        listing_hash=fp,
                        source_channel=item.source_channel,
                        source_msg_id=item.source_msg_id
                    )

                # Send notification
                if story_cloner_service._bot_instance:
                    try:
                        price_str = f"${item.price:g}" if item.price else "Aniqlangan"
                        story_link_html = f'\n{LINK} <a href="{story_url}">Istoriyani ochish</a>' if story_url else ''
                        text = f"""
{STAR_SPARKLE} <b>Sara variant navbatdan Istoriyaga joylandi!</b>

├ {TROPHY} <b>Sifat bali:</b> <b>{item.score}/100</b>
├ {CHANNEL} <b>Manba kanal:</b> {item.source_channel}
├ {MONEY} <b>Narxi:</b> <b>{price_str}</b>
├ {LOCATION} <b>Hudud:</b> {item.district or "Aniqlanmadi"}
└ {TIMER} <b>Vaqt:</b> Prime Time (Optimal ko'rish soati){story_link_html}
"""

                        await story_cloner_service._bot_instance.send_message(
                            chat_id=item.user_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True
                        )
                    except Exception as ne:
                        logger.warning(f"Notification error: {ne}")
            else:
                retries = payload.get("retries", 0)
                if retries < 3:
                    payload["retries"] = retries + 1
                    delay_sec = 120 * (retries + 1)
                    new_scheduled_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_sec)).strftime("%Y-%m-%d %H:%M:%S")
                    logger.warning(
                        f"Queue item #{item.id} post failed (attempt {retries + 1}/3): {res_msg}. "
                        f"Rescheduling in {delay_sec}s at {new_scheduled_at}."
                    )
                    async with db_manager.write_transaction() as db:
                        await db.execute(
                            "UPDATE story_queue SET payload_json = ?, scheduled_at = ?, status = 'pending', error_message = ? WHERE id = ?",
                            (json.dumps(payload, ensure_ascii=False), new_scheduled_at, f"Retry #{retries + 1}: {str(res_msg)[:400]}", item.id)
                        )
                else:
                    logger.error(f"Queue item #{item.id} post permanently failed after 3 attempts: {res_msg}")
                    await db_manager.mark_story_queue_item_done(item.id, "failed", res_msg)

        except Exception as e:
            err_str = str(e)
            logger.error(f"Error publishing queue item #{item.id}: {err_str}", exc_info=True)
            try:
                payload = json.loads(item.payload_json) if item.payload_json else {}
                retries = payload.get("retries", 0)
                if retries < 3:
                    payload["retries"] = retries + 1
                    delay_sec = 120 * (retries + 1)
                    new_scheduled_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_sec)).strftime("%Y-%m-%d %H:%M:%S")
                    logger.warning(
                        f"Queue item #{item.id} exception (attempt {retries + 1}/3): {err_str}. "
                        f"Rescheduling in {delay_sec}s at {new_scheduled_at}."
                    )
                    async with db_manager.write_transaction() as db:
                        await db.execute(
                            "UPDATE story_queue SET payload_json = ?, scheduled_at = ?, status = 'pending', error_message = ? WHERE id = ?",
                            (json.dumps(payload, ensure_ascii=False), new_scheduled_at, f"Retry #{retries + 1}: {err_str[:400]}", item.id)
                        )
                    return
            except Exception as retry_err:
                logger.error(f"Failed to reschedule queue item #{item.id}: {retry_err}")
            await db_manager.mark_story_queue_item_done(item.id, "failed", err_str)

    def stop_worker(self):
        """Stops the queue worker"""
        self._is_running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()


story_queue_service = StoryQueueService()
