"""Turning energy into goods: what a manual run costs and yields, the run
itself, the 24/7 line behind /autocraft, and the daily redistribution behind
/autosend.

A manual run is a database row with a finish time, not a task: `start_production`
charges the energy up front and writes the row, and main.py: production_loop
polls for due rows and calls `finish_production`. That is what makes a pending
run survive a restart, and it is why the energy is spent before anything is
promised — a run that cannot pay never starts, rather than stopping half way.

Duration and batch size both grow with the quality level, and the good's
category sets the tempo (food quick and bulky, pharmacy slow and scarce).
`run_autoproduce` is the same arithmetic advanced by elapsed hours at
auto_efficiency of the manual tempo, with the fraction of a unit left over
carried on the autocraft row — capped below one unit, so downtime cannot bank
a burst.

Several goods may be produced at once — up to `parallel_production` of them,
one run of each — and both halves divide by the number of lines running: a
manual batch is cut by `split_qty` when it starts, an automatic line by its
owner's line count on every tick. Five goods at once therefore yield what one
good would, in five streams, which is what makes producing all five base
categories a choice about breadth rather than a way to produce five times as
much.

Mastery is credited to the *worker* even when the batch goes to an enterprise;
the enterprise accrues its own in parallel, which is what prices its warehouse.

Not this module's zone: the formulas themselves (economy/money.py), the
storage (db/goods.py) and the announcement of a finished batch (main.py).
"""
import random
import time

import db
from economy.money import _cat_cfg, _cfg, craft_cost, mastery_level, unit_value

def category_time_mult(category):
    """How much longer (or shorter) a production run takes for this good's
    category. 1.0 for a good with no category."""
    return float(_cat_cfg("category_time_mult", category, 1.0))

def category_yield(category):
    """How many units one run of this category yields before the quality
    bonus. Bulk categories yield more."""
    return float(_cat_cfg("category_yield", category, 1.0))

def produce_params(good, m):
    """(duration_seconds, units) of one manual production run at mastery `m`.
    Higher quality means a longer run but also a bigger and better batch; the
    category sets the base tempo (food is quick and bulky, pharmacy slow and
    scarce). Duration is clamped to at most produce_max_sec (~5 minutes)."""
    level = mastery_level(m)
    dur = (_cfg("produce_base_sec", 45)
           * category_time_mult(good["category"])
           * (1 + _cfg("produce_level_time", 0.15) * (level - 1)))
    dur = int(max(_cfg("produce_min_sec", 10), min(dur, _cfg("produce_max_sec", 300))))
    qty = max(1, int(round(category_yield(good["category"])
                           * (1 + _cfg("produce_level_yield", 0.10) * (level - 1)))))
    return dur, qty

def production_energy_cost(good, m, qty):
    """Energy for a whole run: the per-unit craft cost, unit by unit, as mastery
    grows inside the batch."""
    return sum(craft_cost(good, m + i) for i in range(int(qty)))

def batch_value(good, m, qty):
    """Total worth of a whole batch in minor units, summed unit by unit as
    mastery grows inside it — the same walk `production_energy_cost` does for the
    cost, so the two stay consistent.

    This is what is credited to the good's bank as goods backing, not what anyone
    is paid: nothing is sold here."""
    return sum(unit_value(good, m + i) for i in range(int(qty)))

def energy_bank_for(good, worker):
    """Which bank's energy pays for producing `good`: the good's own currency
    when it has one; for the currency-less base goods, the worker's account with
    the most energy."""
    if good["bank_code"]:
        return good["bank_code"]
    best, best_e = None, 0
    for acc in db.get_owner_accounts(*worker):
        if acc["energy"] > best_e:
            best, best_e = acc["bank_code"], acc["energy"]
    return best

def parallel_limit():
    """How many goods one person may keep in production at the same time."""
    return max(int(_cfg("parallel_production", 5)), 1)

