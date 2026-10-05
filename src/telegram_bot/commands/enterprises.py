"""Enterprises on Telegram: founding one, its card, the `/edit_enterprise`
menu, its workers and positions, and everything it sells or exports.

An enterprise is a party with a warehouse and a payroll, and the commands
mirror that. Salaries come in two shapes and the difference matters — an exact
amount in minor units, or a percent of the period's sales stored as the number
the user typed.

An export always needs the *receiving* side's consent, which arrives as an
inline keyboard through `_pending_econ_consents`. Once accepted, the goods
leave the warehouse immediately and a priced sale escrows the payment; the
shipment lands later (economy/logistics.py, main.py: shipment_loop), which is
why these commands report an ETA rather than a result.

Not this module's zone: the transit physics (economy/logistics.py), payroll
(economy/payroll.py) and the rows (db/enterprises.py).
"""
import time

from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile

import db
import economy
from message_relay import clean_display_name, escape_html
from utils import (
    format_stored_user, get_chat_lang, good_display_name, is_admin,
    localized, rate_limit_ok, send_service_event,
)

from telegram_bot.client import GROUP_CHAT_TYPES, _chat_key, _tg_user_label, router, is_server_admin
from telegram_bot.dialogs import (
    _dialog_logo_tg, _dialog_text_tg, _econ_consent_keyboard,
    _extract_logo_tg, _numbered_choice_tg, _parse_tg_user_ref,
    _quiz_target_label_tg, _register_econ_consent, _resolve_tg_target,
    _wait_user_message,
)
from telegram_bot.parties import _party_code_validator

def _fmt_eta(seconds):
    """mm:ss for short waits, h:mm:ss once a shipment runs into hours."""
    seconds = max(int(seconds), 0)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"

def _ent_line_tg(ent):
    """One line naming an enterprise in a list — 'Name [CODE]'."""
    return f"{ent['name']} [{ent['code']}]"

async def _resolve_led_enterprise_tg(message, lang, query=None):
    """The enterprise the caller leads: the named one, the only one, or a
    numbered choice among several."""
    if query:
        ent = db.find_enterprise(query)
        if not ent:
            await message.reply(localized("enterprise_not_found", lang))
            return None
        neutral_admin = (ent["neutral"] and ent["platform"] == "telegram"
                         and str(message.chat.id) == ent["server_id"]
                         and await is_server_admin(message.chat.id, message.from_user.id))
        if not (db.is_enterprise_leader(ent["code"], "telegram", message.from_user.id)
                or is_admin("telegram", message.from_user.id) or neutral_admin):
            await message.reply(localized("ent_not_leader", lang))
            return None
        return ent
    ents = db.user_led_enterprises("telegram", message.from_user.id)
    if (message.chat.type in GROUP_CHAT_TYPES
            and await is_server_admin(message.chat.id, message.from_user.id)):
        ents += [e for e in db.get_server_enterprises("telegram", message.chat.id)
                 if e["neutral"] and e["code"] not in {x["code"] for x in ents}]
    if not ents:
        await message.reply(localized("ent_none_led", lang))
        return None
    if len(ents) == 1:
        return ents[0]
    return await _numbered_choice_tg(message, lang, localized("ent_choose", lang),
                                     ents, _ent_line_tg)

