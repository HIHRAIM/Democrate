"""Banks and the money in them on Telegram: founding a bank, its card and
leadership, the `/edit_bank` menu, accounts, payments, earning channels and
fines.

Amounts are parsed and formatted by economy/money.py and are integers in minor
units the whole way through, exactly as on Discord. `/fine` is the one command
here that may take an account below zero: a negative balance is the holder's
debt to the bank.

Leadership offers go out as inline keyboards registered in
`_pending_econ_consents` and are answered by telegram_bot/callbacks.py:
handle_econ_consent.

Not this module's zone: treaties, rates, wages and dues (commands/trade.py),
goods (commands/goods.py), the money primitives (db/banks.py).
"""
from aiogram.filters import Command
from aiogram.types import Message

import db
import economy
from message_relay import clean_display_name, escape_html
from utils import (
    format_stored_user, get_chat_lang, is_admin, localized,
    send_service_event,
)

from telegram_bot.client import (
    GROUP_CHAT_TYPES, _chat_key, _tg_user_label, chat_display_name,
    is_server_admin, router,
)
from telegram_bot.dialogs import (
    _dialog_text_tg, _econ_consent_keyboard, _numbered_choice_tg,
    _parse_tg_user_ref, _quiz_target_label_tg, _register_econ_consent,
    _reply_temp, _resolve_tg_target,
)

def _currency_code_validator_tg(value):
    """Accept a 4-character currency code only when it is free across the
    whole shared namespace."""
    code = value.strip().upper()
    if economy.CURRENCY_CODE_RE.match(code) and not db.code_taken(code):
        return code
    return None

def _bank_line_tg(bank):
    """One line naming a bank in a list — 'Currency [CODE] emoji'."""
    emoji = f" {bank['emoji']}" if bank["emoji"] else ""
    return f"{bank['currency_name']} [{bank['code']}]{emoji}"

async def _resolve_led_bank_tg(message, lang):
    """The bank the caller leads, asking which one when they lead several.

    Bank leadership is global rather than per union, so this is not scoped to the
    current group."""
    banks = db.user_led_banks("telegram", message.from_user.id)
    if not banks:
        await message.reply(localized("bank_none_led", lang))
        return None
    if len(banks) == 1:
        return banks[0]
    return await _numbered_choice_tg(message, lang, localized("bank_choose", lang),
                                     banks, _bank_line_tg)

