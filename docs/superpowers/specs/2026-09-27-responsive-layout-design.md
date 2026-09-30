# Responsive layout: phone and tablet support

Date: 2026-09-27. Branch: `mobile-design`. Status: draft for operator review.

## Goal

The dashboard reads and operates correctly at three widths: phone, tablet,
desktop. On a phone the six tables show their primary columns without
horizontal scrolling, every secondary value stays one tap away, and nothing
a desktop user can do is lost. Desktop rendering does not change.

## What exists today

The page already has partial responsive rules in `web/app.css`, added ad hoc
at four widths:

| Width | Rule today |
|---|---|
| 1240 | monitor grid goes to 3 fixed columns above this |
| 1080 | rail collapses to icons, toggle hidden |
| 700 | main padding shrinks, ring shrinks, pane height floor, title column pins left, seam blanking |
| 640 | rail lies flat across the top, body becomes one grid column |
| `pointer: coarse` | 44px theme buttons, grip column hidden, row actions always visible |

The tables come from one builder, `table(cols, rows, build)` in
`web/app.js`. Every cell is `white-space: nowrap`; the pane scrolls inside
`.scroll` at 60vh with the scrollbar hidden. Column counts per tab:

| Tab | Columns |
|---|---|
| Queue | 9 (grip, Rank, SRC Mb/s, Src size, Quality, Title, NAS, Src folder, Status) |
| History | 12 (#, Title, Original, Output, Saved, Shrink, Quality, Tracks, NAS, Moved to, Encode time, Finished) |
| Errors | 7 |
| Processes | 6 |
| Blacklist | 5 |
| Events | 2 |

Phone failures this causes:

- History needs roughly 1400px. On a 390px phone the reader scrolls
  sideways with no scrollbar and no edge cue, and the seam script blanks
  whichever column straddles an edge, which reads as missing data.
- The flat rail packs brand, six tabs with counts, connection text and a
  three-segment theme switch into one row. It overflows and scrolls
  sideways at 390px.
- Two scroll surfaces: the page scrolls and the pane scrolls inside it.
  On touch the inner pane traps the swipe.
- The 700px and 640px breakpoints disagree, so between 640 and 700 the rail
  is a column while the main area is already in phone compaction.
- The stats grid falls to one 190px column and stacks six tall cards above
  the live card, pushing the encode below the fold.

## Tiers and breakpoints

Three tiers, two breakpoints, nothing else. The 700px block is retired; its
rules move to whichever tier they belong to.

| Tier | Media query | Rail | Table |
|---|---|---|---|
| Phone | `max-width: 639px` | two rows across the top | grid rows, primary columns only, tap to reveal |
| Tablet | `640px` to `1080px` | icon column | full table, title pinned, seam blanking, edge fade |
| Desktop | `min-width: 1081px` | full rail, collapsible | unchanged |

The monitor's 1240px three-column rule stays as a layout detail inside the
desktop tier. A repo invariant test pins the allowed set of widths in media
queries to `639`, `640`, `641`, `1080`, `1081`, `1240` so a fourth
breakpoint cannot creep back in.

Landscape phones (844×390) fall in the tablet tier by width. That is
intended: at 844px the full table with a pinned title is the better use of
the space.

## Table on the phone: tiered columns with a disclosure row

### Options considered

1. **Tiered columns, grid rows, tap to reveal (chosen).** Each column is
   primary or secondary. On the phone a row becomes a CSS grid: primary
   cells on the first line, secondary cells hidden until the row is opened,
   then laid out as label and value pairs beneath. One DOM node per cell,
   so in-place progress updates, chips, pickers and row actions keep
   working untouched.
2. **Card per row.** Rebuild each row as a card on the phone. Duplicates
   interactive controls or forks the renderer per width, and loses the
   sticky header and the `progRefs` in-place updates.
3. **Keep the scroll, add affordance.** Cheapest, but a 12-column History
   still needs three screens of sideways travel on a phone. Kept only as the
   tablet behaviour.

### Column tiers

Primary columns are what the operator scans for. Every other column is
secondary and appears in the opened row, in its desktop order. No column is
dropped anywhere.

| Tab | Primary | Secondary |
|---|---|---|
| Queue | Rank, Title, SRC Mb/s, Status | Src size, Quality, NAS, Src folder |
| History | #, Title, Saved, Finished | Original, Output, Shrink, Quality, Tracks, NAS, Moved to, Encode time |
| Errors | State, Title, What happened | SRC Mb/s, Src size, NAS, Src folder |
| Processes | Process, Purpose, CPU % | PID, Working on, Running for |
| Blacklist | Entry, Blocks | Kind, Titles it blocks, Changed by |
| Events | Time, Event | none; Time stacks above Event in one cell-pair |

The grip column stays hidden on coarse pointers as today. Row actions (skip,
start, pull, abort) and the CRF and encoder pickers live in the Quality and
Status cells already; Quality is secondary on the Queue, so the pickers sit
in the opened row, and Status stays primary so the encoding state is always
visible.

### Mechanism

- `table()` accepts a `tier: "s"` flag per column. It writes class `c-s` on
  the secondary `th` and `td`, and `data-l` (the column label) on every
  `td`. Labels are static strings from the column list, never server data.
- Each table also gets a class naming its tab (`t-queue`, `t-history`, and
  so on) so the phone CSS can give each its own grid template for the
  primary line.
- Phone CSS: `table, thead, tbody { display: block }`, `tr { display: grid;
  grid-template-columns: var(--cols) }` per tab. Secondary cells are
  `display: none` until `tr.open`, where each becomes `grid-column: 1 / -1`
  with `::before { content: attr(data-l) }` as the label. The header row
  keeps the same grid so the primary labels line up; secondary `th` stay
  hidden. `nowrap` is lifted on Title, Src folder, Moved to, What happened,
  Working on and Purpose so they wrap instead of overflowing.
- A full-width transfer row (`.xrow`) spans `1 / -1` and is never a
  disclosure target. The stalled and moving states render exactly as on the
  desktop.
- The disclosure: one delegated click handler on `#pane`, active only while
  `matchMedia("(max-width: 639px)")` matches. It ignores clicks inside a
  button, select, link or picker. It toggles `.open` on the row and records
  the row key in an in-memory set. `build()` re-applies `.open` from that
  set, so a repaint does not close what the operator opened. The key is the
  row's `data-title` for Queue and Errors, the title plus finished time for
  History, the PID for Processes, the entry for Blacklist. The set never
  enters a repaint key; open state is a view fact, not a shape.
- A small chevron in the first primary cell turns when the row is open, so
  the tap target is discoverable without a hover.
- The seam script exits early when the phone query matches. With grid rows
  there is no horizontal scroll, so nothing straddles an edge.

## Tablet table

- The pinned title column and the seam blanking move from 700px to the
  tablet tier, so a 1024px iPad landscape with a 12-column History keeps
  the title in view.
- A right-edge fade signals overflow. The seam script already measures the
  pane on scroll and resize; it toggles `.can-right` and `.can-left` on
  `#tablewrap` when there is room to scroll that way, and the CSS draws a
  gradient over that edge. The gradient is a token colour so both themes
  follow.
- Cell padding drops to 7px 9px, as the 700px block does today.

## Phone shell

- **One scroll surface.** On the phone `.scroll` loses its `max-height` and
  `overflow`, so the page is the only thing that scrolls. `th` stays sticky
  against the viewport, under the tab strip.
- **Rail in two rows.** Row one: pulse, wordmark, connection dot, theme
  switch. Row two: the six tabs as equal-width icon buttons with their
  counts, no labels, filling the width, no horizontal scroll at 320px. Row
  two is `position: sticky; top: 0` so the tabs stay reachable while the
  table scrolls. The rail toggle stays hidden.
- **Page head.** The heading sits alone on its line. The stats become a
  two-column grid of compact stats (label, value, qualifier) without the
  card chrome, so the six totals take two rows instead of six.
- **Live card.** The ring shrinks to 112px and the hero body wraps under it.
  The `kv` grid goes to two columns. The pause control keeps its 44px hit
  area.
- **Monitor.** One chart per row as today. The zoom slider is already full
  width. The tooltip clamps to the viewport so it cannot hang off the right
  edge. The three mini sparklines shrink to 48px.
- **Chrome.** `index.html` adds `viewport-fit=cover` like the login page
  does, and main and the rail pad with `env(safe-area-inset-*)`. The footer
  wraps. Main padding is 16px 12px 48px.
- **Touch targets.** The coarse-pointer block extends its 44px rule to
  `.act`, `.panebtn`, the pickers and the row disclosure.

## Out of scope

- `login.html` is already a single 360px column and needs no change.
- Reordering by drag stays desktop-only; skip and pin buttons carry the
  phone.
- No column is hidden outright anywhere, and no number is rounded
  differently by width.

## Constraints carried through

- Static labels only in `data-l` and `::before`. No server data reaches
  `innerHTML`; the three CSP nonces are untouched because no new script or
  style block is added.
- Every new colour is a token defined in both themes.
- Byte counts and progress still reach the DOM through `progRefs` in place;
  the grid layout does not rebuild rows.
- The server restarts after the CSS and JS edits, per repo rule.
- `pipeline/` is not touched. `web/` is safe to edit while the driver runs.

## Testing

- `tests/test_table_tiers.ts` (new, bun, runs the real `table()`): every
  column list declares tiers; every secondary `th` has a matching `c-s`
  `td` in every row; every `td` carries `data-l` equal to its header; a
  row opened by key stays open across a rebuild; an `.xrow` is never
  opened; the handler ignores a click on a button inside the row.
- `tests/test_repo_invariants.py`: media-query widths limited to the
  allowed set; no `@media (max-width: 700px)` remains; `.c-s` and `data-l`
  are never written from a server field.
- `tests/check_page.py`: `viewport-fit=cover` present in the assembled
  page.
- `tests/visual/shoot.mts`: add `--viewport phone|tablet|desktop` presets
  (390×844 at 3x, 820×1180 at 2x, 1280×1400 at 2x) and a `--tab` flag so
  each tab can be shot at each preset in both themes. The screenshots are
  the acceptance check for layout, the same way they were for the 7 d
  monitor widening.
- `bun run verify` stays green.

## Delivery order

Each step leaves the desktop unchanged and the suite green.

1. Breakpoint unification and the invariant test. Move the 700px rules to
   their tiers. No visual change above 1080px.
2. Phone shell: two-row rail, one scroll surface, stats grid, safe areas,
   footer.
3. Table tiers: builder flag, per-tab tiers, phone grid CSS, disclosure and
   open-state persistence, seam script gated to the tablet tier.
4. Tablet pass: pinned title to 1080px, edge fades, wrapping columns.
5. Live card, monitor and touch-target adjustments.
6. Visual harness presets, shoot every tab at every preset, fix what the
   screenshots show.

## Decisions for the operator

1. The primary column picks above, in particular Saved and Finished for
   History and Status over Src size for the Queue.
2. Tap-the-row disclosure with a chevron, versus a dedicated expand button
   only.
3. A sticky tab strip on the phone, versus the whole rail scrolling away.
