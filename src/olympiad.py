"""The wiki Olympiad: the platform-neutral rules both bots share.

`db.py` stores the event, its contests, their candidate wikis and the votes
awaiting review; this module holds the *policy* around them — when the event is
open and when voting is actually allowed, how a contest is named across the
localizations, in what order contests are offered to a particular person, how a
reply listing candidate numbers is read, and what a vote looks like once it is
ready to be posted for review.

An Olympiad is a bounded event. `/setolympiad MM-DD-YYYY MM-DD-YYYY` opens it;
from that moment until the end of the closing day (UTC) Bot Admins may run the
setup commands, while everyone else may vote only inside the same window. When
it is over the contests, the candidates and every vote are deleted — see
`db.clear_olympiad_data`.

Like the quizzes, this feature keeps its own localization: one flat JSON file
per language in `i18n/olympiad/`, read through `text()`. That keeps a few dozen
Olympiad strings out of the main locale files, which are shared with the
translation commands and measured by `/locale`.
"""

import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import db
from utils import (DEFAULT_LANG, LANG_ORDER, SUPPORTED_LANGS, language_name,
                   normalize_wiki_url)

logger = logging.getLogger("dem.olympiad")

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONTENT_DIR = os.path.join(_HERE, "i18n", "olympiad")

INTERNATIONAL = "int"

# Shown before a contest's name so the language category is readable at a glance.
LANG_FLAG = {
    "ru": "🇷🇺", "uk": "🇺🇦", "pl": "🇵🇱",
    "en": "🇬🇧", "es": "🇪🇸", "pt": "🇵🇹",
    INTERNATIONAL: "🌐",
}

# How many wikis one person may support in a single contest.
MAX_CHOICES = 3

_content_cache = None

def _load_content():
    """{lang: content_dict} for every i18n/olympiad/<lang>.json present."""
    global _content_cache
    if _content_cache is None:
        langs = {}
        if os.path.isdir(_CONTENT_DIR):
            for fname in sorted(os.listdir(_CONTENT_DIR)):
                if not fname.endswith(".json"):
                    continue
                lang = fname[:-len(".json")]
                try:
                    with open(os.path.join(_CONTENT_DIR, fname), encoding="utf-8") as f:
                        langs[lang] = json.load(f)
                except Exception as e:
                    logger.warning("Failed to load Olympiad content %s: %s", fname, e)
        _content_cache = langs
    return _content_cache

def text(_key, _lang, **kwargs):
    """A localized Olympiad string, falling back to the default language and
    finally to the key itself, so a missing translation never breaks a dialog.
    The two positional names are underscored so that a message may carry a
    {key} or {lang} placeholder of its own — the vote confirmations do."""
    langs = _load_content()
    template = langs.get(_lang, {}).get(_key)
    if template is None:
        template = langs.get(DEFAULT_LANG, {}).get(_key, _key)
    if not isinstance(template, str):
        return template
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def content_langs():
    """Languages the Olympiad has its own localization for, in display order."""
    have = _load_content()
    return [L for L in LANG_ORDER if L in have]

def contest_langs():
    """The language categories a contest may belong to: every localization, plus
    'int' for a cross-language contest."""
    return [L for L in LANG_ORDER if L in SUPPORTED_LANGS] + [INTERNATIONAL]

# ── The event window ────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*$")

def parse_date(raw):
    """'MM-DD-YYYY' → a UTC midnight datetime. Raises ValueError on bad input."""
    m = _DATE_RE.match(raw or "")
    if not m:
        raise ValueError("invalid_date")
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return datetime(year, month, day, tzinfo=timezone.utc)

def parse_period(start_raw, end_raw):
    """(start_ts, end_ts) for two 'MM-DD-YYYY' arguments. The closing day counts
    in full, so the period ends at 23:59:59 UTC of the end date. Raises
    ValueError('invalid_date') on unparsable input and ValueError('bad_order')
    when the end is before the start."""
    start = parse_date(start_raw)
    end = parse_date(end_raw) + timedelta(days=1) - timedelta(seconds=1)
    if end <= start:
        raise ValueError("bad_order")
    return int(start.timestamp()), int(end.timestamp())

