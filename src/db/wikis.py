"""Wikis attached to unions, the administration cache read from them, and the
wikis a community chose not to greet.

A wiki is one row in `wikis` however many unions it belongs to, and
`union_wikis` is the attachment itself; detaching a wiki from its last union
deletes the row together with its cache, so nothing about a wiki outlives the
last union that wanted it. `wiki_staff` is a snapshot of the wiki's public
administration, rebuilt by main.py: wiki_refresh_loop rather than asked for
on demand — the union profile and the representative role read it many times
a day, and the wiki is asked twice a day at most.

The founding moment has two columns on purpose. `founded_at` is what the bot
detected when the wiki was attached; `founded_manual` is a Bot Admin's
correction, and `wiki_founded` lets it win without the detected value being
lost, so `/wiki founded … auto` can go back to it.

Not this module's zone: reading the wikis (wikis.py at src/), and the
community settings the greetings are timed by (db/community.py).
"""
from db import conn, cur, _now

def wiki_founded(row):
    """The founding moment greetings count from: a Bot Admin's correction
    when there is one, otherwise what was detected. None when neither is
    known."""
    if row is None:
        return None
    if row["founded_manual"] is not None:
        return int(row["founded_manual"])
    return int(row["founded_at"]) if row["founded_at"] is not None else None

def find_wiki(base_url):
    """A known wiki by its canonical root, or None."""
    return cur.execute("SELECT * FROM wikis WHERE base_url=?", (base_url,)).fetchone()

def get_wiki(wiki_id):
    """A known wiki by id, or None."""
    return cur.execute("SELECT * FROM wikis WHERE id=?", (int(wiki_id),)).fetchone()

def all_wikis():
    """Every wiki attached anywhere, for the refresh."""
    return cur.execute("SELECT * FROM wikis ORDER BY id").fetchall()

def save_wiki(info, founded_at=None):
    """Insert a wiki, or bring a known one's name, language and links up to
    date. `info` is what wikis.py: describe returned. The detected founding
    moment is written only when the row has none yet, so a refresh never
    moves a wiki's birthday. Returns the wiki id."""
    now = _now()
    cur.execute(
        "INSERT INTO wikis (base_url, api_url, main_url, name, lang, city_id, is_fandom,"
        " founded_at, refreshed_at, refresh_error, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,NULL,?)"
        " ON CONFLICT(base_url) DO UPDATE SET api_url=excluded.api_url,"
        " main_url=excluded.main_url, name=excluded.name, lang=excluded.lang,"
        " city_id=excluded.city_id, is_fandom=excluded.is_fandom,"
        " founded_at=COALESCE(wikis.founded_at, excluded.founded_at),"
        " refreshed_at=excluded.refreshed_at, refresh_error=NULL",
        (info["base_url"], info["api_url"], info["main_url"], info["name"],
         info["lang"], info.get("city_id"), 1 if info.get("is_fandom") else 0,
         founded_at, now, now))
    conn.commit()
    return find_wiki(info["base_url"])["id"]

def set_wiki_refresh_error(wiki_id, error):
    """Remember why the last refresh of a wiki failed; the cache it already
    holds is left as it was."""
    cur.execute("UPDATE wikis SET refresh_error=? WHERE id=?",
                (str(error)[:300] if error else None, int(wiki_id)))
    conn.commit()

def set_wiki_founded_manual(wiki_id, founded_at):
    """Set a Bot Admin's founding moment, or clear it with None so that the
    detected one counts again."""
    cur.execute("UPDATE wikis SET founded_manual=? WHERE id=?",
                (int(founded_at) if founded_at is not None else None, int(wiki_id)))
    conn.commit()

def attach_wiki(union_code, wiki_id, added_by):
    """Attach a wiki to a union. Returns False when it already was."""
    exists = cur.execute("SELECT 1 FROM union_wikis WHERE union_code=? AND wiki_id=?",
                         (union_code, int(wiki_id))).fetchone()
    if exists:
        return False
    cur.execute("INSERT INTO union_wikis (union_code, wiki_id, added_by, added_at)"
                " VALUES (?,?,?,?)", (union_code, int(wiki_id), str(added_by), _now()))
    conn.commit()
    return True

def detach_wiki(union_code, wiki_id):
    """Detach a wiki from a union. A wiki left in no union is forgotten
    entirely: its row, its administration cache and every community's choice
    not to greet it. Returns whether the attachment existed."""
    gone = cur.execute("DELETE FROM union_wikis WHERE union_code=? AND wiki_id=?",
                       (union_code, int(wiki_id))).rowcount > 0
    if gone and not cur.execute("SELECT 1 FROM union_wikis WHERE wiki_id=?",
                                (int(wiki_id),)).fetchone():
        cur.execute("DELETE FROM wiki_staff WHERE wiki_id=?", (int(wiki_id),))
        cur.execute("DELETE FROM wiki_greeting_off WHERE wiki_id=?", (int(wiki_id),))
        cur.execute("DELETE FROM wikis WHERE id=?", (int(wiki_id),))
    conn.commit()
    return gone

