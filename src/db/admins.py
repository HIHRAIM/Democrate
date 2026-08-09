"""Delegated rights: Server Admins and Localizers.

Neither table is the whole answer to "may this user do X". A Server Admin is
also anyone holding the native Administrator / Manage Server permission on
Discord or creator/administrator status on Telegram — the two halves ask that
half of the question themselves (discord_bot/client.py and
telegram_bot/client.py, both `is_server_admin`). A Localizer is also every
delegated Server Admin implicitly, for as long as they remain one.

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
    """Revoke a delegated server-admin grant (/remadmin). Silent when there was
    none: a member holding the native Administrator permission has no row here
    and cannot be demoted through the bot at all."""
    cur.execute(
        "DELETE FROM server_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    )
    conn.commit()

def is_server_admin(platform, server_id, user_id):
    """Whether the user holds a *delegated* grant in this server.

    Only half the answer, and never the one a command should ask on its own:
    the native platform permission is the other half, and both halves are
    joined in discord_bot/client.py and telegram_bot/client.py."""
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
