"""The interaction toolkit both the party and the economy dialogs are built
from: waiting for a user's next message, saying something back, reading a
logo, offering a numbered choice, and the three consent views.

A dialog here is a coroutine that asks a question and then blocks on
`_wait_message`, which is `bot.wait_for` with a predicate — so the Discord half
needs no handler of its own to run a conversation, unlike the Telegram half
where an unfiltered catchall has to feed the waiting future
(telegram_bot/dialogs.py). Every wait is bounded by DIALOG_TIMEOUT (30
minutes); an expired wait returns None and the caller says so.

The three consent views differ only in what they ask about — a party
leadership offer, a bank leadership offer, an enterprise offer or export — and
all three share the same shape: the buttons work for one named user, the
callbacks close over the pending decision, and the view dies with its timeout.
They are *not* persistent views: an offer that outlives a restart is meant to
be re-made rather than silently accepted later. The one persistent view in the
bot is the join request (discord_bot/commands/parties.py), which is re-armed
in client.py: setup_hook.

`_econ_embed` and `_ereply` are here rather than beside the economy commands
because `_dialog_text` needs them too — every economy dialog speaks in embeds,
and a dialog is what this module is.

Not this module's zone: any command, and the party card the edit menus render
(discord_bot/parties.py).
"""
import asyncio
import re

import discord
from discord import ui, ButtonStyle

import db
from utils import DEFAULT_EMBED_COLOR, is_admin, localized

from discord_bot.client import bot

USER_REF_RE = re.compile(r"^<@!?(\d+)>$|^(\d{5,25})$")

DIALOG_TIMEOUT = 30 * 60

async def _wait_message(channel_id, user_id, timeout=DIALOG_TIMEOUT):
    """Wait up to 30 minutes for the next message of `user_id` in `channel_id`."""
    def check(m):
        """Accept only the awaited person's next message in this very channel."""
        return m.author.id == user_id and m.channel.id == channel_id
    try:
        return await bot.wait_for("message", check=check, timeout=timeout)
    except asyncio.TimeoutError:
        return None

async def _say(interaction: discord.Interaction, content=None, **kwargs):
    """First reply goes through the interaction, later ones to the channel.

    `ephemeral` only exists on the interaction response, so it is dropped once
    we fall back to a normal channel message (e.g. after a numbered-choice
    dialog has already consumed the interaction response)."""
    if not interaction.response.is_done():
        await interaction.response.send_message(content, **kwargs)
        try:
            return await interaction.original_response()
        except Exception:
            return None
    kwargs.pop("ephemeral", None)
    return await interaction.channel.send(content, **kwargs)

def _econ_embed(desc, title=None, color=DEFAULT_EMBED_COLOR):
    """Wrap a line of text in the economy's standard embed.

    The economy speaks entirely in embeds, in one colour, so that a wall of money
    messages reads as one system. Used by `_ereply` and directly wherever a
    message goes to the channel rather than through an interaction."""
    embed = discord.Embed(description=desc, color=discord.Color(color))
    if title:
        embed.title = title
    return embed

async def _ereply(interaction, key, lang, *, ephemeral=True, title=None, **kw):
    """Reply to an interaction with a localized economy embed.

    The workhorse of every economy command: `key` is a localization key and the
    keyword arguments are its parameters. Ephemeral by default — a refusal
    concerns only the person who asked — and explicitly not ephemeral for the
    outcomes other people in the chat should see."""
    await _say(interaction, embed=_econ_embed(localized(key, lang, **kw), title=title),
               ephemeral=ephemeral)

