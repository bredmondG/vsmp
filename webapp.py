# -*- coding:utf-8 -*-
"""A small Flask web server that shows where the movie player has got to, and
lets anyone on the LAN add an image to the queue.

It answers the same question as ``python3 vsmp.py status`` -- "where is it?" --
but over HTTP on the local network, so you can glance at it from a phone or
laptop instead of SSHing in. It also hosts the upload page: an image dropped
here joins a FIFO queue the player drains one frame-slot (150s) at a time.

Design choices worth knowing:

* It reads ``state.json`` and derives the summary through vsmp.py's own
  ``read_state()`` and ``summarize_state()``. The CLI and this page therefore
  cannot disagree: there is one source of truth, and it lives in vsmp.py.
* The ONLY thing it writes is the queue directory. It never imports the
  e-paper driver. The image conversion is done by vsmp.convert_image_to_panel_png
  (ffmpeg under the hood), so an uploaded image is prepared with the exact same
  scale/letterbox/grayscale filter the movie frames use -- the two can never
  drift (IMAGE-QUEUE-DESIGN.md section 4).
* There is no lock and no database, by design. The queue is a directory; the
  player claims the oldest file with an atomic rename. The webapp's queue-length
  count is an unlocked live snapshot -- the player may claim the oldest file
  between the moment we count and the moment we render -- and that is fine: it
  is only ever used to tell a visitor roughly where their image will land.
* A successful upload answers with a redirect (Post/Redirect/Get) to
  ``/upload?added=<name>``, so reloading the confirmation never re-submits the
  photo. The confirmation's position and preview are worked out on that GET.
* Config comes from environment variables so the systemd unit can set them from
  an env file, exactly like the player's /etc/default/vsmp.

Endpoints, kept deliberately small so there is room to grow:

    GET  /               the rendered status page (auto-refreshing)
    GET  /status.json    the derived summary as JSON
    GET  /upload         the phone-friendly landing + upload page
    POST /upload         accept one image, convert it, publish it to the queue
    GET  /queue/<name>   a queued image as it will appear on the panel (preview)
    GET  /queue.json     the queue summary (length, names, estimated wait) as JSON
    GET  /healthz        liveness check that does not depend on state.json existing
"""
import os
import re
import secrets
import threading
import time
from datetime import datetime

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, url_for)
from werkzeug.utils import secure_filename

import vsmp

app = Flask(__name__)

# A phone photo can be tens of megabytes; an unbounded upload would let a single
# request exhaust RAM. Cap the request body so an oversized upload returns a
# clean 413 instead. 16 MiB comfortably covers a modern phone photo.
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024


def _int_env(name, default):
    """An integer env var, falling back to ``default`` if unset or malformed."""
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# Where the player keeps its position. Defaults to the same STATE_FILE the
# player and CLI use, resolved against the working directory. Overridable so the
# unit can point at an absolute path.
STATE_PATH = os.environ.get('VSMP_STATE', vsmp.STATE_FILE)

# The queue directory. This MUST be the same directory the player reads
# (vsmp.QUEUE_DIR, itself overridable via VSMP_QUEUE), or the player will never
# see what the webapp publishes. We re-read VSMP_QUEUE here so a single env var
# configures both processes.
QUEUE_PATH = os.environ.get('VSMP_QUEUE', vsmp.QUEUE_DIR)

# How often the status page reloads itself, in seconds. Frames only change every
# 150s (24/hour), so refreshing every 30s is already generous. 0 disables it.
REFRESH_SECONDS = _int_env('VSMP_WEB_REFRESH', 30)

# The most images allowed to wait at once. Anyone on the LAN can upload, so
# without a cap one person could queue hundreds of pictures and push everyone
# else's wait out by hours. 30 images is 75 minutes of panel time. 0 = no cap.
QUEUE_MAX = _int_env('VSMP_QUEUE_MAX', 30)

