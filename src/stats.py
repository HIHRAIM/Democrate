"""Server-level economic reporting: the content of the posts the bot publishes
into the channels chosen with /setlogs and /settasks.

Three posts live here — the daily FX update (currency values with day-over-day
change), the weekly server statistics (provision, goods, GDP, task progress)
and the monthly task announcement (with the previous month's verdict). The
builders return plain localized strings; `post_to_channel` renders them on
either platform (an embed on Discord, HTML on Telegram) so `main.py`'s
schedulers stay platform-neutral.
"""
import logging
import time

import db
import economy
from utils import (
    localized, get_chat_lang, category_label, good_display_name,
    provision_level_label,
)

logger = logging.getLogger("dem.stats")

PROVISION_LEVEL_EMOJI = {
    "insufficient": "🔴",
    "acceptable": "🟡",
    "good": "🟢",
    "excellent": "🌟",
}

def channel_lang(row):
    """The language of a stats/tasks channel: the channel's own /locallang if
    set, else the server default."""
    if row["platform"] == "discord":
        return get_chat_lang(f"{row['server_id']}:{row['channel_key']}")
    return get_chat_lang(row["channel_key"])

def fx_lines(union_code, lang):
    """One line per bank of the union: the published value and its change since
    yesterday's recompute."""
    lines = []
    for bank in db.get_banks_in_union(union_code):
        old, new = bank["prev_value"], bank["value"]
        if old:
            pct = (new - old) / old * 100.0
            arrow = "📈" if pct > 0.005 else ("📉" if pct < -0.005 else "➡️")
            change = f"{arrow} {pct:+.2f}%"
        else:
            change = "➡️"
        emoji = f"{bank['emoji']} " if bank["emoji"] else ""
        lines.append(localized("fx_log_line", lang, emoji=emoji,
                               name=bank["currency_name"], code=bank["code"],
                               value=f"{new:.4f}", change=change))
    return lines

def _percent(x):
    """A ratio as a rounded percentage string."""
    return f"{x * 100:.0f}%"

def weekly_stats(platform, server_id, lang):
    """(title, lines) of the weekly server statistics post."""
    report = economy.provision_report(platform, server_id)
    lines = [localized(
        "stats_provision_total", lang,
        emoji=PROVISION_LEVEL_EMOJI.get(report["level"], ""),
        percent=_percent(report["score"]),
        level=provision_level_label(report["level"], lang))]
    if report["task_bonus"]:
        lines.append(localized("stats_task_bonus", lang))
    for cat in db.GOOD_CATEGORIES:
        c = report["categories"][cat]
        mark = "" if not c["base_only"] else " " + localized("stats_base_only_mark", lang)
        short = c["need"] - c["produced"]
        if short > 0.5:
            mark += " " + localized("stats_shortage_mark", lang,
                                    qty=int(round(short)))
        lines.append(localized("stats_category_line", lang,
                               category=category_label(cat, lang),
                               percent=_percent(c["score"])) + mark)
    now = int(time.time())
    week_ago = now - 7 * 86400
    top = db.server_top_goods(platform, server_id, week_ago, now, limit=5)
    total_units = sum(r["qty"] or 0 for r in
                      db.server_category_production(platform, server_id, week_ago, now))
    lines.append("")
    lines.append(localized("stats_goods_header", lang, total=total_units))
    for r in top:
        good = db.get_good(r["good_code"])
        if not good:
            continue
        emoji = f"{good['emoji']} " if good["emoji"] else ""
        lines.append(localized("stats_good_line", lang, emoji=emoji,
                               name=good_display_name(good, lang),
                               code=good["code"], qty=r["qty"]))
    gdp = economy.server_gdp(platform, server_id)
    lines.append("")
    lines.append(localized("stats_gdp_line", lang, gdp=f"{gdp:.2f}"))
    for bank in db.get_banks_in_chat(server_id):
        lines.append(localized("stats_bank_line", lang,
                               emoji=f"{bank['emoji']} " if bank["emoji"] else "",
                               code=bank["code"],
                               supply=economy.format_amount(db.bank_money_supply(bank["code"])),
                               value=f"{bank['value']:.4f}"))
    month = time.strftime("%Y-%m", time.gmtime(now))
    progress = economy.task_progress(platform, server_id, month)
    if progress:
        mandatory = [p for p in progress if p["mandatory"]]
        done = sum(1 for p in mandatory if p["done"] >= p["qty"])
        lines.append("")
        lines.append(localized("stats_task_line", lang, done=done, total=len(mandatory)))
    return localized("stats_title", lang), lines

def _task_item_lines(items, lang):
    """One line per task item: what was asked, what was made, and whether it
    is done — mandatory items marked apart from the optional ones."""
    lines = []
    for item in items:
        if item["kind"] == "category":
            label = category_label(item["key"], lang)
        else:
            good = db.get_good(item["key"])
            label = (f"{good['emoji']} " if good and good["emoji"] else "") + \
                    (good_display_name(good, lang) if good else item["key"]) + \
                    f" [{item['key']}]"
        key = "task_item_mandatory" if item["mandatory"] else "task_item_optional"
        lines.append(localized(key, lang, item=label, qty=item["qty"]))
    return lines

def monthly_task_post(platform, server_id, month, lang):
    """(title, lines) of the 1st-of-month task announcement. Evaluates the
    previous month's task first, then generates and stores this month's."""
    prev = economy.prev_month(month)
    prev_task = db.get_server_task(platform, server_id, prev)
    verdict = None
    if prev_task:
        if prev_task["completed"] is None:
            verdict = economy.evaluate_task(platform, server_id, prev)
        else:
            verdict = bool(prev_task["completed"])
    items = economy.generate_monthly_task(platform, server_id, month)
    lines = []
    if verdict is not None:
        lines.append(localized(
            "task_prev_done" if verdict else "task_prev_failed", lang))
        lines.append("")
    lines.append(localized("task_intro", lang))
    lines.extend(_task_item_lines(items, lang))
    lines.append("")
    lines.append(localized("task_footer", lang))
    return localized("task_title", lang, month=month), lines

async def post_to_channel(platform, channel_key, title, lines):
    """Deliver a built post to a Discord channel or a Telegram chat/topic."""
    text_lines = [l for l in lines]
    if platform == "discord":
        from discord_bot import bot as dc
        import discord
        from utils import DEFAULT_EMBED_COLOR
        ch = dc.get_channel(int(channel_key))
        if not ch:
            ch = await dc.fetch_channel(int(channel_key))
        embed = discord.Embed(title=title or None,
                              description="\n".join(text_lines)[:4000],
                              color=discord.Color(DEFAULT_EMBED_COLOR))
        await ch.send(embed=embed)
    else:
        from telegram_bot import bot as tg
        from message_relay import escape_html
        chat_id, _, thread = str(channel_key).partition(":")
        body = "\n".join(escape_html(l) for l in text_lines)
        text = (f"<b>{escape_html(title)}</b>\n{body}" if title else body)[:4000]
        await tg.send_message(int(chat_id), text, parse_mode="HTML",
                              message_thread_id=int(thread) if thread and int(thread) else None)
