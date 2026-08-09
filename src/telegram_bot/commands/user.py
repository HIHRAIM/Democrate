"""What an ordinary member runs about themselves on Telegram: `/help`,
`/add_discord`, `/privacy`.

`help_cmd` is a hand-written list of localization keys, like HELP_SECTIONS on
the Discord side, and it carries the same trap: a command moved between modules
keeps working while quietly vanishing from the help. It is also chunked by
hand, because Telegram caps a message at 4096 characters and there is no
paginated embed to fall back on. The Olympiad block lists only `/setolympiad`
while no Olympiad runs — the rest of its commands refuse to answer then, so
listing them would point at nothing.

There is no `/verify` here: Fandom verification happens on Discord, and
`/add_discord` is one half of the thirty-minute handshake that lends a Telegram
account that verification.

Not this module's zone: the identity model (db/users.py) and the quiz data the
privacy actions delete (db/quizzes.py).
"""
import asyncio

from aiogram.filters import Command
from aiogram.types import Message

import db
import olympiad
import quizzes
from message_relay import escape_html
from utils import (
    get_chat_lang, localized, localized_help, privacy_actions,
    privacy_option_labels, rate_limit_ok,
)

from discord_bot import olympiad_help_keys
from telegram_bot.client import _chat_key, router
from telegram_bot.dialogs import _numbered_choice_tg, _wait_user_message

