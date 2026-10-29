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
| `/upload`      | Phone-friendly page to add an image to the queue (GET = form, POST = upload). |
| `/status.json` | The derived summary as JSON. Good base for scripts or a widget. |
| `/queue.json`  | The queue length, ordered names, and estimated wait as JSON.    |
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
unit runs).

**ffmpeg is now a dependency of the web server too.** `POST /upload` shells out
to `ffmpeg` (and `ffprobe`) to convert each uploaded photo into an 800x480
grayscale PNG in-request, the same way the player converts movie frames. Both
must be on the `PATH` of the web process:

```
sudo apt-get install ffmpeg
```

If you run the web server on a different host from the player, that host needs
ffmpeg installed regardless. Pillow (already pulled in with the player's
`requirements.txt`) is used to validate the result. **pillow-heif** is now a
runtime dependency of the web service too — it decodes HEIC/HEIF uploads (see
below) — so it must be in the web venv. Its wheel bundles libheif, so no system
libheif package is needed on the laptop or the Raspberry Pi; a plain
`pip install -r requirements.txt` is enough.

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

| Variable             | Default          | Meaning                                                      |
|----------------------|------------------|--------------------------------------------------------------|
| `VSMP_WEB_BIND`      | `0.0.0.0:8080`   | gunicorn listen address. `127.0.0.1:8080` = local.           |
| `VSMP_STATE`         | `state.json`     | State file to read (relative to the repo).                   |
| `VSMP_WEB_REFRESH`   | `30`             | Page auto-refresh in seconds; `0` disables it.               |
| `VSMP_QUEUE`         | `queue`          | Shared upload queue directory. MUST match the player's.      |
| `VSMP_ARCHIVE_DIR`   | _(unset)_        | If set, the player keeps shown images here instead of deleting. |
| `VSMP_ARCHIVE_MAX`   | `50`             | Cap on archived images, most-recent kept.                    |
| `VSMP_QUEUE_MAX`     | `30`             | Most images allowed to wait at once (30 = 75 min of panel time). `0` = no cap. |
| `VSMP_UPLOAD_SECRET` | _(unset)_        | If set, `POST /upload` requires this shared secret (the page shows a password box). Status pages stay open. Off = open on LAN. |

## The shared upload queue

The `/upload` page lets anyone on the LAN add an image to the player. An upload
is converted to an 800x480 grayscale PNG and dropped into a **queue directory**;
the player drains that directory one image per 150s tick, shows each for a
single tick without interrupting the current image, and returns to the movie
when the queue is empty.

For this to work the player and the web server must point at **one and the same
directory**:

- Web server: `VSMP_QUEUE` in `/etc/default/vsmp-web`.
- Player: `VSMP_QUEUE` in `/etc/default/vsmp`, or `--queue-dir` in `VSMP_ARGS`.

Set both to the same absolute path (the examples use `/home/pi/vsmp/queue`). If
they disagree, uploads land where the player never looks and are silently never
shown. The directory is created on demand and holds the published `*.png` files
plus two dotfile subdirs it manages itself: `.incoming/` (mid-conversion temp
files) and `.showing/` (an image the player has claimed for the current tick).

**The web service needs write access to this directory.** It is the only thing
the web process writes. The unit grants it with `ReadWritePaths=<queue dir>` in
`systemd/vsmp-web.service` (which replaced the old purely-`ReadOnlyPaths`
hardening). Point that line at the same path as `VSMP_QUEUE`. The player only
reads from and claims out of the queue, so no extra permission is needed there.

By default an image is **deleted** once its tick is over. Set `VSMP_ARCHIVE_DIR`
(read by the player) to keep a copy instead; the archive is pruned to the
most-recent `VSMP_ARCHIVE_MAX` files (default 50).

### Upload size cap

`webapp.py` sets `MAX_CONTENT_LENGTH` to 16 MB. A larger upload — an oversized
phone photo, say — is rejected with a `413` that re-renders the upload page with
a readable message, instead of being read into memory. The page also checks the
size in the browser before sending. A full-resolution phone JPEG or HEIC is
comfortably under the cap; raw or multi-shot files may not be.

### Queue cap and the confirmation page

When `VSMP_QUEUE_MAX` images are already waiting, the form is replaced by a
"queue is full" note and `POST /upload` answers `429`. A successful upload
redirects (`303`) to `/upload?added=<name>`, so reloading the confirmation never
uploads the photo twice. That page shows the place in line, the estimated show
time (taken from the player's `next_frame_local`; it falls back to a minutes
estimate when the player is not running), and a preview of the converted image
served from `/queue/<name>` while it is still waiting.

## Run the webapp locally and upload from your phone

You can exercise the whole upload path on a laptop with no Pi and no e-ink panel
attached — conversion is real ffmpeg writing a PNG to disk, which needs no
hardware. This is the quickest way to see the `/upload` page and confirm the
queue fills.

1. On the laptop, in the repo with the venv active, start the built-in server:

   ```
   python3 webapp.py
   ```

   It binds `0.0.0.0:8080`, so it is reachable from other devices on the same
   wifi. (ffmpeg and ffprobe must be on `PATH`; pillow-heif must be installed
   for HEIC/HEIF uploads — it is in `requirements.txt`.)

2. Find the laptop's LAN IP (`ipconfig getifaddr en0` on macOS, `hostname -I` on
   Linux) and, from a phone on the same wifi, browse to
   `http://<laptop-LAN-ip>:8080/upload`.

3. The page explains what the player is and how the queue works, shows the
   current queue length and estimated wait, and offers a file picker. Pick a
   photo (JPEG, PNG, or HEIC from an iPhone) and submit. The confirmation tells
   you where in line it landed. HEIC/HEIF from an iPhone is decoded by
   pillow-heif (libheif), on both the laptop and the Pi; every other format is
   decoded by ffmpeg.

4. Confirm a finished `800x480` grayscale PNG appeared in the queue directory:

   ```
   ls -la queue/
   python3 -c "from PIL import Image; im=Image.open('queue/<name>.png'); print(im.size, im.mode)"
   ```

   You should see `(800, 480) L`. The temp subdirs `.incoming/` and `.showing/`
   stay empty between uploads. Because there is no panel here, nothing is
   displayed — the player on the Pi is what drains the queue onto the screen.

`Ctrl-C` to stop. As with the status page, the built-in server is for this
hand-test only; production uses gunicorn via the unit.

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
- **If a route needs to write** (a cache, say): grant it the narrowest possible
  write access in the unit, the way `/upload` already does — the queue directory
  is listed under `ReadWritePaths` while the repo stays under `ReadOnlyPaths`.
  Add the specific path, do not open up the whole repo.
