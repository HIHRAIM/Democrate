"""The party commands on Telegram: founding, editing, the card, and the whole
membership path from a join request to a kick.

`/add_party` is a dialog here as it is on Discord, but the waiting works
differently — see telegram_bot/dialogs.py. Approving a join request is an
inline keyboard whose token lives in `_pending_consents`, not a persistent view:
Telegram keyboards survive a restart as *buttons*, but their meaning does not,
so a stale one answers that it has expired instead of acting.

The card and the menus are not here — they are in telegram_bot/parties.py, and
their content comes from the Discord half.

Not this module's zone: the card and menus (telegram_bot/parties.py), the
consent callback (telegram_bot/callbacks.py), party money
(commands/trade.py).
"""
from aiogram.filters import Command
from aiogram.types import Message

import db
from message_relay import clean_display_name
from utils import get_chat_lang, is_admin, localized, send_service_event

from discord_bot import _party_line, _search_party
from telegram_bot.client import (
    _chat_key, _resolve_tg_admin_target, _tg_user_label, router,
)
from telegram_bot.dialogs import (
    _dialog_logo_tg, _dialog_text_tg, _numbered_choice_tg,
    _require_verified_tg, _wait_user_message,
)
from telegram_bot.parties import (
    _edit_party_option_tg, _party_code_validator, _resolve_led_party_tg,
    _resolve_party_union_tg, _send_menu_tg, _send_party_info_tg,
)

async def _has_party_role(union_code, telegram_id):
    """Whether a Telegram user clears the Discord-role requirement
    `/allow-parties` sets for a union.

    The requirement is a set of *Discord* role ids, so it can only be asked of
    somebody who has a Discord account linked to theirs: the answer is looked
    up across the union's Discord servers, and holding the role in any one of
    them is enough. Somebody with no linked Discord account has no role to
    check and passes — for them the Fandom account is the whole gate, which is
    the only way a Telegram-only founder can exist at all."""
    _enabled, role_ids = db.get_party_settings(union_code)
    if not role_ids:
        return True
    link = db.get_link_by_telegram(telegram_id)
    if not link or not link["discord_id"]:
        return True

    from discord_bot import bot as dc_bot

    discord_id = int(link["discord_id"])
    for chat in db.get_union_chats(union_code):
        if chat["platform"] != "discord":
            continue
        try:
            guild = dc_bot.get_guild(int(chat["chat_id"]))
        except (TypeError, ValueError):
            continue
        if guild is None:
            continue
        member = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except Exception:
                member = None
        if member is None:
            continue
        if {r.id for r in getattr(member, "roles", [])} & role_ids:
            return True
    return False

