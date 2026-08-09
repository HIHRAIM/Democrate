"""The monthly production task: generating it from the server's own history,
reading its progress, and judging it once the month is over.

A task is not drawn from a global table — it is last month's output on this
server multiplied by task_growth, with a floor so that a server which produced
nothing still gets something to do. The mandatory half is the five base
categories and must be produced on this server; the optional half is the
server's own top custom goods.

Evaluation is deliberately separated from the posting. `evaluate_finished_tasks`
judges every past month still open, on every server, because the verdict feeds
next month's provision bonus even on a server that never configured a tasks
channel and will never see the post.

Month arithmetic is UTC and inclusive of the first, exclusive of the next
first, which is the boundary `server_category_production` is queried with.

Not this module's zone: the task rows and the production rows behind them
(db/serverstats.py), the provision bonus itself (economy/consumption.py) and
the post (stats.py).
"""
import math
import time

import db
from economy.money import _cfg

def month_bounds(month):
    """(start_ts, end_ts) of a 'YYYY-MM' month in UTC."""
    from datetime import datetime, timezone
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())

def prev_month(month):
    """The 'YYYY-MM' month before this one, rolling the year over.

    Two very different callers: task generation, which measures last month's
    output to grow on, and the provision score, which asks whether last month's
    task was completed."""
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - (m == 1):04d}-{12 if m == 1 else m - 1:02d}"

def generate_monthly_task(platform, server_id, month):
    """Build (and store) the server's task for `month`: the mandatory part asks
    every base category to grow on last month's output (task_growth, with a
    floor of task_min_qty units); the optional part lists the server's top
    custom goods at the same growth. Returns the task item list."""
    import json
    start, end = month_bounds(prev_month(month))
    rows = db.server_category_production(platform, server_id, start, end)
    growth = _cfg("task_growth", 1.10)
    min_qty = int(_cfg("task_min_qty", 20))
    items = []
    for cat in db.GOOD_CATEGORIES:
        prev_qty = sum(r["qty"] for r in rows if r["category"] == cat)
        qty = max(min_qty, int(math.ceil(prev_qty * growth)))
        items.append({"kind": "category", "key": cat, "qty": qty, "mandatory": True})
    top = db.server_top_goods(platform, server_id, start, end,
                              limit=int(_cfg("task_optional_goods", 3)) + len(db.BASE_GOOD_CODES))
    added = 0
    for r in top:
        if db.is_base_good(r["good_code"]) or added >= int(_cfg("task_optional_goods", 3)):
            continue
        qty = max(max(min_qty // 4, 5), int(math.ceil((r["qty"] or 0) * growth)))
        items.append({"kind": "good", "key": r["good_code"], "qty": qty, "mandatory": False})
        added += 1
    db.save_server_task(platform, server_id, month, json.dumps(items))
    return items

def task_progress(platform, server_id, month):
    """The stored task with live progress: [{kind, key, qty, mandatory, done}].
    Returns None when the month has no task."""
    import json
    task = db.get_server_task(platform, server_id, month)
    if not task:
        return None
    start, end = month_bounds(month)
    rows = db.server_category_production(platform, server_id, start, min(end, int(time.time())))
    out = []
    for item in json.loads(task["spec_json"]):
        if item["kind"] == "category":
            done = sum(r["qty"] for r in rows if r["category"] == item["key"])
        else:
            done = sum(r["qty"] for r in rows if r["good_code"] == item["key"])
        out.append({**item, "done": done})
    return out

def evaluate_task(platform, server_id, month):
    """Mark the month's task completed/failed once it is over. Completed means
    every mandatory item reached its quantity. Returns True/False, or None when
    there was no task."""
    task = db.get_server_task(platform, server_id, month)
    if not task:
        return None
    progress = task_progress(platform, server_id, month)
    completed = all(p["done"] >= p["qty"] for p in progress if p["mandatory"])
    db.set_server_task_completed(task["id"], completed)
    return completed

def evaluate_finished_tasks(now=None):
    """Judge every past month's task that is still open — the provision bonus
    depends on the verdict even on servers that dropped their tasks channel."""
    month = time.strftime("%Y-%m", time.gmtime(now or time.time()))
    for task in db.get_unevaluated_tasks(month):
        evaluate_task(task["platform"], task["server_id"], task["month"])
