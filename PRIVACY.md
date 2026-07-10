# Democrate Privacy Policy

_Last updated: 2026-07-10_

Democrate is a self-hosted, open-source coordination bot for unions of wiki communities that runs on Discord and Telegram. This document describes what data the software processes, why, for how long, and what choices users have.

> **Who is responsible for your data.** Democrate is software, not a service: anyone can run their own instance. The person or team operating a given instance (the **operator**) controls that instance's database, configuration and backups, and is the data controller for it. This document describes what the software itself does; a specific operator may add their own infrastructure (hosting, logging, backups) around it.

## What the bot processes

Democrate keeps a registry of unions (confederations of wiki communities) and of the Discord servers and Telegram groups linked to them, hosts the unions' parties and government bodies, runs ideology quizzes, and answers commands in the language configured for each chat. It reads messages to detect its own commands and, during the interactive dialogs (`/add-party`, `/edit-party`, `/edit-party-admin`, `/privacy`, and the quizzes), to receive the answers of the user who started the dialog; ordinary conversation is neither stored nor forwarded anywhere.

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
| Language settings | Server/group and channel/topic IDs with the chosen language code | Until changed, or removed together with the chat binding |
| Parties | Name, code, logo image, embed color, description, ideologies, article link, suspension flag, invite rule; founder's, leaders' and members' platform, user ID and display name (as recorded when they took the role/joined); alliances (for upcoming commands) | Until changed by the party's leaders or removed by the operator; a member's record is removed on `/party-leave` or `/party-kick` |
| Government bodies | Body name and member titles per union; members' platform, user ID and display name | Until changed/removed |
| Verification | Discord user ID, the verified Fandom username and numeric id, the matched Discord handle, timestamp | Until you re-verify or the operator removes it |
| Account links | The paired Discord and Telegram user IDs; short-lived pending handshakes hold the acting username and the claimed other username | Link: until re-linked/removed. Pending handshake: **≤ 30 minutes** |
| Join requests | The party, and the requester's platform, ID, display name and language | Until a leader answers; stale ones pruned after 30 days |
| Localization suggestions | Suggester's platform, ID and username, target language, reply code, suggested text | Until answered with `/loc-reply`, at most **1 year** |
| Quiz results (**only with your consent**) | Your platform and user ID, the quiz, the language you took it in, the computed breakdown, your raw answers and the timestamp | Only the **two most recent** attempts per quiz are kept, and never longer than **10 years**; until you delete them with `/quizzes-clear` or `/privacy` |
| Quiz progress (`/quizzes-stop`) | Your platform and user ID, the quiz, the question you stopped at, the shuffled option order and the answers given so far | **≤ 7 days**, then discarded; cleared as soon as you finish the quiz |
| Quiz privacy settings (`/privacy`) | Your platform and user ID, whether comparison is forbidden, and for which quiz (or for all of them) | Until you change them |
| Wiki anniversaries (`/wiki-founday`) | The server and channel, the wiki's link, name, founding timestamp, posting time and language, and the user ID of the admin who registered it | Until removed with `/wiki-founday-remove`, or until the bot leaves the server |
| Message content | **Not stored**, with three exceptions: localization suggestions submitted via `/loc-suggest`, the party texts (name, description, ideologies) submitted voluntarily through the party dialogs, and the answers you give inside a quiz | — |

## Where data goes

