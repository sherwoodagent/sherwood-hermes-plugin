# Telegram group fund

The `sherwood-monitor` plugin can run a Sherwood fund inside a Telegram group
through Hermes' Telegram gateway. Members pitch ideas to the bot, the agent
riffs on them, the group fires the good ones into draft proposals, and every
proposal gets a live card with a veto meter and a countdown.

Code: `sherwood_monitor/telegram/` (cards, links, flow, handlers) and
`sherwood_monitor/fund/telegram.py` (the adapter to fund state).

## What is social and what is onchain

| Action | Where it happens | Gate |
|---|---|---|
| Pitch an idea (mention the bot) | chat | none |
| 🔥 an idea (reaction or a reply that is just 🔥) | chat, social | 3 distinct people → draft requested |
| Host says "ship it" | chat, social | host Telegram id |
| 👍 Back it | chat, social (callback) | none; it never votes |
| ❌ Object | browser: casts Against | wallet signature, onchain |
| Join / Deposit / Withdraw | browser | wallet signature, onchain |
| 🚀 Ship it / ✍️ Co-sign / approve or remove | chat callback (host only), then onchain where it applies | host Telegram id in chat, wallet onchain |

Governance is optimistic veto: only Against counts. A proposal passes unless
Against reaches 20% of share supply before the window ends (1800 s in the
demo). Silence means pass.

Money never moves from chat. Every money action is a plain URL button that
opens `${APP_BASE_URL}/tg/...` in the browser, where the wallet signs. There is
no Mini App: Telegram's Bot Developer Terms (7.1-7.4) allow blockchain Mini
Apps on TON only. The chat allowlist decides who can talk to the bot; it does
not gate money. The contracts do.

Bots never see each other's messages in groups. Guest agents coordinate through
the shared fund state, never by reading each other in chat.

## Setup

### 1. BotFather

The demo bot is `@sherwoodagentbot`. Its configured state:

- `/setprivacy` → **Disable**. The bot then receives every group message, which
  the pitch flow needs. After changing privacy, remove the bot from the group
  and add it again: Telegram keeps the old setting until it rejoins.
- `/setjoingroups` → enabled.
- `/setdomain` → `app.sherwood.sh`.
- `/setcommands`:

  ```
  fund - Fund card: NAV, P&L, positions, open proposals
  leaderboard - Best ideas by P&L
  join_fund - Join this fund
  ```

  Telegram command names allow only lowercase letters, digits and `_`, so the
  command is `/join_fund`. The handler also answers a typed `/join-fund`.

### 2. Make the bot a group admin

Telegram sends `message_reaction` updates only to bots that are admins in the
chat, whatever the privacy setting. Promote `@sherwoodagentbot` to admin (no
extra rights needed) so 🔥 reactions count. The handler checks
`getChatMember` and adapts:

| Mode | When | What works |
|---|---|---|
| `full` | bot is admin | everything: pitches, 🔥 reactions, 🔥 replies, "ship it" |
| `all_messages` | privacy off, not admin (current state) | pitches, 🔥 replies, "ship it"; reactions are not delivered |
| `mention_only` | privacy on, not admin | commands, replies to the bot; plain `@mentions` are not guaranteed by Telegram's docs |

In the two degraded modes the `/fund` card ends with a one-line hint, and the
gateway logs one warning per chat. Nothing breaks: the flow just sees fewer
updates.

### 3. Find the group chat id

With the bot in the group, run:

```bash
TELEGRAM_BOT_TOKEN=... python scripts/telegram_chat_id.py
```

It prints `<chat_id>\t<type>\t<title>` for every chat in the bot's pending
updates and never prints the token. It sends no `offset`, so it consumes
nothing. The join itself produces a `my_chat_member` update; if the output is
empty, send `/start@sherwoodagentbot` in the group and run it again. A 409
Conflict means another process (the Hermes gateway, or a webhook) owns the
token: stop it first or read the id from the gateway log.

