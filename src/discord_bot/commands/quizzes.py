"""Taking a quiz on Discord, and everything around a stored result.

A run is a live dialog: the question goes out as an embed with a dropdown of
its options, and `_wait_message_or_view` waits for either a pick or a typed
answer, whichever comes first. `_active_quizzes` maps a user to the running
session so that `/quizzes-stop` can reach into it — it must exist exactly once,
and its Telegram twin is `_active_quizzes_tg`.

Every choice here is answerable through a component and not only by typing, and
that is a requirement rather than a convenience. These five commands are
`allowed_installs(users=True)`: they can be run where the app is installed on
somebody's account rather than in the server, and there the bot receives the
interaction but no messages at all. A dialog that waits for a typed number in
that situation simply never hears it — the command answers, the list appears,
and nothing happens for half an hour. Typing still works everywhere it used to;
the dropdown is what makes the dialog answerable everywhere the command is.

A result is stored only after the taker says yes, and reading it always goes
through the identity set (db/quizzes.py), so a linked Discord+Telegram pair
sees one history. `/quizzes-compare` asks the *other* person's privacy row
before showing anything.

`_resume_state` is shared with the Telegram half: a run paused on one platform
is resumed on the other.

Not this module's zone: the quiz engine, its questions and its scoring
(quizzes.py, data in src/quizzes/) and the stored rows (db/quizzes.py).
"""
import asyncio
import json

import discord
from discord import app_commands, ButtonStyle

import db
import quizzes
from utils import (
    DEFAULT_EMBED_COLOR, format_quiz_date, get_chat_lang, localized,
    resolve_previous_quiz,
)

from discord_bot.client import _chat_key, bot
from discord_bot.dialogs import (
    _ChoiceSelect, _QuizButtons, _parse_user_ref, _wait_message_or_view,
)

_active_quizzes = {}

async def _pick_from_list(interaction, lang, title, header, items, render_line,
                          placeholder_key="quiz_pick_placeholder"):
    """Show a numbered list and take the answer either way: a pick from the
    dropdown, or the number typed into the chat.

    Every numbered choice in this module goes through here because these
    commands are installable on an account as well as in a server, and where the
    bot is only installed on the account it never sees the typed reply — the
    dropdown is what keeps the dialog answerable there. Returns the chosen item
    or None."""
    lines = [header] + [f"{i + 1}. {render_line(it)}" for i, it in enumerate(items)]
    view = _ChoiceSelect(
        interaction.user.id, lang,
        [(f"{i + 1}. {render_line(it)}"[:100], None, str(i)) for i, it in enumerate(items)],
        placeholder=localized(placeholder_key, lang))
    await interaction.response.send_message(
        embed=discord.Embed(title=title, description="\n".join(lines)[:4000],
                            color=discord.Color(DEFAULT_EMBED_COLOR)),
        view=view)
    outcome, answer = await _wait_message_or_view(
        interaction.channel_id, interaction.user.id, view)
    if outcome == "choice" and answer is not None:
        return items[int(answer)]
    if outcome != "message":
        return None
    value = (answer.content or "").strip().rstrip(".")
    if value.isdigit() and 1 <= int(value) <= len(items):
        return items[int(value) - 1]
    await interaction.channel.send(localized("choice_invalid", lang))
    return None

def _quiz_question_embed(quiz_id, lang, qindex, order, allow_prev, total):
    """One question as an embed: the topic, the question text and the
    shuffled options, with a page counter."""
    topic = quizzes.question_topic(quiz_id, lang, qindex)
    texts, _kinds = quizzes.display_options(quiz_id, lang, qindex, order, allow_prev)
    lines = [f"{i + 1}. {t}" for i, t in enumerate(texts)]
    desc = "\n".join(lines) + "\n\n" + localized("quiz_answer_hint", lang)
    return discord.Embed(
        title=f"{topic}  ({qindex + 1}/{total})"[:256],
        description=desc[:4096],
        color=discord.Color(DEFAULT_EMBED_COLOR),
    )

