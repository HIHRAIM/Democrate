"""How the Telegram half holds a conversation: the pending-answer registries,
the wait helpers, the logo reader, the numbered choice and every consent
keyboard.

This is where the two halves differ most. Discord can block on
`bot.wait_for(...)` inside a command; aiogram has no such facility, so
`_wait_user_message` parks a future in `_pending_inputs` keyed by
`(chat_id, user_id)` and it is telegram_bot/catchall.py: _dialog_catchall — a
`@router.message()` with no filter at all — that receives the answer and
resolves the future. That is why the catchall must be registered last, and why
a dialog that never gets its answer simply times out after DIALOG_TIMEOUT.

Every registry in this module must exist exactly once, and they all work the
same way: an inline button carries a short random token, the pending decision
is stored under that token, and the callback handler in
telegram_bot/callbacks.py looks it up. Two modules each declaring "their own"
copy is the classic split bug — the button writes one dict, the waiting dialog
reads the other, and the dialog hangs until it times out. `_pending_inputs` and
`_pending_consents` serve the dialogs and party consent; `_pending_quiz_buttons`
serves the quiz option buttons; `_pending_econ_consents` serves the bank,
enterprise and export offers; `_pending_oly_buttons` serves the Olympiad's
buttons.

Not this module's zone: the callback handlers themselves
(telegram_bot/callbacks.py) and the catchall that feeds the futures
(telegram_bot/catchall.py).
"""
import asyncio
import io
import re
import secrets
import time

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

from message_relay import telegram_entities_to_discord
from utils import is_verified, localized

from telegram_bot.client import bot

TG_USER_REF_RE = re.compile(r"^@([A-Za-z0-9_]{3,64})$|^(\d{3,20})$")

DIALOG_TIMEOUT = 30 * 60

_pending_inputs = {}

_pending_consents = {}

