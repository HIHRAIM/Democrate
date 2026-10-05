"""What a server produced, what it consumed, what it was asked to make, and
where to post about it.

`server_production` gets one row per finished batch and is the input to three
different readers: the weekly statistics, the monthly task's evaluation and
next month's task generation. `server_consumption` gets one row per server per
day per category — what was needed, what covered it and at what quality —
keyed by the date, so a tick that somehow runs twice cannot count one meal
into the week twice. Provision reads those rows back rather than recomputing
anything, which is why both tables are kept about ten weeks.
`neutral_activity` keeps one row per unaffiliated earner/community/UTC day for
eight days. The daily meal reads those counts and writes the consumed virtual
base units into `server_consumption`; neutral output never enters production,
inventory, GDP or currency backing.

`chat_channels` holds the /setlogs and /settasks bindings together with the
weekday, time and UTC offset each server chose; `last_marker` on the row is
what makes a scheduled post fire once — main.py: _due_channel_posts compares
it instead of trusting the minute it woke up in.

Not this module's zone: the numbers themselves (economy/consumption.py,
economy/tasks.py) and their rendering (stats.py).
"""
from db import conn, cur, _now

def record_server_production(platform, server_id, good_code, category, producer_type,
                             producer, qty, value, quality_level):
    """One production event attributed to a server. `producer` is the worker's
    canonical (platform, user_id) — the basis for the active-population count in
    the provision formula."""
    if platform is None or server_id is None:
        return
    pp, pi = producer if producer else (None, None)
    cur.execute(
        "INSERT INTO server_production (platform, server_id, good_code, category,"
        " producer_type, producer_platform, producer_id, qty, value, quality_level, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (platform, str(server_id), good_code, category, producer_type,
         pp, str(pi) if pi is not None else None,
         int(qty), int(value), int(quality_level), _now()))
    conn.commit()

def server_category_production(platform, server_id, since_ts, until_ts=None):
    """{category: [(good_code, qty, quality_level, is_base_qty_weighted rows)]} —
    raw rows of the window, grouped by category in Python by the caller."""
    until_ts = until_ts or _now()
    return cur.execute(
        "SELECT good_code, category, qty, value, quality_level FROM server_production"
        " WHERE platform=? AND server_id=? AND created_at>=? AND created_at<=?",
        (platform, str(server_id), int(since_ts), int(until_ts))).fetchall()

def server_top_goods(platform, server_id, since_ts, until_ts=None, limit=5):
    """The server's biggest goods of a window, by total value then quantity.

    Two readers with different needs: the weekly statistics take the default
    handful, while `generate_monthly_task` asks for more than it wants and
    filters the base goods out afterwards — base goods carry value 0 and would
    otherwise crowd out nothing, but they must not become optional task
    items."""
    until_ts = until_ts or _now()
    return cur.execute(
        "SELECT good_code, SUM(qty) AS qty, SUM(value) AS value FROM server_production"
        " WHERE platform=? AND server_id=? AND created_at>=? AND created_at<=?"
        " GROUP BY good_code ORDER BY value DESC, qty DESC LIMIT ?",
        (platform, str(server_id), int(since_ts), int(until_ts), limit)).fetchall()

def server_active_producers(platform, server_id, since_ts, until_ts=None):
    """How many distinct people produced anything on the server in the window —
    the provision formula's population."""
    until_ts = until_ts or _now()
    row = cur.execute(
        "SELECT COUNT(DISTINCT producer_platform || '|' || producer_id) AS c"
        " FROM server_production WHERE platform=? AND server_id=?"
        " AND producer_id IS NOT NULL AND created_at>=? AND created_at<=?",
        (platform, str(server_id), int(since_ts), int(until_ts))).fetchone()
    return row["c"] or 0

def record_neutral_activity(platform, server_id, user_platform, user_id, now=None):
    """Count one accepted message in a bounded person/day row, never its text."""
    import time

    now = int(now or _now())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    cur.execute(
        "INSERT INTO neutral_activity (platform, server_id, day, user_platform,"
        " user_id, messages, updated_at) VALUES (?,?,?,?,?,1,?)"
        " ON CONFLICT(platform, server_id, day, user_platform, user_id) DO UPDATE SET"
        " messages=neutral_activity.messages+1, updated_at=excluded.updated_at",
        (platform, str(server_id), day, user_platform, str(user_id), now))
    conn.commit()

def neutral_message_count(platform, server_id, day):
    """Accepted non-worker messages of one UTC day for local base supply."""
    row = cur.execute(
        "SELECT COALESCE(SUM(messages),0) AS n FROM neutral_activity"
        " WHERE platform=? AND server_id=? AND day=?",
        (platform, str(server_id), day)).fetchone()
    return int(row["n"] or 0)

