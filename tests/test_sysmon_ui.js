/* The resource-monitor chart math, run against the real functions in
 * server.py's _PAGE (same extraction trick as test_repaint_key.js):
 *
 *   node tests/test_sysmon_ui.js
 *
 * Pins:
 *   - the zoom mapping's ends and monotonicity (0 -> 1 h, 100 -> 24 h)
 *   - decimation: a one-second spike must survive into a bucket's MAX
 *     (averaging alone would erase it at 24 h zoom)
 *   - a missing second is a NaN bucket -- a GAP, never an interpolation
 *   - a stale ring slot (ts from yesterday in today's index) never renders
 *   - NaN samples (metric unreadable) are skipped, not zeroed
 *   - throughput axis nicing (1/2/5 steps), and the honest formatting of
 *     missing values as an em dash
 */
'use strict';
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'server.py'), 'utf8');

function block(startIdx) {
  let depth = 0, started = false;
  for (let i = startIdx; i < src.length; i++) {
    if (src[i] === '{') { depth++; started = true; }
    else if (src[i] === '}') { depth--; if (started && depth === 0) return src.slice(startIdx, i + 1); }
  }
  throw new Error('unbalanced block');
}
function fn(name) {
  const at = src.indexOf('function ' + name + '(');
  if (at < 0) throw new Error('not found in server.py: ' + name);
  return block(at);
}

const code = [
  'var MON_SLOTS=86400, MIB=1048576;',
  fn('monSpan'), fn('spanLabel'), fn('niceMax'), fn('monTicks'),
  fn('monBuckets'), fn('monFmtPct'), fn('monFmtMibs'), fn('axLab'),
].join('\n');
/* eslint-disable no-eval */
const get = new Function(code + `
  return {monSpan, spanLabel, niceMax, monTicks, monBuckets,
          monFmtPct, monFmtMibs, axLab};`)();

let failures = 0;
function ok(cond, msg) {
  if (cond) { console.log('  ok - ' + msg); }
  else { failures++; console.log('  FAIL - ' + msg); }
}
function eq(a, b, msg) { ok(Object.is(a, b), msg + ` (got ${a}, want ${b})`); }

console.log('zoom mapping');
eq(get.monSpan(0), 3600, 'slider 0 is exactly 1 h');
eq(get.monSpan(100), 86400, 'slider 100 is exactly 24 h');
ok(get.monSpan(50) > 3600 && get.monSpan(50) < 86400, 'midpoint sits between');
let mono = true;
for (let p = 1; p <= 100; p++) if (get.monSpan(p) <= get.monSpan(p - 1)) mono = false;
ok(mono, 'span is strictly increasing across the slider');
eq(get.spanLabel(86400), '24 h', '24 h label');
eq(get.spanLabel(3600), '1 h', '1 h label');

console.log('axis nicing');
eq(get.niceMax(3.2), 5, '3.2 -> 5');
eq(get.niceMax(17), 20, '17 -> 20');
eq(get.niceMax(50), 50, '50 -> 50');
eq(get.niceMax(51), 100, '51 -> 100');
eq(get.niceMax(0), 1, 'no data floors at 1, never 0 (a 0-height axis lies)');
ok([900, 1800, 3600, 7200, 10800].includes(get.monTicks(86400)), 'tick step is a clock-round interval');

console.log('decimation');
const SLOTS = 86400;
function ring(samples) {
  const ts = new Float64Array(SLOTS), v = new Float32Array(SLOTS).fill(NaN);
  for (const [t, val] of samples) { ts[t % SLOTS] = t; v[t % SLOTS] = val; }
  return { ts, v };
}
const T0 = 1700000000;
{
  /* 100 s window, 10 buckets: a single 1 s spike of 100 among 5s */
  const samples = [];
  for (let s = T0; s < T0 + 100; s++) samples.push([s, s === T0 + 42 ? 100 : 5]);
  const r = ring(samples);
  const b = get.monBuckets(r.ts, r.v, T0, T0 + 100, 10);
  eq(b.hi[4], 100, 'one-second spike survives as the bucket MAX');
  ok(b.avg[4] > 5 && b.avg[4] < 100, 'mean line moves but does not equal the spike');
  eq(b.lo[4], 5, 'bucket MIN keeps the floor');
}
{
  /* a 20 s hole in the middle: bucket must be NaN, not bridged */
  const samples = [];
  for (let s = T0; s < T0 + 100; s++) if (s < T0 + 40 || s >= T0 + 60) samples.push([s, 10]);
  const r = ring(samples);
  const b = get.monBuckets(r.ts, r.v, T0, T0 + 100, 5);
  ok(Number.isNaN(b.avg[2]), 'a sampler outage renders as a gap (NaN bucket)');
  ok(!Number.isNaN(b.avg[1]) && !Number.isNaN(b.avg[3]), 'neighbours still draw');
}
{
  /* stale slot: yesterday's ts occupies today's index */
  const r = ring([[T0 - SLOTS + 10, 99]]);   // same slot index as T0+10
  const b = get.monBuckets(r.ts, r.v, T0, T0 + 100, 5);
  ok(Number.isNaN(b.avg[0]), "yesterday's sample in today's slot never renders");
}
{
  /* NaN sample (metric unreadable) is skipped, not treated as 0 */
  const r = ring([[T0 + 1, NaN], [T0 + 2, 50]]);
  const b = get.monBuckets(r.ts, r.v, T0, T0 + 10, 1);
  eq(b.avg[0], 50, 'NaN sample does not drag the mean toward 0');
  eq(b.n[0], 1, 'NaN sample does not count');
}

console.log('formatting');
eq(get.monFmtPct(null), '—', 'missing % is an em dash, never 0%');
eq(get.monFmtPct(NaN), '—', 'NaN % is an em dash');
eq(get.monFmtPct(43.4), '43%', 'percent rounds whole');
eq(get.monFmtMibs(null), '—', 'missing rate is an em dash, never 0');
eq(get.monFmtMibs(12.6 * 1048576), '13 MiB/s', 'rates >= 10 round whole');
eq(get.monFmtMibs(3.27 * 1048576), '3.3 MiB/s', 'rates < 10 keep one decimal');
eq(get.monFmtMibs(50000), '49 KiB/s', 'a live 50 KiB/s trickle never prints as 0 MiB/s');
eq(get.monFmtMibs(0), '0 KiB/s', 'a true zero is 0, distinguishable from — (unreadable)');
eq(get.axLab(12.4), 12, 'axis label >= 10 rounds whole');
eq(get.axLab(2.5), 2.5, 'axis label < 10 keeps one decimal');

console.log('');
if (failures) { console.log(failures + ' FAILURE(S)'); process.exit(1); }
console.log('sysmon ui: all passed');
