"""Email + Slack notifications for the events a human wants to hear about:
an encode done, failed, laddered up or down, gone to the ERROR state, a
sync that failed, the driver's stop condition, and — the one irreversible
act — a library original deleted. Imported ONLY by `dashboard/server.py`;
the decision path never loads this, so a dead webhook or a Gmail outage can
never stall a verdict or a deletion.

It is an OBSERVER of the same logs the Events tab reads — `events.events()`
is the one parser, so a notification can never disagree with the tab. One
daemon thread ticks every POLL_SECONDS, diffs the current snapshot against
the keys it has already seen, classifies what is new, and delivers.

The rules that keep it honest:

- The first snapshot is a BASELINE, never a backlog. A fresh `notify.cursor`
  must not replay 250 lines of history into a channel.
- An EMPTY snapshot (X9 unmounted) is never a baseline and never a diff.
- The seen-set is a UNION, bounded at SEEN_CAP, never a replacement:
  events.py degrades PER SOURCE, so one unreadable watch log leaves a
  non-empty snapshot, and a replaced seen-set would re-send every deletion
  notice in the tail a tick later.
- Identity is (src, kind, text) — plus ts for driver lines, which are
  always stamped. A watcher line's ts flips to None when its log is re-used
  (the KILLED line stops being the final line), so ts must not key it.
- The ladder anchors on the driver's stamped LADDER line, not the watcher's
  KILLED line: the driver truncates the watch log in the same pass that
  logs LADDER, so a poll can miss the KILLED line entirely. The kill's
  projection is remembered from an earlier tick and added when known.
- The deletion notice is built from the LEDGER row (RECORD writes it before
  the sync), never from the folded shell output under the SYNC line: syncs
  are backgrounded and unstamped, so that text can belong to another title,
  and it carried the ssh user@host.
- A send that fails stays PENDING (persisted per channel the moment a
  channel lands, so a restart cannot lose the deletion notice OR resend the
  half that landed), retried with a doubling backoff from RETRY_SECONDS to
  RETRY_CAP_SECONDS, and dropped after MAX_PENDING_SECONDS with a log line.
- A burst above BURST_CAP keeps the most severe (newest within a class) and
  sends ONE summary naming what it dropped.

Config is `notify.json` beside the ledger (gitignored; MUST be 0600, it
holds the Gmail app password — a looser mode is refused, the way ssh
refuses a loose key):

    {"slack": {"webhook": "https://hooks.slack.com/services/..."},
     "email": {"host": "smtp.gmail.com", "port": 587,
               "user": "you@gmail.com", "password": "<app password>",
               "to": "you@gmail.com", "from": "you@gmail.com"}}

Either channel may be omitted; no file means the feature is off, silently.
"""

import datetime
import json
import os
import re
import smtplib
import ssl
import sys
import threading
import time
from email.message import EmailMessage
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline.core as core  # noqa: E402
from dashboard import events

CONFIG_NAME = "notify.json"
CURSOR_NAME = "notify.cursor"
POLL_SECONDS = 5
RETRY_SECONDS = 60
RETRY_CAP_SECONDS = 3600
MAX_PENDING_SECONDS = 24 * 3600
BURST_CAP = 10
SEEN_CAP = 4000
HTTP_TIMEOUT = 15
GIB = 1073741824

# Every `what` a notification can carry, with its glyph and label. A `what`
# outside this table would render "?" — Formatting.test_every_what_has_a_label
# pins the table total. The glyph is read BEFORE the words on a lock screen,
# so the two events a human must act on (error, deletion) carry the loudest
# marks and the two ladder directions cannot share a shape.
WHATS = {
    "finished": ("🎬", "Encode done"),
    "failed": ("💥", "Failed"),
    "ladder-up": ("🔺", "Ladder UP"),
    "ladder-down": ("🔻", "Ladder DOWN"),
    "last-rung": ("🏁", "Last rung — finishing anyway"),
    "error": ("🚨", "Error state"),
    "sync-failed": ("⛔", "Sync failed"),
    "stopped": ("🛑", "Driver stopped"),
    "deleted": ("🗑️", "Original deleted"),
    "burst": ("…", "More events"),
    "test": ("🧪", "Test message"),
}
# Which survive a burst cap, most important first.
PRIORITY = (
    "deleted",
    "sync-failed",
    "error",
    "stopped",
    "failed",
    "last-rung",
    "ladder-up",
    "ladder-down",
    "finished",
)

