/* The size-projection component, run against the real functions in server.py.
 *
 *   node tests/test_projection_ui.js
 *
 * This is the band the user reads before authorising a ~70 GB deletion, so the
 * boundaries and the WORDING are both load-bearing. In particular: 30% is a
 * TARGET floor, not a defect threshold -- 5 of the first 12 completed encodes
 * landed under it and every one was a good encode. Anything that renders
 * "below target" as an error is a defect in this component.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'server.py'), 'utf8');

/* Pull the functions out of _PAGE by name, so this tests SHIPPED code rather
   than a copy that can drift. */
function fn(name) {
  const at = src.indexOf('function ' + name + '(');
  if (at < 0) throw new Error('not found in server.py: ' + name);
  let depth = 0, started = false;
  for (let i = at; i < src.length; i++) {
    if (src[i] === '{') { depth++; started = true; }
    else if (src[i] === '}') { depth--; if (started && depth === 0) return src.slice(at, i + 1); }
  }
  throw new Error('unbalanced: ' + name);
}

// Minimal DOM. Only what el() and the component actually touch.
const document = {
  createElement: () => ({
    className: '', textContent: '', hidden: false, style: {}, attrs: {}, kids: [],
    appendChild(c) { this.kids.push(c); return c; },
    setAttribute(k, v) { this.attrs[k] = v; },
  }),
};
const GIB = 2 ** 30, TIB = 2 ** 40;
const scope = { document, GIB, TIB, Math };
const code = ['gib', 'pct', 'el', 'projBand', 'projBlock', 'updateProj'].map(fn).join('\n');
const make = new Function('document', 'GIB', 'TIB',
  code + '\nreturn {gib,pct,el,projBand,projBlock,updateProj};');
const M = make(document, GIB, TIB);

let pass = 0, fail = 0;
const ck = (what, got, want) => {
  if (got === want) { console.log('PASS ' + what); pass++; }
  else { console.log(`FAIL ${what}: got '${got}' want '${want}'`); fail++; }
};
const ckHas = (what, hay, needle) => {
  if (String(hay).includes(needle)) { console.log('PASS ' + what); pass++; }
  else { console.log(`FAIL ${what}: '${hay}' lacks '${needle}'`); fail++; }
};

// --- band boundaries ------------------------------------------------------
const band = r => M.projBand(r).cls;
ck('100% of source is bad',          band(100),  'bad');
ck('just over 100 is bad',           band(140),  'bad');
ck('85% is bad (not worth doing)',   band(85),   'bad');
ck('84.9% is over-target',           band(84.9), 'over');
ck('80.1% is over-target',           band(80.1), 'over');
ck('80% is the top of the band',     band(80),   'on');
ck('55% mid-band is on-target',      band(55),   'on');
ck('30% is the bottom of the band',  band(30),   'on');
ck('29.9% is under-target',          band(29.9), 'under');
ck('12% is under-target',            band(12),   'under');
ck('11.9% is implausible',           band(11.9), 'bad');
ck('no ratio has no band',           band(null), '');

// --- every real shipped ratio must classify sensibly ----------------------
// From ledger.jsonl. All twelve are encodes the pipeline accepted as good.
const shipped = [49.2, 40.0, 37.0, 33.2, 71.3, 25.1, 24.7, 58.0, 9.4, 22.8, 60.4, 17.8];
ck('no shipped encode reads as over-target',
   shipped.filter(r => band(r) === 'over').length, 0);
ck('four shipped encodes read as under-target',
   shipped.filter(r => band(r) === 'under').length, 4);
ck('Flight at 9.4% is the only implausible one',
   shipped.filter(r => band(r) === 'bad').length, 1);

// --- wording: under-target must not read as a failure ---------------------
ckHas('under-target says it is normal', M.projBand(24.7).label, 'normal');
ck('under-target is informational, not a warning', M.projBand(24.7).cls, 'under');

// --- updateProj against real encode shapes --------------------------------
const build = () => projBlockRefs();
const projBlockRefs = () => M.projBlock().refs;

// too early: below 5% the server sends null and we must not invent a number
let p = build();
M.updateProj(p, { pct: 3.57, ratio_pct: null, projected_bytes: null,
                  source_bytes: 75785844608, crop_factor: 1.347 });
ck('too early shows no size',   p.size.textContent, '—');
ck('too early shows no ratio',  p.ratio.textContent, '—');
ck('too early hides the marker', p.mark.hidden, true);
ckHas('too early explains why',  p.note.textContent, 'opens at 5%');

// a real mid-band encode
p = build();
M.updateProj(p, { pct: 42, ratio_pct: 40.0, projected_bytes: 33.5 * GIB,
                  source_bytes: 83.8 * GIB, crop_factor: 1.0, norm_ratio_pct: 40.0 });
ck('on-target size',    p.size.textContent, '33.50 GiB');
ck('on-target ratio',   p.ratio.textContent, '40.0%');
ck('on-target class',   p.ratioWrap.className, 'proj-ratio on');
ck('marker is placed',  p.mark.style.left, '40%');
ckHas('note names the band', p.note.textContent, 'IN THE 30-80% TARGET BAND');
ckHas('note states what it frees', p.note.textContent, 'frees');

// crop-adjusted: the raw ratio understates how hard the encoder is working,
// so the per-retained-pixel figure has to be shown beside it.
p = build();
M.updateProj(p, { pct: 50, ratio_pct: 24.3, projected_bytes: 18.4 * GIB,
                  source_bytes: 75.8 * GIB, crop_factor: 1.347, norm_ratio_pct: 32.7 });
ckHas('crop-adjusted figure is shown', p.note.textContent, '32.7% per retained pixel');
ck('band uses the RAW ratio, not the adjusted one', p.mark.className, 'proj-mark under');

// a blowup must not be quietly clamped out of view
p = build();
M.updateProj(p, { pct: 30, ratio_pct: 137.0, projected_bytes: 96 * GIB,
                  source_bytes: 70 * GIB, crop_factor: 1.0 });
ck('over-100 marker clamps to the track end', p.mark.style.left, '100%');
ck('over-100 is flagged bad', p.ratioWrap.className, 'proj-ratio bad');
ckHas('over-100 says larger', p.note.textContent, 'LARGER THAN THE SOURCE');

// missing source size: never back-solve it
p = build();
M.updateProj(p, { pct: 60, ratio_pct: null, projected_bytes: 22 * GIB,
                  source_bytes: null, crop_factor: 1.0 });
ck('unknown source is said, not guessed', p.srcCap.textContent, 'source size unknown');
ck('unknown source yields no ratio', p.ratio.textContent, '—');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
