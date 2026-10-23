"""The shared runtime both halves of the bot lean on: localization, role
checks, the parsers the commands take their arguments through, the rate
limiter, and the service-event feed.

The localization is the largest part and the one with a trap in it. The six
`i18n/*.json` files are read once at import through `_load_i18n`, and the path
is built from *this file's* directory — so utils.py must stay directly in
`src/`. Moved one level deeper it still imports, still runs, and answers every
`localized()` call with the bare key: no error, no log line, just a bot that
has forgotten how to speak. `localized_help` and `localized_service_event` are
separate entry points because their keys live under their own prefixes.

A command is written in the texts the way Discord spells it, `/bank pay`, and
`localized` / `localized_help` rewrite it as `/bank_pay` while a Telegram
update is being handled (`command_spelling`, COMMAND_GROUPS).

`is_admin` reads config.py rather than the database, so the one role able to
grant every other cannot itself be granted or revoked through the bot.
`_rate_buckets` is the in-memory sliding-window limiter — one dictionary, and
losing it to a restart costs nothing but a forgotten window.

Not this module's zone: anything that knows about a platform. Nothing here
imports discord or aiogram, which is what lets both halves and the background
loops share it.
"""
import asyncio
import contextlib
import contextvars
import difflib
import logging
import re
import time
import weakref
from datetime import datetime, timedelta, timezone

from config import ADMINS, SERVICE_CHATS
import db
import quizzes

logger = logging.getLogger("dem.utils")

DEFAULT_EMBED_COLOR = 0x245590

PARTY_CODE_RE = re.compile(r"^[A-Za-z0-9]{4}$")

QUIZ_PREVIOUS_WINDOW = 30 * 60

