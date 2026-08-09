"""The party as a Telegram user sees it: the card, the numbered edit menu, and
the option handler behind it.

The *content* of all of this is shared with the Discord half — the menu texts,
the balance lines, the search — and imported from `discord_bot` by name at the
bottom of the import block. What lives here is only the Telegram rendering:
HTML instead of embeds, a reply instead of an ephemeral response, and a
photo sent separately because Telegram has no equivalent of an embed thumbnail.
Merging the two into one platform-neutral renderer is explicitly not wanted:
the difference is the whole reason there are two files.

`_edit_party_option_tg` is the one place a party field is validated and written
on this side, reached from `/edit_party`, `/edit_rules` and
`/edit_party_admin` with different rights and different menus but the same
writes.

Not this module's zone: the commands (telegram_bot/commands/parties.py), the
consent callbacks (telegram_bot/callbacks.py) and storage (db/parties.py).
"""
import re

from aiogram.types import (
    BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton,
)

import db
from message_relay import (
    clean_display_name, discord_to_telegram_html, escape_html,
)
from utils import (
    PARTY_CODE_RE, format_stored_user, localized,
)

from discord_bot import (
    _edit_admin_menu_text, _edit_menu_text, _leaders_differ_from_founder,
    _party_balance_lines, _party_line, _rules_menu_text, parties_enabled,
)
from telegram_bot.client import GROUP_CHAT_TYPES
from telegram_bot.dialogs import (
    _consent_keyboard, _dialog_logo_tg, _dialog_text_tg, _markdown_from_tg,
    _numbered_choice_tg, _parse_tg_user_ref, _register_consent,
)

COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")

URL_RE = re.compile(r"^https?://\S+$")

def _party_code_validator(value):
    """Accept a 4-character party code only when it is free across the whole
    shared namespace."""
    code = value.strip().upper()
    if PARTY_CODE_RE.match(code) and not db.party_code_taken(code):
        return code
    return None

def _party_info_text_tg(party, lang):
    """The party card as HTML: founder, leaders, ideologies, allies, member
    count, government seats, bank balances, description.

    The same content as the Discord embed, rendered for a platform with no embeds —
    which is exactly why the two halves are separate files."""
    lines = [f"<b>{escape_html(party['name'])} [{escape_html(party['code'])}]</b>"]
    if party["suspended"]:
        lines.append(escape_html(localized("party_suspended_mark", lang)))
    founder = format_stored_user("telegram", party["founder_platform"],
                                 party["founder_id"], party["founder_name"])
    lines.append(f"<b>{escape_html(localized('party_field_founder', lang))}:</b> {escape_html(founder)}")
    leaders = db.get_party_leaders(party["code"])
    if _leaders_differ_from_founder(party, leaders):
        shown = ", ".join(
            format_stored_user("telegram", l["platform"], l["user_id"], l["display_name"])
            for l in leaders
        )
        lines.append(f"<b>{escape_html(localized('party_field_leader', lang))}:</b> {escape_html(shown)}")
    if party["ideologies"]:
        lines.append(f"<b>{escape_html(localized('party_field_ideologies', lang))}:</b> "
                     f"{discord_to_telegram_html(party['ideologies'])}")
    allies = db.get_party_allies(party["code"])
    if allies:
        shown = ", ".join(f"{a['name']} [{a['code']}]" for a in allies)
        lines.append(f"<b>{escape_html(localized('party_field_allies', lang))}:</b> {escape_html(shown)}")
    lines.append(f"<b>{escape_html(localized('party_field_members', lang))}:</b> "
                 f"{len(db.get_party_user_set(party['code']))}")
    seats = db.party_govt_seats(party["code"])
    if seats:
        lines.append(f"<b>{escape_html(localized('party_field_seats', lang))}:</b> {seats}")
    balances = _party_balance_lines(party["code"])
    if balances:
        lines.append(f"<b>{escape_html(localized('party_field_balance', lang))}:</b> "
                     f"{escape_html(', '.join(balances))}")
    if party["description"]:
        lines.append(f"<b>{escape_html(localized('party_field_description', lang))}:</b>\n"
                     f"{discord_to_telegram_html(party['description'])}")
    return "\n".join(lines)

