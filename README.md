# vsmp — very slow movie player

Displays a movie on a Waveshare 7.5" black-and-white e-paper panel (800x480)
driven by a Raspberry Pi, one frame at a time, at 24 frames per hour.

A two-hour film takes about 298 days.

## How it works

The player seeks into the movie file for each frame, scales and letterboxes it to
the panel, dithers it to 1-bit, and shows it. Position is kept in `state.json`
as an absolute frame number, written after every frame, so it resumes exactly
where it left off after a restart or a power cut.

There is **nothing to prepare**. Point it at the movie file and it plays. Earlier
versions pre-sliced the film into twenty sections; that is gone, along with the
class of ordering bugs that came with it. If you have an old `<movie>/`
directory full of `*_section*.mp4` files, it is no longer used and can be
deleted.

## First-time setup on a new Pi: enable SPI

The panel is driven over SPI, which is **off by default** on a fresh Raspberry Pi
OS install. Cloning the repo does not change that, so enable it before anything
else:

```
sudo raspi-config nonint do_spi 0
sudo reboot
```

Verify after the reboot — both nodes should exist:

```
ls -l /dev/spidev*        # expect /dev/spidev0.0 and /dev/spidev0.1
```

Without this, `epd.init()` fails immediately on `spidev.open(0, 0)`:

```
File "epd/epdconfig.py", line 105, in module_init
    self.SPI.open(0, 0)
FileNotFoundError: [Errno 2] No such file or directory
```

The missing file is `/dev/spidev0.0`. Equivalent to the command above:
`sudo raspi-config` → Interface Options → SPI, or add `dtparam=spi=on` to
`/boot/firmware/config.txt`. A reboot is required either way.

If the device node exists but you get `PermissionError` instead, the account
running the player is not in the `spi` and `gpio` groups. Check with `groups`,
then `sudo usermod -aG spi,gpio $USER` and log in again. This applies to the
`User=` account in `systemd/vsmp.service` too.

## Usage

```
python3 vsmp.py <movie>                 # play, resuming from state.json
python3 vsmp.py <movie> --restart       # start again from frame 0
python3 vsmp.py status                  # where has it got to?
python3 vsmp.py status --json           # the raw state file
```

`play` is implied, so `vsmp.py movie.mp4` and `vsmp.py play movie.mp4` are the
same thing.

### Options

| Option | What it does |
|---|---|
| `--restart` | Ignore any saved position and start from frame 0. |
| `--contrast F` | Contrast factor applied before the 1-bit dither. `1.0` is a no-op and is the default. The right value depends on the film and has to be judged by eye on the panel. |
| `--count-frames` | Count frames exactly at startup instead of trusting the container. Decodes the whole movie, so it is slow — minutes to hours on a Pi. Only worth it if the log warns that the container's count disagrees with duration x fps. |
| `--state PATH` | Use a different state file. Handy for playing two movies from one directory. |

### Checking progress

```
$ python3 vsmp.py status
movie      howls_moving_castle.mp4
progress   [##......................................] 5.132%
frame      8796 of 171437
timecode   00:06:06.783 of 01:59:09.166
state      playing
last frame 2026-09-14T14:07:53Z
next frame 2026-09-14T14:10:23Z
this run   142 frames since 2026-09-13T02:11:06Z
last frame took  extract 0.199s, display 3.104s
errors     0   anomalies 0
remaining  162641 frames, about 282.4 days at 24 frames/hour
```

`state.json` is plain JSON, so you can also just read it, and you can hand-edit
`frame` to seek. Stop the player first — it rewrites the file every frame.

`errors` counts frames that failed and were skipped. `anomalies` counts times the
position on disk did not match where the player thought it was, which usually
means two players are running at once.

## Running as a service (recommended)

Runs on boot and restarts itself if it stops. Details and all the tunables are
commented in `systemd/vsmp.service`.

Assumed layout, with the virtualenv as a sibling of the repo:

```
/home/pi/
├── venv/          # virtualenv with requirements.txt installed
└── vsmp/          # this repo
```

1. Edit the `PATHS` block in `systemd/vsmp.service` if your user or directories
   differ from the above.

