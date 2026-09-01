# Architecture

This is the map of the code: what the moving parts are called, how a union holds them together, and where to find the code behind each feature. The commands themselves are documented in [README.md](README.md); this file is about structure.

## The one idea

Democrate organizes wiki communities into *unions*. A **union** is a bare code plus a name in each of the six languages (`unions`, `union_names`); it owns nothing by itself and exists only so that the things below can point at it. A **chat** is one community the bot answers in — a Discord server keyed by its bare guild id, a Telegram group keyed by its bare chat id (`chats`) — and `/setup` is the single act that binds a chat to a union. Everything public the bot does asks the same question first: which union is this chat in? A server that answers nothing is a server the bot has nothing to say in, which is also why an unbound one is eventually left (see The setup deadline).

Inside a union live the political layer and the economic layer. The political layer is **parties** (founded through a dialog, joined by request, led by a founder and appointed leaders) and **governing bodies** (named seats filled by union members, whose party affiliation is counted back onto the party cards). The economic layer is **banks** — each one a currency, anchored to a central server and thereby to a union — plus the goods people and **enterprises** produce, the accounts they are paid into, the treaties and rates that make two currencies convertible, the shipments that move goods between servers, and the daily meal each server eats out of its enterprises' warehouses. On top of both sit the features that are attached to a chat rather than to a union: ideology **quizzes**, the wiki **Olympiad**, wiki anniversary greetings, Fandom verification and the localization workflow.

Codes are the user-facing keys throughout, and the important ones share one namespace — see The code namespace. Money is an integer count of minor units everywhere in the code and only becomes a decimal string at the moment it is printed; a percentage is stored the way a user typed it (`5` meaning five percent), never as `0.05`.

Both halves of the bot run in one Python process on one asyncio loop: a `discord.py` client and an `aiogram` dispatcher side by side, sharing one SQLite database (`src/dem.db`, WAL mode, guarded by a re-entrant lock in `db/__init__.py`) and one set of platform-neutral modules — `db`, `utils`, `economy`, `olympiad`, `quizzes`, `stats`, `message_relay`. What is *not* shared is the commands: every command exists twice, once per half, deliberately, because a slash command with typed parameters and ephemeral embeds and a Telegram command with positional arguments and HTML are different programs wearing one name. Unlike the sibling bots, no message ever crosses between the halves here — what crosses is state, so the two sides must agree about the database and about nothing else. The Telegram half reaches into the Discord half by a module-level import of the shared card and menu renderers (`telegram_bot/parties.py`), and everywhere else through call-site imports (`from telegram_bot import bot as tg_bot` inside a function) — that is the deliberate cycle-breaker. Keep the pattern.

## The code namespace

Union codes, party codes, currency codes, good codes and enterprise codes are checked against **one shared namespace** (`db/unions.py: code_taken`, and the per-kind `*_code_taken` helpers behind it): a party may not take a code a currency already uses, and a good may not take a code a party uses. That is not tidiness. A user types a bare code into `/pay`, `/craft`, `/sell`, `/party` and `/enterprise` alike, and the command decides what the code *is* by looking it up; two kinds sharing one code would make the same four characters mean two things in one union.

Union codes are 2–12 characters and chosen by the admin who runs `/add-unia`. Party, currency, good and enterprise codes are exactly 4 characters, uppercase, and either chosen or generated (`db/unions.py: generate_code`, which retries against the shared namespace). A code is a primary key, so renaming one is not an `UPDATE` of a field but a rewrite of every reference to it: `rename_bank_code`, `rename_party_code` and `rename_enterprise_code` each walk their own list of referring tables. Adding a table that stores one of these codes means adding it to that list.

The five **base goods** (`FOOD`, `HYGN`, `WARD`, `PHRM`, `HOUS`) are permanent occupants of the good side of the namespace. They are seeded once by `db/schema_economy.py: _seed_base_goods`, called from `init_economy()` and from nowhere else; the function is idempotent by design — it inserts what is missing and refreshes the energy cost of what is there — but it is still the one place in the schema layer that writes rows rather than DDL, so it must stay exactly one function called exactly once.

## Roles

