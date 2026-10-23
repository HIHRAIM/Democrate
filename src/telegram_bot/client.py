"""The aiogram objects the whole Telegram half hangs on, the resolvers every
command reuses, and the one membership event.

Importing this module creates `bot`, `dp` and `router`. Nothing else here
registers a command — the @router decorators live in the modules below, and
this one is imported first because they all need `router`.

Every request the bot makes passes through one session middleware that takes
the custom emoji of Discord servers out of the text it sends (a currency's
emoji may be one, and the bank is shared by both platforms); see
`_without_discord_emoji`.

`my_chat_member_update` is here rather than in an events module because it is
the only event Telegram gives this bot, and because it is really about the
client's own membership: it is the one moment the bot can learn it has been
*added* to a group, which is what starts the seven-day setup deadline. Telegram
publishes no join timestamp, so this row is the clock itself — unlike Discord,
where `Guild.me.joined_at` is authoritative and the stored row is only a record.

`is_server_admin` is deliberately a separate function from its Discord twin
(discord_bot/client.py): the native half of the answer is a group membership
status here and a permission bitfield there.

Not this module's zone: any command, and the dialog machinery
(telegram_bot/dialogs.py).
"""
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.types import ChatMemberUpdated, Message

import db
from config import TELEGRAM_TOKEN
from utils import is_admin, send_service_event, set_command_platform, strip_discord_emoji

logger = logging.getLogger("dem.telegram")

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

async def _without_discord_emoji(make_request, session_bot, method):
    """Take Discord's custom emoji (`<:name:id>`) out of the `text` or
    `caption` of every request before it is sent.

    Telegram cannot show them, and they reach it through every path that
    writes a currency or a good: replies, edited messages, photo captions,
    the scheduled posts, the service notices. One request middleware covers
    them all; stripping line by line would miss the next line somebody adds.
    It runs after the bot has escaped its HTML, which is why
    utils.strip_discord_emoji knows the escaped form too. A text that would
    be left empty is sent as it was."""
    for field in ("text", "caption"):
        value = getattr(method, field, None)
        if isinstance(value, str):
            cleaned = strip_discord_emoji(value)
            if cleaned != value and cleaned.strip():
                setattr(method, field, cleaned)
    return await make_request(session_bot, method)

bot.session.middleware(_without_discord_emoji)

@dp.update.outer_middleware()
async def _telegram_spelling(handler, event, data):
    """Make every text produced while this update is handled spell commands
    the Telegram way, `/bank_pay` rather than Discord's `/bank pay`
    (utils.command_spelling). An outer middleware, not a handler: it runs for
    every update before any filter, and registers nothing."""
    set_command_platform("telegram")
    return await handler(event, data)

GROUP_CHAT_TYPES = ("group", "supergroup")

def _chat_key(message: Message):
    """The localization key of the chat a message came from,
    'chat_id:topic_id'.

    Topic 0 is the group itself, so a forum group and its General topic share one
    key. db/settings.py resolves outwards from here, which is how one topic can
    speak a different language from the group around it."""
    thread = message.message_thread_id or 0
    return f"{message.chat.id}:{thread}"

async def chat_display_name(chat_id):
    """What to call a server or group in front of people: its name, or its bare
    id when nothing knows the name.

    The Telegram twin of discord_bot/client.py: chat_display_name, and async
    for the same reason that one is not: naming a Telegram group means asking
    the API, so this is only ever called from a command somebody typed, never
    per message. A group the bot can see has its name written back to
    `chats.title`, which is what lets the Discord half name a Telegram central
    chat it could never ask about itself."""
    chat_id = str(chat_id)
    try:
        chat = await bot.get_chat(int(chat_id))
        title = getattr(chat, "title", None) or getattr(chat, "full_name", None)
    except Exception:
        title = None
    if title:
        if db.chat_title(chat_id) != title:
            db.set_chat_title(chat_id, title)
        return title
    return db.chat_title(chat_id) or chat_id

def _tg_user_label(user):
    """'@username' when available, otherwise the full name."""
    if user is None:
        return "?"
    if getattr(user, "username", None):
        return f"@{user.username}"
    return user.full_name or str(user.id)

async def is_server_admin(chat_id: int, user_id: int):
    """Only an explicit /admin_add appointment grants community-admin rights."""
    return db.is_server_admin("telegram", chat_id, user_id)

async def _resolve_tg_admin_target(message: Message, arg):
    """Resolve an admin-command target to (user_id, username|None):
    a reply, a text-mention entity, a numeric id, or a public @username."""
    from telegram_bot.dialogs import _parse_tg_user_ref

    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, u.username
    for ent in (message.entities or []):
        if ent.type == "text_mention" and ent.user:
            return ent.user.id, ent.user.username
    ref = _parse_tg_user_ref(arg)
    if ref is None:
        return None, None
    if ref[0] == "id":
        try:
            chat = await bot.get_chat(ref[1])
            return ref[1], getattr(chat, "username", None)
        except Exception:
            return ref[1], None
    try:
        chat = await bot.get_chat("@" + ref[1])
        return chat.id, getattr(chat, "username", None) or ref[1]
    except Exception:
        return None, None

@router.my_chat_member()
async def my_chat_member_update(update: ChatMemberUpdated):
    """The bot's own membership changed somewhere.

    This is the one moment the bot can learn that it has been *added* to a
    group, which is what starts the seven-day setup deadline
    (setup_deadline.py): Telegram has no equivalent of Discord's join
    timestamp, so a group with no row is simply never examined. The change
    must come from outside the chat — old status left or kicked — or a
    promotion to administrator in a group the bot has been sitting in for
    years would read as a fresh arrival and put it on the clock.

    Being removed drops the row, so that a later re-invitation is a fresh
    seven days. The group's union binding is deliberately left alone: only
    `/server_setup` and the Discord side's on_guild_remove touch that."""
    try:
        if not update.new_chat_member or update.new_chat_member.user.id != bot.id:
            return
        new_status = str(update.new_chat_member.status)
        old_status = str(update.old_chat_member.status) if update.old_chat_member else ""
        if new_status in ("left", "kicked"):
            db.forget_deadline("telegram", update.chat.id)
            return
        if update.chat.type in GROUP_CHAT_TYPES and old_status in ("left", "kicked"):
            db.record_join("telegram", update.chat.id)
            await send_service_event(
                "joined_chat",
                platform="Telegram",
                chat=update.chat.title or str(update.chat.id),
                chat_id=update.chat.id,
            )
    except Exception as e:
        logger.warning("my_chat_member handling failed: %s", e)

def _setup_gate_ok(message: Message):
    """Public commands work only in set-up groups (Bot Admins and private
    chats with the bot are exempt)."""
    if message.chat.type not in GROUP_CHAT_TYPES:
        return True
    if message.from_user and is_admin("telegram", message.from_user.id):
        return True
    return db.is_setup(message.chat.id)

async def main():
    """Start long-polling. The coroutine main.py gathers beside the Discord
    client's own task — both halves live on one asyncio loop."""
    await dp.start_polling(bot)
