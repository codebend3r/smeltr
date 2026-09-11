/* The resource-monitor chart math, run against the real functions in
 * web/app.js (same extraction trick as test_repaint_key.js):
 *
 *   bun tests/test_sysmon_ui.js
 *
 * Pins:
 *   - the zoom stops: ends (15 min .. 7 d), strict monotonicity, snapping
 *     (every slider position is one of the twelve named windows), and the
 *     clamp on an out-of-range position
 *   - decimation: a one-second spike must survive into a bucket's MAX
 *     (averaging alone would erase it at 7 d zoom)
 *   - a missing second is a NaN bucket -- a GAP, never an interpolation
 *   - a stale ring slot (a ts one full ring older, in the same index)
 *     never renders
 *   - NaN samples (metric unreadable) are skipped, not zeroed
 *   - throughput axis nicing (1/2/5 steps), and the honest formatting of
 *     missing values as an em dash
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

const code = [
  "var MON_SLOTS=604800, MIB=1048576;",
  /* MON_STOPS is the table monSpan() indexes; pull it out of app.js too so
     the stops under test are the stops the page ships. */
  src.match(/var MON_STOPS\s*=\s*\[[^\]]*\];/)[0],
  fn("monSpan"),
  fn("spanLabel"),
  fn("niceMax"),
  fn("monTicks"),
  fn("monBuckets"),
  fn("monFmtPct"),
  fn("monFmtMibs"),
  fn("axLab"),
].join("\n");
/* eslint-disable no-eval */
const get = new Function(
  code +
    `
  return {monSpan, spanLabel, niceMax, monTicks, monBuckets,
          monFmtPct, monFmtMibs, axLab, MON_STOPS};`,
)();

let section = "";
const sect = (s) => {
  section = s;
};
let failures = 0;
function ok(cond, msg?) {
  if (cond) {
    process.stdout.write(".");
  } else {
    failures++;
    console.log("\nFAIL [" + section + "] " + msg);
    process.exit(1);
  }
}
function eq(a, b, msg?) {
  ok(Object.is(a, b), msg + ` (got ${a}, want ${b})`);
}

sect("zoom stops");
const STOPS = get.MON_STOPS;
eq(get.monSpan(0), 900, "slider 0 is exactly 15 min");
eq(get.monSpan(STOPS.length - 1), 604800, "the last stop is exactly 7 d");
eq(get.monSpan(7), 86400, "the default stop (7) is exactly 24 h");
let mono = true;
for (let p = 1; p < STOPS.length; p++) if (get.monSpan(p) <= get.monSpan(p - 1)) mono = false;
ok(mono, "span is strictly increasing across the slider");
/* The point of the change: NO position produces an unnamed window. */
let snapped = true;
for (let p = 0; p < STOPS.length; p++) if (!STOPS.includes(get.monSpan(p))) snapped = false;
ok(snapped, "every slider position lands on a named stop");
ok(
  STOPS.every((v) => v <= 604800),
  "no stop reaches past the 7 d ring",
);
eq(get.monSpan(-3), 900, "a position below the track clamps to 15 min");
eq(get.monSpan(999), 604800, "a position past the track clamps to 7 d");
eq(get.monSpan(NaN), 900, "a NaN position clamps rather than yielding undefined");
eq(get.spanLabel(900), "15 min", "15 min label");
eq(get.spanLabel(1800), "30 min", "30 min label");
eq(get.spanLabel(3600), "1 h", "1 h label");
eq(get.spanLabel(86400), "1 d", "a full day labels in days, not 24 h");
eq(get.spanLabel(604800), "7 d", "7 d label");
/* Every stop must have a label a human reads as a window, not a rounding. */
ok(
  STOPS.every((v) => /^\d+(\.\d)? (min|h|d)$/.test(get.spanLabel(v))),
  "every stop labels as a whole named window",
);

sect("axis nicing");
eq(get.niceMax(3.2), 5, "3.2 -> 5");
eq(get.niceMax(17), 20, "17 -> 20");
eq(get.niceMax(50), 50, "50 -> 50");
eq(get.niceMax(51), 100, "51 -> 100");
eq(get.niceMax(0), 1, "no data floors at 1, never 0 (a 0-height axis lies)");
ok(
  [900, 1800, 3600, 7200, 10800].includes(get.monTicks(86400)),
  "tick step is a clock-round interval",
);
/* A tick step must divide the window into a readable number of gridlines:
   too few and the axis carries no scale, too many and the labels collide. */
STOPS.forEach((v) => {
  const n = v / get.monTicks(v);
  ok(n >= 3 && n <= 16, `${get.spanLabel(v)} window draws ${n} gridlines (3..16)`);
});
eq(get.monTicks(604800), 86400, "the 7 d window ticks once per day");
eq(get.monTicks(900), 300, "the 15 min window ticks every 5 min");

