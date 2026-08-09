"""Quiz results, paused runs and the per-quiz privacy switch.

Results are stored only with the user's explicit consent and only two per
quiz per person (`QUIZ_HISTORY_LIMIT`): `prune_quiz_results` trims the rest,
and it runs again when two accounts are linked, because the pair then shares
one history. Every read goes through `_identity_filter(user_identities(...))`
rather than a bare (platform, user_id) match, which is what makes that shared
history work from either side.

`quiz_privacy` is the other person's veto: a row with no_compare blocks
/quizzes-compare against them, and a row with an empty quiz_id blocks it for
every quiz at once.

Not this module's zone: the quiz engine, its questions and its scoring
(src/quizzes.py, data in src/quizzes/), and the identity model itself
(db/users.py).
"""
import time

from db import conn, cur
from db.users import user_identities

QUIZ_HISTORY_LIMIT = 2
QUIZ_PROGRESS_TTL = 7 * 86400
QUIZ_RESULT_TTL = 10 * 365 * 86400

def _identity_filter(identities):
    """Build a `(platform=? AND user_id=?) OR …` clause plus its parameters
    from a `user_identities` list.

    Every read in this module goes through it instead of matching one
    (platform, user_id) pair, which is the whole mechanism behind a linked
    Discord+Telegram user seeing one shared history from either side. The
    clause is interpolated into the SQL, but only its *shape* is — the values
    stay parameters."""
    clause = " OR ".join("(platform=? AND user_id=?)" for _ in identities)
    params = [value for pair in identities for value in pair]
    return f"({clause})", params

def save_quiz_result(platform, user_id, quiz_id, lang, result_json, answers_json):
    """Store a completed quiz attempt and drop everything older than the two
    newest attempts of that quiz. Returns the new row id."""
    c = cur.execute(
        "INSERT INTO quiz_results"
        " (platform, user_id, quiz_id, lang, result_json, answers_json, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (platform, str(user_id), quiz_id, lang, result_json, answers_json, int(time.time()))
    )
    conn.commit()
    prune_quiz_results(platform, user_id, quiz_id)
    return c.lastrowid

def prune_quiz_results(platform, user_id, quiz_id=None):
    """Keep at most QUIZ_HISTORY_LIMIT attempts per quiz across the user's linked
    accounts. Called after every save and right after two accounts are linked."""
    where, params = _identity_filter(user_identities(platform, user_id))
    if quiz_id:
        quiz_ids = [quiz_id]
    else:
        quiz_ids = [r["quiz_id"] for r in cur.execute(
            f"SELECT DISTINCT quiz_id FROM quiz_results WHERE {where}", params
        ).fetchall()]
    for qid in quiz_ids:
        rows = cur.execute(
            f"SELECT id FROM quiz_results WHERE {where} AND quiz_id=?"
            " ORDER BY created_at DESC, id DESC",
            params + [qid]
        ).fetchall()
        for row in rows[QUIZ_HISTORY_LIMIT:]:
            cur.execute("DELETE FROM quiz_results WHERE id=?", (row["id"],))
    conn.commit()

def get_quiz_result(result_id):
    """One attempt by row id, or None once it was pruned or deleted. The only
    read here that is *not* identity-filtered — the id is already specific."""
    return cur.execute("SELECT * FROM quiz_results WHERE id=?", (result_id,)).fetchone()

def get_user_quiz_results(platform, user_id):
    """Every stored attempt of the user, oldest first."""
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} ORDER BY created_at, id", params
    ).fetchall()

def get_user_quiz_results_for(platform, user_id, quiz_id):
    """One quiz's stored attempts, oldest first — at most QUIZ_HISTORY_LIMIT of
    them, because saving prunes the rest."""
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} AND quiz_id=? ORDER BY created_at, id",
        params + [quiz_id]
    ).fetchall()

def get_latest_quiz_result(platform, user_id, quiz_id):
    """The newest attempt of one quiz, or None. This is what `/quizzes-compare`
    puts side by side, so the comparison is always between two people's most
    recent runs rather than their best ones."""
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} AND quiz_id=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        params + [quiz_id]
    ).fetchone()

def get_previous_quiz_result(platform, user_id, quiz_id):
    """The attempt right before the latest one, or None when there is only one."""
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} AND quiz_id=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1 OFFSET 1",
        params + [quiz_id]
    ).fetchone()

def get_last_quiz_result(platform, user_id):
    """The user's most recent attempt of any quiz — /quizzes-previous uses its
    timestamp to decide whether the no-argument form is still allowed."""
    where, params = _identity_filter(user_identities(platform, user_id))
    return cur.execute(
        f"SELECT * FROM quiz_results WHERE {where} ORDER BY created_at DESC, id DESC LIMIT 1",
        params
    ).fetchone()

