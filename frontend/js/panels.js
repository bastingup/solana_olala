// Panel renderers: command bar, wallet rail, feed, roster, chip wall.
// All pure render-from-state; interaction wiring stays in app.js.

import { clockTime, escapeHtml, fmtPct, fmtSigned, fmtSol, fmtUsd,
         shortAddr, timeAgo } from "./format.js";

export { escapeHtml };

const ICONS = {
  trade: '<svg class="icon" viewBox="0 0 16 16"><path d="M2 5h9m0 0L8.5 2.5M11 5 8.5 7.5M14 11H5m0 0 2.5-2.5M5 11l2.5 2.5"/></svg>',
  reject: '<svg class="icon" viewBox="0 0 16 16"><path d="M8 1.5 14 4v4c0 3.5-2.5 6-6 6.5C4.5 14 2 11.5 2 8V4l6-2.5ZM5.5 5.5l5 5M10.5 5.5l-5 5"/></svg>',
  trader: '<svg class="icon" viewBox="0 0 16 16"><path d="M8 1.5 9.8 5.6l4.4.4-3.3 3 1 4.4L8 11l-3.9 2.4 1-4.4-3.3-3 4.4-.4L8 1.5Z"/></svg>',
  system: '<svg class="icon" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 8 12 4M8 5v3"/></svg>',
};

export function renderCommand(state, totals) {
  const equityEl = document.getElementById("fleet-equity");
  const upnlEl = document.getElementById("fleet-upnl");
  const priceEl = document.getElementById("sol-price");
  equityEl.textContent = fmtSol(totals.equity, 2);
  if (state.solPrice) {
    equityEl.textContent += `  ${fmtUsd(totals.equity * state.solPrice)}`;
  }
  upnlEl.textContent = fmtSigned(totals.upnl);
  upnlEl.className = `readout-value ${totals.upnl > 0 ? "gain"
    : totals.upnl < 0 ? "loss" : ""}`;
  priceEl.textContent = state.solPrice ? fmtUsd(state.solPrice) : "—";

  // The badge only ever warns: "…" while state is unknown, DEV while the
  // relaxed dev gates are on. In normal operation it stays hidden — live
  // risk is read per wallet (armed/dark), not from a global mode.
  const badge = document.getElementById("mode-badge");
  if (!state.hydrated) {
    badge.hidden = false;
    badge.dataset.mode = "unknown";
    badge.textContent = "…";
    badge.title = "Waiting for the backend snapshot.";
  } else if (state.devMode) {
    badge.hidden = false;
    badge.dataset.mode = "dev";
    badge.textContent = "DEV";
    badge.title = "Dev mode: relaxed gates for testing. "
      + "Arming live wallets is locked out.";
  } else {
    badge.hidden = true;
  }

  const link = document.getElementById("link-status");
  link.dataset.state = state.connected;
  document.getElementById("link-label").textContent =
    { open: "STREAM LIVE", connecting: "LINKING", closed: "RELINKING" }[state.connected];

  // The keystore panel appears whenever the keystore is locked — creating
  // one IS the first unlock, so it must be reachable on a fresh install.
  // It stays hidden until the snapshot lands, because before that we do
  // not know whether a keystore exists and must not claim otherwise.
  const note = document.getElementById("keystore-note");
  note.hidden = !state.hydrated || !state.keystore.locked;
  if (!note.hidden) {
    const fresh = !state.keystore.exists;
    note.querySelector("p").textContent = fresh
      ? "No keystore yet. Choose a passphrase — it encrypts your private keys on disk."
      : "Keystore locked. Enter your passphrase to arm live wallets.";
    note.querySelector("button").textContent = fresh ? "CREATE" : "UNLOCK";
    note.querySelector("input").placeholder = fresh
      ? "New keystore passphrase" : "Keystore passphrase";
  }
}

export function renderWallets(state) {
  const list = document.getElementById("wallet-list");
  const items = [...state.wallets.values()].map((wallet) => {
    const equity = wallet.equity_sol ?? 0;
    const cash = wallet.cash_sol ?? 0;
    const reserve = Math.min(wallet.reserve_sol ?? 0, cash);
    const positions = wallet.positions_value_sol ?? 0;
    const free = Math.max(cash - reserve, 0);
    const total = Math.max(equity, 0.0001);
    return `<li class="wallet-card" data-wallet="${wallet.id}" tabindex="0"
      role="button" aria-label="Wallet ${escapeHtml(wallet.label)}">
      <div class="wallet-name-row">
        <span class="wallet-name">${escapeHtml(wallet.label)}</span>
        <span class="wallet-kind ${wallet.is_paper ? "" : "live"}">
          ${wallet.is_paper ? "PAPER"
            : wallet.armed ? "LIVE · ARMED" : "LIVE · DARK"}</span>
      </div>
      <div class="wallet-equity">${fmtSol(equity, 2)}
        ${state.solPrice ? `<span class="wallet-usd">${fmtUsd(equity * state.solPrice)}</span>` : ""}
      </div>
      <div class="fuel-bar" title="cash ${fmtSol(free)} · reserve ${fmtSol(reserve)} · in positions ${fmtSol(positions)}">
        <span class="fuel-cash" style="width:${(free / total) * 100}%"></span>
        <span class="fuel-reserve" style="width:${(reserve / total) * 100}%"></span>
        <span class="fuel-positions" style="width:${(positions / total) * 100}%"></span>
      </div>
      <div class="wallet-meta">
        <span>${wallet.open_positions ?? 0} open</span>
        <span>reserve ${fmtSol(reserve, 1)}</span>
      </div>
    </li>`;
  });
  list.innerHTML = items.join("");
}

