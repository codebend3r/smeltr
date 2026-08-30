"""sysmon: the parsers, the rate math, and the ring file.

The parsers run against captured fixture output so a macOS format drift shows
up as a failing test, not a silently flat chart. The ring tests pin the
wrap-in-place layout, stale-slot invalidation at read time, and the header
check that recreates (never misreads) an older file.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard import sysmon  # noqa: E402


IOREG_GPU = '''
+-o AGXAcceleratorG13X  <class AGXAcceleratorG13X, id 0x100000287>
    {
      "PerformanceStatistics" = {"Device Utilization %"=37,"Renderer Utilization %"=35}
      "IOClass" = "AGXAcceleratorG13X"
    }
'''

IOREG_DISK = '''
+-o AppleAPFSMedia  <class IOBlockStorageDriver>
      "Statistics" = {"Bytes (Read)"=60399001600,"Bytes (Write)"=24870031360}
+-o Disk2  <class IOBlockStorageDriver>
      "Statistics" = {"Bytes (Read)"=111092603904,"Bytes (Write)"=39086864896}
'''

NETSTAT = '''\
Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                        114624     0 1142291140   114624     0 1142291140     0
lo0        16384 127           localhost         114624     - 1142291140   114624     - 1142291140     -
gif0*      1280  <Link#2>                             0     0          0        0     0          0     0
stf0*      1280  <Link#3>                             0     0          0        0     0          0     0
en0        1500  <Link#9>    a4:cf:99:9c:14:99  7000000     0 9000000000  5000000     0 1000000000     0
en0        1500  fe80::1     fe80:9::1          7000000     - 9000000000  5000000     - 1000000000     -
utun0      1380  <Link#17>                          100     0      50000      100     0      40000     0
awdl0      1484  <Link#12>   aa:bb:cc:dd:ee:ff      500     0     250000      400     0     150000     0
bridge0    1500  <Link#20>   36:d1:45:f3:0a:00     9000     0    4000000     8000     0    3000000     0
en1        1500  <Link#10>   a4:cf:99:9c:14:9a     1000     0    2000000      900     0    1000000     0
'''

VM_STAT = '''\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     5555.
Pages active:                                1157535.
Pages inactive:                              1172923.
Pages speculative:                           1647101.
Pages throttled:                                   0.
Pages wired down:                             160384.
Pages purgeable:                                8193.
"Translation faults":                      614987016.
Pages copy-on-write:                       101502256.
Anonymous pages:                             1594188.
Pages stored in compressor:                     7720.
Pages occupied by compressor:                   1871.
Pageins:                                    10050437.
'''


class TestParsers(unittest.TestCase):
    def test_gpu(self):
        self.assertEqual(sysmon.parse_ioreg_gpu(IOREG_GPU), 37)

    def test_gpu_absent_is_none_not_zero(self):
        # No accelerator reading must render as a gap, never a flat 0% line.
        self.assertIsNone(sysmon.parse_ioreg_gpu("+-o Something\n"))

    def test_gpu_multiple_takes_max(self):
        two = IOREG_GPU + IOREG_GPU.replace("=37", "=61")
        self.assertEqual(sysmon.parse_ioreg_gpu(two), 61)

    def test_disk_sums_all_drivers(self):
        self.assertEqual(sysmon.parse_ioreg_disk(IOREG_DISK),
                         (60399001600 + 111092603904,
                          24870031360 + 39086864896))

    def test_disk_absent(self):
        self.assertIsNone(sysmon.parse_ioreg_disk(""))

    def test_netstat_sums_physical_only(self):
        # en0 + en1 only: lo0 (chatter), utun0 and bridge0 (double count),
        # gif/stf (dead), awdl (AirDrop sidecar) are all excluded. Only
        # <Link#> rows count -- per-address rows repeat the same counters.
        self.assertEqual(sysmon.parse_netstat(NETSTAT),
                         (9000000000 + 2000000, 1000000000 + 1000000))

    def test_netstat_empty(self):
        self.assertIsNone(sysmon.parse_netstat("Name Mtu\n"))

    def test_vm_stat_activity_monitor_used(self):
        total = 68719476736
        pct = sysmon.parse_vm_stat(VM_STAT, total)
        used = (1594188 - 8193 + 160384 + 1871) * 16384
        self.assertAlmostEqual(pct, 100.0 * used / total, places=3)

    def test_vm_stat_missing_field_is_none(self):
        self.assertIsNone(sysmon.parse_vm_stat("Pages free: 5.\n", 1))

    def test_vm_stat_zero_total_is_none(self):
        self.assertIsNone(sysmon.parse_vm_stat(VM_STAT, 0))


class TestRateMath(unittest.TestCase):
    def test_cpu_pct(self):
        self.assertAlmostEqual(
            sysmon.cpu_pct([0, 0, 0, 0], [948, 44, 22, 0]),
            100.0 * (1 - 22 / 1014), places=3)

    def test_cpu_counter_reset_is_none(self):
        self.assertIsNone(sysmon.cpu_pct([100, 100, 100, 0], [50, 100, 100, 0]))

    def test_cpu_no_elapsed_ticks_is_none(self):
        self.assertIsNone(sysmon.cpu_pct([1, 2, 3, 4], [1, 2, 3, 4]))

    def test_rate(self):
        self.assertAlmostEqual(sysmon.rate(1000, 3000, 2.0), 1000.0)

    def test_rate_counter_reset_is_none_never_negative(self):
        self.assertIsNone(sysmon.rate(3000, 1000, 1.0))

    def test_rate_missing_side_is_none(self):
        self.assertIsNone(sysmon.rate(None, 100, 1.0))
        self.assertIsNone(sysmon.rate(100, None, 1.0))


class TestRing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "sysmon.ring")

    def tearDown(self):
        self.dir.cleanup()

    def test_write_and_reload(self):
        r = sysmon.Ring(self.path)
        now = 1_700_000_000
        r.write(now, [50.0, None, 40.0, 1e6, 2e6, 3e6, 4e6])
        r.close()
        r2 = sysmon.Ring(self.path)
        loaded = r2.load(now)
        self.assertIn(now, loaded)
        vals = loaded[now]
        self.assertAlmostEqual(vals[0], 50.0, places=3)
        self.assertIsNone(vals[1])            # NaN round-trips to None
        self.assertAlmostEqual(vals[3], 1e6, delta=1)
        r2.close()

    def test_stale_slot_skipped_at_read(self):
        # A slot written >24 h ago occupies the same index as "now" would;
        # load() must drop it rather than resurrect yesterday's spike.
        r = sysmon.Ring(self.path)
        old = 1_700_000_000
        r.write(old, [10.0] * 7)
        loaded = r.load(old + sysmon.SLOTS + 5)
        self.assertEqual(loaded, {})
        r.close()

    def test_wrap_overwrites_in_place(self):
        r = sysmon.Ring(self.path)
        t1 = 1_700_000_000
        t2 = t1 + sysmon.SLOTS                # same slot, one day later
        r.write(t1, [10.0] * 7)
        r.write(t2, [20.0] * 7)
        loaded = r.load(t2)
        self.assertNotIn(t1, loaded)
        self.assertAlmostEqual(loaded[t2][0], 20.0, places=3)
        size = os.path.getsize(self.path)
        self.assertEqual(size, 16 + sysmon.SLOTS * sysmon.SLOT_BYTES)
        r.close()

    def test_bad_header_recreates_empty(self):
        with open(self.path, "wb") as f:
            f.write(b"NOT A RING" + b"\0" * 100)
        r = sysmon.Ring(self.path)
        self.assertEqual(r.load(1_700_000_000), {})
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(16), sysmon.MAGIC)
        r.close()

    def test_slot_layout_is_pinned(self):
        # The browser parses these exact 32 bytes with a DataView; a layout
        # change must be a deliberate, versioned act (bump MAGIC).
        self.assertEqual(sysmon.SLOT_BYTES, 32)
        self.assertEqual(sysmon.SLOT.format, "<I7f")
        self.assertEqual(len(sysmon.MAGIC), 16)


class TestHistoryServing(unittest.TestCase):
    def test_history_bytes_ordered_and_windowed(self):
        with tempfile.TemporaryDirectory() as d:
            s = sysmon.Sampler(d)             # no .start(): no thread
            import time as _t
            now = int(_t.time())
            for k, ts in enumerate([now - 10, now - 5, now - 2]):
                s._store(ts, [float(k)] * 7)
            stale = now - sysmon.SLOTS - 100
            if s._buf[(stale % sysmon.SLOTS) * sysmon.SLOT_BYTES] == 0:
                s._store(stale, [99.0] * 7)   # don't clobber a test slot
            raw = s.history_bytes()
            self.assertEqual(len(raw) % sysmon.SLOT_BYTES, 0)
            stamps = [sysmon.SLOT.unpack_from(raw, o)[0]
                      for o in range(0, len(raw), sysmon.SLOT_BYTES)]
            self.assertEqual(stamps, sorted(stamps))
            self.assertIn(now - 10, stamps)
            self.assertNotIn(stale, stamps)

    def test_history_bytes_span_narrows_the_window(self):
        # The page asks for the window it is drawing: the ring is 7 d /
        # ~19 MB and the default zoom shows a day of it. `span` may only
        # NARROW -- a caller asking for more than the ring holds gets the
        # ring, and one asking for nothing at all still gets the ring.
        with tempfile.TemporaryDirectory() as d:
            s = sysmon.Sampler(d)             # no .start(): no thread
            now = int(time.time())
            inside, outside = now - 100, now - 4000
            s._store(inside, [1.0] * 7)
            s._store(outside, [2.0] * 7)

            def stamps(*args):
                raw = s.history_bytes(*args)
                return [sysmon.SLOT.unpack_from(raw, o)[0]
                        for o in range(0, len(raw), sysmon.SLOT_BYTES)]

            self.assertIn(outside, stamps())            # default: whole ring
            wide = stamps(3600)
            self.assertIn(inside, wide)
            self.assertNotIn(outside, wide)             # older than the span
            self.assertEqual(stamps(sysmon.SLOTS * 99), stamps())
            self.assertEqual(stamps(0), stamps(1))      # clamped, never empty-by-zero

    def test_history_bytes_coalesces_runs_across_the_wrap(self):
        # Records are emitted as contiguous runs now, not one slice per
        # slot. The ring wraps at SLOTS-1 -> 0, so the run must BREAK
        # there and still come out oldest-first: a run that walked past
        # the wrap would splice the newest samples in front of the oldest.
        with tempfile.TemporaryDirectory() as d:
            s = sysmon.Sampler(d)
            now = int(time.time())
            edge = (now // sysmon.SLOTS) * sysmon.SLOTS - 1   # slot SLOTS-1
            want = [edge - 1, edge, edge + 1, edge + 2]
            if want[0] <= now - sysmon.SLOTS or want[-1] > now:
                self.skipTest("the wrap point is not inside the live window")
            for ts in want:
                s._store(ts, [float(ts % 7)] * 7)
            raw = s.history_bytes()
            got = [sysmon.SLOT.unpack_from(raw, o)[0]
                   for o in range(0, len(raw), sysmon.SLOT_BYTES)]
            self.assertEqual([t for t in got if t in want], want)
            self.assertEqual(got, sorted(got))


class TestSinceCursor(unittest.TestCase):
    """The SSE stream is a cursor. A slow server iteration must ship the
    seconds it stepped over, not drop them -- a dropped second renders as a
    chart gap, and a gap claims the sampler could not read that second."""

    def _sampler(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        return sysmon.Sampler(self.dir.name)

    def test_fresh_connection_gets_only_newest(self):
        s = self._sampler()
        for ts in (1000, 1001, 1002):
            s._store(ts, [1.0] * 7)
        out = s.since(None)
        self.assertEqual([m["t"] for m in out], [1002])

    def test_cursor_ships_every_missed_second(self):
        s = self._sampler()
        for ts in (1000, 1001, 1002, 1003):
            s._store(ts, [float(ts % 10)] * 7)
        out = s.since(1000)
        self.assertEqual([m["t"] for m in out], [1001, 1002, 1003])

    def test_cursor_skips_seconds_the_sampler_missed(self):
        s = self._sampler()
        s._store(1000, [1.0] * 7)
        s._store(1003, [2.0] * 7)             # 1001-1002 never sampled
        self.assertEqual([m["t"] for m in s.since(1000)], [1003])

    def test_caught_up_cursor_gets_nothing(self):
        s = self._sampler()
        s._store(1000, [1.0] * 7)
        self.assertEqual(s.since(1000), [])

    def test_cap_bounds_a_long_stall(self):
        s = self._sampler()
        for ts in range(1000, 1100):
            s._store(ts, [1.0] * 7)
        out = s.since(1000, cap=10)
        self.assertEqual(len(out), 10)
        self.assertEqual(out[-1]["t"], 1099)  # newest survives the cap

    def test_none_metric_round_trips_as_null(self):
        s = self._sampler()
        s._store(1000, [50.0, None, 40.0, 1.0, 2.0, 3.0, 4.0])
        s._store(1001, [51.0, None, 41.0, 1.0, 2.0, 3.0, 4.0])
        out = s.since(1000)
        self.assertIsNone(out[0]["v"][1])
        self.assertAlmostEqual(out[0]["v"][0], 51.0, places=1)

    def test_empty_sampler_yields_nothing(self):
        s = self._sampler()
        self.assertEqual(s.since(None), [])
        self.assertEqual(s.since(999), [])


if __name__ == "__main__":
    unittest.main()
