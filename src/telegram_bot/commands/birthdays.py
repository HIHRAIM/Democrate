"""Personal birthdays on Telegram: a person's own date and the group's
greeting place.

`/birthday_set 25.12` sets the caller's day and month, `/birthday_set off` forgets
it, `/birthday_set` alone shows it; a linked Discord account shares the date,
and in a group the answer clears itself after a minute. `/birthday_channel`
binds the group or the topic it is run in, `[off]` unbinds it, and an
optional UTC offset (`UTC+3`) becomes the group's — Bot Admins and Server
Admins of a group bound to a union.

Not this module's zone: the greetings themselves (telegram_bot/celebrations.py)
and the Discord twins (discord_bot/commands/birthdays.py), which also own the
birthday role a Telegram group has no equivalent of.
"""
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import celebrations
import db
from utils import format_tz_offset, get_chat_lang, is_admin, localized, parse_tz_offset

from message_relay import escape_html
from telegram_bot.client import GROUP_CHAT_TYPES, _chat_key, bot, is_server_admin, router
from telegram_bot.dialogs import _reply_temp

def _birthday_text(day, month, lang):
    """A stored birthday for people, e.g. '25.12'."""
    return localized("birthday_date", lang, date=f"{int(day):02d}.{int(month):02d}")

async def _answer(message, text):
    """Reply privately as such, and in a group with a message that clears
    itself — a birthday is the person's own business."""
    if message.chat.type in GROUP_CHAT_TYPES:
        await _reply_temp(message, text)
    else:
        await message.reply(text)

@router.message(Command("birthday_set", "birthday-set", "birthday"))
async def birthday_tg(message: Message):
    """Set, show or clear the caller's birthday: /birthday_set DD.MM | off."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    uid = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) == 1:
        current = db.get_birthday("telegram", uid)
        await _answer(message, localized("birthday_none", lang) if current is None else
                      localized("birthday_current", lang,
                                date=_birthday_text(current[0], current[1], lang)))
        return
    if parts[1].lower() in ("off", "clear", "-"):
        key = "birthday_cleared" if db.clear_birthday("telegram", uid) else "birthday_none"
        await _answer(message, localized(key, lang))
        return
    try:
        day, month = celebrations.parse_day_month(parts[1])
    except ValueError:
        await _answer(message, localized("birthday_bad_date_tg", lang))
        return
    db.set_birthday("telegram", uid, day, month, display_name=message.from_user.full_name)
    await _answer(message, localized("birthday_saved", lang,
                                     date=_birthday_text(day, month, lang)))

@router.message(Command("birthday_channel", "birthday-channel"))
async def birthday_channel_tg(message: Message):
    """Bind this group or topic for birthday greetings, or unbind it with
    'off'; an optional UTC offset becomes the group's (Bot Admins and Server
    Admins): /birthday_channel [off] [UTC+3]."""
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
    off = any(a.lower() in ("off", "remove", "disable") for a in args)
    fields = {}
    for arg in args:
        if arg.lower() in ("off", "remove", "disable"):
            continue
        try:
            fields["tz_offset"] = parse_tz_offset(arg)
        except ValueError:
            await message.reply(localized("setchannel_bad_tz", lang))
            return
    if off:
        db.set_community_fields("telegram", message.chat.id, birthday_channel=None, **fields)
        await message.reply(localized("birthday_channel_off", lang))
        return
    db.set_community_fields("telegram", message.chat.id,
                            birthday_channel=_chat_key(message), **fields)
    await message.reply(localized("birthday_channel_set_tg", lang,
                                  time=f"{celebrations.GREETING_HOUR:02d}:00",
                                  tz=format_tz_offset(db.community_tz("telegram", message.chat.id))))

async def _calendar_people(chat_id, scope, start, end):
    """(day, month, name) of the people whose birthday falls in the window:
    members of this group — Telegram is asked about each — or, for the
    whole bot, everyone by the name stored with the birthday. A person the
    bot cannot name is left out rather than shown by id."""
    people = []
    for person in celebrations.birthday_people(celebrations.window_pairs(start, end)):
        name = None
        if scope == "l":
            uid = person["ids"].get("telegram")
            if not uid:
                continue
            try:
                member = await bot.get_chat_member(int(chat_id), int(uid))
            except Exception:
                continue
            status = str(getattr(member, "status", "")).split(".")[-1].lower()
            if status in ("member", "administrator", "creator") or (
                    status == "restricted" and getattr(member, "is_member", False)):
                name = member.user.full_name
        else:
            name = person["name"]
        if name:
            people.append((person["day"], person["month"], name))
    return people

async def calendar_message(chat, lang, state):
    """One page of the calendar as (HTML text, keyboard) for `chat`, from a
    packed state ('l15:0' — see celebrations.calendar_state). Outside a group
    bound to a union the scope is the whole bot. The buttons carry the next
    state, so telegram_bot/callbacks.py: handle_calendar needs nothing
    else."""
    scope, kinds, page = celebrations.calendar_state(state)
    bound = db.get_chat(chat.id) if chat.type in GROUP_CHAT_TYPES else None
    if scope == "l" and bound is None:
        scope = "g"
    start, end = celebrations.calendar_window(page)
    people = await _calendar_people(chat.id, scope, start, end) if "person" in kinds else []
    events = celebrations.calendar_events(bound["union_code"] if scope == "l" else None,
                                          kinds, people, "telegram")
    title, lines = celebrations.calendar_lines(events, page, lang)
    text = f"<b>{escape_html(title)}</b>\n\n" + "\n".join(escape_html(l) for l in lines)
    if scope == "g":
        text += f"\n\n<i>{escape_html(localized('calendar_scope_global', lang))}</i>"

    def button(label, new_scope, new_kinds, new_page):
        """One button that redraws the calendar in the given state."""
        state = celebrations.pack_calendar_state(new_scope, new_kinds, new_page)
        return InlineKeyboardButton(text=label, callback_data="cal:" + state)

    toggles = [button(("✅ " if k in kinds else "▫️ ") + localized(f"calendar_btn_{k}", lang),
                      scope, kinds ^ {k}, page) for k in celebrations.CALENDAR_ORDER]
    other = "g" if scope == "l" else "l"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [button("◀", scope, kinds, page - 1), button("▶", scope, kinds, page + 1)],
        toggles,
        [button(localized("calendar_btn_global" if scope == "l" else "calendar_btn_local", lang),
                other, kinds, page)],
    ])
    return text, keyboard

@router.message(Command("calendar"))
async def calendar_tg(message: Message):
    """Show the coming founding days of unions and wikis and members'
    birthdays, with buttons to turn the page, hide a kind of event, or see
    the whole bot (everyone)."""
    lang = get_chat_lang(_chat_key(message))
    text, keyboard = await calendar_message(
        message.chat, lang, celebrations.pack_calendar_state("l", celebrations.CALENDAR_KINDS, 0))
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)
