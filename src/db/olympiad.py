"""The Olympiad's storage: the event itself, its contests, their candidate
wikis, the votes cast, and the wiki accounts reviewers chose to remember.

The wiki Olympiad is a bounded event: /setolympiad opens it, contests and
their candidate wikis are set up by Bot Admins, people vote from their private
chat with the bot, and every trace of the contests and the votes is deleted
the moment the voting period is over (`clear_olympiad_data`). Only two things
outlive it — the language someone picked in their DM (db/settings.py) and the
wiki account a reviewer chose to remember here — and both expire after a year
of their own.

A contest belongs to one localization or to `int` for a cross-language one,
and carries the ids of two Discord chats: where votes go for review and where
accepted votes are published. Those are always Discord chats even for votes
cast on Telegram. One person holds one vote per contest, so voting again
replaces the earlier row and sends it back for review marked as an update.

Not this module's zone: the schema of these tables (db/schema.py:
init_olympiad), the event's rules and dialogs (olympiad.py), and the review
embeds and verdict commands (discord_bot/olympiad.py).
"""
import time

from db import conn, cur
from db.settings import get_setting, set_setting
from db.users import get_fandom_verification, get_link_by_telegram

def get_olympiad():
    """(start_ts, end_ts) of the open Olympiad, or None when none is set."""
    start, end = get_setting("olympiad_start"), get_setting("olympiad_end")
    if start is None or end is None:
        return None
    try:
        return int(start), int(end)
    except (TypeError, ValueError):
        return None

def set_olympiad(start_ts, end_ts):
    """Open an Olympiad over a voting period, as two unix timestamps.

    Stored in `bot_settings` rather than in a table of its own, because there is
    at most one Olympiad at a time and the pair is really a deployment-wide
    setting. Everything else about the event hangs off these two numbers."""
    set_setting("olympiad_start", int(start_ts))
    set_setting("olympiad_end", int(end_ts))

def clear_olympiad():
    """Close the Olympiad by forgetting its dates.

    Only the dates: the contests, candidates and votes are dropped separately by
    `clear_olympiad_data`, and the two are always called together. Splitting them
    means a cancelled event cannot leave votes behind that nothing can reach."""
    cur.execute("DELETE FROM bot_settings WHERE key IN ('olympiad_start','olympiad_end')")
    conn.commit()

def clear_olympiad_data():
    """Wipe the contests, their candidates and every vote — what happens when
    the voting period ends and when an Olympiad is cancelled."""
    cur.executescript(
        "DELETE FROM olympiad_votes;"
        "DELETE FROM olympiad_candidates;"
        "DELETE FROM olympiad_contest_names;"
        "DELETE FROM olympiad_contests;"
    )
    conn.commit()

def add_contest(lang, review_chat, approved_chat, created_by=None):
    """Create a contest and return its id.

    `lang` is one of the six localizations, or 'int' for a cross-language contest
    that is named in all of them. `review_chat` and `approved_chat` are Discord
    channel ids even for votes cast on Telegram — the review always happens on
    Discord."""
    cur.execute(
        "INSERT INTO olympiad_contests (lang, review_chat, approved_chat, created_by,"
        " created_at) VALUES (?,?,?,?,?)",
        (lang, str(review_chat), str(approved_chat), created_by, int(time.time()))
    )
    conn.commit()
    return cur.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def set_contest_name(contest_id, lang, name):
    """Name a contest in one language, replacing any earlier name.

    A contest in a single language needs one row; an 'int' contest needs one per
    localization, which is why the name lives in its own table rather than on the
    contest row."""
    cur.execute(
        "INSERT INTO olympiad_contest_names (contest_id, lang, name) VALUES (?,?,?)"
        " ON CONFLICT(contest_id, lang) DO UPDATE SET name=excluded.name",
        (int(contest_id), lang, name)
    )
    conn.commit()

def set_approved_chat_for_lang(contest_id, lang, chat):
    """Route a contest's *approved* votes cast in `lang` to a specific chat,
    overriding the contest's single approved_chat. Pass chat=None to clear the
    override so that language falls back to approved_chat again."""
    if chat is None:
        cur.execute(
            "DELETE FROM olympiad_contest_approved_langs WHERE contest_id=? AND lang=?",
            (int(contest_id), lang))
    else:
        cur.execute(
            "INSERT INTO olympiad_contest_approved_langs (contest_id, lang, chat)"
            " VALUES (?,?,?)"
            " ON CONFLICT(contest_id, lang) DO UPDATE SET chat=excluded.chat",
            (int(contest_id), lang, str(chat)))
    conn.commit()

def approved_chat_for(contest, lang):
    """The chat an approved vote should be posted to: a per-language override
    when one is set for this contest, otherwise the contest's approved_chat."""
    if lang:
        row = cur.execute(
            "SELECT chat FROM olympiad_contest_approved_langs"
            " WHERE contest_id=? AND lang=?",
            (int(contest["id"]), lang)).fetchone()
        if row and row["chat"]:
            return row["chat"]
    return contest["approved_chat"]

def get_contests():
    """Every contest of the current Olympiad, oldest first — the list the
    voting dialog numbers."""
    return cur.execute("SELECT * FROM olympiad_contests ORDER BY id").fetchall()

def get_contest(contest_id):
    """One contest by id, or None once the Olympiad has been cleared."""
    return cur.execute("SELECT * FROM olympiad_contests WHERE id=?",
                       (int(contest_id),)).fetchone()