2. Install the unit and its config:

   ```
   sudo cp systemd/vsmp.service /etc/systemd/system/vsmp.service
   sudo cp systemd/vsmp.env.example /etc/default/vsmp
   sudo nano /etc/default/vsmp          # set VSMP_MOVIE
   sudo systemctl daemon-reload
   sudo systemctl enable --now vsmp
   ```

3. Check it:

   ```
   systemctl status vsmp
   journalctl -u vsmp -f
   ```

Upgrading an existing Pi from the old sliced-sections version? See `DEPLOY.md`.

### Day to day

| Task | Command |
|------|---------|
| Where has it got to? | `python3 vsmp.py status` |
| Is it running? Why did it stop? | `systemctl status vsmp` |
| Follow live output | `journalctl -u vsmp -f` |
| Output since a time | `journalctl -u vsmp --since '1 hour ago'` |
| Change movie | edit `/etc/default/vsmp`, then `sudo systemctl restart vsmp` |
| Stop (stays stopped) | `sudo systemctl stop vsmp` |
| Player's own log | `tail -f log.txt` in the repo directory |

After editing `vsmp.service` itself, run `sudo systemctl daemon-reload`.

### Reading the log

One line per frame, designed to be grepped and plotted:

```
frame=8796/171437 pct=5.132 tc=00:06:06.783 extract_s=0.20 display_s=3.10 \
  next=2026-09-14T14:10:23Z errors=0 anomalies=0
```

### Notes

**Filenames with dots are fine.** Release-style names such as
`Howl's.Moving.Castle.2004.1080p.x265-Rapta.mp4` work as-is. Older versions
could not run against them at all.

**Stopping powers the panel down.** The unit sends `SIGINT` rather than
`SIGTERM` so `vsmp.py`'s interrupt handler runs, closing SPI and powering off
the panel's 5V rail. A plain `SIGTERM` would kill the process instantly and
leave the panel powered.

**When the movie finishes**, the process exits cleanly, systemd restarts it, the
new process sees `"finished": true` in `state.json` and exits again without
touching the panel. `StartLimitBurst` stops this becoming a loop: after 5
attempts in 10 minutes the unit enters the failed state, visible in
`systemctl status`. To play it again, add `--restart` to `VSMP_ARGS` in
`/etc/default/vsmp`, then `sudo systemctl reset-failed vsmp && sudo systemctl start vsmp`.

**Variable frame rate sources are not supported.** Frame numbers are mapped to
timestamps as `frame / fps`, which is only valid at a constant frame rate. The
player checks at startup and warns if the source looks variable; re-encode to
CFR if so.

## Tests

```
python3 tests/run.py                    # everything, about two minutes
python3 tests/run.py test_extract       # one module
```

Needs `ffmpeg`, `ffprobe` and Pillow. Does **not** need a Raspberry Pi or a
panel — `tests/stubs` stands in for the driver.

Some checks need a feature-length movie, which is not committed for obvious
reasons. They are skipped unless you provide one:

```
VSMP_TEST_MOVIE=/path/to/movie.mp4 python3 tests/run.py
```

A single `.mp4` sitting beside the repo is picked up automatically. See
`tests/README.md` for what each module covers.

## Requirements

```
pip install -r requirements.txt
```

Plus `ffmpeg` and `ffprobe` on `PATH`. On Raspberry Pi OS:
`sudo apt install ffmpeg`.

## Files

| Path | What |
|---|---|
| `vsmp.py` | The player. This is the only thing you need to run. |
| `epd/` | Vendored Waveshare drivers. `epd7in5_V2_old.py` is the one in use, with a local patch adding a timeout to `ReadBusy()`. |
| `systemd/` | Service unit and its config file. |
| `tests/` | Verification suite. |
| `DEPLOY.md` | Upgrading a Pi that is running an older version. |
| `pi-diagnose.sh` | Read-only diagnosis of a Pi whose writes do not survive a reboot. |

Superseded and kept only for history: `yojimbo.py`, `vsbp.py`, `vsmp.ipynb`,
`edit_pickle.py` (its job was reading `progress.pkl`; `state.json` is readable
without it), `progress.pkl`, `pg4300.txt`.
