"""A server's own profile and settings on Discord: `/server info` and
`/server edit`.

`/server info` is anyone's and shows what the server is in the union's economy —
its banks, the currencies earned in its channels, the accounts of its banks,
its enterprises — and the taxes paid in it; the content is
stats.py: community_profile, shared with Telegram. A Discord earning binding
is keyed by a bare channel id, so this half hands over the ids of the
server's channels and threads for the profile to recognise.

`/server birthday` lets the same people add the server's founding day,
and optionally an invitation, to the union's calendar and wiki birthday
posts (celebrations.py) — or take it away again. Nothing is shown about a
server whose administrators did not add it.

`/server edit` is a numbered menu for Bot Admins and Server Admins: the
community tax (economy/taxes.py — burned, taken from payments, sales and
message earnings made here), the server's timezone, the one offset the
celebrations are timed by, and the parts of the economy switched off here
(economy/switches.py), toggled one at a time by number.

Not this module's zone: the tax arithmetic (economy/taxes.py) and the
union's profile (commands/unions.py).
"""
import time

import discord
from discord import app_commands

import celebrations
import db
import economy
import stats
from utils import (
    DEFAULT_EMBED_COLOR, format_tz_offset, get_chat_lang, is_admin, localized,
    parse_tz_offset, rate_limit_ok,
)

from discord_bot.client import _chat_key, bot, is_server_admin
from discord_bot.dialogs import _dialog_text, _econ_embed, _ereply, _numbered_choice, _say
from discord_bot.groups import server_group

_EDIT_SERVER_OPTIONS = ("tax", "tz", "econ")

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

@server_group.command(name="birthday", description=localized("slash_server_birthday", "en"))
@app_commands.describe(date=localized("slash_server_birthday_date", "en"),
                       link=localized("slash_server_birthday_link", "en"))
async def server_birthday_cmd(interaction: discord.Interaction, date: str = None,
                              link: str = None):
    """Add this server's founding day to the union's calendar and wiki
    birthday posts, change it, or take it away (Bot Admins and Server
    Admins).

    Adding it is the server's consent to being named there. 'auto' takes the
    day Discord created the server; a typed date counts from midnight UTC.
    The invitation is optional, must lead to a Discord server or a Telegram
    group (celebrations.parse_invite), and '-' removes it; 'off' removes
    both. Without arguments the command shows what is set."""
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
    server = str(interaction.guild_id)
    row = db.get_community("discord", server)
    founded = row["founded_at"] if row else None
    url = row["invite_url"] if row else None
    if date is None and link is None:
        if founded is None:
            await _ereply(interaction, "server_birthday_usage_discord", lang)
        else:
            await _say(interaction, embed=_econ_embed(celebrations.community_birthday_text(
                "server_birthday_status", founded, url, lang)), ephemeral=True)
        return
    value = (date or "").strip()
    if value.lower() == "off":
        db.set_community_fields("discord", server, founded_at=None, invite_url=None)
        await _ereply(interaction, "server_birthday_off", lang, ephemeral=False)
        return
    if value.lower() == "auto":
        founded = int(interaction.guild.created_at.timestamp())
    elif value:
        try:
            founded = celebrations.parse_date(value)
        except ValueError:
            founded = None
        if founded is None or founded > time.time():
            await _ereply(interaction, "server_birthday_bad_date", lang)
            return
    if link is not None:
        if link.strip() == "-":
            url = None
        else:
            url = celebrations.parse_invite(link)
            if url is None:
                await _ereply(interaction, "server_birthday_bad_link", lang)
                return
    if founded is None:
        await _ereply(interaction, "server_birthday_usage_discord", lang)
        return
    db.set_chat_title(interaction.guild.id, interaction.guild.name)
    db.set_community_fields("discord", server, founded_at=founded, invite_url=url)
    await _say(interaction, embed=_econ_embed(celebrations.community_birthday_text(
        "server_birthday_set", founded, url, lang)))

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
    tax, the timezone and the parts of the economy (Bot Admins and Server
    Admins)."""
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
    elif action == "econ":
        off = economy.community_off("discord", server)
        part = await _numbered_choice(
            interaction, lang,
            localized("econ_menu_community", lang, name=interaction.guild.name),
            list(economy.ECON_PARTS), lambda p: economy.part_line(p, off, lang))
        if part is None:
            return
        economy.set_community_part("discord", server, part, part not in off)
        await channel.send(embed=_econ_embed(
            economy.switched_text(part, part not in off, lang)))
