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

logger = logging.getLogger("dem.telegram")

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

UNION_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,12}$")
COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")
URL_RE = re.compile(r"^https?://\S+$")
TG_USER_REF_RE = re.compile(r"^@([A-Za-z0-9_]{3,64})$|^(\d{3,20})$")

DIALOG_TIMEOUT = 30 * 60

GROUP_CHAT_TYPES = ("group", "supergroup")

def _chat_key(message: Message):
    thread = message.message_thread_id or 0
    return f"{message.chat.id}:{thread}"

def _tg_user_label(user):
    """'@username' when available, otherwise the full name."""
    if user is None:
        return "?"
    if getattr(user, "username", None):
        return f"@{user.username}"
    return user.full_name or str(user.id)

async def is_server_admin(chat_id: int, user_id: int):
    """Server Admins on Telegram are the group's native administrators,
    plus users delegated with /setadmin."""
    if db.is_server_admin("telegram", chat_id, user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False

async def _resolve_tg_admin_target(message: Message, arg):
    """Resolve an admin-command target to (user_id, username|None):
    a reply, a text-mention entity, a numeric id, or a public @username."""
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
    `/setup` and the Discord side's on_guild_remove touch that."""
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

@router.message(Command("setadmin"))
async def setadmin_cmd(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    has_reply = message.reply_to_message and message.reply_to_message.from_user
    if not arg and not has_reply:
        await message.reply(localized("setadmin_usage", lang))
        return

    uid, username = await _resolve_tg_admin_target(message, arg)
    if uid is None:
        await message.reply(localized("could_not_resolve_user", lang))
        return

    if db.is_server_admin("telegram", message.chat.id, uid):
        await message.reply(localized("setadmin_already", lang, user_id=uid))
        return

    db.add_server_admin("telegram", message.chat.id, uid,
                        username=username, added_by=message.from_user.id)
    await message.reply(localized("setadmin_success", lang, user_id=uid))
    try:
        await bot.send_message(
            uid,
            localized("setadmin_dm", lang,
                      server=message.chat.title or str(message.chat.id))
        )
    except Exception:
        pass

@router.message(Command("remadmin"))
async def remadmin_cmd(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    has_reply = message.reply_to_message and message.reply_to_message.from_user
    if not arg and not has_reply:
        await message.reply(localized("remadmin_usage", lang))
        return

    uid, _ = await _resolve_tg_admin_target(message, arg)
    if uid is None:
        await message.reply(localized("could_not_resolve_user", lang))
        return

    if not db.is_server_admin("telegram", message.chat.id, uid):
        await message.reply(localized("remadmin_not_admin", lang, user_id=uid))
        return

    db.remove_server_admin("telegram", message.chat.id, uid)
    await message.reply(localized("remadmin_success", lang, user_id=uid))

@router.message(Command("localizer_add", "localizer-add"))
async def localizer_add_cmd(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    has_reply = message.reply_to_message and message.reply_to_message.from_user
    if not arg and not has_reply:
        await message.reply(localized("localizer_add_usage", lang))
        return

    uid, username = await _resolve_tg_admin_target(message, arg)
    if uid is None:
        await message.reply(localized("could_not_resolve_user", lang))
        return

    if db.is_localizer("telegram", uid):
        await message.reply(localized("localizer_add_already", lang, user_id=uid))
        return

    db.add_localizer("telegram", uid, username=username,
                     added_by=message.from_user.id)
    await message.reply(localized("localizer_add_done", lang, user_id=uid))
    try:
        await bot.send_message(uid, localized("localizer_add_dm", lang))
    except Exception:
        pass

@router.message(Command("localizer_rem", "localizer-rem"))
async def localizer_rem_cmd(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    has_reply = message.reply_to_message and message.reply_to_message.from_user
    if not arg and not has_reply:
        await message.reply(localized("localizer_rem_usage", lang))
        return

    uid, _ = await _resolve_tg_admin_target(message, arg)
    if uid is None:
        await message.reply(localized("could_not_resolve_user", lang))
        return

    if not db.remove_localizer("telegram", uid):
        await message.reply(localized("localizer_rem_not", lang, user_id=uid))
        return

    await message.reply(localized("localizer_rem_done", lang, user_id=uid))

@router.message(Command("setup"))
async def setup_cmd(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply(localized("setup_usage", lang))
        return

    union = parts[1].strip().upper()
    code = parts[2].strip().lower()
    if not db.union_exists(union):
        await message.reply(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes()))
        )
        return
    if code not in SUPPORTED_LANGS:
        await message.reply(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS)))
        )
        return

    db.setup_chat("telegram", message.chat.id, union, message.from_user.id)
    set_chat_lang(str(message.chat.id), code)
    await message.reply(
        localized("setup_success", code,
                  union=db.get_union_name(union, code), code=union,
                  lang_name=language_name(code), lang=code)
    )
    await send_service_event("setup_done", platform="Telegram",
                             chat=message.chat.title or str(message.chat.id),
                             union=union, user=_tg_user_label(message.from_user))

@router.message(Command("add_unia", "add-unia"))
async def add_unia_cmd(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(localized("add_unia_usage", lang))
        return

    code = parts[1].strip().upper()
    if not UNION_CODE_RE.match(code):
        await message.reply(localized("add_unia_invalid_code", lang))
        return

    names = {}
    for chunk in parts[2].split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not sep or key not in SUPPORTED_LANGS or not value:
            await message.reply(localized("add_unia_usage", lang))
            return
        names[key] = value
    if not names:
        await message.reply(localized("add_unia_need_name", lang))
        return

    if not db.add_union(code, names):
        await message.reply(localized("add_unia_exists", lang, code=code))
        return

    listed = "\n".join(f"{language_name(L)}: {names[L]}" for L in LANG_ORDER if L in names)
    await message.reply(localized("add_unia_created", lang, code=code, names=listed))
    await send_service_event("union_created", code=code,
                             user=_tg_user_label(message.from_user))

@router.message(Command("lang"))
async def lang_cmd(message: Message):
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

def _setup_gate_ok(message: Message):
    """Public commands work only in set-up groups (Bot Admins and private
    chats with the bot are exempt)."""
    if message.chat.type not in GROUP_CHAT_TYPES:
        return True
    if message.from_user and is_admin("telegram", message.from_user.id):
        return True
    return db.is_setup(message.chat.id)

@router.message(Command("help"))
async def help_cmd(message: Message):
    chat_key = _chat_key(message)
    lang = get_chat_lang(chat_key)

    requester = message.from_user.id if message.from_user else message.chat.id
    if not rate_limit_ok(("help-cmd", "telegram", requester), limit=5, window_seconds=60):
        return

    async def _reply_autodelete(text: str):
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

    # Only /setolympiad while no Olympiad runs — the rest of its commands do not
    # answer then, so listing them would point at nothing.
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

@router.message(Command("list_chats"))
async def list_chats_cmd(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    lines = [localized("list_chats_discord_header", lang)]
    try:
        from discord_bot import bot as dc_bot
        for g in dc_bot.guilds:
            row = db.get_chat(str(g.id))
            union = f" [{row['union_code']}]" if row else ""
            lines.append(f"- {g.name} — id: {g.id}{union}")
    except Exception:
        pass

    tg_ids = db.get_telegram_group_ids()
    if tg_ids:
        lines.append("\n" + localized("list_chats_telegram_header", lang))
        for pid in tg_ids:
            row = db.get_chat(pid)
            union = f" [{row['union_code']}]" if row else ""
            try:
                chat = await bot.get_chat(int(pid))
                title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(pid)
            except Exception:
                title = str(pid)
            lines.append(f"- {title} — id: {pid}{union}")
    else:
        lines.append("\n" + localized("list_chats_no_telegram", lang))

    msg = "\n".join(lines)
    if len(msg) > 4000:
        from aiogram.types import BufferedInputFile
        doc = BufferedInputFile(msg.encode("utf-8"), filename="chat_list.txt")
        await message.reply_document(doc, caption=localized("list_chats_too_long", lang))
    else:
        await message.reply(msg)

@router.message(Command("force_leave"))
async def force_leave_cmd(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply(localized("force_leave_usage", lang))
        return

    platform = parts[1].strip().lower()
    target_id = parts[2].strip()

    if platform == "discord":
        try:
            gid = int(target_id)
        except ValueError:
            await message.reply(localized("force_leave_invalid_id", lang))
            return
        try:
            from discord_bot import bot as dc_bot
            guild = dc_bot.get_guild(gid)
        except Exception:
            guild = None
        if not guild:
            await message.reply(localized("force_leave_not_member", lang))
            return
        try:
            await guild.leave()
        except Exception as e:
            await message.reply(localized("force_leave_failed", lang, error=e))
            return
        db.remove_chat(gid)
        await message.reply(localized("force_leave_success_discord", lang, guild_id=gid))
        return

    if platform == "telegram":
        try:
            tid = int(target_id)
        except ValueError:
            await message.reply(localized("force_leave_invalid_id", lang))
            return
        try:
            await bot.leave_chat(tid)
        except Exception as e:
            await message.reply(localized("force_leave_failed", lang, error=e))
        db.remove_chat(tid)
        await message.reply(localized("force_leave_success_telegram", lang, chat_id=tid))
        return

    await message.reply(localized("force_leave_unsupported_platform", lang))

@router.message(Command("backup"))
async def backup_cmd(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return
    if message.chat.type != "private":
        await message.reply(localized("backup_private_only", lang))
        return
    try:
        from aiogram.types import BufferedInputFile
        from backup_crypto import build_encrypted_backup, encrypted_filename
        data = build_encrypted_backup("dem.db")
        doc = BufferedInputFile(data, filename=encrypted_filename("dem.db"))
        await bot.send_document(chat_id=message.chat.id, document=doc)
    except Exception as e:
        logger.warning("Failed to send database backup: %s", e)
        await message.reply(localized("backup_failed", lang, error=str(e)))

@router.message(Command("locale"))
async def locale_cmd(message: Message):
    chat_key = _chat_key(message)
    ui_lang = get_chat_lang(chat_key)
    if not _setup_gate_ok(message):
        await message.reply(localized("chat_not_setup", ui_lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 and parts[1].strip() else None

    if not arg:
        lines = [localized("loc_list_header", ui_lang)]
        for code in available_locales():
            st = locale_stats(code)
            lines.append(f"{language_name(code)} ({code}): {locale_bar(code)} {st['percent']}%")
        lines.append("")
        lines.append(localized("loc_list_footer", ui_lang))
        await message.reply("\n".join(lines))
        return

    if arg not in available_locales():
        await message.reply(localized("loc_unknown_lang", ui_lang, lang=arg, supported=", ".join(available_locales())))
        return

    if not rate_limit_ok(("locale-file", "telegram", message.chat.id), limit=1, window_seconds=600):
        await message.reply(localized("loc_cooldown", ui_lang))
        return

    path = os.path.join(os.path.dirname(utils.__file__), "i18n", f"{arg}.json")
    st = locale_stats(arg)
    caption = localized("loc_file_caption", ui_lang, name=language_name(arg), code=arg, percent=st["percent"])
    try:
        from aiogram.types import BufferedInputFile
        with open(path, "rb") as f:
            data = f.read()
        await message.reply_document(BufferedInputFile(data, filename=f"{arg}.json"), caption=caption)
    except Exception:
        await message.reply(caption)

@router.message(Command("loc_compare", "loc-compare"))
async def loc_compare_cmd(message: Message):
    ui_lang = get_chat_lang(_chat_key(message))
    if not _setup_gate_ok(message):
        await message.reply(localized("chat_not_setup", ui_lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("loc_compare_usage", ui_lang))
        return

    key = parts[1].strip()
    data = compare_reply(key)
    if data is None:
        await message.reply(localized("loc_compare_not_found", ui_lang, key=key))
        return

    lines = [localized("loc_compare_header", ui_lang, key=key)]
    for code in LANG_ORDER:
        if code not in data:
            continue
        status, text = data[code]
        emoji = LOCALE_STATUS_EMOJI.get(status, "")
        if text is None:
            shown = localized("loc_compare_untranslated", ui_lang)
        else:
            shown = str(text)
            if len(shown) > 300:
                shown = shown[:297] + "..."
        lines.append(f"{emoji} {language_name(code)}: {shown}")
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000]
    await message.reply(msg)

@router.message(Command("loc_suggest", "loc-suggest"))
async def loc_suggest_cmd(message: Message):
    ui_lang = get_chat_lang(_chat_key(message))
    if not _setup_gate_ok(message):
        await message.reply(localized("chat_not_setup", ui_lang))
        return

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 4:
        await message.reply(localized("loc_suggest_usage", ui_lang))
        return

    language = parts[1].strip().lower()
    code = parts[2].strip()
    text = parts[3].strip()
    if language not in SUPPORTED_LANGS:
        await message.reply(
            localized("loc_unknown_lang", ui_lang, lang=language, supported=", ".join(available_locales()))
        )
        return
    if not SUPPORT_CHATS.get("discord") and not SUPPORT_CHATS.get("telegram"):
        await message.reply(localized("loc_suggest_no_support", ui_lang))
        return

    username = message.from_user.username or message.from_user.full_name if message.from_user else "?"
    user_id = message.from_user.id if message.from_user else message.chat.id
    msg_code = secrets.token_hex(4)
    db.add_loc_suggestion(msg_code, "telegram", user_id, str(username),
                          language, code, text, ui_lang)
    from discord_bot import post_loc_suggestion
    await post_loc_suggestion(lang=language, key=code, suggestion=text, code=msg_code,
                              ui_lang=ui_lang, username=str(username), user_id=user_id)
    await message.reply(localized("loc_suggest_confirm", ui_lang, code=msg_code))

@router.message(Command("loc_reply", "loc-reply"))
async def loc_reply_cmd(message: Message):
    ui_lang_cmd = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", ui_lang_cmd))
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(localized("loc_reply_usage", ui_lang_cmd))
        return

    code = parts[1].strip()
    text = parts[2].strip()
    row = db.get_loc_suggestion(code)
    if not row:
        await message.reply(localized("loc_reply_not_found", ui_lang_cmd, code=code))
        return

    ui_lang = row["ui_lang"] or DEFAULT_LANG
    title = localized("loc_reply_dm_title", ui_lang)
    body = localized("loc_reply_dm_body", ui_lang,
                     suggestion=row["suggestion"], reply=text,
                     name=language_name(row["lang"]), lang=row["lang"], key=row["rkey"])

    ok = False
    if row["platform"] == "telegram":
        try:
            await bot.send_message(int(row["user_id"]), f"{title}\n\n{body}")
            ok = True
        except Exception:
            ok = False
    elif row["platform"] == "discord":
        try:
            from discord_bot import bot as dc_bot
            import discord as _discord
            user = await dc_bot.fetch_user(int(row["user_id"]))
            await user.send(embed=_discord.Embed(title=title, description=body))
            ok = True
        except Exception:
            ok = False

    admin_name = message.from_user.username or message.from_user.full_name
    from discord_bot import post_loc_reply
    await post_loc_reply(admin=str(admin_name), code=code, ui_lang=ui_lang, title=title, body=body)

    if ok:
        db.delete_loc_suggestion(code)
        await message.reply(localized("loc_reply_sent", ui_lang_cmd))
    else:
        await message.reply(localized("loc_reply_failed", ui_lang_cmd))

async def main():
    await dp.start_polling(bot)

from discord_bot import (
    _search_party, _search_govt, _govt_member_lines, _edit_menu_text,
    _rules_menu_text, _party_line, _leaders_differ_from_founder,
    _edit_admin_menu_text, _resume_state, parties_enabled, _party_balance_lines,
    olympiad_help_keys, post_olympiad_vote, sync_olympiad_commands,
)

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

TEMP_REPLY_SECONDS = 60

async def _reply_temp(message: Message, text, seconds=TEMP_REPLY_SECONDS, **kwargs):
    """Reply with a message that clears itself after a while — Telegram's
    nearest equivalent of Discord's ephemeral answers. Used for administrative
    confirmations that would otherwise sit in a live chat forever. The removal
    runs as its own task, so the handler never waits on it."""
    sent = await message.reply(text, **kwargs)

    async def _remove():
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

def _party_code_validator(value):
    code = value.strip().upper()
    if PARTY_CODE_RE.match(code) and not db.party_code_taken(code):
        return code
    return None

def _markdown_from_tg(m):
    """Canonical (Discord-markdown) text of a Telegram message, links preserved."""
    if m.text is not None:
        return telegram_entities_to_discord(m.text, m.entities).strip()
    return telegram_entities_to_discord(m.caption or "", m.caption_entities).strip()

def _party_info_text_tg(party, lang):
    lines = [f"<b>{escape_html(party['name'])} [{escape_html(party['code'])}]</b>"]
    if party["suspended"]:
        lines.append(escape_html(localized("party_suspended_mark", lang)))
    founder = format_stored_user("telegram", party["founder_platform"],
                                 party["founder_id"], party["founder_name"])
    lines.append(f"<b>{escape_html(localized('party_field_founder', lang))}:</b> {escape_html(founder)}")
    leaders = db.get_party_leaders(party["code"])
    if _leaders_differ_from_founder(party, leaders):
        shown = ", ".join(
            format_stored_user("telegram", l["platform"], l["user_id"], l["display_name"])
            for l in leaders
        )
        lines.append(f"<b>{escape_html(localized('party_field_leader', lang))}:</b> {escape_html(shown)}")
    if party["ideologies"]:
        lines.append(f"<b>{escape_html(localized('party_field_ideologies', lang))}:</b> "
                     f"{discord_to_telegram_html(party['ideologies'])}")
    allies = db.get_party_allies(party["code"])
    if allies:
        shown = ", ".join(f"{a['name']} [{a['code']}]" for a in allies)
        lines.append(f"<b>{escape_html(localized('party_field_allies', lang))}:</b> {escape_html(shown)}")
    lines.append(f"<b>{escape_html(localized('party_field_members', lang))}:</b> "
                 f"{len(db.get_party_user_set(party['code']))}")
    seats = db.party_govt_seats(party["code"])
    if seats:
        lines.append(f"<b>{escape_html(localized('party_field_seats', lang))}:</b> {seats}")
    balances = _party_balance_lines(party["code"])
    if balances:
        lines.append(f"<b>{escape_html(localized('party_field_balance', lang))}:</b> "
                     f"{escape_html(', '.join(balances))}")
    if party["description"]:
        lines.append(f"<b>{escape_html(localized('party_field_description', lang))}:</b>\n"
                     f"{discord_to_telegram_html(party['description'])}")
    return "\n".join(lines)

async def _send_party_info_tg(message, party, lang):
    text = _party_info_text_tg(party, lang)
    kb = None
    if party["article_url"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=localized("party_more_button", lang),
                                 url=party["article_url"]),
        ]])
    if party["logo"]:
        photo = BufferedInputFile(party["logo"], filename="logo.png")
        if len(text) <= 1024:
            await message.reply_photo(photo, caption=text, parse_mode="HTML", reply_markup=kb)
            return
        await message.reply_photo(photo)
    await message.reply(text, parse_mode="HTML", reply_markup=kb)

async def _send_menu_tg(message, party, lang, rules=False, admin=False):
    if admin:
        title_key, body = "edit_admin_menu_title", _edit_admin_menu_text(party, lang)
    elif rules:
        title_key, body = "rules_menu_title", _rules_menu_text(party, lang)
    else:
        title_key, body = "edit_menu_title", _edit_menu_text(party, lang)
    title = localized(title_key, lang, name=party["name"], code=party["code"])
    text = f"<b>{escape_html(title)}</b>\n{escape_html(body)}"
    if party["logo"]:
        photo = BufferedInputFile(party["logo"], filename="logo.png")
        if len(text) <= 1024:
            await message.reply_photo(photo, caption=text, parse_mode="HTML")
            return
        await message.reply_photo(photo)
    await message.reply(text, parse_mode="HTML")

async def _resolve_party_union_tg(message, lang):
    """The union of this group, refusing when the group is not set up or the
    union has not turned parties on. Returns the union code, or None."""
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return None
    chat = db.get_chat(str(message.chat.id))
    if not chat:
        await message.reply(localized("chat_not_setup", lang))
        return None
    if not parties_enabled(chat["union_code"]):
        await message.reply(localized("party_feature_disabled", lang))
        return None
    return chat["union_code"]

async def _resolve_led_party_tg(message, lang):
    """The party the caller manages in this group's union; asks when several."""
    union = await _resolve_party_union_tg(message, lang)
    if union is None:
        return None
    parties = db.user_led_parties(union, "telegram", message.from_user.id)
    if not parties:
        await message.reply(localized("party_none_led", lang))
        return None
    if len(parties) == 1:
        return parties[0]
    return await _numbered_choice_tg(message, lang, localized("party_choose", lang),
                                     parties, lambda p: _party_line(p, lang))

def _consent_keyboard(lang, token):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=localized("consent_accept", lang),
                             callback_data=f"pc:{token}:1"),
        InlineKeyboardButton(text=localized("consent_decline", lang),
                             callback_data=f"pc:{token}:0"),
    ]])

