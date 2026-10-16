"""The platform-neutral half of the celebrations: when a day begins, whose
birthday it is, which greeting to use, and how a wiki birthday is worded.

Every community keeps one UTC offset (db/community.py), and both greetings go
out once a day at GREETING_HOUR local time; "once" is the stored local date
the greeting last went out on, compared rather than trusted to a timer, so a
bot that was down at ten still greets when it comes back that day. A birthday
on 29 February is celebrated on the 28th in a common year, for people and
wikis alike.

**The birthday role covers the whole day wherever it is that day.** A person's
day is the UTC calendar day and the community's local calendar day of their
birthday at once, and the role is held from whichever of the two begins first
until whichever ends last (`in_birthday_window`). The two intervals always
overlap, since no offset reaches a day, so that is one interval.

**A greeting is chosen to avoid repeats.** There are BIRTHDAY_GREETINGS
numbered greetings (`birthday_greeting_<n>` in the six i18n files). The last
five used in a channel are never chosen there; among what is left, the last
five a person received are avoided when anything else remains. With twelve
greetings both rules can always hold.

**A wiki birthday is one message per community and day.** The first line is
in the union's language and names every wiki whose anniversary it is,
"turned" for those whose founding moment has already come round today and
"turns" for those still ahead; a wiki in another language the greeting can be
written in adds that language's flag to the line and a line of its own in
that language under it. The main page links follow, one per wiki. Those
languages are the six the bot speaks plus every wiki language with a file of
its own in `i18n/wiki_greetings/` (Belarusian, `be.json`): the greeting
phrases only, `{key: text}`, read once at import through this file's own
directory — so this module must stay directly in `src/` — and editable in the
control panel as a raw file. Such a language is not one the bot answers in.

**The calendar** (`calendar_events`, `calendar_lines`) lists what comes
round in the coming month, a page at a time: the founding days of
unions (✨) and of wikis (📚), each with the flag of its language, and the
birthdays of people (👤) by the name they go by. Its controls — which scope, which kinds, which page —
travel in the button itself on Telegram (`calendar_state`), so a calendar
there keeps working across a restart; Discord keeps them in an ordinary
view that lasts as long as a dialog.

Not this module's zone: storage (db/birthdays.py, db/community.py,
db/wikis.py) and delivery (discord_bot/celebrations.py,
telegram_bot/celebrations.py).
"""
import calendar
import json
import os
import random
import re
from datetime import date, datetime, timedelta, timezone

import db
from utils import (
    LANG_ORDER, format_date, language_flag, language_name, localized,
    plural_category, utc_from_epoch,
)

_WIKI_GREETINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n",
                                   "wiki_greetings")

def _load_wiki_greetings():
    """{language: {key: text}} from i18n/wiki_greetings/<language>.json —
    the greeting phrases of wiki languages the bot does not otherwise
    speak."""
    texts = {}
    if os.path.isdir(_WIKI_GREETINGS_DIR):
        for name in sorted(os.listdir(_WIKI_GREETINGS_DIR)):
            if name.endswith(".json"):
                with open(os.path.join(_WIKI_GREETINGS_DIR, name), encoding="utf-8") as f:
                    texts[name[:-5]] = json.load(f)
    return texts

_WIKI_GREETINGS = _load_wiki_greetings()
GREETING_LANGS = list(LANG_ORDER) + [code for code in _WIKI_GREETINGS if code not in LANG_ORDER]

def _greeting_text(key, lang, **values):
    """A wiki greeting phrase in `lang`: from its own file for a wiki
    language beyond the six, from the bot's localization otherwise."""
    template = _WIKI_GREETINGS.get(lang, {}).get(key)
    if template is None:
        return localized(key, lang, **values)
    try:
        return template.format(**values)
    except Exception:
        return template

GREETING_HOUR = 10
BIRTHDAY_GREETINGS = 12
NO_REPEAT = 5

_DAY_MONTH_RE = re.compile(r"^\s*(\d{1,2})[./](\d{1,2})\s*$")
_DATE_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$|^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")

