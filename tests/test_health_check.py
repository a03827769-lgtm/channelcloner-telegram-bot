import unittest
import os
import asyncio
import aiohttp
from unittest.mock import patch, MagicMock
from run import start_health_server

class TestHealthCheckEmpirical(unittest.IsolatedAsyncioTestCase):
    """
    Empirical tests for Keep-Alive HTTP Healthcheck server.
    Tests real TCP socket binding, dynamic PORT configuration, GET / HEAD routes,
    payload schema, concurrency, and 404 responses.
    """

    async def asyncSetUp(self):
        # Default test port to avoid conflict with running instances
        self.test_port = 8089
        os.environ["PORT"] = str(self.test_port)

    async def asyncTearDown(self):
        if "PORT" in os.environ:
            del os.environ["PORT"]

    async def test_health_server_get_routes_and_schema(self):
        """Verify GET / and GET /health return 200 OK and strict JSON schema."""
        self.test_port = 8095
        os.environ["PORT"] = str(self.test_port)
        
        runner = await start_health_server()
        try:
            async with aiohttp.ClientSession() as session:
                for path in ["/", "/health"]:
                    url = f"http://127.0.0.1:{self.test_port}{path}"
                    async with session.get(url) as resp:
                        self.assertEqual(resp.status, 200, f"GET {path} did not return 200")
                        self.assertIn("application/json", resp.headers.get("Content-Type", ""))
                        
                        data = await resp.json()
                        # Strict schema assertions
                        self.assertIn("status", data)
                        self.assertEqual(data["status"], "ok")
                        self.assertIn("bot", data)
                        self.assertEqual(data["bot"], "running")
                        self.assertIn("service", data)
                        self.assertEqual(data["service"], "telegram-channel-cloner")
                        self.assertIn("telethon_connected", data)
                        self.assertIsInstance(data["telethon_connected"], bool)
        finally:
            await runner.cleanup()

    async def test_health_server_head_routes(self):
        """Verify HEAD / and HEAD /health return 200 OK with empty body."""
        self.test_port = 8090
        os.environ["PORT"] = str(self.test_port)
        
        runner = await start_health_server()
        try:
            async with aiohttp.ClientSession() as session:
                for path in ["/", "/health"]:
                    url = f"http://127.0.0.1:{self.test_port}{path}"
                    async with session.head(url) as resp:
                        self.assertEqual(resp.status, 200, f"HEAD {path} did not return 200")
                        self.assertIn("application/json", resp.headers.get("Content-Type", ""))
                        body = await resp.read()
                        self.assertEqual(len(body), 0, f"HEAD {path} returned non-empty body")
        finally:
            await runner.cleanup()

    async def test_health_server_dynamic_port_binding(self):
        """Verify dynamic port binding on arbitrary PORT (e.g. 8091 and 8092)."""
        for custom_port in [8091, 8092]:
            os.environ["PORT"] = str(custom_port)
            runner = await start_health_server()
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"http://127.0.0.1:{custom_port}/health"
                    async with session.get(url) as resp:
                        self.assertEqual(resp.status, 200)
                        data = await resp.json()
                        self.assertEqual(data["status"], "ok")
            finally:
                await runner.cleanup()

    async def test_health_server_concurrency_and_stress(self):
        """Stress-test health server with 50 concurrent requests."""
        self.test_port = 8093
        os.environ["PORT"] = str(self.test_port)
        
        runner = await start_health_server()
        try:
            async with aiohttp.ClientSession() as session:
                async def fetch(url):
                    async with session.get(url) as resp:
                        self.assertEqual(resp.status, 200)
                        data = await resp.json()
                        return data["status"]

                urls = [f"http://127.0.0.1:{self.test_port}/health" for _ in range(50)]
                results = await asyncio.gather(*[fetch(u) for u in urls])
                self.assertEqual(len(results), 50)
                self.assertTrue(all(r == "ok" for r in results))
        finally:
            await runner.cleanup()

    async def test_health_server_404_for_unknown_paths(self):
        """Verify that undefined endpoints return 404 Not Found."""
        self.test_port = 8094
        os.environ["PORT"] = str(self.test_port)
        
        runner = await start_health_server()
        try:
            async with aiohttp.ClientSession() as session:
                for path in ["/nonexistent", "/api/v1", "/status"]:
                    url = f"http://127.0.0.1:{self.test_port}{path}"
                    async with session.get(url) as resp:
                        self.assertEqual(resp.status, 404, f"Path {path} did not return 404")
        finally:
            await runner.cleanup()

if __name__ == "__main__":
    unittest.main()
