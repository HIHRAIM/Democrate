"""Goods and what one person does with them: creating a good, crafting it,
the inventory, selling, the standing orders, professions and gifts.

`/craft` starts a timed run and returns immediately — the batch is announced
later by main.py: production_loop in the channel the run was started in, which
is why the command stores a notify key rather than awaiting anything. Energy is
charged up front.

A good belongs to a person or an enterprise; the bank named at creation only
*denominates* it, and is where `/sell` sends it. `/create-good` therefore needs
a bank of the current union, and the union's shared code namespace decides
whether the code it asks for is free.

Not this module's zone: enterprises and their warehouse
(commands/enterprises.py), the production formulas (economy/production.py) and
the rows (db/goods.py).
"""
import discord
from discord import app_commands
from datetime import datetime, timezone

import db
import economy
from message_relay import clean_display_name
from utils import (
    DEFAULT_EMBED_COLOR, category_label, get_chat_lang, good_display_name,
    is_admin, localized,
)

from discord_bot.client import _chat_key, bot
from discord_bot.dialogs import _econ_embed, _ereply, _parse_user_ref, _say

def _resolve_good_bank(chat, currency):
    """The bank a new good is denominated in: the named one (it must belong to
    the chat's union) or the union's only bank. Returns (bank, error_key)."""
    banks = db.get_banks_in_union(chat["union_code"])
    if currency:
        bank = db.get_bank(currency.strip().upper())
        if not bank:
            return None, "bank_not_found"
        if bank["union_code"] != chat["union_code"]:
            return None, "create_good_wrong_union"
        return bank, None
    if len(banks) == 1:
        return banks[0], None
    return None, "create_good_need_bank"

def _normalize_category(raw):
    """(category|None, ok). Base-category key, case-insensitive; '-' clears."""
    s = (raw or "").strip().lower()
    if not s or s == "-":
        return None, True
    return (s, True) if s in db.GOOD_CATEGORIES else (None, False)

@bot.tree.command(name="create-good", description="create a good you or your enterprise will produce")
@app_commands.describe(name="Good name", base_value="Base value in the chosen currency, e.g. 5.00",
                       energy_cost="Energy cost per unit", emoji="Optional emoji",
                       category="Base category the good counts toward",
                       currency="Currency code (required when the union has several banks)",
                       enterprise="Enterprise code, when the good belongs to your enterprise")
async def create_good_cmd(interaction: discord.Interaction, name: str, base_value: str,
                          energy_cost: int, emoji: str = None, category: str = None,
                          currency: str = None, enterprise: str = None):
    """Create a good you or your enterprise will produce.

    The good belongs to a person or an enterprise; the bank only *denominates* it
    and is where `/sell` sends it. A category ties its quality to the server's
    provision. `base_value` is parsed into minor units and `energy_cost` is a plain
    integer."""
    lang = get_chat_lang(_chat_key(interaction))
    chat = db.get_chat(str(interaction.guild_id)) if interaction.guild_id else None
    if not chat:
        await _ereply(interaction, "chat_not_setup", lang)
        return
    bank, err = _resolve_good_bank(chat, currency)
    if err:
        await _ereply(interaction, err, lang)
        return
    cat, ok = _normalize_category(category)
    if not ok:
        await _ereply(interaction, "create_good_bad_category", lang,
                      categories=", ".join(db.GOOD_CATEGORIES))
        return
    owner = db.canonical_user("discord", interaction.user.id)
    if enterprise:
        ent = db.find_enterprise(enterprise)
        if not ent:
            await _ereply(interaction, "enterprise_not_found", lang)
            return
        if not db.is_enterprise_leader(ent["code"], "discord", interaction.user.id):
            await _ereply(interaction, "ent_not_leader", lang)
            return
        owner = db.enterprise_owner(ent["code"])
    base = economy.parse_amount(base_value)
    if base is None or energy_cost <= 0:
        await _ereply(interaction, "create_good_bad", lang)
        return
    nm = clean_display_name(name, max_len=40)
    parts = (emoji or "").strip().split()
    em = parts[0][:16] if parts else ""
    gcode = db.create_good(bank["code"], nm, base, energy_cost, em, owner=owner, category=cat)
    await _ereply(interaction, "create_good_created", lang, ephemeral=False, name=nm, code=gcode,
                  value=economy.format_money(base, bank), energy=economy.format_energy(energy_cost),
                  bank=bank["code"])
    if cat:
        await interaction.channel.send(embed=_econ_embed(
            localized("create_good_category_note", lang, category=category_label(cat, lang))))

