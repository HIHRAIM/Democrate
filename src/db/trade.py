"""What connects two banks or a bank and a party: conversion treaties,
manually pegged rates, profession wages and party dues.

A treaty is an unordered pair, which `_pair` enforces by sorting the two codes
before every read and write — otherwise A↔B and B↔A would be two rows and
half the lookups would miss. A pegged rate is stored on the treaty row and is
honoured only while the two banks are each other's sole partner; the check
itself is economy/trade.py: can_manual_peg.

Wages and dues are monthly obligations rather than transfers: this module only
remembers the amount attached to a profession or to a party's membership, and
economy/payroll.py is what actually moves the money once a month.

Not this module's zone: the computed value of a currency and the rate derived
from it (economy/trade.py), and the transfers themselves (db/banks.py).
"""
from db import conn, cur, _now

def _pair(a, b):
    """The two bank codes in canonical order (bank_a <= bank_b).

    Every read and write of a treaty goes through this. A treaty is a fact
    about a pair, not a direction, so storing it twice — or storing it one way
    and looking it up the other — would make half the lookups miss."""
    return (a, b) if a <= b else (b, a)

def add_treaty(a, b):
    """Sign a conversion treaty between two banks, making them convertible.

    INSERT OR IGNORE: signing an existing treaty again is a no-op rather than
    an error, and in particular it does not clear a pegged rate the pair has
    already agreed on."""
    x, y = _pair(a, b)
    cur.execute(
        "INSERT OR IGNORE INTO treaties (bank_a, bank_b, pegged_rate, created_at)"
        " VALUES (?,?,NULL,?)", (x, y, _now()))
    conn.commit()

def get_treaty(a, b):
    """The treaty row for a pair in either order, or None. The caller wants it
    mostly for `pegged_rate`, which is NULL until the pair fixes one."""
    x, y = _pair(a, b)
    return cur.execute("SELECT * FROM treaties WHERE bank_a=? AND bank_b=?", (x, y)).fetchone()

def has_treaty(a, b):
    """Whether two banks are convertible at all. `/convert` refuses without
    one, and a bank is always convertible with itself without a row."""
    return get_treaty(a, b) is not None

def remove_treaty(a, b):
    """Tear up a treaty. The pegged rate goes with it, so re-signing later
    starts on computed rates again."""
    x, y = _pair(a, b)
    cur.execute("DELETE FROM treaties WHERE bank_a=? AND bank_b=?", (x, y))
    conn.commit()

def treaty_partners(code):
    """Every bank this one has a treaty with, read from both columns.

    The list's *length* is what matters most: a manual peg is allowed only
    while it is exactly one (economy/trade.py: can_manual_peg), because a fixed
    edge inside a triangle of currencies lets the triangle disagree with
    itself."""
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
    """Set the monthly wage a bank pays for one profession, or clear it.

    `amount` is in minor units and `None` deletes the row — `/set-wage` maps a
    typed `0` onto that, so there is no such thing as a wage of zero. The
    profession is named by the good that defines it (db/goods.py:
    get_profession), which is why the key is a good code."""
    if amount is None:
        cur.execute("DELETE FROM wages WHERE bank_code=? AND good_code=?", (bank_code, good_code))
    else:
        cur.execute(
            "INSERT INTO wages (bank_code, good_code, amount) VALUES (?,?,?)"
            " ON CONFLICT(bank_code, good_code) DO UPDATE SET amount=excluded.amount",
            (bank_code, good_code, int(amount)))
    conn.commit()

def get_wage(bank_code, good_code):
    """The monthly wage for one profession in minor units, or 0 when none is
    set. Zero and unset are the same thing to every caller: the monthly tick
    skips both."""
    row = cur.execute(
        "SELECT amount FROM wages WHERE bank_code=? AND good_code=?",
        (bank_code, good_code)).fetchone()
    return row["amount"] if row else 0

def get_bank_wages(bank_code):
    """Every wage a bank pays, joined to the good that names the profession, so
    a card can print it with its emoji. A wage whose good was deleted drops out
    of the list through the JOIN — it is unreachable anyway."""
    return cur.execute(
        "SELECT w.good_code, w.amount, g.name, g.emoji FROM wages w"
        " JOIN goods g ON g.code=w.good_code WHERE w.bank_code=? ORDER BY g.name",
        (bank_code,)).fetchall()

def banks_with_wages():
    """The banks that pay any wage at all — the monthly tick's work list, so
    that a deployment where nobody set a wage costs one query a month."""
    rows = cur.execute("SELECT DISTINCT bank_code FROM wages").fetchall()
    return [r["bank_code"] for r in rows]

def set_party_dues(party_code, bank_code, amount):
    """Set a party's monthly member contribution in one currency, or clear it.

    `amount` is in minor units, and zero or None deletes the row. A party may
    hold dues in several currencies at once — one row per (party, bank) — and
    the monthly tick collects each separately."""
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
    """One party's dues, joined to their banks so a card can name the currency.
    Used for display only; the collection walks `all_party_dues` instead."""
    return cur.execute(
        "SELECT d.bank_code, d.amount, b.currency_name, b.emoji FROM party_dues d"
        " JOIN banks b ON b.code=d.bank_code WHERE d.party_code=?", (party_code,)).fetchall()

def all_party_dues():
    """Every dues row in the deployment — the monthly tick's work list
    (economy/payroll.py: run_party_dues)."""
    return cur.execute("SELECT * FROM party_dues").fetchall()
