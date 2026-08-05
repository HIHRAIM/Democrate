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
    _migrate_core()
    init_economy()
    init_olympiad()

def _migrate_core():
    """Schema upgrades for the party/quiz core. `chat_settings` learns when a
    language was chosen and whether the chat is a private conversation with the
    bot, so the one-year retention of DM languages can find its rows without
    guessing at id shapes."""
    with _db_lock:
        _ensure_column("chat_settings", "updated_at", "INTEGER")
        _ensure_column("chat_settings", "is_dm", "INTEGER DEFAULT 0")

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

    CREATE TABLE IF NOT EXISTS enterprises (
        code TEXT PRIMARY KEY,
        name TEXT,
        platform TEXT,
        server_id TEXT,
        founder_platform TEXT,
        founder_id TEXT,
        founder_name TEXT,
        description TEXT,
        logo BLOB,
        logo_mime TEXT,
        salary_period TEXT DEFAULT 'monthly',
        salary_bank TEXT,
        period_sales INTEGER DEFAULT 0,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS enterprise_leaders (
        enterprise_code TEXT,
        platform TEXT,
        user_id TEXT,
        display_name TEXT,
        PRIMARY KEY (enterprise_code, platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS enterprise_members (
        enterprise_code TEXT,
        platform TEXT,
        user_id TEXT,
        display_name TEXT,
        position TEXT,
        PRIMARY KEY (enterprise_code, platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS enterprise_positions (
        enterprise_code TEXT,
        position TEXT,
        amount INTEGER,
        percent REAL,
        PRIMARY KEY (enterprise_code, position)
    );

    CREATE TABLE IF NOT EXISTS enterprise_salaries (
        enterprise_code TEXT,
        platform TEXT,
        user_id TEXT,
        amount INTEGER,
        percent REAL,
        PRIMARY KEY (enterprise_code, platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS productions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_type TEXT, owner_platform TEXT, owner_id TEXT,
        starter_platform TEXT, starter_id TEXT,
        starter_display TEXT,
        good_code TEXT,
        qty INTEGER,
        energy_bank TEXT,
        server_platform TEXT, server_id TEXT,
        notify_platform TEXT, notify_key TEXT,
        lang TEXT,
        finish_at INTEGER,
        created_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_productions_due ON productions (finish_at);

    CREATE TABLE IF NOT EXISTS server_production (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT, server_id TEXT,
        good_code TEXT, category TEXT,
        producer_type TEXT,
        producer_platform TEXT, producer_id TEXT,
        qty INTEGER, value INTEGER, quality_level INTEGER,
        created_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_server_production
        ON server_production (platform, server_id, created_at);

    -- One day's demand of one server in one base category, and what covered it.
    -- Written by the daily consumption tick: `need` is what the day's active
    -- population wanted, `consumed` the units the warehouses actually gave up,
    -- `satisfaction` the same units weighted by quality (the provision score is
    -- satisfaction/need). `base_only` marks a day fed by base goods alone,
    -- which caps that day's score at 1.0.
    CREATE TABLE IF NOT EXISTS server_consumption (
        platform TEXT, server_id TEXT,
        day TEXT,
        category TEXT,
        population INTEGER,
        need REAL,
        consumed INTEGER,
        satisfaction REAL,
        base_only INTEGER,
        created_at INTEGER,
        PRIMARY KEY (platform, server_id, day, category)
    );
    CREATE INDEX IF NOT EXISTS idx_server_consumption
        ON server_consumption (platform, server_id, created_at);

    CREATE TABLE IF NOT EXISTS chat_channels (
        platform TEXT, server_id TEXT, kind TEXT,
        channel_key TEXT,
        weekday INTEGER DEFAULT 0,
        hour INTEGER DEFAULT 12, minute INTEGER DEFAULT 0,
        tz_offset INTEGER DEFAULT 0,
        last_marker TEXT,
        created_at INTEGER,
        PRIMARY KEY (platform, server_id, kind)
    );

    CREATE TABLE IF NOT EXISTS server_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT, server_id TEXT,
        month TEXT,
        spec_json TEXT,
        completed INTEGER,
        created_at INTEGER,
        UNIQUE (platform, server_id, month)
    );

    CREATE TABLE IF NOT EXISTS auto_exports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_ent TEXT, to_ent TEXT,
        good_code TEXT,
        qty INTEGER,
        price INTEGER,
        bank_code TEXT,
        created_by TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS shipments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_ent TEXT, to_ent TEXT,
        good_code TEXT,
        qty INTEGER,
        escrow_amount INTEGER DEFAULT 0,
        bank_code TEXT,
        dispatch_at INTEGER,
        arrive_at INTEGER,
        notify_platform TEXT, notify_key TEXT,
        lang TEXT,
        created_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_shipments_due ON shipments (arrive_at);
    """)
    conn.commit()
    _migrate_economy()
    _seed_base_goods()

def _ensure_column(table, column, ddl):
    """Guarded ALTER TABLE ... ADD COLUMN for SQLite (no IF NOT EXISTS there)."""
    have = {r["name"] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in have:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

def _migrate_economy():
    """Schema upgrades for databases created before goods were owned by people
    and enterprises: goods gain an owner and a category, autocraft becomes the
    continuous 24/7 autoproduction (fractional units carry over between hourly
    runs; a worker may run it for an enterprise), banks remember the previous
    published value for the FX change log."""
    with _db_lock:
        _ensure_column("goods", "owner_type", "TEXT")
        _ensure_column("goods", "owner_platform", "TEXT")
        _ensure_column("goods", "owner_id", "TEXT")
        _ensure_column("goods", "category", "TEXT")
        _ensure_column("autocraft", "carry", "REAL DEFAULT 0")
        _ensure_column("autocraft", "starter_platform", "TEXT")
        _ensure_column("autocraft", "starter_id", "TEXT")
        _ensure_column("autocraft", "server_platform", "TEXT")
        _ensure_column("autocraft", "server_id", "TEXT")
        _ensure_column("banks", "prev_value", "REAL")
        orphans = cur.execute(
            "SELECT code, bank_code FROM goods WHERE owner_type IS NULL"
            " AND bank_code IS NOT NULL").fetchall()
        for g in orphans:
            leader = cur.execute(
                "SELECT * FROM bank_leaders WHERE bank_code=? ORDER BY rowid LIMIT 1",
                (g["bank_code"],)).fetchone()
            if leader:
                cur.execute(
                    "UPDATE goods SET owner_type='user', owner_platform=?, owner_id=?"
                    " WHERE code=?",
                    (leader["platform"], str(leader["user_id"]), g["code"]))
        conn.commit()

GOOD_CATEGORIES = ("food", "hygiene", "wardrobe", "pharmacy", "household")

BASE_GOODS = (
    ("FOOD", "food", "🥕", "Food"),
    ("HYGN", "hygiene", "🧼", "Hygiene products"),
    ("WARD", "wardrobe", "👘", "Wardrobe"),
    ("PHRM", "pharmacy", "💊", "Pharmacy goods"),
    ("HOUS", "household", "🧺", "Household goods"),
)

BASE_GOOD_CODES = {c for c, _cat, _e, _n in BASE_GOODS}

def is_base_good(code):
    return code in BASE_GOOD_CODES

def _seed_base_goods():
    from config import ECONOMY
    energy = int(ECONOMY.get("base_good_energy", 8))
    with _db_lock:
        for code, cat, emoji, name in BASE_GOODS:
            row = cur.execute("SELECT 1 FROM goods WHERE code=?", (code,)).fetchone()
            if row:
                cur.execute("UPDATE goods SET energy_cost=? WHERE code=?", (energy, code))
            else:
                cur.execute(
                    "INSERT INTO goods (code, bank_code, name, base_value, energy_cost,"
                    " emoji, category, created_at) VALUES (?,NULL,?,0,?,?,?,?)",
                    (code, name, energy, emoji, cat, _now()))
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

def set_chat_lang(chat_id, lang_code, is_dm=False):
    """Remember a chat's language. `is_dm` marks a private conversation with the
    bot; those rows are dropped again after a year (see cleanup_old_dm_langs),
    while a server's choice is kept for as long as the bot is in the server."""
    cur.execute(
        "INSERT INTO chat_settings (chat_id, lang, updated_at, is_dm) VALUES (?,?,?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET lang=excluded.lang,"
        " updated_at=excluded.updated_at, is_dm=MAX(chat_settings.is_dm, excluded.is_dm)",
        (chat_id, lang_code, int(time.time()), 1 if is_dm else 0)
    )
    conn.commit()

def cleanup_old_dm_langs(max_age_seconds=365 * 24 * 3600):
    """The language a person picked in their private chat with the bot is kept
    at most a year after they last set it."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute(
        "DELETE FROM chat_settings WHERE is_dm=1 AND updated_at IS NOT NULL"
        " AND updated_at < ?",
        (cutoff,)
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

def enterprise_code_taken(code):
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

BANK_EDITABLE_FIELDS = ("currency_name", "emoji")

_BANK_CODE_COLUMNS = (
    ("bank_leaders", "bank_code"),
    ("accounts", "bank_code"),
    ("transactions", "bank_code"),
    ("goods", "bank_code"),
    ("professions", "bank_code"),
    ("earn_channels", "bank_code"),
    ("wages", "bank_code"),
    ("party_dues", "bank_code"),
    ("enterprises", "salary_bank"),
    ("productions", "energy_bank"),
    ("auto_exports", "bank_code"),
    ("shipments", "bank_code"),
    ("treaties", "bank_a"),
    ("treaties", "bank_b"),
)

def update_bank_field(code, field, value):
    """Change one plain bank column. The whitelist keeps callers away from the
    columns the daily FX recompute owns — value, prev_value and the period
    sums — which must only ever move through economy.py's own formulas."""
    if field not in BANK_EDITABLE_FIELDS:
        raise ValueError(f"bank field not editable: {field}")
    with _db_lock:
        cur.execute(f"UPDATE banks SET {field}=? WHERE code=?", (value, code))
        conn.commit()

def set_bank_central_chat(code, chat_id, union_code):
    """Re-anchor a bank to another central chat. The union always follows the
    chat, so a bank cannot end up claiming a union its central server left."""
    with _db_lock:
        cur.execute("UPDATE banks SET central_chat=?, union_code=? WHERE code=?",
                    (str(chat_id), union_code, code))
        conn.commit()

def rename_bank_code(old_code, new_code):
    """Give a bank a new currency code, rewriting every reference in one
    transaction. The code is a bare TEXT key in a dozen tables rather than a
    real foreign key, so every column holding one is listed in
    _BANK_CODE_COLUMNS: miss one and its rows silently point at a bank that no
    longer exists. A new column holding a bank code belongs in that list."""
    with _db_lock:
        cur.execute("UPDATE banks SET code=? WHERE code=?", (new_code, old_code))
        for table, column in _BANK_CODE_COLUMNS:
            cur.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?",
                        (new_code, old_code))
        conn.commit()

def delete_bank(code):
    """Remove a bank and everything denominated in its currency: accounts and
    their journal, treaties, wages, party dues, earning channels, and the goods
    it prices together with the inventories, mastery and standing orders behind
    them. Cargo still in transit is handed back to its sender first, so nobody's
    warehouse loses goods to the deletion. Base goods are untouched: they carry
    no bank_code."""
    with _db_lock:
        for row in cur.execute("SELECT * FROM shipments WHERE bank_code=?",
                               (code,)).fetchall():
            _refund_shipment(row, row["from_ent"])
        for row in cur.execute("SELECT code FROM goods WHERE bank_code=?",
                               (code,)).fetchall():
            good = row["code"]
            for table in ("inventory", "mastery", "autocraft", "autosend",
                          "productions", "auto_exports", "shipments"):
                cur.execute(f"DELETE FROM {table} WHERE good_code=?", (good,))
        cur.execute("DELETE FROM goods WHERE bank_code=?", (code,))
        for table in ("bank_leaders", "accounts", "transactions", "professions",
                      "earn_channels", "wages", "party_dues", "auto_exports"):
            cur.execute(f"DELETE FROM {table} WHERE bank_code=?", (code,))
        cur.execute("DELETE FROM treaties WHERE bank_a=? OR bank_b=?", (code, code))
        cur.execute("UPDATE enterprises SET salary_bank=NULL WHERE salary_bank=?", (code,))
        cur.execute("UPDATE productions SET energy_bank=NULL WHERE energy_bank=?", (code,))
        cur.execute("DELETE FROM banks WHERE code=?", (code,))
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

def create_good(bank_code, name, base_value, energy_cost, emoji, owner=None, category=None):
    """Create a market good. It belongs to `owner` — a person or an enterprise —
    and is only *denominated* in the bank's currency (the bank buys it on /sell,
    it does not own it). `category` optionally ties it to a base category, so
    its quality feeds the owner server's provision."""
    code = generate_code()
    if code is None:
        return None
    ot, op, oi = owner if owner else (None, None, None)
    cur.execute(
        "INSERT INTO goods (code, bank_code, name, base_value, energy_cost, emoji,"
        " owner_type, owner_platform, owner_id, category, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (code, bank_code, name, int(base_value), int(energy_cost), emoji,
         ot, op, str(oi) if oi is not None else None, category, _now()))
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

def set_autocraft(owner, good_code, enabled, starter=None, server=None):
    """Toggle continuous autoproduction. `starter` is the worker whose mastery
    and energy drive it (needed when the owner is an enterprise); `server` is the
    server/group the output is attributed to in the provision statistics."""
    ot, op, oi = owner
    sp, si = starter if starter else (None, None)
    vp, vi = server if server else (None, None)
    cur.execute(
        "INSERT INTO autocraft (owner_type, owner_platform, owner_id, good_code, enabled,"
        " carry, starter_platform, starter_id, server_platform, server_id)"
        " VALUES (?,?,?,?,?,0,?,?,?,?)"
        " ON CONFLICT(owner_type, owner_platform, owner_id, good_code)"
        " DO UPDATE SET enabled=excluded.enabled,"
        " starter_platform=COALESCE(excluded.starter_platform, autocraft.starter_platform),"
        " starter_id=COALESCE(excluded.starter_id, autocraft.starter_id),"
        " server_platform=COALESCE(excluded.server_platform, autocraft.server_platform),"
        " server_id=COALESCE(excluded.server_id, autocraft.server_id)",
        (ot, op, str(oi), good_code, 1 if enabled else 0,
         sp, str(si) if si is not None else None,
         vp, str(vi) if vi is not None else None))
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

def enterprise_owner(code):
    return "enterprise", "", str(code)

def create_enterprise(code, name, platform, server_id, founder_platform, founder_id,
                      founder_name, description=None, logo=None, logo_mime=None):
    with _db_lock:
        cur.execute(
            "INSERT INTO enterprises (code, name, platform, server_id, founder_platform,"
            " founder_id, founder_name, description, logo, logo_mime, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (code, name, platform, str(server_id), founder_platform, str(founder_id),
             founder_name, description, logo, logo_mime, _now()))
        cur.execute(
            "INSERT OR REPLACE INTO enterprise_leaders"
            " (enterprise_code, platform, user_id, display_name) VALUES (?,?,?,?)",
            (code, founder_platform, str(founder_id), founder_name))
        conn.commit()

def get_enterprise(code):
    return cur.execute("SELECT * FROM enterprises WHERE code=?", (code,)).fetchone()

def find_enterprise(query):
    """An enterprise matched exactly (case-insensitively) by code or name."""
    q = (query or "").strip()
    if not q:
        return None
    return cur.execute(
        "SELECT * FROM enterprises WHERE LOWER(code)=LOWER(?) OR LOWER(name)=LOWER(?)"
        " ORDER BY CASE WHEN LOWER(code)=LOWER(?) THEN 0 ELSE 1 END LIMIT 1",
        (q, q, q)).fetchone()

def get_server_enterprises(platform, server_id):
    return cur.execute(
        "SELECT * FROM enterprises WHERE platform=? AND server_id=? ORDER BY name",
        (platform, str(server_id))).fetchall()

def update_enterprise_field(code, field, value):
    assert field in ("name", "description", "salary_period", "salary_bank")
    cur.execute(f"UPDATE enterprises SET {field}=? WHERE code=?", (value, code))
    conn.commit()

def update_enterprise_logo(code, logo, logo_mime):
    cur.execute("UPDATE enterprises SET logo=?, logo_mime=? WHERE code=?",
                (logo, logo_mime, code))
    conn.commit()

def rename_enterprise_code(old_code, new_code):
    with _db_lock:
        for table, col in (
            ("enterprises", "code"), ("enterprise_leaders", "enterprise_code"),
            ("enterprise_members", "enterprise_code"),
            ("enterprise_positions", "enterprise_code"),
            ("enterprise_salaries", "enterprise_code"),
        ):
            cur.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new_code, old_code))
        for col in ("owner_id",):
            cur.execute(
                f"UPDATE accounts SET {col}=? WHERE owner_type='enterprise' AND {col}=?",
                (new_code, old_code))
            cur.execute(
                f"UPDATE inventory SET {col}=? WHERE owner_type='enterprise' AND {col}=?",
                (new_code, old_code))
            cur.execute(
                f"UPDATE mastery SET {col}=? WHERE owner_type='enterprise' AND {col}=?",
                (new_code, old_code))
            cur.execute(
                f"UPDATE autocraft SET {col}=? WHERE owner_type='enterprise' AND {col}=?",
                (new_code, old_code))
        cur.execute(
            "UPDATE autosend SET target_id=? WHERE target_type='enterprise' AND target_id=?",
            (new_code, old_code))
        cur.execute(
            "UPDATE goods SET owner_id=? WHERE owner_type='enterprise' AND owner_id=?",
            (new_code, old_code))
        cur.execute("UPDATE auto_exports SET from_ent=? WHERE from_ent=?", (new_code, old_code))
        cur.execute("UPDATE auto_exports SET to_ent=? WHERE to_ent=?", (new_code, old_code))
        cur.execute("UPDATE shipments SET from_ent=? WHERE from_ent=?", (new_code, old_code))
        cur.execute("UPDATE shipments SET to_ent=? WHERE to_ent=?", (new_code, old_code))
        cur.execute(
            "UPDATE productions SET owner_id=? WHERE owner_type='enterprise' AND owner_id=?",
            (new_code, old_code))
        conn.commit()

def _refund_shipment(row, surviving_code):
    """A shipment cannot complete because one enterprise is being deleted: give
    the cargo and any escrowed payment to the surviving side."""
    if surviving_code and get_enterprise(surviving_code):
        owner = enterprise_owner(surviving_code)
        add_inventory(owner, row["good_code"], int(row["qty"]))
        escrow = int(row["escrow_amount"] or 0)
        if escrow > 0 and row["bank_code"] and get_bank(row["bank_code"]):
            mint(row["bank_code"], owner, escrow, "shipment_refund")
    cur.execute("DELETE FROM shipments WHERE id=?", (row["id"],))

def delete_enterprise(code):
    """Remove an enterprise and its whole economy footprint. Goods still in
    transit to or from it are refunded to the surviving counterpart first."""
    with _db_lock:
        for row in cur.execute(
                "SELECT * FROM shipments WHERE from_ent=? OR to_ent=?",
                (code, code)).fetchall():
            surviving = row["to_ent"] if row["from_ent"] == code else row["from_ent"]
            _refund_shipment(row, surviving)
        cur.execute("DELETE FROM enterprise_leaders WHERE enterprise_code=?", (code,))
        cur.execute("DELETE FROM enterprise_members WHERE enterprise_code=?", (code,))
        cur.execute("DELETE FROM enterprise_positions WHERE enterprise_code=?", (code,))
        cur.execute("DELETE FROM enterprise_salaries WHERE enterprise_code=?", (code,))
        cur.execute("DELETE FROM accounts WHERE owner_type='enterprise' AND owner_id=?", (code,))
        cur.execute("DELETE FROM inventory WHERE owner_type='enterprise' AND owner_id=?", (code,))
        cur.execute("DELETE FROM mastery WHERE owner_type='enterprise' AND owner_id=?", (code,))
        cur.execute("DELETE FROM autocraft WHERE owner_type='enterprise' AND owner_id=?", (code,))
        cur.execute("DELETE FROM autosend WHERE (owner_type='enterprise' AND owner_id=?)"
                    " OR (target_type='enterprise' AND target_id=?)", (code, code))
        cur.execute("DELETE FROM productions WHERE owner_type='enterprise' AND owner_id=?", (code,))
        cur.execute("DELETE FROM auto_exports WHERE from_ent=? OR to_ent=?", (code, code))
        cur.execute("UPDATE goods SET owner_type=NULL, owner_platform=NULL, owner_id=NULL"
                    " WHERE owner_type='enterprise' AND owner_id=?", (code,))
        cur.execute("DELETE FROM enterprises WHERE code=?", (code,))
        conn.commit()

def get_enterprise_leaders(code):
    return cur.execute(
        "SELECT * FROM enterprise_leaders WHERE enterprise_code=?", (code,)).fetchall()

def add_enterprise_leader(code, platform, user_id, display_name):
    cur.execute(
        "INSERT OR REPLACE INTO enterprise_leaders"
        " (enterprise_code, platform, user_id, display_name) VALUES (?,?,?,?)",
        (code, platform, str(user_id), display_name))
    conn.commit()

def set_enterprise_leader(code, platform, user_id, display_name):
    """Transfer: the new user becomes the only leader."""
    with _db_lock:
        cur.execute("DELETE FROM enterprise_leaders WHERE enterprise_code=?", (code,))
        cur.execute(
            "INSERT OR REPLACE INTO enterprise_leaders"
            " (enterprise_code, platform, user_id, display_name) VALUES (?,?,?,?)",
            (code, platform, str(user_id), display_name))
        conn.commit()

def remove_enterprise_leader(code, platform, user_id):
    cur.execute(
        "DELETE FROM enterprise_leaders WHERE enterprise_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id)))
    conn.commit()

def is_enterprise_leader(code, platform, user_id):
    row = get_enterprise(code)
    if row and row["founder_platform"] == platform and row["founder_id"] == str(user_id):
        return True
    return cur.execute(
        "SELECT 1 FROM enterprise_leaders WHERE enterprise_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))).fetchone() is not None

def get_enterprise_members(code):
    return cur.execute(
        "SELECT * FROM enterprise_members WHERE enterprise_code=? ORDER BY display_name",
        (code,)).fetchall()

def get_enterprise_member(code, platform, user_id):
    return cur.execute(
        "SELECT * FROM enterprise_members WHERE enterprise_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))).fetchone()

def add_enterprise_member(code, platform, user_id, display_name):
    cur.execute(
        "INSERT OR REPLACE INTO enterprise_members"
        " (enterprise_code, platform, user_id, display_name) VALUES (?,?,?,?)",
        (code, platform, str(user_id), display_name))
    conn.commit()

def remove_enterprise_member(code, platform, user_id):
    with _db_lock:
        c = cur.execute(
            "DELETE FROM enterprise_members WHERE enterprise_code=? AND platform=? AND user_id=?",
            (code, platform, str(user_id)))
        cur.execute(
            "DELETE FROM enterprise_salaries WHERE enterprise_code=? AND platform=? AND user_id=?",
            (code, platform, str(user_id)))
        conn.commit()
        return c.rowcount > 0

def set_member_position(code, platform, user_id, position):
    cur.execute(
        "UPDATE enterprise_members SET position=? WHERE enterprise_code=? AND platform=?"
        " AND user_id=?", (position, code, platform, str(user_id)))
    conn.commit()

def is_enterprise_worker(code, platform, user_id):
    """Whether the user works at the enterprise (leaders and the founder count)."""
    if is_enterprise_leader(code, platform, user_id):
        return True
    return get_enterprise_member(code, platform, user_id) is not None

def get_enterprise_user_set(code):
    """Every (platform, user_id) at the enterprise: founder + leaders + members."""
    users = set()
    row = get_enterprise(code)
    if row:
        users.add((row["founder_platform"], row["founder_id"]))
    for r in get_enterprise_leaders(code):
        users.add((r["platform"], r["user_id"]))
    for r in get_enterprise_members(code):
        users.add((r["platform"], r["user_id"]))
    return users

def user_led_enterprises(platform, user_id):
    """Enterprises where the user is the founder or a leader."""
    return cur.execute(
        """
        SELECT DISTINCT e.* FROM enterprises e
        LEFT JOIN enterprise_leaders l ON l.enterprise_code = e.code
        WHERE (e.founder_platform=? AND e.founder_id=?)
           OR (l.platform=? AND l.user_id=?)
        ORDER BY e.name
        """,
        (platform, str(user_id), platform, str(user_id))).fetchall()

def user_member_enterprises(platform, user_id):
    """Enterprises the user works at (as a plain member)."""
    return cur.execute(
        "SELECT e.* FROM enterprises e JOIN enterprise_members m"
        " ON m.enterprise_code=e.code WHERE m.platform=? AND m.user_id=? ORDER BY e.name",
        (platform, str(user_id))).fetchall()

def set_position_salary(code, position, amount, percent):
    """Salary attached to a position: a fixed `amount` (minor units) OR a
    `percent` of the enterprise's period sales. Both None clears the position."""
    if amount is None and percent is None:
        cur.execute("DELETE FROM enterprise_positions WHERE enterprise_code=? AND position=?",
                    (code, position))
    else:
        cur.execute(
            "INSERT INTO enterprise_positions (enterprise_code, position, amount, percent)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(enterprise_code, position) DO UPDATE SET"
            " amount=excluded.amount, percent=excluded.percent",
            (code, position, amount, percent))
    conn.commit()

def get_position_salary(code, position):
    return cur.execute(
        "SELECT * FROM enterprise_positions WHERE enterprise_code=? AND position=?",
        (code, position)).fetchone()

def get_enterprise_positions(code):
    return cur.execute(
        "SELECT * FROM enterprise_positions WHERE enterprise_code=? ORDER BY position",
        (code,)).fetchall()

def set_personal_salary(code, platform, user_id, amount, percent):
    """Per-worker salary override; both None clears it."""
    if amount is None and percent is None:
        cur.execute(
            "DELETE FROM enterprise_salaries WHERE enterprise_code=? AND platform=? AND user_id=?",
            (code, platform, str(user_id)))
    else:
        cur.execute(
            "INSERT INTO enterprise_salaries (enterprise_code, platform, user_id, amount, percent)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(enterprise_code, platform, user_id) DO UPDATE SET"
            " amount=excluded.amount, percent=excluded.percent",
            (code, platform, str(user_id), amount, percent))
    conn.commit()

def get_personal_salary(code, platform, user_id):
    return cur.execute(
        "SELECT * FROM enterprise_salaries WHERE enterprise_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))).fetchone()

def get_all_enterprises():
    return cur.execute("SELECT * FROM enterprises ORDER BY code").fetchall()

def add_enterprise_sales(code, delta):
    """Track income in the salary currency since the last payout (the base for
    percent-of-sales salaries)."""
    cur.execute("UPDATE enterprises SET period_sales=period_sales+? WHERE code=?",
                (int(delta), code))
    conn.commit()

def reset_enterprise_sales(code):
    cur.execute("UPDATE enterprises SET period_sales=0 WHERE code=?", (code,))
    conn.commit()

def create_production(owner, starter, good_code, qty, energy_bank, server, notify, lang,
                      finish_at, starter_display=None):
    ot, op, oi = owner
    sp, si = starter
    srv_p, srv_i = server
    np_, nk = notify
    c = cur.execute(
        "INSERT INTO productions (owner_type, owner_platform, owner_id, starter_platform,"
        " starter_id, starter_display, good_code, qty, energy_bank, server_platform,"
        " server_id, notify_platform, notify_key, lang, finish_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ot, op, str(oi), sp, str(si), starter_display, good_code, int(qty), energy_bank,
         srv_p, str(srv_i) if srv_i is not None else None, np_, str(nk) if nk else None,
         lang, int(finish_at), _now()))
    conn.commit()
    return c.lastrowid

def get_due_productions(now_ts):
    return cur.execute(
        "SELECT * FROM productions WHERE finish_at<=? ORDER BY finish_at", (int(now_ts),)
    ).fetchall()

def get_active_production(starter_platform, starter_id):
    """The unfinished run a worker is busy with, if any (one job at a time)."""
    return cur.execute(
        "SELECT * FROM productions WHERE starter_platform=? AND starter_id=?"
        " ORDER BY finish_at LIMIT 1", (starter_platform, str(starter_id))).fetchone()

def delete_production(prod_id):
    cur.execute("DELETE FROM productions WHERE id=?", (prod_id,))
    conn.commit()

def record_server_production(platform, server_id, good_code, category, producer_type,
                             producer, qty, value, quality_level):
    """One production event attributed to a server. `producer` is the worker's
    canonical (platform, user_id) — the basis for the active-population count in
    the provision formula."""
    if platform is None or server_id is None:
        return
    pp, pi = producer if producer else (None, None)
    cur.execute(
        "INSERT INTO server_production (platform, server_id, good_code, category,"
        " producer_type, producer_platform, producer_id, qty, value, quality_level, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (platform, str(server_id), good_code, category, producer_type,
         pp, str(pi) if pi is not None else None,
         int(qty), int(value), int(quality_level), _now()))
    conn.commit()

def server_category_production(platform, server_id, since_ts, until_ts=None):
    """{category: [(good_code, qty, quality_level, is_base_qty_weighted rows)]} —
    raw rows of the window, grouped by category in Python by the caller."""
    until_ts = until_ts or _now()
    return cur.execute(
        "SELECT good_code, category, qty, value, quality_level FROM server_production"
        " WHERE platform=? AND server_id=? AND created_at>=? AND created_at<=?",
        (platform, str(server_id), int(since_ts), int(until_ts))).fetchall()

def server_top_goods(platform, server_id, since_ts, until_ts=None, limit=5):
    until_ts = until_ts or _now()
    return cur.execute(
        "SELECT good_code, SUM(qty) AS qty, SUM(value) AS value FROM server_production"
        " WHERE platform=? AND server_id=? AND created_at>=? AND created_at<=?"
        " GROUP BY good_code ORDER BY value DESC, qty DESC LIMIT ?",
        (platform, str(server_id), int(since_ts), int(until_ts), limit)).fetchall()

def server_active_producers(platform, server_id, since_ts, until_ts=None):
    """How many distinct people produced anything on the server in the window —
    the provision formula's population."""
    until_ts = until_ts or _now()
    row = cur.execute(
        "SELECT COUNT(DISTINCT producer_platform || '|' || producer_id) AS c"
        " FROM server_production WHERE platform=? AND server_id=?"
        " AND producer_id IS NOT NULL AND created_at>=? AND created_at<=?",
        (platform, str(server_id), int(since_ts), int(until_ts))).fetchone()
    return row["c"] or 0

def server_good_qty(platform, server_id, good_code, since_ts, until_ts=None):
    until_ts = until_ts or _now()
    row = cur.execute(
        "SELECT COALESCE(SUM(qty),0) AS q FROM server_production"
        " WHERE platform=? AND server_id=? AND good_code=? AND created_at>=? AND created_at<=?",
        (platform, str(server_id), good_code, int(since_ts), int(until_ts))).fetchone()
    return row["q"] or 0

def record_server_consumption(platform, server_id, day, category, population,
                              need, consumed, satisfaction, base_only):
    """One day's demand and what covered it. Keyed by the day, so a tick that
    runs twice for the same date overwrites its row instead of counting the
    meal twice into the week."""
    cur.execute(
        "INSERT INTO server_consumption (platform, server_id, day, category,"
        " population, need, consumed, satisfaction, base_only, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(platform, server_id, day, category) DO UPDATE SET"
        " population=excluded.population, need=excluded.need,"
        " consumed=excluded.consumed, satisfaction=excluded.satisfaction,"
        " base_only=excluded.base_only, created_at=excluded.created_at",
        (platform, str(server_id), day, category, int(population), float(need),
         int(consumed), float(satisfaction), 1 if base_only else 0, _now()))
    conn.commit()

def server_consumption_window(platform, server_id, since_ts, until_ts=None):
    """The consumption rows of a time window — what the provision score reads."""
    until_ts = until_ts or _now()
    return cur.execute(
        "SELECT category, population, need, consumed, satisfaction, base_only"
        " FROM server_consumption WHERE platform=? AND server_id=?"
        " AND created_at>=? AND created_at<=?",
        (platform, str(server_id), int(since_ts), int(until_ts))).fetchall()

def enterprise_category_stock(code, category):
    """An enterprise's stock in one base category, best goods first: market
    goods (ordered by base value) ahead of the base goods behind them, so the
    population eats the good stuff first and base production is the filler.
    Base goods are the rows with no bank_code."""
    return cur.execute(
        "SELECT i.good_code, i.qty FROM inventory i JOIN goods g ON g.code=i.good_code"
        " WHERE i.owner_type='enterprise' AND i.owner_id=? AND i.qty>0"
        " AND g.category=?"
        " ORDER BY (g.bank_code IS NULL) ASC, g.base_value DESC, i.good_code",
        (str(code), category)).fetchall()

def cleanup_old_server_consumption(max_age_seconds=70 * 86400):
    """Kept as long as the production statistics, for the same reason: the
    weekly report never looks further back than that."""
    cur.execute("DELETE FROM server_consumption WHERE created_at < ?",
                (_now() - int(max_age_seconds),))
    conn.commit()

def all_chats():
    """Every set-up server and group — for the loops that must walk them all."""
    return cur.execute("SELECT * FROM chats ORDER BY chat_id").fetchall()

def cleanup_old_server_production(max_age_seconds=70 * 86400):
    """Production statistics are kept ~10 weeks: enough for the weekly stats,
    the monthly task evaluation and next month's task generation."""
    cutoff = _now() - max_age_seconds
    cur.execute("DELETE FROM server_production WHERE created_at<?", (cutoff,))
    conn.commit()

def set_chat_channel(platform, server_id, kind, channel_key, weekday=None, hour=None,
                     minute=None, tz_offset=None):
    """Bind the server's 'logs' or 'tasks' channel; schedule fields keep their
    previous (or default) values when passed as None."""
    cur.execute(
        "INSERT INTO chat_channels (platform, server_id, kind, channel_key, weekday,"
        " hour, minute, tz_offset, last_marker, created_at)"
        " VALUES (?,?,?,?,COALESCE(?,0),COALESCE(?,12),COALESCE(?,0),COALESCE(?,0),NULL,?)"
        " ON CONFLICT(platform, server_id, kind) DO UPDATE SET"
        " channel_key=excluded.channel_key,"
        " weekday=COALESCE(?, chat_channels.weekday),"
        " hour=COALESCE(?, chat_channels.hour),"
        " minute=COALESCE(?, chat_channels.minute),"
        " tz_offset=COALESCE(?, chat_channels.tz_offset)",
        (platform, str(server_id), kind, str(channel_key), weekday, hour, minute, tz_offset,
         _now(), weekday, hour, minute, tz_offset))
    conn.commit()

def get_chat_channel(platform, server_id, kind):
    return cur.execute(
        "SELECT * FROM chat_channels WHERE platform=? AND server_id=? AND kind=?",
        (platform, str(server_id), kind)).fetchone()

def remove_chat_channel(platform, server_id, kind):
    c = cur.execute(
        "DELETE FROM chat_channels WHERE platform=? AND server_id=? AND kind=?",
        (platform, str(server_id), kind))
    conn.commit()
    return c.rowcount > 0

def all_chat_channels(kind=None):
    if kind is None:
        return cur.execute("SELECT * FROM chat_channels").fetchall()
    return cur.execute("SELECT * FROM chat_channels WHERE kind=?", (kind,)).fetchall()

def set_chat_channel_marker(platform, server_id, kind, marker):
    cur.execute(
        "UPDATE chat_channels SET last_marker=? WHERE platform=? AND server_id=? AND kind=?",
        (marker, platform, str(server_id), kind))
    conn.commit()

def save_server_task(platform, server_id, month, spec_json):
    cur.execute(
        "INSERT INTO server_tasks (platform, server_id, month, spec_json, completed, created_at)"
        " VALUES (?,?,?,?,NULL,?)"
        " ON CONFLICT(platform, server_id, month) DO UPDATE SET spec_json=excluded.spec_json",
        (platform, str(server_id), month, spec_json, _now()))
    conn.commit()

def get_server_task(platform, server_id, month):
    return cur.execute(
        "SELECT * FROM server_tasks WHERE platform=? AND server_id=? AND month=?",
        (platform, str(server_id), month)).fetchone()

def get_unevaluated_tasks(before_month):
    """Tasks of past months whose completion has not been judged yet."""
    return cur.execute(
        "SELECT * FROM server_tasks WHERE completed IS NULL AND month<?",
        (before_month,)).fetchall()

def set_server_task_completed(task_id, completed):
    cur.execute("UPDATE server_tasks SET completed=? WHERE id=?",
                (1 if completed else 0, task_id))
    conn.commit()

def cleanup_old_server_tasks(max_age_seconds=400 * 86400):
    cutoff = _now() - max_age_seconds
    cur.execute("DELETE FROM server_tasks WHERE created_at<?", (cutoff,))
    conn.commit()

def add_auto_export(from_ent, to_ent, good_code, qty, price, bank_code, created_by):
    c = cur.execute(
        "INSERT INTO auto_exports (from_ent, to_ent, good_code, qty, price, bank_code,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (from_ent, to_ent, good_code, int(qty), int(price), bank_code,
         str(created_by), _now()))
    conn.commit()
    return c.lastrowid

def get_auto_exports(from_ent=None):
    if from_ent is None:
        return cur.execute("SELECT * FROM auto_exports ORDER BY id").fetchall()
    return cur.execute("SELECT * FROM auto_exports WHERE from_ent=? ORDER BY id",
                       (from_ent,)).fetchall()

def remove_auto_export(from_ent, to_ent, good_code):
    c = cur.execute(
        "DELETE FROM auto_exports WHERE from_ent=? AND to_ent=? AND good_code=?",
        (from_ent, to_ent, good_code))
    conn.commit()
    return c.rowcount > 0

def create_shipment(from_ent, to_ent, good_code, qty, escrow_amount, bank_code,
                    dispatch_at, arrive_at, notify, lang):
    np_, nk = notify if notify else (None, None)
    c = cur.execute(
        "INSERT INTO shipments (from_ent, to_ent, good_code, qty, escrow_amount,"
        " bank_code, dispatch_at, arrive_at, notify_platform, notify_key, lang, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (from_ent, to_ent, good_code, int(qty), int(escrow_amount or 0), bank_code,
         int(dispatch_at), int(arrive_at), np_, str(nk) if nk else None, lang, _now()))
    conn.commit()
    return c.lastrowid

def get_due_shipments(now_ts):
    return cur.execute(
        "SELECT * FROM shipments WHERE arrive_at<=? ORDER BY arrive_at", (int(now_ts),)
    ).fetchall()

def get_enterprise_shipments(ent_code):
    """Every shipment still in transit that this enterprise sends or receives."""
    return cur.execute(
        "SELECT * FROM shipments WHERE from_ent=? OR to_ent=? ORDER BY arrive_at",
        (ent_code, ent_code)).fetchall()

def delete_shipment(shipment_id):
    cur.execute("DELETE FROM shipments WHERE id=?", (shipment_id,))
    conn.commit()

def cleanup_old_shipments(max_age_seconds=7 * 86400):
    """Safety net: a shipment is normally removed the moment it arrives, but drop
    anything left far past its arrival (e.g. both enterprises long gone)."""
    cutoff = _now() - max_age_seconds
    cur.execute("DELETE FROM shipments WHERE arrive_at<?", (cutoff,))
    conn.commit()

def set_autocraft_carry(owner, good_code, carry):
    ot, op, oi = owner
    cur.execute(
        "UPDATE autocraft SET carry=? WHERE owner_type=? AND owner_platform=?"
        " AND owner_id=? AND good_code=?",
        (float(carry), ot, op, str(oi), good_code))
    conn.commit()

def set_bank_prev_value(code, value):
    cur.execute("UPDATE banks SET prev_value=? WHERE code=?",
                (float(value) if value is not None else None, code))
    conn.commit()

# ── Olympiad ────────────────────────────────────────────────────────────────
# The wiki Olympiad is a bounded event: /setolympiad opens it, contests and
# their candidate wikis are set up by Bot Admins, people vote from their private
# chat with the bot, and every trace of the contests and the votes is deleted
# the moment the voting period is over. Only two things outlive it — the
# language someone picked in their DM and the wiki account a reviewer chose to
# remember — and both expire after a year of their own.

def init_olympiad():
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS olympiad_contests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lang TEXT,
        review_chat TEXT,
        approved_chat TEXT,
        created_by TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS olympiad_contest_names (
        contest_id INTEGER NOT NULL,
        lang TEXT NOT NULL,
        name TEXT,
        PRIMARY KEY (contest_id, lang)
    );

    CREATE TABLE IF NOT EXISTS olympiad_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contest_id INTEGER NOT NULL,
        name TEXT,
        url TEXT,
        lang TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS olympiad_votes (
        key TEXT PRIMARY KEY,
        contest_id INTEGER NOT NULL,
        platform TEXT,
        user_id TEXT,
        username TEXT,
        lang TEXT,
        candidates TEXT,
        account_text TEXT,
        account_known INTEGER DEFAULT 0,
        activity_text TEXT,
        body TEXT,
        is_update INTEGER DEFAULT 0,
        created_at INTEGER
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_olympiad_votes_one
        ON olympiad_votes (contest_id, platform, user_id);

    CREATE TABLE IF NOT EXISTS wiki_accounts (
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        fandom_name TEXT,
        linked_by TEXT,
        linked_at INTEGER,
        PRIMARY KEY (platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS olympiad_contest_approved_langs (
        contest_id INTEGER NOT NULL,
        lang TEXT NOT NULL,
        chat TEXT,
        PRIMARY KEY (contest_id, lang)
    );
    """)
    conn.commit()

def get_olympiad():
    """(start_ts, end_ts) of the open Olympiad, or None when none is set."""
    start, end = get_setting("olympiad_start"), get_setting("olympiad_end")
    if start is None or end is None:
        return None
    try:
        return int(start), int(end)
    except (TypeError, ValueError):
        return None

def set_olympiad(start_ts, end_ts):
    set_setting("olympiad_start", int(start_ts))
    set_setting("olympiad_end", int(end_ts))

def clear_olympiad():
    cur.execute("DELETE FROM bot_settings WHERE key IN ('olympiad_start','olympiad_end')")
    conn.commit()

def clear_olympiad_data():
    """Wipe the contests, their candidates and every vote — what happens when
    the voting period ends and when an Olympiad is cancelled."""
    cur.executescript(
        "DELETE FROM olympiad_votes;"
        "DELETE FROM olympiad_candidates;"
        "DELETE FROM olympiad_contest_names;"
        "DELETE FROM olympiad_contests;"
    )
    conn.commit()

def add_contest(lang, review_chat, approved_chat, created_by=None):
    cur.execute(
        "INSERT INTO olympiad_contests (lang, review_chat, approved_chat, created_by,"
        " created_at) VALUES (?,?,?,?,?)",
        (lang, str(review_chat), str(approved_chat), created_by, int(time.time()))
    )
    conn.commit()
    return cur.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def set_contest_name(contest_id, lang, name):
    cur.execute(
        "INSERT INTO olympiad_contest_names (contest_id, lang, name) VALUES (?,?,?)"
        " ON CONFLICT(contest_id, lang) DO UPDATE SET name=excluded.name",
        (int(contest_id), lang, name)
    )
    conn.commit()

def set_approved_chat_for_lang(contest_id, lang, chat):
    """Route a contest's *approved* votes cast in `lang` to a specific chat,
    overriding the contest's single approved_chat. Pass chat=None to clear the
    override so that language falls back to approved_chat again."""
    if chat is None:
        cur.execute(
            "DELETE FROM olympiad_contest_approved_langs WHERE contest_id=? AND lang=?",
            (int(contest_id), lang))
    else:
        cur.execute(
            "INSERT INTO olympiad_contest_approved_langs (contest_id, lang, chat)"
            " VALUES (?,?,?)"
            " ON CONFLICT(contest_id, lang) DO UPDATE SET chat=excluded.chat",
            (int(contest_id), lang, str(chat)))
    conn.commit()

def approved_chat_for(contest, lang):
    """The chat an approved vote should be posted to: a per-language override
    when one is set for this contest, otherwise the contest's approved_chat."""
    if lang:
        row = cur.execute(
            "SELECT chat FROM olympiad_contest_approved_langs"
            " WHERE contest_id=? AND lang=?",
            (int(contest["id"]), lang)).fetchone()
        if row and row["chat"]:
            return row["chat"]
    return contest["approved_chat"]

def get_contests():
    return cur.execute("SELECT * FROM olympiad_contests ORDER BY id").fetchall()

def get_contest(contest_id):
    return cur.execute("SELECT * FROM olympiad_contests WHERE id=?",
                       (int(contest_id),)).fetchone()

def get_contest_names(contest_id):
    rows = cur.execute(
        "SELECT lang, name FROM olympiad_contest_names WHERE contest_id=?",
        (int(contest_id),)).fetchall()
    return {r["lang"]: r["name"] for r in rows}

def add_candidate(contest_id, name, url, lang=None):
    cur.execute(
        "INSERT INTO olympiad_candidates (contest_id, name, url, lang, created_at)"
        " VALUES (?,?,?,?,?)",
        (int(contest_id), name, url, lang, int(time.time()))
    )
    conn.commit()
    return cur.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def get_candidates(contest_id):
    return cur.execute(
        "SELECT * FROM olympiad_candidates WHERE contest_id=? ORDER BY id",
        (int(contest_id),)).fetchall()

def get_candidate(candidate_id):
    return cur.execute("SELECT * FROM olympiad_candidates WHERE id=?",
                       (int(candidate_id),)).fetchone()

def update_candidate(candidate_id, name, url, lang=None):
    cur.execute(
        "UPDATE olympiad_candidates SET name=?, url=?, lang=? WHERE id=?",
        (name, url, lang, int(candidate_id)))
    conn.commit()

def delete_candidate(candidate_id):
    cur.execute("DELETE FROM olympiad_candidates WHERE id=?", (int(candidate_id),))
    conn.commit()

def add_vote(key, contest_id, platform, user_id, username, lang, candidates_json,
             account_text, account_known, activity_text, body, is_update):
    """Store a vote awaiting review. A person has one vote per contest: casting
    again replaces the earlier one (which is then marked as an update)."""
    cur.execute(
        "DELETE FROM olympiad_votes WHERE contest_id=? AND platform=? AND user_id=?",
        (int(contest_id), platform, str(user_id)))
    cur.execute(
        "INSERT INTO olympiad_votes (key, contest_id, platform, user_id, username,"
        " lang, candidates, account_text, account_known, activity_text, body,"
        " is_update, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, int(contest_id), platform, str(user_id), username, lang,
         candidates_json, account_text, 1 if account_known else 0, activity_text,
         body, 1 if is_update else 0, int(time.time()))
    )
    conn.commit()

def get_vote(key):
    return cur.execute("SELECT * FROM olympiad_votes WHERE key=?", (key,)).fetchone()

def get_user_vote(contest_id, platform, user_id):
    return cur.execute(
        "SELECT * FROM olympiad_votes WHERE contest_id=? AND platform=? AND user_id=?",
        (int(contest_id), platform, str(user_id))).fetchone()

def delete_vote(key):
    cur.execute("DELETE FROM olympiad_votes WHERE key=?", (key,))
    conn.commit()

def set_wiki_account(platform, user_id, fandom_name, linked_by=None):
    cur.execute(
        "INSERT INTO wiki_accounts (platform, user_id, fandom_name, linked_by, linked_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(platform, user_id) DO UPDATE SET"
        " fandom_name=excluded.fandom_name, linked_by=excluded.linked_by,"
        " linked_at=excluded.linked_at",
        (platform, str(user_id), fandom_name, linked_by, int(time.time()))
    )
    conn.commit()

def get_wiki_account(platform, user_id):
    return cur.execute(
        "SELECT * FROM wiki_accounts WHERE platform=? AND user_id=?",
        (platform, str(user_id))).fetchone()

def cleanup_old_wiki_accounts(max_age_seconds=365 * 24 * 3600):
    """A reviewer-remembered wiki account is kept at most a year."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute(
        "DELETE FROM wiki_accounts WHERE linked_at IS NOT NULL AND linked_at < ?",
        (cutoff,))
    conn.commit()

def known_fandom_name(platform, user_id):
    """The Fandom username the bot already knows for someone: from /verify (their
    own Discord account, or the Discord account their Telegram is linked to),
    otherwise from a reviewer's /accepted … remember. None when unknown."""
    uid = str(user_id)
    discord_id = uid if platform == "discord" else None
    if platform == "telegram":
        link = get_link_by_telegram(uid)
        if link and link["discord_id"]:
            discord_id = str(link["discord_id"])
    if discord_id:
        row = get_fandom_verification(discord_id)
        if row and row["fandom_name"]:
            return row["fandom_name"]
    row = get_wiki_account(platform, uid)
    return row["fandom_name"] if row and row["fandom_name"] else None