def format_quiz_date(ts):
    """A stored quiz timestamp as 'YYYY-MM-DD HH:MM' in UTC."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "?"

def resolve_previous_quiz(platform, user_id, number):
    """Which quiz /quiz previous should compare. With a number, the quiz at
    that position in the /quiz take menu; without one, the quiz just taken —
    but only within QUIZ_PREVIOUS_WINDOW of finishing it.
    Returns (quiz_id, None) or (None, error_reply_key)."""
    ids = quizzes.list_quizzes()
    if number is None:
        row = db.get_last_quiz_result(platform, user_id)
        if not row:
            return None, "quizzes_previous_no_results"
        if int(time.time()) - int(row["created_at"]) > QUIZ_PREVIOUS_WINDOW:
            return None, "quizzes_previous_no_recent"
        return row["quiz_id"], None
    if number < 1 or number > len(ids):
        return None, "quizzes_previous_invalid"
    return ids[number - 1], None

def privacy_actions(platform, user_id):
    """The privacy areas open to this user, in menu order. Options that would do
    nothing — deleting results nobody stored, picking among a single quiz — are
    left out, so the numbering shifts with what the user actually has. Hiding
    from the /good top boards is always offered, and always first."""
    quiz_ids = db.get_user_quiz_ids(platform, user_id)
    actions = ["hide_top"]
    if quiz_ids:
        actions.append("clear_all")
    if len(quiz_ids) > 1:
        actions.append("clear_one")
    if quiz_ids:
        actions.append("block_all")
    if len(quiz_ids) > 1:
        actions.append("block_one")
    return actions

def privacy_option_labels(platform, user_id, lang, actions):
    """Display line for each privacy action, with the current state where the
    action is a toggle."""
    blocked_all = db.is_quiz_compare_blocked(platform, user_id)
    state = localized("state_yes" if blocked_all else "state_no", lang)
    hidden = db.is_leaderboard_hidden(platform, user_id)
    texts = {
        "hide_top": localized("privacy_opt_hide_top", lang,
                              state=localized("state_yes" if hidden else "state_no", lang)),
        "clear_all": localized("privacy_opt_clear_all", lang),
        "clear_one": localized("privacy_opt_clear_one", lang),
        "block_all": localized("privacy_opt_block_all", lang, state=state),
        "block_one": localized("privacy_opt_block_one", lang),
    }
    return [texts[a] for a in actions]

FOUNDAY_DEFAULT_TIME = (6, 0)

_WIKI_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)
_FOUNDAY_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*$")

def normalize_wiki_url(raw):
    """Accept a wiki link with or without the scheme; returns the canonical
    https form the Olympiad stores a candidate under, or None when it is not
    a link."""
    url = (raw or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    url = url.rstrip("/")
    return url if _WIKI_URL_RE.match(url) else None

def parse_founday_time(text, default=FOUNDAY_DEFAULT_TIME):
    """'HH:MM' or bare 'HH'. Raises ValueError on bad input. Named after the
    retired wiki anniversaries that introduced it; /server logs and /server tasks
    read their posting time through it."""
    if text is None or not str(text).strip():
        return default
    m = _FOUNDAY_TIME_RE.match(str(text))
    if not m:
        raise ValueError("invalid_founday_time")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("invalid_founday_time")
    return hour, minute

def utc_from_epoch(ts):
    """A Unix timestamp as an aware UTC datetime. datetime.fromtimestamp
    refuses negative values on Windows; wikis founded before 1970 are absurd,
    but the arithmetic form costs nothing."""
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=int(ts))

def format_date(moment, lang, with_year=True):
    """A date in words for people — '9 июля 2026', 'July 9, 2026' — from a
    Unix timestamp or a date; without the year for a calendar line."""
    if isinstance(moment, (int, float)):
        moment = utc_from_epoch(moment)
    month = localized(f"month_gen_{moment.month}", lang)
    return localized("date_long" if with_year else "date_day_month", lang,
                     day=moment.day, month=month, year=moment.year)

def plural_category(lang, n):
    """CLDR plural category of `n` for the languages the bot speaks, and for
    Belarusian, which a wiki birthday greeting may be written in."""
    n = abs(int(n))
    if lang in ("ru", "uk", "be"):
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    if lang == "pl":
        if n == 1:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    return "one" if n == 1 else "other"

def parse_union_names(text):
    """'en=Name | ru=Название | uk=-' as {lang: name or None}: a name to set,
    or None where '-' asks to remove the name in that language. Returns None
    when any part is not 'code=value' with one of the six codes."""
    names = {}
    for chunk in (text or "").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or key not in SUPPORTED_LANGS or not value:
            return None
        names[key] = None if value == "-" else value[:100]
    return names or None

_TZ_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::(\d{2}))?$", re.IGNORECASE)

def parse_tz_offset(text):
    """A timezone as a UTC offset — 'UTC+3', '+3', '-05:30', 'UTC' — in minutes
    east of UTC. Raises ValueError on bad input."""
    s = (text or "").strip()
    if not s or s.upper() in ("UTC", "GMT", "0", "Z"):
        return 0
    m = _TZ_RE.match(s)
    if not m:
        raise ValueError("invalid_tz")
    sign = -1 if m.group(1) == "-" else 1
    hours, minutes = int(m.group(2)), int(m.group(3) or 0)
    if hours > 14 or minutes > 59:
        raise ValueError("invalid_tz")
    return sign * (hours * 60 + minutes)

def format_tz_offset(minutes):
    """A UTC offset in minutes back as 'UTC+3' / 'UTC-05:30'."""
    minutes = int(minutes or 0)
    sign = "-" if minutes < 0 else "+"
    m = abs(minutes)
    return f"UTC{sign}{m // 60:02d}:{m % 60:02d}"

_WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2,
    "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4,
    "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}

def parse_weekday(text):
    """A weekday as 1-7 (1 = Monday) or an English name/abbreviation, returned
    as 0-6 with Monday 0. Raises ValueError on bad input."""
    s = (text or "").strip().lower()
    if s.isdigit():
        n = int(s)
        if 1 <= n <= 7:
            return n - 1
        raise ValueError("invalid_weekday")
    if s in _WEEKDAY_ALIASES:
        return _WEEKDAY_ALIASES[s]
    raise ValueError("invalid_weekday")

def weekday_name(index, lang):
    """The localized name of a weekday (0 = Monday)."""
    return localized(f"weekday_{int(index) % 7}", lang)

CATEGORY_EMOJI = {cat: emoji for _code, cat, emoji, _name in db.BASE_GOODS}

def category_label(category, lang, with_emoji=True):
    """The localized display name of a base category, with its emoji."""
    name = localized(f"category_{category}", lang)
    emoji = CATEGORY_EMOJI.get(category, "")
    return f"{emoji} {name}".strip() if with_emoji else name

def good_display_name(good, lang):
    """A good's name for humans: the five base goods are localized, user-created
    goods keep the name they were given."""
    if db.is_base_good(good["code"]):
        return localized(f"category_{good['category']}", lang)
    return good["name"]

def provision_level_label(level, lang):
    """The localized name of a provision level."""
    return localized(f"provision_level_{level}", lang)

_FIND_PERIOD_RE = re.compile(
    r"^\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*(?:\+\s*(\d+)\s*[dD])?\s*$"
)

def parse_find_period(text):
    """Parse 'MM-DD-YYYY' or 'MM-DD-YYYY + <N>D' into an aware UTC
    (start, end) pair covering N days (1 day when N is omitted).
    Raises ValueError on bad input."""
    m = _FIND_PERIOD_RE.match(text or "")
    if not m:
        raise ValueError("invalid_period")
    month, day, year, days = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    days = int(days) if days else 1
    if days < 1:
        raise ValueError("invalid_period")
    start = datetime(year, month, day, tzinfo=timezone.utc)
    return start, start + timedelta(days=days)

def parse_keywords(text):
    """Semicolon-separated keywords, lowercased; empty chunks dropped."""
    return [part.strip().lower() for part in (text or "").split(";") if part.strip()]

def find_close_names(query, candidates, n=5, cutoff=0.5):
    """Fuzzy match: `candidates` is {label_lowercase: item}; returns unique items
    ordered by similarity to `query`."""
    matches = difflib.get_close_matches(query.lower(), list(candidates.keys()), n=n, cutoff=cutoff)
    out = []
    for label in matches:
        item = candidates[label]
        if item not in out:
            out.append(item)
    return out

class DialogOwners:
    """Which dialog a person is answering in a chat: one at a time.

    A dialog is the asyncio task of the command that asks the questions, and
    every wait for the person's next message claims its place first. A newer
    dialog of the same person in the same chat takes the place over: the
    older one's pending wait is cancelled (the future it handed to `watch`),
    and `claim` refuses the older task from then on, so its next question
    returns at once instead of competing for the same answer. Before this,
    Discord handed one message to every dialog waiting in the channel, so a
    single reply answered all of them, and on Telegram an older dialog could
    take the place back from a newer one at its next question.

    Keys are (platform, chat, user): dialogs in different chats do not touch
    each other. `replaced()` tells a dialog that has just been given nothing
    why, so it can say so (`dialog_end`). One instance, DIALOGS, serves both
    halves."""

    def __init__(self):
        """Nobody is in a dialog yet."""
        self._owners = {}
        self._waits = {}
        self._replaced = weakref.WeakSet()

    def claim(self, key):
        """Make the current task the dialog at `key`, replacing a different
        one that is still running. Returns False when the current task has
        itself been replaced there, or anywhere — its dialog is over."""
        task = asyncio.current_task()
        if task is None:
            return True
        if task in self._replaced:
            return False
        ref = self._owners.get(key)
        owner = ref() if ref is not None else None
        if owner is not None and owner is not task and not owner.done():
            self._replaced.add(owner)
            pending = self._waits.pop(key, None)
            if pending is not None and not pending.done():
                pending.cancel()
        self._owners[key] = weakref.ref(task)
        return True

    def owns(self, key, task):
        """Whether `task` is still the dialog at `key`. Discord asks this in
        the filter of every wait: cancelling a replaced wait takes effect a
        step of the event loop later, and a message arriving in between must
        not reach the replaced dialog."""
        ref = self._owners.get(key)
        return ref is not None and ref() is task

    def watch(self, key, future):
        """The future the dialog at `key` is waiting on — what a newer
        dialog cancels."""
        self._waits[key] = future

    def release(self, key, future):
        """The wait on `future` is over."""
        if self._waits.get(key) is future:
            self._waits.pop(key, None)

    def replaced(self):
        """Whether the current task's dialog was replaced by a newer one."""
        task = asyncio.current_task()
        return task is not None and task in self._replaced

