"""Union wikis on Discord: attaching and detaching them, correcting a
founding date, and the server's wiki birthday channel, per-wiki opt-out and
representative role.

Attaching is the one command here that talks to a wiki: `/wiki add` resolves
the link, reads the wiki's name, language, founding moment and
administration at once (wikis.py: load_wiki), and only then attaches it, so a
union never holds a wiki the bot could not read. Every other command finds
the wiki among the known ones by its link (wikis.py: match_known_wiki) and
asks the network nothing.

All six commands are for Bot Admins. The channel and the representative
role are a server's, so they need a server bound to a union; the wiki
commands take the union code and work anywhere.

Not this module's zone: what is posted and when (discord_bot/celebrations.py,
celebrations.py), and the Telegram twins of the wiki commands
(telegram_bot/commands/wikis.py).
"""
import asyncio

import discord
from discord import app_commands

import celebrations
import db
import wikis
from utils import format_tz_offset, get_chat_lang, is_admin, localized, parse_tz_offset

from discord_bot.celebrations import (
    OFF_WORDS, can_manage_role, rep_role_pass, target_channel, target_role,
)
from discord_bot.client import _chat_key, bot
from discord_bot.dialogs import _ereply
from discord_bot.groups import wiki_group

async def _require_bot_admin(interaction, lang):
    """Refuse anyone but a Bot Admin; returns whether they may go on."""
    if is_admin("discord", interaction.user.id):
        return True
    await _ereply(interaction, "no_permission", lang)
    return False

async def _require_setup_guild(interaction, lang):
    """The chats row of this server, replying when there is none."""
    if interaction.guild is None:
        await _ereply(interaction, "guild_only", lang)
        return None
    chat = db.get_chat(interaction.guild_id)
    if chat is None:
        await _ereply(interaction, "chat_not_setup", lang)
    return chat

def known_wiki(url):
    """The known wiki a typed link means, or None."""
    return wikis.match_known_wiki(url, db.all_wikis())

@wiki_group.command(name="add", description=localized("slash_add_wiki", "en"))
@app_commands.describe(union=localized("slash_param_union", "en"),
                       url=localized("slash_param_wiki_url", "en"))
async def add_wiki_cmd(interaction: discord.Interaction, union: str, url: str):
    """Attach a wiki to a union (Bot Admins). The wiki is read at once — its
    name, language, founding moment and administration — and attached only
    when that worked."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_bot_admin(interaction, lang):
        return
    union = union.strip().upper()
    if not db.union_exists(union):
        await _ereply(interaction, "setup_unknown_union", lang, code=union,
                      codes=", ".join(db.get_union_codes()))
        return
    await interaction.response.defer(thinking=True)
    try:
        row = await wikis.load_wiki(url)
    except wikis.WikiBlocked:
        await interaction.followup.send(localized("wiki_blocked", lang))
        return
    except wikis.WikiError:
        await interaction.followup.send(localized("wiki_not_found", lang))
        return
    if not db.attach_wiki(union, row["id"], interaction.user.id):
        await interaction.followup.send(localized("wiki_already_attached", lang,
                                                  name=row["name"], union=union))
        return
    await interaction.followup.send(localized("wiki_attached", lang, union=union) + "\n"
                                    + celebrations.wiki_summary(row, lang),
                                    allowed_mentions=discord.AllowedMentions.none())

@wiki_group.command(name="remove", description=localized("slash_remove_wiki", "en"))
@app_commands.describe(url=localized("slash_param_wiki_url", "en"),
                       union=localized("slash_param_union_optional", "en"))
async def remove_wiki_cmd(interaction: discord.Interaction, url: str, union: str = None):
    """Detach a wiki from a union (Bot Admins). The union may be left out
    when the wiki belongs to one union only."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_bot_admin(interaction, lang):
        return
    row = known_wiki(url)
    if row is None:
        await _ereply(interaction, "wiki_unknown", lang)
        return
    codes = db.wiki_union_codes(row["id"])
    code = union.strip().upper() if union else (codes[0] if len(codes) == 1 else None)
    if code is None:
        await _ereply(interaction, "wiki_specify_union", lang, codes=", ".join(codes))
        return
    if not db.detach_wiki(code, row["id"]):
        await _ereply(interaction, "wiki_not_in_union", lang, name=row["name"], union=code)
        return
    await _ereply(interaction, "wiki_detached", lang, ephemeral=False,
                  name=row["name"], union=code)

@wiki_group.command(name="founded", description=localized("slash_wiki_founded", "en"))
@app_commands.describe(url=localized("slash_param_wiki_url", "en"),
                       date=localized("slash_wiki_founded_date", "en"))
