/* The size-projection component, run against the real functions in web/app.js.
 *
 *   bun tests/test_projection_ui.js
 *
 * This strip is the largest thing on the live card, and the card carries an
 * abort button. What it renders decides whether a human kills a good encode or
 * green-lights deleting an irreplaceable original. Two rules are load-bearing:
 *
 *   1. Colour comes from `e.verdict` -- the server's verdict, the same one the
 *      pipeline acts on. An earlier version re-derived it in the browser and
 *      disagreed with the server in three ranges. Two were dangerous, and both
 *      are pinned below.
 *   2. The 30-80% band is the user's TARGET, not a defect threshold. 4 of the
 *      first 12 completed encodes landed under it and every one was good.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");

/* Pull the functions and tables out of web/app.js by name, so this tests SHIPPED
   code rather than a copy that can drift. */
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
function line(name) {
  const m = new RegExp("^\\s*var " + name + "\\s*=.*$", "m").exec(src);
  if (!m) throw new Error("not found in web/app.js: " + name);
  return m[0];
}
function tbl(name) {
  const m = new RegExp("var " + name + "\\s*=\\s*\\{").exec(src);
  if (!m) throw new Error("not found in web/app.js: " + name);
  return "var " + name + "=" + block(m.index + m[0].length - 1) + ";";
}

const document = {
  createElement: (tag) => {
    const set = new Set();
    return {
      tag,
      id: "",
      type: "",
      className: "",
      textContent: "",
      hidden: false,
      style: {},
      attrs: {},
      kids: [],
      appendChild(c) {
        this.kids.push(c);
        return c;
      },
      setAttribute(k, v) {
        this.attrs[k] = v;
      },
      handlers: {},
      addEventListener(type, fn) {
        (this.handlers[type] = this.handlers[type] || []).push(fn);
      },
      /* classList is tracked apart from className, as in a real DOM:
         updateProj assigns className and then projApply toggles "closed". */
      classList: {
        toggle(c, on) {
          on ? set.add(c) : set.delete(c);
        },
        contains(c) {
          return set.has(c);
        },
      },
    };
  },
};
const GIB = 2 ** 30,
  TIB = 2 ** 40;
const code = [
  "var projClosed=false;",
  "var projTouched=false;",
  "var projLoudSeen=false;",
  "var localStorage={getItem:function(){return null},setItem:function(){}};",
  "function _setClosed(v){projClosed=v;}",
  "function _reset(){projClosed=false;projTouched=false;projLoudSeen=false;}",
  "function _fire(n,t){(n.handlers[t]||[]).forEach(function(f){f();});}",
  line("NOTE_HEAD"),
  "var tipSeq=0;",
  tbl("PROJ_CLASS"),
  tbl("PROJ_LEAD"),
  tbl("PROJ_LOUD"),
]
  .concat(
    [
      "gib",
      "pct",
      "el",
      "bandText",
      "gibApprox",
      "projApply",
      "projBlock",
      "updateProj",
      "splitNote",
      "writeVerdict",
    ].map(fn),
  )
  .join("\n");
const M = new Function(
  "document",
  "GIB",
  "TIB",
  code +
    "\nreturn {gib,pct,el,bandText,gibApprox,projBlock,updateProj,PROJ_CLASS,PROJ_LEAD," +
    "PROJ_LOUD,_setClosed,_reset,_fire,splitNote,writeVerdict};",
)(document, GIB, TIB);

let pass = 0,
  fail = 0;
const ck = (what, got, want) => {
  if (got === want) {
    process.stdout.write(".");
    pass++;
  } else {
    console.log(`\nFAIL ${what}: got '${got}' want '${want}'`);
    fail++;
  }
};
const ckHas = (what, hay, needle) => {
  if (String(hay).includes(needle)) {
    process.stdout.write(".");
    pass++;
  } else {
    console.log(`\nFAIL ${what}: '${hay}' lacks '${needle}'`);
    fail++;
  }
};
const ckNot = (what, hay, needle) => {
  if (!String(hay).includes(needle)) {
    process.stdout.write(".");
    pass++;
  } else {
    console.log(`\nFAIL ${what}: '${hay}' must not contain '${needle}'`);
    fail++;
  }
};
const build = () => M.projBlock().refs;

