// Bootstrap: wires the store, stream, galaxy, panels, and controls.

import { api } from "./api.js";
import { escapeHtml as esc, fmtPct, fmtSigned, fmtSol, shortAddr, timeAgo }
  from "./format.js";
import { Galaxy } from "./galaxy.js";
import { escapeHtml, renderChips, renderCommand, renderFeed, renderRoster,
         renderWallets, showToast } from "./panels.js";
import { Store } from "./state.js";
import { StreamClient } from "./ws.js";

const store = new Store();

const galaxy = new Galaxy(document.getElementById("galaxy"), {
  onInspect: openInspector,
  onHoverMint: (mint, on) => {
    document.querySelectorAll(`.chip[data-mint="${mint}"]`)
      .forEach((chip) => chip.classList.toggle("lit", on));
  },
  onReassign: async (address, wallet) => {
    try {
      await api.assignTrader(address, wallet.id);
      showToast(`${shortAddr(address)} now feeds ${wallet.label}.`);
    } catch (error) {
      showToast(error.message, true);
    }
  },
});

// -- render loop ---------------------------------------------------------

let galaxyTimer = null;

store.subscribe((kind, payload, state) => {
  renderCommand(state, store.fleetTotals());
  renderWallets(state);
  renderFeed(state);
  renderRoster(state, store.displayTraders());
  renderChips(state);
  updateScanBanner(state);
  clearTimeout(galaxyTimer);
  if (kind === "snapshot") {
    galaxy.update(state);
  } else {
    galaxyTimer = setTimeout(() => galaxy.update(state), 200);
  }
});

const PHASE_LABELS = {
  sweep_start: "SWEEPING",
  finding_winners: "HUNTING WINNERS",
  mining_holders: "MINING HOLDERS",
  screening: "SCREENING",
  reading_history: "READING HISTORY",
  sweep_done: "STANDING BY",
  roster_full: "ROSTER FULL",
};

function updateScanBanner(state) {
  const phaseEl = document.getElementById("scan-phase");
  const sourceEl = document.getElementById("scan-source");
  const countdownEl = document.getElementById("scan-countdown");
  const text = document.getElementById("scan-text");
  const counters = document.getElementById("scan-counters");
  const d = state.discovery;

  phaseEl.textContent = d ? (PHASE_LABELS[d.phase] || "DISCOVERY") : "DISCOVERY";
  sourceEl.textContent = d ? d.source : "";

  if (!d) {
    text.textContent = "Waiting for the first sweep…";
    counters.innerHTML = "";
    return;
  }

  // Live activity line: what the scanner is doing at this instant, with
  // the aggregate read progress underneath it.
  let sigs = 0;
  let swaps = 0;
  for (const p of state.scanProgress.values()) {
    sigs += p.signatures_scanned;
    swaps += p.trades_found;
  }
  const readNote = sigs
    ? ` · ${sigs} signatures read, ${swaps} swaps reconstructed`
    : "";
  text.textContent = (d.detail || "Working…") + readNote;

  counters.innerHTML = [
    `<span>winners <b>${d.counters.winners_mined ?? 0}</b></span>`,
    `<span>smart holders <b>${d.counters.smart_holders ?? 0}</b></span>`,
    `<span>screened <b>${d.counters.wallets_screened}</b></span>`,
    `<span class="blocked">bots blocked <b>${d.counters.bots_blocked}</b></span>`,
    `<span>too thin <b>${d.counters.too_thin ?? 0}</b></span>`,
    `<span>rejected <b>${d.counters.rejected}</b></span>`,
    `<span class="admitted">following <b>${d.followed}/${d.roster_target}</b></span>`,
    `<span>in review <b>${d.candidates}</b></span>`,
    d.budget_left !== null && d.budget_left !== undefined
      ? `<span>RPC budget <b>${d.budget_left}</b></span>` : "",
  ].join("");

  countdownEl.textContent = "";
  if (d.next_sweep_at) {
    const left = Math.max(0, d.next_sweep_at - Date.now() / 1000);
    countdownEl.textContent = left > 0
      ? `next sweep in ${Math.floor(left / 60)}:`
        + `${String(Math.floor(left % 60)).padStart(2, "0")}`
      : "sweeping…";
  }

  updateTrackBar(state);
  updateTrackingLine(state);
  updateSourceStrip(state);
}

