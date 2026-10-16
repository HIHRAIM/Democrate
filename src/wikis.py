"""Reading a wiki over its public MediaWiki API: turning a typed link into the
wiki, its founding moment, its administration and the Discord handles that
administration publishes on Fandom.

Every request here is anonymous and read-only, and goes through `_get`, which
spaces requests out by REQUEST_GAP seconds so that a refresh of every wiki is
a slow trickle rather than a burst. The callers decide how often to ask:
`/wiki add` asks once, and main.py: wiki_refresh_loop asks twice a day.

**A link becomes a wiki by asking candidates.** Fandom puts a language wiki
under a path (`confederation.fandom.com/ru`) and a link may point at any
article, at `/wiki/`, or at the bare root, so `api_candidates` lists the
api.php addresses the link can mean and `describe` keeps the first that
answers siteinfo. The canonical key of a wiki is `server + scriptpath` from
that answer — never the text that was typed. A Fandom wiki in another
language than English lives only under its language path, and a link to the
bare host (`ficcion-sin-limites.fandom.com/wiki/`) finds nothing at the root,
so such a link is also tried under the common language paths
(FANDOM_LANG_PATHS) after the root.

**The founding moment is Wiki-Bot's.** `/overview` in Wiki-Bot
(github.com/Markus-Rost/discord-wiki-bot, ISC) takes the oldest entry of the
wiki's log and, on Fandom, Fandom's own `creation_date` when that is earlier;
`founded_at` does the same. Fandom no longer returns `creation_date`, so in
practice it is the oldest log entry, which can be later than the oldest page
of a wiki that was moved or merged — that is what `/wiki founded` is for.

**The administration is bureaucrats, administrators and content moderators**
(`STAFF_GROUPS`), read with `list=allusers`. Accounts in the `bot` group and
accounts without `autoconfirmed` — the system users MediaWiki keeps, such as
the abuse filter — are not people and are left out. When a person's status
began is read from the rights log only for people the cache has not seen yet;
the cache keeps the answer, which is what keeps a refresh at about two
requests per wiki.

**A wiki's own variables vary.** `siprop=variables` is a list of dicts on
Fandom but a list of bare names on other MediaWikis (es.minecraft.wiki), so
only dict entries are read. A Cloudflare challenge instead of an answer is
`WikiBlocked`, told apart from a link that is no wiki.

**Discord handles come in bulk.** On Fandom a user id is global and the
user-attribute service answers for many ids at once; nothing but the
`discordHandle` field of that answer is kept.

**What is read is cached, not asked for again.** `load_wiki` is what
`/wiki add` runs and `refresh_all` is the twice-daily refresh; both write
through db/wikis.py and import `db` at the call site, so that this module can
also be imported on its own to test the API side.

Not this module's zone: the SQL (db/wikis.py) and deciding what to do with
the cache (the role pass and the greetings in the two halves).
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import aiohttp

logger = logging.getLogger("dem.wikis")

STAFF_GROUPS = ("bureaucrat", "sysop", "content-moderator")
REQUEST_GAP = 1.0
REPRESENTATIVE_AFTER = 30 * 86400

_HEADERS = {"User-Agent": "Democrate-bot/1.0 (union coordination bot)"}
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_BULK_URL = "https://services.fandom.com/user-attribute/user/bulk"
_DETAILS_URL = "https://community.fandom.com/wikia.php"
_FANDOM_HOSTS = ("fandom.com", "wikia.org", "gamepedia.com")
_BULK_CHUNK = 50
FANDOM_LANG_PATHS = ("es", "ru", "pt-br", "pl", "uk", "de", "fr", "it", "ja", "zh", "pt")

_last_request = 0.0
_request_lock = asyncio.Lock()

class WikiError(Exception):
    """The wiki could not be read; the message is for the log, the caller
    words the refusal."""

class WikiBlocked(WikiError):
    """The wiki's own protection (Cloudflare's "Just a moment…" challenge)
    answered instead of the API. It is a block on the address the bot runs
    from, not a wrong link, so the caller says so and suggests trying later."""

async def _get(session, url, params=None):
    """One GET returning parsed JSON, at most one request per REQUEST_GAP
    across the whole process."""
    global _last_request
    async with _request_lock:
        wait = _last_request + REQUEST_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()
    async with session.get(url, params=params, headers=_HEADERS) as resp:
        if resp.status != 200:
            body = await resp.text(errors="replace")
            if resp.status in (403, 429, 503) and ("Just a moment" in body
                                                   or "cf-chl" in body or "challenge" in body):
                raise WikiBlocked(f"HTTP {resp.status} challenge from {url}")
            raise WikiError(f"HTTP {resp.status} from {url}")
        return await resp.json(content_type=None)

def session():
    """A client session with this module's timeout, for a caller that makes
    several requests in a row."""
    return aiohttp.ClientSession(timeout=_TIMEOUT)

def is_fandom_host(host):
    """Whether a host belongs to the Fandom farm."""
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in _FANDOM_HOSTS)

def api_candidates(raw):
    """The api.php addresses a typed wiki link can mean, most likely first,
    or [] when it is not a link at all. The scheme may be omitted."""
    text = (raw or "").strip().strip("<>")
    if not text:
        return []
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if not host or "." not in host or " " in host:
        return []
    root = f"https://{host}"
    segments = [s for s in parts.path.split("/") if s]
    prefixes = []
    if "wiki" in segments:
        prefixes.append("/".join(segments[:segments.index("wiki")]))
    elif segments and segments[0] not in ("api.php", "index.php"):
        prefixes.append(segments[0])
    prefixes.append("")
    out = []
    for prefix in prefixes:
        base = f"{root}/{prefix}" if prefix else root
        for api in (f"{base}/api.php", f"{base}/w/api.php"):
            if api not in out:
                out.append(api)
    if is_fandom_host(host):
        out = [a for a in out if "/w/api.php" not in a]
        if not any(prefixes):
            out += [f"{root}/{code}/api.php" for code in FANDOM_LANG_PATHS]
    return out

def _general_to_info(general, variables, api_url):
    """What `describe` returns, built from a siteinfo answer."""
    server = general.get("server") or ""
    if server.startswith("//"):
        server = "https:" + server
    script = general.get("scriptpath") or ""
    article = general.get("articlepath") or "/wiki/$1"
    city_id = None
    for var in variables or []:
        if isinstance(var, dict) and var.get("id") == "wgCityId":
            try:
                city_id = int(var.get("*") if "*" in var else var.get("content"))
            except (TypeError, ValueError):
                city_id = None
    host = urlsplit(server).hostname or ""
    return {
        "base_url": f"{server}{script}".rstrip("/"),
        "api_url": f"{server}{script}/api.php",
        "main_url": f"{server}{article.replace('$1', '')}",
        "name": general.get("sitename") or host,
        "lang": (general.get("lang") or "").lower(),
        "city_id": city_id,
        "is_fandom": city_id is not None or is_fandom_host(host),
    }

async def describe(sess, raw=None, api_url=None):
    """Find the wiki a link means and describe it: base_url, api_url,
    main_url, name, lang, city_id, is_fandom. Raises WikiError when no
    candidate answers like a MediaWiki."""
    candidates = [api_url] if api_url else api_candidates(raw)
    if not candidates:
        raise WikiError("not a link")
    last = blocked = None
    for candidate in candidates:
        try:
            data = await _get(sess, candidate, {
                "action": "query", "meta": "siteinfo", "siprop": "general|variables",
                "format": "json", "formatversion": "2"})
        except WikiBlocked as e:
            blocked = last = e
            continue
        except Exception as e:
            last = e
            continue
        general = (data or {}).get("query", {}).get("general") if isinstance(data, dict) else None
        if general:
            return _general_to_info(general, data["query"].get("variables"), candidate)
    if blocked is not None:
        raise WikiBlocked(f"blocked by the wiki's protection ({blocked})")
    raise WikiError(f"no MediaWiki API answered ({last})")

def _parse_ts(value):
    """A MediaWiki or Fandom timestamp as Unix seconds, or None."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())

