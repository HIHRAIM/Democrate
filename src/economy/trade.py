"""Where a good changes hands and a currency becomes another currency: selling
between producers, the daily currency valuation, and conversion between two
banks that signed a treaty.

`sell` mints nothing. A bank does not buy goods: producers and enterprises sell
to each other and to people, goods moving one way and money the other between
two accounts that already exist. The currency enters the economy through
messages (economy/activity.py) and through the daily meal the server buys off
its enterprises (economy/consumption.py), and those two are the only sources
there are. Base goods carry no market value and are handed over rather than
sold.

`recompute_value` is run once a day, per bank, and at startup, and publishes
its result as it is. A currency is worth the activity of the people who earn
it: a new one is worth fx_min_value, it grows as people write in its earning
channels — more for many people than for one person writing a lot — and as
priced goods are produced in it, and every day of that activity fades by a
thirtieth a day, so a currency nobody uses loses value slowly instead of
collapsing.

A conversion rate is simply value(from) / value(to). The manual peg overrides
it, and `can_manual_peg` is narrow on purpose: two banks may fix their rate
only while each is the other's sole treaty partner, because a fixed edge inside
a triangle of three currencies lets the triangle disagree with itself.

Not this module's zone: the treaty rows and the pegged rate itself
(db/trade.py), and the money primitives (db/banks.py).
"""
import math
import time
from datetime import date, datetime, timezone

import db
from config import ECONOMY
from economy.money import _minor, unit_value
from economy.switches import convert_refusal, deal_refusal

def asking_price(owner, good, qty):
    """What `qty` units are worth at the seller's own quality: the price
    `/good sell` offers when nobody named one.

    The same arithmetic the daily meal pays and the shipment escrow is measured
    against, so a good has one value across the economy however it changes
    hands."""
    return unit_value(good, db.get_produced(owner, good["code"])) * max(int(qty), 0)