// How long until every followed wallet has fresh data, and whether the
// last pull worked. The backend reports a coverage WINDOW rather than a
// countdown so this can interpolate smoothly between the 1s status
// messages, and so both gears answer the same question the same way.
function updateTrackBar(state) {
  const bar = document.getElementById("track-bar");
  const t = state.tracking;
  if (!t || !t.roster || !t.coverage_complete_at) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;

  const now = Date.now() / 1000;
  const start = t.coverage_started_at;
  const end = t.coverage_complete_at;
  const remaining = Math.max(end - now, 0);

  // Round-robin fills by POSITION, not by elapsed time. Ticks can run
  // late, and a clock-driven bar would then claim wallets had been
  // refreshed when they had not — the one thing this bar exists to be
  // honest about. A batch sweep refreshes everyone at once, so there is
  // no position to report and time is all there is.
  const done = t.gear === "round_robin"
    ? clamp01(t.pass_position / Math.max(t.roster, 1))
    : clamp01((now - start) / Math.max(end - start, 0.001));

  // Past the window with no new one opened, the pull is in flight or
  // late — either way the honest thing is to stop implying progress.
  const polling = remaining <= 0;

  const fill = document.getElementById("track-bar-fill");
  setFill(fill, done * 100);

  const rail = fill.parentElement;
  rail.setAttribute("aria-valuenow", Math.round(done * 100));

  const label = document.getElementById("track-bar-label");
  if (t.gear === "round_robin") {
    // One wallet per tick: say which wallet of how many, because "next
    // pull in 1s" is true but useless — what matters is when the whole
    // roster has been seen.
    label.innerHTML = `Refreshing wallets <b>${t.pass_position}</b>/`
      + `<b>${t.roster}</b> — all seen in `
      + `<b>${formatCountdown(remaining)}</b>`;
  } else {
    label.innerHTML = `Next pull of all <b>${t.roster}</b> wallets in `
      + `<b>${formatCountdown(remaining)}</b>`;
  }

  const status = document.getElementById("track-bar-status");
  const outcome = pollOutcome(t, polling);
  status.dataset.state = outcome.state;
  status.textContent = outcome.text;
  status.title = outcome.title;
  bar.dataset.state = outcome.state;
}

function pollOutcome(t, polling) {
  if (!t.last_poll_at) {
    return { state: "waiting", text: "waiting for first pull",
             title: "The tracker has not completed a pull yet." };
  }
  const detail = t.last_poll_detail || "";
  const age = Math.max(Date.now() / 1000 - t.last_poll_at, 0);
  const when = `${timeAgo(t.last_poll_at)} ago`;
  if (polling && t.last_poll_ok) {
    return { state: "polling", text: "pulling…",
             title: `Last pull ${when}: ${detail}` };
  }
  if (!t.last_poll_ok) {
    // Partial: some wallets answered, some did not. Calling that "ok"
    // would hide exactly the wallets that are going stale.
    const partial = /\d+\/\d+ wallets refreshed/.test(detail)
      && !detail.startsWith("0/");
    return {
      state: partial ? "partial" : "failed",
      text: partial ? `partial — ${detail}` : "last pull FAILED",
      title: `${detail || "no detail"} (${when})`,
    };
  }
  return {
    state: "ok",
    text: `last pull ok · ${when}`,
    title: `${detail} — ${t.polls_ok} successful, ${t.polls_failed} failed`
      + (age > 30 ? " — this is unusually stale" : ""),
  };
}

// Forward motion glides; the reset at the end of a pass SNAPS. Easing
// the bar backwards reads as progress being undone, when what actually
// happened is that a new pass began at zero.
function setFill(fill, percent) {
  const previous = Number(fill.dataset.pct || 0);
  const scale = (percent / 100).toFixed(4);
  if (percent < previous) {
    fill.style.transition = "none";
    fill.style.transform = `scaleX(${scale})`;
    void fill.offsetWidth;            // commit before easing returns
    fill.style.transition = "";
  } else {
    fill.style.transform = `scaleX(${scale})`;
  }
  fill.dataset.pct = String(percent);
}

function clamp01(value) {
  return Math.max(0, Math.min(1, value));
}

function formatCountdown(seconds) {
  if (seconds >= 60) {
    return `${Math.floor(seconds / 60)}m `
      + `${String(Math.floor(seconds % 60)).padStart(2, "0")}s`;
  }
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
}

