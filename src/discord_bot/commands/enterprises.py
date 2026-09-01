"""Enterprises: founding one, its card, the `/edit-enterprise` menu, its
workers and positions, and everything it sells or exports.

An enterprise is a party with a warehouse and a payroll, and the commands
mirror that: a dialog to found it, consent buttons to admit a worker or accept
an export, a numbered edit menu behind one command. Salaries come in two shapes
and the difference matters — an exact amount in minor units, or a percent of
the period's sales stored as the number the user typed.

An export is the one action that always needs the *receiving* side's consent.
Once accepted, the goods leave the warehouse immediately and a priced sale
escrows the payment; the shipment then travels and lands later
(economy/logistics.py, main.py: shipment_loop), which is why these commands
report an ETA rather than a result.

Not this module's zone: the transit physics (economy/logistics.py), payroll
(economy/payroll.py) and the rows (db/enterprises.py).
"""
import io
from datetime import datetime, timezone

import discord
from discord import app_commands

import db
import economy
from message_relay import clean_display_name
from utils import (
    DEFAULT_EMBED_COLOR, format_stored_user, get_chat_lang, good_display_name,
    is_admin, localized, rate_limit_ok, send_service_event,
)

from discord_bot.client import _chat_key, bot
from discord_bot.dialogs import (
    _ConsentView, _EntConsentView, _LOGO_EXT, _dialog_logo, _dialog_text,
    _econ_embed, _ereply, _extract_logo, _numbered_choice, _parse_user_ref,
    _say, _wait_message,
)
from discord_bot.parties import _party_code_validator

def _fmt_eta(seconds):
    """mm:ss for short waits, h:mm:ss once a shipment runs into hours."""
    seconds = max(int(seconds), 0)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"

def _distance_label(distance, lang):
    """The localized name of a transport distance band."""
    return localized(f"transport_{distance}", lang)

def _ent_line(ent):
    """One line naming an enterprise in a list — 'Name [CODE]'."""
    return f"{ent['name']} [{ent['code']}]"

async def _resolve_led_enterprise(interaction, lang, query=None):
    """The enterprise the caller leads: the named one, the only one, or a
    numbered choice among several."""
    if query:
        ent = db.find_enterprise(query)
        if not ent:
            await _ereply(interaction, "enterprise_not_found", lang)
            return None
        if not (db.is_enterprise_leader(ent["code"], "discord", interaction.user.id)
                or is_admin("discord", interaction.user.id)):
            await _ereply(interaction, "ent_not_leader", lang)
            return None
        return ent
    ents = db.user_led_enterprises("discord", interaction.user.id)
    if not ents:
        await _ereply(interaction, "ent_none_led", lang)
        return None
    if len(ents) == 1:
        return ents[0]
    return await _numbered_choice(interaction, lang, localized("ent_choose", lang),
                                  ents, _ent_line)

