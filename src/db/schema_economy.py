"""The economy's schema: banks, accounts, goods, enterprises, shipments and
the per-server production and consumption records, plus their migrations and
the five base goods that must exist in every deployment.

`init_economy()` is called from db/schema.py's `init()` and from nowhere else.
It ends with `_seed_base_goods()`, which is the one function in the schema
layer that writes *rows* rather than DDL: it inserts FOOD, HYGN, WARD, PHRM
and HOUS when they are missing and refreshes their energy cost when they are
not, so it is safe to run on every start but must stay exactly one function
called exactly once.

The base goods are also permanent occupants of the shared 4-character code
namespace (db/unions.py: code_taken), which is why their codes live here
beside the rows rather than in a config file.

Not this module's zone: the core schema (db/schema.py) and every accessor
reading these tables (db/banks.py, db/goods.py, db/enterprises.py,
db/logistics.py, db/serverstats.py, db/trade.py).
"""
from db import conn, cur, _db_lock, _now
from db.schema import _ensure_column

def init_economy():
    """Schema for the cross-community economy: banks and their currencies,
    accounts and the append-only transaction journal, goods/inventory/mastery,
    per-channel earning, FX treaties, wages and party dues, and the autocraft /
    autosend automation. Kept in its own script so the party/quiz core above
    stays readable.

    Called from db/schema.py: init() and from nowhere else. Ends with
    `_migrate_economy()` (additive column adds only) and then
    `_seed_base_goods()` — in that order, because the migration adds
    `goods.category`, which the seeding writes."""
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

    -- The spread a bank takes when money is converted *out of* its currency,
    -- set by that bank's own leaders. `target_code` is the currency being
    -- converted into, or '' for the bank's own default across every currency;
    -- the resolution order is the specific row, then the default row, then
    -- ECONOMY["convert_fee"]. Both columns hold bank codes and are therefore
    -- rewritten by db/banks.py: rename_bank_code, which is what keeps a fee
    -- attached to its currency when the currency is renamed. Read by
    -- economy/trade.py: convert_fee.
    CREATE TABLE IF NOT EXISTS bank_convert_fees (
        bank_code TEXT,
        target_code TEXT,
        fee REAL,
        PRIMARY KEY (bank_code, target_code)
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
        archived_union_code TEXT,
        founder_platform TEXT,
        founder_id TEXT,
        founder_name TEXT,
        description TEXT,
        logo BLOB,
        logo_mime TEXT,
        salary_period TEXT DEFAULT 'monthly',
        salary_bank TEXT,
        period_sales INTEGER DEFAULT 0,
        created_at INTEGER,
        neutral INTEGER NOT NULL DEFAULT 0
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
        neutral_units REAL NOT NULL DEFAULT 0,
        created_at INTEGER,
        PRIMARY KEY (platform, server_id, day, category)
    );
    CREATE INDEX IF NOT EXISTS idx_server_consumption
        ON server_consumption (platform, server_id, created_at);

    -- Accepted earning messages from people outside local enterprises.
    -- One row per person and UTC day; daily neutral production reads this,
    -- and retention removes it after the seven-day reporting window.
    CREATE TABLE IF NOT EXISTS neutral_activity (
        platform TEXT NOT NULL, server_id TEXT NOT NULL, day TEXT NOT NULL,
        user_platform TEXT NOT NULL, user_id TEXT NOT NULL,
        messages INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
        PRIMARY KEY (platform, server_id, day, user_platform, user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_neutral_activity_window
        ON neutral_activity (platform, server_id, updated_at);

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
        _ensure_column("enterprises", "neutral", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column("server_consumption", "neutral_units", "REAL NOT NULL DEFAULT 0")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_enterprises_one_neutral"
            " ON enterprises (platform, server_id) WHERE neutral=1")
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
    """Whether this good code is one of the five built-in base goods.

    A base good is not sellable, is worth nothing on the market and feeds a
    server's provision for free — which is why several code paths ask this
    before pricing anything (economy/trade.py: sell,
    economy/consumption.py: _pay_for_consumed)."""
    return code in BASE_GOOD_CODES

def _seed_base_goods():
    """Insert the five base goods, or refresh the energy cost of the ones that
    are already there.

    The one function in the schema layer that writes *rows*. It runs on every
    start, from `init_economy()` and nowhere else, and must stay exactly one
    function called exactly once — two copies would race on the same five
    primary keys. Idempotent by construction: a missing good is inserted with
    base_value 0 and no bank_code (that is what makes it a base good), and a
    present one only has its energy cost brought in line with the current
    `ECONOMY['base_good_energy']`. Everything else about an existing row —
    inventories, mastery, standing orders — is left alone.

    ECONOMY is imported here rather than at module level because the schema
    layer is imported before the process has necessarily read its config."""
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