def _register_consent(action, party_code, target_kind, target, lang):
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

async def _offer_leadership_tg(message, party, lang, transfer):
    prompt_key = "edit_ask_transfer" if transfer else "edit_ask_leader"
    m = await _dialog_text_tg(message, lang, localized(prompt_key, lang))
    if m is None:
        return
    ref = _parse_tg_user_ref(m.text or "")
    if ref is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    target_kind, target = ref
    mention = f"@{target}" if target_kind == "username" else str(target)

    action = "transfer" if transfer else "leader"
    token = _register_consent(action, party["code"], target_kind, target, lang)
    offer_key = "consent_transfer_offer" if transfer else "consent_leader_offer"
    await message.answer(
        localized(offer_key, lang, mention=mention, name=party["name"], code=party["code"]),
        reply_markup=_consent_keyboard(lang, token),
    )

async def _confirm_suspend_tg(message, party, lang):
    action = "resume" if party["suspended"] else "suspend"
    token = _register_consent(action, party["code"], "id", message.from_user.id, lang)
    ask_key = "resume_confirm" if party["suspended"] else "suspend_confirm"
    await message.reply(localized(ask_key, lang, name=party["name"]),
                        reply_markup=_consent_keyboard(lang, token))

async def _confirm_delete_party_tg(message, party, lang):
    token = _register_consent("delete", party["code"], "id", message.from_user.id, lang)
    await message.reply(
        localized("delete_party_confirm", lang, name=party["name"], code=party["code"]),
        reply_markup=_consent_keyboard(lang, token))

@router.callback_query(lambda c: c.data and c.data.startswith("pc:"))
async def handle_party_consent(query: CallbackQuery):
    try:
        _, token, flag = query.data.split(":")
    except Exception:
        await query.answer()
        return

    pend = _pending_consents.get(token)
    if not pend or time.time() - pend["created"] > DIALOG_TIMEOUT:
        _pending_consents.pop(token, None)
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    lang = pend["lang"]
    user = query.from_user
    if pend["target_kind"] == "id":
        allowed = user is not None and user.id == int(pend["target"])
    else:
        allowed = user is not None and \
            (user.username or "").lower() == str(pend["target"]).lower()
    if not allowed:
        await query.answer(localized("consent_not_yours", lang), show_alert=True)
        return

    _pending_consents.pop(token, None)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    party = db.get_party(pend["party_code"])
    if party is None:
        await query.answer()
        return

    accepted = flag == "1"
    display = _tg_user_label(user)
    action = pend["action"]
    if action == "leader":
        if accepted:
            db.add_party_leader(party["code"], "telegram", user.id, display)
            text = localized("leader_added", lang, user=display, name=party["name"])
        else:
            text = localized("leader_declined", lang)
    elif action == "transfer":
        if accepted:
            db.set_party_leader(party["code"], "telegram", user.id, display)
            text = localized("transfer_done", lang, user=display, name=party["name"])
        else:
            text = localized("transfer_declined", lang)
    elif action in ("suspend", "resume"):
        if accepted:
            db.update_party_field(party["code"], "suspended",
                                  0 if action == "resume" else 1)
            text = localized("resume_done" if action == "resume" else "suspend_done",
                             lang, name=party["name"])
        else:
            text = localized("action_cancelled", lang)
    elif action == "delete":
        if accepted:
            db.delete_party(party["code"])
            text = localized("delete_party_done", lang, name=party["name"], code=party["code"])
            await send_service_event("party_deleted", name=party["name"], party=party["code"],
                                     union=party["union_code"], user=display)
        else:
            text = localized("action_cancelled", lang)
    else:
        await query.answer()
        return

    await query.answer()
    try:
        await query.message.reply(text)
    except Exception:
        pass

@router.message(Command("allow_parties", "allow-parties"))
async def allow_parties_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("allow_parties_usage", lang))
        return

    union = parts[1].strip().upper()
    action = parts[2].strip().lower()
    if not db.union_exists(union):
        await message.reply(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes()))
        )
        return

    if action == "disable":
        db.set_parties_enabled(union, False)
        await message.reply(localized("allow_parties_disabled", lang, code=union))
        await send_service_event("parties_disabled", code=union,
                                 user=_tg_user_label(message.from_user))
        return
    if action != "enable":
        await message.reply(localized("allow_parties_usage", lang))
        return

    roles_raw = "".join(parts[3:])
    role_ids = [p.strip() for p in roles_raw.split(",") if p.strip()]
    if not role_ids:
        await message.reply(localized("allow_parties_need_roles", lang))
        return
    if not all(p.isdigit() for p in role_ids):
        await message.reply(localized("allow_parties_invalid_roles", lang))
        return

    db.set_parties_enabled(union, True, role_ids)
    await message.reply(localized("allow_parties_enabled", lang, code=union,
                                  roles=", ".join(role_ids)))
    await send_service_event("parties_enabled", code=union,
                             user=_tg_user_label(message.from_user))

@router.message(Command("add_party", "add-party"))
async def add_party_tg(message: Message):
    """Role requirements can only be checked on Discord, so parties are founded
    there; the rest of the party commands work on both platforms."""
    lang = get_chat_lang(_chat_key(message))
    if await _resolve_party_union_tg(message, lang) is None:
        return
    await message.reply(localized("add_party_discord_only", lang))

async def _edit_party_option_tg(message, party, lang, option, admin=False):
    if option == 1:
        m = await _dialog_text_tg(message, lang, localized("edit_ask_name", lang))
        if m is None:
            return
        db.update_party_field(party["code"], "name",
                              clean_display_name(m.text or m.caption, max_len=100))
        await message.reply(localized("edit_saved", lang))
    elif option == 2:
        code = await _dialog_text_tg(message, lang, localized("edit_ask_code", lang),
                                     validator=_party_code_validator,
                                     error_key="add_party_invalid_code")
        if code is None:
            return
        db.rename_party_code(party["code"], code)
        await message.reply(localized("edit_saved", lang))
    elif option == 3:
        logo = await _dialog_logo_tg(message, lang, localized("edit_ask_logo", lang))
        if logo is None:
            return
        db.update_party_logo(party["code"], logo[0], logo[1])
        await message.reply(localized("edit_saved", lang))
    elif option == 4:
        def color_validator(value):
            m2 = COLOR_RE.match(value.strip())
            return f"#{m2.group(1).lower()}" if m2 else None
        color = await _dialog_text_tg(message, lang, localized("edit_ask_color", lang),
                                      validator=color_validator,
                                      error_key="edit_invalid_color")
        if color is None:
            return
        db.update_party_field(party["code"], "color", color)
        await message.reply(localized("edit_saved", lang))
    elif option == 5:
        m = await _dialog_text_tg(message, lang, localized("edit_ask_description", lang))
        if m is None:
            return
        db.update_party_field(party["code"], "description", _markdown_from_tg(m)[:1024])
        await message.reply(localized("edit_saved", lang))
    elif option == 6:
        m = await _dialog_text_tg(message, lang, localized("edit_ask_ideologies", lang))
        if m is None:
            return
        db.update_party_field(party["code"], "ideologies", _markdown_from_tg(m)[:1024])
        await message.reply(localized("edit_saved", lang))
    elif option == 7:
        await _offer_leadership_tg(message, party, lang, transfer=False)
    elif option == 8:
        await _offer_leadership_tg(message, party, lang, transfer=True)
    elif option == 9:
        await _confirm_suspend_tg(message, party, lang)
    elif option == 10:
        def url_validator(value):
            return value.strip() if URL_RE.match(value.strip()) else None
        url = await _dialog_text_tg(message, lang, localized("edit_ask_article", lang),
                                    validator=url_validator,
                                    error_key="edit_invalid_article")
        if url is None:
            return
        db.update_party_field(party["code"], "article_url", url)
        await message.reply(localized("edit_saved", lang))
    elif admin and option == 11:
        new_value = 0 if party["allow_member_invites"] else 1
        db.update_party_field(party["code"], "allow_member_invites", new_value)
        key = "rules_invites_enabled" if new_value else "rules_invites_disabled"
        await message.reply(localized(key, lang))
    elif admin and option == 12:
        def union_validator(value):
            code = value.strip().upper()
            return code if db.union_exists(code) else None
        union = await _dialog_text_tg(message, lang, localized("edit_ask_union", lang),
                                      validator=union_validator,
                                      error_key="edit_invalid_union")
        if union is None:
            return
        db.update_party_union(party["code"], union)
        await message.reply(localized("edit_union_changed", lang, name=party["name"],
                                      union=db.get_union_name(union, lang)))
    elif admin and option == 13:
        await _confirm_delete_party_tg(message, party, lang)
    else:
        await message.reply(localized("edit_invalid_option", lang))

