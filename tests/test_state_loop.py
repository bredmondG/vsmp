"""State file handling, the frame loop, error tolerance, scheduling and the CLI.

Kept separate from test_extract.py so loop behaviour can be iterated on without
re-running the slow real-movie extraction comparisons.
"""
import json
import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

import harness
from harness import check, section, FakeEpd

import vsmp
from PIL import Image

SYNTH = harness.ensure_fixture()
REPO = harness.REPO_ROOT
info = vsmp.probe_video(SYNTH)
MOVIE = SYNTH.name


def cli(args, cwd=None):
    """Run vsmp.py as a real subprocess, with the stubs on the path."""
    env = dict(os.environ,
               PYTHONPATH="{}:{}".format(harness.STUBS_DIR, REPO))
    return subprocess.run([sys.executable, str(REPO / 'vsmp.py')] + args,
                          cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, env=env)


# ---------------------------------------------------------------------------
section("state file: JSON, keyed on an absolute frame index (recs 8, 9, 10)")

st = vsmp.new_state(MOVIE, info)
check("a new state starts at frame 0", st['frame'] == 0)
check("records the schema version", st['schema'] == vsmp.STATE_SCHEMA)
check("records which movie it belongs to", st['movie'] == MOVIE)
check("records the total frame count", st['total_frames'] == 60)
check("stores fps as an exact string rather than a lossy float",
      st['fps'] == '25' and isinstance(st['fps'], str), st['fps'])

vsmp.save_state('state.json', st)
check("the state file is valid JSON",
      json.loads(Path('state.json').read_text())['frame'] == 0)
check("it is human readable: indented and key-sorted",
      Path('state.json').read_text().startswith('{\n  "anomalies"'),
      repr(Path('state.json').read_text()[:28]))
check("no .tmp file is left behind after a successful save",
      not Path('state.json.tmp').exists())

# The point of rec 9 is that this is inspectable with ordinary tools.
other = subprocess.run(
    [sys.executable, '-c',
     'import json; print(json.load(open("state.json"))["movie"])'],
    capture_output=True, text=True)
check("readable by an unrelated process while the player holds it",
      other.stdout.strip() == MOVIE, other.stdout.strip())

check("state round-trips", vsmp.load_state('state.json', MOVIE, info)['frame'] == 0)
check("resuming resets the per-run frame counter",
      vsmp.load_state('state.json', MOVIE, info)['frames_this_run'] == 0)

st['frame'] = 42
vsmp.save_state('state.json', st)
check("resumes at the saved absolute frame",
      vsmp.load_state('state.json', MOVIE, info)['frame'] == 42)
check("--restart returns to frame 0",
      vsmp.load_state('state.json', MOVIE, info, restart=True)['frame'] == 0)

# Refusing to guess. Each of these could otherwise silently lose someone's
# position in a film they have been watching for a month.
try:
    vsmp.load_state('state.json', 'a_different_movie.mp4', info)
    check("refuses to resume a different movie at this frame number", False,
          "no exception")
except RuntimeError as e:
    check("refuses to resume a different movie at this frame number",
          'different_movie' in str(e) and '--restart' in str(e), str(e)[:90])

Path('badschema.json').write_text(json.dumps(dict(st, schema=99)))
try:
    vsmp.load_state('badschema.json', MOVIE, info)
    check("refuses an unknown schema version", False, "no exception")
except RuntimeError as e:
    check("refuses an unknown schema version", 'schema' in str(e), str(e)[:80])

Path('corrupt.json').write_text('{"schema": 2, "frame": tru')
try:
    vsmp.load_state('corrupt.json', MOVIE, info)
    check("corrupt state fails loudly rather than restarting the movie",
          False, "no exception")
except ValueError as e:
    check("corrupt state fails loudly rather than restarting the movie", True,
          type(e).__name__)
check("the corrupt file is left on disk to be inspected or repaired",
      Path('corrupt.json').exists())

Path('negative.json').write_text(json.dumps(dict(st, frame=-5)))
try:
    vsmp.load_state('negative.json', MOVIE, info)
    check("rejects a nonsensical frame number", False, "no exception")
except RuntimeError as e:
    check("rejects a nonsensical frame number", 'nonsensical' in str(e), str(e)[:60])

# The legacy pickle must be ignored, not converted into a confident wrong answer.
Path('legacy').mkdir(exist_ok=True)
os.chdir('legacy')
with open(vsmp.LEGACY_STATE_FILE, 'wb') as f:
    pickle.dump({'sections': ['brazil_section%d.mp4' % i for i in range(80)] + ['.DS_Store'],
                 'sections_ran': ['brazil_section%d.mp4' % i for i in range(25)],
                 'frame': 1391}, f)