Supergroup ids look like `-100XXXXXXXXXX`. Use that value below.

### 4. Hermes gateway config

`docs/telegram.example.yaml` has the full snippet. In short:

```yaml
telegram:
  group_allowed_chats: ["-100XXXXXXXXXX"]   # authorizes the whole group, no DM access
  allowed_chats: ["-100XXXXXXXXXX"]         # where the bot answers
  require_mention: true                      # the agent answers mentions and replies only
  observe_unmentioned_group_messages: true   # other messages become context, not turns
```

Environment (the profile `.env`; secrets are read through Hermes'
`agent.secret_scope.get_secret`, never logged):

| Key | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | bot token (secret) |
| `TELEGRAM_GROUP_ALLOWED_CHATS` | same as `telegram.group_allowed_chats`; this plugin's handlers also honour it |
| `TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES=true` | same as the YAML key; needs privacy off or admin |
| `TELEGRAM_ALLOWED_USERS` | the host's Telegram user id only (DM access for the host) |
| `SHERWOOD_TG_LINK_SECRET` | HMAC key for personal links; shared with the web app (secret) |
| `APP_BASE_URL` | web app base, default `https://app.sherwood.sh` |
| `SHERWOOD_TG_PERSONAL_LINKS` | `ephemeral` (default) or `dm`, see below |
| `TELEGRAM_BOT_USERNAME` | optional; otherwise taken from `getMe` |

Hermes polls with `allowed_updates=Update.ALL_TYPES`, which includes
`message_reaction`. A standalone runner must list `message_reaction`
explicitly; `getUpdates` leaves it out by default.

## Commands

| Command | Reply |
|---|---|
| `/fund` | fund card |
| `/leaderboard` | ideas ranked by P&L |
| `/join_fund` (or `/join-fund`) | join prompt with a Join button |

These run before Hermes' own command handling. If plugin-core also registers a
Hermes slash command named `fund`, the Telegram handler answers it in groups.

## Cards and buttons

Renderers in `sherwood_monitor/telegram/cards.py` are pure and return
`(text_html, InlineKeyboardMarkup)`: proposal card, fund card, member #N card,
settle receipt, draft card, join prompt, member request, agent card,
leaderboard, personal link.

A voting proposal card looks like this:

```
🗳 VOTING · Proposal #42
Long NVDA into earnings

NVDA 60% · AMD 40%
Max capital 3,000 USDC · max drawdown 15.0%
Size ▮▮▮░░░░░░░ 30% of NAV

Against 5.0% ▮▮░░░░░░ 20% · ends in 24m
👍 @bob CS · ❌ @dave
idea by @alice · agent atlas
bond 12.4 WOOD (≈$3.10) · agent's skin in the game

Only Against counts. Silence = pass unless Against reaches 20% of shares before the window ends.
[👍 Back it · 2] [❌ Object]
[📊 View fund]
```

"Size" compares the proposal's max capital with fund NAV.

Callback data is `shfund:<action>:<arg>`, at most 64 bytes, routed by one
`CallbackQueryHandler(pattern=r"^shfund:")` so Hermes' own buttons keep working:

| Callback | Who | Effect |
|---|---|---|
| `shfund:back:<pid>` | anyone | toggle 👍 (social), edit the card in place |
| `shfund:vote:<pid>` | anyone | personal `/tg/vote` link (❌ Object) |
| `shfund:deposit:<vault>` | anyone | personal `/tg/deposit` link |
| `shfund:withdraw:<vault>` | anyone | personal `/tg/withdraw` link |
| `shfund:join:<vault>` | anyone | personal `/tg/join` link |
| `shfund:shipit:<draft>` | host | ship the draft to a vote |
| `shfund:propose:<draft>` | host | pre-GA co-sign: personal link to the host signing page |
| `shfund:approve_member:<tg_id>` | host | approve a member |
| `shfund:approve_agent:<name>` | host | approve a guest agent |
| `shfund:remove_agent:<name>` | host | remove an agent |

