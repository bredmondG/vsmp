"""The web side of the image queue: upload -> real ffmpeg convert -> publish.

Covers IMAGE-QUEUE-DESIGN.md sections 3-6 as seen from the webapp (FEAT-002).
A visitor POSTs an image to /upload; the server converts it with the SAME
ffmpeg filter the player uses (vsmp.convert_image_to_panel_png), publishes it
atomically into the queue directory the player drains, and tells the visitor
where in line it landed.

Conversion here is REAL ffmpeg, never stubbed -- that is the whole point of the
test: a phone JPEG/PNG/HEIC must come out the far side as an 800x480 grayscale
PNG that passes the panel's size guard. The EPD is still the FakeEpd stub,
because we are not testing the panel, only that the produced image is one the
panel would accept.

VSMP_QUEUE is pointed at a temp dir under this module's work dir BEFORE webapp
is imported, so the app and the player agree on one directory and nothing is
written into the repo.
"""
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import harness
from harness import check, section, FakeEpd

# Point the queue at a scratch dir under the (already chdir'd) work dir, BEFORE
# importing webapp so its module-level QUEUE_PATH picks this up.
QUEUE = (harness.WORK_DIR / 'queue').resolve()
os.environ['VSMP_QUEUE'] = str(QUEUE)

import vsmp
import webapp
from PIL import Image

webapp.app.config['TESTING'] = True


def client():
    return webapp.app.test_client()


def make_png(path, size, color):
    """A plain solid-color image on disk, for feeding to an upload."""
    Image.new('RGB', size, color).save(path)


def upload_bytes(path):
    """Read a file into an in-memory (bytes, name) pair for the test client."""
    p = Path(path)
    return io.BytesIO(p.read_bytes()), p.name


def published_pngs():
    """Finished images visible to the player: <queue>/*.png only."""
    return sorted(QUEUE.glob('*.png'))


def panel_guard_accepts(png_path):
    """True if display_on_e_ink would accept this image against a FakeEpd."""
    image = Image.open(png_path)
    image.load()
    try:
        vsmp.display_on_e_ink(FakeEpd(), image)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
section("GET /upload renders the landing page")

c = client()
r = c.get('/upload')
body = r.get_data(as_text=True)
check("GET /upload returns 200", r.status_code == 200, r.status_code)
check("explainer 1 present: what the VSMP is",
      'very slow movie player' in body.lower() and '150' in body,
      "found the slow-movie-player explainer")
check("explainer 2 present: how the queue works",
      'queue' in body.lower() and 'grayscale' in body.lower(),
      "found the queue explainer")
check("the form targets /upload as multipart",
      'action="/upload"' in body and 'multipart/form-data' in body)
check("the file input accepts images and has no capture= attribute",
      "accept=\"image/*\"" in body and 'capture' not in body,
      "accept=image/* present, capture absent (existing photos choosable)")
check("empty queue shows length 0 and the 'movie is playing' wording",
      'Queue is empty' in body and 'movie is playing' in body,
      "zero count and message agree")

# NEGATIVE CONTROL: the empty-queue listing really is empty, not just unshown.
check("NEGATIVE CONTROL: no published files exist for an empty queue",
      published_pngs() == [], published_pngs())


# ---------------------------------------------------------------------------
section("POST /upload: a real JPEG and a real PNG each publish one 800x480 PNG")

src_jpg = harness.WORK_DIR / 'photo.jpg'
Image.new('RGB', (1234, 987), (200, 30, 30)).save(src_jpg, 'JPEG')
src_png = harness.WORK_DIR / 'picture.png'
make_png(src_png, (640, 640), (20, 180, 90))

for label, src in (('JPEG', src_jpg), ('PNG', src_png)):
    before = len(published_pngs())
    data, fname = upload_bytes(src)
    resp = client().post('/upload', data={'image': (data, fname)},
                          content_type='multipart/form-data')
    check("{} upload returns 200".format(label), resp.status_code == 200,
          resp.status_code)
    rbody = resp.get_data(as_text=True)
    check("{} upload confirms a landed position".format(label),
          'in line' in rbody, "landed-position confirmation shown")
    after = published_pngs()
    check("{} upload published exactly one new file".format(label),
          len(after) == before + 1, "{} -> {}".format(before, len(after)))
    newest = after[-1]
    sz = Image.open(newest).size
    check("{} result is exactly 800x480".format(label), sz == (800, 480), sz)
    check("{} result passes the panel size guard".format(label),
          panel_guard_accepts(newest), newest.name)

check("generated names sort in arrival order (FIFO contract)",
      [p.name for p in published_pngs()] == sorted(p.name for p in published_pngs()),
      "lexical sort == arrival order")

