"""Stage-on-demand: pull one library title onto the staging drive from the page.

Moved out of `server.py` unchanged in behaviour. The engine is a queue, one
dispatcher thread (the "pump") and one worker thread per pull; everything here
runs under `LOCK`. It needs three things from the server and nothing else, and
`bind()` hands them over once at import: the queue rows, the banner note, and
"drop the cached state payload". Dashboard only -- never imported by the
pipeline.
"""

import os
import re
import shutil
import signal
import subprocess
import threading
import time

from pipeline import core
from dashboard import pushes
from dashboard import probe

_host: dict = {}


def bind(rows, note, drop_cache) -> None:
    """Wire the server's three hooks. Called once, by `server.py`, at import."""
    _host.update(rows=rows, note=note, drop_cache=drop_cache)


def _rows() -> list:
    return _host["rows"]()


def _note(msg, kind="warn") -> None:
    _host["note"](msg, kind)


def _drop_cache() -> None:
    _host["drop_cache"]()


def hidden_pulls() -> list:
    """Every hidden ".pull-<title>" folder, as (name, path): in queue/, where
    a pull lands since 2026-09-07, plus any left at the X9 root by a server
    that predates the layout. Nothing else ever creates that prefix."""
    out = []
    for parent in (core.stage_dir(), core.X9):
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for n in names:
            if n.startswith(".pull-") and os.path.isdir(os.path.join(parent, n)):
                out.append((n, os.path.join(parent, n)))
    return out


# ------------------------------------------------------------- stage control
# The third thing server.py can DO (after queue overrides and encode control):
# pull one library title onto the staging drive on demand. It deliberately
# mirrors the per-title pull in .replenish-queue.sh -- same .ssh-xfer.sh pull,
# same free-space margin, same delete-the-folder cleanup on failure -- because
# the two are separate implementations of one procedure and must not drift.
# Progress needs no plumbing of its own: the pull lands as "<name>.partial",
# which _arrivals() already reports and the queue row already draws as a bar.
# Still inside the sanctioned boundary: this steers which title is staged,
# never the verdict, sync, or a deletion.
LOCK = threading.Lock()
# One dashboard-initiated pull at a time; the title travelling right now.
ACTIVE: dict = {"title": None}
# Titles waiting their turn on the wire, in click order. IN MEMORY ONLY: a
# server restart drops the list and the rows simply offer "stage" again,
# which is the honest failure. A persisted plan would outlive the code change
# that prompted the restart and could dispatch against stale assumptions.
QUEUE: list = []
# Why the head of the queue is not moving, in the row's own words, or None.
# Set by the pump, rendered on the queued row -- a queue that silently sits
# still is indistinguishable from one that is broken.
WAIT: dict = {"why": None}
PUMP: dict = {"on": False}
# The pump only pgreps and statvfs's; it is cheap enough to tick often and
# slow enough not to matter beside a multi-GiB transfer.
STAGE_PUMP_SECONDS = 20.0


def _wire_busy_locked():
    """Reason a pull cannot start RIGHT NOW, or None. Caller holds the lock.

    All three conditions are transient by nature, so the queue holds against
    them rather than dropping work. Liveness is pgrep, not the lock file: the
    documented pause procedure kills orphans with -9 and leaves mkdir locks
    behind, and a lock with no process starves the replenisher too -- so that
    case is called out by name with the command to fix it.
    """
    if ACTIVE["title"]:
        return "waiting — %s is on the wire" % ACTIVE["title"]
    if pushes.TRANSFERS_PAUSED.is_set():
        return "waiting — transfers are paused"
    # NAME THE TRANSFER (2026-09-07). "another pull owns the wire" named
    # nothing a person could go and check, and on 2026-09-07 the only pull on
    # the machine was a replenish pull of a hand-skipped title -- whose row,
    # and whose arrival bar, had lifted to the Errors tab. The Queue tab
    # showed a blocking transfer that appeared nowhere on it, which reads as
    # the UI inventing one. A TITLE is stable for the length of a transfer,
    # so it is shape and not a live number: unlike the free-space figure this
    # string deliberately omits, it cannot thrash paint()'s repaint key.
    busy, who = probe.wire_title(r"ssh-xfer\.sh pull")
    if busy:
        if who:
            return "waiting — %s is on the wire" % who
        return "waiting — another pull owns the wire"
    if os.path.isdir(os.path.join(core.X9, ".replenish.lock")):
        rbusy, rwho = probe.wire_title(r"ssh-xfer\.sh pull|replenish-queue\.sh")
        if rbusy:
            if rwho:
                return "waiting — the replenisher is staging %s" % rwho
            return "waiting — the replenisher is staging"
        return "waiting — a STALE .replenish.lock is blocking the replenisher"
    return None


