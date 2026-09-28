# Responsive Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The dashboard reads and operates at phone (≤639px), tablet (640–1080px) and desktop widths, with every table showing its primary columns on a phone and every secondary value one tap away.

**Architecture:** Pure CSS tiers on top of the existing single-page dashboard, plus one small change to the shared `table()` builder in `web/app.js` that tags each cell with its column label and tier. On the phone a row becomes a CSS grid whose secondary cells hide until the row is opened; a delegated click handler on the pane toggles the row and an in-memory set keeps rows open across the 2 s repaint. The seam-blanking script and pinned title column move to the tablet tier. No renderer forks by width; one DOM node per cell so `progRefs` in-place updates keep working.

**Tech Stack:** Vanilla CSS and JS in `web/`, bun for tests (`bun tests/*.ts`, hand-rolled `check()` harness, no framework), Python `unittest` for repo invariants, the existing CDP screenshot harness `tests/visual/shoot.mts`.

**Spec:** `docs/superpowers/specs/2026-09-27-responsive-layout-design.md`

## Global Constraints

- Do not commit, push, merge or open a PR until the operator says so (CLAUDE.md). Every "Commit" step below is therefore **"Stage"**: `git add` the files and stop. The operator commits.
- Commit subjects, when the operator asks, use the `SMLTR:` prefix (`commit-format` skill).
- Only `web/`, `tests/`, `docs/` change. `pipeline/` is never touched.
- No `innerHTML` with server data. `data-l` and `::before` content carry static column labels only.
- Every new colour is a CSS token defined in both `:root` and `:root[data-theme="light"]` (`tests/test_repo_invariants.py::ThemeTokens` enforces same-named sets).
- The three CSP nonces in `web/index.html` stay. No new `<style>` or `<script>` blocks.
- Media-query widths allowed: `639px`, `640px`, `641px`, `1080px`, `1081px`, `1240px`. Nothing else.
- Desktop (>1080px) rendering must not change. Every task is checked against that.
- After any `web/` edit, restart the server with `bun run restart` so the operator's browser sees it.
- Formatting: `bun run format` (oxfmt for CSS/JS, ruff for Python) before staging. `bun run lint` and `bun run test` must pass at the end of every task.
- Row-open state never enters a repaint key (repaint keys carry shape, not view state).

## Review Focus

Failure modes the spec implies that no task test covered until added here. Each has a test pinned to its owning task.

1. **A History row whose transfer `.xrow` follows it.** Tapping the pair on a phone must open only the title row, and the full-width transfer row must never gain `.open` or `data-l`. Test in Task 4.
2. **Repaint while a row is open.** The 2 s SSE repaint rebuilds the table; the operator's opened row must come back open. Test in Task 4.
3. **A click on a picker or button inside a phone row.** The CRF `<select>` and the skip/start `.act` buttons live inside rows; a click on them must not toggle the row. Test in Task 4.
4. **A column list that forgets a tier.** A new column added without `tier` must default to primary, never vanish. Test in Task 3.
5. **Desktop regression.** The grid-row CSS must be scoped entirely inside the phone media query; a `display: grid` on `tr` outside it would break every desktop table. Invariant test in Task 1 pins the allowed widths; Task 3 adds an assertion that `tr{display:grid` appears only inside the `639px` block.

---

### Task 1: Breakpoint unification and the width invariant

Retire the `700px` block. Its phone-only rules move to a `max-width: 639px` block; its pinned-title and seam rules move to a tablet block `(min-width: 640px) and (max-width: 1080px)`. Pin the allowed widths with a test.

**Files:**
- Modify: `web/app.css:2654-2755` (the `@media (max-width: 700px)` block)
- Modify: `tests/test_repo_invariants.py` (add a class after `StickyHeaderOutranksTheTitleColumn`, ~line 946)

**Interfaces:**
- Produces: two named blocks in `app.css`, marked with comments `/* ==== PHONE (<=639px) ==== */` and `/* ==== TABLET (640-1080px) ==== */`, that later tasks append to.

- [ ] **Step 1: Write the failing invariant test**

Add to `tests/test_repo_invariants.py` after the `StickyHeaderOutranksTheTitleColumn` class:

```python
class ResponsiveTiers(unittest.TestCase):
    """Three tiers, two breakpoints (spec 2026-09-27).

    Phone is <=639px, tablet 640-1080px, desktop above. The old 700px block
    disagreed with the 640px rail flip, so between the two the rail was a
    column while main was already in phone compaction. Any width outside
    the allowed set is a fourth breakpoint sneaking back in.
    """

    ALLOWED = {"639", "640", "641", "1080", "1081", "1240"}

    def _media_widths(self):
        css = re.sub(r"/\*.*?\*/", " ", read("web/app.css"), flags=re.S)
        return re.findall(r"@media[^{]*?\b(?:min|max)-width:\s*(\d+)px", css)

    def test_only_the_named_breakpoints_exist(self):
        widths = self._media_widths()
        self.assertTrue(widths, "no width media queries found; regex has rotted")
        stray = sorted({w for w in widths if w not in self.ALLOWED})
        self.assertEqual(stray, [], f"media-query widths outside the tier set: {stray}")

    def test_the_700px_block_is_gone(self):
        self.assertNotIn("700px", read("web/app.css"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_repo_invariants.ResponsiveTiers -v`
Expected: both tests FAIL, `stray == ['700']`.

- [ ] **Step 3: Split the 700px block**

In `web/app.css` replace the header line `@media (max-width: 700px) {` and its contents (lines 2654–2755) with two blocks. Everything from `.monmini { gap: 10px }` through `.kv { grid-template-columns ... }` and the `.scroll` height floor and the `th, td` padding is phone-only. Everything from the `tr { background: var(--panel) }` comment through the `.cut` rules is the pinned-title mechanism and is tablet. Exact result:

