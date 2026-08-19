# Tasks — open work log

Keep this current every session: check off what ships, add what you find.
Context in [[Project]]; standing decisions in [[Claude]].

## Done — TRACKING REWORK: measured polling, source router, HFT removed (2026-08-19)

Operator brief: consistent few-second visibility on followed wallets,
fetch separated from sweeping, "wildly intelligent fall-through"
(LEGO bricks — pull one out, the rest still trades), sweep from Solana
Tracker but WATCH on chain, remove HFT mode, no technical debt, then go
bug hunting against an online source.

The plan was rejected once for guessing. **Everything below was measured
against the live endpoints first**, and the measurements changed the
design more than once.

### What was measured (probe scripts, live endpoints)

| Probe | Result |
|---|---|
| publicnode, 50-wallet batch | 50/50 in 334 ms, in request order |
| publicnode, batch ceiling | **100/100 in 670 ms** |
| publicnode, 3s cadence x20 | 16/20 clean — throttles after ~40s |
| publicnode, **5s cadence x18** | **18/18 clean**, median 469 ms, max 7.9 s |
| publicnode, 8s cadence x11 | 11/11 clean, max 561 ms |
| publicnode, single call | 62 ms |
| **Helius, 50-element batch** | **HTTP 429 in 34 ms — refused outright** |
| Helius, 10-element batch | 10/10, in order, 889 ms |
| mainnet-beta, 50-wallet batch | 42/50 per-element 429s, **out of order** |

**The invariant that set the whole design: cost = wallets ÷ interval.**
Public nodes meter by SUB-CALL, so a 50-address batch costs 50, not 1.
16.7/s throttles; 10/s does not.

### What that forced

- [x] **The interval is DERIVED, never hardcoded**:
      `max(min_interval_sec, ceil(roster / max_wallet_calls_per_sec))`.
      42 wallets → 5s; 100 wallets → 10s. Growing the roster widens the
      cadence instead of silently producing 429s.
