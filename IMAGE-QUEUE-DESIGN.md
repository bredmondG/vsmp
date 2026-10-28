# Image queue feature — design & implementation plan

A plan for letting people upload their own images to the very slow movie player
from a web page (including a phone on the same network). Uploaded images are
shown on the e-paper panel one per 150s tick; when the queue is empty the movie
plays as it does today.

This document is the brief for the work. It is written to be picked up cold at a
later date, so it records the behaviour, the design decisions, the reasons
behind them, and a concrete task list. No code has been written yet.

---

## 1. Goal

- Add a web page where a user can upload an image from a phone or laptop.
- The page a visitor lands on briefly explains what the VSMP is and how the
  upload/queue works, so a first-time user understands it before uploading.
- The page shows the **current queue length** (and an estimated wait derived
  from it), so a user can see how many images are ahead and roughly when theirs
  will appear.
- Uploaded images are queued and displayed on the panel, one per tick.
- Multiple people may upload at any time, concurrently.
- When no images are queued, the player shows the movie as it does now.

### Agreed behaviour (the exact spec)

- The player checks the queue at the **top of each 150s tick**, before it would
  extract the next movie frame.
- If the queue is **non-empty**: display the **oldest** queued image (FIFO),
  remove it from the queue, and **do not advance the movie frame**. The movie
  resumes from exactly where it paused.
- If the queue is **empty**: extract and display the next movie frame, exactly
  as today.
- An uploaded image occupies **exactly one tick** (150s). It is **never cut
  short**: the panel only ever changes at the normal tick boundary, so the
  image currently on screen is not interrupted by a new upload.
- Images uploaded within a single tick **stack up** and drain **one per tick**
  in arrival order.
- Empty-queue fallback is the **movie** (resume where it left off). A default
  image is only relevant to a cold start where no movie is configured; that is
  a minor nicety, not core to this feature.

---

## 2. Why this is the shape it is (the existing architecture)

Two invariants in the current codebase drive every decision below. They are
stated explicitly in the source docstrings and must be preserved:

1. **Exactly one process may touch the e-paper panel.** The driver
   (`epd/epdconfig.py`) claims the GPIO pins the instant it is imported, so a
   second importer dies with `GPIO busy`. The player is the only process that
   imports the driver; `vsmp.py status` and `webapp.py` deliberately never do.

2. **The player is the only writer of player state; the webapp is a pure
   reader.** `webapp.py`'s docstring is explicit: it "never imports the e-paper
   driver and never writes anything." It reads `state.json` (written atomically
   by the player) and renders it.

This feature requires the webapp to *influence what the panel shows*, which
touches invariant 2. The design keeps the invariants intact by using a
**file-drop queue**:

- The webapp does **not** draw to the panel. It writes finished,
  panel-ready PNGs into a `queue/` directory.
- The **player** is still the only process that imports the driver and draws.
  It reads the queue directory, claims a file, and displays it.

So the webapp gains exactly one new capability — write access to one directory —
and nothing else about the two-process contract changes. There is no shared
lock, no database, and no shared mutable state beyond the directory.

---

## 3. Concurrency contract (multiple uploaders + one player)

The correctness of concurrent uploads and FIFO ordering rests entirely on how
files enter and leave the queue directory.

### Ordering: sortable unique filenames

Each queued file is named with a monotonic timestamp prefix, a per-process
counter, and a short random suffix, e.g.:

```
1730130000123_000007_a1b2c3.png
└ ms since epoch┘ └count┘ └rand┘
```

- Lexical sort of the directory == arrival order (FIFO). The player always
  claims the lexically smallest name.
- The random suffix means two uploaders writing in the same millisecond cannot
  collide on a filename.

### Atomic publish (write-then-rename)

The webapp never writes directly into `queue/`. It:

1. Converts the upload into a finished 800x480 grayscale PNG in a temp location
   on the **same filesystem** (e.g. `queue/.incoming/<name>.tmp`).
2. `os.rename`s it into `queue/<name>.png`.