```css
/* ==== PHONE (<=639px) ====================================================
   Compaction only. The table becomes grid rows in Task 3; the shell in
   Task 2. Anything here must not exist above 639px. */
@media (max-width: 639px) {
  .monmini {
    gap: 10px;
  }
  .monmini canvas {
    width: 64px;
  }
  main {
    padding: 16px 12px 48px;
  }
  .hero {
    gap: 16px;
  }
  .ring {
    width: 132px;
    height: 132px;
  }
  .ring-mid .pctbig {
    font-size: 24px;
  }
  .pagehead .statsgrid {
    gap: 8px 18px;
  }
  #monZoom {
    width: 100%;
    min-width: 0;
  }
  .monzoom {
    width: 100%;
    margin-left: 0;
  }
  .monleg {
    gap: 9px;
  }
  .monchart canvas {
    height: 110px;
  }
  .monchart.tall canvas {
    height: 132px;
  }
  .kv {
    grid-template-columns: repeat(auto-fit, minmax(104px, 1fr));
  }
  th,
  td {
    padding: 7px 9px;
  }
}

/* ==== TABLET (640-1080px) ================================================
   The full table with the title column pinned left while the rest scrolls,
   and seam blanking for a column sliced at an edge. A 1024px iPad landscape
   still shows all twelve History columns, so the pin earns its place here;
   on the phone (Task 3) rows are grids and nothing scrolls sideways. */
@media (min-width: 640px) and (max-width: 1080px) {
  .scroll {
    max-height: min(max(60vh, 340px), 72dvh);
  }
  th,
  td {
    padding: 7px 9px;
  }
  /* Row colour lives on the tr so the pinned title inherits it opaquely --
     a transparent pinned cell shows the columns sliding through it. */
  tr {
    background: var(--panel);
  }
  thead tr {
    background: var(--panel-2);
  }
  tbody tr:hover {
    background: var(--row-hover);
  }
  .rowenc {
    background: var(--row-enc);
  }
  /* Both halves are named so this outranks `td.title-cell{position:relative}`
     above; a bare `.title-cell` here loses to it on the td and the pinned
     column stops pinning. */
  th.title-cell,
  td.title-cell {
    min-width: 190px;
    position: sticky;
    left: 0;
    z-index: 1;
    background: inherit;
    border-right: 1px solid var(--line);
  }
  th {
    z-index: 2;
  }
  th.title-cell {
    z-index: 3;
  }
  /* A column sliced at a boundary renders EMPTY, not clipped. Clipped on its
     left, a right-aligned "70.64 GiB" still parses -- as "0.64 GiB"; clipped
     on its right it loses its unit. The .cut class is applied by the seam
     script at the bottom of app.js. */
  td.cut,
  th.cut {
    color: transparent;
  }
  td.cut *,
  th.cut * {
    visibility: hidden;
  }
}
```

Keep the existing `@media (max-width: 640px)` rail block, but change its query to `(max-width: 639px)` so the rail flip and the phone tier are the same edge. Update the two comments in `web/app.css` (line ~2575 "Below 640px") and `web/index.html` (line ~40 "below 640px") to say "at 639px and below".

- [ ] **Step 4: Update the seam script comment and gate**

In `web/app.js` the seam IIFE comment (~line 3274) says "Below 700px". Change it to "Between 640px and 1080px (the tablet tier)". No logic change yet; Task 3 adds the phone early-exit.

- [ ] **Step 5: Run the invariant test and the full gate**

Run: `python3 -m unittest tests.test_repo_invariants -v && bun run format && bun run lint && bun run test`
Expected: all PASS. Then `bun run restart` and eyeball the desktop at full width: nothing changed.

- [ ] **Step 6: Stage**

```bash
git add web/app.css web/app.js web/index.html tests/test_repo_invariants.py
```

---

### Task 2: Phone shell (rail, single scroll surface, stats, safe areas)

**Files:**
- Modify: `web/index.html:16` (viewport meta), `web/index.html:38-77` (rail markup: no structural change, one wrapper class)
- Modify: `web/app.css` phone block from Task 1, and the `@media (max-width: 639px)` rail block (~line 2603)
- Test: `tests/check_page.py` (one assertion), `tests/test_repo_invariants.py::ResponsiveTiers` (one assertion)

**Interfaces:**
- Produces: CSS custom property `--strip` (phone tab-strip height, `48px`) that Task 3 uses for the sticky header offset.

- [ ] **Step 1: Write the failing assertions**

Append to `tests/check_page.py` before the final exit logic:

```python
if "viewport-fit=cover" not in server.PAGE:
    sys.exit("index.html viewport meta lacks viewport-fit=cover (notched phones)")
```

Add to `ResponsiveTiers` in `tests/test_repo_invariants.py`:

```python
    def test_phone_has_one_scroll_surface(self):
        # Inside the phone block .scroll must drop its own scrolling so the
        # page is the only thing that scrolls under a thumb.
        css = re.sub(r"/\*.*?\*/", " ", read("web/app.css"), flags=re.S)
        i = css.index("@media (max-width: 639px)")
        phone = css[i : css.index("@media (min-width: 640px)", i)]
        dense = re.sub(r"\s*([{}:;>,])\s*", r"\1", phone)
        self.assertIn(".scroll{max-height:none;overflow:visible", dense)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 tests/check_page.py; python3 -m unittest tests.test_repo_invariants.ResponsiveTiers -v`
Expected: check_page exits with the viewport message; the new test FAILS.

- [ ] **Step 3: Viewport and markup**

`web/index.html` line 16 becomes:

```html
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
```

Wrap `.brand` in one `div.railtop` so the phone grid can place it. DOM order is otherwise unchanged (the desktop rail relies on `.railfill` pushing `.railfoot` to the bottom, and the phone CSS places the three groups by `grid-area`, so nothing needs to move):

```html
<nav class="rail" id="rail" aria-label="Sections">
  <div class="railtop">
    <div class="brand">
      ... (existing brand markup, unchanged) ...
    </div>
  </div>

  <div class="navlist" role="tablist">
    ... (existing six buttons, unchanged) ...
  </div>
  ... (railsec, gauges, railfill, railfoot unchanged) ...
</nav>
```

- [ ] **Step 4: Phone CSS**

Replace the `@media (max-width: 639px)` rail block (the one that starts `body, body.railed { grid-template-columns: minmax(0, 1fr) }`) with:

