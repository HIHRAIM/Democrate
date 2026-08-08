# Democrate Privacy Policy

_Last updated: 2026-07-18_

Democrate is a self-hosted, open-source coordination bot for unions of wiki communities that runs on Discord and Telegram. This document describes what data the software processes, why, for how long, and what choices users have.

> **Who is responsible for your data.** Democrate is software, not a service: anyone can run their own instance. The person or team operating a given instance (the **operator**) controls that instance's database, configuration and backups, and is the data controller for it. This document describes what the software itself does; a specific operator may add their own infrastructure (hosting, logging, backups) around it.

## What the bot processes

Democrate keeps a registry of unions (confederations of wiki communities) and of the Discord servers and Telegram groups linked to them, hosts the unions' parties and governing bodies, runs a cross-community economy (including enterprises, timed production of goods and per-server statistics), runs ideology quizzes, and answers commands in the language configured for each chat. It reads messages to detect its own commands and, during the interactive dialogs (`/add-party`, `/edit-party`, `/edit-party-admin`, `/create-bank`, `/add-enterprise`, `/edit-enterprise`, `/privacy`, and the quizzes), to receive the answers of the user who started the dialog; ordinary conversation is neither stored nor forwarded anywhere.

The **economy** rewards chat activity in channels an admin explicitly marked with `/set-earn`. In such a channel the bot looks at each ordinary message only to measure its **length** (which determines the reward) and apply anti-abuse limits; the message's text is not stored, and messages in unmarked channels are not processed at all. Every change to an account balance — message rewards, transfers, sales, fines, conversions, wages, dues, and the daily consumption that buys goods from enterprises — is recorded in an append-only transaction journal so that balances stay auditable. The daily consumption figures themselves are per-server counts (how many units a category needed and how many it got) and name nobody.

Two moderation commands, `/find-with` and `/find-without` (Discord, server admins), read a channel's message history — including embeds — over an admin-chosen period to match keywords and reply to matching messages with a mention of their author. The scan is transient: nothing from the scanned messages is stored in the database.

