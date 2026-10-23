"""Server-level economic reporting: the content of the posts the bot publishes
into the channels chosen with /server logs and /server tasks.

Three posts live here — the daily FX update (currency values with day-over-day
change), the weekly server statistics (provision, goods, GDP, task progress)
and the monthly task announcement (with the previous month's verdict). The
builders return plain localized strings; `post_to_channel` renders them on
either platform (an embed on Discord, HTML on Telegram) so `main.py`'s
schedulers stay platform-neutral.

`union_profile` is the content of `/union info`: what a union is, counted, and
`union_list` the full lists behind those counts and the union's economic
summary, paged by `list_page`; `community_profile` is the same for one server
or group (/server info), and `transaction_lines` a person's journal
(/bank transactions). They return data rather than finished messages, because the Discord half shows
them as embeds with buttons and the Telegram half as HTML with an inline
keyboard, while what they say is one union fact for both.
"""
import logging
import time

import db
import economy
from utils import (
    localized, format_stored_user, get_chat_lang, category_label, language_flag,
    good_display_name, provision_level_label,
)

logger = logging.getLogger("dem.stats")

PROVISION_LEVEL_EMOJI = {
    "insufficient": "🔴",
    "acceptable": "🟡",
    "good": "🟢",
    "excellent": "🌟",
}

def channel_lang(row):
    """The language of a stats/tasks channel: the channel's own /server lang-channel if
    set, else the server default."""
    if row["platform"] == "discord":
        return get_chat_lang(f"{row['server_id']}:{row['channel_key']}")
    return get_chat_lang(row["channel_key"])

TOP_KINDS = ("week", "good", "wealth")
TOP_LIMIT = 10

def leaderboard(kind, viewer_platform, platform, server_id, lang, code=None):
    """`(title, lines)` of one `/good top` board, or `(title, [])` when nobody is on
    it yet — and `(None, None)` when the arguments do not name a board.

    Three of them, because they answer the three questions people actually ask
    of an economy: who has been busy lately (`week`, this server's production
    over seven days), who is the best at one thing (`good`, all-time mastery)
    and who is rich (`wealth`, balances in one currency). The first is the
    default because it is the one a newcomer can still get onto.

    Built here rather than in either half's command so that the two platforms
    cannot drift into showing different boards, and off the same
    `server_production` rows as the weekly statistics so that a board cannot
    disagree with the figures posted beside it."""
    def _name(row_platform, row_id, stored=None):
        return format_stored_user(viewer_platform, row_platform, row_id,
                                  stored or db.user_display_name(row_platform, row_id))

    def _shown(rows):
        """The board's rows without the people who asked in /account privacy to be
        left off, cut to TOP_LIMIT."""
        return [r for r in rows if not db.is_leaderboard_hidden(r["p"], r["i"])][:TOP_LIMIT]

    if kind == "week":
        rows = _shown(db.top_producers(platform, server_id, int(time.time()) - 7 * 86400,
                                       limit=TOP_LIMIT * 3))
        return (localized("top_header_week", lang),
                [localized("top_line_qty", lang, place=i, user=_name(r["p"], r["i"]),
                           qty=r["qty"])
                 for i, r in enumerate(rows, 1)])

    if kind == "good":
        good = db.get_good((code or "").strip().upper()) if code else None
        if not good:
            return None, None
        rows = _shown(db.top_masters(good["code"], limit=TOP_LIMIT * 3))
        return (localized("top_header_good", lang,
                          name=good_display_name(good, lang), code=good["code"]),
                [localized("top_line_qty", lang, place=i, user=_name(r["p"], r["i"]),
                           qty=r["produced"])
                 for i, r in enumerate(rows, 1)])

    if kind == "wealth":
        bank = db.get_bank((code or "").strip().upper()) if code else None
        if not bank:
            return None, None
        rows = _shown(db.top_balances(bank["code"], limit=TOP_LIMIT * 3))
        return (localized("top_header_wealth", lang, name=bank["currency_name"],
                          code=bank["code"]),
                [localized("top_line_money", lang, place=i,
                           user=_name(r["p"], r["i"], r["display_name"]),
                           amount=economy.format_money(r["balance"], bank))
                 for i, r in enumerate(rows, 1)])

    return None, None

