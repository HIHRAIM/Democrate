"""What an ordinary member runs about themselves: `/help`, `/verify`,
`/add-telegram` and `/privacy`.

HELP_SECTIONS is a hand-written list of localization keys, not something
derived from the command tree, and that is the trap: a command moved between
modules keeps working while quietly disappearing from the help. Adding a
command means adding its key here and its six i18n entries. The Olympiad page
is the exception — it is built from olympiad_help_keys(), which lists only
`/setolympiad` while no Olympiad is running, because the rest of its commands
are not registered then.

`/verify` is a single Fandom lookup at one moment, not a subscription;
`/add-telegram` is one half of a thirty-minute handshake whose other half is
`/add-discord` on Telegram. `/privacy` is where a person deletes what the bot
keeps about them.

Not this module's zone: the Fandom request itself (fandom.py), the identity
model (db/users.py) and the quiz data those privacy actions delete
(db/quizzes.py).
"""
import discord
from discord import app_commands, ui, ButtonStyle

import db
import fandom
import olympiad
import quizzes
from utils import (
    DEFAULT_EMBED_COLOR, get_chat_lang, is_verified, localized, localized_help,
    privacy_actions, privacy_option_labels, rate_limit_ok,
)

from discord_bot.client import _chat_key, bot
from discord_bot.dialogs import DIALOG_TIMEOUT, _numbered_choice, _wait_message
from discord_bot.olympiad import olympiad_help_keys

HELP_SECTIONS = [
    ("section_everyone", [
        "cmd_verify", "cmd_add_telegram", "cmd_party", "cmd_govt", "cmd_add_party",
        "cmd_edit_party", "cmd_edit_rules", "cmd_party_join", "cmd_party_leave",
        "cmd_party_kick", "cmd_quizzes", "cmd_quizzes_stop", "cmd_quizzes_clear",
        "cmd_quizzes_compare", "cmd_quizzes_previous", "cmd_privacy", "cmd_locale",
        "cmd_loc_compare", "cmd_loc_suggest", "cmd_help",
    ]),
    ("section_admins", [
        "cmd_lang", "cmd_locallang", "cmd_find_with", "cmd_find_without",
        "cmd_wiki_founday", "cmd_wiki_foundays", "cmd_wiki_founday_remove",
        "cmd_setlogs", "cmd_settasks",
    ]),
    ("section_bot_admins", [
        "cmd_setup", "cmd_setadmin", "cmd_remadmin",
        "cmd_localizer_add", "cmd_localizer_rem",
        "cmd_add_unia", "cmd_allow_parties", "cmd_add_govt",
        "cmd_edit_party_admin", "cmd_loc_reply", "cmd_list_chats", "cmd_force_leave",
        "cmd_backup",
    ]),
    ("section_economy", [
        "cmd_create_bank", "cmd_bank", "cmd_edit_bank", "cmd_bank_add_leader",
        "cmd_bank_transfer",
        "cmd_open_account", "cmd_balance", "cmd_pay", "cmd_set_earn",
        "cmd_create_good", "cmd_goods", "cmd_craft", "cmd_inventory", "cmd_sell",
        "cmd_give_good", "cmd_autocraft", "cmd_autosend", "cmd_set_profession",
        "cmd_fine", "cmd_treaty", "cmd_set_rate", "cmd_convert", "cmd_rates",
        "cmd_set_wage", "cmd_party_dues",
    ]),
    ("section_enterprises", [
        "cmd_add_enterprise", "cmd_enterprise", "cmd_edit_enterprise",
        "cmd_ent_join", "cmd_ent_leave", "cmd_ent_kick",
        "cmd_ent_position", "cmd_ent_assign", "cmd_ent_salary",
        "cmd_ent_sell", "cmd_export", "cmd_auto_export", "cmd_transit",
    ]),
]

EMBED_FIELD_LIMIT = 1024

_FIELD_CONTINUATION = "​"

def _chunk_lines(lines, limit=EMBED_FIELD_LIMIT):
    """Group lines into blocks that each fit in one embed field."""
    blocks, current = [], []
    for line in lines:
        line = line if len(line) <= limit else line[:limit - 1] + "…"
        candidate = ("\n".join(current + [line]))
        if current and len(candidate) > limit:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks

def _help_pages(lang):
    """One page per section. A section too long for a single embed field is
    spread over several fields on the same page. The Olympiad page lists only
    /setolympiad while no Olympiad is running — the rest of its commands are not
    registered then, so listing them would point at nothing."""
    pages = []
    for title_key, keys in HELP_SECTIONS:
        blocks = _chunk_lines([localized_help(k, lang) for k in keys])
        pages.append((localized_help(title_key, lang), blocks))
    lines = [olympiad.text(k, lang) for k in olympiad_help_keys()]
    pages.append((olympiad.text("section_title", lang), _chunk_lines(lines)))
    return pages

def _help_embed(lang, pages, index):
    """Render one help page as an embed.

    A section too long for a single embed field is spread over several, and only
    the first of them carries the section title — the rest use a zero-width
    character, because a field must have a name."""
    title, blocks = pages[index]
    embed = discord.Embed(title=localized_help("title", lang),
                          color=discord.Color(DEFAULT_EMBED_COLOR))
    for i, block in enumerate(blocks):
        embed.add_field(name=title if i == 0 else _FIELD_CONTINUATION,
                        value=block, inline=False)
    embed.set_footer(text=localized("help_page", lang, page=index + 1, total=len(pages)))
    return embed

class _HelpView(ui.View):
    """Prev/next buttons over the help pages; only the caller may press them."""

    def __init__(self, lang, pages, user_id):
        """Build the two arrows and show the first page."""
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.lang = lang
        self.pages = pages
        self.user_id = user_id
        self.index = 0
        self.prev = ui.Button(label=localized("help_prev", lang), style=ButtonStyle.secondary)
        self.next = ui.Button(label=localized("help_next", lang), style=ButtonStyle.secondary)
        self.prev.callback = self._make_cb(-1)
        self.next.callback = self._make_cb(1)
        self.add_item(self.prev)
        self.add_item(self.next)
        self._sync()

    def _sync(self):
        """Grey out the arrow that has nowhere to go."""
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1

    def _make_cb(self, step):
        """One arrow's callback, closing over which way it steps."""
        async def callback(interaction: discord.Interaction):
            """Step a page for the person who ran /help, and nobody else."""
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    localized("consent_not_yours", self.lang), ephemeral=True)
                return
            self.index = max(0, min(len(self.pages) - 1, self.index + step))
            self._sync()
            await interaction.response.edit_message(
                embed=_help_embed(self.lang, self.pages, self.index), view=self)
        return callback

@bot.tree.command(name="help", description="show this command list")
async def help_command(interaction: discord.Interaction):
    """Show the command list, paginated (ephemeral).

    Built from the hand-written HELP_SECTIONS rather than from the command tree, so
    a command added without a line here works but is invisible."""
    lang = get_chat_lang(_chat_key(interaction))
    pages = _help_pages(lang)
    view = _HelpView(lang, pages, interaction.user.id) if len(pages) > 1 else None
    await interaction.response.send_message(
        embed=_help_embed(lang, pages, 0), view=view, ephemeral=True)

