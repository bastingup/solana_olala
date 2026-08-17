# Project — technical documentation

Solana-olala: a fully automated, risk-gated copy-trading system for Solana
DEXes. Single operator, localhost only, paper wallets by default. See
[[Claude]] for session instructions and [[Tasks]] for open work.

## Architecture

```
frontend (vanilla JS + D3, served by Flask as static files)
    ▲ WebSocket /ws (state stream)      ▲ REST /api/* (actions)
backend (Flask + flask-sock, one process, daemon threads)
    ├── TraderDiscoveryDaemon   — finds & qualifies traders
    ├── FollowDaemon            — watches followed traders, emits signals
    ├── MarkDaemon              — prices positions, ATR, panic stop
    ├── TradingEngine           — risk-gates signals, executes
    └── SQLite (olala.db) + EventBus (pub/sub → WebSocket)
chain access: public RPC / Helius (keyed, optional) + DexScreener + Jupiter
```

One process, one port (8420). `run.sh` / `run.bat` create the venv at
`backend/.venv`, install `backend/requirements.txt`, start `backend/run.py`,
open the browser.

## Backend layout (`backend/olala/`)

| Module | Responsibility |
|---|---|
| `config.py` | Dataclass config: defaults ← `config.yaml` ← REST updates (persisted back). Sections: server, chain, filters, risk, discovery, follow, paper. |
| `events.py` | `EventBus`: thread-safe pub/sub; per-client bounded queues (drop-oldest on lag). |
| `domain/models.py` | Value objects: `TokenInfo`, `ObservedTrade`, `TraderStats`, `TraderProfile`, `CopySignal`, `RiskVerdict`, `Position`, `Fill`; enums for sides/statuses/exit reasons. |
| `domain/wallet.py` | `Wallet` (ABC) → `SolanaWallet` → `PaperSolanaWallet` (local balance) / `LiveSolanaWallet` (balance from RPC, keys in keystore). Future chains subclass `Wallet`. |
| `chain/rate_limiter.py` | Token-bucket limiter; every outbound HTTP call passes one. |
| `chain/provider.py` | `RpcProvider` (ABC, JSON-RPC gateway with retry/backoff/rotation) → `PublicRpcProvider` (rotating keyless endpoints) / `HeliusRpcProvider` (keyed, faster). `build_provider(config)` picks by presence of a Helius key. |
| `chain/market_data.py` | DexScreener client; best SOL-quoted pair per token; 45s cache. Produces `TokenInfo` (price SOL/USD, liquidity, mcap, pair age). |
| `chain/jupiter.py` | Jupiter lite-api client: quotes + swap-transaction build (live path only). |
| `persistence/database.py` | SQLite behind one lock. Tables: wallets, traders (with history/follow cursors), observed_trades, positions, fills. |
| `services/daemon.py` | `Daemon` base: thread, tick interval, stop event, exception isolation. |
| `services/traders.py` | `TraderRegistry`: in-memory roster backed by DB; owns profiles + cursors; publishes trader events. |
| `discovery/reconstruction.py` | `TradeReconstructor`: DEX-agnostic swap detection by diffing pre/post balances (exactly one non-SOL token moving against SOL). Fee-payer fee excluded from traded amount. |
| `discovery/scoring.py` | FIFO round-trip matching per token; win = positive realized PnL on a sell; score = 0.6·win_rate + 0.2·history + 0.2·volume. The same matching yields quantity-weighted holding durations → `median_hold_minutes`, plus `trades_per_day`. |
| `discovery/filters.py` | Admission gate: history days, trade count, ≥20 round trips before win rate counts, win rate, activity, net profitability, token-quality sampling (median liquidity + mcap band) — plus the **copyability gates**: `trades_per_day ≤ filters.max_trades_per_day` (40) and `median_hold_minutes ≥ filters.min_median_hold_minutes` (30). Profitable-but-uncopyable (arb/MEV) fails here by design. |
| `chain/solana_tracker.py` | `SolanaTrackerClient`: `/v2/pnl/leaderboard/top` — windowed PnL leaderboard with win rates, arb bots excluded upstream. Primary leaderboard feeder when `chain.solana_tracker_api_key` is set (free tier 10k req/month); every failure raises `SolanaTrackerError` for fall-through. |
| `chain/birdeye.py` | `BirdeyeClient`: `/trader/gainers-losers` leaderboard — secondary leaderboard feeder when a key exists; on-chain works without it. |
| `discovery/scanner.py` | `TraderDiscoveryDaemon` (v4, **census-first, win-rate-ranked**). Operator doctrine: enumeration may be broad, but judgment is ONLY our own computed, windowed, bag-adjusted win rate — never a proxy. Primary enumerator: the **DEX census** — every sweep observes live flow of the configured DEX programs (Jupiter v6, Raydium v4, Orca Whirlpool), tallies fee payers in the persistent `sightings` table, and promotes wallets seen trading in ≥`census_min_sightings` sweeps. Leaderboard services (Solana Tracker → Birdeye, keyed, throttled to `discovery.leaderboard_interval_sec` even on failure) and winners' top holders (3 RPC calls/winner) feed the same measurement; ANY service failure falls through to on-chain. Service win rates only order the deep-scan queue. All candidates: pre-screen (thin-by-arithmetic + machine-frequency), then full history scan to `max(min_history_days, skill_window_days)` depth, then windowed scoring. **Roster-full does not idle discovery**: sweeps keep hunting, and a passing candidate whose score clears the weakest followed trader's by `discovery.replace_margin` evicts it automatically (`trader_retired` with "replaced by …"; positions stay with their wallet under the panic stop). |
| Skill metrics | Computed per candidate over `discovery.skill_window_days` (90): **adjusted win rate** (every stale bag — unsold in-window inventory older than `filters.stale_bag_days` (7d) with ≥0.05 SOL cost — counts as a loss), **SHARP** (per-trade Sharpe: mean return per SOL deployed ÷ stdev, capped ±10, needs ≥5 closed trades; gate `filters.min_sharpe` = 0.1), realized PnL, holds, trades/day. History/activity gates use the FULL record so windowing can't dodge the 90-day requirement. Deposits/transfers never enter any number — PnL is swap round trips only. |
| `risk/token_safety.py` | Structural honeypot screen: mint/freeze authority must be revoked, top-10 holders ≤50% of supply, liquidity floor, mcap band, pair ≥14d old. Unavailable safety data ⇒ unsafe. 30-min cache. |
| `risk/engine.py` | `RiskEngine.evaluate_entry`: size = min(equity·per_trade_fraction, 1% of pool liquidity − already invested, cash − reserve [new entries only], per-position ceiling). Rejects under 0.05 SOL with the binding constraint named. |
| `risk/atr.py` | 1-minute candles from price marks; Wilder ATR(14); trailing stop = peak − 3.5·ATR; no stop until warm. |
| `trading/executor.py` | `TradeExecutor` (ABC) → `PaperExecutor` (liquidity-derived slippage model, flat fee) / `LiveJupiterExecutor` (quote → build → sign via keystore → send → **confirm on chain → reconstruct actual amounts from the landed tx**; the quote only sizes the order). Every live attempt records a `Receipt` (confirmed/failed/timeout, quoted vs actual, fee, slot) via `AppContext.record_receipt` → DB + `receipt_recorded` event. Timeout past 100s is definitive (blockhash expiry), so failed closes safely keep the position open. |
| `trading/portfolio.py` | `PortfolioManager`: wallets, positions, balances, exposure snapshots, all mutations persisted + broadcast. |
| `trading/engine.py` | `TradingEngine`: the only caller of executors. Buy: market data → safety → exposure → risk verdict → fill. Sell: full close on trader exit. Panic/manual closes. Executor choice: live only for an armed live wallet. |
| `trading/follower.py` | `FollowDaemon`: per-trader signature polling against `follow_cursor`, oldest-first replay of new swaps into `CopySignal`s. First contact arms the cursor without replaying history. |
| `trading/marker.py` | `MarkDaemon`: re-prices open positions, feeds ATR, trails stops, triggers panic closes, derives SOL/USD, emits `portfolio_tick`. |
| `security/keystore.py` | scrypt(passphrase, salt) → Fernet; file `backend/keystore.enc` (0600). Accepts base58 or solana-keygen JSON secrets. Keys never leave backend memory; never returned by any API. |
| `api/rest.py` | Actions: keystore unlock, wallet add (paper/live), wallet arm/disarm (arming requires unlocked keystore holding the key; refused in dev_mode), trader unfollow, position close, config get/put, state snapshot, fills. |
| `api/stream.py` | `/ws`: snapshot on connect, then every event; ping frames on quiet. |
| `api/server.py` | `AppContext` composition root: builds everything, wires wallet assignment (least-loaded, random tie-break), serves `frontend/` statically. |

