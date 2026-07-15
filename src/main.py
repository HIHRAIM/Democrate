import asyncio
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("dem.main")

import db
import economy
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

def _run_due_economy_ticks():
    """Fire the daily and monthly economy ticks when their UTC period rolls over.
    The last-run markers live in bot_settings so a restart never double-runs a
    period nor skips one it missed while the bot was down (mirrors the wiki
    founday loop's last_sent_year bookkeeping)."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if db.get_setting("econ_last_daily") != today:
        summary = economy.run_daily_tick()
        db.set_setting("econ_last_daily", today)
        logger.info("Economy daily tick: %s", summary)
    month = now.strftime("%Y-%m")
    if db.get_setting("econ_last_monthly") != month:
        summary = economy.run_monthly_tick()
        db.set_setting("econ_last_monthly", month)
        logger.info("Economy monthly tick: %s", summary)

async def economy_loop():
    """FX recompute, autocraft/autosend (daily) and wages + party dues (monthly),
    by the example of retention_loop. Polls hourly and acts only when a period is
    actually due."""
    while True:
        try:
            _run_due_economy_ticks()
        except Exception:
            logger.exception("Economy tick failed")
        await asyncio.sleep(3600)

async def main():
    tasks = [
        asyncio.create_task(tg_main()),
        asyncio.create_task(discord_bot.start(DISCORD_TOKEN)),
        asyncio.create_task(retention_loop()),
        asyncio.create_task(economy_loop()),
    ]

    await asyncio.sleep(5)
    await send_service_event("bot_started")

    try:
        await asyncio.gather(*tasks)
    finally:
        await send_service_event("bot_stopped")

if __name__ == "__main__":
    asyncio.run(main())
