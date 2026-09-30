/* Screenshot the resource monitor at every zoom stop, in both themes --
 * or, with --tab, one tab's table card at a device width.
 *
 *   bun tests/visual/shoot.mts [--out DIR] [--depth-seconds N] [--stop N]
 *                              [--viewport desktop|tablet|phone]
 *                              [--tab queue|ledger|errors|procs|blacklist|events]
 *                              [--target ELEMENT_ID|viewport]
 *
 * --target names the element to clip (default `tablewrap` with --tab,
 * `sysmon` without). `viewport` scrolls the table card to the top and shoots
 * the screen as a person holding the phone sees it: the sticky tab strip and
 * table header stacked above the rows. On the phone every --tab shot is
 * taken twice, the second time with the first openable row tapped open.
 *
 * This is the EYEBALL half of the monitor's coverage. tests/test_sysmon_render.js
 * asserts what drawMon() emits; this renders the real page in a real browser,
 * where CSS, layout, fonts and the theme tokens are also on trial -- the 7 d
 * widening shipped a chart that passed every arithmetic test and still looked
 * broken, because 55 of 60 minutes were bare wash.
 *
 * No dependency and nothing installed: it drives the Chrome already on
 * this Mac over the DevTools Protocol using bun's built-in WebSocket, the
 * same way `bun run lint:py` reaches ruff through uvx. Absent Chrome, it SKIPS LOUDLY.
 *
 * It never touches the live dashboard. A throwaway server is started against
 * a temp SMELTR_DIR holding a synthetic ring (tests/visual/seed_ring.py), so
 * the charts show a full week and the ring beside the ledger is untouched.
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");

const CHROMES = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
];

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---- CDP: request/response over one WebSocket, flat-session mode --------- */
/* One in-flight request. `res` takes `unknown` because the reply is whatever
   the browser sent; `send<T>` is where a caller states the shape it expects. */
type PendingCall = {
  res: (v: unknown) => void;
  rej: (e: Error) => void;
  method: string;
};

class CDP {
  /* Declared, not just assigned in the constructor: a TS class has no
     implicit fields. `declare` erases at build time, so the emitted class is
     byte-for-byte the one this file has always run. */
  declare ws: WebSocket;
  declare id: number;
  declare pending: Map<number, PendingCall>;

  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
  }
  static async attach(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.onopen = res;
      ws.onerror = () => rej(new Error("cannot open " + wsUrl));
    });
    const cdp = new CDP(ws);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      const p = cdp.pending.get(msg.id);
      if (!p) return; // an event, not a reply
      cdp.pending.delete(msg.id);
      if (msg.error) p.rej(new Error(msg.method + ": " + JSON.stringify(msg.error)));
      else p.res(msg.result);
    };
    return cdp;
  }
  /* T is the caller's claim about the reply shape -- the protocol's, not
     something inferable here. The one cast is that claim meeting the
     `unknown` the socket actually delivers. */
  send<T = unknown>(method, params = {}, sessionId?): Promise<T> {
    const id = ++this.id;
    const msg: { id: number; method: string; params: object; sessionId?: string } = {
      id,
      method,
      params,
    };
    if (sessionId) msg.sessionId = sessionId;
    this.ws.send(JSON.stringify(msg));
    return new Promise<T>((res, rej) => {
      this.pending.set(id, { res: res as (v: unknown) => void, rej, method });
      setTimeout(() => {
        if (this.pending.delete(id)) rej(new Error(method + " timed out"));
      }, 30000);
    });
  }
}

/* Evaluate an expression in the page and return its JSON value. The page is
 * CSP `default-src 'none'` with nonced scripts; Runtime.evaluate is not
 * subject to that, which is why this works without weakening the policy. */
async function evaluate(cdp, session, expression) {
  const r = await cdp.send(
    "Runtime.evaluate",
    {
      expression,
      returnByValue: true,
      awaitPromise: true,
    },
    session,
  );
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " :: " + expression);
  return r.result.value;
}

async function waitFor(cdp, session, expression, what, timeoutMs = 20000) {
  const until = Date.now() + timeoutMs;
  for (;;) {
    if (await evaluate(cdp, session, expression)) return;
    if (Date.now() > until) throw new Error("timed out waiting for " + what);
    await sleep(150);
  }
}

