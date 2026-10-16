"""Personal birthdays and the memory of which greeting was used.

A birthday is a day and a month, nothing more, and a linked Discord and
Telegram pair has one: `set_birthday` clears every identity of the person
before writing the canonical one (the Discord side of a linked pair), and the
readers look across both identities, so a date set from Telegram before the
link still counts after it.

`birthday_greeting_history` keeps the last five greeting numbers per channel
and per person; `record_greeting` trims both keys on every insert, so the
table never holds more than the no-repeat rule reads.

Not this module's zone: choosing a greeting and the date arithmetic
(celebrations.py at src/), sending it (the two halves' celebrations.py).
"""
from db import conn, cur, _now
from db.users import canonical_user, user_identities

HISTORY_DEPTH = 5

def set_birthday(platform, user_id, day, month, display_name=None):
    """Store a person's birthday under their canonical account, with the
    name they go by — /calendar shows it where it cannot ask a platform."""
    _kind, c_platform, c_id = canonical_user(platform, user_id)
    for p, uid in user_identities(c_platform, c_id):
        cur.execute("DELETE FROM birthdays WHERE platform=? AND user_id=?", (p, str(uid)))
    cur.execute("INSERT INTO birthdays (platform, user_id, day, month, display_name, set_at)"
                " VALUES (?,?,?,?,?,?)", (c_platform, str(c_id), int(day), int(month),
                                          (display_name or "")[:100] or None, _now()))
    conn.commit()

def clear_birthday(platform, user_id):
    """Forget a person's birthday on every identity they have, together with
    the record of which greetings they received. Returns whether there was a
    birthday."""
    _kind, c_platform, c_id = canonical_user(platform, user_id)
    gone = False
    for p, uid in user_identities(c_platform, c_id):
        gone = cur.execute("DELETE FROM birthdays WHERE platform=? AND user_id=?",
                           (p, str(uid))).rowcount > 0 or gone
        cur.execute("DELETE FROM birthday_greeting_history WHERE scope='user' AND key=?",
                    (user_key(p, uid),))
    conn.commit()
    return gone

def user_key(platform, user_id):
    """The history key of a person: their canonical account as
    'platform:id', so a linked pair shares one memory."""
    _kind, c_platform, c_id = canonical_user(platform, user_id)
    return f"{c_platform}:{c_id}"

def get_birthday(platform, user_id):
    """(day, month) of a person, looked up across their linked identities,
    or None."""
    _kind, c_platform, c_id = canonical_user(platform, user_id)
    for p, uid in user_identities(c_platform, c_id):
        row = cur.execute("SELECT day, month FROM birthdays WHERE platform=? AND user_id=?",
                          (p, str(uid))).fetchone()
        if row:
            return int(row["day"]), int(row["month"])
    return None

def birthdays_on(dates):
    """Every stored birthday falling on one of `dates` — (month, day) pairs —
    as rows with platform, user_id, day and month."""
    if not dates:
        return []
    cond = " OR ".join("(month=? AND day=?)" for _ in dates)
    params = [v for m, d in dates for v in (int(m), int(d))]
    return cur.execute(f"SELECT * FROM birthdays WHERE {cond}", params).fetchall()

def recent_greetings(scope, key, limit=HISTORY_DEPTH):
    """The greeting numbers used last for a channel (scope 'chat') or a person
    (scope 'user'), newest first."""
    return [r["number"] for r in cur.execute(
        "SELECT number FROM birthday_greeting_history WHERE scope=? AND key=?"
        " ORDER BY id DESC LIMIT ?", (scope, str(key), int(limit))).fetchall()]

def record_greeting(chat_key, user_key, number):
    """Remember that greeting `number` went to `user_key` in `chat_key`, and
    keep only the last five numbers of each."""
    now = _now()
    for scope, key in (("chat", chat_key), ("user", user_key)):
        cur.execute("INSERT INTO birthday_greeting_history (scope, key, number, used_at)"
                    " VALUES (?,?,?,?)", (scope, str(key), int(number), now))
        cur.execute(
            "DELETE FROM birthday_greeting_history WHERE scope=? AND key=? AND id NOT IN"
            " (SELECT id FROM birthday_greeting_history WHERE scope=? AND key=?"
            "  ORDER BY id DESC LIMIT ?)",
            (scope, str(key), scope, str(key), HISTORY_DEPTH))
    conn.commit()