`os.rename` within one filesystem is atomic, so the player only ever sees a
complete file. It can never catch a half-converted upload. This is the same
write-then-swap trick `save_state()` already uses for `state.json`.

### Atomic claim (rename-out)

The player takes ownership of the oldest file by `os.rename`-ing it out of
`queue/` into a work path (e.g. `queue/.showing/<name>.png`) before displaying.
After a successful display it disposes of the work file (see §3a). Consequences:

- A player crash mid-display loses at most that one image; the movie position
  (`state.json`) is untouched. On the next start, any leftover file under
  `.showing/` is cleaned up (it was already claimed, so it is not re-shown) —
  mirroring how `clean_frame_dir()` sweeps stale movie frames at startup today.
- Because claim is a rename, even if a future change introduced a second
  consumer, exactly one would win the file. Today there is only one player, so
  this is belt-and-braces, but it keeps the contract clean.

### 3a. Disposal after display — cleanup vs. keep

A displayed image is disposed of after its tick, **not** left to accumulate.
The existing codebase is deliberate about never letting files pile up on the SD
card over a months-long run — `discard_bad_frame()` and `clean_frame_dir()`
exist for exactly that reason — and an unbounded pile of user uploads would be
the same failure.

**Decision (default): delete after display, with an optional capped archive.**

- **Default**: once the image has been shown, delete the work file. Nothing
  accumulates; the only disk a queued image ever occupies is the one slot
  between upload and display, plus the one being shown.
- **Optional archive** (off by default, enabled by a config/env knob such as
  `VSMP_ARCHIVE_DIR` + `VSMP_ARCHIVE_MAX`): instead of deleting, move the shown
  file into an archive directory that is **capped** — keep the most recent N
  (e.g. 50) and prune the oldest beyond that, so the archive can never grow
  without bound. This gives a "what did people send" record without the SD-card
  risk.

This keeps the safe default (clean) while leaving a clearly-bounded way to
retain images if wanted. The cap is mandatory whenever the archive is on;
there is no unbounded-retention option by design.

### No locks

Multiple concurrent uploaders are safe because each writes a uniquely named
temp file and publishes atomically. The player and the uploaders never contend
for the same name. No lock files, no database.

---

## 4. Image conversion (handles phone uploads)

Phone photos are large and often HEIC/HEIF. For JPEG/PNG/WebP the decode is done
by **ffmpeg**, which the player and tests already depend on.

HEIC/HEIF is the exception and is decoded by **pillow-heif** (libheif), not
ffmpeg. The earlier assumption that "ffmpeg reads HEIC uniformly" was wrong: a
real iPhone HEIC stores its main image as a grid of HEVC tiles plus auxiliary
planes, and the ffmpeg build here does not assemble that grid — `-map 0:v:0`
grabbed a single tile or an aux plane, so those uploads rendered washed-out/gray
or black on the panel. pillow-heif/libheif assembles the grid correctly, so the
HEIF branch decodes the upload to a full-resolution PNG first and then hands that
PNG to the SAME ffmpeg scale/letterbox/grayscale pass (below). pillow-heif is a
runtime dependency of the web service; its wheel bundles libheif (no system
libheif package needed on the dev laptop or the Raspberry Pi).

HEIF is detected by actually attempting a libheif decode (content-based), never
by trusting the uploaded extension.