DIALOGS = DialogOwners()

def dialog_end(lang):
    """What a dialog says when it ends without an answer: that a newer
    dialog replaced it, or that time ran out."""
    return localized("dialog_replaced" if DIALOGS.replaced() else "dialog_timeout", lang)

EMOJI_MAX = 32
_DISCORD_EMOJI = r"a?:[A-Za-z0-9_]{2,32}:[0-9]{17,20}"
_DISCORD_EMOJI_RE = re.compile(rf"<{_DISCORD_EMOJI}>")
_DISCORD_EMOJI_ANYWHERE_RE = re.compile(rf"<{_DISCORD_EMOJI}>|&lt;{_DISCORD_EMOJI}&gt;")
_CLOSING_PUNCTUATION = ",.;:!?)]}"

def parse_emoji(text):
    """The emoji somebody typed for a currency or a good, ready to store.

    A custom emoji of a Discord server arrives as `<:name:id>` (`<a:name:id>`
    when animated): up to 32 characters of name and 17 to 20 digits of id, 57
    characters at most. It is returned whole, even when text follows it
    without a space. Anything else that starts with `<` is not an emoji — a
    custom emoji cut short, or markup — and gives an empty string, so no
    fragment is ever stored. Otherwise the first word is taken, at most
    EMOJI_MAX characters: that is long enough for the longest composed emoji
    (flags, ZWJ sequences, skin tones), and a word that is longer is not an
    emoji, so a joiner left dangling by the cut is dropped.

    Discord's own emoji mean nothing in Telegram; strip_discord_emoji takes
    them off what goes there."""
    text = (text or "").strip()
    found = _DISCORD_EMOJI_RE.match(text)
    if found:
        return found.group(0)
    if not text or text.startswith("<"):
        return ""
    return text.split(None, 1)[0][:EMOJI_MAX].rstrip("\u200d")

