"""Discord Patreon tiers and ownership of Democrate unions."""
import time
from datetime import datetime, timezone

from db import _db_lock, conn, cur

GRACE_SECONDS = 30 * 86400
COMMUNITY_WAIT_SECONDS = 7 * 86400
COMMUNITY_GRACE_SECONDS = 86400
ARCHIVE_SECONDS = 70 * 86400

def _rank(tier):
    """A community role is below every paid role but above no role."""
    return 0 if tier == -1 else -1 if tier == 0 else tier

def _community_ready(row, now):
    return bool(row["community_role_since"] is not None and
                now - int(row["community_role_since"]) >= COMMUNITY_WAIT_SECONDS)

def observe_community_role(discord_id, present):
    """Start the seven-day clock on first observation, resetting on removal."""
    owner = str(discord_id)
    now = int(time.time())
    row = cur.execute("SELECT community_role_present FROM sponsor_tiers WHERE discord_id=?",
                      (owner,)).fetchone()
    if row is None and not present:
        return
    if row is None:
        cur.execute("INSERT INTO sponsor_tiers"
                    " (discord_id,tier,checked_at,community_role_since,community_role_present)"
                    " VALUES (?,0,?,?,1)", (owner, now, now))
    elif present and not row["community_role_present"]:
        cur.execute("UPDATE sponsor_tiers SET community_role_since=?,community_role_present=1"
                    " WHERE discord_id=?", (now, owner))
    elif not present and row["community_role_present"]:
        cur.execute("UPDATE sponsor_tiers SET community_role_present=0 WHERE discord_id=?",
                    (owner,))
    conn.commit()

def paid_sponsor_tier(discord_id):
    """Tier visible on the latest successful Patreon role check."""
    row = cur.execute("SELECT tier FROM sponsor_tiers WHERE discord_id=?",
                      (str(discord_id),)).fetchone()
    return int(row[0]) if row else 0

def sponsor_tier(discord_id):
    """Effective tier after role maturity and the applicable grace period."""
    row = cur.execute("SELECT tier,grace_since,grace_tier,community_role_since,"
                      " community_role_present FROM sponsor_tiers WHERE discord_id=?",
                      (str(discord_id),)).fetchone()
    if row is None:
        return 0
    now = time.time()
    tier = int(row["tier"])
    former = int(row["grace_tier"] or 0)
    window = COMMUNITY_GRACE_SECONDS if former == -1 else GRACE_SECONDS
    if (row["grace_since"] is not None and former
            and _rank(former) > _rank(tier)
            and now - int(row["grace_since"]) < window):
        return int(row["grace_tier"])
    if tier == -1:
        return -1 if _community_ready(row, now) else 0
    return tier

