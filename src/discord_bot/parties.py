"""The party as the user sees it: the card, the numbered edit menus, the
option handlers behind them, leadership offers, and the confirmations for
suspending and dissolving.

Everything here is shared with the Telegram half, which imports these helpers
by name (telegram_bot/parties.py). That is deliberate: what a party card
*contains* — description, rules, founder and leaders, allies, government seats,
bank balances — is a fact about the union, while only its formatting is a fact
about the platform. When a field is added to a party, it is added here once.

`_edit_party_option` is the one place a party field is validated and written,
and it is reached from three commands with different rights: `/edit-party`
(the founder and leaders), `/edit-rules` (the same people, rules only) and
`/edit-party-admin` (any Bot Admin, any union). The menu texts differ per
entry point, the writes do not.

`_resolve_party_union` is the gate every party command goes through except
`/edit-party-admin` — a Bot Admin must still be able to tidy up a union after
parties were switched off.

Not this module's zone: the commands themselves
(discord_bot/commands/parties.py), the join-request buttons, and storage
(db/parties.py).
"""
import io
import re

import discord
from discord import ui, ButtonStyle

import db
import economy
from message_relay import clean_display_name
from utils import (
    DEFAULT_EMBED_COLOR, PARTY_CODE_RE, find_close_names, format_stored_user,
    localized, send_service_event,
)

from discord_bot.client import _refuse_not_setup, parties_enabled
from discord_bot.dialogs import (
    _ConsentView, _LOGO_EXT, _dialog_logo, _dialog_text, _numbered_choice,
    _parse_user_ref, _say,
)

COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")

URL_RE = re.compile(r"^https?://\S+$")

async def _resolve_party_union(interaction: discord.Interaction, lang):
    """The union of this server, refusing when the server is not set up or the
    union has not turned parties on. Returns the union code, or None.

    Every party command goes through this — /edit-party-admin excepted, so a Bot
    Admin can still tidy up a union after parties were switched off."""
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return None
    chat = db.get_chat(str(interaction.guild_id))
    if not chat:
        await _refuse_not_setup(interaction, lang)
        return None
    if not parties_enabled(chat["union_code"]):
        await interaction.response.send_message(localized("party_feature_disabled", lang), ephemeral=True)
        return None
    return chat["union_code"]

def _party_code_validator(value):
    """Accept a 4-character party code only when it is free across the whole
    shared namespace. Returns the upper-cased code, or None so `_dialog_text`
    re-asks."""
    code = value.strip().upper()
    if PARTY_CODE_RE.match(code) and not db.party_code_taken(code):
        return code
    return None

def _party_color(party):
    """The party's colour as a discord.Color, falling back to the bot's
    default.

    Wrapped in a bare except on purpose: the colour is free text a leader typed,
    and a malformed one should show the default rather than break the card."""
    try:
        return discord.Color(int(party["color"].lstrip("#"), 16))
    except Exception:
        return discord.Color(DEFAULT_EMBED_COLOR)

def _party_line(party, lang):
    """One line naming a party in a list — 'Name [CODE]', with a pause mark
    when it is suspended. Shared with the Telegram half, which is why it takes
    `lang` even though it does not use it yet."""
    mark = " ⏸️" if party["suspended"] else ""
    return f"{party['name']} [{party['code']}]{mark}"

def _party_logo_file(party):
    """(discord.File, attachment url) for the party logo, or (None, None)."""
    if not party["logo"]:
        return None, None
    ext = _LOGO_EXT.get(party["logo_mime"], "png")
    fname = f"logo.{ext}"
    return discord.File(io.BytesIO(party["logo"]), filename=fname), f"attachment://{fname}"

def _party_balance_lines(party_code):
    """The party's bank balances, one line per account (only accounts that
    exist; a party gets one on its first incoming transfer or dues run)."""
    lines = []
    for acc in db.get_owner_accounts(*db.party_owner(party_code)):
        bank = db.get_bank(acc["bank_code"])
        if bank:
            lines.append(economy.format_money(acc["balance"], bank))
    return lines

def _leaders_differ_from_founder(party, leaders):
    """Whether the leader list is worth printing at all.

    False in the ordinary case of a party whose founder is its only leader — then
    the card would just repeat the founder line."""
    return not (
        len(leaders) == 1
        and leaders[0]["platform"] == party["founder_platform"]
        and leaders[0]["user_id"] == party["founder_id"]
    )

