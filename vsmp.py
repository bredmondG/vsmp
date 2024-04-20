#!/usr/bin/python
# -*- coding:utf-8 -*-
import argparse
import sys
import os
import ffmpeg
import logging
from epd import epd7in5_V2
from epd import epd7in5bc
import time
from threading import Thread
from PIL import Image,ImageDraw,ImageFont,ImageEnhance
import traceback
import pickle
from pathlib import Path
import subprocess
import random
        
logging.basicConfig(filename='log.txt', filemode='w', level=logging.DEBUG)
logging.warning('hello log')

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
        enhance = ImageEnhance.Contrast(im)
        enhanced_im = enhance.enhance(10)
        converted_im = enhanced_im.convert('P')
        # This setting seemed to work better with metropolis
        # converted_im = Image.open(os.path.join('%s/out_img%d.jpg' % (folder, frame))).convert('P')
        sized = converted_im.resize((800,480))
        epd.display(epd.getbuffer(sized))
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

def save_data(file, data):
    with open(file, 'wb') as f:
        pickle.dump(data, f)

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
        epd = epd7in5_V2.EPD()
        logging.info("init and Clear")
        epd.init()
        epd.Clear()
        play_movie(epd, filename)

        
    except IOError as e:
        raise Exception(logging.info(e))
        
    except KeyboardInterrupt:    
        logging.info("ctrl + c:")
        epd7in5_V2.epdconfig.module_exit()
        exit()