def get_contest_names(contest_id):
    """A contest's names as {lang: name}. Empty for a contest nobody named,
    which the label helpers fall back from."""
    rows = cur.execute(
        "SELECT lang, name FROM olympiad_contest_names WHERE contest_id=?",
        (int(contest_id),)).fetchall()
    return {r["lang"]: r["name"] for r in rows}

def add_candidate(contest_id, name, url, lang=None):
    """Add a candidate wiki to a contest and return its id.

    `lang` is meaningful only in an 'int' contest, where it says which language
    the wiki belongs to; in a single-language contest it stays None."""
    cur.execute(
        "INSERT INTO olympiad_candidates (contest_id, name, url, lang, created_at)"
        " VALUES (?,?,?,?,?)",
        (int(contest_id), name, url, lang, int(time.time()))
    )
    conn.commit()
    return cur.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def get_candidates(contest_id):
    """A contest's candidate wikis in the order they were added — the order
    the voting dialog numbers them in, so the numbers a voter types stay stable
    for as long as nobody edits the list."""
    return cur.execute(
        "SELECT * FROM olympiad_candidates WHERE contest_id=? ORDER BY id",
        (int(contest_id),)).fetchall()

def get_candidate(candidate_id):
    """One candidate by id, or None."""
    return cur.execute("SELECT * FROM olympiad_candidates WHERE id=?",
                       (int(candidate_id),)).fetchone()

def update_candidate(candidate_id, name, url, lang=None):
    """Overwrite a candidate's name, url and language.

    Editing rather than delete-and-re-add on purpose: a candidate keeps its id, so
    the numbers already shown to voters do not shift under them."""
    cur.execute(
        "UPDATE olympiad_candidates SET name=?, url=?, lang=? WHERE id=?",
        (name, url, lang, int(candidate_id)))
    conn.commit()

def delete_candidate(candidate_id):
    """Remove a candidate. Votes already cast for it keep its id in their
    stored list and simply resolve to nothing — the reviewer sees the gap."""
    cur.execute("DELETE FROM olympiad_candidates WHERE id=?", (int(candidate_id),))
    conn.commit()

def add_vote(key, contest_id, platform, user_id, username, lang, candidates_json,
             account_text, account_known, activity_text, body, is_update):
    """Store a vote awaiting review. A person has one vote per contest: casting
    again replaces the earlier one (which is then marked as an update)."""
    cur.execute(
        "DELETE FROM olympiad_votes WHERE contest_id=? AND platform=? AND user_id=?",
        (int(contest_id), platform, str(user_id)))
    cur.execute(
        "INSERT INTO olympiad_votes (key, contest_id, platform, user_id, username,"
        " lang, candidates, account_text, account_known, activity_text, body,"
        " is_update, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, int(contest_id), platform, str(user_id), username, lang,
         candidates_json, account_text, 1 if account_known else 0, activity_text,
         body, 1 if is_update else 0, int(time.time()))
    )
    conn.commit()

def get_vote(key):
    """One vote by the short key printed in its review embed, or None. The key
    is what a reviewer types into `/accepted` and `/denied`, so it is a lookup by
    something typed by hand."""
    return cur.execute("SELECT * FROM olympiad_votes WHERE key=?", (key,)).fetchone()

def get_user_vote(contest_id, platform, user_id):
    """This person's existing vote in a contest, or None. Read before casting
    so the dialog can say the new one replaces it."""
    return cur.execute(
        "SELECT * FROM olympiad_votes WHERE contest_id=? AND platform=? AND user_id=?",
        (int(contest_id), platform, str(user_id))).fetchone()

def delete_vote(key):
    """Remove a vote once it has been accepted or rejected — the review queue
    is the table itself, so an answered vote leaves it."""
    cur.execute("DELETE FROM olympiad_votes WHERE key=?", (key,))
    conn.commit()

def set_wiki_account(platform, user_id, fandom_name, linked_by=None):
    """Remember which wiki account a messenger account belongs to.

    Written by `/accepted … remember` and by nothing else, so that a voter is not
    asked again next time. One of only two things that outlive an Olympiad, and it
    expires after a year of its own."""
    cur.execute(
        "INSERT INTO wiki_accounts (platform, user_id, fandom_name, linked_by, linked_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(platform, user_id) DO UPDATE SET"
        " fandom_name=excluded.fandom_name, linked_by=excluded.linked_by,"
        " linked_at=excluded.linked_at",
        (platform, str(user_id), fandom_name, linked_by, int(time.time()))
    )
    conn.commit()

def get_wiki_account(platform, user_id):
    """The remembered wiki account of a messenger account, or None."""
    return cur.execute(
        "SELECT * FROM wiki_accounts WHERE platform=? AND user_id=?",
        (platform, str(user_id))).fetchone()

def cleanup_old_wiki_accounts(max_age_seconds=365 * 24 * 3600):
    """A reviewer-remembered wiki account is kept at most a year."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute(
        "DELETE FROM wiki_accounts WHERE linked_at IS NOT NULL AND linked_at < ?",
        (cutoff,))
    conn.commit()

def known_fandom_name(platform, user_id):
    """The Fandom username the bot already knows for someone: from /verify (their
    own Discord account, or the Discord account their Telegram is linked to),
    otherwise from a reviewer's /accepted … remember. None when unknown."""
    uid = str(user_id)
    discord_id = uid if platform == "discord" else None
    if platform == "telegram":
        link = get_link_by_telegram(uid)
        if link and link["discord_id"]:
            discord_id = str(link["discord_id"])
    if discord_id:
        row = get_fandom_verification(discord_id)
        if row and row["fandom_name"]:
            return row["fandom_name"]
    row = get_wiki_account(platform, uid)
    return row["fandom_name"] if row and row["fandom_name"] else None

