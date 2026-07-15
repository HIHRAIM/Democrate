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

    # ── Crafting mastery ──
    # Quality/value growth. Unit value = base_value * (1 + alpha * sqrt(m)),
    # where m is how many of the good the crafter has ever produced.
    "alpha": 0.05,
    # Craft-cost growth. cost = energy_cost * (1 + beta * quality_level), where
    # quality_level = 1 + floor(log2(m + 1)). Rising cost keeps value finite.
    "beta": 0.35,
    # Safety ceiling on how many units one autocraft run may produce per tick.
    "autocraft_cap": 50,

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
}