@router.message(Command("edit_party_admin", "edit-party-admin"))
async def edit_party_admin_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("edit_admin_usage", lang))
        return

    query = parts[1].strip()
    party = db.find_party(query)
    if party is None:
        await message.reply(localized("edit_admin_not_found", lang, query=query))
        return

    await _send_menu_tg(message, party, lang, admin=True)
    m = await _wait_user_message(message.chat.id, message.from_user.id)
    if m is None:
        return
    value = (m.text or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= 13):
        await message.reply(localized("choice_invalid", lang))
        return
    await _edit_party_option_tg(message, party, lang, int(value), admin=True)

@router.message(Command("edit_party", "edit-party"))
async def edit_party_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if not await _require_verified_tg(message, lang):
        return
    party = await _resolve_led_party_tg(message, lang)
    if party is None:
        return

    parts = (message.text or "").split()
    option = None
    if len(parts) > 1:
        if not parts[1].strip().isdigit():
            await message.reply(localized("edit_invalid_option", lang))
            return
        option = int(parts[1].strip())

    if option is None:
        await _send_menu_tg(message, party, lang)
        return
    await _edit_party_option_tg(message, party, lang, option)

@router.message(Command("edit_rules", "edit-rules"))
async def edit_rules_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if not await _require_verified_tg(message, lang):
        return
    party = await _resolve_led_party_tg(message, lang)
    if party is None:
        return

    parts = (message.text or "").split()
    option = None
    if len(parts) > 1:
        if not parts[1].strip().isdigit():
            await message.reply(localized("edit_invalid_option", lang))
            return
        option = int(parts[1].strip())

    if option is None:
        await _send_menu_tg(message, party, lang, rules=True)
        return

    if option == 1:
        new_value = 0 if party["allow_member_invites"] else 1
        db.update_party_field(party["code"], "allow_member_invites", new_value)
        key = "rules_invites_enabled" if new_value else "rules_invites_disabled"
        await message.reply(localized(key, lang))
    else:
        await message.reply(localized("edit_invalid_option", lang))

@router.message(Command("party"))
async def party_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    union = await _resolve_party_union_tg(message, lang)
    if union is None:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("party_usage", lang))
        return

    party, suggestions = _search_party(union, parts[1])
    if party is None:
        if not suggestions:
            await message.reply(localized("party_not_found", lang))
            return
        party = await _numbered_choice_tg(message, lang,
                                          localized("party_not_found_suggest", lang),
                                          suggestions, lambda p: _party_line(p, lang))
        if party is None:
            return
    await _send_party_info_tg(message, party, lang)

@router.message(Command("add_govt", "add-govt"))
async def add_govt_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=2)
    chunks = [c.strip() for c in parts[2].split("|")] if len(parts) == 3 else []
    if len(chunks) != 3 or not all(chunks):
        await message.reply(localized("add_govt_usage", lang))
        return

    union = parts[1].strip().upper()
    if not db.union_exists(union):
        await message.reply(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes()))
        )
        return

    name = clean_display_name(chunks[0], max_len=100)
    if not db.create_govt_body(union, name, chunks[1], chunks[2]):
        await message.reply(localized("add_govt_exists", lang))
        return
    await message.reply(
        localized("add_govt_created", lang, name=name,
                  union=db.get_union_name(union, lang),
                  singular=chunks[1], plural=chunks[2])
    )

@router.message(Command("govt"))
async def govt_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return
    chat = db.get_chat(str(message.chat.id))
    if not chat:
        await message.reply(localized("chat_not_setup", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("govt_usage", lang))
        return

    body, suggestions = _search_govt(chat["union_code"], parts[1])
    if body is None:
        if not suggestions:
            await message.reply(localized("govt_not_found", lang))
            return

        def render(b):
            line = b["name"]
            if b["union_code"] != chat["union_code"]:
                line += " " + localized("govt_other_union_mark", lang, code=b["union_code"])
            return line

        body = await _numbered_choice_tg(message, lang,
                                         localized("govt_not_found_suggest", lang),
                                         suggestions, render)
        if body is None:
            return

    members_text = _govt_member_lines(body, lang, viewer_platform="telegram") \
        or localized("govt_no_members", lang)
    union_name = db.get_union_name(body["union_code"], lang)
    text = (f"<b>{escape_html(body['name'])}</b>\n"
            f"{escape_html(members_text)}\n\n"
            f"<i>{escape_html(union_name)}</i>")
    await message.reply(text, parse_mode="HTML")

@router.message(Command("add_discord", "add-discord"))
async def add_discord_tg(message: Message):
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

@router.message(Command("party_join", "party-join"))
async def party_join_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    union = await _resolve_party_union_tg(message, lang)
    if union is None:
        return
    if not await _require_verified_tg(message, lang):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("party_join_usage", lang))
        return

    from discord_bot import _find_party_exact, _notify_discord_leaders
    party = _find_party_exact(union, parts[1])
    if not party:
        await message.reply(localized("party_not_found", lang))
        return
    if party["suspended"]:
        await message.reply(localized("party_join_suspended", lang))
        return

    existing = db.find_user_party(union, "telegram", message.from_user.id)
    if existing:
        if existing["code"] == party["code"]:
            await message.reply(localized("party_join_already_member", lang))
        else:
            await message.reply(localized("party_join_in_other", lang, name=existing["name"]))
        return
    if db.has_open_join_request(party["code"], "telegram", message.from_user.id):
        await message.reply(localized("party_join_pending", lang))
        return

    requester_name = _tg_user_label(message.from_user)
    rid = db.create_join_request(party["code"], "telegram", message.from_user.id, requester_name, lang)
    delivered = await _notify_discord_leaders(party, rid, lang, requester_name)
    if delivered == 0:
        db.delete_join_request(rid)
        await message.reply(localized("party_join_no_leader", lang))
        return
    await message.reply(localized("party_join_sent", lang, name=party["name"]))

@router.message(Command("party_leave", "party-leave"))
async def party_leave_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    union = await _resolve_party_union_tg(message, lang)
    if union is None:
        return
    party = db.find_user_party(union, "telegram", message.from_user.id)
    if not party:
        await message.reply(localized("party_leave_none", lang))
        return
    if db.is_founder(party["code"], "telegram", message.from_user.id):
        await message.reply(localized("party_leave_founder", lang))
        return
    db.remove_party_leader(party["code"], "telegram", message.from_user.id)
    db.remove_party_member(party["code"], "telegram", message.from_user.id)
    await message.reply(localized("party_leave_done", lang, name=party["name"]))

def _find_kick_target_tg(code, target):
    raw = (target or "").strip()
    is_id = raw.isdigit()
    name = raw.lstrip("@").lower()
    for mem in db.get_party_members(code):
        if mem["platform"] != "telegram":
            continue
        if is_id and mem["user_id"] == raw:
            return mem
        if (mem["display_name"] or "").lstrip("@").lower() == name:
            return mem
    return None

@router.message(Command("party_kick", "party-kick"))
async def party_kick_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    party = await _resolve_led_party_tg(message, lang)
    if party is None:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("party_kick_usage", lang))
        return
    target = parts[1].strip()

    mem = _find_kick_target_tg(party["code"], target)
    if mem is None:
        raw = target.lstrip("@").lower()
        is_id = target.strip().isdigit()
        for l in db.get_party_leaders(party["code"]):
            if l["platform"] != "telegram":
                continue
            if (is_id and l["user_id"] == target.strip()) or \
                    (l["display_name"] or "").lstrip("@").lower() == raw:
                if db.is_founder(party["code"], "telegram", l["user_id"]):
                    await message.reply(localized("party_kick_founder", lang))
                else:
                    await message.reply(localized("party_kick_leader", lang))
                return
        await message.reply(localized("party_kick_not_member", lang))
        return

    db.remove_party_member(party["code"], "telegram", mem["user_id"])
    await message.reply(localized("party_kick_done", lang,
                                  user=mem["display_name"] or mem["user_id"], name=party["name"]))

_pending_quiz_buttons = {}

def _new_quiz_button_token(user_id, lang):
    token = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    _pending_quiz_buttons[token] = {
        "future": fut, "user_id": user_id, "lang": lang, "created": time.time()}
    return token, fut

async def _await_quiz_button(fut, timeout=DIALOG_TIMEOUT):
    try:
        return await asyncio.wait_for(fut, timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None

@router.callback_query(lambda c: c.data and c.data.startswith("qb:"))
async def handle_quiz_button(query: CallbackQuery):
    try:
        _, token, value = query.data.split(":")
    except Exception:
        await query.answer()
        return
    pend = _pending_quiz_buttons.get(token)
    if not pend or time.time() - pend["created"] > DIALOG_TIMEOUT:
        _pending_quiz_buttons.pop(token, None)
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if not query.from_user or query.from_user.id != pend["user_id"]:
        await query.answer(localized("consent_not_yours", pend["lang"]), show_alert=True)
        return
    _pending_quiz_buttons.pop(token, None)
    await query.answer()
    if not pend["future"].done():
        pend["future"].set_result(value)

_active_quizzes_tg = {}

def _quiz_question_text_tg(quiz_id, lang, qindex, order, allow_prev, total):
    topic = quizzes.question_topic(quiz_id, lang, qindex)
    texts, _kinds = quizzes.display_options(quiz_id, lang, qindex, order, allow_prev)
    lines = [f"{topic}  ({qindex + 1}/{total})", ""]
    lines += [f"{i + 1}. {t}" for i, t in enumerate(texts)]
    lines += ["", localized("quiz_answer_hint", lang)]
    return "\n".join(lines)[:4096]

def _quiz_results_text_tg(quiz_id, lang, results, name, with_desc=True):
    lines = [f"<b>{escape_html(localized('quiz_results_title', lang, name=name))}</b>", ""]
    for a in quizzes.format_results(quiz_id, lang, results):
        breakdown = " · ".join(f"{e['name']} — {e['percent']}%" for e in a["entries"])
        lines.append(f"<b>{escape_html(a['axis_name'])}</b>")
        lines.append(escape_html(breakdown))
        if with_desc and a["top_desc"]:
            lines.append(escape_html(a["top_desc"]))
        lines.append("")
    return "\n".join(lines).strip()

def _quiz_compare_text_tg(quiz_id, lang, res_a, res_b, name_a, name_b, title=None):
    if title is None:
        title = localized("quizzes_compare_title", lang, name=quizzes.quiz_name(quiz_id, lang))
    lines = [f"<b>{escape_html(title)}</b>", ""]
    fb = {a["axis"]: a for a in quizzes.format_results(quiz_id, lang, res_b)}
    for a in quizzes.format_results(quiz_id, lang, res_a):
        b = fb.get(a["axis"], {"entries": []})
        va = " · ".join(f"{e['name']} {e['percent']}%" for e in a["entries"])
        vb = " · ".join(f"{e['name']} {e['percent']}%" for e in b["entries"])
        lines.append(f"<b>{escape_html(a['axis_name'])}</b>")
        lines.append(f"{escape_html(name_a)}: {escape_html(va)}")
        lines.append(f"{escape_html(name_b)}: {escape_html(vb)}")
        lines.append("")
    return "\n".join(lines).strip()

async def _edit_quiz_msg(chat_id, msg_id, text, reply_markup=None, parse_mode=None):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                    reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except Exception:
        return False

async def _run_quiz_tg(message: Message, quiz_id):
    lang = get_chat_lang(_chat_key(message))
    chat_id = message.chat.id
    user_id = message.from_user.id
    name = quizzes.quiz_name(quiz_id, lang)
    n = quizzes.num_questions(quiz_id)

    saved = db.get_quiz_progress("telegram", user_id, quiz_id)
    resumed = _resume_state(saved, quiz_id, n) if saved else None

    if resumed:
        orders, answers, qindex = resumed
        sent = await message.answer(
            localized("quiz_resumed", lang, name=name, num=qindex + 1, total=n))
        msg_id = sent.message_id
    else:
        token, fut = _new_quiz_button_token(user_id, lang)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=localized("quiz_go", lang), callback_data=f"qb:{token}:go"),
            InlineKeyboardButton(text=localized("quiz_later", lang), callback_data=f"qb:{token}:later"),
        ]])
        start_text = localized("quiz_start_prompt", lang, name=name)
        credit = quizzes.quiz_credit(quiz_id, lang)
        if credit:
            start_text = f"{start_text}\n\n{credit}"
        sent = await message.answer(start_text, reply_markup=kb)
        msg_id = sent.message_id
        if await _await_quiz_button(fut) != "go":
            await _edit_quiz_msg(chat_id, msg_id, localized("quiz_cancelled", lang))
            return
        orders = quizzes.make_orders(quiz_id)
        answers = [None] * n
        qindex = 0

    session = {"quiz_id": quiz_id, "stop": asyncio.Event()}
    _active_quizzes_tg[user_id] = session
    try:
        while qindex < n:
            allow_prev = qindex > 0
            await _edit_quiz_msg(chat_id, msg_id,
                                 _quiz_question_text_tg(quiz_id, lang, qindex, orders[qindex], allow_prev, n))
            outcome, reply = await _wait_user_message_or_stop(chat_id, user_id, session["stop"])
            if outcome == "stop":
                db.save_quiz_progress("telegram", user_id, quiz_id, lang, qindex,
                                      json.dumps(orders),
                                      json.dumps(answers, ensure_ascii=False))
                stopped = localized("quiz_stop_saved", lang, name=name)
                if not await _edit_quiz_msg(chat_id, msg_id, stopped):
                    await message.answer(stopped)
                return
            if outcome == "timeout":
                await message.answer(localized("dialog_timeout", lang))
                return
            parsed = quizzes.parse_answer(reply.text or reply.caption or "")
            if parsed is None:
                await message.answer(localized("quiz_answer_invalid", lang))
                continue
            num, weight = parsed
            _texts, kinds = quizzes.display_options(quiz_id, lang, qindex, orders[qindex], allow_prev)
            if num < 1 or num > len(kinds):
                await message.answer(localized("quiz_answer_out_of_range", lang))
                continue
            kind = kinds[num - 1]
            accepted = True
            if kind[0] == "prev":
                qindex -= 1
            elif kind[0] == "nota":
                answers[qindex] = ("nota",)
                qindex += 1
            elif kind[0] == "idc":
                answers[qindex] = ("idc",)
                qindex += 1
            else:
                if weight is None:
                    await message.answer(localized("quiz_answer_need_weight", lang, num=num))
                    accepted = False
                else:
                    answers[qindex] = ("real", kind[1], weight)
                    qindex += 1
            if accepted:
                try:
                    await reply.delete()
                except Exception:
                    pass
    finally:
        _active_quizzes_tg.pop(user_id, None)

    db.delete_quiz_progress("telegram", user_id, quiz_id)
    results = quizzes.score(quiz_id, answers)

    token, fut = _new_quiz_button_token(user_id, lang)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=localized("quiz_consent_yes", lang), callback_data=f"qb:{token}:yes"),
        InlineKeyboardButton(text=localized("quiz_consent_no", lang), callback_data=f"qb:{token}:no"),
    ]])
    if not await _edit_quiz_msg(chat_id, msg_id, localized("quiz_consent_prompt", lang), reply_markup=kb):
        await message.answer(localized("quiz_consent_prompt", lang), reply_markup=kb)
    consent = await _await_quiz_button(fut)
    if consent == "yes":
        db.save_quiz_result("telegram", user_id, quiz_id, lang,
                            json.dumps(results, ensure_ascii=False),
                            json.dumps(answers, ensure_ascii=False))
        await message.answer(localized("quiz_saved", lang))
    elif consent == "no":
        await message.answer(localized("quiz_not_saved", lang))

    text = _quiz_results_text_tg(quiz_id, lang, results, name)
    if len(text) > 4096:
        text = _quiz_results_text_tg(quiz_id, lang, results, name, with_desc=False)
    if not await _edit_quiz_msg(chat_id, msg_id, text, parse_mode="HTML"):
        await message.answer(text, parse_mode="HTML")

