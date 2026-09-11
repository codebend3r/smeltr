#!/usr/bin/env python3
"""Run every `tests/test_*.py` module in its own process, all at once, and
stop at the first failure.

`bun run test:py` (2026-09-11, operator's rule: parallel, and stop on the
first error). Stdlib unittest has no parallel runner, so this is the
smallest one that keeps its semantics: each module is launched with EXACTLY
the command the sequential form used --

    python3 -W error::ResourceWarning -m unittest discover --failfast \\
        -s tests -p <module>.py

-- so imports, warnings and the per-module `--failfast` are unchanged; only
the modules overlap. Up to `os.cpu_count()` run at once. The first module
that exits non-zero has its full output printed, every other live module is
terminated, and the run exits 1 -- nothing after the first failure is
reported, because nothing after it ran to completion.

Output is one dot per test as each module completes and one summary line,
the same shape as the other suites. Every module isolates its writes (a
temp dir or a swapped `SMELTR_DIR`), which is what makes this safe; a new
module that writes beside the live ledger is wrong for the sequential form
too, this just finds it sooner.
"""
import glob
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UNITTEST = [
    sys.executable, "-W", "error::ResourceWarning", "-m", "unittest",
    "discover", "--failfast", "-s", "tests",
]
RAN = re.compile(r"^Ran (\d+) tests? in", re.M)


def main() -> int:
    mods = sorted(
        os.path.basename(p)[:-3] for p in glob.glob(os.path.join(HERE, "test_*.py"))
    )
    if not mods:
        print("no tests/test_*.py found", file=sys.stderr)
        return 1
    width = max(1, os.cpu_count() or 4)
    pending = list(mods)
    live = {}  # Popen -> module
    t0 = time.monotonic()
    ran = 0
    done = 0
    try:
        while pending or live:
            while pending and len(live) < width:
                m = pending.pop(0)
                live[subprocess.Popen(
                    UNITTEST + ["-p", m + ".py"], cwd=ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )] = m
            for p in list(live):
                if p.poll() is None:
                    continue
                m = live.pop(p)
                out = p.stdout.read().decode("utf-8", "replace")
                p.stdout.close()
                if p.returncode != 0:
                    sys.stdout.write("\nFAIL %s (exit %d)\n%s" % (m, p.returncode, out))
                    sys.stdout.flush()
                    _stop(live)
                    return 1
                hit = RAN.search(out)
                if not hit:
                    sys.stdout.write("\nFAIL %s: no 'Ran N tests' line\n%s" % (m, out))
                    sys.stdout.flush()
                    _stop(live)
                    return 1
                ran += int(hit.group(1))
                done += 1
                sys.stdout.write("." * int(hit.group(1)))
                sys.stdout.flush()
            time.sleep(0.02)
    except KeyboardInterrupt:
        _stop(live)
        return 130
    sys.stdout.write(
        "\nRan %d tests in %d modules, %.1fs\n\nOK\n" % (ran, done, time.monotonic() - t0)
    )
    return 0


def _stop(live) -> None:
    for p in live:
        try:
            p.terminate()
        except OSError:
            pass
    for p in live:
        try:
            p.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            p.kill()
        finally:
            if p.stdout:
                p.stdout.close()


if __name__ == "__main__":
    sys.exit(main())
