"""The image queue: the player's queue-drain contract (FEAT-001).

Covers IMAGE-QUEUE-DESIGN.md sections 3-5. The player checks the queue at the
top of every 150s tick, before it would extract a movie frame. A queued image
occupies exactly one tick, drains FIFO (lexical-name) order, and does NOT
advance the movie frame counter -- the film resumes exactly where it paused.

Tested against the EPD stub, like test_state_loop.py: no Raspberry Pi and no
panel, so the queue-drain logic is fully exercisable on a laptop. Each queued
image is a solid-gray 800x480 PNG at a distinct level, so reading one pixel of
what the panel was shown says which image came back -- the same self-identifying
trick make_fixtures.py uses for movie frames.
"""
import json
import os
from pathlib import Path

import harness
from harness import check, section, FakeEpd

import vsmp
from PIL import Image

SYNTH = harness.ensure_fixture()
info = vsmp.probe_video(SYNTH)
MOVIE = SYNTH.name

# Keep the suite quick; the drain logic is interval-independent.
vsmp.FRAME_INTERVAL_S = 0.05


class RecordingEpd(FakeEpd):
    """A FakeEpd that also records the center-pixel gray level of every image.

    getbuffer() is where display_on_e_ink() hands the finished PIL image over,
    so capturing the pixel here records exactly what the panel was shown --
    whether it came from a movie frame or a queued upload. The center pixel
    (400, 240) is read so the white letterbox bars never confuse the reading.
    """

    def __init__(self):
        super().__init__()
        self.levels = []

    def getbuffer(self, image):
        self.levels.append(image.convert('L').getpixel((400, 240)))
        return super().getbuffer(image)


def make_queue_image(path, level):
    """A solid-gray 800x480 PNG, already panel-sized, identifiable by its level."""
    Image.new('L', (vsmp.PANEL_W, vsmp.PANEL_H), level).save(path)


def seed_state(frame=0, total=None):
    st = vsmp.new_state(MOVIE, info)
    if total is not None:
        st['total_frames'] = total
    st['frame'] = frame
    return st


# ---------------------------------------------------------------------------
section("new_state carries images_shown and summarize_state surfaces it")

fresh = vsmp.new_state(MOVIE, info)
check("new_state seeds images_shown at 0", fresh.get('images_shown') == 0,
      fresh.get('images_shown'))
check("summarize_state surfaces images_shown",
      vsmp.summarize_state(fresh)['images_shown'] == 0)

# An older state file that predates the counter still loads and reads 0.
old = vsmp.new_state(MOVIE, info)
del old['images_shown']
check("summarize_state defaults images_shown to 0 for an older state",
      vsmp.summarize_state(old)['images_shown'] == 0)

# ---------------------------------------------------------------------------
section("queue drains one image per tick, FIFO (lexical-name) order")

qdir = Path('q_fifo')
# Names are claimed lexically smallest first; the levels below are chosen so
# arrival order and level order agree, making the FIFO check readable.
make_queue_image(vsmp.queue_paths(qdir)[0] / '001.png', 60)
make_queue_image(vsmp.queue_paths(qdir)[0] / '002.png', 120)
make_queue_image(vsmp.queue_paths(qdir)[0] / '003.png', 200)

epd = RecordingEpd()
state = seed_state(frame=10, total=60)
vsmp.play_movie(epd, SYNTH, 'fifo.json', state, info, 1.0, queue_dir=str(qdir))

# The three images show first, in name order, then the movie plays frames 10..59.
check("first three displays are the queued images, FIFO by name",
      epd.levels[:3] == [60, 120, 200], epd.levels[:3])
check("the movie then played every remaining frame (10..59)",
      len(epd.levels) == 3 + 50, "{} total displays".format(len(epd.levels)))
check("the queue drained -- no published PNGs remain",
      vsmp.list_queue(qdir) == [], vsmp.list_queue(qdir))

# ---------------------------------------------------------------------------
section("an image tick does NOT advance the movie frame; the movie resumes")

qdir2 = Path('q_resume')
make_queue_image(vsmp.queue_paths(qdir2)[0] / 'a.png', 77)
make_queue_image(vsmp.queue_paths(qdir2)[0] / 'b.png', 155)

epd2 = RecordingEpd()
state2 = seed_state(frame=50, total=60)
vsmp.play_movie(epd2, SYNTH, 'resume.json', state2, info, 1.0, queue_dir=str(qdir2))

check("two images shown, then exactly the 10 remaining movie frames",
      len(epd2.levels) == 2 + 10, "{} displays".format(len(epd2.levels)))
check("the movie ended at the last frame (queue ticks did not advance it)",
      state2['frame'] == 60, state2['frame'])
check("images_shown counted exactly the two uploads",
      state2['images_shown'] == 2, state2['images_shown'])
