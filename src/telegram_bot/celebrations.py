"""The Telegram side of the celebrations: greeting the members of a group on
their birthday, in the group or topic `/birthday_channel` chose.

Telegram does not list a group's members to a bot, so the question is asked
the other way round: everyone whose birthday it is and who has a Telegram
identity — their own, or the one linked to their Discord account — is looked
up with `get_chat_member`, and greeted when they are in the group. That is
one request per birthday person per group a day, and nothing at all on a day
nobody has a birthday.

None of this is a command: `birthday_pass` runs once a minute from main.py:
celebration_loop. The choice of greeting and the clock are celebrations.py at
src/.

Not this module's zone: the setup command (telegram_bot/commands/birthdays.py)
and the Discord greetings and roles (discord_bot/celebrations.py).
"""
from datetime import date, datetime, timezone

import celebrations
import db
from message_relay import escape_html
from utils import get_chat_lang

from telegram_bot.client import bot, logger

_PRESENT = ("member", "administrator", "creator")
_PLACEHOLDER = "\x00user\x00"

def _is_present(member):
    """Whether a get_chat_member answer means the person is in the group."""
    status = str(getattr(member, "status", "")).split(".")[-1].lower()
    if status in _PRESENT:
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))

async def _greet_group(row, today):
    """Greet one group's birthday people for `today`, one message each."""
    channel_key = row["birthday_channel"]
    chat_id, _, thread = channel_key.partition(":")
    lang = get_chat_lang(channel_key)
    chat_key = f"telegram:{channel_key}"
    people = celebrations.birthday_people(
        celebrations.birthday_pairs_on(date.fromisoformat(today)))
    for person in people:
        uid = person["ids"].get("telegram")
        if not uid:
            continue
        try:
            member = await bot.get_chat_member(int(chat_id), int(uid))
        except Exception:
            continue
        if not _is_present(member):
            continue
        user_key = db.user_key("telegram", uid)
        number = celebrations.pick_greeting(chat_key, user_key)
        mention = f'<a href="tg://user?id={int(uid)}">{escape_html(member.user.full_name)}</a>'
        text = escape_html(celebrations.greeting_text(number, lang, _PLACEHOLDER))
        try:
            await bot.send_message(int(chat_id), text.replace(escape_html(_PLACEHOLDER), mention),
                                   parse_mode="HTML",
                                   message_thread_id=int(thread) if thread and int(thread) else None)
        except Exception as e:
            logger.warning("Birthday greeting failed in %s: %s", channel_key, e)
            continue
        db.record_greeting(chat_key, user_key, number)

async def birthday_pass(now=None):
    """Greet the birthday people of every group whose local ten o'clock has
    passed today and who has not been greeted yet."""
    now = now or datetime.now(timezone.utc)
    for row in db.all_communities("telegram"):
        if not row["birthday_channel"]:
            continue
        today = celebrations.greeting_due(row, "birthday_marker", now)
        if today is None:
            continue
        try:
            await _greet_group(row, today)
        except Exception as e:
            logger.warning("Birthday greetings for %s failed: %s", row["server_id"], e)
        db.set_community_fields("telegram", row["server_id"], birthday_marker=today)