@bot.tree.command(name="add-enterprise", description="found an enterprise on this server (interactive dialog)")
async def add_enterprise_cmd(interaction: discord.Interaction):
    """Found an enterprise on this server through a dialog: name, code,
    description, logo.

    The server it is founded on is fixed for life — it decides which monthly task
    the enterprise works toward and how far its exports have to travel. The founder
    becomes its first leader."""
    lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return
    if not db.is_setup(str(interaction.guild_id)):
        await _ereply(interaction, "chat_not_setup", lang)
        return
    if not rate_limit_ok(f"addent|discord|{interaction.user.id}", 3, 86400):
        await _ereply(interaction, "rate_limited", lang)
        return
    name_msg = await _dialog_text(interaction, lang, localized("add_ent_ask_name", lang),
                                  as_embed=True)
    if name_msg is None:
        return
    name = clean_display_name(name_msg.content, max_len=60)
    code = await _dialog_text(interaction, lang, localized("add_ent_ask_code", lang),
                              validator=_party_code_validator, error_key="add_ent_bad_code",
                              as_embed=True)
    if code is None:
        return
    desc_msg = await _dialog_text(interaction, lang, localized("add_ent_ask_desc", lang),
                                  as_embed=True)
    if desc_msg is None:
        return
    description = (desc_msg.content or "").strip()
    description = None if description == "-" else description[:500]
    await interaction.channel.send(embed=_econ_embed(localized("add_ent_ask_logo", lang)))
    logo = logo_mime = None
    for _ in range(5):
        msg = await _wait_message(interaction.channel_id, interaction.user.id)
        if msg is None:
            await interaction.channel.send(embed=_econ_embed(localized("dialog_timeout", lang)))
            return
        if (msg.content or "").strip() == "-":
            break
        got = await _extract_logo(msg)
        if got:
            logo, logo_mime = got
            break
        await interaction.channel.send(embed=_econ_embed(localized("add_party_invalid_logo", lang)))
    db.create_enterprise(code, name, "discord", interaction.guild_id,
                         "discord", interaction.user.id, str(interaction.user),
                         description=description, logo=logo, logo_mime=logo_mime)
    await interaction.channel.send(embed=_econ_embed(
        localized("add_ent_created", lang, name=name, code=code)))
    await send_service_event("enterprise_created", name=name, code=code,
                             user=str(interaction.user))

