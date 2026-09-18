"""Build the synthetic test movie.

Run automatically by the suite; also runnable by hand:

    python3 tests/make_fixtures.py

Frame N is a solid gray whose level increases with N. That makes an extracted
frame self-identifying: reading one pixel says which frame came back, which
gives the suite a way to check frame accuracy that does not depend on ffmpeg's
own select filter being correct.

Encoded with an explicit 15-frame GOP. Without that, flat frames compress to
almost all keyframes and every seek would land directly on one, so the test
would not exercise the decode-forward-from-a-keyframe path that makes input
seeking bounded rather than free.

Not committed to git: it is a generated artifact, and tests/.gitignore excludes
the fixtures directory.
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image

FRAMES = 60
FPS = 25
WIDTH, HEIGHT = 640, 360      # 16:9, so scaling to the 800x480 panel letterboxes
GOP = 15

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / 'fixtures'
OUT = FIXTURES / 'synth.mp4'


def gray_level(n):
    """The gray value for frame n. Strictly increasing, and spread wide enough
    that lossy artifacts could not reorder two frames."""
    return 10 + n * 4          # 10..246 over 60 frames


def main():
    src = FIXTURES / 'src_frames'
    src.mkdir(parents=True, exist_ok=True)
    for n in range(FRAMES):
        Image.new('L', (WIDTH, HEIGHT), gray_level(n)).save(
            src / 'f{:04d}.png'.format(n))

    subprocess.run([
        'ffmpeg', '-y', '-nostdin', '-loglevel', 'error',
        '-framerate', str(FPS),
        '-i', str(src / 'f%04d.png'),
        '-c:v', 'libx264', '-crf', '0',            # lossless: levels survive
        '-g', str(GOP), '-keyint_min', str(GOP),
        '-pix_fmt', 'yuv420p',
        '-r', str(FPS),                            # force constant frame rate
        str(OUT),
    ], check=True, stdin=subprocess.DEVNULL)

    for stale in src.glob('*.png'):
        stale.unlink()
    src.rmdir()

    print("wrote {} -- {} frames at {}fps, {}x{}, GOP {}".format(
        OUT, FRAMES, FPS, WIDTH, HEIGHT, GOP))


if __name__ == '__main__':
    main()
