import asyncio
import logging
import sys
import os
import signal
from aiohttp import web

# Ensure UTF-8 stdout encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# High performance C-based event loop on Linux in Docker
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from aiogram.types import BotCommand, BotCommandScopeDefault
from config.settings import settings
from database.db_manager import db_manager
from services.cloner_engine import cloner_engine
from services.telethon_listener import telethon_listener
from services.media_handler import media_handler
from services.subscription_watcher import SubscriptionWatcher
from services.drip_feed_queue import drip_feed_service
from bot.bot_instance import create_bot, create_dispatcher
from admin_bot.bot_instance import create_admin_bot, create_admin_dispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ChannelClonerApp")

async def start_health_server():
    """Lightweight 24/7 Keep-Alive HTTP healthcheck server for cloud PaaS (Koyeb, Render)"""
    app = web.Application()

    async def health_handler(request):
        telethon_ok = False
        try:
            telethon_ok = bool(telethon_listener.is_connected())
        except Exception:
            telethon_ok = False

        return web.json_response({
            "status": "ok",
            "bot": "running",
            "service": "telegram-channel-cloner",
            "telethon_connected": telethon_ok
        }, status=200)

    # Register routes for / and /health (aiohttp add_get automatically registers HEAD handler)
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🚀 Keep-Alive HTTP Healthcheck server running on 0.0.0.0:{port} (/ and /health)")
    return runner

async def setup_bot_commands(bot, admin_bot=None):
    """Sets strictly the top 3 essential commands in Telegram menu for public and admin bots"""
    # 1. Top 3 commands for public client bot
    user_commands = [
        BotCommand(command="start", description="🏠 Bosh menyu"),
        BotCommand(command="cloner", description="🔄 Kanallar"),
        BotCommand(command="help", description="📚 Qo'llanma")
    ]
    try:
        await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
        logger.info("Top 3 public bot commands registered successfully.")
    except Exception as e:
        logger.warning(f"Could not register public bot commands: {e}")

    # 2. Top 3 commands for dedicated admin bot
    if admin_bot:
        admin_commands = [
            BotCommand(command="start", description="👑 Admin paneli"),
            BotCommand(command="status", description="📊 Tizim holati"),
            BotCommand(command="admin", description="⚙️ Boshqaruv markazi")
        ]
        try:
            await admin_bot.set_my_commands(admin_commands, scope=BotCommandScopeDefault())
            logger.info("Top 3 admin bot commands registered successfully.")
        except Exception as e:
            logger.debug(f"Could not register admin bot commands: {e}")

async def main():
    logger.info("🚀 Starting 100k High-Concurrency Telegram Channel Cloner system...")

    if not settings.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN is missing! Please configure .env or setup_wizard.py")
        print("\n" + "=" * 60)
        print("⚠️  BOT_TOKEN topilmadi!")
        print("Iltimos, .env faylini to'ldiring.")
        print("=" * 60 + "\n")
        return

    # 1. Initialize SQLite Database with WAL and in-memory caches
    logger.info("Initializing SQLite database with WAL & high-load memory mapping...")
    await db_manager.init_db()

    # 2. Create Public Bot & Dispatcher
    bot = create_bot()
    dp = create_dispatcher()
    cloner_engine.set_bot(bot)

    # 3. Create Dedicated Admin Bot & Dispatcher if configured
    admin_bot = None
    admin_dp = None
    if settings.ADMIN_BOT_TOKEN:
        admin_bot = create_admin_bot()
        admin_dp = create_admin_dispatcher()
        logger.info("Dedicated Admin Bot instance initialized.")

    # 4. Register Telegram Commands Menu
    await setup_bot_commands(bot, admin_bot)

    # 5. Start Media Garbage Collector, Subscription Trial Watcher & Drip Feed Worker
    media_handler.start_background_cleanup(interval=300, max_age=300)
    sub_watcher = SubscriptionWatcher(bot)
    sub_watcher.start()
    asyncio.create_task(drip_feed_service.start_worker(bot, cloner_engine))

    # 6. Start Telethon MTProto Listener
    if telethon_listener.is_configured():
        logger.info("Starting Telethon MTProto Listener...")
        await telethon_listener.start()
    else:
        logger.warning("Telethon credentials not configured. MTProto listener paused.")

    # 7. Start Keep-Alive Healthcheck Server (PaaS Never-Sleep & Cloud Monitoring)
    http_runner = None
    try:
        http_runner = await start_health_server()
    except Exception as e:
        logger.warning(f"Could not start HTTP health server on port {os.getenv('PORT', 8080)}: {e}")

    # 8. Start Concurrent Polling for Public Bot and Admin Bot
    logger.info("Starting Aiogram 3 Concurrent Bot Polling...")
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_exit_signal(sig_num):
        sig_name = signal.Signals(sig_num).name if hasattr(signal, "Signals") else str(sig_num)
        logger.info(f"Received signal {sig_name}. Initiating graceful shutdown...")
        stop_event.set()

    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig: _handle_exit_signal(s))
            except (NotImplementedError, RuntimeError):
                pass

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        bot_user = await bot.get_me()
        logger.info(f"Public Client Bot started as @{bot_user.username} (ID: {bot_user.id})")

        polling_coroutines = [dp.start_polling(bot, handle_in_background=True)]

        if admin_bot and admin_dp:
            await admin_bot.delete_webhook(drop_pending_updates=True)
            admin_user = await admin_bot.get_me()
            logger.info(f"Dedicated Admin Bot started as @{admin_user.username} (ID: {admin_user.id})")
            polling_coroutines.append(admin_dp.start_polling(admin_bot, handle_in_background=True))

        polling_task = asyncio.create_task(asyncio.gather(*polling_coroutines))

        async def _signal_watcher():
            await stop_event.wait()
            polling_task.cancel()

        watcher_task = None
        if sys.platform != "win32":
            watcher_task = asyncio.create_task(_signal_watcher())

        try:
            await polling_task
        except asyncio.CancelledError:
            logger.info("Polling tasks stopped by shutdown signal.")
        finally:
            if watcher_task and not watcher_task.done():
                watcher_task.cancel()

    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down bot...")
    finally:
        logger.info("Cleaning up resources...")
        if http_runner:
            try:
                await http_runner.cleanup()
            except Exception as e:
                logger.debug(f"HTTP runner cleanup: {e}")
        try:
            sub_watcher.stop()
        except Exception:
            pass
        try:
            await telethon_listener.stop()
        except Exception:
            pass
        try:
            await bot.session.close()
        except Exception:
            pass
        if admin_bot:
            try:
                await admin_bot.session.close()
            except Exception:
                pass
        logger.info("Telegram Channel Cloner shut down cleanly.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application terminated.")
        sys.exit(0)