The conversion applies the **same scale / letterbox / grayscale filter** that
`extract_frame()` already uses in `vsmp.py`, producing an 800x480 (`PANEL_W` x
`PANEL_H`) grayscale PNG. This guarantees the uploaded image passes the size
guard in `display_on_e_ink()`, which refuses anything that is not exactly the
panel size (it would otherwise paint the panel solid black — see that
function's docstring).

The existing filter, for reference (from `extract_frame`):

```
scale={w}:{h},pad={PANEL_W}:{PANEL_H}:{px}:{py}:color=white,format=gray
```

For an arbitrary uploaded image we do not know a SAR, so geometry is computed
from the image's own dimensions with square pixels (SAR 1:1), fitting inside
800x480 and letterboxing with white. The geometry maths in `panel_geometry()`
can be reused directly by passing `sar=Fraction(1)`.

Validation is "did the decoder (pillow-heif for HEIF, ffmpeg otherwise) accept
it and produce a non-empty PNG" rather than trusting the uploaded extension or
MIME type. Any decode failure — pillow-heif raising, or ffmpeg writing nothing —
rejects the upload with a clear error and writes nothing to `queue/`.

---

## 5. Changes by file

### `vsmp.py` (the player)

- **Queue config**: a `--queue-dir` CLI option (default e.g. `queue`), plumbed
  like `--state`. An env/default constant `QUEUE_DIR`.
- **`prepare_uploaded_image()`** (or similar): given a path to an already
  panel-ready PNG in the queue, load it for display. Conversion itself happens
  in the webapp; the player just loads + displays. (If we decide the player
  should also be able to convert — e.g. for a default image — factor the ffmpeg
  filter used by `extract_frame` into a shared helper so both call sites agree.)
- **Queue claim/display helper**: list `queue/*.png`, pick the lexically
  smallest, rename it into the work dir, return its path (or `None` if empty).
- **Play-loop change** in `play_movie()`: at the **top** of the `while` loop,
  before `render_one_frame`, check the queue.
  - If an image was claimed: display it via the existing
    `display_on_e_ink()` path, delete the work file, and `continue` the loop
    **without** incrementing `state['frame']`. Still honour the tick schedule
    (`next_deadline`), the watchdog ping, and a structured log line noting an
    image tick.
  - If empty: the existing movie-frame path runs unchanged.
- **Timing/watchdog**: an image tick is *cheaper* than a movie frame (no ffmpeg
  extract — conversion already happened in the webapp; just load PNG + push to
  panel), so it stays comfortably within the per-tick budget and the watchdog
  interval. No special handling needed, but the image-tick path must still call
  `watchdog_ping()` and respect `sleep_until(next_deadline)` exactly like the
  movie path, so the cadence and watchdog honesty are preserved.
- **State/logging**: optionally record a counter like `images_shown` in
  `state.json` and surface it in `summarize_state()` so the status page/CLI can
  show how many uploads have been displayed. The movie frame fields are
  untouched on an image tick.

### `webapp.py` (gains write access to the queue dir only)

- **`MAX_CONTENT_LENGTH`**: cap request size (e.g. 16 MB) so a large phone photo
  returns a clean 413 instead of exhausting RAM on the Pi.
- **`GET /upload`**: render the upload form plus the current queue listing.
- **`POST /upload`**: validate presence of a file, size-cap (also enforced by
  `MAX_CONTENT_LENGTH`), run ffmpeg conversion to an 800x480 grayscale PNG in a
  temp file, then atomically rename into `queue/`. On ffmpeg failure, reject
  with a readable message and write nothing.
- **Filename safety**: never trust the uploaded name
  (`werkzeug.utils.secure_filename` for any display use); the stored name is the
  generated sortable name, not the user's.
- **Queue listing helper**: read `queue/*.png`, sorted, returning both the
  ordered list and its length, for display on the upload page and
  (optionally) a `GET /queue.json`. The length is a live snapshot: the player
  may claim the oldest file between the read and the page render, so the count
  is approximate by nature — it only ever reads the directory, never locks it,
  which keeps the webapp's no-shared-lock contract intact. Count only published
  files in `queue/` itself; files mid-conversion live under `queue/.incoming/`
  and files being displayed live under `queue/.showing/`, so neither inflates
  the count. The estimated wait is derived here (`length x 150s`) so the CLI,
  JSON, and page agree on one formula.
- **`VSMP_QUEUE` env var**: the queue directory path, mirroring how `VSMP_STATE`
  is configured. Must point at the **same** directory the player reads.
- Still **never imports the e-paper driver**. The only new privilege is writing
  to the queue directory.

### `templates/upload.html` (new)

This is the page a visitor **lands on**, so it must explain itself before it
asks for anything. It opens with two short, plain-language explainers, then the
upload form, then the live queue.

**Explainer 1 — what the VSMP is** (one short paragraph): this is a *very slow
movie player*. A movie is shown on an e-paper panel one frame at a time, at 24
frames per hour — one new frame every 150 seconds. It is meant to be glanced at
over days, not watched.

**Explainer 2 — what happens to your image** (one short paragraph + the key
points): your uploaded picture joins a queue. When its turn comes it is shown on
the panel for a single 150-second slot, then the player moves on. It does **not**
interrupt whatever is currently on screen — the panel only changes at the next
slot. If several people upload, the images are shown one per slot in the order
they arrived. When the queue is empty, the movie plays on and resumes exactly
where it left off. Keep expectations honest: the image is rendered in grayscale,
scaled and letterboxed to fit the panel.

Copy should be short and scannable (a sentence or two each, not a wall of text),
since it is read on a phone. Keep it in the template (not hardcoded in Python)
so wording is easy to tweak without touching code.

Phone-friendly, matches the existing `status.html` card layout and `style.css`.
The essential element is a native file input — no JavaScript, no app:

```html
<section class="intro">
  <h1>Very Slow Movie Player</h1>
  <p>This screen shows a movie on e-paper one frame at a time — 24 frames an
     hour, a new frame every 150 seconds. It is made to be glanced at, not
     watched.</p>
  <p>Upload a picture and it joins the queue. When its turn comes it appears on
     the panel for one 150-second slot, then the player moves on — it never
     interrupts what is already on screen. Several uploads are shown one per
     slot, in the order they arrive. When the queue empties, the movie carries
     on from where it paused. Your image is shown in grayscale, scaled to fit.</p>
</section>

<form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
  <input type="file" name="image" accept="image/*" required>
  <button type="submit">Add to queue</button>
</form>
```

- `accept="image/*"` makes a phone offer its camera and photo library.
- `enctype="multipart/form-data"` on the form is mandatory for file upload.
- Deliberately **not** using `capture=...` so existing photos can be chosen, not
  just a fresh camera shot.
- The page shows the **queue length** prominently — a count of images waiting
  — plus an estimated wait for a newly added image. Since images drain one per
  150s tick, the estimate is simply `queue_length x 150s` (show it in minutes,
  e.g. "3 images ahead — about 7.5 min until a new upload appears"). This is an
  upper-bound-ish estimate: it ignores that the slot may currently be mid-tick,
  which is fine for a rough "where will mine end up" signal. After a successful
  upload, the confirmation should state the position the image landed at (e.g.
  "added — 4th in line").
- **Empty queue (movie playing normally): the length shows `0`.** There are no
  published PNGs in `queue/`, so the count is zero and the page reads e.g.
  "Queue is empty — the movie is playing." A newly uploaded image would be next
  up, so the estimated wait is ~0 (shows on the next tick). The zero count and
  the "movie playing" message are two views of the same fact and must agree.
- The page also lists the current queue (so a visitor can see roughly where
  their image sits in line) and links back to the status page.

### `templates/status.html`

- Add a link to the upload page so the two pages are reachable from each other.

### `static/style.css`

- Minor additions only if needed for the form/queue list; reuse existing
  classes (`card`, `facts`, `muted`, `small`, `state`) where possible.

### systemd + deploy docs

- `systemd/vsmp-web.env.example`: add `VSMP_QUEUE=/path/to/queue`.
- `systemd/vsmp.env.example`: ensure the player's `--queue-dir` / `VSMP_QUEUE`
  points at the **same** directory.
- `systemd/vsmp-web.service`: the web unit now needs **write** access to the
  queue dir (e.g. `ReadWritePaths=`), and **ffmpeg** is now a runtime dependency
  of the web service, not only the player. Note both.
- `DEPLOY-WEB.md`: document the new upload page, the shared queue directory, the
  write-access requirement, ffmpeg dependency, and the size cap. Add the "run
  the webapp locally and upload from your phone" section (see §7).

---

## 6. Access control (open question, defaulting to open)

Today anything on the LAN can read the status page. The upload endpoint is a
"draw anything on my wall" capability. For a home LAN, leaving it open is the
agreed default for now. The implementation will leave a clearly marked place
where a password / shared secret would slot in (e.g. a `before_request` check
gated on an env var), so adding auth later is a small, localised change.

**Decision needed before shipping beyond the LAN**: whether to require a secret.
Not blocking local development or home use.

---

## 7. Local testing on this laptop (no e-ink hardware)

This is a hard requirement: the webapp half must be fully testable without the
panel. The design makes that clean because the webapp never touches hardware.

### Webapp + conversion + queue publishing — fully testable locally

- Run `python3 webapp.py` and open `http://localhost:8080/upload`.
- From a phone on the same wifi, open `http://<laptop-LAN-ip>:8080/upload`
  (the app already binds `0.0.0.0`).
- Upload JPEG / PNG / HEIC. Confirm a finished 800x480 grayscale PNG appears in
  `queue/`. Open it — it is exactly what the panel would show, so the
  scale/letterbox/grayscale conversion is eyeballable without the panel.
- Upload from several tabs/phones at once to exercise concurrency: every file
  must land with a unique name and no half-written file may ever appear in
  `queue/`.
- ffmpeg (JPEG/PNG/WebP decode + the shared scale/letterbox/gray pass) and
  pillow-heif (HEIC/HEIF decode) are the runtime dependencies of conversion;
  both are present on this laptop, so conversion is real, not stubbed.

### Player queue-drain logic — testable against the existing EPD stub

The repo already fakes the panel for tests: `tests/stubs/epd/` holds a stub
driver and `tests/harness.py` / `tests/test_e2e.py` run the player against it
with no hardware. The queue-drain logic is tested the same way:

- The stub records "display called with an 800x480 image" instead of driving
  GPIO.
- Assert: queue drains one image per tick in FIFO order; the movie frame counter
  does **not** advance on an image tick; the movie resumes correctly afterwards;
  the on-screen image is never swapped mid-tick.

### What genuinely cannot be verified without the panel

- How grayscale + 1-bit Floyd-Steinberg dithering actually *look* on e-paper.
- Real panel refresh timing.

These are deferred to the Pi. All logic, conversion output, and the queue
contract are exercisable on the laptop.

---

## 8. Task checklist

1. Factor the ffmpeg scale/pad/gray filter out of `extract_frame()` into a
   shared helper usable for arbitrary images (SAR 1:1).
2. `vsmp.py`: add `--queue-dir` / `QUEUE_DIR`; queue list/claim helpers;
   disposal-after-display (delete by default; optional capped archive via
   `VSMP_ARCHIVE_DIR` / `VSMP_ARCHIVE_MAX`); startup sweep of a leftover
   `.showing/` file (like `clean_frame_dir()`); play-loop top-of-tick queue
   check that displays an image without advancing the movie frame; image-tick
   logging + watchdog + schedule handling; optional `images_shown` in state and
   `summarize_state`.
3. `webapp.py`: `MAX_CONTENT_LENGTH`; `GET /upload`; `POST /upload` with ffmpeg
   conversion + atomic publish; queue listing; `VSMP_QUEUE` config; keep it
   driver-free; leave the auth hook in place but disabled.
4. `templates/upload.html`: short plain-language explainers (what the VSMP is;
   what happens to an uploaded image and how the queue works); prominent queue
   length + estimated wait; phone-friendly form + queue list; post-upload
   confirmation stating the landed position; link from `status.html`.
5. Tests: stub-based player tests for the queue-drain contract; a webapp test
   for upload → convert → atomic publish, including a HEIC fixture and a
   concurrent-upload check.
6. systemd env examples + `vsmp-web.service` (ReadWritePaths, ffmpeg dep) +
   `DEPLOY-WEB.md` updates, including the local-testing section from §7.
7. Decide access control before any non-LAN exposure (§6).

---

## 9. Suggested execution

This is a multi-file change with a real concurrency contract and a modification
to the player's frame loop, so it is worth running through an implement +
review workflow rather than editing inline, with the stub-based and webapp tests
as the verification gate. The webapp and conversion can be demoed locally
(including from a phone) before the player loop change ever reaches the Pi.