@router.message(Command("add_party", "add-party"))
async def add_party_tg(message: Message):
    """Found a party through a dialog: name, code, logo.

    What a founder needs is a Fandom account the bot knows about, and there are
    two ways to have one: `/verify` on a Discord account linked to this
    Telegram one, or an Olympiad reviewer's `/accepted … remember`, which is
    how somebody with no Discord account at all gets one. `db.known_fandom_name`
    answers both at once — this command deliberately does not use
    `_require_verified_tg`, which knows only the first.

    The Discord-role requirement is applied to whoever has a Discord account to
    apply it to; see `_has_party_role`. A Bot Admin skips it and may name
    somebody else as the founder, by replying to them or giving their id or
    @username."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    union = await _resolve_party_union_tg(message, lang)
    if union is None:
        return

    caller_is_bot_admin = is_admin("telegram", message.from_user.id)
    parts = (message.text or "").split(maxsplit=1)
    founder_arg = parts[1].strip() if len(parts) > 1 else None
    if founder_arg and not caller_is_bot_admin:
        await message.reply(localized("add_party_founder_admin_only", lang))
        return

    founder_id = message.from_user.id
    founder_name = _tg_user_label(message.from_user)
    if founder_arg:
        target_id, username = await _resolve_tg_admin_target(message, founder_arg)
        if target_id is None:
            await message.reply(localized("edit_invalid_user", lang))
            return
        founder_id = target_id
        founder_name = f"@{username}" if username else str(target_id)

    if not caller_is_bot_admin:
        if not db.known_fandom_name("telegram", message.from_user.id):
            await message.reply(localized("verify_required_tg", lang))
            return
        if not await _has_party_role(union, message.from_user.id):
            await message.reply(localized("add_party_no_role", lang))
            return

    m = await _dialog_text_tg(message, lang, localized("add_party_ask_name", lang))
    if m is None:
        return
    name = clean_display_name(m.text or "", max_len=100)

    code = await _dialog_text_tg(message, lang, localized("add_party_ask_code", lang),
                                 validator=_party_code_validator,
                                 error_key="add_party_invalid_code")
    if code is None:
        return

    logo = await _dialog_logo_tg(message, lang, localized("add_party_ask_logo", lang))
    if logo is None:
        return

    db.create_party(code, union, name, "telegram", founder_id, founder_name,
                    logo=logo[0], logo_mime=logo[1])
    await message.reply(localized("add_party_created", lang, name=name, code=code,
                                  union=db.get_union_name(union, lang),
                                  founder=founder_name))
    await send_service_event("party_created", name=name, party=code, union=union,
                             user=_tg_user_label(message.from_user))

@router.message(Command("edit_party_admin", "edit-party-admin"))
async def edit_party_admin_tg(message: Message):
    """Edit any party of any union by code or exact name (Bot Admins).

    The one party command outside the parties-enabled gate, so a union with parties
    switched off can still be tidied up."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("edit_admin_usage", lang))
        return

    query = parts[1].strip()
    party = db.find_party(query)
    if party is None:
        await message.reply(localized("edit_admin_not_found", lang, query=query))
        return

    await _send_menu_tg(message, party, lang, admin=True)
    m = await _wait_user_message(message.chat.id, message.from_user.id)
    if m is None:
        return
    value = (m.text or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= 13):
        await message.reply(localized("choice_invalid", lang))
        return
    await _edit_party_option_tg(message, party, lang, int(value), admin=True)

@router.message(Command("edit_party", "edit-party"))
async def edit_party_tg(message: Message):
    """Manage your party through a numbered menu (founders and party
    leaders)."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if not await _require_verified_tg(message, lang):
        return
    party = await _resolve_led_party_tg(message, lang)
    if party is None:
        return

    parts = (message.text or "").split()
    option = None
    if len(parts) > 1:
        if not parts[1].strip().isdigit():
            await message.reply(localized("edit_invalid_option", lang))
            return
        option = int(parts[1].strip())

    if option is None:
        await _send_menu_tg(message, party, lang)
        return
    await _edit_party_option_tg(message, party, lang, option)

@router.message(Command("edit_rules", "edit-rules"))
async def edit_rules_tg(message: Message):
    """Manage your party's rules (founders and party leaders)."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if not await _require_verified_tg(message, lang):
        return
    party = await _resolve_led_party_tg(message, lang)
    if party is None:
        return

    parts = (message.text or "").split()
    option = None
    if len(parts) > 1:
        if not parts[1].strip().isdigit():
            await message.reply(localized("edit_invalid_option", lang))
            return
        option = int(parts[1].strip())

    if option is None:
        await _send_menu_tg(message, party, lang, rules=True)
        return

    if option == 1:
        new_value = 0 if party["allow_member_invites"] else 1
        db.update_party_field(party["code"], "allow_member_invites", new_value)
        key = "rules_invites_enabled" if new_value else "rules_invites_disabled"
        await message.reply(localized(key, lang))
    else:
        await message.reply(localized("edit_invalid_option", lang))

@router.message(Command("party"))
async def party_tg(message: Message):
    """Show a party of this union, falling back to near matches and then to a
    few random parties."""
    lang = get_chat_lang(_chat_key(message))
    union = await _resolve_party_union_tg(message, lang)
    if union is None:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("party_usage", lang))
        return

    party, suggestions = _search_party(union, parts[1])
    if party is None:
        if not suggestions:
            await message.reply(localized("party_not_found", lang))
            return
        party = await _numbered_choice_tg(message, lang,
                                          localized("party_not_found_suggest", lang),
                                          suggestions, lambda p: _party_line(p, lang))
        if party is None:
            return
    await _send_party_info_tg(message, party, lang)

