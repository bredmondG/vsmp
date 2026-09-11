#!/usr/bin/python
# -*- coding:utf-8 -*-
import argparse
import sys
import os
import ffmpeg
import logging
from logging.handlers import RotatingFileHandler
from epd import epd7in5_V2_old
from epd import epd7in5bc
import time
from threading import Thread
from PIL import Image,ImageDraw,ImageFont,ImageEnhance
import pickle
from pathlib import Path
import socket
import subprocess
import random
        
LOG_FILE = 'log.txt'
LOG_MAX_BYTES = 5 * 1024 * 1024   # rotate once a log file reaches 5 MB
LOG_BACKUP_COUNT = 5              # keep log.txt plus log.txt.1 ... log.txt.5


def configure_logging():
    """Set up file logging that survives a restart.

    This player is expected to run unattended for weeks, and the interesting
    question after a stall is always "what did it say just before it stopped?".

    The previous setup was:

        logging.basicConfig(filename='log.txt', filemode='w', ...)

    ``filemode='w'`` truncates the file every time the process starts, so a
    crash-and-restart (or an SSH-in-and-restart) wiped the only record of the
    failure. Appending instead means the evidence outlives the process.

    Appending forever would eventually fill the SD card, so a RotatingFileHandler
    caps total log size at LOG_MAX_BYTES * (LOG_BACKUP_COUNT + 1).
    """
    handler = RotatingFileHandler(
        LOG_FILE,
        mode='a',                      # append: never destroy previous runs
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    # Timestamp every line. Without this, correlating a stall against
    # `dmesg` / `journalctl` output is guesswork.
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-8s %(name)s: %(message)s'
    ))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)


configure_logging()
# A restart is the single most useful thing to be able to find in the log,
# so mark it explicitly now that previous runs are no longer overwritten.
logging.info('=== vsmp starting ===')

# --- systemd watchdog --------------------------------------------------------
#
# Why this exists (recommendation 3). Two protections are already in place and
# neither covers the remaining case:
#
#   * The BUSY timeout in the display driver bounds hangs inside ReadBusy().
#   * The systemd unit restarts the player whenever it exits.
#
# What is left is a stall somewhere else. generate_frame() runs ffmpeg through
# os.popen(...).read(), which has no timeout, so a wedged ffmpeg leaves a
# perfectly healthy-looking process that never displays another frame. Nothing
# crashes and nothing exits, so a restart-on-exit supervisor never triggers.
#
# systemd's WatchdogSec= handles exactly this: it expects a WATCHDOG=1 datagram
# at least every WATCHDOG_USEC/2, and kills and restarts the service if the
# messages stop arriving.
#
# This talks to the notify socket directly rather than depending on the
# python3-systemd package, which would be a new dependency for about thirty
# lines of work.

_watchdog_sock = None
_watchdog_addr = None
_watchdog_interval_s = None


def configure_watchdog():
    """Set up the systemd watchdog, if we are running under systemd.

    A no-op when NOTIFY_SOCKET or WATCHDOG_USEC are absent, which is the case
    when vsmp.py is started by hand. Manual runs therefore behave exactly as
    they did before.
    """
    global _watchdog_sock, _watchdog_addr, _watchdog_interval_s

    addr = os.environ.get('NOTIFY_SOCKET')
    usec = os.environ.get('WATCHDOG_USEC')
    if not addr or not usec:
        logging.info("systemd watchdog not active "
                     "(NOTIFY_SOCKET/WATCHDOG_USEC not set), continuing without it")
        return

    # A leading '@' means the Linux abstract socket namespace, which Python
    # expresses as a leading NUL byte. Only the first character is replaced.
    if addr.startswith('@'):
        addr = '\0' + addr[1:]

    try:
        watchdog_s = int(usec) / 1_000_000
        _watchdog_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        _watchdog_addr = addr
        # systemd requires a ping at least every half interval. Use a third to
        # leave headroom for a slow frame without tripping the watchdog.
        _watchdog_interval_s = watchdog_s / 3
    except (OSError, ValueError):
        logging.exception("Could not set up the systemd watchdog, continuing without it")
        _watchdog_sock = None
        return

    logging.info("systemd watchdog active: WatchdogSec=%.0fs, pinging every %.0fs",
                 watchdog_s, _watchdog_interval_s)


