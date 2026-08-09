"""Goods and what one person does with them on Telegram: creating a good,
crafting it, the inventory, selling, the standing orders, professions and
gifts.

`/craft` starts a timed run and returns immediately — the batch is announced
later by main.py: production_loop in the chat the run was started in, which is
why the command stores a notify key rather than awaiting anything. Energy is
charged up front.

A good belongs to a person or an enterprise; the bank named at creation only
*denominates* it, and is where `/sell` sends it.

Not this module's zone: enterprises and their warehouse
(commands/enterprises.py), the production formulas (economy/production.py) and
the rows (db/goods.py).
"""
import time

from aiogram.filters import Command
from aiogram.types import Message

import db
import economy
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
                                      time=_fmt_duration(info["finish_at"] - int(time.time()))))
        return
    if status == "no_energy":
        await message.reply(localized("craft_no_energy", lang,
                                      cost=economy.format_energy(info["cost"]),
                                      have=economy.format_energy(info["have"])))
        return
    emoji = f" {good['emoji']}" if good["emoji"] else ""
    await message.reply(localized("production_started", lang,
                                  name=good_display_name(good, lang), emoji=emoji,
                                  qty=info["qty"], duration=_fmt_duration(info["duration"]),
                                  cost=economy.format_energy(info["cost"])))

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
        bank = db.get_bank(row["bank_code"])
        val = economy.format_money(economy.unit_value(good, m), bank) if good and bank else "—"
        emoji = f"{row['emoji']} " if row["emoji"] else ""
        lines.append(escape_html(localized(
            "inventory_line", lang, emoji=emoji,
            name=good_display_name(good, lang) if good else row["name"],
            code=row["good_code"], qty=row["qty"], value=val,
            level=economy.mastery_level(m))))
    await message.reply("\n".join(lines), parse_mode="HTML")

@router.message(Command("sell"))
async def sell_tg(message: Message):
    """Sell a good back to its bank at the current unit value.

    The economy's money source — the bank mints the proceeds. Base goods carry no
    market value and cannot be sold."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("sell_usage", lang))
        return
    good = db.get_good(parts[1].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    have = db.get_inventory_qty(owner, good["code"])
    want = have
    if len(parts) > 2 and parts[2].isdigit() and int(parts[2]) > 0:
        want = int(parts[2])
    status, info = economy.sell(owner, good, want)
    if status == "not_sellable":
        await message.reply(localized("sell_not_sellable", lang,
                                      name=good_display_name(good, lang)))
        return
    if status == "nothing":
        await message.reply(localized("sell_nothing", lang, name=good["name"]))
        return
    bank = db.get_bank(good["bank_code"])
    await message.reply(localized("sell_done", lang, qty=info["qty"], name=good["name"],
                                  unit=economy.format_money(info["unit"], bank),
                                  total=economy.format_money(info["total"], bank)))

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
    db.set_autocraft(owner, good["code"], on,
                     starter=(worker[1], worker[2]), server=server)
    await message.reply(localized("autocraft_on" if on else "autocraft_off", lang,
                                  name=good_display_name(good, lang)))

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
