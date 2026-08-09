"""Bot-wide key/value settings, the language of every chat, and the
localization suggestions waiting for an answer.

`bot_settings` is the deployment's own scratchpad: the last-run markers of the
economy ticks and the moment the setup-deadline rule came into force live
there, which is how a restart neither skips a period nor runs one twice.

Chat languages resolve from the most specific key outwards — the exact
channel/thread/topic set by /locallang, then the bare server or group id set
by /lang or /setup, then the legacy '<group_id>:0' key — and return None when
nothing matches, leaving the default to the caller. A row marked `is_dm` is
somebody's private conversation with the bot and is dropped after a year;
a community's choice is kept as long as the bot is in it.

Not this module's zone: unions and the chats bound to them (db/unions.py),
admin grants (db/admins.py), the per-server log and task channels
(db/serverstats.py).
"""
import time

from db import conn, cur

def get_setting(key, default=None):
    """One bot-wide setting as a string, or `default` when it was never
    written.

    Values are always TEXT: callers that store a number compare it as a string
    (`econ_last_daily` against '2026-08-08', `econ_last_weekly` against
    '2026-W32'), which is exactly why the tick markers are period *labels* and
    not timestamps — comparing them cannot drift."""
    row = cur.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    """Write one bot-wide setting, stringifying whatever it is given.

    Used for the economy tick markers, the moment the setup-deadline rule came
    into force, and the Olympiad's dates. There is no delete: a key that stops
    mattering is simply never read again."""
    cur.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    conn.commit()

def set_chat_lang(chat_id, lang_code, is_dm=False):
    """Remember a chat's language. `is_dm` marks a private conversation with the
    bot; those rows are dropped again after a year (see cleanup_old_dm_langs),
    while a server's choice is kept for as long as the bot is in the server."""
    cur.execute(
        "INSERT INTO chat_settings (chat_id, lang, updated_at, is_dm) VALUES (?,?,?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET lang=excluded.lang,"
        " updated_at=excluded.updated_at, is_dm=MAX(chat_settings.is_dm, excluded.is_dm)",
        (chat_id, lang_code, int(time.time()), 1 if is_dm else 0)
    )
    conn.commit()

def cleanup_old_dm_langs(max_age_seconds=365 * 24 * 3600):
    """The language a person picked in their private chat with the bot is kept
    at most a year after they last set it."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute(
        "DELETE FROM chat_settings WHERE is_dm=1 AND updated_at IS NOT NULL"
        " AND updated_at < ?",
        (cutoff,)
    )
    conn.commit()

def get_chat_lang(chat_id):
    """
    Lookup language for given chat_id. Resolution order:
      1. exact chat_id (channel/thread/topic — set with /locallang),
      2. bare community prefix (guild id / group chat id — set with /lang or /setup),
      3. legacy '<group_id>:0' key (group-wide behavior).
    If not found → returns None.
    """
    row = cur.execute(
        "SELECT lang FROM chat_settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if row and row["lang"]:
        return row["lang"]

    if ":" in chat_id:
        prefix = chat_id.split(":", 1)[0]
        for fallback_key in (prefix, f"{prefix}:0"):
            row = cur.execute(
                "SELECT lang FROM chat_settings WHERE chat_id=?",
                (fallback_key,)
            ).fetchone()
            if row and row["lang"]:
                return row["lang"]

    return None

def add_loc_suggestion(code, platform, user_id, username, lang, rkey, suggestion, ui_lang):
    """Store one `/loc-suggest` under a short random code.

    The row exists so that `/loc-reply <code>` can answer the person later, on
    whichever platform they wrote from — hence platform and user_id — and in
    the language they were using when they wrote (`ui_lang`), which is not
    necessarily `lang`, the language their suggestion is *about*. INSERT OR
    REPLACE because the code is drawn fresh per suggestion; a collision would
    overwrite a stale one rather than fail."""
    cur.execute(
        "INSERT OR REPLACE INTO loc_suggestions "
        "(code, platform, user_id, username, lang, rkey, suggestion, ui_lang, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (code, platform, str(user_id), username, lang, rkey, suggestion, ui_lang, int(time.time()))
    )
    conn.commit()

def get_loc_suggestion(code):
    """The suggestion behind a code, or None once it was answered or expired —
    `/loc-reply` must check, because the code is typed by hand from a message
    that may be a year old."""
    return cur.execute(
        "SELECT * FROM loc_suggestions WHERE code=?",
        (code,)
    ).fetchone()

def delete_loc_suggestion(code):
    """Drop a suggestion once it has been answered. Only `/loc-reply` calls
    this: an unanswered suggestion waits for the yearly sweep instead."""
    cur.execute("DELETE FROM loc_suggestions WHERE code=?", (code,))
    conn.commit()

def cleanup_old_loc_suggestions(max_age_seconds=365 * 24 * 3600):
    """Localization-suggestion dialog codes are kept at most a year."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute(
        "DELETE FROM loc_suggestions WHERE created_at IS NOT NULL AND created_at < ?",
        (cutoff,)
    )
    conn.commit()