## Event vocabulary (WebSocket)

`snapshot`, `portfolio_tick`, `wallet_added/update`, `position_opened/
resized/closed`, `trader_candidate/admitted/rejected/retired`,
`discovery_scan`, `copy_signal`, `risk_rejected`, `execution_error`,
`trade_executed`, `receipt_recorded`, `config_changed`,
`keystore_unlocked`, `ping`. Payloads are the domain objects' `to_dict()`
forms. The snapshot additionally carries `fills` and `receipts` (last 50
each); `GET /api/receipts` lists the full receipt trail.

## Frontend (`frontend/`)

Vanilla ES modules + vendored D3 v7 (`js/vendor/d3.v7.min.js`).
`state.js` (Store: reducers over stream events, derived token wall),
`ws.js` (reconnecting stream), `api.js` (commands), `galaxy.js` (force
starmap: planets pinned, moons via orbit links, satellites linked to
planet+moon, candidates top band, rejected embers bottom band; layout
settled synchronously with 150 manual ticks before paint), `panels.js`
(command bar, wallet rail, feed, roster, chip wall), `app.js` (wiring,
inspector, drawer, per-wallet power button). Design contract: first comment in
`index.html`. Fonts: Chakra Petch (display) + Red Hat Mono (data) via
Google Fonts. The design system is recorded in `DESIGN.md` at repo root.

