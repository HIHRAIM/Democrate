"""Banks and the money in them: the currency row, its leaders, the accounts,
the append-only journal, and energy.

Money is an integer count of MINOR units (hundredths) everywhere in this
module — nothing here has ever seen a decimal point, which is
economy/money.py's business alone. Balances are signed: a fine may push an
account below zero, a negative balance is that owner's debt to the bank, and
incoming money pays it down by ordinary addition. Every change goes through
`mint`, `burn` or `transfer`, each of which writes a `transactions` row under
the connection lock; nothing else may touch `accounts.balance`.

Energy is the second resource and sits on the same account row. It is earned
from messages beside currency and spent by production, and `spend_energy`
charges the whole batch before a run starts — a run that cannot pay does not
begin rather than stopping half way.

An owner is a (type, platform, id) triple: a user (through
db/users.py: canonical_user), a party (`party_owner`), an enterprise
(db/enterprises.py: enterprise_owner) or the bank itself (`_bank_owner`, the
counterparty every emission and every burn is written against).

Not this module's zone: goods and what they are worth (db/goods.py),
treaties, rates, wages and dues (db/trade.py), and every formula
(economy/).
"""
import json
import time

from db import conn, cur, _db_lock, _now

def party_owner(code):
    """The owner triple a party's accounts are keyed by.

    Parties have no platform — a party is a union-wide object and its members may
    be on either side — so the platform slot is deliberately the empty string
    rather than None: it is part of a primary key, and NULL would not compare
    equal to itself."""
    return "party", "", str(code)

def _bank_owner(code):
    """The owner triple standing for the bank itself.

    Every emission is written in the journal as a transfer *from* this owner and
    every burn as one *to* it, which is what makes the journal balance: money is
    never created without a counterparty, it just happens to be the issuer. The
    bank holds no account row, so nothing accumulates there."""
    return "bank", "", str(code)

def create_bank(code, union_code, central_chat, currency_name, emoji,
                leader_platform, leader_id, leader_name):
    """Found a bank and make its creator the first leader, in one transaction.

    The initial published value is `ECONOMY['fx_min_value']`, which is what
    economy/trade.py: recompute_value gives a currency with no messages and
    no goods yet — the value is correct from the first moment, and the daily
    recompute follows the currency's activity from there. `code` must
    already have been checked against the shared namespace by the caller."""
    from config import ECONOMY
    with _db_lock:
        cur.execute(
            "INSERT INTO banks (code, union_code, central_chat, currency_name, emoji,"
            " value, period_goods_value, period_energy, created_at)"
            " VALUES (?,?,?,?,?,?,0,0,?)",
            (code, union_code, str(central_chat), currency_name, emoji,
             float(ECONOMY["fx_min_value"]), _now()))
        cur.execute(
            "INSERT OR REPLACE INTO bank_leaders (bank_code, platform, user_id, display_name)"
            " VALUES (?,?,?,?)",
            (code, leader_platform, str(leader_id), leader_name))
        conn.commit()

def get_bank(code):
    """A bank's row by its currency code, or None when it was deleted.

    The code is the primary key and the thing users type into `/bank pay` and
    `/bank convert`, which is why renaming goes through `rename_bank_code` rather than
    an UPDATE of a field. Callers must handle None: a deleted bank still appears
    in old transaction rows."""
    return cur.execute(
        "SELECT b.* FROM banks b LEFT JOIN sponsor_unions su"
        " ON su.code=b.union_code WHERE b.code=?"
        " AND (su.archived_at IS NULL)", (code,)
    ).fetchone()

def get_all_banks():
    """Every bank in the deployment, by code — the daily FX recompute's work
    list."""
    return cur.execute(
        "SELECT b.* FROM banks b LEFT JOIN sponsor_unions su"
        " ON su.code=b.union_code WHERE su.archived_at IS NULL ORDER BY b.code"
    ).fetchall()

def get_banks_in_union(union_code):
    """The banks of one union. A union may hold any number, and this is what
    decides whether a command has to ask which currency it means."""
    return cur.execute(
        "SELECT b.* FROM banks b LEFT JOIN sponsor_unions su"
        " ON su.code=b.union_code WHERE b.union_code=?"
        " AND su.archived_at IS NULL ORDER BY b.code", (union_code,)).fetchall()

