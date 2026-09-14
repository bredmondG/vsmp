"""Shared test scaffolding for the vsmp suite.

Import this FIRST, before vsmp, in every test module:

    import harness
    import vsmp

Importing it does three things, in this order, and the order matters:

  1. Puts tests/stubs on sys.path ahead of the repo, so `from epd import ...`
     resolves to the stub driver. The real epd/epdconfig.py reads /proc/cpuinfo
     at import time and raises RuntimeError on a development machine.
  2. Puts the repo root on sys.path so `import vsmp` works from anywhere.
  3. chdir()s into a fresh scratch directory under tests/work/.

Step 3 has to happen before `import vsmp`, because vsmp calls
configure_logging() at import time and writes log.txt relative to the current
directory. Without it the tests would litter the repo with log.txt and
state.json -- which the suite then asserts has not happened.
"""
import os
import shutil
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
STUBS_DIR = TESTS_DIR / 'stubs'
FIXTURES_DIR = TESTS_DIR / 'fixtures'
SYNTH_MOVIE = FIXTURES_DIR / 'synth.mp4'

# Named after the importing module so suites do not tread on each other and a
# failure can be picked apart afterwards.
_module_name = Path(sys.argv[0]).stem or 'harness'
WORK_DIR = TESTS_DIR / 'work' / _module_name


def _bootstrap():
    sys.path.insert(0, str(STUBS_DIR))
    sys.path.insert(1, str(REPO_ROOT))
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True)
    os.chdir(WORK_DIR)


_bootstrap()


# --- the optional real movie -------------------------------------------------
#
# Some checks are only meaningful against a genuine feature-length film: that
# extraction cost stays flat over 171,437 frames, that a real container's
# nb_frames is trustworthy, that the last frames of a two-hour movie extract
# without error. A synthetic 60-frame fixture cannot stand in for those.
#
# It is deliberately NOT committed -- it is a 700 MB copyrighted film. Point the
# suite at one with:
#
#     VSMP_TEST_MOVIE=/path/to/movie.mp4 python3 tests/run.py
#
# Without it those checks are SKIPPED and reported as skipped, so the suite is
# still useful on a machine that does not have the film.

def find_real_movie():
    explicit = os.environ.get('VSMP_TEST_MOVIE')
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    # Convenience: a single .mp4 sitting beside the repo, as in this workspace.
    for candidate in sorted(REPO_ROOT.parent.glob('*.mp4')):
        return candidate
    return None


REAL_MOVIE = find_real_movie()


# --- assertion counting ------------------------------------------------------

PASS = 0
FAIL = 0
SKIP = 0
FAILURES = []


def section(title):
    print("\n=== {} ===".format(title))


def check(name, ok, detail=''):
    """Record one assertion. `detail` is printed either way -- on a pass it is
    the evidence, on a failure it is the diagnosis."""
    global PASS, FAIL
    detail = '' if detail is None else str(detail)
    if ok:
        PASS += 1
        print("  ok   {}{}".format(name, ' -- ' + detail if detail else ''))
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  FAIL {}{}".format(name, ' -- ' + detail if detail else ''))


def skip(name, why):
    global SKIP
    SKIP += 1
    print("  skip {} -- {}".format(name, why))


def needs_real_movie(name):
    """Return True if the real-movie checks can run, else record a skip."""
    if REAL_MOVIE is None:
        skip(name, "no real movie; set VSMP_TEST_MOVIE to enable")
        return False
    return True


def finish():
    """Print the summary and exit non-zero if anything failed."""
    print("\n{}: {} passed, {} failed, {} skipped".format(
        _module_name, PASS, FAIL, SKIP))
    if FAILURES:
        print("failed: {}".format(FAILURES))
    # Machine-readable line for run.py to aggregate.
    print("HARNESS_RESULT pass={} fail={} skip={}".format(PASS, FAIL, SKIP))
    sys.exit(1 if FAIL else 0)


# --- helpers shared by more than one module ---------------------------------

def sha256(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ensure_fixture():
    """Build the synthetic test movie if it is not already there."""
    if SYNTH_MOVIE.exists():
        return SYNTH_MOVIE
    import subprocess
    subprocess.run(
        [sys.executable, str(TESTS_DIR / 'make_fixtures.py')],
        check=True)
    return SYNTH_MOVIE


class FakeEpd:
    """An e-paper panel that records what it was asked to show.

    Separate from the stub driver in tests/stubs: that one exists so `import
    vsmp` works and mirrors the real getbuffer(), this one is for asserting on
    what the frame loop did.
    """

    def __init__(self):
        self.width, self.height = 800, 480
        self.shown = []
        self.slept = 0

    def init(self):
        pass

    def Clear(self):
        pass

    def display(self, buf):
        self.shown.append(buf)

    def sleep(self):
        self.slept += 1

    def getbuffer(self, image):
        if image.size != (self.width, self.height):
            raise AssertionError(
                "the size guard in display_on_e_ink should have stopped this")
        return image.convert('1').tobytes('raw')
