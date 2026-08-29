import asyncio
from aiogram import Bot
from config.settings import settings

async def main():
    bot = Bot(token=settings.BOT_TOKEN)
    try:
        chat = await bot.get_chat("@yashil_reestr")
        print("Chat title:", chat.title)
        member = await bot.get_chat_member(chat_id="@yashil_reestr", user_id=bot.id)
        print("Bot status:", member.status)
        print("Can post messages:", getattr(member, "can_post_messages", None))
    except Exception as e:
        print("Error checking channel:", e)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
