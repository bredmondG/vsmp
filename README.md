# vsmp — very slow movie player

Displays a movie on a Waveshare 7.5" black-and-white e-paper panel (800x480)
driven by a Raspberry Pi, one frame at a time, at 24 frames per hour.

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

### Day to day

| Task | Command |
|------|---------|
| Is it running? Why did it stop? | `systemctl status vsmp` |
| Follow live output | `journalctl -u vsmp -f` |
| Output since a time | `journalctl -u vsmp --since '1 hour ago'` |
| Change movie | edit `/etc/default/vsmp`, then `sudo systemctl restart vsmp` |
| Stop (stays stopped) | `sudo systemctl stop vsmp` |
| Player's own log | `tail -f log.txt` in the repo directory |

After editing `vsmp.service` itself, run `sudo systemctl daemon-reload`.

### Notes

**The movie filename must not contain dots** other than the extension.
`vsmp.py` derives the movie directory from `filename.split(".")[0]`, so
`Howl's.Moving.Castle.2004.x265.mp4` is read as a directory named `Howl's`
containing clips named `Howl's_section0.Moving`. Rename to a plain basename
such as `howls.mp4`.

**Stopping powers the panel down.** The unit sends `SIGINT` rather than
`SIGTERM` so `vsmp.py`'s interrupt handler runs, closing SPI and powering off
the panel's 5V rail. A plain `SIGTERM` would kill the process instantly and
leave the panel powered.

**When the movie finishes**, the process exits cleanly, systemd restarts it, and
the new process finds every section already recorded in `progress.pkl` and exits
again. `StartLimitBurst` stops this becoming a loop: after 5 attempts in 10
minutes the unit enters the failed state, visible in `systemctl status`.

## Running by hand

For a one-off or while debugging:

```
python3 vsmp.py filename.mp4
```

The previously documented form still works, but note it does not survive a
reboot and nothing restarts it:

```
nohup python3 vsmp.py filename.mp4 > /dev/null 2>&1 &
```

Both write `log.txt` and `progress.pkl` into the current directory, so run them
from the repo directory.

## Preparing a movie

`slice_video()` in `vsmp.py` pre-splits a movie into 20 sections that
`play_movie()` then walks in order. See `RECOMMENDATIONS.md` for known issues
with the slicing and extraction path.