* **Bot Admin** — hard-coded in `config.py: ADMINS`, checked by `utils.is_admin`. Operates the bot itself: creates unions, runs `/setup`, switches parties on, edits any party of any union, appoints bank leaders anywhere, opens and closes the Olympiad, forces the bot out of a chat, pulls a database backup.
* **Server Admin** — a member with the native Administrator / Manage Server permission on Discord or the creator/administrator status on Telegram, plus users delegated with `/setadmin` (`server_admins`). Checked by `discord_bot/client.py: is_server_admin` and `telegram_bot/client.py: is_server_admin` — two functions because the native half of the answer is asked differently on each platform. Sets the chat's language, the economic log and task channels, the wiki anniversaries, and may make a channel earn a currency.
* **Localizer** — `/localizer-add`, stored in `localizers`. May edit this bot's localization through the control panel; a delegated Server Admin holds the status implicitly while they remain one.
* **Party Founder** — the user who ran `/add-party`, stored on the party row. May do everything a party leader may, plus appoint leaders, transfer the founding, suspend and dissolve the party.
* **Party Leader** — appointed by the founder with consent buttons (`party_leaders`). Edits the party card and rules, approves and rejects join requests, kicks members, sets the party's monthly dues.
* **Bank Leader** — appointed by another bank leader or by a Bot Admin (`bank_leaders`); need not be an admin of anything. Runs the bank through `/edit-bank`: currency name, emoji, code, central server, co-leaders; mints and fines, sets professions and wages, signs treaties and pegs rates.
* **Enterprise Founder and Enterprise Leader** — the same shape as a party, one table down (`enterprises.founder_*`, `enterprise_leaders`). Admit and dismiss workers, define positions and salaries, sell and export the warehouse.
* **Verified user** — anyone whose Fandom account the bot has checked (`fandom_verifications`), or whose Telegram account is linked to such a Discord account (`account_links`). Not a rank: it is the gate on the Fandom-activity commands only, and the economy deliberately does not ask for it.

## Verification and account links

Two different things wear the word "verification" here, and only the first involves Fandom. `/verify <fandom_name>` fetches that profile through Fandom's public user API (`fandom.py`), reads the Discord handle the profile publishes, and compares it with the caller's (`fandom.py: handle_matches`, which normalizes the old `name#1234` and the new handle forms). It is a single request at one moment, not a subscription — nothing re-checks the profile afterwards.

The second is the **account link**: `/add-telegram <tg_nick>` on Discord and `/add-discord <discord_nick>` on Telegram, each naming the other side, both within thirty minutes (`pending_links`, swept by `cleanup_old_pending_links`). `db/users.py: register_link_attempt` writes the caller's half and completes the link when the opposite half already names them back. The link is what makes a Telegram user count as verified at all — there is no Fandom check on Telegram.

A linked pair is **one person** in the two places where it matters. `db/users.py: user_identities` returns both `(platform, user_id)` pairs, and the quiz history is read through it, so a linked user sees one history from either side. `db/users.py: canonical_user` collapses the pair onto the Discord account, and every account, balance and inventory is keyed by its result — which is why linking after both accounts have earned money merges nothing: the Telegram wallet simply stops being addressed. That is a known sharp edge, not a bug to route around silently.

## Parties

`/add-party` is a dialog, not an argument list: name, then a 4-character code, then an optional logo, each answer waited for up to thirty minutes (see Interactive dialogs). The union must have parties switched on (`/allow-parties`, which also names the Discord role ids whose holders may found one) — every party command routes through `_resolve_party_union` / `_resolve_party_union_tg` for exactly that check, `/edit-party-admin` excepted so that a Bot Admin can still tidy a union up after parties were switched off.

