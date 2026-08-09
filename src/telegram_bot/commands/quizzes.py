"""Taking a quiz on Telegram, and everything around a stored result.

A run edits one message in place rather than sending a new one per question,
which is what `_edit_quiz_msg` is for — a Telegram chat has no ephemeral
messages, so a quiz that posted every question would bury the group. The option
buttons go through `_pending_quiz_buttons` and the callback handler in
telegram_bot/callbacks.py; a typed answer arrives through the catchall instead,
and `_wait_user_message_or_stop` waits for whichever comes first.

`_active_quizzes_tg` is the running-session registry and must exist exactly
once; its Discord twin is `_active_quizzes`. `_resume_state` is imported from
the Discord half, because a run paused on one platform is resumed on the other:
the progress row is keyed by identity, not by platform.

Not this module's zone: the quiz engine (quizzes.py, data in src/quizzes/) and
the stored rows (db/quizzes.py).
"""
import asyncio
import json

from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message,
)

import db
import quizzes
from message_relay import escape_html
from utils import format_quiz_date, get_chat_lang, localized, resolve_previous_quiz

from discord_bot import _resume_state
from telegram_bot.client import _chat_key, bot, router
from telegram_bot.dialogs import (
    _await_quiz_button, _new_quiz_button_token, _quiz_target_label_tg,
    _resolve_tg_target, _wait_user_message, _wait_user_message_or_stop,
)

_active_quizzes_tg = {}

def _quiz_question_text_tg(quiz_id, lang, qindex, order, allow_prev, total):
    """One question as HTML: the topic, the question and the shuffled
    options, with a page counter."""
    topic = quizzes.question_topic(quiz_id, lang, qindex)
    texts, _kinds = quizzes.display_options(quiz_id, lang, qindex, order, allow_prev)
    lines = [f"{topic}  ({qindex + 1}/{total})", ""]
    lines += [f"{i + 1}. {t}" for i, t in enumerate(texts)]
    lines += ["", localized("quiz_answer_hint", lang)]
    return "\n".join(lines)[:4096]

def _quiz_results_text_tg(quiz_id, lang, results, name, with_desc=True):
    """The finished breakdown as HTML, one block per axis."""
    lines = [f"<b>{escape_html(localized('quiz_results_title', lang, name=name))}</b>", ""]
    for a in quizzes.format_results(quiz_id, lang, results):
        breakdown = " · ".join(f"{e['name']} — {e['percent']}%" for e in a["entries"])
        lines.append(f"<b>{escape_html(a['axis_name'])}</b>")
        lines.append(escape_html(breakdown))
        if with_desc and a["top_desc"]:
            lines.append(escape_html(a["top_desc"]))
        lines.append("")
    return "\n".join(lines).strip()

def _quiz_compare_text_tg(quiz_id, lang, res_a, res_b, name_a, name_b, title=None):
    """Two people's breakdowns side by side, axis by axis."""
    if title is None:
        title = localized("quizzes_compare_title", lang, name=quizzes.quiz_name(quiz_id, lang))
    lines = [f"<b>{escape_html(title)}</b>", ""]
    fb = {a["axis"]: a for a in quizzes.format_results(quiz_id, lang, res_b)}
    for a in quizzes.format_results(quiz_id, lang, res_a):
        b = fb.get(a["axis"], {"entries": []})
        va = " · ".join(f"{e['name']} {e['percent']}%" for e in a["entries"])
        vb = " · ".join(f"{e['name']} {e['percent']}%" for e in b["entries"])
        lines.append(f"<b>{escape_html(a['axis_name'])}</b>")
        lines.append(f"{escape_html(name_a)}: {escape_html(va)}")
        lines.append(f"{escape_html(name_b)}: {escape_html(vb)}")
        lines.append("")
    return "\n".join(lines).strip()

async def _edit_quiz_msg(chat_id, msg_id, text, reply_markup=None, parse_mode=None):
    """Edit the running quiz's message in place, ignoring the "message is
    not modified" error Telegram raises when nothing changed.

    A quiz edits one message rather than sending a new one per question: a group
    would otherwise be buried under a quiz somebody is taking in it."""
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                    reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except Exception:
        return False

