"""Cross-community economy: the platform-neutral rules that both bots share.

`db.py` holds the storage and the atomic money primitives; this module holds the
*policy* built on top of them — how a message becomes currency and energy, how
crafting mastery grows a good's value and its craft cost, how the daily foreign-
exchange recompute prices each currency, and how the daily / monthly schedulers
mint wages, collect party dues, autocraft and autosend. Everything display-
related (embeds, HTML, localization) stays in the bots; the helpers here return
plain numbers and short status strings.

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
    The caller is responsible for the verified / not-a-command / not-a-bot gate."""
    if chars < ECONOMY["min_chars"]:
        return None
    owner = db.canonical_user(platform, user_id)
    a = _grant_activity(owner, chars)
    if a <= 0:
        return None
    currency = int(round(a * float(rate) * ECONOMY["currency_per_activity"]))
    energy = int(round(a * ECONOMY["energy_per_activity"]))
    if currency > 0:
        db.mint(bank_code, owner, currency, "message", display)
    if energy > 0:
        db.add_energy(bank_code, owner, energy, display)
        db.add_period_energy(bank_code, energy)
    return {"currency": currency, "energy": energy}

def craft(owner, good, display=None):
    """Spend energy to craft one unit into the owner's inventory. Returns
    (status, info): ('ok', {...}) or ('no_energy', {'cost', 'have'})."""
    bank_code = good["bank_code"]
    m = db.get_produced(owner, good["code"])
    cost = craft_cost(good, m)
    have = db.get_energy(bank_code, owner)
    if have < cost:
        return "no_energy", {"cost": cost, "have": have}
    if not db.spend_energy(bank_code, owner, cost):
        return "no_energy", {"cost": cost, "have": have}
    value = unit_value(good, m)
    db.add_inventory(owner, good["code"], 1)
    db.add_produced(owner, good["code"], 1)
    db.add_period_goods_value(bank_code, value)
    return "ok", {"cost": cost, "value": value, "level": mastery_level(m + 1), "produced": m + 1}

def sell(owner, good, qty):
    """Sell up to `qty` units back to the bank at the current unit value (the
    bank mints the proceeds). Returns (status, info)."""
    have = db.get_inventory_qty(owner, good["code"])
    qty = min(int(qty), have)
    if qty <= 0:
        return "nothing", {"have": have}
    m = db.get_produced(owner, good["code"])
    value = unit_value(good, m)
    total = value * qty
    db.add_inventory(owner, good["code"], -qty)
    db.mint(good["bank_code"], owner, total, "sell")
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

def run_autocraft():
    """Craft as much as standing energy allows for every autocraft subscription,
    up to a per-tick safety cap, then hand off to autosend."""
    crafted = 0
    for row in db.get_autocraft_all():
        owner = (row["owner_type"], row["owner_platform"], row["owner_id"])
        good = db.get_good(row["good_code"])
        if not good:
            continue
        for _ in range(int(ECONOMY["autocraft_cap"])):
            status, _info = craft(owner, good)
            if status != "ok":
                break
            crafted += 1
    return crafted

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
    """FX recompute (resetting the activity windows), then autocraft + autosend."""
    for bank in db.get_all_banks():
        value = recompute_value(bank)
        db.set_bank_value(bank["code"], value)
        db.reset_bank_periods(bank["code"])
    crafted = run_autocraft()
    sent = run_autosend()
    return {"crafted": crafted, "sent": sent}

def run_wages():
    """Pay every verified account holder the wage set for their profession."""
    paid = 0
    for bank_code in db.banks_with_wages():
        for acc in db.get_bank_accounts(bank_code, users_only=True):
            owner = (acc["owner_type"], acc["owner_platform"], acc["owner_id"])
            if not db.is_owner_verified(*owner):
                continue
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
    return {"wages": paid, "dues": collected}
