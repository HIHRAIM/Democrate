"""Banks and the money in them: founding a bank, its card and leadership, the
`/bank edit` menu, accounts, payments, earning channels and fines.

A bank is created through a dialog because it needs several answers and a
central server that must itself be set up; the creator becomes its first
leader. Leadership is always consented to — `_LeaderConsentView` — except when
a Bot Admin appoints directly.

Amounts in every command here are parsed and formatted by economy/money.py and
are integers in minor units the whole way through. `/bank fine` is the one command
that may take an account below zero: a negative balance is the holder's debt to
the bank, and incoming money pays it down by ordinary addition.

Not this module's zone: treaties, rates, wages and dues (commands/trade.py),
goods (commands/goods.py), and the money primitives themselves (db/banks.py).
"""
import discord
from discord import app_commands

import db
import economy
import stats
from message_relay import clean_display_name
from utils import (
    DEFAULT_EMBED_COLOR, LANG_ORDER, format_stored_user, get_chat_lang, is_admin,
    language_name, localized, parse_emoji, send_service_event,
)

from discord_bot.client import (
    _chat_key, _earn_keys, bot, chat_display_name, is_server_admin,
)
from discord_bot.dialogs import (
    _ConsentView, _LinesPager, _dialog_text, _econ_embed, _ereply,
    _numbered_choice, _parse_user_ref, _say,
)
from discord_bot.groups import bank_group

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

@bank_group.command(name="add", description="found a bank and its currency (bot/server admins)")
async def create_bank_cmd(interaction: discord.Interaction):
    """Found a bank and its currency through a dialog (Bot/Server Admins).

    Anchors the bank to a central server, which must itself be `/server setup`-bound —
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
    emoji = parse_emoji(emoji_msg.content)

    db.create_bank(code, union, chat["chat_id"], currency_name, emoji,
                   "discord", interaction.user.id, str(interaction.user))
    await interaction.channel.send(embed=_econ_embed(
        localized("create_bank_created", lang, name=currency_name, code=code,
                  emoji=emoji, union=db.get_union_name(union, lang))))
    await send_service_event("bank_created", name=currency_name, code=code, union=union,
                             user=str(interaction.user))

def _build_bank_embed(bank, lang):
    """The bank card: currency, central server, leaders, published value and
    its day-over-day change, money supply, outstanding debt, account count
    and the parts of the economy switched off for the currency.

    The central server is named, not numbered: `chat_display_name` asks Discord
    for the server's own name and falls back on what `/server setup` remembered, so a
    card reads as a place rather than as an id."""
    emoji = f" {bank['emoji']}" if bank["emoji"] else ""
    embed = discord.Embed(title=f"{bank['currency_name']} [{bank['code']}]{emoji}",
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    embed.add_field(name=localized("bank_field_union", lang),
                    value=db.get_union_name(bank["union_code"], lang), inline=True)
    embed.add_field(name=localized("bank_field_central", lang),
                    value=chat_display_name(bank["central_chat"]), inline=True)
    leaders = db.get_bank_leaders(bank["code"])
    embed.add_field(
        name=localized("bank_field_leaders", lang),
        value=", ".join(format_stored_user("discord", l["platform"], l["user_id"],
                                           l["display_name"]) for l in leaders) or "—",
        inline=False)
    embed.add_field(name=localized("bank_field_supply", lang),
                    value=economy.format_money(db.bank_money_supply(bank["code"]), bank, lang),
                    inline=True)
    embed.add_field(name=localized("bank_field_accounts", lang),
                    value=str(db.bank_account_count(bank["code"])), inline=True)
    debt = db.bank_debt(bank["code"])
    if debt:
        embed.add_field(name=localized("bank_field_debt", lang),
                        value=economy.format_money(debt, bank, lang), inline=True)
    embed.add_field(name=localized("bank_field_value", lang),
                    value=f"{bank['value']:.4f}", inline=True)
    if bank["tax_percent"]:
        embed.add_field(name=localized("bank_field_tax", lang),
                        value=economy.format_percent(bank["tax_percent"]), inline=True)
    off = economy.parts_text(economy.bank_off(bank["code"]), lang)
    if off:
        embed.add_field(name=localized("bank_field_econ_off", lang), value=off, inline=False)
    return embed

@bank_group.command(name="info", description="information about a bank and its currency")
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
        economy.record_audit(bank["code"], "leader_transfer" if transfer else "leader_added",
                             user=display)
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
    """Shared body of `/bank leader` and `/bank transfer`.

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
        economy.record_audit(bank["code"], "leader_transfer" if transfer else "leader_added",
                             user=display)
        await _ereply(interaction, key, lang, ephemeral=False,
                      user=f"<@{target_id}>", name=bank["currency_name"])
        return
    await _offer_bank_leadership(interaction, bank, lang, target_id, transfer)

