"""Goods on the move: standing weekly export contracts and the shipments
actually in flight.

A shipment is a row with an arrival timestamp, which is what makes an export
survive a restart — main.py: shipment_loop polls for due rows rather than
holding a task. The row carries everything the arrival needs: the cargo, the
escrowed payment, the currency it is held in, and where to announce the
delivery.

`cleanup_old_shipments` is a safety net, not the normal path: a shipment is
deleted the moment it arrives, so anything still here long past its arrival
means both enterprises are gone.

Not this module's zone: the physics of a shipment — distance, transit time,
perishing (economy/logistics.py) — and the refund path when an enterprise is
deleted (db/enterprises.py: _refund_shipment).
"""
from db import conn, cur, _now

def add_auto_export(from_ent, to_ent, good_code, qty, price, bank_code, created_by):
    """Store a standing weekly export contract and return its id.

    `qty` is units per delivery and `price` the whole delivery's price in minor
    units, not per unit. Nothing is checked here: a contract whose seller has
    no stock or whose buyer has no funds simply waits out the week when the
    weekly tick tries it (economy/logistics.py: run_auto_exports)."""
    c = cur.execute(
        "INSERT INTO auto_exports (from_ent, to_ent, good_code, qty, price, bank_code,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (from_ent, to_ent, good_code, int(qty), int(price), bank_code,
         str(created_by), _now()))
    conn.commit()
    return c.lastrowid

def get_auto_exports(from_ent=None):
    """One enterprise's outgoing contracts, or every contract in the
    deployment when `from_ent` is omitted — the latter is what the weekly tick
    walks."""
    if from_ent is None:
        return cur.execute("SELECT * FROM auto_exports ORDER BY id").fetchall()
    return cur.execute("SELECT * FROM auto_exports WHERE from_ent=? ORDER BY id",
                       (from_ent,)).fetchall()

def remove_auto_export(from_ent, to_ent, good_code):
    """Cancel a contract, identified the way the user named it rather than by
    id. Returns whether one existed, so `/auto-export off` can say which."""
    c = cur.execute(
        "DELETE FROM auto_exports WHERE from_ent=? AND to_ent=? AND good_code=?",
        (from_ent, to_ent, good_code))
    conn.commit()
    return c.rowcount > 0

def create_shipment(from_ent, to_ent, good_code, qty, escrow_amount, bank_code,
                    dispatch_at, arrive_at, notify, lang):
    """Put a dispatched shipment in flight and return its id.

    Everything the arrival will need is frozen into the row, because the chat
    it was launched from may be unreachable by then: the cargo, the escrowed
    payment and the currency it is held in, both timestamps (the difference is
    what perishing is computed from), and `notify` — a (platform, chat key)
    pair, or None for a shipment nobody asked about, such as one a weekly
    contract sent on its own."""
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
    """Shipments whose arrival time has passed, oldest first.

    Polled every 20 seconds by main.py: shipment_loop. Rows the bot slept
    through are simply all due at once, in the order they should have landed —
    which is why the query is `<=` and ordered rather than a window."""
    return cur.execute(
        "SELECT * FROM shipments WHERE arrive_at<=? ORDER BY arrive_at", (int(now_ts),)
    ).fetchall()

def get_enterprise_shipments(ent_code):
    """Every shipment still in transit that this enterprise sends or receives."""
    return cur.execute(
        "SELECT * FROM shipments WHERE from_ent=? OR to_ent=? ORDER BY arrive_at",
        (ent_code, ent_code)).fetchall()

def delete_shipment(shipment_id):
    """Remove a shipment once it has been settled.

    economy/logistics.py: arrive_shipment deletes the row *first* and settles
    afterwards, so a failure half way cannot deliver the same cargo twice."""
    cur.execute("DELETE FROM shipments WHERE id=?", (shipment_id,))
    conn.commit()

def cleanup_old_shipments(max_age_seconds=7 * 86400):
    """Safety net: a shipment is normally removed the moment it arrives, but drop
    anything left far past its arrival (e.g. both enterprises long gone)."""
    cutoff = _now() - max_age_seconds
    cur.execute("DELETE FROM shipments WHERE arrive_at<?", (cutoff,))
    conn.commit()
