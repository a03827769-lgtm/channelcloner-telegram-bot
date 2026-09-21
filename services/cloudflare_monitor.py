import asyncio
import logging
import requests
import os
import aiomysql

logger = logging.getLogger("CloudflareMonitor")

class CloudflareMonitor:
    def __init__(self, bot):
        self.bot = bot
        self._running = False
        self._task = None
        self.metrics_url = os.getenv("CLOUDFLARE_METRICS_URL", "http://cloudflared:20241/quicktunnel")
        self.current_url = None
        self._db_pool = None

    async def _init_pool(self):
        if not self._db_pool:
            self._db_pool = await aiomysql.create_pool(
                host=os.getenv("MYSQL_HOST", "mysql"),
                port=int(os.getenv("MYSQL_PORT", 3306)),
                user=os.getenv("MYSQL_USER", "cloner_user"),
                password=os.getenv("MYSQL_PASSWORD", "cloner_pass_2026"),
                db=os.getenv("MYSQL_DATABASE", "channelcloner"),
                autocommit=True
            )

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._monitor_loop())
            logger.info("CloudflareMonitor started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            logger.info("CloudflareMonitor stopped")
        if self._db_pool:
            self._db_pool.close()

    async def _monitor_loop(self):
        await self._init_pool()
        while self._running:
            try:
                await self._check_tunnel()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in CloudflareMonitor: {e}")
            
            # Poll every 10 seconds
            await asyncio.sleep(10)

    async def _check_tunnel(self):
        # If a permanent domain is configured, use it directly (Named Tunnel mode)
        permanent_domain = os.getenv("CLOUDFLARE_TUNNEL_DOMAIN", "").strip()
        if not permanent_domain:
            webapp_url = os.getenv("WEBAPP_URL", "").strip()
            if webapp_url.startswith("https://") and len(webapp_url) > len("https://"):
                permanent_domain = webapp_url.replace("https://", "").rstrip("/")
        
        if permanent_domain:
            hostname = f"https://{permanent_domain}"
            if hostname != self.current_url:
                logger.info(f"🚀 Using permanent domain: {hostname}")
                self.current_url = hostname
                await self._sync_url(hostname)
            return
        
        # Fallback: Quick Tunnel mode — poll /quicktunnel endpoint
        try:
            response = await asyncio.to_thread(requests.get, self.metrics_url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if "hostname" in data and data["hostname"]:
                    hostname = data["hostname"]
                    if not hostname.startswith("http"):
                        hostname = "https://" + hostname
                    
                    if hostname != self.current_url and len(hostname) > len("https://"):
                        logger.info(f"🚀 Detected new Cloudflare Tunnel URL: {hostname}")
                        self.current_url = hostname
                        await self._sync_url(hostname)
        except requests.RequestException:
            pass
        except Exception as e:
            logger.error(f"Failed to check Cloudflare metrics: {e}")

    async def _sync_url(self, url: str):
        # 1. Update MySQL
        try:
            async with self._db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO bot_settings (key_name, value_text) VALUES (%s, %s) "
                        "ON DUPLICATE KEY UPDATE value_text = %s",
                        ("webapp_url", url, url)
                    )
            logger.info(f"✅ Synchronized active URL to MySQL: {url}")
        except Exception as e:
            logger.error(f"MySQL sync failed: {e}")

        # 2. Update Telegram Menu Button
        try:
            from aiogram.types import MenuButtonWebApp, WebAppInfo
            await self.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🖥 Mini App",
                    web_app=WebAppInfo(url=url)
                )
            )
            logger.info(f"✅ Synchronized active URL to Telegram Bot Menu Button: {url}")
        except Exception as e:
            logger.error(f"Telegram Menu Button sync failed: {e}")
