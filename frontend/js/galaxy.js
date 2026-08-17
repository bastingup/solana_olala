// The galaxy: one force-directed sky holding every trader, wallet, and
// position. Traders are stars (halo arc = win rate), wallets are ringed
// cores, positions are satellites sized by exposure and colored by PnL.
// Candidates glimmer faint; rejected traders cool to embers with their
// reason on hover. Nodes keep their coordinates across data updates so the
// sky feels persistent, not re-rolled.

import { fmtPct, shortAddr } from "./format.js";

const REDUCED_MOTION =
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Position satellites orbit their trader moon (slowly — a full pass takes
// about half a minute) and drag a fading comet tail along the orbit path.
const SATELLITE_ORBIT_SPEED = 0.0002;   // rad/ms
const SATELLITE_ORBIT_BASE_R = 34;      // px around the moon
const SATELLITE_ORBIT_GAP = 12;         // extra radius per extra satellite
const TRAIL_SEGMENT_SWEEP = 0.5;        // rad per fading tail segment

export class Galaxy {
  constructor(svgElement, { onInspect, onHoverMint, onReassign }) {
    this._svg = d3.select(svgElement);
    this._onInspect = onInspect;
    this._onHoverMint = onHoverMint;
    this._onReassign = onReassign;
    this._memory = new Map();

    const defs = this._svg.append("defs");
    const gradients = {
      "planet-fill": ["#312057", "#181129", "#0b0912"],
      "planet-fill-live": ["#4c1622", "#220b10", "#0e0608"],
      "planet-fill-armed": ["#9f1f35", "#4c1220", "#1c070c"],
    };
    for (const [id, [core, mid, edge]] of Object.entries(gradients)) {
      const gradient = defs.append("radialGradient")
        .attr("id", id).attr("cx", "38%").attr("cy", "32%");
      gradient.append("stop").attr("offset", "0%").attr("stop-color", core);
      gradient.append("stop").attr("offset", "55%").attr("stop-color", mid);
      gradient.append("stop").attr("offset", "100%").attr("stop-color", edge);
    }

    this._root = this._svg.append("g").attr("class", "galaxy-root");
    this._starfield = this._root.append("g");
    this._linkLayer = this._root.append("g");
    this._trailLayer = this._root.append("g");
    this._nodeLayer = this._root.append("g");

    this._svg.call(
      d3.zoom()
        .scaleExtent([0.35, 3])
        .on("zoom", (event) => this._root.attr("transform", event.transform)));

    this._simulation = d3.forceSimulation()
      .force("charge", d3.forceManyBody()
        .strength((d) => d.type === "wallet" ? -120 : -35)
        .distanceMax(220))
      .force("collide", d3.forceCollide().radius((d) => d.r + 6))
      .force("x", d3.forceX((d) => d.tx).strength((d) => d.anchor))
      .force("y", d3.forceY((d) => d.ty).strength((d) => d.anchor))
      .force("link", d3.forceLink().id((d) => d.id)
        .distance((d) => d.distance).strength(0.7))
      .on("tick", () => this._tick());
    if (REDUCED_MOTION) this._simulation.alphaDecay(0.3);

    this._drawStarfield();
    window.addEventListener("resize", () => this._drawStarfield());
    if (!REDUCED_MOTION) {
      d3.timer((elapsed) => this._advanceOrbits(elapsed));
    }
  }

  // Moons genuinely orbit: their anchor angle advances slowly around the
  // planet, and the low-energy simulation carries satellites along. The
  // same clockwork moves position satellites around their trader moon.
  _advanceOrbits(elapsed) {
    const nodes = this._simulation.nodes();
    let moved = false;
    for (const node of nodes) {
      if (node.type === "trader" && node.orbit) {
        const planet = this._memory.get(`w:${node.orbit.walletId}`);
        if (!planet) continue;
        const radius = this._orbitR || 110;
        const angle = node.orbit.base + elapsed * 0.00008;
        node.tx = (planet.fx ?? planet.x) + Math.cos(angle) * radius;
        node.ty = (planet.fy ?? planet.y) + Math.sin(angle) * radius;
        moved = true;
      } else if (node.type === "position" && node.orbitCenter) {
        const center = this._memory.get(node.orbitCenter);
        if (!center) continue;
        const angle = node.orbitBase + elapsed * SATELLITE_ORBIT_SPEED;
        node.tx = (center.fx ?? center.x) + Math.cos(angle) * node.orbitDist;
        node.ty = (center.fy ?? center.y) + Math.sin(angle) * node.orbitDist;
        moved = true;
      }
    }
    if (moved && this._simulation.alpha() < 0.03) {
      this._simulation.alpha(0.04).restart();
    }
  }

  _size() {
    const rect = this._svg.node().getBoundingClientRect();
    return { w: rect.width || 900, h: rect.height || 600 };
  }

  _drawStarfield() {
    const { w, h } = this._size();
    const random = d3.randomLcg(7);
    const stars = d3.range(160).map(() => ({
      x: random() * w, y: random() * h,
      r: random() * 1.1 + 0.2, o: random() * 0.5 + 0.1,
    }));
    this._starfield.selectAll("circle").data(stars).join("circle")
      .attr("cx", (d) => d.x).attr("cy", (d) => d.y).attr("r", (d) => d.r)
      .attr("fill", "#c4b5fd").attr("opacity", (d) => d.o);
  }

  // -- data --------------------------------------------------------------

  update(state) {
    const { w, h } = this._size();
    // Orbit radius shrinks with the stage so moons and labels never cross
    // a neighboring planet on narrow screens.
    this._orbitR = Math.max(70, Math.min(110, w * 0.16));
    const { followed, candidates, rejected } = this._traderSets(state);
    const wallets = [...state.wallets.values()];
    const openPositions = [...state.positions.values()]
      .filter((p) => p.status === "open");

    const nodes = [];
    const links = [];

    // Wallets are the planets: primary bodies spread across the sky.
    const planetAnchor = new Map();
    wallets.forEach((wallet, index) => {
      const spread = (index + 1) / (wallets.length + 1);
      const tx = w * (0.18 + spread * 0.64);
      const ty = h * (index % 2 === 0 ? 0.42 : 0.56);
      planetAnchor.set(wallet.id, { tx, ty });
      const planet = this._node({
        id: `w:${wallet.id}`, type: "wallet", data: wallet,
        // Dwarf planets for thin wallets, giants for heavy ones.
        r: 11 + Math.min(Math.sqrt(wallet.equity_sol || 0) * 3.4, 27),
        tx, ty, anchor: 0.9,
        live: !wallet.is_paper,
        armedGlow: !wallet.is_paper && wallet.armed,
      });
      planet.fx = tx;
      planet.fy = ty;
      nodes.push(planet);
    });

    // Followed traders are moons: each orbits the planet it feeds.
    const moonCount = new Map();
    followed.forEach((trader) => {
      const walletId = trader.assigned_wallet_id;
      const planet = planetAnchor.get(walletId);
      const index = moonCount.get(walletId) || 0;
      moonCount.set(walletId, index + 1);
      const angle = index * 2.4 - Math.PI / 2;
      nodes.push(this._node({
        id: `t:${trader.address}`, type: "trader", data: trader,
        r: 6 + (trader.score || 0) * 6,
        tx: planet ? planet.tx + Math.cos(angle) * this._orbitR : w * 0.5,
        ty: planet ? planet.ty + Math.sin(angle) * this._orbitR : h * 0.15,
        anchor: 0.12,
        orbit: walletId ? { walletId, base: angle } : null,
      }));
      if (walletId && state.wallets.has(walletId)) {
        links.push({ source: `t:${trader.address}`, target: `w:${walletId}`,
                     kind: "assign", distance: this._orbitR });
      }
    });

    // Unproven traders drift at the edges: candidates along the upper
    // band, rejected embers along the lower.
    candidates.forEach((trader, index) => {
      nodes.push(this._node({
        id: `c:${trader.address}`, type: "candidate", data: trader,
        r: 2.6,
        tx: 40 + (index * 97) % Math.max(w - 80, 100),
        ty: 30 + (index * 61) % Math.max(h * 0.14, 40), anchor: 0.3,
      }));
    });

    rejected.forEach((trader, index) => {
      nodes.push(this._node({
        id: `x:${trader.address}`, type: "rejected", data: trader,
        r: 3.2,
        tx: 40 + (index * 131) % Math.max(w - 80, 100),
        ty: h - 30 - (index * 53) % Math.max(h * 0.12, 40), anchor: 0.3,
      }));
    });

    // Positions are satellites in orbit around the moon of the trader
    // they copy (their wallet's planet when that moon has left the sky),
    // stacked outward when one moon carries several.
    const satelliteCount = new Map();
    openPositions.forEach((position) => {
      const centerId =
        state.traders.get(position.trader)?.status === "followed"
          ? `t:${position.trader}` : `w:${position.wallet_id}`;
      const index = satelliteCount.get(centerId) || 0;
      satelliteCount.set(centerId, index + 1);
      const base = index * 2.4 + 0.7;
      const dist = SATELLITE_ORBIT_BASE_R + index * SATELLITE_ORBIT_GAP;
      const center = this._memory.get(centerId);
      nodes.push(this._node({
        id: `p:${position.id}`, type: "position", data: position,
        // Fixed size: value lives in the inspector, not the geometry
        // (operator decision — sized satellites read as other objects).
        r: 7,
        orbitCenter: centerId, orbitBase: base, orbitDist: dist,
        tx: (center ? (center.fx ?? center.tx) : w * 0.5)
          + Math.cos(base) * dist,
        ty: (center ? (center.fy ?? center.ty) : h * 0.5)
          + Math.sin(base) * dist,
        anchor: 0.35,
      }));
    });

    const valid = new Set(nodes.map((n) => n.id));
    this._render(nodes, links.filter(
      (l) => valid.has(l.source) && valid.has(l.target)));
  }

  _traderSets(state) {
    const followed = [];
    const candidates = [];
    const rejected = [];
    for (const trader of state.traders.values()) {
      if (trader.status === "followed") followed.push(trader);
      else if (trader.status === "candidate") candidates.push(trader);
      else if (trader.status === "rejected") rejected.push(trader);
    }
    return {
      followed,
      candidates: candidates.slice(-60),
      rejected: rejected.slice(-30),
    };
  }

  _node(spec) {
    const previous = this._memory.get(spec.id);
    const node = previous
      ? Object.assign(previous, spec)
      : { ...spec, x: spec.tx + (Math.random() - 0.5) * 60,
          y: spec.ty + (Math.random() - 0.5) * 60 };
    this._memory.set(spec.id, node);
    return node;
  }

  // -- rendering ---------------------------------------------------------

  _render(nodes, links) {
    this._links = this._linkLayer.selectAll("line")
      .data(links, (d) => `${d.source.id || d.source}|${d.target.id || d.target}`)
      .join("line")
      .attr("class", (d) => d.kind === "assign" ? "assign-link" : "pos-link");

    // One comet tail per orbiting satellite: three arc segments fading
    // out behind it, colored like the satellite itself.
    this._trails = this._trailLayer.selectAll("g.pos-trail")
      .data(nodes.filter((d) => d.type === "position" && d.orbitCenter),
            (d) => d.id)
      .join((enter) => {
        const group = enter.append("g").attr("class", "pos-trail");
        for (const segment of [3, 2, 1]) {
          group.append("path").attr("class", `trail-${segment}`);
        }
        return group;
      })
      .attr("stroke", (d) => (d.data.unrealized_pnl_sol || 0) >= 0
        ? "var(--gain)" : "var(--loss)");

    const groups = this._nodeLayer.selectAll("g.galaxy-node")
      .data(nodes, (d) => d.id)
      .join(
        (enter) => this._enterNode(enter),
        (update) => update,
        (exit) => exit.transition().duration(REDUCED_MOTION ? 0 : 500)
          .attr("opacity", 0).remove());

    groups.each(function (d) { d3.select(this).call(refreshNode, d); });
    this._nodes = groups;

    // Settle the layout synchronously so the sky is composed on first
    // paint, then keep only a gentle live drift running.
    this._simulation.nodes(nodes);
    this._simulation.force("link").links(links);
    this._simulation.alpha(1);
    for (let i = 0; i < 150; i++) this._simulation.tick();
    this._tick();
    if (!REDUCED_MOTION) this._simulation.alpha(0.08).restart();
  }

  _enterNode(enter) {
    const self = this;
    const group = enter.append("g")
      .attr("class", (d) => `galaxy-node type-${d.type}`)
      .attr("id", (d) => `gn-${cssSafe(d.id)}`)
      .on("click", (event, d) => { event.stopPropagation(); self._onInspect(d); })
      .on("mouseenter", function (event, d) {
        d3.select(this).classed("lit", true);
        if (d.type === "position") self._onHoverMint(d.data.mint, true);
      })
      .on("mouseleave", function (event, d) {
        d3.select(this).classed("lit", false);
        if (d.type === "position") self._onHoverMint(d.data.mint, false);
      });

    group.each(function (d) { buildNode(d3.select(this), d); });
    group.filter((d) => d.type === "trader").call(this._moonDrag());
    return group;
  }

  // -- drag a moon onto another planet to reassign the trader ------------

  _moonDrag() {
    const self = this;
    return d3.drag()
      .on("start", function (event, d) {
        event.sourceEvent.stopPropagation();  // keep zoom/pan out of it
        d.fx = d.x;
        d.fy = d.y;
        d3.select(this).classed("dragging", true);
      })
      .on("drag", (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
        self._simulation.alpha(0.12).restart();
        self._highlightDropTarget(self._planetAt(event.x, event.y, d));
      })
      .on("end", async function (event, d) {
        d3.select(this).classed("dragging", false);
        delete d.fx;
        delete d.fy;
        const planet = self._planetAt(event.x, event.y, d);
        self._highlightDropTarget(null);
        self._simulation.alpha(0.3).restart();
        if (planet && planet.data.id !== d.data.assigned_wallet_id) {
          self._onReassign(d.data.address, planet.data);
        }
      });
  }

  _planetAt(x, y, exclude) {
    for (const node of this._simulation.nodes()) {
      if (node.type !== "wallet" || node === exclude) continue;
      const distance = Math.hypot(node.x - x, node.y - y);
      if (distance <= node.r + 26) return node;
    }
    return null;
  }

  _highlightDropTarget(planet) {
    this._nodeLayer.selectAll("g.type-wallet")
      .classed("drop-target", (d) => planet !== null && d === planet);
  }

  _tick() {
    if (this._links) {
      this._links
        .attr("x1", (d) => d.source.x).attr("y1", (d) => d.source.y)
        .attr("x2", (d) => d.target.x).attr("y2", (d) => d.target.y);
    }
    if (this._nodes) {
      this._nodes.attr("transform", (d) => `translate(${d.x},${d.y})`);
    }
    if (this._trails) {
      const memory = this._memory;
      this._trails.each(function (d) {
        const center = memory.get(d.orbitCenter);
        if (!center) return;
        const cx = center.fx ?? center.x;
        const cy = center.fy ?? center.y;
        // The tail hangs off the satellite's ACTUAL bearing, so it stays
        // glued even when collision forces nudge it off the ideal circle.
        const radius = Math.hypot(d.x - cx, d.y - cy);
        // d3.arc measures angles from 12 o'clock; atan2 from 3 o'clock.
        const head = Math.atan2(d.y - cy, d.x - cx) + Math.PI / 2;
        const group = d3.select(this)
          .attr("transform", `translate(${cx},${cy})`);
        for (let segment = 0; segment < 3; segment++) {
          group.select(`.trail-${segment + 1}`).attr("d", d3.arc()({
            innerRadius: radius, outerRadius: radius,
            startAngle: head - TRAIL_SEGMENT_SWEEP * (segment + 1),
            endAngle: head - TRAIL_SEGMENT_SWEEP * segment,
          }));
        }
      });
    }
  }

  lightMint(mint, on) {
    this._nodeLayer.selectAll("g.type-position")
      .classed("lit", (d) => on && d.data.mint === mint);
  }

  lightId(id, on) {
    this._nodeLayer.select(`#gn-${cssSafe(id)}`).classed("lit", on);
  }
}

// -- node anatomy (module-level: used by enter and refresh) ---------------

function cssSafe(id) { return id.replace(/[^a-zA-Z0-9_-]/g, "_"); }

// Deterministic per-wallet PRNG so decorations are stable across renders
// and sessions — the same wallet always shows the same face.
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hashString(text) {
  let hash = 2166136261;
  for (let i = 0; i < text.length; i++) {
    hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
  }
  return hash >>> 0;
}

// Each decoration rolls its own probability; no two planets are forced to
// match, most differ, some rhyme.
function planetDecorations(walletId) {
  const rng = mulberry32(hashString(walletId));
  const decor = [];
  if (rng() < 0.42) {
    decor.push({ kind: "ring", tilt: -28 + rng() * 56,
                 span: 1.55 + rng() * 0.35, squash: 0.28 + rng() * 0.14 });
    if (rng() < 0.35) {
      decor.push({ kind: "ring", tilt: decor[0].tilt,
                   span: decor[0].span + 0.22 + rng() * 0.12,
                   squash: decor[0].squash, thin: true });
    }
  }
  if (rng() < 0.32) {
    decor.push({ kind: "moonlet", angle: rng() * Math.PI * 2,
                 dist: 1.5 + rng() * 0.3, size: 0.13 + rng() * 0.08 });
  }
  if (rng() < 0.38) {
    decor.push({ kind: "band", offset: -0.25 + rng() * 0.5,
                 arc: 0.5 + rng() * 0.3 });
  }
  if (rng() < 0.25) {
    decor.push({ kind: "cap", top: rng() < 0.5 });
  }
  return decor;
}

const DECOR_BASE_R = 20;

function buildDecorations(group, d) {
  const decor = planetDecorations(d.data.id);
  const layer = group.append("g").attr("class", "planet-decor");
  for (const item of decor) {
    if (item.kind === "ring") {
      layer.append("ellipse")
        .attr("class", `planet-ring${item.thin ? " thin" : ""}`)
        .attr("rx", DECOR_BASE_R * item.span)
        .attr("ry", DECOR_BASE_R * item.span * item.squash)
        .attr("transform", `rotate(${item.tilt})`);
    } else if (item.kind === "moonlet") {
      layer.append("circle")
        .attr("class", "planet-moonlet")
        .attr("cx", Math.cos(item.angle) * DECOR_BASE_R * item.dist)
        .attr("cy", Math.sin(item.angle) * DECOR_BASE_R * item.dist)
        .attr("r", DECOR_BASE_R * item.size);
    } else if (item.kind === "band") {
      const y = DECOR_BASE_R * item.offset;
      const x = DECOR_BASE_R * item.arc;
      layer.append("path")
        .attr("class", "planet-band")
        .attr("d", `M ${-x} ${y} Q 0 ${y + DECOR_BASE_R * 0.14} ${x} ${y}`);
    } else if (item.kind === "cap") {
      layer.append("ellipse")
        .attr("class", "planet-cap")
        .attr("cx", 0)
        .attr("cy", DECOR_BASE_R * 0.62 * (item.top ? -1 : 1))
        .attr("rx", DECOR_BASE_R * 0.42)
        .attr("ry", DECOR_BASE_R * 0.16);
    }
  }
}

function buildNode(group, d) {
  if (d.type === "wallet") {
    group.append("circle").attr("class", "wallet-ring");
    group.append("circle").attr("class", "wallet-core");
    buildDecorations(group, d);
    group.append("text").attr("class", "node-label")
      .attr("text-anchor", "middle");
  } else if (d.type === "trader") {
    group.append("circle").attr("class", "star-halo");
    group.append("circle").attr("class", "star-core");
    group.append("path").attr("class", "star-winrate");
    group.append("text").attr("class", "node-label")
      .attr("text-anchor", "middle");
    group.append("title");
  } else if (d.type === "position") {
    group.append("circle").attr("class", "pos-body");
    group.append("text").attr("class", "pos-label");
    group.append("title");
  } else {
    group.append("circle")
      .attr("class", d.type === "candidate" ? "candidate-node" : "rejected-node");
    group.append("title");
  }
}

function refreshNode(group, d) {
  if (d.type === "wallet") {
    group.classed("live", Boolean(d.live))
      .classed("armed-glow", Boolean(d.armedGlow));
    group.select(".wallet-ring").attr("r", d.r + 6);
    group.select(".wallet-core").attr("r", d.r);
    group.select(".planet-decor")
      .attr("transform", `scale(${d.r / DECOR_BASE_R})`);
    group.select("text")
      .attr("y", d.r + 18)
      .text(d.data.label.toUpperCase()
            + (d.live && !d.data.armed ? " · DARK" : ""));
  } else if (d.type === "trader") {
    const winRate = d.data.stats ? d.data.stats.win_rate : 0;
    group.select(".star-halo").attr("r", d.r + 5);
    group.select(".star-core").attr("r", d.r);
    group.select(".star-winrate")
      .attr("stroke-width", 2.5)
      .attr("d", d3.arc()({
        innerRadius: d.r + 5, outerRadius: d.r + 5,
        startAngle: 0, endAngle: winRate * Math.PI * 2,
      }));
    group.select("text").attr("y", d.r + 18).text(shortAddr(d.data.address));
    group.select("title").text(
      `Trader ${d.data.address}\nwin rate ${fmtPct(winRate)} · score ` +
      `${(d.data.score || 0).toFixed(2)}\nclick to inspect`);
  } else if (d.type === "position") {
    const gain = (d.data.unrealized_pnl_sol || 0) >= 0;
    group.select(".pos-body")
      .attr("r", d.r)
      .attr("fill", gain ? "var(--gain)" : "var(--loss)")
      .attr("fill-opacity", 0.85);
    group.select(".pos-label").attr("y", -d.r - 5).text(d.data.symbol);
    group.select("title").text(
      `${d.data.symbol} · ${d.data.market_value_sol.toFixed(3)} ◎` +
      `\nPnL ${d.data.unrealized_pnl_sol.toFixed(3)} ◎\nclick to inspect`);
  } else if (d.type === "candidate") {
    group.select("circle").attr("r", d.r);
    group.select("title").text(
      `Candidate ${d.data.address}\nhistory still being read from chain`);
  } else {
    group.select("circle").attr("r", d.r);
    group.select("title").text(
      `Rejected ${shortAddr(d.data.address)}\n${d.data.rejection_reason}`);
  }
}
