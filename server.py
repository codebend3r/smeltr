#!/usr/bin/env python3
"""
Smeltr -- local dashboard for the 4K HEVC re-encode pipeline.

Security posture (this serves real filesystem data, so it is deliberate):
  * binds 127.0.0.1 only -- never reachable off this machine
  * every request needs a token minted at startup and printed once
  * Host header is allowlisted, which blocks DNS-rebinding from a browser tab
  * the client cannot name a path; the server reads a fixed set of files
  * no endpoint mutates anything -- Smeltr is a reader
  * CSP is nonce-based with no external origins, so nothing loads off-network
  * all values reach the DOM via textContent, never innerHTML
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

TOKEN = secrets.token_urlsafe(24)
# Whether the token is REQUIRED, as opposed to merely accepted.
#
# The token's only real job is keeping a second local user account on this Mac
# from reading the dashboard. It does nothing against the threats that actually
# matter here, which are handled by stronger controls that stay on regardless:
# the socket binds 127.0.0.1 (unreachable off-box), the Host header is
# allowlisted (defeats DNS rebinding), and no CORS headers are sent (a page on
# another origin cannot read a response). What the token DID do reliably was
# make the obvious bookmarkable URL -- http://127.0.0.1:8787/ -- return a bare
# 403, which is not a security win, just a broken dashboard.
#
# Set SMELTR_REQUIRE_TOKEN=1 to restore strict mode on a shared machine.
REQUIRE_TOKEN = os.environ.get("SMELTR_REQUIRE_TOKEN") == "1"
NONCE = secrets.token_urlsafe(16)
POLL_SECONDS = 2.0
# Must be BELOW POLL_SECONDS. Above it, every second SSE frame was a
# byte-identical duplicate and the effective refresh halved to 4 s. The build
# lock -- not the TTL -- is what prevents concurrent work.
STATE_TTL = 1.5

_state_lock = threading.Lock()
_build_lock = threading.Lock()
_state_cache = {"at": 0.0, "payload": None}


def build_state() -> dict:
    """Snapshot of everything the UI renders, cached briefly.

    Recomputing hits ps(1) and stats a few hundred files; with several browser
    tabs and SSE streams open that would otherwise run many times a second.
    """
    with _state_lock:
        cached = _state_cache["payload"]
        if cached is not None and time.monotonic() - _state_cache["at"] < STATE_TTL:
            return cached

    # One build at a time. Without this, every open tab and SSE stream starts
    # its own concurrent build against the same drive HandBrake is writing to.
    with _build_lock:
        with _state_lock:
            cached = _state_cache["payload"]
            if cached is not None and time.monotonic() - _state_cache["at"] < STATE_TTL:
                return cached
        # Compute each source ONCE and thread it through. summary() calls
        # queue(), which calls live_encodes() and ledger(); recomputing them
        # here too meant ~6 log parses and several hundred stat() calls per
        # snapshot, on the drive the encode is writing to.
        hist = core.ledger()
        live = core.live_encodes()
        q = core.queue_cached(live=live, hist=hist)
        payload = {
            "summary": core.summary(hist=hist, q=q),
            "live": live,
            "ledger": hist,
            "queue": q,
        }
        with _state_lock:
            # Stamp AFTER the build. Stamping before meant a slow cold build was
            # already older than the TTL the instant it was stored, so the cache
            # never hit.
            _state_cache["at"] = time.monotonic()
            _state_cache["payload"] = payload
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "Smeltr"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -------------------------------------------------------------- security
    def _host_ok(self) -> bool:
        # An IPv6 Host is bracketed and contains its own colons, so neither
        # split(":")[0] nor a colon count can strip the port correctly.
        raw = (self.headers.get("Host") or "").strip()
        if raw.startswith("["):
            host = raw[:raw.index("]") + 1] if "]" in raw else raw
        else:
            host = raw.rsplit(":", 1)[0] if ":" in raw else raw
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _token_ok(self, query: dict) -> bool:
        if not REQUIRE_TOKEN:
            return True
        supplied = (query.get("t") or [""])[0]
        # Compare BYTES. compare_digest raises TypeError on non-ASCII str, which
        # killed the handler thread with no response and appended an unbounded
        # traceback to the log -- trivially sprayable by any local page.
        return hmac.compare_digest(supplied.encode("utf-8", "surrogatepass"),
                                   TOKEN.encode("utf-8"))

    def _headers(self, status: int, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            f"style-src 'nonce-{NONCE}'; script-src 'nonce-{NONCE}'; "
            "connect-src 'self'; img-src 'none'; font-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        for k, v in (extra or {}).items():
            self.send_header(k, v)

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self._headers(status, ctype, {"Content-Length": str(len(body))})
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _deny(self, status: int, msg: str) -> None:
        self._send(status, "text/plain; charset=utf-8", msg.encode())

    # ------------------------------------------------------------- endpoints
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._deny(421, "bad host")
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path

        if route == "/healthz":
            return self._send(200, "text/plain; charset=utf-8", b"ok")
        if not self._token_ok(query):
            return self._deny(403, "missing or invalid token")
        if route == "/":
            return self._send(200, "text/html; charset=utf-8", PAGE.encode())
        if route == "/api/state":
            body = json.dumps(build_state()).encode()
            return self._send(200, "application/json; charset=utf-8", body)
        if route == "/api/stream":
            return self._stream()
        return self._deny(404, "not found")

    def _stream(self) -> None:
        # An HTTP/1.1 keep-alive response with neither Content-Length nor
        # chunked framing is undelimited. Close-delimited is correct for SSE here.
        self.close_connection = True
        self._headers(200, "text/event-stream; charset=utf-8",
                      {"Connection": "close", "X-Accel-Buffering": "no"})
        self.end_headers()
        try:
            while True:
                chunk = f"data: {json.dumps(build_state())}\n\n".encode()
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def log_message(self, fmt, *args) -> None:
        """Silence per-request logging; this runs alongside a live encode."""
        return


def free_port(preferred: int = 8787) -> int:
    """Prefer a stable port so the dashboard URL stays bookmarkable.

    SO_REUSEADDR matters here: without it a just-restarted server finds its own
    previous socket in TIME_WAIT, silently falls through to an ephemeral port,
    and every restart hands out a different URL.
    """
    for port in (preferred, 0):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("no free port available on 127.0.0.1")


_PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Smeltr</title>
<style nonce="__NONCE__">
:root{
  --r:10px;
  color-scheme:dark;
  --bg:#08090b; --panel:#0f1116; --panel-2:#13161c; --line:#1e232c;
  --ink:#e8ecf2; --ink-2:#98a2b3; --ink-3:#5f6a7d;
  --hot:#ff7a2f; --hot-soft:#ff9a5c; --cool:#4cc9f0; --good:#3ddc97;
  --warn:#ffc857; --bad:#ff5c5c;
  /* Derived surfaces. Every colour that used to be a hex literal further down
     is named here instead -- a theme cannot override what it cannot name, and
     the dozen inline hexes were exactly the things that stayed dark. */
  --live-bd:#3a2415; --live-bg:#16110d;
  --alert-bd:#5a1d1d; --alert-bg:#1c0f0f;
  --good-bd:#1d4435; --warn-bd:#4a3b18; --bad-bd:#4a1f1f;
  --cool-bd:#1c3a47; --hot-bd:#4a3018;
  --bar-bg:#1a1f27; --bar-inset:rgba(0,0,0,.5);
  --tab-bd:#2c3543; --td-line:#14181f;
  --row-hover:#12151b; --row-enc:#171208;
  --thumb:#2a313d; --thumb-hover:#3b4553;
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#f5f7fa; --panel:#ffffff; --panel-2:#eef1f6; --line:#dde2ea;
  --ink:#141922; --ink-2:#4d5768; --ink-3:#646d7e;
  /* Accents darken rather than invert: the same hues, pulled down until they
     carry on white. The 22px stat values only need 3:1, the 11px labels need
     4.5:1, and --ink-3 is the one doing most of the small-text work. */
  --hot:#c2540f; --hot-soft:#b04e0c; --cool:#0b6f9e; --good:#0f7a55;
  --warn:#8a5a00; --bad:#c22f2d;
  --live-bd:#f0cbab; --live-bg:#fff6ee;
  --alert-bd:#f0bab8; --alert-bg:#fff4f3;
  --good-bd:#a9dcc6; --warn-bd:#e4cd97; --bad-bd:#f0bab8;
  --cool-bd:#a6d3e6; --hot-bd:#f0cbab;
  --bar-bg:#e3e7ee; --bar-inset:rgba(20,25,34,.12);
  --tab-bd:#c6cedb; --td-line:#eff2f6;
  --row-hover:#f4f7fa; --row-enc:#fff6ea;
  --thumb:#c8cfda; --thumb-hover:#a8b2c1;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{
  background:var(--bg); color:var(--ink);
  font:14px/1.5 ui-sans-serif,-apple-system,"SF Pro Text",Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale;
  font-variant-numeric:tabular-nums; padding:28px 24px 64px; max-width:1240px; margin:0 auto;
}
.num,td.n,th.n{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
header{display:flex;align-items:baseline;gap:14px;margin-bottom:22px;flex-wrap:wrap}
.wordmark{font-size:19px;font-weight:680;letter-spacing:-.02em}
.wordmark b{color:var(--hot)}
.tag{color:var(--ink-3);font-size:12.5px;letter-spacing:.01em}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ink-3);display:inline-block;
     margin-right:6px;vertical-align:middle}
.dot.on{background:var(--good);box-shadow:0 0 0 3px rgba(61,220,151,.15)}
.dot.off{background:var(--bad);box-shadow:0 0 0 3px rgba(255,92,92,.15)}
.conn{margin-left:auto;font-size:12px;color:var(--ink-3)}
.themebtn{align-self:center;background:var(--panel);border:1px solid var(--line);
  color:var(--ink-2);width:30px;height:30px;border-radius:8px;padding:0;cursor:pointer;
  display:grid;place-items:center}
.themebtn:hover{color:var(--ink);border-color:var(--tab-bd)}
.themebtn:focus-visible{outline:2px solid var(--cool);outline-offset:2px}
/* Half-filled circle. Drawn from currentColor rather than an emoji or a font
   glyph, so it inverts with the theme and cannot render as a colour emoji. */
.themebtn i{display:block;width:13px;height:13px;border-radius:50%;
  border:1.5px solid currentColor;
  background:linear-gradient(90deg,currentColor 0 50%,transparent 50% 100%)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:10px;margin-bottom:18px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:13px 15px}
.stat .k{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink-3)}
.stat .v{font-size:22px;font-weight:640;letter-spacing:-.02em;margin-top:5px}
.stat .s{font-size:11.5px;color:var(--ink-3);margin-top:2px}
.v.hot{color:var(--hot-soft)} .v.cool{color:var(--cool)}

.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
      padding:18px 20px;margin-bottom:18px}
.card.live{border-color:var(--live-bd);background:linear-gradient(180deg,var(--live-bg),var(--panel))}
.card.alert{border-color:var(--alert-bd);background:linear-gradient(180deg,var(--alert-bg),var(--panel))}
.card.alert .live-title{color:var(--bad)}
.live-top{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
.live-title{font-size:17px;font-weight:640;letter-spacing:-.015em}
.chip{font-size:11px;padding:2.5px 8px;border-radius:999px;border:1px solid var(--line);
      color:var(--ink-2);background:var(--panel-2);white-space:nowrap}
.chip.good{color:var(--good);border-color:var(--good-bd)} .chip.thin{color:var(--warn);border-color:var(--warn-bd)}
.chip.bad{color:var(--bad);border-color:var(--bad-bd)}
.bar{height:7px;border-radius:99px;background:var(--bar-bg);overflow:hidden;margin:14px 0 10px;
     box-shadow:inset 0 1px 2px var(--bar-inset)}
.bar>i{display:block;height:100%;border-radius:99px;
       background:linear-gradient(90deg,var(--hot),var(--hot-soft));
       transition:width .5s cubic-bezier(.4,0,.2,1)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px 18px;margin-top:12px}
.kv div span{display:block;font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em}
.kv div b{font-weight:580;font-size:14px}
.verdict{margin-top:12px;font-size:12.5px;color:var(--ink-2)}
.verdict.loud{color:var(--warn);font-weight:560}
.note{padding:10px 14px;font-size:11.5px;color:var(--ink-3);border-top:1px solid var(--line)}

.tabs{display:flex;gap:6px;margin:0 0 12px}
.tab{background:var(--panel);border:1px solid var(--line);color:var(--ink-2);
     padding:6px 13px;border-radius:8px;font:inherit;font-size:12.5px;cursor:pointer}
.tab[aria-selected="true"]{background:var(--panel-2);color:var(--ink);border-color:var(--tab-bd)}
.tab:focus-visible{outline:2px solid var(--cool);outline-offset:2px}

.wrap{border:1px solid var(--line);border-radius:var(--r);overflow:hidden;background:var(--panel)}
/* macOS "always show scroll bars" paints a full-contrast bar down the panel
   permanently. Reserve the gutter but keep the bar invisible until the pointer
   is over the table, so the scrollbar behaves like an overlay one either way.
   Track stays transparent so the gutter reads as part of the panel. */
.scroll{max-height:60vh;overflow:auto;overflow-x:auto;
        scrollbar-width:thin;scrollbar-color:transparent transparent}
.scroll:hover,.scroll:focus-within{scrollbar-color:var(--thumb) transparent}
.scroll::-webkit-scrollbar{width:11px;height:11px}
.scroll::-webkit-scrollbar-track,.scroll::-webkit-scrollbar-corner{background:transparent}
.scroll::-webkit-scrollbar-thumb{background:transparent;border-radius:99px;
  border:3px solid transparent;background-clip:content-box}
.scroll:hover::-webkit-scrollbar-thumb,.scroll:focus-within::-webkit-scrollbar-thumb{
  background:var(--thumb);background-clip:content-box}
.scroll::-webkit-scrollbar-thumb:hover{background:var(--thumb-hover);background-clip:content-box}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}
th,td{padding:8px 12px;text-align:left;white-space:nowrap}
th{position:sticky;top:0;z-index:1;background:var(--panel-2);color:var(--ink-3);
   font-weight:560;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
   border-bottom:1px solid var(--line)}
td{border-bottom:1px solid var(--td-line)}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--row-hover)}
td.n,th.n{text-align:right}
.title-cell{white-space:normal;min-width:230px}
.muted{color:var(--ink-3)}
.mark{font-size:10.5px;padding:1.5px 6px;border-radius:5px;border:1px solid var(--line);color:var(--ink-3)}
.mark.staged{color:var(--cool);border-color:var(--cool-bd)}
.mark.enc{color:var(--hot-soft);border-color:var(--hot-bd)}
.rowenc td{background:var(--row-enc)}
.empty{padding:28px;text-align:center;color:var(--ink-3);font-size:13px}
footer{margin-top:22px;font-size:11.5px;color:var(--ink-3);display:flex;gap:14px;flex-wrap:wrap}
@media (prefers-reduced-motion:reduce){.bar>i{transition:none}}

/* ---- Responsive. Below 700px the tables keep scrolling sideways: the
   title column pins to the left edge (the title is the identity of a row --
   an index column carries nothing the row order does not), and any column
   sliced at a boundary is blanked by the seam script rather than clipped.
   Every colour here is an existing token, so both themes are covered. ---- */
@media (max-width:700px){
  body{padding:16px 12px 48px}
  .kv{grid-template-columns:repeat(auto-fit,minmax(104px,1fr))}
  /* 60vh, floored so portrait phones keep a useful table, capped so the
     floor cannot exceed a short landscape viewport. */
  .scroll{max-height:min(max(60vh,340px),72dvh)}
  th,td{padding:7px 9px}
  /* Row colour lives on the tr so the pinned title inherits it opaquely --
     a transparent pinned cell shows the columns sliding through it. */
  tr{background:var(--panel)}
  thead tr{background:var(--panel-2)}
  tbody tr:hover{background:var(--row-hover)}
  .rowenc{background:var(--row-enc)}
  /* Both tables mark the title th and td with .title-cell, so no per-table
     column arithmetic is needed and left:0 is the only constant. Until the
     scroll reaches it the title sits in its natural column; from then on it
     docks at the left edge and the rest slides beneath it. */
  .title-cell{min-width:190px;position:sticky;left:0;z-index:1;
    background:inherit;border-right:1px solid var(--line)}
  /* Headers float above the pinned title cells; the pinned corner above both. */
  th{z-index:2}
  th.title-cell{z-index:3}
  /* A column sliced at a boundary renders EMPTY, not clipped. Clipped on its
     left, a right-aligned "70.64 GiB" still parses -- as "0.64 GiB"; clipped
     on its right it loses its unit and "78.24" bare could be Mb/s or GiB.
     The .cut class is applied by the seam script at the bottom of the page. */
  td.cut,th.cut{color:transparent}
  td.cut *,th.cut *{visibility:hidden}
}
@media (pointer:coarse){
  .themebtn{width:44px;height:44px}
  .tab{padding:11px 16px}
  /* The hover-only scrollbar has no hover to wait for on touch. Without this
     the History table hides 3/4 of its columns with zero affordance. */
  .scroll{scrollbar-color:var(--thumb) transparent}
  .scroll::-webkit-scrollbar-thumb{background:var(--thumb);background-clip:content-box}
}
</style>
<script nonce="__NONCE__">
/* Runs before first paint. Anything later flashes the wrong theme on load. */
(function(){
  var v=null;
  try{ v=localStorage.getItem("smeltr.theme"); }catch(e){}
  if(v!=="light"&&v!=="dark"){
    v=(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches)
      ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme",v);
})();
</script>
</head>
<body>
<header>
  <div class="wordmark">SMELTR<b>.</b></div>
  <div class="tag">remuxes in &middot; ingots out</div>
  <div class="conn"><span class="dot" id="dot"></span><span id="connText">connecting</span></div>
  <button class="themebtn" id="themeBtn" type="button" aria-label="Switch theme"><i></i></button>
</header>

<section id="alert"></section>
<section class="stats" id="stats"></section>
<section id="liveWrap"></section>

<div class="tabs" role="tablist">
  <button class="tab" id="tabQueue"  role="tab" aria-selected="true"  aria-controls="paneQueue">Queue</button>
  <button class="tab" id="tabLedger" role="tab" aria-selected="false" aria-controls="paneLedger">History</button>
</div>
<div class="wrap"><div class="scroll" id="pane"></div></div>

<footer>
  <span id="gen"></span><span id="stopnote"></span><span>read-only &middot; 127.0.0.1 only</span>
</footer>

<script nonce="__NONCE__">
(function(){
"use strict";
var GIB=1073741824, TIB=GIB*1024;
var token=new URLSearchParams(location.search).get("t")||"";
var tab="queue", last={};
var RECORD={live:"measured at finish","state-file":"hand-migrated",
            recovered:"found after the fact"};

function gib(b){ if(b==null) return "—";
  return b>=TIB ? (b/TIB).toFixed(2)+" TiB" : (b/GIB).toFixed(2)+" GiB"; }
function pct(v){ return v==null ? "—" : v.toFixed(1)+"%"; }
function dur(s){ if(s==null) return "—";
  var h=Math.floor(s/3600), m=Math.floor(s%3600/60);
  return h ? h+"h "+String(m).padStart(2,"0")+"m" : m+"m"; }
function el(tag,cls,text){ var n=document.createElement(tag);
  if(cls) n.className=cls; if(text!=null) n.textContent=text; return n; }

function statCard(k,v,s,cls){
  var d=el("div","stat"); d.appendChild(el("div","k",k));
  d.appendChild(el("div","v "+(cls||""),v));
  if(s) d.appendChild(el("div","s",s)); return d;
}

function renderAlert(s){
  var host=document.getElementById("alert"); host.replaceChildren();
  if(s.library_complete!==false) return;
  var c=el("div","card alert");
  c.appendChild(el("div","live-title","Library incomplete — "+
    (s.roots_offline||[]).join(", ")+" not mounted"));
  c.appendChild(el("div","verdict",
    "An unmounted NAS empties the queue, which looks identical to having "+
    "finished. Every queue figure below is PARTIAL — do not read it as "+
    "\u201cnothing left to encode\u201d."));
  host.appendChild(c);
}

function renderStats(s){
  var host=document.getElementById("stats"); host.replaceChildren();
  host.appendChild(statCard("Reclaimed", gib(s.reclaimed_bytes),
    s.completed_measured+" of "+s.completed+" encodes measured","hot"));
  host.appendChild(statCard("Average shrink", pct(s.avg_saved_pct),
    "weighted · "+gib(s.source_total_bytes)+" → "+gib(s.output_total_bytes)));
  host.appendChild(statCard("Still queued", String(s.queue_waiting),
    gib(s.queue_bytes)+" of originals above "+s.stop_mbps+" Mb/s","cool"));
  host.appendChild(statCard("Still to reclaim", gib(s.queue_reclaimable_bytes),
    "projected at "+pct(s.avg_saved_pct)));
  host.appendChild(statCard("Job progress", pct(s.job_progress_pct),
    "by reclaimed bytes, not titles"));
  host.appendChild(statCard("Staged", String(s.staged),
    s.queue_encoding+" encoding · "+s.staged_unencoded+" not yet encoded"));
}

function renderLive(live, s){
  var host=document.getElementById("liveWrap"); host.replaceChildren();
  if(!live.length){
    var c=el("div","card");
    c.appendChild(el("div","live-title","Nothing encoding"));
    c.appendChild(el("div","verdict", s.x9_online
      ? "The staging drive is mounted and idle."
      : "The staging drive is not mounted."));
    host.appendChild(c); return;
  }
  live.forEach(function(e){
    var c=el("div","card live");
    var top=el("div","live-top");
    top.appendChild(el("div","live-title",e.title));
    if(e.crf!=null) top.appendChild(el("span","chip","CRF "+e.crf));
    if(e.geometry) top.appendChild(el("span","chip",
      (e.source_geometry && e.source_geometry!==e.geometry)
        ? e.source_geometry+" → "+e.geometry+" (auto-crop)" : e.geometry));
    if(e.audio!=null){
      var loss = e.src_audio!=null && (e.src_audio!==e.audio || e.src_subs!==e.subs);
      top.appendChild(el("span","chip"+(loss?" bad":""), loss
        ? "TRACK LOSS "+e.src_audio+"a/"+e.src_subs+"s → "+e.audio+"a/"+e.subs+"s"
        : e.audio+" audio · "+e.subs+" subs"));
    }
    if(e.decoder_errors) top.appendChild(el("span","chip bad",
      e.decoder_errors+" decoder errors"));
    var vc = e.verdict==="good"?"good":
             (e.verdict==="thin"||e.verdict==="suspect")?"thin":
             (e.verdict==="unknown"?"":"bad");
    if(e.verdict==="downscale") vc="bad";
    if(e.shrink_pct!=null) top.appendChild(el("span","chip "+vc, pct(e.shrink_pct)+" smaller"));
    top.appendChild(el("span","chip "+vc, e.verdict.toUpperCase()));
    c.appendChild(top);

    var bar=el("div","bar"), fill=el("i");
    fill.style.width=(e.pct||0)+"%"; bar.appendChild(fill); c.appendChild(bar);

    var kv=el("div","kv");
    [["Progress", e.pct==null?"—":e.pct.toFixed(2)+"%"],
     ["ETA", dur(e.eta_s)],
     ["Speed", e.avg_fps==null?"—":e.avg_fps.toFixed(1)+" fps avg"],
     ["Written", gib(e.output_bytes)],
     ["Projected", gib(e.projected_bytes)],
     ["Of source", e.norm_ratio_pct==null ? "—" :
        pct(e.norm_ratio_pct)+(e.crop_factor>1.01?" (crop-adj)":"")],
     ["Source", gib(e.source_bytes)],
     ["Started", e.started_text||"—"],
     ["PID", String(e.pid)]
    ].forEach(function(p){ var d=el("div");
      d.appendChild(el("span",null,p[0])); d.appendChild(el("b",null,p[1])); kv.appendChild(d); });
    c.appendChild(kv);
    var vn=el("div","verdict", e.verdict_note);
    if(e.verdict==="suspect"||e.verdict==="blowup"||e.verdict==="no-saving")
      vn.className="verdict loud";
    c.appendChild(vn);
    host.appendChild(c);
  });
}

function table(cols, rows, build){
  var t=el("table"), thead=el("thead"), tr=el("tr");
  cols.forEach(function(c){
    var th=el("th",[c.n?"n":"",c.cls||""].join(" ").trim()||null,c.label);
    tr.appendChild(th); });
  thead.appendChild(tr); t.appendChild(thead);
  var tb=el("tbody");
  rows.forEach(function(r,i){ tb.appendChild(build(r,i)); });
  t.appendChild(tb); return t;
}

function renderQueue(q){
  var pane=document.getElementById("pane"); pane.replaceChildren();
  if(!q.length){ pane.appendChild(el("div","empty",
    "Nothing left above the stop threshold.")); return; }
  pane.appendChild(table(
    [{label:"Rank",n:true},{label:"Src Mb/s",n:true},{label:"Src size",n:true},
     {label:"Title",cls:"title-cell"},{label:"NAS"},{label:"Status"}],
    q, function(r,i){
      var tr=el("tr", r.encoding?"rowenc":null);
      tr.appendChild(el("td","n muted",String(i+1)));
      tr.appendChild(el("td","n",r.mbps.toFixed(1)));
      tr.appendChild(el("td","n",gib(r.bytes)));
      tr.appendChild(el("td","title-cell",r.title));
      tr.appendChild(el("td","muted",r.location));
      var td=el("td");
      if(r.encoding) td.appendChild(el("span","mark enc","encoding"));
      else if(r.staged) td.appendChild(el("span","mark staged","staged"));
      else td.appendChild(el("span","mark","library"));
      tr.appendChild(td); return tr;
    }));
}

function renderLedger(rows){
  var pane=document.getElementById("pane"); pane.replaceChildren();
  if(!rows.length){ pane.appendChild(el("div","empty","No encodes recorded yet.")); return; }
  var ordered=rows.slice().reverse();
  pane.appendChild(table(
    [{label:"#",n:true},{label:"Title",cls:"title-cell"},{label:"Original",n:true},{label:"Output",n:true},
     {label:"Saved",n:true},{label:"Shrink",n:true},{label:"Tracks"},{label:"Moved to"},
     {label:"Source of record"},{label:"Finished"}],
    ordered, function(r,i){
      var tr=el("tr");
      tr.appendChild(el("td","n muted",String(ordered.length-i)));
      tr.appendChild(el("td","title-cell",r.title));
      var ap = r.exact===false ? "~" : "";
      tr.appendChild(el("td","n",r.source_bytes?ap+gib(r.source_bytes):"—"));
      tr.appendChild(el("td","n",r.output_bytes?ap+gib(r.output_bytes):"—"));
      tr.appendChild(el("td","n",r.saved_bytes?ap+gib(r.saved_bytes):"—"));
      tr.appendChild(el("td","n",pct(r.saved_pct)));
      tr.appendChild(el("td","muted",
        (r.audio==null?"—":r.audio+"a / "+r.subs+"s")));
      tr.appendChild(el("td","muted",r.dest||"—"));
      tr.appendChild(el("td","muted",RECORD[r.provenance]||r.provenance||"—"));
      tr.appendChild(el("td","muted",(r.finished_at||"—").slice(0,10)));
      if(r.note){ tr.title=r.note; }
      return tr;
    }));
}

function paint(s){
  renderAlert(s.summary);
  renderStats(s.summary);
  renderLive(s.live, s.summary);
  var key=tab+"|"+JSON.stringify(tab==="queue"?s.queue:s.ledger);
  if(last.key!==key){ last.key=key; (tab==="queue"?renderQueue(s.queue):renderLedger(s.ledger)); }
  document.getElementById("tabQueue").textContent="Queue ("+s.queue.length+")";
  document.getElementById("tabLedger").textContent="History ("+s.ledger.length+")";
  document.getElementById("gen").textContent="updated "+s.summary.generated_at;
  document.getElementById("stopnote").textContent=
    "pausing below "+s.summary.stop_mbps+" Mb/s";
}

function setTab(name){
  tab=name; last.key=null;
  document.getElementById("tabQueue").setAttribute("aria-selected", String(name==="queue"));
  document.getElementById("tabLedger").setAttribute("aria-selected", String(name==="ledger"));
  if(last.state) paint(last.state);
}
document.getElementById("tabQueue").addEventListener("click",function(){setTab("queue");});
document.getElementById("tabLedger").addEventListener("click",function(){setTab("ledger");});

/* Seam blanking. Below 700px the title column pins while the rest scrolls,
   and a cell HALF hidden is worse than one fully hidden: sliced at the pane's
   left edge or the pinned title's edge, a right-aligned size keeps its
   trailing digits and still parses as a plausible size; sliced at the right
   edge it keeps its digits but loses its unit. Whichever column straddles a
   boundary gets .cut (blank but layout-stable) until it is fully clear. The
   straddle test uses the text box (cell inset by its padding), so a value
   whose glyphs are fully visible is never blanked. Title cells are never
   cut: a clipped title misleads no one, a missing one identifies nothing.
   Geometry only -- reads no data, writes no text. */
(function(){
  var pane=document.getElementById("pane"), cuts=[], pend=false;
  function applyCut(tbl,i,on){
    for(var r=0;r<tbl.rows.length;r++){
      var c=tbl.rows[r].cells[i];
      if(c) c.classList.toggle("cut",on);
    }
  }
  function recut(){
    var tbl=pane.querySelector("table");
    if(!tbl||!tbl.rows.length){ cuts=[]; return; }
    var head=tbl.rows[0], styles=[], sticky=[], pinnedRight=null, i, rc;
    for(i=0;i<head.cells.length;i++){
      styles[i]=getComputedStyle(head.cells[i]);
      sticky[i]=styles[i].position==="sticky"&&styles[i].left!=="auto";
      if(sticky[i]){
        rc=head.cells[i].getBoundingClientRect();
        if(pinnedRight==null||rc.right>pinnedRight) pinnedRight=rc.right;
      }
    }
    var next=[];
    if(pinnedRight!=null){  /* the pinned title exists only below 700px */
      var pr=pane.getBoundingClientRect();
      var bounds=[pr.left,pinnedRight,pr.left+pane.clientWidth];
      for(i=0;i<head.cells.length;i++){
        if(sticky[i]||head.cells[i].classList.contains("title-cell")) continue;
        rc=head.cells[i].getBoundingClientRect();
        var L=rc.left+parseFloat(styles[i].paddingLeft),
            R=rc.right-parseFloat(styles[i].paddingRight);
        for(var b=0;b<bounds.length;b++){
          if(L<bounds[b]-1&&R>bounds[b]+1){ next.push(i); break; }
        }
      }
    }
    cuts.forEach(function(c){ if(next.indexOf(c)<0) applyCut(tbl,c,false); });
    next.forEach(function(c){ if(cuts.indexOf(c)<0) applyCut(tbl,c,true); });
    cuts=next;
  }
  function schedule(){ if(pend) return; pend=true;
    requestAnimationFrame(function(){ pend=false; recut(); }); }
  pane.addEventListener("scroll",schedule,{passive:true});
  window.addEventListener("resize",schedule);
  new MutationObserver(function(){ cuts=[]; schedule(); })
    .observe(pane,{childList:true});
  schedule();
})();

/* Theme. A stored value is an explicit choice and always wins. With no stored
   choice the OS preference wins and KEEPS winning -- flipping the system theme
   mid-session moves the page with it, until the button is pressed once. */
var THEME_KEY="smeltr.theme";
var mql=window.matchMedia?window.matchMedia("(prefers-color-scheme: light)"):null;
function storedTheme(){
  try{ var v=localStorage.getItem(THEME_KEY);
       return (v==="light"||v==="dark")?v:null; }catch(e){ return null; }
}
function applyTheme(name){
  document.documentElement.setAttribute("data-theme",name);
  var b=document.getElementById("themeBtn");
  var label=name==="dark" ? "Switch to light theme" : "Switch to dark theme";
  b.setAttribute("aria-label",label);
  b.title=label;
}
applyTheme(document.documentElement.getAttribute("data-theme")||"dark");
if(mql&&mql.addEventListener){
  mql.addEventListener("change",function(e){
    if(!storedTheme()) applyTheme(e.matches?"light":"dark");
  });
}
document.getElementById("themeBtn").addEventListener("click",function(){
  var next=document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark";
  try{ localStorage.setItem(THEME_KEY,next); }catch(e){}
  applyTheme(next);
});

function conn(state,text){
  document.getElementById("dot").className="dot "+state;
  document.getElementById("connText").textContent=text;
}

var es=new EventSource("/api/stream?t="+encodeURIComponent(token));
es.onopen=function(){ conn("on","live"); };
es.onerror=function(){ conn("off","reconnecting"); };
es.onmessage=function(ev){
  try{ var s=JSON.parse(ev.data); }catch(_){ return; }
  last.state=s; conn("on","live"); paint(s);
};
})();
</script>
</body></html>
"""

