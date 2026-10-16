"""Unions and the chats bound to them: `/server setup`, `/union add`,
`/union parties`, the union's public profile `/union info` and its edit menu
`/union edit`, and the two bot-admin tools for seeing and leaving chats.

This is the module the whole bot is gated on — a chat with no `/server setup` row
answers almost nothing, and a union with `/union parties` off answers no party
command. `/bot leave` is here rather than in an admin module because it acts
on the same object: the chat.

`/bot chats` and `/bot leave` reach into the Telegram half at the call site,
which is the deliberate cycle-breaker between the two halves; keep the import
inside the function.

`/union edit` is the union's counterpart of `/bank edit`: a numbered menu
whose items ask one question each. Bot Admins may edit any union and sponsors
only their own, and the owner item is the one that is not symmetrical. A Bot
Admin may hand a union to any account (or make it an operator union with
`none`) at once; an owner without an active subscription stops the union's
message earning and may put its communities on the sponsor-retention clock,
so that case is confirmed by typing the union code first
(sponsors.py: owner_change_warning). A sponsor's transfer goes only to an
active sponsor with room for the union and every community it holds
(sponsors.py: can_receive_union) and waits for that sponsor's consent.

`/union info` answers with the counts and a row of buttons, one per full list
(stats.py: union_list); a list comes back ephemerally to whoever pressed,
with arrows when it runs past one page.

Not this module's zone: the chat's language (commands/settings.py), the party
rows a union holds (commands/parties.py), and what the profile counts
(stats.py: union_profile).
"""
import io
import re

import discord
from discord import ButtonStyle, app_commands, ui

import db
import sponsors
import stats
from message_relay import clean_display_name
from utils import (
    DEFAULT_EMBED_COLOR, LANG_ORDER, SUPPORTED_LANGS, get_chat_lang, is_admin,
    apply_party_roles, language_name, localized, parse_role_ids, parse_union_names,
    rate_limit_ok, send_service_event,
    format_date, set_chat_lang,
)

from discord_bot.client import _chat_key, bot
from discord_bot.dialogs import (
    DIALOG_TIMEOUT, _LOGO_EXT, _ConsentView, _LinesPager, _dialog_logo, _dialog_text,
    _econ_embed, _ereply, _numbered_choice, _parse_user_ref, _say,
)
from discord_bot.groups import bot_group, server_group, union_group

UNION_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,12}$")

@server_group.command(name="setup", description="link this server to a union and set its language (bot admins)")
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

@union_group.command(name="add", description="create a new union; give its name in at least one language (bot admins)")
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

@union_group.command(name="parties", description="parties in a union: on or off, and who may found one (bot admins, sponsors)")
@app_commands.describe(
    union="Union code",
    action="enable | disable | add | remove",
    roles="Roles (mentions or IDs, comma-separated) whose holders may use /party add",
)
async def allow_parties_cmd(interaction: discord.Interaction, union: str, action: str,
                            roles: str = None):
    """Switch parties on or off in a union, and choose the Discord roles
    whose holders may found one (Bot Admins; sponsors for their own unions).

    `enable` takes the role list or keeps the one already stored, `add` and
    `remove` change single roles (utils.apply_party_roles). Switching parties
    off leaves the existing ones untouched and only stops the commands — a
    Bot Admin can still tidy up through `/party edit-admin`. A role deleted
    from its server leaves the list by itself (discord_bot/events.py:
    on_guild_role_delete)."""
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
    role_ids = parse_role_ids(roles)
    if role_ids is None:
        await interaction.response.send_message(localized("allow_parties_invalid_roles", lang),
                                                ephemeral=True)
        return
    action = action.strip().lower()
    key, allowed, enabled = apply_party_roles(union, action, role_ids)
    if key in ("allow_parties_usage", "allow_parties_need_roles"):
        await interaction.response.send_message(localized(key, lang), ephemeral=True)
        return
    text = localized(key, lang, code=union, roles=", ".join(f"<@&{r}>" for r in allowed) or "—")
    if action in ("add", "remove") and not enabled:
        text += "\n" + localized("allow_parties_still_off", lang, code=union)
    await interaction.response.send_message(text)
    event = {"enable": "parties_enabled", "disable": "parties_disabled"}.get(action,
                                                                             "parties_roles")
    await send_service_event(event, code=union, user=str(interaction.user),
                             roles=", ".join(str(r) for r in allowed) or "—")

