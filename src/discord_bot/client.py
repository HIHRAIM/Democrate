"""The Discord client itself: the DemBot class, its loops, the error handler,
the encrypted backups it ships, and the permission gates every command reuses.

Importing this module creates `bot`. Nothing else here registers a command —
the decorators live in the modules below, and this one is imported first
because they all need `bot` to hang themselves on.

`setup_hook` does three things in a fixed order: it adds the Olympiad's
runtime commands to the tree (so that a restart during an open Olympiad puts
them back), syncs the tree once, and re-arms the persistent Accept/Decline
views of every join request still open, which is what lets a request survive a
restart. The three loops it starts are a fixed presence re-applied hourly, the
12-hour encrypted backup sent to BOTH platforms' backup chats, and the
per-minute wiki-anniversary check.

The gates at the bottom — is_server_admin, _require_verified,
_refuse_not_setup, parties_enabled, _chat_key — are here rather than in a
commands module because every half of the bot asks them and none of them owns
them. Their Telegram twins are in telegram_bot/client.py, deliberately written
separately: the native half of "is this user an admin" is a different question
on each platform.

Not this module's zone: the @bot.event handlers (discord_bot/events.py) and
anything with a slash command in it.
"""
import asyncio
import io
import logging
import traceback

import discord
from discord import app_commands

import db
from utils import STATUS_TEXT, get_chat_lang, is_verified, localized

logger = logging.getLogger("dem.discord")

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
        """Build the client with the intents the bot cannot work without.

        `message_content` is privileged and must be enabled in the Developer Portal:
        message earning, the interactive dialogs and every numbered menu read ordinary
        message text, so without it the bot starts and then silently answers
        nothing."""
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        """Prepare the client before it connects, in a fixed order.

        The Olympiad's runtime commands are added to the tree *before* it is synced, so
        a restart during an open Olympiad puts them back rather than dropping them for
        an hour. Then the three loops start, and finally every still-open join request
        gets its persistent view re-armed — that is what makes an Accept/Decline button
        posted last week still work after a restart. Both imports are at the call site:
        the modules they come from import this one."""
        from discord_bot.commands.parties import JoinRequestView
        from discord_bot.olympiad import _apply_olympiad_commands

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
        """Log the identity the bot connected as. Fires again on every reconnect,
        so nothing that must happen once may live here."""
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
        """Send an encrypted database snapshot to both platforms' backup chats
        every 12 hours.

        Sleeps first, deliberately: a bot that is restarted often would otherwise
        flood the backup chats. The snapshot is always encrypted — a deployment
        without BACKUP_KEY gets an error instead of a plaintext database."""
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(12 * 3600)
            await _send_db_backup_discord(self)
            await _send_db_backup_telegram()

    async def founday_loop(self):
        """Wiki anniversaries are due at a whole minute, so poll once a minute."""
        from discord_bot.commands.foundays import _post_due_foundays

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
    """Last-resort handler for anything a slash command raises.

    Logs the traceback and tells the caller in their chat's language, choosing
    between an initial response and a follow-up because the command may already
    have answered. Every send here is wrapped: failing to report a failure must not
    raise a second one."""
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
    """Send the encrypted snapshot to the Discord backup channels.

    Builds the file once and fans it out, fetching a channel the client has not
    cached. Every failure is logged and skipped rather than raised: one
    unreachable backup chat must not cost the others their copy."""
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
    """Send the same encrypted snapshot to the Telegram backup chats.

    The Telegram bot is imported at the call site — this is the cycle-breaker
    between the halves — and the chat entries are 'chat_id:thread_id' strings, so
    a backup can land in one topic of a forum group."""
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

async def _refuse_not_setup(interaction: discord.Interaction, lang):
    """Tell the caller this chat is not bound to a union. The single wording
    for a refusal several dozen commands share."""
    await interaction.response.send_message(localized("chat_not_setup", lang), ephemeral=True)

def parties_enabled(union_code):
    """Whether a union has parties switched on. The first half of the party
    gate; the second is whether the chat is set up at all."""
    return db.get_party_settings(union_code)[0]

def _chat_key(interaction: discord.Interaction):
    """The localization key of the channel an interaction came from,
    'guild:channel'.

    Always the specific key, never the bare server id: db/settings.py resolves
    outwards from it, so a channel with its own /locallang wins and everything
    else falls back to the server."""
    return f"{interaction.guild_id}:{interaction.channel_id}"
