"""Quiz engine for the /quizzes commands (Discord + Telegram).

The first quiz, **WikiCharts**, is adapted from the open-source WikiCharts test
(https://github.com/WikiCharts/wikicharts.github.io): the user is shown a topic
and a set of statements and picks the one closest to their view, with a strong
or a weak preference. Answers are scored on four axes — governance, speech,
security (legal) and foreign policy — into a breakdown of ideologies.

This module is platform-neutral: it loads the scoring key and the localized
question/answer/result text, presents questions, parses answers and computes
results. The Discord and Telegram bots own the dialog flow and storage.

`_compute` and its helpers are a faithful port of the site's computeScores.js
vector algorithm; Python dicts preserve insertion order, matching the JS object
key ordering the original relies on for its rounding-residual assignment.
"""

import json
import logging
import math
import os
import random
import re

logger = logging.getLogger("dem.quizzes")

_HERE = os.path.dirname(os.path.abspath(__file__))
_QUIZ_DIR = os.path.join(_HERE, "quizzes")
_CONTENT_DIR = os.path.join(_HERE, "i18n", "quizzes")

_REGISTRY = [
    {"id": "wikicharts", "default_lang": "ru"},
]

_FULL_LABEL = {"rep": "repr", "rep*": "repr*", "retrib": "retribu", "retrib*": "retribu*"}

_scoring_cache = {}
_content_cache = {}

def _load_scoring(quiz_id):
    """Load and cache one quiz's scoring data from
    quizzes/<id>/scoring.json.

    The path is built from this module's own directory, so quizzes.py must stay
    directly in src/ — one level deeper and every quiz silently becomes
    non-existent."""
    if quiz_id not in _scoring_cache:
        path = os.path.join(_QUIZ_DIR, quiz_id, "scoring.json")
        with open(path, encoding="utf-8") as f:
            _scoring_cache[quiz_id] = json.load(f)
    return _scoring_cache[quiz_id]

def _load_content(quiz_id):
    """{lang: content_dict} for every i18n/quizzes/<quiz_id>.<lang>.json present."""
    if quiz_id not in _content_cache:
        langs = {}
        if os.path.isdir(_CONTENT_DIR):
            prefix = quiz_id + "."
            for fname in sorted(os.listdir(_CONTENT_DIR)):
                if fname.startswith(prefix) and fname.endswith(".json"):
                    lang = fname[len(prefix):-len(".json")]
                    try:
                        with open(os.path.join(_CONTENT_DIR, fname), encoding="utf-8") as f:
                            langs[lang] = json.load(f)
                    except Exception as e:
                        logger.warning("Failed to load quiz content %s: %s", fname, e)
        _content_cache[quiz_id] = langs
    return _content_cache[quiz_id]

def _quiz_meta(quiz_id):
    """The registry entry of a quiz: its id, folder and content file."""
    for q in _REGISTRY:
        if q["id"] == quiz_id:
            return q
    return None

def list_quizzes():
    """Available quiz ids, in menu order."""
    return [q["id"] for q in _REGISTRY]

def quiz_exists(quiz_id):
    """Whether a quiz id is registered."""
    return _quiz_meta(quiz_id) is not None

def content(quiz_id, lang):
    """Localized content for a quiz, falling back to the quiz's default language
    and then to any available language."""
    langs = _load_content(quiz_id)
    if not langs:
        return {}
    meta = _quiz_meta(quiz_id) or {}
    default = meta.get("default_lang", "ru")
    return langs.get(lang) or langs.get(default) or next(iter(langs.values()))

def quiz_name(quiz_id, lang):
    """The quiz's display name in a language."""
    return content(quiz_id, lang).get("name", quiz_id)

def quiz_credit(quiz_id, lang):
    """Attribution/source note for the quiz, shown with the start prompt."""
    return content(quiz_id, lang).get("credit", "")

def axes(quiz_id):
    """The quiz's axes — the dimensions a result is broken down along."""
    return _load_scoring(quiz_id)["axes"]

def questions(quiz_id):
    """The quiz's question list, straight from the scoring data."""
    return _load_scoring(quiz_id)["questions"]

def num_questions(quiz_id):
    """How many questions the quiz has. The length every stored progress row
    is validated against, so a quiz that gained a question invalidates the paused
    runs rather than resuming them wrongly."""
    return len(_load_scoring(quiz_id)["questions"])

