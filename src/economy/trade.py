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

def asking_price(owner, good, qty):
    """What `qty` units are worth at the seller's own quality: the price
    `/sell` offers when nobody named one.

    The same arithmetic the daily meal pays and the shipment escrow is measured
    against, so a good has one value across the economy however it changes
    hands."""
    return unit_value(good, db.get_produced(owner, good["code"])) * max(int(qty), 0)

def sell(seller, buyer, good, qty, price, bank_code):
    """Sell up to `qty` units of `good` from `seller` to `buyer` for `price`.

    Goods move between two holders and money between two accounts of the same
    currency: nothing is minted here. That is the point — a bank buys nothing,
    so producing goods is not a way to print money, and the currency enters the
    economy through messages and the daily meal instead.

    The buyer may be a person or an enterprise, and so may the seller. When an
    enterprise sells, the proceeds count into its period sales — the base for
    percent-of-sales salaries. Base-category goods carry no market value and are
    given away rather than sold (`/give-good`). Returns (status, info)."""
    if db.is_base_good(good["code"]):
        return "not_sellable", {}
    if seller == buyer:
        return "self", {}
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
    if price > 0 and seller[0] == "enterprise":
        ent = db.get_enterprise(seller[2])
        if ent and ent["salary_bank"] == bank_code:
            db.add_enterprise_sales(seller[2], price)
    return "ok", {"qty": qty, "total": price,
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
    dialog and the `/bank` card: the default first, then each currency that
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

def convert(owner, from_code, to_code, amount, fee_reason="convert_fee"):
    """Convert between the owner's own accounts at the current rate, taking the
    spread the source bank charges. Returns (status, info).

    The fee is charged in the source currency and paid to that bank's leaders,
    so only the principal actually crosses into the other currency: the source
    supply falls by what left it and by nothing more. Before, the fee was
    destroyed — the only real money sink the economy had, and one that fired
    just often enough to be invisible."""
    if from_code == to_code:
        return "same", {}
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
