"""Banks and the money in them: founding a bank, its card and leadership, the
`/edit-bank` menu, accounts, payments, earning channels and fines.

A bank is created through a dialog because it needs several answers and a
central server that must itself be set up; the creator becomes its first
leader. Leadership is always consented to — `_LeaderConsentView` — except when
a Bot Admin appoints directly.

Amounts in every command here are parsed and formatted by economy/money.py and
are integers in minor units the whole way through. `/fine` is the one command
that may take an account below zero: a negative balance is the holder's debt to
the bank, and incoming money pays it down by ordinary addition.

Not this module's zone: treaties, rates, wages and dues (commands/trade.py),
goods (commands/goods.py), and the money primitives themselves (db/banks.py).
"""
import discord
from discord import app_commands

import db
import economy
from message_relay import clean_display_name
from utils import (
    DEFAULT_EMBED_COLOR, format_stored_user, get_chat_lang, is_admin,
    localized, send_service_event,
)

from discord_bot.client import _chat_key, bot, is_server_admin
from discord_bot.dialogs import (
    _ConsentView, _dialog_text, _econ_embed, _ereply,
    _numbered_choice, _parse_user_ref, _say,
)

def _currency_code_validator(value):
    """Accept a 4-character currency code only when it is free across the
    whole shared namespace. Returns it upper-cased, or None so the dialog
    re-asks."""
    code = value.strip().upper()
    if economy.CURRENCY_CODE_RE.match(code) and not db.code_taken(code):
        return code
    return None

def _bank_line(bank, lang):
    """One line naming a bank in a list — 'Currency [CODE] emoji'."""
    emoji = f" {bank['emoji']}" if bank["emoji"] else ""
    return f"{bank['currency_name']} [{bank['code']}]{emoji}"

async def _resolve_led_bank(interaction, lang):
    """The bank the caller leads; asks which one when they lead several. Bank
    leadership is global, so this is not scoped to the current server's union."""
    banks = db.user_led_banks("discord", interaction.user.id)
    if not banks:
        await _ereply(interaction, "bank_none_led", lang)
        return None
    if len(banks) == 1:
        return banks[0]
    return await _numbered_choice(interaction, lang, localized("bank_choose", lang),
                                  banks, lambda b: _bank_line(b, lang))

