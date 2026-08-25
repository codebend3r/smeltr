"""System resource sampler for the dashboard's monitor strip.

Samples CPU / GPU / memory / network / disk once per second on a daemon
thread, keeps 24 h of history, and persists it to a fixed-size binary ring
file so a `./smeltr restart` costs a few seconds of gap, not the whole chart.

Imported ONLY by server.py. Nothing in the decision path (core / verdict /
next_title / record) ever loads this module; if every sampler here breaks,
the pipeline keeps encoding and only the monitor strip goes blank.

Sources, all root-free and dependency-free:
  cpu   ctypes -> host_statistics(HOST_CPU_LOAD_INFO)   (no subprocess)
  gpu   `ioreg -r -d 1 -c IOAccelerator`   "Device Utilization %"
  ram   `vm_stat`                          Activity-Monitor-style "used"
  net   `netstat -ib`                      per-interface cumulative bytes
  disk  `ioreg -r -c IOBlockStorageDriver` cumulative read/write bytes

Rates are deltas between consecutive ticks. A counter that goes BACKWARDS
(interface bounce, driver reset) yields None for that tick, never a negative
or invented number. A metric that cannot be read yields None. A second with
no sample at all renders as a gap in the chart -- the honest shape.

The ring file: 16-byte header, then 86400 slots of 32 bytes
(`<I7f`: epoch-seconds uint32, then cpu% gpu% ram% net_in net_out disk_r
disk_w as float32, NaN = missing). Slot index = ts % 86400, so a day wraps
in place and the file never grows or needs compaction. Stale slots (ts older
than 24 h) are skipped at read time and overwritten in their turn.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import os
import re
import struct
import subprocess
import sys
import threading
import time

SLOTS = 86400                       # one slot per second, 24 h
SLOT = struct.Struct("<I7f")        # ts + cpu gpu ram net_in net_out disk_r disk_w
SLOT_BYTES = SLOT.size              # 32
MAGIC = b"SMLTRMON1\0\0\0\0\0\0\0"  # 16 bytes; bump the digit to invalidate
N_METRICS = 7
RING_NAME = "sysmon.ring"

_NAN = float("nan")


# --------------------------------------------------------------- pure parsers
# Each takes raw command output so the tests can feed fixtures. They return
# cumulative counters or absolute readings; rate() turns counters into /s.

def parse_ioreg_gpu(text: str):
    """Max "Device Utilization %" across accelerators, or None if absent."""
    vals = [int(m) for m in re.findall(r'"Device Utilization %"=(\d+)', text)]
    return max(vals) if vals else None


def parse_ioreg_disk(text: str):
    """(read_bytes, write_bytes) summed across all block-storage drivers."""
    reads = [int(m) for m in re.findall(r'"Bytes \(Read\)"=(\d+)', text)]
    writes = [int(m) for m in re.findall(r'"Bytes \(Write\)"=(\d+)', text)]
    if not reads and not writes:
        return None
    return (sum(reads), sum(writes))


def parse_netstat(text: str):
    """(in_bytes, out_bytes) summed over physical <Link#> rows.

    lo0 is local chatter, not traffic; utun* double-counts a tunnel's bytes
    against the physical interface that actually carries them; bridge*
    double-counts its member interface the same way; gif/stf are dead 6to4
    shims; awdl/llw (AirDrop sidecar), ap* (hotspot AP) and anpi* (Apple
    private interconnect) are radio/virtual chatter, not the wire this
    chart is watching. Rows with and without a MAC address have different
    column counts, so the byte fields are indexed from the END of the row.
    """
    total_in = total_out = 0
    seen = False
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or not parts[2].startswith("<Link#"):
            continue
        name = parts[0].rstrip("*")
        if name.startswith(("lo", "utun", "gif", "stf", "bridge",
                            "awdl", "llw", "ap", "anpi")):
            continue
        try:
            total_in += int(parts[-5])
            total_out += int(parts[-2])
        except ValueError:
            continue
        seen = True
    return (total_in, total_out) if seen else None


def parse_vm_stat(text: str, total_bytes: int):
    """Memory used %, Activity-Monitor style: (anonymous - purgeable) + wired
    + compressor-occupied, over physical RAM. None if the shape changed."""
    page = 4096
    m = re.search(r"page size of (\d+)", text)
    if m:
        page = int(m.group(1))
    fields = {}
    for line in text.splitlines():
        m = re.match(r'"?([A-Za-z -]+)"?:\s+(\d+)\.', line)
        if m:
            fields[m.group(1).strip()] = int(m.group(2))
    try:
        used_pages = (fields["Anonymous pages"] - fields["Pages purgeable"]
                      + fields["Pages wired down"]
                      + fields["Pages occupied by compressor"])
    except KeyError:
        return None
    if total_bytes <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * used_pages * page / total_bytes))


def cpu_pct(prev_ticks, cur_ticks):
    """Busy % from two HOST_CPU_LOAD_INFO readings (USER SYS IDLE NICE)."""
    if prev_ticks is None or cur_ticks is None:
        return None
    d = [c - p for p, c in zip(prev_ticks, cur_ticks)]
    total = sum(d)
    if total <= 0 or any(x < 0 for x in d):   # counter reset / no time passed
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - d[2] / total)))


def rate(prev, cur, dt):
    """Bytes/s from two cumulative counters. None on reset (negative) or a
    dt that would divide a lie into the number."""
    if prev is None or cur is None or dt <= 0:
        return None
    d = cur - prev
    if d < 0:
        return None
    return d / dt


# ------------------------------------------------------------------ ring file

class Ring:
    """Fixed-size on-disk ring of one sample per second, 24 h deep.

    write() is one pwrite per second; load() reads the whole file once at
    startup. A header mismatch (older layout, torn create) recreates the file
    empty rather than misreading 2.7 MB of stale bytes as data.
    """

    def __init__(self, path: str):
        self.path = path
        self._fd = None
        self._open()

    def _open(self):
        size = 16 + SLOTS * SLOT_BYTES
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            header = os.pread(fd, 16, 0)
            if header != MAGIC or os.fstat(fd).st_size != size:
                os.ftruncate(fd, 0)
                os.ftruncate(fd, size)
                os.pwrite(fd, MAGIC, 0)
            self._fd = fd
        except OSError as exc:
            print(f"sysmon: ring unavailable ({exc}); history is memory-only",
                  file=sys.stderr)
            self._fd = None

    def write(self, ts: int, values):
        self.write_packed(ts, SLOT.pack(ts, *[_NAN if v is None else float(v)
                                              for v in values]))

    def write_packed(self, ts: int, packed: bytes):
        if self._fd is None:
            return
        try:
            os.pwrite(self._fd, packed, 16 + (ts % SLOTS) * SLOT_BYTES)
            self._fails = 0
        except OSError as exc:
            # An unlinked file or unmounted volume fails every write from
            # here on; say so once and try a reopen rather than rotting
            # silently until the next restart reveals an empty chart.
            self._fails = getattr(self, "_fails", 0) + 1
            if self._fails == 3:
                print(f"sysmon: ring writes failing ({exc}); reopening",
                      file=sys.stderr)
                self.close()
                self._open()

    def raw(self) -> bytes:
        """The whole slots region, b"" if the ring is unavailable."""
        if self._fd is None:
            return b""
        try:
            return os.pread(self._fd, SLOTS * SLOT_BYTES, 16)
        except OSError:
            return b""

    def load(self, now: int):
        """Every slot still inside the 24 h window, as {ts: [7 floats|None]}."""
        out = {}
        if self._fd is None:
            return out
        try:
            raw = os.pread(self._fd, SLOTS * SLOT_BYTES, 16)
        except OSError:
            return out
        floor = now - SLOTS
        for i in range(0, len(raw) - SLOT_BYTES + 1, SLOT_BYTES):
            ts = SLOT.unpack_from(raw, i)[0]
            if ts == 0 or ts <= floor or ts > now + 2:
                continue
            vals = SLOT.unpack_from(raw, i)[1:]
            out[ts] = [None if math.isnan(v) else v for v in vals]
        return out

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


# ------------------------------------------------------------------- sampler

def _run(cmd, timeout=5):
    """subprocess.run()'s timeout path ends in an UNBOUNDED wait() after the
    kill -- an `ioreg` stuck in an uninterruptible IOKit wait (a wedged USB
    enumeration, exactly the sick-X9 case) would park the sampler thread
    forever. This version abandons an unkillable child instead."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return ""
    try:
        out, _ = proc.communicate(timeout=timeout)
        return out or ""
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass                              # D-state: abandon, never block
        return ""
    except OSError:
        return ""


