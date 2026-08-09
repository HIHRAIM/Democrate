"""Finding the people behind messages: `/find-with` and `/find-without`.

Both commands read a channel's history over a period and reply-mention the
authors whose messages do or do not contain the keywords. They are the only
place in this bot that walks message history, they are Server Admin only, and
nothing here deletes or bans — "moderation" means reviewing, not punishing.

What they are actually pointed at is the wiki activity that **Confederate** and
**Wiki-Bot** relay into a Discord channel: edits and forum posts arrive there as
messages, often inside an embed, and these two commands pull one person's out of
the stream. That is why `_message_corpus` searches embeds as well as message
text — a relayed edit carries almost nothing outside them.

There is no Telegram twin because there is no such relay to read on that side;
the pair belongs to Discord, not to a platform limitation.

Not this module's zone: verification, which is what these commands are usually
run alongside (commands/user.py).
"""
import asyncio
import re

import discord
from discord import app_commands

from utils import (
    get_chat_lang, is_admin, localized, parse_find_period, parse_keywords,
)

from discord_bot.client import _chat_key, _require_verified, bot, is_server_admin

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
    """Walk a channel's history over a period and reply-mention the matching
    authors.

    Serves both commands; `want_match` inverts the test. The period parser accepts a
    single day or a day plus a span, and the keyword list is semicolon-separated
    because a keyword may contain spaces. Bots and the caller's own messages are
    skipped. Long author lists are chunked — a mention list can outgrow one
    message quickly."""
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
    """Reply-mention the authors of messages in a channel that contain any of
    the keywords (Server Admins)."""
    await _find_impl(interaction, channel_id, period, keywords, want_match=True)

@bot.tree.command(name="find-without", description="reply-mention authors of messages NOT containing the keywords (server admins)")
@app_commands.describe(
    channel_id="Channel ID to scan",
    period="MM-DD-YYYY, or MM-DD-YYYY + 7D for a range of days",
    keywords="Keywords separated by semicolons",
)
async def find_without_cmd(interaction: discord.Interaction, channel_id: str, period: str, keywords: str):
    """Reply-mention the authors of messages in a channel that contain none of
    the keywords (Server Admins).

    The inverse of `/find-with`, and the one people actually run: it answers "who
    wrote here without saying the thing they were asked to say"."""
    await _find_impl(interaction, channel_id, period, keywords, want_match=False)
