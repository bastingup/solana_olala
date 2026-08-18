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
  throttled to `discovery.leaderboard_interval_sec`), with the DEX
  census and winners' holders as the always-available on-chain base.
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
- **Chain access is pluggable, public-first.** Public RPC endpoints +
  DexScreener + Jupiter lite-api, all keyless. A free Helius key in
  `backend/config.yaml` (chain.helius_api_key) upgrades throughput via
  `HeliusRpcProvider` automatically. Do not add providers that require
  paid accounts without asking.
- **Strict trader filters** are the shipped defaults (90d history, 200
  trades, 60% win rate, 24h activity, $20M–$5B mcap band, $500k
  liquidity). Loosen only in config, never in code.
- **Config is split by trading style (operator decision 2026-08-18):**
  master `config.yaml` holds the mode flags (`dev_mode`, `hft`),
  credentials and RISK EXPOSURE; `config.hft.yaml` /
  `config.slow.yaml` hold filters/discovery/follow. `hft: true` picks
  the HFT profile at boot. Risk stays in the master ON PURPOSE —
  switching strategy must never change how much money a trade touches.
  Keep the flags in the master and the active profile in the snapshot:
  the earlier parallel-file design was reverted precisely because the
  flag was invisible to the running config.
- **The HFT profile** (operator's current setting): 7d judgment window
  (persistence via the 30 active-day nomination floor), 600 trades/day
  ceiling, hold-time gate off, leaderboard sorted by realized PnL.
  Operator explicitly accepts service win-rate distortion; judgment =
  net realized PnL + SHARP. The 2-5s copy latency caveat is recorded in
  [[Tasks]] — paper PnL of the copies is the arbiter before live is
  ever considered.
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
- **The DEX census is a bot generator by construction** — live DEX flow
  is dominated by high-frequency bots, so wallets seen repeatedly in it
  are usually bots (measured twice: MVP round, and again 2026-08-18).
  The leaderboard is NOT the bot source: 85/100 of its top-100 are at
  human cadence. When "bots blocked" spikes, suspect the census, not the
  service.
- **Trades/day and signatures/day are different metrics.** The
  pre-screen measures on-chain SIGNATURE rate (all activity: transfers,
  failed txs, ATA creation), which runs far above a wallet's swap rate.
  The ceiling is `max_trades_per_day × 5` for exactly that reason.
