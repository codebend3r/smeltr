/* The pause switch, run against the real functions in web/app.js.
 *
 *   bun tests/test_pause_toggle.js
 *
 * The switch used to disable itself for the whole POST round-trip. That
 * round-trip is NOT short: /api/pause answers with a freshly built state
 * payload, which stats the NAS roots over SMB -- 0.5-1.0 s on this machine.
 * A click inside that window landed on a disabled button, so it never
 * reached api() and never even raised the "another action is still in
 * flight" notice, which is the one outcome that notice exists to prevent.
 * Driving the real page over CDP, six of ten rapid clicks vanished with no
 * response of any kind.
 *
 * So the switch never disables. Two rules replace it, and both are pinned
 * here:
 *
 *   1. The click is drawn IMMEDIATELY -- pauseState() prefers the user's
 *      unsettled intent over the server's committed value, so the flip is
 *      never waiting on SMB.
 *   2. The LAST click wins. A click while a write is in flight is recorded,
 *      not dropped, and the in-flight call drains it when it lands. Two
 *      round-trips are never in flight at once (the old single-flight
 *      property is kept; only the silent refusal is gone).
 *
 * A write that never took -- denied from the network, a dead server -- must
 * still snap the switch back to the truth rather than leave the optimistic
 * flip standing, so that is pinned too.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "..", "web", "app.css"), "utf8");

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

/* Minimal DOM. The one behaviour that matters: a disabled button swallows
   the click without running a handler -- which is exactly how the old switch
   lost them. */
function node(tag, cls?, text?) {
  return {
    tagName: tag,
    className: cls || "",
    textContent: text == null ? "" : text,
    disabled: false,
    attrs: {},
    handlers: [],
    children: [],
    setAttribute(k, v) {
      this.attrs[k] = v;
    },
    getAttribute(k) {
      return this.attrs[k];
    },
    appendChild(c) {
      this.children.push(c);
      return c;
    },
    addEventListener(_ev, h) {
      this.handlers.push(h);
    },
    click() {
      if (this.disabled) return false;
      this.handlers.forEach((h) => h());
      return true;
    },
  };
}

/* A controllable api(): every call is recorded and its promise is settled by
   the test, so "while a write is in flight" is a state the test can hold. */
function harness() {
  const calls = [];
  const notices = [];
  const api = (path, payload) => {
    let settle;
    const p = new Promise((res) => {
      settle = res;
    });
    calls.push({
      path,
      payload,
      settle: () => {
        settle();
        return p;
      },
      p,
    });
    return p;
  };
  const notice = (t) => notices.push(t);

  const code = [
    /* Mirrors the two module-level vars in web/app.js; the assertion below
       fails if either is renamed or dropped there. */
    "var pauseWant=null, pauseBusy=false;",
    "var last={state:{summary:{paused:false}}};",
    /* Stands in for paint(): re-derives what the card would draw. */
    "var drawn=null;",
    "function paint(s){ drawn=pauseState(s.summary); }",
    fn("pauseState"),
    fn("pauseSend"),
    fn("pauseSwitch"),
    "return {pauseState,pauseSend,pauseSwitch," +
      "setState:function(p){last.state={summary:{paused:p}};}," +
      "drawn:function(){return drawn;}," +
      "server:function(){return last.state.summary.paused;}};",
  ].join("\n");
  const mod = new Function("el", "api", "notice", code)(node, api, notice);
  return { mod, calls, notices };
}

let section = "";
const sect = (s) => {
  section = s;
};
let failed = 0;
function check(name, cond, detail?) {
  if (cond) process.stdout.write(".");
  else {
    failed++;
    console.log("\nFAIL [" + section + "] " + name + (detail ? "\n     " + detail : ""));
  }
}
const tick = () => new Promise((r) => setImmediate(r));

