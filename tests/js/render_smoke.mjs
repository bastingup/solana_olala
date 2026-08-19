/**
 * Runs the scan-banner render functions against a stub DOM.
 *
 * This exists because of a real bug: an edit left `fill.parentElement`
 * referring to a `const fill` that had been removed, so every render
 * threw a ReferenceError. The bar kept working (it is updated first),
 * while the gear line and source chips below it silently stopped
 * updating — the panels vanished with nothing in the console anyone was
 * watching. `node --check` passes such code happily, because it is a
 * runtime error, not a syntax one.
 *
 * No npm dependency: this frontend vendors its libraries on purpose, so
 * the DOM is stubbed to exactly what these functions touch.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const appPath = path.resolve(here, "../../frontend/js/app.js");
const src = fs.readFileSync(appPath, "utf8");

// -- extract the functions under test --------------------------------------

/** Lift one top-level `function X(...) {}` or `const X = {...}` out of
 *  app.js by name. Missing names throw, so the harness fails closed when
 *  app.js grows a dependency this file does not know about — better a
 *  loud test failure than a silently narrower check. */
function extract(name) {
  const forms = [`function ${name}(`, `const ${name} = {`];
  const start = forms.map((f) => src.indexOf(f)).find((i) => i >= 0);
  if (start === undefined) {
    throw new Error(`render_smoke: app.js has no top-level ${name}. If it `
      + `was renamed or removed, update DEPENDENCIES in this file.`);
  }
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) {
      // Consts need their terminating semicolon to parse standalone.
      const tail = src[j + 1] === ";" ? j + 2 : j + 1;
      return src.slice(start, tail);
    }
  }
  throw new Error(`render_smoke: unbalanced braces in ${name}`);
}

const DEPENDENCIES = ["SOURCE_STATE_NOTE", "updateTrackBar",
                      "updateTrackingLine", "updateSourceStrip",
                      "pollOutcome", "setFill", "clamp01", "formatCountdown"];
const body = DEPENDENCIES.map(extract).join("\n\n");

// -- the smallest DOM these functions actually touch ------------------------

