"""Timestamp maths, geometry, tool invocation, probing and frame extraction.

This is the suite that justifies the rewrite: it checks that input seeking
returns the same frames the old select=gte() path did, and that it does so at a
cost that does not grow with position in the film.
"""
import json
import os
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import harness
from harness import check, section, skip, sha256

import vsmp
from PIL import Image

SYNTH = harness.ensure_fixture()
REAL = harness.REAL_MOVIE


def ground_truth_frame(movie, frame, info, out_path):
    """Extract frame N the OLD way, with select=gte() and the SAME filter chain.

    This is the reference the new input-seeking path is measured against. If the
    two files are byte-identical then the new path chose the same frame and
    processed it identically, which is a stronger statement than comparing
    pixels with a tolerance.
    """
    g = info['geometry']
    vf = ('select=gte(n\\,{n}),scale={w}:{h},'
          'pad={pw}:{ph}:{px}:{py}:color=white,format=gray').format(
        n=frame, w=g['width'], h=g['height'],
        pw=vsmp.PANEL_W, ph=vsmp.PANEL_H, px=g['pad_x'], py=g['pad_y'])
    subprocess.run([
        'ffmpeg', '-y', '-nostdin', '-loglevel', 'error',
        '-i', str(movie), '-map', '0:v:0', '-vf', vf,
        '-frames:v', '1', '-update', '1', '-f', 'image2', str(out_path),
    ], check=True, stdin=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
section("timestamp and formatting maths (rec 22)")

fps_real = Fraction(1199, 50)

check("frame_timestamp uses (frame - 0.5) / fps",
      vsmp.frame_timestamp(50000, fps_real)
      == (Fraction(50000) - Fraction(1, 2)) / fps_real)
check("frame 0 is clamped to zero, never a negative seek",
      vsmp.frame_timestamp(0, fps_real) == 0,
      vsmp.frame_timestamp(0, fps_real))
check("returns an exact Fraction, not a float",
      isinstance(vsmp.frame_timestamp(12345, fps_real), Fraction))
check("no drift at frame 171000 (exact rational arithmetic)",
      vsmp.frame_timestamp(171000, fps_real)
      == (Fraction(171000) - Fraction(1, 2)) / fps_real)

check("format_seconds is exact decimal",
      vsmp.format_seconds(Fraction(2499975, 1199)) == '2085.050042',
      vsmp.format_seconds(Fraction(2499975, 1199)))
check("format_seconds pads fractional digits",
      vsmp.format_seconds(Fraction(1, 4)) == '0.250000')
check("format_seconds handles zero",
      vsmp.format_seconds(Fraction(0)) == '0.000000')
check("format_seconds never emits scientific notation",
      'e' not in vsmp.format_seconds(Fraction(1, 1000000)),
      vsmp.format_seconds(Fraction(1, 1000000)))

check("format_timecode renders h/m/s/ms",
      vsmp.format_timecode(3661.234) == '01:01:01.234',
      vsmp.format_timecode(3661.234))
check("format_timecode handles zero", vsmp.format_timecode(0) == '00:00:00.000')

check("_parse_ratio reads num/den", vsmp._parse_ratio('1199/50') == Fraction(1199, 50))
check("_parse_ratio reads num:den", vsmp._parse_ratio('4920:4921') == Fraction(4920, 4921))
check("_parse_ratio treats N/A as unknown", vsmp._parse_ratio('N/A', 'dflt') == 'dflt')
check("_parse_ratio treats 0/1 as unknown", vsmp._parse_ratio('0/1', 'dflt') == 'dflt')
check("_parse_ratio survives 0/0 without raising",
      vsmp._parse_ratio('0/0', 'dflt') == 'dflt')

# ---------------------------------------------------------------------------
section("aspect-correct geometry (rec 26)")

# The film in this workspace: 888x480 storage, SAR 4920:4921, DAR 246:133.
g = vsmp.panel_geometry(888, 480, Fraction(4920, 4921))
check("1.85:1 film scales to 800x433 rather than stretching to 800x480",
      (g['width'], g['height']) == (800, 433), g)
check("letterboxed by 23px top", (g['pad_x'], g['pad_y']) == (0, 23), g)
check("image plus offset stays inside the panel",
      g['width'] + g['pad_x'] <= 800 and g['height'] + g['pad_y'] <= 480)
check("centred to within a pixel",
      abs(800 - g['width'] - 2 * g['pad_x']) <= 1
      and abs(480 - g['height'] - 2 * g['pad_y']) <= 1,
      "an odd amount of padding leaves the extra row at the bottom")
check("display aspect preserved at 1.8496",
      abs(g['display_aspect'] - 1.8496) < 0.001, g['display_aspect'])

check("16:9 letterboxes to 800x450",
      vsmp.panel_geometry(1920, 1080, Fraction(1))['height'] == 450)

# Anamorphic handling. This is the case ffmpeg's force_original_aspect_ratio
# gets wrong, because it looks only at storage dimensions and ignores SAR.
dvd43 = vsmp.panel_geometry(720, 480, Fraction(8, 9))      # NTSC DVD, 4:3
dvd169 = vsmp.panel_geometry(720, 480, Fraction(32, 27))   # NTSC DVD, 16:9
check("SAR 8:9 on 720x480 is corrected to 4:3 and pillarboxed",
      abs(dvd43['display_aspect'] - 4 / 3) < 0.001 and dvd43['pad_x'] > 0, dvd43)
check("4:3 anamorphic gives 640x480 with 80px side bars",
      (dvd43['width'], dvd43['height'], dvd43['pad_x']) == (640, 480, 80), dvd43)
check("SAR 32:27 on 720x480 is corrected to 16:9",
      abs(dvd169['display_aspect'] - 16 / 9) < 0.001, dvd169)
check("identical storage size, different geometry per SAR",
      (dvd43['width'], dvd43['height']) != (dvd169['width'], dvd169['height']),
      "exactly what force_original_aspect_ratio would have got wrong")

tall = vsmp.panel_geometry(480, 800, Fraction(1))
check("a portrait source is pillarboxed and stays in bounds",
      tall['height'] == 480 and tall['width'] <= 800 and tall['pad_x'] > 0, tall)

check("NEGATIVE CONTROL: the old resize((800,480)) really did distort",
      abs((800 / 480) - g['display_aspect']) > 0.15,
      "panel 1.667 vs true 1.850, an 11% vertical stretch")

# ---------------------------------------------------------------------------
section("run_tool: timeouts, exit status, no shell (rec 30)")

check("returns stdout", vsmp.run_tool(['echo', 'hello'], 5, 'echo').strip() == 'hello')

try:
    vsmp.run_tool(['false'], 5, 'deliberate failure')
    check("raises on a non-zero exit", False, "no exception raised")
except RuntimeError as e:
    check("raises on a non-zero exit", True, e)

t0 = time.monotonic()
try:
    vsmp.run_tool(['sleep', '30'], 1, 'deliberate timeout')
    check("raises on timeout", False, "no exception raised")
except subprocess.TimeoutExpired:
    check("raises on timeout", time.monotonic() - t0 < 5,
          "after {:.1f}s".format(time.monotonic() - t0))

check("NEGATIVE CONTROL: os.popen has no timeout to offer",
      'timeout' not in (os.popen.__doc__ or '').lower(),
      "os.popen(...).read() blocks indefinitely by design, which is the hang "
      "this replaced")

# A filename a shell would mangle or act on. This is recs 30 and 31 together.
hostile_dir = Path("a dir with spaces")
hostile_dir.mkdir(exist_ok=True)
hostile = hostile_dir / "it's a movie; rm -rf $HOME.mp4"
subprocess.run(['cp', str(SYNTH), str(hostile)], check=True)
info_hostile = vsmp.probe_video(hostile)
check("spaces, apostrophes, semicolons and $ in a filename all work",
      info_hostile['total_frames'] == 60, info_hostile['total_frames'])
check("HOME survived a filename containing 'rm -rf $HOME'",
      Path(os.environ['HOME']).exists())

# ---------------------------------------------------------------------------
section("probe_video (recs 22, 23)")

info_s = vsmp.probe_video(SYNTH)
check("fps read as an exact rational", info_s['fps'] == Fraction(25, 1), info_s['fps'])
check("total frames = 60", info_s['total_frames'] == 60, info_s['total_frames'])
check("recognised as constant frame rate", info_s['cfr'] is True)
check("absent SAR defaults to 1:1", info_s['sar'] == Fraction(1), info_s['sar'])
check("16:9 fixture letterboxes to 800x450",
      (info_s['geometry']['width'], info_s['geometry']['height']) == (800, 450))

counted = vsmp.probe_video(SYNTH, count_frames=True)
check("--count-frames agrees with the container", counted['total_frames'] == 60)
check("--count-frames records its provenance",
      counted['frame_count_source'] == 'counted', counted['frame_count_source'])

# No video stream: must be a clear error, not the IndexError that the old
# frame_count()'s int(s.split()[0]) produced on empty ffprobe output.
audio_only = Path('audio_only.m4a')
subprocess.run(['ffmpeg', '-y', '-nostdin', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'anullsrc', '-t', '1', str(audio_only)],
               check=True, stdin=subprocess.DEVNULL)
try:
    vsmp.probe_video(audio_only)
    check("a file with no video stream is rejected clearly", False, "no exception")
except RuntimeError as e:
    check("a file with no video stream is rejected clearly",
          'no video stream' in str(e), e)
except Exception as e:
    check("a file with no video stream is rejected clearly", False,
          "wrong exception type: {}".format(type(e).__name__))

if harness.needs_real_movie("real movie probe"):
    info_r = vsmp.probe_video(REAL)
    check("real movie fps is an exact rational", info_r['fps'].denominator != 1
          or info_r['fps'] > 0, info_r['fps'])
    check("real movie frame count came from the container",
          info_r['frame_count_source'] == 'container nb_frames',
          info_r['frame_count_source'])
    derived = round(info_r['duration_s'] * float(info_r['fps']))
    check("container count agrees with duration x fps",
          abs(info_r['total_frames'] - derived) <= vsmp.FRAME_COUNT_TOLERANCE,
          "container {} vs derived {}".format(info_r['total_frames'], derived))
    check("real movie is constant frame rate", info_r['cfr'] is True,
          "if this fails, frame/fps is not a valid mapping for it")
    check("real movie is long enough to be a meaningful test",
          info_r['total_frames'] > 10000, info_r['total_frames'])
else:
    info_r = None

# ---------------------------------------------------------------------------
section("frame accuracy against select=gte ground truth (rec 21)")

# Frames chosen around GOP boundaries (the fixture's GOP is 15), because that is
# where a seek that decoded from the wrong keyframe would show up.
for frame in (0, 1, 7, 14, 15, 16, 30, 59):
    mine, theirs = Path('m{}.png'.format(frame)), Path('t{}.png'.format(frame))
    vsmp.extract_frame(SYNTH, mine, frame, info_s)
    ground_truth_frame(SYNTH, frame, info_s, theirs)
    check("fixture frame {} byte-identical to select=gte".format(frame),
          sha256(mine) == sha256(theirs))

# Independent of ffmpeg's select filter: the fixture's gray level rises with
# frame number, so consecutive requests must return strictly rising levels.
levels = []
for frame in range(20):
    p = Path('o{}.png'.format(frame))
    vsmp.extract_frame(SYNTH, p, frame, info_s)
    levels.append(Image.open(p).getpixel((400, 240)))
check("consecutive frames come back strictly increasing in gray level",
      all(b > a for a, b in zip(levels, levels[1:])), levels)
check("20 consecutive frames are all distinct images", len(set(levels)) == 20)

# NEGATIVE CONTROL for the corrected formula. The original recommendation said
# to aim at (frame + 0.5) / fps; that lands on frame N+1.
def seek_to(ts, out):
    subprocess.run([
        'ffmpeg', '-y', '-nostdin', '-loglevel', 'error', '-accurate_seek',
        '-ss', vsmp.format_seconds(ts), '-i', str(SYNTH), '-map', '0:v:0',
        '-frames:v', '1', '-update', '1', '-f', 'image2', str(out)],
        check=True, stdin=subprocess.DEVNULL)
    return Image.open(out).getpixel((320, 180))


target = 30
lvl_plus = seek_to((Fraction(target) + Fraction(1, 2)) / info_s['fps'], 'plus.png')
lvl_minus = seek_to(vsmp.frame_timestamp(target, info_s['fps']), 'minus.png')
check("NEGATIVE CONTROL: the original '+0.5' formula picks a different frame",
      lvl_plus != lvl_minus,
      "+0.5 gave {}, -0.5 gave {}".format(lvl_plus, lvl_minus))
check("NEGATIVE CONTROL: '+0.5' overshoots to the later frame",
      lvl_plus > lvl_minus, "higher gray level means a later frame")

# Output shape. A mismatch here would make the driver paint the panel black.
probe = json.loads(subprocess.run(
    ['ffprobe', '-v', 'error', '-show_entries', 'stream=width,height,pix_fmt',
     '-of', 'json', 'm30.png'], capture_output=True, text=True,
    check=True).stdout)['streams'][0]
check("extracted PNG is exactly panel-sized",
      (probe['width'], probe['height']) == (800, 480), probe)
check("extracted PNG is grayscale, so no 'P' mode round trip (rec 27)",
      probe['pix_fmt'] == 'gray', probe['pix_fmt'])
check("output is PNG, not JPEG (rec 28)",
      Path('m30.png').read_bytes()[:8] == b'\x89PNG\r\n\x1a\n')
check("letterbox bars are white, not black",
      Image.open('m30.png').getpixel((400, 2)) >= 250,
      Image.open('m30.png').getpixel((400, 2)))

# Past the end of the stream: ffmpeg exits 0 having written nothing, so this has
# to be detected explicitly or it looks like a corrupt frame.
try:
    vsmp.extract_frame(SYNTH, Path('past.png'), 100000, info_s)
    check("seeking past the end raises EndOfMovie", False, "no exception")
except vsmp.EndOfMovie as e:
    check("seeking past the end raises EndOfMovie", True, str(e)[:70])
except Exception as e:
    check("seeking past the end raises EndOfMovie", False,
          "wrong type: {}".format(type(e).__name__))

if info_r is not None:
    for frame in (0, 1, 47, 1000, 30000):
        mine = Path('rm{}.png'.format(frame))
        theirs = Path('rt{}.png'.format(frame))
        vsmp.extract_frame(REAL, mine, frame, info_r)
        ground_truth_frame(REAL, frame, info_r, theirs)
        check("real movie frame {} byte-identical to select=gte".format(frame),
              sha256(mine) == sha256(theirs))
else:
    skip("real movie frame accuracy", "no real movie available")

# ---------------------------------------------------------------------------
section("extraction cost does not grow with position (rec 21)")

if info_r is not None:
    total = info_r['total_frames']
    probes = [100, total // 4, total // 2, int(total * 0.95)]
    new_times = []
    for frame in probes:
        t0 = time.monotonic()
        vsmp.extract_frame(REAL, Path('perf.png'), frame, info_r)
        new_times.append((frame, time.monotonic() - t0))
    print("     input seeking: " + ", ".join(
        "f{}={:.2f}s".format(f, t) for f, t in new_times))
    fastest = min(t for _, t in new_times)
    slowest = max(t for _, t in new_times)
    check("extraction time is flat across the whole film",
          slowest < fastest + 1.0,
          "fastest {:.2f}s, slowest {:.2f}s".format(fastest, slowest))
    check("a frame 95% through is not slower than one near the start",
          new_times[-1][1] < new_times[0][1] * 5 + 1.0,
          "f{} {:.2f}s vs f{} {:.2f}s".format(
              new_times[0][0], new_times[0][1],
              new_times[-1][0], new_times[-1][1]))

    old_times = []
    for frame in probes[:3]:
        t0 = time.monotonic()
        ground_truth_frame(REAL, frame, info_r, Path('perf_old.png'))
        old_times.append((frame, time.monotonic() - t0))
    print("     select=gte:    " + ", ".join(
        "f{}={:.2f}s".format(f, t) for f, t in old_times))
    check("NEGATIVE CONTROL: select=gte degrades badly with position",
          old_times[-1][1] > old_times[0][1] * 5,
          "f{} {:.2f}s -> f{} {:.2f}s".format(
              old_times[0][0], old_times[0][1],
              old_times[-1][0], old_times[-1][1]))
    check("NEGATIVE CONTROL: input seeking beats select=gte mid-film",
          new_times[2][1] < old_times[2][1] / 3,
          "{:.2f}s vs {:.2f}s".format(new_times[2][1], old_times[2][1]))
else:
    skip("extraction cost comparison",
         "needs a feature-length movie; the 60-frame fixture cannot show this")

harness.finish()