```css
@media (max-width: 639px) {
  :root {
    --strip: 48px;
  }
  body,
  body.railed {
    grid-template-columns: minmax(0, 1fr);
  }
  /* Two rows: brand + foot on the first, the six tabs on the second. The
     tab row is sticky so the section switch stays under the thumb while a
     long History scrolls; the brand row scrolls away. */
  .rail {
    position: static;
    height: auto;
    display: grid;
    grid-template-columns: 1fr auto;
    grid-template-areas:
      "brand foot"
      "tabs tabs";
    align-items: center;
    gap: 6px 8px;
    padding: 8px 12px 0;
    padding-left: max(12px, env(safe-area-inset-left));
    padding-right: max(12px, env(safe-area-inset-right));
    border-right: 0;
    border-bottom: 0;
    overflow: visible;
  }
  .railtop {
    grid-area: brand;
    min-width: 0;
  }
  .rail .brand {
    padding: 0;
  }
  .railfill {
    display: none;
  }
  .railfoot {
    grid-area: foot;
    flex-direction: row;
    padding: 0;
    gap: 8px;
  }
  .railfoot .conn .rlbl {
    display: none;
  }
  .themeseg {
    flex-direction: row;
  }
  .themebtn + .themebtn {
    border-left: 1px solid var(--line);
    border-top: 0;
  }
  #railToggle {
    display: none;
  }
  .navlist {
    grid-area: tabs;
    position: sticky;
    top: 0;
    z-index: 5;
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 0;
    height: var(--strip);
    margin: 0 -12px;
    padding: 0 max(12px, env(safe-area-inset-left)) 0 max(12px, env(safe-area-inset-right));
    background: var(--panel);
    border-bottom: 1px solid var(--line);
  }
  .nav {
    flex-direction: column;
    justify-content: center;
    gap: 2px;
    padding: 4px 0;
    border-radius: 0;
    width: auto;
  }
  .nav .rlbl {
    display: none;
  }
  .nav .navn {
    display: inline;
    font-size: 10.5px;
  }
  .nav[aria-selected="true"] {
    box-shadow: inset 0 -2px 0 var(--ink);
  }
  main {
    padding-left: max(12px, env(safe-area-inset-left));
    padding-right: max(12px, env(safe-area-inset-right));
    padding-bottom: calc(48px + env(safe-area-inset-bottom));
  }
  /* One scroll surface. */
  .scroll {
    max-height: none;
    overflow: visible;
  }
  .pagehead h1 {
    margin-bottom: 10px;
  }
  .pagehead .statsgrid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px 12px;
  }
  .pagehead .stat:not(.skel) .v {
    padding: 10px 12px;
    margin-bottom: 0;
  }
  footer {
    gap: 8px 14px;
  }
}
```

Check `--ink` exists as a token (it does; `grep -n "^  --ink:" web/app.css`). If the selected-tab indicator should use a different token, pick `--hot`.

- [ ] **Step 5: Run, restart, eyeball**

Run: `python3 tests/check_page.py && python3 -m unittest tests.test_repo_invariants -v && bun run format && bun run lint && bun run test && bun run restart`
Expected: all PASS. In the browser at 390px wide: brand row, sticky six-tab strip, six stats in two columns, page scrolls as one. At 1280px: unchanged.

- [ ] **Step 6: Stage**

```bash
git add web/index.html web/app.css tests/check_page.py tests/test_repo_invariants.py
```

---

### Task 3: Column tiers in `table()` and the phone grid rows

**Files:**
- Modify: `web/app.js:1392-1414` (`table()`)
- Modify: `web/app.js` column lists: Queue `1897-1907`, History `2173-2186`, Errors `2551-2559`, Processes `2693-2700`, Blacklist `2474-2480`, Events (`table([{ label: "Time" }, { label: "Event" }]`, ~line 2990)
- Modify: `web/app.css` phone block
- Create: `tests/test_table_tiers.ts`
- Modify: `package.json` scripts (add `"test:js:tiers": "bun tests/test_table_tiers.ts"`)
- Modify: `tests/test_repo_invariants.py::ResponsiveTiers`

**Interfaces:**
- Consumes: `el(tag, cls, text)` from `web/app.js`.
- Produces: `table(cols, rows, build, keyOf)` where `cols[i] = { label, n?, cls?, tier?: "s" }` and `keyOf(row) -> string` is optional. Every non-`.xrow` `<tr>` gets `data-key` (when `keyOf` given) and each of its cells gets `data-l` and, for `tier: "s"`, class `c-s`. Every `<table>` gets class `t-<tab>` from a module variable `tab`. Exposes `openRows` (a plain object used as a set) and `applyOpen(tr)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_table_tiers.ts` (copy the `block`/`fn`/`check` harness style from `tests/test_repaint_key.ts`):