def wiki_union_codes(wiki_id):
    """The unions a wiki is attached to."""
    return [r["union_code"] for r in cur.execute(
        "SELECT union_code FROM union_wikis WHERE wiki_id=? ORDER BY union_code",
        (int(wiki_id),)).fetchall()]

def get_union_wikis(union_code):
    """Every wiki of a union, by name."""
    return cur.execute(
        "SELECT w.* FROM wikis w JOIN union_wikis u ON u.wiki_id=w.id"
        " WHERE u.union_code=? ORDER BY w.name COLLATE NOCASE, w.id",
        (union_code,)).fetchall()

def get_wiki_staff(wiki_id):
    """The cached administration of one wiki."""
    return cur.execute("SELECT * FROM wiki_staff WHERE wiki_id=? ORDER BY name",
                       (int(wiki_id),)).fetchall()

def replace_wiki_staff(wiki_id, members):
    """Make the cache of one wiki's administration exactly `members` — dicts
    with user_id, account_key, name, groups (a list), since and
    discord_handle. Whoever is missing from the list has lost the status and
    is dropped; everyone else is written over."""
    now = _now()
    keep = [str(m["user_id"]) for m in members]
    if keep:
        marks = ",".join("?" for _ in keep)
        cur.execute(f"DELETE FROM wiki_staff WHERE wiki_id=? AND user_id NOT IN ({marks})",
                    (int(wiki_id), *keep))
    else:
        cur.execute("DELETE FROM wiki_staff WHERE wiki_id=?", (int(wiki_id),))
    for m in members:
        cur.execute(
            "INSERT INTO wiki_staff (wiki_id, user_id, account_key, name, groups, since,"
            " discord_handle, seen_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(wiki_id, user_id) DO UPDATE SET account_key=excluded.account_key,"
            " name=excluded.name, groups=excluded.groups, since=excluded.since,"
            " discord_handle=excluded.discord_handle, seen_at=excluded.seen_at",
            (int(wiki_id), str(m["user_id"]), m["account_key"], m["name"],
             ",".join(m["groups"]), m["since"], m.get("discord_handle"), now))
    conn.commit()

def get_union_staff(union_code):
    """The cached administration of every wiki of a union, one row per person
    per wiki, with the wiki's name beside it."""
    return cur.execute(
        "SELECT s.*, w.name AS wiki_name FROM wiki_staff s"
        " JOIN union_wikis u ON u.wiki_id=s.wiki_id JOIN wikis w ON w.id=s.wiki_id"
        " WHERE u.union_code=?", (union_code,)).fetchall()

def union_staff_count(union_code):
    """How many different people hold an administrative status on the union's
    wikis — a person on three of them counts once."""
    return cur.execute(
        "SELECT COUNT(DISTINCT s.account_key) FROM wiki_staff s"
        " JOIN union_wikis u ON u.wiki_id=s.wiki_id WHERE u.union_code=?",
        (union_code,)).fetchone()[0]

def discord_fandom_accounts():
    """Every Fandom account the bot already ties to a Discord account, as
    rows of discord_id, fandom_name and fandom_userid (NULL when only the
    name is known): a /account verify, a reviewer's /olympiad accept … remember on the
    Discord account, or one on the Telegram account linked to it."""
    return cur.execute(
        "SELECT discord_id, fandom_name, fandom_userid FROM fandom_verifications"
        " UNION ALL SELECT user_id, fandom_name, NULL FROM wiki_accounts"
        " WHERE platform='discord'"
        " UNION ALL SELECT l.discord_id, w.fandom_name, NULL FROM wiki_accounts w"
        " JOIN account_links l ON l.telegram_id=w.user_id WHERE w.platform='telegram'"
    ).fetchall()

def set_wiki_greeting(platform, server_id, wiki_id, enabled):
    """Switch the greeting of one wiki on or off in one community."""
    if enabled:
        cur.execute("DELETE FROM wiki_greeting_off WHERE platform=? AND server_id=? AND wiki_id=?",
                    (platform, str(server_id), int(wiki_id)))
    else:
        cur.execute("INSERT OR IGNORE INTO wiki_greeting_off (platform, server_id, wiki_id)"
                    " VALUES (?,?,?)", (platform, str(server_id), int(wiki_id)))
    conn.commit()

def greeting_off_wikis(platform, server_id):
    """The ids of the wikis a community does not greet."""
    return {r["wiki_id"] for r in cur.execute(
        "SELECT wiki_id FROM wiki_greeting_off WHERE platform=? AND server_id=?",
        (platform, str(server_id))).fetchall()}
