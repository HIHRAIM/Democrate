"""Unions and the chats bound to them: `/setup`, `/add-unia`,
`/allow-parties`, and the two bot-admin tools for seeing and leaving chats.

This is the module the whole bot is gated on — a chat with no `/setup` row
answers almost nothing, and a union with `/allow-parties` off answers no party
command. `/force_leave` is here rather than in an admin module because it acts
on the same object: the chat.

`/list_chats` and `/force_leave` reach into the Telegram half at the call site,
which is the deliberate cycle-breaker between the two halves; keep the import
inside the function.

Not this module's zone: the chat's language (commands/settings.py), the party
rows a union holds (commands/parties.py).
"""
import io
import re

import discord
from discord import app_commands

import db
import sponsors
from utils import (
    LANG_ORDER, SUPPORTED_LANGS, get_chat_lang, is_admin, language_name,
    localized, send_service_event, set_chat_lang,
)

from discord_bot.client import _chat_key, bot

UNION_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,12}$")

@bot.tree.command(name="setup", description="link this server to a union and set its language (bot admins)")
@app_commands.describe(
    union="Union code",
    code="Language code (ru, uk, pl, en, es, pt)",
)
async def setup_cmd(interaction: discord.Interaction, union: str, code: str):
    """Bind this server to a union and set its language (Bot Admins).

    The one command the whole bot is gated on: without a `chats` row almost every
    public command refuses. Running it again re-binds the server to another union
    — the parties, banks and enterprises already there are not moved with it.
    Also settles the seven-day deadline, and reports to the service chats."""
    lang = get_chat_lang(_chat_key(interaction))
    if interaction.guild is None:
        await interaction.response.send_message(localized("guild_only", lang), ephemeral=True)
        return

    union = union.strip().upper()
    code = code.strip().lower()
    admin = is_admin("discord", interaction.user.id)
    if not admin:
        await sponsors.refresh_tier(interaction.user.id)
        perms = interaction.user.guild_permissions
        if (not (perms.administrator or perms.manage_guild) or
                not sponsors.can_setup_server(interaction.user.id, interaction.guild_id, union)):
            await interaction.response.send_message(localized("sponsor_setup_forbidden", lang), ephemeral=True)
            return
    if not db.union_exists(union):
        await interaction.response.send_message(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes())),
            ephemeral=True
        )
        return
    if code not in SUPPORTED_LANGS:
        await interaction.response.send_message(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
            ephemeral=True
        )
        return

    if admin:
        former = db.union_owner(union)
        if former is not None:
            db.operator_takeover_union(union, former)
    db.setup_chat("discord", interaction.guild_id, union, interaction.user.id,
                  title=interaction.guild.name if interaction.guild else None)
    set_chat_lang(str(interaction.guild_id), code)
    await interaction.response.send_message(
        localized("setup_success", code,
                  union=db.get_union_name(union, code), code=union,
                  lang_name=language_name(code), lang=code)
    )
    await send_service_event("setup_done", platform="Discord",
                             chat=interaction.guild.name, union=union,
                             user=str(interaction.user))

@bot.tree.command(name="add-unia", description="create a new union; give its name in at least one language (bot admins)")
@app_commands.describe(
    code="New union code (2-12 latin letters/digits)",
    name_en="Union name in English",
    name_ru="Union name in Russian",
    name_uk="Union name in Ukrainian",
    name_pl="Union name in Polish",
    name_es="Union name in Spanish",
    name_pt="Union name in Portuguese",
)
async def add_unia_cmd(
    interaction: discord.Interaction,
    code: str,
    name_en: str = None,
    name_ru: str = None,
    name_uk: str = None,
    name_pl: str = None,
    name_es: str = None,
    name_pt: str = None,
):
    """Create a union, named in any subset of the six languages (Bot Admins).

    At least one name is required; the rest fall back through `get_union_name`.
    The code goes into the shared 4-character namespace, so a union cannot take a
    code a party or a currency already uses."""
    lang = get_chat_lang(_chat_key(interaction))
    admin = is_admin("discord", interaction.user.id)
    if not admin:
        tier = await sponsors.refresh_tier(interaction.user.id)
        if tier == 0:
            await interaction.response.send_message(localized("sponsor_not_active", lang), ephemeral=True)
            return
        if len(db.sponsor_unions(interaction.user.id)) >= sponsors.limits(tier)["unions"]:
            await interaction.response.send_message(localized("sponsor_union_limit", lang), ephemeral=True)
            return

    code = code.strip().upper()
    if not UNION_CODE_RE.match(code):
        await interaction.response.send_message(localized("add_unia_invalid_code", lang), ephemeral=True)
        return

    names = {}
    for L, name in (("en", name_en), ("ru", name_ru), ("uk", name_uk),
                    ("pl", name_pl), ("es", name_es), ("pt", name_pt)):
        if name and name.strip():
            names[L] = name.strip()
    if not names:
        await interaction.response.send_message(localized("add_unia_need_name", lang), ephemeral=True)
        return

    if not db.add_union(code, names, owner_id=None if admin else interaction.user.id):
        await interaction.response.send_message(localized("add_unia_exists", lang, code=code), ephemeral=True)
        return

    listed = "\n".join(f"{language_name(L)}: {names[L]}" for L in LANG_ORDER if L in names)
    await interaction.response.send_message(localized("add_unia_created", lang, code=code, names=listed))
    await send_service_event("union_created", code=code, user=str(interaction.user))

