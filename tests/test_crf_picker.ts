/* The per-title quality picker in the queue's Quality column, run against the
 * real crfPicker/rowActions out of web/app.js.
 *
 *   bun tests/test_crf_picker.js
 *
 * The picker is not a preference control: what it writes is read by
 * .autopilot.sh (`smeltr crf`) as the -q for an encode that may not start for
 * hours. So the things pinned here are the ones that would make it lie.
 *
 *  1. It offers the LADDER and nothing else. A rung .watch-encode.sh cannot
 *     step from is a dead end -- the encode is auto-killed and never retried.
 *     The old menu carried 24 for exactly that reason and it is gone.
 *  2. "auto" CLEARS the override (crf: null), rather than pinning the current
 *     default as a choice. Without a way back, a row is opted out of a
 *     default that later moves and nothing on screen says so.
 *  3. A running encode gets NO picker. -q is fixed for the next several
 *     hours; a control there is a promise nothing can keep.
 *  4. There is exactly ONE quality control per row. The hover actions used to
 *     carry a second one that applied only to a hand-started encode, sitting
 *     one column away from a number that meant something else.
 *  5. Every option NAMES its scale. x265 quality is CRF and VideoToolbox is
 *     CQ on Apple's reversed scale, so a bare number in a menu carrying both
 *     is ambiguous in the direction that costs a ~90 GB original.
 *  6. An x265 rung goes to /api/queue/crf and a hardware one to
 *     /api/queue/encoder -- one request either way, because each endpoint
 *     clears the other file.
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

/* Minimal DOM. Options carry `value` and the select records assignment, which
   is all the picker's own logic touches. Listeners are kept so a test can
   fire `change` and see the request that goes out. */
function node(tag, cls?, text?) {
  return {
    tagName: tag,
    className: cls || "",
    textContent: text == null ? "" : text,
    value: undefined,
    disabled: false,
    title: "",
    type: "",
    draggable: false,
    children: [],
    attrs: {},
    dataset: {},
    classList: { add() {}, remove() {} },
    on: {},
    appendChild(c) {
      this.children.push(c);
      return c;
    },
    setAttribute(k, v) {
      this.attrs[k] = v;
    },
    addEventListener(ev, f) {
      (this.on[ev] = this.on[ev] || []).push(f);
    },
    fire(ev) {
      (this.on[ev] || []).forEach((f) => f({ stopPropagation() {} }));
    },
  };
}

const posted = [];
const env = {
  el: node,
  gib: (b) => (b == null ? "—" : (b / 1073741824).toFixed(2) + " GiB"),
  api: (p, body) => {
    posted.push([p, body]);
    return {
      finally(f) {
        f();
      },
    };
  },
  last: { pending: false, state: null, key: null },
  paint: () => {},
};
const code = [
  "var crfOpen=null;",
  "var armedTitle=null;",
  "var drag=null;",
  fn("arm").replace(/^function arm/, "function arm"),
  fn("qLabel"),
  fn("crfPicker"),
  fn("rowActions"),
  "return {crfPicker,rowActions,open:function(){return crfOpen;}};",
].join("\n");
const ui = new Function("el", "gib", "api", "last", "paint", code)(
  env.el,
  env.gib,
  env.api,
  env.last,
  env.paint,
);

let section = "";
const sect = (s) => {
  section = s;
};
let failed = 0;
function check(name, cond, detail?) {
  if (cond) {
    process.stdout.write(".");
  } else {
    failed++;
    console.log("\nFAIL [" + section + "] " + name + (detail ? "\n     " + detail : ""));
  }
}

const LADDER = [10, 12, 14, 16, 18, 20, 22];
const CQ = [50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100];
const S = {
  crf_choices: LADDER,
  crf_default: 14,
  // The real default since 2026-09-06: an untouched row starts on VideoToolbox
  // CQ 70, and crf_default is only the x265 rung `smeltr crf` still answers.
  encoder_default: "vt_h265_10bit",
  quality_default: 70,
  encoder_choices: { x265_10bit: LADDER, vt_h265_10bit: CQ },
  can_start: true,
  stage_busy: false,
};
const row = (o?) =>
  Object.assign(
    {
      title: "Kubo (2016)",
      mbps: 91.2,
      bytes: 70 * 1073741824,
      crf: 14,
      crf_set: false,
      skipped: false,
      pinned: false,
      encoding: false,
      ready: false,
      staged: true,
      next_up: false,
      arriving_bytes: null,
      stage_queued: null,
    },
    o || {},
  );

