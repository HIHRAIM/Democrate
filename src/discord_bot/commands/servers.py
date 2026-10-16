"""A server's own profile and settings on Discord: `/server info` and
`/server edit`.

`/server info` is anyone's and shows what the server is in the union's economy —
its banks, the currencies earned in its channels, the accounts of its banks,
its enterprises — and the taxes paid in it; the content is
stats.py: community_profile, shared with Telegram. A Discord earning binding
is keyed by a bare channel id, so this half hands over the ids of the
server's channels and threads for the profile to recognise.

`/server edit` is a numbered menu for Bot Admins and Server Admins: the
community tax (economy/taxes.py — burned, taken from payments, sales and
message earnings made here) and the server's timezone, the one offset the
celebrations are timed by.

Not this module's zone: the tax arithmetic (economy/taxes.py) and the
union's profile (commands/unions.py).
"""
import discord
from discord import app_commands

import db
import economy
import stats
from utils import (
    DEFAULT_EMBED_COLOR, format_tz_offset, get_chat_lang, is_admin, localized,
    parse_tz_offset, rate_limit_ok,
)

from discord_bot.client import _chat_key, bot, is_server_admin
from discord_bot.dialogs import _dialog_text, _econ_embed, _ereply, _numbered_choice
from discord_bot.groups import server_group

_EDIT_SERVER_OPTIONS = ("tax", "tz")

@server_group.command(name="info", description=localized("slash_server", "en"))
async def server_cmd(interaction: discord.Interaction):
    """Show this server's profile (everyone)."""
    lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is None:
        await _ereply(interaction, "guild_only", lang)
        return
    if not db.is_setup(interaction.guild_id):
        await _ereply(interaction, "chat_not_setup", lang)
        return
    if not rate_limit_ok(("server-profile", "discord", interaction.user.id), limit=5,
                         window_seconds=60):
        await _ereply(interaction, "union_profile_slow_down", lang)
        return
    guild = interaction.guild
    db.set_chat_title(guild.id, guild.name)
    keys = [c.id for c in guild.channels] + [t.id for t in guild.threads]
    profile = stats.community_profile("discord", str(guild.id), lang, keys)
    embed = discord.Embed(title=guild.name or profile["title"],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    for title, rows in profile["sections"]:
        embed.add_field(name=title, value="\n".join(f"**{k}:** {v}" for k, v in rows)[:1024],
                        inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await interaction.response.send_message(embed=embed)

def _tz_minutes(value):
    """A typed UTC offset in minutes, or None so the dialog asks again."""
    try:
        return parse_tz_offset(value)
    except ValueError:
        return None

@server_group.command(name="edit", description=localized("slash_edit_server", "en"))
@app_commands.describe(option=localized("slash_param_menu_option", "en"))
async def edit_server_cmd(interaction: discord.Interaction, option: int = None):
    """Edit this server's settings through a numbered menu: the community
    tax and the timezone (Bot Admins and Server Admins)."""
    lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is None:
        await _ereply(interaction, "guild_only", lang)
        return
    if not (is_admin("discord", interaction.user.id) or is_server_admin(interaction)):
        await _ereply(interaction, "no_permission", lang)
        return
    if not db.is_setup(interaction.guild_id):
        await _ereply(interaction, "chat_not_setup", lang)
        return
    if option is None or not 1 <= option <= len(_EDIT_SERVER_OPTIONS):
        items = [localized(f"edit_server_opt_{key}", lang) for key in _EDIT_SERVER_OPTIONS]
        chosen = await _numbered_choice(
            interaction, lang, localized("edit_server_menu_header", lang,
                                         name=interaction.guild.name),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_SERVER_OPTIONS[chosen[0]]
    else:
        action = _EDIT_SERVER_OPTIONS[option - 1]
    server = str(interaction.guild_id)
    channel = interaction.channel
    if action == "tax":
        rate = await _dialog_text(
            interaction, lang,
            localized("edit_server_ask_tax", lang,
                      current=economy.format_percent(db.community_tax("discord", server)),
                      max=economy.format_percent(economy.MAX_TAX_PERCENT)),
            validator=economy.parse_percent, error_key="tax_bad_percent", as_embed=True)
        if rate is None:
            return
        db.set_community_fields("discord", server, tax_percent=rate)
        await channel.send(embed=_econ_embed(localized(
            "edit_server_tax_set", lang, percent=economy.format_percent(rate))))
    elif action == "tz":
        minutes = await _dialog_text(
            interaction, lang,
            localized("edit_server_ask_tz", lang,
                      current=format_tz_offset(db.community_tz("discord", server))),
            validator=_tz_minutes, error_key="setchannel_bad_tz", as_embed=True)
        if minutes is None:
            return
        db.set_community_fields("discord", server, tz_offset=minutes)
        await channel.send(embed=_econ_embed(localized(
            "edit_server_tz_set", lang, tz=format_tz_offset(minutes))))
