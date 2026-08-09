"""The Telegram half of the Olympiad: the permission gates, the dialog object
and the four commands.

The dialogs themselves live in olympiad.py and are shared with the Discord bot;
what stays here is the Telegram dialog object they drive — the same four
methods (`send`, `ask`, `choose`, `choose_numbers`) over inline keyboards and
plain replies instead of embeds and views.

Telegram has no published command list to hide anything from, so the gating
people see is `/help` (which omits these commands while no Olympiad runs) and
the refusal in `_olympiad_admin_gate_tg`. Their Discord counterparts really do
disappear from the command tree; that asymmetry is the platform's, not a
choice.

The votes still travel to the contest's Discord chats: `post_olympiad_vote` is
imported from the Discord half, because the review and accepted-vote chats are
always on Discord.

Not this module's zone: the event's rules, dates and dialogs (olympiad.py), the
review embeds and verdict commands (discord_bot/olympiad.py) and the rows
(db/olympiad.py).
"""
import asyncio

from aiogram.filters import Command
from aiogram.types import Message

import db
import olympiad
from utils import (
    DEFAULT_LANG, available_locales, get_chat_lang, is_admin, language_name,
    localized, set_chat_lang,
)

from discord_bot import post_olympiad_vote, sync_olympiad_commands
from telegram_bot.client import _chat_key, _tg_user_label, router
from telegram_bot.dialogs import (
    DIALOG_TIMEOUT, _await_oly_button, _new_oly_token, _oly_keyboard,
    _pending_inputs, _pending_oly_buttons,
)

async def _wait_reply_or_button_tg(chat_id, user_id, token, fut, timeout=DIALOG_TIMEOUT):
    """Race the caller's next message against a press on the inline keyboard.
    Returns ('text', message), ('button', value) or ('timeout', None)."""
    key = (chat_id, user_id)
    old = _pending_inputs.pop(key, None)
    if old and not old.done():
        old.cancel()
    msg_fut = asyncio.get_running_loop().create_future()
    _pending_inputs[key] = msg_fut
    try:
        done, _pending = await asyncio.wait({msg_fut, fut}, timeout=timeout,
                                            return_when=asyncio.FIRST_COMPLETED)
        if fut in done:
            return "button", fut.result()
        if msg_fut in done:
            try:
                return "text", msg_fut.result()
            except Exception:
                return "timeout", None
        return "timeout", None
    finally:
        if _pending_inputs.get(key) is msg_fut:
            _pending_inputs.pop(key, None)
        if not msg_fut.done():
            msg_fut.cancel()
        _pending_oly_buttons.pop(token, None)

class _OlyDialogTg:
    """The Telegram half of an Olympiad conversation: the same four coroutines
    olympiad.py expects, so the dialogs themselves are written only once."""

    def __init__(self, message: Message, lang):
        """Hold the message, the chat, the voter and the language every step
        of the dialog answers in."""
        self.message = message
        self.lang = lang
        self.chat_id = message.chat.id
        self.user_id = message.from_user.id
        self.author = _tg_user_label(message.from_user)

    async def send(self, text, reply_markup=None):
        """Send one dialog line, optionally with a keyboard."""
        return await self.message.answer(text[:4000], reply_markup=reply_markup)

    async def ask(self, prompt, *, validator=None, error_text=None, attempts=5,
                  buttons=()):
        """Ask `prompt` and wait for an answer. Returns ('text', value),
        ('button', value), or None when stopped or timed out."""
        pending = prompt
        for _ in range(attempts):
            token, fut = _new_oly_token(self.user_id, self.lang)
            row = list(buttons) + [(olympiad.text("stop_button", self.lang), "stop")]
            await self.send(pending, reply_markup=_oly_keyboard(token, row,
                                                                per_row=len(row)))
            kind, payload = await _wait_reply_or_button_tg(self.chat_id, self.user_id,
                                                           token, fut)
            if kind == "button":
                if payload == "stop":
                    await self.send(olympiad.text("dialog_stopped", self.lang))
                    return None
                return "button", payload
            if kind != "text":
                await self.send(localized("dialog_timeout", self.lang))
                return None
            value = (payload.text or payload.caption or "").strip()
            if validator is None:
                return "text", value
            ok = validator(value)
            if ok is not None:
                return "text", ok
            pending = error_text or localized("choice_invalid", self.lang)
        await self.send(localized("dialog_timeout", self.lang))
        return None

    async def choose(self, header, items, render, *, extra=None):
        """Ask a question with buttons and wait for one to be pressed, or for a
        typed answer."""
        lines = [header] + [f"{i + 1}. {render(it)}" for i, it in enumerate(items)]
        if extra:
            lines.append(f"{len(items) + 1}. {extra}")
        total = len(items) + (1 if extra else 0)

        def _validator(value):
            """Accept only a language code the Olympiad content exists in."""
            v = value.strip().rstrip(".")
            return int(v) if v.isdigit() and 1 <= int(v) <= total else None

        answer = await self.ask("\n".join(lines), validator=_validator,
                                error_text=localized("choice_invalid", self.lang))
        if not answer:
            return None
        number = answer[1]
        if extra and number == total:
            return "__extra__"
        return items[number - 1]

    async def choose_numbers(self, header, items, render, limit):
        """Ask for up to `limit` numbers and parse what was typed, re-asking
        on anything that is not a valid selection."""
        lines = [header, ""] + [f"{i + 1}. {render(it)}" for i, it in enumerate(items)]
        answer = await self.ask(
            "\n".join(lines),
            validator=lambda v: olympiad.parse_numbers(v, len(items), limit),
            error_text=olympiad.text("candidates_invalid", self.lang, max=limit))
        return answer[1] if answer else None