def _stage_candidate(title: str, rows: list):
    """Resolve a title to (row, src, size). Returns (row, src, size, err, hold).

    `hold` distinguishes "not yet" from "never": a hold keeps the title in the
    queue and retries, an err without hold drops it. Used by BOTH the enqueue
    POST and the pump, so a click is refused for the same reasons a dispatch
    is -- one implementation, no drift between the two moments.
    """
    matches = [r for r in rows if r["title"].lower() == title.lower()]
    if not matches:
        return None, None, None, "it is no longer in the queue", False
    if len(matches) > 1:
        return (
            None,
            None,
            None,
            ("two queue rows share this folder name; refusing to act on both"),
            False,
        )
    row = matches[0]
    if row.get("skipped"):
        return None, None, None, "it is skipped — restore it first", False
    # Arriving before staged: "already there" is the wrong message for a file
    # that is mostly missing.
    if row.get("arriving_bytes") is not None:
        pct = (
            " (%.0f%% pulled)" % (row["arriving_bytes"] / row["bytes"] * 100)
            if row.get("bytes")
            else ""
        )
        return (
            None,
            None,
            None,
            ("it is already being copied to the staging drive%s" % pct),
            False,
        )
    if row.get("staged"):
        return None, None, None, "it is already on the staging drive", False
    srcs = set()
    for rec in core.load_index():
        p = rec.get("path", "")
        if not p or "2160p hevc" in os.path.basename(p).lower():
            continue
        if os.path.basename(os.path.dirname(p)).lower() == row["title"].lower():
            srcs.add(p)
    if not srcs:
        return None, None, None, "its file is not in the bitrate index", False
    if len(srcs) > 1:
        return (
            None,
            None,
            None,
            (
                "the index lists more than one source file "
                "for this title; refusing to pick one"
            ),
            False,
        )
    src = srcs.pop()
    # An unreachable NAS is a HOLD, not a drop: the share remounts, and a
    # queue that empties itself during a blip is worse than one that waits.
    if not os.path.isfile(src):
        return None, None, None, "waiting — the library file is unreachable", True
    try:
        size = os.path.getsize(src)
    except OSError as e:
        return None, None, None, "waiting — cannot stat the library file (%s)" % e, True
    return row, src, size, None, False


def _space_hold(size: int, rows: list):
    """Free-space gate, same margin as .replenish-queue.sh, or None.

    The message deliberately carries NO volatile number: `need` is stable for
    a given title, but free space moves every second as the encode writes, and
    a reason string that changes every frame would rebuild the table on every
    frame. The measured figure goes to the banner note instead, once.
    """
    try:
        st = os.statvfs(core.X9)
        avail = st.f_bavail * st.f_frsize
    except OSError:
        return "waiting — cannot read free space on the staging drive", None
    # PLUS the bytes other in-flight pulls have promised but not yet written:
    # statvfs only counts what has already landed.
    inbound = sum(
        max(0, (r.get("bytes") or 0) - r["arriving_bytes"])
        for r in rows
        if r.get("arriving_bytes") is not None
    )
    need = size + inbound + 10 * 1024**3
    if avail < need:
        return ("waiting for room — needs %.0f GiB free" % (need / 1073741824.0)), avail
    return None, avail


def _begin_pull_locked(row: dict, src: str, size: int):
    """Create the hidden folder and start the worker. Caller holds the lock."""
    # The pull lands HIDDEN (dot-prefixed, so invisible to next_title.py,
    # core.staged_folders(), and the replenisher's find) and is renamed into
    # place only once the byte count checks out. A visible folder with no
    # source file HALTS the driver.
    hidden = os.path.join(core.stage_dir(), ".pull-" + row["title"])
    try:
        os.makedirs(hidden, exist_ok=True)
    except OSError as e:
        return "could not create the pull folder: %s" % e
    ACTIVE["title"] = row["title"]
    try:
        threading.Thread(
            target=_stage_worker, args=(row["title"], src, hidden), daemon=True
        ).start()
    except RuntimeError as e:
        ACTIVE["title"] = None
        shutil.rmtree(hidden, ignore_errors=True)
        return "could not start the pull thread: %s" % e
    _note(
        "Staging %s — pulling %.2f GiB from the library over SSH."
        % (row["title"], size / 1073741824.0),
        kind="ok",
    )
    return None


