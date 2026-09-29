import asyncio
import html
import ipaddress
import json
import logging
import math
import re
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Optional, Dict, Any, List, Set, Tuple
from urllib.parse import urlsplit

import aiohttp

from config.settings import settings
from database.db_manager import db_manager
from database.models import SupplierConfig, StoreProduct, StoreOrder

logger = logging.getLogger("SupplierService")

STARS_PER_USD = 50          # retail conversion used for Stars pricing
MAX_INVOICE_STARS = 100000  # Telegram Stars invoice upper bound per order
DEFAULT_MARGIN_PERCENT = Decimal("25")
TARGET_LINK_RE = re.compile(r"https?://(t\.me|telegram\.me)/[\w+/\-]{3,200}|@[A-Za-z]\w{3,31}")
# Every supplier call is bounded: a hanging panel must never block a payment handler or the sync worker
BALANCE_TIMEOUT = aiohttp.ClientTimeout(total=12, connect=6)
CATALOG_TIMEOUT = aiohttp.ClientTimeout(total=25, connect=6)
ORDER_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=6)
MAX_CATALOG_ITEMS = 20000


class SupplierService:
    """
    Production-grade integration service for Supplier / Reseller APIs (v2 protocol).
    Handles:
    - 24/7 background catalog synchronization
    - Dynamic stock availability tracking
    - Real-time balance monitoring
    - Automated API ordering with intelligent low-balance fallback to admin escrow
    """

    def __init__(self):
        self._sync_task: Optional[asyncio.Task] = None
        self._is_running: bool = False

    # --- SUPPLIER ENDPOINT -----------------------------------------------------------------

    @staticmethod
    def _api_endpoint(cfg: Optional[SupplierConfig]) -> Optional[str]:
        """The supplier API URL comes from the database, so it is validated before every request: only
        https URLs of a public host (no credentials, no loopback/private/link-local IP literals)."""
        url = (getattr(cfg, "api_url", "") or "").strip()
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            _ = parts.port  # raises ValueError for an invalid port
        except ValueError:
            logger.warning("Supplier API URL is malformed; supplier requests are disabled")
            return None
        if parts.scheme != "https" or not host or parts.username or parts.password:
            logger.warning("Supplier API URL must be an https URL without credentials; supplier requests are disabled")
            return None
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if host.lower() == "localhost" or (ip is not None and not ip.is_global):
            logger.warning("Supplier API URL points to a local/private address; supplier requests are disabled")
            return None
        return url

    async def fetch_balance(self, config: Optional[SupplierConfig] = None) -> float:
        """Fetches current balance from supplier API"""
        cfg = config or await db_manager.get_supplier_config()
        if not cfg or not cfg.api_key or not cfg.is_active:
            return cfg.balance if cfg else 0.0
        endpoint = self._api_endpoint(cfg)
        if not endpoint:
            return cfg.balance

        try:
            params = {
                "key": cfg.api_key,
                "action": "balance"
            }
            async with aiohttp.ClientSession(timeout=BALANCE_TIMEOUT) as session:
                async with session.post(endpoint, data=params, allow_redirects=False) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict) and "balance" in data:
                            bal = float(data["balance"])
                            if math.isfinite(bal):
                                await db_manager.update_supplier_balance_and_sync_time(cfg.id or 1, bal)
                                return bal
        except Exception as e:
            logger.warning(f"Could not fetch supplier balance: {e!r}")
        return cfg.balance

    @staticmethod
    def _parse_service(s: Any, supplier_id: int) -> Optional[Dict[str, Any]]:
        """Validates one catalogue entry; entries with a non-positive rate or broken limits are skipped so a
        faulty supplier response can never publish a product for 1 Star."""
        if not isinstance(s, dict):
            return None
        try:
            s_id = int(s.get("service") or 0)
            rate = Decimal(str(s.get("rate")))
            min_q = int(s.get("min") or 10)
            max_q = int(s.get("max") or 10000)
        except (TypeError, ValueError, InvalidOperation):
            return None
        if s_id <= 0 or not rate.is_finite() or rate <= 0 or min_q < 1 or max_q < min_q:
            return None
        return {
            "supplier_id": supplier_id,
            "service": s_id,
            "name": str(s.get("name") or f"Service #{s_id}")[:200],
            "category": str(s.get("category") or "General")[:100],
            "type": str(s.get("type") or "Default")[:50],
            "rate": float(rate),
            "min": min_q,
            "max": max_q,
        }

    async def sync_catalog(self, supplier_id: int = 1) -> Dict[str, Any]:
        """
        Synchronizes product catalog with supplier API.
        - Adds new products
        - Updates prices & margins
        - Marks removed/depleted products as 'out_of_stock'
        """
        cfg = await db_manager.get_supplier_config(supplier_id)
        if not cfg or not cfg.is_active:
            return {"status": "skipped", "message": "Supplier config not active"}

        if not cfg.api_key:
            # Seed mock demo services if no real API key is configured yet
            demo_services = [
                {"service": 1001, "name": "Telegram Stars (50 Stars)", "category": "Telegram Stars", "rate": 0.85, "min": 1, "max": 100, "type": "Package"},
                {"service": 1002, "name": "Telegram Stars (100 Stars)", "category": "Telegram Stars", "rate": 1.65, "min": 1, "max": 100, "type": "Package"},
                {"service": 1003, "name": "Telegram Stars (500 Stars)", "category": "Telegram Stars", "rate": 8.00, "min": 1, "max": 100, "type": "Package"},
                {"service": 2001, "name": "Telegram Premium Obuna (1 Oy)", "category": "Telegram Premium", "rate": 3.99, "min": 1, "max": 10, "type": "Subscription"},
                {"service": 2002, "name": "Telegram Premium Obuna (1 Yil)", "category": "Telegram Premium", "rate": 28.50, "min": 1, "max": 10, "type": "Subscription"},
                {"service": 3001, "name": "Kanal A'zolari (Jonli O'zbek)", "category": "Kanal A'zolari", "rate": 2.20, "min": 100, "max": 50000, "type": "Default"},
                {"service": 3002, "name": "Post Ko'rishlar (Tezkor 1000 ta)", "category": "Ko'rishlar", "rate": 0.15, "min": 100, "max": 100000, "type": "Default"},
            ]
            for s in demo_services:
                await db_manager.upsert_synced_product(s, margin_percent=cfg.margin_percent)
            return {"status": "ok", "synced": len(demo_services), "demo": True}

        endpoint = self._api_endpoint(cfg)
        if not endpoint:
            return {"status": "error", "message": "Supplier API URL is not a valid public https URL"}

        try:
            params = {
                "key": cfg.api_key,
                "action": "services"
            }
            async with aiohttp.ClientSession(timeout=CATALOG_TIMEOUT) as session:
                async with session.post(endpoint, data=params, allow_redirects=False) as resp:
                    if resp.status != 200:
                        return {"status": "error", "message": f"Supplier returned HTTP {resp.status}"}
                    services = await resp.json(content_type=None)
                    if not isinstance(services, list):
                        return {"status": "error", "message": "Invalid response format from supplier"}

            active_service_ids: Set[int] = set()
            skipped = 0
            for s in services[:MAX_CATALOG_ITEMS]:
                s_data = self._parse_service(s, cfg.id or 1)
                if s_data is None:
                    skipped += 1
                    continue
                try:
                    await db_manager.upsert_synced_product(s_data, margin_percent=cfg.margin_percent)
                    active_service_ids.add(s_data["service"])
                except Exception as e_svc:
                    logger.debug(f"Error upserting service {s_data['service']}: {e_svc}")

            # Mark removed or missing products as 'out_of_stock'
            depleted_count = await db_manager.mark_unlisted_products_out_of_stock(active_service_ids, cfg.id or 1)

            # Refresh balance
            balance = await self.fetch_balance(cfg)

            logger.info(
                f"✅ Supplier catalog synced: {len(active_service_ids)} active, {depleted_count} out-of-stock, "
                f"{skipped} invalid entries skipped. Balance: ${balance:.2f}"
            )
            return {
                "status": "ok",
                "synced_count": len(active_service_ids),
                "out_of_stock_count": depleted_count,
                "skipped_count": skipped,
                "balance": balance
            }
        except Exception as e:
            logger.error(f"Catalog sync failed: {e!r}", exc_info=True)
            return {"status": "error", "message": str(e)[:300]}

    # --- PRICING -------------------------------------------------------------------------
    # Prices are computed with Decimal and rounded up once to whole Stars: binary floats turn e.g.
    # 1.1 * 50 into 55.00000000000001, which would overcharge by one Star after rounding up.

    @staticmethod
    def _decimal(value: Any, default: Decimal) -> Decimal:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return default
        return number if number.is_finite() else default

    @staticmethod
    def is_priced_per_unit(product: StoreProduct) -> bool:
        """Reseller panels quote packages/small-quantity services per unit and everything else per 1000."""
        return (product.type or "").lower() == "package" or (product.max_quantity or 0) <= 100

    # Backwards compatible alias
    _is_priced_per_unit = is_priced_per_unit

    def display_quantity(self, product: StoreProduct) -> int:
        """Quantity the catalogue price is shown for: one unit, or 1000 units for per-1000 services."""
        return 1 if self.is_priced_per_unit(product) else 1000

    @staticmethod
    def quantity_bounds(product: StoreProduct) -> Tuple[int, int]:
        min_q = max(1, int(product.min_quantity or 1))
        max_q = max(min_q, int(product.max_quantity or min_q))
        return min_q, max_q

    def _supplier_cost(self, product: StoreProduct, quantity: int) -> Decimal:
        rate = max(self._decimal(product.supplier_rate, Decimal(0)), Decimal(0))
        cost = rate * int(quantity)
        if not self.is_priced_per_unit(product):
            cost = cost / 1000
        return cost

    def supplier_cost_usd(self, product: StoreProduct, quantity: int) -> float:
        return float(self._supplier_cost(product, quantity))

    def _retail_stars(self, product: StoreProduct, quantity: int, margin_percent: float) -> Decimal:
        margin = self._decimal(margin_percent, DEFAULT_MARGIN_PERCENT)
        return self._supplier_cost(product, quantity) * STARS_PER_USD * (100 + margin) / 100

    def quote_price_stars(self, product: StoreProduct, quantity: int, margin_percent: float) -> int:
        """Authoritative price in whole Telegram Stars for `quantity` units (never computed on the client)."""
        stars = self._retail_stars(product, quantity, margin_percent)
        return max(1, int(stars.to_integral_value(rounding=ROUND_CEILING)))

    def unit_price_stars(self, product: StoreProduct, margin_percent: float) -> float:
        """Display-only price for a single unit (can be fractional for per-1000 services)."""
        return round(float(self._retail_stars(product, 1, margin_percent)), 4)

    # --- CHECKOUT & FULFILLMENT ------------------------------------------------------------

    async def create_checkout(self, bot, user_id: int, product_id: int, quantity: int, target_link: str) -> Dict[str, Any]:
        """Validates the request, creates an `awaiting_payment` order and a Telegram Stars invoice link.
        Nothing is ordered from the supplier until Telegram confirms the payment.
        Failures return {"success": False, "code": ..., "message": ...}."""
        product = await db_manager.get_store_product(product_id)
        if not product:
            return {"success": False, "code": "NOT_FOUND", "message": "Mahsulot topilmadi"}
        if not product.is_available or product.stock_status == "out_of_stock":
            return {"success": False, "code": "OUT_OF_STOCK", "message": "Kechirasiz, ushbu mahsulot vaqtincha tugagan"}

        min_q, max_q = self.quantity_bounds(product)
        if quantity < min_q or quantity > max_q:
            return {"success": False, "code": "INVALID_QUANTITY", "message": f"Miqdor {min_q} dan {max_q} gacha bo'lishi kerak"}
        if not TARGET_LINK_RE.fullmatch(target_link or ""):
            return {"success": False, "code": "INVALID_LINK", "message": "Havola noto'g'ri. Masalan: https://t.me/kanal yoki @kanal"}

        cfg = await db_manager.get_supplier_config(product.supplier_id)
        if not cfg or not cfg.is_active:
            return {"success": False, "code": "SUPPLIER_INACTIVE", "message": "Ta'minotchi tizimi faol emas"}

        price_stars = self.quote_price_stars(product, quantity, cfg.margin_percent)
        if price_stars > MAX_INVOICE_STARS:
            return {"success": False, "code": "PRICE_LIMIT", "message": f"Bir buyurtma {MAX_INVOICE_STARS} Stars dan oshmasligi kerak"}

        order_id = await db_manager.create_store_order(StoreOrder(
            user_id=user_id,
            product_id=product.id or 0,
            product_name=product.name,
            quantity=quantity,
            price_stars=price_stars,
            target_link=target_link,
            status="awaiting_payment",
            admin_notified=False,
            note=""
        ))

        from aiogram.types import LabeledPrice
        title = (product.name or "Buyurtma")[:32]
        try:
            invoice_link = await bot.create_invoice_link(
                title=title,
                description=f"{product.name} - {quantity} ta. Havola: {target_link}"[:255],
                payload=json.dumps({"t": "store", "o": order_id, "u": user_id}),
                currency="XTR",
                prices=[LabeledPrice(label=title, amount=price_stars)],
            )
        except Exception as e_inv:
            logger.error(f"Could not create a Stars invoice for store order #{order_id}: {e_inv!r}")
            await db_manager.update_store_order(order_id, status="failed", note=f"Invoice error: {e_inv!r}"[:300])
            return {"success": False, "code": "INVOICE_FAILED",
                    "message": "To'lov hisobini yaratib bo'lmadi. Birozdan so'ng qayta urinib ko'ring."}
        return {
            "success": True,
            "status": "awaiting_payment",
            "order_id": order_id,
            "price_stars": price_stars,
            "invoice_link": invoice_link,
            "message": f"{price_stars} Stars to'lov hisobi yaratildi"
        }

    async def _claim_paid_order(self, order_id: int) -> bool:
        """Atomically moves a paid order to pending_admin. Only the caller that wins this compare-and-set
        may talk to the supplier, so a duplicated payment update or a retry can never order twice; if the
        process dies half-way the order stays visible to the admins as pending_admin."""
        cursor = await db_manager.execute(
            "UPDATE store_orders SET status = 'pending_admin' WHERE id = ? AND status = 'paid'", (order_id,)
        )
        return (getattr(cursor, "rowcount", 0) or 0) == 1

    async def _place_supplier_order(self, endpoint: str, cfg: SupplierConfig, product: StoreProduct,
                                    order: StoreOrder) -> Tuple[bool, Optional[int], str]:
        """Returns (accepted, supplier_order_id, note)."""
        order_payload = {
            "key": cfg.api_key,
            "action": "add",
            "service": product.supplier_service_id,
            "link": order.target_link,
            "quantity": order.quantity
        }
        try:
            async with aiohttp.ClientSession(timeout=ORDER_TIMEOUT) as session:
                async with session.post(endpoint, data=order_payload, allow_redirects=False) as resp:
                    res_json = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.error(f"Supplier order request for #{order.id} timed out")
            return False, None, "Supplier request timed out: check the supplier panel before fulfilling manually"
        except Exception as e_order:
            logger.error(f"Error placing supplier order #{order.id}: {e_order!r}")
            return False, None, f"Exception: {e_order!r}"[:300]

        if isinstance(res_json, dict) and res_json.get("order") not in (None, ""):
            raw_id = res_json["order"]
            try:
                supplier_order_id: Optional[int] = int(raw_id)
            except (TypeError, ValueError):
                supplier_order_id = None
            return True, supplier_order_id, f"Supplier order: {raw_id}"[:300]
        logger.warning(f"Supplier rejected order #{order.id}: {str(res_json)[:300]}")
        return False, None, f"Provider response: {str(res_json)[:280]}"

    async def fulfill_paid_order(self, order_id: int, user_full_name: str = "", user_username: str = "") -> Dict[str, Any]:
        """Routes a PAID order: automated supplier order when the balance allows, otherwise admin escrow."""
        order = await db_manager.get_store_order(order_id)
        if not order or order.status != "paid":
            return {"success": False, "message": "Buyurtma to'lanmagan yoki topilmadi"}
        if not await self._claim_paid_order(order_id):
            return {"success": False, "message": "Buyurtma allaqachon qayta ishlanmoqda"}

        product = await db_manager.get_store_product(order.product_id)
        cfg = await db_manager.get_supplier_config(product.supplier_id) if product else None
        supplier_cost = self.supplier_cost_usd(product, order.quantity) if product else 0.0
        endpoint = self._api_endpoint(cfg) if cfg else None
        current_balance = cfg.balance if cfg else 0.0

        note = "Supplier API not configured"
        if product and cfg and cfg.is_active and cfg.api_key and endpoint:
            current_balance = await self.fetch_balance(cfg)
            if current_balance >= supplier_cost:
                accepted, supplier_order_id, note = await self._place_supplier_order(endpoint, cfg, product, order)
                if accepted:
                    await db_manager.update_store_order(
                        order_id, status="completed", supplier_order_id=supplier_order_id, note=note
                    )
                    return {"success": True, "status": "completed", "order_id": order_id,
                            "message": "Buyurtmangiz qabul qilindi va bajarilmoqda! 🚀"}
            else:
                note = "Supplier balance insufficient"

        await db_manager.update_store_order(order_id, status="pending_admin", note=note[:300])
        notified = await self._notify_admins_about_pending_order(
            order_id=order_id,
            user_id=order.user_id,
            user_full_name=user_full_name,
            user_username=user_username,
            product_name=order.product_name,
            quantity=order.quantity,
            price_stars=order.price_stars,
            target_link=order.target_link,
            supplier_cost=supplier_cost,
            current_balance=current_balance,
            reason=note,
        )
        await db_manager.update_store_order(order_id, admin_notified=bool(notified))
        admin_handle = (await db_manager.get_support_username() or "admin").lstrip("@")
        return {
            "success": True,
            "status": "pending_admin",
            "order_id": order_id,
            "requires_admin_contact": True,
            "admin_username": admin_handle,
            "admin_link": f"https://t.me/{admin_handle}",
            "message": f"To'lov qabul qilindi. Buyurtma administrator tomonidan bajariladi: @{admin_handle}"
        }

    @staticmethod
    async def _send_to_admins(bot, admin_ids: List[int], text: str) -> List[int]:
        """Sends `text` to each admin; returns the ids that could not be reached."""
        failed = []
        for admin_id in admin_ids:
            try:
                await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            except Exception as e_adm:
                logger.warning(f"Could not notify admin {admin_id} about a store order: {e_adm!r}")
                failed.append(admin_id)
        return failed

    async def _notify_admins_about_pending_order(
        self,
        order_id: int,
        user_id: int,
        user_full_name: str,
        user_username: str,
        product_name: str,
        quantity: int,
        price_stars: int,
        target_link: str,
        supplier_cost: float,
        current_balance: float,
        reason: str = ""
    ) -> bool:
        """Sends an urgent notification to all super admins about a PAID order that needs manual fulfillment.
        The dedicated admin bot is tried first; admins it cannot reach (never started it) are notified
        through the public bot. Returns True when at least one admin received the message."""
        text = (
            "🚨 <b>YANGI TO'LANGAN DO'KON BUYURTMASI (QO'LDA BAJARISH)</b>\n\n"
            f"🆔 <b>Buyurtma ID:</b> #{int(order_id)}\n"
            f"👤 <b>Mijoz:</b> {html.escape(user_full_name or '')} (@{html.escape(user_username or 'yoq')}) [<code>{int(user_id)}</code>]\n"
            f"📦 <b>Mahsulot:</b> {html.escape(product_name or '')}\n"
            f"🔢 <b>Miqdori:</b> {int(quantity)} ta\n"
            f"⭐ <b>To'langan:</b> {int(price_stars)} Stars\n"
            f"🔗 <b>Havola:</b> {html.escape(target_link or '')}\n\n"
            f"💰 <b>Ta'minotchi narxi:</b> ${supplier_cost:.2f}\n"
            f"📉 <b>Ta'minotchi balansi:</b> ${current_balance:.2f}\n"
            f"ℹ️ <b>Sabab:</b> {html.escape((reason or '-')[:300])}\n\n"
            "<i>Iltimos, buyurtmani ta'minotchi panelidan qo'lda bajaring (avval panelda bu buyurtma "
            "yaratilmaganini tekshiring).</i>"
        )
        pending = sorted(settings.admin_ids)
        if not pending:
            logger.warning(f"Store order #{order_id} needs manual fulfillment but no super admin is configured")
            return False
        total = len(pending)

        if settings.ADMIN_BOT_TOKEN:
            try:
                from admin_bot.bot_instance import create_admin_bot
                admin_bot = create_admin_bot()
                try:
                    pending = await self._send_to_admins(admin_bot, pending, text)
                finally:
                    await admin_bot.session.close()
            except Exception as e_notify:
                logger.warning(f"Admin bot notification failed: {e_notify!r}")

        if pending and settings.BOT_TOKEN:
            try:
                from admin_bot.public_bot import get_public_bot
                public_bot = get_public_bot()
                if public_bot is not None:
                    pending = await self._send_to_admins(public_bot, pending, text)
            except Exception as e_notify:
                logger.warning(f"Public bot admin notification failed: {e_notify!r}")

        if pending:
            logger.error(f"Store order #{order_id}: {len(pending)} of {total} admins could not be notified")
        return len(pending) < total

    async def start_worker(self):
        """Starts 24/7 background catalog & balance sync worker"""
        if self._is_running:
            return
        self._is_running = True

        async def _loop():
            logger.info("🛒 Supplier 24/7 background sync worker started.")
            # Initial sync on startup
            try:
                await self.sync_catalog()
            except Exception:
                logger.debug("Initial catalog sync note", exc_info=True)

            while self._is_running:
                try:
                    await asyncio.sleep(900)  # Sync every 15 minutes
                    await self.sync_catalog()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"Supplier sync worker error: {e}")
                    await asyncio.sleep(60)

        self._sync_task = asyncio.create_task(_loop())

    async def stop_worker(self):
        self._is_running = False
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass


supplier_service = SupplierService()