async def founded_at(sess, info):
    """The wiki's founding moment, Wiki-Bot's way: the oldest log entry, or
    Fandom's creation_date when that is earlier. None when neither is
    known."""
    found = None
    try:
        data = await _get(sess, info["api_url"], {
            "action": "query", "list": "logevents", "ledir": "newer", "lelimit": "1",
            "leprop": "timestamp", "format": "json", "formatversion": "2"})
        events = data.get("query", {}).get("logevents") or []
        if events:
            found = _parse_ts(events[0].get("timestamp"))
    except Exception as e:
        logger.info("Oldest log entry of %s unreadable: %s", info["base_url"], e)
    if info.get("city_id"):
        try:
            data = await _get(sess, _DETAILS_URL, {
                "controller": "WikisApiController", "method": "getDetails",
                "ids": str(info["city_id"]), "format": "json"})
            item = (data.get("items") or {}).get(str(info["city_id"])) or {}
            created = _parse_ts(item.get("creation_date"))
            if created is not None and (found is None or created < found):
                found = created
        except Exception as e:
            logger.info("Fandom details of %s unreadable: %s", info["base_url"], e)
    return found

async def administration(sess, info):
    """The wiki's people with an administrative status, as dicts with
    user_id, name and groups (only the STAFF_GROUPS they hold)."""
    people, params = [], {
        "action": "query", "list": "allusers", "augroup": "|".join(STAFF_GROUPS),
        "auprop": "groups", "aulimit": "500", "format": "json", "formatversion": "2"}
    while True:
        data = await _get(sess, info["api_url"], params)
        if "error" in data:
            raise WikiError(str(data["error"].get("info") or data["error"]))
        for user in data.get("query", {}).get("allusers", []):
            groups = set(user.get("groups") or [])
            if "bot" in groups or "autoconfirmed" not in groups:
                continue
            held = [g for g in STAFF_GROUPS if g in groups]
            if held and user.get("userid"):
                people.append({"user_id": str(user["userid"]), "name": user["name"],
                               "groups": held})
        cont = data.get("continue")
        if not cont:
            return people
        params = {**params, **cont}

