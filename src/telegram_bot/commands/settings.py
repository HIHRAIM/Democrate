"""Per-chat settings on Telegram: `/lang`, `/locallang`, `/setlogs`,
`/settasks`.

`/lang` writes the bare group id and `/locallang` writes `chat:topic`, so a
forum topic can speak a different language from the group around it — the
lookup in db/settings.py resolves the most specific key outwards.

`_set_channel_tg` serves both scheduled-post commands. Its arguments are
positional and optional in any combination, which is why `_classify_channel_args`
exists: on Discord the same information arrives as named parameters and no
classification is needed. That asymmetry is the reason the two halves are
written separately.

Not this module's zone: the union binding (commands/unions.py) and what is
posted into those channels (stats.py).
"""
import re

from aiogram.filters import Command
from aiogram.types import Message

import db
from utils import (
    SUPPORTED_LANGS, format_tz_offset, get_chat_lang, is_admin, localized,
    parse_founday_time, parse_tz_offset, parse_weekday, set_chat_lang,
    weekday_name,
)

from telegram_bot.client import (
    GROUP_CHAT_TYPES, _chat_key, bot, is_server_admin, router,
)

@router.message(Command("lang"))
async def lang_cmd(message: Message):
    """Set the language of the whole group (Server Admins)."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return

    if message.chat.type == "private":
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.reply(localized("lang_usage", lang))
            return
        code = parts[1].strip().lower()
        try:
            set_chat_lang(_chat_key(message), code, is_dm=True)
        except Exception:
            await message.reply(
                localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS)))
            )
            return
        await message.reply(localized("lang_set", code, code=code))
        return

    allowed = is_admin("telegram", message.from_user.id) or \
        await is_server_admin(message.chat.id, message.from_user.id)
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return
    if not db.is_setup(message.chat.id):
        await message.reply(localized("chat_not_setup", lang))
        return

    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply(localized("lang_usage", lang))
        return

    code = parts[1].strip().lower()
    try:
        set_chat_lang(str(message.chat.id), code)
    except Exception:
        await message.reply(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS)))
        )
        return

    await message.reply(localized("lang_set_server", code, code=code))

@router.message(Command("locallang"))
async def locallang_cmd(message: Message):
    """Set the language of this forum topic alone (Server Admins).

    Writes the specific 'chat:topic' key, which wins over the group's own."""
    chat_key = _chat_key(message)
    lang = get_chat_lang(chat_key)
    if not message.from_user:
        return

    if message.chat.type == "private":
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.reply(localized("locallang_usage", lang))
            return
        code = parts[1].strip().lower()
        try:
            set_chat_lang(chat_key, code, is_dm=True)
        except Exception:
            await message.reply(
                localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS)))
            )
            return
        await message.reply(localized("lang_set", code, code=code))
        return

    allowed = is_admin("telegram", message.from_user.id) or \
        await is_server_admin(message.chat.id, message.from_user.id)
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return
    if not db.is_setup(message.chat.id):
        await message.reply(localized("chat_not_setup", lang))
        return

    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply(localized("locallang_usage", lang))
        return

    code = parts[1].strip().lower()
    try:
        set_chat_lang(chat_key, code)
    except Exception:
        await message.reply(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS)))
        )
        return

    await message.reply(localized("lang_set", code, code=code))

_CHAT_KEY_ARG_RE = re.compile(r"^-?\d{5,}(:\d+)?$")

_TIME_ARG_RE = re.compile(r"^\d{1,2}:\d{2}$")

def _classify_channel_args(parts):
    """Sort /setlogs and /settasks tokens by shape: a long number is the chat,
    HH:MM the time, a signed offset the timezone, the rest the weekday."""
    chat = wd = tm = tz = None
    off = False
    for p in parts:
        s = p.strip()
        if not s:
            continue
        low = s.lower()
        if low in ("off", "remove", "disable"):
            off = True
        elif chat is None and _CHAT_KEY_ARG_RE.match(s):
            chat = s
        elif tm is None and _TIME_ARG_RE.match(s):
            tm = s
        elif tz is None and (s[0] in "+-" or low.startswith(("utc", "gmt"))):
            tz = s
        elif wd is None:
            wd = s
    return chat, wd, tm, tz, off

async def _set_channel_tg(message, kind):
    """Bind, re-schedule or unbind one of the two scheduled-post channels.

    Serves both `/setlogs` and `/settasks`. Unlike the Discord side, the arguments
    arrive as bare words in any order, which is why `_classify_channel_args` has to
    work out what each one is before anything can be stored. 'off' unbinds. The
    timezone is stored as a UTC offset in minutes."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return
    if not (is_admin("telegram", message.from_user.id)
            or await is_server_admin(message.chat.id, message.from_user.id)):
        await message.reply(localized("no_permission", lang))
        return
    server_id = str(message.chat.id)
    if not db.is_setup(server_id):
        await message.reply(localized("chat_not_setup", lang))
        return
    parts = (message.text or "").split()[1:]
    chat_arg, wd_arg, time_arg, tz_arg, off = _classify_channel_args(parts)
    if off:
        db.remove_chat_channel("telegram", server_id, kind)
        await message.reply(localized(f"set{kind}_removed", lang))
        return
    if kind == "tasks" and wd_arg is not None:
        await message.reply(localized("setchannel_bad_time", lang))
        return
    if chat_arg:
        chat_id_part = chat_arg.split(":", 1)[0]
        try:
            await bot.get_chat(int(chat_id_part))
        except Exception:
            await message.reply(localized("setchannel_bad_channel", lang))
            return
        channel_key = chat_arg if ":" in chat_arg else f"{chat_arg}:0"
    else:
        channel_key = _chat_key(message)
    wd = None
    if wd_arg is not None:
        try:
            wd = parse_weekday(wd_arg)
        except ValueError:
            await message.reply(localized("setchannel_bad_weekday", lang))
            return
    hour = minute = None
    if time_arg is not None:
        try:
            hour, minute = parse_founday_time(time_arg)
        except ValueError:
            await message.reply(localized("setchannel_bad_time", lang))
            return
    offset = None
    if tz_arg is not None:
        try:
            offset = parse_tz_offset(tz_arg)
        except ValueError:
            await message.reply(localized("setchannel_bad_tz", lang))
            return
    db.set_chat_channel("telegram", server_id, kind, channel_key,
                        weekday=wd, hour=hour, minute=minute, tz_offset=offset)
    row = db.get_chat_channel("telegram", server_id, kind)
    when = f"{row['hour']:02d}:{row['minute']:02d}"
    tz_disp = format_tz_offset(row["tz_offset"])
    if kind == "logs":
        schedule = localized("setlogs_schedule", lang,
                             weekday=weekday_name(row["weekday"], lang),
                             time=when, tz=tz_disp)
    else:
        schedule = localized("settasks_schedule", lang, time=when, tz=tz_disp)
    await message.reply(localized(f"set{kind}_done", lang, channel=channel_key,
                                  schedule=schedule))

@router.message(Command("setlogs"))
async def setlogs_tg(message: Message):
    """Set this group's economic log channel and weekly schedule (Server
    Admins)."""
    await _set_channel_tg(message, "logs")

@router.message(Command("settasks"))
async def settasks_tg(message: Message):
    """Set the channel where the monthly task is posted on the 1st (Server
    Admins)."""
    await _set_channel_tg(message, "tasks")