- [x] **Two gears.** `ROUND_ROBIN` (1 wallet/tick, ~1 call/s — the
      operator's clock, and **10x cheaper**) runs while the push stream
      has PROVEN itself live; `BATCH` (all wallets, derived interval)
      runs at startup, whenever the stream is unproven, and after a gap.
      No batch-capable source → round-robin rather than blindness.
- [x] **Helius is NOT a tracking peer.** It refuses roster-sized batches
      and a round-robin heartbeat there would cost ~2.6M credits/month
      against a 1M allowance. Its jobs: stream, per-trade fetches,
      broadcast/confirm.
- [x] Batch responses matched by JSON-RPC **id, never position** —
      mainnet-beta was observed returning them out of order, and zipping
      by index would attribute one wallet's trades to another.

### Architecture

- [x] `chain/errors.py` — error taxonomy (`SourceRateLimited`,
      `SourceUnavailable`, `SourceRejected`, `SourceIncomplete`,
      `SourceUnsupported`, `SourceDataError`), all subclassing
      `ChainError` so existing handlers keep working. Throttling was
      previously indistinguishable from breakage, which is why nothing
      could route around it.
- [x] `chain/sources/` — ONE config-driven `JsonRpcSource`. No class per
      vendor: they differ only by URL, credential, rate and batching,
      all of which are data.
- [x] `chain/router.py` — ordered fall-through per POLICY (`tracking`,
      `history`, `metadata`, `broadcast`, `confirm`, `stream`), circuit
      breakers, per-(source, method) support map, metrics.
      `SourceRejected` does NOT fail over. A source that cannot grant
      budget in time is SKIPPED, not waited on.
- [x] **Session pinning** — broadcast and confirmation share one source.
      Sent on Helius and confirmed on publicnode, an honest "never seen
      it" `null` was read as "definitively never landed": a TIMEOUT
      receipt written while the swap was actually confirming.
- [x] `provider.py` is now a facade — `RoutedProvider` implements the
      old interface, so ~22 call sites did not change.
- [x] `chain/signature_walk.py` — the never-skip/never-replay walk,
      written ONCE (it existed twice, with different bugs).
- [x] `trading/tracker.py` `WalletTracker` replaces `follower.py`.
- [x] `trading/signals.py` `SignalQueue` — per-trader serialization,
      worker pool. A 1s tick and a 100s confirmation cannot otherwise
      coexist. Overflow drops the oldest BUY, **never a SELL**.
- [x] `chain/subscriber.py` rewritten: uses the signature+slot already in
      the notification (it used to throw them away and spend a call
      rediscovering them), enqueues instead of running handlers on the
      socket thread, keepalive, unsubscribe on unfollow, missing
      `websocket-client` is a visible degraded state.

### Cursor integrity — the replay bug

- [x] **A bare signature cannot be compared.** When the poll window did
      not contain the cursor, the follower treated EVERY entry as fresh
      and re-executed trades it had already copied. Replaced by a
      `(slot, signature)` watermark plus a PERSISTED processed set
      (it was a 500-entry in-memory LRU, so a restart mid-window
      replayed a copy).
- [x] A window that does not reach the watermark pages back; if it still
      cannot, that is `SourceIncomplete` and the cursor **does not
      move**. Losing sight of trades is recoverable; copying them twice
      is not.
- [x] Push never advances the watermark (it arrives at `confirmed` while
      the sweep reconciles below it); it only adds to `processed`.
- [x] Migration: pre-slot cursors that are still visible are upgraded IN
      PLACE (nothing lost — 24-29 of 42 on the live DB); ones no longer
      visible are re-armed with a loud warning that the gap is not
      copied. Counted separately, because one loses trades and one does
      not.

### Money safety

- [x] **Staleness gate** — `block_time` existed and nothing checked it.
      After an outage the backlog would buy into positions the trader
      had already exited. Entries older than `max_signal_age_sec` are
      refused; **exits never are**.
- [x] **Blind policy** (operator's choice), and PER TRADER — the real
      failure is one wallet dropping out of view while the dashboard
      stays green. Blocks entries, never exits.
- [x] **Portfolio no longer holds its lock across a chain read.**
      `snapshot()` and `exposure()` both called `base_balance()` — a
      blocking RPC for a live wallet — under the lock, so a slow node
      stalled every buy, sell and panic stop. Balances are cached with
      a TTL; sizing reads fresh, outside the lock.
- [x] `getMultipleAccounts` pages instead of silently dropping every
      holder past the hundredth.

### Bug hunt — cross-checked against Solana Tracker

- [x] **Dollar-quoted swaps were completely invisible.** Solana Tracker
      reported 12 trades on a followed wallet where we reconstructed
      ZERO: the reconstructor required the counter-asset to be SOL, so
      every USDC/USDT-denominated swap was dropped. Missing such an
      ENTRY is a dead seat; missing such an **EXIT means holding a token
      the trader already sold**. Now recognised (verified on a live
      wallet trading entirely in USDT: 0 → 7 swaps, 4 of them exits).
      They are deliberately EXCLUDED from scoring — they have no SOL
      price, and inventing an exchange rate would distort every win rate.
- [x] Malformed `Retry-After` raised a bare `ValueError` out of the 429
      path (Python 3.13 changed `parsedate_to_datetime` to raise).
- [x] The first batch sweep could never run: a 42-cost reservation was
      judged by the 1.5s timeout meant for single calls. The batch budget
      wait is now derived from cost ÷ rate.
- [x] `sources.*.api_key` would have shipped the live Helius key to every
      connected browser through the snapshot. Redaction is now recursive
      over the whole config, by key SHAPE, not a hand-maintained list.

### Consolidation

- [x] `chain/http.py` — one HTTP client. `jupiter`, `market_data` and
      `solana_tracker` were three copies of one skeleton, and
      solana_tracker detected 429s **without ever telling its own
      limiter**, so it kept issuing into a wall.
- [x] `constants.py` — `SOL_MINT`, `LAMPORTS_PER_SOL`, `SECONDS_PER_DAY`
      (duplicated across 3, 2 and 6 modules; SOL_MINT had two names).
- [x] `Daemon` compensates its sleep for tick duration (the period was
      `interval + work`) and re-reads its interval each tick; overruns
      are counted and never stacked.
- [x] DB: WAL + `synchronous=NORMAL`, nestable `transaction()` so a
      position and its fill commit atomically, `schema_version`, and
      declarative added-column migrations.
- [x] Money constants to config: Jupiter slippage, `min_order_sol`,
      `max_position_equity_multiple`, paper fee/spread/impact.
- [x] Dead code: `TX_CALLS_PER_CANDIDATE`, `PRESCREEN_PROBE`, unused
      imports, 11 function-local `import time`, `RpcBudget` used as an
      unimported annotation (moved to `discovery/budget.py` — the
      annotation was silently invalid).

### Removed

- [x] **HFT mode, entirely.** `config.hft.yaml`, `config.slow.yaml`, the
      `hft` flag, all profile machinery, the frontend HFT badge, and
      `test_config_profiles.py`. One `config.yaml`, which the process
      now treats as READ ONLY — runtime edits go to `config.runtime.yaml`,
      because one `PUT /api/config` used to `yaml.safe_dump` the whole
      file and erase every comment in it.
- [x] **The DEX census.** It spent a fixed slice of every scan budget to
      enumerate wallets that are merely ACTIVE — the one property a copy
      trader can find anywhere. Winners' holders selects on having been
      early into something that worked.

### Verification

- 386 tests green, pyflakes clean. New: `test_http_client`,
  `test_sources`, `test_router`, `test_cursor`, `test_tracker`,
  `test_signal_queue`, `test_subscriber`, `test_health_degraded`,
  `test_config_single`. H1/H2 (never skip, never replay) ported from the
  follower and still green through batching, gears and routing.
- Live: 42-wallet batch sweeps against publicnode — 0 errors, 0 429s,
  0 gaps; watermarks armed with real slots; legacy cursors upgraded in
  place with nothing lost.

## Done — refresh bar + stream health by evidence (2026-08-19)

Operator asked for a loading bar showing when followed wallets are next
pulled and whether the last pull worked. Building it surfaced a design
bug in the gear switching.

- [x] **Coverage window in `TrackingStatus`** (`coverage_started_at` /
      `coverage_complete_at` / `pass_position`) plus poll outcome
      (`last_poll_ok` / `last_poll_detail` / `polls_ok` / `polls_failed`).
      Both gears answer one question — "when will every followed wallet
      have fresh data?" — so the UI shows one bar, not two mental models.
- [x] Round-robin fills by POSITION, not clock: late ticks would
      otherwise let the bar claim wallets were refreshed when they were
      not, which is the one thing it exists to be honest about.
- [x] A partly-failed sweep reports PARTIAL, never "ok" — calling it ok
      hides exactly the wallets going stale.
- [x] Bugs found while verifying: the batch window opened at sweep START
      so it had already expired by the time a ~4s sweep finished (bar
      pinned at 100%); an unexpected exception left the bar showing the
      last success while the tracker was broken; a gear change showed the
      old gear's countdown for a frame; `_open_coverage_window` reset
      `pass_position` as a side effect, clobbering what the sweep had
      just reported.
- [x] The bar SNAPS to zero at the end of a pass instead of easing
      backwards — animating backwards reads as progress being undone.

**Operator observed the tracker flipping round-robin → batch and asked
why.** It was not the bar: `stream_proof_interval_sec` treated 60s of
SILENCE as a dead stream, and a full round-robin pass is 42s, so the two
timers coincided. Silence is not evidence — a quiet market is silent too.

- [x] Replaced the staleness timeout with a real cross-check: the stream
      is distrusted when the POLL finds a trade older than
      `tracking.stream_miss_grace_sec` that the stream never delivered,
      or when the socket drops. Config key
      `stream_proof_interval_sec` → `stream_miss_grace_sec`.
- [x] Only trades the stream was ANSWERABLE for count — after it proved
      itself, and not across a reconnect. Found live: without this, the
      first sweep after any restart blamed the stream for trades from
      while the app was off, pinning it to the expensive gear forever.

## Done — source health made visible (2026-08-19)

- [x] **A render bug hid two panels.** The transform change left
      `fill.parentElement` pointing at a removed `const fill`, so every
      render threw a ReferenceError — the bar (updated first) kept
      working while the gear line and source chips below it silently
      stopped. `node --check` passes such code; only running it finds it.
      `tests/js/render_smoke.mjs` now executes the real render functions
      against a stubbed DOM and asserts every panel fills, wired into
      pytest as `test_frontend_render.py`. Verified it catches the exact
      bug when reintroduced. No npm dependency — this frontend vendors
      its libraries on purpose.
- [x] **Idle sources are health-probed** (`chain/health.py`). Routing
      only ever contacts the source it routes to, so a standby collected
      no evidence at all — leaving "we have not needed it" and "it is
      dead" indistinguishable, which is exactly the failure the
      fall-through ordering exists to survive. `getHealth` every 30s,
      skipped for anything that served real traffic recently; metered
      sources 10x less often, because a credit spent proving Helius is
      alive is a credit not spent fetching a trade.
- [x] Per-source `state` computed in the router (`off` / `active` /
      `ready` / `down` / `unknown`) so the UI never has to guess.
      "Active" counts ROUTED traffic only — a probe answer was briefly
      making an idle standby look like it was carrying the load for ten
      seconds out of every thirty.
- [x] Chips render the state: filled cyan = serving, cyan outline =
      answering and standing by, rose = not answering, dim = never asked
      or disabled. Operator asked for blue/green/red; the locked palette
      has neither a blue nor a green, so the two HEALTHY states share one
      hue at two weights instead — at chip size "which of these two blues
      is the good one" is a question nobody should have to answer.
      Worth revisiting with them if they want literal hues added to
      DESIGN.md.

## Open — from this rework

- [ ] `TraderProfile` is still mutable and handed out live by the
      registry; callers mutate it outside the lock while Flask threads
      serialize it. Freeze it + copy-on-write registry with
      compare-and-set. (Planned as phase 10; the portfolio hot-path and
      cursor-write halves shipped, the freeze did not.)
- [ ] Credit meter / budget governor for metered sources: persisted
      monthly usage and refusal of low-priority work when projected to
      exceed the cap. `SourceStats.metered_units` counts it today, but
      nothing acts on the projection.
- [ ] Budget LANES (tracking / discovery / execution draw from separate
      buckets on the same source) — today discovery and tracking share
      one limiter per source, so a heavy scan can still slow tracking.
- [ ] Dollar-quoted trades are copied but not scored. A SOL/USD rate
      would let them be judged too; decide whether that is wanted before
      adding it (it would change every historical win rate).
- [ ] `chain/signature_walk.py` is used by the tracker; the discovery
      scanner still has its own walk. Point it at the shared one.
- [ ] Rotate the Helius and Solana Tracker keys — both sat in plaintext
      in `backend/config.yaml` throughout this work.

## Fixed — win-rate unit trap (2026-08-19)

- [x] **Operator report: "the win rate did not get applied."** Correct,
      and the cause was a unit trap I built into the config. Two
      win-rate settings, two different units:
      `filters_onchain.min_win_rate` is a FRACTION (0.8 = 80%) while
      `filters_solanatracker.min_win_rate_pct` was a PERCENT. The
      operator wrote `0.7` in both, meaning 70% — the tracker read it
      as **0.7%**, which filters nothing.
- [x] Evidence from the DB: 42 followed traders seated in 1.9s with win
      rates down to **20.9%, 21.8%, 24.2%** — sorted by realized PnL,
      so high-earning low-accuracy wallets sailed through. (The roster
      cap itself was fine: `max_followed_traders: 42` was the setting.)
- [x] **Fix: one unit for win rate everywhere — a FRACTION**, converted
      to percent inside the client at the wire boundary. ROI keeps the
      explicit `_pct` suffix (real values are 100%-20000%; fractions
      would be worse). The operator's `0.7` now means exactly what they
      intended.
- [x] **Validation that would have caught it instantly:** a win rate
      outside 0..1 raises at config load with "did you mean 0.55?", and
      an ROI percent between 0 and 1 raises with "did you mean 50?".
      Config errors now fail at boot, not silently at runtime.
- [x] Verified live: nominee win rates went from a 20.9% minimum to a
      **70.0% minimum** (median 90.9%). 232 tests green (3 new pins).
- [ ] **The 42 traders already in the DB were seated under the broken
      filter** and keep their seats (replacement ranks by board
      position, not win rate). Clear `backend/olala.db` for a clean
      roster.

## Done — filter switch inverted + sections renamed (2026-08-18)

- [x] **`dev_mode` now means the OPPOSITE of the convention** (operator
      spec, deliberate): `true` = APPLY every `filters_onchain` gate;
      `false` = IGNORE them. It governs the ON-CHAIN stream only —
      pre-screen, scan depth, admission. Documented loudly in the config
      and in code, because the name reads backwards to a newcomer.
- [x] **Renamed** `filters:` -> `filters_onchain:` and `leaderboard:` ->
      `filters_solanatracker:` (classes `OnChainFilters` /
      `SolanaTrackerFilters`) so a section's name says which stream it
      governs.
- [x] **Two decouplings the inversion forced, both safety-positive:**
      (1) the token safety screen now follows the switch for PAPER
      wallets but is UNCONDITIONAL for live ones — real money is never
      exposed to a honeypot because a filter flag was off;
      (2) the dev-mode live-arming lockout is GONE — its rationale
      ("dev configs relax the safety screens") no longer holds, and
      under the new meaning it would have blocked arming in the STRICT
      mode. Arming is gated by the keystore alone, as it should be.

## Done — post-refactor bug scan (2026-08-18)

Scanned the two new stream modules, the roster, and the daemon:

- [x] **Roster could shrink.** `claim_seat` EVICTS the weakest to make
      room, but `LeaderboardSource._follow` called it BEFORE
      `add_candidate`, so a failed add left a seat evicted and unfilled.
      Reordered: register first, and reject cleanly if the seat is lost.
- [x] **`max_followed_traders: 0` crashed the sweep** — `min()` over an
      empty roster raised ValueError. Guarded.
- [x] **Silent dead seat.** A trader followed while `assign_wallet()`
      returned "" (no wallets registered) occupies a seat and can never
      trade; the engine drops its signals with no explanation. Now logs
      a warning naming the trader.
- [x] **Progress bar lied.** `candidate_progress.target_days` used
      `min_history_days` while `_has_enough_depth` requires
      `max(min_history_days, skill_window_days) * 1.1` — the bar read
      100% while the scan kept running. Target now matches the gate.
- [x] **Fall-through hid a real bug.** A missed rename
      (`config.leaderboard` in leaderboard.py) raised AttributeError,
      which the broad `except Exception` logged as a routine "service
      unavailable". Fall-through stays unconditional, but a non-
      `SolanaTrackerError` now logs a full traceback via
      `logger.exception` — resilient AND diagnosable.
- [x] 229 tests green (4 new regression pins). Live run confirms the
      pipeline end to end: 3/3 seated from the ROI board, 0 RPC.

## Done — two-stream modularization (2026-08-18)

- [x] **Operator decision: the streams are separate, and so are their
      rules.** Stream A (`discovery/leaderboard.py`) takes the service's
      ROI-ranked output as given. Stream B (`discovery/onchain.py`,
      census + winners + pre-screen) does the work itself, and the
      `filters` section is ITS admission gate. `discovery/roster.py`
      holds the seat competition both streams share.
- [x] **Config mirrors the split:** new `leaderboard:` profile section
      (enabled, sort, window_days, min_active_days, min_trades,
      min_roi_pct, min_win_rate_pct, pages, interval_sec,
      max_trades_per_day). All `leaderboard_*` keys, `trust_leaderboard`
      and `enabled` are GONE — stream A follows directly by definition,
      and the API KEY is its only on/off switch (one switch, not two
      that can disagree).
      `filters` no longer leaks into the service request (it was
      sending `filters.min_trades`, coupling the two streams).
- [x] The one limit kept on stream A is `leaderboard.max_trades_per_day`
      — mechanical, not quality: the speed past which we can neither
      copy nor afford a trader. Applied in the client from payload data.
- [x] **Fall-through verified four ways** (parametrised test + live
      run): service raises, no API key, stream disabled, stream
      throttled — on-chain harvest runs every sweep in all four. An
      external service can slow discovery, never stop it.
- [x] scanner.py 754 -> ~380 lines (orchestration + deep scan +
      admission). 224 tests green; stream tests live in
      `tests/test_streams.py`, roster tests in
      `test_leaderboard_replacement.py`.
- [x] Live check: stream A seated 3/3 traders (scores 1.00/0.99/0.98,
      68-553 trades) with zero RPC; both fall-through cases confirmed.

## Done — trust the service + burst-rate bug (2026-08-18)

- [x] **Pre-screen rate bug, found by auditing our own code against the
      API.** The probe fetched only 30 signatures and computed
      `count / span`, which measures the most recent BURST, not the
      sustained pace. Audited 18 real nominees: `2sqG7wVVg` read
      **1,284 sigs/day at 30 signatures vs 42/day at 500** — 30x over,
      rejected as a machine despite the service reporting 18 trades/day.
      Fix: always probe `PRESCREEN_MAX_FETCH` (1,000). Costs the SAME
      single credit — the shallow probe bought nothing.
      (A span guard was tried and removed: 250 signatures in 4 minutes
      IS a machine, and the deep probe alone fixes the burst case.)
- [x] **Operator decision — `trust_leaderboard: true` (both profiles).**
      "If the leaderboard sends us great traders vetted on Solana
      Tracker's side, re-filtering just drops viable traders." Correct:
      the service gates on trade count, active days, ROI, win rate and
      non-arbitrage, then our deep scan re-judged the same wallets with
      a 7-day window and a reconstructor blind to multi-hop swaps.
      Nominees are now FOLLOWED directly — no pre-screen, no deep scan.
      Seat competition and `replace_margin` still apply; stats/score come
      from the service payload (overwritten if we ever scan them).
      **This REVISES the standing "judgment is only our own win rate"
      doctrine — recorded in [[Claude]].**
- [x] Verified on the real production path (real config, real API, real
      registry): **roster filled to 3/3 in ONE sweep, 0 RPC calls
      spent**, traders holding 513-1,719 trades. Previously: hours of
      scanning and zero admissions.
- [x] What still protects real money, unchanged: token safety screen
      (mint/freeze authority, holder concentration, liquidity, pair
      age), risk sizing (1% liquidity cap, reserve, per-position
      ceiling), ATR panic stop, and the copyability trades/day cap
      applied free from the payload. 226 tests green (5 new).

## Done — ROI floor: the fix for "we only get bots" (2026-08-18)

- [x] **Operator's observation was right, cause was elsewhere.** They
      saw real traders on solanatracker.io/leaderboard/pnl but our API
      calls returned 200k+ trade machines, and suspected we filtered on
      the wrong metric. Investigated by probing the live API.
      - NOT `minDays` (relaxing 30→5 moved humanish nominees 1→8 of 100)
      - NOT signatures-vs-trades confusion
      - **It was the RANKING.** `sort=realized` = ranking by absolute
        dollars = ranking by scale. Median wallet on that board: 487,246
        trades at 16,247/day. Only 8/100 at human cadence.
      - `sort=win_percentage` gives 85/100 human cadence but tiny PnL.
- [x] **Fix: push ROI + win-rate floors to the service**
      (`leaderboard_min_roi_pct: 100`, `leaderboard_min_win_rate_pct:
      55`, both PERCENT to match the API and the website's own filter
      UI). ROI is the discriminator — machines earn 20–85% on huge
      volume, real traders earn hundreds-to-thousands of percent.
      `minWinRate` alone is useless (bots score high there too).
      Verified end-to-end through the real client: **100/100 nominees
      under the activity cap** (was 4/100), median 43 trades/day, PnL
      $277k–$1.27M, top names 35–213 trades/day. Also lowered
      `leaderboard_min_active_days` 30→10: on a 30-day board, demanding
      30 active days means "traded every single day", a bot trait.
- [x] 222 tests green (3 new pins: floors reach the service, zero
      disables them, floors present in profile configs).
- [ ] **Tension to watch:** only 39/100 of these good nominees have a
      SERVICE win rate ≥90%, so `filters.min_win_rate: 0.9` still gates
      hard — and our own bag-adjusted number may read lower again.
      If admissions stall, that is the first knob (0.55–0.6 matches the
      quality of what the board now returns).

## Done — fills price at the market (2026-08-18)

- [x] **Measurement-integrity fix, required before trusting any HFT
      paper result:** paper fills were priced from the DexScreener cache
      (`CACHE_TTL_SEC = 45`). On a fast-moving token a 45s-old mark is
      wildly off and biases SYSTEMATICALLY — it can make a losing fast
      strategy look profitable. `get_token_info(mint, max_age=...)` now
      takes a freshness bound: browsing/gating uses 10s,
      `TradingEngine` re-reads with `FILL_PRICE_MAX_AGE_SEC = 1.0`
      immediately before every buy AND every close. A sub-second-old
      mark is reused (no wasted latency); anything older is refetched.
      DexScreener is keyless and NOT metered against Helius, so this
      costs nothing but politeness. 4 pins in `test_fill_pricing.py`.
- [x] **Aggression tuned to what the free tier actually buys.**
      Operator set `max_trades_per_day: 8000`; measured that at 3 seats
      it costs 2.22M credits/month (2.2x over). Set to **2,000** — the
      real ceiling (3 x 2,000 x 2 calls = 12k/day; total 0.99M/mo).
      Discovery trimmed 75->60 calls/sweep and follow poll 90->120s to
      buy that copy volume; safe because at 2,000 trades/day a trader
      emits ~3 signatures per 120s poll, far inside the 30-signature
      window. Max 6,000 copies/day.
- [x] Flags set for the run: `hft: true`, `dev_mode: false` (dev mode
      would bypass the very filters this profile is built on).
- [ ] **WATCH:** `min_win_rate: 0.9` (operator's, targeting the ~98%
      never-sell-losers wallets). Defensible HERE because at a 7d window
      with 7d staleness the bag adjustment is inert, so it reads the raw
      rate — but it is the most likely reason the roster stays empty.
      If nothing is admitted within a few hours, lower it before
      touching anything else. Second suspect: the reconstructor returns
      None for multi-hop routes, so heavy router users may show too few
      round trips for `min_round_trips: 10`.

## Done — master config + trading profiles (2026-08-18)

- [x] **Operator-requested restructure:** one MASTER file plus one file
      per trading style, so switching strategy is a one-line edit and
      never a code change.
      - `backend/config.yaml` (master, gitignored — holds secrets):
        `dev_mode`, `hft`, server, chain, **risk exposure**, paper.
        Risk lives here deliberately: changing strategy must never
        silently change how much money a trade may touch.
      - `backend/config.hft.yaml` / `config.slow.yaml` (TRACKED, no
        secrets — shareable presets): `filters`, `discovery`, `follow`.
      - `hft: true|false` selects the profile at BOOT (like dev_mode;
        it reshapes the discovery pipeline, so changing it needs a
        restart).
- [x] `_save()` splits the write the same way as the read, so REST
      config updates preserve the structure: `risk` → master,
      `filters`/`discovery`/`follow` → the ACTIVE profile file (the
      inactive one is never touched). Pinned by test.
- [x] **The old trap is designed out.** A previous session's
      `config.dev.yaml` + `OLALA_CONFIG` design was reverted because
      "the flag never being in the running config" hid dev mode from
      the UI. Here the flags live in the always-read master, the active
      profile name rides in the WebSocket snapshot (`profile`), the
      boot log names it, and the UI shows an HFT badge. The running
      config cannot silently disagree with the operator's file.
- [x] Legacy single-file configs still load (master's profile sections
      are applied first, profile file overrides them), so nothing
      breaks mid-migration. 216 tests green (8 new, incl. a test that
      loads the two REAL shipped profiles).
- [x] Slow profile designed as the true counterpart, not a copy: 30d
      history and skill window, 40 trades/day cap, 30-min minimum hold,
      win-rate ranking (bag adjustment actually bites at 30d), 8 seats,
      3 leaderboard pages. Ledger ~943k/month.

## Done — HIGH-FREQUENCY PIVOT (operator decision, 2026-08-18)

- [x] **Operator doctrine change:** copy high-frequency traders (up to
      ~500 trades/day). Distorted service win rates accepted; judgment
      = net realized PnL + SHARP over a 7-DAY window (a ~3,500-trade
      sample for a 500/day wallet), with 30-day PERSISTENCE enforced
      free via `leaderboard_min_active_days`. Decisions from
      consultation: Helius free tier, 7d window, 5 seats, leaderboard
      sorted by realized PnL.
- [x] Config rebalance to fit ~1M credits/month (verified by script:
      0.99M): discovery 75 calls/300s (0.65M), follow safety poll 5×90s
      (0.14M), copy fetches ~0.15M, screens ~0.04M. Filters:
      history 7d, max_trades_per_day 600 (pre-screen ceiling 3,000
      sigs/day), hold-time gate off, inactive 24h,
      signatures_per_trader 12,000, winners_per_scan 3, tracker poll
      450s (~5.8k of 10k tier).
- [x] Code: `discovery.leaderboard_sort` (win_percentage|realized|
      trades, validated at load; ConfigStore.update made transactional
      so a rejected patch cannot leave a half-mutated config);
      deep-scan queue ranks by leaderboard POSITION; scanner
      SIGNATURE_BATCH 50→500 (listing ~10× cheaper); follower
      SIGNATURES_PER_POLL 15→30, MAX_TX_FETCHES_PER_TRADER 5→10
      (burst headroom ~20 sigs/s). 204 tests green.
- [x] **Pagination shipped (2026-08-18, follow-up):** the client walks
      up to `discovery.leaderboard_pages` (10) cursor pages per poll,
      keeping only copyable-cadence wallets, until 100 keepers are
      found. Poll hourly = 7.2k of the 10k tracker tier. Live verified:
      100 copyable nominees per poll, top entries $0.8M–$4.6M realized
      PnL/30d at 13–450 trades/day. Partial page failures return what
      was collected; only a first-page failure raises (falls through).
- [x] **Two truths pinned by live probes:** (1) the API silently
      IGNORES unknown params — `maxTrades=500` left 99/100 rows over
      500, so server-side maximum filtering does not exist and
      client-side capping stands; (2) `minDays` cannot exceed the
      board window (`days=7, minDays=30` correctly returns total=0) —
      nomination therefore moved to the 30-DAY board (persistence
      lives there) while our deep scan still judges the last 7 days.
      The client widens the board automatically when
      `min_active_days > window`.
- [ ] **Latency caveat (recorded, unresolved by design):** copies land
      ~2-5s after the trader; an HF wallet's speed edge may not
      transfer. Judge the COPIES' paper PnL over days, not the
      trader's stats. Live HF copying is impossible on free tier
      (confirm-polling alone) and needs a fee budget — paid-tier
      decision for later.

## Done — nomination quality + census de-emphasis (2026-08-18)

- [x] **Measured finding that reframed the problem:** the operator
      believed the leaderboard was serving high-frequency bots. Live
      probe says otherwise — 85/100 of the top-100 are at human cadence,
      and 7/8 sampled pass our pre-screen with 1–11 signatures/day. The
      bots come from the **DEX census**, which by construction samples
      bot-dominated live flow (matches the MVP-era measurement in the
      superseded section below).
- [x] **The API has NO maximum-activity filter** — every server-side
      filter is a minimum (minTrades/minDays/minInvested/minWinRate/
      minRoi/minClosedTokens) plus maxSingleTokenPct + excludeArbitrage.
      So the cap is applied client-side: `trades_per_day` is derived
      from `counts.trades / period.tradingDays` and wallets above
      `filters.max_trades_per_day` are dropped BEFORE any RPC is spent
      (counted as `bots_blocked`). A missing rate never reads as a bot.
- [x] **Our floors pushed to the service:** `minTrades` from
      `filters.min_trades` and `minDays` from the new
      `discovery.leaderboard_min_active_days` (30; the service default
      of 3 nominated week-old wallets that could never clear the history
      gate). Measured effect on the live API: median nominee went from
      74 → 240 trades, wallets under 50 trades 36 → 9, and 90 of 100
      nominees were replaced.
- [x] **Census de-emphasized** (operator config): `census_tx_sample`
      16→8 (frees ~24 RPC calls/sweep for deep scans),
      `census_min_sightings` 2→3.
- [x] **`stale_bag_days` 3.0 → 7.0** — at 3 days a normal swing trade
      scored as a realized loss, which was suppressing nearly every
      admission. 200 tests green (4 new).

## Done — Birdeye removed (2026-08-18)

- [x] Operator decision: Solana Tracker is the only leaderboard service.
      Deleted `chain/birdeye.py`, the `chain.birdeye_api_key` and
      `discovery.gainers_window`/`gainers_limit` config fields, the
      scanner's secondary-source hop, and `birdeye_enabled` from the
      public config. Fall-through is now tracker → winners' holders
      (census always runs); all pins ported to FakeTracker. 197 tests
      green. Do not reintroduce without an operator decision.

## Done — reassignment liquidates copied positions (2026-08-17)

- [x] **Operator-reported bug:** after dragging a trader moon onto a new
      (live) wallet planet, the trader's position satellite followed the
      moon — visually attaching a position to a wallet that does not
      hold it (the satellite orbits the moon since the orbit rework, so
      the moon's wallet reads as the custodian).
- [x] **Operator decision — reassignment liquidates:** `POST /api/
      traders/<addr>/assign` now closes EVERY open position copied from
      that trader (new `ExitReason.REASSIGNED`, feed: "Liquidated on
      reassignment") BEFORE re-anchoring; `position_closed` events are
      published before `trader_reassigned` (order pinned by test). If
      any close cannot execute (dark live wallet, chain failure) the
      call returns 409 and the assignment stays put — no half states.
      Same-wallet drags are no-ops and liquidate nothing.
      `TradingEngine.close_position` now returns success; the manual
      close endpoint 409s honestly instead of always saying ok.
- [x] Galaxy honesty guard: a satellite orbits its trader's moon ONLY
      when `trader.assigned_wallet_id == position.wallet_id`; otherwise
      it orbits its own wallet planet, where custody actually lives.
      197 tests green (3 new).

## Done — live path hardened: confirm → reconstruct → receipt (2026-08-17)

- [x] **M6 closed (was: REQUIRED BEFORE LIVE IS EVER ARMED).**
      `LiveJupiterExecutor` no longer books anything from the quote:
      send → poll `getSignatureStatuses` (searchTransactionHistory) →
      on confirmation fetch the landed tx and reconstruct the ACTUAL
      amounts via `TradeReconstructor` (network fee from meta.fee) →
      only then return the Fill. Landed-but-reverted (`err`) raises;
      unconfirmed past 100s raises — safe because the blockhash expires
      ~60–90s after send, so "timeout" is a definitive no-execution
      (engine aborts the close and the position stays open, correctly).
      Reconstruction fallback: if the landed tx can't be fetched/parsed,
      quote amounts are used and the receipt is flagged.
- [x] **Receipts — the on-chain audit trail.** New `Receipt` domain
      object + `receipts` table (quoted vs actual, fee, slot, blockTime,
      status confirmed/failed/timeout, detail). EVERY live attempt
      records one via `AppContext.record_receipt` (DB +
      `receipt_recorded` event). Snapshot carries `receipts` (50),
      REST `GET /api/receipts`. Frontend: feed lines (confirmed = trade
      tone with actual SOL; failed/timeout = reject tone with detail),
      and the live-wallet inspector shows CHAIN RECEIPTS (last 5, each
      linking to solscan.io/tx/<sig>).
- [x] **Signing mechanics unit-tested for the first time** (half of the
      dress-rehearsal task): a real solders `VersionedTransaction` is
      built, signed via the keystore path, and verified non-default in
      `tests/test_live_receipts.py`. 194 tests green (8 new).

## Done — PnL leaderboard service + automatic roster replacement (2026-08-17)

- [x] **Solana Tracker Data API** as the primary leaderboard feeder
      (`chain/solana_tracker.py`, `chain.solana_tracker_api_key`; free
      tier: 10k req/month, 3 rps, signup at solanatracker.io/data-api).
      `/v2/pnl/leaderboard/top` with days=90 (matches skill window),
      excludeArbitrage, sort by win rate. Verified live: keyless call
      401s and raises the typed error. Birdeye kept as secondary (its
      free tier allows gainers-losers but 30k CU/month is tight).
- [x] **Fall-through chain (operator requirement):** census always runs;
      leaderboards (tracker → birdeye) only when keyed AND due per
      `discovery.leaderboard_interval_sec` (900s ≈ 2.9k req/month);
      ANY service failure — missing key, 429, outage — logs and falls
      through to winners' holders. Failed attempts also start the
      throttle window so a rate-limited service is never hammered.
      Services only NOMINATE (service win rate orders the deep-scan
      queue via `_service_rank`); judgment stays our on-chain scan.
- [x] **Automatic replacement of the weakest followed trader (operator
      requirement):** roster-full no longer idles discovery — sweeps
      continue hunting. A candidate that passes admission while the
      roster is full evicts the lowest-score followed trader iff its
      score clears `discovery.replace_margin` (0.02) above the
      incumbent's; else rejected with the honest "does not beat" reason.
      Eviction mirrors manual unfollow: positions stay with the wallet,
      panic stop keeps protecting them; `trader_retired` carries
      "replaced by <addr>… (score a vs b)" and the feed shows it.
      NOTE: discovery RPC spend no longer stops at roster-full — the
      old "stops when full" line in the budget table is obsolete.
- [x] 186 tests green (10 new: sourcing, fall-through, throttle,
      replace/no-churn margin, roster-full-keeps-sweeping).

## Done — universe mode removed (2026-08-17)

- [x] **Operator decision: no more `mode: paper|live`.** Paper and live
      wallets coexist; a live wallet's own arm switch is the ONLY gate.
      Removed: `AppConfig.mode`, `POST /api/mode`, `mode_changed` event,
      snapshot `mode` field, engine's universe check, the hold-to-arm
      key, and the SAFE/ARMED badge (badge now shows only "…" pre-
      hydration and DEV in dev mode; hidden otherwise). The dev_mode
      live lockout MOVED to per-wallet arming: `POST /api/wallets/<id>/
      arm {armed:true}` returns 400 while dev_mode is on; disarming
      always allowed. Legacy `mode` keys: ignored on config load
      (dropped at next save), 400 on config PUT. Armed-planet glow now
      follows `wallet.armed` alone. 176 tests green; DESIGN.md
      (Power-Button Rule replaces Two-Key, One Peach replaces One
      Orange) and vault docs updated.

## Done — repo moved + satellite orbits (2026-08-17)

- [x] Repo copied to `~/Repositories/solana_olala` (underscore). The
      copied venv broke (venv scripts hardcode absolute paths — never
      copy a venv); rebuilt at `backend/.venv`, 175 tests green. Local
      state (config.yaml, keystore.enc, olala.db) survived the copy.
- [x] **Satellite orbits (operator request):** position satellites no
      longer tether to planet/moon with lines. Each orbits the moon of
      the trader it copies (wallet planet as fallback if the moon left
      the sky) — radius 34px +12 per extra satellite, ~31s per pass via
      the same `_advanceOrbits` clockwork as moons — and drags a comet
      tail: three fading arc segments in the satellite's PnL color,
      anchored to its actual bearing so force nudges never detach it.
      Rendering only; no logic changed. Verified with the fixture-
      replayer screenshot trick (gain + loss satellites both shown).
      DESIGN.md updated (satellite entry, links entry, north star).

## Fixed — Infinity in trader stats killed the frontend (2026-08-16 night)

- [x] **"Frontend does not connect anymore":** root cause was NOT the
      connection. A trader with zero observed trades has
      `TraderStats.inactive_hours == inf`; Python's `json.dumps`
      serializes that as the token `Infinity`, which Python's own parser
      accepts but every browser rejects. One such trader in the roster
      made the ENTIRE WebSocket snapshot unparseable — `JSON.parse`
      threw, ws.js swallowed the error silently, and the page sat on
      "LINKING"/pre-hydration forever while small events (portfolio_tick)
      still applied. REST bodies (`/api/state`, `/api/traders`, the
      assign/unfollow responses) carried the same poison via Flask's
      default JSON provider. Diagnosed end-to-end with an instrumented
      frontend copy in headless Firefox (error trap POSTing to a local
      logger) against the live backend.
- [x] Fix, three layers: (1) `TraderStats.to_dict()` emits `null` for
      non-finite `inactive_hours` (filters still see `inf` internally);
      (2) `events.json_safe()` recursively nulls non-finite floats and
      the stream frames with `allow_nan=False` as a tripwire;
      (3) `StrictJSONProvider` on the Flask app does the same for every
      `jsonify`. Frontend: ws.js now `console.error`s unparseable frames
      instead of dropping them silently. Pinned by three tests in
      `tests/test_audit_regressions.py`. 175 tests green. Verified in
      headless Firefox: full hydration (galaxy, wallets, roster, DEV
      badge) within ~1.2s of load.

## Done — win-rate-first skill measurement (2026-08-16 late)

- [x] Operator doctrine encoded: judgment is ONLY our computed win rate,
      windowed (`skill_window_days: 90`), bag-adjusted (stale unsold
      inventory counts as losses — the "hold 100 losers" wallet
      collapses), with a SHARP consistency gate. History gates use the
      full record. Score = 0.8·adjusted_win_rate + tie-breakers.
- [x] DEX census as primary enumerator: persistent sightings ledger
      across Jupiter/Raydium/Orca flow; promotion at ≥2 sightings.
      Verified live: 19 wallets tallied in sweep one. Winners' holders
      demoted to secondary feeder. Budgets raised in operator config
      (200 calls/sweep @ 180s, 3000-signature scans).
- [x] 158 tests green; UI shows census counters, bag-adjusted win rate,
      SHARP, and stale-bag SOL in the inspector.

## Fixed — pre-hydration state was rendered as fact (2026-08-16 night)

- [x] The frontend announced "No keystore yet" to an operator who HAD a
      keystore and a live wallet. Backend was correct
      (`exists: true, locked: true`); the page was rendering the store's
      pre-snapshot GUESS (`exists: false`) as a claim during the window
      before the WebSocket delivered state. Same cause behind "I didn't
      see the DEV badge" — the badge asserted SAFE before it knew.
- [x] Fix: `store.state.hydrated` (set on first snapshot). Until then
      the keystore panel stays hidden, the mode badge shows a neutral
      "…", and the arm key is disabled. Unknown is never rendered as
      false. Ruled out caching as a factor — assets already serve
      `Cache-Control: no-cache` + ETag.

## Done — field-report fixes + drag reassignment (2026-08-16 night)

- [x] **Phantom-position investigation:** operator saw "position
      satellites" and unchanged equity; DB showed ZERO fills/positions
      ever. Root cause: the win-rate arc was cyan — identical to
      position color — so a high-win-rate moon read as a position
      circling it, and the arc's start-dot as a satellite (including on
      a dark live planet). Trading is NOT corrupted; no signal had
      fired yet. Fix: arc recolored to nebula purple (arc itself kept —
      operator likes it); cyan now belongs exclusively to positions.
- [x] Position satellites are fixed-size (r=7) — value lives in the
      inspector, not the geometry (operator decision).
- [x] **Drag-and-drop reassignment:** grab a followed trader's moon and
      drop it on any planet; planets glow cyan as drop targets;
      `POST /api/traders/<addr>/assign` persists it and publishes
      `trader_reassigned` (feed line + orbit re-anchors). Open
      positions stay with the wallet that opened them.
      [SUPERSEDED 2026-08-17: reassignment now LIQUIDATES the trader's
      copied positions first — see the reassignment entry above.]
- [x] F5/persistence question answered: positions/assignments/balances
      are DB-persisted; the feed is per-session by design; frontend
      refresh never triggers backend sweeps (coincidental timing).
      172 tests green.

## Done — backoff, key redaction, dev mode (2026-08-16 evening)

- [x] **Adaptive rate-limit backoff (AIMD):** a 429 halves the shared
      limiter's issue rate and opens an escalating cooldown
      (Retry-After honored); rate climbs back ~15%/20s while clean.
      One log line per episode instead of a warning storm. Verified
      live: "backing off 1.0s, issue rate now 4.00/s".
- [x] **API keys redacted from all RPC logs** (they were printed in
      full in every warning). `redact()` + test pin.
- [x] **Dev mode, consolidated after operator feedback:** ONE config
      file, one `dev_mode: true` flag in config.yaml. Four bypass
      checks (pre-screen, scan depth→one batch, admission filters,
      token safety screen); risk sizing stays on. Live arming refuses
      while dev_mode is on (config layer + API). Cyan DEV badge +
      disabled arm key in UI. The earlier config.dev.yaml/OLALA_CONFIG
      parallel-file design was reverted as over-engineered — the flag
      never being in the running config was why the DEV badge did not
      appear.

## Fixed — first-run keystore deadlock (2026-08-16 late)

- [x] The unlock panel was gated on `keystore.exists`, so on a fresh
      install it never rendered — and `add_key` requires an unlocked
      keystore, making the FIRST live wallet impossible to register
      ("keystore is locked"). Panel now shows whenever locked, with
      create-vs-unlock copy, and the live-wallet form carries an inline
      passphrase field so unlock+register is one submit. Pinned by
      `test_first_live_wallet_flow_from_empty_keystore`.

## Open — planned next for discovery

- [ ] **Streaming PnL ledger (Strategy B):** subscribe to DEX program
      logs, reconstruct every swap once, maintain incremental per-wallet
      win-rate/PnL ledgers in SQLite. After weeks of running this makes
      the census backfills nearly free and enables continuous re-scoring
      of followed traders. Sampling (every k-th tx) keeps it inside the
      Helius free tier.

## Open — high value

- [ ] **Ongoing trader re-scoring.** Stats freeze at admission. The
      replacement half shipped 2026-08-17 (stronger candidates now evict
      the weakest followed trader automatically); still missing is the
      other half: periodically RE-MEASURE followed traders so a decayed
      trader's score drops and makes them evictable — today they keep
      their admission-day score forever.
- [ ] **Proportional sells.** Trader sells 30% → we currently close 100%.
      Needs the trader's token balance (1 extra RPC call per sell) to
      mirror the fraction.
- [ ] **Live-path dress rehearsal (second half).** Signing is now
      unit-tested against a synthetic VersionedTransaction (2026-08-17),
      and every order confirms on chain with a receipt. Remaining before
      arming real money: one minimum-size real swap on a throwaway
      wallet to shake out Jupiter tx-format/fee surprises end-to-end.
- [x] ~~Wallet-discovery quality pass~~ — superseded by discovery v2
      (2026-08-16 evening): seed harvesting removed (operator decision);
      Birdeye top-PnL leaderboard as primary source with Jupiter-program
      flow sampling as fallback; one-call bot pre-screen (signature-rate
      ceiling) at harvest; copyability admission gates (median hold ≥30m,
      ≤40 trades/day). Verified live: three bots at 4k–216k sigs/day
      rejected at the door, human-cadence wallet admitted and scanned.
      141 tests green. Operator still needs to add
      `chain.birdeye_api_key` to activate the leaderboard source.

## RESOLVED — on-chain-only elite discovery (2026-08-16 evening)

Operator made on-chain-only non-negotiable. Three architectures were
built and MEASURED live; the third works:

1. ~~Random DEX-flow sampling~~ — 0/12 survivors, ~100% bot density.
2. ~~Winners pool-history backtrack~~ — pool signatures are ~98% MEV
   spam; 40/40 deep-page samples unreconstructable; even "calm" winners
   need >12k signatures to reach the pre-run window. Removed.
3. **Winners' holders (SHIPPED):** trending 24h winners (keyless Jupiter
   tokens API, DexScreener search fallback) → `getTokenLargestAccounts`
   → owners via `getMultipleAccounts` → drop off-curve PDAs (vaults/
   lockers) and >10%-of-supply accounts → pre-screen → verification.
   3 RPC calls per winner. Live result: 2 winners mined → 37 smart
   holders → 27 candidates in review, 10 thin wallets rejected at the
   door, in ~6 seconds of sweep. Cross-winner tally prioritizes wallets
   holding size in multiple winners.

Note: `filters.min_win_rate` is still **0.9** in the operator's
config.yaml — with that, nothing will ever be admitted (elite is
55–70%). Flagged twice; operator's call.

## Superseded measurements (same day)

Measured 2026-08-16 against live mainnet (Helius, no Birdeye):

- Jupiter v6 processes ~12.5 tx/s (~1.08M swaps/day). 25 signatures span
  **2 seconds** of chain time — sampling is instant because it is dipping
  into a firehose, never because it is finding good wallets.
- Sampling 12 consecutive Jupiter transactions: **11 were dust/
  unreconstructable** (bot routers, multi-hop arb — the reconstructor
  correctly returns None), 1 was a 259 tx/day bot, **0 survived**. The
  same wallet appeared 5× in 12 consecutive transactions: at any instant
  the flow is dominated by a handful of high-frequency bots.
- Conclusion: random flow sampling can *verify* a trader but cannot
  *find* one. The yield of elite traders is effectively zero.

Options (operator decision):

- [ ] **Birdeye leaderboard** (already built, needs a free key) — inverts
      the problem: start from wallets already ranked by realized PnL.
- [ ] **Winners-backtrack harvest** (not built) — the on-chain method
      that actually targets skill: take tokens that ran up hard over the
      last week (DexScreener price change), paginate their pool history
      back to the early days, and harvest the wallets that bought early.
      Those wallets demonstrated skill by construction. Costs far more
      RPC per candidate than flow sampling, but the yield is real.
- [ ] Note: `filters.min_win_rate` is currently **0.9**. A 90% win rate
      over 200+ trades is effectively nonexistent; with that set, nothing
      will ever be admitted regardless of the sourcing fix.

## Open — operator decision

- [ ] **Free-tier credit budget.** Helius key is in place (provider
      confirmed `helius`, zero rate-limit warnings). A full 10-trader
      roster at the default 12s poll would burn ~2.2M credits/month vs a
      ~1M free tier. Push subscriptions already deliver trades in
      seconds, so raising `follow.poll_interval_sec` to ~45 brings it
      inside the tier at almost no latency cost. Left at 12 pending the
      operator's call — see the budget table in [[Project]].

## Done — discovery observability (2026-08-16 evening)

- [x] `discovery_status` event + console: phase, source, live activity
      line, counters (screened / bots blocked / rejected / following /
      in review / RPC budget), countdown to next sweep. Status retained
      on the daemon and included in the snapshot so cold page loads are
      never blank (this was a real bug — status is emitted at sweep time,
      and sweeps are 5 minutes apart).
- [x] Rejections now post to the live feed with their reason; completed
      history reads post a summary. 144 tests green.

## Open — from the independent audit (2026-08-16, non-blocking for paper)

- [ ] **M3 growth/eviction:** `portfolio_tick` streams every position ever
      (send open + recently closed only); call `atr.forget` when the last
      position in a mint closes; evict `MarketDataService` cache and prune
      galaxy `_memory` / frontend maps.
- [ ] **M4 remainder:** persist discovery `_oldest_seen`/`_newest_seen`
      (currently memory-only; restarts lose depth progress).
- [x] ~~M6 remainder~~ — DONE 2026-08-17: live executor now confirms
      every order on chain before booking (see "live path hardened").
- [ ] **M7:** config PUT lacks shape validation for lists/nested values
      (a string for `seed_mints` becomes a char list); validate types.
- [ ] **M8:** follower worst-case RPC demand (10 traders × 6 calls / 12s)
      exceeds the 2 rps public budget → copy latency in minutes; consider
      per-trader stagger and removing the post-final-attempt backoff sleep
      in `provider._call`.
- [ ] **L1:** scrypt N to 2^17; chmod-before-write; unlock rate limit;
      warn when unlock initializes a fresh keystore.
- [ ] **L2:** daemon intervals read once at construction — config changes
      to intervals need restart (document or fix).
- [ ] **L3:** paper mode with a registered live wallet mixes real balance
      into equity without debiting; exclude live wallets from paper trades
      or display them separately.
- [ ] **L4/L6:** batch the 2-writes-per-position-per-tick; cache live
      `getBalance` lookups.

## Done — live/paper universe round (2026-08-16, afternoon)

- [x] Side-by-side paper/live: per-wallet arm state (DB column, REST
      endpoint, keystore-gated arming, always-allowed disarm), universe
      arm as second switch; dark live wallets hold fire with visible
      rejections.
- [x] Correlation gate: ≤2 live wallets per token (config
      `risk.max_live_wallets_per_token`), paper/resize exempt.
- [x] Galaxy: red live planets (dark/glowing), equity-scaled planet
      sizes, seeded probabilistic decorations (rings, moonlets, bands,
      caps), power button in the wallet inspector, SAFE/ARMED badge.
- [x] Push-triggered copying: logsSubscribe WebSocket subscriber poking
      FollowDaemon.poll_now; interval polling stays as safety net.
- [x] 134 tests green (test_arming.py added).

## Fixed from the audit (same day)

- [x] C1 config-PUT live-mode bypass; C2 XSS via token symbols/attributes;
      H1 dropped trades on bursts; H2 duplicate execution after mid-poll
      RPC failure; H3 double-close minting SOL; H4 discovery cursor
      skipping unfetched history; M1 ATR half-range; M2 per-wallet (now
      fleet-wide) liquidity cap; M5 stale signals at admission; M6 slice
      (KeystoreError crash-loop). All pinned in
      `tests/test_audit_regressions.py`.

## Open — hardening

- [ ] Backend restart replays no galaxy history to new WS clients beyond
      the snapshot — consider persisting the feed's last N events.
- [ ] Windows launcher has not been run on a real Windows box.
- [ ] Config PUT accepts any float; add range validation (e.g.
      reserve_fraction ∈ [0,0.9]) — overlaps audit M7.

## Open — product

- [ ] Config editing UI in the frontend (thresholds are REST-only today).
- [ ] Position history view (closed positions + realized PnL over time).
- [ ] Sound/notification option on panic stop and admission events.

## Done (MVP, 2026-08-16)

- [x] Backend: config, event bus, SQLite, provider abstraction
      (public/Helius), DexScreener, Jupiter, discovery pipeline
      (harvest → reconstruct → score → strict filters → admit), risk
      engine (1% liquidity cap, reserve, per-position ceiling), token
      safety screen, ATR panic stop, paper + dormant live executors,
      portfolio, REST + WebSocket.
- [x] Encrypted keystore (scrypt + Fernet, 0600, keys never leave
      backend).
- [x] Frontend: galaxy command deck (planets/moons/satellites), wallet
      rail, live feed, roster, token chip wall, hold-to-arm key, drawer,
      inspector; Impeccable direction locked by operator (Galaxy Starmap,
      seed 8217fc00); hierarchy corrected per operator (wallets=planets).
- [x] Launch scripts (run.sh / run.bat), vendored D3 + self-hosted fonts,
      README, CLAUDE.md, vault docs, DESIGN.md (Impeccable-documented).
- [x] Full dev test suite at `tests/` — 110 tests (unit, REST, WebSocket,
      frontend assets/contract), all offline, all green
      (`tests/run.sh` / `tests\run.bat`).
- [x] Impeccable finish review passed (ship) after two fix rounds;
      operator-locked Galaxy Starmap direction.
- [x] DESIGN.md + `.impeccable/design.json` refreshed after the live-red
      round (2026-08-16): `--live` #f87171 token, live/armed planet
      gradients, power button, SAFE/ARMED badge, planet decorations,
      wider equity→radius scale; The Two-Key Rule added, One Orange /
      Glow-Is-Meaning / Drawn-Stroke rules amended; sidecar ramps
      completed from shipped CSS values.
