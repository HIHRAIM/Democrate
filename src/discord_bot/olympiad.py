"""The Discord half of the Olympiad: the commands that appear only while one
is running, the voting dialog, and the review the votes go through.

`/setolympiad` opens the event and is the only Olympiad command that always
exists; the five below it are added to the command tree when an Olympiad is
running and taken off it again when the voting period ends, so people never see
a command they cannot use. They are declared with `@app_commands.command` and
collected in OLYMPIAD_COMMANDS for exactly that reason — turning one into a
`@bot.tree.command` would drop it out of the mechanism. Discord distributes a
tree change to clients on its own schedule, which is why `/setolympiad` says
the list may take a while.

The review and the accepted-vote chats always live on Discord, so the embeds
and the two reviewer commands are here even for votes cast on Telegram; the
Telegram bot calls post_olympiad_vote for its own voters.

The dialogs themselves (`/setcontest`, `/editcontest` and the vote) live in
olympiad.py and are shared with the Telegram half; what stays here is the
permission gate and the Discord dialog object that drives them. Voting happens
only in a private chat with the bot, and every question carries a Stop button.

Not this module's zone: the event's rules, its dates and the dialogs
(olympiad.py), and the rows (db/olympiad.py).
"""
import asyncio

import discord
from discord import app_commands, ButtonStyle

import db
import olympiad
from utils import (
    DEFAULT_EMBED_COLOR, DEFAULT_LANG, SUPPORTED_LANGS, available_locales,
    get_chat_lang, is_admin, language_name, localized, set_chat_lang,
)

from discord_bot.client import _chat_key, bot, logger
from discord_bot.dialogs import DIALOG_TIMEOUT, _QuizButtons

def olympiad_help_keys():
    """The Olympiad help lines to show right now."""
    if not olympiad.is_open():
        return ["help_setolympiad"]
    return ["help_setolympiad", "help_setcontest", "help_editcontest",
            "help_olympiad", "help_accepted", "help_denied"]

def _apply_olympiad_commands():
    """Attach the gated commands to the tree, or take them off it, to match
    whether an Olympiad is running. Returns True when the tree actually
    changed — only then is a sync with Discord worth its rate limit."""
    want = olympiad.is_open()
    have = bot.tree.get_command("olympiad") is not None
    if want == have:
        return False
    for cmd in OLYMPIAD_COMMANDS:
        if want:
            bot.tree.add_command(cmd, override=True)
        else:
            bot.tree.remove_command(cmd.name)
    return True

async def sync_olympiad_commands():
    """Bring the published command list in line with the Olympiad's state."""
    if not _apply_olympiad_commands():
        return
    try:
        await bot.tree.sync()
    except Exception as e:
        logger.warning("Olympiad command sync failed: %s", e)

def _oly_lang(interaction):
    """The language an Olympiad message should use in this chat."""
    return get_chat_lang(_chat_key(interaction))

def _review_lang(contest):
    """The language the review chat is addressed in: the contest's own, or the
    default one for a cross-language contest."""
    return contest["lang"] if contest["lang"] in SUPPORTED_LANGS else DEFAULT_LANG

async def _olympiad_channel(chat_id):
    """The Discord channel a contest posts to, or None when it is unreachable."""
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return None
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except Exception:
            channel = None
    return channel

async def _dm_channel(interaction: discord.Interaction):
    """The caller's private chat with the bot, opening it if needed, or None
    when they do not accept DMs — which is where the whole voting dialog and every
    verdict notice go."""
    channel = interaction.channel
    if channel is None:
        channel = interaction.user.dm_channel or await interaction.user.create_dm()
    return channel

