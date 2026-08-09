"""Parties: the party row, its founder and leaders, its members, its
alliances, the per-union switch that allows parties at all, and the join
requests waiting for a leader's answer.

Deletion is the sharp edge here. `delete_party` removes the party together
with its leaders, members, alliances, open join requests, monthly dues, its
bank accounts and any /autosend order pointing at it — the party's money goes
with it and nothing brings it back. Suspension is the reversible alternative:
the row stays, the card is marked, and joining is refused.

`party_govt_seats` reaches into the governing-body tables (db/govt.py) on
purpose: a seat count is a fact about the party, read from the other side of
the same relation.

Not this module's zone: governing bodies themselves (db/govt.py), party
accounts and their journal (db/banks.py), the monthly dues transfer
(economy/payroll.py).
"""
import time

from db import conn, cur
from db.govt import get_govt_bodies, get_govt_members
from db.unions import code_taken

def set_parties_enabled(union_code, enabled, role_ids=None):
    """Enable/disable parties in a union. role_ids: iterable of Discord role IDs
    whose holders may use /add-party (stored comma-separated)."""
    roles_str = ",".join(str(r) for r in role_ids) if role_ids else None
    cur.execute(
        "INSERT INTO union_party_settings (union_code, enabled, role_ids) VALUES (?,?,?)"
        " ON CONFLICT(union_code) DO UPDATE SET enabled=excluded.enabled,"
        " role_ids=COALESCE(excluded.role_ids, role_ids)",
        (union_code, 1 if enabled else 0, roles_str)
    )
    conn.commit()

def get_party_settings(union_code):
    """Returns (enabled: bool, role_ids: set[int]). Parties are off by default."""
    row = cur.execute(
        "SELECT enabled, role_ids FROM union_party_settings WHERE union_code=?",
        (union_code,)
    ).fetchone()
    if not row:
        return False, set()
    roles = set()
    for part in (row["role_ids"] or "").split(","):
        part = part.strip()
        if part.isdigit():
            roles.add(int(part))
    return bool(row["enabled"]), roles

def party_code_taken(code):
    """Party codes share one namespace with union, currency and good codes."""
    return code_taken(code)

def create_party(code, union_code, name, founder_platform, founder_id, founder_name,
                 logo=None, logo_mime=None):
    """Found a party and make its founder the first leader.

    The founder is written twice on purpose: onto the party row, where it cannot be
    taken away except by transferring the party, and into `party_leaders`, so that
    every "is this person a leader" check is one lookup. `code` must already have
    been checked against the shared namespace."""
    cur.execute(
        "INSERT INTO parties (code, union_code, name, founder_platform, founder_id,"
        " founder_name, logo, logo_mime, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (code, union_code, name, founder_platform, str(founder_id), founder_name,
         logo, logo_mime, int(time.time()))
    )
    cur.execute(
        "INSERT OR REPLACE INTO party_leaders (party_code, platform, user_id, display_name)"
        " VALUES (?,?,?,?)",
        (code, founder_platform, str(founder_id), founder_name)
    )
    conn.commit()

def get_party(code):
    """A party's row by code, or None. Carries the logo as a BLOB, so callers
    that only need the name should still expect a fat row."""
    return cur.execute("SELECT * FROM parties WHERE code=?", (code,)).fetchone()

def get_union_parties(union_code):
    """Every party of a union, by name — the list `/party` searches and the
    menus number."""
    return cur.execute(
        "SELECT * FROM parties WHERE union_code=? ORDER BY name",
        (union_code,)
    ).fetchall()

def get_random_union_parties(union_code, limit=3):
    """A random handful of a union's parties.

    Used to suggest what exists when someone searched for a party and found
    nothing: random rather than alphabetical, so the same three are not always the
    ones newcomers see."""
    return cur.execute(
        "SELECT * FROM parties WHERE union_code=? ORDER BY RANDOM() LIMIT ?",
        (union_code, limit)
    ).fetchall()

def find_party(query):
    """Any party of any union matched exactly (case-insensitively) by code or
    name — what /edit-party-admin resolves its argument with."""
    q = (query or "").strip()
    if not q:
        return None
    return cur.execute(
        "SELECT * FROM parties WHERE LOWER(code)=LOWER(?) OR LOWER(name)=LOWER(?)"
        " ORDER BY CASE WHEN LOWER(code)=LOWER(?) THEN 0 ELSE 1 END LIMIT 1",
        (q, q, q)
    ).fetchone()

def update_party_field(code, field, value):
    """Write one plain party column, refusing anything not on the list.

    The assert is the guard rail: the columns left out of it are the identity ones
    — code, union, founder, logo — and each has its own function because changing
    it means rewriting references or a BLOB."""
    assert field in ("name", "color", "description", "ideologies", "article_url",
                     "suspended", "allow_member_invites")
    cur.execute(f"UPDATE parties SET {field}=? WHERE code=?", (value, code))
    conn.commit()

