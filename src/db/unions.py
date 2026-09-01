"""Unions, their localized names, the chats bound to them, and the shared
code namespace all of that lives in.

A union is a bare code plus a name per language; a chat is one Discord server
or Telegram group, keyed by its bare id, and `/setup` is what binds the two.
Removing a chat forgets everything scoped to that server, which is why
`remove_chat` deletes language rows, wiki anniversaries and log channels too.

The second half of this module is the namespace: union, party, currency, good
and enterprise codes are checked against each other by `code_taken`, so the
same four characters can never mean two things in one union. `generate_code`
draws against that same check. A code is a primary key, so renaming one is a
rewrite of every reference (see rename_bank_code, rename_party_code,
rename_enterprise_code in their own modules) rather than an UPDATE.

Not this module's zone: the per-chat language itself (db/settings.py), the
seven-day deadline attached to a join (db/onboarding.py), and the party
switch a union carries (db/parties.py).
"""
import random
import time

from db import conn, cur

def add_union(code, names):
    """Create a union with its localized names ({lang: name} dict).
    Returns False if the union already exists. The program ships with no
    unions — every union is created at runtime with /add-unia."""
    if union_exists(code):
        return False
    cur.execute(
        "INSERT INTO unions (code, created_at) VALUES (?,?)",
        (code, int(time.time()))
    )
    for lang, name in names.items():
        cur.execute(
            "INSERT OR IGNORE INTO union_names (code, lang, name) VALUES (?,?,?)",
            (code, lang, name)
        )
    conn.commit()
    return True

def union_exists(code):
    """Whether a union with this code exists.

    Also one of the five probes behind `code_taken`: a union code occupies the
    same namespace as party, currency, good and enterprise codes."""
    return cur.execute("SELECT 1 FROM unions WHERE code=?", (code,)).fetchone() is not None

def get_union_codes():
    """Every union code, sorted — the list `/setup` prints when it is given one
    it does not know."""
    return [r["code"] for r in cur.execute("SELECT code FROM unions ORDER BY code").fetchall()]

def get_union_name(code, lang, fallback="en"):
    """Union display name in `lang`, falling back to `fallback`, then to any
    available name, then to the bare code."""
    for L in (lang, fallback):
        row = cur.execute(
            "SELECT name FROM union_names WHERE code=? AND lang=?",
            (code, L)
        ).fetchone()
        if row:
            return row["name"]
    row = cur.execute(
        "SELECT name FROM union_names WHERE code=? ORDER BY lang LIMIT 1",
        (code,)
    ).fetchone()
    return row["name"] if row else code

def get_union_names(code):
    """All of a union's names as {lang: name}, for the languages it actually
    has one in — `/add-unia` accepts any subset of the six, so a union may be
    named in one language and unnamed in five."""
    return {
        r["lang"]: r["name"]
        for r in cur.execute("SELECT lang, name FROM union_names WHERE code=?", (code,)).fetchall()
    }

def setup_chat(platform, prefix, union_code, setup_by, title=None):
    """Register a Discord server / Telegram group (its bare prefix id) in a union.

    `title` is the community's own name, remembered so that anything printing a
    server — a bank's central chat, a service event — has something to say when
    the bot cannot see the place at that moment. A call without one leaves any
    name already stored alone, so re-running `/setup` never blanks it."""
    cur.execute(
        "INSERT INTO chats (platform, chat_id, union_code, setup_by, title, created_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET union_code=excluded.union_code,"
        " setup_by=excluded.setup_by, title=COALESCE(excluded.title, chats.title)",
        (platform, str(prefix), union_code, str(setup_by), title, int(time.time()))
    )
    conn.commit()

def set_chat_title(prefix, title):
    """Remember a community's current name, if it is bound to a union at all.

    Cheap enough to call whenever the bot happens to see the place: it touches
    one row and never creates one, so a chat nobody has `/setup` yet stays
    absent rather than half-registered."""
    if not title:
        return
    cur.execute("UPDATE chats SET title=? WHERE chat_id=?", (str(title)[:200], str(prefix)))
    conn.commit()

def chat_title(prefix):
    """The stored name of a server/group, or None."""
    row = get_chat(prefix)
    return row["title"] if row and row["title"] else None

def get_chat(prefix):
    """The chats row of a server/group by its bare id, or None when it was
    never `/setup`-bound.

    `prefix` is the community id and never a channel or topic key: this is the
    row that answers "which union is this?", and almost every public command
    starts by asking it."""
    return cur.execute("SELECT * FROM chats WHERE chat_id=?", (str(prefix),)).fetchone()

def is_setup(prefix):
    """Whether the server/group is bound to a union — the gate most public
    commands refuse on, and what the seven-day deadline sweep looks for."""
    return get_chat(prefix) is not None

def get_union_chats(union_code):
    """Every server and group bound to one union, both platforms mixed."""
    return cur.execute(
        "SELECT * FROM chats WHERE union_code=?",
        (union_code,)
    ).fetchall()

def remove_chat(prefix):
    """Forget a server/group: its union binding, every language setting, any
    wiki anniversaries scheduled in it and its economic log/tasks channels."""
    prefix = str(prefix)
    cur.execute("DELETE FROM chats WHERE chat_id=?", (prefix,))
    cur.execute(
        "DELETE FROM chat_settings WHERE chat_id LIKE ? OR chat_id=?",
        (f"{prefix}:%", prefix)
    )
    cur.execute("DELETE FROM wiki_foundays WHERE guild_id=?", (prefix,))
    cur.execute("DELETE FROM chat_channels WHERE server_id=?", (prefix,))
    conn.commit()

def get_telegram_group_ids():
    """The ids of every set-up Telegram group, for the loops that have to reach
    all of them (the Discord half walks its client's guild list instead)."""
    rows = cur.execute(
        "SELECT chat_id FROM chats WHERE platform='telegram'"
    ).fetchall()
    return [r["chat_id"] for r in rows]

_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

def currency_code_taken(code):
    """Whether a bank already uses this currency code. One of the five probes
    behind `code_taken`; a deleted bank frees its code again."""
    return cur.execute("SELECT 1 FROM banks WHERE code=?", (code,)).fetchone() is not None

def good_code_taken(code):
    """Whether a good already uses this code — including the five base goods,
    which occupy FOOD, HYGN, WARD, PHRM and HOUS permanently."""
    return cur.execute("SELECT 1 FROM goods WHERE code=?", (code,)).fetchone() is not None

def enterprise_code_taken(code):
    """Whether an enterprise already uses this code."""
    return cur.execute("SELECT 1 FROM enterprises WHERE code=?", (code,)).fetchone() is not None

def code_taken(code):
    """A 4-char code must be unique across unions, parties, currencies, goods
    and enterprises."""
    if union_exists(code) or currency_code_taken(code) or good_code_taken(code):
        return True
    if enterprise_code_taken(code):
        return True
    return cur.execute("SELECT 1 FROM parties WHERE code=?", (code,)).fetchone() is not None

def generate_code():
    """A fresh 4-char code free across the whole shared namespace.

    Draws at random and re-checks, rather than counting up, so codes carry no
    creation order. Returns None after 10000 failed draws — with 36^4 codes
    that means the namespace is effectively full, and the caller must say so
    rather than hand out a duplicate."""
    for _ in range(10000):
        code = "".join(random.choice(_CODE_ALPHABET) for _ in range(4))
        if not code_taken(code):
            return code
    return None

def all_chats():
    """Every set-up server and group — for the loops that must walk them all."""
    return cur.execute("SELECT * FROM chats ORDER BY chat_id").fetchall()
