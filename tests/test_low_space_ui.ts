/* The staging-drive floor on the page (2026-09-08), pinned against the real
 * source of web/app.js.
 *
 *   bun tests/test_low_space_ui.ts
 *
 * The driver WAITS under 100 GiB free (next_title exit 3). The page has to
 * say so in the three places a person looks -- the alert stack, the idle
 * live card, and the queue's "next" pill -- and every one of them must read
 * the ONE carrier, summary.low_space, never re-derive it from a byte count.
 * The idle card's rebuild signature must carry the flag (shape), never the
 * free-bytes figure (a live number that moves every frame).
 */
"use strict";
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");

function block(startIdx: number) {
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
function fn(name: string) {
  const at = src.indexOf("function " + name + "(");
  if (at < 0) throw new Error("not found in web/app.js: " + name);
  return block(at);
}

let pass = 0,
  fail = 0;
function ok(cond: unknown, msg: string) {
  if (cond) {
    pass++;
    process.stdout.write(".");
  } else {
    fail++;
    console.log("\nFAIL: " + msg);
    process.exit(1);
  }
}

const alert = fn("renderAlert");
ok(/s\.low_space/.test(alert), "renderAlert reads summary.low_space");
ok(/x9_free_bytes/.test(alert), "the alert names the free figure");
ok(
  /low_space_floor_bytes/.test(alert),
  "the alert names the floor from the payload, not a literal",
);
ok(!/100 GiB/.test(alert), "no hard-coded 100 GiB on the page -- the server owns the floor");

const live = fn("renderLive");
ok(
  /\|ls:/.test(live) && /s\.low_space/.test(live),
  "the idle card's signature carries the low_space flag",
);
ok(
  !/x9_free_bytes[^;]*sig|sig[^;]*x9_free_bytes/.test(live),
  "the signature never carries the free-bytes figure",
);
ok(/low space/i.test(live), "the idle card says why nothing is starting");

const queue = fn("renderQueue");
ok(/s\.low_space/.test(queue), "the queue's next pill knows about the floor");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