@bank_group.command(name="leader", description="add a co-leader to a bank (bank leaders / bot admins)")
@app_commands.describe(code="Currency code", user="User ID or mention")
async def bank_add_leader_cmd(interaction: discord.Interaction, code: str, user: str):
    """Offer somebody co-leadership of a bank (bank leaders / Bot Admins)."""
    await _bank_leadership_cmd(interaction, code, user, transfer=False)

@bank_group.command(name="transfer", description="transfer bank leadership (bank leaders / bot admins)")
@app_commands.describe(code="Currency code", user="User ID or mention")
async def bank_transfer_cmd(interaction: discord.Interaction, code: str, user: str):
    """Offer somebody the whole bank (bank leaders / Bot Admins). On
    acceptance they become its only leader."""
    await _bank_leadership_cmd(interaction, code, user, transfer=True)

_EDIT_BANK_OPTIONS = ("name", "emoji", "code", "central", "fee", "add_leader",
                      "transfer", "delete", "tax", "audit", "forms", "econ")

def _forms_lang(value):
    """One of the six language codes, or None so the dialog asks again."""
    code = (value or "").strip().lower()
    return code if code in economy.FORMS else None

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

@bank_group.command(name="edit", description="manage a bank: currency name and its forms, emoji, code, central server, leaders")
@app_commands.describe(option="Menu item number",
                       code="Currency code (bot admins, or when you lead several banks)")
