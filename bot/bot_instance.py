import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from config.settings import settings
from bot.handlers.start import router as start_router
from bot.handlers.stars_billing import router as stars_billing_router
from bot.handlers.cloner_menu import router as cloner_menu_router
from bot.handlers.settings_menu import router as settings_menu_router
from bot.handlers.history_clone import router as history_clone_router
from bot.handlers.help_guide import router as help_guide_router
from bot.middlewares.user_registration_middleware import UserRegistrationMiddleware

logger = logging.getLogger(__name__)

def create_bot() -> Bot:
    """Creates high-performance Aiogram Bot instance for public users"""
    session = AiohttpSession()
    return Bot(token=settings.BOT_TOKEN, session=session)

def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    
    # Attach automatic user registration middleware for all users
    user_reg_mw = UserRegistrationMiddleware()
    dp.message.middleware(user_reg_mw)
    dp.callback_query.middleware(user_reg_mw)

    # Register routers for client features
    dp.include_router(start_router)
    dp.include_router(stars_billing_router)
    dp.include_router(cloner_menu_router)
    dp.include_router(settings_menu_router)
    dp.include_router(history_clone_router)
    dp.include_router(help_guide_router)
    
    return dp