@router.message(Command("party_join", "party-join"))
async def party_join_tg(message: Message):
    """Ask to join a party of this union.

    Sends Accept/Decline buttons to the leaders. Refused for a suspended party and
    for somebody already in a party of this union."""
    lang = get_chat_lang(_chat_key(message))
    union = await _resolve_party_union_tg(message, lang)
    if union is None:
        return
    if not await _require_verified_tg(message, lang):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("party_join_usage", lang))
        return

    from discord_bot import _find_party_exact, _notify_discord_leaders
    party = _find_party_exact(union, parts[1])
    if not party:
        await message.reply(localized("party_not_found", lang))
        return
    if party["suspended"]:
        await message.reply(localized("party_join_suspended", lang))
        return

    existing = db.find_user_party(union, "telegram", message.from_user.id)
    if existing:
        if existing["code"] == party["code"]:
            await message.reply(localized("party_join_already_member", lang))
        else:
            await message.reply(localized("party_join_in_other", lang, name=existing["name"]))
        return
    if db.has_open_join_request(party["code"], "telegram", message.from_user.id):
        await message.reply(localized("party_join_pending", lang))
        return

    requester_name = _tg_user_label(message.from_user)
    rid = db.create_join_request(party["code"], "telegram", message.from_user.id, requester_name, lang)
    delivered = await _notify_discord_leaders(party, rid, lang, requester_name)
    if delivered == 0:
        db.delete_join_request(rid)
        await message.reply(localized("party_join_no_leader", lang))
        return
    await message.reply(localized("party_join_sent", lang, name=party["name"]))

@router.message(Command("party_leave", "party-leave"))
async def party_leave_tg(message: Message):
    """Leave your party in this union. Rank-and-file members only — a
    founder transfers or dissolves instead."""
    lang = get_chat_lang(_chat_key(message))
    union = await _resolve_party_union_tg(message, lang)
    if union is None:
        return
    party = db.find_user_party(union, "telegram", message.from_user.id)
    if not party:
        await message.reply(localized("party_leave_none", lang))
        return
    if db.is_founder(party["code"], "telegram", message.from_user.id):
        await message.reply(localized("party_leave_founder", lang))
        return
    db.remove_party_leader(party["code"], "telegram", message.from_user.id)
    db.remove_party_member(party["code"], "telegram", message.from_user.id)
    await message.reply(localized("party_leave_done", lang, name=party["name"]))

def _find_kick_target_tg(code, target):
    """Resolve a kick argument to a member of the party: a reply, a mention,
    an id or a @username."""
    raw = (target or "").strip()
    is_id = raw.isdigit()
    name = raw.lstrip("@").lower()
    for mem in db.get_party_members(code):
        if mem["platform"] != "telegram":
            continue
        if is_id and mem["user_id"] == raw:
            return mem
        if (mem["display_name"] or "").lstrip("@").lower() == name:
            return mem
    return None

@router.message(Command("party_kick", "party-kick"))
async def party_kick_tg(message: Message):
    """Remove a member from your party (party leaders). Reaches rank-and-file
    members only."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    party = await _resolve_led_party_tg(message, lang)
    if party is None:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("party_kick_usage", lang))
        return
    target = parts[1].strip()

    mem = _find_kick_target_tg(party["code"], target)
    if mem is None:
        raw = target.lstrip("@").lower()
        is_id = target.strip().isdigit()
        for l in db.get_party_leaders(party["code"]):
            if l["platform"] != "telegram":
                continue
            if (is_id and l["user_id"] == target.strip()) or \
                    (l["display_name"] or "").lstrip("@").lower() == raw:
                if db.is_founder(party["code"], "telegram", l["user_id"]):
                    await message.reply(localized("party_kick_founder", lang))
                else:
                    await message.reply(localized("party_kick_leader", lang))
                return
        await message.reply(localized("party_kick_not_member", lang))
        return

    db.remove_party_member(party["code"], "telegram", mem["user_id"])
    await message.reply(localized("party_kick_done", lang,
                                  user=mem["display_name"] or mem["user_id"], name=party["name"]))