def get_user_quiz_ids(platform, user_id):
    """Distinct quiz ids the user has any stored result for, in first-taken order."""
    where, params = _identity_filter(user_identities(platform, user_id))
    rows = cur.execute(
        f"SELECT quiz_id, MIN(created_at) AS first FROM quiz_results WHERE {where}"
        " GROUP BY quiz_id ORDER BY first", params
    ).fetchall()
    return [r["quiz_id"] for r in rows]

def delete_quiz_result(result_id):
    """Erase one attempt by id — the single-result branch of `/privacy`."""
    cur.execute("DELETE FROM quiz_results WHERE id=?", (result_id,))
    conn.commit()

def delete_user_quiz_results(platform, user_id, quiz_id=None):
    """Erase the user's results — all of them, or one quiz's — on every linked
    account. Returns how many rows were removed."""
    where, params = _identity_filter(user_identities(platform, user_id))
    sql = f"DELETE FROM quiz_results WHERE {where}"
    if quiz_id:
        sql += " AND quiz_id=?"
        params = params + [quiz_id]
    c = cur.execute(sql, params)
    conn.commit()
    return c.rowcount

def set_quiz_compare_blocked(platform, user_id, quiz_id, blocked):
    """Forbid or re-allow other users comparing themselves against this user.
    `quiz_id` of '' covers every quiz."""
    cur.execute(
        "INSERT INTO quiz_privacy (platform, user_id, quiz_id, no_compare, updated_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(platform, user_id, quiz_id) DO UPDATE SET"
        " no_compare=excluded.no_compare, updated_at=excluded.updated_at",
        (platform, str(user_id), quiz_id or "", 1 if blocked else 0, int(time.time()))
    )
    conn.commit()

def is_quiz_compare_blocked(platform, user_id, quiz_id=None):
    """True when the user forbade comparison — globally, or for `quiz_id`. The
    setting is honoured across their linked accounts."""
    where, params = _identity_filter(user_identities(platform, user_id))
    keys = [""] if quiz_id is None else ["", quiz_id]
    placeholders = ",".join("?" for _ in keys)
    return cur.execute(
        f"SELECT 1 FROM quiz_privacy WHERE {where} AND quiz_id IN ({placeholders})"
        " AND no_compare=1 LIMIT 1",
        params + keys
    ).fetchone() is not None

def get_blocked_quiz_ids(platform, user_id):
    """The quiz ids this person blocked individually, as a set.

    Deliberately excludes the '' row that blocks everything: `/privacy` shows
    the global switch separately, and mixing the two would make the list look
    like a per-quiz choice the person never made."""
    where, params = _identity_filter(user_identities(platform, user_id))
    rows = cur.execute(
        f"SELECT DISTINCT quiz_id FROM quiz_privacy WHERE {where} AND no_compare=1"
        " AND quiz_id<>''", params
    ).fetchall()
    return {r["quiz_id"] for r in rows}

def save_quiz_progress(platform, user_id, quiz_id, lang, qindex, orders_json, answers_json):
    """Park an unfinished quiz so /quizzes can pick it up again within a week."""
    cur.execute(
        "INSERT INTO quiz_progress"
        " (platform, user_id, quiz_id, lang, qindex, orders_json, answers_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(platform, user_id, quiz_id) DO UPDATE SET"
        " lang=excluded.lang, qindex=excluded.qindex, orders_json=excluded.orders_json,"
        " answers_json=excluded.answers_json, created_at=excluded.created_at",
        (platform, str(user_id), quiz_id, lang, int(qindex), orders_json, answers_json,
         int(time.time()))
    )
    conn.commit()

def get_quiz_progress(platform, user_id, quiz_id, max_age_seconds=QUIZ_PROGRESS_TTL):
    """A paused run, or None when there is none or it has expired.

    Not identity-filtered, unlike the results: progress belongs to the account
    that paused it, because the shuffled option order stored with it was sent
    to that one chat. Resuming on the other platform starts fresh."""
    cutoff = int(time.time()) - max_age_seconds
    return cur.execute(
        "SELECT * FROM quiz_progress WHERE platform=? AND user_id=? AND quiz_id=?"
        " AND created_at>=?",
        (platform, str(user_id), quiz_id, cutoff)
    ).fetchone()

def delete_quiz_progress(platform, user_id, quiz_id):
    """Drop a paused run — on finishing it, and on abandoning it."""
    cur.execute(
        "DELETE FROM quiz_progress WHERE platform=? AND user_id=? AND quiz_id=?",
        (platform, str(user_id), quiz_id)
    )
    conn.commit()

def cleanup_old_quiz_progress(max_age_seconds=QUIZ_PROGRESS_TTL):
    """Sweep paused runs older than a week. The reads enforce the same window
    themselves, so an unswept row is already unreachable."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute("DELETE FROM quiz_progress WHERE created_at<?", (cutoff,))
    conn.commit()

def cleanup_old_quiz_results(max_age_seconds=QUIZ_RESULT_TTL):
    """A stored quiz result is never kept longer than ten years, whatever the
    user consented to."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute("DELETE FROM quiz_results WHERE created_at<?", (cutoff,))
    conn.commit()