async def edit_bank_cmd(interaction: discord.Interaction, option: int = None,
                        code: str = None):
    """Manage a bank through a numbered menu: currency name, emoji, code,
    central server, conversion fees, leaders, deletion, bank tax, audit
    channel, the forms of the currency's name in each language
    (economy/names.py), asked as one comma-separated answer per language,
    and the parts of the economy switched off for the currency everywhere
    (economy/switches.py), toggled one at a time by number.

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
        db.update_bank_field(bank["code"], "emoji", parse_emoji(msg.content))
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
    elif action == "fee":
        msg = await _dialog_text(
            interaction, lang,
            localized("edit_bank_ask_fee", lang,
                      current=economy.convert_fee_summary(bank["code"], lang)),
            as_embed=True)
        if msg is None:
            return
        parsed = economy.parse_convert_fee_input(msg.content)
        if parsed is None:
            await channel.send(embed=_econ_embed(localized("edit_bank_bad_fee", lang)))
            return
        target, fee = parsed
        if target != db.CONVERT_FEE_DEFAULT_KEY and not db.get_bank(target):
            await channel.send(embed=_econ_embed(localized("bank_not_found", lang)))
            return
        db.set_convert_fee(bank["code"], target, fee)
        summary = economy.convert_fee_summary(bank["code"], lang)
        await channel.send(embed=_econ_embed(localized("edit_bank_fee_set", lang, current=summary)))
        economy.record_audit(bank["code"], "fee", current=summary, by=str(interaction.user))
    elif action == "tax":
        rate = await _dialog_text(
            interaction, lang,
            localized("edit_bank_ask_tax", lang,
                      current=economy.format_percent(bank["tax_percent"] or 0),
                      max=economy.format_percent(economy.MAX_TAX_PERCENT)),
            validator=economy.parse_percent, error_key="tax_bad_percent", as_embed=True)
        if rate is None:
            return
        db.set_bank_tax(bank["code"], rate)
        await channel.send(embed=_econ_embed(localized(
            "edit_bank_tax_set", lang, percent=economy.format_percent(rate))))
        economy.record_audit(bank["code"], "tax", percent=economy.format_percent(rate),
                             by=str(interaction.user))
    elif action == "econ":
        off = economy.bank_off(bank["code"])
        label = f"{bank['currency_name']} [{bank['code']}]"
        part = await _numbered_choice(
            interaction, lang, localized("econ_menu_bank", lang, bank=label),
            list(economy.ECON_BANK_PARTS), lambda p: economy.part_line(p, off, lang))
        if part is None:
            return
        economy.set_bank_part(bank["code"], part, part not in off)
        await channel.send(embed=_econ_embed(
            economy.switched_text(part, part not in off, lang, bank=label)))
        economy.record_audit(bank["code"], "econ_on" if part in off else "econ_off",
                             part=part, by=str(interaction.user))
    elif action == "forms":
        filled = ", ".join(f"{localized('lang_flag', l)} {l}"
                           for l in db.currency_form_langs(bank["code"])) or "—"
        target = await _dialog_text(
            interaction, lang,
            localized("edit_bank_ask_forms_lang", lang, current=filled,
                      supported=", ".join(LANG_ORDER)),
            validator=_forms_lang, error_key="edit_bank_bad_forms_lang", as_embed=True)
        if target is None:
            return
        forms = await _dialog_text(
            interaction, lang,
            localized("edit_bank_ask_forms", lang, name=bank["currency_name"],
                      language=language_name(target),
                      order=localized(f"currency_forms_order_{target}", lang),
                      example=localized(f"currency_forms_example_{target}", lang)),
            validator=lambda value: economy.parse_currency_forms(target, value),
            error_key="edit_bank_bad_forms", as_embed=True)
        if forms is None:
            return
        db.set_currency_forms(bank["code"], target, forms)
        if forms:
            await channel.send(embed=_econ_embed(localized(
                "edit_bank_forms_set", lang, language=language_name(target),
                sample=economy.forms_sample(bank, target))))
        else:
            await channel.send(embed=_econ_embed(localized(
                "edit_bank_forms_cleared", lang, language=language_name(target))))
    elif action == "audit":
        msg = await _dialog_text(interaction, lang, localized("edit_bank_ask_audit", lang),
                                 as_embed=True)
        if msg is None:
            return
        if (msg.content or "").strip().lower() in ("off", "-", "none"):
            db.set_bank_audit(bank["code"], None, None)
            await channel.send(embed=_econ_embed(localized("edit_bank_audit_off", lang)))
            return
        if interaction.guild_id is None:
            await channel.send(embed=_econ_embed(localized("guild_only", lang)))
            return
        db.set_bank_audit(bank["code"], "discord", f"{interaction.guild_id}:{interaction.channel_id}")
        await channel.send(embed=_econ_embed(localized("edit_bank_audit_set", lang)))
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

@bank_group.command(name="open", description="open an account in a bank")
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
                  name=economy.currency_name(bank), code=bank["code"])

@bank_group.command(name="balance", description="your balance and energy")
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
        rank = db.balance_rank(b["code"], owner)
        if rank:
            line += "\n" + localized("balance_rank", lang, place=rank[0], total=rank[1])
        blocks.append(f"**{b['currency_name']} [{b['code']}]**\n{line}")
    embed = discord.Embed(title=localized("balance_title", lang),
                          description="\n\n".join(blocks)[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed, ephemeral=True)

@bank_group.command(name="pay", description="send money to another user")
@app_commands.describe(user="Recipient ID or mention", amount="Amount, e.g. 12.34", code="Currency code")
async def pay_cmd(interaction: discord.Interaction, user: str, amount: str, code: str):
    """Send money to another user in one currency.

    Both sides must hold an account; the amount is parsed into minor units and the
    transfer is atomic and journalled. Refused when the balance does not cover it —
    `/bank pay` never puts anyone into debt, unlike `/bank fine`."""
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
    server = ("discord", str(interaction.guild_id)) if interaction.guild_id else None
    taxes = economy.withhold(bank["code"], target, minor, server)
    text = localized("pay_done", lang, amount=economy.format_money(minor, bank),
                     user=f"<@{target_id}>")
    extra = economy.taxes_line(taxes, bank, lang)
    await _say(interaction, embed=_econ_embed(f"{text}\n{extra}" if extra else text))

@bank_group.command(name="earn", description="make this channel earn a bank's currency (bank leaders / server admins)")
@app_commands.describe(code="Currency code", action="on | off", rate="Earning multiplier (default 1.0)")
async def set_earn_cmd(interaction: discord.Interaction, code: str, action: str, rate: float = 1.0):
    """Make this channel earn a bank's currency, or stop it (bank leaders /
    Server Admins).

    The chat must belong to the bank's union. `rate` multiplies the currency
    reward only — energy is never scaled — and one channel earns exactly one
    currency, which is how several banks coexist on a server. A multiplier
    above 1× prints more of the currency, so it is set by the currency's own
    people: its leaders, Bot Admins, and the Server Admins of the bank's
    central server. A Server Admin elsewhere binds channels at up to 1×, so
    that no community can print another community's currency.

    A binding covers what is inside the channel: threads and forum posts under
    it earn the same currency unless one of them names its own. That is also why
    switching it off inside a thread that never had a binding of its own says so
    rather than pretending to have done something — the currency it earns comes
    from the channel around it."""
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
        removed = db.remove_earn_channel("discord", interaction.channel_id)
        if not removed and db.resolve_earn_channel("discord", _earn_keys(interaction.channel)):
            await _ereply(interaction, "set_earn_off_inherited", lang)
            return
        await _ereply(interaction, "set_earn_off", lang)
        return
    if action not in ("on", "enable", "1", "true", "yes"):
        await _ereply(interaction, "set_earn_usage", lang)
        return
    r = max(0.0, min(float(rate), 100.0))
    home = (bank["central_chat"] or "").split(":", 1)[0] == str(interaction.guild_id)
    if r > 1.0 and not (db.is_bank_leader(bank["code"], "discord", interaction.user.id)
                        or is_admin("discord", interaction.user.id)
                        or (home and is_server_admin(interaction))):
        await _ereply(interaction, "set_earn_rate_leaders_only", lang)
        return
    db.set_earn_channel("discord", interaction.channel_id, bank["code"], r,
                        set_by=("discord", interaction.user.id, str(interaction.user)))
    await _ereply(interaction, "set_earn_on", lang, code=bank["code"], rate=f"{r:g}")

@bank_group.command(name="fine", description="fine a user in your bank's currency (bank leaders)")
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
    shown_reason = reason.strip()[:200] if reason else localized("fine_no_reason", lang)
    await _ereply(interaction, "fine_done", lang, ephemeral=False, user=f"<@{tid}>",
                  amount=economy.format_money(minor, bank), reason=shown_reason)
    try:
        fined = str(await bot.fetch_user(tid))
    except Exception:
        fined = str(tid)
    economy.record_audit(bank["code"], "fine", user=fined, amount=economy.format_money(minor, bank),
                         reason=shown_reason, by=str(interaction.user))

def _emit_target(raw, union_code, lang):
    """The owner `/bank emit` pays and how to name it: a user (mention or id), or
    a party or enterprise of the bank's union by code. Returns (owner, label,
    display name) or (None, error key, None)."""
    uid = _parse_user_ref(raw)
    if uid is not None:
        return db.canonical_user("discord", uid), f"<@{uid}>", None
    code = (raw or "").strip().upper()
    party = db.get_party(code)
    if party:
        if party["union_code"] != union_code:
            return None, "emit_wrong_union", None
        return db.party_owner(code), f"{party['name']} [{code}]", party["name"]
    ent = db.get_enterprise(code)
    if ent:
        chat = db.get_chat(ent["server_id"])
        if not chat or chat["union_code"] != union_code:
            return None, "emit_wrong_union", None
        return db.enterprise_owner(code), f"{ent['name']} [{code}]", ent["name"]
    return None, "emit_bad_target", None

@bank_group.command(name="emit", description=localized("slash_emit", "en"))
@app_commands.describe(target=localized("slash_emit_target", "en"),
                       amount=localized("slash_emit_amount", "en"),
                       code=localized("slash_emit_code", "en"),
                       reason=localized("slash_emit_reason", "en"))
async def emit_cmd(interaction: discord.Interaction, target: str, amount: str, code: str,
                   reason: str = None):
    """Issue a bank's currency to a person, a party or an enterprise (bank
    leaders, within the month's limit; Bot Admins without one)."""
    lang = get_chat_lang(_chat_key(interaction))
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    admin = is_admin("discord", interaction.user.id)
    if not (admin or db.is_bank_leader(bank["code"], "discord", interaction.user.id)):
        await _ereply(interaction, "bank_not_leader", lang)
        return
    minor = economy.parse_amount(amount)
    if not minor:
        await _ereply(interaction, "bad_amount", lang)
        return
    owner, label, display = _emit_target(target, bank["union_code"], lang)
    if owner is None:
        await _ereply(interaction, label, lang)
        return
    if owner[0] == "user" and display is None:
        try:
            display = str(await bot.fetch_user(int(owner[2])))
        except Exception:
            display = None
    status, info = economy.emit(bank["code"], owner, minor, reason, capped=not admin,
                                display_name=display)
    if status == "over_cap":
        await _ereply(interaction, "emit_over_cap", lang,
                      left=economy.format_money(info["left"], bank))
        return
    shown_reason = (reason or "").strip()[:200] or localized("fine_no_reason", lang)
    await _ereply(interaction, "emit_done", lang, ephemeral=False, target=label,
                  amount=economy.format_money(minor, bank), reason=shown_reason)
    economy.record_audit(bank["code"], "emit", target=display or label,
                         amount=economy.format_money(minor, bank), reason=shown_reason,
                         by=str(interaction.user))