def _pump_once_locked(rows: list) -> None:
    """Try to start the head of the queue. Caller holds LOCK.

    `rows` is a queue snapshot the CALLER built outside the lock -- build_state
    takes LOCK itself to report the queue, so building it in here would
    deadlock the dispatcher against the page.

    EVERY gate is re-applied here, at dispatch, because a check made when the
    button was clicked can be hours stale by the time the wire frees up --
    the replenisher may have staged the title meanwhile, the drive may have
    filled, the NAS may have gone. A transient refusal holds the head and
    says so on the row; a permanent one drops that ONE title and moves on, so
    a single dead title cannot wedge the whole queue.
    """

    def hold(why):
        if WAIT["why"] != why:
            WAIT["why"] = why
        return

    def drop(title, why, kind):
        QUEUE.pop(0)
        WAIT["why"] = None
        _note("Dropped the queued pull of %s — %s." % (title, why), kind=kind)

    title = QUEUE[0]
    busy = _wire_busy_locked()
    if busy:
        return hold(busy)
    row, src, size, err, held = _stage_candidate(title, rows)
    if err:
        if held:
            return hold(err)
        # "already staged" is a success, not a failure: the replenisher got
        # there first and the title is exactly where the click wanted it.
        done = "already on the staging drive" in err or "already being copied" in err
        return drop(title, err, "ok" if done else "bad")
    why, avail = _space_hold(size, rows)
    if why:
        if WAIT["why"] != why and avail is not None:
            _note(
                "Pull queue is waiting for room on the staging drive: "
                "%s needs %.0f GiB free, %.0f GiB available."
                % (title, (size + 10 * 1024**3) / 1073741824.0, avail / 1073741824.0),
                kind="warn",
            )
        return hold(why)
    err = _begin_pull_locked(row, src, size)
    if err:
        return drop(title, err, "bad")
    QUEUE.pop(0)
    WAIT["why"] = None


def _stage_pump_loop() -> None:
    """One dispatcher for the whole queue; exits when the queue drains."""
    try:
        while True:
            with LOCK:
                if not QUEUE:
                    WAIT["why"] = None
                    return
                idle = ACTIVE["title"] is None
            # OUTSIDE the lock: build_state() takes LOCK to report the
            # queue, so holding it here would deadlock the pump against every
            # open page. A slightly stale snapshot is fine -- it only chooses
            # whether to try; _begin_pull_locked is what commits.
            rows = _rows() if idle else []
            with LOCK:
                before = (WAIT["why"], len(QUEUE), ACTIVE["title"])
                if idle and QUEUE and ACTIVE["title"] is None:
                    _pump_once_locked(rows)
                changed = before != (
                    WAIT["why"],
                    len(QUEUE),
                    ACTIVE["title"],
                )
            if changed:
                _drop_cache()
            time.sleep(STAGE_PUMP_SECONDS)
    finally:
        with LOCK:
            PUMP["on"] = False
        _drop_cache()


def _ensure_pump_locked() -> None:
    """Start the dispatcher if it is not already running. Caller holds lock."""
    if PUMP["on"]:
        return
    PUMP["on"] = True
    try:
        threading.Thread(target=_stage_pump_loop, daemon=True).start()
    except RuntimeError:
        PUMP["on"] = False
        raise


def _count_download() -> None:
    """One more file downloaded and ready to encode, for the operator's budget.

    `download_budget` / `downloads_done` beside the ledger are the
    replenisher's (operator's rule, 2026-09-07: the budget counts DOWNLOADS,
    never encodes). A dashboard stage pull is a download too, so it is
    counted the same way, on the commit rename -- but it is never REFUSED by
    the budget: a hand click is an explicit request. Best effort: a counting
    failure must not undo a landed pull.
    """
    try:
        if not os.path.exists(os.path.join(core.SMELTR_DIR, "download_budget")):
            return
        f = os.path.join(core.SMELTR_DIR, "downloads_done")
        try:
            with open(f, encoding="utf-8") as fh:
                n = int(re.sub(r"\D", "", fh.read()) or 0)
        except (OSError, ValueError):
            n = 0
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("%d\n" % (n + 1))
    except OSError:
        pass