@router.message(Command("setolympiad"))
async def setolympiad_tg(message: Message):
    """Open the Olympiad and fix its voting period, or cancel it with 'off'
    (Bot Admins).

    Also re-syncs the Discord command tree, since the five runtime commands appear
    and disappear there rather than here."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split()
    if len(parts) == 2 and parts[1].strip().lower() in ("off", "stop", "cancel"):
        if not db.get_olympiad():
            await message.reply(olympiad.text("setolympiad_off_none", lang))
            return
        db.clear_olympiad_data()
        db.clear_olympiad()
        await message.reply(olympiad.text("setolympiad_off", lang))
        await sync_olympiad_commands()
        return

    if len(parts) != 3:
        await message.reply(olympiad.text("setolympiad_usage", lang))
        return
    try:
        start_ts, end_ts = olympiad.parse_period(parts[1], parts[2])
    except ValueError as e:
        key = "setolympiad_bad_order" if str(e) == "bad_order" else "setolympiad_bad_date"
        await message.reply(olympiad.text(key, lang))
        return

    existed = db.get_olympiad() is not None
    db.set_olympiad(start_ts, end_ts)
    await message.reply(olympiad.text(
        "setolympiad_updated" if existed else "setolympiad_set", lang,
        start=olympiad.format_date(start_ts), end=olympiad.format_date(end_ts)))
    await sync_olympiad_commands()

async def _olympiad_admin_gate_tg(message: Message, lang):
    """The two setup commands are for Bot Admins, for as long as the Olympiad
    runs."""
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return False
    if not olympiad.is_open():
        await message.reply(olympiad.text("not_active", lang))
        return False
    return True

@router.message(Command("setcontest"))
async def setcontest_tg(message: Message):
    """Create a contest of the Olympiad through a dialog (Bot Admins)."""
    lang = get_chat_lang(_chat_key(message))
    if not await _olympiad_admin_gate_tg(message, lang):
        return
    await olympiad.run_setcontest(_OlyDialogTg(message, lang), lang)

@router.message(Command("editcontest"))
async def editcontest_tg(message: Message):
    """Manage a contest's candidate wikis through a dialog (Bot Admins)."""
    lang = get_chat_lang(_chat_key(message))
    if not await _olympiad_admin_gate_tg(message, lang):
        return
    await olympiad.run_editcontest(_OlyDialogTg(message, lang), lang)

async def _ask_dm_language_tg(dialog, message):
    """With no language chosen for this private chat yet, ask in English and
    offer a button per localization. The answer is remembered for the chat (and
    dropped again after a year). Returns the language code, or None."""
    token, fut = _new_oly_token(dialog.user_id, DEFAULT_LANG)
    keyboard = _oly_keyboard(token, [(language_name(c), c) for c in available_locales()],
                             per_row=2)
    await dialog.send(olympiad.text("ask_lang", DEFAULT_LANG), reply_markup=keyboard)
    code = await _await_oly_button(fut)
    _pending_oly_buttons.pop(token, None)
    if not code:
        return None
    set_chat_lang(_chat_key(message), code, is_dm=True)
    dialog.lang = code
    await dialog.send(olympiad.text("lang_chosen", code))
    return code

@router.message(Command("olympiad"))
async def olympiad_tg(message: Message):
    """Vote in the Olympiad — private chat with the bot only.

    The same dialog as on Discord, driven through inline keyboards and plain
    replies. The finished vote goes to the contest's review chat, which is always
    on Discord."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if message.chat.type != "private":
        await message.reply(olympiad.text("dm_only", lang))
        return
    period = olympiad.period()
    if not period:
        await message.reply(olympiad.text("not_active", lang))
        return
    if not olympiad.is_voting_open():
        await message.reply(olympiad.text("voting_not_started", lang,
                                          start=olympiad.format_date(period[0])))
        return

    dialog = _OlyDialogTg(message, lang)
    if db.get_chat_lang(_chat_key(message)) is None:
        code = await _ask_dm_language_tg(dialog, message)
        if code is None:
            return
        lang = code
    await olympiad.run_vote(dialog, lang, "telegram", message.from_user.id,
                            _tg_user_label(message.from_user), "Telegram",
                            post_olympiad_vote)
