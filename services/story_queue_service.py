import asyncio
import html
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple

from config.limits import STORY_MAX_PER_DAY_MIN, STORY_MAX_PER_DAY_MAX, STORY_DRIP_DELAY_MAX_MINUTES
from database.db_manager import db_manager
from database.models import StorySettings, StoryQueueItem
from services.custom_emojis import STAR_SPARKLE, TROPHY, CHANNEL, MONEY, LOCATION, TIMER, LINK
from services.listing_analyzer import format_price_usd

logger = logging.getLogger(__name__)


# Standard Tashkent Timezone (UTC+5, no DST)
UZB_TZ = timezone(timedelta(hours=5))
_DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Accounts without Telegram Premium can keep at most this many active stories
NON_PREMIUM_DAILY_STORY_LIMIT = 3
# A 'processing' claim older than this belongs to a crashed / killed worker
PROCESSING_STALE_MINUTES = 30
TRANSIENT_MAX_RETRIES = 3
CLIENT_MAX_RETRIES = 10
# STORIES_TOO_MUCH / FloodWait deferrals before the listing is dropped
DEFERRAL_MAX_ATTEMPTS = 7
STORIES_LIMIT_RETRY_SECONDS = 24 * 3600
MIN_QUEUE_SPACING_MINUTES = 15


