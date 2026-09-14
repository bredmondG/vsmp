"""systemd watchdog wiring and ping cadence.

The failure this guards against is a spurious restart during healthy playback.
systemd wants a ping every WatchdogSec/2; if the idle gap between frames were
slept through in one block, the worst-case silence would be a whole
extract-and-display plus the 150s gap, which exceeds what WatchdogSec=600
allows.
"""
import os
import socket
import tempfile
import time

import harness
from harness import check, section

import vsmp

section("watchdog configuration")

# A real AF_UNIX datagram socket, so this exercises the actual sendto() path
# rather than a mock of it.
sock_path = os.path.join(tempfile.mkdtemp(), 'notify')
server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
server.bind(sock_path)
server.setblocking(False)


def drain():
    n = 0
    while True:
        try:
            server.recv(64)
            n += 1
        except BlockingIOError:
            return n


os.environ['NOTIFY_SOCKET'] = sock_path
os.environ['WATCHDOG_USEC'] = str(600 * 1_000_000)      # WatchdogSec=600
vsmp.configure_watchdog()

check("configured from the environment", vsmp._watchdog_sock is not None)
check("pings every WatchdogSec/3, leaving headroom over the required half",
      abs(vsmp._watchdog_interval_s - 200.0) < 0.01, vsmp._watchdog_interval_s)

vsmp.watchdog_ping()
got = server.recv(64)
check("a ping reaches the notify socket", got == b'WATCHDOG=1', repr(got))

section("ping cadence while idle between frames")

required = 600 / 2
worst_display = 4 * 30      # init, Clear, display, sleep: each ReadBusy bounded at 30s
worst_frame = vsmp.FFMPEG_TIMEOUT_S + worst_display

check("NEGATIVE CONTROL: one ping per frame would have exceeded WatchdogSec/2",
      worst_frame + vsmp.FRAME_INTERVAL_S > required,
      "worst case {}s work + {}s idle = {}s, but only {}s is allowed".format(
          worst_frame, vsmp.FRAME_INTERVAL_S,
          worst_frame + vsmp.FRAME_INTERVAL_S, required))
check("the idle sleep chunk is capped rather than set to the ping interval",
      min(vsmp._watchdog_interval_s, vsmp.PING_MAX_GAP_S) == vsmp.PING_MAX_GAP_S,
      "chunk {}s vs interval {}s".format(
          vsmp.PING_MAX_GAP_S, vsmp._watchdog_interval_s))
check("with the cap, the work alone is inside the limit",
      worst_frame + vsmp.PING_MAX_GAP_S < required,
      "{}s + {}s < {}s".format(worst_frame, vsmp.PING_MAX_GAP_S, required))

# Real short sleep with a shrunken cap, so the cadence is observed rather than
# just computed.
drain()
original_cap = vsmp.PING_MAX_GAP_S
vsmp.PING_MAX_GAP_S = 0.2
t0 = time.monotonic()
vsmp.sleep_until(time.monotonic() + 1.0)
elapsed = time.monotonic() - t0
pings = drain()
vsmp.PING_MAX_GAP_S = original_cap

check("sleep_until waits for the full deadline", abs(elapsed - 1.0) < 0.15,
      "{:.2f}s".format(elapsed))
check("sleep_until pings repeatedly through the idle gap", pings >= 4,
      "{} pings in 1.0s with a 0.2s cap".format(pings))

section("degrading gracefully without systemd")

vsmp._watchdog_sock = None
vsmp._watchdog_interval_s = None
t0 = time.monotonic()
vsmp.sleep_until(time.monotonic() + 0.3)
check("without systemd it collapses to a plain sleep",
      abs(time.monotonic() - t0 - 0.3) < 0.1,
      "so running by hand behaves as it always did")

t0 = time.monotonic()
vsmp.sleep_until(time.monotonic() - 100)
check("a deadline already in the past returns immediately",
      time.monotonic() - t0 < 0.05)

# A ping with no socket must be a no-op, not an error.
vsmp.watchdog_ping()
check("pinging with no watchdog configured is harmless", True)

# An unset NOTIFY_SOCKET must not configure anything.
os.environ.pop('NOTIFY_SOCKET', None)
os.environ.pop('WATCHDOG_USEC', None)
vsmp.configure_watchdog()
check("no NOTIFY_SOCKET means no watchdog", vsmp._watchdog_sock is None)

server.close()
harness.finish()
