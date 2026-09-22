#!/usr/bin/python
# -*- coding:utf-8 -*-
"""Very slow movie player.

Displays a movie on a Waveshare 7.5" black-and-white e-paper panel (800x480),
one frame at a time, at 24 frames per hour.

Architecture note (recommendation 15). This plays the *single source movie file*
straight through. It does not pre-slice the movie into sections. The old design
sliced the film into 20 pieces because extraction used
``-vf "select=gte(n\\,N)"``, which decodes every frame from the start of the
input to reach frame N; slicing capped that worst case at 1/20th of the film.
Switching to ffmpeg input seeking (recommendation 21) removed the reason for
slicing, and with it an entire class of ordering bugs -- "is it in order?" is now
just "is the frame index incrementing by 1?".
"""
import argparse
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from fractions import Fraction
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PIL import Image, ImageEnhance

# NOTE: the e-paper driver is imported lazily inside the play path, NOT here.
#
# `from epd import epd7in5_V2_old` pulls in epd/epdconfig.py, whose module body
# does `implementation = RaspberryPi()`, and RaspberryPi.__init__ claims the
# GPIO pins (gpiozero.LED(...)) the instant it runs. Importing at module top
# therefore grabs the hardware for *every* invocation -- including
# `vsmp.py status`, which is meant to read a file and print it without going
# anywhere near the panel.
#
# When the player service is already running it holds those pins, so a status
# command that imported the driver died with `lgpio.error: 'GPIO busy'` before
# cmd_status ever ran. Deferring the import to cmd_play() keeps status a pure
# read (see main(), which routes status away from any display code).

LOG_FILE = 'log.txt'
LOG_MAX_BYTES = 5 * 1024 * 1024   # rotate once a log file reaches 5 MB
LOG_BACKUP_COUNT = 5              # keep log.txt plus log.txt.1 ... log.txt.5

# The panel's native resolution. Frames are scaled and letterboxed to exactly
# this size by ffmpeg (recommendation 26). Anything else is a bug -- see the
# size guard in display_on_e_ink() for why that matters more than it looks.
PANEL_W = 800
PANEL_H = 480

# The whole point of the project: 24 frames per hour, i.e. one every 150s.
FRAMES_PER_HOUR = 24
FRAME_INTERVAL_S = 3600.0 / FRAMES_PER_HOUR

# Recommendation 30. os.popen() had no timeout, so a wedged ffmpeg hung the
# player forever. This bound must stay comfortably BELOW FRAME_INTERVAL_S,
# otherwise one slow extraction pushes the whole schedule out, and comfortably
# below WatchdogSec in systemd/vsmp.service, so the tidy local error fires
# before systemd kills the process. Input seeking costs a fraction of a second
# on a dev machine; this is sized for a Pi having a very bad day.
FFMPEG_TIMEOUT_S = 120
FFPROBE_TIMEOUT_S = 60
# An exact frame count decodes the entire movie. Opt-in only (--count-frames),
# and it needs an upper bound measured in hours rather than seconds.
COUNT_FRAMES_TIMEOUT_S = 6 * 60 * 60

# Recommendation 9: JSON state, not a pickle. Bump SCHEMA whenever the shape
# changes incompatibly so an old file is rejected loudly instead of
# misinterpreted.
STATE_FILE = 'state.json'
STATE_SCHEMA = 2
LEGACY_STATE_FILE = 'progress.pkl'

# Recommendation 7. How many frames may fail back-to-back before we treat the
# problem as systemic rather than as a run of bad luck. Ten failures at one frame
# per 150s is about 25 minutes of a blank screen, which is long enough to rule
# out a transient glitch and short enough that a restart still helps.
MAX_CONSECUTIVE_FRAME_ERRORS = 10

# Tolerance when cross-checking a container's advertised frame count against
# duration x fps (recommendation 23). They disagree by a rounding error on a
# healthy file; a real disagreement means one of the two is not trustworthy.
FRAME_COUNT_TOLERANCE = 2

SUBCOMMANDS = ('play', 'status')


def configure_logging():
    """Set up file logging that survives a restart.

    This player is expected to run unattended for weeks, and the interesting
    question after a stall is always "what did it say just before it stopped?".

    The previous setup was:

        logging.basicConfig(filename='log.txt', filemode='w', ...)

    ``filemode='w'`` truncates the file every time the process starts, so a
    crash-and-restart (or an SSH-in-and-restart) wiped the only record of the
    failure. Appending instead means the evidence outlives the process.

    Appending forever would eventually fill the SD card, so a RotatingFileHandler
    caps total log size at LOG_MAX_BYTES * (LOG_BACKUP_COUNT + 1).
    """
    handler = RotatingFileHandler(
        LOG_FILE,
        mode='a',                      # append: never destroy previous runs
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    # Timestamp every line. Without this, correlating a stall against
    # `dmesg` / `journalctl` output is guesswork.
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-8s %(name)s: %(message)s'
    ))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)


# NOTE: logging is configured in main() on the play path, NOT here at import
# time. configure_logging() opens log.txt for writing via RotatingFileHandler,
# which is a side effect no importer should trigger just by `import vsmp`. The
# status CLI does not need it (it is a pure read), and neither does the web
# server (webapp.py imports this module to reuse read_state/summarize_state and
# must not open -- let alone require write access to -- the player's log file).
# See main().

# --- systemd watchdog --------------------------------------------------------
#
# Why this exists (recommendation 3). Two protections are already in place and
# neither covers the remaining case:
#
#   * The BUSY timeout in the display driver bounds hangs inside ReadBusy().
#   * The systemd unit restarts the player whenever it exits.
#
# What is left is a stall somewhere else in the frame loop. Note that the
# original motivating case -- a wedged ffmpeg inside os.popen(...).read(), which
# had no timeout -- is now handled more precisely by the timeout in run_tool()
# (recommendation 30). The watchdog stays as the backstop for everything that
# is not an ffmpeg call: it is the only protection that does not depend on
# having anticipated the specific thing that hung.
#
# systemd's WatchdogSec= handles exactly this: it expects a WATCHDOG=1 datagram
# at least every WATCHDOG_USEC/2, and kills and restarts the service if the
# messages stop arriving.
#
# This talks to the notify socket directly rather than depending on the
# python3-systemd package, which would be a new dependency for about thirty
# lines of work.

_watchdog_sock = None
_watchdog_addr = None
_watchdog_interval_s = None

# Longest we will go without a ping while idle between frames. See the reasoning
# in sleep_until(); this exists so that a slow panel refresh plus a slow
# extraction cannot add up to a spurious watchdog restart.
PING_MAX_GAP_S = 30