def get_banks_in_chat(chat_id):
    """The banks anchored to one server as their central chat — what
    `/bank add` checks against and what the server's card lists."""
    return cur.execute(
        "SELECT b.* FROM banks b LEFT JOIN sponsor_unions su"
        " ON su.code=b.union_code WHERE b.central_chat=?"
        " AND su.archived_at IS NULL ORDER BY b.code", (str(chat_id),)).fetchall()

def set_bank_value(code, value):
    """Publish a currency's new value.

    Written by the daily FX recompute and, at startup, by
    economy/trade.py: reprice_all, both with recompute_value's result and
    nothing else: the value is the denominator of every conversion rate, so an
    ad-hoc write here would move every rate in the union at once."""
    cur.execute("UPDATE banks SET value=? WHERE code=?", (float(value), code))
    conn.commit()

def _add_bank_day(code, column, delta):
    """Add to one column of the currency's row for today (UTC) in
    `bank_days`."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    cur.execute(
        f"INSERT INTO bank_days (bank_code, day, {column}) VALUES (?,?,?)"
        f" ON CONFLICT(bank_code, day) DO UPDATE SET {column}={column}+excluded.{column}",
        (code, day, int(delta)))
    conn.commit()

def add_period_energy(code, delta):
    """Count message energy earned in a currency today — the energy term of
    the FX recompute, averaged over thirty days."""
    _add_bank_day(code, "energy", delta)

def add_period_goods_value(code, delta):
    """Count the value of priced goods produced in a currency today, in
    minor units — the backing term of the FX recompute, averaged over thirty
    days."""
    _add_bank_day(code, "goods_value", delta)

def bank_days_since(code, since):
    """(day, goods value, energy) of a currency for every UTC day from the
    one holding the timestamp `since` up to today that has a row."""
    first = time.strftime("%Y-%m-%d", time.gmtime(since))
    return cur.execute("SELECT day, goods_value, energy FROM bank_days"
                       " WHERE bank_code=? AND day>=?", (code, first)).fetchall()

def bank_earners_since(code, since):
    """(day, owner platform, owner id, messages) of a currency: how many
    accepted earning messages each person had on each UTC day since the
    timestamp `since`. A `message` entry exists in the journal only after the
    earning gates and cooldowns accepted the message, and it is written to
    the canonical owner, so a linked pair of accounts is one person."""
    return cur.execute(
        "SELECT date(created_at,'unixepoch') AS day, to_platform, to_id, COUNT(*) AS n"
        " FROM transactions WHERE bank_code=? AND reason='message' AND created_at>=?"
        " GROUP BY day, to_type, to_platform, to_id",
        (str(code), int(since))).fetchall()

def cleanup_old_bank_days(days=180):
    """Keep the days whose weight in the FX recompute still counts — after
    half a year a day's weight has faded to a quarter of a percent."""
    first = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    cur.execute("DELETE FROM bank_days WHERE day<?", (first,))
    conn.commit()

BANK_EDITABLE_FIELDS = ("currency_name", "emoji", "econ_off")

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
    ("currency_forms", "bank_code"),
    ("treaties", "bank_a"),
    ("treaties", "bank_b"),
    ("bank_convert_fees", "bank_code"),
    ("bank_convert_fees", "target_code"),
    ("bank_days", "bank_code"),
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
    _forms_cache.clear()

_forms_cache = {}

def get_currency_forms(bank_code, lang):
    """The forms of a currency's name in one language as {form: word} —
    empty when its leaders filled in none. Read for every amount a text
    shows, so the answers are kept in memory until the forms change."""
    key = (bank_code, lang)
    if key not in _forms_cache:
        row = cur.execute("SELECT forms FROM currency_forms WHERE bank_code=? AND lang=?",
                          key).fetchone()
        _forms_cache[key] = json.loads(row["forms"]) if row else {}
    return _forms_cache[key]