Host-only callbacks compare the tapper's Telegram id with the fund's host id
and refuse everyone else with an alert. That check is a chat convenience; the
contracts still gate anything onchain.

### Live cards

`cards_live.py` posts cards and edits them in place:

- `editMessageText` for text cards; for cards with an image,
  `editMessageCaption` when only the text changed and `editMessageMedia` when
  the image changed.
- Edits are idempotent: the last rendered text, keyboard and image are kept per
  message, unchanged renders skip the API call, and Telegram's
  "message is not modified" counts as success. Rate limits skip one tick; a
  deleted card stops its job.
- While a proposal is `voting`, a job re-renders it every 60 s (countdown and
  veto meter). When the window closes or the status leaves `voting`, one final
  edit lands and the job stops. It uses PTB's `JobQueue` when the `job-queue`
  extra is installed, an asyncio task otherwise.

## Deep links (contract with the web app)

```
${APP_BASE_URL}/tg/<join|deposit|withdraw>?fund=<vault>&tg=<telegram_user_id>&ts=<unix_seconds>&sig=<hex>
    sig = hex(hmac_sha256(SHERWOOD_TG_LINK_SECRET, "<vault>|<tg>|<ts>"))

${APP_BASE_URL}/tg/vote?proposal=<governor>:<proposal_id>&tg=<telegram_user_id>&ts=<unix_seconds>&sig=<hex>
    sig = hex(hmac_sha256(SHERWOOD_TG_LINK_SECRET, "<governor>:<proposal_id>|<tg>|<ts>"))

${APP_BASE_URL}/tg/fund/<vault>        public, unsigned
```

Rules both sides follow (`sherwood_monitor/telegram/links.py`):

- Addresses are lowercase `0x` + 40 hex, in the signed string and in the URL.
- `tg` is the decimal Telegram user id; `ts` is integer unix seconds.
- The `:` in `proposal=` is written literally. A parser that sees `%3A`
  decodes it to the same value. Sign over the decoded query values.
- `sig` is 64 lowercase hex chars. Verify with a constant-time compare, and
  reject `ts` older than 15 minutes or more than 60 s in the future
  (`verify_link_signature` is the reference).
- The signature proves the bot issued the link to that Telegram user. It does
  not authorize money: the wallet signs in the browser.

Test vectors (secret is the ASCII string `sherwood-test-vector-secret`, used
only for tests):

| Signed string | `sig` |
|---|---|
| `0x1111111111111111111111111111111111111111\|123456789\|1700000000` | `db93ae34690df02d34a6394373e409cdc8683b891431c10b59a42a837b1a680e` |
| `0x2222222222222222222222222222222222222222:42\|123456789\|1700000000` | `7f612dafad917a80a2d0c75a571fb9c8e7d24a770336d17848ac41b020da854a` |
| `0xabcdefabcdefabcdefabcdefabcdefabcdefabcd\|987654321\|1762300800` (built from the mixed-case `0xAbCdEf…AbCd`) | `ae3700a91ef375f41a5fbfa4635c2ff3dec4f617b31e0f01ae599baa5cba67f7` |

Full URLs for the first two:

```
https://app.sherwood.sh/tg/deposit?fund=0x1111111111111111111111111111111111111111&tg=123456789&ts=1700000000&sig=db93ae34690df02d34a6394373e409cdc8683b891431c10b59a42a837b1a680e
https://app.sherwood.sh/tg/vote?proposal=0x2222222222222222222222222222222222222222:42&tg=123456789&ts=1700000000&sig=7f612dafad917a80a2d0c75a571fb9c8e7d24a770336d17848ac41b020da854a
```

Check one from a shell:

```bash
printf '0x1111111111111111111111111111111111111111|123456789|1700000000' \
  | openssl dgst -sha256 -hmac 'sherwood-test-vector-secret'
```

`tests/test_tg_links.py` pins the same vectors.

### Personal links cost two taps

