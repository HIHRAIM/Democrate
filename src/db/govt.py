"""Governing bodies of a union and the people holding their seats.

A body is a name plus its own words for one member and for several; it holds
no money, has no leaders and no join requests, and everything about it is one
command each way. The relation is read from both sides: /govt lists a body's
members with the party each belongs to, and db/parties.py: party_govt_seats
reads the same rows to put a seat count on a party card.

Not this module's zone: parties (db/parties.py), and the union itself
(db/unions.py).
"""
import time

from db import conn, cur

def create_govt_body(union_code, name, member_singular, member_plural):
    """Returns False if a body with this name (case-insensitive) exists in the union."""
    row = cur.execute(
        "SELECT 1 FROM govt_bodies WHERE union_code=? AND LOWER(name)=LOWER(?)",
        (union_code, name)
    ).fetchone()
    if row:
        return False
    cur.execute(
        "INSERT INTO govt_bodies (union_code, name, member_singular, member_plural, created_at)"
        " VALUES (?,?,?,?,?)",
        (union_code, name, member_singular, member_plural, int(time.time()))
    )
    conn.commit()
    return True

def get_govt_bodies(union_code=None):
    """A union's governing bodies by name, or every body in every union when
    `union_code` is omitted — the latter is what db/parties.py: party_govt_seats
    walks when counting a party's seats."""
    if union_code is None:
        return cur.execute("SELECT * FROM govt_bodies ORDER BY name").fetchall()
    return cur.execute(
        "SELECT * FROM govt_bodies WHERE union_code=? ORDER BY name",
        (union_code,)
    ).fetchall()

def get_govt_body(body_id):
    """One body by its autoincrement id, or None once it is gone. Bodies are
    addressed by id rather than by code — they are the one object in this bot
    outside the shared 4-character namespace."""
    return cur.execute("SELECT * FROM govt_bodies WHERE id=?", (body_id,)).fetchone()

def get_govt_members(body_id):
    """The people holding a body's seats, ordered by the display name stored
    when they were seated. The row carries no party: /govt looks that up per
    member through db/parties.py: find_user_party, so a member who changes
    party is shown under the new one."""
    return cur.execute(
        "SELECT * FROM govt_members WHERE body_id=? ORDER BY display_name",
        (body_id,)
    ).fetchall()
