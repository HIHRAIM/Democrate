"""What a server's people eat every day, and the provision score that measures
whether they were fed.

The daily meal is a purchase, not a levy: each base category is taken out of
the warehouses of the enterprises based on that server — market goods first,
base goods as the filler behind them — and every market unit is paid for at the
unit value /sell would have fetched, counting into the enterprise's period
sales like any other sale. Base goods carry no market value and feed for free.
Nothing is ever taken from a personal inventory, and a server whose population
produced nothing that day is not fed at all.

Provision reads the consumption rows back rather than recomputing anything,
which is the whole point of the design: the score answers "was everyone fed?"
and not "was anyone busy?", so stock sitting in a warehouse counts and a
category nobody supplies drags the weighted average down however lively the
chat is. A category fed only by base goods is capped at 1.0 — base production
alone reaches acceptable and never higher.

GDP comes from the opposite table: the week's production valued at each
currency's published value, normalised across currencies.

Not this module's zone: the stored rows (db/serverstats.py), the monthly task
whose completion lifts the score (economy/tasks.py) and the rendering
(stats.py).
"""
import math
import time

import db
from economy.money import _cat_cfg, _cfg, _minor, mastery_level, unit_value
from economy.tasks import prev_month

PROVISION_LEVELS = (
    (0.4, "insufficient"),
    (1.0, "acceptable"),
    (1.5, "good"),
)

def provision_level(score):
    """The name of the band a provision score falls into.

    Read against PROVISION_LEVELS from the bottom up, so the first bound the score
    is *below* wins and anything past the last one is 'excellent'. The names are
    localization keys, not display text."""
    for bound, name in PROVISION_LEVELS:
        if score < bound:
            return name
    return "excellent"

def daily_need(category, population):
    """Units of one base category the day's active population wants. The weekly
    per-person figure divided over seven days, times the category's weight."""
    per_person = _cfg("prov_daily_units_per_person",
                      _cfg("prov_units_per_person", 5.0) / 7.0)
    return per_person * population * float(_cat_cfg("prov_category_weight", category, 1.0))

def _pay_for_consumed(owner, good, qty):
    """Pay an enterprise for what the population ate, at the unit value /sell
    would have fetched: consumption is a sale to the server, not a levy. Base
    goods carry no market value and so pay nothing — they feed for free. Like a
    sale, it counts into the period sales behind percent-of-sales salaries."""
    if not good["bank_code"] or db.is_base_good(good["code"]):
        return 0
    total = unit_value(good, db.get_produced(owner, good["code"])) * qty
    if total <= 0:
        return 0
    db.mint(good["bank_code"], owner, total, "consumption")
    ent = db.get_enterprise(owner[2])
    if ent and ent["salary_bank"] == good["bank_code"]:
        db.add_enterprise_sales(owner[2], total)
    return total

def consume_for_server(platform, server_id, day, now=None):
    """One server's daily meal.

    The population is the people who produced on the server that day; each base
    category is then eaten out of the warehouses of the enterprises based
    there, market goods first and base goods as the filler behind them. Every
    unit is bought from its enterprise, so feeding the server pays as well as
    selling to the bank. What the warehouses cannot cover is the shortfall the
    provision score is measured against — a server that produces nothing is
    hungry, however busy its chat is."""
    now = int(now or time.time())
    population = db.server_active_producers(platform, server_id, now - 86400, now)
    result = {"population": population, "categories": {}, "paid": 0}
    if population <= 0:
        return result
    enterprises = db.get_server_enterprises(platform, server_id)
    for cat in db.GOOD_CATEGORIES:
        need = daily_need(cat, population)
        want = int(math.ceil(need))
        taken, satisfaction, market = 0, 0.0, False
        for ent in enterprises:
            if taken >= want:
                break
            owner = db.enterprise_owner(ent["code"])
            for row in db.enterprise_category_stock(ent["code"], cat):
                if taken >= want:
                    break
                good = db.get_good(row["good_code"])
                if not good:
                    continue
                qty = min(int(row["qty"]), want - taken)
                if qty <= 0:
                    continue
                db.add_inventory(owner, good["code"], -qty)
                taken += qty
                if db.is_base_good(good["code"]):
                    satisfaction += qty
                else:
                    market = True
                    level = mastery_level(db.get_produced(owner, good["code"]))
                    satisfaction += qty * (1.0 + _cfg("prov_quality_bonus", 0.25)
                                           * max(level - 1, 0))
                    result["paid"] += _pay_for_consumed(owner, good, qty)
        db.record_server_consumption(platform, server_id, day, cat, population,
                                     need, taken, satisfaction, not market)
        result["categories"][cat] = {"need": need, "consumed": taken,
                                     "satisfaction": satisfaction}
    return result

