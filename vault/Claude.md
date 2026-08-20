# Claude — instructions to future sessions

You are working on **Solana-olala**, a fully automated, risk-gated Solana
copy-trading system. Read [[Project]] for the complete technical picture
and [[Tasks]] for what is open. Keep both current — they are the project's
memory, and the operator relies on them across sessions.

## Standing decisions (do not re-litigate)

- **Paper simulation is the default.** Pure local simulation against
  real mainnet data. The operator explicitly chose local simulation over
  devnet after the devnet trade-offs were explained (no Jupiter, no
  liquidity, no traders there). There is NO universe-level mode anymore
  (operator removed it 2026-08-17): paper and live wallets coexist, and
  live execution is dormant behind each wallet's own arm switch
  (unlocked keystore holding that wallet's key; dev_mode refuses to
  arm; disarming always allowed). Do not reintroduce a global mode.
- **Discovery has no seed-token list** (operator removed it 2026-08-16
  after the old pool-sampling harvested an arb bot). Candidate sources:
  the Solana Tracker PnL leaderboard (`chain.solana_tracker_api_key`,
  throttled to `filters_solanatracker.interval_sec`), with winners'
  holders as the always-available on-chain base. **The DEX census was
  removed 2026-08-19** — it spent a fixed slice of every scan budget to
  enumerate wallets that are merely ACTIVE, which is the one property a
  copy trader can find anywhere. Do not reintroduce it.
  Birdeye was removed entirely (operator decision 2026-08-18) — do not
  reintroduce it. Operator requirement (2026-08-17): the service is an
  OPTIONAL accelerator — no key, a rate limit, or an outage must fall
  through to on-chain, never stall the sweep. Services only nominate;
  judgment is our own scan. Do not reintroduce seed lists.
- **The roster self-improves** (operator requirement 2026-08-17): when a
  passing candidate measures stronger than the weakest followed trader
  (by `discovery.replace_margin`), the weakest is auto-retired and
  replaced. Discovery never idles on a full roster. Do not restore the
  roster-full early return.
- **Chain access is routed, public-first (reworked 2026-08-19).** There
  is ONE `JsonRpcSource` described entirely by config; `chain/router.py`
  fails over between sources per POLICY (`tracking`, `history`,
  `metadata`, `broadcast`, `confirm`, `stream`). Adding, reordering or
  disabling an endpoint is a config edit, never code. Do not add a class
  per vendor, and do not add providers requiring paid accounts without
  asking.
- **Tracking numbers are MEASURED, not chosen.** Cost is
  `wallets ÷ interval` because public nodes meter by SUB-CALL, NOT by
  HTTP request. This is counter-intuitive and was assumed the wrong way
  round in the first plan draft, which is why that plan was rejected and
  rewritten. Controlled proof (2026-08-20), holding request rate fixed
  at exactly 1 POST/s: 1 wallet per request ran 8/8 clean, 100 wallets
  per request throttled 3 of 8. Same requests, 100x the sub-calls. A
  50-address batch costs 50. Measured on publicnode: 10 wallet-calls/s
  runs clean, 16.7/s throttles within ~40s, and a 100-wallet batch is
  served in 670 ms. The poll interval is DERIVED from roster size
  against that ceiling; never hardcode a cadence. Re-measure before
  changing any of it (probe scripts pattern is in [[Tasks]]).
- **Helius is not a tracking peer.** It refuses roster-sized batches
  (50 elements → HTTP 429 in 34 ms) and a heartbeat there would cost
  ~2.6M credits/month against a 1M allowance. Its jobs are the
  WebSocket stream, per-trade fetches, and broadcast/confirm.
- **Broadcast and confirmation must share one source.** A node that
  never saw our transaction answers `null`, which is indistinguishable
  from "it never landed" — that wrote TIMEOUT receipts for swaps that
  were confirming. Use `RoutedProvider.broadcast_session()`.
- **The push stream is judged by what it MISSES, never by silence.**
  A quiet market produces no notifications, so a staleness timeout
  flapped tracking onto the expensive gear every time trading went still
  for a minute. Evidence of a dead subscription is the POLL catching a
  trade older than `tracking.stream_miss_grace_sec` that the stream never
  reported — and only for trades that landed while the stream was
  answerable (after it had proven itself, and not across a reconnect),
  or every restart would blame it for downtime.