async def _wait_user_message(chat_id, user_id, timeout=DIALOG_TIMEOUT):
    """Wait up to 30 minutes for the user's next non-command message in the chat."""
    key = (chat_id, user_id)
    old = _pending_inputs.pop(key, None)
    if old and not old.done():
        old.cancel()
    fut = asyncio.get_running_loop().create_future()
    _pending_inputs[key] = fut
    try:
        return await asyncio.wait_for(fut, timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None
    finally:
        if _pending_inputs.get(key) is fut:
            _pending_inputs.pop(key, None)

async def _wait_user_message_or_stop(chat_id, user_id, stop_event, timeout=DIALOG_TIMEOUT):
    """Race the taker's next message against a /quizzes-stop. Returns
    ('message', msg), ('stop', None) or ('timeout', None)."""
    key = (chat_id, user_id)
    old = _pending_inputs.pop(key, None)
    if old and not old.done():
        old.cancel()
    fut = asyncio.get_running_loop().create_future()
    _pending_inputs[key] = fut
    stop_task = asyncio.ensure_future(stop_event.wait())
    try:
        done, pending = await asyncio.wait({fut, stop_task}, timeout=timeout,
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if stop_task in done:
            return "stop", None
        if fut in done:
            try:
                return "message", fut.result()
            except Exception:
                return "timeout", None
        return "timeout", None
    finally:
        if _pending_inputs.get(key) is fut:
            _pending_inputs.pop(key, None)

async def _dialog_text_tg(message, lang, prompt, *, validator=None, error_key=None,
                          attempts=5):
    """Ask `prompt` and wait for the caller's answer, re-asking on invalid input.
    Returns the validated value (or the Message when no validator) or None."""
    await message.reply(prompt)
    for _ in range(attempts):
        m = await _wait_user_message(message.chat.id, message.from_user.id)
        if m is None:
            await message.reply(localized("dialog_timeout", lang))
            return None
        if validator is None:
            return m
        value = (m.text or m.caption or "").strip()
        ok = validator(value)
        if ok is not None:
            return ok
        await message.reply(localized(error_key, lang))
    await message.reply(localized("dialog_timeout", lang))
    return None

async def _extract_logo_tg(m):
    """Return (bytes, mime) if the message carries a photo or an image document."""
    file_id = None
    mime = "image/jpeg"
    if m.photo:
        file_id = m.photo[-1].file_id
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        if m.document.file_size and m.document.file_size > 8 * 1024 * 1024:
            return None
        file_id = m.document.file_id
        mime = m.document.mime_type
    if not file_id:
        return None
    try:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        return buf.getvalue(), mime
    except Exception:
        return None

async def _dialog_logo_tg(message, lang, prompt, attempts=5):
    """Ask for a logo and wait for a photo or an image document, re-asking on
    anything else.

    Telegram delivers the same picture two ways — compressed as a photo, or
    untouched as a document — so both are accepted. Returns (bytes, mime) or None
    on timeout."""
    await message.reply(prompt)
    for _ in range(attempts):
        m = await _wait_user_message(message.chat.id, message.from_user.id)
        if m is None:
            await message.reply(localized("dialog_timeout", lang))
            return None
        logo = await _extract_logo_tg(m)
        if logo:
            return logo
        await message.reply(localized("add_party_invalid_logo", lang))
    await message.reply(localized("dialog_timeout", lang))
    return None

async def _numbered_choice_tg(message, lang, header, items, render_line):
    """Send a numbered list and wait for the caller to reply with a number.

    The Telegram counterpart of the Discord numbered choice, and it waits the same
    way every dialog here does: through `_pending_inputs` and the catchall."""
    lines = [header] + [f"{i + 1}. {render_line(it)}" for i, it in enumerate(items)]
    await message.reply("\n".join(lines))
    m = await _wait_user_message(message.chat.id, message.from_user.id)
    if m is None:
        return None
    value = (m.text or "").strip().rstrip(".")
    if value.isdigit() and 1 <= int(value) <= len(items):
        return items[int(value) - 1]
    await message.reply(localized("choice_invalid", lang))
    return None

def _parse_tg_user_ref(text):
    """@username or numeric ID -> ('username', name) / ('id', int) / None."""
    m = TG_USER_REF_RE.match((text or "").strip())
    if not m:
        return None
    if m.group(1):
        return "username", m.group(1)
    return "id", int(m.group(2))

async def _resolve_tg_target(message: Message, arg):
    """Resolve the compare target to a numeric Telegram id: a reply, a
    text-mention entity, a numeric id, or a public @username."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    for ent in (message.entities or []):
        if ent.type == "text_mention" and ent.user:
            return ent.user.id
    ref = _parse_tg_user_ref(arg)
    if ref is None:
        return None
    if ref[0] == "id":
        return ref[1]
    try:
        chat = await bot.get_chat("@" + ref[1])
        return chat.id
    except Exception:
        return None

async def _quiz_target_label_tg(target_id):
    """A display name for a Telegram id: '@username', a full name, or the
    bare id when the bot cannot see the account.

    The name is misleading — it is used far beyond the quizzes, by `/pay`,
    `/fine`, `/give_good` and every enterprise command that names somebody — but
    renaming it would break the imports of a dozen call sites."""
    try:
        chat = await bot.get_chat(int(target_id))
        if getattr(chat, "username", None):
            return "@" + chat.username
        return getattr(chat, "full_name", None) or str(target_id)
    except Exception:
        return str(target_id)

TEMP_REPLY_SECONDS = 60

async def _reply_temp(message: Message, text, seconds=TEMP_REPLY_SECONDS, **kwargs):
    """Reply with a message that clears itself after a while — Telegram's
    nearest equivalent of Discord's ephemeral answers. Used for administrative
    confirmations that would otherwise sit in a live chat forever. The removal
    runs as its own task, so the handler never waits on it."""
    sent = await message.reply(text, **kwargs)

    async def _remove():
        """Delete the temporary message after its time is up, ignoring a message
        somebody already deleted."""
        await asyncio.sleep(seconds)
        try:
            await sent.delete()
        except Exception:
            pass

    asyncio.create_task(_remove())
    return sent

async def _require_verified_tg(message: Message, lang):
    """Gate the Fandom-activity commands on Telegram. Returns True when the
    caller is verified (linked to a Fandom-verified Discord account)."""
    if message.from_user and is_verified("telegram", message.from_user.id):
        return True
    await message.reply(localized("verify_required_tg", lang))
    return False

def _markdown_from_tg(m):
    """Canonical (Discord-markdown) text of a Telegram message, links preserved."""
    if m.text is not None:
        return telegram_entities_to_discord(m.text, m.entities).strip()
    return telegram_entities_to_discord(m.caption or "", m.caption_entities).strip()

def _consent_keyboard(lang, token):
    """The two-button Accept/Decline keyboard for a party decision, carrying
    the token the callback will look up."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=localized("consent_accept", lang),
                             callback_data=f"pc:{token}:1"),
        InlineKeyboardButton(text=localized("consent_decline", lang),
                             callback_data=f"pc:{token}:0"),
    ]])

