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
import olympiad
import quizzes
import utils
from config import SUPPORT_CHATS
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
from message_relay import clean_display_name
import fandom

logger = logging.getLogger("dem.discord")

UNION_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,12}$")
COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")
URL_RE = re.compile(r"^https?://\S+$")
USER_REF_RE = re.compile(r"^<@!?(\d+)>$|^(\d{5,25})$")

DIALOG_TIMEOUT = 30 * 60

def is_server_admin(interaction: discord.Interaction):
    """Server Admins on Discord are members with the native Administrator or
    Manage Server permission, plus users delegated with /setadmin."""
    if interaction.guild is None:
        return False
    if db.is_server_admin("discord", interaction.guild.id, interaction.user.id):
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None:
        return False
    return perms.administrator or perms.manage_guild

async def _require_verified(interaction: discord.Interaction, lang):
    """Gate the Fandom-activity commands. Returns True when the caller may
    proceed; otherwise replies with a prompt to /verify and returns False."""
    if is_verified("discord", interaction.user.id):
        return True
    await interaction.response.send_message(localized("verify_required", lang), ephemeral=True)
    return False

class DemBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        _apply_olympiad_commands()
        await self.tree.sync()
        self.loop.create_task(self.status_loop())
        self.loop.create_task(self.backup_loop())
        self.loop.create_task(self.founday_loop())
        try:
            for req in db.get_open_join_requests():
                self.add_view(JoinRequestView(req["id"], req["lang"]))
        except Exception as e:
            logger.warning("Could not restore join-request views: %s", e)

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)

    async def status_loop(self):
        """The presence is the fixed motto; re-applied hourly so it survives
        reconnects."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.change_presence(activity=discord.CustomActivity(name=STATUS_TEXT))
            except Exception as e:
                logger.warning("Status update error: %s", e)
            await asyncio.sleep(3600)

    async def backup_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(12 * 3600)
            await _send_db_backup_discord(self)
            await _send_db_backup_telegram()

    async def founday_loop(self):
        """Wiki anniversaries are due at a whole minute, so poll once a minute."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await _post_due_foundays()
            except Exception as e:
                logger.warning("Wiki founday loop error: %s", e)
            await asyncio.sleep(60)

bot = DemBot()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    tb = traceback.format_exc()
    logger.error("Command '%s': %s\n%s", interaction.command.name if interaction.command else "?", error, tb)
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    msg = localized("internal_error", lang, error=error)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        pass

async def _send_db_backup_discord(client):
    from config import BACKUP_CHATS
    from backup_crypto import build_encrypted_backup, encrypted_filename
    try:
        data = build_encrypted_backup("dem.db")
    except Exception as e:
        logger.warning("Periodic backup failed to build: %s", e)
        return
    fname = encrypted_filename("dem.db")
    for channel_id in BACKUP_CHATS.get("discord", set()):
        try:
            ch = client.get_channel(channel_id)
            if not ch:
                try:
                    ch = await client.fetch_channel(channel_id)
                except Exception:
                    logger.warning("Periodic backup: cannot fetch Discord channel %s", channel_id)
                    continue
            if ch:
                await ch.send(file=discord.File(io.BytesIO(data), filename=fname))
        except Exception as e:
            logger.warning("Periodic backup: failed to send to Discord channel %s: %s", channel_id, e)

async def _send_db_backup_telegram():
    from config import BACKUP_CHATS
    from telegram_bot import bot as tg_bot
    from backup_crypto import build_encrypted_backup, encrypted_filename
    try:
        data = build_encrypted_backup("dem.db")
    except Exception as e:
        logger.warning("Periodic backup failed to build: %s", e)
        return
    fname = encrypted_filename("dem.db")
    for chat_entry in BACKUP_CHATS.get("telegram", set()):
        try:
            chat_id_str, thread_str = str(chat_entry).split(":")
            from aiogram.types import BufferedInputFile
            doc = BufferedInputFile(data, filename=fname)
            await tg_bot.send_document(
                chat_id=int(chat_id_str),
                document=doc,
                message_thread_id=int(thread_str) or None,
            )
        except Exception as e:
            logger.warning("Periodic backup: failed to send to Telegram chat %s: %s", chat_entry, e)

@bot.event
async def on_guild_join(guild: discord.Guild):
    """The bot was added to a server: record the join and tell the service
    chats, since a Bot Admin now has seven days to bind it to a union with
    `/setup` (setup_deadline.py).

    The row is a record, not the clock — the sweep reads Discord's own
    `Guild.me.joined_at`, so a missed event costs nothing but this notice."""
    db.record_join("discord", guild.id)
    await send_service_event(
        "joined_chat",
        platform="Discord",
        chat=guild.name or str(guild.id),
        chat_id=guild.id,
    )

@bot.event
async def on_guild_remove(guild: discord.Guild):
    """The bot was kicked from (or left) a server: forget the server's union
    binding, and its setup-deadline row with it, so that a later
    re-invitation is a fresh seven days rather than a settlement inherited
    from the last time."""
    db.remove_chat(guild.id)
    db.forget_deadline("discord", guild.id)

@bot.event
async def on_message(message: discord.Message):
    """Message earning. Independent of the wait_for dialogs and slash commands
    (those are dispatched separately), so counting a message here never disturbs
    them. A message earns only when the channel is a /set-earn earning channel,
    the author is a verified human, and the anti-abuse gate (min length, per-user
    cooldown, hourly cap) lets it through — see economy.earn_from_message."""
    try:
        if message.author.bot or message.guild is None:
            return
        content = (message.content or "").strip()
        if not content or content.startswith("/"):
            return
        earn = db.get_earn_channel("discord", message.channel.id)
        if not earn:
            return
        economy.earn_from_message("discord", message.author.id, str(message.author),
                                  earn["bank_code"], len(content), earn["rate"])
    except Exception as e:
        logger.warning("on_message earning error: %s", e)

async def _refuse_not_setup(interaction: discord.Interaction, lang):
    await interaction.response.send_message(localized("chat_not_setup", lang), ephemeral=True)

def parties_enabled(union_code):
    return db.get_party_settings(union_code)[0]

async def _resolve_party_union(interaction: discord.Interaction, lang):
    """The union of this server, refusing when the server is not set up or the
    union has not turned parties on. Returns the union code, or None.

    Every party command goes through this — /edit-party-admin excepted, so a Bot
    Admin can still tidy up a union after parties were switched off."""
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return None
    chat = db.get_chat(str(interaction.guild_id))
    if not chat:
        await _refuse_not_setup(interaction, lang)
        return None
    if not parties_enabled(chat["union_code"]):
        await interaction.response.send_message(localized("party_feature_disabled", lang), ephemeral=True)
        return None
    return chat["union_code"]

def _chat_key(interaction: discord.Interaction):
    return f"{interaction.guild_id}:{interaction.channel_id}"

@bot.tree.command(name="setup", description="link this server to a union and set its language (bot admins)")
@app_commands.describe(
    union="Union code",
    code="Language code (ru, uk, pl, en, es, pt)",
)
async def setup_cmd(interaction: discord.Interaction, union: str, code: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return

    union = union.strip().upper()
    code = code.strip().lower()
    if not db.union_exists(union):
        await interaction.response.send_message(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes())),
            ephemeral=True
        )
        return
    if code not in SUPPORTED_LANGS:
        await interaction.response.send_message(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
            ephemeral=True
        )
        return

    db.setup_chat("discord", interaction.guild_id, union, interaction.user.id)
    set_chat_lang(str(interaction.guild_id), code)
    await interaction.response.send_message(
        localized("setup_success", code,
                  union=db.get_union_name(union, code), code=union,
                  lang_name=language_name(code), lang=code)
    )
    await send_service_event("setup_done", platform="Discord",
                             chat=interaction.guild.name, union=union,
                             user=str(interaction.user))