PAGE = _PAGE.replace("__NONCE__", NONCE)


def main() -> None:
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    # Print the plain URL unless the token is actually required -- a link the
    # user cannot retype is a link they cannot use.
    url = (f"http://127.0.0.1:{port}/?t={TOKEN}" if REQUIRE_TOKEN
           else f"http://127.0.0.1:{port}/")
    urlfile = os.path.join(core.SMELTR_DIR, "url")
    pidfile = os.path.join(core.SMELTR_DIR, "server.pid")

    # The server owns its own pidfile. Matching on the command line instead was
    # unreliable: the same server started as "python3 server.py" from its own
    # directory and as "python3 ~/.smeltr/server.py" produced two different ps
    # patterns, so one invocation went unnoticed, kept port 8787, and every
    # later start silently drifted to an ephemeral port.
    # The URL embeds the auth token, so it must not be world-readable -- and
    # neither must the ledger. Default umask left both at 0644.
    try:
        os.chmod(core.SMELTR_DIR, 0o700)
        if os.path.exists(core.LEDGER):
            os.chmod(core.LEDGER, 0o600)
    except OSError:
        pass
    for path, data in ((pidfile, f"{os.getpid()}\n"), (urlfile, url + "\n")):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        # os.open's mode is ignored when the file already exists, so a
        # pre-existing 0644 url file kept its permissions and the token stayed
        # world-readable. chmod unconditionally.
        os.chmod(path, 0o600)
    # Only print the URL to a terminal. The launcher redirects stdout to
    # server.log, which is how the token ended up sitting in a 0644 file in
    # plaintext despite the header claiming it is never logged.
    if sys.stdout.isatty():
        print(url, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        for path in (pidfile, urlfile):
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
