"""Entry point: both bots and every cross-platform background loop.

`db.init()` runs here, before either half is imported far enough to touch a
table, and it is the only call that ever creates schema. The process must be
started with cwd = `src/` — the database and `.env` are opened by relative path,
and the control panel launches it exactly that way.

The loops below are all shaped alike: poll on a fixed interval and act only on
what is actually due, deciding that by comparing a stored marker rather than by
trusting the clock they woke up on. That is what makes a bot which was down
through a scheduled minute do the work when it comes back, and a bot restarted
three times in an hour do it once. The economy ticks compare a UTC period label
in `bot_settings`, the scheduled channel posts compare a per-row `last_marker`,
the celebrations compare the local date a greeting last went out on, and the
wiki refresh compares the time of the last one (`wiki_refreshed_at`).

`economy_loop`'s ordering is load-bearing twice over: consumption runs before
the FX recompute inside the daily tick, and the autoproduction advance is
measured against a monotonic clock rather than the loop's nominal hour, so a
slow tick does not silently produce less.

The two loops that live on the Discord client instead — presence and backups —
are in discord_bot/client.py, because they need the client to be ready first.

`main` starts its tasks and stops on the first one to end, rather than
waiting for all of them: see its docstring for why a `gather` there turned every
`systemctl stop` into a SIGKILL.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("dem.main")

import db
import economy
import olympiad
import stats
from config import DISCORD_TOKEN
from discord_bot import bot as discord_bot
from telegram_bot import bot as tg_bot, main as tg_main
from utils import send_service_event, localized, format_stored_user, good_display_name

db.init()
for _chat in db.all_chats():
    db.ensure_neutral_enterprise(_chat["platform"], _chat["chat_id"], _chat["title"])
db.repair_neutral_names()
db.merge_all_linked_economies()
db.rule_since()

async def retention_loop():
    """Run every retention sweep once a day.

    The sweeps enforce nothing on their own — each table's reads already respect
    its own window — so the whole loop is wrapped in one bare except: a failing
    sweep must not take the other ten down with it."""
    while True:
        try:
            db.cleanup_old_loc_suggestions()
            db.cleanup_old_pending_links()
            db.cleanup_old_join_requests()
            db.cleanup_old_quiz_progress()
            db.cleanup_old_quiz_results()
            db.cleanup_old_server_production()
            db.cleanup_old_server_consumption()
            db.cleanup_old_neutral_activity()
            db.cleanup_old_earn_stats()
            db.cleanup_old_server_tasks()
            db.cleanup_old_shipments()
            db.cleanup_old_dm_langs()
            db.cleanup_old_wiki_accounts()
        except Exception:
            pass
        await asyncio.sleep(24 * 3600)

async def olympiad_loop():
    """Close a finished Olympiad: its contests, candidates and votes are deleted
    and its commands come off the Discord command list. Polled once a minute, so
    a period that ended while the bot was down is closed as soon as it is back."""
    from discord_bot import sync_olympiad_commands
    while True:
        try:
            if olympiad.cleanup_expired():
                await sync_olympiad_commands()
        except Exception:
            logger.exception("Olympiad expiry check failed")
        await asyncio.sleep(60)

async def _broadcast_fx_update():
    """After the daily FX recompute: post each union's new currency values to
    every server that configured an economic log channel with /server logs."""
    for row in db.all_chat_channels("logs"):
        try:
            chat = db.get_chat(row["server_id"])
            if not chat:
                continue
            lang = stats.channel_lang(row)
            lines = stats.fx_lines(chat["union_code"], lang)
            if not lines:
                continue
            await stats.post_to_channel(row["platform"], row["channel_key"],
                                        localized("fx_log_title", lang), lines)
        except Exception as e:
            logger.warning("FX log post failed for %s/%s: %s",
                           row["platform"], row["server_id"], e)

def _run_due_economy_ticks():
    """Fire the daily, weekly and monthly economy ticks when their UTC period
    rolls over. The last-run markers live in bot_settings so a restart never
    double-runs a period nor skips one it missed while the bot was down (the
    celebrations keep their greeting dates the same way). Returns whether the
    daily tick ran (the FX broadcast follows it)."""
    now = datetime.now(timezone.utc)
    daily_ran = False
    today = now.strftime("%Y-%m-%d")
    if db.get_setting("econ_last_daily") != today:
        summary = economy.run_daily_tick()
        economy.evaluate_finished_tasks()
        db.set_setting("econ_last_daily", today)
        logger.info("Economy daily tick: %s", summary)
        daily_ran = True
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if db.get_setting("econ_last_weekly") != week:
        summary = economy.run_weekly_tick()
        db.set_setting("econ_last_weekly", week)
        logger.info("Economy weekly tick: %s", summary)
    month = now.strftime("%Y-%m")
    if db.get_setting("econ_last_monthly") != month:
        summary = economy.run_monthly_tick()
        db.set_setting("econ_last_monthly", month)
        logger.info("Economy monthly tick: %s", summary)
    return daily_ran

async def economy_loop():
    """FX recompute + autosend (daily), enterprise payroll + recurring exports
    (weekly), wages + party dues + monthly payroll (monthly), and the hourly
    advance of the 24/7 autoproduction lines. Polls hourly and acts only when a
    period is actually due."""
    last_auto = time.monotonic()
    while True:
        try:
            daily_ran = _run_due_economy_ticks()
            if daily_ran:
                await _broadcast_fx_update()
        except Exception:
            logger.exception("Economy tick failed")
        try:
            now_mono = time.monotonic()
            hours = max(0.0, (now_mono - last_auto) / 3600.0)
            last_auto = now_mono
            produced = economy.run_autoproduce(hours)
            if produced:
                logger.info("Autoproduction made %d unit(s)", produced)
        except Exception:
            logger.exception("Autoproduction run failed")
        await asyncio.sleep(3600)

async def _send_progress_dm(platform, user_id, text):
    """Tell one person privately that something of theirs has moved on.

    Only ever called about the result of a run they started themselves, which
    is the whole rule for these messages: the bot opens an account for anyone
    who merely talks in an earning channel, and that person has not asked the
    bot for anything and must not be written to. `/good craft` is asking."""
    try:
        if platform == "discord":
            from discord_bot import bot as dc
            user = dc.get_user(int(user_id)) or await dc.fetch_user(int(user_id))
            if user is not None:
                await user.send(text)
        elif platform == "telegram":
            from telegram_bot import bot as tg
            await tg.send_message(int(user_id), text)
    except Exception as e:
        logger.info("Progress DM not delivered to %s %s: %s", platform, user_id, e)

async def production_loop():
    """Manual production runs take minutes; poll for finished ones and announce
    each in the channel where it was started.

    Two things beyond the batch itself may come out of a run, and both are
    reported here because this is where a run ends: a batch that came out
    lucky says so in the channel, and a run that raised the worker's quality
    level is sent to them privately, since a level is theirs rather than the
    channel's news."""
    while True:
        try:
            for row in db.get_due_productions(int(time.time())):
                info = economy.finish_production(row)
                if not info:
                    continue
                lang = row["lang"] or "en"
                good = info["good"]
                emoji = f"{good['emoji']} " if good["emoji"] else ""
                levelled = info["level"] > info["level_before"]

                if row["notify_key"]:
                    user = format_stored_user(
                        row["notify_platform"], row["starter_platform"],
                        row["starter_id"], row["starter_display"])
                    dest = localized("production_dest_enterprise", lang,
                                     code=row["owner_id"]) \
                        if row["owner_type"] == "enterprise" \
                        else localized("production_dest_inventory", lang)
                    text = localized("production_done", lang, user=user, qty=info["qty"],
                                     emoji=emoji, name=good_display_name(good, lang),
                                     level=info["level"], dest=dest)
                    if info["lucky"]:
                        text += "\n" + localized("production_lucky", lang, qty=info["qty"])
                    try:
                        await stats.post_to_channel(row["notify_platform"],
                                                    row["notify_key"], None, [text])
                    except Exception as e:
                        logger.warning("Production notice failed: %s", e)

                if levelled:
                    await _send_progress_dm(
                        row["starter_platform"], row["starter_id"],
                        localized("progress_level_up", lang, emoji=emoji,
                                  name=good_display_name(good, lang),
                                  level=info["level"]))
        except Exception:
            logger.exception("Production loop failed")
        await asyncio.sleep(20)

