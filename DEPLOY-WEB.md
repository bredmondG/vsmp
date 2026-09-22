# Status web server

A small Flask page that shows where the player has got to, served over the local
network. It answers the same question as `python3 vsmp.py status`, but from a
browser so you can glance at it from a phone or laptop without SSHing in.

It reads `state.json` through vsmp.py's own `read_state()` and
`summarize_state()`, so the page and the CLI can never disagree. It writes
nothing and never touches the panel or GPIO, so it is safe to run alongside the
player as a separate service.

## What it serves

| Route          | Purpose                                                         |
|----------------|-----------------------------------------------------------------|
| `/`            | Rendered status page, auto-refreshing (default every 30s).      |
| `/status.json` | The derived summary as JSON. Good base for scripts or a widget. |
| `/healthz`     | Liveness of the server itself, independent of `state.json`.     |

## Install the dependencies

The web server needs a few packages the player did not. They are pinned in
`requirements.txt` under the "Status web server" block. Install into the same
venv the player uses:

```
cd ~/vsmp
../venv/bin/pip install -r requirements.txt
```

That adds Flask, its runtime deps, and gunicorn (the production WSGI server the
unit runs). Nothing here is needed by the player itself.

## Try it by hand first

Confirm it works before wiring up systemd. From the repo, with the venv:

```
cd ~/vsmp
../venv/bin/python webapp.py
```

That starts Flask's built-in server on `0.0.0.0:8080`. From another device on
the network, browse to `http://<pi-hostname>:8080/` (or the Pi's IP). You should
see the status card. If the player has not written a `state.json` yet, the page
says so rather than erroring.

`Ctrl-C` to stop. The built-in server is for this hand-test only; the service
below uses gunicorn.

## Install the service

Runs under gunicorn, survives reboot, restarts on crash — same shape as the
player unit. Edit the `PATHS` block in `systemd/vsmp-web.service` so `User=` and
`WorkingDirectory=` and the gunicorn path match your machine (defaults assume
`/home/pi/vsmp` and `/home/pi/venv`).

```
sudo cp systemd/vsmp-web.service /etc/systemd/system/vsmp-web.service
sudo cp systemd/vsmp-web.env.example /etc/default/vsmp-web   # optional
sudo systemctl daemon-reload
sudo systemctl enable --now vsmp-web
```

`enable` makes it come back after a reboot. Check it took:

```
systemctl status vsmp-web
journalctl -u vsmp-web -f
curl -s localhost:8080/healthz        # {"status":"ok"}
curl -s localhost:8080/status.json    # the summary, or a 404 if no state yet
```

Then browse to `http://<pi-hostname>:8080/` from any device on the LAN.

## Configuration

All optional — the unit ships working defaults. Set these in
`/etc/default/vsmp-web` (see `systemd/vsmp-web.env.example`) and
`sudo systemctl restart vsmp-web`:

| Variable           | Default          | Meaning                                            |
|--------------------|------------------|----------------------------------------------------|
| `VSMP_WEB_BIND`    | `0.0.0.0:8080`   | gunicorn listen address. `127.0.0.1:8080` = local. |
| `VSMP_STATE`       | `state.json`     | State file to read (relative to the repo).         |
| `VSMP_WEB_REFRESH` | `30`             | Page auto-refresh in seconds; `0` disables it.     |

## A note on access

Bound to `0.0.0.0` on port 8080, the page is reachable by anything on your LAN,
with no authentication. On a trusted home network that is the intended, simple
setup. If you ever want it exposed beyond the LAN, do not open the port
directly — put it behind a reverse proxy with TLS and auth, and set
`VSMP_WEB_BIND=127.0.0.1:8080` so only the proxy can reach gunicorn.

## Extending it later

The app is one small module (`webapp.py`) plus a template and a stylesheet, kept
deliberately minimal so there is room to grow:

- **More data on the page**: add fields to `summarize_state()` in `vsmp.py` and
  they appear to both the CLI and the web page at once.
- **New endpoints** (a history graph, a log tail, a JSON feed for a desk
  widget): add routes to `webapp.py`. `/status.json` is already there as a
  starting point.
- **If a route needs to write** (a cache, an uploaded movie): relax the
  hardening in the unit — swap `ReadOnlyPaths` for `ReadWritePaths` on the
  specific path — since the service currently runs read-only by design.