def parse_day_month(text):
    """'DD.MM' (or 'DD/MM') as (day, month); 29.02 is allowed. Raises
    ValueError on anything else."""
    m = _DAY_MONTH_RE.match(text or "")
    if not m:
        raise ValueError("invalid_day_month")
    return check_day_month(int(m.group(1)), int(m.group(2)))

def check_day_month(day, month):
    """Validate a day and a month against a leap year, so 29 February
    passes. Returns (day, month) or raises ValueError."""
    try:
        date(2000, int(month), int(day))
    except (TypeError, ValueError):
        raise ValueError("invalid_day_month")
    return int(day), int(month)

def parse_date(text):
    """'DD.MM.YYYY' or 'YYYY-MM-DD' as a UTC midnight timestamp. Raises
    ValueError on anything else."""
    m = _DATE_RE.match(text or "")
    if not m:
        raise ValueError("invalid_date")
    if m.group(1):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        year, month, day = int(m.group(4)), int(m.group(5)), int(m.group(6))
    try:
        return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())
    except ValueError:
        raise ValueError("invalid_date")

def occurrence(day, month, year):
    """The date a day-and-month falls on in `year`: 29 February becomes the
    28th in a common year."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)

def local_now(tz_offset, now=None):
    """The community's wall clock as a naive datetime."""
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(minutes=int(tz_offset or 0))).replace(tzinfo=None)

def greeting_due(row, marker_field, now=None):
    """The local date (ISO) a community's greeting is due for, or None: due
    once GREETING_HOUR has passed locally and the stored marker is not today."""
    local = local_now(row["tz_offset"], now)
    if local.hour < GREETING_HOUR:
        return None
    today = local.date().isoformat()
    return None if row[marker_field] == today else today

def in_birthday_window(day, month, tz_offset, now=None):
    """Whether `now` lies in the birthday's UTC day or its local day, taken
    together as one interval."""
    now = now or datetime.now(timezone.utc)
    shift = timedelta(minutes=int(tz_offset or 0))
    for year in (now.year - 1, now.year, now.year + 1):
        d = occurrence(day, month, year)
        utc_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        local_start = utc_start - shift
        start = min(utc_start, local_start)
        end = max(utc_start, local_start) + timedelta(days=1)
        if start <= now < end:
            return True
    return False

def window_dates(now=None):
    """The (month, day) pairs whose birthday window can contain `now`: the
    UTC day before, of and after it, plus 29 February when a 28th is among
    them in a common year."""
    now = now or datetime.now(timezone.utc)
    pairs = []
    for delta in (-1, 0, 1):
        d = (now + timedelta(days=delta)).date()
        pairs.append((d.month, d.day))
        if (d.month, d.day) == (2, 28) and not calendar.isleap(d.year):
            pairs.append((2, 29))
    return list(dict.fromkeys(pairs))

def birthday_pairs_on(local_date):
    """The (month, day) pairs celebrated on a local date — 29 February is
    added to a common year's 28th."""
    pairs = [(local_date.month, local_date.day)]
    if (local_date.month, local_date.day) == (2, 28) and not calendar.isleap(local_date.year):
        pairs.append((2, 29))
    return pairs

def birthday_people(dates):
    """Everyone with a birthday on one of `dates`, as a list of their
    identity sets: each item is a dict {platform: user_id} holding the
    account the birthday was set from and its linked twin, with the day,
    the month and the name stored with the birthday."""
    people = []
    for row in db.birthdays_on(dates):
        ids = dict(db.user_identities(row["platform"], row["user_id"]))
        people.append({"day": row["day"], "month": row["month"], "ids": ids,
                       "name": row["display_name"]})
    return people

def pick_greeting(chat_key, user_key):
    """The number of the greeting to use: never one of the last five in the
    channel, and not one of the last five the person got when anything else
    is left."""
    pool = [n for n in range(1, BIRTHDAY_GREETINGS + 1)
            if n not in db.recent_greetings("chat", chat_key, NO_REPEAT)]
    if not pool:
        pool = list(range(1, BIRTHDAY_GREETINGS + 1))
    fresh = [n for n in pool if n not in db.recent_greetings("user", user_key, NO_REPEAT)]
    return random.choice(fresh or pool)