def _build_enterprise_embed(ent, lang, viewer_platform="discord"):
    """The enterprise card: description, leaders, workers, positions and
    their salaries, the salary currency and period, stock, and a summary of what is
    in transit.

    Takes `viewer_platform` because the Telegram half renders the same content: a
    Discord member shown on Telegram has to be a stored name, not a ping that would
    not resolve."""
    embed = discord.Embed(title=f"{ent['name']} [{ent['code']}]",
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    if ent["description"]:
        embed.description = ent["description"][:2000]
    guild = bot.get_guild(int(ent["server_id"])) if ent["platform"] == "discord" else None
    embed.add_field(name=localized("ent_field_server", lang),
                    value=(guild.name if guild else str(ent["server_id"])), inline=True)
    leaders = db.get_enterprise_leaders(ent["code"])
    embed.add_field(
        name=localized("ent_field_leaders", lang),
        value=", ".join(format_stored_user(viewer_platform, l["platform"], l["user_id"],
                                           l["display_name"]) for l in leaders) or "—",
        inline=False)
    members = db.get_enterprise_members(ent["code"])
    if members:
        lines = []
        for m in members[:15]:
            pos = f" — {m['position']}" if m["position"] else ""
            lines.append(format_stored_user(viewer_platform, m["platform"], m["user_id"],
                                            m["display_name"]) + pos)
        more = len(members) - 15
        if more > 0:
            lines.append(f"… +{more}")
        embed.add_field(name=localized("ent_field_workers", lang, count=len(members)),
                        value="\n".join(lines)[:1024], inline=False)
    balances = []
    for acc in db.get_owner_accounts(*db.enterprise_owner(ent["code"])):
        bank = db.get_bank(acc["bank_code"])
        if bank:
            balances.append(economy.format_money(acc["balance"], bank))
    if balances:
        embed.add_field(name=localized("ent_field_balance", lang),
                        value="\n".join(balances)[:1024], inline=True)
    period = localized(f"salary_period_{ent['salary_period'] or 'monthly'}", lang)
    salary_bank = ent["salary_bank"] or "—"
    embed.add_field(name=localized("ent_field_salary", lang),
                    value=f"{period} · {salary_bank}", inline=True)
    positions = db.get_enterprise_positions(ent["code"])
    if positions:
        plines = []
        for p in positions:
            if p["percent"] is not None:
                sal = localized("salary_percent", lang, percent=f"{p['percent']:g}")
            else:
                sal = economy.format_amount(p["amount"] or 0)
            plines.append(f"{p['position']}: {sal}")
        embed.add_field(name=localized("ent_field_positions", lang),
                        value="\n".join(plines)[:1024], inline=False)
    inv = db.get_inventory(db.enterprise_owner(ent["code"]))
    if inv:
        glines = []
        for row in inv[:8]:
            emoji = f"{row['emoji']} " if row["emoji"] else ""
            good = db.get_good(row["good_code"])
            nm = good_display_name(good, lang) if good else row["name"]
            glines.append(f"{emoji}{nm} [{row['good_code']}] ×{row['qty']}")
        embed.add_field(name=localized("ent_field_goods", lang),
                        value="\n".join(glines)[:1024], inline=False)
    transit = db.get_enterprise_shipments(ent["code"])
    if transit:
        embed.add_field(name=localized("ent_field_transit", lang, count=len(transit)),
                        value="\n".join(_transit_lines(ent["code"], transit[:8], lang))[:1024],
                        inline=False)
    return embed

def _transit_lines(ent_code, shipments, lang):
    """Render in-transit shipments from the point of view of `ent_code`: a ➡️
    line for cargo it is sending, a ⬅️ line for cargo coming in."""
    now = int(datetime.now(timezone.utc).timestamp())
    lines = []
    for s in shipments:
        good = db.get_good(s["good_code"])
        emoji = f"{good['emoji']} " if good and good["emoji"] else ""
        name = good_display_name(good, lang) if good else s["good_code"]
        eta = _fmt_eta(max(int(s["arrive_at"]) - now, 0))
        if s["from_ent"] == ent_code:
            other = db.get_enterprise(s["to_ent"])
            key, label = "transit_line_out", (_ent_line(other) if other else s["to_ent"])
        else:
            other = db.get_enterprise(s["from_ent"])
            key, label = "transit_line_in", (_ent_line(other) if other else s["from_ent"])
        lines.append(localized(key, lang, emoji=emoji, name=name, qty=s["qty"],
                               other=label, eta=eta))
    return lines

@bot.tree.command(name="enterprise", description="an enterprise's card, or this server's enterprises")
@app_commands.describe(query="Enterprise code or name (omit to list this server's enterprises)")
async def enterprise_cmd(interaction: discord.Interaction, query: str = None):
    """Show an enterprise's card, or list this server's enterprises."""
    lang = get_chat_lang(_chat_key(interaction))
    if not query:
        ents = db.get_server_enterprises("discord", str(interaction.guild_id)) \
            if interaction.guild_id else []
        if not ents:
            await _ereply(interaction, "ent_list_empty", lang)
            return
        lines = [_ent_line(e) for e in ents]
        await _say(interaction, embed=_econ_embed("\n".join(lines)[:4000],
                                                  title=localized("ent_list_header", lang)))
        return
    ent = db.find_enterprise(query)
    if not ent:
        await _ereply(interaction, "enterprise_not_found", lang)
        return
    embed = _build_enterprise_embed(ent, lang)
    if ent["logo"]:
        ext = _LOGO_EXT.get(ent["logo_mime"], "png")
        file = discord.File(io.BytesIO(ent["logo"]), filename=f"logo.{ext}")
        embed.set_thumbnail(url=f"attachment://logo.{ext}")
        await _say(interaction, embed=embed, file=file)
    else:
        await _say(interaction, embed=embed)

_EDIT_ENT_OPTIONS = ("name", "description", "logo", "add_leader", "transfer",
                     "salary_bank", "salary_period", "delete")

@bot.tree.command(name="edit-enterprise", description="manage your enterprise (founder and leaders)")
@app_commands.describe(option="Menu item number", enterprise="Enterprise code (when you lead several)")
async def edit_enterprise_cmd(interaction: discord.Interaction, option: int = None,
                              enterprise: str = None):
    """Manage your enterprise through a numbered menu (founder and leaders).

    Covers the name, description, logo, salary currency and period, leadership
    offers and dissolving. Renaming the code rewrites every reference — an
    enterprise is addressed three different ways in the schema."""
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_ENT_OPTIONS):
        items = [localized(f"edit_ent_opt_{key}", lang) for key in _EDIT_ENT_OPTIONS]
        chosen = await _numbered_choice(
            interaction, lang,
            localized("edit_ent_menu_header", lang, name=ent["name"]),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_ENT_OPTIONS[chosen[0]]
    else:
        action = _EDIT_ENT_OPTIONS[option - 1]

    if action == "name":
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_name", lang),
                                 as_embed=True)
        if msg is None:
            return
        db.update_enterprise_field(ent["code"], "name",
                                   clean_display_name(msg.content, max_len=60))
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action == "description":
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_desc", lang),
                                 as_embed=True)
        if msg is None:
            return
        text = (msg.content or "").strip()
        db.update_enterprise_field(ent["code"], "description",
                                   None if text == "-" else text[:500])
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action == "logo":
        logo = await _dialog_logo(interaction, lang, localized("edit_ent_ask_logo", lang))
        if logo is None:
            return
        db.update_enterprise_logo(ent["code"], logo[0], logo[1])
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action in ("add_leader", "transfer"):
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_leader", lang),
                                 as_embed=True)
        if msg is None:
            return
        target_id = _parse_user_ref((msg.content or "").strip())
        if target_id is None:
            await interaction.channel.send(embed=_econ_embed(localized("edit_invalid_user", lang)))
            return
        transfer = action == "transfer"
        channel = interaction.channel

        async def on_accept(interaction2):
            """Write the grant now that the target has agreed — adding a leader, or
            handing the enterprise over when this was a transfer."""
            display = str(interaction2.user)
            if transfer:
                db.set_enterprise_leader(ent["code"], "discord", target_id, display)
                key = "ent_transfer_done"
            else:
                db.add_enterprise_leader(ent["code"], "discord", target_id, display)
                key = "ent_leader_added"
            await channel.send(embed=_econ_embed(
                localized(key, lang, user=f"<@{target_id}>", name=ent["name"])))

        async def on_decline(interaction2):
            """Say the offer was turned down. Nothing was written."""
            key = "ent_transfer_declined" if transfer else "ent_leader_declined"
            await channel.send(embed=_econ_embed(localized(key, lang)))

        offer_key = "ent_transfer_offer" if transfer else "ent_leader_offer"
        await channel.send(
            f"<@{target_id}>",
            embed=_econ_embed(localized(offer_key, lang, mention=f"<@{target_id}>",
                                        name=ent["name"], code=ent["code"])),
            view=_ConsentView(lang, target_id, on_accept, on_decline),
            allowed_mentions=discord.AllowedMentions(users=True))
    elif action == "salary_bank":
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_salary_bank", lang),
                                 as_embed=True)
        if msg is None:
            return
        bank = db.get_bank((msg.content or "").strip().upper())
        if not bank:
            await interaction.channel.send(embed=_econ_embed(localized("bank_not_found", lang)))
            return
        db.update_enterprise_field(ent["code"], "salary_bank", bank["code"])
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action == "salary_period":
        msg = await _dialog_text(interaction, lang, localized("edit_ent_ask_salary_period", lang),
                                 as_embed=True)
        if msg is None:
            return
        period = (msg.content or "").strip().lower()
        if period not in ("weekly", "monthly"):
            await interaction.channel.send(embed=_econ_embed(localized("edit_ent_bad_value", lang)))
            return
        db.update_enterprise_field(ent["code"], "salary_period", period)
        await interaction.channel.send(embed=_econ_embed(localized("edit_ent_done", lang)))
    elif action == "delete":
        msg = await _dialog_text(interaction, lang,
                                 localized("edit_ent_confirm_delete", lang, code=ent["code"]),
                                 as_embed=True)
        if msg is None:
            return
        if (msg.content or "").strip().upper() != ent["code"]:
            await interaction.channel.send(embed=_econ_embed(localized("action_cancelled", lang)))
            return
        db.delete_enterprise(ent["code"])
        await interaction.channel.send(embed=_econ_embed(
            localized("edit_ent_deleted", lang, name=ent["name"])))
        await send_service_event("enterprise_deleted", name=ent["name"], code=ent["code"],
                                 user=str(interaction.user))

