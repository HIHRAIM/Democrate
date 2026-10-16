"""The command groups of the Discord half: `/party add`, `/bank pay`,
`/union info`.

Discord lets an application register at most 100 top-level commands, and
Democrate has more than that once the Olympiad's commands are on the tree.
A group is one top-level command however many actions it holds, and it is
also what makes Discord's picker useful: typing `/party` lists every party
action with its description, which is how people find a command here rather
than through `/help`. Only `/help` and `/calendar` stay on their own.

Each command module decorates its callbacks with `@<group>.command(...)`,
so this module is imported right after the client and before anything that
registers a command; `register()` puts the groups on the tree once. Where
commands must work in private chats or under a user install (the quizzes,
the language of a private chat, privacy settings, the backup, the
Olympiad), that is a property of the group — Discord does not allow it per
subcommand, so the rest of such a group shows there too and refuses with
`guild_only` where it needs a server.

The Telegram half has no groups: the same actions are `/party_add`,
`/bank_pay`, and utils.localized writes a command mentioned in a reply in
that spelling when the reply goes to Telegram.

Not this module's zone: the commands themselves (discord_bot/commands/,
discord_bot/olympiad.py).
"""
from discord import app_commands
from discord.app_commands import AppCommandContext, AppInstallationType

from utils import COMMAND_GROUPS, localized

from discord_bot.client import bot, logger

_ANYWHERE = {"allowed_contexts": AppCommandContext(guild=True, dm_channel=True,
                                                   private_channel=True),
             "allowed_installs": AppInstallationType(guild=True, user=True)}

def _group(name, **extra):
    """A top-level command group named `name`, described by
    `slash_group_<name>`."""
    return app_commands.Group(name=name, description=localized(f"slash_group_{name}", "en"),
                              **extra)

account_group = _group("account", **_ANYWHERE)
admin_group = _group("admin")
bank_group = _group("bank")
birthday_group = _group("birthday")
bot_group = _group("bot", **_ANYWHERE)
enterprise_group = _group("enterprise")
find_group = _group("find")
good_group = _group("good")
govt_group = _group("govt")
locale_group = _group("locale")
localizer_group = _group("localizer")
olympiad_group = _group("olympiad", **_ANYWHERE)
party_group = _group("party")
quiz_group = _group("quiz", **_ANYWHERE)
server_group = _group("server", **_ANYWHERE)
sponsor_group = _group("sponsor")
union_group = _group("union")
wiki_group = _group("wiki")

GROUPS = (account_group, admin_group, bank_group, birthday_group, bot_group,
          enterprise_group, find_group, good_group, govt_group, locale_group,
          localizer_group, olympiad_group, party_group, quiz_group, server_group,
          sponsor_group, union_group, wiki_group)

DISCORD_ONLY = {"account": {"verify", "telegram"}, "birthday": {"role"},
                "find": {"with", "without"}, "olympiad": {"accept", "deny"},
                "wiki": {"channel", "greeting", "rep-role"}}

def register():
    """Put every group on the command tree. Called once, from
    discord_bot/__init__.py, after every command module has added its
    subcommands.

    Also compares each group with utils.COMMAND_GROUPS, the table that turns
    `/bank pay` into `/bank_pay` in texts read on Telegram: an action added
    here and missing there still works, but Telegram readers would be told a
    command with a space in it. The Olympiad's runtime actions are compared
    with the commands that carry them (OLYMPIAD_COMMANDS), whether or not they
    are on the group right now; DISCORD_ONLY are the actions that have no
    Telegram twin and must stay out of the table."""
    from discord_bot.olympiad import OLYMPIAD_COMMANDS

    for group in GROUPS:
        bot.tree.add_command(group, override=True)
        actions = {cmd.name for cmd in group.commands}
        if group is olympiad_group:
            actions |= {cmd.name for cmd in OLYMPIAD_COMMANDS}
        actions -= DISCORD_ONLY.get(group.name, set())
        known = set(COMMAND_GROUPS.get(group.name, ()))
        if actions != known:
            logger.warning("utils.COMMAND_GROUPS[%r] differs from the group: missing %s, extra %s",
                           group.name, sorted(actions - known), sorted(known - actions))
