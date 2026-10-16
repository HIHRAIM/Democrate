"""The Discord side of the celebrations: the wiki birthday post, the personal
birthday greetings, the birthday role and the wiki representative role, plus
the two resolvers the setup commands share.

None of this is a command. `daily_pass` runs once a minute from main.py:
celebration_loop and does what is due at that minute; `rep_role_pass` runs
hourly and after every wiki refresh. Every greeting is decided by
celebrations.py at src/, which owns the clock and the wording rules; this
module only finds the channels and members and sends.

**Roles are synced, never imposed.** `_sync_role` gives the role to whoever
should hold it and does not already, and takes it back only from the members
`role_grants` says the bot gave it to — so a member who had the role by hand
keeps it, a role removed by hand is not pushed back the same day, and changing
or switching off the configured role takes the old one back from exactly the
people who got it from the bot.

**A representative is somebody the bot can tie to a wiki account.** A member
of the server holds the role when one of the union's wikis lists them among
its bureaucrats, administrators or content moderators for longer than
wikis.REPRESENTATIVE_AFTER, and either their Discord username is the handle
that Fandom profile publishes, or their Discord account is already tied to
that wiki account (`/account verify`, or a reviewer's `/olympiad accept … remember`). The
role is given with the reason "<wiki username> | <the wikis they
administer>", which is what Discord's audit log shows.

Not this module's zone: reading the wikis (wikis.py), the date rules and
wording (celebrations.py), and the commands that configure all of this
(discord_bot/commands/wikis.py, discord_bot/commands/birthdays.py).
"""
import re
import time
from datetime import date, datetime, timezone

import discord

import celebrations
import db
import fandom
import wikis
from utils import get_chat_lang

from discord_bot.client import bot, logger

CHANNEL_REF_RE = re.compile(r"^<#(\d+)>$|^(\d{5,25})$")
ROLE_REF_RE = re.compile(r"^<@&(\d+)>$|^(\d{5,25})$")
OFF_WORDS = ("off", "remove", "disable", "none", "-")

def _ref_id(regex, text):
    """The id inside a mention or a bare id, or None."""
    m = regex.match((text or "").strip())
    return int(m.group(1) or m.group(2)) if m else None

def target_channel(interaction, raw):
    """The text channel a setup command names — a #mention, an id, or this
    channel when nothing was given — if it belongs to this server; else
    None."""
    if raw:
        cid = _ref_id(CHANNEL_REF_RE, raw)
        channel = interaction.guild.get_channel(cid) if cid else None
    else:
        channel = interaction.channel
    if channel is None or not hasattr(channel, "send") \
            or getattr(channel, "guild", None) is None or channel.guild.id != interaction.guild_id:
        return None
    return channel

def target_role(interaction, raw):
    """The role a setup command names (a mention or an id) in this server,
    or None. @everyone and roles managed by an integration are refused."""
    rid = _ref_id(ROLE_REF_RE, raw)
    role = interaction.guild.get_role(rid) if rid else None
    if role is None or role.is_default() or role.managed:
        return None
    return role

def can_manage_role(guild, role):
    """Whether the bot can give `role`: Manage Roles, and the role below its
    own highest one."""
    me = guild.me
    return bool(me and me.guild_permissions.manage_roles and role < me.top_role)

async def _channel(channel_id):
    """A channel by id, fetched when it is not cached; None when gone."""
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden):
            return None
    return channel

def _chunks(text, limit=2000):
    """Split a message on line breaks into pieces Discord accepts."""
    pieces, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            pieces.append(current)
            current = line[:limit]
        else:
            current = candidate[:limit]
    if current:
        pieces.append(current)
    return pieces

