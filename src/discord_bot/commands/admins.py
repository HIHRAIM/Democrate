"""Delegating rights, and the one command that hands out the database:
`/setadmin`, `/remadmin`, `/localizer-add`, `/localizer-rem`, `/backup`.

`/backup` lives here rather than beside the backup loop because it is the
manual form of the same bot-admin privilege; the encryption and the automatic
12-hour send are in discord_bot/client.py and backup_crypto.py. The file it
sends is always encrypted — a deployment without BACKUP_KEY gets an error
rather than a plaintext database.

`_resolve_user_ref` is the tolerant form of the reference parser: a ping, an
id, or a username looked up against the guild's member list, which is why it is
async and why only these two commands use it. The cheap form used everywhere
else is dialogs.py: _parse_user_ref.

Not this module's zone: per-object leadership (parties, banks, enterprises),
which is granted by the objects' own commands.
"""
import io

import discord
from discord import app_commands

import db
from utils import get_chat_lang, is_admin, localized

from discord_bot.client import _chat_key, bot, logger
from discord_bot.dialogs import USER_REF_RE

@bot.tree.command(name="setadmin", description="delegate bot server-admin rights to a user (bot admins)")
@app_commands.describe(user="User to make a server admin: ping or ID")
async def setadmin_cmd(interaction: discord.Interaction, user: str):
    """Delegate server-admin rights to a user in this server (Bot Admins).

    A delegated admin can do everything a native Administrator can do *through the
    bot*, and implicitly counts as a Localizer for the control panel."""
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
    """Revoke a delegated server-admin grant (Bot Admins).

    Only the delegation: a member with the native Administrator permission keeps
    their rights and cannot be demoted through the bot."""
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
    """Grant Localizer status, letting the user edit this bot's localization
    in the control panel (Bot Admins).

    The username is stored beside the id because the panel logs people in by
    username, not by messenger id."""
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
    """Revoke a delegated Localizer status (Bot Admins).

    Says so plainly when there was no row: delegated server admins are localizers
    implicitly and cannot be demoted here."""
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

@bot.tree.command(name="backup", description="get a database backup (bot admins)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def backup_discord_cmd(interaction: discord.Interaction):
    """Send the caller an encrypted database snapshot (Bot Admins).

    The manual form of the 12-hour backup loop, and the same encryption: without
    BACKUP_KEY the build fails and the command reports it rather than sending a
    plaintext database. Ephemeral, and usable in a private chat with the bot."""
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