# Clean the queue for the isolated checks that follow.
for p in published_pngs():
    p.unlink()


# ---------------------------------------------------------------------------
section("HEIC (phone) upload -- decoded by pillow-heif, not ffmpeg")

# The fixture is a pillow-heif-encoded HEIF, not a sips-built one. The old sips
# fixture was single-tile and decoded fine through the OLD ffmpeg -map 0:v:0
# path, so the suite never exercised the real HEIF decode and missed the
# washed-out/black bug on tiled iPhone HEICs. This fixture forces the pillow-heif
# branch. HONEST-COVERAGE NOTE: pillow-heif's encoder emits a SINGLE-TILE HEIF,
# so this proves the decode branch is wired and yields a full-range panel PNG; it
# does NOT reproduce the iPhone tile grid. The genuinely-tiled regression is only
# reproduced by running the real photos through convert_image_to_panel_png by
# hand (see tests/README.md and .agents/tasks/verification.md). Do not read this
# as tiled coverage.
import make_fixtures

if not vsmp._HEIF_OK:
    harness.skip("HEIC upload decoded by pillow-heif to a full-range 800x480 PNG",
                 "pillow-heif not importable; install pillow-heif to run this "
                 "(it is a runtime dependency of the web service)")
else:
    heic = harness.WORK_DIR / 'phone.heic'
    built = make_fixtures.make_gradient_heif(heic, size=(900, 1200))  # portrait
    if built is None or not heic.exists():
        harness.skip("HEIC upload decoded by pillow-heif to a full-range 800x480 PNG",
                     "pillow-heif could not encode a HEIF fixture")
    else:
        # Spy on the HEIF detector so we can assert the pillow-heif branch is the
        # one that ran, not the ffmpeg -map path -- a single-tile fixture could
        # otherwise pass through ffmpeg and hide a broken branch.
        orig_is_heif = vsmp._is_heif
        heif_branch_taken = []

        def spy_is_heif(path):
            result = orig_is_heif(path)
            heif_branch_taken.append((str(path), result))
            return result

        vsmp._is_heif = spy_is_heif
        try:
            before = len(published_pngs())
            data, fname = upload_bytes(heic)
            resp = client().post('/upload', data={'image': (data, fname)},
                                 content_type='multipart/form-data')
            check("HEIC upload returns 200", resp.status_code == 200, resp.status_code)
            after = published_pngs()
            check("HEIC upload published exactly one new file",
                  len(after) == before + 1, "{} -> {}".format(before, len(after)))

            # The HEIF decode branch was actually taken (pillow-heif), not ffmpeg.
            check("pillow-heif is available (_HEIF_OK)", vsmp._HEIF_OK is True,
                  vsmp._HEIF_OK)
            check("the pillow-heif HEIF branch ran (detector saw HEIF=True)",
                  any(flag for _, flag in heif_branch_taken), heif_branch_taken)

            if after:
                published = after[-1]
                img = Image.open(published)
                img.load()
                check("HEIC result is exactly 800x480", img.size == (800, 480),
                      img.size)

                # The webapp publishes into <queue>/<name> after converting into
                # a '.tmp' temp file, and ffmpeg's image2 muxer stores a '.tmp'
                # as JPEG(RGB) -- the same as the JPEG/PNG upload paths. The
                # grayscale guarantee is in the ffmpeg `format=gray` filter, so
                # the panel sees a neutral image (R==G==B) regardless of the
                # container's colour type. Assert grayscale CONTENT, not mode.
                rgb = img.convert('RGB')
                pixels = list(rgb.getdata())
                check("HEIC result is grayscale content (every pixel R==G==B)",
                      all(r == g == b for r, g, b in pixels),
                      "neutral regardless of JPEG RGB container")
                check("HEIC result passes the panel size guard",
                      panel_guard_accepts(published), published.name)

                # Full tonal range: the whole point. A collapsed (all-black or
                # all-white) decode -- the washed-out bug -- would fail here.
                # Measured on the grayscale-equivalent of the published image.
                gray = img.convert('L')
                hist = gray.histogram()
                present = [i for i, count in enumerate(hist) if count > 0]
                lo, hi, distinct = min(present), max(present), len(present)
                check("HEIC result has a real tonal range (min<50, max>200, "
                      ">50 distinct grays)",
                      lo < 50 and hi > 200 and distinct > 50,
                      "min={} max={} distinct={}".format(lo, hi, distinct))
        finally:
            vsmp._is_heif = orig_is_heif
        for p in published_pngs():
            p.unlink()


# ---------------------------------------------------------------------------
section("a non-image upload is rejected with nothing written")