legacy_state = vsmp.load_state('state.json', MOVIE, info)
check("a legacy progress.pkl does not become a wrong absolute frame",
      legacy_state['frame'] == 0,
      "its 'frame' was an index within a section, not an absolute position")
check("the legacy pickle is left on disk, not deleted",
      Path(vsmp.LEGACY_STATE_FILE).exists())
os.chdir('..')

drift = vsmp.new_state(MOVIE, info)
drift['frame'] = 5
drift['total_frames'] = 999999
vsmp.save_state('drift.json', drift)
reloaded = vsmp.load_state('drift.json', MOVIE, info)
check("a stale total frame count is corrected from a fresh probe",
      reloaded['total_frames'] == 60, reloaded['total_frames'])
check("correcting the total does not lose the position", reloaded['frame'] == 5)

# ---------------------------------------------------------------------------
section("atomic state writes (rec 6, carried forward)")

corrupt_reads = 0
samples = []
atomic = vsmp.new_state(MOVIE, info)
for i in range(300):
    atomic['frame'] = i
    vsmp.save_state('atomic.json', atomic)
    try:
        samples.append(json.loads(Path('atomic.json').read_text())['frame'])
    except Exception:
        corrupt_reads += 1
check("300 interleaved reads saw zero partial writes",
      corrupt_reads == 0, "{} corrupt".format(corrupt_reads))
check("every read returned a complete, plausible position",
      all(0 <= s < 300 for s in samples), "{} samples".format(len(samples)))

# ---------------------------------------------------------------------------
section("the frame loop: one file, start to finish (rec 15)")

vsmp.FRAME_INTERVAL_S = 0.05        # keep the suite quick; the logic is unchanged

epd = FakeEpd()
state = vsmp.new_state(MOVIE, info)
vsmp.play_movie(epd, SYNTH, 'loop.json', state, info, 1.0)

check("displayed every frame of the movie exactly once", len(epd.shown) == 60,
      "{} displays".format(len(epd.shown)))
check("ended at the last frame", state['frame'] == 60, state['frame'])
check("marked finished", state['finished'] is True)
check("no errors in a clean run", state['errors'] == 0)
check("no ordering anomalies in a clean run", state['anomalies'] == 0)
check("panel put to sleep at the end", epd.slept >= 1)

final = json.loads(Path('loop.json').read_text())
check("final position persisted", final['frame'] == 60 and final['finished'] is True)
check("percent reached 100", abs(final['percent'] - 100.0) < 0.001, final['percent'])
check("timecode advanced to the end of the movie",
      final['timecode'].startswith('00:00:02'), final['timecode'])
check("next_frame_utc cleared once finished", final['next_frame_utc'] is None)
check("no frame images left on disk",
      not list(Path('{}_frames'.format(SYNTH.stem)).glob('frame_*.png')))

epd2 = FakeEpd()
mid = vsmp.new_state(MOVIE, info)
mid['frame'] = 55
vsmp.play_movie(epd2, SYNTH, 'resume.json', mid, info, 1.0)
check("resuming at frame 55 displays only the remaining 5",
      len(epd2.shown) == 5, "{} displays".format(len(epd2.shown)))

# ---------------------------------------------------------------------------
section("position verification (rec 16)")

# The check has to be able to fail, so drive the real failure mode: something
# other than this player writing the state file. DEPLOY.md warns about exactly
# this -- two players running at once.
real_save = vsmp.save_state
tampered = []


def tampering_save(path, payload):
    real_save(path, payload)
    if payload['frame'] == 53:
        doc = json.loads(Path(path).read_text())
        doc['frame'] = 12345
        Path(path).write_text(json.dumps(doc))
        tampered.append(payload['frame'])


vsmp.save_state = tampering_save
tstate = vsmp.new_state(MOVIE, info)
tstate['frame'] = 50
vsmp.play_movie(FakeEpd(), SYNTH, 'tamper.json', tstate, info, 1.0)
vsmp.save_state = real_save

check("the test really did tamper with the file", tampered == [53])
check("a state file written by another process is detected",
      tstate['anomalies'] == 1, "anomalies {}".format(tstate['anomalies']))
check("the run continues after an anomaly rather than dying",
      tstate['frame'] == 60, tstate['frame'])

control = vsmp.new_state(MOVIE, info)
control['frame'] = 50
vsmp.play_movie(FakeEpd(), SYNTH, 'control.json', control, info, 1.0)
check("NEGATIVE CONTROL: an untampered run reports zero anomalies",
      control['anomalies'] == 0, "anomalies {}".format(control['anomalies']))


def truncating_save(path, payload):
    real_save(path, payload)
    if payload['frame'] == 57:
        Path(path).write_text('{ truncated')