async def shipment_loop():
    """Exports travel between servers; deliver the ones that have arrived and
    announce each in the channel where the export was launched."""
    while True:
        try:
            for row in db.get_due_shipments(int(time.time())):
                info = economy.arrive_shipment(row)
                if not info or not row["notify_key"]:
                    continue
                lang = row["lang"] or "en"
                good = info["good"]
                emoji = f"{good['emoji']} " if good["emoji"] else ""
                src = info["from_ent"]
                tgt = info["to_ent"]
                key = "export_arrived_perished" if info["lost"] else "export_arrived"
                text = localized(key, lang, qty=info["qty"], emoji=emoji,
                                 name=good_display_name(good, lang),
                                 source=f"{src['name']} [{src['code']}]",
                                 target=f"{tgt['name']} [{tgt['code']}]",
                                 lost=info["lost"])
                try:
                    await stats.post_to_channel(row["notify_platform"],
                                                row["notify_key"], None, [text])
                except Exception as e:
                    logger.warning("Shipment notice failed: %s", e)
        except Exception:
            logger.exception("Shipment loop failed")
        await asyncio.sleep(20)

def _due_channel_posts(now_utc):
    """Yield ('logs'|'tasks', row, marker) for every configured channel whose
    scheduled moment (in its own timezone) has passed and was not posted yet.
    A bot that was down at the scheduled minute posts as soon as it is back."""
    for row in db.all_chat_channels():
        local = now_utc + timedelta(minutes=row["tz_offset"] or 0)
        if row["kind"] == "logs":
            monday = local - timedelta(days=local.weekday())
            sched = monday.replace(hour=row["hour"] or 0, minute=row["minute"] or 0,
                                   second=0, microsecond=0) \
                + timedelta(days=row["weekday"] or 0)
            if sched > local:
                continue
            iso = sched.isocalendar()
            marker = f"{iso[0]}-W{iso[1]:02d}"
        else:
            sched = local.replace(day=1, hour=row["hour"] or 0,
                                  minute=row["minute"] or 0, second=0, microsecond=0)
            if sched > local:
                continue
            marker = local.strftime("%Y-%m")
        if row["last_marker"] != marker:
            yield row["kind"], row, marker