// Tracking is the half of the system that decides whether a copy
// happens at all, so it gets its own line rather than being inferred
// from silence.
function updateTrackingLine(state) {
  const el = document.getElementById("track-line");
  const t = state.tracking;
  if (!t || !t.roster) {
    el.innerHTML = "";
    return;
  }
  const gear = t.gear === "round_robin" ? "ROUND ROBIN" : "BATCH";
  const gearNote = t.gear === "round_robin"
    ? "One wallet per tick — the cheap gear, used while the push stream "
      + "keeps proving itself live."
    : "Every wallet in one batched request — used at startup and whenever "
      + "the push stream cannot be trusted.";
  const coverage = t.gear === "round_robin"
    ? `full sweep every <b>${Math.round(t.full_coverage_sec)}s</b>`
    : `every <b>${t.configured_interval_sec}s</b>`;
  const achieved = t.achieved_interval_sec
    ? ` (achieved ${t.achieved_interval_sec.toFixed(1)}s)` : "";
  const parts = [
    `<span class="gear" title="${esc(gearNote)}">${gear}</span>`,
    `<span>watching <b>${t.roster}</b> wallets, ${coverage}${achieved}</span>`,
    `<span>copied <b>${t.signals_emitted}</b></span>`,
  ];
  if (!t.stream_proven) {
    parts.push('<span class="warn" title="The push stream has not proven '
      + 'it delivers, so the sweep stays on the batch gear. A quiet '
      + 'market is not the cause — the stream is judged by trades it '
      + 'fails to deliver, never by silence.">stream unproven</span>');
  }
  if (t.stream_misses) {
    parts.push(`<span class="warn" title="Trades the poll caught that the `
      + `push stream never reported. Each one drops tracking back to the `
      + `batch sweep until the stream delivers again.">`
      + `stream missed <b>${t.stream_misses}</b></span>`);
  }
  if (t.stale_entries_blocked) {
    parts.push(`<span class="warn" title="Entries older than the staleness `
      + `limit are refused: after an outage the backlog would buy into `
      + `positions the trader already exited.">stale entries blocked `
      + `<b>${t.stale_entries_blocked}</b></span>`);
  }
  if (t.gaps_detected) {
    parts.push(`<span class="warn" title="A history gap could not be `
      + `bridged, so the cursor did not advance. Nothing was copied twice.">`
      + `gaps <b>${t.gaps_detected}</b></span>`);
  }
  if (t.legacy_rearmed) {
    parts.push(`<span class="warn" title="Cursors written before slots were `
      + `recorded were re-armed at the newest transaction. Trades in the `
      + `gap were not copied.">re-armed <b>${t.legacy_rearmed}</b></span>`);
  }
  el.innerHTML = parts.join("");
}

// One chip per RPC source, saying which is carrying the load and which
// are standing by ready to take it. A silent fall-through is not
// something an operator should have to infer from a latency graph, and a
// standby endpoint of unknown health is not a fall-through you can
// trust — so idle sources are health-probed rather than left grey.
const SOURCE_STATE_NOTE = {
  active: "Serving traffic right now.",
  ready: "Answered its health check — standing by to take over.",
  down: "Not answering. Traffic routes past it.",
  unknown: "Not contacted yet, so nothing is claimed either way.",
  off: "Configured but disabled — usually a missing credential.",
};

function updateSourceStrip(state) {
  const el = document.getElementById("source-strip");
  const metrics = state.sources;
  if (!metrics || !metrics.sources) {
    el.innerHTML = "";
    return;
  }
  const routed = metrics.routed || {};
  const chips = Object.entries(metrics.sources).map(([name, s]) => {
    const mode = s.state || "unknown";
    const bits = [SOURCE_STATE_NOTE[mode] || mode];
    bits.push(`${routed[name] || 0} calls routed`);
    if (s.last_latency_ms) bits.push(`${Math.round(s.last_latency_ms)}ms`);
    if (s.rate_limited) bits.push(`${s.rate_limited}x 429`);
    if (s.failures) bits.push(`${s.failures} failed`);
    if (s.breaker_open_for_sec > 0) {
      bits.push(`retrying in ${Math.ceil(s.breaker_open_for_sec)}s`);
    }
    if (s.metered) bits.push(`metered: ${s.metered_units} units used`);
    const title = bits.join(" · ")
      + (s.last_error ? ` — ${s.last_error}` : "");
    return `<span class="src" data-state="${esc(mode)}" `
      + `title="${esc(title)}">${esc(name)}</span>`;
  });
  if (metrics.failovers) {
    chips.push('<span class="src" data-state="down" title="Calls that fell '
      + 'through to another source after the preferred one failed.">'
      + `${metrics.failovers} failovers</span>`);
  }
  el.innerHTML = chips.join("");
}

