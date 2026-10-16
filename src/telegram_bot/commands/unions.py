"""Unions and the chats bound to them on Telegram: `/server_setup`, `/union_add`,
`/union_parties`, `/union_info`, `/union_edit`, `/bot_chats`, `/bot_leave`.

The Discord twin is discord_bot/commands/unions.py and the two are deliberately
separate programs: this one reads positional arguments out of message text and
answers with HTML, that one takes typed slash-command parameters and answers
with an embed. Only the writes are the same.

`/bot_chats` and `/bot_leave` reach into the Discord half at the call site,
which is the deliberate cycle-breaker between the two halves.

`/union_edit` names a new owner by Discord account id, since a subscription
belongs to a Discord account. A Bot Admin hands the union over at once,
confirming with the union code when the new owner has no active
subscription. A sponsor's offer is accepted with the economy consent
keyboard by the Telegram account linked to that Discord account, and
telegram_bot/callbacks.py: handle_econ_consent checks everything again when
the button is pressed; without such a link the transfer has to be offered on
Discord.

`/union_info` answers with the counts and an inline keyboard of full lists; the
buttons come back to telegram_bot/callbacks.py: handle_union_list, which
renders a page with `union_list_message`.

Not this module's zone: the group's language (commands/settings.py), the
party rows a union holds (commands/parties.py), and what the profile counts
(stats.py: union_profile).
"""
import re

from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus
from aiogram.types import Message

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

import db
import sponsors
import stats
from message_relay import clean_display_name, escape_html
from utils import (
    LANG_ORDER, SUPPORTED_LANGS, get_chat_lang, is_admin, language_name,
    apply_party_roles, localized, parse_role_ids, parse_union_names, rate_limit_ok,
    send_service_event,
    format_date, set_chat_lang,
)

from telegram_bot.client import GROUP_CHAT_TYPES, _chat_key, _tg_user_label, bot, router
from telegram_bot.dialogs import (
    _dialog_logo_tg, _dialog_text_tg, _econ_consent_keyboard, _numbered_choice_tg,
    _register_econ_consent,
)

UNION_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,12}$")

@router.message(Command("server_setup", "server-setup", "setup"))
async def setup_cmd(message: Message):
    """Bind this group to a union and set its language (Bot Admins).

    The command the whole bot is gated on — an unbound group answers almost
    nothing. Also settles the seven-day deadline and reports to the service
    chats."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        await message.reply(localized("no_permission", lang))
        return
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply(localized("setup_usage", lang))
        return

    union = parts[1].strip().upper()
    code = parts[2].strip().lower()
    admin = is_admin("telegram", message.from_user.id)
    if not admin:
        owner = sponsors.linked_sponsor_id(message.from_user.id)
        try:
            member = await bot.get_chat_member(message.chat.id, message.from_user.id)
            manages = member.status in (ChatMemberStatus.CREATOR,
                                        ChatMemberStatus.ADMINISTRATOR)
        except Exception:
            manages = False
        if (not owner or not manages or
                not sponsors.can_setup_server(owner, message.chat.id, union)):
            await message.reply(localized("sponsor_setup_forbidden", lang))
            return
    if not db.union_exists(union):
        await message.reply(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes()))
        )
        return
    if code not in SUPPORTED_LANGS:
        await message.reply(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS)))
        )
        return

    if admin:
        former = db.union_owner(union)
        if former is not None:
            db.operator_takeover_union(union, former)
    db.setup_chat("telegram", message.chat.id, union, message.from_user.id,
                  title=message.chat.title)
    set_chat_lang(str(message.chat.id), code)
    await message.reply(
        localized("setup_success", code,
                  union=db.get_union_name(union, code), code=union,
                  lang_name=language_name(code), lang=code)
    )
    await send_service_event("setup_done", platform="Telegram",
                             chat=message.chat.title or str(message.chat.id),
                             union=union, user=_tg_user_label(message.from_user))

@router.message(Command("union_add", "union-add", "add_unia", "add-unia"))
async def add_unia_cmd(message: Message):
    """Create a union, named in any subset of the six languages (Bot
    Admins)."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        await message.reply(localized("no_permission", lang))
        return

    admin = is_admin("telegram", message.from_user.id)
    owner = None if admin else sponsors.linked_sponsor_id(message.from_user.id)
    if not admin:
        if not owner or db.sponsor_tier(owner) == 0:
            await message.reply(localized("sponsor_not_active", lang))
            return
        if len(db.sponsor_unions(owner)) >= sponsors.limits(db.sponsor_tier(owner))["unions"]:
            await message.reply(localized("sponsor_union_limit", lang))
            return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(localized("add_unia_usage", lang))
        return

    code = parts[1].strip().upper()
    if not UNION_CODE_RE.match(code):
        await message.reply(localized("add_unia_invalid_code", lang))
        return

    names = {}
    for chunk in parts[2].split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not sep or key not in SUPPORTED_LANGS or not value:
            await message.reply(localized("add_unia_usage", lang))
            return
        names[key] = value
    if not names:
        await message.reply(localized("add_unia_need_name", lang))
        return

    if not db.add_union(code, names, owner_id=owner):
        await message.reply(localized("add_unia_exists", lang, code=code))
        return

    listed = "\n".join(f"{language_name(L)}: {names[L]}" for L in LANG_ORDER if L in names)
    await message.reply(localized("add_unia_created", lang, code=code, names=listed))
    await send_service_event("union_created", code=code,
                             user=_tg_user_label(message.from_user))