// --- colour is the server's verdict, never a second opinion ---------------
// Every code core._verdict() can return must map to a class, or the strip
// silently renders unstyled for it.
["good", "thin", "suspect", "no-saving", "blowup", "downscale", "unknown"].forEach((v) =>
  ck('verdict "' + v + '" has a class', typeof M.PROJ_CLASS[v], "string"),
);
ck("good is the only positive class", M.PROJ_CLASS.good, "on");
ck("thin warns, never green", M.PROJ_CLASS.thin, "warn");
ck("suspect warns", M.PROJ_CLASS.suspect, "warn");
ck("downscale is the worst class", M.PROJ_CLASS.downscale, "bad");

// --- CRITICAL 1: a downscale must never render as on-target ---------------
// Output frame narrower than source is the ONE state where the original must
// survive. It scored 45% -- squarely mid-band -- and rendered green.
let p = build();
M.updateProj(p, {
  pct: 50,
  verdict: "downscale",
  ratio_pct: 45.0,
  projected_bytes: 31 * GIB,
  source_bytes: 70 * GIB,
  crop_factor: 1.0,
  shrink_pct: 55.0,
});
ck("downscale marker is bad", p.mark.className, "proj-mark bad");
ck("downscale greys the ratio", p.ratioWrap.className, "proj-ratio lost");
ck("downscale greys the whole strip", p.root.className, "proj lost");
ck("downscale lead is bad", p.lead.className, "proj-lead bad");
ckHas("downscale says do not delete", p.lead.textContent, "DO NOT DELETE THE ORIGINAL");
ckNot("downscale never claims the band", p.detail.textContent, "target band");
ckNot("downscale never advertises a saving", p.detail.textContent, "freed");
ckHas("downscale says why the ratio is void", p.detail.textContent, "not comparable");

// --- CRITICAL 2: a verified-good small encode must not read as broken -----
// Flight (2012), ledger row: 8.08 GiB of 86.32 GiB, SSIM 0.9931/0.9945.
// The server calls this `good`; the strip must agree with the server.
p = build();
M.updateProj(p, {
  pct: 100,
  verdict: "good",
  ratio_pct: 9.4,
  projected_bytes: 8.08 * GIB,
  source_bytes: 86.32 * GIB,
  crop_factor: 1.35,
  norm_ratio_pct: 12.6,
  shrink_pct: 90.6,
});
ck("a good 9.4% encode is green", p.ratioWrap.className, "proj-ratio on");
ck("its marker is green", p.mark.className, "proj-mark on");
ckNot("nothing calls it implausible", p.lead.textContent + p.detail.textContent, "IMPLAUSIB");
ckHas("it is described as below target", p.detail.textContent, "below the 30–80% target band");
ckHas("and that is called normal", p.detail.textContent, "normal for a clean digital source");

// --- the strip must not reassure where the server is suspicious -----------
// core: OUTLIER_FACTOR 0.45 x median 35.1% = ~15.8%. The old browser table
// used 12%, so 12-15.8% rendered a calm "normal" over an amber SUSPECT.
p = build();
M.updateProj(p, {
  pct: 40,
  verdict: "suspect",
  ratio_pct: 14.0,
  projected_bytes: 10 * GIB,
  source_bytes: 70 * GIB,
  crop_factor: 1.0,
  shrink_pct: 86.0,
});
ck("server suspicion wins over the band", p.ratioWrap.className, "proj-ratio warn");
ckHas("and it says to verify first", p.lead.textContent, "verify the picture before deleting");