def currency_form_langs(bank_code):
    """The languages a currency's name has forms in, sorted."""
    return [r["lang"] for r in cur.execute(
        "SELECT lang FROM currency_forms WHERE bank_code=? ORDER BY lang", (bank_code,))]

def set_currency_forms(bank_code, lang, forms):
    """Store a currency's name forms in one language, or forget them with an
    empty `forms`."""
    with _db_lock:
        if forms:
            cur.execute("INSERT INTO currency_forms (bank_code, lang, forms) VALUES (?,?,?)"
                        " ON CONFLICT(bank_code, lang) DO UPDATE SET forms=excluded.forms",
                        (bank_code, lang, json.dumps(forms, ensure_ascii=False)))
        else:
            cur.execute("DELETE FROM currency_forms WHERE bank_code=? AND lang=?",
                        (bank_code, lang))
        conn.commit()
    _forms_cache.clear()

def delete_bank(code):
    """Remove a bank and everything denominated in its currency: accounts and
    their journal, treaties, wages, party dues, earning channels, and the goods
    it prices together with the inventories, mastery and standing orders behind
    them. Cargo still in transit is handed back to its sender first, so nobody's
    warehouse loses goods to the deletion. Base goods are untouched: they carry
    no bank_code."""
    from db.enterprises import _refund_shipment

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
                      "earn_channels", "wages", "party_dues", "auto_exports",
                      "currency_forms", "bank_days"):
            cur.execute(f"DELETE FROM {table} WHERE bank_code=?", (code,))
        cur.execute("DELETE FROM treaties WHERE bank_a=? OR bank_b=?", (code, code))
        cur.execute("DELETE FROM bank_convert_fees WHERE bank_code=? OR target_code=?",
                    (code, code))
        cur.execute("UPDATE enterprises SET salary_bank=NULL WHERE salary_bank=?", (code,))
        cur.execute("UPDATE productions SET energy_bank=NULL WHERE energy_bank=?", (code,))
        cur.execute("DELETE FROM banks WHERE code=?", (code,))
        conn.commit()
    _forms_cache.clear()

def get_bank_leaders(code):
    """Everyone who leads a bank. A bank normally has one leader and may have
    several; there is no ordering, so no leader outranks another."""
    return cur.execute("SELECT * FROM bank_leaders WHERE bank_code=?", (code,)).fetchall()