def strip_discord_emoji(text):
    """`text` without the custom emoji of Discord servers, which Telegram
    cannot show, and without the space they leave behind: '12.34 <:s:1…> STAR'
    becomes '12.34 STAR'.

    What is left of the spacing is decided by what stands on each side of the
    gap. Two spaces become one; a space is dropped where it would end a line
    or stand before a closing mark ('STAR <:s:1…>' → 'STAR', 'x <:s:1…>,' →
    'x,') and where it would begin a line ('<:s:1…> STAR' → 'STAR'); a gap
    with a space on one side only, or none, is just closed ('Bank <:s:1…>Star'
    → 'Bank Star'), so two words are never glued together by the removal.

    Finds the emoji both raw and HTML-escaped (`&lt;…&gt;`), because by the
    time a message reaches Telegram the bot has usually escaped it — which is
    why the one place that calls this is the Telegram client's request
    middleware (telegram_bot/client.py) and not each line of output."""
    if not isinstance(text, str) or ":" not in text:
        return text
    segments = _DISCORD_EMOJI_ANYWHERE_RE.split(text)
    if len(segments) == 1:
        return text
    result = segments[0]
    last = len(segments) - 1
    for index, segment in enumerate(segments[1:], 1):
        if not segment and index != last:
            continue
        left, right = result[-1:], segment[:1]
        if left in (" ", "\t") and right in (" ", "\t"):
            segment = segment[1:]
        elif left in (" ", "\t") and (not right or right == "\n" or right in _CLOSING_PUNCTUATION):
            result = result[:-1]
        elif left in ("", "\n") and right in (" ", "\t"):
            segment = segment[1:]
        result += segment
    return result

_ROLE_REF_RE = re.compile(r"^<@&(\d+)>$|^(\d{5,25})$")

def parse_role_ids(text):
    """The Discord role ids in `text` — mentions (`<@&123>`) or bare ids,
    separated by commas or spaces — as ints in the order given, or None when
    any piece is neither."""
    ids = []
    for piece in (text or "").replace(",", " ").split():
        m = _ROLE_REF_RE.match(piece)
        if not m:
            return None
        value = int(m.group(1) or m.group(2))
        if value not in ids:
            ids.append(value)
    return ids

def apply_party_roles(union_code, action, role_ids):
    """Carry out `/union parties <union> <action> [roles]` on the stored
    settings — the part both halves share. `action` is enable, disable, add
    or remove. Returns (reply key, the roles now allowed, whether parties
    are on); the key is an error key when nothing was changed.

    `enable` with roles replaces the list, and without them keeps the list
    the union already has — it needs one either way, or nobody could found
    a party. `add` and `remove` change single roles and leave parties on or
    off as they were; removing the last role of an enabled union is allowed
    and said in the reply."""
    enabled, current = db.get_party_settings(union_code)
    if action == "disable":
        db.set_parties_enabled(union_code, False)
        return "allow_parties_disabled", sorted(current), False
    if action == "enable":
        roles = list(role_ids) or sorted(current)
        if not roles:
            return "allow_parties_need_roles", [], enabled
        db.set_parties_enabled(union_code, True, roles)
        return "allow_parties_enabled", roles, True
    if action not in ("add", "remove"):
        return "allow_parties_usage", sorted(current), enabled
    if not role_ids:
        return "allow_parties_need_roles", sorted(current), enabled
    roles = (current | set(role_ids)) if action == "add" else (current - set(role_ids))
    db.set_party_roles(union_code, roles)
    if enabled and not roles:
        return "allow_parties_no_roles_left", [], enabled
    return "allow_parties_roles_set", sorted(roles), enabled