def fx_lines(banks, lang):
    """One line per bank given: the published value, its change since
    yesterday's recompute, and how many people earned it and with how many
    messages on the day that just ended — what moved it."""
    lines = []
    for bank in banks:
        old, new = bank["prev_value"], bank["value"]
        if old:
            pct = (new - old) / old * 100.0
            arrow = "📈" if pct > 0.005 else ("📉" if pct < -0.005 else "➡️")
            change = f"{arrow} {pct:+.2f}%"
        else:
            change = "➡️"
        emoji = f"{bank['emoji']} " if bank["emoji"] else ""
        people, messages = economy.yesterday_activity(bank["code"])
        lines.append(localized("fx_log_line", lang, emoji=emoji,
                               name=bank["currency_name"], code=bank["code"],
                               value=f"{new:.4f}", change=change)
                     + " · " + localized("fx_log_activity", lang, people=people,
                                         messages=messages))
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

UNION_LISTS = ("wikis", "parties", "govt", "banks", "enterprises", "economy")
UNION_LIST_PAGE = 15

def _union_goods_count(banks):
    """The goods a union's communities can work with — the base goods plus
    the market goods its banks denominate — counted the way /good info lists
    them."""
    base = sum(1 for code, *_rest in db.BASE_GOODS if db.get_good(code))
    market = sum(1 for bank in banks for good in db.get_bank_goods(bank["code"])
                 if not db.is_base_good(good["code"]))
    return base + market

def platform_counts(counts, platform):
    """'Discord: 3, Telegram: 2' — or, read on Discord with
    config.DISCORD_EMOJI / TELEGRAM_EMOJI set, '<:discord:…> 3,
    <:telegram:…> 2'. A platform with no communities is left out, and an
    emoji is used only on Discord, the one place that can draw it; either
    setting may be absent from config.py."""
    import config
    parts = []
    for key, name, setting in (("discord", "Discord", "DISCORD_EMOJI"),
                               ("telegram", "Telegram", "TELEGRAM_EMOJI")):
        if not counts.get(key):
            continue
        emoji = getattr(config, setting, None) if platform == "discord" else None
        parts.append(f"{emoji} {counts[key]}" if emoji else f"{name}: {counts[key]}")
    return ", ".join(parts)

