# solana-olala

![Screenshot](./SolOlala.png)

¥€$ !!!! :)

**Solana-olala** is a fully automated, risk-gated copy-trading system for
Solana DEXes. It continuously scans the chain for wallets with long,
profitable, verifiable trading histories, follows the few that survive
strict admission filters, and mirrors their long positions across your
wallets — under hard risk ceilings, with a gaming-style "galaxy" dashboard
where wallets are planets, copied traders are their moons, and every
position is a satellite you can touch.

## Quick start

```bash
./run.sh        # Linux (Fedora)
run.bat         # Windows
```

One script creates the Python venv, installs dependencies, starts the
backend, serves the frontend, and opens http://127.0.0.1:8420.

**The MVP starts in paper mode**: real chain data, real discovery, real
risk math — simulated fills. Nothing signs a transaction until you hold
the orange ARM key with an unlocked keystore and a registered live wallet.

## What it does

- **Discovers traders** by watching the pools of reputable medium/large-cap
  tokens, reconstructing each candidate wallet's swap history from raw
  transactions, and admitting only wallets with ≥90 days of history,
  ≥200 trades, ≥60% win rate, recent activity, and net profitability
  (thresholds configurable).
- **Spreads exposure**: each admitted trader is assigned to one of your
  wallets (least-loaded, randomized tie-break) instead of every wallet
  copying every trader.
- **Caps risk structurally**: capital added to a token never exceeds 1% of
  its existing liquidity; a SOL reserve is held back for re-sizing into
  dips; honeypot heuristics (active mint/freeze authority, holder
  concentration, pool depth, pair age) are disqualifying; an ATR-derived
  trailing panic stop is armed generously by the backend itself.
- **Streams truth**: the dashboard holds a WebSocket to the backend —
  positions and balances are pushed, never polled. REST exists for actions
  (wallets, config, mode) only.

## Stack

Python (Flask + flask-sock) backend, vanilla JS + D3 frontend, SQLite
persistence, encrypted keystore (scrypt + Fernet). Chain access is
public-first (public RPC + DexScreener + Jupiter free tiers, no signups)
and ROUTED: `backend/config.yaml` lists RPC sources and an ordered
fall-through per policy, so a throttled or broken endpoint is stepped
over rather than taking the system down with it. Adding a free Helius
API key enables the push stream and higher-throughput transaction
fetches; the wallet-tracking heartbeat deliberately stays on the public
endpoint, which is the only one that will serve roster-sized batches.

## Documentation

High-level: this README. Everything else lives in the Obsidian vault under
[vault/](vault/) — full technical docs, session instructions for Claude,
and the open-task log. Root [CLAUDE.md](CLAUDE.md) points any Claude Code
session at the vault before it does anything else.

## Honesty section

There is no trading history, no backtest, and no performance record yet.
Paper mode exists to earn that evidence before any real SOL moves.