## Verifying discovery is actually working

Three independent checks, cheapest first:

1. **The discovery console** (top of the galaxy stage). Narrates what the
   scanner is doing right now, driven by the `discovery_status` event:
   phase (SWEEPING / SAMPLING FLOW / SCREENING / READING HISTORY /
   STANDING BY / ROSTER FULL), the candidate source, a one-line activity
   detail, running counters (screened, bots blocked, rejected, following,
   in review, RPC budget left), and a live countdown to the next sweep.
   The daemon retains its last payload in `TraderDiscoveryDaemon
   .last_status`, which rides in the WebSocket snapshot — so a page
   opened between sweeps shows current state instead of an empty console.
   Candidate rows in the roster show `sigs / swaps / depth-of-target`
   with a progress bar; rejected rows carry the reason verbatim, and
   every rejection also posts to the live feed.
2. **The log.** `discovery tick:` lines each scan interval, and one
   `candidate <addr>… scanned N signatures, M swaps, history depth Xd of
   90d needed` per candidate advanced. Provider is named at boot:
   `application context ready (provider: helius|public-rpc[, dev mode])`.
3. **The database** — ground truth:
   ```bash
   sqlite3 backend/olala.db \
     "SELECT status, COUNT(*) FROM traders GROUP BY status;
      SELECT COUNT(*) FROM observed_trades;"
   ```

Expect slow progress by design: a candidate must show 90 days of history
and 200+ trades before it is even scored. Most candidates are rejected;
`rejection_reason` on the trader row says why.

## RPC provider and free-tier credit budget

`chain.helius_api_key` in `backend/config.yaml` switches the provider to
Helius automatically (8 rps vs 2 on public RPC, and the push-subscription
socket moves there too). `chain.requests_per_second` stays at the
public-safe 2.0 — `HeliusRpcProvider` raises its own floor to 8, so the
config value is the public fallback, not the Helius rate. Don't raise it
unless you also plan to keep the key.