Deposit, withdraw, join and vote links carry the tapper's Telegram id, so they
cannot sit on a shared card. The card has a generic callback button; tapping it
produces a link for that person:

1. **Ephemeral message (default).** The bot answers in the group with a message
   only the tapper sees, holding the URL button. This uses Bot API 10.3
   `ephemeral_message_parameters` (`receiver_user_id`, `callback_query_id`),
   sent within 15 s of the tap. PTB 22.8 speaks Bot API 10.0, so the field is
   passed through `api_kwargs`. Telegram does not guarantee delivery to an
   offline client.
2. **DM.** If the ephemeral send fails, the bot DMs the link, which works once
   the user has started the bot.
3. **Start link.** If the DM is refused, the callback answer opens
   `https://t.me/sherwoodagentbot?start=<payload>`. The user lands in the bot's
   DM, presses Start, and gets the link there. Payloads: `sf<k>_<vault hex>[_<id>]`
   with `k` = `d` deposit, `w` withdraw, `j` join, `v` vote, `p` host co-sign.

So a member taps twice (the card button, then the URL button), three times on
the first-ever DM path. `answerCallbackQuery` cannot open the app directly: the
Bot API only lets its `url` open a game or a `t.me/<bot>?start=` link. Set
`SHERWOOD_TG_PERSONAL_LINKS=dm` to skip the ephemeral attempt.

## Pitch → riff → draft

`flow.py`:

1. A member mentions the bot with a trade idea (a `$TICKER` or words like
   long, short, buy, hedge). That message is the pitch. The flow queues a short
   context block; the plugin's `pre_llm_call` hook adds it to the agent turn
   Hermes starts for that mention, and the agent replies in its own words (the
   riff).
2. The group 🔥s it: reactions on the pitch or the riff (admin only), or
   replies whose whole text is 🔥 (any mode). Distinct people across the pitch
   and its riff count together.
3. At 3 people, or when the host replies "ship it" (to the pitch or the riff,
   or with a mention, which targets the latest open pitch), the flow emits
   `DraftRequested(idea_by, text, source_message_id, chat_id, trigger, ...)`
   once per pitch through `on_draft_requested`. If that callback raises, the
   next 🔥 or "ship it" retries.

The bot never receives its own messages and Hermes has no hook that returns
the message id of the agent's reply. The flow learns riff ids from replies to
bot messages, treats an unseen message right after a pitch as its riff when it
gets a reaction, and accepts `PitchFlow.link_riff(chat_id, pitch_id, riff_id)`
when the integrator knows the id. Cards the plugin posts are never counted as
riffs. Pitches live 6 hours, in memory.

## Integration (plugin-core)

```python
from sherwood_monitor.fund.telegram import CallbackFundGateway, register_telegram

gateway = CallbackFundGateway(get_fund_for_chat=..., get_proposal=..., ...)
runtime = register_telegram(ctx, gateway)   # factory + pre_llm_call hook
...
runtime.submit(runtime.post_proposal_card(chat_id, proposal_id))   # any thread
runtime.submit(runtime.refresh_proposal_card(chat_id, proposal_id))
runtime.submit(runtime.post_draft_card(chat_id, draft_id, bond_shortfall_wood=4.0))
runtime.submit(runtime.post_receipt(chat_id, receipt))
```

`CallbackFundGateway` takes sync or async callables over plugin-core's records;
its docstring lists each one. PTB is imported only inside the factory Hermes
calls when the Telegram adapter connects, so `register()` works without it.

## Smoke test

```bash
pip install -e '.[telegram]'
TELEGRAM_BOT_TOKEN=... TELEGRAM_TEST_CHAT_ID=-100XXXXXXXXXX python scripts/telegram_smoke.py
python scripts/telegram_smoke.py --dry-run   # render only, no network
```

It prints the bot's privacy and admin status in the chat, posts a proposal
card, edits it twice (veto meter, then countdown) plus one no-op edit, posts a
fund card and a receipt, and exits. It never prints the token.
