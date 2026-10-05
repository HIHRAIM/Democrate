"""Explain one Patreon subscription's union and server capacity."""
import discord

import db
import sponsors
from config import SPONSOR_URL
from discord_bot.client import bot
from utils import get_chat_lang, localized

@bot.tree.command(name="sponsor", description="show your Democrate subscription and limits")
async def sponsor_cmd(interaction: discord.Interaction):
    """Show only this account's unions and account-wide server usage."""
    lang = get_chat_lang(str(interaction.guild_id or ""))
    tier = await sponsors.refresh_tier(interaction.user.id)
    limits = sponsors.limits(tier)
    unions = db.sponsor_unions(interaction.user.id)
    message = localized(
        "sponsor_status", lang,
        tier=(localized("sponsor_tier_community", lang) if tier == -1 else tier),
        unions=len(unions),
        union_limit=limits["unions"], servers=db.sponsor_server_usage(interaction.user.id),
        server_limit=limits["servers"], names=", ".join(unions) or "—",
        activity=db.activity_usage(interaction.user.id),
        activity_limit=limits["activity_day"],
        activity_minute=limits["activity_minute"],
        url=SPONSOR_URL)
    addendum = sponsors.status_addendum(interaction.user.id, lang)
    if addendum:
        message += "\n" + addendum
    await interaction.response.send_message(message, ephemeral=True)