def _build_party_embed(party, lang):
    """Party info embed (logo as the top-right thumbnail) + file + link button view."""
    embed = discord.Embed(
        title=f"{party['name']} [{party['code']}]",
        color=_party_color(party),
    )
    if party["suspended"]:
        embed.description = localized("party_suspended_mark", lang)

    embed.add_field(
        name=localized("party_field_founder", lang),
        value=format_stored_user("discord", party["founder_platform"],
                                 party["founder_id"], party["founder_name"]),
        inline=False,
    )
    leaders = db.get_party_leaders(party["code"])
    if _leaders_differ_from_founder(party, leaders):
        embed.add_field(
            name=localized("party_field_leader", lang),
            value=", ".join(
                format_stored_user("discord", l["platform"], l["user_id"], l["display_name"])
                for l in leaders
            ) or "—",
            inline=False,
        )
    if party["ideologies"]:
        embed.add_field(name=localized("party_field_ideologies", lang),
                        value=party["ideologies"][:1024], inline=False)
    allies = db.get_party_allies(party["code"])
    if allies:
        embed.add_field(
            name=localized("party_field_allies", lang),
            value=", ".join(f"{a['name']} [{a['code']}]" for a in allies)[:1024],
            inline=False,
        )
    embed.add_field(name=localized("party_field_members", lang),
                    value=str(len(db.get_party_user_set(party["code"]))), inline=True)
    seats = db.party_govt_seats(party["code"])
    if seats:
        embed.add_field(name=localized("party_field_seats", lang),
                        value=str(seats), inline=True)
    balances = _party_balance_lines(party["code"])
    if balances:
        embed.add_field(name=localized("party_field_balance", lang),
                        value="\n".join(balances)[:1024], inline=False)
    if party["description"]:
        embed.add_field(name=localized("party_field_description", lang),
                        value=party["description"][:1024], inline=False)

    file, thumb = _party_logo_file(party)
    if thumb:
        embed.set_thumbnail(url=thumb)

    view = None
    if party["article_url"]:
        view = ui.View(timeout=None)
        view.add_item(ui.Button(label=localized("party_more_button", lang),
                                style=ButtonStyle.link, url=party["article_url"]))
    return embed, file, view

async def _send_party_info(interaction, party, lang):
    """Send a party card, attaching the logo and the article button only when
    the party has them."""
    embed, file, view = _build_party_embed(party, lang)
    kwargs = {"embed": embed}
    if file:
        kwargs["file"] = file
    if view:
        kwargs["view"] = view
    await _say(interaction, **kwargs)

async def _resolve_led_party(interaction, lang):
    """The party the caller manages in this union. Asks which one when several."""
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return None
    parties = db.user_led_parties(union, "discord", interaction.user.id)
    if not parties:
        await interaction.response.send_message(localized("party_none_led", lang), ephemeral=True)
        return None
    if len(parties) == 1:
        return parties[0]
    return await _numbered_choice(interaction, lang, localized("party_choose", lang),
                                  parties, lambda p: _party_line(p, lang))

def _edit_menu_text(party, lang):
    """The ten numbered options a founder or leader may run.

    The ⚠️ marks are the point: they flag the fields a party has not filled in
    yet, so the menu doubles as a checklist without needing a separate one."""
    lines = [
        localized("edit_menu_1", lang),
        localized("edit_menu_2", lang),
        localized("edit_menu_3", lang),
        localized("edit_menu_4", lang),
        ("" if party["description"] else "⚠️ ") + localized("edit_menu_5", lang),
        ("" if party["ideologies"] else "⚠️ ") + localized("edit_menu_6", lang),
        localized("edit_menu_7", lang),
        localized("edit_menu_8", lang),
        localized("edit_menu_9_resume" if party["suspended"] else "edit_menu_9", lang),
        ("" if party["article_url"] else "⚠️ ") + localized("edit_menu_10", lang),
    ]
    return "\n".join(lines)

def _rules_menu_text(party, lang):
    """The `/edit-rules` menu — a single toggle today, phrased with its
    current state so a leader can see what pressing it would do."""
    state = localized("state_yes" if party["allow_member_invites"] else "state_no", lang)
    return localized("rules_menu_1", lang, state=state)

def _edit_admin_menu_text(party, lang):
    """The whole party: the ten leader options, the rules toggle, and the two
    that only a Bot Admin gets — moving the party to another union and deleting
    it."""
    state = localized("state_yes" if party["allow_member_invites"] else "state_no", lang)
    return "\n".join([
        _edit_menu_text(party, lang),
        localized("edit_admin_menu_11", lang, state=state),
        localized("edit_admin_menu_12", lang),
        localized("edit_admin_menu_13", lang),
        "",
        localized("edit_admin_choose", lang),
    ])

