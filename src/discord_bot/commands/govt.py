"""Governing bodies: `/add-govt` creates one, `/govt` shows who sits in it.

A body holds no money and has no leaders, so there is nothing here beyond
creation and display — the interesting half of the relation is read from the
other side, where db/parties.py: party_govt_seats puts a seat count on a party
card.

`_govt_member_lines` and `_search_govt` are shared with the Telegram half,
which imports them by name for the same reason the party card is shared.

Not this module's zone: parties (commands/parties.py) and the union itself
(commands/unions.py).
"""
import discord
from discord import app_commands

import db
import sponsors
from message_relay import clean_display_name
from utils import (
    DEFAULT_EMBED_COLOR, find_close_names, format_stored_user,
    get_chat_lang, is_admin, localized,
)

from discord_bot.client import _chat_key, _refuse_not_setup, bot
from discord_bot.dialogs import _numbered_choice, _say

@bot.tree.command(name="add-govt", description="create a government body in a union (bot admins)")
@app_commands.describe(
    union="Union code",
    name="Name of the government body",
    member_singular="What one member is called (e.g. minister)",
    member_plural="What several members are called (e.g. ministers)",
)
async def add_govt_cmd(interaction: discord.Interaction, union: str, name: str,
                       member_singular: str, member_plural: str):
    """Create a governing body in a union (Bot Admins).

    The two member words are asked for because the body names its own seats — a
    "minister" and "ministers" — and every later mention of a member uses them."""
    lang = get_chat_lang(_chat_key(interaction))
    union = union.strip().upper()
    if not sponsors.can_manage_union(interaction.user.id, union):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

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
    """One line per seat holder, each with the party they belong to.

    The party is looked up now rather than stored on the seat, so a member who
    switches party is shown under the new one. Shared with the Telegram half,
    hence the explicit `viewer_platform`: a Discord member shown on Telegram must
    be a stored name, not an unresolvable ping."""
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
    """One line naming a body in a list, with its member count."""
    line = body["name"]
    if body["union_code"] != union_code:
        line += " " + localized("govt_other_union_mark", lang, code=body["union_code"])
    return line

async def _send_govt_info(interaction, body, lang):
    """Send a body's card: its name, its member word, and who holds its
    seats."""
    members_text = _govt_member_lines(body, lang) or localized("govt_no_members", lang)
    embed = discord.Embed(title=body["name"], description=members_text[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    embed.set_footer(text=db.get_union_name(body["union_code"], lang))
    await _say(interaction, embed=embed)

@bot.tree.command(name="govt", description="information about a government body of this union")
@app_commands.describe(query="Name of the government body")
async def govt_cmd(interaction: discord.Interaction, query: str):
    """Show a governing body of this union and who sits in it.

    Falls back to a numbered choice when the query matches several, and to
    suggestions when it matches none."""
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
