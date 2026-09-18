"""Push-back state per finished title -- what ops/push-complete.sh is doing.

Dashboard only (imported by server.py and report.py; in DASHBOARD_ONLY). The
pusher leaves its evidence as files beside the driver's other markers, and
this module only READS them:

- a folder in $X9/complete/ holding exactly one finished encode is PENDING,
  and the pusher takes the largest first -- so the queue position here is
  computed by the same rule (size descending) and never stored anywhere;
- `.pushing-<title>` (pid inside) names the transfer on the wire,
  and only while that pid answers -- a marker left by a crashed push is not
  a transfer (the pusher clears it on its next tick; the page must not wait
  for that);
- `.push-failed-<title>` is a PERMANENT refusal with its reason (no or two
  library folders, a copy that did not verify) -- the title waits in
  complete/ with both copies kept until a human deletes the marker;
- `.pushed-<title>` records where the encode went once the X9 copy was
  removed, so the History row can say "on Vhagar" instead of "on the X9".

States, keyed by lowercased title: queued (with `pos` of `total`), pushing,
failed (with `note`), pushed (with `dest`, `nas`, `bucket`).
"""

import os
from typing import Optional

from pipeline import core


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return True


def _marker(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return None


def _dest_of(marker: str) -> str:
    """'<title>: pushed to <dest> at <stamp>' -> <dest>. The title and the
    dest may both contain ': ' and ' at ', so cut from the first 'pushed to '
    and at the LAST ' at '."""
    i = marker.find("pushed to ")
    body = marker[i + len("pushed to ") :] if i >= 0 else marker
    j = body.rfind(" at ")
    return body[:j] if j >= 0 else body


def _place(dest: str) -> dict:
    root = next(
        (r for r in core.LIBRARY_ROOTS if dest.startswith(r.rstrip("/") + os.sep)),
        None,
    )
    if root is None:
        return {"nas": core.volume_name(dest), "bucket": None}
    rel = dest[len(root.rstrip("/")) + 1 :]
    return {"nas": core.volume_name(root), "bucket": rel.split(os.sep)[0] if rel else None}


def pending() -> list:
    """(bytes, title) for every complete/ folder holding one finished encode
    and nothing in flux, largest first -- the pusher's own order."""
    comp = core.complete_dir()
    try:
        names = os.listdir(comp)
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith("."):
            continue
        d = os.path.join(comp, n)
        try:
            files = os.listdir(d)
        except OSError:
            continue
        if any(f.endswith(".partial") or f.endswith(".killing") for f in files):
            continue
        hevc = [
            f
            for f in files
            if f.lower().endswith(".mkv")
            and "2160p hevc" in f.lower()
            and not f.startswith("._")
        ]
        if len(hevc) != 1:
            continue
        try:
            size = os.path.getsize(os.path.join(d, hevc[0]))
        except OSError:
            continue
        out.append((size, n))
    out.sort(key=lambda t: (-t[0], t[1].lower()))
    return out


def on_wire() -> set:
    """Lowercased titles whose `.pushing-<title>` names a LIVE pid."""
    x9 = core.X9
    try:
        root = os.listdir(x9)
    except OSError:
        return set()
    out = set()
    for n in root:
        if not n.startswith(".pushing-"):
            continue
        title = n[len(".pushing-") :]
        try:
            with open(os.path.join(x9, n), encoding="utf-8") as fh:
                pid = int(fh.readline().strip() or "0")
        except (OSError, ValueError):
            continue
        if title and _alive(pid):
            out.add(title.lower())
    return out


def push_hold() -> Optional[str]:
    """Why the pusher is waiting (its `.push-hold`), or None."""
    return _marker(os.path.join(core.X9, ".push-hold")) or None


def pushes() -> dict:
    x9 = core.X9
    try:
        root = os.listdir(x9)
    except OSError:
        root = []
    wire = on_wire()
    out = {}
    rows = pending()
    # "#n of m" counts the titles still WAITING: not the one on the wire, not
    # a refused one.
    total = sum(
        1
        for _, n in rows
        if n.lower() not in wire
        and not os.path.exists(os.path.join(x9, ".push-failed-" + n))
    )
    pos = 0
    for size, n in rows:
        low = n.lower()
        note = _marker(os.path.join(x9, ".push-failed-" + n))
        if note is not None:
            out[low] = {"title": n, "state": "failed", "bytes": size, "note": note or "push failed"}
        elif low in wire:
            out[low] = {"title": n, "state": "pushing", "bytes": size}
        else:
            pos += 1
            out[low] = {"title": n, "state": "queued", "bytes": size, "pos": pos, "total": total}
    for n in root:
        if not n.startswith(".pushed-"):
            continue
        title = n[len(".pushed-") :]
        low = title.lower()
        if low in out:
            continue
        m = _marker(os.path.join(x9, n)) or ""
        dest = _dest_of(m)
        rec = {"title": title, "state": "pushed", "dest": dest, "note": m}
        rec.update(_place(dest))
        out[low] = rec
    return out


def push_off() -> bool:
    return os.path.exists(os.path.join(core.X9, ".push-off"))