@router.message(Command("add_enterprise", "add-enterprise"))
async def add_enterprise_tg(message: Message):
    """Found an enterprise on this group through a dialog: name, code,
    description, logo.

    The group it is founded on is fixed for life — it decides which monthly task
    the enterprise works toward and how far its exports travel."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return
    if not db.is_setup(str(message.chat.id)):
        await message.reply(localized("chat_not_setup", lang))
        return
    if not rate_limit_ok(f"addent|telegram|{message.from_user.id}", 3, 86400):
        await message.reply(localized("rate_limited", lang))
        return
    m = await _dialog_text_tg(message, lang, localized("add_ent_ask_name", lang))
    if m is None:
        return
    name = clean_display_name(m.text or "", max_len=60)
    code = await _dialog_text_tg(message, lang, localized("add_ent_ask_code", lang),
                                 validator=_party_code_validator,
                                 error_key="add_ent_bad_code")
    if code is None:
        return
    m = await _dialog_text_tg(message, lang, localized("add_ent_ask_desc", lang))
    if m is None:
        return
    description = (m.text or "").strip()
    description = None if description == "-" else description[:500]
    await message.reply(localized("add_ent_ask_logo", lang))
    logo = logo_mime = None
    for _ in range(5):
        m = await _wait_user_message(message.chat.id, message.from_user.id)
        if m is None:
            await message.reply(localized("dialog_timeout", lang))
            return
        if (m.text or "").strip() == "-":
            break
        got = await _extract_logo_tg(m)
        if got:
            logo, logo_mime = got
            break
        await message.reply(localized("add_party_invalid_logo", lang))
    db.create_enterprise(code, name, "telegram", message.chat.id,
                         "telegram", message.from_user.id,
                         _tg_user_label(message.from_user),
                         description=description, logo=logo, logo_mime=logo_mime)
    await message.reply(localized("add_ent_created", lang, name=name, code=code))
    await send_service_event("enterprise_created", name=name, code=code,
                             user=_tg_user_label(message.from_user))

def _enterprise_card_tg(ent, lang):
    """The enterprise card as HTML: description, leaders, workers,
    positions and salaries, salary currency and period, stock and cargo in
    transit."""
    lines = [f"<b>{escape_html(ent['name'])} [{escape_html(ent['code'])}]</b>"]
    if ent["neutral"]:
        lines.append(f"<b>{escape_html(localized('ent_neutral_title', lang))}</b>: "
                     f"{escape_html(localized('ent_neutral_description', lang))}")
    if ent["description"]:
        lines.append(escape_html(ent["description"][:1000]))
    lines.append(f"<b>{escape_html(localized('ent_field_server', lang))}:</b> "
                 f"{escape_html(str(ent['server_id']))}")
    leaders = db.get_enterprise_leaders(ent["code"])
    shown = ", ".join(format_stored_user("telegram", l["platform"], l["user_id"],
                                         l["display_name"]) for l in leaders) or "—"
    lines.append(f"<b>{escape_html(localized('ent_field_leaders', lang))}:</b> {escape_html(shown)}")
    members = db.get_enterprise_members(ent["code"])
    if members:
        mlines = []
        for m in members[:15]:
            pos = f" — {m['position']}" if m["position"] else ""
            mlines.append(escape_html(format_stored_user(
                "telegram", m["platform"], m["user_id"], m["display_name"]) + pos))
        if len(members) > 15:
            mlines.append(f"… +{len(members) - 15}")
        lines.append(f"<b>{escape_html(localized('ent_field_workers', lang, count=len(members)))}:</b>\n"
                     + "\n".join(mlines))
    balances = []
    for acc in db.get_owner_accounts(*db.enterprise_owner(ent["code"])):
        bank = db.get_bank(acc["bank_code"])
        if bank:
            balances.append(escape_html(economy.format_money(acc["balance"], bank)))
    if balances:
        lines.append(f"<b>{escape_html(localized('ent_field_balance', lang))}:</b> "
                     + ", ".join(balances))
    period = localized(f"salary_period_{ent['salary_period'] or 'monthly'}", lang)
    lines.append(f"<b>{escape_html(localized('ent_field_salary', lang))}:</b> "
                 f"{escape_html(period)} · {escape_html(ent['salary_bank'] or '—')}")
    positions = db.get_enterprise_positions(ent["code"])
    if positions:
        plines = []
        for p in positions:
            if p["percent"] is not None:
                sal = localized("salary_percent", lang, percent=f"{p['percent']:g}")
            else:
                sal = economy.format_amount(p["amount"] or 0)
            plines.append(escape_html(f"{p['position']}: {sal}"))
        lines.append(f"<b>{escape_html(localized('ent_field_positions', lang))}:</b>\n"
                     + "\n".join(plines))
    inv = db.get_inventory(db.enterprise_owner(ent["code"]))
    if inv:
        glines = []
        for row in inv[:8]:
            emoji = f"{row['emoji']} " if row["emoji"] else ""
            good = db.get_good(row["good_code"])
            nm = good_display_name(good, lang) if good else row["name"]
            glines.append(escape_html(f"{emoji}{nm} [{row['good_code']}] ×{row['qty']}"))
        lines.append(f"<b>{escape_html(localized('ent_field_goods', lang))}:</b>\n"
                     + "\n".join(glines))
    transit = db.get_enterprise_shipments(ent["code"])
    if transit:
        lines.append(
            f"<b>{escape_html(localized('ent_field_transit', lang, count=len(transit)))}:</b>\n"
            + "\n".join(escape_html(l) for l in _transit_lines_tg(ent["code"], transit[:8], lang)))
    return "\n".join(lines)

def _transit_lines_tg(ent_code, shipments, lang):
    """In-transit shipments seen from `ent_code`: outgoing (➡️) and incoming (⬅️)."""
    now = int(time.time())
    out = []
    for s in shipments:
        good = db.get_good(s["good_code"])
        emoji = f"{good['emoji']} " if good and good["emoji"] else ""
        name = good_display_name(good, lang) if good else s["good_code"]
        eta = _fmt_eta(max(int(s["arrive_at"]) - now, 0))
        if s["from_ent"] == ent_code:
            other = db.get_enterprise(s["to_ent"])
            key, label = "transit_line_out", (_ent_line_tg(other) if other else s["to_ent"])
        else:
            other = db.get_enterprise(s["from_ent"])
            key, label = "transit_line_in", (_ent_line_tg(other) if other else s["from_ent"])
        out.append(localized(key, lang, emoji=emoji, name=name, qty=s["qty"],
                             other=label, eta=eta))
    return out

@router.message(Command("enterprise"))
async def enterprise_tg(message: Message):
    """Show an enterprise's card, or list this group's enterprises."""
    lang = get_chat_lang(_chat_key(message))
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        ents = db.get_server_enterprises("telegram", str(message.chat.id)) \
            if message.chat.type in GROUP_CHAT_TYPES else []
        if not ents:
            await message.reply(localized("ent_list_empty", lang))
            return
        lines = [f"<b>{escape_html(localized('ent_list_header', lang))}</b>"]
        lines += [escape_html(_ent_line_tg(e)) for e in ents]
        await message.reply("\n".join(lines), parse_mode="HTML")
        return
    ent = db.find_enterprise(parts[1])
    if not ent:
        await message.reply(localized("enterprise_not_found", lang))
        return
    card = _enterprise_card_tg(ent, lang)
    if ent["logo"]:
        try:
            photo = BufferedInputFile(ent["logo"], filename="logo.png")
            await message.reply_photo(photo, caption=card[:1024], parse_mode="HTML")
            return
        except Exception:
            pass
    await message.reply(card, parse_mode="HTML")

_EDIT_ENT_OPTIONS = ("name", "description", "logo", "add_leader", "transfer",
                     "salary_bank", "salary_period", "delete")

@router.message(Command("edit_enterprise", "edit-enterprise"))
async def edit_enterprise_tg(message: Message):
    """/edit-enterprise [option] [code] — numbered management menu."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    option = None
    query = None
    for p in parts:
        if p.isdigit() and option is None:
            option = int(p)
        elif query is None:
            query = p
    ent = await _resolve_led_enterprise_tg(message, lang, query)
    if ent is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_ENT_OPTIONS):
        items = [localized(f"edit_ent_opt_{key}", lang) for key in _EDIT_ENT_OPTIONS]
        chosen = await _numbered_choice_tg(
            message, lang, localized("edit_ent_menu_header", lang, name=ent["name"]),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_ENT_OPTIONS[chosen[0]]
    else:
        action = _EDIT_ENT_OPTIONS[option - 1]

    if action == "name":
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_name", lang))
        if m is None:
            return
        db.update_enterprise_field(ent["code"], "name",
                                   clean_display_name(m.text or "", max_len=60))
        await message.reply(localized("edit_ent_done", lang))
    elif action == "description":
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_desc", lang))
        if m is None:
            return
        text = (m.text or "").strip()
        db.update_enterprise_field(ent["code"], "description",
                                   None if text == "-" else text[:500])
        await message.reply(localized("edit_ent_done", lang))
    elif action == "logo":
        logo = await _dialog_logo_tg(message, lang, localized("edit_ent_ask_logo", lang))
        if logo is None:
            return
        db.update_enterprise_logo(ent["code"], logo[0], logo[1])
        await message.reply(localized("edit_ent_done", lang))
    elif action in ("add_leader", "transfer"):
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_leader", lang))
        if m is None:
            return
        ref = _parse_tg_user_ref((m.text or "").strip())
        if ref is None:
            await message.reply(localized("edit_invalid_user", lang))
            return
        target_kind, target = ref
        mention = f"@{target}" if target_kind == "username" else str(target)
        transfer = action == "transfer" or bool(ent["neutral"])
        token = _register_econ_consent({
            "action": "ent_transfer" if transfer else "ent_leader",
            "ent": ent["code"], "target_kind": target_kind, "target": target,
            "lang": lang})
        offer_key = "ent_transfer_offer" if transfer else "ent_leader_offer"
        await message.answer(
            localized(offer_key, lang, mention=mention, name=ent["name"], code=ent["code"]),
            reply_markup=_econ_consent_keyboard(lang, token))
    elif action == "salary_bank":
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_salary_bank", lang))
        if m is None:
            return
        bank = db.get_bank((m.text or "").strip().upper())
        if not bank:
            await message.reply(localized("bank_not_found", lang))
            return
        db.update_enterprise_field(ent["code"], "salary_bank", bank["code"])
        await message.reply(localized("edit_ent_done", lang))
    elif action == "salary_period":
        m = await _dialog_text_tg(message, lang, localized("edit_ent_ask_salary_period", lang))
        if m is None:
            return
        period = (m.text or "").strip().lower()
        if period not in ("weekly", "monthly"):
            await message.reply(localized("edit_ent_bad_value", lang))
            return
        db.update_enterprise_field(ent["code"], "salary_period", period)
        await message.reply(localized("edit_ent_done", lang))
    elif action == "delete":
        m = await _dialog_text_tg(message, lang,
                                  localized("edit_ent_confirm_delete", lang, code=ent["code"]))
        if m is None:
            return
        if (m.text or "").strip().upper() != ent["code"]:
            await message.reply(localized("action_cancelled", lang))
            return
        db.delete_enterprise(ent["code"])
        await message.reply(localized("edit_ent_deleted", lang, name=ent["name"]))
        await send_service_event("enterprise_deleted", name=ent["name"], code=ent["code"],
                                 user=_tg_user_label(message.from_user))

@router.message(Command("ent_join", "ent-join"))
async def ent_join_tg(message: Message):
    """Ask to join an enterprise; a leader approves with the inline
    buttons."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(localized("ent_join_usage", lang))
        return
    ent = db.find_enterprise(parts[1])
    if not ent:
        await message.reply(localized("enterprise_not_found", lang))
        return
    if db.is_enterprise_worker(ent["code"], "telegram", message.from_user.id):
        await message.reply(localized("ent_join_already", lang))
        return
    display = _tg_user_label(message.from_user)
    token = _register_econ_consent({
        "action": "ent_join", "ent": ent["code"], "requester_id": message.from_user.id,
        "requester_name": display, "lang": lang})
    await message.answer(
        localized("ent_join_request", lang, user=display, name=ent["name"], code=ent["code"]),
        reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("ent_leave", "ent-leave"))
async def ent_leave_tg(message: Message):
    """Leave an enterprise you work at. Your personal salary override goes
    with you."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    ents = db.user_member_enterprises("telegram", message.from_user.id)
    if len(parts) > 1:
        q = parts[1].strip().lower()
        ents = [e for e in ents if e["code"].lower() == q or e["name"].lower() == q]
    if not ents:
        await message.reply(localized("ent_leave_none", lang))
        return
    ent = ents[0]
    if len(ents) > 1:
        ent = await _numbered_choice_tg(message, lang, localized("ent_choose", lang),
                                        ents, _ent_line_tg)
        if ent is None:
            return
    db.remove_enterprise_member(ent["code"], "telegram", message.from_user.id)
    await message.reply(localized("ent_leave_done", lang, name=ent["name"]))

@router.message(Command("ent_kick", "ent-kick"))
async def ent_kick_tg(message: Message):
    """/ent-kick <user> [code] — or reply to the worker with /ent-kick [code]."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        query = parts[0] if parts else None
    else:
        if not parts:
            await message.reply(localized("ent_kick_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        query = parts[1] if len(parts) > 1 else None
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    ent = await _resolve_led_enterprise_tg(message, lang, query)
    if ent is None:
        return
    if not db.remove_enterprise_member(ent["code"], "telegram", target_id):
        await message.reply(localized("ent_kick_not_member", lang))
        return
    disp = await _quiz_target_label_tg(target_id)
    await message.reply(localized("ent_kick_done", lang, user=escape_html(disp),
                                  name=ent["name"]))

def _salary_display(kind, value, lang):
    """Render a salary the way it was set — an amount in the enterprise's
    currency, or a percentage of period sales."""
    if kind == "percent":
        return localized("salary_percent", lang, percent=f"{value:g}")
    return economy.format_amount(value)

@router.message(Command("ent_position", "ent-position"))
async def ent_position_tg(message: Message):
    """/ent-position <name> | <salary> [| <enterprise>]"""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    body = (message.text or "").split(maxsplit=1)
    fields = [p.strip() for p in body[1].split("|")] if len(body) > 1 else []
    if len(fields) < 2:
        await message.reply(localized("ent_position_usage", lang))
        return
    ent = await _resolve_led_enterprise_tg(message, lang,
                                           fields[2] if len(fields) > 2 else None)
    if ent is None:
        return
    parsed = economy.parse_salary(fields[1])
    if parsed is None:
        await message.reply(localized("bad_amount", lang))
        return
    kind, value = parsed
    pos = clean_display_name(fields[0], max_len=40)
    if kind == "clear":
        db.set_position_salary(ent["code"], pos, None, None)
        await message.reply(localized("ent_position_cleared", lang, position=pos))
        return
    db.set_position_salary(ent["code"], pos,
                           value if kind == "amount" else None,
                           value if kind == "percent" else None)
    await message.reply(localized("ent_position_done", lang, position=pos,
                                  salary=_salary_display(kind, value, lang)))

@router.message(Command("ent_assign", "ent-assign"))
async def ent_assign_tg(message: Message):
    """/ent-assign <user> | <position> [| <enterprise>] — or a reply with
    /ent-assign <position> [| <enterprise>]."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    body = (message.text or "").split(maxsplit=1)
    fields = [p.strip() for p in body[1].split("|")] if len(body) > 1 else []
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        pos_field = fields[0] if fields else None
        query = fields[1] if len(fields) > 1 else None
    else:
        if len(fields) < 2:
            await message.reply(localized("ent_assign_usage", lang))
            return
        target_id = await _resolve_tg_target(message, fields[0])
        pos_field = fields[1]
        query = fields[2] if len(fields) > 2 else None
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    if not pos_field:
        await message.reply(localized("ent_assign_usage", lang))
        return
    ent = await _resolve_led_enterprise_tg(message, lang, query)
    if ent is None:
        return
    if not db.get_enterprise_member(ent["code"], "telegram", target_id):
        await message.reply(localized("ent_kick_not_member", lang))
        return
    disp = await _quiz_target_label_tg(target_id)
    pos = clean_display_name(pos_field, max_len=40)
    if pos == "-":
        db.set_member_position(ent["code"], "telegram", target_id, None)
        await message.reply(localized("ent_assign_cleared", lang, user=escape_html(disp)))
        return
    db.set_member_position(ent["code"], "telegram", target_id, pos)
    await message.reply(localized("ent_assign_done", lang, user=escape_html(disp),
                                  position=pos))

@router.message(Command("ent_salary", "ent-salary"))
async def ent_salary_tg(message: Message):
    """/ent-salary <user> <salary> [enterprise] — or a reply with
    /ent-salary <salary> [enterprise]."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        if not parts:
            await message.reply(localized("ent_salary_usage", lang))
            return
        salary_s = parts[0]
        query = parts[1] if len(parts) > 1 else None
    else:
        if len(parts) < 2:
            await message.reply(localized("ent_salary_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        salary_s = parts[1]
        query = parts[2] if len(parts) > 2 else None
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    ent = await _resolve_led_enterprise_tg(message, lang, query)
    if ent is None:
        return
    if not db.is_enterprise_worker(ent["code"], "telegram", target_id):
        await message.reply(localized("ent_kick_not_member", lang))
        return
    parsed = economy.parse_salary(salary_s)
    if parsed is None:
        await message.reply(localized("bad_amount", lang))
        return
    kind, value = parsed
    disp = await _quiz_target_label_tg(target_id)
    if kind == "clear":
        db.set_personal_salary(ent["code"], "telegram", target_id, None, None)
        await message.reply(localized("ent_salary_cleared", lang, user=escape_html(disp)))
        return
    db.set_personal_salary(ent["code"], "telegram", target_id,
                           value if kind == "amount" else None,
                           value if kind == "percent" else None)
    await message.reply(localized("ent_salary_done", lang, user=escape_html(disp),
                                  salary=_salary_display(kind, value, lang)))

@router.message(Command("ent_sell", "ent-sell"))
async def ent_sell_tg(message: Message):
    """/ent-sell <good_code> <buyer> [qty] [price] [enterprise] — sell the
    enterprise's goods to a member or to another enterprise (leaders).

    No bank buys goods, so this is a sale between two holders and the buyer has
    to accept it. `offer_sale_tg` comes from commands/goods.py at the call site:
    a personal sale and an enterprise's sale are the same offer with a different
    seller, and a module-level import would drag that module's commands into
    registration ahead of their place."""
    from telegram_bot.commands.goods import offer_sale_tg

    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if len(parts) < 2:
        await message.reply(localized("ent_sell_usage", lang))
        return
    good = db.get_good(parts[0].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    qty = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    price = parts[3] if len(parts) > 3 else None
    ent = await _resolve_led_enterprise_tg(message, lang, parts[4] if len(parts) > 4 else None)
    if ent is None:
        return
    await offer_sale_tg(message, lang, db.enterprise_owner(ent["code"]), good, qty,
                        parts[1], price)

def _resolve_export_args_tg(target_query, price_s, currency, source):
    """(target, price, bank, error_key_or_None) — Telegram mirror."""
    target = db.find_enterprise(target_query)
    if not target:
        return None, None, None, "export_bad_target"
    if source and target["code"] == source["code"]:
        return None, None, None, "export_same"
    price = 0
    bank = None
    if price_s:
        price = economy.parse_amount(price_s)
        if price is None:
            return None, None, None, "bad_amount"
        code = (currency or (source["salary_bank"] if source else None) or "").strip().upper()
        bank = db.get_bank(code) if code else None
        if not bank:
            return None, None, None, "export_need_currency"
    return target, price, bank, None

@router.message(Command("export"))
async def export_tg(message: Message):
    """/export <good_code> <qty> <target_ent> [price] [currency] [your_ent]"""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if len(parts) < 3:
        await message.reply(localized("export_usage", lang))
        return
    good = db.get_good(parts[0].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    if not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.reply(localized("bad_amount", lang))
        return
    qty = int(parts[1])
    source = await _resolve_led_enterprise_tg(message, lang,
                                              parts[5] if len(parts) > 5 else None)
    if source is None:
        return
    tgt, price, bank, err = _resolve_export_args_tg(
        parts[2], parts[3] if len(parts) > 3 else None,
        parts[4] if len(parts) > 4 else None, source)
    if err:
        await message.reply(localized(err, lang))
        return
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    name = good_display_name(good, lang)
    price_disp = economy.format_money(price, bank) if price else localized("export_free", lang)
    token = _register_econ_consent({
        "action": "export", "ent": tgt["code"], "from": source["code"],
        "good": good["code"], "qty": qty, "price": price,
        "bank": bank["code"] if bank else None,
        "notify_platform": "telegram", "notify_key": _chat_key(message), "lang": lang})
    await message.answer(
        localized("export_offer", lang, source=_ent_line_tg(source), target=_ent_line_tg(tgt),
                  qty=qty, emoji=emoji, name=name, price=price_disp),
        reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("auto_export", "auto-export"))
async def auto_export_tg(message: Message):
    """/auto-export <good_code> <qty|off> <target_ent> [price] [currency] [your_ent]"""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if len(parts) < 3:
        await message.reply(localized("auto_export_usage", lang))
        return
    good = db.get_good(parts[0].strip().upper())
    if not good:
        await message.reply(localized("good_not_found", lang))
        return
    source = await _resolve_led_enterprise_tg(message, lang,
                                              parts[5] if len(parts) > 5 else None)
    if source is None:
        return
    tgt = db.find_enterprise(parts[2])
    if not tgt:
        await message.reply(localized("export_bad_target", lang))
        return
    if parts[1].strip().lower() in ("off", "0", "stop"):
        db.remove_auto_export(source["code"], tgt["code"], good["code"])
        await message.reply(localized("auto_export_removed", lang,
                                      name=good_display_name(good, lang),
                                      target=_ent_line_tg(tgt)))
        return
    if not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.reply(localized("bad_amount", lang))
        return
    qty = int(parts[1])
    tgt, price, bank, err = _resolve_export_args_tg(
        parts[2], parts[3] if len(parts) > 3 else None,
        parts[4] if len(parts) > 4 else None, source)
    if err:
        await message.reply(localized(err, lang))
        return
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    name = good_display_name(good, lang)
    price_disp = economy.format_money(price, bank) if price else localized("export_free", lang)
    token = _register_econ_consent({
        "action": "auto_export", "ent": tgt["code"], "from": source["code"],
        "good": good["code"], "qty": qty, "price": price,
        "bank": bank["code"] if bank else None,
        "created_by": message.from_user.id, "lang": lang})
    await message.answer(
        localized("auto_export_offer", lang, source=_ent_line_tg(source),
                  target=_ent_line_tg(tgt), qty=qty, emoji=emoji, name=name, price=price_disp),
        reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("transit"))
async def transit_tg(message: Message):
    """/transit [code] — an enterprise's goods in transit."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        ent = db.find_enterprise(parts[1])
        if not ent:
            await message.reply(localized("enterprise_not_found", lang))
            return
    else:
        ent = await _resolve_led_enterprise_tg(message, lang)
        if ent is None:
            return
    shipments = db.get_enterprise_shipments(ent["code"])
    if not shipments:
        await message.reply(localized("transit_empty", lang, name=ent["name"]))
        return
    header = localized("transit_title", lang, name=ent["name"], code=ent["code"])
    body = "\n".join(escape_html(l) for l in _transit_lines_tg(ent["code"], shipments, lang))
    await message.reply(f"<b>{escape_html(header)}</b>\n{body}"[:4000], parse_mode="HTML")