def _quiz_results_embed(quiz_id, lang, results, name):
    """The finished breakdown: one line per axis with its percentage and the
    label it falls under."""
    embed = discord.Embed(
        title=localized("quiz_results_title", lang, name=name)[:256],
        color=discord.Color(DEFAULT_EMBED_COLOR),
    )
    for a in quizzes.format_results(quiz_id, lang, results):
        breakdown = " · ".join(f"{e['name']} — {e['percent']}%" for e in a["entries"])
        value = breakdown
        if a["top_desc"]:
            value += "\n\n" + a["top_desc"]
        embed.add_field(name=a["axis_name"][:256], value=value[:1024], inline=False)
    return embed

async def _ask_weight(msg, channel, user_id, lang, num):
    """Ask how strongly an option is meant and return 1.0, 1.5 or None.

    A real option needs a strength as well as a number. Somebody typing their
    answer supplies both at once ('2-Y'); somebody picking from the dropdown has
    only given the number, so the strength is a second question. Returns None
    when they never answer, which sends the question round again."""
    view = _QuizButtons(user_id, lang, [
        (localized("quiz_weight_weak", lang), ButtonStyle.secondary, 1.0),
        (localized("quiz_weight_strong", lang), ButtonStyle.success, 1.5),
    ])
    prompt = await channel.send(
        embed=discord.Embed(description=localized("quiz_answer_need_weight", lang, num=num),
                            color=discord.Color(DEFAULT_EMBED_COLOR)),
        view=view)
    weight = await view.wait_click()
    try:
        await prompt.delete()
    except Exception:
        pass
    return weight

def _resume_state(row, quiz_id, n):
    """(orders, answers, qindex) of a parked attempt, or None when the saved
    shape no longer matches the quiz."""
    try:
        orders = json.loads(row["orders_json"])
        answers = json.loads(row["answers_json"])
    except Exception:
        return None
    if len(orders) != n or len(answers) != n:
        return None
    qindex = int(row["qindex"])
    if not 0 <= qindex < n:
        return None
    return orders, answers, qindex

