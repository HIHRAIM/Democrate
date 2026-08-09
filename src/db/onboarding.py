"""The seven-day setup deadline: when the bot arrived somewhere and whether
that somewhere ever got set up.

One row per server or group the bot was added to *after* the rule came into
force. `rule_since` plants that moment on first call and never moves it, which
is the grandfathering line: everything the bot was already sitting in has no
row and is never examined. `settled_at`, written by the first sweep that finds
a chat bound to a union, makes the check one-shot — a binding removed years
later cannot put the bot out of a door it was long since welcomed through.

`joined_at` means different things on the two platforms. On Discord it is only
a record: `Guild.me.joined_at` is authoritative and survives a restart, so a
missed event costs nothing. On Telegram it is the clock itself, written by the
my_chat_member update that adds the bot, because Telegram publishes no join
time at all.

Not this module's zone: the policy and the sweep (setup_deadline.py), and the
retention sweeps of every other table (they live beside their own tables).
"""
import time

from db import conn, cur
from db.settings import get_setting, set_setting

def rule_since():
    """The unix time the seven-day setup deadline came into force, planted on
    first call and stable ever after.

    Every server and group the bot was already in joined before that instant,
    and the sweep leaves those alone — which is what keeps a deployment from
    walking out of its own communities the day the rule ships."""
    value = get_setting("setup_rule_since")
    if value:
        try:
            return int(value)
        except ValueError:
            pass
    now = int(time.time())
    set_setting("setup_rule_since", now)
    return now

def record_join(platform, prefix, joined_at=None):
    """Remember that the bot has just been added to a server or group.

    Does nothing when a row already exists: a Telegram promotion that follows
    the join, or a Discord GUILD_CREATE the library replays, must not restart
    a deadline — least of all a settled one."""
    cur.execute(
        "INSERT OR IGNORE INTO setup_deadlines (platform, server_id, joined_at)"
        " VALUES (?,?,?)",
        (platform, str(prefix), int(joined_at if joined_at is not None else time.time()))
    )
    conn.commit()

def get_deadline_row(platform, prefix):
    """The chat's deadline row, or None — which is what every chat from
    before the rule looks like."""
    return cur.execute(
        "SELECT * FROM setup_deadlines WHERE platform=? AND server_id=?",
        (platform, str(prefix))
    ).fetchone()

def get_pending_deadlines(platform):
    """Chats of this platform still under the deadline: recorded, and not yet
    found set up."""
    return cur.execute(
        "SELECT * FROM setup_deadlines WHERE platform=? AND settled_at IS NULL",
        (platform,)
    ).fetchall()

def mark_settled(platform, prefix):
    """Note that the chat has been through /setup, which takes it out of the
    rule for good."""
    now = int(time.time())
    cur.execute(
        "INSERT INTO setup_deadlines (platform, server_id, joined_at, settled_at)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(platform, server_id) DO UPDATE SET settled_at=excluded.settled_at",
        (platform, str(prefix), now, now)
    )
    conn.commit()

def forget_deadline(platform, prefix):
    """Drop the row once the bot has left, so that a later re-invitation is a
    fresh seven days rather than a settlement inherited from last time."""
    cur.execute(
        "DELETE FROM setup_deadlines WHERE platform=? AND server_id=?",
        (platform, str(prefix))
    )
    conn.commit()
