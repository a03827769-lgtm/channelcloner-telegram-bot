import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from config.settings import settings
from admin_bot.middlewares.admin_auth_middleware import AdminStrictAuthMiddleware
from admin_bot.handlers.dashboard import router as dashboard_router
from admin_bot.handlers.mtproto_auth import router as auth_router
from admin_bot.handlers.system_status import router as status_router
from admin_bot.handlers.broadcast import router as broadcast_router
from admin_bot.handlers.backup import router as backup_router
from admin_bot.handlers.user_management import router as user_mgmt_router

logger = logging.getLogger(__name__)

def create_admin_bot() -> Bot:
    """Creates dedicated Aiogram Bot instance for Super Admins"""
    session = AiohttpSession()
    return Bot(token=settings.ADMIN_BOT_TOKEN, session=session)

def create_admin_dispatcher() -> Dispatcher:
    """Creates dedicated Aiogram Dispatcher with strict security middleware"""
    dp = Dispatcher(storage=MemoryStorage())
    
    # Strictly gate all events to admin users only
    admin_mw = AdminStrictAuthMiddleware()
    dp.message.middleware(admin_mw)
    dp.callback_query.middleware(admin_mw)

    # Register admin routers
    dp.include_router(dashboard_router)
    dp.include_router(auth_router)
    dp.include_router(status_router)
    dp.include_router(broadcast_router)
    dp.include_router(backup_router)
    dp.include_router(user_mgmt_router)
    
    return dp