def make_orders(quiz_id):
    """A per-question shuffled list of option positions, so answer order does not
    bias respondents. Kept for the whole session (also used to map a chosen
    number back to its scoring position when going to a previous question)."""
    orders = []
    for q in questions(quiz_id):
        order = list(range(len(q["options"])))
        random.shuffle(order)
        orders.append(order)
    return orders

def display_options(quiz_id, lang, qindex, order, allow_prev):
    """The numbered options shown for a question.

    Returns (option_texts, kinds): `option_texts` are the display strings in
    order; `kinds` describes each — ("real", scoring_position) for a statement,
    or ("nota",) / ("idc",) / ("prev",) for the special options that need only a
    number (no Mb/Y suffix)."""
    c = content(quiz_id, lang)
    q = questions(quiz_id)[qindex]
    qtext = c.get("questions", {}).get(q["key"], {})
    opts = qtext.get("options", [])
    texts, kinds = [], []
    for pos in order:
        texts.append(opts[pos] if pos < len(opts) else q["options"][pos])
        kinds.append(("real", pos))
    special = c.get("special", {})
    texts.append(special.get("nota_opt", "—"))
    kinds.append(("nota",))
    texts.append(special.get("idc_opt", "—"))
    kinds.append(("idc",))
    if allow_prev:
        texts.append(special.get("prev_opt", "—"))
        kinds.append(("prev",))
    return texts, kinds

def question_topic(quiz_id, lang, qindex):
    """The topic line shown above one question."""
    q = questions(quiz_id)[qindex]
    qtext = content(quiz_id, lang).get("questions", {}).get(q["key"], {})
    return qtext.get("topic", q["key"])

_ANS_RE = re.compile(r"^\s*(\d{1,2})[-\s]*([A-Za-zА-Яа-я]{1,3})?\s*$")

def parse_answer(text):
    """Parse a 'Number-Answer' reply. Returns (number, weight) where weight is
    None (no suffix given), 1.0 for a weak preference (Mb/MB) or 1.5 for a
    definite one (Y); or None if the reply is not a valid answer at all."""
    m = _ANS_RE.match((text or "").strip())
    if not m:
        return None
    num = int(m.group(1))
    suf = (m.group(2) or "").lower()
    if suf == "":
        weight = None
    elif suf in ("mb", "мб"):
        weight = 1.0
    elif suf in ("y", "д"):
        weight = 1.5
    else:
        return None
    return num, weight

def _vectorize(pair):
    """Turn a scoring entry into a plain list of floats, one per axis."""
    label, weight = pair[0], pair[1]
    t = {}
    for d in label.split("/"):
        t[d] = t.get(d, 0) + weight
    return t

def _vadd(a, b):
    """Add two score vectors element-wise."""
    res = {}
    for key in a:
        if b.get(key + "*"):
            res[key + "*"] = a[key]
        else:
            res[key] = a[key]
    for key in b:
        if res.get(key + "*"):
            res[key + "*"] = max(0, res[key + "*"] + b[key])
        else:
            res[key] = max(0, res.get(key, 0) + b[key])
    return res

def _vdot(a, b):
    """Multiply a score vector by a weight vector element-wise."""
    res = {}
    for key in a:
        if key.endswith("*"):
            res[key] = a[key] * (b.get(key, 0) + b.get(key.replace("*", ""), 0))
        else:
            if b.get(key + "*"):
                res[key + "*"] = a[key] * b[key + "*"]
            else:
                res[key] = a[key] * b.get(key, 0)
    return res

def _vtimes(v, tms):
    """Scale a score vector by a number."""
    return {key: v[key] * tms for key in v}

def _normalize(v):
    """Map a raw axis total onto 0..100."""
    total = 0.0
    for key in v:
        total += v[key]
    if total == 0:
        return dict(v)
    return _vtimes(v, 1.0 / total)

def _flatten(v, multiplier):
    """Flatten nested axis groups into one ordered list."""
    m = 0
    for key in v:
        m = max(m, v[key])
    if m == 0:
        return v
    return _vtimes(v, multiplier / m)

def _compress(nums, glob, amt, epsilon):
    """Round a vector to the precision the results are displayed at."""
    return _normalize(_vadd(_vadd(nums, _vtimes(glob, -amt)), {"cent": epsilon}))

def _round_js(x):
    """Math.round: half rounds toward +Infinity. Our values are non-negative."""
    return int(math.floor(x + 0.5))