- **A watermark that cannot advance is as dangerous as one that
  advances wrongly.** The walk must page until it REACHES the watermark,
  never stopping because a page held no fresh work — a restart leaves
  every recent signature already in `processed`, and stopping there froze
  the marker while the chain moved on, until the wallet was wedged
  permanently. And an unbridgeable gap must RE-ARM, not wedge: skipping a
  gap is safe (the marker never moves backwards), while going blind just
  fails quietly. Measured 2026-08-20: 858 gap errors, three wallets blind
  for eighteen hours, one copied trade overnight.
- **The cursor is a `(slot, signature)` watermark, never a bare
  signature.** A signature cannot be compared, so a window that missed
  it made the old follower treat every entry as fresh and re-copy them.
  A walk that cannot reach the watermark raises `SourceIncomplete` and
  the cursor does NOT move: losing sight of trades is recoverable,
  copying them twice is not.
- **Strict trader filters** are the shipped defaults (90d history, 200
  trades, 60% win rate, 24h activity, $20M–$5B mcap band, $500k
  liquidity). Loosen only in config, never in code.
- **One config file (operator decision 2026-08-19).** `config.yaml` is
  READ ONLY to the process, so its comments survive; runtime edits from
  the UI persist to `config.runtime.yaml` and layer on top at startup.
  One `PUT /api/config` used to `yaml.safe_dump` the whole file and
  erase every explanation in it. Built-in defaults stay STRICT.
- **HFT mode is gone (operator decision 2026-08-19): "We are only doing
  normal trading now."** The `hft` flag, both profile files, all profile
  machinery and the frontend badge were removed. Do not reintroduce a
  trading-style switch; if a second style is ever wanted, it is a
  separate config file the operator points at, not a mode flag.
- **Long-only, DEX-only, copy-only.** No shorts, no hedges, no
  self-originated trades. Risk math gates everything; a blocked trade is
  declined, never squeezed through.
- **Visual world: Galaxy Starmap** (user-locked on the Impeccable decision
  page, seed 8217fc00). Hierarchy is user-corrected and binding: wallets
  are planets, traders are moons orbiting their assigned planet, positions
  are satellites between. Black dominant, purple secondary, cyan gains,
  rose losses, live-red armed wallets. The direction contract lives as
  the first comment in `frontend/index.html`.

## How to work here

- Production-grade code only; no debug prints, no dev scaffolding in the
  repo. Logging goes through the `logging` module.
- The OOP abstractions are load-bearing: `Wallet` → `SolanaWallet` →
  paper/live; `RpcProvider` → public/Helius; `TradeExecutor` →
  paper/live. New chains and providers are additions behind these types,
  never rewrites.
- Everything the daemons learn is published on the `EventBus` and mirrored
  to SQLite; the frontend renders only what the stream says. If you add
  state, add its event and its snapshot field.
- Use the venv at `backend/.venv` for everything Python. Never install
  into the system interpreter.
- Test with the mock-frontend trick: copy `frontend/` to a scratchpad,
  replace `js/ws.js` with a fixture replayer, serve statically, screenshot
  with headless Firefox. Headless Firefox runs no rAF frames — first
  paints must be synchronous (the galaxy settles its force layout with
  manual ticks for exactly this reason).

## Traps already discovered

- CSS `display` on a class defeats the `hidden` attribute — the
  stylesheet carries explicit `[hidden] { display: none }` rules for
  `.drawer`, `.inspector`, `.scan-banner`. Keep that pattern.
- D3 force layouts explode off-canvas if charge outweighs weak positional
  anchors: planets are pinned (`fx`/`fy`), charge is capped with
  `distanceMax`, and the layout is settled synchronously (150 manual
  ticks) before paint.
- `getSignaturesForAddress` returns newest-first; all cursor logic
  (discovery `history_cursor`, follower `follow_cursor`) depends on that.
- **Python json is not browser JSON.** `json.dumps` emits `Infinity`/`NaN`
  tokens that `JSON.parse` rejects — one non-finite float anywhere in a
  payload makes the WHOLE snapshot/response unparseable in the browser
  while every Python-side test still passes (Python's parser accepts the
  tokens). All frontend-bound serialization must go through
  `events.json_safe()` + `allow_nan=False` (stream `_frame`,
  `StrictJSONProvider` in server.py). Domain `to_dict()`s must never
  emit non-finite floats (`TraderStats.inactive_hours` → null).
