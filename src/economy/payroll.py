"""The three schedulers and everything they pay out: the daily, weekly and
monthly ticks, wages, party dues and enterprise salaries.

The order inside the daily tick is load-bearing: consumption runs first, so
that the money it pays the enterprises for the day's meal is already in the
money supply that the FX recompute prices against. Then the recompute resets
each bank's period windows, and autosend redistributes the day's stock.

The ticks are called by main.py: _run_due_economy_ticks, which decides what is
due by comparing a UTC period marker with the last one stored in bot_settings —
so this module is never asked to know what time it is or whether it has already
run today.

Signs and limits differ per payment on purpose. Party dues are an obligation:
a member who cannot pay is pushed into debt (allow_negative=True). Enterprise
salaries are not: payroll stops at the account's balance and the enterprise
never goes into debt for it, and the sales counter a percent salary is taken
from resets after each payout either way. Wages are an emission — minted, not
transferred — balanced by the sinks elsewhere (fines, dues, the conversion
spread).

Not this module's zone: what is being paid for (economy/consumption.py,
economy/trade.py, economy/logistics.py) and the amounts themselves
(db/trade.py, db/enterprises.py).
"""
import db
from economy.consumption import run_consumption
from economy.logistics import run_auto_exports
from economy.production import _dedup_owners, run_autosend
from economy.trade import recompute_value

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
    """The monthly tick: profession wages, party dues, then the payroll of
    the enterprises that pay monthly.

    Order matters here as it does in the daily tick — wages are minted first, so
    that a member paid this month can cover the dues collected in the same run
    rather than being pushed into debt by timing alone."""
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
