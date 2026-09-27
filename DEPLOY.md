# Deploying to the Raspberry Pi

Covers upgrading a Pi to the current code. There are two upgrades folded into
this, and it matters which one you are doing:

- **From the original `nohup python3 vsmp.py ... &` setup** — you are getting the
  reliability work *and* the new playback architecture. Read all of it.
- **From the reliability-only version** (systemd unit, watchdog, `-y` on ffmpeg,
  but still pre-slicing into sections) — you are getting the playback rewrite.
  Sections 1, 3, 4 and 6 are the ones that changed.

Read section 1 before typing anything. It captures evidence that is destroyed
the moment you restart the player.

**No new Python packages are needed.** Everything added is standard library.
`requirements.txt` is unchanged, so the existing venv is fine.

## What changed, in one paragraph

The player no longer pre-slices the movie. It seeks directly into the single file
for each frame using ffmpeg input seeking, which costs the same anywhere in the
film instead of getting slower as it goes. Because there are no sections, the
frame number is now absolute and meaningful on its own. State moved from
`progress.pkl` to `state.json`, timing moved to absolute deadlines, the aspect
ratio is no longer stretched, and dots in filenames work. There is a `status`
subcommand.

---

## 1. Before you change anything: capture the evidence

Restarting the player destroys the current `log.txt` contents you care about, and
`progress.pkl` is about to stop being used. Copy both off first.

```
ssh pi@raspberrypi
cd ~/vsmp                      # wherever the repo lives
cp log.txt ~/log-before-upgrade.txt
cp progress.pkl ~/progress-before-upgrade.pkl
```

Then collect the rest:

```
{
  echo "=== ffmpeg version ==="
  ffmpeg -version | head -3

  echo; echo "=== undervoltage / kernel complaints ==="
  dmesg | grep -iE "under-voltage|voltage|throttl" | tail -20
  vcgencmd get_throttled 2>/dev/null    # 0x0 means never throttled

  echo; echo "=== is the old player still running? ==="
  pgrep -af "vsmp.py"

  echo; echo "=== any ffmpeg stuck at an overwrite prompt? ==="
  pgrep -af ffmpeg

  echo; echo "=== zero-byte frame files ==="
  find . -name 'out_img*.jpg' -size 0 -print

  echo; echo "=== how much disk are the old sections using? ==="
  du -sh */ 2>/dev/null | sort -h | tail -5

  echo; echo "=== disk and inodes ==="
  df -h .
  df -i .
} > ~/vsmp-preflight.txt 2>&1

cat ~/vsmp-preflight.txt
```

### What the answers mean

| Finding | Meaning |
|---|---|
| `ffmpeg` version 4.x or 5.x | Confirms the overwrite-prompt hang mechanism was available to bite you. Already fixed, and the new code no longer reuses frame files at all. |
| any `out_img*.jpg` of size 0 | A stalled extraction left one behind. Harmless now — the new code always extracts fresh and never reuses an existing file — but they are dead weight. Cleared automatically on first start. |
| `Under-voltage detected`, or `get_throttled` not `0x0` | Power supply problem. **No code change fixes this.** Replace the PSU or cable. |
| a large `<movie>/` directory of `*_section*.mp4` | The old pre-sliced sections. No longer used. Reclaim the space in step 4. |
| `df -i` showing exhausted inodes | Old frame files were never cleaned up. |

Keep both files. If the player misbehaves afterwards, the comparison is the
fastest way to tell a new problem from an old one.

---

## 2. Stop the old player

Two processes writing state and driving the panel at once will corrupt both.

```
sudo systemctl stop vsmp       # if you already have the unit installed
pkill -f "vsmp.py"             # if you are still on nohup
sleep 2
pgrep -af "vsmp.py"            # expect no output
```

If a wedged `ffmpeg` is still around it will not exit on its own:

```
pgrep -af ffmpeg
pkill -f ffmpeg                # only if it is clearly a stuck vsmp extraction
```

---

## 3. Get the new code

`progress.pkl` is tracked in git and the Pi rewrites it, so `git pull` will
refuse:

```
error: Your local changes to the following files would be overwritten by merge:
        progress.pkl
```

You already backed it up in step 1, and **the new code does not read it**, so you
can simply get it out of the way:

```
cd ~/vsmp
git stash push -- progress.pkl
git pull
git stash drop
```

The repo now ships a `.gitignore` covering `state.json`, `log.txt`, `*_frames/`
and `progress.pkl`, so this dance does not repeat. Stop tracking the pickle while
you are here:

```
git rm --cached progress.pkl
git commit -m "stop tracking runtime state"
```

---

## 4. Your position: what carries over and what does not

**Your old position does not carry over, and it is not silently guessed at.**

`progress.pkl`'s `frame` was an index *within the current section*, not an
absolute frame number. Recovering an absolute position from it would need every
section file to add up the frames in each one already played. The player
therefore ignores the pickle, logs that it is doing so, and starts from frame 0:

```
WARNING  Found the old progress.pkl but no state.json. It is being ignored, not
         migrated: its 'frame' was an index within a section, so it cannot be
         converted to an absolute frame number without the section files.
         Starting from frame 0.
```