// thin: 70-80% is inside the user's band but the server wants a human call
p = build();
M.updateProj(p, {
  pct: 90,
  verdict: "thin",
  ratio_pct: 71.3,
  projected_bytes: 52.71 * GIB,
  source_bytes: 73.91 * GIB,
  crop_factor: 1.0,
  shrink_pct: 28.7,
});
ck("a thin saving is not green even in band", p.ratioWrap.className, "proj-ratio warn");
ckHas("band position still stated", p.detail.textContent, "in the 30–80% target band");

// --- band text is positional only, never a severity ----------------------
ckHas("above band", M.bandText(84.9), "above the 30–80% target band");
ckHas("top of band", M.bandText(80), "in the 30–80% target band");
ckHas("bottom of band", M.bandText(30), "in the 30–80% target band");
ckHas("below band", M.bandText(29.9), "below the 30–80% target band");
ck("no ratio, no band text", M.bandText(null), "");
["above", "in the", "below"].forEach(() => {});
[84.9, 80, 30, 29.9].forEach((r) =>
  ckNot("bandText(" + r + ") carries no severity word", M.bandText(r), "IMPLAUS"),
);

// --- estimates are marked as estimates -----------------------------------
ck("approx GiB is tilde-marked and coarse", M.gibApprox(78.24 * GIB), "~78 GiB");
ck("approx GiB handles null", M.gibApprox(null), "—");
p = build();
M.updateProj(p, {
  pct: 42,
  verdict: "good",
  ratio_pct: 40.0,
  projected_bytes: 33.5 * GIB,
  source_bytes: 83.8 * GIB,
  crop_factor: 1.0,
  shrink_pct: 60.0,
});
ckHas("saving is marked approximate", p.detail.textContent, "~50 GiB freed when it finishes");
ck("size keeps full precision", p.size.textContent, "33.50 GiB");
ck("marker is placed", p.mark.style.left, "40%");

// --- polarity: both percentages present so the tables cannot contradict ---
ckHas("caption says which way the percent runs", p.srcCap.textContent, "kept, of");
ckHas("shrink is stated beside it", p.detail.textContent, "60.0% smaller");

// --- unit casing survives (the caption is the one carrying GiB) ----------
ckHas("source caption keeps GiB casing", p.srcCap.textContent, "GiB");
ckNot("never GIB", p.srcCap.textContent, "GIB");

// --- too-early and unknown-source states ---------------------------------
p = build();
M.updateProj(p, {
  pct: 3.57,
  verdict: "unknown",
  ratio_pct: null,
  projected_bytes: null,
  source_bytes: 75785844608,
  crop_factor: 1.347,
});
ck("too early shows no size", p.size.textContent, "—");
ck("too early hides the marker", p.mark.hidden, true);
ckHas("too early explains why", p.detail.textContent, "opens at 5%");
ck("too early has no lead", p.lead.textContent, "");

p = build();
M.updateProj(p, {
  pct: 60,
  verdict: "unknown",
  ratio_pct: null,
  projected_bytes: 22 * GIB,
  source_bytes: null,
  crop_factor: 1.0,
});
ck("unknown source is said, not guessed", p.srcCap.textContent, "source size unknown");
ckHas(
  'a known size is not called "no projection"',
  p.detail.textContent,
  "percentage cannot be computed",
);
ckHas(
  "aria agrees with the visible text",
  p.scale.attrs["aria-label"],
  "ratio to the source cannot be computed",
);

// --- a blowup is not quietly clamped out of view -------------------------
p = build();
M.updateProj(p, {
  pct: 30,
  verdict: "blowup",
  ratio_pct: 137.0,
  projected_bytes: 96 * GIB,
  source_bytes: 70 * GIB,
  crop_factor: 1.0,
});
ck("over-100 marker clamps to the track end", p.mark.style.left, "100%");
ck("over-100 is flagged bad", p.ratioWrap.className, "proj-ratio bad");
ckHas("over-100 says larger", p.lead.textContent, "LARGER THAN THE SOURCE");

