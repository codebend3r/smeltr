/* What the monitor charts actually DRAW, at every one of the twelve zoom
 * stops, run against the real drawMon() pulled out of web/app.js:
 *
 *   bun tests/test_sysmon_render.js
 *
 * The other two bun suites test the chart's arithmetic. This one tests the
 * picture: drawMon() is handed a recording 2D context and the ops it emits
 * are asserted directly, so a timeframe that renders as a line hanging off
 * the right-hand edge -- or as a plausible-looking flat chart that is really
 * a wiped ring -- fails here instead of in a screenshot nobody took.
 *
 * Pins:
 *   - every stop draws a line across the FULL plot width, never a stub at
 *     one edge (the 2026-08-29 7 d widening rendered 55 of 60 minutes as
 *     bare wash with the data crushed into the last 8% of the canvas)
 *   - the no-history stretch rides the 0 baseline AND keeps its --skel
 *     wash: the zeros are drawn so the chart reads as one series, the wash
 *     is what says they were never measured. Both, or neither is honest.
 *   - a wash is drawn if and ONLY if the window reaches past the oldest
 *     held sample, and its width is that overhang to the pixel
 *   - a gap INSIDE the history still breaks the path -- the baseline is a
 *     statement about how long we have been recording, not a licence to
 *     bridge a second the sampler missed
 *   - gridlines stay legible at every stop (3..16 of them) and no two tick
 *     labels collide, which is what forces the weekday past 24 h
 *   - the mean line is smoothed ONLY where a bucket aggregates 4+ samples:
 *     at the narrow stops the min-max shade collapses to ~1 px of 16% alpha,
 *     the line is the only evidence on screen, and smoothing it redrew a
 *     measured 98 MiB/s burst at a third of its height. And where smoothing
 *     IS on, the window is symmetric and stops at gaps/edges, so the line's
 *     tip is always the raw bucket mean.
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
function decl(re) {
  const m = src.match(re);
  if (!m) throw new Error("not found in web/app.js: " + re);
  return m[0];
}

/* The page's own constants and drawing code, verbatim. monCss() is stubbed
 * to echo the token name back, so a stroke's colour identifies which series
 * drew it; the real one reads getComputedStyle. */
const code = [
  "var MON_SLOTS=604800, MIB=1048576;",
  decl(/var MON_STOPS\s*=\s*\[[^\]]*\];/),
  decl(/var MON_GUT\s*=\s*\d+,\s*MON_PADT\s*=\s*\d+,\s*MON_AXIS\s*=\s*\d+;/),
  "var window={devicePixelRatio:1};",
  "var monTs, monV, monDataV=0, monHover=null;",
  "function monCss(name){ return name; }",
  fn("monSpan"),
  fn("spanLabel"),
  fn("niceMax"),
  fn("monTicks"),
  fn("monBuckets"),
  decl(/var MON_SMOOTH_W\s*=\s*\[[^\]]*\];/),
  fn("monSmooth"),
  decl(/var CLOCK_RE\s*=[^\n]*;/),
  fn("clock12"),
  fn("hhmm"),
  decl(/var MON_DAYS\s*=\s*\[[^\]]*\];/),
  fn("tickLab"),
  fn("axLab"),
  fn("drawMon"),
  "function setRing(ts,v,dv){ monTs=ts; monV=v; monDataV=dv; }",
].join("\n");
/* eslint-disable no-new-func */
const api = new Function(
  code +
    `
  return {drawMon, setRing, monSpan, spanLabel, monTicks, monBuckets,
          MON_STOPS, MON_GUT, MON_PADT, MON_AXIS};`,
)();

