"""
Regression tests for the database layer rework: plan arithmetic, idempotent payments, lost-renewal
protection, owner pause vs. billing suspension, loop detection, clone-record lookups and lifecycle.
Every test uses its own throw-away database file.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from config.settings import settings
from database.db_manager import DatabaseManager
from services.cache_manager import cache_manager


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "t.db"))
    with patch.object(settings, "ADMIN_IDS_RAW", "777"), patch.object(settings, "PRIMARY_SUPER_ADMIN_ID", 0):
        await manager.init_db()
        yield manager
    await manager.close()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _fresh_sub(db, user_id):
    await cache_manager.sub_cache.delete(f"sub_{user_id}")
    return await db.get_user_subscription(user_id)


async def test_init_db_is_idempotent_and_stamps_version(db):
    await db.init_db()
    async with db.get_connection() as conn:
        cur = await conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == DatabaseManager.SCHEMA_VERSION


async def test_payment_is_idempotent_on_charge_id(db):
    await db.get_or_create_user(1, "Alice", "alice")
    first = await db.activate_subscription(1, "pro", 100, "charge-1", days=30)
    again = await db.activate_subscription(1, "pro", 100, "charge-1", days=30)
    assert first.tier == "pro"
    assert again.expires_at == first.expires_at


async def test_upgrade_converts_remaining_time_at_price_ratio(db):
    await db.activate_subscription(2, "pro", 100, "c-pro", days=30)
    vip = await db.activate_subscription(2, "vip", 300, "c-vip", days=30)
    days_left = (datetime.fromisoformat(vip.expires_at) - _now()).days
    assert vip.tier == "vip"
    assert 39 <= days_left <= 40  # 30 VIP days + 30 Pro days worth 10 VIP days


async def test_buying_lower_tier_never_downgrades(db):
    vip = await db.activate_subscription(3, "vip", 300, "c-vip3", days=30)
    after = await db.activate_subscription(3, "pro", 100, "c-pro3", days=30)
    assert after.tier == "vip"
    extra = datetime.fromisoformat(after.expires_at) - datetime.fromisoformat(vip.expires_at)
    assert timedelta(days=9, hours=23) <= extra <= timedelta(days=10, hours=1)


async def test_foreign_key_failure_is_not_reported_as_duplicate(db):
    # A payment for an unknown user must still be recorded (the users row is created on the fly)
    sub = await db.activate_subscription(4, "pro", 100, "c-fk", days=30)
    assert sub.tier == "pro"
    assert await db.is_payment_processed("c-fk")


async def test_expired_plan_is_suspended_but_renewal_is_kept(db):
    await db.activate_subscription(5, "pro", 100, "c5", days=30)
    async with db.write_transaction() as w:
        await w.execute("UPDATE subscriptions SET expires_at = ? WHERE user_id = 5", ((_now() - timedelta(days=1)).isoformat(),))
    assert (await _fresh_sub(db, 5)).tier == "free"
    renewed = await db.activate_subscription(5, "pro", 100, "c5b", days=30)
    assert renewed.tier == "pro"
    assert (await _fresh_sub(db, 5)).tier == "pro"


async def test_owner_pause_survives_renewal_and_limits_apply(db):
    await db.get_or_create_user(6, "Bob", None)
    await db.activate_subscription(6, "pro", 100, "c6", days=30)
    ids = [await db.add_channel_pair(6, source_channel=f"@src{i}", target_channel=f"@tgt{i}") for i in range(7)]
    assert await db.add_channel_pair(6, source_channel="@src0", target_channel="@tgt0") == ids[0]

    await db.enforce_plan_limits(6)
    assert sum(p.is_active for p in await db.get_user_channel_pairs(6)) == 5

    state, error = await db.set_pair_active_by_owner(ids[6], True)
    assert error == "plan_limit" and state is False

    state, error = await db.set_pair_active_by_owner(ids[0], False)
    assert (state, error) == (False, None)
    await db.activate_subscription(6, "pro", 100, "c6b", days=30)
    assert (await db.get_pair_by_id(ids[0])).is_active is False


async def test_inactive_subscription_blocks_resume(db):
    await db.get_or_create_user(7, "Eve", None)
    await db.get_user_subscription(7)  # creates the trial row
    pair_id = await db.add_channel_pair(7, source_channel="@a7", target_channel="@b7")
    await db.set_pair_active_by_owner(pair_id, False)
    async with db.write_transaction() as w:
        await w.execute("UPDATE subscriptions SET trial_expires_at = ? WHERE user_id = 7", ((_now() - timedelta(days=1)).isoformat(),))
    await cache_manager.sub_cache.delete("sub_7")
    state, error = await db.set_pair_active_by_owner(pair_id, True)
    assert error == "subscription_inactive"


async def test_cycle_detection(db):
    await db.get_or_create_user(8, "Cy", None)
    await db.add_channel_pair(8, source_channel="@alpha", target_channel="@beta")
    await db.add_channel_pair(8, source_channel="@beta", target_channel="@gamma")
    assert await db.would_create_cycle("@gamma", "@alpha") is True
    assert await db.would_create_cycle("@beta", "@alpha") is True
    assert await db.would_create_cycle("@alpha", "@delta") is False
    assert await db.would_create_cycle("@same", "@same") is True


async def test_clone_record_lookup_by_any_peer_id_form(db):
    await db.get_or_create_user(9, "Lu", None)
    pair_id = await db.add_channel_pair(9, source_channel="-1001234567890", target_channel="@t9",
                                        source_id=-1001234567890, target_id=-1009999999999)
    await db.record_cloned_message(pair_id, 55, target_msg_id=900)
    rows = await db.get_cloned_messages_for_source(55, peer_id=1234567890)
    assert len(rows) == 1 and rows[0]["target_msg_id"] == 900 and rows[0]["pair_target_id"] == -1009999999999
    assert await db.get_cloned_messages_for_source(55, peer_id=-1001234567890)
    assert await db.is_own_clone_post(-1009999999999, 900) is True
    assert await db.is_own_clone_post(9999999999, 901) is False


async def test_forgotten_records_can_be_retried(db):
    await db.get_or_create_user(10, "Re", None)
    pair_id = await db.add_channel_pair(10, source_channel="@s10", target_channel="@t10")
    await db.record_cloned_message(pair_id, 70, target_msg_id=None, status="queued")
    assert await db.is_message_cloned(pair_id, 70)
    await db.forget_cloned_messages(pair_id, [70])
    assert not await db.is_message_cloned(pair_id, 70)


async def test_source_change_resets_watermark(db):
    await db.get_or_create_user(11, "Wm", None)
    pair_id = await db.add_channel_pair(11, source_channel="@s11", target_channel="@t11", source_id=111)
    await db.update_pair_last_seen_msg_id(pair_id, 500)
    await db.update_pair_source_id(pair_id, -100222)
    assert await db.get_effective_last_source_msg_id(pair_id) is None


async def test_story_settings_partial_update_keeps_other_columns(db):
    st = await db.update_story_settings(12, source_channel="@listings", min_price=500.0)
    assert st.source_channel == "@listings" and st.min_price == 500.0
    st = await db.update_story_settings(12, is_active=False)
    assert st.is_active is False and st.source_channel == "@listings"


async def test_env_admin_is_not_persisted_and_granted_admin_is(db):
    assert await db.is_admin(777)
    await db.get_or_create_user(777, "Root", "root", is_admin=True)
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT is_admin FROM users WHERE user_id = 777")
        assert (await cur.fetchone())[0] == 0
    await db.set_admin_status(13, True)
    assert await db.is_admin(13)
    await db.set_admin_status(13, False)
    assert not await db.is_admin(13)


async def test_maintenance_and_final_close(tmp_path):
    manager = DatabaseManager(str(tmp_path / "m.db"))
    await manager.init_db()
    stats = await manager.run_maintenance()
    assert "deleted_fsm_rows" in stats
    await manager.close(final=True)
    with pytest.raises(RuntimeError):
        await manager.get_user_by_id(1)


async def test_write_transaction_commits_without_explicit_commit(db):
    async with db.write_transaction() as w:
        await w.execute("INSERT INTO app_settings (key, value) VALUES ('k', 'v')")
    # A rollback issued afterwards must not discard the committed row
    async with db.get_connection() as conn:
        await conn.rollback()
        cur = await conn.execute("SELECT value FROM app_settings WHERE key = 'k'")
        assert (await cur.fetchone())[0] == "v"


async def test_resume_refuses_self_loops_cycles_and_duplicates(db):
    await db.get_or_create_user(40, "Owner", "owner40")
    await db.activate_subscription(40, "vip", 300, "c-vip40", days=30)
    a_to_b = await db.add_channel_pair(40, "@chan_a", "A", "@chan_b", "B", source_id=-1001000000001, target_id=-1001000000002)
    await db.set_pair_active_by_owner(a_to_b, False)
    # A new B -> A pair is refused even while A -> B is paused (paused pairs still count for loops)
    assert await db.would_create_cycle("@chan_b", "@chan_a", -1001000000002, -1001000000001)

    # Legacy data may already hold a loop: resuming either side of it is refused
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO channel_pairs (user_id, source_channel, source_title, target_channel, target_title, "
            "source_id, target_id, is_active) VALUES (40, '@chan_b', 'B', '@chan_a', 'A', -1001000000002, -1001000000001, 1)"
        )
    assert await db.set_pair_active_by_owner(a_to_b, True) == (False, "cycle")

    twin = await db.add_channel_pair(40, "@chan_c", "C", "@chan_d", "D")
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO channel_pairs (user_id, source_channel, source_title, target_channel, target_title, is_active) "
            "VALUES (40, '@Chan_C', 'C', 'https://t.me/chan_d', 'D', 0)"
        )
        cur = await conn.execute("SELECT MAX(id) FROM channel_pairs")
        copy_id = (await cur.fetchone())[0]
    assert copy_id != twin
    assert await db.set_pair_active_by_owner(copy_id, True) == (False, "duplicate")
    # Admins bypass plan limits, never the safety checks
    assert await db.set_pair_active_by_owner(copy_id, True, bypass_limits=True) == (False, "duplicate")


def test_subscription_dates_with_a_z_suffix_parse_on_every_python():
    """datetime.fromisoformat() rejects a trailing "Z" before Python 3.11 (CI runs 3.10): such an expiry
    must still count as UTC instead of turning an active plan into an expired one."""
    from database.models import Subscription
    parse = Subscription._parse_iso_to_utc_naive
    assert parse("2026-01-01T10:00:00Z") == datetime(2026, 1, 1, 10, 0)
    assert parse("2026-01-01 10:00:00") == datetime(2026, 1, 1, 10, 0)
    assert parse("2026-01-01T15:00:00+05:00") == datetime(2026, 1, 1, 10, 0)
    assert parse("not a date") is None and parse("") is None and parse(None) is None
    assert Subscription(user_id=1, tier="vip", expires_at="2099-01-01T00:00:00Z").is_active is True