def format_date(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%m-%d-%Y")

def period(now=None):
    """(start_ts, end_ts) of the Olympiad while it is still running, else None.
    A period whose end has passed counts as no Olympiad at all."""
    stored = db.get_olympiad()
    if not stored:
        return None
    start, end = stored
    if int(now or time.time()) > end:
        return None
    return start, end

def is_open(now=None):
    """Whether the Olympiad commands exist at all: true from the moment
    /setolympiad ran until the end of the closing day."""
    return period(now) is not None

def is_voting_open(now=None):
    """Whether people may cast a vote right now — inside the announced window.
    Bot Admins may prepare contests before it opens; voting waits."""
    p = period(now)
    if not p:
        return False
    start, end = p
    return start <= int(now or time.time()) <= end

def is_expired(now=None):
    """A stored Olympiad whose end has passed and whose data is due for
    deletion."""
    stored = db.get_olympiad()
    return bool(stored) and int(now or time.time()) > stored[1]

# ── Contests ────────────────────────────────────────────────────────────────

def contest_name(contest, lang):
    """A contest's name in `lang`. A single-language contest has exactly one
    name and shows it to everybody; an international one falls back to the
    default language and then to any name it has."""
    names = db.get_contest_names(contest["id"])
    if not names:
        return f"#{contest['id']}"
    return (names.get(lang) or names.get(DEFAULT_LANG)
            or names.get(contest["lang"]) or next(iter(names.values())))

def contest_label(contest, lang):
    """'🇷🇺 Name' — the flag makes the language category visible in the list."""
    flag = LANG_FLAG.get(contest["lang"], "")
    name = contest_name(contest, lang)
    return f"{flag} {name}".strip()

def _contest_group(contest, user_lang):
    """0 = the person's own language, 1 = international, 2 = everything else."""
    if contest["lang"] == user_lang:
        return 0
    if contest["lang"] == INTERNATIONAL:
        return 1
    return 2

def sort_contests(contests, user_lang):
    """Contests in the order they are offered to someone: their own language
    first, then the international ones, then the rest, alphabetical by the name
    they will actually see within each group."""
    return sorted(contests,
                  key=lambda c: (_contest_group(c, user_lang),
                                 contest_name(c, user_lang).casefold(),
                                 c["id"]))

def candidate_label(candidate, contest):
    """'Name (ru) — https://…' for an international contest, without the code
    for a single-language one."""
    parts = [candidate["name"]]
    if contest["lang"] == INTERNATIONAL and candidate["lang"]:
        parts.append(f"({candidate['lang']})")
    line = " ".join(parts)
    return f"{line} — {candidate['url']}" if candidate["url"] else line

# ── Reading the answers ─────────────────────────────────────────────────────

_NUMBER_RE = re.compile(r"\d+")

def parse_numbers(raw, count, limit=MAX_CHOICES):
    """The candidate numbers in a reply. Commas, semicolons and spaces all
    separate, and it is whole numbers that are read, not digits — '10, 2' is two
    candidates, not three. Returns the chosen numbers in the order given, without
    repeats, or None when the reply is empty, too long or out of range."""
    found = _NUMBER_RE.findall(raw or "")
    if not found:
        return None
    picked = []
    for token in found:
        n = int(token)
        if n < 1 or n > count:
            return None
        if n not in picked:
            picked.append(n)
    if not picked or len(picked) > limit:
        return None
    return picked

def new_key():
    """The short key that identifies a vote in the review chat."""
    return secrets.token_hex(4)

# ── Votes ───────────────────────────────────────────────────────────────────

def vote_candidates(vote):
    """The candidate rows a vote supports, skipping any that were deleted since
    it was cast."""
    try:
        ids = json.loads(vote["candidates"])
    except Exception:
        return []
    out = []
    for cid in ids:
        row = db.get_candidate(cid)
        if row:
            out.append(row)
    return out

def vote_candidate_names(vote):
    """'Wiki A, Wiki B' — the subtitle of the review embed and the line a
    rejected voter is reminded of."""
    names = [c["name"] for c in vote_candidates(vote)]
    return ", ".join(names)

def vote_body(vote, lang):
    """The text of a vote: what the person wrote, or the localized note that
    each of their wikis simply gets one point."""
    if vote["body"]:
        return vote["body"]
    return text("vote_one_point_body", lang)

def cleanup_expired(now=None):
    """Delete the finished Olympiad. Returns True when something was removed, so
    the caller knows to unregister the commands."""
    if not is_expired(now):
        return False
    db.clear_olympiad_data()
    db.clear_olympiad()
    logger.info("Olympiad period over: contests and votes deleted")
    return True

# ── The dialogs ─────────────────────────────────────────────────────────────
# The three conversations are the same on both messengers, so they live here
# rather than twice over in the bots. Each bot passes in its own `dialog`, an
# object with four coroutines:
#
#   send(text)                                  – say something
#   ask(prompt, validator=, error_text=,        – ask, and wait for a reply;
#       buttons=)                                 returns ('text', value),
#                                                 ('button', value) or None
#   choose(header, items, render, extra=)       – a numbered list, one pick
#   choose_numbers(header, items, render, n)    – a numbered list, several picks
#
# `ask` returning None means the person pressed Stop or stopped answering; every
# flow below simply returns when that happens, and the bot has already said so.

async def run_setcontest(dialog, lang):
    """Create a contest: its language category, its name (in every localization
    when it is international), and the two Discord chats its votes travel
    through."""
    codes = contest_langs()
    listed = ", ".join(codes)
    answer = await dialog.ask(
        text("setcontest_ask_lang", lang, langs=listed),
        validator=lambda v: v.strip().lower() if v.strip().lower() in codes else None,
        error_text=text("setcontest_bad_lang", lang, langs=listed))
    if not answer:
        return
    contest_lang = answer[1]

    if contest_lang == INTERNATIONAL:
        order = [L for L in LANG_ORDER if L in SUPPORTED_LANGS]
        order_names = ", ".join(language_name(L) for L in order)

        def _names(value):
            parts = [p.strip() for p in value.split(",")]
            return parts if len(parts) == len(order) and all(parts) else None

        answer = await dialog.ask(
            text("setcontest_ask_names_int", lang, order=order_names),
            validator=_names,
            error_text=text("setcontest_bad_names", lang,
                            count=len(order), order=order_names))
        if not answer:
            return
        names = dict(zip(order, answer[1]))
    else:
        answer = await dialog.ask(text("setcontest_ask_name", lang),
                                  validator=lambda v: v.strip() or None)
        if not answer:
            return
        names = {contest_lang: answer[1]}

    chats = []
    for key in ("setcontest_ask_review", "setcontest_ask_approved"):
        answer = await dialog.ask(text(key, lang), validator=_chat_id,
                                  error_text=text("setcontest_bad_chat", lang))
        if not answer:
            return
        chats.append(answer[1])

    contest_id = db.add_contest(contest_lang, chats[0], chats[1], dialog.author)
    for code, name in names.items():
        db.set_contest_name(contest_id, code, name)
    contest = db.get_contest(contest_id)
    await dialog.send(text("setcontest_created", lang,
                           name=contest_label(contest, lang)))

_CHAT_ID_RE = re.compile(r"^\d{5,25}$")

def _chat_id(value):
    v = (value or "").strip()
    return v if _CHAT_ID_RE.match(v) else None

async def _candidate_fields(dialog, lang, contest, current=None):
    """Ask for a candidate's name, its link and — in a cross-language contest —
    its language code. Returns (name, url, lang) or None."""
    answer = await dialog.ask(text("cand_ask_name", lang),
                              validator=lambda v: v.strip() or None)
    if not answer:
        return None
    name = answer[1]

    answer = await dialog.ask(text("cand_ask_url", lang),
                              validator=normalize_wiki_url,
                              error_text=text("cand_bad_url", lang))
    if not answer:
        return None
    url = answer[1]

    code = current["lang"] if current is not None else None
    if contest["lang"] == INTERNATIONAL:
        codes = [L for L in LANG_ORDER if L in SUPPORTED_LANGS]
        listed = ", ".join(codes)
        answer = await dialog.ask(
            text("cand_ask_lang", lang, langs=listed),
            validator=lambda v: v.strip().lower() if v.strip().lower() in codes else None,
            error_text=text("cand_bad_lang", lang, langs=listed))
        if not answer:
            return None
        code = answer[1]
    return name, url, code

async def run_editcontest(dialog, lang):
    """Pick a contest, then add a candidate wiki, or change or remove one that is
    already there."""
    contests = db.get_contests()
    if not contests:
        await dialog.send(text("editcontest_none", lang))
        return
    contest = await dialog.choose(text("editcontest_choose", lang),
                                  sort_contests(contests, lang),
                                  lambda c: contest_label(c, lang))
    if contest is None:
        return
    name = contest_name(contest, lang)

    candidates = db.get_candidates(contest["id"])
    header = (text("editcontest_candidates_header", lang, name=name) if candidates
              else text("editcontest_no_candidates", lang, name=name))
    chosen = await dialog.choose(header, candidates,
                                 lambda c: candidate_label(c, contest),
                                 extra=text("editcontest_add_option", lang))
    if chosen is None:
        return

    if chosen == "__extra__":
        fields = await _candidate_fields(dialog, lang, contest)
        if fields is None:
            return
        db.add_candidate(contest["id"], *fields)
        await dialog.send(text("cand_added", lang, name=fields[0], contest=name))
        return

    action = await dialog.choose(
        text("cand_menu", lang, name=chosen["name"]),
        [("edit", text("cand_opt_edit", lang)),
         ("delete", text("cand_opt_delete", lang))],
        lambda opt: opt[1])
    if action is None:
        return

    if action[0] == "delete":
        db.delete_candidate(chosen["id"])
        await dialog.send(text("cand_deleted", lang, name=chosen["name"], contest=name))
        return

    fields = await _candidate_fields(dialog, lang, contest, current=chosen)
    if fields is None:
        return
    db.update_candidate(chosen["id"], *fields)
    await dialog.send(text("cand_updated", lang, name=fields[0]))

async def _ask_account(dialog, lang, platform_name):
    """The person's Fandom username, or — behind the "Another way" button — a
    link to a profile elsewhere that shows their messenger handle. The reviewer
    is the one who checks it; the bot only carries the claim."""
    answer = await dialog.ask(
        text("ask_account", lang),
        validator=lambda v: v.strip() or None,
        buttons=[(text("other_way_button", lang), "other")])
    if not answer:
        return None
    if answer[0] == "button":
        answer = await dialog.ask(
            text("ask_account_other", lang, platform=platform_name),
            validator=lambda v: v.strip() or None)
        if not answer:
            return None
    return answer[1]

async def run_vote(dialog, lang, platform, user_id, username, platform_name,
                   post_vote):
    """The voting conversation, from the account question to the vote landing in
    its contest's review chat. `post_vote(key)` is the bot's own delivery
    coroutine; a vote that cannot be delivered is not kept."""
    known = db.known_fandom_name(platform, user_id)
    if known:
        await dialog.send(text("ask_account_known", lang, name=known))
        account_text, account_known = known, True
    else:
        account_text = await _ask_account(dialog, lang, platform_name)
        if account_text is None:
            return
        account_known = False

    answer = await dialog.ask(text("ask_activity", lang),
                              validator=lambda v: v.strip() or None)
    if not answer:
        return
    activity_text = answer[1]

    contests = db.get_contests()
    if not contests:
        await dialog.send(text("no_contests", lang))
        return
    contest = await dialog.choose(text("choose_contest", lang),
                                  sort_contests(contests, lang),
                                  lambda c: contest_label(c, lang))
    if contest is None:
        return
    name = contest_name(contest, lang)

    candidates = db.get_candidates(contest["id"])
    if not candidates:
        await dialog.send(text("no_candidates", lang, name=name))
        return

    previous = db.get_user_vote(contest["id"], platform, user_id)
    if previous:
        await dialog.send(text("replacing_vote", lang, name=name))

    numbers = await dialog.choose_numbers(
        text("choose_candidates", lang, max=MAX_CHOICES), candidates,
        lambda c: candidate_label(c, contest), MAX_CHOICES)
    if numbers is None:
        return
    picked = [candidates[n - 1] for n in numbers]

    answer = await dialog.ask(
        text("ask_vote", lang),
        validator=lambda v: v.strip() or None,
        buttons=[(text("one_point_button", lang), "one_point")])
    if not answer:
        return
    body = None if answer[0] == "button" else answer[1]

    key = new_key()
    db.add_vote(key, contest["id"], platform, user_id, username, lang,
                json.dumps([c["id"] for c in picked]), account_text, account_known,
                activity_text, body, bool(previous))
    if not await post_vote(key):
        db.delete_vote(key)
        await dialog.send(text("vote_no_channel", lang))
        return
    await dialog.send(text("vote_replaced" if previous else "vote_sent", lang, key=key))