async def wiki_founded_cmd(interaction: discord.Interaction, url: str, date: str):
    """Correct the founding date of a wiki (Bot Admins), or go back to the
    detected one with 'auto'. The date counts from midnight UTC."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_bot_admin(interaction, lang):
        return
    row = known_wiki(url)
    if row is None:
        await _ereply(interaction, "wiki_unknown", lang)
        return
    if date.strip().lower() == "auto":
        db.set_wiki_founded_manual(row["id"], None)
    else:
        try:
            db.set_wiki_founded_manual(row["id"], celebrations.parse_date(date))
        except ValueError:
            await _ereply(interaction, "wiki_bad_date", lang)
            return
    await _ereply(interaction, "wiki_founded_set", lang, ephemeral=False,
                  name=row["name"], date=celebrations.wiki_date(db.get_wiki(row["id"])))

@wiki_group.command(name="channel", description=localized("slash_wiki_channel", "en"))
@app_commands.describe(channel=localized("slash_param_channel", "en"),
                       tz=localized("slash_param_tz", "en"))
async def wiki_channel_cmd(interaction: discord.Interaction, channel: str = None, tz: str = None):
    """Choose this server's wiki birthday channel, or switch the greetings off
    with 'off' (Bot Admins). A timezone given here becomes the server's."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_bot_admin(interaction, lang):
        return
    if await _require_setup_guild(interaction, lang) is None:
        return
    fields = {}
    if tz is not None:
        try:
            fields["tz_offset"] = parse_tz_offset(tz)
        except ValueError:
            await _ereply(interaction, "setchannel_bad_tz", lang)
            return
    if channel and channel.strip().lower() in OFF_WORDS:
        db.set_community_fields("discord", interaction.guild_id, wiki_channel=None, **fields)
        await _ereply(interaction, "wiki_channel_off", lang, ephemeral=False)
        return
    target = target_channel(interaction, channel)
    if target is None:
        await _ereply(interaction, "setchannel_bad_channel", lang)
        return
    db.set_community_fields("discord", interaction.guild_id, wiki_channel=str(target.id), **fields)
    await _ereply(interaction, "wiki_channel_set", lang, ephemeral=False, channel=target.mention,
                  time=f"{celebrations.GREETING_HOUR:02d}:00",
                  tz=format_tz_offset(db.community_tz("discord", interaction.guild_id)))

@wiki_group.command(name="greeting", description=localized("slash_wiki_greeting", "en"))
@app_commands.describe(url=localized("slash_param_wiki_url", "en"),
                       enabled=localized("slash_wiki_greeting_enabled", "en"))
async def wiki_greeting_cmd(interaction: discord.Interaction, url: str, enabled: bool):
    """Switch the birthday greeting of one wiki of this server's union off or
    back on, in this server only (Bot Admins)."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_bot_admin(interaction, lang):
        return
    chat = await _require_setup_guild(interaction, lang)
    if chat is None:
        return
    row = wikis.match_known_wiki(url, db.get_union_wikis(chat["union_code"]))
    if row is None:
        await _ereply(interaction, "wiki_not_in_union", lang, name=url, union=chat["union_code"])
        return
    db.set_wiki_greeting("discord", interaction.guild_id, row["id"], enabled)
    await _ereply(interaction, "wiki_greeting_on" if enabled else "wiki_greeting_off",
                  lang, ephemeral=False, name=row["name"])

@wiki_group.command(name="rep-role", description=localized("slash_wiki_rep_role", "en"))
@app_commands.describe(role=localized("slash_param_role", "en"))
async def wiki_rep_role_cmd(interaction: discord.Interaction, role: str):
    """Choose the role this server gives to the representatives of its
    union's wikis, or 'off' (Bot Admins). The members who qualify get it in a
    moment; switching it off takes it back from those the bot gave it to."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_bot_admin(interaction, lang):
        return
    if await _require_setup_guild(interaction, lang) is None:
        return
    if role.strip().lower() in OFF_WORDS:
        db.set_community_fields("discord", interaction.guild_id, rep_role=None)
        await _ereply(interaction, "rep_role_off", lang, ephemeral=False)
    else:
        target = target_role(interaction, role)
        if target is None:
            await _ereply(interaction, "role_bad", lang)
            return
        db.set_community_fields("discord", interaction.guild_id, rep_role=str(target.id))
        text_key = "rep_role_set" if can_manage_role(interaction.guild, target) else "role_set_cannot_manage"
        await _ereply(interaction, text_key, lang, ephemeral=False, role=target.mention,
                      days=wikis.REPRESENTATIVE_AFTER // 86400)
    asyncio.create_task(rep_role_pass())
