import unittest
import os
import aiohttp
from run import start_health_server
from database.db_manager import db_manager

class TestMiniAppAPIEmpirical(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_port = 8096
        os.environ["PORT"] = str(self.test_port)
        await db_manager.init_db()

    async def asyncTearDown(self):
        if "PORT" in os.environ:
            del os.environ["PORT"]

    async def test_mini_app_api_endpoints(self):
        runner = await start_health_server()
        try:
            async with aiohttp.ClientSession() as session:
                base_url = f"http://127.0.0.1:{self.test_port}"

                # 1. Test /api/me
                async with session.get(f"{base_url}/api/me?user_id=1234567") as resp:
                    self.assertEqual(resp.status, 200)
                    data = await resp.json()
                    self.assertEqual(data["status"], "ok")
                    self.assertIn("user", data)
                    self.assertIn("subscription", data)
                    self.assertIn("stats", data)

                # 2. Test /api/pairs
                async with session.get(f"{base_url}/api/pairs?user_id=1234567") as resp:
                    self.assertEqual(resp.status, 200)
                    data = await resp.json()
                    self.assertEqual(data["status"], "ok")
                    self.assertIsInstance(data["pairs"], list)

                # 3. Test /api/audio-tracks
                async with session.get(f"{base_url}/api/audio-tracks") as resp:
                    self.assertEqual(resp.status, 200)
                    data = await resp.json()
                    self.assertEqual(data["status"], "ok")
                    self.assertTrue(len(data["tracks"]) >= 10)

                # 4. Test /api/system
                async with session.get(f"{base_url}/api/system") as resp:
                    self.assertEqual(resp.status, 200)
                    data = await resp.json()
                    self.assertEqual(data["status"], "ok")
                    self.assertIn("telemetry", data)
                    self.assertIn("logs", data)

                # 5. Test /api/story/settings
                async with session.get(f"{base_url}/api/story/settings?user_id=1234567") as resp:
                    self.assertEqual(resp.status, 200)
                    data = await resp.json()
                    self.assertEqual(data["status"], "ok")
                    self.assertIn("settings", data)

                # 6. Test Mini App static frontend delivery at /app
                async with session.get(f"{base_url}/app") as resp:
                    self.assertEqual(resp.status, 200)
                    text = await resp.text()
                    self.assertIn("<div id=\"root\"></div>", text)
                    self.assertIn("ChannelCloner Pro", text)
        finally:
            await runner.cleanup()

if __name__ == "__main__":
    unittest.main()
