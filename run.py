import asyncio
import logging
import logging.handlers
import sys
import os
import signal
import time
from typing import Any, Callable, List, Optional

logger = logging.getLogger("ChannelClonerApp")

def _disable_windows_power_throttling():
    """Opts this process out of Windows 11 EcoQoS/"efficiency mode". Windowless background processes
    (pythonw, scheduled/autostart launches) are otherwise CPU-throttled, especially on battery, which
    made cold starts take minutes and let the watchdog kill the bot before it became healthy."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _PowerThrottlingState(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

        execution_speed, ignore_timer_resolution = 0x1, 0x4
        state = _PowerThrottlingState(1, execution_speed | ignore_timer_resolution, 0)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state))
    except Exception:
        pass


_disable_windows_power_throttling()

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
        logger.debug("uvloop install skipped or not available", exc_info=True)

# Ensure UTF-8 stdout encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        logger.debug("stdout reconfigure skipped", exc_info=True)

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand, BotCommandScopeDefault
from config.settings import settings, PROJECT_ROOT
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
from services.log_viewer import log_viewer, install_redaction_filter

HEALTH_SERVICE_NAME = "telegram-channel-cloner"
PID_FILE = str(PROJECT_ROOT / "app.pid")
LOCK_FILE = str(PROJECT_ROOT / "app.lock")
MAINTENANCE_INITIAL_DELAY = 300.0     # keep the daily database housekeeping out of the cold start
MAINTENANCE_INTERVAL = 86400.0
WAL_CHECKPOINT_INTERVAL = 21600.0
BACKGROUND_STOP_TIMEOUT = 5.0


class RuntimeState:
    """Liveness/readiness facts reported by /health and /ready. None means "not managed by this process"
    (the HTTP server was started on its own, e.g. by tests or scripts) and never downgrades /health."""

    def __init__(self):
        self.started_at = time.time()
        self.db_ready: Optional[bool] = None
        self.polling_alive: Optional[bool] = None
        self.admin_polling_alive: Optional[bool] = None


runtime_state = RuntimeState()


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
                logger.debug("SafeRotatingFileHandler rollover fallback skipped", exc_info=True)

def build_log_handlers() -> List[logging.Handler]:
    """Console (+ rotating file unless CLONER_DISABLE_FILE_LOG=1) handlers, each with secret/phone redaction."""
    log_handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if os.getenv("CLONER_DISABLE_FILE_LOG") != "1":
        try:
            log_dir = PROJECT_ROOT / "data"
            os.makedirs(log_dir, exist_ok=True)
            log_handlers.append(
                SafeRotatingFileHandler(
                    str(log_dir / "app.log"), maxBytes=15 * 1024 * 1024, backupCount=3, encoding="utf-8"
                )
            )
        except Exception:
            logger.debug("Log file handler creation skipped", exc_info=True)
    for handler in log_handlers:
        install_redaction_filter(handler)
    return log_handlers

def configure_logging():
    """Configures console + rotating file logging. Called only when run as the application entrypoint,
    so importing this module (e.g. from tests) never attaches handlers to the production log file."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=build_log_handlers(),
        force=True
    )
    install_redaction_filter(log_viewer)
    if log_viewer not in logging.getLogger().handlers:
        logging.getLogger().addHandler(log_viewer)