def watchdog_ping():
    """Tell systemd the player is still alive.

    Only call this where the player has actually made progress, or where it is
    legitimately idle between frames.

    Deliberately NOT driven from a background timer thread. A thread pinging on
    a schedule would keep reporting healthy while the frame loop sat wedged in
    ffmpeg, which is precisely the failure this is supposed to catch. Tying the
    ping to real progress is the whole point.
    """
    global _watchdog_sock

    if _watchdog_sock is None:
        return
    try:
        _watchdog_sock.sendto(b'WATCHDOG=1', _watchdog_addr)
    except OSError:
        # Stop trying rather than logging this every frame. Letting the pings
        # lapse is the right outcome anyway: systemd will notice and restart
        # us, which is what we would want if the notify socket has gone.
        logging.exception("Watchdog ping failed, disabling pings. "
                          "systemd will restart the service when WatchdogSec expires.")
        _watchdog_sock = None


def sleep_between_frames(seconds):
    """Wait out the gap to the next frame, pinging the watchdog as we go.

    Split into chunks instead of one long sleep. If the ping only happened once
    per frame, WatchdogSec would have to be longer than the entire frame
    interval plus the slowest possible extraction, which makes the detection
    window needlessly coarse and ties it to the frame rate. Pinging through the
    idle period means WatchdogSec only has to cover extract-and-display.

    The process is genuinely healthy while waiting here, so pinging is honest:
    a hang shows up as extraction never finishing, which stops the pings.

    Note for recommendation 24: when the schedule moves to absolute deadlines,
    keep the chunked sleep and the ping inside it.
    """
    if seconds <= 0:
        return

    # Without a watchdog, fall back to a single sleep so manual runs are
    # unchanged.
    chunk = _watchdog_interval_s or seconds

    # monotonic() so an NTP step mid-sleep cannot stretch or collapse the wait.
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(chunk, remaining))
        watchdog_ping()


def slice_video(filename, start_range = 0):
    #cuts file into 20 equal sections
    stop_range = start_range + 20
    movie_name = filename.split(".")[0]
    file_type = "." + filename.split(".")[1]
    os.mkdir(movie_name)
    s = os.popen('ffprobe -v error -select_streams v:0 -show_entries stream=duration -of default=noprint_wrappers=1:nokey=1 {}'.format(filename))
    seconds = float(s.read().split()[0])
    duration = seconds/20
    t = 0
    for i in range(start_range, stop_range):
        print(t, t + duration)
        # -y -nostdin for the same reason as generate_frame: never block on an
        # overwrite prompt with nobody there to answer it. Re-slicing a movie
        # now replaces existing sections rather than stopping partway.
        command = 'ffmpeg -y -nostdin -i {} -ss {} -t {} -c copy {}/{}_section{}{}'.format(filename, t, duration, movie_name, movie_name, i, file_type)
        output = os.popen(command)
        print(output.read())
        t+= duration
        
def frame_count(clip, movie_name):
    fast_count = 'ffprobe -v error -select_streams v:0 -show_entries stream=nb_frames -of default=nokey=1:noprint_wrappers=1 {}/{}'.format(movie_name, clip)
    s = os.popen(
                fast_count
                ).read()
    frames = int(s.split()[0])
    return frames
        
def generate_frames(clip, frame, frame_len, movie_name):
    folder = '{}_frames'.format(movie_name)
    
    while frame < frame_len:
        if len(os.listdir(folder)) < 3000:
            if ('out_img%d.jpg' %(frame)) not in os.listdir(folder):
                # -y -nostdin as in generate_frame. This function is currently
                # unreachable (see recommendation 33) but is fixed too so it
                # cannot reintroduce the stall if anyone wires it back up.
                os.system('ffmpeg -y -nostdin -i {}/{} -vf "select=gte(n\,{})" -vframes 1 {}/out_img{}.jpg'.format(movie_name, clip, frame, folder, frame))
            frame +=1

    logging.info("generate_frames done")
    