async def _run_quiz_discord(interaction: discord.Interaction, quiz_id):
    """Run one quiz from the first question to the stored result.

    The session is registered in `_active_quizzes` so `/quizzes-stop` can reach into
    it, and each question waits for either an option button or a typed answer. A
    paused run keeps its progress — including the shuffled option order, which is
    why resuming shows the same options in the same places. The result is stored
    only after the taker consents."""
    lang = get_chat_lang(_chat_key(interaction))
    user_id = interaction.user.id
    channel = interaction.channel
    if channel is None:
        channel = interaction.user.dm_channel or await interaction.user.create_dm()
    name = quizzes.quiz_name(quiz_id, lang)
    n = quizzes.num_questions(quiz_id)

    saved = db.get_quiz_progress("discord", user_id, quiz_id)
    resumed = _resume_state(saved, quiz_id, n) if saved else None

    if resumed:
        orders, answers, qindex = resumed
        msg = await channel.send(embed=discord.Embed(
            title=name[:256],
            description=localized("quiz_resumed", lang, name=name, num=qindex + 1, total=n),
            color=discord.Color(DEFAULT_EMBED_COLOR)))
    else:
        start_view = _QuizButtons(user_id, lang, [
            (localized("quiz_go", lang), ButtonStyle.success, "go"),
            (localized("quiz_later", lang), ButtonStyle.secondary, "later"),
        ])
        prompt = localized("quiz_start_prompt", lang, name=name)
        credit = quizzes.quiz_credit(quiz_id, lang)
        if credit:
            prompt = f"{prompt}\n\n{credit}"
        embed = discord.Embed(title=name[:256],
                              description=prompt[:4096],
                              color=discord.Color(DEFAULT_EMBED_COLOR))
        msg = await channel.send(embed=embed, view=start_view)
        if await start_view.wait_click() != "go":
            await msg.edit(
                embed=discord.Embed(description=localized("quiz_cancelled", lang),
                                    color=discord.Color(DEFAULT_EMBED_COLOR)),
                view=None)
            return
        orders = quizzes.make_orders(quiz_id)
        answers = [None] * n
        qindex = 0

    session = {"quiz_id": quiz_id, "stop": asyncio.Event()}
    _active_quizzes[user_id] = session
    try:
        while qindex < n:
            allow_prev = qindex > 0
            texts, kinds = quizzes.display_options(quiz_id, lang, qindex,
                                                   orders[qindex], allow_prev)
            view = _ChoiceSelect(
                user_id, lang,
                [(f"{i + 1}. {t}"[:100], None, str(i + 1)) for i, t in enumerate(texts)],
                placeholder=localized("quiz_pick_placeholder", lang))
            await msg.edit(embed=_quiz_question_embed(quiz_id, lang, qindex, orders[qindex],
                                                      allow_prev, n), view=view)
            outcome, answer = await _wait_message_or_view(
                channel.id, user_id, view, session["stop"])
            if outcome == "stop":
                db.save_quiz_progress("discord", user_id, quiz_id, lang, qindex,
                                      json.dumps(orders),
                                      json.dumps(answers, ensure_ascii=False))
                await msg.edit(embed=discord.Embed(
                    description=localized("quiz_stop_saved", lang, name=name),
                    color=discord.Color(DEFAULT_EMBED_COLOR)), view=None)
                return
            if outcome == "timeout":
                await channel.send(localized("dialog_timeout", lang))
                return
            reply = None
            if outcome == "choice":
                if answer is None:
                    continue
                num, weight = int(answer), None
            else:
                reply = answer
                parsed = quizzes.parse_answer(reply.content or "")
                if parsed is None:
                    await channel.send(localized("quiz_answer_invalid", lang))
                    continue
                num, weight = parsed
            if num < 1 or num > len(kinds):
                await channel.send(localized("quiz_answer_out_of_range", lang))
                continue
            kind = kinds[num - 1]
            accepted = True
            if kind[0] == "prev":
                qindex -= 1
            elif kind[0] == "nota":
                answers[qindex] = ("nota",)
                qindex += 1
            elif kind[0] == "idc":
                answers[qindex] = ("idc",)
                qindex += 1
            else:
                if weight is None:
                    weight = await _ask_weight(msg, channel, user_id, lang, num)
                if weight is None:
                    accepted = False
                else:
                    answers[qindex] = ("real", kind[1], weight)
                    qindex += 1
            if accepted and reply is not None:
                try:
                    await reply.delete()
                except Exception:
                    pass
    finally:
        _active_quizzes.pop(user_id, None)

    db.delete_quiz_progress("discord", user_id, quiz_id)
    results = quizzes.score(quiz_id, answers)

    consent_view = _QuizButtons(user_id, lang, [
        (localized("quiz_consent_yes", lang), ButtonStyle.success, "yes"),
        (localized("quiz_consent_no", lang), ButtonStyle.danger, "no"),
    ])
    await msg.edit(embed=discord.Embed(description=localized("quiz_consent_prompt", lang),
                                       color=discord.Color(DEFAULT_EMBED_COLOR)),
                   view=consent_view)
    consent = await consent_view.wait_click()
    if consent == "yes":
        db.save_quiz_result("discord", user_id, quiz_id, lang,
                            json.dumps(results, ensure_ascii=False),
                            json.dumps(answers, ensure_ascii=False))
        await channel.send(localized("quiz_saved", lang))
    elif consent == "no":
        await channel.send(localized("quiz_not_saved", lang))

    await msg.edit(embed=_quiz_results_embed(quiz_id, lang, results, name), view=None)

