#!/usr/bin/env python3
"""Run the whole vsmp suite and aggregate the results.

    python3 tests/run.py                 # all modules
    python3 tests/run.py test_extract    # one module

Some checks need a feature-length movie, which is not committed. They are
skipped unless one is available:

    VSMP_TEST_MOVIE=/path/to/movie.mp4 python3 tests/run.py

If a single .mp4 sits beside the repo it is picked up automatically.

Requires ffmpeg and ffprobe on PATH, and Pillow. Does not require a Raspberry Pi
or an e-paper panel -- tests/stubs stands in for the driver.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
MODULES = ['test_extract', 'test_state_loop', 'test_e2e', 'test_watchdog']

RESULT = re.compile(r'HARNESS_RESULT pass=(\d+) fail=(\d+) skip=(\d+)')

MODULE_TIMEOUT_S = 300


def main():
    wanted = sys.argv[1:] or MODULES
    unknown = [m for m in wanted if m not in MODULES]
    if unknown:
        sys.exit("unknown module(s): {}\nknown: {}".format(
            ', '.join(unknown), ', '.join(MODULES)))

    for tool in ('ffmpeg', 'ffprobe'):
        if subprocess.run(['which', tool], capture_output=True).returncode != 0:
            sys.exit("{} is not on PATH; the suite needs it".format(tool))

    totals = [0, 0, 0]
    failed_modules = []

    for module in wanted:
        print("\n" + "=" * 72)
        print("RUNNING {}".format(module))
        print("=" * 72)
        try:
            proc = subprocess.run([sys.executable, str(TESTS / (module + '.py'))],
                                  capture_output=True, text=True,
                                  timeout=MODULE_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            if e.stdout:
                sys.stdout.write(e.stdout if isinstance(e.stdout, str)
                                 else e.stdout.decode(errors='replace'))
            print("  !! {} exceeded {}s and was killed -- treating as failure"
                  .format(module, MODULE_TIMEOUT_S))
            failed_modules.append(module)
            continue
        sys.stdout.write(proc.stdout)
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)

        match = RESULT.search(proc.stdout)
        if match:
            for i in range(3):
                totals[i] += int(match.group(i + 1))
        else:
            # No result line means the module died before finishing, which is a
            # failure even though it reported no failed assertion.
            print("  !! {} produced no result line -- it did not finish"
                  .format(module))
            failed_modules.append(module)
        if proc.returncode != 0 and module not in failed_modules:
            failed_modules.append(module)

    print("\n" + "=" * 72)
    print("TOTAL: {} passed, {} failed, {} skipped".format(*totals))
    if totals[2]:
        print("({} skipped -- set VSMP_TEST_MOVIE to run the real-movie checks)"
              .format(totals[2]))
    if failed_modules:
        print("modules with failures: {}".format(', '.join(failed_modules)))
    print("=" * 72)
    return 1 if failed_modules or totals[1] else 0


if __name__ == '__main__':
    sys.exit(main())