export function renderFeed(state) {
  const feed = document.getElementById("feed");
  document.getElementById("feed-count").textContent = state.feed.length;
  feed.innerHTML = state.feed.map((entry) =>
    `<li class="kind-${entry.kind}">
      ${ICONS[entry.kind] || ICONS.system}
      <span class="what">${entry.html}</span>
      <span class="when" title="${clockTime(entry.ts)}">${timeAgo(entry.ts)}</span>
    </li>`).join("");
}

export function renderRoster(state, sets) {
  const roster = document.getElementById("roster");
  document.getElementById("roster-counts").textContent =
    `${sets.followed.length} followed · ${sets.candidates.length} scanning`;

  const rows = [];
  for (const trader of sets.followed) {
    const stats = trader.stats || {};
    rows.push(`<li data-trader="${trader.address}" tabindex="0" role="button">
      <div class="r-row">
        <span class="r-addr">${shortAddr(trader.address, 6)}</span>
        <span class="r-status followed">FOLLOWED</span>
      </div>
      <div class="r-stats">
        <span>win ${fmtPct(stats.adjusted_win_rate ?? stats.win_rate)}</span>
        <span>SHARP ${(stats.sharpe ?? 0).toFixed(2)}</span>
        <span>${stats.total_trades ?? "?"} trades</span>
      </div>
    </li>`);
  }
  for (const trader of sets.candidates.slice(0, 8)) {
    const p = state.scanProgress.get(trader.address);
    const depthPct = p && p.target_days
      ? Math.min(100, (p.depth_days / p.target_days) * 100) : 0;
    rows.push(`<li data-trader="${trader.address}" tabindex="0" role="button">
      <div class="r-row">
        <span class="r-addr">${shortAddr(trader.address, 6)}</span>
        <span class="r-status candidate">${p ? "READING CHAIN" : "QUEUED"}</span>
      </div>
      ${p ? `<div class="r-stats">
        <span>${p.signatures_scanned} sigs</span>
        <span>${p.trades_found} swaps</span>
        <span>${p.depth_days.toFixed(0)}/${p.target_days}d</span>
      </div>
      <div class="scan-bar" role="img"
        aria-label="History read: ${depthPct.toFixed(0)} percent of the required window">
        <span style="transform:scaleX(${(depthPct / 100).toFixed(3)})"></span>
      </div>` : ""}
    </li>`);
  }
  for (const trader of sets.rejected.slice(0, 5)) {
    rows.push(`<li data-trader="${trader.address}" tabindex="0" role="button">
      <div class="r-row">
        <span class="r-addr">${shortAddr(trader.address, 6)}</span>
        <span class="r-status rejected">REJECTED</span>
      </div>
      <div class="r-reason">${escapeHtml(trader.rejection_reason || "")}</div>
    </li>`);
  }
  roster.innerHTML = rows.join("")
    || `<li><div class="r-reason">No traders yet — the discovery daemon is
        harvesting candidates from reputable token pools. Qualification under
        strict filters takes a while on public RPC.</div></li>`;
}

export function renderChips(state) {
  const wall = document.getElementById("chips");
  const rank = (t) => t.open ? 2 : t.watch ? 0 : 1;
  const tokens = [...state.tokens.values()]
    .sort((a, b) => rank(b) - rank(a) || b.exposure - a.exposure);
  if (!tokens.length) {
    wall.innerHTML = `<span class="wall-empty">No tokens yet — chips light
      up as positions open and traders trade.</span>`;
    return;
  }
  wall.innerHTML = tokens.map((token) => {
    const tone = token.watch ? "" : token.pnl >= 0 ? "gain" : "loss";
    const amount = token.watch ? "seen"
      : token.open ? fmtSol(token.exposure, 2) : fmtSigned(token.pnl, 2);
    const label = token.watch ? "traded by a followed trader"
      : token.open ? `open exposure ${fmtSol(token.exposure)}`
      : `closed, realized ${fmtSigned(token.pnl)}`;
    return `<button class="chip ${tone} ${token.open ? "" : "watch"}"
      data-mint="${escapeHtml(token.mint)}"
      aria-label="${escapeHtml(token.symbol)}: ${label}">
      <span class="sym">${escapeHtml(token.symbol)}</span>
      <span class="amt">${amount}</span>
    </button>`;
  }).join("");
}

export function showToast(message, isError = false) {
  const zone = document.getElementById("toast-zone");
  const toast = document.createElement("div");
  toast.className = `toast ${isError ? "err" : ""}`;
  toast.textContent = message;
  zone.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}