@router.message(Command("help"))
async def help_cmd(message: Message):
    """Show the command list.

    Hand-written, like HELP_SECTIONS on the Discord side, and chunked by hand
    because Telegram caps a message at 4096 characters and there is no paginated
    embed to fall back on. The chunks are sent together and delete themselves after
    a minute, so a help request does not sit in a group forever.

    The Olympiad block lists only `/setolympiad` while no Olympiad runs — the
    rest of its commands do not answer then, so listing them would point at
    nothing."""
    chat_key = _chat_key(message)
    lang = get_chat_lang(chat_key)

    requester = message.from_user.id if message.from_user else message.chat.id
    if not rate_limit_ok(("help-cmd", "telegram", requester), limit=5, window_seconds=60):
        return

    async def _reply_autodelete(text: str):
        """Send one chunk and remove it after a minute."""
        sent = await message.reply(text, parse_mode="HTML")
        await asyncio.sleep(60)
        try:
            await sent.delete()
        except Exception:
            pass

    everyone_lines = "\n".join([
        escape_html(localized_help("cmd_add_discord", lang)),
        escape_html(localized_help("cmd_party", lang)),
        escape_html(localized_help("cmd_govt", lang)),
        escape_html(localized_help("cmd_add_party_tg", lang)),
        escape_html(localized_help("cmd_edit_party_tg", lang)),
        escape_html(localized_help("cmd_edit_rules_tg", lang)),
        escape_html(localized_help("cmd_party_join_tg", lang)),
        escape_html(localized_help("cmd_party_leave_tg", lang)),
        escape_html(localized_help("cmd_party_kick_tg", lang)),
        escape_html(localized_help("cmd_quizzes", lang)),
        escape_html(localized_help("cmd_quizzes_stop_tg", lang)),
        escape_html(localized_help("cmd_quizzes_clear_tg", lang)),
        escape_html(localized_help("cmd_quizzes_compare_tg", lang)),
        escape_html(localized_help("cmd_quizzes_previous_tg", lang)),
        escape_html(localized_help("cmd_privacy", lang)),
        escape_html(localized_help("cmd_locale", lang)),
        escape_html(localized_help("cmd_loc_compare_tg", lang)),
        escape_html(localized_help("cmd_loc_suggest_tg", lang)),
        escape_html(localized_help("cmd_help", lang)),
    ])

    admins_lines = "\n".join([
        escape_html(localized_help("cmd_lang", lang)),
        escape_html(localized_help("cmd_locallang", lang)),
        escape_html(localized_help("cmd_setlogs", lang)),
        escape_html(localized_help("cmd_settasks", lang)),
    ])

    bot_admins_lines = "\n".join([
        escape_html(localized_help("cmd_setup", lang)),
        escape_html(localized_help("cmd_setadmin_tg", lang)),
        escape_html(localized_help("cmd_remadmin_tg", lang)),
        escape_html(localized_help("cmd_localizer_add_tg", lang)),
        escape_html(localized_help("cmd_localizer_rem_tg", lang)),
        escape_html(localized_help("cmd_add_unia_tg", lang)),
        escape_html(localized_help("cmd_allow_parties_tg", lang)),
        escape_html(localized_help("cmd_add_govt_tg", lang)),
        escape_html(localized_help("cmd_edit_party_admin_tg", lang)),
        escape_html(localized_help("cmd_loc_reply_tg", lang)),
        escape_html(localized_help("cmd_list_chats", lang)),
        escape_html(localized_help("cmd_force_leave", lang)),
        escape_html(localized_help("cmd_backup", lang)),
    ])

    economy_lines = "\n".join([
        escape_html(localized_help("cmd_create_bank", lang)),
        escape_html(localized_help("cmd_bank", lang)),
        escape_html(localized_help("cmd_edit_bank_tg", lang)),
        escape_html(localized_help("cmd_bank_add_leader", lang)),
        escape_html(localized_help("cmd_bank_transfer", lang)),
        escape_html(localized_help("cmd_open_account", lang)),
        escape_html(localized_help("cmd_balance", lang)),
        escape_html(localized_help("cmd_pay", lang)),
        escape_html(localized_help("cmd_set_earn", lang)),
        escape_html(localized_help("cmd_create_good", lang)),
        escape_html(localized_help("cmd_goods", lang)),
        escape_html(localized_help("cmd_craft", lang)),
        escape_html(localized_help("cmd_inventory", lang)),
        escape_html(localized_help("cmd_sell", lang)),
        escape_html(localized_help("cmd_give_good", lang)),
        escape_html(localized_help("cmd_autocraft", lang)),
        escape_html(localized_help("cmd_autosend", lang)),
        escape_html(localized_help("cmd_set_profession", lang)),
        escape_html(localized_help("cmd_fine", lang)),
        escape_html(localized_help("cmd_treaty", lang)),
        escape_html(localized_help("cmd_set_rate", lang)),
        escape_html(localized_help("cmd_convert", lang)),
        escape_html(localized_help("cmd_rates", lang)),
        escape_html(localized_help("cmd_set_wage", lang)),
        escape_html(localized_help("cmd_party_dues", lang)),
    ])

    enterprise_lines = "\n".join([
        escape_html(localized_help("cmd_add_enterprise", lang)),
        escape_html(localized_help("cmd_enterprise", lang)),
        escape_html(localized_help("cmd_edit_enterprise", lang)),
        escape_html(localized_help("cmd_ent_join", lang)),
        escape_html(localized_help("cmd_ent_leave", lang)),
        escape_html(localized_help("cmd_ent_kick", lang)),
        escape_html(localized_help("cmd_ent_position", lang)),
        escape_html(localized_help("cmd_ent_assign", lang)),
        escape_html(localized_help("cmd_ent_salary", lang)),
        escape_html(localized_help("cmd_ent_sell", lang)),
        escape_html(localized_help("cmd_export", lang)),
        escape_html(localized_help("cmd_auto_export", lang)),
        escape_html(localized_help("cmd_transit", lang)),
    ])

    olympiad_lines = "\n".join(
        escape_html(olympiad.text(k, lang)) for k in olympiad_help_keys())

    blocks = [
        f"<b>{localized_help('title', lang)}</b>\n\n"
        f"<b>{localized_help('section_everyone', lang)}</b>\n{everyone_lines}",
        f"<b>{localized_help('section_economy', lang)}</b>\n{economy_lines}",
        f"<b>{localized_help('section_enterprises', lang)}</b>\n{enterprise_lines}",
        f"<b>{localized_help('section_admins', lang)}</b>\n{admins_lines}\n\n"
        f"<b>{localized_help('section_bot_admins', lang)}</b>\n{bot_admins_lines}\n\n"
        f"<b>{escape_html(olympiad.text('section_title', lang))}</b>\n{olympiad_lines}",
    ]
    chunks, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > 3900:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    await asyncio.gather(*(_reply_autodelete(chunk) for chunk in chunks))

