# Democrate

Democrate is a cross-platform coordination bot for unions of wiki communities (confederations). It runs on Discord and Telegram at the same time, keeps a registry of unions, links Discord servers and Telegram groups to them, hosts the political life of each union — parties with leaders, logos, colors and ideologies, and government bodies — runs ideology quizzes with user-controlled privacy, and answers in six languages with per-server and per-channel language settings. Its Discord presence carries the motto **“Unio, progressio et diplomatia”**.

Democrate is the diplomatic companion to [Confederate](https://github.com/HIHRAIM/Confederate) and [Confederate Guard](https://github.com/HIHRAIM/Confederate-Guard) and shares their architecture: local SQLite storage, JSON localization files with community suggestion tooling, and encrypted automatic backups.

> **Roadmap:** message relay between the chats of a union and collecting poll votes through the bot's DMs into forum threads are planned features; the current release covers the union registry, parties, government bodies, quizzes, moderation channel scans and localization.

## Requirements

- Python **3.10+** (recommended 3.11+)
- A Discord bot token
- A Telegram bot token
- SQLite (uses local `dem.db`, no external DB required)
- Python packages used by the project (see `requirements.txt`):
  - `discord.py`
  - `aiogram`
  - `aiohttp` (used by `/verify` to reach Fandom's public API)

> **Message Content intent:** the party dialogs (`/add-party`, `/edit-party`, `/edit-party-admin`), the quizzes, the `/privacy` menu and the channel scans (`/find-with`, `/find-without`) read message content, which requires the privileged **Message Content Intent** — enable it for the bot in the Discord Developer Portal, otherwise the bot will not start.

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
   pip install -r requirements.txt
   ```

4. **Create config file**
   - Copy `src/config.example.py` to `src/config.py`.
   - Set environment variables (the example config reads tokens from env), or copy `src/.env.example` to `src/.env` and fill it in — the config loads it automatically (already-set environment variables take precedence):
     - `DISCORD_BOT_TOKEN` — your Discord bot token.
     - `TELEGRAM_BOT_TOKEN` — your Telegram bot token.
     - `BACKUP_KEY` — the encryption key for database backups (keep a copy outside the server; without it backups are unreadable).
   - Edit `src/config.py`:
     - `ADMINS["discord"]` and `ADMINS["telegram"]` — sets of numeric user IDs with global bot-admin rights.
     - `SERVICE_CHATS["discord"]` and `SERVICE_CHATS["telegram"]` — chat IDs where the bot sends startup/shutdown events. Telegram format: `"-1000000000000:0"` (chat\_id:thread\_id); Discord format: numeric channel ID.
     - `BACKUP_CHATS["discord"]` and `BACKUP_CHATS["telegram"]` — chat IDs where the bot sends automatic database backups every 12 hours. Same format as `SERVICE_CHATS`.
     - `SUPPORT_CHATS["discord"]` and `SUPPORT_CHATS["telegram"]` — chats that receive localization suggestions submitted via `/loc-suggest` (Discord as an embed, Telegram as a message). Same format as `SERVICE_CHATS`.

5. **Run the bot**
   ```bash
   python src/main.py
   ```

---

## Unions

A **union** is a confederation of wiki communities identified by a short code (2–12 Latin letters/digits) and named in up to six languages. The software ships with an empty registry: Bot Admins create every union at runtime with `/add-unia`, giving its name in at least one supported language. The name shown in replies follows the chat's language (falling back to English, then to any available name).

A Discord server or Telegram group joins a union with `/setup <union> <lang>` (Bot Admins). Until a chat is set up, only `/help` and Bot Admin commands work there — everything else answers with a “not set up yet” notice.

---

## Verification

A range of Fandom-activity commands — `/add-party`, `/edit-party`, `/edit-rules`, `/find-with`, `/find-without`, `/party-join` — require the caller to be **verified**, and prompt for it on first use.

- **On Discord**, `/verify <fandom_username>` proves you own a Fandom account: the bot reads the Discord handle you set on that Fandom profile (through Fandom's public API) and checks it against your Discord username. Verification is **global** — once verified, it applies on every server and union.
- **On Telegram**, you verify by linking your account to a verified Discord account: run `/add-discord <your_discord_name>` on Telegram and `/add-telegram <your_@username>` on Discord within 30 minutes of each other (either order). The link makes you verified on Telegram **as long as the linked Discord account is Fandom-verified**.

---

## Parties and government bodies

Each union can host **parties**. They are disabled by default; Bot Admins enable them per union with `/allow-parties`, at the same time listing the Discord role IDs whose holders may found parties. A party is created with the interactive `/add-party` dialog (name → 4-character code → logo; every answer is awaited for 30 minutes) and managed by its founder and leaders through `/edit-party` (rename, recode, logo, embed color, description, ideologies, co-leaders and ownership transfer with consent buttons, suspension, article link) and `/edit-rules`. Anyone can look a party up with `/party` — a fuzzy search with numbered suggestions when nothing matches exactly.

Membership is handled by `/party-join` (request to join; the party's Discord leaders get a DM with **Accept**/**Reject** buttons — one is enough), `/party-leave` (leave unilaterally) and `/party-kick` (a leader removes a member unilaterally). A user belongs to at most one party per union.

**Government bodies** are created per union by Bot Admins with `/add-govt` (body name plus what one/several members are called) and looked up with `/govt`, which lists the members separated by an interpunct, each with their party after a `|`. Commands for party alliances and appointing government members are planned; the database schema and the info cards already account for them.

---

## Quizzes

`/quizzes` lists the available quizzes and runs the one you pick — in a channel, a group or the bot's DMs. The first quiz, **WikiCharts**, is adapted from the open-source [WikiCharts test](https://github.com/WikiCharts/wikicharts.github.io): each question shows a topic and a set of statements, you answer `<number>-Mb` for a slight preference or `<number>-Y` for a definite one, and the answers are scored on four axes into a breakdown of ideologies.

- **Pausing.** `/quizzes-stop` parks the run: the answers so far are kept for **7 days**, and starting the same quiz again resumes it at the question you left off. After 7 days the parked progress is discarded and the quiz starts over.
- **Storing results is opt-in.** Before showing the results the bot asks whether it may remember them. Only the **two most recent** attempts of each quiz are kept, so a result always has at most one predecessor.
- **Comparing.** `/quizzes-previous [number]` puts your latest result next to the one before it, each labelled with the day it was taken. Called without a number it only works within **30 minutes** of finishing a quiz; afterwards pass the quiz's number from the `/quizzes` list. `/quizzes-compare <user>` compares your latest result with another user's.
- **One history across platforms.** Once you link your Discord and Telegram accounts (`/add-discord` + `/add-telegram`), both accounts share one quiz history: the two most recent attempts of each quiz, wherever they were taken, and one set of privacy settings.
- **Privacy.** `/privacy` opens a numbered menu covering everything the bot stores about you: delete all your quiz results, delete the results of one quiz, forbid other users from comparing themselves against you, or forbid it for one quiz only. Options that would do nothing — deleting results nobody stored, choosing among a single quiz — are left out, so the numbering follows what you actually have. `/quizzes-clear` deletes a single stored attempt.

Whatever a user consents to, a stored result is **never kept longer than ten years**.

---

## Wiki anniversaries

`/wiki-founday` (Discord, Server Admins) registers a wiki whose birthday the bot then announces **every year**, posting through a webhook named after the chat's language — “Дни рождения”, “Birthdays”, “Urodziny” and so on.

```
/wiki-founday url:amongus.fandom.com/ru name:Among Us Вики founded:06-15-2018 14:30
```

`founded` is UTC, `MM-DD-YYYY` or `MM-DD-YYYY HH:MM` (midnight when the time is left out). The optional arguments are the target channel (default: the channel the command was run in), the posting time `at` as `HH:MM` or bare `HH` (default `06:00` UTC), and `wiki_lang`, the wiki's own language (default: the chat's language).

The greeting is written in the past tense when the wiki's founding **time of day** falls before the posting time, and in the future tense when it is still ahead that day:

> Сегодня Among Us Вики исполнилось 2 года!
> Сегодня Among Us Вики исполнится 5 лет!

Year counts are pluralized per language (`1 год` / `2 года` / `5 лет`, `1 rok` / `2 lata` / `5 lat`, …). When the wiki's language differs from the chat's, its own greeting follows on a second line and the wiki's flag marks both:

> Сегодня Among Us Вікі исполнится 2 года! 🇺🇦
> 🇺🇦Сьогодні Among Us Вікі виповниться 2 роки!
> https://amongus.fandom.com/uk

`/wiki-foundays` lists what a server has registered, and `/wiki-founday-remove <url>` stops an announcement. Registering the same wiki url again updates the entry in place.

---

## Commands

Permission roles used below:

- **Everyone** — any user in a set-up server/group.
- **Server Admins** — the platform's native administrators: on Discord, members with the **Administrator** or **Manage Server** permission; on Telegram, the group's creator and administrators. Nothing is delegated through the bot.
- **Bot Admins** — global admins defined in `config.py` (`ADMINS`).
- ✳️ — special access: `/add-party` needs one of the roles configured with `/allow-parties`; `/edit-party`, `/edit-rules` and `/party-kick` are for the party's founder and leaders. (`/edit-party-admin` is the Bot Admin equivalent of `/edit-party` and needs no leadership.)
- 🔒 — requires [verification](#verification) (first use prompts for `/verify`).

### Discord commands

| Command | Purpose | Everyone | Server Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/setup <union> <lang>` | Link this server to a union and set its language | ❌ | ❌ | ✅ |
| `/verify <fandom_username>` | Verify ownership of a Fandom account (checks the Discord handle on your Fandom profile); unlocks the 🔒 commands, globally | ✅ | ✅ | ✅ |
| `/add-telegram <@username>` | Link your Telegram account (pair with `/add-discord` on Telegram within 30 min) | ✅ | ✅ | ✅ |
| `/add-unia <code> [name_en] [name_ru] [name_uk] [name_pl] [name_es] [name_pt]` | Create a new union; at least one name is required | ❌ | ❌ | ✅ |
| `/allow-parties <union> <enable\|disable> [role_ids]` | Enable/disable parties in a union; on `enable`, list the comma-separated role IDs allowed to use `/add-party` | ❌ | ❌ | ✅ |
| `/add-party [founder_id]` 🔒 | Found a party via an interactive dialog (name → 4-char code → logo; 30 min per answer). Only Bot Admins may specify a founder | ✳️ | ✳️ | ✅ |
| `/edit-party [option]` 🔒 | Party settings menu (embed with the logo top-right), or run option 1–10 directly: name, code, logo, color, description ⚠️, ideologies ⚠️, add leader, transfer, suspend/resume, article link ⚠️ | ✳️ | ✳️ | ✳️ |
| `/edit-rules [option]` 🔒 | Party rules menu; option 1 toggles whether members may invite new members (default: no) | ✳️ | ✳️ | ✳️ |
| `/party <code\|name>` | Party info card (embed in the party's color, logo top-right, “More” link button); fuzzy suggestions with a 30-minute numbered choice when nothing matches | ✅ | ✅ | ✅ |
| `/party-join <code\|exact name>` 🔒 | Request to join a party; its Discord leaders get a DM with Accept/Reject (one is enough) | ✅ | ✅ | ✅ |
| `/party-leave` | Leave your party in this union (the founder must transfer or suspend instead) | ✅ | ✅ | ✅ |
| `/party-kick <id\|username>` | Remove a member from your party | ✳️ | ✳️ | ✳️ |
| `/edit-party-admin <code\|full name>` | Settings menu (embed) for **any** party of any union, matched exactly by code or full name: the ten `/edit-party` options plus 11 the invite rule, 12 move to another union, 13 delete the party | ❌ | ❌ | ✅ |
| `/quizzes` | List the quizzes and take the one you pick; resumes a run parked with `/quizzes-stop` | ✅ | ✅ | ✅ |
| `/quizzes-stop` | Pause the quiz you are taking; the progress is kept for 7 days | ✅ | ✅ | ✅ |
| `/quizzes-previous [number]` | Compare your latest result of a quiz with the one before it, both labelled with their dates. Without a number, only within 30 minutes of finishing a quiz | ✅ | ✅ | ✅ |
| `/quizzes-compare <user>` | Compare your latest quiz results with another user's (refused if they forbade it in `/privacy`) | ✅ | ✅ | ✅ |
| `/quizzes-clear` | Delete one of your saved quiz results | ✅ | ✅ | ✅ |
| `/privacy` | Numbered menu of the privacy areas you can control: delete quiz results (all, or one quiz), and forbid others from comparing against you (all, or one quiz) | ✅ | ✅ | ✅ |
| `/add-govt <union> <name> <member> <members>` | Create a government body (name must be unique within the union) | ❌ | ❌ | ✅ |
| `/govt <name>` | Government body info: members listed with an interpunct, each with their party after a `\|`; fuzzy suggestions mark bodies of other unions | ✅ | ✅ | ✅ |
| `/find-with <channel_id> <MM-DD-YYYY[ + ND]> <keywords;…>` 🔒 | Scan the channel over the period and reply-mention the author of every message **containing** at least one keyword (embeds are searched too) | ❌ | ✅ | ✅ |
| `/find-without <channel_id> <MM-DD-YYYY[ + ND]> <keywords;…>` 🔒 | Same scan, but replies to every message **not containing** any of the keywords | ❌ | ✅ | ✅ |
| `/wiki-founday <url> <name> <MM-DD-YYYY[ HH:MM]> [channel_id] [at] [wiki_lang]` | Congratulate a wiki on its anniversary every year, through a “Birthdays” webhook. Defaults: this channel, 06:00 UTC, this chat's language | ❌ | ✅ | ✅ |
| `/wiki-foundays` | List this server's wiki anniversaries | ❌ | ✅ | ✅ |
| `/wiki-founday-remove <url>` | Stop announcing a wiki's anniversary | ❌ | ✅ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set the default bot language for the whole server (used wherever no `/locallang` override is set) | ❌ | ✅ | ✅ |
| `/locallang <ru\|uk\|pl\|en\|es\|pt>` | Set bot language for this channel/thread/forum post (overrides the server-wide `/lang`) | ❌ | ✅ | ✅ |
| `/locale [code]` | Show localization status (bar + verified %), or send a language's localization file (10-min per-server cooldown for the file) | ✅ | ✅ | ✅ |
| `/loc-compare <code>` | Compare a reply across all languages with status emoji | ✅ | ✅ | ✅ |
| `/loc-suggest <lang> <code> <text>` | Suggest a localization; sent to the support chats | ✅ | ✅ | ✅ |
| `/help` | Show command reference | ✅ | ✅ | ✅ |
| `/loc-reply <code> <text>` | Reply (via DM) to a user's localization suggestion | ❌ | ❌ | ✅ |
| `/list_chats` | List all Discord servers and Telegram groups known to the bot, with their unions | ❌ | ❌ | ✅ |
| `/force_leave <platform> <id>` | Force the bot to leave a server/group and clean up DB records | ❌ | ❌ | ✅ |
| `/backup` | Send the current encrypted database backup file | ❌ | ❌ | ✅ |

### Telegram commands

| Command | Purpose | Everyone | Server Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/setup <union> <lang>` | Link this group to a union and set its language | ❌ | ❌ | ✅ |
| `/add_discord <discord_name>` | Link your Discord account (pair with `/add-telegram` on Discord within 30 min); this verifies you on Telegram | ✅ | ✅ | ✅ |
| `/add_unia <CODE> en=Name \| ru=Название \| …` | Create a new union; at least one `lang=name` pair is required | ❌ | ❌ | ✅ |
| `/allow_parties <union> <enable\|disable> [role_ids]` | Enable/disable parties in a union (role IDs are Discord roles) | ❌ | ❌ | ✅ |
| `/add_party` | Points to Discord: parties are founded there, because the required roles can only be checked on Discord | — | — | — |
| `/edit_party [option]` 🔒 | Party settings menu (with the logo attached), or run option 1–10 directly; leaders are referenced as `@username` or ID, consent is given with inline «Принимаю»/«Не принимаю» buttons | ✳️ | ✳️ | ✳️ |
| `/edit_rules [option]` 🔒 | Party rules menu; option 1 toggles whether members may invite new members (default: no) | ✳️ | ✳️ | ✳️ |
| `/party <code\|name>` | Party info card (logo photo + HTML text, “More” URL button); fuzzy suggestions with a 30-minute numbered choice | ✅ | ✅ | ✅ |
| `/party_join <code\|exact name>` 🔒 | Request to join a party; its Discord leaders get a DM with Accept/Reject | ✅ | ✅ | ✅ |
| `/party_leave` | Leave your party in this union (the founder must transfer or suspend instead) | ✅ | ✅ | ✅ |
| `/party_kick <id\|username>` | Remove a Telegram member from your party | ✳️ | ✳️ | ✳️ |
| `/edit_party_admin <code\|full name>` | Settings menu for **any** party of any union, matched exactly by code or full name: the ten `/edit_party` options plus 11 the invite rule, 12 move to another union, 13 delete the party | ❌ | ❌ | ✅ |
| `/quizzes` | List the quizzes and take the one you pick; resumes a run parked with `/quizzes_stop` | ✅ | ✅ | ✅ |
| `/quizzes_stop` | Pause the quiz you are taking; the progress is kept for 7 days | ✅ | ✅ | ✅ |
| `/quizzes_previous [number]` | Compare your latest result of a quiz with the one before it, both labelled with their dates. Without a number, only within 30 minutes of finishing a quiz | ✅ | ✅ | ✅ |
| `/quizzes_compare <user>` | Compare your latest quiz results with another user's (refused if they forbade it in `/privacy`) | ✅ | ✅ | ✅ |
| `/quizzes_clear` | Delete one of your saved quiz results | ✅ | ✅ | ✅ |
| `/privacy` | Numbered menu of the privacy areas you can control: delete quiz results (all, or one quiz), and forbid others from comparing against you (all, or one quiz) | ✅ | ✅ | ✅ |
| `/add_govt <UNION> <name> \| <member> \| <members>` | Create a government body (name must be unique within the union) | ❌ | ❌ | ✅ |
| `/govt <name>` | Government body info: members listed with an interpunct, each with their party after a `\|` | ✅ | ✅ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set the default bot language for the whole group | ❌ | ✅ | ✅ |
| `/locallang <ru\|uk\|pl\|en\|es\|pt>` | Set bot language for the current topic (overrides the group-wide `/lang`) | ❌ | ✅ | ✅ |
| `/locale [code]` | Show localization status, or send a language's localization file (10-min per-group cooldown for the file) | ✅ | ✅ | ✅ |
| `/loc_compare <code>` | Compare a reply across all languages with status emoji | ✅ | ✅ | ✅ |
| `/loc_suggest <lang> <code> <text>` | Suggest a localization; sent to the support chats | ✅ | ✅ | ✅ |
| `/help` | Show command reference (the reply self-deletes after a minute) | ✅ | ✅ | ✅ |
| `/loc_reply <code> <text>` | Reply (via DM) to a user's localization suggestion | ❌ | ❌ | ✅ |
| `/list_chats` | List all Discord servers and Telegram groups known to the bot, with their unions | ❌ | ❌ | ✅ |
| `/force_leave <platform> <id>` | Force the bot to leave a server/group and clean up DB records | ❌ | ❌ | ✅ |
| `/backup` | Send the current encrypted database backup file (private chat with the bot only) | ❌ | ❌ | ✅ |

> Telegram command names use underscores where Discord uses hyphens (`/loc_compare` ↔ `/loc-compare`, `/party_join` ↔ `/party-join`); both spellings are accepted on Telegram.

---

## Mechanics

### Setup gating

`/setup` (Bot Admins) binds a Discord server or Telegram group to a union and sets its language. The binding is stored per server/group; running `/setup` again re-binds the chat to another union. Until a chat is set up, its members can only use `/help` — the public localization commands answer with a localized “not set up yet” notice, so the bot stays silent-by-default on servers that merely invited it.

### Languages

Replies are localized **per chat**. The language is resolved in this order: the channel/thread/topic's own `/locallang` setting → the server/group-wide default set with `/lang` (or `/setup`) → English. Supported languages: `ru`, `uk`, `pl`, `en`, `es`, `pt`.

### Verification

The Fandom-activity commands (`/add-party`, `/edit-party`, `/edit-rules`, `/find-with`, `/find-without`, `/party-join`) are gated: an unverified caller is told to verify first, and the command does nothing else.

On Discord, `/verify <fandom_username>` resolves the Fandom account through two public, unauthenticated Fandom endpoints — the MediaWiki users API (username → numeric id) and the user-attribute service (id → `discordHandle`) — and compares the profile's Discord handle to the caller's Discord username (case-insensitively, ignoring any legacy `#discriminator`). On success the Discord user id is stored as verified. Verification is **global**: it is a property of the account, not of a server or union.

On Telegram, verification is derived from an account link. `/add-discord <discord_name>` (Telegram) and `/add-telegram <@username>` (Discord) form a mutual handshake: each command records the caller's own username and the other account it claims, and the link is made the moment the two halves reference each other, in either order, within **30 minutes**. A Telegram user counts as verified only while linked to a Discord account that is itself Fandom-verified — so a `/verify` on the Discord side that happens after the link takes effect immediately. Linking needs the Telegram user to have a public `@username`.

### Parties

Parties live inside a union and are off until a Bot Admin runs `/allow-parties <union> enable <role_ids>`. Until then **every** party command — `/party`, `/party-join`, `/party-leave`, `/party-kick`, `/edit-party`, `/edit-rules`, `/add-party`, and their Telegram spellings — answers with a localized “parties are disabled in this union”. `/edit-party-admin` is the one exception, so a Bot Admin can still tidy up or delete a party after parties were switched off.

Founding (`/add-party`, Discord only) is a dialog in the channel: the bot asks for the name, then a unique 4-character code (Latin letters/digits, sharing one namespace with union codes), then the logo image; each answer is awaited for **30 minutes**, invalid answers are re-asked. A Bot Admin may found a party on someone else's behalf via the `founder` parameter.

`/edit-party` shows a numbered settings menu (options that are still unset — description, ideologies, article link — are marked with ⚠️) or runs one option directly. Adding a co-leader (7) and transferring the party (8) mention the target user — `<@id>` on Discord, `@username` on Telegram — and require them to press «Принимаю»; otherwise the leadership stays unchanged. Suspending (9) asks for confirmation, keeps all data, marks the party as suspended in `/party`, and turns option 9 into “resume”. Descriptions and ideologies keep their links and formatting across platforms: they are stored as Discord markdown and rendered as HTML on Telegram.

`/party` finds a party by exact code/name, otherwise offers the closest matches (or 3 random parties of the union) as a numbered list and waits 30 minutes for the choice. The info card shows the founder, the current leaders (when they differ from the founder), ideologies, allied parties, the member count, government seats held by party members, the description and a localized “More” button with the article link; on Discord it is an embed in the party's color (default `#245590`) with the logo top-right.

`/edit-party-admin <code|full name>` (Bot Admins) is the same menu for any party of any union, with no leadership check: the argument must match a party code or its full name exactly (case-insensitively). The menu — an embed on Discord, in the party's color — carries the ten `/edit-party` options plus **11** the member-invite rule, **12** moving the party to another union, and **13** deleting the party. The Bot Admin answers with the option number within 30 minutes. Deletion asks for confirmation, then erases the party together with its leaders, members, alliances and open join requests, and reports it to the service chats.

### Quizzes

`/quizzes` lists the quizzes and runs the chosen one as a dialog in the current channel/chat (DMs included). Each question is a topic with numbered statements; the option order is reshuffled per question so answer position does not bias respondents. An answer is `<number>-Mb` (slight preference) or `<number>-Y` (definite); the trailing “disagree with all” / “don't care” / “previous question” options need only their number. Answers are scored on four axes by a faithful port of the WikiCharts `computeScores.js` vector algorithm.

`/quizzes-stop` sets a stop flag that the running dialog races against the next answer: the current question index, the shuffled option orders and the answers so far are stored and kept for **7 days** (pruned daily by the retention loop). Starting the same quiz again resumes it automatically at the parked question — the start prompt is skipped — and finishing or letting the parking expire clears it. Parked progress is per platform; a run parked on Discord is resumed on Discord.

Storing results is opt-in: the bot asks before showing them. Only the **two most recent** attempts of each quiz survive — saving a third drops the oldest. `/quizzes-previous [number]` compares the newest attempt with its predecessor, labelling each with the day it was taken; the number is the quiz's position in the `/quizzes` list, and may be omitted only within **30 minutes** of finishing a quiz. `/quizzes-compare <user>` compares your latest result with another user's.

Linking a Discord and a Telegram account makes them one identity for quizzes: every lookup resolves both user IDs, so the two most recent attempts of each quiz are shared across the platforms (the merge is re-pruned to two at the moment the handshake completes), as is the privacy configuration.

### Privacy controls

`/privacy` builds a numbered menu from what the caller actually has stored, so the numbering shifts with their data:

1. delete all my quiz results — shown when any result is stored;
2. delete my results for one quiz — shown when more than one quiz was taken; the bot then sends a numbered list of those quizzes;
3. forbid other users from comparing my quiz results (a toggle; the current state is shown) — shown when any result is stored. While set, `/quizzes-compare` refuses to compare anyone against this user;
4. forbid comparison for one quiz only — shown when more than one quiz was taken; the bot sends the numbered quiz list, each with its current state.

A user with nothing stored is simply told there is nothing to configure. Both bans are honoured across a user's linked accounts, and `/quizzes-clear` removes a single stored attempt.

### Party membership

`/party-join <code|exact name>` (verified users) sends a join request to the party's **Discord** leaders — the founder is always one of them — as a DM carrying **Accept**/**Reject** buttons; any single leader's answer resolves it, and the requester is notified of the outcome. A user may belong to only one party per union, and the request is refused for a suspended party. The buttons are persistent: they keep working after a bot restart. `/party-leave` removes the caller from their party of the current union (the founder is refused and pointed to transfer/suspension instead). `/party-kick <id|username>` lets a founder or leader remove a plain member — not another leader or the founder — matching the target by id, mention or stored nickname on the leader's own platform.

Everywhere a member is shown, a Discord member seen from Telegram appears as their **nickname**, never a Discord ping (which would not resolve there).

### Government bodies

`/add-govt` (Bot Admins) registers a body per union with a unique name and the localizable titles of its members (singular/plural). `/govt` looks a body up like `/party` does — but suggestions may include bodies of other unions (marked as such), and when nothing is similar the bot simply reports “not found”. The card lists the members separated by an interpunct (`·`), each followed by `| <party name>` when the member belongs to a union party.

### Wiki anniversary scheduling

A background loop wakes once a minute and posts every registration whose anniversary is today, once the posting minute has passed. The year of the last post is stored, so an announcement fires exactly once per year — and if the bot was offline at the appointed minute it still catches up later the same day. A wiki founded on 29 February is congratulated on 28 February in common years. The bot reuses (or creates) its own webhook named after the chat's language in the target channel; without the **Manage Webhooks** permission it falls back to an ordinary message and says so when the anniversary is registered. Leaving a server forgets its anniversaries.

### Channel keyword scans

`/find-with` and `/find-without` (Discord, Server Admins, and verified) walk through a channel's history over a period — `MM-DD-YYYY` for one day or `MM-DD-YYYY + 7D` for a range — and reply to every matching message with a mention of its author. *All* messages are considered: keyword matching (case-insensitive, semicolon-separated keywords) covers the message text **and** every embed (title, description, fields, footer, author). Replies are throttled to about one per second; a summary with the checked/matched counts arrives when the scan finishes.

### Localization

All bot-facing strings live in per-language JSON files under `src/i18n/`. Each entry carries a translation **status**: `verified` (🟩), `unverified` (🟧) or `untranslated` (🟥, a key missing relative to the reference `DEFAULT_LANG`).

- `/locale` shows each language with an emoji bar and the percentage of verified strings; `/locale <code>` sends that language's JSON file (so the reply codes are visible for use with the other commands).
- `/loc-compare <code>` compares one reply across all languages with status emoji.
- `/loc-suggest <lang> <code> <text>` forwards a translation suggestion to `SUPPORT_CHATS`, tagged with a unique dialog code.
- `/loc-reply <code> <text>` (Bot Admins) DMs the original suggester **and** posts the reply into the support chats, so the team can see how a suggestion was resolved; the dialog code is then removed. Suggestion codes are kept at most **1 year**.

### Service events and automatic backups

Start/stop notices and administrative results — a chat linked with `/setup`, a union created with `/add-unia`, parties enabled/disabled with `/allow-parties`, a party founded with `/add-party`, a party deleted with `/edit-party-admin` — go to the `SERVICE_CHATS` channels, localized per channel. Encrypted database backups (authenticated BLAKE2 keystream, standard library only) are posted to `BACKUP_CHATS` every **12 hours**; `/backup` returns one on demand, and `python src/restore_backup.py <input.db.enc> <output.db>` decrypts it with the `BACKUP_KEY` environment variable.

---

## Data collection and retention

The bot stores operational data in local SQLite (`dem.db`). The full privacy policy lives in [PRIVACY.md](PRIVACY.md).

### What data is stored

- **Union registry**
  - Union codes, their localized names, creation timestamps.
  - Per-union party settings: enabled flag and the Discord role IDs allowed to use `/add-party`.
- **Chat bindings**
  - Platform, server/group ID, bound union, the ID of the Bot Admin who ran `/setup`, timestamp.
- **Language settings**
  - Per-server/group and per-channel/topic language choices.
- **Parties**
  - Name, code, logo image, color, description, ideologies, article link, suspension flag, invite rule.
  - Founder, leaders and members: platform, user ID and display name at the time of recording; party alliances (schema for upcoming commands).
- **Government bodies**
  - Body name and member titles per union; members' platform, user ID and display name.
- **Verification and account links**
  - Fandom verifications: Discord user ID, Fandom username/id, the matched Discord handle, timestamp.
  - Discord↔Telegram account links, and short-lived pending link handshakes.
- **Join requests**
  - Open `/party-join` requests: party, requester platform/ID/name, language.
- **Localization suggestions**
  - `/loc-suggest` dialog codes: submitter platform/ID/username, target language, reply code, suggested text.
- **Quizzes** (only with the taker's consent)
  - Quiz results: platform, user ID, quiz id, language, the computed breakdown and the raw answers, timestamp — at most the two most recent attempts per quiz.
  - Quiz progress parked by `/quizzes-stop`: platform, user ID, quiz id, language, question index, option orders, answers so far.
  - Quiz privacy settings from `/privacy`: platform, user ID, the quiz the ban applies to (or all), the ban flag.
- **Wiki anniversaries**
  - Server and channel ID, wiki link and name, founding timestamp, posting time, wiki language, the ID of the admin who registered it, and the year it was last announced.

Channel scans (`/find-with`, `/find-without`) read message history transiently to match keywords; nothing from the scanned messages is stored.

### Retention periods

- **Union registry and chat bindings**: kept until changed/removed by an admin, or until the bot leaves the server/group (`/force_leave` also cleans them up).
- **Language settings**: kept until changed, or removed together with the chat binding.
- **Parties and government bodies**: kept until changed by their leaders/admins; a suspended party keeps its data until resumed or removed. A member's record is removed on `/party-leave` or `/party-kick`.
- **Verifications and account links**: kept until superseded (re-verifying or re-linking) or removed by the operator.
- **Pending link handshakes**: at most **30 minutes**. **Join requests**: cleared once answered (stale ones pruned after 30 days).
- **Localization-suggestion codes**: up to **1 year**, and removed immediately once answered with `/loc-reply`.
- **Quiz results**: only the **two most recent** attempts per quiz are kept, and never for longer than **10 years**; the user can remove them at any time with `/quizzes-clear` or `/privacy`. **Quiz progress**: at most **7 days**. **Quiz privacy settings**: until the user changes them.
- **Wiki anniversaries**: until removed with `/wiki-founday-remove`, or until the bot leaves the server.

### Data usage boundaries

- The bot uses stored data only to operate the union registry, parties, government bodies, verification, permissions, localization and backups.
- Verification calls out to Fandom's public API (`community.fandom.com`, `services.fandom.com`) to read the Discord handle on the given profile; only the Fandom username you supply is sent.
- It does not implement analytics/tracking pipelines in this repository.
- Data is local to the bot runtime environment unless your deployment adds external backup/logging.