sect("decimation");
const SLOTS = 604800;
function ring(samples) {
  const ts = new Float64Array(SLOTS),
    v = new Float32Array(SLOTS).fill(NaN);
  for (const [t, val] of samples) {
    ts[t % SLOTS] = t;
    v[t % SLOTS] = val;
  }
  return { ts, v };
}
const T0 = 1700000000;
{
  /* 100 s window, 10 buckets: a single 1 s spike of 100 among 5s */
  const samples = [];
  for (let s = T0; s < T0 + 100; s++) samples.push([s, s === T0 + 42 ? 100 : 5]);
  const r = ring(samples);
  const b = get.monBuckets(r.ts, r.v, T0, T0 + 100, 10);
  eq(b.hi[4], 100, "one-second spike survives as the bucket MAX");
  ok(b.avg[4] > 5 && b.avg[4] < 100, "mean line moves but does not equal the spike");
  eq(b.lo[4], 5, "bucket MIN keeps the floor");
}
{
  /* a 20 s hole in the middle: bucket must be NaN, not bridged */
  const samples = [];
  for (let s = T0; s < T0 + 100; s++) if (s < T0 + 40 || s >= T0 + 60) samples.push([s, 10]);
  const r = ring(samples);
  const b = get.monBuckets(r.ts, r.v, T0, T0 + 100, 5);
  ok(Number.isNaN(b.avg[2]), "a sampler outage renders as a gap (NaN bucket)");
  ok(!Number.isNaN(b.avg[1]) && !Number.isNaN(b.avg[3]), "neighbours still draw");
}
{
  /* stale slot: a ts one full ring older occupies the same index */
  const r = ring([[T0 - SLOTS + 10, 99]]); // same slot index as T0+10
  const b = get.monBuckets(r.ts, r.v, T0, T0 + 100, 5);
  ok(Number.isNaN(b.avg[0]), "a sample one full ring old never renders in its reused slot");
}
{
  /* NaN sample (metric unreadable) is skipped, not treated as 0 */
  const r = ring([
    [T0 + 1, NaN],
    [T0 + 2, 50],
  ]);
  const b = get.monBuckets(r.ts, r.v, T0, T0 + 10, 1);
  eq(b.avg[0], 50, "NaN sample does not drag the mean toward 0");
  eq(b.n[0], 1, "NaN sample does not count");
}

sect("formatting");
eq(get.monFmtPct(null), "—", "missing % is an em dash, never 0%");
eq(get.monFmtPct(NaN), "—", "NaN % is an em dash");
eq(get.monFmtPct(43.4), "43%", "percent rounds whole");
eq(get.monFmtMibs(null), "—", "missing rate is an em dash, never 0");
eq(get.monFmtMibs(12.6 * 1048576), "13 MiB/s", "rates >= 10 round whole");
eq(get.monFmtMibs(3.27 * 1048576), "3.3 MiB/s", "rates < 10 keep one decimal");
eq(get.monFmtMibs(50000), "49 KiB/s", "a live 50 KiB/s trickle never prints as 0 MiB/s");
eq(get.monFmtMibs(0), "0 KiB/s", "a true zero is 0, distinguishable from — (unreadable)");
eq(get.axLab(12.4), 12, "axis label >= 10 rounds whole");
eq(get.axLab(2.5), 2.5, "axis label < 10 keeps one decimal");

sect("folded head");
/* The collapsed card shows CPU, GPU and RAM, each a sparkline plus the
   latest reading. The markup is pinned to the Utilization chart's series
   list by data-k, so a series added or reordered there fails here instead
   of drawing one line under another's label. */
{
  const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
  const start = html.indexOf('id="monMini"');
  const mini = html.slice(start, html.indexOf('class="monnote"', start));
  const items = Array.from(
    mini.matchAll(
      /class="monmini-s" data-k="(\d+)"><canvas[^>]*><\/canvas><span>(\w+) <b class="num">/g,
    ),
  ).map((m) => ({ k: +m[1], label: m[2] }));
  const util = src.slice(src.indexOf("var MON_CHARTS"), src.indexOf('cv: "monN"'));
  const series = Array.from(
    util.matchAll(/\{ k: (\d+), tok: "--ch-\d", cls: "ch\d", label: "(\w+)" \}/g),
  ).map((m) => ({ k: +m[1], label: m[2] }));
  eq(series.length, 3, "the Utilization chart draws three series");
  eq(
    JSON.stringify(items),
    JSON.stringify(series),
    "folded head carries every Utilization series, same key, same label, same order",
  );
  ok(
    /MON_CHARTS\[0\]\.series\.map/.test(src),
    "MON_MINI is built from the chart's own series list",
  );
  ok(/monCss\(m\.se\.tok\)/.test(src), "each sparkline draws in its series' own colour token");
  ok(/monLast\.v\[m\.se\.k\]/.test(src), "each reading is that series' latest sample");
}

if (failures) {
  console.log("\n" + failures + " FAILURE(S)");
  process.exit(1);
}
console.log("\nsysmon ui: all passed");
