# Democrate

Democrate is a cross-platform coordination bot for **unions** of wiki communities that runs on Discord and Telegram. It keeps a registry of unions and the servers/groups bound to them, hosts each union's **political parties** and **governing bodies**, verifies users through their **Fandom** profiles, runs ideology **quizzes**, announces **wiki anniversaries**, and powers a cross-community **economy**: banks with their own currencies, message-based earning, timed **production** of goods owned by people and **enterprises**, salaries, fines, party dues, currency exchange, exports between enterprises, a per-server **provision** metric with weekly statistics and monthly production tasks. All replies are localized into six languages (en, ru, uk, pl, es, pt).

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
     - `SUPPORT_CHATS` — chats that receive localization suggestions from `/loc-suggest`. Same format.
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
                       the Olympiad, retention, the setup deadline)
  config.py            this deployment's ids and the economy's tuning constants
                       (untracked; config.example.py is the template)
  env_loader.py        reads src/.env
  utils.py             localization runtime, role checks, parsers, rate limiter,
                       service events
  quizzes.py           the quiz engine (its data lives in src/quizzes/)
  olympiad.py          the Olympiad engine and its shared voting dialog
  stats.py             weekly statistics, monthly task posts, FX log lines
  message_relay.py     markup conversion between Discord and Telegram
  fandom.py            Fandom profile lookup for /verify
  setup_deadline.py    leaves communities nobody bound to a union
  backup_crypto.py     encrypted database snapshots
  restore_backup.py    their restore tool

  db/                  SQLite layer: one connection, one module per domain
    __init__.py          connection (conn/cur), init(), the whole public API
    schema.py            core CREATE TABLEs, migrations, the Olympiad schema
    schema_economy.py    the economy's schema, column migrations, base goods
    unions.py   settings.py   admins.py    users.py
    parties.py  govt.py       quizzes.py   foundays.py
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
    events.py            the three @bot.event handlers
    dialogs.py           interactive dialogs and consent views
    parties.py           party cards and edit menus (shared with the TG half)
    olympiad.py          dynamic commands, review embeds, verdicts
    commands/            slash commands: unions, settings, admins, locale, user,
                         parties, govt, quizzes, moderation, foundays, banks,
                         trade, goods, enterprises

  telegram_bot/        the Telegram half, mirroring the Discord one
    client.py            bot/dispatcher/router, polling, resolvers
    dialogs.py           the pending-answer registries and consent keyboards
    callbacks.py         the four @router.callback_query handlers
    parties.py           party card and edit menu, Telegram rendering
    olympiad.py          the Telegram Olympiad gates and dialog
    commands/            the same domains as the Discord side
    catchall.py          the unfiltered handler — imported last, always

  i18n/                the six localization files, plus olympiad/ and quizzes/
  quizzes/             quiz data: <quiz id>/scoring.json
