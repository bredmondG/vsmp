# Implementation Plan — HEIC decode fix (approach A: pillow-heif)

## Context (verified against the code, not re-decided)

Root cause confirmed by reading `convert_image_to_panel_png()` in
`.worktrees/heic-fix/vsmp.py` (lines 698–761): the HEIF/HEIC decode goes through
the single ffmpeg pass at lines 744–758, which uses `-map 0:v:0` (line 752).
On real iPhone HEICs the HEIF demuxer exposes the main image as a grid of HEVC
tiles plus auxiliary planes; this ffmpeg build does not assemble the grid, so
`0:v:0` grabs one tile or an aux plane → washed-out/gray or black output. The
existing HEIC test fixture (`tests/test_webapp.py` lines ~140–170) is built with
macOS `sips` from a flat PNG, which produces a single-tile HEIC that decodes fine
through ffmpeg — so the suite never exercised the tiled case and missed the bug.

Fix (approach A, already chosen): decode HEIC/HEIF with the `pillow-heif` library
on all platforms; keep ffmpeg as the decode path for every other format.

Environment facts verified: venv at `/Users/P3074665/development/scratch/vsmp/.venv`
runs Python 3.9.6, pip 21.2.4, Pillow 10.3.0; host is macOS 26 on arm64.

### Dependency decision (pin, with rationale)

Pin **`pillow-heif==0.22.0`** in requirements.txt.

- pillow-heif `1.2.0` dropped Python 3.9 support, and `1.1.1` requires
  `pillow>=11.1.0` (would force a Pillow upgrade from the installed 10.3.0).
  `0.22.0` requires only `pillow>=10.1.0`, so it installs against the existing
  Pillow with no upgrade.
- `0.22.0` publishes cp39 wheels for `macosx_14_0_arm64` (installs on this macOS
  26 / arm64 dev machine) and `manylinux2014_aarch64` (Raspberry Pi), and bundles
  libheif — no system libheif needed. requires_python is `>=3.9`, so old pip
  21.2.4 resolves it.
- Note for the coder: `0.22.0` carries a known libheif-bindings advisory
  (heap OOB on crafted oversized images, fixed upstream in 1.3.0). 1.3.0+ cannot
  run on Python 3.9, so on this stack 0.22.0 is the correct pin; record this
  tradeoff in the requirements.txt comment. If `pip install pillow-heif==0.22.0`
  unexpectedly fails to find a wheel on this machine, document the failure in the
  requirements comment and in tests/README.md and still keep the 0.22.0 pin for
  the Pi (manylinux2014_aarch64 wheel exists) rather than silently switching
  versions.

### Route decision (a vs b): **route (a)** — decode to a temp full-res PNG, then run the existing ffmpeg scale/pad/gray pass

Rationale: route (a) keeps exactly one geometry+filter implementation
(`panel_geometry` + `panel_video_filter`), which is the stated design invariant
("the two can never drift", vsmp.py docstring and IMAGE-QUEUE-DESIGN.md §4).
pillow-heif only has to do what ffmpeg cannot — assemble the HEIF tile grid into
one full-resolution RGB image — and the already-trusted ffmpeg pass does the
scale/letterbox(white)/grayscale to exactly 800x480 on a plain PNG, which ffmpeg
handles correctly. This is the smallest change and does not re-implement resize
quality / letterbox / gray in Pillow. The output path, size guard contract, and
non-HEIF behavior are unchanged.

---

# Implementation Plan

- [ ] 1. Add the `pillow-heif==0.22.0` dependency.
      Append `pillow-heif==0.22.0` to `requirements.txt` with a short comment
      explaining: it is the HEIF decoder for HEIC/HEIF uploads (ffmpeg cannot
      assemble tiled iOS HEICs), it is the last release compatible with Python
      3.9 + Pillow 10.3.0, it bundles libheif, and the 1.3.0 security fix is
      unavailable on py39 (hence this pin). Place it near the Flask block or in a
      clearly-labeled new section.
      Files: `requirements.txt`
      Verify: `/Users/P3074665/development/scratch/vsmp/.venv/bin/python3 -m pip install pillow-heif==0.22.0` succeeds and
      `/Users/P3074665/development/scratch/vsmp/.venv/bin/python3 -c "import pillow_heif; print(pillow_heif.__version__)"`
      prints `0.22.0`. If install fails, follow the documented-fallback note in the context section.

- [ ] 2. Register the HEIF opener once at import, driver-free.
      In `vsmp.py`, after the existing `from PIL import Image, ImageEnhance`
      (line 34), import pillow-heif and call `register_heif_opener()` so
      `PIL.Image.open()` can decode HEIC/HEIF. Guard the import so a missing
      pillow-heif does not break `import vsmp` for the non-HEIF paths: wrap in
      try/except ImportError, set a module flag (e.g. `_HEIF_OK`) and log at
      import-is-safe time only (no logging side effects at import — mirror the
      existing "no configure_logging at import" rule; just set the flag).
      This import must stay driver-free: do NOT import epd. pillow-heif is pure
      library code, so webapp.py (which imports vsmp) remains driver-free too.
      Files: `vsmp.py`
      Verify: `/Users/P3074665/development/scratch/vsmp/.venv/bin/python3 -c "import vsmp; print(vsmp._HEIF_OK)"` prints `True`
      and does not import epd (no GPIO error).