async def _send_edit_menu(interaction, party, lang, title_key="edit_menu_title", body=None):
    """Render an edit menu as an embed in the party's own colour, with its
    logo.

    `title_key` selects between the party menu and the rules menu, and `body`
    overrides the text entirely for the admin menu, which has three more
    options."""
    if body is None:
        body = _edit_menu_text(party, lang) if title_key == "edit_menu_title" \
            else _rules_menu_text(party, lang)
    embed = discord.Embed(
        title=localized(title_key, lang, name=party["name"], code=party["code"]),
        description=body,
        color=_party_color(party),
    )
    file, thumb = _party_logo_file(party)
    kwargs = {"embed": embed}
    if thumb:
        embed.set_thumbnail(url=thumb)
        kwargs["file"] = file
    await _say(interaction, **kwargs)

async def _offer_leadership(interaction, party, lang, transfer):
    """Ask who should be made a leader, then put the offer to them with
    consent buttons.

    `transfer=True` makes it a handover — the target becomes the *only* leader —
    against simply adding a co-leader. Nothing is written until the target presses
    Accept: leadership is never imposed, not even by a founder."""
    prompt_key = "edit_ask_transfer" if transfer else "edit_ask_leader"
    msg = await _dialog_text(interaction, lang, localized(prompt_key, lang))
    if msg is None:
        return
    target_id = _parse_user_ref(msg.content)
    if target_id is None:
        await interaction.channel.send(localized("edit_invalid_user", lang))
        return

    offer_key = "consent_transfer_offer" if transfer else "consent_leader_offer"
    channel = interaction.channel

    async def on_accept(interaction2):
        """Write the grant and announce it — either adding a co-leader or
        handing the party over, depending on `transfer`."""
        display = str(interaction2.user)
        if transfer:
            db.set_party_leader(party["code"], "discord", target_id, display)
            text = localized("transfer_done", lang, user=f"<@{target_id}>", name=party["name"])
        else:
            db.add_party_leader(party["code"], "discord", target_id, display)
            text = localized("leader_added", lang, user=f"<@{target_id}>", name=party["name"])
        await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))

    async def on_decline(interaction2):
        """Say the offer was turned down. Nothing was written, so there is
        nothing to undo."""
        key = "transfer_declined" if transfer else "leader_declined"
        await channel.send(localized(key, lang))

    await channel.send(
        localized(offer_key, lang, mention=f"<@{target_id}>",
                  name=party["name"], code=party["code"]),
        view=_ConsentView(lang, target_id, on_accept, on_decline),
        allowed_mentions=discord.AllowedMentions(users=True),
    )

async def _confirm_suspend(interaction, party, lang):
    """Ask the caller to confirm suspending the party, or resuming it.

    One function for both directions, because it is one menu entry whose wording
    flips with the party's current state."""
    resume = bool(party["suspended"])
    channel = interaction.channel
    ask_key = "resume_confirm" if resume else "suspend_confirm"

    async def on_accept(interaction2):
        """Flip the suspended flag and say which way it went."""
        db.update_party_field(party["code"], "suspended", 0 if resume else 1)
        key = "resume_done" if resume else "suspend_done"
        await channel.send(localized(key, lang, name=party["name"]))

    async def on_decline(interaction2):
        """Say the action was cancelled."""
        await channel.send(localized("action_cancelled", lang))

    await _say(interaction, localized(ask_key, lang, name=party["name"]),
               view=_ConsentView(lang, interaction.user.id, on_accept, on_decline))

async def _confirm_delete_party(interaction, party, lang):
    """Ask the caller to confirm dissolving the party.

    Everything the confirmation needs is captured *before* the buttons go out —
    name, code, union, who asked — because by the time somebody presses Accept the
    party row is about to stop existing. Dissolving also takes the party's bank
    accounts with it, which is why it is the one action behind a confirmation this
    explicit."""
    channel = interaction.channel
    name, code, union = party["name"], party["code"], party["union_code"]
    actor = str(interaction.user)

    async def on_accept(interaction2):
        """Dissolve the party, tell the chat, and report it to the service
        chats — the last chance to see the party's name is here."""
        db.delete_party(code)
        await channel.send(localized("delete_party_done", lang, name=name, code=code))
        await send_service_event("party_deleted", name=name, party=code,
                                 union=union, user=actor)

    async def on_decline(interaction2):
        """Say the action was cancelled."""
        await channel.send(localized("action_cancelled", lang))

    await _say(interaction, localized("delete_party_confirm", lang, name=name, code=code),
               view=_ConsentView(lang, interaction.user.id, on_accept, on_decline))