def search_by_name(query, items, labels, limit=5, cutoff=0.6):
    """(the item `query` names exactly, or None; the items it resembles) —
    how `/party info` and `/union info` find what somebody typed.

    `labels(item)` gives every string an item is known by: its code and its
    names. A label equal to the query, ignoring case, wins outright.
    Otherwise an item resembles the query when one of its labels contains
    it (three characters or more) or difflib rates the two at `cutoff` or
    better; the best `limit` come back, closest first. A query that
    resembles nothing finds nothing — there is no fallback to unrelated
    items, which used to answer gibberish with three random parties."""
    q = (query or "").strip().lower()
    if not q:
        return None, []
    scored = []
    for item in items:
        names = [str(label).lower() for label in labels(item) if label]
        if q in names:
            return item, []
        best = 0.0
        for name in names:
            if len(q) >= 3 and q in name:
                best = max(best, 0.9)
            best = max(best, difflib.SequenceMatcher(None, q, name).ratio())
        if best >= cutoff:
            scored.append((best, item))
    scored.sort(key=lambda pair: -pair[0])
    return None, [item for _score, item in scored[:limit]]

def format_stored_user(viewer_platform, platform, user_id, display_name):
    """Render a stored (platform, user_id, name) for a viewer on some platform.

    A real ping is used only when the member and the viewer are both on Discord;
    everywhere else (in particular a Discord member shown in Telegram) the saved
    nickname is used, never a cross-platform ping that would not resolve."""
    if viewer_platform == platform == "discord":
        return f"<@{user_id}>"
    if platform == "telegram" and display_name and display_name.startswith("@"):
        return display_name
    return display_name or str(user_id)

def is_verified(platform, user_id):
    """Whether the user may use the Fandom-activity commands. Discord users
    verify with /account verify; Telegram users by linking to a verified Discord
    account with /account_discord + /account telegram. Verification is global."""
    if platform == "discord":
        return db.is_discord_verified(str(user_id))
    if platform == "telegram":
        return db.is_telegram_verified(str(user_id))
    return False

def is_admin(platform, user_id):
    """Whether the user is a Bot Admin.

    Read from config.py, never from the database, so that the one role able to
    grant every other cannot be granted — or revoked — through the bot itself."""
    return user_id in ADMINS.get(platform, set())

_rate_buckets = {}

def rate_limit_ok(key, limit, window_seconds):
    """Sliding-window rate limiter. Returns True if the action is allowed,
    False if `limit` actions already happened within `window_seconds`."""
    now = time.monotonic()
    if len(_rate_buckets) > 10000:
        stale = [k for k, v in _rate_buckets.items() if not v or v[-1] < now - 3600]
        for k in stale:
            _rate_buckets.pop(k, None)
    bucket = _rate_buckets.setdefault(key, [])
    cutoff = now - window_seconds
    while bucket and bucket[0] <= cutoff:
        bucket.pop(0)
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True

SUPPORTED_LANGS = {"ru", "uk", "pl", "en", "es", "pt"}
DEFAULT_LANG = "en"

STATUS_TEXT = "Unio, progressio et diplomatia"

import os as _i18n_os
import json as _i18n_json
import logging as _i18n_logging

_I18N_DIR = _i18n_os.path.join(_i18n_os.path.dirname(__file__), "i18n")

LOCALE_STATUS_EMOJI = {"verified": "\U0001F7E9", "unverified": "\U0001F7E7", "untranslated": "\U0001F7E5"}

def _load_i18n():
    """Build the runtime localization structures from the i18n/<lang>.json files.

    Returns (locale, status, flat):
      locale[key][lang] = text, with dotted keys 'group.sub' rebuilt into
        locale[group][sub][lang] so the legacy localized_* helpers keep working.
      status[flat_key][lang] = 'verified' | 'unverified' | 'untranslated'
      flat[lang][flat_key] = text
    """
    locale, status, flat = {}, {}, {}
    if _i18n_os.path.isdir(_I18N_DIR):
        for _fname in sorted(_i18n_os.listdir(_I18N_DIR)):
            if not _fname.endswith(".json"):
                continue
            _lang = _fname[:-5]
            with open(_i18n_os.path.join(_I18N_DIR, _fname), encoding="utf-8") as _f:
                _entries = _i18n_json.load(_f)
            flat[_lang] = {}
            for _k, _entry in _entries.items():
                _text = _entry["text"]
                flat[_lang][_k] = _text
                status.setdefault(_k, {})[_lang] = _entry.get("status", "unverified")
                if "." in _k:
                    _g, _s = _k.split(".", 1)
                    locale.setdefault(_g, {}).setdefault(_s, {})[_lang] = _text
                else:
                    locale.setdefault(_k, {})[_lang] = _text
    return locale, status, flat

_LOCALE, _LOCALE_STATUS, _LOCALE_FLAT = _load_i18n()

