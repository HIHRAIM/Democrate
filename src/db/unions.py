"""Unions, their localized names, the chats bound to them, and the shared
code namespace all of that lives in.

A union is a bare code plus a name per language, with a language of its own
(English until /union edit sets another) and an optional logo; a chat is one
Discord server or Telegram group, keyed by its bare id, and `/server setup` is what
binds the two. Removing a chat forgets everything scoped to that server, which
is why `remove_chat` deletes language rows, log channels and the celebration
settings too.

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

def add_union(code, names, owner_id=None):
    """Create a union with its localized names ({lang: name} dict).
    Returns False if the union already exists. The program ships with no
    unions — every union is created at runtime with /union add."""
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
    if owner_id is not None:
        cur.execute(
            "INSERT INTO sponsor_unions (code,discord_id,claimed_at) VALUES (?,?,?)",
            (str(code), str(owner_id), int(time.time()))
        )
    conn.commit()
    return True

def union_exists(code):
    """Whether a union with this code exists.

    Also one of the five probes behind `code_taken`: a union code occupies the
    same namespace as party, currency, good and enterprise codes."""
    return cur.execute("SELECT 1 FROM unions WHERE code=?", (code,)).fetchone() is not None

def get_union_codes():
    """Every union code, sorted — the list `/server setup` prints when it is given one
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
    has one in — `/union add` accepts any subset of the six, so a union may be
    named in one language and unnamed in five."""
    return {
        r["lang"]: r["name"]
        for r in cur.execute("SELECT lang, name FROM union_names WHERE code=?", (code,)).fetchall()
    }

def get_union(code):
    """The unions row (created_at, lang, logo, logo_mime), or None."""
    return cur.execute("SELECT * FROM unions WHERE code=?", (code,)).fetchone()

def union_lang(code):
    """The union's own language, which the wiki greetings speak first.
    English until /union edit sets another."""
    row = get_union(code)
    return (row["lang"] if row and row["lang"] else None) or "en"

def set_union_created(code, created_at):
    """Set the moment a union counts as founded — its birthday in /calendar
    and the date its profile shows."""
    cur.execute("UPDATE unions SET created_at=? WHERE code=?", (int(created_at), code))
    conn.commit()

def set_union_lang(code, lang):
    """Set the union's language."""
    cur.execute("UPDATE unions SET lang=? WHERE code=?", (lang, code))
    conn.commit()

def set_union_logo(code, logo, logo_mime):
    """Replace the union's logo, stored in the row like a party's."""
    cur.execute("UPDATE unions SET logo=?, logo_mime=? WHERE code=?", (logo, logo_mime, code))
    conn.commit()

def set_union_names(code, names):
    """Apply {lang: name or None} to a union's names: a name replaces the one
    in that language, None removes it. Refuses (returns False) to leave the
    union with no name at all."""
    current = get_union_names(code)
    for lang, name in names.items():
        if name:
            current[lang] = name
        else:
            current.pop(lang, None)
    if not current:
        return False
    for lang, name in names.items():
        if name:
            cur.execute("INSERT INTO union_names (code, lang, name) VALUES (?,?,?)"
                        " ON CONFLICT(code, lang) DO UPDATE SET name=excluded.name",
                        (code, lang, name))
        else:
            cur.execute("DELETE FROM union_names WHERE code=? AND lang=?", (code, lang))
    conn.commit()
    return True

def union_community_counts(code):
    """How many Discord servers and Telegram groups a union holds, as
    {'discord': n, 'telegram': m}."""
    rows = cur.execute("SELECT platform, COUNT(*) AS n FROM chats WHERE union_code=?"
                       " GROUP BY platform", (code,)).fetchall()
    counts = {"discord": 0, "telegram": 0}
    counts.update({r["platform"]: r["n"] for r in rows})
    return counts

def get_union_enterprises(code):
    """Every enterprise based in one of the union's communities, the
    communities' neutral enterprises included: the ordinary ones first, each
    group by name."""
    return cur.execute(
        "SELECT e.* FROM enterprises e JOIN chats c"
        " ON c.chat_id=e.server_id AND c.platform=e.platform"
        " WHERE c.union_code=? ORDER BY e.neutral, e.name COLLATE NOCASE, e.code",
        (code,)).fetchall()

def setup_chat(platform, prefix, union_code, setup_by, title=None):
    """Register a Discord server / Telegram group (its bare prefix id) in a union.

    `title` is the community's own name, remembered so that anything printing a
    server — a bank's central chat, a service event — has something to say when
    the bot cannot see the place at that moment. A call without one leaves any
    name already stored alone, so re-running `/server setup` never blanks it."""
    cur.execute(
        "INSERT INTO chats (platform, chat_id, union_code, setup_by, title, created_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET union_code=excluded.union_code,"
        " setup_by=excluded.setup_by, title=COALESCE(excluded.title, chats.title),"
        " neutral_disabled=0",
        (platform, str(prefix), union_code, str(setup_by), title, int(time.time()))
    )
    conn.commit()
    from db.enterprises import ensure_neutral_enterprise, rename_auto_neutral
    ensure_neutral_enterprise(platform, prefix, title)
    rename_auto_neutral(platform, prefix, title)

def set_chat_title(prefix, title):
    """Remember a community's current name, if it is bound to a union at all.

    Cheap enough to call whenever the bot happens to see the place: it touches
    one row and never creates one, so a chat nobody has `/server setup` yet stays
    absent rather than half-registered. The community's neutral enterprise
    takes the new name with it while its name is still the bot's own."""
    if not title:
        return
    cur.execute("UPDATE chats SET title=? WHERE chat_id=?", (str(title)[:200], str(prefix)))
    conn.commit()
    row = get_chat(prefix)
    if row is not None:
        from db.enterprises import rename_auto_neutral
        rename_auto_neutral(row["platform"], prefix, row["title"])

def chat_title(prefix):
    """The stored name of a server/group, or None."""
    row = get_chat(prefix)
    return row["title"] if row and row["title"] else None

def get_chat(prefix):
    """The chats row of a server/group by its bare id, or None when it was
    never `/server setup`-bound.

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
    """Forget a server/group: its union binding, every language setting, its
    economic log/tasks channels, its celebration settings and the roles the
    bot handed out in it, and any rows of the retired per-server wiki
    anniversaries."""
    prefix = str(prefix)
    from db.enterprises import delete_enterprise
    for neutral in cur.execute(
            "SELECT code FROM enterprises WHERE server_id=? AND neutral=1",
            (prefix,)).fetchall():
        delete_enterprise(neutral["code"])
    cur.execute("DELETE FROM neutral_activity WHERE server_id=?", (prefix,))
    cur.execute("DELETE FROM chats WHERE chat_id=?", (prefix,))
    cur.execute(
        "DELETE FROM chat_settings WHERE chat_id LIKE ? OR chat_id=?",
        (f"{prefix}:%", prefix)
    )
    cur.execute("DELETE FROM wiki_foundays WHERE guild_id=?", (prefix,))
    cur.execute("DELETE FROM chat_channels WHERE server_id=?", (prefix,))
    for table in ("community_settings", "wiki_greeting_off", "role_grants"):
        cur.execute(f"DELETE FROM {table} WHERE server_id=?", (prefix,))
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
