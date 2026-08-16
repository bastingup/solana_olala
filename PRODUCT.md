# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Vanilla JavaScript frontend with D3.js for visualization; Python Flask backend serving both a WebSocket stream and a REST API. React was permitted but explicitly not preferred — vanilla is the standing choice. Two launch scripts, one for Linux (Fedora is the daily driver) and one for Windows, each starting backend and frontend together. Flask must be run with a WebSocket-capable server; if Flask proves unable to carry the streaming requirement, the WebSocket requirement wins and the server choice is revisited.

## Users

A single operator — the project owner — running the system on their own machine. No second audience, no accounts, no multi-tenancy. The operator is technically fluent, comfortable with the chain and with running a local stack, and is watching a live automated system rather than manually placing trades. Their job during a session is to understand what the system is doing with their money right now, and to intervene or adjust when they disagree with it.

## Product Purpose

Solana-olala is a fully automated copy-trading system. It continuously scans the Solana chain for wallets with strong, sustained trading records, follows a filtered subset of them, and mirrors their long positions across the operator's own wallets under explicit risk controls. Success is compounding returns from disciplined day-trading of legitimate tokens — not from catching a lottery ticket. The system exists so that the operator captures the edge of proven traders without watching the chain themselves.

## Positioning

Copy trading with risk management as the primary mechanism rather than an afterthought. Three things distinguish it from the ambient category of copy-trade bots:

- **Exposure is spread by design.** Wallet-to-trader assignment is randomized rather than universal. Instead of ten wallets buying the same token in ten small positions, one wallet takes a single larger position — spreading correlated risk across wallets while paying one transaction fee instead of ten.
- **Liquidity is a hard ceiling, not a warning.** Capital added to a token never exceeds 1% of that token's existing liquidity pool. This is the rule that keeps the system out of the rugpull tier structurally, not by judgment.
- **The trader filter is the product.** Success rate, length of trading history, sustained activity, and the quality of tokens a wallet actually trades gate who gets copied at all. Success rate is the most important single signal.

This is day trading, not degenerate speculation. Medium-market-cap tokens traded by reputable on-chain addresses only. Honeypots, hackable contracts, and shady smart contracts are disqualifying, and finding "the next 100x rugpull" is an explicit non-goal.

## Operating Context

Runs locally on the operator's own machine, bound to localhost. Single user, no authentication, no hosting, no network exposure. Started by running one script; the backend then runs unattended, including daemon-style routines that keep scanning the chain for better traders to follow while the rest of the system trades.

The operator's session is a live-monitoring scene: the frontend holds an open WebSocket to the backend and receives a continuous data stream, so open positions and wallet contents are never fetched on a polling tick. REST exists alongside it for actions — registering a new wallet, changing configuration — not for reading state.

Project documentation lives in an Obsidian vault in the repository, treated as the working memory of the project across sessions: an instruction file for future Claude sessions, a full technical project document, and a running open-task log. A root CLAUDE.md directs any session to read the entire vault before doing anything else. The README stays high-level.

## Capabilities and Constraints

**Trading scope**
- Long positions only. No shorts, no hedged positions, ever.
- 100% DEX. No centralized venues.
- Copy trading exclusively — the system does not originate its own trade ideas. Where math is done, it is risk math.

**Risk controls**
- Position size never exceeds 1% of the target token's existing liquidity pool.
- An ATR-derived panic stop-loss is triggered by the backend itself, set generously — the intent is to follow copied traders closely, not to get shaken out of their drawdowns.
- A bounded reserve of base SOL is held back for re-sizing, so that when copied traders scale into local dips the system can follow without running out of capital.
- Trader admission filters: length of trading history, sustained activity, success rate (the dominant signal), and the market-cap tier and legitimacy of the tokens they trade.
- Token safety screening for honeypots, hackable contracts, and shady contract behavior.

**Trading mode**
- The MVP runs in paper mode: full discovery, filtering, risk math, position tracking, and dashboard operate against real chain data, with simulated fills. Nothing is signed.
- Live execution sits behind an explicit configuration switch, armed only once the operator trusts the system.

**Wallets and keys**
- Multiple Solana wallets, at least one.
- Private keys are entered once through the frontend, encrypted at rest on disk under a passphrase the operator supplies at startup, and decrypted only in backend memory. A key is never returned to the frontend after entry.

**Chain access**
- Public Solana RPC endpoints plus Jupiter's open API. No third-party accounts, no API keys, no signups.
- Consequence to design around, not to discover later: public RPC is heavily rate-limited. The wallet-discovery daemon cannot scan at arbitrary aggressiveness, and scan cadence, backoff, caching, and endpoint rotation are first-class concerns rather than tuning details. If discovery quality proves unacceptable under this ceiling, the provider decision is reopened with the operator — it is not silently worked around.

**Engineering constraints**
- Production-grade code from the first commit. Debug output and throwaway dev code are prohibited.
- Object-oriented with genuine abstraction: an abstract `Wallet` base type with `SolanaWallet` inheriting from it, so `EthereumWallet` or `BitcoinWallet` become additions rather than rewrites. Chain-specific logic stays behind generic interfaces.
- No spaghetti, no hardcoded dependencies. Software-engineering best practices are a stated requirement, not a preference.
- Concurrency is used sparingly and deliberately — daemon-style background routines where genuinely needed (trader discovery), not broad multithreading or multiprocessing.
- Must run on both Fedora and Windows.

**Undecided**
- Capital scale, number of wallets, and per-wallet allocation are not yet established.
- Concrete threshold values for the trader filters (minimum history length, minimum success rate, market-cap band) are not yet set.
- The specific token-safety screening method under public-RPC-only access is not yet settled.

## Brand Commitments

- The product is named **Solana-olala**. The name is a joke and is meant to read as one.
- The interface must feel like a gaming site, not a finance dashboard — explicitly not a generic dashboard.
- Black is the dominant color, purple the secondary. This is binding.
- The UX must be self-explanatory: no guessing, no convoluted or deeply nested interactions.
- The system must surface every token symbol across all wallets and all traders, and make aggregate state graspable through visualization with real informational depth rather than through navigation.

## Evidence on Hand

Nothing yet. The repository contains only the original brief, a README, and empty `backend/`, `frontend/`, and `vault/` directories. There is no trading history, no backtest, no performance record, no user, and no third-party validation. Future work must not fabricate returns, win rates, testimonials, benchmark figures, or claims about capital under management — none exist, and in paper mode none will exist for a while.

## Product Principles

1. **Risk math gates everything.** Liquidity ceilings, contract safety, and trader track record decide whether a trade happens at all. When a filter would block a trade, the system declines the trade — it never loosens the filter to stay busy.
2. **Spread exposure rather than multiply it.** Correlated positions across wallets are the failure mode being engineered against. Fewer, larger, distributed positions beat many identical small ones.
3. **Nothing signs without earning it.** Paper mode is the default state. Real execution is an explicit, reversible decision by the operator, never a default and never implicit.
4. **Streamed truth, never polled.** The operator's view of positions and balances is a live stream from the backend. Staleness is a correctness bug, not a latency inconvenience.
5. **Abstraction ahead of chains.** Solana is the first implementation, not the architecture. Anything chain-specific lives behind a generic type so a second chain is an addition.
6. **Comprehension is a product requirement.** The system's value depends on the operator understanding total exposure across every wallet and trader at a glance. An execution engine the operator cannot read is an unfinished product.
