import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("dem.main")

import db
from config import DISCORD_TOKEN
from discord_bot import bot as discord_bot
from telegram_bot import main as tg_main
from utils import send_service_event

db.init()

async def retention_loop():
    while True:
        try:
            db.cleanup_old_loc_suggestions()
            db.cleanup_old_pending_links()
            db.cleanup_old_join_requests()
            db.cleanup_old_quiz_progress()
            db.cleanup_old_quiz_results()
        except Exception:
            pass
        await asyncio.sleep(24 * 3600)

async def main():
    tasks = [
        asyncio.create_task(tg_main()),
        asyncio.create_task(discord_bot.start(DISCORD_TOKEN)),
        asyncio.create_task(retention_loop()),
    ]

    await asyncio.sleep(5)
    await send_service_event("bot_started")

    try:
        await asyncio.gather(*tasks)
    finally:
        await send_service_event("bot_stopped")

if __name__ == "__main__":
    asyncio.run(main())
