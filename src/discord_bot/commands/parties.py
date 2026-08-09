"""The party commands: founding, editing, the card, and the whole membership
path from a join request to a kick.

`/add-party` is a dialog rather than an argument list — name, code, logo — and
the founder may be someone else when a Bot Admin runs it. Joining is a request:
`JoinRequestView` is the one **persistent** view in this bot, restored on every
start by client.py: setup_hook, so a request survives a restart and its buttons
still work. Its custom_id shape is what the restore depends on; changing it
kills every button already hanging in Discord.

The card, the edit menus and the option handlers are not here — they are in
discord_bot/parties.py, shared with the Telegram half.

Not this module's zone: the card and menus (discord_bot/parties.py), the party
switch of a union (commands/unions.py), party money (commands/trade.py).
"""
import discord
from discord import app_commands, ui, ButtonStyle

import db
from message_relay import clean_display_name
from utils import DEFAULT_LANG, get_chat_lang, is_admin, localized, send_service_event

from discord_bot.client import _chat_key, _require_verified, bot
from discord_bot.dialogs import (
    USER_REF_RE, _dialog_logo, _dialog_text, _numbered_choice, _parse_user_ref,
    _say, _wait_message,
)
from discord_bot.parties import (
    _edit_admin_menu_text, _edit_party_option, _party_code_validator,
    _party_line, _resolve_led_party, _resolve_party_union, _search_party,
    _send_edit_menu, _send_party_info,
)

@bot.tree.command(name="add-party", description="found a new party in this union (interactive dialog)")
@app_commands.describe(founder="Founder's user ID or mention (bot admins only)")
async def add_party_cmd(interaction: discord.Interaction, founder: str = None):
    """Found a party through a dialog: name, code, logo.

    The union must have parties on and the caller must hold one of the roles
    `/allow-parties` named. `founder` lets a Bot Admin found a party on somebody
    else's behalf; for everyone else the caller is the founder."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_verified(interaction, lang):
        return
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return
    _enabled, role_ids = db.get_party_settings(union)

    caller_is_bot_admin = is_admin("discord", interaction.user.id)
    if founder is not None and not caller_is_bot_admin:
        await interaction.response.send_message(localized("add_party_founder_admin_only", lang), ephemeral=True)
        return
    if not caller_is_bot_admin:
        member_roles = {r.id for r in getattr(interaction.user, "roles", [])}
        if not member_roles & role_ids:
            await interaction.response.send_message(localized("add_party_no_role", lang), ephemeral=True)
            return

    founder_id = interaction.user.id
    if founder is not None:
        parsed = _parse_user_ref(founder)
        if parsed is None:
            await interaction.response.send_message(localized("edit_invalid_user", lang), ephemeral=True)
            return
        founder_id = parsed
    founder_name = str(interaction.user)
    if founder_id != interaction.user.id:
        try:
            founder_user = await bot.fetch_user(founder_id)
            founder_name = str(founder_user)
        except Exception:
            founder_name = str(founder_id)

    name_msg = await _dialog_text(interaction, lang, localized("add_party_ask_name", lang))
    if name_msg is None:
        return
    name = clean_display_name(name_msg.content, max_len=100)

    code = await _dialog_text(
        interaction, lang, localized("add_party_ask_code", lang),
        validator=_party_code_validator, error_key="add_party_invalid_code",
    )
    if code is None:
        return

    logo = await _dialog_logo(interaction, lang, localized("add_party_ask_logo", lang))
    if logo is None:
        return

    db.create_party(code, union, name, "discord", founder_id, founder_name,
                    logo=logo[0], logo_mime=logo[1])
    await interaction.channel.send(
        localized("add_party_created", lang, name=name, code=code,
                  union=db.get_union_name(union, lang),
                  founder=f"<@{founder_id}>"),
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    await send_service_event("party_created", name=name, party=code, union=union,
                             user=str(interaction.user))

@bot.tree.command(name="edit-party", description="manage your party (founders and party leaders)")
@app_commands.describe(option="Option number 1-10; omit to see the menu")
async def edit_party_cmd(interaction: discord.Interaction, option: int = None):
    """Manage your party through a numbered menu (founders and party leaders).

    Without an option number the menu is shown. Which party is meant is worked out
    from what the caller leads in this union."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_verified(interaction, lang):
        return
    party = await _resolve_led_party(interaction, lang)
    if party is None:
        return

    if option is None:
        await _send_edit_menu(interaction, party, lang)
        return
    await _edit_party_option(interaction, party, lang, option)

