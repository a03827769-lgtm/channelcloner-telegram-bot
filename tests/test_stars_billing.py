import unittest
import asyncio
import os
import uuid
from database.db_manager import DatabaseManager

class TestStarsBilling(unittest.TestCase):
    def setUp(self):
        self.db_path = f"temp_media/test_stars_{uuid.uuid4().hex[:8]}.db"
        os.makedirs("temp_media", exist_ok=True)
        self.db = DatabaseManager(self.db_path)
        asyncio.run(self.db.init_db())

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_subscription_and_quota(self):
        async def run():
            user_id = 999111
            await self.db.get_or_create_user(user_id, "Test User")

            # 1. Default subscription is Free with 14-day trial
            sub = await self.db.get_user_subscription(user_id)
            self.assertEqual(sub.tier, "free")
            self.assertTrue(sub.is_trial_active)
            self.assertTrue(sub.is_active)
            self.assertEqual(sub.max_channels, 1)

            # 2. Check quota: initially can add 1 channel
            can_add, max_allowed, cur_cnt = await self.db.can_user_add_channel(user_id)
            self.assertTrue(can_add)
            self.assertEqual(max_allowed, 1)
            self.assertEqual(cur_cnt, 0)

            # Add 1 channel
            await self.db.add_channel_pair(user_id, "@src", "Source", "@tgt", "Target")
            can_add_again, _, cur_cnt = await self.db.can_user_add_channel(user_id)
            self.assertFalse(can_add_again)  # Reached limit for Free tier

            # 3. Upgrade to Pro via 100 Stars
            new_sub = await self.db.activate_subscription(user_id, tier="pro", stars=100, charge_id="chg_123", days=30)
            self.assertEqual(new_sub.tier, "pro")
            self.assertEqual(new_sub.stars_spent, 100)
            self.assertEqual(new_sub.max_channels, 5)

            # Check quota now: can add up to 5
            can_add_pro, max_allowed_pro, _ = await self.db.can_user_add_channel(user_id)
            self.assertTrue(can_add_pro)
            self.assertEqual(max_allowed_pro, 5)

            # 4. Upgrade to VIP via 300 Stars
            vip_sub = await self.db.activate_subscription(user_id, tier="vip", stars=300, charge_id="chg_vip", days=30)
            self.assertEqual(vip_sub.tier, "vip")
            self.assertEqual(vip_sub.stars_spent, 400)
            self.assertEqual(vip_sub.max_channels, 999)

        asyncio.run(run())

if __name__ == "__main__":
    unittest.main()