async def _send_party_info_tg(message, party, lang):
    """Send a party card, with the logo as a photo when the party has one.

    Telegram has no embed thumbnail, so the logo becomes a captioned photo and the
    caption limit is what the text is clipped to."""
    text = _party_info_text_tg(party, lang)
    kb = None
    if party["article_url"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=localized("party_more_button", lang),
                                 url=party["article_url"]),
        ]])
    if party["logo"]:
        photo = BufferedInputFile(party["logo"], filename="logo.png")
        if len(text) <= 1024:
            await message.reply_photo(photo, caption=text, parse_mode="HTML", reply_markup=kb)
            return
        await message.reply_photo(photo)
    await message.reply(text, parse_mode="HTML", reply_markup=kb)

async def _send_menu_tg(message, party, lang, rules=False, admin=False):
    """Send an edit menu as HTML, with the party's logo when there is
    one."""
    if admin:
        title_key, body = "edit_admin_menu_title", _edit_admin_menu_text(party, lang)
    elif rules:
        title_key, body = "rules_menu_title", _rules_menu_text(party, lang)
    else:
        title_key, body = "edit_menu_title", _edit_menu_text(party, lang)
    title = localized(title_key, lang, name=party["name"], code=party["code"])
    text = f"<b>{escape_html(title)}</b>\n{escape_html(body)}"
    if party["logo"]:
        photo = BufferedInputFile(party["logo"], filename="logo.png")
        if len(text) <= 1024:
            await message.reply_photo(photo, caption=text, parse_mode="HTML")
            return
        await message.reply_photo(photo)
    await message.reply(text, parse_mode="HTML")

async def _resolve_party_union_tg(message, lang):
    """The union of this group, refusing when the group is not set up or the
    union has not turned parties on. Returns the union code, or None."""
    if message.chat.type not in GROUP_CHAT_TYPES:
        await message.reply(localized("group_only", lang))
        return None
    chat = db.get_chat(str(message.chat.id))
    if not chat:
        await message.reply(localized("chat_not_setup", lang))
        return None
    if not parties_enabled(chat["union_code"]):
        await message.reply(localized("party_feature_disabled", lang))
        return None
    return chat["union_code"]

async def _resolve_led_party_tg(message, lang):
    """The party the caller manages in this group's union; asks when several."""
    union = await _resolve_party_union_tg(message, lang)
    if union is None:
        return None
    parties = db.user_led_parties(union, "telegram", message.from_user.id)
    if not parties:
        await message.reply(localized("party_none_led", lang))
        return None
    if len(parties) == 1:
        return parties[0]
    return await _numbered_choice_tg(message, lang, localized("party_choose", lang),
                                     parties, lambda p: _party_line(p, lang))

async def _offer_leadership_tg(message, party, lang, transfer):
    """Ask who should be made a leader, then put the offer to them with an
    inline keyboard.

    Nothing is written until they press Accept; `transfer=True` hands the party
    over instead of adding a co-leader."""
    prompt_key = "edit_ask_transfer" if transfer else "edit_ask_leader"
    m = await _dialog_text_tg(message, lang, localized(prompt_key, lang))
    if m is None:
        return
    ref = _parse_tg_user_ref(m.text or "")
    if ref is None:
        await message.reply(localized("edit_invalid_user", lang))
        return
    target_kind, target = ref
    mention = f"@{target}" if target_kind == "username" else str(target)

    action = "transfer" if transfer else "leader"
    token = _register_consent(action, party["code"], target_kind, target, lang)
    offer_key = "consent_transfer_offer" if transfer else "consent_leader_offer"
    await message.answer(
        localized(offer_key, lang, mention=mention, name=party["name"], code=party["code"]),
        reply_markup=_consent_keyboard(lang, token),
    )

async def _confirm_suspend_tg(message, party, lang):
    """Ask the caller to confirm suspending the party, or resuming it."""
    action = "resume" if party["suspended"] else "suspend"
    token = _register_consent(action, party["code"], "id", message.from_user.id, lang)
    ask_key = "resume_confirm" if party["suspended"] else "suspend_confirm"
    await message.reply(localized(ask_key, lang, name=party["name"]),
                        reply_markup=_consent_keyboard(lang, token))