# The exact shape generate_queue_name() produces. Anything arriving from a URL
# (the ?added= confirmation, the /queue/<name> preview) must match it before it
# is used, so a crafted value can never name a path outside the queue.
QUEUE_NAME_RE = re.compile(r'^\d{13}_\d{6}_[0-9a-f]{6}\.png$')

# The timestamp format the player writes into state.json (vsmp.local_now_iso).
_STATE_TIME_FORMAT = '%Y-%m-%dT%H:%M:%S%z'

# A per-process counter that makes generated filenames unique even when two
# uploads land in the same millisecond on the same worker. Guarded by a lock
# because gunicorn may serve requests from multiple threads.
_name_counter = 0
_name_lock = threading.Lock()


def generate_queue_name():
    """A sortable, collision-proof filename for a freshly uploaded image.

    The format is ``{ms:013d}_{counter:06d}_{rand6}.png``:

      * ``ms``      -- milliseconds since the epoch, zero-padded to 13 digits so
                       the lexical sort of the name equals arrival order. This is
                       the FIFO contract the player relies on (list_queue returns
                       names sorted lexically; IMAGE-QUEUE-DESIGN.md section 3).
      * ``counter`` -- a per-process counter so two uploads in the same
                       millisecond on one worker still differ.
      * ``rand6``   -- 6 hex random chars so two *separate* workers cannot
                       collide even within a millisecond.

    The uploaded filename is never trusted or reused; this generated name is the
    only name the file is stored under.
    """
    global _name_counter
    with _name_lock:
        _name_counter = (_name_counter + 1) % 1000000
        counter = _name_counter
    ms = int(time.time() * 1000)
    return '{:013d}_{:06d}_{}.png'.format(ms, counter, secrets.token_hex(3))


def _read_state_quietly():
    """state.json, or None if it is missing or unreadable.

    The upload page only uses state for the "shows at about 14:32" estimate, so
    a half-written or corrupt state file must not take the upload page down with
    it -- it just falls back to a plain minutes estimate.
    """
    try:
        return vsmp.read_state(STATE_PATH)
    except Exception:
        return None


def next_slot_epoch(state, now=None):
    """Epoch seconds of the player's next tick, or None if it is not known.

    The player claims the oldest queued image at the top of each tick, so this is
    when position 1 in the queue will show. None when there is no state, no
    scheduled frame (finished movie, player not started), or the scheduled time
    is more than a full interval in the past -- that means the player is not
    running, and promising a clock time would be a lie. A deadline a few seconds
    in the past just means a tick is in progress, so it is clamped to now.
    """
    if not state:
        return None
    raw = state.get('next_frame_local')
    if not raw:
        return None
    try:
        when = datetime.strptime(raw, _STATE_TIME_FORMAT).timestamp()
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    if when < now - vsmp.FRAME_INTERVAL_S:
        return None
    return max(when, now)


def _added_epoch(name):
    """When a queued image was uploaded, read back from its generated name."""
    try:
        return int(name.split('_', 1)[0]) / 1000.0
    except ValueError:
        return None


