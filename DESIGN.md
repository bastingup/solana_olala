---
name: Solana-olala
description: Copy-trade command deck — a charted galaxy of wallet planets, trader moons, and position satellites on true-black space.
colors:
  space: "#050508"
  space-raised: "#0c0a14"
  space-panel: "#100d1c"
  nebula: "#a855f7"
  nebula-deep: "#6d28d9"
  nebula-soft: "#c4b5fd"
  nebula-line: "rgba(168, 85, 247, 0.22)"
  gain: "#22d3ee"
  loss: "#fb7185"
  live: "#f87171"
  arm: "#f97316"
  ink: "#ece8f6"
  ink-dim: "#a89fc7"
  ink-faint: "#8d84b0"
typography:
  display:
    fontFamily: "Chakra Petch, Trebuchet MS, sans-serif"
    fontSize: "17px"
    fontWeight: 700
    letterSpacing: "0.08em"
  label:
    fontFamily: "Chakra Petch, Trebuchet MS, sans-serif"
    fontSize: "9px"
    fontWeight: 600
    letterSpacing: "0.18em"
  body:
    fontFamily: "Red Hat Mono, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
  data:
    fontFamily: "Red Hat Mono, Consolas, monospace"
    fontSize: "15px"
    fontWeight: 600
    fontVariation: "tabular-nums"
  readout:
    fontFamily: "Red Hat Mono, Consolas, monospace"
    fontSize: "16px"
    fontWeight: 600
    fontVariation: "tabular-nums"
  control:
    fontFamily: "Chakra Petch, Trebuchet MS, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    letterSpacing: "0.06em"
  ui:
    fontFamily: "Red Hat Mono, Consolas, monospace"
    fontSize: "11px"
    lineHeight: 1.45
  caption:
    fontFamily: "Red Hat Mono, Consolas, monospace"
    fontSize: "10px"
rounded:
  focus: "2px"
  sm: "3px"
  md: "4px"
  card: "5px"
  lg: "6px"
  chip-pill: "10px"
  pill: "14px"
  banner-pill: "20px"
spacing:
  xs: "6px"
  sm: "8px"
  md: "10px"
  lg: "14px"
  xl: "18px"
components:
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.nebula-soft}"
    rounded: "{rounded.sm}"
    padding: "4px 9px"
  button-ghost-hover:
    backgroundColor: "rgba(168, 85, 247, 0.18)"
    textColor: "{colors.ink}"
  button-commit:
    backgroundColor: "{colors.nebula-deep}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "8px 10px"
  button-arm:
    backgroundColor: "{colors.arm}"
    textColor: "#1a0d02"
    rounded: "{rounded.md}"
    padding: "8px 14px"
  chip:
    backgroundColor: "rgba(16, 13, 28, 0.8)"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "5px 10px"
  wallet-card:
    backgroundColor: "{colors.space-raised}"
    textColor: "{colors.ink}"
    rounded: "5px"
    padding: "10px 11px"
  mode-badge:
    backgroundColor: "rgba(109, 40, 217, 0.18)"
    textColor: "{colors.nebula-soft}"
    rounded: "{rounded.sm}"
    padding: "5px 12px"
  mode-badge-armed:
    backgroundColor: "rgba(249, 115, 22, 0.2)"
    textColor: "#ffd9bd"
    rounded: "{rounded.sm}"
    padding: "5px 12px"
  power-btn:
    backgroundColor: "radial-gradient(circle at 40% 32%, #221318, #120a0d)"
    textColor: "#6b4a52"
    rounded: "50%"
    size: "46px"
  power-btn-on:
    backgroundColor: "radial-gradient(circle at 40% 32%, #57121e, #200a0f)"
    textColor: "{colors.live}"
  input:
    backgroundColor: "{colors.space-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "7px 8px"
---

# Design System: Solana-olala

## Overview

**Creative North Star: "The Galaxy Command Deck"**