@bot.tree.command(name="ent-join", description="ask to join an enterprise (its leaders approve)")
@app_commands.describe(code="Enterprise code or name")
async def ent_join_cmd(interaction: discord.Interaction, code: str):
    """Ask to join an enterprise; a leader approves with the consent
    buttons."""
    lang = get_chat_lang(_chat_key(interaction))
    ent = db.find_enterprise(code)
    if not ent:
        await _ereply(interaction, "enterprise_not_found", lang)
        return
    if db.is_enterprise_worker(ent["code"], "discord", interaction.user.id):
        await _ereply(interaction, "ent_join_already", lang)
        return
    requester_id = interaction.user.id
    requester_name = str(interaction.user)
    channel = interaction.channel

    async def on_accept(interaction2):
        """Hire the applicant now that a leader has agreed, and say so."""
        db.add_enterprise_member(ent["code"], "discord", requester_id, requester_name)
        await channel.send(embed=_econ_embed(
            localized("ent_join_approved", lang, user=f"<@{requester_id}>", name=ent["name"])))

    async def on_decline(interaction2):
        """Say the application was turned down."""
        await channel.send(embed=_econ_embed(localized("ent_join_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("ent_join_request", lang, user=f"<@{requester_id}>",
                  name=ent["name"], code=ent["code"])),
        view=_EntConsentView(lang, ent["code"], on_accept, on_decline))