# Frame 50's own gray level is 10 + 4*50 = 210; the first movie display after
# the two image ticks must be that frame, proving the film resumed where it
# paused rather than skipping two frames.
check("the movie resumed at the exact frame it paused on",
      abs(epd2.levels[2] - (10 + 4 * 50)) <= 6, epd2.levels[2])

# ---------------------------------------------------------------------------
section("the shown file is deleted by default and .showing/ ends empty")

qdir3 = Path('q_dispose')
make_queue_image(vsmp.queue_paths(qdir3)[0] / 'only.png', 90)

# No archive dir configured, so disposal is a delete.
assert vsmp.ARCHIVE_DIR is None, "this test assumes no VSMP_ARCHIVE_DIR"
epd3 = RecordingEpd()
state3 = seed_state(frame=59, total=60)
vsmp.play_movie(epd3, SYNTH, 'dispose.json', state3, info, 1.0, queue_dir=str(qdir3))

_, _, showing3 = vsmp.queue_paths(qdir3)
check("the published image is gone after display",
      not (qdir3 / 'only.png').exists())
check(".showing/ is empty after the tick (file disposed, not left claimed)",
      list(showing3.iterdir()) == [], list(showing3.iterdir()))
check("images_shown incremented for the single upload",
      state3['images_shown'] == 1, state3['images_shown'])

# ---------------------------------------------------------------------------
section("sweep_showing removes a pre-seeded leftover .showing/ file at startup")

qdir4 = Path('q_sweep')
_, _, showing4 = vsmp.queue_paths(qdir4)
leftover = showing4 / 'already_claimed.png'
make_queue_image(leftover, 130)
check("the leftover claimed file exists before the run", leftover.exists())

epd4 = RecordingEpd()
state4 = seed_state(frame=59, total=60)
vsmp.play_movie(epd4, SYNTH, 'sweep.json', state4, info, 1.0, queue_dir=str(qdir4))

check("sweep_showing removed the leftover claimed file at startup",
      not leftover.exists())
check("the leftover claimed file was NOT re-shown (only movie frames displayed)",
      130 not in epd4.levels, epd4.levels)

# ---------------------------------------------------------------------------
section("no mid-tick swap: an image dropped mid-tick waits for the next tick")

# render_one_frame is monkeypatched to drop an image into the queue while a
# movie frame is being rendered. The queue is only ever read at the TOP of a
# tick, so the image must not appear until the following tick -- never swapped
# in on top of the frame currently being shown.
qdir5 = Path('q_midtick')
vsmp.queue_paths(qdir5)
real_render = vsmp.render_one_frame
dropped = {'done': False}


def render_then_drop(movie, frame, epd_, inf, frames_dir, contrast):
    result = real_render(movie, frame, epd_, inf, frames_dir, contrast)
    if not dropped['done']:
        make_queue_image(qdir5 / 'late.png', 180)
        dropped['done'] = True
    return result


vsmp.render_one_frame = render_then_drop
epd5 = RecordingEpd()
state5 = seed_state(frame=57, total=60)
vsmp.play_movie(epd5, SYNTH, 'midtick.json', state5, info, 1.0, queue_dir=str(qdir5))
vsmp.render_one_frame = real_render

check("the mid-tick upload was dropped during the run", dropped['done'])
# Frame 57 is shown first (no swap), then the image appears on a LATER display,
# never as the first one.
check("the first display is the movie frame, not the late upload",
      epd5.levels[0] != 180, epd5.levels[0])
check("the late upload WAS eventually shown, on a following tick",
      180 in epd5.levels, epd5.levels)
check("the upload displayed exactly once", epd5.levels.count(180) == 1,
      epd5.levels)

# ---------------------------------------------------------------------------
section("NEGATIVE CONTROL: an empty queue plays the movie normally")

qdir6 = Path('q_empty')
vsmp.queue_paths(qdir6)        # created but left empty
epd6 = RecordingEpd()
state6 = seed_state(frame=55, total=60)
vsmp.play_movie(epd6, SYNTH, 'empty.json', state6, info, 1.0, queue_dir=str(qdir6))

check("NEGATIVE CONTROL: the movie advances normally with an empty queue",
      state6['frame'] == 60, state6['frame'])
check("NEGATIVE CONTROL: images_shown stays 0 when nothing is queued",
      state6['images_shown'] == 0, state6['images_shown'])
check("NEGATIVE CONTROL: exactly the 5 remaining movie frames displayed",
      len(epd6.levels) == 5, "{} displays".format(len(epd6.levels)))

# ---------------------------------------------------------------------------
section("no repo pollution")

REPO = harness.REPO_ROOT
check("no log.txt written into the repo", not (REPO / 'log.txt').exists())
check("no state.json written into the repo", not (REPO / 'state.json').exists())
check("no queue directory written into the repo",
      not (REPO / 'queue').exists())

harness.finish()