- Public RPC is ~2 req/s shared across all daemons via token-bucket rate
  limiters. Qualifying one trader takes hours; that is expected, not a
  bug. The scan banner tells the operator so.
- **Enumerating ACTIVE wallets finds bots, not traders** — live DEX flow
  is dominated by high-frequency bots, so wallets seen repeatedly in it
  are usually bots (measured twice: MVP round, and again 2026-08-18).
  This is why the DEX census was deleted 2026-08-19. The leaderboard is
  NOT the bot source: 85/100 of its top-100 are at human cadence. Select
  on having been EARLY into something that worked, never on activity.
- **Trades/day and signatures/day are different metrics, with different
  denominators.** The service's `trades_per_day` is
  `counts.trades / period.tradingDays` — per ACTIVE trading day. The
  pre-screen's rate is on-chain SIGNATURES (transfers, failed txs, ATA
  creation — not just swaps) per CALENDAR day across the sampled span.
  The ceiling is `max_trades_per_day × 5` because of that mismatch.
- **Never measure a rate from a shallow signature sample** (fixed
  2026-08-18). The probe used to fetch 30 signatures, which measures the
  wallet's most recent BURST: a real nominee read 1,284 sigs/day at 30
  signatures vs 42/day at 500 — a 30× overestimate that rejected a
  genuine trader. `getSignaturesForAddress` returns up to 1,000 for the
  SAME one credit, so `PRESCREEN_PROBE` is always the max.
- **`dev_mode` is INVERTED from the usual convention** (operator spec
  2026-08-18): `true` = APPLY `filters_onchain`; `false` = IGNORE them.
  It governs the on-chain stream only. Token safety follows it for paper
  wallets but is UNCONDITIONAL for live wallets, and arming no longer
  depends on it at all.
- **Win rates are FRACTIONS everywhere** (0.55 = 55%), in both
  `filters_onchain.min_win_rate` and
  `filters_solanatracker.min_win_rate`; the client converts to percent
  at the wire. ROI keeps `_pct` (percent). Config load REJECTS a win
  rate outside 0..1 and an ROI percent under 1 — mixed units once let
  `0.7` mean 0.7% and seated traders winning one trade in five.
- **Config sections are named for the stream they govern:**
  `filters_onchain` (stream B) and `filters_solanatracker` (stream A).
- **TWO STREAMS, TWO RULE SETS** (operator decision 2026-08-18; REVISES
  the earlier "judgment is ONLY our own computed win rate" doctrine).
  *Stream A* `discovery/leaderboard.py`: the service ranked and vetted
  these wallets, so we follow what it returns. Configured ONLY by the
  `filters_solanatracker` config section. *Stream B*
  `discovery/onchain.py`: winners' holders, nobody vetted them, so
  `filters_onchain` is their admission gate.
  **Never apply `filters_onchain` to stream A** — that coupling
  re-derived the service's judgment with a narrower window and a
  reconstructor blind to multi-hop swaps, and dropped vetted traders.
  Both compete for seats through `discovery/roster.py`.
  Risk is untouched either way: token safety, position sizing and the
  ATR panic stop gate every trade. `leaderboard.max_trades_per_day` is
  kept as a MECHANICAL limit (copy speed + RPC affordability), not a
  quality judgment.
- **Fall-through is unconditional and tested.** Stream B runs every
  sweep whether stream A is disabled, unkeyed, throttled, rate-limited
  or raising. An external service may slow discovery; it must never
  stop it.
- **Ranking by absolute PnL is ranking by SCALE, not skill** (measured
  2026-08-18). `sort=realized` returns a board whose MEDIAN wallet does
  ~487,000 trades at ~16,000/day — MEV machines out-earn humans in
  dollars, so a money-ranking surfaces machines. **ROI is the fix**:
  machines show ~20–85% return on enormous volume, real traders show
  hundreds to thousands of percent. `leaderboard_min_roi_pct: 100` +
  `min_win_rate_pct: 55` took nominees from 4/100 to 100/100 under our
  activity cap (median 43 trades/day, PnL $277k–$1.3M). `minWinRate`
  ALONE does not help — bots have high win rates too. Do not remove the
  ROI floor.