@bank_group.command(name="transactions", description=localized("slash_transactions", "en"))
@app_commands.describe(code=localized("slash_transactions_code", "en"))
async def transactions_cmd(interaction: discord.Interaction, code: str = None):
    """Show the caller's own transaction history, newest first, in every
    currency or one (only the caller sees it)."""
    lang = get_chat_lang(_chat_key(interaction))
    bank_code = code.strip().upper() if code else None
    if bank_code and not db.get_bank(bank_code):
        await _ereply(interaction, "bank_not_found", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    lines = stats.transaction_lines(owner, "discord", lang, bank_code)
    if not lines:
        await _ereply(interaction, "transactions_none", lang)
        return
    pager = _LinesPager(localized("transactions_title", lang), lines, lang)
    extra = {"view": pager} if pager.pages > 1 else {}
    await interaction.response.send_message(embed=pager.embed(), ephemeral=True, **extra)

@bank_group.command(name="earn-info", description=localized("slash_earn_info", "en"))
@app_commands.describe(code=localized("slash_earn_info_code", "en"))
async def earn_info_cmd(interaction: discord.Interaction, code: str = None):
    """List the channels where currency is earned, with the details of each
    (stats.earn_channel_lines) — only the caller sees it.

    Bank leaders see every channel of the currencies they lead, wherever it
    is; Server Admins the channels of this server, whatever they earn; Bot
    Admins every channel. `code` narrows the list to one currency."""
    lang = get_chat_lang(_chat_key(interaction))
    uid = interaction.user.id
    bank_code = code.strip().upper() if code else None
    if bank_code and not db.get_bank(bank_code):
        await _ereply(interaction, "bank_not_found", lang)
        return
    rows = {}
    allowed = is_admin("discord", uid)
    if allowed:
        rows = {(r["platform"], r["chan_key"]): r for r in db.earn_channels_where(bank_code)}
    else:
        for bank in db.user_led_banks("discord", uid):
            if bank_code in (None, bank["code"]):
                allowed = True
                rows.update({(r["platform"], r["chan_key"]): r
                             for r in db.earn_channels_where(bank["code"])})
        if interaction.guild is not None and is_server_admin(interaction):
            allowed = True
            for r in db.earn_channels_where(bank_code, platform="discord"):
                channel = bot.get_channel(int(r["chan_key"])) if r["chan_key"].isdigit() else None
                if channel is not None and getattr(channel, "guild", None) is not None \
                        and channel.guild.id == interaction.guild_id:
                    rows[(r["platform"], r["chan_key"])] = r
    if not allowed:
        await _ereply(interaction, "no_permission", lang)
        return
    entries = stats.earn_channel_lines(list(rows.values()), lang)
    pager = _LinesPager(localized("earn_info_title", lang),
                        [discord.utils.escape_markdown(e) for e in entries], lang,
                        empty=localized("earn_info_empty", lang))
    extra = {"view": pager} if pager.pages > 1 else {}
    await interaction.response.send_message(embed=pager.embed(), ephemeral=True, **extra)