@bot.tree.command(name="edit-party-admin", description="edit any party of any union (bot admins)")
@app_commands.describe(party="Party code, or its full name exactly as written")
async def edit_party_admin_cmd(interaction: discord.Interaction, party: str):
    """Edit any party of any union by code or exact name (Bot Admins).

    The one party command that does not go through the parties-enabled gate, so
    that a union with parties switched off can still be tidied up. Unlocks three
    extra options: the invite rule, moving the party to another union, and
    dissolving it."""
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    row = db.find_party(party)
    if row is None:
        await interaction.response.send_message(
            localized("edit_admin_not_found", lang, query=party.strip()), ephemeral=True)
        return

    await _send_edit_menu(interaction, row, lang, title_key="edit_admin_menu_title",
                          body=_edit_admin_menu_text(row, lang))

    msg = await _wait_message(interaction.channel_id, interaction.user.id)
    if msg is None:
        return
    value = (msg.content or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= 13):
        await interaction.channel.send(localized("choice_invalid", lang))
        return
    await _edit_party_option(interaction, row, lang, int(value), admin=True)

@bot.tree.command(name="edit-rules", description="manage your party's rules (founders and party leaders)")
@app_commands.describe(option="Option number; omit to see the menu")
async def edit_rules_cmd(interaction: discord.Interaction, option: int = None):
    """Manage your party's rules (founders and party leaders) — today the one
    toggle that decides whether ordinary members may invite."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_verified(interaction, lang):
        return
    party = await _resolve_led_party(interaction, lang)
    if party is None:
        return

    if option is None:
        await _send_edit_menu(interaction, party, lang, title_key="rules_menu_title")
        return

    if option == 1:
        new_value = 0 if party["allow_member_invites"] else 1
        db.update_party_field(party["code"], "allow_member_invites", new_value)
        key = "rules_invites_enabled" if new_value else "rules_invites_disabled"
        await _say(interaction, localized(key, lang))
    else:
        await _say(interaction, localized("edit_invalid_option", lang), ephemeral=True)

@bot.tree.command(name="party", description="information about a party of this union")
@app_commands.describe(query="Party code or name")
async def party_cmd(interaction: discord.Interaction, query: str):
    """Show a party of this union: card, leaders, allies, seats, balances.

    Falls back to a numbered choice of near matches, and then to a few random
    parties of the union, so an unrecognised query still shows what exists."""
    lang = get_chat_lang(_chat_key(interaction))
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return

    party, suggestions = _search_party(union, query)
    if party is None:
        if not suggestions:
            await interaction.response.send_message(localized("party_not_found", lang))
            return
        party = await _numbered_choice(interaction, lang,
                                       localized("party_not_found_suggest", lang),
                                       suggestions, lambda p: _party_line(p, lang))
        if party is None:
            return
    await _send_party_info(interaction, party, lang)

def _find_party_exact(union_code, query):
    """Exact match by code or name (case-insensitive), as /party-join requires."""
    q = (query or "").strip()
    for p in db.get_union_parties(union_code):
        if p["code"].lower() == q.lower() or p["name"].lower() == q.lower():
            return p
    return None

class JoinRequestView(ui.View):
    """Persistent Accept/Reject buttons on a leader's DM. custom_ids embed the
    request id so the buttons keep working after a restart (views are re-added
    in setup_hook)."""

    def __init__(self, request_id, lang):
        """Build the two buttons with custom_ids carrying the request id.

        The view has no timeout, which is what makes it *persistent*: its buttons
        are re-armed on every start by client.py: setup_hook, so a request posted
        last week still works. The custom_id shape is what the restore matches —
        changing it kills every button already hanging in Discord."""
        super().__init__(timeout=None)
        self.request_id = request_id
        accept = ui.Button(label=localized("party_join_accept", lang),
                           style=ButtonStyle.success, custom_id=f"pj:{request_id}:1")
        reject = ui.Button(label=localized("party_join_reject", lang),
                           style=ButtonStyle.danger, custom_id=f"pj:{request_id}:0")
        accept.callback = self._make_cb(True)
        reject.callback = self._make_cb(False)
        self.add_item(accept)
        self.add_item(reject)

    def _make_cb(self, accepted):
        """One button's callback, closing over whether it means accept."""
        async def callback(interaction: discord.Interaction):
            """Hand the press to _resolve_join_request, which re-checks that the
            request still exists and that the presser leads the party."""
            await _resolve_join_request(interaction, self.request_id, accepted)
        return callback