@bot.tree.command(name="create-bank", description="found a bank and its currency (bot/server admins)")
async def create_bank_cmd(interaction: discord.Interaction):
    """Found a bank and its currency through a dialog (Bot/Server Admins).

    Anchors the bank to a central server, which must itself be `/setup`-bound —
    that is what gives the bank its union. The creator becomes its first leader,
    without a consent step, because they asked for it."""
    lang = get_chat_lang(_chat_key(interaction))
    if not (is_admin("discord", interaction.user.id) or is_server_admin(interaction)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    id_msg = await _dialog_text(interaction, lang, localized("create_bank_ask_chat", lang),
                                as_embed=True)
    if id_msg is None:
        return
    raw = (id_msg.content or "").strip().lstrip("-")
    if not raw.isdigit():
        await interaction.channel.send(embed=_econ_embed(localized("create_bank_bad_chat", lang)))
        return
    chat = db.get_chat((id_msg.content or "").strip())
    if not chat:
        await interaction.channel.send(embed=_econ_embed(localized("create_bank_chat_not_setup", lang)))
        return
    union = chat["union_code"]

    name_msg = await _dialog_text(interaction, lang, localized("create_bank_ask_name", lang),
                                  as_embed=True)
    if name_msg is None:
        return
    currency_name = clean_display_name(name_msg.content, max_len=40)

    code = await _dialog_text(interaction, lang, localized("create_bank_ask_code", lang),
                              validator=_currency_code_validator, error_key="create_bank_bad_code",
                              as_embed=True)
    if code is None:
        return

    emoji_msg = await _dialog_text(interaction, lang, localized("create_bank_ask_emoji", lang),
                                   as_embed=True)
    if emoji_msg is None:
        return
    parts = (emoji_msg.content or "").strip().split()
    emoji = parts[0][:16] if parts else ""

    db.create_bank(code, union, chat["chat_id"], currency_name, emoji,
                   "discord", interaction.user.id, str(interaction.user))
    await interaction.channel.send(embed=_econ_embed(
        localized("create_bank_created", lang, name=currency_name, code=code,
                  emoji=emoji, union=db.get_union_name(union, lang))))
    await send_service_event("bank_created", name=currency_name, code=code, union=union,
                             user=str(interaction.user))

def _build_bank_embed(bank, lang):
    """The bank card: currency, central server, leaders, published value and
    its day-over-day change, money supply, outstanding debt and account count."""
    emoji = f" {bank['emoji']}" if bank["emoji"] else ""
    embed = discord.Embed(title=f"{bank['currency_name']} [{bank['code']}]{emoji}",
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    embed.add_field(name=localized("bank_field_union", lang),
                    value=db.get_union_name(bank["union_code"], lang), inline=True)
    embed.add_field(name=localized("bank_field_central", lang),
                    value=str(bank["central_chat"]), inline=True)
    leaders = db.get_bank_leaders(bank["code"])
    embed.add_field(
        name=localized("bank_field_leaders", lang),
        value=", ".join(format_stored_user("discord", l["platform"], l["user_id"],
                                           l["display_name"]) for l in leaders) or "—",
        inline=False)
    embed.add_field(name=localized("bank_field_supply", lang),
                    value=economy.format_money(db.bank_money_supply(bank["code"]), bank),
                    inline=True)
    embed.add_field(name=localized("bank_field_accounts", lang),
                    value=str(db.bank_account_count(bank["code"])), inline=True)
    debt = db.bank_debt(bank["code"])
    if debt:
        embed.add_field(name=localized("bank_field_debt", lang),
                        value=economy.format_money(debt, bank), inline=True)
    embed.add_field(name=localized("bank_field_value", lang),
                    value=f"{bank['value']:.4f}", inline=True)
    return embed

@bot.tree.command(name="bank", description="information about a bank and its currency")
@app_commands.describe(code="Currency code (4 characters)")
async def bank_cmd(interaction: discord.Interaction, code: str):
    """Show a bank and its currency by code."""
    lang = get_chat_lang(_chat_key(interaction))
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    await _say(interaction, embed=_build_bank_embed(bank, lang))

async def _offer_bank_leadership(interaction, bank, lang, target_id, transfer):
    """Put a leadership offer to somebody with consent buttons.

    Nothing is written until they accept. `transfer=True` hands the bank over —
    the target becomes its only leader — against simply adding a co-leader."""
    channel = interaction.channel
    name = bank["currency_name"]

    async def on_accept(interaction2):
        """Write the grant and announce it — adding a co-leader, or handing the
        bank over when this was a transfer."""
        display = str(interaction2.user)
        if transfer:
            db.set_bank_leader(bank["code"], "discord", target_id, display)
            text = localized("bank_transfer_done", lang, user=f"<@{target_id}>", name=name)
        else:
            db.add_bank_leader(bank["code"], "discord", target_id, display)
            text = localized("bank_leader_added", lang, user=f"<@{target_id}>", name=name)
        await channel.send(embed=_econ_embed(text))

    async def on_decline(interaction2):
        """Say the offer was turned down. Nothing was written."""
        key = "bank_transfer_declined" if transfer else "bank_leader_declined"
        await channel.send(embed=_econ_embed(localized(key, lang)))

    offer_key = "bank_transfer_offer" if transfer else "bank_leader_offer"
    await _say(interaction, f"<@{target_id}>", embed=_econ_embed(
        localized(offer_key, lang, mention=f"<@{target_id}>", name=name, code=bank["code"])),
        view=_ConsentView(lang, target_id, on_accept, on_decline),
        allowed_mentions=discord.AllowedMentions(users=True))

async def _bank_leadership_cmd(interaction, code, user, transfer):
    """Shared body of `/bank-add-leader` and `/bank-transfer`.

    A Bot Admin may appoint anywhere; otherwise the caller must lead the bank. The
    target still has to accept."""
    lang = get_chat_lang(_chat_key(interaction))
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    caller_is_admin = is_admin("discord", interaction.user.id)
    if not caller_is_admin and not db.is_bank_leader(bank["code"], "discord", interaction.user.id):
        await _ereply(interaction, "bank_not_leader", lang)
        return
    if caller_is_admin:
        try:
            display = str(await bot.fetch_user(target_id))
        except Exception:
            display = str(target_id)
        if transfer:
            db.set_bank_leader(bank["code"], "discord", target_id, display)
            key = "bank_transfer_done"
        else:
            db.add_bank_leader(bank["code"], "discord", target_id, display)
            key = "bank_leader_added"
        await _ereply(interaction, key, lang, ephemeral=False,
                      user=f"<@{target_id}>", name=bank["currency_name"])
        return
    await _offer_bank_leadership(interaction, bank, lang, target_id, transfer)

@bot.tree.command(name="bank-add-leader", description="add a co-leader to a bank (bank leaders / bot admins)")
@app_commands.describe(code="Currency code", user="User ID or mention")
async def bank_add_leader_cmd(interaction: discord.Interaction, code: str, user: str):
    """Offer somebody co-leadership of a bank (bank leaders / Bot Admins)."""
    await _bank_leadership_cmd(interaction, code, user, transfer=False)

@bot.tree.command(name="bank-transfer", description="transfer bank leadership (bank leaders / bot admins)")
@app_commands.describe(code="Currency code", user="User ID or mention")
async def bank_transfer_cmd(interaction: discord.Interaction, code: str, user: str):
    """Offer somebody the whole bank (bank leaders / Bot Admins). On
    acceptance they become its only leader."""
    await _bank_leadership_cmd(interaction, code, user, transfer=True)

_EDIT_BANK_OPTIONS = ("name", "emoji", "code", "central", "add_leader",
                      "transfer", "delete")

async def _resolve_edit_bank(interaction, lang, query=None):
    """The bank the caller may edit: any of them for a Bot Admin, only one they
    lead otherwise. Without a code it is the single bank they lead, or a
    numbered choice when they lead several."""
    admin = is_admin("discord", interaction.user.id)
    if query:
        bank = db.get_bank(query.strip().upper())
        if not bank:
            await _ereply(interaction, "bank_not_found", lang)
            return None
        if not (admin or db.is_bank_leader(bank["code"], "discord", interaction.user.id)):
            await _ereply(interaction, "edit_bank_no_permission", lang)
            return None
        return bank
    led = [b for b in db.get_all_banks()
           if db.is_bank_leader(b["code"], "discord", interaction.user.id)]
    if not led:
        await _ereply(interaction, "edit_bank_specify" if admin
                      else "edit_bank_no_permission", lang)
        return None
    if len(led) == 1:
        return led[0]
    return await _numbered_choice(interaction, lang,
                                  localized("edit_bank_choose", lang), led,
                                  lambda b: _bank_line(b, lang))

@bot.tree.command(name="edit-bank", description="manage a bank: currency name, emoji, code, central server, leaders")
@app_commands.describe(option="Menu item number",
                       code="Currency code (bot admins, or when you lead several banks)")
async def edit_bank_cmd(interaction: discord.Interaction, option: int = None,
                        code: str = None):
    """Manage a bank through a numbered menu: currency name, emoji, code,
    central server, leaders.

    Renaming the code rewrites every reference in one transaction — it is a
    primary key, not a label. Without an option number the menu is shown
    instead."""
    lang = get_chat_lang(_chat_key(interaction))
    bank = await _resolve_edit_bank(interaction, lang, code)
    if bank is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_BANK_OPTIONS):
        items = [localized(f"edit_bank_opt_{key}", lang) for key in _EDIT_BANK_OPTIONS]
        chosen = await _numbered_choice(
            interaction, lang,
            localized("edit_bank_menu_header", lang, name=bank["currency_name"],
                      code=bank["code"]),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_BANK_OPTIONS[chosen[0]]
    else:
        action = _EDIT_BANK_OPTIONS[option - 1]

    channel = interaction.channel
    if action == "name":
        msg = await _dialog_text(interaction, lang, localized("edit_bank_ask_name", lang),
                                 as_embed=True)
        if msg is None:
            return
        db.update_bank_field(bank["code"], "currency_name",
                             clean_display_name(msg.content, max_len=40))
        await channel.send(embed=_econ_embed(localized("edit_bank_done", lang)))
    elif action == "emoji":
        msg = await _dialog_text(interaction, lang, localized("edit_bank_ask_emoji", lang),
                                 as_embed=True)
        if msg is None:
            return
        parts = (msg.content or "").strip().split()
        db.update_bank_field(bank["code"], "emoji", parts[0][:16] if parts else "")
        await channel.send(embed=_econ_embed(localized("edit_bank_done", lang)))
    elif action == "code":
        new_code = await _dialog_text(interaction, lang, localized("edit_bank_ask_code", lang),
                                      validator=_currency_code_validator,
                                      error_key="edit_bank_bad_code", as_embed=True)
        if new_code is None:
            return
        old_code = bank["code"]
        db.rename_bank_code(old_code, new_code)
        await channel.send(embed=_econ_embed(
            localized("edit_bank_code_changed", lang, old=old_code, code=new_code)))
    elif action == "central":
        msg = await _dialog_text(interaction, lang, localized("edit_bank_ask_central", lang),
                                 as_embed=True)
        if msg is None:
            return
        chat = db.get_chat((msg.content or "").strip())
        if not chat:
            await channel.send(embed=_econ_embed(localized("edit_bank_bad_central", lang)))
            return
        db.set_bank_central_chat(bank["code"], chat["chat_id"], chat["union_code"])
        await channel.send(embed=_econ_embed(localized("edit_bank_done", lang)))
    elif action in ("add_leader", "transfer"):
        msg = await _dialog_text(interaction, lang, localized("edit_bank_ask_leader", lang),
                                 as_embed=True)
        if msg is None:
            return
        target_id = _parse_user_ref((msg.content or "").strip())
        if target_id is None:
            await channel.send(embed=_econ_embed(localized("edit_invalid_user", lang)))
            return
        await _offer_bank_leadership(interaction, bank, lang, target_id,
                                     action == "transfer")
    elif action == "delete":
        msg = await _dialog_text(interaction, lang,
                                 localized("edit_bank_confirm_delete", lang, code=bank["code"]),
                                 as_embed=True)
        if msg is None:
            return
        if (msg.content or "").strip().upper() != bank["code"]:
            await channel.send(embed=_econ_embed(localized("action_cancelled", lang)))
            return
        db.delete_bank(bank["code"])
        await channel.send(embed=_econ_embed(
            localized("edit_bank_deleted", lang, name=bank["currency_name"],
                      code=bank["code"])))
        await send_service_event("bank_deleted", name=bank["currency_name"],
                                 code=bank["code"], user=str(interaction.user))

@bot.tree.command(name="open-account", description="open an account in a bank")
@app_commands.describe(code="Currency code")
async def open_account_cmd(interaction: discord.Interaction, code: str):
    """Open an account in a bank.

    Rarely needed: an account also appears silently on the first message that earns
    in a channel. This exists so that somebody can hold a currency before earning
    any of it."""
    lang = get_chat_lang(_chat_key(interaction))
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    if db.account_exists(bank["code"], *owner):
        await _ereply(interaction, "account_exists", lang, code=bank["code"])
        return
    db.ensure_account(bank["code"], *owner, display_name=str(interaction.user))
    await _ereply(interaction, "account_opened", lang, ephemeral=False,
                  name=bank["currency_name"], code=bank["code"])

@bot.tree.command(name="balance", description="your balance and energy")
@app_commands.describe(code="Currency code (omit for all your accounts)")
async def balance_cmd(interaction: discord.Interaction, code: str = None):
    """Show your balances and energy — in one currency, or in all of them.

    The place people discover an account they never opened, since earning opens one
    without saying so."""
    lang = get_chat_lang(_chat_key(interaction))
    owner = db.canonical_user("discord", interaction.user.id)
    if code:
        bank = db.get_bank(code.strip().upper())
        if not bank:
            await _ereply(interaction, "bank_not_found", lang)
            return
        banks = [bank]
    else:
        banks = [db.get_bank(a["bank_code"]) for a in db.get_owner_accounts(*owner)]
        banks = [b for b in banks if b]
    if not banks:
        await _ereply(interaction, "balance_none", lang)
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
        blocks.append(f"**{b['currency_name']} [{b['code']}]**\n{line}")
    embed = discord.Embed(title=localized("balance_title", lang),
                          description="\n\n".join(blocks)[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="pay", description="send money to another user")
@app_commands.describe(user="Recipient ID or mention", amount="Amount, e.g. 12.34", code="Currency code")
async def pay_cmd(interaction: discord.Interaction, user: str, amount: str, code: str):
    """Send money to another user in one currency.

    Both sides must hold an account; the amount is parsed into minor units and the
    transfer is atomic and journalled. Refused when the balance does not cover it —
    `/pay` never puts anyone into debt, unlike `/fine`."""
    lang = get_chat_lang(_chat_key(interaction))
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    sender = db.canonical_user("discord", interaction.user.id)
    target = db.canonical_user("discord", target_id)
    if target == sender:
        await _ereply(interaction, "pay_self", lang)
        return
    try:
        tdisp = str(await bot.fetch_user(target_id))
    except Exception:
        tdisp = str(target_id)
    if not db.transfer(bank["code"], sender, target, minor, "pay", to_display=tdisp):
        await _ereply(interaction, "pay_insufficient", lang)
        return
    await _ereply(interaction, "pay_done", lang, ephemeral=False,
                  amount=economy.format_money(minor, bank), user=f"<@{target_id}>")

@bot.tree.command(name="set-earn", description="make this channel earn a bank's currency (bank leaders / server admins)")
@app_commands.describe(code="Currency code", action="on | off", rate="Earning multiplier (default 1.0)")
async def set_earn_cmd(interaction: discord.Interaction, code: str, action: str, rate: float = 1.0):
    """Make this channel earn a bank's currency, or stop it (bank leaders /
    Server Admins).

    The chat must belong to the bank's union. `rate` multiplies the currency
    reward only — energy is never scaled — and one channel earns exactly one
    currency, which is how several banks coexist on a server."""
    lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    if not (db.is_bank_leader(bank["code"], "discord", interaction.user.id)
            or is_server_admin(interaction) or is_admin("discord", interaction.user.id)):
        await _ereply(interaction, "set_earn_no_permission", lang)
        return
    chat = db.get_chat(str(interaction.guild_id))
    if not chat or chat["union_code"] != bank["union_code"]:
        await _ereply(interaction, "set_earn_wrong_union", lang)
        return
    action = action.strip().lower()
    if action in ("off", "disable", "0", "false", "no"):
        db.remove_earn_channel("discord", interaction.channel_id)
        await _ereply(interaction, "set_earn_off", lang)
        return
    if action not in ("on", "enable", "1", "true", "yes"):
        await _ereply(interaction, "set_earn_usage", lang)
        return
    r = max(0.0, min(float(rate), 100.0))
    db.set_earn_channel("discord", interaction.channel_id, bank["code"], r)
    await _ereply(interaction, "set_earn_on", lang, code=bank["code"], rate=f"{r:g}")

@bot.tree.command(name="fine", description="fine a user in your bank's currency (bank leaders)")
@app_commands.describe(user="User ID or mention", amount="Amount, e.g. 10.00", reason="Optional reason")
async def fine_cmd(interaction: discord.Interaction, user: str, amount: str, reason: str = None):
    """Debit a user's account in your bank's currency (bank leaders).

    The one command that may take a balance below zero: a negative balance is that
    member's debt to the bank, no interest accrues, and incoming money pays it down
    first by ordinary addition."""
    lang = get_chat_lang(_chat_key(interaction))
    bank = await _resolve_led_bank(interaction, lang)
    if bank is None:
        return
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    tid = _parse_user_ref(user)
    if tid is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    towner = db.canonical_user("discord", tid)
    db.burn(bank["code"], towner, minor, f"fine: {reason}" if reason else "fine", allow_negative=True)
    await _ereply(interaction, "fine_done", lang, ephemeral=False, user=f"<@{tid}>",
                  amount=economy.format_money(minor, bank),
                  reason=(reason.strip()[:200] if reason else localized("fine_no_reason", lang)))