@bot.tree.command(name="ent-leave", description="leave an enterprise you work at")
@app_commands.describe(code="Enterprise code (when you work at several)")
async def ent_leave_cmd(interaction: discord.Interaction, code: str = None):
    """Leave an enterprise you work at.

    Your personal salary override goes with you, so being hired again later starts
    from the position's salary rather than from a figure agreed months ago."""
    lang = get_chat_lang(_chat_key(interaction))
    ents = db.user_member_enterprises("discord", interaction.user.id)
    if code:
        ents = [e for e in ents if e["code"].lower() == code.strip().lower()
                or e["name"].lower() == code.strip().lower()]
    if not ents:
        await _ereply(interaction, "ent_leave_none", lang)
        return
    ent = ents[0]
    if len(ents) > 1:
        ent = await _numbered_choice(interaction, lang, localized("ent_choose", lang),
                                     ents, _ent_line)
        if ent is None:
            return
    db.remove_enterprise_member(ent["code"], "discord", interaction.user.id)
    await _ereply(interaction, "ent_leave_done", lang, ephemeral=False, name=ent["name"])

@bot.tree.command(name="ent-kick", description="remove a worker from your enterprise (leaders)")
@app_commands.describe(user="Worker ID or mention", enterprise="Enterprise code (when you lead several)")
async def ent_kick_cmd(interaction: discord.Interaction, user: str, enterprise: str = None):
    """Dismiss a worker from your enterprise (leaders)."""
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    if not db.remove_enterprise_member(ent["code"], "discord", target_id):
        await _ereply(interaction, "ent_kick_not_member", lang)
        return
    await _ereply(interaction, "ent_kick_done", lang, ephemeral=False,
                  user=f"<@{target_id}>", name=ent["name"])

def _salary_display(kind, value, lang):
    """Render a salary the way it was set — an amount in the enterprise's
    currency, or a percentage of period sales."""
    if kind == "percent":
        return localized("salary_percent", lang, percent=f"{value:g}")
    return economy.format_amount(value)

@bot.tree.command(name="ent-position", description="create a position and its salary in your enterprise (leaders)")
@app_commands.describe(position="Position name", salary="Amount (12.34), percent of sales (5%), or 0 to remove",
                       enterprise="Enterprise code (when you lead several)")