def _fmt_duration(seconds):
    """mm:ss for a production wait."""
    seconds = max(int(seconds), 0)
    return f"{seconds // 60}:{seconds % 60:02d}"

def _resolve_production_target(platform, user_id, guild_key, enterprise_query):
    """(owner, server, error_key) of a production request: the caller's own
    inventory and the current server — or the enterprise and its home server
    when one is named (the caller must work there)."""
    if enterprise_query:
        ent = db.find_enterprise(enterprise_query)
        if not ent:
            return None, None, "enterprise_not_found"
        if not db.is_enterprise_worker(ent["code"], platform, user_id):
            return None, None, "ent_not_worker"
        return db.enterprise_owner(ent["code"]), (ent["platform"], ent["server_id"]), None
    owner = db.canonical_user(platform, user_id)
    server = (platform, str(guild_key)) if guild_key else (None, None)
    return owner, server, None

@bot.tree.command(name="craft", description="start producing a good — the batch arrives in a few minutes")
@app_commands.describe(good_code="Good code (4 characters)",
                       enterprise="Enterprise code to produce for (its workers only)")
async def craft_cmd(interaction: discord.Interaction, good_code: str, enterprise: str = None):
    """Start producing a good; the batch arrives in a few minutes.

    Returns immediately — main.py: production_loop announces the batch in this
    channel when it lands. Energy for the whole batch is charged up front, and one
    worker runs one job at a time."""
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    owner, server, err = _resolve_production_target(
        "discord", interaction.user.id, interaction.guild_id, enterprise)
    if err:
        await _ereply(interaction, err, lang)
        return
    worker = db.canonical_user("discord", interaction.user.id)
    notify = ("discord", str(interaction.channel_id))
    status, info = economy.start_production(worker, good, owner, server, notify, lang,
                                            starter_display=str(interaction.user))
    if status == "busy":
        await _ereply(interaction, "production_busy", lang,
                      time=_fmt_duration(info["finish_at"] - int(datetime.now(timezone.utc).timestamp())))
        return
    if status == "no_energy":
        await _ereply(interaction, "craft_no_energy", lang,
                      cost=economy.format_energy(info["cost"]),
                      have=economy.format_energy(info["have"]))
        return
    emoji = f" {good['emoji']}" if good["emoji"] else ""
    await _ereply(interaction, "production_started", lang, ephemeral=False,
                  name=good_display_name(good, lang), emoji=emoji, qty=info["qty"],
                  duration=_fmt_duration(info["duration"]),
                  cost=economy.format_energy(info["cost"]))