def union_profile(code, lang, platform="telegram"):
    """The public profile of a union as data both halves render: a dict with
    title, code, logo (bytes or None), logo_mime, sections and lists.

    `sections` is a list of (title, [(label, value)]) in the order a reader
    wants them. The first has no title — the union's language, creation
    date and communities sit right under the name, which already carries
    the code. The wikis follow only when the union has a wiki, politics only
    when it has a party or a governing body, then the economy. `lists` are
    the kinds of UNION_LISTS that have anything in them, which is what the
    buttons under the profile offer; `platform` decides how the communities
    are counted (`platform_counts`).

    Only counts are shown; the full lists are `union_list`, and every count
    here is the length of the list of the same name, so the two never
    disagree. A bank is a currency in this economy, so banks and currencies
    are one figure. The owner of a sponsored union is left out on purpose —
    who pays for a union is not a fact about the union."""
    from utils import format_date, language_name
    union = db.get_union(code)
    wiki_rows = db.get_union_wikis(code)
    banks = db.get_banks_in_union(code)
    enterprises = db.get_union_enterprises(code)
    parties_on = db.get_party_settings(code)[0]
    parties = db.get_union_parties(code) if parties_on else []
    bodies = db.get_govt_bodies(code)
    union_language = db.union_lang(code)
    created = format_date(union["created_at"], lang) if union and union["created_at"] else "—"
    wiki_langs = {}
    for row in wiki_rows:
        key = (row["lang"] or "?").split("-")[0]
        wiki_langs[key] = wiki_langs.get(key, 0) + 1
    langs_text = ", ".join(f"{language_name(k)} — {n}" for k, n in
                           sorted(wiki_langs.items(), key=lambda kv: (-kv[1], kv[0]))) or "—"
    label = lambda key: localized(f"union_profile_{key}", lang)
    sections = [(None, [
        (label("lang"), f"{localized('lang_flag', union_language)} {language_name(union_language)}"),
        (label("created"), created),
        (label("communities"), platform_counts(db.union_community_counts(code), platform) or "—"),
    ])]
    if wiki_rows:
        sections.append((label("section_wikis"), [
            (label("wikis"), str(len(wiki_rows))),
        ] + ([(label("wiki_langs"), langs_text)] if len(wiki_langs) > 1 else []) + [
            (label("staff"), str(db.union_staff_count(code))),
        ]))
    if parties or bodies:
        sections.append((label("section_politics"),
                         ([(label("parties"), str(len(parties)))] if parties_on else []) + [
            (label("govt"), str(len(bodies))),
        ]))
    sections.append((label("section_economy"), [
        (label("banks"), str(len(banks))),
        (label("accounts"), str(sum(db.bank_account_count(b["code"]) for b in banks))),
        (label("enterprises"), str(len(enterprises))),
        (label("goods"), str(_union_goods_count(banks))),
    ]))
    present = {"wikis": wiki_rows, "parties": parties, "govt": bodies, "banks": banks,
               "enterprises": enterprises, "economy": banks}
    return {
        "title": db.get_union_name(code, lang),
        "code": code,
        "logo": union["logo"] if union else None,
        "logo_mime": union["logo_mime"] if union else None,
        "sections": sections,
        "lists": [kind for kind in UNION_LISTS if present[kind]],
    }

def find_union(query, lang):
    """(union code or None, [suggested codes]) for what somebody typed into
    `/union info`: a code or a name in any of the union's languages, the
    same way `/party info` finds a party (utils.search_by_name). A query
    that resembles no union finds nothing."""
    from utils import search_by_name
    codes = db.get_union_codes()
    exact, close = search_by_name(
        query, codes, lambda code: [code, *db.get_union_names(code).values()])
    return exact, close

def union_choice_line(code, lang):
    """How a suggested union is written in the numbered choice."""
    return f"{db.get_union_name(code, lang)} [{code}]"

def union_list(code, kind, lang):
    """(title, lines) of one full list of a union — `kind` is one of
    UNION_LISTS — as plain text lines both halves page through with
    `list_page`."""
    import celebrations
    lines = []
    if kind == "wikis":
        for row in db.get_union_wikis(code):
            flag = language_flag(celebrations.wiki_base_lang(row))
            lines.append(flag + " " + localized("union_list_wiki_line", lang,
                                   name=row["name"] or row["base_url"],
                                   url=row["main_url"] or row["base_url"]))
    elif kind == "parties":
        for party in db.get_union_parties(code):
            line = localized("union_list_party_line", lang, name=party["name"], code=party["code"],
                             members=len(db.get_party_user_set(party["code"])))
            lines.append(f"{line} ⏸️" if party["suspended"] else line)
    elif kind == "govt":
        for body in db.get_govt_bodies(code):
            lines.append(localized("union_list_govt_line", lang, name=body["name"],
                                   members=len(db.get_govt_members(body["id"]))))
    elif kind == "banks":
        for bank in db.get_banks_in_union(code):
            emoji = f"{bank['emoji']} " if bank["emoji"] else ""
            lines.append(localized("union_list_bank_line", lang, emoji=emoji,
                                   name=bank["currency_name"], code=bank["code"],
                                   accounts=db.bank_account_count(bank["code"])))
    elif kind == "economy":
        lines = union_economy_lines(code, lang)
    elif kind == "enterprises":
        for ent in db.get_union_enterprises(code):
            community = db.chat_title(ent["server_id"])
            line = (localized("union_list_enterprise_line", lang, name=ent["name"],
                              code=ent["code"], community=community) if community
                    else f"{ent['name']} [{ent['code']}]")
            if ent["neutral"]:
                line = f"{line} · {localized('ent_neutral_title', lang)}"
            lines.append(line)
    if kind != "economy":
        lines = [f"{number}. {line}" for number, line in enumerate(lines, 1)]
    title = localized("union_list_title", lang, union=db.get_union_name(code, lang),
                      kind=localized(f"union_list_kind_{kind}", lang))
    return title, lines