@bot_group.command(name="chats", description="list all chats the bot is in (bot admins)")
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

@bot_group.command(name="leave", description="make the bot leave a chat (bot admins)")
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

@union_group.command(name="info", description=localized("slash_union", "en"))
@app_commands.describe(query=localized("slash_param_union_query", "en"))
async def union_cmd(interaction: discord.Interaction, query: str = None):
    """Show a union's public profile (everyone): this server's union, or the
    one named by its code or by a name in any of its languages.

    A query that matches no union exactly is answered with a numbered choice
    of the unions it resembles (stats.find_union), or with "not found" when
    it resembles none."""
    lang = get_chat_lang(_chat_key(interaction))
    if not rate_limit_ok(("union-profile", "discord", interaction.user.id), limit=5,
                         window_seconds=60):
        await _ereply(interaction, "union_profile_slow_down", lang)
        return
    if query and query.strip():
        code, close = stats.find_union(query, lang)
        if code is None:
            if not close:
                await _ereply(interaction, "union_not_found", lang)
                return
            code = await _numbered_choice(interaction, lang,
                                          localized("union_not_found_suggest", lang), close,
                                          lambda c: stats.union_choice_line(c, lang))
            if code is None:
                return
    else:
        chat = db.get_chat(interaction.guild_id) if interaction.guild_id else None
        code = chat["union_code"] if chat else None
    if not code:
        await _ereply(interaction, "union_profile_specify", lang)
        return
    profile = stats.union_profile(code, lang, "discord")
    first_title, first_rows = profile["sections"][0]
    embed = discord.Embed(title=f"{profile['title']} [{code}]",
                          description="\n".join(f"**{label}:** {val}" for label, val in first_rows),
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    for title, rows in profile["sections"][1:]:
        value = "\n".join(f"**{label}:** {val}" for label, val in rows)
        embed.add_field(name=title, value=value[:1024], inline=False)
    kwargs = {}
    if profile["lists"]:
        embed.set_footer(text=localized("union_profile_lists_hint", lang))
        kwargs["view"] = _UnionListsView(code, lang, profile["lists"])
    if profile["logo"]:
        fname = f"union.{_LOGO_EXT.get(profile['logo_mime'], 'png')}"
        kwargs["file"] = discord.File(io.BytesIO(profile["logo"]), filename=fname)
        embed.set_thumbnail(url=f"attachment://{fname}")
    await _say(interaction, embed=embed, **kwargs)

class _UnionListsView(ui.View):
    """The buttons under a /union info profile, one per full list. Anybody may
    press them — the lists are as public as the profile — and the list comes
    back to the presser alone, so a busy channel is not flooded."""

    def __init__(self, code, lang, kinds):
        """One button per list that has anything in it."""
        super().__init__(timeout=DIALOG_TIMEOUT)
        for kind in kinds:
            button = ui.Button(label=localized(f"union_list_kind_{kind}", lang),
                               style=ButtonStyle.secondary)
            button.callback = self._make_cb(code, kind)
            self.add_item(button)

    def _make_cb(self, code, kind):
        """One button's callback, closing over the list it opens."""
        async def cb(interaction2: discord.Interaction):
            """Send the first page of the list, with arrows when it has more."""
            lang = get_chat_lang(_chat_key(interaction2))
            if not db.union_exists(code):
                await interaction2.response.send_message(
                    localized("setup_unknown_union", lang, code=code,
                              codes=", ".join(db.get_union_codes())), ephemeral=True)
                return
            if kind == "enterprises":
                await stats.refresh_union_titles(code)
            title, lines = stats.union_list(code, kind, lang)
            pager = _LinesPager(title, [discord.utils.escape_markdown(l) for l in lines], lang,
                                empty=localized("union_list_empty", lang))
            extra = {"view": pager} if pager.pages > 1 else {}
            await interaction2.response.send_message(embed=pager.embed(), ephemeral=True, **extra)
        return cb

_EDIT_UNION_OPTIONS = ("name", "logo", "owner", "lang", "created")

async def _resolve_edit_union(interaction, lang, code):
    """The union the caller may edit: the one named; else this server's
    union when they may manage it; else the only union they own. Replies and
    returns None when there is none."""
    uid = interaction.user.id
    if not is_admin("discord", uid):
        await sponsors.refresh_tier(uid)
    if code:
        code = code.strip().upper()
        if not db.union_exists(code):
            await _ereply(interaction, "setup_unknown_union", lang, code=code,
                          codes=", ".join(db.get_union_codes()))
            return None
    else:
        chat = db.get_chat(interaction.guild_id) if interaction.guild_id else None
        owned = db.sponsor_unions(uid)
        if chat and sponsors.can_manage_union(uid, chat["union_code"]):
            code = chat["union_code"]
        elif len(owned) == 1:
            code = owned[0]
        else:
            await _ereply(interaction, "edit_union_specify", lang)
            return None
    if not sponsors.can_manage_union(uid, code):
        await _ereply(interaction, "no_permission", lang)
        return None
    return code

def _names_text(code):
    """A union's names, one 'Language: name' line each."""
    names = db.get_union_names(code)
    return "\n".join(f"{language_name(L)}: {names[L]}" for L in LANG_ORDER if L in names)

async def _edit_union_owner(interaction, lang, code, raw):
    """The owner item of /union edit. A Bot Admin may hand the union to any
    account; when that account has no active subscription the consequences
    are spelled out and the union code must be typed to confirm, and when it
    is merely over its limits the warning follows the transfer. A sponsor
    offers the union only to an active sponsor with room for it, who accepts
    with buttons."""
    channel = interaction.channel
    admin = is_admin("discord", interaction.user.id)
    if raw.strip().lower() in ("none", "-"):
        if not admin:
            await channel.send(embed=_econ_embed(localized("edit_union_owner_admin_only", lang)))
            return
        db.set_union_owner(code, None)
        await channel.send(embed=_econ_embed(
            localized("edit_union_owner_operator", lang, union=code)))
        await send_service_event("union_owner_changed", code=code, owner="—",
                                 user=str(interaction.user))
        return
    target = _parse_user_ref(raw)
    if target is None:
        await channel.send(embed=_econ_embed(localized("edit_invalid_user", lang)))
        return
    reason = await sponsors.can_receive_union(target, code)
    if reason == "edit_union_owner_same" or (reason and not admin):
        await channel.send(embed=_econ_embed(localized(reason, lang)))
        return
    if admin:
        confirm, warning = sponsors.owner_change_warning(target, reason, lang, f"<@{target}>")
        if confirm:
            answer = await _dialog_text(
                interaction, lang,
                warning + "\n\n" + localized("edit_union_owner_confirm", lang, code=code),
                as_embed=True)
            if answer is None:
                return
            if (answer.content or "").strip().upper() != code:
                await channel.send(embed=_econ_embed(localized("action_cancelled", lang)))
                return
            warning = ""
        db.set_union_owner(code, target)
        text = localized("edit_union_owner_done", lang, union=code, user=f"<@{target}>")
        await channel.send(embed=_econ_embed(f"{text}\n{warning}" if warning else text))
        await send_service_event("union_owner_changed", code=code, owner=str(target),
                                 user=str(interaction.user))
        return
    owner = interaction.user.id

    async def on_accept(interaction2):
        """Hand the union over, after checking that nothing changed while the
        offer was open."""
        reason = await sponsors.can_receive_union(target, code)
        if db.union_owner(code) != str(owner) or reason:
            await channel.send(embed=_econ_embed(
                localized(reason or "edit_union_owner_stale", lang)))
            return
        db.set_union_owner(code, target)
        await channel.send(embed=_econ_embed(localized(
            "edit_union_owner_done", lang, union=code, user=f"<@{target}>")))
        await send_service_event("union_owner_changed", code=code, owner=str(target),
                                 user=str(interaction.user))

    async def on_decline(interaction2):
        """Say the offer was turned down; nothing was written."""
        await channel.send(embed=_econ_embed(localized("edit_union_owner_declined", lang)))

    await _say(interaction, f"<@{target}>", embed=_econ_embed(
        localized("edit_union_owner_offer", lang, mention=f"<@{target}>",
                  union=db.get_union_name(code, lang), code=code)),
        view=_ConsentView(lang, target, on_accept, on_decline),
        allowed_mentions=discord.AllowedMentions(users=True))

def _date_value(value):
    """A typed date (DD.MM.YYYY or YYYY-MM-DD) as a UTC timestamp, or None so
    the dialog asks again."""
    import celebrations
    try:
        return celebrations.parse_date(value)
    except ValueError:
        return None

def _lang_code(value):
    """One of the six language codes, or None so the dialog asks again."""
    code = value.strip().lower()
    return code if code in SUPPORTED_LANGS else None

@union_group.command(name="edit", description=localized("slash_edit_union", "en"))
@app_commands.describe(union=localized("slash_param_union_optional", "en"),
                       option=localized("slash_param_menu_option", "en"))
async def edit_union_cmd(interaction: discord.Interaction, union: str = None,
                         option: int = None):
    """Edit a union through a numbered menu: its names, logo, owner and
    language (Bot Admins; sponsors for their own unions)."""
    lang = get_chat_lang(_chat_key(interaction))
    code = await _resolve_edit_union(interaction, lang, union)
    if code is None:
        return
    if option is None or not 1 <= option <= len(_EDIT_UNION_OPTIONS):
        items = [localized(f"edit_union_opt_{key}", lang) for key in _EDIT_UNION_OPTIONS]
        chosen = await _numbered_choice(
            interaction, lang,
            localized("edit_union_menu_header", lang, name=db.get_union_name(code, lang),
                      code=code),
            list(enumerate(items)), lambda it: it[1])
        if chosen is None:
            return
        action = _EDIT_UNION_OPTIONS[chosen[0]]
    else:
        action = _EDIT_UNION_OPTIONS[option - 1]

    channel = interaction.channel
    if action == "name":
        msg = await _dialog_text(interaction, lang,
                                 localized("edit_union_ask_name", lang, current=_names_text(code)),
                                 as_embed=True)
        if msg is None:
            return
        names = parse_union_names(msg.content)
        if names is None:
            await channel.send(embed=_econ_embed(localized("edit_union_bad_name", lang)))
            return
        names = {L: clean_display_name(n, max_len=100) if n else None for L, n in names.items()}
        if not db.set_union_names(code, names):
            await channel.send(embed=_econ_embed(localized("add_unia_need_name", lang)))
            return
        await channel.send(embed=_econ_embed(localized("edit_union_names_done", lang,
                                                       names=_names_text(code))))
    elif action == "logo":
        logo = await _dialog_logo(interaction, lang, localized("edit_union_ask_logo", lang))
        if logo is None:
            return
        db.set_union_logo(code, logo[0], logo[1])
        await channel.send(embed=_econ_embed(localized("edit_union_done", lang)))
    elif action == "owner":
        msg = await _dialog_text(interaction, lang, localized("edit_union_ask_owner", lang),
                                 as_embed=True)
        if msg is None:
            return
        await _edit_union_owner(interaction, lang, code, msg.content or "")
    elif action == "lang":
        new = await _dialog_text(
            interaction, lang,
            localized("edit_union_ask_lang", lang, supported=", ".join(LANG_ORDER)),
            validator=_lang_code, error_key="edit_union_bad_lang", as_embed=True)
        if new is None:
            return
        db.set_union_lang(code, new)
        await channel.send(embed=_econ_embed(localized("edit_union_lang_done", lang,
                                                       language=language_name(new))))
    elif action == "created":
        union_row = db.get_union(code)
        moment = await _dialog_text(
            interaction, lang,
            localized("edit_union_ask_created", lang,
                      current=format_date(union_row["created_at"], lang)
                      if union_row and union_row["created_at"] else "—"),
            validator=_date_value, error_key="edit_union_bad_created", as_embed=True)
        if moment is None:
            return
        db.set_union_created(code, moment)
        await channel.send(embed=_econ_embed(localized("edit_union_created_done", lang,
                                                       date=format_date(moment, lang))))