- [ ] 3. Add a HEIF detector and the HEIF decode branch in `convert_image_to_panel_png`.
      Add a small helper (e.g. `_is_heif(src_path)`) that decides HEIF by
      actually attempting a decode rather than trusting the extension: try
      `pillow_heif.open_heif(str(src))` (or `Image.open` + `.format in {"HEIF"}`)
      and treat success as HEIF, failure as not-HEIF. Do NOT validate on the
      uploaded extension alone (contract: never trust the uploaded extension).
      Then in `convert_image_to_panel_png` (vsmp.py lines 698–761), BEFORE the
      current ffprobe+ffmpeg block, branch: if the source is HEIF, decode it to a
      full-resolution temporary PNG with pillow-heif/Pillow
      (`img = Image.open(src).convert("RGB")`; write to a temp `.png` beside the
      output, e.g. `out.with_suffix(".heif_src.png")` or a NamedTemporaryFile),
      then set the variable used by the rest of the function so the EXISTING
      ffprobe-dimensions + `panel_geometry(w, h, sar=Fraction(1), bar_color='white')`
      + `panel_video_filter` + single ffmpeg pass run on that temp PNG unchanged
      (route a). Remove the temp PNG in a finally block. For non-HEIF inputs the
      existing ffmpeg `-i` path runs exactly as today. Preserve the validation
      contract: any decode/convert failure (pillow-heif raises, or ffmpeg writes
      nothing) must raise so the caller publishes nothing — keep the existing
      final non-empty-PNG check (lines 760–763) and let pillow-heif exceptions
      propagate.
      Files: `vsmp.py`
      Verify: `/Users/P3074665/development/scratch/vsmp/.venv/bin/python3 -c "import vsmp, PIL.Image as I; p=I.new('RGB',(1234,987),(200,30,30)); p.save('/tmp/heicfix_jpg.png'); vsmp.convert_image_to_panel_png('/tmp/heicfix_jpg.png','/tmp/heicfix_out.png'); o=I.open('/tmp/heicfix_out.png'); assert o.size==(800,480) and o.mode=='L', (o.size,o.mode); print('ok non-heif')"`
      (non-HEIF still works through ffmpeg, unchanged).

- [ ] 4. Decide and apply the `-map 0:v:0` question for the general (non-HEIF) ffmpeg path.
      The HEIF branch (item 3) no longer uses ffmpeg for HEIF decode, so the
      washed-out cause is removed for HEIC without touching the general path. For
      the non-HEIF ffmpeg pass at vsmp.py line 752, keep `-map 0:v:0` as-is: it is
      correct and intended for real videos/animated inputs (take the first video
      stream), and removing it risks regressing movie-frame-style and multi-stream
      inputs. Record this as a one-line decision comment in the code next to the
      non-HEIF ffmpeg call: HEIF now bypasses ffmpeg decode, so `-map 0:v:0` stays
      for everything else. (The twin call in `extract_frame`, line 678, is movie
      playback and is out of scope — do not touch it.)
      Files: `vsmp.py`
      Verify: covered by the full suite in item 7 (test_extract, test_queue,
      test_webapp all still green — JPEG/PNG/movie-frame behavior unchanged).

- [ ] 5. Add an honest HEIF-decode test that pins the new branch.
      Replace the sips-based HEIC block in `tests/test_webapp.py` (lines ~138–172)
      so the fixture is built with pillow-heif (consistent on mac and Pi), not
      sips. Steps:
      (a) In `tests/make_fixtures.py` (or inline in test_webapp with a guard),
      add a helper that, if pillow-heif is importable, encodes a HEIC fixture from
      a synthetic full-range gradient image via
      `pillow_heif.from_pillow(img).save(path)` (or `img.save(path, format="HEIF")`
      after `register_heif_opener`). Use a gradient, not a flat fill, so a
      wrong-tile/aux-plane decode would show a collapsed tonal range.
      (b) Rewrite the HEIC upload test to use that fixture; if pillow-heif is not
      importable, `harness.skip` with a clear reason instead of silently passing.
      (c) Assert the HEIF branch is actually taken: assert `vsmp._HEIF_OK` is True
      and, by monkeypatching or a counter, that the pillow-heif decode path ran
      (e.g. spy on `_is_heif`/`Image.open` returning HEIF) rather than the ffmpeg
      `-i` path. At minimum assert the published PNG is exactly 800x480, mode 'L',
      passes `panel_guard_accepts`, AND has a real tonal range (min well below 50,
      max well above 200, and >50 distinct gray values) — not the broken mean~0
      black or mean~240 white with few distinct values.
      (d) Document in the test and in `tests/README.md` that this fixture is a
      pillow-heif-encoded HEIF which may be SINGLE-TILE (encoders typically do not
      emit a tiled grid), so it proves the pillow-heif decode branch is wired and
      produces a full-range panel PNG, but does NOT by itself reproduce the tiled
      iPhone grid; the genuinely-tiled regression is covered by the real-photo
      check in item 6. Do not pretend tiled coverage.
      Files: `tests/test_webapp.py`, `tests/make_fixtures.py` (if the encoder
      helper lives there), `tests/README.md`
      Verify: `cd /Users/P3074665/development/scratch/vsmp/vsmp/.worktrees/heic-fix && /Users/P3074665/development/scratch/vsmp/.venv/bin/python3 tests/run.py test_webapp`
      passes, including the new HEIF-branch assertions; ffmpeg stays real (not
      stubbed) for the JPEG/PNG cases.