@router.message(Command("quizzes"))
async def quizzes_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    ids = quizzes.list_quizzes()
    lines = [localized("quizzes_list_header", lang)]
    for i, qid in enumerate(ids):
        lines.append(f"{i + 1}. {quizzes.quiz_name(qid, lang)}")
    await message.reply("\n".join(lines))

    reply = await _wait_user_message(message.chat.id, message.from_user.id)
    if reply is None:
        return
    value = (reply.text or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(ids)):
        await message.reply(localized("choice_invalid", lang))
        return
    await _run_quiz_tg(message, ids[int(value) - 1])

@router.message(Command("quizzes_stop", "quizzes-stop"))
async def quizzes_stop_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    session = _active_quizzes_tg.get(message.from_user.id)
    if not session:
        await message.reply(localized("quiz_stop_none", lang))
        return
    session["stop"].set()

@router.message(Command("quizzes_clear", "quizzes-clear"))
async def quizzes_clear_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    rows = db.get_user_quiz_results("telegram", message.from_user.id)
    if not rows:
        await message.reply(localized("quizzes_clear_none", lang))
        return
    lines = [localized("quizzes_clear_header", lang)]
    for i, r in enumerate(rows):
        lines.append(f"{i + 1}. {quizzes.quiz_name(r['quiz_id'], lang)} — {format_quiz_date(r['created_at'])}")
    await message.reply("\n".join(lines))

    reply = await _wait_user_message(message.chat.id, message.from_user.id)
    if reply is None:
        return
    value = (reply.text or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(rows)):
        await message.reply(localized("choice_invalid", lang))
        return
    row = rows[int(value) - 1]
    db.delete_quiz_result(row["id"])
    await message.reply(localized("quizzes_clear_deleted", lang,
                                  name=quizzes.quiz_name(row["quiz_id"], lang),
                                  date=format_quiz_date(row["created_at"])))

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
    try:
        chat = await bot.get_chat(int(target_id))
        if getattr(chat, "username", None):
            return "@" + chat.username
        return getattr(chat, "full_name", None) or str(target_id)
    except Exception:
        return str(target_id)

@router.message(Command("quizzes_compare", "quizzes-compare"))
async def quizzes_compare_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("quizzes_compare_usage", lang))
        return

    target_id = await _resolve_tg_target(message, parts[1].strip())
    if target_id is None:
        await message.reply(localized("quizzes_compare_user_invalid", lang))
        return
    if str(target_id) == str(message.from_user.id):
        await message.reply(localized("quizzes_compare_self", lang))
        return

    if db.is_quiz_compare_blocked("telegram", target_id):
        await message.reply(localized("quizzes_compare_blocked", lang))
        return

    blocked = db.get_blocked_quiz_ids("telegram", target_id)
    mine = db.get_user_quiz_ids("telegram", message.from_user.id)
    theirs = set(db.get_user_quiz_ids("telegram", target_id))
    shared = [q for q in mine if q in theirs and q not in blocked]
    if not shared:
        if any(q in theirs for q in mine):
            await message.reply(localized("quizzes_compare_blocked", lang))
        else:
            await message.reply(localized("quizzes_compare_none", lang))
        return

    name_a = localized("quizzes_compare_you", lang)
    name_b = await _quiz_target_label_tg(target_id)

    async def show(quiz_id):
        ra = db.get_latest_quiz_result("telegram", message.from_user.id, quiz_id)
        rb = db.get_latest_quiz_result("telegram", target_id, quiz_id)
        if not ra or not rb:
            return
        text = _quiz_compare_text_tg(quiz_id, lang,
                                     json.loads(ra["result_json"]), json.loads(rb["result_json"]),
                                     name_a, name_b)
        await message.answer(text, parse_mode="HTML")

    if len(shared) == 1:
        await show(shared[0])
        return

    lines = [localized("quizzes_compare_header", lang)]
    for i, qid in enumerate(shared):
        lines.append(f"{i + 1}. {quizzes.quiz_name(qid, lang)}")
    await message.reply("\n".join(lines))

    reply = await _wait_user_message(message.chat.id, message.from_user.id)
    if reply is None:
        return
    value = (reply.text or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(shared)):
        await message.reply(localized("choice_invalid", lang))
        return
    await show(shared[int(value) - 1])

def _quiz_history_text_tg(quiz_id, lang, latest, previous):
    """The user's two most recent attempts of one quiz, each labelled with the
    day it was taken."""
    return _quiz_compare_text_tg(
        quiz_id, lang,
        json.loads(latest["result_json"]), json.loads(previous["result_json"]),
        localized("quizzes_previous_current", lang, date=format_quiz_date(latest["created_at"])),
        localized("quizzes_previous_earlier", lang, date=format_quiz_date(previous["created_at"])),
        title=localized("quizzes_previous_title", lang, name=quizzes.quiz_name(quiz_id, lang)),
    )

@router.message(Command("quizzes_previous", "quizzes-previous"))
async def quizzes_previous_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    user_id = message.from_user.id

    parts = (message.text or "").split()
    number = None
    if len(parts) > 1:
        if not parts[1].strip().isdigit():
            await message.reply(localized("quizzes_previous_usage", lang))
            return
        number = int(parts[1].strip())

    quiz_id, error = resolve_previous_quiz("telegram", user_id, number)
    if error:
        await message.reply(localized(error, lang))
        return

    latest = db.get_latest_quiz_result("telegram", user_id, quiz_id)
    previous = db.get_previous_quiz_result("telegram", user_id, quiz_id)
    if not latest or not previous:
        await message.reply(localized("quizzes_previous_none", lang,
                                      name=quizzes.quiz_name(quiz_id, lang)))
        return

    await message.answer(_quiz_history_text_tg(quiz_id, lang, latest, previous),
                         parse_mode="HTML")

async def _run_privacy_action_tg(message: Message, lang, action):
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

_pending_econ_consents = {}

def _currency_code_validator_tg(value):
    code = value.strip().upper()
    if economy.CURRENCY_CODE_RE.match(code) and not db.code_taken(code):
        return code
    return None

def _bank_line_tg(bank):
    emoji = f" {bank['emoji']}" if bank["emoji"] else ""
    return f"{bank['currency_name']} [{bank['code']}]{emoji}"

async def _resolve_led_bank_tg(message, lang):
    banks = db.user_led_banks("telegram", message.from_user.id)
    if not banks:
        await message.reply(localized("bank_none_led", lang))
        return None
    if len(banks) == 1:
        return banks[0]
    return await _numbered_choice_tg(message, lang, localized("bank_choose", lang),
                                     banks, _bank_line_tg)

def _econ_consent_keyboard(lang, token):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=localized("consent_accept", lang),
                             callback_data=f"ec:{token}:1"),
        InlineKeyboardButton(text=localized("consent_decline", lang),
                             callback_data=f"ec:{token}:0"),
    ]])

def _register_econ_consent(data):
    token = secrets.token_hex(8)
    data["created"] = time.time()
    _pending_econ_consents[token] = data
    return token

@router.callback_query(lambda c: c.data and c.data.startswith("ec:"))
async def handle_econ_consent(query: CallbackQuery):
    try:
        _, token, flag = query.data.split(":")
    except Exception:
        await query.answer()
        return
    pend = _pending_econ_consents.get(token)
    if not pend or time.time() - pend["created"] > DIALOG_TIMEOUT:
        _pending_econ_consents.pop(token, None)
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    lang = pend["lang"]
    user = query.from_user
    action = pend["action"]
    if action in ("bank_leader", "bank_transfer", "ent_leader", "ent_transfer"):
        if pend["target_kind"] == "id":
            allowed = user is not None and user.id == int(pend["target"])
        else:
            allowed = user is not None and \
                (user.username or "").lower() == str(pend["target"]).lower()
        deny_key = "consent_not_yours"
    elif action in ("treaty", "peg"):
        allowed = user is not None and (db.is_bank_leader(pend["other"], "telegram", user.id)
                                        or is_admin("telegram", user.id))
        deny_key = "bank_consent_not_leader"
    elif action in ("ent_join", "export", "auto_export"):
        allowed = user is not None and \
            (db.is_enterprise_leader(pend["ent"], "telegram", user.id)
             or is_admin("telegram", user.id))
        deny_key = "ent_consent_not_leader"
    else:
        await query.answer()
        return
    if not allowed:
        await query.answer(localized(deny_key, lang), show_alert=True)
        return

    _pending_econ_consents.pop(token, None)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    accepted = flag == "1"
    display = _tg_user_label(user)
    text = None
    if action == "bank_leader":
        bank = db.get_bank(pend["bank_code"])
        if bank and accepted:
            db.add_bank_leader(bank["code"], "telegram", user.id, display)
            text = localized("bank_leader_added", lang, user=display, name=bank["currency_name"])
        else:
            text = localized("bank_leader_declined", lang)
    elif action == "bank_transfer":
        bank = db.get_bank(pend["bank_code"])
        if bank and accepted:
            db.set_bank_leader(bank["code"], "telegram", user.id, display)
            text = localized("bank_transfer_done", lang, user=display, name=bank["currency_name"])
        else:
            text = localized("bank_transfer_declined", lang)
    elif action == "treaty":
        if accepted:
            db.add_treaty(pend["mine"], pend["other"])
            text = localized("treaty_done", lang, a=pend["mine"], b=pend["other"])
        else:
            text = localized("treaty_declined", lang)
    elif action == "peg":
        if accepted:
            db.set_pegged_rate(pend["mine"], pend["other"], pend["rate"])
            text = localized("set_rate_done", lang, a=pend["mine"],
                             rate=f"{pend['rate']:g}", b=pend["other"])
        else:
            text = localized("set_rate_declined", lang)
    elif action == "ent_leader":
        ent = db.get_enterprise(pend["ent"])
        if ent and accepted:
            db.add_enterprise_leader(ent["code"], "telegram", user.id, display)
            text = localized("ent_leader_added", lang, user=display, name=ent["name"])
        else:
            text = localized("ent_leader_declined", lang)
    elif action == "ent_transfer":
        ent = db.get_enterprise(pend["ent"])
        if ent and accepted:
            db.set_enterprise_leader(ent["code"], "telegram", user.id, display)
            text = localized("ent_transfer_done", lang, user=display, name=ent["name"])
        else:
            text = localized("ent_transfer_declined", lang)
    elif action == "ent_join":
        ent = db.get_enterprise(pend["ent"])
        if ent and accepted:
            db.add_enterprise_member(ent["code"], "telegram",
                                     pend["requester_id"], pend["requester_name"])
            text = localized("ent_join_approved", lang,
                             user=pend["requester_name"], name=ent["name"])
        else:
            text = localized("ent_join_declined", lang)
    elif action == "export":
        if accepted:
            good = db.get_good(pend["good"])
            if good:
                status, info = economy.dispatch_shipment(
                    pend["from"], pend["ent"], good, pend["qty"], pend["price"],
                    pend["bank"], (pend.get("notify_platform"), pend.get("notify_key")),
                    lang)
                if status == "ok":
                    src, tgt = db.get_enterprise(pend["from"]), db.get_enterprise(pend["ent"])
                    emoji = f"{good['emoji']} " if good["emoji"] else ""
                    text = localized(
                        "export_dispatched", lang, qty=info["qty"], emoji=emoji,
                        name=good_display_name(good, lang),
                        source=f"{src['name']} [{src['code']}]" if src else pend["from"],
                        target=f"{tgt['name']} [{tgt['code']}]" if tgt else pend["ent"],
                        eta=_fmt_eta(info["duration"]),
                        distance=localized(f"transport_{info['distance']}", lang))
                else:
                    text = localized(f"export_{status}", lang)
        else:
            text = localized("export_declined", lang)
    elif action == "auto_export":
        if accepted:
            db.remove_auto_export(pend["from"], pend["ent"], pend["good"])
            db.add_auto_export(pend["from"], pend["ent"], pend["good"], pend["qty"],
                               pend["price"], pend["bank"], pend["created_by"])
            good = db.get_good(pend["good"])
            tgt = db.get_enterprise(pend["ent"])
            bank = db.get_bank(pend["bank"]) if pend["bank"] else None
            emoji = f"{good['emoji']} " if good and good["emoji"] else ""
            text = localized(
                "auto_export_set", lang, qty=pend["qty"], emoji=emoji,
                name=good_display_name(good, lang) if good else pend["good"],
                target=f"{tgt['name']} [{tgt['code']}]" if tgt else pend["ent"],
                price=economy.format_money(pend["price"], bank) if bank
                else localized("export_free", lang))
        else:
            text = localized("export_declined", lang)

    await query.answer()
    if text:
        try:
            await query.message.reply(text)
        except Exception:
            pass