@bot.tree.command(name="allow-parties", description="enable or disable parties in a union (bot admins)")
@app_commands.describe(
    union="Union code",
    action="enable | disable",
    roles="Comma-separated role IDs whose holders may use /add-party (required for enable)",
)
async def allow_parties_cmd(interaction: discord.Interaction, union: str, action: str,
                            roles: str = None):
    """Switch parties on or off in a union and name who may found one (Bot
    Admins).

    `roles` is a comma-separated list of Discord role ids and is required to
    enable: without it nobody could use `/add-party` at all. Switching parties off
    leaves the existing ones untouched and only stops the commands — a Bot Admin
    can still tidy up through `/edit-party-admin`."""
    lang = get_chat_lang(_chat_key(interaction))
    union = union.strip().upper()
    if not sponsors.can_manage_union(interaction.user.id, union):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    if not db.union_exists(union):
        await interaction.response.send_message(
            localized("setup_unknown_union", lang, code=union, codes=", ".join(db.get_union_codes())),
            ephemeral=True,
        )
        return

    action = action.strip().lower()
    if action == "disable":
        db.set_parties_enabled(union, False)
        await interaction.response.send_message(localized("allow_parties_disabled", lang, code=union))
        await send_service_event("parties_disabled", code=union, user=str(interaction.user))
        return
    if action != "enable":
        await interaction.response.send_message(localized("allow_parties_usage", lang), ephemeral=True)
        return

    role_ids = [p.strip() for p in (roles or "").replace(" ", ",").split(",") if p.strip()]
    if not role_ids:
        await interaction.response.send_message(localized("allow_parties_need_roles", lang), ephemeral=True)
        return
    if not all(p.isdigit() for p in role_ids):
        await interaction.response.send_message(localized("allow_parties_invalid_roles", lang), ephemeral=True)
        return

    db.set_parties_enabled(union, True, role_ids)
    await interaction.response.send_message(
        localized("allow_parties_enabled", lang, code=union,
                  roles=", ".join(f"<@&{r}>" for r in role_ids))
    )
    await send_service_event("parties_enabled", code=union, user=str(interaction.user))

@bot.tree.command(name="list_chats", description="list all chats the bot is in (bot admins)")
async def list_chats(interaction: discord.Interaction):
    """List every chat the bot is in, on both platforms, with their union
    bindings (Bot Admins).

    The Telegram half is reached at the call site — the cycle-breaker between the
    halves — and its groups are listed from the database rather than from the API,
    because aiogram has no equivalent of a guild list."""
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    lines = [localized("list_chats_discord_header", lang)]
    for g in bot.guilds:
        row = db.get_chat(str(g.id))
        union = f" [{row['union_code']}]" if row else ""
        lines.append(f"- {g.name} — id: {g.id}{union}")

    tg_ids = db.get_telegram_group_ids()
    if tg_ids:
        lines.append("\n" + localized("list_chats_telegram_header", lang))
        try:
            from telegram_bot import bot as tg_bot
            for pid in tg_ids:
                row = db.get_chat(pid)
                union = f" [{row['union_code']}]" if row else ""
                try:
                    chat = await tg_bot.get_chat(int(pid))
                    title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(pid)
                except Exception:
                    title = str(pid)
                lines.append(f"- {title} — id: {pid}{union}")
        except Exception:
            for pid in tg_ids:
                lines.append(f"- id: {pid}")
    else:
        lines.append("\n" + localized("list_chats_no_telegram", lang))

    msg = "\n".join(lines)
    if len(msg) > 1900:
        bio = io.BytesIO(msg.encode("utf-8"))
        bio.seek(0)
        await interaction.response.send_message(localized("list_chats_too_long", lang), ephemeral=True)
        await interaction.followup.send(file=discord.File(bio, filename="chat_list.txt"))
    else:
        await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="force_leave", description="make the bot leave a chat (bot admins)")
@app_commands.describe(platform="discord or telegram", target_id="Server / group ID")
async def force_leave(interaction: discord.Interaction, platform: str, target_id: str):
    """Make the bot leave a server or group by id (Bot Admins).

    Leaving is not deleting: the union binding is dropped so a later
    re-invitation starts clean, but nothing the community created — parties,
    banks, enterprises — is touched."""
    lang = get_chat_lang(_chat_key(interaction))
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    platform = platform.strip().lower()
    target_id = target_id.strip()

    if platform == "discord":
        try:
            gid = int(target_id)
        except ValueError:
            await interaction.response.send_message(localized("force_leave_invalid_id", lang), ephemeral=True)
            return

        guild = bot.get_guild(gid)
        if not guild:
            await interaction.response.send_message(localized("force_leave_not_member", lang), ephemeral=True)
            return

        try:
            await guild.leave()
        except Exception as e:
            await interaction.response.send_message(localized("force_leave_failed", lang, error=e), ephemeral=True)
            return

        db.remove_chat(gid)
        await interaction.response.send_message(localized("force_leave_success_discord", lang, guild_id=gid), ephemeral=True)
        return

    if platform == "telegram":
        try:
            tid = int(target_id)
        except ValueError:
            await interaction.response.send_message(localized("force_leave_invalid_id", lang), ephemeral=True)
            return

        try:
            from telegram_bot import bot as tg_bot
            await tg_bot.leave_chat(tid)
        except Exception as e:
            await interaction.response.send_message(localized("force_leave_failed", lang, error=e), ephemeral=True)
        db.remove_chat(tid)
        await interaction.response.send_message(localized("force_leave_success_telegram", lang, chat_id=tid), ephemeral=True)
        return

    await interaction.response.send_message(localized("force_leave_unsupported_platform", lang), ephemeral=True)
