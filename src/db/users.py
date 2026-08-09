"""Who a person is: Fandom verification, the Discord-to-Telegram account
link, and the two questions the rest of the bot asks about identity.

Verification is global and one-directional. A Discord user verifies against
their Fandom profile (/verify, fandom.py); a Telegram user is verified only
by being linked to an already-verified Discord account. The link itself is a
handshake with a thirty-minute window: each side names the other's username
(`pending_links`), and `register_link_attempt` completes it the moment the
opposite half already names them back.

A linked pair is one person in exactly two ways. `user_identities` returns
both (platform, user_id) pairs and is what makes the quiz history shared;
`canonical_user` collapses the pair onto the Discord account and is what every
account, balance and inventory is keyed by. Note that collapsing happens at
lookup time, not at link time: money already earned on a Telegram account
before the link simply stops being addressed, and nothing merges it.

Not this module's zone: quiz storage itself (db/quizzes.py), the privacy
switches over it, and anything about money (db/banks.py).
"""
import time

from db import conn, cur

def add_fandom_verification(discord_id, fandom_name, fandom_userid, discord_handle):
    """Record that a Discord account was verified against a Fandom profile.

    INSERT OR REPLACE, so verifying again simply moves the person to a
    different wiki account. What is stored is a snapshot of one moment:
    nothing ever re-reads the profile, so a handle removed from the wiki later
    does not un-verify anybody."""
    cur.execute(
        "INSERT OR REPLACE INTO fandom_verifications"
        " (discord_id, fandom_name, fandom_userid, discord_handle, verified_at)"
        " VALUES (?,?,?,?,?)",
        (str(discord_id), fandom_name, str(fandom_userid), discord_handle, int(time.time()))
    )
    conn.commit()

def get_fandom_verification(discord_id):
    """The verification row, or None. Read for the wiki name as well as for the
    fact — the Olympiad skips its account question when this answers it."""
    return cur.execute(
        "SELECT * FROM fandom_verifications WHERE discord_id=?",
        (str(discord_id),)
    ).fetchone()

def is_discord_verified(discord_id):
    """Whether this Discord account passed `/verify`. Verification is global,
    not per server: the gate is about the person, not about where they are."""
    return get_fandom_verification(discord_id) is not None

def link_accounts(discord_id, telegram_id):
    """One-to-one Discord<->Telegram link; replaces any previous link on either side."""
    cur.execute("DELETE FROM account_links WHERE discord_id=? OR telegram_id=?",
                (str(discord_id), str(telegram_id)))
    cur.execute(
        "INSERT INTO account_links (discord_id, telegram_id, linked_at) VALUES (?,?,?)",
        (str(discord_id), str(telegram_id), int(time.time()))
    )
    conn.commit()

def get_link_by_telegram(telegram_id):
    """The link row a Telegram account belongs to, or None. This is the lookup
    behind both `canonical_user` and `is_telegram_verified`, so it runs on
    every counted Telegram message."""
    return cur.execute(
        "SELECT * FROM account_links WHERE telegram_id=?", (str(telegram_id),)
    ).fetchone()

def get_link_by_discord(discord_id):
    """The link row a Discord account belongs to, or None — the same relation
    read from the other side."""
    return cur.execute(
        "SELECT * FROM account_links WHERE discord_id=?", (str(discord_id),)
    ).fetchone()

def is_telegram_verified(telegram_id):
    """A Telegram user is verified when linked to a Fandom-verified Discord account."""
    row = get_link_by_telegram(telegram_id)
    return bool(row and is_discord_verified(row["discord_id"]))

def add_pending_link(platform, user_id, username, claimed_other):
    """Store one half of the account-linking handshake.

    `username` is the caller's own name on this platform and `claimed_other`
    the name they say they hold on the other one; the link completes when a row
    exists whose two fields are the mirror image. One row per account (INSERT
    OR REPLACE), so naming a different partner simply replaces the claim."""
    cur.execute(
        "INSERT OR REPLACE INTO pending_links (platform, user_id, username, claimed_other, created_at)"
        " VALUES (?,?,?,?,?)",
        (platform, str(user_id), username, claimed_other, int(time.time()))
    )
    conn.commit()

def get_pending_links(platform, max_age_seconds=30 * 60):
    """The still-valid half-handshakes made from one platform.

    The thirty minutes are enforced here, in the query, and not only by the
    retention sweep: an expired row that has not been swept yet must not be
    able to complete a link."""
    cutoff = int(time.time()) - max_age_seconds
    return cur.execute(
        "SELECT * FROM pending_links WHERE platform=? AND created_at>=?",
        (platform, cutoff)
    ).fetchall()

def remove_pending_link(platform, user_id):
    """Drop a half-handshake once the link it belonged to has completed. Both
    halves are removed together by `register_link_attempt`."""
    cur.execute(
        "DELETE FROM pending_links WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    )
    conn.commit()

def cleanup_old_pending_links(max_age_seconds=30 * 60):
    """Sweep the handshake window. Runs daily from main.py: retention_loop,
    which is far coarser than the thirty minutes — the reads enforce the
    deadline themselves, so this only keeps the table small."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute("DELETE FROM pending_links WHERE created_at<?", (cutoff,))
    conn.commit()

def _name_eq(a, b):
    """Compare two usernames the way a person would type them: ignoring case,
    surrounding spaces and a leading '@'. Both halves of the handshake are
    typed by hand on the other platform's keyboard, so exact matching would
    fail far more often than it would protect anything."""
    return (a or "").strip().lstrip("@").lower() == (b or "").strip().lstrip("@").lower()

def user_identities(platform, user_id):
    """Every (platform, user_id) pair that is the same person: the caller plus
    the account they linked with /add-discord + /add-telegram. Quiz results are
    stored per platform but read through this set, so a linked user sees one
    shared history on both platforms."""
    ids = [(platform, str(user_id))]
    if platform == "discord":
        row = get_link_by_discord(user_id)
        if row and row["telegram_id"]:
            ids.append(("telegram", str(row["telegram_id"])))
    elif platform == "telegram":
        row = get_link_by_telegram(user_id)
        if row and row["discord_id"]:
            ids.append(("discord", str(row["discord_id"])))
    return ids

def register_link_attempt(platform, user_id, username, claimed_other):
    """Record the caller's half of the /add-discord + /add-telegram handshake and
    link the accounts when the matching half already exists (each side names the
    other's username and is itself named by it). Returns
    ('linked', discord_id, telegram_id) or ('pending', None, None)."""
    from db.quizzes import prune_quiz_results

    add_pending_link(platform, str(user_id), username, claimed_other)
    other = "telegram" if platform == "discord" else "discord"
    for p in get_pending_links(other):
        if _name_eq(p["claimed_other"], username) and _name_eq(p["username"], claimed_other):
            discord_id = str(user_id) if platform == "discord" else p["user_id"]
            telegram_id = str(user_id) if platform == "telegram" else p["user_id"]
            link_accounts(discord_id, telegram_id)
            remove_pending_link(platform, str(user_id))
            remove_pending_link(other, p["user_id"])
            prune_quiz_results("discord", discord_id)
            return "linked", discord_id, telegram_id
    return "pending", None, None

def canonical_user(platform, user_id):
    """The identity that owns a person's money: their Discord account when the
    two are linked, otherwise the platform account itself. Mirrors verification,
    so a linked Telegram user and their Discord account share one wallet."""
    uid = str(user_id)
    if platform == "telegram":
        row = get_link_by_telegram(uid)
        if row and row["discord_id"]:
            return "user", "discord", str(row["discord_id"])
        return "user", "telegram", uid
    return "user", "discord", uid
