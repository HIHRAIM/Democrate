import calendar
import difflib
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from config import ADMINS, SERVICE_CHATS
import db
import quizzes

logger = logging.getLogger("dem.utils")

DEFAULT_EMBED_COLOR = 0x245590

PARTY_CODE_RE = re.compile(r"^[A-Za-z0-9]{4}$")

QUIZ_PREVIOUS_WINDOW = 30 * 60

def format_quiz_date(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "?"

def resolve_previous_quiz(platform, user_id, number):
    """Which quiz /quizzes-previous should compare. With a number, the quiz at
    that position in the /quizzes menu; without one, the quiz just taken —
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
    left out, so the numbering shifts with what the user actually has."""
    quiz_ids = db.get_user_quiz_ids(platform, user_id)
    actions = []
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
    texts = {
        "clear_all": localized("privacy_opt_clear_all", lang),
        "clear_one": localized("privacy_opt_clear_one", lang),
        "block_all": localized("privacy_opt_block_all", lang, state=state),
        "block_one": localized("privacy_opt_block_one", lang),
    }
    return [texts[a] for a in actions]

FOUNDAY_DEFAULT_TIME = (6, 0)

_WIKI_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)
_FOUNDAY_DT_RE = re.compile(r"^\s*(\d{1,2})-(\d{1,2})-(\d{4})(?:[\s,]+(\d{1,2})(?::(\d{2}))?)?\s*$")
_FOUNDAY_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*$")

def normalize_wiki_url(raw):
    """Accept a wiki link with or without the scheme; returns the canonical
    https form used as the per-guild key, or None when it is not a link."""
    url = (raw or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    url = url.rstrip("/")
    return url if _WIKI_URL_RE.match(url) else None

def parse_founday_datetime(text):
    """'MM-DD-YYYY' or 'MM-DD-YYYY HH[:MM]' in UTC. Without a time the wiki
    counts as founded at midnight. Raises ValueError on bad input."""
    m = _FOUNDAY_DT_RE.match(text or "")
    if not m:
        raise ValueError("invalid_founday_date")
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour, minute = int(m.group(4) or 0), int(m.group(5) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("invalid_founday_date")
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)

def parse_founday_time(text, default=FOUNDAY_DEFAULT_TIME):
    """'HH:MM' or bare 'HH' in UTC. Raises ValueError on bad input."""
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
    """datetime.fromtimestamp refuses negative values on Windows; wikis founded
    before 1970 are absurd, but the arithmetic form costs nothing."""
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=int(ts))

def plural_category(lang, n):
    """CLDR plural category of `n` for the languages the bot speaks."""
    n = abs(int(n))
    if lang in ("ru", "uk"):
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

def is_founday_today(founded, now):
    """Whether `now` falls on the wiki's anniversary. A 29 February wiki is
    congratulated on 28 February in common years."""
    if founded.month == 2 and founded.day == 29 and not calendar.isleap(now.year):
        return (now.month, now.day) == (2, 28)
    return (now.month, now.day) == (founded.month, founded.day)

def founday_line(lang, name, years, past):
    tense = "past" if past else "future"
    return localized(f"founday_{tense}_{plural_category(lang, years)}", lang, name=name, n=years)

def founday_message(server_lang, wiki_lang, name, years, url, past):
    """The anniversary post. When the wiki speaks another language than the
    chat, its own greeting follows on a second line and the wiki's flag marks
    both."""
    first = founday_line(server_lang, name, years, past)
    if wiki_lang == server_lang:
        return f"{first}\n{url}"
    flag = localized("lang_flag", wiki_lang)
    second = founday_line(wiki_lang, name, years, past)
    return f"{first} {flag}\n{flag}{second}\n{url}"

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
    verify with /verify; Telegram users by linking to a verified Discord
    account with /add-discord + /add-telegram. Verification is global."""
    if platform == "discord":
        return db.is_discord_verified(str(user_id))
    if platform == "telegram":
        return db.is_telegram_verified(str(user_id))
    return False

def is_admin(platform, user_id):
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
}

LANG_ORDER = ["ru", "uk", "pl", "en", "es", "pt"]

def language_name(code):
    return LANGUAGE_NAMES.get(code, code)

def available_locales():
    """Languages that have an i18n file, in display order."""
    return [L for L in LANG_ORDER if L in _LOCALE_FLAT]

def reply_keys():
    """All reply codes, taken from the reference (DEFAULT_LANG) localization."""
    return sorted(_LOCALE_FLAT.get(DEFAULT_LANG, {}).keys())

def get_reply(lang, key):
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

def localized(_key, locale, **kwargs):
    """Generic flat-key accessor (used everywhere a reply is produced)."""
    table = _LOCALE.get(_key)
    if table is None:
        _i18n_logging.getLogger("dem.i18n").warning(
            "Missing localization key %r — i18n files are older than the code?", _key
        )
        table = {}
    template = table.get(locale, table.get(DEFAULT_LANG, _key))
    if isinstance(template, (list, tuple)):
        return template
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def get_chat_lang(chat_id):
    lang = db.get_chat_lang(chat_id)
    if lang and lang in SUPPORTED_LANGS:
        return lang
    return DEFAULT_LANG

def set_chat_lang(chat_id, lang_code, is_dm=False):
    if lang_code not in SUPPORTED_LANGS:
        raise ValueError("unsupported_lang")
    db.set_chat_lang(chat_id, lang_code, is_dm=is_dm)

def localized_help(event_key, lang, **kwargs):
    table = _LOCALE.get("help", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_service_event(event_key, lang, **kwargs):
    table = _LOCALE.get("service_event", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def _normalize_service_chat_key(platform, raw_key):
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
    """Post a localized service notice (bot start/stop, /setup, /add-unia,
    /allow-parties, /add-party results) to every SERVICE_CHATS entry. Lives here
    so both bots can call it without re-importing main."""
    from telegram_bot import bot as tg
    from discord_bot import bot as dc

    for chat_key in SERVICE_CHATS.get("telegram", set()):
        try:
            chat_id, thread = _normalize_service_chat_key("telegram", chat_key)
            if chat_id is None:
                continue
            lang = get_chat_lang(f"{chat_id}:{thread}")
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
            text = localized_service_event(event_key, lang, **kwargs)
            if ch:
                await ch.send(text)
        except Exception as e:
            logger.warning("send_service_event failed for discord %s: %s", chat_key, e)
