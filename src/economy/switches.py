"""The parts of the economy a community or a currency can switch off.

A community's appointed administrators may switch off any of four parts on
its own ground (/server edit); the leaders of a bank may switch off three of
them for their currency everywhere (/bank edit):

* `enterprises` (a community only): the enterprises founded there are
  frozen. Nothing is founded or joined there, and its enterprises produce
  nothing, pay no salaries, are not drawn by the daily meal and neither sell
  nor ship; their warehouses, members and settings stay as they are, and
  switching the part back on resumes everything. A neutral enterprise's
  virtual supply is no warehouse and keeps feeding the minimum.
* `trade`: trade between servers. A sale or a shipment crosses servers when
  the places it touches — the community it is made in and the home of every
  enterprise taking part — are more than one. A community refuses every such
  deal that touches it; a currency refuses those priced in it.
* `convert`: currency exchange. A community refuses /bank convert run in it;
  a currency refuses exchange from it and into it anywhere.
* `goods`: own goods — every good but the five base goods. A community
  refuses creating, producing and selling them there, its enterprises' lines
  and warehouses included, and its daily meal stops buying them; a currency
  does the same for the goods denominated in it. Base goods are untouched:
  they are what keeps a server fed.

A check answers None or a refusal, `(key, values)` for utils.localized and
`refusal_text`: the part is a `Part`, which names itself in the reader's
language, and the place is "this community" when it is the one the person
is in (`here`), otherwise the community's stored name (its id when it
has none). The commands check before
they offer anything; economy/trade.py: sell and convert and
economy/logistics.py: dispatch_shipment check again and answer the status
"off" with the refusal in `info["refusal"]`, because an offer may be accepted
after a part was switched off and a weekly export contract asks nobody. The
daily loops skip what is frozen without a word.

Not this module's zone: the stored switches (the `econ_off` columns of
db/community.py and db/banks.py) and the menus that set them.
"""
import db

PARTS = ("enterprises", "trade", "convert", "goods")
BANK_PARTS = ("trade", "convert", "goods")

class Part(str):
    """A part's code that writes itself as the part's name in the reader's
    language (utils._TextValues asks every value for `for_lang`)."""

    def for_lang(self, lang):
        """The part's name in `lang`."""
        from utils import localized
        return localized(f"econ_part_{self}", lang)

def _parse(text, allowed=PARTS):
    """The parts named in a stored `econ_off` value."""
    return frozenset(p for p in (text or "").split(",") if p in allowed)

def _dump(parts):
    """The stored form of a set of parts: their codes in menu order, or None
    when nothing is off."""
    return ",".join(p for p in PARTS if p in parts) or None

def _place(place):
    """A (platform, server id) pair, or None for a private chat or nowhere."""
    if not place or not place[0] or place[1] is None:
        return None
    return place[0], str(place[1])

def home(ent):
    """The community an enterprise was founded in."""
    return ent["platform"], str(ent["server_id"])

def community_off(platform, server_id):
    """The parts a community switched off."""
    if not platform or server_id is None:
        return frozenset()
    row = db.get_community(platform, server_id)
    return _parse(row["econ_off"]) if row else frozenset()

def bank_off(bank_code):
    """The parts the leaders of a bank switched off for its currency."""
    bank = db.get_bank(bank_code) if bank_code else None
    return _parse(bank["econ_off"], BANK_PARTS) if bank else frozenset()

def set_community_part(platform, server_id, part, off):
    """Switch one part off (`off=True`) or back on in a community. Returns
    the parts now off."""
    parts = set(community_off(platform, server_id))
    (parts.add if off else parts.discard)(part)
    db.set_community_fields(platform, server_id, econ_off=_dump(parts))
    return frozenset(parts)

def set_bank_part(bank_code, part, off):
    """Switch one part off or back on for a currency. Returns the parts now
    off."""
    parts = set(bank_off(bank_code))
    (parts.add if off else parts.discard)(part)
    db.update_bank_field(bank_code, "econ_off", _dump(parts))
    return frozenset(parts)

def refusal(place, part, here=None):
    """The refusal of `part` in a community, or None when it is on there."""
    place = _place(place)
    if place is None or part not in community_off(*place):
        return None
    if place == _place(here):
        return "econ_off_here", {"part": Part(part)}
    return "econ_off_community", {"part": Part(part),
                                   "name": db.chat_title(place[1]) or place[1]}