def greeting_text(number, lang, user):
    """The text of greeting `number` addressed to `user` (a mention or a
    name already formatted for the platform)."""
    return localized(f"birthday_greeting_{int(number)}", lang, user=user)

def wiki_base_lang(row):
    """A wiki's content language without its variant ('pt-br' → 'pt',
    'be-tarask' → 'be'), or '' when unknown."""
    return (row["lang"] or "").split("-")[0].lower()

def wiki_lang(row):
    """The language a wiki can be greeted in: its content language reduced
    to one of GREETING_LANGS, or None when there are no greeting phrases in
    it."""
    code = wiki_base_lang(row)
    return code if code in GREETING_LANGS else None

def wiki_date(row):
    """A wiki's effective founding date for people (DD.MM.YYYY), or a dash
    when it is not known."""
    ts = db.wiki_founded(row)
    return utc_from_epoch(ts).strftime("%d.%m.%Y") if ts is not None else "—"

def wiki_summary(row, lang):
    """What the wiki commands report about a wiki: name, main page, founding
    date, language and the size of its administration."""
    code = wiki_base_lang(row)
    return localized("wiki_summary", lang, name=row["name"], url=row["main_url"] or row["base_url"],
                     date=wiki_date(row),
                     language=language_name(code) if code else "?",
                     staff=len(db.get_wiki_staff(row["id"])))

def wiki_anniversaries(wiki_rows, local_date, now=None):
    """The wikis among `wiki_rows` whose anniversary is `local_date`, as
    dicts with row, years and past. A wiki in its first year is skipped."""
    now = now or datetime.now(timezone.utc)
    out = []
    for row in wiki_rows:
        ts = db.wiki_founded(row)
        if ts is None:
            continue
        founded = utc_from_epoch(ts)
        if occurrence(founded.day, founded.month, local_date.year) != local_date:
            continue
        years = local_date.year - founded.year
        if years <= 0:
            continue
        d = occurrence(founded.day, founded.month, local_date.year)
        moment = datetime(d.year, d.month, d.day, founded.hour, founded.minute,
                          founded.second, tzinfo=timezone.utc)
        out.append({"row": row, "years": years, "past": moment <= now})
    return out

def _join(items, lang):
    """'a', 'a and b', 'a, b and c' in a language."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + _greeting_text("wiki_bday_and", lang) + items[-1]

def _wiki_line(entries, lang):
    """One greeting line naming `entries` in `lang`."""
    def item(entry):
        tense = "past" if entry["past"] else "future"
        return _greeting_text(f"wiki_bday_{tense}_{plural_category(lang, entry['years'])}", lang,
                              name=entry["row"]["name"], n=entry["years"])
    past = [item(e) for e in entries if e["past"]]
    future = [item(e) for e in entries if not e["past"]]
    if past and future:
        body = _greeting_text("wiki_bday_contrast", lang, past=_join(past, lang),
                              future=_join(future, lang))
    else:
        body = _join(past or future, lang)
    return _greeting_text("wiki_bday_line", lang, items=body)

def wiki_greeting(entries, union_language):
    """The whole wiki birthday message for one community."""
    entries = sorted(entries, key=lambda e: (not e["past"], (e["row"]["name"] or "").lower()))
    foreign = {}
    for entry in entries:
        code = wiki_lang(entry["row"])
        if code and code != union_language:
            foreign.setdefault(code, []).append(entry)
    first = _wiki_line(entries, union_language)
    flags = "".join(language_flag(code) for code in GREETING_LANGS if code in foreign)
    lines = [f"{first} {flags}" if flags else first]
    for code in GREETING_LANGS:
        if code in foreign:
            lines.append(f"{language_flag(code)} {_wiki_line(foreign[code], code)}")
    lines.extend(e["row"]["main_url"] for e in entries if e["row"]["main_url"])
    return "\n".join(lines)

CALENDAR_KINDS = ("union", "wiki", "person")
CALENDAR_EMOJI = {"union": "✨", "wiki": "📚", "person": "👤"}
CALENDAR_CHARS = 3800

def add_months(day, months):
    """`day` moved by whole calendar months, the day of the month kept — or
    the month's last day when it has fewer (31 January + 1 = 28 or 29
    February)."""
    index = day.year * 12 + day.month - 1 + int(months)
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))

def calendar_window(page, today=None):
    """The [start, end) dates of calendar page `page`: one calendar month
    from today's day of the month — 13 October to 12 November, then 13
    November to 12 December — page 0 starting today, negative pages going
    back. Each page starts where the one before it ends, so no day is on two
    pages."""
    today = today or datetime.now(timezone.utc).date()
    return add_months(today, page), add_months(today, int(page) + 1)

def window_pairs(start, end):
    """The (month, day) pairs of every date in [start, end), 29 February
    added to a common year's 28th."""
    pairs, day = [], start
    while day < end:
        pairs.extend(birthday_pairs_on(day))
        day += timedelta(days=1)
    return list(dict.fromkeys(pairs))