def configure_watchdog():
    """Set up the systemd watchdog, if we are running under systemd.

    A no-op when NOTIFY_SOCKET or WATCHDOG_USEC are absent, which is the case
    when vsmp.py is started by hand. Manual runs therefore behave exactly as
    they did before.
    """
    global _watchdog_sock, _watchdog_addr, _watchdog_interval_s

    addr = os.environ.get('NOTIFY_SOCKET')
    usec = os.environ.get('WATCHDOG_USEC')
    if not addr or not usec:
        logging.info("systemd watchdog not active "
                     "(NOTIFY_SOCKET/WATCHDOG_USEC not set), continuing without it")
        return

    # A leading '@' means the Linux abstract socket namespace, which Python
    # expresses as a leading NUL byte. Only the first character is replaced.
    if addr.startswith('@'):
        addr = '\0' + addr[1:]

    try:
        watchdog_s = int(usec) / 1_000_000
        _watchdog_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        _watchdog_addr = addr
        # systemd requires a ping at least every half interval. Use a third to
        # leave headroom for a slow frame without tripping the watchdog.
        _watchdog_interval_s = watchdog_s / 3
    except (OSError, ValueError):
        logging.exception("Could not set up the systemd watchdog, continuing without it")
        _watchdog_sock = None
        return

    logging.info("systemd watchdog active: WatchdogSec=%.0fs, pinging every %.0fs",
                 watchdog_s, _watchdog_interval_s)


def watchdog_ping():
    """Tell systemd the player is still alive.

    Only call this where the player has actually made progress, or where it is
    legitimately idle between frames.

    Deliberately NOT driven from a background timer thread. A thread pinging on
    a schedule would keep reporting healthy while the frame loop sat wedged in
    ffmpeg, which is precisely the failure this is supposed to catch. Tying the
    ping to real progress is the whole point.
    """
    global _watchdog_sock

    if _watchdog_sock is None:
        return
    try:
        _watchdog_sock.sendto(b'WATCHDOG=1', _watchdog_addr)
    except OSError:
        # Stop trying rather than logging this every frame. Letting the pings
        # lapse is the right outcome anyway: systemd will notice and restart
        # us, which is what we would want if the notify socket has gone.
        logging.exception("Watchdog ping failed, disabling pings. "
                          "systemd will restart the service when WatchdogSec expires.")
        _watchdog_sock = None


def sleep_until(deadline_monotonic):
    """Wait for an absolute monotonic deadline, pinging the watchdog as we go.

    Split into chunks instead of one long sleep. If the ping only happened once
    per frame, WatchdogSec would have to be longer than the entire frame
    interval plus the slowest possible extraction, which makes the detection
    window needlessly coarse and ties it to the frame rate. Pinging through the
    idle period means WatchdogSec only has to cover extract-and-display.

    The process is genuinely healthy while waiting here, so pinging is honest:
    a hang shows up as extraction never finishing, which stops the pings.

    Takes an absolute deadline rather than a duration (recommendation 24) so
    that the time spent inside this function cannot itself become drift.
    """
    # Without a watchdog, fall back to a single sleep so manual runs are
    # unchanged.
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return
    if _watchdog_interval_s is None:
        chunk = remaining
    else:
        # Cap the chunk well below the watchdog interval rather than setting it
        # equal to it. With WatchdogSec=600 the interval is 200s, which is longer
        # than the 150s frame gap, so a single chunk would put exactly one ping
        # at the end of each sleep. The worst-case gap between pings would then
        # be extract + display + 150s, and display is not cheap: the driver
        # reaches ReadBusy() four times per frame (init, Clear, display, sleep),
        # each bounded at 30s by the recommendation 2 timeout. That is up to
        # 120s of display plus 120s of ffmpeg timeout plus the 150s sleep, which
        # exceeds the 300s systemd allows and would cause a spurious restart
        # during otherwise healthy playback. Pinging every 30s costs one
        # datagram and removes the whole interaction.
        chunk = min(_watchdog_interval_s, PING_MAX_GAP_S)

    while True:
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(chunk, remaining))
        watchdog_ping()


# --- running ffmpeg and ffprobe ---------------------------------------------


def run_tool(argv, timeout, what):
    """Run ffmpeg/ffprobe and return stdout, or raise.

    Recommendation 30. This replaces ``os.popen('... {}'.format(...))``, which
    had three problems:

      * **No timeout.** A wedged ffmpeg hung the player indefinitely, with no
        error and no log line. That was one of the candidate causes of the
        reported stalls.
      * **Shell injection / quoting.** The command was a formatted string handed
        to a shell, so any movie filename containing a space, quote or ``$``
        either broke or did something unintended.
      * **No error detection.** ``os.popen(...).read()`` discards the exit
        status, so a failed extraction looked exactly like a successful one and
        surfaced later as a confusing PIL error on a missing or empty file.

    Passing an argument *list* means no shell is involved at all, so filenames
    need no escaping and cannot be interpreted as syntax.

    ``stdin=DEVNULL`` is the structural version of ffmpeg's ``-nostdin``: even
    if a future command loses that flag, there is no terminal for ffmpeg to ask
    a question on. Both are kept -- see recommendation 36 for why an
    interactive prompt from ffmpeg is a genuine hazard here.
    """
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run() kills the child before re-raising, so we are not
        # leaving a wedged ffmpeg behind to be found by a puzzled human later.
        logging.error("%s timed out after %ss: %s", what, timeout, shlex.join(argv))
        raise

    if proc.returncode != 0:
        # ffmpeg puts everything on stderr, including the actual reason. Tail it
        # rather than logging the whole banner.
        logging.error("%s failed (exit %d): %s\nstderr tail:\n%s",
                      what, proc.returncode, shlex.join(argv),
                      (proc.stderr or '').strip()[-2000:])
        raise RuntimeError("{} failed with exit status {}".format(what, proc.returncode))

    return proc.stdout


def _parse_ratio(value, default=None):
    """Parse ffprobe's ``num/den`` or ``num:den`` forms into a Fraction.

    Returns `default` for the values ffprobe uses to mean "I don't know":
    absent, ``N/A``, ``0/0``. A zero denominator is also treated as unknown
    rather than allowed to raise, because ffprobe does emit ``0/1`` for
    sample_aspect_ratio on some files.
    """
    if not value or value == 'N/A':
        return default
    text = value.replace(':', '/')
    try:
        frac = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return default
    if frac <= 0:
        return default
    return frac


def format_seconds(seconds, places=6):
    """Format an exact Fraction of seconds as a plain decimal string.

    Deliberately avoids float formatting. The timestamps handed to ffmpeg are
    derived from exact rational arithmetic (recommendation 22), and rendering
    them through ``repr(float)`` reintroduces the imprecision the Fraction was
    there to avoid -- as well as risking scientific notation, which ffmpeg
    would not parse.
    """
    seconds = Fraction(seconds)
    scale = 10 ** places
    units = round(seconds * scale)
    whole, frac = divmod(units, scale)
    return "{}.{:0{}d}".format(whole, frac, places)


