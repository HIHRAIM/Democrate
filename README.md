# Democrate

Democrate is a cross-platform coordination bot for **unions** of wiki communities that runs on Discord and Telegram. It keeps a registry of unions and the servers/groups bound to them, hosts each union's **political parties** and **governing bodies**, verifies users through their **Fandom** profiles, runs ideology **quizzes**, keeps each union's **wikis** with their administrations, congratulates wikis and members on their **birthdays**, and powers a cross-community **economy**: banks with their own currencies, message-based earning, timed **production** of goods owned by people and **enterprises**, salaries, fines, party dues, currency exchange, exports between enterprises, a per-server **provision** metric with weekly statistics and monthly production tasks. All replies are localized into six languages (en, ru, uk, pl, es, pt).

## Sponsorship

Bot Admins can manage any union. A sponsor can create and manage only their own unions and register only communities they administer. `/union edit` lets a sponsor rename their union, change its logo, language and creation date, or offer it to another active sponsor; the transfer happens when that sponsor accepts and their subscription has room for one more union and for all of its communities. A linked Telegram account shares the Discord subscription and its limits, rather than receiving another allowance. Link the accounts with the existing `/account telegram` and `/account_discord` commands, then use `/sponsor info` on either platform to inspect usage.

| One subscription | Community role | €1 | €3 | €5 |
|---|---:|---:|---:|---:|
| Unions | 1 | 1 | 3 | 5 |
| Discord servers and Telegram groups combined | 2 | 2 | 6 | 10 |
| Messages earning currency per minute | 60 | 60 | 180 | 300 |
| Messages earning currency per UTC day | 3000 | 3000 | 9000 | 15000 |

The earning budget is shared across all banks in the sponsor's unions. Either community role on the Patreon server grants the €1 allowance after seven days of continuous observed membership, with one day of continued work after loss. Paid roles have 30 days of continued work after loss. Thirty days after the applicable working grace ends, communities without a replacement sponsor or Bot Admin are erased and the bot leaves. A union whose last community departs becomes a read-only economy archive for 70 days; its currency, balances, goods and related records are then erased, including balances held outside the former union. Task history is also retained for 70 days: only the current and previous months are used in progress calculations.

Active sponsors can use `/sponsor limit` on Discord or their linked Telegram account to see current union/community slots and currency-award message usage for the UTC day and current UTC minute. Linked accounts share the same counters. Daily activity capacity resets at 00:00 UTC. Working grace is included; Bot Admin status alone does not grant access to this command.

## Requirements

- Python **3.10+** (recommended 3.11+)
- A Discord bot token
- A Telegram bot token
- SQLite (uses local `dem.db`, no external DB required)
- Python packages used by the project:
  - `discord.py`
  - `aiogram`
  - `aiohttp`

## Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/HIHRAIM/Democrate
   cd Democrate
   ```

2. **Create and activate a virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install discord.py aiogram aiohttp
   ```

4. **Create config file**
   - Copy `src/config.example.py` to `src/config.py`.
   - Set environment variables (the config reads tokens from env), or copy `src/.env.example` to `src/.env` and fill it in — the config loads it automatically (already-set environment variables take precedence):
     - `DISCORD_BOT_TOKEN` — your Discord bot token.
     - `TELEGRAM_BOT_TOKEN` — your Telegram bot token.
     - `BACKUP_KEY` — passphrase used to encrypt automatic database backups.
   - Edit `config.py`:
     - `ADMINS["discord"]` and `ADMINS["telegram"]` — sets of numeric user IDs with global **Bot Admin** rights.
     - `SERVICE_CHATS` — chats that receive startup/shutdown and administrative events. Telegram format: `"-1000000000000:0"` (chat_id:thread_id); Discord format: numeric channel ID.
     - `BACKUP_CHATS` — chats that receive encrypted database backups every 12 hours. Same format.
     - `SUPPORT_CHATS` — chats that receive localization suggestions from `/locale suggest`. Same format.
     - `ECONOMY` — the economy's tuning constants (documented inline): message-earning anti-abuse knobs (`min_chars`, `cooldown`, `sqrt_cap`, `hourly_activity_cap`, `currency_per_activity`, `energy_per_activity`, `starter_energy`), crafting-mastery curve (`alpha`, `mastery_cost_weight`, `mastery_max_level`, `mastery_scale`, `autocraft_cap`, `parallel_production`), foreign-exchange behavior (`fx_daily_clamp`, `fx_activity_weight`, `fx_min_value`, `fx_initial_value`, `convert_fee`), timed-production balance (`base_good_energy`, `produce_base_sec`, `produce_min_sec`, `produce_max_sec`, `produce_level_time`, `produce_level_yield`, `auto_efficiency`, `lucky_batch_chance`, `lucky_batch_multiplier`, `category_time_mult`, `category_yield`), the provision model (`prov_units_per_person`, `prov_daily_units_per_person`, `prov_quality_bonus`, `prov_max_score`, `prov_task_bonus`, `prov_category_weight`), monthly-task generation (`task_growth`, `task_min_qty`, `task_optional_goods`) and export logistics (`transport_local_sec`, `transport_cross_server_sec`, `transport_cross_union_sec`, `transport_per_unit_sec`, `transport_perish_chance`, `transport_perish_max_share`).

   > **Message Content intent:** message earning, the interactive dialogs and the numbered menus read ordinary messages, which requires the privileged **Message Content Intent** — enable it for the bot in the Discord Developer Portal, otherwise the bot will not start.

5. **Run the bot**
   ```bash
   cd src && python main.py
   ```
   The working directory must be `src/`: the database and `.env` are opened by
   relative path, and the control panel launches the bot exactly that way.

### Running as a service

The bot is meant to run unattended, and the recommended way is a `systemd`
unit with `WorkingDirectory=` pointing at `src/`. Its output goes to the
journal — `journalctl -u dem_bot -f` — because nothing in the code opens a log
file: `logging.basicConfig` in `src/main.py` configures the standard-error
handler and nothing else.

`src/bot_err.log` and `src/bot_run.log` are **leftovers of an older,
shell-redirected launch**. No part of the bot writes to them any more; they are
ignored by git and can be deleted.

Stopping is orderly. aiogram answers `SIGTERM` by stopping its polling, and
`main()` ends on the first of its nine tasks to finish, cancels the other
eight, tells the service chats that the bot is stopping and closes both
clients. `TimeoutStopSec=` in the unit can therefore stay at its default:
the process no longer has to be killed.

---

## Project structure

The code lives in `src/`, split into packages by domain. `ARCHITECTURE.md` describes how the pieces work together and carries a feature → file table.