function die(msg) {
  console.error(msg);
  process.exit(1);
}
function skip(msg) {
  console.log("SKIP " + msg);
  process.exit(0);
}

/* ------------------------------------------------------------------ main -- */
const chrome = CHROMES.find((p) => fs.existsSync(p));
if (!chrome) skip("visual shots: no Chrome/Chromium/Edge on this machine");

const outDir = path.resolve(arg("--out", path.join(os.tmpdir(), "smeltr-visual")));
fs.mkdirSync(outDir, { recursive: true });
const depth = arg("--depth-seconds", String(604800));
const onlyStop = arg("--stop", null);

/* The three tiers the CSS draws. Tablet and phone are `mobile` so the page's
 * viewport meta applies as it would on the device. `mobile` alone does NOT
 * make `(pointer: coarse)` match -- that takes touch emulation, switched on
 * below for every mobile viewport, so the coarse-pointer rules are on trial
 * too. */
type Viewport = { width: number; height: number; deviceScaleFactor: number; mobile: boolean };
const VIEWPORTS: Record<string, Viewport> = {
  desktop: { width: 1280, height: 1400, deviceScaleFactor: 2, mobile: false },
  tablet: { width: 820, height: 1180, deviceScaleFactor: 2, mobile: true },
  phone: { width: 390, height: 844, deviceScaleFactor: 3, mobile: true },
};
const viewportName = arg("--viewport", "desktop");
const viewport = VIEWPORTS[viewportName];
if (!viewport) die("--viewport must be one of " + Object.keys(VIEWPORTS).join(", "));
/* --tab shoots the table section of one tab instead of the monitor stops. */
const TABS = ["queue", "ledger", "errors", "procs", "blacklist", "events"];
const onlyTab = arg("--tab", null);
if (onlyTab && !TABS.includes(onlyTab)) die("--tab must be one of " + TABS.join(", "));
const target = arg("--target", onlyTab ? "tablewrap" : "sysmon");

const work = fs.mkdtempSync(path.join(os.tmpdir(), "smeltr-shoot-"));
const smeltrDir = path.join(work, "smeltr");
const profile = path.join(work, "chrome");
fs.mkdirSync(smeltrDir, { recursive: true });