sect("the menu is the ladder");
{
  const sel = ui.crfPicker(row(), S);
  const values = sel.children.map((o) => o.value);
  check("it is a <select>", sel.tagName === "select", "got " + sel.tagName);
  const want = [""]
    .concat(LADDER.map((c) => "x265_10bit:" + c))
    .concat(CQ.map((c) => "vt_h265_10bit:" + c));
  check(
    "one option per rung of every encoder, plus auto",
    JSON.stringify(values) === JSON.stringify(want),
    JSON.stringify(values),
  );
  check("no off-ladder rung is offered (24 was a dead end)", values.indexOf("x265_10bit:24") < 0);
  const labels = sel.children.slice(1).map((o) => o.textContent);
  check(
    "every option names its own scale — a bare number spans two of them",
    labels.every((t) => /^(CRF|VT CQ) \d+$/.test(t)),
    JSON.stringify(labels),
  );
  check(
    "the CQ rungs are labelled CQ, never CRF",
    CQ.every((c) => labels.indexOf("VT CQ " + c) >= 0),
    JSON.stringify(labels),
  );
  check(
    "the auto option names the default it follows",
    sel.children[0].textContent === "VT CQ 70 (auto)",
    sel.children[0].textContent,
  );
  check(
    "an untouched row sits on auto, not on a pinned 14",
    sel.value === "",
    JSON.stringify(sel.value),
  );
  check(
    "it is labelled for a screen reader",
    /Kubo/.test(sel.attrs["aria-label"] || ""),
    sel.attrs["aria-label"],
  );
}

sect("a hand-picked value reads back");
{
  const sel = ui.crfPicker(row({ crf: 12, crf_set: true }), S);
  check("the chosen rung is selected", sel.value === "x265_10bit:12", sel.value);
  check("and the cell says a human chose it", / set$| set /.test(sel.className), sel.className);
  check("its tooltip says so too, in words", /Chosen by hand/.test(sel.title), sel.title);
}
{
  const sel = ui.crfPicker(row(), S);
  check(
    "an untouched row does NOT claim a human chose it",
    !/ set$| set /.test(sel.className),
    sel.className,
  );
  check(
    "its tooltip names the default it is following",
    /default \(VT CQ 70\)/.test(sel.title),
    sel.title,
  );
}

sect("what a change actually sends");
{
  posted.length = 0;
  const sel = ui.crfPicker(row(), S);
  sel.value = "x265_10bit:12";
  sel.fire("change");
  check(
    "picking an x265 rung writes that rung",
    posted.length === 1 &&
      posted[0][0] === "/api/queue/crf" &&
      posted[0][1].crf === 12 &&
      posted[0][1].title === "Kubo (2016)",
    JSON.stringify(posted),
  );
}
{
  posted.length = 0;
  const sel = ui.crfPicker(row(), S);
  sel.value = "vt_h265_10bit:60";
  sel.fire("change");
  check(
    "picking a hardware rung goes to the ENCODER endpoint, with its quality",
    posted.length === 1 &&
      posted[0][0] === "/api/queue/encoder" &&
      posted[0][1].encoder === "vt_h265_10bit" &&
      posted[0][1].quality === 60,
    JSON.stringify(posted),
  );
}
{
  const sel = ui.crfPicker(row({ enc: "vt_h265_10bit", enc_q: 65 }), S);
  check("a hand-set encoder reads back", sel.value === "vt_h265_10bit:65", sel.value);
  check("and the cell says a human chose it", / set$| set /.test(sel.className), sel.className);
  check(
    "its tooltip names the encoder, not just a number",
    /vt_h265_10bit/.test(sel.title),
    sel.title,
  );
}
{
  posted.length = 0;
  const sel = ui.crfPicker(row({ crf: 12, crf_set: true }), S);
  sel.value = "";
  sel.fire("change");
  check(
    "picking auto CLEARS the override rather than pinning the default",
    posted.length === 1 && posted[0][1].crf === null,
    JSON.stringify(posted),
  );
}

sect("an open picker defers the repaint, and releases it");
{
  const sel = ui.crfPicker(row(), S);
  sel.fire("focus");
  check(
    "focus marks the row so paint() skips the rebuild",
    ui.open() === "Kubo (2016)",
    String(ui.open()),
  );
  sel.fire("blur");
  check("blur releases it", ui.open() === null, String(ui.open()));
}
{
  const sel = ui.crfPicker(row(), S);
  sel.fire("focus");
  sel.value = "x265_10bit:18";
  sel.fire("change");
  check(
    "a committed change releases it too — the response repaints, and the " +
      "deferral must not wedge against a node that is about to be replaced",
    ui.open() === null,
    String(ui.open()),
  );
}