// The countdown must tick even when no events arrive.
setInterval(() => updateScanBanner(store.state), 1000);

// -- inspector -----------------------------------------------------------

const inspector = document.getElementById("inspector");

const CLOSE_ICON = `<svg class="close-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9"/></svg>`;

// The on-chain audit trail for one live wallet: every order attempt with
// its landed amounts and a link to the transaction itself.
function renderReceipts(walletId) {
  const receipts = store.state.receipts
    .filter((r) => r.wallet_id === walletId).slice(0, 5);
  const rows = receipts.length
    ? receipts.map((r) => `<div class="receipt-row ${esc(r.status)}">
        <span class="rc-what">${esc(r.side.toUpperCase())} · ${esc(r.status)}</span>
        <b>${fmtSol(r.status === "confirmed" ? r.actual_sol : r.quoted_sol, 3)}</b>
        <a class="rc-sig" target="_blank" rel="noopener"
           href="https://solscan.io/tx/${encodeURIComponent(r.signature)}"
           title="Open the transaction on Solscan">${esc(r.signature.slice(0, 8))}…</a>
      </div>`).join("")
    : `<div class="receipt-row none">No live orders yet — receipts appear
       here when this wallet signs.</div>`;
  return `<div class="receipt-head">CHAIN RECEIPTS</div>${rows}`;
}