@bot.tree.command(name="inventory", description="your crafted goods")
async def inventory_cmd(interaction: discord.Interaction):
    """Show your crafted goods."""
    lang = get_chat_lang(_chat_key(interaction))
    owner = db.canonical_user("discord", interaction.user.id)
    inv = db.get_inventory(owner)
    if not inv:
        await _ereply(interaction, "inventory_empty", lang, ephemeral=False)
        return
    lines = []
    for row in inv:
        m = db.get_produced(owner, row["good_code"])
        good = db.get_good(row["good_code"])
        bank = db.get_bank(row["bank_code"])
        val = economy.format_money(economy.unit_value(good, m), bank) if good and bank else "—"
        emoji = f"{row['emoji']} " if row["emoji"] else ""
        lines.append(localized("inventory_line", lang, emoji=emoji,
                               name=good_display_name(good, lang) if good else row["name"],
                               code=row["good_code"], qty=row["qty"], value=val,
                               level=economy.mastery_level(m)))
    embed = discord.Embed(title=localized("inventory_title", lang),
                          description="\n".join(lines)[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="sell", description="sell a good back to its bank")
@app_commands.describe(good_code="Good code", qty="How many (default: all)")
async def sell_cmd(interaction: discord.Interaction, good_code: str, qty: int = None):
    """Sell a good back to its bank at the current unit value.

    The economy's money source: the bank mints the proceeds. Base goods carry no
    market value and cannot be sold. Omitting `qty` sells everything."""
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    have = db.get_inventory_qty(owner, good["code"])
    want = qty if (qty and qty > 0) else have
    status, info = economy.sell(owner, good, want)
    if status == "not_sellable":
        await _ereply(interaction, "sell_not_sellable", lang,
                      name=good_display_name(good, lang))
        return
    if status == "nothing":
        await _ereply(interaction, "sell_nothing", lang, name=good["name"])
        return
    bank = db.get_bank(good["bank_code"])
    await _ereply(interaction, "sell_done", lang, ephemeral=False, qty=info["qty"],
                  name=good["name"], unit=economy.format_money(info["unit"], bank),
                  total=economy.format_money(info["total"], bank))

@bot.tree.command(name="autocraft", description="produce a good 24/7, slower than by hand")
@app_commands.describe(good_code="Good code", action="on | off",
                       enterprise="Enterprise code to produce for (its workers only)")
async def autocraft_cmd(interaction: discord.Interaction, good_code: str, action: str,
                        enterprise: str = None):
    """Run a production line 24/7, slower than by hand.

    Advanced hourly at a fraction of the manual tempo, with leftover fractions
    carried over, and halted whenever the worker's energy runs out. Producing for
    an enterprise requires working at it."""
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    owner, server, err = _resolve_production_target(
        "discord", interaction.user.id, interaction.guild_id, enterprise)
    if err:
        await _ereply(interaction, err, lang)
        return
    worker = db.canonical_user("discord", interaction.user.id)
    on = action.strip().lower() in ("on", "enable", "1", "true", "yes")
    db.set_autocraft(owner, good["code"], on,
                     starter=(worker[1], worker[2]), server=server)
    await _ereply(interaction, "autocraft_on" if on else "autocraft_off", lang,
                  ephemeral=False, name=good_display_name(good, lang))

@bot.tree.command(name="autosend", description="auto-send a share of a good to a user or party")
@app_commands.describe(good_code="Good code", percent="Percent 0-100 (0 to stop)",
                       target="Recipient: a user ID/mention or a party code")
async def autosend_cmd(interaction: discord.Interaction, good_code: str, percent: int, target: str):
    """Send a share of a good's stock to a user, party or enterprise every
    day.

    `percent` is a whole number, and `0` stops it. Applied on the daily tick
    against whatever is in stock then, so a percentage of nothing sends
    nothing."""
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    if percent < 0 or percent > 100:
        await _ereply(interaction, "autosend_bad_percent", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    party = db.get_party(target.strip().upper())
    ent = db.get_enterprise(target.strip().upper())
    if party:
        tgt, tname = db.party_owner(party["code"]), f"{party['name']} [{party['code']}]"
    elif ent:
        tgt, tname = db.enterprise_owner(ent["code"]), f"{ent['name']} [{ent['code']}]"
    else:
        tid = _parse_user_ref(target)
        if tid is None:
            await _ereply(interaction, "autosend_bad_target", lang)
            return
        tgt, tname = db.canonical_user("discord", tid), f"<@{tid}>"
    if percent == 0:
        db.set_autosend(owner, good["code"], 0, tgt)
        await _ereply(interaction, "autosend_off", lang, ephemeral=False, name=good["name"])
        return
    db.set_autosend(owner, good["code"], percent, tgt)
    await _ereply(interaction, "autosend_on", lang, ephemeral=False, name=good["name"],
                  percent=percent, target=tname)

@bot.tree.command(name="set-profession", description="set a member's profession in your bank (bank leaders)")
@app_commands.describe(user="Member ID or mention", good_code="Good code that becomes their profession")
async def set_profession_cmd(interaction: discord.Interaction, user: str, good_code: str):
    """Override a member's profession in your bank (bank leaders).

    Without an override a profession is derived from what somebody has produced
    most of; the override matters because wages are attached to professions."""
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    if not (db.is_bank_leader(good["bank_code"], "discord", interaction.user.id)
            or is_admin("discord", interaction.user.id)):
        await _ereply(interaction, "bank_not_leader", lang)
        return
    tid = _parse_user_ref(user)
    if tid is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    db.set_profession(db.canonical_user("discord", tid), good["bank_code"], good["code"])
    await _ereply(interaction, "set_profession_done", lang, ephemeral=False,
                  user=f"<@{tid}>", name=good["name"])

@bot.tree.command(name="give-good", description="hand goods from your inventory to another user")
@app_commands.describe(user="Recipient ID or mention", good_code="Good code", qty="How many (default 1)")
async def give_good_cmd(interaction: discord.Interaction, user: str, good_code: str, qty: int = 1):
    """Hand goods from your inventory to another user.

    A transfer of stock only — mastery is personal and stays with whoever produced
    the units."""
    lang = get_chat_lang(_chat_key(interaction))
    good = db.get_good(good_code.strip().upper())
    if not good:
        await _ereply(interaction, "good_not_found", lang)
        return
    target_id = _parse_user_ref(user)
    if target_id is None:
        await _ereply(interaction, "edit_invalid_user", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    target = db.canonical_user("discord", target_id)
    if target == owner:
        await _ereply(interaction, "give_self", lang)
        return
    if qty <= 0:
        await _ereply(interaction, "bad_amount", lang)
        return
    have = db.get_inventory_qty(owner, good["code"])
    if have < qty:
        await _ereply(interaction, "give_no_goods", lang, have=have)
        return
    db.add_inventory(owner, good["code"], -qty)
    db.add_inventory(target, good["code"], qty)
    emoji = f"{good['emoji']} " if good["emoji"] else ""
    await _ereply(interaction, "give_done", lang, ephemeral=False, qty=qty, emoji=emoji,
                  name=good_display_name(good, lang), user=f"<@{target_id}>")

@bot.tree.command(name="goods", description="the goods this server can produce")
async def goods_cmd(interaction: discord.Interaction):
    """List the goods this server can produce, base goods included."""
    lang = get_chat_lang(_chat_key(interaction))
    lines = []
    for code, cat, emoji, _name in db.BASE_GOODS:
        good = db.get_good(code)
        if not good:
            continue
        lines.append(localized("goods_line_base", lang, emoji=emoji,
                               name=good_display_name(good, lang), code=code,
                               energy=economy.format_energy(good["energy_cost"])))
    chat = db.get_chat(str(interaction.guild_id)) if interaction.guild_id else None
    if chat:
        for bank in db.get_banks_in_union(chat["union_code"]):
            for good in db.get_bank_goods(bank["code"]):
                if db.is_base_good(good["code"]):
                    continue
                emoji = f"{good['emoji']} " if good["emoji"] else ""
                cat_disp = category_label(good["category"], lang) if good["category"] else "—"
                lines.append(localized("goods_line", lang, emoji=emoji, name=good["name"],
                                       code=good["code"], category=cat_disp,
                                       value=economy.format_money(good["base_value"], bank),
                                       energy=economy.format_energy(good["energy_cost"])))
    if not lines:
        await _ereply(interaction, "goods_none", lang)
        return
    embed = discord.Embed(title=localized("goods_header", lang),
                          description="\n".join(lines)[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed)
