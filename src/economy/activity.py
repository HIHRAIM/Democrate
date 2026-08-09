"""Earning from messages: how many characters become how much currency and
energy, and the anti-abuse state that decides whether they become anything.

One counted message yields one activity value A, and A pays out twice — in
currency scaled by the channel's rate, and in energy. The caller (a message
handler in either half) is responsible for the not-a-command / not-a-bot /
is-this-an-earning-channel gate; this module is responsible for everything
after it.

`_earn_state` is the rate limiter and must exist exactly once: it holds each
person's last counted moment and their rolling hour of activity. It lives in
memory on purpose. Losing it to a restart costs one extra message per person,
while writing it down would cost a database transaction per message — and it
is a limiter, not a record of anything.

Not this module's zone: the earning channel itself (db/goods.py) and the
message handlers that call in (discord_bot/events.py,
telegram_bot/catchall.py).
"""
import math
import time

import db
from config import ECONOMY

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
