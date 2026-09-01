"""The four @router.callback_query handlers: every inline button the Telegram
half ever sends comes back here.

They are kept together, and in this order, because aiogram dispatches
callback queries in registration order too, and because each of them is the
*second half* of a dialog whose first half is elsewhere: a party leadership
offer (`pc:`), a quiz option (`qb:`), a bank/enterprise/export offer (`ec:`)
and an Olympiad button (`ob:`). The prefix in the callback data is what routes
them apart, and it is also what makes a stale button from before a restart
harmless: the token is no longer in its registry and the handler says so
instead of acting.

Everything these handlers act on is imported at the call site, inside the
function. That is deliberate and not tidiness: this module is imported before
the command modules (so that the callback order matches the original), and a
module-level import of a command module here would drag every command handler
into registration ahead of its place.

Not this module's zone: the registries and the keyboards that create these
buttons (telegram_bot/dialogs.py).
"""
import time

from aiogram.types import CallbackQuery

import db
import economy
from utils import good_display_name, is_admin, localized, send_service_event

from telegram_bot.client import _tg_user_label, router
from telegram_bot.dialogs import (
    DIALOG_TIMEOUT, _pending_consents, _pending_econ_consents,
    _pending_oly_buttons, _pending_quiz_buttons,
)

@router.callback_query(lambda c: c.data and c.data.startswith("pc:"))
async def handle_party_consent(query: CallbackQuery):
    """Answer a party button ('pc:'): leadership offers, suspension and
    dissolution.

    Looks the token up in `_pending_consents` and refuses when it is gone — a
    button from before a restart, or one somebody already answered, says it expired
    rather than acting. Checks that the presser is the person the offer was made
    to, then applies the decision and strips the keyboard so it cannot be pressed
    twice."""
    try:
        _, token, flag = query.data.split(":")
    except Exception:
        await query.answer()
        return

    pend = _pending_consents.get(token)
    if not pend or time.time() - pend["created"] > DIALOG_TIMEOUT:
        _pending_consents.pop(token, None)
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    lang = pend["lang"]
    user = query.from_user
    if pend["target_kind"] == "id":
        allowed = user is not None and user.id == int(pend["target"])
    else:
        allowed = user is not None and \
            (user.username or "").lower() == str(pend["target"]).lower()
    if not allowed:
        await query.answer(localized("consent_not_yours", lang), show_alert=True)
        return

    _pending_consents.pop(token, None)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    party = db.get_party(pend["party_code"])
    if party is None:
        await query.answer()
        return

    accepted = flag == "1"
    display = _tg_user_label(user)
    action = pend["action"]
    if action == "leader":
        if accepted:
            db.add_party_leader(party["code"], "telegram", user.id, display)
            text = localized("leader_added", lang, user=display, name=party["name"])
        else:
            text = localized("leader_declined", lang)
    elif action == "transfer":
        if accepted:
            db.set_party_leader(party["code"], "telegram", user.id, display)
            text = localized("transfer_done", lang, user=display, name=party["name"])
        else:
            text = localized("transfer_declined", lang)
    elif action in ("suspend", "resume"):
        if accepted:
            db.update_party_field(party["code"], "suspended",
                                  0 if action == "resume" else 1)
            text = localized("resume_done" if action == "resume" else "suspend_done",
                             lang, name=party["name"])
        else:
            text = localized("action_cancelled", lang)
    elif action == "delete":
        if accepted:
            db.delete_party(party["code"])
            text = localized("delete_party_done", lang, name=party["name"], code=party["code"])
            await send_service_event("party_deleted", name=party["name"], party=party["code"],
                                     union=party["union_code"], user=display)
        else:
            text = localized("action_cancelled", lang)
    else:
        await query.answer()
        return

    await query.answer()
    try:
        await query.message.reply(text)
    except Exception:
        pass

@router.callback_query(lambda c: c.data and c.data.startswith("qb:"))
async def handle_quiz_button(query: CallbackQuery):
    """Answer a quiz option button ('qb:').

    Resolves the future the running quiz is waiting on. The same question may also
    be answered by typing, which is why the quiz races this against the catchall
    and whichever arrives first wins."""
    try:
        _, token, value = query.data.split(":")
    except Exception:
        await query.answer()
        return
    pend = _pending_quiz_buttons.get(token)
    if not pend or time.time() - pend["created"] > DIALOG_TIMEOUT:
        _pending_quiz_buttons.pop(token, None)
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if not query.from_user or query.from_user.id != pend["user_id"]:
        await query.answer(localized("consent_not_yours", pend["lang"]), show_alert=True)
        return
    _pending_quiz_buttons.pop(token, None)
    await query.answer()
    if not pend["future"].done():
        pend["future"].set_result(value)