def add_bank_leader(code, platform, user_id, display_name):
    """Add a co-leader without removing anyone.

    INSERT OR REPLACE on the (bank, platform, user) key, so re-adding the same
    person just refreshes their stored display name."""
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
    """Drop one leader. Nothing prevents removing the last one: a bank with no
    leaders keeps working and can only be given a new leader by a Bot Admin."""
    cur.execute(
        "DELETE FROM bank_leaders WHERE bank_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id)))
    conn.commit()

def is_bank_leader(code, platform, user_id):
    """Whether this account leads the bank.

    Asked per platform account and *not* through `canonical_user`: leadership is
    granted to the account that accepted the offer, so a linked person leading on
    Discord does not lead from Telegram."""
    return cur.execute(
        "SELECT 1 FROM bank_leaders WHERE bank_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))).fetchone() is not None

def user_led_banks(platform, user_id):
    """Every bank this account leads, by code.

    The list's length is what most commands care about: with exactly one, the
    currency argument may be omitted; with several, the command asks which."""
    return cur.execute(
        "SELECT b.* FROM banks b JOIN bank_leaders l ON l.bank_code=b.code"
        " LEFT JOIN sponsor_unions su ON su.code=b.union_code"
        " WHERE l.platform=? AND l.user_id=? AND su.archived_at IS NULL"
        " ORDER BY b.code",
        (platform, str(user_id))).fetchall()

def get_account(bank_code, owner_type, owner_platform, owner_id):
    """One owner's account in one bank, or None when they have none.

    The row carries both the balance and the energy — they are two columns of one
    account, not two tables."""
    return cur.execute(
        "SELECT * FROM accounts WHERE bank_code=? AND owner_type=? AND owner_platform=?"
        " AND owner_id=?",
        (bank_code, owner_type, owner_platform, str(owner_id))).fetchone()

def _starter_energy(owner_type, owner_platform, owner_id):
    """The energy a brand-new person starts with — enough for one production
    run and nothing more, or 0 for anyone who is not brand new.

    It is granted on the *first account a human ever opens*, not on every
    account: the second currency is not a second beginning, and an enterprise
    or a party is not a beginner at all. The point is only that somebody who
    has just been noticed by the bot can go and make something immediately
    instead of first writing messages at it for a quarter of an hour — so the
    grant has to cover a whole run of the bulkiest base category, three units
    of food, not one unit of anything."""
    if owner_type != "user":
        return 0
    from config import ECONOMY
    grant = int(ECONOMY.get("starter_energy", 30))
    if grant <= 0:
        return 0
    existing = cur.execute(
        "SELECT 1 FROM accounts WHERE owner_type='user' AND owner_platform=? AND owner_id=?"
        " LIMIT 1", (owner_platform, str(owner_id))).fetchone()
    return 0 if existing else grant

def ensure_account(bank_code, owner_type, owner_platform, owner_id, display_name=None):
    """Return the account id, opening the account on first use — with the
    starter energy when it is the owner's first account anywhere.

    A name that differs from the stored one (a new Telegram @username, a new
    Discord display name) is written to every account of the owner, not only
    this one: the leaderboards read the name from whichever account they
    reach first, and a name refreshed in one currency only would leave the
    old one on the other boards. Neither platform tells a bot about a
    rename, so the name catches up the next time the person is seen here."""
    with _db_lock:
        row = get_account(bank_code, owner_type, owner_platform, owner_id)
        if row:
            if display_name and row["display_name"] != display_name:
                cur.execute("UPDATE accounts SET display_name=? WHERE owner_type=?"
                            " AND owner_platform=? AND owner_id=?",
                            (display_name, owner_type, owner_platform, str(owner_id)))
                conn.commit()
            return row["id"]
        energy = _starter_energy(owner_type, owner_platform, owner_id)
        c = cur.execute(
            "INSERT INTO accounts (bank_code, owner_type, owner_platform, owner_id,"
            " balance, energy, display_name, created_at) VALUES (?,?,?,?,0,?,?,?)",
            (bank_code, owner_type, owner_platform, str(owner_id), energy,
             display_name, _now()))
        conn.commit()
        return c.lastrowid

def user_display_name(platform, user_id):
    """The name stored on any of this person's accounts, or None.

    Leaderboards and production records keep only the (platform, id) pair;
    this is where the name to show beside it comes from, without asking either
    platform's API for something the bot already wrote down."""
    row = cur.execute(
        "SELECT display_name FROM accounts WHERE owner_type='user' AND owner_platform=?"
        " AND owner_id=? AND display_name IS NOT NULL AND display_name<>''"
        " ORDER BY id LIMIT 1", (platform, str(user_id))).fetchone()
    return row["display_name"] if row else None

def top_balances(bank_code, limit=10):
    """The richest account holders of one currency, biggest first. People only
    — a party's or an enterprise's treasury is not in the same contest."""
    return cur.execute(
        "SELECT owner_platform AS p, owner_id AS i, balance, display_name FROM accounts"
        " WHERE bank_code=? AND owner_type='user' AND balance>0"
        " ORDER BY balance DESC LIMIT ?", (bank_code, int(limit))).fetchall()

def account_exists(bank_code, owner_type, owner_platform, owner_id):
    """Whether the owner already holds an account here. `/bank open` uses it
    to tell "opened" from "you already had one"; everything else opens on demand
    through `ensure_account`."""
    return get_account(bank_code, owner_type, owner_platform, owner_id) is not None

def get_owner_accounts(owner_type, owner_platform, owner_id):
    """Every account an owner holds, across all banks."""
    return cur.execute(
        "SELECT * FROM accounts WHERE owner_type=? AND owner_platform=? AND owner_id=?"
        " ORDER BY bank_code",
        (owner_type, owner_platform, str(owner_id))).fetchall()

def get_bank_accounts(bank_code, users_only=False):
    """Every account of a bank, optionally only the personal ones.

    `users_only` matters for the monthly wage run: parties and enterprises hold
    accounts too, and paying them a profession's wage would be nonsense."""
    sql = "SELECT * FROM accounts WHERE bank_code=?"
    if users_only:
        sql += " AND owner_type='user'"
    return cur.execute(sql, (bank_code,)).fetchall()

def bank_money_supply(bank_code):
    """Every balance of the currency added up, in minor units.

    Negative balances count as the debts they are, so the supply is what actually
    exists rather than what was ever issued. It is the denominator of the daily
    recompute's backing term, which is why `recompute_value` floors it at one
    whole unit before dividing."""
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
    """How many accounts the currency has — a bank card statistic, and the
    closest thing this economy has to a population count."""
    row = cur.execute(
        "SELECT COUNT(*) AS c FROM accounts WHERE bank_code=?", (bank_code,)).fetchone()
    return row["c"] or 0

def _record_tx(bank_code, from_owner, to_owner, amount, reason):
    """Append one row to the journal.

    Deliberately without a commit: it is always called inside a `with _db_lock`
    block that commits the balance change too, so the money movement and its record
    land together or not at all. Nothing ever updates or deletes a journal row."""
    ft, fp, fi = from_owner
    tt, tp, ti = to_owner
    cur.execute(
        "INSERT INTO transactions (bank_code, from_type, from_platform, from_id,"
        " to_type, to_platform, to_id, amount, reason, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (bank_code, ft, fp, str(fi), tt, tp, str(ti), int(amount), reason, _now()))

def _adjust(account_id, delta):
    """Add `delta` to an account's balance, no questions asked.

    The one place `accounts.balance` is written. Signed on purpose: balances may go
    negative, and a negative balance is the owner's debt to the bank. Callers do
    the checking — this is only the arithmetic — and every caller holds the lock
    and writes a journal row beside it."""
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
    """Recent journal rows, newest first: for one owner, for one bank, or the
    whole journal.

    The owner form matches both sides of a transaction, so a person's history shows
    what they received as well as what they paid."""
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
    """An owner's energy in one bank, 0 when they have no account there.

    Energy is a whole number and never negative — `spend_energy` refuses rather
    than overdrawing, unlike money."""
    ot, op, oi = owner
    acc = get_account(bank_code, ot, op, oi)
    return acc["energy"] if acc else 0

def add_energy(bank_code, owner, delta, display_name=None):
    """Credit energy, opening the account if this is the owner's first.

    Called on every counted message beside the currency reward. There is no
    ceiling: energy accumulates until production spends it."""
    ot, op, oi = owner
    with _db_lock:
        acc = ensure_account(bank_code, ot, op, oi, display_name)
        cur.execute("UPDATE accounts SET energy=energy+? WHERE id=?", (int(delta), acc))
        conn.commit()

def spend_energy(bank_code, owner, amount):
    """Charge energy, refusing when there is not enough. Returns whether it
    happened.

    The refusal is the point: production charges the whole batch here *before*
    starting, so a run that cannot pay never begins instead of stopping half
    way."""
    ot, op, oi = owner
    with _db_lock:
        acc = get_account(bank_code, ot, op, oi)
        if not acc or acc["energy"] < amount:
            return False
        cur.execute("UPDATE accounts SET energy=energy-? WHERE id=?", (int(amount), acc["id"]))
        conn.commit()
        return True

def set_bank_prev_value(code, value):
    """Remember yesterday's published value before overwriting today's.

    Exists only so the economic log channels can show a day-over-day change; the
    recompute clamps against the *live* value, not this one."""
    cur.execute("UPDATE banks SET prev_value=? WHERE code=?",
                (float(value) if value is not None else None, code))
    conn.commit()

def merge_owner_accounts(old_owner, new_owner):
    """Move every account of `old_owner` onto `new_owner`: the linking of a
    Telegram account to a Discord one, after which only the Discord side is
    addressed (db/users.py: canonical_user).

    Where the new owner already holds an account in that currency, the
    balance (debt included) and the energy are added to it and the old row
    goes; otherwise the old row is simply handed over. Every balance that
    moves leaves a `link_merge` row in the journal, so the money is seen
    changing hands rather than appearing. Returns how many accounts moved."""
    ot, op, oi = old_owner
    nt, np_, ni = new_owner
    with _db_lock:
        rows = cur.execute(
            "SELECT * FROM accounts WHERE owner_type=? AND owner_platform=? AND owner_id=?",
            (ot, op, str(oi))).fetchall()
        for row in rows:
            target = get_account(row["bank_code"], nt, np_, ni)
            if target is None:
                cur.execute("UPDATE accounts SET owner_type=?, owner_platform=?, owner_id=?"
                            " WHERE id=?", (nt, np_, str(ni), row["id"]))
            else:
                _adjust(target["id"], row["balance"])
                cur.execute("UPDATE accounts SET energy=energy+? WHERE id=?",
                            (row["energy"] or 0, target["id"]))
                cur.execute("DELETE FROM accounts WHERE id=?", (row["id"],))
            if row["balance"] > 0:
                _record_tx(row["bank_code"], old_owner, new_owner, row["balance"], "link_merge")
            elif row["balance"] < 0:
                _record_tx(row["bank_code"], new_owner, old_owner, -row["balance"], "link_merge")
        conn.commit()
        return len(rows)

def set_bank_tax(code, percent):
    """Set the tax a bank's leaders take on payments and sales in its
    currency, as the percentage typed (5 meaning five percent)."""
    cur.execute("UPDATE banks SET tax_percent=? WHERE code=?", (float(percent), code))
    conn.commit()

def set_bank_audit(code, platform, channel_key):
    """Choose where a bank's administrative events are posted — a Discord
    channel id or a Telegram 'chat:topic' — or clear it with (None, None)."""
    cur.execute("UPDATE banks SET audit_platform=?, audit_channel=? WHERE code=?",
                (platform, str(channel_key) if channel_key is not None else None, code))
    conn.commit()

def emitted_since(code, since):
    """How much a bank has issued by /bank emit since `since`, in minor units."""
    row = cur.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM transactions WHERE bank_code=?"
        " AND from_type='bank' AND (reason='emit' OR reason LIKE 'emit:%') AND created_at>=?",
        (code, int(since))).fetchone()
    return row["s"] or 0

def balance_rank(code, owner):
    """(place, holders) of a person among the people with money in one
    currency — the place on `/good top wealth` — or None when they have none."""
    ot, op, oi = owner
    acc = get_account(code, ot, op, oi)
    if ot != "user" or acc is None or acc["balance"] <= 0:
        return None
    above = cur.execute("SELECT COUNT(*) AS c FROM accounts WHERE bank_code=?"
                        " AND owner_type='user' AND balance>?", (code, acc["balance"])).fetchone()
    total = cur.execute("SELECT COUNT(*) AS c FROM accounts WHERE bank_code=?"
                        " AND owner_type='user' AND balance>0", (code,)).fetchone()
    return (above["c"] or 0) + 1, total["c"] or 0

def bank_flow_since(code, since):
    """What happened to a currency since `since`: {'issued': money the bank
    created, 'burned': money it took out, 'turnover': money that changed
    hands between holders}. Merging a linked account is bookkeeping, not
    trade, and is left out of the turnover."""
    def total(cond):
        row = cur.execute(f"SELECT COALESCE(SUM(amount),0) AS s FROM transactions"
                          f" WHERE bank_code=? AND created_at>=? AND {cond}",
                          (code, int(since))).fetchone()
        return row["s"] or 0
    return {"issued": total("from_type='bank'"),
            "burned": total("to_type='bank'"),
            "turnover": total("from_type<>'bank' AND to_type<>'bank' AND reason<>'link_merge'")}

def wealth_top_share(code, share=0.1):
    """The part of all people's money in one currency held by its richest
    tenth (at least one person), from 0 to 1 — or None when nobody holds
    any."""
    rows = cur.execute("SELECT balance FROM accounts WHERE bank_code=? AND owner_type='user'"
                       " AND balance>0 ORDER BY balance DESC", (code,)).fetchall()
    if not rows:
        return None
    total = sum(r["balance"] for r in rows)
    top = max(1, -(-len(rows) * share // 1))
    return sum(r["balance"] for r in rows[:int(top)]) / total if total else None
