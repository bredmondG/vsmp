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
* Config comes from environment variables so the systemd unit can set them from
  an env file, exactly like the player's /etc/default/vsmp.

Endpoints, kept deliberately small so there is room to grow:

    GET  /             the rendered status page (auto-refreshing)
    GET  /status.json  the derived summary as JSON
    GET  /upload       the phone-friendly landing + upload page
    POST /upload       accept one image, convert it, publish it to the queue
    GET  /queue.json   the queue summary (length, names, estimated wait) as JSON
    GET  /healthz      liveness check that does not depend on state.json existing
"""
import os
import secrets
import threading
import time

from flask import Flask, abort, jsonify, render_template, request
from werkzeug.utils import secure_filename

import vsmp

app = Flask(__name__)

# A phone photo can be tens of megabytes; an unbounded upload would let a single
# request exhaust RAM. Cap the request body so an oversized upload returns a
# clean 413 instead. 16 MiB comfortably covers a modern phone photo.
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

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
try:
    REFRESH_SECONDS = int(os.environ.get('VSMP_WEB_REFRESH', '30'))
except ValueError:
    REFRESH_SECONDS = 30

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


def list_queue_summary(queue_path):
    """The one shared view of the queue used by the page and by /queue.json.

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

    This is an unlocked live snapshot by design (see the module docstring): the
    player may claim the oldest file between this read and the next render, which
    keeps the webapp's no-lock contract.
    """
    names = [p.name for p in vsmp.list_queue(queue_path)]
    length = len(names)
    wait_seconds = length * vsmp.FRAME_INTERVAL_S
    return {
        'names': names,
        'length': length,
        'wait_seconds': wait_seconds,
        'wait_minutes': wait_seconds / 60.0,
    }


@app.before_request
def require_upload_secret():
    """DISABLED by default. The place a shared secret/password slots in.

    IMAGE-QUEUE-DESIGN.md section 6: for LAN use there is no auth -- anyone who
    can reach the page can upload, which is the whole point. If you ever expose
    this beyond a trusted network, set VSMP_UPLOAD_SECRET and this hook starts
    requiring it as an ``X-Upload-Secret`` header or a ``secret`` query/form
    field. Left off (a no-op) unless the env var is set.

    This is intentionally a coarse gate, not a real auth system. It exists so
    there is one obvious hook to harden; a production exposure would want
    something stronger (a reverse proxy with real auth, say).
    """
    expected = os.environ.get('VSMP_UPLOAD_SECRET')
    if not expected:
        return  # no secret configured -> wide open, the LAN default
    provided = (request.headers.get('X-Upload-Secret')
                or request.values.get('secret'))
    if provided is None or not secrets.compare_digest(provided, expected):
        abort(403)


@app.route('/')
def index():
    """Render the status page, or a friendly 'nothing yet' page if the player
    has not written a frame."""
    state = vsmp.read_state(STATE_PATH)
    summary = vsmp.summarize_state(state) if state is not None else None
    return render_template(
        'status.html',
        summary=summary,
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


@app.route('/upload', methods=['GET'])
def upload():
    """The phone-friendly landing + upload page.

    Shows what the VSMP is and how the queue works (both explainers live in the
    template, not here), the current queue length and estimated wait, and the
    form. All the dynamic numbers come from the single shared summary.
    """
    return render_template(
        'upload.html',
        queue=list_queue_summary(QUEUE_PATH),
        landed_position=None,
        error=None,
    )


@app.route('/upload', methods=['POST'])
def upload_post():
    """Accept one image, convert it with real ffmpeg, publish it atomically.

    The flow, which never leaves a partial file where the player could see it:

      1. Require a file field named 'image'.
      2. Save the raw upload into <queue>/.incoming/ (a dotfile subdir the
         player ignores).
      3. Convert it with vsmp.convert_image_to_panel_png into another .incoming/
         temp file. Validation is "did ffmpeg produce a non-empty 800x480 PNG",
         not the uploaded extension/MIME -- a lie about the file type cannot get
         a bad image onto the panel.
      4. os.rename the finished PNG into <queue>/ under a generated name. Rename
         within one filesystem is atomic, so the player only ever sees a
         complete file (IMAGE-QUEUE-DESIGN.md section 3).

    On any failure nothing is published and the temp files are cleaned up.
    """
    upload = request.files.get('image')
    if upload is None or not upload.filename:
        return _render_upload_error(
            "Please choose an image to upload (no file was received)."), 400

    queue_dir, incoming_dir, _ = vsmp.queue_paths(QUEUE_PATH)

    name = generate_queue_name()
    # secure_filename is used ONLY to make a safe-looking temp name from the
    # user's original filename; the published name is always the generated one.
    safe_hint = secure_filename(upload.filename) or 'upload'
    raw_tmp = incoming_dir / (name + '.raw-' + safe_hint)
    converted_tmp = incoming_dir / (name + '.tmp')

    try:
        upload.save(str(raw_tmp))
        try:
            vsmp.convert_image_to_panel_png(raw_tmp, converted_tmp)
        except Exception:
            return _render_upload_error(
                "That file could not be converted into a panel image. Please "
                "upload a photo or picture (JPEG, PNG, HEIC, WebP)."), 400

        published = queue_dir / name
        os.rename(converted_tmp, published)
    finally:
        # Always remove the raw upload and any leftover converted temp. The
        # published file has already been renamed out of .incoming/ on success,
        # so these unlinks only ever hit temp debris.
        for leftover in (raw_tmp, converted_tmp):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass

    # Landed position: where this name sits in the freshly sorted queue (1-based),
    # so the confirmation can say "added -- 4th in line". Recomputed after publish.
    summary = list_queue_summary(QUEUE_PATH)
    try:
        landed_position = summary['names'].index(name) + 1
    except ValueError:
        # The player may have already claimed it (an empty queue, fast player).
        # Then it is effectively "up next / showing now": position 1.
        landed_position = 1

    return render_template(
        'upload.html',
        queue=summary,
        landed_position=landed_position,
        error=None,
    )


def _render_upload_error(message):
    """Re-render the upload page with an error banner and a fresh queue view."""
    return render_template(
        'upload.html',
        queue=list_queue_summary(QUEUE_PATH),
        landed_position=None,
        error=message,
    )


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
    """A friendlier message than Werkzeug's default when the body cap is hit."""
    limit_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return jsonify(
        error='file too large',
        detail='uploads are capped at {} MB'.format(limit_mb)), 413


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