LANGUAGE_NAMES = {
    "ru": "Русский",
    "uk": "Українська",
    "pl": "Polski",
    "en": "English",
    "es": "Español",
    "pt": "Português",
    "be": "Беларуская",
}

LANG_ORDER = ["ru", "uk", "pl", "en", "es", "pt"]

LANGUAGE_FLAGS = {
    "be": "🇧🇾",
}

def language_name(code):
    """A language's own name for its code, falling back to the bare code.
    Besides the six the bot speaks, LANGUAGE_NAMES knows the languages its
    union wikis are written in (Belarusian)."""
    return LANGUAGE_NAMES.get(code, code)

def language_flag(code):
    """The flag a language is marked with: the six languages' own
    (`lang_flag`), a wiki language's from LANGUAGE_FLAGS, a globe for any
    other."""
    if code in SUPPORTED_LANGS:
        return localized("lang_flag", code)
    return LANGUAGE_FLAGS.get(code, "🌐")

def available_locales():
    """Languages that have an i18n file, in display order."""
    return [L for L in LANG_ORDER if L in _LOCALE_FLAT]

def reply_keys():
    """All reply codes, taken from the reference (DEFAULT_LANG) localization."""
    return sorted(_LOCALE_FLAT.get(DEFAULT_LANG, {}).keys())

def get_reply(lang, key):
    """The raw localization value for a key in a language, or None."""
    return _LOCALE_FLAT.get(lang, {}).get(key)

def reply_status(lang, key):
    """'verified' | 'unverified' | 'untranslated', or None if the key is unknown."""
    known = any(key in _LOCALE_FLAT.get(L, {}) for L in _LOCALE_FLAT)
    if not known:
        return None
    if key not in _LOCALE_FLAT.get(lang, {}):
        return "untranslated"
    return _LOCALE_STATUS.get(key, {}).get(lang, "unverified")

def locale_stats(lang):
    """Counts relative to the DEFAULT_LANG key set, plus the verified percentage."""
    ref = list(_LOCALE_FLAT.get(DEFAULT_LANG, {}).keys())
    total = len(ref)
    have = _LOCALE_FLAT.get(lang, {})
    verified = unverified = untranslated = 0
    for k in ref:
        if k not in have:
            untranslated += 1
            continue
        st = _LOCALE_STATUS.get(k, {}).get(lang, "unverified")
        if st == "verified":
            verified += 1
        elif st == "untranslated":
            untranslated += 1
        else:
            unverified += 1
    percent = round(verified / total * 100) if total else 0
    return {"total": total, "verified": verified, "unverified": unverified,
            "untranslated": untranslated, "percent": percent}

def locale_bar(lang, width=12):
    """A ten-cell progress bar for a language's translation state."""
    s = locale_stats(lang)
    total = s["total"] or 1
    v = round(s["verified"] / total * width)
    u = round(s["unverified"] / total * width)
    v = min(v, width)
    u = min(u, width - v)
    t = width - v - u
    return LOCALE_STATUS_EMOJI["verified"] * v + LOCALE_STATUS_EMOJI["unverified"] * u + LOCALE_STATUS_EMOJI["untranslated"] * t

def compare_reply(key):
    """Return {lang: (status, text|None)} across all languages, or None if unknown."""
    known = any(key in _LOCALE_FLAT.get(L, {}) for L in _LOCALE_FLAT)
    if not known:
        return None
    out = {}
    for L in LANG_ORDER:
        text = _LOCALE_FLAT.get(L, {}).get(key)
        if text is None:
            out[L] = ("untranslated", None)
        else:
            out[L] = (_LOCALE_STATUS.get(key, {}).get(L, "unverified"), text)
    return out

