"""A group's own profile and settings on Telegram: `/server_info` and
`/server_edit`.

The twins of discord_bot/commands/servers.py: `/server_info` shows anyone the
group's place in the union's economy and its taxes (stats.py:
community_profile), `/server_edit [option]` lets Bot Admins and Server
Admins set the community tax and the group's timezone through a numbered
menu.

Not this module's zone: the tax arithmetic (economy/taxes.py).
"""
from aiogram.filters import Command
from aiogram.types import Message

import db
import economy
import stats
from message_relay import escape_html
from utils import (
    format_tz_offset, get_chat_lang, is_admin, localized, parse_tz_offset, rate_limit_ok,
)

from telegram_bot.client import (
    GROUP_CHAT_TYPES, _chat_key, chat_display_name, is_server_admin, router,
)
from telegram_bot.dialogs import _dialog_text_tg, _numbered_choice_tg

_EDIT_SERVER_OPTIONS = ("tax", "tz")

@router.message(Command("server_info", "server-info", "server"))
async def server_tg(message: Message):
    """Show this group's profile (everyone)."""
    lang = get_chat_lang(_chat_key(message))
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return
    if not db.is_setup(message.chat.id):
        await message.reply(localized("chat_not_setup", lang))
        return
    who = message.from_user.id if message.from_user else message.chat.id
    if not rate_limit_ok(("server-profile", "telegram", who), limit=5, window_seconds=60):
        return
    name = await chat_display_name(message.chat.id)
    profile = stats.community_profile("telegram", str(message.chat.id), lang)
    lines = [f"<b>{escape_html(name or profile['title'])}</b>"]
    for title, rows in profile["sections"]:
        lines.append("")
        lines.append(f"<b>{escape_html(title)}</b>")
        lines.extend(f"{escape_html(k)}: {escape_html(str(v))}" for k, v in rows)
    await message.reply("\n".join(lines), parse_mode="HTML")

def _tz_minutes(value):
    """A typed UTC offset in minutes, or None so the dialog asks again."""
    try:
        return parse_tz_offset(value)
    except ValueError:
        return None

@router.message(Command("server_edit", "server-edit", "edit_server", "edit-server"))
async def edit_server_tg(message: Message):
    """Edit this group's settings: the community tax and the timezone (Bot
    Admins and Server Admins): /server_edit [option]."""
    lang = get_chat_lang(_chat_key(message))
    if message.chat.type not in GROUP_CHAT_TYPES or not message.from_user:
        await message.reply(localized("group_only", lang))
        return
    uid = message.from_user.id
    if not (is_admin("telegram", uid) or await is_server_admin(message.chat.id, uid)):
        await message.reply(localized("no_permission", lang))
        return
    if not db.is_setup(message.chat.id):
        await message.reply(localized("chat_not_setup", lang))
        return
    args = (message.text or "").split()[1:]
    option = int(args[0]) if args and args[0].isdigit() else None
    if option is None or not 1 <= option <= len(_EDIT_SERVER_OPTIONS):
        items = [localized(f"edit_server_opt_{key}", lang) for key in _EDIT_SERVER_OPTIONS]
        name = await chat_display_name(message.chat.id)
        chosen = await _numbered_choice_tg(
            message, lang, localized("edit_server_menu_header", lang, name=name),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_SERVER_OPTIONS[chosen[0]]
    else:
        action = _EDIT_SERVER_OPTIONS[option - 1]
    server = str(message.chat.id)
    if action == "tax":
        rate = await _dialog_text_tg(
            message, lang,
            localized("edit_server_ask_tax", lang,
                      current=economy.format_percent(db.community_tax("telegram", server)),
                      max=economy.format_percent(economy.MAX_TAX_PERCENT)),
            validator=economy.parse_percent, error_key="tax_bad_percent")
        if rate is None:
            return
        db.set_community_fields("telegram", server, tax_percent=rate)
        await message.reply(localized("edit_server_tax_set", lang,
                                      percent=economy.format_percent(rate)))
    elif action == "tz":
        minutes = await _dialog_text_tg(
            message, lang,
            localized("edit_server_ask_tz", lang,
                      current=format_tz_offset(db.community_tz("telegram", server))),
            validator=_tz_minutes, error_key="setchannel_bad_tz")
        if minutes is None:
            return
        db.set_community_fields("telegram", server, tz_offset=minutes)
        await message.reply(localized("edit_server_tz_set", lang, tz=format_tz_offset(minutes)))
