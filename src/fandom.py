"""Read a Fandom user's profile Discord handle, to verify account ownership.

Fandom exposes two public, unauthenticated endpoints we chain:

  1. The MediaWiki users API on Community Central maps a username to a numeric
     user id:  ``/api.php?action=query&list=users&ususers=<name>``.
  2. The user-attribute service returns profile fields by id:
     ``services.fandom.com/user-attribute/user/<id>/attr/discordHandle`` →
     ``{"name": "discordHandle", "value": "<handle>"}`` (404 when unset).

The stored handle may be a modern unique username (``name``) or a legacy
``Name#1234`` — :func:`handle_matches` accepts both.
"""
import logging

import aiohttp

logger = logging.getLogger("dem.fandom")

_USERS_API = "https://community.fandom.com/api.php"
_ATTR_URL = "https://services.fandom.com/user-attribute/user/{uid}/attr/discordHandle"
_HEADERS = {"User-Agent": "Democrate-bot/1.0 (union coordination bot)"}
_TIMEOUT = aiohttp.ClientTimeout(total=20)

async def _fandom_user_id(session, name):
    params = {"action": "query", "list": "users", "ususers": name, "format": "json"}
    async with session.get(_USERS_API, params=params, headers=_HEADERS) as resp:
        data = await resp.json()
    users = data.get("query", {}).get("users", [])
    if not users:
        return None, None
    user = users[0]
    if "userid" not in user or user.get("missing") is not None or user.get("invalid") is not None:
        return None, None
    return str(user["userid"]), user.get("name", name)

async def _discord_handle(session, uid):
    async with session.get(_ATTR_URL.format(uid=uid), headers=_HEADERS) as resp:
        if resp.status == 404:
            return None
        data = await resp.json()
    if isinstance(data, dict) and data.get("name") == "discordHandle":
        value = (data.get("value") or "").strip()
        return value or None
    return None

async def lookup_discord_handle(name):
    """Resolve a Fandom username to (user_id, canonical_name, discord_handle).

    Returns:
      ("not_found", None, None)    — no such Fandom account
      ("no_handle", uid, cname)    — account exists but no Discord handle set
      ("ok", uid, cname, handle)   — handle found
      ("error", None, None)        — network/parse failure
    """
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            uid, cname = await _fandom_user_id(session, name)
            if uid is None:
                return ("not_found", None, None, None)
            handle = await _discord_handle(session, uid)
            if not handle:
                return ("no_handle", uid, cname, None)
            return ("ok", uid, cname, handle)
    except Exception as e:
        logger.warning("Fandom lookup failed for %r: %s", name, e)
        return ("error", None, None, None)

def _norm(s):
    return (s or "").strip().lstrip("@").lower()

def handle_matches(fandom_handle, discord_username, discord_str):
    """True if the Fandom profile's Discord handle matches the command caller.

    Compares case-insensitively and ignores any ``#discriminator`` on either
    side, so both the modern (``name``) and legacy (``Name#1234``) formats work.
    """
    fh = _norm(fandom_handle)
    fh_base = fh.split("#", 1)[0]
    candidates = set()
    for value in (discord_username, discord_str):
        c = _norm(value)
        candidates.add(c)
        candidates.add(c.split("#", 1)[0])
    return fh in candidates or fh_base in candidates
