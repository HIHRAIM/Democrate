"""Wiki anniversaries: which wiki, in which channel, at which minute.

One row per (server, wiki url) — the unique index is what makes /wiki-founday
idempotent. `last_sent_year` is the whole yearly cycle: the founday loop
compares it rather than trusting a timer, so a bot that was down at the
scheduled minute still greets the wiki when it comes back that day, and a bot
restarted three times greets it once.

Not this module's zone: the posting itself, its webhook and its wording
(discord_bot/commands/foundays.py, utils.py: founday_message).
"""
import time

from db import conn, cur

def add_wiki_founday(guild_id, channel_id, url, name, founded_at, hour, minute, lang, created_by):
    """Register (or re-register) a wiki's anniversary announcement. A guild may
    hold one entry per wiki url; running the command again updates it."""
    cur.execute(
        "INSERT INTO wiki_foundays"
        " (guild_id, channel_id, url, name, founded_at, hour, minute, lang, created_by,"
        "  last_sent_year, created_at) VALUES (?,?,?,?,?,?,?,?,?,NULL,?)"
        " ON CONFLICT(guild_id, url) DO UPDATE SET"
        " channel_id=excluded.channel_id, name=excluded.name, founded_at=excluded.founded_at,"
        " hour=excluded.hour, minute=excluded.minute, lang=excluded.lang,"
        " created_by=excluded.created_by, last_sent_year=NULL",
        (str(guild_id), str(channel_id), url, name, int(founded_at), int(hour), int(minute),
         lang, str(created_by), int(time.time()))
    )
    conn.commit()

def get_wiki_foundays(guild_id=None):
    """One server's anniversaries by wiki name, or every row in the deployment
    when `guild_id` is omitted — the latter is what the once-a-minute founday
    loop walks."""
    if guild_id is None:
        return cur.execute("SELECT * FROM wiki_foundays ORDER BY id").fetchall()
    return cur.execute(
        "SELECT * FROM wiki_foundays WHERE guild_id=? ORDER BY name",
        (str(guild_id),)
    ).fetchall()

def delete_wiki_founday(guild_id, url):
    """Stop announcing one wiki in one server. Returns whether a row existed,
    so `/wiki-founday-remove` can tell "removed" from "there was nothing" —
    the url is typed by hand and is easy to get wrong."""
    c = cur.execute(
        "DELETE FROM wiki_foundays WHERE guild_id=? AND url=?",
        (str(guild_id), url)
    )
    conn.commit()
    return c.rowcount > 0

def delete_guild_wiki_foundays(guild_id):
    """Drop every anniversary of a server at once — the bot leaving it. Also
    done by db/unions.py: remove_chat, which is the path that actually fires
    on_guild_remove; this one exists for the direct call."""
    cur.execute("DELETE FROM wiki_foundays WHERE guild_id=?", (str(guild_id),))
    conn.commit()

def mark_wiki_founday_sent(founday_id, year):
    """Record that this wiki has been greeted in `year`.

    The whole yearly cycle rests on this one column: the loop greets a wiki
    only when `last_sent_year` differs from the current year, so a bot that was
    down at the scheduled minute still greets it later that day, and a bot
    restarted three times greets it once. Writing the year *before* a failed
    send would skip the year entirely, so the caller marks only after the
    message is out."""
    cur.execute("UPDATE wiki_foundays SET last_sent_year=? WHERE id=?", (int(year), founday_id))
    conn.commit()