@router.message(Command("add_discord", "add-discord"))
async def add_discord_tg(message: Message):
    """Start linking your Discord account: name your Discord username here
    and run `/add-telegram` there within thirty minutes.

    This is what lends a Telegram account the Fandom verification done on Discord —
    there is no `/verify` on this side."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if not message.from_user.username:
        await message.reply(localized("link_need_username", lang))
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("link_usage_telegram", lang))
        return

    discord_nick = parts[1].strip().lstrip("@")
    result, dc_id, _tg = db.register_link_attempt(
        "telegram", message.from_user.id, message.from_user.username, discord_nick)
    if result == "linked":
        if db.is_discord_verified(dc_id):
            await message.reply(localized("link_success", lang))
        else:
            await message.reply(localized("link_success_unverified", lang))
    else:
        await message.reply(localized("link_pending_telegram", lang, nick=discord_nick))

async def _run_privacy_action_tg(message: Message, lang, action):
    """Run one privacy action: show what is stored, delete quiz results, or
    toggle comparison. Acts across the caller's linked accounts."""
    user_id = message.from_user.id

    if action == "clear_all":
        db.delete_user_quiz_results("telegram", user_id)
        await message.answer(localized("privacy_cleared_all", lang))
        return

    if action == "block_all":
        blocked = not db.is_quiz_compare_blocked("telegram", user_id)
        db.set_quiz_compare_blocked("telegram", user_id, "", blocked)
        await message.answer(localized(
            "privacy_block_all_on" if blocked else "privacy_block_all_off", lang))
        return

    quiz_ids = db.get_user_quiz_ids("telegram", user_id)

    if action == "clear_one":
        quiz_id = await _numbered_choice_tg(message, lang,
                                            localized("privacy_choose_quiz_clear", lang),
                                            quiz_ids, lambda q: quizzes.quiz_name(q, lang))
        if quiz_id is None:
            return
        db.delete_user_quiz_results("telegram", user_id, quiz_id)
        await message.answer(localized("privacy_cleared_quiz", lang,
                                       name=quizzes.quiz_name(quiz_id, lang)))
        return

    if action == "block_one":
        blocked_ids = db.get_blocked_quiz_ids("telegram", user_id)

        def render(q):
            """One line naming a privacy action in the numbered menu."""
            state = localized("state_yes" if q in blocked_ids else "state_no", lang)
            return f"{quizzes.quiz_name(q, lang)} ({state})"

        quiz_id = await _numbered_choice_tg(message, lang,
                                            localized("privacy_choose_quiz_block", lang),
                                            quiz_ids, render)
        if quiz_id is None:
            return
        blocked = quiz_id not in blocked_ids
        db.set_quiz_compare_blocked("telegram", user_id, quiz_id, blocked)
        await message.answer(localized(
            "privacy_block_quiz_on" if blocked else "privacy_block_quiz_off", lang,
            name=quizzes.quiz_name(quiz_id, lang)))

@router.message(Command("privacy"))
async def privacy_tg(message: Message):
    """Manage the data the bot keeps about you. Deletions here are immediate
    and not undoable."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    user_id = message.from_user.id

    actions = privacy_actions("telegram", user_id)
    if not actions:
        await message.reply(localized("privacy_none", lang))
        return

    labels = privacy_option_labels("telegram", user_id, lang, actions)
    lines = [localized("privacy_header", lang)]
    lines += [f"{i + 1}. {text}" for i, text in enumerate(labels)]
    await message.reply("\n".join(lines))

    reply = await _wait_user_message(message.chat.id, user_id)
    if reply is None:
        return
    value = (reply.text or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(actions)):
        await message.reply(localized("choice_invalid", lang))
        return
    await _run_privacy_action_tg(message, lang, actions[int(value) - 1])