@router.message(Command("create_bank", "create-bank"))
async def create_bank_tg(message: Message):
    """Found a bank and its currency through a dialog (Bot/Server Admins).

    Anchors the bank to a central server, which must itself be `/setup`-bound. The
    creator becomes its first leader."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    allowed = is_admin("telegram", message.from_user.id)
    if not allowed and message.chat.type in GROUP_CHAT_TYPES:
        allowed = await is_server_admin(message.chat.id, message.from_user.id)
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return

    m = await _dialog_text_tg(message, lang, localized("create_bank_ask_chat", lang))
    if m is None:
        return
    chat = db.get_chat((m.text or "").strip())
    if not chat:
        await message.reply(localized("create_bank_chat_not_setup", lang))
        return
    union = chat["union_code"]

    m = await _dialog_text_tg(message, lang, localized("create_bank_ask_name", lang))
    if m is None:
        return
    currency_name = clean_display_name(m.text or "", max_len=40)

    code = await _dialog_text_tg(message, lang, localized("create_bank_ask_code", lang),
                                 validator=_currency_code_validator_tg,
                                 error_key="create_bank_bad_code")
    if code is None:
        return

    m = await _dialog_text_tg(message, lang, localized("create_bank_ask_emoji", lang))
    if m is None:
        return
    parts = (m.text or "").strip().split()
    emoji = parts[0][:16] if parts else ""

    db.create_bank(code, union, chat["chat_id"], currency_name, emoji,
                   "telegram", message.from_user.id, _tg_user_label(message.from_user))
    await message.reply(localized("create_bank_created", lang, name=currency_name, code=code,
                                  emoji=emoji, union=db.get_union_name(union, lang)))
    await send_service_event("bank_created", name=currency_name, code=code, union=union,
                             user=_tg_user_label(message.from_user))

def _bank_card_tg(bank, lang, central=None):
    """The bank card as HTML: currency, central server, leaders, published
    value, money supply, debt and account count.

    `central` is the central server's name, resolved by the caller because
    naming a Telegram group is an API call and this function is not async. It
    falls back to the bare id, which is what the card used to print
    unconditionally."""
    emoji = f" {bank['emoji']}" if bank["emoji"] else ""
    lines = [f"<b>{escape_html(bank['currency_name'])} [{escape_html(bank['code'])}]{escape_html(emoji)}</b>"]
    lines.append(f"<b>{escape_html(localized('bank_field_union', lang))}:</b> "
                 f"{escape_html(db.get_union_name(bank['union_code'], lang))}")
    lines.append(f"<b>{escape_html(localized('bank_field_central', lang))}:</b> "
                 f"{escape_html(str(central or bank['central_chat']))}")
    leaders = db.get_bank_leaders(bank["code"])
    shown = ", ".join(format_stored_user("telegram", l["platform"], l["user_id"],
                                         l["display_name"]) for l in leaders) or "—"
    lines.append(f"<b>{escape_html(localized('bank_field_leaders', lang))}:</b> {escape_html(shown)}")
    lines.append(f"<b>{escape_html(localized('bank_field_supply', lang))}:</b> "
                 f"{escape_html(economy.format_money(db.bank_money_supply(bank['code']), bank))}")
    lines.append(f"<b>{escape_html(localized('bank_field_accounts', lang))}:</b> "
                 f"{db.bank_account_count(bank['code'])}")
    debt = db.bank_debt(bank["code"])
    if debt:
        lines.append(f"<b>{escape_html(localized('bank_field_debt', lang))}:</b> "
                     f"{escape_html(economy.format_money(debt, bank))}")
    lines.append(f"<b>{escape_html(localized('bank_field_value', lang))}:</b> {bank['value']:.4f}")
    return "\n".join(lines)

@router.message(Command("bank"))
async def bank_tg(message: Message):
    """Show a bank and its currency by code."""
    lang = get_chat_lang(_chat_key(message))
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("bank_usage", lang))
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    central = await chat_display_name(bank["central_chat"])
    await message.reply(_bank_card_tg(bank, lang, central), parse_mode="HTML")

async def _bank_leadership_tg(message, transfer):
    """Shared body of `/bank_add_leader` and `/bank_transfer`.

    A Bot Admin may appoint anywhere; otherwise the caller must lead the bank. The
    target still has to press Accept."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("bank_leader_usage", lang))
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    caller_is_admin = is_admin("telegram", message.from_user.id)
    if not caller_is_admin and not db.is_bank_leader(bank["code"], "telegram", message.from_user.id):
        await message.reply(localized("bank_not_leader", lang))
        return
    ref = _parse_tg_user_ref(parts[2])
    if caller_is_admin:
        tid = await _resolve_tg_target(message, parts[2])
        if tid is None:
            await message.reply(localized("bank_admin_need_id", lang))
            return
        disp = await _quiz_target_label_tg(tid)
        if transfer:
            db.set_bank_leader(bank["code"], "telegram", tid, disp)
            key = "bank_transfer_done"
        else:
            db.add_bank_leader(bank["code"], "telegram", tid, disp)
            key = "bank_leader_added"
        await message.reply(localized(key, lang, user=disp, name=bank["currency_name"]))
        return
    if ref is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    target_kind, target = ref
    mention = f"@{target}" if target_kind == "username" else str(target)
    token = _register_econ_consent({
        "action": "bank_transfer" if transfer else "bank_leader",
        "bank_code": bank["code"], "target_kind": target_kind, "target": target, "lang": lang})
    offer_key = "bank_transfer_offer" if transfer else "bank_leader_offer"
    await message.answer(
        localized(offer_key, lang, mention=mention, name=bank["currency_name"], code=bank["code"]),
        reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("bank_add_leader", "bank-add-leader"))
