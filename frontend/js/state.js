// Central store: the single client-side truth, fed exclusively by the
// backend stream. Panels and the galaxy subscribe; nothing polls.
// Feed entries are HTML: every interpolated value that originates on-chain
// or upstream (symbols, mints, addresses, error text) MUST be escaped.

import { escapeHtml as esc } from "./format.js";

const FEED_LIMIT = 80;
const CANDIDATE_DISPLAY_LIMIT = 60;

export class Store {
  constructor() {
    this.state = {
      connected: "connecting",
      // Nothing below is KNOWN until the first snapshot lands; until then
      // the UI must not assert any of it as fact (a pre-snapshot guess of
      // "no keystore" told the operator their keystore was missing).
      hydrated: false,
      keystore: { exists: false, locked: true },
      wallets: new Map(),
      traders: new Map(),
      positions: new Map(),
      tokens: new Map(),
      watchTokens: new Map(),
      scanProgress: new Map(),
      lastScanAt: 0,
      discovery: null,
      tracking: null,
      sources: null,
      feed: [],
      receipts: [],
      solPrice: 0,
      config: null,
    };
    this._listeners = new Set();
  }

  subscribe(listener) {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  _emit(kind, payload) {
    for (const listener of this._listeners) listener(kind, payload, this.state);
  }

  setConnection(status) {
    this.state.connected = status;
    if (status !== "open") {
      this._pushFeed("system", status === "closed"
        ? "Stream lost — reconnecting…" : "Linking to backend…");
    }
    this._emit("connection");
  }

  apply(event) {
    const { type, data, ts } = event;
    const handler = this[`_on_${type}`];
    if (handler) handler.call(this, data, ts || Date.now() / 1000);
    this._rebuildTokens();
    this._emit(type, data);
  }

  // -- event reducers ----------------------------------------------------

  _on_snapshot(data) {
    this.state.hydrated = true;
    this.state.devMode = Boolean(data.dev_mode);
    this.state.keystore = data.keystore;
    this.state.solPrice = data.sol_price_usd || 0;
    this.state.config = data.config || null;
    this.state.discovery = data.discovery || null;
    this.state.tracking = data.tracking || null;
    this.state.sources = data.sources || null;
    this.state.wallets = new Map(data.wallets.map((w) => [w.id, w]));
    this.state.traders = new Map(data.traders.map((t) => [t.address, t]));
    this.state.positions = new Map(data.positions.map((p) => [p.id, p]));
    this.state.receipts = data.receipts || [];
    this.state.watchTokens = new Map();
    this.state.feed = [];
    for (const fill of (data.fills || []).slice(0, 12).reverse()) {
      this._pushFeed("trade",
        `<b>${esc(fill.side.toUpperCase())}</b> ${esc(fill.mint.slice(0, 4))}… ` +
        `for <b>${fill.sol_amount.toFixed(3)} ◎</b>`, fill.executed_at);
    }
    this._pushFeed("system", "Snapshot loaded — stream is live.");
  }

  _on_portfolio_tick(data) {
    this.state.solPrice = data.sol_price_usd || this.state.solPrice;
    for (const wallet of data.wallets) this.state.wallets.set(wallet.id, wallet);
    for (const position of data.positions) {
      this.state.positions.set(position.id, position);
    }
  }

  _on_wallet_update(data) { this.state.wallets.set(data.id, data); }

  _on_wallet_added(data, ts) {
    this.state.wallets.set(data.id, data);
    this._pushFeed("system",
      `Wallet <b>${esc(data.label)}</b> joined the fleet.`, ts);
  }

  _on_position_opened(data, ts) {
    this.state.positions.set(data.id, data);
    this._pushFeed("trade",
      `Opened <b>${esc(data.symbol)}</b> · ${data.sol_invested.toFixed(3)} ◎`, ts);
  }

  _on_position_resized(data, ts) {
    this.state.positions.set(data.id, data);
    this._pushFeed("trade",
      `Re-sized <b>${esc(data.symbol)}</b> to ${data.sol_invested.toFixed(3)} ◎`,
      ts);
  }

  _on_position_closed(data, ts) {
    this.state.positions.set(data.id, data);
    const pnl = data.realized_pnl_sol;
    const word = { trader_exit: "Trader exited", panic_stop: "PANIC STOP",
                   manual: "Manually closed",
                   reassigned: "Liquidated on reassignment",
                 }[data.exit_reason] || "Closed";
    this._pushFeed("trade",
      `${word} <b>${esc(data.symbol)}</b> · ` +
      `${pnl >= 0 ? "+" : ""}${pnl.toFixed(3)} ◎`, ts);
  }

  _on_trader_candidate(data) { this.state.traders.set(data.address, data); }

  _on_candidate_progress(data) {
    this.state.scanProgress.set(data.address, data);
    this.state.lastScanAt = Date.now() / 1000;
    if (data.complete) {
      this._pushFeed("system",
        `Finished reading <b>${esc(data.address.slice(0, 6))}…</b> — ` +
        `${data.trades_found} swaps over ${data.depth_days.toFixed(0)}d`);
    }
  }

  _on_trader_admitted(data, ts) {
    this.state.traders.set(data.address, data);
    const winRate = data.stats ? (data.stats.win_rate * 100).toFixed(0) : "?";
    this._pushFeed("trader",
      `Star ignited: following <b>${esc(data.address.slice(0, 4))}…</b> ` +
      `(${winRate}% win rate)`, ts);
  }

  _on_trader_rejected(data, ts) {
    this.state.traders.set(data.address, data);
    // Rejections are the most informative thing discovery does — the
    // operator should see who was turned away and why.
    this._pushFeed("reject",
      `Turned away <b>${esc(data.address.slice(0, 6))}…</b> — ` +
      `${esc(data.rejection_reason || "did not meet the bar")}`, ts);
  }

  _on_discovery_status(data) {
    this.state.discovery = data;
  }

  _on_tracking_status(data) {
    this.state.tracking = data;
  }

  _on_trader_reassigned(data, ts) {
    this.state.traders.set(data.address, data);
    const wallet = this.state.wallets.get(data.assigned_wallet_id);
    this._pushFeed("trader",
      `<b>${esc(data.address.slice(0, 6))}…</b> now feeds ` +
      `<b>${esc(wallet ? wallet.label : "another wallet")}</b>.`, ts);
  }

  _on_trader_retired(data, ts) {
    this.state.traders.set(data.address, data);
    const why = data.rejection_reason;
    this._pushFeed("trader",
      `Unfollowed <b>${esc(data.address.slice(0, 4))}…</b>`
      + (why ? ` — ${esc(why)}` : ""), ts);
  }

  _on_discovery_scan(data, ts) {
    this._pushFeed("system",
      `<b>${esc(data.source)}</b> delivered ` +
      `${data.new_candidates} new candidate${data.new_candidates === 1 ? "" : "s"}.`,
      ts);
  }

  _on_copy_signal(data, ts) {
    const symbol = data.symbol || `${data.mint.slice(0, 4)}…`;
    this.state.watchTokens.set(data.mint, { symbol, ts });
    this._pushFeed("trader",
      `<b>${esc(data.trader.slice(0, 4))}…</b> ` +
      `${data.side === "buy" ? "bought" : "sold"} ` +
      `<b>${esc(symbol)}</b> (${data.trader_sol_amount.toFixed(2)} ◎)`, ts);
  }

  _on_risk_rejected(data, ts) {
    this._pushFeed("reject", `Declined: ${esc(data.reason)}`, ts);
  }

  _on_execution_error(data, ts) {
    this._pushFeed("reject", `Execution error: ${esc(data.error)}`, ts);
  }

  _on_receipt_recorded(data, ts) {
    // The on-chain audit trail: every live order attempt lands here,
    // confirmed or not, with its transaction signature.
    this.state.receipts.unshift(data);
    if (this.state.receipts.length > 50) this.state.receipts.pop();
    const sig = `${data.signature.slice(0, 6)}…`;
    if (data.status === "confirmed") {
      this._pushFeed("trade",
        `Receipt: <b>${esc(data.side.toUpperCase())}</b> confirmed on ` +
        `chain — ${data.actual_sol.toFixed(4)} ◎ actual · ${esc(sig)}`, ts);
    } else {
      this._pushFeed("reject",
        `Receipt: <b>${esc(data.side.toUpperCase())}</b> ` +
        `${esc(data.status)} — ` +
        `${esc(data.detail || "order did not execute")} · ${esc(sig)}`, ts);
    }
  }

  _on_config_changed(data) { this.state.config = data; }

  _on_keystore_unlocked() {
    this.state.keystore = { exists: true, locked: false };
    this._pushFeed("system", "Keystore unlocked.");
  }

  _on_trade_executed() {
    // Deliberate no-op: the position_opened/resized/closed events that
    // accompany every execution already carry the feed entries and state.
  }

  _on_ping() {}

  // -- derived -----------------------------------------------------------

  _pushFeed(kind, html, ts) {
    this.state.feed.unshift({ kind, html, ts: ts || Date.now() / 1000 });
    if (this.state.feed.length > FEED_LIMIT) this.state.feed.pop();
  }

  _rebuildTokens() {
    const tokens = new Map();
    for (const position of this.state.positions.values()) {
      const entry = tokens.get(position.mint) || {
        mint: position.mint, symbol: position.symbol,
        exposure: 0, pnl: 0, open: false,
      };
      if (position.status === "open") {
        entry.open = true;
        entry.exposure += position.market_value_sol;
        entry.pnl += position.unrealized_pnl_sol;
      } else {
        entry.pnl += position.realized_pnl_sol;
      }
      tokens.set(position.mint, entry);
    }
    // Tokens followed traders have touched, but we hold no position in:
    // they stay on the wall as watch chips.
    for (const [mint, watch] of this.state.watchTokens) {
      if (!tokens.has(mint)) {
        tokens.set(mint, { mint, symbol: watch.symbol, exposure: 0,
                           pnl: 0, open: false, watch: true });
      }
    }
    this.state.tokens = tokens;
  }

  // -- selectors ---------------------------------------------------------

  fleetTotals() {
    let equity = 0;
    let upnl = 0;
    for (const wallet of this.state.wallets.values()) {
      equity += wallet.equity_sol ?? wallet.base_balance ?? 0;
    }
    for (const position of this.state.positions.values()) {
      if (position.status === "open") upnl += position.unrealized_pnl_sol;
    }
    return { equity, upnl };
  }

  displayTraders() {
    const traders = [...this.state.traders.values()];
    const followed = traders.filter((t) => t.status === "followed");
    const candidates = traders
      .filter((t) => t.status === "candidate")
      .sort((a, b) => b.discovered_at - a.discovered_at)
      .slice(0, CANDIDATE_DISPLAY_LIMIT);
    const rejected = traders
      .filter((t) => t.status === "rejected")
      .sort((a, b) => b.discovered_at - a.discovered_at)
      .slice(0, 30);
    return { followed, candidates, rejected };
  }
}