@bot.tree.command(name="verify", description="verify your Fandom account (checks your Discord handle on your Fandom profile)")
@app_commands.describe(fandom_name="Your username on fandom.com")
async def verify_cmd(interaction: discord.Interaction, fandom_name: str):
    """Verify your Fandom account.

    Fetches the profile through Fandom's public API and checks that the Discord
    handle published there is the caller's. A single check at one moment: nothing
    re-reads the profile later, and verification is global rather than per
    server."""
    lang = get_chat_lang(_chat_key(interaction))
    fandom_name = fandom_name.strip()
    if not fandom_name:
        await interaction.response.send_message(localized("verify_usage", lang), ephemeral=True)
        return
    if not rate_limit_ok(("verify", interaction.user.id), limit=3, window_seconds=600):
        await interaction.response.send_message(localized("verify_cooldown", lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    status, uid, cname, handle = await fandom.lookup_discord_handle(fandom_name)
    if status == "error":
        await interaction.followup.send(localized("verify_error", lang), ephemeral=True)
        return
    if status == "not_found":
        await interaction.followup.send(localized("verify_not_found", lang, name=fandom_name), ephemeral=True)
        return
    if status == "no_handle":
        await interaction.followup.send(localized("verify_no_handle", lang, name=cname), ephemeral=True)
        return

    if not fandom.handle_matches(handle, interaction.user.name, str(interaction.user)):
        await interaction.followup.send(
            localized("verify_mismatch", lang, handle=handle, you=interaction.user.name), ephemeral=True)
        return

    db.add_fandom_verification(interaction.user.id, cname, uid, handle)
    await interaction.followup.send(localized("verify_success", lang, name=cname), ephemeral=True)

@bot.tree.command(name="add-telegram", description="link your Telegram account (run /add-discord on Telegram within 30 min)")
@app_commands.describe(nickname="Your Telegram @username")
async def add_telegram_cmd(interaction: discord.Interaction, nickname: str):
    """Start linking your Telegram account: name your Telegram @username here
    and run `/add_discord` there within thirty minutes.

    Each side must name the other. The link is what makes the Telegram account
    count as verified, and it merges the quiz history — but not money already
    earned separately."""
    lang = get_chat_lang(_chat_key(interaction))
    telegram_nick = nickname.strip().lstrip("@")
    if not telegram_nick:
        await interaction.response.send_message(localized("link_usage_discord", lang), ephemeral=True)
        return
    if not is_verified("discord", interaction.user.id):
        await interaction.response.send_message(localized("link_need_verify", lang), ephemeral=True)
        return
    result, _dc, _tg = db.register_link_attempt("discord", interaction.user.id,
                                                interaction.user.name, telegram_nick)
    if result == "linked":
        await interaction.response.send_message(localized("link_success", lang), ephemeral=True)
    else:
        await interaction.response.send_message(
            localized("link_pending_discord", lang, nick=telegram_nick), ephemeral=True)

async def _run_privacy_action(interaction: discord.Interaction, lang, action):
    """Run one privacy action: show what is stored, delete quiz results,
    or toggle comparison.

    Every branch acts across the caller's linked accounts, because privacy is about
    the person and not about one messenger."""
    user_id = interaction.user.id
    channel = interaction.channel

    if action == "clear_all":
        db.delete_user_quiz_results("discord", user_id)
        await channel.send(localized("privacy_cleared_all", lang))
        return

    if action == "block_all":
        blocked = not db.is_quiz_compare_blocked("discord", user_id)
        db.set_quiz_compare_blocked("discord", user_id, "", blocked)
        await channel.send(localized(
            "privacy_block_all_on" if blocked else "privacy_block_all_off", lang))
        return

    quiz_ids = db.get_user_quiz_ids("discord", user_id)

    if action == "clear_one":
        quiz_id = await _numbered_choice(interaction, lang,
                                         localized("privacy_choose_quiz_clear", lang),
                                         quiz_ids, lambda q: quizzes.quiz_name(q, lang))
        if quiz_id is None:
            return
        db.delete_user_quiz_results("discord", user_id, quiz_id)
        await channel.send(localized("privacy_cleared_quiz", lang,
                                     name=quizzes.quiz_name(quiz_id, lang)))
        return

    if action == "block_one":
        blocked_ids = db.get_blocked_quiz_ids("discord", user_id)

        def render(q):
            """Draw the current page."""
            state = localized("state_yes" if q in blocked_ids else "state_no", lang)
            return f"{quizzes.quiz_name(q, lang)} ({state})"

        quiz_id = await _numbered_choice(interaction, lang,
                                         localized("privacy_choose_quiz_block", lang),
                                         quiz_ids, render)
        if quiz_id is None:
            return
        blocked = quiz_id not in blocked_ids
        db.set_quiz_compare_blocked("discord", user_id, quiz_id, blocked)
        await channel.send(localized(
            "privacy_block_quiz_on" if blocked else "privacy_block_quiz_off", lang,
            name=quizzes.quiz_name(quiz_id, lang)))

@bot.tree.command(name="privacy", description="manage the data the bot keeps about you")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def privacy_cmd(interaction: discord.Interaction):
    """Manage the data the bot keeps about you (ephemeral).

    Offers a numbered menu of the actions above. Deletions here are immediate and
    not undoable."""
    lang = get_chat_lang(_chat_key(interaction))
    user_id = interaction.user.id

    actions = privacy_actions("discord", user_id)
    if not actions:
        await interaction.response.send_message(localized("privacy_none", lang), ephemeral=True)
        return

    labels = privacy_option_labels("discord", user_id, lang, actions)
    lines = [localized("privacy_header", lang)]
    lines += [f"{i + 1}. {text}" for i, text in enumerate(labels)]
    await interaction.response.send_message(embed=discord.Embed(
        title=localized("privacy_title", lang),
        description="\n".join(lines), color=discord.Color(DEFAULT_EMBED_COLOR)))

    reply = await _wait_message(interaction.channel_id, user_id)
    if reply is None:
        return
    value = (reply.content or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(actions)):
        await interaction.channel.send(localized("choice_invalid", lang))
        return
    await _run_privacy_action(interaction, lang, actions[int(value) - 1])