def generate_frame(clip, frame, movie_name):
    folder = '{}_frames'.format(movie_name)
    # -y and -nostdin are load-bearing. Without them this call can stop the
    # player dead, and it is reachable in normal operation.
    #
    # display_frame only calls this when the frame file is missing OR is zero
    # bytes. A zero-byte out_imgN.jpg is exactly what a killed or interrupted
    # ffmpeg leaves behind, so the file already exists when we get here. ffmpeg
    # then refuses to clobber it, and what happens next depends on the build:
    #
    #   older ffmpeg (Raspberry Pi OS ships 4.x/5.x)
    #       prints "File '...' already exists. Overwrite? [y/N]" and reads stdin.
    #       Nobody is there to answer, so os.popen(...).read() below never
    #       returns and the player stalls forever with no error.
    #   newer ffmpeg
    #       gives up without writing the file, and the Image.open() in
    #       display_frame then fails on a zero-byte JPEG.
    #
    # Either way the run is over, and it repeats on every restart because the
    # zero-byte file is still sitting there. -y answers the question up front;
    # -nostdin stops ffmpeg waiting on stdin under any circumstances.
    os.popen('ffmpeg -y -nostdin -probesize 100M -analyzeduration 100M -i {}/{} -vf "select=gte(n\,{})" -vframes 1 {}/out_img{}.jpg'.format(movie_name, clip,frame, folder, frame)).read()
    logging.info("generated_frame: {}".format(frame))
        
        
# def display_frame(clip, frame, frame_len, progress):
#     try:        
#         epd = epd7in5_V2.EPD()
#         logging.info("init and Clear")
#         epd.init()
#         epd.Clear()

        

#     except IOError as e:
#         raise Exception(logging.info(e))
        
#     except KeyboardInterrupt:    
#         logging.info("ctrl + c:")
#         epd7in5_V2.epdconfig.module_exit()
#         exit()

def convert_image(im: Image, enhance = True):
    logging.info(f"Converting Image: {enhance}")
    if enhance:
        enhance = ImageEnhance.Contrast(im)
        enhanced_im = enhance.enhance(1)
        converted_image = enhanced_im.convert('P')
    else:
        converted_image = im

    return converted_image



# Recommendation 7. How many frames may fail back-to-back before we treat the
# problem as systemic rather than as a run of bad luck. Ten failures at one frame
# per 150s is about 25 minutes of a blank screen, which is long enough to rule
# out a transient glitch and short enough that a restart still helps.
MAX_CONSECUTIVE_FRAME_ERRORS = 10


def render_one_frame(clip, frame, epd, movie_name, folder):
    """Extract, convert and show a single frame. Raises if anything goes wrong.

    Split out of display_frame so the whole operation can be wrapped in one
    try/except. Everything in here is per-frame work: nothing touches the loop
    counter or the saved progress, so a failure part-way through leaves no
    inconsistent state behind for the caller to unpick.
    """
    frame_path = '%s/out_img%d.jpg' % (folder, frame)
    # if frame_path doesn't exist or doesn't contain anything
    if (not Path(frame_path).exists()) or (os.stat(frame_path).st_size == 0):
        generate_frame(clip, frame, movie_name)
    im = Image.open(frame_path)

    # alternate between converted and non converted image.
    # This is to give some variety
    converted_im = convert_image(im, enhance=False)
    # if frame % 2 == 0:
    #     converted_im = convert_image(im)
    # else:
    #     converted_im = convert_image(im, enhance=False)

    # This setting seemed to work better with metropolis
    # converted_im = Image.open(frame_path).convert('P')
    sized = converted_im.resize((800, 480))
    logging.info(f"Displaying image: {frame_path}")
    display_on_e_ink(epd, sized)
    os.remove(frame_path)


def discard_bad_frame(folder, frame):
    """Delete a frame image we could not use.

    Leaving a truncated or zero-byte JPEG on disk is a trap. display_frame's
    guard treats a zero-byte file as "needs extracting", so the bad file would be
    handed back to ffmpeg on a later pass -- and before -y was added to the ffmpeg
    calls, that was itself a source of silent hangs. Re-extracting is cheap;
    reasoning about a half-written JPEG is not.
    """
    frame_path = Path('%s/out_img%d.jpg' % (folder, frame))
    try:
        frame_path.unlink(missing_ok=True)
    except OSError:
        logging.exception("Could not delete the unusable frame %s", frame_path)


