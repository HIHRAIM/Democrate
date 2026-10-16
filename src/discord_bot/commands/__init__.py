"""Every slash command of the Discord half, one module per domain.

Importing this package is what registers them: each `@bot.tree.command`
decorator fires when its module is imported, so a module nobody imports here
registers nothing and the bot silently loses those commands. Append new modules
to the list below; the order is the dependency order rather than anything
Discord cares about — `banks` comes before `trade` because `/bank rate` and
`/bank convert` reach for `_resolve_led_bank`.

The composition mirrors telegram_bot/commands/ file for file, so that the same
command is found at the same address on either side. One module has no twin
there, and it is not an omission: `moderation` scans the wiki activity that
Confederate and Wiki-Bot relay into Discord channels. `wikis` and `birthdays`
have twins that are smaller than they are, because the wiki birthday channel,
the per-wiki opt-out and both roles act on Discord channels and roles that a
Telegram group does not have.

The Olympiad's runtime commands are not here — they are declared in
discord_bot/olympiad.py with `@app_commands.command` and added to the tree only
while an Olympiad is open.
"""
from discord_bot.commands import unions
from discord_bot.commands import settings
from discord_bot.commands import admins
from discord_bot.commands import locale
from discord_bot.commands import user
from discord_bot.commands import parties
from discord_bot.commands import govt
from discord_bot.commands import quizzes
from discord_bot.commands import moderation
from discord_bot.commands import wikis
from discord_bot.commands import birthdays
from discord_bot.commands import servers
from discord_bot.commands import banks
from discord_bot.commands import trade
from discord_bot.commands import goods
from discord_bot.commands import enterprises
from discord_bot.commands import sponsors