let section = "";
const sect = (s) => {
  section = s;
};
let failures = 0;
function ok(cond, msg) {
  if (cond) {
    process.stdout.write(".");
  } else {
    failures++;
    console.log("\nFAIL [" + section + "] " + msg);
  }
}
function eq(a, b, msg) {
  ok(Object.is(a, b), msg + ` (got ${a}, want ${b})`);
}
function near(a, b, tol, msg) {
  ok(Math.abs(a - b) <= tol, msg + ` (got ${a}, want ~${b} +-${tol})`);
}

/* ---- a 2D context that records instead of rasterising --------------------- */
function recorder(w, h) {
  const ops = [];
  let cur = [];
  const ctx = {
    strokeStyle: "",
    fillStyle: "",
    globalAlpha: 1,
    lineWidth: 1,
    font: "",
    textAlign: "",
    textBaseline: "",
    lineJoin: "",
    setTransform() {},
    clearRect() {},
    fillRect(x, y, rw, rh) {
      ops.push({ op: "rect", x, y, w: rw, h: rh, fill: ctx.fillStyle, alpha: ctx.globalAlpha });
    },
    beginPath() {
      cur = [];
    },
    moveTo(x, y) {
      cur.push({ m: "M", x, y });
    },
    lineTo(x, y) {
      cur.push({ m: "L", x, y });
    },
    stroke() {
      ops.push({ op: "stroke", pts: cur.slice(), stroke: ctx.strokeStyle, width: ctx.lineWidth });
    },
    fillText(t, x, y) {
      ops.push({ op: "text", t, x, y });
    },
  };
  const canvas = { clientWidth: w, clientHeight: h, width: 0, height: 0, getContext: () => ctx };
  return { canvas, ops };
}

const W = 800,
  H = 118;
const X0 = api.MON_GUT,
  X1 = W - 6,
  Y0 = api.MON_PADT;
const Y1 = H - api.MON_AXIS; // every chart under test sets axis:true
const COLS = Math.round(X1 - X0);
const CSS = {
  ink: "INK",
  ink3: "INK3",
  grid: "GRID",
  nodata: "NODATA",
  nodataBd: "NODATA-BD",
  mono: "mono",
  histStart: 0,
  histLabel: "21:45",
};

/* Three series on one chart, matching the Utilization card. */
function chart(rec) {
  return {
    canvas: rec.canvas,
    axis: true,
    pctAxis: true,
    scale: 1,
    series: [
      { k: 0, tok: "--ch-1" },
      { k: 1, tok: "--ch-2" },
      { k: 2, tok: "--ch-3" },
    ],
  };
}

const SLOTS = 604800;
let ringVersion = 0;
/* A ring holding `depth` seconds of samples ending at `now`. `hole` drops a
 * stretch of seconds so the gap rule can be tested against real history. */
function ring(now, depth, hole) {
  const ts = new Float64Array(SLOTS);
  const v = [];
  for (let k = 0; k < 7; k++) v.push(new Float32Array(SLOTS).fill(NaN));
  for (let s = now - depth + 1; s <= now; s++) {
    if (hole && s >= hole[0] && s < hole[1]) continue;
    const i = ((s % SLOTS) + SLOTS) % SLOTS;
    ts[i] = s;
    for (let k = 0; k < 7; k++) v[k][i] = 20 + (s % 17);
  }
  api.setRing(ts, v, ++ringVersion);
  return { earliest: now - depth + 1 };
}

function draw(now, span, earliest) {
  const rec = recorder(W, H);
  const ch = chart(rec);
  const t1 = now + 1,
    t0 = t1 - span;
  const css = Object.assign({}, CSS, {
    histStart: earliest == null ? t1 : Math.max(t0, earliest),
  });
  api.drawMon(ch, t0, t1, css);
  const strokes = rec.ops.filter((o) => o.op === "stroke");
  return {
    ops: rec.ops,
    t0,
    t1,
    washes: rec.ops.filter((o) => o.op === "rect" && o.fill === "NODATA"),
    edges: rec.ops.filter((o) => o.op === "stroke" && o.stroke === "NODATA-BD"),
    series: strokes.filter((o) => String(o.stroke).startsWith("--ch-")),
    grid: strokes.filter((o) => o.stroke === "GRID"),
    labels: rec.ops.filter((o) => o.op === "text"),
  };
}

