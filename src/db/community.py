"""Per-community settings — the celebrations and the community's tax — and
the roles the bot hands out.

`community_settings` is one row per server or group: the tax the community
charges (economy/taxes.py), the parts of the economy it switched off
(economy/switches.py), its own founding day and invitation link when its
administrators added them (/server birthday — adding them is the consent to
being named in the union's calendar and birthday posts), the single UTC
offset everything here is timed
by, the channels of the wiki and personal birthday greetings, the two
Discord roles, and the local date each greeting last went out on. The offset is one value for the whole community because the
greetings and the birthday role must agree about when a day begins; every
command that accepts one writes the same column, and the last one given wins.

`role_grants` remembers which member received which role from the bot. The
role passes take back only what is written here, which is what keeps a
mistaken role choice — a moderator role picked as the birthday role — from
being stripped from everyone who already had it.

Forgetting a community is db/unions.py: remove_chat, which clears these
tables together with everything else the bot keeps about the place.

Not this module's zone: the wikis and the opt-outs from greeting one of them
(db/wikis.py), the birthdays themselves (db/birthdays.py).
"""
from db import conn, cur, _now

_FIELDS = ("tz_offset", "wiki_channel", "birthday_channel", "birthday_role",
           "rep_role", "wiki_marker", "birthday_marker", "tax_percent", "econ_off",
           "founded_at", "invite_url")

def get_community(platform, server_id):
    """The settings row of one community, or None when nothing was set."""
    return cur.execute("SELECT * FROM community_settings WHERE platform=? AND server_id=?",
                       (platform, str(server_id))).fetchone()

def community_tz(platform, server_id):
    """The community's UTC offset in minutes; 0 until a command sets one."""
    row = get_community(platform, server_id)
    return int(row["tz_offset"]) if row and row["tz_offset"] is not None else 0

def community_tax(platform, server_id):
    """The community's tax on payments, sales and message earnings, as the
    percentage typed; 0 until /server edit sets one."""
    row = get_community(platform, server_id)
    return float(row["tax_percent"] or 0) if row else 0.0

def set_community_fields(platform, server_id, **fields):
    """Write some of a community's settings, creating the row on first use.
    A value of None clears that setting."""
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise ValueError(f"unknown community fields: {sorted(unknown)}")
    cur.execute("INSERT OR IGNORE INTO community_settings (platform, server_id) VALUES (?,?)",
                (platform, str(server_id)))
    for name, value in fields.items():
        if name in ("tz_offset", "tax_percent") and value is None:
            value = 0
        cur.execute(f"UPDATE community_settings SET {name}=? WHERE platform=? AND server_id=?",
                    (value, platform, str(server_id)))
    conn.commit()

def community_birthdays(union_code=None):
    """The communities whose administrators added their founding day
    (/server birthday) — of one union, or of every union — each with its
    union and stored name."""
    sql = ("SELECT s.*, c.union_code, c.title FROM community_settings s"
           " JOIN chats c ON c.chat_id = s.server_id AND c.platform = s.platform"
           " WHERE s.founded_at IS NOT NULL")
    if union_code is None:
        return cur.execute(sql).fetchall()
    return cur.execute(sql + " AND c.union_code=?", (union_code,)).fetchall()

def all_communities(platform=None):
    """Every community with settings, optionally on one platform only."""
    if platform is None:
        return cur.execute("SELECT * FROM community_settings").fetchall()
    return cur.execute("SELECT * FROM community_settings WHERE platform=?",
                       (platform,)).fetchall()

def get_role_grants(server_id, kind):
    """The members who hold the bot's `kind` role ('birthday' or 'rep') in a
    server because the bot gave it to them."""
    return cur.execute("SELECT * FROM role_grants WHERE server_id=? AND kind=?",
                       (str(server_id), kind)).fetchall()

def add_role_grant(server_id, kind, user_id, role_id):
    """Remember that the bot gave `role_id` to a member."""
    cur.execute(
        "INSERT INTO role_grants (server_id, kind, user_id, role_id, granted_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(server_id, kind, user_id) DO UPDATE SET"
        " role_id=excluded.role_id, granted_at=excluded.granted_at",
        (str(server_id), kind, str(user_id), str(role_id), _now()))
    conn.commit()

def remove_role_grant(server_id, kind, user_id):
    """Forget a grant — the role was taken back, or the member is gone."""
    cur.execute("DELETE FROM role_grants WHERE server_id=? AND kind=? AND user_id=?",
                (str(server_id), kind, str(user_id)))
    conn.commit()