def split_qty(qty, streams):
    """A batch cut into `streams` parallel lines. At least one unit per line:
    a category that yields one unit a run would otherwise produce nothing at
    all in parallel, and the energy is charged per unit either way, so the
    rounding buys nothing."""
    return max(1, int(qty) // max(int(streams), 1))

def start_production(worker, good, owner, server, notify, lang, starter_display=None):
    """Begin one timed manual run by `worker` (canonical user owner tuple); the
    batch lands in `owner`'s inventory (the worker, or their enterprise) when
    the run finishes. Energy is charged up front; the pending run survives
    restarts in the productions table.

    A worker may have up to `parallel_production` goods on the bench at once,
    but only one run of each. The batch is divided by the number of lines
    running — five goods at once yield what one good would, in five streams —
    and the energy follows the batch, so parallel work is a matter of breadth
    rather than of speed. Returns (status, info)."""
    active = db.get_active_production(worker[1], worker[2], good["code"])
    if active:
        return "busy", {"finish_at": active["finish_at"]}
    running = db.count_active_productions(worker[1], worker[2])
    limit = parallel_limit()
    if running >= limit:
        return "too_many", {"limit": limit, "running": running}
    m = db.get_produced(worker, good["code"])
    dur, qty = produce_params(good, m)
    qty = split_qty(qty, running + 1)
    cost = production_energy_cost(good, m, qty)
    bank = energy_bank_for(good, worker)
    have = db.get_energy(bank, worker) if bank else 0
    if not bank or have < cost:
        return "no_energy", {"cost": cost, "have": have}
    if not db.spend_energy(bank, worker, cost):
        return "no_energy", {"cost": cost, "have": have}
    if is_first_run_ever(worker):
        dur = 0
    finish_at = int(time.time()) + dur
    db.create_production(owner, (worker[1], worker[2]), good["code"], qty, bank,
                         server, notify, lang, finish_at, starter_display=starter_display)
    return "ok", {"duration": dur, "qty": qty, "cost": cost, "finish_at": finish_at,
                  "level": mastery_level(m), "first_run": dur == 0}

def is_first_run_ever(worker):
    """Whether this worker has never produced anything, of any good.

    Their first run finishes at once rather than after the usual minutes. Every
    other run is worth waiting for — this one is worth *seeing*: a person who
    has just met the bot should hold something they made before they decide
    whether any of this is for them. It happens once per person for as long as
    they play."""
    return db.total_produced(worker) <= 0

def _deliver_units(worker, owner, good, qty):
    """Credit a finished batch: inventory to the owner, mastery to the worker
    (and to the enterprise as well, so its stock is valued by its own output
    history), goods backing to the good's bank. Returns (total_value, level)."""
    m = db.get_produced(worker, good["code"])
    total_value = batch_value(good, m, qty)
    db.add_inventory(owner, good["code"], qty)
    db.add_produced(worker, good["code"], qty)
    if owner != worker and owner[0] == "enterprise":
        db.add_produced(owner, good["code"], qty)
    if good["bank_code"] and total_value > 0:
        db.add_period_goods_value(good["bank_code"], total_value)
    return total_value, mastery_level(m + qty)

def roll_lucky_batch(qty):
    """`(qty, lucky)` — now and then a manual run comes out better than it
    should have.

    The only die the economy rolled until now was the one that spoils cargo in
    transit, so the single surprise in the game was a loss. This is its
    opposite and it is deliberately small: it costs no extra energy, it cannot
    be aimed at, and it lands on the run somebody chose to start rather than on
    a line running by itself, because the point of it is to make the deliberate
    act the memorable one. Set `lucky_batch_chance` to 0 to switch it off."""
    chance = float(_cfg("lucky_batch_chance", 0.05))
    if chance <= 0 or random.random() >= chance:
        return qty, False
    factor = max(float(_cfg("lucky_batch_multiplier", 2.0)), 1.0)
    lucky_qty = max(qty + 1, int(round(qty * factor)))
    return lucky_qty, True

def finish_production(row):
    """Complete a due production row: deliver the batch and record the server
    statistics. Returns info for the completion notice, or None when the good
    vanished meanwhile.

    `level_before` rides along so the caller can tell a run that merely
    finished from one that raised the worker's quality — the notice for the
    second is worth sending where the first is not."""
    good = db.get_good(row["good_code"])
    db.delete_production(row["id"])
    if not good:
        return None
    worker = ("user", row["starter_platform"], row["starter_id"])
    owner = (row["owner_type"], row["owner_platform"], row["owner_id"])
    level_before = mastery_level(db.get_produced(worker, good["code"]))
    qty, lucky = roll_lucky_batch(int(row["qty"]))
    total_value, level = _deliver_units(worker, owner, good, qty)
    db.record_server_production(
        row["server_platform"], row["server_id"], good["code"], good["category"],
        "enterprise" if owner[0] == "enterprise" else "user",
        (worker[1], worker[2]), qty, total_value, level)
    return {"qty": qty, "value": total_value, "level": level, "good": good,
            "level_before": level_before, "lucky": lucky, "worker": worker}

def _dedup_owners(pairs):
    """Canonicalise (platform, user_id) pairs to wallet owners and de-duplicate,
    so a linked person is only touched once."""
    seen, out = set(), []
    for platform, uid in pairs:
        owner = db.canonical_user(platform, uid)
        if owner not in seen:
            seen.add(owner)
            out.append(owner)
    return out

def autocraft_slots(owner, good_code):
    """(running_lines_excluding_this_good, limit) for an owner about to switch
    a 24/7 line on — what `/autocraft` refuses on and what it reports."""
    return db.count_autocraft(owner, exclude_good=good_code), parallel_limit()

def run_autoproduce(hours=1.0):
    """Advance every 24/7 autoproduction subscription by `hours`. The line runs
    at auto_efficiency of the worker's manual tempo, divided between the owner's
    parallel lines — five goods at once advance at a fifth of the speed each, so
    breadth costs tempo rather than being free. Fractional units carry over
    between runs (capped below one unit so downtime cannot bank a burst), and a
    unit is only produced while the worker's energy covers its craft cost."""
    produced_total = 0
    cap = int(_cfg("autocraft_cap", 50))
    lines_per_owner = {}
    for row in db.get_autocraft_all():
        good = db.get_good(row["good_code"])
        if not good:
            continue
        owner = (row["owner_type"], row["owner_platform"], row["owner_id"])
        if row["starter_platform"] and row["starter_id"]:
            worker = ("user", row["starter_platform"], row["starter_id"])
        elif owner[0] == "user":
            worker = owner
        else:
            continue
        if owner not in lines_per_owner:
            lines_per_owner[owner] = max(db.count_autocraft(owner), 1)
        streams = lines_per_owner[owner]
        m = db.get_produced(worker, good["code"])
        dur, qty_run = produce_params(good, m)
        rate = _cfg("auto_efficiency", 0.5) * qty_run * 3600.0 / max(dur, 1) / streams
        amount = min(float(row["carry"] or 0.0), 1.0) + rate * hours
        units = min(int(amount), cap)
        made = 0
        total_value = 0
        for _ in range(units):
            cost = craft_cost(good, m + made)
            bank = energy_bank_for(good, worker)
            if not bank or not db.spend_energy(bank, worker, cost):
                break
            total_value += unit_value(good, m + made)
            made += 1
        if made:
            db.add_inventory(owner, good["code"], made)
            db.add_produced(worker, good["code"], made)
            if owner != worker and owner[0] == "enterprise":
                db.add_produced(owner, good["code"], made)
            if good["bank_code"] and total_value > 0:
                db.add_period_goods_value(good["bank_code"], total_value)
            db.record_server_production(
                row["server_platform"], row["server_id"], good["code"], good["category"],
                "enterprise" if owner[0] == "enterprise" else "user",
                (worker[1], worker[2]), made, total_value, mastery_level(m + made))
        db.set_autocraft_carry(owner, good["code"], amount - int(amount))
        produced_total += made
    return produced_total

def run_autosend():
    """Move the configured percentage of each owner's current stock of a good to
    the target user or party."""
    sent = 0
    for row in db.cur.execute("SELECT * FROM autosend WHERE percent>0").fetchall():
        owner = (row["owner_type"], row["owner_platform"], row["owner_id"])
        target = (row["target_type"], row["target_platform"], row["target_id"])
        if target == owner:
            continue
        have = db.get_inventory_qty(owner, row["good_code"])
        qty = have * int(row["percent"]) // 100
        if qty <= 0:
            continue
        db.add_inventory(owner, row["good_code"], -qty)
        db.add_inventory(target, row["good_code"], qty)
        sent += qty
    return sent