async def _wiki_greeting(row, today, now):
    """Post one server's wiki birthday message for `today`. Returns whether
    the day is settled — posted, nothing to post, or a channel that cannot
    take it — as opposed to a failure worth retrying next minute."""
    chat = db.get_chat(row["server_id"])
    if chat is None:
        return True
    off = db.greeting_off_wikis("discord", row["server_id"])
    rows = [w for w in db.get_union_wikis(chat["union_code"]) if w["id"] not in off]
    entries = celebrations.wiki_anniversaries(rows, date.fromisoformat(today), now)
    if not entries:
        return True
    channel = await _channel(row["wiki_channel"])
    if channel is None or not hasattr(channel, "send"):
        return True
    text = celebrations.wiki_greeting(entries, db.union_lang(chat["union_code"]))
    try:
        for piece in _chunks(text):
            await channel.send(piece, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        logger.warning("Wiki greeting not allowed in channel %s", row["wiki_channel"])
    return True

async def _birthday_greetings(row, today):
    """Greet every member of one server whose birthday is `today` in its
    birthday channel, one message each. Returns whether the day is
    settled."""
    guild = bot.get_guild(int(row["server_id"]))
    channel = await _channel(row["birthday_channel"])
    if guild is None or channel is None or not hasattr(channel, "send"):
        return True
    lang = get_chat_lang(f"{guild.id}:{channel.id}")
    chat_key = f"discord:{channel.id}"
    people = celebrations.birthday_people(
        celebrations.birthday_pairs_on(date.fromisoformat(today)))
    for person in people:
        uid = person["ids"].get("discord")
        member = guild.get_member(int(uid)) if uid else None
        if member is None or member.bot:
            continue
        user_key = db.user_key("discord", uid)
        number = celebrations.pick_greeting(chat_key, user_key)
        try:
            await channel.send(celebrations.greeting_text(number, lang, member.mention),
                               allowed_mentions=discord.AllowedMentions(users=[member]))
        except discord.HTTPException as e:
            logger.warning("Birthday greeting failed in channel %s: %s", channel.id, e)
            continue
        db.record_greeting(chat_key, user_key, number)
    return True

async def _sync_role(guild, kind, role, wanted, reasons=None):
    """Make the bot's `kind` role ('birthday' or 'rep') held by exactly the
    members in `wanted` (ids as strings), as far as the bot's own grants go.
    `role` None takes every grant back. `reasons` maps a member id to the
    text Discord's audit log shows for giving them the role; without one
    the reason names the bot and the kind of role."""
    grants = {g["user_id"]: g for g in db.get_role_grants(guild.id, kind)}
    reasons = reasons or {}
    reason = f"Democrate: {kind} role"
    for uid, grant in grants.items():
        if role is not None and grant["role_id"] == str(role.id) and uid in wanted:
            continue
        member = guild.get_member(int(uid))
        old = guild.get_role(int(grant["role_id"]))
        if member is not None and old is not None and old in member.roles:
            try:
                await member.remove_roles(old, reason=reason)
            except discord.HTTPException as e:
                logger.info("Could not take %s role back in %s: %s", kind, guild.id, e)
                continue
        db.remove_role_grant(guild.id, kind, uid)
    if role is None or not can_manage_role(guild, role):
        return
    for uid in wanted:
        grant = grants.get(uid)
        if grant is not None and grant["role_id"] == str(role.id):
            continue
        member = guild.get_member(int(uid))
        if member is None or role in member.roles:
            continue
        try:
            await member.add_roles(role, reason=reasons.get(uid, reason))
        except discord.HTTPException as e:
            logger.info("Could not give %s role in %s: %s", kind, guild.id, e)
            continue
        db.add_role_grant(guild.id, kind, uid, role.id)

async def _birthday_roles(now):
    """Give the birthday role to members inside their birthday window and
    take it back from the rest."""
    people = celebrations.birthday_people(celebrations.window_dates(now))
    for row in db.all_communities("discord"):
        guild = bot.get_guild(int(row["server_id"]))
        if guild is None:
            continue
        role = guild.get_role(int(row["birthday_role"])) if row["birthday_role"] else None
        wanted = set()
        if role is not None:
            for person in people:
                uid = person["ids"].get("discord")
                if uid and guild.get_member(int(uid)) is not None and \
                        celebrations.in_birthday_window(person["day"], person["month"],
                                                        row["tz_offset"], now):
                    wanted.add(str(uid))
        try:
            await _sync_role(guild, "birthday", role, wanted)
        except Exception as e:
            logger.warning("Birthday role sync failed in %s: %s", guild.id, e)

async def daily_pass(now=None):
    """Everything due this minute: each server's wiki birthday post and
    birthday greetings once its local ten o'clock has passed, and the
    birthday role everywhere."""
    now = now or datetime.now(timezone.utc)
    for row in db.all_communities("discord"):
        for field, channel_field, step in (
                ("wiki_marker", "wiki_channel", lambda r, t: _wiki_greeting(r, t, now)),
                ("birthday_marker", "birthday_channel", _birthday_greetings)):
            if not row[channel_field]:
                continue
            today = celebrations.greeting_due(row, field, now)
            if today is None:
                continue
            try:
                settled = await step(row, today)
            except Exception as e:
                logger.warning("%s for %s failed: %s", field, row["server_id"], e)
                settled = False
            if settled:
                db.set_community_fields("discord", row["server_id"], **{field: today})
    await _birthday_roles(now)

def _norm_fandom_name(name):
    """A wiki username as the wiki itself writes it: underscores as spaces,
    single spaces, the first letter capitalised."""
    text = " ".join((name or "").replace("_", " ").split())
    return text[:1].upper() + text[1:]

AUDIT_REASON_LIMIT = 512

def _rep_reason(names, wiki_names):
    """The audit-log reason for a representative role: the person's wiki
    username, then the wikis whose administration they belong to, as many
    as Discord's 512 characters allow."""
    head = ", ".join(sorted(names))
    text = head
    for index, wiki in enumerate(sorted(set(wiki_names), key=str.lower)):
        part = (" | " if index == 0 else ", ") + wiki
        if len(text) + len(part) > AUDIT_REASON_LIMIT - 2:
            return text + " …"
        text += part
    return text[:AUDIT_REASON_LIMIT]

def _representatives(guild, staff):
    """The members of `guild` who are one of the `staff` rows — by the
    Discord handle the Fandom profile publishes, or by an account the bot
    already ties to that wiki account — as {member id: audit-log reason},
    the reason naming their wiki account and the wikis it administers."""
    by_name = {}
    for member in guild.members:
        if not member.bot:
            by_name.setdefault(member.name.lower(), member)
    tied_names, tied_ids = {}, {}
    for acc in db.discord_fandom_accounts():
        tied_names.setdefault(_norm_fandom_name(acc["fandom_name"]), set()).add(str(acc["discord_id"]))
        if acc["fandom_userid"]:
            tied_ids.setdefault(str(acc["fandom_userid"]), set()).add(str(acc["discord_id"]))
    found = {}
    for person in staff:
        matched = set()
        handle = person["discord_handle"]
        if handle:
            member = by_name.get(fandom._norm(handle).split("#", 1)[0])
            if member is not None and fandom.handle_matches(handle, member.name, str(member)):
                matched.add(str(member.id))
        candidates = set(tied_names.get(_norm_fandom_name(person["name"]), set()))
        if person["account_key"].startswith("fandom:"):
            candidates |= tied_ids.get(person["user_id"], set())
        matched |= {uid for uid in candidates if guild.get_member(int(uid)) is not None}
        for uid in matched:
            entry = found.setdefault(uid, (set(), []))
            entry[0].add(person["name"])
            entry[1].append(person["wiki_name"])
    return {uid: _rep_reason(names, wiki_names) for uid, (names, wiki_names) in found.items()}

async def rep_role_pass():
    """Give the wiki representative role to the qualifying members of every
    server that configured one, and take it back where it no longer
    applies."""
    cutoff = int(time.time()) - wikis.REPRESENTATIVE_AFTER
    for row in db.all_communities("discord"):
        guild = bot.get_guild(int(row["server_id"]))
        if guild is None:
            continue
        role = guild.get_role(int(row["rep_role"])) if row["rep_role"] else None
        wanted = {}
        chat = db.get_chat(guild.id)
        if role is not None and chat is not None:
            staff = [s for s in db.get_union_staff(chat["union_code"])
                     if s["since"] is not None and int(s["since"]) <= cutoff]
            wanted = _representatives(guild, staff)
        try:
            await _sync_role(guild, "rep", role, set(wanted), reasons=wanted)
        except Exception as e:
            logger.warning("Representative role sync failed in %s: %s", guild.id, e)
