"""One Patreon role tier shared with the other bots, scoped to Dem unions."""
import logging
import time
import math

import db
from config import PATREON_GUILD_ID, PATREON_TIER_ROLES, COMMUNITY_SPONSOR_ROLE_IDS

logger = logging.getLogger("dem.sponsors")

TIER1_UNIONS = 1
TIER1_SERVERS = 2
TIER2_MULTIPLIER = 3
TIER3_MULTIPLIER = 5
TIER1_ACTIVITY_MINUTE = 60
TIER1_ACTIVITY_DAY = 3000

_activity_owner_cache = {}
_activity_denied_until = {}

def limits(tier):
    """Small fixed capacity per subscription across all Dem unions."""
    factor = (0 if tier == 0 else TIER3_MULTIPLIER if tier == 3
              else TIER2_MULTIPLIER if tier == 2 else 1)
    return {"unions": TIER1_UNIONS * factor,
            "servers": TIER1_SERVERS * factor,
            "activity_minute": TIER1_ACTIVITY_MINUTE * factor,
            "activity_day": TIER1_ACTIVITY_DAY * factor}

def status_addendum(discord_id, lang):
    """Explain role maturation, short community grace and retention."""
    from utils import localized
    row = db.cur.execute(
        "SELECT tier,grace_tier,grace_since,community_role_since"
        " FROM sponsor_tiers WHERE discord_id=?", (str(discord_id),),
    ).fetchone()
    if not row:
        return ""
    effective = db.sponsor_tier(discord_id)
    if row["tier"] == -1 and effective == 0 and row["community_role_since"]:
        days = max(1, math.ceil((7 * 86400 -
                                 (time.time() - row["community_role_since"])) / 86400))
        return localized("sponsor_community_pending", lang, days=days)
    grace = db.sponsor_grace_days(discord_id)
    if row["tier"] == 0 and grace:
        key = ("sponsor_community_grace" if row["grace_tier"] == -1
               else "sponsor_grace_active")
        return localized(key, lang, days=grace)
    if row["tier"] == 0 and effective == 0 and db.sponsor_unions(discord_id):
        return localized("sponsor_retention_warning", lang)
    return ""

async def live_tier(discord_id):
    """0/1/2 from Patreon roles, or None on a temporary lookup failure."""
    from discord_bot import bot
    guild = bot.get_guild(int(PATREON_GUILD_ID))
    if guild is None:
        return None
    try:
        member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))
    except Exception as error:
        if "404" in str(error) or "Unknown Member" in str(error):
            return 0
        logger.info("Patreon role lookup failed for %s: %s", discord_id, error)
        return None
    role_ids = {role.id for role in member.roles}
    community = bool(role_ids.intersection(map(int, COMMUNITY_SPONSOR_ROLE_IDS)))
    db.observe_community_role(discord_id, community)
    return max((int(tier) for tier, role_id in PATREON_TIER_ROLES.items()
                if int(role_id) in role_ids), default=(-1 if community else 0))

async def refresh_tier(discord_id):
    """Persist a known role state and keep the last state during an outage."""
    tier = await live_tier(discord_id)
    if tier is not None:
        db.save_sponsor_tier(discord_id, tier)
    return db.sponsor_tier(discord_id)

async def reconcile():
    """Repair missed role changes once per hour."""
    from discord_bot import bot
    guild = bot.get_guild(int(PATREON_GUILD_ID))
    discovered = set()
    if guild:
        for role_id in list(COMMUNITY_SPONSOR_ROLE_IDS) + list(PATREON_TIER_ROLES.values()):
            role = guild.get_role(int(role_id))
            if role:
                discovered.update(str(member.id) for member in role.members)
    if guild is None:
        return
    for owner in db.known_sponsor_ids() | discovered:
        await refresh_tier(owner)
    await cleanup_due_chats()
    await cleanup_archived_unions()

