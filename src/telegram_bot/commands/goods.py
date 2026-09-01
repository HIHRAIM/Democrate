"""Goods and what one person does with them on Telegram: creating a good,
crafting it, the inventory, selling, the standing orders, professions and
gifts.

`/craft` starts a timed run and returns immediately — the batch is announced
later by main.py: production_loop in the chat the run was started in, which is
why the command stores a notify key rather than awaiting anything. Energy is
charged up front. Up to five goods may be on the bench at once, one run of
each, and the batch is divided between them.

A good belongs to a person or an enterprise; the bank named at creation only
*denominates* it — it prices the good and it is the currency `/sell` is paid
in, but it never buys anything itself.

`offer_sale_tg` is the body of both `/sell` and `/ent-sell`, and it only puts
the question: the goods and the money change hands in
telegram_bot/callbacks.py: handle_econ_consent, once the buyer has agreed.

Not this module's zone: enterprises and their warehouse
(commands/enterprises.py), the production formulas (economy/production.py) and
the rows (db/goods.py).
"""
import time

from aiogram.filters import Command
from aiogram.types import Message

import db
import economy
import stats
from message_relay import clean_display_name, escape_html
from utils import category_label, get_chat_lang, good_display_name, is_admin, localized

from telegram_bot.client import GROUP_CHAT_TYPES, _chat_key, _tg_user_label, router
from telegram_bot.dialogs import _quiz_target_label_tg, _resolve_tg_target

def _resolve_good_bank_tg(chat, currency):
    """Mirror of the Discord helper: the bank a new good is denominated in."""
    banks = db.get_banks_in_union(chat["union_code"])
    if currency:
        bank = db.get_bank(currency.strip().upper())
        if not bank:
            return None, "bank_not_found"
        if bank["union_code"] != chat["union_code"]:
            return None, "create_good_wrong_union"
        return bank, None
    if len(banks) == 1:
        return banks[0], None
    return None, "create_good_need_bank"

def _normalize_category(raw):
    """(category|None, ok). Base-category key, case-insensitive; '-'
    clears."""
    s = (raw or "").strip().lower()
    if not s or s == "-":
        return None, True
    return (s, True) if s in db.GOOD_CATEGORIES else (None, False)