def server_active_population(platform, server_id, since_ts, until_ts=None):
    """Distinct producers and unaffiliated earners, linked accounts counted once."""
    until_ts = int(until_ts or _now())
    row = cur.execute(
        "SELECT COUNT(*) AS n FROM ("
        " SELECT producer_platform AS p, producer_id AS u FROM server_production"
        " WHERE platform=? AND server_id=? AND producer_id IS NOT NULL"
        " AND created_at>=? AND created_at<?"
        " UNION SELECT user_platform AS p, user_id AS u FROM neutral_activity"
        " WHERE platform=? AND server_id=? AND updated_at>=? AND updated_at<?)",
        (platform, str(server_id), int(since_ts), until_ts,
         platform, str(server_id), int(since_ts), until_ts)).fetchone()
    return int(row["n"] or 0)

def server_good_qty(platform, server_id, good_code, since_ts, until_ts=None):
    """Units of one good produced on the server in a window, 0 when none —
    the per-good half of the monthly task's progress."""
    until_ts = until_ts or _now()
    row = cur.execute(
        "SELECT COALESCE(SUM(qty),0) AS q FROM server_production"
        " WHERE platform=? AND server_id=? AND good_code=? AND created_at>=? AND created_at<=?",
        (platform, str(server_id), good_code, int(since_ts), int(until_ts))).fetchone()
    return row["q"] or 0

def record_server_consumption(platform, server_id, day, category, population,
                              need, consumed, satisfaction, base_only,
                              neutral_units=0):
    """One day's demand and what covered it. Keyed by the day, so a tick that
    runs twice for the same date overwrites its row instead of counting the
    meal twice into the week."""
    cur.execute(
        "INSERT INTO server_consumption (platform, server_id, day, category,"
        " population, need, consumed, satisfaction, base_only, neutral_units, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(platform, server_id, day, category) DO UPDATE SET"
        " population=excluded.population, need=excluded.need,"
        " consumed=excluded.consumed, satisfaction=excluded.satisfaction,"
        " base_only=excluded.base_only, neutral_units=excluded.neutral_units,"
        " created_at=excluded.created_at",
        (platform, str(server_id), day, category, int(population), float(need),
         int(consumed), float(satisfaction), 1 if base_only else 0,
         float(neutral_units), _now()))
    conn.commit()

def server_consumption_window(platform, server_id, since_ts, until_ts=None):
    """The consumption rows of a time window — what the provision score reads."""
    until_ts = until_ts or _now()
    return cur.execute(
        "SELECT category, population, need, consumed, satisfaction, base_only, neutral_units"
        " FROM server_consumption WHERE platform=? AND server_id=?"
        " AND created_at>=? AND created_at<=?",
        (platform, str(server_id), int(since_ts), int(until_ts))).fetchall()

def enterprise_category_stock(code, category):
    """An enterprise's stock in one base category, best goods first: market
    goods (ordered by base value) ahead of the base goods behind them, so the
    population eats the good stuff first and base production is the filler.
    Base goods are the rows with no bank_code."""
    return cur.execute(
        "SELECT i.good_code, i.qty FROM inventory i JOIN goods g ON g.code=i.good_code"
        " WHERE i.owner_type='enterprise' AND i.owner_id=? AND i.qty>0"
        " AND g.category=?"
        " ORDER BY (g.bank_code IS NULL) ASC, g.base_value DESC, i.good_code",
        (str(code), category)).fetchall()

def cleanup_old_server_consumption(max_age_seconds=70 * 86400):
    """Kept as long as the production statistics, for the same reason: the
    weekly report never looks further back than that."""
    cur.execute("DELETE FROM server_consumption WHERE created_at < ?",
                (_now() - int(max_age_seconds),))
    conn.commit()

def cleanup_old_neutral_activity(max_age_seconds=8 * 86400):
    """Only the active-day calculation and seven-day population report need rows."""
    cur.execute("DELETE FROM neutral_activity WHERE updated_at<?",
                (_now() - int(max_age_seconds),))
    conn.commit()

def cleanup_old_server_production(max_age_seconds=70 * 86400):
    """Production statistics are kept ~10 weeks: enough for the weekly stats,
    the monthly task evaluation and next month's task generation."""
    cutoff = _now() - max_age_seconds
    cur.execute("DELETE FROM server_production WHERE created_at<?", (cutoff,))
    conn.commit()