def run_consumption(now=None):
    """Feed every set-up server once for the day."""
    now = int(now or time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    served = hungry = 0
    for chat in db.all_chats():
        res = consume_for_server(chat["platform"], chat["chat_id"], day, now)
        if not res["categories"]:
            continue
        served += 1
        if any(c["satisfaction"] < c["need"] for c in res["categories"].values()):
            hungry += 1
    return {"servers": served, "hungry": hungry}

def provision_report(platform, server_id, now=None):
    """The server's provision over the trailing 7 days: how much of what its
    people needed the warehouses actually covered. The daily consumption tick
    writes the need and the satisfaction per category; this only reads them
    back, so the score answers "was everyone fed?" rather than "was anyone
    busy?" — stock that sits unsold now counts for something."""
    now = int(now or time.time())
    since = now - 7 * 86400
    rows = db.server_consumption_window(platform, server_id, since, now)
    population = max(db.server_active_producers(platform, server_id, since, now), 1)
    weights = {c: float(_cat_cfg("prov_category_weight", c, 1.0))
               for c in db.GOOD_CATEGORIES}
    cats = {}
    for cat in db.GOOD_CATEGORIES:
        cat_rows = [r for r in rows if r["category"] == cat]
        need = sum(r["need"] or 0.0 for r in cat_rows)
        produced = sum(r["satisfaction"] or 0.0 for r in cat_rows)
        consumed = sum(r["consumed"] or 0 for r in cat_rows)
        base_only = all(r["base_only"] for r in cat_rows) if cat_rows else True
        cap = 1.0 if base_only else _cfg("prov_max_score", 2.0)
        score = min(produced / need, cap) if need > 0 else 0.0
        cats[cat] = {"produced": produced, "need": need, "score": score,
                     "base_only": base_only, "consumed": consumed}
    total_w = sum(weights.values()) or 1.0
    overall = sum(cats[c]["score"] * weights[c] for c in cats) / total_w
    task_bonus = False
    prev = prev_month(time.strftime("%Y-%m", time.gmtime(now)))
    task = db.get_server_task(platform, server_id, prev)
    if task and task["completed"]:
        overall = min(overall * (1 + _cfg("prov_task_bonus", 0.10)),
                      _cfg("prov_max_score", 2.0))
        task_bonus = True
    return {"score": overall, "level": provision_level(overall), "categories": cats,
            "population": population, "task_bonus": task_bonus}

def server_gdp(platform, server_id, now=None):
    """The week's production value, normalised across currencies by each bank's
    published value: Σ (minor units / minor) × value(bank). Base goods carry no
    market value and add 0."""
    now = int(now or time.time())
    rows = db.server_top_goods(platform, server_id, now - 7 * 86400, now, limit=1000)
    gdp = 0.0
    for r in rows:
        good = db.get_good(r["good_code"])
        if not good or not good["bank_code"]:
            continue
        bank = db.get_bank(good["bank_code"])
        if not bank:
            continue
        gdp += (r["value"] / _minor()) * (bank["value"] or 0.0)
    return gdp
