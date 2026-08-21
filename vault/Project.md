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
| `config.py` | Dataclass config: defaults ← master `config.yaml` ← active profile file ← REST updates (each section persisted back to the file that owns it). **Master** (gitignored, holds secrets): `dev_mode`, `hft`, server, chain, **risk**, paper. **Profiles** (tracked, no secrets): `config.hft.yaml` / `config.slow.yaml` carry **`leaderboard`** (stream A), `filters` + `discovery` (stream B), and `follow`. `hft: true|false` picks the profile at boot. Legacy single-file configs still load. |
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
| `discovery/leaderboard.py` | **STREAM A.** Polls the service board on `filters_solanatracker.interval_sec`, pages until it has enough names, and FOLLOWS them directly. Configured solely by `filters_solanatracker`; `filters_onchain` never applies. The stream runs whenever `chain.solana_tracker_api_key` is set — the key is the only on/off switch. **Clean-active admission (2026-08-20):** beyond the server-side ROI/win-rate floors, the client applies MEASURED payload gates that separate a real trader from a bot/sniper/rug — activity floor (`min_trades_per_day`), bot ceiling (`max_trades_per_day` 60), anti-sniper (`max_tokens_per_day`, distinct tokens/day), never-sell-losers cap (`max_win_rate` 0.97), consistency (`min_profitable_days_ratio`, green-day fraction), real capital (`min_avg_buy_usd`), and recency (`max_last_trade_hours` 48 — the 30d board metric lags current behaviour). **Score is a COMPOSITE** `0.45·win + 0.30·consistency + 0.25·log-volume` (board position only a tie-break), so seats go to the strongest of what survives the gates. |
| `discovery/onchain.py` | **STREAM B.** DEX census + winners' holders + the signature pre-screen. Nobody vetted these wallets, so `filters` is their admission gate. Probe depth is always 1,000 signatures — a shallow sample measures a burst, not a rate. |
| `discovery/roster.py` | Seat competition shared by both streams: claim a free seat or beat the weakest incumbent by `replace_margin`. |
| `chain/solana_tracker.py` | `SolanaTrackerClient`: `/v2/pnl/leaderboard/top`. Pages the board (cursor) until it has enough copyable names, pushes the `leaderboard` section's floors server-side (`sort`, `minTrades`, `minDays`, `minRoi`, `minWinRate`, `excludeArbitrage`, `pnlMode=strict`), and derives `trades_per_day` from the payload because **the API has no maximum-activity filter** — unknown params are silently ignored (probed 2026-08-18). Every failure raises `SolanaTrackerError` so the caller falls through. |
| `discovery/scanner.py` | `TraderDiscoveryDaemon`: orchestrates the two streams and owns what is shared — the per-sweep RPC budget, incremental history reconstruction (`SIGNATURE_BATCH` 500, cursors persisted), the `filters` admission gate for stream-B candidates, and the live `discovery_status` payload. Stream A runs first (free), then stream B **unconditionally**. Deep-scan queue order: winners'-holders multi-hits, then leaderboard rank, then discovery time. **Roster-full never idles discovery** — sweeps keep hunting so a stronger find can evict the weakest seat. |
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
`trade_executed`, `receipt_recorded`, `trader_performance`,
`config_changed`, `keystore_unlocked`, `ping`. Payloads are the domain
objects' `to_dict()`
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

**publicnode keeps only ~2 days of signature history** (MEASURED
2026-08-20: a wallet whose newest transaction was 49h old returned ZERO
signatures and a `null` `getTransaction` there, while mainnet_beta and
Helius both served it; the oldest visible roster signature was ~38h
back). A null tx and an empty signature page are both SUCCESSFUL
responses, so ordinary routing returns them verbatim — a wallet quiet
for two days would go silently invisible on the `tracking` policy.

**Result-value failover (`RpcRouter.call_accept`, 2026-08-20).** A call
may pass an `accept(result)` predicate; a result that fails it is a SOFT
MISS — the router tries the next source and returns the first accepted
result (or the last miss if none does better). A soft miss is not an
error: the source stays healthy and no failover is counted, so a
legitimately-quiet wallet never trips a breaker. Two opt-in flags ride
this: `RoutedProvider.get_transaction(sig, failover_on_null=True)` (the
COPY path, so an aged-tx null escalates instead of wedging) and
`get_signatures(addr, failover_on_empty=True)` (the tracker arming an
otherwise-invisible followed wallet). Both default to False — discovery
hits nulls/empties by the thousand and must stay on the cheap source, or
the firehose moves onto Helius and burns the credit budget. See [[Tasks]].

**Sustained usage matters on a free plan (~1M credits/month). The
operator's HF-pivot ledger (2026-08-18, verified by script — 0.99M/mo):**