_LADDER = re.compile(r"^LADDER (.+) -> CRF (\d+)$")
# Both watcher wordings: "(CRF 14)" pre-2026-08-25, "(x265_10bit Q14)" since,
# and "(x265_10bit CRF 14)" / "(vt_h265_10bit VT CQ 60)" on a FINAL line. The
# CRF-only pattern had matched nothing since the encoder-aware watcher
# deployed, so every Ladder UP/DOWN message silently shipped without the
# projection it is documented to carry.
_KILL = re.compile(
    r"projected ([\d.]+)% of original at ([\d.]+)% "
    r"\([\w ]*?(?:CRF|CQ|Q) ?(\d+)\)"
)
_SYNC_FAILED = re.compile(r"^SYNC FAILED for (.+?) - (.*)$")
_SYNC_ABORTED = re.compile(r"^SYNC ABORTED: (.+?) for (.+?) - (.*)$")
_SEP = " — "
_MOVED_ON = " - marked for review, moving on"


def _err(msg: str) -> None:
    print(f"smeltr notify: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------- classify --


def _split_watcher(text: str):
    """'KILLED Movie (2000) — projected …' -> ('Movie (2000)', 'projected …')"""
    head, _, rest = text.partition(_SEP)
    title = head.split(" ", 1)[1] if " " in head else head
    return title, rest


def _exhausted(why: str) -> str:
    if "none-too-small" in why:
        return (
            "CRF ladder exhausted going DOWN — the projection stayed under "
            "the 30% band even at the lowest rung"
        )
    if "none-too-big" in why:
        return (
            "CRF ladder exhausted going UP — the projection stayed over "
            "the 80% band even at the highest rung"
        )
    return why


def classify(e: dict, err=_err):
    """An events.py event -> {what, title, text, ts, approx, detail}, or None
    for the noise (START, JUDGE, DEFER, waiting, …).

    Deliberately silent: the watcher's KILLED line (see the module docstring
    — the driver's LADDER line is the anchor) and its `exhausted` line (the
    driver's ERROR line follows and names the direction). Its FINAL line is
    NOT silent: no driver line follows it, because past the last rung the
    watcher lets the encode run and the driver is never told."""
    kind, text = e.get("kind"), e.get("text") or ""
    base = {"ts": e.get("ts"), "approx": bool(e.get("approx")), "detail": None}
    if kind == "complete":
        title, rest = _split_watcher(text)
        return dict(
            base, what="finished", title=title, text=f"{rest} · verdict pending"
        )
    if kind == "failed":
        title, rest = _split_watcher(text)
        return dict(base, what="failed", title=title, text=rest)
    if kind == "lastrung":
        # END OF THE LADDER (2026-09-06). Unlike a kill, this one has NO
        # driver line behind it — the watcher leaves the encode running and
        # .autopilot.sh never learns the ladder ran out — so if this is not
        # sent here it is not sent at all.
        #
        # THE TWO ARMS DO NOT END THE SAME WAY, and the message must not
        # average them into one reassurance. A too-BIG file is >80% of source,
        # which is `no-saving` — the original is safe. A too-SMALL file
        # anywhere from the 15.0 floor to the 30% band edge is a `good`
        # verdict (the band is a target, not a defect threshold), which syncs
        # and DELETES the ~90 GB library original unattended — and that is
        # precisely where a too-small terminal rung lands. Saying "nothing is
        # deleted" here would be false in the one direction where it matters.
        title, rest = _split_watcher(text)
        # Fails toward the DANGEROUS arm: anything that does not positively
        # say "too-big" is treated as the arm that can delete an original.
        # A watcher deployed from an earlier build words this line
        # differently, and `"too-small" in rest` took the reassuring branch
        # on it -- the false sentence, on the arm that deletes, in the one
        # message this path ever produces.
        small = "too-big" not in rest
        tail = (
            "a below-band result is still a `good` verdict once it clears the "
            f"{core.OUTLIER_FLOOR_NORM:.0f}% floor, which SYNCS and deletes "
            "the library original unattended — check it before then if you "
            "want it kept"
            if small
            else "an above-band result is `no-saving`, so it will not sync "
            "and the library original is safe"
        )
        return dict(
            base,
            what="last-rung",
            title=title,
            text=f"{rest} · the encode is still running and will finish · "
            f"nothing is deleted yet, the verdict decides · {tail}",
        )
    if kind == "ladder":
        m = _LADDER.match(text)
        if not m:
            err(f"unparseable LADDER line, no notification: {text!r}")
            return None
        title, nxt = m.group(1), int(m.group(2))
        # The ladder is one rung per retry and every rung above the pivot is
        # an up-rung, so the previous rung and the direction both follow
        # from the target alone — no watcher line needed.
        up = nxt > core.CRF_DEFAULT
        cur = nxt - 2 if up else nxt + 2
        side = (
            "ABOVE the 30–80% band (too big)"
            if up
            else "BELOW the 30–80% band (too small)"
        )
        return dict(
            base,
            what="ladder-up" if up else "ladder-down",
            title=title,
            text=f"projection landed {side} · encode killed, partial "
            f"deleted · retrying at CRF {cur} → {nxt}",
        )
    if kind == "cycle" and text.startswith("CYCLE COMPLETE "):
        return dict(
            base, what="deleted", title=text[len("CYCLE COMPLETE ") :].strip(), text=""
        )
    if kind == "sync":
        m = _SYNC_FAILED.match(text)
        if m:
            return dict(
                base,
                what="sync-failed",
                title=m.group(1),
                text=f"{m.group(2)} · the encode is still on the X9",
            )
        m = _SYNC_ABORTED.match(text)
        if m:
            return dict(
                base,
                what="sync-failed",
                title=m.group(2),
                text=f"{m.group(1)} — {m.group(3)} · the encode is still on the X9",
            )
        return None
    if kind == "info" and text.startswith("ERROR "):
        title, _, why = text[len("ERROR ") :].partition(": ")
        why = _exhausted(why.replace(_MOVED_ON, ""))
        return dict(
            base,
            what="error",
            title=title,
            text=f"{why} · nothing deleted · row stays red until the "
            f".error marker is removed",
        )
    if kind == "info" and text.startswith("STOP CONDITION:"):
        return dict(
            base,
            what="stopped",
            title="queue empty",
            text=text[len("STOP CONDITION:") :].strip(),
        )
    if kind == "halted":
        body = text[len("HALTED:") :].strip() if text.startswith("HALTED:") else text
        title, _, why = body.partition(" needs a human: ")
        return dict(
            base,
            what="error",
            title=title or body,
            text=f"HALTED (old-style): {why or body} · nothing deleted",
        )
    return None


def key_of(e: dict) -> str:
    ts = e.get("ts") if e.get("src") == "driver" else ""
    return "|".join(
        (e.get("src") or "", e.get("kind") or "", ts or "", e.get("text") or "")
    )


def _gib(b) -> str:
    return f"{b / GIB:.2f} GiB"


def _deletion_text(title: str):
    """(text, size-for-subject) from the newest ledger row for the title.
    `record.py` writes the row BEFORE the sync, so it is the authoritative
    size of what the sync then deleted. A row with no source size (the file
    was gone before it was recorded) says so rather than back-solving."""
    row = None
    for r in core.ledger():  # oldest first
        if (r.get("title") or "").lower() == title.lower():
            row = r
    if row is None:
        return (
            "library original deleted — this title is not in the ledger, "
            "see the History tab",
            None,
        )
    sb, ob = row.get("source_bytes"), row.get("output_bytes")
    out = _gib(ob) if ob else "an unrecorded-size"
    if not sb:
        return (
            f"library original DELETED, replaced by the {out} encode "
            f"(original size not recorded — excluded from every total)",
            None,
        )
    text = f"{_gib(sb)} library original DELETED, replaced by the {out} encode"
    if ob:
        text += f" (frees {_gib(sb - ob)})"
    return text, f"{sb / GIB:.2f}"


def _kill_projection(e: dict):
    """(title, 'projected X% of original at Y% progress') off a KILLED line."""
    title, rest = _split_watcher(e.get("text") or "")
    m = _KILL.search(rest)
    if not m:
        return None
    return title, f"projected {m.group(1)}% of original at {m.group(2)}% progress"


# ----------------------------------------------------------------- format --


def _clock12(ts):
    """'2026-09-01 15:24:46' -> '3:24 PM Tue Sep 1'. The log's own local
    wall-clock text, rewritten — the weekday needs the calendar, but no zone
    is applied, so a browser or box in another zone cannot shift it. A
    message can arrive up to 24 h late (retries), so a bare clock is not
    enough."""
    if not isinstance(ts, str):
        return None
    try:
        d = datetime.datetime.strptime(ts[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    suffix = "AM" if d.hour < 12 else "PM"
    return f"{d.hour % 12 or 12}:{d.minute:02d} {suffix} {d.strftime('%a %b')} {d.day}"


def _line(n: dict) -> str:
    emoji, label = WHATS.get(n.get("what"), ("", "?"))
    parts = [f"{emoji} {label}: {n.get('title')}".strip()]
    if n.get("text"):
        parts.append(str(n["text"]))
    if n.get("detail"):
        parts.append(str(n["detail"]))
    at = _clock12(n.get("ts"))
    if at and n.get("approx"):
        at = f"~{at}, estimated from the watch log's file time"
    return _SEP.join(parts) + (f" (at {at})" if at else "")


def slack_message(n: dict) -> dict:
    return {"text": _line(n)}


def email_message(n: dict) -> dict:
    _, label = WHATS.get(n.get("what"), ("", "?"))
    if n.get("size"):
        label = f"{label} ({n['size']} GiB)"
    return {"subject": f"smeltr: {label} — {n.get('title')}", "body": _line(n) + "\n"}


# -------------------------------------------------------------- transport --


def send_slack(cfg: dict, message: dict) -> None:
    req = Request(
        cfg["webhook"],
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        if not 200 <= resp.status < 300:
            raise OSError(
                f"slack webhook answered {resp.status}: {resp.read()[:200]!r}"
            )


def send_email(cfg: dict, message: dict) -> None:
    msg = EmailMessage()
    msg["Subject"] = message["subject"]
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(message["body"])
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=HTTP_TIMEOUT) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(cfg["user"], cfg["password"])
        s.send_message(msg)


# ----------------------------------------------------------------- config --


def load_config(path: str, err=_err):
    """The parsed config, or None when the feature is off. A missing file is
    silent; a malformed, loose-moded or useless one is loud, because "I set
    it up and nothing arrived" is the failure this line exists to explain.
    Never raises: this runs in server main() above the socket bind."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        err(f"{CONFIG_NAME} unreadable, notifications OFF: {e}")
        return None
    if st.st_mode & 0o077:
        err(
            f"{CONFIG_NAME} is readable by other users and holds a password; "
            f"notifications OFF until you `chmod 600 {path}`"
        )
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        err(f"{CONFIG_NAME} unreadable, notifications OFF: {e}")
        return None
    if not isinstance(raw, dict):
        err(f"{CONFIG_NAME} is not an object, notifications OFF")
        return None
    cfg = {}
    slack = raw.get("slack")
    if isinstance(slack, dict) and slack.get("webhook"):
        cfg["slack"] = {"webhook": str(slack["webhook"])}
    email = raw.get("email")
    if isinstance(email, dict):
        missing = [k for k in ("host", "user", "password", "to") if not email.get(k)]
        try:
            port = int(email.get("port") or 587)
        except (TypeError, ValueError):
            missing.append("port (not a number)")
        if missing:
            err(f"{CONFIG_NAME} email is missing {missing}; email OFF")
        else:
            cfg["email"] = {
                "host": str(email["host"]),
                "port": port,
                "user": str(email["user"]),
                "password": str(email["password"]),
                "to": str(email["to"]),
                "from": str(email.get("from") or email["user"]),
            }
    if not cfg:
        err(f"{CONFIG_NAME} configures no channel, notifications OFF")
        return None
    return cfg


# --------------------------------------------------------------- notifier --


def _valid_item(item) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("note"), dict)
        and isinstance(item.get("sent"), list)
        and isinstance(item.get("since"), (int, float))
        and item["note"].get("what") in WHATS
    )


class Notifier:
    def __init__(
        self,
        dir_,
        config,
        slack=send_slack,
        email=send_email,
        source=None,
        now=time.time,
        err=_err,
    ):
        self.cursor_path = os.path.join(dir_, CURSOR_NAME)
        self.config = config
        self.channels = {}
        if "slack" in config:
            self.channels["slack"] = (slack, slack_message)
        if "email" in config:
            self.channels["email"] = (email, email_message)
        self.source = source or (lambda: events.events()["events"])
        self.now = now
        self.err = err
        self.seen = None  # None = no baseline yet; else an insertion-ordered dict
        self.pending = []
        self.kills = {}  # title -> the last KILLED projection seen, any tick
        # Per-channel backoff: name -> (not-before, current delay).
        self.backoff = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self):
        try:
            with open(self.cursor_path, encoding="utf-8") as fh:
                d = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.err(f"{CURSOR_NAME} unreadable ({e}); starting from a fresh baseline")
            return
        if not isinstance(d, dict):
            self.err(f"{CURSOR_NAME} is not an object; starting from a fresh baseline")
            return
        seen = d.get("seen")
        if isinstance(seen, list) and seen:
            self.seen = dict.fromkeys(str(k) for k in seen)
        raw = d.get("pending")
        raw = raw if isinstance(raw, list) else []
        self.pending = [i for i in raw if _valid_item(i)]
        bad = len(raw) - len(self.pending)
        if bad:
            self.err(f"{CURSOR_NAME}: dropped {bad} malformed pending item(s)")

    def _save(self):
        tmp = f"{self.cursor_path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"seen": list(self.seen or ()), "pending": self.pending}, fh)
            os.replace(tmp, self.cursor_path)
        except OSError as e:
            self.err(f"cannot write {CURSOR_NAME}: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- one pass ----------------------------------------------------------
    def tick(self) -> int:
        """Diff, enqueue, deliver. Returns notifications fully delivered."""
        try:
            snapshot = self.source()
        except Exception as e:  # noqa: BLE001 - a parser crash must not kill the thread
            self.err(f"event source failed: {e}")
            snapshot = []
        if snapshot:
            self._remember_kills(snapshot)
            keys = [key_of(e) for e in reversed(snapshot)]  # oldest first
            if self.seen is None:
                self.seen = dict.fromkeys(keys)
                self._save()
            else:
                fresh = [e for e in reversed(snapshot) if key_of(e) not in self.seen]
                # Union, never replacement — see the module docstring.
                self.seen.update(dict.fromkeys(keys))
                while len(self.seen) > SEEN_CAP:
                    del self.seen[next(iter(self.seen))]
                self._enqueue(fresh)
        return self._deliver()

    def _remember_kills(self, snapshot):
        # Newest-first snapshot: the first hit per title this pass is the
        # latest kill, and it replaces whatever an earlier pass remembered.
        done = set()
        for e in snapshot:
            if e.get("kind") == "killed" and e.get("src") == "watcher":
                k = _kill_projection(e)
                if k and k[0] not in done:
                    done.add(k[0])
                    self.kills[k[0]] = k[1]

    def _enqueue(self, fresh):
        notes = []
        for e in fresh:
            n = classify(e, err=self.err)
            if n is None:
                continue
            if n["what"] == "deleted":
                n["text"], n["size"] = _deletion_text(n["title"])
            elif n["what"] in ("ladder-up", "ladder-down"):
                k = self.kills.pop(n["title"], None)
                if k:
                    n["text"] = f"{k} — {n['text']}"
            elif n["what"] == "error":
                n["detail"] = e.get("detail")
            notes.append(n)
        if len(notes) > BURST_CAP:
            # Severity decides what survives, NEWEST first within a class:
            # oldest-first capping once kept ten ladder lines and dropped
            # the newest deletion.
            order = {n_id: i for i, n_id in enumerate(map(id, notes))}
            ranked = sorted(
                notes,
                key=lambda n: (
                    PRIORITY.index(n["what"])
                    if n["what"] in PRIORITY
                    else len(PRIORITY),
                    -order[id(n)],
                ),
            )
            kept, dropped = ranked[:BURST_CAP], ranked[BURST_CAP:]
            counts = {}
            for n in dropped:
                label = WHATS.get(n["what"], ("", n["what"]))[1]
                counts[label] = counts.get(label, 0) + 1
            what = ", ".join(f"{c} × {label}" for label, c in counts.items())
            notes = kept + [
                {
                    "what": "burst",
                    "title": f"{len(dropped)} more not sent",
                    "text": f"{what} — see the dashboard's Events tab",
                    "ts": None,
                    "approx": False,
                    "detail": None,
                }
            ]
        for n in notes:
            self.pending.append({"note": n, "sent": [], "since": self.now()})
        if notes:
            self._save()

    def _channel_open(self, name) -> bool:
        nb = self.backoff.get(name)
        return nb is None or self.now() >= nb[0]

    def _channel_failed(self, name):
        delay = min(
            RETRY_CAP_SECONDS,
            (self.backoff[name][1] * 2) if name in self.backoff else RETRY_SECONDS,
        )
        self.backoff[name] = (self.now() + delay, delay)

    def _deliver(self) -> int:
        done = 0
        keep = []
        changed = False
        tried = set()
        for item in self.pending:
            if self.now() - item["since"] > MAX_PENDING_SECONDS:
                self.err(
                    f"dropped after {MAX_PENDING_SECONDS // 3600} h unsent: "
                    f"{_line(item['note'])}"
                )
                changed = True
                continue
            for name, (send, fmt) in self.channels.items():
                if (
                    name in item["sent"]
                    or name in tried
                    or not self._channel_open(name)
                ):
                    continue
                try:
                    send(self.config[name], fmt(item["note"]))
                except Exception as e:  # noqa: BLE001 - any transport failure is "retry later"
                    self._channel_failed(name)
                    tried.add(name)  # one failure per channel per tick
                    self.err(
                        f"{name} send failed, retrying in "
                        f"{self.backoff[name][1]} s: {e}"
                    )
                    continue
                self.backoff.pop(name, None)
                item["sent"].append(name)
                # Persist the landed half NOW: a SIGKILL inside the next
                # channel's 15 s handshake must not resend this one.
                self._save()
            if set(item["sent"]) >= set(self.channels):
                done += 1
                changed = True
            else:
                keep.append(item)
        if changed:
            self.pending = keep
            self._save()
        return done


def start(dir_=None) -> bool:
    """Start the daemon thread if notify.json configures a channel.
    Returns whether it started, so main() can log it. Never raises: this
    runs above the server's socket bind, and an optional notifier must not
    be able to take the page down."""
    try:
        dir_ = dir_ or core.SMELTR_DIR
        cfg = load_config(os.path.join(dir_, CONFIG_NAME))
        if cfg is None:
            return False
        n = Notifier(dir_, cfg)
    except Exception as e:  # noqa: BLE001 - see the docstring
        _err(f"could not start, notifications OFF: {e}")
        return False

    def loop():
        last_rev = None
        while True:
            try:
                rev = events.rev()
                if rev != last_rev or n.pending:
                    last_rev = rev
                    n.tick()
            except Exception as e:  # noqa: BLE001 - the thread must outlive any one failure
                _err(f"tick failed: {e}")
            time.sleep(POLL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="smeltr-notify").start()
    return True


def _main(argv) -> int:
    """`smeltr notify-test`: one test message through every configured
    channel, so the webhook and the app password are proven before the first
    real event. Never touches the cursor."""
    if argv[1:] != ["--test"]:
        print("usage: notify.py --test", file=sys.stderr)
        return 2
    cfg = load_config(os.path.join(core.SMELTR_DIR, CONFIG_NAME))
    if cfg is None:
        print(
            f"smeltr notify: no usable {CONFIG_NAME} in {core.SMELTR_DIR} — "
            "nothing to send. See README.md for the format (and chmod 600 it).",
            file=sys.stderr,
        )
        return 1
    note = {
        "what": "test",
        "title": "smeltr notify-test",
        "text": "if you can read this, this channel works",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "detail": None,
    }
    failed = 0
    for name, (send, fmt) in (
        ("slack", (send_slack, slack_message)),
        ("email", (send_email, email_message)),
    ):
        if name not in cfg:
            print(f"{name}: not configured")
            continue
        try:
            send(cfg[name], fmt(note))
            print(f"{name}: sent")
        except Exception as e:  # noqa: BLE001 - report every channel, then exit non-zero
            print(f"{name}: FAILED — {e}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