class _Mach:
    """ctypes handle for host_statistics. Fixed 4-uint32 ABI since 10.0."""

    def __init__(self):
        self.ok = False
        if sys.platform != "darwin":
            return
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            libc.mach_host_self.restype = ctypes.c_uint
            self._host = libc.mach_host_self()
            self._stats = libc.host_statistics
            self.ok = True
        except (OSError, AttributeError):
            pass

    def cpu_ticks(self):
        if not self.ok:
            return None
        buf = (ctypes.c_uint32 * 4)()
        count = ctypes.c_uint32(4)
        if self._stats(self._host, 3,          # HOST_CPU_LOAD_INFO
                       ctypes.byref(buf), ctypes.byref(count)) != 0:
            return None
        return list(buf)


class Sampler:
    """The 1 Hz loop. The in-memory mirror is a bytearray in the SAME
    packed `<I7f` layout as the ring file -- 2.8 MB instead of ~19 MB of
    Python objects, and serving history is a memcpy under the lock plus a
    filter outside it, never 50 ms of struct.pack while the sampler and
    every SSE thread wait."""

    def __init__(self, directory: str):
        self.ring = Ring(os.path.join(directory, RING_NAME))
        self._mach = _Mach()
        self._mem_total = self._read_mem_total()
        self._lock = threading.Lock()
        self._buf = bytearray(SLOTS * SLOT_BYTES)   # packed mirror of the ring
        self._latest = None                   # (ts, [7 floats|None])
        raw = self.ring.raw()
        if len(raw) == len(self._buf):
            self._buf[:] = raw                # stale slots filtered at serve time
        self._prev_cpu = None                 # ticks
        self._prev_net = None                 # (t, (in, out))
        self._prev_disk = None                # (t, (read, write))

    @staticmethod
    def _read_mem_total():
        try:
            out = _run(["sysctl", "-n", "hw.memsize"]).strip()
            return int(out) if out else 0
        except ValueError:
            return 0

    @staticmethod
    def _rate2(prev, cur):
        """Per-counter rates with per-counter timestamps: each cumulative
        counter is stamped WHEN it was read, not when the tick started --
        the subprocesses before it cost 30-190 ms under load, and a shared
        start-of-tick dt turned that jitter into phantom throughput spikes
        correlated with real load."""
        if prev is None or cur is None:
            return (None, None)
        (pt, pv), (t, v) = prev, cur
        dt = t - pt
        return (rate(pv[0], v[0], dt), rate(pv[1], v[1], dt))

    def _tick(self):
        ticks = self._mach.cpu_ticks()
        gpu = parse_ioreg_gpu(_run(["ioreg", "-r", "-d", "1", "-c",
                                    "IOAccelerator"]))
        ram = parse_vm_stat(_run(["vm_stat"]), self._mem_total)
        net_raw = parse_netstat(_run(["netstat", "-ib"]))
        net = None if net_raw is None else (time.time(), net_raw)
        disk_raw = parse_ioreg_disk(_run(["ioreg", "-r", "-w0", "-c",
                                          "IOBlockStorageDriver"]))
        disk = None if disk_raw is None else (time.time(), disk_raw)
        ts = int(time.time())

        pticks, pnet, pdisk = self._prev_cpu, self._prev_net, self._prev_disk
        self._prev_cpu = ticks
        if net is not None:
            self._prev_net = net
        if disk is not None:
            self._prev_disk = disk
        if pticks is None and pnet is None and pdisk is None:
            return                            # rates need two readings
        net_in, net_out = self._rate2(pnet, net)
        disk_r, disk_w = self._rate2(pdisk, disk)
        self._store(ts, [
            cpu_pct(pticks, ticks),
            None if gpu is None else float(gpu),
            ram,
            net_in, net_out, disk_r, disk_w,
        ])

    def _store(self, ts: int, vals):
        packed = SLOT.pack(ts, *[_NAN if v is None else float(v)
                                 for v in vals])
        off = (ts % SLOTS) * SLOT_BYTES
        with self._lock:
            self._buf[off:off + SLOT_BYTES] = packed
            self._latest = (ts, vals)
        self.ring.write_packed(ts, packed)

    def _loop(self):
        while True:
            started = time.time()
            try:
                self._tick()
            except Exception as exc:          # never let one bad read kill 24 h
                print(f"sysmon: tick failed: {exc!r}", file=sys.stderr)
            took = time.time() - started
            if took > 5.0:                    # a stalled tick must leave a trace
                print(f"sysmon: tick took {took:.1f}s (a probe is stalling)",
                      file=sys.stderr)
            time.sleep(max(0.05, 1.0 - took))

    def start(self):
        threading.Thread(target=self._loop, name="sysmon", daemon=True).start()
        return self

    # ------------------------------------------------------------- serving
    @staticmethod
    def _sample_dict(ts, vals):
        return {"t": ts, "v": [None if v is None or math.isnan(v)
                               else round(v, 1) for v in vals]}

    def latest(self):
        """{"t": ts, "v": [7 numbers|null]} for the newest sample, or None."""
        with self._lock:
            if self._latest is None:
                return None
            ts, vals = self._latest
        return self._sample_dict(ts, vals)

    def since(self, last_t, cap: int = 60):
        """Every sample newer than last_t, oldest first -- the SSE stream is
        a CURSOR, not a latest-snapshot: a slow iteration (build_state on a
        cold NAS) must never turn seconds the sampler recorded into gaps the
        chart then presents as a sampler outage. last_t=None means a fresh
        connection: it gets only the newest sample (history covers the rest).
        A stall longer than cap seconds drops the excess; the page's
        reconnect refetch is the backstop."""
        with self._lock:
            if self._latest is None:
                return []
            lt, lvals = self._latest
            if last_t is None or last_t >= lt:
                return [] if last_t == lt else [self._sample_dict(lt, lvals)]
            out = []
            for ts in range(max(last_t + 1, lt - cap + 1), lt + 1):
                off = (ts % SLOTS) * SLOT_BYTES
                rec = SLOT.unpack_from(self._buf, off)
                if rec[0] != ts:
                    continue
                out.append(self._sample_dict(ts, rec[1:]))
            return out

    def history_bytes(self):
        """Every in-window sample, oldest first, as raw `<I7f` records.
        The lock is held only for the mirror memcpy (~1 ms); the 86400-slot
        filter runs on the snapshot."""
        now = int(time.time())
        floor = now - SLOTS
        with self._lock:
            snap = bytes(self._buf)
        chunks = []
        start = (now + 1) % SLOTS             # oldest slot, walking forward
        for k in range(SLOTS):
            off = ((start + k) % SLOTS) * SLOT_BYTES
            ts = int.from_bytes(snap[off:off + 4], "little")
            if ts == 0 or ts <= floor or ts > now + 2:
                continue
            chunks.append(snap[off:off + SLOT_BYTES])
        return b"".join(chunks)


_sampler = None


def start(directory: str) -> Sampler:
    global _sampler
    if _sampler is None:
        _sampler = Sampler(directory).start()
    return _sampler


def get() -> Sampler | None:
    return _sampler