async def channel_post_loop():
    """Per-server scheduled posts: the weekly statistics into the /server logs
    channel, the monthly task into the /server tasks channel."""
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            for kind, row, marker in _due_channel_posts(now_utc):
                lang = stats.channel_lang(row)
                try:
                    if kind == "logs":
                        title, lines = stats.weekly_stats(row["platform"],
                                                          row["server_id"], lang)
                    else:
                        title, lines = stats.monthly_task_post(
                            row["platform"], row["server_id"], marker, lang)
                    await stats.post_to_channel(row["platform"], row["channel_key"],
                                                title, lines)
                except Exception as e:
                    logger.warning("Scheduled %s post failed for %s/%s: %s",
                                   kind, row["platform"], row["server_id"], e)
                db.set_chat_channel_marker(row["platform"], row["server_id"],
                                           row["kind"], marker)
        except Exception:
            logger.exception("Channel post loop failed")
        await asyncio.sleep(60)

async def setup_deadline_loop():
    """Daily wrapper around setup_deadline.setup_deadline_pass: leaves the
    communities whose seven days ran out without a `/server setup`, and reports each
    departure to the service chats.

    Waits for the Discord client first — the sweep reads `Guild.me.joined_at`
    off the guild list, and an empty one would simply find nothing to do."""
    from setup_deadline import setup_deadline_pass
    from telegram_bot import bot as tg_bot

    await discord_bot.wait_until_ready()
    while True:
        try:
            for event_key, fields in await setup_deadline_pass(discord_bot, tg_bot):
                await send_service_event(event_key, **fields)
        except Exception:
            logger.exception("Setup deadline sweep failed")
        await asyncio.sleep(24 * 3600)

async def sponsor_loop():
    """Repair cached Patreon tiers after missed Discord role events."""
    import sponsors
    await discord_bot.wait_until_ready()
    while True:
        try:
            await sponsors.reconcile()
        except Exception:
            logger.exception("Sponsor role reconciliation failed")
        await asyncio.sleep(3600)

WIKI_REFRESH_SECONDS = 12 * 3600

async def celebration_loop():
    """Once a minute: the wiki birthday posts, the birthday greetings on both
    platforms and the birthday role; hourly, the wiki representative role.

    What is due is decided by the stored local date of each greeting, not by
    the minute the loop woke up in, so a bot that was down at ten o'clock
    still greets when it is back that day and a restart does not greet twice.
    Waits for the Discord client, whose member lists the passes read."""
    from discord_bot import daily_pass, rep_role_pass
    from telegram_bot import birthday_pass

    await discord_bot.wait_until_ready()
    last_rep = 0.0
    while True:
        now = datetime.now(timezone.utc)
        try:
            await daily_pass(now)
        except Exception:
            logger.exception("Discord celebrations failed")
        try:
            await birthday_pass(now)
        except Exception:
            logger.exception("Telegram birthday greetings failed")
        if time.monotonic() - last_rep >= 3600:
            last_rep = time.monotonic()
            try:
                await rep_role_pass()
            except Exception:
                logger.exception("Representative role pass failed")
        await asyncio.sleep(60)