- **Discord and Telegram.** Replies to commands are posted through the official APIs of both platforms and are subject to their own privacy policies.
- **Fandom.** `/verify` queries Fandom's public API (`community.fandom.com`, `services.fandom.com`) to read the Discord handle on the profile of the Fandom username you supply. Only that username is sent; the bot reads publicly visible profile data and stores no Fandom credentials.
- **Cross-platform notifications.** A `/party-join` request is delivered to the party's Discord leaders as a direct message; the outcome is sent back to the requester by direct message on their platform.
- **Linked accounts.** Once you link a Discord and a Telegram account, the bot treats them as one person for quizzes: your two most recent results of each quiz, and your `/privacy` settings, apply on both platforms. Nothing is sent anywhere new — the same local database is simply read through both of your user IDs.
- **Localization suggestions.** A `/loc-suggest` submission (with the suggester's username and ID) is posted to the operator-configured `SUPPORT_CHATS`, and the `/loc-reply` answer is sent to the suggester by DM and posted to the same support chats.
- **Backups.** Every 12 hours (and on `/backup`) the database is sent to the operator-configured backup channels — **always encrypted** (authenticated BLAKE2 keystream; the key never leaves the `BACKUP_KEY` environment variable on the operator's machine). The destination channels only ever store ciphertext.
- **Service notices.** Start/stop events and administrative results (`/setup`, `/add-unia`, `/allow-parties`, `/add-party`, party deletion via `/edit-party-admin`) go to operator-configured service channels. They contain union/party names and the acting admin's or founder's username.

The software contains **no analytics, tracking, advertising or data-sale pipelines**, and sends nothing to the developers of Democrate.

## What other users can see

- The union a server/group is bound to is visible to anyone who can read the `/setup` confirmation in that chat; Bot Admins can list all bound chats with `/list_chats`.
- `/party` shows anyone in the union's chats a party's card: its texts and logo, the founder's and leaders' names/mentions, member and government-seat counts. `/govt` shows a body's members with their party affiliations. Founding, leading or joining a party, and holding a government seat, are therefore public within the union. Discord members are shown to Telegram viewers by their nickname, not a ping.
- `/find-with` and `/find-without` reply-mention message authors publicly in the scanned channel; the commands are restricted to verified server admins.
- A localization suggestion is shown, together with the suggester's username and ID, to the team members reading the operator's support chats.
- If you let the bot store a quiz result, another user can put their own result next to yours with `/quizzes-compare`, which shows your per-axis breakdown. You can switch this off — for every quiz or for one of them — in `/privacy`, and the bot then refuses the comparison. Quiz results are never posted anywhere on their own; they appear only when you or someone running a comparison asks for them.

## Your choices

- **Commands are opt-in.** The bot stores nothing about individual users unless they verify a Fandom account, link a Telegram account, submit a localization suggestion, found/co-lead/join a party, consent to storing a quiz result, or are appointed to a government body by the operator's admins.
- **Verification is your choice.** `/verify` only reads a Fandom profile you name and stores the resulting link; you can ask the operator to delete it. Account linking requires both sides to opt in within 30 minutes.
- **Membership is consent-based.** Joining a party takes a leader's approval; becoming a leader or taking over a party takes the target's «Принимаю»; a member can leave at any time with `/party-leave`.
- **Quiz results are yours.** You see them whether or not they are stored — the bot asks for consent before saving, keeps at most your two most recent attempts per quiz and never for more than ten years, and `/privacy` lets you delete them (all, or one quiz's) and forbid other users from comparing themselves against you. `/quizzes-clear` removes a single attempt. A quiz you abandon leaves nothing behind after 7 days.
- **Suggestion removal.** A suggestion is deleted as soon as it is answered with `/loc-reply`, and in any case after a year; for earlier removal, ask the operator of your instance.
- **Questions and erasure requests** (e.g. removal of your name from a former party's records) should go to the operator of your instance — they hold the database. For questions about the software itself, open an issue in the repository.

## Security

The database is a local file readable only by the operator's environment; backups leave the machine only encrypted; bot tokens and the backup key are read from environment variables / a local `.env` file and are never written to the database or sent anywhere.

## Age requirements

Democrate runs on top of Discord and Telegram and inherits their minimum-age requirements; it performs no age verification of its own and is not directed at children.

## Changes

This document is versioned together with the source code. Material changes to what the software collects or shares will be reflected here and in the release notes.