COMMAND_GROUPS = {
    "account": ("privacy",),
    "admin": ("add", "remove"),
    "bank": ("add", "info", "leader", "transfer", "edit", "open", "balance", "pay", "earn",
             "fine", "emit", "transactions", "treaty", "rate", "convert", "rates", "wage",
             "profession", "earn-info"),
    "birthday": ("set", "channel"),
    "bot": ("chats", "leave", "backup"),
    "enterprise": ("add", "info", "edit", "join", "leave", "kick", "position", "assign",
                   "salary", "sell", "export", "autoexport", "transit"),
    "good": ("add", "info", "craft", "autocraft", "autosend", "inventory", "sell", "give", "top"),
    "govt": ("add", "info"),
    "locale": ("info", "compare", "suggest", "reply"),
    "localizer": ("add", "remove"),
    "olympiad": ("set", "contest-add", "contest-edit", "vote"),
    "party": ("add", "info", "edit", "edit-admin", "rules", "join", "leave", "kick", "dues"),
    "quiz": ("take", "stop", "clear", "compare", "previous"),
    "server": ("setup", "info", "edit", "birthday", "lang", "lang-channel", "logs", "tasks"),
    "sponsor": ("info", "limit"),
    "union": ("add", "info", "edit", "parties"),
    "wiki": ("add", "remove", "founded"),
}
_command_platform = contextvars.ContextVar("dem_command_platform", default="discord")
_UNDERSCORED_COMMAND_RE = re.compile(r"(?<![\w/\\])/[a-z]+(?:_[a-z]+)+")
_TELEGRAM_COMMAND_RE = re.compile(r"(?<![\w/])/(" + "|".join(sorted(
    (re.escape(f"{group} {action}") for group, actions in COMMAND_GROUPS.items()
     for action in actions), key=len, reverse=True)) + r")(?![\w-])")

def set_command_platform(platform):
    """Spell the commands in every text produced from here on, in this task,
    for `platform` ('discord' or 'telegram').

    A context variable rather than an argument because a reply is produced by
    hundreds of `localized()` calls, all of which would have to be told. The
    Telegram half sets it once per update in an outer middleware
    (telegram_bot/client.py); asyncio copies the context into each task, so
    it stays with the update and never leaks into a Discord one."""
    _command_platform.set(platform)

@contextlib.contextmanager
def command_platform(platform):
    """Spell the commands of every text produced inside the block for
    `platform`, then put the previous setting back.

    The spelling belongs to the reader, and the middleware only knows the
    reader of the update being handled. Anything that writes for somebody
    else — a service notice sent to chats on both platforms, a message
    handed to the other messenger — decides at the point where it knows who
    will read, with this."""
    token = _command_platform.set(platform)
    try:
        yield
    finally:
        _command_platform.reset(token)

def command_spelling(text, platform=None):
    """`text` with every command written as `platform` spells it.

    On Discord related commands are subcommands of one group, `/bank pay`
    (discord_bot/groups.py); a Telegram command cannot contain a space, so
    there the same action is `/bank_pay`. The texts are written once, in the
    Discord spelling, and this turns `/<group> <action>` into
    `/<group>_<action>` (a hyphen in the action becomes an underscore too)
    when the reader is on Telegram.

    COMMAND_GROUPS lists only the actions that exist on both platforms, so a
    Discord-only one (`/account verify`, `/wiki channel`, `/birthday role`,
    `/find with`, `/olympiad accept`) keeps its space and still reads as the
    Discord command it is. A Telegram-only one (`/account_discord`) is written
    with its underscore in the texts and needs nothing. A text that asks a
    Telegram reader to run a shared command *on Discord* writes the space as a
    no-break space (U+00A0), which this leaves alone.
    discord_bot/groups.py: register warns when a group gains an action this
    table does not know.

    Read on Discord, a Telegram spelling has its underscores escaped
    instead: Discord's markdown takes an underscore for the start of
    italics, and one `/account_discord` in a help page used to turn the
    whole rest of the page italic."""
    if not isinstance(text, str):
        return text
    if (platform or _command_platform.get()) != "telegram":
        return _UNDERSCORED_COMMAND_RE.sub(lambda m: m.group(0).replace("_", "\\_"), text)
    return _TELEGRAM_COMMAND_RE.sub(
        lambda m: "/" + m.group(1).replace(" ", "_").replace("-", "_"), text)

_NAME_FORMS = ("nom_pl", "gen_pl", "nom", "gen", "dat", "acc", "ins", "loc", "sg", "pl")

class _TextValues(dict):
    """The values of one localized text, adapted to its reader's language.

    A value that can write itself for a language (economy/names.py: Money,
    an amount) is written that way. A placeholder `{<value>_<form>}` that
    was not passed — `{name_loc}`, `{name_ins}` — is the form of value
    `<value>` (economy/names.py: CurrencyName answers its declined forms),
    or the value itself when it has no forms, so a call site that passes a
    plain name still produces a whole sentence."""

    def __init__(self, values, locale):
        """Adapt `values` to `locale`."""
        super().__init__({key: value.for_lang(locale) if hasattr(value, "for_lang") else value
                          for key, value in values.items()})
        self._raw, self._locale = values, locale

    def __missing__(self, key):
        """A form of a value that was passed under its own name."""
        for form in _NAME_FORMS:
            base = key[:-len(form) - 1]
            if key.endswith("_" + form) and base in self._raw:
                value = self._raw[base]
                return value.form(self._locale, form) if hasattr(value, "form") else value
        raise KeyError(key)