def currency_refusal(bank_code, part):
    """The refusal of `part` in a currency, or None when it is on there."""
    if not bank_code or part not in bank_off(bank_code):
        return None
    bank = db.get_bank(bank_code)
    return "econ_off_bank", {"part": Part(part),
                             "bank": f"{bank['currency_name']} [{bank['code']}]"}

def enterprise_refusal(ent, here=None):
    """Why an enterprise is frozen, or None when it works."""
    return refusal(home(ent), "enterprises", here)

def goods_refusal(bank_code, places, here=None):
    """Why an own good denominated in `bank_code` may not be created,
    produced or sold at `places`, or None."""
    for place in places:
        found = refusal(place, "goods", here)
        if found:
            return found
    return currency_refusal(bank_code, "goods")

def good_refusal(good, places, here=None):
    """`goods_refusal` for an existing good; base goods are never refused."""
    if good is None or db.is_base_good(good["code"]):
        return None
    return goods_refusal(good["bank_code"], places, here)

def _owner_places(owner, server, here):
    """(refusal, places) of work done for `owner` on `server`: an enterprise
    owner adds its home to the places, or refuses when it is frozen."""
    places = [server]
    if owner[0] == "enterprise":
        ent = db.get_enterprise(owner[2])
        if ent:
            found = enterprise_refusal(ent, here)
            if found:
                return found, places
            places.append(home(ent))
    return None, places

def production_refusal(good, owner, server, here=None):
    """Why `good` may not be produced for `owner` (a person or an
    enterprise) on `server`, or None."""
    found, places = _owner_places(owner, server, here)
    return found or good_refusal(good, places, here)

def new_good_refusal(bank_code, owner, here):
    """Why an own good denominated in `bank_code` may not be created for
    `owner` in the community `here`, or None."""
    found, places = _owner_places(owner, here, here)
    return found or goods_refusal(bank_code, places, here)

def deal_refusal(parties, good, bank_code, price, server=None, here=None):
    """Why a sale or a shipment of `good` between `parties` (owner tuples)
    may not happen, or None.

    The places of a deal are the community it is made in (`server`, None for
    a shipment or a private chat) and the home of every enterprise taking
    part; more than one place makes it trade between servers."""
    places = []
    if _place(server):
        places.append(_place(server))
    for owner in parties:
        if owner[0] != "enterprise":
            continue
        ent = db.get_enterprise(owner[2])
        if not ent:
            continue
        found = enterprise_refusal(ent, here)
        if found:
            return found
        if home(ent) not in places:
            places.append(home(ent))
    found = good_refusal(good, places, here)
    if found:
        return found
    if len(places) > 1:
        for place in places:
            found = refusal(place, "trade", here)
            if found:
                return found
        if price and price > 0:
            return currency_refusal(bank_code, "trade")
    return None

def convert_refusal(from_code, to_code, place=None):
    """Why money may not be exchanged between two currencies in `place`, or
    None."""
    return (refusal(place, "convert", place)
            or currency_refusal(from_code, "convert")
            or currency_refusal(to_code, "convert"))

def refusal_text(found, lang):
    """A refusal written for a reader of `lang`."""
    from utils import localized
    key, values = found
    return localized(key, lang, **values)

def part_line(part, off, lang):
    """One line of a switches menu: the part's state and its name."""
    name = Part(part).for_lang(lang)
    return f"{'⛔' if part in off else '✅'} {name[:1].upper()}{name[1:]}"

def switched_text(part, off, lang, bank=None):
    """What a menu answers after switching `part` off (`off=True`) or back
    on, for a community or, given `bank` ('Name [CODE]'), for a currency."""
    from utils import localized
    if not off:
        return localized("econ_set_on", lang, part=Part(part))
    if bank:
        return localized(f"econ_bank_set_off_{part}", lang, bank=bank)
    return localized(f"econ_set_off_{part}", lang)

def parts_text(parts, lang):
    """The names of some parts in `lang`, comma-separated, or None."""
    if not parts:
        return None
    return ", ".join(Part(p).for_lang(lang) for p in PARTS if p in parts)
