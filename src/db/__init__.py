"""The bot's single SQLite connection and the public db API.

This package replaces the old monolithic db.py. The split is by domain —
see each submodule's docstring — but the *interface* is unchanged: every
helper is re-imported here, so call sites keep saying ``db.get_bank(...)``
and ``db.cur.execute(...)`` exactly as before.

Connection model: one process-wide connection in WAL mode, wrapped in
_LockingConnection so the Discord half, the Telegram half and the background
loops can use it concurrently. ``cur`` is an alias of ``conn`` — the facade
returns a fresh cursor from every execute(), so chained fetches never share
cursor state between threads. The database file is opened by RELATIVE path:
the process must run with cwd = src/ (main.py and the control panel both do).

Import order at the bottom matters, twice over. Submodules do ``from db
import conn, cur`` against this partially-initialized module, which works
only because conn/cur/_now are defined above those imports; and a submodule
that imports another at module level must come after it (schema before
schema_economy, users before quizzes). Cycles between submodules are broken
the way the whole project breaks them — an import at the call site, inside the
function (db/banks.py: delete_bank reaching for _refund_shipment). Keep new
submodule imports at the bottom, and re-export new helpers here by name:
never ``import *``, which would swallow a typo in silence.

The four stdlib imports below are part of the package's public surface as
well: the flat db.py exposed them, so removing them here would quietly delete
names from ``dir(db)`` that something may be reading.
"""
import random
import sqlite3
import threading
import time

_db_lock = threading.RLock()
_raw_conn = sqlite3.connect("dem.db", check_same_thread=False)
_raw_conn.execute("PRAGMA journal_mode=WAL;")
_raw_conn.execute("PRAGMA synchronous=NORMAL;")
_raw_conn.row_factory = sqlite3.Row

class _LockingConnection:
    """Thread-safe facade over sqlite3.Connection.

    `execute()` returns a brand new cursor on every call, so chained
    `.fetchone()/.fetchall()/.lastrowid` always operate on a private cursor.
    All access is guarded by a re-entrant lock to make concurrent use from the
    Telegram bot, the Discord bot and the background loops safe.
    """

    def __init__(self, raw_conn, lock):
        """Wrap `raw_conn`; every locking method below shares `lock`."""
        self._conn = raw_conn
        self._lock = lock

    def execute(self, sql, params=()):
        """Locked sqlite3.Connection.execute — returns a fresh cursor."""
        with self._lock:
            return self._conn.execute(sql, params)

    def executescript(self, sql):
        """Locked executescript, used only by the schema DDL."""
        with self._lock:
            return self._conn.executescript(sql)

    def commit(self):
        """Locked commit. Every writer in db/ calls this itself, so a caller
        never has to think about transactions."""
        with self._lock:
            return self._conn.commit()

    def __getattr__(self, name):
        """Everything else (row_factory, backup, …) passes through unlocked —
        acceptable because nothing mutates connection state at runtime."""
        return getattr(self._conn, name)

conn = _LockingConnection(_raw_conn, _db_lock)
cur = conn

def _now():
    """Unix seconds as an int — the one clock every table's timestamps use.

    Defined here rather than in a domain module because every submodule needs
    it and none of them owns it."""
    return int(time.time())

