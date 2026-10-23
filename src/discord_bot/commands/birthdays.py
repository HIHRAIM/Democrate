"""Personal birthdays on Discord: a member's own date, and the server's
greeting channel and birthday role.

`/birthday set` is everyone's and works anywhere, private messages included; the
date is a person's, not a server's, and a linked Telegram account shares it.
The channel and the role are a server's, set by its Bot Admins and Server
Admins, and each takes an optional timezone that becomes the server's one
offset (db/community.py) — the same one the wiki birthday post uses.

Not this module's zone: the greetings, the role pass and the date rules
(discord_bot/celebrations.py, celebrations.py), and Telegram's `/birthday set`
(telegram_bot/commands/birthdays.py).
"""
import discord
from discord import ButtonStyle, app_commands, ui

import celebrations
import db
from utils import (
    DEFAULT_EMBED_COLOR, format_tz_offset, get_chat_lang, is_admin, localized, parse_tz_offset,
)

from discord_bot.celebrations import (
    OFF_WORDS, can_manage_role, target_channel, target_role,
)
from discord_bot.client import _chat_key, bot, is_server_admin
from discord_bot.dialogs import DIALOG_TIMEOUT, _ereply
from discord_bot.groups import birthday_group

def birthday_text(day, month, lang):
    """A stored birthday for people, e.g. '25.12'."""
    return localized("birthday_date", lang, date=f"{int(day):02d}.{int(month):02d}")

@birthday_group.command(name="set", description=localized("slash_birthday", "en"))
@app_commands.describe(day=localized("slash_birthday_day", "en"),
                       month=localized("slash_birthday_month", "en"),
                       clear=localized("slash_birthday_clear", "en"))
async def birthday_cmd(interaction: discord.Interaction, day: int = None, month: int = None,
                       clear: bool = False):
    """Set, show or clear the caller's birthday (day and month only)."""
    lang = get_chat_lang(_chat_key(interaction))
    uid = interaction.user.id
    if clear:
        key = "birthday_cleared" if db.clear_birthday("discord", uid) else "birthday_none"
        await _ereply(interaction, key, lang)
        return
    if day is None and month is None:
        current = db.get_birthday("discord", uid)
        if current is None:
            await _ereply(interaction, "birthday_none", lang)
        else:
            await _ereply(interaction, "birthday_current", lang,
                          date=birthday_text(current[0], current[1], lang))
        return
    try:
        day, month = celebrations.check_day_month(day, month)
    except ValueError:
        await _ereply(interaction, "birthday_bad_date", lang)
        return
    db.set_birthday("discord", uid, day, month,
                    display_name=interaction.user.global_name or interaction.user.name)
    await _ereply(interaction, "birthday_saved", lang, date=birthday_text(day, month, lang))

async def _require_birthday_admin(interaction, lang):
    """Bot Admins and Server Admins of a server bound to a union may set the
    birthday channel and role. Returns whether the caller may go on."""
    if interaction.guild is None:
        await _ereply(interaction, "guild_only", lang)
        return False
    if not (is_admin("discord", interaction.user.id) or is_server_admin(interaction)):
        await _ereply(interaction, "no_permission", lang)
        return False
    if not db.is_setup(interaction.guild_id):
        await _ereply(interaction, "chat_not_setup", lang)
        return False
    return True

def _tz_field(tz):
    """{'tz_offset': minutes} for a given timezone, {} for none; raises
    ValueError on a bad one."""
    return {} if tz is None else {"tz_offset": parse_tz_offset(tz)}

@birthday_group.command(name="channel", description=localized("slash_birthday_channel", "en"))
@app_commands.describe(channel=localized("slash_param_channel", "en"),
                       tz=localized("slash_param_tz", "en"))