async def bank_add_leader_tg(message: Message):
    """Offer somebody co-leadership of a bank (bank leaders / Bot
    Admins)."""
    await _bank_leadership_tg(message, transfer=False)

@router.message(Command("bank_transfer", "bank-transfer"))
async def bank_transfer_tg(message: Message):
    """Offer somebody the whole bank (bank leaders / Bot Admins). On
    acceptance they become its only leader."""
    await _bank_leadership_tg(message, transfer=True)

_EDIT_BANK_OPTIONS = ("name", "emoji", "code", "central", "fee", "add_leader",
                      "transfer", "delete")

async def _resolve_edit_bank_tg(message, lang, query=None):
    """Mirror of the Discord helper: any bank for a Bot Admin, only a led one
    otherwise; without a code the single bank the caller leads."""
    admin = is_admin("telegram", message.from_user.id)
    if query:
        bank = db.get_bank(query.strip().upper())
        if not bank:
            await message.reply(localized("bank_not_found", lang))
            return None
        if not (admin or db.is_bank_leader(bank["code"], "telegram", message.from_user.id)):
            await message.reply(localized("edit_bank_no_permission", lang))
            return None
        return bank
    led = [b for b in db.get_all_banks()
           if db.is_bank_leader(b["code"], "telegram", message.from_user.id)]
    if not led:
        await message.reply(localized("edit_bank_specify" if admin
                                      else "edit_bank_no_permission", lang))
        return None
    if len(led) == 1:
        return led[0]
    return await _numbered_choice_tg(message, lang, localized("edit_bank_choose", lang),
                                     led, lambda b: f"{b['currency_name']} [{b['code']}]")