```
src/
  main.py              entry point: both bots plus the cross-platform loops
                       (economy ticks, production, shipments, scheduled posts,
                       the Olympiad, retention, the setup deadline, the
                       celebrations and the wiki refresh)
  config.py            this deployment's ids and the economy's tuning constants
                       (untracked; config.example.py is the template)
  env_loader.py        reads src/.env
  utils.py             localization runtime, role checks, parsers, rate limiter,
                       service events
  quizzes.py           the quiz engine (its data lives in src/quizzes/)
  olympiad.py          the Olympiad engine and its shared voting dialog
  stats.py             weekly statistics, monthly task posts, FX log lines,
                       the union profile
  message_relay.py     markup conversion between Discord and Telegram
  fandom.py            Fandom profile lookup for /account verify
  wikis.py             reading union wikis: link → wiki, founding date,
                       administration, Discord handles; the twice-daily refresh
  celebrations.py      birthdays and wiki birthdays: the clock, the greeting
                       choice, the wording
  setup_deadline.py    leaves communities nobody bound to a union
  backup_crypto.py     encrypted database snapshots
  restore_backup.py    their restore tool

  db/                  SQLite layer: one connection, one module per domain
    __init__.py          connection (conn/cur), init(), the whole public API
    schema.py            core CREATE TABLEs, migrations, the Olympiad schema
    schema_economy.py    the economy's schema, column migrations, base goods
    unions.py   settings.py   admins.py    users.py
    parties.py  govt.py       quizzes.py   wikis.py
    community.py             birthdays.py
    banks.py    goods.py      trade.py     enterprises.py
    logistics.py             serverstats.py
    onboarding.py        join times and the setup-deadline bookkeeping

  economy/             the rules: money, goods, production, trade
    money.py             amounts, mastery, unit value, craft cost
    activity.py          earning from messages
    production.py        timed production, autocraft, autosend
    trade.py             selling between holders, currency values, rates, conversion
    payroll.py           the daily/weekly/monthly ticks, wages, dues, salaries
    logistics.py         distance, transit time, spoilage risk, shipments
    consumption.py       the daily meal, provision, GDP
    tasks.py             monthly task generation and evaluation

  discord_bot/         the Discord half
    client.py            the client, its loops, the shared permission gates
    groups.py            the command groups (/bank, /party, …) and their contexts
    events.py            the three @bot.event handlers
    dialogs.py           interactive dialogs and consent views
    parties.py           party cards and edit menus (shared with the TG half)
    olympiad.py          dynamic commands, review embeds, verdicts
    celebrations.py      greeting posts, the birthday and representative roles
    commands/            slash commands: unions, settings, admins, locale, user,
                         parties, govt, quizzes, moderation, wikis, birthdays,
                         banks, trade, goods, enterprises, servers, sponsors

  telegram_bot/        the Telegram half, mirroring the Discord one
    client.py            bot/dispatcher/router, the command-spelling middleware, resolvers
    dialogs.py           the pending-answer registries and consent keyboards
    callbacks.py         the @router.callback_query handlers (inline buttons)
    parties.py           party card and edit menu, Telegram rendering
    olympiad.py          the Telegram Olympiad gates and dialog
    celebrations.py      birthday greetings in groups
    commands/            the same domains as the Discord side
    catchall.py          the unfiltered handler — imported last, always

  i18n/                the six localization files, plus olympiad/ and quizzes/
  quizzes/             quiz data: <quiz id>/scoring.json
```

---

## Commands

Commands are grouped by subject. On Discord every group is one slash command with subcommands — `/bank pay`, `/party join`, `/union info` — so typing `/bank` lists every banking action with its description; only `/help` and `/calendar` stand alone. Discord accepts at most 100 top-level commands per application, and the groups keep Democrate at twenty. On Telegram a command cannot contain a space, so the same action is written with an underscore — `/bank_pay`, `/party_join`, `/party_edit_admin` for `/party edit-admin` — and the hyphenated spelling (`/bank-pay`) works too. The names Telegram used before the groups (`/pay`, `/balance`, `/create_bank`, …) remain accepted as aliases. The bot's replies mention commands in the spelling of the platform they are read on.

Permission roles used below:

- **Everyone** — any user in a set-up chat (some commands additionally require [verification](#verification)).
- **Server Admins** — users explicitly appointed with `/admin add` for this community. Discord and Telegram administrator permissions alone grant no additional bot rights.
- **Localizers** — users granted `/localizer add`: they may edit this bot's localization through the [control panel](https://github.com/HIHRAIM/Confederate-Panel). Delegated Server Admins hold the status implicitly while they remain admins.
- **Bot Admins** — user IDs listed in `config.py`.
- **Party Leaders** — a party's founder and appointed leaders.
- **Bank Leaders** — a bank's appointed leaders (need not be admins); they manage it through `/bank edit`.
- **Enterprise Leaders** — an enterprise's founder and appointed leaders.

### Discord commands

| Command | Purpose | Everyone | Server Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/account verify <fandom_name>` | Verify by matching your Discord handle on your Fandom profile | ✅ | ✅ | ✅ |
| `/account telegram <nickname>` | Link your Telegram account (run `/account_discord` with your Discord name on Telegram within 30 min); the Telegram account's money, goods and mastery move to the Discord account | ✅ | ✅ | ✅ |
| `/account privacy` | Inspect and delete the data the bot keeps about you, and hide yourself from the `/good top` leaderboards | ✅ | ✅ | ✅ |
| `/party info <code\|name>` | A party's card: texts, logo, leaders, members, seats, bank balances | ✅ | ✅ | ✅ |
| `/govt info <name>` | A governing body's members with party affiliations | ✅ | ✅ | ✅ |
| `/party add [founder]` | Found a party (interactive dialog; requires a union-configured role; verified) | ✅ | ✅ | ✅ |
| `/party edit [option]` | Manage your party: texts, logo, color, code, leaders, transfer, suspension | ✅ | ✅ | ✅ |
| `/party rules [option]` | Party rules (member invites toggle) | ✅ | ✅ | ✅ |
| `/party join <code\|name>` | Ask to join a party (leaders approve via DM buttons) | ✅ | ✅ | ✅ |
| `/party leave` | Leave your party | ✅ | ✅ | ✅ |
| `/party kick <member>` | Remove a member from your party (party leaders) | ✅ | ✅ | ✅ |
| `/quiz take` | Take an ideology quiz (works in DMs too) | ✅ | ✅ | ✅ |
| `/quiz stop` · `/quiz clear` · `/quiz compare <user>` · `/quiz previous [n]` | Pause a quiz / delete stored results / compare with another user / compare with your previous attempt | ✅ | ✅ | ✅ |
| `/union info [code\|name]` | A union's public profile, found by its code or a name in any of its languages (near matches are offered; a query resembling nothing finds nothing): language, creation date, communities per platform, wikis and the size of their administrations, parties and governing bodies, banks, accounts, enterprises, goods — with buttons for the full, numbered lists and the union's economic summary | ✅ | ✅ | ✅ |
| `/server info` | This server's profile: union, language, timezone, its banks, the currencies in use, accounts, enterprises, community and bank taxes | ✅ | ✅ | ✅ |
| `/birthday set [day] [month] [clear]` | Set, show or forget your birthday (day and month; linked accounts share it) | ✅ | ✅ | ✅ |
| `/calendar` | The coming month of union and wiki founding days and members' birthdays; buttons turn the page by a month, hide a kind of event, or switch between this community and every community | ✅ | ✅ | ✅ |
| `/locale info [code]` · `/locale compare <code>` · `/locale suggest <lang> <code> <text>` | Localization status / cross-language comparison / suggestions | ✅ | ✅ | ✅ |
| `/help` | Paged command reference | ✅ | ✅ | ✅ |
| `/sponsor info` · `/sponsor limit` | Your subscription, its unions and community slots / today's usage (sponsors) | ✅ | ✅ | ✅ |
| `/server lang <code>` · `/server lang-channel <code>` | Server-wide / per-channel bot language (in a DM, `/server lang` sets the language of that conversation for anyone) | ❌ | ✅ | ✅ |
| `/find with` · `/find without <channel> <period> <keywords>` | Reply-mention authors of messages (not) containing keywords (verified) | ❌ | ✅ | ✅ |
| `/server logs [channel] [day] [HH:MM] [tz]` | Economic log channel: daily FX updates + weekly server statistics (`off` unbinds) | ❌ | ✅ | ✅ |
| `/server tasks [channel] [HH:MM] [tz]` | Channel for the monthly production task, posted on the 1st (`off` unbinds) | ❌ | ✅ | ✅ |
| `/server edit [option]` | Numbered menu: the community tax and the server's timezone | ❌ | ✅ | ✅ |
| `/birthday channel [channel\|off] [tz]` | Channel where members are congratulated on their birthday at 10:00 server time | ❌ | ✅ | ✅ |
| `/birthday role <role\|off> [tz]` | Role members hold through their birthday | ❌ | ✅ | ✅ |
| `/admin add <user>` · `/admin remove <user>` | Grant/revoke delegated Server Admin rights (ping or ID) | ❌ | ❌ | ✅ |
| `/localizer add <user>` · `/localizer remove <user>` | Grant/revoke Localizer status — localization editing in the control panel (ping, ID or username); DMs the user | ❌ | ❌ | ✅ |
| `/server setup <union> <lang>` | Bind this server to a union (sponsors: their own unions, communities they administer) | ❌ | ❌ | ✅ |
| `/union add <code> <names…>` | Create a union with localized names (sponsors within their subscription) | ❌ | ❌ | ✅ |
| `/union edit [union] [option]` | Numbered menu: names, logo, owner, language, creation date. Sponsors may edit their own unions | ❌ | ❌ | ✅ |
| `/wiki add <union> <link>` · `/wiki remove <link> [union]` | Attach a wiki to a union (its name, language, founding date and administration are read at once) / detach it | ❌ | ❌ | ✅ |
| `/wiki founded <link> <DD.MM.YYYY\|auto>` | Correct a wiki's founding date, or go back to the detected one | ❌ | ❌ | ✅ |
| `/wiki channel [channel\|off] [tz]` · `/wiki greeting <link> <true/false>` | This server's wiki birthday channel / greet or skip one wiki here | ❌ | ❌ | ✅ |
| `/wiki rep-role <role\|off>` | Role for the union wikis' long-standing bureaucrats, administrators and content moderators on this server | ❌ | ❌ | ✅ |
| `/union parties <union> <enable\|disable\|add\|remove> [roles]` | Turn parties on/off in a union, and add or remove single roles whose holders may found one (mentions or IDs); sponsors for their own unions | ❌ | ❌ | ✅ |
| `/govt add <union> <name> <singular> <plural>` | Create a governing body | ❌ | ❌ | ✅ |
| `/party edit-admin <party>` | Edit/move/delete any party of any union | ❌ | ❌ | ✅ |
| `/locale reply <code> <text>` | Reply to a localization suggestion | ❌ | ❌ | ✅ |
| `/bot chats` · `/bot leave <platform> <id>` | List all bound chats / force-leave one | ❌ | ❌ | ✅ |
| `/bot backup` | Get an encrypted database backup | ❌ | ❌ | ✅ |

#### Discord Olympiad commands

Only `/olympiad set` is always present. The rest are added to the `/olympiad` group when an Olympiad is opened and taken out of it when the voting period ends, so they never appear for an event that is not running — see [Olympiad](#olympiad).

| Command | Purpose | Who |
|---|---|---|
| `/olympiad set <MM-DD-YYYY> <MM-DD-YYYY>` | Open an Olympiad and its voting period (`off` cancels it and deletes its data) | Bot Admins |
| `/olympiad contest-add` | Create a contest: language category → name(s) → review chat id → accepted-votes chat id (interactive dialog) | Bot Admins |
| `/olympiad contest-edit` | Add, change or remove a contest's candidate wikis (interactive dialog) | Bot Admins |
| `/olympiad vote` | Vote — private chat with the bot only (interactive dialog) | Everyone |
| `/olympiad accept <key> <wiki username> [remember]` | Accept a vote under review; `remember` also links that wiki account to the voter | In the contest's review chat |
| `/olympiad deny <key> <reason>` | Reject a vote under review; the voter is told which wikis and why | In the contest's review chat |

#### Discord economy commands

| Command | Purpose | Who |
|---|---|---|
| `/bank add` | Found a bank and its currency (interactive dialog: central server → currency name → 4-char code → emoji) | Bot/Server Admins |
| `/bank info <code>` | A bank's card: currency, union, central server, leaders, money supply, accounts, debt, value | Everyone |
| `/bank leader <code> <user>` | Offer co-leadership of a bank (consent buttons; Bot Admins appoint directly) | Bank Leaders / Bot Admins |
| `/bank transfer <code> <user>` | Offer the bank's leadership (consent buttons; Bot Admins appoint directly) | Bank Leaders / Bot Admins |
| `/bank edit [option] [code]` | Numbered management menu: currency name, emoji, 4-char code, central server, conversion fees, leaders, transfer, deletion, bank tax, audit channel, forms of the currency name per language. Bot Admins may edit any bank, leaders only their own | Bank Leaders / Bot Admins |
| `/bank emit <target> <amount> <code> [reason]` | Issue the currency to a person, or to a party or enterprise of the union by code; leaders within a monthly limit | Bank Leaders / Bot Admins |
| `/bank open <code>` | Open an account (also opened automatically on first earning) | Everyone |
| `/bank balance [code]` | Your money, energy, profession and place on the leaderboard — all banks, or one | Everyone |
| `/bank transactions [code]` | Your own transaction history, newest first (only you see it) | Everyone |
| `/bank pay <user> <amount> <code>` | Transfer money to another user (same currency, atomic); the recipient pays the community and bank taxes out of it | Everyone |
| `/bank earn <code> <on\|off> [rate]` | Make the current channel earn a bank's currency; threads, forum posts and Telegram topics inside it inherit the binding unless they name their own (every reply is ephemeral — on Telegram, self-deleting). A rate above 1 only for the currency's leaders, Bot Admins and the Server Admins of the bank's central server | Bank Leaders / Server Admins / Bot Admins |
| `/bank earn-info [code]` | Where currency is earned: each channel's place, currency, multiplier, community tax, messages counted and currency minted over seven days, who switched it on and when — a bank's leaders see its channels everywhere, Server Admins the channels of this server, Bot Admins all (only the caller sees it) | Bank Leaders / Server Admins / Bot Admins |
| `/bank profession <user> <good_code>` | Override a member's profession in your bank | Bank Leaders |
| `/bank wage <profession> <amount>` | Monthly wage for a profession in your bank (0 clears) | Bank Leaders |
| `/bank fine <user> <amount> [reason]` | Fine a user in your bank's currency (balance may go negative — a debt) | Bank Leaders |
| `/bank treaty <code>` | Propose a conversion treaty with another bank (their leader accepts via buttons) | Bank Leaders |
| `/bank rate <code> <rate>` | Fix a pegged rate — only with your bank's **sole** treaty partner | Bank Leaders |
| `/bank convert <amount> <from> <to>` | Convert between your own accounts at the current rate (fee applies) | Everyone |
| `/bank rates [code]` | Currency values, or one bank's conversion rates | Everyone |
| `/good add <name> <base_value> <energy_cost> [emoji] [category] [currency] [enterprise]` | Create a good owned by you (or your enterprise), denominated in a bank of this union, optionally tied to a base category | Everyone |
| `/good info` | The goods available here: the five base categories plus the union's market goods | Everyone |
| `/good craft <good_code> [enterprise]` | Start a timed production run (a few minutes); the batch lands in your — or your enterprise's — inventory | Everyone |
| `/good inventory` | Your goods with quantities, quality levels and unit values (base goods show no price) | Everyone |
| `/good top [week\|good\|wealth] [code]` | Leaderboards: this week's producers on this server, a good's masters of all time, a currency's largest holdings | Everyone |
| `/good sell <good_code> <buyer> [qty] [price]` | Sell goods to a member or an enterprise; the buyer accepts and pays from their account. Base goods are not sellable | Everyone |
| `/good give <user> <good_code> [qty]` | Hand goods from your inventory to another user | Everyone |
| `/good autocraft <good_code> <on\|off> [enterprise]` | Continuous 24/7 autoproduction — slower than manual runs, up to five lines sharing the tempo, halts without energy | Everyone |
| `/good autosend <good_code> <percent> <user\|party\|enterprise>` | Auto-send a share of a good's stock daily | Everyone |
| `/party dues <code> <amount>` | Your party's monthly member dues in a currency (0 clears) | Party Leaders |

#### Discord enterprise commands

| Command | Purpose | Who |
|---|---|---|
| `/enterprise add` | Found an enterprise on this server (interactive dialog: name → code → description → logo) | Everyone |
| `/enterprise info [code\|name]` | An enterprise's card (leaders, workers, balance, salaries, warehouse) — or the server's enterprise list | Everyone |
| `/enterprise edit [option] [code]` | Numbered management menu: texts, logo, leaders (consent buttons), transfer, salary currency, salary period (weekly/monthly), deletion | Enterprise Leaders; server admins for their neutral enterprise |
| `/enterprise join <code>` | Ask to join an enterprise (a leader accepts via buttons) | Everyone |
| `/enterprise leave [code]` | Leave an enterprise | Everyone |
| `/enterprise kick <user> [code]` | Remove a worker | Enterprise Leaders |
| `/enterprise position <name> <salary> [code]` | A position and its salary: an exact amount (`12.34`), a percent of period sales (`5%`), `0` removes | Enterprise Leaders |
| `/enterprise assign <user> <position> [code]` | Put a worker on a position (`-` clears) | Enterprise Leaders |
| `/enterprise salary <user> <salary> [code]` | A worker's personal salary override (amount / percent / `0`) | Enterprise Leaders |
| `/enterprise sell <good_code> <buyer> [qty] [price] [code]` | Sell the enterprise's stock to a member or another enterprise; proceeds go to the enterprise's account and count as sales | Enterprise Leaders |
| `/enterprise export <good_code> <qty> <target> [price] [currency] [code]` | Ship goods to another enterprise (any server). The target leader must always accept; the cargo then travels for its transit time before arriving | Enterprise Leaders |
| `/enterprise autoexport <good_code> <qty\|off> <target> [price] [currency] [code]` | Recurring weekly shipment contract (the receiving leader accepts once; `off` cancels without consent) | Enterprise Leaders |
| `/enterprise transit [code]` | An enterprise's goods still in transit — what, how much, to/from where, time left | Everyone |

### Telegram commands

Telegram has the same actions under the same groups, written with an underscore (`/bank_pay`, `/enterprise_autoexport`, `/server_lang_channel`); the hyphenated spelling and the pre-group names are accepted as well. Differences:

| Command | Difference from Discord |
|---|---|
| `/account_discord <nickname>` | Telegram's half of account linking (instead of `/account verify`; verification is inherited from the linked, Fandom-verified Discord account). Old names: `/add_discord`, `/add-discord` |
| `/party_add` | The founder needs a Fandom account the bot knows — `/account verify` on a linked Discord account, or one an Olympiad reviewer remembered; the union's Discord-role requirement applies to whoever has a Discord account to check |
| `/good_add <name> \| <base_value> \| <energy_cost> \| [emoji] \| [category] \| [currency] \| [enterprise]` | Arguments are separated by `\|` |
| `/enterprise_position <name> \| <salary> \| [enterprise]` and `/enterprise_assign <user> \| <position> \| [enterprise]` | Arguments are separated by `\|` (position names may contain spaces) |
| `/bank_pay <amount> <code>`, `/bank_fine <amount> [reason]` and `/bank_emit <amount> <code> [reason]` | Can also be used as a **reply** to the target's message (the explicit `<user>` argument is then omitted) |
| `/bank_transactions [code] [page]`, `/bank_earn_info [code] [page]` | Sent to the private chat with the bot; asked in a group, the bot says it sent it there (start the private chat first). On Telegram a group's Server Admins see the group's earning topics |
| `/enterprise_kick`, `/enterprise_assign`, `/enterprise_salary`, `/good_give` | The target may also be given by **replying** to their message |
| `/enterprise_export`, `/enterprise_autoexport`, `/enterprise_transit` | Arguments are space-separated; `/enterprise_transit <code>` shows any enterprise, or the one you lead when omitted |
| `/server_logs [chat_id] [day] [HH:MM] [tz]` | Run it in the group; the optional chat id may carry a topic (`-100…:threadid`). Without an id the current chat/topic is used |
| `/account verify`, `/account telegram`, `/find with`, `/find without`, `/wiki channel`, `/wiki greeting`, `/wiki rep-role`, `/birthday role` | Discord-only (Fandom verification and the commands that operate on Discord channels and roles) |
| `/birthday_set [DD.MM\|off]` | The date is one argument, e.g. `/birthday_set 25.12`; in a group the answer clears itself after a minute |
| `/birthday_channel [off] [tz]` | Run it in the group or topic that should receive the greetings |
| `/calendar` | The same calendar with inline buttons; a community's members are the group's members |
| `/union_edit [code] [option]` | The new owner is named by Discord account ID; a sponsor's offer is accepted by the Telegram account linked to it (otherwise offer it on Discord) |
| `/olympiad_set`, `/olympiad_contest_add`, `/olympiad_contest_edit`, `/olympiad_vote` | Work as on Discord (`/olympiad_vote` in the private chat with the bot only). Telegram has no published command list, so these are hidden by being left out of `/help` and refused while no Olympiad runs |
| `/olympiad accept`, `/olympiad deny` | Discord-only — a contest's review and accepted-votes chats are always Discord chats, even for votes cast on Telegram |
| `/server_lang`, `/server_lang_channel` | Available to the group's Server Admins, as on Discord |
| `/admin_add`, `/admin_remove`, `/localizer_add`, `/localizer_remove` | Target may be given as an ID, a public `@username`, or by replying to the user's message |

Every other command works the same as on Discord, with the same permissions.

---

## Mechanics

### Unions and chats

A **union** is a confederation of wiki communities created with `/union add` (code + names in any subset of the six languages). `/server setup` binds a Discord server or Telegram group to a union and sets its language. Most public commands only work in set-up chats. When the bot leaves a chat, the binding, the chat's language settings and its celebration settings are removed.

A union also has a language of its own — English until it is changed — in which wiki birthday greetings are written first, and an optional logo. `/union edit` changes the names, the logo, the owner and the language; Bot Admins may edit any union and sponsors their own. A Bot Admin may hand a union to any Discord account at once, or make it an operator union with `none`. When that account has no active subscription, the bot first explains what follows — message earning in the union stops, the owner cannot edit the union, and an account whose subscription ended may be on the clock after which the bot leaves the union's communities and erases them — and asks for the union code as confirmation; when the account is only over its limits, the transfer happens and the warning follows. A sponsor can hand a union only to an active sponsor whose subscription has room for one more union and for every community the union holds, and the transfer waits for that sponsor to accept.

`/union info` shows anyone the union's public profile as counts. A union is found by its code or by a name in any of its languages; a query that matches none exactly is answered with a numbered choice of the unions it resembles, or with "not found" when it resembles none. Under the name come the union's language, creation date and communities — `Discord: 3, Telegram: 2`, or on Discord the custom emoji set in `config.DISCORD_EMOJI` / `TELEGRAM_EMOJI` in place of the platform names. The wikis section (their number, their languages when there are several, the number of different people in their administrations) appears only when the union has a wiki, the politics section only when it has a party or a governing body; the economy section counts banks (each bank is one currency), open accounts, enterprises (the communities' own neutral enterprises included) and goods (the five base goods plus the union's market goods, as `/good info` lists them). Buttons under the profile open the full lists of wikis (with their language flags), parties, governing bodies, banks and enterprises — only the lists that have anything in them; on Discord a list comes back only to the person who pressed, and long lists are paged. The profile never names the sponsor who pays for the union.

A chat that is never bound is not kept: see [the seven-day setup deadline](#the-seven-day-setup-deadline).

### Verification

To use the Fandom-activity commands (parties, `/find with`/`/find without`), users verify once, globally. The economy needs no verification — anyone in a set-up chat earns, produces, trades and works:

- **Discord:** `/account verify <fandom_username>` fetches that Fandom profile through Fandom's public API and checks that the Discord handle published there matches the caller.
- **Telegram:** users link their Telegram account to a Fandom-verified Discord account: `/account telegram <tg_nick>` on Discord + `/account_discord <discord_nick>` on Telegram, both within 30 minutes, each side naming the other. The bot's reply says which name to type on the other side, warns when the other side is waiting under a different name, and says so when the accounts are already linked.

A linked pair is treated as **one person**: one quiz history, one set of privacy settings, one wallet in every bank. When the link is made, whatever the Telegram account held — balances and debts, energy, goods, mastery, profession overrides, standing orders and production runs under way — moves to the Discord account, and the move is written into the transaction journal.

### Parties

Parties belong to a union and are enabled per union with `/union parties` (with the Discord roles whose holders may found them: `enable` takes the list or keeps the stored one, `add` and `remove` change single roles). A role deleted on its server is taken off every union's list at once and reported to the service chats. `/party info` finds a party by code or name and offers near matches; a query resembling no party is answered with "not found". `/party add` runs an interactive dialog (name → 4-char code → logo; each answer waited up to 30 minutes). Founders/leaders manage the party through numbered `/party edit` menus; leadership offers and transfers require the target's consent through «Accept / Decline» buttons on both platforms. Joining goes through `/party join` with leader approval delivered as DM buttons that survive bot restarts. `/party info` shows the card, including the party's bank balances.

### Governing bodies

`/govt add` creates a named body in a union (with its member titles); `/govt info` shows a body's members together with their party affiliations. Seats are counted on party cards.

### Economy

The economy adds cross-community currencies with a real production cycle behind them. Its tuning constants live in `config.ECONOMY`.

#### Banks and currencies

`/bank add` (Bot/Server Admins, interactive dialog) creates a **bank**: it is anchored to a **central server** (which must be `/server setup`-bound) and thereby to that server's union. A server or union can host any number of banks. The currency has an English name, a 4-character code — unique across the shared namespace of union, party, currency **and** good codes — and an emoji. The creator becomes the bank's first leader; leadership works like party leadership (consent-button offers and transfers, `/bank leader`, `/bank transfer`), except that Bot Admins may appoint leaders anywhere directly. A bank leader does not need to be any kind of admin.

#### Names of currencies

In `/bank edit` → *Forms of the currency name* a bank's leaders give, for any of the six languages, the forms of the currency's name in one comma-separated answer: the six singular cases and the plural forms numbers take in Russian (genitive plural), Ukrainian and Polish (nominative and genitive plural), a singular and a plural in English, Spanish and Portuguese; `-` stands for a form not given, and `-` alone forgets the language. In a language with forms an amount is written with the name in the form its number needs (1 гривна, 2 гривны, 5 гривен, 1.5 гривны), and texts use the declined name where the sentence needs a case ("Счёт открыт в гривне"); in a language without them the code is shown as before.

#### Money, accounts, journal

Money is stored as **integers in minor units** (`minor_units` per whole unit) and displayed with its decimals plus the currency emoji and code — or the currency's name in the form the number needs, in a language for which the bank's leaders gave the name's forms (see below). Each user (and each party) has at most one account per bank — opened explicitly with `/bank open`, or silently on the first message that counts in an earning channel — the account appears even when both rewards round down to zero, and the user is not told; `/bank balance` is where they find it. Every balance change — earning, `/bank pay`, `/good sell`, `/bank fine`, `/bank convert`, wages, dues — goes through atomic operations under the database lock and is recorded in an **append-only transaction journal** (from, to, amount, currency, reason, time).

#### Earning by messages

`/bank earn <code> <on|off> [rate]` marks the current channel/topic as earning a specific bank's currency (the chat must belong to the bank's union) — this is how several banks coexist on one server: each channel chooses which currency it feeds. Unconfigured channels earn nothing. The binding remembers who switched it on and when, and each counted message adds to the channel's daily count of messages and currency minted (kept eight days); `/bank earn-info` lists the channels with those details.

Each counted message produces one activity value **A = min(√chars, `sqrt_cap`)** — sublinear in length with a ceiling, so long messages are rewarded finitely and one-word spam earns ~0. From A the user is credited **both resources at once**: currency (`A × rate × currency_per_activity`) and **energy** (`A × energy_per_activity`). Anti-abuse: messages shorter than `min_chars`, commands, bot messages and edits are ignored; a per-user `cooldown` runs between counted messages; a rolling **hourly cap** limits total A per user. Verification is not required to earn.

#### Goods, categories, mastery

Goods belong to **people and enterprises**, not to banks. Any user creates one with `/good add`; the good is only *denominated* in a bank of the current union — that bank prices it and is the currency a sale is paid in, but **it never buys anything itself** — and receives its own unique 4-char code. A good may be tied to one of the five **base categories** — 🥕 food, 🧼 hygiene, 👘 wardrobe, 💊 pharmacy, 🧺 household — and then its quality feeds the server's [provision](#provision-and-weekly-statistics).

The five categories are also producible **directly, on every server**, as built-in base goods (`FOOD`, `HYGN`, `WARD`, `PHRM`, `HOUS`). Base goods carry no market value: they cannot be sold at all, only handed over with `/good give` — they exist to supply the server (provision), not to make money.

Mastery **m** = units of that good the worker has ever produced:

- quality level = `min(mastery_max_level, 1 + floor(log₂(1 + m/mastery_scale)))`
- unit value = `base_value × (1 + alpha·√m)`, with *m* clamped at the top level's mastery
- craft cost = `energy_cost × (1 + mastery_cost_weight·alpha·√m)`, same *m* and same clamp

— the more you produce, the higher your quality, the more a unit is worth and the more it costs to make. **Cost follows the same curve as value and at a fraction of it** (`mastery_cost_weight`, default 0.7), which is what makes skill pay: value per unit of energy rises monotonically, by about a quarter from the first unit to the last. Below 1.0 practice pays off, at 1.0 it is exactly neutral, above it every hour of it makes the crafter worse off. The curve **ends**: at `mastery_max_level` (default 10) the level, the unit value and the craft cost all freeze, so a good has a best possible quality rather than an endless creep. `mastery_scale` (default 6) stretches the ladder without reshaping it — every level still costs twice the one below — and is the single knob deciding how long the top takes: at 6 the whole ladder is 3066 units, about a month of a 24/7 line fed by a moderately active earner.

`/good give` hands stock to another user; `/good autosend` moves a percentage of a good's stock daily to a chosen user, party or enterprise. A user's **profession** in a bank defaults to the good they have produced the most of; a bank leader can override it with `/bank profession`.

#### Selling

**No bank buys goods.** `/good sell <good> <buyer> [qty] [price]` is a sale between two holders: the units go to the buyer — a member or an enterprise — and the money comes out of their account into the seller's, in the good's own currency. The buyer must accept with the consent buttons first; nothing moves until they do, and the sale can still fail there on funds. Omitting the price asks what the batch is worth at the seller's quality; omitting the quantity offers everything held. `/enterprise sell` is the same offer with an enterprise as the seller (leaders only), and its proceeds count into the enterprise's **period sales** when they land in its salary currency.

The currency therefore has exactly **two sources**: message earning, and the daily meal the server buys off its enterprises ([consumption](#daily-consumption)). Producing goods is not a way to print money — a good has to find a buyer.

#### Production

Production takes **time**. `/good craft` starts a run: after its duration the whole batch lands in the inventory (yours, or your enterprise's when you produce for one) and the bot announces it in the channel where the run started. Per run, at quality level *L* and the good's category:

- duration = `produce_base_sec × category_time_mult × (1 + produce_level_time·(L−1))`, clamped to `[produce_min_sec, produce_max_sec]` (≤ 5 minutes) — food is quick, pharmacy slow;
- batch size = `category_yield × (1 + produce_level_yield·(L−1))` — bulk categories yield more units;
- energy is charged up front (the per-unit craft cost, summed over the batch). Pending runs survive restarts.

A worker may keep up to `parallel_production` (default 5) **different goods** in production at once — all five base categories, if they like — but only one run of each. The batch is **divided by the number of lines running**, so five goods at once yield what one good would, in five streams: breadth rather than speed. Energy follows the batch, so nothing is free either way.

Two things make a run more than arithmetic. A person's **very first run ever** finishes at once instead of after minutes, and the energy for it is already on their account: the first account anybody opens carries `starter_energy` (default 30, one whole run of the bulkiest base category). Somebody who has just met the bot can therefore make something and hold it before deciding whether any of this is for them, rather than writing messages at it for a quarter of an hour first. And a manual run has a `lucky_batch_chance` (default 5 %) of coming out **double** at no extra energy — announced in the channel when it happens. It applies to `/good craft` and never to a 24/7 line: the surprise belongs to the run somebody chose to start. Until this existed the only die the economy rolled was the one that spoils cargo in transit, so its every surprise was a loss.

`/good autocraft` is the same line running **24/7**: each hour it produces `auto_efficiency` (default 50 %) of the worker's manual tempo divided between the owner's parallel lines, with fractional units carried over, and it halts while the worker's energy is empty. The same limit of five applies. Base goods draw energy from the account where the worker has the most; market goods from their denominating bank.

#### Leaderboards and progress notices

`/good top` shows one of three boards, ten places each: **`week`** (default) — who produced the most on this server over the last seven days, read off the same rows the weekly statistics are built from; **`good <code>`** — who has produced the most of one good, ever; **`wealth <code>`** — the largest personal holdings of one currency. Parties and enterprises are left off all three: a board is between people.

The bot writes to somebody privately only about **what they set going themselves**. Raising a quality level is such a thing — it can only happen through a run they started — so it earns a direct message explaining what the new level changes. Earning money by talking is not: an account opens silently for anyone who writes in an earning channel, and that person has asked the bot for nothing. `/bank balance` is where they find it, as before.

#### Enterprises

Any user founds an **enterprise** with `/enterprise add` (name, 4-char code in the shared namespace, description, logo — like a party). An enterprise belongs to the server/group it was founded on, works toward that server's monthly task, and holds bank accounts like a party. Leadership mirrors parties: consent-button offers, transfer, and a numbered `/enterprise edit` menu. Workers join with `/enterprise join` (a leader approves), leave freely, and can be dismissed.

Each configured Discord server and Telegram group has one **neutral enterprise**. `/server setup` creates it for a new community, and startup creates one if an existing community lacks it. The bot names it after the community and keeps that name in step with the community's name, which it checks at startup and once a day; while the community's name is still unknown, the enterprise is called "Community" with a suffix, never by the community's ID. A server admin appointed with `/admin add` can rename it and edit its settings through `/enterprise edit`, and a name set by hand stays as it is. `/enterprise info` without arguments lists the enterprises of the current server or group, and the enterprise list under `/union info` covers the whole union. A transfer requires the recipient's consent, turns that company into an ordinary enterprise, and creates a replacement neutral enterprise for the community. Deleting the neutral enterprise disables its automatic supply until `/server setup` is run again.

Accepted message earnings from people who do not belong to any active enterprise count toward the neutral company's basic supply. One accepted message contributes up to 0.25 virtual base units in **each** of the five categories, consumed in the next UTC daily tick. That supply fills only the gap to **40%** of each category's need, the minimum acceptable provision threshold. It cannot enter an inventory, be traded, accrue mastery, pay salaries, generate GDP, back a currency, or complete a monthly production task. The bot keeps one aggregate row per person, community and UTC day; its daily sweep removes rows older than eight days. It stores no message text.

A worker (leaders included) produces **for** the enterprise by naming it in `/good craft` or `/good autocraft`: the batch goes to the enterprise's warehouse while the mastery stays personal (the enterprise also accrues its own mastery, which prices its stock). Leaders sell stock with `/enterprise sell` and **export** it to other enterprises — on the same server or any other — one-off (`/enterprise export`) or as a weekly contract (`/enterprise autoexport`).

#### Export logistics

An export is a **shipment that spends time in transit**, modelled on timed production. A leader of the *receiving* enterprise must always accept the offer (free or priced) with the consent buttons; only then does the cargo leave. On dispatch the goods leave the seller's warehouse at once, and a priced sale **escrows** the buyer's payment immediately (no funds, no dispatch). The shipment then travels for a duration set by the **distance** between the two enterprises' servers:

- **local** — same server/group — near-instant (`transport_local_sec`);
- **within the union** — different servers of one union — `transport_cross_server_sec`;
- **between unions** — `transport_cross_union_sec` (always the longest).

`transport_per_unit_sec` optionally lengthens a shipment by its size. On **arrival** the goods reach the buyer and the escrowed payment is released to the seller (counting into its **period sales** when it lands in the salary currency); the bot announces the arrival in the channel where the export was launched. Perishable categories — **food** and **pharmacy** — run a *risk* of spoiling rather than paying a toll: the longest trip the map allows carries the category's full `transport_perish_chance`, a shorter one proportionally less, and durable categories (wardrobe, hygiene, household) none at all. When the risk does come true the loss is a share of the batch drawn up to `transport_perish_max_share` (a third), and at least one unit always arrives — so a long haul with food is a gamble worth weighing rather than an arithmetic certainty, and the odds are stated against the half hour a long delivery actually takes. `/enterprise transit` lists an enterprise's cargo still on the way, and the `/enterprise info` card shows an *in transit* summary. If either enterprise is deleted mid-transit, the goods and any escrow are refunded to the surviving side. Shipments live in the database, so they survive a restart and arrive on schedule.

**Salaries** are paid from the enterprise's account on the schedule its leader picks (weekly or monthly, in `/enterprise edit`), in the enterprise's salary currency: a worker gets their personal override (`/enterprise salary`) if set, otherwise the salary of their position (`/enterprise position` + `/enterprise assign`). Either kind is an exact amount or a **percent of the period's sales**; payroll stops when the account runs dry (an enterprise cannot go into payroll debt), and the sales counter resets after each payout.

#### Daily consumption

Every day each set-up server **eats**. The daily tick counts the server's active population on the preceding UTC day — distinct people who produced or earned eligible neutral activity there — and turns that into a demand per base category:

```
need = prov_daily_units_per_person × population × category_weight
```

The demand is served out of the warehouses of the **ordinary enterprises based on that server**, market goods first and base goods as the filler behind them, until `ceil(need)` units are taken or the warehouses run dry. Within each tier the demand is spread **evenly over every warehouse holding stock**, a pass at a time, rather than emptying whichever one happens to sort first. Every market unit is **bought**, not requisitioned: the enterprise is paid the unit value its quality commands (base goods carry no market value and so feed for free), and the proceeds count into its period sales like any other sale. The neutral enterprise then supplies only any remaining gap up to 40% of the need. A community with neither producers nor eligible earners has no demand; personal inventories are never touched.

Because no bank buys goods, this daily shopping is the enterprises' **main income** and one of the currency's only two sources, which is why the even draw matters: it is a market being served, not a race being won.

Each day's demand and what covered it is written to `server_consumption`, including a separate `neutral_units` amount. The accepted earning counts are aggregated in `neutral_activity`, not stored as individual messages; the daily sweep removes rows older than eight days.

#### Provision and weekly statistics

**Provision** measures how much of what a server's people needed actually reached them over the trailing week. It reads the consumption rows back rather than recomputing anything:

```
satisfaction = Σ qty × quality_mult    # base goods count 1.0; categorised custom
                                       # goods add prov_quality_bonus per level > 1
score        = min(Σ satisfaction / Σ need, cap)
```

While only base goods feed a category its score is capped at **1.0** — base production alone reaches *acceptable*, never higher; custom categorised goods lift the cap to `prov_max_score`. Neutral supply alone stops at **0.4**, and the monthly task bonus applies only to the score earned from ordinary goods. The overall score is the weight-averaged category score with that eligible bonus, and maps to a level: < 40 % *insufficient*, ≤ 100 % *acceptable*, ≤ 150 % *good*, above — *excellent*.

Because the score is coverage rather than output, **stock matters**: goods sitting in a warehouse feed the server, and a category nobody supplies drags the whole average down however busy the chat is. The weekly statistics mark each category that ended the week short.

`/server logs` names the server's **economic log channel** and its posting schedule (weekday, time, timezone as a UTC offset). The bot posts there the daily FX update (each union currency's value with its day-over-day change) and the weekly statistics: provision by category, goods produced, **GDP** (the week's production value, weighted by each currency's published value), the server's banks and the running task progress.

#### Monthly tasks

`/server tasks` names the channel where the bot posts the server's **production task** on the 1st of each month (time and timezone configurable). The task is generated from the server's own history: every base category must grow on last month's output (`task_growth`, floored at `task_min_qty` units) — that part is **mandatory and must be produced on this server/group** — plus up to `task_optional_goods` of the server's top custom goods as optional extras. The post also announces the verdict on the previous month's task; a completed task grants the provision bonus for the following month. Progress is visible in the weekly statistics.

#### Fines and debt

`/bank fine` (bank leaders) debits the target's account in that bank; the balance **may go negative** — a negative balance is the member's debt to the bank. Because balances are signed, incoming money automatically pays debt down first. No interest accrues. The bank's card shows the summed debt outstanding.

#### Emission

`/bank emit` lets a bank's leaders issue its currency to a person, or to a party or enterprise of the union by code — for a contest prize or a grant — with a reason that goes into the journal. It grows the money supply, which the next daily recompute prices the currency against. A leader's emissions in one calendar month (UTC) may total at most 10% of the currency's money supply, and never less than 100.00; Bot Admins are not limited.

#### Taxes

Two taxes can be withheld from whoever receives money, after it arrives — the payer always parts with exactly the amount named, and the confirmation says what was withheld:

- **The community tax**, set by Bot Admins and Server Admins with `/server edit` (0% by default, at most 50%), is taken from payments and sales made in that server or group, from export payments received by an enterprise based there, and from currency earned by messages there. It is destroyed: it leaves circulation.
- **The bank tax**, set by a bank's leaders in `/bank edit` (0% by default, at most 50%), is taken from payments, sales and exports in that currency wherever they happen, and is shared among the bank's leaders.

Wages, salaries, party dues, fines, conversions and emissions are not taxed. `/server info` and the bank card show the rates.

#### Audit channel and history

A bank's leaders may choose an audit channel in `/bank edit` (any Discord channel or Telegram group/topic). The bot posts there the bank's administrative operations: fines, emissions, the monthly wages and party dues paid in the currency, changes of conversion fees and of the bank tax, and changes of leadership. Payments, sales and earnings are not posted. Everyone can read their own movements with `/bank transactions`; nobody else can.

#### Economy summary and leaderboards

The **Economy** button under `/union info` shows, for each of the union's currencies, the money supply and its net change over the last seven days, the outstanding debts, the share of all money held by the richest tenth of its holders, and the money that changed hands between holders over seven days. `/bank balance` shows your place among the holders of each currency. `/account privacy` can hide you from every `/good top` leaderboard.

#### Currency exchange

`/bank treaty <code>` proposes a conversion treaty between two banks; a leader of the other bank accepts through consent buttons, making A↔B convertible. Each day the scheduler recomputes every currency's **value** from goods backing, money supply and accepted message activity:

```
backing         = period_goods_value / max(money_supply, 1 unit)
goods_target    = backing × (1 + fx_activity_weight × ln(1 + period_energy))
activity_target = fx_activity_weight × ln(1 + eligible earning messages in the last 30 days)
value           = clamp(max(goods_target, activity_target, fx_min_value))
```

The 30-day activity component gives a community's currency value even before its first priced good is crafted. Only messages that pass the normal earning checks count. The published value still moves at most `fx_daily_clamp` per day; currencies already at the minimum recover gradually rather than jumping in one update.

The rate A→B = value(A) / value(B). **Manual peg:** if two banks are each other's **only** treaty partner, their leaders may fix the rate by mutual consent with `/bank rate`, overriding the computed one; as soon as a bank has two or more partners only computed rates apply (protecting triangle consistency). `/bank convert` exchanges between your own accounts at the current rate, minus the source bank's spread; `/bank rates` shows values and rates.

**The spread belongs to the bank the money leaves.** Its leaders set it in `/bank edit` → *Conversion fee*: one percentage for every currency, plus a percentage of its own for any particular currency it is worth treating differently (`USD 1`, and `USD -` to drop that one again). Unset, the deployment's `convert_fee` applies. The rows are keyed by currency code and travel with it, so renaming a currency does not detach its fees.

The fee is charged in the currency being sold and **paid to that bank's leaders**, split evenly, the remainder of an uneven split going to the first of them. A bank has no treasury of its own — no account row exists for one — so its leaders are where its income can go; a bank with no leaders left burns the fee instead, which is what every bank used to do with it.

#### Party economy, dues, wages

Parties hold accounts like users; their balances appear in `/party info`. `/party dues <code> <amount>` sets a mandatory monthly member contribution: the monthly scheduler moves it from each member's account to the party's (a shortfall pushes the member into debt to the bank). `/bank wage <profession> <amount>` sets a monthly wage in a bank: the monthly scheduler pays each account holder the wage of their profession (an emission; the sinks against it are fines and — while a bank charges one — the part of a conversion spread that no leader is there to collect).

#### Scheduler

By the pattern of the retention/backup loops: a **daily** tick (consumption, then the FX recompute — in that order, so the money paid for the day's meal is already in the supply the recompute prices against — period-window reset, autosend, task evaluation), a **weekly** tick (weekly enterprise payroll, recurring export shipments) and a **monthly** tick (wages, party dues, monthly enterprise payroll). Last-run markers are stored in `bot_settings`, so restarts never skip or double-run a period. Alongside them run an hourly **autoproduction** advance, a 20-second loop that completes due production runs, a 20-second loop that delivers **arrived shipments**, and a per-minute loop that delivers each server's weekly statistics and monthly task at the schedule (and in the timezone) that server configured — a post missed while the bot was down goes out as soon as it is back.

### Quizzes

`/quiz take` offers ideology quizzes with shuffled options, works in DMs, supports pausing (`/quiz stop`, progress kept 7 days). A result is **stored only with the user's consent**, at most the two most recent attempts per quiz; `/quiz compare` puts two users' breakdowns side by side (can be forbidden in `/account privacy`), `/quiz previous` compares your two attempts.

### Olympiad

A bounded event for choosing the best wiki projects. `/olympiad set MM-DD-YYYY MM-DD-YYYY` opens it and fixes the voting period (UTC, the closing day counting in full); `/olympiad set off` cancels it. From the moment it is opened until the period ends, Bot Admins may run the setup commands; everyone else may vote only inside the announced window.

The commands are **hidden until the event exists**. On Discord `/olympiad contest-add`, `/olympiad contest-edit`, `/olympiad vote`, `/olympiad accept` and `/olympiad deny` are added to the command tree when an Olympiad opens and removed when it closes, so they never show up in the slash-command picker otherwise (Discord may take up to an hour to distribute the change). On Telegram, where no command list is published, the same commands are left out of `/help` and refuse to run.

A **contest** (`/olympiad contest-add`) belongs to a language category — one of the six localizations, or `int` for a cross-language contest, which is named in every language at once. It carries the ids of two Discord chats: one where votes go for review, one where accepted votes are published. Its **candidate wikis** (`/olympiad contest-edit`) have a name, a link, and — in an `int` contest — a language code.

**Voting** (`/olympiad vote`) happens only in a private chat with the bot. Someone who has not picked a language for that chat is asked in English, with a button per localization. The bot then wants to know the person's Fandom account: it skips the question when `/account verify` (or an earlier `remember`) already told it, and otherwise takes either a Fandom username or — behind the "Another way" button — a link to a profile on any wiki host that shows their messenger handle. Next it asks for a wiki of the Confederations or of an Olympiad participant where they have over 50 edits or messages, which speeds the review up. Then comes the contest, up to three candidate wikis (any mix of commas, semicolons and spaces; whole numbers, not digits), and finally either a written-out vote or a button giving each chosen wiki one point. Every question carries a **Stop** button that abandons the dialog.

The vote arrives in the review chat as an embed: the contest as the title, the supported wikis as the subtitle, then the account confirmation, the activity claim and the vote itself, with the voter, their messenger id and the vote's key in the footer. A reviewer answers with `/olympiad accept <key> <wiki username> [remember]` — which publishes the vote to the accepted-votes chat under the voter's messenger name and that wiki name, and with `remember` also links the account for the bot — or `/olympiad deny <key> <reason>`, and either way the voter gets a DM. One person has one vote per contest; voting again replaces the earlier one and goes back for review, marked as an updated vote. Points are counted by people, not by the bot.

When the voting period ends, the contests, their candidates and every vote — reviewed or not — are **deleted**. Only two things outlive an Olympiad, and both expire a year later: the language someone chose in their private chat, and a wiki account a reviewer chose to remember.

### Union wikis

A Bot Admin attaches a wiki to a union with `/wiki add <union> <link>`. The link may point at any page, at `/wiki/` or at the wiki's root, with or without `https://` — `confederation.fandom.com/ru/wiki/Славяноконфедерация/ru` and `confederation.fandom.com/ru` mean the same wiki. A Fandom link without a language path that finds nothing at the root is also tried under the usual language paths (`/es`, `/ru`, `/pt-br` and others). When a wiki's protection (Cloudflare) refuses the bot's address, the bot says so instead of reporting a wrong link; that is a block on the server running the bot, and trying later or from another address is the only remedy. The bot reads the wiki at once: its name, language and main page, its founding date, and its administration — the bureaucrats, administrators and content moderators, without bots and system accounts. The founding date is the one Wiki-Bot's `/overview` shows: the oldest entry of the wiki's log, or Fandom's own creation date when Fandom reports an earlier one. A wiki that was moved or merged can show a later date than its oldest pages; `/wiki founded <link> <DD.MM.YYYY>` corrects it, and `auto` goes back to the detected date. `/wiki remove` detaches a wiki; a wiki left in no union is forgotten together with its cached administration.

Twice a day the bot reads every attached wiki again — name, language, administration and the Discord handles the administrators' Fandom profiles publish — so the union profile and the representative role never wait for a wiki. When a person's status began is taken from the wiki's rights log; when the log says nothing, the status counts from the wiki's founding.

### Wiki birthdays

`/wiki channel` (Bot Admins) chooses the channel of a Discord server where the birthdays of its union's wikis are announced, at 10:00 in the server's timezone. Discord does not tell bots a server's timezone, so `/wiki channel`, `/birthday channel` and `/birthday role` take an optional UTC offset (`UTC+3`); the server keeps one offset, and the last one given applies to all three.

All wikis with a birthday that day share one message. A wiki whose founding moment has already come round that day "turned" its age; one whose moment is still ahead "turns" it, and a message with both says so ("Today D Wiki turned 10 years old, while E Wiki turns 2 years old!"). The first line is written in the union's language. Each wiki in another of the bot's six languages, or in Belarusian, adds its flag to that line and gets a line of its own in its language below it; the main page links follow. A wiki in any other language is named in the first line only and marked 🌐 wherever its language is shown. The Belarusian phrases live in `src/i18n/wiki_greetings/be.json` (editable in the control panel); another wiki language is added the same way, with its name and flag in `utils.py: LANGUAGE_NAMES` and `LANGUAGE_FLAGS`. A wiki founded on 29 February is congratulated on the 28th in common years, and a wiki in its first year is not congratulated. `/wiki greeting <link> false` stops greeting one wiki in this server only; `true` brings it back.

### Personal birthdays

Anyone can save a birthday — day and month only — with `/birthday set`; a linked Discord and Telegram account share one date, and `/birthday set` with `clear` (Telegram: `/birthday_set off`) forgets it. The name the person goes by on that platform is stored with the date, for the calendar.

`/birthday channel` (Bot Admins and Server Admins) chooses where members are congratulated, at 10:00 server time, one message per person. On Telegram the command binds the group or topic it is run in; since Telegram does not list a group's members, the bot asks Telegram about each person whose birthday it is and greets those who are in the group. Each greeting is one of twelve numbered texts in the six languages: none of the last five used in that channel is chosen, and among the rest the last five that person received are avoided whenever another one remains.

`/birthday role` (Bot Admins and Server Admins, Discord) gives members a role for their whole birthday: from whichever begins first, the day in UTC or the day in the server's timezone, until whichever ends last. The bot takes the role back only from members it gave it to; somebody who already held the role keeps it.

### Calendar

`/calendar` (everyone, both platforms) lists what comes round in the coming month, one date per line in words — `1 January — 📚🇷🇺 A Wiki`, `2 January — 👤 A Name`: the founding days of unions (✨) and of their wikis (📚), each with the flag of its language, and members' birthdays (👤). A page is one calendar month from today's day of the month — 13 October to 12 November, then 13 November to 12 December — and ◀ and ▶ move by a page; three buttons hide or show each kind of event; one button switches between this community — its union, that union's wikis and the birthdays of its members, named as the platform names them here — and every community: all unions, all union wikis and everyone who saved a birthday, named by the name stored with the date. A page is cut to what one message holds and says how many events did not fit. Outside a community bound to a union, only the second view exists. On Discord only the person who opened the calendar may press its buttons, for as long as an ordinary dialog lasts; on Telegram anyone in the chat may, and because each button carries the calendar's whole state they keep working after a restart.

### Wiki representatives

`/wiki rep-role` (Bot Admins) chooses the role that marks the representatives of the union's wikis on a Discord server. A member receives it when one of the union's wikis has listed them as a bureaucrat, administrator or content moderator for more than 30 days, and the bot can tie their Discord account to that wiki account: either their Discord username is the Discord handle the Fandom profile publishes, or their Discord account already went through `/account verify` or was remembered by an Olympiad reviewer for that wiki account. The roles are checked every hour and after every refresh; a member who no longer qualifies loses the role, but only if the bot gave it.

### Localization

Every reply exists in six languages (en, ru, uk, pl, es, pt) with per-key verification status. `/server lang` sets a chat's default, `/server lang-channel` overrides per channel/topic/thread. `/locale info` shows per-language progress bars or sends the raw file, `/locale compare` shows one reply across languages, `/locale suggest` sends a suggestion to the operator's support chats, answered with `/locale reply`. Currency names stay in English (as entered), decorated with the currency emoji.

On Discord the economy speaks entirely in **embeds** (default color `#245590`); Telegram uses HTML formatting. Discord users mentioned in Telegram replies are shown by their stored nickname, never as an unresolvable ping.

### The seven-day setup deadline

The bot is invited far more often than it is put to use, so a server or group it has just been added to has **seven days** to be bound to a union with `/server setup`. If that never happens, the bot leaves on its own and says so in `SERVICE_CHATS`, where it also announces every chat it is added to.

Nothing is said in the community itself, neither on arrival nor on the way out. `/server setup` is a bot-admin command, so the people who could act on a warning are the operators — and `SERVICE_CHATS` is exactly where they read.

The rule is deliberately narrow:

- **It never touches a community the bot was already in.** The deadline counts from the join, and the moment the rule came into force is recorded once, on the first start of the version that introduced it; everything that joined before that instant is out of reach for good.
- **It fires at most once per community.** The first daily sweep that finds a chat bound to a union settles it permanently.
- **It skips the deployment's own chats.** A server or group holding any chat named in `config.py` — `SERVICE_CHATS`, `BACKUP_CHATS`, `SUPPORT_CHATS` — is never left.

On Discord the week is measured from Discord's own record of when the bot joined, so a restart or a missed event cannot shorten it. Telegram publishes no such timestamp, so there the clock starts from the update that adds the bot to the group, and a group whose arrival the bot never saw is never examined. Leaving is not a deletion: a community that invites the bot back starts a fresh seven days.

### Service events and automatic backups

Start/stop notices, administrative events (`/server setup`, `/union add`, `/union parties`, a union's change of owner, party and bank creation/deletion) and the chats the bot is added to or leaves on the setup deadline go to the configured `SERVICE_CHATS`. Every 12 hours (and on `/bot backup`) the database is sent to `BACKUP_CHATS` — always encrypted with an authenticated BLAKE2 keystream; the key never leaves the operator's `BACKUP_KEY` environment variable. `restore_backup.py` decrypts a backup file back into `dem.db`.

---

## Data collection and retention

See [PRIVACY.md](PRIVACY.md) for the complete inventory of what the bot stores (including the economy's accounts, transaction journal, goods, inventories, professions, wages, dues, fines/debts, treaties and rates), where data goes, what other users can see, retention periods and the choices users have.
