"""The Discord half of Democrate, as a package.

Importing it builds the client and registers everything: the import order
below runs the @bot.event and @bot.tree.command decorators of every module.
The order is the dependency order — `client` first (it owns `bot`, which every
decorator hangs on), then the shared dialog toolkit, then the party helpers
both halves render cards from, then the Olympiad, then the events, and
`commands` last. Append new modules in a place that respects it, and remember
the Olympiad's five runtime commands live in olympiad.py rather than under
commands/, because they are added to and removed from the tree while the bot
runs.

The re-exports are the package's public API: everything main.py, utils.py and
the Telegram half import from `discord_bot`. Several of them are private by
name (`_search_party`, `_edit_menu_text`, `_resume_state`, …) and re-exported
anyway — the Telegram half renders the same party cards and menus from them,
which is deliberate, because what a card contains is a fact about the union
while only its formatting is a fact about the platform. Keep the Telegram
side's other imports lazy (at the call site), as everywhere else.

The command callbacks are re-exported as well, which looks redundant — nobody
calls `discord_bot.pay_cmd`, Discord does. They are here because the flat
discord_bot.py had them at this address, and a name that silently disappears
from ``dir(discord_bot)`` is exactly the kind of breakage a split is supposed
not to cause. The same goes for the stdlib, discord and utils imports below.

One name is shadowed on purpose and worth knowing about: `discord_bot.olympiad`
is this package's own submodule, not the top-level olympiad.py engine. Nothing
reads the attribute — every module that wants the engine says `import
olympiad` — but do not add `import olympiad` here expecting it to survive.
"""
import asyncio
import io
import json
import logging
import os
import re
import secrets
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands, ui, ButtonStyle

import db
import economy
import fandom
import quizzes
import utils
from config import SUPPORT_CHATS
from message_relay import clean_display_name
from utils import (
    is_admin, rate_limit_ok, get_chat_lang, set_chat_lang,
    localized, localized_help, language_name,
    available_locales, locale_stats, locale_bar, compare_reply,
    LANG_ORDER, LOCALE_STATUS_EMOJI, SUPPORTED_LANGS, DEFAULT_LANG, STATUS_TEXT,
    DEFAULT_EMBED_COLOR, PARTY_CODE_RE, parse_find_period, parse_keywords,
    find_close_names, format_stored_user, send_service_event, is_verified,
    format_quiz_date, resolve_previous_quiz, privacy_actions, privacy_option_labels,
    normalize_wiki_url, parse_founday_datetime, parse_founday_time,
    utc_from_epoch, is_founday_today, founday_message,
    parse_tz_offset, format_tz_offset, parse_weekday, weekday_name,
    category_label, good_display_name,
)

from discord_bot.client import (
    DemBot,
    bot,
    is_server_admin,
    logger,
    on_app_command_error,
    parties_enabled,
)
from discord_bot.dialogs import DIALOG_TIMEOUT, USER_REF_RE
from discord_bot.parties import (
    COLOR_RE,
    URL_RE,
    _edit_admin_menu_text,
    _edit_menu_text,
    _leaders_differ_from_founder,
    _party_balance_lines,
    _party_line,
    _rules_menu_text,
    _search_party,
)
from discord_bot.olympiad import (
    OLYMPIAD_COMMANDS,
    accepted_cmd,
    denied_cmd,
    dm_olympiad_voter,
    editcontest_cmd,
    olympiad_cmd,
    olympiad_help_keys,
    post_olympiad_vote,
    setcontest_cmd,
    setolympiad_cmd,
    sync_olympiad_commands,
)
from discord_bot import events
from discord_bot.events import on_guild_join, on_guild_remove, on_message
from discord_bot import commands
from discord_bot.commands.unions import (
    UNION_CODE_RE,
    add_unia_cmd,
    allow_parties_cmd,
    force_leave,
    list_chats,
    setup_cmd,
)
from discord_bot.commands.settings import (
    lang_command,
    locallang_command,
    setlogs_cmd,
    settasks_cmd,
)
from discord_bot.commands.admins import (
    backup_discord_cmd,
    localizer_add_cmd,
    localizer_rem_cmd,
    remadmin_cmd,
    setadmin_cmd,
)
from discord_bot.commands.locale import (
    loc_compare_cmd,
    loc_reply_cmd,
    loc_suggest_cmd,
    locale_cmd,
    post_loc_reply,
    post_loc_suggestion,
)
from discord_bot.commands.user import (
    EMBED_FIELD_LIMIT,
    HELP_SECTIONS,
    add_telegram_cmd,
    help_command,
    privacy_cmd,
    verify_cmd,
)
from discord_bot.commands.parties import (
    JoinRequestView,
    add_party_cmd,
    edit_party_admin_cmd,
    edit_party_cmd,
    edit_rules_cmd,
    party_cmd,
    party_join_cmd,
    party_kick_cmd,
    party_leave_cmd,
)
from discord_bot.commands.govt import (
    _govt_member_lines,
    _search_govt,
    add_govt_cmd,
    govt_cmd,
)
from discord_bot.commands.quizzes import (
    _resume_state,
    quizzes_clear_cmd,
    quizzes_cmd,
    quizzes_compare_cmd,
    quizzes_previous_cmd,
    quizzes_stop_cmd,
)
from discord_bot.commands.moderation import find_with_cmd, find_without_cmd
from discord_bot.commands.foundays import (
    CHANNEL_REF_RE,
    wiki_founday_cmd,
    wiki_founday_remove_cmd,
    wiki_foundays_cmd,
)
from discord_bot.commands.banks import (
    balance_cmd,
    bank_add_leader_cmd,
    bank_cmd,
    bank_transfer_cmd,
    create_bank_cmd,
    edit_bank_cmd,
    fine_cmd,
    open_account_cmd,
    pay_cmd,
    set_earn_cmd,
)
from discord_bot.commands.trade import (
    convert_cmd,
    party_dues_cmd,
    rates_cmd,
    set_rate_cmd,
    set_wage_cmd,
    treaty_cmd,
)
from discord_bot.commands.goods import (
    autocraft_cmd,
    autosend_cmd,
    craft_cmd,
    create_good_cmd,
    give_good_cmd,
    goods_cmd,
    inventory_cmd,
    sell_cmd,
    set_profession_cmd,
)
from discord_bot.commands.enterprises import (
    add_enterprise_cmd,
    auto_export_cmd,
    edit_enterprise_cmd,
    ent_assign_cmd,
    ent_join_cmd,
    ent_kick_cmd,
    ent_leave_cmd,
    ent_position_cmd,
    ent_salary_cmd,
    ent_sell_cmd,
    enterprise_cmd,
    export_cmd,
    transit_cmd,
)
