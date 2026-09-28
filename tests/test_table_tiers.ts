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
      c.parentNode = this;
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
  /* A real (if minimal) Element.closest: walks the node itself, then each
     parentNode, matching a bare tag name or a .class against a
     comma-separated selector list. Dropping a selector from the real code's
     `closest()` call must fail a test here, not just look like it works. */
  n.closest = (selectorList) => {
    const sels = selectorList.split(",").map((s) => s.trim());
    const matches = (node, sel) =>
      sel[0] === "." ? node.classList.contains(sel.slice(1)) : node.tagName.toLowerCase() === sel;
    let cur = n;
    while (cur) {
      if (sels.some((sel) => matches(cur, sel))) return cur;
      cur = cur.parentNode;
    }
    return null;
  };
  return n;
}
const el = node;

const code = [
  'var tab="queue";',
  /* The real declaration, so test 7 pins the page's own set, not a stand-in. */
  src.match(/var openRows = [^;]+;/)[0],
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
const rows = [
  { t: "A", k: "a" },
  { t: "B", k: "b" },
];
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
check(
  "th primary untagged",
  !ths[0].classList.contains("c-s") && !ths[2].classList.contains("c-s"),
);
check("table carries tab class", t.classList.contains("t-queue"), t.className);
const tr0 = tbody.children[0];
check(
  "td data-l from header",
  tr0.children[1].dataset.l === "Src size",
  JSON.stringify(tr0.children[1].dataset),
);
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
check(
  "xrow untouched",
  xrow.classList.contains("xrow") &&
    !xrow.classList.contains("open") &&
    xrow.children[0].dataset.l === undefined,
);
check("xrow has no key", xrow.dataset.key === undefined);

/* 4b. The class alone skips an .xrow, even one with a full set of cells. */
const buildFull = (r) => {
  const tr = build(r);
  const x = el("tr", "xrow");
  x.appendChild(el("td", null, "a"));
  x.appendChild(el("td", null, "b"));
  x.appendChild(el("td", null, "c"));
  return [tr, x];
};
t = api.table(COLS, rows, buildFull, (r) => r.k);
const xfull = t.children[1].children[1];
check(
  "full-width xrow skipped by class",
  xfull.children[1].dataset.l === undefined &&
    !xfull.children[1].classList.contains("c-s") &&
    xfull.dataset.key === undefined,
);

/* 5. Without keyOf nothing crashes and no key is written. */
t = api.table(COLS, rows, build);
check("no keyOf, no key", t.children[1].children[0].dataset.key === undefined);

/* 6. The row build() returned is the very node in the tbody, cells and all:
   progRefs holds references into these cells, so a clone or rebuild inside
   table() would leave every in-place progress update writing to a ghost. */
const built = [];
const buildKeep = (r) => {
  const tr = build(r);
  built.push({ tr: tr, td: tr.children[1] });
  return tr;
};
t = api.table(COLS, rows, buildKeep, (r) => r.k);
check(
  "row node identity",
  t.children[1].children[0] === built[0].tr && t.children[1].children[1] === built[1].tr,
);
check("cell node identity", t.children[1].children[0].children[1] === built[0].td);

/* 7. A prototype key is not an open row. */
t = api.table(COLS, [{ t: "C", k: "constructor" }], build, (r) => r.k);
check("constructor title not open", !t.children[1].children[0].classList.contains("open"));

/* 8. The disclosure handler (Review Focus 1 and 3). The fake DOM's
   closest() above is a REAL minimal implementation -- tag or .class,
   walking parentNode -- so dropping a selector from the real
   rowToggle()'s guard would fail a case here, not just look like it
   works. */
const code2 = [
  'var tab="queue"; var openRows=Object.create(null); var PHONE={matches:true};',
  fn("applyOpen"),
  fn("rowToggle"),
  "return {rowToggle:rowToggle,openRows:openRows,PHONE:PHONE};",
].join("\n");
const d = new Function("el", code2)(el);

function keyedRow(key) {
  const tr = node("tr");
  tr.dataset.key = key;
  return tr;
}
function cellWith(tr, child) {
  const td = node("td");
  tr.appendChild(td);
  if (child) td.appendChild(child);
  return child || td;
}

let tr = keyedRow("k1");
const td1 = cellWith(tr, null);
d.rowToggle({ target: td1 });
check(
  "a click on a td directly opens the row",
  tr.classList.contains("open") && d.openRows["k1"] === true,
);
d.rowToggle({ target: td1 });
check("second tap on the td closes it", !tr.classList.contains("open") && !d.openRows["k1"]);

tr = keyedRow("k2");
const btn = cellWith(tr, node("button"));
d.rowToggle({ target: btn });
check("a click on a button does not toggle", !tr.classList.contains("open"));

tr = keyedRow("k3");
const sel = cellWith(tr, node("select"));
d.rowToggle({ target: sel });
check("a click on a select does not toggle", !tr.classList.contains("open"));

tr = keyedRow("k4");
const crfcell = cellWith(tr, node("span", "crfcell"));
d.rowToggle({ target: crfcell });
check("a click on .crfcell does not toggle", !tr.classList.contains("open"));

tr = keyedRow("k5");
const inp = cellWith(tr, node("input"));
d.rowToggle({ target: inp });
check("a click on an input does not toggle", !tr.classList.contains("open"));

tr = keyedRow("k6");
const span = cellWith(tr, node("span"));
d.rowToggle({ target: span });
check("a click on a plain span still opens the row", tr.classList.contains("open"));

const x = node("tr", "xrow");
const xtd = cellWith(x, null);
d.rowToggle({ target: xtd });
check("xrow never opens", !x.classList.contains("open"));

d.PHONE.matches = false;
tr = keyedRow("k7");
const td7 = cellWith(tr, null);
d.rowToggle({ target: td7 });
check("no-op above the phone width", !tr.classList.contains("open"));

console.log(failed ? "\n" + failed + " failed" : "\nok");
process.exit(failed ? 1 : 0);