@router.message(Command("create_bank", "create-bank"))
async def create_bank_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    allowed = is_admin("telegram", message.from_user.id)
    if not allowed and message.chat.type in GROUP_CHAT_TYPES:
        allowed = await is_server_admin(message.chat.id, message.from_user.id)
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return

    m = await _dialog_text_tg(message, lang, localized("create_bank_ask_chat", lang))
    if m is None:
        return
    chat = db.get_chat((m.text or "").strip())
    if not chat:
        await message.reply(localized("create_bank_chat_not_setup", lang))
        return
    union = chat["union_code"]

    m = await _dialog_text_tg(message, lang, localized("create_bank_ask_name", lang))
    if m is None:
        return
    currency_name = clean_display_name(m.text or "", max_len=40)

    code = await _dialog_text_tg(message, lang, localized("create_bank_ask_code", lang),
                                 validator=_currency_code_validator_tg,
                                 error_key="create_bank_bad_code")
    if code is None:
        return

    m = await _dialog_text_tg(message, lang, localized("create_bank_ask_emoji", lang))
    if m is None:
        return
    parts = (m.text or "").strip().split()
    emoji = parts[0][:16] if parts else ""

    db.create_bank(code, union, chat["chat_id"], currency_name, emoji,
                   "telegram", message.from_user.id, _tg_user_label(message.from_user))
    await message.reply(localized("create_bank_created", lang, name=currency_name, code=code,
                                  emoji=emoji, union=db.get_union_name(union, lang)))
    await send_service_event("bank_created", name=currency_name, code=code, union=union,
                             user=_tg_user_label(message.from_user))

def _bank_card_tg(bank, lang):
    emoji = f" {bank['emoji']}" if bank["emoji"] else ""
    lines = [f"<b>{escape_html(bank['currency_name'])} [{escape_html(bank['code'])}]{escape_html(emoji)}</b>"]
    lines.append(f"<b>{escape_html(localized('bank_field_union', lang))}:</b> "
                 f"{escape_html(db.get_union_name(bank['union_code'], lang))}")
    lines.append(f"<b>{escape_html(localized('bank_field_central', lang))}:</b> "
                 f"{escape_html(str(bank['central_chat']))}")
    leaders = db.get_bank_leaders(bank["code"])
    shown = ", ".join(format_stored_user("telegram", l["platform"], l["user_id"],
                                         l["display_name"]) for l in leaders) or "—"
    lines.append(f"<b>{escape_html(localized('bank_field_leaders', lang))}:</b> {escape_html(shown)}")
    lines.append(f"<b>{escape_html(localized('bank_field_supply', lang))}:</b> "
                 f"{escape_html(economy.format_money(db.bank_money_supply(bank['code']), bank))}")
    lines.append(f"<b>{escape_html(localized('bank_field_accounts', lang))}:</b> "
                 f"{db.bank_account_count(bank['code'])}")
    debt = db.bank_debt(bank["code"])
    if debt:
        lines.append(f"<b>{escape_html(localized('bank_field_debt', lang))}:</b> "
                     f"{escape_html(economy.format_money(debt, bank))}")
    lines.append(f"<b>{escape_html(localized('bank_field_value', lang))}:</b> {bank['value']:.4f}")
    return "\n".join(lines)

@router.message(Command("bank"))
async def bank_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("bank_usage", lang))
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    await message.reply(_bank_card_tg(bank, lang), parse_mode="HTML")

async def _bank_leadership_tg(message, transfer):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("bank_leader_usage", lang))
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    caller_is_admin = is_admin("telegram", message.from_user.id)
    if not caller_is_admin and not db.is_bank_leader(bank["code"], "telegram", message.from_user.id):
        await message.reply(localized("bank_not_leader", lang))
        return
    ref = _parse_tg_user_ref(parts[2])
    if caller_is_admin:
        tid = await _resolve_tg_target(message, parts[2])
        if tid is None:
            await message.reply(localized("bank_admin_need_id", lang))
            return
        disp = await _quiz_target_label_tg(tid)
        if transfer:
            db.set_bank_leader(bank["code"], "telegram", tid, disp)
            key = "bank_transfer_done"
        else:
            db.add_bank_leader(bank["code"], "telegram", tid, disp)
            key = "bank_leader_added"
        await message.reply(localized(key, lang, user=disp, name=bank["currency_name"]))
        return
    if ref is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    target_kind, target = ref
    mention = f"@{target}" if target_kind == "username" else str(target)
    token = _register_econ_consent({
        "action": "bank_transfer" if transfer else "bank_leader",
        "bank_code": bank["code"], "target_kind": target_kind, "target": target, "lang": lang})
    offer_key = "bank_transfer_offer" if transfer else "bank_leader_offer"
    await message.answer(
        localized(offer_key, lang, mention=mention, name=bank["currency_name"], code=bank["code"]),
        reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("bank_add_leader", "bank-add-leader"))
async def bank_add_leader_tg(message: Message):
    await _bank_leadership_tg(message, transfer=False)

@router.message(Command("bank_transfer", "bank-transfer"))
async def bank_transfer_tg(message: Message):
    await _bank_leadership_tg(message, transfer=True)

_EDIT_BANK_OPTIONS = ("name", "emoji", "code", "central", "add_leader",
                      "transfer", "delete")

async def _resolve_edit_bank_tg(message, lang, query=None):
    """Mirror of the Discord helper: any bank for a Bot Admin, only a led one
    otherwise; without a code the single bank the caller leads."""
    admin = is_admin("telegram", message.from_user.id)
    if query:
        bank = db.get_bank(query.strip().upper())
        if not bank:
            await message.reply(localized("bank_not_found", lang))
            return None
        if not (admin or db.is_bank_leader(bank["code"], "telegram", message.from_user.id)):
            await message.reply(localized("edit_bank_no_permission", lang))
            return None
        return bank
    led = [b for b in db.get_all_banks()
           if db.is_bank_leader(b["code"], "telegram", message.from_user.id)]
    if not led:
        await message.reply(localized("edit_bank_specify" if admin
                                      else "edit_bank_no_permission", lang))
        return None
    if len(led) == 1:
        return led[0]
    return await _numbered_choice_tg(message, lang, localized("edit_bank_choose", lang),
                                     led, lambda b: f"{b['currency_name']} [{b['code']}]")

@router.message(Command("edit_bank", "edit-bank"))
async def edit_bank_tg(message: Message):
    """/edit-bank [option] [code] — numbered management menu for a bank."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    option = None
    query = None
    for p in parts:
        if p.isdigit() and option is None:
            option = int(p)
        elif query is None:
            query = p
    bank = await _resolve_edit_bank_tg(message, lang, query)
    if bank is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_BANK_OPTIONS):
        items = [localized(f"edit_bank_opt_{key}", lang) for key in _EDIT_BANK_OPTIONS]
        chosen = await _numbered_choice_tg(
            message, lang,
            localized("edit_bank_menu_header", lang, name=bank["currency_name"],
                      code=bank["code"]),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_BANK_OPTIONS[chosen[0]]
    else:
        action = _EDIT_BANK_OPTIONS[option - 1]

    if action == "name":
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_name", lang))
        if m is None:
            return
        db.update_bank_field(bank["code"], "currency_name",
                             clean_display_name(m.text or "", max_len=40))
        await message.reply(localized("edit_bank_done", lang))
    elif action == "emoji":
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_emoji", lang))
        if m is None:
            return
        chunks = (m.text or "").strip().split()
        db.update_bank_field(bank["code"], "emoji", chunks[0][:16] if chunks else "")
        await message.reply(localized("edit_bank_done", lang))
    elif action == "code":
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_code", lang))
        if m is None:
            return
        new_code = _currency_code_validator_tg((m.text or "").strip())
        if new_code is None:
            await message.reply(localized("edit_bank_bad_code", lang))
            return
        old_code = bank["code"]
        db.rename_bank_code(old_code, new_code)
        await message.reply(localized("edit_bank_code_changed", lang,
                                      old=old_code, code=new_code))
    elif action == "central":
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_central", lang))
        if m is None:
            return
        chat = db.get_chat((m.text or "").strip())
        if not chat:
            await message.reply(localized("edit_bank_bad_central", lang))
            return
        db.set_bank_central_chat(bank["code"], chat["chat_id"], chat["union_code"])
        await message.reply(localized("edit_bank_done", lang))
    elif action in ("add_leader", "transfer"):
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_leader", lang))
        if m is None:
            return
        ref = _parse_tg_user_ref((m.text or "").strip())
        if ref is None:
            await message.reply(localized("edit_invalid_user", lang))
            return
        target_kind, target = ref
        mention = f"@{target}" if target_kind == "username" else str(target)
        transfer = action == "transfer"
        token = _register_econ_consent({
            "action": "bank_transfer" if transfer else "bank_leader",
            "bank_code": bank["code"], "target_kind": target_kind,
            "target": target, "lang": lang})
        offer_key = "bank_transfer_offer" if transfer else "bank_leader_offer"
        await message.answer(
            localized(offer_key, lang, mention=mention,
                      name=bank["currency_name"], code=bank["code"]),
            reply_markup=_econ_consent_keyboard(lang, token))
    elif action == "delete":
        m = await _dialog_text_tg(message, lang,
                                  localized("edit_bank_confirm_delete", lang,
                                            code=bank["code"]))
        if m is None:
            return
        if (m.text or "").strip().upper() != bank["code"]:
            await message.reply(localized("action_cancelled", lang))
            return
        name = bank["currency_name"]
        db.delete_bank(bank["code"])
        await message.reply(localized("edit_bank_deleted", lang, name=name,
                                      code=bank["code"]))
        await send_service_event("bank_deleted", name=name, code=bank["code"],
                                 user=_tg_user_label(message.from_user))

@router.message(Command("open_account", "open-account"))
async def open_account_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("open_account_usage", lang))
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    if db.account_exists(bank["code"], *owner):
        await message.reply(localized("account_exists", lang, code=bank["code"]))
        return
    db.ensure_account(bank["code"], *owner, display_name=_tg_user_label(message.from_user))
    await message.reply(localized("account_opened", lang, name=bank["currency_name"], code=bank["code"]))

@router.message(Command("balance"))
async def balance_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    parts = (message.text or "").split()
    if len(parts) > 1:
        bank = db.get_bank(parts[1].strip().upper())
        if not bank:
            await message.reply(localized("bank_not_found", lang))
            return
        banks = [bank]
    else:
        banks = [db.get_bank(a["bank_code"]) for a in db.get_owner_accounts(*owner)]
        banks = [b for b in banks if b]
    if not banks:
        await message.reply(localized("balance_none", lang))
        return
    blocks = []
    for b in banks:
        acc = db.get_account(b["code"], *owner)
        bal = acc["balance"] if acc else 0
        energy = acc["energy"] if acc else 0
        prof_code = db.get_profession(owner, b["code"])
        prof_good = db.get_good(prof_code) if prof_code else None
        prof = prof_good["name"] if prof_good else localized("profession_none", lang)
        line = localized("balance_line", lang, money=economy.format_money(bal, b),
                         energy=economy.format_energy(energy), profession=prof)
        if bal < 0:
            line += " " + localized("balance_debt_mark", lang)
        blocks.append(f"<b>{escape_html(b['currency_name'])} [{escape_html(b['code'])}]</b>\n"
                      f"{escape_html(line)}")
    await message.reply(f"<b>{escape_html(localized('balance_title', lang))}</b>\n\n"
                        + "\n\n".join(blocks), parse_mode="HTML")

@router.message(Command("pay"))
async def pay_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        if len(parts) < 2:
            await message.reply(localized("pay_usage", lang))
            return
        amount_s, code_s = parts[0], parts[1]
    else:
        if len(parts) < 3:
            await message.reply(localized("pay_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        amount_s, code_s = parts[1], parts[2]
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    bank = db.get_bank(code_s.strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    minor = economy.parse_amount(amount_s)
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    sender = db.canonical_user("telegram", message.from_user.id)
    target = db.canonical_user("telegram", target_id)
    if target == sender:
        await message.reply(localized("pay_self", lang))
        return
    tdisp = await _quiz_target_label_tg(target_id)
    if not db.transfer(bank["code"], sender, target, minor, "pay", to_display=tdisp):
        await message.reply(localized("pay_insufficient", lang))
        return
    await message.reply(localized("pay_done", lang, amount=economy.format_money(minor, bank),
                                  user=escape_html(tdisp)))

@router.message(Command("set_earn", "set-earn"))
async def set_earn_tg(message: Message):
    """Every answer here is temporary: marking a channel is a one-off setup act,
    and its confirmation should not stay in the chat people earn in."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await _reply_temp(message, localized("group_only", lang))
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await _reply_temp(message, localized("set_earn_usage", lang))
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await _reply_temp(message, localized("bank_not_found", lang))
        return
    if not (db.is_bank_leader(bank["code"], "telegram", message.from_user.id)
            or await is_server_admin(message.chat.id, message.from_user.id)
            or is_admin("telegram", message.from_user.id)):
        await _reply_temp(message, localized("set_earn_no_permission", lang))
        return
    chat = db.get_chat(str(message.chat.id))
    if not chat or chat["union_code"] != bank["union_code"]:
        await _reply_temp(message, localized("set_earn_wrong_union", lang))
        return
    action = parts[2].strip().lower()
    chan_key = _chat_key(message)
    if action in ("off", "disable", "0", "false", "no"):
        db.remove_earn_channel("telegram", chan_key)
        await _reply_temp(message, localized("set_earn_off", lang))
        return
    if action not in ("on", "enable", "1", "true", "yes"):
        await _reply_temp(message, localized("set_earn_usage", lang))
        return
    rate = 1.0
    if len(parts) > 3:
        try:
            rate = max(0.0, min(float(parts[3].replace(",", ".")), 100.0))
        except ValueError:
            rate = 1.0
    db.set_earn_channel("telegram", chan_key, bank["code"], rate)
    await _reply_temp(message, localized("set_earn_on", lang, code=bank["code"],
                                         rate=f"{rate:g}"))

def _resolve_good_bank_tg(chat, currency):
    """Mirror of the Discord helper: the bank a new good is denominated in."""
    banks = db.get_banks_in_union(chat["union_code"])
    if currency:
        bank = db.get_bank(currency.strip().upper())
        if not bank:
            return None, "bank_not_found"
        if bank["union_code"] != chat["union_code"]:
            return None, "create_good_wrong_union"
        return bank, None
    if len(banks) == 1:
        return banks[0], None
    return None, "create_good_need_bank"

def _normalize_category(raw):
    s = (raw or "").strip().lower()
    if not s or s == "-":
        return None, True
    return (s, True) if s in db.GOOD_CATEGORIES else (None, False)

