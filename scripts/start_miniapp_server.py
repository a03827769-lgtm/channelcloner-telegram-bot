import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from run import start_health_server
from database.db_manager import db_manager

async def main():
    os.environ["PORT"] = "8080"
    await db_manager.init_db()
    runner = await start_health_server()
    print("🚀 ChannelCloner Mini App Server running at http://127.0.0.1:8080/app", flush=True)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
