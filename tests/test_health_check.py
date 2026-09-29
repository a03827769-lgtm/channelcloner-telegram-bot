"""
Keep-alive HTTP server of run.py: liveness (/health) schema, HEAD support, PORT binding, concurrency and
404s. Every test binds a free loopback port chosen at runtime, so the suite never collides with a running
bot (8080) or with parallel test runs. "/" serves the Mini App SPA when webapp/dist exists (HTML) and the
health JSON otherwise, so both are accepted there; /health is always JSON.
"""
import asyncio
import os
import socket
from contextlib import asynccontextmanager
from unittest.mock import patch

import aiohttp

from run import start_health_server

HEALTH_KEYS = {"status", "bot", "service", "telethon_connected"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bound_port(runner) -> int:
    for address in runner.addresses:
        if isinstance(address, tuple) and len(address) >= 2:
            return address[1]
    raise AssertionError(f"HTTP server did not bind: {runner.addresses!r}")


@asynccontextmanager
async def running_server(port: int):
    with patch.dict(os.environ, {"PORT": str(port)}):
        runner = await start_health_server()
    try:
        yield runner
    finally:
        await runner.cleanup()


def _assert_health_payload(data: dict) -> None:
    assert HEALTH_KEYS <= set(data), data
    assert data["status"] == "ok"
    assert data["bot"] == "running"
    assert data["service"] == "telegram-channel-cloner"
    assert isinstance(data["telethon_connected"], bool)


async def test_health_endpoint_returns_json_schema():
    port = _free_port()
    async with running_server(port) as runner:
        bound = _bound_port(runner)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{bound}/health") as resp:
                assert resp.status == 200
                assert "application/json" in resp.headers.get("Content-Type", "")
                _assert_health_payload(await resp.json())


async def test_root_serves_spa_html_or_health_json():
    port = _free_port()
    async with running_server(port) as runner:
        bound = _bound_port(runner)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{bound}/") as resp:
                assert resp.status == 200
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    _assert_health_payload(await resp.json())
                else:
                    assert "text/html" in content_type
                    assert "<html" in (await resp.text()).lower()


async def test_head_health_returns_empty_body():
    port = _free_port()
    async with running_server(port) as runner:
        bound = _bound_port(runner)
        async with aiohttp.ClientSession() as session:
            async with session.head(f"http://127.0.0.1:{bound}/health") as resp:
                assert resp.status == 200
                assert "application/json" in resp.headers.get("Content-Type", "")
                assert len(await resp.read()) == 0


async def test_server_binds_the_configured_port():
    for _ in range(2):
        port = _free_port()
        async with running_server(port) as runner:
            assert _bound_port(runner) == port
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                    assert resp.status == 200
                    assert (await resp.json())["status"] == "ok"


async def test_health_under_concurrent_load():
    port = _free_port()
    async with running_server(port) as runner:
        bound = _bound_port(runner)
        async with aiohttp.ClientSession() as session:
            async def fetch():
                async with session.get(f"http://127.0.0.1:{bound}/health") as resp:
                    assert resp.status == 200
                    return (await resp.json())["status"]

            results = await asyncio.gather(*[fetch() for _ in range(50)])
    assert results == ["ok"] * 50


async def test_unknown_paths_return_404():
    port = _free_port()
    async with running_server(port) as runner:
        bound = _bound_port(runner)
        async with aiohttp.ClientSession() as session:
            for path in ("/nonexistent", "/status", "/.env"):
                async with session.get(f"http://127.0.0.1:{bound}{path}") as resp:
                    assert resp.status == 404, path