vsmp.save_state = truncating_save
trunc = vsmp.new_state(MOVIE, info)
trunc['frame'] = 55
vsmp.play_movie(FakeEpd(), SYNTH, 'trunc.json', trunc, info, 1.0)
vsmp.save_state = real_save
check("an unreadable state file is reported but does not end the run",
      trunc['anomalies'] >= 1 and trunc['frame'] == 60,
      "anomalies {}, frame {}".format(trunc['anomalies'], trunc['frame']))

# ---------------------------------------------------------------------------
section("per-frame error tolerance (rec 7, carried forward)")

real_extract = vsmp.extract_frame


def flaky(movie, out_path, frame, inf):
    if frame in (2, 3):
        raise RuntimeError("simulated extraction failure")
    return real_extract(movie, out_path, frame, inf)


vsmp.extract_frame = flaky
epd3 = FakeEpd()
s3 = vsmp.new_state(MOVIE, info)
s3['total_frames'] = 8
vsmp.play_movie(epd3, SYNTH, 'flaky.json', s3, info, 1.0)
vsmp.extract_frame = real_extract

check("two bad frames are skipped, not fatal", s3['frame'] == 8, s3['frame'])
check("bad frames are counted", s3['errors'] == 2, s3['errors'])
check("the remaining 6 frames still displayed", len(epd3.shown) == 6,
      "{} displays".format(len(epd3.shown)))
check("a skipped frame is not also reported as an ordering anomaly",
      s3['anomalies'] == 0, s3['anomalies'])


def always_fails(movie, out_path, frame, inf):
    raise RuntimeError("simulated persistent failure")


vsmp.extract_frame = always_fails
s4 = vsmp.new_state(MOVIE, info)
try:
    vsmp.play_movie(FakeEpd(), SYNTH, 'systemic.json', s4, info, 1.0)
    check("gives up after 10 consecutive failures", False, "no exception")
except RuntimeError as e:
    check("gives up after 10 consecutive failures",
          'in a row' in str(e) and s4['errors'] == 10,
          "{} errors".format(s4['errors']))
check("the position is saved before giving up",
      json.loads(Path('systemic.json').read_text())['frame'] == 10)
vsmp.extract_frame = real_extract


class WedgedEpd(FakeEpd):
    def init(self):
        raise TimeoutError("e-Paper BUSY still held after 30s")


s5 = vsmp.new_state(MOVIE, info)
try:
    vsmp.play_movie(WedgedEpd(), SYNTH, 'wedged.json', s5, info, 1.0)
    check("a wedged panel propagates instead of being skipped", False, "no exception")
except TimeoutError:
    check("a wedged panel propagates instead of being skipped", True,
          "TimeoutError reached the caller")
check("a wedged panel is not miscounted as 10 bad frames", s5['errors'] == 0,
      s5['errors'])

# ---------------------------------------------------------------------------
section("the size guard stops a silently black panel")

from epd import epd7in5_V2_old as stub_driver     # the stub, mirroring the real one

driver = stub_driver.EPD()
wrong = Image.new('L', (640, 360), 128)
buf = driver.getbuffer(wrong)
check("NEGATIVE CONTROL: the driver returns an all-black buffer on a size mismatch",
      set(buf) == {0x00},
      "{} bytes, all 0x00 -- solid black after inversion, and no exception"
      .format(len(buf)))
try:
    vsmp.display_on_e_ink(driver, wrong)
    check("display_on_e_ink refuses a wrongly sized image", False, "no exception")
except ValueError as e:
    check("display_on_e_ink refuses a wrongly sized image",
          'solid black' in str(e), str(e)[:70])
vsmp.display_on_e_ink(driver, Image.new('L', (800, 480), 128))
check("display_on_e_ink accepts a correctly sized image", True)

# ---------------------------------------------------------------------------
section("absolute deadline scheduling (rec 24)")

vsmp.FRAME_INTERVAL_S = 0.30
real_render = vsmp.render_one_frame
starts = []


def instant_render(movie, frame, epd_, inf, frames_dir, contrast):
    starts.append(time.monotonic())
    return 0.0, 0.0


vsmp.render_one_frame = instant_render
s6 = vsmp.new_state(MOVIE, info)
s6['total_frames'] = 8
vsmp.play_movie(FakeEpd(), SYNTH, 'sched.json', s6, info, 1.0)
gaps = [b - a for a, b in zip(starts, starts[1:])]
check("frames are paced at the interval",
      all(abs(gp - 0.30) < 0.08 for gp in gaps),
      ", ".join("{:.3f}".format(gp) for gp in gaps))
check("no cumulative drift over 8 frames",
      abs((starts[-1] - starts[0]) - 0.30 * (len(starts) - 1)) < 0.08,
      "elapsed {:.3f}s vs expected {:.3f}s".format(
          starts[-1] - starts[0], 0.30 * (len(starts) - 1)))