async def ent_position_cmd(interaction: discord.Interaction, position: str, salary: str,
                           enterprise: str = None):
    """Create or price a position in your enterprise (leaders).

    `salary` is an exact amount, a percentage of the period's sales ('5%'), or `0`
    to remove the position. A percentage is stored as the number typed, not as a
    fraction."""
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    parsed = economy.parse_salary(salary)
    if parsed is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    kind, value = parsed
    pos = clean_display_name(position, max_len=40)
    if kind == "clear":
        db.set_position_salary(ent["code"], pos, None, None)
        await _ereply(interaction, "ent_position_cleared", lang, ephemeral=False, position=pos)
        return
    db.set_position_salary(ent["code"], pos,
                           value if kind == "amount" else None,
                           value if kind == "percent" else None)
    await _ereply(interaction, "ent_position_done", lang, ephemeral=False, position=pos,
                  salary=_salary_display(kind, value, lang))

@bot.tree.command(name="ent-assign", description="assign a worker to a position (leaders)")
@app_commands.describe(user="Worker ID or mention", position="Position name ('-' clears)",
                       enterprise="Enterprise code (when you lead several)")
async def ent_assign_cmd(interaction: discord.Interaction, user: str, position: str,
                         enterprise: str = None):
    """Assign a worker to a position, or clear it with '-' (leaders).

    A worker with no position and no personal override earns nothing — the payroll
    simply skips them."""
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    if not db.get_enterprise_member(ent["code"], "discord", target_id):
        await _ereply(interaction, "ent_kick_not_member", lang)
        return
    pos = clean_display_name(position, max_len=40)
    if pos == "-":
        db.set_member_position(ent["code"], "discord", target_id, None)
        await _ereply(interaction, "ent_assign_cleared", lang, ephemeral=False,
                      user=f"<@{target_id}>")
        return
    db.set_member_position(ent["code"], "discord", target_id, pos)
    await _ereply(interaction, "ent_assign_done", lang, ephemeral=False,
                  user=f"<@{target_id}>", position=pos)

@bot.tree.command(name="ent-salary", description="set a worker's personal salary (leaders)")
@app_commands.describe(user="Worker ID or mention", salary="Amount (12.34), percent of sales (5%), or 0 to remove",
                       enterprise="Enterprise code (when you lead several)")
async def ent_salary_cmd(interaction: discord.Interaction, user: str, salary: str,
                         enterprise: str = None):
    """Set a worker's personal salary, overriding their position's (leaders).

    Same two shapes as a position salary. `0` removes the override and puts the
    worker back on their position's terms."""
    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    if not db.is_enterprise_worker(ent["code"], "discord", target_id):
        await _ereply(interaction, "ent_kick_not_member", lang)
        return
    parsed = economy.parse_salary(salary)
    if parsed is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    kind, value = parsed
    if kind == "clear":
        db.set_personal_salary(ent["code"], "discord", target_id, None, None)
        await _ereply(interaction, "ent_salary_cleared", lang, ephemeral=False,
                      user=f"<@{target_id}>")
        return
    db.set_personal_salary(ent["code"], "discord", target_id,
                           value if kind == "amount" else None,
                           value if kind == "percent" else None)
    await _ereply(interaction, "ent_salary_done", lang, ephemeral=False,
                  user=f"<@{target_id}>", salary=_salary_display(kind, value, lang))

@bot.tree.command(name="ent-sell", description="sell your enterprise's goods to a member or another enterprise (leaders)")
@app_commands.describe(good_code="Good code",
                       buyer="Buyer: a user ID/mention or an enterprise code",
                       qty="How many (default: all)",
                       price="Total price (default: what the batch is worth)",
                       enterprise="Enterprise code (when you lead several)")