def list_page(lines, page, size=UNION_LIST_PAGE):
    """(the lines of page `page`, the page clamped into range, the number of
    pages) — at least one page, even for an empty list."""
    total = max(1, -(-len(lines) // size))
    page = min(max(0, int(page)), total - 1)
    return lines[page * size:(page + 1) * size], page, total

async def refresh_union_titles(code=None):
    """Ask both platforms for the current names of a union's communities —
    or of every community, without a code — and write them to
    `chats.title`, which also renames a neutral enterprise still carrying
    the bot's own name. Lists then name places rather than numbers. A
    Discord server is asked through the client's cache, a Telegram group
    through the API; either half is imported at the call site, as
    everywhere a platform-neutral module needs one."""
    from discord_bot.client import chat_display_name as discord_name
    from telegram_bot.client import chat_display_name as telegram_name
    chats = db.get_union_chats(code) if code else db.all_chats()
    for chat in chats:
        try:
            if chat["platform"] == "discord":
                discord_name(chat["chat_id"])
            elif chat["platform"] == "telegram":
                await telegram_name(chat["chat_id"])
        except Exception as e:
            logger.info("Could not refresh the name of %s: %s", chat["chat_id"], e)

TX_REASONS = ("message", "pay", "sell", "fine", "wage", "salary", "party_dues", "emit",
              "convert_in", "convert_out", "convert_fee", "consumption", "export",
              "export_escrow", "shipment_refund", "link_merge", "tax_community", "tax_bank")
TX_HISTORY = 200

def _owner_name(owner_type, platform, owner_id, viewer_platform, lang):
    """What to call the other side of a transaction."""
    if owner_type == "user":
        return format_stored_user(viewer_platform, platform, owner_id,
                                  db.user_display_name(platform, owner_id))
    if owner_type == "party":
        party = db.get_party(owner_id)
        return f"{party['name']} [{owner_id}]" if party else owner_id
    if owner_type == "enterprise":
        ent = db.get_enterprise(owner_id)
        return f"{ent['name']} [{owner_id}]" if ent else owner_id
    return localized("tx_bank", lang, code=owner_id)

def _earn_place(platform, chan_key, lang):
    """(how to name an earning channel, the community it is in or None).

    A Discord key is a channel or a category, named through the Discord
    client in this process; one it cannot see any more is named by its id.
    A Telegram key is 'group:topic', named after the group's stored title,
    with the topic's number — the Bot API has no way to read a topic's
    name."""
    if platform == "discord":
        from discord_bot import bot as dc
        try:
            channel = dc.get_channel(int(chan_key))
        except (TypeError, ValueError):
            channel = None
        if channel is None or getattr(channel, "guild", None) is None:
            return localized("earn_info_unknown_channel", lang, id=chan_key), None
        mark = "📁 " if str(getattr(channel, "type", "")) == "category" else "#"
        return f"{mark}{channel.name} ({channel.guild.name})", str(channel.guild.id)
    group, _sep, topic = str(chan_key).partition(":")
    title = db.chat_title(group) or group
    if topic and topic != "0":
        return localized("earn_info_topic", lang, group=title, topic=topic), group
    return title, group

def earn_channel_lines(rows, lang):
    """One entry per earning channel for /bank earn-info, each a few lines:
    where it is, which currency at what multiplier and under what community
    tax; how many messages it counted and how much currency it minted over
    the last seven days; and who switched it on and when (unknown for
    channels switched on before that was recorded)."""
    from utils import format_date
    entries = []
    for row in rows:
        bank = db.get_bank(row["bank_code"])
        if not bank:
            continue
        place, community = _earn_place(row["platform"], row["chan_key"], lang)
        tax = db.community_tax(row["platform"], community) if community else 0
        emoji = f"{bank['emoji']} " if bank["emoji"] else ""
        messages, minted = db.earn_stats_since(row["platform"], row["chan_key"])
        lines = [
            localized("earn_info_line", lang, place=place,
                      currency=f"{emoji}{bank['currency_name']} [{bank['code']}]",
                      rate=f"{float(row['rate'] or 0):g}", tax=economy.format_percent(tax)),
            localized("earn_info_stats", lang, messages=messages,
                      amount=economy.format_money(minted, bank, lang)),
        ]
        if row["set_at"]:
            lines.append(localized("earn_info_set", lang, user=row["set_by_name"] or "—",
                                   date=format_date(row["set_at"], lang)))
        else:
            lines.append(localized("earn_info_set_unknown", lang))
        entries.append("\n".join(lines))
    return entries

def transaction_lines(owner, viewer_platform, lang, bank_code=None):
    """A person's journal, newest first, one line each: the date, the amount
    signed from their side, the other side, and what it was. The most recent
    TX_HISTORY rows are read; `bank_code` narrows them to one currency."""
    from utils import utc_from_epoch
    rows = db.get_transactions(owner=owner, limit=TX_HISTORY)
    ot, op, oi = owner
    lines = []
    for row in rows:
        if bank_code and row["bank_code"] != bank_code:
            continue
        bank = db.get_bank(row["bank_code"])
        incoming = (row["to_type"], row["to_platform"], str(row["to_id"])) == (ot, op, str(oi))
        other = ((row["from_type"], row["from_platform"], row["from_id"]) if incoming
                 else (row["to_type"], row["to_platform"], row["to_id"]))
        amount = economy.format_money(row["amount"], bank, lang) if bank else str(row["amount"])
        base, _, detail = (row["reason"] or "").partition(":")
        label = localized(f"tx_reason_{base}", lang) if base in TX_REASONS else base
        if detail.strip():
            label = f"{label}: {detail.strip()}"
        lines.append(localized(
            "tx_line", lang, date=utc_from_epoch(row["created_at"]).strftime("%Y-%m-%d"),
            amount=("+" if incoming else "−") + amount,
            other=_owner_name(*other, viewer_platform, lang), what=label))
    return lines

def union_economy_lines(code, lang):
    """The economic summary of a union, a few lines per currency: the money
    supply and its net change over seven days, the debts, the share of the
    richest tenth, and the turnover between holders over seven days."""
    since = int(time.time()) - 7 * 86400
    lines = []
    for bank in db.get_banks_in_union(code):
        flow = db.bank_flow_since(bank["code"], since)
        share = db.wealth_top_share(bank["code"])
        change = flow["issued"] - flow["burned"]
        sign = "+" if change >= 0 else "−"
        lines.append(f"{bank['emoji'] + ' ' if bank['emoji'] else ''}{bank['currency_name']} [{bank['code']}]")
        lines.append(localized("econ_supply", lang,
                               amount=economy.format_money(db.bank_money_supply(bank["code"]), bank),
                               change=sign + economy.format_money(abs(change), bank, lang)))
        lines.append(localized("econ_debt", lang,
                               amount=economy.format_money(db.bank_debt(bank["code"]), bank)))
        lines.append(localized("econ_top_share", lang,
                               share=f"{share * 100:.0f}%" if share is not None else "—"))
        lines.append(localized("econ_turnover", lang,
                               amount=economy.format_money(flow["turnover"], bank)))
        lines.append("")
    return lines[:-1] if lines else lines

def _birthday_row(platform, server_id, lang):
    """The profile's birthday row — the founding date, and the invitation
    beside it — when the community's administrators added one."""
    from utils import format_date
    row = db.get_community(platform, server_id)
    if not row or row["founded_at"] is None:
        return []
    value = format_date(row["founded_at"], lang)
    if row["invite_url"]:
        value += f" · {row['invite_url']}"
    return [(localized("server_profile_birthday", lang), value)]

def _central_banks(server_id):
    """The banks whose central server is this server or group."""
    return [b for b in db.get_all_banks()
            if (b["central_chat"] or "").split(":", 1)[0] == str(server_id)]

def community_currencies(platform, server_id, earn_keys=None):
    """The codes of the currencies used in one server or group: the banks
    centred on it and the currencies its channels earn — what /server info
    calls the currencies in use and what its daily currency post shows.
    `earn_keys` are the community's channel keys on Discord, where an
    earning binding is keyed by a bare channel id the half has to
    recognise; on Telegram a key carries the group id and is found here."""
    if platform == "telegram":
        keys = [r["chan_key"] for r in db.cur.execute(
            "SELECT chan_key FROM earn_channels WHERE platform='telegram' AND chan_key LIKE ?",
            (f"{server_id}:%",)).fetchall()]
    else:
        keys = [str(k) for k in (earn_keys or [])]
    earning = {r["bank_code"] for r in db.cur.execute(
        f"SELECT bank_code FROM earn_channels WHERE platform=? AND chan_key IN"
        f" ({','.join('?' for _ in keys) or 'NULL'})", (platform, *keys)).fetchall()}
    return sorted({b["code"] for b in _central_banks(server_id)} | earning)

def community_profile(platform, server_id, lang, earn_keys=None):
    """The public profile of one server or group, as data both halves
    render: a dict with title and sections — the community itself, its
    economy (with the parts of it switched off here), its taxes.
    `earn_keys` are as in `community_currencies`."""
    from utils import format_tz_offset, language_name, get_chat_lang
    chat = db.get_chat(server_id)
    union = chat["union_code"] if chat else None
    central = _central_banks(server_id)
    currencies = community_currencies(platform, server_id, earn_keys)
    label = lambda key: localized(f"server_profile_{key}", lang)
    code = get_chat_lang(str(server_id))
    sections = [
        (label("section_community"), [
            (label("union"), f"{db.get_union_name(union, lang)} [{union}]" if union else "—"),
            (label("lang"), f"{localized('lang_flag', code)} {language_name(code)}"),
            (label("tz"), format_tz_offset(db.community_tz(platform, server_id))),
        ] + _birthday_row(platform, server_id, lang)),
        (label("section_economy"), [
            (label("banks"), str(len(central))),
            (label("currencies"), ", ".join(currencies) or "—"),
            (label("accounts"), str(sum(db.bank_account_count(b["code"]) for b in central))),
            (label("enterprises"), str(len(db.get_server_enterprises(platform, str(server_id))))),
        ] + [(label("econ_off"), off) for off in
             [economy.parts_text(economy.community_off(platform, server_id), lang)] if off]),
        (label("section_taxes"), [
            (label("tax"), economy.format_percent(db.community_tax(platform, server_id))),
        ] + [(localized("server_profile_bank_tax", lang, code=c),
              economy.format_percent(db.get_bank(c)["tax_percent"] or 0))
             for c in currencies if db.get_bank(c) and (db.get_bank(c)["tax_percent"] or 0)]),
    ]
    return {"title": db.chat_title(server_id) or localized("server_profile_untitled", lang),
            "sections": sections}