function makeElement(id) {
  return {
    id,
    style: { width: "", transform: "", transition: "" },
    dataset: {},
    hidden: false,
    _text: "",
    _html: "",
    _attrs: {},
    parentElement: null,
    offsetWidth: 1,
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    set title(v) { this._attrs.title = String(v); },
    get title() { return this._attrs.title || ""; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
  };
}

const elements = new Map();
for (const id of ["track-bar", "track-bar-label", "track-bar-status",
                  "track-bar-fill", "track-line", "source-strip"]) {
  elements.set(id, makeElement(id));
}
// The bar reads `fill.parentElement` to publish aria-valuenow on the rail.
elements.get("track-bar-fill").parentElement = makeElement("track-bar-rail");

const document = {
  getElementById(id) {
    if (!elements.has(id)) {
      throw new Error(`render_smoke: app.js asked for #${id}, which is not `
        + `in index.html (or not in this stub)`);
    }
    return elements.get(id);
  },
};

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const timeAgo = (ts) => {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? `${Math.floor(s)}s` : `${Math.floor(s / 60)}m`;
};

const render = new Function("document", "esc", "timeAgo",
  `${body}\n return { updateTrackBar, updateTrackingLine, updateSourceStrip };`
)(document, esc, timeAgo);

// -- payloads, shaped exactly as TrackingStatus.to_dict() emits -------------

const now = Date.now() / 1000;
const sources = {
  routed: { publicnode: 140 },
  failovers: 0,
  sources: {
    // Carrying the load, standing by, and broken — the three the strip
    // exists to tell apart at a glance.
    publicnode: { state: "active", calls: 140, failures: 0, rate_limited: 0,
                  last_error: "", breaker_open_for_sec: 0,
                  last_latency_ms: 406, supports_batch: true },
    helius: { state: "ready", calls: 2, failures: 0, rate_limited: 0,
              last_error: "", breaker_open_for_sec: 0, metered: true,
              metered_units: 2, last_latency_ms: 115, supports_batch: true },
    mainnet_beta: { state: "down", calls: 0, failures: 3, rate_limited: 2,
                    last_error: "HTTP 429", breaker_open_for_sec: 12,
                    last_latency_ms: 3900, supports_batch: false },
  },
};

const base = {
  roster: 42, configured_interval_sec: 5, achieved_interval_sec: 1.1,
  full_coverage_sec: 42, stream_proven: true, stream_misses: 0,
  signals_emitted: 3, stale_entries_blocked: 0, gaps_detected: 0,
  legacy_rearmed: 0, polls_ok: 10, polls_failed: 0,
  coverage_started_at: now - 12, coverage_complete_at: now + 30,
  last_poll_at: now - 1, last_poll_ok: true,
};

const CASES = {
  round_robin: { ...base, gear: "round_robin", pass_position: 17 },
  batch: { ...base, gear: "batch", pass_position: 42,
           coverage_started_at: now - 2, coverage_complete_at: now + 3,
           last_poll_detail: "42/42 wallets refreshed" },
  partial: { ...base, gear: "batch", pass_position: 38, last_poll_ok: false,
             last_poll_detail: "38/42 wallets refreshed, 4 failed" },
  failed: { ...base, gear: "batch", pass_position: 0, last_poll_ok: false,
            last_poll_detail: "batch sweep failed: every source refused" },
  unproven: { ...base, gear: "batch", stream_proven: false,
              stream_misses: 2, pass_position: 42 },
  empty_roster: { ...base, roster: 0, gear: "batch", pass_position: 0,
                  coverage_complete_at: 0 },
};

// -- run --------------------------------------------------------------------

let failures = 0;
const fail = (msg) => { console.error(`  FAIL ${msg}`); failures++; };

for (const [name, tracking] of Object.entries(CASES)) {
  for (const el of elements.values()) { el._html = ""; el._text = ""; }
  const state = { tracking, sources };

  try {
    render.updateTrackBar(state);
    render.updateTrackingLine(state);
    render.updateSourceStrip(state);
  } catch (err) {
    fail(`${name}: threw ${err.constructor.name}: ${err.message}`);
    continue;
  }

  const bar = elements.get("track-bar");
  const label = elements.get("track-bar-label");
  const status = elements.get("track-bar-status");
  const line = elements.get("track-line");
  const strip = elements.get("source-strip");

  if (tracking.roster === 0) {
    if (!bar.hidden) fail(`${name}: bar should hide with an empty roster`);
    console.log(`  ok  ${name.padEnd(13)} bar hidden`);
    continue;
  }

  // Every panel must render. The bug this file exists for left the two
  // BELOW the bar empty while the bar itself looked fine.
  if (bar.hidden) fail(`${name}: bar is hidden but the roster is populated`);
  if (!label.innerHTML) fail(`${name}: bar label is empty`);
  if (!status.textContent) fail(`${name}: poll status is empty`);
  if (!line.innerHTML) fail(`${name}: gear line is empty`);
  if (!strip.innerHTML) fail(`${name}: source strip is empty`);

  // The gear must be legible, not inferred.
  const gearWord = tracking.gear === "round_robin" ? "ROUND ROBIN" : "BATCH";
  if (!line.innerHTML.includes(gearWord)) {
    fail(`${name}: gear line never says ${gearWord}`);
  }
  // Every configured source gets a chip, including idle ones — an
  // endpoint missing from the strip is indistinguishable from one that
  // is fine, which defeats the point of showing the fall-through chain.
  for (const [source, s] of Object.entries(sources.sources)) {
    if (!strip.innerHTML.includes(source)) {
      fail(`${name}: source strip omits ${source}`);
    }
    // The state must be carried to CSS, or every chip renders identically.
    if (!strip.innerHTML.includes(`data-state="${s.state}"`)) {
      fail(`${name}: ${source} chip never reports state=${s.state}`);
    }
  }
  if (!tracking.last_poll_ok && status.dataset.state === "ok") {
    fail(`${name}: a failed pull is reported as ok`);
  }

  console.log(`  ok  ${name.padEnd(13)} [${status.dataset.state}] `
    + `${status.textContent}`);
}

if (failures) {
  console.error(`\nrender_smoke: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nrender_smoke: all panels rendered");
