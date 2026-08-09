"""What links two banks, or a bank and a party, on Telegram: `/treaty`,
`/set_rate`, `/convert`, `/rates`, `/set_wage`, `/party_dues`.

A treaty needs the other bank's leader to accept it with the inline buttons.
`/set_rate` refuses whenever either bank has a second treaty partner — the
check is economy/trade.py: can_manual_peg — because a fixed edge inside a
triangle of currencies lets the triangle disagree with itself.

`/set_wage` and `/party_dues` set monthly obligations rather than moving
anything; the transfers happen on the monthly tick (economy/payroll.py).

Not this module's zone: the daily currency valuation (economy/trade.py), the
accounts (commands/banks.py) and the payroll run (economy/payroll.py).
"""
from aiogram.filters import Command
from aiogram.types import Message

import db
import economy
from utils import get_chat_lang, localized

from telegram_bot.client import _chat_key, router
from telegram_bot.commands.banks import _resolve_led_bank_tg
from telegram_bot.dialogs import _econ_consent_keyboard, _register_econ_consent
from telegram_bot.parties import _resolve_led_party_tg

@router.message(Command("treaty"))
async def treaty_tg(message: Message):
    """Propose a currency-conversion treaty to another bank (bank leaders).

    Nothing is written until a leader of the other bank accepts."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("treaty_usage", lang))
        return
    other = db.get_bank(parts[1].strip().upper())
    if not other:
        await message.reply(localized("bank_not_found", lang))
        return
    mine = await _resolve_led_bank_tg(message, lang)
    if mine is None:
        return
    if mine["code"] == other["code"]:
        await message.reply(localized("treaty_self", lang))
        return
    if db.has_treaty(mine["code"], other["code"]):
        await message.reply(localized("treaty_exists", lang))
        return
    token = _register_econ_consent({"action": "treaty", "mine": mine["code"],
                                    "other": other["code"], "lang": lang})
    await message.answer(localized("treaty_offer", lang, proposer=mine["code"],
                                   code=other["code"], name=other["currency_name"]),
                         reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("set_rate", "set-rate"))
async def set_rate_tg(message: Message):
    """Fix the rate with your bank's sole treaty partner (bank leaders).

    Refused as soon as either bank has a second partner; the partner's leader must
    accept. `rate` is units of their currency per one unit of yours."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("set_rate_usage", lang))
        return
    other = db.get_bank(parts[1].strip().upper())
    if not other:
        await message.reply(localized("bank_not_found", lang))
        return
    try:
        rate = float(parts[2].replace(",", "."))
    except ValueError:
        rate = 0.0
    if rate <= 0:
        await message.reply(localized("set_rate_bad", lang))
        return
    mine = await _resolve_led_bank_tg(message, lang)
    if mine is None:
        return
    if not db.has_treaty(mine["code"], other["code"]):
        await message.reply(localized("set_rate_no_treaty", lang))
        return
    if not economy.can_manual_peg(mine["code"], other["code"]):
        await message.reply(localized("set_rate_multi", lang))
        return
    token = _register_econ_consent({"action": "peg", "mine": mine["code"],
                                    "other": other["code"], "rate": rate, "lang": lang})
    await message.answer(localized("set_rate_offer", lang, a=mine["code"], rate=f"{rate:g}",
                                   b=other["code"]),
                         reply_markup=_econ_consent_keyboard(lang, token))