async def _dialog_text(interaction, lang, prompt, *, validator=None, error_key=None,
                       attempts=5, as_embed=False):
    """Ask `prompt` and wait for the caller's text answer, re-asking on invalid
    input. Returns the validated value (or the message when no validator) or
    None on timeout/attempts exhausted. With as_embed=True every dialog message
    is wrapped in an embed (the economy commands use this)."""
    def _wrap(text):
        """Send a dialog line as an embed or as plain text, depending on the
        dialog."""
        return {"embed": _econ_embed(text)} if as_embed else {"content": text}
    await _say(interaction, **_wrap(prompt))
    for _ in range(attempts):
        msg = await _wait_message(interaction.channel_id, interaction.user.id)
        if msg is None:
            await interaction.channel.send(**_wrap(localized("dialog_timeout", lang)))
            return None
        value = (msg.content or "").strip()
        if validator is None:
            return msg
        ok = validator(value)
        if ok is not None:
            return ok
        await interaction.channel.send(**_wrap(localized(error_key, lang)))
    await interaction.channel.send(**_wrap(localized("dialog_timeout", lang)))
    return None

_LOGO_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

async def _extract_logo(msg):
    """Return (bytes, mime) if the message carries an image attachment."""
    for att in msg.attachments:
        ctype = (att.content_type or "").split(";")[0].strip().lower()
        if ctype.startswith("image/") and att.size <= 8 * 1024 * 1024:
            try:
                return await att.read(), ctype
            except Exception:
                return None
    return None

async def _dialog_logo(interaction, lang, prompt, attempts=5):
    """Ask for a logo and wait for an image attachment, re-asking on anything
    else.

    Returns (bytes, mime) or None on timeout or after `attempts` failures. There is
    no way to skip from inside: the caller decides whether a logo is optional by
    what it does with None."""
    await _say(interaction, prompt)
    for _ in range(attempts):
        msg = await _wait_message(interaction.channel_id, interaction.user.id)
        if msg is None:
            await interaction.channel.send(localized("dialog_timeout", lang))
            return None
        logo = await _extract_logo(msg)
        if logo:
            return logo
        await interaction.channel.send(localized("add_party_invalid_logo", lang))
    await interaction.channel.send(localized("dialog_timeout", lang))
    return None

def _parse_user_ref(text):
    """A user id out of a ping or a raw id, or None.

    The cheap resolver, used everywhere a command takes a user argument. It never
    touches the network, unlike commands/admins.py: _resolve_user_ref, which also
    accepts a username and has to search the guild for it."""
    m = USER_REF_RE.match((text or "").strip())
    if not m:
        return None
    return int(m.group(1) or m.group(2))

async def _numbered_choice(interaction, lang, header, items, render_line):
    """Send `header` + a numbered list and wait 30 minutes for the caller to
    reply with a number. Returns the chosen item or None."""
    lines = [header] + [f"{i + 1}. {render_line(it)}" for i, it in enumerate(items)]
    await _say(interaction, "\n".join(lines))
    msg = await _wait_message(interaction.channel_id, interaction.user.id)
    if msg is None:
        return None
    value = (msg.content or "").strip().rstrip(".")
    if value.isdigit() and 1 <= int(value) <= len(items):
        return items[int(value) - 1]
    await interaction.channel.send(localized("choice_invalid", lang))
    return None