function openInspector(node) {
  const { type, data } = node;
  let html = "";
  if (type === "wallet") {
    html = `<h3>WALLET · ${escapeHtml(data.label)}
        <button class="close-x" aria-label="Close">${CLOSE_ICON}</button></h3>
      <div class="spec-row"><span>Equity</span><b>${fmtSol(data.equity_sol, 3)}</b></div>
      <div class="spec-row"><span>Cash</span><b>${fmtSol(data.cash_sol, 3)}</b></div>
      <div class="spec-row"><span>Reserve held</span><b>${fmtSol(data.reserve_sol, 3)}</b></div>
      <div class="spec-row"><span>In positions</span><b>${fmtSol(data.positions_value_sol, 3)}</b></div>
      <div class="spec-row"><span>Open positions</span><b>${data.open_positions}</b></div>
      <div class="spec-addr">${escapeHtml(data.address)}</div>
      ${data.is_paper ? "" : `<div class="power-row">
        <button class="power-btn ${data.armed ? "on" : ""}"
                data-action="toggle-arm" aria-pressed="${data.armed}"
                aria-label="${data.armed ? "Disarm" : "Arm"} this wallet">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M7.5 5.6a8 8 0 1 0 9 0"/><line x1="12" y1="3" x2="12" y2="11"/>
          </svg>
        </button>
        <span class="power-state ${data.armed ? "on" : ""}">
          ${data.armed ? "ARMED — signs real transactions" : "DARK — holds fire"}</span>
      </div>
      ${renderReceipts(data.id)}`}`;
  } else if (type === "trader" || type === "rejected" || type === "candidate") {
    const stats = data.stats || {};
    html = `<h3>TRADER · ${shortAddr(data.address)}
        <button class="close-x" aria-label="Close">${CLOSE_ICON}</button></h3>
      <div class="spec-row"><span>Status</span><b>${data.status.toUpperCase()}</b></div>
      <div class="spec-row"><span>Win rate (bag-adjusted)</span><b>${fmtPct(stats.adjusted_win_rate ?? stats.win_rate)}</b></div>
      <div class="spec-row"><span>SHARP</span><b>${stats.sharpe !== undefined ? stats.sharpe.toFixed(2) : "—"}</b></div>
      <div class="spec-row"><span>Stale bags</span><b>${stats.open_bags ?? 0}${stats.bag_cost_sol ? ` (${stats.bag_cost_sol.toFixed(2)} ◎ stuck)` : ""}</b></div>
      <div class="spec-row"><span>Trades seen</span><b>${stats.total_trades ?? "—"}</b></div>
      <div class="spec-row"><span>History</span><b>${stats.history_days ? stats.history_days.toFixed(0) + "d" : "—"}</b></div>
      <div class="spec-row"><span>Realized PnL</span><b>${stats.realized_pnl_sol !== undefined ? fmtSigned(stats.realized_pnl_sol, 2) : "—"}</b></div>
      <div class="spec-row"><span>Score</span><b>${(data.score || 0).toFixed(3)}</b></div>
      ${data.rejection_reason ? `<div class="spec-row"><span>Rejected</span><b>${escapeHtml(data.rejection_reason)}</b></div>` : ""}
      <div class="spec-addr">${escapeHtml(data.address)}</div>
      ${data.status === "followed"
        ? '<button class="commit-btn danger-btn" data-action="unfollow">STOP COPYING</button>'
        : ""}`;
  } else if (type === "position") {
    html = `<h3>POSITION · ${escapeHtml(data.symbol)}
        <button class="close-x" aria-label="Close">${CLOSE_ICON}</button></h3>
      <div class="spec-row"><span>Invested</span><b>${fmtSol(data.sol_invested, 3)}</b></div>
      <div class="spec-row"><span>Value now</span><b>${fmtSol(data.market_value_sol, 3)}</b></div>
      <div class="spec-row"><span>Unrealized</span><b>${fmtSigned(data.unrealized_pnl_sol, 3)}</b></div>
      <div class="spec-row"><span>Entry price</span><b>${data.entry_price_sol.toExponential(3)}</b></div>
      <div class="spec-row"><span>Last price</span><b>${data.last_price_sol.toExponential(3)}</b></div>
      <div class="spec-row"><span>Panic stop</span><b>${data.stop_price_sol > 0
        ? data.stop_price_sol.toExponential(3) : "warming up (ATR)"}</b></div>
      <div class="spec-row"><span>Copied from</span><b>${shortAddr(data.trader)}</b></div>
      <button class="commit-btn danger-btn" data-action="close-position">CLOSE POSITION NOW</button>`;
  }
  inspector.innerHTML = html;
  inspector.hidden = false;
  inspector.querySelector(".close-x")
    .addEventListener("click", () => { inspector.hidden = true; });
  const action = inspector.querySelector("[data-action]");
  if (action) {
    action.addEventListener("click", async () => {
      try {
        if (action.dataset.action === "unfollow") {
          await api.unfollowTrader(node.data.address);
          showToast(`Stopped copying ${shortAddr(node.data.address)}.`);
        } else if (action.dataset.action === "toggle-arm") {
          const updated = await api.armWallet(node.data.id, !node.data.armed);
          showToast(updated.armed
            ? `${updated.label} armed — it signs real transactions.`
            : `${updated.label} disarmed — dark planet, holds fire.`);
          openInspector({ ...node, data: updated });
          return;
        } else {
          await api.closePosition(node.data.id);
          showToast(`Close order sent for ${node.data.symbol}.`);
        }
        inspector.hidden = true;
      } catch (error) {
        showToast(error.message, true);
      }
    });
  }
}

document.getElementById("galaxy").addEventListener("click", () => {
  inspector.hidden = true;
});

// -- rail panels open the same inspector the galaxy does ------------------

function traderNodeId(profile) {
  const prefix = { followed: "t", candidate: "c", rejected: "x" }[profile.status];
  return prefix ? `${prefix}:${profile.address}` : null;
}

function wireRail(listId, resolve) {
  const list = document.getElementById(listId);
  const rowOf = (event) => event.target.closest("[data-wallet],[data-trader]");
  list.addEventListener("click", (event) => {
    const row = rowOf(event);
    if (!row) return;
    const node = resolve(row);
    if (node) openInspector(node);
  });
  list.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = rowOf(event);
    if (!row) return;
    event.preventDefault();
    const node = resolve(row);
    if (node) openInspector(node);
  });
  list.addEventListener("mouseover", (event) => {
    const row = rowOf(event);
    const node = row && resolve(row);
    if (node && node.lightId) galaxy.lightId(node.lightId, true);
  });
  list.addEventListener("mouseout", (event) => {
    const row = rowOf(event);
    const node = row && resolve(row);
    if (node && node.lightId) galaxy.lightId(node.lightId, false);
  });
}

wireRail("wallet-list", (row) => {
  const wallet = store.state.wallets.get(row.dataset.wallet);
  return wallet && { type: "wallet", data: wallet, lightId: `w:${wallet.id}` };
});

