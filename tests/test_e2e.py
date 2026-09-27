"""End-to-end runs through the real entry point.

Covers what only shows up when the whole program runs: resume after a crash
(problem 3, "is it playing in order?"), a container that overstates its frame
count, the already-finished path that systemd's Restart=always lands on, and the
SIGINT shutdown that systemd's KillSignal=SIGINT triggers.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import harness
from harness import check, section, skip, FakeEpd

import vsmp
from PIL import Image

SYNTH = harness.ensure_fixture()
REAL = harness.REAL_MOVIE
REPO = harness.REPO_ROOT

vsmp.FRAME_INTERVAL_S = 0.02
info = vsmp.probe_video(SYNTH)
MOVIE = SYNTH.name

ENV = dict(os.environ, PYTHONPATH="{}:{}".format(harness.STUBS_DIR, REPO))


def cli(args, cwd, timeout=60):
    """Run vsmp.py as a subprocess, always with a timeout.

    The timeout is not optional paranoia. A subprocess does NOT inherit the
    shortened vsmp.FRAME_INTERVAL_S this module sets for its in-process tests --
    it re-imports vsmp and gets the production 150s. Any call here that actually
    starts playing takes 150s per frame, so without a bound the suite appears to
    hang for hours. Use start_player() for anything meant to play; keep cli()
    for commands that exit on their own.
    """
    try:
        return subprocess.run([sys.executable, str(REPO / 'vsmp.py')] + args,
                              cwd=str(cwd), capture_output=True, text=True,
                              env=ENV, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "vsmp.py {} did not exit within {}s. If it was meant to play "
            "frames, use start_player() instead.".format(' '.join(args), timeout))


def start_player(args, cwd, wait_for=None, timeout=45):
    """Start the player, wait for one observable effect, then stop it.

    Waiting for a whole movie is not an option at 150s a frame, so this waits
    only for the first frame's state write, then sends SIGINT exactly as
    systemd's KillSignal=SIGINT does.
    """
    proc = subprocess.Popen(
        [sys.executable, str(REPO / 'vsmp.py')] + args,
        cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if wait_for is not None and Path(wait_for).exists():
                break
            if proc.poll() is not None:          # exited on its own
                break
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            proc.send_signal(2)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
    return proc.returncode, out.decode(errors='replace'), err.decode(errors='replace')


# ---------------------------------------------------------------------------
section("crash and resume: every frame once, in order")

# The fixture's gray level rises with frame number, so the exact sequence of
# images actually displayed can be reconstructed -- not merely the frame count.
shown = []
real_render = vsmp.render_one_frame


def recording_render(movie, frame, epd, inf, frames_dir, contrast):
    path = frames_dir / 'frame_{}.png'.format(frame)
    vsmp.extract_frame(movie, path, frame, inf)
    shown.append((frame, Image.open(path).getpixel((400, 240))))
    vsmp.display_on_e_ink(epd, vsmp.prepare_for_panel(path, contrast))
    vsmp.discard_bad_frame(path)
    return 0.0, 0.0


def crashing_render(movie, frame, epd, inf, frames_dir, contrast):
    if frame == 25:
        raise RuntimeError("simulated hard failure at frame 25")
    return recording_render(movie, frame, epd, inf, frames_dir, contrast)


vsmp.render_one_frame = crashing_render
state = vsmp.new_state(MOVIE, info)
vsmp.play_movie(FakeEpd(), SYNTH, 'crash.json', state, info, 1.0)

on_disk = json.loads(Path('crash.json').read_text())
check("the failing frame was recorded as an error", on_disk['errors'] == 1,
      "errors {}".format(on_disk['errors']))

# Now simulate the harder case: the process dies outright partway through, and a
# fresh one resumes from whatever reached disk.
shown.clear()
vsmp.render_one_frame = recording_render
partial = vsmp.new_state(MOVIE, info)
partial['total_frames'] = 30
vsmp.play_movie(FakeEpd(), SYNTH, 'part.json', partial, info, 1.0)
first_half = list(shown)

resumed = vsmp.load_state('part.json', MOVIE, info)
check("a fresh process resumes at the frame after the last one saved",
      resumed['frame'] == 30, resumed['frame'])
vsmp.play_movie(FakeEpd(), SYNTH, 'part.json', resumed, info, 1.0)
vsmp.render_one_frame = real_render

frames = [f for f, _ in shown]
check("resumed all the way to the end", resumed['frame'] == 60, resumed['frame'])
check("no frame was displayed twice across the restart",
      len(frames) == len(set(frames)),
      "{} displays, {} distinct".format(len(frames), len(set(frames))))
check("no frame was skipped across the restart",
      set(frames) == set(range(60)),
      "missing {}".format(sorted(set(range(60)) - set(frames))))
check("frames were displayed in strictly increasing order",
      all(b > a for a, b in zip(frames, frames[1:])),
      "first 4 {} ... last 4 {}".format(frames[:4], frames[-4:]))
check("the images match their frame numbers across the restart boundary",
      all(b > a for (_, a), (_, b) in zip(shown, shown[1:])),
      "gray levels rise monotonically, so the right pictures were shown")
check("the restart happened where intended",
      len(first_half) == 30 and frames[29] == 29,
      "{} frames before the restart".format(len(first_half)))

# ---------------------------------------------------------------------------
section("a container that overstates its frame count (rec 23, EndOfMovie)")

overstated = dict(info)
overstated['total_frames'] = 70          # the fixture really holds 60
state_p = vsmp.new_state(MOVIE, overstated)
state_p['frame'] = 57
epd_p = FakeEpd()
vsmp.play_movie(epd_p, SYNTH, 'over.json', state_p, overstated, 1.0)
final_p = json.loads(Path('over.json').read_text())
check("running off the end finishes cleanly", final_p['finished'] is True)
check("the total is corrected to what the stream actually holds",
      final_p['total_frames'] == 60, final_p['total_frames'])
check("running off the end is NOT counted as frame errors",
      final_p['errors'] == 0,
      "otherwise the last frames of every movie would look like failures")
check("only the frames that exist were displayed", len(epd_p.shown) == 3,
      "{} displays".format(len(epd_p.shown)))

# ---------------------------------------------------------------------------
section("the real movie through the real loop")

if harness.needs_real_movie("end of a feature-length film"):
    info_r = vsmp.probe_video(REAL)
    total = info_r['total_frames']
    state_r = vsmp.new_state(REAL.name, info_r)
    # Start seven frames from the end: the far end of the film is where the old
    # select=gte() path was slowest and where the '- 5' fudge used to hide bugs.
    state_r['frame'] = total - 7
    epd_r = FakeEpd()
    t0 = time.monotonic()
    vsmp.play_movie(epd_r, REAL, 'real.json', state_r, info_r, 1.0)
    elapsed = time.monotonic() - t0
    final_r = json.loads(Path('real.json').read_text())

    check("played the last 7 frames of the film", len(epd_r.shown) == 7,
          "{} displays".format(len(epd_r.shown)))
    check("reached the end", final_r['finished'] is True)
    check("no errors at the very end of the movie",
          final_r['errors'] == 0,
          "rec 20's '- 5' guard is not needed any more")
    check("the final frames were really extracted, not skipped",
          final_r['last_extract_s'] is not None and final_r['last_extract_s'] < 5.0,
          "last extract {}s".format(final_r['last_extract_s']))
    check("frames at the end of a 2-hour film are still fast",
          elapsed < 20, "{:.1f}s for 7 frames".format(elapsed))
    check("percent reads 100 at the end", abs(final_r['percent'] - 100.0) < 0.01,
          final_r['percent'])
    check("timecode is near the movie duration",
          abs(sum(int(x) * m for x, m in
                  zip(final_r['timecode'].split(':')[:2], (3600, 60)))
              - info_r['duration_s']) < 120,
          "{} vs duration {}".format(final_r['timecode'],
                                     vsmp.format_timecode(info_r['duration_s'])))

# ---------------------------------------------------------------------------
section("the CLI as systemd invokes it")

run = Path('cli')
run.mkdir(exist_ok=True)

# A finished movie: this is the state systemd's Restart=always keeps landing on
# once a film ends, and StartLimitBurst is what stops it looping.
done_state = vsmp.new_state(MOVIE, info)
done_state['frame'] = 60
done_state['finished'] = True
vsmp.save_state(run / 'state.json', done_state)

# The trailing '' mimics $VSMP_ARGS expanding to an empty argument.
finished = cli([str(SYNTH), ''], run)
check("an already-finished movie exits 0", finished.returncode == 0,
      "rc={} {}".format(finished.returncode, finished.stderr[-200:]))
log = (run / 'log.txt').read_text()
check("the log says why it did nothing", 'already finished' in log)
check("a finished movie does not touch the panel", 'init and Clear' not in log)

# --restart plays it again. Interrupt it the way systemd stops the service.
proc = subprocess.Popen(
    [sys.executable, str(REPO / 'vsmp.py'), str(SYNTH), '', '--restart'],
    cwd=str(run), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
time.sleep(8)
proc.send_signal(2)                        # SIGINT, as KillSignal=SIGINT sends
_, err = proc.communicate(timeout=30)
check("SIGINT exits 0 rather than crashing", proc.returncode == 0,
      "rc={} {}".format(proc.returncode, err[-300:].decode()))

log = (run / 'log.txt').read_text()
check("the interrupt handler ran", 'Interrupted by user' in log)
check("the panel was released on the way out", 'Display released' in log)
check("a frame was displayed before the interrupt",
      json.loads((run / 'state.json').read_text())['frame'] >= 1)
structured = [ln for ln in log.splitlines() if 'extract_s=' in ln]
check("the structured per-frame log line is emitted (rec 12)",
      structured and all(k in structured[0] for k in
                         ('frame=', 'pct=', 'tc=', 'next=', 'errors=')),
      structured[0].split('root: ')[-1] if structured else 'none found')

status = cli(['status'], run)
check("status works against the state the player just wrote",
      status.returncode == 0, status.stderr[-200:])
check("status reports it as playing, not finished", 'playing' in status.stdout)
check("status estimates the time remaining", 'remaining' in status.stdout,
      [ln for ln in status.stdout.splitlines() if 'remaining' in ln])

missing = cli(['no_such_movie.mp4'], run)
check("a missing movie file gives a clear error, not a traceback",
      missing.returncode != 0 and 'No such movie file' in missing.stderr,
      missing.stderr.strip()[-100:])

# Dotted release filenames. This used to make the player unrunnable: the old
# name handling read Howl's.Moving.Castle.2004.mp4 as a directory called
# "Howl's" holding clips named "Howl's_section0.Moving".
dotted = "Howl's.Moving.Castle.2004.1080p.BDRip.x265-Rapta.mp4"
subprocess.run(['cp', str(SYNTH), str(run / dotted)], check=True)
info_dotted = vsmp.probe_video(run / dotted)
check("a dotted release filename probes correctly (rec 31)",
      info_dotted['total_frames'] == 60, info_dotted['total_frames'])
rc, out, err = start_player(
    [dotted, '--restart', '--state', 'dotted.json'],
    run, wait_for=run / 'dotted.json')
check("a dotted release filename actually starts playing",
      (run / 'dotted.json').exists(), err[-200:])
if (run / 'dotted.json').exists():
    check("its state records the full filename",
          json.loads((run / 'dotted.json').read_text())['movie'] == dotted)
    check("its frames directory keeps the whole stem",
          any(p.name.startswith("Howl's.Moving.Castle") for p in run.glob('*_frames')),
          [p.name for p in run.glob('*_frames')])

harness.finish()