def _compute(nums, glob):
    """`nums` is a list of [label, weight] for one axis; `glob` the set of labels
    present in the answers (each 1). Returns [[label, permille], ...] summing to
    1000, or [['cent', 1000]] for four-plus mixed ideologies."""
    epsilon = 1e-9

    mono = _vtimes(glob, epsilon)
    for d in nums:
        if "/" not in d[0]:
            mono = _vadd(mono, _vectorize(d))

    poly = _vtimes(glob, epsilon)
    for d in nums:
        if "/" in d[0]:
            dotted = _vdot(_vectorize(d), _vadd(mono, _vtimes(glob, epsilon)))
            poly = _vadd(poly, _flatten(dotted, d[1]))

    temp = _normalize(_vadd(mono, poly))
    s = _compress(_compress(temp, glob, 0.08, epsilon), glob, 0.2, epsilon)

    sol = []
    count = 0
    for key in s:
        if s[key] > 0.005:
            count += 1
            sol.append([key, _round_js(s[key] * 1000)])
    if count >= 4:
        return [["cent", 1000]]
    if len(sol) == 3:
        sol[0][1] = 1000 - (sol[1][1] + sol[2][1])
    elif len(sol) == 2:
        sol[0][1] = 1000 - sol[1][1]
    elif len(sol) == 1:
        sol[0][1] = 1000
    else:
        return [["cent", 1000]]
    return sol

def score(quiz_id, answers):
    """Compute the per-axis result from `answers`, one entry per question in the
    quiz's canonical order:
      ("real", scoring_position, weight)  — a chosen statement (weight 1 or 1.5)
      ("nota",)                           — disagrees with all
      ("idc",)                            — does not care
    Returns {axis: [[label, permille], ...]}."""
    scoring = _load_scoring(quiz_id)
    qs = scoring["questions"]
    dims = {ax: [] for ax in scoring["axes"]}
    globs = {ax: {} for ax in scoring["axes"]}

    for i, q in enumerate(qs):
        ans = answers[i]
        qweight = (float(q["weight"]) if str(q.get("weight", "")).strip() else 0.0) + 1.0
        if ans[0] == "real":
            pos, w = ans[1], ans[2]
            label = q["options"][pos]
        elif ans[0] == "nota":
            nota = q.get("nota", "")
            label = "nota" + (("/" + nota) if nota else "")
            w = 1.0
        else:
            idc = q.get("idc", "")
            label = "idc" + (("/" + idc) if idc else "")
            w = 1.0
        ax = q["category"]
        dims[ax].append([label, w * qweight])
        for e in label.split("/"):
            globs[ax][e] = 1

    return {ax: _compute(dims[ax], globs[ax]) for ax in scoring["axes"]}

def _label_name(quiz_id, lang, code):
    """The name of the label an axis value falls under."""
    return content(quiz_id, lang).get("labels", {}).get(code, code)

def _label_desc(quiz_id, lang, code):
    """The description of that label, or an empty string."""
    return content(quiz_id, lang).get("descriptions", {}).get(code, "")

def percent_str(permille):
    """455 -> '45.5', 1000 -> '100'."""
    val = permille / 10.0
    if abs(val - round(val)) < 1e-9:
        return str(int(round(val)))
    return f"{val:.1f}"

def format_results(quiz_id, lang, results):
    """Structured, presentation-ready results. Returns a list of axes:
      {axis, axis_name, entries: [{code, name, percent}], top_name, top_desc}
    entries are ordered strongest-first."""
    c_axes = content(quiz_id, lang).get("axes", {})
    out = []
    for ax in axes(quiz_id):
        sol = results.get(ax, [])
        ordered = sorted(sol, key=lambda kv: kv[1], reverse=True)
        entries = [{
            "code": code,
            "name": _label_name(quiz_id, lang, code),
            "percent": percent_str(pm),
        } for code, pm in ordered]
        top_code = ordered[0][0] if ordered else "cent"
        out.append({
            "axis": ax,
            "axis_name": c_axes.get(ax, ax),
            "entries": entries,
            "top_name": _label_name(quiz_id, lang, top_code),
            "top_desc": _label_desc(quiz_id, lang, top_code),
        })
    return out

def summarize_results(quiz_id, lang, results):
    """One-line-per-axis 'Axis: Top 55% · Next 30% · …' summary (used in
    comparisons and compact views)."""
    lines = []
    for a in format_results(quiz_id, lang, results):
        parts = [f"{e['name']} {e['percent']}%" for e in a["entries"]]
        lines.append(f"{a['axis_name']}: " + " · ".join(parts))
    return "\n".join(lines)