// --- the collapse never hides a loud verdict -----------------------------
// The strip can fold to its head line, but a verdict that warns must force
// it open: "downscale" behind a chevron is the quiet-warning bug again.
p = build();
M._setClosed(true);
M.updateProj(p, {
  pct: 50,
  verdict: "downscale",
  ratio_pct: 45.0,
  projected_bytes: 31 * GIB,
  source_bytes: 70 * GIB,
  crop_factor: 1.0,
  shrink_pct: 55.0,
});
ck("a loud verdict forces the strip open", p.root.classList.contains("closed"), false);
M.updateProj(p, {
  pct: 42,
  verdict: "good",
  ratio_pct: 40.0,
  projected_bytes: 28 * GIB,
  source_bytes: 70 * GIB,
  crop_factor: 1.0,
});
ck("a calm verdict honours the collapse", p.root.classList.contains("closed"), true);
ck("collapsed still shows the size", p.size.textContent !== "", true);
ck("collapsed still shows the ratio", p.ratio.textContent !== "", true);
ck("the chevron says it is closed", p.disc.attrs["aria-expanded"], "false");
M._setClosed(false);
M.updateProj(p, {
  pct: 42,
  verdict: "good",
  ratio_pct: 40.0,
  projected_bytes: 28 * GIB,
  source_bytes: 70 * GIB,
  crop_factor: 1.0,
});
ck("open is the default", p.root.classList.contains("closed"), false);
["suspect", "blowup", "no-saving", "downscale"].forEach((v) =>
  ck('"' + v + '" is a loud verdict', !!M.PROJ_LOUD[v], true),
);

// --- one loud set, everywhere --------------------------------------------
// The verdict line in renderLive once carried its own inline copy of the
// warning list and omitted "downscale" -- the only true warning on the card
// rendered unstyled. Styling must come from PROJ_LOUD, never a re-listed
// chain of codes that can drift from it: no verdict code may appear as a
// quoted literal in renderLive at all, whatever the spelling around it.
const renderLiveSrc = fn("renderLive");
ck(
  "renderLive styles the verdict from PROJ_LOUD",
  renderLiveSrc.includes("PROJ_LOUD[e.verdict]"),
  true,
);
Object.keys(M.PROJ_LOUD).forEach((v) =>
  ck(
    'renderLive does not re-list "' + v + '"',
    renderLiveSrc.includes('"' + v + '"') || renderLiveSrc.includes("'" + v + "'"),
    false,
  ),
);

// --- the chevron must actually fold a LOUD strip --------------------------
// The loud override existed to stop a fold RESTORED FROM LOCALSTORAGE hiding
// a warning. It was applied on every frame instead, so on exactly the
// verdicts a person most wants to fold away after reading -- suspect,
// downscale, blowup, no-saving -- `open` was forced true, the click changed
// nothing on screen, and the chevron read as broken. Reported live on a
// `suspect` strip, 2026-09-06.
const SUSPECT = {
  pct: 44,
  verdict: "suspect",
  ratio_pct: 44.8,
  projected_bytes: 23.63 * GIB,
  source_bytes: 52.79 * GIB,
  crop_factor: 1.0,
  shrink_pct: 55.2,
};
M._reset();
let strip = M.projBlock();
let head = strip.node.kids[0];
M.updateProj(strip.refs, SUSPECT);
ck("a loud verdict opens the strip", strip.refs.root.classList.contains("closed"), false);
M._fire(head, "click");
ck("the chevron folds a loud strip BY HAND", strip.refs.root.classList.contains("closed"), true);
ck("and the chevron flips to match", strip.refs.disc.textContent, "▸");
M.updateProj(strip.refs, SUSPECT);
ck("and the fold survives the next SSE frame", strip.refs.root.classList.contains("closed"), true);
M._fire(head, "click");
ck("clicking again reopens it", strip.refs.root.classList.contains("closed"), false);

