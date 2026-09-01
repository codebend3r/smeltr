/* The repaint key and the in-place progress writer, run against the real
 * functions in web/app.js.
 *
 *   node tests/test_repaint_key.js
 *
 * This has swung wrong in BOTH directions and the tests pin both edges:
 *
 *   1. Transfers were once left OUT of paint()'s key entirely. The
 *      transferring row then painted one still frame at ~0 bytes and never
 *      advanced for the length of a 45-minute push -- a stalled transfer and
 *      a healthy one looked identical.
 *   2. The fix put raw byte counts IN the key. The key then changed on every
 *      2 s SSE frame, so the whole table was torn down and rebuilt twice a
 *      second: the pane's fade-in replayed, scroll position reset, and the
 *      page visibly blinked for the entire transfer.
 *
 * The shape/number split is what satisfies both. qShape/xShape carry only
 * what decides which NODES EXIST; updateProgress writes the numbers into
 * those nodes every frame. So: bytes must NOT move the key, and shape changes
 * MUST move it.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');

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
  if (at < 0) throw new Error('not found in web/app.js: ' + name);
  return block(at);
}

/* Minimal DOM: enough for progWrite's create-once/write-many path, and it
   RECORDS rebuilds so a test can prove the slot was not torn down. */
let builds = 0;
function node(tag, cls, text) {
  return {
    tagName: tag, className: cls || '', textContent: text == null ? '' : text,
    hidden: false, style: {}, children: [],
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren() { builds++; this.children = []; },
  };
}
const gib = b => (b == null ? '—' : (b / 1073741824).toFixed(2) + ' GiB');
const pct = v => (v == null ? '—' : v.toFixed(1) + '%');
const dur = s => (s == null ? '—' : Math.round(s / 60) + 'm');
const el = node;

const ctx = { el, gib, pct, dur, progRefs: {} };
const code = [
  'var progRefs={};',
  fn('progSlot'), fn('progWrite'), fn('arrState'), fn('xferState'),
  fn('qShape'), fn('xShape'),
  'return {progSlot,progWrite,arrState,xferState,qShape,xShape,' +
  'refs:function(){return progRefs;}};',
].join('\n');
const api = new Function('el', 'gib', 'pct', 'dur', code)(el, gib, pct, dur);

let failed = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  ok   ' + name); }
  else { failed++; console.log('  FAIL ' + name + (detail ? '\n       ' + detail : '')); }
}
const key = v => JSON.stringify(v);

const GIB = 1073741824;
const arriving = (done, rate) => ({
  title: 'Croods, The (2013)', mbps: 97.4, bytes: 67 * GIB,
  location: 'Vermithor', src_dir: 'Media/4K Family Movies/C',
  skipped: false, pinned: false, encoding: false, ready: false,
  staged: false, next_up: false,
  arriving_bytes: done, arriving_rate_bps: rate, arriving_stalled: false,
});
const xfer = (done, opts) => Object.assign({
  title: 'Kubo (2016)', nas: 'Vhagar', src_dir: 'Media/4K Movies/K',
  done_bytes: done, total_bytes: 40 * GIB, pct: done / (40 * GIB) * 100,
  rate_bps: 8e6, stalled: false,
}, opts || {});

console.log('growing bytes must NOT change the key');
check('a queue row pulling faster does not move qShape',
  key(api.qShape(arriving(1 * GIB, 8e6))) === key(api.qShape(arriving(9 * GIB, 1e6))),
  'byte counts in the key rebuild the whole table every SSE frame');
check('a push moving does not move xShape',
  key(api.xShape(xfer(1 * GIB))) === key(api.xShape(xfer(30 * GIB))));

console.log('shape changes MUST change the key');
check('a pull that starts moves qShape', (() => {
  const idle = arriving(null, 0); idle.arriving_bytes = null;
  return key(api.qShape(idle)) !== key(api.qShape(arriving(1 * GIB, 8e6)));
})());
check('a pull that stalls moves qShape', (() => {
  const s = arriving(9 * GIB, 0); s.arriving_stalled = true;
  return key(api.qShape(s)) !== key(api.qShape(arriving(9 * GIB, 8e6)));
})());
check('a push that stalls moves xShape',
  key(api.xShape(xfer(9 * GIB, { stalled: true }))) !== key(api.xShape(xfer(9 * GIB))));
