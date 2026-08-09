"""Enterprises: the company row, its leaders, its workers, the positions and
salaries it defines, and its running sales counter.

An enterprise is shaped like a party with a warehouse: same 4-character code
from the shared namespace, same founder-and-leaders arrangement, same numbered
edit menu. What differs is that it belongs to the server it was founded on
rather than to the union, and that it holds goods — not in a table of its own
but as ordinary `inventory` and `mastery` rows under its own owner tuple
(`enterprise_owner`), which is what lets one set of production code serve both
a person and a company.

Salaries come in two shapes on purpose, and the sign matters: a fixed amount
is stored in minor units, a percentage is stored as the number the user typed
(5 meaning five percent, never 0.05). `period_sales` is what a percentage is
taken of, and it is reset by the payroll run that consumed it.

`_refund_shipment` lives here because deletion is what needs it: an enterprise
going away must hand any cargo and escrow still in flight to the surviving
counterpart. db/banks.py: delete_bank imports it at the call site for the
same reason.

Not this module's zone: shipments in normal flight (db/logistics.py), the
payroll run (economy/payroll.py), goods (db/goods.py).
"""
from db import conn, cur, _db_lock, _now
from db.banks import get_bank, mint
from db.goods import add_inventory

def enterprise_owner(code):
    """The owner triple an enterprise's accounts, inventory and mastery are
    keyed by.

    The platform slot is the empty string rather than None — an enterprise belongs
    to a server, not to a person's platform, and NULL would not compare equal to
    itself in a primary key. This triple is what lets one set of production and
    money code serve a person and a company alike."""
    return "enterprise", "", str(code)

def create_enterprise(code, name, platform, server_id, founder_platform, founder_id,
                      founder_name, description=None, logo=None, logo_mime=None):
    """Found an enterprise on a server and make its founder the first leader.

    The founder is written both onto the row and into `enterprise_leaders`, the
    same way a party's is. The server it is founded on is fixed for life: it
    decides which monthly task the enterprise works toward and how far its exports
    have to travel."""
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
    """An enterprise's row by code, or None once it is deleted. Callers must
    check — a shipment can outlive one of its two ends."""
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
    """The enterprises based on one server, by name.

    This is the list the daily consumption tick eats out of: only enterprises
    based on the server feed its population, and personal inventories never do."""
    return cur.execute(
        "SELECT * FROM enterprises WHERE platform=? AND server_id=? ORDER BY name",
        (platform, str(server_id))).fetchall()

def update_enterprise_field(code, field, value):
    """Write one plain enterprise column, refusing anything not on the list.

    `salary_period` ('weekly' or 'monthly') and `salary_bank` are here because
    they are ordinary settings; the identity columns — code, server, founder, logo
    — are not, and each has its own function."""
    assert field in ("name", "description", "salary_period", "salary_bank")
    cur.execute(f"UPDATE enterprises SET {field}=? WHERE code=?", (value, code))
    conn.commit()

def update_enterprise_logo(code, logo, logo_mime):
    """Replace an enterprise's logo, or clear it with (None, None)."""
    cur.execute("UPDATE enterprises SET logo=?, logo_mime=? WHERE code=?",
                (logo, logo_mime, code))
    conn.commit()

def rename_enterprise_code(old_code, new_code):
    """Give an enterprise a new code, rewriting every reference in one
    transaction.

    The longest such list in the schema, because an enterprise is addressed three
    different ways: by `enterprise_code` in its own tables, as an owner triple in
    accounts/inventory/mastery/autocraft/productions/goods, and by bare code in
    `auto_exports` and `shipments`. Miss one and its rows silently point at an
    enterprise that no longer exists."""
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
    """The enterprise's appointed leaders — not including the founder, who is
    always one; `is_enterprise_leader` joins the two."""
    return cur.execute(
        "SELECT * FROM enterprise_leaders WHERE enterprise_code=?", (code,)).fetchall()

def add_enterprise_leader(code, platform, user_id, display_name):
    """Appoint a co-leader without removing anyone. Always reached through a
    consent button."""
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
    """Strip one leader of their appointment. The founder is unaffected: their
    leadership lives on the enterprise row."""
    cur.execute(
        "DELETE FROM enterprise_leaders WHERE enterprise_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id)))
    conn.commit()

def is_enterprise_leader(code, platform, user_id):
    """Whether this account leads the enterprise, founder included. Asked per
    platform account, like every other per-object grant."""
    row = get_enterprise(code)
    if row and row["founder_platform"] == platform and row["founder_id"] == str(user_id):
        return True
    return cur.execute(
        "SELECT 1 FROM enterprise_leaders WHERE enterprise_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))).fetchone() is not None

def get_enterprise_members(code):
    """The workers, by display name. Leaders and the founder are not among
    them — they are workers too, but through the other tables."""
    return cur.execute(
        "SELECT * FROM enterprise_members WHERE enterprise_code=? ORDER BY display_name",
        (code,)).fetchall()

def get_enterprise_member(code, platform, user_id):
    """One worker's row, or None. Read mainly for `position`, which decides
    which salary applies when there is no personal override."""
    return cur.execute(
        "SELECT * FROM enterprise_members WHERE enterprise_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))).fetchone()

def add_enterprise_member(code, platform, user_id, display_name):
    """Hire a worker. Reached from an approved `/ent-join` and from nothing
    else; the position starts empty."""
    cur.execute(
        "INSERT OR REPLACE INTO enterprise_members"
        " (enterprise_code, platform, user_id, display_name) VALUES (?,?,?,?)",
        (code, platform, str(user_id), display_name))
    conn.commit()

def remove_enterprise_member(code, platform, user_id):
    """Dismiss a worker and drop their personal salary override with them, in
    one transaction.

    The override goes deliberately: leaving it behind would silently re-apply if
    the same person were hired again months later. Returns whether they were
    actually a member."""
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
    """Assign a worker to a position, or clear it with None.

    The position is a free-text label matched against `enterprise_positions`; a
    worker assigned to a position that does not exist simply earns nothing from
    it, which is the same as having none."""
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
    """The salary row of one position, or None. Read by `resolve_salary` only
    after a personal override has been ruled out."""
    return cur.execute(
        "SELECT * FROM enterprise_positions WHERE enterprise_code=? AND position=?",
        (code, position)).fetchone()

def get_enterprise_positions(code):
    """Every position the enterprise defines, by name — the `/enterprise` card
    and the assignment menus."""
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
    """A worker's salary override, or None when they earn their position's
    salary instead."""
    return cur.execute(
        "SELECT * FROM enterprise_salaries WHERE enterprise_code=? AND platform=? AND user_id=?",
        (code, platform, str(user_id))).fetchone()

def get_all_enterprises():
    """Every enterprise in the deployment, by code — the payroll run's work
    list, which then filters by salary period."""
    return cur.execute("SELECT * FROM enterprises ORDER BY code").fetchall()

def add_enterprise_sales(code, delta):
    """Track income in the salary currency since the last payout (the base for
    percent-of-sales salaries)."""
    cur.execute("UPDATE enterprises SET period_sales=period_sales+? WHERE code=?",
                (int(delta), code))
    conn.commit()

def reset_enterprise_sales(code):
    """Zero the sales counter after a payout.

    Called by the payroll run whether or not anything was actually paid, so that a
    percentage salary is always a share of the period just ended rather than of
    everything since the last successful payment."""
    cur.execute("UPDATE enterprises SET period_sales=0 WHERE code=?", (code,))
    conn.commit()