def _flag(code):
    """The flag a language is marked with (utils.language_flag)."""
    return language_flag(code)

def calendar_events(union_code, kinds, people):
    """What a calendar can show, as (month, day, kind, label) tuples: the
    founding days of the unions — the one named, or every union when
    `union_code` is None — and of their wikis, plus `people`, given by the
    caller as (day, month, name) because who is a member is a platform's
    question."""
    events = []
    if "union" in kinds:
        codes = [union_code] if union_code else db.get_union_codes()
        for code in codes:
            row = db.get_union(code)
            if row and row["created_at"]:
                d = utc_from_epoch(row["created_at"])
                events.append((d.month, d.day, "union",
                               f"{_flag(db.union_lang(code))} {db.get_union_name(code, db.union_lang(code))}"))
    if "wiki" in kinds:
        rows = db.get_union_wikis(union_code) if union_code else db.all_wikis()
        for row in rows:
            ts = db.wiki_founded(row)
            if ts is not None:
                d = utc_from_epoch(ts)
                events.append((d.month, d.day, "wiki", f"{_flag(wiki_base_lang(row))} {row['name']}"))
    if "person" in kinds:
        events.extend((month, day, "person", name) for day, month, name in people)
    return events

def calendar_lines(events, page, lang, today=None):
    """(title, lines) of one calendar page: every event whose next
    occurrence falls inside the page's window, by date, as
    '1 января — 📚🇷🇺 A Вики', cut to what one message holds."""
    start, end = calendar_window(page, today)
    dated = []
    for month, day, kind, label in events:
        for year in (start.year, start.year + 1):
            when = occurrence(day, month, year)
            if start <= when < end:
                dated.append((when, kind, label))
    dated.sort(key=lambda item: (item[0], CALENDAR_KINDS.index(item[1]), item[2].lower()))
    lines, size = [], 0
    for when, kind, label in dated:
        gap = " " if kind == "person" else ""
        line = f"{format_date(when, lang, with_year=False)} — {CALENDAR_EMOJI[kind]}{gap}{label}"
        if size + len(line) + 1 > CALENDAR_CHARS:
            lines.append(localized("calendar_more", lang, count=len(dated) - len(lines)))
            break
        lines.append(line)
        size += len(line) + 1
    title = localized("calendar_title", lang,
                      start=format_date(start, lang, with_year=False),
                      end=format_date(end - timedelta(days=1), lang, with_year=False))
    return title, lines or [localized("calendar_empty", lang)]

def calendar_state(text):
    """A calendar's controls packed into a short string for a button:
    'l' or 'g' (this community or everything), the shown kinds as a bit
    mask, and the page — 'l7:0'. Returns (scope, kinds, page)."""
    try:
        head, page = (text or "l7:0").split(":")
        mask = int(head[1:] or 7)
        scope = "g" if head[:1] == "g" else "l"
        return scope, {k for i, k in enumerate(CALENDAR_KINDS) if mask & (1 << i)}, int(page)
    except ValueError:
        return "l", set(CALENDAR_KINDS), 0

def pack_calendar_state(scope, kinds, page):
    """The inverse of `calendar_state`."""
    mask = sum(1 << i for i, k in enumerate(CALENDAR_KINDS) if k in kinds)
    return f"{scope}{mask}:{int(page)}"
