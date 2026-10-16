"""Union wikis on Telegram: attaching and detaching them and correcting a
founding date — the three wiki commands that do not depend on a Discord
channel or role.

They behave as their Discord twins do (discord_bot/commands/wikis.py), with
positional arguments: `/wiki_add <union> <link>`, `/wiki_remove <link>
[union]`, `/wiki_founded <link> <DD.MM.YYYY|auto>`. All three are for Bot
Admins. The wiki birthday channel, the per-wiki opt-out and the
representative role exist on Discord only, because what they act on is a
Discord channel or role.

Not this module's zone: reading the wikis (wikis.py) and their storage
(db/wikis.py).
"""
from aiogram.filters import Command
from aiogram.types import Message

import celebrations
import db
import wikis
from utils import get_chat_lang, is_admin, localized

from telegram_bot.client import _chat_key, router

def _bot_admin(message):
    """Whether the sender is a Bot Admin."""
    return bool(message.from_user and is_admin("telegram", message.from_user.id))

@router.message(Command("wiki_add", "wiki-add", "add_wiki", "add-wiki"))
async def add_wiki_tg(message: Message):
    """Attach a wiki to a union (Bot Admins): /wiki_add <union> <link>."""
    lang = get_chat_lang(_chat_key(message))
    if not _bot_admin(message):
        await message.reply(localized("no_permission", lang))
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(localized("add_wiki_usage_tg", lang))
        return
    union = parts[1].strip().upper()
    if not db.union_exists(union):
        await message.reply(localized("setup_unknown_union", lang, code=union,
                                      codes=", ".join(db.get_union_codes())))
        return
    try:
        row = await wikis.load_wiki(parts[2])
    except wikis.WikiBlocked:
        await message.reply(localized("wiki_blocked", lang))
        return
    except wikis.WikiError:
        await message.reply(localized("wiki_not_found", lang))
        return
    if not db.attach_wiki(union, row["id"], f"tg:{message.from_user.id}"):
        await message.reply(localized("wiki_already_attached", lang, name=row["name"], union=union))
        return
    await message.reply(localized("wiki_attached", lang, union=union) + "\n"
                        + celebrations.wiki_summary(row, lang),
                        disable_web_page_preview=True)

@router.message(Command("wiki_remove", "wiki-remove", "remove_wiki", "remove-wiki"))
async def remove_wiki_tg(message: Message):
    """Detach a wiki from a union (Bot Admins): /wiki_remove <link> [union]."""
    lang = get_chat_lang(_chat_key(message))
    if not _bot_admin(message):
        await message.reply(localized("no_permission", lang))
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("remove_wiki_usage_tg", lang))
        return
    row = wikis.match_known_wiki(parts[1], db.all_wikis())
    if row is None:
        await message.reply(localized("wiki_unknown", lang))
        return
    codes = db.wiki_union_codes(row["id"])
    code = parts[2].strip().upper() if len(parts) > 2 else (codes[0] if len(codes) == 1 else None)
    if code is None:
        await message.reply(localized("wiki_specify_union", lang, codes=", ".join(codes)))
        return
    if not db.detach_wiki(code, row["id"]):
        await message.reply(localized("wiki_not_in_union", lang, name=row["name"], union=code))
        return
    await message.reply(localized("wiki_detached", lang, name=row["name"], union=code))

@router.message(Command("wiki_founded", "wiki-founded"))
async def wiki_founded_tg(message: Message):
    """Correct a wiki's founding date, or go back to the detected one with
    'auto' (Bot Admins): /wiki_founded <link> <DD.MM.YYYY|auto>."""
    lang = get_chat_lang(_chat_key(message))
    if not _bot_admin(message):
        await message.reply(localized("no_permission", lang))
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply(localized("wiki_founded_usage_tg", lang))
        return
    row = wikis.match_known_wiki(parts[1], db.all_wikis())
    if row is None:
        await message.reply(localized("wiki_unknown", lang))
        return
    if parts[2].strip().lower() == "auto":
        db.set_wiki_founded_manual(row["id"], None)
    else:
        try:
            db.set_wiki_founded_manual(row["id"], celebrations.parse_date(parts[2]))
        except ValueError:
            await message.reply(localized("wiki_bad_date", lang))
            return
    await message.reply(localized("wiki_founded_set", lang, name=row["name"],
                                  date=celebrations.wiki_date(db.get_wiki(row["id"]))))
