"""Taxes: what a community and a bank take from money changing hands.

Two taxes exist, each a percentage stored the way it was typed (5 meaning
five percent), from 0 to MAX_TAX_PERCENT:

* **The community tax** (`community_settings.tax_percent`, set with
  /server edit) is taken from payments and sales made in that server or
  group and from currency earned by messages there, and is **burned**: it
  leaves circulation. A community's admins can therefore lower what their
  members earn, never raise it — the earning multiplier above 1× belongs
  to the currency's leaders and its central server's admins (/bank earn).
* **The bank tax** (`banks.tax_percent`, set from /bank edit) is taken from
  payments and sales in that currency wherever they happen, and is **paid to
  the bank's leaders** evenly, the remainder to the first of them — the
  bank has no account of its own (db/banks.py: _bank_owner) — or burned when
  it has no leaders left, exactly like the conversion fee.

Both are withheld from the side that receives the money, after it arrived:
the payer always parts with exactly the amount named, the journal shows the
gross payment and then each tax as its own row (`tax_community`,
`tax_bank`), and an amount too small to yield a whole minor unit is not
taxed. Where a payment "is made" is the community the command was run in;
an export is taxed where the seller is, when the escrow reaches it.
Wages, salaries, party dues, fines, emissions and conversions are not
taxed.

Not this module's zone: setting the rates (the /server edit and /bank edit
dialogs) and the money primitives (db/banks.py).
"""
import db

MAX_TAX_PERCENT = 50.0

def parse_percent(text):
    """A tax rate typed as '5', '2.5' or '5%' — a float from 0 to
    MAX_TAX_PERCENT — or None."""
    raw = (text or "").strip().rstrip("%").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 <= value <= MAX_TAX_PERCENT else None

def format_percent(value):
    """A rate for people: '5%', '2.5%'."""
    return f"{float(value or 0):g}%"

def portion(amount, percent):
    """The whole minor units `percent` of `amount` comes to, rounded down."""
    return int(int(amount) * float(percent or 0) / 100) if amount > 0 else 0

def community_rate(server):
    """The community tax of `server` — a (platform, server_id) pair, or None
    for a private chat — as a percentage."""
    if not server or server[1] is None or not db.get_chat(server[1]):
        return 0.0
    return db.community_tax(server[0], server[1])

def bank_rate(bank_code):
    """The bank tax of a currency, as a percentage."""
    bank = db.get_bank(bank_code)
    return float(bank["tax_percent"] or 0) if bank else 0.0

def _pay_leaders(bank_code, payer, amount):
    """Hand a bank tax from the payer to the bank's leaders, or burn it when
    the bank has none."""
    leaders = db.get_bank_leaders(bank_code)
    if not leaders:
        db.burn(bank_code, payer, amount, "tax_bank")
        return
    share, extra = divmod(amount, len(leaders))
    for index, leader in enumerate(leaders):
        part = share + (1 if index < extra else 0)
        if part > 0:
            db.transfer(bank_code, payer, db.canonical_user(leader["platform"], leader["user_id"]),
                        part, "tax_bank", allow_negative=True,
                        to_display=leader["display_name"])

def withhold(bank_code, payee, gross, server=None):
    """Take the community tax of `server` and the bank tax from `payee`, who
    has just received `gross` in `bank_code`. Returns {'community', 'bank',
    'net'} in minor units."""
    community = portion(gross, community_rate(server))
    bank = portion(gross, bank_rate(bank_code))
    if community + bank > gross:
        bank = max(0, gross - community)
    if community:
        db.burn(bank_code, payee, community, "tax_community")
    if bank:
        _pay_leaders(bank_code, payee, bank)
    return {"community": community, "bank": bank, "net": gross - community - bank}

def earning_after_tax(amount, server):
    """What a message's reward comes to after the community tax; the
    reward is minted net, so the tax never exists as money."""
    return amount - portion(amount, community_rate(server))

def taxes_line(info, bank, lang):
    """A line telling who received a payment what the taxes took, or '' when
    nothing was taken."""
    from economy.names import format_money
    from utils import localized
    if not info or not (info["community"] or info["bank"]):
        return ""
    parts = []
    if info["community"]:
        parts.append(localized("tax_part_community", lang,
                               amount=format_money(info["community"], bank)))
    if info["bank"]:
        parts.append(localized("tax_part_bank", lang, amount=format_money(info["bank"], bank)))
    return localized("tax_withheld", lang, taxes=", ".join(parts),
                     net=format_money(info["net"], bank))