| Source | Config | Monthly |
|---|---|---|
| Discovery | 75 calls / 300s sweep | ~648k |
| Follower safety poll | 5 traders @ 90s | ~144k |
| Copy fetches (push-triggered) | 5 × ~500 trades/day | ~150k |
| Token-safety screens | 30-min cache | ~45k |

Push (`TraderSubscriber`) delivers copies in ~2s, so the interval poll
is only a safety net — its interval is the main budget lever. Deep
scans dominate the ledger: an HF candidate (~7–10k tx fetches at the
7d window) reaches a verdict in ~8–12h at this pace. Solana Tracker:
450s polling ≈ 5.8k of the 10k/month free tier.

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

## Measured performance — the second hierarchy (2026-08-21)

Closed positions are RETAINED forever (`apply_close` marks them
`status=closed` with fee-inclusive `realized_pnl_sol`; nothing deletes
them). `PortfolioManager` folds each close into a per-trader
`TraderPerformance` aggregate (realized PnL, closed count, wins),
rebuilt from the DB on restart — a trader's own track record IN OUR
SETUP, across paper AND live and across every seat it ever held.

- **First hierarchy** (roster membership): the leaderboard COMPOSITE
  score (win × consistency × volume × activity) decides who holds a
  seat; a stronger candidate evicts the weakest.
- **Second hierarchy** (which seat): among followed traders, our own
  measured realized PnL decides WHICH WALLET each trades through.
  `AppContext._plan_assignment` orders wallets live-first and deals the
  top PROVEN performers (`MIN_CLOSED_FOR_RANK` = 3 closes) into the
  premium live seats; the unproven start on paper until they earn a
  record. Re-evaluated after EVERY close (`portfolio.on_close`).
- **Safe swaps only** (operator decision): `rebalance_assignments` only
  moves a trader that is FLAT (no open position anywhere) — it never
  liquidates real money to reshuffle. A trader holding a position keeps
  its wallet until it closes out on its own, then gets repositioned.
- **Moon colour** encodes it: `trader_performance` event + snapshot
  field carry the map; the galaxy tints each PROVEN moon dark pink
  (weakest on the roster) → light pastel pink (strongest) by realized
  PnL, and leaves unproven moons the default nebula purple.

**Paper fills model fees.** `PaperExecutor` charges `paper_fills.fee_sol`
(≈ Solana base + priority) on BOTH the buy and the sell, plus spread and
liquidity-derived impact — so measured realized PnL is genuinely
fee-inclusive.

## Push-triggered copying

`TraderSubscriber` (chain/subscriber.py, `websocket-client`) keeps one
WebSocket to the RPC provider (`RpcProvider.ws_endpoint()`) with a
`logsSubscribe` (mentions) per followed trader. The notification already
carries the signature and slot, so it goes straight to
`WalletTracker.note_activity` — no `getSignaturesForAddress` at all.
This is the fast path: detection lands about a second after the block.

**The commitment rule.** A notification reaches us at `confirmed`, and
the node's finalized height runs ~31-32 slots (~12.6s) behind it —
measured. So every read of a pushed signature MUST be made at
`confirmed`; `TRANSACTION_OPTIONS` pins it. Asking at `finalized` (the
node default) returns null for the whole window in which a copy is worth
making, and that is exactly what silenced trading on 2026-08-20.

**Null means "not yet", never "nothing".** `_handle_signature` returns
False when the node cannot serve the body, releasing its claim and
counting `status.unreadable`; the sweep stops there rather than
advancing the watermark over it. Recording an unread signature as
handled retires the trade permanently.

The sweep remains the safety net and the sole owner of the watermark —
push only adds to the processed ledger, because a `confirmed`
notification proves nothing about the gap beneath it. If the socket
drops (or the lib is missing), the system degrades to polling, never to
silence.

## Trading semantics

- Copy BUY → assigned wallet only; re-size if the (wallet, trader, mint)
  position exists (may draw on reserve); otherwise new entry (never
  touches reserve).
- Copy SELL → full close of the copied position (partial-exit mirroring is
  an open task, see [[Tasks]]).
- Exits: `trader_exit`, `panic_stop` (ATR trail), `manual` (REST),
  `reassigned` (see below).
- **Reassignment liquidates** (operator decision 2026-08-17): dragging a
  trader's moon to another wallet first closes EVERY open position copied
  from that trader (`ExitReason.REASSIGNED`); if any close cannot execute
  (e.g. dark live wallet), the API returns 409 and the assignment does
  not change. A moon must never appear to carry another wallet's money —
  the galaxy also refuses to orbit a satellite around a moon whose
  assigned wallet differs from the position's wallet. (Retiring/
  unfollowing is different: there positions stay, guarded by the stop.)
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