async def _notify_discord_leaders(party, request_id, lang, requester_display):
    """DM every Discord leader of the party (the founder is always one). Returns
    how many were reached."""
    text = localized("party_join_leader_dm", lang, requester=requester_display,
                     name=party["name"], code=party["code"])
    sent = 0
    for l in db.get_party_leaders(party["code"]):
        if l["platform"] != "discord":
            continue
        try:
            user = await bot.fetch_user(int(l["user_id"]))
            await user.send(text, view=JoinRequestView(request_id, lang))
            sent += 1
        except Exception:
            pass
    return sent

async def _notify_join_result(req, party, accepted):
    """DM the requester the leaders' verdict, in the language they asked
    in.

    Swallows a closed DM: a person who blocks the bot still gets (or does not get)
    their membership — the notice is not what makes it real."""
    lang = req["lang"] or DEFAULT_LANG
    key = "party_join_accepted_user" if accepted else "party_join_rejected_user"
    text = localized(key, lang, name=party["name"], code=party["code"])
    if req["requester_platform"] == "discord":
        try:
            u = await bot.fetch_user(int(req["requester_id"]))
            await u.send(text)
        except Exception:
            pass
    else:
        try:
            from telegram_bot import bot as tg
            await tg.send_message(int(req["requester_id"]), text)
        except Exception:
            pass

async def _resolve_join_request(interaction: discord.Interaction, request_id, accepted):
    """Apply a leader's verdict to a join request.

    Checks that the request still exists and that the presser leads the party, adds
    the member on acceptance, deletes the row either way, and tells the requester.
    Written so that a button pressed twice — or one restored from before a restart
    — answers that the request is gone rather than acting again."""
    req = db.get_join_request(request_id)
    if not req:
        try:
            await interaction.response.edit_message(
                content=localized("party_join_already_handled", get_chat_lang(_chat_key(interaction))),
                view=None)
        except Exception:
            pass
        return
    lang = req["lang"] or DEFAULT_LANG
    party = db.get_party(req["party_code"])
    if not party:
        db.delete_join_request(request_id)
        try:
            await interaction.response.edit_message(content=localized("party_not_found", lang), view=None)
        except Exception:
            pass
        return
    if not db.is_party_leader(party["code"], "discord", interaction.user.id):
        await interaction.response.send_message(localized("party_join_not_leader", lang), ephemeral=True)
        return

    db.delete_join_request(request_id)
    if accepted:
        db.add_party_member(party["code"], req["requester_platform"],
                            req["requester_id"], req["requester_name"])
    outcome = "party_join_accepted_leader" if accepted else "party_join_rejected_leader"
    try:
        await interaction.response.edit_message(
            content=localized(outcome, lang, requester=req["requester_name"], name=party["name"]),
            view=None)
    except Exception:
        pass
    await _notify_join_result(req, party, accepted)

