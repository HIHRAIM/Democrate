"""All three @bot.event handlers and nothing else — the dispatcher into
everything the Discord half does without being asked.

`on_guild_join` records the arrival and tells the service chats, because a Bot
Admin now has seven days to bind the server to a union; the row it writes is a
record rather than the clock, since the sweep reads Discord's own
`Guild.me.joined_at`. `on_guild_remove` forgets the setup deadline. Sponsored
bindings stay until verified retention cleanup; operator bindings are removed
on departure.

`on_message` is message earning and nothing else. It is independent of the
`wait_for` dialogs and of the slash commands — discord.py dispatches those
separately — so counting a message here never disturbs a dialog waiting for
the same message.

Which channel a message earns in is `_earn_keys`, and it is not simply the
channel the message is in: threads and forum posts are channels of their own,
so the binding has to be looked for outwards through the parent channel and
its category.

Not this module's zone: the earning rules themselves (economy/activity.py) and
the seven-day policy (setup_deadline.py).
"""
import discord

import db
import economy
from utils import send_service_event

from discord_bot.client import _earn_keys, bot, logger

@bot.event
async def on_guild_join(guild: discord.Guild):
    """The bot was added to a server: record the join and tell the service
    chats, since a Bot Admin now has seven days to bind it to a union with
    `/setup` (setup_deadline.py).

    The row is a record, not the clock — the sweep reads Discord's own
    `Guild.me.joined_at`, so a missed event costs nothing but this notice."""
    db.record_join("discord", guild.id)
    await send_service_event(
        "joined_chat",
        platform="Discord",
        chat=guild.name or str(guild.id),
        chat_id=guild.id,
    )

@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Keep sponsored bindings until their verified retention cleanup.

    Operator bindings keep their older immediate removal behavior. An
    expired sponsor's cleanup needs the original binding to find and archive
    the union even when the bot was kicked before the retention deadline.
    """
    bound = db.get_chat(guild.id)
    if not bound or db.union_owner(bound["union_code"]) is None:
        db.remove_chat(guild.id)
    db.forget_deadline("discord", guild.id)

@bot.event
async def on_message(message: discord.Message):
    """Message earning. Independent of the wait_for dialogs and slash commands
    (those are dispatched separately), so counting a message here never disturbs
    them. A message earns only when the channel is a /set-earn earning channel,
    the author is a verified human, and the anti-abuse gate (min length, per-user
    cooldown, hourly cap) lets it through — see economy.earn_from_message."""
    try:
        if message.author.bot or message.guild is None:
            return
        content = (message.content or "").strip()
        if not content or content.startswith("/"):
            return
        earn = db.resolve_earn_channel("discord", _earn_keys(message.channel))
        if not earn:
            return
        economy.earn_from_message("discord", message.author.id, str(message.author),
                                  earn["bank_code"], len(content), earn["rate"],
                                  server_id=message.guild.id)
    except Exception as e:
        logger.warning("on_message earning error: %s", e)