Solana-olala's interface is a space-sim star chart under operator command, not a
finance dashboard. The trading system is drawn as one charted sky: wallets are
planets (ringed, gradient-lit primary bodies), copied traders are moons orbiting
the planet they feed, and open positions are satellites strung between them.
Unproven candidates glimmer faint along the upper band; rejected traders cool to
embers along the lower. The sidebar-and-stat-cards crypto dashboard is refused
outright — aggregate state is read from the sky itself, and the operator
intervenes by touching the object in question.

The world is true-black space with nebula purple carrying all structure and
identity. Purple draws the borders, glows, labels, and links; it never renders
a financial verdict. Money outcomes speak in exactly two voices — cyan for gain,
rose for loss — and two colors are held in reserve for real money: safety orange
arms the universe (the hold-to-arm key, the ARMED badge, keystore and
live-wallet warnings), and live-red marks each live wallet itself — a red planet
that stays dark until both the wallet and the universe are armed. Everything
fits one unscrolled cockpit viewport: command bar on top,
wallet rail left, live galaxy center, feed and roster right, and a full token
chip wall across the base. Density is high, type is small and engineered, and
every number is monospaced.

**Key Characteristics:**
- One-viewport cockpit; the page itself never scrolls (`overflow: hidden`), only panels scroll internally.
- Planet / moon / satellite hierarchy is the information architecture, not decoration.
- Purple = structure and identity; cyan/rose = P&L verdicts; orange = the universe arm key; live-red = per-wallet live identity. Nothing else judges or threatens.
- Chakra Petch uppercase command labels over Red Hat Mono tabular readouts.
- Glow is the primary elevation cue; keycap buttons are the primary press affordance.
- Cross-highlighting everywhere: hovering any representation of a thing lights its counterpart in the galaxy (`.lit`).

## Colors

A true-black space ground, one structural purple family, two verdict colors, and two reserved live-money colors.

