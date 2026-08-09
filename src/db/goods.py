"""Goods and everything a person or an enterprise does with them:
inventories, mastery, professions, production runs and the standing orders
behind /autocraft and /autosend, plus the channels that earn a currency.

Mastery is the number of units of one good an owner has ever produced, and it
is the input to every formula in economy/money.py — quality level, unit value
and craft cost all read it, so `add_produced` is not bookkeeping but the thing
that makes production worth doing. A profession is derived from the same
table: the good someone has produced most of in a bank, unless a bank leader
overrode it.

Goods belong to people and enterprises; the bank in `bank_code` only
*denominates* one. A good with no bank is a base good, sellable to nobody.

Not this module's zone: banks, accounts and energy (db/banks.py), enterprises
as organizations (db/enterprises.py), shipments (db/logistics.py) and every
formula (economy/production.py, economy/money.py).
"""
from db import conn, cur, _db_lock, _now
from db.unions import generate_code

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
    """One good's row by code, or None once it is deleted.

    Callers must check: a good can vanish while a production run of it is
    still in flight, and `finish_production` treats that as the run being
    lost rather than crashing."""
    return cur.execute("SELECT * FROM goods WHERE code=?", (code,)).fetchone()

def get_bank_goods(bank_code):
    """Every good denominated in one currency, by name. Base goods are never
    here — they have no bank_code at all."""
    return cur.execute(
        "SELECT * FROM goods WHERE bank_code=? ORDER BY name", (bank_code,)).fetchall()

def find_bank_good(bank_code, query):
    """Find a bank's good by its code or its exact name, case-insensitively.

    Matching by name as well as by code is what lets `/set-wage` take a
    profession the way a person says it. Returns None on no match; there is no
    fuzzy fallback here, so the caller decides whether to suggest anything."""
    q = (query or "").strip()
    for g in get_bank_goods(bank_code):
        if g["code"].lower() == q.lower() or g["name"].lower() == q.lower():
            return g
    return None

def get_inventory_qty(owner, good_code):
    """How many units of one good an owner holds, 0 when none.

    `owner` is the (type, platform, id) triple — a user through
    canonical_user, a party, or an enterprise. Quantities are whole units;
    fractions live only in the autocraft carry."""
    ot, op, oi = owner
    row = cur.execute(
        "SELECT qty FROM inventory WHERE owner_type=? AND owner_platform=? AND owner_id=?"
        " AND good_code=?", (ot, op, str(oi), good_code)).fetchone()
    return row["qty"] if row else 0

def add_inventory(owner, good_code, delta):
    """Add `delta` units to an owner's stock — negative to take them away.

    Upsert under the connection lock, so two concurrent grants cannot lose one
    another. Nothing here forbids a negative total: callers clamp to what is
    actually held *before* calling (`sell`, `dispatch_shipment`,
    `consume_for_server` all do), because a partial take is a normal outcome
    and an exception would not be."""
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
    """An owner's non-empty stock joined to the goods rows, by name — what
    `/inventory` and the enterprise card print. Rows that fell to zero are
    filtered out rather than deleted, so the row survives as a cheap
    record."""
    ot, op, oi = owner
    return cur.execute(
        "SELECT i.good_code, i.qty, g.name, g.emoji, g.bank_code FROM inventory i"
        " JOIN goods g ON g.code=i.good_code"
        " WHERE i.owner_type=? AND i.owner_platform=? AND i.owner_id=? AND i.qty>0"
        " ORDER BY g.name", (ot, op, str(oi))).fetchall()

def get_produced(owner, good_code):
    """Total units of one good this owner has *ever* produced — the mastery
    `m` behind every formula in economy/money.py.

    It only ever grows: selling, giving away or losing goods does not lower it,
    because it measures experience rather than stock."""
    ot, op, oi = owner
    row = cur.execute(
        "SELECT produced FROM mastery WHERE owner_type=? AND owner_platform=? AND owner_id=?"
        " AND good_code=?", (ot, op, str(oi), good_code)).fetchone()
    return row["produced"] if row else 0