def parse_hour(value: Any, default: int) -> int:
    """'9', 9, '09:00' -> 9; anything else -> default"""
    try:
        hour = int(str(value).split(":")[0])
    except (ValueError, TypeError):
        return default
    return hour if 0 <= hour <= 23 else default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_db_time(value: Any) -> Optional[datetime]:
    """Parses a stored UTC 'YYYY-MM-DD HH:MM:SS' timestamp"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(' ', 'T')).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _db_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_DB_TIME_FORMAT)


def channel_key_candidates(source_channel: Optional[str] = None, source_id: Any = None) -> Tuple[str, ...]:
    """Every form a queue / posted_stories row may have stored for one channel (@name, -100<id>, <id>)"""
    keys = []
    if source_channel:
        keys.append(str(source_channel))
    raw_id = db_manager.normalize_peer_id(source_id) if source_id is not None else None
    if raw_id is None and source_channel:
        raw_id = db_manager.normalize_peer_id(source_channel)
    if raw_id is not None:
        keys.extend([f"-100{raw_id}", str(raw_id)])
    return tuple(dict.fromkeys(keys))


class StoryQueueService:
    """
    Intelligent Story Drip-Feed Queue & Prime Hours Scheduler.
    Paces story distribution during viewer prime hours (default 09:00 to 22:00 Tashkent time, windows crossing
    midnight such as 20:00-02:00 are supported), enforces cooldown intervals between stories (default 45 min) and the
    daily limit per Tashkent calendar day, and orders publications by listing quality score (0-100).
    Items are claimed ('processing') before publishing; claims left by a crashed worker are released after
    PROCESSING_STALE_MINUTES.
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
        Returns True if dt (default: now, Tashkent) is inside the prime window [start, end).
        start > end means the window crosses midnight (e.g. 20 -> 02); start == end means all day.
        """
        now = dt or datetime.now(UZB_TZ)
        if now.tzinfo is not None:
            now = now.astimezone(UZB_TZ)
        hour = now.hour
        sh = parse_hour(start_hour, 9)
        eh = parse_hour(end_hour, 22)
        if sh == eh:
            return True
        if sh < eh:
            return sh <= hour < eh
        return hour >= sh or hour < eh

    @staticmethod
    def next_prime_start(dt: datetime, start_hour: Any = 9) -> datetime:
        """Earliest datetime >= dt at the prime window start hour (Tashkent)"""
        local = dt.astimezone(UZB_TZ)
        sh = parse_hour(start_hour, 9)
        candidate = local.replace(hour=sh, minute=0, second=0, microsecond=0)
        if candidate < local:
            candidate += timedelta(days=1)
        return candidate

    def tashkent_day_start(self, now_uzb: Optional[datetime] = None) -> datetime:
        """Today's 00:00 in Tashkent (timezone-aware)"""
        now_uzb = (now_uzb or self.get_uzb_now()).astimezone(UZB_TZ)
        return now_uzb.replace(hour=0, minute=0, second=0, microsecond=0)

    async def count_posted_today(self, user_id: int, now_uzb: Optional[datetime] = None) -> int:
        """Stories successfully posted since Tashkent midnight (the daily limit is per Tashkent day, not UTC day)"""
        day_start_utc = _db_time(self.tashkent_day_start(now_uzb))
        async with db_manager.get_connection() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM posted_stories WHERE user_id = ? AND status = 'success' AND posted_at >= ?",
                (user_id, day_start_utc)
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row and row[0] else 0

    async def is_queued(
        self,
        user_id: int,
        source_msg_id: int,
        source_channel: Optional[str] = None,
        source_id: Any = None
    ) -> bool:
        """True when this channel post already waits in the queue (pending or being published)"""
        keys = channel_key_candidates(source_channel, source_id)
        if not keys:
            return False
        placeholders = ",".join("?" for _ in keys)
        async with db_manager.get_connection() as db:
            async with db.execute(
                f"SELECT 1 FROM story_queue WHERE user_id = ? AND source_msg_id = ? "
                f"AND status IN ('pending', 'processing') AND source_channel IN ({placeholders}) LIMIT 1",
                (user_id, source_msg_id, *keys)
            ) as cur:
                return await cur.fetchone() is not None

    @staticmethod
    def _cooldown(settings: StorySettings, minimum: int = 0) -> timedelta:
        minutes = _to_int(getattr(settings, "drip_delay_minutes", 45), 45)
        minutes = max(minimum, min(STORY_DRIP_DELAY_MAX_MINUTES, minutes))
        return timedelta(minutes=minutes)

    async def calculate_scheduled_time(self, settings: StorySettings, not_before: Optional[datetime] = None) -> datetime:
        """
        Calculates the earliest valid UTC datetime to publish the next story for this user.
        Takes into account:
        1. Prime hours window (Tashkent time)
        2. Cooldown since last published story (at least MIN_QUEUE_SPACING_MINUTES)
        3. Already pending items in queue (staggered spacing)
        4. not_before (retry / flood-wait / next-day deferrals)
        """
        now_uzb = self.get_uzb_now()
        cooldown = self._cooldown(settings, minimum=MIN_QUEUE_SPACING_MINUTES)

        target_uzb = now_uzb
        if not_before is not None:
            nb = not_before if not_before.tzinfo else not_before.replace(tzinfo=timezone.utc)
            target_uzb = max(target_uzb, nb.astimezone(UZB_TZ))

        # 1. Check last posted story time
        last_posted_utc = await db_manager.get_last_posted_story_time(settings.user_id)
        if last_posted_utc:
            target_uzb = max(target_uzb, last_posted_utc.astimezone(UZB_TZ) + cooldown)

        # 2. Check pending queue items for user
        user_pending = await db_manager.get_user_story_queue(settings.user_id)
        if user_pending:
            parsed = [_parse_db_time(q.scheduled_at) for q in user_pending]
            parsed = [p for p in parsed if p is not None]
            if parsed:
                target_uzb = max(target_uzb, max(parsed).astimezone(UZB_TZ) + cooldown)

        # 3. Check Prime Hours constraint
        if settings.prime_hours_enabled:
            sh = parse_hour(settings.prime_hours_start, 9)
            eh = parse_hour(settings.prime_hours_end, 22)
            if not self.is_in_prime_hours(sh, eh, target_uzb):
                target_uzb = self.next_prime_start(target_uzb, sh)

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
        score: int = 0,
        not_before: Optional[datetime] = None
    ) -> Tuple[int, str]:
        """
        Calculates optimal scheduled time and stores listing into the persistent queue.
        Returns: (queue_item_id, scheduled_at_utc_str)
        """
        if not await db_manager.is_vip(user_id):
            logger.warning(f"Rejecting story enqueue for user {user_id}: VIP subscription not active")
            raise PermissionError(f"User {user_id} does not have an active VIP subscription")

        st = await db_manager.get_story_settings(user_id)
        sched_utc = await self.calculate_scheduled_time(st, not_before=not_before)
        sched_str = _db_time(sched_utc)

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

    async def reconcile_stale_processing(self, older_than_minutes: int = PROCESSING_STALE_MINUTES) -> int:
        """Releases 'processing' claims left behind by a crashed worker back to 'pending'"""
        cutoff = _db_time(datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes))
        cursor = await db_manager.execute(
            "UPDATE story_queue SET status = 'pending' WHERE status = 'processing' AND datetime(scheduled_at) <= datetime(?)",
            (cutoff,)
        )
        released = cursor.rowcount if cursor is not None and cursor.rowcount and cursor.rowcount > 0 else 0
        if released:
            logger.warning(f"Released {released} stale 'processing' story queue item(s) back to pending")
        return released

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

                    await self.reconcile_stale_processing()
                    due_items = await db_manager.get_due_story_queue_items()
                    for item in due_items:
                        if not self._is_running:
                            break
                        try:
                            await self._process_due_item(item, story_cloner_service)
                        except asyncio.CancelledError:
                            raise
                        except Exception as item_err:
                            # One broken item must not abort the batch; its claim is released by reconciliation
                            logger.error(f"Error processing story queue item #{item.id}: {item_err}", exc_info=True)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in story queue worker loop: {e}", exc_info=True)
                    next_tick = time.monotonic() + 10.0
                    await asyncio.sleep(10.0)
        finally:
            self._is_running = False
            self._worker_task = None

    # ------------------------------------------------------------------
    # Queue item state transitions
    # ------------------------------------------------------------------

    @staticmethod
    async def _claim(item_id: int) -> bool:
        """pending -> processing (compare-and-set). The claim time is stored in scheduled_at."""
        cursor = await db_manager.execute(
            "UPDATE story_queue SET status = 'processing', scheduled_at = ? WHERE id = ? AND status = 'pending'",
            (_db_time(datetime.now(timezone.utc)), item_id)
        )
        return bool(cursor is not None and cursor.rowcount == 1)

    @staticmethod
    async def _reschedule(item_id: int, when_utc: datetime, payload: Dict[str, Any], error_message: Optional[str] = None) -> str:
        when_str = _db_time(when_utc)
        if error_message is None:
            await db_manager.execute(
                "UPDATE story_queue SET scheduled_at = ?, payload_json = ?, status = 'pending' WHERE id = ?",
                (when_str, json.dumps(payload, ensure_ascii=False), item_id)
            )
        else:
            await db_manager.execute(
                "UPDATE story_queue SET scheduled_at = ?, payload_json = ?, status = 'pending', error_message = ? WHERE id = ?",
                (when_str, json.dumps(payload, ensure_ascii=False), str(error_message)[:500], item_id)
            )
        return when_str

    @staticmethod
    async def _record_terminal_failure(item: StoryQueueItem, payload: Dict[str, Any], settings: StorySettings, error: str) -> None:
        """Marks the item failed and records the post as 'failed', so neither the queue nor the watchdog
        re-renders it again"""
        await db_manager.mark_story_queue_item_done(item.id, "failed", str(error)[:500])
        try:
            await db_manager.record_posted_story(
                user_id=item.user_id,
                source_channel=item.source_channel,
                source_id=db_manager.normalize_peer_id(payload.get("source_id")),
                source_msg_id=item.source_msg_id,
                story_id=None,
                price=item.price,
                caption_snippet=payload.get("caption_snippet", "") or "",
                target_type=getattr(settings, "target_type", "self") or "self",
                status="failed",
                grouped_id=payload.get("grouped_id")
            )
        except Exception:
            logger.debug("Could not record failed story post", exc_info=True)

    async def _effective_daily_limit(self, settings: StorySettings, story_cloner_service, user_id: int) -> int:
        limit = _to_int(getattr(settings, "max_stories_per_day", 5), 5)
        limit = max(STORY_MAX_PER_DAY_MIN, min(STORY_MAX_PER_DAY_MAX, limit))
        try:
            premium = await story_cloner_service.is_premium_account(user_id)
        except Exception:
            premium = None
        if premium is False:
            limit = min(limit, NON_PREMIUM_DAILY_STORY_LIMIT)
        return limit

    @staticmethod
    async def _publish_backoff_until(story_cloner_service, user_id: int) -> Optional[float]:
        try:
            until = await story_cloner_service.get_publish_backoff_until(user_id)
        except Exception:
            return None
        return float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) else None

    @staticmethod
    def _channel_ref(item: StoryQueueItem, payload: Dict[str, Any]) -> Any:
        """A reference the user's own client can resolve: @username, or the -100 channel id as int"""
        username = (payload.get("channel_username") or "").strip().lstrip("@")
        if username:
            return f"@{username}"
        raw_id = db_manager.normalize_peer_id(payload.get("channel_id") or payload.get("source_id"))
        src = str(item.source_channel or "").strip()
        if raw_id is None and src and not src.startswith("@"):
            raw_id = db_manager.normalize_peer_id(src)
        if raw_id is not None:
            return int(f"-100{raw_id}")
        return src

    async def _process_due_item(self, item: StoryQueueItem, story_cloner_service=None):
        """Processes a single due queue item"""
        if story_cloner_service is None:
            from services.story_cloner_service import story_cloner_service as default_scs
            story_cloner_service = default_scs

        if not await self._claim(item.id):
            logger.debug(f"Story queue item #{item.id} already claimed or finished; skipping")
            return

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

        # Parse payload
        try:
            payload = json.loads(item.payload_json) if item.payload_json else {}
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
        except Exception as err:
            await db_manager.mark_story_queue_item_done(item.id, "failed", f"Corrupt payload: {err}")
            return

        try:
            await self._publish_claimed_item(item, payload, st, story_cloner_service)
        except asyncio.CancelledError:
            # Shutdown while publishing: hand the item back to the queue
            try:
                await self._reschedule(item.id, datetime.now(timezone.utc), payload)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            raise
        except Exception as e:
            err_str = str(e)
            logger.error(f"Error publishing queue item #{item.id}: {err_str}", exc_info=True)
            await self._handle_failure(item, payload, st, None, err_str)

    async def _publish_claimed_item(self, item: StoryQueueItem, payload: Dict[str, Any], st: StorySettings, story_cloner_service):
        user_id = item.user_id
        now_utc = datetime.now(timezone.utc)

        # 1. Never publish the same channel post twice
        if await db_manager.is_story_posted(
            user_id=user_id,
            source_channel=item.source_channel,
            source_msg_id=item.source_msg_id,
            source_id=db_manager.normalize_peer_id(payload.get("source_id")),
            grouped_id=payload.get("grouped_id")
        ):
            await db_manager.mark_story_queue_item_done(item.id, "skipped", "Already posted to Stories")
            return

        # 2. Cooldown since the last published story
        cooldown = self._cooldown(st)
        last_posted = await db_manager.get_last_posted_story_time(user_id)
        if last_posted and cooldown and now_utc - last_posted < cooldown:
            when = await self._reschedule(item.id, last_posted + cooldown, payload)
            logger.info(f"Queue item #{item.id} waits for the cooldown until {when} UTC")
            return

        # 3. Daily limit per Tashkent day (capped for non-Premium accounts)
        daily_limit = await self._effective_daily_limit(st, story_cloner_service, user_id)
        today_count = await self.count_posted_today(user_id)
        if today_count >= daily_limit:
            next_day = self.tashkent_day_start() + timedelta(days=1)
            # calculate_scheduled_time staggers after the user's other pending items (no burst at the day start)
            sched = await self.calculate_scheduled_time(st, not_before=next_day)
            when = await self._reschedule(item.id, sched, payload)
            logger.info(f"Queue item #{item.id} postponed to {when} UTC (daily limit {daily_limit} reached)")
            return

        # 4. Account-level back-off after FloodWait / STORIES_TOO_MUCH
        backoff_until = await self._publish_backoff_until(story_cloner_service, user_id)
        if backoff_until and backoff_until > time.time():
            when = await self._reschedule(item.id, datetime.fromtimestamp(backoff_until, tz=timezone.utc) + timedelta(seconds=5), payload)
            logger.info(f"Queue item #{item.id} deferred to {when} UTC (Telegram story back-off for user {user_id})")
            return

        # 5. Telethon client
        client = await story_cloner_service.get_client_for_user(user_id)
        if not client or not client.is_connected():
            retries = _to_int(payload.get("client_retries", 0), 0) + 1
            if retries >= CLIENT_MAX_RETRIES:
                logger.warning(f"User {user_id} client permanently disconnected for #{item.id}; marking failed.")
                await self._record_terminal_failure(item, payload, st, "Telethon client not connected after 10 attempts")
                return
            payload["client_retries"] = retries
            logger.warning(f"User {user_id} Telethon client not connected for queue #{item.id} (attempt {retries}/{CLIENT_MAX_RETRIES}); postponing 5 minutes.")
            await self._reschedule(item.id, now_utc + timedelta(minutes=5), payload)
            return

        # 6. Publish story
        target_ch = self._channel_ref(item, payload)
        logger.info(f"Publishing queued story #{item.id} (score {item.score}) for user {user_id} via channel {target_ch}...")
        result = await story_cloner_service.post_story_from_channel(
            client=client,
            channel_identifier=target_ch,
            msg_id=item.source_msg_id,
            bg_style=st.background_style,
            source_photo_path=payload.get("source_photo_path"),
            badges=payload.get("badges"),
            user_id=user_id,
            pinned=st.pin_to_profile,
            target_type=st.target_type,
            target_channel=st.target_channel
        )
        if not isinstance(result, tuple) or len(result) < 3:
            raise RuntimeError(f"Unexpected publish result: {result!r}")
        ok, story_id, res_msg = result[0], result[1], result[2]
        story_url = result[3] if len(result) > 3 else None

        if not ok:
            await self._handle_failure(item, payload, st, result, str(res_msg))
            return

        await db_manager.mark_story_queue_item_done(item.id, "sent")
        await db_manager.record_posted_story(
            user_id=user_id,
            source_channel=item.source_channel,
            source_id=db_manager.normalize_peer_id(payload.get("source_id")),
            source_msg_id=item.source_msg_id,
            story_id=story_id,
            price=item.price,
            caption_snippet=payload.get("caption_snippet", "") or "",
            target_type=st.target_type,
            status="success",
            grouped_id=payload.get("grouped_id")
        )

        # Record fingerprint for deduplication
        fp = payload.get("fingerprint")
        if fp:
            await db_manager.record_listing_hash(
                user_id=user_id,
                listing_hash=fp,
                source_channel=item.source_channel,
                source_msg_id=item.source_msg_id
            )

        bot = getattr(story_cloner_service, "_bot_instance", None)
        if bot:
            try:
                story_link_html = f'\n{LINK} <a href="{html.escape(str(story_url), quote=True)}">Istoriyani ochish</a>' if isinstance(story_url, str) and story_url else ''
                text = f"""
{STAR_SPARKLE} <b>Sara variant navbatdan Istoriyaga joylandi!</b>

├ {TROPHY} <b>Sifat bali:</b> <b>{_to_int(item.score, 0)}/100</b>
├ {CHANNEL} <b>Manba kanal:</b> {html.escape(str(item.source_channel or ''))}
├ {MONEY} <b>Narxi:</b> <b>{format_price_usd(item.price)}</b>
├ {LOCATION} <b>Hudud:</b> {html.escape(item.district or "Aniqlanmadi")}
└ {TIMER} <b>Vaqt:</b> Prime Time (Optimal ko'rish soati){story_link_html}
"""
                await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except Exception as ne:
                logger.warning(f"Notification error: {ne}")

    async def _handle_failure(
        self,
        item: StoryQueueItem,
        payload: Dict[str, Any],
        st: StorySettings,
        result: Any,
        res_msg: str
    ) -> None:
        """Applies the retry policy for a failed publication:
        permanent errors -> failed (never retried); STORIES_TOO_MUCH / FloodWait -> deferred by Telegram's wait;
        other errors -> up to TRANSIENT_MAX_RETRIES retries with growing delay."""
        kind = getattr(result, "error_kind", None) or "transient"
        retry_after = getattr(result, "retry_after", None)
        if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
            retry_after = None
        now_utc = datetime.now(timezone.utc)

        if kind in ("permanent", "not_found", "vip"):
            logger.warning(f"Queue item #{item.id} failed permanently ({kind}): {res_msg}")
            await self._record_terminal_failure(item, payload, st, res_msg)
            return

        if kind in ("limit", "flood"):
            deferrals = _to_int(payload.get("deferrals", 0), 0) + 1
            if deferrals > DEFERRAL_MAX_ATTEMPTS:
                logger.error(f"Queue item #{item.id} dropped after {DEFERRAL_MAX_ATTEMPTS} deferrals: {res_msg}")
                await self._record_terminal_failure(item, payload, st, res_msg)
                return
            payload["deferrals"] = deferrals
            delay = retry_after if retry_after else STORIES_LIMIT_RETRY_SECONDS
            when = await self._reschedule(
                item.id, now_utc + timedelta(seconds=max(30.0, float(delay))), payload,
                error_message=f"Deferred #{deferrals}: {str(res_msg)[:400]}"
            )
            logger.warning(f"Queue item #{item.id} deferred to {when} UTC ({kind}): {res_msg}")
            return

        retries = _to_int(payload.get("retries", 0), 0)
        if retries < TRANSIENT_MAX_RETRIES:
            payload["retries"] = retries + 1
            delay_sec = 120 * (retries + 1)
            when = await self._reschedule(
                item.id, now_utc + timedelta(seconds=delay_sec), payload,
                error_message=f"Retry #{retries + 1}: {str(res_msg)[:400]}"
            )
            logger.warning(
                f"Queue item #{item.id} post failed (attempt {retries + 1}/{TRANSIENT_MAX_RETRIES}): {res_msg}. "
                f"Rescheduling in {delay_sec}s at {when}."
            )
            return

        logger.error(f"Queue item #{item.id} post permanently failed after {TRANSIENT_MAX_RETRIES} attempts: {res_msg}")
        await self._record_terminal_failure(item, payload, st, res_msg)

    def stop_worker(self):
        """Stops the queue worker"""
        self._is_running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()


story_queue_service = StoryQueueService()