```

---

## Commands

Permission roles used below:

- **Everyone** — any user in a set-up chat (some commands additionally require [verification](#verification)).
- **Server Admins** — members with the native Administrator/Manage Server permission (Discord), group creator/administrators (Telegram), plus users delegated with `/setadmin`.
- **Localizers** — users granted `/localizer-add`: they may edit this bot's localization through the [control panel](https://github.com/HIHRAIM/Confederate-Panel). Delegated Server Admins hold the status implicitly while they remain admins.
- **Bot Admins** — user IDs listed in `config.py`.
- **Party Leaders** — a party's founder and appointed leaders.
- **Bank Leaders** — a bank's appointed leaders (need not be admins); they manage it through `/edit-bank`.
- **Enterprise Leaders** — an enterprise's founder and appointed leaders.

### Discord commands

| Command | Purpose | Everyone | Server Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/verify <fandom_name>` | Verify by matching your Discord handle on your Fandom profile | ✅ | ✅ | ✅ |
| `/add-telegram <nickname>` | Link your Telegram account (run `/add-discord` on Telegram within 30 min) | ✅ | ✅ | ✅ |
| `/party <code\|name>` | A party's card: texts, logo, leaders, members, seats, bank balances | ✅ | ✅ | ✅ |
| `/govt <name>` | A governing body's members with party affiliations | ✅ | ✅ | ✅ |
| `/add-party [founder]` | Found a party (interactive dialog; requires a union-configured role; verified) | ✅ | ✅ | ✅ |
| `/edit-party [option]` | Manage your party: texts, logo, color, code, leaders, transfer, suspension | ✅ | ✅ | ✅ |
| `/edit-rules [option]` | Party rules (member invites toggle) | ✅ | ✅ | ✅ |
| `/party-join <code\|name>` | Ask to join a party (leaders approve via DM buttons) | ✅ | ✅ | ✅ |
| `/party-leave` | Leave your party | ✅ | ✅ | ✅ |
| `/party-kick <member>` | Remove a member from your party (party leaders) | ✅ | ✅ | ✅ |
| `/quizzes` | Take an ideology quiz (works in DMs too) | ✅ | ✅ | ✅ |
| `/quizzes-stop` · `/quizzes-clear` · `/quizzes-compare <user>` · `/quizzes-previous [n]` | Pause a quiz / delete stored results / compare with another user / compare with your previous attempt | ✅ | ✅ | ✅ |
| `/privacy` | Inspect and delete the data the bot keeps about you | ✅ | ✅ | ✅ |
| `/locale [code]` · `/loc-compare <code>` · `/loc-suggest <lang> <code> <text>` | Localization status / cross-language comparison / suggestions | ✅ | ✅ | ✅ |
| `/help` | Paged command reference | ✅ | ✅ | ✅ |
| `/lang <code>` · `/locallang <code>` | Server-wide / per-channel bot language | ❌ | ✅ | ✅ |
| `/find-with` · `/find-without <channel> <period> <keywords>` | Reply-mention authors of messages (not) containing keywords (verified) | ❌ | ✅ | ✅ |
| `/wiki-founday <url> <name> <founded> …` · `/wiki-foundays` · `/wiki-founday-remove <url>` | Yearly wiki-anniversary announcements | ❌ | ✅ | ✅ |
| `/setlogs [channel] [day] [HH:MM] [tz]` | Economic log channel: daily FX updates + weekly server statistics (`off` unbinds) | ❌ | ✅ | ✅ |
| `/settasks [channel] [HH:MM] [tz]` | Channel for the monthly production task, posted on the 1st (`off` unbinds) | ❌ | ✅ | ✅ |
| `/setadmin <user>` · `/remadmin <user>` | Grant/revoke delegated Server Admin rights (ping or ID) | ❌ | ❌ | ✅ |
| `/localizer-add <user>` · `/localizer-rem <user>` | Grant/revoke Localizer status — localization editing in the control panel (ping, ID or username); DMs the user | ❌ | ❌ | ✅ |
| `/setup <union> <lang>` | Bind this server to a union | ❌ | ❌ | ✅ |
| `/add-unia <code> <names…>` | Create a union with localized names | ❌ | ❌ | ✅ |
| `/allow-parties <union> <enable\|disable> [roles]` | Turn parties on/off in a union | ❌ | ❌ | ✅ |
| `/add-govt <union> <name> <singular> <plural>` | Create a governing body | ❌ | ❌ | ✅ |
| `/edit-party-admin <party>` | Edit/move/delete any party of any union | ❌ | ❌ | ✅ |
| `/loc-reply <code> <text>` | Reply to a localization suggestion | ❌ | ❌ | ✅ |
| `/list_chats` · `/force_leave <platform> <id>` | List all bound chats / force-leave one | ❌ | ❌ | ✅ |
| `/backup` | Get an encrypted database backup | ❌ | ❌ | ✅ |

#### Discord Olympiad commands

