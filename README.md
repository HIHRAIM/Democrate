# Democrate

Democrate is a cross-platform coordination bot for **unions** of wiki communities that runs on Discord and Telegram. It keeps a registry of unions and the servers/groups bound to them, hosts each union's **political parties** and **government bodies**, verifies users through their **Fandom** profiles, runs ideology **quizzes**, announces **wiki anniversaries**, and powers a cross-community **economy**: banks with their own currencies, message-based earning, craftable goods, salaries, fines, party dues and currency exchange. All replies are localized into six languages (en, ru, uk, pl, es, pt).

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
     - `ECONOMY` — the economy's tuning constants (documented inline): message-earning anti-abuse knobs (`min_chars`, `cooldown`, `sqrt_cap`, `hourly_activity_cap`, `currency_per_activity`, `energy_per_activity`), crafting-mastery curve (`alpha`, `beta`, `autocraft_cap`), and foreign-exchange behavior (`fx_daily_clamp`, `fx_activity_weight`, `fx_min_value`, `fx_initial_value`, `convert_fee`).

   > **Message Content intent:** message earning, the interactive dialogs and the numbered menus read ordinary messages, which requires the privileged **Message Content Intent** — enable it for the bot in the Discord Developer Portal, otherwise the bot will not start.

5. **Run the bot**
   ```bash
   python src/main.py
   ```

---

## Commands

Permission roles used below:

- **Everyone** — any user in a set-up chat (some commands additionally require [verification](#verification)).
- **Server Admins** — members with the native Administrator/Manage Server permission (Discord), group creator/administrators (Telegram), plus users delegated with `/setadmin`.
- **Localizers** — users granted `/localizer-add`: they may edit this bot's localization through the [control panel](https://github.com/HIHRAIM/Confederate-Panel). Delegated Server Admins hold the status implicitly while they remain admins.
- **Bot Admins** — user IDs listed in `config.py`.
- **Party Leaders** — a party's founder and appointed leaders.
- **Bank Leaders** — a bank's appointed leaders (need not be admins).

### Discord commands

| Command | Purpose | Everyone | Server Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/verify <fandom_name>` | Verify by matching your Discord handle on your Fandom profile | ✅ | ✅ | ✅ |
| `/add-telegram <nickname>` | Link your Telegram account (run `/add-discord` on Telegram within 30 min) | ✅ | ✅ | ✅ |
| `/party <code\|name>` | A party's card: texts, logo, leaders, members, seats, bank balances | ✅ | ✅ | ✅ |
| `/govt <name>` | A government body's members with party affiliations | ✅ | ✅ | ✅ |
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
| `/setadmin <user>` · `/remadmin <user>` | Grant/revoke delegated Server Admin rights (ping or ID) | ❌ | ❌ | ✅ |
| `/localizer-add <user>` · `/localizer-rem <user>` | Grant/revoke Localizer status — localization editing in the control panel (ping, ID or username); DMs the user | ❌ | ❌ | ✅ |
| `/setup <union> <lang>` | Bind this server to a union | ❌ | ❌ | ✅ |
| `/add-unia <code> <names…>` | Create a union with localized names | ❌ | ❌ | ✅ |
| `/allow-parties <union> <enable\|disable> [roles]` | Turn parties on/off in a union | ❌ | ❌ | ✅ |
| `/add-govt <union> <name> <singular> <plural>` | Create a government body | ❌ | ❌ | ✅ |
| `/edit-party-admin <party>` | Edit/move/delete any party of any union | ❌ | ❌ | ✅ |
| `/loc-reply <code> <text>` | Reply to a localization suggestion | ❌ | ❌ | ✅ |
| `/list_chats` · `/force_leave <platform> <id>` | List all bound chats / force-leave one | ❌ | ❌ | ✅ |
| `/backup` | Get an encrypted database backup | ❌ | ❌ | ✅ |

#### Discord economy commands

| Command | Purpose | Who |
|---|---|---|
| `/create-bank` | Found a bank and its currency (interactive dialog: central server → currency name → 4-char code → emoji) | Bot/Server Admins |
| `/bank <code>` | A bank's card: currency, union, central server, leaders, money supply, accounts, debt, value | Everyone |
| `/bank-add-leader <code> <user>` | Offer co-leadership of a bank (consent buttons; Bot Admins appoint directly) | Bank Leaders / Bot Admins |
| `/bank-transfer <code> <user>` | Offer the bank's leadership (consent buttons; Bot Admins appoint directly) | Bank Leaders / Bot Admins |
| `/open-account <code>` | Open an account (also opened automatically on first earning) | Verified |
| `/balance [code]` | Your money, energy and profession — all banks, or one | Verified |
| `/pay <user> <amount> <code>` | Transfer money to another user (same currency, atomic) | Verified |
| `/set-earn <code> <on\|off> [rate]` | Make the current channel earn a bank's currency | Bank Leaders / Server Admins / Bot Admins |
| `/create-good <name> <base_value> <energy_cost> [emoji]` | Create a craftable good owned by your bank | Bank Leaders |
| `/craft <good_code>` | Spend energy, craft one unit into your inventory | Verified |
| `/inventory` | Your goods with quantities, unit values and quality levels | Verified |
| `/sell <good_code> [qty]` | Sell goods back to the bank at the current unit value (the bank mints the proceeds) | Verified |
| `/autocraft <good_code> <on\|off>` | Auto-craft daily while energy lasts | Verified |
| `/autosend <good_code> <percent> <user\|party>` | Auto-send a share of a good's stock daily | Verified |
| `/set-profession <user> <good_code>` | Override a member's profession in your bank | Bank Leaders |
| `/fine <user> <amount> [reason]` | Fine a user in your bank's currency (balance may go negative — a debt) | Bank Leaders |
| `/treaty <code>` | Propose a conversion treaty with another bank (their leader accepts via buttons) | Bank Leaders |
| `/set-rate <code> <rate>` | Fix a pegged rate — only with your bank's **sole** treaty partner | Bank Leaders |
| `/convert <amount> <from> <to>` | Convert between your own accounts at the current rate (fee applies) | Verified |
| `/rates [code]` | Currency values, or one bank's conversion rates | Everyone |
| `/set-wage <profession> <amount>` | Monthly wage for a profession in your bank (0 clears) | Bank Leaders |
| `/party-dues <code> <amount>` | Your party's monthly member dues in a currency (0 clears) | Party Leaders |

### Telegram commands

Telegram mirrors the Discord commands (both `/cmd_name` and `/cmd-name` spellings are accepted). Differences:

| Command | Difference from Discord |
|---|---|
| `/add-discord <nickname>` | Telegram's half of account linking (instead of `/verify`; verification is inherited from the linked, Fandom-verified Discord account) |
| `/add-party` | Points to Discord: parties are founded there because founding is gated by Discord roles |
| `/create-good <name> \| <base_value> \| <energy_cost> \| [emoji]` | Arguments are separated by `\|` |
| `/pay <amount> <code>` and `/fine <amount> [reason]` | Can also be used as a **reply** to the target's message (the explicit `<user>` argument is then omitted) |
| `/find-with`, `/find-without`, `/wiki-founday*` | Discord-only (they operate on Discord channels) |
| `/lang`, `/locallang` | Available to group administrators, as on Discord |
| `/setadmin`, `/remadmin`, `/localizer_add`, `/localizer_rem` | Target may be given as an ID, a public `@username`, or by replying to the user's message |

All other commands — `/setup`, `/add-unia`, `/allow-parties`, `/party`, `/govt`, `/edit-party`, `/edit-rules`, `/edit-party-admin`, `/party-join`, `/party-leave`, `/party-kick`, `/add-govt`, `/quizzes*`, `/privacy`, `/locale`, `/loc_compare`, `/loc_suggest`, `/loc_reply`, `/list_chats`, `/force_leave`, `/backup`, `/help`, and the whole economy set (`/create_bank`, `/bank`, `/bank_add_leader`, `/bank_transfer`, `/open_account`, `/balance`, `/pay`, `/set_earn`, `/craft`, `/inventory`, `/sell`, `/autocraft`, `/autosend`, `/set_profession`, `/fine`, `/treaty`, `/set_rate`, `/convert`, `/rates`, `/set_wage`, `/party_dues`) — work the same as on Discord, with the same permissions.

---

## Mechanics

### Unions and chats

A **union** is a confederation of wiki communities created with `/add-unia` (code + names in any subset of the six languages). `/setup` binds a Discord server or Telegram group to a union and sets its language. Most public commands only work in set-up chats. When the bot leaves a chat, the binding and the chat's language settings are removed.

### Verification

To use the Fandom-activity commands (parties, the whole economy), users verify once, globally:

- **Discord:** `/verify <fandom_username>` fetches that Fandom profile through Fandom's public API and checks that the Discord handle published there matches the caller.
- **Telegram:** users link their Telegram account to a Fandom-verified Discord account: `/add-telegram <tg_nick>` on Discord + `/add-discord <discord_nick>` on Telegram, both within 30 minutes, each side naming the other.

A linked pair is treated as **one person**: one quiz history, one set of privacy settings, one wallet in every bank.

### Parties

Parties belong to a union and are enabled per union with `/allow-parties` (with the Discord role IDs whose holders may found them). `/add-party` runs an interactive dialog (name → 4-char code → logo; each answer waited up to 30 minutes). Founders/leaders manage the party through numbered `/edit-party` menus; leadership offers and transfers require the target's consent through «Accept / Decline» buttons on both platforms. Joining goes through `/party-join` with leader approval delivered as DM buttons that survive bot restarts. `/party` shows the card, including the party's bank balances.

### Governments

`/add-govt` creates a named body in a union (with its member titles); `/govt` shows a body's members together with their party affiliations. Seats are counted on party cards.

### Economy

The economy adds cross-community currencies with a real production cycle behind them. Its tuning constants live in `config.ECONOMY`.

#### Banks and currencies

`/create-bank` (Bot/Server Admins, interactive dialog) creates a **bank**: it is anchored to a **central server** (which must be `/setup`-bound) and thereby to that server's union. A server or union can host any number of banks. The currency has an English name, a 4-character code — unique across the shared namespace of union, party, currency **and** good codes — and an emoji. The creator becomes the bank's first leader; leadership works like party leadership (consent-button offers and transfers, `/bank-add-leader`, `/bank-transfer`), except that Bot Admins may appoint leaders anywhere directly. A bank leader does not need to be any kind of admin.

#### Money, accounts, journal

Money is stored as **integers in minor units** (hundredths) and displayed with two decimals plus the currency emoji and code. Each user (and each party) has at most one account per bank — opened explicitly with `/open-account` or automatically on first earning. Every balance change — earning, `/pay`, `/sell`, `/fine`, `/convert`, wages, dues — goes through atomic operations under the database lock and is recorded in an **append-only transaction journal** (from, to, amount, currency, reason, time).

#### Earning by messages

`/set-earn <code> <on|off> [rate]` marks the current channel/topic as earning a specific bank's currency (the chat must belong to the bank's union) — this is how several banks coexist on one server: each channel chooses which currency it feeds. Unconfigured channels earn nothing.

Each counted message produces one activity value **A = min(√chars, `sqrt_cap`)** — sublinear in length with a ceiling, so long messages are rewarded finitely and one-word spam earns ~0. From A the user is credited **both resources at once**: currency (`A × rate × currency_per_activity`) and **energy** (`A × energy_per_activity`). Anti-abuse: messages shorter than `min_chars`, commands, bot messages and edits are ignored; a per-user `cooldown` runs between counted messages; a rolling **hourly cap** limits total A per user. Only **verified** users earn.

#### Goods, crafting, mastery

Bank leaders create **goods** with `/create-good` (base value in the bank's currency, energy cost, emoji; the good receives its own unique 4-char code). `/craft` spends energy and adds one unit to the inventory. Mastery **m** = units of that good the user has ever produced:

- quality level = `1 + floor(log₂(m+1))`
- unit value = `base_value × (1 + alpha·√m)`
- craft cost = `energy_cost × (1 + beta·level)`

— the more you produce, the higher your quality and per-unit value, but crafting gets costlier, keeping value finite. `/sell` sells to the bank at the current unit value — the bank **mints** the proceeds, which is the economy's money source. `/autocraft` crafts daily while energy lasts; `/autosend` moves a percentage of a good's stock daily to a chosen user or party. A user's **profession** in a bank defaults to the good they have produced the most of; a bank leader can override it with `/set-profession`.

#### Fines and debt

`/fine` (bank leaders) debits the target's account in that bank; the balance **may go negative** — a negative balance is the member's debt to the bank. Because balances are signed, incoming money automatically pays debt down first. No interest accrues. The bank's card shows the summed debt outstanding.

#### Currency exchange

`/treaty <code>` proposes a conversion treaty between two banks; a leader of the other bank accepts through consent buttons, making A↔B convertible. Each day the scheduler recomputes every currency's **value** from three factors — goods backing, money supply and message activity:

```
backing  = period_goods_value / max(money_supply, 1 unit)     # goods crafted this period, at their unit values
activity = 1 + fx_activity_weight × ln(1 + period_energy)     # message energy earned this period
value    = clamp(max(backing × activity, fx_min_value))       # moves at most ±fx_daily_clamp per day
```

The rate A→B = value(A) / value(B). **Manual peg:** if two banks are each other's **only** treaty partner, their leaders may fix the rate by mutual consent with `/set-rate`, overriding the computed one; as soon as a bank has two or more partners only computed rates apply (protecting triangle consistency). `/convert` exchanges between your own accounts at the current rate, minus the `convert_fee` spread (a money sink); `/rates` shows values and rates.

#### Party economy, dues, wages

Parties hold accounts like users; their balances appear in `/party`. `/party-dues <code> <amount>` sets a mandatory monthly member contribution: the monthly scheduler moves it from each member's account to the party's (a shortfall pushes the member into debt to the bank). `/set-wage <profession> <amount>` sets a monthly wage in a bank: the monthly scheduler pays each **verified** account holder the wage of their profession (an emission, balanced by the sinks — fines, dues, conversion fees).

#### Scheduler

By the pattern of the retention/backup loops: a **daily** tick (FX recompute, period-window reset, autocraft, autosend) and a **monthly** tick (wages, party-dues collection). Last-run markers are stored in `bot_settings`, so restarts never skip or double-run a period.

### Quizzes

`/quizzes` offers ideology quizzes with shuffled options, works in DMs, supports pausing (`/quizzes-stop`, progress kept 7 days). A result is **stored only with the user's consent**, at most the two most recent attempts per quiz; `/quizzes-compare` puts two users' breakdowns side by side (can be forbidden in `/privacy`), `/quizzes-previous` compares your two attempts.

### Wiki anniversaries

`/wiki-founday` registers a wiki's founding date; every year at the configured UTC time the bot posts a localized congratulation (via a webhook where it may manage webhooks) — bilingual when the wiki's language differs from the chat's.

### Localization

Every reply exists in six languages (en, ru, uk, pl, es, pt) with per-key verification status. `/lang` sets a chat's default, `/locallang` overrides per channel/topic/thread. `/locale` shows per-language progress bars or sends the raw file, `/loc-compare` shows one reply across languages, `/loc-suggest` sends a suggestion to the operator's support chats, answered with `/loc-reply`. Currency names stay in English (as entered), decorated with the currency emoji.

On Discord the economy speaks entirely in **embeds** (default color `#245590`); Telegram uses HTML formatting. Discord users mentioned in Telegram replies are shown by their stored nickname, never as an unresolvable ping.

### Service events and automatic backups

Start/stop notices and administrative events (`/setup`, `/add-unia`, `/allow-parties`, party and bank creation/deletion) go to the configured `SERVICE_CHATS`. Every 12 hours (and on `/backup`) the database is sent to `BACKUP_CHATS` — always encrypted with an authenticated BLAKE2 keystream; the key never leaves the operator's `BACKUP_KEY` environment variable. `restore_backup.py` decrypts a backup file back into `dem.db`.

---

## Data collection and retention

See [PRIVACY.md](PRIVACY.md) for the complete inventory of what the bot stores (including the economy's accounts, transaction journal, goods, inventories, professions, wages, dues, fines/debts, treaties and rates), where data goes, what other users can see, retention periods and the choices users have.