@router.message(Command("bot_chats", "bot-chats", "list_chats"))
async def list_chats_cmd(message: Message):
    """List every chat the bot is in, on both platforms (Bot Admins).

    The Discord half is reached at the call site — the cycle-breaker between the
    halves."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    lines = [localized("list_chats_discord_header", lang)]
    try:
        from discord_bot import bot as dc_bot
        for g in dc_bot.guilds:
            row = db.get_chat(str(g.id))
            union = f" [{row['union_code']}]" if row else ""
            lines.append(f"- {g.name} — id: {g.id}{union}")
    except Exception:
        pass

    tg_ids = db.get_telegram_group_ids()
    if tg_ids:
        lines.append("\n" + localized("list_chats_telegram_header", lang))
        for pid in tg_ids:
            row = db.get_chat(pid)
            union = f" [{row['union_code']}]" if row else ""
            try:
                chat = await bot.get_chat(int(pid))
                title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(pid)
            except Exception:
                title = str(pid)
            lines.append(f"- {title} — id: {pid}{union}")
    else:
        lines.append("\n" + localized("list_chats_no_telegram", lang))

    msg = "\n".join(lines)
    if len(msg) > 4000:
        from aiogram.types import BufferedInputFile
        doc = BufferedInputFile(msg.encode("utf-8"), filename="chat_list.txt")
        await message.reply_document(doc, caption=localized("list_chats_too_long", lang))
    else:
        await message.reply(msg)

@router.message(Command("bot_leave", "bot-leave", "force_leave"))
async def force_leave_cmd(message: Message):
    """Make the bot leave a server or group by id (Bot Admins).

    Drops the union binding so a later re-invitation starts clean; nothing the
    community created is deleted."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply(localized("force_leave_usage", lang))
        return

    platform = parts[1].strip().lower()
    target_id = parts[2].strip()

    if platform == "discord":
        try:
            gid = int(target_id)
        except ValueError:
            await message.reply(localized("force_leave_invalid_id", lang))
            return
        try:
            from discord_bot import bot as dc_bot
            guild = dc_bot.get_guild(gid)
        except Exception:
            guild = None
        if not guild:
            await message.reply(localized("force_leave_not_member", lang))
            return
        try:
            await guild.leave()
        except Exception as e:
            await message.reply(localized("force_leave_failed", lang, error=e))
            return
        db.remove_chat(gid)
        await message.reply(localized("force_leave_success_discord", lang, guild_id=gid))
        return

    if platform == "telegram":
        try:
            tid = int(target_id)
        except ValueError:
            await message.reply(localized("force_leave_invalid_id", lang))
            return
        try:
            await bot.leave_chat(tid)
        except Exception as e:
            await message.reply(localized("force_leave_failed", lang, error=e))
        db.remove_chat(tid)
        await message.reply(localized("force_leave_success_telegram", lang, chat_id=tid))
        return

    await message.reply(localized("force_leave_unsupported_platform", lang))