def display_frame(clip, frame, frame_len, progress, epd, movie_name):
    folder = '{}_frames'.format(movie_name)

    # Counts failures back-to-back, so it resets on any success. Distinct from
    # progress['errors'], which is a running total for the whole movie.
    consecutive_errors = 0

    while frame < frame_len:
        start_t = time.time()
        logging.info("section: {}".format(clip))
        logging.info("frames in section: %d" %frame_len)
        logging.info("Frame: {}".format(frame))

        try:
            render_one_frame(clip, frame, epd, movie_name, folder)
            consecutive_errors = 0

        except TimeoutError:
            # Raised by ReadBusy() when the panel never released the BUSY line.
            # This is not a bad frame -- the display itself is wedged, and no
            # amount of moving on to the next frame will help. Let it propagate
            # so the process exits and systemd restarts us with a fresh panel
            # init. Swallowing this here would undo the whole point of the BUSY
            # timeout.
            raise

        except Exception:
            # One frame failed. A months-long run should not end because of it,
            # so log it properly, record it, and move on to the next frame.
            #
            # Note the frame is SKIPPED rather than retried. Retrying the same
            # frame forever would look healthy to the watchdog -- pings would
            # keep flowing while the screen never changed -- which is a worse
            # failure than losing one frame out of roughly two hundred thousand.
            consecutive_errors += 1
            progress['errors'] = progress.get('errors', 0) + 1
            logging.exception(
                "Frame %d failed (%d in a row, %d total for this movie), skipping it",
                frame, consecutive_errors, progress['errors'])
            discard_bad_frame(folder, frame)

            if consecutive_errors >= MAX_CONSECUTIVE_FRAME_ERRORS:
                # Not bad luck any more. Something systemic is wrong: the movie
                # file has gone, the disk is full, the panel is failing. Stop,
                # so systemd restarts us and -- if it keeps happening --
                # StartLimitBurst surfaces the unit as failed instead of letting
                # it quietly skip its way through the whole film.
                raise RuntimeError(
                    "{} frames failed in a row, giving up rather than skipping "
                    "through the movie".format(consecutive_errors))

        # Reached on success and on a skipped frame alike. Advancing in both
        # cases is what stops a single bad frame blocking the run forever.
        frame += 1
        progress['frame'] = frame
        save_data('progress.pkl', progress)

        # The position is safely on disk, so the loop has demonstrably moved
        # forward. This is the right place to tell the watchdog we are alive.
        watchdog_ping()

        end_t = time.time()
        lapse = end_t - start_t
        if lapse < 150:
            logging.info("Time to generate: {}s".format(round(lapse, 2)))
            sleep_between_frames(150 - lapse)
        else:
            logging.info("Time to Generate greater than 2.5 minutes")
        logging.info(time.asctime(time.localtime(time.time())))
    logging.info("finished section: {}".format(clip))

def display_on_e_ink(epd, image_to_display):
    epd.init()
    epd.Clear()
    epd.display(epd.getbuffer(image_to_display))
    epd.sleep()

def release_display():
    """Power the panel down and close the SPI bus.

    Call this on every abnormal exit path. If we die still holding SPI and with
    the panel's 5V rail on, the next process to start (a supervisor restarting
    us, or a human over SSH) may fail to initialise the display, turning a
    one-off error into a stuck player.

    Note this is called with no arguments deliberately: only the RaspberryPi
    implementation in epdconfig.py accepts `cleanup`, the other platform
    implementations take none. The kernel reclaims the GPIO pins when the
    process exits anyway, so the useful part here is the power-down.

    Failures are logged and swallowed. This runs while we are already handling
    an error, and a cleanup failure must not mask the original problem.
    """
    try:
        epd7in5_V2_old.epdconfig.module_exit()
        logging.info("Display released (SPI closed, panel powered down)")
    except Exception:
        logging.exception("Could not release the display cleanly, continuing to exit")