async def status_since(sess, info, name):
    """When `name`'s current uninterrupted administrative status began, by
    the rights log: the newest entry that took them from no staff group to
    one. None when the log does not say — the caller then counts from the
    wiki's founding."""
    try:
        data = await _get(sess, info["api_url"], {
            "action": "query", "list": "logevents", "letype": "rights",
            "letitle": f"User:{name}", "lelimit": "100", "leprop": "timestamp|details",
            "format": "json", "formatversion": "2"})
    except Exception as e:
        logger.info("Rights log of %s on %s unreadable: %s", name, info["base_url"], e)
        return None
    staff = set(STAFF_GROUPS)
    for event in data.get("query", {}).get("logevents") or []:
        params = event.get("params") or {}
        old = set(params.get("oldgroups") or [])
        new = set(params.get("newgroups") or [])
        if new & staff and not old & staff:
            return _parse_ts(event.get("timestamp"))
        if old & staff and not new & staff:
            return None
    return None

async def discord_handles(sess, user_ids):
    """({Fandom user id: Discord handle}, {ids that could not be read}) —
    the handles of the profiles that publish one, asked in chunks of fifty.
    An id in the second set is unknown rather than handle-less, so the
    caller keeps what it had."""
    out, failed = {}, set()
    ids = [str(u) for u in dict.fromkeys(user_ids)]
    for start in range(0, len(ids), _BULK_CHUNK):
        chunk = ids[start:start + _BULK_CHUNK]
        query = "&".join(f"id={quote(i)}" for i in chunk)
        try:
            data = await _get(sess, f"{_BULK_URL}?{query}")
        except Exception as e:
            logger.info("Fandom profile bulk read failed: %s", e)
            failed.update(chunk)
            continue
        for uid, attrs in ((data or {}).get("users") or {}).items():
            handle = (attrs or {}).get("discordHandle")
            if handle and str(handle).strip():
                out[str(uid)] = str(handle).strip()
    return out, failed