To use the Fandom-activity commands, users **verify** their identity. On Discord, `/verify <fandom_username>` reads the Discord handle published on that Fandom profile (via Fandom's public API) and checks it against the caller's Discord username. On Telegram, users instead link their account to a Fandom-verified Discord account with `/add-discord` + `/add-telegram`. Verification is global to the account.

The **quizzes** (`/quizzes`) score a user's answers into a breakdown of ideologies. The result is shown either way; it is only **stored if the user agrees** when the bot asks, right before showing it. Stopping a quiz with `/quizzes-stop` parks the unfinished answers for 7 days so the run can be resumed. Everything a quiz stores about a user can be inspected and removed by that user with `/privacy` and `/quizzes-clear`, and no stored result outlives ten years.

**Wiki anniversaries** (`/wiki-founday`, Discord server admins) hold no personal data beyond the user ID of the admin who registered the entry. The announcements the bot posts each year are about a wiki, not about people.

> Message relay between the chats of a union and collecting poll votes through the bot's DMs are **planned** features. They are not part of the current software; if a future release adds them, this document will be updated first.

## What the bot stores

All data lives in a local SQLite database (`dem.db`) on the operator's machine.

| Data | Contents | Retention |
|---|---|---|
| Union registry | Union code, localized names, creation timestamp | Until removed by the operator |
| Party settings per union | Enabled flag, Discord role IDs allowed to found parties | Until changed by a Bot Admin |
| Chat bindings | Platform, server/group ID, bound union code, the user ID of the Bot Admin who ran `/setup`, timestamp | Until re-bound/removed, or until the bot leaves the server/group |
| Setup deadline | For servers and groups added after the rule came into force: the platform, the server/group ID, when the bot joined and — once the chat has been bound with `/setup` — when that was noticed. No user is named | Until the bot leaves the server/group |
| Language settings | Server/group and channel/topic IDs with the chosen language code, when it was last set, and whether the chat is a private conversation with the bot | A server's or channel's choice: until changed, or removed together with the chat binding. A **private chat's** choice: at most **1 year** after you last set it |
| Parties | Name, code, logo image, embed color, description, ideologies, article link, suspension flag, invite rule; founder's, leaders' and members' platform, user ID and display name (as recorded when they took the role/joined); alliances (for upcoming commands) | Until changed by the party's leaders or removed by the operator; a member's record is removed on `/party-leave` or `/party-kick` |
| Governing bodies | Body name and member titles per union; members' platform, user ID and display name | Until changed/removed |
| Verification | Discord user ID, the verified Fandom username and numeric id, the matched Discord handle, timestamp | Until you re-verify or the operator removes it |
| Account links | The paired Discord and Telegram user IDs; short-lived pending handshakes hold the acting username and the claimed other username | Link: until re-linked/removed. Pending handshake: **≤ 30 minutes** |
| Join requests | The party, and the requester's platform, ID, display name and language | Until a leader answers; stale ones pruned after 30 days |
| Localization suggestions | Suggester's platform, ID and username, target language, reply code, suggested text | Until answered with `/loc-reply`, at most **1 year** |
| Delegated roles | Server Admins (`/setadmin`) and Localizers (`/localizer-add`): platform, server ID (admins), user ID, username at delegation time, the delegating admin's ID, timestamp | Until revoked (`/remadmin`, `/localizer-rem`) |
| Quiz results (**only with your consent**) | Your platform and user ID, the quiz, the language you took it in, the computed breakdown, your raw answers and the timestamp | Only the **two most recent** attempts per quiz are kept, and never longer than **10 years**; until you delete them with `/quizzes-clear` or `/privacy` |
| Quiz progress (`/quizzes-stop`) | Your platform and user ID, the quiz, the question you stopped at, the shuffled option order and the answers given so far | **≤ 7 days**, then discarded; cleared as soon as you finish the quiz |
| Quiz privacy settings (`/privacy`) | Your platform and user ID, whether comparison is forbidden, and for which quiz (or for all of them) | Until you change them |
| Wiki anniversaries (`/wiki-founday`) | The server and channel, the wiki's link, name, founding timestamp, posting time and language, and the user ID of the admin who registered it | Until removed with `/wiki-founday-remove`, or until the bot leaves the server |
| Banks | Currency code, name and emoji, the union and central server, the published currency value and the running activity counters behind it, creation timestamp; leaders' platform, user ID and display name | Until removed by the operator |
| Accounts and balances | Per (user or party, bank): the integer balance (possibly negative — a debt), the energy stock, and a display name for cross-platform rendering | Until removed by the operator; a party's account is removed with the party |
| Transaction journal | For every balance change: the paying and receiving side (type, platform, user/party/bank ID), amount, currency, a short machine reason (e.g. `pay`, `fine: …`, `wage`), timestamp. Free-text fine reasons entered by a leader are stored in this field | Append-only; kept until pruned by the operator |
| Goods and inventories | Goods: code, the denominating bank, the owner (a user's platform + ID, or an enterprise), name, base value, energy cost, emoji, optional base category. Per user or enterprise: quantities held and total units ever produced per good (mastery) | Until removed by the operator |
| Enterprises | Name, code, logo image, description, home server/group; founder's, leaders' and workers' platform, user ID and display name; positions with their salaries; per-worker personal salary overrides; the salary currency, payout period and the running sales counter | Until deleted through `/edit-enterprise` or by the operator; a worker's record (and their personal salary) is removed on `/ent-leave` or `/ent-kick` |
| Production runs | Per pending `/craft` run: the good and batch size, the worker's platform, user ID and display name, the receiving inventory (user or enterprise), the server and the channel where the run started (for the completion notice), the reply language and the finish time | Minutes — removed as soon as the run completes |
| Production statistics | Per completed production event: server/group, good, category, quantity, value, quality level, and the producer's platform and user ID (for the provision population count) | ≈ 10 weeks, pruned automatically |
| Economic channels (`/setlogs`, `/settasks`) | The server/group, the chosen channel/topic, the posting schedule (weekday, time, timezone offset) and the last-posted period marker | Until unbound (`off`), or until the bot leaves the server/group |
| Monthly server tasks | The server/group, the month, the generated production items and the completion verdict | ≈ 13 months, pruned automatically |
| Recurring exports | The two enterprises, the good, quantity, price and currency, and the user ID of the leader who set the contract up | Until cancelled (`off`), or removed with either enterprise |
| Shipments (goods in transit) | The two enterprises, the good and quantity, the escrowed payment and its currency, the dispatch and arrival times, and the platform/channel where the export was launched (for the arrival notice) and its language | Removed the moment the shipment arrives (minutes to hours); a safety sweep drops anything still unresolved after 7 days |
| Professions | A bank leader's explicit profession override per (user, bank); without one the profession is derived from mastery and nothing extra is stored | Until changed or cleared |
| Earning channels | Channel/topic ID, the bank it earns for, the rate multiplier | Until switched off with `/set-earn off` |
| Treaties and rates | The two banks of each conversion treaty and, when set, the pegged rate | Until removed by the operator |
| Wages and party dues | Per bank: wage per profession-good. Per party: monthly dues per currency | Until cleared by the leader (amount 0) |
| Autocraft / autosend | Your standing production orders: the good, on/off, the worker driving the line, the receiving inventory (you or an enterprise), the server the output is attributed to, and for autosend the percentage and the receiving user, party or enterprise | Until switched off (percent 0 / off) |
| Olympiad contests | The event's start and end dates; per contest its language category, names, and the ids of the two Discord chats its votes travel through; per candidate wiki its name, link and language code | Deleted when the voting period ends, or on `/setolympiad off` |
| Olympiad votes | Your platform, user ID and username, the contest, the wikis you chose, the language you voted in, what you told the bot about your wiki account and your activity, the text of your vote, and the vote's key | Deleted as soon as a reviewer answers with `/accepted` or `/denied`, and in any case when the voting period ends — **including votes nobody reviewed** |
| Remembered wiki accounts | Your platform and user ID, the wiki username a reviewer confirmed with `/accepted … remember`, and who confirmed it | At most **1 year** |
| Message content | **Not stored**, with four exceptions: localization suggestions submitted via `/loc-suggest`, the party texts (name, description, ideologies) submitted voluntarily through the party dialogs, the answers you give inside a quiz, and the vote (with its account and activity claims) you write in the `/olympiad` dialog. In earning channels only a message's **length** is used, transiently | — |

## Where data goes

- **Discord and Telegram.** Replies to commands are posted through the official APIs of both platforms and are subject to their own privacy policies.
- **Fandom.** `/verify` queries Fandom's public API (`community.fandom.com`, `services.fandom.com`) to read the Discord handle on the profile of the Fandom username you supply. Only that username is sent; the bot reads publicly visible profile data and stores no Fandom credentials.
- **Cross-platform notifications.** A `/party-join` request is delivered to the party's Discord leaders as a direct message; the outcome is sent back to the requester by direct message on their platform.
- **Linked accounts.** Once you link a Discord and a Telegram account, the bot treats them as one person for quizzes: your two most recent results of each quiz, and your `/privacy` settings, apply on both platforms. Nothing is sent anywhere new — the same local database is simply read through both of your user IDs.
- **Localization suggestions.** A `/loc-suggest` submission (with the suggester's username and ID) is posted to the operator-configured `SUPPORT_CHATS`, and the `/loc-reply` answer is sent to the suggester by DM and posted to the same support chats.
- **Olympiad votes.** A vote cast with `/olympiad` is posted to the Discord chat that contest was configured to be reviewed in, carrying the wikis you support, what you told the bot about your wiki account and your activity, your vote text, and your username, messenger id and the vote's key. If a reviewer accepts it, the vote text is published again in that contest's accepted-votes chat under your messenger name and the wiki username the reviewer confirmed. Either decision is sent back to you as a direct message on the platform you voted from. Votes cast on Telegram travel to the same Discord chats.
- **Backups.** Every 12 hours (and on `/backup`) the database is sent to the operator-configured backup channels — **always encrypted** (authenticated BLAKE2 keystream; the key never leaves the `BACKUP_KEY` environment variable on the operator's machine). The destination channels only ever store ciphertext.
- **Service notices.** Start/stop events, administrative results (`/setup`, `/add-unia`, `/allow-parties`, `/add-party`, party deletion via `/edit-party-admin`, bank creation via `/create-bank`), and the chats the bot is added to or leaves because nobody bound them to a union within seven days, go to operator-configured service channels. They contain union/party/bank names, chat names and IDs, and the acting admin's or founder's username.
- **The economy stays local.** Balances, the transaction journal, inventories, professions, fines, treaties, wages and salaries live only in the local database. They are shown to other users only through the command surfaces described below, and are never posted anywhere on their own.
- **Scheduled channel posts.** Into the channels a server admin chose with `/setlogs` and `/settasks`, the bot posts the daily currency values, the weekly server statistics (provision, production totals, GDP — aggregates, no individual balances) and the monthly production task. A finished production run is announced in the channel where it was started, naming the worker and the batch. An export shipment's arrival is announced in the channel where the export was launched, naming the two enterprises and the batch.
- **Control panel (optional).** Operators may run [Confederate Panel](https://github.com/HIHRAIM/Confederate-Panel), a self-hosted web app that reads and edits this database directly. It uses the admin/localizer delegation records above to authenticate its users (a one-time code is sent to them as a DM/private message), and posts a notice to an operator-configured log channel/topic when someone edits the bot's localization through it. Nothing is sent to third parties.

The software contains **no analytics, tracking, advertising or data-sale pipelines**, and sends nothing to the developers of Democrate.

## What other users can see

- The union a server/group is bound to is visible to anyone who can read the `/setup` confirmation in that chat; Bot Admins can list all bound chats with `/list_chats`.
- `/party` shows anyone in the union's chats a party's card: its texts and logo, the founder's and leaders' names/mentions, member and seat counts. `/govt` shows a body's members with their party affiliations. Founding, leading or joining a party, and holding a seat in a governing body, are therefore public within the union. Discord members are shown to Telegram viewers by their nickname, not a ping.
- `/find-with` and `/find-without` reply-mention message authors publicly in the scanned channel; the commands are restricted to verified server admins.
- `/bank` shows anyone a bank's card: its leaders' names/mentions, the total money supply, the number of accounts and the summed outstanding debt — never individual balances. `/rates` shows currency values and conversion rates. `/party` includes the **party's** (not any member's) bank balances.
- Your own `/balance` and `/inventory` are shown only to you (ephemerally on Discord). Other users see movements they are part of: the recipient of `/pay` sees the confirmation, and a `/fine` (with its amount and the leader's free-text reason) is announced in the chat where the leader issued it. Bank leaders do not get a command that lists members' balances.
- `/enterprise` shows anyone an enterprise's card: its texts and logo, the leaders' and workers' names with their positions, the **enterprise's** account balances, the positions with their salaries and the warehouse. Working at an enterprise is therefore public. A personal salary set with `/ent-salary` is announced in the chat where the leader set it; the card itself lists position salaries, not personal ones.
- The weekly statistics and monthly tasks posted to the `/setlogs`/`/settasks` channels are server-level aggregates; the only per-person message is the production-completion notice naming the worker who started the run.
- A localization suggestion is shown, together with the suggester's username and ID, to the team members reading the operator's support chats.
- An Olympiad vote is shown, with your username, messenger id and everything you wrote in the dialog, to whoever can read that contest's review chat; once accepted, your vote text is visible to everyone who can read its accepted-votes chat, under your messenger name and your wiki username. Voting is therefore not anonymous.
- If you let the bot store a quiz result, another user can put their own result next to yours with `/quizzes-compare`, which shows your per-axis breakdown. You can switch this off — for every quiz or for one of them — in `/privacy`, and the bot then refuses the comparison. Quiz results are never posted anywhere on their own; they appear only when you or someone running a comparison asks for them.

## Your choices

- **Commands are opt-in.** The bot stores nothing about individual users unless they verify a Fandom account, link a Telegram account, submit a localization suggestion, found/co-lead/join a party, consent to storing a quiz result, or are appointed to a governing body by the operator's admins.
- **Earning happens only in earning channels.** Message rewards exist only in channels an admin marked with `/set-earn`; anywhere else your messages are not looked at. The economy itself requires no verification, so in such a channel the length of any member's message is measured — an account is opened for you automatically the first time you write there. Everything beyond that (crafting, trading, joining an enterprise) is your own choice, and leadership of a bank requires your explicit «Accept» (except when a Bot Admin appoints directly).
- **Fines and dues are governance features.** They are issued by the bank/party leaders of communities you chose to join; disputes about them go to those leaders or the operator, and the transaction journal keeps them auditable.
- **Verification is your choice.** `/verify` only reads a Fandom profile you name and stores the resulting link; you can ask the operator to delete it. Account linking requires both sides to opt in within 30 minutes.
- **Membership is consent-based.** Joining a party takes a leader's approval; becoming a leader or taking over a party takes the target's «Принимаю»; a member can leave at any time with `/party-leave`. Enterprises work the same way: `/ent-join` needs a leader's approval, leadership offers and transfers need the target's «Accept», a worker leaves at any time with `/ent-leave`, and a priced export or a recurring export contract needs the accepting leader's consent.
- **Quiz results are yours.** You see them whether or not they are stored — the bot asks for consent before saving, keeps at most your two most recent attempts per quiz and never for more than ten years, and `/privacy` lets you delete them (all, or one quiz's) and forbid other users from comparing themselves against you. `/quizzes-clear` removes a single attempt. A quiz you abandon leaves nothing behind after 7 days.
- **Suggestion removal.** A suggestion is deleted as soon as it is answered with `/loc-reply`, and in any case after a year; for earlier removal, ask the operator of your instance.
- **Voting is opt-in and not anonymous.** `/olympiad` only runs when you start it, every question in it carries a **Stop** button, and nothing is stored until you finish the dialog. What you write goes to the reviewers of that contest under your name, and to the accepted-votes chat if it is accepted — so treat it as a public statement. You may replace your vote while the period lasts, which sends the new one back for review. Everything the Olympiad stored about you is deleted when the voting period ends, apart from a wiki account a reviewer chose to remember, which expires after a year and can be removed earlier by the operator.
- **Questions and erasure requests** (e.g. removal of your name from a former party's records) should go to the operator of your instance — they hold the database. For questions about the software itself, open an issue in the repository.

## Security

The database is a local file readable only by the operator's environment; backups leave the machine only encrypted; bot tokens and the backup key are read from environment variables / a local `.env` file and are never written to the database or sent anywhere.

## Age requirements

Democrate runs on top of Discord and Telegram and inherits their minimum-age requirements; it performs no age verification of its own and is not directed at children.

## Changes

This document is versioned together with the source code. Material changes to what the software collects or shares will be reflected here and in the release notes.
