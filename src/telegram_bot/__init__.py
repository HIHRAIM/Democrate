"""The Telegram half of Democrate, as a package.

Importing it builds the aiogram objects and registers every handler: the
import order below runs the @router.* decorators of each module, and aiogram
dispatches in exactly that order.

`catchall` is imported last of all, and that is the one line in this file that
must never move. `telegram_bot/catchall.py: _dialog_catchall` is a
`@router.message()` with no filter — it accepts every message that is not a
command, hands it to whatever dialog is waiting for it, and counts it for
earning. Registered before any command module, it would swallow all of them and
they would die silently: no error, no log line, just nothing happening. Every
other message handler carries a `Command(...)` filter and no two of them claim
the same command name, so their relative order cannot change behaviour.

The rest of the order is the dependency order: `client` first (it owns `bot`,
`dp` and `router`), then the dialog toolkit, then the party renderers, then the
four callback handlers, then the commands, then the Olympiad — whose four
commands sit just above the catchall, as they did in the flat module.

`callbacks` is imported *before* `commands` on purpose: aiogram keeps a
separate observer per update type, so this fixes the callback_query order
(party consent, quiz button, economy consent, Olympiad button) independently of
the commands. Everything those handlers need from a command module is imported
at the call site, which is what keeps that possible.

The re-exports are the package's public API: `bot` is the aiogram Bot every
other module reaches for as ``telegram_bot.bot``, and `main` is the polling
task main.py gathers. The handler callbacks are re-exported too — nobody calls
``telegram_bot.pay_tg``, aiogram does — because the flat telegram_bot.py had
them at this address, and a name that quietly disappears from
``dir(telegram_bot)`` is exactly the breakage a split should not cause. The
same goes for the stdlib, aiogram and utils imports below. Keep imports of the
Discord half lazy (at the call site) except where a module renders a shared
party card; those are at module level and safe, because the Discord half never
imports this one at module level.

One name is shadowed on purpose and worth knowing about: `telegram_bot.olympiad`
is this package's own submodule, not the top-level olympiad.py engine. Nothing
reads the attribute — every module that wants the engine says `import
olympiad` — but do not expect `import olympiad` here to survive it.
"""
import asyncio
import io
import json
import logging
import os
import re
import secrets
import time

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup,
    InlineKeyboardButton, BufferedInputFile,
)

import db
import economy
import olympiad
import quizzes
import utils
from config import TELEGRAM_TOKEN, SUPPORT_CHATS
from message_relay import (
    escape_html, telegram_entities_to_discord, discord_to_telegram_html,
    clean_display_name,
)
from utils import (
    is_admin, rate_limit_ok, get_chat_lang, set_chat_lang,
    localized, localized_help, language_name,
    available_locales, locale_stats, locale_bar, compare_reply,
    LANG_ORDER, LOCALE_STATUS_EMOJI, SUPPORTED_LANGS, DEFAULT_LANG,
    PARTY_CODE_RE, format_stored_user, send_service_event, is_verified,
    format_quiz_date, resolve_previous_quiz, privacy_actions, privacy_option_labels,
    parse_tz_offset, format_tz_offset, parse_weekday, weekday_name,
    parse_founday_time, category_label, good_display_name,
)

from discord_bot import (
    olympiad_help_keys,
    parties_enabled,
    post_olympiad_vote,
    sync_olympiad_commands,
)

from telegram_bot.client import (
    GROUP_CHAT_TYPES,
    bot,
    dp,
    is_server_admin,
    logger,
    main,
    my_chat_member_update,
    router,
)
from telegram_bot.dialogs import DIALOG_TIMEOUT, TEMP_REPLY_SECONDS, TG_USER_REF_RE
from telegram_bot.parties import COLOR_RE, URL_RE
from telegram_bot import callbacks
from telegram_bot.callbacks import (
    handle_econ_consent,
    handle_olympiad_button,
    handle_party_consent,
    handle_quiz_button,
)
from telegram_bot import commands
from telegram_bot.commands.unions import (
    UNION_CODE_RE,
    add_unia_cmd,
    allow_parties_tg,
    force_leave_cmd,
    list_chats_cmd,
    setup_cmd,
)
from telegram_bot.commands.settings import (
    lang_cmd,
    locallang_cmd,
    setlogs_tg,
    settasks_tg,
)
from telegram_bot.commands.admins import (
    backup_cmd,
    localizer_add_cmd,
    localizer_rem_cmd,
    remadmin_cmd,
    setadmin_cmd,
)
from telegram_bot.commands.locale import (
    loc_compare_cmd,
    loc_reply_cmd,
    loc_suggest_cmd,
    locale_cmd,
)
from telegram_bot.commands.user import (
    add_discord_tg,
    help_cmd,
    privacy_tg,
)
from telegram_bot.commands.parties import (
    add_party_tg,
    edit_party_admin_tg,
    edit_party_tg,
    edit_rules_tg,
    party_join_tg,
    party_kick_tg,
    party_leave_tg,
    party_tg,
)
from telegram_bot.commands.govt import add_govt_tg, govt_tg
from telegram_bot.commands.quizzes import (
    quizzes_clear_tg,
    quizzes_compare_tg,
    quizzes_previous_tg,
    quizzes_stop_tg,
    quizzes_tg,
)
from telegram_bot.commands.banks import (
    balance_tg,
    bank_add_leader_tg,
    bank_transfer_tg,
    bank_tg,
    create_bank_tg,
    edit_bank_tg,
    fine_tg,
    open_account_tg,
    pay_tg,
    set_earn_tg,
)
from telegram_bot.commands.trade import (
    convert_tg,
    party_dues_tg,
    rates_tg,
    set_rate_tg,
    set_wage_tg,
    treaty_tg,
)
from telegram_bot.commands.goods import (
    autocraft_tg,
    autosend_tg,
    craft_tg,
    create_good_tg,
    give_good_tg,
    goods_tg,
    inventory_tg,
    sell_tg,
    set_profession_tg,
)
from telegram_bot.commands.enterprises import (
    add_enterprise_tg,
    auto_export_tg,
    edit_enterprise_tg,
    ent_assign_tg,
    ent_join_tg,
    ent_kick_tg,
    ent_leave_tg,
    ent_position_tg,
    ent_salary_tg,
    ent_sell_tg,
    enterprise_tg,
    export_tg,
    transit_tg,
)
from telegram_bot.olympiad import (
    editcontest_tg,
    olympiad_tg,
    setcontest_tg,
    setolympiad_tg,
)
from telegram_bot import catchall