async def _wait_reply_or_button(channel_id, user_id, view, timeout=DIALOG_TIMEOUT):
    """Race the caller's next message against a press on `view`. Returns
    ('text', message), ('button', value) or ('timeout', None)."""
    def check(m):
        """Accept the awaited person's next message in their private chat with
        the bot."""
        return m.author.id == user_id and m.channel.id == channel_id
    msg_task = asyncio.ensure_future(bot.wait_for("message", check=check))
    btn_task = asyncio.ensure_future(view.wait_value())
    done, pending = await asyncio.wait({msg_task, btn_task}, timeout=timeout,
                                       return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if btn_task in done:
        return "button", btn_task.result()
    if msg_task in done:
        try:
            return "text", msg_task.result()
        except Exception:
            return "timeout", None
    return "timeout", None

class _OlyDialog:
    """One Olympiad conversation on Discord.

    Every question carries a Stop button, so the person can walk away from the
    dialog at any point; `ask` then returns None, exactly as it does when they
    simply stop replying. Extra buttons ("Another way", "One point each") ride
    on the same row and come back as ('button', value)."""

    def __init__(self, interaction, lang, channel):
        """Hold the interaction, the language and the channel every step
        answers in."""
        self.interaction = interaction
        self.lang = lang
        self.channel = channel
        self.user_id = interaction.user.id
        self.author = str(interaction.user)

    async def send(self, text, view=None):
        """A dialog message, through the interaction the first time and into the
        channel after that. `view` is left out of the call entirely when there is
        none: `InteractionResponse.send_message` tests it against its own MISSING
        sentinel, so an explicit view=None gets as far as posting the message and
        then raises on it.

        Link previews are suppressed on every message of the dialog. A candidate
        list is a wiki url per line (olympiad.py: candidate_label), and Discord
        would answer a list of three with three embeds — burying the question
        under the answers to it."""
        kwargs = {"view": view} if view is not None else {}
        text = text[:1990]
        if not self.interaction.response.is_done():
            await self.interaction.response.send_message(text, suppress_embeds=True,
                                                         **kwargs)
            try:
                return await self.interaction.original_response()
            except Exception:
                return None
        return await self.channel.send(text, suppress_embeds=True, **kwargs)

    async def ask(self, prompt, *, validator=None, error_text=None, attempts=5,
                  buttons=()):
        """Ask `prompt` and wait for an answer. Returns ('text', value),
        ('button', value), or None when stopped or timed out."""
        pending = prompt
        for _ in range(attempts):
            rows = [(label, ButtonStyle.secondary, ("button", value))
                    for label, value in buttons]
            rows.append((olympiad.text("stop_button", self.lang), ButtonStyle.danger,
                         ("stop", None)))
            view = _QuizButtons(self.user_id, self.lang, rows)
            msg = await self.send(pending, view=view)
            kind, payload = await _wait_reply_or_button(self.channel.id, self.user_id, view)
            view.stop()
            if msg is not None:
                try:
                    await msg.edit(view=None)
                except Exception:
                    pass
            if kind == "button":
                action, value = payload
                if action == "stop":
                    await self.send(olympiad.text("dialog_stopped", self.lang))
                    return None
                return "button", value
            if kind != "text":
                await self.send(localized("dialog_timeout", self.lang))
                return None
            value = (payload.content or "").strip()
            if validator is None:
                return "text", value
            ok = validator(value)
            if ok is not None:
                return "text", ok
            pending = error_text or localized("choice_invalid", self.lang)
        await self.send(localized("dialog_timeout", self.lang))
        return None

    async def choose(self, header, items, render, *, extra=None):
        """A numbered list. Returns the chosen item, the sentinel '__extra__'
        when the trailing extra option is picked, or None."""
        lines = [header] + [f"{i + 1}. {render(it)}" for i, it in enumerate(items)]
        if extra:
            lines.append(f"{len(items) + 1}. {extra}")
        total = len(items) + (1 if extra else 0)

        def _validator(value):
            """Accept only a language code the Olympiad content exists in."""
            v = value.strip().rstrip(".")
            return int(v) if v.isdigit() and 1 <= int(v) <= total else None

        answer = await self.ask("\n".join(lines), validator=_validator,
                                error_text=localized("choice_invalid", self.lang))
        if not answer:
            return None
        number = answer[1]
        if extra and number == total:
            return "__extra__"
        return items[number - 1]

    async def choose_numbers(self, header, items, render, limit):
        """A numbered list several entries may be picked from at once. Returns
        the chosen positions in the order given, or None."""
        lines = [header, ""] + [f"{i + 1}. {render(it)}" for i, it in enumerate(items)]
        answer = await self.ask(
            "\n".join(lines),
            validator=lambda v: olympiad.parse_numbers(v, len(items), limit),
            error_text=olympiad.text("candidates_invalid", self.lang, max=limit))
        return answer[1] if answer else None

def _vote_footer(vote):
    """The footer line of a review embed: who voted, their messenger id, and
    the vote's key — the key being what a reviewer types into `/accepted` and
    `/denied`."""
    platform = "Discord" if vote["platform"] == "discord" else "Telegram"
    return f"{vote['username']} │ {platform} ID: {vote['user_id']} │ {vote['key']}"

def _review_embed(vote, contest):
    """Contest name as the title, the supported wikis as the subtitle, then the
    person's account confirmation, their activity claim and the vote itself.
    The footer carries who they are and the key the reviewer answers with."""
    lang = _review_lang(contest)
    title = olympiad.contest_name(contest, lang)
    if vote["is_update"]:
        title = f"{title} — {olympiad.text('review_updated_mark', lang)}"

    parts = [f"**{olympiad.vote_candidate_names(vote)}**", ""]
    if vote["account_known"]:
        parts.append(olympiad.text("review_account_verified", lang,
                                   name=vote["account_text"]))
    else:
        parts.append(olympiad.text("review_account_claim", lang,
                                   text=vote["account_text"]))
    parts.append(olympiad.text("review_activity", lang, text=vote["activity_text"]))
    parts.append("")
    parts.append(olympiad.vote_body(vote, vote["lang"] or lang))

    embed = discord.Embed(title=title[:256], description="\n".join(parts)[:4096],
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    embed.set_footer(text=_vote_footer(vote)[:2048])
    return embed

def _approved_embed(vote, contest, wiki_name):
    """What lands in the accepted-votes chat: who voted, under both their
    messenger name and the wiki name the reviewer confirmed, the wikis they
    supported as the subtitle, and their vote."""
    lang = _review_lang(contest)
    title = f"{vote['username']} — {wiki_name}"
    if vote["is_update"]:
        title = f"{title} ({olympiad.text('review_updated_mark', lang)})"
    parts = [f"**{olympiad.vote_candidate_names(vote)}**", "",
             olympiad.vote_body(vote, vote["lang"] or lang)]
    return discord.Embed(title=title[:256],
                         description="\n".join(parts)[:4096],
                         color=discord.Color(DEFAULT_EMBED_COLOR))

async def post_olympiad_vote(key):
    """Send a stored vote to its contest's review chat. Both bots use this — the
    review chats are always on Discord. Returns True when it was delivered."""
    vote = db.get_vote(key)
    if not vote:
        return False
    contest = db.get_contest(vote["contest_id"])
    if not contest:
        return False
    channel = await _olympiad_channel(contest["review_chat"])
    if channel is None:
        return False
    try:
        await channel.send(embed=_review_embed(vote, contest))
        return True
    except Exception as e:
        logger.warning("Olympiad vote %s could not be posted for review: %s", key, e)
        return False

async def dm_olympiad_voter(vote, title, body):
    """Deliver a reviewer's decision to the voter, on whichever messenger they
    voted from."""
    if vote["platform"] == "discord":
        try:
            user = await bot.fetch_user(int(vote["user_id"]))
            await user.send(embed=discord.Embed(title=title, description=body,
                                                color=discord.Color(DEFAULT_EMBED_COLOR)))
            return True
        except Exception:
            return False
    try:
        from telegram_bot import bot as tg_bot
        await tg_bot.send_message(int(vote["user_id"]), f"{title}\n\n{body}")
        return True
    except Exception:
        return False

@bot.tree.command(name="setolympiad", description="open the Olympiad and its voting period (bot admins)")
@app_commands.describe(start="First day of voting, MM-DD-YYYY — or 'off' to cancel the Olympiad",
                       end="Last day of voting, MM-DD-YYYY")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def setolympiad_cmd(interaction: discord.Interaction, start: str, end: str = None):
    """Open the Olympiad and fix its voting period, or cancel it with 'off'
    (Bot Admins).

    Opening adds the five runtime commands to the command tree and closing takes
    them off again, which Discord distributes on its own schedule — hence the
    warning that the list may take a while. Cancelling also deletes every contest,
    candidate and vote."""
    lang = _oly_lang(interaction)
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    if (start or "").strip().lower() in ("off", "stop", "cancel"):
        if not db.get_olympiad():
            await interaction.response.send_message(
                olympiad.text("setolympiad_off_none", lang), ephemeral=True)
            return
        db.clear_olympiad_data()
        db.clear_olympiad()
        await interaction.response.send_message(olympiad.text("setolympiad_off", lang))
        await sync_olympiad_commands()
        return

    if not end or not end.strip():
        await interaction.response.send_message(
            olympiad.text("setolympiad_usage", lang), ephemeral=True)
        return
    try:
        start_ts, end_ts = olympiad.parse_period(start, end)
    except ValueError as e:
        key = "setolympiad_bad_order" if str(e) == "bad_order" else "setolympiad_bad_date"
        await interaction.response.send_message(olympiad.text(key, lang), ephemeral=True)
        return

    existed = db.get_olympiad() is not None
    db.set_olympiad(start_ts, end_ts)
    await interaction.response.send_message(olympiad.text(
        "setolympiad_updated" if existed else "setolympiad_set", lang,
        start=olympiad.format_date(start_ts), end=olympiad.format_date(end_ts)))
    await sync_olympiad_commands()

async def _olympiad_admin_gate(interaction, lang):
    """The two setup commands are for Bot Admins, for as long as the Olympiad
    runs. Returns the dialog channel, or None when the caller may not proceed."""
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return None
    if not olympiad.is_open():
        await interaction.response.send_message(
            olympiad.text("not_active", lang), ephemeral=True)
        return None
    return await _dm_channel(interaction)

@app_commands.command(name="setcontest", description="create a contest of the Olympiad (bot admins)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def setcontest_cmd(interaction: discord.Interaction):
    """Create a contest of the Olympiad through a dialog (Bot Admins).

    The dialog itself lives in olympiad.py and is shared with the Telegram half;
    what is here is the permission gate and the Discord dialog object."""
    lang = _oly_lang(interaction)
    channel = await _olympiad_admin_gate(interaction, lang)
    if channel is None:
        return
    await olympiad.run_setcontest(_OlyDialog(interaction, lang, channel), lang)

@app_commands.command(name="editcontest", description="manage a contest's candidate wikis (bot admins)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def editcontest_cmd(interaction: discord.Interaction):
    """Manage a contest's candidate wikis through a dialog (Bot Admins)."""
    lang = _oly_lang(interaction)
    channel = await _olympiad_admin_gate(interaction, lang)
    if channel is None:
        return
    await olympiad.run_editcontest(_OlyDialog(interaction, lang, channel), lang)

async def _ask_dm_language(dialog, interaction):
    """Step 1: with no language chosen for this private chat yet, ask in English
    and offer a button per localization. The answer is remembered for the chat
    (and dropped again after a year). Returns the language code, or None."""
    rows = [(language_name(code), ButtonStyle.primary, code)
            for code in available_locales()]
    view = _QuizButtons(interaction.user.id, DEFAULT_LANG, rows)
    msg = await dialog.send(olympiad.text("ask_lang", DEFAULT_LANG), view=view)
    code = await view.wait_click()
    if msg is not None:
        try:
            await msg.edit(view=None)
        except Exception:
            pass
    if not code:
        return None
    set_chat_lang(_chat_key(interaction), code, is_dm=True)
    dialog.lang = code
    await dialog.send(olympiad.text("lang_chosen", code))
    return code

@app_commands.command(name="olympiad", description="vote in the Olympiad (private chat with the bot only)")
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def olympiad_cmd(interaction: discord.Interaction):
    """Vote in the Olympiad — private chat with the bot only.

    Asks in order: the language of this chat, the voter's wiki account (skipped
    when already known), a wiki they are active on, the contest, up to three
    candidates, and the vote itself. Every question carries a Stop button. The
    finished vote goes to the contest's review chat, which is always on Discord."""
    lang = _oly_lang(interaction)
    if interaction.guild is not None:
        await interaction.response.send_message(
            olympiad.text("dm_only", lang), ephemeral=True)
        return
    period = olympiad.period()
    if not period:
        await interaction.response.send_message(
            olympiad.text("not_active", lang), ephemeral=True)
        return
    if not olympiad.is_voting_open():
        await interaction.response.send_message(olympiad.text(
            "voting_not_started", lang, start=olympiad.format_date(period[0])),
            ephemeral=True)
        return

    channel = await _dm_channel(interaction)
    dialog = _OlyDialog(interaction, lang, channel)
    if db.get_chat_lang(_chat_key(interaction)) is None:
        code = await _ask_dm_language(dialog, interaction)
        if code is None:
            return
        lang = code
    await olympiad.run_vote(dialog, lang, "discord", interaction.user.id,
                            str(interaction.user), "Discord", post_olympiad_vote)

async def _resolve_review(interaction, lang, key, usage_key, not_found_key):
    """The vote a reviewer named, once it is clear the command was run in the
    chat that reviews it — the key alone is not enough to decide a vote from
    somewhere else. Returns (vote, contest) or (None, None)."""
    key = (key or "").strip()
    if not key:
        await interaction.response.send_message(
            olympiad.text(usage_key, lang), ephemeral=True)
        return None, None
    vote = db.get_vote(key)
    contest = db.get_contest(vote["contest_id"]) if vote else None
    if not vote or not contest:
        await interaction.response.send_message(
            olympiad.text(not_found_key, lang, key=key), ephemeral=True)
        return None, None
    if str(interaction.channel_id) != str(contest["review_chat"]):
        await interaction.response.send_message(
            olympiad.text("accepted_wrong_chat", lang), ephemeral=True)
        return None, None
    return vote, contest

@app_commands.command(name="accepted", description="accept a vote under review")
@app_commands.describe(key="The vote's key, from the embed's footer",
                       nickname="The voter's username on the wiki host",
                       remember="Type 'remember' to link that account to this user")
async def accepted_cmd(interaction: discord.Interaction, key: str, nickname: str,
                       remember: str = None):
    """Accept a vote under review by its key.

    Publishes it to the accepted-votes chat under the voter's messenger name and
    the wiki name given here, and tells the voter by DM. With 'remember' the wiki
    account is stored so that person is never asked again — one of only two things
    that outlive an Olympiad."""
    lang = _oly_lang(interaction)
    vote, contest = await _resolve_review(interaction, lang, key, "accepted_usage",
                                          "accepted_not_found")
    if not vote:
        return
    nickname = (nickname or "").strip()
    if not nickname:
        await interaction.response.send_message(
            olympiad.text("accepted_usage", lang), ephemeral=True)
        return

    approved_chat = db.approved_chat_for(contest, vote["lang"])
    channel = await _olympiad_channel(approved_chat)
    if channel is None:
        await interaction.response.send_message(
            olympiad.text("accepted_no_channel", lang), ephemeral=True)
        return
    try:
        await channel.send(embed=_approved_embed(vote, contest, nickname))
    except Exception as e:
        logger.warning("Accepted vote %s could not be posted: %s", vote["key"], e)
        await interaction.response.send_message(
            olympiad.text("accepted_no_channel", lang), ephemeral=True)
        return

    lines = [olympiad.text("accepted_done", lang, key=vote["key"])]
    if (remember or "").strip().lower() == "remember":
        db.set_wiki_account(vote["platform"], vote["user_id"], nickname,
                            str(interaction.user))
        lines.append(olympiad.text("accepted_remembered", lang, name=nickname))

    voter_lang = vote["lang"] or DEFAULT_LANG
    await dm_olympiad_voter(
        vote, olympiad.text("accepted_dm_title", voter_lang),
        olympiad.text("accepted_dm_body", voter_lang,
                      contest=olympiad.contest_name(contest, voter_lang),
                      wikis=olympiad.vote_candidate_names(vote)))
    db.delete_vote(vote["key"])
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@app_commands.command(name="denied", description="reject a vote under review")
@app_commands.describe(key="The vote's key, from the embed's footer",
                       reason="Why the vote is rejected — the voter is told")
async def denied_cmd(interaction: discord.Interaction, key: str, reason: str):
    """Reject a vote under review by its key, with a reason the voter is
    told."""
    lang = _oly_lang(interaction)
    vote, contest = await _resolve_review(interaction, lang, key, "denied_usage",
                                          "denied_not_found")
    if not vote:
        return
    reason = (reason or "").strip()
    if not reason:
        await interaction.response.send_message(
            olympiad.text("denied_usage", lang), ephemeral=True)
        return

    voter_lang = vote["lang"] or DEFAULT_LANG
    await dm_olympiad_voter(
        vote, olympiad.text("denied_dm_title", voter_lang),
        olympiad.text("denied_dm_body", voter_lang,
                      contest=olympiad.contest_name(contest, voter_lang),
                      wikis=olympiad.vote_candidate_names(vote), reason=reason))
    db.delete_vote(vote["key"])
    await interaction.response.send_message(
        olympiad.text("denied_done", lang, key=vote["key"]), ephemeral=True)

OLYMPIAD_COMMANDS = [setcontest_cmd, editcontest_cmd, olympiad_cmd,
                     accepted_cmd, denied_cmd]