def localized(_key, locale, **kwargs):
    """Generic flat-key accessor (used everywhere a reply is produced).
    Amounts and currency names among the values are written for `locale`
    (`_TextValues`)."""
    table = _LOCALE.get(_key)
    if table is None:
        _i18n_logging.getLogger("dem.i18n").warning(
            "Missing localization key %r — i18n files are older than the code?", _key
        )
        table = {}
    template = table.get(locale, table.get(DEFAULT_LANG, _key))
    if isinstance(template, (list, tuple)):
        return [command_spelling(item) for item in template]
    template = command_spelling(template)
    try:
        return template.format_map(_TextValues(kwargs, locale))
    except Exception:
        return template

def get_chat_lang(chat_id):
    """The language to answer a chat in, falling back to the default.

    The thin wrapper every command uses: db/settings.py does the outward
    resolution from the specific key, and this turns a miss into DEFAULT_LANG so
    that no caller has to."""
    lang = db.get_chat_lang(chat_id)
    if lang and lang in SUPPORTED_LANGS:
        return lang
    return DEFAULT_LANG

def set_chat_lang(chat_id, lang_code, is_dm=False):
    """Remember a chat's language. `is_dm` marks a private conversation,
    which is kept only a year."""
    if lang_code not in SUPPORTED_LANGS:
        raise ValueError("unsupported_lang")
    db.set_chat_lang(chat_id, lang_code, is_dm=is_dm)

def localized_help(event_key, lang, **kwargs):
    """A help-section string. Kept apart from `localized` because the help
    keys live under their own prefix and a missing one should show the key rather
    than an empty line."""
    table = _LOCALE.get("help", {}).get(event_key, {})
    template = command_spelling(table.get(lang, table.get(DEFAULT_LANG, event_key)))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_service_event(event_key, lang, **kwargs):
    """The wording of one service event in the operator's language, with its
    commands spelled for the platform in force (`command_platform`)."""
    table = _LOCALE.get("service_event", {}).get(event_key, {})
    template = command_spelling(table.get(lang, table.get(DEFAULT_LANG, event_key)))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def _normalize_service_chat_key(platform, raw_key):
    """Bring a configured chat key to the shape the platform expects: a
    bare channel id on Discord, 'chat_id:thread_id' on Telegram.

    Written for tolerance, because these come from a hand-edited config.py."""
    key = str(raw_key).strip()
    if not key:
        return None, None

    if ":" in key:
        left, right = key.split(":", 1)
        try:
            return int(left), int(right)
        except Exception:
            return None, None

    try:
        single = int(key)
    except Exception:
        return None, None

    if platform == "telegram":
        return single, 0
    if platform == "discord":
        return None, single
    return None, None

async def send_service_event(event_key, **kwargs):
    """Post a localized service notice (bot start/stop, /server setup, /union add,
    /union parties, /party add results) to every SERVICE_CHATS entry. Lives here
    so both bots can call it without re-importing main."""
    from telegram_bot import bot as tg
    from discord_bot import bot as dc

    for chat_key in SERVICE_CHATS.get("telegram", set()):
        try:
            chat_id, thread = _normalize_service_chat_key("telegram", chat_key)
            if chat_id is None:
                continue
            lang = get_chat_lang(f"{chat_id}:{thread}")
            with command_platform("telegram"):
                text = localized_service_event(event_key, lang, **kwargs)
            await tg.send_message(
                int(chat_id),
                text,
                message_thread_id=int(thread) or None
            )
        except Exception as e:
            logger.warning("send_service_event failed for telegram %s: %s", chat_key, e)

    for chat_key in SERVICE_CHATS.get("discord", set()):
        try:
            guild_id, channel_id = _normalize_service_chat_key("discord", chat_key)
            if channel_id is None:
                continue
            ch = dc.get_channel(channel_id)
            if not ch:
                try:
                    ch = await dc.fetch_channel(channel_id)
                except Exception:
                    ch = None
            effective_guild_id = guild_id
            if effective_guild_id is None and ch and getattr(ch, "guild", None):
                effective_guild_id = ch.guild.id
            lang_key = f"{effective_guild_id}:{channel_id}" if effective_guild_id is not None else str(channel_id)
            lang = get_chat_lang(lang_key)
            with command_platform("discord"):
                text = localized_service_event(event_key, lang, **kwargs)
            if ch:
                await ch.send(text)
        except Exception as e:
            logger.warning("send_service_event failed for discord %s: %s", chat_key, e)