Only `/setolympiad` is always present. The rest are registered on the command list when an Olympiad is opened and taken off it when the voting period ends, so they never appear for an event that is not running — see [Olympiad](#olympiad).

| Command | Purpose | Who |
|---|---|---|
| `/setolympiad <MM-DD-YYYY> <MM-DD-YYYY>` | Open an Olympiad and its voting period (`off` cancels it and deletes its data) | Bot Admins |
| `/setcontest` | Create a contest: language category → name(s) → review chat id → accepted-votes chat id (interactive dialog) | Bot Admins |
| `/editcontest` | Add, change or remove a contest's candidate wikis (interactive dialog) | Bot Admins |
| `/olympiad` | Vote — private chat with the bot only (interactive dialog) | Everyone |
| `/accepted <key> <wiki username> [remember]` | Accept a vote under review; `remember` also links that wiki account to the voter | In the contest's review chat |
| `/denied <key> <reason>` | Reject a vote under review; the voter is told which wikis and why | In the contest's review chat |

#### Discord economy commands

| Command | Purpose | Who |
|---|---|---|
| `/create-bank` | Found a bank and its currency (interactive dialog: central server → currency name → 4-char code → emoji) | Bot/Server Admins |
| `/bank <code>` | A bank's card: currency, union, central server, leaders, money supply, accounts, debt, value | Everyone |
| `/bank-add-leader <code> <user>` | Offer co-leadership of a bank (consent buttons; Bot Admins appoint directly) | Bank Leaders / Bot Admins |
| `/bank-transfer <code> <user>` | Offer the bank's leadership (consent buttons; Bot Admins appoint directly) | Bank Leaders / Bot Admins |
| `/edit-bank [option] [code]` | Numbered management menu: currency name, emoji, 4-char code, central server, leaders, transfer, deletion. Bot Admins may edit any bank, leaders only their own | Bank Leaders / Bot Admins |
| `/open-account <code>` | Open an account (also opened automatically on first earning) | Everyone |
| `/balance [code]` | Your money, energy and profession — all banks, or one | Everyone |
| `/pay <user> <amount> <code>` | Transfer money to another user (same currency, atomic) | Everyone |
| `/set-earn <code> <on\|off> [rate]` | Make the current channel earn a bank's currency; threads, forum posts and Telegram topics inside it inherit the binding unless they name their own (every reply is ephemeral — on Telegram, self-deleting) | Bank Leaders / Server Admins / Bot Admins |
| `/create-good <name> <base_value> <energy_cost> [emoji] [category] [currency] [enterprise]` | Create a good owned by you (or your enterprise), denominated in a bank of this union, optionally tied to a base category | Everyone |
| `/goods` | The goods available here: the five base categories plus the union's market goods | Everyone |
| `/craft <good_code> [enterprise]` | Start a timed production run (a few minutes); the batch lands in your — or your enterprise's — inventory | Everyone |
| `/inventory` | Your goods with quantities, quality levels and unit values (base goods show no price) | Everyone |
| `/top [week\|good\|wealth] [code]` | Leaderboards: this week's producers on this server, a good's masters of all time, a currency's largest holdings | Everyone |
| `/sell <good_code> <buyer> [qty] [price]` | Sell goods to a member or an enterprise; the buyer accepts and pays from their account. Base goods are not sellable | Everyone |
| `/give-good <user> <good_code> [qty]` | Hand goods from your inventory to another user | Everyone |
| `/autocraft <good_code> <on\|off> [enterprise]` | Continuous 24/7 autoproduction — slower than manual runs, up to five lines sharing the tempo, halts without energy | Everyone |
| `/autosend <good_code> <percent> <user\|party\|enterprise>` | Auto-send a share of a good's stock daily | Everyone |
| `/set-profession <user> <good_code>` | Override a member's profession in your bank | Bank Leaders |
| `/fine <user> <amount> [reason]` | Fine a user in your bank's currency (balance may go negative — a debt) | Bank Leaders |
| `/treaty <code>` | Propose a conversion treaty with another bank (their leader accepts via buttons) | Bank Leaders |
| `/set-rate <code> <rate>` | Fix a pegged rate — only with your bank's **sole** treaty partner | Bank Leaders |
| `/convert <amount> <from> <to>` | Convert between your own accounts at the current rate (fee applies) | Everyone |
| `/rates [code]` | Currency values, or one bank's conversion rates | Everyone |
| `/set-wage <profession> <amount>` | Monthly wage for a profession in your bank (0 clears) | Bank Leaders |
| `/party-dues <code> <amount>` | Your party's monthly member dues in a currency (0 clears) | Party Leaders |

#### Discord enterprise commands

| Command | Purpose | Who |
|---|---|---|
| `/add-enterprise` | Found an enterprise on this server (interactive dialog: name → code → description → logo) | Everyone |
| `/enterprise [code\|name]` | An enterprise's card (leaders, workers, balance, salaries, warehouse) — or the server's enterprise list | Everyone |
| `/edit-enterprise [option] [code]` | Numbered management menu: texts, logo, leaders (consent buttons), transfer, salary currency, salary period (weekly/monthly), deletion | Enterprise Leaders |
| `/ent-join <code>` | Ask to join an enterprise (a leader accepts via buttons) | Everyone |
| `/ent-leave [code]` | Leave an enterprise | Everyone |
| `/ent-kick <user> [code]` | Remove a worker | Enterprise Leaders |
| `/ent-position <name> <salary> [code]` | A position and its salary: an exact amount (`12.34`), a percent of period sales (`5%`), `0` removes | Enterprise Leaders |
| `/ent-assign <user> <position> [code]` | Put a worker on a position (`-` clears) | Enterprise Leaders |
| `/ent-salary <user> <salary> [code]` | A worker's personal salary override (amount / percent / `0`) | Enterprise Leaders |
| `/ent-sell <good_code> <buyer> [qty] [price] [code]` | Sell the enterprise's stock to a member or another enterprise; proceeds go to the enterprise's account and count as sales | Enterprise Leaders |
| `/export <good_code> <qty> <target> [price] [currency] [code]` | Ship goods to another enterprise (any server). The target leader must always accept; the cargo then travels for its transit time before arriving | Enterprise Leaders |
| `/auto-export <good_code> <qty\|off> <target> [price] [currency] [code]` | Recurring weekly shipment contract (the receiving leader accepts once; `off` cancels without consent) | Enterprise Leaders |
| `/transit [code]` | An enterprise's goods still in transit — what, how much, to/from where, time left | Everyone |

### Telegram commands

Telegram mirrors the Discord commands (both `/cmd_name` and `/cmd-name` spellings are accepted). Differences:

| Command | Difference from Discord |
|---|---|
| `/add-discord <nickname>` | Telegram's half of account linking (instead of `/verify`; verification is inherited from the linked, Fandom-verified Discord account) |
| `/add-party` | Points to Discord: parties are founded there because founding is gated by Discord roles |
| `/create-good <name> \| <base_value> \| <energy_cost> \| [emoji] \| [category] \| [currency] \| [enterprise]` | Arguments are separated by `\|` |
| `/ent-position <name> \| <salary> \| [enterprise]` and `/ent-assign <user> \| <position> \| [enterprise]` | Arguments are separated by `\|` (position names may contain spaces) |
| `/pay <amount> <code>` and `/fine <amount> [reason]` | Can also be used as a **reply** to the target's message (the explicit `<user>` argument is then omitted) |
| `/ent-kick`, `/ent-assign`, `/ent-salary`, `/give-good` | The target may also be given by **replying** to their message |
| `/export`, `/auto-export`, `/transit` | Arguments are space-separated; `/transit <code>` shows any enterprise, or the one you lead when omitted |
| `/setlogs [chat_id] [day] [HH:MM] [tz]` | Run it in the group; the optional chat id may carry a topic (`-100…:threadid`). Without an id the current chat/topic is used |
| `/find-with`, `/find-without`, `/wiki-founday*` | Discord-only (they operate on Discord channels) |
| `/setolympiad`, `/setcontest`, `/editcontest`, `/olympiad` | Work as on Discord (`/olympiad` in the private chat with the bot only). Telegram has no published command list, so these are hidden by being left out of `/help` and refused while no Olympiad runs |
| `/accepted`, `/denied` | Discord-only — a contest's review and accepted-votes chats are always Discord chats, even for votes cast on Telegram |
| `/lang`, `/locallang` | Available to group administrators, as on Discord |
| `/setadmin`, `/remadmin`, `/localizer_add`, `/localizer_rem` | Target may be given as an ID, a public `@username`, or by replying to the user's message |

All other commands — `/setup`, `/add-unia`, `/allow-parties`, `/party`, `/govt`, `/edit-party`, `/edit-rules`, `/edit-party-admin`, `/party-join`, `/party-leave`, `/party-kick`, `/add-govt`, `/quizzes*`, `/privacy`, `/locale`, `/loc_compare`, `/loc_suggest`, `/loc_reply`, `/list_chats`, `/force_leave`, `/backup`, `/help`, `/setlogs`, `/settasks`, the whole economy set (`/create_bank`, `/bank`, `/edit_bank`, `/bank_add_leader`, `/bank_transfer`, `/open_account`, `/balance`, `/pay`, `/set_earn`, `/goods`, `/craft`, `/inventory`, `/top`, `/sell`, `/give_good`, `/autocraft`, `/autosend`, `/set_profession`, `/fine`, `/treaty`, `/set_rate`, `/convert`, `/rates`, `/set_wage`, `/party_dues`) and the whole enterprise set (`/add_enterprise`, `/enterprise`, `/edit_enterprise`, `/ent_join`, `/ent_leave`, `/ent_kick`, `/ent_position`, `/ent_assign`, `/ent_salary`, `/ent_sell`, `/export`, `/auto_export`, `/transit`) — work the same as on Discord, with the same permissions.

---

## Mechanics

### Unions and chats

A **union** is a confederation of wiki communities created with `/add-unia` (code + names in any subset of the six languages). `/setup` binds a Discord server or Telegram group to a union and sets its language. Most public commands only work in set-up chats. When the bot leaves a chat, the binding and the chat's language settings are removed.

A chat that is never bound is not kept: see [the seven-day setup deadline](#the-seven-day-setup-deadline).

### Verification

To use the Fandom-activity commands (parties, `/find-with`/`/find-without`), users verify once, globally. The economy needs no verification — anyone in a set-up chat earns, produces, trades and works:

- **Discord:** `/verify <fandom_username>` fetches that Fandom profile through Fandom's public API and checks that the Discord handle published there matches the caller.
- **Telegram:** users link their Telegram account to a Fandom-verified Discord account: `/add-telegram <tg_nick>` on Discord + `/add-discord <discord_nick>` on Telegram, both within 30 minutes, each side naming the other.

A linked pair is treated as **one person**: one quiz history, one set of privacy settings, one wallet in every bank.

### Parties

Parties belong to a union and are enabled per union with `/allow-parties` (with the Discord role IDs whose holders may found them). `/add-party` runs an interactive dialog (name → 4-char code → logo; each answer waited up to 30 minutes). Founders/leaders manage the party through numbered `/edit-party` menus; leadership offers and transfers require the target's consent through «Accept / Decline» buttons on both platforms. Joining goes through `/party-join` with leader approval delivered as DM buttons that survive bot restarts. `/party` shows the card, including the party's bank balances.

### Governing bodies

`/add-govt` creates a named body in a union (with its member titles); `/govt` shows a body's members together with their party affiliations. Seats are counted on party cards.

### Economy

The economy adds cross-community currencies with a real production cycle behind them. Its tuning constants live in `config.ECONOMY`.

#### Banks and currencies

`/create-bank` (Bot/Server Admins, interactive dialog) creates a **bank**: it is anchored to a **central server** (which must be `/setup`-bound) and thereby to that server's union. A server or union can host any number of banks. The currency has an English name, a 4-character code — unique across the shared namespace of union, party, currency **and** good codes — and an emoji. The creator becomes the bank's first leader; leadership works like party leadership (consent-button offers and transfers, `/bank-add-leader`, `/bank-transfer`), except that Bot Admins may appoint leaders anywhere directly. A bank leader does not need to be any kind of admin.

#### Money, accounts, journal

Money is stored as **integers in minor units** (hundredths) and displayed with two decimals plus the currency emoji and code. Each user (and each party) has at most one account per bank — opened explicitly with `/open-account`, or silently on the first message that counts in an earning channel — the account appears even when both rewards round down to zero, and the user is not told; `/balance` is where they find it. Every balance change — earning, `/pay`, `/sell`, `/fine`, `/convert`, wages, dues — goes through atomic operations under the database lock and is recorded in an **append-only transaction journal** (from, to, amount, currency, reason, time).

#### Earning by messages

`/set-earn <code> <on|off> [rate]` marks the current channel/topic as earning a specific bank's currency (the chat must belong to the bank's union) — this is how several banks coexist on one server: each channel chooses which currency it feeds. Unconfigured channels earn nothing.

Each counted message produces one activity value **A = min(√chars, `sqrt_cap`)** — sublinear in length with a ceiling, so long messages are rewarded finitely and one-word spam earns ~0. From A the user is credited **both resources at once**: currency (`A × rate × currency_per_activity`) and **energy** (`A × energy_per_activity`). Anti-abuse: messages shorter than `min_chars`, commands, bot messages and edits are ignored; a per-user `cooldown` runs between counted messages; a rolling **hourly cap** limits total A per user. Verification is not required to earn.

#### Goods, categories, mastery

Goods belong to **people and enterprises**, not to banks. Any user creates one with `/create-good`; the good is only *denominated* in a bank of the current union — that bank prices it and is the currency a sale is paid in, but **it never buys anything itself** — and receives its own unique 4-char code. A good may be tied to one of the five **base categories** — 🥕 food, 🧼 hygiene, 👘 wardrobe, 💊 pharmacy, 🧺 household — and then its quality feeds the server's [provision](#provision-and-weekly-statistics).

The five categories are also producible **directly, on every server**, as built-in base goods (`FOOD`, `HYGN`, `WARD`, `PHRM`, `HOUS`). Base goods carry no market value: they cannot be sold at all, only handed over with `/give-good` — they exist to supply the server (provision), not to make money.

Mastery **m** = units of that good the worker has ever produced:

- quality level = `min(mastery_max_level, 1 + floor(log₂(1 + m/mastery_scale)))`
- unit value = `base_value × (1 + alpha·√m)`, with *m* clamped at the top level's mastery
- craft cost = `energy_cost × (1 + mastery_cost_weight·alpha·√m)`, same *m* and same clamp

— the more you produce, the higher your quality, the more a unit is worth and the more it costs to make. **Cost follows the same curve as value and at a fraction of it** (`mastery_cost_weight`, default 0.7), which is what makes skill pay: value per unit of energy rises monotonically, by about a quarter from the first unit to the last. Below 1.0 practice pays off, at 1.0 it is exactly neutral, above it every hour of it makes the crafter worse off. The curve **ends**: at `mastery_max_level` (default 10) the level, the unit value and the craft cost all freeze, so a good has a best possible quality rather than an endless creep. `mastery_scale` (default 6) stretches the ladder without reshaping it — every level still costs twice the one below — and is the single knob deciding how long the top takes: at 6 the whole ladder is 3066 units, about a month of a 24/7 line fed by a moderately active earner.

`/give-good` hands stock to another user; `/autosend` moves a percentage of a good's stock daily to a chosen user, party or enterprise. A user's **profession** in a bank defaults to the good they have produced the most of; a bank leader can override it with `/set-profession`.

#### Selling

**No bank buys goods.** `/sell <good> <buyer> [qty] [price]` is a sale between two holders: the units go to the buyer — a member or an enterprise — and the money comes out of their account into the seller's, in the good's own currency. The buyer must accept with the consent buttons first; nothing moves until they do, and the sale can still fail there on funds. Omitting the price asks what the batch is worth at the seller's quality; omitting the quantity offers everything held. `/ent-sell` is the same offer with an enterprise as the seller (leaders only), and its proceeds count into the enterprise's **period sales** when they land in its salary currency.

The currency therefore has exactly **two sources**: message earning, and the daily meal the server buys off its enterprises ([consumption](#daily-consumption)). Producing goods is not a way to print money — a good has to find a buyer.

#### Production

Production takes **time**. `/craft` starts a run: after its duration the whole batch lands in the inventory (yours, or your enterprise's when you produce for one) and the bot announces it in the channel where the run started. Per run, at quality level *L* and the good's category:

- duration = `produce_base_sec × category_time_mult × (1 + produce_level_time·(L−1))`, clamped to `[produce_min_sec, produce_max_sec]` (≤ 5 minutes) — food is quick, pharmacy slow;
- batch size = `category_yield × (1 + produce_level_yield·(L−1))` — bulk categories yield more units;
- energy is charged up front (the per-unit craft cost, summed over the batch). Pending runs survive restarts.

A worker may keep up to `parallel_production` (default 5) **different goods** in production at once — all five base categories, if they like — but only one run of each. The batch is **divided by the number of lines running**, so five goods at once yield what one good would, in five streams: breadth rather than speed. Energy follows the batch, so nothing is free either way.

Two things make a run more than arithmetic. A person's **very first run ever** finishes at once instead of after minutes, and the energy for it is already on their account: the first account anybody opens carries `starter_energy` (default 30, one whole run of the bulkiest base category). Somebody who has just met the bot can therefore make something and hold it before deciding whether any of this is for them, rather than writing messages at it for a quarter of an hour first. And a manual run has a `lucky_batch_chance` (default 5 %) of coming out **double** at no extra energy — announced in the channel when it happens. It applies to `/craft` and never to a 24/7 line: the surprise belongs to the run somebody chose to start. Until this existed the only die the economy rolled was the one that spoils cargo in transit, so its every surprise was a loss.

`/autocraft` is the same line running **24/7**: each hour it produces `auto_efficiency` (default 50 %) of the worker's manual tempo divided between the owner's parallel lines, with fractional units carried over, and it halts while the worker's energy is empty. The same limit of five applies. Base goods draw energy from the account where the worker has the most; market goods from their denominating bank.

#### Leaderboards and progress notices

`/top` shows one of three boards, ten places each: **`week`** (default) — who produced the most on this server over the last seven days, read off the same rows the weekly statistics are built from; **`good <code>`** — who has produced the most of one good, ever; **`wealth <code>`** — the largest personal holdings of one currency. Parties and enterprises are left off all three: a board is between people.

The bot writes to somebody privately only about **what they set going themselves**. Raising a quality level is such a thing — it can only happen through a run they started — so it earns a direct message explaining what the new level changes. Earning money by talking is not: an account opens silently for anyone who writes in an earning channel, and that person has asked the bot for nothing. `/balance` is where they find it, as before.

#### Enterprises

Any user founds an **enterprise** with `/add-enterprise` (name, 4-char code in the shared namespace, description, logo — like a party). An enterprise belongs to the server/group it was founded on, works toward that server's monthly task, and holds bank accounts like a party. Leadership mirrors parties: consent-button offers, transfer, and a numbered `/edit-enterprise` menu. Workers join with `/ent-join` (a leader approves), leave freely, and can be dismissed.

A worker (leaders included) produces **for** the enterprise by naming it in `/craft` or `/autocraft`: the batch goes to the enterprise's warehouse while the mastery stays personal (the enterprise also accrues its own mastery, which prices its stock). Leaders sell stock with `/ent-sell` and **export** it to other enterprises — on the same server or any other — one-off (`/export`) or as a weekly contract (`/auto-export`).

#### Export logistics

An export is a **shipment that spends time in transit**, modelled on timed production. A leader of the *receiving* enterprise must always accept the offer (free or priced) with the consent buttons; only then does the cargo leave. On dispatch the goods leave the seller's warehouse at once, and a priced sale **escrows** the buyer's payment immediately (no funds, no dispatch). The shipment then travels for a duration set by the **distance** between the two enterprises' servers:

- **local** — same server/group — near-instant (`transport_local_sec`);
- **within the union** — different servers of one union — `transport_cross_server_sec`;
- **between unions** — `transport_cross_union_sec` (always the longest).

`transport_per_unit_sec` optionally lengthens a shipment by its size. On **arrival** the goods reach the buyer and the escrowed payment is released to the seller (counting into its **period sales** when it lands in the salary currency); the bot announces the arrival in the channel where the export was launched. Perishable categories — **food** and **pharmacy** — run a *risk* of spoiling rather than paying a toll: the longest trip the map allows carries the category's full `transport_perish_chance`, a shorter one proportionally less, and durable categories (wardrobe, hygiene, household) none at all. When the risk does come true the loss is a share of the batch drawn up to `transport_perish_max_share` (a third), and at least one unit always arrives — so a long haul with food is a gamble worth weighing rather than an arithmetic certainty, and the odds are stated against the half hour a long delivery actually takes. `/transit` lists an enterprise's cargo still on the way, and the `/enterprise` card shows an *in transit* summary. If either enterprise is deleted mid-transit, the goods and any escrow are refunded to the surviving side. Shipments live in the database, so they survive a restart and arrive on schedule.

**Salaries** are paid from the enterprise's account on the schedule its leader picks (weekly or monthly, in `/edit-enterprise`), in the enterprise's salary currency: a worker gets their personal override (`/ent-salary`) if set, otherwise the salary of their position (`/ent-position` + `/ent-assign`). Either kind is an exact amount or a **percent of the period's sales**; payroll stops when the account runs dry (an enterprise cannot go into payroll debt), and the sales counter resets after each payout.

#### Daily consumption

Every day each set-up server **eats**. The daily tick counts the server's active population — the distinct people who produced anything on it in the last 24 hours — and turns that into a demand per base category:

```
need = prov_daily_units_per_person × population × category_weight
```

The demand is served out of the warehouses of the **enterprises based on that server**, market goods first and base goods as the filler behind them, until `ceil(need)` units are taken or the warehouses run dry. Within each tier the demand is spread **evenly over every warehouse holding stock**, a pass at a time, rather than emptying whichever one happens to sort first. Every unit is **bought**, not requisitioned: the enterprise is paid the unit value its quality commands (base goods carry no market value and so feed for free), and the proceeds count into its period sales like any other sale. A server with no producers that day is not fed at all, and nothing is taken from personal inventories — only enterprises supply the population.

Because no bank buys goods, this daily shopping is the enterprises' **main income** and one of the currency's only two sources, which is why the even draw matters: it is a market being served, not a race being won.

Each day's demand and what covered it is written to `server_consumption`, keyed by the date so a tick that runs twice cannot count the same meal into the week.

#### Provision and weekly statistics

**Provision** measures how much of what a server's people needed actually reached them over the trailing week. It reads the consumption rows back rather than recomputing anything:

```
satisfaction = Σ qty × quality_mult    # base goods count 1.0; categorised custom
                                       # goods add prov_quality_bonus per level > 1
score        = min(Σ satisfaction / Σ need, cap)
```

While only base goods feed a category its score is capped at **1.0** — base production alone reaches *acceptable*, never higher; custom categorised goods lift the cap to `prov_max_score`. The overall score is the weight-averaged category score (+`prov_task_bonus` while last month's task stands completed), and maps to a level: < 40 % *insufficient*, ≤ 100 % *acceptable*, ≤ 150 % *good*, above — *excellent*.

Because the score is coverage rather than output, **stock matters**: goods sitting in a warehouse feed the server, and a category nobody supplies drags the whole average down however busy the chat is. The weekly statistics mark each category that ended the week short.

`/setlogs` names the server's **economic log channel** and its posting schedule (weekday, time, timezone as a UTC offset). The bot posts there the daily FX update (each union currency's value with its day-over-day change) and the weekly statistics: provision by category, goods produced, **GDP** (the week's production value, weighted by each currency's published value), the server's banks and the running task progress.

#### Monthly tasks

`/settasks` names the channel where the bot posts the server's **production task** on the 1st of each month (time and timezone configurable). The task is generated from the server's own history: every base category must grow on last month's output (`task_growth`, floored at `task_min_qty` units) — that part is **mandatory and must be produced on this server/group** — plus up to `task_optional_goods` of the server's top custom goods as optional extras. The post also announces the verdict on the previous month's task; a completed task grants the provision bonus for the following month. Progress is visible in the weekly statistics.

#### Fines and debt

`/fine` (bank leaders) debits the target's account in that bank; the balance **may go negative** — a negative balance is the member's debt to the bank. Because balances are signed, incoming money automatically pays debt down first. No interest accrues. The bank's card shows the summed debt outstanding.

#### Currency exchange

`/treaty <code>` proposes a conversion treaty between two banks; a leader of the other bank accepts through consent buttons, making A↔B convertible. Each day the scheduler recomputes every currency's **value** from three factors — goods backing, money supply and message activity:

```
backing  = period_goods_value / max(money_supply, 1 unit)     # goods crafted this period, at their unit values
activity = 1 + fx_activity_weight × ln(1 + period_energy)     # message energy earned this period
value    = clamp(max(backing × activity, fx_min_value))       # moves at most ±fx_daily_clamp per day
```

The rate A→B = value(A) / value(B). **Manual peg:** if two banks are each other's **only** treaty partner, their leaders may fix the rate by mutual consent with `/set-rate`, overriding the computed one; as soon as a bank has two or more partners only computed rates apply (protecting triangle consistency). `/convert` exchanges between your own accounts at the current rate, minus the source bank's spread; `/rates` shows values and rates.

**The spread belongs to the bank the money leaves.** Its leaders set it in `/edit-bank` → *Conversion fee*: one percentage for every currency, plus a percentage of its own for any particular currency it is worth treating differently (`USD 1`, and `USD -` to drop that one again). Unset, the deployment's `convert_fee` applies. The rows are keyed by currency code and travel with it, so renaming a currency does not detach its fees.

The fee is charged in the currency being sold and **paid to that bank's leaders**, split evenly, the remainder of an uneven split going to the first of them. A bank has no treasury of its own — no account row exists for one — so its leaders are where its income can go; a bank with no leaders left burns the fee instead, which is what every bank used to do with it.

#### Party economy, dues, wages

Parties hold accounts like users; their balances appear in `/party`. `/party-dues <code> <amount>` sets a mandatory monthly member contribution: the monthly scheduler moves it from each member's account to the party's (a shortfall pushes the member into debt to the bank). `/set-wage <profession> <amount>` sets a monthly wage in a bank: the monthly scheduler pays each account holder the wage of their profession (an emission; the sinks against it are fines and — while a bank charges one — the part of a conversion spread that no leader is there to collect).

#### Scheduler

By the pattern of the retention/backup loops: a **daily** tick (consumption, then the FX recompute — in that order, so the money paid for the day's meal is already in the supply the recompute prices against — period-window reset, autosend, task evaluation), a **weekly** tick (weekly enterprise payroll, recurring export shipments) and a **monthly** tick (wages, party dues, monthly enterprise payroll). Last-run markers are stored in `bot_settings`, so restarts never skip or double-run a period. Alongside them run an hourly **autoproduction** advance, a 20-second loop that completes due production runs, a 20-second loop that delivers **arrived shipments**, and a per-minute loop that delivers each server's weekly statistics and monthly task at the schedule (and in the timezone) that server configured — a post missed while the bot was down goes out as soon as it is back.

### Quizzes

`/quizzes` offers ideology quizzes with shuffled options, works in DMs, supports pausing (`/quizzes-stop`, progress kept 7 days). A result is **stored only with the user's consent**, at most the two most recent attempts per quiz; `/quizzes-compare` puts two users' breakdowns side by side (can be forbidden in `/privacy`), `/quizzes-previous` compares your two attempts.

### Olympiad

A bounded event for choosing the best wiki projects. `/setolympiad MM-DD-YYYY MM-DD-YYYY` opens it and fixes the voting period (UTC, the closing day counting in full); `/setolympiad off` cancels it. From the moment it is opened until the period ends, Bot Admins may run the setup commands; everyone else may vote only inside the announced window.

The commands are **hidden until the event exists**. On Discord `/setcontest`, `/editcontest`, `/olympiad`, `/accepted` and `/denied` are added to the command tree when an Olympiad opens and removed when it closes, so they never show up in the slash-command picker otherwise (Discord may take up to an hour to distribute the change). On Telegram, where no command list is published, the same commands are left out of `/help` and refuse to run.

A **contest** (`/setcontest`) belongs to a language category — one of the six localizations, or `int` for a cross-language contest, which is named in every language at once. It carries the ids of two Discord chats: one where votes go for review, one where accepted votes are published. Its **candidate wikis** (`/editcontest`) have a name, a link, and — in an `int` contest — a language code.

**Voting** (`/olympiad`) happens only in a private chat with the bot. Someone who has not picked a language for that chat is asked in English, with a button per localization. The bot then wants to know the person's Fandom account: it skips the question when `/verify` (or an earlier `remember`) already told it, and otherwise takes either a Fandom username or — behind the "Another way" button — a link to a profile on any wiki host that shows their messenger handle. Next it asks for a wiki of the Confederations or of an Olympiad participant where they have over 50 edits or messages, which speeds the review up. Then comes the contest, up to three candidate wikis (any mix of commas, semicolons and spaces; whole numbers, not digits), and finally either a written-out vote or a button giving each chosen wiki one point. Every question carries a **Stop** button that abandons the dialog.

The vote arrives in the review chat as an embed: the contest as the title, the supported wikis as the subtitle, then the account confirmation, the activity claim and the vote itself, with the voter, their messenger id and the vote's key in the footer. A reviewer answers with `/accepted <key> <wiki username> [remember]` — which publishes the vote to the accepted-votes chat under the voter's messenger name and that wiki name, and with `remember` also links the account for the bot — or `/denied <key> <reason>`, and either way the voter gets a DM. One person has one vote per contest; voting again replaces the earlier one and goes back for review, marked as an updated vote. Points are counted by people, not by the bot.

When the voting period ends, the contests, their candidates and every vote — reviewed or not — are **deleted**. Only two things outlive an Olympiad, and both expire a year later: the language someone chose in their private chat, and a wiki account a reviewer chose to remember.

### Wiki anniversaries

`/wiki-founday` registers a wiki's founding date; every year at the configured UTC time the bot posts a localized congratulation (via a webhook where it may manage webhooks) — bilingual when the wiki's language differs from the chat's.

### Localization

Every reply exists in six languages (en, ru, uk, pl, es, pt) with per-key verification status. `/lang` sets a chat's default, `/locallang` overrides per channel/topic/thread. `/locale` shows per-language progress bars or sends the raw file, `/loc-compare` shows one reply across languages, `/loc-suggest` sends a suggestion to the operator's support chats, answered with `/loc-reply`. Currency names stay in English (as entered), decorated with the currency emoji.

On Discord the economy speaks entirely in **embeds** (default color `#245590`); Telegram uses HTML formatting. Discord users mentioned in Telegram replies are shown by their stored nickname, never as an unresolvable ping.

### The seven-day setup deadline

The bot is invited far more often than it is put to use, so a server or group it has just been added to has **seven days** to be bound to a union with `/setup`. If that never happens, the bot leaves on its own and says so in `SERVICE_CHATS`, where it also announces every chat it is added to.

Nothing is said in the community itself, neither on arrival nor on the way out. `/setup` is a bot-admin command, so the people who could act on a warning are the operators — and `SERVICE_CHATS` is exactly where they read.

The rule is deliberately narrow:

- **It never touches a community the bot was already in.** The deadline counts from the join, and the moment the rule came into force is recorded once, on the first start of the version that introduced it; everything that joined before that instant is out of reach for good.
- **It fires at most once per community.** The first daily sweep that finds a chat bound to a union settles it permanently.
- **It skips the deployment's own chats.** A server or group holding any chat named in `config.py` — `SERVICE_CHATS`, `BACKUP_CHATS`, `SUPPORT_CHATS` — is never left.

On Discord the week is measured from Discord's own record of when the bot joined, so a restart or a missed event cannot shorten it. Telegram publishes no such timestamp, so there the clock starts from the update that adds the bot to the group, and a group whose arrival the bot never saw is never examined. Leaving is not a deletion: a community that invites the bot back starts a fresh seven days.

### Service events and automatic backups

Start/stop notices, administrative events (`/setup`, `/add-unia`, `/allow-parties`, party and bank creation/deletion) and the chats the bot is added to or leaves on the setup deadline go to the configured `SERVICE_CHATS`. Every 12 hours (and on `/backup`) the database is sent to `BACKUP_CHATS` — always encrypted with an authenticated BLAKE2 keystream; the key never leaves the operator's `BACKUP_KEY` environment variable. `restore_backup.py` decrypts a backup file back into `dem.db`.

---

## Data collection and retention

See [PRIVACY.md](PRIVACY.md) for the complete inventory of what the bot stores (including the economy's accounts, transaction journal, goods, inventories, professions, wages, dues, fines/debts, treaties and rates), where data goes, what other users can see, retention periods and the choices users have.