def format_timecode(seconds):
    """Render a position as HH:MM:SS.mmm for humans (recommendations 10, 12)."""
    total_ms = int(round(float(seconds) * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    return "{:02d}:{:02d}:{:02d}.{:03d}".format(
        total_s // 3600, (total_s // 60) % 60, total_s % 60, ms)


def frame_timestamp(frame, fps):
    """The ffmpeg seek timestamp for an absolute frame number.

    Recommendation 22, with a correction that matters. The original advice was
    to aim at the *middle* of the frame, ``(frame + 0.5) / fps``. That is
    off by one, and it was measured to be off by one against real ffmpeg.

    Accurate seek keeps the first frame whose presentation timestamp is
    ``>= ts`` and discards everything before it. Frame N's own pts is
    ``N / fps``, so asking for half a frame *past* that lands on frame N+1.
    Aiming half a frame *before* it selects frame N, with half a frame of
    margin on either side -- which is what the mitigation was reaching for.

    Verified against ``select=gte(n\\,N)`` as ground truth on a real 171,437
    frame movie: ``+0.5`` returned frame 50001 for a request for 50000, while
    ``-0.5`` and exact ``N/fps`` both returned 50000. Exact ``N/fps`` sits
    precisely on the comparison boundary, so any rounding upward silently costs
    a frame; ``-0.5`` has margin.

    Rational arithmetic throughout, per recommendation 22: at 171,437 frames a
    float fps error of 0.001 is seconds of drift by the end of the film.
    """
    ts = (Fraction(frame) - Fraction(1, 2)) / fps
    if ts < 0:
        # Frame 0 has no half-frame of margin before it.
        ts = Fraction(0)
    return ts


def panel_geometry(width, height, sar):
    """Work out the scale-and-letterbox geometry for this movie.

    Recommendation 26. The old code did ``im.resize((800, 480))``, which
    stretches any non-1.67 movie to fit. For the 1.85:1 film in this workspace
    that is an 11% vertical stretch -- everything slightly squashed, all the
    time.

    Computed here in Python rather than with ffmpeg's
    ``force_original_aspect_ratio=decrease`` for one specific reason: that
    option only looks at *storage* dimensions and ignores the sample aspect
    ratio. The movie in this workspace is 888x480 storage with a SAR of
    4920:4921 and a display aspect of 246:133, and anamorphic sources (DVD rips
    especially) can have a SAR far from 1:1. Correcting for SAR here also keeps
    the ffmpeg command free of nested filter expressions and makes the geometry
    loggable and testable.
    """
    display_w = Fraction(width) * (sar or Fraction(1))
    display_h = Fraction(height)

    # Fit inside the panel without cropping: scale by whichever axis binds.
    factor = min(Fraction(PANEL_W) / display_w, Fraction(PANEL_H) / display_h)

    out_w = min(PANEL_W, max(1, round(display_w * factor)))
    out_h = min(PANEL_H, max(1, round(display_h * factor)))
    return {
        'width': out_w,
        'height': out_h,
        'pad_x': (PANEL_W - out_w) // 2,
        'pad_y': (PANEL_H - out_h) // 2,
        'display_aspect': float(display_w / display_h),
    }


def probe_video(movie, count_frames=False):
    """Read frame rate, duration, geometry and total frame count, once.

    Recommendation 23. The old ``frame_count()`` shelled out to ffprobe for
    every section and parsed the result with ``int(s.split()[0])``, which
    raised IndexError on empty output -- the crash waiting at the end of the
    movie. This runs once per process, parses JSON rather than whitespace, and
    the result is cached in the state file.

    Also implements the recommendation 22 precondition: the frame-number to
    timestamp mapping is only valid for constant frame rate content, so a
    variable frame rate source is detected and reported rather than silently
    producing wrong frames.
    """
    raw = run_tool([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries',
        'stream=r_frame_rate,avg_frame_rate,width,height,'
        'sample_aspect_ratio,duration,nb_frames',
        '-show_entries', 'format=duration',
        '-of', 'json', str(movie),
    ], FFPROBE_TIMEOUT_S, 'ffprobe stream probe')

    parsed = json.loads(raw)
    streams = parsed.get('streams') or []
    if not streams:
        raise RuntimeError("{} has no video stream that ffprobe can see".format(movie))
    stream = streams[0]

    fps = _parse_ratio(stream.get('r_frame_rate'))
    if fps is None:
        raise RuntimeError(
            "Could not read a frame rate from {}. Without it there is no mapping "
            "from frame number to movie time.".format(movie))

    # Recommendation 22's validity check. r_frame_rate is the *base* rate and
    # avg_frame_rate the average actually achieved; on CFR content they agree.
    avg_fps = _parse_ratio(stream.get('avg_frame_rate'))
    cfr = True
    if avg_fps is not None and avg_fps != fps:
        drift = abs(float(avg_fps) - float(fps)) / float(fps)
        if drift > 0.001:
            cfr = False
            logging.warning(
                "This movie looks variable frame rate: r_frame_rate=%s but "
                "avg_frame_rate=%s (%.3f%% apart). Frame numbers are mapped to "
                "timestamps as frame/fps, which is only valid for constant "
                "frame rate, so the frames displayed may not be the frames "
                "requested. Re-encode to CFR if the ordering looks wrong.",
                fps, avg_fps, drift * 100)

    duration = stream.get('duration')
    if duration in (None, 'N/A'):
        # Some containers only carry duration at the format level.
        duration = (parsed.get('format') or {}).get('duration')
    if duration in (None, 'N/A'):
        raise RuntimeError("Could not read a duration from {}".format(movie))
    duration = float(duration)

    width = int(stream['width'])
    height = int(stream['height'])
    sar = _parse_ratio(stream.get('sample_aspect_ratio'), Fraction(1))

    # Two independent estimates of the total, so a container lying about
    # nb_frames is caught rather than trusted.
    derived = int(round(duration * float(fps)))
    advertised = stream.get('nb_frames')
    advertised = int(advertised) if advertised not in (None, 'N/A') else None

    if count_frames:
        # Exact, and expensive: this decodes the entire movie.
        logging.info("Counting frames exactly, this decodes the whole movie and will be slow")
        counted = run_tool([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-count_frames', '-show_entries', 'stream=nb_read_frames',
            '-of', 'default=nokey=1:noprint_wrappers=1', str(movie),
        ], COUNT_FRAMES_TIMEOUT_S, 'ffprobe exact frame count').strip()
        total = int(counted)
        source = 'counted'
    elif advertised is not None:
        total = advertised
        source = 'container nb_frames'
        if abs(advertised - derived) > FRAME_COUNT_TOLERANCE:
            logging.warning(
                "Container says %d frames but duration x fps gives %d. Using the "
                "container value. If playback runs off the end of the movie or "
                "stops early, re-run once with --count-frames for an exact count.",
                advertised, derived)
    else:
        total = derived
        source = 'duration x fps'
        logging.info("Container carries no nb_frames, using duration x fps = %d", total)

    if total <= 0:
        raise RuntimeError("Refusing to play {}: computed {} frames".format(movie, total))

    geometry = panel_geometry(width, height, sar)
    info = {
        'fps': fps,
        'duration_s': duration,
        'total_frames': total,
        'frame_count_source': source,
        'width': width,
        'height': height,
        'sar': sar,
        'cfr': cfr,
        'geometry': geometry,
    }

    logging.info(
        "Probed %s: %dx%d sar=%s dar=%.4f fps=%s (%.4f) duration=%s "
        "frames=%d (%s) -> scaling to %dx%d padded to %dx%d",
        movie.name, width, height, sar, geometry['display_aspect'],
        fps, float(fps), format_timecode(duration), total, source,
        geometry['width'], geometry['height'], PANEL_W, PANEL_H)
    return info


class EndOfMovie(Exception):
    """Raised when a seek lands past the end of the stream.

    Distinct from a frame error on purpose. The recommendation 7 error handler
    skips bad frames and gives up after ten in a row; running off the end of the
    movie is not a fault and must not be reported as ten failures.
    """


def extract_frame(movie, out_path, frame, info):
    """Extract one frame as an 800x480 grayscale PNG, ready for the panel.

    This is recommendations 21, 22, 26 and 28 in one command, and it is the
    change the whole rewrite exists for.

    ``-ss`` goes **before** ``-i``, which is input seeking: ffmpeg uses the
    container index to jump to the keyframe preceding the target and decodes
    forward from there. The old ``-vf "select=gte(n\\,N)"`` decoded every frame
    from the start of the input, so extraction got steadily slower as a section
    progressed. Measured on the 171,437 frame movie in this workspace,
    ``select=gte`` took 0.15s at frame 100, 1.78s at frame 10,000 and 8.55s at
    frame 50,000, while input seeking stayed between 0.13s and 0.21s all the way
    to frame 171,000. The cost is bounded by keyframe spacing rather than
    growing with position, which is what makes the timing in play_movie()
    meaningful.

    ``-accurate_seek`` is ffmpeg's default and is passed explicitly because the
    frame accuracy of this whole approach depends on it; it should not be
    possible to lose it to a change of default without noticing. It is
    frame-accurate when transcoding (writing a PNG counts) rather than stream
    copying.

    Scaling happens here rather than in PIL for two reasons. It fixes the
    aspect ratio (recommendation 26), and it fixes the resampling quality
    problem in recommendation 27: PIL always uses nearest-neighbour for 'P' and
    '1' mode images, so any resize after a mode conversion is aliased. Doing
    the resize in ffmpeg on full-depth video means the only mode conversion
    left is the final 1-bit dither inside the driver's getbuffer().

    PNG rather than JPEG (recommendation 28): JPEG ringing artifacts get
    amplified by a hard 1-bit dither, and one PNG at a time costs nothing.
    """
    geometry = info['geometry']
    timestamp = format_seconds(frame_timestamp(frame, info['fps']))
    video_filter = (
        'scale={w}:{h},'
        'pad={pw}:{ph}:{px}:{py}:color=white,'
        'format=gray'
    ).format(w=geometry['width'], h=geometry['height'],
             pw=PANEL_W, ph=PANEL_H,
             px=geometry['pad_x'], py=geometry['pad_y'])

    run_tool([
        'ffmpeg', '-y', '-nostdin',
        '-accurate_seek',
        '-ss', timestamp,          # BEFORE -i: input seeking, not output seeking
        '-i', str(movie),
        '-map', '0:v:0',           # exactly the first video stream, ignore the rest
        '-frames:v', '1',
        '-update', '1',            # tells the image2 muxer this is a single file
        '-vf', video_filter,
        '-f', 'image2',
        str(out_path),
    ], FFMPEG_TIMEOUT_S, 'ffmpeg frame extract')

    # ffmpeg exits 0 having written nothing when the seek target is past the end
    # of the stream -- verified against real ffmpeg. Without this check that
    # becomes a confusing PIL error on a missing file, and at the end of a movie
    # it would be misread as ten corrupt frames in a row.
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise EndOfMovie(
            "ffmpeg produced no frame at {}s (frame {}), which means the seek "
            "target is past the end of the stream".format(timestamp, frame))

    return timestamp


# --- state -------------------------------------------------------------------


def utc_now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def new_state(movie_name, info):
    """The initial state for a movie played from the beginning.

    Recommendations 8, 9 and 10. Two things changed from ``progress.pkl``:

      * **The frame index is absolute.** It used to reset to 0 at every section
        boundary, so the stored number was meaningless without also knowing
        which section was playing. There are no sections now, so
        ``frame`` is the single source of truth and everything else --
        timecode, percent, position -- is derived from it.
      * **It is JSON.** ``progress.pkl`` needed a bespoke script
        (``edit_pickle.py``) to read, could not be inspected over SSH, and
        unpickling an attacker-writable file is arbitrary code execution.
    """
    return {
        'schema': STATE_SCHEMA,
        'movie': movie_name,
        'frame': 0,
        'total_frames': info['total_frames'],
        'fps': str(info['fps']),
        'duration_s': round(info['duration_s'], 3),
        'frame_interval_s': FRAME_INTERVAL_S,
        'percent': 0.0,
        'timecode': '00:00:00.000',
        'finished': False,
        'last_frame_utc': None,
        'next_frame_utc': None,
        'run_started_utc': utc_now_iso(),
        'frames_this_run': 0,
        'last_extract_s': None,
        'last_display_s': None,
        'errors': 0,
        'anomalies': 0,
    }


def save_state(path, state):
    """Write `state` to `path` as JSON, atomically.

    This is called after every single frame, which means it runs roughly 576
    times a day for months. The original code opened the real file with mode
    'wb' and pickled straight into it. That truncates the file to zero bytes
    first, so the player spent a small slice of every frame with its state file
    in a half-written state. Losing power in that window left a truncated file,
    and the next start raised on it -- the run was over until someone SSHed in.

    The fix is write-then-swap: build a complete temp file, force it to disk,
    then move it into place in one indivisible step. A reader at any instant
    sees either the previous good file or the new good file, never a partial
    one. That matters more now than it did with a pickle, because the whole
    point of JSON is that a human can `cat` this file while the player is
    running.
    """
    target = Path(path)
    # The temp file must sit in the same directory as the target. os.replace()
    # is only atomic within a single filesystem, so writing to /tmp and moving
    # across would degrade into a non-atomic copy.
    tmp = target.with_name(target.name + '.tmp')
    try:
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write('\n')
            # flush() only pushes bytes out of Python's buffer into the OS page
            # cache; fsync() is what forces them onto the SD card. Without this
            # the swap below can publish a file whose contents have not actually
            # been written yet, which is the very failure being fixed here.
            f.flush()
            os.fsync(f.fileno())

        # Atomic on POSIX: the target either still points at the old file or
        # points at the fully written new one.
        os.replace(tmp, target)

        # The rename itself also needs flushing, otherwise the directory entry
        # can be lost even though the file contents survived. Best effort --
        # not every filesystem allows fsync on a directory handle, and failing
        # to harden the rename is not worth losing an already-saved frame over.
        try:
            # For a bare filename like 'state.json' this is Path('.').
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            logging.debug("Could not fsync directory for %s (harmless)", target)

    except Exception:
        # Leave no stale .tmp lying around to confuse the next run or a human
        # poking at the directory over SSH.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_saved_frame(path):
    """Read just the frame number back off disk, or None if it cannot be read.

    Used for the per-frame position check in play_movie (recommendation 16).
    Returns None rather than raising: the caller treats that as an anomaly to
    report, and a failure to *verify* the position must not be what ends a
    months-long run.

    Honest limitation: shortly after save_state() this read is served from the
    page cache, so it confirms the file's contents are what we wrote and that
    nothing else has overwritten them. It does not independently confirm the
    bytes reached the SD card -- the fsync in save_state() is what covers that.
    """
    try:
        with open(path) as f:
            return json.load(f).get('frame')
    except Exception:
        logging.exception("Could not read the position back from %s to verify it", path)
        return None


def load_state(path, movie_name, info, restart=False):
    """Load saved state, or build fresh state, refusing to guess.

    Every branch here is a deliberate decision about a case that could
    otherwise silently lose someone's position in a film they have been
    watching for a month.
    """
    target = Path(path)

    if restart:
        if target.exists():
            logging.warning("--restart given, discarding saved position and starting from frame 0")
        return new_state(movie_name, info)

    if not target.exists():
        legacy = Path(LEGACY_STATE_FILE)
        if legacy.exists():
            # Deliberately not migrated. The old file's 'frame' was an index
            # *within a section*, and recovering an absolute position from it
            # would need the sliced section files to add up the frames in
            # every section already played. Guessing would put the player at a
            # confidently wrong position, which is worse than starting over.
            logging.warning(
                "Found the old %s but no %s. It is being ignored, not migrated: its "
                "'frame' was an index within a section, so it cannot be converted to an "
                "absolute frame number without the section files. Starting from frame 0. "
                "Delete %s once you are happy, or use 'status' to check the new state.",
                LEGACY_STATE_FILE, STATE_FILE, LEGACY_STATE_FILE)
        else:
            logging.info("No %s yet, starting from frame 0", STATE_FILE)
        return new_state(movie_name, info)

    # A corrupt file is NOT silently replaced with defaults. Those defaults
    # include frame 0, so falling back would quietly restart a months-long
    # movie from the beginning. Failing loudly leaves the file on disk to be
    # inspected -- and being JSON, it can now be repaired by hand.
    with open(target) as f:
        state = json.load(f)

    if state.get('schema') != STATE_SCHEMA:
        raise RuntimeError(
            "{} has schema {!r}, this version of vsmp writes schema {}. Refusing to "
            "guess at its meaning. Move it aside to start over, or edit it to match."
            .format(target, state.get('schema'), STATE_SCHEMA))

    if state.get('movie') != movie_name:
        raise RuntimeError(
            "{} holds the position for {!r} but you asked to play {!r}. Refusing to "
            "resume one movie at another's frame number. Pass --restart to start {!r} "
            "from the beginning, or move the state file aside to keep the old position."
            .format(target, state.get('movie'), movie_name, movie_name))

    # The movie file can be replaced in place. Re-probing every start is cheap
    # and catches that, where trusting the cached total would run off the end.
    if state.get('total_frames') != info['total_frames']:
        logging.warning(
            "Saved total of %s frames does not match the %s frames probed now. "
            "The movie file may have been re-encoded. Using the probed value.",
            state.get('total_frames'), info['total_frames'])
        state['total_frames'] = info['total_frames']

    frame = state.get('frame')
    if not isinstance(frame, int) or frame < 0:
        raise RuntimeError("{} has a nonsensical frame number {!r}".format(target, frame))

    # Fields added by later versions, so a hand-edited or older file still works.
    for key, value in new_state(movie_name, info).items():
        state.setdefault(key, value)
    state['run_started_utc'] = utc_now_iso()
    state['frames_this_run'] = 0

    logging.info("Resuming %s at frame %d of %d (%.2f%%)",
                 movie_name, frame, state['total_frames'],
                 100.0 * frame / max(1, state['total_frames']))
    return state


# --- rendering ---------------------------------------------------------------


def prepare_for_panel(frame_path, contrast):
    """Load the extracted PNG and apply contrast, leaving the 1-bit conversion
    to the driver.

    Recommendations 27 and 29. The image arrives from ffmpeg already 800x480 and
    already grayscale, so there is no resize and no ``convert('P')`` here. That
    ordering is the point: PIL always resamples 'P' and '1' mode images with
    nearest-neighbour, so the old resize-after-convert path would have produced
    an aliased downscale. The driver's ``getbuffer()`` calls ``convert('1')``,
    which applies Floyd-Steinberg dithering, and that is now the only mode
    change in the pipeline.

    Contrast is a CLI option rather than a magic constant (recommendation 29).
    Note the default of 1.0 is a no-op, which matches what the code actually did
    before: ``display_frame`` called ``convert_image(im, enhance=False)``, and
    the enhance value at HEAD was ``enhance(1)`` -- also a no-op. Contrast
    enhancement was doing nothing, twice over. The right value has to be chosen
    by eye on the real panel; 1.0 keeps the current appearance until someone
    does that.
    """
    im = Image.open(frame_path)
    im.load()
    if contrast != 1.0:
        im = ImageEnhance.Contrast(im).enhance(contrast)
    return im


def discard_bad_frame(frame_path):
    """Delete a frame image we could not use.

    Leaving a truncated or zero-byte PNG on disk used to be a trap: the old
    guard treated a zero-byte file as "needs extracting", so the bad file was
    handed back to ffmpeg on a later pass -- which, before -y was added, was
    itself a source of silent hangs (recommendation 36).

    That reuse path is gone now: every frame is extracted fresh, so a stale file
    can no longer be mistaken for a good one. This is kept anyway, because
    leaving debris on an SD card to accumulate for months is its own problem.
    """
    try:
        Path(frame_path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logging.exception("Could not delete the unusable frame %s", frame_path)


def render_one_frame(movie, frame, epd, info, frames_dir, contrast):
    """Extract, convert and show a single frame. Raises if anything goes wrong.

    Kept as one function so the whole operation can be wrapped in a single
    try/except by the caller (recommendation 7). Nothing in here touches the
    loop counter or the saved state, so a failure part-way through leaves no
    inconsistent state behind to unpick.

    The frame number is in the filename and the file is always written fresh.
    The old code reused an existing file if it was non-empty, which saved a
    fraction of a second once per restart and in exchange created the zero-byte
    trap described in recommendation 36.
    """
    frame_path = frames_dir / 'frame_{}.png'.format(frame)
    try:
        extract_started = time.monotonic()
        extract_frame(movie, frame_path, frame, info)
        extract_s = time.monotonic() - extract_started

        image = prepare_for_panel(frame_path, contrast)

        display_started = time.monotonic()
        display_on_e_ink(epd, image)
        display_s = time.monotonic() - display_started
    finally:
        # Always clean up, including on the failure paths. One frame at a time
        # is the whole storage budget; recommendation 35 (keeping this on tmpfs)
        # becomes trivial from here.
        discard_bad_frame(frame_path)

    return extract_s, display_s


def display_on_e_ink(epd, image_to_display):
    """Push one image to the panel.

    The size guard is not defensive noise. ``getbuffer()`` in the vendored
    driver responds to an unexpected image size by logging a warning and
    returning an all-zero buffer, and because the e-paper convention is
    inverted relative to PIL, that buffer paints the panel **solid black**. So a
    geometry bug would not raise, it would silently display black frames for as
    long as nobody looked at the panel. Raising here turns that into an error
    that the recommendation 7 handler counts and the log records.
    """
    if image_to_display.size != (epd.width, epd.height):
        raise ValueError(
            "Refusing to display a {}x{} image on a {}x{} panel: the driver would "
            "silently paint the panel solid black rather than fail.".format(
                image_to_display.size[0], image_to_display.size[1],
                epd.width, epd.height))

    epd.init()
    epd.Clear()
    epd.display(epd.getbuffer(image_to_display))
    epd.sleep()


def release_display():
    """Power the panel down and close the SPI bus.

    Call this on every abnormal exit path. If we die still holding SPI and with
    the panel's 5V rail on, the next process to start (a supervisor restarting
    us, or a human over SSH) may fail to initialise the display, turning a
    one-off error into a stuck player.

    Note this is called with no arguments deliberately: only the RaspberryPi
    implementation in epdconfig.py accepts `cleanup`, the other platform
    implementations take none. The kernel reclaims the GPIO pins when the
    process exits anyway, so the useful part here is the power-down.

    Failures are logged and swallowed. This runs while we are already handling
    an error, and a cleanup failure must not mask the original problem.
    """
    try:
        # Lazy import to match cmd_play: the driver must never be loaded on the
        # status path. This only runs on an abnormal exit from cmd_play, by
        # which point the import has already succeeded, so the cost here is just
        # a dictionary lookup of the cached module.
        from epd import epd7in5_V2_old
        epd7in5_V2_old.epdconfig.module_exit()
        logging.info("Display released (SPI closed, panel powered down)")
    except Exception:
        logging.exception("Could not release the display cleanly, continuing to exit")


def clean_frame_dir(frames_dir):
    """Remove leftover frame images from previous runs.

    Includes the old ``out_img*.jpg`` naming, so upgrading clears the zero-byte
    files that recommendation 36 was about instead of asking a human to run
    `find -size 0 -delete` on the Pi.
    """
    removed = 0
    for pattern in ('frame_*.png', 'out_img*.jpg'):
        for stale in frames_dir.glob(pattern):
            try:
                stale.unlink()
                removed += 1
            except OSError:
                logging.warning("Could not remove leftover frame %s", stale)
    if removed:
        logging.info("Cleared %d leftover frame file(s) from %s", removed, frames_dir)


# --- the frame loop ----------------------------------------------------------


def play_movie(epd, movie, state_path, state, info, contrast):
    """Display the movie one frame at a time, in order, at 24 frames per hour.

    Recommendation 15: one loop over the single source file, from
    ``state['frame']`` to ``total_frames``. No sections, so no section
    boundaries to drop or duplicate frames at, no ``os.listdir()`` being used as
    a section count, and no ``- 5`` quietly skipping the last five frames of
    each of twenty sections.

    Recommendation 24: the schedule is anchored to absolute deadlines.
    """
    frames_dir = Path('{}_frames'.format(movie.stem))
    frames_dir.mkdir(parents=True, exist_ok=True)
    clean_frame_dir(frames_dir)

    total = state['total_frames']
    consecutive_errors = 0

    # Recommendation 24. The old code measured how long the work took and slept
    # `150 - lapse`, which drifts: everything outside the measured window (the
    # state write, the logging, the loop itself) accumulates, and when the work
    # took longer than 150s it just logged and made no attempt to recover.
    #
    # Deadlines are computed from a fixed epoch instead, so per-frame error
    # cannot accumulate. monotonic() rather than time() because a Pi has no
    # battery-backed clock and can take a large NTP step shortly after boot,
    # which would otherwise skew or collapse the schedule.
    next_deadline = time.monotonic()

    while state['frame'] < total:
        frame = state['frame']

        extract_s = display_s = None
        try:
            extract_s, display_s = render_one_frame(
                movie, frame, epd, info, frames_dir, contrast)
            consecutive_errors = 0

        except EndOfMovie:
            # Not an error. The container's frame count was optimistic, which is
            # common, so stop cleanly here rather than letting the recommendation
            # 7 handler report the last few frames as failures.
            logging.info(
                "Reached the end of the stream at frame %d, though the frame count "
                "said %d. Treating the movie as finished.", frame, total)
            state['total_frames'] = frame
            break

        except TimeoutError:
            # Raised by ReadBusy() when the panel never released the BUSY line.
            # This is not a bad frame -- the display itself is wedged, and no
            # amount of moving on to the next frame will help. Let it propagate
            # so the process exits and systemd restarts us with a fresh panel
            # init. Swallowing this here would undo the whole point of the BUSY
            # timeout.
            raise

        except Exception:
            # One frame failed. A months-long run should not end because of it,
            # so log it properly, record it, and move on to the next frame.
            #
            # Note the frame is SKIPPED rather than retried. Retrying the same
            # frame forever would look healthy to the watchdog -- pings would
            # keep flowing while the screen never changed -- which is a worse
            # failure than losing one frame out of roughly two hundred thousand.
            consecutive_errors += 1
            state['errors'] = state.get('errors', 0) + 1
            logging.exception(
                "Frame %d failed (%d in a row, %d total for this movie), skipping it",
                frame, consecutive_errors, state['errors'])

            if consecutive_errors >= MAX_CONSECUTIVE_FRAME_ERRORS:
                # Not bad luck any more. Something systemic is wrong: the movie
                # file has gone, the disk is full, the panel is failing. Stop,
                # so systemd restarts us and -- if it keeps happening --
                # StartLimitBurst surfaces the unit as failed instead of letting
                # it quietly skip its way through the whole film.
                state['frame'] = frame + 1
                save_state(state_path, state)
                raise RuntimeError(
                    "{} frames failed in a row, giving up rather than skipping "
                    "through the movie".format(consecutive_errors))

        # Reached on success and on a skipped frame alike. Advancing in both
        # cases is what stops a single bad frame blocking the run forever.
        state['frame'] = frame + 1
        state['frames_this_run'] = state.get('frames_this_run', 0) + 1

        # Recommendation 10: everything a human might want to know, derived from
        # the absolute frame number and written every frame.
        position_s = float(Fraction(state['frame']) / info['fps'])
        state['timecode'] = format_timecode(position_s)
        state['percent'] = round(100.0 * state['frame'] / max(1, total), 4)
        state['last_frame_utc'] = utc_now_iso()
        state['last_extract_s'] = round(extract_s, 3) if extract_s is not None else None
        state['last_display_s'] = round(display_s, 3) if display_s is not None else None

        # Work out the next deadline before saving, so the state file can
        # advertise when the next frame is due.
        next_deadline += FRAME_INTERVAL_S
        now_monotonic = time.monotonic()
        behind = now_monotonic - next_deadline
        if behind > 0:
            # Explicit overrun policy, which recommendation 24 asked for as a
            # decision rather than a side effect. We do NOT skip ahead to the
            # frame matching wall-clock time: this is a movie meant to be seen
            # in full, and dropping frames to keep a schedule defeats the point.
            # Falling behind just means the film finishes a little later.
            #
            # Re-anchoring is what stops that becoming a sprint. Left alone, a
            # long stall would leave several deadlines already in the past and
            # the player would fire frames back-to-back with no wait to catch
            # up, which is the one behaviour clearly worse than either option.
            logging.warning(
                "Behind schedule by %.1fs after frame %d. Not skipping frames; "
                "re-anchoring the schedule so the backlog is not raced through.",
                behind, frame)
            next_deadline = now_monotonic + FRAME_INTERVAL_S

        state['next_frame_utc'] = datetime.fromtimestamp(
            time.time() + (next_deadline - time.monotonic()), timezone.utc
        ).strftime('%Y-%m-%dT%H:%M:%SZ')

        if state['frame'] >= total:
            state['finished'] = True

        save_state(state_path, state)

        # Recommendation 16, which asked for a *positive* signal that ordering is
        # correct rather than merely an absence of evidence.
        #
        # Note what this deliberately does not do. The obvious implementation --
        # comparing state['frame'] against the previous iteration's value -- is
        # tautological here, because the line above is the only thing that ever
        # writes it, from this loop's own counter. It would pass unconditionally
        # and prove nothing.
        #
        # Reading the position back off disk is independent of the loop counter,
        # so it can actually fail, and the ways it fails are real: a second
        # player started by hand while the service is running (DEPLOY.md warns
        # about exactly this), a save that silently did not take, or a corrupted
        # file. Any of those mean the recorded position is no longer the position
        # being played, which is the thing that has to be trustworthy for
        # "is it in order?" to have an answer.
        on_disk = read_saved_frame(state_path)
        if on_disk != state['frame']:
            state['anomalies'] = state.get('anomalies', 0) + 1
            logging.warning(
                "Position check failed: %s records frame %r but the player is at "
                "frame %d (anomalies this movie: %d). The usual cause is a second "
                "vsmp writing the same state file -- check for another process "
                "before trusting the position.",
                state_path, on_disk, state['frame'], state['anomalies'])

        # The position is safely on disk, so the loop has demonstrably moved
        # forward. This is the right place to tell the watchdog we are alive.
        watchdog_ping()

        # Recommendation 12: one structured, greppable line per frame instead of
        # six scattered logging.info calls. Everything here is machine-parseable
        # so progress can be plotted from the log after the fact.
        logging.info(
            "frame=%d/%d pct=%.3f tc=%s extract_s=%s display_s=%s "
            "next=%s errors=%d anomalies=%d",
            state['frame'], total, state['percent'], state['timecode'],
            'skip' if extract_s is None else '{:.2f}'.format(extract_s),
            'skip' if display_s is None else '{:.2f}'.format(display_s),
            state['next_frame_utc'], state['errors'], state['anomalies'])

        if state['frame'] >= total:
            break

        sleep_until(next_deadline)

    state['finished'] = True
    state['next_frame_utc'] = None
    save_state(state_path, state)
    logging.info("Movie finished at frame %d of %d (%d errors, %d anomalies)",
                 state['frame'], state['total_frames'],
                 state['errors'], state['anomalies'])
    epd.sleep()


# --- subcommands -------------------------------------------------------------


def read_state(state_path=STATE_FILE):
    """Load and parse the state file, or return None if it does not exist.

    Reads only, and the player writes state.json atomically, so this is safe to
    call at any time -- including from another process such as the web server
    (see webapp.py), which is why this is a standalone function rather than
    being inlined in cmd_status.
    """
    target = Path(state_path)
    if not target.exists():
        return None
    with open(target) as f:
        return json.load(f)


def summarize_state(state):
    """Turn a raw state dict into the derived, display-ready summary.

    This is the single source of truth for "where is the player, in numbers a
    human cares about": percent, frame counts, timecodes and the remaining-time
    estimate. cmd_status prints from it and the web server renders from it, so
    the CLI and the web page can never drift apart. Pure and side-effect free;
    give it a dict, get a dict back.
    """
    total = state.get('total_frames') or 0
    frame = state.get('frame') or 0
    finished = bool(state.get('finished'))

    summary = {
        'movie': state.get('movie'),
        'frame': frame,
        'total_frames': total,
        'percent': state.get('percent', 0.0),
        'timecode': state.get('timecode'),
        'duration': format_timecode(state.get('duration_s') or 0),
        'duration_s': state.get('duration_s') or 0,
        'state': 'finished' if finished else 'playing',
        'finished': finished,
        'last_frame_utc': state.get('last_frame_utc') or 'never',
        'next_frame_utc': state.get('next_frame_utc') or 'not scheduled',
        'frames_this_run': state.get('frames_this_run', 0),
        'run_started_utc': state.get('run_started_utc'),
        'last_extract_s': state.get('last_extract_s'),
        'last_display_s': state.get('last_display_s'),
        'errors': state.get('errors', 0),
        'anomalies': state.get('anomalies', 0),
        'remaining_frames': None,
        'remaining_days': None,
        'frames_per_hour': FRAMES_PER_HOUR,
    }

    if total and not finished:
        remaining_frames = total - frame
        summary['remaining_frames'] = remaining_frames
        summary['remaining_days'] = remaining_frames / FRAMES_PER_HOUR / 24

    return summary


def cmd_status(args):
    """Print the current position (recommendation 11).

    The point is that ``ssh pi 'cd vsmp && python3 vsmp.py status'`` answers
    "where is it?" in one command, without unpickling anything or reading the
    log. Reads only; safe to run while the player is going, because state writes
    are atomic.
    """
    state = read_state(args.state)
    if state is None:
        print("No {} yet -- the player has not written a frame.".format(args.state))
        if Path(LEGACY_STATE_FILE).exists():
            print("(An old {} is present. It is not used by this version.)"
                  .format(LEGACY_STATE_FILE))
        return 1

    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    s = summarize_state(state)
    width = 40
    filled = int(width * s['frame'] / s['total_frames']) if s['total_frames'] else 0

    print("movie      {}".format(s['movie']))
    print("progress   [{}{}] {:.3f}%".format('#' * filled, '.' * (width - filled),
                                             s['percent']))
    print("frame      {} of {}".format(s['frame'], s['total_frames']))
    print("timecode   {} of {}".format(s['timecode'], s['duration']))
    print("state      {}".format(s['state']))
    print("last frame {}".format(s['last_frame_utc']))
    print("next frame {}".format(s['next_frame_utc']))
    print("this run   {} frames since {}".format(
        s['frames_this_run'], s['run_started_utc']))
    print("last frame took  extract {}s, display {}s".format(
        s['last_extract_s'], s['last_display_s']))
    print("errors     {}   anomalies {}".format(s['errors'], s['anomalies']))

    if s['remaining_frames'] is not None:
        print("remaining  {} frames, about {:.1f} days at {} frames/hour".format(
            s['remaining_frames'], s['remaining_days'], s['frames_per_hour']))
    return 0


def cmd_play(args):
    movie = Path(args.filename)
    if not movie.exists():
        raise SystemExit("No such movie file: {}".format(movie))

    # Recommendation 31. The old code used filename.split(".")[0] for the movie
    # name and filename.split(".")[1] for the extension, which broke on any
    # filename containing a dot or a directory component -- and every real
    # release filename contains dots. The movie in this workspace,
    # 'Howl's.Moving.Castle.2004.1080p.x265-Rapta.mp4', became a movie named
    # "Howl's" with an extension of ".Moving". pathlib gets this right, so the
    # movie no longer has to be renamed to run.
    movie_name = movie.name

    # Imported here rather than at module top so that `vsmp.py status` never
    # loads the driver -- importing epd/epdconfig.py claims the GPIO pins as a
    # side effect, which makes status fail with 'GPIO busy' whenever the player
    # is already running. See the note by the imports at the top of the file.
    from epd import epd7in5_V2_old

    configure_watchdog()

    info = probe_video(movie, count_frames=args.count_frames)
    state = load_state(args.state, movie_name, info, restart=args.restart)

    if state['finished'] and not args.restart:
        # Same behaviour as before, but explicit. systemd's Restart=always will
        # bring us straight back; StartLimitBurst in the unit is what stops that
        # becoming an endless loop, and the unit ends up in `failed`, which is
        # visible in `systemctl status`.
        logging.info("%s is already finished (%d frames). Nothing to do; "
                     "pass --restart to play it again.",
                     movie_name, state['total_frames'])
        return 0

    epd = epd7in5_V2_old.EPD()
    logging.info("init and Clear")
    epd.init()
    epd.Clear()
    play_movie(epd, movie, args.state, state, info, args.contrast)
    logging.info("Finished!")
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Very slow movie player: 24 frames per hour on an e-paper panel.")
    sub = parser.add_subparsers(dest='command')

    play = sub.add_parser('play', help="play a movie (the default)")
    play.add_argument("filename", help="the movie file, e.g. movie.mp4")
    play.add_argument("--state", default=STATE_FILE,
                     help="where to keep the position (default: %(default)s)")
    play.add_argument("--restart", action='store_true',
                      help="ignore any saved position and start from frame 0")
    play.add_argument("--contrast", type=float, default=1.0,
                      help="contrast factor before the 1-bit dither. 1.0 is a "
                           "no-op and is the default; the right value depends on "
                           "the movie and has to be judged on the panel "
                           "(default: %(default)s)")
    play.add_argument("--count-frames", action='store_true',
                      help="count frames exactly instead of trusting the "
                           "container. Decodes the whole movie, so it is slow, "
                           "but it is the fix if the frame count looks wrong")
    play.set_defaults(func=cmd_play)

    status = sub.add_parser('status', help="print where the player has got to")
    status.add_argument("--state", default=STATE_FILE,
                        help="the state file to read (default: %(default)s)")
    status.add_argument("--json", action='store_true',
                        help="print the raw state file instead of a summary")
    status.set_defaults(func=cmd_status)

    # Drop empty arguments before parsing.
    #
    # The unit's ExecStart ends with `$VSMP_ARGS`, an optional list of extra
    # options from /etc/default/vsmp. systemd documents the unbraced `$VAR` form
    # as splitting on whitespace and expanding to *no arguments at all* when the
    # variable is empty or unset, which is exactly what is wanted. But if that
    # ever yields a single empty argument instead, argparse rejects it with
    # "unrecognized arguments" and exits 2 -- and since VSMP_ARGS is normally
    # empty, that would mean the service failing to start on every deployment
    # rather than in some rare corner.
    #
    # The consequence is bad and the guard is one line, so this does not rely on
    # getting another program's expansion rules right. An empty string is never a
    # meaningful argument here in any case.
    argv = [a for a in argv if a != '']

    # Backwards compatibility: `vsmp.py movie.mp4` still means
    # `vsmp.py play movie.mp4`. The systemd unit installed on the Pi runs the
    # bare form, and pulling new code does not update /etc/systemd/system, so
    # breaking it here would break a running deployment on upgrade.
    if argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith('-'):
        argv.insert(0, 'play')

    args = parser.parse_args(argv)
    if not getattr(args, 'command', None):
        parser.error("nothing to do: give a movie filename, or the 'status' subcommand")
    return args


def main(argv):
    args = parse_args(argv)

    # `status` reads a file and prints it. It must not touch the panel, and it
    # must not be caught by the display cleanup below. It also does not set up
    # file logging: it is a pure read, and configuring the RotatingFileHandler
    # would open log.txt for writing for no reason.
    if args.func is cmd_status:
        return cmd_status(args)

    # Only the player writes a log. Configure it here rather than at import
    # time so that importing this module (e.g. from the web server) has no
    # side effects. A restart is the single most useful thing to find in the
    # log, so mark it explicitly now that previous runs are no longer
    # overwritten (configure_logging appends rather than truncates).
    configure_logging()
    logging.info('=== vsmp starting ===')

    try:
        return cmd_play(args)

    except KeyboardInterrupt:
        # A deliberate Ctrl-C is not a fault, so exit 0 to distinguish it from a
        # crash. This needs its own handler because KeyboardInterrupt inherits
        # from BaseException rather than Exception, so the handler below would
        # never catch it.
        logging.info("Interrupted by user (ctrl + c), shutting down")
        release_display()
        return 0

    except Exception:
        # logging.exception() writes the message AND the full traceback to
        # log.txt, which survives restarts. The bare `raise` then re-raises the
        # original exception with its traceback intact, so the process still
        # exits non-zero and a supervisor can see it failed.
        #
        # This replaces `except IOError as e: raise Exception(logging.info(e))`,
        # which had two bugs: logging.info() returns None, so it raised
        # Exception(None) and destroyed both the message and the traceback; and
        # only IOError was caught, so a PIL decode error or an SPI error killed
        # the run with a traceback on stderr that `nohup ... 2>&1 &` sent to
        # /dev/null.
        logging.exception("Unhandled error, shutting down")
        release_display()
        raise


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
