"""Cross-community economy: the platform-neutral rules that both bots share.

`db.py` holds the storage and the atomic money primitives; this module holds the
*policy* built on top of them — how a message becomes currency and energy, how
crafting mastery grows a good's value and its craft cost, how timed production
runs and 24/7 autoproduction turn energy into goods for people and enterprises,
how the daily foreign-exchange recompute prices each currency, how a server's
provision and GDP are measured and its monthly tasks generated, and how the
daily / weekly / monthly schedulers mint wages, pay enterprise salaries, collect
party dues, run exports and autosend. Everything display-related (embeds, HTML,
localization) stays in the bots; the helpers here return plain numbers and short
status strings.

Money is always an integer number of *minor units* (hundredths). The tuning
constants all live in `config.ECONOMY`.
"""
import logging
import math
import re
import time

import db
from config import ECONOMY

logger = logging.getLogger("dem.economy")

CURRENCY_CODE_RE = re.compile(r"^[A-Za-z0-9]{4}$")
ENERGY_EMOJI = "⚡"

def _minor():
    return int(ECONOMY["minor_units"])

def format_amount(minor):
    """A bare decimal string for an integer minor-unit amount ('1234' → '12.34')."""
    minor = int(minor)
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    units = _minor()
    return f"{sign}{minor // units}.{minor % units:0{len(str(units)) - 1}d}"

def format_money(minor, bank):
    """'12.34 🪙 CODE' for an amount in a bank's currency."""
    emoji = (bank["emoji"] + " ") if bank["emoji"] else ""
    return f"{format_amount(minor)} {emoji}{bank['code']}"

def format_energy(n):
    return f"{int(n)} {ENERGY_EMOJI}"

_AMOUNT_RE = re.compile(r"^\d+(?:[.,]\d{1,2})?$")

def parse_amount(text):
    """Parse a positive decimal ('12', '12.5', '12,34') into minor units, or
    return None. Zero and negatives are rejected."""
    s = (text or "").strip()
    if not _AMOUNT_RE.match(s):
        return None
    s = s.replace(",", ".")
    units = _minor()
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "00")[:len(str(units)) - 1]
    else:
        whole, frac = s, "0" * (len(str(units)) - 1)
    try:
        value = int(whole) * units + int(frac)
    except ValueError:
        return None
    return value if value > 0 else None

def parse_salary(text):
    """A salary argument: '12.34' → ('amount', minor units), '5%' → ('percent',
    5.0) of the enterprise's period sales, '0' or '-' → ('clear', None).
    Returns None on bad input."""
    s = (text or "").strip()
    if s in ("0", "0.0", "0,0", "-"):
        return "clear", None
    if s.endswith("%"):
        try:
            p = float(s[:-1].replace(",", "."))
        except ValueError:
            return None
        return ("percent", p) if 0 < p <= 100 else None
    minor = parse_amount(s)
    return ("amount", minor) if minor is not None else None

def mastery_level(m):
    """Quality level for someone who has produced `m` units: 1 + floor(log2(m+1))."""
    return 1 + int(math.floor(math.log2(m + 1)))

def unit_value(good, m):
    """Current worth of one unit for a crafter of mastery `m`, in minor units:
    base_value * (1 + alpha * sqrt(m))."""
    return int(round(good["base_value"] * (1 + ECONOMY["alpha"] * math.sqrt(max(m, 0)))))

def craft_cost(good, m):
    """Energy to craft the next unit at mastery `m`: energy_cost * (1 + beta * level).
    Rising with the quality level keeps a good's value finite rather than free."""
    level = mastery_level(m)
    return int(math.ceil(good["energy_cost"] * (1 + ECONOMY["beta"] * level)))

_earn_state = {}

def message_activity(chars):
    """A = min(sqrt(chars), sqrt_cap): sublinear in length and capped, so a long
    post is rewarded but not without bound and spammy one-word posts pay ~0."""
    return min(math.sqrt(max(chars, 0)), ECONOMY["sqrt_cap"])

