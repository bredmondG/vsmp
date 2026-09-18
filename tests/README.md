# vsmp tests

```
python3 tests/run.py                    # everything, about two minutes
python3 tests/run.py test_extract       # a single module
python3 tests/make_fixtures.py          # rebuild the synthetic movie by hand
```

Needs `ffmpeg`, `ffprobe` and Pillow on the host. Does **not** need a Raspberry
Pi or an e-paper panel.

## How it runs off-Pi

`epd/epdconfig.py` reads `/proc/cpuinfo` at *import* time to choose a GPIO
backend and raises `RuntimeError` on anything that is not a Pi, so `import vsmp`
cannot work directly on a development machine. `tests/stubs/epd/` shadows the
real package on `sys.path` to get around that.

`tests/stubs/epd/epd7in5_V2_old.py` deliberately reproduces one specific
behaviour of the real driver: given an image that is not exactly 800x480,
`getbuffer()` logs a warning and returns an all-zero buffer, which after the
e-paper inversion convention means **solid black**. It does not raise. That is
why `vsmp.display_on_e_ink()` checks the size itself, and the test for that guard
is only meaningful while the stub fails the same way the hardware does. Keep the
two in sync.

`harness.py` must be imported **before** `vsmp`, because it `chdir`s into a
scratch directory first and `vsmp` writes `log.txt` relative to the working
directory at import time. Every module starts:

```python
import harness
import vsmp
```

Scratch directories live in `tests/work/<module>/` and are wiped at the start of
each run, so a failure can be picked apart afterwards. Both `tests/work/` and
`tests/fixtures/` are git-ignored, and the suite asserts it wrote nothing into
the repo.

## The optional real movie

Some properties cannot be shown with a 60-frame fixture: that extraction cost
stays flat across 171,437 frames, that a real container's `nb_frames` is
trustworthy, that the last frames of a two-hour film extract without error.

Those checks need a feature-length movie, which is not committed. Provide one
with `VSMP_TEST_MOVIE=/path/to/movie.mp4`, or leave a single `.mp4` beside the
repo and it is found automatically. Without one, the affected checks report as
`skip` and the rest of the suite still runs.

## The synthetic fixture

`make_fixtures.py` builds `fixtures/synth.mp4`: 60 frames, 25fps CFR, 640x360,
lossless h264 with an explicit 15-frame GOP.

Two details are load-bearing. Frame N is a solid gray of level `10 + 4N`, so an
extracted frame is **self-identifying** — reading one pixel says which frame came
back. That gives the suite a way to verify frame accuracy that does not depend on
ffmpeg's `select` filter being correct. And the forced GOP matters because flat
frames otherwise compress to almost all keyframes, so every seek would land
directly on one and the decode-forward-from-a-keyframe path would never be
exercised.

## Modules

| Module | Covers |
|---|---|
| `test_extract` | Timestamp maths, aspect-correct geometry, `run_tool` timeouts and quoting, `probe_video`, frame accuracy against `select=gte` ground truth, and that extraction cost does not grow with position. |
| `test_state_loop` | The `state.json` schema and every refusal-to-guess path, atomic writes, the frame loop, position verification, per-frame error tolerance, the size guard, deadline scheduling, and CLI parsing. |
| `test_e2e` | Whole-program runs: crash and resume with no repeated or skipped frame, a container that overstates its frame count, the end of a real film, and the CLI as systemd invokes it. |
| `test_watchdog` | systemd watchdog wiring and ping cadence, against a real `AF_UNIX` datagram socket. |

## Conventions worth keeping

**Negative controls.** Any check asserting an absence of failure is paired with
one proving it can fail. The atomic-write test passes trivially unless you
confirm the old implementation corrupts under the same reader; the position
check is meaningless unless tampering with the file actually trips it. These are
labelled `NEGATIVE CONTROL` and are the most valuable assertions in the suite.

**Detail strings on passes, not just failures.** `check()` prints its detail
either way, so a passing run is a readable record of measured values rather than
a wall of `ok`. That is how the `select=gte` timings ended up documented.

**Bound every subprocess.** A subprocess re-imports `vsmp` and gets the
production 150-second frame interval, *not* whatever `FRAME_INTERVAL_S` the test
module patched in its own process. An unbounded `subprocess.run` that starts
playing therefore looks like a hang for hours. `cli()` always passes a timeout
and turns one into a clear assertion failure; use `start_player()` for anything
meant to actually play, which waits for the first frame and then sends SIGINT.
This has caught us once already.

## What the suite does not cover

- **Real hardware.** No Pi, no panel, no SPI. The stub cannot tell you whether
  `epdconfig.module_exit()` really powers the panel down, whether the `ReadBusy()`
  timeout behaves against a genuinely misbehaving panel, or what a frame actually
  looks like after dithering.
- **The systemd unit.** `systemd/vsmp.service` is not validated here. Checking it
  needs `systemd-analyze verify` under real systemd, e.g. in a container.
- **Power-loss atomicity.** The state-write tests prove a concurrent reader never
  sees a partial file. They do not prove durability across a real power cut on SD
  storage.
- **Timing over long periods.** Deadline scheduling is tested at a 0.3s interval
  over 8 frames, not at 150s over months.