@bot.tree.command(name="party-join", description="request to join a party of this union")
@app_commands.describe(query="Party code or its exact name")
async def party_join_cmd(interaction: discord.Interaction, query: str):
    """Ask to join a party of this union.

    Writes a request and sends Accept/Decline buttons to the leaders. Refused for a
    suspended party, for somebody already in a party of this union, and for a
    second request to the same party."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_verified(interaction, lang):
        return
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return

    party = _find_party_exact(union, query)
    if not party:
        await interaction.response.send_message(localized("party_not_found", lang))
        return
    if party["suspended"]:
        await interaction.response.send_message(localized("party_join_suspended", lang), ephemeral=True)
        return

    existing = db.find_user_party(union, "discord", interaction.user.id)
    if existing:
        if existing["code"] == party["code"]:
            await interaction.response.send_message(localized("party_join_already_member", lang), ephemeral=True)
        else:
            await interaction.response.send_message(
                localized("party_join_in_other", lang, name=existing["name"]), ephemeral=True)
        return
    if db.has_open_join_request(party["code"], "discord", interaction.user.id):
        await interaction.response.send_message(localized("party_join_pending", lang), ephemeral=True)
        return

    rid = db.create_join_request(party["code"], "discord", interaction.user.id,
                                 str(interaction.user), lang)
    delivered = await _notify_discord_leaders(party, rid, lang, f"<@{interaction.user.id}>")
    if delivered == 0:
        db.delete_join_request(rid)
        await interaction.response.send_message(localized("party_join_no_leader", lang), ephemeral=True)
        return
    await interaction.response.send_message(localized("party_join_sent", lang, name=party["name"]), ephemeral=True)

@bot.tree.command(name="party-leave", description="leave your party in this union")
async def party_leave_cmd(interaction: discord.Interaction):
    """Leave your party in this union.

    Only for rank-and-file members: a founder cannot walk away from their own party
    — they transfer or dissolve it."""
    lang = get_chat_lang(_chat_key(interaction))
    union = await _resolve_party_union(interaction, lang)
    if union is None:
        return
    party = db.find_user_party(union, "discord", interaction.user.id)
    if not party:
        await interaction.response.send_message(localized("party_leave_none", lang), ephemeral=True)
        return
    if db.is_founder(party["code"], "discord", interaction.user.id):
        await interaction.response.send_message(localized("party_leave_founder", lang), ephemeral=True)
        return
    db.remove_party_leader(party["code"], "discord", interaction.user.id)
    db.remove_party_member(party["code"], "discord", interaction.user.id)
    await interaction.response.send_message(localized("party_leave_done", lang, name=party["name"]), ephemeral=True)

def _find_kick_target(code, platform, target):
    """Locate a plain member of the party by ID/mention or by stored nickname."""
    raw = (target or "").strip()
    m = USER_REF_RE.match(raw)
    tid = (m.group(1) or m.group(2)) if m else None
    name = raw.lstrip("@").lower()
    for mem in db.get_party_members(code):
        if mem["platform"] != platform:
            continue
        if tid and mem["user_id"] == tid:
            return mem
        if (mem["display_name"] or "").lstrip("@").lower() == name:
            return mem
    return None

@bot.tree.command(name="party-kick", description="remove a member from your party (party leaders)")
@app_commands.describe(target="Member's ID, mention or username")
async def party_kick_cmd(interaction: discord.Interaction, target: str):
    """Remove a member from your party (party leaders).

    Reaches rank-and-file members only; a leader has to be stripped of their
    appointment first."""
    lang = get_chat_lang(_chat_key(interaction))
    party = await _resolve_led_party(interaction, lang)
    if party is None:
        return

    mem = _find_kick_target(party["code"], "discord", target)
    if mem is None:
        m = USER_REF_RE.match(target.strip())
        tid = (m.group(1) or m.group(2)) if m else None
        if tid and db.is_founder(party["code"], "discord", tid):
            await _say(interaction, localized("party_kick_founder", lang), ephemeral=True)
        elif tid and db.is_party_leader(party["code"], "discord", tid):
            await _say(interaction, localized("party_kick_leader", lang), ephemeral=True)
        else:
            await _say(interaction, localized("party_kick_not_member", lang), ephemeral=True)
        return

    db.remove_party_member(party["code"], "discord", mem["user_id"])
    await _say(interaction,
               localized("party_kick_done", lang, user=f"<@{mem['user_id']}>", name=party["name"]),
               allowed_mentions=discord.AllowedMentions(users=True))
