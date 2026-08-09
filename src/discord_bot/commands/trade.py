"""What links two banks, or a bank and a party: `/treaty`, `/set-rate`,
`/convert`, `/rates`, `/set-wage`, `/party-dues`.

A treaty needs the other bank's leader to accept it with the consent buttons,
which is why proposing one is a dialog and not a write. `/set-rate` refuses
whenever either bank has a second treaty partner — the check is
economy/trade.py: can_manual_peg — because a fixed edge inside a triangle of
currencies lets the triangle disagree with itself.

`/set-wage` and `/party-dues` set monthly obligations rather than moving
anything; the transfers happen on the monthly tick (economy/payroll.py). Both
are stored in minor units, and a `0` clears them.

Not this module's zone: the daily currency valuation (economy/trade.py), the
accounts (commands/banks.py) and the payroll run (economy/payroll.py).
"""
import discord
from discord import app_commands

import db
import economy
from utils import DEFAULT_EMBED_COLOR, get_chat_lang, localized

from discord_bot.client import _chat_key, bot
from discord_bot.commands.banks import _resolve_led_bank
from discord_bot.dialogs import _LeaderConsentView, _econ_embed, _ereply, _say
from discord_bot.parties import _resolve_led_party

@bot.tree.command(name="treaty", description="propose a currency-conversion treaty with another bank (bank leaders)")
@app_commands.describe(code="Currency code of the other bank")
async def treaty_cmd(interaction: discord.Interaction, code: str):
    """Propose a currency-conversion treaty to another bank (bank leaders).

    Nothing is written until a leader of the other bank accepts with the consent
    buttons — a treaty binds both sides. Once signed, the two currencies are
    convertible at the computed rate."""
    lang = get_chat_lang(_chat_key(interaction))
    other = db.get_bank(code.strip().upper())
    if not other:
        await _ereply(interaction, "bank_not_found", lang)
        return
    mine = await _resolve_led_bank(interaction, lang)
    if mine is None:
        return
    if mine["code"] == other["code"]:
        await _ereply(interaction, "treaty_self", lang)
        return
    if db.has_treaty(mine["code"], other["code"]):
        await _ereply(interaction, "treaty_exists", lang)
        return
    channel = interaction.channel

    async def on_accept(interaction2):
        """Sign the treaty now that the other bank's leader has agreed, and say
        so in the chat."""
        db.add_treaty(mine["code"], other["code"])
        await channel.send(embed=_econ_embed(
            localized("treaty_done", lang, a=mine["code"], b=other["code"])))

    async def on_decline(interaction2):
        """Say the treaty was refused. Nothing was written."""
        await channel.send(embed=_econ_embed(localized("treaty_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("treaty_offer", lang, proposer=mine["code"], code=other["code"],
                  name=other["currency_name"])),
        view=_LeaderConsentView(lang, other["code"], on_accept, on_decline))

@bot.tree.command(name="set-rate", description="fix a pegged rate with your bank's sole treaty partner (bank leaders)")
@app_commands.describe(code="The partner bank's currency code",
                       rate="Units of the partner currency per 1 unit of yours")
async def set_rate_cmd(interaction: discord.Interaction, code: str, rate: float):
    """Fix the rate with your bank's sole treaty partner (bank leaders).

    Refused as soon as either bank has a second partner: a fixed edge inside a
    triangle of currencies would let the triangle disagree with itself. `rate` is
    units of the partner's currency per one unit of yours, and the partner's leader
    must accept it."""
    lang = get_chat_lang(_chat_key(interaction))
    other = db.get_bank(code.strip().upper())
    if not other:
        await _ereply(interaction, "bank_not_found", lang)
        return
    mine = await _resolve_led_bank(interaction, lang)
    if mine is None:
        return
    if not db.has_treaty(mine["code"], other["code"]):
        await _ereply(interaction, "set_rate_no_treaty", lang)
        return
    if not economy.can_manual_peg(mine["code"], other["code"]):
        await _ereply(interaction, "set_rate_multi", lang)
        return
    if rate <= 0:
        await _ereply(interaction, "set_rate_bad", lang)
        return
    channel = interaction.channel

    async def on_accept(interaction2):
        """Pin the rate now that the partner bank's leader has agreed."""
        db.set_pegged_rate(mine["code"], other["code"], rate)
        await channel.send(embed=_econ_embed(
            localized("set_rate_done", lang, a=mine["code"], rate=f"{rate:g}", b=other["code"])))

    async def on_decline(interaction2):
        """Say the peg was refused; the computed rate stays in force."""
        await channel.send(embed=_econ_embed(localized("set_rate_declined", lang)))

    await _say(interaction, embed=_econ_embed(
        localized("set_rate_offer", lang, a=mine["code"], rate=f"{rate:g}", b=other["code"])),
        view=_LeaderConsentView(lang, other["code"], on_accept, on_decline))