overrun_starts = []


def slow_once(movie, frame, epd_, inf, frames_dir, contrast):
    overrun_starts.append(time.monotonic())
    if frame == 2:
        time.sleep(1.0)         # more than three intervals
    return 0.0, 0.0


vsmp.render_one_frame = slow_once
s7 = vsmp.new_state(MOVIE, info)
s7['total_frames'] = 7
vsmp.play_movie(FakeEpd(), SYNTH, 'sched2.json', s7, info, 1.0)
vsmp.render_one_frame = real_render

after = [b - a for a, b in zip(overrun_starts, overrun_starts[1:])][3:]
check("every frame is still shown after an overrun -- none skipped",
      len(overrun_starts) == 7, "{} rendered".format(len(overrun_starts)))
check("the player does not sprint through the backlog after an overrun",
      all(gp > 0.20 for gp in after),
      ", ".join("{:.3f}".format(gp) for gp in after))
check("pacing returns to the interval after an overrun",
      all(abs(gp - 0.30) < 0.10 for gp in after),
      ", ".join("{:.3f}".format(gp) for gp in after))

vsmp.FRAME_INTERVAL_S = 3600.0 / 24

# ---------------------------------------------------------------------------
section("CLI parsing, including the systemd compatibility guards (rec 11)")

check("a bare filename still means 'play', so the installed unit keeps working",
      vsmp.parse_args(['movie.mp4']).command == 'play')
check("the explicit 'play' subcommand works",
      vsmp.parse_args(['play', 'movie.mp4']).filename == 'movie.mp4')
check("the 'status' subcommand parses", vsmp.parse_args(['status']).command == 'status')
check("play flags parse",
      vsmp.parse_args(['m.mp4', '--restart', '--contrast', '1.4']).contrast == 1.4)
check("contrast defaults to a no-op", vsmp.parse_args(['m.mp4']).contrast == 1.0)

# Empty arguments. The unit's ExecStart ends with $VSMP_ARGS, which is normally
# empty; if that ever expands to an empty argument instead of to nothing,
# argparse would exit 2 and the service would fail to start on every Pi.
empty_cases = [
    ['movie.mp4', ''],
    ['movie.mp4', '', ''],
    ['movie.mp4', '', '--restart'],
    ['movie.mp4', '--restart', ''],
    ['play', 'movie.mp4', ''],
    ['', 'movie.mp4'],
    ["Howl's.Moving.Castle.2004.1080p.mp4", ''],
    ['a movie with spaces.mp4', ''],
]
for argv in empty_cases:
    try:
        parsed = vsmp.parse_args(argv)
        ok = parsed.command == 'play' and parsed.filename.endswith('.mp4')
    except SystemExit:
        ok = False
    check("empty arguments tolerated: {!r}".format(argv), ok)

for bad, label in ((['movie.mp4', '--nonsense'], "an unknown flag"),
                   ([''], "an argv of nothing but empties")):
    try:
        vsmp.parse_args(bad)
        check("NEGATIVE CONTROL: {} is still rejected".format(label), False,
              "it was accepted")
    except SystemExit:
        check("NEGATIVE CONTROL: {} is still rejected".format(label), True)

# ---------------------------------------------------------------------------
section("status subcommand output")

st_run = cli(['status', '--state', 'loop.json'])
check("status exits 0 against a real state file", st_run.returncode == 0,
      st_run.stderr[-200:])
check("status names the movie", MOVIE in st_run.stdout)
check("status reports the position", '60 of 60' in st_run.stdout)
check("status draws a progress bar", '#' * 20 in st_run.stdout)

missing = cli(['status', '--state', 'nope.json'])
check("status exits non-zero when nothing has been written yet",
      missing.returncode == 1, missing.returncode)
check("status explains that no frame has been written",
      'has not written a frame' in missing.stdout, missing.stdout.strip())

as_json = cli(['status', '--state', 'loop.json', '--json'])
check("status --json emits parseable JSON",
      json.loads(as_json.stdout)['frame'] == 60)

# ---------------------------------------------------------------------------
section("no repo pollution")

check("no log.txt written into the repo", not (REPO / 'log.txt').exists())
check("no state.json written into the repo", not (REPO / 'state.json').exists())
check("no frames directory written into the repo",
      not list(REPO.glob('*_frames')))
dirty = subprocess.run(['git', 'status', '--short'], cwd=str(REPO),
                       capture_output=True, text=True).stdout.splitlines()
stray = [ln for ln in dirty if 'tests/work' in ln or 'tests/fixtures' in ln]
check("test artifacts are git-ignored", not stray, stray)

harness.finish()