@router.message(Command("create_good", "create-good"))
async def create_good_tg(message: Message):
    """/create-good <name> | <base_value> | <energy> | [emoji] | [category]
    | [currency] | [enterprise] — the good belongs to the caller (or their
    enterprise) and is denominated in a bank of this group's union."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return
    body = (message.text or "").split(maxsplit=1)
    fields = [p.strip() for p in body[1].split("|")] if len(body) > 1 else []
    if len(fields) < 3:
        await message.reply(localized("create_good_usage", lang))
        return
    chat = db.get_chat(str(message.chat.id))
    if not chat:
        await message.reply(localized("chat_not_setup", lang))
        return
    currency = fields[5] if len(fields) > 5 else None
    bank, err = _resolve_good_bank_tg(chat, currency)
    if err:
        await message.reply(localized(err, lang))
        return
    cat, ok = _normalize_category(fields[4] if len(fields) > 4 else None)
    if not ok:
        await message.reply(localized("create_good_bad_category", lang,
                                      categories=", ".join(db.GOOD_CATEGORIES)))
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    if len(fields) > 6 and fields[6]:
        ent = db.find_enterprise(fields[6])
        if not ent:
            await message.reply(localized("enterprise_not_found", lang))
            return
        if not db.is_enterprise_leader(ent["code"], "telegram", message.from_user.id):
            await message.reply(localized("ent_not_leader", lang))
            return
        owner = db.enterprise_owner(ent["code"])
    name = clean_display_name(fields[0], max_len=40)
    base = economy.parse_amount(fields[1])
    try:
        energy_cost = int(fields[2])
    except ValueError:
        energy_cost = 0
    if base is None or energy_cost <= 0:
        await message.reply(localized("create_good_bad", lang))
        return
    emoji = fields[3].split()[0][:16] if len(fields) > 3 and fields[3].split() else ""
    gcode = db.create_good(bank["code"], name, base, energy_cost, emoji,
                           owner=owner, category=cat)
    await message.reply(localized("create_good_created", lang, name=name, code=gcode,
                                  value=economy.format_money(base, bank),
                                  energy=economy.format_energy(energy_cost), bank=bank["code"]))
    if cat:
        await message.answer(localized("create_good_category_note", lang,
                                       category=category_label(cat, lang)))

def _fmt_duration(seconds):
    seconds = max(int(seconds), 0)
    return f"{seconds // 60}:{seconds % 60:02d}"

def _fmt_eta(seconds):
    """mm:ss for short waits, h:mm:ss once a shipment runs into hours."""
    seconds = max(int(seconds), 0)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"

def _resolve_production_target_tg(message, enterprise_query):
    """(owner, server, error_key) of a production request — Telegram mirror."""
    if enterprise_query:
        ent = db.find_enterprise(enterprise_query)
        if not ent:
            return None, None, "enterprise_not_found"
        if not db.is_enterprise_worker(ent["code"], "telegram", message.from_user.id):
            return None, None, "ent_not_worker"
        return db.enterprise_owner(ent["code"]), (ent["platform"], ent["server_id"]), None
    owner = db.canonical_user("telegram", message.from_user.id)
    server = ("telegram", str(message.chat.id)) \
        if message.chat.type in GROUP_CHAT_TYPES else (None, None)
    return owner, server, None

@router.message(Command("craft"))
async def craft_tg(message: Message):
    """/craft <good_code> [enterprise] — start a timed production run."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("craft_usage", lang))
        return
    good = db.get_good(parts[1].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    owner, server, err = _resolve_production_target_tg(
        message, parts[2] if len(parts) > 2 else None)
    if err:
        await message.reply(localized(err, lang))
        return
    worker = db.canonical_user("telegram", message.from_user.id)
    notify = ("telegram", _chat_key(message))
    status, info = economy.start_production(
        worker, good, owner, server, notify, lang,
        starter_display=_tg_user_label(message.from_user))
    if status == "busy":
        await message.reply(localized("production_busy", lang,
                                      time=_fmt_duration(info["finish_at"] - int(time.time()))))
        return
    if status == "no_energy":
        await message.reply(localized("craft_no_energy", lang,
                                      cost=economy.format_energy(info["cost"]),
                                      have=economy.format_energy(info["have"])))
        return
    emoji = f" {good['emoji']}" if good["emoji"] else ""
    await message.reply(localized("production_started", lang,
                                  name=good_display_name(good, lang), emoji=emoji,
                                  qty=info["qty"], duration=_fmt_duration(info["duration"]),
                                  cost=economy.format_energy(info["cost"])))

@router.message(Command("inventory"))
async def inventory_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    inv = db.get_inventory(owner)
    if not inv:
        await message.reply(localized("inventory_empty", lang))
        return
    lines = [f"<b>{escape_html(localized('inventory_title', lang))}</b>"]
    for row in inv:
        m = db.get_produced(owner, row["good_code"])
        good = db.get_good(row["good_code"])
        bank = db.get_bank(row["bank_code"])
        val = economy.format_money(economy.unit_value(good, m), bank) if good and bank else "—"
        emoji = f"{row['emoji']} " if row["emoji"] else ""
        lines.append(escape_html(localized(
            "inventory_line", lang, emoji=emoji,
            name=good_display_name(good, lang) if good else row["name"],
            code=row["good_code"], qty=row["qty"], value=val,
            level=economy.mastery_level(m))))
    await message.reply("\n".join(lines), parse_mode="HTML")

@router.message(Command("sell"))
async def sell_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("sell_usage", lang))
        return
    good = db.get_good(parts[1].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    have = db.get_inventory_qty(owner, good["code"])
    want = have
    if len(parts) > 2 and parts[2].isdigit() and int(parts[2]) > 0:
        want = int(parts[2])
    status, info = economy.sell(owner, good, want)
    if status == "not_sellable":
        await message.reply(localized("sell_not_sellable", lang,
                                      name=good_display_name(good, lang)))
        return
    if status == "nothing":
        await message.reply(localized("sell_nothing", lang, name=good["name"]))
        return
    bank = db.get_bank(good["bank_code"])
    await message.reply(localized("sell_done", lang, qty=info["qty"], name=good["name"],
                                  unit=economy.format_money(info["unit"], bank),
                                  total=economy.format_money(info["total"], bank)))

@router.message(Command("autocraft"))
async def autocraft_tg(message: Message):
    """/autocraft <good_code> <on|off> [enterprise] — 24/7 autoproduction."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("autocraft_usage", lang))
        return
    good = db.get_good(parts[1].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    owner, server, err = _resolve_production_target_tg(
        message, parts[3] if len(parts) > 3 else None)
    if err:
        await message.reply(localized(err, lang))
        return
    worker = db.canonical_user("telegram", message.from_user.id)
    on = parts[2].strip().lower() in ("on", "enable", "1", "true", "yes")
    db.set_autocraft(owner, good["code"], on,
                     starter=(worker[1], worker[2]), server=server)
    await message.reply(localized("autocraft_on" if on else "autocraft_off", lang,
                                  name=good_display_name(good, lang)))

@router.message(Command("autosend"))
async def autosend_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 4:
        await message.reply(localized("autosend_usage", lang))
        return
    good = db.get_good(parts[1].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    if not parts[2].isdigit() or int(parts[2]) > 100:
        await message.reply(localized("autosend_bad_percent", lang))
        return
    percent = int(parts[2])
    owner = db.canonical_user("telegram", message.from_user.id)
    target_arg = parts[3]
    party = db.get_party(target_arg.strip().upper())
    ent = db.get_enterprise(target_arg.strip().upper())
    if party:
        tgt, tname = db.party_owner(party["code"]), f"{party['name']} [{party['code']}]"
    elif ent:
        tgt, tname = db.enterprise_owner(ent["code"]), f"{ent['name']} [{ent['code']}]"
    else:
        tid = await _resolve_tg_target(message, target_arg)
        if tid is None:
            await message.reply(localized("autosend_bad_target", lang))
            return
        tgt = db.canonical_user("telegram", tid)
        tname = await _quiz_target_label_tg(tid)
    if percent == 0:
        db.set_autosend(owner, good["code"], 0, tgt)
        await message.reply(localized("autosend_off", lang, name=good["name"]))
        return
    db.set_autosend(owner, good["code"], percent, tgt)
    await message.reply(localized("autosend_on", lang, name=good["name"], percent=percent,
                                  target=tname))

@router.message(Command("set_profession", "set-profession"))
async def set_profession_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("set_profession_usage", lang))
        return
    good = db.get_good(parts[2].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    if not (db.is_bank_leader(good["bank_code"], "telegram", message.from_user.id)
            or is_admin("telegram", message.from_user.id)):
        await message.reply(localized("bank_not_leader", lang))
        return
    tid = await _resolve_tg_target(message, parts[1])
    if tid is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    db.set_profession(db.canonical_user("telegram", tid), good["bank_code"], good["code"])
    disp = await _quiz_target_label_tg(tid)
    await message.reply(localized("set_profession_done", lang, user=escape_html(disp),
                                  name=good["name"]))

@router.message(Command("fine"))
async def fine_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    bank = await _resolve_led_bank_tg(message, lang)
    if bank is None:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        if not parts:
            await message.reply(localized("fine_usage", lang))
            return
        amount_s = parts[0]
        reason = " ".join(parts[1:]).strip() or None
    else:
        if len(parts) < 2:
            await message.reply(localized("fine_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        amount_s = parts[1]
        reason = " ".join(parts[2:]).strip() or None
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    minor = economy.parse_amount(amount_s)
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    towner = db.canonical_user("telegram", target_id)
    db.burn(bank["code"], towner, minor, f"fine: {reason}" if reason else "fine", allow_negative=True)
    disp = await _quiz_target_label_tg(target_id)
    await message.reply(localized("fine_done", lang, user=escape_html(disp),
                                  amount=economy.format_money(minor, bank),
                                  reason=(reason[:200] if reason else localized("fine_no_reason", lang))))

@router.message(Command("treaty"))
async def treaty_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("treaty_usage", lang))
        return
    other = db.get_bank(parts[1].strip().upper())
    if not other:
        await message.reply(localized("bank_not_found", lang))
        return
    mine = await _resolve_led_bank_tg(message, lang)
    if mine is None:
        return
    if mine["code"] == other["code"]:
        await message.reply(localized("treaty_self", lang))
        return
    if db.has_treaty(mine["code"], other["code"]):
        await message.reply(localized("treaty_exists", lang))
        return
    token = _register_econ_consent({"action": "treaty", "mine": mine["code"],
                                    "other": other["code"], "lang": lang})
    await message.answer(localized("treaty_offer", lang, proposer=mine["code"],
                                   code=other["code"], name=other["currency_name"]),
                         reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("set_rate", "set-rate"))
async def set_rate_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("set_rate_usage", lang))
        return
    other = db.get_bank(parts[1].strip().upper())
    if not other:
        await message.reply(localized("bank_not_found", lang))
        return
    try:
        rate = float(parts[2].replace(",", "."))
    except ValueError:
        rate = 0.0
    if rate <= 0:
        await message.reply(localized("set_rate_bad", lang))
        return
    mine = await _resolve_led_bank_tg(message, lang)
    if mine is None:
        return
    if not db.has_treaty(mine["code"], other["code"]):
        await message.reply(localized("set_rate_no_treaty", lang))
        return
    if not economy.can_manual_peg(mine["code"], other["code"]):
        await message.reply(localized("set_rate_multi", lang))
        return
    token = _register_econ_consent({"action": "peg", "mine": mine["code"],
                                    "other": other["code"], "rate": rate, "lang": lang})
    await message.answer(localized("set_rate_offer", lang, a=mine["code"], rate=f"{rate:g}",
                                   b=other["code"]),
                         reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("convert"))
async def convert_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 4:
        await message.reply(localized("convert_usage", lang))
        return
    minor = economy.parse_amount(parts[1])
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    fb = db.get_bank(parts[2].strip().upper())
    tb = db.get_bank(parts[3].strip().upper())
    if not fb or not tb:
        await message.reply(localized("bank_not_found", lang))
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    status, info = economy.convert(owner, fb["code"], tb["code"], minor)
    keymap = {"same": "convert_same", "not_convertible": "convert_not_convertible",
              "too_small": "convert_too_small", "no_funds": "convert_no_funds"}
    if status != "ok":
        await message.reply(localized(keymap.get(status, "convert_not_convertible"), lang))
        return
    await message.reply(localized("convert_done", lang, amount=economy.format_money(minor, fb),
                                  credited=economy.format_money(info["credited"], tb),
                                  rate=f"{info['rate']:.4f}",
                                  fee=economy.format_money(info["fee"], fb)))

@router.message(Command("rates"))
async def rates_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    parts = (message.text or "").split()
    if len(parts) > 1:
        bank = db.get_bank(parts[1].strip().upper())
        if not bank:
            await message.reply(localized("bank_not_found", lang))
            return
        partners = db.treaty_partners(bank["code"])
        if not partners:
            await message.reply(localized("rates_none", lang, code=bank["code"]))
            return
        lines = [localized("rates_title_one", lang, code=bank["code"])]
        for p in partners:
            r = economy.rate(bank["code"], p)
            treaty = db.get_treaty(bank["code"], p)
            peg = f" {localized('rates_pegged', lang)}" if treaty and treaty["pegged_rate"] is not None else ""
            lines.append(localized("rates_line", lang, a=bank["code"], b=p,
                                   rate=f"{r:.4f}" if r is not None else "—", peg=peg))
    else:
        banks = db.get_all_banks()
        if not banks:
            await message.reply(localized("rates_no_banks", lang))
            return
        lines = [localized("rates_title_all", lang)]
        lines += [localized("rates_value_line", lang, code=b["code"], name=b["currency_name"],
                            value=f"{b['value']:.4f}") for b in banks]
    await message.reply("\n".join(lines))

@router.message(Command("set_wage", "set-wage"))
async def set_wage_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("set_wage_usage", lang))
        return
    bank = await _resolve_led_bank_tg(message, lang)
    if bank is None:
        return
    good = db.find_bank_good(bank["code"], parts[1])
    if not good:
        await message.reply(localized("set_wage_no_good", lang, bank=bank["code"]))
        return
    if parts[2].strip() in ("0", "0.0", "0.00"):
        db.set_wage(bank["code"], good["code"], None)
        await message.reply(localized("set_wage_cleared", lang, name=good["name"]))
        return
    minor = economy.parse_amount(parts[2])
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    db.set_wage(bank["code"], good["code"], minor)
    await message.reply(localized("set_wage_done", lang, name=good["name"],
                                  amount=economy.format_money(minor, bank)))

@router.message(Command("party_dues", "party-dues"))
async def party_dues_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("party_dues_usage", lang))
        return
    party = await _resolve_led_party_tg(message, lang)
    if party is None:
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    if bank["union_code"] != party["union_code"]:
        await message.reply(localized("dues_wrong_union", lang))
        return
    if parts[2].strip() in ("0", "0.0", "0.00"):
        db.set_party_dues(party["code"], bank["code"], 0)
        await message.reply(localized("dues_cleared", lang, name=party["name"]))
        return
    minor = economy.parse_amount(parts[2])
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    db.set_party_dues(party["code"], bank["code"], minor)
    await message.reply(localized("dues_done", lang, name=party["name"],
                                  amount=economy.format_money(minor, bank)))

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
    await _set_channel_tg(message, "logs")

@router.message(Command("settasks"))
async def settasks_tg(message: Message):
    await _set_channel_tg(message, "tasks")

def _ent_line_tg(ent):
    return f"{ent['name']} [{ent['code']}]"

async def _resolve_led_enterprise_tg(message, lang, query=None):
    if query:
        ent = db.find_enterprise(query)
        if not ent:
            await message.reply(localized("enterprise_not_found", lang))
            return None
        if not (db.is_enterprise_leader(ent["code"], "telegram", message.from_user.id)
                or is_admin("telegram", message.from_user.id)):
            await message.reply(localized("ent_not_leader", lang))
            return None
        return ent
    ents = db.user_led_enterprises("telegram", message.from_user.id)
    if not ents:
        await message.reply(localized("ent_none_led", lang))
        return None
    if len(ents) == 1:
        return ents[0]
    return await _numbered_choice_tg(message, lang, localized("ent_choose", lang),
                                     ents, _ent_line_tg)

@router.message(Command("add_enterprise", "add-enterprise"))
async def add_enterprise_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return
    if not db.is_setup(str(message.chat.id)):
        await message.reply(localized("chat_not_setup", lang))
        return
    if not rate_limit_ok(f"addent|telegram|{message.from_user.id}", 3, 86400):
        await message.reply(localized("rate_limited", lang))
        return
    m = await _dialog_text_tg(message, lang, localized("add_ent_ask_name", lang))
    if m is None:
        return
    name = clean_display_name(m.text or "", max_len=60)
    code = await _dialog_text_tg(message, lang, localized("add_ent_ask_code", lang),
                                 validator=_party_code_validator,
                                 error_key="add_ent_bad_code")
    if code is None:
        return
    m = await _dialog_text_tg(message, lang, localized("add_ent_ask_desc", lang))
    if m is None:
        return
    description = (m.text or "").strip()
    description = None if description == "-" else description[:500]
    await message.reply(localized("add_ent_ask_logo", lang))
    logo = logo_mime = None
    for _ in range(5):
        m = await _wait_user_message(message.chat.id, message.from_user.id)
        if m is None:
            await message.reply(localized("dialog_timeout", lang))
            return
        if (m.text or "").strip() == "-":
            break
        got = await _extract_logo_tg(m)
        if got:
            logo, logo_mime = got
            break
        await message.reply(localized("add_party_invalid_logo", lang))
    db.create_enterprise(code, name, "telegram", message.chat.id,
                         "telegram", message.from_user.id,
                         _tg_user_label(message.from_user),
                         description=description, logo=logo, logo_mime=logo_mime)
    await message.reply(localized("add_ent_created", lang, name=name, code=code))
    await send_service_event("enterprise_created", name=name, code=code,
                             user=_tg_user_label(message.from_user))

def _enterprise_card_tg(ent, lang):
    lines = [f"<b>{escape_html(ent['name'])} [{escape_html(ent['code'])}]</b>"]
    if ent["description"]:
        lines.append(escape_html(ent["description"][:1000]))
    lines.append(f"<b>{escape_html(localized('ent_field_server', lang))}:</b> "
                 f"{escape_html(str(ent['server_id']))}")
    leaders = db.get_enterprise_leaders(ent["code"])
    shown = ", ".join(format_stored_user("telegram", l["platform"], l["user_id"],
                                         l["display_name"]) for l in leaders) or "—"
    lines.append(f"<b>{escape_html(localized('ent_field_leaders', lang))}:</b> {escape_html(shown)}")
    members = db.get_enterprise_members(ent["code"])
    if members:
        mlines = []
        for m in members[:15]:
            pos = f" — {m['position']}" if m["position"] else ""
            mlines.append(escape_html(format_stored_user(
                "telegram", m["platform"], m["user_id"], m["display_name"]) + pos))
        if len(members) > 15:
            mlines.append(f"… +{len(members) - 15}")
        lines.append(f"<b>{escape_html(localized('ent_field_workers', lang, count=len(members)))}:</b>\n"
                     + "\n".join(mlines))
    balances = []
    for acc in db.get_owner_accounts(*db.enterprise_owner(ent["code"])):
        bank = db.get_bank(acc["bank_code"])
        if bank:
            balances.append(escape_html(economy.format_money(acc["balance"], bank)))
    if balances:
        lines.append(f"<b>{escape_html(localized('ent_field_balance', lang))}:</b> "
                     + ", ".join(balances))
    period = localized(f"salary_period_{ent['salary_period'] or 'monthly'}", lang)
    lines.append(f"<b>{escape_html(localized('ent_field_salary', lang))}:</b> "
                 f"{escape_html(period)} · {escape_html(ent['salary_bank'] or '—')}")
    positions = db.get_enterprise_positions(ent["code"])
    if positions:
        plines = []
        for p in positions:
            if p["percent"] is not None:
                sal = localized("salary_percent", lang, percent=f"{p['percent']:g}")
            else:
                sal = economy.format_amount(p["amount"] or 0)
            plines.append(escape_html(f"{p['position']}: {sal}"))
        lines.append(f"<b>{escape_html(localized('ent_field_positions', lang))}:</b>\n"
                     + "\n".join(plines))
    inv = db.get_inventory(db.enterprise_owner(ent["code"]))
    if inv:
        glines = []
        for row in inv[:8]:
            emoji = f"{row['emoji']} " if row["emoji"] else ""
            good = db.get_good(row["good_code"])
            nm = good_display_name(good, lang) if good else row["name"]
            glines.append(escape_html(f"{emoji}{nm} [{row['good_code']}] ×{row['qty']}"))
        lines.append(f"<b>{escape_html(localized('ent_field_goods', lang))}:</b>\n"
                     + "\n".join(glines))
    transit = db.get_enterprise_shipments(ent["code"])
    if transit:
        lines.append(
            f"<b>{escape_html(localized('ent_field_transit', lang, count=len(transit)))}:</b>\n"
            + "\n".join(escape_html(l) for l in _transit_lines_tg(ent["code"], transit[:8], lang)))
    return "\n".join(lines)

def _transit_lines_tg(ent_code, shipments, lang):
    """In-transit shipments seen from `ent_code`: outgoing (➡️) and incoming (⬅️)."""
    now = int(time.time())
    out = []
    for s in shipments:
        good = db.get_good(s["good_code"])
        emoji = f"{good['emoji']} " if good and good["emoji"] else ""
        name = good_display_name(good, lang) if good else s["good_code"]
        eta = _fmt_eta(max(int(s["arrive_at"]) - now, 0))
        if s["from_ent"] == ent_code:
            other = db.get_enterprise(s["to_ent"])
            key, label = "transit_line_out", (_ent_line_tg(other) if other else s["to_ent"])
        else:
            other = db.get_enterprise(s["from_ent"])
            key, label = "transit_line_in", (_ent_line_tg(other) if other else s["from_ent"])
        out.append(localized(key, lang, emoji=emoji, name=name, qty=s["qty"],
                             other=label, eta=eta))
    return out

@router.message(Command("enterprise"))
async def enterprise_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        ents = db.get_server_enterprises("telegram", str(message.chat.id)) \
            if message.chat.type in GROUP_CHAT_TYPES else []
        if not ents:
            await message.reply(localized("ent_list_empty", lang))
            return
        lines = [f"<b>{escape_html(localized('ent_list_header', lang))}</b>"]
        lines += [escape_html(_ent_line_tg(e)) for e in ents]
        await message.reply("\n".join(lines), parse_mode="HTML")
        return
    ent = db.find_enterprise(parts[1])
    if not ent:
        await message.reply(localized("enterprise_not_found", lang))
        return
    card = _enterprise_card_tg(ent, lang)
    if ent["logo"]:
        try:
            photo = BufferedInputFile(ent["logo"], filename="logo.png")
            await message.reply_photo(photo, caption=card[:1024], parse_mode="HTML")
            return
        except Exception:
            pass
    await message.reply(card, parse_mode="HTML")

_EDIT_ENT_OPTIONS = ("name", "description", "logo", "add_leader", "transfer",
                     "salary_bank", "salary_period", "delete")

@router.message(Command("edit_enterprise", "edit-enterprise"))
async def edit_enterprise_tg(message: Message):
    """/edit-enterprise [option] [code] — numbered management menu."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    option = None
    query = None
    for p in parts:
        if p.isdigit() and option is None:
            option = int(p)
        elif query is None:
            query = p
    ent = await _resolve_led_enterprise_tg(message, lang, query)
    if ent is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_ENT_OPTIONS):
        items = [localized(f"edit_ent_opt_{key}", lang) for key in _EDIT_ENT_OPTIONS]
        chosen = await _numbered_choice_tg(
            message, lang, localized("edit_ent_menu_header", lang, name=ent["name"]),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_ENT_OPTIONS[chosen[0]]
    else:
        action = _EDIT_ENT_OPTIONS[option - 1]

    if action == "name":
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_name", lang))
        if m is None:
            return
        db.update_enterprise_field(ent["code"], "name",
                                   clean_display_name(m.text or "", max_len=60))
        await message.reply(localized("edit_ent_done", lang))
    elif action == "description":
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_desc", lang))
        if m is None:
            return
        text = (m.text or "").strip()
        db.update_enterprise_field(ent["code"], "description",
                                   None if text == "-" else text[:500])
        await message.reply(localized("edit_ent_done", lang))
    elif action == "logo":
        logo = await _dialog_logo_tg(message, lang, localized("edit_ent_ask_logo", lang))
        if logo is None:
            return
        db.update_enterprise_logo(ent["code"], logo[0], logo[1])
        await message.reply(localized("edit_ent_done", lang))
    elif action in ("add_leader", "transfer"):
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_leader", lang))
        if m is None:
            return
        ref = _parse_tg_user_ref((m.text or "").strip())
        if ref is None:
            await message.reply(localized("edit_invalid_user", lang))
            return
        target_kind, target = ref
        mention = f"@{target}" if target_kind == "username" else str(target)
        transfer = action == "transfer"
        token = _register_econ_consent({
            "action": "ent_transfer" if transfer else "ent_leader",
            "ent": ent["code"], "target_kind": target_kind, "target": target,
            "lang": lang})
        offer_key = "ent_transfer_offer" if transfer else "ent_leader_offer"
        await message.answer(
            localized(offer_key, lang, mention=mention, name=ent["name"], code=ent["code"]),
            reply_markup=_econ_consent_keyboard(lang, token))
    elif action == "salary_bank":
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_salary_bank", lang))
        if m is None:
            return
        bank = db.get_bank((m.text or "").strip().upper())
        if not bank:
            await message.reply(localized("bank_not_found", lang))
            return
        db.update_enterprise_field(ent["code"], "salary_bank", bank["code"])
        await message.reply(localized("edit_ent_done", lang))
    elif action == "salary_period":
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_salary_period", lang))
        if m is None:
            return
        period = (m.text or "").strip().lower()
        if period not in ("weekly", "monthly"):
            await message.reply(localized("edit_ent_bad_value", lang))
            return
        db.update_enterprise_field(ent["code"], "salary_period", period)
        await message.reply(localized("edit_ent_done", lang))
    elif action == "delete":
        m = await _dialog_text_tg(message, lang,
                                  localized("edit_ent_confirm_delete", lang, code=ent["code"]))
        if m is None:
            return
        if (m.text or "").strip().upper() != ent["code"]:
            await message.reply(localized("action_cancelled", lang))
            return
        db.delete_enterprise(ent["code"])
        await message.reply(localized("edit_ent_deleted", lang, name=ent["name"]))
        await send_service_event("enterprise_deleted", name=ent["name"], code=ent["code"],
                                 user=_tg_user_label(message.from_user))

@router.message(Command("ent_join", "ent-join"))
async def ent_join_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(localized("ent_join_usage", lang))
        return
    ent = db.find_enterprise(parts[1])
    if not ent:
        await message.reply(localized("enterprise_not_found", lang))
        return
    if db.is_enterprise_worker(ent["code"], "telegram", message.from_user.id):
        await message.reply(localized("ent_join_already", lang))
        return
    display = _tg_user_label(message.from_user)
    token = _register_econ_consent({
        "action": "ent_join", "ent": ent["code"], "requester_id": message.from_user.id,
        "requester_name": display, "lang": lang})
    await message.answer(
        localized("ent_join_request", lang, user=display, name=ent["name"], code=ent["code"]),
        reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("ent_leave", "ent-leave"))
async def ent_leave_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    ents = db.user_member_enterprises("telegram", message.from_user.id)
    if len(parts) > 1:
        q = parts[1].strip().lower()
        ents = [e for e in ents if e["code"].lower() == q or e["name"].lower() == q]
    if not ents:
        await message.reply(localized("ent_leave_none", lang))
        return
    ent = ents[0]
    if len(ents) > 1:
        ent = await _numbered_choice_tg(message, lang, localized("ent_choose", lang),
                                        ents, _ent_line_tg)
        if ent is None:
            return
    db.remove_enterprise_member(ent["code"], "telegram", message.from_user.id)
    await message.reply(localized("ent_leave_done", lang, name=ent["name"]))

@router.message(Command("ent_kick", "ent-kick"))
async def ent_kick_tg(message: Message):
    """/ent-kick <user> [code] — or reply to the worker with /ent-kick [code]."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        query = parts[0] if parts else None
    else:
        if not parts:
            await message.reply(localized("ent_kick_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        query = parts[1] if len(parts) > 1 else None
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    ent = await _resolve_led_enterprise_tg(message, lang, query)
    if ent is None:
        return
    if not db.remove_enterprise_member(ent["code"], "telegram", target_id):
        await message.reply(localized("ent_kick_not_member", lang))
        return
    disp = await _quiz_target_label_tg(target_id)
    await message.reply(localized("ent_kick_done", lang, user=escape_html(disp),
                                  name=ent["name"]))

def _salary_display(kind, value, lang):
    if kind == "percent":
        return localized("salary_percent", lang, percent=f"{value:g}")
    return economy.format_amount(value)

@router.message(Command("ent_position", "ent-position"))
async def ent_position_tg(message: Message):
    """/ent-position <name> | <salary> [| <enterprise>]"""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    body = (message.text or "").split(maxsplit=1)
    fields = [p.strip() for p in body[1].split("|")] if len(body) > 1 else []
    if len(fields) < 2:
        await message.reply(localized("ent_position_usage", lang))
        return
    ent = await _resolve_led_enterprise_tg(message, lang,
                                           fields[2] if len(fields) > 2 else None)
    if ent is None:
        return
    parsed = economy.parse_salary(fields[1])
    if parsed is None:
        await message.reply(localized("bad_amount", lang))
        return
    kind, value = parsed
    pos = clean_display_name(fields[0], max_len=40)
    if kind == "clear":
        db.set_position_salary(ent["code"], pos, None, None)
        await message.reply(localized("ent_position_cleared", lang, position=pos))
        return
    db.set_position_salary(ent["code"], pos,
                           value if kind == "amount" else None,
                           value if kind == "percent" else None)
    await message.reply(localized("ent_position_done", lang, position=pos,
                                  salary=_salary_display(kind, value, lang)))

@router.message(Command("ent_assign", "ent-assign"))
async def ent_assign_tg(message: Message):
    """/ent-assign <user> | <position> [| <enterprise>] — or a reply with
    /ent-assign <position> [| <enterprise>]."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    body = (message.text or "").split(maxsplit=1)
    fields = [p.strip() for p in body[1].split("|")] if len(body) > 1 else []
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        pos_field = fields[0] if fields else None
        query = fields[1] if len(fields) > 1 else None
    else:
        if len(fields) < 2:
            await message.reply(localized("ent_assign_usage", lang))
            return
        target_id = await _resolve_tg_target(message, fields[0])
        pos_field = fields[1]
        query = fields[2] if len(fields) > 2 else None
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    if not pos_field:
        await message.reply(localized("ent_assign_usage", lang))
        return
    ent = await _resolve_led_enterprise_tg(message, lang, query)
    if ent is None:
        return
    if not db.get_enterprise_member(ent["code"], "telegram", target_id):
        await message.reply(localized("ent_kick_not_member", lang))
        return
    disp = await _quiz_target_label_tg(target_id)
    pos = clean_display_name(pos_field, max_len=40)
    if pos == "-":
        db.set_member_position(ent["code"], "telegram", target_id, None)
        await message.reply(localized("ent_assign_cleared", lang, user=escape_html(disp)))
        return
    db.set_member_position(ent["code"], "telegram", target_id, pos)
    await message.reply(localized("ent_assign_done", lang, user=escape_html(disp),
                                  position=pos))

@router.message(Command("ent_salary", "ent-salary"))
async def ent_salary_tg(message: Message):
    """/ent-salary <user> <salary> [enterprise] — or a reply with
    /ent-salary <salary> [enterprise]."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        if not parts:
            await message.reply(localized("ent_salary_usage", lang))
            return
        salary_s = parts[0]
        query = parts[1] if len(parts) > 1 else None
    else:
        if len(parts) < 2:
            await message.reply(localized("ent_salary_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        salary_s = parts[1]
        query = parts[2] if len(parts) > 2 else None
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    ent = await _resolve_led_enterprise_tg(message, lang, query)
    if ent is None:
        return
    if not db.is_enterprise_worker(ent["code"], "telegram", target_id):
        await message.reply(localized("ent_kick_not_member", lang))
        return
    parsed = economy.parse_salary(salary_s)
    if parsed is None:
        await message.reply(localized("bad_amount", lang))
        return
    kind, value = parsed
    disp = await _quiz_target_label_tg(target_id)
    if kind == "clear":
        db.set_personal_salary(ent["code"], "telegram", target_id, None, None)
        await message.reply(localized("ent_salary_cleared", lang, user=escape_html(disp)))
        return
    db.set_personal_salary(ent["code"], "telegram", target_id,
                           value if kind == "amount" else None,
                           value if kind == "percent" else None)
    await message.reply(localized("ent_salary_done", lang, user=escape_html(disp),
                                  salary=_salary_display(kind, value, lang)))

@router.message(Command("ent_sell", "ent-sell"))
async def ent_sell_tg(message: Message):
    """/ent-sell <good_code> [qty] [enterprise]"""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if not parts:
        await message.reply(localized("ent_sell_usage", lang))
        return
    good = db.get_good(parts[0].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    qty = None
    query = None
    for p in parts[1:]:
        if p.isdigit() and qty is None:
            qty = int(p)
        elif query is None:
            query = p
    ent = await _resolve_led_enterprise_tg(message, lang, query)
    if ent is None:
        return
    owner = db.enterprise_owner(ent["code"])
    have = db.get_inventory_qty(owner, good["code"])
    want = qty if (qty and qty > 0) else have
    status, info = economy.sell(owner, good, want)
    if status == "not_sellable":
        await message.reply(localized("sell_not_sellable", lang,
                                      name=good_display_name(good, lang)))
        return
    if status == "nothing":
        await message.reply(localized("sell_nothing", lang, name=good["name"]))
        return
    bank = db.get_bank(good["bank_code"])
    await message.reply(localized("sell_done", lang, qty=info["qty"], name=good["name"],
                                  unit=economy.format_money(info["unit"], bank),
                                  total=economy.format_money(info["total"], bank)))

def _resolve_export_args_tg(target_query, price_s, currency, source):
    """(target, price, bank, error_key_or_None) — Telegram mirror."""
    target = db.find_enterprise(target_query)
    if not target:
        return None, None, None, "export_bad_target"
    if source and target["code"] == source["code"]:
        return None, None, None, "export_same"
    price = 0
    bank = None
    if price_s:
        price = economy.parse_amount(price_s)
        if price is None:
            return None, None, None, "bad_amount"
        code = (currency or (source["salary_bank"] if source else None) or "").strip().upper()
        bank = db.get_bank(code) if code else None
        if not bank:
            return None, None, None, "export_need_currency"
    return target, price, bank, None

@router.message(Command("export"))
async def export_tg(message: Message):
    """/export <good_code> <qty> <target_ent> [price] [currency] [your_ent]"""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if len(parts) < 3:
        await message.reply(localized("export_usage", lang))
        return
    good = db.get_good(parts[0].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    if not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.reply(localized("bad_amount", lang))
        return
    qty = int(parts[1])
    source = await _resolve_led_enterprise_tg(message, lang,
                                              parts[5] if len(parts) > 5 else None)
    if source is None:
        return
    tgt, price, bank, err = _resolve_export_args_tg(
        parts[2], parts[3] if len(parts) > 3 else None,
        parts[4] if len(parts) > 4 else None, source)
    if err:
        await message.reply(localized(err, lang))
        return
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    name = good_display_name(good, lang)
    price_disp = economy.format_money(price, bank) if price else localized("export_free", lang)
    token = _register_econ_consent({
        "action": "export", "ent": tgt["code"], "from": source["code"],
        "good": good["code"], "qty": qty, "price": price,
        "bank": bank["code"] if bank else None,
        "notify_platform": "telegram", "notify_key": _chat_key(message), "lang": lang})
    await message.answer(
        localized("export_offer", lang, source=_ent_line_tg(source), target=_ent_line_tg(tgt),
                  qty=qty, emoji=emoji, name=name, price=price_disp),
        reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("auto_export", "auto-export"))
async def auto_export_tg(message: Message):
    """/auto-export <good_code> <qty|off> <target_ent> [price] [currency] [your_ent]"""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if len(parts) < 3:
        await message.reply(localized("auto_export_usage", lang))
        return
    good = db.get_good(parts[0].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    source = await _resolve_led_enterprise_tg(message, lang,
                                              parts[5] if len(parts) > 5 else None)
    if source is None:
        return
    tgt = db.find_enterprise(parts[2])
    if not tgt:
        await message.reply(localized("export_bad_target", lang))
        return
    if parts[1].strip().lower() in ("off", "0", "stop"):
        db.remove_auto_export(source["code"], tgt["code"], good["code"])
        await message.reply(localized("auto_export_removed", lang,
                                      name=good_display_name(good, lang),
                                      target=_ent_line_tg(tgt)))
        return
    if not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.reply(localized("bad_amount", lang))
        return
    qty = int(parts[1])
    tgt, price, bank, err = _resolve_export_args_tg(
        parts[2], parts[3] if len(parts) > 3 else None,
        parts[4] if len(parts) > 4 else None, source)
    if err:
        await message.reply(localized(err, lang))
        return
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    name = good_display_name(good, lang)
    price_disp = economy.format_money(price, bank) if price else localized("export_free", lang)
    token = _register_econ_consent({
        "action": "auto_export", "ent": tgt["code"], "from": source["code"],
        "good": good["code"], "qty": qty, "price": price,
        "bank": bank["code"] if bank else None,
        "created_by": message.from_user.id, "lang": lang})
    await message.answer(
        localized("auto_export_offer", lang, source=_ent_line_tg(source),
                  target=_ent_line_tg(tgt), qty=qty, emoji=emoji, name=name, price=price_disp),
        reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("transit"))
async def transit_tg(message: Message):
    """/transit [code] — an enterprise's goods in transit."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        ent = db.find_enterprise(parts[1])
        if not ent:
            await message.reply(localized("enterprise_not_found", lang))
            return
    else:
        ent = await _resolve_led_enterprise_tg(message, lang)
        if ent is None:
            return
    shipments = db.get_enterprise_shipments(ent["code"])
    if not shipments:
        await message.reply(localized("transit_empty", lang, name=ent["name"]))
        return
    header = localized("transit_title", lang, name=ent["name"], code=ent["code"])
    body = "\n".join(escape_html(l) for l in _transit_lines_tg(ent["code"], shipments, lang))
    await message.reply(f"<b>{escape_html(header)}</b>\n{body}"[:4000], parse_mode="HTML")

@router.message(Command("give_good", "give-good"))
async def give_good_tg(message: Message):
    """/give-good <user> <good_code> [qty] — or a reply with
    /give-good <good_code> [qty]."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        if not parts:
            await message.reply(localized("give_good_usage", lang))
            return
        good_s = parts[0]
        qty_s = parts[1] if len(parts) > 1 else "1"
    else:
        if len(parts) < 2:
            await message.reply(localized("give_good_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        good_s = parts[1]
        qty_s = parts[2] if len(parts) > 2 else "1"
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    good = db.get_good(good_s.strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    if not qty_s.isdigit() or int(qty_s) <= 0:
        await message.reply(localized("bad_amount", lang))
        return
    qty = int(qty_s)
    owner = db.canonical_user("telegram", message.from_user.id)
    target = db.canonical_user("telegram", target_id)
    if target == owner:
        await message.reply(localized("give_self", lang))
        return
    have = db.get_inventory_qty(owner, good["code"])
    if have < qty:
        await message.reply(localized("give_no_goods", lang, have=have))
        return
    db.add_inventory(owner, good["code"], -qty)
    db.add_inventory(target, good["code"], qty)
    disp = await _quiz_target_label_tg(target_id)
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    await message.reply(localized("give_done", lang, qty=qty, emoji=emoji,
                                  name=good_display_name(good, lang),
                                  user=escape_html(disp)))

@router.message(Command("goods"))
async def goods_tg(message: Message):
    lang = get_chat_lang(_chat_key(message))
    lines = [f"<b>{escape_html(localized('goods_header', lang))}</b>"]
    for code, cat, emoji, _name in db.BASE_GOODS:
        good = db.get_good(code)
        if not good:
            continue
        lines.append(escape_html(localized(
            "goods_line_base", lang, emoji=emoji, name=good_display_name(good, lang),
            code=code, energy=economy.format_energy(good["energy_cost"]))))
    chat = db.get_chat(str(message.chat.id)) \
        if message.chat.type in GROUP_CHAT_TYPES else None
    if chat:
        for bank in db.get_banks_in_union(chat["union_code"]):
            for good in db.get_bank_goods(bank["code"]):
                if db.is_base_good(good["code"]):
                    continue
                emoji = f"{good['emoji']} " if good["emoji"] else ""
                cat_disp = category_label(good["category"], lang) if good["category"] else "—"
                lines.append(escape_html(localized(
                    "goods_line", lang, emoji=emoji, name=good["name"], code=good["code"],
                    category=cat_disp,
                    value=economy.format_money(good["base_value"], bank),
                    energy=economy.format_energy(good["energy_cost"]))))
    await message.reply("\n".join(lines)[:4000], parse_mode="HTML")

# ── Olympiad ────────────────────────────────────────────────────────────────
# The dialogs themselves live in olympiad.py and are shared with the Discord
# bot; here are the Telegram permission gates and the dialog object they drive.
# Telegram has no published command list to hide anything from, so the gating
# people see is /help (which omits the commands while no Olympiad runs) and the
# refusal below. The votes still travel to the contest's Discord chats.

_pending_oly_buttons = {}

def _new_oly_token(user_id, lang):
    token = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    _pending_oly_buttons[token] = {"future": fut, "user_id": user_id, "lang": lang,
                                   "created": time.time()}
    return token, fut

def _oly_keyboard(token, buttons, per_row=1):
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

@router.callback_query(lambda c: c.data and c.data.startswith("ob:"))
async def handle_olympiad_button(query: CallbackQuery):
    try:
        _, token, value = query.data.split(":", 2)
    except Exception:
        await query.answer()
        return
    pend = _pending_oly_buttons.get(token)
    if not pend or time.time() - pend["created"] > DIALOG_TIMEOUT:
        _pending_oly_buttons.pop(token, None)
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if not query.from_user or query.from_user.id != pend["user_id"]:
        await query.answer(localized("consent_not_yours", pend["lang"]), show_alert=True)
        return
    _pending_oly_buttons.pop(token, None)
    await query.answer()
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not pend["future"].done():
        pend["future"].set_result(value)

async def _await_oly_button(fut, timeout=DIALOG_TIMEOUT):
    try:
        return await asyncio.wait_for(fut, timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None

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
        self.message = message
        self.lang = lang
        self.chat_id = message.chat.id
        self.user_id = message.from_user.id
        self.author = _tg_user_label(message.from_user)

    async def send(self, text, reply_markup=None):
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
        lines = [header] + [f"{i + 1}. {render(it)}" for i, it in enumerate(items)]
        if extra:
            lines.append(f"{len(items) + 1}. {extra}")
        total = len(items) + (1 if extra else 0)

        def _validator(value):
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
        lines = [header, ""] + [f"{i + 1}. {render(it)}" for i, it in enumerate(items)]
        answer = await self.ask(
            "\n".join(lines),
            validator=lambda v: olympiad.parse_numbers(v, len(items), limit),
            error_text=olympiad.text("candidates_invalid", self.lang, max=limit))
        return answer[1] if answer else None

@router.message(Command("setolympiad"))
async def setolympiad_tg(message: Message):
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
    lang = get_chat_lang(_chat_key(message))
    if not await _olympiad_admin_gate_tg(message, lang):
        return
    await olympiad.run_setcontest(_OlyDialogTg(message, lang), lang)

@router.message(Command("editcontest"))
async def editcontest_tg(message: Message):
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

@router.message()
async def _dialog_catchall(message: Message):
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
