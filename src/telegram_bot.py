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
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)

import db
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
    """Server Admins on Telegram are the group's native administrators."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False

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
            set_chat_lang(_chat_key(message), code)
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
            set_chat_lang(chat_key, code)
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
    ])

    bot_admins_lines = "\n".join([
        escape_html(localized_help("cmd_setup", lang)),
        escape_html(localized_help("cmd_add_unia_tg", lang)),
        escape_html(localized_help("cmd_allow_parties_tg", lang)),
        escape_html(localized_help("cmd_add_govt_tg", lang)),
        escape_html(localized_help("cmd_edit_party_admin_tg", lang)),
        escape_html(localized_help("cmd_loc_reply_tg", lang)),
        escape_html(localized_help("cmd_list_chats", lang)),
        escape_html(localized_help("cmd_force_leave", lang)),
        escape_html(localized_help("cmd_backup", lang)),
    ])

    text = (
        f"<b>{localized_help('title', lang)}</b>\n\n"
        f"<b>{localized_help('section_everyone', lang)}</b>\n{everyone_lines}\n\n"
        f"<b>{localized_help('section_admins', lang)}</b>\n{admins_lines}\n\n"
        f"<b>{localized_help('section_bot_admins', lang)}</b>\n{bot_admins_lines}"
    )
    await _reply_autodelete(text)

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
    _edit_admin_menu_text, _resume_state, parties_enabled,
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

@router.message()
async def _dialog_catchall(message: Message):
    if not message.from_user:
        return
    if (message.text or "").startswith("/"):
        return
    key = (message.chat.id, message.from_user.id)
    fut = _pending_inputs.get(key)
    if fut and not fut.done():
        fut.set_result(message)