@router.message(Command("edit_bank", "edit-bank"))
async def edit_bank_tg(message: Message):
    """/edit-bank [option] [code] — numbered management menu for a bank."""
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
    bank = await _resolve_edit_bank_tg(message, lang, query)
    if bank is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_BANK_OPTIONS):
        items = [localized(f"edit_bank_opt_{key}", lang) for key in _EDIT_BANK_OPTIONS]
        chosen = await _numbered_choice_tg(
            message, lang,
            localized("edit_bank_menu_header", lang, name=bank["currency_name"],
                      code=bank["code"]),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_BANK_OPTIONS[chosen[0]]
    else:
        action = _EDIT_BANK_OPTIONS[option - 1]

    if action == "name":
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_name", lang))
        if m is None:
            return
        db.update_bank_field(bank["code"], "currency_name",
                             clean_display_name(m.text or "", max_len=40))
        await message.reply(localized("edit_bank_done", lang))
    elif action == "emoji":
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_emoji", lang))
        if m is None:
            return
        chunks = (m.text or "").strip().split()
        db.update_bank_field(bank["code"], "emoji", chunks[0][:16] if chunks else "")
        await message.reply(localized("edit_bank_done", lang))
    elif action == "code":
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_code", lang))
        if m is None:
            return
        new_code = _currency_code_validator_tg((m.text or "").strip())
        if new_code is None:
            await message.reply(localized("edit_bank_bad_code", lang))
            return
        old_code = bank["code"]
        db.rename_bank_code(old_code, new_code)
        await message.reply(localized("edit_bank_code_changed", lang,
                                      old=old_code, code=new_code))
    elif action == "central":
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_central", lang))
        if m is None:
            return
        chat = db.get_chat((m.text or "").strip())
        if not chat:
            await message.reply(localized("edit_bank_bad_central", lang))
            return
        db.set_bank_central_chat(bank["code"], chat["chat_id"], chat["union_code"])
        await message.reply(localized("edit_bank_done", lang))
    elif action == "fee":
        m = await _dialog_text_tg(
            message, lang,
            localized("edit_bank_ask_fee", lang,
                      current=economy.convert_fee_summary(bank["code"], lang)))
        if m is None:
            return
        parsed = economy.parse_convert_fee_input(m.text)
        if parsed is None:
            await message.reply(localized("edit_bank_bad_fee", lang))
            return
        target, fee = parsed
        if target != db.CONVERT_FEE_DEFAULT_KEY and not db.get_bank(target):
            await message.reply(localized("bank_not_found", lang))
            return
        db.set_convert_fee(bank["code"], target, fee)
        await message.reply(localized("edit_bank_fee_set", lang,
                                      current=economy.convert_fee_summary(bank["code"], lang)))
    elif action in ("add_leader", "transfer"):
        m = await _dialog_text_tg(message, lang, localized("edit_bank_ask_leader", lang))
        if m is None:
            return
        ref = _parse_tg_user_ref((m.text or "").strip())
        if ref is None:
            await message.reply(localized("edit_invalid_user", lang))
            return
        target_kind, target = ref
        mention = f"@{target}" if target_kind == "username" else str(target)
        transfer = action == "transfer"
        token = _register_econ_consent({
            "action": "bank_transfer" if transfer else "bank_leader",
            "bank_code": bank["code"], "target_kind": target_kind,
            "target": target, "lang": lang})
        offer_key = "bank_transfer_offer" if transfer else "bank_leader_offer"
        await message.answer(
            localized(offer_key, lang, mention=mention,
                      name=bank["currency_name"], code=bank["code"]),
            reply_markup=_econ_consent_keyboard(lang, token))
    elif action == "delete":
        m = await _dialog_text_tg(message, lang,
                                  localized("edit_bank_confirm_delete", lang,
                                            code=bank["code"]))
        if m is None:
            return
        if (m.text or "").strip().upper() != bank["code"]:
            await message.reply(localized("action_cancelled", lang))
            return
        name = bank["currency_name"]
        db.delete_bank(bank["code"])
        await message.reply(localized("edit_bank_deleted", lang, name=name,
                                      code=bank["code"]))
        await send_service_event("bank_deleted", name=name, code=bank["code"],
                                 user=_tg_user_label(message.from_user))

@router.message(Command("open_account", "open-account"))
async def open_account_tg(message: Message):
    """Open an account in a bank.

    Rarely needed — an account also appears silently on the first message that
    earns in a channel."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("open_account_usage", lang))
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    if db.account_exists(bank["code"], *owner):
        await message.reply(localized("account_exists", lang, code=bank["code"]))
        return
    db.ensure_account(bank["code"], *owner, display_name=_tg_user_label(message.from_user))
    await message.reply(localized("account_opened", lang, name=bank["currency_name"], code=bank["code"]))

@router.message(Command("balance"))
async def balance_tg(message: Message):
    """Show your balances and energy, in one currency or in all of them."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    parts = (message.text or "").split()
    if len(parts) > 1:
        bank = db.get_bank(parts[1].strip().upper())
        if not bank:
            await message.reply(localized("bank_not_found", lang))
            return
        banks = [bank]
    else:
        banks = [db.get_bank(a["bank_code"]) for a in db.get_owner_accounts(*owner)]
        banks = [b for b in banks if b]
    if not banks:
        await message.reply(localized("balance_none", lang))
        return
    blocks = []
    for b in banks:
        acc = db.get_account(b["code"], *owner)
        bal = acc["balance"] if acc else 0
        energy = acc["energy"] if acc else 0
        prof_code = db.get_profession(owner, b["code"])
        prof_good = db.get_good(prof_code) if prof_code else None
        prof = prof_good["name"] if prof_good else localized("profession_none", lang)
        line = localized("balance_line", lang, money=economy.format_money(bal, b),
                         energy=economy.format_energy(energy), profession=prof)
        if bal < 0:
            line += " " + localized("balance_debt_mark", lang)
        blocks.append(f"<b>{escape_html(b['currency_name'])} [{escape_html(b['code'])}]</b>\n"
                      f"{escape_html(line)}")
    await message.reply(f"<b>{escape_html(localized('balance_title', lang))}</b>\n\n"
                        + "\n\n".join(blocks), parse_mode="HTML")

@router.message(Command("pay"))
async def pay_tg(message: Message):
    """Send money to another user in one currency.

    Refused when the balance does not cover it: `/pay` never puts anyone into debt,
    unlike `/fine`."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        if len(parts) < 2:
            await message.reply(localized("pay_usage", lang))
            return
        amount_s, code_s = parts[0], parts[1]
    else:
        if len(parts) < 3:
            await message.reply(localized("pay_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        amount_s, code_s = parts[1], parts[2]
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    bank = db.get_bank(code_s.strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    minor = economy.parse_amount(amount_s)
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    sender = db.canonical_user("telegram", message.from_user.id)
    target = db.canonical_user("telegram", target_id)
    if target == sender:
        await message.reply(localized("pay_self", lang))
        return
    tdisp = await _quiz_target_label_tg(target_id)
    if not db.transfer(bank["code"], sender, target, minor, "pay", to_display=tdisp):
        await message.reply(localized("pay_insufficient", lang))
        return
    await message.reply(localized("pay_done", lang, amount=economy.format_money(minor, bank),
                                  user=escape_html(tdisp)))

@router.message(Command("set_earn", "set-earn"))
async def set_earn_tg(message: Message):
    """Every answer here is temporary: marking a channel is a one-off setup act,
    and its confirmation should not stay in the chat people earn in."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await _reply_temp(message, localized("group_only", lang))
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await _reply_temp(message, localized("set_earn_usage", lang))
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await _reply_temp(message, localized("bank_not_found", lang))
        return
    if not (db.is_bank_leader(bank["code"], "telegram", message.from_user.id)
            or await is_server_admin(message.chat.id, message.from_user.id)
            or is_admin("telegram", message.from_user.id)):
        await _reply_temp(message, localized("set_earn_no_permission", lang))
        return
    chat = db.get_chat(str(message.chat.id))
    if not chat or chat["union_code"] != bank["union_code"]:
        await _reply_temp(message, localized("set_earn_wrong_union", lang))
        return
    action = parts[2].strip().lower()
    chan_key = _chat_key(message)
    if action in ("off", "disable", "0", "false", "no"):
        from telegram_bot.catchall import _earn_keys_tg

        removed = db.remove_earn_channel("telegram", chan_key)
        if not removed and db.resolve_earn_channel("telegram", _earn_keys_tg(message)):
            await _reply_temp(message, localized("set_earn_off_inherited", lang))
            return
        await _reply_temp(message, localized("set_earn_off", lang))
        return
    if action not in ("on", "enable", "1", "true", "yes"):
        await _reply_temp(message, localized("set_earn_usage", lang))
        return
    rate = 1.0
    if len(parts) > 3:
        try:
            rate = max(0.0, min(float(parts[3].replace(",", ".")), 100.0))
        except ValueError:
            rate = 1.0
    db.set_earn_channel("telegram", chan_key, bank["code"], rate)
    await _reply_temp(message, localized("set_earn_on", lang, code=bank["code"],
                                         rate=f"{rate:g}"))

@router.message(Command("fine"))
async def fine_tg(message: Message):
    """Debit a user's account in your bank's currency (bank leaders).

    The one command that may take a balance below zero — that negative balance is
    the member's debt to the bank."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    bank = await _resolve_led_bank_tg(message, lang)
    if bank is None:
        return
    parts = (message.text or "").split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        if not parts:
            await message.reply(localized("fine_usage", lang))
            return
        amount_s = parts[0]
        reason = " ".join(parts[1:]).strip() or None
    else:
        if len(parts) < 2:
            await message.reply(localized("fine_usage", lang))
            return
        target_id = await _resolve_tg_target(message, parts[0])
        amount_s = parts[1]
        reason = " ".join(parts[2:]).strip() or None
    if target_id is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    minor = economy.parse_amount(amount_s)
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    towner = db.canonical_user("telegram", target_id)
    db.burn(bank["code"], towner, minor, f"fine: {reason}" if reason else "fine", allow_negative=True)
    disp = await _quiz_target_label_tg(target_id)
    await message.reply(localized("fine_done", lang, user=escape_html(disp),
                                  amount=economy.format_money(minor, bank),
                                  reason=(reason[:200] if reason else localized("fine_no_reason", lang))))