@router.callback_query(lambda c: c.data and c.data.startswith("ec:"))
async def handle_econ_consent(query: CallbackQuery):
    """Answer an economy button ('ec:'): bank and enterprise leadership,
    treaties, pegs, join requests, sales and export offers.

    The longest handler in the half, because every economy decision that needs
    somebody else's agreement lands here. Who may press depends on the action — the
    named person for a leadership offer, any leader of the other bank for a treaty
    or a peg, any leader of the receiving enterprise for a join request or an
    export, and the buyer for a sale, whoever the buyer happens to be — and the
    money only moves after that check passes."""
    from telegram_bot.commands.enterprises import _fmt_eta

    try:
        _, token, flag = query.data.split(":")
    except Exception:
        await query.answer()
        return
    pend = _pending_econ_consents.get(token)
    if not pend or time.time() - pend["created"] > DIALOG_TIMEOUT:
        _pending_econ_consents.pop(token, None)
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    lang = pend["lang"]
    user = query.from_user
    action = pend["action"]
    if action in ("bank_leader", "bank_transfer", "ent_leader", "ent_transfer"):
        if pend["target_kind"] == "id":
            allowed = user is not None and user.id == int(pend["target"])
        else:
            allowed = user is not None and \
                (user.username or "").lower() == str(pend["target"]).lower()
        deny_key = "consent_not_yours"
    elif action in ("treaty", "peg"):
        allowed = user is not None and (db.is_bank_leader(pend["other"], "telegram", user.id)
                                        or is_admin("telegram", user.id))
        deny_key = "bank_consent_not_leader"
    elif action in ("ent_join", "export", "auto_export"):
        allowed = user is not None and \
            (db.is_enterprise_leader(pend["ent"], "telegram", user.id)
             or is_admin("telegram", user.id))
        deny_key = "ent_consent_not_leader"
    elif action == "sell":
        if pend["buyer_kind"] == "enterprise":
            allowed = user is not None and \
                (db.is_enterprise_leader(pend["buyer"], "telegram", user.id)
                 or is_admin("telegram", user.id))
            deny_key = "ent_consent_not_leader"
        else:
            allowed = user is not None and user.id == int(pend["buyer"])
            deny_key = "consent_not_yours"
    else:
        await query.answer()
        return
    if not allowed:
        await query.answer(localized(deny_key, lang), show_alert=True)
        return

    _pending_econ_consents.pop(token, None)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    accepted = flag == "1"
    display = _tg_user_label(user)
    text = None
    if action == "bank_leader":
        bank = db.get_bank(pend["bank_code"])
        if bank and accepted:
            db.add_bank_leader(bank["code"], "telegram", user.id, display)
            text = localized("bank_leader_added", lang, user=display, name=bank["currency_name"])
        else:
            text = localized("bank_leader_declined", lang)
    elif action == "bank_transfer":
        bank = db.get_bank(pend["bank_code"])
        if bank and accepted:
            db.set_bank_leader(bank["code"], "telegram", user.id, display)
            text = localized("bank_transfer_done", lang, user=display, name=bank["currency_name"])
        else:
            text = localized("bank_transfer_declined", lang)
    elif action == "treaty":
        if accepted:
            db.add_treaty(pend["mine"], pend["other"])
            text = localized("treaty_done", lang, a=pend["mine"], b=pend["other"])
        else:
            text = localized("treaty_declined", lang)
    elif action == "peg":
        if accepted:
            db.set_pegged_rate(pend["mine"], pend["other"], pend["rate"])
            text = localized("set_rate_done", lang, a=pend["mine"],
                             rate=f"{pend['rate']:g}", b=pend["other"])
        else:
            text = localized("set_rate_declined", lang)
    elif action == "ent_leader":
        ent = db.get_enterprise(pend["ent"])
        if ent and accepted:
            db.add_enterprise_leader(ent["code"], "telegram", user.id, display)
            text = localized("ent_leader_added", lang, user=display, name=ent["name"])
        else:
            text = localized("ent_leader_declined", lang)
    elif action == "ent_transfer":
        ent = db.get_enterprise(pend["ent"])
        if ent and accepted:
            db.set_enterprise_leader(ent["code"], "telegram", user.id, display)
            text = localized("ent_transfer_done", lang, user=display, name=ent["name"])
        else:
            text = localized("ent_transfer_declined", lang)
    elif action == "ent_join":
        ent = db.get_enterprise(pend["ent"])
        if ent and accepted:
            db.add_enterprise_member(ent["code"], "telegram",
                                     pend["requester_id"], pend["requester_name"])
            text = localized("ent_join_approved", lang,
                             user=pend["requester_name"], name=ent["name"])
        else:
            text = localized("ent_join_declined", lang)
    elif action == "export":
        if accepted:
            good = db.get_good(pend["good"])
            if good:
                status, info = economy.dispatch_shipment(
                    pend["from"], pend["ent"], good, pend["qty"], pend["price"],
                    pend["bank"], (pend.get("notify_platform"), pend.get("notify_key")),
                    lang)
                if status == "ok":
                    src, tgt = db.get_enterprise(pend["from"]), db.get_enterprise(pend["ent"])
                    emoji = f"{good['emoji']} " if good["emoji"] else ""
                    text = localized(
                        "export_dispatched", lang, qty=info["qty"], emoji=emoji,
                        name=good_display_name(good, lang),
                        source=f"{src['name']} [{src['code']}]" if src else pend["from"],
                        target=f"{tgt['name']} [{tgt['code']}]" if tgt else pend["ent"],
                        eta=_fmt_eta(info["duration"]),
                        distance=localized(f"transport_{info['distance']}", lang))
                else:
                    text = localized(f"export_{status}", lang)
        else:
            text = localized("export_declined", lang)
    elif action == "sell":
        if accepted:
            good = db.get_good(pend["good"])
            if good:
                seller = tuple(pend["seller"])
                buyer = tuple(pend["buyer_owner"])
                status, info = economy.sell(seller, buyer, good, pend["qty"],
                                            pend["price"], pend["bank"])
                bank = db.get_bank(pend["bank"]) if pend["bank"] else None
                emoji = f"{good['emoji']} " if good["emoji"] else ""
                if status == "ok":
                    text = localized(
                        "sell_done", lang, qty=info["qty"], emoji=emoji,
                        name=good_display_name(good, lang), buyer=pend["buyer_name"],
                        total=economy.format_money(info["total"], bank) if bank
                        else localized("export_free", lang))
                else:
                    text = localized(f"sell_{status}", lang,
                                     name=good_display_name(good, lang))
        else:
            text = localized("sell_declined", lang)
    elif action == "auto_export":
        if accepted:
            db.remove_auto_export(pend["from"], pend["ent"], pend["good"])
            db.add_auto_export(pend["from"], pend["ent"], pend["good"], pend["qty"],
                               pend["price"], pend["bank"], pend["created_by"])
            good = db.get_good(pend["good"])
            tgt = db.get_enterprise(pend["ent"])
            bank = db.get_bank(pend["bank"]) if pend["bank"] else None
            emoji = f"{good['emoji']} " if good and good["emoji"] else ""
            text = localized(
                "auto_export_set", lang, qty=pend["qty"], emoji=emoji,
                name=good_display_name(good, lang) if good else pend["good"],
                target=f"{tgt['name']} [{tgt['code']}]" if tgt else pend["ent"],
                price=economy.format_money(pend["price"], bank) if bank
                else localized("export_free", lang))
        else:
            text = localized("export_declined", lang)

    await query.answer()
    if text:
        try:
            await query.message.reply(text)
        except Exception:
            pass

@router.callback_query(lambda c: c.data and c.data.startswith("ob:"))
async def handle_olympiad_button(query: CallbackQuery):
    """Answer an Olympiad button ('ob:'): the language choice and the
    in-dialog options.

    Resolves the future the voting dialog is waiting on, and is raced against a
    typed answer the same way the quiz buttons are."""
    try:
        _, token, value = query.data.split(":", 2)
    except Exception:
        await query.answer()
        return
    pend = _pending_oly_buttons.get(token)
    if not pend or time.time() - pend["created"] > DIALOG_TIMEOUT:
        _pending_oly_buttons.pop(token, None)
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if not query.from_user or query.from_user.id != pend["user_id"]:
        await query.answer(localized("consent_not_yours", pend["lang"]), show_alert=True)
        return
    _pending_oly_buttons.pop(token, None)
    await query.answer()
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not pend["future"].done():
        pend["future"].set_result(value)