def _register_consent(action, party_code, target_kind, target, lang):
    """Park a pending party decision under a fresh random token.

    The token, not the decision, travels in the button; a token that is no longer
    in the registry — because the bot restarted, or because somebody already
    answered — makes the handler say the offer expired instead of acting."""
    token = secrets.token_hex(8)
    _pending_consents[token] = {
        "action": action,
        "party_code": party_code,
        "target_kind": target_kind,
        "target": target,
        "lang": lang,
        "created": time.time(),
    }
    return token

_pending_quiz_buttons = {}

def _new_quiz_button_token(user_id, lang):
    """Register a future under a fresh token for one set of quiz buttons."""
    token = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    _pending_quiz_buttons[token] = {
        "future": fut, "user_id": user_id, "lang": lang, "created": time.time()}
    return token, fut

async def _await_quiz_button(fut, timeout=DIALOG_TIMEOUT):
    """Wait for one of a set of quiz buttons to be pressed, or give up.

    Always raced against a typed answer: on Telegram a quiz may be answered either
    way, and whichever arrives first wins."""
    try:
        return await asyncio.wait_for(fut, timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None

_pending_econ_consents = {}

def _econ_consent_keyboard(lang, token):
    """The Accept/Decline keyboard for a bank, enterprise or export
    decision."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=localized("consent_accept", lang),
                             callback_data=f"ec:{token}:1"),
        InlineKeyboardButton(text=localized("consent_decline", lang),
                             callback_data=f"ec:{token}:0"),
    ]])

def _register_econ_consent(data):
    """Park a pending economy decision under a fresh random token.

    Kept apart from the party registry because the payloads are different and the
    callback prefixes are different ('ec:' against 'pc:') — one dictionary with two
    shapes in it would be the harder thing to read."""
    token = secrets.token_hex(8)
    data["created"] = time.time()
    _pending_econ_consents[token] = data
    return token

_pending_oly_buttons = {}

def _new_oly_token(user_id, lang):
    """Register a future under a fresh token for one set of Olympiad
    buttons."""
    token = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    _pending_oly_buttons[token] = {"future": fut, "user_id": user_id, "lang": lang,
                                   "created": time.time()}
    return token, fut

def _oly_keyboard(token, buttons, per_row=1):
    """Build an Olympiad button row from (label, value) pairs, carrying the
    token."""
    rows, current = [], []
    for label, value in buttons:
        current.append(InlineKeyboardButton(text=label,
                                            callback_data=f"ob:{token}:{value}"))
        if len(current) == per_row:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _await_oly_button(fut, timeout=DIALOG_TIMEOUT):
    """Wait for an Olympiad button, or give up after the timeout."""
    try:
        return await asyncio.wait_for(fut, timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None