async def _run_quiz_tg(message: Message, quiz_id):
    """Run one quiz from the first question to the stored result.

    The session is registered in `_active_quizzes_tg` so `/quizzes_stop` can reach
    into it, and each question waits for either an option button or a typed answer,
    whichever comes first. A paused run keeps its shuffled option order, so
    resuming shows the same options in the same places. The result is stored only
    after the taker consents."""
    lang = get_chat_lang(_chat_key(message))
    chat_id = message.chat.id
    user_id = message.from_user.id
    name = quizzes.quiz_name(quiz_id, lang)
    n = quizzes.num_questions(quiz_id)

    saved = db.get_quiz_progress("telegram", user_id, quiz_id)
    resumed = _resume_state(saved, quiz_id, n) if saved else None

    if resumed:
        orders, answers, qindex = resumed
        sent = await message.answer(
            localized("quiz_resumed", lang, name=name, num=qindex + 1, total=n))
        msg_id = sent.message_id
    else:
        token, fut = _new_quiz_button_token(user_id, lang)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=localized("quiz_go", lang), callback_data=f"qb:{token}:go"),
            InlineKeyboardButton(text=localized("quiz_later", lang), callback_data=f"qb:{token}:later"),
        ]])
        start_text = localized("quiz_start_prompt", lang, name=name)
        credit = quizzes.quiz_credit(quiz_id, lang)
        if credit:
            start_text = f"{start_text}\n\n{credit}"
        sent = await message.answer(start_text, reply_markup=kb)
        msg_id = sent.message_id
        if await _await_quiz_button(fut) != "go":
            await _edit_quiz_msg(chat_id, msg_id, localized("quiz_cancelled", lang))
            return
        orders = quizzes.make_orders(quiz_id)
        answers = [None] * n
        qindex = 0

    session = {"quiz_id": quiz_id, "stop": asyncio.Event()}
    _active_quizzes_tg[user_id] = session
    try:
        while qindex < n:
            allow_prev = qindex > 0
            await _edit_quiz_msg(chat_id, msg_id,
                                 _quiz_question_text_tg(quiz_id, lang, qindex, orders[qindex], allow_prev, n))
            outcome, reply = await _wait_user_message_or_stop(chat_id, user_id, session["stop"])
            if outcome == "stop":
                db.save_quiz_progress("telegram", user_id, quiz_id, lang, qindex,
                                      json.dumps(orders),
                                      json.dumps(answers, ensure_ascii=False))
                stopped = localized("quiz_stop_saved", lang, name=name)
                if not await _edit_quiz_msg(chat_id, msg_id, stopped):
                    await message.answer(stopped)
                return
            if outcome == "timeout":
                await message.answer(localized("dialog_timeout", lang))
                return
            parsed = quizzes.parse_answer(reply.text or reply.caption or "")
            if parsed is None:
                await message.answer(localized("quiz_answer_invalid", lang))
                continue
            num, weight = parsed
            _texts, kinds = quizzes.display_options(quiz_id, lang, qindex, orders[qindex], allow_prev)
            if num < 1 or num > len(kinds):
                await message.answer(localized("quiz_answer_out_of_range", lang))
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
                    await message.answer(localized("quiz_answer_need_weight", lang, num=num))
                    accepted = False
                else:
                    answers[qindex] = ("real", kind[1], weight)
                    qindex += 1
            if accepted:
                try:
                    await reply.delete()
                except Exception:
                    pass
    finally:
        _active_quizzes_tg.pop(user_id, None)

    db.delete_quiz_progress("telegram", user_id, quiz_id)
    results = quizzes.score(quiz_id, answers)

    token, fut = _new_quiz_button_token(user_id, lang)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=localized("quiz_consent_yes", lang), callback_data=f"qb:{token}:yes"),
        InlineKeyboardButton(text=localized("quiz_consent_no", lang), callback_data=f"qb:{token}:no"),
    ]])
    if not await _edit_quiz_msg(chat_id, msg_id, localized("quiz_consent_prompt", lang), reply_markup=kb):
        await message.answer(localized("quiz_consent_prompt", lang), reply_markup=kb)
    consent = await _await_quiz_button(fut)
    if consent == "yes":
        db.save_quiz_result("telegram", user_id, quiz_id, lang,
                            json.dumps(results, ensure_ascii=False),
                            json.dumps(answers, ensure_ascii=False))
        await message.answer(localized("quiz_saved", lang))
    elif consent == "no":
        await message.answer(localized("quiz_not_saved", lang))

    text = _quiz_results_text_tg(quiz_id, lang, results, name)
    if len(text) > 4096:
        text = _quiz_results_text_tg(quiz_id, lang, results, name, with_desc=False)
    if not await _edit_quiz_msg(chat_id, msg_id, text, parse_mode="HTML"):
        await message.answer(text, parse_mode="HTML")

@router.message(Command("quizzes"))
async def quizzes_tg(message: Message):
    """Take a quiz. A paused run is offered for resuming rather than
    restarted."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    ids = quizzes.list_quizzes()
    lines = [localized("quizzes_list_header", lang)]
    for i, qid in enumerate(ids):
        lines.append(f"{i + 1}. {quizzes.quiz_name(qid, lang)}")
    await message.reply("\n".join(lines))

    reply = await _wait_user_message(message.chat.id, message.from_user.id)
    if reply is None:
        return
    value = (reply.text or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(ids)):
        await message.reply(localized("choice_invalid", lang))
        return
    await _run_quiz_tg(message, ids[int(value) - 1])

@router.message(Command("quizzes_stop", "quizzes-stop"))
async def quizzes_stop_tg(message: Message):
    """Pause the quiz you are taking; the progress is kept for seven
    days."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    session = _active_quizzes_tg.get(message.from_user.id)
    if not session:
        await message.reply(localized("quiz_stop_none", lang))
        return
    session["stop"].set()

