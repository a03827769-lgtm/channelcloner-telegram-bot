import os
import hmac
import hashlib
import zipfile
import tempfile
import asyncio
import unittest
import logging
from unittest.mock import patch, MagicMock

from database.db_manager import DatabaseManager, ReentrantAsyncLock
from services.log_viewer import MemoryLogHandler
from scripts.decrypt_backup import decrypt_backup
from services.translator_service import TranslatorService
from services.ai_paraphraser import AIParaphraserService

class TestAuditPhase2Verification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_audit.db")
        self.db = DatabaseManager(db_path=self.db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        await self.db.close()
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. Stars to'lov idempotentligi
    async def test_stars_payment_idempotency_and_db_constraint(self):
        user_id = 99887766
        await self.db.get_or_create_user(user_id, "Idempotent User", "idemouser")

        charge_id = "test_stars_unique_tx_9988"
        self.assertFalse(await self.db.is_payment_processed(charge_id))

        # First activation
        sub1 = await self.db.activate_subscription(
            user_id=user_id,
            tier="pro",
            stars=100,
            charge_id=charge_id,
            days=30
        )
        self.assertEqual(sub1.tier, "pro")
        self.assertEqual(sub1.stars_spent, 100)
        self.assertTrue(await self.db.is_payment_processed(charge_id))

        # Second activation attempt with identical charge_id (replay)
        sub2 = await self.db.activate_subscription(
            user_id=user_id,
            tier="pro",
            stars=100,
            charge_id=charge_id,
            days=30
        )
        self.assertEqual(sub2.tier, "pro")
        # Expiration date and stars_spent must remain identical, not doubled!
        self.assertEqual(sub1.expires_at, sub2.expires_at)
        self.assertEqual(sub2.stars_spent, 100)

        # Direct DB check: exactly 1 payment record exists
        async with self.db.get_connection() as db:
            cur = await db.execute("SELECT COUNT(*) FROM payments WHERE telegram_payment_charge_id = ?", (charge_id,))
            count = (await cur.fetchone())[0]
            self.assertEqual(count, 1)

            # Direct duplicate insert must trigger SQLite UNIQUE constraint violation
            with self.assertRaises(Exception):
                await db.execute(
                    "INSERT INTO payments (user_id, telegram_payment_charge_id, amount, tier) VALUES (?, ?, ?, ?)",
                    (user_id, charge_id, 100, "pro")
                )
                await db.commit()

    # 2. Backward compatibility: Unencrypted .zip backups in decrypt_backup.py
    def test_decrypt_backup_legacy_unencrypted_zip_compatibility(self):
        legacy_zip_path = os.path.join(self.temp_dir, "legacy_backup.zip")
        dummy_content = b"SQLite legacy unencrypted database snapshot payload"

        with zipfile.ZipFile(legacy_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("cloner.db", dummy_content)

        self.assertTrue(zipfile.is_zipfile(legacy_zip_path))

        # Test decrypt_backup with extraction
        output_extracted_dir = os.path.join(self.temp_dir, "extracted_legacy_backup")
        success = decrypt_backup(
            file_path=legacy_zip_path,
            extract=True
        )
        self.assertTrue(success)

        extracted_file = os.path.join(output_extracted_dir, "cloner.db")
        self.assertTrue(os.path.exists(extracted_file))
        with open(extracted_file, "rb") as f:
            self.assertEqual(f.read(), dummy_content)

    # 3. Log orqali sir chiqishi (log_viewer secret redaction)
    def test_log_viewer_secret_redaction(self):
        handler = MemoryLogHandler(maxlen=50)
        logger = logging.getLogger("test_redact_logger")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        fake_bot_token = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ_1234567"
        fake_phone = "+998901234567"
        fake_session = "1BQANAAABBBCCCDDDEEEFFFGGGHHHIIIJJJKKKLLLMMMNNNOOOPPQQQRRRSSSTTTUUUVVVWWWXXXYYYZZZ111222333444555"

        logger.info(f"Bot connected with token {fake_bot_token} for user {fake_phone} and session {fake_session}")

        recent = handler.get_recent_logs(count=1)
        self.assertEqual(len(recent), 1)
        log_line = recent[0]

        # Ensure raw sensitive credentials are NOT present
        self.assertNotIn(fake_bot_token, log_line)
        self.assertNotIn(fake_phone, log_line)
        self.assertNotIn(fake_session, log_line)

        # Ensure placeholders replaced them
        self.assertIn("[REDACTED_BOT_TOKEN]", log_line)
        self.assertIn("[REDACTED_PHONE]", log_line)
        self.assertIn("[REDACTED_SESSION]", log_line)

    # 4. ReentrantAsyncLock functionality & depth
    async def test_reentrant_async_lock_reentrancy_and_mutual_exclusion(self):
        lock = ReentrantAsyncLock()
        events = []

        async def outer_and_inner():
            async with lock:
                self.assertEqual(lock._depth, 1)
                events.append("outer_enter")
                async with lock:
                    self.assertEqual(lock._depth, 2)
                    events.append("inner_enter")
                self.assertEqual(lock._depth, 1)
                events.append("inner_exit")
            self.assertEqual(lock._depth, 0)
            events.append("outer_exit")

        await outer_and_inner()
        self.assertEqual(events, ["outer_enter", "inner_enter", "inner_exit", "outer_exit"])

    # 5. Database migration idempotency (multiple consecutive init_db calls)
    async def test_database_migration_idempotency(self):
        # Calling init_db second and third time must not raise duplicate column / index errors
        await self.db.init_db()
        await self.db.init_db()

        # Check that existing tables still function properly
        user = await self.db.get_or_create_user(555, "Test Migrations", "testmig")
        self.assertEqual(user.user_id, 555)

    # 6. Failure resiliency: deep-translator & Gemini API
    async def test_translator_and_paraphraser_resilience_on_failure(self):
        translator = TranslatorService()
        with patch.object(translator, "_get_translator") as mock_gt:
            mock_inst = MagicMock()
            mock_inst.translate.side_effect = Exception("DeepTranslator Connection Timeout")
            mock_gt.return_value = mock_inst

            # Translation failure should log and gracefully return original text
            raw_text = "Salom dunyo bu muhim xabar"
            res = await translator.translate_text(raw_text, target_lang="en", source_lang="uz")
            self.assertEqual(res, raw_text)

        paraphraser = AIParaphraserService()
        with patch.object(paraphraser, "_paraphrase_with_gemini", side_effect=Exception("Gemini Quota Exceeded")):
            raw_content = "Kvartira sotiladi 3 xona Chilonzor"
            # Must gracefully fall back without raising exception
            res = paraphraser.paraphrase(raw_content, mode="formal")
            self.assertTrue(len(res) > 0)
            self.assertIn("Chilonzor", res)

    # 7. GitHub Webhook Signature Verification logic (testing WebhookHandler)
    def test_webhook_handler_signature_verification(self):
        import io
        from deploy.webhook.deploy_webhook import WebhookHandler
        import deploy.webhook.deploy_webhook as dwh

        orig_secret = dwh.WEBHOOK_SECRET
        try:
            dwh.WEBHOOK_SECRET = "test_webhook_secret_key"
            payload = b'{"ref": "refs/heads/main"}'
            valid_sig = "sha256=" + hmac.new(b"test_webhook_secret_key", payload, hashlib.sha256).hexdigest()

            # 1. Invalid signature should be rejected with 403
            handler = MagicMock()
            handler.headers = {
                'Content-Length': str(len(payload)),
                'X-Hub-Signature-256': 'sha256=invalid_signature',
                'X-GitHub-Event': 'push'
            }
            handler.rfile = io.BytesIO(payload)
            handler.wfile = io.BytesIO()
            handler.send_response = MagicMock()
            handler.end_headers = MagicMock()

            handler._reply = lambda status, payload: WebhookHandler._reply(handler, status, payload)
            WebhookHandler.do_POST(handler)
            handler.send_response.assert_called_with(403)

            # 2. Valid signature with ping event should return 200
            handler_valid = MagicMock()
            handler_valid.headers = {
                'Content-Length': str(len(payload)),
                'X-Hub-Signature-256': valid_sig,
                'X-GitHub-Event': 'ping'
            }
            handler_valid.rfile = io.BytesIO(payload)
            handler_valid.wfile = io.BytesIO()
            handler_valid.send_response = MagicMock()
            handler_valid.end_headers = MagicMock()

            handler_valid._reply = lambda status, payload: WebhookHandler._reply(handler_valid, status, payload)
            WebhookHandler.do_POST(handler_valid)
            handler_valid.send_response.assert_called_with(200)
        finally:
            dwh.WEBHOOK_SECRET = orig_secret

    # 8. ReentrantAsyncLock child task deadlock scenario
    async def test_reentrant_async_lock_child_task_deadlock_scenario(self):
        lock = ReentrantAsyncLock()

        # Demonstrate the deadlock scenario:
        # A task holding the lock spawns a child task that also requests the lock.
        # Since the child task has a different asyncio.current_task() identity,
        # it blocks waiting for the lock. If the parent awaits the child, deadlock ensues.
        async def parent_holding_lock():
            async with lock:
                async def child_task():
                    async with lock:
                        return "acquired_by_child"

                # Child task should NOT be able to acquire lock while parent holds it,
                # and waiting on it will timeout (deadlock proof)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(child_task(), timeout=0.1)

        await parent_holding_lock()

    # 9. PID lock and graceful shutdown temp_media purge
    def test_media_handler_purge_temp_media(self):
        from services.media_handler import MediaHandler
        test_temp = os.path.join(self.temp_dir, "test_temp_media")
        os.makedirs(test_temp, exist_ok=True)
        handler = MediaHandler(temp_dir=test_temp)

        # Create dummy orphan file
        orphan_file = os.path.join(test_temp, "orphan_photo.jpg")
        with open(orphan_file, "w") as f:
            f.write("dummy")

        # Create dummy active queued story file (protected from purge)
        protected_file = os.path.join(test_temp, "story_draft.png")
        with open(protected_file, "w") as f:
            f.write("dummy")

        # Purge files older than 0 seconds
        cleaned = handler.purge_temp_media(max_age=0)
        self.assertEqual(cleaned, 1)
        self.assertFalse(os.path.exists(orphan_file))
        self.assertTrue(os.path.exists(protected_file))

if __name__ == "__main__":
    unittest.main()