def _strip_scheme(url):
    """A URL without its scheme, for comparing http and https forms."""
    return re.sub(r"^https?://", "", (url or "").strip().lower())

def match_known_wiki(raw, rows):
    """The row among `rows` (wikis rows) that a typed link means, without a
    network request: the link's api.php candidates are compared with the
    stored api_url. None when it means none of them."""
    by_api = {_strip_scheme(r["api_url"]): r for r in rows}
    for candidate in api_candidates(raw):
        row = by_api.get(_strip_scheme(candidate))
        if row is not None:
            return row
    return None

async def sync_staff(sess, wiki_id, info, founded):
    """Read one wiki's administration and write it into the cache.

    A person the cache already dates keeps that date; a newcomer is dated by
    the rights log, or by the wiki's founding when the log is silent. Discord
    handles are read in bulk on Fandom only, and a handle that could not be
    read this time keeps its previous value."""
    import db
    people = await administration(sess, info)
    known = {r["user_id"]: r for r in db.get_wiki_staff(wiki_id)}
    for person in people:
        old = known.get(person["user_id"])
        if old is not None and old["since"] is not None:
            person["since"] = int(old["since"])
        else:
            since = await status_since(sess, info, person["name"])
            person["since"] = since if since is not None else founded
        person["account_key"] = (f"fandom:{person['user_id']}" if info["is_fandom"]
                                 else f"{wiki_id}:{person['user_id']}")
    handles, failed = ({}, set())
    if info["is_fandom"] and people:
        handles, failed = await discord_handles(sess, [p["user_id"] for p in people])
    for person in people:
        old = known.get(person["user_id"])
        if person["user_id"] in failed and old is not None:
            person["discord_handle"] = old["discord_handle"]
        else:
            person["discord_handle"] = handles.get(person["user_id"])
    db.replace_wiki_staff(wiki_id, people)

async def load_wiki(raw):
    """Resolve a typed link and make the bot know that wiki: its row, its
    founding moment (detected only the first time) and its administration.
    Returns the wikis row. Raises WikiError when the link is no wiki; a
    failure to read the administration is recorded on the row instead, and
    the next refresh tries again."""
    import db
    async with session() as sess:
        info = await describe(sess, raw)
        existing = db.find_wiki(info["base_url"])
        if existing is not None and existing["founded_at"] is not None:
            detected = int(existing["founded_at"])
        else:
            detected = await founded_at(sess, info)
        wiki_id = db.save_wiki(info, detected)
        try:
            await sync_staff(sess, wiki_id, info, db.wiki_founded(db.get_wiki(wiki_id)))
        except Exception as e:
            logger.warning("Administration of %s unreadable: %s", info["base_url"], e)
            db.set_wiki_refresh_error(wiki_id, e)
    return db.get_wiki(wiki_id)

async def refresh_all():
    """Bring every known wiki's name, language, links and administration up
    to date, one wiki after another. A wiki that fails keeps its cache and
    records why. Returns how many wikis were refreshed."""
    import db
    done = 0
    async with session() as sess:
        for row in db.all_wikis():
            try:
                info = await describe(sess, api_url=row["api_url"])
                info["base_url"] = row["base_url"]
                detected = row["founded_at"]
                if detected is None:
                    detected = await founded_at(sess, info)
                db.save_wiki(info, detected)
                await sync_staff(sess, row["id"], info, db.wiki_founded(db.get_wiki(row["id"])))
                done += 1
            except Exception as e:
                logger.warning("Refresh of %s failed: %s", row["base_url"], e)
                db.set_wiki_refresh_error(row["id"], e)
    return done
