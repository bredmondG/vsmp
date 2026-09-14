"""Stub Waveshare 7.5" driver, standing in for epd/epd7in5_V2_old.py.

getbuffer() deliberately reproduces the REAL driver's behaviour on a size
mismatch: it logs a warning and returns an all-zero buffer, which after the
e-paper inversion convention paints the panel SOLID BLACK rather than raising.
Tests for the size guard in vsmp.display_on_e_ink() are only meaningful if the
stub fails the same way the hardware does, so keep this in sync with
epd/epd7in5_V2_old.py:491 if that ever changes.

Failure injection, for driving vsmp.py's error paths:

    VSMP_STUB_FAIL=init,display     methods that should raise
    VSMP_STUB_FAIL_WITH=TimeoutError  what they raise (default OSError)

Every call is also appended to $VSMP_STUB_LOG, so a test can prove a method
really was reached rather than inferring it.
"""
import logging
import os

logger = logging.getLogger(__name__)

EPD_WIDTH = 800
EPD_HEIGHT = 480

CALLS = []


def _record(method):
    CALLS.append(method)
    log = os.environ.get('VSMP_STUB_LOG')
    if log:
        with open(log, 'a') as f:
            f.write(method + '\n')
    failing = [m for m in os.environ.get('VSMP_STUB_FAIL', '').split(',') if m]
    if method in failing:
        kind = os.environ.get('VSMP_STUB_FAIL_WITH', 'OSError')
        if kind == 'TimeoutError':
            # What the patched ReadBusy() raises when BUSY is never released.
            raise TimeoutError(
                "e-Paper BUSY still held after 30s, giving up (stub)")
        raise OSError("stub failure in {}".format(method))


class EPD:
    def __init__(self):
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
        self.last_buffer = None

    def init(self):
        _record('init')
        return 0

    def Clear(self):
        _record('Clear')

    def display(self, image):
        _record('display')
        self.last_buffer = image

    def sleep(self):
        _record('sleep')

    def getbuffer(self, image):
        _record('getbuffer')
        img = image
        imwidth, imheight = img.size
        if imwidth == self.width and imheight == self.height:
            img = img.convert('1')
        elif imwidth == self.height and imheight == self.width:
            img = img.rotate(90, expand=True).convert('1')
        else:
            logger.warning("Wrong image dimensions: must be "
                           + str(self.width) + "x" + str(self.height))
            # Solid black once inverted. This is the real driver's behaviour and
            # the reason vsmp.py refuses a wrongly sized image up front.
            return [0x00] * (int(self.width / 8) * self.height)

        buf = bytearray(img.tobytes('raw'))
        # PIL: 0=black, 1=white. E-paper: 0=white, 1=black.
        for i in range(len(buf)):
            buf[i] ^= 0xFF
        return buf


class _EpdConfig:
    def module_exit(self):
        _record('module_exit')


epdconfig = _EpdConfig()