def update_party_union(code, union_code):
    """Move a party to another union. A Bot Admin's tool: nothing else can
    change which union a party belongs to, and members are not re-checked."""
    cur.execute("UPDATE parties SET union_code=? WHERE code=?", (union_code, code))
    conn.commit()

def delete_party(code):
    """Remove a party and everything attached to it (including its economy
    footprint: bank account, monthly dues, and any autosend targeting it)."""
    cur.execute("DELETE FROM party_leaders WHERE party_code=?", (code,))
    cur.execute("DELETE FROM party_members WHERE party_code=?", (code,))
    cur.execute("DELETE FROM party_allies WHERE party_code=? OR ally_code=?", (code, code))
    cur.execute("DELETE FROM join_requests WHERE party_code=?", (code,))
    cur.execute("DELETE FROM accounts WHERE owner_type='party' AND owner_id=?", (code,))
    cur.execute("DELETE FROM party_dues WHERE party_code=?", (code,))
    cur.execute("DELETE FROM autosend WHERE target_type='party' AND target_id=?", (code,))
    cur.execute("DELETE FROM parties WHERE code=?", (code,))
    conn.commit()

def update_party_logo(code, logo, logo_mime):
    """Replace a party's logo, or clear it with (None, None). Stored as a BLOB
    in the row rather than as a file, so a party card needs no filesystem."""
    cur.execute("UPDATE parties SET logo=?, logo_mime=? WHERE code=?", (logo, logo_mime, code))
    conn.commit()

def rename_party_code(old_code, new_code):
    """Give a party a new code, rewriting every reference in one transaction.

    The lists here and in `delete_party` must stay in step: this one renames
    leaders, members and both sides of an alliance, while dues, accounts and
    autosend targets are keyed by code as well. A new table holding a party code
    belongs in both."""
    cur.execute("UPDATE parties SET code=? WHERE code=?", (new_code, old_code))
    cur.execute("UPDATE party_leaders SET party_code=? WHERE party_code=?", (new_code, old_code))
    cur.execute("UPDATE party_members SET party_code=? WHERE party_code=?", (new_code, old_code))
    cur.execute("UPDATE party_allies SET party_code=? WHERE party_code=?", (new_code, old_code))
    cur.execute("UPDATE party_allies SET ally_code=? WHERE ally_code=?", (new_code, old_code))
    conn.commit()

def get_party_leaders(code):
    """The party's appointed leaders. Does *not* include the founder, who is
    separately always a leader — `is_party_leader` joins the two."""
    return cur.execute(
        "SELECT * FROM party_leaders WHERE party_code=?", (code,)
    ).fetchall()

def add_party_leader(code, platform, user_id, display_name):
    """Appoint a co-leader without removing anyone. Always reached through a
    consent button: leadership is never imposed."""
    cur.execute(
        "INSERT OR REPLACE INTO party_leaders (party_code, platform, user_id, display_name)"
        " VALUES (?,?,?,?)",
        (code, platform, str(user_id), display_name)
    )
    conn.commit()

def set_party_leader(code, platform, user_id, display_name):
    """Transfer the party: the new user becomes the only leader."""
    cur.execute("DELETE FROM party_leaders WHERE party_code=?", (code,))
    add_party_leader(code, platform, user_id, display_name)

def is_party_leader(code, platform, user_id):
    """Whether this account leads the party, founder included.

    Asked per platform account rather than through `canonical_user`: the grant
    belongs to the account that accepted it, so a linked person leading from
    Discord does not lead from Telegram."""
    row = get_party(code)
    if row and row["founder_platform"] == platform and row["founder_id"] == str(user_id):
        return True
    return cur.execute(
        "SELECT 1 FROM party_leaders WHERE party_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))
    ).fetchone() is not None

def user_led_parties(union_code, platform, user_id):
    """Parties of the union where the user is the founder or a leader."""
    return cur.execute(
        """
        SELECT DISTINCT p.* FROM parties p
        LEFT JOIN party_leaders l ON l.party_code = p.code
        WHERE p.union_code=? AND (
            (p.founder_platform=? AND p.founder_id=?)
            OR (l.platform=? AND l.user_id=?)
        )
        ORDER BY p.name
        """,
        (union_code, platform, str(user_id), platform, str(user_id))
    ).fetchall()

def get_party_user_set(code):
    """Every (platform, user_id) belonging to the party: founder + leaders + members."""
    users = set()
    row = get_party(code)
    if row:
        users.add((row["founder_platform"], row["founder_id"]))
    for r in get_party_leaders(code):
        users.add((r["platform"], r["user_id"]))
    for r in cur.execute("SELECT * FROM party_members WHERE party_code=?", (code,)).fetchall():
        users.add((r["platform"], r["user_id"]))
    return users