def add_produced(owner, good_code, delta):
    """Credit produced units to an owner's mastery.

    Called twice for one batch when a worker produces for an enterprise: once
    for the worker, whose personal mastery drives the formulas, and once for
    the enterprise, whose own mastery prices its warehouse."""
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
    """Pin an owner's profession in a bank, or clear the override with
    `good_code=None`.

    Clearing does not remove the profession — it falls back to the good they
    have produced most of, which is what `get_profession` computes."""
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
    """Make one channel or topic earn a bank's currency.

    `chan_key` is the channel id on Discord and the group's chat id on
    Telegram, and one row per (platform, key) is what lets several banks
    coexist on one server: each channel names the currency it feeds. `rate` is
    a multiplier on the currency reward only — energy is never scaled."""
    cur.execute(
        "INSERT INTO earn_channels (platform, chan_key, bank_code, rate) VALUES (?,?,?,?)"
        " ON CONFLICT(platform, chan_key) DO UPDATE SET bank_code=excluded.bank_code,"
        " rate=excluded.rate",
        (platform, str(chan_key), bank_code, float(rate)))
    conn.commit()

def remove_earn_channel(platform, chan_key):
    """Stop a channel earning. Returns whether it was earning at all, so
    `/set-earn off` can say which."""
    c = cur.execute("DELETE FROM earn_channels WHERE platform=? AND chan_key=?",
                    (platform, str(chan_key)))
    conn.commit()
    return c.rowcount > 0

def get_earn_channel(platform, chan_key):
    """The earning binding of a channel, or None — read on *every* message in
    both halves, which is why it is a single indexed lookup and why an
    unconfigured channel costs exactly one query."""
    return cur.execute(
        "SELECT * FROM earn_channels WHERE platform=? AND chan_key=?",
        (platform, str(chan_key))).fetchone()

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
    """Every enabled 24/7 production line — the hourly tick's work list.
    Disabled rows stay behind so that switching the line back on keeps its
    carried fraction."""
    return cur.execute("SELECT * FROM autocraft WHERE enabled=1").fetchall()

def set_autosend(owner, good_code, percent, target):
    """Set (or clear, with percent 0 or None) a daily transfer of a share of
    one good's stock.

    `percent` is stored as the whole number the user typed — 25 meaning a
    quarter, never 0.25. `target` is an owner triple and may be a user, a party
    or an enterprise; sending to yourself is filtered out by the daily tick
    rather than here."""
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
    """One standing autosend order, or None."""
    ot, op, oi = owner
    return cur.execute(
        "SELECT * FROM autosend WHERE owner_type=? AND owner_platform=? AND owner_id=?"
        " AND good_code=?", (ot, op, str(oi), good_code)).fetchone()

def create_production(owner, starter, good_code, qty, energy_bank, server, notify, lang,
                      finish_at, starter_display=None):
    """Put a started production run in flight and return its id.

    The row is the run: there is no task and no timer, so a restart loses
    nothing and main.py: production_loop finds it by `finish_at`. Everything
    the completion needs is frozen in — who worked (`starter`, whose mastery
    grows), who receives it (`owner`), which server the output counts for,
    where to announce it and in which language, and `energy_bank`, kept only as
    a record since the energy was already charged before this call."""
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
    """Runs whose finish time has passed, oldest first. Polled every 20
    seconds; runs the bot slept through are all due at once, in order."""
    return cur.execute(
        "SELECT * FROM productions WHERE finish_at<=? ORDER BY finish_at", (int(now_ts),)
    ).fetchall()

def get_active_production(starter_platform, starter_id):
    """The unfinished run a worker is busy with, if any (one job at a time)."""
    return cur.execute(
        "SELECT * FROM productions WHERE starter_platform=? AND starter_id=?"
        " ORDER BY finish_at LIMIT 1", (starter_platform, str(starter_id))).fetchone()

def delete_production(prod_id):
    """Remove a finished run. `finish_production` deletes the row before
    delivering anything, so a failure half way cannot deliver twice."""
    cur.execute("DELETE FROM productions WHERE id=?", (prod_id,))
    conn.commit()

def set_autocraft_carry(owner, good_code, carry):
    """Store the fraction of a unit left over after an hourly autoproduction
    advance.

    Written on every tick, including when nothing was produced. The reader caps
    it below one unit before using it, so a line that was idle for a week
    cannot bank a burst of production."""
    ot, op, oi = owner
    cur.execute(
        "UPDATE autocraft SET carry=? WHERE owner_type=? AND owner_platform=?"
        " AND owner_id=? AND good_code=?",
        (float(carry), ot, op, str(oi), good_code))
    conn.commit()