### Primary
- **Nebula Purple** (`nebula` #a855f7): the structural and identity color — panel borders on hover/lit states, focus rings, trader moon cores, the scan pulse, glow shadows, selection caret. Carries the brand's binding purple commitment.
- **Deep Nebula** (`nebula-deep` #6d28d9): pressed/filled purple — commit-button gradient base, scrollbar thumbs, `::selection` background, brand-mark orbit stroke.
- **Soft Nebula** (`nebula-soft` #c4b5fd): purple-tinted heading and label color (rail headings, ghost buttons, wallet planet rims, starfield stars).
- **Nebula Line** (`nebula-line` rgba(168, 85, 247, 0.22)): the universal hairline — every resting panel border, divider, and chip outline is this one translucent purple line.

### Secondary
- **Gain Cyan** (`gain` #22d3ee): positive P&L, open exposure, win-rate arcs, the FOLLOWED status, the live-stream dot, position satellites in profit. Financial "good" is always cyan, never green.
- **Loss Rose** (`loss` #fb7185): negative P&L, rejections, closed-link state, error toasts and form errors, danger-button family. Financial "bad" is always this rose, never pure red.

### Tertiary
- **Safety Orange** (`arm` #f97316): reserved exclusively for the universe-level arm — the HOLD-TO-ARM keycap, the ARMED mode badge, the locked-keystore notice, the LIVE wallet-card tag (as peach tints #f8c9a4 / #ffd9bd). It appears nowhere else in the interface.
- **Live Red** (`live` #f87171): the per-wallet live identity. Live wallets render as red planets — a dark gradient body (#4c1622 → #220b10 → #0e0608, rim #7f2434) while disarmed, a hot one (#9f1f35 → #4c1220 → #1c070c, live-red rim) when armed — and the inspector's power button, its state text, and the legend's live-planet swatch all speak it. It glows only when the wallet and the universe are both armed; it never renders a P&L verdict.

### Neutral
- **Space** (`space` #050508): the true-black page ground, warmed by two enormous purple radial washes on `body`.
- **Raised Space** (`space-raised` #0c0a14): input fields and raised fills.
- **Panel Space** (`space-panel` #100d1c): drawer and toast surfaces; also the base of translucent panel fills (rails at rgba(12,10,20,0.72), floating layers at rgba(9,7,16,0.85–0.93)).
- **Ink** (`ink` #ece8f6): primary text and emphasized values — a faintly violet white.
- **Dim Ink** (`ink-dim` #a89fc7): secondary text, feed prose, meta rows.
- **Faint Ink** (`ink-faint` #8d84b0): labels, hints, timestamps, doctrine text, candidate nodes.

### Named Rules
**The One Orange Rule.** Safety orange (#f97316) marks universe-level real-money execution and nothing else: the hold-to-arm key, the ARMED mode badge, keystore and live-wallet warnings (peach tints #f8c9a4 / #ffd9bd). Per-wallet live identity is live-red, never orange; using orange for decoration, emphasis, or any non-live-money purpose is a violation — its scarcity is what makes the arm key legible as danger.

**The Two-Key Rule.** Live is dark by default and red when hot. A live wallet renders as a dark red planet with an unlit power button until *both* keys are turned — the wallet's own power button and the universe arm key. Only then does live-red (#f87171) glow: planet drop-shadow, power-button halo, state text. Live-red never appears on paper surfaces and never judges money.

**The Verdict Rule.** Purple never judges money. Every P&L, exposure, and status verdict is cyan (#22d3ee) for gain/followed or rose (#fb7185) for loss/rejected; purple is only allowed to say "this exists and belongs to the system."

**The One Hairline Rule.** All resting borders are the same translucent purple hairline (`rgba(168,85,247,0.22)`, 1px). State changes promote the border to full `nebula`, optionally with the glow; they never introduce a new border color.

## Typography

**Display Font:** Chakra Petch (with Trebuchet MS fallback) — self-hosted woff2, weights 500/600/700
**Body/Data Font:** Red Hat Mono (with Consolas fallback) — self-hosted woff2, weights 400/500/600

**Character:** A squared, technical display face barking short uppercase commands over a calm monospace that carries every readout. The pairing is the cockpit: Chakra Petch is signage, Red Hat Mono is instrumentation. There is no third face and no serif anywhere.

### Hierarchy
- **Display** (700, 17px, 0.08em tracking): the brand wordmark only (`SOLANA·OLALA`), with a nebula text-glow.
- **Label** (Chakra Petch 600, 9–12px, 0.08–0.22em tracking, always uppercase): rail headings, readout labels, button text, status words, node labels in the galaxy. Smaller size gets wider tracking (9px labels run 0.18–0.22em).
- **Data** (Red Hat Mono 600, 15–16px, `tabular-nums`): fleet readouts and wallet equity — the numbers the operator watches.
- **Body** (Red Hat Mono 400, 11–13px, line-height ~1.45): feed prose, form labels, doctrine text. Base body size is 13px on `body`; dense panel text runs 11px.
- **Micro** (9–10px): timestamps, hints, addresses, rejection reasons, in `ink-faint`.

### Named Rules
**The Tabular Truth Rule.** Every number the operator compares — equity, PnL, prices, win rates — is set in Red Hat Mono with `font-variant-numeric: tabular-nums` so columns of digits never shimmy as the stream updates.

**The Signage Rule.** Chakra Petch appears only uppercase, only tracked out, only short: it labels and commands. Sentences, prose, and data always fall to Red Hat Mono.

## Layout

The cockpit is a fixed three-row grid on `body` — 56px command bar, 1fr main, 64px chip wall — with `overflow: hidden`: the page never scrolls in either axis. The main row is a three-column grid: 272px wallet rail (`--rail-w`), fluid galaxy stage, 316px ops rail (`--ops-w`). Rails are translucent panels separated from the stage by single purple hairlines. Scrolling happens only inside lists (wallet list, feed, roster scroll vertically; the chip wall scrolls horizontally).

Spacing runs a tight 6/8/10/12/14/18px rhythm: 14px rail padding, 18px bar padding, 10–12px gaps between cards, 6–8px gaps inside them. Density is deliberately high — this is an instrument panel, not a marketing page.

**Responsive: whole panels, never squish.** Below 1180px the ops rail leaves the grid and a FEED toggle appears in the command bar; below 860px the wallet rail leaves too (WALLETS toggle), the command bar wraps to two lines, and the layout is single-column. The toggles swap the galaxy stage for the requested rail *as a whole panel* — one visible at a time, `aria-pressed` tracked, Escape restores the galaxy. No panel is ever fractionally compressed, truncated, or turned into an overlay sliver.

**The Whole-Panel Rule.** At narrow widths, panels are swapped in and out as complete units via the command-bar toggles. Squeezing a rail, shrinking the galaxy to a corner, or hiding content without a toggle to bring it back are all violations.

## Elevation & Depth

Depth is glow, not shadow-stacking. The resting interface is flat translucent panels on true black; importance and interactivity are signaled by the nebula glow (`--glow-nebula: 0 0 14px rgba(168, 85, 247, 0.45)`) appearing on hover, `.lit` cross-highlight, and floating layers. Floating layers (inspector, toasts, drawer) add a deep black ambient drop (e.g. `0 12px 30px rgba(0,0,0,0.6)`) under the glow to lift off the sky.

The exception is the keycap: physical action buttons (arm key, commit, danger) carry a hard 2–3px bottom ledge (`0 3px 0 <dark rim>` plus a soft drop) that collapses on `:active` with a 2px `translateY` — a pressable key, the world's one piece of physical materiality.

### Shadow Vocabulary
- **Nebula glow** (`box-shadow: 0 0 14px rgba(168, 85, 247, 0.45)`): hover/lit emphasis on cards and chips; identity glow on floating panels.
- **Keycap ledge** (`0 3px 0 #9a3d07, 0 5px 10px rgba(0,0,0,0.6)` on the arm key; `0 2px 0 #3b1478, 0 4px 8px rgba(0,0,0,0.5)` on commit buttons): the pressed state drops to a 0–1px ledge with `translateY(2px)`.
- **Floating-layer drop** (`0 10–12px 24–30px rgba(0,0,0,0.5–0.6)`): inspector, toasts, drawer — always paired with a 1px purple (or state-colored) border.
- **Rose alarm glow** (`0 0 14px rgba(251, 113, 133, 0.35)`): error toasts only.
- **Live armed glow** (`filter: drop-shadow(0 0 12px rgba(248, 113, 113, 0.65))` on the planet core; `0 0 16px rgba(248, 113, 113, 0.55), inset 0 0 8px rgba(248, 113, 113, 0.35)` on the power button; `text-shadow: 0 0 8px rgba(248, 113, 113, 0.6)` on the state text): the hot state of a live wallet, present only while wallet and universe are both armed (The Two-Key Rule).

### Named Rules
**The Glow-Is-Meaning Rule.** A glow is a statement that this object is currently interesting (hovered, linked to the thing under the cursor, or floating above the sky). Nothing glows at rest except the brand wordmark — and an armed live planet, whose steady red glow is the danger light, not decoration.

## Shapes

Rectangles are tight and technical: 3px on small controls (ghost buttons, tabs, inputs, badges), 4–6px on cards and floating panels (feed rows 4px, wallet cards 5px, inspector 6px). Fully rounded pills mark *status and tokens*: count chips (10px), token chips (14px), the scan banner (20px). Everything in the galaxy is a circle — planet cores with dashed orbit rings (`stroke-dasharray: 3 5`), moon cores with halos and win-rate arcs, satellite dots, candidate/ember specks — and the power button is the same circle brought into the UI (46px, 50% radius). Planet decorations stay in the family: tilted ring ellipses, moonlet dots, quadratic equator bands, polar-cap ellipses — arcs and circles, never polygons. The brand mark itself is the shape system in miniature: core, orbit ellipse, moon.

Borders are always 1px hairlines (see The One Hairline Rule); no shape carries more than one border. Icons are drawn, not glyphed: inline SVG, `stroke: currentColor`, `stroke-width: 1.5`, round caps, no fills — the feed's trade/reject/trader/system icons, the close X, and the brand mark all follow this.

**The Drawn-Stroke Rule.** Every icon is a hand-drawn inline SVG in `currentColor` with round caps — 1.5 stroke at the standard 14–16px sizes (the 20px power glyph steps up to 2 to hold weight inside its 46px button). Icon fonts, emoji, filled glyphs, and third-party icon packs are outside the world.

## Components

### Galaxy Nodes (the signature system)
The force-directed sky in `frontend/js/galaxy.js`, drawn over a seeded 160-star field (soft-nebula dots, LCG seed so it never re-rolls). A bottom-left legend names the five species: paper planet, live planet, trader moon, position, candidate.
- **Wallet planet:** dashed outer ring (`r+6`, rgba(196,181,253,0.25), dash 3 5) around a core filled with the shared radial gradient `#planet-fill` (#312057 → #181129 → #0b0912, light source at 38%/32%) and a 2px soft-nebula rim. Radius 11–38px scales with √equity (`r = 11 + min(√equity × 3.4, 27)`) — dwarf planets for thin wallets, giants for heavy ones. Uppercase Chakra Petch label 18px below. Planets are pinned (`fx/fy`) — the fixed geography of the sky.
- **Live planet:** the same body in the live-red family. Disarmed it is dark — `#planet-fill-live` (#4c1622 → #220b10 → #0e0608), rim #7f2434, ring rgba(248,113,113,0.22), label suffixed "· DARK". When the wallet *and* the universe are armed it goes hot: `#planet-fill-armed` (#9f1f35 → #4c1220 → #1c070c), live-red rim, ring rgba(248,113,113,0.45), and the 12px red drop-glow (The Two-Key Rule).
- **Planet decorations:** every planet rolls a stable set of dressings — tilted ring (42%, with a 35% chance of a thin outer companion), moonlet (32%), equator band (38%), polar cap (25%) — from a mulberry32 PRNG seeded by an FNV-1a hash of the wallet id, so the same wallet always shows the same face across renders and sessions. Drawn at base radius 20 and scaled with the body (`scale(r/20)`); rings are soft-nebula rgba(196,181,253,0.35) (live planets tint them rgba(248,113,113,0.35)), bands and caps are faint ink-white washes, moonlets faint-ink dots.
- **Trader moon:** nebula core (radius 6–12 by score) with a faint halo ring and a cyan win-rate arc (2.5px, round caps) sweeping `win_rate × 360°` around it. Labeled with the shortened address. Moons orbit their assigned planet.
- **Position satellite:** a dot sized by √market-value (6–22px), filled `gain` or `loss` at 0.85 opacity, symbol label above; linked to its wallet by a faint cyan line and to its source trader when followed.
- **Candidate speck:** 2.6px faint-ink dot at 0.7 opacity drifting in the upper band. **Rejected ember:** 3.2px #3c3654 dot in the lower band, rejection reason on hover (`<title>`).
- **Links:** assignment lines purple rgba(168,85,247,0.35) 1.2px; position lines cyan rgba(34,211,238,0.28) 1px.
- Every node is clickable (opens the inspector) and hover-lights (`.lit` adds a purple drop-glow); position hover lights the matching token chip and vice versa.

### Buttons
- **Ghost** (`.ghost-btn`): transparent, purple hairline border, soft-nebula uppercase 10px label, 3px radius; hover fills rgba(168,85,247,0.18) and brightens to ink. The default rail/utility action. As a rail toggle, `aria-pressed="true"` fills rgba(168,85,247,0.25) with a full nebula border.
- **Commit** (`.commit-btn`): purple keycap — gradient #8b5cf6 → nebula-deep, 4px radius, ink text, `0 2px 0 #3b1478` ledge, presses down 2px on `:active` (90ms ease-out). The affirmative form action (REGISTER WALLET).
- **Danger** (`.danger-btn`): the commit keycap in rose — gradient #f43f5e → #9f1239, ledge #5f0a22. Destructive inspector actions (STOP COPYING, CLOSE POSITION NOW).
- **Arm key** (`.arm-key`): the one orange object. Orange keycap gradient (#fb923c → #f97316), near-black text (#1a0d02), `0 3px 0 #9a3d07` ledge. Requires a deliberate 1200ms press-and-hold: a white 45%-opacity fill sweeps left-to-right as progress; releasing early cancels. When the universe is armed, the key re-skins to the purple family (soft-nebula gradient, #4c1d95 ledge, #140a26 text) and reads HOLD TO DISARM. Works by pointer and by held Enter/Space.

### Power Button
The per-wallet arm control, rendered only in live-wallet inspectors: a 46px circle (2px #3d2a33 border, dark red radial fill #221318 → #120a0d) holding a 20px drawn power glyph (2-stroke, round caps) in dim #6b4a52. Toggled on, everything goes live-red: border and glyph #f87171, hot radial fill #57121e → #200a0f, a 16px outer glow with an inset ember. Beside it the power-state label (Chakra Petch 11px, 0.16em tracking) reads "ARMED — trades when universe is armed" (live-red, text-glow) or "DARK — holds fire" (faint ink). It tracks `aria-pressed` and arms only this wallet; real execution still requires the universe arm key (The Two-Key Rule).

### Wallet Cards
5px-radius cards on a diagonal purple-tinted gradient with the standard hairline; hover or `.lit` promotes the border to nebula plus glow. Anatomy: Chakra Petch name row with a status tag — PAPER, or LIVE · ARMED / LIVE · DARK for live wallets (peach #f8c9a4) — 16px tabular equity with dim USD suffix, then the **fuel bar** — an 8px three-segment gauge: solid nebula for free cash, a 45° purple-on-deep-violet candy stripe (`repeating-linear-gradient(45deg, #7c3aed 0 4px, #4c1d95 4px 8px)`) for held reserve, cyan for value in positions — and a dim meta row (open count, reserve amount). Cards are keyboard-focusable buttons opening the wallet inspector.

### Feed Entries
16px/1fr/auto grid rows on rgba(16,13,28,0.6), 4px radius, 11px mono text, with a 1px colored left border classifying the event: cyan trades, rose rejections, purple trader events, faint-ink system notes. Each entry leads with a 14px drawn 1.5-stroke icon in `currentColor` (arrows-exchange for trades, slashed shield for rejections, star for trader events, clock for system) and ends with a faint relative timestamp (absolute time on hover).

### Chips
The token chip wall: pill buttons (14px radius) on rgba(16,13,28,0.8) with the hairline border — Chakra Petch symbol plus a mono amount. Open positions color the amount by P&L (`gain`/`loss` classes); the **watch tier** (tokens merely traded by followed traders) sits at 0.65 opacity with "seen" as its amount. Sort order: open exposure first, then closed, then watch. Hover or cross-highlight promotes border to nebula plus glow; hovering a chip lights every matching position satellite in the galaxy and vice versa. Count chips (rail headers) are the 10px-radius micro-pill variant of the same language.

### Inspector
The single detail surface, shared by galaxy nodes, wallet cards, and roster rows: a 250px card floating top-right over the stage, full-nebula border, 6px radius, near-black rgba(9,7,16,0.93) fill, glow plus deep drop. Anatomy: Chakra Petch header (`TYPE · name`) with a drawn close X, `.spec-row` label/value pairs (dim label left, ink tabular value right), a word-broken faint address line, and — where intervention applies — one danger keycap; live-wallet inspectors carry the power row (power button + state text) as their intervention instead. Closes on Escape, on its X, or by clicking empty sky.

### Drawer
The add-wallet flow: a full-height 340px panel sliding from the left over a rgba(3,2,6,0.7) scrim, panel-space fill, nebula right border, 220ms `cubic-bezier(0.16,1,0.3,1)` entrance. PAPER/LIVE tabs (3px-radius ghost tabs; active fills rgba(109,40,217,0.25) with nebula border), mono inputs on raised space, rose inline form error, one commit keycap. Scrim click, X, and Escape all close it.

### Inputs
Red Hat Mono 12px on `space-raised`, hairline border, 3px radius, 7×8px padding, faint-ink placeholders. Focus is the global ring: 2px nebula outline, 2px offset. Errors are rose text below the field, never a red border.

### Status Indicators
- **Mode badge:** tracked-out Chakra Petch pill-adjacent badge (3px radius) reading the universe state; SAFE = purple border on purple tint; ARMED = orange border, peach text (#ffd9bd), orange tint plus text-glow.
- **Link status:** 8px dot plus 10px tracked label — faint/LINKING, cyan glowing dot/STREAM LIVE, rose/RELINKING.
- **Scan banner:** floating 20px pill at stage top with a 2.2s pulsing purple dot; visible only while no trader is followed; copy states the doctrine ("Strict filters mean admission takes time; that is the point.").
- **Toasts:** panel-space cards bottom-right (above the chip wall), nebula border and glow (rose for errors), 260ms rise-in, auto-dismiss at 5s.

### Navigation
There is no navigation — the cockpit is one screen. The command bar carries identity (glowing wordmark plus orbit mark), the three fleet readouts, the narrow-width rail toggles, link status, mode badge, and the arm key. Comprehension is spatial, not hierarchical.

## Do's and Don'ts

### Do:
- **Do** settle the galaxy synchronously before showing it: run the force simulation ~150 ticks at full alpha on data changes, paint the composed sky, then restart at alpha 0.08 for gentle drift. The sky is never seen mid-explosion.
- **Do** keep orbital motion barely perceptible: moon anchors advance at 0.00008 rad/ms (~78s per revolution), and node positions persist across data updates (the `_memory` map) so the sky feels like a place, not a re-roll.
- **Do** honor `prefers-reduced-motion` fully: no orbit timer, no drift restart (alphaDecay 0.3, instant settles), no pulse/entrance animations, all transitions zeroed.
- **Do** cross-highlight bidirectionally with the shared `.lit` class: chip ↔ satellite, wallet card ↔ planet, roster row ↔ moon. Any new representation of an existing object must light its counterpart.
- **Do** open the same inspector from every representation of an object, and put the intervention button (danger keycap) inside it — the operator acts on the object, never in a settings page.
- **Do** make dangerous state changes deliberate: mode switching is a 1200ms hold with visible progress and early-release cancel.
- **Do** keep live wallets dark until both keys turn: the red planet, power button, and state text glow only when the wallet is armed *and* the universe is armed (The Two-Key Rule).
- **Do** debounce galaxy re-layout on streaming deltas (200ms) while rendering panels immediately; snapshots update the galaxy at once.
- **Do** keep Escape as the universal dismiss: inspector, drawer, and narrow-width rail swaps all clear.
- **Do** theme the browser surfaces purple: `::selection` on nebula-deep, thin nebula-deep scrollbars, nebula caret, and the 2px nebula `:focus-visible` ring with 2px offset everywhere.

### Don't:
- **Don't** introduce sidebar-and-stat-cards dashboard furniture (KPI grids, chart panels, breadcrumbs, nav menus). The galaxy plus its rails is the entire information architecture.
- **Don't** use orange outside universe-arm surfaces, live-red outside live-wallet identity, green for gains, or red for losses (losses are always rose #fb7185). The palette is closed: purple structure, cyan/rose verdicts, one orange key, one live red.
- **Don't** squish panels at narrow widths — swap whole panels via the command-bar toggles per The Whole-Panel Rule.
- **Don't** let the page scroll. New content goes inside a scrolling panel or behind a toggle; the cockpit frame (command bar, stage, chip wall) stays fixed.
- **Don't** use icon fonts, emoji, or filled glyph icons; every icon is a drawn 1.5-stroke `currentColor` SVG (The Drawn-Stroke Rule).
- **Don't** set comparable numbers in the display face or proportional figures; readouts are Red Hat Mono `tabular-nums` (The Tabular Truth Rule).
- **Don't** fabricate performance evidence in the UI — no returns, win-rate claims, or benchmarks beyond what the stream reports (PRODUCT.md: none exist).
- **Don't** add second borders, colored border variants, or heavy outlines; state is expressed by promoting the one hairline to nebula and adding glow.