def list_queue_summary(queue_path, next_slot=None):
    """The one shared view of the queue used by the pages and by /queue.json.

    Returns a dict with:
      * ``names``          -- published image names in FIFO order.
      * ``length``         -- how many images are waiting. Counts ONLY files
                              directly in <queue>/ (vsmp.list_queue excludes the
                              .incoming/ and .showing/ dotfile subdirs), so a
                              file mid-conversion or mid-display never inflates
                              the number a visitor sees.
      * ``wait_seconds``   -- length * one frame interval (150s). This is the
                              single formula; the template and /queue.json both
                              read it rather than recomputing.
      * ``wait_minutes``   -- the same, in minutes, for display.
      * ``entries``        -- per-image dicts (name, position, added_epoch,
                              shows_epoch) for the "waiting now" list. Not
                              called ``items``: in Jinja ``queue.items`` would
                              resolve to the dict's .items() method.
      * ``next_new_epoch`` -- when an image uploaded right now would show, or
                              None if the player's schedule is unknown.

    ``next_slot`` is the epoch of the player's next tick (next_slot_epoch). When
    given, the clock-time fields are filled in: position N shows at
    next_slot + (N - 1) * interval, because the player claims one image per tick.

    This is an unlocked live snapshot by design (see the module docstring): the
    player may claim the oldest file between this read and the next render, which
    keeps the webapp's no-lock contract.
    """
    names = [p.name for p in vsmp.list_queue(queue_path)]
    length = len(names)
    wait_seconds = length * vsmp.FRAME_INTERVAL_S
    interval = vsmp.FRAME_INTERVAL_S

    entries = []
    for index, name in enumerate(names):
        entries.append({
            'name': name,
            'position': index + 1,
            'added_epoch': _added_epoch(name),
            'shows_epoch': (next_slot + index * interval
                            if next_slot is not None else None),
        })

    return {
        'names': names,
        'length': length,
        'wait_seconds': wait_seconds,
        'wait_minutes': wait_seconds / 60.0,
        'entries': entries,
        'next_new_epoch': (next_slot + length * interval
                           if next_slot is not None else None),
    }


def upload_secret_required():
    """True when VSMP_UPLOAD_SECRET is set. Read per request so it can be
    toggled by restarting the service (or by a test) without code changes."""
    return bool(os.environ.get('VSMP_UPLOAD_SECRET'))


def upload_secret_ok():
    """The coarse shared-secret gate for POST /upload. DISABLED by default.

    IMAGE-QUEUE-DESIGN.md section 6: for LAN use there is no auth -- anyone who
    can reach the page can upload, which is the whole point. If you ever expose
    this beyond a trusted network, set VSMP_UPLOAD_SECRET and uploads must carry
    it as an ``X-Upload-Secret`` header or a ``secret`` form/query field (the
    upload page shows a password box for it). Only uploads are gated; the
    status pages, JSON and /healthz stay readable.

    This is intentionally a coarse gate, not a real auth system. A production
    exposure would want something stronger (a reverse proxy with real auth, say).
    """
    expected = os.environ.get('VSMP_UPLOAD_SECRET')
    if not expected:
        return True  # no secret configured -> wide open, the LAN default
    provided = (request.headers.get('X-Upload-Secret')
                or request.form.get('secret')
                or request.args.get('secret'))
    if not provided:
        return False
    # compare_digest on bytes, so a non-ASCII guess cannot raise TypeError.
    return secrets.compare_digest(provided.encode('utf-8'),
                                  expected.encode('utf-8'))


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

@app.context_processor
def inject_constants():
    """Numbers the templates quote, taken from the code so the copy can never
    drift from the real cadence or limits."""
    max_bytes = app.config['MAX_CONTENT_LENGTH']
    return {
        'frame_interval_s': int(vsmp.FRAME_INTERVAL_S),
        'frames_per_hour': vsmp.FRAMES_PER_HOUR,
        'max_upload_bytes': max_bytes,
        'max_upload_mb': max_bytes // (1024 * 1024),
        'queue_max': QUEUE_MAX,
        'secret_required': upload_secret_required(),
    }


@app.template_filter('ordinal')
def ordinal(n):
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 111 -> '111th', 122 -> '122nd'."""
    n = int(n)
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return '{}{}'.format(n, suffix)


def _to_local_datetime(value):
    """An epoch number or a state.json timestamp string -> aware datetime."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, vsmp.LOCAL_TZ)
    if isinstance(value, str):
        try:
            return datetime.strptime(value, _STATE_TIME_FORMAT).astimezone(
                vsmp.LOCAL_TZ)
        except ValueError:
            return None
    return None