Founding works on both platforms, but the two gates it passes are not symmetrical. What a founder must have is a Fandom account the bot knows about, and `db/olympiad.py: known_fandom_name` answers that from either source — a `/verify` on a Discord account (the caller's own, or the one their Telegram is linked to) or an Olympiad reviewer's `/accepted … remember`, which is the only route for somebody with no Discord account at all. The **role** requirement is a set of Discord role ids, so it can only be asked of somebody who has a Discord account to ask it of: `telegram_bot/commands/parties.py: _has_party_role` checks it across the union's Discord servers for a linked founder, and a Telegram-only founder passes on the Fandom account alone.

Membership is a request: `/party-join` writes a `join_requests` row and delivers Accept/Decline buttons to the leaders. On Discord those buttons are a persistent view re-armed on every start (`discord_bot/client.py: setup_hook` walks `get_open_join_requests` and re-adds a `JoinRequestView` for each), so a request survives a restart; on Telegram the same decision arrives as an inline keyboard whose token lives in `_pending_consents`. Leadership offers and transfers are always consented to, never imposed — `_ConsentView` on Discord, the `pc:` callback keyboards on Telegram.

Leaving is free (`/party-leave`); being removed is a leader's decision (`/party-kick`); dissolving is the founder's, behind a confirmation button, because `db/parties.py: delete_party` drops in one transaction the party row, its leaders, members, alliances, open join requests, monthly dues, **its bank accounts** and any `/autosend` order pointing at it — the money goes with the party and nothing brings it back. **Suspension** is the reversible half of the same menu item: the row stays, the card carries a paused mark, and `/party-join` refuses. **Alliances** live in `party_allies` as unordered pairs, are read onto the card by `get_party_allies`, and are carried through renames and deletions — but no command writes one, so today they can only be entered by hand.

`/party` renders the card: description, rules, founder and leaders, member count, allied parties, the seats the party holds in the union's governing bodies (`db/parties.py: party_govt_seats`), and its bank balances — a party holds accounts exactly like a user, addressed through `db/banks.py: party_owner` instead of `canonical_user`. Both halves render that card from the same helpers in `discord_bot/parties.py` — the Telegram half imports them by name rather than keeping a second copy, because the *content* of a card is a union fact while only its formatting is a platform fact.

The tables behind all of this are `parties`, `party_leaders`, `party_members`, `party_allies`, `join_requests` and the per-union switch in `union_party_settings`; `db/parties.py` owns every one of them.

## Governing bodies

A governing body is a named set of seats in a union (`govt_bodies`, `govt_members`) with its own words for one member and for several, created by a Bot Admin with `/add-govt`. It is not a party and holds no money: `/govt` lists the members with the party each belongs to, and the same relation read the other way is what puts a seat count on a party card. Bodies have no leaders, no join requests and no dialog — everything about them is one command each way.

## Banks, money and energy

A **bank** is a currency. `/create-bank` runs a dialog and anchors the bank to a *central server*, which must itself be `/setup`-bound; the bank's union is that server's union, and a server or union may host any number of banks. The 4-character code is the currency code, the primary key, and the thing users type — hence `rename_bank_code` rather than a field update.

Money is stored as **integers in minor units** — hundredths — everywhere: `accounts.balance`, every amount in `transactions`, wages, dues, salaries and prices. `economy/money.py: parse_amount` turns what the user typed into that integer and `format_amount` turns it back, and those two functions are the only place the decimal point exists. Balances are **signed**: `/fine` may push an account below zero, a negative balance is the holder's debt to the bank, and incoming money pays the debt down first because addition is all it takes. Every change goes through `db/banks.py: mint`, `burn` or `transfer`, each of which writes an append-only row into `transactions` under the connection lock; nothing else may write `accounts.balance`.

**Energy** is the second resource and is not money: it is earned from messages alongside currency, it is held on the account row beside the balance, and it is spent by production. `spend_energy` charges the whole batch up front, before the run starts — a run that cannot pay does not begin, rather than stopping half way. An account is created by `/open-account` or silently by the first message that earns in a channel (`ensure_account`), which is why a user may discover an account they never opened; `/balance` is where they find it.

Tables: `banks` (the currency, its central server, its period counters and its published value), `bank_leaders`, `accounts` (one row per owner per bank, holding both balance and energy) and `transactions` (the append-only journal). All of them belong to `db/banks.py`.

## Earning from messages

A channel earns nothing until `/set-earn` names a bank on it (`earn_channels`, one row per channel/topic, with a rate multiplier). That is how several banks coexist on one server: each channel picks the currency it feeds. A binding is resolved **outwards** (`db/goods.py: resolve_earn_channel`), because a Discord thread or forum post and a Telegram forum topic are places of their own: the message's own key is tried first, then the parent channel and its category, then the group itself — the same outwards resolution `db/settings.py` does for the language, and the reason a `/set-earn` on a channel now covers everything inside it. `discord_bot/events.py: on_message` and `telegram_bot/catchall.py: _try_earn_tg` are the two entry points, and both do nothing but call `economy/activity.py: earn_from_message`.

Activity per counted message is `A = min(√chars, sqrt_cap)` — sublinear with a ceiling, so a wall of text is worth little more than a paragraph and one word is worth nothing. From one A the user is credited both resources: currency `A × rate × currency_per_activity` and energy `A × energy_per_activity`. The anti-abuse state — last-counted time and the rolling hourly total per user — lives in `economy/activity.py: _earn_state`, in memory and deliberately not in the database: it is a rate limiter, and losing it on a restart costs one extra message per user, while writing it would cost a transaction per message.

## Goods, mastery and production

Goods belong to people and enterprises, not to banks; a bank only *denominates* a good — it prices the good and it is the currency a sale is paid in, but **a bank never buys anything**. `/sell` and `/ent-sell` move goods one way and money the other between two holders (`economy/trade.py: sell`), minting nothing, and the buyer's consent is what settles them. The currency has exactly two sources: message earning and the daily meal (`economy/consumption.py`). A good may be tied to one of five base categories (food, hygiene, wardrobe, pharmacy, household), and then its quality feeds the server's provision.

**Mastery** is the number of units of that good a worker has ever produced (`mastery`), and three formulas hang off it, all in `economy/money.py`: quality `level = min(mastery_max_level, 1 + floor(log₂(1 + m/mastery_scale)))`, unit value `base_value × (1 + alpha·√m)` and craft cost `energy_cost × (1 + mastery_cost_weight·alpha·√m)`, both clamped at `mastery_cap`. The cost deliberately has the **same shape** as the value and a fraction of its size, so that value per unit of energy rises monotonically all the way up. It has not always: cost used to grow linearly in the *level*, which is itself the logarithm of the count, while value grew with the count's square root — the two shapes crossed badly and a crafter's return per unit of energy fell to 73 % of a beginner's around level 4, recovering only near level 9, three thousand units later. A curve that punishes practice for weeks is worse than no curve, and the knob to keep an eye on is the fraction: below 1.0 skill pays, at 1.0 it is neutral, above it the trap returns. The curve also **ends**: at the top level (10) all three freeze, so a good has a best possible quality rather than an endless creep, and `mastery_scale` stretches the ladder — every level still costs twice the one below — so that the top is about a month of continuous production. Mastery is personal even when the batch goes to an enterprise; the enterprise accrues its own mastery in parallel, and that is what prices its warehouse.

**Production takes time.** `/craft` writes a `productions` row with an arrival timestamp, charges the energy immediately, and returns; `main.py: production_loop` polls every twenty seconds for due rows, calls `economy/production.py: finish_production`, and announces the batch in the channel where the run started. Duration and batch size come from the quality level and the good's category (`produce_params`), clamped to at most five minutes. A worker may keep up to `parallel_production` (5) different goods in production at once but only one run of each, and the batch is divided by the number of lines running (`split_qty`) — five goods at once yield what one would, in five streams. A pending run survives a restart because it is a database row rather than a task.

A run may also come out **lucky** — `lucky_batch_chance` (5 %) of a double batch at no extra energy, rolled in `finish_production` so the surprise lands with the goods rather than with the promise, and only for a manual run: an automatic line is not a decision. A person's **first run ever** (`is_first_run_ever`) finishes at once instead of after minutes, and the energy for it came with their first account (`db/banks.py: _starter_energy`), so the path from meeting the bot to holding something one made is a single command.

`/autocraft` is the same line running continuously at `auto_efficiency` of the manual tempo divided between the owner's parallel lines (the same limit of five), advanced hourly by `economy/production.py: run_autoproduce` with fractional units carried over in `autocraft.carry`, and halted while the worker's energy is empty. Base goods draw that energy from whichever of the worker's accounts holds the most, market goods from their denominating bank (`economy/production.py: energy_bank_for`). `/autosend` moves a percentage of a good's stock to a user, party or enterprise on the daily tick.

Tables: `goods` (definition, denominating bank, category, base value, energy cost), `inventory` (who holds how many of what), `mastery` (units ever produced, per owner per good — the number every formula above reads), `productions` (a run in flight, with where to announce it), `autocraft` and `autosend` (the standing orders), `professions` and `earn_channels`. `db/goods.py` owns them all.

## Enterprises

An enterprise is a party with a warehouse and a payroll: same shared-namespace code, same founder-and-leaders shape, same consent buttons, same numbered edit menu. What differs is that it belongs to the *server* it was founded on rather than to the union, works toward that server's monthly task, holds the goods its workers produce for it, and pays them.

Salaries are read in `economy/payroll.py: resolve_salary`: a worker's personal override (`/ent-salary`) wins over the salary of their position (`/ent-position` + `/ent-assign`), and either kind is stored as one of two shapes — an exact amount in minor units, or a percent of the period's sales, stored as the number the user typed (`5` for five percent). Payroll runs weekly or monthly as the leader chose, pays out of the enterprise's account in its salary currency, stops the moment the account cannot cover the next payment — an enterprise never goes into payroll debt — and resets the sales counter afterwards.

Tables: `enterprises`, `enterprise_leaders`, `enterprise_members` (with the position each holds), `enterprise_positions` (a position and its salary) and `enterprise_salaries` (the personal overrides), all owned by `db/enterprises.py`. The warehouse is not a table of its own: an enterprise holds `inventory` and `mastery` rows under its own owner tuple, which is what lets one set of production code serve both a person and a company.

## Export logistics

An export is a shipment that spends time in transit, and it is the one economic action that always needs the other side's consent: a leader of the receiving enterprise accepts the offer with the consent buttons, and only then does the cargo leave. On dispatch the goods leave the seller's warehouse immediately and a priced sale escrows the buyer's payment in full; no funds means no dispatch.

Travel time is a function of **distance** between the two enterprises' servers — same server, different servers of one union, or different unions — plus an optional per-unit term (`economy/logistics.py: transport_distance`, `transport_time`). Food and pharmacy run a **risk** of spoiling rather than paying a toll (`perish_chance`, `perished_qty`): the longest trip the map allows carries the category's full chance, a shorter one proportionally less, and a batch that is unlucky loses at most a third of itself with at least one unit surviving. Wardrobe, hygiene and household never spoil. The odds are measured against the half hour a long delivery actually takes, not against an hour no delivery lasts. `main.py: shipment_loop` polls for arrivals every twenty seconds, hands the goods over and releases the escrow to the seller (counting it into period sales when it lands in the salary currency). If either enterprise is deleted while a shipment is on the way, `db/enterprises.py: _refund_shipment` returns the goods and the escrow to the surviving side. `/auto-export` is the same thing as a standing weekly contract.

## Consumption and provision

Every set-up server **eats once a day**. The daily tick counts the server's active population — the distinct people who produced anything on it in the last twenty-four hours — turns it into a per-category demand, and serves that demand out of the warehouses of the enterprises based on that server: market goods first, base goods as the filler behind them. Within each tier the demand is spread **evenly over every warehouse holding stock** (`_draw_evenly`), a pass at a time, rather than emptying whichever sorts first. Every unit is **bought**, not requisitioned — the enterprise is paid the unit value its quality commands, base goods being worth nothing and so feeding for free. Since no bank buys goods, this is the enterprises' main income and one of the currency's only two sources, which is what the even draw is protecting. A server with no producers that day is not fed at all, and personal inventories are never touched.

What happened is written to `server_consumption` keyed by the date, so a tick that somehow runs twice cannot count one meal into the week twice. **Provision** then reads those rows back rather than recomputing anything (`economy/consumption.py: provision_report`): satisfaction over need, per category, averaged by weight, capped at 1.0 for a category only base goods feed and higher when categorised custom goods reach it. It is a coverage measure, not an output measure — which is why stock in a warehouse counts and a category nobody supplies drags the average down however busy the chat is. **GDP** (`economy/consumption.py: server_gdp`) is the other half of the same reading and comes from the opposite table: the week's `server_production` rows valued at each currency's published value, so it measures what a server made while provision measures what its people got.

Tables: `server_production` (one row per batch produced on a server, the input to statistics, tasks and GDP) and `server_consumption` (one row per server per day per category: what was needed, what covered it, and at what quality), both in `db/serverstats.py`.

## Currency exchange

Two banks become convertible through a **treaty** (`/treaty`, accepted by a leader of the other bank with consent buttons; `treaties` stores the unordered pair). Each daily tick recomputes every currency's **value** from goods backing, money supply and message energy, clamped to a maximum daily move (`economy/trade.py: recompute_value`), and the rate A→B is simply `value(A) / value(B)`.

The **manual peg** is narrow on purpose: two banks may fix their own rate with `/set-rate` only while each is the other's *sole* treaty partner (`economy/trade.py: can_manual_peg`). The moment a bank signs a second treaty, only computed rates apply again — a fixed rate inside a web of three or more currencies would let the triangle disagree with itself. `/convert` exchanges between the caller's own accounts at the current rate minus the spread the **source** bank charges (`economy/trade.py: convert_fee`): the fee that bank's leaders set for this particular currency, else their default for all of them, else `ECONOMY["convert_fee"]`. The rows live in `bank_convert_fees` keyed by bank code, and both of its columns are in `_BANK_CODE_COLUMNS`, which is what carries a fee across a `/edit-bank` rename.

The fee is charged in the currency being sold and **paid to that bank's leaders**, evenly, the remainder to the first of them (`_pay_convert_fee`). A bank has no account of its own — `_bank_owner` is a journal counterparty, not a balance — so the leaders are the only place a bank's income can accumulate; with no leaders left the fee is burned, which is what it always was. The supply of the source currency therefore falls by the principal alone, and the fee is a transfer rather than a sink.

## Party economy, dues and wages

Two recurring transfers exist beside enterprise payroll, and both are monthly. `/party-dues <code> <amount>` sets a mandatory member contribution: on the monthly tick `economy/payroll.py: run_party_dues` moves the amount from each member's account to the party's, and a member who cannot pay is pushed into debt to the bank rather than skipped — the dues are an obligation, not a best effort. `/set-wage <profession> <amount>` sets a monthly wage in a bank, and `run_wages` pays every account holder the wage attached to their profession.

A **profession** is the good someone has produced most of (`db/goods.py: get_profession` computes it from `mastery`), which a bank leader may override with `/set-profession`. Wages are therefore the one emission in the system that is not backed by a sale, which is why the sinks — fines, dues, the `convert_fee` spread — exist at all: `wages` and `party_dues` are what a bank pays out, `transactions` is where all of it is written down, and `db/trade.py` owns both tables together with `treaties`.

## Monthly tasks and weekly statistics

A server's **monthly task** is generated from its own history rather than from a global table (`economy/tasks.py: generate_monthly_task`): every base category must beat last month's output by `task_growth`, floored at `task_min_qty` units, and that part is mandatory and must be produced on this server; up to `task_optional_goods` of the server's own top custom goods are added as optional extras. `evaluate_task` grades last month's row on the 1st and a completed task grants next month's provision bonus; `task_progress` is what the weekly statistics show in the meantime. The rows live in `server_tasks` (`db/serverstats.py`), one per server per month, with the verdict written back onto the same row.

Both scheduled posts are per server and per timezone. `/setlogs` names the economic log channel plus a weekday, a time and a UTC offset; `/settasks` names the task channel plus a time on the 1st. Both are rows in `chat_channels` (`db/serverstats.py`), and `main.py: _due_channel_posts` walks them once a minute, converts *now* into each row's own local time, and compares the resulting period marker with `last_marker`. `stats.py` renders what goes out — `weekly_stats` (provision by category, goods produced, GDP, the server's banks, task progress), `monthly_task_post` (the new task and the verdict on the old one) and `fx_lines` (each union currency's new value with its day-over-day change, broadcast right after the daily FX recompute).

## Interactive dialogs

Neither sibling bot has this: several of Democrate's commands are conversations. `/add-party`, `/create-bank`, `/add-enterprise`, the numbered `/edit-*` menus and the Olympiad's voting dialog all ask a question, then wait up to thirty minutes for the user's next ordinary message.

The two halves wait differently, and the difference is the reason the Telegram handler order matters. On Discord `discord_bot/dialogs.py: _wait_message` uses the client's own `wait_for` with a predicate, so a dialog needs no handler of its own. On Telegram there is no such facility: `telegram_bot/dialogs.py: _wait_user_message` parks a future in `_pending_inputs` keyed by `(chat_id, user_id)`, and it is `telegram_bot/catchall.py: _dialog_catchall` — a `@router.message()` with no filter at all — that receives the answer and resolves the future. That catchall must be the **last** handler registered on the router, because aiogram dispatches in registration order and registration order is module import order; registered any earlier, it would swallow every command below it.

Consent buttons follow the same split. Discord uses view objects (`_ConsentView`, `_LeaderConsentView`, `_EntConsentView`) whose callbacks close over the pending decision; Telegram issues a random token, stores the decision in `_pending_consents` / `_pending_econ_consents` / `_pending_quiz_buttons` / `_pending_oly_buttons`, and puts the token in the callback data. Each of those registries is one dictionary in one module, imported by name everywhere else — two modules each declaring their own copy is the classic split bug, and its symptom is a dialog that hangs until it times out.

## Background loops

From `main.py` (cross-platform, started in `main()`):

| Loop | Period | Job |
|---|---|---|
| `retention_loop` | every 24 h | runs every `db.cleanup_old_*` sweep — see Retention |
| `economy_loop` | every 60 min | fires the daily / weekly / monthly ticks that are due (`_run_due_economy_ticks`), broadcasts the new currency values to the `/setlogs` channels after a daily tick, then advances the 24/7 autoproduction by the hours actually elapsed |
| `production_loop` | every 20 s | finishes due `/craft` runs and announces each batch where it was started |
| `shipment_loop` | every 20 s | delivers arrived exports, releases the escrow, announces the arrival |
| `channel_post_loop` | every 60 s | posts each server's weekly statistics and monthly task at the time and in the timezone that server configured (`_due_channel_posts`) |
| `olympiad_loop` | every 60 s | closes an Olympiad whose voting period has ended and re-syncs the command tree |
| `setup_deadline_loop` | every 24 h | leaves the communities whose seven days ran out without a `/setup` |

From `discord_bot/client.py` (started in `DemBot.setup_hook`):

| Loop | Period | Job |
|---|---|---|
| `status_loop` | every 60 min | re-applies the fixed presence text, so it survives a reconnect |
| `backup_loop` | every 12 h | encrypted `dem.db` snapshot to the Discord *and* the Telegram backup chats |
| `founday_loop` | every 60 s | posts the wiki anniversaries whose configured minute has passed |

"Due" is never "the timer fired": the economy ticks compare a UTC period marker against the last one stored in `bot_settings` (`econ_last_daily`, `econ_last_weekly`, `econ_last_monthly`), the scheduled channel posts compare a per-row `last_marker`, and a wiki anniversary compares `last_sent_year`. A bot that was down through the scheduled minute therefore acts as soon as it is back, and a bot restarted twice in an hour does not act twice. The daily tick runs consumption *before* the FX recompute deliberately — the money paid for the day's meal has to be in the supply that the recompute prices against.

## Quizzes

A quiz is data on disk, not code: `src/quizzes/<id>/scoring.json` holds the axes, questions and scoring, and `src/i18n/quizzes/<id>.<lang>.json` holds its text in the six languages. `quizzes.py` is the engine — it loads and caches both (`_scoring_cache`, `_content_cache`), shuffles the options per run, scores the answers and formats the breakdown. It stays a single module and must not become a package: `src/quizzes/` is already that name on disk, and a package would shadow the data directory and take the engine with it.

A result is stored only with the user's explicit consent, at most the two most recent attempts per quiz (`QUIZ_HISTORY_LIMIT`), and it is read through `user_identities` so a linked pair shares one history. `/quizzes-compare` puts two people's breakdowns side by side unless the other person forbade it in `/privacy` (`quiz_privacy`); `/quizzes-previous` compares a person's own two attempts. A paused run keeps its progress for seven days (`quiz_progress`).

## The Olympiad

The Olympiad is a bounded event for choosing the best wiki projects, and almost everything about it is temporary. `/setolympiad` opens it and fixes the voting period; contests (`/setcontest`) belong to a language category or to `int` for a cross-language one, and carry the ids of two Discord chats — one where votes go for review, one where accepted votes are published. Candidate wikis are managed with `/editcontest`. Voting happens only in a private chat with the bot, as a dialog with a Stop button on every question.

Its five follow-up commands are **not** `@bot.tree.command`. They are declared with `@app_commands.command`, collected into `discord_bot/olympiad.py: OLYMPIAD_COMMANDS`, and added to or removed from the command tree at runtime by `_apply_olympiad_commands` / `sync_olympiad_commands` — because the commands should not appear in the picker while no Olympiad exists. Converting any of them into a static `@bot.tree.command` removes it from that mechanism. Telegram publishes no command list, so there the same gating is `/help` omitting them plus a refusal in the handler.

The voting dialog itself lives in `olympiad.py` and is shared by both halves, which each supply a dialog object with the same four methods (`send`, `ask`, `choose`, `choose_numbers`) over their own transport — that is the one place in this bot where the two platforms meet behind one implementation, and it works only because the dialog asks questions and reads text rather than rendering anything. It asks, in order: the language of this private chat (with a button per localization) unless one is already known; the person's Fandom account, skipped outright when `/verify` or an earlier `remember` already answered it and otherwise taken either as a username or, behind an "Another way" button, as a link to a profile on any wiki host showing their messenger handle; a wiki where they have real activity, which is what makes the review quick; the contest; up to three candidate wikis by number; and finally the vote itself. Every question carries a Stop button.

The vote arrives in the contest's review chat as an embed whose footer carries the voter, their messenger id and the vote's key. A reviewer answers `/accepted <key> <wiki username> [remember]`, which republishes the vote in the accepted-votes chat under that wiki name and, with `remember`, writes the account into `wiki_accounts` so the bot never asks that person again — or `/denied <key> <reason>`. Either way the voter is told by DM. One person holds one vote per contest: voting again replaces the earlier one and goes back for review marked as an update. Points are counted by people, not by the bot.

The review and accepted-vote chats are always on Discord, so the embeds and the two reviewer commands live in the Discord half even for votes cast on Telegram; `telegram_bot/olympiad.py` calls `post_olympiad_vote` for its own voters. When the period ends every contest, candidate and vote is deleted. Exactly two things outlive an Olympiad, and both expire a year later on their own: the language someone picked in their private chat, and a wiki account a reviewer chose to remember with `/accepted … remember`.

## Wiki anniversaries

`/wiki-founday` registers a wiki's URL, name, founding moment and the UTC time of day the greeting should go out (`wiki_foundays`). Once a minute `founday_loop` asks which rows are due today at a minute already past, and posts a localized congratulation — bilingual when the wiki's language differs from the chat's. Delivery prefers a webhook so the greeting carries the wiki's own name and avatar, and falls back to an ordinary bot message where the bot may not manage webhooks. `last_sent_year` on the row is what makes the yearly cycle idempotent: it is compared, not a timer, so a bot that was down at 06:00 still greets the wiki when it comes back that day.

## The setup deadline

A community added to the bot has seven days (`setup_deadline.py: SETUP_GRACE_SECONDS`) to be bound to a union with `/setup`; otherwise the daily sweep leaves it and reports the departure to the service chats. Nothing is said in the community itself — `/setup` is a Bot Admin command, so the people who could act on a warning are the operators, and the service chats are where they read.

Three guards decide who the rule can reach, and each answers a different failure. `bot_settings.setup_rule_since`, planted on the first start of the version that introduced the rule, grandfathers everything that joined before it, which is what stops a deployment from walking out of every server it was already sitting in. `setup_deadlines.settled_at`, written by the first sweep that finds a community bound, makes the check one-shot, so a union binding removed years later cannot put the bot out of a door it was long since welcomed through. And the chats named in `config.py` — `SERVICE_CHATS`, `BACKUP_CHATS`, `SUPPORT_CHATS` — are exempt outright, because that infrastructure is the operator's own and is bound to nothing.

The clock itself is asymmetric. Discord publishes `Guild.me.joined_at`, so the Discord half needs no stored time at all and cannot be confused by a restart or a lost event; `on_guild_join` writes a row anyway, but as a record rather than as the clock. Telegram publishes nothing of the kind, so `telegram_bot/client.py: my_chat_member_update` writes the row that starts the clock, and only when the old status was `left` or `kicked` — a promotion to administrator in a group the bot has been in for years must not read as a fresh arrival. A group with no row is never examined, which is exactly what every pre-rule group looks like.

## Retention

`main.py: retention_loop` runs every sweep once a day; each function carries its own age and the reason for it. Thirty minutes: `pending_links` (the account-linking handshake window). Seven days: `quiz_progress` (a paused run) and `shipments` past their arrival — the latter a safety net, since a shipment is normally deleted the moment it lands. Thirty days: `join_requests`. Seventy days: `server_production` and `server_consumption`, which is as far back as the weekly statistics, the monthly evaluation and next month's task generation ever look. Four hundred days: `server_tasks`. One year: `loc_suggestions`, the language someone chose in a private chat (`chat_settings.is_dm`) and a reviewer-remembered `wiki_accounts` row. Ten years: `quiz_results`, an outer bound that applies whatever the user consented to.

Two more deletions are not on the loop because they belong to an event rather than to a clock: `on_guild_remove` drops a server's union binding, language rows, anniversaries and log channels, and the end of an Olympiad drops every contest, candidate and vote.

## Feature → file

| Feature | Where the code lives |
|---|---|
| DB connection, `init()`, the whole public API | `db/__init__.py` |
| Schema and additive migrations | `db/schema.py`, `db/schema_economy.py` |
| Base goods and their seeding | `db/schema_economy.py: BASE_GOODS`, `_seed_base_goods` |
| Shared code namespace, code generation | `db/unions.py: code_taken`, `generate_code` |
| Unions and chats, `/setup` | `db/unions.py`, `discord_bot/commands/unions.py`, `telegram_bot/commands/unions.py` |
| Chat languages, `/lang`, `/locallang`, log/task channels | `db/settings.py`, `db/serverstats.py`, the two `commands/settings.py` |
| Admin roles, `/setadmin`, `/localizer-add`, `/backup` | `db/admins.py`, the two `commands/admins.py` |
| Verification, `/verify`, `/add-telegram`, `/add-discord`, `/privacy` | `fandom.py`, `db/users.py`, the two `commands/user.py` |
| Parties: cards, menus, leadership | `discord_bot/parties.py`, `telegram_bot/parties.py`, storage in `db/parties.py` |
| Party commands, join requests | `discord_bot/commands/parties.py`, `telegram_bot/commands/parties.py` |
| Governing bodies | `db/govt.py`, the two `commands/govt.py` |
| Banks, accounts, journal, energy | `db/banks.py`, the two `commands/banks.py` |
| Amount parsing and formatting, mastery, unit value, craft cost | `economy/money.py` |
| Earning from messages | `economy/activity.py`, entry points `discord_bot/events.py: on_message` and `telegram_bot/catchall.py: _try_earn_tg` |
| Earning channels, `/set-earn` | `db/goods.py`, the two `commands/banks.py` |
| Goods, inventories, mastery, professions | `db/goods.py`, the two `commands/goods.py` |
| Production, `/craft`, `/autocraft`, `/autosend` | `economy/production.py`, loop in `main.py: production_loop` |
| Leaderboards (`/top`) | `stats.py: leaderboard`, queries in `db/goods.py` and `db/banks.py`, commands in the two `commands/goods.py` |
| Private progress notices | `main.py: _send_progress_dm`, sent only for what a person started themselves |
| Conversion spreads, per bank and per pair | `economy/trade.py: convert_fee`, storage in `db/trade.py`, set from `/edit-bank` |
| Enterprises, positions, salaries | `db/enterprises.py`, `economy/payroll.py`, the two `commands/enterprises.py` |
| Export logistics, shipments, `/transit` | `economy/logistics.py`, `db/logistics.py`, loop in `main.py: shipment_loop` |
| Daily consumption, provision, GDP | `economy/consumption.py`, storage in `db/serverstats.py` |
| Monthly tasks | `economy/tasks.py`, storage in `db/serverstats.py` |
| Weekly statistics and the monthly task post | `stats.py`, loop in `main.py: channel_post_loop` |
| Fines and debt | `db/banks.py: mint`/`burn`/`transfer`, `/fine` in the two `commands/banks.py` |
| Treaties, currency values, rates, `/convert` | `economy/trade.py`, `db/trade.py`, the two `commands/trade.py` |
| Wages, party dues, payroll ticks | `economy/payroll.py`, `db/trade.py`, the two `commands/trade.py` |
| Economy tick scheduling | `main.py: _run_due_economy_ticks`, `economy_loop` |
| Quizzes: engine, data, results | `quizzes.py`, data in `src/quizzes/`, storage in `db/quizzes.py`, the two `commands/quizzes.py` |
| Olympiad: engine and dialogs | `olympiad.py`, storage in `db/olympiad.py` |
| Olympiad: dynamic commands, review embeds, verdicts | `discord_bot/olympiad.py`, `telegram_bot/olympiad.py` |
| Wiki anniversaries | `db/foundays.py`, `discord_bot/commands/foundays.py`, loop in `discord_bot/client.py` |
| Message search, `/find-with`, `/find-without` | `discord_bot/commands/moderation.py` |
| Interactive dialogs and consent buttons | `discord_bot/dialogs.py`, `telegram_bot/dialogs.py`, `telegram_bot/callbacks.py`, `telegram_bot/catchall.py` |
| Setup deadline: policy and sweep | `setup_deadline.py`, storage in `db/onboarding.py`, joins recorded in `discord_bot/events.py: on_guild_join` and `telegram_bot/client.py: my_chat_member_update` |
| Retention sweeps | the `cleanup_old_*` helpers across `db/`, driven by `main.py: retention_loop` |
| Localization runtime | `utils.py` (`_load_i18n`, `localized*`), files in `src/i18n/` |
| Localization commands and suggestions | the two `commands/locale.py` |
| Markup conversion (TG entities ↔ Discord markdown) | `message_relay.py` |
| Rate limiting | `utils.py: rate_limit_ok` |
| Service events, encrypted backups | `utils.py: send_service_event`, `backup_crypto.py`, `restore_backup.py`, loop in `discord_bot/client.py` |

## Neighbours

The parent folder holds sibling projects this repo cooperates with but never imports: **Confederate** in `bridge_bot/` and **Confederate Guard** in `guard_bot/` (both built by the same hands and split along the same lines, which is why the package and module names here are theirs), `panel` (the web control panel; it reads our `src/config.py`, `src/.env`, `src/i18n/` — including the nested `olympiad/` and `quizzes/` sets — and `dem.db` directly from disk, and launches `python main.py` with cwd `src/`), and `clean_code.py` (a comment stripper run over the whole tree from time to time: it deletes every `#` comment and leaves docstrings alone, so anything worth keeping has to be a docstring — or, inside SQL, a `--` comment within the string literal).