@bot.tree.command(name="convert", description="convert between your accounts at the current rate")
@app_commands.describe(amount="Amount in the source currency", from_code="Source currency", to_code="Target currency")
async def convert_cmd(interaction: discord.Interaction, amount: str, from_code: str, to_code: str):
    """Convert between your own accounts at the current rate.

    Charges the configured spread, which is one of the sinks that balance wage
    emission. Requires a treaty (or the same currency twice, which is refused as
    pointless)."""
    lang = get_chat_lang(_chat_key(interaction))
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    fb = db.get_bank(from_code.strip().upper())
    tb = db.get_bank(to_code.strip().upper())
    if not fb or not tb:
        await _ereply(interaction, "bank_not_found", lang)
        return
    owner = db.canonical_user("discord", interaction.user.id)
    status, info = economy.convert(owner, fb["code"], tb["code"], minor)
    keymap = {"same": "convert_same", "not_convertible": "convert_not_convertible",
              "too_small": "convert_too_small", "no_funds": "convert_no_funds"}
    if status != "ok":
        await _ereply(interaction, keymap.get(status, "convert_not_convertible"), lang)
        return
    await _ereply(interaction, "convert_done", lang, ephemeral=False,
                  amount=economy.format_money(minor, fb),
                  credited=economy.format_money(info["credited"], tb),
                  rate=f"{info['rate']:.4f}", fee=economy.format_money(info["fee"], fb))

@bot.tree.command(name="rates", description="current currency values and conversion rates")
@app_commands.describe(code="Currency code (omit for all currency values)")
async def rates_cmd(interaction: discord.Interaction, code: str = None):
    """Show currency values, or one currency's rates against its treaty
    partners."""
    lang = get_chat_lang(_chat_key(interaction))
    if code:
        bank = db.get_bank(code.strip().upper())
        if not bank:
            await _ereply(interaction, "bank_not_found", lang)
            return
        partners = db.treaty_partners(bank["code"])
        if not partners:
            await _ereply(interaction, "rates_none", lang, ephemeral=False, code=bank["code"])
            return
        lines = []
        for p in partners:
            r = economy.rate(bank["code"], p)
            treaty = db.get_treaty(bank["code"], p)
            peg = f" {localized('rates_pegged', lang)}" if treaty and treaty["pegged_rate"] is not None else ""
            lines.append(localized("rates_line", lang, a=bank["code"], b=p,
                                   rate=f"{r:.4f}" if r is not None else "—", peg=peg))
        title = localized("rates_title_one", lang, code=bank["code"])
    else:
        banks = db.get_all_banks()
        if not banks:
            await _ereply(interaction, "rates_no_banks", lang, ephemeral=False)
            return
        lines = [localized("rates_value_line", lang, code=b["code"], name=b["currency_name"],
                           value=f"{b['value']:.4f}") for b in banks]
        title = localized("rates_title_all", lang)
    embed = discord.Embed(title=title, description="\n".join(lines)[:4000],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    await _say(interaction, embed=embed)

@bot.tree.command(name="set-wage", description="set the monthly wage for a profession in your bank (bank leaders)")
@app_commands.describe(profession="Good code or name defining the profession", amount="Monthly wage (0 to clear)")
async def set_wage_cmd(interaction: discord.Interaction, profession: str, amount: str):
    """Set the monthly wage for a profession in your bank (bank leaders).

    The profession is named by a good — a person's profession is the good they have
    produced most of. `0` clears the wage. The money is minted on the monthly tick,
    not now."""
    lang = get_chat_lang(_chat_key(interaction))
    bank = await _resolve_led_bank(interaction, lang)
    if bank is None:
        return
    good = db.find_bank_good(bank["code"], profession)
    if not good:
        await _ereply(interaction, "set_wage_no_good", lang, bank=bank["code"])
        return
    if amount.strip() in ("0", "0.0", "0.00"):
        db.set_wage(bank["code"], good["code"], None)
        await _ereply(interaction, "set_wage_cleared", lang, ephemeral=False, name=good["name"])
        return
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    db.set_wage(bank["code"], good["code"], minor)
    await _ereply(interaction, "set_wage_done", lang, ephemeral=False, name=good["name"],
                  amount=economy.format_money(minor, bank))

@bot.tree.command(name="party-dues", description="set your party's monthly member dues (party leaders)")
@app_commands.describe(code="Currency code", amount="Monthly dues per member (0 to clear)")
async def party_dues_cmd(interaction: discord.Interaction, code: str, amount: str):
    """Set your party's monthly member dues in one currency (party leaders).

    Collected on the monthly tick from every member, and a member who cannot pay is
    pushed into debt rather than skipped. `0` clears it."""
    lang = get_chat_lang(_chat_key(interaction))
    party = await _resolve_led_party(interaction, lang)
    if party is None:
        return
    bank = db.get_bank(code.strip().upper())
    if not bank:
        await _ereply(interaction, "bank_not_found", lang)
        return
    if bank["union_code"] != party["union_code"]:
        await _ereply(interaction, "dues_wrong_union", lang)
        return
    if amount.strip() in ("0", "0.0", "0.00"):
        db.set_party_dues(party["code"], bank["code"], 0)
        await _ereply(interaction, "dues_cleared", lang, ephemeral=False, name=party["name"])
        return
    minor = economy.parse_amount(amount)
    if minor is None:
        await _ereply(interaction, "bad_amount", lang)
        return
    db.set_party_dues(party["code"], bank["code"], minor)
    await _ereply(interaction, "dues_done", lang, ephemeral=False, name=party["name"],
                  amount=economy.format_money(minor, bank))