const cleanups = [];
function cleanup() {
  while (cleanups.length) {
    try {
      cleanups.pop()();
    } catch {
      /* best effort */
    }
  }
  try {
    fs.rmSync(work, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}
process.on("exit", cleanup);
for (const sig of ["SIGINT", "SIGTERM"])
  process.on(sig, () => {
    cleanup();
    process.exit(1);
  });

/* 1. Synthetic ring, in a directory the live server knows nothing about. */
const seed = spawnSync(
  "python3",
  [path.join(HERE, "seed_ring.py"), smeltrDir, "--depth-seconds", depth],
  { encoding: "utf8" },
);
if (seed.status !== 0) die("seed_ring.py failed:\n" + (seed.stderr || seed.stdout));
console.log("seeded  " + seed.stdout.trim());

/* The tables need rows. The calibration ledger is the fixture with the
 * most shapes (exact and estimated sizes, notes, several destinations). */
fs.copyFileSync(
  path.join(REPO, "tests", "fixtures", "ledger-calibration.jsonl"),
  path.join(smeltrDir, "ledger.jsonl"),
);

/* 2. A throwaway dashboard on loopback, bound to that directory. */
const server = spawn("python3", [path.join(REPO, "dashboard", "server.py")], {
  /* SMELTR_X9 points at a path that does not exist, on purpose. Left
   * unset, the throwaway server resolves the operator's live staging drive,
   * and its start-up orphan-pull sweep could rmtree a real half-pull there.
   * Never create this directory. */
  env: {
    ...process.env,
    SMELTR_DIR: smeltrDir,
    SMELTR_BIND: "127.0.0.1",
    SMELTR_X9: path.join(work, "no-x9"),
  },
  stdio: ["ignore", "pipe", "pipe"],
});
cleanups.push(() => server.kill("SIGTERM"));
let serverErr = "";
server.stderr.on("data", (b) => {
  serverErr += b;
});
server.stdout.resume();

const urlFile = path.join(smeltrDir, "url");
let pageUrl = null;
for (let i = 0; i < 200 && !pageUrl; i++) {
  if (fs.existsSync(urlFile)) pageUrl = fs.readFileSync(urlFile, "utf8").trim();
  else await sleep(100);
}
if (!pageUrl) die("the throwaway server never wrote its url file\n" + serverErr);
console.log("serving " + pageUrl.replace(/\?t=.*/, "?t=<token>"));

/* 3. Headless Chrome, its DevTools port discovered from the profile dir. */
const browser = spawn(
  chrome,
  [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--remote-debugging-port=0",
    "--user-data-dir=" + profile,
    "--force-device-scale-factor=2",
    "about:blank",
  ],
  { stdio: ["ignore", "ignore", "pipe"] },
);
cleanups.push(() => browser.kill("SIGKILL"));
let browserErr = "";
browser.stderr.on("data", (b) => {
  browserErr += b;
});

const portFile = path.join(profile, "DevToolsActivePort");
let devPort = null;
for (let i = 0; i < 200 && !devPort; i++) {
  if (fs.existsSync(portFile)) {
    const line = fs.readFileSync(portFile, "utf8").split("\n")[0].trim();
    if (line) devPort = line;
  } else await sleep(100);
}
if (!devPort) die("Chrome never opened a DevTools port\n" + browserErr);

const version = await (await fetch(`http://127.0.0.1:${devPort}/json/version`)).json();
const cdp = await CDP.attach(version.webSocketDebuggerUrl);
const { targetId } = await cdp.send<{ targetId: string }>("Target.createTarget", {
  url: "about:blank",
});
const { sessionId } = await cdp.send<{ sessionId: string }>("Target.attachToTarget", {
  targetId,
  flatten: true,
});
await cdp.send("Page.enable", {}, sessionId);
await cdp.send("Runtime.enable", {}, sessionId);
await cdp.send("Emulation.setDeviceMetricsOverride", { ...viewport }, sessionId);
/* A phone and a tablet are touch devices: without this `(pointer: coarse)`
 * stays false and every coarse-pointer rule is skipped in the shot. */
if (viewport.mobile)
  await cdp.send(
    "Emulation.setTouchEmulationEnabled",
    { enabled: true, maxTouchPoints: 5 },
    sessionId,
  );

/* The url file carries `?t=<token>` only when the server wants one; a
 * loopback bind writes a bare url, so the tab goes in through URL, never a
 * hand-glued `&`. */
const navUrl = new URL(pageUrl);
if (onlyTab) navUrl.searchParams.set("tab", onlyTab);
await cdp.send("Page.navigate", { url: navUrl.toString() }, sessionId);
await waitFor(cdp, sessionId, 'document.readyState==="complete"', "the page to load");
/* The skeleton is replaced only when the first SSE frame lands. Shooting
 * before that photographs the boot skeleton, not the charts. */
await waitFor(
  cdp,
  sessionId,
  'document.body.classList.contains("booted")',
  "the first state frame",
);
await waitFor(cdp, sessionId, '!!document.getElementById("monZoom")', "the monitor card");
console.log(
  "coarse=" + (await evaluate(cdp, sessionId, 'matchMedia("(pointer: coarse)").matches')),
);

/* web/app.js is an IIFE, so the page exposes no globals to poke at. The
 * slider itself is the source of truth for how many stops there are, and
 * the label beside it is the source of truth for what each one is called --
 * which is the right coupling anyway: this suite should see exactly what a
 * person looking at the page sees. */
const nStops = 1 + (await evaluate(cdp, sessionId, '+document.getElementById("monZoom").max'));
const shots = [];

/* No tier may scroll the PAGE sideways; a wide table scrolls inside its own
 * card. Checked on every shot, printed, and failed at the end so one bad
 * cell of a matrix run does not hide the shots after it. */
let overflowed = false;
async function checkScrollWidth(when) {
  const sw = await evaluate(cdp, sessionId, "document.documentElement.scrollWidth");
  const ok = sw <= viewport.width;
  if (!ok) overflowed = true;
  console.log(`  width ${sw} <= ${viewport.width} ${ok ? "ok" : "FAIL"} (${when})`);
}

/* `overflow: clip` on the phone's .wrap hides a clipped cell from the page's
 * scrollWidth, so each table cell is measured too: it must sit inside the
 * card and hold its own content. Hidden revealed-line cells (.c-s) and
 * display:none cells (the phone's grip column) are skipped. The containment
 * half runs only where nothing may scroll sideways -- on the tablet the
 * table scrolls inside its card by design, so a cell past the card edge is
 * the fade's business, not a failure. */
async function checkCells(when) {
  const bad = await evaluate(
    cdp,
    sessionId,
    `(function(){
    var w=document.querySelector("#tablewrap > .wrap");
    if(!w)return null;
    var wr=w.getBoundingClientRect();
    var L=wr.left+w.clientLeft,R=L+w.clientWidth,T=wr.top+w.clientTop,B=T+w.clientHeight;
    var contain=${JSON.stringify(viewportName === "phone")};
    var cells=document.querySelectorAll("#pane td:not(.c-s), #pane th:not(.c-s)");
    for(var i=0;i<cells.length;i++){
      var c=cells[i];
      if(!c.getClientRects().length)continue;
      var r=c.getBoundingClientRect();
      var out=contain&&(r.left<L-1||r.right>R+1||r.top<T-1||r.bottom>B+1);
      if(out||c.scrollWidth>c.clientWidth+1)
        return {tag:c.tagName.toLowerCase(),cls:c.className,text:c.textContent.trim().slice(0,60),
          rect:[Math.round(r.left),Math.round(r.top),Math.round(r.right),Math.round(r.bottom)],
          card:[Math.round(L),Math.round(T),Math.round(R),Math.round(B)],
          sw:c.scrollWidth,cw:c.clientWidth};
    }
    return null;
  })()`,
  );
  if (bad) {
    overflowed = true;
    console.log(`  cells FAIL (${when}) first offender ${JSON.stringify(bad)}`);
  } else console.log(`  cells ok (${when})`);
}

/* A tab has settled when its table is drawn or its empty state is final.
 * "Loading events…" and "Listing processes…" are the fetch still in flight;
 * an empty Queue or Errors on this throwaway server is a real, final state. */
const TAB_SETTLED = `(function(){
  var p=document.getElementById("pane");
  if(!p)return false;
  if(p.querySelector("table"))return true;
  var e=p.querySelector(".empty");
  return !!e && !/^(Loading|Listing)/.test(e.textContent);
})()`;

async function shootTarget(theme, suffix) {
  let clip = null;
  if (target === "viewport") {
    await evaluate(
      cdp,
      sessionId,
      'document.getElementById("tablewrap").scrollIntoView({block:"start"}),true',
    );
    await sleep(200);
  } else {
    const box = await evaluate(
      cdp,
      sessionId,
      `(function(){var el=document.getElementById(${JSON.stringify(target)});if(!el)return null;var r=el.getBoundingClientRect();return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height};})()`,
    );
    if (!box) die("--target: no element with id " + target);
    clip = { ...box, scale: 1 };
  }
  const shot = await cdp.send<{ data: string }>(
    "Page.captureScreenshot",
    clip ? { format: "png", captureBeyondViewport: true, clip } : { format: "png" },
    sessionId,
  );
  const tgt = target === (onlyTab ? "tablewrap" : "sysmon") ? "" : "-" + target;
  const name = `${theme}-${viewportName}-${onlyTab}${tgt}${suffix}.png`;
  fs.writeFileSync(path.join(outDir, name), Buffer.from(shot.data, "base64"));
  shots.push({
    theme,
    label: `${viewportName} ${onlyTab}${tgt}${suffix}`,
    file: path.join(outDir, name),
  });
  console.log("  shot  " + name);
}

for (const theme of ["dark", "light"]) {
  await evaluate(
    cdp,
    sessionId,
    `document.documentElement.setAttribute("data-theme",${JSON.stringify(theme)})`,
  );
  if (onlyTab) {
    await waitFor(cdp, sessionId, TAB_SETTLED, "the " + onlyTab + " tab");
    await sleep(300);
    await checkScrollWidth(theme + " " + onlyTab);
    await checkCells(theme + " " + onlyTab);
    await shootTarget(theme, "");
    /* The phone folds each row to its primary cells; a tap opens it. Shoot
     * the first openable row open (the same `tr[data-key]` predicate the
     * page's disclosure handler uses), then close it for the next theme. */
    if (viewportName === "phone") {
      const opened = await evaluate(
        cdp,
        sessionId,
        '(function(){var r=document.querySelector("#pane tbody tr[data-key]");if(!r)return false;r.click();return r.classList.contains("open");})()',
      );
      if (opened) {
        await sleep(200);
        await checkScrollWidth(theme + " " + onlyTab + " open");
        await checkCells(theme + " " + onlyTab + " open");
        await shootTarget(theme, "-open");
        await evaluate(
          cdp,
          sessionId,
          '(function(){var r=document.querySelector("#pane tbody tr[data-key].open");if(r)r.click();return true;})()',
        );
      } else console.log("  (no openable row on " + onlyTab + ")");
    }
    continue;
  }
  await checkScrollWidth(theme + " monitor");
  for (let i = 0; i < nStops; i++) {
    if (onlyStop != null && String(i) !== onlyStop) continue;
    const label = await evaluate(
      cdp,
      sessionId,
      `(function(){
      var el=document.getElementById("monZoom");
      el.value=${JSON.stringify(String(i))};
      el.dispatchEvent(new Event("input",{bubbles:true}));
      return document.getElementById("monSpanLbl").textContent;
    })()`,
    );
    /* The wider stops re-fetch history; a shot taken mid-fetch is a photo of
     * the "loading history…" state, not of the window. */
    await waitFor(
      cdp,
      sessionId,
      'document.getElementById("monSince").textContent.indexOf("loading")<0',
      `the ${label} history fetch`,
    );
    await sleep(400); // one redraw, plus the width transition

    const box = await evaluate(
      cdp,
      sessionId,
      `(function(){
      var r=document.getElementById("sysmon").getBoundingClientRect();
      return {x:r.x,y:r.y,width:r.width,height:r.height};
    })()`,
    );
    const shot = await cdp.send<{ data: string }>(
      "Page.captureScreenshot",
      {
        format: "png",
        captureBeyondViewport: true,
        clip: { x: box.x, y: box.y, width: box.width, height: box.height, scale: 2 },
      },
      sessionId,
    );
    const name = `${theme}-${String(i).padStart(2, "0")}-${label.replace(/\s+/g, "")}.png`;
    fs.writeFileSync(path.join(outDir, name), Buffer.from(shot.data, "base64"));
    shots.push({ theme, label, file: path.join(outDir, name) });
    console.log("  shot  " + name);
  }
}

/* 4. One contact sheet per theme, so twelve windows can be compared at a
 *    glance rather than opened one file at a time. ImageMagick is optional.
 *    It ships without fontconfig on macOS, so `-label` needs a font FILE --
 *    a bare family name fails with "unable to read font ''" and takes the
 *    whole sheet with it. Unlabelled beats absent, so the font is optional
 *    too; the filenames carry the window either way. */
const magick = ["magick", "montage"].find(
  (c) => spawnSync("command", ["-v", c], { shell: true }).status === 0,
);
const FONTS = [
  "/System/Library/Fonts/Supplemental/Arial.ttf",
  "/System/Library/Fonts/Helvetica.ttc",
  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
];
const font = FONTS.find((f) => fs.existsSync(f));
for (const theme of ["dark", "light"]) {
  const set = shots.filter((s) => s.theme === theme);
  if (!magick || !set.length) continue;
  const sheet = path.join(outDir, `contact-${theme}.png`);
  const args = magick === "magick" ? ["montage"] : [];
  if (font) args.push("-font", font, "-pointsize", "30", "-fill", "#e8e8e8");
  for (const s of set) {
    if (font) args.push("-label", s.label);
    args.push(s.file);
  }
  args.push("-tile", "2x", "-geometry", "+10+10", "-background", "#1a1a1a", sheet);
  const r = spawnSync(magick, args, { encoding: "utf8" });
  if (r.status === 0) console.log("sheet   " + sheet);
  else console.log("sheet   skipped (" + (r.stderr || "").trim().split("\n")[0] + ")");
}

console.log(`\n${shots.length} shots in ${outDir}`);
cleanup();
if (overflowed) console.log("FAIL the page or a table cell overflows at " + viewportName);
process.exit(overflowed ? 1 : 0);