def _grant_activity(owner, chars):
    """Apply the per-user cooldown and rolling-hour cap. Returns the activity A
    actually granted (0 when on cooldown or the hour is already full)."""
    now = time.monotonic()
    if len(_earn_state) > 20000:
        stale = [k for k, v in _earn_state.items() if v["last"] < now - 3600]
        for k in stale:
            _earn_state.pop(k, None)
    key = "|".join(owner)
    st = _earn_state.setdefault(key, {"last": -1e9, "window": []})
    if now - st["last"] < ECONOMY["cooldown"]:
        return 0.0
    cutoff = now - 3600
    st["window"] = [(t, a) for (t, a) in st["window"] if t >= cutoff]
    used = sum(a for _, a in st["window"])
    remaining = ECONOMY["hourly_activity_cap"] - used
    if remaining <= 0:
        return 0.0
    a = min(message_activity(chars), remaining)
    if a <= 0:
        return 0.0
    st["last"] = now
    st["window"].append((now, a))
    return a

def earn_from_message(platform, user_id, display, bank_code, chars, rate=1.0):
    """Reward one counted message with currency (scaled by the channel `rate`)
    and energy at once. Returns {'currency', 'energy'} or None when the message
    earns nothing (too short, on cooldown, or the hour's cap is reached).
    The caller is responsible for the not-a-command / not-a-bot gate.

    A message that passes the gate opens the account before anything is paid
    out, so writing in an earning channel is enough to have one even when both
    rewards round down to zero. Nobody is told: the account appears silently,
    and /balance is where people find it."""
    if chars < ECONOMY["min_chars"]:
        return None
    owner = db.canonical_user(platform, user_id)
    a = _grant_activity(owner, chars)
    if a <= 0:
        return None
    db.ensure_account(bank_code, *owner, display_name=display)
    currency = int(round(a * float(rate) * ECONOMY["currency_per_activity"]))
    energy = int(round(a * ECONOMY["energy_per_activity"]))
    if currency > 0:
        db.mint(bank_code, owner, currency, "message", display)
    if energy > 0:
        db.add_energy(bank_code, owner, energy, display)
        db.add_period_energy(bank_code, energy)
    return {"currency": currency, "energy": energy}

def _cfg(key, default):
    """ECONOMY knob with a code-side default, so configs written before the
    production/provision update keep working unchanged."""
    return ECONOMY.get(key, default)

def _cat_cfg(dict_key, category, default):
    table = _cfg(dict_key, {})
    if not isinstance(table, dict):
        return default
    return table.get(category or "default", table.get("default", default))

def category_time_mult(category):
    return float(_cat_cfg("category_time_mult", category, 1.0))

def category_yield(category):
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

def start_production(worker, good, owner, server, notify, lang, starter_display=None):
    """Begin one timed manual run by `worker` (canonical user owner tuple); the
    batch lands in `owner`'s inventory (the worker, or their enterprise) when
    the run finishes. Energy is charged up front; the pending run survives
    restarts in the productions table. One run per worker at a time.
    Returns (status, info)."""
    active = db.get_active_production(worker[1], worker[2])
    if active:
        return "busy", {"finish_at": active["finish_at"]}
    m = db.get_produced(worker, good["code"])
    dur, qty = produce_params(good, m)
    cost = production_energy_cost(good, m, qty)
    bank = energy_bank_for(good, worker)
    have = db.get_energy(bank, worker) if bank else 0
    if not bank or have < cost:
        return "no_energy", {"cost": cost, "have": have}
    if not db.spend_energy(bank, worker, cost):
        return "no_energy", {"cost": cost, "have": have}
    finish_at = int(time.time()) + dur
    db.create_production(owner, (worker[1], worker[2]), good["code"], qty, bank,
                         server, notify, lang, finish_at, starter_display=starter_display)
    return "ok", {"duration": dur, "qty": qty, "cost": cost, "finish_at": finish_at,
                  "level": mastery_level(m)}

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

def finish_production(row):
    """Complete a due production row: deliver the batch and record the server
    statistics. Returns info for the completion notice, or None when the good
    vanished meanwhile."""
    good = db.get_good(row["good_code"])
    db.delete_production(row["id"])
    if not good:
        return None
    worker = ("user", row["starter_platform"], row["starter_id"])
    owner = (row["owner_type"], row["owner_platform"], row["owner_id"])
    qty = int(row["qty"])
    total_value, level = _deliver_units(worker, owner, good, qty)
    db.record_server_production(
        row["server_platform"], row["server_id"], good["code"], good["category"],
        "enterprise" if owner[0] == "enterprise" else "user",
        (worker[1], worker[2]), qty, total_value, level)
    return {"qty": qty, "value": total_value, "level": level, "good": good}

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