async def audit_loop():
    """Post the banks' queued administrative events to their audit channels
    every thirty seconds (economy/audit.py). The economy queues them from
    synchronous code — a payroll run cannot await a platform — and an event
    waits in `audit_outbox` across a restart rather than being lost."""
    await discord_bot.wait_until_ready()
    while True:
        try:
            await economy.flush_audits()
        except Exception:
            logger.exception("Audit posting failed")
        await asyncio.sleep(30)

async def community_title_loop():
    """Learn the current name of every bound community at start and then
    once a day: a Discord server from the client's cache, a Telegram group
    from the API. The names go to `chats.title`, which is what lists print
    instead of a bare id, and a neutral enterprise still carrying the bot's
    own name is renamed with its community (db/enterprises.py:
    rename_auto_neutral)."""
    await discord_bot.wait_until_ready()
    while True:
        try:
            await stats.refresh_union_titles()
        except Exception:
            logger.exception("Community name refresh failed")
        await asyncio.sleep(24 * 3600)

async def wiki_refresh_loop():
    """Re-read every union wiki's name, language and administration twice a
    day, then bring the representative roles up to date.

    The time of the last refresh is kept in `bot_settings` and written before
    the refresh starts, so frequent restarts do not multiply the requests to
    the wikis and a refresh that crashes is not retried within the hour."""
    import wikis
    from discord_bot import rep_role_pass

    await discord_bot.wait_until_ready()
    while True:
        try:
            last = int(db.get_setting("wiki_refreshed_at") or 0)
            if time.time() - last >= WIKI_REFRESH_SECONDS:
                db.set_setting("wiki_refreshed_at", str(int(time.time())))
                count = await wikis.refresh_all()
                logger.info("Wiki refresh: %d wiki(s) read", count)
                await rep_role_pass()
        except Exception:
            logger.exception("Wiki refresh failed")
        await asyncio.sleep(600)

async def main():
    """Start both bots and every cross-platform loop, then wait for whichever
    of them ends first and take the others down with it.

    Waiting for *all* of them is what a `gather` would do, and it is wrong
    here. aiogram installs its own SIGINT/SIGTERM handler and answers the
    signal by stopping the polling neatly — at which point the other tasks
    know nothing about it, and the loops sitting in an
    `asyncio.sleep` of up to a day hold the process open. A service manager
    waiting for it to exit runs out of patience and sends SIGKILL: an orderly
    stop becomes a killing in the middle of whatever the economy was writing
    to dem.db, and the `finally` below — the notice to the service chats —
    never runs at all. Ending on the first task to finish, and cancelling the
    rest, is what makes the bot close when it is asked to. fd_bot/src/main.py
    stops the same way, for the same reason.

    The five-second wait before announcing the start is there so the two
    clients are actually connected when the service chats hear about it. The
    stop notice is in a finally, so an orderly shutdown says so; it goes out
    before either client is closed, because it is sent through both of them.
    Closing them is last, and each on its own, so that a half which is already
    gone cannot keep the other one open."""
    tasks = [
        asyncio.create_task(tg_main(), name="telegram"),
        asyncio.create_task(discord_bot.start(DISCORD_TOKEN), name="discord"),
        asyncio.create_task(retention_loop(), name="retention"),
        asyncio.create_task(economy_loop(), name="economy"),
        asyncio.create_task(production_loop(), name="production"),
        asyncio.create_task(shipment_loop(), name="shipment"),
        asyncio.create_task(channel_post_loop(), name="channel-post"),
        asyncio.create_task(olympiad_loop(), name="olympiad"),
        asyncio.create_task(setup_deadline_loop(), name="setup-deadline"),
        asyncio.create_task(sponsor_loop(), name="sponsors"),
        asyncio.create_task(celebration_loop(), name="celebrations"),
        asyncio.create_task(wiki_refresh_loop(), name="wiki-refresh"),
        asyncio.create_task(community_title_loop(), name="community-titles"),
        asyncio.create_task(audit_loop(), name="audit"),
    ]

    await asyncio.sleep(5)
    await send_service_event("bot_started")

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error:
                logger.error("the %s task stopped: %s", task.get_name(), error)
            else:
                logger.info("the %s task finished, shutting down", task.get_name())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        try:
            await send_service_event("bot_stopped")
        except Exception:
            logger.warning("could not report the shutdown to the service chats")
        finally:
            for close in (tg_bot.session.close(), discord_bot.close()):
                try:
                    await close
                except Exception:
                    pass

if __name__ == "__main__":
    asyncio.run(main())