**Sustained usage matters on a free plan (~1M credits/month):**

| Source | Rate | Monthly |
|---|---|---|
| Follower, 10 traders @ 12s poll | ~0.83 calls/s | ~2.2M ⚠ over |
| Follower, 10 traders @ 45s poll | ~0.22 calls/s | ~580k |
| Discovery, 60 calls / 300s tick | ~0.2 calls/s | ~520k (runs even when the roster is full — it hunts for upgrades) |

A full roster at the default 12s poll would exhaust a 1M free tier in
roughly two weeks. Because `TraderSubscriber` already delivers trades by
push within seconds, the interval poll is only a safety net — raising
`follow.poll_interval_sec` to 45 costs almost no copy latency and brings
usage inside the free tier. Left at 12 by default (maximum fidelity if
push ever drops); this is the operator's call.

## Arming model (per-wallet, single switch)

Paper and live wallets coexist in one universe. Paper wallets ALWAYS
simulate — nothing arms or disarms them. There is NO universe-level
mode (operator removed it 2026-08-17). Live wallets are red planets
with exactly one switch:

- **Per-wallet arm** — `POST /api/wallets/<id>/arm {armed: bool}`,
  toggled by the power button in the wallet inspector. Arming requires
  an unlocked keystore holding that wallet's key and is refused while
  `dev_mode` is on (dev configs relax the safety screens); disarming is
  always allowed. Persisted in the wallets table (`armed` column).

A disarmed ("dark") live wallet neither opens nor closes positions —
signals are rejected with a visible reason, and closes emit
`execution_error` until the wallet is re-armed. `TradingEngine`
enforces this at signal intake AND at executor routing (defense in depth).

**Correlation gate:** a token may be held by at most
`risk.max_live_wallets_per_token` (default 2) live wallets at once.
Paper wallets and re-sizes are exempt; SOL is the base currency and is
never a position, so it is inherently exempt.

## Push-triggered copying

`TraderSubscriber` (chain/subscriber.py, `websocket-client`) keeps one
WebSocket to the RPC provider (`RpcProvider.ws_endpoint()`) with a
`logsSubscribe` (mentions) per followed trader. Notifications poke
`FollowDaemon.poll_now(address)` (debounced 1.5s), which runs the normal
cursor protocol under a poll lock — so push-triggered and interval polls
share ordering, dedupe, and budget rules. The 12s interval poll remains
as the safety net; if the socket drops (or the lib is missing), the
system degrades to polling, never to silence.

## Trading semantics

- Copy BUY → assigned wallet only; re-size if the (wallet, trader, mint)
  position exists (may draw on reserve); otherwise new entry (never
  touches reserve).
- Copy SELL → full close of the copied position (partial-exit mirroring is
  an open task, see [[Tasks]]).
- Exits: `trader_exit`, `panic_stop` (ATR trail), `manual` (REST).
- Paper fills price at live DexScreener marks with modeled impact
  (½ · trade/liquidity, capped 5%, plus 10bp spread).

## Configuration

`backend/config.yaml` (gitignored, created on first change). Everything
under `filters`, `risk`, `discovery`, `follow` is mutable at
runtime via `PUT /api/config` and persists. Seed mints
for discovery: JUP, JTO, PYTH, RAY, ORCA, WIF, BONK (editable).

## Known limitations (deliberate MVP cuts)

- Public-RPC discovery is slow by design (rate ceilings); Helius key is
  the upgrade path.
- Sells close fully rather than proportionally (trader's remaining
  holdings are unknown without extra RPC spend).
- Trader stats freeze at admission; no ongoing re-scoring yet.
- ATR price sampling only runs while positions are open (stop warms up
  during the first ~15 minutes of a position).
- Live executor confirms on chain and records receipts; signing is
  unit-tested, but no real mainnet swap has been sent yet (dress
  rehearsal on a throwaway wallet still pending, see [[Tasks]]).