@router.message(Command("create_good", "create-good"))
async def create_good_tg(message: Message):
    """/create-good <name> | <base_value> | <energy> | [emoji] | [category]
    | [currency] | [enterprise] — the good belongs to the caller (or their
    enterprise) and is denominated in a bank of this group's union."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return
    body = (message.text or "").split(maxsplit=1)
    fields = [p.strip() for p in body[1].split("|")] if len(body) > 1 else []
    if len(fields) < 3:
        await message.reply(localized("create_good_usage", lang))
        return
    chat = db.get_chat(str(message.chat.id))
    if not chat:
        await message.reply(localized("chat_not_setup", lang))
        return
    currency = fields[5] if len(fields) > 5 else None
    bank, err = _resolve_good_bank_tg(chat, currency)
    if err:
        await message.reply(localized(err, lang))
        return
    cat, ok = _normalize_category(fields[4] if len(fields) > 4 else None)
    if not ok:
        await message.reply(localized("create_good_bad_category", lang,
                                      categories=", ".join(db.GOOD_CATEGORIES)))
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    if len(fields) > 6 and fields[6]:
        ent = db.find_enterprise(fields[6])
        if not ent:
            await message.reply(localized("enterprise_not_found", lang))
            return
        if not db.is_enterprise_leader(ent["code"], "telegram", message.from_user.id):
            await message.reply(localized("ent_not_leader", lang))
            return
        owner = db.enterprise_owner(ent["code"])
    name = clean_display_name(fields[0], max_len=40)
    base = economy.parse_amount(fields[1])
    try:
        energy_cost = int(fields[2])
    except ValueError:
        energy_cost = 0
    if base is None or energy_cost <= 0:
        await message.reply(localized("create_good_bad", lang))
        return
    emoji = fields[3].split()[0][:16] if len(fields) > 3 and fields[3].split() else ""
    gcode = db.create_good(bank["code"], name, base, energy_cost, emoji,
                           owner=owner, category=cat)
    await message.reply(localized("create_good_created", lang, name=name, code=gcode,
                                  value=economy.format_money(base, bank),
                                  energy=economy.format_energy(energy_cost), bank=bank["code"]))
    if cat:
        await message.answer(localized("create_good_category_note", lang,
                                       category=category_label(cat, lang)))

def _fmt_duration(seconds):
    """mm:ss for a production wait."""
    seconds = max(int(seconds), 0)
    return f"{seconds // 60}:{seconds % 60:02d}"

def _resolve_production_target_tg(message, enterprise_query):
    """(owner, server, error_key) of a production request — Telegram mirror."""
    if enterprise_query:
        ent = db.find_enterprise(enterprise_query)
        if not ent:
            return None, None, "enterprise_not_found"
        if not db.is_enterprise_worker(ent["code"], "telegram", message.from_user.id):
            return None, None, "ent_not_worker"
        return db.enterprise_owner(ent["code"]), (ent["platform"], ent["server_id"]), None
    owner = db.canonical_user("telegram", message.from_user.id)
    server = ("telegram", str(message.chat.id)) \
        if message.chat.type in GROUP_CHAT_TYPES else (None, None)
    return owner, server, None

@router.message(Command("craft"))
async def craft_tg(message: Message):
    """/craft <good_code> [enterprise] — start a timed production run."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("craft_usage", lang))
        return
    good = db.get_good(parts[1].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    owner, server, err = _resolve_production_target_tg(
        message, parts[2] if len(parts) > 2 else None)
    if err:
        await message.reply(localized(err, lang))
        return
    worker = db.canonical_user("telegram", message.from_user.id)
    notify = ("telegram", _chat_key(message))
    status, info = economy.start_production(
        worker, good, owner, server, notify, lang,
        starter_display=_tg_user_label(message.from_user))
    if status == "busy":
        await message.reply(localized("production_busy", lang,
                                      name=good_display_name(good, lang),
                                      time=_fmt_duration(info["finish_at"] - int(time.time()))))
        return
    if status == "too_many":
        await message.reply(localized("production_too_many", lang, limit=info["limit"]))
        return
    if status == "no_energy":
        await message.reply(localized("craft_no_energy", lang,
                                      cost=economy.format_energy(info["cost"]),
                                      have=economy.format_energy(info["have"])))
        return
    emoji = f" {good['emoji']}" if good["emoji"] else ""
    started = localized("production_started", lang,
                        name=good_display_name(good, lang), emoji=emoji,
                        qty=info["qty"], duration=_fmt_duration(info["duration"]),
                        cost=economy.format_energy(info["cost"]))
    if info.get("first_run"):
        started += "\n" + localized("production_first_run", lang)
    await message.reply(started)

@router.message(Command("inventory"))
async def inventory_tg(message: Message):
    """Show your crafted goods."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    inv = db.get_inventory(owner)
    if not inv:
        await message.reply(localized("inventory_empty", lang))
        return
    lines = [f"<b>{escape_html(localized('inventory_title', lang))}</b>"]
    for row in inv:
        m = db.get_produced(owner, row["good_code"])
        good = db.get_good(row["good_code"])
        bank = db.get_bank(row["bank_code"]) if row["bank_code"] else None
        emoji = f"{row['emoji']} " if row["emoji"] else ""
        name = good_display_name(good, lang) if good else row["name"]
        level = economy.mastery_level(m)
        if good and bank:
            lines.append(escape_html(localized(
                "inventory_line", lang, emoji=emoji, name=name, code=row["good_code"],
                qty=row["qty"], level=level, max_level=economy.max_level(),
                value=economy.format_money(economy.unit_value(good, m), bank))))
        else:
            lines.append(escape_html(localized(
                "inventory_line_base", lang, emoji=emoji, name=name,
                code=row["good_code"], qty=row["qty"], level=level,
                max_level=economy.max_level())))
    await message.reply("\n".join(lines), parse_mode="HTML")

async def _resolve_buyer_tg(message, target):
    """(buyer_owner, display, (kind, key)) for whoever is being sold to: an
    enterprise by code, or a Telegram account by reply, @username or id."""
    ent = db.find_enterprise(target) if target else None
    if ent:
        return (db.enterprise_owner(ent["code"]), f"{ent['name']} [{ent['code']}]",
                ("enterprise", ent["code"]))
    uid = await _resolve_tg_target(message, target)
    if uid is None:
        return None, None, None
    return (db.canonical_user("telegram", uid), await _quiz_target_label_tg(uid),
            ("user", str(uid)))

async def offer_sale_tg(message, lang, seller, good, want, target_arg, price_arg):
    """The body behind `/sell` and `/ent-sell`: resolve the buyer and the price,
    then put the offer to them with consent buttons.

    Nothing moves here. The goods and the money change hands inside
    telegram_bot/callbacks.py: handle_econ_consent once the buyer has pressed
    accept, which is also where the sale can still fail on funds."""
    from telegram_bot.dialogs import _econ_consent_keyboard, _register_econ_consent

    if db.is_base_good(good["code"]):
        await message.reply(localized("sell_not_sellable", lang,
                                      name=good_display_name(good, lang)))
        return
    have = db.get_inventory_qty(seller, good["code"])
    if want is None or want <= 0:
        want = have
    want = min(want, have)
    if want <= 0:
        await message.reply(localized("sell_nothing", lang, name=good["name"]))
        return
    buyer, buyer_name, consent = await _resolve_buyer_tg(message, target_arg)
    if buyer is None:
        await message.reply(localized("sell_bad_target", lang))
        return
    if buyer == seller:
        await message.reply(localized("sell_self", lang))
        return
    price = economy.parse_amount(price_arg) if price_arg \
        else economy.asking_price(seller, good, want)
    if price is None:
        await message.reply(localized("bad_amount", lang))
        return
    bank = db.get_bank(good["bank_code"]) if good["bank_code"] else None
    if price > 0 and not bank:
        await message.reply(localized("sell_no_bank", lang))
        return
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    token = _register_econ_consent({
        "action": "sell", "seller": list(seller), "buyer_owner": list(buyer),
        "buyer_kind": consent[0], "buyer": consent[1], "buyer_name": buyer_name,
        "good": good["code"], "qty": want, "price": price,
        "bank": bank["code"] if bank else None, "lang": lang})
    await message.answer(
        localized("sell_offer", lang, qty=want, emoji=emoji,
                  name=good_display_name(good, lang), buyer=buyer_name,
                  price=economy.format_money(price, bank) if bank
                  else localized("export_free", lang)),
        reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("sell"))
async def sell_tg(message: Message):
    """/sell <good_code> <buyer> [qty] [price] — sell a good to another member
    or to an enterprise.

    No bank buys goods: the units go to the buyer and the money comes out of
    their account, in the good's own currency. The buyer has to accept."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("sell_usage", lang))
        return
    good = db.get_good(parts[1].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    qty = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
    await offer_sale_tg(message, lang, db.canonical_user("telegram", message.from_user.id),
                        good, qty, parts[2], parts[4] if len(parts) > 4 else None)

@router.message(Command("autocraft"))
async def autocraft_tg(message: Message):
    """/autocraft <good_code> <on|off> [enterprise] — 24/7 autoproduction."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("autocraft_usage", lang))
        return
    good = db.get_good(parts[1].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    owner, server, err = _resolve_production_target_tg(
        message, parts[3] if len(parts) > 3 else None)
    if err:
        await message.reply(localized(err, lang))
        return
    worker = db.canonical_user("telegram", message.from_user.id)
    on = parts[2].strip().lower() in ("on", "enable", "1", "true", "yes")
    others, limit = economy.autocraft_slots(owner, good["code"])
    if on and others >= limit:
        await message.reply(localized("autocraft_too_many", lang, limit=limit))
        return
    db.set_autocraft(owner, good["code"], on,
                     starter=(worker[1], worker[2]), server=server)
    await message.reply(localized("autocraft_on" if on else "autocraft_off", lang,
                                  name=good_display_name(good, lang),
                                  lines=others + 1 if on else others))

@router.message(Command("autosend"))
async def autosend_tg(message: Message):
    """Send a share of a good's stock to a user, party or enterprise every
    day. `0` stops it."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 4:
        await message.reply(localized("autosend_usage", lang))
        return
    good = db.get_good(parts[1].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    if not parts[2].isdigit() or int(parts[2]) > 100:
        await message.reply(localized("autosend_bad_percent", lang))
        return
    percent = int(parts[2])
    owner = db.canonical_user("telegram", message.from_user.id)
    target_arg = parts[3]
    party = db.get_party(target_arg.strip().upper())
    ent = db.get_enterprise(target_arg.strip().upper())
    if party:
        tgt, tname = db.party_owner(party["code"]), f"{party['name']} [{party['code']}]"
    elif ent:
        tgt, tname = db.enterprise_owner(ent["code"]), f"{ent['name']} [{ent['code']}]"
    else:
        tid = await _resolve_tg_target(message, target_arg)
        if tid is None:
            await message.reply(localized("autosend_bad_target", lang))
            return
        tgt = db.canonical_user("telegram", tid)
        tname = await _quiz_target_label_tg(tid)
    if percent == 0:
        db.set_autosend(owner, good["code"], 0, tgt)
        await message.reply(localized("autosend_off", lang, name=good["name"]))
        return
    db.set_autosend(owner, good["code"], percent, tgt)
    await message.reply(localized("autosend_on", lang, name=good["name"], percent=percent,
                                  target=tname))

@router.message(Command("set_profession", "set-profession"))
async def set_profession_tg(message: Message):
    """Override a member's profession in your bank (bank leaders).

    Professions matter because wages are attached to them; without an override one
    is derived from what somebody has produced most of."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("set_profession_usage", lang))
        return
    good = db.get_good(parts[2].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    if not (db.is_bank_leader(good["bank_code"], "telegram", message.from_user.id)
            or is_admin("telegram", message.from_user.id)):
        await message.reply(localized("bank_not_leader", lang))
        return
    tid = await _resolve_tg_target(message, parts[1])
    if tid is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    db.set_profession(db.canonical_user("telegram", tid), good["bank_code"], good["code"])
    disp = await _quiz_target_label_tg(tid)
    await message.reply(localized("set_profession_done", lang, user=escape_html(disp),
                                  name=good["name"]))

@router.message(Command("give_good", "give-good"))
async def give_good_tg(message: Message):
    """/give-good <user> <good_code> [qty] — or a reply with
    /give-good <good_code> [qty]."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        if not parts:
            await message.reply(localized("give_good_usage", lang))
            return
        good_s = parts[0]
        qty_s = parts[1] if len(parts) > 1 else "1"
    else:
        if len(parts) < 2:
            await message.reply(localized("give_good_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        good_s = parts[1]
        qty_s = parts[2] if len(parts) > 2 else "1"
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    good = db.get_good(good_s.strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    if not qty_s.isdigit() or int(qty_s) <= 0:
        await message.reply(localized("bad_amount", lang))
        return
    qty = int(qty_s)
    owner = db.canonical_user("telegram", message.from_user.id)
    target = db.canonical_user("telegram", target_id)
    if target == owner:
        await message.reply(localized("give_self", lang))
        return
    have = db.get_inventory_qty(owner, good["code"])
    if have < qty:
        await message.reply(localized("give_no_goods", lang, have=have))
        return
    db.add_inventory(owner, good["code"], -qty)
    db.add_inventory(target, good["code"], qty)
    disp = await _quiz_target_label_tg(target_id)
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    await message.reply(localized("give_done", lang, qty=qty, emoji=emoji,
                                  name=good_display_name(good, lang),
                                  user=escape_html(disp)))

@router.message(Command("goods"))
async def goods_tg(message: Message):
    """List the goods this group can produce, base goods included."""
    lang = get_chat_lang(_chat_key(message))
    lines = [f"<b>{escape_html(localized('goods_header', lang))}</b>"]
    for code, cat, emoji, _name in db.BASE_GOODS:
        good = db.get_good(code)
        if not good:
            continue
        lines.append(escape_html(localized(
            "goods_line_base", lang, emoji=emoji, name=good_display_name(good, lang),
            code=code, energy=economy.format_energy(good["energy_cost"]))))
    chat = db.get_chat(str(message.chat.id)) \
        if message.chat.type in GROUP_CHAT_TYPES else None
    if chat:
        for bank in db.get_banks_in_union(chat["union_code"]):
            for good in db.get_bank_goods(bank["code"]):
                if db.is_base_good(good["code"]):
                    continue
                emoji = f"{good['emoji']} " if good["emoji"] else ""
                cat_disp = category_label(good["category"], lang) if good["category"] else "—"
                lines.append(escape_html(localized(
                    "goods_line", lang, emoji=emoji, name=good["name"], code=good["code"],
                    category=cat_disp,
                    value=economy.format_money(good["base_value"], bank),
                    energy=economy.format_energy(good["energy_cost"]))))
    await message.reply("\n".join(lines)[:4000], parse_mode="HTML")

@router.message(Command("top"))
async def top_tg(message: Message):
    """Show one of the three leaderboards: /top [week|good|wealth] [code]."""
    lang = get_chat_lang(_chat_key(message))
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("top_no_server", lang))
        return
    parts = (message.text or "").split()[1:]
    kind = (parts[0].lower() if parts else "week")
    code = parts[1] if len(parts) > 1 else None
    title, lines = stats.leaderboard(kind, "telegram", "telegram",
                                     str(message.chat.id), lang, code)
    if title is None:
        await message.reply(localized("top_bad_kind", lang))
        return
    if not lines:
        await message.reply(localized("top_none", lang))
        return
    body = [f"<b>{escape_html(title)}</b>"] + [escape_html(line) for line in lines]
    await message.reply("\n".join(body)[:4000], parse_mode="HTML")