def run_autoproduce(hours=1.0):
    """Advance every 24/7 autoproduction subscription by `hours`. The line runs
    at auto_efficiency of the worker's manual tempo; fractional units carry over
    between runs (capped below one unit so downtime cannot bank a burst), and a
    unit is only produced while the worker's energy covers its craft cost."""
    produced_total = 0
    cap = int(_cfg("autocraft_cap", 50))
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
        m = db.get_produced(worker, good["code"])
        dur, qty_run = produce_params(good, m)
        rate = _cfg("auto_efficiency", 0.5) * qty_run * 3600.0 / max(dur, 1)
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

def run_daily_tick():
    """Consumption, then the FX recompute (resetting the activity windows),
    then autosend. Consumption runs first so the money it pays the enterprises
    is already in the supply the recompute prices against. The old value is
    kept in prev_value so the economic log channels can show the day-over-day
    change. Returns the summary plus the FX moves."""
    eaten = run_consumption()
    fx = []
    for bank in db.get_all_banks():
        value = recompute_value(bank)
        db.set_bank_prev_value(bank["code"], bank["value"])
        db.set_bank_value(bank["code"], value)
        db.reset_bank_periods(bank["code"])
        fx.append({"code": bank["code"], "old": bank["value"], "new": value})
    sent = run_autosend()
    return {"sent": sent, "fx": fx, "consumption": eaten}

def run_wages():
    """Pay every account holder the wage set for their profession."""
    paid = 0
    for bank_code in db.banks_with_wages():
        for acc in db.get_bank_accounts(bank_code, users_only=True):
            owner = (acc["owner_type"], acc["owner_platform"], acc["owner_id"])
            prof = db.get_profession(owner, bank_code)
            if not prof:
                continue
            wage = db.get_wage(bank_code, prof)
            if wage > 0:
                db.mint(bank_code, owner, wage, "wage")
                paid += 1
    return paid

def run_party_dues():
    """Collect each party's monthly dues from its members' accounts; a shortfall
    is allowed to push the member into debt."""
    collected = 0
    for row in db.all_party_dues():
        party_code, bank_code, amount = row["party_code"], row["bank_code"], row["amount"]
        if amount <= 0:
            continue
        target = db.party_owner(party_code)
        for owner in _dedup_owners(db.get_party_user_set(party_code)):
            db.transfer(bank_code, owner, target, amount, "party_dues",
                        allow_negative=True)
            collected += 1
    return collected

def run_monthly_tick():
    paid = run_wages()
    collected = run_party_dues()
    salaries = run_enterprise_salaries("monthly")
    return {"wages": paid, "dues": collected, "salaries": salaries}

def run_weekly_tick():
    """Weekly-paying enterprises settle their payroll and the recurring exports
    are executed."""
    salaries = run_enterprise_salaries("weekly")
    exported = run_auto_exports()
    return {"salaries": salaries, "exports": exported}

def resolve_salary(ent, platform, user_id, position):
    """A worker's salary in minor units: the personal override wins, then the
    salary of their position. Percent salaries are a share of the enterprise's
    period sales (income in its salary currency since the last payout).
    Returns 0 when nothing applies."""
    sales = int(ent["period_sales"] or 0)
    row = db.get_personal_salary(ent["code"], platform, user_id)
    if row is None and position:
        row = db.get_position_salary(ent["code"], position)
    if row is None:
        return 0
    if row["amount"] is not None:
        return int(row["amount"])
    if row["percent"] is not None:
        return int(round(sales * float(row["percent"]) / 100.0))
    return 0

def run_enterprise_salaries(period):
    """Pay the salaries of every enterprise on the given schedule ('weekly' or
    'monthly') out of its account. Payment stops when the account runs dry (an
    enterprise cannot go into debt for payroll); the sales period resets after
    the payout either way."""
    paid = 0
    for ent in db.get_all_enterprises():
        if (ent["salary_period"] or "monthly") != period:
            continue
        bank_code = ent["salary_bank"]
        if not bank_code or not db.get_bank(bank_code):
            continue
        source = db.enterprise_owner(ent["code"])
        seen = set()
        for platform, uid in sorted(db.get_enterprise_user_set(ent["code"])):
            worker = db.canonical_user(platform, uid)
            if worker in seen:
                continue
            seen.add(worker)
            member = db.get_enterprise_member(ent["code"], platform, uid)
            position = member["position"] if member else None
            amount = resolve_salary(ent, platform, uid, position)
            if amount <= 0:
                continue
            if db.transfer(bank_code, source, worker, amount, "salary",
                           allow_negative=False):
                paid += 1
        db.reset_enterprise_sales(ent["code"])
    return paid

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

