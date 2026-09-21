import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import Message, CallbackQuery, User as AiogramUser, Chat
from bot.middlewares.throttling_middleware import ThrottlingMiddleware

class TestThrottlingMiddleware(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.middleware = ThrottlingMiddleware(rate_limit=0.1, burst_limit=3, window_seconds=1.0)
        self.user = AiogramUser(id=1234567, is_bot=False, first_name="TestUser")
        self.chat = Chat(id=1234567, type="private")

    async def test_burst_throttling(self):
        handler_calls = 0
        async def mock_handler(event, data):
            nonlocal handler_calls
            handler_calls += 1
            return "ok"

        msg = MagicMock(spec=Message)
        msg.from_user = self.user
        data = {"event_from_user": self.user}

        # 3 requests within window should pass
        for _ in range(3):
            res = await self.middleware(mock_handler, msg, data)
            self.assertEqual(res, "ok")
        self.assertEqual(handler_calls, 3)

        # 4th request exceeds burst_limit=3, should be throttled (returns None, handler not called)
        res_throttled = await self.middleware(mock_handler, msg, data)
        self.assertIsNone(res_throttled)
        self.assertEqual(handler_calls, 3)

    async def test_callback_query_throttling_notifies_user(self):
        async def mock_handler(event, data):
            return "ok"

        cb = MagicMock(spec=CallbackQuery)
        cb.from_user = self.user
        cb.answer = AsyncMock()
        data = {"event_from_user": self.user}

        # Exhaust 3 tokens
        for _ in range(3):
            await self.middleware(mock_handler, cb, data)

        # 4th callback query
        res = await self.middleware(mock_handler, cb, data)
        self.assertIsNone(res)
        cb.answer.assert_called_once()
        args, kwargs = cb.answer.call_args
        self.assertIn("Juda tez", args[0])

if __name__ == '__main__':
    unittest.main()
