"""The economy's base layer: units, the formulas every other module reads,
and the reader for the tuning knobs in config.ECONOMY.

Money is an integer number of MINOR units (hundredths) everywhere in the
package and in db/. `parse_amount` and `format_amount` are the only two places
the decimal point exists, and `parse_salary` is where the second shape appears:
a percentage is carried as the number the user typed (5 meaning five percent,
never 0.05), because that is also how db/enterprises.py stores it.

The three mastery formulas are the whole progression curve. Producing more of
one good raises its quality level (log2 of the count), raises what a unit is
worth (square root of the count) and raises what the next unit costs to make
(linear in the level) — the last one is what keeps the first two finite.

`_cfg` and `_cat_cfg` live here rather than beside their callers because every
module reads knobs and none of them owns the table: `_cfg` supplies a code-side
default so a config written before a later update keeps working, and `_cat_cfg`
resolves a per-category knob through its 'default' entry.

Not this module's zone: anything that touches the database. This module is
pure arithmetic on numbers and rows handed to it.
"""
import math
import re

from config import ECONOMY

CURRENCY_CODE_RE = re.compile(r"^[A-Za-z0-9]{4}$")
ENERGY_EMOJI = "⚡"

def _minor():
    """Minor units per whole unit — 100 in every sane configuration.

    Read from config rather than hard-coded so that a deployment could in
    principle run a currency without subunits, and used by the two formatters and
    by the FX recompute's supply floor."""
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
    """Energy with its emoji ('42 ⚡'). Energy is a plain integer count, never
    minor units, so there is nothing to scale here."""
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

def _cfg(key, default):
    """ECONOMY knob with a code-side default, so configs written before the
    production/provision update keep working unchanged."""
    return ECONOMY.get(key, default)

def _cat_cfg(dict_key, category, default):
    """One entry of a per-category knob table, falling back to its 'default'
    entry and then to `default`.

    Category tables are how food is made quick and bulky while pharmacy is slow
    and scarce. A knob that is not a dict at all — an older config that set a
    single number — is ignored rather than crashed on."""
    table = _cfg(dict_key, {})
    if not isinstance(table, dict):
        return default
    return table.get(category or "default", table.get("default", default))
