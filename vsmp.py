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
        command = 'ffmpeg -i {} -ss {} -t {} -c copy {}/{}_section{}{}'.format(filename, t, duration, movie_name, movie_name, i, file_type)
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
                os.system('ffmpeg -i {}/{} -vf "select=gte(n\,{})" -vframes 1 {}/out_img{}.jpg'.format(movie_name, clip, frame, folder, frame))
            frame +=1

    logging.info("generate_frames done")
    
def generate_frame(clip, frame, movie_name):
    folder = '{}_frames'.format(movie_name)
    os.popen('ffmpeg -probesize 100M -analyzeduration 100M -i {}/{} -vf "select=gte(n\,{})" -vframes 1 {}/out_img{}.jpg'.format(movie_name, clip,frame, folder, frame)).read()
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



def display_frame(clip, frame, frame_len, progress, epd, movie_name):
    folder = '{}_frames'.format(movie_name)
    while frame < frame_len:
        start_t = time.time()
        logging.info("section: {}".format(clip))
        logging.info("frames in section: %d" %frame_len)
        logging.info("Frame: {}".format(frame))
        frame_path = '%s/out_img%d.jpg' % (folder, frame)
        # if frame_path doesn't exist or doesn't contain anything
        if (not Path(frame_path).exists()) or (os.stat(frame_path).st_size == 0):
            generate_frame(clip,frame, movie_name)
        im = Image.open(os.path.join('%s/out_img%d.jpg' % (folder, frame)))

        # alternate between converted and non converted image. 
        # This is to give some variety
        converted_im = convert_image(im, enhance=False)
        # if frame % 2 == 0:
        #     converted_im = convert_image(im)
        # else:
        #     converted_im = convert_image(im, enhance=False)
        
        # This setting seemed to work better with metropolis
        # converted_im = Image.open(os.path.join('%s/out_img%d.jpg' % (folder, frame))).convert('P')
        sized = converted_im.resize((800,480))
        logging.info(f"Displaying image: {frame_path}")
        display_on_e_ink(epd, sized)
        os.remove(frame_path)
        frame += 1
        progress['frame'] = frame
        save_data('progress.pkl', progress)
        end_t = time.time()
        lapse = end_t - start_t
        if lapse < 150:
            logging.info("Time to generate: {}s".format(round(lapse, 2)))
            time.sleep(150 - lapse)
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


