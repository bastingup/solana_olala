# Tasks — open work log

Keep this current every session: check off what ships, add what you find.
Context in [[Project]]; standing decisions in [[Claude]].

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
