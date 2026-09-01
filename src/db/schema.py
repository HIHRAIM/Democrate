"""Every CREATE TABLE the bot needs outside the economy, plus the column
migrations that keep an old database usable.

`init()` is the one entry point: main.py calls it and nothing else, and it
brings the whole schema up to date by running the core DDL below, then
`_migrate_core()`, then the economy's own `init_economy()` (db/schema_economy.py)
and the Olympiad's `init_olympiad()`. That chain is load-bearing — a caller
that runs only part of it leaves half a database behind.

Migrations here are strictly additive, and there is exactly one way to add a
column: `_ensure_column`, which reads PRAGMA table_info first. No DROP, no
destructive ALTER, no rebuilding a table — src/dem.db is a live production
file that nobody re-creates.

Not this module's zone: the economy's tables (db/schema_economy.py) and every
accessor built on any of these tables (the domain modules beside this one).
"""
from db import conn, cur, _db_lock

def init():
    """Bring the whole schema up to date and seed what has to exist.

    The one entry point: main.py calls this and nothing else. Runs the core
    DDL, then `_migrate_core()`, then the economy's `init_economy()` and the
    Olympiad's `init_olympiad()` — that chain must stay, a caller that runs
    only part of it leaves half a database behind. `init_economy` is imported
    at the call site because db/schema_economy.py imports `_ensure_column`
    back from here; that is the project's usual way of breaking a cycle."""
    from db.schema_economy import init_economy

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

    -- The seven-day setup deadline (setup_deadline.py). One row per server or
    -- group the bot was added to AFTER the rule came into force — everything
    -- it was already sitting in has no row and is never examined. joined_at
    -- is what the deadline counts from; on Discord it is only a record, since
    -- Guild.me.joined_at is authoritative there and survives a restart, while
    -- Telegram offers nothing of the kind and the my_chat_member update that
    -- adds the bot is the only moment the time can be learnt. settled_at is
    -- set the first time the chat is found bound to a union with /setup, and
    -- is what makes the check one-shot: a chat set up once is never left.
    CREATE TABLE IF NOT EXISTS setup_deadlines (
        platform TEXT NOT NULL,
        server_id TEXT NOT NULL,
        joined_at INTEGER,
        settled_at INTEGER,
        PRIMARY KEY (platform, server_id)
    );
    """)
    conn.commit()
    _migrate_core()
    init_economy()
    init_olympiad()

def _migrate_core():
    """Schema upgrades for the party/quiz core. `chat_settings` learns when a
    language was chosen and whether the chat is a private conversation with the
    bot, so the one-year retention of DM languages can find its rows without
    guessing at id shapes. `chats` learns the community's own name, which is
    what lets a bank card say where its central server is instead of printing
    the bare id at somebody."""
    with _db_lock:
        _ensure_column("chat_settings", "updated_at", "INTEGER")
        _ensure_column("chat_settings", "is_dm", "INTEGER DEFAULT 0")
        _ensure_column("chats", "title", "TEXT")

def _ensure_column(table, column, ddl):
    """Guarded ALTER TABLE ... ADD COLUMN for SQLite (no IF NOT EXISTS there)."""
    have = {r["name"] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in have:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

def init_olympiad():
    """Create the Olympiad's tables.

    Called from `init()` on every start, even though an Olympiad exists only
    now and then: the tables are cheap and their absence would turn the first
    `/setolympiad` of a deployment into an error. Almost everything they hold
    is deleted the moment a voting period ends (db/olympiad.py:
    clear_olympiad_data) — only the remembered wiki accounts outlive it, and
    those expire after a year of their own."""
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