def perished_qty(good, qty, seconds):
    """How many of `qty` units spoil over `seconds` in transit. Perishable
    categories decay by transport_perish_rate per hour (compounding); durable
    categories never spoil. At least one unit always survives a delivery."""
    rate = _cfg("transport_perish_rate", {}).get(good["category"], 0.0)
    if rate <= 0 or qty <= 0 or seconds <= 0:
        return 0
    hours = seconds / 3600.0
    survivors = qty * ((1.0 - rate) ** hours)
    lost = qty - int(math.ceil(survivors - 1e-9))
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

PROVISION_LEVELS = (
    (0.4, "insufficient"),
    (1.0, "acceptable"),
    (1.5, "good"),
)

def provision_level(score):
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

def month_bounds(month):
    """(start_ts, end_ts) of a 'YYYY-MM' month in UTC."""
    from datetime import datetime, timezone
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())

def prev_month(month):
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - (m == 1):04d}-{12 if m == 1 else m - 1:02d}"

def generate_monthly_task(platform, server_id, month):
    """Build (and store) the server's task for `month`: the mandatory part asks
    every base category to grow on last month's output (task_growth, with a
    floor of task_min_qty units); the optional part lists the server's top
    custom goods at the same growth. Returns the task item list."""
    import json
    start, end = month_bounds(prev_month(month))
    rows = db.server_category_production(platform, server_id, start, end)
    growth = _cfg("task_growth", 1.10)
    min_qty = int(_cfg("task_min_qty", 20))
    items = []
    for cat in db.GOOD_CATEGORIES:
        prev_qty = sum(r["qty"] for r in rows if r["category"] == cat)
        qty = max(min_qty, int(math.ceil(prev_qty * growth)))
        items.append({"kind": "category", "key": cat, "qty": qty, "mandatory": True})
    top = db.server_top_goods(platform, server_id, start, end,
                              limit=int(_cfg("task_optional_goods", 3)) + len(db.BASE_GOOD_CODES))
    added = 0
    for r in top:
        if db.is_base_good(r["good_code"]) or added >= int(_cfg("task_optional_goods", 3)):
            continue
        qty = max(max(min_qty // 4, 5), int(math.ceil((r["qty"] or 0) * growth)))
        items.append({"kind": "good", "key": r["good_code"], "qty": qty, "mandatory": False})
        added += 1
    db.save_server_task(platform, server_id, month, json.dumps(items))
    return items

def task_progress(platform, server_id, month):
    """The stored task with live progress: [{kind, key, qty, mandatory, done}].
    Returns None when the month has no task."""
    import json
    task = db.get_server_task(platform, server_id, month)
    if not task:
        return None
    start, end = month_bounds(month)
    rows = db.server_category_production(platform, server_id, start, min(end, int(time.time())))
    out = []
    for item in json.loads(task["spec_json"]):
        if item["kind"] == "category":
            done = sum(r["qty"] for r in rows if r["category"] == item["key"])
        else:
            done = sum(r["qty"] for r in rows if r["good_code"] == item["key"])
        out.append({**item, "done": done})
    return out

def evaluate_task(platform, server_id, month):
    """Mark the month's task completed/failed once it is over. Completed means
    every mandatory item reached its quantity. Returns True/False, or None when
    there was no task."""
    task = db.get_server_task(platform, server_id, month)
    if not task:
        return None
    progress = task_progress(platform, server_id, month)
    completed = all(p["done"] >= p["qty"] for p in progress if p["mandatory"])
    db.set_server_task_completed(task["id"], completed)
    return completed

def evaluate_finished_tasks(now=None):
    """Judge every past month's task that is still open — the provision bonus
    depends on the verdict even on servers that dropped their tasks channel."""
    month = time.strftime("%Y-%m", time.gmtime(now or time.time()))
    for task in db.get_unevaluated_tasks(month):
        evaluate_task(task["platform"], task["server_id"], task["month"])