def resolve_http_port() -> int:
    """Process environment wins (Docker/PaaS inject PORT); otherwise the value from .env via settings."""
    raw = os.getenv("PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning(f"Invalid PORT environment value {raw!r}; using configured port")
    return settings.PORT or 8080

def build_health_payload() -> dict:
    """/health body. "status" is "ok" while the database is open and the public bot is polling; the MTProto
    listener is reported separately because an account that is not logged in is a normal state (the Windows
    watchdog restarts the process whenever status != "ok")."""
    telethon_connected = False
    try:
        telethon_connected = bool(telethon_listener.is_connected())
    except Exception:
        telethon_connected = False
    db_ready = runtime_state.db_ready is not False
    polling_alive = runtime_state.polling_alive is not False
    return {
        "status": "ok" if (db_ready and polling_alive) else "degraded",
        "bot": "running" if polling_alive else "stopped",
        "service": HEALTH_SERVICE_NAME,
        "pid": os.getpid(),
        "uptime": int(time.time() - runtime_state.started_at),
        "db_ready": db_ready,
        "polling_alive": polling_alive,
        "admin_polling_alive": runtime_state.admin_polling_alive,
        "telethon_connected": telethon_connected,
    }

async def start_health_server(bot=None, publish_port_file: bool = False):
    """Keep-Alive HTTP Healthcheck Server for Docker container and host monitoring"""
    app = web.Application()
    try:
        from aiohttp.web import AppKey
        bot_key = AppKey("bot")
        app[bot_key] = bot
    except Exception:
        app['bot'] = bot

    async def health_handler(request):
        # Liveness: answers 200 as long as the event loop runs
        return web.json_response(build_health_payload(), status=200)

    async def ready_handler(request):
        # Readiness: database initialized and the public bot polling (the MTProto listener is optional)
        payload = build_health_payload()
        ready = runtime_state.db_ready is True and runtime_state.polling_alive is True
        payload["ready"] = ready
        return web.json_response(payload, status=200 if ready else 503)

    async def favicon_handler(request):
        return web.Response(status=204)

    # Register keep-alive routes
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)
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
            if request.query.get("format") == "json" or request.headers.get("User-Agent", "").startswith("Watchdog"):
                return await health_handler(request)
            index_file = os.path.join(webapp_dist, "index.html")
            if os.path.exists(index_file):
                return web.FileResponse(index_file)
            return web.Response(text="Mini App build not found", status=404)

        app.router.add_get("/", app_index_handler)
        app.router.add_get("/app", app_index_handler)
        app.router.add_get("/app/{tail:.*}", app_index_handler)
        assets_dir = os.path.join(webapp_dist, "assets")
        if os.path.exists(assets_dir):
            app.router.add_static("/app/assets", assets_dir)
            app.router.add_static("/assets", assets_dir)
    else:
        app.router.add_get("/", health_handler)

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
    port = resolve_http_port()
    # SO_REUSEADDR on Windows lets a second process bind an already-used port (silent port hijack),
    # so it is only enabled on POSIX where it merely allows fast restarts over TIME_WAIT sockets.
    reuse = os.name != "nt"
    bound_port = port
    site = None
    candidates = [port] + [p for p in (8000, 8091, 8092, 8095, 9080, 0) if p != port]
    for candidate in candidates:
        try:
            site = web.TCPSite(runner, "0.0.0.0", candidate, reuse_address=reuse)
            await site.start()
            server = getattr(site, "_server", None)
            sockets = getattr(server, "sockets", None) if server else None
            bound_port = sockets[0].getsockname()[1] if sockets else candidate
            if candidate != port:
                logger.warning(f"⚠️ Port {port} is busy; HTTP server fell back to 0.0.0.0:{bound_port}")
            logger.info(f"🚀 HTTP server (health, Mini App & API) running on 0.0.0.0:{bound_port}")
            break
        except (OSError, PermissionError) as bind_err:
            logger.warning(f"⚠️ Could not bind HTTP server on 0.0.0.0:{candidate} ({bind_err})")
            site = None
    if site is None:
        logger.error("❌ Failed to bind HTTP server on any port. Continuing bot execution without HTTP server.")
    elif publish_port_file:
        # The Windows keep-alive watchdog reads this file to probe the right port.
        try:
            logs_dir = PROJECT_ROOT / "logs"
            os.makedirs(logs_dir, exist_ok=True)
            with open(logs_dir / "health_port.txt", "w", encoding="utf-8") as pf:
                pf.write(str(bound_port))
        except OSError:
            logger.debug("Could not persist bound health port", exc_info=True)
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

