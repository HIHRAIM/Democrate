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

    -- Retired: the per-server anniversaries of the former /wiki-founday,
    -- replaced by the union wikis below. Nothing writes it any more; it stays
    -- because migrations here are additive, and db/unions.py: remove_chat
    -- still clears a departing server's old rows.
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

    -- A wiki the bot knows: one row per wiki, however many unions it is
    -- attached to (union_wikis). base_url is the canonical script root
    -- ("https://confederation.fandom.com/ru") that every typed link is
    -- resolved to, api_url is where the wiki is read, main_url is the link a
    -- greeting carries. founded_at is detected once, on attachment, the way
    -- Wiki-Bot's /overview does it; founded_manual is a Bot Admin's
    -- correction (/wiki founded) and wins whenever it is set. city_id is the
    -- Fandom wiki id and is NULL off the Fandom farm. Read by db/wikis.py and
    -- by the wiki refresh in main.py.
    CREATE TABLE IF NOT EXISTS wikis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        base_url TEXT NOT NULL UNIQUE,
        api_url TEXT NOT NULL,
        main_url TEXT,
        name TEXT,
        lang TEXT,
        city_id INTEGER,
        is_fandom INTEGER NOT NULL DEFAULT 0,
        founded_at INTEGER,
        founded_manual INTEGER,
        refreshed_at INTEGER,
        refresh_error TEXT,
        created_at INTEGER
    );

    -- Which union a wiki belongs to. Written by /wiki add, removed by
    -- /wiki remove; a wiki left in no union is deleted together with its
    -- administration cache.
    CREATE TABLE IF NOT EXISTS union_wikis (
        union_code TEXT NOT NULL,
        wiki_id INTEGER NOT NULL,
        added_by TEXT,
        added_at INTEGER,
        PRIMARY KEY (union_code, wiki_id)
    );

    -- The administration of a wiki as last read from its public API:
    -- bureaucrats, administrators and content moderators, without bots and
    -- system accounts. account_key is 'fandom:<id>' on the Fandom farm, where
    -- user ids are global, and '<wiki id>:<id>' elsewhere; the union profile
    -- counts it distinct. since is when the current uninterrupted status began
    -- by the rights log (the wiki's founding when the log is silent), and the
    -- representative role waits thirty days from it. discord_handle is what
    -- the person's Fandom profile publishes. Rebuilt by main.py:
    -- wiki_refresh_loop; read by the role pass and the union profile.
    CREATE TABLE IF NOT EXISTS wiki_staff (
        wiki_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        account_key TEXT NOT NULL,
        name TEXT,
        groups TEXT,
        since INTEGER,
        discord_handle TEXT,
        seen_at INTEGER,
        PRIMARY KEY (wiki_id, user_id)
    );

    -- One row per community for the celebrations: the single UTC offset the
    -- greetings and the birthday role are timed by (every command that takes
    -- one overwrites it), the wiki birthday channel (Discord), the personal
    -- birthday channel (a Discord channel id or a Telegram 'chat:topic'), the
    -- birthday and wiki representative roles (Discord role ids), and the local
    -- date each greeting last went out on, which is what makes it fire once a
    -- day. Owned by db/community.py.
    CREATE TABLE IF NOT EXISTS community_settings (
        platform TEXT NOT NULL,
        server_id TEXT NOT NULL,
        tz_offset INTEGER NOT NULL DEFAULT 0,
        wiki_channel TEXT,
        birthday_channel TEXT,
        birthday_role TEXT,
        rep_role TEXT,
        wiki_marker TEXT,
        birthday_marker TEXT,
        PRIMARY KEY (platform, server_id)
    );

    -- Wikis one community chose not to greet (/wiki greeting).
    CREATE TABLE IF NOT EXISTS wiki_greeting_off (
        platform TEXT NOT NULL,
        server_id TEXT NOT NULL,
        wiki_id INTEGER NOT NULL,
        PRIMARY KEY (platform, server_id, wiki_id)
    );

    -- A person's birthday, day and month only, under the account that set it
    -- with /birthday set (a linked pair keeps one row, on its Discord side).
    -- Cleared by /birthday set off and by nothing else.
    CREATE TABLE IF NOT EXISTS birthdays (
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        day INTEGER NOT NULL,
        month INTEGER NOT NULL,
        set_at INTEGER,
        PRIMARY KEY (platform, user_id)
    );

    -- The numbers of the birthday greetings used last, per channel (scope
    -- 'chat') and per person (scope 'user'), trimmed to the last five of each
    -- key on every insert: that is the whole memory the no-repeat rule needs.
    CREATE TABLE IF NOT EXISTS birthday_greeting_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        number INTEGER NOT NULL,
        used_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_birthday_greeting_history
        ON birthday_greeting_history (scope, key);

    -- Discord roles the bot gave itself ('birthday' or 'rep'), so that it
    -- takes back only those: a role somebody granted by hand is never removed.
    CREATE TABLE IF NOT EXISTS role_grants (
        server_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        user_id TEXT NOT NULL,
        role_id TEXT NOT NULL,
        granted_at INTEGER,
        PRIMARY KEY (server_id, kind, user_id)
    );

    -- The seven-day setup deadline (setup_deadline.py). One row per server or
    -- group the bot was added to AFTER the rule came into force — everything
    -- it was already sitting in has no row and is never examined. joined_at
    -- is what the deadline counts from; on Discord it is only a record, since
    -- Guild.me.joined_at is authoritative there and survives a restart, while
    -- Telegram offers nothing of the kind and the my_chat_member update that
    -- adds the bot is the only moment the time can be learnt. settled_at is
    -- set the first time the chat is found bound to a union with /server setup, and
    -- is what makes the check one-shot: a chat set up once is never left.
    CREATE TABLE IF NOT EXISTS setup_deadlines (
        platform TEXT NOT NULL,
        server_id TEXT NOT NULL,
        joined_at INTEGER,
        settled_at INTEGER,
        PRIMARY KEY (platform, server_id)
    );

    -- Sponsor-owned unions remain distinct from operator unions. A lapsed
    -- tier leaves the union and its economy records in place.
    CREATE TABLE IF NOT EXISTS sponsor_tiers (
        discord_id TEXT PRIMARY KEY,
        tier INTEGER NOT NULL,
        checked_at INTEGER NOT NULL,
        grace_since INTEGER,
        grace_tier INTEGER,
        community_role_since INTEGER,
        community_role_present INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sponsor_unions (
        code TEXT PRIMARY KEY,
        discord_id TEXT NOT NULL,
        claimed_at INTEGER NOT NULL,
        archived_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS sponsor_activity_usage (
        discord_id TEXT NOT NULL,
        day TEXT NOT NULL,
        day_units INTEGER NOT NULL DEFAULT 0,
        minute_started INTEGER NOT NULL DEFAULT 0,
        minute_units INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (discord_id, day)
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
    the bare id at somebody. `unions` learns its own language (NULL reads as
    English), logo and logo type, all set from /union edit. A community's
    settings learn the tax it charges on payments, sales and message
    earnings and the parts of the economy it switched off, as comma-separated
    codes (/server edit, economy/switches.py), and its founding day (a UTC
    timestamp) and invitation link once its administrators add them
    (/server birthday). A birthday learns the name its owner had when
    they set it, which is what /calendar shows outside their community."""
    with _db_lock:
        _ensure_column("unions", "lang", "TEXT")
        _ensure_column("unions", "logo", "BLOB")
        _ensure_column("unions", "logo_mime", "TEXT")
        _ensure_column("community_settings", "tax_percent", "REAL NOT NULL DEFAULT 0")
        _ensure_column("community_settings", "econ_off", "TEXT")
        _ensure_column("community_settings", "founded_at", "INTEGER")
        _ensure_column("community_settings", "invite_url", "TEXT")
        _ensure_column("birthdays", "display_name", "TEXT")
        _ensure_column("chat_settings", "updated_at", "INTEGER")
        _ensure_column("chat_settings", "is_dm", "INTEGER DEFAULT 0")
        _ensure_column("chats", "title", "TEXT")
        _ensure_column("chats", "neutral_disabled", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column("sponsor_tiers", "grace_since", "INTEGER")
        _ensure_column("sponsor_tiers", "grace_tier", "INTEGER")
        _ensure_column("sponsor_tiers", "community_role_since", "INTEGER")
        _ensure_column("sponsor_tiers", "community_role_present", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column("sponsor_unions", "archived_at", "INTEGER")
        _ensure_column("enterprises", "archived_union_code", "TEXT")

def _ensure_column(table, column, ddl):
    """Guarded ALTER TABLE ... ADD COLUMN for SQLite (no IF NOT EXISTS there)."""
    have = {r["name"] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    if not have:
        return
    if column not in have:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

def init_olympiad():
    """Create the Olympiad's tables.

    Called from `init()` on every start, even though an Olympiad exists only
    now and then: the tables are cheap and their absence would turn the first
    `/olympiad set` of a deployment into an error. Almost everything they hold
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
