"""How a currency is named in text: its declined forms and amounts in words.

A bank's leaders may give the forms of its currency's name in any of the six
languages (`/bank edit`, stored in db `currency_forms`). FORMS lists what is
asked per language, in the order the dialog asks for it: the six cases of the
singular and the plural forms that numbers need in Russian, Ukrainian and
Polish, and a singular and a plural in English, Spanish and Portuguese.

**Amounts.** `format_money` writes '12.34 🪙 GRIV' as it always did, but the
value it returns is a `Money`: a string that utils.localized rewrites in
the reader's language as '12.34 🪙 гривны' when that language has the form
the number needs (`amount_form`) — 1 гривна, 2 гривны, 5 гривен, 1.50 гривны;
1 hryvnia, 2 hryvnias. A language without the form keeps the code, so a
currency nobody declined reads exactly as before. An amount glued into a
line before it reaches `localized` stays in the code form.

**Cases in texts.** `currency_name(bank)` is the name as typed, as a string
that also knows its forms; a text may ask for one by suffix — `{name_loc}`,
`{name_ins}` — and utils.localized looks it up, falling back to the name as
typed when that form was not given.

Not this module's zone: storage (db/banks.py: get_currency_forms) and the
dialog that fills it in (the two commands/banks.py).
"""
import db
from economy.money import _minor, format_amount

FORMS = {
    "ru": ("nom", "gen", "dat", "acc", "ins", "loc", "gen_pl"),
    "uk": ("nom", "gen", "dat", "acc", "ins", "loc", "nom_pl", "gen_pl"),
    "pl": ("nom", "gen", "dat", "acc", "ins", "loc", "nom_pl", "gen_pl"),
    "en": ("sg", "pl"),
    "es": ("sg", "pl"),
    "pt": ("sg", "pl"),
}
FORM_SUFFIXES = tuple(sorted({f for forms in FORMS.values() for f in forms},
                             key=len, reverse=True))

def amount_form(lang, minor):
    """The form of the name an amount of `minor` units takes in `lang`.

    A whole amount follows the number's plural rule; one with a fraction
    takes the genitive singular in the Slavic languages (2.50 гривны,
    2,50 złotego) and the plural in English and Spanish. Portuguese uses the
    singular for anything below two (1.50 real)."""
    units = _minor()
    minor = abs(int(minor))
    whole, fraction = divmod(minor, units)
    if lang in ("en", "es"):
        return "sg" if whole == 1 and not fraction else "pl"
    if lang == "pt":
        return "sg" if whole <= 1 else "pl"
    if fraction:
        return "gen"
    last, last_two = whole % 10, whole % 100
    one = last == 1 and last_two != 11
    few = 2 <= last <= 4 and not 12 <= last_two <= 14
    if lang == "ru":
        return "nom" if one else ("gen" if few else "gen_pl")
    if lang == "uk":
        return "nom" if one else ("nom_pl" if few else "gen_pl")
    if lang == "pl":
        return "nom" if whole == 1 else ("nom_pl" if few else "gen_pl")
    return None

class Money(str):
    """An amount as '12.34 🪙 GRIV' that utils.localized rewrites in words
    for its reader (`for_lang`)."""

    def __new__(cls, minor, bank):
        """The amount in the code form, remembering what it is."""
        obj = str.__new__(cls, _with_code(minor, bank))
        obj.minor, obj.bank = int(minor), bank
        return obj

    def for_lang(self, lang):
        """The amount written for a reader of `lang`."""
        return format_money(self.minor, self.bank, lang)

class CurrencyName(str):
    """A currency's name as typed, which also answers its declined forms
    (`form`) — what utils.localized reads for `{name_loc}`."""

    def __new__(cls, bank):
        """The name, remembering whose it is."""
        obj = str.__new__(cls, bank["currency_name"])
        obj.code = bank["code"]
        return obj

    def form(self, lang, form):
        """The name in one form of `lang`, or as typed when not given."""
        return db.get_currency_forms(self.code, lang).get(form) or str(self)

def currency_name(bank):
    """The name of a bank's currency for a text (see CurrencyName)."""
    return CurrencyName(bank)

def _with_code(minor, bank):
    """'12.34 🪙 CODE'."""
    emoji = (bank["emoji"] + " ") if bank["emoji"] else ""
    return f"{format_amount(minor)} {emoji}{bank['code']}"

def format_money(minor, bank, lang=None):
    """An amount in a bank's currency. Without `lang` a Money, written
    '12.34 🪙 CODE' until localized meets it; with one, the text itself —
    with the name in the form the number needs when the bank gave it in
    that language, with the code otherwise."""
    if lang is None:
        return Money(minor, bank)
    form = amount_form(lang, minor)
    word = db.get_currency_forms(bank["code"], lang).get(form) if form else None
    if not word:
        return _with_code(minor, bank)
    emoji = (bank["emoji"] + " ") if bank["emoji"] else ""
    return f"{format_amount(minor)} {emoji}{word}"

def parse_currency_forms(lang, text):
    """The forms typed for `lang` as {form: word}, in FORMS order and
    separated by commas, a '-' standing for a form not given; {} when the
    whole answer is '-' (forget this language); None when the number of
    forms is wrong."""
    text = (text or "").strip()
    if text in ("-", "—", "off"):
        return {}
    pieces = [p.strip() for p in text.split(",")]
    if len(pieces) != len(FORMS[lang]):
        return None
    forms = {}
    for form, word in zip(FORMS[lang], pieces):
        word = " ".join(word.split())[:40]
        if word and word not in ("-", "—"):
            forms[form] = word
    return forms or None

def forms_sample(bank, lang):
    """A few amounts written with the new forms — what the confirmation
    shows: 1, 2, 5 and 1.50 of the currency."""
    units = _minor()
    return ", ".join(format_money(n, bank, lang)
                     for n in (units, 2 * units, 5 * units, units + units // 2))