(async function () {
  sect("the harness still mirrors web/app.js");
  check(
    "pauseWant and pauseBusy are module-level vars there",
    /var\s+pauseWant\s*=\s*null;/.test(src) && /var\s+pauseBusy\s*=\s*false;/.test(src),
    "the injected preamble stands in for them and would silently diverge",
  );
  check(
    "renderLive draws through pauseState, not summary.paused directly",
    /var\s+paused\s*=\s*pauseState\(s\);/.test(src),
    "an unsettled click would wait for SMB before it showed",
  );

  sect("the control is ONE native button -- mouse, finger and keyboard");
  {
    const { mod, calls } = harness();
    const row = mod.pauseSwitch(false, "pause after this encode");
    const pill = row.children[0],
      lbl = row.children[1];
    check(
      "the control itself is a <button>",
      row.tagName === "button",
      "a div only gets a tap-synthesised click on SOME platforms -- iOS Safari " +
        "was the one where it did not, which is where this is watched",
    );
    check("it is typed, so it never submits anything", row.type === "button");
    check(
      "it is the switch a screen reader drives",
      row.getAttribute("role") === "switch" && row.getAttribute("aria-checked") === "false",
    );
    check(
      "the sentence is INSIDE it, so aiming at the words works",
      lbl.className.indexOf("swt-label") >= 0 && lbl.textContent.length > 0,
      "only the 36x20 pill used to be clickable",
    );
    check(
      "the pill is decoration inside it, not a second control",
      pill.tagName === "span" &&
        pill.getAttribute("aria-hidden") === "true" &&
        pill.handlers.length === 0,
    );
    check("exactly one handler -- two would fire twice and toggle back", row.handlers.length === 1);
    row.click();
    check(
      "one write, from a click anywhere on it",
      calls.length === 1 && calls[0].payload.paused === true,
    );
  }

  sect("and its CSS makes it a real target on a touch screen");
  {
    check(
      "it hugs pill+sentence rather than the whole card width",
      /\.pauserow\s*\{[^}]*display:\s*inline-flex/.test(css),
      "a full-width target would toggle the pipeline on a stray tap",
    );
    check(
      "it carries the button reset now that it IS the button",
      /\.pauserow\s*\{[^}]*border:\s*0/.test(css) &&
        /\.pauserow\s*\{[^}]*background:\s*none/.test(css),
    );
    check("it says it is clickable", /\.pauserow\s*\{[^}]*cursor:\s*pointer/.test(css));
    check(
      "taps are not delayed by double-tap zoom",
      /\.pauserow\s*\{[^}]*touch-action:\s*manipulation/.test(css),
    );
    check(
      "a finger gets a 44px target",
      /@media \(pointer:\s*coarse\)\s*\{\s*\.pauserow\s*\{\s*min-height:\s*44px;?\s*\}/.test(css),
      "the pill is only 20px tall",
    );
    check("focus is still visible on the button", /\.pauserow:focus-visible\s*\{/.test(css));
    check(
      "the pill no longer claims to be interactive itself",
      !/\.swt\s*\{[^}]*cursor:\s*pointer/.test(css) && !/\.swt:focus-visible/.test(css),
    );
  }

  sect("a click is never swallowed");
  {
    const { mod, calls } = harness();
    const row = mod.pauseSwitch(false, "pause after this encode");
    const sw = row.children[0];
    check("the switch does not disable itself", sw.disabled === false);
    check("clicking it is accepted", row.click() === true);
    check(
      "one write went out, asking to pause",
      calls.length === 1 && calls[0].payload.paused === true,
      JSON.stringify(calls.map((c) => c.payload)),
    );
    check(
      "the flip is drawn before the server answers -- not after SMB",
      mod.drawn() === true,
      "drawn=" + mod.drawn(),
    );
    check("but the server value has NOT been assumed", mod.server() === false);
  }

  sect("the last click wins");
  {
    const { mod, calls } = harness();
    /* Click 1: pause. Nothing has settled, so this write is in flight. */
    mod.pauseSwitch(false, "").click();
    check("write #1 asks to pause", calls.length === 1 && calls[0].payload.paused === true);
    /* Click 2 lands inside that window -- the case that used to vanish. */
    mod.pauseSwitch(true, "").click(); /* resume */
    check(
      "no second round-trip races the first",
      calls.length === 1,
      "saw " + calls.length + " concurrent writes",
    );
    check("the newest intent is what the card draws", mod.drawn() === false);
    calls[0].settle();
    await tick();
    await tick();
    check(
      "the queued intent is sent when the first write lands",
      calls.length === 2,
      "saw " + calls.length + " writes",
    );
    check(
      "and it carries the LAST click",
      calls.length === 2 && calls[1].payload.paused === false,
      calls.length === 2 ? JSON.stringify(calls[1].payload) : "no second write",
    );
  }

  sect("clicking back to where it started asks for nothing");
  {
    const { mod, calls } = harness();
    mod.pauseSwitch(false, "").click(); /* pause */
    mod.pauseSwitch(true, "").click(); /* resume */
    mod.pauseSwitch(false, "").click(); /* pause again */
    /* api() assigns the server's fresh payload to last.state before our
       continuation runs, so model that: the write it sent DID commit. */
    mod.setState(true);
    calls[0].settle();
    await tick();
    await tick();
    check(
      "the settled write already carries the final intent -- no redundant POST",
      calls.length === 1,
      "saw " + calls.length + " writes for a round trip back",
    );
    check("and the card still draws it", mod.drawn() === true);
  }

  sect("a settled write hands the switch back to the server");
  {
    const { mod, calls } = harness();
    mod.pauseSwitch(false, "").click();
    mod.setState(true); /* the server agreed */
    calls[0].settle();
    await tick();
    await tick();
    check("intent is released once it is committed", mod.pauseState({ paused: true }) === true);
    check(
      "and the server value alone now decides",
      mod.pauseState({ paused: false }) === false,
      "a stale optimistic flip outlived its write",
    );
  }

  sect("a write that never took snaps back to the truth");
  {
    const { mod, calls } = harness();
    mod.pauseSwitch(false, "").click();
    check("the optimistic flip is showing", mod.drawn() === true);
    /* api() resolves even on failure -- it catches and raises a notice --
       so "failed" reaches here as a settled write the server never honoured. */
    calls[0].settle();
    await tick();
    await tick();
    check(
      "the switch returns to the un-paused truth",
      mod.drawn() === false,
      "a denied LAN write would leave the switch lying",
    );
  }

  sect("a later click still works after all of that");
  {
    const { mod, calls } = harness();
    for (let i = 0; i < 6; i++) {
      const on = i % 2 === 1;
      mod.pauseSwitch(on, "").click();
      calls[calls.length - 1].settle();
      await tick();
      await tick();
      mod.setState(!on);
    }
    check("six consecutive toggles produced six writes", calls.length === 6, "saw " + calls.length);
    check(
      "they alternate",
      calls.map((c) => c.payload.paused).join(",") === "true,false,true,false,true,false",
      calls.map((c) => c.payload.paused).join(","),
    );
  }

  console.log(failed ? "\nFAILED: " + failed : "\nAll pause-toggle tests passed");
  process.exit(failed ? 1 : 0);
})();
