"""Unions and the chats bound to them on Telegram: `/setup`, `/add_unia`,
`/allow_parties`, `/list_chats`, `/force_leave`.

The Discord twin is discord_bot/commands/unions.py and the two are deliberately
separate programs: this one reads positional arguments out of message text and
answers with HTML, that one takes typed slash-command parameters and answers
with an embed. Only the writes are the same.

`/list_chats` and `/force_leave` reach into the Discord half at the call site,
which is the deliberate cycle-breaker between the two halves.

Not this module's zone: the group's language (commands/settings.py) and the
party rows a union holds (commands/parties.py).
"""
import re

from aiogram.filters import Command
from aiogram.types import Message

import db
from utils import (
    LANG_ORDER, SUPPORTED_LANGS, get_chat_lang, is_admin, language_name,
    localized, send_service_event, set_chat_lang,
)

from telegram_bot.client import GROUP_CHAT_TYPES, _chat_key, _tg_user_label, bot, router

UNION_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,12}$")

@router.message(Command("setup"))
async def setup_cmd(message: Message):
    """Bind this group to a union and set its language (Bot Admins).

    The command the whole bot is gated on — an unbound group answers almost
    nothing. Also settles the seven-day deadline and reports to the service
    chats."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply(localized("setup_usage", lang))
        return

    union = parts[1].strip().upper()
    code = parts[2].strip().lower()
    if not db.union_exists(union):
        await message.reply(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes()))
        )
        return
    if code not in SUPPORTED_LANGS:
        await message.reply(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS)))
        )
        return

    db.setup_chat("telegram", message.chat.id, union, message.from_user.id,
                  title=message.chat.title)
    set_chat_lang(str(message.chat.id), code)
    await message.reply(
        localized("setup_success", code,
                  union=db.get_union_name(union, code), code=union,
                  lang_name=language_name(code), lang=code)
    )
    await send_service_event("setup_done", platform="Telegram",
                             chat=message.chat.title or str(message.chat.id),
                             union=union, user=_tg_user_label(message.from_user))

@router.message(Command("add_unia", "add-unia"))
async def add_unia_cmd(message: Message):
    """Create a union, named in any subset of the six languages (Bot
    Admins)."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(localized("add_unia_usage", lang))
        return

    code = parts[1].strip().upper()
    if not UNION_CODE_RE.match(code):
        await message.reply(localized("add_unia_invalid_code", lang))
        return

    names = {}
    for chunk in parts[2].split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not sep or key not in SUPPORTED_LANGS or not value:
            await message.reply(localized("add_unia_usage", lang))
            return
        names[key] = value
    if not names:
        await message.reply(localized("add_unia_need_name", lang))
        return

    if not db.add_union(code, names):
        await message.reply(localized("add_unia_exists", lang, code=code))
        return

    listed = "\n".join(f"{language_name(L)}: {names[L]}" for L in LANG_ORDER if L in names)
    await message.reply(localized("add_unia_created", lang, code=code, names=listed))
    await send_service_event("union_created", code=code,
                             user=_tg_user_label(message.from_user))

@router.message(Command("list_chats"))
async def list_chats_cmd(message: Message):
    """List every chat the bot is in, on both platforms (Bot Admins).

    The Discord half is reached at the call site — the cycle-breaker between the
    halves."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    lines = [localized("list_chats_discord_header", lang)]
    try:
        from discord_bot import bot as dc_bot
        for g in dc_bot.guilds:
            row = db.get_chat(str(g.id))
            union = f" [{row['union_code']}]" if row else ""
            lines.append(f"- {g.name} — id: {g.id}{union}")
    except Exception:
        pass

    tg_ids = db.get_telegram_group_ids()
    if tg_ids:
        lines.append("\n" + localized("list_chats_telegram_header", lang))
        for pid in tg_ids:
            row = db.get_chat(pid)
            union = f" [{row['union_code']}]" if row else ""
            try:
                chat = await bot.get_chat(int(pid))
                title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(pid)
            except Exception:
                title = str(pid)
            lines.append(f"- {title} — id: {pid}{union}")
    else:
        lines.append("\n" + localized("list_chats_no_telegram", lang))

    msg = "\n".join(lines)
    if len(msg) > 4000:
        from aiogram.types import BufferedInputFile
        doc = BufferedInputFile(msg.encode("utf-8"), filename="chat_list.txt")
        await message.reply_document(doc, caption=localized("list_chats_too_long", lang))
    else:
        await message.reply(msg)

@router.message(Command("force_leave"))
async def force_leave_cmd(message: Message):
    """Make the bot leave a server or group by id (Bot Admins).

    Drops the union binding so a later re-invitation starts clean; nothing the
    community created is deleted."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply(localized("force_leave_usage", lang))
        return

    platform = parts[1].strip().lower()
    target_id = parts[2].strip()

    if platform == "discord":
        try:
            gid = int(target_id)
        except ValueError:
            await message.reply(localized("force_leave_invalid_id", lang))
            return
        try:
            from discord_bot import bot as dc_bot
            guild = dc_bot.get_guild(gid)
        except Exception:
            guild = None
        if not guild:
            await message.reply(localized("force_leave_not_member", lang))
            return
        try:
            await guild.leave()
        except Exception as e:
            await message.reply(localized("force_leave_failed", lang, error=e))
            return
        db.remove_chat(gid)
        await message.reply(localized("force_leave_success_discord", lang, guild_id=gid))
        return

    if platform == "telegram":
        try:
            tid = int(target_id)
        except ValueError:
            await message.reply(localized("force_leave_invalid_id", lang))
            return
        try:
            await bot.leave_chat(tid)
        except Exception as e:
            await message.reply(localized("force_leave_failed", lang, error=e))
        db.remove_chat(tid)
        await message.reply(localized("force_leave_success_telegram", lang, chat_id=tid))
        return

    await message.reply(localized("force_leave_unsupported_platform", lang))

@router.message(Command("allow_parties", "allow-parties"))
async def allow_parties_tg(message: Message):
    """Switch parties on or off in a union (Bot Admins).

    The role list it takes is a list of *Discord* role ids: founding a party is
    gated on a Discord role even when the command is run from Telegram, because
    Telegram groups have no roles to gate on."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("allow_parties_usage", lang))
        return

    union = parts[1].strip().upper()
    action = parts[2].strip().lower()
    if not db.union_exists(union):
        await message.reply(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes()))
        )
        return

    if action == "disable":
        db.set_parties_enabled(union, False)
        await message.reply(localized("allow_parties_disabled", lang, code=union))
        await send_service_event("parties_disabled", code=union,
                                 user=_tg_user_label(message.from_user))
        return
    if action != "enable":
        await message.reply(localized("allow_parties_usage", lang))
        return

    roles_raw = "".join(parts[3:])
    role_ids = [p.strip() for p in roles_raw.split(",") if p.strip()]
    if not role_ids:
        await message.reply(localized("allow_parties_need_roles", lang))
        return
    if not all(p.isdigit() for p in role_ids):
        await message.reply(localized("allow_parties_invalid_roles", lang))
        return

    db.set_parties_enabled(union, True, role_ids)
    await message.reply(localized("allow_parties_enabled", lang, code=union,
                                  roles=", ".join(role_ids)))
    await send_service_event("parties_enabled", code=union,
                             user=_tg_user_label(message.from_user))