check('skipping moves qShape', (() => {
  const r = arriving(1 * GIB, 8e6); const s = Object.assign({}, r, { skipped: true });
  return key(api.qShape(r)) !== key(api.qShape(s));
})());
check('a hand-picked CRF moves qShape', (() => {
  /* The picker's selected option is a NODE-level fact: the cell must be
     rebuilt for the new rung to show. Left out of the key, the column would
     keep rendering the old value until something else happened to change the
     shape -- and the number in the CRF column is exactly what the operator
     would check to confirm the click landed. */
  const a = arriving(1 * GIB, 8e6); a.crf = 16; a.crf_set = false;
  const b = Object.assign({}, a, { crf: 12, crf_set: true });
  return key(api.qShape(a)) !== key(api.qShape(b));
})());
check('pinning the default rung still moves qShape', (() => {
  /* 16-by-default and 16-by-choice render differently (muted vs solid), so
     the flag has to be in the key even when the number does not move. */
  const a = arriving(1 * GIB, 8e6); a.crf = 16; a.crf_set = false;
  const b = Object.assign({}, a, { crf_set: true });
  return key(api.qShape(a)) !== key(api.qShape(b));
})());
check('a queue position change moves qShape', (() => {
  const a = Object.assign(arriving(null, 0), { arriving_bytes: null, stage_queued: 1 });
  const b = Object.assign({}, a, { stage_queued: 2 });
  return key(api.qShape(a)) !== key(api.qShape(b));
})());

console.log('progWrite writes in place');
const td = node('td');
api.progSlot('arr|croods', td);
api.progWrite('arr|croods', api.arrState(arriving(1 * GIB, 8e6)));
const first = api.refs()['arr|croods'];
const buildsAfterFirst = builds;
const fillNode = first.fill, txtNode = first.txt;
api.progWrite('arr|croods', api.arrState(arriving(9 * GIB, 8e6)));
check('the same shape reuses the fill node (keeps its width transition)',
  api.refs()['arr|croods'].fill === fillNode && builds === buildsAfterFirst);
check('the caption is the same node, rewritten',
  api.refs()['arr|croods'].txt === txtNode && txtNode.textContent.indexOf('9.00 GiB') >= 0);
check('the bar width advanced',
  fillNode.style.width === (9 / 67 * 100) + '%',
  'got ' + fillNode.style.width);
const stalled = arriving(9 * GIB, 0); stalled.arriving_stalled = true;
api.progWrite('arr|croods', api.arrState(stalled));
check('a shape change DOES rebuild the slot', builds > buildsAfterFirst);
check('a stalled pull draws no bar, ever',
  api.refs()['arr|croods'].fill === null &&
  api.refs()['arr|croods'].mark.textContent === 'stalled');
check('a stalled pull says it is not moving',
  api.refs()['arr|croods'].txt.textContent.indexOf('not moving') >= 0);

console.log('captions keep their units and do not repeat the destination');
check('the caption does not repeat the destination (the pills are on the ledger row above)',
  api.xferState(xfer(9 * GIB)).text.indexOf('copied to') < 0 &&
  api.xferState(xfer(9 * GIB)).text.indexOf('copied') >= 0);
check('a moving push carries rate and ETA',
  ['MB/s', 'left'].every(w =>
    api.xferState(xfer(9 * GIB)).text.indexOf(w) >= 0));
check('a stalled push claims no rate and no ETA',
  api.xferState(xfer(9 * GIB, { stalled: true })).text.indexOf('MB/s') < 0);
check('both operands carry GiB',
  (api.xferState(xfer(9 * GIB)).text.match(/GiB/g) || []).length === 2);
check('a stalled push is never given a percentage bar',
  api.xferState(xfer(9 * GIB, { stalled: true })).shape === 'stall');
check('an over-large partial is a stale leftover, not 110% progress',
  api.arrState(Object.assign(arriving(80 * GIB, 8e6), {})).shape === 'stall');

console.log(failed ? '\nFAILED: ' + failed : '\nAll repaint-key tests passed');
process.exit(failed ? 1 : 0);