```ts
/* Column tiers on the shared table() builder (spec 2026-09-27).
 *
 *   bun tests/test_table_tiers.ts
 *
 * On a phone each row is a grid: primary cells on one line, secondary
 * cells hidden until the row is opened. The builder is the one place that
 * knows which is which, so it must tag every cell -- a renderer that
 * appends cells positionally gets the tags for free, and a cell without
 * data-l would print no label when revealed.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");

function block(startIdx) {
  let depth = 0,
    started = false;
  for (let i = startIdx; i < src.length; i++) {
    if (src[i] === "{") {
      depth++;
      started = true;
    } else if (src[i] === "}") {
      depth--;
      if (started && depth === 0) return src.slice(startIdx, i + 1);
    }
  }
  throw new Error("unbalanced block");
}
function fn(name) {
  const at = src.indexOf("function " + name + "(");
  if (at < 0) throw new Error("not found in web/app.js: " + name);
  return block(at);
}

/* Minimal DOM: className/classList, dataset, children, cells/rows views. */
function node(tag, cls?, text?) {
  const n: any = {
    tagName: tag.toUpperCase(),
    className: cls || "",
    textContent: text == null ? "" : text,
    dataset: {},
    children: [],
    appendChild(c) {
      this.children.push(c);
      return c;
    },
  };
  n.classList = {
    add: (c) => {
      if (!n.classList.contains(c)) n.className = (n.className + " " + c).trim();
    },
    remove: (c) => {
      n.className = n.className
        .split(/\s+/)
        .filter((x) => x && x !== c)
        .join(" ");
    },
    toggle: (c, on) => {
      const has = n.classList.contains(c);
      if (on === true || (on === undefined && !has)) n.classList.add(c);
      else n.classList.remove(c);
    },
    contains: (c) => n.className.split(/\s+/).indexOf(c) >= 0,
  };
  Object.defineProperty(n, "cells", { get: () => n.children });
  return n;
}
const el = node;

const code = [
  'var tab="queue"; var openRows={};',
  fn("applyOpen"),
  fn("table"),
  "return {table:table,applyOpen:applyOpen,openRows:openRows};",
].join("\n");
const api = new Function("el", code)(el);

let failed = 0;
function check(name, cond, detail?) {
  if (cond) process.stdout.write(".");
  else {
    failed++;
    console.log("\nFAIL " + name + (detail ? "\n     " + detail : ""));
  }
}

const COLS = [
  { label: "Rank", n: true },
  { label: "Src size", n: true, tier: "s" },
  { label: "Title", cls: "title-cell" },
];
const rows = [{ t: "A", k: "a" }, { t: "B", k: "b" }];
const build = (r) => {
  const tr = el("tr");
  tr.appendChild(el("td", "n", "1"));
  tr.appendChild(el("td", "n", "10 GiB"));
  tr.appendChild(el("td", "title-cell", r.t));
  return tr;
};

/* 1. Header and cells carry tier + label. */
let t = api.table(COLS, rows, build, (r) => r.k);
const thead = t.children[0],
  tbody = t.children[1];
const ths = thead.children[0].children;
check("th secondary tagged", ths[1].classList.contains("c-s"));
check("th primary untagged", !ths[0].classList.contains("c-s") && !ths[2].classList.contains("c-s"));
check("table carries tab class", t.classList.contains("t-queue"), t.className);
const tr0 = tbody.children[0];
check("td data-l from header", tr0.children[1].dataset.l === "Src size", JSON.stringify(tr0.children[1].dataset));
check("td secondary class", tr0.children[1].classList.contains("c-s"));
check("td primary keeps its classes", tr0.children[0].className === "n");
check("row key", tr0.dataset.key === "a");

/* 2. A column without tier defaults to primary (Review Focus 4). */
check("untiered column is primary", !tr0.children[2].classList.contains("c-s"));

/* 3. Open state survives a rebuild (Review Focus 2). */
api.openRows["b"] = true;
t = api.table(COLS, rows, build, (r) => r.k);
check("open row re-opened after rebuild", t.children[1].children[1].classList.contains("open"));
check("other row closed", !t.children[1].children[0].classList.contains("open"));

/* 4. A full-width transfer row is left alone (Review Focus 1). */
const buildPair = (r) => {
  const tr = build(r);
  const x = el("tr", "xrow");
  const td = el("td");
  td.colSpan = 3;
  x.appendChild(td);
  return [tr, x];
};
api.openRows["a"] = true;
t = api.table(COLS, rows, buildPair, (r) => r.k);
const xrow = t.children[1].children[1];
check("xrow untouched", xrow.classList.contains("xrow") && !xrow.classList.contains("open") && xrow.children[0].dataset.l === undefined);
check("xrow has no key", xrow.dataset.key === undefined);

/* 5. Without keyOf nothing crashes and no key is written. */
t = api.table(COLS, rows, build);
check("no keyOf, no key", t.children[1].children[0].dataset.key === undefined);

console.log(failed ? "\n" + failed + " failed" : "\nok");
process.exit(failed ? 1 : 0);
```

- [ ] **Step 2: Run to verify it fails**

Run: `bun tests/test_table_tiers.ts`
Expected: throws `not found in web/app.js: applyOpen`.

- [ ] **Step 3: Implement the builder change**

Replace `table()` in `web/app.js` (lines 1392–1414) with:

```js
  /* Row-open state for the phone tier: which rows the operator has tapped
   open, keyed by the row's data-key. A plain object as a set, in memory
   only, and deliberately OUTSIDE every repaint key -- open is a view fact,
   not a shape. table() re-applies it on every rebuild so the 2 s repaint
   cannot fold a row the operator just opened. */
  var openRows = {};
  function applyOpen(tr) {
    var k = tr.dataset.key;
    if (k != null && openRows[k]) tr.classList.add("open");
  }

  /* cols[i] = { label, n?, cls?, tier? }. tier "s" marks a SECONDARY column:
   hidden on the phone until the row is opened. Every cell gets data-l (its
   header text) so the phone can print a label beside a revealed value, and
   secondary cells get .c-s. Rows are tagged here, positionally, so no
   renderer has to know about tiers. A row whose cell count differs from
   the column count (a full-width .xrow with one colspan cell) is left
   alone. keyOf(row) names the row for open-state persistence. */
  function table(cols, rows, build, keyOf) {
    var t = el("table", "t-" + tab),
      thead = el("thead"),
      tr = el("tr");
    cols.forEach(function (c) {
      var cls = [c.n ? "n" : "", c.cls || "", c.tier === "s" ? "c-s" : ""].join(" ").trim();
      tr.appendChild(el("th", cls || null, c.label));
    });
    thead.appendChild(tr);
    t.appendChild(thead);
    var tb = el("tbody");
    function tag(row, r) {
      if (row.classList.contains("xrow") || row.cells.length !== cols.length) return;
      for (var i = 0; i < cols.length; i++) {
        var td = row.cells[i];
        td.dataset.l = cols[i].label;
        if (cols[i].tier === "s") td.classList.add("c-s");
      }
      if (keyOf) {
        row.dataset.key = keyOf(r);
        applyOpen(row);
      }
    }
    /* build() may return one <tr> or an ARRAY of them -- the History tab gives a
     title whose file is still travelling a second, full-width row. */
    rows.forEach(function (r, i) {
      var out = build(r, i);
      if (Array.isArray(out))
        out.forEach(function (n) {
          tag(n, r);
          tb.appendChild(n);
        });
      else {
        tag(out, r);
        tb.appendChild(out);
      }
    });
    t.appendChild(tb);
    return t;
  }
```

`tab` is the module-level variable declared at `web/app.js:6`; `table()` is inside the same IIFE so it is in scope.

- [ ] **Step 4: Tier every column list and pass `keyOf`**

Queue (`web/app.js` ~1897):

```js
        [
          { label: "", cls: "gripcol" },
          { label: "Rank", n: true },
          { label: "SRC Mb/s", n: true, cls: "unit" },
          { label: "Src size", n: true, tier: "s" },
          { label: "Quality", n: true, tier: "s" },
          { label: "Title", cls: "title-cell" },
          { label: "NAS", tier: "s" },
          { label: "Src folder", tier: "s" },
          { label: "Status" },
        ],
        q,
        function (r, i) { ...unchanged... },
        function (r) {
          return r.title;
        },
```

History (~2173):