async def ent_sell_cmd(interaction: discord.Interaction, good_code: str, buyer: str,
                       qty: int = None, price: str = None, enterprise: str = None):
    """Sell your enterprise's goods to a member or to another enterprise
    (leaders).

    No bank buys goods, so this is a sale between two holders and the buyer has
    to accept it. The proceeds count into the period sales that percentage
    salaries are taken from, but only when they land in the enterprise's own
    salary currency. Sending stock to another enterprise without a buyer on the
    other end is `/export` instead.

    The two helpers come from commands/goods.py at the call site: a personal
    sale and an enterprise's sale are the same offer with a different seller,
    and importing that module here at module level would drag its commands into
    registration ahead of their place."""
    from discord_bot.commands.goods import _offer_sale, _resolve_buyer

    lang = get_chat_lang(_chat_key(interaction))
    ent = await _resolve_led_enterprise(interaction, lang, enterprise)
    if ent is None:
        return
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    if db.is_base_good(good["code"]):
        await _ereply(interaction, "sell_not_sellable", lang,
                      name=good_display_name(good, lang))
        return
    seller = db.enterprise_owner(ent["code"])
    have = db.get_inventory_qty(seller, good["code"])
    want = qty if (qty and qty > 0) else have
    if want <= 0 or have <= 0:
        await _ereply(interaction, "sell_nothing", lang, name=good["name"])
        return
    want = min(want, have)
    target, buyer_name, consent_target = _resolve_buyer(buyer)
    if target is None:
        await _ereply(interaction, "sell_bad_target", lang)
        return
    if target == seller:
        await _ereply(interaction, "sell_self", lang)
        return
    minor = economy.parse_amount(price) if price else economy.asking_price(seller, good, want)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    bank = db.get_bank(good["bank_code"]) if good["bank_code"] else None
    if minor > 0 and not bank:
        await _ereply(interaction, "sell_no_bank", lang)
        return
    await _offer_sale(interaction, lang, seller, target, good, want, minor, bank,
                      buyer_name, consent_target)

async def _run_export(interaction, lang, source, target, good, qty, price, bank):
    """Offer a one-off export between two enterprises. A leader of the receiving
    enterprise must always accept — free or priced — before the shipment leaves.
    Once accepted, the cargo travels and arrives after its transit time."""
    channel = interaction.channel
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    name = good_display_name(good, lang)
    notify = ("discord", str(interaction.channel_id))
    price_disp = economy.format_money(price, bank) if price else localized("export_free", lang)

    async def on_accept(interaction2):
        """Dispatch the shipment now that the buyer has agreed: the goods
        leave the warehouse and a priced sale escrows the payment."""
        status, info = economy.dispatch_shipment(
            source["code"], target["code"], good, qty, price,
            bank["code"] if bank else None, notify, lang)
        if status == "ok":
            text = localized("export_dispatched", lang, qty=info["qty"], emoji=emoji, name=name,
                             source=_ent_line(source), target=_ent_line(target),
                             eta=_fmt_eta(info["duration"]),
                             distance=_distance_label(info["distance"], lang))
        else:
            text = localized(f"export_{status}", lang)
        await channel.send(embed=_econ_embed(text))

    async def on_decline(interaction2):
        """Say the export was refused. Nothing left the warehouse."""
        await channel.send(embed=_econ_embed(localized("export_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("export_offer", lang, source=_ent_line(source), target=_ent_line(target),
                  qty=qty, emoji=emoji, name=name, price=price_disp)),
        view=_EntConsentView(lang, target["code"], on_accept, on_decline))

def _resolve_export_args(lang, target_query, price_s, currency, source):
    """(target, price, bank, error_key_or_None) shared by /export and /auto-export."""
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

@bot.tree.command(name="export", description="export your enterprise's goods to another enterprise (leaders)")
@app_commands.describe(good_code="Good code", qty="How many units",
                       target="Receiving enterprise code or name",
                       price="Total price (the buyer pays on acceptance)",
                       currency="Currency code of the price",
                       enterprise="Your enterprise code (when you lead several)")
async def export_cmd(interaction: discord.Interaction, good_code: str, qty: int, target: str,
                     price: str = None, currency: str = None, enterprise: str = None):
    """Export goods to another enterprise, once (leaders).

    The receiving side must accept. A priced sale escrows the buyer's payment on
    dispatch — no funds, no dispatch — and perishable categories lose part of the
    cargo in transit."""
    lang = get_chat_lang(_chat_key(interaction))
    source = await _resolve_led_enterprise(interaction, lang, enterprise)
    if source is None:
        return
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    if qty <= 0:
        await _ereply(interaction, "bad_amount", lang)
        return
    tgt, price_minor, bank, err = _resolve_export_args(lang, target, price, currency, source)
    if err:
        await _ereply(interaction, err, lang)
        return
    await _run_export(interaction, lang, source, tgt, good, qty, price_minor, bank)

