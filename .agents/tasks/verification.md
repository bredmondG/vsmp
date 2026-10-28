# Verification — HEIC decode fix (approach A: pillow-heif)

Branch: `heic-decode-fix`. All commands run from inside the worktree with the
venv at `/Users/P3074665/development/scratch/vsmp/.venv` (the `python3` on PATH).

## Dependency install

`pillow-heif==0.22.0` installed cleanly on this machine (pip 21.2.4, Python
3.9.6): it pulled the `cp39 / macosx_14_0_arm64` wheel and left the existing
Pillow 10.3.0 untouched (`pillow>=10.1.0` satisfied). Same pin serves the Pi
(`manylinux2014_aarch64` wheel). No version discrepancy between laptop and Pi.

```
import pillow_heif -> 0.22.0
import vsmp -> _HEIF_OK = True, and the epd driver is NOT imported
```

## Four real iPhone HEICs (OUT of repo, NOT committed)

Each of the four photos at `/Users/P3074665/development/scratch/vsmp/heic_photos/`
was run through `vsmp.convert_image_to_panel_png` (read by absolute path, never
copied into the repo). Every output is exactly 800x480, mode 'L', with a real
tonal range (min < 50, max > 200, > 50 distinct gray values) — none of the
broken mean~0 (black) or mean~240 (white)-with-few-distinct-values results. The
previously-black tiled files IMG_3676 and IMG_3677 now decode correctly.

| file          | size    | mode | min | max | distinct | mean  | result |
|---------------|---------|------|-----|-----|----------|-------|--------|
| IMG_3380.HEIC | 800x480 | L    | 0   | 255 | 256      | 190.6 | PASS   |
| IMG_3381.HEIC | 800x480 | L    | 0   | 255 | 256      | 196.8 | PASS   |
| IMG_3676.HEIC | 800x480 | L    | 2   | 255 | 254      | 119.6 | PASS   |
| IMG_3677.HEIC | 800x480 | L    | 0   | 255 | 256      | 188.7 | PASS   |

(Direct `convert_image_to_panel_png` output is mode 'L' because the output path
ends in `.png`. The webapp path writes into a `.tmp` temp file, where ffmpeg's
image2 muxer stores JPEG(RGB) — the same as the JPEG/PNG upload paths — but the
content is neutral/grayscale, R==G==B per pixel, because of the `format=gray`
filter. The panel sees a grayscale image either way.)

## Full suite (`python3 tests/run.py`)

```
TOTAL: 256 passed, 0 failed, 4 skipped
(4 skipped -- the optional real-movie checks; set VSMP_TEST_MOVIE to run them)
```

The HEIC test asserts the pillow-heif branch is the one that ran (spying on
`vsmp._is_heif`, which returned True for the `.heic` upload), the published image
is 800x480, grayscale content (every pixel R==G==B), passes the panel size guard,
and has a real tonal range (measured min=0 max=255 distinct=227 on the gradient
fixture). ffmpeg stays REAL (not stubbed) for the JPEG/PNG upload paths and the
shared scale/letterbox/gray pass.

## Honest coverage note

The in-repo HEIC fixture is pillow-heif-encoded, which emits a SINGLE-TILE HEIF.
It proves the pillow-heif decode branch is wired and produces a full-range panel
PNG, but does NOT reproduce the multi-tile grid a real iPhone HEIC uses. The
genuinely-tiled regression is covered only by the four real-photo conversions
recorded above (run by hand, not committed). This split is documented in
`tests/README.md` and in the test itself.