junk = harness.WORK_DIR / 'notanimage.txt'
junk.write_bytes(b'this is plainly not an image, ffmpeg will refuse it')
before = published_pngs()
data, fname = upload_bytes(junk)
resp = client().post('/upload', data={'image': (data, fname)},
                     content_type='multipart/form-data')
check("a non-image upload is rejected (4xx)",
      400 <= resp.status_code < 500, resp.status_code)
check("nothing was published for a non-image upload",
      published_pngs() == before, published_pngs())
incoming = QUEUE / '.incoming'
leftover = list(incoming.glob('*')) if incoming.exists() else []
check("no temp debris left in .incoming/ after a rejected upload",
      leftover == [], leftover)

# NEGATIVE CONTROL: a missing file field is a clean 400, not a crash.
resp = client().post('/upload', data={}, content_type='multipart/form-data')
check("NEGATIVE CONTROL: POST with no file field returns 400",
      resp.status_code == 400, resp.status_code)


# ---------------------------------------------------------------------------
section("the empty-queue listing reports 0 and 'movie is playing'")

summary = webapp.list_queue_summary(QUEUE)
check("empty queue length is 0", summary['length'] == 0, summary['length'])
check("empty queue estimated wait is 0s",
      summary['wait_seconds'] == 0, summary['wait_seconds'])
body = client().get('/upload').get_data(as_text=True)
check("the page shows the 'movie is playing' wording for an empty queue",
      'movie is playing' in body)

# /queue.json shares the one wait formula with the page.
qj = json.loads(client().get('/queue.json').get_data(as_text=True))
check("/queue.json reports length 0 for an empty queue", qj['length'] == 0, qj)
check("/queue.json wait = length * FRAME_INTERVAL_S (shared formula)",
      qj['wait_seconds'] == 0, qj['wait_seconds'])


# ---------------------------------------------------------------------------
section("concurrent uploads: unique names, no partial file ever visible")

# Pre-build several distinct source images so the convert cost is off the clock.
sources = []
for i in range(8):
    s = harness.WORK_DIR / 'conc_{}.png'.format(i)
    make_png(s, (500 + i * 10, 400), (i * 20 % 256, 60, 180))
    sources.append(s)

errors = []
scan_stop = threading.Event()
partial_seen = []


def scanner():
    """While uploads are in flight, keep scanning queue/ for any file that is
    not a finished 800x480 PNG. All mid-flight work must live under .incoming/,
    so a scan of queue/*.png must NEVER see a partial or temp file."""
    while not scan_stop.is_set():
        for p in QUEUE.glob('*'):
            if p.is_dir():
                continue
            if p.suffix != '.png':
                partial_seen.append(('bad-suffix', p.name))
                continue
            try:
                if Image.open(p).size != (800, 480):
                    partial_seen.append(('wrong-size', p.name))
            except Exception as e:
                partial_seen.append(('unopenable', p.name, str(e)))


def do_upload(src):
    try:
        data, fname = upload_bytes(src)
        resp = client().post('/upload', data={'image': (data, fname)},
                             content_type='multipart/form-data')
        if resp.status_code != 200:
            errors.append((src.name, resp.status_code))
    except Exception as e:
        errors.append((src.name, repr(e)))


scan_thread = threading.Thread(target=scanner)
scan_thread.start()
threads = [threading.Thread(target=do_upload, args=(s,)) for s in sources]
for t in threads:
    t.start()
for t in threads:
    t.join()
scan_stop.set()
scan_thread.join()

check("every concurrent upload succeeded", errors == [], errors)
names = [p.name for p in published_pngs()]
check("every concurrent upload landed", len(names) == len(sources),
      "{} of {}".format(len(names), len(sources)))
check("every published name is unique", len(set(names)) == len(names), names)
check("no partial or temp file was ever visible in queue/ during publishing",
      partial_seen == [], partial_seen)

for p in published_pngs():
    p.unlink()


# ---------------------------------------------------------------------------
section("webapp imports no e-paper driver")

check("importing webapp did not pull in the epd driver",
      'epd.epd7in5_V2_old' not in sys.modules,
      "webapp stays a driver-free pure reader+writer")


# ---------------------------------------------------------------------------
section("no repo pollution")

REPO = harness.REPO_ROOT
check("no queue dir written into the repo", not (REPO / 'queue').exists())
dirty = subprocess.run(['git', 'status', '--short'], cwd=str(REPO),
                       capture_output=True, text=True).stdout.splitlines()
stray = [ln for ln in dirty if 'tests/work' in ln or 'tests/fixtures' in ln]
check("test artifacts are git-ignored", not stray, stray)

harness.finish()
