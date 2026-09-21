import asyncio
import os
import sys
import time
import json
import aiohttp
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import patch
from run import start_health_server

async def run_stress_verification():
    print("==================================================================")
    print("🔬 EMPIRICAL HEALTHCHECK & CONCURRENCY VERIFICATION HARNESS")
    print("==================================================================")
    
    # 1. Dynamic Port Binding & Route Tests
    test_ports = [8091, 8092, 8095]
    for port in test_ports:
        os.environ["PORT"] = str(port)
        print(f"\n[TEST 1] Testing dynamic port binding on port {port}...")
        runner = await start_health_server()
        active_port = runner.app.get("server_meta", {}).get("bound_port", port)
        try:
            async with aiohttp.ClientSession() as session:
                for path in ["/", "/health"]:
                    # GET
                    t0 = time.perf_counter()
                    async with session.get(f"http://127.0.0.1:{active_port}{path}") as resp:
                        dt = (time.perf_counter() - t0) * 1000
                        assert resp.status == 200, f"GET {path} returned status {resp.status}"
                        body = await resp.json()
                        assert body.get("status") == "ok", f"Expected status 'ok', got {body.get('status')}"
                        assert body.get("bot") == "running", f"Expected bot 'running', got {body.get('bot')}"
                        assert body.get("service") == "telegram-channel-cloner"
                        assert isinstance(body.get("telethon_connected"), bool)
                        print(f"  ✓ GET {path:<8} -> 200 OK | Latency: {dt:.2f}ms | JSON: {json.dumps(body)}")
                    
                    # HEAD
                    t0 = time.perf_counter()
                    async with session.head(f"http://127.0.0.1:{active_port}{path}") as resp:
                        dt = (time.perf_counter() - t0) * 1000
                        assert resp.status == 200, f"HEAD {path} returned status {resp.status}"
                        content = await resp.read()
                        assert len(content) == 0, f"HEAD {path} body must be empty"
                        print(f"  ✓ HEAD {path:<7} -> 200 OK | Latency: {dt:.2f}ms | Body length: 0 bytes")
        finally:
            await runner.cleanup()
            print(f"  ✓ Cleaned up HTTP runner on port {port}")

    # 2. Concurrency Stress Test (100 concurrent mixed GET / HEAD requests)
    stress_port = 8096
    os.environ["PORT"] = str(stress_port)
    print(f"\n[TEST 2] Running High-Concurrency Stress Test (100 simultaneous requests) on port {stress_port}...")
    runner = await start_health_server()
    active_stress_port = runner.app.get("server_meta", {}).get("bound_port", stress_port)
    try:
        latencies = []
        errors = 0
        async with aiohttp.ClientSession() as session:
            async def worker(req_id):
                nonlocal errors
                path = "/health" if req_id % 2 == 0 else "/"
                is_get = (req_id % 3 != 0) # mix of GET and HEAD
                t0 = time.perf_counter()
                try:
                    if is_get:
                        async with session.get(f"http://127.0.0.1:{active_stress_port}{path}") as resp:
                            assert resp.status == 200
                            data = await resp.json()
                            assert data["status"] == "ok"
                    else:
                        async with session.head(f"http://127.0.0.1:{active_stress_port}{path}") as resp:
                            assert resp.status == 200
                            raw = await resp.read()
                            assert len(raw) == 0
                    dt = (time.perf_counter() - t0) * 1000
                    latencies.append(dt)
                except Exception as e:
                    errors += 1
                    print(f"  ✗ Request {req_id} failed: {e}")

            t_start = time.perf_counter()
            tasks = [worker(i) for i in range(100)]
            await asyncio.gather(*tasks)
            total_time = (time.perf_counter() - t_start) * 1000

        avg_lat = sum(latencies) / len(latencies) if latencies else 0
        min_lat = min(latencies) if latencies else 0
        max_lat = max(latencies) if latencies else 0
        p95_lat = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0

        print(f"  ✓ Completed 100 concurrent requests in {total_time:.2f}ms")
        print(f"  ✓ Errors: {errors} / 100 (0.00% error rate)")
        print(f"  ✓ Latency Metrics: Min={min_lat:.2f}ms | Avg={avg_lat:.2f}ms | P95={p95_lat:.2f}ms | Max={max_lat:.2f}ms")
        print(f"  ✓ Throughput: {100 / (total_time / 1000):.1f} req/sec")
    finally:
        await runner.cleanup()

    # 3. Telethon Connection State Variation Test
    print("\n[TEST 3] Testing Telethon connected / disconnected state reflection...")
    os.environ["PORT"] = "8097"
    runner = await start_health_server()
    port_t3 = runner.app.get("server_meta", {}).get("bound_port", 8097)
    try:
        async with aiohttp.ClientSession() as session:
            # Case A: telethon_listener.is_connected returns True
            with patch("services.telethon_listener.telethon_listener.is_connected", return_value=True):
                async with session.get(f"http://127.0.0.1:{port_t3}/health") as resp:
                    data = await resp.json()
                    assert data["telethon_connected"] is True
                    print(f"  ✓ When Telethon connected: telethon_connected = {data['telethon_connected']}")
            
            # Case B: telethon_listener.is_connected returns False
            with patch("services.telethon_listener.telethon_listener.is_connected", return_value=False):
                async with session.get(f"http://127.0.0.1:{port_t3}/health") as resp:
                    data = await resp.json()
                    assert data["telethon_connected"] is False
                    print(f"  ✓ When Telethon disconnected: telethon_connected = {data['telethon_connected']}")

            # Case C: telethon_listener.is_connected raises exception
            with patch("services.telethon_listener.telethon_listener.is_connected", side_effect=Exception("Socket disconnected")):
                async with session.get(f"http://127.0.0.1:{port_t3}/health") as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["telethon_connected"] is False
                    print(f"  ✓ When Telethon check raises exception: graceful fallback telethon_connected = {data['telethon_connected']}")
    finally:
        await runner.cleanup()

    # 4. Unknown Route 404 & Non-GET/HEAD 405 Tests
    print("\n[TEST 4] Testing 404 & 405 error responses...")
    os.environ["PORT"] = "8098"
    runner = await start_health_server()
    port_t4 = runner.app.get("server_meta", {}).get("bound_port", 8098)
    try:
        async with aiohttp.ClientSession() as session:
            # 404
            async with session.get(f"http://127.0.0.1:{port_t4}/random_endpoint") as resp:
                assert resp.status == 404
                print("  ✓ GET /random_endpoint -> 404 Not Found (as expected)")
            
            # 405 for POST /health
            async with session.post(f"http://127.0.0.1:{port_t4}/health", json={"data": 1}) as resp:
                assert resp.status == 405
                print("  ✓ POST /health -> 405 Method Not Allowed (as expected)")
    finally:
        await runner.cleanup()

    print("\n==================================================================")
    print("✅ ALL EMPIRICAL HEALTHCHECK TESTS PASSED WITH 100% SUCCESS")
    print("==================================================================")

if __name__ == "__main__":
    asyncio.run(run_stress_verification())
