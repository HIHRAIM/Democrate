"""Where a good becomes money and a currency becomes another currency:
selling to a bank, the daily currency valuation, and conversion between two
banks that signed a treaty.

`sell` is the economy's money source — the bank mints the proceeds — which is
why base goods, having no denominating bank, cannot be sold at all. Everything
else is a redistribution.

`recompute_value` is run once a day, per bank, and prices a currency from three
things measured over the period just ended: the value of the goods crafted in
it (backing), how much money exists (supply) and how much message energy was
earned (activity). The result is floored and clamped to a maximum daily move,
so a quiet day cannot collapse a currency and a busy one cannot spike it.

A conversion rate is simply value(from) / value(to). The manual peg overrides
it, and `can_manual_peg` is narrow on purpose: two banks may fix their rate
only while each is the other's sole treaty partner, because a fixed edge inside
a triangle of three currencies lets the triangle disagree with itself.

Not this module's zone: the treaty rows and the pegged rate itself
(db/trade.py), and the money primitives (db/banks.py).
"""
import math

import db
from config import ECONOMY
from economy.money import _minor, unit_value

def sell(owner, good, qty):
    """Sell up to `qty` units back to the good's bank at the current unit value
    (the bank mints the proceeds). Base-category goods carry no market value and
    cannot be sold. When an enterprise sells, the proceeds count into its period
    sales — the base for percent-of-sales salaries. Returns (status, info)."""
    if not good["bank_code"] or db.is_base_good(good["code"]):
        return "not_sellable", {}
    have = db.get_inventory_qty(owner, good["code"])
    qty = min(int(qty), have)
    if qty <= 0:
        return "nothing", {"have": have}
    m = db.get_produced(owner, good["code"])
    value = unit_value(good, m)
    total = value * qty
    db.add_inventory(owner, good["code"], -qty)
    db.mint(good["bank_code"], owner, total, "sell")
    if owner[0] == "enterprise":
        ent = db.get_enterprise(owner[2])
        if ent and ent["salary_bank"] == good["bank_code"]:
            db.add_enterprise_sales(owner[2], total)
    return "ok", {"qty": qty, "unit": value, "total": total}

def convertible(from_code, to_code):
    """Whether money can move between two currencies at all: the same one, or
    a pair that signed a treaty."""
    return from_code == to_code or db.has_treaty(from_code, to_code)

def rate(from_code, to_code):
    """Units of `to` obtained for one unit of `from`, or None when the two are
    not linked by a treaty. Honours a manual peg when the treaty carries one,
    otherwise divides the two published currency values."""
    if from_code == to_code:
        return 1.0
    treaty = db.get_treaty(from_code, to_code)
    if not treaty:
        return None
    lo, hi = sorted([from_code, to_code])
    if treaty["pegged_rate"] is not None:
        stored = treaty["pegged_rate"]
        return stored if from_code == lo else (1.0 / stored if stored else None)
    fb, tb = db.get_bank(from_code), db.get_bank(to_code)
    if not fb or not tb or not tb["value"]:
        return None
    return fb["value"] / tb["value"]

def can_manual_peg(bank_code, partner_code):
    """A pair may fix a manual rate only when it is each bank's *only* treaty
    partner — with two or more partners the triangle must stay on computed rates."""
    if not db.has_treaty(bank_code, partner_code):
        return False
    return (db.treaty_partners(bank_code) == [partner_code]
            and db.treaty_partners(partner_code) == [bank_code])

def convert(owner, from_code, to_code, amount, fee_reason="convert_fee"):
    """Convert between the owner's own accounts at the current rate, taking the
    configured spread as a sink. Returns (status, info)."""
    if from_code == to_code:
        return "same", {}
    r = rate(from_code, to_code)
    if r is None:
        return "not_convertible", {}
    fee = int(round(amount * ECONOMY["convert_fee"]))
    net = amount - fee
    credited = int(round(net * r))
    if credited <= 0:
        return "too_small", {}
    if not db.burn(from_code, owner, amount, "convert_out", allow_negative=False):
        return "no_funds", {}
    db.mint(to_code, owner, credited, "convert_in")
    return "ok", {"credited": credited, "rate": r, "fee": fee}

def recompute_value(bank):
    """The daily currency value: goods backing, divided by money supply, lifted
    by message activity, floored, and clamped to at most ±fx_daily_clamp of the
    previous value.

        backing  = period_goods_value / max(money_supply, minor_units)
        activity = 1 + fx_activity_weight * ln(1 + period_energy)
        value    = clamp(max(backing * activity, fx_min_value))
    """
    code = bank["code"]
    supply = max(db.bank_money_supply(code), _minor())
    backing = bank["period_goods_value"] / supply
    activity = 1 + ECONOMY["fx_activity_weight"] * math.log1p(max(bank["period_energy"], 0))
    target = max(backing * activity, ECONOMY["fx_min_value"])
    prev = bank["value"] or ECONOMY["fx_initial_value"]
    clamp = ECONOMY["fx_daily_clamp"]
    lo, hi = prev * (1 - clamp), prev * (1 + clamp)
    value = max(min(target, hi), lo, ECONOMY["fx_min_value"])
    return value