```js
        [
          { label: "#", n: true },
          { label: "Title", cls: "title-cell" },
          { label: "Original", n: true, tier: "s" },
          { label: "Output", n: true, tier: "s" },
          { label: "Saved", n: true },
          { label: "Shrink", n: true, tier: "s" },
          { label: "Quality", n: true, tier: "s" },
          { label: "Tracks", tier: "s" },
          { label: "NAS", tier: "s" },
          { label: "Moved to", tier: "s" },
          { label: "Encode time", tier: "s" },
          { label: "Finished" },
        ],
        ordered,
        function (r, i) { ...unchanged... },
        function (r) {
          return r.title + "\u0000" + (r.finished_at || "");
        },
```

Errors (~2551):

```js
        [
          { label: "State" },
          { label: "SRC Mb/s", n: true, cls: "unit", tier: "s" },
          { label: "Src size", n: true, tier: "s" },
          { label: "Title", cls: "title-cell" },
          { label: "NAS", tier: "s" },
          { label: "Src folder", tier: "s" },
          { label: "What happened" },
        ],
        rows,
        function (r) { ...unchanged... },
        function (r) {
          return r.title;
        },
```

Processes (~2693):

```js
          [
            { label: "Process" },
            { label: "PID", n: true, tier: "s" },
            { label: "Purpose" },
            { label: "Working on", tier: "s" },
            { label: "Running for", n: true, tier: "s" },
            { label: "CPU %", n: true, cls: "unit" },
          ],
          procs,
          function (p) { ...unchanged... },
          function (p) {
            return String(p.pid);
          },
```

Blacklist (~2474):

```js
        [
          { label: "Kind", tier: "s" },
          { label: "Entry", cls: "title-cell" },
          { label: "Blocks", n: true },
          { label: "Titles it blocks", cls: "title-cell", tier: "s" },
          { label: "Changed by", tier: "s" },
        ],
        rows,
        function (e) { ...unchanged... },
        function (e) {
          return e.pattern;
        },
```

Events: both columns primary; no `keyOf` (nothing to open). Leave the call as is.

- [ ] **Step 5: Phone grid CSS**

Append to the phone block in `web/app.css`:

```css
  /* ---- tables as grid rows ------------------------------------------------
     One DOM node per cell, same as the desktop: the row is a grid, primary
     cells fill the first line, secondary cells (.c-s) stay hidden until the
     row is .open and then take a full-width line each with their header
     printed from data-l. Nothing scrolls sideways, so the seam script exits
     early at this width. Per-table templates below name the PRIMARY cells
     in DOM order; ::before is the chevron and is the first grid item. */
  table,
  thead,
  tbody {
    display: block;
  }
  tr {
    display: grid;
    grid-template-columns: 18px var(--cols, minmax(0, 1fr));
    align-items: baseline;
    column-gap: 8px;
    border-bottom: 1px solid var(--td-line);
  }
  tbody tr:last-child {
    border-bottom: 0;
  }
  th,
  td {
    border-bottom: 0;
    white-space: normal;
    overflow-wrap: anywhere;
  }
  td.title-cell,
  th.title-cell {
    min-width: 0;
  }
  /* The header row sticks under the tab strip (--strip from Task 2); the
     individual th cells give up their own stickiness inside a grid row. */
  thead tr {
    background: var(--panel-2);
    position: sticky;
    top: var(--strip);
    z-index: 2;
  }
  thead tr::before {
    content: "";
  }
  th {
    position: static;
    top: auto;
  }
  /* The chevron. A 6px square rotated; turns down when the row is open. */
  tbody tr:not(.xrow)::before {
    content: "";
    width: 6px;
    height: 6px;
    margin: 6px 0 0 5px;
    border-right: 1.5px solid var(--ink-3);
    border-bottom: 1.5px solid var(--ink-3);
    transform: rotate(-45deg);
    transition: transform 0.15s;
  }
  tbody tr.open::before {
    transform: rotate(45deg);
  }
  tbody tr:not(.xrow) {
    cursor: pointer;
  }
  .c-s {
    display: none;
  }
  tr.open td.c-s {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    grid-column: 2 / -1;
    padding-top: 3px;
    padding-bottom: 3px;
    text-align: left;
  }
  tr.open td.c-s::before {
    content: attr(data-l);
    flex: none;
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-3);
  }
  /* The first revealed line gets breathing room from the primary line. */
  tr.open td.c-s:first-of-type {
    padding-top: 8px;
  }
  /* Transfer rows span the whole width, chevron column included. */
  tr.xrow {
    grid-template-columns: minmax(0, 1fr);
  }
  tr.xrow td {
    grid-column: 1 / -1;
  }
  /* Drag handles are meaningless on a phone whatever the pointer. */
  th.gripcol,
  td.gripcol {
    display: none;
  }
  /* Per-table primary templates, DOM order of the primary cells. */
  .t-queue tr {
    --cols: 3ch 5ch minmax(0, 1fr) auto;
  }
  .t-ledger tr {
    --cols: 3ch minmax(0, 1fr) 9ch 8ch;
  }
  .t-errors tr {
    --cols: auto minmax(0, 1fr) minmax(0, 1.2fr);
  }
  .t-procs tr {
    --cols: minmax(0, 1fr) minmax(0, 1fr) 6ch;
  }
  .t-blacklist tr {
    --cols: minmax(0, 1fr) 6ch;
  }
  .t-events tr {
    --cols: 8ch minmax(0, 1fr);
  }
```

Add to `ResponsiveTiers` in `tests/test_repo_invariants.py` (Review Focus 5):

```python
    def test_grid_rows_live_only_in_the_phone_block(self):
        css = re.sub(r"/\*.*?\*/", " ", read("web/app.css"), flags=re.S)
        dense = re.sub(r"\s*([{}:;>,])\s*", r"\1", css)
        hits = [m.start() for m in re.finditer(r"(^|[}\s;])tr\{display:grid", dense)]
        self.assertTrue(hits, "phone grid-row rule missing")
        start = dense.index("@media(max-width:639px)")
        end = dense.index("@media(min-width:640px)", start)
        for h in hits:
            self.assertTrue(start < h < end, "tr{display:grid} outside the phone block breaks every desktop table")
```

- [ ] **Step 6: Gate the seam script**

In the seam IIFE in `web/app.js` (~line 3286, `function recut()`), add as the first lines of `recut()`:

```js
      /* Phone rows are grids; nothing scrolls sideways and nothing straddles. */
      if (PHONE.matches) {
        if (cuts.length) {
          var tb0 = pane.querySelector("table");
          if (tb0) cuts.forEach(function (c) { applyCut(tb0, c, false); });
        }
        cuts = [];
        return;
      }
```