const NOW = 1756500000; // fixed, so tick labels are stable
const STOPS = api.MON_STOPS;

/* ---- 1. a full ring: every stop draws edge to edge, with no wash --------- */
sect("full history, every stop");
ring(NOW, SLOTS);
for (const span of STOPS) {
  const d = draw(NOW, span, NOW - SLOTS + 1);
  const label = api.spanLabel(span);
  eq(d.washes.length, 0, `${label}: a full ring draws no no-data wash`);
  eq(d.edges.length, 0, `${label}: and no history-starts-here rule`);
  eq(d.series.length, 3, `${label}: all three series draw`);
  const first = d.series[0].pts[0],
    last = d.series[0].pts[d.series[0].pts.length - 1];
  near(first.x, X0, 1.5, `${label}: the line starts at the left edge of the plot`);
  near(last.x, X1, 1.5, `${label}: the line reaches the right edge of the plot`);
  ok(
    d.series[0].pts.length > COLS * 0.9,
    `${label}: the line is continuous across the window (${d.series[0].pts.length} of ${COLS} columns)`,
  );
}

/* ---- 2. gridlines and tick labels stay readable at every stop ------------ */
sect("gridlines and tick labels");
for (const span of STOPS) {
  const d = draw(NOW, span, NOW - SLOTS + 1);
  const label = api.spanLabel(span);
  /* Five horizontal rules (0 / 25 / 50 / 75 / 100) plus one vertical per
     tick. Value axes draw the same five but label only the halves. */
  const vertical = d.grid.filter((o) => o.pts[0].x === o.pts[1].x);
  ok(
    vertical.length >= 3 && vertical.length <= 16,
    `${label}: ${vertical.length} vertical gridlines (3..16)`,
  );
  eq(d.grid.length - vertical.length, 5, `${label}: five horizontal rules`);
  /* Time labels sit below the plot; the y-axis numbers sit left of it. */
  const ticks = d.labels.filter((o) => o.y > Y1);
  eq(ticks.length, vertical.length, `${label}: every gridline is labelled`);
  const texts = ticks.map((o) => o.t);
  eq(
    new Set(texts).size,
    texts.length,
    `${label}: no two tick labels read the same (${texts[0]} .. ${texts[texts.length - 1]})`,
  );
}

/* ---- 3. a partial ring: baseline AND wash, both, at every stop ----------- */
sect("partial history -- flat 0 under a --skel wash");
const HELD = 300; // five minutes, the screenshot case
ring(NOW, HELD);
const EARLIEST = NOW - HELD + 1;
for (const span of STOPS) {
  const d = draw(NOW, span, EARLIEST);
  const label = api.spanLabel(span);
  const overhang = (span - HELD) / span; // fraction with nothing behind it
  eq(d.washes.length, 1, `${label}: the empty stretch is washed exactly once`);
  near(
    d.washes[0].w,
    overhang * (X1 - X0),
    2,
    `${label}: the wash covers the ${(overhang * 100).toFixed(1)}% with no samples`,
  );
  eq(d.washes[0].x, X0, `${label}: the wash starts at the left edge`);
  /* The wash is a shade; the rule is what makes it unmissable in the dark
     theme, where --skel used to leave a flat 0 line looking like an idle
     Mac with no disclaimer at all. */
  eq(d.edges.length, 1, `${label}: a rule marks where the record begins`);
  near(
    d.edges[0].pts[0].x,
    X0 + d.washes[0].w,
    1,
    `${label}: the rule sits at the edge of the wash`,
  );

  const s0 = d.series[0].pts;
  near(s0[0].x, X0, 0.6, `${label}: the line still starts at the left edge`);
  eq(s0[0].y, Y1, `${label}: it starts ON the 0 baseline, not mid-air`);
  eq(s0[1].y, Y1, `${label}: and stays flat across the unmeasured stretch`);
  near(s0[1].x, X0 + overhang * COLS, 2, `${label}: the baseline ends where the history begins`);
  ok(
    !s0.slice(1).some((p) => p.m === "M"),
    `${label}: the baseline joins the real data -- one line, no leading break`,
  );
}

