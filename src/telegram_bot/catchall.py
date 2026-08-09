"""The unfiltered handler, and the message earning behind it.

`_dialog_catchall` is a `@router.message()` with no filter: it sees every
message that is not a command. It does two things — hands the text to whatever
dialog is waiting for it (`telegram_bot/dialogs.py: _pending_inputs`) and
counts the message for earning.

**This module must be imported last of all.** aiogram dispatches in
registration order and registration order is import order, so a catchall
registered before any command module would swallow every command below it and
they would die silently — no error, no log line, just nothing happening. That
is the single reason this file exists separately instead of living at the
bottom of another module: a file cannot be imported half way.

Not this module's zone: the earning rules (economy/activity.py) and the dialog
machinery whose futures this feeds (telegram_bot/dialogs.py).
"""
from aiogram.types import Message

import db
import economy

from telegram_bot.client import (
    GROUP_CHAT_TYPES, _chat_key, _tg_user_label, logger, router,
)
from telegram_bot.dialogs import _pending_inputs

@router.message()
async def _dialog_catchall(message: Message):
    """Every message that is not a command.

    Two jobs, in this order: hand the text to a dialog waiting for it
    (`_pending_inputs`), and otherwise count the message for earning. A message
    that feeds a dialog is deliberately not counted — it is an answer to the bot,
    not a conversation in the chat.

    **Registered last of all handlers.** It has no filter, so aiogram would hand it
    every command as well if it came earlier, and every command below it would die
    silently."""
    if not message.from_user:
        return
    if (message.text or "").startswith("/"):
        return
    _try_earn_tg(message)
    key = (message.chat.id, message.from_user.id)
    fut = _pending_inputs.get(key)
    if fut and not fut.done():
        fut.set_result(message)

def _try_earn_tg(message: Message):
    """Message earning on Telegram. Runs from the catch-all (the last handler),
    so it sees ordinary group chatter without disturbing command handlers or the
    wait_for dialogs. Earns only in /set-earn earning topics, for humans (the
    economy needs no verification, so bots are skipped explicitly, as on
    Discord), subject to the same anti-abuse gate as Discord."""
    try:
        if message.chat.type not in GROUP_CHAT_TYPES:
            return
        text = (message.text or message.caption or "").strip()
        if not text:
            return
        chan_key = _chat_key(message)
        earn = db.get_earn_channel("telegram", chan_key)
        if not earn:
            return
        if not message.from_user or message.from_user.is_bot:
            return
        economy.earn_from_message("telegram", message.from_user.id,
                                  _tg_user_label(message.from_user),
                                  earn["bank_code"], len(text), earn["rate"])
    except Exception as e:
        logger.warning("telegram earning error: %s", e)