@app.template_filter('clock')
def clock(value):
    """A short local wall-clock time: '14:32' today, 'Fri 30 Oct 14:32' otherwise.

    Accepts an epoch or a state.json timestamp. Anything else (e.g. the summary's
    'never' / 'not scheduled' placeholders) is passed through unchanged.
    """
    when = _to_local_datetime(value)
    if when is None:
        return '' if value is None else value
    if when.date() == datetime.now(vsmp.LOCAL_TZ).date():
        return when.strftime('%H:%M')
    return when.strftime('%a %d %b %H:%M')


@app.template_filter('ago')
def ago(epoch):
    """A relative age for an epoch: 'just now', '4 min ago', '2 h ago'."""
    if epoch is None:
        return 'a while ago'
    seconds = max(0, time.time() - epoch)
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return '{} min ago'.format(int(seconds // 60))
    return '{} h ago'.format(int(seconds // 3600))


@app.template_filter('until')
def until(epoch):
    """A relative wait to an epoch: 'any moment', 'in about 8 min'."""
    seconds = epoch - time.time()
    if seconds < 60:
        return 'any moment'
    return 'in about {} min'.format(int(round(seconds / 60.0)))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    """Render the status page, or a friendly 'nothing yet' page if the player
    has not written a frame."""
    state = vsmp.read_state(STATE_PATH)
    summary = vsmp.summarize_state(state) if state is not None else None
    return render_template(
        'status.html',
        summary=summary,
        queue=list_queue_summary(QUEUE_PATH, next_slot_epoch(state)),
        refresh=REFRESH_SECONDS,
        state_path=STATE_PATH,
    )


@app.route('/status.json')
def status_json():
    """The derived summary as JSON. Returns 404 with a clear message when there
    is no state file yet, so a caller can tell 'not started' from 'broken'."""
    state = vsmp.read_state(STATE_PATH)
    if state is None:
        return jsonify(error='no state yet',
                       detail='the player has not written {} yet'.format(STATE_PATH)), 404
    return jsonify(vsmp.summarize_state(state))


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _render_upload(status=200, error=None, added=None):
    """The single way the upload page is rendered: plain, error, or confirmation.

    ``added`` is a generated queue name from the post-upload redirect. Its
    position is looked up in a fresh snapshot; if it is no longer queued the
    player has already claimed it, and the page says it has had its turn.
    """
    queue = list_queue_summary(QUEUE_PATH, next_slot_epoch(_read_state_quietly()))
    added_item = None
    if added:
        added_item = next((item for item in queue['entries']
                           if item['name'] == added), None)
    queue_full = bool(QUEUE_MAX) and queue['length'] >= QUEUE_MAX
    return render_template(
        'upload.html',
        queue=queue,
        added=added,
        added_item=added_item,
        queue_full=queue_full,
        error=error,
    ), status


@app.route('/upload', methods=['GET'])
def upload():
    """The phone-friendly landing + upload page.

    With ``?added=<name>`` (the redirect target after a successful POST) it also
    shows the confirmation: place in line, estimated show time, and a preview of
    the converted image. A value that is not a generated queue name is ignored.
    """
    added = request.args.get('added')
    if added and not QUEUE_NAME_RE.match(added):
        added = None
    return _render_upload(added=added)


@app.route('/upload', methods=['POST'])
def upload_post():
    """Accept one image, convert it with real ffmpeg, publish it atomically.

    The flow, which never leaves a partial file where the player could see it:

      1. Check the optional shared secret and the queue cap.
      2. Require a file field named 'image'.
      3. Save the raw upload into <queue>/.incoming/ (a dotfile subdir the
         player ignores).
      4. Convert it with vsmp.convert_image_to_panel_png into another .incoming/
         temp file. Validation is "did ffmpeg produce a non-empty 800x480 PNG",
         not the uploaded extension/MIME -- a lie about the file type cannot get
         a bad image onto the panel.
      5. os.rename the finished PNG into <queue>/ under a generated name. Rename
         within one filesystem is atomic, so the player only ever sees a
         complete file (IMAGE-QUEUE-DESIGN.md section 3).
      6. Redirect (303) to the confirmation, so a reload cannot re-upload.

    On any failure nothing is published and the temp files are cleaned up.
    """
    if not upload_secret_ok():
        return _render_upload(403, error="That upload password is not right.")

    if QUEUE_MAX and len(vsmp.list_queue(QUEUE_PATH)) >= QUEUE_MAX:
        return _render_upload(429, error=(
            "The queue is full right now. Please try again in a few minutes."))

    upload_file = request.files.get('image')
    if upload_file is None or not upload_file.filename:
        return _render_upload(400, error=(
            "Please choose an image to upload (no file was received)."))

    queue_dir, incoming_dir, _ = vsmp.queue_paths(QUEUE_PATH)

    name = generate_queue_name()
    # secure_filename is used ONLY to make a safe-looking temp name from the
    # user's original filename; the published name is always the generated one.
    safe_hint = secure_filename(upload_file.filename) or 'upload'
    raw_tmp = incoming_dir / (name + '.raw-' + safe_hint)
    converted_tmp = incoming_dir / (name + '.tmp')

    try:
        upload_file.save(str(raw_tmp))
        try:
            vsmp.convert_image_to_panel_png(raw_tmp, converted_tmp)
        except Exception:
            return _render_upload(400, error=(
                "That file could not be converted into a panel image. Please "
                "upload a photo or picture (JPEG, PNG, HEIC, WebP)."))

        os.rename(converted_tmp, queue_dir / name)
    finally:
        # Always remove the raw upload and any leftover converted temp. The
        # published file has already been renamed out of .incoming/ on success,
        # so these unlinks only ever hit temp debris.
        for leftover in (raw_tmp, converted_tmp):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass

    return redirect(url_for('upload', added=name), code=303)


@app.route('/queue/<name>')
def queue_image(name):
    """A queued image exactly as it will appear on the panel, for the preview on
    the confirmation page. Only generated queue names are served, and only while
    the image is still waiting; once the player claims it this is a 404.
    """
    if not QUEUE_NAME_RE.match(name):
        abort(404)
    path = vsmp.queue_paths(QUEUE_PATH)[0] / name
    try:
        with open(path, 'rb') as f:
            magic = f.read(8)
    except FileNotFoundError:
        abort(404)
    # The converter's temp file is written by ffmpeg's image2 muxer, which picks
    # the codec from the '.tmp' name, so the bytes may be JPEG despite the .png
    # name. Sniff rather than trust the extension.
    mimetype = 'image/png' if magic.startswith(b'\x89PNG') else 'image/jpeg'
    return send_file(path, mimetype=mimetype, max_age=3600)


@app.route('/queue.json')
def queue_json():
    """The queue summary as JSON: length, ordered names, estimated wait seconds.

    Same shared formula as the page (list_queue_summary), so a script and the
    rendered page can never disagree about the wait.
    """
    summary = list_queue_summary(QUEUE_PATH)
    return jsonify(
        length=summary['length'],
        names=summary['names'],
        wait_seconds=summary['wait_seconds'],
    )


@app.errorhandler(413)
def too_large(_err):
    """An oversized upload gets the upload page with a readable message, not a
    bare JSON body -- the person hitting this is on a phone, not using curl."""
    limit_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return _render_upload(413, error=(
        "That file is too large. Uploads are capped at {} MB; try a smaller "
        "photo or a screenshot of it.".format(limit_mb)))


@app.route('/healthz')
def healthz():
    """Liveness of the web server itself, independent of the player. Useful for
    a future uptime check; deliberately does not touch state.json."""
    return jsonify(status='ok')


if __name__ == '__main__':
    # For local development only. In production the systemd unit runs this under
    # a real WSGI server (gunicorn); see systemd/vsmp-web.service. Binding to
    # 0.0.0.0 exposes it on the LAN, which is the whole point.
    host = os.environ.get('VSMP_WEB_HOST', '0.0.0.0')
    port = int(os.environ.get('VSMP_WEB_PORT', '8080'))
    app.run(host=host, port=port, debug=False)