async def _edit_party_option(interaction, party, lang, option, admin=False):
    """Run one numbered option of the edit menus.

    The single place a party field is validated and written, reached from three
    commands with different rights: `/edit-party` and `/edit-rules` for the
    founder and leaders, `/edit-party-admin` for a Bot Admin. `admin` unlocks
    options 11–13 — the invite rule, moving the party to another union, and
    dissolving it — and nothing else differs between the callers."""
    if option == 1:
        msg = await _dialog_text(interaction, lang, localized("edit_ask_name", lang))
        if msg is None:
            return
        db.update_party_field(party["code"], "name", clean_display_name(msg.content, max_len=100))
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 2:
        code = await _dialog_text(interaction, lang, localized("edit_ask_code", lang),
                                  validator=_party_code_validator,
                                  error_key="add_party_invalid_code")
        if code is None:
            return
        db.rename_party_code(party["code"], code)
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 3:
        logo = await _dialog_logo(interaction, lang, localized("edit_ask_logo", lang))
        if logo is None:
            return
        db.update_party_logo(party["code"], logo[0], logo[1])
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 4:
        def color_validator(value):
            """Accept '#rrggbb' or 'rrggbb', returning it normalised."""
            m = COLOR_RE.match(value.strip())
            return f"#{m.group(1).lower()}" if m else None
        color = await _dialog_text(interaction, lang, localized("edit_ask_color", lang),
                                   validator=color_validator, error_key="edit_invalid_color")
        if color is None:
            return
        db.update_party_field(party["code"], "color", color)
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 5:
        msg = await _dialog_text(interaction, lang, localized("edit_ask_description", lang))
        if msg is None:
            return
        db.update_party_field(party["code"], "description", (msg.content or "").strip()[:1024])
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 6:
        msg = await _dialog_text(interaction, lang, localized("edit_ask_ideologies", lang))
        if msg is None:
            return
        db.update_party_field(party["code"], "ideologies", (msg.content or "").strip()[:1024])
        await interaction.channel.send(localized("edit_saved", lang))
    elif option == 7:
        await _offer_leadership(interaction, party, lang, transfer=False)
    elif option == 8:
        await _offer_leadership(interaction, party, lang, transfer=True)
    elif option == 9:
        await _confirm_suspend(interaction, party, lang)
    elif option == 10:
        def url_validator(value):
            """Accept only an http(s) link, as typed."""
            return value.strip() if URL_RE.match(value.strip()) else None
        url = await _dialog_text(interaction, lang, localized("edit_ask_article", lang),
                                 validator=url_validator, error_key="edit_invalid_article")
        if url is None:
            return
        db.update_party_field(party["code"], "article_url", url)
        await interaction.channel.send(localized("edit_saved", lang))
    elif admin and option == 11:
        new_value = 0 if party["allow_member_invites"] else 1
        db.update_party_field(party["code"], "allow_member_invites", new_value)
        key = "rules_invites_enabled" if new_value else "rules_invites_disabled"
        await _say(interaction, localized(key, lang))
    elif admin and option == 12:
        def union_validator(value):
            """Accept a union code only if that union exists."""
            code = value.strip().upper()
            return code if db.union_exists(code) else None
        union = await _dialog_text(interaction, lang, localized("edit_ask_union", lang),
                                   validator=union_validator, error_key="edit_invalid_union")
        if union is None:
            return
        db.update_party_union(party["code"], union)
        await interaction.channel.send(
            localized("edit_union_changed", lang, name=party["name"],
                      union=db.get_union_name(union, lang)))
    elif admin and option == 13:
        await _confirm_delete_party(interaction, party, lang)
    else:
        await _say(interaction, localized("edit_invalid_option", lang), ephemeral=True)

def _search_party(union_code, query):
    """(exact_match_or_None, suggestions). Falls back to 3 random union parties."""
    q = (query or "").strip()
    parties = db.get_union_parties(union_code)
    for p in parties:
        if p["code"].lower() == q.lower() or p["name"].lower() == q.lower():
            return p, []
    candidates = {}
    for p in parties:
        candidates.setdefault(p["name"].lower(), p)
        candidates.setdefault(p["code"].lower(), p)
    close = find_close_names(q, candidates)
    if not close:
        close = list(db.get_random_union_parties(union_code, 3))
    return None, close