@router.message(Command("convert"))
async def convert_tg(message: Message):
    """Convert between your own accounts at the current rate, minus the
    spread."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 4:
        await message.reply(localized("convert_usage", lang))
        return
    minor = economy.parse_amount(parts[1])
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    fb = db.get_bank(parts[2].strip().upper())
    tb = db.get_bank(parts[3].strip().upper())
    if not fb or not tb:
        await message.reply(localized("bank_not_found", lang))
        return
    owner = db.canonical_user("telegram", message.from_user.id)
    status, info = economy.convert(owner, fb["code"], tb["code"], minor)
    keymap = {"same": "convert_same", "not_convertible": "convert_not_convertible",
              "too_small": "convert_too_small", "no_funds": "convert_no_funds"}
    if status != "ok":
        await message.reply(localized(keymap.get(status, "convert_not_convertible"), lang))
        return
    await message.reply(localized("convert_done", lang, amount=economy.format_money(minor, fb),
                                  credited=economy.format_money(info["credited"], tb),
                                  rate=f"{info['rate']:.4f}",
                                  fee=economy.format_money(info["fee"], fb)))

@router.message(Command("rates"))
async def rates_tg(message: Message):
    """Show currency values, or one currency's rates against its treaty
    partners."""
    lang = get_chat_lang(_chat_key(message))
    parts = (message.text or "").split()
    if len(parts) > 1:
        bank = db.get_bank(parts[1].strip().upper())
        if not bank:
            await message.reply(localized("bank_not_found", lang))
            return
        partners = db.treaty_partners(bank["code"])
        if not partners:
            await message.reply(localized("rates_none", lang, code=bank["code"]))
            return
        lines = [localized("rates_title_one", lang, code=bank["code"])]
        for p in partners:
            r = economy.rate(bank["code"], p)
            treaty = db.get_treaty(bank["code"], p)
            peg = f" {localized('rates_pegged', lang)}" if treaty and treaty["pegged_rate"] is not None else ""
            lines.append(localized("rates_line", lang, a=bank["code"], b=p,
                                   rate=f"{r:.4f}" if r is not None else "—", peg=peg))
    else:
        banks = db.get_all_banks()
        if not banks:
            await message.reply(localized("rates_no_banks", lang))
            return
        lines = [localized("rates_title_all", lang)]
        lines += [localized("rates_value_line", lang, code=b["code"], name=b["currency_name"],
                            value=f"{b['value']:.4f}") for b in banks]
    await message.reply("\n".join(lines))

@router.message(Command("set_wage", "set-wage"))
async def set_wage_tg(message: Message):
    """Set the monthly wage for a profession in your bank (bank leaders).

    Paid on the monthly tick, not now; `0` clears it."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("set_wage_usage", lang))
        return
    bank = await _resolve_led_bank_tg(message, lang)
    if bank is None:
        return
    good = db.find_bank_good(bank["code"], parts[1])
    if not good:
        await message.reply(localized("set_wage_no_good", lang, bank=bank["code"]))
        return
    if parts[2].strip() in ("0", "0.0", "0.00"):
        db.set_wage(bank["code"], good["code"], None)
        await message.reply(localized("set_wage_cleared", lang, name=good["name"]))
        return
    minor = economy.parse_amount(parts[2])
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    db.set_wage(bank["code"], good["code"], minor)
    await message.reply(localized("set_wage_done", lang, name=good["name"],
                                  amount=economy.format_money(minor, bank)))

@router.message(Command("party_dues", "party-dues"))
async def party_dues_tg(message: Message):
    """Set your party's monthly member dues in one currency (party leaders).

    Collected on the monthly tick, and a member who cannot pay is pushed into debt
    rather than skipped."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("party_dues_usage", lang))
        return
    party = await _resolve_led_party_tg(message, lang)
    if party is None:
        return
    bank = db.get_bank(parts[1].strip().upper())
    if not bank:
        await message.reply(localized("bank_not_found", lang))
        return
    if bank["union_code"] != party["union_code"]:
        await message.reply(localized("dues_wrong_union", lang))
        return
    if parts[2].strip() in ("0", "0.0", "0.00"):
        db.set_party_dues(party["code"], bank["code"], 0)
        await message.reply(localized("dues_cleared", lang, name=party["name"]))
        return
    minor = economy.parse_amount(parts[2])
    if minor is None:
        await message.reply(localized("bad_amount", lang))
        return
    db.set_party_dues(party["code"], bank["code"], minor)
    await message.reply(localized("dues_done", lang, name=party["name"],
                                  amount=economy.format_money(minor, bank)))