def get_party_allies(code):
    """The parties allied with this one, read from both columns of the
    unordered pair and resolved to full rows.

    Nothing in the bot writes `party_allies` — no command creates an alliance —
    so today these rows can only be entered by hand. The reads and the maintenance
    in `rename_party_code` / `delete_party` are here for when one does."""
    rows = cur.execute(
        "SELECT ally_code FROM party_allies WHERE party_code=?"
        " UNION SELECT party_code FROM party_allies WHERE ally_code=?",
        (code, code)
    ).fetchall()
    out = []
    for r in rows:
        ally = get_party(r[0])
        if ally:
            out.append(ally)
    return out

def find_user_party(union_code, platform, user_id):
    """The union party a user belongs to (founder/leader/member), or None."""
    for p in get_union_parties(union_code):
        if (platform, str(user_id)) in get_party_user_set(p["code"]):
            return p
    return None

def party_govt_seats(code):
    """How many government seats (across the party's union) are held by party users."""
    party = get_party(code)
    if not party:
        return 0
    users = get_party_user_set(code)
    seats = 0
    for body in get_govt_bodies(party["union_code"]):
        for m in get_govt_members(body["id"]):
            if (m["platform"], m["user_id"]) in users:
                seats += 1
    return seats

def add_party_member(code, platform, user_id, display_name):
    """Add a rank-and-file member. Reached from an approved join request and
    from nothing else — there is no way to add somebody who did not ask."""
    cur.execute(
        "INSERT OR REPLACE INTO party_members (party_code, platform, user_id, display_name)"
        " VALUES (?,?,?,?)",
        (code, platform, str(user_id), display_name)
    )
    conn.commit()

def remove_party_member(code, platform, user_id):
    """Remove a member, whether they left or were kicked. Silent when there
    was no such row."""
    cur.execute(
        "DELETE FROM party_members WHERE party_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))
    )
    conn.commit()

def is_plain_member(code, platform, user_id):
    """True if the user is a rank-and-file member (not founder/leader)."""
    return cur.execute(
        "SELECT 1 FROM party_members WHERE party_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))
    ).fetchone() is not None

def get_party_members(code):
    """The party's rank-and-file members by display name — the founder and the
    leaders are not among them."""
    return cur.execute(
        "SELECT * FROM party_members WHERE party_code=? ORDER BY display_name",
        (code,)
    ).fetchall()

def is_founder(code, platform, user_id):
    """Whether this account founded the party.

    The founder is the only role that cannot be removed: they may transfer the
    party, but nobody else can take it. Suspending and dissolving are theirs
    alone."""
    row = get_party(code)
    return bool(row and row["founder_platform"] == platform and row["founder_id"] == str(user_id))

def remove_party_leader(code, platform, user_id):
    """Strip one leader of their appointment. Does nothing to the founder,
    whose leadership lives on the party row rather than in this table."""
    cur.execute(
        "DELETE FROM party_leaders WHERE party_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))
    )
    conn.commit()

def create_join_request(party_code, requester_platform, requester_id, requester_name, lang):
    """Record a request to join and return its id.

    The id is what the Accept/Decline buttons carry, on both platforms, so it must
    survive a restart — which is why the request is a row rather than a pending
    future. `lang` is the language the requester was speaking when they asked, so
    the answer can reach them in it later."""
    c = cur.execute(
        "INSERT INTO join_requests"
        " (party_code, requester_platform, requester_id, requester_name, lang, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (party_code, requester_platform, str(requester_id), requester_name, lang, int(time.time()))
    )
    conn.commit()
    return c.lastrowid

def get_join_request(request_id):
    """One request by id, or None once it was answered — the buttons check
    this first, because a restored Discord view outlives the request it belongs
    to."""
    return cur.execute("SELECT * FROM join_requests WHERE id=?", (request_id,)).fetchone()

def get_open_join_requests():
    """Every unanswered request in the deployment.

    Walked once on start by discord_bot/client.py: setup_hook, which re-arms a
    persistent view for each so its buttons keep working after a restart."""
    return cur.execute("SELECT * FROM join_requests").fetchall()

def has_open_join_request(party_code, requester_platform, requester_id):
    """Whether this person already has a request pending at this party — what
    stops `/party-join` from filling a leader's DMs."""
    return cur.execute(
        "SELECT 1 FROM join_requests WHERE party_code=? AND requester_platform=? AND requester_id=?",
        (party_code, requester_platform, str(requester_id))
    ).fetchone() is not None

def delete_join_request(request_id):
    """Drop a request once it has been accepted or declined. The buttons are
    left hanging; they answer "expired" when pressed again."""
    cur.execute("DELETE FROM join_requests WHERE id=?", (request_id,))
    conn.commit()

def cleanup_old_join_requests(max_age_seconds=30 * 86400):
    """Sweep requests nobody answered within a month.

    They are not refused, only forgotten: a leader who never pressed anything
    leaves no trace, and the person may ask again."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute("DELETE FROM join_requests WHERE created_at<?", (cutoff,))
    conn.commit()