# --- SINGLE-INSTANCE GUARD ---

_lock_handle = None

def _lock_file_handle(lock_file: str):
    """Opens `lock_file` and takes an exclusive, non-blocking OS lock on it (msvcrt on Windows, flock on
    POSIX). The OS releases the lock when the process exits, even on a crash or a hard kill, so a stale
    file never blocks a restart and a reused PID never matters. Returns the open handle, or None."""
    handle = open(lock_file, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle

def _unlock_file_handle(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        logger.debug("Instance lock release skipped", exc_info=True)
    finally:
        handle.close()

def acquire_pid_lock(pid_file: Optional[str] = None, lock_file: Optional[str] = None) -> bool:
    """Single-instance guard held for the process lifetime. Fails closed: when the lock cannot be taken for
    any reason the process must not start (two pollers on one token duplicate every post). The PID is also
    written to `pid_file` for the keep-alive watchdog."""
    global _lock_handle
    if _lock_handle is not None:
        return True
    pid_file = pid_file or PID_FILE
    lock_file = lock_file or LOCK_FILE
    try:
        handle = _lock_file_handle(lock_file)
    except OSError as e:
        logger.error(f"Cannot open the instance lock file {lock_file}: {e}. Refusing to start.")
        return False
    if handle is None:
        logger.error(f"Another Channel Cloner instance holds the instance lock ({lock_file}).")
        return False
    _lock_handle = handle
    try:
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        logger.warning(f"Could not write the PID file {pid_file}: {e}")
    return True

def release_pid_lock(pid_file: Optional[str] = None):
    """Removes our PID file, then releases the instance lock."""
    global _lock_handle
    pid_file = pid_file or PID_FILE
    try:
        if os.path.exists(pid_file):
            with open(pid_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content == str(os.getpid()):
                os.remove(pid_file)
    except Exception:
        logger.debug("PID file cleanup skipped", exc_info=True)
    handle, _lock_handle = _lock_handle, None
    if handle is not None:
        _unlock_file_handle(handle)

async def probe_running_instance(port: int, timeout: float = 3.0) -> Optional[dict]:
    """/health payload of ANOTHER Channel Cloner process answering on 127.0.0.1:<port> (e.g. a Docker container
    publishing the port next to a host process), else None. Payloads without a pid come from older versions
    and count as another instance."""
    import aiohttp
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout, trust_env=False) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("service") != HEALTH_SERVICE_NAME:
        return None
    if data.get("pid") == os.getpid():
        return None
    return data

# --- SIGNALS ---

def install_exit_signal_handlers(loop: asyncio.AbstractEventLoop, on_signal: Callable[[int], Any]) -> List[tuple]:
    """Routes SIGINT/SIGTERM (and SIGBREAK on Windows) to one graceful-shutdown callback. aiogram's own signal
    handling is disabled (handle_signals=False): every dispatcher would replace this process-wide handler and
    only stop itself. Returns what was installed, for remove_exit_signal_handlers()."""
    installed: List[tuple] = []
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    for sig in signals:
        if sys.platform != "win32":
            try:
                loop.add_signal_handler(sig, on_signal, sig)
                installed.append(("loop", sig, None))
                continue
            except (NotImplementedError, RuntimeError, ValueError):
                logger.debug("Loop signal handler not supported; falling back to signal.signal", exc_info=True)
        try:
            previous = signal.signal(sig, lambda s, _f: loop.call_soon_threadsafe(on_signal, s))
            installed.append(("signal", sig, previous))
        except (ValueError, OSError, RuntimeError):
            logger.debug(f"Could not install a handler for {sig!r}", exc_info=True)
    return installed

def remove_exit_signal_handlers(loop: asyncio.AbstractEventLoop, installed: List[tuple]) -> None:
    for kind, sig, previous in installed:
        try:
            if kind == "loop":
                loop.remove_signal_handler(sig)
            else:
                signal.signal(sig, previous if previous is not None else signal.SIG_DFL)
        except Exception:
            logger.debug("Signal handler removal skipped", exc_info=True)

# --- BOT POLLING ---

# aiogram 3.x start_polling() options: aiogram must not install its own SIGINT/SIGTERM handlers (each dispatcher
# would replace the process-wide handler and stop only itself), and bot sessions are closed by run.py after the
# shutdown sequence, not by the dispatcher while other services still use the bot.
POLLING_KWARGS = {"handle_signals": False, "close_bot_session": False}

async def prepare_bot_for_polling(bot, drop_pending: bool, stop_event: asyncio.Event, label: str = "Public bot",
                                  initial_delay: float = 2.0, max_delay: float = 60.0):
    """Removes any webhook and loads the bot identity (cached by aiogram for its polling loop), retrying
    network failures with exponential backoff instead of crashing the process. Returns the bot user, or None
    when a shutdown was requested meanwhile. A rejected token raises TelegramUnauthorizedError."""
    delay = initial_delay
    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        try:
            await bot.delete_webhook(drop_pending_updates=drop_pending)
            return await bot.me()
        except TelegramUnauthorizedError:
            raise
        except Exception as e:
            logger.warning(f"{label}: Telegram Bot API not reachable ({type(e).__name__}: {e}); retry #{attempt} in {delay:.0f}s")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(max_delay, delay * 2)
    return None

async def run_admin_polling(admin_bot, admin_dp, stop_event: asyncio.Event, drop_pending: bool, initial_delay: float = 5.0):
    """Runs the dedicated admin bot. It is optional: startup or polling failures are logged and retried with
    backoff and never take the public bot or the MTProto listener down."""
    delay = initial_delay
    while not stop_event.is_set():
        try:
            admin_user = await prepare_bot_for_polling(
                admin_bot, drop_pending, stop_event, label="Admin bot", initial_delay=initial_delay
            )
            if admin_user is None:
                return
            logger.info(f"Dedicated Admin Bot started as @{admin_user.username} (ID: {admin_user.id})")
            delay = initial_delay
            runtime_state.admin_polling_alive = True
            await admin_dp.start_polling(admin_bot, **POLLING_KWARGS)
            if stop_event.is_set():
                return
            logger.error("Admin bot polling stopped unexpectedly; restarting it.")
        except asyncio.CancelledError:
            raise
        except TelegramUnauthorizedError:
            logger.error("ADMIN_BOT_TOKEN was rejected by Telegram (revoked?). The admin bot stays offline until the token is fixed.")
            return
        except Exception as e:
            logger.error(f"Admin bot failed: {e!r}. Retrying in {delay:.0f}s (public bot and listener keep running).")
        finally:
            runtime_state.admin_polling_alive = False
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(300.0, delay * 2)

async def _stop_polling(dispatchers: List[Any], tasks: List[Optional[asyncio.Task]], timeout: float = 10.0):
    """Stops aiogram polling gracefully and waits (bounded) for in-flight update handlers."""
    for dispatcher in dispatchers:
        if dispatcher is None:
            continue
        try:
            await asyncio.wait_for(dispatcher.stop_polling(), timeout=timeout)
        except RuntimeError:
            pass  # polling was not running
        except Exception:
            logger.debug("stop_polling did not complete cleanly", exc_info=True)
    running = [t for t in tasks if t is not None and not t.done()]
    if running:
        _done, pending = await asyncio.wait(running, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2.0)
    for task in tasks:
        if task is not None and task.done() and not task.cancelled():
            task.exception()  # retrieved: never "Task exception was never retrieved"
    handler_tasks = [
        t for dispatcher in dispatchers if dispatcher is not None
        for t in list(getattr(dispatcher, "_handle_update_tasks", ()) or ()) if not t.done()
    ]
    if handler_tasks:
        await asyncio.wait(handler_tasks, timeout=5.0)

# --- APPLICATION ---

async def run_application() -> bool:
    """Runs the whole system until a shutdown is requested. Returns True when a hot re-exec was requested."""
    if not settings.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN is missing! Please configure .env or setup_wizard.py")
        logger.error("⚠️ BOT_TOKEN topilmadi! Iltimos, .env faylini to'ldiring.")
        return False

    port = resolve_http_port()
    other_instance = await probe_running_instance(port)
    if other_instance:
        logger.error(
            f"Another Channel Cloner instance (pid {other_instance.get('pid', '?')}) already answers on "
            f"127.0.0.1:{port}/health. Refusing to start a second poller for the same bot token."
        )
        logger.error("⚠️ Boshqa bot nusxasi allaqachon ishlayapti (ikkinchi nusxa ishga tushirilmadi)!")
        return False

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    should_reexec = False

    def _on_exit_signal(sig_num):
        try:
            sig_name = signal.Signals(sig_num).name
        except Exception:
            sig_name = str(sig_num)
        if stop_event.is_set():
            logger.warning(f"Received signal {sig_name} again; shutdown is already in progress.")
            return
        logger.info(f"Received signal {sig_name}. Initiating graceful shutdown...")
        stop_event.set()

    installed_signals = install_exit_signal_handlers(loop, _on_exit_signal)

    # 1. Create Public Bot & Dispatcher
    bot = create_bot()
    dp = create_dispatcher()
    cloner_engine.set_bot(bot)

    admin_bot = None
    admin_dp = None
    http_runner = None
    sub_watcher = None
    polling_task: Optional[asyncio.Task] = None
    admin_task: Optional[asyncio.Task] = None
    _background_tasks = set()

    def _track_background_task(task: asyncio.Task, name: str):
        """Keeps a strong reference and surfaces crashes that would otherwise vanish silently."""
        _background_tasks.add(task)

        def _on_done(t: asyncio.Task):
            _background_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error(f"Background service '{name}' crashed: {t.exception()!r}", exc_info=t.exception())

        task.add_done_callback(_on_done)

    try:
        # 2. Initialize SQLite Database with WAL and in-memory caches
        logger.info("Initializing SQLite database with WAL & high-load memory mapping...")
        await db_manager.init_db()
        runtime_state.db_ready = True
        runtime_state.polling_alive = False

        # 3. Health / Mini App HTTP server right after the database is ready (API routes need it)
        try:
            http_runner = await start_health_server(bot=bot, publish_port_file=True)
        except Exception as e:
            logger.warning(f"Could not start HTTP health server on port {port}: {e}")

        # 4. Create Dedicated Admin Bot & Dispatcher if configured
        if settings.ADMIN_BOT_TOKEN and settings.ADMIN_BOT_TOKEN != settings.BOT_TOKEN:
            admin_bot = create_admin_bot()
            admin_dp = create_admin_dispatcher()
            logger.info("Dedicated Admin Bot instance initialized.")
        elif settings.ADMIN_BOT_TOKEN == settings.BOT_TOKEN:
            logger.warning("ADMIN_BOT_TOKEN matches BOT_TOKEN! Polling same token twice skipped to prevent conflict.")
        telethon_listener.set_alert_bot(admin_bot or bot)

        # 5. Register Telegram Commands Menu (network calls: never delays polling)
        _track_background_task(asyncio.create_task(setup_bot_commands(bot, admin_bot)), "bot_commands")

        # 6. Start Media Garbage Collector, System Supervisor, Subscription Watcher & Cache Pruner
        cache_manager.start_pruner()
        media_handler.start_background_cleanup(interval=300, max_age=3600)
        system_supervisor.start()
        sub_watcher = SubscriptionWatcher(bot)
        sub_watcher.start()

        async def _periodic_db_maintenance():
            """Daily database housekeeping (retention, stale rows, WAL checkpoint) and rate-limiter pruning"""
            await asyncio.sleep(MAINTENANCE_INITIAL_DELAY)
            while True:
                try:
                    stats = await db_manager.run_maintenance()
                    logger.info(f"Daily database maintenance finished: {stats}")
                except asyncio.CancelledError:
                    raise
                except Exception as e_clean:
                    logger.error(f"Error during daily database maintenance: {e_clean!r}")
                try:
                    # Prune stale rate limiter channel locks (channels idle >1h)
                    from services.rate_limiter import rate_limiter
                    await rate_limiter.prune_stale_locks(max_idle_seconds=3600.0)
                except asyncio.CancelledError:
                    raise
                except Exception as e_prune:
                    logger.warning(f"Rate limiter pruning failed: {e_prune!r}")
                await asyncio.sleep(MAINTENANCE_INTERVAL)

        _track_background_task(asyncio.create_task(_periodic_db_maintenance()), "db_maintenance")

        async def _periodic_wal_checkpoint():
            """Periodically checkpoint the SQLite WAL file to prevent unbounded growth"""
            while True:
                await asyncio.sleep(WAL_CHECKPOINT_INTERVAL)
                try:
                    await db_manager.checkpoint()
                    logger.debug("SQLite WAL checkpoint completed successfully.")
                except asyncio.CancelledError:
                    raise
                except Exception as e_wal:
                    logger.warning(f"WAL checkpoint error: {e_wal}")

        _track_background_task(asyncio.create_task(_periodic_wal_checkpoint()), "wal")

        _track_background_task(asyncio.create_task(drip_feed_service.start_worker(bot, cloner_engine)), "drip")

        # 7. Start Telethon MTProto Listener in background
        if telethon_listener.is_configured():
            logger.info("Starting Telethon MTProto Listener in background...")
            _track_background_task(asyncio.create_task(telethon_listener.start()), "telethon")
        else:
            logger.warning("Telethon credentials not configured. MTProto listener paused.")

        # 7.5. Start Luxury Real Estate Story Cloner Engine, Channel Watchdogs & Queue Worker
        from services.story_cloner_service import story_cloner_service
        from services.story_queue_service import story_queue_service
        story_cloner_service.set_bot(bot)
        _track_background_task(asyncio.create_task(story_cloner_service.start_all_active_monitors()), "story_monitor")
        _track_background_task(asyncio.create_task(story_queue_service.start_worker(story_cloner_service)), "story_queue")

        # 7.8. Start 24/7 Cloudflare Tunnel URL Synchronizer & Telegram Menu Button Watchdog
        from services.tunnel_sync_service import tunnel_sync_service
        _track_background_task(asyncio.create_task(tunnel_sync_service.start_worker(bot)), "tunnel_sync")

        # 7.9. Start 24/7 Supplier API & Store Synchronization Worker
        from services.supplier_service import supplier_service
        _track_background_task(asyncio.create_task(supplier_service.start_worker()), "supplier_sync")

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

        # 8. Start polling for the Public Bot (critical) and the Admin Bot (optional)
        logger.info("Starting Aiogram 3 Concurrent Bot Polling...")
        drop_pending = getattr(settings, "DROP_PENDING_UPDATES", False)
        try:
            bot_user = await prepare_bot_for_polling(bot, drop_pending, stop_event)
        except TelegramUnauthorizedError as e_auth:
            logger.critical(f"BOT_TOKEN was rejected by Telegram ({e_auth}). Fix the token in .env; shutting down.")
            bot_user = None
        if bot_user is not None:
            logger.info(f"Public Client Bot started as @{bot_user.username} (ID: {bot_user.id})")
            polling_task = asyncio.create_task(dp.start_polling(bot, **POLLING_KWARGS))
            runtime_state.polling_alive = True
            if admin_bot and admin_dp:
                admin_task = asyncio.create_task(run_admin_polling(admin_bot, admin_dp, stop_event, drop_pending))

            stop_waiter = asyncio.create_task(stop_event.wait())
            try:
                await asyncio.wait({polling_task, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop_waiter.cancel()
            if polling_task.done() and not stop_event.is_set():
                runtime_state.polling_alive = False
                crash = None if polling_task.cancelled() else polling_task.exception()
                logger.critical(
                    f"Public bot polling stopped unexpectedly ({crash!r}). "
                    "Shutting down so the watchdog restarts the process."
                )

    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down bot...")
    finally:
        stop_event.set()
        runtime_state.polling_alive = False
        logger.info("Cleaning up resources...")

        # 1. Stop taking new work: Telegram updates and HTTP API requests
        try:
            await _stop_polling([dp, admin_dp], [polling_task, admin_task])
        except Exception:
            logger.debug("Polling shutdown incomplete", exc_info=True)
        if http_runner:
            try:
                await http_runner.cleanup()
            except Exception as e:
                logger.debug(f"HTTP runner cleanup: {e}")

        # 2. MTProto listener first among the workers: it stops ingesting, delivers buffered albums and queued
        #    posts (bounded) and awaits its cancelled clone tasks before disconnecting
        try:
            await telethon_listener.stop()
        except Exception:
            logger.warning("Telethon listener shutdown incomplete", exc_info=True)

        # 3. Story engine
        try:
            from services.story_cloner_service import story_cloner_service
            from services.story_queue_service import story_queue_service
            story_queue_service.stop_worker()
            await story_cloner_service.stop_all()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

        # 4. Remaining background services (cancelled and awaited, bounded)
        pending_bg = [t for t in _background_tasks if not t.done()]
        for bg_task in pending_bg:
            bg_task.cancel()
        if pending_bg:
            _done, still_running = await asyncio.wait(pending_bg, timeout=BACKGROUND_STOP_TIMEOUT)
            if still_running:
                logger.warning(f"{len(still_running)} background task(s) did not stop within {BACKGROUND_STOP_TIMEOUT:.0f}s.")
        for service_name, closer in (
            ("subscription watcher", lambda: sub_watcher.aclose() if sub_watcher else None),
            ("system supervisor", lambda: system_supervisor.aclose()),
            ("cache pruner", lambda: cache_manager.stop_pruner()),
            ("media cleanup", lambda: media_handler.stop_background_cleanup()),
        ):
            try:
                result = closer()
                if result is not None:
                    await asyncio.wait_for(result, timeout=BACKGROUND_STOP_TIMEOUT)
            except Exception:
                logger.debug(f"{service_name} shutdown incomplete", exc_info=True)
        try:
            cleaned = media_handler.purge_temp_media(max_age=3600)
            if cleaned:
                logger.info(f"Purged {cleaned} temporary files from media directory on shutdown.")
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        if getattr(settings, "AUTO_RELOAD", False):
            try:
                from services.code_reloader import code_reloader
                code_reloader.stop()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        # 5. Database: the very last database step (later calls raise instead of reopening it)
        runtime_state.db_ready = False
        try:
            await db_manager.close(final=True)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

        # 6. Bot HTTP sessions and helper pools
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
        try:
            from services.story_renderer import shutdown_playwright_pool
            shutdown_playwright_pool(wait=False, cancel_futures=True)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        remove_exit_signal_handlers(loop, installed_signals)
        logger.info("Telegram Channel Cloner shut down cleanly.")
    return should_reexec

async def main():
    logger.info("🚀 Starting 100k High-Concurrency Telegram Channel Cloner system...")

    if not acquire_pid_lock():
        logger.error("Another instance of Channel Cloner is already running (instance lock held). Exiting.")
        logger.error("⚠️ Boshqa bot nusxasi allaqachon ishlayapti (app.lock band)!")
        return False
    try:
        return await run_application()
    finally:
        release_pid_lock()

if __name__ == "__main__":
    configure_logging()
    while True:
        try:
            reexec = asyncio.run(main())
            if reexec:
                logger.info("🔄 [HOT-RELOAD] Re-executing application process with updated source...")
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
