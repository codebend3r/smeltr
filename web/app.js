(function () {
  "use strict";
  var GIB = 1073741824,
    TIB = GIB * 1024;
  var token = new URLSearchParams(location.search).get("t") || "";
  var tab = "queue",
    last = {};
  var RECORD = {
    live: "measured at finish",
    "state-file": "hand-migrated",
    recovered: "found after the fact",
  };

  function gib(b) {
    if (b == null) return "—";
    return b >= TIB ? (b / TIB).toFixed(2) + " TiB" : (b / GIB).toFixed(2) + " GiB";
  }
  function pct(v) {
    return v == null ? "—" : v.toFixed(1) + "%";
  }
  /* What the quality number MEANS, which depends on who made it. x265 quality
   is CRF (lower = better); VideoToolbox is CQ on Apple's reversed 0-100 scale.
   The two are not comparable, so a bare number on screen is ambiguous the
   moment a second encoder exists. A missing encoder is x265: every row before
   2026-08-25 was. */
  function qLabel(enc) {
    return enc && enc.indexOf("vt") === 0 ? "VT CQ" : "CRF";
  }
  /* Live encode progress at HandBrake's own precision -- two decimals. The log
   never carries a third digit, so printing one was always a trailing zero. */
  function pctLive(v) {
    return v == null ? "—" : v.toFixed(2) + "%";
  }
  function dur(s) {
    if (s == null) return "—";
    var h = Math.floor(s / 3600),
      m = Math.floor((s % 3600) / 60);
    return h ? h + "h " + String(m).padStart(2, "0") + "m" : m + "m";
  }
  /* EVERY clock the page prints is 12-hour (operator's pick 2026-09-03):
   the History stamp, the Events timeline, the live card's start line, the
   monitor's axis, and the footer.

   `clock12` REWRITES the HH:MM[:SS] inside a string; it never re-parses the
   string as a date. Most of these stamps are local wall-clock carrying no
   offset -- `finished_at`, `generated_at`, an event `ts`, HandBrake's own
   header line -- and handing one to Date() lets a browser running in another
   zone re-interpret it and shift every row, the same +4h that once moved the
   monitor's axis labels. Anything that is not a clock is passed through. */
  var CLOCK_RE = /\b([01]?\d|2[0-3]):([0-5]\d)(:[0-5]\d)?\b/g;
  function clock12(s) {
    if (s == null) return s;
    return String(s).replace(CLOCK_RE, function (_, h, m, sec) {
      return (h % 12 || 12) + ":" + m + (sec || "") + " " + (h < 12 ? "am" : "pm");
    });
  }
  /* HandBrake's header reads "Wed Sep  2 21:14:37 2026". The year moves ahead
   of the clock so the am/pm suffix is not stranded in the middle of the
   line; a line that does not match is still clock-converted in place. */
  function startedText(s) {
    var m = /^(.*?)\s+(\d{1,2}:\d{2}:\d{2})\s+(\d{4})$/.exec(s);
    return m ? m[1].replace(/\s+/g, " ") + " " + m[3] + " " + clock12(m[2]) : clock12(s);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* A line glyph from the symbol sheet in index.html. Built with the SVG
   namespace, never innerHTML; the id is one of ours, never server data. */
  function icon(id) {
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "ic");
    svg.setAttribute("aria-hidden", "true");
    var u = document.createElementNS(NS, "use");
    u.setAttribute("href", "#" + id);
    svg.appendChild(u);
    return svg;
  }

  function statCard(k, v, s, cls) {
    var d = el("div", "stat");
    d.appendChild(el("div", "k", k));
    d.appendChild(el("div", "v " + (cls || ""), v));
    if (s) d.appendChild(el("div", "s", s));
    return d;
  }

  /* The only two writes the page can make. Both name titles by exact match
   against the queue the server just sent; the response is a fresh state
   snapshot, painted immediately so the click lands without waiting for SSE. */
  var NAS_FIXED = { vhagar: "nas-cool", vermithor: "nas-good" };
  var NAS_CLASSES = ["nas-cool", "nas-good", "nas-warn", "nas-hot"];
  function nasClass(name) {
    var cls = NAS_FIXED[name.toLowerCase()];
    if (!cls) {
      var h = 0;
      for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
      cls = NAS_CLASSES[h % NAS_CLASSES.length];
    }
    return cls;
  }
  function nasMark(name) {
    if (!name || name === "?") return el("span", "muted", name || "—");
    return el("span", "mark " + nasClass(name), name);
  }

  /* Parent folder of the original, relative to the NAS volume; the title
   folder itself is dropped (it repeats the Title cell). Ambiguous or
   unknown paths come through as null and stay an honest em dash.
   Rendered as the same root+bucket pill pair as the History tab's
   "Moved to" cell, coloured by the row's NAS so the two columns read as
   one destination; a row with no known NAS keeps the plain mono text. */
  function srcDirTd(srcDir, title, nas) {
    var td = el("td", "src-dir", "—");
    if (srcDir) {
      var rel = srcDir.replace(/^\/Volumes\/[^/]+\//, "").replace(/^Media\//, "");
      var tail = "/" + title;
      if (rel.slice(-tail.length) === tail) rel = rel.slice(0, -tail.length);
      td.title = srcDir;
      if (nas && nas !== "?") {
        td.textContent = "";
        var cut = rel.lastIndexOf("/");
        var root = cut > 0 ? rel.slice(0, cut) : rel;
        var bucket = cut > 0 ? rel.slice(cut + 1) : "";
        td.appendChild(el("span", "mark " + nasClass(nas), root));
        if (bucket) td.appendChild(el("span", "mark bucket " + nasClass(nas), bucket));
      } else td.textContent = rel;
    }
    return td;
  }

  var noticeTimer = null;
  function notice(msg) {
    var n = document.getElementById("uiNotice");
    n.textContent = msg;
    n.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () {
      n.hidden = true;
    }, 5000);
  }

  var posting = false;
  function api(path, payload) {
    /* Dropping a second click with NO feedback is the one path where nothing
     at all happens; every other failure produces a notice, so this must. */
    if (posting) {
      notice("another action is still in flight — try again in a moment");
      return Promise.resolve();
    }
    posting = true;
    return fetch(path + (token ? "?t=" + encodeURIComponent(token) : ""), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Smeltr": "1" },
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        if (!r.ok)
          return r.text().then(function (t) {
            throw new Error(t || "request failed");
          });
        return r.json();
      })
      .then(function (s) {
        last.state = s;
        last.key = null;
        paint(s);
      })
      .catch(function (err) {
        notice(((err && err.message) || "action failed").slice(0, 120));
      })
      .finally(function () {
        posting = false;
      });
  }

  /* Collapsible cards: chevron injected top-right, state per card key in
   localStorage so SSE rebuilds and reloads keep the fold. localStorage can
   throw (private windows); a failed read is just "nothing collapsed". */
  function clpsState() {
    try {
      return JSON.parse(localStorage.getItem("smeltr.collapse") || "{}");
    } catch (e) {
      return {};
    }
  }
  function makeCollapsible(card, key, loud) {
    if (!card || card.querySelector(":scope>.clps, :scope>.monhead>.clps, :scope>.clpshead>.clps"))
      return;
    var st = clpsState(),
      head = card.querySelector(":scope>.monhead, :scope>.clpshead");
    /* A loud card NEVER honours a stored fold: a fold saved on the calm card
     that used to occupy this slot must not pre-hide a bad note, a loud
     verdict, an offline-NAS warning, or the pause switch. The chevron stays
     so it can still be folded BY HAND, this session, eyes open. */
    var open = loud === true || !st[key];
    if (!open) card.classList.add("collapsed");
    var b = el("button", "clps" + (head ? " inhead" : ""), open ? "▾" : "▸");
    b.type = "button";
    b.title = "Collapse or expand this card";
    b.setAttribute("aria-label", "Collapse or expand this card");
    b.setAttribute("aria-expanded", open ? "true" : "false");
    b.addEventListener("click", function () {
      var c = card.classList.toggle("collapsed");
      b.textContent = c ? "▸" : "▾";
      b.setAttribute("aria-expanded", c ? "false" : "true");
      var s2 = clpsState();
      if (c) s2[key] = 1;
      else delete s2[key];
      try {
        localStorage.setItem("smeltr.collapse", JSON.stringify(s2));
      } catch (e) {}
    });
    (head || card).appendChild(b);
    return b;
  }

  function renderAlert(s, note) {
    var host = document.getElementById("alert");
    host.replaceChildren();
    /* Outcome of the last start/abort. The slow halves (track parity, the kill
     grace) finish long after their POST returned, so this banner is how they
     report. kind=bad stays until the next action; ok/warn are informational. */
    if (note && note.msg) {
      var nc = el("div", "card alert" + (note.kind === "ok" ? " okline" : ""));
      nc.appendChild(el("div", "verdict" + (note.kind === "bad" ? " loud" : ""), note.msg));
      makeCollapsible(nc, "alert-note", note.kind === "bad");
      host.appendChild(nc);
    }
    if (s.overrides_corrupt) {
      var oc = el("div", "card alert");
      oc.appendChild(el("div", "live-title", "queue_overrides.json is unreadable"));
      oc.appendChild(
        el(
          "div",
          "verdict",
          "Skips and hand-priorities are NOT being applied — the pipeline is " +
            "running in stock bitrate order. Fix or delete the file; any skip or " +
            "reorder here rewrites it cleanly.",
        ),
      );
      makeCollapsible(oc, "alert-ov", true);
      host.appendChild(oc);
    }
    if (s.encoder_overrides_corrupt) {
      var ec = el("div", "card alert");
      ec.appendChild(el("div", "live-title", "encoder_overrides.json is unreadable"));
      ec.appendChild(
        el(
          "div",
          "verdict",
          "Per-title encoder choices are NOT being applied — every encode will " +
            "start on the x265 default. Fix or delete the file; changing any " +
            "title's encoder here rewrites it cleanly.",
        ),
      );
      host.appendChild(ec);
    }
    /* The staging-drive floor (2026-09-08). The driver is WAITING, not
     stopped, and the fix is on the human: name the free figure and the line.
     Both numbers come from the payload -- the server owns the floor -- so the
     page can never disagree with the driver's own reason. */
    if (s.low_space) {
      var lc = el("div", "card alert");
      var freeTxt = s.x9_free_bytes != null ? gib(s.x9_free_bytes) : "—";
      var floorTxt = s.low_space_floor_bytes != null ? gib(s.low_space_floor_bytes) : "—";
      lc.appendChild(
        el("div", "live-title", "Low space on the staging drive — " + freeTxt + " free"),
      );
      lc.appendChild(
        el(
          "div",
          "verdict loud",
          "Nothing new starts until " +
            floorTxt +
            " is free. The running encode (if any) still finishes and moves to complete/. " +
            "Free up space on the X9 and the driver resumes by itself within a minute.",
        ),
      );
      makeCollapsible(lc, "alert-space", true);
      host.appendChild(lc);
    }
    if (s.library_complete !== false) return;
    var c = el("div", "card alert");
    c.appendChild(
      el(
        "div",
        "live-title",
        "Library incomplete — " + (s.roots_offline || []).join(", ") + " not mounted",
      ),
    );
    c.appendChild(
      el(
        "div",
        "verdict",
        "An unmounted NAS empties the queue, which looks identical to having " +
          "finished. Every queue figure below is PARTIAL — do not read it as " +
          "\u201cnothing left to encode\u201d.",
      ),
    );
    makeCollapsible(c, "alert-lib", true);
    host.appendChild(c);
  }

  var prevStats = null;
  function renderStats(s) {
    var host = document.getElementById("statsGrid");
    host.replaceChildren();
    var seen = {};
    function add(k, v, sub, cls) {
      var d = statCard(k, v, sub, cls);
      if (prevStats && prevStats[k] !== undefined && prevStats[k] !== v) d.classList.add("flash");
      seen[k] = v;
      host.appendChild(d);
    }
    add(
      "Reclaimed",
      gib(s.reclaimed_bytes),
      s.completed_measured + " of " + s.completed + " encodes measured",
      "hot",
    );
    add(
      "Average shrink",
      pct(s.avg_saved_pct),
      "weighted · " + gib(s.source_total_bytes) + " → " + gib(s.output_total_bytes),
    );
    var complete = s.library_complete !== false;
    /* Set-aside titles are out of every queue total, and the cards have to
     say so or the numbers read as the whole job. Skipped and errored are
     counted SEPARATELY on purpose: one is a preference you can undo from
     the Errors tab, the other is work the pipeline refused and cannot
     resume until a marker file is deleted. Rolling them into one "excluded"
     figure would hide the half that needs you. */
    function asideNote(verb) {
      var bits = [];
      if (s.queue_errored) bits.push(s.queue_errored + " errored");
      if (s.queue_skipped) bits.push(s.queue_skipped + " skipped");
      return bits.length ? " · " + verb + " " + bits.join(" + ") : "";
    }
    var skipnote = asideNote("excludes");
    if (complete) {
      add(
        "Still queued",
        String(s.queue_waiting),
        gib(s.queue_bytes) +
          " of originals above " +
          s.stop_mbps +
          " Mb/s" +
          (s.queue_encoding ? " · incl. " + s.queue_encoding + " encoding" : "") +
          asideNote("excludes"),
        "cool",
      );
      add(
        "Still to reclaim",
        s.queue_reclaimable_bytes == null ? "—" : "~" + gib(s.queue_reclaimable_bytes),
        "projected at " + pct(s.avg_saved_pct) + skipnote,
      );
      add(
        "Job progress",
        s.job_progress_pct == null ? "—" : "~" + pct(s.job_progress_pct),
        "by reclaimed bytes, not titles" + asideNote("goal still counts"),
      );
    } else {
      /* An unmounted NAS empties the queue; a hard 0 in these slots is the
       most dangerous cell on the page. Refuse to print a number, exactly
       as the terminal report does. */
      add(
        "Still queued",
        "unknown",
        "library not fully mounted · " + s.queue_waiting + " readable",
      );
      add("Still to reclaim", "unknown", "library not fully mounted — partial");
      add("Job progress", "unknown", "cannot be computed while a root is offline");
    }
    add(
      "Staged",
      String(s.staged),
      gib(s.staged_bytes) +
        " of originals · " +
        s.queue_encoding +
        " encoding · " +
        s.staged_unencoded +
        " not yet encoded",
    );
    prevStats = seen;
    /* The collapsed digest. Same values as the cards above, same refusal
     to print a queue number while a root is offline -- a folded "148
     queued" read off a partial library is the most dangerous cell on
     the page whether or not the grid is showing. */
    var sum = document.getElementById("statsSum");
    if (sum)
      sum.textContent =
        gib(s.reclaimed_bytes) +
        " reclaimed · " +
        pct(s.avg_saved_pct) +
        " average shrink · " +
        (complete
          ? s.queue_waiting +
            " queued · " +
            (s.job_progress_pct == null ? "progress —" : "~" + pct(s.job_progress_pct) + " done")
          : "queue unknown — library not fully mounted");
  }

  function liveChips(e) {
    var top = el("div", "live-top");
    /* e.title can arrive as a full staging path; show only the movie's name.
     The folder name is the identity everywhere else, so prefer it, falling
     back to the last path segment. Full value stays in the tooltip. */
    var name = String(e.folder || e.title)
      .split("/")
      .pop();
    var t = el("div", "live-title", name);
    if (name !== e.title) t.title = e.title;
    top.appendChild(t);
    /* Same fact as the queue's "manual" mark, carried for the whole run: this
     encode ends in the error state with its output left in place. */
    if (e.manual) {
      var mchip = el("span", "chip warn", "manual — left in place");
      mchip.title =
        "Added by hand, not from the library index. Nothing will be synced or " +
        "deleted; move the finished file yourself.";
      top.appendChild(mchip);
    }
    if (e.crf != null) top.appendChild(el("span", "chip", qLabel(e.encoder) + " " + e.crf));
    if (e.geometry)
      top.appendChild(
        el(
          "span",
          "chip",
          e.source_geometry && e.source_geometry !== e.geometry
            ? e.source_geometry + " → " + e.geometry + " (auto-crop)"
            : e.geometry,
        ),
      );
    if (e.audio != null) {
      var loss = e.src_audio != null && (e.src_audio !== e.audio || e.src_subs !== e.subs);
      top.appendChild(
        el(
          "span",
          "chip" + (loss ? " bad" : ""),
          loss
            ? "TRACK LOSS " +
                e.src_audio +
                "a/" +
                e.src_subs +
                "s → " +
                e.audio +
                "a/" +
                e.subs +
                "s"
            : e.audio + " audio · " + e.subs + " subs",
        ),
      );
    }
    top.appendChild(
      e.decoder_errors
        ? el("span", "chip bad", e.decoder_errors + " decoder errors")
        : el(
            "span",
            "chip",
            e.decoder_errors === 0 ? "0 decoder errors" : "decoder errors not yet reported",
          ),
    );
    var vc =
      e.verdict === "good"
        ? "good"
        : e.verdict === "thin" || e.verdict === "suspect"
          ? "thin"
          : e.verdict === "unknown"
            ? ""
            : "bad";
    if (e.verdict === "downscale") vc = "bad";
    if (e.shrink_pct != null)
      top.appendChild(el("span", "chip " + vc, pct(e.shrink_pct) + " smaller"));
    top.appendChild(el("span", "chip " + vc, e.verdict.toUpperCase()));
    return top;
  }

  /* [label, value, glyph, colour]. The glyph and colour are option 6 of six
   previews (operator 2026-09-11): each icon takes a page colour -- the clock
   cool, the gauge hot, the play green, the two byte/number fields plain. */
  function liveFields(e) {
    return [
      ["ETA", dur(e.eta_s), "i-clock", "c-cool"],
      ["Speed", e.avg_fps == null ? "—" : e.avg_fps.toFixed(1) + " fps avg", "i-gauge", "c-hot"],
      ["Written", gib(e.output_bytes), "i-disk", "c-ink"],
      ["Started", e.started_text ? startedText(e.started_text) : "—", "i-play", "c-good"],
      ["PID", e.pid == null ? "—" : String(e.pid), "i-hash", "c-ink"],
    ];
  }
  /* One cell of the field row: glyph + label over the value. */
  function kvCell(label, glyph, colour, value) {
    var d = el("div", colour);
    var sp = el("span");
    sp.appendChild(icon(glyph));
    sp.appendChild(document.createTextNode(label));
    d.appendChild(sp);
    var b = el("b", null, value);
    d.appendChild(b);
    return { node: d, b: b };
  }
  /* The sixth field: free space on the staging drive, with a meter under the
   figure (option 4's cell, operator 2026-09-11). Green fill is free out of
   the drive's capacity, the amber tick is the driver's floor; under the
   floor the glyph and the figure go amber, the same fact the low-space
   alert states in words. The figure and the fill update in place every
   frame -- this is a byte count, so it never rides the card's rebuild key.
   No capacity means no meter: a fill against a made-up total is a lie. */
  function diskField() {
    var c = kvCell("Free on disk", "i-drive", "c-good", "—");
    c.node.classList.add("disk");
    var m = el("div", "meter");
    var fill = el("i");
    var tick = el("u");
    m.appendChild(fill);
    m.appendChild(tick);
    m.hidden = true;
    c.node.appendChild(m);
    return { node: c.node, b: c.b, meter: m, fill: fill, tick: tick };
  }
  function updateDisk(d, s, total) {
    var free = s.x9_free_bytes,
      floor = s.low_space_floor_bytes;
    d.b.textContent = free == null ? "—" : gib(free);
    var under = free != null && floor != null && free < floor;
    d.node.classList.toggle("c-warn", under);
    d.node.classList.toggle("c-good", !under);
    if (free == null || total == null || total <= 0) {
      d.meter.hidden = true;
      return;
    }
    d.meter.hidden = false;
    d.fill.style.width = Math.max(0, Math.min(100, (free / total) * 100)) + "%";
    d.fill.className = under ? "under" : "";
    if (floor != null) {
      d.tick.hidden = false;
      d.tick.style.left = Math.max(0, Math.min(100, (floor / total) * 100)) + "%";
      d.tick.title = "floor " + gib(floor) + " — nothing new starts under it";
    } else d.tick.hidden = true;
    d.meter.title = gib(free) + " free of " + gib(total);
  }

  /* The strip's colour and headline come from `e.verdict` -- the SAME verdict
   `core._verdict()` computed and the pipeline acts on. An earlier version
   classified the ratio again here, in the browser, against its own copy of the
   thresholds. It disagreed with the server in three ranges, and two of those
   were dangerous:
     - a `downscale` (output frame narrower than source, the ONE state where the
       original must survive) scored 45% and rendered GREEN, "IN THE TARGET
       BAND", "frees 39 GiB" -- while the real warning sat below in grey prose.
     - Flight (2012) at 9.4% rendered RED "IMPLAUSIBLY SMALL". That encode is in
       the ledger with a recorded SSIM of 0.9931/0.9945 against the cropped
       original. The abort button is a hover away on that row.
   One threshold table, one verdict, one colour. Do not reintroduce a second.

   The target band is the user's TARGET, so it stays -- but as an uncoloured
   position on the scale and a plain sentence, never as a severity. */
  var PROJ_CLASS = {
    good: "on",
    thin: "warn",
    suspect: "warn",
    "no-saving": "bad",
    blowup: "bad",
    downscale: "bad",
    unknown: "",
  };
  var PROJ_LEAD = {
    good: "SOLID REDUCTION",
    thin: "THIN SAVING — worth a human call",
    /* Deliberately EMPTY (operator's call, 2026-09-06): the "UNUSUAL FOR THIS
       JOB -- verify the picture before deleting the original" lead was noise
       on a page where nothing is deleted. The amber class still marks the
       ratio; the detail line below still states the numbers. */
    suspect: "",
    "no-saving": "BARELY SMALLER THAN THE SOURCE",
    blowup: "LARGER THAN THE SOURCE",
    downscale: "RESOLUTION LOST — DO NOT DELETE THE ORIGINAL",
    unknown: "",
  };

  /* Where the ratio sits against the user's target. Plain words, no severity:
   4 of the first 12 completed encodes landed under 30% and every one was a
   good encode, so "below" must never read as "broken". */
  /* The target band, % of source -- the same pair core.BAND_LO/BAND_HI
   carries in summary() and .watch-encode.sh ladders against (10-70 since
   2026-09-06, was 30-80). tests/test_repo_invariants.py pins these two to
   core's, because a band typed in three places drifts. */
  var BAND_LO = 10;
  var BAND_HI = 70;
  var BAND_TXT = BAND_LO + "–" + BAND_HI + "%";
  function bandText(r) {
    if (r == null) return "";
    if (r > BAND_HI) return "above the " + BAND_TXT + " target band";
    if (r >= BAND_LO) return "in the " + BAND_TXT + " target band";
    return "below the " + BAND_TXT + " target band — normal for a clean digital source";
  }

  /* Estimates are marked. The stat cards and the History table already prefix
   "~" on anything derived; this is a linear extrapolation off a part-finished
   encode, so it gets the tilde and whole GiB rather than two decimals. */
  function gibApprox(b) {
    return b == null ? "—" : "~" + Math.round(b / GIB) + " GiB";
  }

  /* The strip collapses to its head line (size, kept-%, verdict word) on
   request, and the choice sticks across visits. A LOUD verdict overrides the
   collapse: a strip hiding "downscale" behind a chevron would be the exact
   quiet-warning bug the projection rules exist to prevent. */
  var projClosed = false;
  try {
    projClosed = localStorage.getItem("smeltr.proj.closed") === "1";
  } catch (e) {}
  /* Whether the fold was chosen BY HAND this session, and whether the verdict
   was already loud last frame. Both exist to keep the loud override from
   eating the click: overriding a STORED fold is the point, overriding the
   chevron the person is pressing right now just renders as a dead button. */
  var projTouched = false;
  var projLoudSeen = false;
  /* The ONE list of warning verdicts. Both consumers -- the collapse override
   here and the verdict line's loud styling in renderLive -- read it: an
   inline copy of this set once omitted "downscale" and the only true warning
   on the card rendered unstyled. */
  var PROJ_LOUD = { suspect: 1, blowup: 1, "no-saving": 1, downscale: 1 };

  function projApply(p) {
    var loud = !!PROJ_LOUD[p.lastV];
    /* The override applies to a fold restored from localStorage, NOT to one
     made this session with eyes open -- the same rule makeCollapsible()
     already follows ("the chevron stays so it can still be folded BY HAND").
     Without this the strip was permanently stuck open on exactly the
     verdicts a person most wants to fold away after reading them, and the
     chevron looked broken. A verdict that has just TURNED loud still forces
     it open and clears the hand-fold: that transition is news. */
    if (loud && !projLoudSeen) projTouched = false;
    projLoudSeen = loud;
    var open = projTouched ? !projClosed : !projClosed || loud;
    p.root.classList.toggle("closed", !open);
    p.disc.textContent = open ? "▾" : "▸";
    p.disc.setAttribute("aria-expanded", String(open));
  }

  function projBlock() {
    var refs = {},
      n = el("div", "proj");
    refs.root = n;
    refs.lastV = "unknown";
    var head = el("div", "proj-head");
    var lhs = el("div", "proj-size");
    refs.size = el("b", null, "—");
    lhs.appendChild(refs.size);
    lhs.appendChild(el("span", null, "projected final size"));
    var rhs = el("div", "proj-ratio");
    refs.ratioWrap = rhs;
    refs.ratio = el("b", null, "—");
    rhs.appendChild(refs.ratio);
    refs.srcCap = el("span", null, "of source");
    rhs.appendChild(refs.srcCap);
    refs.disc = el("button", "proj-disc", "▾");
    refs.disc.type = "button";
    refs.disc.title = "Collapse or expand the size projection";
    refs.disc.setAttribute("aria-label", "Collapse or expand the size projection");
    head.appendChild(lhs);
    head.appendChild(rhs);
    head.appendChild(refs.disc);
    /* One listener on the head serves mouse and keyboard both: activating the
     chevron button dispatches a click that bubbles here. */
    head.addEventListener("click", function () {
      projClosed = !projClosed;
      projTouched = true;
      try {
        localStorage.setItem("smeltr.proj.closed", projClosed ? "1" : "0");
      } catch (e) {}
      projApply(refs);
    });
    n.appendChild(head);

    refs.scale = el("div", "proj-scale");
    refs.scale.setAttribute("role", "img");
    refs.scale.appendChild(el("i", "proj-zone"));
    refs.mark = el("i", "proj-mark");
    refs.mark.hidden = true;
    refs.scale.appendChild(refs.mark);
    n.appendChild(refs.scale);

    var lg = el("div", "proj-legend");
    lg.appendChild(el("span", null, "0% = nothing kept"));
    lg.appendChild(el("span", null, "target " + BAND_TXT));
    lg.appendChild(el("span", null, "100% = source"));
    n.appendChild(lg);
    refs.lead = el("div", "proj-lead", "");
    n.appendChild(refs.lead);
    refs.detail = el("div", "proj-detail", "");
    n.appendChild(refs.detail);
    projApply(refs);
    return { node: n, refs: refs };
  }

  function updateProj(p, e) {
    var r = e.ratio_pct,
      v = e.verdict || "unknown";
    var cls = PROJ_CLASS[v] == null ? "" : PROJ_CLASS[v];
    var lost = v === "downscale";
    p.size.textContent = gib(e.projected_bytes);
    p.ratio.textContent = pct(r);
    /* className= wipes "closed", so reapply the collapse after it -- with the
     verdict this frame, which may force the strip open. */
    p.root.className = lost ? "proj lost" : "proj";
    p.lastV = v;
    projApply(p);
    p.ratioWrap.className = "proj-ratio " + (lost ? "lost" : cls);
    p.srcCap.textContent =
      e.source_bytes == null
        ? "source size unknown"
        : "kept, of " + gib(e.source_bytes) + " source";
    p.lead.className = "proj-lead " + cls;
    p.lead.textContent = PROJ_LEAD[v] || "";

    if (r == null) {
      p.mark.hidden = true;
      /* Never fill the gap with a guess. Below 5% the projection is dominated by
       studio logos and black frames, which encode to almost nothing. Say which
       of the two gaps this is -- a size with no ratio is not "no projection". */
      p.detail.textContent =
        e.projected_bytes != null
          ? "Source size unknown, so the percentage cannot be computed."
          : e.pct != null && e.pct < 5
            ? "Estimate opens at 5% — logos and black frames flatter it before that."
            : "No projection yet.";
      p.scale.setAttribute(
        "aria-label",
        e.projected_bytes != null
          ? "Projected size known, but the ratio to the source cannot be computed"
          : "Projected size not available yet",
      );
      return;
    }
    p.mark.hidden = false;
    p.mark.style.left = Math.max(0, Math.min(100, r)) + "%";
    p.mark.className = "proj-mark " + (lost ? "bad" : cls);

    var bits = [];
    /* A downscale changed the pixel count, so output/source bytes are not
     comparable -- offering a band position for it would dress up a number
     that means nothing. */
    if (lost) {
      bits.push("frame is narrower than the source; the size ratio is not comparable");
    } else {
      bits.push(bandText(r));
      if (e.shrink_pct != null) bits.push(pct(e.shrink_pct) + " smaller");
      if (e.crop_factor > 1.01 && e.norm_ratio_pct != null)
        bits.push(pct(e.norm_ratio_pct) + " per retained pixel after auto-crop");
      if (e.source_bytes != null && e.projected_bytes != null)
        bits.push(gibApprox(e.source_bytes - e.projected_bytes) + " freed when it finishes");
    }
    p.detail.textContent = bits.join(". ") + ".";
    p.scale.setAttribute(
      "aria-label",
      lost
        ? "Resolution was lost; the size ratio is not comparable"
        : "Projected output keeps " +
            pct(r) +
            " of the source. Target band is " +
            BAND_LO +
            " to " +
            BAND_HI +
            " percent.",
    );
  }

  /* The live card updates IN PLACE. Rebuilding it on every SSE frame silently
   restarted every CSS animation and defeated the bar's width transition --
   the fill was always a brand-new node, so it could never animate. Structure
   is rebuilt only when the set of running encodes changes; numbers and chips
   update on the nodes already there. Progress lives beside the bar at
   HandBrake's full precision, and only there -- one number, one precision. */
  var liveRefs = {};
  /* One control for both cards. The switch never touches the running encode:
   on means only that the NEXT one will not start.

   It used to DISABLE itself for the whole POST round-trip, and that
   round-trip is not short: /api/pause answers with a freshly built state
   payload, which stats the NAS roots over SMB -- 0.5-1.0 s here. A click
   inside that window landed on a disabled button, so it never reached api()
   and never even raised the "another action is still in flight" notice, the
   ONE outcome that notice exists to prevent. Driving the real page, six of
   ten rapid clicks vanished with no feedback of any kind. Pause/resume is
   precisely the control a person hammers -- pause, look at the machine,
   resume, pause again -- so refusing input for a second at a time is the
   whole bug.

   The switch therefore never disables. Two rules replace it:
     - pauseState() draws the user's UNSETTLED intent over the server's
       committed value, so the flip lands on the click, not after the SMB
       stats.
     - The LAST click wins: a click during a write is recorded rather than
       dropped, and the in-flight call drains it when it lands. Two
       round-trips still never race -- that single-flight property is what
       the disable was really protecting -- but nothing is silently refused,
       and a round trip back to where it started sends nothing at all.

   Intent is released the moment a write settles, so a denied LAN write or a
   dead server snaps the switch back to the truth instead of leaving the
   optimistic flip standing. The queue tab's "next after resume" mark stays
   on the server's value on purpose: an unconfirmed intent may draw the
   control under the finger, never a claim about what the driver will do. */
  var pauseWant = null; /* what was last asked for, until a write settles */
  var pauseBusy = false; /* a write is in flight */

  function pauseState(s) {
    return pauseWant !== null ? pauseWant : s.paused === true;
  }

  function pauseSend(want) {
    pauseWant = want;
    /* Draw it on this click, before anything touches the wire. */
    if (last.state) paint(last.state);
    if (pauseBusy) return;
    pauseBusy = true;
    (function drain() {
      var sent = pauseWant;
      api("/api/pause", { paused: sent }).then(function () {
        /* A click landed while this was in flight and asked for something
         else: send the newer intent rather than losing it. */
        if (pauseWant !== sent) return drain();
        /* Settled. api() has already painted the server's answer on success
         and raised a notice on failure; releasing the intent is what lets
         either of those reach the switch. */
        pauseWant = null;
        pauseBusy = false;
        if (last.state) paint(last.state);
      });
    })();
  }

  /* ---- the big play/pause toggle ------------------------------------------
   One oversized control at the top of the live section, because the states
   it covers were previously spread across three places (the pause switch,
   the queue's start button, and a nohup line in CLAUDE.md). Three states:
     driver dead          -> PLAY  = POST /api/driver/start (launches the
                             autopilot; the server clears a leftover pause
                             flag first -- play means play)
     driver alive, paused -> PLAY  = resume, via the SAME pauseSend the
                             switch uses, so the two controls share one
                             intent and can never race each other
     driver alive         -> PAUSE = pause-after-current
   The "next up" line is ALWAYS the server's own pick (next_up, from
   _mark_ready -> core.pick_next) -- never re-derived here, the same rule as
   the queue's green row. Same native-<button> and never-disable rules as
   the pause switch: a start in flight redraws as "starting…" through
   driverWant, and a refused start snaps back when the intent releases. */
  var driverWant = null;
  function driverStartSend() {
    if (driverWant !== null) return;
    driverWant = true;
    if (last.state) paint(last.state);
    api("/api/driver/start", {}).then(function () {
      driverWant = null;
      if (last.state) paint(last.state);
    });
  }
  /* ONE state machine for BOTH renditions of the control: the full-width
   .bigplay card (drawn only while nothing is encoding -- there is no bar for
   a circle to lead) and the .pp circle at the head of the live card's
   progress bar (2026-09-08, operator's pick). Two renditions of one decision
   drift the moment one is edited alone, so neither carries its own branch. */
  function toggleIntent(paused, driverAlive, live, nextTitle, lowSpace) {
    var pick = nextTitle ? "next up: " + nextTitle : "nothing pickable — check the queue";
    if (!driverAlive && driverWant !== null)
      return {
        cls: "busy",
        act: "Starting the driver…",
        sub: nextTitle ? "first pick: " + nextTitle : "",
        go: null,
      };
    if (!driverAlive) return { cls: "play", act: "Start encoding", sub: pick, go: driverStartSend };
    if (paused)
      return {
        cls: "play",
        act: "Resume encoding",
        sub: pick,
        go: function () {
          pauseSend(false);
        },
      };
    return {
      cls: "pause",
      act: "Pause encoding",
      sub: live.length
        ? "after this encode — " + live[0].title + " still finishes and syncs"
        : lowSpace
          ? "waiting: low space on the staging drive — free up space"
          : "nothing new will start",
      go: function () {
        pauseSend(true);
      },
    };
  }
  function bigToggle(paused, driverAlive, live, nextTitle, lowSpace) {
    var t = toggleIntent(paused, driverAlive, live, nextTitle, lowSpace);
    var b = el("button", "bigplay " + t.cls);
    b.type = "button";
    var icon = el("span", "bp-icon");
    icon.setAttribute("aria-hidden", "true");
    var txt = el("span", "bp-text");
    txt.appendChild(el("span", "bp-act", t.act));
    txt.appendChild(el("span", "bp-sub", t.sub));
    b.appendChild(icon);
    b.appendChild(txt);
    if (t.go) b.addEventListener("click", t.go);
    return b;
  }
  /* The circle. It leads the progress bar inside the live card, so the
   control sits ON the thing it controls instead of in a card above it. The
   sentence the big card carried becomes its accessible name and tooltip;
   the ARMED consequence (a sync REPLACES the library original) still renders
   on the card as .ppnote, because a tooltip never renders on the phones. */
  function circleToggle(paused, driverAlive, live, nextTitle) {
    var t = toggleIntent(paused, driverAlive, live, nextTitle, false);
    var b = el("button", "pp " + t.cls);
    b.type = "button";
    var name = t.act + (t.sub ? " — " + t.sub : "");
    b.setAttribute("aria-label", name);
    b.title = name;
    var icon = el("span", "pp-icon");
    icon.setAttribute("aria-hidden", "true");
    b.appendChild(icon);
    if (t.go) b.addEventListener("click", t.go);
    return b;
  }

  /* The paused ghost card (operator 2026-09-11, option 4 of six previews):
   while paused with a live driver and nothing encoding, the slot draws the
   RUNNING card's exact layout -- title, chips, the circle at the head of the
   bar, the projection strip, the field row -- with every value at a dash
   and the circle in its play state. Resuming then changes nothing on the
   card but the circle and the numbers filling in. The big full-width card
   is kept only for the states that are not a pause: driver dead (start),
   low space, and the two sensor-failure pauses that must say themselves
   first. The chips mirror liveChips() field for field: the next pick's
   name, the quality it WILL start at (same derivation as the queue's start
   button), and the two trailing chips liveChips() always draws. */
  function ghostCard(paused, driverAlive, nextRow, defs) {
    var c = el("div", "card live");
    var top = el("div", "live-top");
    var name = String(nextRow.folder || nextRow.title)
      .split("/")
      .pop();
    var t = el("div", "live-title", name);
    if (name !== nextRow.title) t.title = nextRow.title;
    top.appendChild(t);
    /* One state chip, the same three states the circle draws. */
    if (paused) {
      var pchip = el("span", "chip warn", "PAUSED");
      pchip.title = "Nothing starts until you press play. This is the next pick.";
      top.appendChild(pchip);
    } else if (!driverAlive) {
      var dchip = el("span", "chip warn", "NO DRIVER");
      dchip.title =
        "No autopilot process exists. Press play to launch it; check " +
        ".autopilot.log for a HALTED: line first.";
      top.appendChild(dchip);
    } else {
      var nchip = el("span", "chip", "NEXT UP");
      nchip.title = "The driver's next pick. It starts on the driver's next pass.";
      top.appendChild(nchip);
    }
    var enc = nextRow.enc
      ? nextRow.enc
      : nextRow.crf_set
        ? "x265_10bit"
        : defs.encoder_default || "x265_10bit";
    var q = nextRow.enc
      ? nextRow.enc_q
      : nextRow.crf_set
        ? nextRow.crf
        : defs.quality_default != null
          ? defs.quality_default
          : nextRow.crf;
    if (q != null) top.appendChild(el("span", "chip", qLabel(enc) + " " + q));
    top.appendChild(el("span", "chip", "decoder errors not yet reported"));
    top.appendChild(el("span", "chip", "UNKNOWN"));
    c.appendChild(top);
    var row = el("div", "barrow"),
      bar = el("div", "bar");
    row.appendChild(circleToggle(paused, driverAlive, [], nextRow.title));
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", "Encode progress");
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.appendChild(el("i"));
    row.appendChild(bar);
    row.appendChild(el("div", "pctbig num dim", "—"));
    c.appendChild(row);
    var pj = projBlock();
    updateProj(pj.refs, {
      verdict: "unknown",
      source_bytes: nextRow.bytes == null ? null : nextRow.bytes,
    });
    pj.refs.detail.textContent = "The projection starts once the encode does.";
    c.appendChild(pj.node);
    var kv = el("div", "kv");
    liveFields({}).forEach(function (p) {
      kv.appendChild(kvCell(p[0], p[2], p[3], "—").node);
    });
    var disk = diskField();
    kv.appendChild(disk.node);
    c.appendChild(kv);
    return { node: c, disk: disk };
  }

  function renderLive(live, s, driverAlive, nextTitle, nextRow, defs) {
    /* paused rides in the summary -- the ONE carrier, the same field report.py
     banners -- never a second top-level copy. It and driverAlive are in the
     sig: the pause control and the idle card's claims are built once per
     card build, so flipping either must rebuild the card (rare events, not
     the 2s SSE frames the animation-restart rule is about). pauseState()
     lets an unsettled click draw itself here without waiting for the write
     to land -- the sig then moves on the click, which is what makes the
     switch feel like a switch. */
    var paused = pauseState(s);
    var host = document.getElementById("liveWrap");
    var sig =
      live
        .map(function (e) {
          return e.title;
        })
        .join("|") +
      "|p:" +
      (paused ? 1 : 0) +
      "|d:" +
      (driverAlive ? 1 : 0) +
      "|n:" +
      (nextTitle || "") +
      "|nq:" +
      (nextRow ? [nextRow.enc, nextRow.enc_q, nextRow.crf_set ? nextRow.crf : ""].join(",") : "") +
      "|w:" +
      (driverWant !== null ? 1 : 0) +
      "|ls:" +
      (s.low_space ? 1 : 0);
    if (host.dataset.sig !== sig) {
      host.replaceChildren();
      liveRefs = {};
      host.dataset.sig = sig;
      /* THE GHOST IS THE IDLE CARD (operator 2026-09-11, twice): whenever
       nothing encodes and there is a pick, the slot draws the running card's
       shape -- paused, between encodes, or with no driver -- and only the
       circle and the state chip say which. The big card and the prose idle
       card survive for the states that have no pick to be the card OF, or
       where a sensor fact must speak first: drive unmounted, low space,
       an empty queue. */
      if (s.x9_online !== false && !s.low_space && nextRow && !live.length) {
        var g = ghostCard(paused, driverAlive, nextRow, defs || {});
        makeCollapsible(g.node, "live-idle");
        host.appendChild(g.node);
        liveRefs.__ghost = g;
      } else if (!live.length) {
        host.appendChild(bigToggle(paused, driverAlive, live, nextTitle, s.low_space === true));
        var c = el("div", "card");
        if (paused) {
          /* A user-chosen state must never mask a sensor failure: the drive
           being gone, or no driver existing to honour the resume, are the
           louder facts and say themselves first. */
          if (s.x9_online === false) {
            c.appendChild(el("div", "live-title", "Paused — and the staging drive is not mounted"));
            c.appendChild(
              el(
                "div",
                "verdict",
                "The X9 is unreachable, so nothing could encode regardless of " +
                  "the pause. Reconnect the drive, then resume.",
              ),
            );
          } else if (!driverAlive) {
            c.appendChild(el("div", "live-title", "Paused — but no driver is running"));
            c.appendChild(
              el(
                "div",
                "verdict",
                "No autopilot process exists, so nothing will start when you " +
                  "resume either. Check .autopilot.log for a HALTED: line " +
                  "before relaunching.",
              ),
            );
          } else {
            /* Paused with nothing pickable: the ghost card above needs a
             next pick to be the card OF, so this is the one paused state
             the big toggle still fronts. */
            c.appendChild(el("div", "live-title", "Paused — nothing will start"));
          }
          makeCollapsible(c, "live-idle");
          host.appendChild(c);
          return;
        }
        if (s.low_space) {
          /* The floor is a wait the driver chose FOR the human: say so where
           the encode would be, not only in the alert stack above. */
          c.appendChild(el("div", "live-title", "Waiting — low space on the staging drive"));
          c.appendChild(
            el(
              "div",
              "verdict",
              "Nothing new starts until enough space is free (see the alert above " +
                "for the figures). Free up space on the X9; the driver rechecks " +
                "every minute and resumes by itself — no switch to flip.",
            ),
          );
        } else {
          c.appendChild(el("div", "live-title", "Nothing encoding"));
          c.appendChild(
            el(
              "div",
              "verdict",
              s.x9_online
                ? "The staging drive is mounted and idle."
                : "The staging drive is not mounted.",
            ),
          );
        }
        makeCollapsible(c, "live-idle");
        host.appendChild(c);
        return;
      }
      live.forEach(function (e) {
        var c = el("div", "card live"),
          refs = {};
        refs.top = liveChips(e);
        c.appendChild(refs.top);
        var row = el("div", "barrow"),
          bar = el("div", "bar");
        row.appendChild(circleToggle(paused, driverAlive, live, nextTitle));
        bar.setAttribute("role", "progressbar");
        bar.setAttribute("aria-label", "Encode progress");
        bar.setAttribute("aria-valuemin", "0");
        bar.setAttribute("aria-valuemax", "100");
        refs.bar = bar;
        refs.fill = el("i");
        bar.appendChild(refs.fill);
        row.appendChild(bar);
        refs.pct = el("div", "pctbig num", "—");
        row.appendChild(refs.pct);
        c.appendChild(row);
        var pj = projBlock();
        refs.proj = pj.refs;
        c.appendChild(pj.node);
        var kv = el("div", "kv");
        refs.kv = {};
        liveFields(e).forEach(function (p) {
          var cell = kvCell(p[0], p[2], p[3], "—");
          refs.kv[p[0]] = cell.b;
          kv.appendChild(cell.node);
        });
        refs.disk = diskField();
        kv.appendChild(refs.disk.node);
        c.appendChild(kv);
        refs.verdict = el("div", "verdict", "");
        c.appendChild(refs.verdict);
        /* Pause-after-current is the circle at the head of the bar. The
         encode itself is never touched: the flag only stops the NEXT one
         from starting, which is the difference between this and the abort
         button on the queue row. The armed label must name the consequence
         ON the card -- "sync" means the library original is REPLACED, and a
         tooltip never renders on the phones. */
        if (paused)
          c.appendChild(
            el(
              "div",
              "ppnote",
              "will pause after this encode — " +
                e.title +
                " still finishes, syncs, and replaces its " +
                (e.source_bytes != null ? gib(e.source_bytes) + " " : "") +
                "library original",
            ),
          );
        /* Distinct key from the idle card — folding "Nothing encoding" must
         not fold the next real encode — and loud verdicts always open. */
        makeCollapsible(c, "live-run", !!PROJ_LOUD[e.verdict]);
        host.appendChild(c);
        liveRefs[e.title] = refs;
      });
    }
    live.forEach(function (e) {
      var refs = liveRefs[e.title];
      if (!refs) return;
      var top = liveChips(e);
      refs.top.replaceWith(top);
      refs.top = top;
      refs.fill.style.width = (e.pct || 0) + "%";
      refs.bar.setAttribute("aria-valuenow", String(e.pct || 0));
      refs.pct.textContent = pctLive(e.pct);
      updateProj(refs.proj, e);
      liveFields(e).forEach(function (p) {
        var b = refs.kv[p[0]];
        if (b) b.textContent = p[1];
      });
      updateDisk(refs.disk, s, defs && defs.x9_total_bytes);
      writeVerdict(refs, e.verdict_note, !!PROJ_LOUD[e.verdict]);
    });
    if (!live.length && liveRefs.__ghost)
      updateDisk(liveRefs.__ghost.disk, s, defs && defs.x9_total_bytes);
  }

  /* The verdict note behind a caution icon.
   ------------------------------------------------------------------
   The note is ONE string from `core._verdict()`, and the three long ones all
   lead with a capitalised clause and a colon ("RESOLUTION LOST: ...",
   "NO vt_h265_10bit BASELINE YET: ...", "UNUSUAL: ..."). That lead is the
   headline; the rest is the explanation. The split is done HERE and not in
   core.py on purpose: core is the decision path, editing it needs the pause
   procedure, and this is a presentation question that changes no verdict.

   The headline STAYS ON THE CARD. Only the explanation goes behind the icon.
   This channel also carries "Do not delete the original", and the iPad is
   where this job is actually watched -- a warning that renders as a bare
   glyph on the one device that cannot hover is not a warning. Short notes
   ("Solid reduction - let it run.") have no body and get no icon at all. */
  var NOTE_HEAD = /^([^:]{1,60}):\s+/;
  var tipSeq = 0;

  function splitNote(note) {
    note = (note || "").trim();
    var m = NOTE_HEAD.exec(note);
    return m ? { head: m[1], body: note.slice(m[0].length) } : { head: note, body: "" };
  }

  /* Written in place like every other live-card field, and ONLY when the text
   or the severity actually moves. Rebuilding it each SSE frame would slam a
   tooltip shut twice a second while somebody was reading it -- the same
   in-place rule the bar's width transition needs. */
  function writeVerdict(refs, note, loud) {
    var cls = "verdict" + (loud ? " loud" : "");
    if (refs.noteText === note && refs.noteCls === cls) return;
    refs.noteText = note;
    refs.noteCls = cls;
    var box = refs.verdict;
    box.className = cls;
    box.textContent = "";
    var parts = splitNote(note);
    if (!parts.body) {
      box.textContent = parts.head;
      return;
    }
    var id = "vtip" + ++tipSeq;
    /* A real <button>, for the reason the pause switch is one: iOS Safari
     only synthesises a click from a tap on natively interactive elements,
     so a listener on a span is a coin toss on the device this is read on. */
    var btn = el("button", "cautionbtn", "\u26a0");
    btn.type = "button";
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-describedby", id);
    btn.setAttribute("aria-label", "why this matters");
    var tip = el("div", "tip", parts.body);
    tip.id = id;
    tip.setAttribute("role", "tooltip");
    /* Hover and focus are CSS. The click is what makes it work on a finger,
     and it LATCHES -- a tap that opened a tip the finger is still covering
     would be useless if it closed again on the next frame. */
    btn.addEventListener("click", function () {
      var open = box.classList.toggle("tipopen");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    box.appendChild(btn);
    box.appendChild(el("span", "vhead", parts.head));
    box.appendChild(tip);
  }

  function table(cols, rows, build) {
    var t = el("table"),
      thead = el("thead"),
      tr = el("tr");
    cols.forEach(function (c) {
      var th = el("th", [c.n ? "n" : "", c.cls || ""].join(" ").trim() || null, c.label);
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    t.appendChild(thead);
    var tb = el("tbody");
    /* build() may return one <tr> or an ARRAY of them -- the History tab gives a
     title whose file is still travelling a second, full-width row. */
    rows.forEach(function (r, i) {
      var out = build(r, i);
      if (Array.isArray(out))
        out.forEach(function (n) {
          tb.appendChild(n);
        });
      else tb.appendChild(out);
    });
    t.appendChild(tb);
    return t;
  }

  /* The queue is hand-editable: rows drag to reorder (the order you drop is
   the order the pipeline picks from) and any non-encoding row can be
   skipped. Skipped rows keep their place at the BOTTOM, greyed, with a
   restore button -- a skip that vanished would read as "finished". */
  /* Two-step confirm shared by start and abort: first click arms the button and
   rewrites its label with the consequence; the second click within 6s acts.
   SSE repaints every 2s would disarm it mid-decision, so an armed control
   pins its .rowacts visible via the armed class and paint() skips rebuilds
   while one is armed (see armedTitle). */
  var armedTitle = null;
  function arm(b, acts, armedLabel, fire) {
    if (b.dataset.armed === "1") {
      b.dataset.armed = "";
      armedTitle = null;
      b.disabled = true;
      fire();
      return;
    }
    b.dataset.armed = "1";
    armedTitle = b.dataset.title;
    var plain = b.textContent;
    b.textContent = armedLabel;
    b.classList.add("armed");
    acts.classList.add("armed");
    setTimeout(function () {
      if (b.dataset.armed !== "1") return;
      b.dataset.armed = "";
      armedTitle = null;
      b.textContent = plain;
      b.classList.remove("armed");
      acts.classList.remove("armed");
      if (last.pending && last.state) {
        last.pending = false;
        paint(last.state);
      }
    }, 6000);
  }

  /* The quality picker. It writes an override the DRIVER reads (`smeltr crf`
   for an x265 rung, `smeltr encoder` for a hardware one), so this control
   decides -e and -q for a title the driver may not reach for hours -- not
   just for a hand-started encode.

   ONE control, not two. x265 quality is CRF and VideoToolbox quality is CQ on
   Apple's reversed scale, so the number alone is ambiguous and every option
   names its scale. A second dropdown for the encoder beside a number that
   meant something else is the shape this column already rejected once.

   The two override files stay consistent because the SERVER clears the other
   one on every write -- one round trip, one committed state, no window where
   a row has a CRF from one file and an encoder from the other.

   "auto" is a real option, not decoration: it CLEARS the override so the row
   goes back to tracking the pipeline default. Without it there is no way out
   of a hand-picked value except picking the default and pinning it, which
   silently opts the row out of a default that later moves.

   A repaint while the menu is open would tear the <select> out from under
   the pointer, so an open picker defers the rebuild the way an armed confirm
   and an active drag already do (crfOpen), and releases it on blur. */
  var crfOpen = null;
  function crfPicker(r, s) {
    var choices = (s && s.crf_choices) || [10, 12, 14, 16, 18, 20, 22];
    var def = (s && s.crf_default) || 14;
    /* What an untouched row ACTUALLY starts on. The default encoder is
     VideoToolbox since 2026-09-06, so "auto" must say "VT CQ 70", never the
     x265 rung crf_default carries -- that number is real (smeltr crf still
     answers it) but the driver only consults it for x265 titles. */
    var encDef = (s && s.encoder_default) || "x265_10bit";
    var qDef = s && s.quality_default != null ? s.quality_default : def;
    var defTxt = qLabel(encDef) + " " + qDef;
    var menus = (s && s.encoder_choices) || { x265_10bit: choices };
    var set = !!(r.crf_set || r.enc);
    var sel = el("select", "crfsel crfcell" + (set ? " set" : ""));
    sel.setAttribute("aria-label", "Start encoder and quality for " + r.title);
    sel.title = r.enc
      ? "Chosen by hand — this title's encode starts on " +
        r.enc +
        " at " +
        qLabel(r.enc) +
        " " +
        r.enc_q +
        ". The auto-kill ladder may still step it from there."
      : r.crf_set
        ? "Chosen by hand — this title's encode starts at CRF " +
          r.crf +
          ". The auto-kill ladder may still step it from there."
        : "Following the pipeline default (" +
          defTxt +
          "). Pick a value to fix " +
          "this title's start; the ladder may still step it from there.";
    var auto = el("option", null, defTxt + " (auto)");
    auto.value = "";
    sel.appendChild(auto);
    Object.keys(menus).forEach(function (enc) {
      (menus[enc] || []).forEach(function (c) {
        var o = el("option", null, qLabel(enc) + " " + c);
        o.value = enc + ":" + c;
        sel.appendChild(o);
      });
    });
    sel.value = r.enc ? r.enc + ":" + r.enc_q : r.crf_set ? "x265_10bit:" + r.crf : "";
    /* Interacting with the picker must not start a drag on the row, and must
     not arm/disarm anything in the title cell beside it. */
    sel.addEventListener("mousedown", function (e) {
      e.stopPropagation();
    });
    sel.addEventListener("click", function (e) {
      e.stopPropagation();
    });
    sel.addEventListener("focus", function () {
      crfOpen = r.title;
    });
    sel.addEventListener("blur", function () {
      if (crfOpen !== r.title) return;
      crfOpen = null;
      if (last.pending) {
        last.pending = false;
        last.key = null;
        if (last.state) paint(last.state);
      }
    });
    sel.addEventListener("change", function () {
      var parts = sel.value === "" ? null : sel.value.split(":");
      /* Released on blur, not here: the response repaints the table and the
       fresh <select> reads the committed value, so leaving it set would
       wedge the deferral against a node that no longer exists. */
      crfOpen = null;
      sel.disabled = true;
      /* An x265 rung is the CRF override the driver already reads; anything
       else is an encoder override. Either endpoint clears the other file, so
       one request settles the whole row. */
      var call =
        parts && parts[0] !== "x265_10bit"
          ? api("/api/queue/encoder", {
              title: r.title,
              encoder: parts[0],
              quality: parseInt(parts[1], 10),
            })
          : api("/api/queue/crf", {
              title: r.title,
              crf: parts ? parseInt(parts[1], 10) : null,
            });
      call.finally(function () {
        sel.disabled = false;
      });
    });
    return sel;
  }

  function rowActions(r, s) {
    var acts = el("span", "rowacts");
    if (r.encoding) {
      /* Abort kills HandBrake, deletes the partial, and writes a skip so the
       driver cannot immediately restart the same title. The armed label says
       what is being thrown away. */
      var ab = el("button", "act", "abort");
      ab.type = "button";
      ab.dataset.title = r.title;
      ab.title = "Stop this encode, discard the partial output, and skip the title";
      ab.addEventListener("click", function () {
        arm(ab, acts, "discard the encode so far?", function () {
          api("/api/encode/abort", { title: r.title });
        });
      });
      acts.appendChild(ab);
      return acts;
    }
    if (r.ready && s && s.can_start !== false) {
      /* No CRF control here any more. The picker lives in the CRF COLUMN, on
       every queued row, and the driver honours it too -- a second picker in
       the hover actions was a rung that applied only to a hand-started
       encode, sitting one column away from a number that meant something
       else. One control, one value, both consumers. */
      var go = el("button", "act go", "start encode");
      go.type = "button";
      go.dataset.title = r.title;
      /* The row's own planned start, encoder included -- the same pair the
       column picker shows and the driver would use. */
      /* Encoder BY NAME in every case. An untouched row starts on the
       pipeline default (VideoToolbox CQ 70 since 2026-09-06), a hand-picked
       CRF means x265, and an encoder override names itself -- posting
       {crf: 14, encoder: null} would have the server pick VT and read 14
       as a CQ. */
      var startEnc = r.enc
        ? r.enc
        : r.crf_set
          ? "x265_10bit"
          : (s && s.encoder_default) || "x265_10bit";
      var startQ = r.enc
        ? r.enc_q
        : r.crf_set
          ? r.crf
          : s && s.quality_default != null
            ? s.quality_default
            : r.crf;
      var startLbl = qLabel(startEnc) + " " + startQ;
      go.title = "Start encoding this title now at " + startLbl;
      go.addEventListener("click", function () {
        arm(go, acts, "start at " + startLbl + "?", function () {
          api("/api/encode/start", {
            title: r.title,
            crf: startQ,
            encoder: startEnc,
          });
        });
      });
      acts.appendChild(go);
    }
    if (!r.skipped && !r.staged && r.arriving_bytes == null) {
      if (r.stage_queued != null) {
        /* Pending, not moving: nothing has been written yet, so this needs no
         arming — there is nothing to throw away. */
        var un = el("button", "act", "unqueue");
        un.type = "button";
        un.dataset.title = r.title;
        un.title = "Take this title out of the pull queue";
        un.addEventListener("click", function () {
          un.disabled = true;
          api("/api/stage/cancel", { title: r.title }).finally(function () {
            un.disabled = false;
          });
        });
        acts.appendChild(un);
      } else {
        /* Library-only rows can be pulled onto the staging drive on demand. The
         armed label states the cost up front — this is a multi-GiB transfer —
         and, when the wire is busy, that the click BUYS A PLACE IN LINE
         rather than starting anything. */
        var busy = !!(s && s.stage_busy);
        var pull = el("button", "act", busy ? "queue pull" : "stage");
        pull.type = "button";
        pull.dataset.title = r.title;
        pull.title = busy
          ? "Add this title to the pull queue — one transfer runs at a time"
          : "Copy this title's file from the library to the staging drive now";
        pull.addEventListener("click", function () {
          arm(
            pull,
            acts,
            r.bytes == null
              ? busy
                ? "queue this title (size unknown)?"
                : "pull this title (size unknown) to the X9?"
              : busy
                ? "queue " + gib(r.bytes) + " behind the current pull?"
                : "pull " + gib(r.bytes) + " to the X9?",
            function () {
              api("/api/stage/start", { title: r.title });
            },
          );
        });
        acts.appendChild(pull);
      }
    }
    var b = el("button", "act" + (r.skipped ? " restore" : ""), r.skipped ? "restore" : "skip");
    b.type = "button";
    b.title = r.skipped
      ? "Put this title back in the queue"
      : "Skip this title — the pipeline moves on to the next one";
    b.addEventListener("click", function () {
      b.disabled = true;
      api("/api/queue/skip", { title: r.title, skipped: !r.skipped }).finally(function () {
        b.disabled = false;
      });
    });
    acts.appendChild(b);
    return acts;
  }

  /* ---- live numbers, written IN PLACE ---------------------------------------
   Two places draw a growing transfer: an arriving queue row, and the History
   tab's full-width row under its ledger row. (A push used to render on the
   Queue tab too, as a synthetic "transferring" row up top — removed
   2026-09-01, operator's call: a recorded title has left the queue, and a
   row there read as work still waiting to encode. History is the one tab
   that draws a push.) All of these used to be redrawn by rebuilding the
   entire table once per SSE
   frame, because paint()'s repaint key carried their byte counts and those
   change on every frame. That rebuild replayed the pane's fade-in and reset
   the scroll position, so the page visibly blinked every two seconds for the
   whole length of a 45-minute transfer.

   Same discipline as the live card: STRUCTURE rebuilds, NUMBERS write in
   place. renderQueue/renderLedger drop an empty slot; updateProgress() fills
   it on every frame and only rebuilds the slot's children when the SHAPE
   changes (a bar becoming "stalled", a pull that started or landed). The old
   frozen-bar bug stays fixed -- the numbers are still painted every frame,
   they are just no longer painted by a teardown. */
  var progRefs = {};
  function progSlot(key, host) {
    var slot = el("span", "progslot");
    host.appendChild(slot);
    progRefs[key] = { slot: slot, shape: null, fill: null, txt: null, mark: null };
  }
  function progWrite(key, st) {
    var ref = progRefs[key];
    if (!ref) return;
    if (ref.shape !== st.shape) {
      ref.shape = st.shape;
      ref.slot.replaceChildren();
      ref.mark = el("span", "mark " + (st.shape === "stall" ? "stall" : "xfer"), st.label);
      ref.slot.appendChild(ref.mark);
      ref.fill = null;
      if (st.shape === "bar") {
        var bar = el("span", "minibar" + (st.pull ? " pull" : ""));
        ref.fill = el("i");
        bar.appendChild(ref.fill);
        ref.slot.appendChild(bar);
      }
      ref.txt = el("span", "xfer-pct");
      ref.slot.appendChild(ref.txt);
    }
    if (ref.mark.textContent !== st.label) ref.mark.textContent = st.label;
    if (ref.fill && st.pct != null) ref.fill.style.width = Math.max(0, Math.min(100, st.pct)) + "%";
    var t = st.text || "";
    if (ref.txt.textContent !== t) ref.txt.textContent = t;
    ref.txt.hidden = !t;
  }
  /* A pull still landing — replenisher or dashboard, same thing. Denominator is
   the row's own library original. A partial that is not moving is "stalled",
   never a progress bar; one LARGER than the source is a stale leftover. */
  function arrState(r) {
    if (r.arriving_stalled || (r.bytes && r.arriving_bytes > r.bytes))
      return {
        shape: "stall",
        label: "stalled",
        text: gib(r.arriving_bytes) + " of " + gib(r.bytes) + " pulled — not moving",
      };
    if (!r.bytes) return { shape: "plain", label: "arriving" };
    var p = Math.max(0, Math.min(100, (r.arriving_bytes / r.bytes) * 100));
    var t = p.toFixed(1) + "% · " + gib(r.arriving_bytes) + " of " + gib(r.bytes) + " pulled";
    /* MiB/s, not MB/s: the sizes in this sentence and the Network chart above
     are binary, and one decimal rate beside them read 4.9% high. The ETA is
     an extrapolation, so it carries the page's "~" estimate marker. */
    if (r.arriving_rate_bps > 0)
      t +=
        " · " +
        (r.arriving_rate_bps / 1048576).toFixed(0) +
        " MiB/s · ~" +
        dur((r.bytes - r.arriving_bytes) / r.arriving_rate_bps) +
        " left";
    return { shape: "bar", pull: true, label: "arriving", pct: p, text: t };
  }
  /* Both operands carry their unit. The destination is NOT repeated in the
   caption: the ledger row directly above carries the NAS and folder pills.
   Rate and ETA fit because the transfer owns a full row of its own. */
  function xferState(t) {
    var stall = !!t.stalled;
    var moved = gib(t.done_bytes) + " of " + gib(t.total_bytes) + " copied";
    var txt = stall ? "no progress — " + moved : t.pct != null ? pct(t.pct) + " · " + moved : moved;
    if (!stall && t.rate_bps > 0)
      txt +=
        " · " +
        (t.rate_bps / 1048576).toFixed(0) +
        " MiB/s · ~" +
        dur((t.total_bytes - t.done_bytes) / t.rate_bps) +
        " left";
    return {
      shape: stall ? "stall" : t.pct != null ? "bar" : "plain",
      label: stall ? "stalled" : "transferring",
      pct: t.pct,
      text: txt,
    };
  }
  function updateProgress(s) {
    if (tab === "queue") {
      (s.queue || []).forEach(function (r) {
        if (r.skipped || r.encoding || r.arriving_bytes == null) return;
        progWrite("arr|" + r.title.toLowerCase(), arrState(r));
      });
    } else if (tab === "ledger") {
      (s.transfers || []).forEach(function (t) {
        progWrite("led|" + t.title, xferState(t));
      });
    }
  }

  /* The row shape renderQueue draws. Deliberately NOT arriving_bytes or
   arriving_rate_bps: those change every frame and are painted by
   updateProgress. The BOOLEANS derived from them are here, because they
   decide which nodes exist. */
  function qShape(r) {
    return [
      r.title,
      r.mbps,
      r.bytes,
      r.location,
      r.src_dir,
      r.crf,
      !!r.crf_set,
      !!r.skipped,
      !!r.pinned,
      !!r.manual,
      !!r.encoding,
      !!r.ready,
      !!r.staged,
      !!r.next_up,
      !!r.error,
      r.error_note || null,
      !!r.done,
      r.done_note || null,
      r.arriving_bytes != null,
      !!r.arriving_stalled,
      r.stage_queued == null ? null : r.stage_queued,
      r.stage_wait || null,
      r.enc || null,
      r.enc_q == null ? null : r.enc_q,
    ];
  }
  function xShape(t) {
    return [t.title, t.nas, t.src_dir, !!t.stalled, t.total_bytes, t.pct != null];
  }

  function renderQueue(q, s, live) {
    progRefs = {};
    var liveCrf = {};
    (live || []).forEach(function (e) {
      /* {q, enc}, not a bare number: the encoding row has to print the scale
       the running encoder actually uses, and only the encoder can say which. */
      if (e.crf != null)
        liveCrf[(e.folder || e.title).toLowerCase()] = { q: e.crf, enc: e.encoder };
    });
    var pane = document.getElementById("pane");
    pane.replaceChildren();
    /* An unmounted NAS must never look like a finished job — and neither must
     a push still travelling: the driver's own stop condition refuses to fire
     while a sync is in flight, so this tab may not claim what the driver
     won't. The transfer itself renders on History. */
    if (s && s.library_complete === false)
      pane.appendChild(
        el(
          "div",
          "empty",
          "Library not fully mounted — " +
            (s.roots_offline || []).join(", ") +
            " offline. This list is PARTIAL, not empty.",
        ),
      );
    if (!q.length) {
      if (!(s && s.library_complete === false))
        pane.appendChild(
          el(
            "div",
            "empty",
            s && s.xfer_count
              ? "Nothing left to encode — " +
                  s.xfer_count +
                  " file" +
                  (s.xfer_count > 1 ? "s" : "") +
                  " still copying to the NAS (see History)"
              : "Nothing left above the stop threshold.",
          ),
        );
      return;
    }
    var pinned = q.filter(function (r) {
      return r.pinned && !r.skipped;
    }).length;
    var active = q.filter(function (r) {
      return !r.skipped;
    }).length;
    var ranks = [],
      rn = 0;
    q.forEach(function (r, idx) {
      ranks[idx] = r.skipped ? null : ++rn;
    });
    pane.appendChild(
      table(
        [
          { label: "", cls: "gripcol" },
          { label: "Rank", n: true },
          { label: "SRC Mb/s", n: true, cls: "unit" },
          { label: "Src size", n: true },
          { label: "Quality", n: true },
          { label: "Title", cls: "title-cell" },
          { label: "NAS" },
          { label: "Src folder" },
          { label: "Status" },
        ],
        q,
        function (r, i) {
          /* encoding (amber) beats ready (green) beats skipped. ready is the ONE
         row next_title.py would pick -- server-computed WHOLE, in
         _mark_ready: absent while anything encodes, absent while paused.
         Green is the page's vocabulary for "going now"; no client-side
         re-gating, or the invariant splits across the wire. */
          var tr = el(
            "tr",
            r.encoding ? "rowenc" : r.ready ? "rowready" : r.skipped ? "rowskip" : null,
          );
          tr.dataset.title = r.title;
          tr.dataset.idx = String(i);
          var grip = el("td", "gripcol");
          if (!r.skipped) {
            var g = el("span", "grip", "⋮⋮");
            g.title = "Drag to reorder";
            grip.appendChild(g);
            tr.draggable = true;
          }
          tr.appendChild(grip);
          tr.appendChild(el("td", "n q-rank", ranks[i] == null ? "—" : String(ranks[i])));
          var band = r.mbps >= 90 ? "mbps-hi" : r.mbps >= 80 ? "mbps-mid" : "mbps-lo";
          tr.appendChild(el("td", "n mono q-mbps " + band, r.mbps.toFixed(1)));
          tr.appendChild(el("td", "n mono q-size", gib(r.bytes)));
          /* CRF: the encoding row shows the encoder's ACTUAL value (the ladder
         may have stepped it down) and is READ-ONLY -- -q is fixed for the
         next several hours, so an editable control there would be a promise
         nothing can keep. Every other unskipped row is a PICKER: the driver
         asks `smeltr crf` for this exact value before it spawns HandBrake,
         so the choice holds whether the encode is started here or by the
         driver hours from now. Skipped rows will not encode, so no number is
         claimed. */
          var lc = liveCrf[r.title.toLowerCase()];
          var crfTd;
          if (r.skipped) {
            crfTd = el("td", "n q-crf", "—");
          } else if (r.encoding) {
            /* The scale comes from the encoder that is ACTUALLY running, not
             from the row's plan: a CQ printed as a bare number reads as a
             near-lossless CRF, one column from a size that says otherwise. */
            var le = lc ? lc.enc : r.enc;
            crfTd = el("td", "n q-crf", qLabel(le) + " " + (lc ? lc.q : r.crf));
            crfTd.title = "The " + qLabel(le) + " this encode is actually running at";
          } else {
            crfTd = el("td", "n q-crf");
            crfTd.appendChild(crfPicker(r, s));
          }
          tr.appendChild(crfTd);
          var titleTd = el("td", "title-cell");
          var cell = el("div", "tcell");
          var name = el("span", "tname", r.title);
          if (r.skipped) name.className = "tname struck";
          /* Ladder-exhausted ERROR state (operator's rule 2026-08-31): red
         title + error emoji, never struck through and never hidden -- the
         pipeline moved on but a human still owes this row a decision. The
         emoji is textContent like everything else (titles are filesystem
         strings; rule 2 stands). */
          if (r.error) {
            name.className = "tname err";
            var em = el("span", "err-emoji", "❗");
            em.setAttribute("role", "img");
            em.setAttribute("aria-label", "error");
            em.title = r.error_note || EMPTY_ERROR_NOTE;
            cell.appendChild(em);
          }
          cell.appendChild(name);
          cell.appendChild(rowActions(r, s));
          titleTd.appendChild(cell);
          tr.appendChild(titleTd);
          var nasTd = el("td");
          nasTd.appendChild(nasMark(r.location));
          tr.appendChild(nasTd);
          tr.appendChild(srcDirTd(r.src_dir, r.title, r.location));
          var td = el("td");
          if (r.skipped) td.appendChild(el("span", "mark skip", "skipped"));
          else {
            if (r.pinned) td.appendChild(el("span", "mark pin", "pinned"));
            /* Hand-added: this title is not in the library index, so the driver
             cannot resolve a library original for it. It encodes normally and
             then STOPS -- error state, output left beside the source, nothing
             synced and nothing deleted. Without this mark the row is
             indistinguishable from a staged title that will sync itself, and a
             person would wait for a push that is never coming. */
            if (r.manual) {
              var man = el("span", "mark manual", "manual");
              man.title =
                "Added by hand, not from the library index. It will encode and " +
                "then be left in place for you to move — nothing is synced or deleted.";
              td.appendChild(man);
            }
            if (r.encoding) td.appendChild(el("span", "mark enc", "encoding"));
            else if (r.arriving_bytes != null) {
              /* Cell stays empty on purpose: the arrival rides a full-width row
             of its own below, the same pair History gives a push — an 84px
             bar and its caption crammed in this cell ran off the table's
             right edge (operator's call 2026-09-01). */
            } else if (r.staged) {
              td.appendChild(el("span", "mark staged", "staged"));
              /* "next…" is the ONE row next_title.py would pick — computed
             server-side (next_up), even while an encode runs. Rank cannot
             say this: library-only rows outrank staged ones constantly, so
             rank 1 is usually NOT the next encode. It sits BESIDE "staged",
             not instead of it: location fact and schedule fact are
             different columns of meaning. The wording carries the timing —
             "now" vs after a multi-hour encode vs not-until-resumed. */
              if (r.next_up) {
                if (s && s.paused)
                  td.appendChild(el("span", "mark next paused", "next after resume"));
                else if (s && s.low_space)
                  td.appendChild(el("span", "mark next paused", "next once space is freed"));
                else if (live && live.length)
                  td.appendChild(el("span", "mark next", "next after current"));
                else td.appendChild(el("span", "mark next", "next up"));
              }
            } else {
              td.appendChild(el("span", "mark", "library"));
              /* Queued is INTENT, and the position is the whole point of the
             feature — a queue that does not say where you are in it is just
             a button that did nothing. Only the head carries a reason,
             because only the head can be the one being held up. */
              if (r.stage_queued != null) {
                td.appendChild(el("span", "mark queued", "queued " + r.stage_queued));
                if (r.stage_wait) td.appendChild(el("span", "xfer-pct", r.stage_wait));
              }
            }
          }
          tr.appendChild(td);
          if (r.pinned && !r.skipped && ranks[i] === pinned && pinned < active)
            tr.classList.add("pin-end");
          if (!r.skipped && !r.encoding && r.arriving_bytes != null) {
            /* The arriving pull gets a SECOND row spanning every column —
           mark, bar and caption drawn by updateProgress() into the slot,
           exactly the History transfer pair. The xrow carries no dataset,
           so wireDrag ignores it and drop indices stay q-indices. */
            tr.classList.add("rowmoving");
            var xtr = el("tr", "xrow"),
              xtd = el("td");
            xtd.colSpan = 9;
            progSlot("arr|" + r.title.toLowerCase(), xtd);
            xtr.appendChild(xtd);
            return [tr, xtr];
          }
          return tr;
        },
      ),
    );
    /* A recorded title has LEFT the queue — its push renders on the History
     tab only, as the full-width row under its ledger row. It used to get a
     synthetic "transferring" row up top here as well; the operator's call
     (2026-09-01) is that a row on this tab reads as work still waiting to
     encode, which a recorded title is not. */
    wireDrag(pane.querySelector("table"), q);
  }

  /* Drag semantics: dropping a row at position K pins the first K+1 visible
   titles as the explicit head of the queue, so the table always encodes in
   exactly the order shown. Everything below the pinned head keeps the
   bitrate ranking. "Reset order" clears the head. */
  var drag = null;
  function wireDrag(tbl, q) {
    var tb = tbl.tBodies[0];
    function clearMarks() {
      Array.prototype.forEach.call(tb.rows, function (r) {
        r.classList.remove("dropline", "dropline-after");
      });
    }
    function rowOf(ev) {
      var n = ev.target;
      while (n && n.nodeName !== "TR") n = n.parentNode;
      return n && n.dataset && n.dataset.title != null ? n : null;
    }
    tb.addEventListener("dragstart", function (ev) {
      var tr = rowOf(ev);
      if (!tr || !tr.draggable) return;
      drag = { title: tr.dataset.title };
      tr.classList.add("dragsrc");
      ev.dataTransfer.effectAllowed = "move";
      try {
        ev.dataTransfer.setData("text/plain", tr.dataset.title);
      } catch (e) {}
    });
    tb.addEventListener("dragend", function (ev) {
      clearMarks();
      var tr = rowOf(ev);
      if (tr) tr.classList.remove("dragsrc");
      drag = null;
      if (last.pending) {
        last.pending = false;
        last.key = null;
        if (last.state) paint(last.state);
      }
    });
    tb.addEventListener("dragover", function (ev) {
      if (!drag) return;
      var tr = rowOf(ev);
      if (!tr || tr.dataset.title === drag.title) return;
      var i = parseInt(tr.dataset.idx, 10);
      if (q[i] && q[i].skipped) return; // no dropping into the skipped zone
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      clearMarks();
      var rect = tr.getBoundingClientRect();
      tr.classList.add(ev.clientY > rect.top + rect.height / 2 ? "dropline-after" : "dropline");
    });
    tb.addEventListener("drop", function (ev) {
      if (!drag) return;
      ev.preventDefault();
      var tr = rowOf(ev);
      clearMarks();
      if (!tr) return;
      var ti = parseInt(tr.dataset.idx, 10);
      if (!q[ti] || q[ti].skipped) return;
      var rect = tr.getBoundingClientRect();
      var after = ev.clientY > rect.top + rect.height / 2;
      /* Skipped rows sit inline in the table, so a q index is not an index
       into the active sequence; count the active rows above the drop. */
      var target = 0;
      for (var k = 0; k < ti; k++) if (!q[k].skipped) target++;
      if (after) target++;
      var order = q
        .filter(function (r) {
          return !r.skipped;
        })
        .map(function (r) {
          return r.title;
        });
      var from = order.indexOf(drag.title);
      if (from < 0) return;
      if (target > from) target--;
      order.splice(from, 1);
      if (target > order.length) target = order.length;
      order.splice(target, 0, drag.title);
      /* Pin the MINIMAL head that reproduces this exact visible order under
       the server sort (priority first, then bitrate): the longest tail that
       is already in descending-bitrate order needs no pinning. Dropping a
       row back into pure bitrate order therefore clears the pins. */
      var mb = {};
      q.forEach(function (r) {
        mb[r.title] = r.mbps;
      });
      var cut = order.length - 1;
      while (cut > 0 && mb[order[cut - 1]] >= mb[order[cut]]) cut--;
      api("/api/queue/order", { order: order.slice(0, cut) });
    });
  }

  function renderLedger(rows, xfers) {
    progRefs = {};
    var pane = document.getElementById("pane");
    pane.replaceChildren();
    if (!rows.length) {
      pane.appendChild(el("div", "empty", "No encodes recorded yet."));
      return;
    }
    /* "Moved to" is written at record time -- a promise, not an observation.
     While the file is still travelling, show the observed transfer instead
     of presenting the promise as done. */
    var moving = {};
    (xfers || []).forEach(function (t) {
      moving[t.title] = t;
    });
    var ordered = rows.slice().reverse();
    pane.appendChild(
      table(
        [
          { label: "#", n: true },
          { label: "Title", cls: "title-cell" },
          { label: "Original", n: true },
          { label: "Output", n: true },
          { label: "Saved", n: true },
          { label: "Shrink", n: true },
          { label: "Quality", n: true },
          { label: "Tracks" },
          { label: "NAS" },
          { label: "Moved to" },
          { label: "Finished" },
        ],
        ordered,
        function (r, i) {
          var tr = el("tr");
          tr.appendChild(el("td", "n muted", String(ordered.length - i)));
          tr.appendChild(el("td", "title-cell", r.title));
          var ap = r.exact === false ? "~" : "";
          tr.appendChild(el("td", "n", r.source_bytes ? ap + gib(r.source_bytes) : "—"));
          tr.appendChild(el("td", "n", r.output_bytes ? ap + gib(r.output_bytes) : "—"));
          tr.appendChild(el("td", "n", r.saved_bytes ? ap + gib(r.saved_bytes) : "—"));
          tr.appendChild(el("td", "n", pct(r.saved_pct)));
          /* A recorded VT row prints "CQ 60", never a bare 60: this column sits
         beside the size of an original that was DELETED on the strength of
         it, and 60 read as a CRF says the opposite of what it means. */
          var qtd = el(
            "td",
            "n" + (r.crf == null ? " muted" : ""),
            r.crf == null ? "—" : qLabel(r.encoder) + " " + r.crf,
          );
          if (r.crf != null)
            qtd.title =
              r.encoder && r.encoder.indexOf("vt") === 0
                ? "VideoToolbox CQ this encode was recorded at " +
                  "(Apple's reversed scale; not a CRF)"
                : "CRF this encode was recorded at";
          tr.appendChild(qtd);
          tr.appendChild(
            el("td", "muted", r.audio == null ? "—" : r.audio + "a / " + r.subs + "s"),
          );
          /* NAS and folder are two columns, mirroring the queue tab. dest
         records volume and bucket but NOT the library root; the root pill
         comes from source_path where the ledger holds one — sync replaces
         the original in its own library folder, so that dir IS the
         destination (it already fed the tooltip). A dest-only row keeps
         its bucket pill; an unknown volume ("?") gets no pill on either
         half. */
          var nasTd = el("td"),
            destTd;
          if (r.dest) {
            var vol = r.dest.split("/")[0];
            nasTd.appendChild(nasMark(vol));
            var rest = r.dest.slice(vol.length).replace(/^\//, "");
            if (r.source_path && vol && vol !== "?")
              destTd = srcDirTd(r.source_path.replace(/\/[^/]*$/, ""), r.title, vol);
            else {
              destTd = el("td");
              if (rest && vol && vol !== "?")
                destTd.appendChild(el("span", "mark bucket " + nasClass(vol), rest));
              else if (rest) destTd.appendChild(el("span", "muted", "/" + rest));
              else destTd.appendChild(el("span", "muted", "—"));
            }
          } else if (r.kept) {
            /* Kept in place under the no-delete policy: encoded, recorded,
             nothing moved. Named, never a dash that reads as "unknown". */
            nasTd.appendChild(el("span", "mark done", "kept"));
            destTd = el("td");
            var kp = el("span", "muted", "on the X9, beside its source");
            kp.title = "No-delete policy: nothing was synced or deleted. Move it by hand.";
            destTd.appendChild(kp);
          } else {
            nasTd.appendChild(el("span", "muted", "—"));
            destTd = el("td");
            destTd.appendChild(el("span", "muted", "—"));
          }
          tr.appendChild(nasTd);
          tr.appendChild(destTd);
          /* Date AND time — "2026-08-31" alone could not answer "when did this
         one actually land". Format is the operator's pick (2026-09-01):
         MM-DD-YY h:mm am/pm, minutes precision. finished_at still records
         seconds; the row tooltip is not asked to repeat them. The eight
         rows hand-migrated from the old state file carry no timestamp at
         all and still say "—": a missing time is never back-filled from
         the file's mtime. */
          var fin = el("td", "muted nowrap");
          if (r.finished_at) {
            var fa = r.finished_at;
            fin.appendChild(
              el("span", null, fa.slice(5, 7) + "-" + fa.slice(8, 10) + "-" + fa.slice(2, 4)),
            );
            var hm = fa.slice(11, 16);
            /* A REAL space in the text, not just the margin: the cell is copied
           and read aloud as its textContent, and a CSS gap alone yielded
           "2026-08-3109:27" to both. */
            if (hm) fin.appendChild(el("span", "fin-t", " " + clock12(hm)));
          } else fin.appendChild(el("span", null, "—"));
          tr.appendChild(fin);
          /* The "Source of record" COLUMN is gone, not the disclosure. Every row
         that was not measured live already announces itself in the numbers
         themselves -- "~" on every approximated figure (exact===false) and
         "—" where a size is unrecoverable -- so the column repeated in words
         what the digits already said, in the widest cell of the table. The
         provenance still rides in the row tooltip, and summary() still
         excludes an unrecoverable original from every total rather than
         back-solving it. */
          var tips = [];
          if (r.note) tips.push(r.note);
          if (r.provenance && r.provenance !== "live")
            tips.push("source of record: " + (RECORD[r.provenance] || r.provenance));
          if (tips.length) tr.title = tips.join(" · ");
          if (!moving[r.title]) return tr;
          /* A push still in flight gets a SECOND row of its own, spanning every
         column. Squeezed into the "Moved to" cell it had an 84px bar and a
         caption that ran off the right edge of the table -- the one row on
         the page whose numbers move was the one with no room. The destination
         stays on the ledger row above (the pills), so the caption still does
         not repeat it. */
          tr.classList.add("rowmoving");
          var xtr = el("tr", "xrow"),
            xtd = el("td");
          xtd.colSpan = 11;
          progSlot("led|" + r.title, xtd);
          xtr.appendChild(xtd);
          return [tr, xtr];
        },
      ),
    );
    var noted = [];
    ordered.forEach(function (r, i) {
      if (r.note) noted.push(ordered.length - i + ". " + r.title + " — " + r.note);
    });
    if (noted.length) {
      var box = el("div", "lnotes");
      noted.forEach(function (t) {
        box.appendChild(el("div", null, t));
      });
      pane.appendChild(box);
    }
  }

  /* ---- the Errors tab -------------------------------------------------------
   Titles that will NOT encode until a person does something about them. Two
   kinds, and the tab keeps them apart because the remedies are different:

     ERROR   the pipeline gave up on this title and moved on -- a
             $X9/.error-<title> marker written by .autopilot.sh when the band
             ladder ran out of rungs, or when a verdict needed a human. It
             ends when someone deletes that file, which is deliberately NOT a
             button here: un-erroring re-arms an unattended ~90 GiB deletion.
     SKIPPED your click on the Queue tab. "restore" undoes it, right here.

   They used to sit in place in the queue, greyed, on the reasoning that a
   skip which vanished would read as "finished". That reasoning holds and is
   why this tab is COUNTED IN ITS LABEL and never hidden when empty-ish --
   but four dead rows scattered through 130 live ones is not visibility, and
   since the queue's rank column became a running order (2026-09-06) a row
   that will never run cannot sit inside it. */
  /* The driver writes each marker as "<title>: <what happened>". That is the
   right shape for a bare file on the drive and the wrong one in a table whose
   previous column is the title — so the echo comes off here rather than being
   read twice on every row. */
  /* Mirrors core.EMPTY_ERROR_NOTE: an empty marker is a write the driver
   could not make (a full drive), never a guess about what went wrong. */
  var EMPTY_ERROR_NOTE =
    "error marker is empty - the driver could not write the reason " +
    "(the staging drive was almost certainly full)";
  function trimNote(note, title) {
    note = (note || EMPTY_ERROR_NOTE).trim();
    var lead = title + ":";
    return note.slice(0, lead.length).toLowerCase() === lead.toLowerCase()
      ? note.slice(lead.length).trim()
      : note;
  }

  function eShape(r) {
    return [
      r.title,
      r.mbps,
      r.bytes,
      r.location,
      r.src_dir,
      !!r.error,
      r.error_note || null,
      !!r.done,
      r.done_note || null,
      !!r.skipped,
    ];
  }

  function renderErrors(rows) {
    var pane = document.getElementById("pane");
    pane.replaceChildren();
    if (!rows.length) {
      pane.appendChild(el("div", "empty", "Nothing set aside — no errored titles, no hand-skips."));
      return;
    }
    pane.appendChild(
      table(
        [
          { label: "State" },
          { label: "SRC Mb/s", n: true, cls: "unit" },
          { label: "Src size", n: true },
          { label: "Title", cls: "title-cell" },
          { label: "NAS" },
          { label: "Src folder" },
          { label: "What happened" },
        ],
        rows,
        function (r) {
          /* Done rows never reach this table: server.py sets them aside for
           the History tab (their ledger row), so the only shapes here are
           errored and skipped. */
          var tr = el("tr", r.error ? "rowerr" : "rowskip");
          tr.dataset.title = r.title;
          var st = el("td");
          /* Both chips when a title is both. The error one comes first: a
           skipped row that ALSO errored is still a title the pipeline could
           not finish, and reading only "skipped" there would credit the
           operator with a decision the pipeline actually made. */
          if (r.error) st.appendChild(el("span", "mark err", "error"));
          if (r.skipped) st.appendChild(el("span", "mark skip", "skipped"));
          tr.appendChild(st);
          var band = r.mbps >= 90 ? "mbps-hi" : r.mbps >= 80 ? "mbps-mid" : "mbps-lo";
          tr.appendChild(el("td", "n mono q-mbps " + band, r.mbps.toFixed(1)));
          tr.appendChild(el("td", "n mono q-size", gib(r.bytes)));
          var titleTd = el("td", "title-cell");
          var cell = el("div", "tcell");
          var name = el("span", "tname" + (r.error ? " err" : r.skipped ? " struck" : ""), r.title);
          if (r.error) {
            var em = el("span", "err-emoji", "❗");
            em.setAttribute("role", "img");
            em.setAttribute("aria-label", "error");
            cell.appendChild(em);
          }
          cell.appendChild(name);
          cell.appendChild(rowActions(r, null));
          titleTd.appendChild(cell);
          tr.appendChild(titleTd);
          var nasTd = el("td");
          nasTd.appendChild(nasMark(r.location));
          tr.appendChild(nasTd);
          tr.appendChild(srcDirTd(r.src_dir, r.title, r.location));
          /* The marker's own first line, verbatim — it names the failure and
           carries the timestamp the driver wrote. A hand-skip has no note
           and says so plainly rather than borrowing the error column's
           vocabulary for a thing that is not a failure. */
          var why = el(
            "td",
            "err-note",
            r.error
              ? trimNote(r.error_note, r.title)
              : r.done
                ? trimNote(r.done_note, r.title)
                : "Skipped from the Queue tab — nothing wrong with it.",
          );
          if (r.error)
            why.title =
              "The pipeline moved on. Delete the .error-" +
              r.title +
              " marker file on the staging drive to put this title back in play.";
          else if (r.done)
            why.title =
              "Encoded and kept beside its source (no-delete policy). Move the file " +
              "by hand, then delete the .done-" +
              r.title +
              " marker on the staging drive.";
          tr.appendChild(why);
          return tr;
        },
      ),
    );
  }

  /* ---- the Processes tab ----------------------------------------------------
   Every smeltr process on this Mac with what it is FOR, plus the
   LaunchAgents that start things when nothing is running. Fetched from
   /api/processes on its own clock while the tab is open. A missing detail
   (no title in a command line) renders as an em dash, never a guess. */
  var PROC_EVERY = 5000;
  var prData = null,
    prErr = false,
    prFetching = false,
    prFetchedAt = 0,
    prKey = "";
  function fetchProcs() {
    if (prFetching) return;
    prFetching = true;
    fetch("/api/processes" + (token ? "?t=" + encodeURIComponent(token) : ""))
      .then(function (r) {
        if (!r.ok) throw 0;
        return r.json();
      })
      .then(function (j) {
        prFetching = false;
        prErr = false;
        prData = j;
        /* The key is the SHAPE of the list -- pids, kinds, details -- so a
         changed cpu% alone never rebuilds the table. */
        prKey =
          JSON.stringify(
            j.procs.map(function (p) {
              return [p.pid, p.kind, p.detail, p.dup];
            }),
          ) + JSON.stringify(j.agents);
        if (tab === "procs" && last.state) paint(last.state);
      })
      .catch(function () {
        prFetching = false;
        prErr = true;
        prKey = "err" + Date.now();
        if (tab === "procs" && last.state) {
          last.key = null;
          paint(last.state);
        }
      });
  }
  /* Kind -> chip colour. Movers are good (something is happening), the
   driver and its guardians are plain, a duplicate driver is bad. */
  var PR_CLS = {
    encode: "good",
    pull: "good",
    push: "good",
    sync: "warn",
    sweeper: "warn",
  };
  function renderProcs() {
    progRefs = {};
    var pane = document.getElementById("pane");
    pane.replaceChildren();
    if (prData === null) {
      pane.appendChild(
        el("div", "empty", prErr ? "Could not list processes — retrying" : "Listing processes…"),
      );
      return;
    }
    var procs = prData.procs || [];
    if (!procs.length) {
      pane.appendChild(el("div", "empty", "Nothing of smeltr's is running on this Mac."));
    } else {
      pane.appendChild(
        table(
          [
            { label: "Process" },
            { label: "PID", n: true },
            { label: "Purpose" },
            { label: "Working on" },
            { label: "Running for", n: true },
            { label: "CPU %", n: true, cls: "unit" },
          ],
          procs,
          function (p) {
            var tr = el("tr", p.dup ? "rowerr" : null);
            var td = el("td");
            var cls = p.dup ? "bad" : PR_CLS[p.kind] || "";
            var chip = el("span", "mark" + (cls ? " ev-" + cls : ""), p.label);
            chip.title = p.cmd;
            td.appendChild(chip);
            if (p.dup)
              td.appendChild(
                el("span", "evtext", "two drivers — the lock should make this impossible"),
              );
            tr.appendChild(td);
            tr.appendChild(el("td", "n mono", String(p.pid)));
            tr.appendChild(el("td", "muted", p.purpose));
            tr.appendChild(el("td", "title-cell", p.detail || "—"));
            tr.appendChild(el("td", "n mono", p.elapsed));
            tr.appendChild(el("td", "n mono", p.cpu));
            return tr;
          },
        ),
      );
    }
    var agents = prData.agents || [];
    var h = el("div", "evfoot");
    h.appendChild(
      el(
        "span",
        null,
        agents.length
          ? "Scheduled by launchd — these start things when nothing is running:"
          : "No smeltr LaunchAgents are loaded on this Mac.",
      ),
    );
    pane.appendChild(h);
    if (agents.length) {
      pane.appendChild(
        table([{ label: "Agent" }, { label: "Purpose" }, { label: "State" }], agents, function (a) {
          var tr = el("tr");
          tr.appendChild(el("td", "mono", a.label));
          tr.appendChild(el("td", "muted", a.purpose));
          /* launchctl's third column is the pid while a run is up, else
             the LAST EXIT STATUS: 0 is "ran and finished", anything else
             is the last run failing and is said so. */
          var st = a.pid
            ? "running (pid " + a.pid + ")"
            : a.status === "0"
              ? "loaded · last run ok"
              : "loaded · last run exited " + a.status;
          tr.appendChild(el("td", a.pid || a.status === "0" ? null : "err", st));
          return tr;
        }),
      );
    }
    pane.appendChild(el("div", "evfoot", "snapshot " + clock12(prData.generated_at || "")));
  }

  /* ---- the Events tab -------------------------------------------------------
   The driver/watcher timeline, for debugging: every stamped line of
   .autopilot.log plus the band ladder's KILLED/COMPLETE verdicts, newest
   first. The timeline does NOT ride the 2 s SSE frames — the state payload
   carries only events_rev (log mtimes), and the page refetches /api/events
   when that moves while this tab is open. A row with no time really has
   none (an old KILLED line in a re-used watch log); it is never guessed. */
  var evData = null,
    evRev = null,
    evTotal = 0,
    evFetching = false,
    evFetchedFor = null,
    evErr = false;
  function fetchEvents() {
    if (evFetching) return;
    evFetching = true;
    fetch("/api/events" + (token ? "?t=" + encodeURIComponent(token) : ""))
      .then(function (r) {
        if (!r.ok) throw 0;
        return r.json();
      })
      .then(function (j) {
        evFetching = false;
        evErr = false;
        evData = j.events;
        evRev = j.rev;
        evTotal = j.total || j.events.length;
        if (tab === "events" && last.state) paint(last.state);
      })
      .catch(function () {
        /* A dropped request must not freeze the tab on "Loading events…"
         forever: clearing evFetchedFor lets the next 2 s frame retry, and
         evErr puts the failure on screen instead of a spinner. */
        evFetching = false;
        evFetchedFor = null;
        evErr = true;
        if (tab === "events" && last.state) {
          last.key = null;
          paint(last.state);
        }
      });
  }
  /* Severity only — an unlisted kind renders as a plain pill, never an error.
   "exhausted" and "failed" are deliberately distinct labels from "killed":
   a routine ladder retry heals itself; those two need a human.
   "lastrung" is the end of the ladder (2026-09-06): the encode was NOT
   killed, it is still running and heading out of band. It is "bad", not
   "warn", even though nothing has been lost yet — on the too-small arm the
   file it is about to finish can be a `good` verdict (15-30% of source is
   below the band but above the floor), which syncs and DELETES the library
   original. Its predecessor `exhausted` was red and ended with the original
   safe; this ends with the original at risk, so the colour may not fall. */
  /* Chip wording where the kind itself is not a phrase a person reads.
   Anything unlisted renders as its bare kind, which is how every other one
   already read. */
  var EV_LABEL = { up: "driver up", lastrung: "last rung" };
  var EV_CLS = {
    halted: "bad",
    killed: "bad",
    failed: "bad",
    exhausted: "bad",
    lastrung: "bad",
    stale: "warn",
    defer: "warn",
    cycle: "good",
    complete: "good",
    done: "good",
  };
  function renderEvents(x9on) {
    progRefs = {};
    var pane = document.getElementById("pane");
    pane.replaceChildren();
    if (evData === null) {
      pane.appendChild(
        el(
          "div",
          "empty",
          evErr ? "Could not load events — retrying on the next update" : "Loading events…",
        ),
      );
      return;
    }
    if (!evData.length) {
      pane.appendChild(
        el(
          "div",
          "empty",
          x9on === false ? "No events — the staging drive is offline" : "No events recorded yet.",
        ),
      );
      return;
    }
    pane.appendChild(
      table([{ label: "Time" }, { label: "Event" }], evData, function (e) {
        var tr = el("tr");
        var td = el("td", "muted nowrap mono evtime");
        var d = e.ts ? e.ts.slice(5, 7) + "-" + e.ts.slice(8, 10) + "-" + e.ts.slice(2, 4) : null;
        if (!e.ts) {
          td.textContent = "—";
          td.title = "time unknown — an earlier line of a re-used watch log";
        } else if (e.approx) {
          /* Inferred from the watch log's mtime, so it wears the page's
           estimate marker and claims minutes, never seconds. */
          td.textContent = "~" + d + " " + clock12(e.ts.slice(11, 16));
          td.title =
            "time inferred from the watch log's file mtime — " +
            "the watcher wrote this line and exited";
        } else {
          td.textContent = d + " " + clock12(e.ts.slice(11, 19));
        }
        tr.appendChild(td);
        var ev = el("td");
        var cls = EV_CLS[e.kind];
        ev.appendChild(el("span", "mark" + (cls ? " ev-" + cls : ""), EV_LABEL[e.kind] || e.kind));
        ev.appendChild(el("span", "evtext", e.text));
        if (e.count > 1) {
          var c = el("span", "evcount", "× " + e.count);
          c.title =
            "this line repeated " + e.count + " times in a row — " + "newest occurrence shown";
          ev.appendChild(c);
        }
        if (e.detail) ev.appendChild(el("div", "evdetail", e.detail));
        tr.appendChild(ev);
        return tr;
      }),
    );
    if (evTotal > evData.length)
      pane.appendChild(
        el(
          "div",
          "lnotes",
          "newest " +
            evData.length +
            " of " +
            evTotal +
            " events — older history " +
            "stays in .autopilot.log on the staging drive",
        ),
      );
  }

  function paint(s) {
    renderAlert(s.summary, s.encode_note);
    renderStats(s.summary);
    var nextUp = (s.queue || []).filter(function (r) {
      return r.next_up;
    })[0];
    renderLive(s.live, s.summary, s.driver_alive === true, nextUp ? nextUp.title : null, nextUp, {
      encoder_default: s.encoder_default,
      quality_default: s.quality_default,
      x9_total_bytes: s.x9_total_bytes,
    });
    /* The key must cover EVERYTHING the pane's STRUCTURE depends on — and
     nothing more. Transfers are in the LEDGER key only: History is the one
     tab that draws a push (2026-09-01 — the queue's synthetic transferring
     row is gone, so transfers in the queue key would rebuild that table for
     a row it no longer renders). They were once omitted from the key
     entirely, and the transferring row painted a single still frame (at ~0
     bytes) that never advanced for the whole 45-minute push. The fix for
     that put raw byte counts in the key, which swung the bug the other way:
     the key then changed every frame and rebuilt the whole table twice a
     second. Now the key carries only shape (qShape/xShape) and the byte
     counts reach the DOM through updateProgress() below — a bar that moves
     every frame, inside a table that is left alone. */
    /* Refetch the timeline at most once per events_rev move, and only while
     the tab is open — evFetchedFor is the rev a fetch was already started
     for, so a stale state cache can never refetch in a loop. */
    if (tab === "events" && evFetchedFor !== s.events_rev) {
      evFetchedFor = s.events_rev;
      fetchEvents();
    }
    /* The Processes tab polls on its own clock (every PROC_EVERY ms while
     open): a process list has no rev to ride, and `ps` twice a second
     for a tab that changes once a minute would be waste. */
    if (tab === "procs" && Date.now() - prFetchedAt >= PROC_EVERY) {
      prFetchedAt = Date.now();
      fetchProcs();
    }
    /* The queue key carries the transfer COUNT (it decides the empty-state
     sentence), never the transfers themselves — their bytes and shape belong
     to the ledger key alone. */
    var key =
      tab +
      "|" +
      (tab === "procs" ? prKey : "") +
      JSON.stringify(
        tab === "queue"
          ? [
              s.queue.map(qShape),
              s.live.map(function (e) {
                return [e.folder, e.crf, e.encoder];
              }),
              s.summary.library_complete,
              s.summary.roots_offline,
              s.can_start,
              s.encode_note,
              s.summary.paused,
              s.stage_active,
              (s.transfers || []).length,
            ]
          : tab === "ledger"
            ? [s.ledger, (s.transfers || []).map(xShape)]
            : tab === "errors"
              ? [(s.errors || []).map(eShape)]
              : [evRev, evData === null, evErr],
      );
    if (last.key !== key) {
      /* An armed confirm or an active drag must survive the 2s SSE repaint. */
      if (tab === "queue" && (drag || armedTitle !== null || crfOpen !== null)) {
        last.pending = true;
      } else {
        last.key = key;
        tab === "queue"
          ? renderQueue(
              s.queue,
              Object.assign({}, s.summary, {
                can_start: s.can_start,
                crf_choices: s.crf_choices,
                encoder_choices: s.encoder_choices,
                crf_default: s.crf_default,
                /* The picker's "(auto)" names what an untouched row starts on.
                Omitting these two made every auto row read "CRF 14" while the
                driver started VT CQ 70 -- seen live 2026-09-07 00:06. */
                encoder_default: s.encoder_default,
                quality_default: s.quality_default,
                /* A busy wire changes the button's PROMISE from "pull now" to
                "wait in line"; saying "stage" while five titles queue ahead
                would misstate what the click does. */
                stage_busy: s.stage_active != null || (s.stage_queue || []).length > 0,
                xfer_count: (s.transfers || []).length,
              }),
              s.live,
            )
          : tab === "ledger"
            ? renderLedger(s.ledger, s.transfers)
            : tab === "errors"
              ? renderErrors(s.errors || [])
              : tab === "procs"
                ? renderProcs()
                : renderEvents(s.summary.x9_online);
      }
    }
    /* EVERY frame, rebuilt or not: this is what keeps the bars moving now that
     their numbers are out of the key. It runs after a skipped rebuild too —
     an armed confirm or an active drag must not freeze a transfer. */
    updateProgress(s);
    /* The header beacon: on while anything is actually MOVING — an encode, a
     push to the NAS, a staging pull, or an arriving replenish. Toggled every
     frame like the bars, never part of the repaint key. */
    var pd = document.getElementById("pulse");
    if (pd) {
      /* MOVING only. A stalled transfer or arrival is by definition not
       progress, and a stale leftover .partial would otherwise pin the
       beacon on forever while the table below says "stalled". */
      var mvX = (s.transfers || []).some(function (t) {
        return !t.stalled;
      });
      var mvA = (s.queue || []).some(function (r) {
        return r.arriving_bytes != null && !r.arriving_stalled;
      });
      var busy =
        (s.live && s.live.length > 0) || mvX || s.stage_active != null || s.syncing === true || mvA;
      pd.classList.toggle("on", !!busy);
      pd.title = busy
        ? "Work in progress: encode, transfer, staging pull, " +
          "arrival, or a sync replacing a library original"
        : "";
    }
    /* The queue count is summary()'s, which already excludes skipped rows —
     and now excludes nothing else, because the rows this tab no longer shows
     are exactly the ones it counted separately. The "· N skipped" suffix
     that used to ride here has moved to the Errors tab's own label, where
     the rows it counts actually are. */
    var nq = s.summary.queue_count != null ? s.summary.queue_count : s.queue.length;
    document.getElementById("tabQueue").textContent = "Queue (" + nq + ")";
    /* Named for the serious half. A tab reading "Errors (0)" while three
     titles sit skipped would be wrong, so the label counts BOTH and the
     breakdown rides behind it whenever the two differ. */
    var errs = s.errors || [];
    var nerr = errs.filter(function (r) {
      return r.error;
    }).length;
    document.getElementById("tabErrors").textContent =
      "Errors (" +
      errs.length +
      (nerr && nerr !== errs.length ? " · " + nerr + " errored" : "") +
      ")";
    document.getElementById("tabErrors").classList.toggle("hasErr", nerr > 0);
    document.getElementById("tabLedger").textContent = "History (" + s.ledger.length + ")";
    /* "(250 of 266)" — a bare "(250)" read as a count of everything that
     exists, while both a row limit and the log-tail window cut it. */
    document.getElementById("tabEvents").textContent =
      "Events" +
      (evData
        ? " (" + (evTotal > evData.length ? evData.length + " of " + evTotal : evData.length) + ")"
        : "");
    document.getElementById("tabProcs").textContent =
      "Processes" + (prData ? " (" + prData.procs.length + ")" : "");
    var anyPin = s.queue.some(function (r) {
      return r.pinned && !r.skipped;
    });
    document.getElementById("resetOrder").hidden = !(tab === "queue" && anyPin);
    document.getElementById("gen").textContent = "updated " + clock12(s.summary.generated_at);
    document.getElementById("stopnote").textContent =
      "pausing below " + s.summary.stop_mbps + " Mb/s";
  }

  /* The selected tab lives in the URL (?tab=queue|ledger|errors|events) and
   the URL is the state: a refresh, a self-reload after a server restart, or
   a shared link all land on the same tab. replaceState, not pushState -- a
   tab switch is not a page the back button should walk through. The token
   and anything else in the query survive untouched. */
  var TABS = { queue: 1, ledger: 1, errors: 1, events: 1, procs: 1 };
  function tabFromUrl() {
    try {
      var v = new URLSearchParams(location.search).get("tab");
      return v && TABS[v] ? v : null;
    } catch (_) {
      return null;
    }
  }
  function tabToUrl(name) {
    try {
      var u = new URL(location.href);
      u.searchParams.set("tab", name);
      history.replaceState(null, "", u.toString());
    } catch (_) {}
  }
  function setTab(name) {
    tab = name;
    tabToUrl(name);
    last.key = null;
    var tw = document.getElementById("tablewrap");
    if (tw && tw.classList.contains("collapsed") && tableFold) tableFold.click();
    document.getElementById("tabQueue").setAttribute("aria-selected", String(name === "queue"));
    document.getElementById("tabLedger").setAttribute("aria-selected", String(name === "ledger"));
    document.getElementById("tabErrors").setAttribute("aria-selected", String(name === "errors"));
    document.getElementById("tabEvents").setAttribute("aria-selected", String(name === "events"));
    document.getElementById("tabProcs").setAttribute("aria-selected", String(name === "procs"));
    /* Opening the tab fetches now, not at the next poll boundary. */
    if (name === "procs") prFetchedAt = 0;
    if (last.state) paint(last.state);
  }
  document.getElementById("tabQueue").addEventListener("click", function () {
    setTab("queue");
  });
  document.getElementById("tabLedger").addEventListener("click", function () {
    setTab("ledger");
  });
  document.getElementById("tabErrors").addEventListener("click", function () {
    setTab("errors");
  });
  document.getElementById("tabEvents").addEventListener("click", function () {
    setTab("events");
  });
  document.getElementById("tabProcs").addEventListener("click", function () {
    setTab("procs");
  });
  document.getElementById("resetOrder").addEventListener("click", function () {
    api("/api/queue/order", { order: [] });
  });
  /* Restore the tab the URL names before the first frame paints. */
  (function () {
    var t0 = tabFromUrl();
    if (t0 && t0 !== tab) setTab(t0);
  })();

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
  (function () {
    var pane = document.getElementById("pane"),
      cuts = [],
      pend = false;
    function applyCut(tbl, i, on) {
      for (var r = 0; r < tbl.rows.length; r++) {
        /* A full-width transfer row has ONE cell spanning every column; cells[0]
         there is the whole row, not column 0. Blanking it would erase the
         transfer instead of a sliced number. */
        if (tbl.rows[r].classList.contains("xrow")) continue;
        var c = tbl.rows[r].cells[i];
        if (c) c.classList.toggle("cut", on);
      }
    }
    function recut() {
      var tbl = pane.querySelector("table");
      if (!tbl || !tbl.rows.length) {
        cuts = [];
        return;
      }
      var head = tbl.rows[0],
        styles = [],
        sticky = [],
        pinnedRight = null,
        i,
        rc;
      for (i = 0; i < head.cells.length; i++) {
        styles[i] = getComputedStyle(head.cells[i]);
        sticky[i] = styles[i].position === "sticky" && styles[i].left !== "auto";
        if (sticky[i]) {
          rc = head.cells[i].getBoundingClientRect();
          if (pinnedRight == null || rc.right > pinnedRight) pinnedRight = rc.right;
        }
      }
      var next = [];
      if (pinnedRight != null) {
        /* the pinned title exists only below 700px */
        var pr = pane.getBoundingClientRect();
        var bounds = [pr.left, pinnedRight, pr.left + pane.clientWidth];
        for (i = 0; i < head.cells.length; i++) {
          if (sticky[i] || head.cells[i].classList.contains("title-cell")) continue;
          rc = head.cells[i].getBoundingClientRect();
          var L = rc.left + parseFloat(styles[i].paddingLeft),
            R = rc.right - parseFloat(styles[i].paddingRight);
          for (var b = 0; b < bounds.length; b++) {
            if (L < bounds[b] - 1 && R > bounds[b] + 1) {
              next.push(i);
              break;
            }
          }
        }
      }
      cuts.forEach(function (c) {
        if (next.indexOf(c) < 0) applyCut(tbl, c, false);
      });
      next.forEach(function (c) {
        if (cuts.indexOf(c) < 0) applyCut(tbl, c, true);
      });
      cuts = next;
    }
    function schedule() {
      if (pend) return;
      pend = true;
      requestAnimationFrame(function () {
        pend = false;
        recut();
      });
    }
    pane.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    new MutationObserver(function () {
      cuts = [];
      schedule();
    }).observe(pane, { childList: true });
    schedule();
  })();

  /* Theme. Three segments: an explicit light/dark choice is stored and always
   wins; "system" CLEARS the stored choice, so the OS preference wins and
   KEEPS winning -- flipping the system theme mid-session moves the page with
   it. The pressed segment shows the choice, not the resolved colour. */
  var THEME_KEY = "smeltr.theme";
  var mql = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  function storedTheme() {
    try {
      var v = localStorage.getItem(THEME_KEY);
      return v === "light" || v === "dark" ? v : null;
    } catch (e) {
      return null;
    }
  }
  var themeBtns = {
    light: document.getElementById("themeLight"),
    dark: document.getElementById("themeDark"),
    system: document.getElementById("themeSystem"),
  };
  function applyTheme() {
    var mode = storedTheme() || "system";
    var shown = mode === "system" ? (mql && mql.matches ? "light" : "dark") : mode;
    document.documentElement.setAttribute("data-theme", shown);
    for (var k in themeBtns)
      themeBtns[k].setAttribute("aria-pressed", k === mode ? "true" : "false");
  }
  applyTheme();
  if (mql && mql.addEventListener) {
    mql.addEventListener("change", function () {
      if (!storedTheme()) applyTheme();
    });
  }
  ["light", "dark"].forEach(function (name) {
    themeBtns[name].addEventListener("click", function () {
      try {
        localStorage.setItem(THEME_KEY, name);
      } catch (e) {}
      applyTheme();
    });
  });
  themeBtns.system.addEventListener("click", function () {
    try {
      localStorage.removeItem(THEME_KEY);
    } catch (e) {}
    applyTheme();
  });

  function conn(state, text) {
    document.getElementById("dot").className = "dot " + state;
    document.getElementById("connText").textContent = text;
  }

  /* ---- Resource monitor ---------------------------------------------------
   Charts are fed by two sources: a history fetch (raw 32-byte records,
   parsed with a DataView into a client-side ring mirroring the server's),
   then 1 s `mon` SSE frames appended on top. Everything renders from the
   ring, decimated to one bucket per pixel column with a MIN/MAX band plus
   the mean line -- a one-second spike must survive a 7 d window, and
   averaging alone would erase it.

   The fetch is SIZED TO THE VISIBLE WINDOW. The ring is a week deep and
   ~19 MB whole; a phone opening the page on the 24 h stop pulls a day and
   only widens when you drag past what it holds.

   Honesty rules, same as the tables: a second with no sample is a GAP in
   the line, never an interpolation; a metric the sampler could not read
   (GPU on a box with no readable accelerator) is an absent line and an em
   dash, never a flat zero. The whole strip lives OUTSIDE paint() and its
   repaint keys: a chart frame never rebuilds a table.

   The slider SNAPS to a named window rather than sliding along a log
   curve. Thirteen stops, 1 min to 7 d, one per integer position: "3 h" is a
   window you can hold in your head and compare against yesterday's, where
   the old continuous mapping handed out "3.4 h" and made two readings of
   the same chart incomparable. The window is always anchored at now. ---- */
  var MON_SLOTS = 604800,
    MIB = 1048576; /* 7 d, matching sysmon.SLOTS */
  var MON_STOPS = [
    60, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400, 172800, 259200, 432000, 604800,
  ];
  var MON_DEFAULT = 8; /* 24 h -- the stop the page opens on */
  function monSpan(pos) {
    var i = Math.round(pos);
    if (!(i >= 0)) i = 0; /* NaN included */
    if (i > MON_STOPS.length - 1) i = MON_STOPS.length - 1;
    return MON_STOPS[i];
  }
  function spanLabel(sec) {
    if (sec < 3600) return Math.round(sec / 60) + " min";
    if (sec < 86400) {
      var h = sec / 3600;
      return (h >= 9.5 ? Math.round(h) : Math.round(h * 10) / 10) + " h";
    }
    var d = sec / 86400;
    return (d >= 9.5 ? Math.round(d) : Math.round(d * 10) / 10) + " d";
  }
  function niceMax(v) {
    if (!(v > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log(v) / Math.LN10)),
      m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }
  function monTicks(span) {
    return span <= 60
      ? 10
      : span <= 1800
        ? 300
        : span <= 3600
          ? 600
          : span <= 7200
            ? 900
            : span <= 14400
              ? 1800
              : span <= 28800
                ? 3600
                : span <= 43200
                  ? 7200
                  : span <= 86400
                    ? 10800
                    : span <= 172800
                      ? 21600
                      : span <= 259200
                        ? 43200
                        : 86400;
  }
  function monBuckets(tsArr, valArr, t0, t1, cols) {
    var lo = new Float64Array(cols),
      hi = new Float64Array(cols),
      sum = new Float64Array(cols),
      n = new Int32Array(cols),
      c,
      s,
      i,
      v;
    for (c = 0; c < cols; c++) {
      lo[c] = Infinity;
      hi[c] = -Infinity;
    }
    var span = t1 - t0;
    for (s = t0; s < t1; s++) {
      i = s % MON_SLOTS;
      if (tsArr[i] !== s) continue; /* empty or >7 d stale slot */
      v = valArr[i];
      if (v !== v) continue; /* NaN: metric unreadable that second */
      c = (((s - t0) * cols) / span) | 0;
      if (c >= cols) c = cols - 1;
      if (v < lo[c]) lo[c] = v;
      if (v > hi[c]) hi[c] = v;
      sum[c] += v;
      n[c]++;
    }
    var avg = new Float64Array(cols);
    for (c = 0; c < cols; c++) {
      if (n[c]) avg[c] = sum[c] / n[c];
      else {
        avg[c] = NaN;
        lo[c] = NaN;
        hi[c] = NaN;
      }
    }
    return { lo: lo, hi: hi, avg: avg, n: n };
  }
  /* The drawn mean line is lightly smoothed -- a weighted average over the two
   buckets either side -- purely so 1 Hz noise reads as a curve instead of
   sawteeth. Honesty rules, in force:
   - drawMon only smooths where the min-max shade is REAL (4+ samples per
     bucket). With ~one sample per bucket the shade collapses to a 1 px rect
     at 16% alpha, the line is the only evidence on screen, and averaging it
     redrew a measured 98 MiB/s burst at 33 -- so the narrow stops draw the
     raw mean.
   - the window is SYMMETRIC: the radius is the smaller of what the two
     sides hold, stopping at a gap or the array edge. An asymmetric window
     drags the value sideways -- a trailing-only average at the right edge
     drew a CPU that had just pinned at 92% as 50% -- and it means the tip
     of the line and the point beside a gap are always the raw bucket mean.
   - a bucket with no samples stays NaN, so the path still breaks and no
     smoothed segment ever bridges a sampler outage.
   The band, legend and tooltip stay raw; the header discloses the rest. */
  var MON_SMOOTH_W = [3, 2, 1];
  function monSmooth(avg, cols) {
    var R = MON_SMOOTH_W.length - 1,
      out = new Float64Array(cols),
      c,
      d,
      v,
      s,
      n,
      la,
      ra,
      r;
    for (c = 0; c < cols; c++) {
      v = avg[c];
      if (v !== v) {
        out[c] = NaN;
        continue;
      }
      la = 0;
      while (la < R && c - la - 1 >= 0 && avg[c - la - 1] === avg[c - la - 1]) la++;
      ra = 0;
      while (ra < R && c + ra + 1 < cols && avg[c + ra + 1] === avg[c + ra + 1]) ra++;
      r = Math.min(la, ra);
      s = v * MON_SMOOTH_W[0];
      n = MON_SMOOTH_W[0];
      for (d = 1; d <= r; d++) {
        s += (avg[c - d] + avg[c + d]) * MON_SMOOTH_W[d];
        n += 2 * MON_SMOOTH_W[d];
      }
      out[c] = s / n;
    }
    return out;
  }
  /* The line is drawn as a CURVE at the 1 min and 15 min stops only
   (operator's pick, 2026-09-11) -- there a bucket is one sample and spans
   whole pixels, so a polyline reads as facets. The curve is a monotone
   cubic (Fritsch-Carlson): it passes through every bucket mean and never
   rises or dips between two of them past either, so a one-second spike
   still peaks at its measured value and nothing is drawn that no sample
   reached. Wider stops keep the straight polyline. A null point is a gap
   and breaks the path; each run traces from its own moveTo. */
  var MON_CURVE_MAX = 900;
  function monTrace(ctx, pts, curved) {
    var runs = [],
      run = [],
      i;
    for (i = 0; i < pts.length; i++) {
      if (pts[i]) run.push(pts[i]);
      else if (run.length) {
        runs.push(run);
        run = [];
      }
    }
    if (run.length) runs.push(run);
    runs.forEach(function (r) {
      var n = r.length,
        k;
      ctx.moveTo(r[0].x, r[0].y);
      if (!curved || n < 3) {
        for (k = 1; k < n; k++) ctx.lineTo(r[k].x, r[k].y);
        return;
      }
      var d = new Float64Array(n - 1),
        m = new Float64Array(n);
      for (k = 0; k < n - 1; k++) d[k] = (r[k + 1].y - r[k].y) / (r[k + 1].x - r[k].x);
      m[0] = d[0];
      m[n - 1] = d[n - 2];
      for (k = 1; k < n - 1; k++) m[k] = d[k - 1] * d[k] <= 0 ? 0 : (d[k - 1] + d[k]) / 2;
      for (k = 0; k < n - 1; k++) {
        if (d[k] === 0) {
          m[k] = 0;
          m[k + 1] = 0;
          continue;
        }
        var a = m[k] / d[k],
          b = m[k + 1] / d[k],
          s = a * a + b * b;
        if (s > 9) {
          var t = 3 / Math.sqrt(s);
          m[k] = t * a * d[k];
          m[k + 1] = t * b * d[k];
        }
      }
      for (k = 0; k < n - 1; k++) {
        var h = r[k + 1].x - r[k].x;
        ctx.bezierCurveTo(
          r[k].x + h / 3,
          r[k].y + (m[k] * h) / 3,
          r[k + 1].x - h / 3,
          r[k + 1].y - (m[k + 1] * h) / 3,
          r[k + 1].x,
          r[k + 1].y,
        );
      }
    });
  }
  function monFmtPct(v) {
    return v == null || v !== v ? "—" : Math.round(v) + "%";
  }
  function monFmtMibs(v) {
    if (v == null || v !== v) return "—";
    var m = v / MIB;
    if (m < 1) {
      /* a live 50 KiB/s trickle must not print as 0 */
      var kb = v / 1024;
      return (kb >= 10 ? Math.round(kb) : Math.round(kb * 10) / 10) + " KiB/s";
    }
    return (m >= 10 ? Math.round(m) : Math.round(m * 10) / 10) + " MiB/s";
  }
  function axLab(v) {
    return v >= 10 ? Math.round(v) : Math.round(v * 10) / 10;
  }
  function hhmm(t) {
    var d = new Date(t * 1000);
    return clock12(
      String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"),
    );
  }
  function hhmmss(t) {
    var d = new Date(t * 1000);
    return clock12(
      String(d.getHours()).padStart(2, "0") +
        ":" +
        String(d.getMinutes()).padStart(2, "0") +
        ":" +
        String(d.getSeconds()).padStart(2, "0"),
    );
  }
  var MON_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  /* Past 24 h the window crosses midnight, and a bare "06:00" on the axis --
   or in the tooltip -- names three different mornings at the 7 d stop.
   Anything not from today carries its weekday. */
  function isToday(t) {
    var d = new Date(t * 1000),
      n = new Date();
    return (
      d.getFullYear() === n.getFullYear() &&
      d.getMonth() === n.getMonth() &&
      d.getDate() === n.getDate()
    );
  }
  function stampLab(t) {
    return isToday(t) ? hhmm(t) : MON_DAYS[new Date(t * 1000).getDay()] + " " + hhmm(t);
  }
  function tickLab(t, span) {
    /* A sub-minute tick step puts several gridlines inside one hh:mm. */
    if (monTicks(span) < 60) return hhmmss(t);
    if (span <= 86400) return hhmm(t);
    var d = new Date(t * 1000),
      day = MON_DAYS[d.getDay()];
    /* At a whole-day tick step the time is always 00:00 and carries nothing. */
    return d.getHours() === 0 && d.getMinutes() === 0 ? day : day + " " + hhmm(t);
  }

  var monTs = new Float64Array(MON_SLOTS),
    monV = [],
    monLast = null,
    monEarliest = null; /* oldest sample held; before it the chart says NO DATA */
  (function () {
    for (var k = 0; k < 7; k++) {
      var a = new Float32Array(MON_SLOTS);
      a.fill(NaN);
      monV.push(a);
    }
  })();

  /* ch-1 = CPU / outbound / writes, ch-2 = GPU / inbound / reads, ch-3 = RAM
   -- identity is carried by the legend and tooltip, never colour alone. */
  var MON_CHARTS = [
    {
      cv: "monU",
      leg: "legU",
      pctAxis: true,
      fmt: monFmtPct,
      scale: 1,
      series: [
        { k: 0, tok: "--ch-1", cls: "ch1", label: "CPU" },
        { k: 1, tok: "--ch-2", cls: "ch2", label: "GPU" },
        { k: 2, tok: "--ch-3", cls: "ch3", label: "RAM" },
      ],
    },
    {
      cv: "monN",
      leg: "legN",
      fmt: monFmtMibs,
      scale: MIB,
      series: [
        { k: 3, tok: "--ch-2", cls: "ch2", label: "in" },
        { k: 4, tok: "--ch-1", cls: "ch1", label: "out" },
      ],
    },
    {
      cv: "monD",
      leg: "legD",
      axis: true,
      fmt: monFmtMibs,
      scale: MIB,
      series: [
        { k: 5, tok: "--ch-2", cls: "ch2", label: "read" },
        { k: 6, tok: "--ch-1", cls: "ch1", label: "write" },
      ],
    },
  ];

  var monCard = document.getElementById("sysmon"),
    monTipEl = document.getElementById("monTip"),
    /* The slider is the one element read back for a VALUE rather than
       written to, so it is annotated: getElementById answers HTMLElement,
       which has no .value, and index.html declares this one an
       <input type="range">. */
    monZoomEl = /** @type {HTMLInputElement} */ (document.getElementById("monZoom")),
    monLblEl = document.getElementById("monSpanLbl"),
    monSinceEl = document.getElementById("monSince"),
    monHover = null,
    monPos = MON_DEFAULT,
    monFetching = false,
    monHistBroken = false,
    monDataV = 0,
    monRaf = 0,
    /* How much history we have ASKED the server for. The ring is 7 d /
       ~19 MB and the page usually draws one day of it, so the fetch is
       sized to the window and re-run when you zoom wider. */
    monHaveSpan = 0,
    monWantSpan = 0;

  MON_CHARTS.forEach(function (ch) {
    ch.canvas = document.getElementById(ch.cv);
    var host = document.getElementById(ch.leg);
    ch.legRefs = ch.series.map(function (se) {
      var item = el("span", "");
      item.appendChild(el("span", "sw " + se.cls));
      item.appendChild(document.createTextNode(se.label + " "));
      var b = el("b", "", "—");
      item.appendChild(b);
      host.appendChild(item);
      return b;
    });
  });

  /* The stored value is the window in SECONDS ("smeltr.monwin"), so adding a
   stop never moves anyone's saved choice. The two older keys are read once
   and converted: "smeltr.monstop" was a stop INDEX into the table before the
   1 min stop was prepended (so index i is MON_STOPS[i + 1] now), and
   "smeltr.monspan" a 0..100 position on a log curve. A stale key left behind
   would silently restore a position that no longer means anything. */
  function monNearest(want) {
    var best = MON_DEFAULT,
      bd = Infinity;
    MON_STOPS.forEach(function (v, i) {
      var d = Math.abs(Math.log(v) - Math.log(want));
      if (d < bd) {
        bd = d;
        best = i;
      }
    });
    return best;
  }
  try {
    var _sv = localStorage.getItem("smeltr.monwin");
    if (_sv != null && MON_STOPS.indexOf(+_sv) >= 0) monPos = MON_STOPS.indexOf(+_sv);
    else {
      var _idx = localStorage.getItem("smeltr.monstop"),
        _old = localStorage.getItem("smeltr.monspan");
      if (_idx != null && +_idx >= 0 && +_idx <= MON_STOPS.length - 2)
        monPos = Math.round(+_idx) + 1;
      else if (_old != null && +_old >= 0 && +_old <= 100)
        monPos = monNearest(3600 * Math.pow(24, +_old / 100));
      localStorage.setItem("smeltr.monwin", String(monSpan(monPos)));
      localStorage.removeItem("smeltr.monstop");
      localStorage.removeItem("smeltr.monspan");
    }
  } catch (_) {}
  monZoomEl.value = String(monPos);
  monLblEl.textContent = spanLabel(monSpan(monPos));
  monZoomEl.addEventListener("input", function () {
    monPos = Math.round(+monZoomEl.value);
    monLblEl.textContent = spanLabel(monSpan(monPos));
    try {
      localStorage.setItem("smeltr.monwin", String(monSpan(monPos)));
    } catch (_) {}
    monLoad(); /* no-op unless this stop needs more history */
    monDrawSoon();
  });

  function monPush(t, vals) {
    var i = t % MON_SLOTS;
    monTs[i] = t;
    for (var k = 0; k < 7; k++) monV[k][i] = vals[k] == null ? NaN : vals[k];
    if (monEarliest == null || t < monEarliest) monEarliest = t;
    monLast = { t: t, v: vals };
    monDataV++;
  }

  /* Fetch exactly the window being drawn, and only re-fetch when a wider
   stop asks for more than we already hold. `force` is the reconnect path:
   the same span, re-read, because we may have missed samples while the
   stream was down. */
  function monLoad(force) {
    var span = monSpan(monPos);
    if (monFetching) return;
    if (!force && span <= monHaveSpan) return;
    monFetching = true;
    monWantSpan = span;
    monDrawSoon(); /* repaint the caption as "loading" */
    fetch("/api/sysmon/history?t=" + encodeURIComponent(token) + "&span=" + span, {
      cache: "no-store",
    })
      .then(function (r) {
        return r.ok ? r.arrayBuffer() : null;
      })
      .then(function (buf) {
        monFetching = false;
        if (!buf) {
          monHistFail();
          return;
        }
        monHistBroken = false;
        if (span > monHaveSpan) monHaveSpan = span;
        var dv = new DataView(buf),
          t = 0,
          i,
          k;
        for (var o = 0; o + 32 <= buf.byteLength; o += 32) {
          t = dv.getUint32(o, true);
          i = t % MON_SLOTS;
          monTs[i] = t;
          if (monEarliest == null || t < monEarliest) monEarliest = t;
          for (k = 0; k < 7; k++) monV[k][i] = dv.getFloat32(o + 4 + 4 * k, true);
        }
        monDataV++;
        if (t && (!monLast || t > monLast.t)) {
          /* records are oldest-first */
          var vals = [];
          i = t % MON_SLOTS;
          for (k = 0; k < 7; k++) {
            var vv = monV[k][i];
            vals.push(vv === vv ? vv : null);
          }
          monLast = { t: t, v: vals };
        }
        monDrawSoon();
      })
      .catch(function () {
        monFetching = false;
        monHistFail();
      });
  }
  /* A FAILED history fetch is "history unavailable", never "no samples yet"
   -- the server may hold a full week we simply could not read. Retry. */
  function monHistFail() {
    monHistBroken = true;
    monWantSpan = 0;
    monDrawSoon();
    setTimeout(function () {
      monLoad(true);
    }, 30000);
  }

  function monCss(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  var MON_GUT = 44,
    MON_PADT = 6,
    MON_AXIS = 16;
  function drawMon(ch, t0, t1, css) {
    var cv = ch.canvas,
      dpr = window.devicePixelRatio || 1;
    var w = cv.clientWidth,
      h = cv.clientHeight;
    if (!w || !h) return;
    var pw = Math.round(w * dpr),
      ph = Math.round(h * dpr);
    if (cv.width !== pw || cv.height !== ph) {
      cv.width = pw;
      cv.height = ph;
    }
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    var x0 = MON_GUT,
      x1 = w - 6,
      y0 = MON_PADT,
      y1 = h - (ch.axis ? MON_AXIS : 6);
    /* One bucket per pixel column -- but NEVER more buckets than the window
     has seconds. A 15 min window on a 1200 px canvas has 900 samples for
     1156 columns, so a quarter of them hold nothing, and the honest
     "empty bucket = gap" rule then draws a 1 Hz series as a DOTTED line.
     The samples are not missing; there is simply more resolution on screen
     than in the data. Capping the bucket count and giving each bucket a
     fractional pixel width is what keeps a gap meaning "the sampler missed
     this second". */
    var cols = Math.max(1, Math.min(Math.round(x1 - x0), t1 - t0));
    var cw = (x1 - x0) / cols; /* pixels per bucket, >= 1 */
    /* Bucketing is O(window seconds) -- 604800 iterations per series at the
     7 d zoom -- so it is cached per (window, width, data version): a
     hover storm repaints from the cache instead of re-walking the week. */
    var bkey = t0 + "|" + t1 + "|" + cols + "|" + monDataV,
      bks;
    if (ch._bkey === bkey) {
      bks = ch._bks;
    } else {
      /* Smooth only where the shade is real -- see monSmooth(). Below 4
       samples per bucket b.sm stays unset and the line draws b.avg raw. */
      var smOk = (t1 - t0) / cols >= 4;
      bks = ch.series.map(function (se) {
        var b = monBuckets(monTs, monV[se.k], t0, t1, cols);
        if (smOk) b.sm = monSmooth(b.avg, cols);
        return b;
      });
      ch._bkey = bkey;
      ch._bks = bks;
    }
    var ymax;
    if (ch.pctAxis) ymax = 100;
    else {
      var m = 0;
      bks.forEach(function (b) {
        for (var c = 0; c < cols; c++) {
          var v = b.hi[c];
          if (v === v && v > m) m = v;
        }
      });
      ymax = niceMax(m / ch.scale) * ch.scale;
      /* Hard 1 MiB/s floor: without it a quiet minute of background chatter
       autoscales to a mountain range, and sub-1 axis labels round into
       lies (0 / 0.1 / 0.1). */
      if (!(ymax >= ch.scale)) ymax = ch.scale;
    }
    function X(sec) {
      return x0 + ((sec - t0) / (t1 - t0)) * (x1 - x0);
    }
    function Y(v) {
      return y1 - (Math.min(v, ymax) / ymax) * (y1 - y0);
    }
    ctx.font = "10px " + css.mono;
    /* The stretch of the window BEFORE the oldest held sample is washed with
     the skeleton token: no-data must never look like a machine at rest.
     preCols is that stretch in whole columns -- the series loops below ride
     the 0 baseline across it, and the wash is what keeps those zeros from
     reading as measurements. */
    var preCols = 0;
    if (css.histStart > t0) {
      preCols = Math.max(
        0,
        Math.min(cols, Math.round(((Math.min(css.histStart, t1) - t0) / (t1 - t0)) * cols)),
      );
      var edge = Math.min(x1, X(css.histStart));
      ctx.fillStyle = css.nodata;
      ctx.fillRect(x0, y0, edge - x0, y1 - y0);
      /* A hard edge where the record starts. The wash alone is a subtle
       shade; the rule is what makes "the data begins HERE" unmissable at a
       glance, and it is the only mark separating a wiped ring from a quiet
       machine. */
      ctx.strokeStyle = css.nodataBd;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(edge) + 0.5, y0);
      ctx.lineTo(Math.round(edge) + 0.5, y1);
      ctx.stroke();
      /* Said in words too, once per card, where there is room for it. The
       header caption carries the same fact, but it sits outside the plot
       and reads as a note about the page rather than about this chart. */
      if (edge - x0 > 190) {
        ctx.fillStyle = css.ink3;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("no samples before " + css.histLabel, x0 + (edge - x0) / 2, (y0 + y1) / 2);
      }
    }
    ctx.strokeStyle = css.grid;
    ctx.fillStyle = css.ink;
    ctx.lineWidth = 1;
    /* Quarter rules everywhere: the taller plots leave 0/50/100 too far apart
     to read a level against. The % chart labels all five; value axes label
     only the halves -- axLab prints a quarter of a niceMax 5 as 1.3, and a
     mislabelled rule is worse than a bare one. */
    [0, 0.25, 0.5, 0.75, 1].forEach(function (f) {
      var y = Math.round(Y(ymax * f)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x0, y);
      ctx.lineTo(x1, y);
      ctx.stroke();
      if (!ch.pctAxis && f % 0.5 !== 0) return;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(
        String(ch.pctAxis ? Math.round(100 * f) : axLab((ymax * f) / ch.scale)),
        x0 - 6,
        Math.max(y0 + 4, Math.min(y1 - 4, y)),
      );
    });
    var step = monTicks(t1 - t0);
    for (var tt = Math.ceil(t0 / step) * step; tt < t1; tt += step) {
      var gx = Math.round(X(tt)) + 0.5;
      ctx.strokeStyle = css.grid;
      ctx.beginPath();
      ctx.moveTo(gx, y0);
      ctx.lineTo(gx, y1);
      ctx.stroke();
      if (ch.axis) {
        ctx.fillStyle = css.ink;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(tickLab(tt, t1 - t0), gx, y1 + 4);
      }
    }
    ch.series.forEach(function (se, si) {
      var b = bks[si],
        colr = monCss(se.tok);
      ctx.globalAlpha = 0.16;
      ctx.fillStyle = colr;
      for (var c = 0; c < cols; c++) {
        if (b.lo[c] !== b.lo[c]) continue;
        var yh = Y(b.hi[c]),
          yl = Y(b.lo[c]);
        ctx.fillRect(x0 + c * cw, yh, Math.max(1, cw), Math.max(1, yl - yh));
      }
      ctx.globalAlpha = 1;
      ctx.strokeStyle = colr;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      var pts = [];
      /* BEFORE the oldest held sample the line rides the 0 baseline. Nothing
       was measured there, and the --skel wash under it plus the "history
       since HH:MM" caption are what say so -- the baseline exists only so a
       window wider than the ring reads as one chart instead of a broken
       one hanging off the right edge. This is NOT the gap rule: a missing
       second INSIDE the history still breaks the path below, because there
       the sampler was running and produced nothing, which is a fact about
       the machine rather than about how long we have been recording. */
      if (preCols > 0) {
        pts.push({ x: x0, y: Y(0) });
        pts.push({ x: x0 + preCols * cw, y: Y(0) });
      }
      for (c = preCols; c < cols; c++) {
        var v = (b.sm || b.avg)[c];
        /* gap: a null breaks the path, never bridges it */
        pts.push(v !== v ? null : { x: x0 + (c + 0.5) * cw, y: Y(v) });
      }
      monTrace(ctx, pts, t1 - t0 <= MON_CURVE_MAX);
      ctx.stroke();
    });
    if (monHover && monHover.t >= t0 && monHover.t < t1) {
      var hx = Math.round(X(monHover.t)) + 0.5;
      ctx.strokeStyle = css.ink3;
      ctx.globalAlpha = 0.7;
      ctx.beginPath();
      ctx.moveTo(hx, y0);
      ctx.lineTo(hx, y1);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
    ch._geom = { x0: x0, x1: x1, t0: t0, t1: t1 };
  }

  /* The folded head's sparklines: CPU, GPU and RAM, one series each, no
   gutter, axis, grid or wash -- a glance, not a reading. They are driven by
   the Utilization chart's OWN series list (MON_CHARTS[0]), so the label,
   the colour and the sample key here are the chart's by construction. Each
   draws the SAME window the slider holds, through the same
   monBuckets()/monSmooth() the full chart uses, so folding the card never
   changes what "the last hour" means and the line here is that chart's
   line with the chrome removed. A gap still breaks the path. clientWidth
   is 0 while the card is expanded (display:none), so the early return
   makes it free until the fold. */
  var MON_MINI = MON_CHARTS[0].series.map(function (se) {
    var host = document.querySelector('#monMini .monmini-s[data-k="' + se.k + '"]');
    return {
      se: se,
      cv: /** @type {HTMLCanvasElement} */ (host.querySelector("canvas")),
      val: host.querySelector("b"),
      bk: { key: "", b: null },
    };
  });
  function drawMonMini(m, t0, t1, css) {
    var cv = m.cv,
      dpr = window.devicePixelRatio || 1;
    var w = cv.clientWidth,
      h = cv.clientHeight;
    if (!w || !h) return;
    var pw = Math.round(w * dpr),
      ph = Math.round(h * dpr);
    if (cv.width !== pw || cv.height !== ph) {
      cv.width = pw;
      cv.height = ph;
    }
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    var cols = Math.max(1, Math.min(Math.round(w), t1 - t0)),
      cw = w / cols;
    var bkey = t0 + "|" + t1 + "|" + cols + "|" + monDataV;
    if (m.bk.key !== bkey) {
      var nb = monBuckets(monTs, monV[m.se.k], t0, t1, cols);
      if ((t1 - t0) / cols >= 4) nb.sm = monSmooth(nb.avg, cols);
      m.bk.key = bkey;
      m.bk.b = nb;
    }
    var line = m.bk.b.sm || m.bk.b.avg,
      y0 = 1,
      y1 = h - 1,
      colr = monCss(m.se.tok);
    function Y(v) {
      return y1 - (Math.min(v, 100) / 100) * (y1 - y0);
    }
    ctx.strokeStyle = css.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, y1 + 0.5);
    ctx.lineTo(w, y1 + 0.5);
    ctx.stroke();
    /* Fill under each contiguous run, then stroke it: a run ends at the
     first empty bucket so a sampler gap reads as a break, never a slope. */
    var c = 0;
    while (c < cols) {
      if (line[c] !== line[c]) {
        c++;
        continue;
      }
      var s = c,
        run = [];
      while (c < cols && line[c] === line[c]) {
        run.push({ x: (c + 0.5) * cw, y: Y(line[c]) });
        c++;
      }
      ctx.beginPath();
      monTrace(ctx, run, t1 - t0 <= MON_CURVE_MAX);
      ctx.strokeStyle = colr;
      ctx.lineWidth = 1.5;
      ctx.lineJoin = "round";
      ctx.stroke();
      ctx.lineTo((c - 0.5) * cw, y1);
      ctx.lineTo((s + 0.5) * cw, y1);
      ctx.closePath();
      ctx.globalAlpha = 0.16;
      ctx.fillStyle = colr;
      ctx.fill();
      ctx.globalAlpha = 1;
    }
  }

  /* All redraw triggers funnel through one rAF gate: N pointermove events in
   a frame cost one draw, and a draw never runs on a hidden tab. */
  function monDrawSoon() {
    if (monRaf || document.hidden) return;
    monRaf = requestAnimationFrame(function () {
      monRaf = 0;
      monDraw();
    });
  }
  function monDraw() {
    if (document.hidden) return;
    var now = Math.floor(Date.now() / 1000);
    var span = monSpan(monPos),
      t1 = now + 1,
      t0 = t1 - span;
    /* Gridlines carry the scale, so they use --line (a data reference), not
     the fainter --td-line row separator; numerals get --ink-2 for the same
     reason -- the axis is the only thing separating a 0.2 MiB/s chart from
     a 200 MiB/s one. */
    var early = monEarliest;
    if (early != null && early < t1 - MON_SLOTS) early = t1 - MON_SLOTS;
    var css = {
      ink: monCss("--ink-2"),
      ink3: monCss("--ink-3"),
      grid: monCss("--line"),
      nodata: monCss("--nodata"),
      nodataBd: monCss("--nodata-bd"),
      mono: monCss("--mono") || "monospace",
      histStart: early == null ? t1 : Math.max(t0, early),
      histLabel: early == null ? "this session" : stampLab(early),
    };
    MON_CHARTS.forEach(function (ch) {
      drawMon(ch, t0, t1, css);
    });
    /* The legend is the LATEST sample, and says so; a sampler that has gone
     quiet must show an em dash, not its last reading forever. */
    var stale = !monLast || now - monLast.t > 5;
    MON_MINI.forEach(function (m) {
      drawMonMini(m, t0, t1, css);
      m.val.textContent = monFmtPct(stale ? null : monLast.v[m.se.k]);
    });
    /* A window we have not fetched yet must NOT report itself as a machine
     with no history: "history since" is a claim about the sampler, and
     while a wider fetch is in flight the only true statement is that we
     are still reading. */
    monSinceEl.textContent = monHistBroken
      ? "history unavailable — live only"
      : monFetching && monWantSpan > monHaveSpan
        ? "loading history…"
        : early == null
          ? "no samples yet"
          : early > t0
            ? "history since " + stampLab(early)
            : "";
    MON_CHARTS.forEach(function (ch) {
      ch.series.forEach(function (se, si) {
        var v = stale ? null : monLast.v[se.k];
        ch.legRefs[si].textContent = ch.fmt(v == null ? null : v);
      });
    });
    monTipDraw(t0, t1);
  }

  function monSampleAt(t, tol) {
    for (var d = 0; d <= tol; d++) {
      var a = t - d,
        i = a % MON_SLOTS;
      if (a > 0 && monTs[i] === a) return a;
      var b = t + d;
      i = b % MON_SLOTS;
      if (monTs[i] === b) return b;
    }
    return null;
  }
  var MON_NAMES = ["CPU", "GPU", "RAM", "net in", "net out", "disk read", "disk write"];
  function monTipDraw(t0, t1) {
    if (!monHover) {
      monTipEl.hidden = true;
      return;
    }
    var tol = Math.max(2, Math.round((t1 - t0) / 600));
    var st = monSampleAt(monHover.t, tol);
    monTipEl.replaceChildren();
    /* The tooltip is ONE 1-second sample; the line under the cursor is a
     bucket mean (smoothed at wide zooms, raw at the narrow stops). Scope
     it explicitly so the two cannot be read as the same number
     disagreeing. */
    var when = st || monHover.t,
      d = new Date(when * 1000);
    monTipEl.appendChild(
      el(
        "div",
        "t",
        "1 s sample · " + stampLab(when) + ":" + String(d.getSeconds()).padStart(2, "0"),
      ),
    );
    for (var k = 0; k < 7; k++) {
      var row = el("div", "row");
      row.appendChild(el("span", "", MON_NAMES[k]));
      var v = null;
      if (st != null) {
        var raw = monV[k][st % MON_SLOTS];
        if (raw === raw) v = raw;
      }
      row.appendChild(el("b", "", k < 3 ? monFmtPct(v) : monFmtMibs(v)));
      monTipEl.appendChild(row);
    }
    var cr = monCard.getBoundingClientRect();
    var lx = monHover.cx - cr.left + 14,
      ly = monHover.cy - cr.top + 10;
    if (lx + 180 > cr.width) lx = Math.max(4, monHover.cx - cr.left - 194);
    monTipEl.style.left = lx + "px";
    monTipEl.style.top = ly + "px";
    monTipEl.hidden = false;
  }
  function monHoverEnd() {
    if (!monHover) return;
    monHover = null;
    monTipEl.hidden = true;
    monDrawSoon();
  }
  MON_CHARTS.forEach(function (ch) {
    ch.canvas.addEventListener("pointermove", function (ev) {
      var g = ch._geom;
      if (!g) return;
      var r = ch.canvas.getBoundingClientRect(),
        x = ev.clientX - r.left;
      if (x < g.x0 || x > g.x1) return monHoverEnd();
      monHover = {
        t: Math.round(g.t0 + ((x - g.x0) / (g.x1 - g.x0)) * (g.t1 - g.t0)),
        cx: ev.clientX,
        cy: ev.clientY,
      };
      monDrawSoon();
    });
    ch.canvas.addEventListener("pointerleave", monHoverEnd);
  });

  if (window.ResizeObserver) new ResizeObserver(monDrawSoon).observe(monCard);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) monDrawSoon();
  });
  /* A theme flip swaps every token under the canvas; repaint from the new
   ones. (System-mode OS flips re-resolve on the next 1 s frame anyway.) */
  new MutationObserver(monDrawSoon).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });

  /* ---- Boot states --------------------------------------------------------
   Nothing paints until the first frame arrives, so the skeleton is all the
   user has until then. Two things can go wrong, and each gets a FACT rather
   than a shimmer that continues forever:

     * slow  -- a cold build_state() stats three NAS roots and normally takes
                ~2 s. Past BOOT_SLOW_MS the wait is abnormal and says so.
     * dead  -- a non-200 (typically a 403 from a stale token) permanently
                CLOSES the EventSource. It will never reconnect, so the
                skeleton would shimmer forever over a page that is never
                coming. Replace it with the reason and the fix.

   Both are gated on !booted. A MID-SESSION disconnect must leave the
   last-known data on screen -- it is still the truth, just frozen -- and
   change only the header dot. Only a boot that never produced a single frame
   may put an error where the data would have been. ---- */
  var BOOT_SLOW_MS = 8000;
  var booted = false;

  /* Static card: fold control attached once at load. The chevron rides in
   .monhead (flex, right edge) so it never overlaps the zoom slider. */
  makeCollapsible(document.getElementById("sysmon"), "mon");
  makeCollapsible(document.getElementById("stats"), "stats");
  var tableFold = makeCollapsible(document.getElementById("tablewrap"), "table");

  function bootSkel() {
    return document.getElementById("bootSkel");
  }

  function bootSlow() {
    if (booted) return;
    var sk = bootSkel();
    if (!sk || sk.querySelector(".boot-note")) return;
    sk.appendChild(
      el("div", "boot-note", "still waiting on the library — a NAS root may be slow to answer"),
    );
  }

  function bootFail(why) {
    if (booted) return; /* never replace real data with an error */
    var host = document.getElementById("pane");
    if (!host || !bootSkel()) return;
    var box = el("div", "boot-fail");
    box.appendChild(el("b", "", "disconnected"));
    box.appendChild(el("div", "", why));
    host.replaceChildren(box);
    /* The ghost stats and live card are just as dead; drop them too rather than
     leave three shimmering blocks above a message saying nothing is coming. */
    document.querySelectorAll(".skelwrap").forEach(function (n) {
      n.remove();
    });
  }

  var slowTimer = setTimeout(bootSlow, BOOT_SLOW_MS);

  var PAGE_REV = (document.body && document.body.dataset.rev) || "";
  var reloading = false;
  var es = new EventSource("/api/stream?t=" + encodeURIComponent(token));
  es.onopen = function () {
    conn("on", "live");
    /* A reconnect means missed seconds (and possibly a restarted server whose
     ring has samples this page never saw). Refetch history to fill the gap
     rather than leave a hole that never heals. */
    if (booted) monLoad(true);
  };
  es.onerror = function () {
    /* The spec permanently CLOSES an EventSource on a non-200 (e.g. a 403 from
     a stale token) -- it will never reconnect, so "reconnecting" would be a
     lie over frozen data. Say so plainly instead. */
    var dead = es.readyState === EventSource.CLOSED;
    conn("off", dead ? "disconnected — reload the page" : "reconnecting");
    /* A pulsing beacon over a dead stream is a false proof of life — the
     exact "log tails are not proof of life" failure, in CSS form. */
    var p = document.getElementById("pulse");
    if (p) {
      p.classList.remove("on");
      p.title = "";
    }
    if (dead)
      bootFail(
        "the live stream closed before any data arrived. " +
          "Your link may carry a stale token — reload the page, or reopen it " +
          "from ./smeltr url.",
      );
  };
  es.onmessage = function (ev) {
    try {
      var s = JSON.parse(ev.data);
    } catch (_) {
      return;
    }
    /* A server that is serving a DIFFERENT page than the one this tab holds
     (web/* is inlined at import; every restart after an edit is one) is a
     tab showing stale controls behind a live feed. Reload once, the moment
     the first frame says so -- never loop on it. */
    if (s.page_rev && PAGE_REV && s.page_rev !== PAGE_REV && !reloading) {
      reloading = true;
      location.reload();
      return;
    }
    last.state = s;
    conn("on", "live");
    paint(s);
    if (!booted) {
      booted = true;
      clearTimeout(slowTimer);
      setTimeout(function () {
        document.body.classList.add("booted");
      }, 900);
    }
  };
  /* A `mon` frame is an ARRAY of samples -- the server's cursor ships every
   second since the last frame, so a slow build_state() upstream cannot
   punch fake gaps into the chart. */
  es.addEventListener("mon", function (ev) {
    try {
      var arr = JSON.parse(ev.data);
    } catch (_) {
      return;
    }
    if (!Array.isArray(arr)) return;
    for (var j = 0; j < arr.length; j++) {
      var m = arr[j];
      if (!m || typeof m.t !== "number" || !Array.isArray(m.v) || m.v.length !== 7) continue;
      if (monLast && m.t <= monLast.t) continue;
      monPush(m.t, m.v);
    }
  });
  /* The redraw clock is a plain interval, not the SSE frames: the window is
   anchored to now and must keep sliding -- and the legend must go stale --
   even when the sampler or the stream stops feeding it. */
  setInterval(monDrawSoon, 1000);
  monLoad(true);
})();
