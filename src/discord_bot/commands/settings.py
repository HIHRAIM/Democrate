"""Per-chat settings: the language of a server or of one channel, and the two
scheduled-post channels.

`/lang` writes the bare server id and `/locallang` writes `guild:channel`; the
lookup in db/settings.py resolves the most specific key outwards, which is the
whole mechanism behind a channel that speaks a different language from its
server.

`_set_channel_cmd` serves both `/setlogs` and `/settasks` because they differ
only in which arguments they accept: the weekly statistics need a weekday, the
monthly task does not. Both store a UTC offset rather than a timezone name, and
main.py: _due_channel_posts converts with it.

Not this module's zone: the union binding (commands/unions.py) and what is
posted into the channels (stats.py).
"""
import discord
from discord import app_commands

import db
from utils import (
    SUPPORTED_LANGS, get_chat_lang, format_tz_offset, is_admin, localized,
    parse_founday_time, parse_tz_offset, parse_weekday, set_chat_lang,
    weekday_name,
)

from discord_bot.client import _chat_key, _refuse_not_setup, bot, is_server_admin
from discord_bot.dialogs import _ereply

@bot.tree.command(name="lang", description="set the default bot language for the whole server (server admins)")
@app_commands.describe(code="Language code (ru, uk, pl, en, es, pt)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def lang_command(interaction: discord.Interaction, code: str):
    """Set the language of the whole server (Server Admins).

    Writes the bare server id, which is the key `/locallang` overrides per
    channel. Also usable in a private chat with the bot, where it sets the
    language of that conversation."""
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
    """Set the language of this channel or thread alone (Server Admins).

    Writes the specific 'guild:channel' key, which wins over the server's own in
    db/settings.py: get_chat_lang — that is the whole mechanism behind a channel
    speaking a different language from the server around it."""
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
    """Set the server's economic log channel and its weekly schedule (Server
    Admins).

    Two different posts land here: the daily FX update after every daily tick, and
    the weekly statistics at the configured weekday and time."""
    await _set_channel_cmd(interaction, "logs", chat_id, weekday, post_time, tz)

@bot.tree.command(name="settasks", description="set this server's monthly-task channel (server admins)")
@app_commands.describe(chat_id="Channel ID (default: this channel; 'off' unbinds)",
                       post_time="Posting time on the 1st, HH:MM",
                       tz="Timezone as a UTC offset, e.g. UTC+3")
async def settasks_cmd(interaction: discord.Interaction, chat_id: str = None,
                       post_time: str = None, tz: str = None):
    """Set the channel where the server's monthly task is posted on the 1st
    (Server Admins).

    The post also carries the verdict on the previous month. The judging itself
    happens regardless — a server without this channel still gets its provision
    bonus, it just never sees the announcement."""
    await _set_channel_cmd(interaction, "tasks", chat_id, None, post_time, tz)