// A fold restored from storage is still overridden -- that half is the point.
M._reset();
M._setClosed(true);
strip = M.projBlock();
M.updateProj(strip.refs, SUSPECT);
ck("a STORED fold never hides a loud verdict", strip.refs.root.classList.contains("closed"), false);

// A calm strip still honours a stored fold.
M._reset();
M._setClosed(true);
strip = M.projBlock();
M.updateProj(strip.refs, { ...SUSPECT, verdict: "good", ratio_pct: 44.8 });
ck("a calm strip honours a stored fold", strip.refs.root.classList.contains("closed"), true);

// A verdict that TURNS loud is news: it reopens a hand-folded strip.
M._reset();
strip = M.projBlock();
head = strip.node.kids[0];
M.updateProj(strip.refs, { ...SUSPECT, verdict: "good" });
M._fire(head, "click");
ck("hand-folded while calm", strip.refs.root.classList.contains("closed"), true);
M.updateProj(strip.refs, SUSPECT);
ck("a verdict turning loud reopens it", strip.refs.root.classList.contains("closed"), false);

// --- the verdict note: headline on the card, explanation behind the icon --
// This channel also carries "Do not delete the original", and the iPad cannot
// hover, so the headline may never disappear into a bare glyph.
ck(
  "an uppercase lead is the headline",
  M.splitNote("NO vt_h265_10bit BASELINE YET: this is one of the first encodes.").head,
  "NO vt_h265_10bit BASELINE YET",
);
ckHas(
  "and the rest is the body",
  M.splitNote("NO vt_h265_10bit BASELINE YET: this is one of the first encodes.").body,
  "one of the first encodes",
);
ck(
  "downscale splits too",
  M.splitNote("RESOLUTION LOST: the output frame is narrower than the source.").head,
  "RESOLUTION LOST",
);
ck(
  "a short note is all headline",
  M.splitNote("Solid reduction - let it run.").head,
  "Solid reduction - let it run.",
);
ck("a short note has no body", M.splitNote("Solid reduction - let it run.").body, "");
ck(
  "a long clause is never mistaken for a headline",
  M.splitNote("this encode keeps 9.4% of source bytes and the median is 31.6%: which is unusual.")
    .body,
  "",
);

const vrefs = { verdict: M.el("div", "verdict", "") };
M.writeVerdict(vrefs, "Solid reduction - let it run.", false);
ck("a calm short note is plain text", vrefs.verdict.textContent, "Solid reduction - let it run.");
ck("and gets no caution button", vrefs.verdict.kids.length, 0);

M.writeVerdict(vrefs, "RESOLUTION LOST: the output frame is narrower than the source.", true);
ck("a long note builds three nodes", vrefs.verdict.kids.length, 3);
ck("the first is the caution button", vrefs.verdict.kids[0].className, "cautionbtn");
ck("which is a real <button>", vrefs.verdict.kids[0].tag, "button");
ck("the headline stays on the card", vrefs.verdict.kids[1].textContent, "RESOLUTION LOST");
ckHas("the body is the tooltip", vrefs.verdict.kids[2].textContent, "narrower than the source");
ck("the tooltip is announced as one", vrefs.verdict.kids[2].attrs.role, "tooltip");
ck(
  "and the button points at it",
  vrefs.verdict.kids[0].attrs["aria-describedby"],
  vrefs.verdict.kids[2].id,
);
ck("loud styling survives the rebuild", vrefs.verdict.className, "verdict loud");

// The in-place rule: an unchanged note must NOT rebuild, or a tooltip somebody
// is reading would be torn down and slammed shut twice a second.
const before = vrefs.verdict.kids[0];
M.writeVerdict(vrefs, "RESOLUTION LOST: the output frame is narrower than the source.", true);
ck("an unchanged note rebuilds nothing", vrefs.verdict.kids[0], before);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
