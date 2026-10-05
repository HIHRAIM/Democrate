"""Delegated rights: Server Admins and Localizers.

Server Admins are explicitly appointed with /setadmin. Native Discord and
Telegram permissions do not grant this role. A Localizer is also every
appointed Server Admin implicitly, for as long as they remain one.

Bot Admins are not here at all: they are hard-coded in config.py and checked
by utils.is_admin, so that the one role able to grant the others can never be
revoked through the bot.

Not this module's zone: party, bank and enterprise leadership, which are
per-object roles kept beside their objects.
"""

from db import conn, cur

def add_server_admin(platform, server_id, user_id, username=None, added_by=None):
    """Delegate bot server-admin rights to a user (set with /setadmin).
    The username, when known, is kept for the control panel's username login."""
    cur.execute(
        "INSERT INTO server_admins (platform, server_id, user_id, username, added_by, added_at)"
        " VALUES (?,?,?,?,?,strftime('%s','now'))"
        " ON CONFLICT(platform, server_id, user_id) DO UPDATE SET"
        " username=COALESCE(excluded.username, server_admins.username)",
        (platform, str(server_id), str(user_id), username,
         str(added_by) if added_by is not None else None)
    )
    conn.commit()

def remove_server_admin(platform, server_id, user_id):
    """Revoke a server-admin appointment (/remadmin); absent rows are harmless."""
    cur.execute(
        "DELETE FROM server_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    )
    conn.commit()

def is_server_admin(platform, server_id, user_id):
    """Whether /setadmin appointed this user in this community.

    Bot Admin access, where allowed, is checked separately by the caller."""
    return cur.execute(
        "SELECT 1 FROM server_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    ).fetchone() is not None

def add_localizer(platform, user_id, username=None, added_by=None):
    """Grant localizer status (set with /localizer-add): the user may edit
    this bot's localization through the control panel.  The username, when
    known, is kept for the panel's username login."""
    cur.execute(
        "INSERT INTO localizers (platform, user_id, username, added_by, added_at)"
        " VALUES (?,?,?,?,strftime('%s','now'))"
        " ON CONFLICT(platform, user_id) DO UPDATE SET"
        " username=COALESCE(excluded.username, localizers.username)",
        (platform, str(user_id), username,
         str(added_by) if added_by is not None else None)
    )
    conn.commit()

def remove_localizer(platform, user_id):
    """Revoke a delegated localizer status.  Returns True when a row existed
    (admins are localizers implicitly and have no row to remove)."""
    removed = cur.execute(
        "DELETE FROM localizers WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).rowcount
    conn.commit()
    return removed > 0

def is_localizer(platform, user_id):
    """Whether the user holds an explicit localizer grant.

    Read by the control panel rather than by any bot command. Delegated server
    admins are localizers implicitly and have no row here, so the panel checks
    both."""
    return cur.execute(
        "SELECT 1 FROM localizers WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchone() is not None