async def birthday_channel_cmd(interaction: discord.Interaction, channel: str = None,
                               tz: str = None):
    """Choose the channel where members are congratulated on their birthday,
    or 'off' (Bot Admins and Server Admins)."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_birthday_admin(interaction, lang):
        return
    try:
        fields = _tz_field(tz)
    except ValueError:
        await _ereply(interaction, "setchannel_bad_tz", lang)
        return
    if channel and channel.strip().lower() in OFF_WORDS:
        db.set_community_fields("discord", interaction.guild_id, birthday_channel=None, **fields)
        await _ereply(interaction, "birthday_channel_off", lang, ephemeral=False)
        return
    target = target_channel(interaction, channel)
    if target is None:
        await _ereply(interaction, "setchannel_bad_channel", lang)
        return
    db.set_community_fields("discord", interaction.guild_id,
                            birthday_channel=str(target.id), **fields)
    await _ereply(interaction, "birthday_channel_set", lang, ephemeral=False,
                  channel=target.mention, time=f"{celebrations.GREETING_HOUR:02d}:00",
                  tz=format_tz_offset(db.community_tz("discord", interaction.guild_id)))

@birthday_group.command(name="role", description=localized("slash_birthday_role", "en"))
@app_commands.describe(role=localized("slash_param_role", "en"),
                       tz=localized("slash_param_tz", "en"))
async def birthday_role_cmd(interaction: discord.Interaction, role: str, tz: str = None):
    """Choose the role members hold through their birthday, or 'off' (Bot
    Admins and Server Admins). The role lasts from the earlier start to the
    later end of the birthday in UTC and in the server's timezone; the
    minute loop gives and takes it, so a change shows within a minute."""
    lang = get_chat_lang(_chat_key(interaction))
    if not await _require_birthday_admin(interaction, lang):
        return
    try:
        fields = _tz_field(tz)
    except ValueError:
        await _ereply(interaction, "setchannel_bad_tz", lang)
        return
    if role.strip().lower() in OFF_WORDS:
        db.set_community_fields("discord", interaction.guild_id, birthday_role=None, **fields)
        await _ereply(interaction, "birthday_role_off", lang, ephemeral=False)
    else:
        target = target_role(interaction, role)
        if target is None:
            await _ereply(interaction, "role_bad", lang)
            return
        db.set_community_fields("discord", interaction.guild_id,
                                birthday_role=str(target.id), **fields)
        key = "birthday_role_set" if can_manage_role(interaction.guild, target) \
            else "role_set_cannot_manage"
        await _ereply(interaction, key, lang, ephemeral=False, role=target.mention,
                      tz=format_tz_offset(db.community_tz("discord", interaction.guild_id)))

def _calendar_people(guild, scope, start, end):
    """(day, month, name) of the people whose birthday falls in the window:
    members of this server by their server name, or — for the whole bot —
    everyone, by the name stored with the birthday. A person the bot cannot
    name is left out rather than shown by id."""
    people = []
    for person in celebrations.birthday_people(celebrations.window_pairs(start, end)):
        uid = person["ids"].get("discord")
        if scope == "l":
            member = guild.get_member(int(uid)) if guild is not None and uid else None
            name = member.display_name if member is not None else None
        else:
            user = bot.get_user(int(uid)) if uid else None
            name = person["name"] or (user.display_name if user is not None else None)
        if name:
            people.append((person["day"], person["month"], name))
    return people

def _calendar_embed(guild, lang, scope, kinds, page):
    """One page of the calendar as an embed. Outside a server bound to a
    union there is no community to narrow to, so the scope is the whole
    bot."""
    chat = db.get_chat(guild.id) if guild is not None else None
    if scope == "l" and chat is None:
        scope = "g"
    start, end = celebrations.calendar_window(page)
    people = _calendar_people(guild, scope, start, end) if "person" in kinds else []
    events = celebrations.calendar_events(chat["union_code"] if scope == "l" else None,
                                          kinds, people, "discord")
    title, lines = celebrations.calendar_lines(events, page, lang)
    embed = discord.Embed(title=title, description="\n".join(lines)[:4096],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    if scope == "g":
        embed.set_footer(text=localized("calendar_scope_global", lang))
    return embed

class _CalendarView(ui.View):
    """The calendar's controls: back and forward a page, a switch for each
    kind of event and one between this community and the whole bot. Only
    the person who opened the calendar may use them."""

    def __init__(self, user_id, guild, lang, scope="l", kinds=None, page=0):
        """Build the buttons for the given state."""
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.user_id, self.guild, self.lang = user_id, guild, lang
        self.scope, self.kinds, self.page = scope, set(kinds or celebrations.CALENDAR_KINDS), page
        self._build()

    def _build(self):
        """(Re)create the buttons so their labels and colours show the
        state."""
        self.clear_items()
        self._add("◀", ButtonStyle.secondary, lambda: self._turn(-1))
        self._add("▶", ButtonStyle.secondary, lambda: self._turn(1))
        for kind in celebrations.CALENDAR_ORDER:
            on = kind in self.kinds
            self._add(localized(f"calendar_btn_{kind}", self.lang),
                      ButtonStyle.success if on else ButtonStyle.secondary,
                      lambda k=kind: self._toggle(k), row=1)
        self._add(localized("calendar_btn_global" if self.scope == "l" else "calendar_btn_local",
                            self.lang), ButtonStyle.primary, self._flip, row=2)

    def _add(self, label, style, action, row=0):
        """One button whose press runs `action` and redraws the calendar."""
        button = ui.Button(label=label, style=style, row=row)

        async def callback(interaction: discord.Interaction):
            """Refuse anyone but the opener, apply the change, redraw."""
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    localized("consent_not_yours", self.lang), ephemeral=True)
                return
            action()
            self._build()
            await interaction.response.edit_message(
                embed=_calendar_embed(self.guild, self.lang, self.scope, self.kinds, self.page),
                view=self)
        button.callback = callback
        self.add_item(button)

    def _turn(self, step):
        """Move a page back or forward."""
        self.page += step

    def _toggle(self, kind):
        """Show or hide one kind of event."""
        self.kinds ^= {kind}

    def _flip(self):
        """Switch between this community and the whole bot."""
        self.scope = "g" if self.scope == "l" else "l"

@bot.tree.command(name="calendar", description=localized("slash_calendar", "en"))
async def calendar_cmd(interaction: discord.Interaction):
    """Show the coming founding days of unions and wikis and members'
    birthdays: this community's for the coming month, with buttons to
    turn the page, hide a kind of event, or see the whole bot (everyone)."""
    lang = get_chat_lang(_chat_key(interaction))
    view = _CalendarView(interaction.user.id, interaction.guild, lang)
    await interaction.response.send_message(
        embed=_calendar_embed(interaction.guild, lang, view.scope, view.kinds, view.page),
        view=view)