@router.message(Command("quizzes_clear", "quizzes-clear"))
async def quizzes_clear_tg(message: Message):
    """Delete your saved quiz results — one quiz's, or all of them, across
    your linked accounts."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    rows = db.get_user_quiz_results("telegram", message.from_user.id)
    if not rows:
        await message.reply(localized("quizzes_clear_none", lang))
        return
    lines = [localized("quizzes_clear_header", lang)]
    for i, r in enumerate(rows):
        lines.append(f"{i + 1}. {quizzes.quiz_name(r['quiz_id'], lang)} — {format_quiz_date(r['created_at'])}")
    await message.reply("\n".join(lines))

    reply = await _wait_user_message(message.chat.id, message.from_user.id)
    if reply is None:
        return
    value = (reply.text or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(rows)):
        await message.reply(localized("choice_invalid", lang))
        return
    row = rows[int(value) - 1]
    db.delete_quiz_result(row["id"])
    await message.reply(localized("quizzes_clear_deleted", lang,
                                  name=quizzes.quiz_name(row["quiz_id"], lang),
                                  date=format_quiz_date(row["created_at"])))

@router.message(Command("quizzes_compare", "quizzes-compare"))
async def quizzes_compare_tg(message: Message):
    """Compare quiz results with another user.

    Refused when the other person forbade comparison in `/privacy` — theirs is the
    veto, not the caller's."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("quizzes_compare_usage", lang))
        return

    target_id = await _resolve_tg_target(message, parts[1].strip())
    if target_id is None:
        await message.reply(localized("quizzes_compare_user_invalid", lang))
        return
    if str(target_id) == str(message.from_user.id):
        await message.reply(localized("quizzes_compare_self", lang))
        return

    if db.is_quiz_compare_blocked("telegram", target_id):
        await message.reply(localized("quizzes_compare_blocked", lang))
        return

    blocked = db.get_blocked_quiz_ids("telegram", target_id)
    mine = db.get_user_quiz_ids("telegram", message.from_user.id)
    theirs = set(db.get_user_quiz_ids("telegram", target_id))
    shared = [q for q in mine if q in theirs and q not in blocked]
    if not shared:
        if any(q in theirs for q in mine):
            await message.reply(localized("quizzes_compare_blocked", lang))
        else:
            await message.reply(localized("quizzes_compare_none", lang))
        return

    name_a = localized("quizzes_compare_you", lang)
    name_b = await _quiz_target_label_tg(target_id)

    async def show(quiz_id):
        """Render one quiz's comparison, once the quiz has been chosen."""
        ra = db.get_latest_quiz_result("telegram", message.from_user.id, quiz_id)
        rb = db.get_latest_quiz_result("telegram", target_id, quiz_id)
        if not ra or not rb:
            return
        text = _quiz_compare_text_tg(quiz_id, lang,
                                     json.loads(ra["result_json"]), json.loads(rb["result_json"]),
                                     name_a, name_b)
        await message.answer(text, parse_mode="HTML")

    if len(shared) == 1:
        await show(shared[0])
        return

    lines = [localized("quizzes_compare_header", lang)]
    for i, qid in enumerate(shared):
        lines.append(f"{i + 1}. {quizzes.quiz_name(qid, lang)}")
    await message.reply("\n".join(lines))

    reply = await _wait_user_message(message.chat.id, message.from_user.id)
    if reply is None:
        return
    value = (reply.text or "").strip().rstrip(".")
    if not value.isdigit() or not (1 <= int(value) <= len(shared)):
        await message.reply(localized("choice_invalid", lang))
        return
    await show(shared[int(value) - 1])

def _quiz_history_text_tg(quiz_id, lang, latest, previous):
    """The user's two most recent attempts of one quiz, each labelled with the
    day it was taken."""
    return _quiz_compare_text_tg(
        quiz_id, lang,
        json.loads(latest["result_json"]), json.loads(previous["result_json"]),
        localized("quizzes_previous_current", lang, date=format_quiz_date(latest["created_at"])),
        localized("quizzes_previous_earlier", lang, date=format_quiz_date(previous["created_at"])),
        title=localized("quizzes_previous_title", lang, name=quizzes.quiz_name(quiz_id, lang)),
    )

@router.message(Command("quizzes_previous", "quizzes-previous"))
async def quizzes_previous_tg(message: Message):
    """Compare your latest result with the one before it.

    The quiz number may be omitted within half an hour of finishing one."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    user_id = message.from_user.id

    parts = (message.text or "").split()
    number = None
    if len(parts) > 1:
        if not parts[1].strip().isdigit():
            await message.reply(localized("quizzes_previous_usage", lang))
            return
        number = int(parts[1].strip())

    quiz_id, error = resolve_previous_quiz("telegram", user_id, number)
    if error:
        await message.reply(localized(error, lang))
        return

    latest = db.get_latest_quiz_result("telegram", user_id, quiz_id)
    previous = db.get_previous_quiz_result("telegram", user_id, quiz_id)
    if not latest or not previous:
        await message.reply(localized("quizzes_previous_none", lang,
                                      name=quizzes.quiz_name(quiz_id, lang)))
        return

    await message.answer(_quiz_history_text_tg(quiz_id, lang, latest, previous),
                         parse_mode="HTML")