- [ ] 6. Verify the fix against the four real iPhone HEICs (out-of-repo, do not commit).
      Write a throwaway script (NOT committed — run from /tmp) that runs each of
      `/Users/P3074665/development/scratch/vsmp/heic_photos/IMG_3380.HEIC`,
      `IMG_3381.HEIC`, `IMG_3676.HEIC`, `IMG_3677.HEIC` through
      `vsmp.convert_image_to_panel_png` and asserts each output PNG is exactly
      800x480, mode 'L', with a real tonal range (min < 50, max > 200, >50
      distinct gray values) — i.e. none of the broken mean~0 (black) or mean~240
      (white)-with-<50-distinct-values results. These photos are personal and live
      OUTSIDE the worktree: read by absolute path, never copy or commit them.
      Files: none committed (temp script under /tmp)
      Verify: all four conversions assert full-range 800x480 'L' PNGs, with
      IMG_3676 and IMG_3677 (the previously-black tiled files) now passing.

- [ ] 7. Run the full suite and confirm no regressions.
      Files: none
      Verify: `cd /Users/P3074665/development/scratch/vsmp/vsmp/.worktrees/heic-fix && /Users/P3074665/development/scratch/vsmp/.venv/bin/python3 tests/run.py`
      reports 0 failed (only the optional real-movie checks may skip). Confirm the
      repo is left clean (`git status --short` shows only the intended source/doc
      edits, no test debris, no heic_photos, no temp PNGs).

- [ ] 8. Update the docs.
      - `requirements.txt`: the pin + comment from item 1 (already done there).
      - `IMAGE-QUEUE-DESIGN.md` §4 ("Image conversion"): correct the now-wrong
        claim that ffmpeg reads HEIC uniformly. State that HEIC/HEIF is decoded by
        pillow-heif (ffmpeg on this build cannot assemble tiled iOS HEIF grids,
        producing washed-out/black output), then handed to the SAME
        panel_geometry/panel_video_filter ffmpeg scale-letterbox-gray pass; all
        other formats still decode via ffmpeg directly. Keep the size-guard and
        white-letterbox reasoning.
      - `DEPLOY-WEB.md`: update the HEIC mention (around lines 141, 166) to note
        HEIC uploads are decoded via pillow-heif on both laptop and Pi, and that
        pillow-heif is now a runtime dependency of the web service (it must be in
        the web venv). Keep the 16 MB cap wording.
      Files: `requirements.txt`, `IMAGE-QUEUE-DESIGN.md`, `DEPLOY-WEB.md`
      Verify: re-read each edited section; ensure no remaining claim that ffmpeg
      decodes HEIC. (Prose change — confirmed by reading, not by a grep count.)

## Ordering notes

Item 1 (install) gates items 2–7 (nothing imports pillow-heif until it is
installed). Item 2 (register opener) gates item 3 (the branch uses it). Item 3 is
the core fix; item 4 is a small decision/comment that depends on item 3's branch
existing. Items 5–7 are verification and must run after item 3. Item 8 (docs) is
independent of 5–7 and can be done any time after item 3, but is listed last so
the prose matches the final code. Every item leaves the tree buildable:
the suite (item 7) is the gate before finalize.

## Assumptions / gaps noted

- pillow-heif's encoder usually emits single-tile HEIF, so an in-repo tiled
  fixture cannot be guaranteed; item 5 documents this and item 6 (real photos)
  carries the genuine tiled-regression proof. This is the honest coverage split,
  per the task's instruction not to fake tiled coverage.
- If `register_heif_opener()` + `Image.open` on a given file does not reliably
  flag HEIF, fall back to `pillow_heif.open_heif()` for detection; both are in
  0.22.0's API. The coder should confirm the exact call against the installed
  version during item 3.