sect("one quality control per row");
{
  const acts = ui.rowActions(row({ ready: true }), S);
  const selects = acts.children.filter((c) => c.tagName === "select");
  check(
    "the hover actions carry no second picker",
    selects.length === 0,
    "found " + selects.length,
  );
  const go = acts.children.filter((c) => /\bgo\b/.test(c.className))[0];
  check("start encode is still offered on the ready row", !!go);
  // An untouched row starts on the pipeline default, which is VideoToolbox
  // CQ 70 since 2026-09-06 -- never the x265 rung crf_default still carries.
  check(
    "and it names the row's real start (the default)",
    go && /VT CQ 70/.test(go.title),
    go && go.title,
  );
  const goSet = ui
    .rowActions(row({ ready: true, crf: 16, crf_set: true }), S)
    .children.filter((c) => /\bgo\b/.test(c.className))[0];
  check(
    "a hand-picked CRF row names its x265 rung",
    goSet && /CRF 16/.test(goSet.title),
    goSet && goSet.title,
  );
}
{
  posted.length = 0;
  const acts = ui.rowActions(row({ ready: true, enc: "vt_h265_10bit", enc_q: 55 }), S);
  const go = acts.children.filter((c) => /\bgo\b/.test(c.className))[0];
  check("a hardware row's start button names CQ, not CRF", /VT CQ 55/.test(go.title), go.title);
  go.fire("click");
  go.fire("click");
  check(
    "and it starts on that encoder — a CQ handed over as a CRF is near-lossless",
    posted.length === 1 && posted[0][1].encoder === "vt_h265_10bit" && posted[0][1].crf === 55,
    JSON.stringify(posted),
  );
}
{
  posted.length = 0;
  const acts = ui.rowActions(row({ ready: true, crf: 20, crf_set: true }), S);
  const go = acts.children.filter((c) => /\bgo\b/.test(c.className))[0];
  go.fire("click"); // arms
  go.fire("click"); // fires
  check(
    "starting by hand uses the row's planned CRF, not a hard-coded default",
    posted.length === 1 && posted[0][0] === "/api/encode/start" && posted[0][1].crf === 20,
    JSON.stringify(posted),
  );
}

sect("a running encode gets no picker");
{
  /* The branch lives inside renderQueue, which is too entangled with the
     table builder to call here -- so this reads the source. Weaker than
     driving it, and still worth having: the failure it guards is a control
     offered on a row whose -q was fixed hours ago. */
  const cell = src.slice(src.indexOf("var crfTd;"), src.indexOf("tr.appendChild(crfTd);"));
  check(
    "the encoding branch renders TEXT, never crfPicker",
    /r\.encoding\)\s*\{[\s\S]*?crfTd\s*=\s*el\("td",\s*"n q-crf",\s*qLabel\(/.test(cell) &&
      cell.indexOf("crfPicker") > cell.search(/else\s*\{/),
    cell,
  );
  check(
    "and it labels the scale of the encoder ACTUALLY running, not the plan",
    /var le = lc \? lc\.enc : r\.enc;/.test(cell),
    cell,
  );
  check(
    "the skipped row still claims no number",
    /r\.skipped\)\s*\{\s*crfTd\s*=\s*el\("td",\s*"n q-crf",\s*"—"\)/.test(cell),
    cell,
  );
}

sect("paint() hands the picker the defaults it names");
{
  /* crfPicker reads encoder_default/quality_default off the object paint()
     builds for renderQueue -- NOT the raw state. On 2026-09-07 that object
     carried crf_default and neither of the other two, so every auto row on
     the live page read "CRF 14 (auto)" while the driver started VT CQ 70.
     The unit tests above passed because they hand crfPicker a full state. */
  const at = src.indexOf("? renderQueue(");
  const call = src.slice(at, src.indexOf("s.live,", at));
  check(
    "renderQueue's state carries encoder_default",
    /encoder_default:\s*s\.encoder_default/.test(call),
    call,
  );
  check(
    "renderQueue's state carries quality_default",
    /quality_default:\s*s\.quality_default/.test(call),
    call,
  );
  check("and still crf_default", /crf_default:\s*s\.crf_default/.test(call), call);
}

console.log(failed ? "\ncrf picker: " + failed + " FAILED" : "\ncrf picker: all passed");
process.exit(failed ? 1 : 0);
