"""Goods in transit between enterprises: how far, how long, how much spoils,
and the dispatch and arrival of a shipment.

A shipment is modelled on a production run — a row with an arrival time, polled
by main.py: shipment_loop — for the same reason: it must survive a restart.
Dispatch is where both sides commit. The cargo leaves the seller's warehouse
immediately and a priced sale burns the buyer's payment into escrow
immediately; no funds means no dispatch. Arrival is where both are settled: the
surviving goods reach the buyer and the escrow is minted to the seller,
counting into its period sales when it lands in the salary currency.

Distance is a three-step ladder — same server, same union, different unions —
and spoilage is a *risk* attached to it rather than a toll taken from it. The
longest trip the map allows carries the category's full chance of spoiling; a
shorter one carries proportionally less, and durable categories carry none.
When the risk does come true the loss is a share of the batch, never more than
a third of it, and at least one unit always survives. Hauling food between
unions is therefore a gamble worth thinking about rather than an arithmetic
certainty, and the odds are stated against the half hour a long delivery really
takes instead of against an hour no delivery ever lasts.

If one enterprise vanishes mid-transit, everything goes to the survivor. The
same rule, from the deletion's side, is db/enterprises.py: _refund_shipment.

Not this module's zone: the shipment rows and the standing weekly contracts
(db/logistics.py), and the announcement of an arrival (main.py).
"""
import math
import random
import time

import db
from economy.money import _cfg

def enterprise_union(ent):
    """The union of an enterprise's home server/group, or None."""
    chat = db.get_chat(ent["server_id"])
    return chat["union_code"] if chat else None

def transport_distance(from_ent, to_ent):
    """'local' (same server/group), 'cross_server' (same union) or
    'cross_union'."""
    if (str(from_ent["platform"]), str(from_ent["server_id"])) == \
       (str(to_ent["platform"]), str(to_ent["server_id"])):
        return "local"
    ua, ub = enterprise_union(from_ent), enterprise_union(to_ent)
    if ua and ub and ua == ub:
        return "cross_server"
    return "cross_union"

def transport_time(from_ent, to_ent, qty):
    """(seconds, distance) a shipment of `qty` units needs to arrive."""
    dist = transport_distance(from_ent, to_ent)
    base = {
        "local": _cfg("transport_local_sec", 5),
        "cross_server": _cfg("transport_cross_server_sec", 300),
        "cross_union": _cfg("transport_cross_union_sec", 1800),
    }[dist]
    dur = int(base + _cfg("transport_per_unit_sec", 0) * max(int(qty), 0))
    return max(dur, 0), dist

def longest_transit():
    """The longest trip the map allows — a delivery between two unions. The
    yardstick every shorter trip's spoilage risk is measured against."""
    return max(float(_cfg("transport_cross_union_sec", 1800)), 1.0)

def perish_chance(good, seconds):
    """The probability that a shipment of `good` spoils at all over `seconds`
    in transit.

    Spoilage is a risk, not a toll. The longest haul the map allows carries the
    category's full `transport_perish_chance`, a shorter trip a proportionally
    smaller one, and a durable category none at all — so the odds are stated
    against the half-hour the longest delivery actually takes, rather than
    against an hour no delivery ever lasts."""
    chance = _cfg("transport_perish_chance", {}).get(good["category"], 0.0)
    if chance <= 0 or seconds <= 0:
        return 0.0
    return max(0.0, min(float(chance) * (seconds / longest_transit()), 1.0))

def perished_qty(good, qty, seconds, roll=None):
    """How many of `qty` units spoil in transit — usually none.

    One roll decides whether the batch was unlucky; when it was, the loss is a
    share of the batch drawn up to `transport_perish_max_share` (a third), so
    even a spoiled delivery mostly arrives. At least one unit always survives.
    `roll` takes a (chance) -> float pair of numbers in tests; production
    passes nothing and gets `random`."""
    chance = perish_chance(good, seconds)
    if chance <= 0 or qty <= 0:
        return 0
    rand = roll or random.random
    if rand() >= chance:
        return 0
    share = float(_cfg("transport_perish_max_share", 1.0 / 3.0)) * rand()
    lost = int(math.floor(qty * max(share, 0.0)))
    return max(0, min(qty - 1, lost))