def set_chat_channel(platform, server_id, kind, channel_key, weekday=None, hour=None,
                     minute=None, tz_offset=None):
    """Bind the server's 'logs' or 'tasks' channel; schedule fields keep their
    previous (or default) values when passed as None."""
    cur.execute(
        "INSERT INTO chat_channels (platform, server_id, kind, channel_key, weekday,"
        " hour, minute, tz_offset, last_marker, created_at)"
        " VALUES (?,?,?,?,COALESCE(?,0),COALESCE(?,12),COALESCE(?,0),COALESCE(?,0),NULL,?)"
        " ON CONFLICT(platform, server_id, kind) DO UPDATE SET"
        " channel_key=excluded.channel_key,"
        " weekday=COALESCE(?, chat_channels.weekday),"
        " hour=COALESCE(?, chat_channels.hour),"
        " minute=COALESCE(?, chat_channels.minute),"
        " tz_offset=COALESCE(?, chat_channels.tz_offset)",
        (platform, str(server_id), kind, str(channel_key), weekday, hour, minute, tz_offset,
         _now(), weekday, hour, minute, tz_offset))
    conn.commit()

def get_chat_channel(platform, server_id, kind):
    """A server's 'logs' or 'tasks' binding, or None when it has none. Read to
    show the current schedule before changing part of it."""
    return cur.execute(
        "SELECT * FROM chat_channels WHERE platform=? AND server_id=? AND kind=?",
        (platform, str(server_id), kind)).fetchone()

def remove_chat_channel(platform, server_id, kind):
    """Unbind a scheduled-post channel (`/setlogs off`). Returns whether there
    was one, so the command can tell "unbound" from "there was nothing"."""
    c = cur.execute(
        "DELETE FROM chat_channels WHERE platform=? AND server_id=? AND kind=?",
        (platform, str(server_id), kind))
    conn.commit()
    return c.rowcount > 0

def all_chat_channels(kind=None):
    """Every scheduled-post binding, or only one kind.

    Both forms are used: main.py: _due_channel_posts walks all of them once a
    minute, while the FX broadcast after a daily tick asks for 'logs' alone."""
    if kind is None:
        return cur.execute("SELECT * FROM chat_channels").fetchall()
    return cur.execute("SELECT * FROM chat_channels WHERE kind=?", (kind,)).fetchall()

def set_chat_channel_marker(platform, server_id, kind, marker):
    """Record which period this channel has already been posted for.

    The marker is a period *label* — '2026-W32' for weekly statistics,
    '2026-08' for a monthly task — and comparing it is what makes a scheduled
    post fire exactly once: a bot that slept through the minute posts when it
    wakes, and a bot restarted twice in an hour does not post twice. It is
    written after the attempt either way, so a chat the bot cannot reach does
    not block the following weeks."""
    cur.execute(
        "UPDATE chat_channels SET last_marker=? WHERE platform=? AND server_id=? AND kind=?",
        (marker, platform, str(server_id), kind))
    conn.commit()

def save_server_task(platform, server_id, month, spec_json):
    """Store the generated task for one server and month.

    `completed` is left NULL on insert and deliberately not reset on conflict:
    a month already judged keeps its verdict even if the task is regenerated,
    because next month's provision bonus depends on it."""
    cur.execute(
        "INSERT INTO server_tasks (platform, server_id, month, spec_json, completed, created_at)"
        " VALUES (?,?,?,?,NULL,?)"
        " ON CONFLICT(platform, server_id, month) DO UPDATE SET spec_json=excluded.spec_json",
        (platform, str(server_id), month, spec_json, _now()))
    conn.commit()

def get_server_task(platform, server_id, month):
    """One month's task row, or None when the server had none.

    `completed` is a three-valued field and callers must treat it that way:
    NULL means the month is not judged yet, 0 failed, 1 completed — and only
    the last one grants the provision bonus."""
    return cur.execute(
        "SELECT * FROM server_tasks WHERE platform=? AND server_id=? AND month=?",
        (platform, str(server_id), month)).fetchone()

def get_unevaluated_tasks(before_month):
    """Tasks of past months whose completion has not been judged yet."""
    return cur.execute(
        "SELECT * FROM server_tasks WHERE completed IS NULL AND month<?",
        (before_month,)).fetchall()

def set_server_task_completed(task_id, completed):
    """Write the verdict, turning NULL into 0 or 1 and taking the row out of
    `get_unevaluated_tasks` for good."""
    cur.execute("UPDATE server_tasks SET completed=? WHERE id=?",
                (1 if completed else 0, task_id))
    conn.commit()

def cleanup_old_server_tasks(max_age_seconds=70 * 86400):
    """Keep the current and preceding monthly task through a delayed verdict.

    Only those two months are read for progress and the provision bonus; older
    task rows are no longer needed for calculations.
    """
    cutoff = _now() - max_age_seconds
    cur.execute("DELETE FROM server_tasks WHERE created_at<?", (cutoff,))
    conn.commit()
