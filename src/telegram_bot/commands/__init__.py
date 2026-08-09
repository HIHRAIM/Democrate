"""Every command of the Telegram half, one module per domain.

Importing this package is what registers them: each `@router.message(Command(...))`
decorator fires when its module is imported, so a module nobody imports here
registers nothing and the bot silently loses those commands.

The order below matters twice. It is a dependency order — `banks` before
`trade`, because `/set_rate` and `/convert` reach for `_resolve_led_bank_tg` —
and it is the dispatch order aiogram will use. Every command handler here
carries a `Command(...)` filter and no two of them claim the same name, so no
message can match two handlers and the relative order among them cannot change
behaviour. The one handler where order *is* load-bearing is not here at all:
telegram_bot/catchall.py has no filter and is imported after everything.

The composition mirrors discord_bot/commands/ file for file, so that the same
command is found at the same address on either side. Two Discord modules have
no twin here and neither is an omission. `moderation` (`/find-with`,
`/find-without`) reads back the wiki activity that Confederate and Wiki-Bot
relay into Discord channels, so that an admin can pick one person's edits out
of it — there is no such relay to read on this side. `foundays` posts an
anniversary greeting through a Discord webhook, which is what lets it carry the
wiki's own name and avatar; Telegram has no equivalent.

`/verify` is likewise Discord-only, and for a reason outside this bot: a Fandom
profile can publish a Discord handle and nothing else. A Telegram user reaches
the same standing by linking to a verified Discord account with `/add_discord`,
or by having an Olympiad reviewer remember their wiki account.
"""
from telegram_bot.commands import unions
from telegram_bot.commands import settings
from telegram_bot.commands import admins
from telegram_bot.commands import locale
from telegram_bot.commands import user
from telegram_bot.commands import parties
from telegram_bot.commands import govt
from telegram_bot.commands import quizzes
from telegram_bot.commands import banks
from telegram_bot.commands import trade
from telegram_bot.commands import goods
from telegram_bot.commands import enterprises