/* ---- 4. an empty ring reads as no-data, never as an idle machine --------- */
sect("empty ring");
ring(NOW, 0);
{
  const d = draw(NOW, 3600, null);
  eq(d.washes.length, 1, "the whole plot is washed");
  near(d.washes[0].w, X1 - X0, 1, "the wash spans the full plot width");
  const note = d.ops.find((o) => o.op === "text" && /no samples before/.test(o.t));
  ok(note, "and it SAYS so in words, inside the plot: " + (note && note.t));
  const s0 = d.series[0].pts;
  eq(s0.length, 2, "the series is a single flat segment");
  eq(s0[0].y, Y1, "drawn on the 0 baseline");
  eq(s0[1].y, Y1, "and flat all the way across");
  /* This is the pairing that matters: zeros are only honest UNDER the wash.
     A flat line with no wash is indistinguishable from an idle Mac. */
  ok(
    d.washes[0].w >= s0[1].x - s0[0].x - 1,
    "every drawn zero sits inside the wash that disclaims it",
  );
}

/* ---- 5. more pixels than seconds: a 1 Hz line must not read as dotted --- */
sect("a window narrower than the canvas is wide");
{
  /* 900 seconds of samples on a 1200 px canvas: there are fewer seconds
     than pixel columns, so one bucket per column leaves a quarter of them
     empty and the gap rule renders a fully-sampled series as a dashed
     line. Caught by the visual sheet, not by arithmetic -- pinned here so
     it stays caught. */
  const WIDE = 1240;
  const wx0 = api.MON_GUT,
    wx1 = WIDE - 6;
  ok(wx1 - wx0 > 900, "the canvas really is wider than the window has seconds");
  ring(NOW, SLOTS);
  const rec = recorder(WIDE, H);
  const ch = chart(rec);
  api.drawMon(ch, NOW + 1 - 900, NOW + 1, Object.assign({}, CSS, { histStart: NOW - SLOTS }));
  const line = rec.ops.filter((o) => o.op === "stroke" && String(o.stroke).startsWith("--ch-"))[0];
  const breaks = line.pts.filter((p, i) => i > 0 && p.m === "M");
  eq(breaks.length, 0, "a fully-sampled 15 min window draws ONE unbroken line");
  ok(
    line.pts.length <= 900,
    `never more buckets than the window has seconds (${line.pts.length} <= 900)`,
  );
  near(line.pts[0].x, wx0, 2, "still starts at the left edge");
  near(line.pts[line.pts.length - 1].x, wx1, 2, "still reaches the right edge");
}

/* ---- 6. a gap inside the history is still a gap -------------------------- */
sect("a missing second inside the history");
{
  const hole = [NOW - 500, NOW - 400];
  ring(NOW, 900, hole);
  const d = draw(NOW, 900, NOW - 899);
  const s0 = d.series[0].pts;
  const breaks = s0.filter((p, i) => i > 0 && p.m === "M");
  ok(breaks.length >= 1, "a sampler outage breaks the path -- the 0 baseline never bridges it");
  eq(d.washes.length, 0, "and it draws no wash: this window IS covered by the ring");
  ok(
    !s0.some((p) => p.m === "L" && p.y === Y1 && p.x > X0 + 10),
    "nothing is drawn at 0 across the outage",
  );
}

/* ---- 7. smoothing is gated on the min-max shade being real --------------- */
sect("smoothing: raw where the shade is not real, honest where it is");
/* A flat 20 with one 1-second spike to 100, plus a step to 80 over the last
 * 10 seconds so the line's tip has something to lag behind. */