def _stage_worker(title: str, src: str, hidden: str) -> None:
    """Run the pull into the hidden folder, then move it into place.

    The rename is the commit. Until .ssh-xfer.sh's byte-count check passes,
    nothing outside ".pull-<title>" exists, so next_title.py, the
    replenisher's folder count, and the queue's staged flag never see a
    half-arrived title -- a VISIBLE folder without a source .mkv is exactly
    the state that halts the driver ("no source file", exit 2). A leftover
    from a crashed server stays hidden too: it can only ever render as a
    stalled arrival, never as an encodable folder -- until the next server
    start, when _adopt_orphan_pulls() finishes what this thread could not.
    """
    slug = probe.slug_of(title)
    log = os.path.join(core.X9, ".pull-%s.log" % slug)
    xfer = os.path.join(core.X9, ".ssh-xfer.sh")
    keep = False
    try:
        rc = None
        try:
            with open(log, "wb") as fh:
                proc = subprocess.Popen(
                    ["/bin/bash", xfer, "pull", src, hidden],
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            try:
                rc = proc.wait(timeout=6 * 3600)
            except subprocess.TimeoutExpired:
                # Kill the whole group: bash's ssh child keeps the partial's
                # fd open otherwise, and deleting a folder around a live
                # writer leaks the bytes into an unlinked inode df still
                # counts but no listing shows.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
                _note(
                    "Staging %s gave up after 6 hours — killed the pull;"
                    " removing the folder." % title,
                    kind="bad",
                )
        except OSError as e:
            _note(
                "Staging %s FAILED before the transfer started (%s)." % (title, e),
                kind="bad",
            )
        if rc == 0:
            dest = os.path.join(core.stage_dir(), title)
            try:
                os.makedirs(core.stage_dir(), exist_ok=True)
                os.rename(hidden, dest)
                _count_download()
                keep = True
                _note(
                    "Staged %s — a copy. The library original is "
                    "untouched now, and is deleted only when a good "
                    "encode of it syncs back." % title,
                    kind="ok",
                )
            except OSError as e:
                # Never delete a completed 70 GB pull over a rename problem.
                keep = True
                _note(
                    "Pulled %s but could NOT move it into place (%s) — "
                    "the complete file is in %s; move it by hand."
                    % (title, e, os.path.basename(hidden)),
                    kind="bad",
                )
        elif rc is not None:
            _note(
                "Staging %s FAILED — see %s. Removing the folder."
                % (title, os.path.basename(log)),
                kind="bad",
            )
        if not keep:
            # Delete only inside the staging drive, however hidden was built.
            real = os.path.realpath(hidden)
            if real.startswith(os.path.realpath(core.X9) + os.sep):
                shutil.rmtree(real, ignore_errors=True)
    finally:
        with LOCK:
            ACTIVE["title"] = None
        _drop_cache()


def _finish_orphan_locked(title: str, hidden: str) -> None:
    """Commit or clean up ONE orphaned pull folder. Caller holds LOCK.

    The commit evidence is the folder's contents, not a return code: the
    .ssh-xfer.sh script renames "<name>.partial" to its final name only on a
    byte-count match, so a final .mkv with no .partial beside it IS the
    completed, verified pull. Anything else is a dead half-pull and gets the
    same delete-the-folder cleanup as the worker's failure path.
    """
    try:
        files = os.listdir(hidden)
    except OSError:
        return
    real = [f for f in files if not f.startswith("._")]
    complete = any(f.endswith(core.SOURCE_EXTS) for f in real) and not any(
        f.endswith(".partial") for f in real
    )
    if complete:
        dest = os.path.join(core.stage_dir(), title)
        try:
            # folder_dir() answers the legacy root folder too, so a title
            # that still sits at the root is a collision, not a free name.
            if os.path.exists(dest) or os.path.exists(core.folder_dir(title)):
                raise OSError("a folder named %s already exists" % title)
            os.makedirs(core.stage_dir(), exist_ok=True)
            os.rename(hidden, dest)
            _count_download()
            _note(
                "Staged %s — finished a pull orphaned by a server "
                "restart. The library original is untouched, and is "
                "deleted only when a good encode of it syncs back." % title,
                kind="ok",
            )
        except OSError as e:
            # Never delete a completed 70 GB pull over a rename problem.
            _note(
                "Found the completed orphaned pull of %s but could "
                "NOT move it into place (%s) — the file is in %s; "
                "move it by hand." % (title, e, os.path.basename(hidden)),
                kind="bad",
            )
        return
    # Delete only inside the staging drive, however hidden was built.
    realpath = os.path.realpath(hidden)
    if realpath.startswith(os.path.realpath(core.X9) + os.sep):
        shutil.rmtree(realpath, ignore_errors=True)
    _note(
        "Removed the half-finished pull of %s left behind by a server "
        "restart — stage it again." % title,
        kind="bad",
    )


def _sweep_orphans_once() -> bool:
    """Finish pulls orphaned by a server restart. True when nothing is left.

    The pull child is start_new_session'd, so it survives a server restart --
    but the _stage_worker thread that waits on it and does the commit rename
    dies with the old process. On 2026-08-31 that stranded a complete,
    verified 61 GB pull as a stalled arrival until a human renamed it. This
    sweep applies the commit-or-cleanup the dead worker would have.

    False means "come back later": a puller is still alive (the orphaned
    transfer itself, or the replenisher's -- pgrep cannot tell them apart and
    does not need to), or a worker in THIS process owns a hidden folder
    (ACTIVE covers the pre-spawn and post-exit windows pgrep misses).
    """
    if not os.path.isdir(core.X9):
        # No staging drive to sweep. The next restart with it mounted adopts
        # whatever is there; retrying here would tick forever on a machine
        # that simply has no X9.
        return True
    orphans = hidden_pulls()
    if not orphans:
        return True
    with LOCK:
        if ACTIVE["title"] is not None:
            return False
        if probe.pgrep(r"ssh-xfer\.sh pull"):
            return False
        for n, d in orphans:
            _finish_orphan_locked(n[len(".pull-") :], d)
    _drop_cache()
    return True


def adopt_orphan_pulls() -> None:
    """Startup: finish pulls a previous server run left behind, off-thread.

    Off the startup path because the cleanup half can rmtree tens of GB. The
    loop keeps ticking while an orphan's transfer is still running -- the
    child survived the restart -- and commits once it exits.
    """
    try:
        if not hidden_pulls():
            return
    except OSError:
        return

    def loop():
        while not _sweep_orphans_once():
            time.sleep(STAGE_PUMP_SECONDS)

    threading.Thread(target=loop, daemon=True).start()


def enqueue(body: dict):
    """Enqueue a pull. The wire being busy means QUEUED, not refused.

    Everything that can be decided at click time still is, so a bad click
    gets an immediate 4xx instead of a silent drop twenty minutes later.
    What is NOT decided here is anything that can change while the title
    waits -- free space, the replenisher, the wire -- because a check made
    now is worthless by the time this title's turn arrives. The pump
    re-runs all of them at dispatch.
    """
    title = body.get("title")
    if not isinstance(title, str):
        return "expected {title: str}"
    if not os.path.isdir(core.X9):
        return "the staging drive is not mounted"
    rows = _rows()
    with LOCK:
        if (
            ACTIVE["title"]
            and ACTIVE["title"].lower() == title.lower()
        ):
            return "this title is being pulled right now"
        if any(t.lower() == title.lower() for t in QUEUE):
            return "this title is already in the pull queue at position %d" % (
                1 + [t.lower() for t in QUEUE].index(title.lower())
            )
        row, src, size, err, held = _stage_candidate(title, rows)
        if err:
            return err if not held else err.replace("waiting — ", "")
        QUEUE.append(row["title"])
        pos = len(QUEUE)
        try:
            _ensure_pump_locked()
        except RuntimeError as e:
            QUEUE.pop()
            return "could not start the pull dispatcher: %s" % e
        busy = _wire_busy_locked()
    if busy or pos > 1:
        _note(
            "Queued %s (%.2f GiB) — position %d. One transfer runs "
            "at a time; the rest wait their turn."
            % (row["title"], size / 1073741824.0, pos),
            kind="ok",
        )
    # Position 1 with a free wire: the pump starts it on its next tick,
    # and _begin_pull_locked writes the note that says so.
    return None

def cancel(body: dict):
    """Take a PENDING title out of the queue.

    It deliberately cannot touch the pull that is already running: there
    is no abort path for a transfer, and pretending otherwise would leave
    a half-written hidden folder that nothing owns.
    """
    title = body.get("title")
    if not isinstance(title, str):
        return "expected {title: str}"
    with LOCK:
        if (
            ACTIVE["title"]
            and ACTIVE["title"].lower() == title.lower()
        ):
            return (
                "that pull is already running — it finishes or it "
                "fails; the dashboard does not abort a transfer"
            )
        keep = [t for t in QUEUE if t.lower() != title.lower()]
        if len(keep) == len(QUEUE):
            return "that title is not in the pull queue"
        QUEUE[:] = keep
        if not QUEUE:
            WAIT["why"] = None
    _note("Removed %s from the pull queue." % title, kind="ok")
    return None