@bot.tree.command(name="setadmin", description="delegate bot server-admin rights to a user (bot admins)")
@app_commands.describe(user="User to make a server admin: ping or ID")
async def setadmin_cmd(interaction: discord.Interaction, user: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message(localized("group_only", lang), ephemeral=True)
        return

    m = USER_REF_RE.match(user.strip())
    if not m:
        await interaction.response.send_message(localized("setadmin_invalid_id", lang), ephemeral=True)
        return
    uid = int(m.group(1) or m.group(2))

    if db.is_server_admin("discord", interaction.guild.id, uid):
        await interaction.response.send_message(
            localized("setadmin_already", lang, user_id=uid), ephemeral=True)
        return

    username = None
    member = None
    try:
        member = interaction.guild.get_member(uid) or await bot.fetch_user(uid)
        username = getattr(member, "name", None)
    except Exception:
        pass
    db.add_server_admin("discord", interaction.guild.id, uid,
                        username=username, added_by=interaction.user.id)
    await interaction.response.send_message(localized("setadmin_success", lang, user_id=uid))
    try:
        if member:
            await member.send(localized("setadmin_dm", lang, server=interaction.guild.name))
    except Exception:
        pass

@bot.tree.command(name="remadmin", description="revoke bot server-admin rights from a user (bot admins)")
@app_commands.describe(user="User to demote: ping or ID")
async def remadmin_cmd(interaction: discord.Interaction, user: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message(localized("group_only", lang), ephemeral=True)
        return

    m = USER_REF_RE.match(user.strip())
    if not m:
        await interaction.response.send_message(localized("setadmin_invalid_id", lang), ephemeral=True)
        return
    uid = int(m.group(1) or m.group(2))

    if not db.is_server_admin("discord", interaction.guild.id, uid):
        await interaction.response.send_message(
            localized("remadmin_not_admin", lang, user_id=uid), ephemeral=True)
        return

    db.remove_server_admin("discord", interaction.guild.id, uid)
    await interaction.response.send_message(localized("remadmin_success", lang, user_id=uid))

async def _resolve_user_ref(guild, identifier):
    """Resolve a ping, raw ID or (best-effort) username to a user id."""
    identifier = identifier.strip()
    m = USER_REF_RE.match(identifier)
    if m:
        return int(m.group(1) or m.group(2))
    if guild is not None:
        name = identifier.lstrip("@").casefold()
        for member in guild.members:
            if member.name.casefold() == name \
                    or (member.display_name or "").casefold() == name:
                return member.id
        try:
            async for member in guild.fetch_members(limit=1000):
                if member.name.casefold() == name \
                        or (member.display_name or "").casefold() == name:
                    return member.id
        except Exception:
            pass
    return None

@bot.tree.command(name="localizer-add", description="grant Localizer status: lets the user edit this bot's localization in the control panel (bot admins)")
@app_commands.describe(user="User to make a localizer: ping, ID or username")
async def localizer_add_cmd(interaction: discord.Interaction, user: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    uid = await _resolve_user_ref(interaction.guild, user)
    if uid is None:
        await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
        return

    if db.is_localizer("discord", uid):
        await interaction.response.send_message(
            localized("localizer_add_already", lang, user_id=uid), ephemeral=True)
        return

    username = None
    member = None
    try:
        member = (interaction.guild.get_member(uid) if interaction.guild else None) \
            or await bot.fetch_user(uid)
        username = getattr(member, "name", None)
    except Exception:
        pass
    db.add_localizer("discord", uid, username=username, added_by=interaction.user.id)
    await interaction.response.send_message(localized("localizer_add_done", lang, user_id=uid))
    try:
        if member:
            await member.send(localized("localizer_add_dm", lang))
    except Exception:
        pass

@bot.tree.command(name="localizer-rem", description="revoke a delegated Localizer status (bot admins)")
@app_commands.describe(user="User to demote: ping, ID or username")
async def localizer_rem_cmd(interaction: discord.Interaction, user: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    uid = await _resolve_user_ref(interaction.guild, user)
    if uid is None:
        await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
        return

    if not db.remove_localizer("discord", uid):
        await interaction.response.send_message(
            localized("localizer_rem_not", lang, user_id=uid), ephemeral=True)
        return

    await interaction.response.send_message(localized("localizer_rem_done", lang, user_id=uid))

@bot.tree.command(name="add-unia", description="create a new union; give its name in at least one language (bot admins)")
@app_commands.describe(
    code="New union code (2-12 latin letters/digits)",
    name_en="Union name in English",
    name_ru="Union name in Russian",
    name_uk="Union name in Ukrainian",
    name_pl="Union name in Polish",
    name_es="Union name in Spanish",
    name_pt="Union name in Portuguese",
)
async def add_unia_cmd(
    interaction: discord.Interaction,
    code: str,
    name_en: str = None,
    name_ru: str = None,
    name_uk: str = None,
    name_pl: str = None,
    name_es: str = None,
    name_pt: str = None,
):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    code = code.strip().upper()
    if not UNION_CODE_RE.match(code):
        await interaction.response.send_message(localized("add_unia_invalid_code", lang), ephemeral=True)
        return

    names = {}
    for L, name in (("en", name_en), ("ru", name_ru), ("uk", name_uk),
                    ("pl", name_pl), ("es", name_es), ("pt", name_pt)):
        if name and name.strip():
            names[L] = name.strip()
    if not names:
        await interaction.response.send_message(localized("add_unia_need_name", lang), ephemeral=True)
        return

    if not db.add_union(code, names):
        await interaction.response.send_message(localized("add_unia_exists", lang, code=code), ephemeral=True)
        return

    listed = "\n".join(f"{language_name(L)}: {names[L]}" for L in LANG_ORDER if L in names)
    await interaction.response.send_message(localized("add_unia_created", lang, code=code, names=listed))
    await send_service_event("union_created", code=code, user=str(interaction.user))

@bot.tree.command(name="lang", description="set the default bot language for the whole server (server admins)")
@app_commands.describe(code="Language code (ru, uk, pl, en, es, pt)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def lang_command(interaction: discord.Interaction, code: str):
    lang = get_chat_lang(_chat_key(interaction))
    code = code.strip().lower()

    if interaction.guild is None:
        try:
            set_chat_lang(_chat_key(interaction), code, is_dm=True)
        except Exception:
            await interaction.response.send_message(
                localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(localized("lang_set", code, code=code), ephemeral=True)
        return

    if not (is_admin("discord", interaction.user.id) or is_server_admin(interaction)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    if not db.is_setup(interaction.guild_id):
        await _refuse_not_setup(interaction, lang)
        return

    try:
        set_chat_lang(str(interaction.guild_id), code)
    except Exception:
        await interaction.response.send_message(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
            ephemeral=True
        )
        return

    await interaction.response.send_message(localized("lang_set_server", code, code=code), ephemeral=True)

@bot.tree.command(name="locallang", description="set bot language for this channel/thread (server admins)")
@app_commands.describe(code="Language code (ru, uk, pl, en, es, pt)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def locallang_command(interaction: discord.Interaction, code: str):
    chat_key = _chat_key(interaction)
    lang = get_chat_lang(chat_key)
    code = code.strip().lower()

    if interaction.guild is None:
        try:
            set_chat_lang(chat_key, code, is_dm=True)
        except Exception:
            await interaction.response.send_message(
                localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(localized("lang_set", code, code=code), ephemeral=True)
        return

    if not (is_admin("discord", interaction.user.id) or is_server_admin(interaction)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    if not db.is_setup(interaction.guild_id):
        await _refuse_not_setup(interaction, lang)
        return

    try:
        set_chat_lang(chat_key, code)
    except Exception:
        await interaction.response.send_message(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
            ephemeral=True
        )
        return

    await interaction.response.send_message(localized("lang_set", code, code=code), ephemeral=True)

@bot.tree.command(name="list_chats", description="list all chats the bot is in (bot admins)")
async def list_chats(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    lines = [localized("list_chats_discord_header", lang)]
    for g in bot.guilds:
        row = db.get_chat(str(g.id))
        union = f" [{row['union_code']}]" if row else ""
        lines.append(f"- {g.name} — id: {g.id}{union}")

    tg_ids = db.get_telegram_group_ids()
    if tg_ids:
        lines.append("\n" + localized("list_chats_telegram_header", lang))
        try:
            from telegram_bot import bot as tg_bot
            for pid in tg_ids:
                row = db.get_chat(pid)
                union = f" [{row['union_code']}]" if row else ""
                try:
                    chat = await tg_bot.get_chat(int(pid))
                    title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(pid)
                except Exception:
                    title = str(pid)
                lines.append(f"- {title} — id: {pid}{union}")
        except Exception:
            for pid in tg_ids:
                lines.append(f"- id: {pid}")
    else:
        lines.append("\n" + localized("list_chats_no_telegram", lang))

    msg = "\n".join(lines)
    if len(msg) > 1900:
        bio = io.BytesIO(msg.encode("utf-8"))
        bio.seek(0)
        await interaction.response.send_message(localized("list_chats_too_long", lang), ephemeral=True)
        await interaction.followup.send(file=discord.File(bio, filename="chat_list.txt"))
    else:
        await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="force_leave", description="make the bot leave a chat (bot admins)")
@app_commands.describe(platform="discord or telegram", target_id="Server / group ID")
async def force_leave(interaction: discord.Interaction, platform: str, target_id: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    platform = platform.strip().lower()
    target_id = target_id.strip()

    if platform == "discord":
        try:
            gid = int(target_id)
        except ValueError:
            await interaction.response.send_message(localized("force_leave_invalid_id", lang), ephemeral=True)
            return

        guild = bot.get_guild(gid)
        if not guild:
            await interaction.response.send_message(localized("force_leave_not_member", lang), ephemeral=True)
            return

        try:
            await guild.leave()
        except Exception as e:
            await interaction.response.send_message(localized("force_leave_failed", lang, error=e), ephemeral=True)
            return

        db.remove_chat(gid)
        await interaction.response.send_message(localized("force_leave_success_discord", lang, guild_id=gid), ephemeral=True)
        return

    if platform == "telegram":
        try:
            tid = int(target_id)
        except ValueError:
            await interaction.response.send_message(localized("force_leave_invalid_id", lang), ephemeral=True)
            return

        try:
            from telegram_bot import bot as tg_bot
            await tg_bot.leave_chat(tid)
        except Exception as e:
            await interaction.response.send_message(localized("force_leave_failed", lang, error=e), ephemeral=True)
        db.remove_chat(tid)
        await interaction.response.send_message(localized("force_leave_success_telegram", lang, chat_id=tid), ephemeral=True)
        return

    await interaction.response.send_message(localized("force_leave_unsupported_platform", lang), ephemeral=True)

async def post_loc_suggestion(*, lang, key, suggestion, code, ui_lang, username, user_id, avatar_url=None):
    """Post a localization suggestion to the Discord and Telegram support chat(s)."""
    body = localized("loc_suggest_support_body", ui_lang,
                     suggestion=suggestion, name=language_name(lang), lang=lang, key=key)
    footer = f"{username} │ ID: {user_id} │ {code}"

    for cid in SUPPORT_CHATS.get("discord", set()):
        channel = bot.get_channel(int(cid))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(cid))
            except Exception:
                channel = None
        if channel is None:
            continue
        embed = discord.Embed(description=body, color=discord.Color(DEFAULT_EMBED_COLOR))
        embed.set_footer(text=footer, icon_url=avatar_url)
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None
    if tg_bot is not None:
        for chat_key in SUPPORT_CHATS.get("telegram", set()):
            try:
                tg_chat_id, thread = str(chat_key).split(":")
                await tg_bot.send_message(
                    int(tg_chat_id), f"{body}\n\n{footer}",
                    message_thread_id=int(thread) or None
                )
            except Exception:
                pass

async def post_loc_reply(*, admin, code, ui_lang, title, body):
    """Publish an admin's /loc-reply to the support chat(s)."""
    prefix = localized("loc_reply_support_prefix", ui_lang, admin=admin, code=code)
    text = f"{prefix}\n\n{body}"

    for cid in SUPPORT_CHATS.get("discord", set()):
        channel = bot.get_channel(int(cid))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(cid))
            except Exception:
                channel = None
        if channel is None:
            continue
        try:
            await channel.send(embed=discord.Embed(title=title, description=text,
                                                   color=discord.Color(DEFAULT_EMBED_COLOR)))
        except Exception:
            pass

    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None
    if tg_bot is not None:
        for chat_key in SUPPORT_CHATS.get("telegram", set()):
            try:
                tg_chat_id, thread = str(chat_key).split(":")
                await tg_bot.send_message(
                    int(tg_chat_id), text, message_thread_id=int(thread) or None
                )
            except Exception:
                pass

@bot.tree.command(name="locale", description="localization status, or a language's file")
@app_commands.describe(lang="Language code (optional). With a code, sends that language's localization file.")
async def locale_cmd(interaction: discord.Interaction, lang: str = None):
    ui_lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is not None and not db.is_setup(interaction.guild_id) \
            and not is_admin("discord", interaction.user.id):
        await _refuse_not_setup(interaction, ui_lang)
        return

    if not lang or not lang.strip():
        lines = [localized("loc_list_header", ui_lang)]
        for code in available_locales():
            st = locale_stats(code)
            lines.append(f"{language_name(code)} (`{code}`): {locale_bar(code)} {st['percent']}%")
        lines.append("")
        lines.append(localized("loc_list_footer", ui_lang))
        await interaction.response.send_message("\n".join(lines))
        return

    code = lang.strip().lower()
    if code not in available_locales():
        await interaction.response.send_message(
            localized("loc_unknown_lang", ui_lang, lang=code, supported=", ".join(available_locales())),
            ephemeral=True
        )
        return

    if not rate_limit_ok(("locale-file", "discord", interaction.guild_id or interaction.user.id),
                         limit=1, window_seconds=600):
        await interaction.response.send_message(localized("loc_cooldown", ui_lang), ephemeral=True)
        return

    path = os.path.join(os.path.dirname(utils.__file__), "i18n", f"{code}.json")
    st = locale_stats(code)
    caption = localized("loc_file_caption", ui_lang, name=language_name(code), code=code, percent=st["percent"])
    try:
        await interaction.response.send_message(caption, file=discord.File(path, filename=f"{code}.json"))
    except Exception:
        await interaction.response.send_message(caption, ephemeral=True)

@bot.tree.command(name="loc-compare", description="compare a reply across languages")
@app_commands.describe(key="Reply code (as shown in the localization file)")
async def loc_compare_cmd(interaction: discord.Interaction, key: str):
    ui_lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is not None and not db.is_setup(interaction.guild_id) \
            and not is_admin("discord", interaction.user.id):
        await _refuse_not_setup(interaction, ui_lang)
        return

    key = key.strip()
    data = compare_reply(key)
    if data is None:
        await interaction.response.send_message(localized("loc_compare_not_found", ui_lang, key=key), ephemeral=True)
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
    if len(msg) > 1990:
        msg = msg[:1990]
    await interaction.response.send_message(msg)

@bot.tree.command(name="loc-suggest", description="suggest a localization")
@app_commands.describe(language="Language code", code="Reply code", text="Suggested text")
async def loc_suggest_cmd(interaction: discord.Interaction, language: str, code: str, text: str):
    ui_lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is not None and not db.is_setup(interaction.guild_id) \
            and not is_admin("discord", interaction.user.id):
        await _refuse_not_setup(interaction, ui_lang)
        return

    language = language.strip().lower()
    if language not in SUPPORTED_LANGS:
        await interaction.response.send_message(
            localized("loc_unknown_lang", ui_lang, lang=language, supported=", ".join(available_locales())),
            ephemeral=True
        )
        return
    if not SUPPORT_CHATS.get("discord") and not SUPPORT_CHATS.get("telegram"):
        await interaction.response.send_message(localized("loc_suggest_no_support", ui_lang), ephemeral=True)
        return

    msg_code = secrets.token_hex(4)
    db.add_loc_suggestion(msg_code, "discord", interaction.user.id, str(interaction.user),
                          language, code.strip(), text, ui_lang)
    try:
        avatar_url = interaction.user.display_avatar.url
    except Exception:
        avatar_url = None
    await post_loc_suggestion(lang=language, key=code.strip(), suggestion=text, code=msg_code,
                              ui_lang=ui_lang, username=str(interaction.user),
                              user_id=interaction.user.id, avatar_url=avatar_url)
    await interaction.response.send_message(localized("loc_suggest_confirm", ui_lang, code=msg_code), ephemeral=True)

@bot.tree.command(name="loc-reply", description="reply to a localization suggestion (bot admins)")
@app_commands.describe(code="Message code from the suggestion", text="Reply text")
async def loc_reply_cmd(interaction: discord.Interaction, code: str, text: str):
    ui_lang_cmd = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", ui_lang_cmd), ephemeral=True)
        return

    row = db.get_loc_suggestion(code.strip())
    if not row:
        await interaction.response.send_message(localized("loc_reply_not_found", ui_lang_cmd, code=code), ephemeral=True)
        return

    ui_lang = row["ui_lang"] or DEFAULT_LANG
    title = localized("loc_reply_dm_title", ui_lang)
    body = localized("loc_reply_dm_body", ui_lang,
                     suggestion=row["suggestion"], reply=text,
                     name=language_name(row["lang"]), lang=row["lang"], key=row["rkey"])

    ok = False
    if row["platform"] == "discord":
        try:
            user = await bot.fetch_user(int(row["user_id"]))
            await user.send(embed=discord.Embed(title=title, description=body,
                                                color=discord.Color(DEFAULT_EMBED_COLOR)))
            ok = True
        except Exception:
            ok = False
    elif row["platform"] == "telegram":
        try:
            from telegram_bot import bot as tg_bot
            await tg_bot.send_message(int(row["user_id"]), f"{title}\n\n{body}")
            ok = True
        except Exception:
            ok = False

    await post_loc_reply(admin=str(interaction.user), code=code.strip(),
                         ui_lang=ui_lang, title=title, body=body)

    if ok:
        db.delete_loc_suggestion(code.strip())
        await interaction.response.send_message(localized("loc_reply_sent", ui_lang_cmd), ephemeral=True)
    else:
        await interaction.response.send_message(localized("loc_reply_failed", ui_lang_cmd), ephemeral=True)

HELP_SECTIONS = [
    ("section_everyone", [
        "cmd_verify", "cmd_add_telegram", "cmd_party", "cmd_govt", "cmd_add_party",
        "cmd_edit_party", "cmd_edit_rules", "cmd_party_join", "cmd_party_leave",
        "cmd_party_kick", "cmd_quizzes", "cmd_quizzes_stop", "cmd_quizzes_clear",
        "cmd_quizzes_compare", "cmd_quizzes_previous", "cmd_privacy", "cmd_locale",
        "cmd_loc_compare", "cmd_loc_suggest", "cmd_help",
    ]),
    ("section_admins", [
        "cmd_lang", "cmd_locallang", "cmd_find_with", "cmd_find_without",
        "cmd_wiki_founday", "cmd_wiki_foundays", "cmd_wiki_founday_remove",
        "cmd_setlogs", "cmd_settasks",
    ]),
    ("section_bot_admins", [
        "cmd_setup", "cmd_setadmin", "cmd_remadmin",
        "cmd_localizer_add", "cmd_localizer_rem",
        "cmd_add_unia", "cmd_allow_parties", "cmd_add_govt",
        "cmd_edit_party_admin", "cmd_loc_reply", "cmd_list_chats", "cmd_force_leave",
        "cmd_backup",
    ]),
    ("section_economy", [
        "cmd_create_bank", "cmd_bank", "cmd_edit_bank", "cmd_bank_add_leader",
        "cmd_bank_transfer",
        "cmd_open_account", "cmd_balance", "cmd_pay", "cmd_set_earn",
        "cmd_create_good", "cmd_goods", "cmd_craft", "cmd_inventory", "cmd_sell",
        "cmd_give_good", "cmd_autocraft", "cmd_autosend", "cmd_set_profession",
        "cmd_fine", "cmd_treaty", "cmd_set_rate", "cmd_convert", "cmd_rates",
        "cmd_set_wage", "cmd_party_dues",
    ]),
    ("section_enterprises", [
        "cmd_add_enterprise", "cmd_enterprise", "cmd_edit_enterprise",
        "cmd_ent_join", "cmd_ent_leave", "cmd_ent_kick",
        "cmd_ent_position", "cmd_ent_assign", "cmd_ent_salary",
        "cmd_ent_sell", "cmd_export", "cmd_auto_export", "cmd_transit",
    ]),
]

EMBED_FIELD_LIMIT = 1024
_FIELD_CONTINUATION = "​"

def _chunk_lines(lines, limit=EMBED_FIELD_LIMIT):
    """Group lines into blocks that each fit in one embed field."""
    blocks, current = [], []
    for line in lines:
        line = line if len(line) <= limit else line[:limit - 1] + "…"
        candidate = ("\n".join(current + [line]))
        if current and len(candidate) > limit:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks

def _help_pages(lang):
    """One page per section. A section too long for a single embed field is
    spread over several fields on the same page. The Olympiad page lists only
    /setolympiad while no Olympiad is running — the rest of its commands are not
    registered then, so listing them would point at nothing."""
    pages = []
    for title_key, keys in HELP_SECTIONS:
        blocks = _chunk_lines([localized_help(k, lang) for k in keys])
        pages.append((localized_help(title_key, lang), blocks))
    lines = [olympiad.text(k, lang) for k in olympiad_help_keys()]
    pages.append((olympiad.text("section_title", lang), _chunk_lines(lines)))
    return pages

def _help_embed(lang, pages, index):
    title, blocks = pages[index]
    embed = discord.Embed(title=localized_help("title", lang),
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    for i, block in enumerate(blocks):
        embed.add_field(name=title if i == 0 else _FIELD_CONTINUATION,
                        value=block, inline=False)
    embed.set_footer(text=localized("help_page", lang, page=index + 1, total=len(pages)))
    return embed

class _HelpView(ui.View):
    """Prev/next buttons over the help pages; only the caller may press them."""

    def __init__(self, lang, pages, user_id):
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.lang = lang
        self.pages = pages
        self.user_id = user_id
        self.index = 0
        self.prev = ui.Button(label=localized("help_prev", lang), style=ButtonStyle.secondary)
        self.next = ui.Button(label=localized("help_next", lang), style=ButtonStyle.secondary)
        self.prev.callback = self._make_cb(-1)
        self.next.callback = self._make_cb(1)
        self.add_item(self.prev)
        self.add_item(self.next)
        self._sync()

    def _sync(self):
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1

    def _make_cb(self, step):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    localized("consent_not_yours", self.lang), ephemeral=True)
                return
            self.index = max(0, min(len(self.pages) - 1, self.index + step))
            self._sync()
            await interaction.response.edit_message(
                embed=_help_embed(self.lang, self.pages, self.index), view=self)
        return callback

@bot.tree.command(name="help", description="show this command list")
async def help_command(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    pages = _help_pages(lang)
    view = _HelpView(lang, pages, interaction.user.id) if len(pages) > 1 else None
    await interaction.response.send_message(
        embed=_help_embed(lang, pages, 0), view=view, ephemeral=True)

@bot.tree.command(name="backup", description="get a database backup (bot admins)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def backup_discord_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        from backup_crypto import build_encrypted_backup, encrypted_filename
        data = build_encrypted_backup("dem.db")
        await interaction.followup.send(
            file=discord.File(io.BytesIO(data), filename=encrypted_filename("dem.db")),
            ephemeral=True,
        )
    except Exception as e:
        logger.warning("Failed to build/send database backup: %s", e)
        try:
            await interaction.followup.send(localized("backup_failed", lang, error=str(e)), ephemeral=True)
        except Exception:
            pass

async def _wait_message(channel_id, user_id, timeout=DIALOG_TIMEOUT):
    """Wait up to 30 minutes for the next message of `user_id` in `channel_id`."""
    def check(m):
        return m.author.id == user_id and m.channel.id == channel_id
    try:
        return await bot.wait_for("message", check=check, timeout=timeout)
    except asyncio.TimeoutError:
        return None

async def _say(interaction: discord.Interaction, content=None, **kwargs):
    """First reply goes through the interaction, later ones to the channel.

    `ephemeral` only exists on the interaction response, so it is dropped once
    we fall back to a normal channel message (e.g. after a numbered-choice
    dialog has already consumed the interaction response)."""
    if not interaction.response.is_done():
        await interaction.response.send_message(content, **kwargs)
        try:
            return await interaction.original_response()
        except Exception:
            return None
    kwargs.pop("ephemeral", None)
    return await interaction.channel.send(content, **kwargs)

async def _dialog_text(interaction, lang, prompt, *, validator=None, error_key=None,
                       attempts=5, as_embed=False):
    """Ask `prompt` and wait for the caller's text answer, re-asking on invalid
    input. Returns the validated value (or the message when no validator) or
    None on timeout/attempts exhausted. With as_embed=True every dialog message
    is wrapped in an embed (the economy commands use this)."""
    def _wrap(text):
        return {"embed": _econ_embed(text)} if as_embed else {"content": text}
    await _say(interaction, **_wrap(prompt))
    for _ in range(attempts):
        msg = await _wait_message(interaction.channel_id, interaction.user.id)
        if msg is None:
            await interaction.channel.send(**_wrap(localized("dialog_timeout", lang)))
            return None
        value = (msg.content or "").strip()
        if validator is None:
            return msg
        ok = validator(value)
        if ok is not None:
            return ok
        await interaction.channel.send(**_wrap(localized(error_key, lang)))
    await interaction.channel.send(**_wrap(localized("dialog_timeout", lang)))
    return None

_LOGO_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

async def _extract_logo(msg):
    """Return (bytes, mime) if the message carries an image attachment."""
    for att in msg.attachments:
        ctype = (att.content_type or "").split(";")[0].strip().lower()
        if ctype.startswith("image/") and att.size <= 8 * 1024 * 1024:
            try:
                return await att.read(), ctype
            except Exception:
                return None
    return None

async def _dialog_logo(interaction, lang, prompt, attempts=5):
    await _say(interaction, prompt)
    for _ in range(attempts):
        msg = await _wait_message(interaction.channel_id, interaction.user.id)
        if msg is None:
            await interaction.channel.send(localized("dialog_timeout", lang))
            return None
        logo = await _extract_logo(msg)
        if logo:
            return logo
        await interaction.channel.send(localized("add_party_invalid_logo", lang))
    await interaction.channel.send(localized("dialog_timeout", lang))
    return None

def _parse_user_ref(text):
    m = USER_REF_RE.match((text or "").strip())
    if not m:
        return None
    return int(m.group(1) or m.group(2))

def _party_code_validator(value):
    code = value.strip().upper()
    if PARTY_CODE_RE.match(code) and not db.party_code_taken(code):
        return code
    return None

async def _numbered_choice(interaction, lang, header, items, render_line):
    """Send `header` + a numbered list and wait 30 minutes for the caller to
    reply with a number. Returns the chosen item or None."""
    lines = [header] + [f"{i + 1}. {render_line(it)}" for i, it in enumerate(items)]
    await _say(interaction, "\n".join(lines))
    msg = await _wait_message(interaction.channel_id, interaction.user.id)
    if msg is None:
        return None
    value = (msg.content or "").strip().rstrip(".")
    if value.isdigit() and 1 <= int(value) <= len(items):
        return items[int(value) - 1]
    await interaction.channel.send(localized("choice_invalid", lang))
    return None

def _party_color(party):
    try:
        return discord.Color(int(party["color"].lstrip("#"), 16))
    except Exception:
        return discord.Color(DEFAULT_EMBED_COLOR)

def _party_line(party, lang):
    mark = " ⏸️" if party["suspended"] else ""
    return f"{party['name']} [{party['code']}]{mark}"

def _party_logo_file(party):
    """(discord.File, attachment url) for the party logo, or (None, None)."""
    if not party["logo"]:
        return None, None
    ext = _LOGO_EXT.get(party["logo_mime"], "png")
    fname = f"logo.{ext}"
    return discord.File(io.BytesIO(party["logo"]), filename=fname), f"attachment://{fname}"

def _party_balance_lines(party_code):
    """The party's bank balances, one line per account (only accounts that
    exist; a party gets one on its first incoming transfer or dues run)."""
    lines = []
    for acc in db.get_owner_accounts(*db.party_owner(party_code)):
        bank = db.get_bank(acc["bank_code"])
        if bank:
            lines.append(economy.format_money(acc["balance"], bank))
    return lines

def _leaders_differ_from_founder(party, leaders):
    return not (
        len(leaders) == 1
        and leaders[0]["platform"] == party["founder_platform"]
        and leaders[0]["user_id"] == party["founder_id"]
    )

def _build_party_embed(party, lang):
    """Party info embed (logo as the top-right thumbnail) + file + link button view."""
    embed = discord.Embed(
        title=f"{party['name']} [{party['code']}]",
        color=_party_color(party),
    )
    if party["suspended"]:
        embed.description = localized("party_suspended_mark", lang)

    embed.add_field(
        name=localized("party_field_founder", lang),
        value=format_stored_user("discord", party["founder_platform"],
                                 party["founder_id"], party["founder_name"]),
        inline=False,
    )
    leaders = db.get_party_leaders(party["code"])
    if _leaders_differ_from_founder(party, leaders):
        embed.add_field(
            name=localized("party_field_leader", lang),
            value=", ".join(
                format_stored_user("discord", l["platform"], l["user_id"], l["display_name"])
                for l in leaders
            ) or "—",
            inline=False,
        )
    if party["ideologies"]:
        embed.add_field(name=localized("party_field_ideologies", lang),
                        value=party["ideologies"][:1024], inline=False)
    allies = db.get_party_allies(party["code"])
    if allies:
        embed.add_field(
            name=localized("party_field_allies", lang),
            value=", ".join(f"{a['name']} [{a['code']}]" for a in allies)[:1024],
            inline=False,
        )
    embed.add_field(name=localized("party_field_members", lang),
                    value=str(len(db.get_party_user_set(party["code"]))), inline=True)
    seats = db.party_govt_seats(party["code"])
    if seats:
        embed.add_field(name=localized("party_field_seats", lang),
                        value=str(seats), inline=True)
    balances = _party_balance_lines(party["code"])
    if balances:
        embed.add_field(name=localized("party_field_balance", lang),
                        value="\n".join(balances)[:1024], inline=False)
    if party["description"]:
        embed.add_field(name=localized("party_field_description", lang),
                        value=party["description"][:1024], inline=False)

    file, thumb = _party_logo_file(party)
    if thumb:
        embed.set_thumbnail(url=thumb)

    view = None
    if party["article_url"]:
        view = ui.View(timeout=None)
        view.add_item(ui.Button(label=localized("party_more_button", lang),
                                style=ButtonStyle.link, url=party["article_url"]))
    return embed, file, view

async def _send_party_info(interaction, party, lang):
    embed, file, view = _build_party_embed(party, lang)
    kwargs = {"embed": embed}
    if file:
        kwargs["file"] = file
    if view:
        kwargs["view"] = view
    await _say(interaction, **kwargs)

class _ConsentView(ui.View):
    """«Принимаю» / «Не принимаю» buttons that only the target user may press."""

    def __init__(self, lang, target_id, on_accept, on_decline):
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.lang = lang
        self.target_id = target_id
        self._on_accept = on_accept
        self._on_decline = on_decline

        accept = ui.Button(label=localized("consent_accept", lang), style=ButtonStyle.success)
        decline = ui.Button(label=localized("consent_decline", lang), style=ButtonStyle.danger)
        accept.callback = self._make_callback(True)
        decline.callback = self._make_callback(False)
        self.add_item(accept)
        self.add_item(decline)

    def _make_callback(self, accepted):
        async def callback(interaction2: discord.Interaction):
            if interaction2.user.id != self.target_id:
                await interaction2.response.send_message(
                    localized("consent_not_yours", self.lang), ephemeral=True)
                return
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await interaction2.response.edit_message(view=self)
            except Exception:
                pass
            if accepted:
                await self._on_accept(interaction2)
            else:
                await self._on_decline(interaction2)
        return callback

def _message_corpus(msg):
    """All text of a message, embeds included, lowercased."""
    parts = [msg.content or ""]
    for e in msg.embeds:
        parts.append(e.title or "")
        parts.append(e.description or "")
        if e.footer and e.footer.text:
            parts.append(e.footer.text)
        if e.author and e.author.name:
            parts.append(e.author.name)
        for f in e.fields:
            parts.append(f.name or "")
            parts.append(f.value or "")
    return "\n".join(p for p in parts if p).lower()

async def _find_impl(interaction: discord.Interaction, channel_id: str, period: str,
                     keywords: str, want_match: bool):
    lang = get_chat_lang(_chat_key(interaction))
    if not (is_admin("discord", interaction.user.id) or is_server_admin(interaction)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    if not await _require_verified(interaction, lang):
        return

    raw = channel_id.strip()
    m = re.match(r"^<#(\d+)>$", raw)
    if m:
        raw = m.group(1)
    if not raw.isdigit():
        await interaction.response.send_message(localized("find_invalid_channel", lang), ephemeral=True)
        return
    ch = bot.get_channel(int(raw))
    if not ch:
        try:
            ch = await bot.fetch_channel(int(raw))
        except Exception:
            ch = None
    if ch is None or not hasattr(ch, "history"):
        await interaction.response.send_message(localized("find_channel_not_found", lang), ephemeral=True)
        return
    same_guild = getattr(ch, "guild", None) and interaction.guild_id == ch.guild.id
    if not same_guild and not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("find_channel_other_guild", lang), ephemeral=True)
        return

    try:
        start, end = parse_find_period(period)
    except ValueError:
        await interaction.response.send_message(localized("find_invalid_date", lang), ephemeral=True)
        return

    words = parse_keywords(keywords)
    if not words:
        await interaction.response.send_message(localized("find_no_keywords", lang), ephemeral=True)
        return

    await interaction.response.send_message(
        localized("find_started", lang, channel=ch.mention,
                  start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
                  count=len(words)),
        ephemeral=True,
    )

    checked = found = 0
    mentions = discord.AllowedMentions(users=True, everyone=False, roles=False,
                                       replied_user=False)
    try:
        async for msg in ch.history(limit=None, after=start, before=end, oldest_first=True):
            if bot.user and msg.author.id == bot.user.id:
                continue
            checked += 1
            has_keyword = any(w in _message_corpus(msg) for w in words)
            if has_keyword == want_match:
                found += 1
                try:
                    await msg.reply(f"<@{msg.author.id}>", allowed_mentions=mentions)
                except Exception:
                    pass
                await asyncio.sleep(1.0)
    except Exception as e:
        try:
            await interaction.followup.send(localized("find_history_error", lang, error=e), ephemeral=True)
        except Exception:
            pass
        return

    summary = localized("find_done", lang, checked=checked, found=found)
    try:
        await interaction.followup.send(summary, ephemeral=True)
    except Exception:
        try:
            await interaction.channel.send(f"{interaction.user.mention} {summary}")
        except Exception:
            pass

@bot.tree.command(name="find-with", description="reply-mention authors of messages containing the keywords (server admins)")
@app_commands.describe(
    channel_id="Channel ID to scan",
    period="MM-DD-YYYY, or MM-DD-YYYY + 7D for a range of days",
    keywords="Keywords separated by semicolons",
)
async def find_with_cmd(interaction: discord.Interaction, channel_id: str, period: str, keywords: str):
    await _find_impl(interaction, channel_id, period, keywords, want_match=True)

@bot.tree.command(name="find-without", description="reply-mention authors of messages NOT containing the keywords (server admins)")
@app_commands.describe(
    channel_id="Channel ID to scan",
    period="MM-DD-YYYY, or MM-DD-YYYY + 7D for a range of days",
    keywords="Keywords separated by semicolons",
)
async def find_without_cmd(interaction: discord.Interaction, channel_id: str, period: str, keywords: str):
    await _find_impl(interaction, channel_id, period, keywords, want_match=False)

@bot.tree.command(name="allow-parties", description="enable or disable parties in a union (bot admins)")
@app_commands.describe(
    union="Union code",
    action="enable | disable",
    roles="Comma-separated role IDs whose holders may use /add-party (required for enable)",
)
async def allow_parties_cmd(interaction: discord.Interaction, union: str, action: str,
                            roles: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    union = union.strip().upper()
    if not db.union_exists(union):
        await interaction.response.send_message(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes())),
            ephemeral=True,
        )
        return

    action = action.strip().lower()
    if action == "disable":
        db.set_parties_enabled(union, False)
        await interaction.response.send_message(localized("allow_parties_disabled", lang, code=union))
        await send_service_event("parties_disabled", code=union, user=str(interaction.user))
        return
    if action != "enable":
        await interaction.response.send_message(localized("allow_parties_usage", lang), ephemeral=True)
        return

    role_ids = [p.strip() for p in (roles or "").replace(" ", ",").split(",") if p.strip()]
    if not role_ids:
        await interaction.response.send_message(localized("allow_parties_need_roles", lang), ephemeral=True)
        return
    if not all(p.isdigit() for p in role_ids):
        await interaction.response.send_message(localized("allow_parties_invalid_roles", lang), ephemeral=True)
        return

    db.set_parties_enabled(union, True, role_ids)
    await interaction.response.send_message(
        localized("allow_parties_enabled", lang, code=union,
                  roles=", ".join(f"<@&{r}>" for r in role_ids))
    )
    await send_service_event("parties_enabled", code=union, user=str(interaction.user))

@bot.tree.command(name="add-party", description="found a new party in this union (interactive dialog)")
@app_commands.describe(founder="Founder's user ID or mention (bot admins only)")
async def add_party_cmd(interaction: discord.Interaction, founder: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_verified(interaction, lang):
        return
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return
    _enabled, role_ids = db.get_party_settings(union)

    caller_is_bot_admin = is_admin("discord", interaction.user.id)
    if founder is not None and not caller_is_bot_admin:
        await interaction.response.send_message(localized("add_party_founder_admin_only", lang), ephemeral=True)
        return
    if not caller_is_bot_admin:
        member_roles = {r.id for r in getattr(interaction.user, "roles", [])}
        if not member_roles & role_ids:
            await interaction.response.send_message(localized("add_party_no_role", lang), ephemeral=True)
            return

    founder_id = interaction.user.id
    if founder is not None:
        parsed = _parse_user_ref(founder)
        if parsed is None:
            await interaction.response.send_message(localized("edit_invalid_user", lang), ephemeral=True)
            return
        founder_id = parsed
    founder_name = str(interaction.user)
    if founder_id != interaction.user.id:
        try:
            founder_user = await bot.fetch_user(founder_id)
            founder_name = str(founder_user)
        except Exception:
            founder_name = str(founder_id)

    name_msg = await _dialog_text(interaction, lang, localized("add_party_ask_name", lang))
    if name_msg is None:
        return
    name = clean_display_name(name_msg.content, max_len=100)

    code = await _dialog_text(
        interaction, lang, localized("add_party_ask_code", lang),
        validator=_party_code_validator, error_key="add_party_invalid_code",
    )
    if code is None:
        return

    logo = await _dialog_logo(interaction, lang, localized("add_party_ask_logo", lang))
    if logo is None:
        return

    db.create_party(code, union, name, "discord", founder_id, founder_name,
                    logo=logo[0], logo_mime=logo[1])
    await interaction.channel.send(
        localized("add_party_created", lang, name=name, code=code,
                  union=db.get_union_name(union, lang),
                  founder=f"<@{founder_id}>"),
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    await send_service_event("party_created", name=name, party=code, union=union,
                             user=str(interaction.user))

async def _resolve_led_party(interaction, lang):
    """The party the caller manages in this union. Asks which one when several."""
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return None
    parties = db.user_led_parties(union, "discord", interaction.user.id)
    if not parties:
        await interaction.response.send_message(localized("party_none_led", lang), ephemeral=True)
        return None
    if len(parties) == 1:
        return parties[0]
    return await _numbered_choice(interaction, lang, localized("party_choose", lang),
                                  parties, lambda p: _party_line(p, lang))

def _edit_menu_text(party, lang):
    lines = [
        localized("edit_menu_1", lang),
        localized("edit_menu_2", lang),
        localized("edit_menu_3", lang),
        localized("edit_menu_4", lang),
        ("" if party["description"] else "⚠️ ") + localized("edit_menu_5", lang),
        ("" if party["ideologies"] else "⚠️ ") + localized("edit_menu_6", lang),
        localized("edit_menu_7", lang),
        localized("edit_menu_8", lang),
        localized("edit_menu_9_resume" if party["suspended"] else "edit_menu_9", lang),
        ("" if party["article_url"] else "⚠️ ") + localized("edit_menu_10", lang),
    ]
    return "\n".join(lines)

def _rules_menu_text(party, lang):
    state = localized("state_yes" if party["allow_member_invites"] else "state_no", lang)
    return localized("rules_menu_1", lang, state=state)

def _edit_admin_menu_text(party, lang):
    """The whole party: the ten leader options, the rules toggle, and the two
    that only a Bot Admin gets — moving the party to another union and deleting
    it."""
    state = localized("state_yes" if party["allow_member_invites"] else "state_no", lang)
    return "\n".join([
        _edit_menu_text(party, lang),
        localized("edit_admin_menu_11", lang, state=state),
        localized("edit_admin_menu_12", lang),
        localized("edit_admin_menu_13", lang),
        "",
        localized("edit_admin_choose", lang),
    ])

async def _send_edit_menu(interaction, party, lang, title_key="edit_menu_title", body=None):
    if body is None:
        body = _edit_menu_text(party, lang) if title_key == "edit_menu_title" \
            else _rules_menu_text(party, lang)
    embed = discord.Embed(
        title=localized(title_key, lang, name=party["name"], code=party["code"]),
        description=body,
        color=_party_color(party),
    )
    file, thumb = _party_logo_file(party)
    kwargs = {"embed": embed}
    if thumb:
        embed.set_thumbnail(url=thumb)
        kwargs["file"] = file
    await _say(interaction, **kwargs)

async def _offer_leadership(interaction, party, lang, transfer):
    prompt_key = "edit_ask_transfer" if transfer else "edit_ask_leader"
    msg = await _dialog_text(interaction, lang, localized(prompt_key, lang))
    if msg is None:
        return
    target_id = _parse_user_ref(msg.content)
    if target_id is None:
        await interaction.channel.send(localized("edit_invalid_user", lang))
        return

    offer_key = "consent_transfer_offer" if transfer else "consent_leader_offer"
    channel = interaction.channel

    async def on_accept(interaction2):
        display = str(interaction2.user)
        if transfer:
            db.set_party_leader(party["code"], "discord", target_id, display)
            text = localized("transfer_done", lang, user=f"<@{target_id}>", name=party["name"])
        else:
            db.add_party_leader(party["code"], "discord", target_id, display)
            text = localized("leader_added", lang, user=f"<@{target_id}>", name=party["name"])
        await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))

    async def on_decline(interaction2):
        key = "transfer_declined" if transfer else "leader_declined"
        await channel.send(localized(key, lang))

    await channel.send(
        localized(offer_key, lang, mention=f"<@{target_id}>",
                  name=party["name"], code=party["code"]),
        view=_ConsentView(lang, target_id, on_accept, on_decline),
        allowed_mentions=discord.AllowedMentions(users=True),
    )

async def _confirm_suspend(interaction, party, lang):
    resume = bool(party["suspended"])
    channel = interaction.channel
    ask_key = "resume_confirm" if resume else "suspend_confirm"

    async def on_accept(interaction2):
        db.update_party_field(party["code"], "suspended", 0 if resume else 1)
        key = "resume_done" if resume else "suspend_done"
        await channel.send(localized(key, lang, name=party["name"]))

    async def on_decline(interaction2):
        await channel.send(localized("action_cancelled", lang))

    await _say(interaction, localized(ask_key, lang, name=party["name"]),
               view=_ConsentView(lang, interaction.user.id, on_accept, on_decline))

async def _confirm_delete_party(interaction, party, lang):
    channel = interaction.channel
    name, code, union = party["name"], party["code"], party["union_code"]
    actor = str(interaction.user)

    async def on_accept(interaction2):
        db.delete_party(code)
        await channel.send(localized("delete_party_done", lang, name=name, code=code))
        await send_service_event("party_deleted", name=name, party=code,
                                 union=union, user=actor)

    async def on_decline(interaction2):
        await channel.send(localized("action_cancelled", lang))

    await _say(interaction, localized("delete_party_confirm", lang, name=name, code=code),
               view=_ConsentView(lang, interaction.user.id, on_accept, on_decline))

async def _edit_party_option(interaction, party, lang, option, admin=False):
    if option == 1:
        msg = await _dialog_text(interaction, lang, localized("edit_ask_name", lang))
        if msg is None:
            return
        db.update_party_field(party["code"], "name", clean_display_name(msg.content, max_len=100))
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 2:
        code = await _dialog_text(interaction, lang, localized("edit_ask_code", lang),
                                  validator=_party_code_validator,
                                  error_key="add_party_invalid_code")
        if code is None:
            return
        db.rename_party_code(party["code"], code)
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 3:
        logo = await _dialog_logo(interaction, lang, localized("edit_ask_logo", lang))
        if logo is None:
            return
        db.update_party_logo(party["code"], logo[0], logo[1])
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 4:
        def color_validator(value):
            m = COLOR_RE.match(value.strip())
            return f"#{m.group(1).lower()}" if m else None
        color = await _dialog_text(interaction, lang, localized("edit_ask_color", lang),
                                   validator=color_validator, error_key="edit_invalid_color")
        if color is None:
            return
        db.update_party_field(party["code"], "color", color)
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 5:
        msg = await _dialog_text(interaction, lang, localized("edit_ask_description", lang))
        if msg is None:
            return
        db.update_party_field(party["code"], "description", (msg.content or "").strip()[:1024])
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 6:
        msg = await _dialog_text(interaction, lang, localized("edit_ask_ideologies", lang))
        if msg is None:
            return
        db.update_party_field(party["code"], "ideologies", (msg.content or "").strip()[:1024])
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 7:
        await _offer_leadership(interaction, party, lang, transfer=False)
    elif option == 8:
        await _offer_leadership(interaction, party, lang, transfer=True)
    elif option == 9:
        await _confirm_suspend(interaction, party, lang)
    elif option == 10:
        def url_validator(value):
            return value.strip() if URL_RE.match(value.strip()) else None
        url = await _dialog_text(interaction, lang, localized("edit_ask_article", lang),
                                 validator=url_validator, error_key="edit_invalid_article")
        if url is None:
            return
        db.update_party_field(party["code"], "article_url", url)
        await interaction.channel.send(localized("edit_saved", lang))
    elif admin and option == 11:
        new_value = 0 if party["allow_member_invites"] else 1
        db.update_party_field(party["code"], "allow_member_invites", new_value)
        key = "rules_invites_enabled" if new_value else "rules_invites_disabled"
        await _say(interaction, localized(key, lang))
    elif admin and option == 12:
        def union_validator(value):
            code = value.strip().upper()
            return code if db.union_exists(code) else None
        union = await _dialog_text(interaction, lang, localized("edit_ask_union", lang),
                                   validator=union_validator, error_key="edit_invalid_union")
        if union is None:
            return
        db.update_party_union(party["code"], union)
        await interaction.channel.send(
            localized("edit_union_changed", lang, name=party["name"],
                      union=db.get_union_name(union, lang)))
    elif admin and option == 13:
        await _confirm_delete_party(interaction, party, lang)
    else:
        await _say(interaction, localized("edit_invalid_option", lang), ephemeral=True)

@bot.tree.command(name="edit-party", description="manage your party (founders and party leaders)")
@app_commands.describe(option="Option number 1-10; omit to see the menu")
async def edit_party_cmd(interaction: discord.Interaction, option: int = None):
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_verified(interaction, lang):
        return
    party = await _resolve_led_party(interaction, lang)
    if party is None:
        return

    if option is None:
        await _send_edit_menu(interaction, party, lang)
        return
    await _edit_party_option(interaction, party, lang, option)

@bot.tree.command(name="edit-party-admin", description="edit any party of any union (bot admins)")
@app_commands.describe(party="Party code, or its full name exactly as written")
async def edit_party_admin_cmd(interaction: discord.Interaction, party: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    row = db.find_party(party)
    if row is None:
        await interaction.response.send_message(
            localized("edit_admin_not_found", lang, query=party.strip()), ephemeral=True)
        return

    await _send_edit_menu(interaction, row, lang, title_key="edit_admin_menu_title",
                          body=_edit_admin_menu_text(row, lang))

    msg = await _wait_message(interaction.channel_id, interaction.user.id)
    if msg is None:
        return
    value = (msg.content or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= 13):
        await interaction.channel.send(localized("choice_invalid", lang))
        return
    await _edit_party_option(interaction, row, lang, int(value), admin=True)

@bot.tree.command(name="edit-rules", description="manage your party's rules (founders and party leaders)")
@app_commands.describe(option="Option number; omit to see the menu")
async def edit_rules_cmd(interaction: discord.Interaction, option: int = None):
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_verified(interaction, lang):
        return
    party = await _resolve_led_party(interaction, lang)
    if party is None:
        return

    if option is None:
        await _send_edit_menu(interaction, party, lang, title_key="rules_menu_title")
        return

    if option == 1:
        new_value = 0 if party["allow_member_invites"] else 1
        db.update_party_field(party["code"], "allow_member_invites", new_value)
        key = "rules_invites_enabled" if new_value else "rules_invites_disabled"
        await _say(interaction, localized(key, lang))
    else:
        await _say(interaction, localized("edit_invalid_option", lang), ephemeral=True)

def _search_party(union_code, query):
    """(exact_match_or_None, suggestions). Falls back to 3 random union parties."""
    q = (query or "").strip()
    parties = db.get_union_parties(union_code)
    for p in parties:
        if p["code"].lower() == q.lower() or p["name"].lower() == q.lower():
            return p, []
    candidates = {}
    for p in parties:
        candidates.setdefault(p["name"].lower(), p)
        candidates.setdefault(p["code"].lower(), p)
    close = find_close_names(q, candidates)
    if not close:
        close = list(db.get_random_union_parties(union_code, 3))
    return None, close

@bot.tree.command(name="party", description="information about a party of this union")
@app_commands.describe(query="Party code or name")
async def party_cmd(interaction: discord.Interaction, query: str):
    lang = get_chat_lang(_chat_key(interaction))
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return

    party, suggestions = _search_party(union, query)
    if party is None:
        if not suggestions:
            await interaction.response.send_message(localized("party_not_found", lang))
            return
        party = await _numbered_choice(interaction, lang,
                                       localized("party_not_found_suggest", lang),
                                       suggestions, lambda p: _party_line(p, lang))
        if party is None:
            return
    await _send_party_info(interaction, party, lang)

@bot.tree.command(name="add-govt", description="create a government body in a union (bot admins)")
@app_commands.describe(
    union="Union code",
    name="Name of the government body",
    member_singular="What one member is called (e.g. minister)",
    member_plural="What several members are called (e.g. ministers)",
)
async def add_govt_cmd(interaction: discord.Interaction, union: str, name: str,
                       member_singular: str, member_plural: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    union = union.strip().upper()
    if not db.union_exists(union):
        await interaction.response.send_message(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes())),
            ephemeral=True,
        )
        return

    name = clean_display_name(name, max_len=100)
    if not db.create_govt_body(union, name, member_singular.strip(), member_plural.strip()):
        await interaction.response.send_message(localized("add_govt_exists", lang), ephemeral=True)
        return
    await interaction.response.send_message(
        localized("add_govt_created", lang, name=name,
                  union=db.get_union_name(union, lang),
                  singular=member_singular.strip(), plural=member_plural.strip())
    )

def _govt_member_lines(body, lang, viewer_platform="discord"):
    members = db.get_govt_members(body["id"])
    if not members:
        return None
    parts = []
    for m in members:
        shown = format_stored_user(viewer_platform, m["platform"], m["user_id"], m["display_name"])
        party = db.find_user_party(body["union_code"], m["platform"], m["user_id"])
        if party:
            shown = f"{shown} | {party['name']}"
        parts.append(shown)
    return " · ".join(parts)

def _search_govt(union_code, query):
    """(exact_match_or_None, suggestions). Suggestions may include bodies of
    other unions (marked by the caller); no random fallback."""
    q = (query or "").strip()
    for b in db.get_govt_bodies(union_code):
        if b["name"].lower() == q.lower():
            return b, []
    candidates = {}
    for b in db.get_govt_bodies():
        candidates.setdefault(b["name"].lower(), b)
    return None, find_close_names(q, candidates)

def _govt_line(body, union_code, lang):
    line = body["name"]
    if body["union_code"] != union_code:
        line += " " + localized("govt_other_union_mark", lang, code=body["union_code"])
    return line

async def _send_govt_info(interaction, body, lang):
    members_text = _govt_member_lines(body, lang) or localized("govt_no_members", lang)
    embed = discord.Embed(title=body["name"], description=members_text[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    embed.set_footer(text=db.get_union_name(body["union_code"], lang))
    await _say(interaction, embed=embed)

@bot.tree.command(name="govt", description="information about a government body of this union")
@app_commands.describe(query="Name of the government body")
async def govt_cmd(interaction: discord.Interaction, query: str):
    lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return
    chat = db.get_chat(str(interaction.guild_id))
    if not chat:
        await _refuse_not_setup(interaction, lang)
        return

    body, suggestions = _search_govt(chat["union_code"], query)
    if body is None:
        if not suggestions:
            await interaction.response.send_message(localized("govt_not_found", lang))
            return
        body = await _numbered_choice(interaction, lang,
                                      localized("govt_not_found_suggest", lang),
                                      suggestions,
                                      lambda b: _govt_line(b, chat["union_code"], lang))
        if body is None:
            return
    await _send_govt_info(interaction, body, lang)

@bot.tree.command(name="verify", description="verify your Fandom account (checks your Discord handle on your Fandom profile)")
@app_commands.describe(fandom_name="Your username on fandom.com")
async def verify_cmd(interaction: discord.Interaction, fandom_name: str):
    lang = get_chat_lang(_chat_key(interaction))
    fandom_name = fandom_name.strip()
    if not fandom_name:
        await interaction.response.send_message(localized("verify_usage", lang), ephemeral=True)
        return
    if not rate_limit_ok(("verify", interaction.user.id), limit=3, window_seconds=600):
        await interaction.response.send_message(localized("verify_cooldown", lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    status, uid, cname, handle = await fandom.lookup_discord_handle(fandom_name)
    if status == "error":
        await interaction.followup.send(localized("verify_error", lang), ephemeral=True)
        return
    if status == "not_found":
        await interaction.followup.send(localized("verify_not_found", lang, name=fandom_name), ephemeral=True)
        return
    if status == "no_handle":
        await interaction.followup.send(localized("verify_no_handle", lang, name=cname), ephemeral=True)
        return

    if not fandom.handle_matches(handle, interaction.user.name, str(interaction.user)):
        await interaction.followup.send(
            localized("verify_mismatch", lang, handle=handle, you=interaction.user.name), ephemeral=True)
        return

    db.add_fandom_verification(interaction.user.id, cname, uid, handle)
    await interaction.followup.send(localized("verify_success", lang, name=cname), ephemeral=True)

@bot.tree.command(name="add-telegram", description="link your Telegram account (run /add-discord on Telegram within 30 min)")
@app_commands.describe(nickname="Your Telegram @username")
async def add_telegram_cmd(interaction: discord.Interaction, nickname: str):
    lang = get_chat_lang(_chat_key(interaction))
    telegram_nick = nickname.strip().lstrip("@")
    if not telegram_nick:
        await interaction.response.send_message(localized("link_usage_discord", lang), ephemeral=True)
        return
    if not is_verified("discord", interaction.user.id):
        await interaction.response.send_message(localized("link_need_verify", lang), ephemeral=True)
        return
    result, _dc, _tg = db.register_link_attempt("discord", interaction.user.id,
                                                interaction.user.name, telegram_nick)
    if result == "linked":
        await interaction.response.send_message(localized("link_success", lang), ephemeral=True)
    else:
        await interaction.response.send_message(
            localized("link_pending_discord", lang, nick=telegram_nick), ephemeral=True)

def _find_party_exact(union_code, query):
    """Exact match by code or name (case-insensitive), as /party-join requires."""
    q = (query or "").strip()
    for p in db.get_union_parties(union_code):
        if p["code"].lower() == q.lower() or p["name"].lower() == q.lower():
            return p
    return None

class JoinRequestView(ui.View):
    """Persistent Accept/Reject buttons on a leader's DM. custom_ids embed the
    request id so the buttons keep working after a restart (views are re-added
    in setup_hook)."""

    def __init__(self, request_id, lang):
        super().__init__(timeout=None)
        self.request_id = request_id
        accept = ui.Button(label=localized("party_join_accept", lang),
                           style=ButtonStyle.success, custom_id=f"pj:{request_id}:1")
        reject = ui.Button(label=localized("party_join_reject", lang),
                           style=ButtonStyle.danger, custom_id=f"pj:{request_id}:0")
        accept.callback = self._make_cb(True)
        reject.callback = self._make_cb(False)
        self.add_item(accept)
        self.add_item(reject)

    def _make_cb(self, accepted):
        async def callback(interaction: discord.Interaction):
            await _resolve_join_request(interaction, self.request_id, accepted)
        return callback

async def _notify_discord_leaders(party, request_id, lang, requester_display):
    """DM every Discord leader of the party (the founder is always one). Returns
    how many were reached."""
    text = localized("party_join_leader_dm", lang, requester=requester_display,
                     name=party["name"], code=party["code"])
    sent = 0
    for l in db.get_party_leaders(party["code"]):
        if l["platform"] != "discord":
            continue
        try:
            user = await bot.fetch_user(int(l["user_id"]))
            await user.send(text, view=JoinRequestView(request_id, lang))
            sent += 1
        except Exception:
            pass
    return sent

async def _notify_join_result(req, party, accepted):
    lang = req["lang"] or DEFAULT_LANG
    key = "party_join_accepted_user" if accepted else "party_join_rejected_user"
    text = localized(key, lang, name=party["name"], code=party["code"])
    if req["requester_platform"] == "discord":
        try:
            u = await bot.fetch_user(int(req["requester_id"]))
            await u.send(text)
        except Exception:
            pass
    else:
        try:
            from telegram_bot import bot as tg
            await tg.send_message(int(req["requester_id"]), text)
        except Exception:
            pass

async def _resolve_join_request(interaction: discord.Interaction, request_id, accepted):
    req = db.get_join_request(request_id)
    if not req:
        try:
            await interaction.response.edit_message(
                content=localized("party_join_already_handled", get_chat_lang(_chat_key(interaction))),
                view=None)
        except Exception:
            pass
        return
    lang = req["lang"] or DEFAULT_LANG
    party = db.get_party(req["party_code"])
    if not party:
        db.delete_join_request(request_id)
        try:
            await interaction.response.edit_message(content=localized("party_not_found", lang), view=None)
        except Exception:
            pass
        return
    if not db.is_party_leader(party["code"], "discord", interaction.user.id):
        await interaction.response.send_message(localized("party_join_not_leader", lang), ephemeral=True)
        return

    db.delete_join_request(request_id)
    if accepted:
        db.add_party_member(party["code"], req["requester_platform"],
                            req["requester_id"], req["requester_name"])
    outcome = "party_join_accepted_leader" if accepted else "party_join_rejected_leader"
    try:
        await interaction.response.edit_message(
            content=localized(outcome, lang, requester=req["requester_name"], name=party["name"]),
            view=None)
    except Exception:
        pass
    await _notify_join_result(req, party, accepted)

@bot.tree.command(name="party-join", description="request to join a party of this union")
@app_commands.describe(query="Party code or its exact name")
async def party_join_cmd(interaction: discord.Interaction, query: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_verified(interaction, lang):
        return
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return

    party = _find_party_exact(union, query)
    if not party:
        await interaction.response.send_message(localized("party_not_found", lang))
        return
    if party["suspended"]:
        await interaction.response.send_message(localized("party_join_suspended", lang), ephemeral=True)
        return

    existing = db.find_user_party(union, "discord", interaction.user.id)
    if existing:
        if existing["code"] == party["code"]:
            await interaction.response.send_message(localized("party_join_already_member", lang), ephemeral=True)
        else:
            await interaction.response.send_message(
                localized("party_join_in_other", lang, name=existing["name"]), ephemeral=True)
        return
    if db.has_open_join_request(party["code"], "discord", interaction.user.id):
        await interaction.response.send_message(localized("party_join_pending", lang), ephemeral=True)
        return

    rid = db.create_join_request(party["code"], "discord", interaction.user.id,
                                 str(interaction.user), lang)
    delivered = await _notify_discord_leaders(party, rid, lang, f"<@{interaction.user.id}>")
    if delivered == 0:
        db.delete_join_request(rid)
        await interaction.response.send_message(localized("party_join_no_leader", lang), ephemeral=True)
        return
    await interaction.response.send_message(localized("party_join_sent", lang, name=party["name"]), ephemeral=True)

@bot.tree.command(name="party-leave", description="leave your party in this union")
async def party_leave_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return
    party = db.find_user_party(union, "discord", interaction.user.id)
    if not party:
        await interaction.response.send_message(localized("party_leave_none", lang), ephemeral=True)
        return
    if db.is_founder(party["code"], "discord", interaction.user.id):
        await interaction.response.send_message(localized("party_leave_founder", lang), ephemeral=True)
        return
    db.remove_party_leader(party["code"], "discord", interaction.user.id)
    db.remove_party_member(party["code"], "discord", interaction.user.id)
    await interaction.response.send_message(localized("party_leave_done", lang, name=party["name"]), ephemeral=True)

def _find_kick_target(code, platform, target):
    """Locate a plain member of the party by ID/mention or by stored nickname."""
    raw = (target or "").strip()
    m = USER_REF_RE.match(raw)
    tid = (m.group(1) or m.group(2)) if m else None
    name = raw.lstrip("@").lower()
    for mem in db.get_party_members(code):
        if mem["platform"] != platform:
            continue
        if tid and mem["user_id"] == tid:
            return mem
        if (mem["display_name"] or "").lstrip("@").lower() == name:
            return mem
    return None

@bot.tree.command(name="party-kick", description="remove a member from your party (party leaders)")
@app_commands.describe(target="Member's ID, mention or username")
async def party_kick_cmd(interaction: discord.Interaction, target: str):
    lang = get_chat_lang(_chat_key(interaction))
    party = await _resolve_led_party(interaction, lang)
    if party is None:
        return

    mem = _find_kick_target(party["code"], "discord", target)
    if mem is None:
        m = USER_REF_RE.match(target.strip())
        tid = (m.group(1) or m.group(2)) if m else None
        if tid and db.is_founder(party["code"], "discord", tid):
            await _say(interaction, localized("party_kick_founder", lang), ephemeral=True)
        elif tid and db.is_party_leader(party["code"], "discord", tid):
            await _say(interaction, localized("party_kick_leader", lang), ephemeral=True)
        else:
            await _say(interaction, localized("party_kick_not_member", lang), ephemeral=True)
        return

    db.remove_party_member(party["code"], "discord", mem["user_id"])
    await _say(interaction,
               localized("party_kick_done", lang, user=f"<@{mem['user_id']}>", name=party["name"]),
               allowed_mentions=discord.AllowedMentions(users=True))

_active_quizzes = {}

async def _wait_message_or_stop(channel_id, user_id, stop_event, timeout=DIALOG_TIMEOUT):
    """Race the taker's next message against a /quizzes-stop. Returns
    ('message', msg), ('stop', None) or ('timeout', None)."""
    def check(m):
        return m.author.id == user_id and m.channel.id == channel_id

    msg_task = asyncio.ensure_future(bot.wait_for("message", check=check, timeout=timeout))
    stop_task = asyncio.ensure_future(stop_event.wait())
    done, pending = await asyncio.wait({msg_task, stop_task},
                                       return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if stop_task in done:
        return "stop", None
    try:
        return "message", msg_task.result()
    except Exception:
        return "timeout", None

class _QuizButtons(ui.View):
    """A row of buttons only one person may press; `wait_click` resolves with the
    pressed button's value (or None on timeout), `wait_value` waits without a
    deadline of its own so the caller can race it against something else. Used by
    the quizzes and by the Olympiad dialogs."""

    def __init__(self, user_id, lang, buttons, timeout=DIALOG_TIMEOUT):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.lang = lang
        self.value = None
        self._event = asyncio.Event()
        for label, style, value in buttons:
            b = ui.Button(label=label, style=style)
            b.callback = self._make_cb(value)
            self.add_item(b)

    def _make_cb(self, value):
        async def cb(interaction2: discord.Interaction):
            if interaction2.user.id != self.user_id:
                await interaction2.response.send_message(
                    localized("consent_not_yours", self.lang), ephemeral=True)
                return
            self.value = value
            try:
                await interaction2.response.defer()
            except Exception:
                pass
            self._event.set()
            self.stop()
        return cb

    async def wait_click(self):
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            pass
        return self.value

    async def wait_value(self):
        await self._event.wait()
        return self.value

def _quiz_question_embed(quiz_id, lang, qindex, order, allow_prev, total):
    topic = quizzes.question_topic(quiz_id, lang, qindex)
    texts, _kinds = quizzes.display_options(quiz_id, lang, qindex, order, allow_prev)
    lines = [f"{i + 1}. {t}" for i, t in enumerate(texts)]
    desc = "\n".join(lines) + "\n\n" + localized("quiz_answer_hint", lang)
    return discord.Embed(
        title=f"{topic}  ({qindex + 1}/{total})"[:256],
        description=desc[:4096],
        color=discord.Color(DEFAULT_EMBED_COLOR),
    )

def _quiz_results_embed(quiz_id, lang, results, name):
    embed = discord.Embed(
        title=localized("quiz_results_title", lang, name=name)[:256],
        color=discord.Color(DEFAULT_EMBED_COLOR),
    )
    for a in quizzes.format_results(quiz_id, lang, results):
        breakdown = " · ".join(f"{e['name']} — {e['percent']}%" for e in a["entries"])
        value = breakdown
        if a["top_desc"]:
            value += "\n\n" + a["top_desc"]
        embed.add_field(name=a["axis_name"][:256], value=value[:1024], inline=False)
    return embed

def _resume_state(row, quiz_id, n):
    """(orders, answers, qindex) of a parked attempt, or None when the saved
    shape no longer matches the quiz."""
    try:
        orders = json.loads(row["orders_json"])
        answers = json.loads(row["answers_json"])
    except Exception:
        return None
    if len(orders) != n or len(answers) != n:
        return None
    qindex = int(row["qindex"])
    if not 0 <= qindex < n:
        return None
    return orders, answers, qindex

async def _run_quiz_discord(interaction: discord.Interaction, quiz_id):
    lang = get_chat_lang(_chat_key(interaction))
    user_id = interaction.user.id
    channel = interaction.channel
    if channel is None:
        channel = interaction.user.dm_channel or await interaction.user.create_dm()
    name = quizzes.quiz_name(quiz_id, lang)
    n = quizzes.num_questions(quiz_id)

    saved = db.get_quiz_progress("discord", user_id, quiz_id)
    resumed = _resume_state(saved, quiz_id, n) if saved else None

    if resumed:
        orders, answers, qindex = resumed
        msg = await channel.send(embed=discord.Embed(
            title=name[:256],
            description=localized("quiz_resumed", lang, name=name, num=qindex + 1, total=n),
            color=discord.Color(DEFAULT_EMBED_COLOR)))
    else:
        start_view = _QuizButtons(user_id, lang, [
            (localized("quiz_go", lang), ButtonStyle.success, "go"),
            (localized("quiz_later", lang), ButtonStyle.secondary, "later"),
        ])
        prompt = localized("quiz_start_prompt", lang, name=name)
        credit = quizzes.quiz_credit(quiz_id, lang)
        if credit:
            prompt = f"{prompt}\n\n{credit}"
        embed = discord.Embed(title=name[:256],
                              description=prompt[:4096],
                              color=discord.Color(DEFAULT_EMBED_COLOR))
        msg = await channel.send(embed=embed, view=start_view)
        if await start_view.wait_click() != "go":
            await msg.edit(
                embed=discord.Embed(description=localized("quiz_cancelled", lang),
                                    color=discord.Color(DEFAULT_EMBED_COLOR)),
                view=None)
            return
        orders = quizzes.make_orders(quiz_id)
        answers = [None] * n
        qindex = 0

    session = {"quiz_id": quiz_id, "stop": asyncio.Event()}
    _active_quizzes[user_id] = session
    try:
        while qindex < n:
            allow_prev = qindex > 0
            await msg.edit(embed=_quiz_question_embed(quiz_id, lang, qindex, orders[qindex],
                                                      allow_prev, n), view=None)
            outcome, reply = await _wait_message_or_stop(channel.id, user_id, session["stop"])
            if outcome == "stop":
                db.save_quiz_progress("discord", user_id, quiz_id, lang, qindex,
                                      json.dumps(orders),
                                      json.dumps(answers, ensure_ascii=False))
                await msg.edit(embed=discord.Embed(
                    description=localized("quiz_stop_saved", lang, name=name),
                    color=discord.Color(DEFAULT_EMBED_COLOR)), view=None)
                return
            if outcome == "timeout":
                await channel.send(localized("dialog_timeout", lang))
                return
            parsed = quizzes.parse_answer(reply.content or "")
            if parsed is None:
                await channel.send(localized("quiz_answer_invalid", lang))
                continue
            num, weight = parsed
            _texts, kinds = quizzes.display_options(quiz_id, lang, qindex, orders[qindex], allow_prev)
            if num < 1 or num > len(kinds):
                await channel.send(localized("quiz_answer_out_of_range", lang))
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
                    await channel.send(localized("quiz_answer_need_weight", lang, num=num))
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
        _active_quizzes.pop(user_id, None)

    db.delete_quiz_progress("discord", user_id, quiz_id)
    results = quizzes.score(quiz_id, answers)

    consent_view = _QuizButtons(user_id, lang, [
        (localized("quiz_consent_yes", lang), ButtonStyle.success, "yes"),
        (localized("quiz_consent_no", lang), ButtonStyle.danger, "no"),
    ])
    await msg.edit(embed=discord.Embed(description=localized("quiz_consent_prompt", lang),
                                       color=discord.Color(DEFAULT_EMBED_COLOR)),
                   view=consent_view)
    consent = await consent_view.wait_click()
    if consent == "yes":
        db.save_quiz_result("discord", user_id, quiz_id, lang,
                            json.dumps(results, ensure_ascii=False),
                            json.dumps(answers, ensure_ascii=False))
        await channel.send(localized("quiz_saved", lang))
    elif consent == "no":
        await channel.send(localized("quiz_not_saved", lang))

    await msg.edit(embed=_quiz_results_embed(quiz_id, lang, results, name), view=None)

@bot.tree.command(name="quizzes", description="take a quiz (works in DMs too)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    ids = quizzes.list_quizzes()
    lines = [localized("quizzes_list_header", lang)]
    for i, qid in enumerate(ids):
        lines.append(f"{i + 1}. {quizzes.quiz_name(qid, lang)}")
    await interaction.response.send_message(embed=discord.Embed(
        title=localized("quizzes_title", lang),
        description="\n".join(lines), color=discord.Color(DEFAULT_EMBED_COLOR)))

    reply = await _wait_message(interaction.channel_id, interaction.user.id)
    if reply is None:
        return
    value = (reply.content or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(ids)):
        await interaction.channel.send(localized("choice_invalid", lang))
        return
    await _run_quiz_discord(interaction, ids[int(value) - 1])

@bot.tree.command(name="quizzes-stop", description="pause the quiz you are taking; your progress is kept for 7 days")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_stop_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    session = _active_quizzes.get(interaction.user.id)
    if not session:
        await interaction.response.send_message(localized("quiz_stop_none", lang), ephemeral=True)
        return
    session["stop"].set()
    await interaction.response.send_message(
        localized("quiz_stop_saved", lang, name=quizzes.quiz_name(session["quiz_id"], lang)),
        ephemeral=True)

@bot.tree.command(name="quizzes-clear", description="delete your saved quiz results")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_clear_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    rows = db.get_user_quiz_results("discord", interaction.user.id)
    if not rows:
        await interaction.response.send_message(localized("quizzes_clear_none", lang), ephemeral=True)
        return
    lines = [localized("quizzes_clear_header", lang)]
    for i, r in enumerate(rows):
        lines.append(f"{i + 1}. {quizzes.quiz_name(r['quiz_id'], lang)} — {format_quiz_date(r['created_at'])}")
    await interaction.response.send_message(embed=discord.Embed(
        title=localized("quizzes_title", lang),
        description="\n".join(lines), color=discord.Color(DEFAULT_EMBED_COLOR)))

    reply = await _wait_message(interaction.channel_id, interaction.user.id)
    if reply is None:
        return
    value = (reply.content or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(rows)):
        await interaction.channel.send(localized("choice_invalid", lang))
        return
    row = rows[int(value) - 1]
    db.delete_quiz_result(row["id"])
    await interaction.channel.send(localized(
        "quizzes_clear_deleted", lang,
        name=quizzes.quiz_name(row["quiz_id"], lang), date=format_quiz_date(row["created_at"])))

def _quiz_compare_embed(quiz_id, lang, res_a, res_b, name_a, name_b):
    embed = discord.Embed(
        title=localized("quizzes_compare_title", lang, name=quizzes.quiz_name(quiz_id, lang))[:256],
        color=discord.Color(DEFAULT_EMBED_COLOR))
    fa = quizzes.format_results(quiz_id, lang, res_a)
    fb = quizzes.format_results(quiz_id, lang, res_b)
    fb_by_axis = {a["axis"]: a for a in fb}
    for a in fa:
        b = fb_by_axis.get(a["axis"], {"entries": []})
        va = " · ".join(f"{e['name']} {e['percent']}%" for e in a["entries"])
        vb = " · ".join(f"{e['name']} {e['percent']}%" for e in b["entries"])
        value = f"**{name_a}:** {va}\n**{name_b}:** {vb}"
        embed.add_field(name=a["axis_name"][:256], value=value[:1024], inline=False)
    return embed

async def _quiz_target_name(target_id):
    try:
        u = await bot.fetch_user(int(target_id))
        return str(u)
    except Exception:
        return str(target_id)

@bot.tree.command(name="quizzes-compare", description="compare quiz results with another user")
@app_commands.describe(user="The other user's ID or mention")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_compare_cmd(interaction: discord.Interaction, user: str):
    lang = get_chat_lang(_chat_key(interaction))
    target_id = _parse_user_ref(user)
    if target_id is None:
        await interaction.response.send_message(localized("quizzes_compare_user_invalid", lang), ephemeral=True)
        return
    if str(target_id) == str(interaction.user.id):
        await interaction.response.send_message(localized("quizzes_compare_self", lang), ephemeral=True)
        return

    if db.is_quiz_compare_blocked("discord", target_id):
        await interaction.response.send_message(localized("quizzes_compare_blocked", lang), ephemeral=True)
        return

    blocked = db.get_blocked_quiz_ids("discord", target_id)
    mine = db.get_user_quiz_ids("discord", interaction.user.id)
    theirs = set(db.get_user_quiz_ids("discord", target_id))
    shared = [q for q in mine if q in theirs and q not in blocked]
    if not shared:
        if any(q in theirs for q in mine):
            await interaction.response.send_message(localized("quizzes_compare_blocked", lang), ephemeral=True)
        else:
            await interaction.response.send_message(localized("quizzes_compare_none", lang))
        return

    name_a = localized("quizzes_compare_you", lang)
    name_b = await _quiz_target_name(target_id)

    async def show(quiz_id, via_channel):
        ra = db.get_latest_quiz_result("discord", interaction.user.id, quiz_id)
        rb = db.get_latest_quiz_result("discord", target_id, quiz_id)
        if not ra or not rb:
            return
        res_a = json.loads(ra["result_json"])
        res_b = json.loads(rb["result_json"])
        embed = _quiz_compare_embed(quiz_id, lang, res_a, res_b, name_a, name_b)
        if via_channel:
            await interaction.channel.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

    if len(shared) == 1:
        await show(shared[0], via_channel=False)
        return

    lines = [localized("quizzes_compare_header", lang)]
    for i, qid in enumerate(shared):
        lines.append(f"{i + 1}. {quizzes.quiz_name(qid, lang)}")
    await interaction.response.send_message(embed=discord.Embed(
        title=localized("quizzes_title", lang),
        description="\n".join(lines), color=discord.Color(DEFAULT_EMBED_COLOR)))

    reply = await _wait_message(interaction.channel_id, interaction.user.id)
    if reply is None:
        return
    value = (reply.content or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(shared)):
        await interaction.channel.send(localized("choice_invalid", lang))
        return
    await show(shared[int(value) - 1], via_channel=True)

def _quiz_history_embed(quiz_id, lang, latest, previous):
    """The user's two most recent attempts of one quiz, side by side, each
    labelled with the day it was taken."""
    embed = _quiz_compare_embed(
        quiz_id, lang,
        json.loads(latest["result_json"]), json.loads(previous["result_json"]),
        localized("quizzes_previous_current", lang, date=format_quiz_date(latest["created_at"])),
        localized("quizzes_previous_earlier", lang, date=format_quiz_date(previous["created_at"])),
    )
    embed.title = localized("quizzes_previous_title", lang,
                            name=quizzes.quiz_name(quiz_id, lang))[:256]
    return embed

@bot.tree.command(name="quizzes-previous", description="compare your latest quiz result with the one before it")
@app_commands.describe(number="Quiz number from /quizzes; omit it within 30 minutes of finishing a quiz")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_previous_cmd(interaction: discord.Interaction, number: int = None):
    lang = get_chat_lang(_chat_key(interaction))
    user_id = interaction.user.id

    quiz_id, error = resolve_previous_quiz("discord", user_id, number)
    if error:
        await interaction.response.send_message(localized(error, lang), ephemeral=True)
        return

    latest = db.get_latest_quiz_result("discord", user_id, quiz_id)
    previous = db.get_previous_quiz_result("discord", user_id, quiz_id)
    if not latest or not previous:
        await interaction.response.send_message(
            localized("quizzes_previous_none", lang, name=quizzes.quiz_name(quiz_id, lang)),
            ephemeral=True)
        return

    await interaction.response.send_message(
        embed=_quiz_history_embed(quiz_id, lang, latest, previous))

async def _run_privacy_action(interaction: discord.Interaction, lang, action):
    user_id = interaction.user.id
    channel = interaction.channel

    if action == "clear_all":
        db.delete_user_quiz_results("discord", user_id)
        await channel.send(localized("privacy_cleared_all", lang))
        return

    if action == "block_all":
        blocked = not db.is_quiz_compare_blocked("discord", user_id)
        db.set_quiz_compare_blocked("discord", user_id, "", blocked)
        await channel.send(localized(
            "privacy_block_all_on" if blocked else "privacy_block_all_off", lang))
        return

    quiz_ids = db.get_user_quiz_ids("discord", user_id)

    if action == "clear_one":
        quiz_id = await _numbered_choice(interaction, lang,
                                         localized("privacy_choose_quiz_clear", lang),
                                         quiz_ids, lambda q: quizzes.quiz_name(q, lang))
        if quiz_id is None:
            return
        db.delete_user_quiz_results("discord", user_id, quiz_id)
        await channel.send(localized("privacy_cleared_quiz", lang,
                                     name=quizzes.quiz_name(quiz_id, lang)))
        return

    if action == "block_one":
        blocked_ids = db.get_blocked_quiz_ids("discord", user_id)

        def render(q):
            state = localized("state_yes" if q in blocked_ids else "state_no", lang)
            return f"{quizzes.quiz_name(q, lang)} ({state})"

        quiz_id = await _numbered_choice(interaction, lang,
                                         localized("privacy_choose_quiz_block", lang),
                                         quiz_ids, render)
        if quiz_id is None:
            return
        blocked = quiz_id not in blocked_ids
        db.set_quiz_compare_blocked("discord", user_id, quiz_id, blocked)
        await channel.send(localized(
            "privacy_block_quiz_on" if blocked else "privacy_block_quiz_off", lang,
            name=quizzes.quiz_name(quiz_id, lang)))

@bot.tree.command(name="privacy", description="manage the data the bot keeps about you")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def privacy_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    user_id = interaction.user.id

    actions = privacy_actions("discord", user_id)
    if not actions:
        await interaction.response.send_message(localized("privacy_none", lang), ephemeral=True)
        return

    labels = privacy_option_labels("discord", user_id, lang, actions)
    lines = [localized("privacy_header", lang)]
    lines += [f"{i + 1}. {text}" for i, text in enumerate(labels)]
    await interaction.response.send_message(embed=discord.Embed(
        title=localized("privacy_title", lang),
        description="\n".join(lines), color=discord.Color(DEFAULT_EMBED_COLOR)))

    reply = await _wait_message(interaction.channel_id, user_id)
    if reply is None:
        return
    value = (reply.content or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(actions)):
        await interaction.channel.send(localized("choice_invalid", lang))
        return
    await _run_privacy_action(interaction, lang, actions[int(value) - 1])

CHANNEL_REF_RE = re.compile(r"^<#(\d+)>$|^(\d{5,25})$")

def _parse_channel_ref(text):
    m = CHANNEL_REF_RE.match((text or "").strip())
    if not m:
        return None
    return int(m.group(1) or m.group(2))

def _can_manage_webhooks(channel):
    me = getattr(channel.guild, "me", None)
    if me is None:
        return False
    return channel.permissions_for(me).manage_webhooks

async def _founday_webhook(channel, lang):
    """The bot's own «Birthdays» webhook in `channel`, created on first use.
    Returns None when the bot may not manage webhooks there."""
    name = localized("founday_webhook_name", lang)
    try:
        for hook in await channel.webhooks():
            if hook.name == name and hook.user and bot.user and hook.user.id == bot.user.id:
                return hook
        return await channel.create_webhook(name=name)
    except discord.Forbidden:
        return None
    except Exception as e:
        logger.warning("Could not obtain founday webhook in channel %s: %s", channel.id, e)
        return None

async def _send_founday(row, now):
    channel = bot.get_channel(int(row["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(row["channel_id"]))
        except Exception:
            logger.warning("Founday: channel %s is gone", row["channel_id"])
            return False
    if not hasattr(channel, "send"):
        return False

    server_lang = get_chat_lang(f"{row['guild_id']}:{row['channel_id']}")
    founded = utc_from_epoch(row["founded_at"])
    years = now.year - founded.year
    if years <= 0:
        return False
    past = (founded.hour, founded.minute) < (row["hour"], row["minute"])
    text = founday_message(server_lang, row["lang"], row["name"], years, row["url"], past)

    hook = await _founday_webhook(channel, server_lang)
    if hook is not None:
        await hook.send(content=text, username=localized("founday_webhook_name", server_lang))
    else:
        await channel.send(text)
    return True

async def _post_due_foundays():
    now = datetime.now(timezone.utc)
    for row in db.get_wiki_foundays():
        try:
            founded = utc_from_epoch(row["founded_at"])
            if not is_founday_today(founded, now):
                continue
            if row["last_sent_year"] == now.year:
                continue
            if (now.hour, now.minute) < (row["hour"], row["minute"]):
                continue
            if await _send_founday(row, now):
                db.mark_wiki_founday_sent(row["id"], now.year)
        except Exception as e:
            logger.warning("Founday %s failed: %s", row["id"], e)

async def _require_founday_admin(interaction: discord.Interaction, lang):
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return False
    if not (is_admin("discord", interaction.user.id) or is_server_admin(interaction)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return False
    return True

@bot.tree.command(name="wiki-founday", description="announce a wiki's anniversary every year (server admins)")
@app_commands.describe(
    url="Wiki link (https:// may be omitted)",
    name="Wiki name, as it should appear in the greeting",
    founded="Founding date and time in UTC: MM-DD-YYYY or MM-DD-YYYY HH:MM",
    channel_id="Channel to post in (default: this channel)",
    at="Posting time in UTC, HH:MM or HH (default: 06:00)",
    wiki_lang="Language of the wiki (default: this chat's language)",
)
async def wiki_founday_cmd(interaction: discord.Interaction, url: str, name: str, founded: str,
                           channel_id: str = None, at: str = None, wiki_lang: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_founday_admin(interaction, lang):
        return

    wiki_url = normalize_wiki_url(url)
    if wiki_url is None:
        await interaction.response.send_message(localized("founday_invalid_url", lang), ephemeral=True)
        return

    try:
        founded_at = parse_founday_datetime(founded)
    except ValueError:
        await interaction.response.send_message(localized("founday_invalid_date", lang), ephemeral=True)
        return
    if founded_at >= datetime.now(timezone.utc):
        await interaction.response.send_message(localized("founday_future_date", lang), ephemeral=True)
        return

    try:
        hour, minute = parse_founday_time(at)
    except ValueError:
        await interaction.response.send_message(localized("founday_invalid_time", lang), ephemeral=True)
        return

    target = interaction.channel
    if channel_id:
        parsed = _parse_channel_ref(channel_id)
        target = interaction.guild.get_channel(parsed) if parsed else None
    if target is None or not hasattr(target, "send") or getattr(target, "guild", None) is None \
            or target.guild.id != interaction.guild_id:
        await interaction.response.send_message(localized("founday_invalid_channel", lang), ephemeral=True)
        return

    code = (wiki_lang or lang).strip().lower()
    if code not in SUPPORTED_LANGS:
        await interaction.response.send_message(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
            ephemeral=True)
        return

    clean_name = clean_display_name(name, max_len=100)
    db.add_wiki_founday(interaction.guild_id, target.id, wiki_url, clean_name,
                        int(founded_at.timestamp()), hour, minute, code, interaction.user.id)

    reply = localized("founday_added", lang, name=clean_name, url=wiki_url,
                      channel=target.mention, date=founded_at.strftime("%m-%d-%Y %H:%M"),
                      time=f"{hour:02d}:{minute:02d}", lang_name=language_name(code))
    if not _can_manage_webhooks(target):
        reply += "\n" + localized("founday_no_webhook_perm", lang, channel=target.mention)
    await interaction.response.send_message(reply)

@bot.tree.command(name="wiki-foundays", description="list this server's wiki anniversaries (server admins)")
async def wiki_foundays_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_founday_admin(interaction, lang):
        return

    rows = db.get_wiki_foundays(interaction.guild_id)
    if not rows:
        await interaction.response.send_message(localized("founday_list_empty", lang), ephemeral=True)
        return

    lines = [localized("founday_list_header", lang)]
    for row in rows:
        founded = utc_from_epoch(row["founded_at"])
        lines.append(
            f"- **{row['name']}** — {row['url']} · <#{row['channel_id']}> · "
            f"{founded.strftime('%m-%d-%Y %H:%M')} UTC → {row['hour']:02d}:{row['minute']:02d} UTC · "
            f"{language_name(row['lang'])}"
        )
    await interaction.response.send_message(embed=discord.Embed(
        title=localized("founday_list_title", lang),
        description="\n".join(lines)[:4096],
        color=discord.Color(DEFAULT_EMBED_COLOR)), ephemeral=True)

@bot.tree.command(name="wiki-founday-remove", description="stop announcing a wiki's anniversary (server admins)")
@app_commands.describe(url="The wiki link used when it was added")
async def wiki_founday_remove_cmd(interaction: discord.Interaction, url: str):
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_founday_admin(interaction, lang):
        return

    wiki_url = normalize_wiki_url(url)
    if wiki_url is None or not db.delete_wiki_founday(interaction.guild_id, wiki_url):
        await interaction.response.send_message(localized("founday_not_found", lang), ephemeral=True)
        return
    await interaction.response.send_message(localized("founday_removed", lang, url=wiki_url))

def _econ_embed(desc, title=None, color=DEFAULT_EMBED_COLOR):
    embed = discord.Embed(description=desc, color=discord.Color(color))
    if title:
        embed.title = title
    return embed

async def _ereply(interaction, key, lang, *, ephemeral=True, title=None, **kw):
    await _say(interaction, embed=_econ_embed(localized(key, lang, **kw), title=title),
               ephemeral=ephemeral)

def _currency_code_validator(value):
    code = value.strip().upper()
    if economy.CURRENCY_CODE_RE.match(code) and not db.code_taken(code):
        return code
    return None

def _bank_line(bank, lang):
    emoji = f" {bank['emoji']}" if bank["emoji"] else ""
    return f"{bank['currency_name']} [{bank['code']}]{emoji}"

async def _resolve_led_bank(interaction, lang):
    """The bank the caller leads; asks which one when they lead several. Bank
    leadership is global, so this is not scoped to the current server's union."""
    banks = db.user_led_banks("discord", interaction.user.id)
    if not banks:
        await _ereply(interaction, "bank_none_led", lang)
        return None
    if len(banks) == 1:
        return banks[0]
    return await _numbered_choice(interaction, lang, localized("bank_choose", lang),
                                  banks, lambda b: _bank_line(b, lang))

class _LeaderConsentView(ui.View):
    """Accept/decline buttons that any leader of `bank_code` (or a Bot Admin) may
    press — used for treaty and peg agreements between two banks."""

    def __init__(self, lang, bank_code, on_accept, on_decline):
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.lang = lang
        self.bank_code = bank_code
        self._on_accept = on_accept
        self._on_decline = on_decline
        accept = ui.Button(label=localized("consent_accept", lang), style=ButtonStyle.success)
        decline = ui.Button(label=localized("consent_decline", lang), style=ButtonStyle.danger)
        accept.callback = self._make_cb(True)
        decline.callback = self._make_cb(False)
        self.add_item(accept)
        self.add_item(decline)

    def _make_cb(self, accepted):
        async def callback(interaction2: discord.Interaction):
            if not (db.is_bank_leader(self.bank_code, "discord", interaction2.user.id)
                    or is_admin("discord", interaction2.user.id)):
                await interaction2.response.send_message(
                    localized("bank_consent_not_leader", self.lang), ephemeral=True)
                return
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await interaction2.response.edit_message(view=self)
            except Exception:
                pass
            await (self._on_accept if accepted else self._on_decline)(interaction2)
        return callback

@bot.tree.command(name="create-bank", description="found a bank and its currency (bot/server admins)")
async def create_bank_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    if not (is_admin("discord", interaction.user.id) or is_server_admin(interaction)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    id_msg = await _dialog_text(interaction, lang, localized("create_bank_ask_chat", lang),
                                as_embed=True)
    if id_msg is None:
        return
    raw = (id_msg.content or "").strip().lstrip("-")
    if not raw.isdigit():
        await interaction.channel.send(embed=_econ_embed(localized("create_bank_bad_chat", lang)))
        return
    chat = db.get_chat((id_msg.content or "").strip())
    if not chat:
        await interaction.channel.send(embed=_econ_embed(localized("create_bank_chat_not_setup", lang)))
        return
    union = chat["union_code"]

    name_msg = await _dialog_text(interaction, lang, localized("create_bank_ask_name", lang),
                                  as_embed=True)
    if name_msg is None:
        return
    currency_name = clean_display_name(name_msg.content, max_len=40)

    code = await _dialog_text(interaction, lang, localized("create_bank_ask_code", lang),
                              validator=_currency_code_validator, error_key="create_bank_bad_code",
                              as_embed=True)
    if code is None:
        return

    emoji_msg = await _dialog_text(interaction, lang, localized("create_bank_ask_emoji", lang),
                                   as_embed=True)
    if emoji_msg is None:
        return
    parts = (emoji_msg.content or "").strip().split()
    emoji = parts[0][:16] if parts else ""

    db.create_bank(code, union, chat["chat_id"], currency_name, emoji,
                   "discord", interaction.user.id, str(interaction.user))
    await interaction.channel.send(embed=_econ_embed(
        localized("create_bank_created", lang, name=currency_name, code=code,
                  emoji=emoji, union=db.get_union_name(union, lang))))
    await send_service_event("bank_created", name=currency_name, code=code, union=union,
                             user=str(interaction.user))

def _build_bank_embed(bank, lang):
    emoji = f" {bank['emoji']}" if bank["emoji"] else ""
    embed = discord.Embed(title=f"{bank['currency_name']} [{bank['code']}]{emoji}",
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    embed.add_field(name=localized("bank_field_union", lang),
                    value=db.get_union_name(bank["union_code"], lang), inline=True)
    embed.add_field(name=localized("bank_field_central", lang),
                    value=str(bank["central_chat"]), inline=True)
    leaders = db.get_bank_leaders(bank["code"])
    embed.add_field(
        name=localized("bank_field_leaders", lang),
        value=", ".join(format_stored_user("discord", l["platform"], l["user_id"],
                                           l["display_name"]) for l in leaders) or "—",
        inline=False)
    embed.add_field(name=localized("bank_field_supply", lang),
                    value=economy.format_money(db.bank_money_supply(bank["code"]), bank),
                    inline=True)
    embed.add_field(name=localized("bank_field_accounts", lang),
                    value=str(db.bank_account_count(bank["code"])), inline=True)
    debt = db.bank_debt(bank["code"])
    if debt:
        embed.add_field(name=localized("bank_field_debt", lang),
                        value=economy.format_money(debt, bank), inline=True)
    embed.add_field(name=localized("bank_field_value", lang),
                    value=f"{bank['value']:.4f}", inline=True)
    return embed

@bot.tree.command(name="bank", description="information about a bank and its currency")
@app_commands.describe(code="Currency code (4 characters)")
async def bank_cmd(interaction: discord.Interaction, code: str):
    lang = get_chat_lang(_chat_key(interaction))
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    await _say(interaction, embed=_build_bank_embed(bank, lang))

async def _offer_bank_leadership(interaction, bank, lang, target_id, transfer):
    channel = interaction.channel
    name = bank["currency_name"]

    async def on_accept(interaction2):
        display = str(interaction2.user)
        if transfer:
            db.set_bank_leader(bank["code"], "discord", target_id, display)
            text = localized("bank_transfer_done", lang, user=f"<@{target_id}>", name=name)
        else:
            db.add_bank_leader(bank["code"], "discord", target_id, display)
            text = localized("bank_leader_added", lang, user=f"<@{target_id}>", name=name)
        await channel.send(embed=_econ_embed(text))

    async def on_decline(interaction2):
        key = "bank_transfer_declined" if transfer else "bank_leader_declined"
        await channel.send(embed=_econ_embed(localized(key, lang)))

    offer_key = "bank_transfer_offer" if transfer else "bank_leader_offer"
    await _say(interaction, f"<@{target_id}>", embed=_econ_embed(
        localized(offer_key, lang, mention=f"<@{target_id}>", name=name, code=bank["code"])),
        view=_ConsentView(lang, target_id, on_accept, on_decline),
        allowed_mentions=discord.AllowedMentions(users=True))

async def _bank_leadership_cmd(interaction, code, user, transfer):
    lang = get_chat_lang(_chat_key(interaction))
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    caller_is_admin = is_admin("discord", interaction.user.id)
    if not caller_is_admin and not db.is_bank_leader(bank["code"], "discord", interaction.user.id):
        await _ereply(interaction, "bank_not_leader", lang)
        return
    if caller_is_admin:
        try:
            display = str(await bot.fetch_user(target_id))
        except Exception:
            display = str(target_id)
        if transfer:
            db.set_bank_leader(bank["code"], "discord", target_id, display)
            key = "bank_transfer_done"
        else:
            db.add_bank_leader(bank["code"], "discord", target_id, display)
            key = "bank_leader_added"
        await _ereply(interaction, key, lang, ephemeral=False,
                      user=f"<@{target_id}>", name=bank["currency_name"])
        return
    await _offer_bank_leadership(interaction, bank, lang, target_id, transfer)

@bot.tree.command(name="bank-add-leader", description="add a co-leader to a bank (bank leaders / bot admins)")
@app_commands.describe(code="Currency code", user="User ID or mention")
async def bank_add_leader_cmd(interaction: discord.Interaction, code: str, user: str):
    await _bank_leadership_cmd(interaction, code, user, transfer=False)

@bot.tree.command(name="bank-transfer", description="transfer bank leadership (bank leaders / bot admins)")
@app_commands.describe(code="Currency code", user="User ID or mention")
async def bank_transfer_cmd(interaction: discord.Interaction, code: str, user: str):
    await _bank_leadership_cmd(interaction, code, user, transfer=True)

_EDIT_BANK_OPTIONS = ("name", "emoji", "code", "central", "add_leader",
                      "transfer", "delete")

async def _resolve_edit_bank(interaction, lang, query=None):
    """The bank the caller may edit: any of them for a Bot Admin, only one they
    lead otherwise. Without a code it is the single bank they lead, or a
    numbered choice when they lead several."""
    admin = is_admin("discord", interaction.user.id)
    if query:
        bank = db.get_bank(query.strip().upper())
        if not bank:
            await _ereply(interaction, "bank_not_found", lang)
            return None
        if not (admin or db.is_bank_leader(bank["code"], "discord", interaction.user.id)):
            await _ereply(interaction, "edit_bank_no_permission", lang)
            return None
        return bank
    led = [b for b in db.get_all_banks()
           if db.is_bank_leader(b["code"], "discord", interaction.user.id)]
    if not led:
        await _ereply(interaction, "edit_bank_specify" if admin
                      else "edit_bank_no_permission", lang)
        return None
    if len(led) == 1:
        return led[0]
    return await _numbered_choice(interaction, lang,
                                  localized("edit_bank_choose", lang), led,
                                  lambda b: _bank_line(b, lang))

@bot.tree.command(name="edit-bank", description="manage a bank: currency name, emoji, code, central server, leaders")
@app_commands.describe(option="Menu item number",
                       code="Currency code (bot admins, or when you lead several banks)")
async def edit_bank_cmd(interaction: discord.Interaction, option: int = None,
                        code: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    bank = await _resolve_edit_bank(interaction, lang, code)
    if bank is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_BANK_OPTIONS):
        items = [localized(f"edit_bank_opt_{key}", lang) for key in _EDIT_BANK_OPTIONS]
        chosen = await _numbered_choice(
            interaction, lang,
            localized("edit_bank_menu_header", lang, name=bank["currency_name"],
                      code=bank["code"]),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_BANK_OPTIONS[chosen[0]]
    else:
        action = _EDIT_BANK_OPTIONS[option - 1]

    channel = interaction.channel
    if action == "name":
        msg = await _dialog_text(interaction, lang, localized("edit_bank_ask_name", lang),
                                 as_embed=True)
        if msg is None:
            return
        db.update_bank_field(bank["code"], "currency_name",
                             clean_display_name(msg.content, max_len=40))
        await channel.send(embed=_econ_embed(localized("edit_bank_done", lang)))
    elif action == "emoji":
        msg = await _dialog_text(interaction, lang, localized("edit_bank_ask_emoji", lang),
                                 as_embed=True)
        if msg is None:
            return
        parts = (msg.content or "").strip().split()
        db.update_bank_field(bank["code"], "emoji", parts[0][:16] if parts else "")
        await channel.send(embed=_econ_embed(localized("edit_bank_done", lang)))
    elif action == "code":
        new_code = await _dialog_text(interaction, lang, localized("edit_bank_ask_code", lang),
                                      validator=_currency_code_validator,
                                      error_key="edit_bank_bad_code", as_embed=True)
        if new_code is None:
            return
        old_code = bank["code"]
        db.rename_bank_code(old_code, new_code)
        await channel.send(embed=_econ_embed(
            localized("edit_bank_code_changed", lang, old=old_code, code=new_code)))
    elif action == "central":
        msg = await _dialog_text(interaction, lang, localized("edit_bank_ask_central", lang),
                                 as_embed=True)
        if msg is None:
            return
        chat = db.get_chat((msg.content or "").strip())
        if not chat:
            await channel.send(embed=_econ_embed(localized("edit_bank_bad_central", lang)))
            return
        db.set_bank_central_chat(bank["code"], chat["chat_id"], chat["union_code"])
        await channel.send(embed=_econ_embed(localized("edit_bank_done", lang)))
    elif action in ("add_leader", "transfer"):
        msg = await _dialog_text(interaction, lang, localized("edit_bank_ask_leader", lang),
                                 as_embed=True)
        if msg is None:
            return
        target_id = _parse_user_ref((msg.content or "").strip())
        if target_id is None:
            await channel.send(embed=_econ_embed(localized("edit_invalid_user", lang)))
            return
        await _offer_bank_leadership(interaction, bank, lang, target_id,
                                     action == "transfer")
    elif action == "delete":
        msg = await _dialog_text(interaction, lang,
                                 localized("edit_bank_confirm_delete", lang, code=bank["code"]),
                                 as_embed=True)
        if msg is None:
            return
        if (msg.content or "").strip().upper() != bank["code"]:
            await channel.send(embed=_econ_embed(localized("action_cancelled", lang)))
            return
        db.delete_bank(bank["code"])
        await channel.send(embed=_econ_embed(
            localized("edit_bank_deleted", lang, name=bank["currency_name"],
                      code=bank["code"])))
        await send_service_event("bank_deleted", name=bank["currency_name"],
                                 code=bank["code"], user=str(interaction.user))

@bot.tree.command(name="open-account", description="open an account in a bank")
@app_commands.describe(code="Currency code")
async def open_account_cmd(interaction: discord.Interaction, code: str):
    lang = get_chat_lang(_chat_key(interaction))
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    if db.account_exists(bank["code"], *owner):
        await _ereply(interaction, "account_exists", lang, code=bank["code"])
        return
    db.ensure_account(bank["code"], *owner, display_name=str(interaction.user))
    await _ereply(interaction, "account_opened", lang, ephemeral=False,
                  name=bank["currency_name"], code=bank["code"])

@bot.tree.command(name="balance", description="your balance and energy")
@app_commands.describe(code="Currency code (omit for all your accounts)")
async def balance_cmd(interaction: discord.Interaction, code: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    owner = db.canonical_user("discord", interaction.user.id)
    if code:
        bank = db.get_bank(code.strip().upper())
        if not bank:
            await _ereply(interaction, "bank_not_found", lang)
            return
        banks = [bank]
    else:
        banks = [db.get_bank(a["bank_code"]) for a in db.get_owner_accounts(*owner)]
        banks = [b for b in banks if b]
    if not banks:
        await _ereply(interaction, "balance_none", lang)
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
        blocks.append(f"**{b['currency_name']} [{b['code']}]**\n{line}")
    embed = discord.Embed(title=localized("balance_title", lang),
                          description="\n\n".join(blocks)[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="pay", description="send money to another user")
@app_commands.describe(user="Recipient ID or mention", amount="Amount, e.g. 12.34", code="Currency code")
async def pay_cmd(interaction: discord.Interaction, user: str, amount: str, code: str):
    lang = get_chat_lang(_chat_key(interaction))
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    sender = db.canonical_user("discord", interaction.user.id)
    target = db.canonical_user("discord", target_id)
    if target == sender:
        await _ereply(interaction, "pay_self", lang)
        return
    try:
        tdisp = str(await bot.fetch_user(target_id))
    except Exception:
        tdisp = str(target_id)
    if not db.transfer(bank["code"], sender, target, minor, "pay", to_display=tdisp):
        await _ereply(interaction, "pay_insufficient", lang)
        return
    await _ereply(interaction, "pay_done", lang, ephemeral=False,
                  amount=economy.format_money(minor, bank), user=f"<@{target_id}>")

@bot.tree.command(name="set-earn", description="make this channel earn a bank's currency (bank leaders / server admins)")
@app_commands.describe(code="Currency code", action="on | off", rate="Earning multiplier (default 1.0)")
async def set_earn_cmd(interaction: discord.Interaction, code: str, action: str, rate: float = 1.0):
    lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    if not (db.is_bank_leader(bank["code"], "discord", interaction.user.id)
            or is_server_admin(interaction) or is_admin("discord", interaction.user.id)):
        await _ereply(interaction, "set_earn_no_permission", lang)
        return
    chat = db.get_chat(str(interaction.guild_id))
    if not chat or chat["union_code"] != bank["union_code"]:
        await _ereply(interaction, "set_earn_wrong_union", lang)
        return
    action = action.strip().lower()
    if action in ("off", "disable", "0", "false", "no"):
        db.remove_earn_channel("discord", interaction.channel_id)
        await _ereply(interaction, "set_earn_off", lang)
        return
    if action not in ("on", "enable", "1", "true", "yes"):
        await _ereply(interaction, "set_earn_usage", lang)
        return
    r = max(0.0, min(float(rate), 100.0))
    db.set_earn_channel("discord", interaction.channel_id, bank["code"], r)
    await _ereply(interaction, "set_earn_on", lang, code=bank["code"], rate=f"{r:g}")

def _resolve_good_bank(chat, currency):
    """The bank a new good is denominated in: the named one (it must belong to
    the chat's union) or the union's only bank. Returns (bank, error_key)."""
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
    """(category|None, ok). Base-category key, case-insensitive; '-' clears."""
    s = (raw or "").strip().lower()
    if not s or s == "-":
        return None, True
    return (s, True) if s in db.GOOD_CATEGORIES else (None, False)

@bot.tree.command(name="create-good", description="create a good you or your enterprise will produce")
@app_commands.describe(name="Good name", base_value="Base value in the chosen currency, e.g. 5.00",
                       energy_cost="Energy cost per unit", emoji="Optional emoji",
                       category="Base category the good counts toward",
                       currency="Currency code (required when the union has several banks)",
                       enterprise="Enterprise code, when the good belongs to your enterprise")
async def create_good_cmd(interaction: discord.Interaction, name: str, base_value: str,
                          energy_cost: int, emoji: str = None, category: str = None,
                          currency: str = None, enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    chat = db.get_chat(str(interaction.guild_id)) if interaction.guild_id else None
    if not chat:
        await _ereply(interaction, "chat_not_setup", lang)
        return
    bank, err = _resolve_good_bank(chat, currency)
    if err:
        await _ereply(interaction, err, lang)
        return
    cat, ok = _normalize_category(category)
    if not ok:
        await _ereply(interaction, "create_good_bad_category", lang,
                      categories=", ".join(db.GOOD_CATEGORIES))
        return
    owner = db.canonical_user("discord", interaction.user.id)
    if enterprise:
        ent = db.find_enterprise(enterprise)
        if not ent:
            await _ereply(interaction, "enterprise_not_found", lang)
            return
        if not db.is_enterprise_leader(ent["code"], "discord", interaction.user.id):
            await _ereply(interaction, "ent_not_leader", lang)
            return
        owner = db.enterprise_owner(ent["code"])
    base = economy.parse_amount(base_value)
    if base is None or energy_cost <= 0:
        await _ereply(interaction, "create_good_bad", lang)
        return
    nm = clean_display_name(name, max_len=40)
    parts = (emoji or "").strip().split()
    em = parts[0][:16] if parts else ""
    gcode = db.create_good(bank["code"], nm, base, energy_cost, em, owner=owner, category=cat)
    await _ereply(interaction, "create_good_created", lang, ephemeral=False, name=nm, code=gcode,
                  value=economy.format_money(base, bank), energy=economy.format_energy(energy_cost),
                  bank=bank["code"])
    if cat:
        await interaction.channel.send(embed=_econ_embed(
            localized("create_good_category_note", lang, category=category_label(cat, lang))))

def _fmt_duration(seconds):
    seconds = max(int(seconds), 0)
    return f"{seconds // 60}:{seconds % 60:02d}"

def _fmt_eta(seconds):
    """mm:ss for short waits, h:mm:ss once a shipment runs into hours."""
    seconds = max(int(seconds), 0)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"

def _distance_label(distance, lang):
    return localized(f"transport_{distance}", lang)

def _resolve_production_target(platform, user_id, guild_key, enterprise_query):
    """(owner, server, error_key) of a production request: the caller's own
    inventory and the current server — or the enterprise and its home server
    when one is named (the caller must work there)."""
    if enterprise_query:
        ent = db.find_enterprise(enterprise_query)
        if not ent:
            return None, None, "enterprise_not_found"
        if not db.is_enterprise_worker(ent["code"], platform, user_id):
            return None, None, "ent_not_worker"
        return db.enterprise_owner(ent["code"]), (ent["platform"], ent["server_id"]), None
    owner = db.canonical_user(platform, user_id)
    server = (platform, str(guild_key)) if guild_key else (None, None)
    return owner, server, None

@bot.tree.command(name="craft", description="start producing a good — the batch arrives in a few minutes")
@app_commands.describe(good_code="Good code (4 characters)",
                       enterprise="Enterprise code to produce for (its workers only)")
async def craft_cmd(interaction: discord.Interaction, good_code: str, enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    owner, server, err = _resolve_production_target(
        "discord", interaction.user.id, interaction.guild_id, enterprise)
    if err:
        await _ereply(interaction, err, lang)
        return
    worker = db.canonical_user("discord", interaction.user.id)
    notify = ("discord", str(interaction.channel_id))
    status, info = economy.start_production(worker, good, owner, server, notify, lang,
                                            starter_display=str(interaction.user))
    if status == "busy":
        await _ereply(interaction, "production_busy", lang,
                      time=_fmt_duration(info["finish_at"] - int(datetime.now(timezone.utc).timestamp())))
        return
    if status == "no_energy":
        await _ereply(interaction, "craft_no_energy", lang,
                      cost=economy.format_energy(info["cost"]),
                      have=economy.format_energy(info["have"]))
        return
    emoji = f" {good['emoji']}" if good["emoji"] else ""
    await _ereply(interaction, "production_started", lang, ephemeral=False,
                  name=good_display_name(good, lang), emoji=emoji, qty=info["qty"],
                  duration=_fmt_duration(info["duration"]),
                  cost=economy.format_energy(info["cost"]))

@bot.tree.command(name="inventory", description="your crafted goods")
async def inventory_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    owner = db.canonical_user("discord", interaction.user.id)
    inv = db.get_inventory(owner)
    if not inv:
        await _ereply(interaction, "inventory_empty", lang, ephemeral=False)
        return
    lines = []
    for row in inv:
        m = db.get_produced(owner, row["good_code"])
        good = db.get_good(row["good_code"])
        bank = db.get_bank(row["bank_code"])
        val = economy.format_money(economy.unit_value(good, m), bank) if good and bank else "—"
        emoji = f"{row['emoji']} " if row["emoji"] else ""
        lines.append(localized("inventory_line", lang, emoji=emoji,
                               name=good_display_name(good, lang) if good else row["name"],
                               code=row["good_code"], qty=row["qty"], value=val,
                               level=economy.mastery_level(m)))
    embed = discord.Embed(title=localized("inventory_title", lang),
                          description="\n".join(lines)[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="sell", description="sell a good back to its bank")
@app_commands.describe(good_code="Good code", qty="How many (default: all)")
async def sell_cmd(interaction: discord.Interaction, good_code: str, qty: int = None):
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    have = db.get_inventory_qty(owner, good["code"])
    want = qty if (qty and qty > 0) else have
    status, info = economy.sell(owner, good, want)
    if status == "not_sellable":
        await _ereply(interaction, "sell_not_sellable", lang,
                      name=good_display_name(good, lang))
        return
    if status == "nothing":
        await _ereply(interaction, "sell_nothing", lang, name=good["name"])
        return
    bank = db.get_bank(good["bank_code"])
    await _ereply(interaction, "sell_done", lang, ephemeral=False, qty=info["qty"],
                  name=good["name"], unit=economy.format_money(info["unit"], bank),
                  total=economy.format_money(info["total"], bank))

@bot.tree.command(name="autocraft", description="produce a good 24/7, slower than by hand")
@app_commands.describe(good_code="Good code", action="on | off",
                       enterprise="Enterprise code to produce for (its workers only)")
async def autocraft_cmd(interaction: discord.Interaction, good_code: str, action: str,
                        enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    owner, server, err = _resolve_production_target(
        "discord", interaction.user.id, interaction.guild_id, enterprise)
    if err:
        await _ereply(interaction, err, lang)
        return
    worker = db.canonical_user("discord", interaction.user.id)
    on = action.strip().lower() in ("on", "enable", "1", "true", "yes")
    db.set_autocraft(owner, good["code"], on,
                     starter=(worker[1], worker[2]), server=server)
    await _ereply(interaction, "autocraft_on" if on else "autocraft_off", lang,
                  ephemeral=False, name=good_display_name(good, lang))

@bot.tree.command(name="autosend", description="auto-send a share of a good to a user or party")
@app_commands.describe(good_code="Good code", percent="Percent 0-100 (0 to stop)",
                       target="Recipient: a user ID/mention or a party code")
async def autosend_cmd(interaction: discord.Interaction, good_code: str, percent: int, target: str):
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    if percent < 0 or percent > 100:
        await _ereply(interaction, "autosend_bad_percent", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    party = db.get_party(target.strip().upper())
    ent = db.get_enterprise(target.strip().upper())
    if party:
        tgt, tname = db.party_owner(party["code"]), f"{party['name']} [{party['code']}]"
    elif ent:
        tgt, tname = db.enterprise_owner(ent["code"]), f"{ent['name']} [{ent['code']}]"
    else:
        tid = _parse_user_ref(target)
        if tid is None:
            await _ereply(interaction, "autosend_bad_target", lang)
            return
        tgt, tname = db.canonical_user("discord", tid), f"<@{tid}>"
    if percent == 0:
        db.set_autosend(owner, good["code"], 0, tgt)
        await _ereply(interaction, "autosend_off", lang, ephemeral=False, name=good["name"])
        return
    db.set_autosend(owner, good["code"], percent, tgt)
    await _ereply(interaction, "autosend_on", lang, ephemeral=False, name=good["name"],
                  percent=percent, target=tname)

@bot.tree.command(name="set-profession", description="set a member's profession in your bank (bank leaders)")
@app_commands.describe(user="Member ID or mention", good_code="Good code that becomes their profession")
async def set_profession_cmd(interaction: discord.Interaction, user: str, good_code: str):
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    if not (db.is_bank_leader(good["bank_code"], "discord", interaction.user.id)
            or is_admin("discord", interaction.user.id)):
        await _ereply(interaction, "bank_not_leader", lang)
        return
    tid = _parse_user_ref(user)
    if tid is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    db.set_profession(db.canonical_user("discord", tid), good["bank_code"], good["code"])
    await _ereply(interaction, "set_profession_done", lang, ephemeral=False,
                  user=f"<@{tid}>", name=good["name"])

@bot.tree.command(name="fine", description="fine a user in your bank's currency (bank leaders)")
@app_commands.describe(user="User ID or mention", amount="Amount, e.g. 10.00", reason="Optional reason")
async def fine_cmd(interaction: discord.Interaction, user: str, amount: str, reason: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    bank = await _resolve_led_bank(interaction, lang)
    if bank is None:
        return
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    tid = _parse_user_ref(user)
    if tid is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    towner = db.canonical_user("discord", tid)
    db.burn(bank["code"], towner, minor, f"fine: {reason}" if reason else "fine", allow_negative=True)
    await _ereply(interaction, "fine_done", lang, ephemeral=False, user=f"<@{tid}>",
                  amount=economy.format_money(minor, bank),
                  reason=(reason.strip()[:200] if reason else localized("fine_no_reason", lang)))

@bot.tree.command(name="treaty", description="propose a currency-conversion treaty with another bank (bank leaders)")
@app_commands.describe(code="Currency code of the other bank")
async def treaty_cmd(interaction: discord.Interaction, code: str):
    lang = get_chat_lang(_chat_key(interaction))
    other = db.get_bank(code.strip().upper())
    if not other:
        await _ereply(interaction, "bank_not_found", lang)
        return
    mine = await _resolve_led_bank(interaction, lang)
    if mine is None:
        return
    if mine["code"] == other["code"]:
        await _ereply(interaction, "treaty_self", lang)
        return
    if db.has_treaty(mine["code"], other["code"]):
        await _ereply(interaction, "treaty_exists", lang)
        return
    channel = interaction.channel

    async def on_accept(interaction2):
        db.add_treaty(mine["code"], other["code"])
        await channel.send(embed=_econ_embed(
            localized("treaty_done", lang, a=mine["code"], b=other["code"])))

    async def on_decline(interaction2):
        await channel.send(embed=_econ_embed(localized("treaty_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("treaty_offer", lang, proposer=mine["code"], code=other["code"],
                  name=other["currency_name"])),
        view=_LeaderConsentView(lang, other["code"], on_accept, on_decline))

@bot.tree.command(name="set-rate", description="fix a pegged rate with your bank's sole treaty partner (bank leaders)")
@app_commands.describe(code="The partner bank's currency code",
                       rate="Units of the partner currency per 1 unit of yours")
async def set_rate_cmd(interaction: discord.Interaction, code: str, rate: float):
    lang = get_chat_lang(_chat_key(interaction))
    other = db.get_bank(code.strip().upper())
    if not other:
        await _ereply(interaction, "bank_not_found", lang)
        return
    mine = await _resolve_led_bank(interaction, lang)
    if mine is None:
        return
    if not db.has_treaty(mine["code"], other["code"]):
        await _ereply(interaction, "set_rate_no_treaty", lang)
        return
    if not economy.can_manual_peg(mine["code"], other["code"]):
        await _ereply(interaction, "set_rate_multi", lang)
        return
    if rate <= 0:
        await _ereply(interaction, "set_rate_bad", lang)
        return
    channel = interaction.channel

    async def on_accept(interaction2):
        db.set_pegged_rate(mine["code"], other["code"], rate)
        await channel.send(embed=_econ_embed(
            localized("set_rate_done", lang, a=mine["code"], rate=f"{rate:g}", b=other["code"])))

    async def on_decline(interaction2):
        await channel.send(embed=_econ_embed(localized("set_rate_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("set_rate_offer", lang, a=mine["code"], rate=f"{rate:g}", b=other["code"])),
        view=_LeaderConsentView(lang, other["code"], on_accept, on_decline))

@bot.tree.command(name="convert", description="convert between your accounts at the current rate")
@app_commands.describe(amount="Amount in the source currency", from_code="Source currency", to_code="Target currency")
async def convert_cmd(interaction: discord.Interaction, amount: str, from_code: str, to_code: str):
    lang = get_chat_lang(_chat_key(interaction))
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    fb = db.get_bank(from_code.strip().upper())
    tb = db.get_bank(to_code.strip().upper())
    if not fb or not tb:
        await _ereply(interaction, "bank_not_found", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    status, info = economy.convert(owner, fb["code"], tb["code"], minor)
    keymap = {"same": "convert_same", "not_convertible": "convert_not_convertible",
              "too_small": "convert_too_small", "no_funds": "convert_no_funds"}
    if status != "ok":
        await _ereply(interaction, keymap.get(status, "convert_not_convertible"), lang)
        return
    await _ereply(interaction, "convert_done", lang, ephemeral=False,
                  amount=economy.format_money(minor, fb),
                  credited=economy.format_money(info["credited"], tb),
                  rate=f"{info['rate']:.4f}", fee=economy.format_money(info["fee"], fb))

@bot.tree.command(name="rates", description="current currency values and conversion rates")
@app_commands.describe(code="Currency code (omit for all currency values)")
async def rates_cmd(interaction: discord.Interaction, code: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    if code:
        bank = db.get_bank(code.strip().upper())
        if not bank:
            await _ereply(interaction, "bank_not_found", lang)
            return
        partners = db.treaty_partners(bank["code"])
        if not partners:
            await _ereply(interaction, "rates_none", lang, ephemeral=False, code=bank["code"])
            return
        lines = []
        for p in partners:
            r = economy.rate(bank["code"], p)
            treaty = db.get_treaty(bank["code"], p)
            peg = f" {localized('rates_pegged', lang)}" if treaty and treaty["pegged_rate"] is not None else ""
            lines.append(localized("rates_line", lang, a=bank["code"], b=p,
                                   rate=f"{r:.4f}" if r is not None else "—", peg=peg))
        title = localized("rates_title_one", lang, code=bank["code"])
    else:
        banks = db.get_all_banks()
        if not banks:
            await _ereply(interaction, "rates_no_banks", lang, ephemeral=False)
            return
        lines = [localized("rates_value_line", lang, code=b["code"], name=b["currency_name"],
                           value=f"{b['value']:.4f}") for b in banks]
        title = localized("rates_title_all", lang)
    embed = discord.Embed(title=title, description="\n".join(lines)[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed)

@bot.tree.command(name="set-wage", description="set the monthly wage for a profession in your bank (bank leaders)")
@app_commands.describe(profession="Good code or name defining the profession", amount="Monthly wage (0 to clear)")
async def set_wage_cmd(interaction: discord.Interaction, profession: str, amount: str):
    lang = get_chat_lang(_chat_key(interaction))
    bank = await _resolve_led_bank(interaction, lang)
    if bank is None:
        return
    good = db.find_bank_good(bank["code"], profession)
    if not good:
        await _ereply(interaction, "set_wage_no_good", lang, bank=bank["code"])
        return
    if amount.strip() in ("0", "0.0", "0.00"):
        db.set_wage(bank["code"], good["code"], None)
        await _ereply(interaction, "set_wage_cleared", lang, ephemeral=False, name=good["name"])
        return
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    db.set_wage(bank["code"], good["code"], minor)
    await _ereply(interaction, "set_wage_done", lang, ephemeral=False, name=good["name"],
                  amount=economy.format_money(minor, bank))

@bot.tree.command(name="party-dues", description="set your party's monthly member dues (party leaders)")
@app_commands.describe(code="Currency code", amount="Monthly dues per member (0 to clear)")
async def party_dues_cmd(interaction: discord.Interaction, code: str, amount: str):
    lang = get_chat_lang(_chat_key(interaction))
    party = await _resolve_led_party(interaction, lang)
    if party is None:
        return
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    if bank["union_code"] != party["union_code"]:
        await _ereply(interaction, "dues_wrong_union", lang)
        return
    if amount.strip() in ("0", "0.0", "0.00"):
        db.set_party_dues(party["code"], bank["code"], 0)
        await _ereply(interaction, "dues_cleared", lang, ephemeral=False, name=party["name"])
        return
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    db.set_party_dues(party["code"], bank["code"], minor)
    await _ereply(interaction, "dues_done", lang, ephemeral=False, name=party["name"],
                  amount=economy.format_money(minor, bank))

async def _set_channel_cmd(interaction, kind, chat_id, weekday, post_time, tz):
    """Shared body of /setlogs and /settasks: server admins bind (or unbind with
    'off') the channel and optionally tune the schedule."""
    lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return
    if not (is_admin("discord", interaction.user.id) or is_server_admin(interaction)):
        await _ereply(interaction, "no_permission", lang)
        return
    server_id = str(interaction.guild_id)
    if not db.is_setup(server_id):
        await _ereply(interaction, "chat_not_setup", lang)
        return
    if chat_id and chat_id.strip().lower() in ("off", "remove", "disable"):
        db.remove_chat_channel("discord", server_id, kind)
        await _ereply(interaction, f"set{kind}_removed", lang, ephemeral=False)
        return
    raw = (chat_id or str(interaction.channel_id)).strip()
    channel = None
    if raw.isdigit():
        channel = interaction.guild.get_channel(int(raw))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(raw))
            except Exception:
                channel = None
    if channel is None or getattr(channel, "guild", None) is None \
            or channel.guild.id != interaction.guild_id:
        await _ereply(interaction, "setchannel_bad_channel", lang)
        return
    wd = None
    if weekday is not None:
        try:
            wd = parse_weekday(weekday)
        except ValueError:
            await _ereply(interaction, "setchannel_bad_weekday", lang)
            return
    hour = minute = None
    if post_time is not None:
        try:
            hour, minute = parse_founday_time(post_time)
        except ValueError:
            await _ereply(interaction, "setchannel_bad_time", lang)
            return
    offset = None
    if tz is not None:
        try:
            offset = parse_tz_offset(tz)
        except ValueError:
            await _ereply(interaction, "setchannel_bad_tz", lang)
            return
    db.set_chat_channel("discord", server_id, kind, channel.id,
                        weekday=wd, hour=hour, minute=minute, tz_offset=offset)
    row = db.get_chat_channel("discord", server_id, kind)
    when = f"{row['hour']:02d}:{row['minute']:02d}"
    tz_disp = format_tz_offset(row["tz_offset"])
    if kind == "logs":
        schedule = localized("setlogs_schedule", lang,
                             weekday=weekday_name(row["weekday"], lang),
                             time=when, tz=tz_disp)
    else:
        schedule = localized("settasks_schedule", lang, time=when, tz=tz_disp)
    await _ereply(interaction, f"set{kind}_done", lang, ephemeral=False,
                  channel=f"<#{channel.id}>", schedule=schedule)

@bot.tree.command(name="setlogs", description="set this server's economic log channel (server admins)")
@app_commands.describe(chat_id="Channel ID (default: this channel; 'off' unbinds)",
                       weekday="Weekly statistics day: 1-7 (1 = Monday) or a name",
                       post_time="Weekly statistics time, HH:MM",
                       tz="Timezone as a UTC offset, e.g. UTC+3")
async def setlogs_cmd(interaction: discord.Interaction, chat_id: str = None,
                      weekday: str = None, post_time: str = None, tz: str = None):
    await _set_channel_cmd(interaction, "logs", chat_id, weekday, post_time, tz)

@bot.tree.command(name="settasks", description="set this server's monthly-task channel (server admins)")
@app_commands.describe(chat_id="Channel ID (default: this channel; 'off' unbinds)",
                       post_time="Posting time on the 1st, HH:MM",
                       tz="Timezone as a UTC offset, e.g. UTC+3")
async def settasks_cmd(interaction: discord.Interaction, chat_id: str = None,
                       post_time: str = None, tz: str = None):
    await _set_channel_cmd(interaction, "tasks", chat_id, None, post_time, tz)

class _EntConsentView(ui.View):
    """Accept/decline buttons that any leader of `ent_code` (or a Bot Admin) may
    press — join requests and priced export offers."""

    def __init__(self, lang, ent_code, on_accept, on_decline):
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.lang = lang
        self.ent_code = ent_code
        self._on_accept = on_accept
        self._on_decline = on_decline
        accept = ui.Button(label=localized("consent_accept", lang), style=ButtonStyle.success)
        decline = ui.Button(label=localized("consent_decline", lang), style=ButtonStyle.danger)
        accept.callback = self._make_cb(True)
        decline.callback = self._make_cb(False)
        self.add_item(accept)
        self.add_item(decline)

    def _make_cb(self, accepted):
        async def callback(interaction2: discord.Interaction):
            if not (db.is_enterprise_leader(self.ent_code, "discord", interaction2.user.id)
                    or is_admin("discord", interaction2.user.id)):
                await interaction2.response.send_message(
                    localized("ent_consent_not_leader", self.lang), ephemeral=True)
                return
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await interaction2.response.edit_message(view=self)
            except Exception:
                pass
            await (self._on_accept if accepted else self._on_decline)(interaction2)
        return callback

def _ent_line(ent):
    return f"{ent['name']} [{ent['code']}]"

async def _resolve_led_enterprise(interaction, lang, query=None):
    """The enterprise the caller leads: the named one, the only one, or a
    numbered choice among several."""
    if query:
        ent = db.find_enterprise(query)
        if not ent:
            await _ereply(interaction, "enterprise_not_found", lang)
            return None
        if not (db.is_enterprise_leader(ent["code"], "discord", interaction.user.id)
                or is_admin("discord", interaction.user.id)):
            await _ereply(interaction, "ent_not_leader", lang)
            return None
        return ent
    ents = db.user_led_enterprises("discord", interaction.user.id)
    if not ents:
        await _ereply(interaction, "ent_none_led", lang)
        return None
    if len(ents) == 1:
        return ents[0]
    return await _numbered_choice(interaction, lang, localized("ent_choose", lang),
                                  ents, _ent_line)

@bot.tree.command(name="add-enterprise", description="found an enterprise on this server (interactive dialog)")
async def add_enterprise_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return
    if not db.is_setup(str(interaction.guild_id)):
        await _ereply(interaction, "chat_not_setup", lang)
        return
    if not rate_limit_ok(f"addent|discord|{interaction.user.id}", 3, 86400):
        await _ereply(interaction, "rate_limited", lang)
        return
    name_msg = await _dialog_text(interaction, lang, localized("add_ent_ask_name", lang),
                                  as_embed=True)
    if name_msg is None:
        return
    name = clean_display_name(name_msg.content, max_len=60)
    code = await _dialog_text(interaction, lang, localized("add_ent_ask_code", lang),
                              validator=_party_code_validator, error_key="add_ent_bad_code",
                              as_embed=True)
    if code is None:
        return
    desc_msg = await _dialog_text(interaction, lang, localized("add_ent_ask_desc", lang),
                                  as_embed=True)
    if desc_msg is None:
        return
    description = (desc_msg.content or "").strip()
    description = None if description == "-" else description[:500]
    await interaction.channel.send(embed=_econ_embed(localized("add_ent_ask_logo", lang)))
    logo = logo_mime = None
    for _ in range(5):
        msg = await _wait_message(interaction.channel_id, interaction.user.id)
        if msg is None:
            await interaction.channel.send(embed=_econ_embed(localized("dialog_timeout", lang)))
            return
        if (msg.content or "").strip() == "-":
            break
        got = await _extract_logo(msg)
        if got:
            logo, logo_mime = got
            break
        await interaction.channel.send(embed=_econ_embed(localized("add_party_invalid_logo", lang)))
    db.create_enterprise(code, name, "discord", interaction.guild_id,
                         "discord", interaction.user.id, str(interaction.user),
                         description=description, logo=logo, logo_mime=logo_mime)
    await interaction.channel.send(embed=_econ_embed(
        localized("add_ent_created", lang, name=name, code=code)))
    await send_service_event("enterprise_created", name=name, code=code,
                             user=str(interaction.user))

def _build_enterprise_embed(ent, lang, viewer_platform="discord"):
    embed = discord.Embed(title=f"{ent['name']} [{ent['code']}]",
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    if ent["description"]:
        embed.description = ent["description"][:2000]
    guild = bot.get_guild(int(ent["server_id"])) if ent["platform"] == "discord" else None
    embed.add_field(name=localized("ent_field_server", lang),
                    value=(guild.name if guild else str(ent["server_id"])), inline=True)
    leaders = db.get_enterprise_leaders(ent["code"])
    embed.add_field(
        name=localized("ent_field_leaders", lang),
        value=", ".join(format_stored_user(viewer_platform, l["platform"], l["user_id"],
                                           l["display_name"]) for l in leaders) or "—",
        inline=False)
    members = db.get_enterprise_members(ent["code"])
    if members:
        lines = []
        for m in members[:15]:
            pos = f" — {m['position']}" if m["position"] else ""
            lines.append(format_stored_user(viewer_platform, m["platform"], m["user_id"],
                                            m["display_name"]) + pos)
        more = len(members) - 15
        if more > 0:
            lines.append(f"… +{more}")
        embed.add_field(name=localized("ent_field_workers", lang, count=len(members)),
                        value="\n".join(lines)[:1024], inline=False)
    balances = []
    for acc in db.get_owner_accounts(*db.enterprise_owner(ent["code"])):
        bank = db.get_bank(acc["bank_code"])
        if bank:
            balances.append(economy.format_money(acc["balance"], bank))
    if balances:
        embed.add_field(name=localized("ent_field_balance", lang),
                        value="\n".join(balances)[:1024], inline=True)
    period = localized(f"salary_period_{ent['salary_period'] or 'monthly'}", lang)
    salary_bank = ent["salary_bank"] or "—"
    embed.add_field(name=localized("ent_field_salary", lang),
                    value=f"{period} · {salary_bank}", inline=True)
    positions = db.get_enterprise_positions(ent["code"])
    if positions:
        plines = []
        for p in positions:
            if p["percent"] is not None:
                sal = localized("salary_percent", lang, percent=f"{p['percent']:g}")
            else:
                sal = economy.format_amount(p["amount"] or 0)
            plines.append(f"{p['position']}: {sal}")
        embed.add_field(name=localized("ent_field_positions", lang),
                        value="\n".join(plines)[:1024], inline=False)
    inv = db.get_inventory(db.enterprise_owner(ent["code"]))
    if inv:
        glines = []
        for row in inv[:8]:
            emoji = f"{row['emoji']} " if row["emoji"] else ""
            good = db.get_good(row["good_code"])
            nm = good_display_name(good, lang) if good else row["name"]
            glines.append(f"{emoji}{nm} [{row['good_code']}] ×{row['qty']}")
        embed.add_field(name=localized("ent_field_goods", lang),
                        value="\n".join(glines)[:1024], inline=False)
    transit = db.get_enterprise_shipments(ent["code"])
    if transit:
        embed.add_field(name=localized("ent_field_transit", lang, count=len(transit)),
                        value="\n".join(_transit_lines(ent["code"], transit[:8], lang))[:1024],
                        inline=False)
    return embed

def _transit_lines(ent_code, shipments, lang):
    """Render in-transit shipments from the point of view of `ent_code`: a ➡️
    line for cargo it is sending, a ⬅️ line for cargo coming in."""
    now = int(datetime.now(timezone.utc).timestamp())
    lines = []
    for s in shipments:
        good = db.get_good(s["good_code"])
        emoji = f"{good['emoji']} " if good and good["emoji"] else ""
        name = good_display_name(good, lang) if good else s["good_code"]
        eta = _fmt_eta(max(int(s["arrive_at"]) - now, 0))
        if s["from_ent"] == ent_code:
            other = db.get_enterprise(s["to_ent"])
            key, label = "transit_line_out", (_ent_line(other) if other else s["to_ent"])
        else:
            other = db.get_enterprise(s["from_ent"])
            key, label = "transit_line_in", (_ent_line(other) if other else s["from_ent"])
        lines.append(localized(key, lang, emoji=emoji, name=name, qty=s["qty"],
                               other=label, eta=eta))
    return lines

@bot.tree.command(name="enterprise", description="an enterprise's card, or this server's enterprises")
@app_commands.describe(query="Enterprise code or name (omit to list this server's enterprises)")
async def enterprise_cmd(interaction: discord.Interaction, query: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    if not query:
        ents = db.get_server_enterprises("discord", str(interaction.guild_id)) \
            if interaction.guild_id else []
        if not ents:
            await _ereply(interaction, "ent_list_empty", lang)
            return
        lines = [_ent_line(e) for e in ents]
        await _say(interaction, embed=_econ_embed("\n".join(lines)[:4000],
                                                  title=localized("ent_list_header", lang)))
        return
    ent = db.find_enterprise(query)
    if not ent:
        await _ereply(interaction, "enterprise_not_found", lang)
        return
    embed = _build_enterprise_embed(ent, lang)
    if ent["logo"]:
        ext = _LOGO_EXT.get(ent["logo_mime"], "png")
        file = discord.File(io.BytesIO(ent["logo"]), filename=f"logo.{ext}")
        embed.set_thumbnail(url=f"attachment://logo.{ext}")
        await _say(interaction, embed=embed, file=file)
    else:
        await _say(interaction, embed=embed)

_EDIT_ENT_OPTIONS = ("name", "description", "logo", "add_leader", "transfer",
                     "salary_bank", "salary_period", "delete")

@bot.tree.command(name="edit-enterprise", description="manage your enterprise (founder and leaders)")
@app_commands.describe(option="Menu item number", enterprise="Enterprise code (when you lead several)")
async def edit_enterprise_cmd(interaction: discord.Interaction, option: int = None,
                              enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_ENT_OPTIONS):
        items = [localized(f"edit_ent_opt_{key}", lang) for key in _EDIT_ENT_OPTIONS]
        chosen = await _numbered_choice(
            interaction, lang,
            localized("edit_ent_menu_header", lang, name=ent["name"]),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_ENT_OPTIONS[chosen[0]]
    else:
        action = _EDIT_ENT_OPTIONS[option - 1]

    if action == "name":
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_name", lang),
                                 as_embed=True)
        if msg is None:
            return
        db.update_enterprise_field(ent["code"], "name",
                                   clean_display_name(msg.content, max_len=60))
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action == "description":
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_desc", lang),
                                 as_embed=True)
        if msg is None:
            return
        text = (msg.content or "").strip()
        db.update_enterprise_field(ent["code"], "description",
                                   None if text == "-" else text[:500])
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action == "logo":
        logo = await _dialog_logo(interaction, lang, localized("edit_ent_ask_logo", lang))
        if logo is None:
            return
        db.update_enterprise_logo(ent["code"], logo[0], logo[1])
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action in ("add_leader", "transfer"):
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_leader", lang),
                                 as_embed=True)
        if msg is None:
            return
        target_id = _parse_user_ref((msg.content or "").strip())
        if target_id is None:
            await interaction.channel.send(embed=_econ_embed(localized("edit_invalid_user", lang)))
            return
        transfer = action == "transfer"
        channel = interaction.channel

        async def on_accept(interaction2):
            display = str(interaction2.user)
            if transfer:
                db.set_enterprise_leader(ent["code"], "discord", target_id, display)
                key = "ent_transfer_done"
            else:
                db.add_enterprise_leader(ent["code"], "discord", target_id, display)
                key = "ent_leader_added"
            await channel.send(embed=_econ_embed(
                localized(key, lang, user=f"<@{target_id}>", name=ent["name"])))

        async def on_decline(interaction2):
            key = "ent_transfer_declined" if transfer else "ent_leader_declined"
            await channel.send(embed=_econ_embed(localized(key, lang)))

        offer_key = "ent_transfer_offer" if transfer else "ent_leader_offer"
        await channel.send(
            f"<@{target_id}>",
            embed=_econ_embed(localized(offer_key, lang, mention=f"<@{target_id}>",
                                        name=ent["name"], code=ent["code"])),
            view=_ConsentView(lang, target_id, on_accept, on_decline),
            allowed_mentions=discord.AllowedMentions(users=True))
    elif action == "salary_bank":
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_salary_bank", lang),
                                 as_embed=True)
        if msg is None:
            return
        bank = db.get_bank((msg.content or "").strip().upper())
        if not bank:
            await interaction.channel.send(embed=_econ_embed(localized("bank_not_found", lang)))
            return
        db.update_enterprise_field(ent["code"], "salary_bank", bank["code"])
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action == "salary_period":
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_salary_period", lang),
                                 as_embed=True)
        if msg is None:
            return
        period = (msg.content or "").strip().lower()
        if period not in ("weekly", "monthly"):
            await interaction.channel.send(embed=_econ_embed(localized("edit_ent_bad_value", lang)))
            return
        db.update_enterprise_field(ent["code"], "salary_period", period)
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action == "delete":
        msg = await _dialog_text(interaction, lang,
                                 localized("edit_ent_confirm_delete", lang, code=ent["code"]),
                                 as_embed=True)
        if msg is None:
            return
        if (msg.content or "").strip().upper() != ent["code"]:
            await interaction.channel.send(embed=_econ_embed(localized("action_cancelled", lang)))
            return
        db.delete_enterprise(ent["code"])
        await interaction.channel.send(embed=_econ_embed(
            localized("edit_ent_deleted", lang, name=ent["name"])))
        await send_service_event("enterprise_deleted", name=ent["name"], code=ent["code"],
                                 user=str(interaction.user))

@bot.tree.command(name="ent-join", description="ask to join an enterprise (its leaders approve)")
@app_commands.describe(code="Enterprise code or name")
async def ent_join_cmd(interaction: discord.Interaction, code: str):
    lang = get_chat_lang(_chat_key(interaction))
    ent = db.find_enterprise(code)
    if not ent:
        await _ereply(interaction, "enterprise_not_found", lang)
        return
    if db.is_enterprise_worker(ent["code"], "discord", interaction.user.id):
        await _ereply(interaction, "ent_join_already", lang)
        return
    requester_id = interaction.user.id
    requester_name = str(interaction.user)
    channel = interaction.channel

    async def on_accept(interaction2):
        db.add_enterprise_member(ent["code"], "discord", requester_id, requester_name)
        await channel.send(embed=_econ_embed(
            localized("ent_join_approved", lang, user=f"<@{requester_id}>", name=ent["name"])))

    async def on_decline(interaction2):
        await channel.send(embed=_econ_embed(localized("ent_join_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("ent_join_request", lang, user=f"<@{requester_id}>",
                  name=ent["name"], code=ent["code"])),
        view=_EntConsentView(lang, ent["code"], on_accept, on_decline))

@bot.tree.command(name="ent-leave", description="leave an enterprise you work at")
@app_commands.describe(code="Enterprise code (when you work at several)")
async def ent_leave_cmd(interaction: discord.Interaction, code: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    ents = db.user_member_enterprises("discord", interaction.user.id)
    if code:
        ents = [e for e in ents if e["code"].lower() == code.strip().lower()
                or e["name"].lower() == code.strip().lower()]
    if not ents:
        await _ereply(interaction, "ent_leave_none", lang)
        return
    ent = ents[0]
    if len(ents) > 1:
        ent = await _numbered_choice(interaction, lang, localized("ent_choose", lang),
                                     ents, _ent_line)
        if ent is None:
            return
    db.remove_enterprise_member(ent["code"], "discord", interaction.user.id)
    await _ereply(interaction, "ent_leave_done", lang, ephemeral=False, name=ent["name"])

@bot.tree.command(name="ent-kick", description="remove a worker from your enterprise (leaders)")
@app_commands.describe(user="Worker ID or mention", enterprise="Enterprise code (when you lead several)")
async def ent_kick_cmd(interaction: discord.Interaction, user: str, enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    if not db.remove_enterprise_member(ent["code"], "discord", target_id):
        await _ereply(interaction, "ent_kick_not_member", lang)
        return
    await _ereply(interaction, "ent_kick_done", lang, ephemeral=False,
                  user=f"<@{target_id}>", name=ent["name"])

def _salary_display(kind, value, lang):
    if kind == "percent":
        return localized("salary_percent", lang, percent=f"{value:g}")
    return economy.format_amount(value)

@bot.tree.command(name="ent-position", description="create a position and its salary in your enterprise (leaders)")
@app_commands.describe(position="Position name", salary="Amount (12.34), percent of sales (5%), or 0 to remove",
                       enterprise="Enterprise code (when you lead several)")
async def ent_position_cmd(interaction: discord.Interaction, position: str, salary: str,
                           enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    parsed = economy.parse_salary(salary)
    if parsed is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    kind, value = parsed
    pos = clean_display_name(position, max_len=40)
    if kind == "clear":
        db.set_position_salary(ent["code"], pos, None, None)
        await _ereply(interaction, "ent_position_cleared", lang, ephemeral=False, position=pos)
        return
    db.set_position_salary(ent["code"], pos,
                           value if kind == "amount" else None,
                           value if kind == "percent" else None)
    await _ereply(interaction, "ent_position_done", lang, ephemeral=False, position=pos,
                  salary=_salary_display(kind, value, lang))

@bot.tree.command(name="ent-assign", description="assign a worker to a position (leaders)")
@app_commands.describe(user="Worker ID or mention", position="Position name ('-' clears)",
                       enterprise="Enterprise code (when you lead several)")
async def ent_assign_cmd(interaction: discord.Interaction, user: str, position: str,
                         enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    if not db.get_enterprise_member(ent["code"], "discord", target_id):
        await _ereply(interaction, "ent_kick_not_member", lang)
        return
    pos = clean_display_name(position, max_len=40)
    if pos == "-":
        db.set_member_position(ent["code"], "discord", target_id, None)
        await _ereply(interaction, "ent_assign_cleared", lang, ephemeral=False,
                      user=f"<@{target_id}>")
        return
    db.set_member_position(ent["code"], "discord", target_id, pos)
    await _ereply(interaction, "ent_assign_done", lang, ephemeral=False,
                  user=f"<@{target_id}>", position=pos)

@bot.tree.command(name="ent-salary", description="set a worker's personal salary (leaders)")
@app_commands.describe(user="Worker ID or mention", salary="Amount (12.34), percent of sales (5%), or 0 to remove",
                       enterprise="Enterprise code (when you lead several)")
async def ent_salary_cmd(interaction: discord.Interaction, user: str, salary: str,
                         enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    if not db.is_enterprise_worker(ent["code"], "discord", target_id):
        await _ereply(interaction, "ent_kick_not_member", lang)
        return
    parsed = economy.parse_salary(salary)
    if parsed is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    kind, value = parsed
    if kind == "clear":
        db.set_personal_salary(ent["code"], "discord", target_id, None, None)
        await _ereply(interaction, "ent_salary_cleared", lang, ephemeral=False,
                      user=f"<@{target_id}>")
        return
    db.set_personal_salary(ent["code"], "discord", target_id,
                           value if kind == "amount" else None,
                           value if kind == "percent" else None)
    await _ereply(interaction, "ent_salary_done", lang, ephemeral=False,
                  user=f"<@{target_id}>", salary=_salary_display(kind, value, lang))

@bot.tree.command(name="ent-sell", description="sell your enterprise's goods to the bank (leaders)")
@app_commands.describe(good_code="Good code", qty="How many (default: all)",
                       enterprise="Enterprise code (when you lead several)")
async def ent_sell_cmd(interaction: discord.Interaction, good_code: str, qty: int = None,
                       enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    owner = db.enterprise_owner(ent["code"])
    have = db.get_inventory_qty(owner, good["code"])
    want = qty if (qty and qty > 0) else have
    status, info = economy.sell(owner, good, want)
    if status == "not_sellable":
        await _ereply(interaction, "sell_not_sellable", lang,
                      name=good_display_name(good, lang))
        return
    if status == "nothing":
        await _ereply(interaction, "sell_nothing", lang, name=good["name"])
        return
    bank = db.get_bank(good["bank_code"])
    await _ereply(interaction, "sell_done", lang, ephemeral=False, qty=info["qty"],
                  name=good["name"], unit=economy.format_money(info["unit"], bank),
                  total=economy.format_money(info["total"], bank))

async def _run_export(interaction, lang, source, target, good, qty, price, bank):
    """Offer a one-off export between two enterprises. A leader of the receiving
    enterprise must always accept — free or priced — before the shipment leaves.
    Once accepted, the cargo travels and arrives after its transit time."""
    channel = interaction.channel
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    name = good_display_name(good, lang)
    notify = ("discord", str(interaction.channel_id))
    price_disp = economy.format_money(price, bank) if price else localized("export_free", lang)

    async def on_accept(interaction2):
        status, info = economy.dispatch_shipment(
            source["code"], target["code"], good, qty, price,
            bank["code"] if bank else None, notify, lang)
        if status == "ok":
            text = localized("export_dispatched", lang, qty=info["qty"], emoji=emoji, name=name,
                             source=_ent_line(source), target=_ent_line(target),
                             eta=_fmt_eta(info["duration"]),
                             distance=_distance_label(info["distance"], lang))
        else:
            text = localized(f"export_{status}", lang)
        await channel.send(embed=_econ_embed(text))

    async def on_decline(interaction2):
        await channel.send(embed=_econ_embed(localized("export_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("export_offer", lang, source=_ent_line(source), target=_ent_line(target),
                  qty=qty, emoji=emoji, name=name, price=price_disp)),
        view=_EntConsentView(lang, target["code"], on_accept, on_decline))

def _resolve_export_args(lang, target_query, price_s, currency, source):
    """(target, price, bank, error_key_or_None) shared by /export and /auto-export."""
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

@bot.tree.command(name="export", description="export your enterprise's goods to another enterprise (leaders)")
@app_commands.describe(good_code="Good code", qty="How many units",
                       target="Receiving enterprise code or name",
                       price="Total price (the buyer pays on acceptance)",
                       currency="Currency code of the price",
                       enterprise="Your enterprise code (when you lead several)")
async def export_cmd(interaction: discord.Interaction, good_code: str, qty: int, target: str,
                     price: str = None, currency: str = None, enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    source = await _resolve_led_enterprise(interaction, lang, enterprise)
    if source is None:
        return
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    if qty <= 0:
        await _ereply(interaction, "bad_amount", lang)
        return
    tgt, price_minor, bank, err = _resolve_export_args(lang, target, price, currency, source)
    if err:
        await _ereply(interaction, err, lang)
        return
    await _run_export(interaction, lang, source, tgt, good, qty, price_minor, bank)

@bot.tree.command(name="auto-export", description="export goods to another enterprise every week (leaders)")
@app_commands.describe(good_code="Good code", qty="Units per week, or 'off' to cancel",
                       target="Receiving enterprise code or name",
                       price="Total price per delivery", currency="Currency code of the price",
                       enterprise="Your enterprise code (when you lead several)")
async def auto_export_cmd(interaction: discord.Interaction, good_code: str, qty: str, target: str,
                          price: str = None, currency: str = None, enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    source = await _resolve_led_enterprise(interaction, lang, enterprise)
    if source is None:
        return
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    tgt = db.find_enterprise(target)
    if not tgt:
        await _ereply(interaction, "export_bad_target", lang)
        return
    if qty.strip().lower() in ("off", "0", "stop"):
        db.remove_auto_export(source["code"], tgt["code"], good["code"])
        await _ereply(interaction, "auto_export_removed", lang, ephemeral=False,
                      name=good_display_name(good, lang), target=_ent_line(tgt))
        return
    if not qty.strip().isdigit() or int(qty) <= 0:
        await _ereply(interaction, "bad_amount", lang)
        return
    qty_n = int(qty)
    tgt, price_minor, bank, err = _resolve_export_args(lang, target, price, currency, source)
    if err:
        await _ereply(interaction, err, lang)
        return
    channel = interaction.channel
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    name = good_display_name(good, lang)
    price_disp = economy.format_money(price_minor, bank) if price_minor \
        else localized("export_free", lang)

    async def on_accept(interaction2):
        db.remove_auto_export(source["code"], tgt["code"], good["code"])
        db.add_auto_export(source["code"], tgt["code"], good["code"], qty_n, price_minor,
                           bank["code"] if bank else None, interaction.user.id)
        await channel.send(embed=_econ_embed(
            localized("auto_export_set", lang, qty=qty_n, emoji=emoji, name=name,
                      target=_ent_line(tgt), price=price_disp)))

    async def on_decline(interaction2):
        await channel.send(embed=_econ_embed(localized("export_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("auto_export_offer", lang, source=_ent_line(source), target=_ent_line(tgt),
                  qty=qty_n, emoji=emoji, name=name, price=price_disp)),
        view=_EntConsentView(lang, tgt["code"], on_accept, on_decline))

@bot.tree.command(name="transit", description="an enterprise's goods in transit")
@app_commands.describe(enterprise="Enterprise code or name (omit for the one you lead)")
async def transit_cmd(interaction: discord.Interaction, enterprise: str = None):
    lang = get_chat_lang(_chat_key(interaction))
    if enterprise:
        ent = db.find_enterprise(enterprise)
        if not ent:
            await _ereply(interaction, "enterprise_not_found", lang)
            return
    else:
        ent = await _resolve_led_enterprise(interaction, lang)
        if ent is None:
            return
    shipments = db.get_enterprise_shipments(ent["code"])
    if not shipments:
        await _ereply(interaction, "transit_empty", lang, name=ent["name"])
        return
    embed = discord.Embed(
        title=localized("transit_title", lang, name=ent["name"], code=ent["code"]),
        description="\n".join(_transit_lines(ent["code"], shipments, lang))[:4000],
        color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed)

@bot.tree.command(name="give-good", description="hand goods from your inventory to another user")
@app_commands.describe(user="Recipient ID or mention", good_code="Good code", qty="How many (default 1)")
async def give_good_cmd(interaction: discord.Interaction, user: str, good_code: str, qty: int = 1):
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    target = db.canonical_user("discord", target_id)
    if target == owner:
        await _ereply(interaction, "give_self", lang)
        return
    if qty <= 0:
        await _ereply(interaction, "bad_amount", lang)
        return
    have = db.get_inventory_qty(owner, good["code"])
    if have < qty:
        await _ereply(interaction, "give_no_goods", lang, have=have)
        return
    db.add_inventory(owner, good["code"], -qty)
    db.add_inventory(target, good["code"], qty)
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    await _ereply(interaction, "give_done", lang, ephemeral=False, qty=qty, emoji=emoji,
                  name=good_display_name(good, lang), user=f"<@{target_id}>")

@bot.tree.command(name="goods", description="the goods this server can produce")
async def goods_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(_chat_key(interaction))
    lines = []
    for code, cat, emoji, _name in db.BASE_GOODS:
        good = db.get_good(code)
        if not good:
            continue
        lines.append(localized("goods_line_base", lang, emoji=emoji,
                               name=good_display_name(good, lang), code=code,
                               energy=economy.format_energy(good["energy_cost"])))
    chat = db.get_chat(str(interaction.guild_id)) if interaction.guild_id else None
    if chat:
        for bank in db.get_banks_in_union(chat["union_code"]):
            for good in db.get_bank_goods(bank["code"]):
                if db.is_base_good(good["code"]):
                    continue
                emoji = f"{good['emoji']} " if good["emoji"] else ""
                cat_disp = category_label(good["category"], lang) if good["category"] else "—"
                lines.append(localized("goods_line", lang, emoji=emoji, name=good["name"],
                                       code=good["code"], category=cat_disp,
                                       value=economy.format_money(good["base_value"], bank),
                                       energy=economy.format_energy(good["energy_cost"])))
    if not lines:
        await _ereply(interaction, "goods_none", lang)
        return
    embed = discord.Embed(title=localized("goods_header", lang),
                          description="\n".join(lines)[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed)

# ── Olympiad ────────────────────────────────────────────────────────────────
# /setolympiad opens the event and is the only Olympiad command that always
# exists; the five below it are added to the command tree when an Olympiad is
# running and taken off it again when the voting period ends, so people never
# see a command they cannot use. Discord distributes a tree change to clients on
# its own schedule, which is why /setolympiad says the list may take a while.
#
# The review and the accepted-vote chats always live on Discord, so the embeds
# and the two reviewer commands are here even for votes cast on Telegram; the
# Telegram bot calls post_olympiad_vote for its own voters.

def olympiad_help_keys():
    """The Olympiad help lines to show right now."""
    if not olympiad.is_open():
        return ["help_setolympiad"]
    return ["help_setolympiad", "help_setcontest", "help_editcontest",
            "help_olympiad", "help_accepted", "help_denied"]

def _apply_olympiad_commands():
    """Attach the gated commands to the tree, or take them off it, to match
    whether an Olympiad is running. Returns True when the tree actually
    changed — only then is a sync with Discord worth its rate limit."""
    want = olympiad.is_open()
    have = bot.tree.get_command("olympiad") is not None
    if want == have:
        return False
    for cmd in OLYMPIAD_COMMANDS:
        if want:
            bot.tree.add_command(cmd, override=True)
        else:
            bot.tree.remove_command(cmd.name)
    return True

async def sync_olympiad_commands():
    """Bring the published command list in line with the Olympiad's state."""
    if not _apply_olympiad_commands():
        return
    try:
        await bot.tree.sync()
    except Exception as e:
        logger.warning("Olympiad command sync failed: %s", e)

def _oly_lang(interaction):
    return get_chat_lang(_chat_key(interaction))

def _review_lang(contest):
    """The language the review chat is addressed in: the contest's own, or the
    default one for a cross-language contest."""
    return contest["lang"] if contest["lang"] in SUPPORTED_LANGS else DEFAULT_LANG

async def _olympiad_channel(chat_id):
    """The Discord channel a contest posts to, or None when it is unreachable."""
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return None
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except Exception:
            channel = None
    return channel

async def _dm_channel(interaction: discord.Interaction):
    channel = interaction.channel
    if channel is None:
        channel = interaction.user.dm_channel or await interaction.user.create_dm()
    return channel

async def _wait_reply_or_button(channel_id, user_id, view, timeout=DIALOG_TIMEOUT):
    """Race the caller's next message against a press on `view`. Returns
    ('text', message), ('button', value) or ('timeout', None)."""
    def check(m):
        return m.author.id == user_id and m.channel.id == channel_id
    msg_task = asyncio.ensure_future(bot.wait_for("message", check=check))
    btn_task = asyncio.ensure_future(view.wait_value())
    done, pending = await asyncio.wait({msg_task, btn_task}, timeout=timeout,
                                       return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if btn_task in done:
        return "button", btn_task.result()
    if msg_task in done:
        try:
            return "text", msg_task.result()
        except Exception:
            return "timeout", None
    return "timeout", None

class _OlyDialog:
    """One Olympiad conversation on Discord.

    Every question carries a Stop button, so the person can walk away from the
    dialog at any point; `ask` then returns None, exactly as it does when they
    simply stop replying. Extra buttons ("Another way", "One point each") ride
    on the same row and come back as ('button', value)."""

    def __init__(self, interaction, lang, channel):
        self.interaction = interaction
        self.lang = lang
        self.channel = channel
        self.user_id = interaction.user.id
        self.author = str(interaction.user)

    async def send(self, text, view=None):
        """A dialog message, through the interaction the first time and into the
        channel after that. `view` is left out of the call entirely when there is
        none: `InteractionResponse.send_message` tests it against its own MISSING
        sentinel, so an explicit view=None gets as far as posting the message and
        then raises on it."""
        kwargs = {"view": view} if view is not None else {}
        text = text[:1990]
        if not self.interaction.response.is_done():
            await self.interaction.response.send_message(text, **kwargs)
            try:
                return await self.interaction.original_response()
            except Exception:
                return None
        return await self.channel.send(text, **kwargs)

    async def ask(self, prompt, *, validator=None, error_text=None, attempts=5,
                  buttons=()):
        """Ask `prompt` and wait for an answer. Returns ('text', value),
        ('button', value), or None when stopped or timed out."""
        pending = prompt
        for _ in range(attempts):
            rows = [(label, ButtonStyle.secondary, ("button", value))
                    for label, value in buttons]
            rows.append((olympiad.text("stop_button", self.lang), ButtonStyle.danger,
                         ("stop", None)))
            view = _QuizButtons(self.user_id, self.lang, rows)
            msg = await self.send(pending, view=view)
            kind, payload = await _wait_reply_or_button(self.channel.id, self.user_id, view)
            view.stop()
            if msg is not None:
                try:
                    await msg.edit(view=None)
                except Exception:
                    pass
            if kind == "button":
                action, value = payload
                if action == "stop":
                    await self.send(olympiad.text("dialog_stopped", self.lang))
                    return None
                return "button", value
            if kind != "text":
                await self.send(localized("dialog_timeout", self.lang))
                return None
            value = (payload.content or "").strip()
            if validator is None:
                return "text", value
            ok = validator(value)
            if ok is not None:
                return "text", ok
            pending = error_text or localized("choice_invalid", self.lang)
        await self.send(localized("dialog_timeout", self.lang))
        return None

    async def choose(self, header, items, render, *, extra=None):
        """A numbered list. Returns the chosen item, the sentinel '__extra__'
        when the trailing extra option is picked, or None."""
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
        """A numbered list several entries may be picked from at once. Returns
        the chosen positions in the order given, or None."""
        lines = [header, ""] + [f"{i + 1}. {render(it)}" for i, it in enumerate(items)]
        answer = await self.ask(
            "\n".join(lines),
            validator=lambda v: olympiad.parse_numbers(v, len(items), limit),
            error_text=olympiad.text("candidates_invalid", self.lang, max=limit))
        return answer[1] if answer else None

# ── The embeds the reviewers see ────────────────────────────────────────────

def _vote_footer(vote):
    platform = "Discord" if vote["platform"] == "discord" else "Telegram"
    return f"{vote['username']} │ {platform} ID: {vote['user_id']} │ {vote['key']}"

def _review_embed(vote, contest):
    """Contest name as the title, the supported wikis as the subtitle, then the
    person's account confirmation, their activity claim and the vote itself.
    The footer carries who they are and the key the reviewer answers with."""
    lang = _review_lang(contest)
    title = olympiad.contest_name(contest, lang)
    if vote["is_update"]:
        title = f"{title} — {olympiad.text('review_updated_mark', lang)}"

    parts = [f"**{olympiad.vote_candidate_names(vote)}**", ""]
    if vote["account_known"]:
        parts.append(olympiad.text("review_account_verified", lang,
                                   name=vote["account_text"]))
    else:
        parts.append(olympiad.text("review_account_claim", lang,
                                   text=vote["account_text"]))
    parts.append(olympiad.text("review_activity", lang, text=vote["activity_text"]))
    parts.append("")
    parts.append(olympiad.vote_body(vote, vote["lang"] or lang))

    embed = discord.Embed(title=title[:256], description="\n".join(parts)[:4096],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    embed.set_footer(text=_vote_footer(vote)[:2048])
    return embed

def _approved_embed(vote, contest, wiki_name):
    """What lands in the accepted-votes chat: who voted, under both their
    messenger name and the wiki name the reviewer confirmed, the wikis they
    supported as the subtitle, and their vote."""
    lang = _review_lang(contest)
    title = f"{vote['username']} — {wiki_name}"
    if vote["is_update"]:
        title = f"{title} ({olympiad.text('review_updated_mark', lang)})"
    parts = [f"**{olympiad.vote_candidate_names(vote)}**", "",
             olympiad.vote_body(vote, vote["lang"] or lang)]
    return discord.Embed(title=title[:256],
                         description="\n".join(parts)[:4096],
                         color=discord.Color(DEFAULT_EMBED_COLOR))

async def post_olympiad_vote(key):
    """Send a stored vote to its contest's review chat. Both bots use this — the
    review chats are always on Discord. Returns True when it was delivered."""
    vote = db.get_vote(key)
    if not vote:
        return False
    contest = db.get_contest(vote["contest_id"])
    if not contest:
        return False
    channel = await _olympiad_channel(contest["review_chat"])
    if channel is None:
        return False
    try:
        await channel.send(embed=_review_embed(vote, contest))
        return True
    except Exception as e:
        logger.warning("Olympiad vote %s could not be posted for review: %s", key, e)
        return False

async def dm_olympiad_voter(vote, title, body):
    """Deliver a reviewer's decision to the voter, on whichever messenger they
    voted from."""
    if vote["platform"] == "discord":
        try:
            user = await bot.fetch_user(int(vote["user_id"]))
            await user.send(embed=discord.Embed(title=title, description=body,
                                                color=discord.Color(DEFAULT_EMBED_COLOR)))
            return True
        except Exception:
            return False
    try:
        from telegram_bot import bot as tg_bot
        await tg_bot.send_message(int(vote["user_id"]), f"{title}\n\n{body}")
        return True
    except Exception:
        return False

# ── /setolympiad ────────────────────────────────────────────────────────────

@bot.tree.command(name="setolympiad", description="open the Olympiad and its voting period (bot admins)")
@app_commands.describe(start="First day of voting, MM-DD-YYYY — or 'off' to cancel the Olympiad",
                       end="Last day of voting, MM-DD-YYYY")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def setolympiad_cmd(interaction: discord.Interaction, start: str, end: str = None):
    lang = _oly_lang(interaction)
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    if (start or "").strip().lower() in ("off", "stop", "cancel"):
        if not db.get_olympiad():
            await interaction.response.send_message(
                olympiad.text("setolympiad_off_none", lang), ephemeral=True)
            return
        db.clear_olympiad_data()
        db.clear_olympiad()
        await interaction.response.send_message(olympiad.text("setolympiad_off", lang))
        await sync_olympiad_commands()
        return

    if not end or not end.strip():
        await interaction.response.send_message(
            olympiad.text("setolympiad_usage", lang), ephemeral=True)
        return
    try:
        start_ts, end_ts = olympiad.parse_period(start, end)
    except ValueError as e:
        key = "setolympiad_bad_order" if str(e) == "bad_order" else "setolympiad_bad_date"
        await interaction.response.send_message(olympiad.text(key, lang), ephemeral=True)
        return

    existed = db.get_olympiad() is not None
    db.set_olympiad(start_ts, end_ts)
    await interaction.response.send_message(olympiad.text(
        "setolympiad_updated" if existed else "setolympiad_set", lang,
        start=olympiad.format_date(start_ts), end=olympiad.format_date(end_ts)))
    await sync_olympiad_commands()

# ── /setcontest and /editcontest ────────────────────────────────────────────
# Both dialogs themselves live in olympiad.py, shared with the Telegram bot;
# what stays here is the permission gate and the Discord dialog object.

async def _olympiad_admin_gate(interaction, lang):
    """The two setup commands are for Bot Admins, for as long as the Olympiad
    runs. Returns the dialog channel, or None when the caller may not proceed."""
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return None
    if not olympiad.is_open():
        await interaction.response.send_message(
            olympiad.text("not_active", lang), ephemeral=True)
        return None
    return await _dm_channel(interaction)

@app_commands.command(name="setcontest", description="create a contest of the Olympiad (bot admins)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def setcontest_cmd(interaction: discord.Interaction):
    lang = _oly_lang(interaction)
    channel = await _olympiad_admin_gate(interaction, lang)
    if channel is None:
        return
    await olympiad.run_setcontest(_OlyDialog(interaction, lang, channel), lang)

@app_commands.command(name="editcontest", description="manage a contest's candidate wikis (bot admins)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def editcontest_cmd(interaction: discord.Interaction):
    lang = _oly_lang(interaction)
    channel = await _olympiad_admin_gate(interaction, lang)
    if channel is None:
        return
    await olympiad.run_editcontest(_OlyDialog(interaction, lang, channel), lang)


# ── /olympiad ───────────────────────────────────────────────────────────────

async def _ask_dm_language(dialog, interaction):
    """Step 1: with no language chosen for this private chat yet, ask in English
    and offer a button per localization. The answer is remembered for the chat
    (and dropped again after a year). Returns the language code, or None."""
    rows = [(language_name(code), ButtonStyle.primary, code)
            for code in available_locales()]
    view = _QuizButtons(interaction.user.id, DEFAULT_LANG, rows)
    msg = await dialog.send(olympiad.text("ask_lang", DEFAULT_LANG), view=view)
    code = await view.wait_click()
    if msg is not None:
        try:
            await msg.edit(view=None)
        except Exception:
            pass
    if not code:
        return None
    set_chat_lang(_chat_key(interaction), code, is_dm=True)
    dialog.lang = code
    await dialog.send(olympiad.text("lang_chosen", code))
    return code

@app_commands.command(name="olympiad", description="vote in the Olympiad (private chat with the bot only)")
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def olympiad_cmd(interaction: discord.Interaction):
    lang = _oly_lang(interaction)
    if interaction.guild is not None:
        await interaction.response.send_message(
            olympiad.text("dm_only", lang), ephemeral=True)
        return
    period = olympiad.period()
    if not period:
        await interaction.response.send_message(
            olympiad.text("not_active", lang), ephemeral=True)
        return
    if not olympiad.is_voting_open():
        await interaction.response.send_message(olympiad.text(
            "voting_not_started", lang, start=olympiad.format_date(period[0])),
            ephemeral=True)
        return

    channel = await _dm_channel(interaction)
    dialog = _OlyDialog(interaction, lang, channel)
    if db.get_chat_lang(_chat_key(interaction)) is None:
        code = await _ask_dm_language(dialog, interaction)
        if code is None:
            return
        lang = code
    await olympiad.run_vote(dialog, lang, "discord", interaction.user.id,
                            str(interaction.user), "Discord", post_olympiad_vote)


# ── /accepted and /denied ───────────────────────────────────────────────────

async def _resolve_review(interaction, lang, key, usage_key, not_found_key):
    """The vote a reviewer named, once it is clear the command was run in the
    chat that reviews it — the key alone is not enough to decide a vote from
    somewhere else. Returns (vote, contest) or (None, None)."""
    key = (key or "").strip()
    if not key:
        await interaction.response.send_message(
            olympiad.text(usage_key, lang), ephemeral=True)
        return None, None
    vote = db.get_vote(key)
    contest = db.get_contest(vote["contest_id"]) if vote else None
    if not vote or not contest:
        await interaction.response.send_message(
            olympiad.text(not_found_key, lang, key=key), ephemeral=True)
        return None, None
    if str(interaction.channel_id) != str(contest["review_chat"]):
        await interaction.response.send_message(
            olympiad.text("accepted_wrong_chat", lang), ephemeral=True)
        return None, None
    return vote, contest

@app_commands.command(name="accepted", description="accept a vote under review")
@app_commands.describe(key="The vote's key, from the embed's footer",
                       nickname="The voter's username on the wiki host",
                       remember="Type 'remember' to link that account to this user")
async def accepted_cmd(interaction: discord.Interaction, key: str, nickname: str,
                       remember: str = None):
    lang = _oly_lang(interaction)
    vote, contest = await _resolve_review(interaction, lang, key, "accepted_usage",
                                          "accepted_not_found")
    if not vote:
        return
    nickname = (nickname or "").strip()
    if not nickname:
        await interaction.response.send_message(
            olympiad.text("accepted_usage", lang), ephemeral=True)
        return

    approved_chat = db.approved_chat_for(contest, vote["lang"])
    channel = await _olympiad_channel(approved_chat)
    if channel is None:
        await interaction.response.send_message(
            olympiad.text("accepted_no_channel", lang), ephemeral=True)
        return
    try:
        await channel.send(embed=_approved_embed(vote, contest, nickname))
    except Exception as e:
        logger.warning("Accepted vote %s could not be posted: %s", vote["key"], e)
        await interaction.response.send_message(
            olympiad.text("accepted_no_channel", lang), ephemeral=True)
        return

    lines = [olympiad.text("accepted_done", lang, key=vote["key"])]
    if (remember or "").strip().lower() == "remember":
        db.set_wiki_account(vote["platform"], vote["user_id"], nickname,
                            str(interaction.user))
        lines.append(olympiad.text("accepted_remembered", lang, name=nickname))

    voter_lang = vote["lang"] or DEFAULT_LANG
    await dm_olympiad_voter(
        vote, olympiad.text("accepted_dm_title", voter_lang),
        olympiad.text("accepted_dm_body", voter_lang,
                      contest=olympiad.contest_name(contest, voter_lang),
                      wikis=olympiad.vote_candidate_names(vote)))
    db.delete_vote(vote["key"])
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@app_commands.command(name="denied", description="reject a vote under review")
@app_commands.describe(key="The vote's key, from the embed's footer",
                       reason="Why the vote is rejected — the voter is told")
async def denied_cmd(interaction: discord.Interaction, key: str, reason: str):
    lang = _oly_lang(interaction)
    vote, contest = await _resolve_review(interaction, lang, key, "denied_usage",
                                          "denied_not_found")
    if not vote:
        return
    reason = (reason or "").strip()
    if not reason:
        await interaction.response.send_message(
            olympiad.text("denied_usage", lang), ephemeral=True)
        return

    voter_lang = vote["lang"] or DEFAULT_LANG
    await dm_olympiad_voter(
        vote, olympiad.text("denied_dm_title", voter_lang),
        olympiad.text("denied_dm_body", voter_lang,
                      contest=olympiad.contest_name(contest, voter_lang),
                      wikis=olympiad.vote_candidate_names(vote), reason=reason))
    db.delete_vote(vote["key"])
    await interaction.response.send_message(
        olympiad.text("denied_done", lang, key=vote["key"]), ephemeral=True)

OLYMPIAD_COMMANDS = [setcontest_cmd, editcontest_cmd, olympiad_cmd,
                     accepted_cmd, denied_cmd]