If you want to resume near where you were, work out the absolute frame yourself
and set it. With `sections_ran` of length N and 20 equal sections:

```
python3 - <<'EOF'
import pickle
p = pickle.load(open('progress.pkl','rb'))
print("sections completed:", len(p['sections_ran']))
print("frame within the current section:", p['frame'])
EOF

# total frames in the movie
ffprobe -v error -select_streams v:0 -show_entries stream=nb_frames \
  -of default=nokey=1:noprint_wrappers=1 <movie>
```

Absolute frame is roughly `total * (sections_completed / 20) + frame`. Start the
player once so it writes a `state.json`, stop it, then edit `frame` in that file.
It is plain JSON. Treat the result as approximate — the old sections were cut on
keyframes, so the boundaries drifted from where they were supposed to be, which
is part of why this design is gone.

Most people should just accept starting over, or pick a round number.

### Reclaim the space and clear the debris

```
# the old pre-sliced sections -- no longer used by anything
du -sh <movie_name>/
rm -rf <movie_name>/

# old frame files, including the zero-byte ones
find . -name 'out_img*.jpg' -delete

# stray .DS_Store files, which used to crash the player at end of movie
find . -name '.DS_Store' -delete
```

**No renaming is needed any more.** Previous versions derived the movie
directory from `filename.split(".")[0]`, so a release-style name became
unusable and had to be renamed to a dot-free basename. That is fixed. If you
renamed your movie to work around it, you can rename it back — just keep
`VSMP_MOVIE` in step 6 matching whatever the file is actually called.

---

## 5. Smoke-test by hand before involving systemd

Confirm it runs before adding a supervisor that will restart it repeatedly. Run
in the foreground so you can see what happens.

```
cd ~/vsmp
../venv/bin/python vsmp.py "<your movie>.mp4"
```

Expect, within a few seconds:

```
=== vsmp starting ===
systemd watchdog not active (NOTIFY_SOCKET/WATCHDOG_USEC not set), continuing without it
Probed howls_moving_castle.mp4: 888x480 sar=4920/4921 dar=1.8496 fps=1199/50
  (23.9800) duration=01:59:09.166 frames=171437 (container nb_frames)
  -> scaling to 800x433 padded to 800x480
No state.json yet, starting from frame 0
init and Clear
frame=1/171437 pct=0.001 tc=00:00:00.042 extract_s=0.42 display_s=4.10
  next=2026-09-14T14:10:23Z errors=0 anomalies=0
```

The watchdog line is expected here — there is no systemd in a manual run, so it
degrades to doing nothing.

Check the probe line carefully. It is the first time the player has told you what
it thinks your movie is:

- **`fps=`** should look like a real frame rate (`24000/1001`, `1199/50`, `25/1`).
- **`scaling to WxH`** should preserve your film's shape. A 1.85:1 film becomes
  800x433 with white bars; 16:9 becomes 800x450. If it says 800x480 for a
  widescreen film, something is wrong.
- **`frames=`** should be plausible. If a warning says the container disagrees
  with duration x fps, re-run once with `--count-frames`.
- **A variable frame rate warning** is serious: frame-to-time mapping is invalid
  for VFR and the wrong frames will be shown. Re-encode to CFR.

**Note the `extract_s=` figure.** It matters for step 7. On a dev machine it is
about 0.2s anywhere in the film. On a Pi expect more, but it should be roughly
the *same* at frame 1 and frame 100,000 — that flatness is the point of the
rewrite. If it climbs as the film progresses, something is wrong.

Watch one frame appear on the panel, then `Ctrl-C`:

```
Interrupted by user (ctrl + c), shutting down
Display released (SPI closed, panel powered down)
```

Then confirm the position was recorded:

```
../venv/bin/python vsmp.py status
```

If it failed instead, the traceback is in `log.txt` as well as on screen:

```
tail -40 log.txt
```

---

## 6. Install or update the systemd service

**If you already have the unit installed, you must re-copy it.** `git pull`
updates the repo, not `/etc/systemd/system`. The unit changed: `ExecStart` now
passes `$VSMP_ARGS`, and the comments documenting `WatchdogSec` are now derived
rather than guessed.

Find your real paths rather than trusting the defaults:

```
whoami                 # the User= value
pwd                    # inside ~/vsmp: the WorkingDirectory= value
readlink -f ../venv/bin/python
```

Edit the `PATHS` block in `systemd/vsmp.service` so `User=`,
`WorkingDirectory=` and `ExecStart=` match. The file ships assuming
`/home/pi/vsmp` and `/home/pi/venv`.

```
sudo cp systemd/vsmp.service /etc/systemd/system/vsmp.service
sudo cp systemd/vsmp.env.example /etc/default/vsmp
sudo nano /etc/default/vsmp          # set VSMP_MOVIE, and VSMP_ARGS if wanted
sudo systemctl daemon-reload
sudo systemctl enable --now vsmp
```

`enable` is what makes it survive a reboot, which `nohup` never did.

