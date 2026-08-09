"""Cross-community economy: the platform-neutral rules that both bots share.

`db/` holds the storage and the atomic money primitives; this package holds the
*policy* built on top of them — how a message becomes currency and energy, how
crafting mastery grows a good's value and its craft cost, how timed production
runs and 24/7 autoproduction turn energy into goods for people and enterprises,
how the daily foreign-exchange recompute prices each currency, how a server's
provision and GDP are measured and its monthly tasks generated, and how the
daily / weekly / monthly schedulers mint wages, pay enterprise salaries, collect
party dues, run exports and autosend. Everything display-related (embeds, HTML,
localization) stays in the bots; the helpers here return plain numbers and short
status strings.

Money is always an integer number of *minor units* (hundredths). The tuning
constants all live in `config.ECONOMY`.

This package replaces the old monolithic economy.py, split by domain — see each
submodule's docstring — with the interface unchanged: everything is re-imported
here, so call sites keep saying ``economy.craft_cost(...)``.

The import order at the bottom is the dependency order, from the arithmetic
outwards: money → activity → production → trade → logistics → tasks →
consumption → payroll. `payroll` comes last because the three schedulers call
into all of the others, and `consumption` follows `tasks` because the provision
score reads last month's task verdict. There are no cycles here and none should
be introduced; re-export new helpers by name, never ``import *``.

`logger`, and the imports of logging/math/re/time/db/ECONOMY below, are part of
the package's public surface as well: the flat economy.py exposed them, so
dropping them here would quietly delete names from ``dir(economy)``.
"""
import logging
import math
import re
import time

import db
from config import ECONOMY

logger = logging.getLogger("dem.economy")

from economy.money import (
    CURRENCY_CODE_RE,
    ENERGY_EMOJI,
    craft_cost,
    format_amount,
    format_energy,
    format_money,
    mastery_level,
    parse_amount,
    parse_salary,
    unit_value,
)
from economy.activity import (
    earn_from_message,
    message_activity,
)
from economy.production import (
    batch_value,
    category_time_mult,
    category_yield,
    energy_bank_for,
    finish_production,
    produce_params,
    production_energy_cost,
    run_autoproduce,
    run_autosend,
    start_production,
)
from economy.trade import (
    can_manual_peg,
    convert,
    convertible,
    rate,
    recompute_value,
    sell,
)
from economy.logistics import (
    arrive_shipment,
    dispatch_shipment,
    enterprise_union,
    perished_qty,
    run_auto_exports,
    transport_distance,
    transport_time,
)
from economy.tasks import (
    evaluate_finished_tasks,
    evaluate_task,
    generate_monthly_task,
    month_bounds,
    prev_month,
    task_progress,
)
from economy.consumption import (
    PROVISION_LEVELS,
    consume_for_server,
    daily_need,
    provision_level,
    provision_report,
    run_consumption,
    server_gdp,
)
from economy.payroll import (
    resolve_salary,
    run_daily_tick,
    run_enterprise_salaries,
    run_monthly_tick,
    run_party_dues,
    run_wages,
    run_weekly_tick,
)
