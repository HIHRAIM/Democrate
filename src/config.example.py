import os

from env_loader import load_env
load_env()

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

ADMINS = {
    "discord": {ADMINISTRATOR_ID, ADMINISTRATOR_ID},
    "telegram": {ADMINISTRATOR_ID, ADMINISTRATOR_ID}
}

SERVICE_CHATS = {
    "discord": {
        CHAT_ID,
        CHAT_ID,
    },
    "telegram": {
        "CHAT_ID", # Example: -1000000000000:00000
        "CHAT_ID",
    },
}

BACKUP_CHATS = {
    "discord": {
        CHAT_ID,
        CHAT_ID,
    },
    "telegram": {
        "CHAT_ID",
        "CHAT_ID",
    },
}

SUPPORT_CHATS = {
    "discord": {
        CHAT_ID,
    },
    "telegram": {
        "CHAT_ID",
    },
}

# ── Cross-community economy tuning ──────────────────────────────────────────
# Every number here is a knob for the message-economy: how messages turn into
# currency and energy, how crafting mastery grows value and cost, and how the
# daily FX recompute weighs its three factors. See economy.py for the formulas
# that consume them and README.md § Economy for the rationale. Money is always
# stored as an integer number of *minor units* (hundredths); MINOR_UNITS is the
# hundredths-per-unit divisor used for display.
ECONOMY = {
    # Minor units per whole currency unit (2 decimal places).
    "minor_units": 100,

    # ── Message earning (anti-abuse) ──
    # Messages shorter than this many characters are ignored entirely.
    "min_chars": 5,
    # Seconds a user must wait between two *counted* messages.
    "cooldown": 30,
    # A = min(sqrt(chars), sqrt_cap): sublinear reward, capped so one very long
    # message cannot pay out without bound.
    "sqrt_cap": 12.0,
    # Ceiling on the total activity A a single user may bank in one rolling hour.
    "hourly_activity_cap": 90.0,
    # Currency minted per 1.0 of activity, before the channel's own rate factor.
    "currency_per_activity": 25,   # → 0.25 currency units per point of activity
    # Energy granted per 1.0 of activity (the crafting resource).
    "energy_per_activity": 10,
    # Energy handed to a person on the first account they ever open, so that
    # their first /craft works immediately instead of after a quarter of an
    # hour of writing messages at the bot. It has to cover one whole run of the
    # bulkiest base category, which is base_good_energy times that category's
    # yield (8 x 3 for food) — a grant that covers only part of a run buys
    # nothing at all. 0 switches it off.
    "starter_energy": 30,

    # ── Crafting mastery ──
    # Quality/value growth. Unit value = base_value * (1 + alpha * sqrt(m)),
    # where m is how many of the good the crafter has ever produced.
    "alpha": 0.05,
    # Craft-cost growth, as a fraction of value growth:
    # cost = energy_cost * (1 + mastery_cost_weight * alpha * sqrt(m)).
    # Cost follows the same curve as value and at a fraction of it, so value
    # per unit of energy rises all the way up the ladder. Below 1.0 skill pays
    # off, at 1.0 it is exactly neutral, above it every hour of practice makes
    # the crafter worse off — which is what a cost curve of a different shape
    # from the value curve did.
    "mastery_cost_weight": 0.7,
    # The top quality level. Mastery stops there: the level, the unit value and
    # the craft cost all freeze once it is reached, so a good has a best
    # possible quality rather than an endless creep.
    "mastery_max_level": 10,
    # How many units one level step costs at the bottom of the curve. The whole
    # ladder is 511 * mastery_scale units long, so this is the one number that
    # decides how long the top level takes: at 6, a 24/7 /autocraft line fed by
    # a moderately active earner reaches level 10 in about a month.
    "mastery_scale": 6,
    # Safety ceiling on how many units one autocraft run may produce per tick.
    "autocraft_cap": 50,
    # How many goods one person may have in production at the same time —
    # manual /craft runs and 24/7 /autocraft lines are counted separately, and
    # each is capped here. The output is divided between them: five goods at
    # once produce the same total as one, in five streams.
    "parallel_production": 5,

    # ── Foreign exchange (daily recompute) ──
    # value(bank) = max(backing * activity, fx_min_value), where
    #   backing  = period_goods_value / max(money_supply, minor_units)
    #   activity = 1 + fx_activity_weight * ln(1 + period_energy)
    # The published value may move at most ±fx_daily_clamp per day.
    "fx_daily_clamp": 0.10,     # ±10 % per day
    "fx_activity_weight": 0.10,
    "fx_min_value": 0.01,
    # Starting value of a freshly created currency.
    "fx_initial_value": 1.0,
    # Conversion spread taken as a sink on /convert (2 %).
    "convert_fee": 0.02,

    # ── Timed production ──
    # Energy cost of one unit of a base-category good (🥕🧼👘💊🧺).
    "base_good_energy": 8,
    # One manual /craft run: duration = produce_base_sec * category_time_mult
    # * (1 + produce_level_time * (level - 1)), clamped to
    # [produce_min_sec, produce_max_sec] (the ~5-minute ceiling).
    "produce_base_sec": 45,
    "produce_min_sec": 10,
    "produce_max_sec": 300,
    "produce_level_time": 0.15,
    # Units per run = category_yield * (1 + produce_level_yield * (level - 1)).
    "produce_level_yield": 0.10,
    # 24/7 autoproduction speed as a share of the manual tempo (< 1: the line
    # runs day and night but slower than a working person).
    "auto_efficiency": 0.5,
    # Now and then a manual run comes out better than it should have: this is
    # the chance of it and what it multiplies the batch by. It costs no extra
    # energy and applies to /craft only, never to a 24/7 line — the surprise
    # belongs to the run somebody chose to start. 0 switches it off.
    "lucky_batch_chance": 0.05,
    "lucky_batch_multiplier": 2.0,
    # Per-category tempo and batch size ("default" covers uncategorised goods).
    "category_time_mult": {
        "food": 0.8, "hygiene": 0.9, "wardrobe": 1.2,
        "pharmacy": 1.5, "household": 1.0, "default": 1.0,
    },
    "category_yield": {
        "food": 3, "hygiene": 2, "wardrobe": 1,
        "pharmacy": 1, "household": 2, "default": 1,
    },

    # ── Provision (weekly server supply metric) ──
    # Weekly per-person need, in units, per category (before the weight).
    "prov_units_per_person": 5.0,
    # Daily per-person need used by the consumption tick, which eats that much
    # out of the warehouses of the server's enterprises every day and pays for
    # what it takes. Defaults to prov_units_per_person / 7 when absent, so the
    # week's demand matches the weekly figure above; raise it to make servers
    # hungrier than they produce.
    "prov_daily_units_per_person": 5.0 / 7.0,
    # Provision weight bonus per quality level above 1 for categorised
    # user-created goods (base goods always count at 1.0).
    "prov_quality_bonus": 0.25,
    # Category score ceiling once custom goods feed it (base-only production
    # is always capped at 1.0 — "acceptable").
    "prov_max_score": 2.0,
    # Provision multiplier bonus while the previous month's task is completed.
    "prov_task_bonus": 0.10,
    # How much of each category a person is assumed to need, relative to food.
    "prov_category_weight": {
        "food": 1.0, "hygiene": 0.7, "wardrobe": 0.5,
        "pharmacy": 0.6, "household": 0.7,
    },

    # ── Monthly server tasks ──
    # Required quantity = max(task_min_qty, last month's output * task_growth).
    "task_growth": 1.10,
    "task_min_qty": 20,
    # How many of the server's own goods join the task as optional items.
    "task_optional_goods": 3,

    # ── Enterprise export logistics ──
    # An export is a shipment that spends time in transit before it arrives.
    # Its duration is chosen by the "distance" between the two enterprises'
    # servers: same server/group (local) is near-instant; different servers of
    # the same union take transport_cross_server_sec; different unions take
    # transport_cross_union_sec (which MUST be greater than cross-server).
    # transport_per_unit_sec optionally lengthens a shipment by its size.
    "transport_local_sec": 5,
    "transport_cross_server_sec": 300,      # ~5 minutes within a union
    "transport_cross_union_sec": 1800,      # ~30 minutes between unions
    "transport_per_unit_sec": 0,            # extra seconds per unit shipped
    # Spoilage is a risk, not a toll. On the longest haul a perishable category
    # runs this chance of losing part of the batch; a shorter trip runs a
    # proportionally smaller one, and a durable category (wardrobe, hygiene,
    # household — absent here) runs none at all.
    "transport_perish_chance": {"food": 0.35, "pharmacy": 0.25},
    # When the risk does come true, at most this share of the batch is lost,
    # and never the whole of it — at least one unit always arrives.
    "transport_perish_max_share": 1.0 / 3.0,
}