async def cleanup_archived_unions():
    """Retire seventy-day-old economy archives after a fresh role check."""
    checked = {}
    for union in db.due_archived_unions():
        owner = str(union["discord_id"])
        if owner not in checked:
            checked[owner] = await live_tier(owner)
            if checked[owner] is not None:
                db.save_sponsor_tier(owner, checked[owner])
        if checked[owner] is None or db.sponsor_tier(owner) != 0:
            continue
        if db.purge_archived_union(union["code"], owner):
            logger.info("expired sponsor economy archive removed (%s)", union["code"])

async def cleanup_due_chats():
    """Leave only communities still attached to a fully lapsed union."""
    import discord
    from discord_bot import bot
    from config import PATREON_GUILD_ID

    for row in db.due_sponsor_owners():
        owner = str(row["discord_id"])
        live = await live_tier(owner)
        if live is None:
            return
        db.save_sponsor_tier(owner, live)
        if live != 0 or db.sponsor_tier(owner) != 0:
            continue
        for chat in db.lapsed_owner_chats(owner):
            platform = chat["platform"]
            server = str(chat["chat_id"])
            if not db.lapsed_chat_owned_by(platform, server, owner):
                continue
            try:
                if platform == "discord":
                    if server == str(PATREON_GUILD_ID):
                        continue
                    guild = bot.get_guild(int(server))
                    if guild is None:
                        try:
                            guild = await bot.fetch_guild(int(server))
                        except discord.NotFound:
                            guild = None
                        except discord.HTTPException as error:
                            logger.warning("expired sponsor guild lookup failed (%s): %s",
                                           server, error)
                            continue
                    if guild is not None:
                        await guild.leave()
                elif platform == "telegram":
                    from telegram_bot import bot as tg_bot
                    if not await tg_bot.leave_chat(int(server)):
                        continue
                else:
                    continue
            except Exception as error:
                logger.warning("expired sponsor community leave failed (%s:%s): %s",
                               platform, server, error)
                continue
            if db.purge_lapsed_chat(platform, server, owner):
                logger.info("expired sponsor community removed (%s:%s)", platform, server)

def can_manage_union(discord_id, code):
    """Bot Admins may manage any union; sponsors only their paid ones."""
    from utils import is_admin
    return (is_admin("discord", discord_id) or
            (db.union_owner(code) == str(discord_id) and db.sponsor_tier(discord_id) != 0))

def linked_sponsor_id(telegram_id):
    """Resolve Telegram to the same quota and owner as Discord."""
    row = db.get_link_by_telegram(telegram_id)
    return str(row["discord_id"]) if row and row["discord_id"] else None

def can_setup_server(discord_id, guild_id, code):
    """A sponsor cannot rebind somebody else's server or exceed their slots."""
    if db.union_owner(code) != str(discord_id) or db.sponsor_tier(discord_id) == 0:
        return False
    existing = db.get_chat(guild_id)
    if existing and db.union_owner(existing["union_code"]) != str(discord_id):
        former = db.union_owner(existing["union_code"])
        if former is None or db.sponsor_tier(former) != 0:
            return False
    if existing and db.union_owner(existing["union_code"]) == str(discord_id):
        return True
    return db.sponsor_server_usage(discord_id) < limits(db.sponsor_tier(discord_id))["servers"]

def allow_activity_for_bank(bank_code):
    """Bound message earning across all banks in one sponsor's unions."""
    now = time.time()
    cached = _activity_owner_cache.get(str(bank_code))
    if cached and now - cached[0] < 60:
        owner = cached[1]
    else:
        bank = db.get_bank(bank_code)
        owner = db.union_owner(bank["union_code"]) if bank else None
        _activity_owner_cache[str(bank_code)] = (now, owner)
    if owner is None:
        return True
    if _activity_denied_until.get(owner, 0) > now:
        return False
    tier = db.sponsor_tier(owner)
    if tier == 0:
        _activity_denied_until[owner] = now + 60
        return False
    cap = limits(tier)
    allowed = db.spend_activity(owner, cap["activity_minute"], cap["activity_day"])
    if not allowed:
        _activity_denied_until[owner] = (int(now) // 60 + 1) * 60
    return allowed
