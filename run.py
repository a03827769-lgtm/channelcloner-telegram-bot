import asyncio
import logging
import logging.handlers
import sys
import os
import signal

# Strip invalid/broken proxy configurations injected by container environments
for _p_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    _val = os.environ.get(_p_var, "")
    if _val and ("8888" in _val or "192.168.65.254" in _val or "dozzle" in _val.lower() or not _val.startswith("http")):
        os.environ.pop(_p_var, None)

from aiohttp import web

# Optional high-performance event loop for Linux/Cloud PaaS
if sys.platform != "win32":
    try:
        import uvloop
        uvloop.install()
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

# Ensure UTF-8 stdout encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

from aiogram.types import BotCommand, BotCommandScopeDefault
from config.settings import settings
from database.db_manager import db_manager
from services.cloner_engine import cloner_engine
from services.telethon_listener import telethon_listener
from services.media_handler import media_handler
from services.subscription_watcher import SubscriptionWatcher
from services.drip_feed_queue import drip_feed_service
from services.memory_supervisor import system_supervisor
from services.cache_manager import cache_manager
from bot.bot_instance import create_bot, create_dispatcher
from admin_bot.bot_instance import create_admin_bot, create_admin_dispatcher
from services.log_viewer import log_viewer

class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler that gracefully recovers from Windows/Docker volume file locks."""
    def doRollover(self):
        try:
            super().doRollover()
        except (PermissionError, OSError):
            try:
                if self.stream:
                    self.stream.close()
                    self.stream = None
                with open(self.baseFilename, 'w', encoding=self.encoding) as f:
                    f.truncate(0)
                if not self.delay:
                    self.stream = self._open()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

log_handlers = [logging.StreamHandler(sys.stderr)]
try:
    os.makedirs("data", exist_ok=True)
    log_handlers.append(
        SafeRotatingFileHandler(
            "data/app.log", maxBytes=15 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    )
except Exception:
    logger.debug("Ignored exception", exc_info=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=log_handlers,
    force=True
)
if log_viewer not in logging.getLogger().handlers:
    logging.getLogger().addHandler(log_viewer)
logger = logging.getLogger("ChannelClonerApp")

async def start_health_server(bot=None):
    """Keep-Alive HTTP Healthcheck Server for Docker container and host monitoring"""
    app = web.Application()
    try:
        from aiohttp.web import AppKey
        bot_key = AppKey("bot")
        app[bot_key] = bot
    except Exception:
        app['bot'] = bot

    async def health_handler(request):
        telethon_connected = False
        try:
            from services.telethon_listener import telethon_listener
            if telethon_listener:
                if hasattr(telethon_listener, "is_connected") and callable(telethon_listener.is_connected):
                    telethon_connected = bool(telethon_listener.is_connected())
                elif telethon_listener.client:
                    telethon_connected = bool(telethon_listener.client.is_connected())
        except Exception:
            telethon_connected = False

        return web.json_response({
            "status": "ok",
            "bot": "running",
            "service": "telegram-channel-cloner",
            "telethon_connected": telethon_connected
        }, status=200)

    async def favicon_handler(request):
        return web.Response(status=204)

    # Register keep-alive routes
    app.router.add_get("/health", health_handler)
    app.router.add_get("/", health_handler)
    app.router.add_get("/favicon.ico", favicon_handler)

    # Register Telegram Mini App REST API routes
    try:
        from services.api_routes import register_api_routes
        register_api_routes(app)
        logger.info("📱 Telegram Mini App REST API routes registered on /api/*")
    except Exception as e_api:
        logger.warning(f"Could not register Mini App API routes: {e_api}")

    # Register Mini App static frontend distribution
    webapp_dist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "dist")
    if os.path.exists(webapp_dist):
        async def app_index_handler(request):
            index_file = os.path.join(webapp_dist, "index.html")
            if os.path.exists(index_file):
                return web.FileResponse(index_file)
            return web.Response(text="Mini App build not found", status=404)

        app.router.add_get("/app", app_index_handler)
        app.router.add_get("/app/{tail:.*}", app_index_handler)
        assets_dir = os.path.join(webapp_dist, "assets")
        if os.path.exists(assets_dir):
            app.router.add_static("/app/assets", assets_dir)
            app.router.add_static("/assets", assets_dir)

    # Register server metadata dictionary before runner.setup()
    server_meta = {"bound_port": 8080}
    try:
        from aiohttp.web import AppKey
        meta_key = AppKey("server_meta", dict)
        app[meta_key] = server_meta
    except Exception:
        try:
            app['server_meta'] = server_meta
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    runner = web.AppRunner(app)
    await runner.setup()
    port_env = os.getenv("PORT", "8080")
    try:
        port = int(port_env)
    except (ValueError, TypeError):
        port = 8080
    bound_port = port
    try:
        site = web.TCPSite(runner, "0.0.0.0", port, reuse_address=True)
        await site.start()
        logger.info(f"🚀 Keep-Alive HTTP health server running on 0.0.0.0:{port} (/health, /)")
    except (OSError, PermissionError) as bind_err:
        logger.warning(f"⚠️ Could not bind health server on 0.0.0.0:{port} ({bind_err}). Trying fallback ports...")
        site = None
        for fb_port in [8000, 8091, 8092, 8095, 8888, 9080, 0]:
            if fb_port == port:
                continue
            try:
                site = web.TCPSite(runner, "0.0.0.0", fb_port, reuse_address=True)
                await site.start()
                if hasattr(site, "_server") and site._server and hasattr(site._server, "sockets") and site._server.sockets:
                    bound_port = site._server.sockets[0].getsockname()[1]
                else:
                    bound_port = fb_port
                logger.info(f"🚀 Keep-Alive HTTP health server running on fallback 0.0.0.0:{bound_port} (/health, /)")
                break
            except (OSError, PermissionError):
                continue
        if site is None:
            logger.error(f"❌ Failed to bind HTTP health server on any port. Continuing bot execution without HTTP server.")
    server_meta["bound_port"] = bound_port
    return runner

async def setup_bot_commands(bot, admin_bot=None):
    """Sets essential commands in Telegram menu and resets chat Menu Button to default"""
    user_commands = [
        BotCommand(command="start", description="🏠 Bosh menyu"),
        BotCommand(command="app", description="📱 Mini Appni ochish"),
        BotCommand(command="cloner", description="🔄 Kanallar"),
        BotCommand(command="story", description="🎬 Istoriya Kloner (VIP)"),
        BotCommand(command="help", description="📚 Qo'llanma")
    ]
    try:
        await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
        logger.info("Public bot commands registered successfully.")
        
        # Configure chat Menu Button to WebApp Mini App if live HTTPS URL is available
        try:
            from aiogram.types import MenuButtonWebApp, MenuButtonDefault, WebAppInfo
            from bot.keyboards.inline_buttons import get_active_webapp_url
            active_url = get_active_webapp_url()
            if active_url and active_url.startswith("https://"):
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="📱 Mini App", web_app=WebAppInfo(url=active_url)))
                logger.info(f"Telegram Chat Menu Button set to WebApp: {active_url}")
            else:
                await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
                logger.info("Telegram Chat Menu Button set to default commands menu.")
        except Exception as e_btn:
            logger.warning(f"Could not set chat menu button: {e_btn}")
    except Exception as e:
        logger.warning(f"Could not register public bot commands: {e}")

    # 2. Commands and Menu Button for dedicated admin bot
    if admin_bot:
        admin_commands = [
            BotCommand(command="start", description="👑 Admin paneli"),
            BotCommand(command="status", description="📊 Tizim holati"),
            BotCommand(command="admin", description="⚙️ Boshqaruv markazi")
        ]
        try:
            await admin_bot.set_my_commands(admin_commands, scope=BotCommandScopeDefault())
            logger.info("Admin bot commands registered successfully.")
            try:
                from aiogram.types import MenuButtonDefault
                await admin_bot.set_chat_menu_button(menu_button=MenuButtonDefault())
                logger.info("Admin Bot Chat Menu Button reset to default commands menu.")
            except Exception as e_abtn:
                logger.warning(f"Could not reset admin chat menu button: {e_abtn}")
        except Exception as e:
            logger.debug(f"Could not register admin bot commands: {e}")

_pid_fd = None

def acquire_pid_lock(pid_file: str = "app.pid") -> bool:
    global _pid_fd
    try:
        if os.name != "nt":
            import fcntl
            _pid_fd = open(pid_file, "a+")
            try:
                fcntl.flock(_pid_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                _pid_fd.seek(0)
                _pid_fd.truncate()
                _pid_fd.write(f"{os.getpid()}\n")
                _pid_fd.flush()
                return True
            except (BlockingIOError, IOError):
                return False
        else:
            if os.path.exists(pid_file):
                try:
                    with open(pid_file, "r") as f:
                        old_pid = int(f.read().strip())
                    if old_pid == os.getpid():
                        return True
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    SYNCHRONIZE = 0x00100000
                    process = kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)
                    if process:
                        kernel32.CloseHandle(process)
                        try:
                            import psutil
                            if psutil.pid_exists(old_pid):
                                proc = psutil.Process(old_pid)
                                p_name = proc.name().lower()
                                if "python" in p_name:
                                    return False
                        except Exception:
                            # If psutil throws AccessDenied/NoSuchProcess, the PID was recycled by system
                            pass
                except (ValueError, OSError):
                    pass
            with open(pid_file, "w") as f:
                f.write(str(os.getpid()))
            return True
    except Exception:
        return True

def release_pid_lock(pid_file: str = "app.pid"):
    global _pid_fd
    try:
        if _pid_fd is not None:
            if os.name != "nt":
                import fcntl
                try:
                    fcntl.flock(_pid_fd.fileno(), fcntl.LOCK_UN)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            try:
                _pid_fd.close()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            _pid_fd = None
        if os.path.exists(pid_file):
            with open(pid_file, "r") as f:
                content = f.read().strip()
            if content == str(os.getpid()):
                os.remove(pid_file)
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

async def main():
    logger.info("🚀 Starting 100k High-Concurrency Telegram Channel Cloner system...")

    if not acquire_pid_lock():
        logger.warning("Another instance of Channel Cloner is already running (PID lock active). Exiting.")
        print("\n⚠️ Boshqa bot nusxasi allaqachon ishlayapti (app.pid faol)!\n")
        return

    if not settings.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN is missing! Please configure .env or setup_wizard.py")
        print("\n" + "=" * 60)
        print("⚠️  BOT_TOKEN topilmadi!")
        print("Iltimos, .env faylini to'ldiring.")
        print("=" * 60 + "\n")
        release_pid_lock()
        return

    # 1. Create Public Bot & Dispatcher
    bot = create_bot()
    dp = create_dispatcher()
    cloner_engine.set_bot(bot)

    # 2. Start Keep-Alive Healthcheck & Mini App Server immediately
    http_runner = None
    _background_tasks = set()
    sub_watcher = None  # initialized early to avoid UnboundLocalError in finally block
    try:
        http_runner = await start_health_server(bot=bot)
    except Exception as e:
        logger.warning(f"Could not start HTTP health server on port {os.getenv('PORT', 8080)}: {e}")

    # 3. Initialize SQLite Database with WAL and in-memory caches
    logger.info("Initializing SQLite database with WAL & high-load memory mapping...")
    await db_manager.init_db()

    # 4. Create Dedicated Admin Bot & Dispatcher if configured
    admin_bot = None
    admin_dp = None
    if settings.ADMIN_BOT_TOKEN and settings.ADMIN_BOT_TOKEN != settings.BOT_TOKEN:
        admin_bot = create_admin_bot()
        admin_dp = create_admin_dispatcher()
        logger.info("Dedicated Admin Bot instance initialized.")
    elif settings.ADMIN_BOT_TOKEN == settings.BOT_TOKEN:
        logger.warning("ADMIN_BOT_TOKEN matches BOT_TOKEN! Polling same token twice skipped to prevent conflict.")

    # 5. Register Telegram Commands Menu
    await setup_bot_commands(bot, admin_bot)

    # 6. Start Media Garbage Collector, System Supervisor, Subscription Watcher & Cache Pruner
    cache_manager.start_pruner()
    media_handler.start_background_cleanup(interval=300, max_age=300)
    system_supervisor.start()
    sub_watcher = SubscriptionWatcher(bot)
    sub_watcher.start()

    async def _periodic_db_cleanup():
        while True:
            try:
                await db_manager.clean_old_cloned_messages(days=180)
                # Prune stale rate limiter channel locks (channels idle >1h)
                from services.rate_limiter import rate_limiter
                await rate_limiter.prune_stale_locks(max_idle_seconds=3600.0)
            except asyncio.CancelledError:
                break
            except Exception as e_clean:
                logger.error(f"Error during periodic message cleanup: {e_clean}")
            try:
                await asyncio.sleep(86400)
            except asyncio.CancelledError:
                break

    clean_task = asyncio.create_task(_periodic_db_cleanup())
    _background_tasks.add(clean_task)
    clean_task.add_done_callback(_background_tasks.discard)

    async def _periodic_wal_checkpoint():
        """Periodically checkpoint the SQLite WAL file to prevent unbounded growth"""
        while True:
            try:
                await asyncio.sleep(21600)  # every 6 hours
                await db_manager.checkpoint()
                logger.debug("SQLite WAL checkpoint completed successfully.")
            except asyncio.CancelledError:
                break
            except Exception as e_wal:
                logger.warning(f"WAL checkpoint error: {e_wal}")

    wal_task = asyncio.create_task(_periodic_wal_checkpoint())
    _background_tasks.add(wal_task)
    wal_task.add_done_callback(_background_tasks.discard)

    from services.command_poller import CommandPoller
    cmd_poller = CommandPoller(bot)
    cmd_poller.start()
    _background_tasks.add(cmd_poller._task)
    cmd_poller._task.add_done_callback(_background_tasks.discard)

    from services.cloudflare_monitor import CloudflareMonitor
    cf_monitor = CloudflareMonitor(bot)
    cf_monitor.start()
    _background_tasks.add(cf_monitor._task)
    cf_monitor._task.add_done_callback(_background_tasks.discard)

    drip_task = asyncio.create_task(drip_feed_service.start_worker(bot, cloner_engine))
    _background_tasks.add(drip_task)
    drip_task.add_done_callback(_background_tasks.discard)

    # 7. Start Telethon MTProto Listener in background
    if telethon_listener.is_configured():
        logger.info("Starting Telethon MTProto Listener in background...")
        telethon_task = asyncio.create_task(telethon_listener.start())
        _background_tasks.add(telethon_task)
        telethon_task.add_done_callback(_background_tasks.discard)
    else:
        logger.warning("Telethon credentials not configured. MTProto listener paused.")

    # 7.5. Start Luxury Real Estate Story Cloner Engine, Channel Watchdogs & Queue Worker
    from services.story_cloner_service import story_cloner_service
    from services.story_queue_service import story_queue_service
    story_cloner_service.set_bot(bot)
    story_monitor_task = asyncio.create_task(story_cloner_service.start_all_active_monitors())
    _background_tasks.add(story_monitor_task)
    story_monitor_task.add_done_callback(_background_tasks.discard)

    story_queue_task = asyncio.create_task(story_queue_service.start_worker(story_cloner_service))
    _background_tasks.add(story_queue_task)
    story_queue_task.add_done_callback(_background_tasks.discard)

    # 7.8. Start 24/7 Cloudflare Tunnel URL Synchronizer & Telegram Menu Button Watchdog
    from services.tunnel_sync_service import tunnel_sync_service
    tunnel_sync_task = asyncio.create_task(tunnel_sync_service.start_worker(bot))
    _background_tasks.add(tunnel_sync_task)
    tunnel_sync_task.add_done_callback(_background_tasks.discard)

    # 8. Start Concurrent Polling for Public Bot and Admin Bot
    logger.info("Starting Aiogram 3 Concurrent Bot Polling...")
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    should_reexec = False

    if getattr(settings, "AUTO_RELOAD", False):
        try:
            from services.code_reloader import code_reloader
            def _on_reloader_trigger(changed_list):
                nonlocal should_reexec
                should_reexec = True
                logger.info(f"🔄 [AUTO-RELOAD] Source changes detected: {changed_list}. Initiating hot restart...")
                stop_event.set()
            code_reloader.add_reload_callback(_on_reloader_trigger)
            code_reloader.start()
        except Exception as e_reload:
            logger.warning(f"Could not initialize in-process code reloader: {e_reload}")

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
    else:
        try:
            signal.signal(signal.SIGINT, lambda s, f: _handle_exit_signal(signal.SIGINT))
            signal.signal(signal.SIGTERM, lambda s, f: _handle_exit_signal(signal.SIGTERM))
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    drop_pending = getattr(settings, "DROP_PENDING_UPDATES", False)
    try:
        await bot.delete_webhook(drop_pending_updates=drop_pending)
        bot_user = await bot.get_me()
        logger.info(f"Public Client Bot started as @{bot_user.username} (ID: {bot_user.id})")

        polling_coroutines = [dp.start_polling(bot, handle_in_background=True)]

        if admin_bot and admin_dp:
            await admin_bot.delete_webhook(drop_pending_updates=drop_pending)
            admin_user = await admin_bot.get_me()
            logger.info(f"Dedicated Admin Bot started as @{admin_user.username} (ID: {admin_user.id})")
            polling_coroutines.append(admin_dp.start_polling(admin_bot, handle_in_background=True))

        async def _run_polling():
            await asyncio.gather(*polling_coroutines, return_exceptions=True)

        polling_task = asyncio.create_task(_run_polling())

        async def _signal_watcher():
            await stop_event.wait()
            try:
                await dp.stop_polling()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            if admin_dp:
                try:
                    await admin_dp.stop_polling()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            polling_task.cancel()

        watcher_task = asyncio.create_task(_signal_watcher())

        try:
            await polling_task
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("Polling tasks stopped by shutdown signal / KeyboardInterrupt.")
            stop_event.set()
            if not polling_task.done():
                polling_task.cancel()
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
            await cache_manager.stop_pruner()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        try:
            await media_handler.flush_pending_albums()
        except Exception as e:
            logger.debug(f"Media buffer flush on shutdown: {e}")
        try:
            await media_handler.stop_background_cleanup()
            cleaned = media_handler.purge_temp_media(max_age=3600)
            if cleaned:
                logger.info(f"Purged {cleaned} temporary files from media directory on shutdown.")
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        pending_bg = [t for t in _background_tasks if not t.done()]
        for bg_task in pending_bg:
            bg_task.cancel()
        if pending_bg:
            try:
                await asyncio.wait_for(asyncio.gather(*pending_bg, return_exceptions=True), timeout=5.0)
            except (asyncio.TimeoutError, Exception) as bg_err:
                logger.warning(f"Background tasks shutdown timed out: {bg_err}")
        try:
            system_supervisor.stop()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        try:
            sub_watcher.stop()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        try:
            from services.story_cloner_service import story_cloner_service
            from services.story_queue_service import story_queue_service
            story_queue_service.stop_worker()
            await story_cloner_service.stop_all()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        try:
            await telethon_listener.stop()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        try:
            await db_manager.close()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        try:
            if bot and getattr(bot, "session", None):
                await bot.session.close()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        if admin_bot and getattr(admin_bot, "session", None):
            try:
                await admin_bot.session.close()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        try:
            from admin_bot.handlers.user_management import close_cached_public_bot
            await close_cached_public_bot()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        if getattr(settings, "AUTO_RELOAD", False):
            try:
                from services.code_reloader import code_reloader
                code_reloader.stop()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        try:
            from services.story_renderer import shutdown_playwright_pool
            shutdown_playwright_pool(wait=False, cancel_futures=True)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        release_pid_lock()
        logger.info("Telegram Channel Cloner shut down cleanly.")
        return should_reexec

if __name__ == "__main__":
    while True:
        try:
            reexec = asyncio.run(main())
            if reexec:
                logger.info("🔄 [HOT-RELOAD] Re-executing application process with updated source...")
                import time
                time.sleep(0.5)
                if sys.platform != "win32":
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                else:
                    import subprocess
                    subprocess.Popen([sys.executable] + sys.argv)
                    sys.exit(0)
            break
        except (KeyboardInterrupt, SystemExit):
            logger.info("Application terminated.")
            sys.exit(0)