@bot.tree.command(name="quizzes", description="take a quiz (works in DMs too)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_cmd(interaction: discord.Interaction):
    """Take a quiz. Works in a private chat with the bot as well as in a
    server; a paused run is offered for resuming rather than restarted."""
    lang = get_chat_lang(_chat_key(interaction))
    ids = quizzes.list_quizzes()
    chosen = await _pick_from_list(
        interaction, lang, localized("quizzes_title", lang),
        localized("quizzes_list_header", lang), ids,
        lambda qid: quizzes.quiz_name(qid, lang))
    if chosen is None:
        return
    await _run_quiz_discord(interaction, chosen)

@bot.tree.command(name="quizzes-stop", description="pause the quiz you are taking; your progress is kept for 7 days")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_stop_cmd(interaction: discord.Interaction):
    """Pause the quiz you are taking; the progress is kept for seven days."""
    lang = get_chat_lang(_chat_key(interaction))
    session = _active_quizzes.get(interaction.user.id)
    if not session:
        await interaction.response.send_message(localized("quiz_stop_none", lang), ephemeral=True)
        return
    session["stop"].set()
    await interaction.response.send_message(
        localized("quiz_stop_saved", lang, name=quizzes.quiz_name(session["quiz_id"], lang)),
        ephemeral=True)

@bot.tree.command(name="quizzes-clear", description="delete your saved quiz results")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_clear_cmd(interaction: discord.Interaction):
    """Delete your saved quiz results — one quiz's, or all of them.

    Acts across your linked accounts, because the history is shared between
    them."""
    lang = get_chat_lang(_chat_key(interaction))
    rows = db.get_user_quiz_results("discord", interaction.user.id)
    if not rows:
        await interaction.response.send_message(localized("quizzes_clear_none", lang), ephemeral=True)
        return
    row = await _pick_from_list(
        interaction, lang, localized("quizzes_title", lang),
        localized("quizzes_clear_header", lang), rows,
        lambda r: f"{quizzes.quiz_name(r['quiz_id'], lang)} — {format_quiz_date(r['created_at'])}")
    if row is None:
        return
    db.delete_quiz_result(row["id"])
    await interaction.channel.send(localized(
        "quizzes_clear_deleted", lang,
        name=quizzes.quiz_name(row["quiz_id"], lang), date=format_quiz_date(row["created_at"])))

def _quiz_compare_embed(quiz_id, lang, res_a, res_b, name_a, name_b):
    """Two people's breakdowns side by side, axis by axis."""
    embed = discord.Embed(
        title=localized("quizzes_compare_title", lang, name=quizzes.quiz_name(quiz_id, lang))[:256],
        color=discord.Color(DEFAULT_EMBED_COLOR))
    fa = quizzes.format_results(quiz_id, lang, res_a)
    fb = quizzes.format_results(quiz_id, lang, res_b)
    fb_by_axis = {a["axis"]: a for a in fb}
    for a in fa:
        b = fb_by_axis.get(a["axis"], {"entries": []})
        va = " · ".join(f"{e['name']} {e['percent']}%" for e in a["entries"])
        vb = " · ".join(f"{e['name']} {e['percent']}%" for e in b["entries"])
        value = f"**{name_a}:** {va}\n**{name_b}:** {vb}"
        embed.add_field(name=a["axis_name"][:256], value=value[:1024], inline=False)
    return embed

async def _quiz_target_name(target_id):
    """A display name for the person being compared against, falling back to
    their id when the bot cannot see them."""
    try:
        u = await bot.fetch_user(int(target_id))
        return str(u)
    except Exception:
        return str(target_id)

@bot.tree.command(name="quizzes-compare", description="compare quiz results with another user")
@app_commands.describe(user="The other user's ID or mention")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_compare_cmd(interaction: discord.Interaction, user: str):
    """Compare quiz results with another user.

    Refused when the other person forbade comparison in `/privacy` — theirs is the
    veto, not the caller's — and when either side has no stored result for the
    quiz."""
    lang = get_chat_lang(_chat_key(interaction))
    target_id = _parse_user_ref(user)
    if target_id is None:
        await interaction.response.send_message(localized("quizzes_compare_user_invalid", lang), ephemeral=True)
        return
    if str(target_id) == str(interaction.user.id):
        await interaction.response.send_message(localized("quizzes_compare_self", lang), ephemeral=True)
        return

    if db.is_quiz_compare_blocked("discord", target_id):
        await interaction.response.send_message(localized("quizzes_compare_blocked", lang), ephemeral=True)
        return

    blocked = db.get_blocked_quiz_ids("discord", target_id)
    mine = db.get_user_quiz_ids("discord", interaction.user.id)
    theirs = set(db.get_user_quiz_ids("discord", target_id))
    shared = [q for q in mine if q in theirs and q not in blocked]
    if not shared:
        if any(q in theirs for q in mine):
            await interaction.response.send_message(localized("quizzes_compare_blocked", lang), ephemeral=True)
        else:
            await interaction.response.send_message(localized("quizzes_compare_none", lang))
        return

    name_a = localized("quizzes_compare_you", lang)
    name_b = await _quiz_target_name(target_id)

    async def show(quiz_id, via_channel):
        """Render one quiz's result, through the channel or the interaction
        depending on how far the command has already answered."""
        ra = db.get_latest_quiz_result("discord", interaction.user.id, quiz_id)
        rb = db.get_latest_quiz_result("discord", target_id, quiz_id)
        if not ra or not rb:
            return
        res_a = json.loads(ra["result_json"])
        res_b = json.loads(rb["result_json"])
        embed = _quiz_compare_embed(quiz_id, lang, res_a, res_b, name_a, name_b)
        if via_channel:
            await interaction.channel.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

    if len(shared) == 1:
        await show(shared[0], via_channel=False)
        return

    chosen = await _pick_from_list(
        interaction, lang, localized("quizzes_title", lang),
        localized("quizzes_compare_header", lang), shared,
        lambda qid: quizzes.quiz_name(qid, lang))
    if chosen is None:
        return
    await show(chosen, via_channel=True)

def _quiz_history_embed(quiz_id, lang, latest, previous):
    """The user's two most recent attempts of one quiz, side by side, each
    labelled with the day it was taken."""
    embed = _quiz_compare_embed(
        quiz_id, lang,
        json.loads(latest["result_json"]), json.loads(previous["result_json"]),
        localized("quizzes_previous_current", lang, date=format_quiz_date(latest["created_at"])),
        localized("quizzes_previous_earlier", lang, date=format_quiz_date(previous["created_at"])),
    )
    embed.title = localized("quizzes_previous_title", lang,
                            name=quizzes.quiz_name(quiz_id, lang))[:256]
    return embed

@bot.tree.command(name="quizzes-previous", description="compare your latest quiz result with the one before it")
@app_commands.describe(number="Quiz number from /quizzes; omit it within 30 minutes of finishing a quiz")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def quizzes_previous_cmd(interaction: discord.Interaction, number: int = None):
    """Compare your latest result with the one before it.

    The quiz number may be omitted within half an hour of finishing one, which is
    what makes the command usable straight after a run without looking anything
    up."""
    lang = get_chat_lang(_chat_key(interaction))
    user_id = interaction.user.id

    quiz_id, error = resolve_previous_quiz("discord", user_id, number)
    if error:
        await interaction.response.send_message(localized(error, lang), ephemeral=True)
        return

    latest = db.get_latest_quiz_result("discord", user_id, quiz_id)
    previous = db.get_previous_quiz_result("discord", user_id, quiz_id)
    if not latest or not previous:
        await interaction.response.send_message(
            localized("quizzes_previous_none", lang, name=quizzes.quiz_name(quiz_id, lang)),
            ephemeral=True)
        return

    await interaction.response.send_message(
        embed=_quiz_history_embed(quiz_id, lang, latest, previous))
