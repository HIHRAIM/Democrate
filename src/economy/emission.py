"""Emission: a bank issuing its currency by decision, with /bank emit.

Every other way money enters the economy follows a rule — a message, the
daily meal, a wage. `/bank emit` is the one discretionary source: a bank's
leaders (or a Bot Admin) create money for a person, a party or an
enterprise of the bank's union, with a reason — a contest prize, a grant.
It is minted like any other issue, so it grows the money supply and the
next daily recompute prices the currency against it; the journal writes it
as `emit: <reason>`, and the bank's audit channel hears about it.

A leader's emissions in one calendar month (UTC) are capped at
`ECONOMY['emit_monthly_share']` (default 10%) of the currency's money supply,
but never below `ECONOMY['emit_monthly_floor']` minor units (default
100.00), so a young bank with no supply can still award a prize. Bot Admins
are not capped: they run the deployment, not the currency.

Not this module's zone: who may call it and how the target is named (the
two halves' commands/banks.py).
"""
import time
from datetime import datetime, timezone

import db
from config import ECONOMY

def _month_start():
    """Unix seconds of the first moment of the current UTC month."""
    now = datetime.now(timezone.utc)
    return int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())

def emit_cap(bank_code):
    """How much a bank's leaders may issue this month in total, in minor
    units."""
    share = float(ECONOMY.get("emit_monthly_share", 0.10))
    floor = int(ECONOMY.get("emit_monthly_floor", 10000))
    return max(floor, int(max(db.bank_money_supply(bank_code), 0) * share))

def emit_left(bank_code):
    """What a bank's leaders may still issue this month, in minor units."""
    return max(0, emit_cap(bank_code) - db.emitted_since(bank_code, _month_start()))

def emit(bank_code, owner, amount, reason=None, capped=True, display_name=None):
    """Issue `amount` of a currency to `owner`. Returns ('ok', {}) or
    ('over_cap', {'left': minor units still allowed}) when a capped caller
    would go past the month's limit."""
    amount = int(amount)
    if capped:
        left = emit_left(bank_code)
        if amount > left:
            return "over_cap", {"left": left}
    note = (reason or "").strip()[:200]
    db.mint(bank_code, owner, amount, f"emit: {note}" if note else "emit", display_name)
    return "ok", {"at": int(time.time())}
