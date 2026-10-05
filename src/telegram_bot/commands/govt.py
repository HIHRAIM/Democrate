"""Governing bodies on Telegram: `/add_govt` creates one, `/govt` shows who
sits in it.

The search and the member lines are shared with the Discord half and imported
from it; what is written here is the HTML rendering and the argument parsing.

Not this module's zone: parties (commands/parties.py) and the union itself
(commands/unions.py).
"""
from aiogram.filters import Command
from aiogram.types import Message

import db
import sponsors
from message_relay import clean_display_name, escape_html
from utils import get_chat_lang, is_admin, localized

from discord_bot import _govt_member_lines, _search_govt
from telegram_bot.client import GROUP_CHAT_TYPES, _chat_key, router
from telegram_bot.dialogs import _numbered_choice_tg

@router.message(Command("add_govt", "add-govt"))
async def add_govt_tg(message: Message):
    """Create a governing body in a union (Bot Admins), with its own words
    for one member and for several."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=2)
    chunks = [c.strip() for c in parts[2].split("|")] if len(parts) == 3 else []
    if len(chunks) != 3 or not all(chunks):
        await message.reply(localized("add_govt_usage", lang))
        return

    union = parts[1].strip().upper()
    if not (is_admin("telegram", message.from_user.id) or
            sponsors.can_manage_union(
                sponsors.linked_sponsor_id(message.from_user.id), union)):
        await message.reply(localized("no_permission", lang))
        return
    if not db.union_exists(union):
        await message.reply(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes()))
        )
        return

    name = clean_display_name(chunks[0], max_len=100)
    if not db.create_govt_body(union, name, chunks[1], chunks[2]):
        await message.reply(localized("add_govt_exists", lang))
        return
    await message.reply(
        localized("add_govt_created", lang, name=name,
                  union=db.get_union_name(union, lang),
                  singular=chunks[1], plural=chunks[2])
    )

@router.message(Command("govt"))
async def govt_tg(message: Message):
    """Show a governing body of this union and who sits in it, with the party
    each member belongs to."""
    lang = get_chat_lang(_chat_key(message))
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return
    chat = db.get_chat(str(message.chat.id))
    if not chat:
        await message.reply(localized("chat_not_setup", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("govt_usage", lang))
        return

    body, suggestions = _search_govt(chat["union_code"], parts[1])
    if body is None:
        if not suggestions:
            await message.reply(localized("govt_not_found", lang))
            return

        def render(b):
            """One line naming a body in the numbered fallback list."""
            line = b["name"]
            if b["union_code"] != chat["union_code"]:
                line += " " + localized("govt_other_union_mark", lang, code=b["union_code"])
            return line

        body = await _numbered_choice_tg(message, lang,
                                         localized("govt_not_found_suggest", lang),
                                         suggestions, render)
        if body is None:
            return

    members_text = _govt_member_lines(body, lang, viewer_platform="telegram") \
        or localized("govt_no_members", lang)
    union_name = db.get_union_name(body["union_code"], lang)
    text = (f"<b>{escape_html(body['name'])}</b>\n"
            f"{escape_html(members_text)}\n\n"
            f"<i>{escape_html(union_name)}</i>")
    await message.reply(text, parse_mode="HTML")
