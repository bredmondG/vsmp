# -*- coding:utf-8 -*-
"""A small Flask web server that shows where the movie player has got to.

It answers the same question as ``python3 vsmp.py status`` -- "where is it?" --
but over HTTP on the local network, so you can glance at it from a phone or
laptop instead of SSHing in.

Design choices worth knowing:

* It reads ``state.json`` and derives the summary through vsmp.py's own
  ``read_state()`` and ``summarize_state()``. The CLI and this page therefore
  cannot disagree: there is one source of truth, and it lives in vsmp.py.
* It never imports the e-paper driver and never writes anything. It is a pure
  reader, safe to run alongside the player (state.json is written atomically).
* Config comes from environment variables so the systemd unit can set them from
  an env file, exactly like the player's /etc/default/vsmp.

Endpoints, kept deliberately small so there is room to grow:

    GET /             the rendered status page (auto-refreshing)
    GET /status.json  the derived summary as JSON (for scripts, widgets, later
                      dashboards). Add more routes here as the project grows.
    GET /healthz      liveness check that does not depend on state.json existing
"""
import os

from flask import Flask, jsonify, render_template

import vsmp

app = Flask(__name__)

# Where the player keeps its position. Defaults to the same STATE_FILE the
# player and CLI use, resolved against the working directory. Overridable so the
# unit can point at an absolute path.
STATE_PATH = os.environ.get('VSMP_STATE', vsmp.STATE_FILE)

# How often the page reloads itself, in seconds. Frames only change every 150s
# (24/hour), so refreshing every 30s is already generous. 0 disables it.
try:
    REFRESH_SECONDS = int(os.environ.get('VSMP_WEB_REFRESH', '30'))
except ValueError:
    REFRESH_SECONDS = 30


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