async def _confirm_delete_party_tg(message, party, lang):
    """Ask the caller to confirm dissolving the party.

    The party's bank accounts go with it, which is why this one is behind an
    explicit confirmation."""
    token = _register_consent("delete", party["code"], "id", message.from_user.id, lang)
    await message.reply(
        localized("delete_party_confirm", lang, name=party["name"], code=party["code"]),
        reply_markup=_consent_keyboard(lang, token))

async def _edit_party_option_tg(message, party, lang, option, admin=False):
    """Run one numbered option of the edit menus.

    The Telegram side's single place a party field is validated and written,
    reached from `/edit_party`, `/edit_rules` and `/edit_party_admin`; `admin`
    unlocks the three options only a Bot Admin gets."""
    if option == 1:
        m = await _dialog_text_tg(message, lang, localized("edit_ask_name", lang))
        if m is None:
            return
        db.update_party_field(party["code"], "name",
                              clean_display_name(m.text or m.caption, max_len=100))
        await message.reply(localized("edit_saved", lang))
    elif option == 2:
        code = await _dialog_text_tg(message, lang, localized("edit_ask_code", lang),
                                     validator=_party_code_validator,
                                     error_key="add_party_invalid_code")
        if code is None:
            return
        db.rename_party_code(party["code"], code)
        await message.reply(localized("edit_saved", lang))
    elif option == 3:
        logo = await _dialog_logo_tg(message, lang, localized("edit_ask_logo", lang))
        if logo is None:
            return
        db.update_party_logo(party["code"], logo[0], logo[1])
        await message.reply(localized("edit_saved", lang))
    elif option == 4:
        def color_validator(value):
            """Accept '#rrggbb' or 'rrggbb', returning it normalised."""
            m2 = COLOR_RE.match(value.strip())
            return f"#{m2.group(1).lower()}" if m2 else None
        color = await _dialog_text_tg(message, lang, localized("edit_ask_color", lang),
                                      validator=color_validator,
                                      error_key="edit_invalid_color")
        if color is None:
            return
        db.update_party_field(party["code"], "color", color)
        await message.reply(localized("edit_saved", lang))
    elif option == 5:
        m = await _dialog_text_tg(message, lang, localized("edit_ask_description", lang))
        if m is None:
            return
        db.update_party_field(party["code"], "description", _markdown_from_tg(m)[:1024])
        await message.reply(localized("edit_saved", lang))
    elif option == 6:
        m = await _dialog_text_tg(message, lang, localized("edit_ask_ideologies", lang))
        if m is None:
            return
        db.update_party_field(party["code"], "ideologies", _markdown_from_tg(m)[:1024])
        await message.reply(localized("edit_saved", lang))
    elif option == 7:
        await _offer_leadership_tg(message, party, lang, transfer=False)
    elif option == 8:
        await _offer_leadership_tg(message, party, lang, transfer=True)
    elif option == 9:
        await _confirm_suspend_tg(message, party, lang)
    elif option == 10:
        def url_validator(value):
            """Accept only an http(s) link, as typed."""
            return value.strip() if URL_RE.match(value.strip()) else None
        url = await _dialog_text_tg(message, lang, localized("edit_ask_article", lang),
                                    validator=url_validator,
                                    error_key="edit_invalid_article")
        if url is None:
            return
        db.update_party_field(party["code"], "article_url", url)
        await message.reply(localized("edit_saved", lang))
    elif admin and option == 11:
        new_value = 0 if party["allow_member_invites"] else 1
        db.update_party_field(party["code"], "allow_member_invites", new_value)
        key = "rules_invites_enabled" if new_value else "rules_invites_disabled"
        await message.reply(localized(key, lang))
    elif admin and option == 12:
        def union_validator(value):
            """Accept a union code only if that union exists."""
            code = value.strip().upper()
            return code if db.union_exists(code) else None
        union = await _dialog_text_tg(message, lang, localized("edit_ask_union", lang),
                                      validator=union_validator,
                                      error_key="edit_invalid_union")
        if union is None:
            return
        db.update_party_union(party["code"], union)
        await message.reply(localized("edit_union_changed", lang, name=party["name"],
                                      union=db.get_union_name(union, lang)))
    elif admin and option == 13:
        await _confirm_delete_party_tg(message, party, lang)
    else:
        await message.reply(localized("edit_invalid_option", lang))
