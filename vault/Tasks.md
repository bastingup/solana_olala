# Tasks — open work log

Keep this current every session: check off what ships, add what you find.
Context in [[Project]]; standing decisions in [[Claude]].

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

- [ ] **Ongoing trader re-scoring.** Stats freeze at admission; a followed
      trader who decays should be auto-retired (win-rate window, drawdown
      guard) and replaced from the candidate pool.
- [ ] **Proportional sells.** Trader sells 30% → we currently close 100%.
      Needs the trader's token balance (1 extra RPC call per sell) to
      mirror the fraction.
- [ ] **Live-path dress rehearsal.** The Jupiter+solders signing code has
      never run against a real transaction. Before anyone arms live mode:
      unit-test signing against a recorded swap transaction, then a
      minimum-size real swap on a throwaway wallet.
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
- [ ] **M6 remainder — REQUIRED BEFORE LIVE IS EVER ARMED:** the live
      executor books fills from the Jupiter quote without confirming the
      transaction landed; add confirmation polling before recording.
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