If you are **keeping an existing `/etc/default/vsmp`**, it will not have
`VSMP_ARGS`. That is fine — an unset variable expands to nothing. Add it if you
want `--contrast` or `--count-frames`.

Check it took:

```
systemctl status vsmp
journalctl -u vsmp -f
```

The line proving the watchdog is wired up:

```
systemd watchdog active: WatchdogSec=600s, pinging every 200s
```

If you see `systemd watchdog not active` instead, the service is running but
unmonitored — check that `WatchdogSec=600` and `NotifyAccess=main` survived your
edits.

---

## 7. Confirm it is actually healthy

Leave it for about an hour, then check. Roughly 24 frames should have gone by.

```
python3 vsmp.py status                              # the one-command answer

systemctl show vsmp -p NRestarts --value            # expect 0
systemctl is-active vsmp                            # expect active
journalctl -u vsmp | grep -i "watchdog timeout"     # expect nothing
grep -c "^.*frame=" log.txt                         # frames advancing
grep -c "skipping it" log.txt                       # errors tolerated, not fatal
grep -i "anomal" log.txt                            # position disagreements
```

Then check extraction cost is flat, which is the whole premise of the rewrite:

```
grep -o 'frame=[0-9]*/[0-9]* .*extract_s=[0-9.]*' log.txt | head -3
grep -o 'frame=[0-9]*/[0-9]* .*extract_s=[0-9.]*' log.txt | tail -3
```

The `extract_s` values at the end should be about the same as at the start.

### Reading the results

| Observation | Interpretation | Action |
|---|---|---|
| `NRestarts` 0, frames advancing, `extract_s` flat | Working. | Nothing. |
| `extract_s` climbing steadily | Input seeking is not working as intended — possibly a container without a usable index. | Check `log.txt` for the probe line; report it. |
| `Watchdog timeout (limit 600s)` during healthy playback | Extract-and-display is slower than assumed. The 600s figure allows 240s of work (120s ffmpeg ceiling + 120s of panel refresh). | **Raise** `WatchdogSec`, do not remove it. Check `extract_s` and `display_s` to see which term is large. |
| `Start request repeated too quickly`, unit `failed` | 5 restarts in 10 minutes. If the movie just finished, this is expected and harmless. Otherwise something systemic. | `journalctl -u vsmp -n 100`. |
| `N frames failed in a row, giving up` | Ten consecutive failures. Not a bad frame — a missing movie file, a full disk, a failing panel. | Read the traceback above it. |
| `e-Paper BUSY still held after 30s` | The panel never finished a refresh. | Reseat the ribbon cable; check the power supply. This used to hang forever silently. |
| `Position check failed: state.json records frame N but the player is at M` | Something else is writing the state file. | `pgrep -af vsmp.py` — you probably have two players running. |
| `Refusing to display a WxH image on a 800x480 panel` | A geometry bug was caught before it painted the panel solid black. | Report it with the probe line from `log.txt`. |
| Frames advancing but the panel is blank | Frames are being skipped. | `grep "skipping it" log.txt` and read the tracebacks. |
| Picture looks vertically squashed | You are still on old code. | The aspect fix is in the probe line: check it says `scaling to 800x433`, not 800x480. |

Reboot once to confirm it comes back on its own:

```
sudo reboot
# after it comes up
systemctl is-active vsmp
python3 vsmp.py status
```

---

## 8. Rolling back

```
sudo systemctl stop vsmp
cd ~/vsmp
cp state.json ~/state-keep.json      # in case you come forward again
git log --oneline -5                 # find the commit to go back to
git checkout <commit>
```

**Your position does not survive going backwards**, for the same reason it did
not survive coming forwards: the old code reads `progress.pkl` and knows nothing
about `state.json`. Restore your `~/progress-before-upgrade.pkl` from step 1 if
you go back, and note the old code needs its sliced sections, which step 4 told
you to delete. Back those up first if rollback is a serious possibility.

---

## 9. What has and has not been tested

The suite is in the repo and runs off-Pi:

```
python3 tests/run.py
```

202 checks, about two minutes, no Pi or panel required. Verified on a development
machine against real ffmpeg 8.0.1, a stubbed panel, and a real 171,437-frame
film: frames extracted by input seeking are byte-identical to the old
`select=gte()` path, extraction stays at 0.13–0.21s anywhere in the film, and a
crash mid-playback resumes with no frame repeated or skipped.

**None of it has run on a real Raspberry Pi or a real e-paper panel.**
Specifically unverified:

- extraction and panel-refresh timings on Pi hardware, which is what
  `WatchdogSec=600` was sized against
- `epdconfig.module_exit()` actually powering the panel down
- the `ReadBusy()` timeout against a genuinely misbehaving panel
- crash-safety of the atomic `state.json` write against a real power cut on SD
- `systemd/vsmp.service` itself — the edited unit has not been through
  `systemd-analyze verify` under real systemd
- how the dithered image actually looks, and therefore what `--contrast` should
  be. It defaults to `1.0`, a no-op, which matches what the old code effectively
  did.

Steps 5 and 7 are what turn the first item from a guess into a measurement.
Please note what you find — the answers belong in `RECOMMENDATIONS.md`.