from db.schema import (
    init,
    init_olympiad,
)
from db.schema_economy import (
    BASE_GOODS,
    BASE_GOOD_CODES,
    GOOD_CATEGORIES,
    init_economy,
    is_base_good,
)
from db.unions import (
    add_union,
    all_chats,
    code_taken,
    currency_code_taken,
    enterprise_code_taken,
    generate_code,
    get_chat,
    get_telegram_group_ids,
    get_union_chats,
    get_union_codes,
    get_union_name,
    get_union_names,
    good_code_taken,
    is_setup,
    remove_chat,
    setup_chat,
    union_exists,
)
from db.settings import (
    add_loc_suggestion,
    cleanup_old_dm_langs,
    cleanup_old_loc_suggestions,
    delete_loc_suggestion,
    get_chat_lang,
    get_loc_suggestion,
    get_setting,
    set_chat_lang,
    set_setting,
)
from db.admins import (
    add_localizer,
    add_server_admin,
    is_localizer,
    is_server_admin,
    remove_localizer,
    remove_server_admin,
)
from db.govt import (
    create_govt_body,
    get_govt_bodies,
    get_govt_body,
    get_govt_members,
)
from db.parties import (
    add_party_leader,
    add_party_member,
    cleanup_old_join_requests,
    create_join_request,
    create_party,
    delete_join_request,
    delete_party,
    find_party,
    find_user_party,
    get_join_request,
    get_open_join_requests,
    get_party,
    get_party_allies,
    get_party_leaders,
    get_party_members,
    get_party_settings,
    get_party_user_set,
    get_random_union_parties,
    get_union_parties,
    has_open_join_request,
    is_founder,
    is_party_leader,
    is_plain_member,
    party_code_taken,
    party_govt_seats,
    remove_party_leader,
    remove_party_member,
    rename_party_code,
    set_parties_enabled,
    set_party_leader,
    update_party_field,
    update_party_logo,
    update_party_union,
    user_led_parties,
)
from db.users import (
    add_fandom_verification,
    add_pending_link,
    canonical_user,
    cleanup_old_pending_links,
    get_fandom_verification,
    get_link_by_discord,
    get_link_by_telegram,
    get_pending_links,
    is_discord_verified,
    is_telegram_verified,
    link_accounts,
    register_link_attempt,
    remove_pending_link,
    user_identities,
)
from db.quizzes import (
    QUIZ_HISTORY_LIMIT,
    QUIZ_PROGRESS_TTL,
    QUIZ_RESULT_TTL,
    cleanup_old_quiz_progress,
    cleanup_old_quiz_results,
    delete_quiz_progress,
    delete_quiz_result,
    delete_user_quiz_results,
    get_blocked_quiz_ids,
    get_last_quiz_result,
    get_latest_quiz_result,
    get_previous_quiz_result,
    get_quiz_progress,
    get_quiz_result,
    get_user_quiz_ids,
    get_user_quiz_results,
    get_user_quiz_results_for,
    is_quiz_compare_blocked,
    prune_quiz_results,
    save_quiz_progress,
    save_quiz_result,
    set_quiz_compare_blocked,
)
from db.foundays import (
    add_wiki_founday,
    delete_guild_wiki_foundays,
    delete_wiki_founday,
    get_wiki_foundays,
    mark_wiki_founday_sent,
)
from db.banks import (
    BANK_EDITABLE_FIELDS,
    account_exists,
    add_bank_leader,
    add_energy,
    add_period_energy,
    add_period_goods_value,
    bank_account_count,
    bank_debt,
    bank_money_supply,
    burn,
    create_bank,
    delete_bank,
    ensure_account,
    get_account,
    get_all_banks,
    get_bank,
    get_bank_accounts,
    get_bank_leaders,
    get_banks_in_chat,
    get_banks_in_union,
    get_energy,
    get_owner_accounts,
    get_transactions,
    is_bank_leader,
    mint,
    party_owner,
    remove_bank_leader,
    rename_bank_code,
    reset_bank_periods,
    set_bank_central_chat,
    set_bank_leader,
    set_bank_prev_value,
    set_bank_value,
    spend_energy,
    transfer,
    update_bank_field,
    user_led_banks,
)
from db.goods import (
    add_inventory,
    add_produced,
    create_good,
    create_production,
    delete_production,
    find_bank_good,
    get_active_production,
    get_autocraft_all,
    get_autosend,
    get_bank_goods,
    get_bank_mastery,
    get_due_productions,
    get_earn_channel,
    get_good,
    get_inventory,
    get_inventory_qty,
    get_produced,
    get_profession,
    remove_earn_channel,
    set_autocraft,
    set_autocraft_carry,
    set_autosend,
    set_earn_channel,
    set_profession,
)
from db.trade import (
    add_treaty,
    all_party_dues,
    banks_with_wages,
    get_bank_wages,
    get_party_dues,
    get_treaty,
    get_wage,
    has_treaty,
    remove_treaty,
    set_party_dues,
    set_pegged_rate,
    set_wage,
    treaty_partners,
)
from db.enterprises import (
    add_enterprise_leader,
    add_enterprise_member,
    add_enterprise_sales,
    create_enterprise,
    delete_enterprise,
    enterprise_owner,
    find_enterprise,
    get_all_enterprises,
    get_enterprise,
    get_enterprise_leaders,
    get_enterprise_member,
    get_enterprise_members,
    get_enterprise_positions,
    get_enterprise_user_set,
    get_personal_salary,
    get_position_salary,
    get_server_enterprises,
    is_enterprise_leader,
    is_enterprise_worker,
    remove_enterprise_leader,
    remove_enterprise_member,
    rename_enterprise_code,
    reset_enterprise_sales,
    set_enterprise_leader,
    set_member_position,
    set_personal_salary,
    set_position_salary,
    update_enterprise_field,
    update_enterprise_logo,
    user_led_enterprises,
    user_member_enterprises,
)
from db.logistics import (
    add_auto_export,
    cleanup_old_shipments,
    create_shipment,
    delete_shipment,
    get_auto_exports,
    get_due_shipments,
    get_enterprise_shipments,
    remove_auto_export,
)
from db.serverstats import (
    all_chat_channels,
    cleanup_old_server_consumption,
    cleanup_old_server_production,
    cleanup_old_server_tasks,
    enterprise_category_stock,
    get_chat_channel,
    get_server_task,
    get_unevaluated_tasks,
    record_server_consumption,
    record_server_production,
    remove_chat_channel,
    save_server_task,
    server_active_producers,
    server_category_production,
    server_consumption_window,
    server_good_qty,
    server_top_goods,
    set_chat_channel,
    set_chat_channel_marker,
    set_server_task_completed,
)
from db.onboarding import (
    forget_deadline,
    get_deadline_row,
    get_pending_deadlines,
    mark_settled,
    record_join,
    rule_since,
)
from db.olympiad import (
    add_candidate,
    add_contest,
    add_vote,
    approved_chat_for,
    cleanup_old_wiki_accounts,
    clear_olympiad,
    clear_olympiad_data,
    delete_candidate,
    delete_vote,
    get_candidate,
    get_candidates,
    get_contest,
    get_contest_names,
    get_contests,
    get_olympiad,
    get_user_vote,
    get_vote,
    get_wiki_account,
    known_fandom_name,
    set_approved_chat_for_lang,
    set_contest_name,
    set_olympiad,
    set_wiki_account,
    update_candidate,
)