function flatRing(now, depth, spikeAt) {
  const ts = new Float64Array(SLOTS);
  const v = [];
  for (let k = 0; k < 7; k++) v.push(new Float32Array(SLOTS).fill(NaN));
  for (let s = now - depth + 1; s <= now; s++) {
    const i = ((s % SLOTS) + SLOTS) % SLOTS;
    ts[i] = s;
    const val = s === spikeAt ? 100 : s > now - 10 ? 80 : 20;
    for (let k = 0; k < 7; k++) v[k][i] = val;
  }
  api.setRing(ts, v, ++ringVersion);
  return { ts, v };
}
{
  /* 15 min stop on a wide canvas: exactly one sample per bucket, so the
   * shade is a 1 px rect and the gate must draw the mean RAW -- the spike
   * reaches the top of the plot instead of a third of the way up. */
  const WIDE = 1240;
  flatRing(NOW, SLOTS, NOW - 450);
  const rec = recorder(WIDE, H);
  const ch = chart(rec);
  api.drawMon(ch, NOW + 1 - 900, NOW + 1, Object.assign({}, CSS, { histStart: NOW - SLOTS }));
  const line = rec.ops.filter((o) => o.op === "stroke" && String(o.stroke).startsWith("--ch-"))[0];
  const YV = (v) => Y1 - (Math.min(v, 100) / 100) * (Y1 - Y0);
  const peak = Math.min(...line.pts.map((p) => p.y));
  near(peak, YV(100), 0.5, "1 sample per bucket: the 100% spike draws at 100, not smoothed to 47");
}
{
  /* 2 h stop on the 800 px canvas: 9.6 samples per bucket, the shade is a
   * real envelope, so the line MAY smooth -- but by the documented kernel,
   * and its tip must stay the raw bucket mean. */
  const SPIKE = NOW - 3600;
  const { ts, v } = flatRing(NOW, SLOTS, SPIKE);
  const t1 = NOW + 1,
    t0 = t1 - 7200;
  const cols = Math.min(COLS, 7200);
  const raw = api.monBuckets(ts, v[0], t0, t1, cols);
  const rec = recorder(W, H);
  const ch = chart(rec);
  api.drawMon(ch, t0, t1, Object.assign({}, CSS, { histStart: NOW - SLOTS }));
  const line = rec.ops.filter((o) => o.op === "stroke" && String(o.stroke).startsWith("--ch-"))[0];
  const YV = (vv) => Y1 - (Math.min(vv, 100) / 100) * (Y1 - Y0);
  const cSpike = Math.floor(((SPIKE - t0) * cols) / 7200);
  ok(raw.avg[cSpike] > 25, "the spike second landed in the expected bucket");
  const want =
    (3 * raw.avg[cSpike] +
      2 * raw.avg[cSpike - 1] +
      2 * raw.avg[cSpike + 1] +
      raw.avg[cSpike - 2] +
      raw.avg[cSpike + 2]) /
    9;
  near(
    line.pts[cSpike].y,
    YV(want),
    0.5,
    "a real-shade stop smooths by exactly the [3,2,1] kernel",
  );
  ok(
    line.pts[cSpike].y > YV(raw.avg[cSpike]) + 1,
    "so the smoothed peak sits visibly below the raw bucket mean the shade carries",
  );
  near(
    line.pts[cols - 1].y,
    YV(raw.avg[cols - 1]),
    0.5,
    "the tip of the line is the RAW last bucket mean -- no trailing-only lag",
  );
  ok(raw.avg[cols - 1] > 60, "and that tip really is the fresh 80% step, not old baseline");
}

if (failures) {
  console.log("\n" + failures + " FAILURE(S)");
  process.exit(1);
}
console.log("\nsysmon render: all passed");
