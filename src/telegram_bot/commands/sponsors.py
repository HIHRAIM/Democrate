"""Show the shared Patreon capacity to a linked Telegram account."""
from aiogram.filters import Command
from aiogram.types import Message

import db
import sponsors
from config import SPONSOR_URL
from telegram_bot.client import router
from utils import get_chat_lang, localized

@router.message(Command("sponsor"))
async def sponsor_cmd(message: Message):
    """Use the same union, community and earning budget as Discord."""
    lang = get_chat_lang(str(message.chat.id))
    owner = sponsors.linked_sponsor_id(message.from_user.id) if message.from_user else None
    if not owner:
        await message.answer(localized("sponsor_not_active", lang))
        return
    tier = await sponsors.refresh_tier(owner)
    caps = sponsors.limits(tier)
    unions = db.sponsor_unions(owner)
    status = localized(
        "sponsor_status", lang,
        tier=(localized("sponsor_tier_community", lang) if tier == -1 else tier),
        unions=len(unions),
        union_limit=caps["unions"], servers=db.sponsor_server_usage(owner),
        server_limit=caps["servers"], names=", ".join(unions) or "—",
        activity=db.activity_usage(owner), activity_limit=caps["activity_day"],
        activity_minute=caps["activity_minute"], url=SPONSOR_URL)
    addendum = sponsors.status_addendum(owner, lang)
    if addendum:
        status += "\n" + addendum
    await message.answer(status)
