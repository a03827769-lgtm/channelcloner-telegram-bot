import unittest
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import CallbackQuery, Message
from database.db_manager import DatabaseManager
from database.models import ChannelPair
from services.cloner_engine import ClonerEngine
from services.telethon_listener import TelethonListener
import bot.handlers.help_guide as help_guide

class TestFixedClonerFeatures(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = os.path.join(self.temp_dir.name, 'test_fixed.db')
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

        self.pair = ChannelPair(
            id=1,
            user_id=7770001,
            source_channel='@source',
            source_title='Source',
            target_channel='@target',
            target_title='Target',
            replace_words='eski_soz=yangi_soz, 901234567=998887766',
            clean_links=True,
            is_active=True
        )

    async def asyncTearDown(self):
        try:
            await self.db.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    async def test_word_and_phone_replacement_in_cloner_engine(self):
        engine = ClonerEngine()
        raw_text = 'Salom bu eski_soz va bizning raqam 901234567.'
        processed = await engine.process_post_text(raw_text, self.pair)
        self.assertIn('yangi_soz', processed)
        self.assertIn('998887766', processed)
        self.assertNotIn('eski_soz', processed)

    def test_telethon_listener_has_methods(self):
        listener = TelethonListener()
        self.assertTrue(hasattr(listener, '_handle_new_message'))
        self.assertTrue(callable(getattr(listener, '_handle_new_message')))
        self.assertTrue(hasattr(listener, 'clone_history'))
        self.assertTrue(callable(getattr(listener, 'clone_history')))

    async def test_help_guide_callback_and_message(self):
        # 1. Test CallbackQuery
        cb = MagicMock(spec=CallbackQuery)
        cb.answer = AsyncMock()
        cb.message = MagicMock(spec=Message)
        cb.message.edit_text = AsyncMock()
        await help_guide.cb_guide(cb)
        cb.message.edit_text.assert_called_once()
        self.assertIn('Telegram Kloner', cb.message.edit_text.call_args[1]['text'])

        # 2. Test Message
        msg = MagicMock(spec=Message)
        msg.answer = AsyncMock()
        await help_guide.cb_guide(msg)
        msg.answer.assert_called_once()
        self.assertIn('Telegram Kloner', msg.answer.call_args[1]['text'])

if __name__ == '__main__':
    unittest.main()