class _ChoiceSelect(ui.View):
    """A dropdown only one person may use, resolving with the value behind the
    option they picked.

    Exists because a numbered choice typed into the chat is not always
    available: a command marked `allowed_installs(users=True)` can be run where
    the bot is installed on the account rather than in the server, and there it
    receives the interaction but no messages at all — so `_wait_message` waits
    out its half hour in silence and the dialog looks dead. A component answer
    comes back as an interaction and therefore works everywhere the command
    itself does. Callers still race it against a typed reply, so nobody loses
    the old way of answering."""

    def __init__(self, user_id, lang, options, placeholder=None, timeout=DIALOG_TIMEOUT):
        """Build one dropdown from (label, description, value) triples. Discord
        allows 25 options, a 100-character label and a 100-character
        description, so all three are cut to fit rather than raising."""
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.lang = lang
        self.value = None
        self._event = asyncio.Event()
        select = ui.Select(
            placeholder=(placeholder or "")[:150] or None,
            options=[discord.SelectOption(label=str(label)[:100],
                                          description=(desc or None) and str(desc)[:100],
                                          value=str(value))
                     for label, desc, value in options[:25]])
        select.callback = self._make_cb(select)
        self.add_item(select)

    def _make_cb(self, select):
        """The dropdown's callback, closing over the select so it can read what
        was picked."""
        async def cb(interaction2: discord.Interaction):
            """Record the picked value, wake whoever is waiting, and stop the
            view — refusing anyone but the person it was sent to."""
            if interaction2.user.id != self.user_id:
                await interaction2.response.send_message(
                    localized("consent_not_yours", self.lang), ephemeral=True)
                return
            self.value = select.values[0] if select.values else None
            try:
                await interaction2.response.defer()
            except Exception:
                pass
            self._event.set()
            self.stop()
        return cb

    async def wait_choice(self):
        """Wait for a pick and return the value behind it, or None on
        timeout."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            pass
        return self.value

    async def wait_value(self):
        """The picked value, without a deadline of its own, so the caller can
        race it against something else."""
        await self._event.wait()
        return self.value

async def _wait_message_or_view(channel_id, user_id, view, stop_event=None,
                                timeout=DIALOG_TIMEOUT):
    """Race a typed answer against a component answer, and optionally against a
    stop signal. Returns ('message', msg), ('choice', value), ('stop', None) or
    ('timeout', None).

    The shape every dialog that can be answered two ways needs: the component is
    what works where the bot cannot read messages, the typed reply is what
    people are used to, and whichever arrives first wins."""
    def check(m):
        """Accept only the awaited person's next message in this very channel."""
        return m.author.id == user_id and m.channel.id == channel_id

    tasks = {
        "message": asyncio.ensure_future(bot.wait_for("message", check=check)),
        "choice": asyncio.ensure_future(view.wait_value()),
    }
    if stop_event is not None:
        tasks["stop"] = asyncio.ensure_future(stop_event.wait())
    done, pending = await asyncio.wait(set(tasks.values()), timeout=timeout,
                                       return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for kind in ("stop", "choice", "message"):
        task = tasks.get(kind)
        if task is None or task not in done:
            continue
        if kind == "stop":
            return "stop", None
        try:
            return kind, task.result()
        except Exception:
            return "timeout", None
    return "timeout", None

class _QuizButtons(ui.View):
    """A row of buttons only one person may press; `wait_click` resolves with the
    pressed button's value (or None on timeout), `wait_value` waits without a
    deadline of its own so the caller can race it against something else. Used by
    the quizzes and by the Olympiad dialogs."""

    def __init__(self, user_id, lang, buttons, timeout=DIALOG_TIMEOUT):
        """Build one button per (label, style, value) triple and arm them all
        with the same callback factory."""
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.lang = lang
        self.value = None
        self._event = asyncio.Event()
        for label, style, value in buttons:
            b = ui.Button(label=label, style=style)
            b.callback = self._make_cb(value)
            self.add_item(b)

    def _make_cb(self, value):
        """One button's callback, closing over the value it stands for."""
        async def cb(interaction2: discord.Interaction):
            """Record the pressed value, wake whoever is waiting, and stop the
            view — refusing anyone but the person the buttons were sent to."""
            if interaction2.user.id != self.user_id:
                await interaction2.response.send_message(
                    localized("consent_not_yours", self.lang), ephemeral=True)
                return
            self.value = value
            try:
                await interaction2.response.defer()
            except Exception:
                pass
            self._event.set()
            self.stop()
        return cb

    async def wait_click(self):
        """Wait for a button and return the value behind it, or None on
        timeout."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            pass
        return self.value

    async def wait_value(self):
        """The value of the button pressed so far, without waiting."""
        await self._event.wait()
        return self.value

class _ConsentView(ui.View):
    """«Принимаю» / «Не принимаю» buttons that only the target user may press."""

    def __init__(self, lang, target_id, on_accept, on_decline):
        """Build the two buttons and bind them to the caller's callbacks.

        The view stops itself after DIALOG_TIMEOUT, at which point the buttons simply
        stop answering: an offer that outlived its half hour is meant to be made again
        rather than accepted late."""
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.lang = lang
        self.target_id = target_id
        self._on_accept = on_accept
        self._on_decline = on_decline

        accept = ui.Button(label=localized("consent_accept", lang), style=ButtonStyle.success)
        decline = ui.Button(label=localized("consent_decline", lang), style=ButtonStyle.danger)
        accept.callback = self._make_callback(True)
        decline.callback = self._make_callback(False)
        self.add_item(accept)
        self.add_item(decline)

    def _make_callback(self, accepted):
        """One button's callback, closing over whether it means yes."""
        async def callback(interaction2: discord.Interaction):
            """Answer the press: refuse anyone but the named person, disable both
            buttons so the decision cannot be taken twice, then run the outcome."""
            if interaction2.user.id != self.target_id:
                await interaction2.response.send_message(
                    localized("consent_not_yours", self.lang), ephemeral=True)
                return
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await interaction2.response.edit_message(view=self)
            except Exception:
                pass
            if accepted:
                await self._on_accept(interaction2)
            else:
                await self._on_decline(interaction2)
        return callback

class _LeaderConsentView(ui.View):
    """Accept/decline buttons that any leader of `bank_code` (or a Bot Admin) may
    press — used for treaty and peg agreements between two banks."""

    def __init__(self, lang, bank_code, on_accept, on_decline):
        """Build the two buttons for a decision that belongs to a bank rather
        than to one person."""
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.lang = lang
        self.bank_code = bank_code
        self._on_accept = on_accept
        self._on_decline = on_decline
        accept = ui.Button(label=localized("consent_accept", lang), style=ButtonStyle.success)
        decline = ui.Button(label=localized("consent_decline", lang), style=ButtonStyle.danger)
        accept.callback = self._make_cb(True)
        decline.callback = self._make_cb(False)
        self.add_item(accept)
        self.add_item(decline)

    def _make_cb(self, accepted):
        """One button's callback, closing over whether it means yes."""
        async def callback(interaction2: discord.Interaction):
            """Answer the press: refuse anyone who does not lead this bank (Bot
            Admins aside), disable both buttons, then run the outcome."""
            if not (db.is_bank_leader(self.bank_code, "discord", interaction2.user.id)
                    or is_admin("discord", interaction2.user.id)):
                await interaction2.response.send_message(
                    localized("bank_consent_not_leader", self.lang), ephemeral=True)
                return
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await interaction2.response.edit_message(view=self)
            except Exception:
                pass
            await (self._on_accept if accepted else self._on_decline)(interaction2)
        return callback

class _EntConsentView(ui.View):
    """Accept/decline buttons that any leader of `ent_code` (or a Bot Admin) may
    press — join requests and priced export offers."""

    def __init__(self, lang, ent_code, on_accept, on_decline):
        """Build the two buttons for a decision that belongs to an enterprise
        rather than to one person."""
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.lang = lang
        self.ent_code = ent_code
        self._on_accept = on_accept
        self._on_decline = on_decline
        accept = ui.Button(label=localized("consent_accept", lang), style=ButtonStyle.success)
        decline = ui.Button(label=localized("consent_decline", lang), style=ButtonStyle.danger)
        accept.callback = self._make_cb(True)
        decline.callback = self._make_cb(False)
        self.add_item(accept)
        self.add_item(decline)

    def _make_cb(self, accepted):
        """One button's callback, closing over whether it means yes."""
        async def callback(interaction2: discord.Interaction):
            """Answer the press: refuse anyone who does not lead this enterprise (Bot
            Admins aside), disable both buttons, then run the outcome."""
            if not (db.is_enterprise_leader(self.ent_code, "discord", interaction2.user.id)
                    or is_admin("discord", interaction2.user.id)):
                await interaction2.response.send_message(
                    localized("ent_consent_not_leader", self.lang), ephemeral=True)
                return
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await interaction2.response.edit_message(view=self)
            except Exception:
                pass
            await (self._on_accept if accepted else self._on_decline)(interaction2)
        return callback