def sell(seller, buyer, good, qty, price, bank_code, server=None):
    """Sell up to `qty` units of `good` from `seller` to `buyer` for `price`.

    Goods move between two holders and money between two accounts of the same
    currency: nothing is minted here. That is the point — a bank buys nothing,
    so producing goods is not a way to print money, and the currency enters the
    economy through messages and the daily meal instead.

    The buyer may be a person or an enterprise, and so may the seller. When an
    enterprise sells, the proceeds count into its period sales — the base for
    percent-of-sales salaries. Base-category goods carry no market value and are
    given away rather than sold (`/good give`). The seller pays the taxes of
    the community the sale was made in (`server`) and of the currency out of
    what it received (economy/taxes.py). A sale that touches a part of the
    economy switched off answers "off" with the refusal
    (economy/switches.py). Returns (status, info)."""
    if db.is_base_good(good["code"]):
        return "not_sellable", {}
    if seller == buyer:
        return "self", {}
    found = deal_refusal((seller, buyer), good, bank_code, price, server, server)
    if found:
        return "off", {"refusal": found}
    have = db.get_inventory_qty(seller, good["code"])
    qty = min(int(qty), have)
    if qty <= 0:
        return "nothing", {"have": have}
    price = int(price)
    if price < 0:
        return "bad_price", {}
    if price > 0:
        if not bank_code or not db.get_bank(bank_code):
            return "no_bank", {}
        if not db.transfer(bank_code, buyer, seller, price, "sell", allow_negative=False):
            return "no_funds", {}
    db.add_inventory(seller, good["code"], -qty)
    db.add_inventory(buyer, good["code"], qty)
    taxes = None
    if price > 0:
        from economy.taxes import withhold
        taxes = withhold(bank_code, seller, price, server)
    if price > 0 and seller[0] == "enterprise":
        ent = db.get_enterprise(seller[2])
        if ent and ent["salary_bank"] == bank_code:
            db.add_enterprise_sales(seller[2], taxes["net"])
    return "ok", {"qty": qty, "total": price, "taxes": taxes,
                  "unit": price // qty if qty else 0}

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

def convert_fee(from_code, to_code):
    """The spread taken when money leaves `from_code` for `to_code`.

    Set by the leaders of the bank the money is leaving, in three steps: the
    fee they set for this particular currency, then their own default for every
    currency, then the deployment's `ECONOMY["convert_fee"]`. The rows are keyed
    by bank code and rewritten with it (`db/banks.py: _BANK_CODE_COLUMNS`), so
    renaming a currency moves its fees along with it.

    Clamped to [0, 1): a fee of one would take the whole conversion and a
    negative one would print money out of a currency swap."""
    stored = db.get_convert_fee_row(from_code, to_code)
    if stored is None:
        stored = db.get_convert_fee_row(from_code, db.CONVERT_FEE_DEFAULT_KEY)
    if stored is None:
        stored = ECONOMY["convert_fee"]
    return min(max(float(stored), 0.0), 0.99)

def parse_convert_fee_input(text):
    """Read one line of the conversion-fee dialog into ``(target_code, fee)``,
    or None when it is not a usable line.

    The accepted shapes are `2.5` (this bank's default for every currency),
    `USD 1` (only when converting into USD) and `USD -` (drop that override, so
    the default applies again). Percent is what a fee is thought in and what
    every screen shows it in, so the dialog takes percent and stores the
    fraction — the one place the two units meet.

    Platform-neutral because both halves take the same line: the Telegram half
    must not reach into the Discord half for a parser."""
    parts = (text or "").strip().split()
    if not parts:
        return None
    if len(parts) == 1:
        target, raw = db.CONVERT_FEE_DEFAULT_KEY, parts[0]
    else:
        target, raw = parts[0].strip().upper(), parts[1]
    if raw in ("-", "—", "x", "X"):
        return target, None
    try:
        percent = float(raw.replace("%", "").replace(",", "."))
    except ValueError:
        return None
    if not 0 <= percent < 100:
        return None
    return target, percent / 100.0

def convert_fee_summary(bank_code, lang):
    """The bank's conversion spreads as one readable line, for the settings
    dialog and the `/bank info` card: the default first, then each currency that
    has one of its own. Percent, because that is what a fee is read in."""
    from utils import localized

    def _percent(fraction):
        text = f"{float(fraction) * 100:.2f}".rstrip("0").rstrip(".")
        return text or "0"

    default = db.get_convert_fee_row(bank_code, db.CONVERT_FEE_DEFAULT_KEY)
    parts = [localized("convert_fee_default", lang,
                       percent=_percent(default if default is not None
                                        else ECONOMY["convert_fee"]))]
    for row in db.get_bank_convert_fees(bank_code):
        if not row["target_code"]:
            continue
        parts.append(localized("convert_fee_for", lang, code=row["target_code"],
                               percent=_percent(row["fee"])))
    return ", ".join(parts)

def _pay_convert_fee(bank_code, amount, reason="convert_fee"):
    """Hand a conversion fee to the bank that charged it.

    A bank holds no account of its own (`db/banks.py: _bank_owner` is a journal
    counterparty, not a balance), so the fee is split evenly between its
    leaders, and the remainder of an uneven split goes to the first of them so
    that not a hundredth is invented or lost. A bank with no leaders left has
    nobody to pay: there the fee is simply burned, which is what it always was.
    Returns the number of leaders paid."""
    leaders = db.get_bank_leaders(bank_code)
    if not leaders or amount <= 0:
        return 0
    share, extra = divmod(amount, len(leaders))
    for index, leader in enumerate(leaders):
        part = share + (1 if index < extra else 0)
        if part <= 0:
            continue
        db.mint(bank_code, db.canonical_user(leader["platform"], leader["user_id"]),
                part, reason, display_name=leader["display_name"])
    return len(leaders)

def convert(owner, from_code, to_code, amount, fee_reason="convert_fee", place=None):
    """Convert between the owner's own accounts at the current rate, taking the
    spread the source bank charges. Exchange switched off in the community it
    is asked in (`place`) or in either currency answers "off" with the
    refusal (economy/switches.py). Returns (status, info).

    The fee is charged in the source currency and paid to that bank's leaders,
    so only the principal actually crosses into the other currency: the source
    supply falls by what left it and by nothing more. Before, the fee was
    destroyed — the only real money sink the economy had, and one that fired
    just often enough to be invisible."""
    if from_code == to_code:
        return "same", {}
    found = convert_refusal(from_code, to_code, place)
    if found:
        return "off", {"refusal": found}
    r = rate(from_code, to_code)
    if r is None:
        return "not_convertible", {}
    fee_rate = convert_fee(from_code, to_code)
    fee = int(round(amount * fee_rate))
    net = amount - fee
    credited = int(round(net * r))
    if credited <= 0:
        return "too_small", {}
    if not db.burn(from_code, owner, amount, "convert_out", allow_negative=False):
        return "no_funds", {}
    db.mint(to_code, owner, credited, "convert_in")
    paid = _pay_convert_fee(from_code, fee, fee_reason)
    return "ok", {"credited": credited, "rate": r, "fee": fee,
                  "fee_rate": fee_rate, "fee_paid_to": paid}

FX_FADE_DAYS = 30
FX_HISTORY_DAYS = 180

def _today(now=None):
    """The current UTC date."""
    return datetime.fromtimestamp(now or time.time(), timezone.utc).date()

def activity_by_day(code, since):
    """{UTC day: (people, messages, activity)} of a currency since the
    timestamp `since`. A day's activity is the sum over the people who earned
    that day of the square root of their accepted messages: ten people
    writing three messages each are worth 17, one person writing a hundred
    is worth 10, so the currency rewards a community talking rather than one
    member writing a lot."""
    days = {}
    for row in db.bank_earners_since(code, since):
        people, messages, activity = days.get(row["day"], (0, 0, 0.0))
        days[row["day"]] = (people + 1, messages + row["n"], activity + math.sqrt(row["n"]))
    return days

def recompute_value(bank, now=None):
    """A currency's value, computed from the activity behind it and from
    nothing else:

        fade(day)       = (1 - 1/30) ** (days since that day)
        score           = sum of activity(day) * fade(day)
        goods_per_day   = sum of goods_value(day) * fade(day) / 30
        energy_per_day  = sum of energy(day) * fade(day) / 30
        activity_target = weight * ln(1 + score)
        goods_target    = goods_per_day / max(money_supply, one unit)
                          * (1 + weight * ln(1 + energy_per_day))
        value           = max(goods_target, activity_target, fx_min_value)

    `activity` is `activity_by_day`'s; the goods and energy days are
    db/banks.py: bank_days. The fading weights of every day ever add up to
    thirty, so steady activity is priced like thirty days of it, while
    every day's part fades instead of dropping out at once: a currency whose
    people fall silent loses about a thirtieth of its score a day, and its
    value falls slower still. A currency with no activity yet — a new one —
    is worth fx_min_value, and it grows from there as people use it.

    The result is published as it is. The value used to approach a target
    by at most a tenth a day from the value before, which kept the published
    value and every rate built on it wrong for weeks after any change."""
    code = bank["code"]
    now = now or time.time()
    today = _today(now)
    fade = 1 - 1 / FX_FADE_DAYS
    weight_of = lambda day: fade ** max((today - date.fromisoformat(day)).days, 0)
    since = now - FX_HISTORY_DAYS * 86400
    score = sum(activity * weight_of(day)
                for day, (_people, _messages, activity) in activity_by_day(code, since).items())
    goods_per_day = energy_per_day = 0.0
    for row in db.bank_days_since(code, since):
        goods_per_day += row["goods_value"] * weight_of(row["day"]) / FX_FADE_DAYS
        energy_per_day += row["energy"] * weight_of(row["day"]) / FX_FADE_DAYS
    weight = ECONOMY["fx_activity_weight"]
    supply = max(db.bank_money_supply(code), _minor())
    goods_target = goods_per_day / supply * (1 + weight * math.log1p(energy_per_day))
    activity_target = weight * math.log1p(score)
    return max(goods_target, activity_target, ECONOMY["fx_min_value"])

def yesterday_activity(code, now=None):
    """(people, messages) of a currency on the UTC day before today — what the
    daily FX post shows beside the new value."""
    day = _today(now).toordinal() - 1
    since = datetime.combine(date.fromordinal(day), datetime.min.time(),
                             timezone.utc).timestamp()
    people, messages, _activity = activity_by_day(code, since).get(
        date.fromordinal(day).isoformat(), (0, 0, 0.0))
    return people, messages

def reprice_all():
    """Set every currency to its value now, leaving the day-over-day record
    of the economic log alone. main.py: main calls it when the bot starts,
    so a value left behind by an older formula does not outlive the restart
    that brings a new one."""
    for bank in db.get_all_banks():
        db.set_bank_value(bank["code"], recompute_value(bank))
