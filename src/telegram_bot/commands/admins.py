"""Delegating rights on Telegram, and the manual database backup:
`/setadmin`, `/remadmin`, `/localizer_add`, `/localizer_rem`, `/backup`.

Resolving *who* an admin command is about is the part that differs from
Discord: there is no user-id mention syntax that survives a copy-paste, so
`client.py: _resolve_tg_admin_target` accepts a reply to the person, a
text-mention entity, a numeric id or a public @username, in that order of
preference.

`/backup` sends an encrypted snapshot and nothing else; a deployment without
BACKUP_KEY gets an error rather than a plaintext database.

Not this module's zone: per-object leadership (parties, banks, enterprises),
granted by the objects' own commands.
"""
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile

import db
from utils import get_chat_lang, is_admin, localized

from telegram_bot.client import (
    GROUP_CHAT_TYPES, _chat_key, _resolve_tg_admin_target, bot, logger,
    router,
)

@router.message(Command("setadmin"))
async def setadmin_cmd(message: Message):
    """Delegate server-admin rights in this group (Bot Admins).

    The target is named by a reply, a mention, an id or a public @username — see
    client.py: _resolve_tg_admin_target — because Telegram has no mention syntax
    that survives being typed out."""
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
    """Revoke a delegated server-admin grant (Bot Admins). A native group
    administrator keeps their rights and cannot be demoted here."""
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
    """Grant Localizer status for the control panel (Bot Admins).

    The @username is stored beside the id because the panel logs people in by
    username."""
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
    """Revoke a delegated Localizer status (Bot Admins)."""
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

@router.message(Command("backup"))
async def backup_cmd(message: Message):
    """Send an encrypted database snapshot (Bot Admins).

    Always encrypted: without BACKUP_KEY the build fails and the command says so
    rather than sending a plaintext database."""
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
