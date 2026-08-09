"""Wiki anniversaries: registering one, listing them, removing one, and the
posting itself.

`_post_due_foundays` is called once a minute by client.py: founday_loop, not by
a command. It looks for rows whose configured minute has passed today and whose
`last_sent_year` is not this year — a comparison rather than a timer, so a bot
that was down at 06:00 still greets the wiki when it comes back that day, and a
bot restarted three times greets it once.

Delivery prefers a webhook, so the greeting carries the wiki's own name and
avatar, and falls back to an ordinary bot message wherever the bot may not
manage webhooks. The message is bilingual when the wiki's language differs from
the chat's.

Not this module's zone: the wording (utils.py: founday_message) and the rows
(db/foundays.py).
"""
import re
from datetime import datetime, timezone

import discord
from discord import app_commands

import db
from message_relay import clean_display_name
from utils import (
    DEFAULT_EMBED_COLOR, SUPPORTED_LANGS, founday_message, get_chat_lang,
    is_admin, is_founday_today, language_name, localized, normalize_wiki_url,
    parse_founday_datetime, parse_founday_time, utc_from_epoch,
)

from discord_bot.client import _chat_key, bot, is_server_admin, logger

CHANNEL_REF_RE = re.compile(r"^<#(\d+)>$|^(\d{5,25})$")

def _parse_channel_ref(text):
    """A channel id out of a #mention or a raw id, or None."""
    m = CHANNEL_REF_RE.match((text or "").strip())
    if not m:
        return None
    return int(m.group(1) or m.group(2))

def _can_manage_webhooks(channel):
    """Whether the bot may create a webhook in this channel — the difference
    between a greeting that carries the wiki's own name and one sent as the
    bot."""
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
    """Post one wiki's anniversary greeting.

    Prefers a webhook so the message carries the wiki's name and avatar, and falls
    back to an ordinary bot message where webhooks are not allowed. The text is
    bilingual when the wiki's language differs from the chat's. Marks the row as
    sent for this year only after the message is out, so a failed send is retried
    next minute rather than skipped for a year."""
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
    """Walk every registered anniversary and post the ones due.

    Called once a minute from the client's founday loop. "Due" is a comparison, not
    a timer: today is the anniversary, the configured minute has passed, and
    `last_sent_year` is not this year — so a bot that was down at 06:00 still
    greets the wiki when it comes back that day."""
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
    """Gate the anniversary commands on Server Admin, replying if not."""
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
    """Register a wiki's anniversary, announced every year (Server Admins).

    The founding moment and the posting time are both UTC. Running it again for the
    same wiki updates the entry and clears `last_sent_year`, so a corrected date can
    still fire this year."""
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
    """List this server's registered wiki anniversaries (Server Admins)."""
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
    """Stop announcing a wiki's anniversary (Server Admins), identified by
    the link it was added with."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_founday_admin(interaction, lang):
        return

    wiki_url = normalize_wiki_url(url)
    if wiki_url is None or not db.delete_wiki_founday(interaction.guild_id, wiki_url):
        await interaction.response.send_message(localized("founday_not_found", lang), ephemeral=True)
        return
    await interaction.response.send_message(localized("founday_removed", lang, url=wiki_url))
