import random
import sqlite3
import threading
import time

_db_lock = threading.RLock()
_raw_conn = sqlite3.connect("dem.db", check_same_thread=False)
_raw_conn.execute("PRAGMA journal_mode=WAL;")
_raw_conn.execute("PRAGMA synchronous=NORMAL;")
_raw_conn.row_factory = sqlite3.Row

class _LockingConnection:
    """Thread-safe facade over sqlite3.Connection.

    `execute()` returns a brand new cursor on every call, so chained
    `.fetchone()/.fetchall()/.lastrowid` always operate on a private cursor.
    All access is guarded by a re-entrant lock to make concurrent use from the
    Telegram bot, the Discord bot and the background loops safe.
    """

    def __init__(self, raw_conn, lock):
        self._conn = raw_conn
        self._lock = lock

    def execute(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def executescript(self, sql):
        with self._lock:
            return self._conn.executescript(sql)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)

conn = _LockingConnection(_raw_conn, _db_lock)
cur = conn

def init():
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS unions (
        code TEXT PRIMARY KEY,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS union_names (
        code TEXT,
        lang TEXT,
        name TEXT,
        PRIMARY KEY (code, lang)
    );

    CREATE TABLE IF NOT EXISTS chats (
        platform TEXT,
        chat_id TEXT PRIMARY KEY,
        union_code TEXT,
        setup_by TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS chat_settings (
        chat_id TEXT PRIMARY KEY,
        lang TEXT
    );

    CREATE TABLE IF NOT EXISTS server_admins (
        platform TEXT NOT NULL,
        server_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        username TEXT,
        added_by TEXT,
        added_at INTEGER,
        PRIMARY KEY (platform, server_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS localizers (
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        username TEXT,
        added_by TEXT,
        added_at INTEGER,
        PRIMARY KEY (platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS loc_suggestions (
        code TEXT PRIMARY KEY,
        platform TEXT,
        user_id TEXT,
        username TEXT,
        lang TEXT,
        rkey TEXT,
        suggestion TEXT,
        ui_lang TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS union_party_settings (
        union_code TEXT PRIMARY KEY,
        enabled INTEGER DEFAULT 0,
        role_ids TEXT
    );

    CREATE TABLE IF NOT EXISTS parties (
        code TEXT PRIMARY KEY,
        union_code TEXT,
        name TEXT,
        founder_platform TEXT,
        founder_id TEXT,
        founder_name TEXT,
        color TEXT,
        description TEXT,
        ideologies TEXT,
        article_url TEXT,
        logo BLOB,
        logo_mime TEXT,
        suspended INTEGER DEFAULT 0,
        allow_member_invites INTEGER DEFAULT 0,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS party_leaders (
        party_code TEXT,
        platform TEXT,
        user_id TEXT,
        display_name TEXT,
        PRIMARY KEY (party_code, platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS party_members (
        party_code TEXT,
        platform TEXT,
        user_id TEXT,
        display_name TEXT,
        PRIMARY KEY (party_code, platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS party_allies (
        party_code TEXT,
        ally_code TEXT,
        PRIMARY KEY (party_code, ally_code)
    );

    CREATE TABLE IF NOT EXISTS govt_bodies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        union_code TEXT,
        name TEXT,
        member_singular TEXT,
        member_plural TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS govt_members (
        body_id INTEGER,
        platform TEXT,
        user_id TEXT,
        display_name TEXT,
        PRIMARY KEY (body_id, platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS fandom_verifications (
        discord_id TEXT PRIMARY KEY,
        fandom_name TEXT,
        fandom_userid TEXT,
        discord_handle TEXT,
        verified_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS account_links (
        discord_id TEXT PRIMARY KEY,
        telegram_id TEXT UNIQUE,
        linked_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS pending_links (
        platform TEXT,
        user_id TEXT,
        username TEXT,
        claimed_other TEXT,
        created_at INTEGER,
        PRIMARY KEY (platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS join_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        party_code TEXT,
        requester_platform TEXT,
        requester_id TEXT,
        requester_name TEXT,
        lang TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS quiz_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT,
        user_id TEXT,
        quiz_id TEXT,
        lang TEXT,
        result_json TEXT,
        answers_json TEXT,
        created_at INTEGER
    );

    CREATE INDEX IF NOT EXISTS idx_quiz_results_user
        ON quiz_results (platform, user_id);

    CREATE TABLE IF NOT EXISTS quiz_progress (
        platform TEXT,
        user_id TEXT,
        quiz_id TEXT,
        lang TEXT,
        qindex INTEGER,
        orders_json TEXT,
        answers_json TEXT,
        created_at INTEGER,
        PRIMARY KEY (platform, user_id, quiz_id)
    );

    CREATE TABLE IF NOT EXISTS quiz_privacy (
        platform TEXT,
        user_id TEXT,
        quiz_id TEXT,
        no_compare INTEGER DEFAULT 0,
        updated_at INTEGER,
        PRIMARY KEY (platform, user_id, quiz_id)
    );

    CREATE TABLE IF NOT EXISTS wiki_foundays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id TEXT,
        channel_id TEXT,
        url TEXT,
        name TEXT,
        founded_at INTEGER,
        hour INTEGER,
        minute INTEGER,
        lang TEXT,
        created_by TEXT,
        last_sent_year INTEGER,
        created_at INTEGER
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_foundays_guild_url
        ON wiki_foundays (guild_id, url);
    """)
    conn.commit()
    init_economy()

def get_setting(key, default=None):
    row = cur.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    cur.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    conn.commit()

def init_economy():
    """Schema for the cross-community economy: banks and their currencies,
    accounts and the append-only transaction journal, goods/inventory/mastery,
    per-channel earning, FX treaties, wages and party dues, and the autocraft /
    autosend automation. Kept in its own script so the party/quiz core above
    stays readable."""
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS banks (
        code TEXT PRIMARY KEY,
        union_code TEXT,
        central_chat TEXT,
        currency_name TEXT,
        emoji TEXT,
        value REAL DEFAULT 1.0,
        period_goods_value INTEGER DEFAULT 0,
        period_energy INTEGER DEFAULT 0,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS bank_leaders (
        bank_code TEXT,
        platform TEXT,
        user_id TEXT,
        display_name TEXT,
        PRIMARY KEY (bank_code, platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank_code TEXT,
        owner_type TEXT,
        owner_platform TEXT,
        owner_id TEXT,
        balance INTEGER DEFAULT 0,
        energy INTEGER DEFAULT 0,
        display_name TEXT,
        created_at INTEGER,
        UNIQUE (bank_code, owner_type, owner_platform, owner_id)
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank_code TEXT,
        from_type TEXT, from_platform TEXT, from_id TEXT,
        to_type TEXT, to_platform TEXT, to_id TEXT,
        amount INTEGER,
        reason TEXT,
        created_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_tx_bank ON transactions (bank_code, created_at);

    CREATE TABLE IF NOT EXISTS goods (
        code TEXT PRIMARY KEY,
        bank_code TEXT,
        name TEXT,
        base_value INTEGER,
        energy_cost INTEGER,
        emoji TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS inventory (
        owner_type TEXT, owner_platform TEXT, owner_id TEXT,
        good_code TEXT,
        qty INTEGER DEFAULT 0,
        PRIMARY KEY (owner_type, owner_platform, owner_id, good_code)
    );

    CREATE TABLE IF NOT EXISTS mastery (
        owner_type TEXT, owner_platform TEXT, owner_id TEXT,
        good_code TEXT,
        produced INTEGER DEFAULT 0,
        PRIMARY KEY (owner_type, owner_platform, owner_id, good_code)
    );

    CREATE TABLE IF NOT EXISTS professions (
        owner_type TEXT, owner_platform TEXT, owner_id TEXT,
        bank_code TEXT,
        good_code TEXT,
        PRIMARY KEY (owner_type, owner_platform, owner_id, bank_code)
    );

    CREATE TABLE IF NOT EXISTS earn_channels (
        platform TEXT,
        chan_key TEXT,
        bank_code TEXT,
        rate REAL DEFAULT 1.0,
        PRIMARY KEY (platform, chan_key)
    );

    CREATE TABLE IF NOT EXISTS treaties (
        bank_a TEXT,
        bank_b TEXT,
        pegged_rate REAL,
        created_at INTEGER,
        PRIMARY KEY (bank_a, bank_b)
    );

    CREATE TABLE IF NOT EXISTS wages (
        bank_code TEXT,
        good_code TEXT,
        amount INTEGER,
        PRIMARY KEY (bank_code, good_code)
    );

    CREATE TABLE IF NOT EXISTS party_dues (
        party_code TEXT,
        bank_code TEXT,
        amount INTEGER,
        PRIMARY KEY (party_code, bank_code)
    );

    CREATE TABLE IF NOT EXISTS autocraft (
        owner_type TEXT, owner_platform TEXT, owner_id TEXT,
        good_code TEXT,
        enabled INTEGER DEFAULT 0,
        PRIMARY KEY (owner_type, owner_platform, owner_id, good_code)
    );

    CREATE TABLE IF NOT EXISTS autosend (
        owner_type TEXT, owner_platform TEXT, owner_id TEXT,
        good_code TEXT,
        percent INTEGER,
        target_type TEXT, target_platform TEXT, target_id TEXT,
        PRIMARY KEY (owner_type, owner_platform, owner_id, good_code)
    );
    """)
    conn.commit()

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
    return cur.execute("SELECT 1 FROM unions WHERE code=?", (code,)).fetchone() is not None

def get_union_codes():
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
    return {
        r["lang"]: r["name"]
        for r in cur.execute("SELECT lang, name FROM union_names WHERE code=?", (code,)).fetchall()
    }

def setup_chat(platform, prefix, union_code, setup_by):
    """Register a Discord server / Telegram group (its bare prefix id) in a union."""
    cur.execute(
        "INSERT INTO chats (platform, chat_id, union_code, setup_by, created_at) VALUES (?,?,?,?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET union_code=excluded.union_code, setup_by=excluded.setup_by",
        (platform, str(prefix), union_code, str(setup_by), int(time.time()))
    )
    conn.commit()

def get_chat(prefix):
    return cur.execute("SELECT * FROM chats WHERE chat_id=?", (str(prefix),)).fetchone()

def is_setup(prefix):
    return get_chat(prefix) is not None

def get_union_chats(union_code):
    return cur.execute(
        "SELECT * FROM chats WHERE union_code=?",
        (union_code,)
    ).fetchall()

def remove_chat(prefix):
    """Forget a server/group: its union binding, every language setting and any
    wiki anniversaries scheduled in it."""
    prefix = str(prefix)
    cur.execute("DELETE FROM chats WHERE chat_id=?", (prefix,))
    cur.execute(
        "DELETE FROM chat_settings WHERE chat_id LIKE ? OR chat_id=?",
        (f"{prefix}:%", prefix)
    )
    cur.execute("DELETE FROM wiki_foundays WHERE guild_id=?", (prefix,))
    conn.commit()

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
    cur.execute(
        "DELETE FROM server_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    )
    conn.commit()

def is_server_admin(platform, server_id, user_id):
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
    return cur.execute(
        "SELECT 1 FROM localizers WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchone() is not None

def set_chat_lang(chat_id, lang_code):
    cur.execute(
        "INSERT INTO chat_settings (chat_id, lang) VALUES (?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET lang=excluded.lang",
        (chat_id, lang_code)
    )
    conn.commit()

def get_chat_lang(chat_id):
    """
    Lookup language for given chat_id. Resolution order:
      1. exact chat_id (channel/thread/topic — set with /locallang),
      2. bare community prefix (guild id / group chat id — set with /lang or /setup),
      3. legacy '<group_id>:0' key (group-wide behavior).
    If not found → returns None.
    """
    row = cur.execute(
        "SELECT lang FROM chat_settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if row and row["lang"]:
        return row["lang"]

    if ":" in chat_id:
        prefix = chat_id.split(":", 1)[0]
        for fallback_key in (prefix, f"{prefix}:0"):
            row = cur.execute(
                "SELECT lang FROM chat_settings WHERE chat_id=?",
                (fallback_key,)
            ).fetchone()
            if row and row["lang"]:
                return row["lang"]

    return None

def get_telegram_group_ids():
    rows = cur.execute(
        "SELECT chat_id FROM chats WHERE platform='telegram'"
    ).fetchall()
    return [r["chat_id"] for r in rows]

def add_loc_suggestion(code, platform, user_id, username, lang, rkey, suggestion, ui_lang):
    cur.execute(
        "INSERT OR REPLACE INTO loc_suggestions "
        "(code, platform, user_id, username, lang, rkey, suggestion, ui_lang, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (code, platform, str(user_id), username, lang, rkey, suggestion, ui_lang, int(time.time()))
    )
    conn.commit()

def get_loc_suggestion(code):
    return cur.execute(
        "SELECT * FROM loc_suggestions WHERE code=?",
        (code,)
    ).fetchone()

def delete_loc_suggestion(code):
    cur.execute("DELETE FROM loc_suggestions WHERE code=?", (code,))
    conn.commit()

def cleanup_old_loc_suggestions(max_age_seconds=365 * 24 * 3600):
    """Localization-suggestion dialog codes are kept at most a year."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute(
        "DELETE FROM loc_suggestions WHERE created_at IS NOT NULL AND created_at < ?",
        (cutoff,)
    )
    conn.commit()

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
    return cur.execute("SELECT * FROM parties WHERE code=?", (code,)).fetchone()

def get_union_parties(union_code):
    return cur.execute(
        "SELECT * FROM parties WHERE union_code=? ORDER BY name",
        (union_code,)
    ).fetchall()

def get_random_union_parties(union_code, limit=3):
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
    assert field in ("name", "color", "description", "ideologies", "article_url",
                     "suspended", "allow_member_invites")
    cur.execute(f"UPDATE parties SET {field}=? WHERE code=?", (value, code))
    conn.commit()

def update_party_union(code, union_code):
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
    cur.execute("UPDATE parties SET logo=?, logo_mime=? WHERE code=?", (logo, logo_mime, code))
    conn.commit()

def rename_party_code(old_code, new_code):
    cur.execute("UPDATE parties SET code=? WHERE code=?", (new_code, old_code))
    cur.execute("UPDATE party_leaders SET party_code=? WHERE party_code=?", (new_code, old_code))
    cur.execute("UPDATE party_members SET party_code=? WHERE party_code=?", (new_code, old_code))
    cur.execute("UPDATE party_allies SET party_code=? WHERE party_code=?", (new_code, old_code))
    cur.execute("UPDATE party_allies SET ally_code=? WHERE ally_code=?", (new_code, old_code))
    conn.commit()

def get_party_leaders(code):
    return cur.execute(
        "SELECT * FROM party_leaders WHERE party_code=?", (code,)
    ).fetchall()

def add_party_leader(code, platform, user_id, display_name):
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
    if union_code is None:
        return cur.execute("SELECT * FROM govt_bodies ORDER BY name").fetchall()
    return cur.execute(
        "SELECT * FROM govt_bodies WHERE union_code=? ORDER BY name",
        (union_code,)
    ).fetchall()

def get_govt_body(body_id):
    return cur.execute("SELECT * FROM govt_bodies WHERE id=?", (body_id,)).fetchone()

def get_govt_members(body_id):
    return cur.execute(
        "SELECT * FROM govt_members WHERE body_id=? ORDER BY display_name",
        (body_id,)
    ).fetchall()

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
    cur.execute(
        "INSERT OR REPLACE INTO party_members (party_code, platform, user_id, display_name)"
        " VALUES (?,?,?,?)",
        (code, platform, str(user_id), display_name)
    )
    conn.commit()

def remove_party_member(code, platform, user_id):
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
    return cur.execute(
        "SELECT * FROM party_members WHERE party_code=? ORDER BY display_name",
        (code,)
    ).fetchall()

def is_founder(code, platform, user_id):
    row = get_party(code)
    return bool(row and row["founder_platform"] == platform and row["founder_id"] == str(user_id))

def remove_party_leader(code, platform, user_id):
    cur.execute(
        "DELETE FROM party_leaders WHERE party_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))
    )
    conn.commit()

def add_fandom_verification(discord_id, fandom_name, fandom_userid, discord_handle):
    cur.execute(
        "INSERT OR REPLACE INTO fandom_verifications"
        " (discord_id, fandom_name, fandom_userid, discord_handle, verified_at)"
        " VALUES (?,?,?,?,?)",
        (str(discord_id), fandom_name, str(fandom_userid), discord_handle, int(time.time()))
    )
    conn.commit()

def get_fandom_verification(discord_id):
    return cur.execute(
        "SELECT * FROM fandom_verifications WHERE discord_id=?",
        (str(discord_id),)
    ).fetchone()

def is_discord_verified(discord_id):
    return get_fandom_verification(discord_id) is not None

def link_accounts(discord_id, telegram_id):
    """One-to-one Discord<->Telegram link; replaces any previous link on either side."""
    cur.execute("DELETE FROM account_links WHERE discord_id=? OR telegram_id=?",
                (str(discord_id), str(telegram_id)))
    cur.execute(
        "INSERT INTO account_links (discord_id, telegram_id, linked_at) VALUES (?,?,?)",
        (str(discord_id), str(telegram_id), int(time.time()))
    )
    conn.commit()

def get_link_by_telegram(telegram_id):
    return cur.execute(
        "SELECT * FROM account_links WHERE telegram_id=?", (str(telegram_id),)
    ).fetchone()

def get_link_by_discord(discord_id):
    return cur.execute(
        "SELECT * FROM account_links WHERE discord_id=?", (str(discord_id),)
    ).fetchone()

def is_telegram_verified(telegram_id):
    """A Telegram user is verified when linked to a Fandom-verified Discord account."""
    row = get_link_by_telegram(telegram_id)
    return bool(row and is_discord_verified(row["discord_id"]))

def add_pending_link(platform, user_id, username, claimed_other):
    cur.execute(
        "INSERT OR REPLACE INTO pending_links (platform, user_id, username, claimed_other, created_at)"
        " VALUES (?,?,?,?,?)",
        (platform, str(user_id), username, claimed_other, int(time.time()))
    )
    conn.commit()

def get_pending_links(platform, max_age_seconds=30 * 60):
    cutoff = int(time.time()) - max_age_seconds
    return cur.execute(
        "SELECT * FROM pending_links WHERE platform=? AND created_at>=?",
        (platform, cutoff)
    ).fetchall()

def remove_pending_link(platform, user_id):
    cur.execute(
        "DELETE FROM pending_links WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    )
    conn.commit()

def cleanup_old_pending_links(max_age_seconds=30 * 60):
    cutoff = int(time.time()) - max_age_seconds
    cur.execute("DELETE FROM pending_links WHERE created_at<?", (cutoff,))
    conn.commit()

def create_join_request(party_code, requester_platform, requester_id, requester_name, lang):
    c = cur.execute(
        "INSERT INTO join_requests"
        " (party_code, requester_platform, requester_id, requester_name, lang, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (party_code, requester_platform, str(requester_id), requester_name, lang, int(time.time()))
    )
    conn.commit()
    return c.lastrowid

def get_join_request(request_id):
    return cur.execute("SELECT * FROM join_requests WHERE id=?", (request_id,)).fetchone()

def get_open_join_requests():
    return cur.execute("SELECT * FROM join_requests").fetchall()

def has_open_join_request(party_code, requester_platform, requester_id):
    return cur.execute(
        "SELECT 1 FROM join_requests WHERE party_code=? AND requester_platform=? AND requester_id=?",
        (party_code, requester_platform, str(requester_id))
    ).fetchone() is not None

def delete_join_request(request_id):
    cur.execute("DELETE FROM join_requests WHERE id=?", (request_id,))
    conn.commit()

def cleanup_old_join_requests(max_age_seconds=30 * 86400):
    cutoff = int(time.time()) - max_age_seconds
    cur.execute("DELETE FROM join_requests WHERE created_at<?", (cutoff,))
    conn.commit()

def _name_eq(a, b):
    return (a or "").strip().lstrip("@").lower() == (b or "").strip().lstrip("@").lower()

QUIZ_HISTORY_LIMIT = 2
QUIZ_PROGRESS_TTL = 7 * 86400
QUIZ_RESULT_TTL = 10 * 365 * 86400

def user_identities(platform, user_id):
    """Every (platform, user_id) pair that is the same person: the caller plus
    the account they linked with /add-discord + /add-telegram. Quiz results are
    stored per platform but read through this set, so a linked user sees one
    shared history on both platforms."""
    ids = [(platform, str(user_id))]
    if platform == "discord":
        row = get_link_by_discord(user_id)
        if row and row["telegram_id"]:
            ids.append(("telegram", str(row["telegram_id"])))
    elif platform == "telegram":
        row = get_link_by_telegram(user_id)
        if row and row["discord_id"]:
            ids.append(("discord", str(row["discord_id"])))
    return ids

def _identity_filter(identities):
    clause = " OR ".join("(platform=? AND user_id=?)" for _ in identities)
    params = [value for pair in identities for value in pair]
    return f"({clause})", params

def save_quiz_result(platform, user_id, quiz_id, lang, result_json, answers_json):
    """Store a completed quiz attempt and drop everything older than the two
    newest attempts of that quiz. Returns the new row id."""
    c = cur.execute(
        "INSERT INTO quiz_results"
        " (platform, user_id, quiz_id, lang, result_json, answers_json, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (platform, str(user_id), quiz_id, lang, result_json, answers_json, int(time.time()))
    )
    conn.commit()
    prune_quiz_results(platform, user_id, quiz_id)
    return c.lastrowid

def prune_quiz_results(platform, user_id, quiz_id=None):
    """Keep at most QUIZ_HISTORY_LIMIT attempts per quiz across the user's linked
    accounts. Called after every save and right after two accounts are linked."""
    where, params = _identity_filter(user_identities(platform, user_id))
    if quiz_id:
        quiz_ids = [quiz_id]
    else:
        quiz_ids = [r["quiz_id"] for r in cur.execute(
            f"SELECT DISTINCT quiz_id FROM quiz_results WHERE {where}", params
        ).fetchall()]
    for qid in quiz_ids:
        rows = cur.execute(
            f"SELECT id FROM quiz_results WHERE {where} AND quiz_id=?"
            " ORDER BY created_at DESC, id DESC",
            params + [qid]
        ).fetchall()
        for row in rows[QUIZ_HISTORY_LIMIT:]:
            cur.execute("DELETE FROM quiz_results WHERE id=?", (row["id"],))
    conn.commit()

def get_quiz_result(result_id):
    return cur.execute("SELECT * FROM quiz_results WHERE id=?", (result_id,)).fetchone()

def get_user_quiz_results(platform, user_id):
    """Every stored attempt of the user, oldest first."""
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} ORDER BY created_at, id", params
    ).fetchall()

def get_user_quiz_results_for(platform, user_id, quiz_id):
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} AND quiz_id=? ORDER BY created_at, id",
        params + [quiz_id]
    ).fetchall()

def get_latest_quiz_result(platform, user_id, quiz_id):
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} AND quiz_id=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        params + [quiz_id]
    ).fetchone()

def get_previous_quiz_result(platform, user_id, quiz_id):
    """The attempt right before the latest one, or None when there is only one."""
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} AND quiz_id=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1 OFFSET 1",
        params + [quiz_id]
    ).fetchone()

def get_last_quiz_result(platform, user_id):
    """The user's most recent attempt of any quiz — /quizzes-previous uses its
    timestamp to decide whether the no-argument form is still allowed."""
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} ORDER BY created_at DESC, id DESC LIMIT 1",
        params
    ).fetchone()

def get_user_quiz_ids(platform, user_id):
    """Distinct quiz ids the user has any stored result for, in first-taken order."""
    where, params = _identity_filter(user_identities(platform, user_id))
    rows = cur.execute(
        f"SELECT quiz_id, MIN(created_at) AS first FROM quiz_results WHERE {where}"
        " GROUP BY quiz_id ORDER BY first", params
    ).fetchall()
    return [r["quiz_id"] for r in rows]

def delete_quiz_result(result_id):
    cur.execute("DELETE FROM quiz_results WHERE id=?", (result_id,))
    conn.commit()

def delete_user_quiz_results(platform, user_id, quiz_id=None):
    """Erase the user's results — all of them, or one quiz's — on every linked
    account. Returns how many rows were removed."""
    where, params = _identity_filter(user_identities(platform, user_id))
    sql = f"DELETE FROM quiz_results WHERE {where}"
    if quiz_id:
        sql += " AND quiz_id=?"
        params = params + [quiz_id]
    c = cur.execute(sql, params)
    conn.commit()
    return c.rowcount

def set_quiz_compare_blocked(platform, user_id, quiz_id, blocked):
    """Forbid or re-allow other users comparing themselves against this user.
    `quiz_id` of '' covers every quiz."""
    cur.execute(
        "INSERT INTO quiz_privacy (platform, user_id, quiz_id, no_compare, updated_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(platform, user_id, quiz_id) DO UPDATE SET"
        " no_compare=excluded.no_compare, updated_at=excluded.updated_at",
        (platform, str(user_id), quiz_id or "", 1 if blocked else 0, int(time.time()))
    )
    conn.commit()

def is_quiz_compare_blocked(platform, user_id, quiz_id=None):
    """True when the user forbade comparison — globally, or for `quiz_id`. The
    setting is honoured across their linked accounts."""
    where, params = _identity_filter(user_identities(platform, user_id))
    keys = [""] if quiz_id is None else ["", quiz_id]
    placeholders = ",".join("?" for _ in keys)
    return cur.execute(
        f"SELECT 1 FROM quiz_privacy WHERE {where} AND quiz_id IN ({placeholders})"
        " AND no_compare=1 LIMIT 1",
        params + keys
    ).fetchone() is not None

def get_blocked_quiz_ids(platform, user_id):
    where, params = _identity_filter(user_identities(platform, user_id))
    rows = cur.execute(
        f"SELECT DISTINCT quiz_id FROM quiz_privacy WHERE {where} AND no_compare=1"
        " AND quiz_id<>''", params
    ).fetchall()
    return {r["quiz_id"] for r in rows}

def save_quiz_progress(platform, user_id, quiz_id, lang, qindex, orders_json, answers_json):
    """Park an unfinished quiz so /quizzes can pick it up again within a week."""
    cur.execute(
        "INSERT INTO quiz_progress"
        " (platform, user_id, quiz_id, lang, qindex, orders_json, answers_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(platform, user_id, quiz_id) DO UPDATE SET"
        " lang=excluded.lang, qindex=excluded.qindex, orders_json=excluded.orders_json,"
        " answers_json=excluded.answers_json, created_at=excluded.created_at",
        (platform, str(user_id), quiz_id, lang, int(qindex), orders_json, answers_json,
         int(time.time()))
    )
    conn.commit()

def get_quiz_progress(platform, user_id, quiz_id, max_age_seconds=QUIZ_PROGRESS_TTL):
    cutoff = int(time.time()) - max_age_seconds
    return cur.execute(
        "SELECT * FROM quiz_progress WHERE platform=? AND user_id=? AND quiz_id=?"
        " AND created_at>=?",
        (platform, str(user_id), quiz_id, cutoff)
    ).fetchone()

def delete_quiz_progress(platform, user_id, quiz_id):
    cur.execute(
        "DELETE FROM quiz_progress WHERE platform=? AND user_id=? AND quiz_id=?",
        (platform, str(user_id), quiz_id)
    )
    conn.commit()

def cleanup_old_quiz_progress(max_age_seconds=QUIZ_PROGRESS_TTL):
    cutoff = int(time.time()) - max_age_seconds
    cur.execute("DELETE FROM quiz_progress WHERE created_at<?", (cutoff,))
    conn.commit()

def cleanup_old_quiz_results(max_age_seconds=QUIZ_RESULT_TTL):
    """A stored quiz result is never kept longer than ten years, whatever the
    user consented to."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute("DELETE FROM quiz_results WHERE created_at<?", (cutoff,))
    conn.commit()

def add_wiki_founday(guild_id, channel_id, url, name, founded_at, hour, minute, lang, created_by):
    """Register (or re-register) a wiki's anniversary announcement. A guild may
    hold one entry per wiki url; running the command again updates it."""
    cur.execute(
        "INSERT INTO wiki_foundays"
        " (guild_id, channel_id, url, name, founded_at, hour, minute, lang, created_by,"
        "  last_sent_year, created_at) VALUES (?,?,?,?,?,?,?,?,?,NULL,?)"
        " ON CONFLICT(guild_id, url) DO UPDATE SET"
        " channel_id=excluded.channel_id, name=excluded.name, founded_at=excluded.founded_at,"
        " hour=excluded.hour, minute=excluded.minute, lang=excluded.lang,"
        " created_by=excluded.created_by, last_sent_year=NULL",
        (str(guild_id), str(channel_id), url, name, int(founded_at), int(hour), int(minute),
         lang, str(created_by), int(time.time()))
    )
    conn.commit()

def get_wiki_foundays(guild_id=None):
    if guild_id is None:
        return cur.execute("SELECT * FROM wiki_foundays ORDER BY id").fetchall()
    return cur.execute(
        "SELECT * FROM wiki_foundays WHERE guild_id=? ORDER BY name",
        (str(guild_id),)
    ).fetchall()

def delete_wiki_founday(guild_id, url):
    c = cur.execute(
        "DELETE FROM wiki_foundays WHERE guild_id=? AND url=?",
        (str(guild_id), url)
    )
    conn.commit()
    return c.rowcount > 0

def delete_guild_wiki_foundays(guild_id):
    cur.execute("DELETE FROM wiki_foundays WHERE guild_id=?", (str(guild_id),))
    conn.commit()

def mark_wiki_founday_sent(founday_id, year):
    cur.execute("UPDATE wiki_foundays SET last_sent_year=? WHERE id=?", (int(year), founday_id))
    conn.commit()

def register_link_attempt(platform, user_id, username, claimed_other):
    """Record the caller's half of the /add-discord + /add-telegram handshake and
    link the accounts when the matching half already exists (each side names the
    other's username and is itself named by it). Returns
    ('linked', discord_id, telegram_id) or ('pending', None, None)."""
    add_pending_link(platform, str(user_id), username, claimed_other)
    other = "telegram" if platform == "discord" else "discord"
    for p in get_pending_links(other):
        if _name_eq(p["claimed_other"], username) and _name_eq(p["username"], claimed_other):
            discord_id = str(user_id) if platform == "discord" else p["user_id"]
            telegram_id = str(user_id) if platform == "telegram" else p["user_id"]
            link_accounts(discord_id, telegram_id)
            remove_pending_link(platform, str(user_id))
            remove_pending_link(other, p["user_id"])
            prune_quiz_results("discord", discord_id)
            return "linked", discord_id, telegram_id
    return "pending", None, None

_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

def _now():
    return int(time.time())

def canonical_user(platform, user_id):
    """The identity that owns a person's money: their Discord account when the
    two are linked, otherwise the platform account itself. Mirrors verification,
    so a linked Telegram user and their Discord account share one wallet."""
    uid = str(user_id)
    if platform == "telegram":
        row = get_link_by_telegram(uid)
        if row and row["discord_id"]:
            return "user", "discord", str(row["discord_id"])
        return "user", "telegram", uid
    return "user", "discord", uid

def party_owner(code):
    return "party", "", str(code)

def _bank_owner(code):
    return "bank", "", str(code)

def is_owner_verified(owner_type, owner_platform, owner_id):
    """Whether a *user* owner is verified (parties never are)."""
    if owner_type != "user":
        return False
    if owner_platform == "discord":
        return is_discord_verified(owner_id)
    if owner_platform == "telegram":
        return is_telegram_verified(owner_id)
    return False

def currency_code_taken(code):
    return cur.execute("SELECT 1 FROM banks WHERE code=?", (code,)).fetchone() is not None

def good_code_taken(code):
    return cur.execute("SELECT 1 FROM goods WHERE code=?", (code,)).fetchone() is not None

def code_taken(code):
    """A 4-char code must be unique across unions, parties, currencies and goods."""
    if union_exists(code) or currency_code_taken(code) or good_code_taken(code):
        return True
    return cur.execute("SELECT 1 FROM parties WHERE code=?", (code,)).fetchone() is not None

def generate_code():
    """A fresh 4-char code free across the whole shared namespace."""
    for _ in range(10000):
        code = "".join(random.choice(_CODE_ALPHABET) for _ in range(4))
        if not code_taken(code):
            return code
    return None

def create_bank(code, union_code, central_chat, currency_name, emoji,
                leader_platform, leader_id, leader_name):
    from config import ECONOMY
    with _db_lock:
        cur.execute(
            "INSERT INTO banks (code, union_code, central_chat, currency_name, emoji,"
            " value, period_goods_value, period_energy, created_at)"
            " VALUES (?,?,?,?,?,?,0,0,?)",
            (code, union_code, str(central_chat), currency_name, emoji,
             float(ECONOMY["fx_initial_value"]), _now()))
        cur.execute(
            "INSERT OR REPLACE INTO bank_leaders (bank_code, platform, user_id, display_name)"
            " VALUES (?,?,?,?)",
            (code, leader_platform, str(leader_id), leader_name))
        conn.commit()

def get_bank(code):
    return cur.execute("SELECT * FROM banks WHERE code=?", (code,)).fetchone()

def get_all_banks():
    return cur.execute("SELECT * FROM banks ORDER BY code").fetchall()

def get_banks_in_union(union_code):
    return cur.execute(
        "SELECT * FROM banks WHERE union_code=? ORDER BY code", (union_code,)).fetchall()

def get_banks_in_chat(chat_id):
    return cur.execute(
        "SELECT * FROM banks WHERE central_chat=? ORDER BY code", (str(chat_id),)).fetchall()

def set_bank_value(code, value):
    cur.execute("UPDATE banks SET value=? WHERE code=?", (float(value), code))
    conn.commit()

def add_period_energy(code, delta):
    cur.execute("UPDATE banks SET period_energy=period_energy+? WHERE code=?",
                (int(delta), code))
    conn.commit()

def add_period_goods_value(code, delta):
    cur.execute("UPDATE banks SET period_goods_value=period_goods_value+? WHERE code=?",
                (int(delta), code))
    conn.commit()

def reset_bank_periods(code):
    cur.execute("UPDATE banks SET period_goods_value=0, period_energy=0 WHERE code=?", (code,))
    conn.commit()

def get_bank_leaders(code):
    return cur.execute("SELECT * FROM bank_leaders WHERE bank_code=?", (code,)).fetchall()

def add_bank_leader(code, platform, user_id, display_name):
    cur.execute(
        "INSERT OR REPLACE INTO bank_leaders (bank_code, platform, user_id, display_name)"
        " VALUES (?,?,?,?)",
        (code, platform, str(user_id), display_name))
    conn.commit()

def set_bank_leader(code, platform, user_id, display_name):
    """Transfer: the new user becomes the bank's only leader."""
    with _db_lock:
        cur.execute("DELETE FROM bank_leaders WHERE bank_code=?", (code,))
        cur.execute(
            "INSERT OR REPLACE INTO bank_leaders (bank_code, platform, user_id, display_name)"
            " VALUES (?,?,?,?)",
            (code, platform, str(user_id), display_name))
        conn.commit()

def remove_bank_leader(code, platform, user_id):
    cur.execute(
        "DELETE FROM bank_leaders WHERE bank_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id)))
    conn.commit()

def is_bank_leader(code, platform, user_id):
    return cur.execute(
        "SELECT 1 FROM bank_leaders WHERE bank_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))).fetchone() is not None

def user_led_banks(platform, user_id):
    return cur.execute(
        "SELECT b.* FROM banks b JOIN bank_leaders l ON l.bank_code=b.code"
        " WHERE l.platform=? AND l.user_id=? ORDER BY b.code",
        (platform, str(user_id))).fetchall()

def get_account(bank_code, owner_type, owner_platform, owner_id):
    return cur.execute(
        "SELECT * FROM accounts WHERE bank_code=? AND owner_type=? AND owner_platform=?"
        " AND owner_id=?",
        (bank_code, owner_type, owner_platform, str(owner_id))).fetchone()

def ensure_account(bank_code, owner_type, owner_platform, owner_id, display_name=None):
    """Return the account id, opening the account (balance 0) on first use."""
    with _db_lock:
        row = get_account(bank_code, owner_type, owner_platform, owner_id)
        if row:
            if display_name and row["display_name"] != display_name:
                cur.execute("UPDATE accounts SET display_name=? WHERE id=?",
                            (display_name, row["id"]))
                conn.commit()
            return row["id"]
        c = cur.execute(
            "INSERT INTO accounts (bank_code, owner_type, owner_platform, owner_id,"
            " balance, energy, display_name, created_at) VALUES (?,?,?,?,0,0,?,?)",
            (bank_code, owner_type, owner_platform, str(owner_id), display_name, _now()))
        conn.commit()
        return c.lastrowid

def account_exists(bank_code, owner_type, owner_platform, owner_id):
    return get_account(bank_code, owner_type, owner_platform, owner_id) is not None

def get_owner_accounts(owner_type, owner_platform, owner_id):
    """Every account an owner holds, across all banks."""
    return cur.execute(
        "SELECT * FROM accounts WHERE owner_type=? AND owner_platform=? AND owner_id=?"
        " ORDER BY bank_code",
        (owner_type, owner_platform, str(owner_id))).fetchall()

def get_bank_accounts(bank_code, users_only=False):
    sql = "SELECT * FROM accounts WHERE bank_code=?"
    if users_only:
        sql += " AND owner_type='user'"
    return cur.execute(sql, (bank_code,)).fetchall()

def bank_money_supply(bank_code):
    row = cur.execute(
        "SELECT COALESCE(SUM(balance),0) AS s FROM accounts WHERE bank_code=?",
        (bank_code,)).fetchone()
    return row["s"] or 0

def bank_debt(bank_code):
    """Total of all negative balances, as a positive number."""
    row = cur.execute(
        "SELECT COALESCE(SUM(balance),0) AS s FROM accounts WHERE bank_code=? AND balance<0",
        (bank_code,)).fetchone()
    return -(row["s"] or 0)

def bank_account_count(bank_code):
    row = cur.execute(
        "SELECT COUNT(*) AS c FROM accounts WHERE bank_code=?", (bank_code,)).fetchone()
    return row["c"] or 0

def _record_tx(bank_code, from_owner, to_owner, amount, reason):
    ft, fp, fi = from_owner
    tt, tp, ti = to_owner
    cur.execute(
        "INSERT INTO transactions (bank_code, from_type, from_platform, from_id,"
        " to_type, to_platform, to_id, amount, reason, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (bank_code, ft, fp, str(fi), tt, tp, str(ti), int(amount), reason, _now()))

def _adjust(account_id, delta):
    cur.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (int(delta), account_id))

def mint(bank_code, owner, amount, reason, display_name=None):
    """The bank issues `amount` to an owner (message rewards, sales, wages)."""
    if amount <= 0:
        return
    ot, op, oi = owner
    with _db_lock:
        acc = ensure_account(bank_code, ot, op, oi, display_name)
        _adjust(acc, amount)
        _record_tx(bank_code, _bank_owner(bank_code), owner, amount, reason)
        conn.commit()

def burn(bank_code, owner, amount, reason, allow_negative=True):
    """An owner pays the bank (fines, dues, fees, purchases). With
    allow_negative=False the debit is refused unless funds cover it; the return
    value says whether it happened."""
    if amount <= 0:
        return False
    ot, op, oi = owner
    with _db_lock:
        acc_row = get_account(bank_code, ot, op, oi)
        if not allow_negative and (not acc_row or acc_row["balance"] < amount):
            return False
        acc = acc_row["id"] if acc_row else ensure_account(bank_code, ot, op, oi)
        _adjust(acc, -amount)
        _record_tx(bank_code, owner, _bank_owner(bank_code), amount, reason)
        conn.commit()
        return True

def transfer(bank_code, from_owner, to_owner, amount, reason,
             allow_negative=False, to_display=None):
    """Move `amount` between two owners of the *same* currency, atomically.
    Returns False when the sender lacks funds (and allow_negative is off)."""
    if amount <= 0:
        return False
    fo_t, fo_p, fo_i = from_owner
    with _db_lock:
        from_acc = get_account(bank_code, fo_t, fo_p, fo_i)
        if not from_acc:
            return False
        if not allow_negative and from_acc["balance"] < amount:
            return False
        to_t, to_p, to_i = to_owner
        to_acc = ensure_account(bank_code, to_t, to_p, to_i, to_display)
        _adjust(from_acc["id"], -amount)
        _adjust(to_acc, amount)
        _record_tx(bank_code, from_owner, to_owner, amount, reason)
        conn.commit()
        return True

def get_transactions(bank_code=None, owner=None, limit=20):
    if owner is not None:
        ot, op, oi = owner
        return cur.execute(
            "SELECT * FROM transactions WHERE (from_type=? AND from_platform=? AND from_id=?)"
            " OR (to_type=? AND to_platform=? AND to_id=?) ORDER BY id DESC LIMIT ?",
            (ot, op, str(oi), ot, op, str(oi), limit)).fetchall()
    if bank_code is not None:
        return cur.execute(
            "SELECT * FROM transactions WHERE bank_code=? ORDER BY id DESC LIMIT ?",
            (bank_code, limit)).fetchall()
    return cur.execute(
        "SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

def get_energy(bank_code, owner):
    ot, op, oi = owner
    acc = get_account(bank_code, ot, op, oi)
    return acc["energy"] if acc else 0

def add_energy(bank_code, owner, delta, display_name=None):
    ot, op, oi = owner
    with _db_lock:
        acc = ensure_account(bank_code, ot, op, oi, display_name)
        cur.execute("UPDATE accounts SET energy=energy+? WHERE id=?", (int(delta), acc))
        conn.commit()

def spend_energy(bank_code, owner, amount):
    ot, op, oi = owner
    with _db_lock:
        acc = get_account(bank_code, ot, op, oi)
        if not acc or acc["energy"] < amount:
            return False
        cur.execute("UPDATE accounts SET energy=energy-? WHERE id=?", (int(amount), acc["id"]))
        conn.commit()
        return True

def create_good(bank_code, name, base_value, energy_cost, emoji):
    code = generate_code()
    if code is None:
        return None
    cur.execute(
        "INSERT INTO goods (code, bank_code, name, base_value, energy_cost, emoji, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (code, bank_code, name, int(base_value), int(energy_cost), emoji, _now()))
    conn.commit()
    return code

def get_good(code):
    return cur.execute("SELECT * FROM goods WHERE code=?", (code,)).fetchone()

def get_bank_goods(bank_code):
    return cur.execute(
        "SELECT * FROM goods WHERE bank_code=? ORDER BY name", (bank_code,)).fetchall()

def find_bank_good(bank_code, query):
    q = (query or "").strip()
    for g in get_bank_goods(bank_code):
        if g["code"].lower() == q.lower() or g["name"].lower() == q.lower():
            return g
    return None

def get_inventory_qty(owner, good_code):
    ot, op, oi = owner
    row = cur.execute(
        "SELECT qty FROM inventory WHERE owner_type=? AND owner_platform=? AND owner_id=?"
        " AND good_code=?", (ot, op, str(oi), good_code)).fetchone()
    return row["qty"] if row else 0

def add_inventory(owner, good_code, delta):
    ot, op, oi = owner
    with _db_lock:
        cur.execute(
            "INSERT INTO inventory (owner_type, owner_platform, owner_id, good_code, qty)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(owner_type, owner_platform, owner_id, good_code)"
            " DO UPDATE SET qty=qty+excluded.qty",
            (ot, op, str(oi), good_code, int(delta)))
        conn.commit()

def get_inventory(owner):
    ot, op, oi = owner
    return cur.execute(
        "SELECT i.good_code, i.qty, g.name, g.emoji, g.bank_code FROM inventory i"
        " JOIN goods g ON g.code=i.good_code"
        " WHERE i.owner_type=? AND i.owner_platform=? AND i.owner_id=? AND i.qty>0"
        " ORDER BY g.name", (ot, op, str(oi))).fetchall()

def get_produced(owner, good_code):
    ot, op, oi = owner
    row = cur.execute(
        "SELECT produced FROM mastery WHERE owner_type=? AND owner_platform=? AND owner_id=?"
        " AND good_code=?", (ot, op, str(oi), good_code)).fetchone()
    return row["produced"] if row else 0

def add_produced(owner, good_code, delta):
    ot, op, oi = owner
    with _db_lock:
        cur.execute(
            "INSERT INTO mastery (owner_type, owner_platform, owner_id, good_code, produced)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(owner_type, owner_platform, owner_id, good_code)"
            " DO UPDATE SET produced=produced+excluded.produced",
            (ot, op, str(oi), good_code, int(delta)))
        conn.commit()

def get_bank_mastery(owner, bank_code):
    """(good_code, produced) rows for the owner across a bank's goods, most
    produced first — the basis for the auto profession."""
    ot, op, oi = owner
    return cur.execute(
        "SELECT m.good_code, m.produced FROM mastery m JOIN goods g ON g.code=m.good_code"
        " WHERE m.owner_type=? AND m.owner_platform=? AND m.owner_id=? AND g.bank_code=?"
        " AND m.produced>0 ORDER BY m.produced DESC, m.good_code",
        (ot, op, str(oi), bank_code)).fetchall()

def set_profession(owner, bank_code, good_code):
    ot, op, oi = owner
    if good_code is None:
        cur.execute(
            "DELETE FROM professions WHERE owner_type=? AND owner_platform=? AND owner_id=?"
            " AND bank_code=?", (ot, op, str(oi), bank_code))
    else:
        cur.execute(
            "INSERT INTO professions (owner_type, owner_platform, owner_id, bank_code, good_code)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(owner_type, owner_platform, owner_id, bank_code)"
            " DO UPDATE SET good_code=excluded.good_code",
            (ot, op, str(oi), bank_code, good_code))
    conn.commit()

def get_profession(owner, bank_code):
    """The good defining the owner's profession in a bank: the leader's override
    if any, otherwise the good they have produced most of. Returns a good_code or
    None."""
    ot, op, oi = owner
    row = cur.execute(
        "SELECT good_code FROM professions WHERE owner_type=? AND owner_platform=?"
        " AND owner_id=? AND bank_code=?", (ot, op, str(oi), bank_code)).fetchone()
    if row and row["good_code"]:
        return row["good_code"]
    top = get_bank_mastery(owner, bank_code)
    return top[0]["good_code"] if top else None

def set_earn_channel(platform, chan_key, bank_code, rate):
    cur.execute(
        "INSERT INTO earn_channels (platform, chan_key, bank_code, rate) VALUES (?,?,?,?)"
        " ON CONFLICT(platform, chan_key) DO UPDATE SET bank_code=excluded.bank_code,"
        " rate=excluded.rate",
        (platform, str(chan_key), bank_code, float(rate)))
    conn.commit()

def remove_earn_channel(platform, chan_key):
    c = cur.execute("DELETE FROM earn_channels WHERE platform=? AND chan_key=?",
                    (platform, str(chan_key)))
    conn.commit()
    return c.rowcount > 0

def get_earn_channel(platform, chan_key):
    return cur.execute(
        "SELECT * FROM earn_channels WHERE platform=? AND chan_key=?",
        (platform, str(chan_key))).fetchone()

def _pair(a, b):
    return (a, b) if a <= b else (b, a)

def add_treaty(a, b):
    x, y = _pair(a, b)
    cur.execute(
        "INSERT OR IGNORE INTO treaties (bank_a, bank_b, pegged_rate, created_at)"
        " VALUES (?,?,NULL,?)", (x, y, _now()))
    conn.commit()

def get_treaty(a, b):
    x, y = _pair(a, b)
    return cur.execute("SELECT * FROM treaties WHERE bank_a=? AND bank_b=?", (x, y)).fetchone()

def has_treaty(a, b):
    return get_treaty(a, b) is not None

def remove_treaty(a, b):
    x, y = _pair(a, b)
    cur.execute("DELETE FROM treaties WHERE bank_a=? AND bank_b=?", (x, y))
    conn.commit()

def treaty_partners(code):
    rows = cur.execute(
        "SELECT bank_b AS p FROM treaties WHERE bank_a=?"
        " UNION SELECT bank_a AS p FROM treaties WHERE bank_b=?", (code, code)).fetchall()
    return [r["p"] for r in rows]

def set_pegged_rate(a, b, rate):
    """Pin the pair's rate. `rate` is units of `b` per one unit of `a`; it is
    stored in the canonical (bank_a<=bank_b) orientation. Pass rate=None to clear."""
    x, y = _pair(a, b)
    stored = None
    if rate is not None:
        stored = float(rate) if a == x else 1.0 / float(rate)
    cur.execute("UPDATE treaties SET pegged_rate=? WHERE bank_a=? AND bank_b=?",
                (stored, x, y))
    conn.commit()

def set_wage(bank_code, good_code, amount):
    if amount is None:
        cur.execute("DELETE FROM wages WHERE bank_code=? AND good_code=?", (bank_code, good_code))
    else:
        cur.execute(
            "INSERT INTO wages (bank_code, good_code, amount) VALUES (?,?,?)"
            " ON CONFLICT(bank_code, good_code) DO UPDATE SET amount=excluded.amount",
            (bank_code, good_code, int(amount)))
    conn.commit()

def get_wage(bank_code, good_code):
    row = cur.execute(
        "SELECT amount FROM wages WHERE bank_code=? AND good_code=?",
        (bank_code, good_code)).fetchone()
    return row["amount"] if row else 0

def get_bank_wages(bank_code):
    return cur.execute(
        "SELECT w.good_code, w.amount, g.name, g.emoji FROM wages w"
        " JOIN goods g ON g.code=w.good_code WHERE w.bank_code=? ORDER BY g.name",
        (bank_code,)).fetchall()

def banks_with_wages():
    rows = cur.execute("SELECT DISTINCT bank_code FROM wages").fetchall()
    return [r["bank_code"] for r in rows]

def set_party_dues(party_code, bank_code, amount):
    if amount is None or amount <= 0:
        cur.execute("DELETE FROM party_dues WHERE party_code=? AND bank_code=?",
                    (party_code, bank_code))
    else:
        cur.execute(
            "INSERT INTO party_dues (party_code, bank_code, amount) VALUES (?,?,?)"
            " ON CONFLICT(party_code, bank_code) DO UPDATE SET amount=excluded.amount",
            (party_code, bank_code, int(amount)))
    conn.commit()

def get_party_dues(party_code):
    return cur.execute(
        "SELECT d.bank_code, d.amount, b.currency_name, b.emoji FROM party_dues d"
        " JOIN banks b ON b.code=d.bank_code WHERE d.party_code=?", (party_code,)).fetchall()

def all_party_dues():
    return cur.execute("SELECT * FROM party_dues").fetchall()

def set_autocraft(owner, good_code, enabled):
    ot, op, oi = owner
    cur.execute(
        "INSERT INTO autocraft (owner_type, owner_platform, owner_id, good_code, enabled)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(owner_type, owner_platform, owner_id, good_code)"
        " DO UPDATE SET enabled=excluded.enabled",
        (ot, op, str(oi), good_code, 1 if enabled else 0))
    conn.commit()

def get_autocraft_all():
    return cur.execute("SELECT * FROM autocraft WHERE enabled=1").fetchall()

def set_autosend(owner, good_code, percent, target):
    ot, op, oi = owner
    if percent is None or percent <= 0:
        cur.execute(
            "DELETE FROM autosend WHERE owner_type=? AND owner_platform=? AND owner_id=?"
            " AND good_code=?", (ot, op, str(oi), good_code))
        conn.commit()
        return
    tt, tp, ti = target
    cur.execute(
        "INSERT INTO autosend (owner_type, owner_platform, owner_id, good_code, percent,"
        " target_type, target_platform, target_id) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(owner_type, owner_platform, owner_id, good_code) DO UPDATE SET"
        " percent=excluded.percent, target_type=excluded.target_type,"
        " target_platform=excluded.target_platform, target_id=excluded.target_id",
        (ot, op, str(oi), good_code, int(percent), tt, tp, str(ti)))
    conn.commit()

def get_autosend(owner, good_code):
    ot, op, oi = owner
    return cur.execute(
        "SELECT * FROM autosend WHERE owner_type=? AND owner_platform=? AND owner_id=?"
        " AND good_code=?", (ot, op, str(oi), good_code)).fetchone()
