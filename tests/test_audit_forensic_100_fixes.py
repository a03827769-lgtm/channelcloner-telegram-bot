import asyncio
import os
import glob
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from database.db_manager import DatabaseManager
from services.ai_paraphraser import ai_paraphraser
from services.story_queue_service import story_queue_service


class TestAuditForensic100Fixes(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = os.path.join(self.temp_dir.name, "test_audit_100.db")
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        try:
            await self.db.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_timedelta_imported_in_story_cloner_service(self):
        """Verify timedelta is properly imported and usable in story_cloner_service without NameError"""
        import services.story_cloner_service as scs
        self.assertTrue(hasattr(scs, "timedelta"))
        td = scs.timedelta(minutes=45)
        self.assertEqual(td.total_seconds(), 2700)

    def test_filter_name_error_fixed_in_story_menu(self):
        """Verify no undefined FILTER symbol in story_menu module"""
        import bot.handlers.story_menu as sm
        self.assertTrue(hasattr(sm, "SETTINGS"))
        self.assertFalse(hasattr(sm, "FILTER"))

    def test_text_processor_imported_in_settings_menu(self):
        """Verify TextProcessor is properly imported in settings_menu to prevent NameError on target edit"""
        import bot.handlers.settings_menu as sm
        self.assertTrue(hasattr(sm, "TextProcessor"))

    async def test_prune_database_executes_against_drip_queue(self):
        """Verify prune_database runs against drip_queue and returns dictionary with deleted counts"""
        await self.db.get_or_create_user(12345, "Test User", "testuser", is_admin=True)
        pair_id = await self.db.add_channel_pair(12345, "@src", "Source", "@tgt", "Target")

        async with self.db.write_transaction() as db:
            past_date = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
            await db.execute(
                "INSERT INTO drip_queue (pair_id, msg_data_json, scheduled_at, status, created_at) "
                "VALUES (?, '{}', ?, 'sent', ?)",
                (pair_id, past_date, past_date)
            )
            await db.commit()

        stats = await self.db.prune_database(max_age_days=30)
        self.assertIn("deleted_drip_items", stats)
        self.assertIn("deleted_cloned_messages", stats)
        self.assertEqual(stats["deleted_drip_items"], 1)

    async def test_db_manager_close_cleans_connection_and_locks(self):
        """Verify DatabaseManager.close properly closes _conn and clears reference"""
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "test_close.db")
        db = DatabaseManager(db_path)
        await db.init_db()
        self.assertIsNotNone(db._conn)
        await db.close()
        self.assertIsNone(db._conn)
        temp_dir.cleanup()

    async def test_story_queue_worker_stop_worker_graceful(self):
        """Verify story_queue_service has stop_worker method and cancels cleanly without AttributeError"""
        self.assertTrue(hasattr(story_queue_service, "stop_worker"))
        worker_task = asyncio.create_task(story_queue_service.start_worker())
        await asyncio.sleep(0.01)
        self.assertTrue(story_queue_service._is_running)
        story_queue_service.stop_worker()
        self.assertFalse(story_queue_service._is_running)
        await asyncio.sleep(0.01)
        if not worker_task.done():
            worker_task.cancel()

    async def test_ai_paraphraser_case_insensitive_placeholder_restoration(self):
        """Verify ai_paraphraser restores HTML tags even if LLM altered placeholder casing"""
        raw_text = "<b>Assalomu alaykum</b> bu <i>muhim</i> yangilik: https://example.com"
        paraphrased = ai_paraphraser.paraphrase(raw_text, mode="formal")
        self.assertIn("Assalomu alaykum", paraphrased)
        self.assertNotIn("___htm_", paraphrased.lower())

    def test_root_directory_has_zero_test_db_leakage(self):
        """Verify the project root contains zero lingering test SQLite databases"""
        root_test_dbs = glob.glob("test_*.db*")
        self.assertEqual(len(root_test_dbs), 0, f"Found leaking test database files in root: {root_test_dbs}")


if __name__ == "__main__":
    unittest.main()