@router.message(Command("union_parties", "union-parties", "allow_parties", "allow-parties"))
async def allow_parties_tg(message: Message):
    """Switch parties on or off in a union, and choose who may found one (Bot
    Admins; sponsors for their own unions):
    /union_parties <union> <enable|disable|add|remove> [role ids].

    The role list is a list of *Discord* role ids: founding a party is gated
    on a Discord role even when the command is run from Telegram, because
    Telegram groups have no roles to gate on. The rules are the Discord
    command's (utils.apply_party_roles)."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(localized("allow_parties_usage", lang))
        return

    union = parts[1].strip().upper()
    action = parts[2].strip().lower()
    if not (is_admin("telegram", message.from_user.id) or
            sponsors.can_manage_union(
                sponsors.linked_sponsor_id(message.from_user.id), union)):
        await message.reply(localized("no_permission", lang))
        return
    if not db.union_exists(union):
        await message.reply(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes()))
        )
        return
    role_ids = parse_role_ids(" ".join(parts[3:]))
    if role_ids is None:
        await message.reply(localized("allow_parties_invalid_roles", lang))
        return
    key, allowed, enabled = apply_party_roles(union, action, role_ids)
    if key in ("allow_parties_usage", "allow_parties_need_roles"):
        await message.reply(localized(key, lang))
        return
    text = localized(key, lang, code=union, roles=", ".join(str(r) for r in allowed) or "—")
    if action in ("add", "remove") and not enabled:
        text += "\n" + localized("allow_parties_still_off", lang, code=union)
    await message.reply(text)
    event = {"enable": "parties_enabled", "disable": "parties_disabled"}.get(action,
                                                                             "parties_roles")
    await send_service_event(event, code=union, user=_tg_user_label(message.from_user),
                             roles=", ".join(str(r) for r in allowed) or "—")

@router.message(Command("union_info", "union-info", "union"))
async def union_tg(message: Message):
    """Show a union's public profile (everyone): /union_info [code or name],
    this group's union when nothing is given. A query that matches no union
    exactly is answered with a numbered choice of the unions it resembles
    (stats.find_union), or with "not found" when it resembles none."""
    lang = get_chat_lang(_chat_key(message))
    requester = message.from_user.id if message.from_user else message.chat.id
    if not rate_limit_ok(("union-profile", "telegram", requester), limit=5, window_seconds=60):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        code, close = stats.find_union(parts[1], lang)
        if code is None:
            if not close or not message.from_user:
                await message.reply(localized("union_not_found", lang))
                return
            code = await _numbered_choice_tg(message, lang,
                                             localized("union_not_found_suggest", lang), close,
                                             lambda c: stats.union_choice_line(c, lang))
            if code is None:
                return
    else:
        chat = db.get_chat(message.chat.id) if message.chat.type in GROUP_CHAT_TYPES else None
        code = chat["union_code"] if chat else None
    if not code:
        await message.reply(localized("union_profile_specify", lang))
        return
    profile = stats.union_profile(code, lang, "telegram")
    lines = [f"<b>{escape_html(profile['title'])} [{escape_html(code)}]</b>"]
    for title, rows in profile["sections"]:
        lines.append("")
        if title:
            lines.append(f"<b>{escape_html(title)}</b>")
        lines.extend(f"{escape_html(label)}: {escape_html(str(value))}" for label, value in rows)
    keyboard = None
    if profile["lists"]:
        lines.append("")
        lines.append("<i>" + escape_html(localized("union_profile_lists_hint", lang)) + "</i>")
        buttons = [InlineKeyboardButton(text=localized(f"union_list_kind_{kind}", lang),
                                        callback_data=f"ul:{code}:{kind}:0:n")
                   for kind in profile["lists"]]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 2]
                                                         for i in range(0, len(buttons), 2)])
    if profile["logo"]:
        try:
            await message.reply_photo(BufferedInputFile(profile["logo"], filename="union.png"))
        except Exception:
            pass
    await message.reply("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True,
                        reply_markup=keyboard)

def union_list_message(code, kind, page, lang):
    """One page of a union's full list as (HTML text, keyboard or None) —
    what telegram_bot/callbacks.py: handle_union_list sends or redraws. The
    arrows carry the page they lead to, so nothing is remembered between
    presses and a list survives a restart."""
    title, lines = stats.union_list(code, kind, lang)
    shown, page, total = stats.list_page(lines, page)
    body = "\n".join(escape_html(line) for line in shown) or escape_html(
        localized("union_list_empty", lang))
    text = f"<b>{escape_html(title)}</b>\n\n{body}"
    if total == 1:
        return text, None
    text += "\n\n<i>" + escape_html(localized("help_page", lang, page=page + 1,
                                                total=total)) + "</i>"
    arrows = []
    if page > 0:
        arrows.append(InlineKeyboardButton(text=localized("help_prev", lang),
                                           callback_data=f"ul:{code}:{kind}:{page - 1}:e"))
    if page < total - 1:
        arrows.append(InlineKeyboardButton(text=localized("help_next", lang),
                                           callback_data=f"ul:{code}:{kind}:{page + 1}:e"))
    return text, InlineKeyboardMarkup(inline_keyboard=[arrows])

_EDIT_UNION_OPTIONS = ("name", "logo", "owner", "lang", "created")

def _union_editor(message):
    """(is Bot Admin, the Discord account a sponsor's rights come from)."""
    uid = message.from_user.id
    return is_admin("telegram", uid), sponsors.linked_sponsor_id(uid)

async def _resolve_edit_union_tg(message, lang, code):
    """The union the sender may edit: the one named; else this group's union
    when they may manage it; else the only union their linked Discord account
    owns. Replies and returns None when there is none."""
    admin, owner = _union_editor(message)
    if owner and not admin:
        await sponsors.refresh_tier(owner)
    may = lambda c: admin or sponsors.can_manage_union(owner, c)
    if code:
        code = code.strip().upper()
        if not db.union_exists(code):
            await message.reply(localized("setup_unknown_union", lang, code=code,
                                          codes=", ".join(db.get_union_codes())))
            return None
    else:
        chat = db.get_chat(message.chat.id) if message.chat.type in GROUP_CHAT_TYPES else None
        owned = db.sponsor_unions(owner) if owner else []
        if chat and may(chat["union_code"]):
            code = chat["union_code"]
        elif len(owned) == 1:
            code = owned[0]
        else:
            await message.reply(localized("edit_union_specify", lang))
            return None
    if not may(code):
        await message.reply(localized("no_permission", lang))
        return None
    return code

def _names_text(code):
    """A union's names, one 'Language: name' line each."""
    names = db.get_union_names(code)
    return "\n".join(f"{language_name(L)}: {names[L]}" for L in LANG_ORDER if L in names)

async def _edit_union_owner_tg(message, lang, code, raw):
    """The owner item of /union_edit. The new owner is named by their
    Discord account id, because a subscription belongs to one. A Bot Admin
    may hand the union to any account — confirming with the union code when
    that account has no active subscription — and a sponsor's offer, which
    only an active sponsor with room may receive, is accepted by the new
    owner's linked Telegram account."""
    admin, owner = _union_editor(message)
    user = _tg_user_label(message.from_user)
    if raw.strip().lower() in ("none", "-"):
        if not admin:
            await message.reply(localized("edit_union_owner_admin_only", lang))
            return
        db.set_union_owner(code, None)
        await message.reply(localized("edit_union_owner_operator", lang, union=code))
        await send_service_event("union_owner_changed", code=code, owner="—", user=user)
        return
    target = raw.strip()
    if not target.isdigit():
        await message.reply(localized("edit_union_owner_need_id", lang))
        return
    reason = await sponsors.can_receive_union(target, code)
    if reason == "edit_union_owner_same" or (reason and not admin):
        await message.reply(localized(reason, lang))
        return
    if admin:
        confirm, warning = sponsors.owner_change_warning(target, reason, lang, target)
        if confirm:
            answer = await _dialog_text_tg(
                message, lang,
                warning + "\n\n" + localized("edit_union_owner_confirm", lang, code=code))
            if answer is None:
                return
            if (answer.text or "").strip().upper() != code:
                await message.reply(localized("action_cancelled", lang))
                return
            warning = ""
        db.set_union_owner(code, target)
        text = localized("edit_union_owner_done", lang, union=code, user=target)
        await message.reply(f"{text}\n{warning}" if warning else text)
        await send_service_event("union_owner_changed", code=code, owner=target, user=user)
        return
    link = db.get_link_by_discord(target)
    if not link or not link["telegram_id"]:
        await message.reply(localized("edit_union_owner_needs_link", lang))
        return
    token = _register_econ_consent({
        "action": "union_owner", "target_kind": "id", "target": int(link["telegram_id"]),
        "union": code, "owner": target, "from": owner, "by": user, "lang": lang})
    await message.reply(
        localized("edit_union_owner_offer", lang, mention=target,
                  union=db.get_union_name(code, lang), code=code),
        reply_markup=_econ_consent_keyboard(lang, token))

def _date_value(value):
    """A typed date (DD.MM.YYYY or YYYY-MM-DD) as a UTC timestamp, or None so
    the dialog asks again."""
    import celebrations
    try:
        return celebrations.parse_date(value)
    except ValueError:
        return None

def _lang_code(value):
    """One of the six language codes, or None so the dialog asks again."""
    code = value.strip().lower()
    return code if code in SUPPORTED_LANGS else None

@router.message(Command("union_edit", "union-edit", "edit_union", "edit-union"))
async def edit_union_tg(message: Message):
    """Edit a union through a numbered menu: names, logo, owner, language
    (Bot Admins; sponsors for their own unions): /union_edit [CODE]
    [option]."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    args = (message.text or "").split()[1:]
    option = code_arg = None
    if len(args) >= 2:
        code_arg = args[0]
        option = int(args[1]) if args[1].isdigit() else None
    elif args:
        is_option = (args[0].isdigit() and 1 <= int(args[0]) <= len(_EDIT_UNION_OPTIONS)
                     and not db.union_exists(args[0].upper()))
        option, code_arg = (int(args[0]), None) if is_option else (None, args[0])
    code = await _resolve_edit_union_tg(message, lang, code_arg)
    if code is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_UNION_OPTIONS):
        items = [localized(f"edit_union_opt_{key}", lang) for key in _EDIT_UNION_OPTIONS]
        chosen = await _numbered_choice_tg(
            message, lang,
            localized("edit_union_menu_header", lang, name=db.get_union_name(code, lang),
                      code=code),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_UNION_OPTIONS[chosen[0]]
    else:
        action = _EDIT_UNION_OPTIONS[option - 1]

    if action == "name":
        m = await _dialog_text_tg(message, lang,
                                  localized("edit_union_ask_name", lang, current=_names_text(code)))
        if m is None:
            return
        names = parse_union_names(m.text or "")
        if names is None:
            await message.reply(localized("edit_union_bad_name", lang))
            return
        names = {L: clean_display_name(n, max_len=100) if n else None for L, n in names.items()}
        if not db.set_union_names(code, names):
            await message.reply(localized("add_unia_need_name", lang))
            return
        await message.reply(localized("edit_union_names_done", lang, names=_names_text(code)))
    elif action == "logo":
        logo = await _dialog_logo_tg(message, lang, localized("edit_union_ask_logo", lang))
        if logo is None:
            return
        db.set_union_logo(code, logo[0], logo[1])
        await message.reply(localized("edit_union_done", lang))
    elif action == "owner":
        m = await _dialog_text_tg(message, lang, localized("edit_union_ask_owner_tg", lang))
        if m is None:
            return
        await _edit_union_owner_tg(message, lang, code, m.text or "")
    elif action == "lang":
        new = await _dialog_text_tg(
            message, lang, localized("edit_union_ask_lang", lang, supported=", ".join(LANG_ORDER)),
            validator=_lang_code, error_key="edit_union_bad_lang")
        if new is None:
            return
        db.set_union_lang(code, new)
        await message.reply(localized("edit_union_lang_done", lang, language=language_name(new)))
    elif action == "created":
        union_row = db.get_union(code)
        moment = await _dialog_text_tg(
            message, lang,
            localized("edit_union_ask_created", lang,
                      current=format_date(union_row["created_at"], lang)
                      if union_row and union_row["created_at"] else "—"),
            validator=_date_value, error_key="edit_union_bad_created")
        if moment is None:
            return
        db.set_union_created(code, moment)
        await message.reply(localized("edit_union_created_done", lang,
                                      date=format_date(moment, lang)))