def sponsor_grace_days(discord_id):
    """Whole days of continued operation after the paid role vanished."""
    row = cur.execute("SELECT tier,grace_since,grace_tier FROM sponsor_tiers WHERE discord_id=?",
                      (str(discord_id),)).fetchone()
    if not row or row["grace_since"] is None or not row["grace_tier"]:
        return 0
    window = COMMUNITY_GRACE_SECONDS if row["grace_tier"] == -1 else GRACE_SECONDS
    left = window - (time.time() - int(row["grace_since"]))
    return max(0, int((left + 86399) // 86400))

def save_sponsor_tier(discord_id, tier):
    """Remember a successful live Discord role read."""
    now = int(time.time())
    old = cur.execute("SELECT tier,grace_since,grace_tier FROM sponsor_tiers WHERE discord_id=?",
                      (str(discord_id),)).fetchone()
    previous = sponsor_tier(discord_id)
    if _rank(tier) < _rank(previous):
        grace_since, grace_tier = now, previous
    elif old and old["grace_since"] and _rank(old["grace_tier"] or 0) > _rank(tier):
        grace_since, grace_tier = old["grace_since"], old["grace_tier"]
    else:
        grace_since, grace_tier = None, None
    cur.execute(
        "INSERT INTO sponsor_tiers"
        " (discord_id,tier,checked_at,grace_since,grace_tier) VALUES (?,?,?,?,?)"
        " ON CONFLICT(discord_id) DO UPDATE SET tier=excluded.tier,"
        " checked_at=excluded.checked_at,grace_since=excluded.grace_since,"
        " grace_tier=excluded.grace_tier",
        (str(discord_id), int(tier), now, grace_since, grace_tier)
    )
    if sponsor_tier(discord_id) != 0:
        cur.execute("UPDATE sponsor_unions SET archived_at=NULL WHERE discord_id=?",
                    (str(discord_id),))
    conn.commit()

def known_sponsor_ids():
    """Every account that needs hourly reconciliation."""
    return {row[0] for row in cur.execute(
        "SELECT discord_id FROM sponsor_tiers UNION SELECT discord_id FROM sponsor_unions"
    ).fetchall()}

def union_owner(code):
    """Discord account owning a union, or None for operator unions."""
    row = cur.execute("SELECT discord_id FROM sponsor_unions WHERE code=?",
                      (str(code),)).fetchone()
    return row[0] if row else None

def sponsor_unions(discord_id):
    """One account's union codes."""
    return [row[0] for row in cur.execute(
        "SELECT code FROM sponsor_unions WHERE discord_id=? ORDER BY claimed_at,code",
        (str(discord_id),)
    ).fetchall()]

def due_sponsor_owners(now=None):
    """Owners whose role grace and following thirty-day retention elapsed."""
    instant = int(now if now is not None else time.time())
    return cur.execute(
        "SELECT * FROM sponsor_tiers WHERE tier=0 AND grace_since IS NOT NULL"
        " AND grace_tier IS NOT NULL AND grace_since +"
        " CASE WHEN grace_tier=-1 THEN 86400 ELSE 2592000 END"
        " + 2592000 <=?", (instant,)
    ).fetchall()

def lapsed_owner_chats(discord_id):
    """Communities still in unions belonging to this expired sponsor."""
    return cur.execute(
        "SELECT c.* FROM chats c JOIN sponsor_unions s"
        " ON s.code=c.union_code WHERE s.discord_id=?"
        " ORDER BY c.platform,c.chat_id", (str(discord_id),)
    ).fetchall()

def lapsed_chat_owned_by(platform, server_id, discord_id):
    """Check exact current union ownership before a bot leaves a community."""
    row = cur.execute(
        "SELECT c.union_code,s.discord_id FROM chats c JOIN sponsor_unions s"
        " ON s.code=c.union_code WHERE c.platform=? AND c.chat_id=?",
        (platform, str(server_id)),
    ).fetchone()
    return bool(row and str(row["discord_id"]) == str(discord_id)
                and not sponsor_tier(discord_id)
                and any(str(d["discord_id"]) == str(discord_id)
                        for d in due_sponsor_owners()))

def operator_takeover_union(code, old_owner):
    """A Bot Admin converts an expired sponsored union into operator work."""
    if sponsor_tier(old_owner) != 0:
        return False
    changed = cur.execute(
        "DELETE FROM sponsor_unions WHERE code=? AND discord_id=?",
        (str(code), str(old_owner)),
    )
    conn.commit()
    return bool(changed.rowcount)

def purge_lapsed_chat(platform, server_id, discord_id):
    """Delete only records keyed to one departed server or group.

    Union currencies, transactions and inventories are shared with other
    communities; their removal is a separate union-level operation. This
    pass erases the departed community's identity, message-earning channels,
    production and consumption history, and queued work on that server.
    """
    if not lapsed_chat_owned_by(platform, server_id, discord_id):
        return False
    server = str(server_id)
    bound = cur.execute("SELECT union_code FROM chats WHERE platform=? AND chat_id=?",
                        (platform, server)).fetchone()
    union_code = bound["union_code"] if bound else None
    if union_code:
        cur.execute("UPDATE enterprises SET archived_union_code=?"
                    " WHERE platform=? AND server_id=?",
                    (union_code, platform, server))
    pattern = f"{server}:%"
    for table in ("server_production", "server_consumption", "server_tasks",
                  "neutral_activity"):
        cur.execute(f"DELETE FROM {table} WHERE platform=? AND server_id=?",
                    (platform, server))
    cur.execute("DELETE FROM productions WHERE server_platform=? AND server_id=?",
                (platform, server))
    cur.execute("DELETE FROM earn_channels WHERE platform=? AND chan_key LIKE ?",
                (platform, pattern))
    cur.execute("UPDATE banks SET central_chat=NULL WHERE union_code=?"
                " AND (central_chat LIKE ? OR central_chat=?)",
                (union_code, pattern, server))
    cur.execute("DELETE FROM server_admins WHERE platform=? AND server_id=?",
                (platform, server))
    cur.execute("DELETE FROM setup_deadlines WHERE platform=? AND server_id=?",
                (platform, server))
    from db.unions import remove_chat
    remove_chat(server)
    if union_code and not cur.execute("SELECT 1 FROM chats WHERE union_code=? LIMIT 1",
                                      (union_code,)).fetchone():
        cur.execute("UPDATE sponsor_unions SET archived_at=?"
                    " WHERE code=? AND discord_id=? AND archived_at IS NULL",
                    (int(time.time()), union_code, str(discord_id)))
    conn.commit()
    return True

def due_archived_unions(now=None):
    """Sponsor unions with no community, archived for seventy full days."""
    instant = int(now if now is not None else time.time())
    return cur.execute(
        "SELECT su.* FROM sponsor_unions su WHERE su.archived_at IS NOT NULL"
        " AND su.archived_at+?<=?"
        " AND NOT EXISTS (SELECT 1 FROM chats c WHERE c.union_code=su.code)",
        (ARCHIVE_SECONDS, instant),
    ).fetchall()

def purge_archived_union(code, discord_id):
    """Erase a complete expired union only after its archive deadline.

    Bank deletion also erases residual balances in other communities because
    those balances are denominated in this retired union's currency. No
    current chat may refer to this union when this operation runs.
    """
    row = cur.execute(
        "SELECT archived_at FROM sponsor_unions WHERE code=? AND discord_id=?",
        (str(code), str(discord_id)),
    ).fetchone()
    if (not row or row["archived_at"] is None
            or time.time() < int(row["archived_at"]) + ARCHIVE_SECONDS
            or cur.execute("SELECT 1 FROM chats WHERE union_code=? LIMIT 1",
                           (str(code),)).fetchone()):
        return False
    from db.banks import delete_bank
    from db.enterprises import delete_enterprise
    from db.parties import delete_party

    for enterprise in cur.execute(
            "SELECT code FROM enterprises WHERE archived_union_code=?",
            (str(code),)).fetchall():
        delete_enterprise(enterprise["code"])
    for party in cur.execute("SELECT code FROM parties WHERE union_code=?",
                             (str(code),)).fetchall():
        delete_party(party["code"])
    for bank in cur.execute("SELECT code FROM banks WHERE union_code=?",
                            (str(code),)).fetchall():
        delete_bank(bank["code"])
    cur.execute("DELETE FROM govt_members WHERE body_id IN"
                " (SELECT id FROM govt_bodies WHERE union_code=?)", (str(code),))
    cur.execute("DELETE FROM govt_bodies WHERE union_code=?", (str(code),))
    cur.execute("DELETE FROM union_party_settings WHERE union_code=?", (str(code),))
    cur.execute("DELETE FROM union_names WHERE code=?", (str(code),))
    cur.execute("DELETE FROM unions WHERE code=?", (str(code),))
    cur.execute("DELETE FROM sponsor_unions WHERE code=? AND discord_id=?",
                (str(code), str(discord_id)))
    conn.commit()
    return True

def sponsor_server_usage(discord_id):
    """Registered Discord servers and Telegram groups in owned unions."""
    return cur.execute(
        "SELECT COUNT(DISTINCT c.chat_id) FROM chats c"
        " JOIN sponsor_unions s ON s.code=c.union_code WHERE s.discord_id=?",
        (str(discord_id),)
    ).fetchone()[0]

def activity_usage(discord_id):
    """Today's eligible earning messages for this subscription."""
    day = datetime.now(timezone.utc).date().isoformat()
    row = cur.execute(
        "SELECT day_units FROM sponsor_activity_usage WHERE discord_id=? AND day=?",
        (str(discord_id), day)
    ).fetchone()
    return int(row[0]) if row else 0

def spend_activity(discord_id, minute_limit, day_limit):
    """Reserve one eligible earning event under both account-wide caps."""
    now = int(time.time())
    minute = now // 60
    day = datetime.now(timezone.utc).date().isoformat()
    with _db_lock:
        changed = cur.execute(
            "INSERT INTO sponsor_activity_usage"
            " (discord_id,day,day_units,minute_started,minute_units)"
            " VALUES (?,?,1,?,1) ON CONFLICT(discord_id,day) DO UPDATE SET"
            " day_units=day_units+1,minute_started=excluded.minute_started,"
            " minute_units=CASE WHEN minute_started=excluded.minute_started"
            " THEN minute_units+1 ELSE 1 END"
            " WHERE day_units<? AND (minute_started<>excluded.minute_started"
            " OR minute_units<?)",
            (str(discord_id), day, minute, int(day_limit), int(minute_limit))
        )
        conn.commit()
        return bool(changed.rowcount)