and declare, just above the seam IIFE:

```js
  /* The phone tier, as the CSS defines it. One MediaQueryList shared by the
   seam script and the row disclosure so the two can never disagree. */
  var PHONE = window.matchMedia ? window.matchMedia("(max-width: 639px)") : { matches: false };
```

- [ ] **Step 7: Run, format, gate, restart**

Run: `bun tests/test_table_tiers.ts && python3 -m unittest tests.test_repo_invariants.ResponsiveTiers -v && bun run format && bun run lint && bun run test && bun run restart`
Expected: PASS. Add the script line to `package.json`:

```json
    "test:js:tiers": "bun tests/test_table_tiers.ts",
```

At 390px: each tab shows its primary cells one row per title with a chevron; nothing scrolls sideways. Rows cannot be opened yet (Task 4). At 1280px: unchanged (verify History and Queue, including the encoding row's bar and CRF picker).

- [ ] **Step 8: Stage**

```bash
git add web/app.js web/app.css tests/test_table_tiers.ts tests/test_repo_invariants.py package.json
```

---

### Task 4: Row disclosure on the phone

**Files:**
- Modify: `web/app.js` (new IIFE next to the seam script, after `PHONE`)
- Modify: `tests/test_table_tiers.ts` (append section 6)

**Interfaces:**
- Consumes: `openRows`, `applyOpen`, `PHONE` from Task 3.
- Produces: `rowToggle(ev)` — the delegated click handler; exported for the test through the same `new Function` trick.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_table_tiers.ts` before the final `console.log`:

```ts
/* 6. The disclosure handler (Review Focus 1 and 3). */
const code2 = [
  'var tab="queue"; var openRows={}; var PHONE={matches:true};',
  fn("applyOpen"),
  fn("rowToggle"),
  "return {rowToggle:rowToggle,openRows:openRows,PHONE:PHONE};",
].join("\n");
const d = new Function("el", code2)(el);

function fakeRow(cls, key) {
  const tr = node("tr", cls);
  if (key != null) tr.dataset.key = key;
  return tr;
}
function fakeEvent(target, tr) {
  /* closest() walks target -> tr; the interactive test puts a button
     between them. */
  return {
    target: {
      closest(sel) {
        if (sel === "tr") return tr;
        return target.interactive && sel.indexOf("button") >= 0 ? target : null;
      },
    },
  };
}
let tr = fakeRow("", "k1");
d.rowToggle(fakeEvent({}, tr));
check("tap opens the row", tr.classList.contains("open") && d.openRows["k1"] === true);
d.rowToggle(fakeEvent({}, tr));
check("second tap closes it", !tr.classList.contains("open") && !d.openRows["k1"]);

tr = fakeRow("", "k2");
d.rowToggle(fakeEvent({ interactive: true }, tr));
check("a click on a control does not toggle", !tr.classList.contains("open"));

const x = fakeRow("xrow", null);
d.rowToggle(fakeEvent({}, x));
check("xrow never opens", !x.classList.contains("open"));

d.PHONE.matches = false;
tr = fakeRow("", "k3");
d.rowToggle(fakeEvent({}, tr));
check("no-op above the phone width", !tr.classList.contains("open"));
```

- [ ] **Step 2: Run to verify it fails**

Run: `bun tests/test_table_tiers.ts`
Expected: throws `not found in web/app.js: rowToggle`.

- [ ] **Step 3: Implement**

Add to `web/app.js` directly after the `PHONE` declaration from Task 3:

```js
  /* Row disclosure (phone tier only). A tap anywhere on a row that is not a
   control toggles its secondary cells. Controls keep their own meaning: a
   tap on the CRF picker, a skip button or the Source-folder link must never
   also fold the row underneath it. The transfer .xrow has nothing to
   reveal. Open rows are remembered by key so the repaint keeps them. */
  function rowToggle(ev) {
    if (!PHONE.matches) return;
    var t = ev.target;
    if (t.closest("button, select, a, input, label, .crfcell")) return;
    var tr = t.closest("tr");
    if (!tr || tr.classList.contains("xrow") || tr.dataset.key == null) return;
    var on = !tr.classList.contains("open");
    tr.classList.toggle("open", on);
    if (on) openRows[tr.dataset.key] = true;
    else delete openRows[tr.dataset.key];
  }
  document.getElementById("pane").addEventListener("click", rowToggle);
```

Note the header row has no `data-key`, so tapping a header does nothing.

- [ ] **Step 4: Run, gate, restart, eyeball**

Run: `bun tests/test_table_tiers.ts && bun run format && bun run lint && bun run test && bun run restart`
Expected: PASS. At 390px on the Queue: tap a row, the Src size, Quality (with the picker), NAS and Src folder lines appear beneath; tap the picker, the row stays open; wait for a repaint (2 s), the row stays open; tap the chevron area again, it folds. On History a title with a moving transfer shows the bar at full width under the row.

- [ ] **Step 5: Stage**

```bash
git add web/app.js tests/test_table_tiers.ts
```

---

### Task 5: Tablet edge fades and wrapping columns

**Files:**
- Modify: `web/app.js` seam IIFE (`recut()`), to toggle `.can-left` / `.can-right` on `#tablewrap`
- Modify: `web/app.css` tablet block; add two tokens to both themes
- Modify: `tests/test_repo_invariants.py::ResponsiveTiers`

**Interfaces:**
- Consumes: `pane` (the `.scroll` element) in the seam IIFE.
- Produces: tokens `--fade-from` (panel colour, opaque) and `--fade-to` (panel colour, transparent) in both themes.

- [ ] **Step 1: Write the failing test**

Add to `ResponsiveTiers`:

```python
    def test_tablet_edge_fade_tokens_exist_in_both_themes(self):
        css = read("web/app.css")
        dark = css[: css.index('[data-theme="light"]')]
        light = css[css.index('[data-theme="light"]') :]
        for tok in ("--fade-from:", "--fade-to:"):
            self.assertIn(tok, dark, tok + " missing from the dark theme")
            self.assertIn(tok, light, tok + " missing from the light theme")
        dense = re.sub(r"\s*([{}:;>,])\s*", r"\1", css)
        self.assertIn("#tablewrap.can-right>.wrap::after", dense)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_repo_invariants.ResponsiveTiers -v`
Expected: FAIL on `--fade-from:`.

- [ ] **Step 3: Tokens and CSS**

In `:root` (dark, after `--line` at ~line 18) add:

```css
  --fade-from: rgba(20, 22, 29, 1);
  --fade-to: rgba(20, 22, 29, 0);
```

In `:root[data-theme="light"]` (after `--line` at ~line 92) add:

```css
  --fade-from: rgba(255, 255, 255, 1);
  --fade-to: rgba(255, 255, 255, 0);
```

(These are `--panel` in each theme, split into an opaque and a transparent stop, because a gradient cannot take a hex token and an alpha separately.)

Append to the tablet block:

```css
  /* Overflow cue. The scrollbar is hidden on purpose (see .scroll), so a
     table wider than the pane needs another way to say "there is more":
     a fade over whichever edge still has room to scroll. The seam script
     already measures the pane on scroll and resize and sets these classes. */
  #tablewrap > .wrap {
    position: relative;
  }
  #tablewrap.can-right > .wrap::after,
  #tablewrap.can-left > .wrap::before {
    content: "";
    position: absolute;
    top: 0;
    bottom: 0;
    width: 28px;
    pointer-events: none;
    z-index: 4;
  }
  #tablewrap.can-right > .wrap::after {
    right: 0;
    background: linear-gradient(to left, var(--fade-from), var(--fade-to));
  }
  #tablewrap.can-left > .wrap::before {
    left: 190px; /* clear of the pinned title column */
    background: linear-gradient(to right, var(--fade-from), var(--fade-to));
  }
  /* Long text columns wrap on a tablet rather than pushing the table wider. */
  td.src-dir,
  td.evtext,
  td.what {
    white-space: normal;
    max-width: 32ch;
  }
```

Check which class the Errors "What happened" cell and the Processes "Working on" cell carry (`grep -n '"what\|evtext\|q-what' web/app.js`). If they have none, give them `what` in their renderers (Errors ~line 2600, Processes ~line 2717): `el("td", "what", ...)`.

- [ ] **Step 4: Seam script sets the classes**

In `recut()` after the phone early-exit, add before the `var tbl = ...` line:

```js
      var tw = document.getElementById("tablewrap");
      if (tw) {
        var over = pane.scrollWidth - pane.clientWidth;
        tw.classList.toggle("can-left", over > 1 && pane.scrollLeft > 1);
        tw.classList.toggle("can-right", over > 1 && pane.scrollLeft < over - 1);
      }
```

- [ ] **Step 5: Run, gate, restart, eyeball**

Run: `python3 -m unittest tests.test_repo_invariants -v && bun run format && bun run lint && bun run test && bun run restart`
Expected: PASS. At 820px on History: a fade on the right; scroll right, a fade appears on the left just past the pinned title; at the far right the right fade is gone. Long "Src folder" values wrap. At 1280px: no fades, no wrapping.

- [ ] **Step 6: Stage**

```bash
git add web/app.js web/app.css tests/test_repo_invariants.py
```

---

### Task 6: Live card, monitor tooltip, touch targets

**Files:**
- Modify: `web/app.css` phone block and the coarse-pointer block at ~line 2756
- Modify: `web/app.js:4338-4344` (tooltip clamp)
- Modify: `tests/test_sysmon_ui.ts` (one check for the clamp) — read its harness first; if the tooltip placement is not exposed as a function, extract the two clamp lines into `function monTipPos(cx, cy, cr)` returning `{lx, ly}` and test that.

**Interfaces:**
- Produces: `monTipPos(cx, cy, cr) -> {lx, ly}` where `cr` is the monitor card's client rect.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sysmon_ui.ts`, using its existing `fn` helper and its assertion helper (read the top of the file; it is `check(name, cond, detail?)` in the sibling suites):

```ts
/* Tooltip clamp: on a 360px card the box must not hang past either edge. */
const posSrc = fn("monTipPos");
const monTipPos = new Function(posSrc + "\nreturn monTipPos;")();
const cr = { left: 0, top: 0, width: 360, height: 300 };
let p = monTipPos(340, 40, cr);
check("tip clamps at the right edge", p.lx + 180 <= 360 && p.lx >= 4, JSON.stringify(p));
p = monTipPos(10, 40, cr);
check("tip clamps at the left edge", p.lx >= 4, JSON.stringify(p));
p = monTipPos(200, 290, cr);
check("tip clamps at the bottom edge", p.ly + 150 <= 300, JSON.stringify(p));
```

- [ ] **Step 2: Run to verify it fails**

Run: `bun tests/test_sysmon_ui.ts`
Expected: throws `not found in web/app.js: monTipPos`.

- [ ] **Step 3: Implement the clamp**

In `web/app.js` replace the four lines at ~4338–4341:

```js
    var cr = monCard.getBoundingClientRect();
    var lx = monHover.cx - cr.left + 14,
      ly = monHover.cy - cr.top + 10;
    if (lx + 180 > cr.width) lx = Math.max(4, monHover.cx - cr.left - 194);
```

with:

```js
    var cr = monCard.getBoundingClientRect();
    var pos = monTipPos(monHover.cx, monHover.cy, cr);
    var lx = pos.lx,
      ly = pos.ly;
```

and add, above `function drawMonTip` (or whatever the enclosing function is named; find it with `grep -n "monTipEl.style.left" web/app.js` and walk up), the pure helper:

```js
  /* Where the tooltip box goes, relative to the monitor card. 180x150 is the
   box's outer size. It sits right-and-below the cursor, flips left when that
   would run off the card, and on a phone -- where the card is narrower than
   two boxes -- it is clamped inside on both axes rather than flipped. */
  function monTipPos(cx, cy, cr) {
    var W = 180,
      H = 150;
    var lx = cx - cr.left + 14,
      ly = cy - cr.top + 10;
    if (lx + W > cr.width) lx = cx - cr.left - W - 14;
    if (lx < 4) lx = Math.max(4, Math.min(cx - cr.left - W / 2, cr.width - W - 4));
    if (ly + H > cr.height) ly = Math.max(4, cr.height - H - 4);
    return { lx: lx, ly: ly };
  }
```

- [ ] **Step 4: CSS**

Append to the phone block:

```css
  .ring {
    width: 112px;
    height: 112px;
  }
  .ring-mid .pctbig {
    font-size: 22px;
  }
  .hero-body {
    flex-basis: 100%;
  }
  .kv {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .monmini canvas {
    width: 48px;
  }
```

(Remove the earlier `.ring { width: 132px }` and `.ring-mid .pctbig { font-size: 24px }` lines that Task 1 moved into this block, so the block does not set them twice.)

Append to the last `@media (pointer: coarse)` block (~line 2756):

```css
  .act,
  .panebtn,
  .crfsel {
    min-height: 44px;
  }
  tbody tr:not(.xrow) > td:first-child {
    min-height: 44px;
  }
```

- [ ] **Step 5: Run, gate, restart, eyeball**

Run: `bun tests/test_sysmon_ui.ts && bun run format && bun run lint && bun run test && bun run restart`
Expected: PASS. At 390px: the live card ring is small with the fields below in two columns; hover/tap the charts near the right edge, the tooltip stays inside the card. At 1280px: tooltip behaves as before.

- [ ] **Step 6: Stage**

```bash
git add web/app.js web/app.css tests/test_sysmon_ui.ts
```

---

### Task 7: Screenshot harness presets and the acceptance pass

**Files:**
- Modify: `tests/visual/shoot.mts` (`--viewport`, `--tab`, `--target` flags; seed a ledger fixture)
- Modify: `tests/visual/seed_ring.py` only if it owns the temp dir contents (read it first; if it only writes `sysmon.ring`, copy the ledger from `shoot.mts`)

**Interfaces:**
- Consumes: `tests/fixtures/ledger-calibration.jsonl`; the page's `?tab=` URL parameter (`tabFromUrl()` in `web/app.js:3209`).
- Produces: `bun run visual -- --viewport phone --tab ledger --target tablewrap` writes `<theme>-<viewport>-<tab>.png`.

- [ ] **Step 1: Add the flags and presets**

Near the other `arg()` reads in `tests/visual/shoot.mts` (~line 40):

```ts
const VIEWPORTS = {
  desktop: { width: 1280, height: 1400, deviceScaleFactor: 2, mobile: false },
  tablet: { width: 820, height: 1180, deviceScaleFactor: 2, mobile: true },
  phone: { width: 390, height: 844, deviceScaleFactor: 3, mobile: true },
};
const viewportName = arg("--viewport", "desktop");
const viewport = VIEWPORTS[viewportName];
if (!viewport) die("--viewport must be one of " + Object.keys(VIEWPORTS).join(", "));
/* --tab shoots the table section of one tab instead of the monitor stops. */
const onlyTab = arg("--tab", null);
const target = arg("--target", onlyTab ? "tablewrap" : "sysmon");
```

Replace the literal `width: 1280, height: 1400, deviceScaleFactor: 2, mobile: false` in the `Emulation.setDeviceMetricsOverride` call with `...viewport`.

- [ ] **Step 2: Seed a ledger so History has rows**

After `seed_ring.py` runs (~line 78) add:

```ts
/* The tables need rows. The calibration ledger is the fixture with the
 * most shapes (exact and estimated sizes, notes, several destinations). */
fs.copyFileSync(
  path.join(REPO, "tests", "fixtures", "ledger-calibration.jsonl"),
  path.join(smeltrDir, "ledger.jsonl"),
);
```

- [ ] **Step 3: Navigate to the tab and shoot the target**

Change `Page.navigate` to append the tab: `url: onlyTab ? pageUrl + "&tab=" + onlyTab : pageUrl`. (The url file already carries `?t=<token>`, so `&` is right.)

Wrap the zoom-stop loop: when `onlyTab` is set, skip the stops loop and shoot once per theme:

```ts
  if (onlyTab) {
    await waitFor(cdp, sessionId, '!!document.querySelector("#pane table")', "the " + onlyTab + " table");
    await sleep(300);
    const box = await evaluate(
      cdp,
      sessionId,
      `(function(){var r=document.getElementById(${JSON.stringify(target)}).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};})()`,
    );
    const shot = await cdp.send<{ data: string }>(
      "Page.captureScreenshot",
      { format: "png", captureBeyondViewport: true, clip: { ...box, scale: viewport.deviceScaleFactor } },
      sessionId,
    );
    const name = `${theme}-${viewportName}-${onlyTab}.png`;
    fs.writeFileSync(path.join(outDir, name), Buffer.from(shot.data, "base64"));
    shots.push({ theme, label: `${viewportName} ${onlyTab}`, file: path.join(outDir, name) });
    console.log("  shot  " + name);
    continue;
  }
```