@bot.tree.command(name="auto-export", description="export goods to another enterprise every week (leaders)")
@app_commands.describe(good_code="Good code", qty="Units per week, or 'off' to cancel",
                       target="Receiving enterprise code or name",
                       price="Total price per delivery", currency="Currency code of the price",
                       enterprise="Your enterprise code (when you lead several)")
async def auto_export_cmd(interaction: discord.Interaction, good_code: str, qty: str, target: str,
                          price: str = None, currency: str = None, enterprise: str = None):
    """Set up (or cancel with 'off') a weekly export contract to another
    enterprise (leaders).

    The receiving side accepts the standing arrangement once. Each week's delivery
    is an ordinary shipment; a week the seller has no stock or the buyer no funds
    is silently skipped."""
    lang = get_chat_lang(_chat_key(interaction))
    source = await _resolve_led_enterprise(interaction, lang, enterprise)
    if source is None:
        return
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    tgt = db.find_enterprise(target)
    if not tgt:
        await _ereply(interaction, "export_bad_target", lang)
        return
    if qty.strip().lower() in ("off", "0", "stop"):
        db.remove_auto_export(source["code"], tgt["code"], good["code"])
        await _ereply(interaction, "auto_export_removed", lang, ephemeral=False,
                      name=good_display_name(good, lang), target=_ent_line(tgt))
        return
    if not qty.strip().isdigit() or int(qty) <= 0:
        await _ereply(interaction, "bad_amount", lang)
        return
    qty_n = int(qty)
    tgt, price_minor, bank, err = _resolve_export_args(lang, target, price, currency, source)
    if err:
        await _ereply(interaction, err, lang)
        return
    channel = interaction.channel
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    name = good_display_name(good, lang)
    price_disp = economy.format_money(price_minor, bank) if price_minor \
        else localized("export_free", lang)

    async def on_accept(interaction2):
        """Store the weekly contract now that the buyer has agreed to it."""
        db.remove_auto_export(source["code"], tgt["code"], good["code"])
        db.add_auto_export(source["code"], tgt["code"], good["code"], qty_n, price_minor,
                           bank["code"] if bank else None, interaction.user.id)
        await channel.send(embed=_econ_embed(
            localized("auto_export_set", lang, qty=qty_n, emoji=emoji, name=name,
                      target=_ent_line(tgt), price=price_disp)))

    async def on_decline(interaction2):
        """Say the standing offer was refused; no contract was stored."""
        await channel.send(embed=_econ_embed(localized("export_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("auto_export_offer", lang, source=_ent_line(source), target=_ent_line(tgt),
                  qty=qty_n, emoji=emoji, name=name, price=price_disp)),
        view=_EntConsentView(lang, tgt["code"], on_accept, on_decline))

@bot.tree.command(name="transit", description="an enterprise's goods in transit")
@app_commands.describe(enterprise="Enterprise code or name (omit for the one you lead)")
async def transit_cmd(interaction: discord.Interaction, enterprise: str = None):
    """List an enterprise's cargo still on the way, with arrival times."""
    lang = get_chat_lang(_chat_key(interaction))
    if enterprise:
        ent = db.find_enterprise(enterprise)
        if not ent:
            await _ereply(interaction, "enterprise_not_found", lang)
            return
    else:
        ent = await _resolve_led_enterprise(interaction, lang)
        if ent is None:
            return
    shipments = db.get_enterprise_shipments(ent["code"])
    if not shipments:
        await _ereply(interaction, "transit_empty", lang, name=ent["name"])
        return
    embed = discord.Embed(
        title=localized("transit_title", lang, name=ent["name"], code=ent["code"]),
        description="\n".join(_transit_lines(ent["code"], shipments, lang))[:4000],
        color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed)