def save_data(file, data):
    """Pickle `data` to `file` atomically, so a power cut cannot corrupt it.

    This is called after every single frame, which means it runs roughly 576
    times a day for months. Previously it opened the real file with mode 'wb'
    and pickled straight into it. That truncates the file to zero bytes first,
    so the player spent a small slice of every frame with progress.pkl in a
    half-written state. Losing power in that window left a truncated file, and
    load_data() then raised on the next start -- the run was over until someone
    SSHed in.

    The fix is write-then-swap: build a complete temp file, force it to disk,
    then move it into place in one indivisible step. A reader at any instant
    sees either the previous good file or the new good file, never a partial one.
    """
    target = Path(file)
    # The temp file must sit in the same directory as the target. os.replace()
    # is only atomic within a single filesystem, so writing to /tmp and moving
    # across would degrade into a non-atomic copy.
    tmp = target.with_name(target.name + '.tmp')
    try:
        with open(tmp, 'wb') as f:
            pickle.dump(data, f)
            # flush() only pushes bytes out of Python's buffer into the OS page
            # cache; fsync() is what forces them onto the SD card. Without this
            # the swap below can publish a file whose contents have not actually
            # been written yet, which is the very failure being fixed here.
            f.flush()
            os.fsync(f.fileno())

        # Atomic on POSIX: the target either still points at the old file or
        # points at the fully written new one.
        os.replace(tmp, target)

        # The rename itself also needs flushing, otherwise the directory entry
        # can be lost even though the file contents survived. Best effort --
        # not every filesystem allows fsync on a directory handle, and failing
        # to harden the rename is not worth losing an already-saved frame over.
        try:
            # For a bare filename like 'progress.pkl' this is Path('.').
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            logging.debug("Could not fsync directory for %s (harmless)", target)

    except Exception:
        # Leave no stale .tmp lying around to confuse the next run or a human
        # poking at the directory over SSH.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def load_data(file, data):
    if Path(file).exists():
        with open(file, 'rb') as f:
            return pickle.load(f)
    return data

def play_random_movie(epd, filename):
    movie_name = filename.split(".")[0]
    os.makedirs(f"{movie_name}_frames", exist_ok=True)
    file_type = "." + filename.split(".")[1]
    folder = '{}_frames'.format(movie_name)
    progress = load_data('progress.pkl', {
                        'sections' : os.listdir(movie_name),
                        'sections_ran': [],
                        'frame' : 0
                        }
                        )
    while True:
        i = random.randint(0, len(progress['sections'])-1)
        clip = '{}_section{}{}'.format(movie_name, i, file_type)
        frame = random.randint(0, frame_count(clip, movie_name) - 5)
        frame_len = frame + 1
        logging.info("Section: {}".format(clip))
        display_frame(clip, frame, frame_len, progress,epd, movie_name)

def play_movie(epd, filename):
    movie_name = filename.split(".")[0]
    os.makedirs(f"{movie_name}_frames", exist_ok=True)
    file_type = "." + filename.split(".")[1]
    progress = load_data('progress.pkl', {
                            'sections' : os.listdir(movie_name),
                            'sections_ran': [],
                            'frame' : 0
                            }
                         )
    logging.info("Playing Movie at frame: {}".format(progress['frame']))
    for i in range(0, len(progress['sections'])):
        clip = '{}_section{}{}'.format(movie_name, i, file_type)
        if clip not in progress['sections_ran']:
            frame = progress['frame']
            frame_len = frame_count(clip, movie_name)- 5
            logging.info("Section: {}".format(clip))
            display_frame(clip, frame, frame_len, progress, epd, movie_name)
            progress['sections_ran'].append(clip)
            progress['frame'] = 0
            save_data('progress.pkl', progress)
        else:
            logging.info("Already Ran: {}".format(clip))
    logging.info("movie finished")
    epd.sleep()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("filename", help="the name of the movie file with .mp4")
    args = parser.parse_args()
    filename = args.filename
    configure_watchdog()

    try:
        epd = epd7in5_V2_old.EPD()
        logging.info("init and Clear")
        epd.init()
        epd.Clear()
        play_movie(epd, filename)
        logging.info("Finished!")

    except KeyboardInterrupt:
        # A deliberate Ctrl-C is not a fault, so exit 0 to distinguish it from a
        # crash. This needs its own handler because KeyboardInterrupt inherits
        # from BaseException rather than Exception, so the handler below would
        # never catch it.
        logging.info("Interrupted by user (ctrl + c), shutting down")
        release_display()
        sys.exit(0)

    except Exception:
        # This replaces:
        #
        #     except IOError as e:
        #         raise Exception(logging.info(e))
        #
        # which had two bugs. First, logging.info() returns None, so that line
        # raised Exception(None) -- the original error's message and traceback
        # were both discarded, leaving nothing to debug from. Second, only
        # IOError was caught, so an IndexError out of frame_count (see the
        # os.listdir/.DS_Store issue), a PIL decode error, or an SPI error all
        # killed the run with a traceback on stderr, which `nohup ... 2>&1 &`
        # sends to /dev/null.
        #
        # logging.exception() writes the message AND the full traceback to
        # log.txt, which now survives restarts. The bare `raise` then re-raises
        # the original exception with its traceback intact, so the process still
        # exits non-zero and a supervisor can see it failed.
        logging.exception("Unhandled error, shutting down")
        release_display()
        raise