For the phone, also shoot with the first row opened: before the capture, when `viewportName === "phone"`, run

```ts
    await evaluate(cdp, sessionId, '(function(){var r=document.querySelector("#pane tbody tr:not(.xrow)");if(r)r.click();return true;})()');
    await sleep(200);
```

- [ ] **Step 4: Run the acceptance matrix**

```bash
for v in phone tablet desktop; do
  for t in ledger errors procs blacklist events queue; do
    bun run visual -- --viewport $v --tab $t --out /tmp/smeltr-visual/$v || exit 1
  done
done
bun run visual -- --viewport phone --out /tmp/smeltr-visual/phone-monitor
```

Expected: PNGs for every pair. Open them (`open /tmp/smeltr-visual/phone/*.png`). The Queue and Errors tabs will be empty against the throwaway server (no staging drive); the operator checks those two on the live dashboard at `bun run url` from a phone. Fix anything the screenshots show, re-running the relevant task's tests.

- [ ] **Step 5: Lint and gate**

Run: `bun run lint && bun run test`
Expected: PASS (`lint:js:syntax` and `lint:ts` cover `tests/visual/*.mts`).

- [ ] **Step 6: Stage**

```bash
git add tests/visual/shoot.mts
```

---

## Final checks before handing back to the operator

- [ ] `bun run verify` passes.
- [ ] Desktop at 1280px and 1920px: side-by-side with `main` (open `git stash`-free: run `git worktree add /tmp/smeltr-main main` and its server on another port via `SMELTR_BIND`), no visible change on any tab.
- [ ] Phone at 390px: every tab, rows open and close, a repaint keeps them open, pickers and buttons work inside opened rows, the transfer bar is full width, the tab strip stays put while scrolling.
- [ ] Tablet at 820px: pinned title, edge fades, seam blanking still blanks a sliced column.
- [ ] Both themes at each width.
- [ ] Report to the operator with the screenshot folder path and the list of staged files. Do not commit.