def dispatch_shipment(from_code, to_code, good, qty, price, bank_code, notify, lang):
    """Send `qty` units of `good` from one enterprise to another. The cargo
    leaves the seller's warehouse now; a priced sale escrows the buyer's payment
    now (no funds, no dispatch). The goods (and the escrow) settle when the
    shipment arrives. Returns (status, info)."""
    seller = db.enterprise_owner(from_code)
    buyer = db.enterprise_owner(to_code)
    from_ent, to_ent = db.get_enterprise(from_code), db.get_enterprise(to_code)
    if not from_ent or not to_ent:
        return "bad_target", {}
    have = db.get_inventory_qty(seller, good["code"])
    qty = min(int(qty), have)
    if qty <= 0:
        return "no_goods", {"have": have}
    escrow = 0
    if price and price > 0:
        if not bank_code or not db.get_bank(bank_code):
            return "no_bank", {}
        if not db.burn(bank_code, buyer, price, "export_escrow", allow_negative=False):
            return "no_funds", {}
        escrow = price
    db.add_inventory(seller, good["code"], -qty)
    dur, dist = transport_time(from_ent, to_ent, qty)
    now = int(time.time())
    db.create_shipment(from_code, to_code, good["code"], qty, escrow, bank_code,
                       now, now + dur, notify, lang)
    return "ok", {"qty": qty, "duration": dur, "distance": dist, "arrive_at": now + dur}

def arrive_shipment(row):
    """Settle a due shipment: deliver the surviving goods to the buyer and pay
    the escrow to the seller (counting into its period sales when it lands in the
    salary currency). If a side vanished mid-transit, everything goes to the
    survivor. Returns info for the arrival notice, or None when both are gone."""
    db.delete_shipment(row["id"])
    good = db.get_good(row["good_code"])
    qty = int(row["qty"])
    escrow = int(row["escrow_amount"] or 0)
    bank_code = row["bank_code"]
    from_ent = db.get_enterprise(row["from_ent"])
    to_ent = db.get_enterprise(row["to_ent"])
    if not good:
        return None
    seller = db.enterprise_owner(row["from_ent"])
    buyer = db.enterprise_owner(row["to_ent"])
    if not to_ent or not from_ent:
        survivor = seller if to_ent is None else buyer
        survivor_ent = from_ent if to_ent is None else to_ent
        if survivor_ent:
            db.add_inventory(survivor, good["code"], qty)
            if escrow > 0 and bank_code and db.get_bank(bank_code):
                db.mint(bank_code, survivor, escrow, "shipment_refund")
        return None
    lost = perished_qty(good, qty, int(row["arrive_at"]) - int(row["dispatch_at"]))
    delivered = qty - lost
    db.add_inventory(buyer, good["code"], delivered)
    if escrow > 0 and bank_code and db.get_bank(bank_code):
        db.mint(bank_code, seller, escrow, "export")
        if from_ent["salary_bank"] == bank_code:
            db.add_enterprise_sales(row["from_ent"], escrow)
    return {"qty": delivered, "lost": lost, "good": good,
            "from_ent": from_ent, "to_ent": to_ent}

def run_auto_exports():
    """Dispatch every recurring export contract once (weekly tick). A contract
    silently waits out the weeks when the seller lacks stock or the buyer lacks
    funds; a delivered contract is a shipment that arrives later, like any
    export."""
    done = 0
    for row in db.get_auto_exports():
        good = db.get_good(row["good_code"])
        if not good:
            continue
        if not db.get_enterprise(row["from_ent"]) or not db.get_enterprise(row["to_ent"]):
            continue
        status, _ = dispatch_shipment(row["from_ent"], row["to_ent"], good,
                                      row["qty"], row["price"], row["bank_code"],
                                      (None, None), None)
        if status == "ok":
            done += 1
    return done