wireRail("roster", (row) => {
  const profile = store.state.traders.get(row.dataset.trader);
  return profile && { type: "trader", data: profile,
                      lightId: traderNodeId(profile) };
});

// -- narrow-width rail toggles -------------------------------------------

const railToggles = [["toggle-wallets", "show-wallets"],
                     ["toggle-ops", "show-ops"]];

function syncRailToggles() {
  for (const [id, cls] of railToggles) {
    document.getElementById(id).setAttribute(
      "aria-pressed", String(document.body.classList.contains(cls)));
  }
}

for (const [id, cls] of railToggles) {
  document.getElementById(id).addEventListener("click", () => {
    const wasOn = document.body.classList.contains(cls);
    for (const [, other] of railToggles) document.body.classList.remove(other);
    if (!wasOn) document.body.classList.add(cls);
    syncRailToggles();
  });
}

// -- escape closes every layer -------------------------------------------

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  inspector.hidden = true;
  drawer.hidden = true;
  for (const [, cls] of railToggles) document.body.classList.remove(cls);
  syncRailToggles();
});

// -- chip wall interaction ------------------------------------------------

document.getElementById("chips").addEventListener("mouseover", (event) => {
  const chip = event.target.closest(".chip");
  if (chip) galaxy.lightMint(chip.dataset.mint, true);
});
document.getElementById("chips").addEventListener("mouseout", (event) => {
  const chip = event.target.closest(".chip");
  if (chip) galaxy.lightMint(chip.dataset.mint, false);
});

// -- wallet drawer --------------------------------------------------------

const drawer = document.getElementById("drawer");
let drawerTab = "paper";

document.getElementById("add-wallet-btn")
  .addEventListener("click", () => {
    drawer.hidden = false;
    syncDrawerTab();
  });
document.getElementById("drawer-close")
  .addEventListener("click", () => { drawer.hidden = true; });
drawer.addEventListener("click", (event) => {
  if (event.target === drawer) drawer.hidden = true;
});

function syncDrawerTab() {
  document.getElementById("paper-fields").hidden = drawerTab !== "paper";
  document.getElementById("live-fields").hidden = drawerTab !== "live";
  // A locked keystore must be openable from inside the live form —
  // otherwise registering the first live wallet is impossible.
  const locked = store.state.keystore.locked;
  const field = document.getElementById("live-passphrase-field");
  field.hidden = drawerTab !== "live" || !locked;
  if (!field.hidden) {
    document.getElementById("live-passphrase-hint").textContent =
      store.state.keystore.exists
        ? "(unlocks your existing keystore)"
        : "(creates your encrypted keystore — remember it, it cannot be recovered)";
  }
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    drawerTab = tab.dataset.tab;
    for (const t of document.querySelectorAll(".tab")) {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-selected", String(t === tab));
    }
    syncDrawerTab();
  });
}

document.getElementById("wallet-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorEl = document.getElementById("wallet-error");
  errorEl.hidden = true;
  const label = document.getElementById("wallet-label").value.trim() || "Wallet";
  try {
    if (drawerTab === "paper") {
      const sol = parseFloat(document.getElementById("wallet-sol").value) || 10;
      await api.addPaperWallet(label, sol);
    } else {
      const secret = document.getElementById("wallet-secret").value.trim();
      if (!secret) throw new Error("Paste the private key to register a live wallet.");
      if (store.state.keystore.locked) {
        const passInput = document.getElementById("live-passphrase");
        const passphrase = passInput.value;
        if (!passphrase) {
          throw new Error(store.state.keystore.exists
            ? "Enter your keystore passphrase to unlock it."
            : "Choose a passphrase — it encrypts your keys on disk.");
        }
        await api.unlockKeystore(passphrase);
        passInput.value = "";
      }
      await api.addLiveWallet(label, secret);
      document.getElementById("wallet-secret").value = "";
    }
    drawer.hidden = true;
    showToast(`Wallet "${label}" registered.`);
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
});

// -- keystore unlock ------------------------------------------------------

document.getElementById("unlock-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("unlock-pass");
  try {
    await api.unlockKeystore(input.value);
    input.value = "";
    showToast("Keystore unlocked.");
  } catch (error) {
    showToast(error.message, true);
  }
});

// -- go ------------------------------------------------------------------

new StreamClient(store).connect();
